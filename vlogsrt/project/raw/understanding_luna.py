"""첫 단계(A4) Luna 의미 판정 — TEXT+IMAGE 증거와 제한된 JSON 출력.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7.2·§7.5·§14.3의 계약을 세운다:

- Luna는 **텍스트·이미지만** 받는다 — 오디오·영상 파일은 가지 않는다. 대화
  내용은 내부 전사 증거(글자와 시각)로, 장면은 프레임 표본 이미지로 들어간다.
- 출력은 제공된 window/frame/utterance/boundary 아이디만 쓰는 제한 JSON이다.
  필수 아이디 누락·제공되지 않은 아이디·범위 이탈·JSON 오류·API 실패는 검증
  오류로 처리되고 — 그 어떤 경로도 영상 삭제·자막 지어내기 권한을 얻지
  못한다(§7.5).
- 실제 호출은 `vlogsrt/transcribe.request_luna_vision`의 승인형 경계 하나뿐이다.
  승인은 출처·설정 지문·범위·모델·예산을 묶은 `OnlineAnalysisApproval`이며
  오프라인 설정이 항상 우선한다(§14.3).

비용은 추정만 존재한다. Luna 단가는 미확인이며 0원으로 계산하지 않는다 —
정직한 `unknown`이거나 사용자 제공 상한일 뿐이다(§14.3).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Protocol

from vlogsrt.project.raw.analysis_config import (
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.config import RawWorkflowConfig, validate_raw_workflow_config
from vlogsrt.project.raw.understanding import (
    AnalysisWindow,
    AsrEvidence,
    BoundaryCandidate,
    FrameEvidence,
    FrameSamplingResult,
    SegmentationProposal,
    SelectedAudio,
    UtteranceEvidence,
    WindowPlan,
    WindowVerdict,
    stage1_evidence_document,
)
from vlogsrt.transcribe import RequestObserver, request_luna_vision

LUNA_OUTPUT_SCHEMA_NAME = "raw_understanding_window_v1"
"""출력 JSON schema의 이름 — 지문과 승인에 함께 실린다."""

ANNOTATION_ACCEPTED = "accepted"
ANNOTATION_REJECTED = "rejected"
ANNOTATION_FAILED = "failed"

PRICE_UNKNOWN = "unknown"
PRICE_USER_SUPPLIED = "user_supplied"


class LunaEvidenceError(RuntimeError):
    """증거 조립이 나가기 전에 닫힌 상황 — 호출은 한 번도 일어나지 않는다."""


class AnalysisApprovalError(RuntimeError):
    """온라인 실행 승인이 계약을 어긴다 — 유료 호출은 시작할 수 없다."""


class BudgetExhausted(RuntimeError):
    """요청 수 예약 한도를 넘었다 — 새 호출은 시작하지 않는다."""


# ─────────────────────────────────────────────────────────────
#  출력 JSON schema — 제공된 아이디만 쓰는 제한된 형태
# ─────────────────────────────────────────────────────────────


LUNA_OUTPUT_FIELDS: tuple[str, ...] = (
    "window_id",
    "speech_summary",
    "visual_summary",
    "scene_tags",
    "evidence_ids",
    "suggested_boundary_ids",
    "editing_notes",
    "needs_review",
    "uncertainties",
)
"""출력 칸 전부 — schema 빌더와 결함 검사가 같은 목록을 쓴다."""


def luna_output_schema() -> dict[str, object]:
    """Luna 출력의 strict JSON schema — 칸은 §7.5의 예시 그대로다.

    `additionalProperties: false`에 모든 칸이 필수다(strict 모드의 요건).
    수치 confidence 칸은 아예 없다 — 보정된 확률로 취급하지 않는다(§7.5).
    """
    properties: dict[str, object] = {
        "window_id": {"type": "string"},
        "speech_summary": {"type": "string"},
        "visual_summary": {"type": "string"},
        "scene_tags": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "suggested_boundary_ids": {"type": "array", "items": {"type": "string"}},
        "editing_notes": {"type": "array", "items": {"type": "string"}},
        "needs_review": {"type": "boolean"},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(LUNA_OUTPUT_FIELDS),
        "additionalProperties": False,
    }


# ─────────────────────────────────────────────────────────────
#  창 증거 — Luna가 보는 것의 전부
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WindowEvidence:
    """창 하나의 증거 묶음 — 창·프레임·발화·경계 후보가 전부다.

    오디오 파일 경로는 어디에도 없다(§7.2). 프레임은 **뽑힌 것만** — 추출에
    실패한 프레임은 증거가 아니므로 여기 오지 않는다.
    """

    window: AnalysisWindow
    frames: tuple[FrameEvidence, ...]
    utterances: tuple[UtteranceEvidence, ...]
    candidates: tuple[BoundaryCandidate, ...]

    def provided_evidence_ids(self) -> frozenset[str]:
        """제공한 증거 아이디 전부 — Luna가 말할 수 있는 아이디의 세계다."""
        return frozenset(
            [self.window.window_id]
            + [frame.frame_id for frame in self.frames]
            + [item.utterance_id for item in self.utterances]
        )

    def provided_boundary_ids(self) -> frozenset[str]:
        """제공한 경계 후보 아이디 — 제안은 이 안에서만 가능하다."""
        return frozenset(item.boundary_id for item in self.candidates)


def window_evidence(
    plan: WindowPlan,
    frames: FrameSamplingResult,
    utterances: Sequence[UtteranceEvidence],
) -> tuple[WindowEvidence, ...]:
    """창마다 증거를 묶는다 — 창 계획이 정한 순서·아이디 그대로다."""
    by_id = {frame.frame_id: frame for frame in frames.frames}
    by_utterance = {item.utterance_id: item for item in utterances}
    candidates_by_window: dict[str, list[BoundaryCandidate]] = {}
    for candidate in plan.candidates:
        candidates_by_window.setdefault(candidate.window_id, []).append(candidate)
    return tuple(
        WindowEvidence(
            window=window,
            frames=tuple(
                by_id[item.frame_id] for item in window.frame_plans if item.frame_id in by_id
            ),
            utterances=tuple(
                by_utterance[utterance_id]
                for utterance_id in window.utterance_ids
                if utterance_id in by_utterance
            ),
            candidates=tuple(candidates_by_window.get(window.window_id, ())),
        )
        for window in plan.windows
    )


def build_luna_payload(
    evidence: WindowEvidence, settings: RawUnderstandingSettings
) -> dict[str, object]:
    """창 증거를 Responses API 페이로드로 — TEXT+IMAGE뿐, 다른 형태는 없다.

    이미지는 data URL(base64 JPEG)로 프레임 순서 그대로 실린다. 출력 강제는
    `text.format`의 strict json_schema로 한다 — 모델이 이 schema 밖을 말하면
    호출이 아니라 파싱 전에 실패해야 한다.
    """
    return {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _instruction(evidence)},
                    *(
                        {
                            "type": "input_image",
                            "image_url": _image_data_url(frame),
                        }
                        for frame in evidence.frames
                    ),
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": LUNA_OUTPUT_SCHEMA_NAME,
                "strict": True,
                "schema": luna_output_schema(),
            }
        },
    }


def _image_data_url(frame: FrameEvidence) -> str:
    """프레임 한 장을 data URL로 — 파일을 그대로 보내지 않고 증거 바이트만."""
    return "data:image/jpeg;base64," + base64.b64encode(frame.image_path.read_bytes()).decode(
        "ascii"
    )


def _instruction(evidence: WindowEvidence) -> str:
    """창 하나에 대한 안내문 — 아이디의 세계를 먼저 알려고 규칙으로 닫는다."""
    window = evidence.window
    utterance_lines = [
        f"  {item.utterance_id} [{_seconds(item.start)}~{_seconds(item.end)}초]"
        f" {item.text.strip()!r}"
        for item in evidence.utterances
    ] or ["  (없음 — 발화가 확인되지 않았다 뿐, 없다고 확정된 것이 아니다)"]
    candidate_lines = [
        f"  {item.boundary_id} ({_seconds(item.position)}초, {item.kind})"
        for item in evidence.candidates
    ] or ["  (없음 — suggested_boundary_ids는 빈 배열이어야 한다)"]
    frame_ids = ", ".join(frame.frame_id for frame in evidence.frames) or "(없음)"
    return "\n".join(
        [
            "원본 영상의 한 분석 시간창을 봅니다. 대화는 내부 전사 증거로, 장면은"
            " 프레임 표본으로 주어집니다. 오디오·영상 파일은 주어지지 않습니다.",
            f"window_id: {window.window_id}",
            f"코어 구간: {_seconds(window.core_start)}초 ~ {_seconds(window.core_end)}초"
            f" (정규화 시각, 표시는 소수 3자리)",
            "발화 증거:",
            *utterance_lines,
            f"프레임 증거(순서대로): {frame_ids}",
            "제안 가능한 경계 후보(이 안에서만 제안하라):",
            *candidate_lines,
            "규칙:",
            "- 제공된 window/frame/utterance/boundary 아이디 외의 아이디를 말하지 마라.",
            "- 증거에 없는 말·장면을 지어내지 마라. 빈 확인은 '확인 안 됨'으로만 다뤄라.",
            "- 경계는 발화·이어지는 동작을 자르지 않는 쪽만 제안하라. 모호하면 제안하지 마라.",
            "- 영상을 지우거나 순서를 바꾸는 명령은 없다. 요약·태그·메모·경계 제안만 있다.",
            "- 출력은 지정된 JSON schema 하나다.",
        ]
    )


def _seconds(value: Fraction) -> str:
    """유리수 시각의 표시용 문자열 — 표시는 근사, 아이디·틱이 정본이다."""
    return f"{float(value):.3f}"


def luna_request_sha256(model: str, payload: Mapping[str, object]) -> str:
    """요청의 신원 — 모델·페이로드를 정렬해 해시한다(판정 기록의 열쇠)."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(f"{model}\0{body}".encode()).hexdigest()


