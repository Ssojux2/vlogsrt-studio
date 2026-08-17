"""비교용 정규화 — 표기 차이가 검증·평가를 흔들면 안 된다."""

from __future__ import annotations

from vlogsrt.hangul import strip_for_compare, to_nfc


def test_nfc_folds_decomposed_hangul() -> None:
    decomposed = "가나"  # NFD로 들어올 수 있다
    import unicodedata

    assert to_nfc(unicodedata.normalize("NFD", decomposed)) == decomposed


def test_strip_removes_spacing_and_punctuation() -> None:
    assert strip_for_compare("안녕하세요, 여러분!") == strip_for_compare("안녕 하세요 여러분")


def test_strip_folds_case() -> None:
    assert strip_for_compare("OK Cafe") == strip_for_compare("ok cafe")


def test_strip_keeps_korean_intact() -> None:
    assert strip_for_compare("망원동") == "망원동"


def test_strip_empty() -> None:
    assert strip_for_compare("  ...  ") == ""
