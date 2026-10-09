"""최종 자막 엔진(A7-2) — 문구와 시간을 **같은 오디오 청크 위에서** 합친다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.3~§11.6·§14.3. 이 모듈은
엔진 라이브러리다 — UI·서비스·CLI 통합(A7-3)은 여기 없다.

계획(§11.3)을 그대로 코드로 옮긴다:

    최종 영상 조사·해시·오디오 확인          → final_video.register_final_video
    모델 기능/수명주기/설정 사전 검증        → 계획 단계 + 온라인 문
    비용 추정과 명시적 온라인 실행 승인       → CaptionApproval (바인딩 검사)
    오디오 추출·분할                          → final_video.plan_caption_chunks
    시간 경로: 세그먼트 → 반복 복구 → 골격   → repair.settle (커터·역할 어댑터 주입)
    문구 경로: 전사 JSON → 불변 문구 증거     → TextTranscriptProvider
    retext 대응 검증과 제한된 문구 교정       → retext.plan (세그먼트 게이트)
    미대응·불일치·누락 의심 보고              → findings · alignment 기록

지키는 선:

- **시각의 원천은 시간 제공자뿐**이다. 최종 시각 환산(`원점 + 청크 시작 샘플/주파수
  + 세그먼트 시각`)은 유리수로 **한 번만** 적용한다(§11.6). 글자 교정은 시각·세그먼트
  경계·순서를 절대 못 바꾼다.
- **문구 응답은 청크당 하나다.** 교정과 검증이 같은 응답을 공유하므로 검증 on/off는
  SRT 바이트도 추가 문구 호출도 바꾸지 않는다(§11.3).
- **실패는 숨지 않는다.** 문구 요청이 실패한 실행은 `partial_failure`·명시적 초안이지
  최신 문구 반영 성공이 아니다. 세그먼트 사이에 낀 문구는 `unplaced` 기록일 뿐
  가짜 cue를 만들지 않는다(§11.5).
- **온라인은 승인이 열어야 한다.** `network_mode`·`VLOGSRT_OFFLINE`·바인딩 검사·
  수명주기 문이 **호출 한 번 나가기 전에** 전부 닫힌다(§14.3).
- 요금은 등록표 스냅샷에서 읽는다 — 리터럴 가격이 코드에 없다(§11.8).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import wave
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path
from typing import cast

from vlogsrt import env, log, repair, retext, transcribe
from vlogsrt.config import Config
from vlogsrt.models import (
    RETEXT_APPLIED,
    RETEXT_OFF,
    Chunk,
    Repair,
    RetextRecord,
    RetextRejection,
    Segment,
)
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw.caption_config import (
    FinalCaptionSettings,
    validate_final_caption_settings,
)
from vlogsrt.project.raw.caption_provider import (
    AudioChunkBinding,
    RescueTimingProvider,
    TextTranscript,
    TextTranscriptProvider,
    TimedTranscript,
    TimingProvider,
    TransportTextProvider,
    TransportTimingProvider,
    audio_chunk_binding,
)
from vlogsrt.project.raw.config import (
    RawWorkflowConfig,
    raw_workflow_config_to_json,
    transcription_identity_view,
    validate_raw_workflow_config,
)
from vlogsrt.project.raw.final_video import (
    FinalVideoChanged,
    FinalVideoInput,
    caption_chunk,
    pcm_span_cutter,
    plan_caption_chunks,
    require_audio_unchanged,
    require_unchanged,
    write_caption_chunk,
)
from vlogsrt.project.raw.understanding import PCM_SAMPLE_WIDTH, SampleSpan, SelectedAudio
from vlogsrt.transcribe import ARBITER_KIND, RESCUE_KIND, ChunkResponse, TranscriptionJob

# ─────────────────────────────────────────────────────────────
#  실행 상태 — 다섯 개는 다른 말이다(§11.11)
# ─────────────────────────────────────────────────────────────

STATUS_NO_AUDIO = "no_audio"
STATUS_CONFIRMED_NO_SPEECH = "confirmed_no_speech"
"""사람 검토가 무발화를 확정한 뒤의 상태 — 이 엔진은 스스로 이 상태를 만들지 않는다.

빈 응답 전부가 정상이라도 그것은 "아무 말이 없다"의 증명이 아니다(§11.11).
이 계층은 빈 결과를 `review_required` + `unconfirmed_empty`로 올리고, 확정은
A7-3의 사람 검토 절차가 내린다.
"""
STATUS_PARTIAL_FAILURE = "partial_failure"
STATUS_REVIEW_REQUIRED = "review_required"
STATUS_READY = "ready"
STATUS_CANCELLED = "cancelled"

# 큐가 없을 때의 근거 — 빈 결과가 무발화의 증명은 아니다(§11.11).
BASIS_CONFIRMED_NO_SPEECH = "confirmed_no_speech"
BASIS_UNCONFIRMED_EMPTY = "unconfirmed_empty"
BASIS_CUES_PRESENT = "cues_present"

# 자동 큐의 근거 칸(§11.5 — 최소 확인 목록).
TEXT_SOURCE_RETEXT = "retext"
TEXT_SOURCE_WHISPER = "timing_spine"
TIMING_SOURCE = "timing_provider"
CORRECTION_ACCEPTED = "accepted"
CORRECTION_REJECTED = "rejected"
CORRECTION_EXCLUDED = "excluded"  # 회복 못 한 되풀이 자리 — 교정하지 않는다(D37)
CORRECTION_UNAVAILABLE = "unavailable"  # 문구 응답을 못 받은 조각
CORRECTION_NONE = "whisper"

CUE_RETEXTED = "retext"
CUE_CLIPPED_START = "clipped_start"
CUE_CLIPPED_END = "clipped_end"
CUE_OUT_OF_FINAL = "out_of_final_range"
CUE_ZERO_EXTENT = "zero_extent"

SECONDS_PER_MINUTE = Decimal(60)
"""분당 요금과 초를 잇는 환산 — Decimal 산술만 쓴다(§11.8)."""


class CaptionRunError(RuntimeError):
    """최종 자막 실행이 계약을 어긴다 — 모든 갈래의 부모."""


class CaptionProviderMissing(CaptionRunError):
    """주입된 제공자가 없다 — 호출 한 번 없이 닫힌다(승인의 부재가 성공이 되는 금지)."""


class CaptionApprovalMissing(CaptionRunError):
    """온라인 실행인데 명시적 승인이 없다(§14.3)."""


class CaptionApprovalMismatch(CaptionRunError):
    """승인이 계획과 다른 것에 묶여 있다 — 위조·낡은 승인은 호출 전에 닫힌다."""


class CaptionBudgetExhausted(CaptionRunError):
    """승인된 예산 안에 다음 요청의 예약분이 들어가지 않는다(§14.3)."""


class CaptionRunCancelled(Exception):
    """취소 게이트가 닫혔다 — 이미 끝난 유효 결과는 보존한다(§14.1)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ─────────────────────────────────────────────────────────────
#  승인 — 계획에 묶인 값 외에는 아무것도 연다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CaptionApproval:
    """온라인 실행 승인 — 무엇을·어디까지·어떤 모델로 돌릴지에 묶인다(§14.3).

    모든 칸이 실행 계획과 **정확히** 같아야 한다. 하나라도 어긋나면 위조이거나
    낡은 승인이다 — 예산 칸은 상한이고 나머지는 등식이다.
    """

    final_video_sha256: str
    audio_stream_index: int
    sample_start: int
    sample_end: int
    text_model: str
    timing_model: str
    settings_fingerprint: str
    budget_usd: Decimal


