"""시간 엔진 표·역할·능력표·사전 검사(W-31c, D92) — whisper는 바이트 그대로, Gemini는 그 짝으로만.

- 엔진 표는 둘이다: whisper-1(verbose_json 세그먼트) · Gemini 3.5 Transcribe(word_info 단어).
- `timing_engine_view`는 whisper에 **같은 객체**를 돌려준다 — 저장된 설정·열쇠·지문이 바뀌지 않는다.
  Gemini 보기는 모델·제공자·응답 형식·입자를 함께 바꾼다(그 짝 말고는 동결 검사가 거부한다).
- 시간 역할은 등록된 Gemini transcribe만 받고, 다른 Gemini(의미 분석)와 글자 역할의 Gemini는 그대로
  거부한다(D86).
- 사전 검사는 엔진마다다: whisper는 2027-02-26 차단 그대로, Gemini는 그 날짜에 막히지 않고 자기
  단가 재확인 기한에 닫힌다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType

import pytest

from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw import config as raw_config
from vlogsrt.project.raw.config import (
    GEMINI_TRANSCRIBE_PROFILE,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
    TIMING_ENGINES,
    RawModelRoles,
    RawWorkflowConfig,
    caption_problems,
    engine_problems,
    role_problems,
    timing_engine_view,
    validate_raw_workflow_config,
)

GEMINI = TIMING_ENGINES[TIMING_ENGINE_GEMINI]
WHISPER = TIMING_ENGINES[TIMING_ENGINE_WHISPER]
BEFORE_SHUTDOWN = date(2026, 11, 1)
AFTER_SHUTDOWN = date(2027, 3, 1)


def test_the_engine_table_names_both_engines() -> None:
    assert WHISPER.model == RawModelRoles().timing_model
    assert GEMINI.model == GEMINI_TRANSCRIBE_PROFILE.model
    assert (WHISPER.rescue, GEMINI.rescue) == (True, False)
    assert (WHISPER.text_pass, GEMINI.text_pass) == ("gate", "candidate")


def test_the_whisper_view_is_the_same_object() -> None:
    config = RawWorkflowConfig()
    assert timing_engine_view(config, WHISPER) is config


def test_the_gemini_view_changes_model_provider_format_and_granularity_together() -> None:
    config = RawWorkflowConfig()
    view = timing_engine_view(config, GEMINI)
    assert view.models.timing_model == GEMINI.model
    assert view.caption.timing_provider == GEMINI.provider == "gemini_transcribe_word_info"
    assert view.caption.timing_response_format == "word_info"
    assert view.caption.timestamp_granularities == ("word",)
    assert (view.models.text_model, view.models.luna_model) == (
        config.models.text_model,
        config.models.luna_model,
    )
    validate_raw_workflow_config(view)


def test_a_mismatched_engine_pair_is_refused() -> None:
    config = RawWorkflowConfig()
    view = timing_engine_view(config, GEMINI)
    whisper_with_gemini_contract = replace(view, models=config.models)
    gemini_with_whisper_contract = replace(config, models=view.models)
    assert engine_problems(whisper_with_gemini_contract)
    assert engine_problems(gemini_with_whisper_contract)
    assert engine_problems(config) == [] and engine_problems(view) == []


def test_word_only_granularity_is_only_for_the_gemini_provider() -> None:
    contract = RawWorkflowConfig().caption
    assert caption_problems(replace(contract, timestamp_granularities=("word",)))
    view = timing_engine_view(RawWorkflowConfig(), GEMINI).caption
    assert caption_problems(view) == []
    assert caption_problems(replace(view, timestamp_granularities=("segment",)))


def test_the_timing_role_takes_only_a_registered_gemini_transcribe() -> None:
    roles = RawModelRoles()
    assert role_problems(replace(roles, timing_model=GEMINI.model)) == []
    assert role_problems(replace(roles, timing_model=roles.luna_model))  # Gemini 의미 분석 모델
    assert role_problems(replace(roles, text_model=GEMINI.model))  # 글자 역할은 여전히 금지


def test_the_gemini_combo_is_confirmed_and_hints_are_refused() -> None:
    capabilities.confirm_request_combo(
        role=capabilities.ROLE_TIMING,
        model=GEMINI.model,
        response_format="word_info",
        features=(
            capabilities.FEATURE_VERBATIM,
            capabilities.FEATURE_WORD_TIMES,
            capabilities.FEATURE_LANGUAGE_CODES,
        ),
    )
    for feature in (
        capabilities.FEATURE_PROMPT,
        capabilities.FEATURE_CUSTOM_VOCABULARY,
        capabilities.FEATURE_SMART_MODE,
        capabilities.FEATURE_SEGMENT_TIMES,
    ):
        with pytest.raises(capabilities.UnverifiedModelCapability):
            capabilities.confirm_request_combo(
                role=capabilities.ROLE_TIMING,
                model=GEMINI.model,
                response_format="word_info",
                features=(capabilities.FEATURE_WORD_TIMES, feature),
            )
    with pytest.raises(capabilities.UnverifiedModelCapability):
        capabilities.confirm_request_combo(
            role=capabilities.ROLE_TIMING, model=GEMINI.model, response_format="verbose_json"
        )


def test_the_whisper_combo_is_unchanged() -> None:
    snapshot = capabilities.capability_snapshot_for_role(capabilities.ROLE_TIMING)
    assert snapshot.model_id == WHISPER.model
    capabilities.confirm_request_combo(
        role=capabilities.ROLE_TIMING,
        model=WHISPER.model,
        response_format="verbose_json",
        features=(capabilities.FEATURE_SEGMENT_TIMES,),
    )


def test_whisper_is_blocked_at_its_shutdown_but_gemini_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whisper = RawWorkflowConfig()
    gemini = timing_engine_view(whisper, GEMINI)
    roles = (capabilities.ROLE_TIMING,)
    capabilities.preflight_online_transcription(whisper, BEFORE_SHUTDOWN, roles)
    capabilities.preflight_online_transcription(gemini, BEFORE_SHUTDOWN, roles)
    with pytest.raises(capabilities.TimingProviderUnavailable):
        capabilities.preflight_online_transcription(whisper, AFTER_SHUTDOWN, roles)
    longer = replace(GEMINI_TRANSCRIBE_PROFILE, price_valid_through=date(2027, 6, 30))
    monkeypatch.setattr(
        raw_config, "GEMINI_TRANSCRIBE_PROFILES", MappingProxyType({longer.model: longer})
    )
    capabilities.preflight_online_transcription(gemini, AFTER_SHUTDOWN, roles)


def test_gemini_closes_after_its_price_recheck_date() -> None:
    gemini = timing_engine_view(RawWorkflowConfig(), GEMINI)
    after = date.fromordinal(GEMINI_TRANSCRIBE_PROFILE.price_valid_through.toordinal() + 1)
    with pytest.raises(capabilities.TimingProviderUnavailable, match="재확인"):
        capabilities.preflight_online_transcription(gemini, after, (capabilities.ROLE_TIMING,))


def test_the_timing_price_follows_the_engine() -> None:
    assert capabilities.timing_price_per_minute(WHISPER.model, BEFORE_SHUTDOWN) == Decimal("0.006")
    assert capabilities.timing_price_per_minute(GEMINI.model, BEFORE_SHUTDOWN) == Decimal("0.005")
    late = date(2027, 6, 1)
    assert capabilities.timing_price_per_minute(GEMINI.model, late) is None
    assert capabilities.price_per_minute(capabilities.ROLE_TIMING) == Decimal("0.006")
