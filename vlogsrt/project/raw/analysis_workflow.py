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
from vlogsrt.project.raw import asr_epoch as asr_epoch_store
from vlogsrt.project.raw import (
    cross_check,
    glossary,
    media_refs,
    request_usage,
    source_text,
    source_transcript,
    source_verify,
)
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    require_live_asr_approval,
    require_text_approval,
)
from vlogsrt.project.raw.analysis_config import (
    ANALYSIS_PROXY_LIMITS,
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
    TransportTextProvider,
    TransportTimingProvider,
    parse_timing_response,
)
from vlogsrt.project.raw.config import SOURCE_TEXT_SETTINGS, RawWorkflowConfig
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

ASR_OUTCOME_DOCUMENT_NAME = "asr_outcome.json"
"""전사가 끝나자마자 남기는 기록(D59) — 오디오 신원과 청크마다의 결과(실패 청크 포함).

원본 자막은 전사 직후 심긴다. 그 뒤 장면 컷·프레임 단계가 죽으면(분석 프록시 시간 초과·앱
종료) 1단계 기록(`asr_phase.json`·`analysis_transcript.json`)은 남지 않는데 자막은 남는다 —
실측 0018: 자막에 1260–1359초가 비었는데 그것이 전사 실패라는 기록이 어디에도 없었다. 이 문서는
재사용·승인·지문 어디에도 읽히지 않는다(재사용 신호는 여전히 `asr_phase.json`이다).
"""

ASR_OUTCOME_SCHEMA = "raw-analysis-asr-outcome/v1"

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
    # 1단계 글자 경로(D59 19c)의 선언 — `text-fixtures.json`이 없으면 None(글자 경로는 묻지 않는다).
    # `text_chunks`는 WAV sha → 글자, `text_sources`는 원본 sha → {청크 번호: 글자}다.
    text_chunks: Mapping[str, str] | None = None
    text_sources: Mapping[str, Mapping[int, str]] = field(default_factory=dict)

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

    def text_for(self, source_sha256: str) -> source_text.ReplayTextProvider | None:
        """원본 하나의 글자 재생자 — 글자 픽스처 파일이 없으면 None(글자 경로는 묻지 않는다).

        파일이 있는데 그 청크의 선언이 없으면 그 청크는 글자 경로 실패로 남는다(지어내지 않는다).
        """
        if self.text_chunks is None:
            return None
        return source_text.ReplayTextProvider(
            exact=self.text_chunks, declared=self.text_sources.get(source_sha256)
        )


