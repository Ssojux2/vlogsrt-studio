"""SRT 파싱·직렬화와 검토용 출력."""

from __future__ import annotations

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Cue
from vlogsrt.output import (
    format_timecode,
    mark_review_text,
    parse_srt,
    parse_timecode,
    to_review_cues,
    write_srt,
)
from vlogsrt.report import dictionary_candidates

from .conftest import make_cue

SAMPLE = (
    "1\n00:00:01,000 --> 00:00:02,500\n안녕하세요\n\n2\n00:00:03,000 --> 00:00:04,000\n두 번째\n"
)


def test_timecode_format() -> None:
    assert format_timecode(0) == "00:00:00,000"
    assert format_timecode(61.5) == "00:01:01,500"


def test_timecode_uses_comma_not_period() -> None:
    assert "," in format_timecode(1.234)
    assert "." not in format_timecode(1.234)


def test_millisecond_rounds_half_up() -> None:
    assert format_timecode(12.4105) == "00:00:12,411"


def test_negative_clamped_to_zero() -> None:
    assert format_timecode(-1.0) == "00:00:00,000"


def test_over_one_hour() -> None:
    assert format_timecode(5025.678) == "01:23:45,678"
    assert parse_timecode("01:23:45,678") == pytest.approx(5025.678)


def test_parse_lf_and_crlf() -> None:
    assert parse_srt(SAMPLE) == parse_srt(SAMPLE.replace("\n", "\r\n"))


def test_parse_bom() -> None:
    cues = parse_srt("﻿" + SAMPLE)
    assert len(cues) == 2
    assert cues[0].lines == ("안녕하세요",)


def test_parse_two_line_cue() -> None:
    cues = parse_srt("1\n00:00:01,000 --> 00:00:02,000\n윗줄\n아랫줄\n")
    assert cues[0].lines == ("윗줄", "아랫줄")


def test_parse_cue_containing_blank_line() -> None:
    body = (
        "1\n00:00:01,000 --> 00:00:02,000\n윗줄\n\n아랫줄\n\n2\n00:00:03,000 --> 00:00:04,000\n끝\n"
    )
    cues = parse_srt(body)
    assert len(cues) == 2
    assert cues[0].lines == ("윗줄", "", "아랫줄")


def test_roundtrip() -> None:
    cues = [
        Cue(start=1.0, end=2.5, lines=("안녕하세요",), index=1),
        Cue(start=3.0, end=4.0, lines=("두 번째", "줄바꿈"), index=2),
    ]
    assert parse_srt(write_srt(cues)) == cues


def test_write_srt_renumbers_from_one() -> None:
    cues = [Cue(start=0.0, end=1.0, lines=("가",), index=7)]
    assert write_srt(cues).splitlines()[0] == "1"


def test_parse_rejects_garbage() -> None:
    with pytest.raises(RuntimeError):
        parse_srt("이건 SRT가 아니다")


def test_review_srt_marks_only_changed_characters() -> None:
    marked = mark_review_text("블루팽귄에서 낮술", "블루펭귄에서 낮술")
    assert marked == "블루[?펭]귄에서 낮술"


def test_review_srt_merges_adjacent_diffs() -> None:
    assert mark_review_text("가나다", "가XY다").count("[?") == 1


def test_review_cues_keep_timing() -> None:
    cues = [make_cue(1.0, 2.0, "블루펭귄", whisper_text="블루팽귄")]
    reviewed = to_review_cues(cues)
    assert (reviewed[0].start, reviewed[0].end) == (1.0, 2.0)
    assert "[?" in reviewed[0].text


def test_dict_candidates_need_two_occurrences() -> None:
    cfg = Config()
    once = [make_cue(0.0, 1.0, "블루펭귄", whisper_text="블루팽귄")]
    twice = [*once, make_cue(2.0, 3.0, "블루펭귄", whisper_text="블루팽귄", index=2)]
    assert dictionary_candidates(once, cfg) == []
    candidates = dictionary_candidates(twice, cfg)
    assert [c.render() for c in candidates] == ["블루펭귄 ← 블루팽귄 2회"]
