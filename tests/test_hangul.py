"""자모 분해와 index map. 여기 버그는 나중에 좌표 오류로 나타난다."""

from __future__ import annotations

import pytest

from vlogsrt.hangul import CHO, JONG, JUNG, decompose_jamo, normalize, strip_for_compare


def test_no_jong() -> None:
    assert decompose_jamo("가") == ["ㄱ", "ㅏ"]


def test_with_jong() -> None:
    assert decompose_jamo("강") == ["ㄱ", "ㅏ", "ㅇ"]


def test_double_jong_stays_single() -> None:
    assert decompose_jamo("앉") == ["ㅇ", "ㅏ", "ㄵ"]


@pytest.mark.parametrize("ch", ["A", "1", "漢", "🍺", " ", ","])
def test_non_hangul_passthrough(ch: str) -> None:
    assert decompose_jamo(ch) == [ch]


def test_table_sizes() -> None:
    assert (len(CHO), len(JUNG), len(JONG)) == (19, 21, 28)
    assert JONG[0] == ""


def test_empty_string() -> None:
    assert normalize("") == ("", [])


def test_normalize_drops_space_and_punctuation() -> None:
    norm, imap = normalize("가, 나")
    assert norm == "ㄱㅏㄴㅏ"
    assert imap == [0, 0, 3, 3]


def test_normalize_keeps_latin_and_digits_folded() -> None:
    norm, _ = normalize("Ab1")
    assert norm == "ab1"


@pytest.mark.parametrize(
    "text",
    [
        "안녕하세요",
        "오늘은 블루펭귄에서 낮술!",
        "A와 B, 그리고 1234",
        "받침 없는 글자와 앉았다 같은 겹받침",
        "🍺 이모지 섞임",
        " ",
        "",
    ],
)
def test_imap_roundtrip(text: str) -> None:
    norm, imap = normalize(text)
    assert len(norm) == len(imap)
    assert all(0 <= index < len(text) for index in imap)
    assert imap == sorted(imap)
    for position in range(1, len(imap)):
        assert imap[position] - imap[position - 1] >= 0


def test_imap_points_at_source_character() -> None:
    text = "강가"
    norm, imap = normalize(text)
    assert norm == "ㄱㅏㅇㄱㅏ"
    assert [text[index] for index in imap] == ["강", "강", "강", "가", "가"]


def test_strip_for_compare_removes_punctuation_only() -> None:
    assert strip_for_compare("안녕, 하세요!") == "안녕하세요"
