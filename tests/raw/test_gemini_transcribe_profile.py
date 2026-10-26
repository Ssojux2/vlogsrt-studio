"""Gemini 3.5 Transcribe 등록표(W-31a, D92) — 시간 엔진의 단가·한도·언어 잠금.

2026-10-26 공식 문서(https://ai.google.dev/gemini-api/docs/transcribe ·
https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-transcribe): 오디오 입력 분당 $0.003 + 글자
출력 분당 $0.002, 단어 시각을 켜면 30분(Cloud 문서는 15분)까지, 인라인 요청 20 MB까지. 단가를
0원으로 두지 않고, 재확인 기한이 지나면 그 단가로 매기지 않는다(D84와 같은 닫힘).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from vlogsrt.project.raw.analysis_config import MAX_ASR_CHUNK_SECONDS
from vlogsrt.project.raw.config import (
    GEMINI_TRANSCRIBE_PROFILE,
    gemini_transcribe_language_codes,
    gemini_transcribe_price_per_minute,
    gemini_transcribe_profile_problems,
)


def test_the_profile_is_the_documented_gemini_3_5_transcribe() -> None:
    profile = GEMINI_TRANSCRIBE_PROFILE
    assert profile.model == "gemini-3.5-transcribe"
    assert (profile.api_base_url, profile.api_version) == (
        "https://generativelanguage.googleapis.com",
        "v1beta",
    )
    assert (profile.mode, profile.granularities) == ("verbatim", ("word",))
    assert profile.audio_input_per_minute == Decimal("0.003")
    assert profile.text_output_per_minute == Decimal("0.002")
    assert profile.pricing_checked_at == date(2026, 10, 26)
    assert (profile.lifecycle_status, profile.word_timestamps_status) == ("preview", "experimental")
    assert gemini_transcribe_profile_problems(profile) == []


def test_every_stage1_chunk_fits_the_word_time_and_inline_limits() -> None:
    """조각은 180초 이하 16 kHz 모노 16비트 WAV — base64로 늘어도 인라인 한도 안이다."""
    profile = GEMINI_TRANSCRIBE_PROFILE
    assert profile.max_audio_seconds_with_word_times >= MAX_ASR_CHUNK_SECONDS
    wav_bytes = int(MAX_ASR_CHUNK_SECONDS * 16_000 * 2) + 44
    assert (wav_bytes + 2) // 3 * 4 < profile.inline_request_max_bytes


def test_the_language_lock_maps_iso_codes_and_refuses_unknown_ones() -> None:
    assert gemini_transcribe_language_codes(("ko",)) == ("ko-KR",)
    with pytest.raises(ValueError, match="언어"):
        gemini_transcribe_language_codes(("xx",))
    with pytest.raises(ValueError, match="언어"):
        gemini_transcribe_language_codes(())


def test_the_price_closes_after_its_recheck_date() -> None:
    profile = GEMINI_TRANSCRIBE_PROFILE
    assert gemini_transcribe_price_per_minute(profile, today=profile.pricing_checked_at) == Decimal(
        "0.005"
    )
    after = date.fromordinal(profile.price_valid_through.toordinal() + 1)
    assert gemini_transcribe_price_per_minute(profile, today=after) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"audio_input_per_minute": Decimal("0")},
        {"text_output_per_minute": Decimal("NaN")},
        {"model": "whisper-1"},
        {"api_base_url": "http://generativelanguage.googleapis.com"},
        {"mode": "smart"},  # smart는 단어 시각과 함께 쓸 수 없다
        {"granularities": ()},
        {"language_codes": {}},
        {"max_audio_seconds_with_word_times": 60.0},
        {"price_valid_through": date(2026, 1, 1)},
    ],
)
def test_a_broken_profile_is_named(bad: dict[str, object]) -> None:
    assert gemini_transcribe_profile_problems(replace(GEMINI_TRANSCRIBE_PROFILE, **bad))


def test_the_chunk_floor_mirrors_the_stage1_chunk_limit() -> None:
    from vlogsrt.project.raw.config import GEMINI_TRANSCRIBE_MIN_CHUNK_SECONDS

    assert GEMINI_TRANSCRIBE_MIN_CHUNK_SECONDS == MAX_ASR_CHUNK_SECONDS
