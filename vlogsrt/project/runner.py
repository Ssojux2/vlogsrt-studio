"""러너 전 단계 — 파싱 · 미디어 해석 · PCM · 부재의 기록 · 분석 · 출판 (PROTO-2·3·4).

파이프라인의 단계들은 이미 다 있다 — 이 모듈은 그 조각들을 잇는다. 1/3(`prepare_audio`,
PROTO-2)은 문서를 파싱하고, 미디어를 해석하고, 오디오를 PCM으로 저장하고, 전사할 수
없는 리소스는 **기록으로** 남긴다. 산출물은 `speech.transcribe_unique_sources`가 이미
고정한 쌍 모양 그대로다 — 러너가 새 모양을 발명하면 소비자의 신원 검사가 짝짓기
오류를 흘려보낸다. 2/3(`analyze_audio`, PROTO-3)은 그 산출물을 받아 전사·자막 투영·
경계 재전사·VAD·가드를 한 번에 흘려 **분석이 끝난 상태**를 낸다. 3/3(`publish_project`,
PROTO-4)은 그 상태를 받아 블록마다 프레임 표본과 비전 판정을 만들고, 계획·검증·
EDIT 자막을 거쳐 패키지를 출판한다 — 비전 호출은 `transcribe.request_vision` 이름으로만
나가고 러너는 클라이언트를 만들지 않으며 주입받는다.

세 가지 부재가 1/3에서만 난다:

- `declared_no_audio` — 리소스가 `hasAudio=0`으로 선언했다. probe하지 않는다:
  `has_audio`은 정확히 그 리소스의 선언에서만 온다(media.py 계약).
- `stream_unavailable` — 선언은 오디오가 있는데 `probe_audio_stream`이 죽었다.
- `pcm_unavailable` — probe는 됐는데 `build_reference`가 죽었다.

`reason`에는 실제 예외 메시지를 실는다 — 상수 문구는 진단을 죽인다. 해석 불가능한
미디어(`UnsupportedProject`)는 부재 기록으로 강등되지 않고 그대로 올라온다 —
media.py의 준비 게이트가 의도적으로 하드하기 때문이다. ffmpeg 미설치도 마찬가지로
한 번의 하드 실패다: 도구 게이트를 자원 게이트 앞에 두어 환경 오류 하나가 N개의
부재 기록으로 번역되는 길을 끊는다.

PCM 논리 주소는 **내용 신원**(`asset_content_id`)으로 키잡는다 — 리소스가 아니라.
서로 다른 xml_id를 가진 리소스 둘이 같은 바이트를 가리키면(파서는 중복 id만
거부한다) 주소가 하나로 접혀 추출은 한 번이다. 주소는 애셋 네임스페이스
(`artifact_dir/<content_id>` — source·boundary가 이미 쓴다)와 갈라진
`artifact_dir/pcm/<content_id>`에 산다.

**알려진 한계**: `AudioPolicy.stream_index`(기본 0)은 첫 오디오 트랙만 듣는다.
카메라 + 랩마이크처럼 트랙이 둘 이상인 애셋에서 나머지는 듣지 않으며, 듣지 않았다는
기록도 없다 — 기존 `pcm.py`의 성질이지 이 단계가 만든 것이 아니다. 결과에 정책을
실어 돌려주는 것이 유일한 완화다.

ffmpeg·ffprobe는 `preprocess.probe_audio_stream`과 `pcm.build_reference`를 통해서만
닿는다(계약 §5). 이 파일은 subprocess를 모른다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from vlogsrt import log, transcribe
from vlogsrt.config import Config
from vlogsrt.preprocess import probe_audio_stream, require_ffmpeg
from vlogsrt.project.boundary import (
    BoundaryPlan,
    BoundaryProjection,
    BoundaryTranscript,
    LogicalSlice,
    UnavailableInstance,
    logical_slices,
    plan_boundaries,
    project_boundaries,
    transcribe_boundaries,
)
from vlogsrt.project.captions import (
    EditCaptionSet,
    SourceCaptionSet,
    project_edit_captions,
    project_source_captions,
)
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.edit import (
    _PROJECTION_CONTEXT,
    BlockPartition,
    EditBlock,
    EditPlan,
    build_edit_plan,
    partition_blocks,
    validate_edit_plan,
    vision_request_for,
)
from vlogsrt.project.export import ExportPackage, publish_package
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import MediaOverride, ResolvedMedia, resolve_media
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.pcm import AudioPolicy, PcmReference, build_reference
from vlogsrt.project.speech import (
    AudioSourceInput,
    SourceAnalysis,
    SourceTrace,
    UnavailableSourceAudio,
    transcribe_unique_sources,
)
from vlogsrt.project.vad.guard import (
    InstanceGuard,
    SourceGuard,
    build_source_guard,
    project_guard,
)
from vlogsrt.project.vad.inference import VadResult, infer_vad
from vlogsrt.project.vad.runtime import bundled_artifact, create_cpu_session
from vlogsrt.project.vision.policy import (
    VisionDecision,
    VisionPolicyError,
    VisionRequest,
    _error_decision,
    build_payload,
    parse_decision,
)
from vlogsrt.project.vision.policy import (
    _require_config as _require_policy_config,
)
from vlogsrt.project.vision.sampling import FrameSamplingError, sample_frames

_PCM_DIR = "pcm"
"""PCM 논리 주소의 네임스페이스 — source·boundary가 쓰는 애셋 디렉터리와 갈라진다."""

_FRAMES_DIR = "frames"
"""프레임 표본(PROTO-4)의 네임스페이스 뿌리 — 미리 만들어 경로로 돌려준다."""

_POINTER_NAME = "reference.pcm"
"""논리 캐시 주소의 파일 이름 — 세대 PCM·세대 기록은 이 이름을 앞두고 나란히 산다."""


@dataclass(frozen=True, slots=True)
class PreparedAudio:
    """오디오 준비 단계의 산출물 — 나머지 러너 태스크(PROTO-3·4)의 입력.

    frozen이지만 계약 §8의 기계 검사는 `models.py` 전용이라 여기는 문서로만 선을
    긋는다. 실제로 쓴 `config`·`project_config`를 실어 돌려주는 이유: 소스 분석 키가
    설정 지문을 먹으므로, 다음 단계가 자기 config를 따로 읽으면 출판 자리·세그먼트
    신원·중복 제거가 어긋난다. `artifact_dir`이 source·boundary 네임스페이스의 뿌리다
    (speech·boundary가 이미 그 아래 애셋 디렉터리로 쓴다).
    """

    document: ProjectDocument
    media: tuple[ResolvedMedia, ...]
    audio_inputs: tuple[AudioSourceInput, ...]
    policy: AudioPolicy
    config: Config
    project_config: ProjectConfig
    artifact_dir: Path
    pcm_dir: Path
    frames_dir: Path
    inputs_by_resource: Mapping[str, AudioSourceInput]

    def __post_init__(self) -> None:
        """규칙 9를 타입으로 못 깨게 한다 — 이 저장소에서 `protected=[]` 계열 결함이
        여섯 번 재발했고, 전부 "없음"이 "제한 없음"으로 읽힌 것이다."""
        if len(self.audio_inputs) != len(self.document.resources):
            raise ValueError(
                f"audio_inputs({len(self.audio_inputs)})가 문서 리소스 수"
                f"({len(self.document.resources)})와 다르다 — 리소스는 조용히 사라지지 않는다"
            )
        for index, (media, _second) in enumerate(self.audio_inputs):
            expected = self.document.resources[index].scoped_id
            if media.resource_scoped_id != expected:
                raise ValueError(
                    f"audio_inputs[{index}]의 자원 신원이 문서 순서와 어긋난다: "
                    f"{media.resource_scoped_id!r} != {expected!r}"
                )


def pcm_destination(pcm_root: Path, asset_content_id: str) -> Path:
    """PCM 논리 캐시 주소 — 내용 신원으로 키잡는다(리소스 신원이 아니라).

    접는 것은 이 주소뿐이다: `_reusable_reference`가 이 포인터를 보고 재사용을
    정하므로, 같은 바이트를 가리키는 리소스 몇 개든 추출은 한 번이다.
    """
    return pcm_root / asset_content_id / _POINTER_NAME


def prepare_audio(
    project_path: Path,
    artifact_dir: Path,
    *,
    overrides: Sequence[MediaOverride] = (),
    policy: AudioPolicy | None = None,
    config: Config | None = None,
    project_config: ProjectConfig | None = None,
) -> PreparedAudio:
    """문서를 파싱해 미디어를 해석하고 PCM 참조를 만든다 — 모든 리소스가 쌍으로 돌아온다.

    쌍은 문서 리소스 순서 그대로다. 둘째 칸은 PCM 참조이거나 부재 기록이다 — 비는
    칸, 조용히 빠지는 리소스가 없다(규칙 9). 해석 불가능한 미디어는 준비 게이트가
    그대로 올려 보낸다. 오디오가 하나라도 있으면 도구 게이트가 먼저 선다.
    """
    used_policy = policy if policy is not None else AudioPolicy()
    document = parse_project(project_path)
    resolved = resolve_media(document, overrides)
    pcm_root, frames_root = _artifact_roots(artifact_dir)
    _require_audio_tools(resolved)
    audio_inputs = tuple(_audio_input(item, used_policy, pcm_root) for item in resolved)
    result = PreparedAudio(
        document=document,
        media=resolved,
        audio_inputs=audio_inputs,
        policy=used_policy,
        config=config if config is not None else Config(),
        project_config=project_config if project_config is not None else ProjectConfig(),
        artifact_dir=artifact_dir,
        pcm_dir=pcm_root,
        frames_dir=frames_root,
        inputs_by_resource={pair[0].resource_scoped_id: pair for pair in audio_inputs},
    )
    _log_summary(result)
    return result


def _artifact_roots(artifact_dir: Path) -> tuple[Path, Path]:
    """쓸 하위 네임스페이스를 미리 만들어 (PCM 뿌리, 프레임 뿌리)로 돌려준다."""
    pcm_root = artifact_dir / _PCM_DIR
    frames_root = artifact_dir / _FRAMES_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    pcm_root.mkdir(parents=True, exist_ok=True)
    frames_root.mkdir(parents=True, exist_ok=True)
    return pcm_root, frames_root


def _require_audio_tools(resolved: Sequence[ResolvedMedia]) -> None:
    """오디오를 저장할 때만 도구를 요구한다 — 없으면 한 번의 하드 실패로 끝난다.

    도구가 없는 것은 자원의 결함이 아니라 환경의 결함이다. 자원 게이트 안에서
    죽으면 부재 기록 N개로 번역되어 진단이 죽는다 — 게이트를 앞으로 뺀 것이 그
    길을 끊는다. 오디오가 하나도 없으면 묻지도 않는다.
    """
    if not any(item.has_audio for item in resolved):
        return
    require_ffmpeg("ffprobe")
    require_ffmpeg("ffmpeg")


def _audio_input(media: ResolvedMedia, policy: AudioPolicy, pcm_root: Path) -> AudioSourceInput:
    """한 리소스를 (미디어, PCM 참조 또는 부재 기록)으로 만든다 — 둘째 칸은 비지 않는다."""
    if not media.has_audio:
        # 선언 그대로다 — probe하지 않는다. has_audio은 그 리소스에서만 온다.
        return media, _declared_no_audio(media)
    try:
        stream = probe_audio_stream(media.original, policy.stream_index)
    except RuntimeError as exc:
        return media, _from_exception(media, "stream_unavailable", exc)
    log.debug(f"오디오 스트림 확인: {media.original.name} → 전역 {stream.stream_index}번")
    try:
        reference = build_reference(
            media.asset_content_id,
            media.original.stem,
            media.original,
            media.original_sha256,
            policy,
            pcm_destination(pcm_root, media.asset_content_id),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        return media, _from_exception(media, "pcm_unavailable", exc)
    return media, reference


def _declared_no_audio(media: ResolvedMedia) -> UnavailableSourceAudio:
    """선언된 무음 — 실패가 아니라 사실이므로 warn이 아니라 info로 남긴다."""
    record = _record(
        media,
        "no_audio",
        "declared_no_audio",
        "리소스가 hasAudio=0으로 선언했다 — 선언을 그대로 옮긴다",
    )
    log.info(f"선언된 무음: {media.resource_scoped_id}")
    return record


def _from_exception(
    media: ResolvedMedia,
    reason_code: Literal["stream_unavailable", "pcm_unavailable"],
    exc: BaseException,
) -> UnavailableSourceAudio:
    """실패의 기록 — reason에는 실제 예외 메시지를 실어 진단을 살린다."""
    reason = " ".join(str(exc).split()) or exc.__class__.__name__
    record = _record(media, "error", reason_code, reason)
    log.warn(f"소스 오디오 부재: {media.resource_scoped_id} — {reason_code}: {reason}")
    return record


def _record(
    media: ResolvedMedia,
    status: Literal["no_audio", "error"],
    reason_code: Literal["declared_no_audio", "stream_unavailable", "pcm_unavailable"],
    reason: str,
) -> UnavailableSourceAudio:
    """부재 기록의 조립 — 신원은 미디어에서 그대로 옮기고 지어내지 않는다."""
    return UnavailableSourceAudio(
        variant="unavailable_source_audio",
        resource_scoped_id=media.resource_scoped_id,
        source_id=media.asset_content_id,
        original_sha256=media.original_sha256,
        status=status,
        reason_code=reason_code,
        reason=reason,
    )


def _log_summary(result: PreparedAudio) -> None:
    """단계의 결산 — 부재도 세어 보고한다(있는 것만 세면 조용한 손실이 된다)."""
    unavailable = sum(
        1 for _media, second in result.audio_inputs if isinstance(second, UnavailableSourceAudio)
    )
    log.info(
        f"오디오 준비 완료: 리소스 {len(result.audio_inputs)}개 — "
        f"PCM {len(result.audio_inputs) - unavailable}, 부재 {unavailable}"
    )


# ═══════════════════════════════════════════════════════════════
#  PROTO-3 — 전사 · 자막 투영 · 경계 · VAD · 가드 (러너 2/3)
# ═══════════════════════════════════════════════════════════════


_UNAVAILABLE_GUARD_STATUS: Mapping[str, Literal["inaudible", "keep_all"]] = {
    "declared_no_audio": "inaudible",
    "stream_unavailable": "keep_all",
    "pcm_unavailable": "keep_all",
}
"""부재 사유 → 합성 가드 상태. 선언된 무음은 가청이 아니고, 나머지 둘은 들을 수
있었는데 못 들은 **실패**다 — 무음이 아니라 실패 닫힘(keep_all)으로 남는다.
하류(`edit._require_audible_link`)는 success만 거부하고 이 대응을 검사하지
않으므로, 이 표를 어기는 짝을 잡는 곳은 `AnalyzedAudio.__post_init__`뿐이다."""


@dataclass(frozen=True, slots=True)
class AnalyzedAudio:
    """분석이 끝난 상태 — PROTO-4(`partition_blocks` 이후)가 그대로 먹는 산출물.

    frozen이지만 `PreparedAudio`와 같은 이유로 깊은 불변은 문서로만 선을 긋는다.
    `instance_guards`는 **문서의 클립 인스턴스 순서 그대로, 인스턴스마다 정확히
    하나**다 — `project_guard`가 내는 소스별 순서를 여기서 다시 세운다. 잠금·부재는
    `BoundaryProjection`이 내놓는 것을 그대로 옮긴다(다시 계산하지 않는다).
    `source_guards`를 버리면 `vad_without_text`의 실제 프레임 수와 보호 비율은
    재유도가 아니라 복원 불가다(D35의 검토 상태). `transcripts`를 여기 실지 않으면
    디스크 캐시 재독으로만 회수된다.
    """

    prepared: PreparedAudio
    analyses: tuple[SourceAnalysis, ...]
    source_captions: SourceCaptionSet
    slices: tuple[LogicalSlice, ...]
    boundary_plan: BoundaryPlan
    boundary: BoundaryProjection
    vad_by_source: Mapping[str, VadResult]
    instance_guards: tuple[InstanceGuard, ...]
    locked_instance_ids: frozenset[str]
    unavailable_instances: tuple[UnavailableInstance, ...]
    source_guards: tuple[SourceGuard, ...]
    transcripts: tuple[BoundaryTranscript, ...]

    def __post_init__(self) -> None:
        """규칙 9를 타입으로 못 깨게 한다 — 하류에 없는 난간을 여기서 만든다.

        `partition_blocks`는 부재 인스턴스의 가드 상태 대응을 검사하지 않는다:
        keep_all·inaudible 오기는 조용히 통과하고(B3), success에 잠금·부재까지
        빼면 클립 전체가 제거 후보가 된다(A1). 그래서 이 다섯 단언이 유일한 난간이다.
        """
        clips = _document_clips(self.prepared.document)
        self._require_guard_count(clips)
        self._require_guard_order(clips)
        self._require_unavailable_status(clips)
        self._require_unavailable_locked(clips)
        self._require_vad_coverage()

    def _require_guard_count(self, clips: tuple[ClipInstance, ...]) -> None:
        """단언 1 — 인스턴스마다 가드가 정확히 하나다."""
        if len(self.instance_guards) != len(clips):
            raise ValueError(
                f"instance_guards({len(self.instance_guards)})가 문서의 클립 인스턴스"
                f" 수({len(clips)})와 다르다 — 없음이 보호 없음이 되면 안 된다"
            )

    def _require_guard_order(self, clips: tuple[ClipInstance, ...]) -> None:
        """단언 2 — 순서·신원이 문서 순서와 정확히 일치한다."""
        expected = tuple(clip.instance_id for clip in clips)
        actual = tuple(guard.instance_id for guard in self.instance_guards)
        if actual != expected:
            raise ValueError(
                f"instance_guards의 순서·신원이 문서와 어긋난다: {actual} != {expected}"
            )

    def _require_unavailable_status(self, clips: tuple[ClipInstance, ...]) -> None:
        """단언 3 — 부재 리소스의 인스턴스는 §2 표의 상태 그대로다(B3 오기를 잡는 유일한 자리)."""
        by_id = {guard.instance_id: guard for guard in self.instance_guards}
        for clip in clips:
            second = self.prepared.inputs_by_resource[clip.resource_scoped_id][1]
            if not isinstance(second, UnavailableSourceAudio):
                continue
            expected = _UNAVAILABLE_GUARD_STATUS[second.reason_code]
            actual = by_id[clip.instance_id].status
            if actual != expected:
                raise ValueError(
                    f"부재 인스턴스의 guard 상태가 대응표와 다르다: {clip.element_path} —"
                    f" {second.reason_code}는 {expected!r}인데 {actual!r}다"
                    " (success는 어떤 부재에도 올 수 없다)"
                )

    def _require_unavailable_locked(self, clips: tuple[ClipInstance, ...]) -> None:
        """단언 4 — 부재 인스턴스는 잠금·부재 기록과 함께 온다(A1을 표현 불가능하게 한다)."""
        recorded = {record.instance_id for record in self.unavailable_instances}
        for clip in clips:
            second = self.prepared.inputs_by_resource[clip.resource_scoped_id][1]
            if not isinstance(second, UnavailableSourceAudio):
                continue
            if clip.instance_id not in self.locked_instance_ids or (
                clip.instance_id not in recorded
            ):
                raise ValueError(
                    f"부재 인스턴스가 잠금·부재 기록과 함께 오지 않았다: {clip.element_path}"
                    " — 잠금·부재 쌍이 빈 success 가드의 제거 권한을 막는 유일한 난간이다"
                )

    def _require_vad_coverage(self) -> None:
        """단언 5 — VAD 키 집합 == PCM 참조를 가진 소스 집합."""
        pcm_sources = frozenset(
            second.source_id
            for _media, second in self.prepared.audio_inputs
            if isinstance(second, PcmReference)
        )
        if frozenset(self.vad_by_source) != pcm_sources:
            raise ValueError(
                f"vad_by_source의 키 {sorted(self.vad_by_source)}가 PCM 참조를 가진 소스"
                f" {sorted(pcm_sources)}와 다르다 — '키가 없다'는 '검사할 것이 없다'가 아니다"
            )


def analyze_audio(
    prepared: PreparedAudio,
    *,
    transcriber: transcribe.Transcriber,
    session: object | None = None,
) -> AnalyzedAudio:
    """오디오 준비 산출물을 분석이 끝난 상태로 만든다 — 유료 호출은 게이트 뒤에 있다.

    전사기는 주입받는다(openai 경계는 `vlogsrt/transcribe.py`뿐이다 — 계약 §4).
    ONNX 세션도 주입받을 수 있고, 아니면 참조가 하나라도 있을 때만 게이트에서
    만든다. `build_source_guard`의 `repairs`는 `()`다 — 이 단계가 손에 쥔
    경계 재전사 기록은 **남긴 범위 국소 시계**라 시계가 다르다(guard.py:168-174).
    """
    references = _unique_references(prepared.audio_inputs)
    vad_session = _vad_session(prepared, references, session)
    analyses = transcribe_unique_sources(
        prepared.audio_inputs, prepared.config, transcriber, prepared.artifact_dir
    )
    traces = tuple(item for item in analyses if isinstance(item, SourceTrace))
    source_captions = project_source_captions(prepared.document, prepared.media, analyses)
    slices = logical_slices(prepared.document, prepared.media)
    boundary_plan, transcripts, boundary = _boundary_stage(
        prepared, references, traces, source_captions, slices, transcriber
    )
    vad_by_source = _infer_vads(prepared, references, vad_session)
    source_guards, by_instance = _projected_guards(
        prepared, references, traces, slices, boundary, vad_by_source
    )
    _add_all(by_instance, _unavailable_guards(prepared))
    result = AnalyzedAudio(
        prepared=prepared,
        analyses=analyses,
        source_captions=source_captions,
        slices=slices,
        boundary_plan=boundary_plan,
        boundary=boundary,
        vad_by_source=vad_by_source,
        instance_guards=_document_order_guards(prepared.document, by_instance),
        locked_instance_ids=boundary.locked_instance_ids,
        unavailable_instances=boundary.unavailable_instances,
        source_guards=source_guards,
        transcripts=transcripts,
    )
    _log_analysis(result)
    return result


def _boundary_stage(
    prepared: PreparedAudio,
    references: Sequence[PcmReference],
    traces: tuple[SourceTrace, ...],
    source_captions: SourceCaptionSet,
    slices: tuple[LogicalSlice, ...],
    transcriber: transcribe.Transcriber,
) -> tuple[BoundaryPlan, tuple[BoundaryTranscript, ...], BoundaryProjection]:
    """경계 계획 → 재전사 → 투영 — 잠금·부재의 정본은 BoundaryProjection이 내놓는다."""
    plan = plan_boundaries(slices, source_captions, traces, references)
    transcripts = transcribe_boundaries(
        plan.requests, references, traces, prepared.config, transcriber, prepared.artifact_dir
    )
    return plan, transcripts, project_boundaries(plan, transcripts, source_captions, traces)


def _infer_vads(
    prepared: PreparedAudio, references: Sequence[PcmReference], session: object
) -> dict[str, VadResult]:
    """소스마다 VAD를 돈다 — 실패한 결과도 그대로 남긴다(가드가 keep_all로 닫는다)."""
    return {
        reference.source_id: infer_vad(
            reference, reference.source_id, prepared.project_config, session
        )
        for reference in references
    }


def _unique_references(inputs: Sequence[AudioSourceInput]) -> tuple[PcmReference, ...]:
    """PCM 참조를 source_id로 유일화한다 — 문서 리소스마다 한 칸씩이므로 같은 내용이 여러 번 온다.

    `prepared.audio_inputs`는 문서 리소스마다 한 쌍이고, PROTO-2가 접는 것은 PCM
    **주소**지 목록이 아니다. 서로 다른 xml_id가 같은 바이트를 가리키면 같은
    source_id의 참조가 둘 오는데, `plan_boundaries`·`transcribe_boundaries`의
    `_index_by`는 같은 값이어도 신원이 두 번 나오면 거부한다 — 유일화가 없으면
    정상 입력이 거기서 죽는다. 트레이스는 `transcribe_unique_sources`가 이미
    정규화해 주지만 참조를 정규화해 주는 곳은 없다.
    """
    by_source: dict[str, PcmReference] = {}
    references: list[PcmReference] = []
    for _media, second in inputs:
        if isinstance(second, PcmReference) and second.source_id not in by_source:
            by_source[second.source_id] = second
            references.append(second)
    return tuple(references)


def _vad_session(
    prepared: PreparedAudio, references: Sequence[PcmReference], injected: object | None
) -> object | None:
    """ONNX 게이트 — 유료 호출 **앞**에서 한 번만 만든다(해시 검증 하나라 공짜다).

    아티팩트가 깨진 환경에서 세션을 전사 뒤에 만들면 77분 회차 전사비를 다 치르고
    나서 `VadUnavailable`로 죽는다 — `require_ffmpeg`를 자원 루프 앞으로 뺀 것과
    같은 논리다. 참조가 하나도 없으면(전 리소스가 `declared_no_audio`) VAD를 한 번도
    안 쓰므로 묻지도 않는다: extra 없는 환경에서 쓰지도 않을 이유로 죽지 않는다.
    환경 실패는 소스마다 부재 기록 N개로 번역하지 않고 그대로 올라온다.
    """
    if injected is not None:
        return injected
    if not references:
        return None
    return create_cpu_session(bundled_artifact(prepared.project_config))


def _projected_guards(
    prepared: PreparedAudio,
    references: Sequence[PcmReference],
    traces: Sequence[SourceTrace],
    slices: Sequence[LogicalSlice],
    boundary: BoundaryProjection,
    vad_by_source: Mapping[str, VadResult],
) -> tuple[tuple[SourceGuard, ...], dict[str, InstanceGuard]]:
    """참조마다 소스 가드를 만들고 인스턴스로 내린다 — 합성 가드는 별도다(아래)."""
    traces_by_source = _traces_by_source(traces)
    slices_by_source: dict[str, list[LogicalSlice]] = {}
    for slice_ in slices:
        slices_by_source.setdefault(slice_.source_id, []).append(slice_)
    by_instance: dict[str, InstanceGuard] = {}
    source_guards: list[SourceGuard] = []
    for reference in references:
        trace = traces_by_source.get(reference.source_id)
        if trace is None:
            raise ValueError(
                f"소스의 전사 트레이스가 없다: source_id={reference.source_id!r}"
                " — 가드를 지어내지 않는다"
            )
        guard = build_source_guard(
            trace,
            reference,
            vad_by_source[reference.source_id],
            boundary,
            (),
            prepared.project_config,
        )
        source_guards.append(guard)
        _add_all(
            by_instance,
            project_guard(
                guard,
                reference,
                slices_by_source.get(reference.source_id, ()),
                prepared.document.frame_duration,
            ),
        )
    return tuple(source_guards), by_instance


def _unavailable_guards(prepared: PreparedAudio) -> tuple[InstanceGuard, ...]:
    """부재 소스의 인스턴스마다 합성 가드 — 문서 클립 순서 그대로.

    `protected_frames`는 `()`지만 그것이 안전한 이유는 `status`가 막기 때문이다
    (`edit.py`: keep_all·inaudible은 빈 protected여도 제거 권한이 없다).
    **`status="success"`인 합성 가드는 어떤 이유로도 만들지 않는다** — 소리를 못
    들은 클립이 "보호할 것이 없는 클립"이 되는, 이 저장소에서 여섯 번 재발한 결함이다.
    """
    guards: list[InstanceGuard] = []
    for clip in _document_clips(prepared.document):
        second = prepared.inputs_by_resource[clip.resource_scoped_id][1]
        if not isinstance(second, UnavailableSourceAudio):
            continue
        guards.append(
            InstanceGuard(
                instance_id=clip.instance_id,
                source_id=second.source_id,
                status=_UNAVAILABLE_GUARD_STATUS[second.reason_code],
                protected_frames=(),
                review=(f"소스 오디오 부재({second.reason_code}) — {second.reason}",),
            )
        )
    return tuple(guards)


def _document_order_guards(
    document: ProjectDocument, by_instance: Mapping[str, InstanceGuard]
) -> tuple[InstanceGuard, ...]:
    """가드를 문서의 클립 순서로 재배열한다 — 소스별로 뭉친 순서가 아니라.

    `project_guard`는 슬라이스 × 인스턴스 순서로 내놓는다. 소스마다 부른 결과를
    이어 붙이면 소스별로 뭉친 순서이고, `_require_guard_linkage`는 순서를 보지
    않으므로 하류는 통과한다 — 문서 순서를 지키는 곳은 여기뿐이다.
    """
    ordered: list[InstanceGuard] = []
    for clip in _document_clips(document):
        guard = by_instance.get(clip.instance_id)
        if guard is None:
            raise ValueError(
                f"클립 인스턴스에 guard가 없다: {clip.element_path}"
                " — 없음이 보호 없음이 되면 안 된다"
            )
        ordered.append(guard)
    return tuple(ordered)


def _document_clips(document: ProjectDocument) -> tuple[ClipInstance, ...]:
    """스토리라인의 클립 순서 — 갭은 가드와 무관하다."""
    return tuple(item for item in document.storyline if isinstance(item, ClipInstance))


def _traces_by_source(traces: Sequence[SourceTrace]) -> dict[str, SourceTrace]:
    """트레이스를 source_id로 묶는다 — 중복은 곧 모호함이므로 그 자리에서 거부한다."""
    indexed: dict[str, SourceTrace] = {}
    for trace in traces:
        if trace.source_id in indexed:
            raise ValueError(f"소스 하나에 트레이스가 둘 이상이다: {trace.source_id!r}")
        indexed[trace.source_id] = trace
    return indexed


def _add_all(by_instance: dict[str, InstanceGuard], guards: Sequence[InstanceGuard]) -> None:
    """가드를 인스턴스로 무는다 — 둘 이상이면 어느 쪽이 진짜 보호인지 말할 수 없다."""
    for guard in guards:
        if guard.instance_id in by_instance:
            raise ValueError(
                f"인스턴스에 guard가 둘 이상 있다: {guard.instance_id!r} — 정확히 하나여야 한다"
            )
        by_instance[guard.instance_id] = guard


def _log_analysis(result: AnalyzedAudio) -> None:
    """단계의 결산 — 부재·VAD 실패도 세어 보고한다(있는 것만 세면 조용한 손실이 된다)."""
    vad_failed = sum(1 for vad in result.vad_by_source.values() if vad.status != "success")
    log.info(
        f"분석 완료: 인스턴스 가드 {len(result.instance_guards)}개 — "
        f"소스 가드 {len(result.source_guards)}, 경계 재전사 {len(result.transcripts)}, "
        f"부재 인스턴스 {len(result.unavailable_instances)}, VAD 실패 {vad_failed}"
    )


# ═══════════════════════════════════════════════════════════════
#  PROTO-4 — 블록 · 프레임 표본 · 비전 판정 · 계획 · EDIT 자막 · 패키지 (러너 3/3)
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PublishedProject:
    """출판이 끝난 상태 — 러너의 마지막 산출물.

    `decisions`는 블록 순서 그대로 블록마다 정확히 하나다. `findings`는 성공한
    결과에서 언제나 `()`다 — `publish_package`가 스스로 재검사해 findings가 있으면
    거부하므로(export) "검사했음"의 기록으로만 유효하다. `dtd_path`는 실제로 쓴
    값이다: `validate_xml`이 `dtd_checked=False`를 돌려주면 `_write_revision`이
    그것을 버리므로 패키지 기록 어디에도 "DTD를 대조했는가"가 남지 않는다 —
    러너가 노출하는 것이 유일한 완화다(export 쪽 수정은 이 태스크 범위 밖).
    """

    analyzed: AnalyzedAudio
    partition: BlockPartition
    decisions: tuple[VisionDecision, ...]
    plan: EditPlan
    findings: tuple[str, ...]
    edit_captions: EditCaptionSet
    package: ExportPackage
    dtd_path: Path | None

    def __post_init__(self) -> None:
        """같은 세 단언의 뒤쪽 난간 — 출판 앞 검사를 지난 값이라도 다시 못 박는다."""
        _require_decision_alignment(self.partition.blocks, self.decisions)


def publish_project(
    analyzed: AnalyzedAudio,
    *,
    vision_client: object,
    destination: Path,
    online: bool = False,
    dtd_path: Path | None = None,
) -> PublishedProject:
    """분석이 끝난 상태를 패키지로 출판한다 — 러너의 마지막 조각.

    비전 클라이언트는 주입받는다(openai 경계는 `vlogsrt/transcribe.py`뿐이다 —
    계약 §4). `online`은 사람의 승인을 표현하는 러너의 인자이고 `request_vision`에
    그대로 간다: 거짓이면 블록마다 오류 판정이 남고 `final_accepted`가 막힌다 —
    그것이 정직한 닫힘이다(승인의 부재가 항상 승인이 되는 길을 끊는다).
    """
    prepared = analyzed.prepared
    partition, decisions = _partition_and_decide(analyzed, vision_client, online)
    _require_decision_alignment(partition.blocks, decisions)
    plan = build_edit_plan(
        prepared.document, partition, analyzed.instance_guards, decisions, prepared.project_config
    )
    findings = validate_edit_plan(plan, prepared.document, analyzed.instance_guards)
    edit_captions = project_edit_captions(plan, analyzed.boundary)
    package = publish_package(
        destination,
        prepared.document,
        prepared.media,
        plan,
        edit_captions,
        source=analyzed.boundary,
        guards=analyzed.instance_guards,
        source_guards=analyzed.source_guards,
        dtd_path=dtd_path,
    )
    result = PublishedProject(
        analyzed=analyzed,
        partition=partition,
        decisions=decisions,
        plan=plan,
        findings=findings,
        edit_captions=edit_captions,
        package=package,
        dtd_path=dtd_path,
    )
    _log_publish(result)
    return result


def _partition_and_decide(
    analyzed: AnalyzedAudio, vision_client: object, online: bool
) -> tuple[BlockPartition, tuple[VisionDecision, ...]]:
    """분할 → 블록마다 판정 — 설정 게이트가 블록 루프 앞에 선다.

    `build_payload`와 `parse_decision`의 설정 검사는 같은 예외 타입이라 블록 루프
    안에서는 구분할 수 없다. 루프 앞에서 한 번 통과시켜 그 뒤의 `VisionPolicyError`는
    전부 그 블록의 사실이게 한다 — 나쁜 설정이 "그 블록이 실패했다" 기록으로
    세탁되면 나머지 블록의 판정까지 버리게 된다.
    """
    prepared = analyzed.prepared
    cfg = prepared.project_config
    _require_policy_config(cfg)
    partition = partition_blocks(
        prepared.document,
        prepared.media,
        analyzed.instance_guards,
        analyzed.locked_instance_ids,
        analyzed.unavailable_instances,
        cfg,
    )
    media_by_content = _media_by_content(prepared.media)
    decisions = tuple(
        _decide_block(
            block,
            media_by_content[block.source_id],
            vision_client,
            prepared.frames_dir,
            cfg,
            online,
        )
        for block in partition.blocks
    )
    return partition, decisions


def _decide_block(
    block: EditBlock,
    media: ResolvedMedia,
    vision_client: object,
    frames_dir: Path,
    cfg: ProjectConfig,
    online: bool,
) -> VisionDecision:
    """블록 하나의 판정 — 표본·조립·전송 실패는 그 블록의 오류 판정이다.

    모델 응답 결함은 `parse_decision`이 이미 오류 판정으로 정규화하므로 그 경로를
    재구현하지 않는다. 전송 실패는 넓게 잡는다(SDK 예외 타입을 import 없이 분간할
    수 없으므로) — 잡은 즉시 오류 판정으로 바꿔 남기고 조용히 두지도 밖으로
    던지지도 않는다. 예외가 블록 루프를 넘어 올라오면 JPEG 한 장 때문에 프로젝트
    전체 출판이 죽고 나머지 블록의 판정까지 버린다.
    """
    request = vision_request_for(block, _PROJECTION_CONTEXT)
    try:
        samples = sample_frames(media, block.sample_requests, frames_dir, cfg)
    except (FrameSamplingError, OSError) as exc:
        return _failure_decision(request, "프레임 표본 실패", exc)
    try:
        payload = build_payload(request, samples, cfg)
    except VisionPolicyError as exc:
        return _failure_decision(request, "비전 페이로드 조립 실패", exc)
    try:
        raw = transcribe.request_vision(
            vision_client,
            model=cfg.vision_model,
            payload=payload,
            online=online,
        )
    except Exception as exc:  # noqa: BLE001 - 블록의 실패 기록으로 바꿔 남긴다
        return _failure_decision(request, "비전 호출 실패", exc)
    return parse_decision(request, raw, cfg)


def _failure_decision(request: VisionRequest, stage: str, exc: BaseException) -> VisionDecision:
    """실패의 판정 — 모양은 정본(`policy._error_decision`)에서, 글월은 실제 예외에서.

    정본이 다는 머리글("비전 응답이 계약을 어겼다")은 모델 응답 결함의 문장이다.
    표본·조립·전송 실패는 다른 사실이므로 머리글만 단계 이름으로 바꾼다 — 나머지
    칸(choice·요청 신원·증거·신뢰도·상태)은 여전히 정본이 계산한다. 손으로 다시
    짜다 하나라도 어긋나면 `_require_decision_linkage`가 raise해 "차단된 패키지"가
    "패키지 없음"으로 바뀐다.
    """
    reason = " ".join(str(exc).split()) or exc.__class__.__name__
    return replace(_error_decision(request, ""), reason=f"{stage} — {reason}")


def _media_by_content(media: Sequence[ResolvedMedia]) -> dict[str, ResolvedMedia]:
    """내용 신원 → 미디어 — 문서 순서에서 첫 항목이 이긴다.

    블록이 드는 `source_id`는 내용 신원인데, 서로 다른 xml_id가 같은 바이트를
    가리키면 같은 신원에 `ResolvedMedia`가 여럿 온다. 원본 바이트는 같아도 프록시
    연결은 다를 수 있고 어느 쪽을 고르느냐가 `used_proxy`·추출 스트림·`review` 사유를
    바꾼다 — 결정성은 이 규칙 한 줄에 달려 있다.
    """
    indexed: dict[str, ResolvedMedia] = {}
    for row in media:
        indexed.setdefault(row.asset_content_id, row)
    return indexed


def _require_decision_alignment(
    blocks: Sequence[EditBlock], decisions: Sequence[VisionDecision]
) -> None:
    """판정 정렬의 난간 — 세 검사가 각자 따로 문다: 개수 → 중복 → 순서·신원.

    `__post_init__`에만 두면 `publish_package`가 `current` 포인터를 원자적으로
    갈아끼운 **뒤에** 돈다 — 디스크에는 결함 리비전이 이미 공개된 채 단언이
    터진다. 파티션과 판정만 있으면 전부 잴 수 있으므로 같은 검사를 출판 앞
    (`build_edit_plan` 호출 전)에서 먼저 돈다. 블록이 0개면 판정도 0개다 —
    `decisions == ()`은 결함이 아니라 정상이다.
    """
    if len(decisions) != len(blocks):
        raise ValueError(
            f"판정 {len(decisions)}개가 파티션 블록 {len(blocks)}개와 다르다"
            " — 블록마다 정확히 하나여야 한다"
        )
    seen: set[str] = set()
    for decision in decisions:
        if decision.block_id in seen:
            raise ValueError(f"판정이 같은 블록을 두 번 가리킨다: {decision.block_id!r}")
        seen.add(decision.block_id)
    for block, decision in zip(blocks, decisions, strict=True):
        if decision.block_id != block.block_id:
            raise ValueError(
                f"판정의 순서·신원이 블록과 어긋난다: 판정 {decision.block_id!r} ≠"
                f" 블록 {block.block_id!r} — 블록 순서 그대로여야 한다"
            )


def _log_publish(result: PublishedProject) -> None:
    """단계의 결산 — 오류 판정도 세어 보고한다(있는 것만 세면 조용한 손실이 된다)."""
    errors = sum(1 for decision in result.decisions if decision.status == "error")
    removed = len(result.plan.removed)
    log.info(
        f"출판 완료: 블록 {len(result.partition.blocks)}개 — 판정 {len(result.decisions)}개"
        f" (오류 {errors}), 제거 {removed}건, 리비전 {result.package.plan_revision[:12]}"
    )