# ─────────────────────────────────────────────────────────────
#  판정 — 파싱과 검증이 같은 문을 지난다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SemanticAnnotation:
    """창 하나의 Luna 판정 — 받아들여졌거나 거부·실패로 기록된다.

    `status`가 accepted여도 `needs_review`·`uncertainties`는 그대로 살아
    올라간다 — 판정이 검토를 대신하지 않는다. `defect`는 거부·실패의 이유다.
    `origin`은 실제 호출(live)과 픽스처 재생(replay)을 가른다 — 재생을 실제
    성공으로 표시하지 않는다(§14.3).
    """

    window_id: str
    status: str
    request_sha256: str
    luna_model: str
    prompt_revision: str
    origin: str
    speech_summary: str = ""
    visual_summary: str = ""
    scene_tags: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    suggested_boundary_ids: tuple[str, ...] = ()
    editing_notes: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    needs_review: bool = False
    defect: str = ""


def parse_luna_response(
    response: Mapping[str, object],
    *,
    evidence: WindowEvidence,
    model: str,
    request_sha256: str,
    origin: str,
    prompt_revision: str,
) -> SemanticAnnotation:
    """모델 응답을 판정으로 — 꼴이 어긋나면 거부, 지어내는 길은 없다.

    필수 키·타입·아이디 소속을 전부 본 뒤에야 accepted가 나온다. 검증 오류는
    예외가 아니라 `rejected` 판정이다 — 창 하나의 실패가 전체 분석을 끄지 않고,
    그 창은 보수적 유지로 흘러간다(§7.6).
    """
    text = response.get("output_text") if isinstance(response, Mapping) else None
    if not isinstance(text, str) or not text.strip():
        return _rejected(
            evidence,
            model,
            request_sha256,
            origin,
            prompt_revision,
            "output_text가 없다 — 판정 본문이 비었다",
        )
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return _rejected(
            evidence, model, request_sha256, origin, prompt_revision, f"JSON이 아니다: {exc}"
        )
    defect = _annotation_defect(parsed, evidence)
    if defect:
        return _rejected(evidence, model, request_sha256, origin, prompt_revision, defect)
    body = parsed
    return SemanticAnnotation(
        window_id=evidence.window.window_id,
        status=ANNOTATION_ACCEPTED,
        request_sha256=request_sha256,
        luna_model=model,
        prompt_revision=prompt_revision,
        origin=origin,
        speech_summary=body["speech_summary"],
        visual_summary=body["visual_summary"],
        scene_tags=tuple(body["scene_tags"]),
        evidence_ids=tuple(body["evidence_ids"]),
        suggested_boundary_ids=tuple(body["suggested_boundary_ids"]),
        editing_notes=tuple(body["editing_notes"]),
        uncertainties=tuple(body["uncertainties"]),
        needs_review=bool(body["needs_review"]),
    )


