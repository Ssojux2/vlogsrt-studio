"""A4 의미 분석 작업 본문 — 받아들인 이해 라이브러리를 서버 소유 작업으로 잇는다.

`understanding.py`·`understanding_luna.py`(받아들임 완료)가 값과 순수 계산을
맡고, 이 모듈은 그것을 **재개 가능한 작업**으로 조립한다(§14.1). 한 원본씩:
SHA-256 재검증 → 조사(시계·오디오 트랙) → (오디오 있으면) PCM 선택·전체 청크
커버리지·내부 ASR 단일 패스 → 공통 파일 시간축 시간창 → 실제 프레임 표본 →
Luna 판정 → **비디오 격자 조정** → 불변 문서 기록.

지키는 선(§7·§13.3·§14):

- **오디오가 없으면 ASR 작업을 만들지 않는다.** 오디오 부재는 무발화와 다른
  상태고, Luna는 프레임 증거만으로 장면을 설명한다 — 빈 PCM·빈 발화·추측 발화를
  지어내지 않는다(§7.3).
- **실제 호출은 승인이 검증한 문맥에서만.** Luna는 라이브러리의 자체 검증
  응답자(`validated_luna_responder`)를, 내부 ASR는 이 모듈의 승인 게이트
  (`ApprovedTimingProvider`)를 지난다 — 환경·설정·출처·분량이 전송 순간에도
  같아야 하고, 청크마다 예약이 먼저다(§14.3).
- **재생은 재생이라고 기록한다.** 픽스처 재생의 근원은 `replay`다 — 없는
  픽스처는 실패로 남지 지어내지 않는다(§14.3).
- **경계는 하나의 비디오 격자로 조정된다.** 받아들인 경계만 자르고, 스냅이
  발화 중간 컷을 만들면 버리고, 마지막 프레임 뒤 오디오 꼬리는 마지막 조각에
  속한다(§7.6·§8.2). 못 믿는 경계는 강제 절단이 아니라 검토 표시다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from datetime import date as date_type
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from vlogsrt import log, preprocess
from vlogsrt.project.raw import media_refs, source_transcript
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    require_live_asr_approval,
)
from vlogsrt.project.raw.analysis_config import (
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.analysis_store import (
    ANALYSIS_ROOT,
    AnalysisStore,
    new_analysis_revision_id,
)
from vlogsrt.project.raw.caption_provider import (
    TimingProvider,
    TransportTimingProvider,
    parse_timing_response,
)
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext
from vlogsrt.project.raw.models import (
    AnalysisError,
    AnalysisRevision,
    AnalysisSourceResult,
    GlobalOrderRevision,
    PlannedInterval,
    RawWorkspaceError,
    SourceAsset,
)
from vlogsrt.project.raw.understanding import (
    AsrChunkOutcome,
    AsrEvidence,
    BoundaryCandidate,
    CommonFileTimeline,
    FrameSamplingResult,
    SegmentationProposal,
    SegmentProposal,
    SelectedAudio,
    UtteranceEvidence,
    VideoGrid,
    WindowPlan,
    WindowVerdict,
    common_timeline,
    plan_common_windows,
    utterances_in_common_clock,
)
from vlogsrt.project.raw.understanding_luna import (
    AnalysisApprovalError,
    OnlineAnalysisApproval,
    OnlineVisualAnalysisApproval,
    ReplaySemanticResponder,
    SemanticAnnotation,
    SemanticResponder,
    annotation_json,
    document_execution_mode,
    validated_visual_luna_responder,
    visual_analysis_fingerprint,
)
from vlogsrt.project.raw.workspace import RawWorkspace

INPUT_FINGERPRINT_SCHEMA = "raw-analysis-input/v1"
"""원본별 입력 지문의 schema — 캐시 무효화가 이 값으로 갈라진다(§14.2)."""

ANALYSIS_DOCUMENT_NAME = "analysis_transcript.json"
"""원본 하나의 첫 단계 산출문서 — SRT가 아니다(§7.1)."""

ANALYSIS_RECORD_NAME = "analysis_record.json"
"""A4 조정 기록(사이드카) — 리비전·시간축·구간·캐시 출처를 실은 실행 증명."""

ASR_PHASE_DOCUMENT_NAME = "asr_phase.json"
"""1단계(내부 ASR) 불변 기록(A4-F1) — 전사·발화·창·프레임과 실측 2단계 지문."""

ASR_PHASE_SCHEMA = "raw-analysis-asr-phase/v1"
"""1단계 기록의 schema — 2단계 재개가 이 값으로 기록 모양을 검증한다."""

REPLAY_FIXTURES_DIRNAME = "replay"
"""작업공간 안 재생 픽스처의 관습 자리 — `analysis/replay/`."""


class ReplayFixtureError(AnalysisError):
    """재생 픽스처가 계약을 어긴다 — 청크·창의 실패로 기록된다(지어내지 않는다)."""


class AsrBudgetExhausted(AnalysisError):
    """내부 ASR 예약 한도를 넘었다 — 새 청크 전송은 시작하지 않는다."""


# ─────────────────────────────────────────────────────────────
#  재생 픽스처 — 명시적으로 선언된 진실만 재생한다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplayFixtures:
    """결정적 재생 묶음 — 오디오 발화 선언·청크 정밀 응답·Luna 창 판정.

    `sources`의 좌표는 **오디오 정규화 시계(초)**다 — 같은 파일을 다시 뽑으면
    같은 청크에 같은 구간이 겹친다. `chunks`는 WAV sha로 직접 재는 정밀 경로다.
    어느 쪽에도 없는 청크는 재생되지 않고 실패로 남는다.
    """

    sources: Mapping[str, tuple[tuple[float, float, str], ...]] = field(default_factory=dict)
    chunks: Mapping[str, tuple[tuple[float, float, str], ...]] = field(default_factory=dict)
    windows: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> ReplayFixtures:
        return cls()

    def luna_responder(self) -> ReplaySemanticResponder:
        """창 판정 재생자 — 없는 창은 예외로 실패 판정이 된다(빈 성공이 아니다)."""
        responses = {
            window_id: {"output_text": json.dumps(body, ensure_ascii=False)}
            for window_id, body in self.windows.items()
        }
        return ReplaySemanticResponder(responses)

    def asr_for(self, source_sha256: str) -> ReplayAsrProvider:
        """원본 하나의 ASR 재생자 — 선언이 없으면 모든 청크가 실패로 남는다."""
        declared = self.sources.get(source_sha256)
        return ReplayAsrProvider(declared=declared, exact=self.chunks)


def load_replay_fixtures(directory: Path) -> ReplayFixtures:
    """픽스처 디렉터리를 읽는다 — `asr-fixtures.json`·`luna-fixtures.json`.

    디렉터리가 없으면 빈 묶음이다(재생은 실패로 기록된다 — 조용한 빈 성공이
    아니다). 어느 파일도 강제하지 않는다: 있는 것만 재생에 들어간다.
    """
    if not directory.is_dir():
        return ReplayFixtures.empty()
    sources: dict[str, tuple[tuple[float, float, str], ...]] = {}
    chunks: dict[str, tuple[tuple[float, float, str], ...]] = {}
    asr_path = directory / "asr-fixtures.json"
    if asr_path.is_file():
        payload = _read_json(asr_path)
        sources = {
            str(sha): _segments(entry, asr_path)
            for sha, entry in _mapping_field(payload, "sources", asr_path).items()
        }
        chunks = {
            str(sha): _segments(entry, asr_path)
            for sha, entry in _mapping_field(payload, "chunks", asr_path).items()
        }
    windows: dict[str, Mapping[str, object]] = {}
    luna_path = directory / "luna-fixtures.json"
    if luna_path.is_file():
        payload = _read_json(luna_path)
        windows = {
            str(key): value for key, value in _mapping_field(payload, "windows", luna_path).items()
        }
    return ReplayFixtures(sources=sources, chunks=chunks, windows=windows)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplayFixtureError(f"픽스처를 읽지 못했다: {path} — {error}") from error
    if not isinstance(payload, dict):
        raise ReplayFixtureError(f"픽스처가 JSON 객체가 아니다: {path}")
    return payload


def _mapping_field(payload: Mapping[str, Any], key: str, path: Path) -> Mapping[str, Any]:
    value = payload.get(key) or {}
    if not isinstance(value, Mapping):
        raise ReplayFixtureError(f"{path.name}의 {key}가 객체가 아니다: {path}")
    return value


def _segments(entry: object, path: Path) -> tuple[tuple[float, float, str], ...]:
    if not isinstance(entry, list):
        raise ReplayFixtureError(f"발화 선언이 배열이 아니다: {path}")
    segments: list[tuple[float, float, str]] = []
    for item in entry:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ReplayFixtureError(
                f"발화 선언 항목은 start/end/text 모양이어야 한다: {path} ({item!r})"
            )
        segments.append((float(item["start"]), float(item["end"]), str(item["text"])))
    return tuple(segments)


@dataclass(frozen=True, slots=True)
class ReplayAsrProvider:
    """픽스처 재생 시간 제공자 — 선언된 발화만, 청크 국소 초로 내놓는다.

    `declared`는 원본 오디오 정규화 시계의 선언이다(`None`은 '선언 자체가 없다'
    — 모든 청크가 실패로 기록된다). 청크에 걸치는 발화는 그 청크 안 구간으로
    잘려 나온다 — 전체 합은 원래 선언을 빈틈없이 덮는다. `exact`는 WAV sha로
    재는 정밀 선언으로, 있으면 앞선다.
    """

    declared: Sequence[tuple[float, float, str]] | None
    exact: Mapping[str, tuple[tuple[float, float, str], ...]] = field(default_factory=dict)

    def timed_transcript(
        self,
        *,
        binding: Any,
        chunk: Any,
        settings: RawWorkflowConfig,
        on: date_type,
        stem: str = "",
        source: str = "",
    ) -> Any:
        exact = self.exact.get(str(binding.audio_sha256))
        if exact is not None:
            segments = exact
        elif self.declared is not None:
            segments = _project_into_chunk(self.declared, binding)
        else:
            raise ReplayFixtureError(
                f"재생할 발화 선언이 없다(원본 sha 미등록) — 청크 {binding.request_id}"
            )
        return parse_timing_response(
            {
                "segments": [
                    {"start": start, "end": end, "text": text} for start, end, text in segments
                ]
            },
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


def _project_into_chunk(
    declared: Sequence[tuple[float, float, str]], binding: Any
) -> tuple[tuple[float, float, str], ...]:
    """원본 오디오 시계의 선언을 청크 국소 시계로 — 겹침만, 지어내지 않고."""
    rate = int(binding.sample_rate)
    start_at = float(Fraction(int(binding.sample_start), rate))
    end_at = float(Fraction(int(binding.sample_end), rate))
    duration = end_at - start_at
    local: list[tuple[float, float, str]] = []
    for start, end, text in declared:
        left = max(0.0, start - start_at)
        right = min(duration, end - start_at)
        if right <= left:
            continue
        local.append((left, right, text))
    return tuple(local)


# ─────────────────────────────────────────────────────────────
#  실제 호출 게이트 — 내부 ASR(승인·예약·수명주기를 전송 순간에 다시)
# ─────────────────────────────────────────────────────────────


class AsrReservationGate(Protocol):
    """청크 예약 문 — 실제 전송 열기 전에 한도를 남겨둔다."""

    def reserve(self, *, count: int, key: str) -> None: ...


@dataclass
class CountingAsrGate:
    """ASR 청크 예약 게이트 — 재시도까지 정직하게 센다(§14.3).

    창 단위 예약과 달리 같은 키의 재예약도 센다: 실패한 청크의 재시도는 새
    호출이므로 예산을 다시 쓴다 — 이미 발생한 비용이 취소되는 것은 아니다.
    """

    limit: int
    remaining: int = 0
    reserved: int = 0

    def __post_init__(self) -> None:
        self.remaining = self.limit

    def reserve(self, *, count: int, key: str) -> None:
        _ = key  # 재시도를 구분하지 않는다 — 호출 수가 곧 예산이다
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"예약 수는 1 이상 정수다: {count!r}")
        if count > self.remaining:
            raise AsrBudgetExhausted(
                f"내부 ASR 청크 예약 한도를 넘었다 — 남음 {self.remaining}, 요청 {count}"
            )
        self.remaining -= count
        self.reserved += count


@dataclass(frozen=True, slots=True)
class ApprovedTimingProvider:
    """승인된 원본의 내부 ASR 전송 경계 — 청크마다 문을 다시 지난다.

    승인(`OnlineAnalysisApproval`)이 본 출처 해시·스트림·분량과 지금의 선택이
    같은지, 환경(`VLOGSRT_OFFLINE`)과 설정(`network_mode`)이 열려 있는지를
    **전송 직전에** 다시 본다(§14.3). 통과하면 예약 문을 지나 실제 전송 경계
    (`inner`)로 간다 — 그 안에서 오늘 날짜의 수명주기 검사가 먼저 돈다.
    """

    inner: TimingProvider
    workflow: RawWorkflowConfig
    audio: SelectedAudio
    approval: OnlineAnalysisApproval
    gate: AsrReservationGate

    def timed_transcript(
        self,
        *,
        binding: Any,
        chunk: Any,
        settings: RawWorkflowConfig,
        on: date_type,
        stem: str = "",
        source: str = "",
    ) -> Any:
        blocked = luna.online_env_blocked()
        if blocked is not None:
            raise AnalysisApprovalError(blocked)
        if self.workflow.network_mode != "online" or settings.network_mode != "online":
            raise AnalysisApprovalError(
                "네트워크 모드가 online이 아니다 — 내부 ASR 전송 문이 닫혀 있다"
            )
        if self.audio.source_sha256 != self.approval.source_sha256:
            raise AnalysisApprovalError("승인이 본 원본과 지금의 원본이 다르다")
        if self.approval.asr_model != settings.models.timing_model:
            raise AnalysisApprovalError("승인이 본 ASR 모델과 설정 모델이 다르다")
        if self.audio.audio_stream_index != self.approval.audio_stream_index:
            raise AnalysisApprovalError("승인이 본 오디오 스트림과 지금의 선택이 다르다")
        if self.audio.sample_count != self.approval.sample_count:
            raise AnalysisApprovalError("승인이 본 오디오 분량과 지금의 분량이 다르다")
        if binding.sample_start < 0 or binding.sample_end > self.audio.sample_count:
            raise AnalysisApprovalError(
                f"청크 범위가 선택 오디오 밖이다: [{binding.sample_start}, {binding.sample_end})"
                f" / {self.audio.sample_count} 샘플"
            )
        self.gate.reserve(count=1, key=str(binding.request_id))
        return self.inner.timed_transcript(
            binding=binding, chunk=chunk, settings=settings, on=on, stem=stem, source=source
        )


@dataclass(frozen=True, slots=True)
class ApprovedPhaseAsrProvider:
    """1단계(내부 ASR) 전송 경계 — `AsrPhaseApproval`을 청크마다 다시 지난다.

    단일 승인형 응답자와 다른 점은 묶는 값이다: 1단계 승인은 전사 **전에** 알 수
    있는 값(출처·스트림·설정 지문·모델·예산)만 묶으므로, 전송 순간의 재검사도
    그 값들을 본다. 청크 범위가 선택 오디오 밖이면 닫고, 예약 문
    (`CountingAsrGate`)은 승인 예산을 넘지 않는다. 수명주기(종료 예정일)는 안쪽
    전송 경계가 맨 먼저 돈다(§11.10·§14.3).
    """

    inner: TimingProvider
    workflow: RawWorkflowConfig
    understanding: RawUnderstandingSettings
    audio: SelectedAudio
    approval: AsrPhaseApproval
    gate: AsrReservationGate

    def timed_transcript(
        self,
        *,
        binding: Any,
        chunk: Any,
        settings: RawWorkflowConfig,
        on: date_type,
        stem: str = "",
        source: str = "",
    ) -> Any:
        require_live_asr_approval(
            self.approval,
            workflow=self.workflow,
            understanding=self.understanding,
            source_sha256=self.audio.source_sha256,
            on=on,
        )
        if settings.network_mode != "online":
            raise AnalysisApprovalError(
                "네트워크 모드가 online이 아니다 — 내부 ASR 전송 문이 닫혀 있다"
            )
        if self.audio.audio_stream_index != self.approval.audio_stream_index:
            raise AnalysisApprovalError("승인이 본 오디오 스트림과 지금의 선택이 다르다")
        if binding.sample_start < 0 or binding.sample_end > self.audio.sample_count:
            raise AnalysisApprovalError(
                f"청크 범위가 선택 오디오 밖이다: [{binding.sample_start}, {binding.sample_end})"
                f" / {self.audio.sample_count} 샘플"
            )
        self.gate.reserve(count=1, key=str(binding.request_id))
        return self.inner.timed_transcript(
            binding=binding, chunk=chunk, settings=settings, on=on, stem=stem, source=source
        )


# ─────────────────────────────────────────────────────────────
#  원본별 입력 지문 — 캐시 무효화의 열쇠(§14.2)
# ─────────────────────────────────────────────────────────────


def source_input_fingerprint(
    *,
    source_sha256: str,
    video: VideoGrid,
    audio: SelectedAudio | None,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    media_fingerprint: str = "original-only/v1",
    transcript_fingerprint: str = "asr-original/v1",
) -> str:
    """원본 하나의 분석 입력 지문 — 결과에 영향을 주는 입력만 묶는다.

    출처 내용 해시·비디오 격자(원점·프레임 길이·프레임 수)·오디오 신원(스트림·
    분량·원점)·분석 설정(창·문맥·표본 수·긴변·프롬프트 리비전·청크 상한)·모델.
    하나라도 바뀌면 **그 원본의 캐시만** 무효가 된다. 표시 전용 값은 오지 않는다.
    """
    parts = [
        INPUT_FINGERPRINT_SCHEMA,
        media_fingerprint,
        transcript_fingerprint,
        UNDERSTANDING_SCHEMA_VERSION,
        source_sha256,
        str(video.stream_index),
        _fraction_text(video.presentation_origin),
        _fraction_text(video.frame_duration),
        str(video.frame_count),
        "-"
        if audio is None
        else "|".join(
            [
                str(audio.audio_stream_index),
                f"{audio.sample_rate}/{audio.channels}/{audio.sample_count}",
                _fraction_text(audio.pcm_sample0_file_time),
            ]
        ),
        "|".join(
            [
                str(settings.window_seconds),
                str(settings.context_seconds),
                str(settings.max_images_per_window),
                str(settings.image_long_edge),
                str(settings.prompt_revision),
                str(settings.asr_chunk_max_seconds),
                str(settings.asr_chunk_max_bytes),
            ]
        ),
        workflow.models.timing_model,
        workflow.models.luna_model,
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


# ─────────────────────────────────────────────────────────────
#  경계 조정 — 받아들인 경계를 하나의 비디오 격자로(§7.6·§8.2)
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceReconciliation:
    """원본 하나의 조정 결과 — 비디오 격자 위의 제안·구간·버린 경계·검토 이유."""

    proposal: SegmentationProposal
    intervals: tuple[PlannedInterval, ...]
    dropped_boundary_ids: tuple[str, ...]
    review_reasons: tuple[str, ...]


def reconcile_to_video_lattice(
    timeline: CommonFileTimeline,
    plan: WindowPlan,
    verdicts: Mapping[str, WindowVerdict],
    spoken: Sequence[UtteranceEvidence],
    asr_failures: Sequence[AsrChunkOutcome] = (),
) -> SourceReconciliation:
    """판정을 하나의 비디오 격자로 확정한다 — 전체 비디오 보존이 언제나 이긴다.

    규칙: 받아들인 창의 제안 경계만 자르며, 발화 중간(열린 구간)에 걸리는 경계는
    **옮기지 않고 버린다**. 격자 스냅(프레임 바닥)이 경계를 발화 안으로 밀면 그
    경계도 버린다 — 스냅이 발화 중간 컷을 만드는 길은 없다. 마지막 프레임 뒤의
    경계(오디오 꼬리)는 아예 후보가 아니었다(§8.2 — 꼬리는 마지막 조각에만).
    판정이 없거나 거짓인 창은 자르지 않는다(보수적 긴 조각 + 검토 표시). 강제
    절단(목표 길이)은 이 함수에 존재하지 않는다(§7.6).
    """
    audio_start = timeline.audio_start if timeline.audio_start is not None else Fraction(0)
    cut_frames: list[int] = []
    dropped: list[str] = []
    reasons: list[str] = []
    for window in plan.windows:
        verdict = verdicts.get(window.window_id)
        if verdict is None:
            reasons.append(f"{window.window_id}:판정 없음 — 보수적 유지")
            continue
        if verdict.needs_review:
            reasons.append(f"{window.window_id}:{verdict.note or '검토 필요'}")
        if not verdict.accepted:
            continue
        for boundary_id in verdict.suggested_boundary_ids:
            candidate = plan.boundary(boundary_id)
            if candidate is None or candidate.window_id != window.window_id:
                dropped.append(boundary_id)
                reasons.append(f"{window.window_id}:{boundary_id}:제공되지 않은 경계 — 거부")
                continue
            frame, note = _cut_frame(timeline, candidate, spoken)
            if note is not None or frame is None:
                dropped.append(boundary_id)
                reasons.append(f"{window.window_id}:{boundary_id}:{note}")
                continue
            if frame in cut_frames:
                dropped.append(boundary_id)
                reasons.append(f"{window.window_id}:{boundary_id}:같은 프레임으로 접힌다 — 유지")
                continue
            cut_frames.append(frame)
    proposal = _lattice_segments(
        timeline, plan, sorted(set(cut_frames)), spoken, asr_failures, audio_start, reasons
    )
    return SourceReconciliation(
        proposal=proposal,
        intervals=tuple(
            PlannedInterval(start=segment.start, end=segment.end) for segment in proposal.segments
        ),
        dropped_boundary_ids=tuple(dropped),
        review_reasons=tuple(reasons),
    )


def _cut_frame(
    timeline: CommonFileTimeline,
    candidate: BoundaryCandidate,
    spoken: Sequence[UtteranceEvidence],
) -> tuple[int | None, str | None]:
    """후보 하나를 프레임 경계로 — 못 믿으면 (None, 이유)를 돌려준다.

    스냅은 **바깥(다음 프레임, 올림)**이다 — 발화 종료를 앞 조각에 온전히
    남긴다(§7.6). 올림이 다음 발화의 안쪽에 경계를 놓으면 그 경계도 버린다:
    스냅이 발화 중간 컷을 만드는 길은 없다.
    """
    if any(item.start < candidate.position < item.end for item in spoken):
        return None, "발화 중간 — 버리고 긴 조각 유지"
    frame = math.ceil(candidate.position / timeline.video.frame_duration)
    if frame <= 0 or frame >= timeline.video.frame_count:
        return None, "비디오 격자 밖이다 — 오디오 꼬리는 마지막 조각에 속한다"
    snapped = frame * timeline.video.frame_duration
    if any(item.start < snapped < item.end for item in spoken):
        return None, "격자 스냅이 발화 중간에 놓는다 — 버리고 긴 조각 유지"
    return frame, None


def _lattice_segments(
    timeline: CommonFileTimeline,
    plan: WindowPlan,
    cut_frames: Sequence[int],
    spoken: Sequence[UtteranceEvidence],
    asr_failures: Sequence[AsrChunkOutcome],
    audio_start: Fraction,
    reasons: Sequence[str],
) -> SegmentationProposal:
    """프레임 경계들을 조각으로 — 빈틈·겹침 없는 전체 비디오 덮음을 못 박는다."""
    video = timeline.video
    frames = sorted({0, *cut_frames, video.frame_count})
    rate = stage1.PCM_SAMPLE_RATE
    segments: list[SegmentProposal] = []
    pairs = list(zip(frames[:-1], frames[1:], strict=True))
    for ordinal, (start_frame, end_frame) in enumerate(pairs):
        start = start_frame * video.frame_duration
        end = end_frame * video.frame_duration
        windows = [
            window for window in plan.windows if window.core_start < end and window.core_end > start
        ]
        spoken_here = [
            item
            for item in spoken
            if (item.start < end and item.end > start and item.text.strip())
            or (item.start == item.end and start <= item.start < end and item.text.strip())
        ]
        failed_here = [
            outcome
            for outcome in asr_failures
            if audio_start + Fraction(outcome.sample_start, rate) < end
            and audio_start + Fraction(outcome.sample_end, rate) > start
        ]
        status = (
            stage1.SPEECH_ANALYSIS_FAILED
            if failed_here
            else stage1.SPEECH_DETECTED
            if spoken_here
            else stage1.SPEECH_UNCONFIRMED_EMPTY
        )
        review = [
            reason
            for reason in reasons
            if any(reason.startswith(f"{window.window_id}:") for window in windows)
        ]
        segments.append(
            _lattice_segment(
                ordinal, start_frame, end_frame, start, end, status, windows, spoken_here, review
            )
        )
    proposal = SegmentationProposal(
        extent=video.extent,
        tick_grid=stage1.TICK_GRID_VIDEO,
        segments=tuple(segments),
        dropped_boundary_ids=(),
        review_reasons=(),
    )
    _require_video_coverage(proposal, video)
    return proposal


def _lattice_segment(
    ordinal: int,
    start_frame: int,
    end_frame: int,
    start: Fraction,
    end: Fraction,
    status: str,
    windows: Sequence[Any],
    spoken_here: Sequence[UtteranceEvidence],
    review: Sequence[str],
) -> SegmentProposal:
    return SegmentProposal(
        segment_id=f"segment_{start_frame:09d}",
        ordinal=ordinal,
        start=start,
        end=end,
        start_tick=start_frame,
        end_tick=end_frame,
        speech_status=status,
        needs_review=bool(review) or status != stage1.SPEECH_DETECTED,
        basis=tuple(
            [f"frame:{start_frame}", *(window.window_id for window in windows)]
            + [item.utterance_id for item in spoken_here]
        ),
        review_reasons=tuple(review),
    )


def _require_video_coverage(proposal: SegmentationProposal, video: VideoGrid) -> None:
    """조각 불변 — 순서·빈틈·겹침 없음·전체 비디오 덮음(§7.6 6단계)."""
    if not proposal.segments:
        raise AnalysisError("조각 제안이 비었다 — 전체 보존 계약 위반")
    cursor = Fraction(0)
    for position, segment in enumerate(proposal.segments):
        if segment.start != cursor:
            raise AnalysisError(
                f"조각 {position}이 범위를 어긴다: 시작 {segment.start}, 기대 {cursor}"
            )
        if segment.end <= segment.start:
            raise AnalysisError(f"조각 {position}이 비거나 뒤집혔다")
        cursor = segment.end
    if cursor != video.extent:
        raise AnalysisError(
            f"조각이 비디오 전체를 덮지 않는다: {cursor}/{video.extent}초 — 보존 계약 위반"
        )


# ─────────────────────────────────────────────────────────────
#  작업 본문 — 원본별 순차 분석, 재개는 캐시가 이어 붙인다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AnalysisProviders:
    """작업이 쓸 호출 경계들 — 재생 묶음이거나 단계별 승인된 실제 호출 재료다.

    실제 호출은 두 단계로만 열린다(A4-F1): `phase="asr"`은 1단계 전사만
    (`asr_approvals`가 문을 지킨다 — Luna는 한 번도 가지 않는다), `phase="semantic"`
    은 2단계 의미 판정만 (`luna_approvals`가 1단계 기록의 실측 지문을 검증한다 —
    전사를 다시 부르지 않는다). `phase="full"`은 재생의 단일 패스다.
    """

    origin: str
    phase: str = "full"
    replay: ReplayFixtures | None = None
    live_sender: object | None = None
    live_cfg: object | None = None
    live_client: object | None = None
    asr_approvals: Mapping[str, AsrPhaseApproval] | None = None
    luna_approvals: Mapping[str, object] | None = None
    on: date_type | None = None

    def is_live(self) -> bool:
        return self.origin == luna.ORIGIN_LIVE


def run_analysis(
    context: JobContext,
    workspace: RawWorkspace,
    revision: AnalysisRevision,
    order: GlobalOrderRevision,
    assets: Mapping[str, SourceAsset],
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
) -> None:
    """분석 작업 본문 — 확정 전체 순서를 하나씩, 선택 밖 원본은 보존만 한다.

    원본 하나의 실패는 그 원본의 기록으로 남고 다음 원본을 멈추지 않는다(§7.3).
    같은 지문의 성공 결과는 캐시로 이어 붙인다 — 재개는 완료 원본을 다시 부르지
    않는다(§14.2). 취소 게이트는 원본 사이에 선다(§14.1). 1단계(`phase="asr"`)는
    전사가 끝난 원본이 하나라도 있으면 리비전을 `awaiting_semantic`으로 멈춘다 —
    두 번째 사람 승인 전에 Luna는 가지 않는다(A4-F1).
    """
    store = AnalysisStore(workspace)
    ordered = sorted(order.entries, key=lambda item: item.ordinal)
    context.set_total(len(ordered))
    context.set_progress(0)
    store.update_revision(revision.revision_id, status="analyzing", error=None)
    done = 0
    try:
        for entry in ordered:
            context.check_cancel()
            asset = assets.get(entry.asset_id)
            if asset is None:
                _record_unanalyzed(store, revision, workspace, entry.asset_id, entry.ordinal)
                context.advance()
                continue
            _analyze_one(
                context,
                workspace,
                store,
                revision,
                entry.ordinal,
                asset,
                settings,
                workflow,
                providers,
            )
            done += 1
            store.update_revision(revision.revision_id, source_done=done)
            context.advance()
    except JobCancelledError:
        store.update_revision(revision.revision_id, status="interrupted")
        raise
    except Exception as error:  # noqa: BLE001 - 실행 전체를 멈추는 실패는 기록하고 올린다
        store.update_revision(revision.revision_id, status="failed", error=str(error)[:480])
        raise
    rows = [
        row for row in store.list_source_results(revision.revision_id) if row.asset_id in assets
    ]
    good = sum(row.status in ("analyzed", "cached") for row in rows)
    bad = any(row.status not in ("analyzed", "cached") for row in rows)
    final = (
        "complete"
        if not bad and len(rows) == len(assets)
        else ("partial" if good or any(row.status == "partial" for row in rows) else "failed")
    )
    if providers.phase == "asr":
        latest = store.list_source_results(revision.revision_id)
        if any(row.status == "transcribed" for row in latest):
            final = "awaiting_semantic"
    store.update_revision(
        revision.revision_id,
        status=final,
        error="일부 분석 구간을 확인하고 재시도하세요" if final in ("partial", "failed") else None,
    )


def _record_unanalyzed(
    store: AnalysisStore,
    revision: AnalysisRevision,
    workspace: RawWorkspace,
    asset_id: str,
    source_order: int,
) -> None:
    """선택에 들지 않은 원본 — 분석 없이 보존만 표시한다(순서는 항상 전부 산다)."""
    _upsert(
        store,
        revision,
        asset_id=asset_id,
        source_order=source_order,
        source_name=workspace.source_asset(asset_id).source_name,
        fingerprint="-",
        status="unanalyzed",
    )


def _complete_source_current(
    workspace: RawWorkspace, asset: SourceAsset, result: AnalysisSourceResult | None
) -> bool:
    if result is None or result.status not in ("analyzed", "cached") or not result.document_path:
        return False
    try:
        document = json.loads((workspace.root / result.document_path).read_text(encoding="utf-8"))
        _require_source_unchanged(workspace.source_media_path(asset), asset)
        return document.get("media_fingerprint", "original-only/v1") == media_refs.fingerprint(
            workspace, asset.asset_id
        ) and document.get(
            "transcript_fingerprint", "asr-original/v1"
        ) == source_transcript.fingerprint(workspace, asset.asset_id)
    except (OSError, ValueError, RawWorkspaceError):
        return False


def _phase_reusable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        artifact = _read_asr_phase_artifact(path)
        if any(c.get("failure") for c in artifact.get("asr", {}).get("chunks", [])):
            return False
        frames = _frames_from_artifact(artifact, path.parent / "frames")
        return not frames.failures
    except (AnalysisError, OSError, ValueError):
        return False


def _analyze_one(
    context: JobContext,
    workspace: RawWorkspace,
    store: AnalysisStore,
    revision: AnalysisRevision,
    source_order: int,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
) -> AnalysisSourceResult:
    """원본 하나의 분석 — 실패는 이 원본의 기록이지 전체 실패가 아니다."""
    existing = store.source_result(revision.revision_id, asset.asset_id)
    if (
        providers.phase == "semantic"
        and existing is not None
        and existing.status == "failed"
        and not existing.document_path
    ):
        return existing  # retain the actual preparation error while healthy sources continue
    if _complete_source_current(workspace, asset, existing):
        assert existing is not None
        return store.upsert_source_result(
            replace(
                existing,
                status="cached",
                cached_from=existing.cached_from or existing.revision_id,
                updated_at=_now(),
            )
        )
    _upsert(
        store,
        revision,
        asset_id=asset.asset_id,
        source_order=source_order,
        source_name=asset.source_name,
        fingerprint="-",
        status="analyzing",
    )
    try:
        return _analyze_source(
            context, workspace, store, revision, source_order, asset, settings, workflow, providers
        )
    except JobCancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 원본 단위 실패는 기록하고 다음으로
        note = f"{type(error).__name__}: {error}"[:480]
        log.warn(f"의미 분석 원본 실패 {asset.source_name}: {note}")
        # 1단계 기록 포인터는 실패로 지우지 않는다 — 2단계 재개가 그 기록에서
        # 다시 시작한다(전사를 다시 부르지 않는다, A4-F1).
        existing = store.source_result(revision.revision_id, asset.asset_id)
        return _upsert(
            store,
            revision,
            asset_id=asset.asset_id,
            source_order=source_order,
            source_name=asset.source_name,
            fingerprint=existing.fingerprint if existing is not None else "-",
            status="failed",
            document_path=existing.document_path if existing is not None else None,
            window_count=existing.window_count if existing is not None else 0,
            error=note,
        )


def _analyze_source(
    context: JobContext,
    workspace: RawWorkspace,
    store: AnalysisStore,
    revision: AnalysisRevision,
    source_order: int,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
) -> AnalysisSourceResult:
    """원본 하나의 분석 본체 — 조사→증거→판정→조정→문서까지."""
    path = workspace.source_media_path(asset)
    _require_source_unchanged(path, asset)
    probe = preprocess.probe_raw_media(path)
    clock = preprocess.probe_extraction_clock(path, probe=probe)
    if not clock.uniform_grid:
        raise AnalysisError(
            f"원본이 균일 격자가 아니다(VFR) — 분석 지원 밖이다: {asset.source_name}"
            f" ({clock.grid_note})"
        )
    grid = VideoGrid(
        stream_index=clock.stream_index,
        presentation_origin=clock.presentation_origin,
        frame_duration=clock.frame_duration,
        frame_count=clock.frame_count,
    )
    source_dir = (
        workspace.root
        / ANALYSIS_ROOT
        / revision.revision_id
        / _source_folder(source_order, asset.asset_id)
    )
    source_dir.mkdir(parents=True, exist_ok=True)
    if providers.phase == "semantic":
        # 2단계는 1단계가 남긴 PCM·전사를 재검증해서 쓴다 — 다시 뽑지 않는다.
        return _semantic_phase_source(
            context,
            workspace,
            store,
            revision,
            source_order,
            asset,
            settings,
            workflow,
            providers,
            path=path,
            grid=grid,
            source_dir=source_dir,
        )
    audio = None
    if probe.audio:
        audio = stage1.select_audio(path, source_dir, source_id=asset.asset_id, audio_ordinal=0)
    timeline = common_timeline(source_id=asset.asset_id, video=grid, audio=audio)
    input_fp = source_input_fingerprint(
        source_sha256=asset.content_sha256 or "",
        video=grid,
        audio=audio,
        settings=settings,
        workflow=workflow,
        media_fingerprint=media_refs.fingerprint(workspace, asset.asset_id),
        transcript_fingerprint=source_transcript.fingerprint(workspace, asset.asset_id),
    )
    cached = _reusable_cache(store, workspace, input_fp, asset.content_sha256 or "")
    if cached is not None:
        log.out(f"의미 분석 캐시 재사용: {asset.source_name} ({input_fp[:12]}…)")
        return _upsert(
            store,
            revision,
            asset_id=asset.asset_id,
            source_order=source_order,
            source_name=asset.source_name,
            fingerprint=input_fp,
            status="cached",
            speech_status=cached.speech_status,
            execution_mode=cached.execution_mode,
            document_path=cached.document_path,
            record_path=cached.record_path,
            window_count=cached.window_count,
            segment_count=cached.segment_count,
            cached_from=cached.cached_from or cached.revision_id,
        )

    if providers.phase == "asr":
        return _asr_phase_source(
            context,
            workspace,
            store,
            revision,
            source_order,
            asset,
            settings,
            workflow,
            providers,
            path=path,
            grid=grid,
            source_dir=source_dir,
            audio=audio,
            timeline=timeline,
            input_fp=input_fp,
        )
    if providers.phase == "semantic":
        raise AnalysisError(
            "2단계(의미) 실행은 1단계 기록에서 시작한다 — 전사 단계부터 다시 걸어야 한다"
        )
    if providers.is_live():
        raise AnalysisApprovalError(
            "승인 없는 실제 호출 전체 실행은 없다 — 1단계(전사)와 2단계(의미) 승인을"
            " 각각 받아라(A4-F1)"
        )
    asr_evidence: AsrEvidence | None = None
    utterances: tuple[UtteranceEvidence, ...] = ()
    if audio is not None:
        context.check_cancel()
        asr_provider = _asr_provider_for(providers, workflow, settings, audio, asset.asset_id)
        spans = stage1.plan_audio_chunks(audio, settings)
        asr_evidence = stage1.transcribe_selected_audio(
            audio,
            spans,
            settings=workflow,
            understanding=settings,
            provider=asr_provider,
            on=providers.on or datetime.now(UTC).date(),
            request_id=f"{revision.revision_id}:{asset.asset_id}",
            workdir=source_dir,
            cache_dir=workspace.root / "cache" / "source-asr" / asset.asset_id,
        )
        utterances = stage1.utterance_evidence(audio, asr_evidence)
    common_spoken = utterances_in_common_clock(timeline, utterances)
    source_transcript.seed(
        workspace,
        asset.asset_id,
        stage1.utterances_json(common_spoken),
        timeline.extent,
        asset.content_sha256 or "",
    )
    edited = source_transcript.spoken(workspace, asset.asset_id, common_spoken)
    offset = timeline.audio_start or Fraction(0)
    utterances = tuple(replace(u, start=u.start - offset, end=u.end - offset) for u in edited)
    plan = _scene_plan(workspace, asset, plan_common_windows(timeline, utterances, settings))
    spoken = utterances_in_common_clock(timeline, utterances)
    context.check_cancel()
    frames = stage1.sample_frames(
        plan,
        directory=source_dir / "frames",
        settings=settings,
        sampler=media_refs.frame_sampler(workspace, asset),
    )
    return _finish_source(
        context,
        workspace,
        store,
        revision,
        source_order,
        asset,
        settings,
        workflow,
        providers,
        path=path,
        grid=grid,
        timeline=timeline,
        audio=audio,
        asr_evidence=asr_evidence,
        plan=plan,
        spoken=spoken,
        frames=frames,
        input_fp=input_fp,
        source_dir=source_dir,
    )


def _asr_phase_source(
    context: JobContext,
    workspace: RawWorkspace,
    store: AnalysisStore,
    revision: AnalysisRevision,
    source_order: int,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
    *,
    path: Path,
    grid: VideoGrid,
    source_dir: Path,
    audio: SelectedAudio | None,
    timeline: CommonFileTimeline,
    input_fp: str,
) -> AnalysisSourceResult:
    """1단계(내부 ASR) — 전사·창·프레임을 남기고 **의미 승인 앞에서 멈춘다**.

    Luna는 한 번도 가지 않는다. 무성 원본은 전송이 없는 그대로 창·프레임만
    남긴다(ASR 작업을 지어내지 않는다, §7.3). 같은 지문의 1단계 기록이 이미
    있으면 다시 부르지 않는다 — 재시작·재개는 전사를 재사용한다(A4-F1).
    """
    artifact_path = source_dir / ASR_PHASE_DOCUMENT_NAME
    if _phase_reusable(artifact_path):
        artifact = _read_asr_phase_artifact(artifact_path)
        if str(artifact.get("input_fingerprint")) != input_fp:
            raise AnalysisError(
                f"1단계 기록이 지금의 입력과 다르다: {asset.source_name} — 새 분석"
                " 리비전으로 1단계부터 다시 받아라"
            )
        log.out(f"1단계 전사 기록 재사용: {asset.source_name} (ASR 재호출 없음)")
        return _upsert(
            store,
            revision,
            asset_id=asset.asset_id,
            source_order=source_order,
            source_name=asset.source_name,
            fingerprint=input_fp,
            status="transcribed",
            document_path=str(artifact_path.relative_to(workspace.root)),
            window_count=int(artifact.get("window_count") or 0),
        )
    asr_evidence: AsrEvidence | None = None
    utterances: tuple[UtteranceEvidence, ...] = ()
    if audio is not None:
        context.check_cancel()
        asr_provider = _asr_provider_for(providers, workflow, settings, audio, asset.asset_id)
        spans = stage1.plan_audio_chunks(audio, settings)
        asr_evidence = stage1.transcribe_selected_audio(
            audio,
            spans,
            settings=workflow,
            understanding=settings,
            provider=asr_provider,
            on=providers.on or datetime.now(UTC).date(),
            request_id=f"{revision.revision_id}:{asset.asset_id}",
            workdir=source_dir,
            cache_dir=workspace.root / "cache" / "source-asr" / asset.asset_id,
        )
        utterances = stage1.utterance_evidence(audio, asr_evidence)
    common_spoken = utterances_in_common_clock(timeline, utterances)
    source_transcript.seed(
        workspace,
        asset.asset_id,
        stage1.utterances_json(common_spoken),
        timeline.extent,
        asset.content_sha256 or "",
    )
    edited = source_transcript.spoken(workspace, asset.asset_id, common_spoken)
    offset = timeline.audio_start or Fraction(0)
    utterances = tuple(replace(u, start=u.start - offset, end=u.end - offset) for u in edited)
    plan = _scene_plan(workspace, asset, plan_common_windows(timeline, utterances, settings))
    context.check_cancel()
    frames = stage1.sample_frames(
        plan,
        directory=source_dir / "frames",
        settings=settings,
        sampler=media_refs.frame_sampler(workspace, asset),
    )
    semantic_fp = _semantic_fingerprint(asset, grid, audio, plan, settings, workflow)
    _write_json_atomic(
        artifact_path,
        {
            "schema": ASR_PHASE_SCHEMA,
            "analysis_revision_id": revision.revision_id,
            "asset_id": asset.asset_id,
            "source_order": source_order,
            "source_name": asset.source_name,
            "source_sha256": asset.content_sha256 or "",
            "input_fingerprint": input_fp,
            "transcript_fingerprint": source_transcript.fingerprint(workspace, asset.asset_id),
            "mode": providers.origin,
            "phase_kind": "semantic" if audio is not None else "visual",
            "video": {
                "stream_index": grid.stream_index,
                "presentation_origin": _fraction_json(grid.presentation_origin),
                "frame_duration": _fraction_json(grid.frame_duration),
                "frame_count": grid.frame_count,
            },
            "audio": (
                {"status": stage1.SPEECH_AUDIO_ABSENT}
                if audio is None
                else stage1.selected_audio_json(audio)
            ),
            "asr": (
                {"status": stage1.SPEECH_AUDIO_ABSENT, "request_id": None, "chunks": []}
                if asr_evidence is None
                else stage1.asr_evidence_json(asr_evidence)
            ),
            "utterances": stage1.utterances_json(utterances),
            "windows": stage1.window_plan_json(plan),
            "frames": stage1.frame_sampling_json(frames),
            "semantic_fingerprint": semantic_fp,
            "window_count": len(plan.windows),
            "image_count": len(frames.frames),
            "created_at": _now(),
        },
    )
    return _upsert(
        store,
        revision,
        asset_id=asset.asset_id,
        source_order=source_order,
        source_name=asset.source_name,
        fingerprint=input_fp,
        status="transcribed",
        document_path=str(artifact_path.relative_to(workspace.root)),
        window_count=len(plan.windows),
    )


def _semantic_phase_source(
    context: JobContext,
    workspace: RawWorkspace,
    store: AnalysisStore,
    revision: AnalysisRevision,
    source_order: int,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
    *,
    path: Path,
    grid: VideoGrid,
    source_dir: Path,
) -> AnalysisSourceResult:
    """2단계(의미 Luna) — 1단계 불변 기록을 재검증해서 그 위에서만 판정한다.

    전사를 다시 부르지 않는다(ASR 0송신). 1단계 기록의 입력 지문·실측 의미
    지문을 지금의 증거에서 다시 계산해 같은지 보고, 어긋나면 닫는다 — 낡은
    승인으로 바뀐 증거를 판정하게 하는 길은 없다(A4-F1).
    """
    artifact_path = source_dir / ASR_PHASE_DOCUMENT_NAME
    if not artifact_path.is_file():
        raise AnalysisError(
            f"1단계 전사 기록이 없다: {asset.source_name} — 먼저 1단계(전사) 실제"
            " 호출을 마쳐야 한다"
        )
    artifact = _read_asr_phase_artifact(artifact_path)
    if (
        str(artifact.get("analysis_revision_id")) != revision.revision_id
        or str(artifact.get("asset_id")) != asset.asset_id
    ):
        raise AnalysisError("1단계 기록의 신원이 이 리비전·원본과 다르다 — 새 1단계를 받아라")
    if str(artifact.get("source_sha256")) != (asset.content_sha256 or ""):
        raise AnalysisError(
            f"1단계 기록이 본 원본과 지금의 원본이 다르다: {asset.source_name} — 다시 가져오세요"
        )
    _require_artifact_grid(artifact, grid)
    if artifact.get("transcript_fingerprint", "asr-original/v1") != source_transcript.fingerprint(
        workspace, asset.asset_id
    ):
        raise AnalysisError("원본 자막이 바뀌었습니다. 갱신 범위를 다시 승인하세요")
    audio = _audio_from_artifact(artifact, source_dir, path, asset)
    utterances = _utterances_from_artifact(artifact)
    timeline = common_timeline(source_id=asset.asset_id, video=grid, audio=audio)
    input_fp = source_input_fingerprint(
        source_sha256=asset.content_sha256 or "",
        video=grid,
        audio=audio,
        settings=settings,
        workflow=workflow,
        media_fingerprint=media_refs.fingerprint(workspace, asset.asset_id),
        transcript_fingerprint=source_transcript.fingerprint(workspace, asset.asset_id),
    )
    if input_fp != str(artifact.get("input_fingerprint")):
        raise AnalysisError(
            f"설정·원본이 1단계 기록과 다르다: {asset.source_name} — 새 1단계 승인을 받아라"
        )
    cached = _reusable_cache(store, workspace, input_fp, asset.content_sha256 or "")
    if cached is not None:
        log.out(f"의미 분석 캐시 재사용(2단계): {asset.source_name} ({input_fp[:12]}…)")
        return _upsert(
            store,
            revision,
            asset_id=asset.asset_id,
            source_order=source_order,
            source_name=asset.source_name,
            fingerprint=input_fp,
            status="cached",
            speech_status=cached.speech_status,
            execution_mode=cached.execution_mode,
            document_path=cached.document_path,
            record_path=cached.record_path,
            window_count=cached.window_count,
            segment_count=cached.segment_count,
            cached_from=cached.cached_from or cached.revision_id,
        )
    common_spoken = utterances_in_common_clock(timeline, utterances)
    source_transcript.seed(
        workspace,
        asset.asset_id,
        stage1.utterances_json(common_spoken),
        timeline.extent,
        asset.content_sha256 or "",
    )
    edited = source_transcript.spoken(workspace, asset.asset_id, common_spoken)
    offset = timeline.audio_start or Fraction(0)
    utterances = tuple(replace(u, start=u.start - offset, end=u.end - offset) for u in edited)
    plan = _scene_plan(workspace, asset, plan_common_windows(timeline, utterances, settings))
    spoken = utterances_in_common_clock(timeline, utterances)
    frames = _frames_from_artifact(artifact, source_dir / "frames")
    semantic_fp = _semantic_fingerprint(asset, grid, audio, plan, settings, workflow)
    if semantic_fp != str(artifact.get("semantic_fingerprint")):
        raise AnalysisError(
            f"1단계 기록의 실측 지문과 지금의 증거가 다르다: {asset.source_name} — 1단계를"
            " 다시 실행하라"
        )
    asr_evidence = _asr_from_artifact(artifact) if audio is not None else None
    context.check_cancel()
    return _finish_source(
        context,
        workspace,
        store,
        revision,
        source_order,
        asset,
        settings,
        workflow,
        providers,
        path=path,
        grid=grid,
        timeline=timeline,
        audio=audio,
        asr_evidence=asr_evidence,
        plan=plan,
        spoken=spoken,
        frames=frames,
        input_fp=input_fp,
        source_dir=source_dir,
    )


def _finish_source(
    context: JobContext,
    workspace: RawWorkspace,
    store: AnalysisStore,
    revision: AnalysisRevision,
    source_order: int,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
    *,
    path: Path,
    grid: VideoGrid,
    timeline: CommonFileTimeline,
    audio: SelectedAudio | None,
    asr_evidence: AsrEvidence | None,
    plan: WindowPlan,
    spoken: Sequence[UtteranceEvidence],
    frames: FrameSamplingResult,
    input_fp: str,
    source_dir: Path,
) -> AnalysisSourceResult:
    """판정→조정→문서 기록까지의 공통 꼬리 — 재생 전체·2단계가 같은 길을 쓴다."""
    evidence_windows = luna.window_evidence(plan, frames, spoken)

    def publish_progress(rows: Sequence[Any]) -> None:
        context.check_cancel()
        for annotation in rows:
            if annotation.status == "accepted":
                _write_json_atomic(
                    cache_dir / (annotation.request_sha256 + ".json"),
                    luna.annotation_json(annotation),
                )
        _write_json_atomic(
            source_dir / "suitability_progress.json",
            {
                "analysis_revision_id": revision.revision_id,
                "source_id": asset.asset_id,
                "windows": stage1.window_plan_json(plan),
                "utterances": stage1.utterances_json(spoken),
                "recommendation_boundaries": _recommendation_boundaries(plan, spoken),
                "annotations": [luna.annotation_json(row) for row in rows],
            },
        )

    cache_settings = hashlib.sha256(
        json.dumps(
            {
                "settings": asdict(settings),
                "models": asdict(workflow.models),
                "media": media_refs.fingerprint(workspace, asset.asset_id),
                "source": asset.content_sha256,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cache_dir = workspace.root / "cache" / "source-luna" / asset.asset_id / cache_settings
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_annotations = {}
    for evidence in evidence_windows:
        digest = luna.luna_request_sha256(
            workflow.models.luna_model, luna.build_luna_payload(evidence, settings)
        )
        candidate = cache_dir / (digest + ".json")
        if candidate.is_file():
            try:
                annotation = SemanticAnnotation(**json.loads(candidate.read_text(encoding="utf-8")))
                cached_annotations[annotation.window_id] = annotation
            except (ValueError, TypeError, OSError):
                pass
    progress_path = source_dir / "suitability_progress.json"
    if progress_path.is_file():
        try:
            for item in json.loads(progress_path.read_text(encoding="utf-8")).get(
                "annotations", []
            ):
                row = SemanticAnnotation(**item)
                if row.status == "accepted":
                    cached_annotations[row.window_id] = row
        except (ValueError, TypeError, OSError):
            pass
    annotations = luna.annotate_windows(
        evidence_windows,
        _luna_responder_for(
            providers, workflow, settings, audio, grid, plan, spoken, frames, asset
        ),
        settings=settings,
        model=workflow.models.luna_model,
        origin=providers.origin,
        on_annotations=publish_progress,
        cached=cached_annotations,
    )
    references = media_refs.load(workspace, asset.asset_id)
    if references and references["validation"]["status"] in ("verified", "proxy_only"):
        annotations = tuple(
            replace(
                a,
                needs_review=True,
                uncertainties=(*a.uncertainties, "프록시 화질 판단은 원본 확인 필요"),
            )
            if any(
                term in a.suitability_reason for term in ("초점", "선명", "흐릿", "focus", "sharp")
            )
            else a
            for a in annotations
        )
    verdicts = luna.verdicts_from_annotations(annotations)
    verdicts = {
        key: replace(
            verdict,
            suggested_boundary_ids=tuple(
                identifier
                for identifier in verdict.suggested_boundary_ids
                if (boundary := plan.boundary(identifier)) is not None
                and boundary.kind != "window_length"
            ),
        )
        for key, verdict in verdicts.items()
    }
    reconciliation = reconcile_to_video_lattice(
        timeline,
        plan,
        verdicts,
        spoken,
        [chunk for chunk in asr_evidence.chunks if not chunk.succeeded] if asr_evidence else (),
    )
    if audio is None:
        reconciliation = replace(
            reconciliation,
            proposal=replace(
                reconciliation.proposal,
                segments=tuple(
                    replace(segment, speech_status=stage1.SPEECH_AUDIO_ABSENT)
                    for segment in reconciliation.proposal.segments
                ),
            ),
        )
    execution_mode = document_execution_mode(annotations)
    document = _stage1_document(
        audio=audio,
        asr=asr_evidence,
        spoken=spoken,
        plan=plan,
        frames=frames,
        reconciliation=reconciliation,
        annotations=annotations,
        settings=settings,
        workflow=workflow,
        input_fingerprint=input_fp,
    )
    from vlogsrt.project.raw.auto_cleanup import decisions

    automatic = decisions(
        document,
        source_id=asset.asset_id,
        revision_id=revision.revision_id,
        pcm_path=audio.pcm_path if audio is not None else None,
        audio_start=timeline.audio_start or Fraction(0),
    )
    document["recommendation_boundaries"] = _recommendation_boundaries(plan, spoken)
    document["transcript_fingerprint"] = source_transcript.fingerprint(workspace, asset.asset_id)
    document["media_fingerprint"] = media_refs.fingerprint(workspace, asset.asset_id)
    document_path = source_dir / ANALYSIS_DOCUMENT_NAME
    stage1.write_stage1_document(document_path, document)
    record_path = source_dir / ANALYSIS_RECORD_NAME
    _write_json_atomic(
        record_path,
        {
            "analysis_revision_id": revision.revision_id,
            "asset_id": asset.asset_id,
            "source_order": source_order,
            "input_fingerprint": input_fp,
            "mode": providers.origin,
            "execution_mode": execution_mode,
            "timeline": {
                "video": {
                    "stream_index": grid.stream_index,
                    "presentation_origin": _fraction_json(grid.presentation_origin),
                    "frame_duration": _fraction_json(grid.frame_duration),
                    "frame_count": grid.frame_count,
                },
                "audio": (
                    {"status": stage1.SPEECH_AUDIO_ABSENT}
                    if audio is None
                    else {
                        "stream_index": audio.audio_stream_index,
                        "sample_count": audio.sample_count,
                        "pcm_sample0_file_time": _fraction_json(audio.pcm_sample0_file_time),
                        "common_start": _fraction_json(timeline.audio_start or Fraction(0)),
                    }
                ),
                "extent": _fraction_json(timeline.extent),
            },
            "intervals": [
                {"start": _fraction_json(interval.start), "end": _fraction_json(interval.end)}
                for interval in reconciliation.intervals
            ],
            "auto_decisions": [item.document() for item in automatic],
            "segments": [segment.segment_id for segment in reconciliation.proposal.segments],
            "dropped_boundary_ids": list(reconciliation.dropped_boundary_ids),
            "review_reasons": list(reconciliation.review_reasons),
            "created_at": _now(),
        },
    )
    return _upsert(
        store,
        revision,
        asset_id=asset.asset_id,
        source_order=source_order,
        source_name=asset.source_name,
        fingerprint=input_fp,
        status=(
            "analyzed"
            if all(a.status == "accepted" for a in annotations)
            and (asr_evidence is None or all(c.succeeded for c in asr_evidence.chunks))
            and not frames.failures
            else "partial"
            if any(a.status == "accepted" for a in annotations)
            else "failed"
        ),
        speech_status=_aggregate_speech_status(reconciliation.proposal, audio is None),
        execution_mode=execution_mode,
        document_path=str(document_path.relative_to(workspace.root)),
        record_path=str(record_path.relative_to(workspace.root)),
        window_count=len(plan.windows),
        segment_count=len(reconciliation.proposal.segments),
    )


def _asr_provider_for(
    providers: AnalysisProviders,
    workflow: RawWorkflowConfig,
    settings: RawUnderstandingSettings,
    audio: SelectedAudio,
    asset_id: str,
) -> TimingProvider:
    """원본 하나의 내부 ASR 경계 — 재생이거나 1단계 승인이 검증된 실제 호출이다."""
    if not providers.is_live():
        if providers.replay is None:
            raise AnalysisError("재생 픽스처가 없다 — 오프라인 재생 분석을 시작할 수 없다")
        return providers.replay.asr_for(audio.source_sha256)
    approval = (providers.asr_approvals or {}).get(asset_id)
    if approval is None:
        raise AnalysisApprovalError(f"1단계(전사) 승인이 없는 원본이다: {asset_id}")
    sender = providers.live_sender
    cfg = providers.live_cfg
    if sender is None or cfg is None:
        raise AnalysisApprovalError(
            "실제 호출에는 전송 경계(sender)와 엔진 설정(cfg)이 필요하다 — 사람이 공급한다"
        )
    spans = stage1.plan_audio_chunks(audio, settings)
    if len(spans) > approval.chunk_budget:
        raise AnalysisApprovalError(
            f"승인 예산의 ASR 청크 한도({approval.chunk_budget})를"
            f" 실행 청크 수({len(spans)})가 넘는다"
        )
    require_live_asr_approval(
        approval,
        workflow=workflow,
        understanding=settings,
        source_sha256=audio.source_sha256,
        on=providers.on or datetime.now(UTC).date(),
    )
    inner = TransportTimingProvider(sender=sender, cfg=cfg)  # type: ignore[arg-type]
    return ApprovedPhaseAsrProvider(
        inner=inner,
        workflow=workflow,
        understanding=settings,
        audio=audio,
        approval=approval,
        gate=CountingAsrGate(limit=approval.chunk_budget),
    )


def _luna_responder_for(
    providers: AnalysisProviders,
    workflow: RawWorkflowConfig,
    settings: RawUnderstandingSettings,
    audio: SelectedAudio | None,
    grid: VideoGrid,
    plan: WindowPlan,
    spoken: Sequence[UtteranceEvidence],
    frames: FrameSamplingResult,
    asset: SourceAsset,
) -> SemanticResponder:
    """Luna 판정 경계 — 재생이거나 라이브러리의 자체 검증 응답자다(우회 불가).

    실제 호출은 2단계 승인만 받는다: 오디오 원본은 `OnlineAnalysisApproval`
    (실측 post-ASR 지문), 무성 원본은 시각 승인 `OnlineVisualAnalysisApproval`
    — 없는 오디오를 지어내지 않는다(A4-F1).
    """
    if not providers.is_live():
        if providers.replay is None:
            raise AnalysisError("재생 픽스처가 없다 — 오프라인 재생 분석을 시작할 수 없다")
        return providers.replay.luna_responder()
    approval = (providers.luna_approvals or {}).get(asset.asset_id)
    if approval is None:
        raise AnalysisApprovalError(f"2단계(의미) 승인이 없는 원본이다: {asset.asset_id}")
    client = providers.live_client
    if client is None:
        raise AnalysisApprovalError(
            "실제 호출에는 Responses 클라이언트가 필요하다 — 사람이 공급한다"
        )
    if audio is not None:
        if not isinstance(approval, OnlineAnalysisApproval):
            raise AnalysisApprovalError(
                f"오디오 원본의 2단계 승인은 OnlineAnalysisApproval이어야 한다: {asset.source_name}"
            )
        return luna.validated_luna_responder(
            client=client,
            settings=workflow,
            understanding=settings,
            audio=audio,
            plan=plan,
            utterances=spoken,
            frames=frames,
            approval=approval,
        )
    if not isinstance(approval, OnlineVisualAnalysisApproval):
        raise AnalysisApprovalError(
            f"무성 원본의 2단계 승인은 OnlineVisualAnalysisApproval이어야 한다: {asset.source_name}"
        )
    return validated_visual_luna_responder(
        client=client,
        settings=workflow,
        understanding=settings,
        source_sha256=asset.content_sha256 or "",
        video=grid,
        plan=plan,
        frames=frames,
        approval=approval,
    )


# ─────────────────────────────────────────────────────────────
#  1단계 불변 기록 — 쓰기·읽기·재구성(2단계 재개의 근거)
# ─────────────────────────────────────────────────────────────


def _semantic_fingerprint(
    asset: SourceAsset,
    grid: VideoGrid,
    audio: SelectedAudio | None,
    plan: WindowPlan,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
) -> str:
    """2단계 승인이 묶는 실측 지문 — 오디오 원본과 무성 원본이 각자의 schema로."""
    if audio is not None:
        return luna.analysis_fingerprint(
            audio=audio,
            plan=plan,
            settings=settings,
            asr_model=workflow.models.timing_model,
            luna_model=workflow.models.luna_model,
        )
    return visual_analysis_fingerprint(
        source_sha256=asset.content_sha256 or "",
        video=grid,
        plan=plan,
        settings=settings,
        luna_model=workflow.models.luna_model,
    )


def semantic_job_fingerprint(revision_id: str) -> str:
    """2단계 작업의 지문 — 리비전 신원에서 결정적으로 나온다(재결합의 키)."""
    digest = hashlib.sha256(f"raw-analysis-semantic/v1\0{revision_id}".encode()).hexdigest()
    return digest


def read_asr_phase_binding(document_path: Path) -> dict[str, Any]:
    """1단계 기록에서 2단계 승인·견적이 볼 값만 꺼낸다(지문·창·이미지·단계).

    서비스의 빠른 실패(2단계 시작 문)가 쓴다 — 기록 전체를 믿고 실행하는
    자리가 아니라, 승인이 본 값이 기록과 같은지만 본다.
    """
    artifact = _read_asr_phase_artifact(document_path)
    return {
        "asset_id": str(artifact["asset_id"]),
        "phase_kind": str(artifact["phase_kind"]),
        "semantic_fingerprint": str(artifact["semantic_fingerprint"]),
        "window_count": int(artifact["window_count"]),
        "image_count": int(artifact["image_count"]),
    }


def _read_asr_phase_artifact(path: Path) -> dict[str, Any]:
    """1단계 기록 읽기 — 모양이 계약과 다르면 조용히 넘기지 않는다."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AnalysisError(f"1단계 기록을 읽지 못했다: {path.name} — {error}") from error
    if not isinstance(body, dict) or body.get("schema") != ASR_PHASE_SCHEMA:
        raise AnalysisError(f"1단계 기록이 {ASR_PHASE_SCHEMA} 모양이 아니다: {path.name}")
    required = (
        "analysis_revision_id",
        "asset_id",
        "source_sha256",
        "input_fingerprint",
        "phase_kind",
        "video",
        "semantic_fingerprint",
        "window_count",
        "image_count",
    )
    missing = [key for key in required if key not in body]
    if missing:
        raise AnalysisError(f"1단계 기록에 필수 칸이 비었다: {', '.join(missing)}")
    return body


