"""STAB-S2B-2b — 승인된 분석의 실제 요청 하나하나를 내구 회계에 묶는 계층.

받은 것은 두 개다: S2B-1의 내구 원장(``budget_ledger.BudgetLedger``)과 S2B-2a의
요청 관찰 경계(``transcribe.RequestObserver``). 이 모듈은 그 둘을 잇는 프로젝트
소유의 관찰자·문맥이다 — 전사기·Responses 호출은 이 객체를 주입받아 유료 요청
마다 **호출 전 예약, 응답 실측 확정**을 지난다.

규칙은 원장의 것을 그대로 잇는다:

- 예약이 곧 허가다. 남은 승인 상한에 못 미치면 요청은 가지 않는다(
  ``AccountingDenied`` — 창 단계에서 미처리로 덮는 근거).
- 실측은 요청마다 딱 한 번 확정한다. 결측·무효·가격 없는 미터링은 0원이
  아니라 미지수다 — 상한을 그대로 묶어둔다.
- 회계 기반구조 자신의 실패(원장·투영·증거 쓰기)는 창 실패가 아니라 실행 전체의
  실패다(``AccountingError`` — 문맥이 닫히고 새 승인이 없다).
- 원장 파일이 없는데 지난 사용량·시도 기록이 있으면 0원 시작이 아니다 —
  명시적 조치를 요구하고 조용히 이주하지 않는다(``LegacyAccountingAmbiguous``).

``analysis-usage.jsonl``은 원장 확정 기록의 **멱등 투영**이다: 합계를 믿지 않고
매번 원장에서 다시 만들며, 병렬 조각이 서로의 줄을 지우지 않게 잠금 안에서
교체한다. 아직 UI가 보지 못하는 내부 산물(가격 없는 미터링 증거)은
``accounting/unpriced-usage.jsonl``에 남긴다 — S2B-3가 사용자 표면을 만든다.

이 모듈은 ``transcribe``를 import하지 않는다. 관찰자는 구조적 프로토콜
(``authorize``/``on_response``/``on_error``)만 지키면 되고, 연산 구분은 메타의
``operation`` 문자열로 한다 — 레거시→프로젝트 import 경계는 그대로다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import env
from vlogsrt.project.budget_ledger import (
    BudgetLedger,
    BudgetLedgerError,
    InsufficientBudget,
    LedgerIdentity,
    LedgerSnapshot,
    LedgerUnavailable,
    ReservationRecord,
)
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.workflow_analysis import AnalysisSettings
from vlogsrt.project.workflow_protocol import (
    ActualUsage,
    AnalysisAuthorization,
    PricingSnapshot,
    decode_message,
    encode_message,
)

ACCOUNTING_DIR = "accounting"
LEDGER_NAME = "budget-ledger.json"
EVIDENCE_NAME = "unpriced-usage.jsonl"
USAGE_NAME = "analysis-usage.jsonl"

_TRANSCRIPTION_OPERATION = "audio.transcriptions"
_RESPONSES_OPERATION = "responses.create"

# transcribe._PUBLIC_OPENAI_BASE_URL과 같은 값 — SDK가 환경 없이 쓰는 공개 엔드포인트.
# 엔드포인트 신원은 값의 해시로만 묶는다(원문은 어디에도 남지 않는다).
_PUBLIC_ENDPOINT = "https://api.openai.com/v1"

_MAX_CAUSE_DEPTH = 16


class AccountingError(RuntimeError):
    """회계 기반구조의 치명 실패 — 창 하나의 실패가 아니라 실행 전체를 닫는다."""


class AccountingDenied(AccountingError):
    """예약이 남은 승인 상한을 넘는다 — 요청을 보내지 않는다(예산 정지의 근거)."""


class LegacyAccountingAmbiguous(AccountingError):
    """원장 없는 과거 기록 — 0원 시작으로 조용히 이주하지 않고 명시적 조치를 요구한다."""


# ─────────────────────────────────────────────────────────────
#  신원 — 다이제스트만 남긴다(값·경로 원문은 없다)
# ─────────────────────────────────────────────────────────────


def workspace_binding_sha256(workspace: Path) -> str:
    """작업공간 자리의 지문 — 다른 루트의 같은 job이 낡은 승인을 재사용하는 길을 닫는다.

    개발 엔진은 job마다 `<루트>/Jobs/<jobID>/workspace`를 만들고 승인 파일만
    스테이징한다. 추정 지문에 이 자리가 묶이면 새 루트에서 다시 계산한 추정이
    이전 루트의 승인과 절대 묶이지 않는다(``require_matches``가 유료 호출 앞에서
    거부한다). 같은 루트의 재시도·재오픈은 같은 지문을 내므로 원장이 그대로
    이어진다.
    """
    return hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()


def endpoint_identity_sha256(project_config: ProjectConfig) -> str:
    """실제 호출이 가는 엔드포인트의 지문 — 키가 없어도 계산된다(견적은 키 없이).

    ``_staged_vision_credentials``와 같은 갈림길을 쓴다: V2 앱 운반이면 역할별
    값(비전은 자기 이름의 base URL), 마커 없는 직접 CLI면 STT 공유 경로(비전도
    STT base URL을 공유한다). 이름만 보지 않고 **푼 값**을 해시로 묶는다 — SDK의
    환경 상속이 승인과 다른 엔드포인트를 고르는 일이 없어야 하기 때문이다.
    키 값은 재료에 없다(회전이 지출을 리셋하지 않는다).
    """
    stt_base = env.base_url() or _PUBLIC_ENDPOINT
    if env.role_credentials_applied():
        payload = {
            "routing": "v2-role",
            "stt_base_url": stt_base,
            "vision_base_url": env.get(project_config.vision_base_url_env).strip()
            or _PUBLIC_ENDPOINT,
            "vision_base_url_env": project_config.vision_base_url_env,
            "vision_key_env": project_config.vision_api_key_env,
        }
    else:
        payload = {
            "routing": "legacy-shared",
            "stt_base_url": stt_base,
            "vision_base_url": stt_base,
        }
    return _digest(payload)


def authorization_identity_sha256(authorization: AnalysisAuthorization) -> str:
    """승인 전체의 지문 — 리비전 넷에 승인 시각까지 묶는다(시각 바꿔치기 리셋 금지)."""
    return _digest(
        {
            "authorized_at": authorization.authorized_at,
            "estimate_revision": authorization.estimate_revision,
            "input_revision": authorization.input_revision,
            "media_grant_revision": authorization.media_grant_revision,
            "pricing_snapshot_revision": authorization.pricing_snapshot_revision,
        }
    )


def quote_policy_sha256(settings: AnalysisSettings) -> str:
    return _digest(
        {
            "max_request_images": settings.max_request_images,
            "max_request_output_tokens": settings.max_request_output_tokens,
            "max_request_text_bytes": settings.max_request_text_bytes,
            "quote_entry_overhead_bytes": settings.quote_entry_overhead_bytes,
            "quote_image_envelope_bytes": settings.quote_image_envelope_bytes,
            "quote_image_input_tokens": settings.quote_image_input_tokens,
            "quote_structural_overhead_bytes": settings.quote_structural_overhead_bytes,
            "quote_structural_overhead_tokens": settings.quote_structural_overhead_tokens,
            "quote_text_bytes_per_token": settings.quote_text_bytes_per_token,
        }
    )


def ledger_policy_sha256(
    settings: AnalysisSettings, *, settings_sha256: str, project_config: ProjectConfig
) -> str:
    """원장 정책 지문 — 유효 설정·견적 정책·엔드포인트를 한데 묶는다."""
    return _digest(
        {
            "endpoint_sha256": endpoint_identity_sha256(project_config),
            "quote_policy_sha256": quote_policy_sha256(settings),
            "settings_sha256": settings_sha256,
        }
    )


# ─────────────────────────────────────────────────────────────
#  요청 상한 — 추정과 실제 승인이 같은 식을 쓴다
# ─────────────────────────────────────────────────────────────


def audio_per_minute_rate(model: str, pricing: PricingSnapshot) -> Decimal:
    """모델 자기 가격 — 스냅샷에 없는 모델은 상한을 정할 수 없으니 호출 앞에서 닫는다."""
    if model == pricing.whisper_model:
        return Decimal(pricing.whisper_per_minute)
    if model == pricing.gpt_transcribe_model:
        return Decimal(pricing.gpt_transcribe_per_minute)
    raise AccountingError(f"가격 스냅샷에 없는 전사 모델이다 — 상한을 정할 수 없다: {model!r}")


def request_bound(meta: Any, *, settings: AnalysisSettings, pricing: PricingSnapshot) -> Decimal:
    """이 요청 하나의 보수적 상한 — 예약은 이 금액으로 디스크에 남는다.

    오디오는 조각 길이 전부를 그 모델의 분당가로 잰다(재시도·거부 인자 재요청은
    각각 새 요청이니 새 예약이다). Luna은 직렬화 글자 바이트·이미지 장수·출력
    상한을 정책 토큰식으로 환산한다 — base64 이미지 바이트를 글자 토큰으로 세지
    않고 low detail 정액의 여유 배수로 잰다.
    """
    operation = getattr(meta, "operation", "")
    if operation == _TRANSCRIPTION_OPERATION:
        return _audio_bound(meta, pricing=pricing)
    if operation == _RESPONSES_OPERATION:
        return _luna_bound(meta, settings=settings, pricing=pricing)
    raise AccountingError(f"지원하지 않는 연산이다 — 상한을 정할 수 없다: {operation!r}")


def _audio_bound(meta: Any, *, pricing: PricingSnapshot) -> Decimal:
    duration = getattr(meta, "chunk_duration", None)
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise AccountingError(f"전사 시도에 조각 길이가 없다 — 상한을 정할 수 없다: {duration!r}")
    seconds = Fraction(str(duration))
    rate = audio_per_minute_rate(str(getattr(meta, "model", "")), pricing)
    return _decimal((_fraction_decimal(seconds) / Decimal(60)) * rate)


def _luna_bound(meta: Any, *, settings: AnalysisSettings, pricing: PricingSnapshot) -> Decimal:
    model = str(getattr(meta, "model", ""))
    if model != pricing.luna_model:
        raise AccountingError(f"가격 스냅샷의 Luna 모델과 다르다 — 상한을 정할 수 없다: {model!r}")
    options = getattr(meta, "options", None)
    if not isinstance(options, Mapping):
        raise AccountingError("요청 인자 사본을 읽을 수 없다 — 상한을 정할 수 없다")
    text_bytes, images = _request_dimensions(options, settings)
    if text_bytes > settings.max_request_text_bytes:
        raise AccountingError(
            f"직렬화 글자 {text_bytes}B가 상한 {settings.max_request_text_bytes}B를 넘는다 — "
            "과소 상한으로 청구하지 않고 안전하게 실패한다"
        )
    if images > settings.max_request_images:
        raise AccountingError(
            f"요청 이미지 {images}장이 상한 {settings.max_request_images}장을 넘는다 — "
            "안전하게 실패한다"
        )
    output_cap = options.get("max_output_tokens")
    if (
        isinstance(output_cap, bool)
        or not isinstance(output_cap, int)
        or output_cap < 1
        or output_cap > settings.max_request_output_tokens
    ):
        raise AccountingError(
            f"출력 상한 {output_cap!r}이 정책 범위(1~"
            f"{settings.max_request_output_tokens})를 벗어났다 — 안전하게 실패한다"
        )
    input_tokens = (
        text_bytes // settings.quote_text_bytes_per_token
        + settings.quote_structural_overhead_tokens
        + images * settings.quote_image_input_tokens
    )
    return _decimal(
        (
            Decimal(input_tokens) * Decimal(pricing.luna_input_per_million_tokens)
            + Decimal(output_cap) * Decimal(pricing.luna_output_per_million_tokens)
        )
        / Decimal(1_000_000)
    )


_RESPONSES_OPTION_KEYS = frozenset({"model", "input", "max_output_tokens", "reasoning"})
_ENTRY_KEYS = frozenset({"role", "content"})
_TEXT_PART_KEYS = frozenset({"type", "text"})
_IMAGE_PART_KEYS = frozenset({"type", "detail", "image_url"})


def _request_dimensions(options: Mapping[str, Any], settings: AnalysisSettings) -> tuple[int, int]:
    """요청 사본에서 구조를 포함한 글자 바이트와 이미지 장수를 잰다.

    모양은 **정확히** 오늘 파이프라인이 만드는 것만 받는다: 모르는 content
    종류·모르는 인자·계약 밖의 마디 모양은 세지 않고 넘기지 않고 안전하게
    실패한다 — 세지 않은 재료가 실은 토큰을 실어 나르면(예: 이미지 마디에
    끼운 글자, 항목에 붙은 메타데이터, instructions 같은 밖의 인자) 상한이 이미
    깨진 채로 호출이 나간다. 정액 겉면(항목 64B·이미지 128B)은 이 검증된
    모양에서만 유효하다. base64 이미지 데이터는 글자 바이트로 세지 않는다 —
    이미지는 장당 정액 토큰이 잰다.
    """
    unknown_options = set(options) - _RESPONSES_OPTION_KEYS
    if unknown_options:
        raise AccountingError(
            f"상한이 증명되지 않은 요청 인자다({sorted(unknown_options)}) — 안전하게 실패한다"
        )
    reasoning = options.get("reasoning")
    if reasoning != {"effort": "none"}:
        raise AccountingError(
            f"reasoning이 {reasoning!r}이다 — 출력 토큰이 판정 JSON에만 쓰이는 모양이"
            " 아니면 상한을 증명할 수 없다"
        )
    items = options.get("input")
    if not isinstance(items, list) or len(items) != 1:
        raise AccountingError("요청 input은 사용자 항목 하나여야 한다 — 상한을 정할 수 없다")
    structured_bytes = settings.quote_structural_overhead_bytes
    images = 0
    for entry in items:
        if not isinstance(entry, Mapping) or set(entry) != _ENTRY_KEYS:
            raise AccountingError("요청 input 항목의 모양이 계약과 다르다 — 안전하게 실패한다")
        if entry.get("role") != "user":
            raise AccountingError("요청 input 항목이 user가 아니다 — 안전하게 실패한다")
        content = entry.get("content")
        if not isinstance(content, list):
            raise AccountingError("요청 content가 리스트가 아니다 — 상한을 정할 수 없다")
        structured_bytes += settings.quote_entry_overhead_bytes
        for part in content:
            if not isinstance(part, Mapping):
                raise AccountingError("요청 content 항목이 객체가 아니다 — 상한을 정할 수 없다")
            kind = part.get("type")
            if kind == "input_text":
                if set(part) != _TEXT_PART_KEYS or not isinstance(part.get("text"), str):
                    raise AccountingError(
                        "input_text 마디의 모양이 계약과 다르다 — 안전하게 실패한다"
                    )
                # 직렬화한 마디 전체(겉면 포함)를 잰다 — 겉면도 요청을 이루는 바이트다.
                structured_bytes += len(_serialize_part(part))
            elif kind == "input_image":
                if set(part) != _IMAGE_PART_KEYS or not isinstance(part.get("image_url"), str):
                    raise AccountingError(
                        "input_image 마디의 모양이 계약과 다르다 — 안전하게 실패한다"
                    )
                if part.get("detail") != "low":
                    raise AccountingError(
                        "low detail가 아닌 이미지는 정액 상한으로 증명할 수 없다"
                        " — 안전하게 실패한다"
                    )
                structured_bytes += settings.quote_image_envelope_bytes
                images += 1
            else:
                raise AccountingError(
                    f"상한이 증명되지 않은 content 종류다({kind!r}) — 안전하게 실패한다"
                )
    return structured_bytes, images


def _serialize_part(part: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(part), sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise AccountingError(f"요청 마디를 직렬화할 수 없다 — 상한을 정할 수 없다: {exc}") from exc


def request_digest(meta: Any) -> str:
    """요청 다이제스트 — 시도 신원·연산·모델·좌표·실제 실린 인자의 사본을 묶는다."""
    payload = {
        "attempt_id": str(getattr(meta, "attempt_id", "")),
        "chunk_duration": getattr(meta, "chunk_duration", None),
        "chunk_index": getattr(meta, "chunk_index", None),
        "chunk_offset": getattr(meta, "chunk_offset", None),
        "kind": str(getattr(meta, "kind", "")),
        "model": str(getattr(meta, "model", "")),
        "operation": str(getattr(meta, "operation", "")),
        "options": dict(getattr(meta, "options", {})),
    }
    return _digest(payload)


# ─────────────────────────────────────────────────────────────
#  미터링 해석 — 변환 성공 여부와 무관하게 원 SDK 응답에서 읽는다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AudioMetering:
    seconds: Fraction
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LunaMetering:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class UnpricedEvidence:
    """가격을 매길 수 없는 미터링의 증거 — 토큰→초·토큰율 발명 없이 양만 남긴다."""

    reason_code: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


def _as_mapping(response: object) -> Mapping[str, Any]:
    """dict은 그대로, SDK 객체는 ``model_dump``·``to_dict``의 결과를 본다.

    변환 자체가 실패하는 응답도 있다 — 그때는 빈 사전으로 미터링이 없다는
    판정에 이르게 둔다(예외를 삼키는 게 아니라 미지수로 보수적으로 남긴다).
    """
    if isinstance(response, Mapping):
        return response
    for attribute in ("model_dump", "to_dict"):
        converter = getattr(response, attribute, None)
        if callable(converter):
            try:
                dumped = converter()
            except Exception:  # noqa: BLE001 - 변환 실패는 미터링 결측이다(0원이 아니다)
                return {}
            return dumped if isinstance(dumped, Mapping) else {}
    return {}


def _field(response: object, mapping: Mapping[str, Any], name: str) -> object:
    if name in mapping:
        return mapping[name]
    return getattr(response, name, None)


def _token_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_tokens(usage: object) -> tuple[int | None, int | None, int | None]:
    if usage is None:
        return (None, None, None)

    def pick(name: str) -> object:
        if isinstance(usage, Mapping) and name in usage:
            return usage[name]
        return getattr(usage, name, None)

    return (
        _token_int(pick("input_tokens")),
        _token_int(pick("output_tokens")),
        _token_int(pick("total_tokens")),
    )


def _seconds_fraction(value: object) -> Fraction | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return Fraction(str(value))


def _usage_field(usage: object, name: str) -> object:
    if isinstance(usage, Mapping) and name in usage:
        return usage[name]
    return getattr(usage, name, None)


def _usage_duration_seconds(usage: object) -> Fraction | None:
    """SDK ``UsageDuration``(type=duration)의 초 — 길이 미터링의 다른 모습.

    설치된 SDK의 전사 응답은 길이를 `duration` 칸이나 `usage`의
    ``UsageDuration(type="duration", seconds=...)`` 중 하나로 실는다(사전·객체
    두 모양 다 있다). 둘 다 같은 분당가 근거다 — 토큰 미터링(``UsageTokens``)과
    모양으로 갈라 본다.
    """
    if usage is None or _usage_field(usage, "type") != "duration":
        return None
    return _seconds_fraction(_usage_field(usage, "seconds"))


def parse_audio_metering(response: object) -> tuple[AudioMetering | None, UnpricedEvidence | None]:
    """전사 응답의 미터링 — 길이(초)가 가격의 근거다.

    토큰만 온 전사 응답은 가격을 매길 수 없다: 분당가 스냅샷으로 토큰을 바꾸는
    비율은 없고, 지어낸 초로 환산하지도 않는다. 양은 증거로 남기고 상한은
    미지수로 묶여 있는다.
    """
    mapping = _as_mapping(response)
    usage = _field(response, mapping, "usage")
    seconds = _seconds_fraction(_field(response, mapping, "duration"))
    if seconds is None:
        seconds = _usage_duration_seconds(usage)
    input_tokens, output_tokens, total_tokens = _usage_tokens(usage)
    if seconds is not None:
        return (
            AudioMetering(
                seconds=seconds,
                input_tokens=input_tokens if input_tokens is not None else 0,
                output_tokens=output_tokens if output_tokens is not None else 0,
            ),
            None,
        )
    if input_tokens is not None or output_tokens is not None or total_tokens is not None:
        return (
            None,
            UnpricedEvidence(
                reason_code="token_only_audio_usage",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
        )
    return None, UnpricedEvidence(reason_code="missing_usage")


def parse_luna_metering(response: object) -> tuple[LunaMetering | None, UnpricedEvidence | None]:
    mapping = _as_mapping(response)
    usage = _field(response, mapping, "usage")
    input_tokens, output_tokens, total_tokens = _usage_tokens(usage)
    if input_tokens is not None and output_tokens is not None:
        return LunaMetering(input_tokens=input_tokens, output_tokens=output_tokens), None
    return (
        None,
        UnpricedEvidence(
            reason_code="missing_or_invalid_usage",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        ),
    )


def audio_cost_text(seconds: Fraction, per_minute: Decimal) -> str:
    return _decimal_text((_fraction_decimal(seconds) / Decimal(60)) * per_minute)


def luna_cost_text(input_tokens: int, output_tokens: int, pricing: PricingSnapshot) -> str:
    return _decimal_text(
        (
            Decimal(input_tokens) * Decimal(pricing.luna_input_per_million_tokens)
            + Decimal(output_tokens) * Decimal(pricing.luna_output_per_million_tokens)
        )
        / Decimal(1_000_000)
    )


def is_budget_denial(exc: BaseException) -> bool:
    """원인 사슬에서 예산 거부를 찾는다 — 관찰 경계는 예외를 한 번 감싸 올린다."""
    cursor: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if cursor is None:
            return False
        if isinstance(cursor, (AccountingDenied, InsufficientBudget)):
            return True
        cursor = cursor.__cause__
    return False


# ─────────────────────────────────────────────────────────────
#  문맥 — 하나의 분석 실행을 통째로 묶는 회계 상태
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Ticket:
    request_id: str
    bound: Decimal
    window_id: str | None


class AnalysisAccounting:
    """실행 하나의 내구 회계 문맥 — 원장·투영·증거를 소유한다.

    치명 실패가 한 번이라도 일어나면 문맥이 닫힌다(``ensure_healthy``가 실행
    단계 사이에서 들춘다). 예산 거부는 닫지 않는다 — 그건 고장이 아니라 상한이
    하는 일이다.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        settings: AnalysisSettings,
        pricing: PricingSnapshot,
        ledger: BudgetLedger,
        attempt_id: str,
        input_revision: str,
    ) -> None:
        self._workspace = workspace
        self._settings = settings
        self._pricing = pricing
        self._ledger = ledger
        self._attempt_id = attempt_id
        self._input_revision = input_revision
        self._usage_path = workspace / USAGE_NAME
        self._evidence_path = workspace / ACCOUNTING_DIR / EVIDENCE_NAME
        self._lock = threading.Lock()
        self._fatal: AccountingError | None = None

    @classmethod
    def open(
        cls,
        *,
        workspace: Path,
        settings: AnalysisSettings,
        pricing: PricingSnapshot,
        authorization: AnalysisAuthorization,
        settings_sha256: str,
        project_config: ProjectConfig,
        attempt_id: str,
        approval_limit: Decimal,
    ) -> AnalysisAccounting:
        """원장을 열거나 다시 열고, 쓰기 전에 투영을 원장에서 다시 세운다.

        원장이 없는 데 지난 사용량·시도 기록이 있으면 이주하지 않는다 — 그 기록은
        0원의 증거가 아니다. 방금 붙인 이번 시도 기록은 현재 시도 ID로 구별한다.
        """
        ledger_path = workspace / ACCOUNTING_DIR / LEDGER_NAME
        exists = _path_exists(ledger_path)
        if not exists:
            _require_clean_history(workspace, attempt_id)
        accounting_dir = ledger_path.parent
        _durable_mkdir(accounting_dir)
        identity = LedgerIdentity(
            input_revision=authorization.input_revision,
            policy_digest=ledger_policy_sha256(
                settings, settings_sha256=settings_sha256, project_config=project_config
            ),
            pricing_revision=pricing.snapshot_revision,
            authorization_id=authorization_identity_sha256(authorization),
        )
        try:
            ledger = BudgetLedger.open(
                ledger_path, identity=identity, approval_limit=approval_limit
            )
        except (BudgetLedgerError, LedgerUnavailable) as exc:
            raise AccountingError(f"내구 원장을 열지 못했다: {exc}") from exc
        context = cls(
            workspace=workspace,
            settings=settings,
            pricing=pricing,
            ledger=ledger,
            attempt_id=attempt_id,
            input_revision=authorization.input_revision,
        )
        context.rebuild_projection()
        return context

    # ── 관찰자 ──

    def observer(self) -> _AccountingObserver:
        return _AccountingObserver(self)

    def window_observer(self, window_id: str) -> _WindowObserver:
        """Responses 호출용 — 티켓에 창 신원을 묶어 실측이 창과 함께 기록되게 한다."""
        return _WindowObserver(self, window_id)

    # ── 상태 ──

    @property
    def fatal_failure(self) -> AccountingError | None:
        with self._lock:
            return self._fatal

    def ensure_healthy(self) -> None:
        """치명 실패가 있으면 실행 단계 사이에서 즉시 들춘다 — 소스 오류로 위장하지 않는다."""
        failure = self.fatal_failure
        if failure is not None:
            raise failure

    def snapshot(self) -> LedgerSnapshot:
        return self._ledger.snapshot()

    # ── 내부 — 관찰자가 부르는 좌석 ──

    def _fail(self, exc: BaseException, stage: str) -> AccountingError:
        failure = AccountingError(f"회계({stage})가 실패했다 — 실행 전체를 닫는다: {exc}")
        with self._lock:
            if self._fatal is None:
                self._fatal = failure
        return failure

    def _require_open(self) -> None:
        failure = self.fatal_failure
        if failure is not None:
            raise AccountingError(f"치명 회계 실패 뒤에는 새 승인이 없다: {failure}") from failure

    def _authorize(self, meta: Any, *, window_id: str | None) -> _Ticket:
        self._require_open()
        bound = request_bound(meta, settings=self._settings, pricing=self._pricing)
        request_id = f"{self._attempt_id}:{getattr(meta, 'attempt_id', '')}"
        try:
            self._ledger.reserve(
                request_id=request_id,
                attempt_id=self._attempt_id,
                request_digest=request_digest(meta),
                model=str(getattr(meta, "model", "")),
                bound=bound,
                reserved_at=_utc_now(),
            )
        except InsufficientBudget as exc:
            raise AccountingDenied(
                f"예약 상한이 남은 승인을 넘는다 — 이 요청은 보내지 않는다: {exc}"
            ) from exc
        except (BudgetLedgerError, LedgerUnavailable) as exc:
            raise self._fail(exc, "reserve") from exc
        return _Ticket(request_id=request_id, bound=bound, window_id=window_id)

    def _on_response(self, ticket: _Ticket, meta: Any, response: object) -> None:
        self._require_open()
        operation = str(getattr(meta, "operation", ""))
        try:
            metering: AudioMetering | LunaMetering | None
            unpriced: UnpricedEvidence | None
            if operation == _TRANSCRIPTION_OPERATION:
                metering, unpriced = parse_audio_metering(response)
            elif operation == _RESPONSES_OPERATION:
                metering, unpriced = parse_luna_metering(response)
            else:
                raise self._fail(ValueError(f"지원하지 않는 연산이다: {operation!r}"), "metering")
            if metering is None:
                assert unpriced is not None
                self._retain_unknown(ticket, meta, unpriced)
                return
            usage = self._measured_usage(ticket, meta, metering)
        except AccountingError:
            raise  # 이미 문맥을 닫은 실패다 — 원인 그대로 올린다
        except Exception as exc:  # noqa: BLE001 - 해석·검증 실패가 열린 문맥을 두고 가지 않는다
            raise self._fail(exc, "metering") from exc
        try:
            self._ledger.reconcile(ticket.request_id, usage, reconciled_at=_utc_now())
        except (BudgetLedgerError, LedgerUnavailable) as exc:
            raise self._fail(exc, "reconcile") from exc
        self.rebuild_projection()

    def _on_error(self, ticket: _Ticket, meta: Any, error: Exception) -> None:
        # SDK 오류는 미지수다 — 0원도 환불도 아니다. 상한은 그대로 묶여 있다.
        try:
            self._ledger.mark_unknown(ticket.request_id, noted_at=_utc_now())
        except (BudgetLedgerError, LedgerUnavailable) as exc:
            raise self._fail(exc, "mark_unknown") from exc

    def _measured_usage(
        self, ticket: _Ticket, meta: Any, metering: AudioMetering | LunaMetering
    ) -> ActualUsage:
        model = str(getattr(meta, "model", ""))
        if isinstance(metering, AudioMetering):
            cost = audio_cost_text(metering.seconds, audio_per_minute_rate(model, self._pricing))
            return ActualUsage.create(
                input_revision=self._input_revision,
                attempt_id=self._attempt_id,
                window_id=ticket.window_id,
                model=model,
                input_tokens=metering.input_tokens,
                output_tokens=metering.output_tokens,
                audio_seconds=metering.seconds,
                cost=cost,
            )
        assert isinstance(metering, LunaMetering)
        if model != self._pricing.luna_model:
            raise self._fail(
                AccountingError(f"확정 미터링의 모델이 스냅샷과 다르다: {model!r}"), "metering"
            )
        return ActualUsage.create(
            input_revision=self._input_revision,
            attempt_id=self._attempt_id,
            window_id=ticket.window_id,
            model=model,
            input_tokens=metering.input_tokens,
            output_tokens=metering.output_tokens,
            cost=luna_cost_text(metering.input_tokens, metering.output_tokens, self._pricing),
        )

    def _retain_unknown(self, ticket: _Ticket, meta: Any, evidence: UnpricedEvidence) -> None:
        """가격 없는 미터링 — 상한은 미지수로 묶고 양은 증거로만 남긴다."""
        try:
            self._ledger.mark_unknown(ticket.request_id, noted_at=_utc_now())
        except (BudgetLedgerError, LedgerUnavailable) as exc:
            raise self._fail(exc, "mark_unknown") from exc
        row = {
            "input_tokens": evidence.input_tokens,
            "model": str(getattr(meta, "model", "")),
            "operation": str(getattr(meta, "operation", "")),
            "output_tokens": evidence.output_tokens,
            "reason_code": evidence.reason_code,
            "recorded_at": _utc_now(),
            "request_id": ticket.request_id,
            "total_tokens": evidence.total_tokens,
        }
        try:
            self._evidence_path.parent.mkdir(parents=True, exist_ok=True)
            with self._evidence_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise self._fail(exc, "evidence") from exc

    # ── 투영 — 원장이 정본, 파일은 다시 만든 결과물 ──

    def rebuild_projection(self) -> None:
        """확정 기록을 원장에서 다시 만들어 ``analysis-usage.jsonl``에 멱등으로 놓는다.

        **투영 잠금을 먼저 얻고 그 안에서** (원장 스냅샷 → 전량 교체)를 한다.
        스냅샷을 잠금 밖에서 먼저 뽑으면 늦게 온 스레드가 낡은 스냅샷으로 새
        투영을 덮어쓰는 경쟁이 그 길이다. 잠금 순서는 언제나 투영 → 원장
        (원장 쪽은 `reconcile`가 놓고 나오므로 순서 뒤집힘이 없다). 투영 실패는
        치명이다 — 원장은 이미 정본을 갖고 있고, 재오픈 때 이 자리에서 다시 세운다.
        """
        self._require_open()
        lock_path = self._usage_path.with_name(self._usage_path.name + ".lock")
        try:
            with _flocked(lock_path):
                try:
                    records: tuple[ReservationRecord, ...] = self._ledger.snapshot().records
                except (BudgetLedgerError, LedgerUnavailable) as exc:
                    raise self._fail(exc, "projection-snapshot") from exc
                payload = "".join(
                    encode_message(record.usage)
                    for record in records
                    if record.state == "reconciled" and record.usage is not None
                )
                _atomic_write_bytes(self._usage_path, payload.encode("utf-8"))
        except OSError as exc:
            raise self._fail(exc, "projection") from exc