# ─────────────────────────────────────────────────────────────
#  실행 계획
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CaptionRunPlan:
    """호출 나가기 전에 얼어 붙는 실행 계획 — 지문·청크·예상 비용까지."""

    run_id: str
    video: FinalVideoInput
    spans: tuple[SampleSpan, ...]
    settings: FinalCaptionSettings
    workflow: RawWorkflowConfig
    engine: Config
    fingerprint: str
    estimated_cost_usd: Decimal
    reserved_cost_usd: Decimal

    @property
    def extent_seconds(self) -> Fraction:
        total = sum(span.end - span.start for span in self.spans)
        return Fraction(total, self.video.audio.sample_rate)


def plan_caption_run(
    video: FinalVideoInput,
    *,
    run_id: str,
    settings: FinalCaptionSettings | None = None,
    workflow: RawWorkflowConfig | None = None,
    engine: Config | None = None,
) -> CaptionRunPlan:
    """실행 계획을 얼린다 — 이 시점에 청크·지문·비용이 전부 결정된다.

    계획이 나가는 즉시 검증도 끝난다: 설정 값역·워크플로 동결 계약·사본 무결성.
    승인은 이 지문에 묶이므로 계획 뒤의 어떤 변경도 승인을 무효로 만든다.
    """
    if not run_id.strip():
        raise CaptionRunError("run_id가 비어 있다")
    settings = settings or FinalCaptionSettings()
    workflow = workflow or RawWorkflowConfig()
    engine = engine or Config()
    validate_final_caption_settings(settings)
    validate_raw_workflow_config(workflow)
    require_unchanged(video)
    spans = plan_caption_chunks(video, settings)
    fingerprint = caption_settings_fingerprint(video, spans, settings, workflow, engine)
    base, reserved = estimate_caption_cost(video, spans, workflow, engine)
    return CaptionRunPlan(
        run_id=run_id,
        video=video,
        spans=spans,
        settings=settings,
        workflow=workflow,
        engine=engine,
        fingerprint=fingerprint,
        estimated_cost_usd=base,
        reserved_cost_usd=reserved,
    )


