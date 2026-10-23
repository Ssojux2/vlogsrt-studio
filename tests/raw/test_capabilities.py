"""모델 기능 등록표와 수명주기 검사(A7-1) — 확인된 조합만 살아남는다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.9·§11.10. 네트워크·API·
ffmpeg를 전혀 부르지 않는다 — 데이터 계약과 사전 차단만 시험한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw.capabilities import (
    FEATURE_JSON,
    FEATURE_KEYWORDS,
    FEATURE_LANGUAGES,
    FEATURE_LOGPROBS,
    FEATURE_PROMPT,
    FEATURE_SEGMENT_TIMES,
    FEATURE_SRT,
    FEATURE_VERBOSE_JSON,
    FEATURE_VTT,
    FEATURE_WORD_TIMES,
    ModelCapabilitySnapshot,
    TimingProviderUnavailable,
    UnverifiedModelCapability,
    capability_snapshot_for_role,
    confirm_request_combo,
    preflight_online_transcription,
    price_per_minute,
)
from vlogsrt.project.raw.config import (
    CONFIRMED_REGISTRY_REVISION,
    RawModelRegistry,
    RawModelRoles,
    RawWorkflowConfig,
)

TEXT_MODEL = RawModelRoles().text_model
TIMING_MODEL = RawModelRoles().timing_model
ANNOUNCED_SHUTDOWN = date(2027, 2, 26)


def snapshot(role: str) -> ModelCapabilitySnapshot:
    return capability_snapshot_for_role(role)


def test_confirmed_snapshots_pin_the_documented_pair() -> None:
    """등록표는 문구·시간 두 역할뿐이고 모델은 설정 기본값 그대로다(§11.9)."""
    records = capabilities.confirmed_capability_snapshots()
    assert [record.role for record in records] == ["text", "timing"]
    text, timing = records
    assert text.model_id == TEXT_MODEL and timing.model_id == TIMING_MODEL
    assert text.registry_revision == CONFIRMED_REGISTRY_REVISION
    assert text.docs_checked_at == date(2026, 10, 2)
    assert {record.endpoint for record in records} == {"audio.transcriptions"}


def test_text_role_confirms_only_text_features() -> None:
    """문구 역할의 확인된 기능은 json·languages·keywords·prompt 뿐이다(§11.4)."""
    text = snapshot("text")
    confirmed = set(text.confirmed_supported_features)
    assert {FEATURE_JSON, FEATURE_LANGUAGES, FEATURE_KEYWORDS, FEATURE_PROMPT} <= confirmed
    refused = set(text.unsupported_or_unverified_features)
    assert {
        FEATURE_VERBOSE_JSON,
        FEATURE_SRT,
        FEATURE_VTT,
        FEATURE_SEGMENT_TIMES,
        FEATURE_WORD_TIMES,
        FEATURE_LOGPROBS,
    } <= refused


def test_timing_role_confirms_only_the_segment_skeleton_word_times_and_language() -> None:
    """시간 역할의 확인된 기능은 verbose_json·세그먼트 입자·단어 입자·단수 언어뿐이다(단어는 D73
    W-25c, 단수 언어는 D77 W-29a — 복수형 languages 힌트는 여전히 미확인)."""
    timing = snapshot("timing")
    assert set(timing.confirmed_supported_features) == {
        FEATURE_VERBOSE_JSON,
        FEATURE_SEGMENT_TIMES,
        FEATURE_WORD_TIMES,
        capabilities.FEATURE_LANGUAGE,
    }
    refused = set(timing.unsupported_or_unverified_features)
    assert {
        FEATURE_JSON,
        FEATURE_LANGUAGES,
        FEATURE_KEYWORDS,
        FEATURE_PROMPT,
        FEATURE_SRT,
        FEATURE_VTT,
        FEATURE_LOGPROBS,
    } <= refused


def test_timing_snapshot_carries_the_announced_lifecycle() -> None:
    """종료 공지(2026-08-26)·종료일(2027-02-26)은 시간 역할에만 있다(§11.10).

    문구 역할에 종료일을 지어 붙이지 않는다 — None이 확인 안 됨의 값이다.
    """
    text, timing = capabilities.confirmed_capability_snapshots()
    assert timing.deprecation_notice_date == date(2026, 8, 26)
    assert timing.shutdown_date == ANNOUNCED_SHUTDOWN
    assert text.deprecation_notice_date is None and text.shutdown_date is None


def test_access_and_integration_status_start_unverified() -> None:
    """미검증은 기록은 되지만 호출을 막는 값이 아니다(§11.9)."""
    for record in capabilities.confirmed_capability_snapshots():
        assert record.account_access_status == "unverified"
        assert record.integration_test_status == "not_run"


@pytest.mark.parametrize(
    ("role", "model", "response_format", "features"),
    [
        ("text", TEXT_MODEL, "json", (FEATURE_LANGUAGES, FEATURE_KEYWORDS, FEATURE_PROMPT)),
        ("text", TEXT_MODEL, "json", ()),
        ("timing", TIMING_MODEL, "verbose_json", (FEATURE_SEGMENT_TIMES,)),
        ("timing", TIMING_MODEL, "verbose_json", (FEATURE_SEGMENT_TIMES, FEATURE_WORD_TIMES)),
    ],
)
def test_confirm_accepts_the_documented_combos(
    role: str, model: str, response_format: str, features: tuple[str, ...]
) -> None:
    confirmed = confirm_request_combo(
        role=role, model=model, response_format=response_format, features=features
    )
    assert confirmed.role == role


@pytest.mark.parametrize(
    ("role", "model", "response_format", "features"),
    [
        # 문구 모델에 시간 형식·입자·자막 직렬화를 붙이는 무단 조합 (§11.4)
        ("text", TEXT_MODEL, "verbose_json", ()),
        ("text", TEXT_MODEL, "json", (FEATURE_SEGMENT_TIMES,)),
        ("text", TEXT_MODEL, "json", (FEATURE_LOGPROBS,)),
        # 시간 역할에 문구 힌트·json 응답을 붙이는 조합
        ("timing", TIMING_MODEL, "json", ()),
        ("timing", TIMING_MODEL, "verbose_json", (FEATURE_PROMPT,)),
        ("timing", TIMING_MODEL, "verbose_json", (FEATURE_LANGUAGES,)),
        # 모델 교체 — 조용한 fallback이 아니라 실패 (§11.9)
        ("text", "gpt-4o-mini-transcribe", "json", ()),
        ("timing", "whisper-large-v3", "verbose_json", ()),
        # 모르는 역할
        ("alignment", TEXT_MODEL, "json", ()),
    ],
)
def test_confirm_rejects_everything_the_registry_does_not_know(
    role: str, model: str, response_format: str, features: tuple[str, ...]
) -> None:
    with pytest.raises(UnverifiedModelCapability):
        confirm_request_combo(
            role=role, model=model, response_format=response_format, features=features
        )


def test_preflight_blocks_timing_from_the_announced_shutdown_day() -> None:
    """종료일 당일부터 신규 온라인 시간 작업은 보수적으로 막힌다(§11.10)."""
    config = RawWorkflowConfig()
    preflight_online_transcription(config, date(2027, 2, 25))  # 전날까지는 연다
    for blocked_on in (ANNOUNCED_SHUTDOWN, date(2027, 6, 1)):
        with pytest.raises(TimingProviderUnavailable):
            preflight_online_transcription(config, blocked_on)


def test_preflight_lets_text_survive_timing_expiry() -> None:
    """시간 제공자가 닫혀도 문구 역할은 별개다 — 텍스트만 남는 실행은 별도 승인이지만
    이 문(수명주기 사전 검사)은 시간 역할에만 종료일을 묻는다."""
    config = RawWorkflowConfig()
    preflight_online_transcription(config, date(2027, 6, 1), roles=(capabilities.ROLE_TEXT,))


def test_preflight_rejects_a_config_that_breaks_the_frozen_contract() -> None:
    """replace로 지어낸 설정은 생성 시 검사를 안 지났다 — 온라인 문이 먼저 닫는다(루트 리뷰).

    종료 차단을 끄거나 확인된 리비전의 종료일을 민 설정은 `validate_raw_workflow_config`가
    거부하고, preflight는 그것을 `UnverifiedModelCapability`로 갈아 올린다.
    """
    unblocked = replace(
        RawWorkflowConfig(),
        registry=replace(RawModelRegistry(), block_online_at_shutdown_date=False),
    )
    with pytest.raises(UnverifiedModelCapability, match="동결 계약을 어긴다"):
        preflight_online_transcription(unblocked, date(2027, 6, 1))
    redated = replace(
        RawWorkflowConfig(),
        registry=replace(
            RawModelRegistry(),
            revision=CONFIRMED_REGISTRY_REVISION,
            timing_shutdown_date=date(2099, 1, 1),
        ),
    )
    with pytest.raises(UnverifiedModelCapability, match="동결 계약을 어긴다"):
        preflight_online_transcription(redated, date(2027, 6, 1))


def test_preflight_rejects_a_wellformed_but_unconfirmed_revision() -> None:
    """모양은 맞지만 확인된 적 없는 리비전도 우회다 — 리비전 고정이 닫는다(§11.9)."""
    unknown = replace(
        RawWorkflowConfig(),
        registry=replace(
            RawModelRegistry(),
            revision="openai-caption-2026-12-01-v2",
            docs_checked_at=date(2026, 12, 1),
            timing_deprecation_notice_date=date(2026, 11, 1),
            timing_shutdown_date=date(2027, 6, 26),
        ),
    )
    with pytest.raises(UnverifiedModelCapability, match="우회는 없다"):
        preflight_online_transcription(unknown, date(2026, 12, 2))


def test_preflight_rejects_a_swapped_model_before_any_call() -> None:
    """설정이 확인된 모델과 다르면 실패 — 옛·새 모델 조용한 갈아끼움 금지(§11.9)."""
    swapped_timing = replace(
        RawWorkflowConfig(), models=replace(RawModelRoles(), timing_model="other-timing")
    )
    with pytest.raises(UnverifiedModelCapability, match="조용한 모델 교체"):
        preflight_online_transcription(swapped_timing, date(2026, 10, 2))
    swapped_text = replace(
        RawWorkflowConfig(), models=replace(RawModelRoles(), text_model="other-text")
    )
    with pytest.raises(UnverifiedModelCapability):
        preflight_online_transcription(
            swapped_text, date(2026, 10, 2), roles=(capabilities.ROLE_TEXT,)
        )


def test_price_per_minute_matches_the_verified_docs() -> None:
    """2026-10-02 공식 페이지 기준 — 문구 $0.0045/분, 시간 $0.006/분(§11.8)."""
    assert price_per_minute("text") == Decimal("0.0045")
    assert price_per_minute("timing") == Decimal("0.006")
