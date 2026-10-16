"""호출 전 내구 예산 예약 원장(STAB-S2B-1).

유료 요청을 보내도 되는지는 이 원장이 정한다. 규칙은 하나다: **예약이 디스크에
남는 것이 곧 허가다.** 쓰기·fsync·교체 어느 단계에서 실패하면 허가는 없고,
그 객체는 다시 열어 검증받기 전까지 못 쓴다.

- 열기: 같은 신원·승인 상한으로만 재오픈한다. 다르면 거절하고 파일은 손대지
  않는다 — 조용히 비우고 다시 초기화하는 길은 없다.
- 예약: 남은 상한 안에 들어오면 디스크에 남긴 뒤 허가를 돌려준다. 모자라면
  기록 없이 거절한다. 같은 요청 ID는 두 번 허가하지 않는다.
- 미지수: 결측·오류·취소는 0USD도 만료도 환불도 아니다 — 상한을 그대로 묶는다.
- 확정: 검증된 ``ActualUsage``로 딱 한 번 예약 상한을 실측으로 갈아끼운다.
  실측이 상한을 넘어도 기록은 남긴다(초과 증거를 버리지 않는다).

모든 변경은 잠금 안에서 디스크를 다시 읽어 검사한다 — 기억을 믿지 않고
독립 인스턴스·스레드가 서로의 쓰기를 잃지 않는다. 합계는 저장하지 않는다;
매번 검증된 기록에서 다시 계산한다.

이 모듈은 전송 계약(``workflow_protocol``)의 인코딩과는 독립적이다. 다만
확정 입력은 그 계약의 ``ActualUsage``를 그대로 받아 재검증한다.
"""

from __future__ import annotations

import decimal
import fcntl
import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from vlogsrt.project.workflow_protocol import ActualUsage

LEDGER_VERSION = 1

# 금액 영역. 기본 십진 문맥(정밀도 28)에서 나오는 실제 원가 — 이를테면
# `workflow_analysis.transcription_usage`가 Fraction(1,3) 오디오로 만드는
# 28자리 원가 — 는 살아야 한다. 그래서 유효 숫자 30자리(28 + 여유),
# 소수 지수 −42, 크기 상한 10^24. 영역 밖은 입구에서 거절한다 —
# "1E+999999999" 같은 유한 문자열이 고정소수 문면으로 펴지며 기억장치를
# 잡아먹는 일도, 합산이 조용히 자리올림되는 일도 없어야 한다.
_MAX_SIGNIFICANT_DIGITS = 30
_MAX_ADJUSTED_EXPONENT = 24
_MIN_AMOUNT_EXPONENT = -42

# 영역 안의 금액은 이 문맥에서 절대 자리올림되지 않는다(최대 전개
# 24 − (−42) + 1 = 67자리 + 합산 올림 여유 ≪ 80). 영역을 벗어나는 연산은
# 함정(trap)이 예외로 막는다 — 조용한 반올림은 남은 상한이 새는 통로다.
_LEDGER_DECIMAL = decimal.Context(
    prec=80,
    traps=[
        decimal.InvalidOperation,
        decimal.DivisionByZero,
        decimal.Overflow,
        decimal.Underflow,
        decimal.Rounded,
        decimal.Inexact,
    ],
)

ReservationState: TypeAlias = Literal["reserved", "unknown", "reconciled"]

_LEDGER_KEYS = frozenset({"approval_limit", "identity", "ledger_version", "records"})
_IDENTITY_KEYS = frozenset(
    {"authorization_id", "input_revision", "policy_digest", "pricing_revision"}
)
_RECORD_KEYS = frozenset(
    {
        "attempt_id",
        "bound",
        "model",
        "reconciled_at",
        "request_digest",
        "request_id",
        "reserved_at",
        "state",
        "unknown_at",
        "usage",
    }
)
_USAGE_KEYS = frozenset(
    {
        "attempt_id",
        "audio_seconds",
        "cost",
        "input_revision",
        "input_tokens",
        "model",
        "output_tokens",
        "usage_revision",
        "window_id",
    }
)
_STATES = frozenset({"reserved", "unknown", "reconciled"})


class BudgetLedgerError(ValueError):
    """원장 거절·검증 실패의 공통 기반 — 전부 '닫힌 쪽'으로 간다."""


class InsufficientBudget(BudgetLedgerError):
    """예약이 남은 승인 상한을 넘는다 — 요청을 보내지 않는다."""