def load_replay_fixtures(directory: Path) -> ReplayFixtures:
    """픽스처 디렉터리를 읽는다 — `asr-fixtures.json`·`luna-fixtures.json`·`text-fixtures.json`.

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
    text_chunks, text_sources = _load_text_fixtures(directory / "text-fixtures.json")
    return ReplayFixtures(
        sources=sources,
        chunks=chunks,
        windows=windows,
        text_chunks=text_chunks,
        text_sources=text_sources,
    )


def _load_text_fixtures(
    path: Path,
) -> tuple[Mapping[str, str] | None, Mapping[str, Mapping[int, str]]]:
    """1단계 글자 경로 재생 선언(D59 19c) — `{"chunks": {WAV sha: 글자}, "sources": {원본 sha:
    {청크 번호: 글자}}}`. 파일이 없으면 (None, {}) — 재생 글자 경로는 묻지 않는다."""
    if not path.is_file():
        return None, {}
    payload = _read_json(path)
    chunks = {
        str(sha): _fixture_text(value, path)
        for sha, value in _mapping_field(payload, "chunks", path).items()
    }
    sources: dict[str, Mapping[int, str]] = {}
    for sha, declared in _mapping_field(payload, "sources", path).items():
        if not isinstance(declared, Mapping):
            raise ReplayFixtureError(f"글자 선언이 객체가 아니다: {path} ({sha})")
        sources[str(sha)] = {
            int(index): _fixture_text(text, path) for index, text in declared.items()
        }
    return chunks, sources


def _fixture_text(value: object, path: Path) -> str:
    if not isinstance(value, str):
        raise ReplayFixtureError(f"글자 선언은 문자열이어야 한다: {path} ({value!r})")
    return value


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
    media_fingerprint: str = media_refs.ORIGINAL_ONLY_FINGERPRINT,
    transcript_fingerprint: str = "asr-original/v1",
    asr_epoch: int = 0,
    cross_check_tag: str | None = None,
) -> str:
    """원본 하나의 분석 입력 지문(결과에 닿는 입력만) — 차례·교차 확인 꼬리표는 있을 때만."""
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
        _audio_identity_text(audio),
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
    return _input_digest(parts + _grid_conform_parts(video), asr_epoch, cross_check_tag)


def _grid_conform_parts(video: VideoGrid) -> list[str]:
    """map 원본(D70)의 프레임 표본은 맞춤 규칙이 고른 패킷이다 — 규칙 판을 묶는다(그 밖은 없음)."""
    return [preprocess.GRID_CONFORM_TAG] if video.slot_packets else []


def _current_input(
    workspace: RawWorkspace,
    asset: SourceAsset,
    grid: VideoGrid,
    audio: SelectedAudio | None,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
) -> tuple[str, int]:
    """원본의 지금 입력 지문과 다시 듣기 차례 — 차례는 한 번 읽어 지문과 청크 열쇠가 함께 본다.

    미디어 참조·원본 자막 지문도 지금 값이다(D59 19b — 1단계와 2단계가 같은 조립을 쓴다). 교차
    확인 꼬리표(D59 19d)는 소리가 있는 원본에만 붙는다 — 두 번째 귀에 물을 것이 없는 무성 원본의
    끝난 분석은 설정을 켜도 그대로 이어 쓴다."""
    epoch = asr_epoch_store.current(workspace, asset.asset_id)
    fingerprint = source_input_fingerprint(
        source_sha256=asset.content_sha256 or "",
        video=grid,
        audio=audio,
        settings=settings,
        workflow=workflow,
        media_fingerprint=media_refs.fingerprint(workspace, asset.asset_id),
        transcript_fingerprint=source_transcript.fingerprint(workspace, asset.asset_id),
        asr_epoch=epoch,
        cross_check_tag=_cross_check_tag(workspace, audio),
    )
    return fingerprint, epoch


def _cross_check_tag(workspace: RawWorkspace, audio: SelectedAudio | None) -> str | None:
    """원본 입력 지문의 교차 확인 꼬리표 — 꺼져 있거나 소리가 없으면 None(바이트 그대로)."""
    return None if audio is None else cross_check.binding_tag(workspace)


def _audio_identity_text(audio: SelectedAudio | None) -> str:
    """입력 지문의 오디오 칸 — 스트림·분량·원점(무성 원본은 `-`)."""
    if audio is None:
        return "-"
    return "|".join(
        [
            str(audio.audio_stream_index),
            f"{audio.sample_rate}/{audio.channels}/{audio.sample_count}",
            _fraction_text(audio.pcm_sample0_file_time),
        ]
    )


def _input_digest(parts: list[str], asr_epoch: int, cross_check_tag: str | None = None) -> str:
    """입력 지문의 해시 — 다시 듣기 차례(D59 19b)는 0이 아닐 때만 맨 끝에 붙는다.

    0이면 지문은 19b 이전 바이트 그대로이고, 차례가 오른 원본은 옛 분석 결과를 캐시로 다시
    쓰지 않는다. 교차 확인 꼬리표(D59 19d)도 있을 때만 그 뒤에 붙는다 — 두 번째 귀에 묻기 전에
    끝난 분석은 캐시로 다시 쓰지 않는다(그래야 1단계가 다시 돌아 글자를 묻는다)."""
    tag = asr_epoch_store.binding_tag(asr_epoch)
    tags = [item for item in (tag, cross_check_tag) if item is not None]
    body = "\0".join([*parts, *tags])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
        _require_source_unchanged(
            workspace, workspace.source_media_path(asset), asset, trust_stat=True
        )
        stored = document.get("media_fingerprint", media_refs.ORIGINAL_ONLY_FINGERPRINT)
        return stored == media_refs.fingerprint(workspace, asset.asset_id) and document.get(
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
    _require_source_unchanged(workspace, path, asset, trust_stat=False)
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
        slot_packets=clock.slot_packets,
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
    input_fp, epoch = _current_input(workspace, asset, grid, audio, settings, workflow)
    cached = _reusable_cache(store, workspace, input_fp, asset.content_sha256 or "")
    if cached is not None:
        return _upsert_cached(store, revision, source_order, asset, input_fp, cached, "")

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
            asr_epoch=epoch,
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
    asr_evidence, utterances, _text_record = _transcribe_source(
        context,
        workspace,
        revision,
        asset,
        settings,
        workflow,
        providers,
        audio=audio,
        grid=grid,
        source_dir=source_dir,
        asr_epoch=epoch,
    )
    utterances, input_fp = _seed_stage1(
        workspace, asset, timeline, utterances, settings, workflow, input_fp, epoch
    )
    plan = _scene_plan(workspace, asset, plan_common_windows(timeline, utterances, settings))
    spoken = utterances_in_common_clock(timeline, utterances)
    context.check_cancel()
    frames = stage1.sample_frames(
        plan,
        directory=source_dir / "frames",
        settings=settings,
        sampler=media_refs.frame_sampler(workspace, asset, slot_packets=grid.slot_packets),
    )
    frame_source = media_refs.frames_fingerprint(
        workspace, asset.asset_id, mapped=bool(grid.slot_packets)
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
        frame_source=frame_source,
    )


def _upsert_cached(
    store: AnalysisStore,
    revision: AnalysisRevision,
    source_order: int,
    asset: SourceAsset,
    input_fp: str,
    cached: AnalysisSourceResult,
    stage_label: str,
) -> AnalysisSourceResult:
    """같은 입력 지문의 끝난 분석을 이 리비전의 캐시 행으로 잇는다(1단계 앞·2단계 앞 공용)."""
    log.out(f"의미 분석 캐시 재사용{stage_label}: {asset.source_name} ({input_fp[:12]}…)")
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


def _seed_transcript(
    workspace: RawWorkspace,
    asset: SourceAsset,
    timeline: CommonFileTimeline,
    utterances: tuple[UtteranceEvidence, ...],
) -> tuple[UtteranceEvidence, ...]:
    """전사를 원본 자막으로 심고(고친 자막은 덮지 않는다) 지금 원본 자막을 PCM 시계로 돌려준다."""
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
    return tuple(replace(u, start=u.start - offset, end=u.end - offset) for u in edited)


def _seed_stage1(
    workspace: RawWorkspace,
    asset: SourceAsset,
    timeline: CommonFileTimeline,
    utterances: tuple[UtteranceEvidence, ...],
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    input_fp: str,
    asr_epoch: int,
) -> tuple[tuple[UtteranceEvidence, ...], str]:
    """1단계의 원본 자막 심기 — 글자 경로가 자막 지문을 바꿨으면 그 지문으로 입력 지문을 다시 잰다.

    두 번째 귀의 글자가 실리면 자막 지문이 발화 해시로 바뀐다(D59 19c). 입력 지문은 전사 **전에**
    쟀으므로 다시 재는 것은 자막 지문 하나다: 지금 값(미디어·차례·교차 확인 꼬리표)으로 **심기
    전 지문**을 다시 재어 처음 입력 지문이 그대로 나올 때만 새 자막 지문으로 바꾼다. 무엇이든
    움직였으면 처음 값 그대로 두어 2단계가 지금 값으로 다시 재 대조해 닫는다(19b와 같은 선)."""
    before = source_transcript.fingerprint(workspace, asset.asset_id)
    seeded = _seed_transcript(workspace, asset, timeline, utterances)
    after = source_transcript.fingerprint(workspace, asset.asset_id)
    if after == before:
        return seeded, input_fp
    media = media_refs.fingerprint(workspace, asset.asset_id)
    cross_tag = _cross_check_tag(workspace, timeline.audio)  # 지금 값 — 그사이 바뀌었으면 안 맞는다

    def measured(transcript: str) -> str:
        return source_input_fingerprint(
            source_sha256=asset.content_sha256 or "",
            video=timeline.video,
            audio=timeline.audio,
            settings=settings,
            workflow=workflow,
            media_fingerprint=media,
            transcript_fingerprint=transcript,
            asr_epoch=asr_epoch,
            cross_check_tag=cross_tag,
        )

    return seeded, measured(after) if measured(before) == input_fp else input_fp


def _transcribe_source(
    context: JobContext,
    workspace: RawWorkspace,
    revision: AnalysisRevision,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
    *,
    audio: SelectedAudio | None,
    grid: VideoGrid,
    source_dir: Path,
    asr_epoch: int = 0,
) -> tuple[AsrEvidence | None, tuple[UtteranceEvidence, ...], dict[str, object] | None]:
    """1단계 전사 — 끝나자마자 결과를 `asr_outcome.json`으로 남긴다(실패 청크 포함, D59).

    whisper 뒤에 같은 청크 WAV로 글자 경로(D59 19c)를 돌려 게이트를 통과한 글자만 얹는다 — 그
    기록은 `asr_text`로 따로 남는다(`asr.chunks`에는 들어가지 않는다). 무성 원본은 '오디오 없음'만
    남긴다. 기록은 자막을 심기 전에 쓴다(`_record_transcription`)."""
    asr_evidence: AsrEvidence | None = None
    utterances: tuple[UtteranceEvidence, ...] = ()
    text_record: dict[str, object] | None = None
    if audio is not None:
        context.check_cancel()
        asr_evidence, utterances, text_record = _hear_source(
            workspace, revision, asset, settings, workflow, providers, audio, source_dir, asr_epoch
        )
    outcome = _asr_outcome_document(revision, asset, grid, audio, asr_evidence, text_record)
    _record_transcription(workspace, asset, source_dir, outcome, asr_evidence, asr_epoch)
    return asr_evidence, utterances, text_record


def _hear_source(
    workspace: RawWorkspace,
    revision: AnalysisRevision,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
    audio: SelectedAudio,
    source_dir: Path,
    asr_epoch: int,
) -> tuple[AsrEvidence, tuple[UtteranceEvidence, ...], dict[str, object]]:
    """whisper로 시각과 글자를 듣고, 같은 청크 WAV를 두 번째 귀에 한 번 더 묻는다(D59 19c)."""
    asr_provider = _asr_provider_for(providers, workflow, settings, audio, asset.asset_id)
    cache_dir = workspace.root / "cache" / "source-asr" / asset.asset_id
    plan = stage1.plan_heard_chunks(
        audio,
        settings,
        settings=workflow,
        provider=asr_provider,
        workdir=source_dir,
        cache_dir=cache_dir,
        asr_epoch=asr_epoch,
    )
    evidence = stage1.transcribe_selected_audio(
        audio,
        plan.spans,
        settings=workflow,
        understanding=settings,
        provider=asr_provider,
        on=providers.on or datetime.now(UTC).date(),
        request_id=f"{revision.revision_id}:{asset.asset_id}",
        workdir=source_dir,
        cache_dir=cache_dir,
        asr_epoch=asr_epoch,
        chunk_plan=plan.schema,
    )
    utterances, record = _cross_check_text(
        workspace, revision, asset, settings, workflow, providers, audio, evidence, source_dir
    )
    return evidence, utterances, record


def _cross_check_text(
    workspace: RawWorkspace,
    revision: AnalysisRevision,
    asset: SourceAsset,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: AnalysisProviders,
    audio: SelectedAudio,
    evidence: AsrEvidence,
    source_dir: Path,
) -> tuple[tuple[UtteranceEvidence, ...], dict[str, object]]:
    """1단계 글자 경로(D59 19c) — whisper가 들은 같은 WAV를 두 번째 귀에 묻고 게이트로 얹는다.

    용어집은 이 요청에만 실린다(규칙 6 — whisper 요청에는 힌트 칸이 없다). 제공자가 없으면(승인이
    글자를 묶지 않은 온라인 — 교차 확인이 꺼져 있거나 물을 청크가 없다 — ·글자 픽스처 없는 재생)
    아무것도 보내지 않고 캐시에 있는 답만 쓴다."""
    request = source_text.TextPassRequest(
        settings=SOURCE_TEXT_SETTINGS,
        glossary=glossary.current(workspace).terms,
        workflow=workflow,
        on=providers.on or datetime.now(UTC).date(),
        request_id=f"{revision.revision_id}:{asset.asset_id}",
        workdir=source_dir,
        cache_dir=workspace.root / "cache" / source_text.TEXT_CACHE_DIRNAME / asset.asset_id,
        origin=providers.origin,
    )
    provider = _text_provider_for(providers, workflow, settings, audio, asset.asset_id, request)
    outcomes = source_text.run_text_pass(audio, evidence, request, provider)
    utterances = stage1.utterance_evidence(audio, evidence)
    result = source_text.apply_text_pass(
        utterances, outcomes, request, audio.sample_rate, _seeded_rows(workspace, asset)
    )
    return result.utterances, result.record


def _seeded_rows(workspace: RawWorkspace, asset: SourceAsset) -> list[dict]:
    """지금 심긴 **고치지 않은** 원본 자막의 줄 — 글자 답이 없는 청크가 이어 갈 증거(19c 검증).

    고친 자막은 심기가 덮지 않으므로 필요 없고, 다른 원본의 자막이면 심기가 거절한다."""
    document = source_transcript.load(workspace, asset.asset_id)
    if document is None or source_transcript.human_edited(document):
        return []
    if document["source_sha256"] != (asset.content_sha256 or ""):
        return []
    return list(document["utterances"])


def _record_transcription(
    workspace: RawWorkspace,
    asset: SourceAsset,
    source_dir: Path,
    outcome: dict[str, object],
    asr_evidence: AsrEvidence | None,
    asr_epoch: int,
) -> None:
    """전사 직후 기록 — 뒤 단계가 죽어도 심긴 자막의 빈 구간이 전사 실패인지 알 수 있다(규칙 9).

    다시 듣기 차례(D59 19b)를 실패 청크 없이 끝까지 들었으면 그 차례를 들은 것으로 적는다 —
    실패가 남았으면 다음 실행이 같은 차례로 실패 청크만 다시 묻는다(성공 청크는 캐시)."""
    _write_json_atomic(source_dir / ASR_OUTCOME_DOCUMENT_NAME, outcome)
    if asr_evidence is not None and all(c.succeeded for c in asr_evidence.chunks):
        asr_epoch_store.mark_heard(workspace, asset.asset_id, asr_epoch)


def _asr_outcome_document(
    revision: AnalysisRevision,
    asset: SourceAsset,
    grid: VideoGrid,
    audio: SelectedAudio | None,
    asr_evidence: AsrEvidence | None,
    text_record: dict[str, object] | None = None,
) -> dict[str, object]:
    """`asr_phase.json`의 오디오·전사 칸과 같은 모양 — 읽는 쪽이 한 가지 모양만 알면 된다.

    글자 경로 기록(D59 19c)은 `asr_text`로 따로 싣는다 — `asr.chunks`에 섞으면 글자 실패가 전사
    실패로 읽혀 whisper를 다시 산다."""
    return {
        **_text_section(text_record),
        "schema": ASR_OUTCOME_SCHEMA,
        "analysis_revision_id": revision.revision_id,
        "asset_id": asset.asset_id,
        "source_sha256": asset.content_sha256 or "",
        "video": {"presentation_origin": _fraction_json(grid.presentation_origin)},
        "audio": _audio_section(audio),
        "asr": _asr_section(asr_evidence),
        "created_at": _now(),
    }


def _audio_section(audio: SelectedAudio | None) -> dict[str, object]:
    """1단계 기록들의 오디오 칸 — 무성 원본은 '오디오 없음'뿐이다."""
    if audio is None:
        return {"status": stage1.SPEECH_AUDIO_ABSENT}
    return stage1.selected_audio_json(audio)


def _asr_section(asr_evidence: AsrEvidence | None) -> dict[str, object]:
    """1단계 기록들의 whisper 칸 — 실패 청크도 그대로 싣는다."""
    if asr_evidence is None:
        return {"status": stage1.SPEECH_AUDIO_ABSENT, "request_id": None, "chunks": []}
    return stage1.asr_evidence_json(asr_evidence)


def _text_section(text_record: dict[str, object] | None) -> dict[str, object]:
    """글자 경로 칸(D59 19c) — `asr.chunks`와 따로 둔다. 무성 원본에는 칸이 없다."""
    return {} if text_record is None else {"asr_text": text_record}


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
    asr_epoch: int = 0,
) -> AnalysisSourceResult:
    """1단계(내부 ASR) — 전사·창·프레임을 남기고 **의미 승인 앞에서 멈춘다**.

    Luna는 한 번도 가지 않는다. 무성 원본은 전송이 없는 그대로 창·프레임만
    남긴다(ASR 작업을 지어내지 않는다, §7.3). 같은 지문의 1단계 기록이 이미
    있으면 다시 부르지 않는다 — 재시작·재개는 전사를 재사용한다(A4-F1).
    """
    artifact_path = source_dir / ASR_PHASE_DOCUMENT_NAME
    artifact = _reusable_phase(artifact_path, providers, asset, input_fp)
    if artifact is not None:
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
    asr_evidence, utterances, text_record = _transcribe_source(
        context,
        workspace,
        revision,
        asset,
        settings,
        workflow,
        providers,
        audio=audio,
        grid=grid,
        source_dir=source_dir,
        asr_epoch=asr_epoch,
    )
    utterances, input_fp = _seed_stage1(
        workspace, asset, timeline, utterances, settings, workflow, input_fp, asr_epoch
    )
    plan = _scene_plan(workspace, asset, plan_common_windows(timeline, utterances, settings))
    context.check_cancel()
    frames = stage1.sample_frames(
        plan,
        directory=source_dir / "frames",
        settings=settings,
        sampler=media_refs.frame_sampler(workspace, asset, slot_packets=grid.slot_packets),
    )
    semantic_fp = _semantic_fingerprint(asset, grid, audio, plan, settings, workflow)
    # 2단계가 장면 컷을 같은 출처에서 다시 재도록 남긴다 — 출처가 바뀌면 실측 지문이 어긋난다.
    frame_source = media_refs.frames_fingerprint(
        workspace, asset.asset_id, mapped=bool(grid.slot_packets)
    )
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
            "audio": _audio_section(audio),
            "asr": _asr_section(asr_evidence),
            **_text_section(text_record),
            "utterances": stage1.utterances_json(utterances),
            "windows": stage1.window_plan_json(plan),
            "frames": stage1.frame_sampling_json(frames),
            "frame_source": frame_source,
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
    input_fp, _epoch = _current_input(workspace, asset, grid, audio, settings, workflow)
    if input_fp != str(artifact.get("input_fingerprint")):
        raise AnalysisError(
            f"설정·원본이 1단계 기록과 다르다: {asset.source_name} — 새 1단계 승인을 받아라"
        )
    cached = _reusable_cache(store, workspace, input_fp, asset.content_sha256 or "")
    if cached is not None:
        return _upsert_cached(store, revision, source_order, asset, input_fp, cached, "(2단계)")
    utterances = _seed_transcript(workspace, asset, timeline, utterances)
    frame_source = _stage1_frame_source(workspace, asset, artifact)
    plan = _scene_plan(
        workspace, asset, plan_common_windows(timeline, utterances, settings), frame_source
    )
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
        frame_source=frame_source,
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
    frame_source: str,
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
        payload = luna.build_semantic_payload(evidence, settings, workflow.models.luna_model)
        digest = luna.luna_request_sha256(workflow.models.luna_model, payload)
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
    # 감사용 출처 기록일 뿐이다 — 재사용·견적이 이 값을 읽으면 프록시 이전 분석이 다시 청구된다.
    document["frame_source"] = frame_source
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


def _text_provider_for(
    providers: AnalysisProviders,
    workflow: RawWorkflowConfig,
    settings: RawUnderstandingSettings,
    audio: SelectedAudio,
    asset_id: str,
    request: source_text.TextPassRequest,
) -> source_text.SourceTextProvider | None:
    """1단계 글자 경로의 제공자 — 재생 선언이거나, 승인(v2)이 글자를 묶은 실제 전송(D59 19d).

    글자 모델이 빠진 승인은 캐시만 쓴다. 글자 문이 닫히면 WARN하고 묻지 않는다(D39)."""
    if not providers.is_live():
        return None if providers.replay is None else providers.replay.text_for(audio.source_sha256)
    approval = (providers.asr_approvals or {}).get(asset_id)
    model = _approved_text_model(approval)
    if approval is None or model is None:
        return None
    identity = source_text.text_identity(request.settings, request.glossary_sha)
    try:
        require_text_approval(approval, workflow=workflow, identity=identity, on=request.on)
    except AnalysisApprovalError as error:
        log.warn(
            f"1단계 글자 경로: 글자 요청을 보내지 않습니다 — whisper 글자 그대로 둡니다: {error}"
        )
        return None
    return _approved_text_provider(providers, workflow, settings, audio, approval, identity)


def _approved_text_provider(
    providers: AnalysisProviders,
    workflow: RawWorkflowConfig,
    settings: RawUnderstandingSettings,
    audio: SelectedAudio,
    approval: AsrPhaseApproval,
    identity: str,
) -> source_text.ApprovedPhaseTextProvider:
    """글자 문을 지난 승인의 전송 경계 — 상한·청크 집합은 승인(견적)이 묶은 그대로다."""
    sender, cfg = providers.live_sender, providers.live_cfg
    if sender is None or cfg is None:
        raise AnalysisApprovalError(
            "실제 호출에는 전송 경계(sender)와 엔진 설정(cfg)이 필요하다 — 사람이 공급한다"
        )
    keys = approval.text_chunk_keys
    return source_text.ApprovedPhaseTextProvider(
        inner=TransportTextProvider(sender=sender, cfg=cfg),  # type: ignore[arg-type]
        workflow=workflow,
        understanding=settings,
        audio=audio,
        approval=approval,
        approved_model=str(approval.text_model),
        approved_identity=identity,
        gate=CountingAsrGate(limit=approval.text_chunk_budget),
        approved_keys=None if keys is None else frozenset(keys),
    )


def _reusable_phase(
    artifact_path: Path, providers: AnalysisProviders, asset: SourceAsset, input_fp: str
) -> dict[str, Any] | None:
    """같은 리비전의 1단계 기록을 그대로 쓸 수 있으면 그 기록, 아니면 None(1단계를 다시 듣는다).

    whisper 실패가 남았거나(`_phase_reusable`), 승인이 글자를 묶었거나(`_text_due`), 기록의 입력
    지문이 지금과 다르면 다시 듣는다. 입력이 다른 기록은 교차 확인이 켜진 리비전에서 생긴다(D59
    19d): 결속은 두 번째 귀 글자를 빼므로 용어집을 되돌리면 옛 리비전에 다시 붙는데, 그 기록은 다른
    글자로 심은 것이다. 다시 들어도 whisper·글자는 캐시에서 오고 새로 사는 것은 승인이 묶은
    만큼뿐이다."""
    if not _phase_reusable(artifact_path) or _text_due(providers, asset.asset_id):
        return None
    artifact = _read_asr_phase_artifact(artifact_path)
    if str(artifact.get("input_fingerprint")) == input_fp:
        return artifact
    log.warn(f"1단계 기록이 지금 입력과 달라 다시 듣습니다: {asset.source_name} (캐시 우선)")
    return None


def _text_due(providers: AnalysisProviders, asset_id: str) -> bool:
    """승인이 이 원본의 글자 요청을 묶었는가(D59 19d) — 그러면 1단계 기록을 다시 쓰지 않는다.

    견적은 답이 캐시에 없는 청크가 있을 때만 글자 분량을 세고, 승인은 그때만 글자를 묶는다. 그
    청크를 묻는 것이 이 승인의 일이므로 같은 리비전의 1단계 기록(글자 실패가 남은 「전사만」)을
    그대로 쓰지 않고 다시 듣는다 — whisper는 청크 캐시에서 오고 글자는 답이 없는 청크만 묻는다."""
    if not providers.is_live():
        return False
    return _approved_text_model((providers.asr_approvals or {}).get(asset_id)) is not None


def _approved_text_model(approval: AsrPhaseApproval | None) -> str | None:
    """1단계 승인이 묶은 글자 모델(D59 19d) — 승인이 없거나 글자 칸이 비었으면 None(요청 0회).

    글자 칸은 「다른 귀로 교차 확인」이 켜져 있고 견적이 물을 청크를 셌을 때만 채워진다
    (`browser_runtime.mint_asr_approvals`) — 사람이 본 견적 없이 유료 글자 요청이 나가는 길이
    없다."""
    if approval is None or not approval.text_model:
        return None
    return approval.text_model


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
            observer=request_usage.observer(),
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
        observer=request_usage.observer(),
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
                **stage1.utterance_text_evidence(item),
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
        scores, words = stage1.asr_chunk_material(item)
        chunks.append(
            AsrChunkOutcome(
                chunk_index=int(item["chunk_index"]),
                sample_start=int(item["sample_start"]),
                sample_end=int(item["sample_end"]),
                wav_sha256=str(item["wav_sha256"]),
                request_id=str(item["request_id"]),
                transcript=transcript,
                failure=str(item.get("failure") or ""),
                clamped_segments=tuple(int(at) for at in item.get("clamped_segments", [])),
                segment_scores=scores,
                words=words,
            )
        )
    return AsrEvidence(
        request_id=str(raw.get("request_id") or ""),
        sample_rate=int(raw.get("sample_rate") or stage1.PCM_SAMPLE_RATE),
        chunks=tuple(chunks),
        chunk_plan=str(raw.get("chunk_plan") or stage1.CHUNK_PLAN_FIXED),
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


def _require_source_unchanged(
    workspace: RawWorkspace, path: Path, asset: SourceAsset, *, trust_stat: bool
) -> None:
    """원본 재검증 — 크기·SHA-256을 지금 다시 읽는다(§14.2).

    `trust_stat`은 재사용 검사(캐시된 분석을 다시 쓸 수 있는가)만 켠다 — 지난 완전 검증 때와
    stat이 같으면 기록된 해시를 믿는다(`source_verify`, D62). 분석 본체는 끄고 완전 해시한다.
    """
    probed = path.stat()
    if probed.st_size != asset.size_bytes:
        raise AnalysisError(
            f"원본 크기가 가져오기 때와 다르다: {asset.source_relative_path}"
            f" ({asset.size_bytes} → {probed.st_size}B)"
        )
    digest = source_verify.source_digest(workspace, asset, path, trust_stat=trust_stat)
    if asset.content_sha256 is None or digest != asset.content_sha256:
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
    "ASR_OUTCOME_DOCUMENT_NAME",
    "ASR_OUTCOME_SCHEMA",
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


def _stage1_frame_source(
    workspace: RawWorkspace, asset: SourceAsset, artifact: Mapping[str, object]
) -> str:
    """1단계가 디코드한 출처 — 기록이 없으면 분석 프록시 이전 1단계라 원본(또는 FCP 프록시)이다."""
    return str(artifact.get("frame_source") or media_refs.fingerprint(workspace, asset.asset_id))


def _scene_cache(workspace: RawWorkspace, asset: SourceAsset, frame_source: str) -> Path:
    digest = hashlib.sha256(
        (str(asset.content_sha256) + frame_source + "scene/v1").encode()
    ).hexdigest()
    return workspace.root / "cache" / (digest + "-scenes.json")


def _scene_times(
    workspace: RawWorkspace, asset: SourceAsset, frame_source: str | None, mapped: bool
) -> tuple[Fraction, ...]:
    """디코드 출처마다 따로 캐시한 장면 컷 — 기록된 출처가 있으면 그 출처를 따른다.

    2단계는 1단계가 잰 출처를 넘긴다. 캐시가 있으면 프록시를 만들러 가지도 않는다. `mapped`(map
    시계, D70)면 출처가 맞춤 규칙 판을 싣는다 — 컷이 놓이는 슬롯이 규칙에서 나온다.
    """
    if frame_source is not None and _scene_cache(workspace, asset, frame_source).is_file():
        cache = _scene_cache(workspace, asset, frame_source)
    else:
        decode_source, actual = media_refs.frames_decode_source(
            workspace, asset, frame_source, mapped=mapped
        )
        cache = _scene_cache(workspace, asset, actual)
        if not cache.is_file():
            limit = ANALYSIS_PROXY_LIMITS.frame_batch_timeout(asset.duration_seconds)
            times = preprocess.verified_scene_times(decode_source, timeout_seconds=limit)
            _write_json_atomic(cache, {"times": [str(t) for t in times]})
            return times
    return tuple(Fraction(t) for t in json.loads(cache.read_text(encoding="utf-8"))["times"])


def _scene_plan(
    workspace: RawWorkspace,
    asset: SourceAsset,
    plan: WindowPlan,
    frame_source: str | None = None,
) -> WindowPlan:
    """Cache decoded cuts and attach only measured boundaries, retaining request image ceilings."""
    assert plan.video is not None
    times = _scene_times(workspace, asset, frame_source, bool(plan.video.slot_packets))
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
