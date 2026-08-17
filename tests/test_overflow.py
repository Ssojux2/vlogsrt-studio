"""정렬이 무너진 자리에서 큐 하나가 텍스트를 통째로 삼키는 것을 막는다.

실측(D16): whisper가 앞 9분을 놓치자 그 구간 정본이 1초짜리 큐에 900자로
쏟아졌고, 멀쩡하던 뒤 4분치 자막까지 밀려 못 쓰게 됐다.
"""

from __future__ import annotations

from vlogsrt.config import Config
from vlogsrt.overflow import OVERFLOW, cap, limit, report

from .conftest import make_cue


def test_normal_cue_is_untouched() -> None:
    cue = make_cue(0.0, 2.0, "오늘은 망원동에 왔어")
    capped, spills = cap([cue], Config())
    assert capped == [cue]
    assert spills == []


def test_dump_is_trimmed_and_reported() -> None:
    cue = make_cue(10.0, 11.0, "가나다 " * 300)  # 1초에 1200자
    capped, spills = cap([cue], Config())
    assert len(capped[0].text) <= limit(cue, Config())
    assert OVERFLOW in capped[0].flags
    assert len(spills) == 1
    assert spills[0].dropped > 1000
    assert spills[0].start == 10.0


def test_trim_lands_on_a_word_boundary() -> None:
    cue = make_cue(0.0, 1.0, "망원동 " * 200)
    capped, _ = cap([cue], Config())
    assert not capped[0].text.endswith("망원")  # 단어를 반으로 자르지 않는다
    assert capped[0].text.split()[-1] == "망원동"


def test_long_cue_gets_a_bigger_budget() -> None:
    cfg = Config()
    short = make_cue(0.0, 1.0, "x")
    long = make_cue(0.0, 7.0, "x")
    assert limit(long, cfg) > limit(short, cfg)
    # 짧은 큐도 최소한 조판 한도만큼은 담는다
    assert limit(short, cfg) >= cfg.max_chars_per_cue


def test_a_cue_that_merely_exceeds_cps_is_kept() -> None:
    """읽기 속도 초과는 정제가 다룬다. 여기서 자를 일이 아니다."""
    cfg = Config()
    cue = make_cue(0.0, 1.0, "가" * (cfg.max_chars_per_cue + 4))
    capped, spills = cap([cue], cfg)
    assert spills == []
    assert capped[0].text == cue.text


def test_report_is_silent_when_nothing_spilled() -> None:
    assert report([], Config()) == []


def test_report_names_the_worst_places() -> None:
    cues = [make_cue(10.0, 11.0, "가나다 " * 300), make_cue(20.0, 21.0, "라마바 " * 100)]
    _, spills = cap(cues, Config())
    lines = report(spills, Config())
    assert "큐 2개" in lines[0]
    assert "10~11초" in lines[1]  # 가장 많이 버린 자리가 먼저


def test_neighbours_survive_a_dump() -> None:
    """망가진 큐 하나가 이웃까지 데려가면 안 된다."""
    good_before = make_cue(0.0, 2.0, "정상 자막입니다")
    dump = make_cue(2.0, 3.0, "가나다 " * 300)
    good_after = make_cue(3.0, 5.0, "이것도 정상입니다")
    capped, spills = cap([good_before, dump, good_after], Config())
    assert capped[0].text == good_before.text
    assert capped[2].text == good_after.text
    assert len(spills) == 1