class DuplicateRequest(BudgetLedgerError):
    """같은 요청 ID가 이미 허가를 받았다 — 두 번째 송신 허가는 없다."""


class ReconciliationConflict(BudgetLedgerError):
    """확정 입력이 예약·원장 신원과 충돌한다."""


class UnknownRequest(BudgetLedgerError):
    """원장에 없는 요청이다."""


class LedgerCorrupt(BudgetLedgerError):
    """디스크의 원장이 계약에 맞지 않는다 — 다른 예약보다 먼저 거절한다."""


class LedgerIdentityMismatch(BudgetLedgerError):
    """다른 신원·승인 상한으로 묶인 원장이다 — 다시 초기화하지 않는다."""


class LedgerUnavailable(RuntimeError):
    """내구 쓰기에 실패했다 — 허가하지 않는다. 다시 열어 검증해야 한다."""


@dataclass(frozen=True, slots=True)
class LedgerIdentity:
    """원장 신원 — 네 불투명 SHA-256이 전부다.

    ``policy_digest``는 유효 구성·정책·엔드포인트를 **비밀이 아닌 재료로만**
    묶은 호출자 계산 다이제스트다. 원장은 이를 그대로만 저장하며, 비밀이나
    엔드포인트 자격증명 원문은 애초에 표현할 자리가 없다.
    """

    input_revision: str
    policy_digest: str
    pricing_revision: str
    authorization_id: str

    def __post_init__(self) -> None:
        _require_sha256(self.input_revision, "입력 리비전")
        _require_sha256(self.policy_digest, "정책 다이제스트")
        _require_sha256(self.pricing_revision, "가격 리비전")
        _require_sha256(self.authorization_id, "승인 신원")

    def payload(self) -> dict[str, str]:
        return {
            "authorization_id": self.authorization_id,
            "input_revision": self.input_revision,
            "policy_digest": self.policy_digest,
            "pricing_revision": self.pricing_revision,
        }


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    """예약 한 건 — 상태는 reserved → (unknown) → reconciled 한 방향이다."""

    request_id: str
    attempt_id: str
    request_digest: str
    model: str
    bound: Decimal
    state: ReservationState
    usage: ActualUsage | None
    reserved_at: str
    unknown_at: str | None
    reconciled_at: str | None


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """원장의 현재량 — 합계는 매번 기록에서 다시 계산한 값이다."""

    identity: LedgerIdentity
    approval_limit: Decimal
    measured_actual: Decimal
    reserved_unresolved: Decimal
    consumed_bound: Decimal
    remaining_bound: Decimal
    records: tuple[ReservationRecord, ...]


@dataclass(frozen=True, slots=True)
class _LedgerFile:
    identity: LedgerIdentity
    approval_limit: Decimal
    records: tuple[ReservationRecord, ...]


@dataclass(frozen=True, slots=True)
class _Totals:
    measured_actual: Decimal
    reserved_unresolved: Decimal
    consumed_bound: Decimal
    remaining_bound: Decimal


def _require_sha256(value: str, label: str) -> None:
    # 16진 64원소 리스트도 len·순회를 통과한다 — 문자열인지부터 본다.
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BudgetLedgerError(f"{label} SHA-256이 아니다: {value!r}")


def _require_amount_domain(value: Decimal, label: str) -> None:
    """지원 금액 영역 안인지 본다 — 값 기준이라 표현이 바뀌어도 같게 판정한다.

    끝자리 0은 표현일 뿐이다(1E+24 ↔ 1000…0). 자릿수·지수는 최소 표현
    기준으로 세므로 직렬화 왕복(고정소수 문면 ↔ 재해석)에 같은 판정이 나온다.
    """
    decimal_tuple = value.as_tuple()
    digits = decimal_tuple.digits
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        # 유한 검사를 건너뛰는 길(원가 문자열 직접 구성)의 NaN/Inf — 여기서 닫는다.
        raise BudgetLedgerError(f"{label}이 유한한 십진수가 아니다: {value}")
    significant = len(digits)
    while significant > 1 and digits[significant - 1] == 0:
        significant -= 1
    minimal_exponent = exponent + (len(digits) - significant)
    if significant > _MAX_SIGNIFICANT_DIGITS:
        raise BudgetLedgerError(
            f"{label}이 유효 숫자 {_MAX_SIGNIFICANT_DIGITS}자리를 넘는다: {value}"
        )
    if minimal_exponent < _MIN_AMOUNT_EXPONENT or (
        minimal_exponent + significant - 1 > _MAX_ADJUSTED_EXPONENT
    ):
        raise BudgetLedgerError(
            f"{label}이 지원 소수/크기 범위"
            f"(10^{_MIN_AMOUNT_EXPONENT}~10^{_MAX_ADJUSTED_EXPONENT})를 벗어난다: {value}"
        )


