"""Gemini 의미 분석의 비용 산술(W-30b, D84) — 순수 함수. 호출·파일·환경을 모른다.

`claude_cost.py`의 거울이다. 상한(ceiling)은 승인 전에 보여 주는 **보수적** 금액이고, 영수증
(`measure_gemini_usage`)은 응답이 말한 토큰을 공식 단가 스냅샷으로 **계산한** 값이다 — 어느 쪽도
공급자의 청구서가 아니다. 못 매기면 가격 미정이다(0원이 아니다).

근거(공식 문서, 2026-10-25 확인):
- 생각 토큰은 출력 단가로 청구되고 `maxOutputTokens` 안에 든다 — 기반 시설이 강제하는 끊김이라
  출력을 한도 전부로 세면 생각까지 덮는다(thinking 문서).
- Gemini 3·4의 이미지는 `MEDIA_RESOLUTION_HIGH`에서 장당 1120토큰이 상한이다(media resolution
  문서) — 크기와 무관하다. 해상도를 적지 않은 이미지는 기본값이 바뀌면 토큰을 모르므로 닫는다.
- 단가는 공식 표가 미리 알린 인상 전 마지막 날까지만 쓴다(`price_valid_through`).
- 창 클립(D85)은 초당 한 장으로 표본된다 — 영상 장당 280 · 소리 초당 32토큰(두 문서 값 가운데 큰
  쪽)으로 센다. 클립 길이는 부분의 `videoMetadata.endOffset`이 말한다(그 칸이 없으면 잴 수 없다).

가정(D84, Claude와 같다): 텍스트 토큰 수는 UTF-8 바이트 수 이하다 — 문서화된 값이 아니므로 첫 사람
실행의 영수증이 확인한다. 응답 schema도 입력으로 센다(보수적).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any, TypeGuard

from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    GEMINI_ANALYSIS_PROFILES,
    GEMINI_MAX_OUTPUT_TOKENS_LIMIT,
    GEMINI_TRANSCRIBE_PROFILES,
    PRICE_TOKEN_UNIT,
    RawGeminiAnalysisProfile,
    gemini_transcribe_price_per_minute,
)
from vlogsrt.project.request_accounting import MeasuredUsage

PAYLOAD_KEYS = frozenset({"contents", "generationConfig"})
"""상한을 잴 수 있는 페이로드의 칸 전부 — 다른 칸이 있으면 무엇이 청구될지 모른다."""
GENERATION_CONFIG_KEYS = frozenset(
    {"maxOutputTokens", "responseMimeType", "responseJsonSchema", "thinkingConfig"}
)
"""`generationConfig`의 칸 전부 — 후보 수·도구 같은 칸은 청구를 늘릴 수 있다."""
JSON_MIME_TYPE = "application/json"
JPEG_MIME_TYPE = "image/jpeg"
VIDEO_MIME_TYPE = "video/mp4"
IMAGE_PART_KEYS = frozenset({"inlineData", "mediaResolution"})
VIDEO_PART_KEYS = frozenset({"inlineData", "mediaResolution", "videoMetadata"})
USER_ROLE = "user"
DURATION = re.compile(r"(0|[1-9][0-9]{0,5})(\.[0-9]{1,9})?s")
"""protobuf Duration의 JSON 표기(`19.5s`) — 음수·지수·단위 없는 값은 받지 않는다."""
INLINE_REQUEST_LIMIT_BYTES = 20_000_000
"""인라인 요청 전체의 상한(video understanding 문서 — 「under 20MB total request size」)."""

UNPRICED_UNKNOWN_MODEL = "unknown_model"
UNPRICED_INVALID_USAGE = "missing_or_invalid_usage"
UNPRICED_MODEL_MISMATCH = "model_mismatch"
UNPRICED_PRICE_EXPIRED = "price_not_current"


@dataclass(frozen=True, slots=True)
class GeminiRequestBound:
    """요청 하나의 보수적 상한 — 입력·출력 토큰과 그 금액."""

    input_tokens: int
    output_tokens: int
    usd: Decimal


def price_current(profile: RawGeminiAnalysisProfile, *, on: date) -> bool:
    """이 날에 이 단가로 매겨도 되는가 — 확인한 날부터 공식 표가 알린 인상 전 마지막 날까지."""
    return profile.pricing_checked_at <= on <= profile.price_valid_through


def _usd(profile: RawGeminiAnalysisProfile, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * profile.input_per_mtok
        + Decimal(output_tokens) * profile.output_per_mtok
    ) / PRICE_TOKEN_UNIT


def clip_tokens(profile: RawGeminiAnalysisProfile, seconds: Fraction) -> int:
    """클립 하나의 토큰 상한 — 초당 장수로 표본한 영상(끝 한 장 더) + 초당 소리."""
    whole = math.ceil(seconds)
    frames = math.ceil(seconds * profile.video_frames_per_second) + 1
    return frames * profile.video_frame_tokens + whole * profile.audio_tokens_per_second


def window_ceiling_usd(
    profile: RawGeminiAnalysisProfile,
    settings: RawUnderstandingSettings,
    *,
    schema_bytes: int,
    on: date,
) -> Decimal | None:
    """창 하나의 승인 상한 — 텍스트 여유 + schema + 부대 여유 + 이미지 상한 + 클립 상한(창 + 앞뒤
    문맥 길이), 출력은 한도 전부.

    단가가 그날 유효하지 않으면 None(가격 미정)이다 — 오래된 단가로 싼 견적을 내지 않는다.
    """
    if not price_current(profile, on=on):
        return None
    longest_clip = Fraction(str(settings.window_seconds)) + 2 * Fraction(
        str(settings.context_seconds)
    )
    input_tokens = (
        profile.quote_text_tokens_per_window
        + schema_bytes
        + profile.quote_overhead_tokens
        + settings.max_images_per_window * profile.image_tokens
        + clip_tokens(profile, longest_clip)
    )
    return _usd(profile, input_tokens, profile.max_output_tokens)


def payload_bound(
    payload: Mapping[str, object], profile: RawGeminiAnalysisProfile, *, on: date
) -> GeminiRequestBound:
    """실제 페이로드 하나의 상한 — 모양을 모르는 페이로드는 잴 수 없으니 ValueError로 닫는다."""
    if not price_current(profile, on=on):
        raise ValueError(f"{profile.label} 단가가 {on.isoformat()}에 유효하지 않다 — 가격 미정")
    unknown = sorted(set(payload) ^ PAYLOAD_KEYS)
    if unknown:
        raise ValueError(f"상한을 잴 수 없는 페이로드 칸: {', '.join(unknown)}")
    max_tokens, schema_bytes = _generation_measure(payload["generationConfig"])
    text_bytes, image_tokens = _contents_measure(payload["contents"], profile)
    input_tokens = text_bytes + schema_bytes + profile.quote_overhead_tokens + image_tokens
    return GeminiRequestBound(input_tokens, max_tokens, _usd(profile, input_tokens, max_tokens))


def _generation_measure(config: object) -> tuple[int, int]:
    """(출력 한도, 응답 schema 바이트) — 칸·값이 아는 모양이 아니면 닫는다."""
    if not isinstance(config, Mapping) or set(config) != GENERATION_CONFIG_KEYS:
        raise ValueError("generationConfig 칸 모양을 모른다")
    max_tokens = config["maxOutputTokens"]
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= GEMINI_MAX_OUTPUT_TOKENS_LIMIT
    ):
        raise ValueError(f"maxOutputTokens가 1~{GEMINI_MAX_OUTPUT_TOKENS_LIMIT} 정수가 아니다")
    if config["responseMimeType"] != JSON_MIME_TYPE:
        raise ValueError("responseMimeType이 application/json이 아니다")
    schema = config["responseJsonSchema"]
    if not isinstance(schema, Mapping):
        raise ValueError("responseJsonSchema가 객체가 아니다")
    encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
    return max_tokens, len(encoded)


def _contents_measure(contents: object, profile: RawGeminiAnalysisProfile) -> tuple[int, int]:
    """(텍스트 UTF-8 바이트, 이미지 토큰) — 사용자 차례 하나의 부분만 잰다."""
    if not isinstance(contents, list) or len(contents) != 1:
        raise ValueError("contents는 사용자 차례 하나여야 한다")
    (turn,) = contents
    if not isinstance(turn, Mapping) or set(turn) != {"role", "parts"}:
        raise ValueError("contents[0] 모양을 모른다")
    parts = turn["parts"]
    if turn["role"] != USER_ROLE or not isinstance(parts, list) or not parts:
        raise ValueError("contents[0]은 부분이 있는 사용자 차례여야 한다")
    text_bytes = media_tokens = 0
    for part in parts:
        if isinstance(part, Mapping) and set(part) == {"text"} and isinstance(part["text"], str):
            text_bytes += len(part["text"].encode("utf-8"))
        elif _fixed_image(part, profile):
            media_tokens += profile.image_tokens
        else:
            seconds = _clip_seconds(part, profile)
            if seconds is None:
                raise ValueError(
                    "토큰을 잴 수 없는 부분이다(글자·고정 해상도 JPEG·길이를 적은 클립만)"
                )
            media_tokens += clip_tokens(profile, seconds)
    return text_bytes, media_tokens


def _clip_seconds(part: object, profile: RawGeminiAnalysisProfile) -> Fraction | None:
    """길이를 적은 고정 해상도 MP4 클립이면 그 길이 — 아니면 None(잴 수 없다)."""
    if not isinstance(part, Mapping) or set(part) != VIDEO_PART_KEYS:
        return None
    if not _inline_base64(part["inlineData"], VIDEO_MIME_TYPE):
        return None
    if part["mediaResolution"] != {"level": profile.media_resolution}:
        return None
    metadata = part["videoMetadata"]
    if not isinstance(metadata, Mapping) or set(metadata) != {"startOffset", "endOffset", "fps"}:
        return None
    if metadata["fps"] != profile.video_frames_per_second:
        return None
    start, end = _duration(metadata["startOffset"]), _duration(metadata["endOffset"])
    if start is None or end is None or end <= start:
        return None
    return end - start


def _duration(value: object) -> Fraction | None:
    if not isinstance(value, str) or not DURATION.fullmatch(value):
        return None
    return Fraction(value[:-1])


def _inline_base64(inline: object, mime_type: str) -> bool:
    if not isinstance(inline, Mapping) or set(inline) != {"mimeType", "data"}:
        return False
    data = inline["data"]
    if inline["mimeType"] != mime_type or not isinstance(data, str):
        return False
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def _fixed_image(part: object, profile: RawGeminiAnalysisProfile) -> bool:
    """해상도가 고정된 base64 JPEG 부분인가 — 그때만 장당 토큰이 상한 `image_tokens`다."""
    if not isinstance(part, Mapping) or set(part) != IMAGE_PART_KEYS:
        return False
    if part["mediaResolution"] != {"level": profile.media_resolution}:
        return False
    return _inline_base64(part["inlineData"], JPEG_MIME_TYPE)


def request_bytes(payload: Mapping[str, object]) -> int:
    """전송이 보내는 압축 JSON 본문의 바이트 — 인라인 상한(20 MB)과 견준다."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(body.encode("utf-8"))