def _rejected(
    evidence: WindowEvidence,
    model: str,
    request_sha256: str,
    origin: str,
    prompt_revision: str,
    defect: str,
) -> SemanticAnnotation:
    """거부 판정 — 근거만 남고 어떤 경계 제안도 남지 않는다."""
    return SemanticAnnotation(
        window_id=evidence.window.window_id,
        status=ANNOTATION_REJECTED,
        request_sha256=request_sha256,
        luna_model=model,
        prompt_revision=prompt_revision,
        origin=origin,
        needs_review=True,
        defect=defect,
    )


def _annotation_defect(parsed: object, evidence: WindowEvidence) -> str:
    """판정 본문의 결함 검사 — 칸·타입·아이디 소속·window 일치를 전부 본다."""
    if not isinstance(parsed, dict):
        return f"판정 본문이 JSON 객체가 아니다: {type(parsed).__name__}"
    unknown = sorted(set(parsed) - set(LUNA_OUTPUT_FIELDS))
    if unknown:
        return f"schema에 없는 칸이 있다: {', '.join(unknown)}"
    missing = sorted(key for key in LUNA_OUTPUT_FIELDS if key not in parsed)
    if missing:
        return f"필수 칸이 비었다: {', '.join(missing)}"
    if parsed["window_id"] != evidence.window.window_id:
        return (
            f"window_id가 이 창이 아니다: {parsed['window_id']!r}"
            f" (기대 {evidence.window.window_id!r})"
        )
    for key in ("speech_summary", "visual_summary"):
        if not isinstance(parsed[key], str):
            return f"{key}이 문자열이 아니다: {parsed[key]!r}"
    for key in ("scene_tags", "editing_notes", "uncertainties"):
        defect = _string_list_defect(parsed[key], key)
        if defect:
            return defect
    if not isinstance(parsed["needs_review"], bool):
        return f"needs_review가 bool이 아니다: {parsed['needs_review']!r}"
    provided = evidence.provided_evidence_ids()
    for identifier in _string_list(parsed["evidence_ids"], "evidence_ids"):
        if identifier not in provided:
            return f"제공되지 않은 증거 아이디다: {identifier!r}"
    boundaries = evidence.provided_boundary_ids()
    for identifier in _string_list(parsed["suggested_boundary_ids"], "suggested_boundary_ids"):
        if identifier not in boundaries:
            return f"제공되지 않은 경계 아이디다: {identifier!r}"
    return ""


