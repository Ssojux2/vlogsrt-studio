"""Workflow Extension과 Python 엔진 사이의 결정적 JSON Lines 계약.

이 모듈은 앱 경계의 메시지만 소유한다. 기존 편집 엔진의 ``edit.EditPlan``과
이름은 같지만 네임스페이스가 다르며, 그 객체를 직렬화하려고 추측하지 않는다.
모든 시각은 정확한 ``Fraction``이고 모든 리비전은 정준 JSON의 SHA-256이다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Literal, TypeAlias, cast

PROTOCOL_VERSION = "1.0"
ANALYSIS_PROTOCOL_VERSION = "1.2"
LEGACY_ANALYSIS_PROTOCOL_VERSION = "1.1"
CAPTION_PROTOCOL_VERSION = "1.1"
DEFAULT_REVIEW_CONFIDENCE = Decimal("0.90")

WorkflowState: TypeAlias = Literal[
    "received",
    "estimate_ready",
    "authorized",
    "analyzing",
    "review_required",
    "roughcut_imported",
    "caption_received",
    "caption_estimate_ready",
    "caption_authorized",
    "caption_ready",
    "cancelled",
    "failed",
]

_NEXT_STATE: dict[WorkflowState, WorkflowState] = {
    "received": "estimate_ready",
    "estimate_ready": "authorized",
    "authorized": "analyzing",
    "analyzing": "review_required",
    "review_required": "roughcut_imported",
    "roughcut_imported": "caption_received",
    "caption_received": "caption_estimate_ready",
    "caption_estimate_ready": "caption_authorized",
    "caption_authorized": "caption_ready",
}

_TERMINAL_STATES = frozenset({"cancelled", "failed"})


@dataclass(frozen=True, slots=True)
class RationalRange:
    start: Fraction
    end: Fraction

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"유효하지 않은 반개 구간이다: [{self.start}, {self.end})")


@dataclass(frozen=True, slots=True)
class ProjectRevision:
    input_sha256: str
    project_uid: str
    project_name: str
    frame_duration: Fraction
    duration: Fraction

    def __post_init__(self) -> None:
        _require_sha256(self.input_sha256, "입력 XML")
        if not self.project_uid:
            raise ValueError("프로젝트 UID가 비어 있다")
        if not self.project_name:
            raise ValueError("프로젝트 이름이 비어 있다")
        if self.frame_duration <= 0 or self.duration <= 0:
            raise ValueError("프레임 길이와 프로젝트 전체 길이는 양수여야 한다")

    @property
    def revision(self) -> str:
        return _digest(_project_revision_payload(self))


@dataclass(frozen=True, slots=True)
class ProjectSelection:
    """Final Cut 타임라인에 열린 프로젝트의 실시간 식별정보."""

    project_uid: str
    project_name: str
    timeline_start: Fraction
    duration: Fraction
    frame_duration: Fraction

    def __post_init__(self) -> None:
        if not self.project_uid or not self.project_name:
            raise ValueError("프로젝트 UID·이름은 비어 있을 수 없다")
        if self.timeline_start < 0 or self.duration <= 0 or self.frame_duration <= 0:
            raise ValueError("타임라인 시작은 0 이상, 길이·프레임 길이는 양수여야 한다")

    @property
    def selection_revision(self) -> str:
        return _digest(_project_selection_payload(self))


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    """승인에 묶이는 공식 Standard 공개 가격의 정규 스냅샷."""

    effective_date: str
    currency: str
    processing_region: str
    service_tier: str
    luna_model: str
    luna_input_per_million_tokens: str
    luna_output_per_million_tokens: str
    whisper_model: str
    whisper_per_minute: str
    gpt_transcribe_model: str
    gpt_transcribe_per_minute: str
    documentation_urls: tuple[str, ...]
    snapshot_revision: str

    @classmethod
    def create(
        cls,
        *,
        effective_date: str,
        currency: str,
        processing_region: str,
        service_tier: str,
        luna_model: str,
        luna_input_per_million_tokens: str,
        luna_output_per_million_tokens: str,
        whisper_model: str,
        whisper_per_minute: str,
        gpt_transcribe_model: str,
        gpt_transcribe_per_minute: str,
        documentation_urls: tuple[str, ...],
    ) -> PricingSnapshot:
        payload = _pricing_snapshot_payload(
            effective_date=effective_date,
            currency=currency,
            processing_region=processing_region,
            service_tier=service_tier,
            luna_model=luna_model,
            luna_input_per_million_tokens=luna_input_per_million_tokens,
            luna_output_per_million_tokens=luna_output_per_million_tokens,
            whisper_model=whisper_model,
            whisper_per_minute=whisper_per_minute,
            gpt_transcribe_model=gpt_transcribe_model,
            gpt_transcribe_per_minute=gpt_transcribe_per_minute,
            documentation_urls=documentation_urls,
        )
        return cls(
            effective_date=effective_date,
            currency=currency,
            processing_region=processing_region,
            service_tier=service_tier,
            luna_model=luna_model,
            luna_input_per_million_tokens=cast(str, payload["luna_input_per_million_tokens"]),
            luna_output_per_million_tokens=cast(str, payload["luna_output_per_million_tokens"]),
            whisper_model=whisper_model,
            whisper_per_minute=cast(str, payload["whisper_per_minute"]),
            gpt_transcribe_model=gpt_transcribe_model,
            gpt_transcribe_per_minute=cast(str, payload["gpt_transcribe_per_minute"]),
            documentation_urls=documentation_urls,
            snapshot_revision=_digest(payload),
        )

    def __post_init__(self) -> None:
        _require_sha256(self.snapshot_revision, "가격 스냅샷 리비전")
        for name in (
            "effective_date",
            "currency",
            "processing_region",
            "service_tier",
            "luna_model",
            "whisper_model",
            "gpt_transcribe_model",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name}은 비어 있을 수 없다")
        if not self.documentation_urls or any(not value for value in self.documentation_urls):
            raise ValueError("가격 스냅샷에는 공식 문서 URL이 필요하다")
        for name in (
            "luna_input_per_million_tokens",
            "luna_output_per_million_tokens",
            "whisper_per_minute",
            "gpt_transcribe_per_minute",
        ):
            if _decimal(getattr(self, name), name) < 0:
                raise ValueError(f"{name}은 0 이상이어야 한다")


@dataclass(frozen=True, slots=True)
class AnalysisEstimate:
    input_revision: str
    model: str
    unique_source_audio_seconds: Fraction
    transcription_seconds: Fraction
    max_windows: int
    max_images: int
    max_image_tokens: int
    cost_upper_bound: str | None
    settings_sha256: str
    pricing_snapshot_revision: str
    media_grant_revision: str
    estimate_revision: str

    @classmethod
    def create(
        cls,
        *,
        input_revision: str,
        model: str,
        unique_source_audio_seconds: Fraction,
        transcription_seconds: Fraction,
        max_windows: int,
        max_images: int,
        max_image_tokens: int,
        cost_upper_bound: str | None,
        settings_sha256: str,
        pricing_snapshot_revision: str,
        media_grant_revision: str,
    ) -> AnalysisEstimate:
        payload = _analysis_estimate_payload(
            input_revision=input_revision,
            model=model,
            unique_source_audio_seconds=unique_source_audio_seconds,
            transcription_seconds=transcription_seconds,
            max_windows=max_windows,
            max_images=max_images,
            max_image_tokens=max_image_tokens,
            cost_upper_bound=cost_upper_bound,
            settings_sha256=settings_sha256,
            pricing_snapshot_revision=pricing_snapshot_revision,
            media_grant_revision=media_grant_revision,
        )
        return cls(
            input_revision=input_revision,
            model=model,
            unique_source_audio_seconds=unique_source_audio_seconds,
            transcription_seconds=transcription_seconds,
            max_windows=max_windows,
            max_images=max_images,
            max_image_tokens=max_image_tokens,
            cost_upper_bound=cast(str | None, payload["cost_upper_bound"]),
            settings_sha256=settings_sha256,
            pricing_snapshot_revision=pricing_snapshot_revision,
            media_grant_revision=media_grant_revision,
            estimate_revision=_digest(payload),
        )

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.settings_sha256, "분석 설정")
        _require_sha256(self.pricing_snapshot_revision, "가격 스냅샷 리비전")
        _require_sha256(self.media_grant_revision, "미디어 권한 리비전")
        _require_sha256(self.estimate_revision, "비용 추정 리비전")
        if not self.model:
            raise ValueError("분석 모델이 비어 있다")
        if self.unique_source_audio_seconds < 0 or self.transcription_seconds < 0:
            raise ValueError("고유 소스·전사 길이는 0 이상이어야 한다")
        if self.transcription_seconds < self.unique_source_audio_seconds:
            raise ValueError("전사 상한이 고유 소스 길이보다 짧다")
        for name in ("max_windows", "max_images", "max_image_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name}는 0 이상 정수여야 한다")
        if self.max_images > self.max_windows * 6:
            raise ValueError("이미지 상한이 창당 6장 상한을 넘는다")
        if self.cost_upper_bound is not None:
            cost = _decimal(self.cost_upper_bound, "비용 상한")
            if cost < 0:
                raise ValueError("비용 상한은 0 이상이어야 한다")


@dataclass(frozen=True, slots=True)
class AnalysisAuthorization:
    input_revision: str
    estimate_revision: str
    pricing_snapshot_revision: str
    media_grant_revision: str
    authorized_at: str

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.estimate_revision, "비용 추정 리비전")
        _require_sha256(self.pricing_snapshot_revision, "가격 스냅샷 리비전")
        _require_sha256(self.media_grant_revision, "미디어 권한 리비전")
        if not self.authorized_at:
            raise ValueError("승인 시각이 비어 있다")

    def require_matches(
        self,
        input_revision: str,
        estimate_revision: str,
        pricing_snapshot_revision: str,
        media_grant_revision: str,
    ) -> None:
        if self.input_revision != input_revision:
            raise ValueError("승인의 입력 리비전이 현재 입력과 다르다")
        if self.estimate_revision != estimate_revision:
            raise ValueError("승인의 추정 리비전이 현재 비용 추정과 다르다")
        if self.pricing_snapshot_revision != pricing_snapshot_revision:
            raise ValueError("승인의 가격 스냅샷이 현재 가격과 다르다")
        if self.media_grant_revision != media_grant_revision:
            raise ValueError("승인의 미디어 권한 리비전이 현재 권한과 다르다")


AnalysisAttemptStatus: TypeAlias = Literal["pending", "running", "succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class AnalysisAttempt:
    input_revision: str
    estimate_revision: str
    attempt_id: str
    status: AnalysisAttemptStatus
    started_at: str
    error: str | None

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.estimate_revision, "비용 추정 리비전")
        if not self.attempt_id or not self.started_at:
            raise ValueError("시도 ID·시작 시각은 비어 있을 수 없다")
        if self.status not in {"pending", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError(f"알 수 없는 분석 시도 상태다: {self.status!r}")
        if self.status == "failed" and not self.error:
            raise ValueError("실패한 분석 시도에는 오류가 필요하다")
        if self.status != "failed" and self.error is not None:
            raise ValueError("실패가 아닌 분석 시도는 오류를 가질 수 없다")


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    input_revision: str
    attempt_id: str
    requested_at: str
    reason: Literal["user", "active_project_changed"]
    cancellation_revision: str

    @classmethod
    def create(
        cls,
        *,
        input_revision: str,
        attempt_id: str,
        requested_at: str,
        reason: Literal["user", "active_project_changed"],
    ) -> CancellationRequest:
        payload = {
            "attempt_id": attempt_id,
            "input_revision": input_revision,
            "reason": reason,
            "requested_at": requested_at,
        }
        return cls(input_revision, attempt_id, requested_at, reason, _digest(payload))

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.cancellation_revision, "취소 리비전")
        if not self.attempt_id or not self.requested_at:
            raise ValueError("취소 시도 ID·요청 시각은 비어 있을 수 없다")
        if self.reason not in {"user", "active_project_changed"}:
            raise ValueError(f"알 수 없는 취소 사유다: {self.reason!r}")


@dataclass(frozen=True, slots=True)
class ActualUsage:
    input_revision: str
    attempt_id: str
    window_id: str | None
    model: str
    input_tokens: int
    output_tokens: int
    audio_seconds: Fraction
    cost: str
    usage_revision: str

    @classmethod
    def create(
        cls,
        *,
        input_revision: str,
        attempt_id: str,
        window_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        audio_seconds: Fraction = Fraction(),
        cost: str,
    ) -> ActualUsage:
        payload = _actual_usage_payload(
            input_revision=input_revision,
            attempt_id=attempt_id,
            window_id=window_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            audio_seconds=audio_seconds,
            cost=cost,
        )
        return cls(
            input_revision,
            attempt_id,
            window_id,
            model,
            input_tokens,
            output_tokens,
            audio_seconds,
            cast(str, payload["cost"]),
            _digest(payload),
        )

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.usage_revision, "사용량 리비전")
        if not self.attempt_id or not self.model:
            raise ValueError("사용량의 시도 ID·모델은 비어 있을 수 없다")
        if min(self.input_tokens, self.output_tokens) < 0 or self.audio_seconds < 0:
            raise ValueError("토큰·오디오 사용량은 0 이상이어야 한다")
        if _decimal(self.cost, "실제 비용") < 0:
            raise ValueError("실제 비용은 0 이상이어야 한다")


@dataclass(frozen=True, slots=True)
class VisionWindowDecision:
    input_revision: str
    window_id: str
    choice: Literal["keep", "propose_cut"]
    candidate_id: str | None
    evidence_frame_ids: tuple[str, ...]
    confidence: str
    response_sha256: str
    decision_revision: str

    @classmethod
    def create(
        cls,
        *,
        input_revision: str,
        window_id: str,
        choice: Literal["keep", "propose_cut"],
        candidate_id: str | None,
        evidence_frame_ids: tuple[str, ...],
        confidence: str,
        response_sha256: str,
    ) -> VisionWindowDecision:
        payload = _vision_decision_payload(
            input_revision=input_revision,
            window_id=window_id,
            choice=choice,
            candidate_id=candidate_id,
            evidence_frame_ids=evidence_frame_ids,
            confidence=confidence,
            response_sha256=response_sha256,
        )
        return cls(
            input_revision=input_revision,
            window_id=window_id,
            choice=choice,
            candidate_id=candidate_id,
            evidence_frame_ids=evidence_frame_ids,
            confidence=cast(str, payload["confidence"]),
            response_sha256=response_sha256,
            decision_revision=_digest(payload),
        )

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.response_sha256, "모델 응답")
        _require_sha256(self.decision_revision, "비전 판정 리비전")
        if not self.window_id:
            raise ValueError("창 ID가 비어 있다")
        if self.choice == "keep" and self.candidate_id is not None:
            raise ValueError("keep 판정은 후보 ID를 가질 수 없다")
        if self.choice == "propose_cut" and not self.candidate_id:
            raise ValueError("propose_cut 판정에는 후보 ID가 필요하다")
        if self.choice == "propose_cut" and not self.evidence_frame_ids:
            raise ValueError("propose_cut 판정에는 근거 프레임 ID가 하나 이상 필요하다")
        confidence_value = _confidence(self.confidence)
        if not confidence_value.is_finite() or not 0 <= confidence_value <= 1:
            raise ValueError("신뢰도는 유한한 0~1이어야 한다")


@dataclass(frozen=True, slots=True)
class CutCandidate:
    candidate_id: str
    input_revision: str
    clip_instance_id: str
    source_range: RationalRange
    timeline_range: RationalRange
    reason: str
    evidence: tuple[str, ...]
    confidence: str
    window_id: str
    decision_revision: str

    @classmethod
    def create(
        cls,
        *,
        input_revision: str,
        clip_instance_id: str,
        source_range: RationalRange,
        timeline_range: RationalRange,
        reason: str,
        evidence: tuple[str, ...],
        confidence: str,
        window_id: str,
        decision_revision: str,
    ) -> CutCandidate:
        return cls(
            candidate_id=cls.identity(
                input_revision=input_revision,
                clip_instance_id=clip_instance_id,
                source_range=source_range,
                timeline_range=timeline_range,
                reason=reason,
                evidence=evidence,
                window_id=window_id,
            ),
            input_revision=input_revision,
            clip_instance_id=clip_instance_id,
            source_range=source_range,
            timeline_range=timeline_range,
            reason=reason,
            evidence=evidence,
            confidence=confidence,
            window_id=window_id,
            decision_revision=decision_revision,
        )

    @staticmethod
    def identity(
        *,
        input_revision: str,
        clip_instance_id: str,
        source_range: RationalRange,
        timeline_range: RationalRange,
        reason: str,
        evidence: tuple[str, ...],
        window_id: str,
    ) -> str:
        # evidence는 Luna가 근거 프레임을 더하면서 풍부해질 수 있다. 모델이 고르는
        # 안정 후보 ID는 호출 전후 같아야 하므로 신원은 기하·사유·창까지만 묶는다.
        _ = evidence
        return _digest_sequence(
            (
                input_revision,
                clip_instance_id,
                _fraction_text(source_range.start),
                _fraction_text(source_range.end),
                _fraction_text(timeline_range.start),
                _fraction_text(timeline_range.end),
                reason,
                window_id,
            )
        )

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        if not self.candidate_id or not self.clip_instance_id or not self.reason:
            raise ValueError("후보 ID·클립 인스턴스·사유는 비어 있을 수 없다")
        if not self.window_id:
            raise ValueError("후보의 창 ID가 비어 있다")
        _require_sha256(self.decision_revision, "비전 판정 리비전")
        value = _confidence(self.confidence)
        if value < 0 or value > 1:
            raise ValueError(f"신뢰도는 0~1이어야 한다: {self.confidence!r}")

    @property
    def selected_by_default(self) -> bool:
        return _confidence(self.confidence) >= DEFAULT_REVIEW_CONFIDENCE


@dataclass(frozen=True, slots=True)
class CandidatePreview:
    candidate_id: str
    input_revision: str
    frame_id: str
    thumbnail_path: Path
    thumbnail_sha256: str
    reason: str
    confidence: str
    removal_duration: Fraction
    preview_revision: str

    @classmethod
    def create(
        cls,
        *,
        candidate_id: str,
        input_revision: str,
        frame_id: str,
        thumbnail_path: Path,
        thumbnail_sha256: str,
        reason: str,
        confidence: str,
        removal_duration: Fraction,
    ) -> CandidatePreview:
        payload = _candidate_preview_payload(
            candidate_id=candidate_id,
            input_revision=input_revision,
            frame_id=frame_id,
            thumbnail_path=thumbnail_path,
            thumbnail_sha256=thumbnail_sha256,
            reason=reason,
            confidence=confidence,
            removal_duration=removal_duration,
        )
        return cls(
            candidate_id,
            input_revision,
            frame_id,
            thumbnail_path,
            thumbnail_sha256,
            reason,
            cast(str, payload["confidence"]),
            removal_duration,
            _digest(payload),
        )

    def __post_init__(self) -> None:
        _require_sha256(self.candidate_id, "후보 ID")
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.thumbnail_sha256, "후보 미리보기")
        _require_sha256(self.preview_revision, "미리보기 리비전")
        if not self.frame_id or not self.reason or self.removal_duration <= 0:
            raise ValueError("미리보기 프레임·사유는 필요하고 제거 길이는 양수여야 한다")
        value = _confidence(self.confidence)
        if value < 0 or value > 1:
            raise ValueError("미리보기 신뢰도는 0~1이어야 한다")


@dataclass(frozen=True, slots=True)
class EditPlan:
    input_revision: str
    approved_candidate_ids: tuple[str, ...]
    approved_decision_revisions: tuple[tuple[str, str], ...]
    rejected_candidate_ids: tuple[str, ...]
    expected_output_duration: Fraction
    plan_revision: str

    @classmethod
    def create(
        cls,
        *,
        input_revision: str,
        approved_candidate_ids: tuple[str, ...],
        rejected_candidate_ids: tuple[str, ...],
        expected_output_duration: Fraction,
        approved_decision_revisions: tuple[tuple[str, str], ...] = (),
    ) -> EditPlan:
        _require_sha256(input_revision, "입력 리비전")
        approved = tuple(sorted(set(approved_candidate_ids)))
        revisions = tuple(sorted(approved_decision_revisions))
        if {candidate_id for candidate_id, _revision in revisions} != set(approved):
            raise ValueError("승인 후보와 판정 리비전 집합이 다르다")
        for _candidate_id, decision_revision in revisions:
            _require_sha256(decision_revision, "비전 판정 리비전")
        rejected = tuple(sorted(set(rejected_candidate_ids)))
        overlap = sorted(set(approved) & set(rejected))
        if overlap:
            raise ValueError(f"후보가 승인과 거절에 동시에 들어 있다: {overlap}")
        if expected_output_duration <= 0:
            raise ValueError("예상 출력 길이는 양수여야 한다")
        payload = {
            "approved_candidate_ids": list(approved),
            "approved_decision_revisions": [list(row) for row in revisions],
            "expected_output_duration": _fraction_text(expected_output_duration),
            "input_revision": input_revision,
            "rejected_candidate_ids": list(rejected),
        }
        return cls(
            input_revision=input_revision,
            approved_candidate_ids=approved,
            approved_decision_revisions=revisions,
            rejected_candidate_ids=rejected,
            expected_output_duration=expected_output_duration,
            plan_revision=_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class CutResult:
    plan_revision: str
    input_revision: str
    output_fcpxml: Path
    review_report: Path

    def __post_init__(self) -> None:
        _require_sha256(self.plan_revision, "계획 리비전")
        _require_sha256(self.input_revision, "입력 리비전")


@dataclass(frozen=True, slots=True)
class CaptionRequest:
    origin_revision: str
    final_revision: str
    render_audio: Path
    render_audio_sha256: str
    final_fcpxml: Path
    insertion_method: Literal["relative"] = "relative"

    def __post_init__(self) -> None:
        _require_sha256(self.origin_revision, "원본 워크플로 리비전")
        _require_sha256(self.final_revision, "최종 FCPXML 리비전")
        _require_sha256(self.render_audio_sha256, "렌더 오디오")
        if self.insertion_method != "relative":
            raise ValueError("자막 삽입 방식은 relative만 지원한다")

    @property
    def request_revision(self) -> str:
        return _digest(
            {
                "final_revision": self.final_revision,
                "origin_revision": self.origin_revision,
                "render_audio_sha256": self.render_audio_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class CaptionEstimate:
    request_revision: str
    origin_revision: str
    final_revision: str
    render_audio_sha256: str
    audio_seconds: Fraction
    transcription_seconds: Fraction
    cost_upper_bound: str
    pricing_snapshot_revision: str
    settings_sha256: str
    estimate_revision: str

    @classmethod
    def create(
        cls,
        *,
        request_revision: str,
        origin_revision: str,
        final_revision: str,
        render_audio_sha256: str,
        audio_seconds: Fraction,
        transcription_seconds: Fraction,
        cost_upper_bound: str,
        pricing_snapshot_revision: str,
        settings_sha256: str,
    ) -> CaptionEstimate:
        payload = _caption_estimate_payload(
            request_revision=request_revision,
            origin_revision=origin_revision,
            final_revision=final_revision,
            render_audio_sha256=render_audio_sha256,
            audio_seconds=audio_seconds,
            transcription_seconds=transcription_seconds,
            cost_upper_bound=cost_upper_bound,
            pricing_snapshot_revision=pricing_snapshot_revision,
            settings_sha256=settings_sha256,
        )
        return cls(
            request_revision,
            origin_revision,
            final_revision,
            render_audio_sha256,
            audio_seconds,
            transcription_seconds,
            cast(str, payload["cost_upper_bound"]),
            pricing_snapshot_revision,
            settings_sha256,
            _digest(payload),
        )

    def __post_init__(self) -> None:
        for value, label in (
            (self.request_revision, "자막 요청 리비전"),
            (self.origin_revision, "원본 리비전"),
            (self.final_revision, "최종 리비전"),
            (self.render_audio_sha256, "렌더 오디오"),
            (self.pricing_snapshot_revision, "가격 스냅샷"),
            (self.settings_sha256, "자막 설정"),
            (self.estimate_revision, "자막 추정"),
        ):
            _require_sha256(value, label)
        if self.audio_seconds <= 0 or self.transcription_seconds < self.audio_seconds:
            raise ValueError("자막 오디오 길이는 양수이고 전사 상한 이하여야 한다")
        if _decimal(self.cost_upper_bound, "자막 비용 상한") < 0:
            raise ValueError("자막 비용 상한은 0 이상이어야 한다")


@dataclass(frozen=True, slots=True)
class CaptionAuthorization:
    request_revision: str
    estimate_revision: str
    pricing_snapshot_revision: str
    authorized_at: str

    def __post_init__(self) -> None:
        _require_sha256(self.request_revision, "자막 요청 리비전")
        _require_sha256(self.estimate_revision, "자막 추정 리비전")
        _require_sha256(self.pricing_snapshot_revision, "가격 스냅샷 리비전")
        if not self.authorized_at:
            raise ValueError("자막 승인 시각이 비어 있다")

    def require_matches(self, estimate: CaptionEstimate) -> None:
        if self.request_revision != estimate.request_revision:
            raise ValueError("자막 승인의 요청 리비전이 현재 요청과 다르다")
        if self.estimate_revision != estimate.estimate_revision:
            raise ValueError("자막 승인의 비용 추정이 현재 추정과 다르다")
        if self.pricing_snapshot_revision != estimate.pricing_snapshot_revision:
            raise ValueError("자막 승인의 가격 스냅샷이 현재 가격과 다르다")


@dataclass(frozen=True, slots=True)
class CaptionResult:
    final_revision: str
    srt_path: Path | None
    warnings: tuple[str, ...]
    quality: tuple[str, ...]
    speech_status: Literal["speech", "no_speech", "error"]

    def __post_init__(self) -> None:
        _require_sha256(self.final_revision, "최종 FCPXML 리비전")
        if self.speech_status == "speech" and self.srt_path is None:
            raise ValueError("발화가 있으면 SRT 경로가 필요하다")
        if self.speech_status != "speech" and self.srt_path is not None:
            raise ValueError("발화 없음 또는 오류 결과는 SRT 경로를 가질 수 없다")


@dataclass(frozen=True, slots=True)
class WorkflowStateEvent:
    input_revision: str
    state: WorkflowState

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")


ProtocolMessage: TypeAlias = (
    ProjectRevision
    | ProjectSelection
    | PricingSnapshot
    | AnalysisEstimate
    | AnalysisAuthorization
    | AnalysisAttempt
    | CancellationRequest
    | ActualUsage
    | VisionWindowDecision
    | CutCandidate
    | CandidatePreview
    | EditPlan
    | CutResult
    | CaptionRequest
    | CaptionEstimate
    | CaptionAuthorization
    | CaptionResult
    | WorkflowStateEvent
)


def encode_message(message: ProtocolMessage) -> str:
    """메시지 하나를 정준 JSONL 한 줄로 만든다."""
    payload = _message_payload(message)
    envelope = {
        "payload": payload,
        "protocol_version": _protocol_version(message),
        "type": type(message).__name__,
    }
    return _canonical_json(envelope) + "\n"


def decode_message(line: str) -> ProtocolMessage:
    """정준 여부와 무관하게 한 JSON 객체를 읽되 버전·타입·리비전은 엄격히 검사한다."""
    if not line.endswith("\n") or line.count("\n") != 1:
        raise ValueError("JSONL 메시지는 정확히 한 줄이고 줄바꿈으로 끝나야 한다")
    try:
        envelope = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSONL 메시지를 해석할 수 없다: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("JSONL 메시지 최상위 값은 객체여야 한다")
    kind = envelope.get("type")
    version = envelope.get("protocol_version")
    expected_version = _protocol_version_for_kind(kind)
    if version != expected_version:
        raise ValueError(f"지원하지 않는 프로토콜 버전이다: {version!r}")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("JSONL 메시지 payload는 객체여야 한다")
    return _decode_payload(kind, payload)


class WorkflowJournal:
    """하나의 입력 리비전에 묶인 append-only 상태 원장."""

    def __init__(self, path: Path, revision: ProjectRevision, state: WorkflowState) -> None:
        self.path = path
        self.revision = revision
        self.state = state

    @classmethod
    def create(cls, path: Path, revision: ProjectRevision) -> WorkflowJournal:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encode_message(revision))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise ValueError(f"워크플로 원장이 이미 있다: {path}") from exc
        return cls(path, revision, "received")

    @classmethod
    def open(cls, path: Path) -> WorkflowJournal:
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except FileNotFoundError as exc:
            raise ValueError(f"워크플로 원장이 없다: {path}") from exc
        return cls._from_lines(path, lines)

    @classmethod
    def _from_lines(cls, path: Path, lines: list[str]) -> WorkflowJournal:
        if not lines:
            raise ValueError(f"워크플로 원장이 비어 있다: {path}")
        first = decode_message(lines[0])
        if not isinstance(first, ProjectRevision):
            raise ValueError("워크플로 원장의 첫 줄은 ProjectRevision이어야 한다")
        state: WorkflowState = "received"
        for line in lines[1:]:
            event = decode_message(line)
            if not isinstance(event, WorkflowStateEvent):
                raise ValueError("워크플로 원장의 상태 줄은 WorkflowStateEvent여야 한다")
            if event.input_revision != first.revision:
                raise ValueError("워크플로 원장에 다른 입력 리비전의 상태가 섞였다")
            if state in _TERMINAL_STATES:
                raise ValueError(f"종료 상태 뒤에는 전이가 없다: {state} → {event.state}")
            expected = _NEXT_STATE.get(state)
            if event.state != expected and event.state not in _TERMINAL_STATES:
                raise ValueError(f"허용되지 않는 상태 전이: {state} → {event.state}")
            state = event.state
        return cls(path, first, state)

    def transition(self, input_revision: str, state: WorkflowState) -> None:
        with self.path.open("r+", encoding="utf-8", newline="\n") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                current = self._from_lines(self.path, stream.readlines())
                if input_revision != current.revision.revision:
                    raise ValueError(
                        f"입력 리비전이 워크플로와 다르다: {input_revision} ≠ "
                        f"{current.revision.revision}"
                    )
                expected = _NEXT_STATE.get(current.state)
                if current.state in _TERMINAL_STATES:
                    raise ValueError(f"종료 상태 뒤에는 전이가 없다: {current.state} → {state}")
                if state != expected and state not in _TERMINAL_STATES:
                    raise ValueError(f"허용되지 않는 상태 전이: {current.state} → {state}")
                stream.seek(0, os.SEEK_END)
                stream.write(encode_message(WorkflowStateEvent(input_revision, state)))
                stream.flush()
                os.fsync(stream.fileno())
                self.state = state
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _message_payload(message: ProtocolMessage) -> dict[str, object]:
    if isinstance(message, ProjectRevision):
        return {**_project_revision_payload(message), "revision": message.revision}
    if isinstance(message, ProjectSelection):
        return {
            **_project_selection_payload(message),
            "selection_revision": message.selection_revision,
        }
    if isinstance(message, PricingSnapshot):
        return {
            **_pricing_snapshot_payload(
                effective_date=message.effective_date,
                currency=message.currency,
                processing_region=message.processing_region,
                service_tier=message.service_tier,
                luna_model=message.luna_model,
                luna_input_per_million_tokens=message.luna_input_per_million_tokens,
                luna_output_per_million_tokens=message.luna_output_per_million_tokens,
                whisper_model=message.whisper_model,
                whisper_per_minute=message.whisper_per_minute,
                gpt_transcribe_model=message.gpt_transcribe_model,
                gpt_transcribe_per_minute=message.gpt_transcribe_per_minute,
                documentation_urls=message.documentation_urls,
            ),
            "snapshot_revision": message.snapshot_revision,
        }
    if isinstance(message, AnalysisEstimate):
        return {
            **_analysis_estimate_payload(
                input_revision=message.input_revision,
                model=message.model,
                unique_source_audio_seconds=message.unique_source_audio_seconds,
                transcription_seconds=message.transcription_seconds,
                max_windows=message.max_windows,
                max_images=message.max_images,
                max_image_tokens=message.max_image_tokens,
                cost_upper_bound=message.cost_upper_bound,
                settings_sha256=message.settings_sha256,
                pricing_snapshot_revision=message.pricing_snapshot_revision,
                media_grant_revision=message.media_grant_revision,
            ),
            "estimate_revision": message.estimate_revision,
        }
    if isinstance(message, AnalysisAuthorization):
        return {
            "authorized_at": message.authorized_at,
            "estimate_revision": message.estimate_revision,
            "input_revision": message.input_revision,
            "media_grant_revision": message.media_grant_revision,
            "pricing_snapshot_revision": message.pricing_snapshot_revision,
        }
    if isinstance(message, AnalysisAttempt):
        return {
            "attempt_id": message.attempt_id,
            "error": message.error,
            "estimate_revision": message.estimate_revision,
            "input_revision": message.input_revision,
            "started_at": message.started_at,
            "status": message.status,
        }
    if isinstance(message, CancellationRequest):
        return {
            "attempt_id": message.attempt_id,
            "cancellation_revision": message.cancellation_revision,
            "input_revision": message.input_revision,
            "reason": message.reason,
            "requested_at": message.requested_at,
        }
    if isinstance(message, ActualUsage):
        return {
            **_actual_usage_payload(
                input_revision=message.input_revision,
                attempt_id=message.attempt_id,
                window_id=message.window_id,
                model=message.model,
                input_tokens=message.input_tokens,
                output_tokens=message.output_tokens,
                audio_seconds=message.audio_seconds,
                cost=message.cost,
            ),
            "usage_revision": message.usage_revision,
        }
    if isinstance(message, VisionWindowDecision):
        return {
            **_vision_decision_payload(
                input_revision=message.input_revision,
                window_id=message.window_id,
                choice=message.choice,
                candidate_id=message.candidate_id,
                evidence_frame_ids=message.evidence_frame_ids,
                confidence=message.confidence,
                response_sha256=message.response_sha256,
            ),
            "decision_revision": message.decision_revision,
        }
    if isinstance(message, CutCandidate):
        return {
            "candidate_id": message.candidate_id,
            "clip_instance_id": message.clip_instance_id,
            "confidence": message.confidence,
            "evidence": list(message.evidence),
            "input_revision": message.input_revision,
            "decision_revision": message.decision_revision,
            "reason": message.reason,
            "selected_by_default": message.selected_by_default,
            "source_range": _range_payload(message.source_range),
            "timeline_range": _range_payload(message.timeline_range),
            "window_id": message.window_id,
        }
    if isinstance(message, CandidatePreview):
        return {
            **_candidate_preview_payload(
                candidate_id=message.candidate_id,
                input_revision=message.input_revision,
                frame_id=message.frame_id,
                thumbnail_path=message.thumbnail_path,
                thumbnail_sha256=message.thumbnail_sha256,
                reason=message.reason,
                confidence=message.confidence,
                removal_duration=message.removal_duration,
            ),
            "preview_revision": message.preview_revision,
        }
    if isinstance(message, EditPlan):
        return {
            "approved_candidate_ids": list(message.approved_candidate_ids),
            "approved_decision_revisions": [
                list(row) for row in message.approved_decision_revisions
            ],
            "expected_output_duration": _fraction_text(message.expected_output_duration),
            "input_revision": message.input_revision,
            "plan_revision": message.plan_revision,
            "rejected_candidate_ids": list(message.rejected_candidate_ids),
        }
    if isinstance(message, CutResult):
        return {
            "input_revision": message.input_revision,
            "output_fcpxml": str(message.output_fcpxml),
            "plan_revision": message.plan_revision,
            "review_report": str(message.review_report),
        }
    if isinstance(message, CaptionRequest):
        return {
            "final_fcpxml": str(message.final_fcpxml),
            "final_revision": message.final_revision,
            "insertion_method": message.insertion_method,
            "origin_revision": message.origin_revision,
            "render_audio": str(message.render_audio),
            "render_audio_sha256": message.render_audio_sha256,
        }
    if isinstance(message, CaptionEstimate):
        return {
            **_caption_estimate_payload(
                request_revision=message.request_revision,
                origin_revision=message.origin_revision,
                final_revision=message.final_revision,
                render_audio_sha256=message.render_audio_sha256,
                audio_seconds=message.audio_seconds,
                transcription_seconds=message.transcription_seconds,
                cost_upper_bound=message.cost_upper_bound,
                pricing_snapshot_revision=message.pricing_snapshot_revision,
                settings_sha256=message.settings_sha256,
            ),
            "estimate_revision": message.estimate_revision,
        }
    if isinstance(message, CaptionAuthorization):
        return {
            "authorized_at": message.authorized_at,
            "estimate_revision": message.estimate_revision,
            "pricing_snapshot_revision": message.pricing_snapshot_revision,
            "request_revision": message.request_revision,
        }
    if isinstance(message, CaptionResult):
        return {
            "final_revision": message.final_revision,
            "quality": list(message.quality),
            "speech_status": message.speech_status,
            "srt_path": str(message.srt_path) if message.srt_path is not None else None,
            "warnings": list(message.warnings),
        }
    if isinstance(message, WorkflowStateEvent):
        return {"input_revision": message.input_revision, "state": message.state}
    raise TypeError(f"지원하지 않는 프로토콜 메시지다: {type(message).__name__}")


def _decode_payload(kind: object, payload: dict[str, object]) -> ProtocolMessage:
    if kind == "ProjectRevision":
        message = ProjectRevision(
            input_sha256=_string(payload, "input_sha256"),
            project_uid=_string(payload, "project_uid"),
            project_name=_string(payload, "project_name"),
            frame_duration=_fraction(payload, "frame_duration"),
            duration=_fraction(payload, "duration"),
        )
        if payload.get("revision") != message.revision:
            raise ValueError("ProjectRevision의 계산 리비전이 기록과 다르다")
        return message
    if kind == "ProjectSelection":
        selection = ProjectSelection(
            project_uid=_string(payload, "project_uid"),
            project_name=_string(payload, "project_name"),
            timeline_start=_fraction(payload, "timeline_start"),
            duration=_fraction(payload, "duration"),
            frame_duration=_fraction(payload, "frame_duration"),
        )
        if payload.get("selection_revision") != selection.selection_revision:
            raise ValueError("ProjectSelection의 계산 리비전이 기록과 다르다")
        return selection
    if kind == "PricingSnapshot":
        snapshot = PricingSnapshot.create(
            effective_date=_string(payload, "effective_date"),
            currency=_string(payload, "currency"),
            processing_region=_string(payload, "processing_region"),
            service_tier=_string(payload, "service_tier"),
            luna_model=_string(payload, "luna_model"),
            luna_input_per_million_tokens=_string(payload, "luna_input_per_million_tokens"),
            luna_output_per_million_tokens=_string(payload, "luna_output_per_million_tokens"),
            whisper_model=_string(payload, "whisper_model"),
            whisper_per_minute=_string(payload, "whisper_per_minute"),
            gpt_transcribe_model=_string(payload, "gpt_transcribe_model"),
            gpt_transcribe_per_minute=_string(payload, "gpt_transcribe_per_minute"),
            documentation_urls=_strings(payload, "documentation_urls"),
        )
        if payload.get("snapshot_revision") != snapshot.snapshot_revision:
            raise ValueError("PricingSnapshot의 계산 리비전이 기록과 다르다")
        return snapshot
    if kind == "AnalysisEstimate":
        estimate = AnalysisEstimate.create(
            input_revision=_string(payload, "input_revision"),
            model=_string(payload, "model"),
            unique_source_audio_seconds=_fraction(payload, "unique_source_audio_seconds"),
            transcription_seconds=_fraction(payload, "transcription_seconds"),
            max_windows=_integer(payload, "max_windows"),
            max_images=_integer(payload, "max_images"),
            max_image_tokens=_integer(payload, "max_image_tokens"),
            cost_upper_bound=_optional_string(payload, "cost_upper_bound"),
            settings_sha256=_string(payload, "settings_sha256"),
            pricing_snapshot_revision=_string(payload, "pricing_snapshot_revision"),
            media_grant_revision=_string(payload, "media_grant_revision"),
        )
        if payload.get("estimate_revision") != estimate.estimate_revision:
            raise ValueError("AnalysisEstimate의 계산 리비전이 기록과 다르다")
        return estimate
    if kind == "AnalysisAuthorization":
        return AnalysisAuthorization(
            input_revision=_string(payload, "input_revision"),
            estimate_revision=_string(payload, "estimate_revision"),
            pricing_snapshot_revision=_string(payload, "pricing_snapshot_revision"),
            media_grant_revision=_string(payload, "media_grant_revision"),
            authorized_at=_string(payload, "authorized_at"),
        )
    if kind == "CancellationRequest":
        cancellation = CancellationRequest.create(
            input_revision=_string(payload, "input_revision"),
            attempt_id=_string(payload, "attempt_id"),
            requested_at=_string(payload, "requested_at"),
            reason=cast(Literal["user", "active_project_changed"], _string(payload, "reason")),
        )
        if payload.get("cancellation_revision") != cancellation.cancellation_revision:
            raise ValueError("CancellationRequest의 계산 리비전이 기록과 다르다")
        return cancellation
    if kind == "ActualUsage":
        window_id = payload.get("window_id")
        if window_id is not None and not isinstance(window_id, str):
            raise ValueError("ActualUsage.window_id는 문자열 또는 null이어야 한다")
        usage = ActualUsage.create(
            input_revision=_string(payload, "input_revision"),
            attempt_id=_string(payload, "attempt_id"),
            window_id=window_id,
            model=_string(payload, "model"),
            input_tokens=_integer(payload, "input_tokens"),
            output_tokens=_integer(payload, "output_tokens"),
            audio_seconds=_fraction(payload, "audio_seconds"),
            cost=_string(payload, "cost"),
        )
        if payload.get("usage_revision") != usage.usage_revision:
            raise ValueError("ActualUsage의 계산 리비전이 기록과 다르다")
        return usage
    if kind == "AnalysisAttempt":
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("AnalysisAttempt.error는 문자열 또는 null이어야 한다")
        return AnalysisAttempt(
            input_revision=_string(payload, "input_revision"),
            estimate_revision=_string(payload, "estimate_revision"),
            attempt_id=_string(payload, "attempt_id"),
            status=cast(AnalysisAttemptStatus, _string(payload, "status")),
            started_at=_string(payload, "started_at"),
            error=error,
        )
    if kind == "VisionWindowDecision":
        candidate_id = payload.get("candidate_id")
        if candidate_id is not None and not isinstance(candidate_id, str):
            raise ValueError("VisionWindowDecision.candidate_id는 문자열 또는 null이어야 한다")
        decision = VisionWindowDecision.create(
            input_revision=_string(payload, "input_revision"),
            window_id=_string(payload, "window_id"),
            choice=cast(Literal["keep", "propose_cut"], _string(payload, "choice")),
            candidate_id=candidate_id,
            evidence_frame_ids=_strings(payload, "evidence_frame_ids"),
            confidence=_string(payload, "confidence"),
            response_sha256=_string(payload, "response_sha256"),
        )
        if payload.get("decision_revision") != decision.decision_revision:
            raise ValueError("VisionWindowDecision의 계산 리비전이 기록과 다르다")
        return decision
    if kind == "CutCandidate":
        candidate = CutCandidate(
            candidate_id=_string(payload, "candidate_id"),
            input_revision=_string(payload, "input_revision"),
            clip_instance_id=_string(payload, "clip_instance_id"),
            source_range=_range(payload, "source_range"),
            timeline_range=_range(payload, "timeline_range"),
            reason=_string(payload, "reason"),
            evidence=_strings(payload, "evidence"),
            confidence=_string(payload, "confidence"),
            window_id=_string(payload, "window_id"),
            decision_revision=_string(payload, "decision_revision"),
        )
        if payload.get("selected_by_default") is not candidate.selected_by_default:
            raise ValueError("CutCandidate의 기본 선택 값이 신뢰도와 다르다")
        return candidate
    if kind == "CandidatePreview":
        preview = CandidatePreview.create(
            candidate_id=_string(payload, "candidate_id"),
            input_revision=_string(payload, "input_revision"),
            frame_id=_string(payload, "frame_id"),
            thumbnail_path=Path(_string(payload, "thumbnail_path")),
            thumbnail_sha256=_string(payload, "thumbnail_sha256"),
            reason=_string(payload, "reason"),
            confidence=_string(payload, "confidence"),
            removal_duration=_fraction(payload, "removal_duration"),
        )
        if payload.get("preview_revision") != preview.preview_revision:
            raise ValueError("CandidatePreview의 계산 리비전이 기록과 다르다")
        return preview
    if kind == "EditPlan":
        plan = EditPlan.create(
            input_revision=_string(payload, "input_revision"),
            approved_candidate_ids=_strings(payload, "approved_candidate_ids"),
            approved_decision_revisions=_string_pairs(payload, "approved_decision_revisions"),
            rejected_candidate_ids=_strings(payload, "rejected_candidate_ids"),
            expected_output_duration=_fraction(payload, "expected_output_duration"),
        )
        if payload.get("plan_revision") != plan.plan_revision:
            raise ValueError("EditPlan의 계산 리비전이 기록과 다르다")
        return plan
    if kind == "CutResult":
        return CutResult(
            plan_revision=_string(payload, "plan_revision"),
            input_revision=_string(payload, "input_revision"),
            output_fcpxml=Path(_string(payload, "output_fcpxml")),
            review_report=Path(_string(payload, "review_report")),
        )
    if kind == "CaptionRequest":
        return CaptionRequest(
            origin_revision=_string(payload, "origin_revision"),
            final_revision=_string(payload, "final_revision"),
            render_audio=Path(_string(payload, "render_audio")),
            render_audio_sha256=_string(payload, "render_audio_sha256"),
            final_fcpxml=Path(_string(payload, "final_fcpxml")),
            insertion_method=cast(Literal["relative"], _string(payload, "insertion_method")),
        )
    if kind == "CaptionEstimate":
        caption_estimate = CaptionEstimate.create(
            request_revision=_string(payload, "request_revision"),
            origin_revision=_string(payload, "origin_revision"),
            final_revision=_string(payload, "final_revision"),
            render_audio_sha256=_string(payload, "render_audio_sha256"),
            audio_seconds=_fraction(payload, "audio_seconds"),
            transcription_seconds=_fraction(payload, "transcription_seconds"),
            cost_upper_bound=_string(payload, "cost_upper_bound"),
            pricing_snapshot_revision=_string(payload, "pricing_snapshot_revision"),
            settings_sha256=_string(payload, "settings_sha256"),
        )
        if payload.get("estimate_revision") != caption_estimate.estimate_revision:
            raise ValueError("CaptionEstimate의 계산 리비전이 기록과 다르다")
        return caption_estimate
    if kind == "CaptionAuthorization":
        return CaptionAuthorization(
            request_revision=_string(payload, "request_revision"),
            estimate_revision=_string(payload, "estimate_revision"),
            pricing_snapshot_revision=_string(payload, "pricing_snapshot_revision"),
            authorized_at=_string(payload, "authorized_at"),
        )
    if kind == "CaptionResult":
        srt = payload.get("srt_path")
        if srt is not None and not isinstance(srt, str):
            raise ValueError("CaptionResult.srt_path는 문자열 또는 null이어야 한다")
        return CaptionResult(
            final_revision=_string(payload, "final_revision"),
            srt_path=Path(srt) if srt is not None else None,
            warnings=_strings(payload, "warnings"),
            quality=_strings(payload, "quality"),
            speech_status=cast(
                Literal["speech", "no_speech", "error"], _string(payload, "speech_status")
            ),
        )
    if kind == "WorkflowStateEvent":
        return WorkflowStateEvent(
            input_revision=_string(payload, "input_revision"),
            state=cast(WorkflowState, _string(payload, "state")),
        )
    raise ValueError(f"지원하지 않는 프로토콜 메시지 타입이다: {kind!r}")


def _project_revision_payload(message: ProjectRevision) -> dict[str, object]:
    return {
        "duration": _fraction_text(message.duration),
        "frame_duration": _fraction_text(message.frame_duration),
        "input_sha256": message.input_sha256,
        "project_name": message.project_name,
        "project_uid": message.project_uid,
    }


def _project_selection_payload(message: ProjectSelection) -> dict[str, object]:
    return {
        "duration": _fraction_text(message.duration),
        "frame_duration": _fraction_text(message.frame_duration),
        "project_name": message.project_name,
        "project_uid": message.project_uid,
        "timeline_start": _fraction_text(message.timeline_start),
    }


def _analysis_estimate_payload(
    *,
    input_revision: str,
    model: str,
    unique_source_audio_seconds: Fraction,
    transcription_seconds: Fraction,
    max_windows: int,
    max_images: int,
    max_image_tokens: int,
    cost_upper_bound: str | None,
    settings_sha256: str,
    pricing_snapshot_revision: str,
    media_grant_revision: str,
) -> dict[str, object]:
    _require_sha256(input_revision, "입력 리비전")
    _require_sha256(settings_sha256, "분석 설정")
    _require_sha256(pricing_snapshot_revision, "가격 스냅샷 리비전")
    _require_sha256(media_grant_revision, "미디어 권한 리비전")
    return {
        "cost_upper_bound": (
            _decimal_text(_decimal(cost_upper_bound, "비용 상한"))
            if cost_upper_bound is not None
            else None
        ),
        "input_revision": input_revision,
        "max_image_tokens": max_image_tokens,
        "max_images": max_images,
        "max_windows": max_windows,
        "media_grant_revision": media_grant_revision,
        "model": model,
        "pricing_snapshot_revision": pricing_snapshot_revision,
        "settings_sha256": settings_sha256,
        "transcription_seconds": _fraction_text(transcription_seconds),
        "unique_source_audio_seconds": _fraction_text(unique_source_audio_seconds),
    }


def _pricing_snapshot_payload(
    *,
    effective_date: str,
    currency: str,
    processing_region: str,
    service_tier: str,
    luna_model: str,
    luna_input_per_million_tokens: str,
    luna_output_per_million_tokens: str,
    whisper_model: str,
    whisper_per_minute: str,
    gpt_transcribe_model: str,
    gpt_transcribe_per_minute: str,
    documentation_urls: tuple[str, ...],
) -> dict[str, object]:
    for name in (effective_date, currency, processing_region, service_tier):
        if not name:
            raise ValueError(f"가격 스냅샷의 {name}이 비어 있다")
    if not documentation_urls or any(not value for value in documentation_urls):
        raise ValueError("가격 스냅샷에는 공식 문서 URL이 필요하다")
    return {
        "currency": currency,
        "documentation_urls": list(documentation_urls),
        "effective_date": effective_date,
        "gpt_transcribe_model": gpt_transcribe_model,
        "gpt_transcribe_per_minute": _decimal_text(
            _decimal(gpt_transcribe_per_minute, "gpt-transcribe 분당 가격")
        ),
        "luna_input_per_million_tokens": _decimal_text(
            _decimal(luna_input_per_million_tokens, "Luna 입력 토큰 가격")
        ),
        "luna_model": luna_model,
        "luna_output_per_million_tokens": _decimal_text(
            _decimal(luna_output_per_million_tokens, "Luna 출력 토큰 가격")
        ),
        "processing_region": processing_region,
        "service_tier": service_tier,
        "whisper_model": whisper_model,
        "whisper_per_minute": _decimal_text(_decimal(whisper_per_minute, "whisper 분당 가격")),
    }


def _actual_usage_payload(
    *,
    input_revision: str,
    attempt_id: str,
    window_id: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    audio_seconds: Fraction,
    cost: str,
) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "audio_seconds": _fraction_text(audio_seconds),
        "cost": _decimal_text(_decimal(cost, "실제 비용")),
        "input_revision": input_revision,
        "input_tokens": input_tokens,
        "model": model,
        "output_tokens": output_tokens,
        "window_id": window_id,
    }


def _candidate_preview_payload(
    *,
    candidate_id: str,
    input_revision: str,
    frame_id: str,
    thumbnail_path: Path,
    thumbnail_sha256: str,
    reason: str,
    confidence: str,
    removal_duration: Fraction,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "confidence": _decimal_text(_confidence(confidence)),
        "frame_id": frame_id,
        "input_revision": input_revision,
        "reason": reason,
        "removal_duration": _fraction_text(removal_duration),
        "thumbnail_path": str(thumbnail_path),
        "thumbnail_sha256": thumbnail_sha256,
    }


def _caption_estimate_payload(
    *,
    request_revision: str,
    origin_revision: str,
    final_revision: str,
    render_audio_sha256: str,
    audio_seconds: Fraction,
    transcription_seconds: Fraction,
    cost_upper_bound: str,
    pricing_snapshot_revision: str,
    settings_sha256: str,
) -> dict[str, object]:
    return {
        "audio_seconds": _fraction_text(audio_seconds),
        "cost_upper_bound": _decimal_text(_decimal(cost_upper_bound, "자막 비용 상한")),
        "final_revision": final_revision,
        "origin_revision": origin_revision,
        "pricing_snapshot_revision": pricing_snapshot_revision,
        "render_audio_sha256": render_audio_sha256,
        "request_revision": request_revision,
        "settings_sha256": settings_sha256,
        "transcription_seconds": _fraction_text(transcription_seconds),
    }


def _vision_decision_payload(
    *,
    input_revision: str,
    window_id: str,
    choice: str,
    candidate_id: str | None,
    evidence_frame_ids: tuple[str, ...],
    confidence: str,
    response_sha256: str,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "choice": choice,
        "confidence": _decimal_text(_confidence(confidence)),
        "evidence_frame_ids": list(evidence_frame_ids),
        "input_revision": input_revision,
        "response_sha256": response_sha256,
        "window_id": window_id,
    }


def _range_payload(value: RationalRange) -> dict[str, str]:
    return {"end": _fraction_text(value.end), "start": _fraction_text(value.start)}


def _range(payload: dict[str, object], key: str) -> RationalRange:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key}는 객체여야 한다")
    return RationalRange(_fraction(value, "start"), _fraction(value, "end"))


def _fraction(payload: dict[str, object], key: str) -> Fraction:
    return Fraction(_string(payload, key))


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key}는 문자열이어야 한다")
    return value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key}는 문자열 또는 null이어야 한다")
    return value


def _strings(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key}는 문자열 배열이어야 한다")
    return tuple(value)


def _string_pairs(payload: dict[str, object], key: str) -> tuple[tuple[str, str], ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)
        for item in value
    ):
        raise ValueError(f"{key}는 문자열 쌍의 배열이어야 한다")
    return tuple((item[0], item[1]) for item in value)


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key}는 정수여야 한다")
    return value


def _confidence(value: str) -> Decimal:
    return _decimal(value, "신뢰도")


def _decimal(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label}가 십진수가 아니다: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label}가 유한한 십진수가 아니다: {value!r}")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} SHA-256이 아니다: {value!r}")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _digest_sequence(parts: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


_ANALYSIS_PROTOCOL_TYPES = frozenset(
    {
        "ProjectSelection",
        "PricingSnapshot",
        "AnalysisEstimate",
        "AnalysisAuthorization",
        "AnalysisAttempt",
        "CancellationRequest",
        "ActualUsage",
        "VisionWindowDecision",
        "CutCandidate",
        "CandidatePreview",
        "EditPlan",
    }
)

_CAPTION_PROTOCOL_TYPES = frozenset(
    {
        "CaptionRequest",
        "CaptionEstimate",
        "CaptionAuthorization",
        "CaptionResult",
    }
)


def _protocol_version(message: ProtocolMessage) -> str:
    return _protocol_version_for_kind(type(message).__name__)


def _protocol_version_for_kind(kind: object) -> str:
    if kind in _ANALYSIS_PROTOCOL_TYPES:
        return ANALYSIS_PROTOCOL_VERSION
    if kind in _CAPTION_PROTOCOL_TYPES:
        return CAPTION_PROTOCOL_VERSION
    return PROTOCOL_VERSION
