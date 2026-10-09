"""Claude 의미 분석의 비용 산술(D54) — 순수 함수. 호출·파일·환경을 모른다.

상한(ceiling)은 승인 전에 보여 주는 **보수적** 금액이고, 영수증(`measure_claude_usage`)은
응답이 말한 토큰을 공식 단가 스냅샷으로 **계산한** 값이다 — 어느 쪽도 공급자의 청구서가
아니다. 못 매기면 가격 미정이다(0원이 아니다).

가정(D54): 텍스트 토큰 수는 UTF-8 바이트 수 이하다 — 문서화된 값이 아니므로 첫 사람 실행의
영수증이 확인한다. 이미지는 실제 페이로드에서는 JPEG가 말하는 크기로, 견적(창당 상한)에서는
긴 변 L의 정사각형으로 센다 — 분석 프레임은 두 변이 모두 `image_long_edge` 이하로 뽑히므로
실제 프레임이 그보다 크면 실행 문맥의 재측정이 상한을 넘겨 닫힌다.

단가표는 표준 단가 하나뿐이다 — 요청은 `inference_geo=global`을 명시하고(`service_tier`는
싣지 않는다 — Opus 5.5에는 우선 등급이 없다), 응답이 다른 지역·등급을 말하면 영수증은 가격
미정이다(US 전용은 1.1배, D54).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeGuard

from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    CLAUDE_ANALYSIS_PROFILES,
    CLAUDE_STANDARD_INFERENCE_GEO,
    CLAUDE_STANDARD_USAGE_GEOS,
    CLAUDE_STANDARD_USAGE_TIERS,
    PRICE_TOKEN_UNIT,
    RawClaudeAnalysisProfile,
)
from vlogsrt.project.request_accounting import MeasuredUsage

PAYLOAD_KEYS = frozenset({"max_tokens", "messages", "output_config", "inference_geo"})
"""상한을 잴 수 있는 페이로드의 칸 전부 — 다른 칸이 있으면 무엇이 청구될지 모른다."""

OUTPUT_CONFIG_KEYS = frozenset({"effort", "format"})
JPEG_MEDIA_TYPE = "image/jpeg"

# JPEG 형식의 구조 상수(ITU T.81) — 조정값이 아니라 파일 형식 자체다.
JPEG_SOI = b"\xff\xd8"
JPEG_MARKER_PREFIX = 0xFF
JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
JPEG_IMAGE_START_MARKERS = frozenset({0xD9, 0xDA})
JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC9, 0xCA, 0xCB})
"""비계층 프레임의 SOF — 순차·확장·점진·무손실(허프만·산술)."""
JPEG_HIERARCHICAL_MARKERS = frozenset({0xDE, 0xC5, 0xC6, 0xC7, 0xCD, 0xCE, 0xCF})
"""계층 JPEG의 DHP와 차분 프레임 SOF5~7·13~15 — 프레임 크기가 그림 크기가 아니다."""
_SEGMENT_HEADER = 4
"""표지 2바이트 + 길이 2바이트."""
_SOF_SIZE_END = 5
"""SOF 본문에서 정밀도·세로·가로가 끝나는 자리."""

UNPRICED_UNKNOWN_MODEL = "unknown_model"
UNPRICED_INVALID_USAGE = "missing_or_invalid_usage"
UNPRICED_MODEL_MISMATCH = "model_mismatch"
UNPRICED_NONSTANDARD_GEO = "nonstandard_inference_geo"
UNPRICED_NONSTANDARD_TIER = "nonstandard_service_tier"


@dataclass(frozen=True, slots=True)
class ClaudeRequestBound:
    """요청 하나의 보수적 상한 — 입력·출력 토큰과 그 금액."""

    input_tokens: int
    output_tokens: int
    usd: Decimal


def per_image_bound(profile: RawClaudeAnalysisProfile, long_edge: int) -> int:
    """이미지 한 장의 토큰 상한 — ⌈L/28⌉² 와 고해상도 등급 상한 중 작은 쪽."""
    patches = -(-long_edge // profile.image_patch_pixels)
    return min(patches * patches, profile.image_max_visual_tokens)


def _usd(profile: RawClaudeAnalysisProfile, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * profile.input_per_mtok
        + Decimal(output_tokens) * profile.output_per_mtok
    ) / PRICE_TOKEN_UNIT


def window_ceiling_usd(
    profile: RawClaudeAnalysisProfile, settings: RawUnderstandingSettings, *, schema_bytes: int
) -> Decimal:
    """창 하나의 승인 상한 — 텍스트 여유 + schema + 부대 여유 + 이미지 상한, 출력은 한도 전부.

    생각(thinking) 토큰은 출력으로 청구되고 `max_tokens` 안에 든다 — 출력을 한도 전부로
    세면 생각까지 덮는다.
    """
    input_tokens = (
        profile.quote_text_tokens_per_window
        + schema_bytes
        + profile.quote_overhead_tokens
        + settings.max_images_per_window * per_image_bound(profile, settings.image_long_edge)
    )
    return _usd(profile, input_tokens, profile.max_output_tokens)


def payload_bound(
    payload: Mapping[str, object], profile: RawClaudeAnalysisProfile
) -> ClaudeRequestBound:
    """실제 페이로드 하나의 상한 — 모양을 모르는 페이로드는 잴 수 없으니 ValueError로 닫는다.

    지역이 표준 단가 값이 아니면 이 단가표로 잴 수 없다(US 전용은 1.1배) — 닫는다. 실리지
    않는 칸(`service_tier` 등)은 모르는 칸이다. 이미지는 설정의 긴 변이 아니라 JPEG가 말하는
    실제 크기로 센다.
    """
    unknown = sorted(set(payload) ^ PAYLOAD_KEYS)
    if unknown:
        raise ValueError(f"상한을 잴 수 없는 페이로드 칸: {', '.join(unknown)}")
    if payload["inference_geo"] != CLAUDE_STANDARD_INFERENCE_GEO:
        raise ValueError(f"표준 단가가 아닌 추론 지역이다: {payload['inference_geo']!r}")
    max_tokens = payload["max_tokens"]
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError(f"max_tokens가 1 이상 정수가 아니다: {max_tokens!r}")
    text_bytes, image_tokens = _content_measure(payload["messages"], profile)
    input_tokens = (
        text_bytes
        + _schema_bytes(payload["output_config"])
        + profile.quote_overhead_tokens
        + image_tokens
    )
    return ClaudeRequestBound(input_tokens, max_tokens, _usd(profile, input_tokens, max_tokens))


def _content_measure(messages: object, profile: RawClaudeAnalysisProfile) -> tuple[int, int]:
    """사용자 메시지의 (텍스트 UTF-8 바이트 합, 이미지 토큰 상한 합) — 모르는 블록은 닫는다."""
    if not isinstance(messages, Sequence) or isinstance(messages, str) or not messages:
        raise ValueError("messages가 비어 있지 않은 배열이 아니다")
    text_bytes = image_tokens = 0
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise ValueError("메시지 모양이 role·content가 아니다")
        if message["role"] != "user" or not isinstance(message["content"], Sequence):
            raise ValueError("사용자 메시지의 content 배열만 잴 수 있다")
        for block in message["content"]:
            if _is_text_block(block):
                text_bytes += len(block["text"].encode("utf-8"))
            elif _is_jpeg_block(block):
                image_tokens += image_token_bound(profile, _decoded(block["source"]["data"]))
            else:
                raise ValueError(f"상한을 잴 수 없는 content 블록: {block!r:.80}")
    return text_bytes, image_tokens


def _decoded(data: str) -> bytes:
    """base64 본문 — 깨진 문자열은 빈 바이트로 돌려 상한 쪽(이미지당 최대)으로 센다."""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return b""


def image_token_bound(profile: RawClaudeAnalysisProfile, data: bytes) -> int:
    """이미지 한 장의 토큰 상한 — ⌈w/28⌉×⌈h/28⌉와 등급 상한 중 작은 쪽, 크기를 모르면 등급 상한."""
    size = jpeg_dimensions(data)
    if size is None:
        return profile.image_max_visual_tokens
    width, height = size
    patch = profile.image_patch_pixels
    patches = -(-width // patch) * -(-height // patch)
    return min(patches, profile.image_max_visual_tokens)


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """JPEG의 (가로, 세로) — 첫 SOF 세그먼트에서 읽는다. 모양이 어긋나면 None(보수적으로 센다)."""
    if data[: len(JPEG_SOI)] != JPEG_SOI:
        return None
    position = len(JPEG_SOI)
    while position + _SEGMENT_HEADER <= len(data):
        if data[position] != JPEG_MARKER_PREFIX:
            return None
        marker = data[position + 1]
        if marker == JPEG_MARKER_PREFIX:  # 채움 바이트 — 다음 바이트가 진짜 표지다
            position += 1
            continue
        if marker in JPEG_STANDALONE_MARKERS:
            position += 2
            continue
        if marker in JPEG_IMAGE_START_MARKERS:  # SOF보다 먼저 영상·끝이 오면 크기를 모른다
            return None
        if marker in JPEG_HIERARCHICAL_MARKERS:  # 계층 JPEG는 크기를 믿지 않는다(보수적)
            return None
        length = int.from_bytes(data[position + 2 : position + _SEGMENT_HEADER], "big")
        end = position + 2 + length
        if length < 2 or end > len(data):
            return None
        if marker in JPEG_SOF_MARKERS:
            return _sof_size(data[position + _SEGMENT_HEADER : end])
        position = end
    return None


def _sof_size(segment: bytes) -> tuple[int, int] | None:
    """SOF 본문(정밀도 1바이트 · 세로 2바이트 · 가로 2바이트 …)의 (가로, 세로)."""
    if len(segment) < _SOF_SIZE_END:
        return None
    height = int.from_bytes(segment[1:3], "big")
    width = int.from_bytes(segment[3:_SOF_SIZE_END], "big")
    return (width, height) if width and height else None


def _is_text_block(block: object) -> TypeGuard[Mapping[str, Any]]:
    return (
        isinstance(block, Mapping)
        and set(block) == {"type", "text"}
        and block["type"] == "text"
        and isinstance(block["text"], str)
    )


def _is_jpeg_block(block: object) -> TypeGuard[Mapping[str, Any]]:
    if not isinstance(block, Mapping) or set(block) != {"type", "source"}:
        return False
    source = block["source"]
    return (
        block["type"] == "image"
        and isinstance(source, Mapping)
        and set(source) == {"type", "media_type", "data"}
        and source["type"] == "base64"
        and source["media_type"] == JPEG_MEDIA_TYPE
        and isinstance(source["data"], str)
    )


def _schema_bytes(output_config: object) -> int:
    """출력 schema의 압축 JSON 바이트 — 전송이 보내는 그 직렬화 그대로다."""
    if not isinstance(output_config, Mapping) or not set(output_config) <= OUTPUT_CONFIG_KEYS:
        raise ValueError("output_config 모양을 모른다")
    form = output_config.get("format")
    if not isinstance(form, Mapping) or set(form) != {"type", "schema"}:
        raise ValueError("output_config.format이 json_schema 모양이 아니다")
    if form["type"] != "json_schema" or not isinstance(form["schema"], Mapping):
        raise ValueError("output_config.format이 json_schema 모양이 아니다")
    return len(json.dumps(form["schema"], ensure_ascii=False, separators=(",", ":")).encode())


# ─────────────────────────────────────────────────────────────
#  영수증 — 응답이 말한 토큰을 공식 스냅샷으로 계산한다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClaudeUsage:
    """응답 `usage`의 검사를 지난 토큰 수 — 캐시 생성은 TTL별로 나뉘었으면 나뉜 대로."""

    input_tokens: int
    output_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_unsplit: int
    cache_read: int

    @property
    def total_input(self) -> int:
        """모든 입력 범주의 합 — 캐시도 입력이다(영수증의 input_tokens)."""
        return (
            self.input_tokens
            + self.cache_write_5m
            + self.cache_write_1h
            + self.cache_write_unsplit
            + self.cache_read
        )


def measure_claude_usage(meta: Any, response: object) -> MeasuredUsage:
    """관찰자가 본 Messages 응답 → 실측 사용량. 못 매기면 가격 미정, 토큰은 남긴다."""
    model = str(getattr(meta, "model", ""))
    body: Mapping[str, Any] = response if isinstance(response, Mapping) else {}
    profile = CLAUDE_ANALYSIS_PROFILES.get(model)
    usage = parse_claude_usage(body.get("usage"))
    common: dict[str, Any] = {
        "model": model,
        "pricing_revision": profile.pricing_revision if profile is not None else "",
        "input_tokens": usage.total_input if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        "provider_request_id": str(body.get("request_id") or "") or None,
    }
    if profile is None:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_UNKNOWN_MODEL)
    if usage is None:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_INVALID_USAGE)
    if body.get("model") != model:
        return MeasuredUsage(**common, unpriced_reason=UNPRICED_MODEL_MISMATCH)
    off_rate = _nonstandard_rate(body["usage"])
    if off_rate is not None:
        return MeasuredUsage(**common, unpriced_reason=off_rate)
    return MeasuredUsage(**common, cost_usd=_decimal_text(claude_cost_usd(profile, usage)))


def _nonstandard_rate(usage: Mapping[str, Any]) -> str | None:
    """응답이 말한 지역·등급이 표준 단가가 아니면 그 이유 — 표준 값으로 적으면 덜 적힌다.

    요청은 `inference_geo=global`을 명시하지만, 응답이 다른 값을 말하면 그 값을 믿는다 —
    1.1배를 $4로 적는 영수증은 만들지 않는다. `service_tier`는 요청에 싣지 않으니 응답이
    말하는 등급만 본다.
    """
    geo = usage.get("inference_geo")
    # 보수적인 쪽이다: 문서상 단가는 **요청한** 지역을 따르고, 이 칸은 추론이 **실제로**
    # 돈 곳을 말한다. global을 요청했으니 이 칸이 us여도 실제 청구는 표준일 수 있다 —
    # 그래도 확인 전에는 표준 값을 적지 않고 가격 미정으로 둔다. 첫 사람 실행의 영수증과
    # 청구서로 이 칸의 실제 값을 확인한다(D54).
    if geo is not None and geo not in CLAUDE_STANDARD_USAGE_GEOS:
        return UNPRICED_NONSTANDARD_GEO
    tier = usage.get("service_tier")
    if tier is not None and tier not in CLAUDE_STANDARD_USAGE_TIERS:
        return UNPRICED_NONSTANDARD_TIER
    return None


def parse_claude_usage(raw: object) -> ClaudeUsage | None:
    """`usage`를 검사한다 — 음수·문자열·어긋난 캐시 분해는 None(가격 미정)이다."""
    if not isinstance(raw, Mapping):
        return None
    input_tokens, output_tokens = raw.get("input_tokens"), raw.get("output_tokens")
    if not (_count(input_tokens) and _count(output_tokens)):
        return None
    split = raw.get("cache_creation")
    if split is not None and not isinstance(split, Mapping):
        return None
    optional = (
        raw.get("cache_creation_input_tokens"),
        raw.get("cache_read_input_tokens"),
        (split or {}).get("ephemeral_5m_input_tokens"),
        (split or {}).get("ephemeral_1h_input_tokens"),
    )
    if not all(value is None or _count(value) for value in optional):
        return None
    created, read, five, hour = (int(value or 0) for value in optional)
    if five + hour > created:
        return None  # 분해가 합보다 크다 — 어느 쪽을 믿을지 모른다
    return ClaudeUsage(input_tokens, output_tokens, five, hour, created - five - hour, read)


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def claude_cost_usd(profile: RawClaudeAnalysisProfile, usage: ClaudeUsage) -> Decimal:
    """범주마다 제 단가로 — TTL 분해가 없는 캐시 생성은 비싼 쪽(1시간)으로 센다(보수적)."""
    total = (
        Decimal(usage.input_tokens) * profile.input_per_mtok
        + Decimal(usage.cache_write_5m) * profile.cache_write_5m_per_mtok
        + Decimal(usage.cache_write_1h + usage.cache_write_unsplit)
        * profile.cache_write_1h_per_mtok
        + Decimal(usage.cache_read) * profile.cache_read_per_mtok
        + Decimal(usage.output_tokens) * profile.output_per_mtok
    )
    return total / PRICE_TOKEN_UNIT


def _decimal_text(value: Decimal) -> str:
    """영수증의 금액 문자열 — 회계 원장과 같은 표기(0은 "0", 나머지는 정규화한 고정소수점)."""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")
