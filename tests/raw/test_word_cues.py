"""단어 → 자막 줄(W-31b, D92) — 큐 경계는 엔진이 준 단어 경계에서만 고른다.

큐 시작 = 첫 시각 단어의 시작, 큐 끝 = 그 큐 시각 단어 끝의 최대값(정확 유리수 그대로). 단어를
쪼개지 않고 단어 사이 시각을 만들지 않는다. 끊는 자리는 문장 끝 부호 뒤 · 쉼(0.6초 이상) 앞 · 글자·
길이 한도를 넘기 전이다. 시각 없는 단어는 이웃 큐에 글자째 남고 세어지며 시각을 주지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.project.raw import gemini_transcribe_parse as gtp
from vlogsrt.project.raw import word_cues
from vlogsrt.project.raw.config import WORD_CUE_SETTINGS, word_cue_problems

F = Fraction
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "gemini"


def w(text: str, start: Fraction | None, end: Fraction | None, display: str | None = None) -> Any:
    return gtp.ParsedWord(text=text, display=display or f"{text} ", start=start, end=end)


def _cues(words: list[Any], **settings: Any) -> word_cues.CueGroup:
    chosen = replace(WORD_CUE_SETTINGS, **settings) if settings else WORD_CUE_SETTINGS
    return word_cues.group(words, chosen)


def _spans(group: word_cues.CueGroup) -> list[tuple[Fraction, Fraction, str]]:
    return [(cue.start, cue.end, cue.text) for cue in group.cues]


def test_the_documented_response_becomes_three_sentence_cues() -> None:
    payload = json.loads((FIXTURES / "transcribe_korean.json").read_text(encoding="utf-8"))
    parsed = gtp.parse_transcription(payload, chunk_duration=F(30))
    assert _spans(word_cues.group(parsed.words, WORD_CUE_SETTINGS)) == [
        (F(1, 10), F(9, 10), "안녕하세요."),
        (F(6, 5), F(14, 5), "오늘은 한강에 왔어요!"),
        (F(18, 5), F(24, 5), "날씨 진짜 좋다"),
    ]


def test_a_pause_of_the_threshold_breaks_and_a_shorter_one_does_not() -> None:
    words = [w("가", F(0), F(1)), w("나", F(8, 5), F(2)), w("다", F(259, 100), F(3))]
    assert _spans(_cues(words)) == [(F(0), F(1), "가"), (F(8, 5), F(3), "나 다")]


def test_the_character_limit_breaks_before_a_word_and_a_long_word_stays_whole() -> None:
    words = [w("가나다라마", F(i), F(i) + F(1, 2)) for i in range(4)]
    group = _cues(words, max_cue_chars=12)
    assert [cue.text for cue in group.cues] == ["가나다라마 가나다라마", "가나다라마 가나다라마"]
    long = [w("가" * 40, F(0), F(2))]
    assert [cue.text for cue in _cues(long).cues] == ["가" * 40]


def test_the_duration_limit_breaks_before_a_word_that_would_end_too_late() -> None:
    words = [w("가", F(0), F(1)), w("나", F(3, 2), F(6)), w("다", F(13, 2), F(15, 2))]
    assert _spans(_cues(words)) == [(F(0), F(6), "가 나"), (F(13, 2), F(15, 2), "다")]


def test_untimed_words_stay_in_a_cue_and_never_give_it_a_time() -> None:
    words = [
        w("음", None, None),
        w("안녕", F(1), F(2)),
        w("어", None, None),
        w("가자", F(21, 10), F(3)),
    ]
    group = _cues(words)
    assert _spans(group) == [(F(1), F(3), "음 안녕 어 가자")]
    assert group.untimed_words == 2


def test_out_of_order_words_keep_response_order_and_are_counted() -> None:
    words = [w("가", F(2), F(3)), w("나", F(1), F(3, 2))]
    group = _cues(words)
    assert _spans(group) == [(F(2), F(3), "가 나")]
    assert group.out_of_order == 1


def test_every_word_survives_in_order_and_grouping_is_deterministic() -> None:
    words = [w(f"말{i}", F(i, 2), F(i, 2) + F(2, 5)) for i in range(40)]
    first = _cues(words)
    assert first == _cues(words)
    assert " ".join(cue.text for cue in first.cues).split() == [f"말{i}" for i in range(40)]


def test_no_words_means_no_cues() -> None:
    assert _cues([]).cues == ()


def test_limits_mirror_the_engine_config_and_bad_settings_are_named() -> None:
    config = Config()
    assert WORD_CUE_SETTINGS.max_cue_chars == config.max_chars_per_cue
    assert WORD_CUE_SETTINGS.max_cue_seconds == F(str(config.max_duration))
    assert word_cue_problems(WORD_CUE_SETTINGS) == []
    for bad in (
        {"max_cue_chars": 0},
        {"max_cue_seconds": F(0)},
        {"pause_break_seconds": F(-1)},
        {"sentence_end_marks": ()},
        {"rule_tag": " "},
    ):
        assert word_cue_problems(replace(WORD_CUE_SETTINGS, **bad))


def test_words_without_any_time_are_refused_not_given_a_time() -> None:
    with pytest.raises(ValueError, match="시각 단어"):
        _cues([w("음", None, None), w("어", None, None)])


def test_each_cue_counts_its_own_untimed_dropped_and_late_words() -> None:
    first = gtp.ParsedWord("안녕", "안녕. ", F(1), F(2), dropped_after=2)
    words = [
        first,
        w("가", F(3), F(4)),
        w("어", None, None),
        w("나", F(5, 2), F(3)),
    ]
    group = _cues(words)
    assert [(c.untimed_words, c.dropped_chars, c.out_of_order) for c in group.cues] == [
        (0, 2, 0),
        (1, 0, 1),
    ]
    assert word_cues.word_notes(group, gtp.ALIGNED) == (
        (0, word_cues.NOTE_UNPLACED),
        (1, word_cues.NOTE_UNTIMED),
        (1, word_cues.NOTE_UNORDERED),
    )


def test_a_fallback_alignment_notes_every_cue_and_a_clean_one_notes_none() -> None:
    group = _cues([w("가", F(1), F(2), "가. "), w("나", F(3), F(4))])
    assert word_cues.word_notes(group, gtp.ALIGNED) == ()
    assert word_cues.word_notes(group, gtp.FALLBACK) == (
        (0, word_cues.NOTE_FALLBACK),
        (1, word_cues.NOTE_FALLBACK),
    )