def _fraction_from_json(value: object, where: str) -> Fraction:
    if not isinstance(value, Mapping) or "num" not in value or "den" not in value:
        raise AnalysisError(f"1단계 기록의 유리수 칸이 어긋났다: {where}")
    return Fraction(int(value["num"]), int(value["den"]))


def _require_artifact_grid(artifact: Mapping[str, object], grid: VideoGrid) -> None:
    """지금 증명한 비디오 격자가 1단계 기록과 같은지 — 다르면 2단계를 시작하지 않는다."""
    video = artifact.get("video")
    if not isinstance(video, Mapping):
        raise AnalysisError("1단계 기록에 비디오 격자가 없다")
    recorded = (
        int(video.get("stream_index", -1)),
        _fraction_from_json(video.get("presentation_origin"), "video.presentation_origin"),
        _fraction_from_json(video.get("frame_duration"), "video.frame_duration"),
        int(video.get("frame_count", -1)),
    )
    current = (
        grid.stream_index,
        grid.presentation_origin,
        grid.frame_duration,
        grid.frame_count,
    )
    if recorded != current:
        raise AnalysisError(
            "지금 증명한 비디오 격자가 1단계 기록과 다르다 — 원본이 바뀌었다, 1단계부터 다시 받아라"
        )


def _audio_from_artifact(
    artifact: Mapping[str, object], source_dir: Path, path: Path, asset: SourceAsset
) -> SelectedAudio | None:
    """1단계가 잰 선택 오디오를 재구성한다 — PCM 증거의 해시를 다시 잰다."""
    audio = artifact.get("audio")
    if not isinstance(audio, Mapping) or audio.get("status") == stage1.SPEECH_AUDIO_ABSENT:
        return None
    required = (
        "source_sha256",
        "audio_stream_index",
        "pcm_sha256",
        "sample_rate",
        "channels",
        "sample_count",
        "pcm_sample0_file_time",
    )
    missing = [key for key in required if key not in audio]
    if missing:
        raise AnalysisError(f"1단계 기록의 오디오 신원 칸이 비었다: {', '.join(missing)}")
    pcm_path = source_dir / "analysis.pcm"
    if not pcm_path.is_file():
        raise AnalysisError(
            "1단계가 남긴 PCM 증거가 없다 — 전사 기록을 다시 만들려면 1단계부터 다시 받아라"
        )
    pcm_sha = _file_sha256(pcm_path)
    if pcm_sha != str(audio["pcm_sha256"]):
        raise AnalysisError(
            "1단계 PCM 증거의 해시가 기록과 다르다 — 증거가 변했으니 1단계부터 다시 받아라"
        )
    return SelectedAudio(
        source_id=asset.asset_id,
        source_path=path,
        source_sha256=str(audio["source_sha256"]),
        audio_stream_index=int(audio["audio_stream_index"]),
        pcm_path=pcm_path,
        pcm_sha256=pcm_sha,
        sample_rate=int(audio["sample_rate"]),
        channels=int(audio["channels"]),
        sample_count=int(audio["sample_count"]),
        pcm_sample0_file_time=_fraction_from_json(
            audio["pcm_sample0_file_time"], "audio.pcm_sample0_file_time"
        ),
    )