def _decimal_amount(value: Decimal, label: str) -> Decimal:
    """API 입구의 금액 검사 — Decimal이 아니면(bool·int·float) 닫는다."""
    if not isinstance(value, Decimal):
        raise BudgetLedgerError(f"{label}은 Decimal이어야 한다: {value!r}")
    if not value.is_finite() or value < 0:
        raise BudgetLedgerError(f"{label}은 유한한 0 이상이어야 한다: {value}")
    _require_amount_domain(value, label)
    return value


def _decimal_from_text(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise LedgerCorrupt(f"{label}은 십진수 문자열이어야 한다: {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerCorrupt(f"{label}이 십진수가 아니다: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise LedgerCorrupt(f"{label}은 유한한 0 이상이어야 한다: {value!r}")
    try:
        _require_amount_domain(parsed, label)
    except BudgetLedgerError as exc:
        raise LedgerCorrupt(str(exc)) from exc
    return parsed


def _decimal_text(value: Decimal) -> str:
    # normalize()는 문맥 정밀도를 따른다 — 저장 문면도 원장 문맥에서 만든다.
    with decimal.localcontext(_LEDGER_DECIMAL):
        if value == 0:
            return "0"
        try:
            return format(value.normalize(), "f")
        except decimal.DecimalException as exc:
            raise BudgetLedgerError(f"금액 문면화가 지원 영역을 벗어났다: {value}") from exc


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BudgetLedgerError(f"{label}은 비지 않은 문자열이어야 한다")
    return value


def _require_usage_field_types(
    *,
    input_tokens: int,
    output_tokens: int,
    audio_seconds: Fraction,
    window_id: str | None,
    cost: str,
) -> None:
    """bool은 ``min() < 0`` 검사를 int로 위장해 통과한다 — 정확한 타입만 받는다."""
    for name, tokens in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise BudgetLedgerError(f"사용량 {name}은 0 이상 정수여야 한다(bool은 정수가 아니다)")
    if not isinstance(audio_seconds, Fraction) or audio_seconds < 0:
        raise BudgetLedgerError("사용량 오디오 길이는 0 이상 Fraction이어야 한다")
    if window_id is not None and not isinstance(window_id, str):
        raise BudgetLedgerError("사용량 창 ID는 문자열 또는 null이어야 한다")
    if not isinstance(cost, str):
        raise BudgetLedgerError("사용량 원가는 십진수 문자열이어야 한다")


def _require_cost_amount(cost: str) -> Decimal:
    try:
        value = Decimal(cost)
    except InvalidOperation as exc:
        raise BudgetLedgerError(f"사용량 원가가 십진수가 아니다: {cost!r}") from exc
    _require_amount_domain(value, "사용량 원가")
    return value


def _rebuilt_usage(
    *,
    input_revision: str,
    attempt_id: str,
    window_id: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    audio_seconds: Fraction,
    cost: str,
) -> ActualUsage:
    """원장 문맥 안에서 ``ActualUsage.create``로 다시 만든다.

    프로토콜의 원가 정규화는 주변 문맥 정밀도를 따른다 — 여기서는 원장
    문맥으로 묶어 다이제스트가 환경마다 달라지지 않게 한다.
    """
    try:
        with decimal.localcontext(_LEDGER_DECIMAL):
            return ActualUsage.create(
                input_revision=input_revision,
                attempt_id=attempt_id,
                window_id=window_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                audio_seconds=audio_seconds,
                cost=cost,
            )
    except (ValueError, decimal.DecimalException) as exc:
        raise BudgetLedgerError(f"사용량이 ActualUsage 계약에 맞지 않는다: {exc}") from exc


def _usage_from_fields(
    *,
    input_revision: str,
    attempt_id: str,
    window_id: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    audio_seconds: Fraction,
    cost: str,
) -> ActualUsage:
    """``ActualUsage.create``로 다시 만들어 원본 계약 검증을 그대로 밟게 한다."""
    _require_usage_field_types(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        audio_seconds=audio_seconds,
        window_id=window_id,
        cost=cost,
    )
    cost_value = _require_cost_amount(cost)
    rebuilt = _rebuilt_usage(
        input_revision=input_revision,
        attempt_id=attempt_id,
        window_id=window_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        audio_seconds=audio_seconds,
        cost=cost,
    )
    if Decimal(rebuilt.cost) != cost_value:
        raise BudgetLedgerError("사용량 원가가 정규화 뒤 값을 유지하지 못했다(자리올림·훼손)")
    return rebuilt


def _validated_usage(usage: ActualUsage) -> ActualUsage:
    """확정 입구 재검증 — 직접 구성된 ActualUsage도 위조 다이제스트를 못 넘긴다."""
    rebuilt = _usage_from_fields(
        input_revision=usage.input_revision,
        attempt_id=usage.attempt_id,
        window_id=usage.window_id,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        audio_seconds=usage.audio_seconds,
        cost=usage.cost,
    )
    if rebuilt.usage_revision != usage.usage_revision:
        raise BudgetLedgerError("사용량 다이제스트가 필드와 맞지 않는다(위조·훼손)")
    return rebuilt


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LedgerCorrupt(f"{key}는 비지 않은 문자열이어야 한다")
    return value


def _payload_optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise LedgerCorrupt(f"{key}는 비지 않은 문자열 또는 null이어야 한다")
    return value


def _usage_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerCorrupt(f"사용량 {key}은 0 이상 정수여야 한다")
    return value


def _usage_fraction(payload: Mapping[str, object], key: str) -> Fraction:
    value = payload.get(key)
    if not isinstance(value, str):
        raise LedgerCorrupt(f"사용량 {key}은 분수 문자열이어야 한다")
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise LedgerCorrupt(f"사용량 {key}이 분수가 아니다: {value!r}") from exc
    if parsed < 0:
        raise LedgerCorrupt(f"사용량 {key}은 0 이상이어야 한다")
    return parsed


def _usage_from_payload(payload: object) -> ActualUsage:
    if not isinstance(payload, dict) or set(payload) != _USAGE_KEYS:
        raise LedgerCorrupt("사용량 블록 모양이 계약과 다르다")
    window_id = payload["window_id"]
    if window_id is not None and not isinstance(window_id, str):
        raise LedgerCorrupt("사용량 창 ID는 문자열 또는 null이어야 한다")
    revision = _payload_text(payload, "usage_revision")
    try:
        usage = _usage_from_fields(
            input_revision=_payload_text(payload, "input_revision"),
            attempt_id=_payload_text(payload, "attempt_id"),
            window_id=window_id,
            model=_payload_text(payload, "model"),
            input_tokens=_usage_integer(payload, "input_tokens"),
            output_tokens=_usage_integer(payload, "output_tokens"),
            audio_seconds=_usage_fraction(payload, "audio_seconds"),
            cost=_payload_text(payload, "cost"),
        )
    except BudgetLedgerError as exc:
        raise LedgerCorrupt(str(exc)) from exc
    if usage.usage_revision != revision:
        raise LedgerCorrupt("사용량 다이제스트가 필드와 맞지 않는다(훼손)")
    return usage


def _parse_identity(payload: object) -> LedgerIdentity:
    if not isinstance(payload, dict) or set(payload) != _IDENTITY_KEYS:
        raise LedgerCorrupt("원장 신원 블록이 계약의 네 다이제스트와 다르다")
    fields = {key: _payload_text(payload, key) for key in _IDENTITY_KEYS}
    try:
        return LedgerIdentity(
            input_revision=fields["input_revision"],
            policy_digest=fields["policy_digest"],
            pricing_revision=fields["pricing_revision"],
            authorization_id=fields["authorization_id"],
        )
    except BudgetLedgerError as exc:
        raise LedgerCorrupt(str(exc)) from exc


def _parse_record(payload: object, identity: LedgerIdentity) -> ReservationRecord:
    if not isinstance(payload, dict) or set(payload) != _RECORD_KEYS:
        raise LedgerCorrupt("예약 기록 모양이 계약과 다르다")
    state_value = payload["state"]
    # 해시 불가능한 값(객체·배열)은 멤버십 검사에서 TypeError를 내므로 먼저 가른다.
    if not isinstance(state_value, str) or state_value not in _STATES:
        raise LedgerCorrupt(f"알 수 없는 예약 상태다: {state_value!r}")
    state = cast(ReservationState, state_value)
    request_digest = _payload_text(payload, "request_digest")
    try:
        _require_sha256(request_digest, "요청 다이제스트")
    except BudgetLedgerError as exc:
        raise LedgerCorrupt(str(exc)) from exc
    reconciled_at = _payload_optional_text(payload, "reconciled_at")
    unknown_at = _payload_optional_text(payload, "unknown_at")
    if state == "reserved" and unknown_at is not None:
        raise LedgerCorrupt("reserved 기록은 미지수 표시를 가질 수 없다")
    if state == "unknown" and unknown_at is None:
        raise LedgerCorrupt("unknown 기록에는 미지수 표시가 필요하다")
    usage = _parse_record_usage(payload["usage"], state, reconciled_at)
    record = ReservationRecord(
        request_id=_payload_text(payload, "request_id"),
        attempt_id=_payload_text(payload, "attempt_id"),
        request_digest=request_digest,
        model=_payload_text(payload, "model"),
        bound=_decimal_from_text(payload["bound"], "예약 상한"),
        state=state,
        usage=usage,
        reserved_at=_payload_text(payload, "reserved_at"),
        unknown_at=unknown_at,
        reconciled_at=reconciled_at,
    )
    _require_record_match(record, identity)
    return record


def _parse_record_usage(
    payload: object, state: str, reconciled_at: str | None
) -> ActualUsage | None:
    if state != "reconciled":
        if payload is not None or reconciled_at is not None:
            raise LedgerCorrupt("확정 전 기록은 사용량·확정 시각을 가질 수 없다")
        return None
    if payload is None or reconciled_at is None:
        raise LedgerCorrupt("확정 기록에는 사용량·확정 시각이 필요하다")
    return _usage_from_payload(payload)


def _require_record_match(record: ReservationRecord, identity: LedgerIdentity) -> None:
    if record.usage is None:
        return
    if record.usage.input_revision != identity.input_revision:
        raise LedgerCorrupt("기록의 사용량이 원장 신원의 입력 리비전과 다르다")
    if record.usage.attempt_id != record.attempt_id:
        raise LedgerCorrupt("기록의 사용량이 예약의 시도 ID와 다르다")
    if record.usage.model != record.model:
        raise LedgerCorrupt("기록의 사용량이 예약의 모델과 다르다")


def _parse_ledger(document: dict[str, object]) -> _LedgerFile:
    if set(document) != _LEDGER_KEYS:
        raise LedgerCorrupt(f"원장 키가 계약과 다르다: {sorted(document)}")
    version = document["ledger_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != LEDGER_VERSION:
        raise LedgerCorrupt(f"알 수 없는 원장 버전이다: {version!r}")
    identity = _parse_identity(document["identity"])
    records_payload = document["records"]
    if not isinstance(records_payload, list):
        raise LedgerCorrupt("원장 기록은 배열이어야 한다")
    records = tuple(_parse_record(payload, identity) for payload in records_payload)
    seen: set[str] = set()
    for record in records:
        if record.request_id in seen:
            raise LedgerCorrupt(f"요청 ID가 원장 안에서 중복된다: {record.request_id}")
        seen.add(record.request_id)
    return _LedgerFile(
        identity, _decimal_from_text(document["approval_limit"], "승인 상한"), records
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json.loads는 중복 키를 조용히 뒤 값으로 덮는다 — 충돌은 열기 전에 닫는다."""
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise LedgerCorrupt(f"원장 객체에 중복 키가 있다: {key}")
        document[key] = value
    return document


def _read_document(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LedgerCorrupt(f"원장을 읽을 수 없다: {path}") from exc
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerCorrupt(f"원장이 잘렸거나 해석할 수 없다: {path}") from exc
    if not isinstance(document, dict):
        raise LedgerCorrupt("원장 최상위 값은 객체여야 한다")
    return document


def _usage_payload(usage: ActualUsage) -> dict[str, object]:
    return {
        "attempt_id": usage.attempt_id,
        "audio_seconds": f"{usage.audio_seconds.numerator}/{usage.audio_seconds.denominator}",
        "cost": usage.cost,
        "input_revision": usage.input_revision,
        "input_tokens": usage.input_tokens,
        "model": usage.model,
        "output_tokens": usage.output_tokens,
        "usage_revision": usage.usage_revision,
        "window_id": usage.window_id,
    }


def _record_payload(record: ReservationRecord) -> dict[str, object]:
    return {
        "attempt_id": record.attempt_id,
        "bound": _decimal_text(record.bound),
        "model": record.model,
        "reconciled_at": record.reconciled_at,
        "request_digest": record.request_digest,
        "request_id": record.request_id,
        "reserved_at": record.reserved_at,
        "state": record.state,
        "unknown_at": record.unknown_at,
        "usage": None if record.usage is None else _usage_payload(record.usage),
    }


def _ledger_document(state: _LedgerFile) -> dict[str, object]:
    return {
        "approval_limit": _decimal_text(state.approval_limit),
        "identity": state.identity.payload(),
        "ledger_version": LEDGER_VERSION,
        "records": [_record_payload(record) for record in state.records],
    }


def _canonical_bytes(document: Mapping[str, object]) -> bytes:
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _write_durable_bytes(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_file(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_existing_parent(path: Path) -> None:
    """원장은 디렉터리를 만들지 않는다 — 조상 내구성은 호출자의 계약이다.

    mkdir(parents=…)로 만든 조상을 이 쪽에서 내구화하려 하면, fsync 실패 뒤
    재시도·동시 오픈이 '이미 있는' 조상을 건너뛰는 구멍이 생긴다 — 만들어졌지만
    아직 내구화되지 않은 상태를 파일시스템이 구별해 주지 않는다. 호출자는 이미
    내구화된 자리에 원장을 놓는다.
    """
    try:
        present = path.parent.is_dir()
    except OSError as exc:
        raise LedgerUnavailable(f"원장 디렉터리를 확인할 수 없다: {path.parent}") from exc
    if not present:
        raise LedgerUnavailable(f"원장 디렉터리가 없다 — 만들지 않는다: {path.parent}")


@contextmanager
def _ledger_lock(path: Path) -> Iterator[None]:
    """원장이 통째로 갈려도 잠금 대상은 변하지 않게 한다 — 전용 잠금 파일.

    잠금은 로컬 파일시스템의 POSIX advisory lock(flock)을 가정한다. 협조하지
    않는 쓰기(이 잠금을 무시하는 외부 프로세스)는 막지 못한다. 스트림을 닫으면
    잠금도 풀린다.
    """
    lock_path = path.with_name(path.name + ".lock")
    try:
        stream = lock_path.open("a")
    except OSError as exc:
        raise LedgerUnavailable(f"원장 잠금 파일을 열 수 없다: {lock_path}") from exc
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        stream.close()
        raise LedgerUnavailable(f"원장 잠금을 얻지 못했다: {lock_path}") from exc
    try:
        yield
    finally:
        stream.close()


def _totals(state: _LedgerFile) -> _Totals:
    # 문맥 정밀도가 차감을 자리올림하면 작은 예약이 남은 상한에서 사라진다.
    # 영역 안이라면 함정이 침묵하고, 벗어나면 예외로 막는다(조용한 반올림 없음).
    try:
        with decimal.localcontext(_LEDGER_DECIMAL):
            measured = sum(
                (
                    Decimal(record.usage.cost)
                    for record in state.records
                    if record.state == "reconciled" and record.usage is not None
                ),
                start=Decimal(0),
            )
            unresolved = sum(
                (record.bound for record in state.records if record.state != "reconciled"),
                start=Decimal(0),
            )
            consumed = measured + unresolved
            remaining = state.approval_limit - consumed
            if remaining < 0:
                remaining = Decimal(0)
    except decimal.DecimalException as exc:
        raise BudgetLedgerError(f"원장 합계가 지원 금액 영역을 벗어났다: {exc}") from exc
    return _Totals(measured, unresolved, consumed, remaining)


def _snapshot(state: _LedgerFile) -> LedgerSnapshot:
    totals = _totals(state)
    return LedgerSnapshot(
        identity=state.identity,
        approval_limit=state.approval_limit,
        measured_actual=totals.measured_actual,
        reserved_unresolved=totals.reserved_unresolved,
        consumed_bound=totals.consumed_bound,
        remaining_bound=totals.remaining_bound,
        records=state.records,
    )


def _find_record(
    records: tuple[ReservationRecord, ...], request_id: str
) -> ReservationRecord | None:
    for record in records:
        if record.request_id == request_id:
            return record
    return None


def _replaced_record(
    records: tuple[ReservationRecord, ...], updated: ReservationRecord
) -> tuple[ReservationRecord, ...]:
    return tuple(
        updated if record.request_id == updated.request_id else record for record in records
    )


def _require_request_arguments(
    request_id: str,
    attempt_id: str,
    request_digest: str,
    model: str,
    bound: Decimal,
    reserved_at: str,
) -> Decimal:
    """API 입구 검사 — 디스크를 touch하기 전에 끝낸다."""
    _nonempty(request_id, "요청 ID")
    _nonempty(attempt_id, "시도 ID")
    _require_sha256(request_digest, "요청 다이제스트")
    _nonempty(model, "모델")
    amount = _decimal_amount(bound, "예약 상한")
    _nonempty(reserved_at, "예약 시각")
    return amount


def _new_reservation(
    *,
    request_id: str,
    attempt_id: str,
    request_digest: str,
    model: str,
    bound: Decimal,
    reserved_at: str,
) -> ReservationRecord:
    return ReservationRecord(
        request_id=request_id,
        attempt_id=attempt_id,
        request_digest=request_digest,
        model=model,
        bound=bound,
        state="reserved",
        usage=None,
        reserved_at=reserved_at,
        unknown_at=None,
        reconciled_at=None,
    )


class BudgetLedger:
    """예약이 곧 허가인 내구 원장 — 디스크 파일이 유일한 정본이다.

    모든 변경은 (1) 잠금 획득 (2) 디스크 재독림·재검증 (3) 검사 (4) 원자적
    내구 쓰기 순서로만 일어난다. 메모리 상태는 신원·상한·막힘 여부뿐이다.
    """

    def __init__(self, path: Path, identity: LedgerIdentity, approval_limit: Decimal) -> None:
        self._path = path
        self._identity = identity
        self._approval_limit = approval_limit
        self._mutex = threading.RLock()
        self._poisoned: str | None = None

    @classmethod
    def open(cls, path: Path, *, identity: LedgerIdentity, approval_limit: Decimal) -> BudgetLedger:
        """같은 신원·상한으로 원장을 열거나 만든다 — 다르면 거절한다.

        원장은 디렉터리를 만들지 않는다: 호출자는 이미 내구화된 자리에
        놓는다(실패한 내구화를 재시도가 건너뛰는 구멍을 남기지 않으려면,
        만드는 쪽이 내구화까지 맡는 게 유일하게 정직하다).
        """
        limit = _decimal_amount(approval_limit, "승인 상한")
        ledger = cls(path, identity, limit)
        _require_existing_parent(path)
        with ledger._mutex, _ledger_lock(path):
            try:
                exists = path.exists()
            except OSError as exc:
                raise LedgerUnavailable(f"원장 자리를 확인할 수 없다: {path}") from exc
            if not exists:
                ledger._persist(_LedgerFile(identity, limit, ()))
                return ledger
            ledger._load()
            return ledger

    def reserve(
        self,
        *,
        request_id: str,
        attempt_id: str,
        request_digest: str,
        model: str,
        bound: Decimal,
        reserved_at: str,
    ) -> ReservationRecord:
        """보내도 된다는 허가 — 디스크에 남은 뒤에만 돌아온다."""
        amount = _require_request_arguments(
            request_id, attempt_id, request_digest, model, bound, reserved_at
        )
        with self._mutex:
            self._require_usable()
            with _ledger_lock(self._path):
                state = self._load()
                if _find_record(state.records, request_id) is not None:
                    raise DuplicateRequest(f"요청 ID가 이미 허가를 받았다: {request_id}")
                totals = _totals(state)
                if amount > totals.remaining_bound:
                    raise InsufficientBudget(
                        f"예약 {_decimal_text(amount)}USD가 남은 승인 상한 "
                        f"{_decimal_text(totals.remaining_bound)}USD를 넘는다 — 보내지 않는다"
                    )
                record = _new_reservation(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    request_digest=request_digest,
                    model=model,
                    bound=amount,
                    reserved_at=reserved_at,
                )
                self._persist(
                    _LedgerFile(state.identity, state.approval_limit, (*state.records, record))
                )
                return record

    def mark_unknown(self, request_id: str, *, noted_at: str) -> ReservationRecord:
        """결과를 모른다는 명시적 표시 — 상한은 그대로 묶여 있다(환불 없음)."""
        _nonempty(request_id, "요청 ID")
        _nonempty(noted_at, "미지수 표시 시각")
        with self._mutex:
            self._require_usable()
            with _ledger_lock(self._path):
                state = self._load()
                record = _find_record(state.records, request_id)
                if record is None:
                    raise UnknownRequest(f"원장에 없는 요청이다: {request_id}")
                if record.state == "unknown":
                    return record
                if record.state == "reconciled":
                    raise ReconciliationConflict(
                        f"이미 실측으로 확정된 요청을 미지수로 되돌릴 수 없다: {request_id}"
                    )
                updated = replace(record, state="unknown", unknown_at=noted_at)
                self._persist(
                    _LedgerFile(
                        state.identity,
                        state.approval_limit,
                        _replaced_record(state.records, updated),
                    )
                )
                return updated

    def reconcile(
        self, request_id: str, usage: ActualUsage, *, reconciled_at: str
    ) -> ReservationRecord:
        """예약을 검증된 실측으로 딱 한 번 확정한다 — 같은 실측은 멱등이다."""
        _nonempty(request_id, "요청 ID")
        _nonempty(reconciled_at, "확정 시각")
        verified = _validated_usage(usage)
        with self._mutex:
            self._require_usable()
            with _ledger_lock(self._path):
                state = self._load()
                record = _find_record(state.records, request_id)
                if record is None:
                    raise UnknownRequest(f"원장에 없는 요청이다: {request_id}")
                if record.state == "reconciled":
                    return _require_same_reconcile(record, verified)
                _require_usage_match(record, verified, state.identity)
                updated = replace(
                    record, state="reconciled", usage=verified, reconciled_at=reconciled_at
                )
                self._persist(
                    _LedgerFile(
                        state.identity,
                        state.approval_limit,
                        _replaced_record(state.records, updated),
                    )
                )
                return updated

    def snapshot(self) -> LedgerSnapshot:
        """검증된 기록에서 다시 계산한 현재량 — 저장된 합계는 믿지 않는다."""
        with self._mutex:
            self._require_usable()
            with _ledger_lock(self._path):
                return _snapshot(self._load())

    def _load(self) -> _LedgerFile:
        state = _parse_ledger(_read_document(self._path))
        if state.identity != self._identity or state.approval_limit != self._approval_limit:
            raise LedgerIdentityMismatch(
                "원장이 다른 신원·승인 상한으로 묶여 있다 — 조용히 다시 초기화하지 않는다"
            )
        return state

    def _persist(self, state: _LedgerFile) -> None:
        payload = _canonical_bytes(_ledger_document(state))
        temp = self._path.with_name(f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            _write_durable_bytes(temp, payload)
            _replace_file(temp, self._path)
            _fsync_directory(self._path.parent)
        except OSError as exc:
            # 정리 실패는 감수한다 — 남은 임시파일을 읽는 길은 애초에 없다.
            # 원장은 정확히 그 이름 하나만 읽는다.
            with suppress(OSError):
                temp.unlink()
            self._poisoned = f"{type(exc).__name__}: {exc}"
            raise LedgerUnavailable(
                f"원장을 내구하게 쓰지 못했다 — 허가하지 않는다: {exc}"
            ) from exc

    def _require_usable(self) -> None:
        if self._poisoned is not None:
            raise LedgerUnavailable(
                f"이 원장 객체는 쓰기 실패 뒤 막혔다 — 다시 열어 검증해야 한다: {self._poisoned}"
            )


def _require_same_reconcile(record: ReservationRecord, verified: ActualUsage) -> ReservationRecord:
    stored = record.usage
    if stored is not None and stored.usage_revision == verified.usage_revision:
        return record
    raise ReconciliationConflict(f"이미 확정된 요청에 다른 실측이 왔다: {record.request_id}")


def _require_usage_match(
    record: ReservationRecord, verified: ActualUsage, identity: LedgerIdentity
) -> None:
    if verified.input_revision != identity.input_revision:
        raise ReconciliationConflict("실측의 입력 리비전이 원장 신원과 다르다")
    if verified.attempt_id != record.attempt_id:
        raise ReconciliationConflict("실측의 시도 ID가 예약과 다르다")
    if verified.model != record.model:
        raise ReconciliationConflict("실측의 모델이 예약과 다르다")