class _AccountingObserver:
    """오디오 경계에 들어가는 관찰자 — 창 없는 요청의 실측은 window_id 없이 남는다."""

    def __init__(self, context: AnalysisAccounting) -> None:
        self._context = context

    def authorize(self, meta: Any) -> _Ticket:
        return self._context._authorize(meta, window_id=None)

    def on_response(self, ticket: _Ticket, meta: Any, response: object) -> None:
        self._context._on_response(ticket, meta, response)

    def on_error(self, ticket: _Ticket, meta: Any, error: Exception) -> None:
        self._context._on_error(ticket, meta, error)


class _WindowObserver:
    """Responses 경계용 감싸개 — 티켓에 창 신원만 얹는다(메타는 건드리지 않는다)."""

    def __init__(self, context: AnalysisAccounting, window_id: str) -> None:
        self._context = context
        self._window_id = window_id

    def authorize(self, meta: Any) -> _Ticket:
        return self._context._authorize(meta, window_id=self._window_id)

    def on_response(self, ticket: _Ticket, meta: Any, response: object) -> None:
        self._context._on_response(ticket, meta, response)

    def on_error(self, ticket: _Ticket, meta: Any, error: Exception) -> None:
        self._context._on_error(ticket, meta, error)


# ─────────────────────────────────────────────────────────────
#  과거 기록 검사 — 원장 없는 0원 시작을 막는다
# ─────────────────────────────────────────────────────────────


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError as exc:
        raise AccountingError(f"원장 자리를 확인할 수 없다: {path}") from exc