def _utterances_from_artifact(artifact: Mapping[str, object]) -> tuple[UtteranceEvidence, ...]:
    """1단계 발화 증거를 재구성한다 — 틱·아이디·글자는 기록 그대로다."""
    raw = artifact.get("utterances")
    if not isinstance(raw, list):
        raise AnalysisError("1단계 기록에 발화 증거가 없다")
    utterances: list[UtteranceEvidence] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise AnalysisError("1단계 기록의 발화 항목이 객체가 아니다")
        utterances.append(
            UtteranceEvidence(
                utterance_id=str(item["utterance_id"]),
                chunk_index=int(item["chunk_index"]),
                position=int(item["position"]),
                text=str(item["text"]),
                start=_fraction_from_json(item.get("start"), "utterance.start"),
                end=_fraction_from_json(item.get("end"), "utterance.end"),
                start_sample=int(item["start_sample"]),
                end_sample=int(item["end_sample"]),
                evidence_id=str(item["evidence_id"]),
            )
        )
    return tuple(utterances)


def _asr_from_artifact(artifact: Mapping[str, object]) -> AsrEvidence:
    """1단계 전사 증거를 재구성한다 — 실패 청크도 그대로(빈 성공이 아니다)."""
    raw = artifact.get("asr")
    if not isinstance(raw, Mapping) or "chunks" not in raw or not isinstance(raw["chunks"], list):
        raise AnalysisError("1단계 기록에 전사 증거가 없다")
    chunks: list[AsrChunkOutcome] = []
    for item in raw["chunks"]:
        if not isinstance(item, Mapping):
            raise AnalysisError("1단계 기록의 청크 항목이 객체가 아니다")
        transcript = tuple(
            (float(seg["start"]), float(seg["end"]), str(seg["text"]))
            for seg in item.get("segments", [])
            if isinstance(seg, Mapping)
        )
        chunks.append(
            AsrChunkOutcome(
                chunk_index=int(item["chunk_index"]),
                sample_start=int(item["sample_start"]),
                sample_end=int(item["sample_end"]),
                wav_sha256=str(item["wav_sha256"]),
                request_id=str(item["request_id"]),
                transcript=transcript,
                failure=str(item.get("failure") or ""),
            )
        )
    return AsrEvidence(
        request_id=str(raw.get("request_id") or ""),
        sample_rate=int(raw.get("sample_rate") or stage1.PCM_SAMPLE_RATE),
        chunks=tuple(chunks),
    )