# ─────────────────────────────────────────────────────────────
#  영수증 — 응답이 말한 토큰을 공식 스냅샷으로 계산한다
# ─────────────────────────────────────────────────────────────


def measure_gemini_usage(meta: Any, response: object, *, on: date) -> MeasuredUsage:
    """관찰자가 본 generateContent 응답 → 실측 사용량. 못 매기면 가격 미정, 토큰은 남긴다.

    입력 = `promptTokenCount`, 출력 = `candidatesTokenCount` + `thoughtsTokenCount`(생각은 출력
    단가로 청구된다 — thinking 문서). 응답의 `modelVersion`이 고른 모델로 시작하지 않으면(다른
    모델이 답했다) 이 단가로 매기지 않는다. 단가가 그날 유효하지 않아도 가격 미정이다.
    """
    model = str(getattr(meta, "model", ""))
    body: Mapping[str, Any] = response if isinstance(response, Mapping) else {}
    profile = GEMINI_ANALYSIS_PROFILES.get(model)
    usage = _gemini_usage(body.get("usageMetadata"))
    common: dict[str, Any] = {
        "model": model,
        "pricing_revision": profile.pricing_revision if profile is not None else "",
        "input_tokens": usage[0] if usage is not None else None,
        "output_tokens": usage[1] if usage is not None else None,
        "provider_request_id": str(body.get("responseId") or "") or None,
    }
    if profile is None:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_UNKNOWN_MODEL)
    if usage is None:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_INVALID_USAGE)
    version = body.get("modelVersion")
    if not isinstance(version, str) or not version.startswith(model):
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_MODEL_MISMATCH)
    if not price_current(profile, on=on):
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_PRICE_EXPIRED)
    return MeasuredUsage(**common, cost_usd=_decimal_text(_usd(profile, *usage)))


