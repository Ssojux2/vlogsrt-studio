"""되풀이 검출 — 순수 함수다. 자리를 짚을 뿐 아무것도 바꾸지 않는다(D27)."""

from __future__ import annotations

from dataclasses import replace as data_replace
from pathlib import Path

import pytest

from vlogsrt.config import Config
from vlogsrt.models import WHY_COMPRESSION, WHY_ECHO, Segment
from vlogsrt.repetition import (
    collapse,
    find_repeats,
    looks_repetitive,
    repetitive_seconds,
    repetitive_share,
    text_compression_ratio,
)


def _echoing(count: int, text: str = "꼬불꼬불 꼬불하고", start: float = 0.0) -> list[Segment]:
    return [
        Segment(id=index, start=start + index, end=start + index + 1.0, text=text)
        for index in range(count)
    ]


def _speaking(count: int, ratio: float = 1.2, start: float = 0.0) -> list[Segment]:
    return [
        Segment(
            id=index,
            start=start + index,
            end=start + index + 1.0,
            text=f"여기는 {index}번째 가게입니다",
            compression_ratio=ratio,
        )
        for index in range(count)
    ]


# ─────────────────────────────────────────────────────────────
#  신호 1 — 연속 세그먼트가 글자 그대로 같다
# ─────────────────────────────────────────────────────────────


def test_an_echo_run_is_found() -> None:
    """실측: 한 문장이 1668개 세그먼트에 걸쳐 1668초 반복됐다(D26)."""
    repeats = find_repeats(_echoing(5), Config())
    assert len(repeats) == 1
    assert repeats[0].count == 5
    assert repeats[0].reason == WHY_ECHO
    assert repeats[0].seg_ids == (0, 1, 2, 3, 4)


def test_a_brief_echo_is_not_a_repeat() -> None:
    """맞장구가 두어 번 겹치는 것은 환청이 아니다."""
    assert find_repeats(_echoing(2), Config()) == []


def test_a_short_run_is_not_a_repeat() -> None:
    cfg = data_replace(Config(), loop_min_seconds=60.0)
    assert find_repeats(_echoing(5), cfg) == []


def test_normal_speech_has_no_repeats() -> None:
    assert find_repeats(_speaking(9), Config()) == []


# ─────────────────────────────────────────────────────────────
#  신호 2 — whisper 자신의 compression_ratio (공짜 신호)
# ─────────────────────────────────────────────────────────────


def test_whispers_own_repetition_metric_is_used() -> None:
    """글자가 매번 달라도 whisper가 스스로 되풀이라고 표시한 구간은 잡는다.

    실측: `compression_ratio > 2.4`인 세그먼트 248개 중 로그 확률이 나쁜 것은
    0개였다. API의 자동 온도 상승은 로그 확률만 보므로 이 병리에 반응하지 않는다(D27).
    """
    spine = _speaking(5, ratio=5.4)
    repeats = find_repeats(spine, Config())
    assert len(repeats) == 1
    assert repeats[0].reason == WHY_COMPRESSION
    assert repeats[0].count == 5


def test_a_healthy_compression_ratio_is_left_alone() -> None:
    assert find_repeats(_speaking(5, ratio=2.39), Config()) == []


def test_the_threshold_comes_from_config() -> None:
    """규칙 8 — 임계값은 코드에 박지 않는다."""
    lenient = data_replace(Config(), loop_compression_ratio=99.0)
    assert find_repeats(_speaking(5, ratio=5.4), lenient) == []


def test_overlapping_signals_become_one_repeat() -> None:
    """같은 구간을 두 신호가 짚으면 한 번만 재전사한다."""
    spine = [data_replace(segment, compression_ratio=5.4) for segment in _echoing(5)]
    repeats = find_repeats(spine, Config())
    assert len(repeats) == 1
    assert repeats[0].start == 0.0
    assert repeats[0].end == 5.0


def test_repeats_come_back_in_time_order() -> None:
    spine = [
        *_echoing(4, text="소세지", start=0.0),
        *_speaking(3, start=10.0),
        *_echoing(4, text="와인넷", start=20.0),
    ]
    repeats = find_repeats(spine, Config())
    assert [repeat.text for repeat in repeats] == ["소세지", "와인넷"]
    assert repeats[0].end <= repeats[1].start


# ─────────────────────────────────────────────────────────────
#  재전사 결과 판정 — 우리도 whisper와 같은 자로 잰다
# ─────────────────────────────────────────────────────────────


def test_repeated_text_measures_as_repetitive() -> None:
    assert looks_repetitive(" ".join(["꼬불꼬불 꼬불하고"] * 20), Config())


def test_ordinary_speech_does_not() -> None:
    said = "안녕하세요 오늘은 망원동에 왔습니다 낮술 하러 왔어요 여기는 블루펭귄이라는 가게입니다"
    assert not looks_repetitive(said, Config())