def caption_settings_fingerprint(
    video: FinalVideoInput,
    spans: Sequence[SampleSpan],
    settings: FinalCaptionSettings,
    workflow: RawWorkflowConfig,
    engine: Config,
) -> str:
    """실행 지문(§11.1의 `caption_settings_hash`)— 재사용 키가 아니라 **승인의 묶음**.

    최종 영상 리비전·오디오 선택(잰 값)·청크 범위·모델 역할·힌트·타이밍/교정
    임계값·기능 등록표 리비전까지 함께 잰다. 하나라도 다르면 다른 실행이다.
    """
    audio = video.audio
    payload = {
        "schema": settings.schema_version,
        "prompt_revision": settings.prompt_revision,
        "final_video_revision": video.final_video_revision,
        "pcm_sha256": audio.pcm_sha256,
        "audio_stream_index": audio.audio_stream_index,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "sample_count": audio.sample_count,
        "pcm_sample0_file_time": str(audio.pcm_sample0_file_time),
        "chunk_max_seconds": settings.chunk_max_seconds,
        "chunk_max_bytes": settings.chunk_max_bytes,
        "spans": [[span.start, span.end] for span in spans],
        "text_model": workflow.models.text_model,
        "timing_model": workflow.models.timing_model,
        "languages": list(workflow.caption.languages),
        "keywords": list(workflow.caption.keywords),
        "registry_revision": workflow.registry.revision,
        # 자막은 의미 분석 모델을 듣지 않는다 — 그 모델이 바뀌어도 같은 실행이다(D54)
        "workflow": json.loads(raw_workflow_config_to_json(transcription_identity_view(workflow))),
        "engine_thresholds": _engine_threshold_snapshot(engine),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _engine_threshold_snapshot(engine: Config) -> dict[str, object]:
    """골격·교정을 바꿀 수 있는 엔진 임계값 — 지문에 함께 얼린다.

    되풀이·교정 임계값이 다르면 같은 오디오로 다른 자막이 나오므로 승인도 다른
    세계를 가리켜야 한다(§14.2 — 캐시·승인 지문의 최소 목록).
    """
    return {
        "repair_enabled": engine.repair_enabled,
        "repair_max_attempts": engine.repair_max_attempts,
        "repair_temperature": engine.repair_temperature,
        "repair_chunk_seconds": engine.repair_chunk_seconds,
        "repair_collapse_min_seconds": engine.repair_collapse_min_seconds,
        "retext_enabled": engine.retext_enabled,
        "retext_segment_match_floor": engine.retext_segment_match_floor,
        "retext_length_ratio_max": engine.retext_length_ratio_max,
        "retext_length_ratio_min": engine.retext_length_ratio_min,
        "retext_chunk_similarity_floor": engine.retext_chunk_similarity_floor,
        "transcribe_temperature": engine.transcribe_temperature,
    }


def estimate_caption_cost(
    video: FinalVideoInput,
    spans: Sequence[SampleSpan],
    workflow: RawWorkflowConfig,
    engine: Config,
) -> tuple[Decimal, Decimal]:
    """기본 전사비와 복구 예약분(§11.8) — 등록표 요금만 쓰고 산술은 Decimal로.

    예약분은 보수적으로 **전체 길이 × 시도 상한**을 잡는다 — 되풀이가 어느 구간에서
    자랄지는 호출 전에 알 수 없고, 예약이 부족해서 실행 도중 예산이 뚫리는 것보다
    승인 단계에서 크게 보는 편이 맞다. 이 값은 상한이지 청구액이 아니다.
    """
    rate = Decimal(video.audio.sample_rate)
    total = Decimal(sum(span.end - span.start for span in spans))
    minutes = total / rate / SECONDS_PER_MINUTE
    per_minute = capabilities.price_per_minute(
        capabilities.ROLE_TEXT
    ) + capabilities.price_per_minute(capabilities.ROLE_TIMING)
    base = per_minute * minutes
    attempts = max(0, engine.repair_max_attempts) if engine.repair_enabled else 0
    reserved = per_minute * minutes * Decimal(attempts)
    return base.quantize(Decimal("0.0001")), reserved.quantize(Decimal("0.0001"))


def expected_approval(plan: CaptionRunPlan) -> CaptionApproval:
    """이 계획에 정확히 묶이는 승인 — 사람이 검토·서명할 값의 목록이다."""
    audio = plan.video.audio
    return CaptionApproval(
        final_video_sha256=plan.video.final_video_revision,
        audio_stream_index=audio.audio_stream_index,
        sample_start=plan.spans[0].start,
        sample_end=plan.spans[-1].end,
        text_model=plan.workflow.models.text_model,
        timing_model=plan.workflow.models.timing_model,
        settings_fingerprint=plan.fingerprint,
        budget_usd=plan.estimated_cost_usd + plan.reserved_cost_usd,
    )


def require_approval(plan: CaptionRunPlan, approval: CaptionApproval | None) -> None:
    """승인이 계획에 묶여 있는지 **호출 전에** 본다 — 어긋나면 무엇이 다른지 말한다."""
    if approval is None:
        raise CaptionApprovalMissing(
            f"온라인 실행 승인이 없다 — {plan.run_id}의 비용 추정과 지문을 먼저 보여라(§14.3)"
        )
    wanted = expected_approval(plan)
    mismatched = [
        name
        for name in (
            "final_video_sha256",
            "audio_stream_index",
            "sample_start",
            "sample_end",
            "text_model",
            "timing_model",
            "settings_fingerprint",
        )
        if getattr(approval, name) != getattr(wanted, name)
    ]
    if mismatched:
        raise CaptionApprovalMismatch(
            f"승인이 실행 계획과 다르다 — {mismatched}. 위조이거나 낡은 승인이다(§14.3)"
        )
    if approval.budget_usd < wanted.budget_usd:
        raise CaptionApprovalMismatch(
            f"승인 예산({approval.budget_usd})이 추정+예약({wanted.budget_usd})보다 작다"
        )


# ─────────────────────────────────────────────────────────────
#  실행 결과 — 불변 기록
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResponseEvidence:
    """요청 하나의 불변 응답 증거 — 신원과 원 payload를 함께 실는다(§11.3)."""

    request_id: str
    role: str
    chunk_index: int
    audio_sha256: str
    sample_start: int
    sample_end: int
    sample_rate: int
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ChunkRunRecord:
    """청크 하나의 실행 기록 — 시간·문구 두 요청의 성패를 나란히 남긴다."""

    chunk_index: int
    sample_start: int
    sample_end: int
    wav_sha256: str
    timing_request_id: str
    text_request_id: str
    timing_failure: str = ""
    text_failure: str = ""
    draft_segments: int = 0
    text_chars: int = 0

    @property
    def timing_ok(self) -> bool:
        return not self.timing_failure

    @property
    def text_ok(self) -> bool:
        return not self.text_failure


@dataclass(frozen=True, slots=True)
class CaptionCue:
    """자동 큐 하나 — 시각·글자·근거를 함께 실은 불변 기록(§11.5).

    시각은 최종 영상 시각의 **정확한 유리수**다(직렬화에서만 밀리초로 반올림).
    `whisper_text`는 시간 골격의 원문이고 `text`가 채택 문구다 — 둘이 다르면
    교정이 반영된 것이다.
    """

    start: Fraction
    end: Fraction
    text: str
    whisper_text: str
    timing_segment_ids: tuple[int, ...]
    timing_evidence_id: str
    text_evidence_ids: tuple[str, ...]
    text_source: str
    timing_source: str
    correction_status: str
    flags: tuple[str, ...] = ()

    @property
    def retexted(self) -> bool:
        return CUE_RETEXTED in self.flags


@dataclass(frozen=True, slots=True)
class QualityFinding:
    """검토 항목 — 경고는 사람이 보게 남고, 글자를 스스로 고치지 않는다(§11.7)."""

    severity: str
    code: str
    message: str
    chunk_index: int | None = None
    seg_id: int | None = None


@dataclass(frozen=True, slots=True)
class VerifyRow:
    """검증 표시 한 줄 — 같은 문구 응답과 채택 큐의 대조(출력은 못 바꾼다, 규칙 5)."""

    chunk_index: int
    similarity: float
    evidence_chars: int
    adopted_chars: int


@dataclass(frozen=True, slots=True)
class FinalCaptionResult:
    """한 실행의 전부 — 상태·큐·골격·수리·교정·증거·검토 항목."""

    run_id: str
    status: str
    fingerprint: str
    final_video_revision: str
    cues: tuple[CaptionCue, ...]
    spine: tuple[Segment, ...]
    repairs: tuple[Repair, ...]
    retext: RetextRecord
    chunks: tuple[ChunkRunRecord, ...]
    evidence: tuple[ResponseEvidence, ...]
    findings: tuple[QualityFinding, ...]
    verify_rows: tuple[VerifyRow, ...]
    speech_basis: str = BASIS_UNCONFIRMED_EMPTY
    cancellation_reason: str = ""

    @property
    def is_draft(self) -> bool:
        """검토 확정 전 결과다 — `final` 게시는 A7-3의 권한이다."""
        return self.status != STATUS_READY


# ─────────────────────────────────────────────────────────────
#  예산 장부 — 요청 예약은 승인 예산 안에서(§14.3)
# ─────────────────────────────────────────────────────────────


@dataclass
class _Ledger:
    """요청별 예약 장부 — 보낸 만큼만, 승인 예산을 넘는 다음 요청은 못 보낸다."""

    approval: CaptionApproval | None
    spent: Decimal = Decimal("0")

    def charge(self, seconds: Fraction, role: str) -> None:
        if self.approval is None:
            return
        minutes = Decimal(seconds.numerator) / Decimal(seconds.denominator) / SECONDS_PER_MINUTE
        price = capabilities.price_per_minute(role) * minutes
        if self.spent + price > self.approval.budget_usd:
            raise CaptionBudgetExhausted(
                f"예산을 넘는다 — 쓴 {self.spent} + 다음 {price.quantize(Decimal('0.0001'))}"
                f" > 승인 {self.approval.budget_usd}"
            )
        self.spent += price


# ─────────────────────────────────────────────────────────────
#  실행
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _TimingOutcome:
    index: int
    span: SampleSpan
    wav_sha256: str
    request_id: str
    transcript: TimedTranscript | None
    failure: str


@dataclass(frozen=True, slots=True)
class _TextOutcome:
    index: int
    span: SampleSpan
    request_id: str
    transcript: TextTranscript | None
    failure: str


@dataclass
class _RunState:
    """실행 도중 쌓이는 기록 — 결과 조립이 끝나면 불변 결과로 얼어붙는다."""

    timing: dict[int, _TimingOutcome] = field(default_factory=dict)
    texts: dict[int, _TextOutcome] = field(default_factory=dict)
    evidence: list[ResponseEvidence] = field(default_factory=list)
    findings: list[QualityFinding] = field(default_factory=list)
    repair_failure: str = ""
    settled: repair.Settled | None = None
    wav_sha256: dict[int, str] = field(default_factory=dict)
    slice_sha256: dict[int, str] = field(default_factory=dict)


def run_final_captions(
    plan: CaptionRunPlan,
    *,
    on: date,
    timing_provider: TimingProvider | None = None,
    text_provider: TextTranscriptProvider | None = None,
    workdir: Path | None = None,
    verify_enabled: bool = True,
    cancellation: Callable[[], None] | None = None,
    approval: CaptionApproval | None = None,
) -> FinalCaptionResult:
    """계획을 실행해 불변 결과를 돌려준다 — 문구 응답은 청크당 정확히 하나다.

    온라인 경로는 `network_mode="online"`일 때만 열리고, 그때는 환경 오프라인
    기본·승인 바인딩·수명주기 검사가 **첫 호출 앞에서** 전부 닫힌다. 알려진
    실전송 어댑터는 `network_mode`가 `online`이 아니면 승인이 아무리 정확해도
    실행 자체가 거부된다 — 설정·환경이 승인보다 앞선다(§14.3, 루트 재검).
    첫 호출 전에 검증 사본과 선택 PCM의 신원(크기·해시)을 다시 본다. 검증
    on/off는 문구 호출 수와 결과를 바꾸지 않는다 — 검증은 이미 받은 응답의
    표시일 뿐이다(§11.3).
    """
    if timing_provider is None or text_provider is None:
        raise CaptionProviderMissing(
            "시간·문구 제공자가 모두 주입되어야 한다 — 없는 제공자로는 호출도 계획도 없다"
        )
    _require_online_config_for_transport(plan, timing_provider, text_provider)
    if plan.workflow.network_mode == "online":
        _require_online_route(plan, approval, on)
        ledger = _Ledger(approval)
    else:
        ledger = _Ledger(None)
    require_unchanged(plan.video)
    require_audio_unchanged(plan.video)
    state = _RunState()
    workdir = workdir or plan.video.audio.pcm_path.parent
    try:
        _timing_pass(plan, timing_provider, on, workdir, ledger, cancellation, state)
        _repair_pass(plan, on, workdir, timing_provider, text_provider, ledger, cancellation, state)
        _text_pass(plan, text_provider, on, workdir, ledger, cancellation, state)
    except CaptionRunCancelled as stop:
        log.warn(f"최종 자막 실행이 취소됐다: {stop.reason} — 끝난 유효 결과는 보존한다")
        return _assemble_result(plan, state, cancelled_reason=stop.reason)
    return _assemble_result(plan, state, cancelled_reason="")


def _require_online_config_for_transport(
    plan: CaptionRunPlan, timing: TimingProvider, text: TextTranscriptProvider
) -> None:
    """알려진 실전송 어댑터는 `network_mode=online` 실행에서만 움직인다(§14.3).

    설정이 `offline`인데 실전송 어댑터가 끼어 있으면 **유효한 승인이 있어도**
    실행하지 않는다 — 설정·환경이 승인보다 앞선다. 승인 문서 하나가 설정보다
    큰 권한을 갖는 순간 오프라인 계약이 무너진다(루트 재검). 가짜·오프라인
    제공자는 이 문과 무관하게 오프라인 실행으로 돈다.
    """
    live = isinstance(timing, TransportTimingProvider) or isinstance(text, TransportTextProvider)
    if live and plan.workflow.network_mode != "online":
        raise CaptionRunError(
            "실전송 어댑터는 network_mode=online 실행에서만 움직인다 — 현재 설정은 "
            f"{plan.workflow.network_mode!r}다(§14.3). 유효한 승인도 오프라인 설정을 열 수 없다"
        )


def _require_online_route(plan: CaptionRunPlan, approval: CaptionApproval | None, on: date) -> None:
    """온라인 문 — 승인·환경·수명주기를 첫 호출 앞에서 한 번에 본다(§14.3·§11.10)."""
    if env.is_offline():
        raise CaptionRunError(
            "VLOGSRT_OFFLINE이 켜져 있다 — 온라인 최종 자막 경로는 못 연다(§14.3)"
        )
    require_approval(plan, approval)
    capabilities.preflight_online_transcription(
        plan.workflow, on, roles=(capabilities.ROLE_TEXT, capabilities.ROLE_TIMING)
    )


def _chunk_of(plan: CaptionRunPlan, index: int, workdir: Path, state: _RunState) -> Chunk:
    """청크 WAV를 계획의 범위에 맞게 확보한다 — 재사용은 실제 바이트가 증명한다.

    파일 이름은 청크 번호가 아니라 **오디오 신원 + 샘플 범위**로 지어진다 — 같은
    workdir에서 청크 폭이 바뀌면 예전 청크가 새 범위로 재사용되는 일이 없다(루트
    재검). 재사용하든 새로 쓰든 보내기 전에 WAV 헤더(주파수·채널·폭)와 프레임
    수·payload 해시를 저장 PCM의 그 슬라이스와 대조한다 — 같은 길이의 다른
    바이트도 거짓 재사용이다.
    """
    span = plan.spans[index]
    workdir.mkdir(parents=True, exist_ok=True)
    wav = workdir / _chunk_wav_name(plan, index)
    expected = _expected_slice_sha256(plan, index, state)
    if not _wav_matches_slice(wav, plan.video.audio, span, expected):
        write_caption_chunk(plan.video, span, wav)
    if not _wav_matches_slice(wav, plan.video.audio, span, expected):
        raise CaptionRunError(
            f"청크 {index} WAV가 저장 PCM 슬라이스 [{span.start}, {span.end})와 같지 않다 — "
            "검증 못 한 바이트는 보내지 않는다"
        )
    state.wav_sha256[index] = _sha256(wav)
    return caption_chunk(plan.video, span, index, wav)


def _chunk_wav_name(plan: CaptionRunPlan, index: int) -> str:
    """청크 WAV 이름 — 오디오 해시와 정확한 샘플 경계로 지어 낡은 파일과 겹치지 않게."""
    span = plan.spans[index]
    stem = plan.video.audio.pcm_sha256[:16]
    return f"caption-chunk-{index:04d}-{stem}-s{span.start}-e{span.end}.wav"


def _expected_slice_sha256(plan: CaptionRunPlan, index: int, state: _RunState) -> str:
    """저장 PCM의 그 슬라이스 해시 — 청크가 덮어야 할 바이트의 정본(실행마다 한 번)."""
    if index not in state.slice_sha256:
        audio = plan.video.audio
        span = plan.spans[index]
        frame_bytes = PCM_SAMPLE_WIDTH * audio.channels
        digest = hashlib.sha256()
        with audio.pcm_path.open("rb") as source:
            source.seek(span.start * frame_bytes)
            remaining = (span.end - span.start) * frame_bytes
            while remaining > 0:
                block = source.read(min(remaining, 1024 * 1024))
                if not block:
                    raise FinalVideoChanged(
                        f"PCM이 계획의 슬라이스보다 짧다: {audio.pcm_path} — "
                        f"[{span.start}, {span.end}) 중 {remaining}바이트가 없다"
                    )
                digest.update(block)
                remaining -= len(block)
        state.slice_sha256[index] = digest.hexdigest()
    return state.slice_sha256[index]


def _wav_matches_slice(
    wav: Path, audio: SelectedAudio, span: SampleSpan, expected_sha: str
) -> bool:
    """WAV가 저장 PCM의 정확히 그 슬라이스인가 — 헤더·프레임 수·payload 해시로."""
    frames = span.end - span.start
    try:
        with wave.open(str(wav), "rb") as handle:
            if handle.getsampwidth() != PCM_SAMPLE_WIDTH:
                return False
            if handle.getnchannels() != audio.channels:
                return False
            if handle.getframerate() != audio.sample_rate:
                return False
            if handle.getnframes() != frames:
                return False
            payload = handle.readframes(frames)
    except (wave.Error, OSError):
        return False
    return hashlib.sha256(payload).hexdigest() == expected_sha


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _binding(plan: CaptionRunPlan, index: int, chunk: Chunk, request_id: str) -> AudioChunkBinding:
    span = plan.spans[index]
    return audio_chunk_binding(
        chunk,
        sample_start=span.start,
        sample_end=span.end,
        sample_rate=plan.video.audio.sample_rate,
        request_id=request_id,
    )


def _cancel(cancellation: Callable[[], None] | None) -> None:
    """취소 게이트 — 게이트가 닫히면 예외로 올라가고 유효 결과는 보존한다(§14.1)."""
    if cancellation is not None:
        cancellation()


def _ask_timing(
    plan: CaptionRunPlan,
    provider: TimingProvider,
    on: date,
    index: int,
    chunk: Chunk,
    request_id: str,
    ledger: _Ledger,
) -> TimedTranscript:
    """청크 하나를 시간 제공자에게 묻는다 — 예약·신원·전송이 한 덩어리다.

    온도 손잡이가 있는 제공자에게는 승인 지문이 얼린 엔진 1차 온도를 그대로 보낸다
    (D28·D55 — 예전엔 지문에만 0.6이 적히고 요청에는 빠져 API 기본 0으로 들렸다).
    """
    ledger.charge(_span_seconds(plan, index), capabilities.ROLE_TIMING)
    binding = _binding(plan, index, chunk, request_id)
    if "temperature" in inspect.signature(provider.timed_transcript).parameters:
        tempered = cast(RescueTimingProvider, provider)  # 서명 검사가 이 계약을 보증한다
        return tempered.timed_transcript(
            binding=binding,
            chunk=chunk,
            settings=plan.workflow,
            on=on,
            stem=chunk.path.stem,
            source=plan.video.source_id,
            temperature=plan.engine.transcribe_temperature,
        )
    return provider.timed_transcript(
        binding=binding,
        chunk=chunk,
        settings=plan.workflow,
        on=on,
        stem=chunk.path.stem,
        source=plan.video.source_id,
    )


def _timing_pass(
    plan: CaptionRunPlan,
    provider: TimingProvider,
    on: date,
    workdir: Path,
    ledger: _Ledger,
    cancellation: Callable[[], None] | None,
    state: _RunState,
) -> None:
    """시간 경로 — 청크마다 묶은 신원으로 시간 제공자를 부른다(§11.3)."""
    for index in range(len(plan.spans)):
        _cancel(cancellation)
        request_id = f"{plan.run_id}:timing:{index:04d}"
        chunk = _chunk_of(plan, index, workdir, state)
        try:
            timed = _ask_timing(plan, provider, on, index, chunk, request_id, ledger)
        except Exception as exc:  # noqa: BLE001 - 청크 실패는 기록이지 실행 정지가 아니다
            _record_chunk_failure(state, plan, index, request_id, exc, role="timing")
            continue
        state.evidence.append(
            _evidence(request_id, capabilities.ROLE_TIMING, index, timed.binding, timed.response)
        )
        _note_kept_overrun(state, index, timed)
        state.timing[index] = _TimingOutcome(
            index=index,
            span=plan.spans[index],
            wav_sha256=state.wav_sha256[index],
            request_id=request_id,
            transcript=timed,
            failure="",
        )


def _note_kept_overrun(state: _RunState, index: int, timed: TimedTranscript) -> None:
    """청크 끝을 넘은 세그먼트 — 이 경로는 원 응답의 whisper 시각을 그대로 쓴다(D55).

    해석기는 청크를 버리지 않았을 뿐이고 엔진 골격은 원 응답에서 선다 — 넘친 끝은 청크 끝으로
    닫히지 않는다. 그 사실을 검토 항목으로 남긴다(조용한 손질도, 조용한 그대로도 없다).
    """
    if not timed.clamped_segments:
        return
    state.findings.append(
        QualityFinding(
            severity="warn",
            code="timing_overrun_kept",
            message=(
                f"청크 {index}: 세그먼트 {len(timed.clamped_segments)}개의 끝이 청크 길이를 넘었다"
                " — 청크는 버리지 않고 whisper 시각 그대로 썼다 (D55)"
            ),
        )
    )


def _ask_text(
    plan: CaptionRunPlan,
    provider: TextTranscriptProvider,
    on: date,
    index: int,
    chunk: Chunk,
    request_id: str,
    ledger: _Ledger,
) -> TextTranscript:
    """청크 하나를 문구 제공자에게 묻는다 — 이 응답을 교정과 검증이 공유한다."""
    ledger.charge(_span_seconds(plan, index), capabilities.ROLE_TEXT)
    binding = _binding(plan, index, chunk, request_id)
    return provider.text_transcript(
        binding=binding,
        chunk=chunk,
        settings=plan.workflow,
        on=on,
        stem=chunk.path.stem,
        source=plan.video.source_id,
    )


def _text_pass(
    plan: CaptionRunPlan,
    provider: TextTranscriptProvider,
    on: date,
    workdir: Path,
    ledger: _Ledger,
    cancellation: Callable[[], None] | None,
    state: _RunState,
) -> None:
    """문구 경로 — 청크당 **하나**의 응답을 교정과 검증이 함께 쓴다(§11.3).

    검증 플래그는 여기 오지 않는다 — 응답은 검증 때문에 더 부르지도 덜 부르지도
    않는다. 실패한 청크는 실패로 남고 그 조각의 글자는 시간 골격의 것이 나간다.
    """
    for index in range(len(plan.spans)):
        _cancel(cancellation)
        request_id = f"{plan.run_id}:text:{index:04d}"
        chunk = _chunk_of(plan, index, workdir, state)
        try:
            text = _ask_text(plan, provider, on, index, chunk, request_id, ledger)
        except Exception as exc:  # noqa: BLE001 - 문구 실패는 partial_failure로 올라간다
            _record_chunk_failure(state, plan, index, request_id, exc, role="text")
            continue
        state.evidence.append(
            _evidence(request_id, capabilities.ROLE_TEXT, index, text.binding, text.response)
        )
        state.texts[index] = _TextOutcome(
            index=index,
            span=plan.spans[index],
            request_id=request_id,
            transcript=text,
            failure="",
        )


def _record_chunk_failure(
    state: _RunState,
    plan: CaptionRunPlan,
    index: int,
    request_id: str,
    exc: Exception,
    *,
    role: str,
) -> None:
    """청크 실패 기록 — 무발화·빈 성공으로 바꾸지 않고 이유를 남긴다(§11.11)."""
    message = f"{type(exc).__name__}: {exc}"
    log.warn(f"최종 자막 {role} 청크 {index} 실패: {message}")
    state.findings.append(
        QualityFinding(
            severity="error",
            code=f"{role}_chunk_failed",
            message=message,
            chunk_index=index,
        )
    )
    if role == "timing" and index not in state.timing:
        state.timing[index] = _TimingOutcome(
            index=index,
            span=plan.spans[index],
            wav_sha256=state.wav_sha256.get(index, ""),
            request_id=request_id,
            transcript=None,
            failure=message,
        )
    if role == "text" and index not in state.texts:
        state.texts[index] = _TextOutcome(
            index=index,
            span=plan.spans[index],
            request_id=request_id,
            transcript=None,
            failure=message,
        )


def _evidence(
    request_id: str, role: str, index: int, binding: AudioChunkBinding, payload: object
) -> ResponseEvidence:
    """응답 증거 — 신원과 원 payload를 함께 얼어붙인다."""
    return ResponseEvidence(
        request_id=request_id,
        role=role,
        chunk_index=index,
        audio_sha256=binding.audio_sha256,
        sample_start=binding.sample_start,
        sample_end=binding.sample_end,
        sample_rate=binding.sample_rate,
        payload=payload if isinstance(payload, Mapping) else {},
    )


# ─────────────────────────────────────────────────────────────
#  되풀이 재전사 — 역할 어댑터로 엔진 repair를 그대로 얹는다
# ─────────────────────────────────────────────────────────────


@dataclass
class _RescueTranscriber:
    """엔진 `repair`가 부르는 Transcriber — 재전사는 시간 제공자, 심판은 문구 제공자.

    되풀이 복구·접기·유지 판정은 엔진 `repair.settle`이 그대로 한다. 이 어댑터는
    역할만 번역한다: `RESCUE_KIND` → 시간 제공자(온도 손잡이는 받는 제공자에만),
    `ARBITER_KIND` → 문구 제공자. 시각은 여전히 whisper 세그먼트에서만 온다.
    """

    plan: CaptionRunPlan
    on: date
    timing: TimingProvider
    text: TextTranscriptProvider
    ledger: _Ledger
    cancellation: Callable[[], None] | None
    state: _RunState
    rounds: int = 0

    def __post_init__(self) -> None:
        signature = inspect.signature(self.timing.timed_transcript)
        self._tempered = "temperature" in signature.parameters

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        if job.kind == RESCUE_KIND:
            return self._rescue(job, cfg)
        if job.kind == ARBITER_KIND:
            return self._arbitrate(job)
        raise CaptionRunError(f"재전사 어댑터는 {job.kind!r} 호출을 모른다")

    def _rescue(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.rounds += 1
        if not self._tempered:
            # 온도를 못 받는 제공자가 회복 요청을 기본 온도로 조용히 보내는 길은
            # 없다(D28) — 회복 시도는 실패로 기록되고 되풀이는 접기/유지로 남는다.
            self.state.findings.append(
                QualityFinding(
                    severity="error",
                    code="rescue_temperature_unsupported",
                    message=(
                        f"시간 제공자가 재전사 온도 손잡이를 받지 않는다"
                        f" ({cfg.transcribe_temperature}로 묻지 못했다) — 회복 시도를 건너뛴다"
                    ),
                )
            )
            raise CaptionRunError(
                "시간 제공자가 temperature를 받지 않는다 — 뜨겁게 다시 묻는 재전사는 못 한다"
            )
        responses: list[ChunkResponse] = []
        for chunk in job.chunks:
            _cancel(self.cancellation)
            request_id = f"{self.plan.run_id}:rescue:{chunk.index:04d}:r{self.rounds}"
            binding = self._binding_for(chunk, request_id)
            self.ledger.charge(_chunk_seconds(chunk), capabilities.ROLE_TIMING)
            provider = cast(
                RescueTimingProvider, self.timing
            )  # _tempered 검사가 이 계약을 보증한다
            timed = provider.timed_transcript(
                binding=binding,
                chunk=chunk,
                settings=self.plan.workflow,
                on=self.on,
                stem=chunk.path.stem,
                source=self.plan.video.source_id,
                temperature=cfg.transcribe_temperature,
            )
            self.state.evidence.append(
                _evidence(
                    request_id, capabilities.ROLE_TIMING, chunk.index, timed.binding, timed.response
                )
            )
            responses.append(
                ChunkResponse(
                    index=chunk.index, offset=chunk.offset, payload=dict(timed.response or {})
                )
            )
        return responses

    def _arbitrate(self, job: TranscriptionJob) -> list[ChunkResponse]:
        responses: list[ChunkResponse] = []
        for chunk in job.chunks:
            _cancel(self.cancellation)
            request_id = f"{self.plan.run_id}:arbiter:{chunk.index:04d}"
            binding = self._binding_for(chunk, request_id)
            self.ledger.charge(_chunk_seconds(chunk), capabilities.ROLE_TEXT)
            text = self.text.text_transcript(
                binding=binding,
                chunk=chunk,
                settings=self.plan.workflow,
                on=self.on,
                stem=chunk.path.stem,
                source=self.plan.video.source_id,
            )
            self.state.evidence.append(
                _evidence(
                    request_id, capabilities.ROLE_TEXT, chunk.index, text.binding, text.response
                )
            )
            responses.append(
                ChunkResponse(
                    index=chunk.index, offset=chunk.offset, payload=dict(text.response or {})
                )
            )
        return responses

    def _binding_for(self, chunk: Chunk, request_id: str) -> AudioChunkBinding:
        rate = self.plan.video.audio.sample_rate
        return audio_chunk_binding(
            chunk,
            sample_start=max(0, round(chunk.offset * rate)),
            sample_end=max(1, round((chunk.offset + chunk.duration) * rate)),
            sample_rate=rate,
            request_id=request_id,
        )


def _span_seconds(plan: CaptionRunPlan, index: int) -> Fraction:
    """청크 하나의 길이 — 유리수로 잰 다음 Decimal 요금과 잇는다."""
    span = plan.spans[index]
    return Fraction(span.end - span.start, plan.video.audio.sample_rate)


def _chunk_seconds(chunk: Chunk) -> Fraction:
    """재전사 조각의 길이 — 조각 메타에서 온 값이다."""
    return Fraction(str(chunk.duration))


def _repair_pass(
    plan: CaptionRunPlan,
    on: date,
    workdir: Path,
    timing: TimingProvider,
    text: TextTranscriptProvider,
    ledger: _Ledger,
    cancellation: Callable[[], None] | None,
    state: _RunState,
) -> None:
    """엔진 되풀이 재전사를 얹고 골격을 얼린다 — **이 뒤로 골격은 불변이다**(규칙 2)."""
    draft = _draft_spine(plan, state)
    adapter = _RescueTranscriber(
        plan=plan,
        on=on,
        timing=timing,
        text=text,
        ledger=ledger,
        cancellation=cancellation,
        state=state,
    )
    source = repair.Source(
        stem=plan.run_id,
        duration=float(plan.video.extent),
        cutter=pcm_span_cutter(plan.video, workdir / "repair"),
    )
    try:
        state.settled = repair.settle(draft, source, plan.engine, adapter)
    except Exception as exc:  # noqa: BLE001 - 재전사가 죽어도 자막은 1차 골격으로 나온다
        state.repair_failure = f"{type(exc).__name__}: {exc}"
        log.warn(f"되풀이 재전사가 실패해 1차 골격으로 둔다: {state.repair_failure}")
        state.findings.append(
            QualityFinding(severity="error", code="repair_failed", message=state.repair_failure)
        )
        state.settled = repair.Settled(spine=tuple(draft), repairs=())


def _draft_spine(plan: CaptionRunPlan, state: _RunState) -> list[Segment]:
    """시간 응답들에서 초안 골격을 만든다 — 오프셋 덧셈이 시각에 가해지는 전부다.

    엔진 `transcribe.parse_segments`를 그대로 쓴다 — 압축률 등 되풀이 신호 메타까지
    그대로 옮겨져 기존 검출·복구가 같은 입력으로 돈다.
    """
    draft: list[Segment] = []
    for index in sorted(state.timing):
        outcome = state.timing[index]
        if outcome.transcript is None:
            continue
        offset = float(plan.video.chunk_time(outcome.span))
        payload = dict(outcome.transcript.response or {})
        draft.extend(transcribe.parse_segments(payload, offset, len(draft)))
    return draft


# ─────────────────────────────────────────────────────────────
#  글자 교정 — 세그먼트 게이트를 통과한 글자만
# ─────────────────────────────────────────────────────────────


def _plan_retext(plan: CaptionRunPlan, state: _RunState) -> RetextRecord:
    """문구 응답을 골격에 대응시켜 교체 계획만 세운다 — 큐를 만들지 않는다(D37).

    실패한 문구 청크는 **빈 문구**로 들어간다 — 게이트가 그 조각 전체를 반려하므로
    글자는 시간 골격의 것이 그대로 나가고, 이웃 조각의 몫이 새어 들어가지 않는다.
    """
    if not plan.engine.retext_enabled:
        return retext.switched_off()
    chunks: list[retext.ChunkText] = []
    for index in sorted(state.texts):
        outcome = state.texts[index]
        chunks.append(
            retext.ChunkText(
                index=index,
                offset=float(plan.video.chunk_time(outcome.span)),
                text=outcome.transcript.text if outcome.transcript else "",
            )
        )
    if not chunks:
        return retext.unavailable("문구 응답이 하나도 없다")
    settled = state.settled
    assert settled is not None  # _repair_pass가 항상 먼저 돈다
    return retext.plan(
        settled.spine,
        chunks,
        float(plan.video.extent),
        settled.repairs,
        plan.engine,
    )


# ─────────────────────────────────────────────────────────────
#  큐 조립 — 시각은 환산·자르기만, 글자는 채택만
# ─────────────────────────────────────────────────────────────


def _span_index_of(plan: CaptionRunPlan, normalized: Fraction | float) -> int:
    """정규화 시각이 어느 청크인지 — 청크 배정은 곧 근거 배정이다(정확 유리수 비교)."""
    offsets = [plan.video.chunk_time(span) for span in plan.spans]
    return max(0, bisect_right(offsets, Fraction(normalized)) - 1)


def _build_cues(
    plan: CaptionRunPlan, state: _RunState, record: RetextRecord
) -> tuple[CaptionCue, ...]:
    """골격 세그먼트마다 큐 하나 — 최종 시각 환산을 **한 번만** 적용한다(§11.6).

    큐 분할·병합·시간 이동은 하지 않는다. 세그먼트 하나가 곧 근거 있는 큐 하나다 —
    시간 근거 없는 균등 분할은 이 계층이 아니라 검토 UI의 몫이다(§11.7). 최종
    영상 범위를 벗어난 시각은 경계로 자르고 **그대로 보고한다** — 지어내지 않는다.
    """
    settled = state.settled
    assert settled is not None
    texts = dict(record.texts)
    rejections = {item.seg_id: item for item in record.rejections}
    excluded = {seg_id for item in record.excluded for seg_id in item.seg_ids}
    extent = Fraction(str(max(plan.video.video_duration, 0.0)))
    cues: list[CaptionCue] = []
    for segment in settled.spine:
        if not segment.text.strip():
            continue  # 글자 없는 세그먼트는 큐를 세우지 않는다(엔진 drop_empty와 같은 선)
        span_index = _span_index_of(plan, segment.start)
        timing_outcome = state.timing.get(span_index)
        text_outcome = state.texts.get(span_index)
        start = plan.video.to_final_time(segment.start)
        end = plan.video.to_final_time(segment.end)
        flags: list[str] = []
        if start < 0:
            flags.append(CUE_CLIPPED_START)
            start = Fraction(0)
        if end > extent:
            flags.append(CUE_CLIPPED_END)
            end = extent
        if start >= extent or end <= 0 or end < start:
            flags.append(CUE_OUT_OF_FINAL)
        if end <= start:
            flags.append(CUE_ZERO_EXTENT)
        cues.append(
            _cue_of(
                plan,
                segment,
                start,
                end,
                tuple(flags),
                texts,
                rejections,
                excluded,
                timing_outcome,
                text_outcome,
            )
        )
    return tuple(cues)


def _cue_of(
    plan: CaptionRunPlan,
    segment: Segment,
    start: Fraction,
    end: Fraction,
    flags: tuple[str, ...],
    texts: dict[int, str],
    rejections: Mapping[int, RetextRejection],
    excluded: set[int],
    timing_outcome: _TimingOutcome | None,
    text_outcome: _TextOutcome | None,
) -> CaptionCue:
    """세그먼트 하나의 큐 — 채택 글자·근거·교정 상태를 함께 실는다(§11.5)."""
    accepted = texts.get(segment.id)
    correction, source, retexted = _correction_of(
        segment, accepted, rejections, excluded, text_outcome
    )
    if retexted:
        flags = (*flags, CUE_RETEXTED)
    text_evidence = (
        ()
        if text_outcome is None or text_outcome.transcript is None
        else (text_outcome.request_id,)
    )
    return CaptionCue(
        start=start,
        end=end,
        text=accepted if accepted is not None else segment.text,
        whisper_text=segment.text,
        timing_segment_ids=(segment.id,),
        timing_evidence_id=timing_outcome.request_id if timing_outcome else "",
        text_evidence_ids=text_evidence,
        text_source=source,
        timing_source=TIMING_SOURCE,
        correction_status=correction,
        flags=flags,
    )


def _correction_of(
    segment: Segment,
    accepted: str | None,
    rejections: Mapping[int, RetextRejection],
    excluded: set[int],
    text_outcome: _TextOutcome | None,
) -> tuple[str, str, bool]:
    """교정 상태 판정 — 채택·제외·불통과·응답 없음·골격 그대로를 갈라 말한다(§11.5)."""
    if accepted is not None:
        # 같은 글자는 갈아낸 것이 아니다(엔진 apply_retext와 같은 선)
        return CORRECTION_ACCEPTED, TEXT_SOURCE_RETEXT, accepted != segment.text
    if segment.id in excluded:
        return CORRECTION_EXCLUDED, TEXT_SOURCE_WHISPER, False
    if text_outcome is not None and text_outcome.transcript is None:
        return CORRECTION_UNAVAILABLE, TEXT_SOURCE_WHISPER, False
    if segment.id in rejections:
        return CORRECTION_REJECTED, TEXT_SOURCE_WHISPER, False
    return CORRECTION_NONE, TEXT_SOURCE_WHISPER, False


def _cue_findings(plan: CaptionRunPlan, cue: CaptionCue) -> list[QualityFinding]:
    """큐 하나의 검토 항목 — 범위 이탈·짧은 표시·읽기 속도. 고치지 않고 보고만 한다."""
    seg_id = cue.timing_segment_ids[0] if cue.timing_segment_ids else None
    duration = float(cue.end - cue.start)
    found: list[QualityFinding] = []
    if CUE_ZERO_EXTENT in cue.flags or CUE_OUT_OF_FINAL in cue.flags:
        found.append(
            QualityFinding(
                "warn", "out_of_final_range", "큐가 최종 영상 범위를 벗어났다", seg_id=seg_id
            )
        )
    if duration < plan.engine.min_duration:
        found.append(
            QualityFinding("warn", "short_display", f"표시가 짧다: {duration:.2f}초", seg_id=seg_id)
        )
    cps = len(cue.text) / duration if duration > 0 else 0.0  # 길이 0은 위 항목이 말한다
    if cps > plan.settings.reading_speed_max_cps:
        found.append(
            QualityFinding(
                "warn", "reading_speed", f"읽기 속도가 빠르다: {cps:.1f}자/초", seg_id=seg_id
            )
        )
    return found


def _quality_findings(
    plan: CaptionRunPlan, cues: Sequence[CaptionCue], record: RetextRecord
) -> tuple[QualityFinding, ...]:
    """읽기 속도·짧은 표시·겹침·범위 이탈·미대응 — 경고만 만든다(§11.7·§11.11)."""
    findings: list[QualityFinding] = []
    for cue in cues:
        findings.extend(_cue_findings(plan, cue))
    for earlier, later in zip(cues, cues[1:], strict=False):
        if earlier.end > later.start:
            findings.append(
                QualityFinding(
                    "warn",
                    "overlap",
                    f"큐가 겹친다: {float(earlier.end):.2f}초 > {float(later.start):.2f}초",
                    seg_id=later.timing_segment_ids[0] if later.timing_segment_ids else None,
                )
            )
    for item in record.unplaced:
        findings.append(
            QualityFinding(
                "warn",
                "unmapped_text",
                f"대응되지 않은 문구 {item.chars}자 — 가짜 cue를 만들지 않는다",
                chunk_index=item.chunk_index,
            )
        )
    for hold in record.excluded:
        findings.append(
            QualityFinding(
                "warn",
                "repair_excluded",
                f"회복 못 한 되풀이({hold.outcome})는 교정하지 않는다 —"
                f" 문구 글자 {hold.gpt_chars}자",
            )
        )
    return tuple(findings)


def _verify_rows(
    plan: CaptionRunPlan, state: _RunState, cues: Sequence[CaptionCue], record: RetextRecord
) -> tuple[VerifyRow, ...]:
    """검증 표시 — 이미 받은 문구 응답과 채택 큐의 대조(규칙 5: 표시만 한다).

    문구 모델을 다시 부르지 않는다. 교정이 반영된 큐는 원문 골격과 비교해
    낮게 나올 수 있는데, 그 차이는 alignment 기록이 이미 설명한다(D37).
    """
    rows: list[VerifyRow] = []
    for index in sorted(state.texts):
        outcome = state.texts[index]
        if outcome.transcript is None:
            continue
        adopted_words = "".join(
            cue.text for cue in cues if _cue_span_index(plan, cue) == index
        ).split()
        adopted_text = "".join(adopted_words)
        evidence = "".join(outcome.transcript.text.split())
        similarity = SequenceMatcher(None, adopted_text, evidence, autojunk=False).ratio()
        rows.append(
            VerifyRow(
                chunk_index=index,
                similarity=round(similarity, 4),
                evidence_chars=len(evidence),
                adopted_chars=len(adopted_text),
            )
        )
    return tuple(rows)


def _cue_span_index(plan: CaptionRunPlan, cue: CaptionCue) -> int:
    """큐가 어느 청크 몫인지 — 근거 배정용이지 시각 계산이 아니다."""
    normalized = cue.start - plan.video.audio.pcm_sample0_file_time
    return _span_index_of(plan, normalized)


# ─────────────────────────────────────────────────────────────
#  상태 판정·결과 조립
# ─────────────────────────────────────────────────────────────

_REVIEW_CODES = frozenset(
    {
        "out_of_final_range",
        "short_display",
        "reading_speed",
        "overlap",
        "unmapped_text",
        "repair_excluded",
        "rescue_temperature_unsupported",
        "empty_run_unconfirmed",
    }
)


def _decide_status(
    state: _RunState,
    cancelled_reason: str,
    record: RetextRecord,
    cues: tuple[CaptionCue, ...],
    findings: tuple[QualityFinding, ...],
    chunk_count: int,
) -> str:
    """다섯 상태는 다른 말이다(§11.11) — 좁은 쪽이 이긴다.

    `partial_failure`는 요청 하나라도 실패하면 선언된다 — 문구 없이 완성된 결과를
    최신 문구 반영 성공이라고 부르는 길이 없다(§11.3). 무발화 확정은 **전부**
    정상 응답했고 전부 비어 있을 때뿐이다(§11.11).
    """
    if cancelled_reason:
        return STATUS_CANCELLED
    failed = any(outcome.failure for outcome in state.timing.values()) or any(
        outcome.failure for outcome in state.texts.values()
    )
    missing = len(state.timing) < chunk_count or len(state.texts) < chunk_count
    if state.repair_failure or failed or missing:
        return STATUS_PARTIAL_FAILURE
    if record.status not in {RETEXT_APPLIED, RETEXT_OFF}:
        return STATUS_REVIEW_REQUIRED
    if record.rejections or record.unplaced or record.excluded:
        return STATUS_REVIEW_REQUIRED
    if any(item.code in _REVIEW_CODES for item in findings):
        return STATUS_REVIEW_REQUIRED
    if not cues:
        # 빈 응답이 모두 정상이라도 무발화의 **증명**은 아니다(§11.11) — 이 계층은
        # confirmed_no_speech를 스스로 확정하지 않는다. 그 확정은 사람 검토(A7-3)다.
        return STATUS_REVIEW_REQUIRED
    return STATUS_READY


def _all_explicitly_empty(state: _RunState) -> bool:
    """빈 결과가 무발화의 증명이 되려면 — 전부 정상 응답했고 전부 비어 있어야 한다."""
    timings = list(state.timing.values())
    texts = list(state.texts.values())
    if not timings or not texts:
        return False
    return all(
        outcome.transcript is not None and not outcome.transcript.segments for outcome in timings
    ) and all(
        outcome.transcript is not None and not outcome.transcript.text.strip() for outcome in texts
    )


def _speech_basis(state: _RunState, cues: tuple[CaptionCue, ...]) -> str:
    """큐가 없을 때의 근거를 기록으로 남긴다(§11.11).

    빈 초안이 무발화의 증명이 되려면 전부 정상 응답했고 전부 비어 있어야 한다 —
    그 아래의 빈 결과는 **미확인 빈 상태**로 소비 계층에 내려가야 한다.
    """
    if cues:
        return BASIS_CUES_PRESENT
    # 전부 정상 응답했고 전부 비어 있어도 미확인이다 — 근거의 강도만 기록으로 남긴다.
    return BASIS_UNCONFIRMED_EMPTY


def _chunk_records(plan: CaptionRunPlan, state: _RunState) -> tuple[ChunkRunRecord, ...]:
    records: list[ChunkRunRecord] = []
    for index in range(len(plan.spans)):
        timing = state.timing.get(index)
        text = state.texts.get(index)
        draft_segments = 0
        if timing is not None and timing.transcript is not None:
            draft_segments = len(timing.transcript.segments)
        records.append(
            ChunkRunRecord(
                chunk_index=index,
                sample_start=plan.spans[index].start,
                sample_end=plan.spans[index].end,
                wav_sha256=state.wav_sha256.get(index, ""),
                timing_request_id=timing.request_id if timing else "",
                text_request_id=text.request_id if text else "",
                timing_failure=timing.failure if timing else "생성되지 않았다",
                text_failure=text.failure if text else "생성되지 않았다",
                draft_segments=draft_segments,
                text_chars=len(text.transcript.text) if text and text.transcript else 0,
            )
        )
    return tuple(records)


def _assemble_result(
    plan: CaptionRunPlan, state: _RunState, *, cancelled_reason: str
) -> FinalCaptionResult:
    """불변 결과 조립 — 골격·수리·교정·증거·검토 항목을 한 번에 얼어붙인다."""
    if state.settled is None:  # 취소가 골격 얼리기보다 먼저 온 경우 — 1차 골격으로 둔다
        state.settled = repair.Settled(spine=tuple(_draft_spine(plan, state)), repairs=())
    record = _plan_retext(plan, state)
    cues = _build_cues(plan, state, record)
    findings = tuple(state.findings) + _quality_findings(plan, cues, record)
    if not cues:
        # 빈 결과는 그 자체로 검토 항목이다 — 무발화 확정은 사람 검토의 몫이다(§11.11).
        basis = (
            "모든 응답이 정상이고 비어 있다"
            if _all_explicitly_empty(state)
            else "일부 응답을 못 받았다"
        )
        findings = (
            *findings,
            QualityFinding("warn", "empty_run_unconfirmed", f"큐가 없다 — {basis}"),
        )
    status = _decide_status(state, cancelled_reason, record, cues, findings, len(plan.spans))
    return FinalCaptionResult(
        run_id=plan.run_id,
        status=status,
        fingerprint=plan.fingerprint,
        final_video_revision=plan.video.final_video_revision,
        cues=cues,
        spine=state.settled.spine,
        repairs=state.settled.repairs,
        retext=record,
        chunks=_chunk_records(plan, state),
        evidence=tuple(state.evidence),
        findings=findings,
        verify_rows=_verify_rows(plan, state, cues, record),
        speech_basis=_speech_basis(state, cues),
        cancellation_reason=cancelled_reason,
    )
