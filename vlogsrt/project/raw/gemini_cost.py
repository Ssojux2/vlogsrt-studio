"""Gemini 의미 분석의 비용 산술(W-30b, D84) — 순수 함수. 호출·파일·환경을 모른다.

`claude_cost.py`의 거울이다. 상한(ceiling)은 승인 전에 보여 주는 **보수적** 금액이다 — 공급자의
청구서가 아니다. 못 매기면 가격 미정이다(0원이 아니다). 영수증(응답이 말한 토큰의 계산)은 W-30c가
사용량 관찰과 함께 더한다.

근거(공식 문서, 2026-10-25 확인):
- 생각 토큰은 출력 단가로 청구되고 `maxOutputTokens` 안에 든다 — 기반 시설이 강제하는 끊김이라
  출력을 한도 전부로 세면 생각까지 덮는다(thinking 문서).
- Gemini 3·4의 이미지는 `MEDIA_RESOLUTION_HIGH`에서 장당 1120토큰이 상한이다(media resolution
  문서) — 크기와 무관하다. 해상도를 적지 않은 이미지는 기본값이 바뀌면 토큰을 모르므로 닫는다.
- 단가는 공식 표가 미리 알린 인상 전 마지막 날까지만 쓴다(`price_valid_through`).

가정(D84, Claude와 같다): 텍스트 토큰 수는 UTF-8 바이트 수 이하다 — 문서화된 값이 아니므로 첫 사람
실행의 영수증이 확인한다. 응답 schema도 입력으로 센다(보수적).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    GEMINI_MAX_OUTPUT_TOKENS_LIMIT,
    PRICE_TOKEN_UNIT,
    RawGeminiAnalysisProfile,
)

PAYLOAD_KEYS = frozenset({"contents", "generationConfig"})
"""상한을 잴 수 있는 페이로드의 칸 전부 — 다른 칸이 있으면 무엇이 청구될지 모른다."""
GENERATION_CONFIG_KEYS = frozenset(
    {"maxOutputTokens", "responseMimeType", "responseJsonSchema", "thinkingConfig"}
)
"""`generationConfig`의 칸 전부 — 후보 수·도구 같은 칸은 청구를 늘릴 수 있다."""
JSON_MIME_TYPE = "application/json"
JPEG_MIME_TYPE = "image/jpeg"
IMAGE_PART_KEYS = frozenset({"inlineData", "mediaResolution"})
USER_ROLE = "user"


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


def window_ceiling_usd(
    profile: RawGeminiAnalysisProfile,
    settings: RawUnderstandingSettings,
    *,
    schema_bytes: int,
    on: date,
) -> Decimal | None:
    """창 하나의 승인 상한 — 텍스트 여유 + schema + 부대 여유 + 이미지 상한, 출력은 한도 전부.

    단가가 그날 유효하지 않으면 None(가격 미정)이다 — 오래된 단가로 싼 견적을 내지 않는다.
    """
    if not price_current(profile, on=on):
        return None
    input_tokens = (
        profile.quote_text_tokens_per_window
        + schema_bytes
        + profile.quote_overhead_tokens
        + settings.max_images_per_window * profile.image_tokens
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
    text_bytes = image_tokens = 0
    for part in parts:
        if isinstance(part, Mapping) and set(part) == {"text"} and isinstance(part["text"], str):
            text_bytes += len(part["text"].encode("utf-8"))
        elif _fixed_image(part, profile):
            image_tokens += profile.image_tokens
        else:
            raise ValueError("토큰을 잴 수 없는 부분이다(글자·고정 해상도 JPEG만)")
    return text_bytes, image_tokens


def _fixed_image(part: object, profile: RawGeminiAnalysisProfile) -> bool:
    """해상도가 고정된 base64 JPEG 부분인가 — 그때만 장당 토큰이 상한 `image_tokens`다."""
    if not isinstance(part, Mapping) or set(part) != IMAGE_PART_KEYS:
        return False
    inline, resolution = part["inlineData"], part["mediaResolution"]
    if not isinstance(inline, Mapping) or set(inline) != {"mimeType", "data"}:
        return False
    if resolution != {"level": profile.media_resolution} or inline["mimeType"] != JPEG_MIME_TYPE:
        return False
    data = inline["data"]
    if not isinstance(data, str):
        return False
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True
