"""비전 판정 — 요청 조립과 응답 정규화 (P5-05).

이 모듈은 **모델에게 무엇을 보낼지 만들고, 돌아온 답을 안전한 판정으로 바꾸는**
것만 한다. 스스로는 아무것도 부르지 않는다 — 유일한 비전 호출 지점은 레거시 공개
경계 `vlogsrt.transcribe.request_vision`이다(불변 규칙 5·7과 같은 선). 지키는 선:

- **모델에게 권한이 아니라 증거만 간다.** 프레임은 Base64 `image_url` 데이터로
  **공급된 순서 그대로** 들어가고, 후보는 우리가 만든 아이디로만 소개된다.
  XML 경로·파일 경로·시각·음소거 권한은 페이로드에 실리지 않는다 — 후보 창의
  초 숫자조차 나가지 않는다. 모델이 돌려줄 수 있는 것은 후보 아이디 하나뿐이다.
- **파서는 언제나 블록을 보존한다.** 모르는 아이디·NaN·무한대·범위 밖 신뢰도·
  깨진 꼴은 전부 오류 상태의 `keep_all` 리뷰로 정규화된다. 어떤 응답도 편집을
  스스로 만들거나 지을 수 없다.
- **승인은 세 조건의 교집합이다.** 알려진 후보 + 명시적 `reason_kind`
  `repetitive_ordinary_broll` + 신뢰도 ≥ `cfg.vision_apply_confidence`. 한 번뿐인
  사건(`unique_event`)과 `uncertain`은 **숫자 신뢰도와 무관하게** `keep_all`로
  정규화된다 — "한 번뿐인 장면"을 높은 확신으로 잘라내는 일이 없게 하기 위해서다.
- **모든 임계값은 설정에서 온다.** 프레임 수 한도는 `cfg.vision_max_images`,
  적용 신뢰도는 `cfg.vision_apply_confidence`. `reason_kind` 토큰은 모델과의
  계약 철자라 이 파일에 얼어 있다.

이미지는 P5-04가 뽑아 해시까지 매긴 파일이다 — 조립은 결정적이지만 바이트는
디스크에서 읽고, 읽은 바이트의 해시가 샘플 해시와 다르면 닫는다. 낡은 이미지를
유료 호출에 실어 보내는 길이 없다. 실제 계정 호환성(Base64 `image_url` 수용
여부)은 PENDING_HUMAN — 여기서는 가짜로만 증명한다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.vision.sampling import FrameSample

KEEP_ALL = "keep_all"
"""모델이 고를 수 있는 보존 선택 — 블록을 통째로 둔다."""

UNCERTAIN = "uncertain"
"""모델이 고를 수 있는 미확신 선택 — 정규화되면 keep_all과 같다."""

REASON_REPETITIVE_BROLL = "repetitive_ordinary_broll"
"""승인이 유일하게 허용되는 사유 — 되풀이되는 평범한 B-roll이라는 명시적 분류."""

REASON_UNIQUE_EVENT = "unique_event"
"""한 번뿐인 장면 분류 — 후보를 골랐어도 keep_all로 정규화된다."""

REASON_KINDS = frozenset({REASON_REPETITIVE_BROLL, REASON_UNIQUE_EVENT})
"""사유 분류의 닫힌 목록 — 이 밖의 토큰은 계약 위반이다."""

_IMAGE_DATA_URL_PREFIX = "data:image/jpeg;base64,"
"""P5-04 추출이 내는 JPEG — 다른 형식은 이 계층에 들어오지 않는다."""


class VisionPolicyError(RuntimeError):
    """조립이 나가기 전에 닫힌 상황 — 메시지가 아이디·값을 말한다."""


@dataclass(frozen=True, slots=True)
class VisionCandidate:
    """후보 하나 — 우리가 만든 유지 창. 블록 안 상대 시각의 정확 유리수다."""

    candidate_id: str
    keep_start: Fraction
    keep_end: Fraction


@dataclass(frozen=True, slots=True)
class VisionRequest:
    """블록 하나에 대한 질문.

    `frame_ids`는 P6가 정한 그대로(1fps 표본 + 양 끝 + 후보 중앙)를 뜻하고,
    여기서는 **순서까지 포함해 그대로** 쓴다 — 다시 고르지 않는다. 창의 초 숫자는
    후보 정렬에만 쓰고 페이로드에는 나가지 않는다.
    """

    block_id: str
    frame_ids: tuple[str, ...]
    candidates: tuple[VisionCandidate, ...]
    adjacent_context: str


@dataclass(frozen=True, slots=True)
class VisionDecision:
    """정규화된 판정. `status`만 권한을 뜻한다 — `accepted`만 적용될 수 있다.

    오류·보존 판정의 `choice`는 언제나 `keep_all`이다(블록 보존). `reason`은
    정규화 사유를 앞에 달고 모델의 원문을 지우지 않는다 — 리뷰 기록물의 재료다.
    """

    block_id: str
    choice: str
    reason: str
    evidence_frame_ids: tuple[str, ...]
    confidence: float
    status: Literal["accepted", "keep_all", "error"]


# ─────────────────────────────────────────────────────────────
#  조립 — 페이로드
# ─────────────────────────────────────────────────────────────


def build_payload(
    request: VisionRequest, samples: Sequence[FrameSample], cfg: ProjectConfig
) -> dict[str, object]:
    """요청과 프레임을 호출 페이로드로 만든다 — 깨진 요청은 여기서 닫는다.

    유료 호출은 깨진 요청에 값을 쓰지 않는다: 신원·한도·짝 검사가 이미지를 읽기
    전에 끝난다. 이미지는 요청 순서로, 글월 안내가 앞에 한 번.
    """
    _require_request(request, cfg)
    by_frame = _samples_by_frame_id(request.frame_ids, samples)
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _instruction(request)},
                    *(
                        {"type": "image_url", "image_url": {"url": _image_data_url(by_frame[id])}}
                        for id in request.frame_ids
                    ),
                ],
            }
        ]
    }


def _require_request(request: VisionRequest, cfg: ProjectConfig) -> None:
    """요청의 신원·타입·한도 — 조립과 해석 양쪽이 같은 검사를 지난다."""
    _require_text(request.block_id, "block_id")
    if not request.frame_ids:
        raise VisionPolicyError("frame_ids가 비었다 — 판정할 프레임이 하나도 없다")
    seen: set[str] = set()
    for frame_id in request.frame_ids:
        _require_text(frame_id, f"frame_id ({frame_id!r})")
        if frame_id in seen:
            raise VisionPolicyError(f"frame_id {frame_id!r}가 요청에 두 번 있다 — 고유해야 한다")
        seen.add(frame_id)
    if len(request.frame_ids) > cfg.vision_max_images:
        raise VisionPolicyError(
            f"프레임 {len(request.frame_ids)}개는 한도 {cfg.vision_max_images}개를 넘는다"
        )
    _require_candidates(request.candidates)
    if not isinstance(request.adjacent_context, str):
        raise VisionPolicyError(
            f"adjacent_context는 문자열이어야 한다: {request.adjacent_context!r}"
        )


def _require_candidates(candidates: tuple[VisionCandidate, ...]) -> None:
    if not candidates:
        raise VisionPolicyError("candidates가 비었다 — 고를 후보가 하나도 없다")
    seen: set[str] = set()
    for candidate in candidates:
        _require_text(candidate.candidate_id, f"candidate_id ({candidate.candidate_id!r})")
        if candidate.candidate_id in seen:
            raise VisionPolicyError(
                f"candidate_id {candidate.candidate_id!r}가 요청에 두 번 있다 — 고유해야 한다"
            )
        seen.add(candidate.candidate_id)
        for name, value in (("keep_start", candidate.keep_start), ("keep_end", candidate.keep_end)):
            if not isinstance(value, Fraction):
                raise VisionPolicyError(
                    f"{name}은 정확한 Fraction이어야 한다 — candidate_id"
                    f" {candidate.candidate_id!r}, 값 {value!r}"
                )
        if candidate.keep_start < 0:
            raise VisionPolicyError(
                f"keep_start는 0 이상이어야 한다 — candidate_id {candidate.candidate_id!r},"
                f" 값 {candidate.keep_start}"
            )
        if candidate.keep_end <= candidate.keep_start:
            raise VisionPolicyError(
                f"keep_end는 keep_start보다 커야 한다 — candidate_id"
                f" {candidate.candidate_id!r}, 창 [{candidate.keep_start},"
                f" {candidate.keep_end})"
            )


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise VisionPolicyError(f"{label}는 비지 않은 문자열이어야 한다: {value!r}")


def _samples_by_frame_id(
    frame_ids: tuple[str, ...], samples: Sequence[FrameSample]
) -> dict[str, FrameSample]:
    """프레임마다 샘플 하나 — 모자라도 넘쳐도 조립은 나가지 않는다."""
    by_frame: dict[str, FrameSample] = {}
    for sample in samples:
        if not isinstance(sample, FrameSample):
            raise VisionPolicyError(
                f"샘플이 FrameSample이 아니다: {type(sample).__name__} — P5-04 결과만 받는다"
            )
        if sample.frame_id in by_frame:
            raise VisionPolicyError(
                f"frame_id {sample.frame_id!r}의 샘플이 두 번 왔다 — 하나여야 한다"
            )
        by_frame[sample.frame_id] = sample
    missing = [frame_id for frame_id in frame_ids if frame_id not in by_frame]
    if missing:
        raise VisionPolicyError(f"요청한 프레임에 샘플이 없다: {missing}")
    extra = sorted(set(by_frame) - set(frame_ids))
    if extra:
        raise VisionPolicyError(f"요청하지 않은 프레임 샘플이 왔다: {extra}")
    return by_frame


def _image_data_url(sample: FrameSample) -> str:
    """한 장을 데이터 URL로 — 바이트 해시가 샘플 해시와 다르면 닫는다."""
    try:
        raw = sample.image_path.read_bytes()
    except OSError as exc:
        raise VisionPolicyError(
            f"프레임 이미지를 읽지 못했다 — frame_id {sample.frame_id!r},"
            f" {sample.image_path} ({exc})"
        ) from exc
    if hashlib.sha256(raw).hexdigest() != sample.image_sha256:
        raise VisionPolicyError(
            f"프레임 이미지가 샘플 해시와 다르다 — frame_id {sample.frame_id!r},"
            f" {sample.image_path}"
        )
    return _IMAGE_DATA_URL_PREFIX + base64.b64encode(raw).decode("ascii")


def _instruction(request: VisionRequest) -> str:
    """글월 안내 — 후보는 앞쪽(keep_start)부터, 프레임은 시간 순으로만 나열한다."""
    candidates = ", ".join(
        candidate.candidate_id
        for candidate in sorted(
            request.candidates, key=lambda c: (c.keep_start, c.keep_end, c.candidate_id)
        )
    )
    context = request.adjacent_context.strip() or "없음"
    return (
        "되풀이되는 평범한 B-roll 블록 하나를 판정한다. 프레임은 시간 순서대로 나열되어 있다.\n"
        "\n"
        "규칙:\n"
        "- 블록 전체가 같은 내용의 단순 되풀이이고 인물의 말이나 특별한 순간이 없을 때만"
        " 후보를 고른다.\n"
        "- 한 번뿐인 사건이나 대사가 섞여 있으면 keep_all, 어느 쪽도 확실하지 않으면"
        " uncertain을 고른다.\n"
        "- reason_kind는 되풀이 평범 B-roll이면 repetitive_ordinary_broll,"
        " 그 외(한 번뿐인 장면 포함)는 unique_event다.\n"
        "\n"
        '답은 다음 JSON 객체로만 한다: {"choice": "<후보 아이디|keep_all|uncertain>",'
        ' "reason": "<근거>", "reason_kind": "repetitive_ordinary_broll|unique_event",'
        ' "evidence_frame_ids": ["<프레임 아이디>"], "confidence": <0.0에서 1.0 사이 실수>}\n'
        "\n"
        f"후보(블록 앞쪽부터): {candidates}\n"
        f"프레임(시간 순): {', '.join(request.frame_ids)}\n"
        f"이웃 맥락: {context}"
    )


# ─────────────────────────────────────────────────────────────
#  해석 — 정규화
# ─────────────────────────────────────────────────────────────


def parse_decision(
    request: VisionRequest, raw: Mapping[str, object], cfg: ProjectConfig
) -> VisionDecision:
    """돌아온 응답을 판정으로 정규화한다 — 응답 결함은 예외가 아니라 오류 판정이다.

    요청 결함은 프로그래머 잘못이라 닫히고(`build_payload`와 같은 검사), 응답
    결함은 모델 잘못이라 `keep_all` 오류 판정으로 남는다. 둘을 섞으면 모델이
    보낸 쓰레기를 우리 버그로 오독하게 된다.
    """
    _require_request(request, cfg)
    try:
        choice, reason, reason_kind, evidence, confidence = _decision_fields(request, raw)
    except VisionPolicyError as exc:
        return _error_decision(request, str(exc))
    return _normalize(request, choice, reason, reason_kind, evidence, confidence, cfg)


def _decision_fields(
    request: VisionRequest, raw: Mapping[str, object]
) -> tuple[str, str, str, list[str], float]:
    """응답에서 다섯 값을 꺼낸다 — 위반 하나만 골라 VisionPolicyError로 던진다."""
    if not isinstance(raw, Mapping):
        raise VisionPolicyError(f"응답이 JSON 객체가 아니다: {type(raw).__name__}")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise VisionPolicyError(f"choices가 정확히 하나인 리스트가 아니다: {choices!r}")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise VisionPolicyError(f"message.content가 문자열이 아니다: {content!r}")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise VisionPolicyError(f"message.content가 JSON이 아니다: {content!r} ({exc})") from exc
    if not isinstance(parsed, dict):
        raise VisionPolicyError(f"답이 JSON 객체가 아니다: {type(parsed).__name__}")
    return _body_fields(request, parsed)


def _body_fields(
    request: VisionRequest, body: Mapping[str, object]
) -> tuple[str, str, str, list[str], float]:
    """다섯 키의 타입·값역 검사 — 모르는 키는 무시한다(판정이 모델 덧칠에 무너지지 않게)."""
    known = {candidate.candidate_id for candidate in request.candidates}
    choice = body.get("choice")
    if not isinstance(choice, str) or choice not in known | {KEEP_ALL, UNCERTAIN}:
        raise VisionPolicyError(f"choice가 알려진 후보·keep_all·uncertain이 아니다: {choice!r}")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise VisionPolicyError(f"reason이 비지 않은 문자열이 아니다: {reason!r}")
    reason_kind = body.get("reason_kind")
    if not isinstance(reason_kind, str) or reason_kind not in REASON_KINDS:
        raise VisionPolicyError(f"reason_kind가 알려진 분류가 아니다: {reason_kind!r}")
    evidence = body.get("evidence_frame_ids")
    if not isinstance(evidence, list):
        raise VisionPolicyError(f"evidence_frame_ids가 리스트가 아니다: {evidence!r}")
    for frame_id in evidence:
        if not isinstance(frame_id, str):
            raise VisionPolicyError(f"증거 아이디가 문자열이 아니다: {frame_id!r}")
        if frame_id not in request.frame_ids:
            raise VisionPolicyError(f"증거가 보내지 않은 프레임을 가리킨다: {frame_id!r}")
    if len(set(evidence)) != len(evidence):
        raise VisionPolicyError(f"증거 아이디가 겹친다: {evidence!r}")
    confidence = body.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise VisionPolicyError(f"confidence가 0 이상 1 이하의 유한 실수가 아니다: {confidence!r}")
    return choice, reason, reason_kind, list(evidence), float(confidence)


def _normalize(
    request: VisionRequest,
    choice: str,
    reason: str,
    reason_kind: str,
    evidence: list[str],
    confidence: float,
    cfg: ProjectConfig,
) -> VisionDecision:
    """승인은 세 조건의 교집합 — 나머지는 전부 블록을 보존하는 keep_all이다."""
    if choice in (KEEP_ALL, UNCERTAIN):
        return VisionDecision(
            request.block_id, KEEP_ALL, reason, tuple(evidence), confidence, "keep_all"
        )
    if reason_kind != REASON_REPETITIVE_BROLL:
        return VisionDecision(
            request.block_id,
            KEEP_ALL,
            f"unique_event 정규화 — 한 번뿐인 사건은 신뢰도와 상관없이 keep_all: {reason}",
            tuple(evidence),
            confidence,
            "keep_all",
        )
    if confidence < cfg.vision_apply_confidence:
        return VisionDecision(
            request.block_id,
            KEEP_ALL,
            f"신뢰도 {confidence}가 적용 한도 {cfg.vision_apply_confidence}에 못 미친다: {reason}",
            tuple(evidence),
            confidence,
            "keep_all",
        )
    return VisionDecision(request.block_id, choice, reason, tuple(evidence), confidence, "accepted")


def _error_decision(request: VisionRequest, defect: str) -> VisionDecision:
    """깨진 응답의 정규형 — 블록은 보존되고 결함은 리뷰에 남는다."""
    return VisionDecision(
        block_id=request.block_id,
        choice=KEEP_ALL,
        reason=f"비전 응답이 계약을 어겼다 — {defect}",
        evidence_frame_ids=(),
        confidence=0.0,
        status="error",
    )
