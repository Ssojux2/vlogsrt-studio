"""배분 — 글자를 발화 구간에 나눠 담는다. 시각을 만들지 않는다(규칙 3)."""

from __future__ import annotations

import pytest

from vlogsrt.align import allocate

TEXT = "안녕하세요 오늘은 망원동에 왔습니다 낮술 하러 왔어요"


def _words(segments: list) -> list[str]:
    return " ".join(segment.text for segment in segments).split()


def test_times_are_the_span_values_untouched() -> None:
    """규칙 3 — 배분은 시각을 만들지도 옮기지도 않는다."""
    spans = [(1.0, 3.0), (4.5, 7.0), (9.0, 11.5)]
    segments = allocate(TEXT, spans, 0)
    assert [(s.start, s.end) for s in segments] == spans


def test_not_one_word_is_lost() -> None:
    """규칙 9 — 어절은 절대 버리지 않는다."""
    spans = [(1.0, 3.0), (4.5, 7.0), (9.0, 11.5)]
    assert _words(allocate(TEXT, spans, 0)) == TEXT.split()


def test_words_keep_their_order() -> None:
    spans = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0), (6.0, 7.0)]
    assert " ".join(_words(allocate(TEXT, spans, 0))) == TEXT


def test_a_word_is_never_split_across_two_spans() -> None:
    spans = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]
    for segment in allocate(TEXT, spans, 0):
        for word in segment.text.split():
            assert word in TEXT.split()


def test_a_longer_span_gets_more_letters() -> None:
    """글자 수 ∝ 발화 시간. 한국어는 음절 문자라 대체로 성립한다(D24)."""
    long_first = allocate(TEXT, [(0.0, 7.0), (8.0, 11.0)], 0)
    long_last = allocate(TEXT, [(0.0, 3.0), (4.0, 11.0)], 0)
    assert len(long_first) == len(long_last) == 2  # 한쪽이 다 삼키면 비교가 무의미하다
    assert len(long_first[0].text) > len(long_first[1].text)
    assert len(long_last[1].text) > len(long_last[0].text)


def test_equal_spans_split_the_text_roughly_in_half() -> None:
    segments = allocate(TEXT, [(0.0, 5.0), (6.0, 11.0)], 0)
    first, second = len(segments[0].text), len(segments[1].text)
    assert abs(first - second) <= max(len(word) for word in TEXT.split())


def test_rounding_error_does_not_pile_up_at_the_end() -> None:
    """구간마다 독립적으로 반올림하면 오차가 뒤로 쌓인다. 누적 목표를 쓰는 이유."""
    words = [f"단어{index:02d}" for index in range(40)]
    spans = [(float(i), float(i) + 0.5) for i in range(20)]
    segments = allocate(" ".join(words), spans, 0)
    counts = [len(segment.text.split()) for segment in segments]
    assert max(counts) - min(counts) <= 1


def test_leftover_words_ride_on_the_last_span() -> None:
    """어절이 구간보다 많이 남아도 버리지 않는다."""
    segments = allocate(TEXT, [(0.0, 1.0)], 0)
    assert len(segments) == 1
    assert segments[0].text == TEXT


def test_more_spans_than_words_leaves_the_tail_silent() -> None:
    """자막이 안 서는 구간이 생기는 건 정상이다. 글자가 사라지는 것과 다르다."""
    spans = [(float(i), float(i) + 0.5) for i in range(20)]
    segments = allocate("한 마디", spans, 0)
    assert _words(segments) == ["한", "마디"]
    assert len(segments) <= 2


def test_empty_text_makes_no_segment() -> None:
    assert allocate("   ", [(0.0, 1.0), (2.0, 3.0)], 0) == []


def test_no_span_makes_no_segment() -> None:
    assert allocate(TEXT, [], 0) == []


def test_ids_continue_from_the_base() -> None:
    """묶음마다 0부터 다시 세면 seg_ids로 역추적이 안 된다(규칙 2)."""
    segments = allocate(TEXT, [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)], 7)
    assert [segment.id for segment in segments] == [7, 8, 9]


def test_ids_have_no_holes_when_a_span_gets_nothing() -> None:
    segments = allocate("한 마디", [(float(i), float(i) + 0.5) for i in range(10)], 3)
    assert [segment.id for segment in segments] == list(range(3, 3 + len(segments)))


def test_a_zero_length_span_does_not_divide_by_zero() -> None:
    segments = allocate(TEXT, [(1.0, 1.0), (2.0, 6.0)], 0)
    assert _words(segments) == TEXT.split()


def test_text_is_stripped_of_surrounding_whitespace() -> None:
    segments = allocate("  안녕하세요  \n 반갑습니다 ", [(0.0, 1.0), (2.0, 3.0)], 0)
    assert [segment.text for segment in segments] == ["안녕하세요", "반갑습니다"]


def test_the_input_spans_are_not_mutated() -> None:
    spans = [(0.0, 1.0), (2.0, 3.0)]
    allocate(TEXT, spans, 0)
    assert spans == [(0.0, 1.0), (2.0, 3.0)]


def test_every_segment_carries_letters() -> None:
    spans = [(float(i), float(i) + 0.5) for i in range(8)]
    for segment in allocate(TEXT, spans, 0):
        assert segment.text.strip()


def test_segments_come_out_in_time_order() -> None:
    spans = [(float(i) * 2, float(i) * 2 + 1.0) for i in range(6)]
    segments = allocate(TEXT, spans, 0)
    for earlier, later in zip(segments, segments[1:], strict=False):
        assert earlier.start < later.start
        assert earlier.end <= later.start


@pytest.mark.parametrize("span_count", [1, 2, 3, 5, 8, 13, 21])
def test_words_survive_any_number_of_spans(span_count: int) -> None:
    spans = [(float(i) * 2, float(i) * 2 + 1.5) for i in range(span_count)]
    assert _words(allocate(TEXT, spans, 0)) == TEXT.split()
