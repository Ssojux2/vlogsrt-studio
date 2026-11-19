"""W-30b — Gemini 의미 분석의 프로파일과 견적 산술(D84). 순수 함수, 유료 호출 0회.

시험이 못 박는 것:

- 등록표 둘(3.8 Flash · 4 Argon)의 단가·한도는 2026-10-25에 확인한 공식 값 그대로다.
- 창당 상한 = (텍스트 여유 + schema + 부대 여유 + 이미지 장수 × 1120) × 입력 단가
  + 출력 한도 × 출력 단가 — 생각 토큰은 `maxOutputTokens` 안에 든다.
- 공식 표가 알린 인상일이 지나면 그 단가로 매기지 않는다(가격 미정 — None, 0원이 아니다).
- 실제 페이로드의 상한은 모양을 아는 칸만 잰다 — 모르는 칸·해상도 없는 이미지·JPEG가 아닌 이미지는
  닫는다(잴 수 없으면 승인 상한을 넘을 수 있다).
- 아직 고를 수 있는 모델이 아니다 — 공급자 판정·화면은 W-30c·d가 잇는다.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction as F

import pytest

from vlogsrt.project.raw import config as raw_config
from vlogsrt.project.raw import gemini_cost
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    GEMINI_ANALYSIS_PROFILES,
    GEMINI_ARGON_ANALYSIS_PROFILE,
    GEMINI_FLASH_ANALYSIS_PROFILE,
    gemini_profile,
    gemini_profile_problems,
)

FLASH = GEMINI_FLASH_ANALYSIS_PROFILE
ARGON = GEMINI_ARGON_ANALYSIS_PROFILE
SETTINGS = RawUnderstandingSettings()
TODAY = date(2026, 10, 25)
SCHEMA = {"type": "object", "properties": {"window_id": {"type": "string"}}}
SCHEMA_BYTES = len(json.dumps(SCHEMA, ensure_ascii=False, separators=(",", ":")).encode())
JPEG = base64.b64encode(b"\xff\xd8\xff\xd9").decode()


def image_part(**overrides: object) -> dict[str, object]:
    part: dict[str, object] = {
        "inlineData": {"mimeType": "image/jpeg", "data": JPEG},
        "mediaResolution": {"level": "MEDIA_RESOLUTION_HIGH"},
    }
    part.update(overrides)
    return part


def payload(parts: list[dict[str, object]], **config: object) -> dict[str, object]:
    generation: dict[str, object] = {
        "maxOutputTokens": FLASH.max_output_tokens,
        "responseMimeType": "application/json",
        "responseJsonSchema": SCHEMA,
        "thinkingConfig": {"thinkingLevel": "MEDIUM"},
    }
    generation.update(config)
    return {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}


# ── 등록표 ───────────────────────────────────────────────────────────────


def test_the_registry_holds_the_official_snapshot_of_2026_10_25() -> None:
    # D168: Hydrogen(2026-11-19 확인)이 기본으로 앞에 섰다 — Flash·Argon의 10-25 판은 그대로다.
    assert list(GEMINI_ANALYSIS_PROFILES) == [
        "gemini-4-hydrogen",
        "gemini-3.8-flash",
        "gemini-4-argon",
    ]
    assert (FLASH.input_per_mtok, FLASH.output_per_mtok) == (Decimal("0.75"), Decimal("3.75"))
    assert FLASH.price_valid_through == date(2026, 12, 31)
    assert (ARGON.input_per_mtok, ARGON.output_per_mtok) == (Decimal("2"), Decimal("10"))
    assert ARGON.price_valid_through == date(2026, 11, 30)
    for profile in (FLASH, ARGON):
        assert profile.pricing_checked_at == TODAY
        assert profile.image_tokens == 1120
        assert profile.media_resolution == "MEDIA_RESOLUTION_HIGH"
        assert profile.thinking_level == "MEDIUM"
        assert profile.api_base_url == "https://generativelanguage.googleapis.com"
        assert profile.api_version == "v1beta"
        assert gemini_profile_problems(profile) == []
        assert gemini_profile(profile.model) is profile


def test_an_unregistered_gemini_model_is_refused() -> None:
    with pytest.raises(ValueError, match="등록되지 않은 Gemini"):
        gemini_profile("gemini-3.6-flash")


@pytest.mark.parametrize(
    "changes",
    [
        {"input_per_mtok": Decimal("0")},
        {"output_per_mtok": Decimal("NaN")},
        {"thinking_level": "MINIMAL"},
        {"media_resolution": "MEDIA_RESOLUTION_LOW"},
        {"max_output_tokens": 65_537},
        {"max_output_tokens": 0},
        {"api_base_url": "http://generativelanguage.googleapis.com"},
        {"api_base_url": "https://generativelanguage.googleapis.com/v1beta"},
        {"image_tokens": 0},
        {"price_valid_through": date(2026, 10, 1)},
        {"model": "claude-gemini"},
    ],
)
def test_a_profile_that_breaks_its_contract_is_reported(changes: dict[str, object]) -> None:
    assert gemini_profile_problems(replace(FLASH, **changes))  # type: ignore[arg-type]


def test_gemini_is_its_own_provider_with_an_official_price() -> None:
    """W-30c2 — `gemini-*`은 google 공급자이고 공식 단가로 창당 상한을 잰다(D86)."""
    assert raw_config.analysis_provider("gemini-3.8-flash") == raw_config.ANALYSIS_PROVIDER_GOOGLE
    assert raw_config.official_price_model("gemini-4-argon")
    assert raw_config.official_price_model("claude-sonnet-5-5")
    assert not raw_config.official_price_model("gpt-6-luna")
    assert raw_config.analysis_model_label("gemini-3.8-flash") == "Gemini 3.8 Flash"


def test_an_unregistered_gemini_analysis_model_is_refused_by_the_settings() -> None:
    roles = raw_config.RawModelRoles(luna_model="gemini-3.6-flash")
    assert any("등록되지 않은 Gemini" in p for p in raw_config.role_problems(roles))
    ok = raw_config.RawModelRoles(luna_model="gemini-3.8-flash")
    assert raw_config.role_problems(ok) == []


@pytest.mark.parametrize("role", ["text_model", "timing_model"])
def test_gemini_cannot_take_the_text_or_timing_role(role: str) -> None:
    roles = replace(raw_config.RawModelRoles(), **{role: "gemini-3.8-flash"})
    assert any("Gemini는 영상 분석" in p for p in raw_config.role_problems(roles))


# ── 단가 유효 기간 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "last_day"),
    [(FLASH, date(2026, 12, 31)), (ARGON, date(2026, 11, 30))],
)
def test_the_price_is_current_through_the_last_announced_day_only(
    profile: raw_config.RawGeminiAnalysisProfile, last_day: date
) -> None:
    assert gemini_cost.price_current(profile, on=TODAY)
    assert gemini_cost.price_current(profile, on=last_day)
    assert not gemini_cost.price_current(profile, on=date.fromordinal(last_day.toordinal() + 1))
    assert not gemini_cost.price_current(profile, on=date(2026, 10, 24))  # 확인하기 전 날


# ── 창당 상한 ────────────────────────────────────────────────────────────


def test_the_window_ceiling_counts_text_schema_overhead_images_clip_and_the_whole_output() -> None:
    ceiling = gemini_cost.window_ceiling_usd(FLASH, SETTINGS, schema_bytes=941, on=TODAY)
    clip = (19 + 1) * 280 + 19 * 32  # 창 15초 + 앞뒤 문맥 2초 · 초당 한 장(끝 한 장 더) + 소리
    input_tokens = 12000 + 941 + 2000 + SETTINGS.max_images_per_window * 1120 + clip
    expected = (Decimal(input_tokens) * Decimal("0.75") + Decimal(12000) * Decimal("3.75")) / (
        Decimal(1_000_000)
    )
    assert ceiling == expected
    assert Decimal("0.06") < ceiling < Decimal("0.07")  # Sonnet 5.5 창당 약 $0.16의 40% 남짓


def test_argon_costs_more_per_window() -> None:
    flash = gemini_cost.window_ceiling_usd(FLASH, SETTINGS, schema_bytes=941, on=TODAY)
    argon = gemini_cost.window_ceiling_usd(ARGON, SETTINGS, schema_bytes=941, on=TODAY)
    assert flash is not None and argon is not None and argon > flash


def test_after_the_announced_raise_the_ceiling_is_unpriced_not_zero() -> None:
    assert (
        gemini_cost.window_ceiling_usd(FLASH, SETTINGS, schema_bytes=941, on=date(2027, 1, 1))
        is None
    )


# ── 실제 페이로드의 상한 ───────────────────────────────────────────────────


def test_a_payload_bound_counts_text_bytes_schema_overhead_and_fixed_image_tokens() -> None:
    parts = [{"text": "frame_000001:"}, image_part(), {"text": "판정해 주세요"}]
    bound = gemini_cost.payload_bound(payload(parts), FLASH, on=TODAY)
    text_bytes = len(b"frame_000001:") + len("판정해 주세요".encode())
    assert bound.input_tokens == text_bytes + SCHEMA_BYTES + FLASH.quote_overhead_tokens + 1120
    assert bound.output_tokens == FLASH.max_output_tokens
    assert bound.usd == (
        Decimal(bound.input_tokens) * Decimal("0.75")
        + Decimal(bound.output_tokens) * Decimal("3.75")
    ) / Decimal(1_000_000)


@pytest.mark.parametrize(
    "broken",
    [
        {"extra": 1},  # 모르는 최상위 칸 — 무엇이 청구될지 모른다
        {"systemInstruction": {"parts": [{"text": "x"}]}},
    ],
)
def test_an_unknown_top_level_field_cannot_be_bounded(broken: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="칸"):
        gemini_cost.payload_bound({**payload([{"text": "a"}]), **broken}, FLASH, on=TODAY)


@pytest.mark.parametrize(
    "config",
    [
        {"tools": []},
        {"responseMimeType": "text/plain"},
        {"maxOutputTokens": 0},
        {"maxOutputTokens": True},
        {"maxOutputTokens": 65_537},
        {"candidateCount": 2},
    ],
)
def test_an_unknown_or_unbounded_generation_config_is_refused(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        gemini_cost.payload_bound(payload([{"text": "a"}], **config), FLASH, on=TODAY)


@pytest.mark.parametrize(
    "part",
    [
        image_part(mediaResolution={"level": "MEDIA_RESOLUTION_ULTRA_HIGH"}),
        {"inlineData": {"mimeType": "image/jpeg", "data": JPEG}},  # 해상도가 없으면 토큰을 모른다
        image_part(inlineData={"mimeType": "image/png", "data": JPEG}),
        image_part(inlineData={"mimeType": "image/jpeg", "data": "not base64!"}),
        {"fileData": {"fileUri": "https://x", "mimeType": "video/mp4"}},
        {"text": 3},
    ],
)
def test_a_part_whose_tokens_are_not_fixed_is_refused(part: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        gemini_cost.payload_bound(payload([part]), FLASH, on=TODAY)


@pytest.mark.parametrize(
    "contents",
    [
        [],
        [{"role": "model", "parts": [{"text": "a"}]}],
        [{"role": "user", "parts": [{"text": "a"}]}, {"role": "user", "parts": [{"text": "b"}]}],
        [{"role": "user", "parts": []}],
    ],
)
def test_only_one_user_turn_can_be_bounded(contents: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError):
        gemini_cost.payload_bound(
            {**payload([{"text": "a"}]), "contents": contents}, FLASH, on=TODAY
        )


def test_a_payload_after_the_announced_raise_cannot_be_bounded() -> None:
    with pytest.raises(ValueError, match="가격 미정"):
        gemini_cost.payload_bound(payload([{"text": "a"}]), FLASH, on=date(2027, 1, 1))


# ── 클립 부분(W-30c2, D86) ─────────────────────────────────────────────────

MP4 = base64.b64encode(b"\x00\x00\x00\x18ftypmp42").decode()


def clip_part(end: str = "19s", **overrides: object) -> dict[str, object]:
    part: dict[str, object] = {
        "inlineData": {"mimeType": "video/mp4", "data": MP4},
        "mediaResolution": {"level": "MEDIA_RESOLUTION_HIGH"},
        "videoMetadata": {"startOffset": "0s", "endOffset": end, "fps": 1},
    }
    part.update(overrides)
    return part


@pytest.mark.parametrize(
    ("seconds", "tokens"),
    [(F(19), 20 * 280 + 19 * 32), (F(1, 2), 2 * 280 + 1 * 32), (F(3), 4 * 280 + 3 * 32)],
)
def test_clip_tokens_count_one_frame_per_second_plus_the_last_and_audio(
    seconds: F, tokens: int
) -> None:
    assert gemini_cost.clip_tokens(FLASH, seconds) == tokens


def test_a_payload_with_a_clip_counts_its_declared_length() -> None:
    parts = [{"text": "clip:"}, clip_part("12.5s"), {"text": "x"}]
    bound = gemini_cost.payload_bound(payload(parts), FLASH, on=TODAY)
    assert bound.input_tokens == (
        len(b"clip:")
        + 1
        + SCHEMA_BYTES
        + FLASH.quote_overhead_tokens
        + gemini_cost.clip_tokens(FLASH, F(25, 2))
    )


@pytest.mark.parametrize(
    "part",
    [
        clip_part(videoMetadata={"startOffset": "0s", "endOffset": "19s"}),  # fps 없음
        clip_part(videoMetadata={"startOffset": "0s", "endOffset": "19s", "fps": 2}),
        clip_part("-1s"),
        clip_part("19"),
        clip_part("1e3s"),
        clip_part(videoMetadata={"startOffset": "5s", "endOffset": "5s", "fps": 1}),
        clip_part(mediaResolution={"level": "MEDIA_RESOLUTION_LOW"}),
        clip_part(inlineData={"mimeType": "video/webm", "data": MP4}),
        {k: v for k, v in clip_part().items() if k != "videoMetadata"},
    ],
)
def test_a_clip_whose_length_or_resolution_is_not_fixed_is_refused(part: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        gemini_cost.payload_bound(payload([part]), FLASH, on=TODAY)


def test_request_bytes_measure_the_compact_json_body() -> None:
    body = payload([{"text": "가"}])
    assert gemini_cost.request_bytes(body) == len(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert gemini_cost.INLINE_REQUEST_LIMIT_BYTES == 20_000_000


# ── 영수증 ───────────────────────────────────────────────────────────────


class Meta:
    def __init__(self, model: str) -> None:
        self.model = model


def reply(**usage: object) -> dict[str, object]:
    metadata = {"promptTokenCount": 9000, "candidatesTokenCount": 200, "thoughtsTokenCount": 800}
    metadata.update(usage)
    return {"usageMetadata": metadata, "modelVersion": "gemini-3.8-flash", "responseId": "r1"}


def test_the_receipt_prices_prompt_and_output_plus_thoughts() -> None:
    usage = gemini_cost.measure_gemini_usage(Meta("gemini-3.8-flash"), reply(), on=TODAY)
    assert (usage.input_tokens, usage.output_tokens) == (9000, 1000)
    expected = (Decimal(9000) * Decimal("0.75") + Decimal(1000) * Decimal("3.75")) / Decimal(10**6)
    assert Decimal(usage.cost_usd or "nan") == expected
    assert usage.pricing_revision == FLASH.pricing_revision
    assert usage.provider_request_id == "r1" and usage.unpriced_reason is None


def test_a_receipt_without_thoughts_counts_candidates_only() -> None:
    body = reply()
    body["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 5}
    usage = gemini_cost.measure_gemini_usage(Meta("gemini-3.8-flash"), body, on=TODAY)
    assert usage.output_tokens == 5


@pytest.mark.parametrize(
    ("model", "body", "reason"),
    [
        ("gemini-3.6-flash", reply(), "unknown_model"),
        ("gemini-3.8-flash", reply(promptTokenCount=-1), "missing_or_invalid_usage"),
        ("gemini-3.8-flash", {"modelVersion": "gemini-3.8-flash"}, "missing_or_invalid_usage"),
        ("gemini-3.8-flash", {**reply(), "modelVersion": "gemini-4-argon"}, "model_mismatch"),
        ("gemini-3.8-flash", {**reply(), "modelVersion": None}, "model_mismatch"),
    ],
)
def test_an_unpriceable_receipt_keeps_its_reason_never_zero(
    model: str, body: dict[str, object], reason: str
) -> None:
    usage = gemini_cost.measure_gemini_usage(Meta(model), body, on=TODAY)
    assert usage.cost_usd is None and usage.unpriced_reason == reason


def test_a_receipt_after_the_announced_raise_is_unpriced() -> None:
    usage = gemini_cost.measure_gemini_usage(Meta("gemini-3.8-flash"), reply(), on=date(2027, 1, 1))
    assert usage.cost_usd is None and usage.unpriced_reason == "price_not_current"
    assert usage.input_tokens == 9000  # 토큰은 남긴다


def test_a_dated_model_version_still_prices() -> None:
    body = {**reply(), "modelVersion": "gemini-3.8-flash-001"}
    usage = gemini_cost.measure_gemini_usage(Meta("gemini-3.8-flash"), body, on=TODAY)
    assert usage.cost_usd is not None


def test_hydrogen_is_registered_with_the_checked_price_of_2026_11_19() -> None:
    """D168 — 입력 $1.50 · 출력(생각 포함) $7.50, 같은 엔드포인트·해상도·생각 깊이, 기한은 재확인
    기한."""
    hydrogen = GEMINI_ANALYSIS_PROFILES["gemini-4-hydrogen"]
    assert (hydrogen.input_per_mtok, hydrogen.output_per_mtok) == (Decimal("1.50"), Decimal("7.50"))
    assert hydrogen.pricing_checked_at == date(2026, 11, 19)
    assert hydrogen.price_valid_through == date(2027, 2, 19)
    assert hydrogen.image_tokens == FLASH.image_tokens and hydrogen.thinking_level == "MEDIUM"
    assert hydrogen.api_base_url == FLASH.api_base_url and hydrogen.api_version == "v1beta"
    assert gemini_profile_problems(hydrogen) == [] and gemini_profile(hydrogen.model) is hydrogen
