"""P6-01 — eligible unprotected runs을 편집 블록으로 쪼갠다 (순수 기하).

이 프로젝트에서 **처음으로 구간을 실제 제거하는 계층**의 첫 걸음이다. 그래서
선이 가장 엄격하다(CLAUDE.md 규칙 4·9): 이 모듈은 오디오를 열지 않고 전사·VAD·
`review` 산문을 읽지 않는다. 타입 있는 `InstanceGuard`의 여집합 — "지우면 안
되는 프레임"의 보수 — 만 본다. "이 구간은 말이 아니다"라는 판단은 상류가
보호라는 형태로 이미 했고 여기서 하는 일은 기하뿐이다. 네트워크·ffmpeg·모델
호출이 없고 소스를 하나도 고치지 않는다 — 후보와 요청을 만들 뿐이다
(`scripts/check_invariants.py`의 "제거 계층은 오디오를 듣지 않는다" 검사).

신원은 내용에 묶는다. 서수 id(`block-3`)는 쓰지 않는다 — 다른 `keep_seconds`,
다른 문서 리비전, 다른 guard 세대에서 만들어진 판정이 "모르는 id" 검사를 통과해
엉뚱한 프레임을 제거한다. 공식(구간은 "start/end", 재료는 NUL로 잇는다):

    block_id     = sha256("vlogsrt-edit-block-v1", document.input_sha256,
                          instance_id, source_id, source_frames, timeline_frames)
    candidate_id = sha256("vlogsrt-edit-candidate-v1", block_id, candidate.source_frames)
    frame_id     = f"{source_id}:{grid_index}"

**프레임이 정본이고 초는 파생이다.** `KeepCandidate.source_frames`가 소스 격자의
반개 절대 구간이고 `vision.keep_start`는 `(source_frames.start_frame −
block.source_frames.start_frame) × frame_duration`로 **프레임에서** 만든다 —
반대 방향으로 나누지 않는다(P4-04의 교훈). 소스 격자에서 인덱스 n은 공용 파일
시계 `n × frame_duration`을 뜻하고 `asset.start + n × frame_duration`이 XML
애셋 시계 자리다. n을 디코드 영상 순번으로 취급하지 않고 `probe_video_stream`의
`frame_count`를 격자 끝으로 가정하지도 않는다 — 경계는 **남은 XML 클립/파일
구간**에서 온다.

설정·연결 결함은 호출자 잘못이라 `raise`로 닫는다 — keep+review로 세탁하지
않는다(P5-05b가 policy에서 그은 선). `_require_config`의 프레임 환산 뒤 관계와
교차 레일은 `ProjectConfig.__post_init__`(P5-06)이 볼 수 없는 P6 고유의
검사다. 방어는 겹쳐야 한다 — 검사받지 않는 층은 썩는다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.boundary import UnavailableInstance
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import AssetResource, ClipInstance, ProjectDocument
from vlogsrt.project.timebase import (
    FrameInterval,
    frame_ceil,
    frame_floor,
    frame_nearest,
    interval_outward,
)
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.vision.policy import (
    VisionCandidate,
    VisionRequest,
    _effective_max_images,
)
from vlogsrt.project.vision.policy import (
    _require_config as _require_policy_config,
)
from vlogsrt.project.vision.sampling import FrameSampleRequest

_BLOCK_ID_DOMAIN = "vlogsrt-edit-block-v1"
"""블록 신원의 도메인 구분자 — 다른 해시 재료와 우연히 겹치지 않게 한다."""

_CANDIDATE_ID_DOMAIN = "vlogsrt-edit-candidate-v1"
"""후보 신원의 도메인 구분자 — block_id와 같은 재료라도 갈라진다."""

_UNAVAILABLE_REASONS = frozenset({"declared_no_audio", "stream_unavailable", "pcm_unavailable"})
"""부재 사유의 닫힌 목록 — boundary가 만드는 세 토큰 밖은 계약 위반이다."""

_SOURCE_GRID_ANCHOR = Fraction(0)
"""소스·타임라인 격자의 닻 — 인덱스 n은 곧 파일/타임라인 시계 n × 프레임 길이다."""


@dataclass(frozen=True, slots=True)
class KeepCandidate:
    """유지 후보 한 창 — `source_frames`가 정본이고 `vision`의 초는 파생이다.

    `timeline_frames`는 블록의 타임라인 격자에 같은 상대 오프셋으로 대응한다.
    `vision.keep_start/keep_end`는 모델에게 보이는 블록 상대 초다:
    `keep_start = (source_frames.start_frame − block.source_frames.start_frame)
    × frame_duration`. P6-02는 프레임을 여기서 그대로 읽고 나누지 않는다.
    """

    candidate_id: str
    source_frames: FrameInterval
    timeline_frames: FrameInterval
    vision: VisionCandidate


@dataclass(frozen=True, slots=True)
class EditBlock:
    """편집 후보 블록 하나 — 아직 판정도 적용도 아니고 기하 후보다.

    모든 구간은 반개 `[start_frame, end_frame)`이고 소스 격자는 공용 파일 시계
    앵커 0이다. `sample_requests`는 시간 순서(오름차순 격자 인덱스)다 — P5-05의
    안내가 프레임을 시간 순으로 나열하기 때문이다.
    """

    block_id: str
    instance_id: str
    source_id: str
    timeline_frames: FrameInterval
    source_frames: FrameInterval
    keep_candidates: tuple[KeepCandidate, ...]
    sample_requests: tuple[FrameSampleRequest, ...]


@dataclass(frozen=True, slots=True)
class BlockPartition:
    """한 번의 분할 — 블록·명시적 잔여·부재 기록과 리비전 증명 해시.

    `kept_short_remainders`는 greedy 분할의 짧은 꼬리 `(instance_id, 구간)`이다
    — 블록도 아니고 사라진 것도 아닌 **유지**의 명시적 기록이다(규칙 9).
    `input_sha256`/`config_sha256`은 이 분할을 만든 문서·설정의 신원이고
    하류(P6-02)가 낡은 판정을 거부하는 재료다.
    """

    blocks: tuple[EditBlock, ...]
    kept_short_remainders: tuple[tuple[str, FrameInterval], ...]
    unavailable_instances: tuple[UnavailableInstance, ...]
    input_sha256: str
    config_sha256: str
    locked_instance_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Limits:
    """프레임 환산 뒤의 블록·후보 한도 — `_require_config`가 증명한 값."""

    min_frames: int
    max_frames: int
    keep_frames: int


def partition_blocks(
    document: ProjectDocument,
    media: Sequence[ResolvedMedia],
    guards: Sequence[InstanceGuard],
    locked_instance_ids: AbstractSet[str],
    unavailable_instances: Sequence[UnavailableInstance],
    cfg: ProjectConfig,
) -> BlockPartition:
    """eligible unprotected runs을 편집 블록으로 쪼갠다 — 판정이 아니라 후보다.

    문서의 클립 인스턴스마다 guard가 정확히 하나여야 하고 가청은 문서에서
    계산한다. 잠긴·부재·keep_all·불가청 인스턴스는 블록을 내지 않고, 성공한
    가청 인스턴스에서만 보호의 여집합을 자른다. 짧은 잔여는 명시적으로 유지된다.
    """
    limits = _require_config(cfg, document.frame_duration)
    media_by_resource = _media_by_resource(document, media)
    unavailable_by_instance = _require_unavailable(
        document, unavailable_instances, locked_instance_ids
    )
    guards_by_instance = _require_guard_linkage(document, media_by_resource, guards)
    blocks, remainders = _partition_instances(
        document,
        media_by_resource,
        guards_by_instance,
        unavailable_by_instance,
        locked_instance_ids,
        cfg,
        limits,
    )
    return BlockPartition(
        blocks=blocks,
        kept_short_remainders=remainders,
        unavailable_instances=tuple(unavailable_instances),
        input_sha256=document.input_sha256,
        config_sha256=cfg.sha256(),
        locked_instance_ids=frozenset(locked_instance_ids),
    )


def vision_request_for(block: EditBlock, adjacent_context: str) -> VisionRequest:
    """블록 → `VisionRequest`의 정본 투영 — 러너(P7)와 검증자(P6-02)가 같은 재료를 해시한다.

    `frame_ids`는 표본 순서 그대로, `candidates`는 후보 순서 그대로다. 다시
    고르거나 다시 정렬하지 않는다 — `policy.request_sha256`의 재료가 이 투영
    하나에서만 나와야 저장된 판정(`VisionDecision.request_sha256`)이 이 프레임과
    이 후보를 봤는지 증명한다(P5-06). 이웃 맥락 글월은 안내일 뿐 해시 재료가
    아니다.
    """
    return VisionRequest(
        block_id=block.block_id,
        frame_ids=tuple(request.frame_id for request in block.sample_requests),
        candidates=tuple(candidate.vision for candidate in block.keep_candidates),
        adjacent_context=adjacent_context,
    )


def _require_config(cfg: ProjectConfig, frame_duration: Fraction) -> _Limits:
    """P6 레일 — 설정 결함은 예외로 닫는다(keep+review 세탁 금지).

    생성자(P5-06)가 이미 범위·교차 검사를 돌려도 이 관문은 필요하다:
    (1) 프레임 환산 뒤 관계는 `frame_duration`이 문서에서 오므로 생성자가 볼 수
    없고, (2) 교차 레일 `block_max_seconds × vision_sample_fps <= 실효 프레임
    한도`는 P6 고유며("성긴 이미지는 아무것도 증명하지 못한다") — 정책이
    `_effective_max_images`로 깎은 **실효** 천장과 비교한다. 날것
    `vision_max_images`와 비교하면 32를 설정한 20초 블록이 표본 15장으로
    판정된다(P6-01b). (3) 검사받지 않는 층은 썩는다. `vision_apply_confidence`는
    policy의 관문을 그대로 재사용한다.
    """
    for name in ("block_min_seconds", "block_max_seconds", "keep_seconds", "vision_sample_fps"):
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, Fraction) or value <= 0:
            raise ValueError(
                f"설정 결함: {name}은(는) bool이 아닌 양수 Fraction이어야 한다: {value!r}"
            )
    max_images = cfg.vision_max_images
    if isinstance(max_images, bool) or not isinstance(max_images, int) or max_images < 5:
        raise ValueError(
            f"설정 결함: vision_max_images는 bool이 아닌 5 이상의 정수여야 한다"
            f"(끝점 2 + 중앙 3): {max_images!r}"
        )
    _require_policy_config(cfg)
    limits = _Limits(
        min_frames=frame_ceil(cfg.block_min_seconds, frame_duration),
        max_frames=frame_floor(cfg.block_max_seconds, frame_duration),
        keep_frames=frame_nearest(cfg.keep_seconds, frame_duration),
    )
    if not 1 <= limits.keep_frames <= limits.min_frames <= limits.max_frames:
        raise ValueError(
            "설정 결함: 프레임 환산 뒤 1 <= keep_frames <= min_frames <= max_frames가"
            f" 성립해야 한다: keep={limits.keep_frames}, min={limits.min_frames},"
            f" max={limits.max_frames} (프레임 길이 {frame_duration}초)"
        )
    ceiling = _effective_max_images(cfg)
    if cfg.block_max_seconds * cfg.vision_sample_fps > ceiling:
        raise ValueError(
            f"설정 결함: block_max_seconds({cfg.block_max_seconds}) ×"
            f" vision_sample_fps({cfg.vision_sample_fps})가 실효 프레임 한도"
            f" min(vision_max_images, 16)={ceiling}을 넘는다"
            f" (vision_max_images={cfg.vision_max_images}) —"
            " 성긴 이미지는 아무것도 증명하지 못한다"
        )
    return limits


def _media_by_resource(
    document: ProjectDocument, media: Sequence[ResolvedMedia]
) -> dict[str, ResolvedMedia]:
    """자원마다 미디어 하나 — 가청 선언의 정본은 문서의 asset이다."""
    resources = {resource.scoped_id: resource for resource in document.resources}
    indexed: dict[str, ResolvedMedia] = {}
    for row in media:
        resource = resources.get(row.resource_scoped_id)
        if resource is None:
            raise ValueError(f"문서에 없는 자원을 가진 미디어다: {row.resource_scoped_id!r}")
        if row.resource_scoped_id in indexed:
            raise ValueError(
                f"미디어 자원이 중복이다: {row.resource_scoped_id!r} — 자원마다 정확히 하나다"
            )
        if row.has_audio != resource.has_audio:
            raise ValueError(
                f"미디어의 가청 선언이 문서와 다르다: resource {resource.xml_id!r}는"
                f" hasAudio={int(resource.has_audio)}인데 media.has_audio={row.has_audio}"
                " — 조용한 무음·조용한 대사가 되므로 거부한다"
            )
        indexed[row.resource_scoped_id] = row
    for clip in _clips(document):
        if clip.resource_scoped_id not in indexed:
            raise ValueError(f"클립이 가리키는 자원에 미디어가 없다: {clip.resource_scoped_id!r}")
    return indexed


def _require_unavailable(
    document: ProjectDocument,
    records: Sequence[UnavailableInstance],
    locked: AbstractSet[str],
) -> dict[str, UnavailableInstance]:
    """부재 기록의 검증 — 문서 신원·사유·잠금 짝이 전부 맞아야 한다.

    자원 스코프 부재는 같은 내용의 건강한 guard보다 강하다: 검증은 내용 신원이
    아니라 정확한 문서 `resource_scoped_id` + `instance_id`로 하고, 통과한
    기록은 타입 있는 그대로 흘려보낸다.
    """
    resources = {resource.scoped_id for resource in document.resources}
    clip_owner = {clip.instance_id: clip.resource_scoped_id for clip in _clips(document)}
    indexed: dict[str, UnavailableInstance] = {}
    for record in records:
        if record.reason_code not in _UNAVAILABLE_REASONS:
            raise ValueError(
                f"알 수 없는 부재 사유 코드다: {record.reason_code!r} —"
                f" {sorted(_UNAVAILABLE_REASONS)}뿐이다"
            )
        if record.resource_scoped_id not in resources:
            raise ValueError(f"문서에 없는 자원의 부재 기록이다: {record.resource_scoped_id!r}")
        if clip_owner.get(record.instance_id) != record.resource_scoped_id:
            raise ValueError(
                f"부재 기록의 인스턴스가 이 자원의 클립이 아니다:"
                f" {record.instance_id!r} (자원 {record.resource_scoped_id!r})"
            )
        if record.instance_id in indexed:
            raise ValueError(f"부재 기록이 중복이다: {record.instance_id!r}")
        if record.instance_id not in locked:
            raise ValueError(
                f"부재 기록의 인스턴스가 잠금에 없다: {record.instance_id!r}"
                " — 부재는 잠금과 함께 온다"
            )
        indexed[record.instance_id] = record
    return indexed


def _require_guard_linkage(
    document: ProjectDocument,
    media_by_resource: dict[str, ResolvedMedia],
    guards: Sequence[InstanceGuard],
) -> dict[str, InstanceGuard]:
    """guard 연결 — 문서의 클립 인스턴스마다 정확히 하나, 짝은 신원 둘 다.

    없음·중복·남음·불일치는 전부 연결 오류다. 그렇지 않으면 "guard 없음"이
    조용히 "보호 없음"이 된다 — `protected=[]`가 옷만 갈아입은 것이다.
    """
    by_instance: dict[str, InstanceGuard] = {}
    for candidate in guards:
        if candidate.instance_id in by_instance:
            raise ValueError(
                f"인스턴스에 guard가 둘 이상 있다: {candidate.instance_id!r} — 정확히 하나여야 한다"
            )
        by_instance[candidate.instance_id] = candidate
    clips = _clips(document)
    for clip in clips:
        guard = by_instance.get(clip.instance_id)
        if guard is None:
            raise ValueError(
                f"클립 인스턴스에 guard가 없다: {clip.element_path}"
                " — 없음이 보호 없음이 되면 안 된다"
            )
        content = media_by_resource[clip.resource_scoped_id].asset_content_id
        if guard.source_id != content:
            raise ValueError(
                f"guard의 source_id가 클립 자원의 내용 신원과 다르다:"
                f" guard {guard.source_id!r}, 미디어 {content!r} ({clip.element_path})"
            )
    extra = sorted(set(by_instance) - {clip.instance_id for clip in clips})
    if extra:
        raise ValueError(f"문서에 없는 인스턴스를 가진 guard다: {extra[0]!r}")
    return by_instance


def _partition_instances(
    document: ProjectDocument,
    media_by_resource: dict[str, ResolvedMedia],
    guards_by_instance: dict[str, InstanceGuard],
    unavailable_by_instance: dict[str, UnavailableInstance],
    locked: AbstractSet[str],
    cfg: ProjectConfig,
    limits: _Limits,
) -> tuple[tuple[EditBlock, ...], tuple[tuple[str, FrameInterval], ...]]:
    """문서 순서대로 인스턴스를 훑는다 — 가청·연결 검사가 늘 잠금·부재보다 먼저다."""
    blocks: list[EditBlock] = []
    remainders: list[tuple[str, FrameInterval]] = []
    resources = {resource.scoped_id: resource for resource in document.resources}
    for clip in _clips(document):
        guard = guards_by_instance[clip.instance_id]
        media = media_by_resource[clip.resource_scoped_id]
        if not _audible(clip, media):
            _require_audible_link(clip, guard)
            continue
        excluded = (
            guard.status != "success"
            or clip.instance_id in unavailable_by_instance
            or clip.instance_id in locked
        )
        if excluded:
            continue
        source, timeline = _clip_grids(clip, resources[clip.resource_scoped_id], document)
        for run in _eligible_runs(source, guard.protected_frames, limits.min_frames):
            pieces, leftover = _split_run(run, limits)
            blocks.extend(
                _build_block(
                    document, clip, media.asset_content_id, piece, source, timeline, cfg, limits
                )
                for piece in pieces
            )
            if leftover is not None:
                remainders.append((clip.instance_id, leftover))
    return tuple(blocks), tuple(remainders)


def _audible(clip: ClipInstance, media: ResolvedMedia) -> bool:
    """P4-01과 정확히 같은 가청 판정 — guard 상태나 슬라이스 기하로 추측하지 않는다."""
    return media.has_audio and clip.enabled and clip.src_enable == "all"


def _require_audible_link(clip: ClipInstance, guard: InstanceGuard) -> None:
    """불가청 인스턴스의 success guard는 연결 오류다 — 적격 판정이 아니라 raise.

    이 방향만 닫는다: 제거를 **넓히는** 모순이므로. keep_all·inaudible이 불가청
    클립에 붙는 것은 보수 방향이라 견딘다(블록만 없어진다).
    """
    if guard.status == "success":
        raise ValueError(
            f"불가청 인스턴스에 status=success guard가 붙었다: {clip.element_path}"
            " — 가청은 문서에서 계산하므로 이 짝은 위조 연결이다"
        )


def _clips(document: ProjectDocument) -> tuple[ClipInstance, ...]:
    """스토리라인의 클립 순서 — 갭은 편집 블록과 무관하다."""
    return tuple(item for item in document.storyline if isinstance(item, ClipInstance))


def _clip_grids(
    clip: ClipInstance, resource: AssetResource, document: ProjectDocument
) -> tuple[FrameInterval, FrameInterval]:
    """클립의 소스·타임라인 격자 구간 — 남은 XML 구간에서, 앵커는 각 시계의 0.

    소스 좌표는 `clip.start − asset.start`(공용 파일 시계), 타임라인 좌표는
    `clip.offset − sequence.tcStart`다. 섭취가 둘 다 격자 위를 보증하므로
    바닥·천장은 값을 움직이지 않는다.
    """
    fd = document.frame_duration
    source_start = clip.start - resource.start
    timeline_start = clip.offset - document.sequence_tc_start
    source = interval_outward(source_start, source_start + clip.duration, fd, _SOURCE_GRID_ANCHOR)
    timeline = interval_outward(
        timeline_start, timeline_start + clip.duration, fd, _SOURCE_GRID_ANCHOR
    )
    return source, timeline


def _eligible_runs(
    clip_frames: FrameInterval,
    protected: Sequence[FrameInterval],
    min_frames: int,
) -> tuple[FrameInterval, ...]:
    """보호 ∩ 클립 구간의 여집합 런 — min 미만 런은 적격이 아니라 그대로 둔다.

    합쳐진 슬라이스의 guard는 슬라이스 전체의 프레임을 싣는다. 그래서 먼저
    클립 자신의 구간과 교집합한 뒤 여집합을 취한다 — 그래야 이웃 칼날의 보호가
    이 클립에 구멍을 만들지 않는다.
    """
    runs: list[FrameInterval] = []
    cursor = clip_frames.start_frame
    for span in protected:
        if span.end_frame <= cursor or span.start_frame >= clip_frames.end_frame:
            continue
        low = max(span.start_frame, cursor)
        high = min(span.end_frame, clip_frames.end_frame)
        if low > cursor:
            runs.append(FrameInterval(cursor, low))
        cursor = max(cursor, high)
    if cursor < clip_frames.end_frame:
        runs.append(FrameInterval(cursor, clip_frames.end_frame))
    return tuple(run for run in runs if run.end_frame - run.start_frame >= min_frames)


def _split_run(
    run: FrameInterval, limits: _Limits
) -> tuple[list[FrameInterval], FrameInterval | None]:
    """런 하나를 블록으로 — balanced가 안 되면 greedy, 짧은 꼬리는 유지다.

    balanced: `n = ceil(L/max)`개, 크기 차 1프레임 이하(큰 블록이 앞). 모든
    블록이 min 이상이면 쓴다. 아니면 greedy — 앞에서 max프레임씩 — 로 자르고
    min 미만 꼬리는 블록이 아니라 명시적으로 유지된다.
    """
    length = run.end_frame - run.start_frame
    sizes = _balanced_sizes(length, limits)
    remainder = 0
    if sizes is None:
        sizes, remainder = _greedy_sizes(length, limits)
    pieces: list[FrameInterval] = []
    cursor = run.start_frame
    for size in sizes:
        pieces.append(FrameInterval(cursor, cursor + size))
        cursor += size
    leftover = FrameInterval(run.end_frame - remainder, run.end_frame) if remainder else None
    return pieces, leftover


def _balanced_sizes(length: int, limits: _Limits) -> list[int] | None:
    """균등 분할의 크기들 — min 미만 블록이 생기면 None(greedy로 간다)."""
    count = -(-length // limits.max_frames)
    base, extra = divmod(length, count)
    sizes = [base + 1] * extra + [base] * (count - extra)
    if min(sizes) < limits.min_frames:
        return None
    return sizes


def _greedy_sizes(length: int, limits: _Limits) -> tuple[list[int], int]:
    """greedy 분할 — max프레임 블록을 앞에서부터, min 미만 남은 분은 유지."""
    sizes: list[int] = []
    left = length
    while left >= limits.min_frames:
        take = min(limits.max_frames, left)
        sizes.append(take)
        left -= take
    return sizes, left


def _build_block(
    document: ProjectDocument,
    clip: ClipInstance,
    source_id: str,
    piece: FrameInterval,
    clip_source: FrameInterval,
    clip_timeline: FrameInterval,
    cfg: ProjectConfig,
    limits: _Limits,
) -> EditBlock:
    """블록 하나 조립 — 신원·후보·표본. 타임라인은 같은 상대 오프셋의 밀림이다."""
    timeline = FrameInterval(
        clip_timeline.start_frame + (piece.start_frame - clip_source.start_frame),
        clip_timeline.start_frame + (piece.end_frame - clip_source.start_frame),
    )
    block_id = _block_id(document.input_sha256, clip.instance_id, source_id, piece, timeline)
    candidates = _keep_candidates(
        block_id, piece, timeline, limits.keep_frames, document.frame_duration
    )
    return EditBlock(
        block_id=block_id,
        instance_id=clip.instance_id,
        source_id=source_id,
        timeline_frames=timeline,
        source_frames=piece,
        keep_candidates=candidates,
        sample_requests=_sample_requests(
            piece, candidates, source_id, document.frame_duration, cfg
        ),
    )


def _keep_candidates(
    block_id: str,
    block: FrameInterval,
    block_timeline: FrameInterval,
    keep_frames: int,
    fd: Fraction,
) -> tuple[KeepCandidate, ...]:
    """앞/중앙/뒤 후보 — 정확히 keep_frames, 중앙은 half-up, 같은 구간은 하나.

    중앙 후보 시작의 상대 오프셋은 `(길이 − keep) / 2` 프레임을 P1 `frame_nearest`
    와 같은 half-up으로 반올림한 `(길이 − keep + 1) // 2`다 — 반 프레임 걸침은
    위로. 세 후보가 같은 구간이면(블록 길이 == keep) 하나만 낸다. 후보가 블록을
    넘거나 통째로 비우는 경우는 없다 — 레일이 `keep <= min <= 블록 길이`를
    증명했다. 전체 삭제 후보는 존재할 수 없다: 가장 짧은 후보도 keep_frames만큼
    유지한다.
    """
    length = block.end_frame - block.start_frame
    starts = (
        block.start_frame,
        block.start_frame + (length - keep_frames + 1) // 2,
        block.end_frame - keep_frames,
    )
    candidates: list[KeepCandidate] = []
    seen: set[tuple[int, int]] = set()
    for start in starts:
        interval = FrameInterval(start, start + keep_frames)
        key = (interval.start_frame, interval.end_frame)
        if key in seen:
            continue
        seen.add(key)
        shift = start - block.start_frame
        candidate_id = _candidate_id(block_id, interval)
        candidates.append(
            KeepCandidate(
                candidate_id=candidate_id,
                source_frames=interval,
                timeline_frames=FrameInterval(
                    block_timeline.start_frame + shift,
                    block_timeline.start_frame + shift + keep_frames,
                ),
                vision=VisionCandidate(
                    candidate_id=candidate_id,
                    keep_start=Fraction(shift) * fd,
                    keep_end=Fraction(shift + keep_frames) * fd,
                ),
            )
        )
    return tuple(candidates)


def _sample_requests(
    block: FrameInterval,
    candidates: Sequence[KeepCandidate],
    source_id: str,
    fd: Fraction,
    cfg: ProjectConfig,
) -> tuple[FrameSampleRequest, ...]:
    """표본 — 명목 fps + 유효 끝점 둘 + 후보 중앙 셋, 한도까지 결정적 축소.

    필수 표본은 블록의 첫 유효 프레임 `start_frame`과 마지막 유효 프레임
    `end_frame − 1`(`end_frame`은 애셋 프레임 수와 같을 수 있다), 그리고 각
    후보의 중앙 `min(시작 + ceil(keep/2), 길이 − 1)`이다 — `keep_frames == 1`이면
    중앙이 `end_frame`에 딱 걸리므로 명목과 마찬가지로 묶는다. 명목 표본은
    `t = k / fps < 블록 길이`를 P1 `frame_nearest` half-up으로 격자 인덱스에
    붙이고 블록 안쪽으로 묶는다 — 29.97처럼 정수 초가 격자 밖이면 그 초를 절대
    직접 보내지 않는다.

    축소 규칙(결정적): 필수는 절대 안 덜고 명목만 줄인다. 명목에서 필수와 겹치는
    인덱스를 먼저 빼고, 남은 수가 방보다 많으면 `step = ceil(남은 수 / 방)` 간격
    스트라이드(`indices[::step]`)로 줄인다. 방이 0이하면 명목은 전부 사라진다.
    같은 입력은 같은 표본 집합을 낸다 — P7이 분석을 재사용하므로 "대충 16개까지"
    는 리비전을 깨뜨린다.
    """
    length = block.end_frame - block.start_frame
    keep = candidates[0].source_frames.end_frame - candidates[0].source_frames.start_frame
    mandatory = {0, length - 1}
    # 중앙도 명목과 같은 클램프를 받는다 — keep_frames == 1이면 (1+1)//2 == 1이
    # 되어 끝 후보의 중앙이 end_frame에 딱 걸린다(P6-01b). end_frame은 애셋
    # 프레임 수와 같을 수 있으므로 절대 표본이 될 수 없다.
    mandatory.update(
        min(
            candidate.source_frames.start_frame - block.start_frame + (keep + 1) // 2,
            length - 1,
        )
        for candidate in candidates
    )
    nominal = _nominal_indices(length, fd, cfg.vision_sample_fps)
    spare = [index for index in nominal if index not in mandatory]
    room = _effective_max_images(cfg) - len(mandatory)
    if room < len(spare):
        spare = _thin(spare, room)
    return tuple(
        FrameSampleRequest(
            frame_id=_frame_id(source_id, block.start_frame + index),
            source_id=source_id,
            source_time=Fraction(block.start_frame + index) * fd,
        )
        for index in sorted(mandatory | set(spare))
    )


def _nominal_indices(length: int, fd: Fraction, fps: Fraction) -> list[int]:
    """명목 fps 표본의 상대 인덱스 — half-up 스냅, 블록 안쪽 [0, 길이−1]로 묶는다."""
    duration = Fraction(length) * fd
    indices: list[int] = []
    count = 0
    tick = Fraction(0)
    while tick < duration:
        indices.append(min(max(frame_nearest(tick, fd), 0), length - 1))
        count += 1
        tick = Fraction(count) / fps
    return indices


def _thin(indices: list[int], room: int) -> list[int]:
    """결정적 축소 — ceil(n/room) 간격 스트라이드. 방이 0 이하이면 빈다."""
    if room <= 0:
        return []
    step = -(-len(indices) // room)
    return indices[::step]


def _block_id(
    document_sha: str,
    instance_id: str,
    source_id: str,
    source: FrameInterval,
    timeline: FrameInterval,
) -> str:
    """블록 신원 — 문서 해시·인스턴스·내용·두 격자 구간의 해시(모듈 머리의 공식)."""
    material = "\0".join(
        (
            _BLOCK_ID_DOMAIN,
            document_sha,
            instance_id,
            source_id,
            _frames(source),
            _frames(timeline),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _candidate_id(block_id: str, source: FrameInterval) -> str:
    """후보 신원 — 블록 신원과 후보 프레임의 해시. keep이 다르면 달라진다."""
    material = "\0".join((_CANDIDATE_ID_DOMAIN, block_id, _frames(source)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _frames(interval: FrameInterval) -> str:
    """구간의 정확한 직렬 "start/end" — 같은 구간은 늘 같은 글자가 된다."""
    return f"{interval.start_frame}/{interval.end_frame}"


def _frame_id(source_id: str, grid_index: int) -> str:
    """표본 신원 — 내용 신원과 소스 격자 인덱스의 짝만으로 만든다."""
    return f"{source_id}:{grid_index}"
