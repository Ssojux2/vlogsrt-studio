"""v2 원본 워크플로 설정(A0) — 동결·검증·수명주기 계약.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11·§15.1의 설정값을 못 박는다.
API·네트워크·ffmpeg를 전혀 부르지 않는다 — 데이터 계약만 시험한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal

import pytest

from vlogsrt.project.raw.config import (
    CAPTION_PROFILE_LATEST_OPENAI_SRT,
    CAPTION_SCHEMA_VERSION,
    TIMING_PROVIDER_WHISPER_VERBOSE_JSON,
    WORKFLOW_SCHEMA_VERSION,
    RawModelRoles,
    RawWorkflowConfig,
    raw_workflow_config_from_json,
    raw_workflow_config_to_json,
    validate_raw_workflow_config,
)


def test_defaults_match_the_2026_10_02_plan() -> None:
    """역할·등록표·요금·스키마는 계획 §11·§15.1의 2026-10-02 확인값 그대로다."""
    config = RawWorkflowConfig()
    assert config.models.text_model == "gpt-transcribe"
    assert config.models.timing_model == "whisper-1"
    # W-18(D58): 새 작업공간의 의미 분석 기본은 Claude Sonnet 5.5다 — 나머지는 2026-10-02 그대로
    assert config.models.luna_model == "claude-sonnet-5-5"
    assert config.registry.revision == "openai-caption-2026-10-02-v1"
    assert config.registry.docs_checked_at == date(2026, 10, 2)
    assert config.registry.account_access_status == "unverified"
    assert config.registry.integration_test_status == "not_run"
    assert config.registry.timing_shutdown_date == date(2027, 2, 26)
    assert config.pricing.text_per_minute == Decimal("0.0045")
    assert config.pricing.timing_per_minute == Decimal("0.006")
    assert config.workflow_schema_version == WORKFLOW_SCHEMA_VERSION == "raw-workspace/v1"
    assert config.caption.schema_version == CAPTION_SCHEMA_VERSION == "raw-caption/v2"
    assert config.caption.profile == CAPTION_PROFILE_LATEST_OPENAI_SRT
    assert config.caption.timing_provider == TIMING_PROVIDER_WHISPER_VERBOSE_JSON


def test_defaults_validate_and_are_offline_by_default() -> None:
    """기본 설정은 검사를 통과하고, 네트워크는 오프라인·명시적 승인 없음이 기본이다."""
    config = RawWorkflowConfig()
    validate_raw_workflow_config(config)
    assert config.network_mode == "offline"
    assert config.require_explicit_authorization is True
    assert config.caption.allow_silent_model_fallback is False
    assert config.caption.allow_guessed_timestamps is False


def test_the_config_is_frozen_at_every_level() -> None:
    """설정은 얕게만 아니라 담긴 계약 객체까지 전부 frozen이다."""
    config = RawWorkflowConfig()
    with pytest.raises(FrozenInstanceError):
        config.network_mode = "online"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        config.models.text_model = "older-model"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        config.registry.timing_shutdown_date = date(2030, 1, 1)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        config.pricing.text_per_minute = Decimal("0")  # type: ignore[misc]


def test_validation_reports_every_problem_at_once() -> None:
    """위반을 하나만 고쳐주고 나머지를 삼키지 않는다 — 전부 나열한다."""
    broken = RawWorkflowConfig(
        workflow_schema_version="raw-workspace/v0",
        caption=replace(
            RawWorkflowConfig().caption,
            schema_version="raw-caption/v1",
            allow_silent_model_fallback=True,
        ),
        require_explicit_authorization=False,
    )
    with pytest.raises(ValueError) as raised:
        validate_raw_workflow_config(broken)
    message = str(raised.value)
    assert "raw-workspace/v1" in message
    assert "raw-caption/v2" in message
    assert "조용한 구형 모델 fallback" in message
    assert "명시적 승인" in message


@pytest.mark.parametrize(
    ("caption_overrides", "expected_fragment"),
    [
        ({"allow_silent_model_fallback": True}, "조용한 구형 모델 fallback"),
        ({"allow_guessed_timestamps": True}, "근거 없는 시각 생성"),
        ({"profile": "legacy_whisper_only"}, "latest_openai_srt"),
        ({"timing_provider": "guessed_alignment"}, "whisper_verbose_json"),
        # 요청 형식 — 문구 모델에 srt·vtt·timestamp_granularities를 못 붙인다(§11.4)
        ({"text_response_format": "srt"}, "문구 응답 형식은 json"),
        ({"text_response_format": "verbose_json"}, "문구 응답 형식은 json"),
        ({"timing_response_format": "json"}, "시간 응답 형식은 verbose_json"),
        # 단어만은 골격이 없다 — 세그먼트와 함께일 때만 받는다(D73, W-25c)
        ({"timestamp_granularities": ("word",)}, "단어만으로는 골격이 없다"),
        ({"timestamp_granularities": ("word", "segment")}, "단어만으로는 골격이 없다"),
    ],
)
def test_caption_contract_violations_are_rejected(
    caption_overrides: dict[str, object], expected_fragment: str
) -> None:
    """문구/시간 분리와 무추론 원칙은 설정 단계에서 이미 막힌다(§0·§11.0·§11.4)."""
    broken = RawWorkflowConfig(caption=replace(RawWorkflowConfig().caption, **caption_overrides))
    with pytest.raises(ValueError, match=expected_fragment):
        validate_raw_workflow_config(broken)


@pytest.mark.parametrize(
    ("languages", "keywords", "expected_fragment"),
    [
        (("ko\n",), (), "줄바꿈·제어문자"),
        (("ko", " "), (), "빈 문자열"),
        ((), ("",), "빈 문자열"),
        (("ko",), ("북촌한옥마을\n",), "줄바꿈·제어문자"),
        (("ko",), ("카페\t오늘",), "줄바꿈·제어문자"),
        (("ko",), ("의외\x00의힌트",), "줄바꿈·제어문자"),
    ],
)
def test_hint_violations_are_rejected(
    languages: tuple[str, ...], keywords: tuple[str, ...], expected_fragment: str
) -> None:
    """힌트는 전송 전에 검증한다 — 줄바꿈·제어문자·빈 칸은 못 넣는다(§11.4)."""
    broken = RawWorkflowConfig(
        caption=replace(RawWorkflowConfig().caption, languages=languages, keywords=keywords)
    )
    with pytest.raises(ValueError, match=expected_fragment):
        validate_raw_workflow_config(broken)


def test_clean_hints_still_validate() -> None:
    """정상 힌트는 통과한다 — 고유명사 힌트 자체가 금지가 아니다."""
    config = RawWorkflowConfig(
        caption=replace(
            RawWorkflowConfig().caption, languages=("ko", "en"), keywords=("북촌한옥마을",)
        )
    )
    validate_raw_workflow_config(config)


def test_empty_hints_mean_no_hint_not_no_speech() -> None:
    """빈 languages·keywords는 힌트 없음이다 — 무발화 추론이나 거부가 아니다(§11.4).

    기본값은 ko를 유지한다. 어댑터는 빈 배열을 필드째 빼고 보낸다.
    """
    config = RawWorkflowConfig(
        caption=replace(RawWorkflowConfig().caption, languages=(), keywords=())
    )
    validate_raw_workflow_config(config)
    assert RawWorkflowConfig().caption.languages == ("ko",)


@pytest.mark.parametrize(
    ("role", "value"),
    [
        ("text_model", ""),
        ("timing_model", ""),
        ("luna_model", ""),
        ("text_model", "   "),
        ("timing_model", "\t"),
        ("luna_model", " \n "),
        ("timing_model", "gpt-transcribe"),
    ],
)
def test_role_violations_are_rejected(role: str, value: str) -> None:
    """빈·공백 역할과 문구=시간 조합은 가짜 시각의 씨앗이라 거부한다(§11.0)."""
    broken = RawWorkflowConfig(models=replace(RawWorkflowConfig().models, **{role: value}))
    with pytest.raises(ValueError, match="모델"):
        validate_raw_workflow_config(broken)


@pytest.mark.parametrize(
    ("field", "value", "expected_fragment"),
    [
        ("revision", "", "리비전이 비어 있다"),
        ("timing_shutdown_date", date(2026, 1, 1), "종료일이 공보다 빠르다"),
        ("docs_checked_at", date(2026, 8, 25), "문서 확인일이 공보다 빠르다"),
    ],
)
def test_registry_violations_are_rejected(
    field: str, value: object, expected_fragment: str
) -> None:
    broken = RawWorkflowConfig(registry=replace(RawWorkflowConfig().registry, **{field: value}))
    with pytest.raises(ValueError, match=expected_fragment):
        validate_raw_workflow_config(broken)


@pytest.mark.parametrize(
    ("field", "value", "expected_fragment"),
    [
        ("timing_shutdown_date", date(2028, 2, 26), "새 리비전"),
        ("timing_shutdown_date", date(2027, 2, 25), "새 리비전"),
    ],
)
def test_the_confirmed_registry_revision_cannot_be_weakened(
    field: str, value: object, expected_fragment: str
) -> None:
    """확인된 리비전 표면을 달고 종료일을 바꾸지 못한다(§11.10).

    약화는 문서 재확인 뒤 **새 리비전 문자열**로만 다룬다(§11.9).
    """
    broken = RawWorkflowConfig(registry=replace(RawWorkflowConfig().registry, **{field: value}))
    with pytest.raises(ValueError, match=expected_fragment):
        validate_raw_workflow_config(broken)


@pytest.mark.parametrize("revision", ["openai-caption-2026-10-02-v1", "brand-new-revision"])
def test_shutdown_enforcement_cannot_be_disabled_by_any_registry(revision: str) -> None:
    """종료 차단은 모든 수락되는 등록표에서 켜져 있다 — 리비전 문자열 바꾸기 우회 불가.

    fail-closed(코디네이터 재결): 임의 리비전으로 `blocks_online_timing`을 끄는
    설정은 검증을 통과하지 못한다.
    """
    registry = replace(
        RawWorkflowConfig().registry,
        revision=revision,
        block_online_at_shutdown_date=False,
    )
    with pytest.raises(ValueError, match="종료 차단은 모든 등록표에서"):
        validate_raw_workflow_config(RawWorkflowConfig(registry=registry))


@pytest.mark.parametrize(
    ("field", "value", "expected_fragment"),
    [
        ("currency", "", "통화가 비어 있다"),
        ("text_per_minute", Decimal("0"), "문구 요금이 0 이하다"),
        ("timing_per_minute", Decimal("-0.001"), "시간 요금이 0 이하다"),
        ("text_per_minute", Decimal("NaN"), "유한하지 않다"),
        ("timing_per_minute", Decimal("Infinity"), "유한하지 않다"),
    ],
)
def test_pricing_violations_are_rejected(field: str, value: object, expected_fragment: str) -> None:
    """가격 미확인을 0원으로 두지 않고 NaN·Infinity도 가격으로 인정하지 않는다(§11.8)."""
    broken = RawWorkflowConfig(pricing=replace(RawWorkflowConfig().pricing, **{field: value}))
    with pytest.raises(ValueError, match=expected_fragment):
        validate_raw_workflow_config(broken)


@pytest.mark.parametrize(
    ("field", "value", "expected_fragment"),
    [
        ("network_mode", "always_on", "알 수 없는 네트워크 모드"),
        ("require_explicit_authorization", False, "명시적 승인"),
    ],
)
def test_network_violations_are_rejected(field: str, value: object, expected_fragment: str) -> None:
    """온라인 전환은 명시적 승인을 우회할 수 없다(§14.3 — VLOGSRT_OFFLINE 존중)."""
    broken = replace(RawWorkflowConfig(), **{field: value})
    with pytest.raises(ValueError, match=expected_fragment):
        validate_raw_workflow_config(broken)


def test_online_timing_is_blocked_from_the_shutdown_date() -> None:
    """§11.10 — 종료 예정일(2027-02-26)부터 신규 온라인 whisper 작업을 차단한다."""
    registry = RawWorkflowConfig().registry
    assert registry.blocks_online_timing(date(2027, 2, 25)) is False
    assert registry.blocks_online_timing(date(2027, 2, 26)) is True
    assert registry.blocks_online_timing(date(2028, 1, 1)) is True


def test_a_rechecked_registry_may_move_the_shutdown_date_but_never_drop_the_block() -> None:
    """새 리비전은 종료일을 옮길 수 있다 — 단 차단은 여전히 켜져 있다(§11.9·§11.10)."""
    registry = replace(
        RawWorkflowConfig().registry,
        revision="openai-caption-2027-recheck-v1",
        docs_checked_at=date(2027, 3, 1),
        timing_shutdown_date=date(2028, 2, 26),
    )
    validate_raw_workflow_config(RawWorkflowConfig(registry=registry))
    assert registry.blocks_online_timing(date(2028, 2, 25)) is False
    assert registry.blocks_online_timing(date(2028, 2, 26)) is True


def test_both_paths_per_minute_is_the_plans_arithmetic_example() -> None:
    """$0.0045 + $0.006 = 분당 $0.0105(§11.8). 상한이 아니라 산술 예시다."""
    assert RawWorkflowConfig().pricing.both_paths_per_minute == Decimal("0.0105")


def test_the_config_survives_a_json_roundtrip_unchanged() -> None:
    """직렬화→재구성→검증 왕복에 원본과 같은 설정이 돌아온다(§9.1 스냅샷 계약).

    date·Decimal은 JSON 기본형이 아니므로 ISO·십진 문자열로 다니고, 재구성 뒤
    검증이 다시 지나간다 — 스냅샷을 읽어 계약 밖 설정이 되살아나지 않는다.
    """
    original = RawWorkflowConfig()
    payload = raw_workflow_config_to_json(original)
    assert "NaN" not in payload and "Infinity" not in payload
    rebuilt = raw_workflow_config_from_json(payload)
    assert rebuilt == original
    validate_raw_workflow_config(rebuilt)
    assert raw_workflow_config_to_json(rebuilt) == payload


def test_a_hint_carrying_config_survives_the_json_roundtrip() -> None:
    """힌트를 실은 설정도 왕복한다 — A1의 고유명사 힌트 저장 경로다."""
    original = RawWorkflowConfig(
        caption=replace(
            RawWorkflowConfig().caption, languages=("ko", "en"), keywords=("북촌한옥마을",)
        )
    )
    assert raw_workflow_config_from_json(raw_workflow_config_to_json(original)) == original


@pytest.mark.parametrize(
    "payload",
    [
        "NaN",
        '{"models": NaN}',
        "Infinity",
        "not json at all",
        '"just a string"',
        "[]",
        "{}",
        '{"workflow_schema_version": 1}',
    ],
)
def test_malformed_json_is_rejected_as_valueerror(payload: str) -> None:
    """모양이 어긋난 페이로드는 ValueError로 끝난다 — 삼키거나 기본값으로 메우지 않는다.

    JSON의 NaN·Infinity 상수는 파싱 단계에서, 잘못된 필드 형태는 재구성·검증
    단계에서 각각 걸린다.
    """
    with pytest.raises(ValueError):
        raw_workflow_config_from_json(payload)


def test_nan_pricing_roundtrips_into_rejection() -> None:
    """NaN 요금은 십진 문자열로 직렬화되지만, 왕복 검증이 다시 거부한다.

    유한성 검사가 직렬화 뒤에도 살아있다 — 스냅샷 경로가 검증의 우회로가 아니다.
    """
    broken = RawWorkflowConfig(
        pricing=replace(RawWorkflowConfig().pricing, text_per_minute=Decimal("NaN"))
    )
    payload = raw_workflow_config_to_json(broken)
    assert '"NaN"' in payload
    with pytest.raises(ValueError, match="유한하지 않다"):
        raw_workflow_config_from_json(payload)


def test_a_json_payload_weaker_than_the_contract_is_rejected() -> None:
    """왕복 검증은 우회로가 아니다 — JSON에서 꺼낸 약화 설정도 그대로 거부된다."""
    original = RawWorkflowConfig()
    payload = raw_workflow_config_to_json(original).replace('"offline"', '"always_on"')
    with pytest.raises(ValueError, match="알 수 없는 네트워크 모드"):
        raw_workflow_config_from_json(payload)


@pytest.mark.parametrize(
    ("mutate", "expected_fragment"),
    [
        (lambda payload: payload[:-1] + ', "unexpected_key": 1}', "알 수 없는 필드"),
        (
            lambda payload: payload.replace('"keywords": []', '"keywords": [], "extra": 0'),
            "알 수 없는 필드",
        ),
        (
            lambda payload: payload.replace(
                '"block_online_at_shutdown_date": true', '"block_online_at_shutdown_date": "true"'
            ),
            "block_online_at_shutdown_date는 bool",
        ),
        (
            lambda payload: payload.replace(
                '"text_per_minute": "0.0045"', '"text_per_minute": 0.0045'
            ),
            "text_per_minute는 str",
        ),
        (
            lambda payload: payload.replace(
                '"docs_checked_at": "2026-10-02"', '"docs_checked_at": "October"'
            ),
            "계약과 다르다",
        ),
    ],
)
def test_the_json_roundtrip_is_fail_closed(
    mutate: Callable[[str], str], expected_fragment: str
) -> None:
    """모르는 필드와 잘못된 형태는 조용히 버리거나 굽히지 않고 거부된다(코디네이터 재결).

    부동소수점 요금·문자열 bool 같은 손실 코어션의 길도 막는다 — 십진 문자열·
    참 bool만 받는다.
    """
    payload = mutate(raw_workflow_config_to_json(RawWorkflowConfig()))
    with pytest.raises(ValueError, match=expected_fragment):
        raw_workflow_config_from_json(payload)


# A0/A1에서 수락된 저장 설정의 글자 그대로의 스냅샷이다. 데이터 계약에 칸을
# 더하는 순간 이 문자열은 더 이상 열리지 않는다 — 그때는 마이그레이션을 먼저
# 정의하고 이 고정 값을 갱신해야 한다(A7-1은 이 파일의 칸을 건드리지 않았다).
ACCEPTED_SETTINGS_JSON = """{
  "caption": {
    "allow_guessed_timestamps": false,
    "allow_silent_model_fallback": false,
    "keywords": [],
    "languages": [
      "ko"
    ],
    "profile": "latest_openai_srt",
    "schema_version": "raw-caption/v2",
    "text_response_format": "json",
    "timestamp_granularities": [
      "segment"
    ],
    "timing_provider": "whisper_verbose_json",
    "timing_response_format": "verbose_json"
  },
  "models": {
    "luna_model": "gpt-6-luna",
    "text_model": "gpt-transcribe",
    "timing_model": "whisper-1"
  },
  "network_mode": "offline",
  "pricing": {
    "checked_at": "2026-10-02",
    "currency": "USD",
    "text_per_minute": "0.0045",
    "timing_per_minute": "0.006"
  },
  "registry": {
    "account_access_status": "unverified",
    "block_online_at_shutdown_date": true,
    "docs_checked_at": "2026-10-02",
    "integration_test_status": "not_run",
    "revision": "openai-caption-2026-10-02-v1",
    "source_urls": [
      "https://developers.openai.com/api/docs/models/gpt-transcribe",
      "https://developers.openai.com/api/docs/guides/transcription",
      "https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create",
      "https://developers.openai.com/api/docs/deprecations",
      "https://developers.openai.com/api/docs/models/whisper-1"
    ],
    "timing_deprecation_notice_date": "2026-08-26",
    "timing_shutdown_date": "2027-02-26"
  },
  "require_explicit_authorization": true,
  "workflow_schema_version": "raw-workspace/v1"
}"""


def test_accepted_saved_settings_json_still_round_trips() -> None:
    """수락된 저장 설정은 A7-1 이후에도 그대로 열린다 — JSON 호환이 깨지면 여기서 먼저 울린다.

    W-18(D58) 뒤로 이 글자는 "지금의 기본"이 아니라 "이미 있는 작업공간이 저장한 W-18 이전의
    기본"이다. 그래서 새 기본(Sonnet 5.5)과 같지 않고, 저장된 gpt-6-luna 그대로 열려야 한다.
    """
    config = raw_workflow_config_from_json(ACCEPTED_SETTINGS_JSON)
    pre_w18 = replace(RawWorkflowConfig(), models=replace(RawModelRoles(), luna_model="gpt-6-luna"))
    assert config == pre_w18
    assert config.models.luna_model == "gpt-6-luna"
    assert raw_workflow_config_to_json(config) == ACCEPTED_SETTINGS_JSON


# ─────────────────────────────────────────────────────────────
#  W-14 / D54 — Claude Opus 5.5를 의미 분석 역할로 고를 수 있다
# ─────────────────────────────────────────────────────────────


def _claude_config() -> RawWorkflowConfig:
    return RawWorkflowConfig(
        models=replace(RawWorkflowConfig().models, luna_model="claude-opus-5-5")
    )


def test_the_claude_profile_carries_the_documented_snapshot() -> None:
    """2026-10-09 공식 가격표·API 버전·엔드포인트 그대로다 — 단가는 0으로 두지 않는다."""
    from vlogsrt.project.raw.config import CLAUDE_ANALYSIS_PROFILES, claude_profile

    profile = claude_profile("claude-opus-5-5")
    assert CLAUDE_ANALYSIS_PROFILES["claude-opus-5-5"] is profile
    assert profile.label == "Claude Opus 5.5"
    assert profile.api_base_url == "https://api.anthropic.com"
    assert profile.api_version == "2023-06-01"
    assert profile.timeout_seconds == 300.0
    assert (profile.effort, profile.max_output_tokens) == ("medium", 8192)
    # 표준 단가 지역을 요청에 못 박는다 — US 전용(1.1배)은 고르지 않는다. 등급은 싣지 않는다
    assert profile.inference_geo == "global"
    assert not hasattr(profile, "service_tier")
    assert profile.pricing_checked_at == date(2026, 10, 9)
    assert profile.pricing_revision == "anthropic-claude-opus-5-5-2026-10-09"
    assert (profile.input_per_mtok, profile.output_per_mtok) == (Decimal("4"), Decimal("20"))
    assert profile.cache_write_5m_per_mtok == Decimal("5")
    assert profile.cache_write_1h_per_mtok == Decimal("8")
    assert profile.cache_read_per_mtok == Decimal("0.20")
    assert (profile.batch_input_per_mtok, profile.batch_output_per_mtok) == (
        Decimal("2"),
        Decimal("10"),
    )
    assert (profile.image_patch_pixels, profile.image_max_visual_tokens) == (28, 4784)
    assert (profile.quote_text_tokens_per_window, profile.quote_overhead_tokens) == (12000, 2000)
    assert profile.account_access_status == "unverified"
    assert profile.integration_test_status == "not_run"
    assert any("pricing" in url for url in profile.source_urls)
    assert all(url.startswith("https://platform.claude.com/") for url in profile.source_urls)


def test_the_registered_profile_passes_its_own_check() -> None:
    from vlogsrt.project.raw.config import claude_profile, claude_profile_problems

    assert claude_profile_problems(claude_profile("claude-opus-5-5")) == []


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("input_per_mtok", Decimal("0"), "input_per_mtok"),
        ("output_per_mtok", Decimal("-1"), "output_per_mtok"),
        ("cache_write_5m_per_mtok", Decimal("NaN"), "cache_write_5m_per_mtok"),
        ("cache_write_1h_per_mtok", Decimal("Infinity"), "cache_write_1h_per_mtok"),
        ("cache_read_per_mtok", Decimal("0"), "cache_read_per_mtok"),
        ("batch_input_per_mtok", Decimal("0"), "batch_input_per_mtok"),
        ("effort", "turbo", "effort"),
        ("max_output_tokens", 0, "max_output_tokens"),
        ("max_output_tokens", 128001, "max_output_tokens"),
        ("api_base_url", "http://api.anthropic.com", "https"),
        ("api_version", " ", "api_version"),
        ("timeout_seconds", 0.0, "timeout"),
        ("inference_geo", "us", "inference_geo"),
    ],
)
def test_profile_problems_catch_unpriced_or_malformed_values(
    field: str, value: object, fragment: str
) -> None:
    from vlogsrt.project.raw.config import claude_profile, claude_profile_problems

    broken = replace(claude_profile("claude-opus-5-5"), **{field: value})
    problems = claude_profile_problems(broken)
    assert problems and any(fragment in problem for problem in problems), problems


def test_provider_resolution_and_choices() -> None:
    from vlogsrt.project.raw.config import (
        ANALYSIS_MODEL_CHOICES,
        ANALYSIS_PROVIDER_ANTHROPIC,
        ANALYSIS_PROVIDER_OPENAI,
        analysis_model_label,
        analysis_provider,
        claude_profile,
    )

    # W-18(D58): 기본(Sonnet 5.5)이 맨 앞, Opus 5.5와 gpt-6-luna도 그대로 고를 수 있다
    assert ANALYSIS_MODEL_CHOICES == ("claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-luna")
    assert analysis_provider("gpt-6-luna") == ANALYSIS_PROVIDER_OPENAI == "openai"
    assert analysis_provider("claude-opus-5-5") == ANALYSIS_PROVIDER_ANTHROPIC == "anthropic"
    assert analysis_model_label("claude-opus-5-5") == "Claude Opus 5.5"
    assert analysis_model_label("gpt-6-luna") == "gpt-6-luna"
    with pytest.raises(ValueError, match="등록되지 않은"):
        claude_profile("claude-unknown-9")


def test_a_registered_claude_analysis_model_validates_and_roundtrips() -> None:
    config = _claude_config()
    validate_raw_workflow_config(config)
    assert raw_workflow_config_from_json(raw_workflow_config_to_json(config)) == config


@pytest.mark.parametrize(
    ("role", "value", "fragment"),
    [
        ("luna_model", "claude-unknown-9", "등록되지 않은 Claude"),
        ("text_model", "claude-opus-5-5", "오디오"),
        ("timing_model", "claude-opus-5-5", "오디오"),
    ],
)
def test_claude_outside_its_registered_analysis_role_is_rejected(
    role: str, value: str, fragment: str
) -> None:
    broken = RawWorkflowConfig(models=replace(RawWorkflowConfig().models, **{role: value}))
    with pytest.raises(ValueError, match=fragment):
        validate_raw_workflow_config(broken)


def test_the_default_workflow_json_is_byte_identical_with_no_new_keys() -> None:
    """W-14·W-18은 RawWorkflowConfig에 칸을 더하지 않는다 — 저장된 작업공간이 그대로 열린다.

    W-18(D58)이 바꾼 것은 새 작업공간의 의미 분석 모델 값 하나뿐이다 — 칸·순서·나머지 값은
    수락된 저장 설정과 바이트까지 같다. 단가·엔드포인트는 설정 밖 프로파일에 산다.
    """
    payload = raw_workflow_config_to_json(RawWorkflowConfig())
    luna_line = '"luna_model": "gpt-6-luna"'
    assert ACCEPTED_SETTINGS_JSON.count(luna_line) == 1
    assert payload == ACCEPTED_SETTINGS_JSON.replace(luna_line, '"luna_model": "claude-sonnet-5-5"')
    assert "anthropic" not in payload.lower()


def test_transcription_identity_view_pins_the_analysis_model() -> None:
    """전사 전용 열쇠는 의미 분석 모델과 무관하다 — 모델을 바꿔도 whisper를 다시 사지 않는다."""
    from vlogsrt.project.raw.config import (
        TRANSCRIPTION_IDENTITY_LUNA_MODEL,
        transcription_identity_view,
    )

    # W-18(D58): 기본이 Sonnet 5.5가 돼도 고정값은 W-14 이전의 기본(저장된 gpt-6-luna) 그대로다
    pinned = raw_workflow_config_from_json(ACCEPTED_SETTINGS_JSON)
    assert TRANSCRIPTION_IDENTITY_LUNA_MODEL == "gpt-6-luna"
    assert transcription_identity_view(pinned) is pinned
    assert transcription_identity_view(RawWorkflowConfig()) == pinned
    assert transcription_identity_view(_claude_config()) == pinned
    online = replace(_claude_config(), network_mode="online")
    assert transcription_identity_view(online) == replace(pinned, network_mode="online")