def _string_list_defect(value: object, key: str) -> str:
    """문자열 배열 칸의 형태 검사."""
    if not isinstance(value, list):
        return f"{key}이 배열이 아니다: {value!r}"
    for item in value:
        if not isinstance(item, str):
            return f"{key}에 문자열이 아닌 항목이 있다: {item!r}"
    return ""


def _string_list(value: object, key: str) -> list[str]:
    """이미 형태 검사를 지난 문자열 배열을 꺼낸다."""
    return list(value) if isinstance(value, list) else []


# ─────────────────────────────────────────────────────────────
#  응답자 — 주입 경계와 실제 호출
# ─────────────────────────────────────────────────────────────


class SemanticResponder(Protocol):
    """창 하나의 의미 판정 요청 경계 — 실제·픽스처 구현이 갈린다."""

    def respond(self, *, window_id: str, payload: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ReplaySemanticResponder:
    """픽스처 재생 — 창 아이디로 준비된 응답을 돌려주는 주입 구현.

    결과의 `origin`은 replay로 기록된다 — 오프라인 재생을 실제 API 성공으로
    표시하지 않는다(§14.3).
    """

    responses: Mapping[str, Mapping[str, object]]

    def respond(self, *, window_id: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        try:
            return self.responses[window_id]
        except KeyError as exc:
            raise LunaEvidenceError(
                f"재생할 Luna 응답이 없다: {window_id} — 실행 설정의 결함이다"
            ) from exc


@dataclass(frozen=True, slots=True)
class LunaSemanticResponder:
    """실제 Luna 호출 — 승인·예약 문을 지난 뒤에만 전송 경계를 연다.

    `request_luna_vision`의 `online=True`도 사람 승인의 결과로만 온다 — 이
    객체가 승인을 만들지 않는다. 예약 게이트는 창마다 한 번(같은 창 재시도는
    새 예약을 세지 않는다).
    """

    client: object
    model: str
    approval: OnlineAnalysisApproval
    gate: ReservationGate | None = None
    observer: RequestObserver | None = None
    cancellation: Callable[[], None] | None = None

    def respond(self, *, window_id: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        blocked = online_env_blocked()  # 이중 문 — 승인 때 열려도 지금 닫혀 있으면 안 간다
        if blocked is not None:
            raise AnalysisApprovalError(blocked)
        if self.gate is not None:
            self.gate.reserve(count=1, key=window_id)
        return request_luna_vision(
            self.client,
            model=self.model,
            payload=payload,
            online=True,
            observer=self.observer,
            cancellation=self.cancellation,
        )


def annotate_windows(
    evidence_windows: Sequence[WindowEvidence],
    responder: SemanticResponder,
    *,
    settings: RawUnderstandingSettings,
    model: str,
    origin: str,
) -> tuple[SemanticAnnotation, ...]:
    """창마다 판정을 얻는다 — 호출 실패는 그 창의 failed 판정이다.

    한 창의 실패(타임아웃·API 오류 포함)가 나머지 창을 멈추지 않는다 — 실패는
    기록이고 보수적 유지의 재료다(§7.6). 조립 오류는 여기서 닫는다: 깨진 증거로
    유료 호출이 나가는 일이 없어야 한다.
    """
    annotations: list[SemanticAnnotation] = []
    for evidence in evidence_windows:
        payload = build_luna_payload(evidence, settings)
        request_sha256 = luna_request_sha256(model, payload)
        try:
            response = responder.respond(window_id=evidence.window.window_id, payload=payload)
        except Exception as exc:
            annotations.append(
                SemanticAnnotation(
                    window_id=evidence.window.window_id,
                    status=ANNOTATION_FAILED,
                    request_sha256=request_sha256,
                    luna_model=model,
                    prompt_revision=settings.prompt_revision,
                    origin=origin,
                    needs_review=True,
                    defect=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        annotations.append(
            parse_luna_response(
                response,
                evidence=evidence,
                model=model,
                request_sha256=request_sha256,
                origin=origin,
                prompt_revision=settings.prompt_revision,
            )
        )
    return tuple(annotations)


def verdicts_from_annotations(
    annotations: Sequence[SemanticAnnotation],
) -> dict[str, WindowVerdict]:
    """판정을 결정적 분할이 쓰는 요약으로 — accepted만 경계 제안을 싣는다."""
    verdicts: dict[str, WindowVerdict] = {}
    for annotation in annotations:
        accepted = annotation.status == ANNOTATION_ACCEPTED
        verdicts[annotation.window_id] = WindowVerdict(
            window_id=annotation.window_id,
            accepted=accepted,
            suggested_boundary_ids=annotation.suggested_boundary_ids if accepted else (),
            needs_review=annotation.needs_review or bool(annotation.uncertainties) or not accepted,
            note=annotation.defect,
        )
    return verdicts


# ─────────────────────────────────────────────────────────────
#  예산·비용 — 미확인 가격을 0원으로 두지 않는다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AnalysisBudget:
    """실행 예산의 상한 — 요청 수 한도와 (있다면) 사용자 제공 단가 상한.

    Luna 단가는 문서 확인값이 없다. `luna_price_status`가 unknown이면 어떤
    금액 계산도 하지 않는다 — 미확인을 무료로 두는 길은 없다(§14.3).
    """

    max_luna_requests: int
    max_asr_chunks: int
    luna_price_status: str = PRICE_UNKNOWN
    luna_user_ceiling_per_request: Decimal | None = None
    currency: str = "USD"

    def __post_init__(self) -> None:
        if isinstance(self.max_luna_requests, bool) or self.max_luna_requests < 1:
            raise ValueError(f"Luna 요청 한도는 1 이상 정수다: {self.max_luna_requests!r}")
        if isinstance(self.max_asr_chunks, bool) or self.max_asr_chunks < 1:
            raise ValueError(f"ASR 청크 한도는 1 이상 정수다: {self.max_asr_chunks!r}")
        if self.luna_price_status not in {PRICE_UNKNOWN, PRICE_USER_SUPPLIED}:
            raise ValueError(f"알 수 없는 단가 상태다: {self.luna_price_status!r}")
        if self.luna_price_status == PRICE_USER_SUPPLIED:
            if not isinstance(self.luna_user_ceiling_per_request, Decimal):
                raise ValueError("사용자 상한은 Decimal이어야 한다")
            if not self.luna_user_ceiling_per_request.is_finite() or (
                self.luna_user_ceiling_per_request <= 0
            ):
                raise ValueError("사용자 상한은 유한한 양수여야 한다")


@dataclass(frozen=True, slots=True)
class LunaCostEstimate:
    """Luna 비용 추정 — 요청 수는 사실, 금액은 상태가 말한다.

    `user_ceiling_total`은 사용자가 제공한 상한의 곱일 뿐 실측 견적이 아니다.
    `note`가 그 한계를 사람 말로 남긴다.
    """

    request_count: int
    price_status: str
    user_ceiling_total: Decimal | None
    note: str


def estimate_luna_cost(window_count: int, budget: AnalysisBudget) -> LunaCostEstimate:
    """창 수로 비용 추정을 만든다 — 미확인 단가는 금액을 만들지 않는다."""
    per_request = budget.luna_user_ceiling_per_request
    if budget.luna_price_status == PRICE_USER_SUPPLIED and isinstance(per_request, Decimal):
        return LunaCostEstimate(
            request_count=window_count,
            price_status=PRICE_USER_SUPPLIED,
            user_ceiling_total=per_request * window_count,
            note="사용자 제공 상한의 곱 — 실측 견적이 아니다",
        )
    return LunaCostEstimate(
        request_count=window_count,
        price_status=PRICE_UNKNOWN,
        user_ceiling_total=None,
        note="Luna 단가 미확인 — 금액 추정을 만들지 않는다(0원 계산 금지)",
    )


class ReservationGate(Protocol):
    """요청 수 예약 문 — 실제 호출 열기 전에 한도를 남겨둔다."""

    def reserve(self, *, count: int, key: str) -> None: ...


@dataclass
class CountingReservationGate:
    """단순 예약 게이트 — 남은 요청 수를 세는 구현(단일 실행 가정).

    같은 key의 재예약은 세지 않는다 — 창 단위 예약이고 재시도가 문을 두 번
    통과하게 하지 않는다. 한도를 넘으면 `BudgetExhausted`로 닫힌다.
    """

    budget: AnalysisBudget
    remaining: int = 0
    keys: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.remaining = self.budget.max_luna_requests

    def reserve(self, *, count: int, key: str) -> None:
        if key in self.keys:
            return
        if count > self.remaining:
            raise BudgetExhausted(
                f"요청 예약 한도를 넘었다 — 남음 {self.remaining}, 요청 {count} (key {key!r})"
            )
        self.remaining -= count
        self.keys.add(key)


# ─────────────────────────────────────────────────────────────
#  온라인 승인 — 출처·지문·범위·모델을 묶는다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OnlineAnalysisApproval:
    """사람이 준 온라인 실행 승인 — 승인이 본 것과 실행이 본 것이 같은지의 증명.

    승인은 출처 해시·오디오 스트림·샘플 수·창 수·분석 지문·모델까지 묶는다.
    실행 중 하나라도 바뀌면 이 승인은 더 유효하지 않다 — 새 추정과 새 승인을
    받아야 한다(§14.3).
    """

    approved: bool
    source_sha256: str
    audio_stream_index: int
    sample_count: int
    window_count: int
    fingerprint: str
    asr_model: str
    luna_model: str
    budget: AnalysisBudget


def online_env_blocked() -> str | None:
    """환경의 오프라인 스위치 — 설정이 online이어도 환경이 닫으면 닫는다.

    `VLOGSRT_OFFLINE`은 사람의 손으로만 여는 문이다(기본 1). 유료 호출은 이
    값과 워크플로 설정 **둘 다** online일 때만 나갈 수 있다 — UI가 설정을
    바꿔서 환경을 우회하는 길이 없다(§14.3).
    """
    from vlogsrt import env as runtime_env

    if runtime_env.is_offline():
        return "VLOGSRT_OFFLINE이 오프라인이다 — 설정과 무관하게 유료 호출은 닫혀 있다"
    return None


def require_online_approval(
    approval: OnlineAnalysisApproval,
    *,
    settings: RawWorkflowConfig,
    audio: SelectedAudio,
    plan: WindowPlan,
    fingerprint: str,
) -> None:
    """온라인 실행 문 — 환경·설정이 함께 열려 있고 승인은 전부 일치해야 한다.

    설정 자체의 동결 검사를 먼저 지나게 한다(우회 불가). 환경(`VLOGSRT_OFFLINE`)
    과 `network_mode` 둘 중 하나라도 offline이면 승인이 있어도 닫힌다 — 픽스처
    재생만 가능하고 그 결과는 replay로 기록된다(§14.3).
    """
    validate_raw_workflow_config(settings)
    blocked = online_env_blocked()
    if blocked is not None:
        raise AnalysisApprovalError(blocked)
    if settings.network_mode != "online":
        raise AnalysisApprovalError(
            f"네트워크 모드가 {settings.network_mode!r}다 — 온라인 실행 문은 닫혀 있다"
        )
    if not approval.approved:
        raise AnalysisApprovalError("승인되지 않은 온라인 실행이다")
    if approval.fingerprint != fingerprint:
        raise AnalysisApprovalError(
            "승인이 본 분석 지문과 실행 지문이 다르다 — 새 추정과 새 승인을 받아라"
        )
    if approval.source_sha256 != audio.source_sha256:
        raise AnalysisApprovalError("승인이 본 원본과 실행 대상 원본이 다르다")
    if approval.audio_stream_index != audio.audio_stream_index:
        raise AnalysisApprovalError("승인이 본 오디오 스트림과 실행 선택이 다르다")
    if approval.sample_count != audio.sample_count:
        raise AnalysisApprovalError("승인이 본 오디오 분량과 실행 분량이 다르다")
    if approval.window_count != len(plan.windows):
        raise AnalysisApprovalError(
            f"승인이 본 창 수({approval.window_count})와 실행 창 수({len(plan.windows)})가 다르다"
        )
    if approval.asr_model != settings.models.timing_model:
        raise AnalysisApprovalError("승인이 본 ASR 모델과 설정 모델이 다르다")
    if approval.luna_model != settings.models.luna_model:
        raise AnalysisApprovalError("승인이 본 Luna 모델과 설정 모델이 다르다")
    if len(plan.windows) > approval.budget.max_luna_requests:
        raise AnalysisApprovalError("승인 예산의 Luna 요청 한도를 창 수가 넘는다")


# ─────────────────────────────────────────────────────────────
#  지문 — 캐시와 승인의 열쇠
# ─────────────────────────────────────────────────────────────


def analysis_fingerprint(
    *,
    audio: SelectedAudio,
    plan: WindowPlan,
    settings: RawUnderstandingSettings,
    asr_model: str,
    luna_model: str,
) -> str:
    """분석 캐시·승인 지문 — 결과에 영향을 주는 입력 전부를 묶는다(§14.2).

    출처 해시·스트림 프로파일·창·프레임 표본(시각·긴변 변환)·경계 후보·ASR·
    Luna 모델·프롬프트/스키마 리비전이 함께 들어간다. 표시 전용 값(자막 표시
    토글 따위)은 애초에 여기 오지 않는다. 오류 응답은 성공 캐시로 저장되지
    않으므로 지문이 같다고 실패가 재생되지 않는다.
    """
    parts = [
        "raw-understanding-fingerprint/v1",
        UNDERSTANDING_SCHEMA_VERSION,
        audio.source_sha256,
        str(audio.audio_stream_index),
        f"{audio.sample_rate}/{audio.channels}/{audio.sample_count}",
        f"{audio.pcm_sample0_file_time.numerator}/{audio.pcm_sample0_file_time.denominator}",
        str(settings.prompt_revision),
        str(settings.image_long_edge),
        LUNA_OUTPUT_SCHEMA_NAME,
        asr_model,
        luna_model,
    ]
    for window in plan.windows:
        parts.append(
            f"{window.window_id}:{window.core_start.numerator}/{window.core_start.denominator}"
            f"-{window.core_end.numerator}/{window.core_end.denominator}"
            f":{','.join(str(item.frame_index) for item in window.frame_plans)}"
        )
    for candidate in plan.candidates:
        parts.append(f"{candidate.boundary_id}:{candidate.kind}:{candidate.tick}")
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest


def cacheable(annotation: SemanticAnnotation) -> bool:
    """성공 캐시에 들어갈 판정인가 — 받아들인 판정만이다(§14.2).

    거부·실패를 캐시에 담으면 다음 실행이 같은 결함을 '이미 성공'으로 재생한다.
    """
    return annotation.status == ANNOTATION_ACCEPTED


def annotation_cache_document(
    fingerprint: str, annotations: Sequence[SemanticAnnotation]
) -> dict[str, object]:
    """성공 판정만 담는 캐시 문서 — 지문이 같은 실행만 재생한다."""
    return {
        "fingerprint": fingerprint,
        "annotations": [annotation_json(item) for item in annotations if cacheable(item)],
    }


# ─────────────────────────────────────────────────────────────
#  직렬화와 문서 조립
# ─────────────────────────────────────────────────────────────


def annotation_json(annotation: SemanticAnnotation) -> dict[str, object]:
    """판정의 JSON — 상태·근거·지문까지 전부 실린다."""
    return {
        "window_id": annotation.window_id,
        "status": annotation.status,
        "request_sha256": annotation.request_sha256,
        "luna_model": annotation.luna_model,
        "prompt_revision": annotation.prompt_revision,
        "origin": annotation.origin,
        "speech_summary": annotation.speech_summary,
        "visual_summary": annotation.visual_summary,
        "scene_tags": list(annotation.scene_tags),
        "evidence_ids": list(annotation.evidence_ids),
        "suggested_boundary_ids": list(annotation.suggested_boundary_ids),
        "editing_notes": list(annotation.editing_notes),
        "uncertainties": list(annotation.uncertainties),
        "needs_review": annotation.needs_review,
        "defect": annotation.defect,
    }


def stage1_document(
    *,
    audio: SelectedAudio,
    asr: AsrEvidence,
    utterances: Sequence[UtteranceEvidence],
    plan: WindowPlan,
    frames: FrameSamplingResult,
    segmentation: SegmentationProposal,
    annotations: Sequence[SemanticAnnotation],
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    fingerprint: str,
) -> dict[str, object]:
    """첫 단계 문서를 조립한다 — 실행 모드는 설정이 말한다(replay/live).

    `network_mode`가 online이어도 실제 호출이 없었다면 문서는 replay다 — 이
    함수는 기록의 진실만 말한다.
    """
    return stage1_evidence_document(
        audio=audio,
        asr=asr,
        utterances=utterances,
        plan=plan,
        frames=frames,
        segmentation=segmentation,
        annotations=[annotation_json(item) for item in annotations],
        models={
            "asr": workflow.models.timing_model,
            "luna": workflow.models.luna_model,
        },
        prompt_revision=settings.prompt_revision,
        fingerprint=fingerprint,
        execution_mode="online_live" if workflow.network_mode == "online" else "offline_replay",
    )
