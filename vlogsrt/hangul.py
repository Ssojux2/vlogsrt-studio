"""비교용 텍스트 정규화.

검증(verify)과 평가(evaluate)가 whisper·gpt 텍스트를 대조하기 전에
표기 차이(공백·부호·대소문자·정규화 형태)를 지운다.
"""

from __future__ import annotations

import unicodedata

# 정규화에서 버리는 유니코드 대분류: 구분자(Z), 문장부호(P), 제어문자(C).
_IGNORED_CATEGORIES = frozenset("ZPC")


def to_nfc(text: str) -> str:
    """비교 전에 합성형으로 맞춘다. 분해형 입력이 섞이면 비교가 무너진다."""
    return unicodedata.normalize("NFC", text)


def is_ignorable(ch: str) -> bool:
    return unicodedata.category(ch)[0] in _IGNORED_CATEGORIES


def strip_for_compare(text: str) -> str:
    """CER 등 텍스트 비교용. 공백·부호만 제거하고 소문자로 접는다."""
    return "".join(ch for ch in to_nfc(text) if not is_ignorable(ch)).casefold()