def _frames_from_artifact(artifact: Mapping[str, object], frames_dir: Path) -> FrameSamplingResult:
    """1단계가 뽑아 남긴 프레임 증거를 재구성한다 — 이미지 해시를 다시 잰다."""
    raw = artifact.get("frames")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("frames"), list):
        raise AnalysisError("1단계 기록에 프레임 표본이 없다")
    frames: list[stage1.FrameEvidence] = []
    failures: list[stage1.FrameExtractionFailure] = []
    for item in raw["frames"]:
        if not isinstance(item, Mapping):
            raise AnalysisError("1단계 기록의 프레임 항목이 객체가 아니다")
        image_path = frames_dir / f"{str(item['frame_id'])}.jpg"
        if not image_path.is_file():
            raise AnalysisError(
                f"1단계가 남긴 프레임 증거가 없다: {image_path.name} — 1단계부터 다시 받아라"
            )
        digest = _file_sha256(image_path)
        if digest != str(item["image_sha256"]):
            raise AnalysisError(f"프레임 증거의 해시가 1단계 기록과 다르다: {image_path.name}")
        frames.append(
            stage1.FrameEvidence(
                frame_id=str(item["frame_id"]),
                frame_index=int(item["frame_index"]),
                image_path=image_path,
                image_sha256=digest,
                file_time=_fraction_from_json(item.get("file_time"), "frame.file_time"),
            )
        )
    for item in raw.get("failures", []) if isinstance(raw.get("failures"), list) else []:
        if isinstance(item, Mapping):
            failures.append(
                stage1.FrameExtractionFailure(
                    frame_id=str(item["frame_id"]), error=str(item["error"])
                )
            )
    return FrameSamplingResult(tuple(frames), tuple(failures))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _stage1_document(
    *,
    audio: SelectedAudio | None,
    asr: AsrEvidence | None,
    spoken: Sequence[UtteranceEvidence],
    plan: WindowPlan,
    frames: FrameSamplingResult,
    reconciliation: SourceReconciliation,
    annotations: Sequence[SemanticAnnotation],
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    input_fingerprint: str,
) -> dict[str, object]:
    """첫 단계 문서 — 오디오 있음은 라이브러리 조립, 없음은 audio_absent 모양.

    오디오가 없으면 `AsrEvidence`가 애초에 존재할 수 없다(빈 PCM을 지어내지
    않는다) — 문서의 `audio`·`asr` 칸이 부재 상태를 그대로 말한다(§7.3).
    """
    if audio is not None and asr is not None:
        return luna.stage1_document(
            audio=audio,
            asr=asr,
            utterances=spoken,
            plan=plan,
            frames=frames,
            segmentation=reconciliation.proposal,
            annotations=annotations,
            settings=settings,
            workflow=workflow,
            fingerprint=input_fingerprint,
        )
    return {
        "stage": "understanding_stage1",
        "audio": {
            "status": stage1.SPEECH_AUDIO_ABSENT,
            "note": "오디오 트랙이 없다 — 무발화가 아니라 별개 상태다(§7.3)",
        },
        "asr": {"status": stage1.SPEECH_AUDIO_ABSENT, "request_id": None, "chunks": []},
        "utterances": [],
        "windows": stage1.window_plan_json(plan),
        "frames": stage1.frame_sampling_json(frames),
        "annotations": [annotation_json(item) for item in annotations],
        "segmentation": stage1.segmentation_json(reconciliation.proposal),
        "models": {"asr": None, "luna": workflow.models.luna_model},
        "prompt_revision": settings.prompt_revision,
        "fingerprint": input_fingerprint,
        "execution_mode": document_execution_mode(annotations),
    }


