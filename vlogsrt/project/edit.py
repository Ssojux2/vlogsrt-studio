"""P6-01 블록 분할 + P6-02 편집 계획 — 순수 기하와 그 적용·검증.

이 프로젝트에서 **처음으로 구간을 실제 제거하는 계층**이다. 그래서
선이 가장 엄격하다(CLAUDE.md 규칙 4·9): 이 모듈은 오디오를 열지 않고 전사·VAD·
`review` 산문을 읽지 않는다. 타입 있는 `InstanceGuard`의 여집합 — "지우면 안
되는 프레임"의 보수 — 만 본다. "이 구간은 말이 아니다"라는 판단은 상류가
보호라는 형태로 이미 했고 여기서 하는 일은 기하뿐이다. 네트워크·ffmpeg·모델
호출이 없고 소스를 하나도 고치지 않는다 — 후보와 요청을 만들고(P6-01),
저장된 판정을 적용해 불변 계획을 세울 뿐이다(P6-02)
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
반대 방향으로 나누지 않는다(P4-04의 교훈). P6-02도 마찬가지다: 후보 프레임을
`choice` 아이디로 조회해서 그대로 읽고, 나눗셈은 없다. 소스 격자에서 인덱스 n은
공용 파일 시계 `n × frame_duration`을 뜻하고 `asset.start + n × frame_duration`이
XML 애셋 시계 자리다. n을 디코드 영상 순번으로 취급하지 않고
`probe_video_stream`의 `frame_count`를 격자 끝으로 가정하지도 않는다 — 경계는
**남은 XML 클립/파일 구간**에서 온다.

**타임라인 격자 앵커를 한 번 못 박는다**(P6-02). 입력 타임라인 인덱스 m은 입력
경과 `m × frame_duration`이고 `sequence.tcStart`는 이미 빠져 있다 — 섭취가 모든
`offset`을 그 격자에, 모든 클립 `start`를 애셋 격자에 강제한다. 출력 타임라인도
같은 격자를 쓰고 0에서 다시 시작한다.

설정·연결 결함은 호출자 잘못이라 `raise`로 닫는다 — keep+review로 세탁하지
않는다(P5-05b가 policy에서 그은 선). `_require_config`의 프레임 환산 뒤 관계와
교차 레일은 `ProjectConfig.__post_init__`(P5-06)이 볼 수 없는 P6 고유의
검사다. 방어는 겹쳐야 한다 — 검사받지 않는 층은 썩는다. 모델·응답 결함은 그
반대편이다: 판정이 모자라면 블록은 그대로 남고 리뷰에만 기록된다.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.boundary import UnavailableInstance
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import (
    AssetResource,
    ClipInstance,
    GapInstance,
    ProjectDocument,
)
from vlogsrt.project.timebase import (
    FrameInterval,
    frame_ceil,
    frame_floor,
    frame_nearest,
    interval_outward,
)
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.vision.policy import (
    KEEP_ALL,
    UNCERTAIN,
    VisionCandidate,
    VisionDecision,
    VisionRequest,
    _effective_max_images,
    request_sha256,
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


# ═════════════════════════════════════════════════════════════
#  P6-02 — 판정을 적용해 불변 편집 계획을 세운다
# ═════════════════════════════════════════════════════════════


_PLAN_REVISION_DOMAIN = "vlogsrt-edit-plan-v1"
"""계획 리비전의 도메인 구분자 — 블록·후보 신원과 같은 재료라도 갈라진다."""

_REVIEW_DECISION_INCONSISTENT = "decision_inconsistent"
_REVIEW_GUARD_OVERLAP = "guard_overlap"

_PROJECTION_CONTEXT = ""
"""정본 투영의 이웃 맥락 글월 — 해시 재료가 아니므로 계획에서는 빈 글월로 못 박는다."""


@dataclass(frozen=True, slots=True)
class RetainedInterval:
    """유지 구간 하나 — 소스·입력·출력 세 격자와 결정적인 유지 사유.

    소스 격자 인덱스는 애셋 시작 앵커 파일 시계다(FCPXML `asset.start`를 더하기
    전). `reason`은 닫힌 어휘다: kept_locked · kept_unavailable · kept_guard ·
    kept_no_blocks · kept_outside_blocks · kept_block · kept_candidate.
    """

    instance_id: str
    source_frames: FrameInterval
    input_timeline_frames: FrameInterval
    output_timeline_frames: FrameInterval
    reason: str


@dataclass(frozen=True, slots=True)
class RemovedInterval:
    """제거 구간 하나 — 되돌릴 자리는 출력 프레임 하나로 유일하다.

    `restore_at_output_frame`은 이 구간이 다시 끼워질 출력 타임라인 지점이다 —
    리플 뒤 그 다음 유지 항목의 출력 시작과 같다. 인스턴스 참조로 쓰면 한
    인스턴스가 제거를 여럿 가질 때 모호해진다(한 블록 ≤ 10초가 보통이다).
    """

    instance_id: str
    source_frames: FrameInterval
    input_timeline_frames: FrameInterval
    restore_at_output_frame: int


@dataclass(frozen=True, slots=True)
class RetainedGap:
    """유지 갭 — 원래 순서·길이 그대로 출력 스토리라인에 선다."""

    instance_id: str
    input_timeline_frames: FrameInterval
    output_timeline_frames: FrameInterval


@dataclass(frozen=True, slots=True)
class RetainedRun:
    """유지 런 — 소스와 출력이 둘 다 연속인 같은 인스턴스 유지 구간의 합.

    `retained`는 블록 경계마다 쪼개진다(제거가 없어도). 큐 부착(P6-03)과 클립
    내보내기(P6-04)가 쓰는 단위는 이 병합이다 — 이음매마다 같은 병합을 다시
    짜지 않게 생산자가 낸다(P4-04 모양). `reasons`는 합쳐진 조각의 사유를
    순서 그대로 보존한다.
    """

    instance_id: str
    source_frames: FrameInterval
    input_timeline_frames: FrameInterval
    output_timeline_frames: FrameInterval
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    """구조화 리뷰 증거 — `code`가 기계 판독 증거이고 `message`는 사람 글월이다.

    위치는 짝으로만 달린다: `instance_id`와 `source_frame`은 둘 다 있거나 둘 다
    없다. `source_frame`은 유지 구간과 같은 애셋 시작 앵커 소스 격자 인덱스다.
    반쯤 찬 좌표는 마커 근원이 모호해지므로 만드는 순간 거부한다. `message`를
    파싱해 시각을 되찾는 일은 없다 — P6-04는 located 이슈만 마커로 만든다.
    """

    code: str
    message: str
    instance_id: str | None
    source_frame: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError(f"ReviewIssue.code는 비지 않은 문자열이어야 한다: {self.code!r}")
        if (self.instance_id is None) != (self.source_frame is None):
            raise ValueError(
                f"리뷰 위치는 짝이어야 한다 — instance_id={self.instance_id!r},"
                f" source_frame={self.source_frame!r} (code {self.code!r})"
            )


@dataclass(frozen=True, slots=True)
class EditPlan:
    """한 번의 편집 계획 — 같은 저장 입력은 같은 계획을 낸다.

    `frame_duration`은 문서 격자를 실어 P6-03이 격자를 되묻지 않게 한다.
    `gaps`가 출력 스토리라인을 완성하므로 P6-04는 리플을 다시 계산하지 않는다.
    `decisions`는 저장된 판정 그대로다 — 적용 여부와 무관한 기록물이다.
    """

    revision: str
    input_sha256: str
    config_sha256: str
    frame_duration: Fraction
    locked_instance_ids: frozenset[str]
    retained: tuple[RetainedInterval, ...]
    runs: tuple[RetainedRun, ...]
    gaps: tuple[RetainedGap, ...]
    removed: tuple[RemovedInterval, ...]
    decisions: tuple[VisionDecision, ...]
    unavailable_instances: tuple[UnavailableInstance, ...]
    review: tuple[ReviewIssue, ...]


def build_edit_plan(
    document: ProjectDocument,
    partition: BlockPartition,
    guards: Sequence[InstanceGuard],
    decisions: Sequence[VisionDecision],
    cfg: ProjectConfig,
) -> EditPlan:
    """저장된 판정을 파티션에 적용해 불변 계획을 세운다 — 순수하고 결정적이다.

    관문 순서가 계약이다: 문서·설정 해시 → 설정 레일 → guard 연결 → 부재 기록 →
    블록 검증 → 판정 연결. 앞의 것들이 어긋나면 raise(섞인 리비전·호출자
    잘못)다. 판정이 모자라거나 모순이면 블록이 남고 리뷰에 기록된다(모델
    잘못). 보호와 겹치는 스냅은 그 판정만 keep_all로 물러난다.
    """
    _require_revision_match(document, partition, cfg)
    guards_by_instance = _require_plan_guards(document, guards)
    unavailable_by_instance = _require_unavailable(
        document, partition.unavailable_instances, partition.locked_instance_ids
    )
    blocks_by_instance = _require_plan_blocks(
        document, partition, unavailable_by_instance, guards_by_instance
    )
    decisions_by_block = _require_decision_linkage(partition, decisions)
    retained, gaps, removed, review = _ripple_storyline(
        document, partition, blocks_by_instance, guards_by_instance, decisions_by_block, cfg
    )
    review += tuple(_unavailable_issues(partition.unavailable_instances))
    revision = _plan_revision(partition, decisions, retained, gaps, removed, review, document)
    return EditPlan(
        revision=revision,
        input_sha256=document.input_sha256,
        config_sha256=partition.config_sha256,
        frame_duration=document.frame_duration,
        locked_instance_ids=partition.locked_instance_ids,
        retained=retained,
        runs=_coalesce_runs(retained),
        gaps=gaps,
        removed=removed,
        decisions=tuple(decisions),
        unavailable_instances=tuple(partition.unavailable_instances),
        review=review,
    )


def validate_edit_plan(
    plan: EditPlan,
    document: ProjectDocument,
    guards: Sequence[InstanceGuard],
) -> tuple[str, ...]:
    """규칙 9의 기계 검증 — 비어 있어야 P6-04가 렌더할 수 있다.

    **검증기의 범위**: 이것은 구조 관문이다 — 관찰 가능한 결과만 잰다(프레임
    보존·출력 타일링·보호·잠금·부재·최소 유지·되돌릴 자리·runs 일관성).
    "모든 제거는 한 블록 안에 있다"는 출처 문장은 여기서 잴 수 없다 — 계획은
    블록을 싣지 않는다. 그 성질은 build가 구조로 보증한다(제거 조각은 늘 한
    블록의 보수에서 깎이고 다른 조각과 합쳐지지 않는다) — 출처는 검증자가
    아니라 **재빌드**가 증명한다. 결함은 예외가 아니라 findings 문장이다 —
    렌더 거부는 P6-04의 판단이다.

    **신뢰 진술**: 하류(P6-04)는 같은 프로세스에서 `build_edit_plan`이 만든
    플랜만 렌더하고 `revision`을 대조한다. 저장된 `EditPlan`을 역직렬화해
    렌더하지 않는다 — 재개는 저장된 분석에서 플랜을 다시 만든다(명세가
    "plan/export는 저장된 분석에서 결정적"이라고 못 박았다).
    """
    findings: list[str] = []
    findings.extend(_identity_findings(plan, document))
    findings.extend(_review_findings(plan, document))
    findings.extend(_unavailable_findings(plan, document))
    findings.extend(_conservation_findings(plan, document))
    findings.extend(_protection_findings(plan, document, guards))
    findings.extend(_tiling_findings(plan, document))
    findings.extend(_restore_findings(plan))
    findings.extend(_runs_findings(plan))
    return tuple(findings)


def _require_revision_match(
    document: ProjectDocument, partition: BlockPartition, cfg: ProjectConfig
) -> None:
    """첫 관문 — 파티션·문서·설정 해시 짝. 어긋나면 섞인 리비전이다."""
    if partition.input_sha256 != document.input_sha256:
        raise ValueError(
            f"파티션의 문서 해시가 문서와 다르다: {partition.input_sha256} ≠"
            f" {document.input_sha256} — 섞인 리비전이다"
        )
    if partition.config_sha256 != cfg.sha256():
        raise ValueError(
            f"파티션의 설정 해시가 설정과 다르다: {partition.config_sha256} ≠"
            f" {cfg.sha256()} — 섞인 리비전이다"
        )
    _require_config(cfg, document.frame_duration)


def _require_plan_guards(
    document: ProjectDocument, guards: Sequence[InstanceGuard]
) -> dict[str, InstanceGuard]:
    """guard 연결 — 문서의 클립 인스턴스마다 정확히 하나. 없음·중복·남음은 raise다."""
    by_instance: dict[str, InstanceGuard] = {}
    for guard in guards:
        if guard.instance_id in by_instance:
            raise ValueError(
                f"인스턴스에 guard가 둘 이상 있다: {guard.instance_id!r} — 정확히 하나여야 한다"
            )
        by_instance[guard.instance_id] = guard
    clips = _clips(document)
    for clip in clips:
        if clip.instance_id not in by_instance:
            raise ValueError(
                f"클립 인스턴스에 guard가 없다: {clip.element_path}"
                " — 없음이 보호 없음이 되면 안 된다"
            )
    extra = sorted(set(by_instance) - {clip.instance_id for clip in clips})
    if extra:
        raise ValueError(f"문서에 없는 인스턴스를 가진 guard다: {extra[0]!r}")
    return by_instance


def _require_plan_blocks(
    document: ProjectDocument,
    partition: BlockPartition,
    unavailable_by_instance: dict[str, UnavailableInstance],
    guards_by_instance: dict[str, InstanceGuard],
) -> dict[str, tuple[EditBlock, ...]]:
    """블록 검증 — 인스턴스·잠금·부재·경계·순서·내용 신원 짝. 위조는 raise다."""
    clips = {clip.instance_id: clip for clip in _clips(document)}
    resources = {resource.scoped_id: resource for resource in document.resources}
    grouped: dict[str, list[EditBlock]] = {}
    for block in partition.blocks:
        clip = clips.get(block.instance_id)
        if clip is None:
            raise ValueError(f"블록의 인스턴스가 문서 클립이 아니다: {block.instance_id!r}")
        if (
            block.instance_id in partition.locked_instance_ids
            or block.instance_id in unavailable_by_instance
        ):
            raise ValueError(
                f"잠긴·부재 인스턴스에 블록이 있다: {block.instance_id!r} — 제거 권한이 없다"
            )
        guard = guards_by_instance[block.instance_id]
        if guard.status != "success":
            raise ValueError(
                f"블록 인스턴스의 guard가 success가 아니다: {block.instance_id!r}"
                f" ({guard.status!r}) — keep_all·inaudible은 빈 protected여도 제거 권한이 없다"
            )
        if guard.source_id != block.source_id:
            raise ValueError(
                f"guard의 source_id가 블록과 다르다: guard {guard.source_id!r},"
                f" 블록 {block.source_id!r}"
            )
        source, timeline = _clip_grids(clip, resources[clip.resource_scoped_id], document)
        if not _within(block.source_frames, source) or not _within(block.timeline_frames, timeline):
            raise ValueError(f"블록이 클립 경계 밖이다: {block.block_id}")
        if block.timeline_frames != _timeline_window(
            source, timeline, block.source_frames.start_frame, block.source_frames.end_frame
        ):
            raise ValueError(f"블록의 타임라인이 소스와 어긋난다: {block.block_id}")
        previous = grouped.get(block.instance_id)
        if previous and previous[-1].source_frames.end_frame > block.source_frames.start_frame:
            raise ValueError(f"블록이 겹치거나 순서가 어긋난다: {block.block_id}")
        grouped.setdefault(block.instance_id, []).append(block)
    return {instance: tuple(blocks) for instance, blocks in grouped.items()}


def _require_decision_linkage(
    partition: BlockPartition, decisions: Sequence[VisionDecision]
) -> dict[str, tuple[VisionDecision, ...]]:
    """판정 연결 — 모르는 블록·남의 후보·모르는 choice·거짓 신원은 섞인 리비전이다.

    요청 신원은 **모든 판정**에서 증명한다. 적용 경로에서만 보면 승인되지
    않은 판정의 위조 해시가 `plan.decisions`와 revision에 조용히 들어간다 —
    섞인 리비전은 status와 무관하게 섞인 리비전이다.
    """
    owner: dict[str, str] = {}
    blocks_by_id: dict[str, EditBlock] = {}
    for block in partition.blocks:
        blocks_by_id[block.block_id] = block
        for candidate in block.keep_candidates:
            if candidate.candidate_id in owner:
                raise ValueError(f"후보 아이디가 파티션에서 둘이다: {candidate.candidate_id}")
            owner[candidate.candidate_id] = block.block_id
    grouped: dict[str, list[VisionDecision]] = {}
    for decision in decisions:
        named = blocks_by_id.get(decision.block_id)
        if named is None:
            raise ValueError(
                f"판정이 파티션에 없는 블록을 가리킨다: {decision.block_id!r} — 섞인 리비전이다"
            )
        if decision.choice not in (KEEP_ALL, UNCERTAIN):
            holder = owner.get(decision.choice)
            if holder is None:
                raise ValueError(f"판정의 choice가 알려진 후보가 아니다: {decision.choice!r}")
            if holder != decision.block_id:
                raise ValueError(
                    f"판정이 남의 블록 후보를 가리킨다: choice {decision.choice!r}는 블록"
                    f" {holder!r}의 것이다 (판정 블록 {decision.block_id!r})"
                )
        expected = request_sha256(vision_request_for(named, _PROJECTION_CONTEXT))
        if decision.request_sha256 != expected:
            raise ValueError(
                f"판정의 요청 신원이 이 블록과 다르다: 블록 {decision.block_id!r},"
                f" 판정 {decision.request_sha256!r} ≠ 정본 투영 {expected!r} — 섞인 리비전이다"
            )
        grouped.setdefault(decision.block_id, []).append(decision)
    return {block_id: tuple(rows) for block_id, rows in grouped.items()}


def _ripple_storyline(
    document: ProjectDocument,
    partition: BlockPartition,
    blocks_by_instance: dict[str, tuple[EditBlock, ...]],
    guards_by_instance: dict[str, InstanceGuard],
    decisions_by_block: dict[str, tuple[VisionDecision, ...]],
    cfg: ProjectConfig,
) -> tuple[
    tuple[RetainedInterval, ...],
    tuple[RetainedGap, ...],
    tuple[RemovedInterval, ...],
    tuple[ReviewIssue, ...],
]:
    """스토리라인 지도 — 유지·갭·제거·리뷰를 입력 순서 그대로 잇는다."""
    retained: list[RetainedInterval] = []
    gaps: list[RetainedGap] = []
    removed: list[RemovedInterval] = []
    review: list[ReviewIssue] = []
    cursor = 0
    resources = {resource.scoped_id: resource for resource in document.resources}
    clips = {clip.instance_id: clip for clip in _clips(document)}
    for item in document.storyline:
        if isinstance(item, GapInstance):
            span = _gap_frames(item, document)
            gaps.append(
                RetainedGap(item.instance_id, span, FrameInterval(cursor, cursor + _length(span)))
            )
            cursor += _length(span)
            continue
        clip = clips[item.instance_id]
        source, timeline = _clip_grids(clip, resources[clip.resource_scoped_id], document)
        cursor = _ripple_clip(
            clip,
            source,
            timeline,
            blocks_by_instance.get(clip.instance_id, ()),
            guards_by_instance[clip.instance_id],
            decisions_by_block,
            partition,
            cfg,
            cursor,
            retained,
            removed,
            review,
        )
    return tuple(retained), tuple(gaps), tuple(removed), tuple(review)


def _ripple_clip(
    clip: ClipInstance,
    source: FrameInterval,
    timeline: FrameInterval,
    blocks: tuple[EditBlock, ...],
    guard: InstanceGuard,
    decisions_by_block: dict[str, tuple[VisionDecision, ...]],
    partition: BlockPartition,
    cfg: ProjectConfig,
    cursor: int,
    retained: list[RetainedInterval],
    removed: list[RemovedInterval],
    review: list[ReviewIssue],
) -> int:
    """클립 하나를 지나간다 — 블록은 판정 따라, 나머지는 통째 유지다."""
    if not blocks:
        retained.append(
            RetainedInterval(
                clip.instance_id,
                source,
                timeline,
                FrameInterval(cursor, cursor + _length(source)),
                _whole_clip_reason(clip.instance_id, partition, guard),
            )
        )
        return cursor + _length(source)
    position = source.start_frame
    for block in blocks:
        if position < block.source_frames.start_frame:
            cursor = _retain(
                clip,
                source,
                timeline,
                FrameInterval(position, block.source_frames.start_frame),
                cursor,
                "kept_outside_blocks",
                retained,
            )
        candidate, issues = _applied_candidate(
            block, decisions_by_block.get(block.block_id, ()), guard, cfg
        )
        review.extend(issues)
        if candidate is None:
            cursor = _retain(
                clip, source, timeline, block.source_frames, cursor, "kept_block", retained
            )
        else:
            cursor = _apply(clip, source, timeline, block, candidate, cursor, retained, removed)
        position = block.source_frames.end_frame
    if position < source.end_frame:
        cursor = _retain(
            clip,
            source,
            timeline,
            FrameInterval(position, source.end_frame),
            cursor,
            "kept_outside_blocks",
            retained,
        )
    return cursor


def _retain(
    clip: ClipInstance,
    source: FrameInterval,
    timeline: FrameInterval,
    span: FrameInterval,
    cursor: int,
    reason: str,
    retained: list[RetainedInterval],
) -> int:
    """유지 구간 하나를 출력에 잇는다 — 입력 타임라인은 문서 격자에서 온다."""
    window = _timeline_window(source, timeline, span.start_frame, span.end_frame)
    retained.append(
        RetainedInterval(
            clip.instance_id,
            span,
            window,
            FrameInterval(cursor, cursor + _length(span)),
            reason,
        )
    )
    return cursor + _length(span)


def _apply(
    clip: ClipInstance,
    source: FrameInterval,
    timeline: FrameInterval,
    block: EditBlock,
    candidate: KeepCandidate,
    cursor: int,
    retained: list[RetainedInterval],
    removed: list[RemovedInterval],
) -> int:
    """후보를 유지하고 블록의 나머지를 제거로 남긴다 — 되돌릴 자리는 출력 커서다.

    머리 조각은 후보가 놓일 자리에, 꼬리 조각은 후보 다음 자리에 끼워진다 —
    각각 그 순간의 출력 커서다.
    """
    if block.source_frames.start_frame < candidate.source_frames.start_frame:
        removed.append(
            _removal(
                clip,
                source,
                timeline,
                FrameInterval(block.source_frames.start_frame, candidate.source_frames.start_frame),
                cursor,
            )
        )
    cursor = _retain(
        clip, source, timeline, candidate.source_frames, cursor, "kept_candidate", retained
    )
    if candidate.source_frames.end_frame < block.source_frames.end_frame:
        removed.append(
            _removal(
                clip,
                source,
                timeline,
                FrameInterval(candidate.source_frames.end_frame, block.source_frames.end_frame),
                cursor,
            )
        )
    return cursor


def _removal(
    clip: ClipInstance,
    source: FrameInterval,
    timeline: FrameInterval,
    span: FrameInterval,
    restore_at: int,
) -> RemovedInterval:
    """제거 구간 하나 — 소스·입력 창과 되돌릴 출력 프레임."""
    window = _timeline_window(source, timeline, span.start_frame, span.end_frame)
    return RemovedInterval(clip.instance_id, span, window, restore_at)


def _applied_candidate(
    block: EditBlock,
    block_decisions: tuple[VisionDecision, ...],
    guard: InstanceGuard,
    cfg: ProjectConfig,
) -> tuple[KeepCandidate | None, tuple[ReviewIssue, ...]]:
    """한 블록의 판정을 후보로 바꾼다 — 모자란 판정은 keep_all, 모순은 리뷰다.

    적용은 세 조건의 교집합이다: `status == "accepted"` 그리고 choice가 **이
    블록의** 후보 아이디 그리고 유한한 `confidence ≥ cfg.vision_apply_confidence`.
    재검사는 좁히기만 한다 — 승인을 confidence나 reason에서 다시 유도하지
    않는다(NaN이 비교를 무력화하는 P5-05b 길이다). 판정의 요청 신원은 연결
    단계에서 모든 판정에 대해 이미 증명됐다.
    """
    if not block_decisions:
        return None, ()
    if len(block_decisions) > 1:
        return None, (
            _inconsistent(block, f"판정이 {len(block_decisions)}개다 — 블록마다 하나여야 한다"),
        )
    decision = block_decisions[0]
    if not math.isfinite(decision.confidence):
        return None, (
            _inconsistent(block, f"신뢰도가 유한 실수가 아니다: {decision.confidence!r}"),
        )
    if decision.status == "accepted" and decision.choice in (KEEP_ALL, UNCERTAIN):
        return None, (_inconsistent(block, f"status=accepted인데 choice가 {decision.choice!r}다"),)
    if decision.status != "accepted" or decision.confidence < cfg.vision_apply_confidence:
        return None, ()
    return _guard_clean_candidate(block, decision, guard)


def _guard_clean_candidate(
    block: EditBlock, decision: VisionDecision, guard: InstanceGuard
) -> tuple[KeepCandidate | None, tuple[ReviewIssue, ...]]:
    """스냅 재검사 — 제거 조각이 보호와 겹치면 그 판정만 keep_all로 물러난다."""
    candidate = _candidate_by_id(block, decision.choice)
    pieces = _removed_pieces(block, candidate)
    for span in guard.protected_frames:
        for piece in pieces:
            frame = _overlap_start(piece, span)
            if frame is not None:
                return None, (
                    ReviewIssue(
                        code=_REVIEW_GUARD_OVERLAP,
                        message=(
                            f"후보 {candidate.candidate_id}의 제거 범위가 보호와 겹친다 —"
                            f" 판정을 유지로 물린다 (블록 {block.block_id})"
                        ),
                        instance_id=block.instance_id,
                        source_frame=frame,
                    ),
                )
    return candidate, ()


def _removed_pieces(block: EditBlock, candidate: KeepCandidate) -> tuple[FrameInterval, ...]:
    """블록에서 후보를 덜어낸 제거 조각들 — 빈 조각은 내지 않는다."""
    pieces = []
    if block.source_frames.start_frame < candidate.source_frames.start_frame:
        pieces.append(
            FrameInterval(block.source_frames.start_frame, candidate.source_frames.start_frame)
        )
    if candidate.source_frames.end_frame < block.source_frames.end_frame:
        pieces.append(
            FrameInterval(candidate.source_frames.end_frame, block.source_frames.end_frame)
        )
    return tuple(pieces)


def _candidate_by_id(block: EditBlock, candidate_id: str) -> KeepCandidate:
    """후보 조회 — 적용은 프레임을 여기서 읽지 나누지 않는다."""
    for candidate in block.keep_candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    raise ValueError(
        f"후보 아이디가 이 블록에 없다: {candidate_id!r} (블록 {block.block_id!r})"
        " — 연결 검사를 통과했는데 없다면 내부 불변 위반이다"
    )


def _inconsistent(block: EditBlock, detail: str) -> ReviewIssue:
    """판정 모순의 리뷰 — 블록은 남고 결함은 기록된다."""
    return ReviewIssue(
        code=_REVIEW_DECISION_INCONSISTENT,
        message=f"판정 모순 — 블록 {block.block_id}: {detail}",
        instance_id=None,
        source_frame=None,
    )


def _whole_clip_reason(instance_id: str, partition: BlockPartition, guard: InstanceGuard) -> str:
    """블록 없는 클립의 유지 사유 — 부재·잠금·guard 실패·적격 런 없음 순서로.

    부재 인스턴스는 늘 잠금과 함께 오므로 더 구체적인 진술이 먼저다.
    """
    if any(record.instance_id == instance_id for record in partition.unavailable_instances):
        return "kept_unavailable"
    if instance_id in partition.locked_instance_ids:
        return "kept_locked"
    if guard.status != "success":
        return "kept_guard"
    return "kept_no_blocks"


def _gap_frames(gap: GapInstance, document: ProjectDocument) -> FrameInterval:
    """갭의 입력 타임라인 구간 — tcStart를 뺀 격자 위 반개 구간."""
    start = gap.offset - document.sequence_tc_start
    return interval_outward(
        start, start + gap.duration, document.frame_duration, _SOURCE_GRID_ANCHOR
    )


def _unavailable_issues(
    records: Sequence[UnavailableInstance],
) -> tuple[ReviewIssue, ...]:
    """부재 기록마다 리뷰 하나 — 위치는 없다. 신원은 타입 기록이 이미 싣는다.

    선언된 무음은 완결된 부재라 단독으로는 최종 승인을 막지 않고, 나머지 둘은
    재시도 가능한 준비 블로커로 남는다 — 그 판정은 P6-04가 타입 기록에서
    한다. 같은 내용의 건강한 형제를 누르지 않는다: 기록은 자원 스코프
    인스턴스에만 묶인다.
    """
    issues: list[ReviewIssue] = []
    for record in records:
        if record.reason_code == "declared_no_audio":
            message = (
                "선언된 무음 부재 — 완결된 기록이며 인스턴스는 그대로 둔다"
                f" (인스턴스 {record.instance_id})"
            )
        else:
            message = (
                f"재시도 가능한 부재({record.reason_code}) — 준비 블로커로 남는다"
                f" (인스턴스 {record.instance_id})"
            )
        issues.append(
            ReviewIssue(
                code=f"unavailable_{record.reason_code}",
                message=message,
                instance_id=None,
                source_frame=None,
            )
        )
    return tuple(issues)


def _plan_revision(
    partition: BlockPartition,
    decisions: Sequence[VisionDecision],
    retained: tuple[RetainedInterval, ...],
    gaps: tuple[RetainedGap, ...],
    removed: tuple[RemovedInterval, ...],
    review: tuple[ReviewIssue, ...],
    document: ProjectDocument,
) -> str:
    """계획 리비전 — 입력·설정·파티션·판정·부재·구간·리뷰 전부를 해시한다.

    같은 저장 입력은 같은 리비전을 낸다. 온도·사전 순서 약속은 없다 — 재료의
    순서는 전부 계약이 정한 입력 순서다(스토리라인·블록·기록). **판정만은
    파티션 블록 순서로 정규화한다**: `plan.decisions`는 호출자가 준 순서의
    기록물이지만, 같은 판정 집합이 순서만 다르게 전해졌다고 리비전이 갈라지면
    "같은 저장 입력 → 같은 계획"이 깨진다(안정 정렬 — 한 블록에 판정이 둘인
    경우는 원래 순서를 지킨다). `runs`는 retained의 순수 함수이므로 재료에
    넣지 않는다 — 해시가 같은 값을 두 번 묶지 않는다.
    """
    block_order = {block.block_id: index for index, block in enumerate(partition.blocks)}
    ordered_decisions = sorted(decisions, key=lambda decision: block_order[decision.block_id])
    material: list[object] = [
        _PLAN_REVISION_DOMAIN,
        document.input_sha256,
        partition.config_sha256,
        [_block_record(block) for block in partition.blocks],
        [[instance, _frames(span)] for instance, span in partition.kept_short_remainders],
        [
            [record.resource_scoped_id, record.instance_id, record.reason_code]
            for record in partition.unavailable_instances
        ],
        sorted(partition.locked_instance_ids),
        [_decision_record(decision) for decision in ordered_decisions],
        [_retained_record(row) for row in retained],
        [
            [
                row.instance_id,
                _frames(row.input_timeline_frames),
                _frames(row.output_timeline_frames),
            ]
            for row in gaps
        ],
        [
            [
                row.instance_id,
                _frames(row.source_frames),
                _frames(row.input_timeline_frames),
                row.restore_at_output_frame,
            ]
            for row in removed
        ],
        [[issue.code, issue.message, issue.instance_id, issue.source_frame] for issue in review],
    ]
    payload = json.dumps(material, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _block_record(block: EditBlock) -> list[object]:
    """블록의 리비전 재료 — 신원·후보·표본 전부를 정확한 직렬로."""
    return [
        block.block_id,
        block.instance_id,
        block.source_id,
        _frames(block.source_frames),
        _frames(block.timeline_frames),
        [
            [
                candidate.candidate_id,
                _frames(candidate.source_frames),
                _frames(candidate.timeline_frames),
                _ratio(candidate.vision.keep_start),
                _ratio(candidate.vision.keep_end),
            ]
            for candidate in block.keep_candidates
        ],
        [
            [request.frame_id, request.source_id, _ratio(request.source_time)]
            for request in block.sample_requests
        ],
    ]


def _decision_record(decision: VisionDecision) -> list[object]:
    """판정의 리비전 재료 — 저장된 판정 그대로. confidence는 repr로 정확히."""
    return [
        decision.block_id,
        decision.request_sha256,
        decision.choice,
        decision.reason,
        list(decision.evidence_frame_ids),
        repr(decision.confidence),
        decision.status,
    ]


def _retained_record(row: RetainedInterval) -> list[object]:
    return [
        row.instance_id,
        _frames(row.source_frames),
        _frames(row.input_timeline_frames),
        _frames(row.output_timeline_frames),
        row.reason,
    ]


# ─────────────────────────────────────────────────────────────
#  검증 도우미 — findings 는 문장이다
# ─────────────────────────────────────────────────────────────


def _identity_findings(plan: EditPlan, document: ProjectDocument) -> list[str]:
    """계획의 신원 — 문서 해시·프레임 길이·잠금 집합이 문서와 짝이어야 한다."""
    findings: list[str] = []
    if plan.input_sha256 != document.input_sha256:
        findings.append(
            f"계획의 문서 해시가 문서와 다르다: {plan.input_sha256} ≠ {document.input_sha256}"
        )
    if plan.frame_duration != document.frame_duration:
        findings.append(
            f"계획의 프레임 길이가 문서와 다르다: {plan.frame_duration} ≠ {document.frame_duration}"
        )
    clip_ids = {clip.instance_id for clip in _clips(document)}
    unknown_locks = sorted(plan.locked_instance_ids - clip_ids)
    if unknown_locks:
        findings.append(f"잠금 집합에 문서 클립이 아닌 인스턴스가 있다: {unknown_locks[0]!r}")
    return findings


def _review_findings(plan: EditPlan, document: ProjectDocument) -> list[str]:
    """리뷰 증거 — located 이슈의 인스턴스·프레임이 그 클립 안에 있어야 한다."""
    findings: list[str] = []
    clips = {clip.instance_id: clip for clip in _clips(document)}
    resources = {resource.scoped_id: resource for resource in document.resources}
    for issue in plan.review:
        if issue.instance_id is None or issue.source_frame is None:
            continue
        clip = clips.get(issue.instance_id)
        if clip is None:
            findings.append(
                f"리뷰가 문서 클립이 아닌 인스턴스를 가리킨다: {issue.instance_id!r}"
                f" (code {issue.code!r})"
            )
            continue
        source, _ = _clip_grids(clip, resources[clip.resource_scoped_id], document)
        if not source.start_frame <= issue.source_frame < source.end_frame:
            findings.append(
                f"리뷰의 source_frame가 인스턴스 클립 밖이다: {issue.instance_id!r},"
                f" 프레임 {issue.source_frame} ∉ {_frames(source)} (code {issue.code!r})"
            )
    return findings


def _unavailable_findings(plan: EditPlan, document: ProjectDocument) -> list[str]:
    """부재 기록 — 사유·자원·인스턴스 짝이 문서와 같고 제거가 없어야 한다."""
    findings: list[str] = []
    resources = {resource.scoped_id for resource in document.resources}
    owner = {clip.instance_id: clip.resource_scoped_id for clip in _clips(document)}
    listed: set[str] = set()
    for record in plan.unavailable_instances:
        if record.reason_code not in _UNAVAILABLE_REASONS:
            findings.append(f"부재 기록의 사유를 모른다: {record.reason_code!r}")
        if record.resource_scoped_id not in resources:
            findings.append(
                f"부재 기록이 문서에 없는 자원을 가리킨다: {record.resource_scoped_id!r}"
            )
        if owner.get(record.instance_id) != record.resource_scoped_id:
            findings.append(
                f"부재 기록의 인스턴스가 이 자원의 클립이 아니다: {record.instance_id!r}"
            )
        if record.instance_id in listed:
            findings.append(f"부재 기록이 중복이다: {record.instance_id!r}")
        listed.add(record.instance_id)
    for row in plan.removed:
        if row.instance_id in listed:
            findings.append(
                f"부재 인스턴스에서 제거가 일어났다: {row.instance_id!r} — 제거 권한이 없다"
            )
    return findings


def _conservation_findings(plan: EditPlan, document: ProjectDocument) -> list[str]:
    """프레임 보존 — 인스턴스마다 유지+제거가 클립 프레임 수와 정확히 같다."""
    findings: list[str] = []
    resources = {resource.scoped_id: resource for resource in document.resources}
    clip_ids = {clip.instance_id for clip in _clips(document)}
    intervals: tuple[RetainedInterval | RemovedInterval, ...] = (*plan.retained, *plan.removed)
    for row in intervals:
        if row.instance_id not in clip_ids:
            findings.append(
                f"유지·제거 구간이 문서 클립이 아닌 인스턴스를 가리킨다: {row.instance_id!r}"
            )
    for clip in _clips(document):
        source, timeline = _clip_grids(clip, resources[clip.resource_scoped_id], document)
        retained = [row for row in plan.retained if row.instance_id == clip.instance_id]
        removed = [row for row in plan.removed if row.instance_id == clip.instance_id]
        findings.extend(_instance_findings(clip, source, timeline, retained, removed))
    return findings


def _instance_findings(
    clip: ClipInstance,
    source: FrameInterval,
    timeline: FrameInterval,
    retained: Sequence[RetainedInterval],
    removed: Sequence[RemovedInterval],
) -> list[str]:
    """한 인스턴스의 보존 — 경계·타임라인 짝·분리·합계·최소 유지."""
    findings: list[str] = []
    rows: list[RetainedInterval | RemovedInterval] = sorted(
        (*retained, *removed), key=lambda row: row.source_frames.start_frame
    )
    span_total = 0
    kept = 0
    previous_end: int | None = None
    for row in rows:
        span = row.source_frames
        if not _within(span, source):
            findings.append(
                f"구간이 클립 경계 밖이다: 인스턴스 {clip.instance_id!r}, {_frames(span)}"
            )
        if row.input_timeline_frames != _timeline_window(
            source, timeline, span.start_frame, span.end_frame
        ):
            findings.append(
                f"구간의 입력 타임라인이 문서와 어긋난다: 인스턴스 {clip.instance_id!r},"
                f" {_frames(span)}"
            )
        if previous_end is not None and span.start_frame < previous_end:
            findings.append(
                f"유지·제거 구간이 서로 겹친다: 인스턴스 {clip.instance_id!r}, {_frames(span)}"
            )
        previous_end = span.end_frame if previous_end is None else max(previous_end, span.end_frame)
        span_total += _length(span)
        if isinstance(row, RetainedInterval):
            kept += _length(span)
            if _length(span) < 1:
                findings.append(
                    f"유지 구간의 소스 길이가 0이다: 인스턴스 {clip.instance_id!r},"
                    f" {_frames(span)} — 프레임은 음수가 아니어도 비어 있으면 못 쓴다"
                )
            if _length(row.output_timeline_frames) != _length(span):
                findings.append(
                    f"유지 구간의 출력 길이가 소스와 다르다: 인스턴스 {clip.instance_id!r},"
                    f" 소스 {_frames(span)} → 출력 {_frames(row.output_timeline_frames)}"
                    " — 렌더가 소스를 자르거나 출력을 겹치게 만든다"
                )
    if span_total != _length(source):
        findings.append(
            f"프레임 보존 위반: 인스턴스 {clip.instance_id!r} — 유지·제거 합계"
            f" {span_total}프레임 ≠ 클립 {_length(source)}프레임"
        )
    if kept < 1:
        findings.append(
            f"인스턴스가 한 프레임도 남기지 않았다: {clip.instance_id!r} — 클립 통째 삭제는 금지다"
        )
    return findings


def _protection_findings(
    plan: EditPlan, document: ProjectDocument, guards: Sequence[InstanceGuard]
) -> list[str]:
    """보호·잠금 — 제거는 보호와 교차하지 않고 잠긴 인스턴스에 없다."""
    findings: list[str] = []
    by_instance: dict[str, list[InstanceGuard]] = {}
    for guard in guards:
        by_instance.setdefault(guard.instance_id, []).append(guard)
    clip_ids = {clip.instance_id for clip in _clips(document)}
    for instance_id in sorted(set(by_instance) - clip_ids):
        findings.append(f"문서에 없는 인스턴스를 가진 guard다: {instance_id!r}")
    for clip in _clips(document):
        rows = by_instance.get(clip.instance_id, [])
        if not rows:
            findings.append(
                f"클립 인스턴스에 guard가 없다: {clip.element_path} — 보호 검증이 불가하다"
            )
        elif len(rows) > 1:
            findings.append(f"인스턴스에 guard가 둘 이상 있다: {clip.instance_id!r}")
        removals = [row for row in plan.removed if row.instance_id == clip.instance_id]
        for guard in rows:
            if removals and guard.status != "success":
                findings.append(
                    f"guard 상태가 {guard.status!r}인 인스턴스에서 제거가 일어났다:"
                    f" {clip.instance_id!r} — keep_all·inaudible은 빈 protected여도"
                    " 제거 권한이 없다"
                )
            findings.extend(_removal_vs_guard(plan, clip, guard))
    for row in plan.removed:
        if row.instance_id in plan.locked_instance_ids:
            findings.append(
                f"잠긴 인스턴스에서 제거가 일어났다: {row.instance_id!r} — 대사 단축 금지다"
            )
    return findings


def _removal_vs_guard(plan: EditPlan, clip: ClipInstance, guard: InstanceGuard) -> list[str]:
    """제거 ∩ 보호 — 조각 단위로 짚고 첫 겹침 프레임을 말한다."""
    findings: list[str] = []
    for row in plan.removed:
        if row.instance_id != clip.instance_id:
            continue
        for span in guard.protected_frames:
            frame = _overlap_start(row.source_frames, span)
            if frame is not None:
                findings.append(
                    f"제거가 보호와 겹친다: 인스턴스 {clip.instance_id!r}, 제거"
                    f" {_frames(row.source_frames)} ∩ 보호 {_frames(span)} (프레임 {frame})"
                )
    return findings


def _tiling_findings(plan: EditPlan, document: ProjectDocument) -> list[str]:
    """출력 타일링 — retained ∪ gaps가 [0, 출력 끝)를 정확히 덮고 입력 순서를 지킨다."""
    findings: list[str] = []
    tiles = sorted(
        [
            (row.output_timeline_frames, row.input_timeline_frames, row.instance_id)
            for row in plan.retained
        ]
        + [
            (row.output_timeline_frames, row.input_timeline_frames, row.instance_id)
            for row in plan.gaps
        ],
        key=lambda tile: (tile[0].start_frame, tile[0].end_frame),
    )
    expected = 0
    previous_input: int | None = None
    for output, incoming, instance_id in tiles:
        if _length(output) == 0:
            findings.append(
                f"출력 길이가 0인 타일이다: 인스턴스 {instance_id!r}, 입력 {_frames(incoming)}"
                " — 정직한 계획은 0 길이 타일을 만들지 않는다(파서가 0 길이를 거부한다)"
            )
            continue
        if output.start_frame != expected:
            kind = "출력에 구멍이 있다" if output.start_frame > expected else "출력 구간이 겹친다"
            findings.append(
                f"{kind}: 인스턴스 {instance_id!r}, 출력 {_frames(output)} (기대 시작 {expected})"
            )
        if previous_input is not None and incoming.start_frame <= previous_input:
            findings.append(
                f"출력 순서가 입력 순서를 어긴난다: 인스턴스 {instance_id!r},"
                f" 입력 {_frames(incoming)} (앞 입력 시작 {previous_input})"
            )
        previous_input = incoming.start_frame
        expected = max(expected, output.end_frame)
    for row in plan.gaps:
        if _length(row.output_timeline_frames) != _length(row.input_timeline_frames):
            findings.append(
                f"갭 길이가 변했다: {row.instance_id!r} — 입력 {_length(row.input_timeline_frames)}"
                f"프레임 → 출력 {_length(row.output_timeline_frames)}프레임"
            )
    gap_ids = {item.instance_id for item in document.storyline if isinstance(item, GapInstance)}
    for row in plan.gaps:
        if row.instance_id not in gap_ids:
            findings.append(f"갭이 문서에 없는 인스턴스를 가리킨다: {row.instance_id!r}")
    expected_gaps = [
        [item.instance_id, _frames(_gap_frames(item, document))]
        for item in document.storyline
        if isinstance(item, GapInstance)
    ]
    plan_gaps = [[row.instance_id, _frames(row.input_timeline_frames)] for row in plan.gaps]
    if plan_gaps != expected_gaps:
        findings.append("갭의 순서·입력 창이 문서와 다르다 — 기존 갭은 그대로여야 한다")
    return findings


def _restore_findings(plan: EditPlan) -> list[str]:
    """되돌릴 자리 — 제거의 입력 끝 이후 첫 타일의 출력 시작과 같아야 한다.

    그런 타일이 없으면(제거가 스토리라인 끝에 붙으면) 출력 끝이다. 이 값이
    `project_trace.json`이 복원 지점을 말하는 근거라 틀리면 복원 기록이 거짓이
    된다 — 보존도 타일링도 이 값 자체는 보지 않는다.
    """
    tiles = sorted(
        [
            (row.input_timeline_frames, row.output_timeline_frames, row.instance_id)
            for row in plan.retained
        ]
        + [
            (row.input_timeline_frames, row.output_timeline_frames, row.instance_id)
            for row in plan.gaps
        ],
        key=lambda tile: tile[0].start_frame,
    )
    output_end = max((tile[1].end_frame for tile in tiles), default=0)
    findings: list[str] = []
    for row in plan.removed:
        expected = next(
            (
                tile[1].start_frame
                for tile in tiles
                if tile[0].start_frame >= row.input_timeline_frames.end_frame
            ),
            output_end,
        )
        if row.restore_at_output_frame != expected:
            findings.append(
                f"되돌릴 자리가 틀리다: 인스턴스 {row.instance_id!r}, 제거 입력"
                f" {_frames(row.input_timeline_frames)}, restore_at_output_frame"
                f" {row.restore_at_output_frame} ≠ 기대 {expected}"
            )
    return findings


def _runs_findings(plan: EditPlan) -> list[str]:
    """runs 일관성 — retained를 병합한 것과 정확히 같아야 한다."""
    if plan.runs != _coalesce_runs(plan.retained):
        return ["runs가 retained의 병합과 일치하지 않는다 — 손으로 고쳐졌다"]
    return []


def _coalesce_runs(retained: tuple[RetainedInterval, ...]) -> tuple[RetainedRun, ...]:
    """유지 구간을 런으로 병합 — 같은 인스턴스·소스 연속·출력 연속일 때만 잇는다.

    하나라도 끊기면 run이 갈라진다: 제거가 있었던 자리는 소스가, 리플 차이는
    출력이 끊는다. 제거가 없는 블록 경계는 둘 다 이어지므로 run에 남는다.
    """
    runs: list[RetainedRun] = []
    for row in retained:
        last = runs[-1] if runs else None
        if (
            last is not None
            and row.instance_id == last.instance_id
            and row.source_frames.start_frame == last.source_frames.end_frame
            and row.output_timeline_frames.start_frame == last.output_timeline_frames.end_frame
        ):
            runs[-1] = RetainedRun(
                instance_id=last.instance_id,
                source_frames=FrameInterval(
                    last.source_frames.start_frame, row.source_frames.end_frame
                ),
                input_timeline_frames=FrameInterval(
                    last.input_timeline_frames.start_frame,
                    row.input_timeline_frames.end_frame,
                ),
                output_timeline_frames=FrameInterval(
                    last.output_timeline_frames.start_frame,
                    row.output_timeline_frames.end_frame,
                ),
                reasons=last.reasons + (row.reason,),
            )
            continue
        runs.append(
            RetainedRun(
                instance_id=row.instance_id,
                source_frames=row.source_frames,
                input_timeline_frames=row.input_timeline_frames,
                output_timeline_frames=row.output_timeline_frames,
                reasons=(row.reason,),
            )
        )
    return tuple(runs)


def _timeline_window(
    source: FrameInterval, timeline: FrameInterval, start: int, end: int
) -> FrameInterval:
    """소스 구간 [start,end)의 타임라인 창 — 같은 상대 오프셋의 밀림이다."""
    return FrameInterval(
        timeline.start_frame + (start - source.start_frame),
        timeline.start_frame + (end - source.start_frame),
    )


def _within(inner: FrameInterval, outer: FrameInterval) -> bool:
    """반개 구간의 담김 — 양끝이 바깥 구간 안에 있는가."""
    return outer.start_frame <= inner.start_frame and inner.end_frame <= outer.end_frame


def _overlap_start(a: FrameInterval, b: FrameInterval) -> int | None:
    """두 반개 구간이 겹치면 첫 겹침 프레임, 아니면 None."""
    start = max(a.start_frame, b.start_frame)
    end = min(a.end_frame, b.end_frame)
    return start if start < end else None


def _length(span: FrameInterval) -> int:
    """구간 길이 — 프레임 수."""
    return span.end_frame - span.start_frame


def _ratio(value: Fraction) -> str:
    """유리수의 정확한 직렬 — 같은 값은 늘 같은 글자가 된다."""
    return f"{value.numerator}/{value.denominator}"