def _gemini_usage(raw: object) -> tuple[int, int] | None:
    """`usageMetadata` → (입력, 출력+생각). 음수·문자열·빠진 칸은 None(가격 미정)이다."""
    if not isinstance(raw, Mapping):
        return None
    prompt, candidates = raw.get("promptTokenCount"), raw.get("candidatesTokenCount", 0)
    thoughts = raw.get("thoughtsTokenCount", 0)
    if not (_count(prompt) and _count(candidates) and _count(thoughts)):
        return None
    return prompt, candidates + thoughts


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _decimal_text(value: Decimal) -> str:
    """영수증의 금액 문자열 — Claude 영수증과 같은 표기(0은 "0", 나머지는 고정소수점)."""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


# ─────────────────────────────────────────────────────────────
#  시간 엔진 Gemini 3.5 Transcribe의 영수증(W-31c, D92)
# ─────────────────────────────────────────────────────────────


def measure_gemini_transcribe_usage(meta: Any, response: object, *, on: date) -> MeasuredUsage:
    """관찰자가 본 Interactions 전사 응답 → 실측 사용량. 못 매기면 가격 미정, 토큰은 남긴다.

    입력 = `usage.total_input_tokens`(오디오), 출력 = `usage.total_output_tokens`(글자) — 등록표의
    100만 토큰당 단가($2 · $12)로 계산한다(가격표의 분당 값과 같은 칸). 응답의 `model`이 고른 모델로
    시작하지 않으면 다른 모델이 답했으니 매기지 않는다. 재확인 기한이 지나도 가격 미정이다. 칸
    이름은 문서 모양이다 — 사람의 녹화(H1)가 다르면 여기만 고친다(그때까지 다르면 가격 미정으로
    남는다).
    """
    model = str(getattr(meta, "model", ""))
    body: Mapping[str, Any] = response if isinstance(response, Mapping) else {}
    profile = GEMINI_TRANSCRIBE_PROFILES.get(model)
    usage = _transcribe_usage(body.get("usage"))
    common: dict[str, Any] = {
        "model": model,
        "pricing_revision": profile.pricing_revision if profile is not None else "",
        "input_tokens": usage[0] if usage is not None else None,
        "output_tokens": usage[1] if usage is not None else None,
        "provider_request_id": str(body.get("id") or "") or None,
    }
    if profile is None:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_UNKNOWN_MODEL)
    if usage is None:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_INVALID_USAGE)
    answered = body.get("model")
    if isinstance(answered, str) and not answered.startswith(model):
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_MODEL_MISMATCH)
    if gemini_transcribe_price_per_minute(profile, today=on) is None:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_PRICE_EXPIRED)
    cost = (
        Decimal(usage[0]) * profile.input_per_mtok + Decimal(usage[1]) * profile.output_per_mtok
    ) / PRICE_TOKEN_UNIT
    return MeasuredUsage(**common, cost_usd=_decimal_text(cost))


def _transcribe_usage(raw: object) -> tuple[int, int] | None:
    """`usage` → (입력, 출력). 음수·문자열·빠진 칸은 None(가격 미정)이다."""
    if not isinstance(raw, Mapping):
        return None
    total_in, total_out = raw.get("total_input_tokens"), raw.get("total_output_tokens")
    if not (_count(total_in) and _count(total_out)):
        return None
    return total_in, total_out