def test_long_clean_speech_is_not_repetitive() -> None:
    """**압축률을 통째로 재던 버그의 회귀 테스트.**

    압축률은 글자가 길어지기만 해도 올라간다. 그대로 두면 27분짜리 구간을 **완벽하게
    되살려도** 되풀이로 보고 접어버린다 — D27이 고치려던 바로 그 사고다.

    실제 회차에서 whisper **스스로 정상이라 표시한**(`compression_ratio <= 2.4`)
    세그먼트만 모아 쓴다. 합성 문장으로는 못 잡는다 — 비슷한 문장을 찍어내면 그건
    진짜 되풀이이고, 검출기가 잡는 것이 맞다.
    """
    said = " ".join(_real_clean_speech()[:529])
    assert len(said) > 5000, "짧은 글로는 이 버그를 못 잡는다"
    assert text_compression_ratio(said) > Config().loop_compression_ratio, "통째로 재면 넘는다"
    assert not looks_repetitive(said, Config()), "창으로 재면 정상이다"


def _real_clean_speech() -> list[str]:
    """실제 회차에서 whisper가 정상이라 표시한 세그먼트의 글자. **읽기만 한다.**"""
    from vlogsrt import transcribe

    path = Path(__file__).parent / "fixtures" / "api" / "fr_day_1_2_fhd.whisper.json"
    spine = transcribe.build_spine(transcribe.load_fixture_responses(path))
    ceiling = Config().loop_compression_ratio
    return [s.text for s in spine if 0.0 < s.compression_ratio <= ceiling]


def test_a_loop_hidden_inside_long_clean_speech_is_still_found() -> None:
    """반대 방향도 봐야 한다 — 창으로 나눠 재도 되풀이는 잡혀야 한다."""
    clean = " ".join(f"{index}번째 가게에 들어와 봤습니다" for index in range(30))
    loop = " ".join(["꼬불꼬불 꼬불하고 여기 안으로 들어갔다가"] * 80)
    assert looks_repetitive(f"{clean} {loop}", Config())


def test_empty_text_is_not_repetitive() -> None:
    assert not looks_repetitive("", Config())
    assert text_compression_ratio("") == 0.0


# ─────────────────────────────────────────────────────────────
#  세그먼트가 있을 때 — 되풀이가 덮는 시간의 비율
# ─────────────────────────────────────────────────────────────


def test_a_clean_rescue_has_no_repetitive_share() -> None:
    assert repetitive_share(_speaking(6), Config()) == 0.0


def test_a_looping_rescue_is_almost_entirely_repetitive() -> None:
    assert repetitive_share(_echoing(10), Config()) > 0.9


def test_a_partly_recovered_rescue_lands_in_between() -> None:
    """일부만 되풀이인 결과는 통째로 버리지 않는다 — 원본보다는 낫다."""
    mixed = [*_speaking(8, start=0.0), *_echoing(4, start=8.0)]
    share = repetitive_share(mixed, Config())
    assert 0.0 < share < 0.5


def test_share_of_nothing_is_zero() -> None:
    assert repetitive_share([], Config()) == 0.0


# ─────────────────────────────────────────────────────────────
#  남은 초 — 비율이 놓치는 것을 잡는다 (D28)
# ─────────────────────────────────────────────────────────────


def test_a_clean_rescue_has_no_repetitive_seconds() -> None:
    assert repetitive_seconds(_speaking(6), Config()) == 0.0
    assert repetitive_seconds([], Config()) == 0.0


def test_the_seconds_are_the_repeat_durations_not_a_ratio() -> None:
    looped = _echoing(10, start=8.0)
    span = looped[-1].end - looped[0].start
    assert repetitive_seconds(looped, Config()) == pytest.approx(span)


def test_seconds_stay_the_same_however_long_the_clean_part_is() -> None:
    """**비율이 놓치는 지점.** 깨끗한 부분이 길어질수록 비율은 0으로 가지만, 되풀이는
    한 초도 줄지 않는다. 490초 구간이 245초를 되풀이해도 합격하던 이유가 이것이다."""
    loop = _echoing(6, start=0.0)
    narrow = [*loop, *_speaking(4, start=10.0)]
    wide = [*loop, *_speaking(60, start=10.0)]
    assert repetitive_share(wide, Config()) < repetitive_share(narrow, Config()) / 2
    assert repetitive_seconds(wide, Config()) == repetitive_seconds(narrow, Config())


# ─────────────────────────────────────────────────────────────
#  접기 — 회복 못 한 구간의 마지막 처리
# ─────────────────────────────────────────────────────────────


def test_collapse_keeps_exactly_the_first_card() -> None:
    """1668장 대신 한 장. 나머지 구간은 자막이 서지 않는다(D27이 규칙 9를 연 지점)."""
    collapsed = collapse(_echoing(5))
    assert len(collapsed) == 1
    assert collapsed[0] == _echoing(5)[0]


def test_collapse_of_nothing_is_nothing() -> None:
    assert collapse([]) == []


def test_detection_never_touches_the_spine() -> None:
    """규칙 2 — 검출은 읽기만 한다."""
    spine = _echoing(5)
    before = list(spine)
    find_repeats(spine, Config())
    assert spine == before