def _aggregate_speech_status(proposal: SegmentationProposal, audio_absent: bool) -> str:
    """원본 하나의 대표 상태 — 실패·감지·미확인·부재가 섞이면 가장 보수적인 것."""
    if audio_absent:
        return stage1.SPEECH_AUDIO_ABSENT
    statuses = {segment.speech_status for segment in proposal.segments}
    if stage1.SPEECH_ANALYSIS_FAILED in statuses:
        return stage1.SPEECH_ANALYSIS_FAILED
    if stage1.SPEECH_DETECTED in statuses:
        return stage1.SPEECH_DETECTED
    return stage1.SPEECH_UNCONFIRMED_EMPTY


def _reusable_cache(
    store: AnalysisStore, workspace: RawWorkspace, fingerprint: str, source_sha256: str
) -> AnalysisSourceResult | None:
    """같은 지문의 성공 캐시 후보 — 문서 존재와 출처 해시를 다시 검사한다(§14.2)."""
    found = store.cached_success(fingerprint)
    if found is None:
        return None
    result, _revision = found
    if not result.document_path or not result.record_path:
        return None
    document = workspace.root / result.document_path
    record = workspace.root / result.record_path
    if not document.is_file() or not record.is_file():
        return None
    try:
        body = json.loads(document.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    audio_section = body.get("audio") if isinstance(body, dict) else None
    if isinstance(audio_section, dict):
        recorded_sha = audio_section.get("source_sha256")
        recorded_status = audio_section.get("status")
    else:
        recorded_sha, recorded_status = None, None
    if isinstance(recorded_sha, str):
        if recorded_sha != source_sha256:
            return None
    elif recorded_status != stage1.SPEECH_AUDIO_ABSENT:
        return None
    return result


def _require_source_unchanged(path: Path, asset: SourceAsset) -> None:
    """원본 재검증 — 크기·SHA-256을 지금 다시 읽는다(§14.2)."""
    probed = path.stat()
    if probed.st_size != asset.size_bytes:
        raise AnalysisError(
            f"원본 크기가 가져오기 때와 다르다: {asset.source_relative_path}"
            f" ({asset.size_bytes} → {probed.st_size}B)"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if asset.content_sha256 is None or digest.hexdigest() != asset.content_sha256:
        raise AnalysisError(
            f"원본 내용이 가져오기 때와 다르다: {asset.source_relative_path} — 다시 가져오세요"
        )


def _source_folder(source_order: int, asset_id: str) -> str:
    """원본 하나의 증거 폴더 — 순번과 자산 ID로(전역 순서 그대로)."""
    return f"{source_order:06d}_{asset_id}"


def _upsert(
    store: AnalysisStore,
    revision: AnalysisRevision,
    *,
    asset_id: str,
    source_order: int,
    source_name: str,
    fingerprint: str,
    status: str,
    speech_status: str | None = None,
    execution_mode: str | None = None,
    document_path: str | None = None,
    record_path: str | None = None,
    window_count: int = 0,
    segment_count: int = 0,
    cached_from: str | None = None,
    error: str | None = None,
) -> AnalysisSourceResult:
    now = _now()
    return store.upsert_source_result(
        AnalysisSourceResult(
            revision_id=revision.revision_id,
            asset_id=asset_id,
            source_order=source_order,
            source_name=source_name,
            fingerprint=fingerprint,
            status=status,
            speech_status=speech_status,
            execution_mode=execution_mode,
            document_path=document_path,
            record_path=record_path,
            window_count=window_count,
            segment_count=segment_count,
            cached_from=cached_from,
            error=error,
            created_at=now,
            updated_at=now,
        )
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> Path:
    """JSON 문서를 원자적으로 쓴다 — 반쪽 파일이 목격되지 않게 `.partial`을 거친다."""
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    try:
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _fraction_json(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_revision_id() -> str:
    return new_analysis_revision_id()


__all__ = [
    "ANALYSIS_DOCUMENT_NAME",
    "ANALYSIS_RECORD_NAME",
    "ANALYSIS_ROOT",
    "ASR_PHASE_DOCUMENT_NAME",
    "ASR_PHASE_SCHEMA",
    "REPLAY_FIXTURES_DIRNAME",
    "AnalysisError",
    "AnalysisProviders",
    "ApprovedPhaseAsrProvider",
    "ApprovedTimingProvider",
    "AsrBudgetExhausted",
    "CountingAsrGate",
    "ReplayAsrProvider",
    "ReplayFixtures",
    "ReplayFixtureError",
    "SourceReconciliation",
    "INPUT_FINGERPRINT_SCHEMA",
    "load_replay_fixtures",
    "new_revision_id",
    "read_asr_phase_binding",
    "reconcile_to_video_lattice",
    "run_analysis",
    "semantic_job_fingerprint",
    "source_input_fingerprint",
]


def _scene_plan(workspace: RawWorkspace, asset: SourceAsset, plan: WindowPlan) -> WindowPlan:
    """Cache decoded cuts and attach only measured boundaries, retaining request image ceilings."""
    assert plan.video is not None
    digest = hashlib.sha256(
        (
            str(asset.content_sha256)
            + media_refs.fingerprint(workspace, asset.asset_id)
            + "scene/v1"
        ).encode()
    ).hexdigest()
    cache = workspace.root / "cache" / (digest + "-scenes.json")
    if cache.is_file():
        times = tuple(Fraction(t) for t in json.loads(cache.read_text(encoding="utf-8"))["times"])
    else:
        times = preprocess.verified_scene_times(media_refs.resolve(workspace, asset, "frames"))
        _write_json_atomic(cache, {"times": [str(t) for t in times]})
    candidates = list(plan.candidates)
    windows = []
    for window in plan.windows:
        owned = []
        frames = list(window.frame_plans)
        for moment in times:
            point = moment - plan.video.presentation_origin
            frame = round(point / plan.video.frame_duration)
            if window.core_start <= point < window.core_end and 0 < frame < plan.video.frame_count:
                owned.append(
                    BoundaryCandidate(
                        f"scene_{frame:08d}",
                        window.window_id,
                        "verified_scene",
                        frame * plan.video.frame_duration,
                        frame,
                    )
                )
                for index in (frame - 1, frame):
                    replacement = stage1.FramePlan(
                        f"frame_{index:08d}", index, index * plan.video.frame_duration
                    )
                    if frames and all(f.frame_index != index for f in frames):
                        nearest = min(
                            range(len(frames)), key=lambda i: abs(frames[i].frame_index - index)
                        )
                        frames[nearest] = replacement
        candidates.extend(owned)
        windows.append(
            replace(
                window,
                boundary_ids=window.boundary_ids + tuple(c.boundary_id for c in owned),
                frame_plans=tuple(sorted(frames, key=lambda f: f.frame_index)),
            )
        )
    return replace(plan, windows=tuple(windows), candidates=tuple(candidates))


def _recommendation_boundaries(plan: WindowPlan, spoken: Sequence[UtteranceEvidence]) -> list[dict]:
    points = {Fraction(0), plan.extent}
    points.update(c.position for c in plan.candidates if c.kind == "verified_scene")
    for utterance in spoken:
        points.update((max(Fraction(0), utterance.start), min(plan.extent, utterance.end)))
    return [_fraction_json(point) for point in sorted(points) if 0 <= point <= plan.extent]
