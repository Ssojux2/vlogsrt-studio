"""Workflow Extension과 Python 엔진 사이의 결정적 JSON Lines 계약.

이 모듈은 앱 경계의 메시지만 소유한다. 기존 편집 엔진의 ``edit.EditPlan``과
이름은 같지만 네임스페이스가 다르며, 그 객체를 직렬화하려고 추측하지 않는다.
모든 시각은 정확한 ``Fraction``이고 모든 리비전은 정준 JSON의 SHA-256이다.
"""

from __future__ import annotations

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
DEFAULT_REVIEW_CONFIDENCE = Decimal("0.90")

WorkflowState: TypeAlias = Literal[
    "received",
    "analyzing",
    "review_required",
    "roughcut_imported",
    "caption_requested",
    "caption_ready",
]

_NEXT_STATE: dict[WorkflowState, WorkflowState] = {
    "received": "analyzing",
    "analyzing": "review_required",
    "review_required": "roughcut_imported",
    "roughcut_imported": "caption_requested",
    "caption_requested": "caption_ready",
}


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
class CutCandidate:
    candidate_id: str
    input_revision: str
    clip_instance_id: str
    source_range: RationalRange
    timeline_range: RationalRange
    reason: str
    evidence: tuple[str, ...]
    confidence: str

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        if not self.candidate_id or not self.clip_instance_id or not self.reason:
            raise ValueError("후보 ID·클립 인스턴스·사유는 비어 있을 수 없다")
        value = _confidence(self.confidence)
        if value < 0 or value > 1:
            raise ValueError(f"신뢰도는 0~1이어야 한다: {self.confidence!r}")

    @property
    def selected_by_default(self) -> bool:
        return _confidence(self.confidence) >= DEFAULT_REVIEW_CONFIDENCE


@dataclass(frozen=True, slots=True)
class EditPlan:
    input_revision: str
    approved_candidate_ids: tuple[str, ...]
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
    ) -> EditPlan:
        _require_sha256(input_revision, "입력 리비전")
        approved = tuple(sorted(set(approved_candidate_ids)))
        rejected = tuple(sorted(set(rejected_candidate_ids)))
        overlap = sorted(set(approved) & set(rejected))
        if overlap:
            raise ValueError(f"후보가 승인과 거절에 동시에 들어 있다: {overlap}")
        if not approved and not rejected:
            raise ValueError("승인 또는 거절한 후보가 하나 이상 있어야 한다")
        if expected_output_duration <= 0:
            raise ValueError("예상 출력 길이는 양수여야 한다")
        payload = {
            "approved_candidate_ids": list(approved),
            "expected_output_duration": _fraction_text(expected_output_duration),
            "input_revision": input_revision,
            "rejected_candidate_ids": list(rejected),
        }
        return cls(
            input_revision=input_revision,
            approved_candidate_ids=approved,
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
    final_fcpxml: Path
    insertion_method: Literal["relative"] = "relative"

    def __post_init__(self) -> None:
        _require_sha256(self.origin_revision, "원본 워크플로 리비전")
        _require_sha256(self.final_revision, "최종 FCPXML 리비전")
        if self.insertion_method != "relative":
            raise ValueError("자막 삽입 방식은 relative만 지원한다")


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
    | CutCandidate
    | EditPlan
    | CutResult
    | CaptionRequest
    | CaptionResult
    | WorkflowStateEvent
)


def encode_message(message: ProtocolMessage) -> str:
    """메시지 하나를 정준 JSONL 한 줄로 만든다."""
    payload = _message_payload(message)
    envelope = {
        "payload": payload,
        "protocol_version": PROTOCOL_VERSION,
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
    version = envelope.get("protocol_version")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"지원하지 않는 프로토콜 버전이다: {version!r}")
    kind = envelope.get("type")
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
            expected = _NEXT_STATE.get(state)
            if event.state != expected:
                raise ValueError(f"허용되지 않는 상태 전이: {state} → {event.state}")
            state = event.state
        return cls(path, first, state)

    def transition(self, input_revision: str, state: WorkflowState) -> None:
        if input_revision != self.revision.revision:
            raise ValueError(
                f"입력 리비전이 워크플로와 다르다: {input_revision} ≠ {self.revision.revision}"
            )
        expected = _NEXT_STATE.get(self.state)
        if state != expected:
            raise ValueError(f"허용되지 않는 상태 전이: {self.state} → {state}")
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encode_message(WorkflowStateEvent(input_revision, state)))
            stream.flush()
            os.fsync(stream.fileno())
        self.state = state


def _message_payload(message: ProtocolMessage) -> dict[str, object]:
    if isinstance(message, ProjectRevision):
        return {**_project_revision_payload(message), "revision": message.revision}
    if isinstance(message, CutCandidate):
        return {
            "candidate_id": message.candidate_id,
            "clip_instance_id": message.clip_instance_id,
            "confidence": message.confidence,
            "evidence": list(message.evidence),
            "input_revision": message.input_revision,
            "reason": message.reason,
            "selected_by_default": message.selected_by_default,
            "source_range": _range_payload(message.source_range),
            "timeline_range": _range_payload(message.timeline_range),
        }
    if isinstance(message, EditPlan):
        return {
            "approved_candidate_ids": list(message.approved_candidate_ids),
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
        )
        if payload.get("selected_by_default") is not candidate.selected_by_default:
            raise ValueError("CutCandidate의 기본 선택 값이 신뢰도와 다르다")
        return candidate
    if kind == "EditPlan":
        plan = EditPlan.create(
            input_revision=_string(payload, "input_revision"),
            approved_candidate_ids=_strings(payload, "approved_candidate_ids"),
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
            final_fcpxml=Path(_string(payload, "final_fcpxml")),
            insertion_method=cast(Literal["relative"], _string(payload, "insertion_method")),
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


def _strings(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key}는 문자열 배열이어야 한다")
    return tuple(value)


def _confidence(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"신뢰도가 십진수가 아니다: {value!r}") from exc


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} SHA-256이 아니다: {value!r}")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