def _require_clean_history(workspace: Path, attempt_id: str) -> None:
    """원장을 새로 만들기 전에 지난 사용량·시도 기록이 없는지 본다.

    사용량 줄이 하나라도 있거나(실제 과금의 흔적), 이번 시도 외의 시도 기록이
    하나라도 있으면(유료였을 수 있는 실행 역사) 명시적 조치를 요구한다. 방금
    붙은 이번 시도(pending·running)는 현재 시도 ID로 구별해 세지 않는다.
    """
    explicit = "새 작업공간에서 시작하거나, 지난 기록을 직접 정리한 뒤 다시 시도하세요"
    usage_path = workspace / USAGE_NAME
    try:
        usage_text = usage_path.read_text(encoding="utf-8") if usage_path.is_file() else ""
    except OSError as exc:
        raise AccountingError(f"사용량 기록을 읽을 수 없다: {usage_path}") from exc
    if any(line.strip() for line in usage_text.splitlines()):
        raise LegacyAccountingAmbiguous(
            f"원장 없이 사용량 기록이 남아 있다: {usage_path} — 지난 지출을 0으로 "
            f"시작하는 길은 없다. {explicit}"
        )
    attempts_path = workspace / "analysis-attempts.jsonl"
    try:
        attempts_text = attempts_path.read_text(encoding="utf-8") if attempts_path.is_file() else ""
    except OSError as exc:
        raise AccountingError(f"시도 기록을 읽을 수 없다: {attempts_path}") from exc
    # decode_message는 JSONL 한 줄이 **줄바꿈으로 끝나는** 문면을 요구한다 —
    # keepends로 그 문면을 그대로 건넨다.
    for line in attempts_text.splitlines(keepends=True):
        if not line.strip():
            continue
        try:
            message = decode_message(line)
        except Exception as exc:  # noqa: BLE001 - 해석 못 하는 기록도 과거 역사다
            raise LegacyAccountingAmbiguous(
                f"시도 기록을 해석할 수 없다: {attempts_path} — "
                f"원장 없이 읽는 길은 없다. {explicit}"
            ) from exc
        attempt = getattr(message, "attempt_id", "")
        if attempt != attempt_id:
            raise LegacyAccountingAmbiguous(
                f"원장 없이 이전 시도 기록이 남아 있다(시도 {attempt or '?'}) — 지난 "
                f"실행이 유료였을 가능성을 0원 시작으로 덮지 않는다. {explicit}"
            )


# ─────────────────────────────────────────────────────────────
#  저장 보조 — 작은 것은 이 파일이 스스로 든다
# ─────────────────────────────────────────────────────────────


def _durable_mkdir(path: Path) -> None:
    """만드는 쪽이 내구화까지 맡는다(원장의 조상 계약, budget_ledger 참조)."""
    try:
        created = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if created:
            _fsync_directory(path.parent)
        _fsync_directory(path)
    except OSError as exc:
        raise AccountingError(f"회계 디렉터리를 내구화하지 못했다: {path}") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


@contextmanager
def _flocked(path: Path) -> Iterator[None]:
    """투영 교체 직렬화 — 협조하는 로컬 POSIX 잠금(원장 잠금과 같은 가정)."""
    stream = path.open("a")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fraction_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _decimal(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0:
        raise AccountingError(f"상한이 유한한 0 이상이어야 한다: {value}")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
