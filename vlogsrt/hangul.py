"""한글 자모 분해와 비교용 정규화.

이후 모든 좌표 계산이 여기에 의존한다. `normalize`가 돌려주는 index map은
정규화 좌표 → 원본 좌표 역변환의 유일한 통로다.
"""

from __future__ import annotations

import unicodedata

SYLLABLE_BASE = 0xAC00
SYLLABLE_LAST = 0xD7A3
JUNG_N = 21
JONG_N = 28

CHO: tuple[str, ...] = (
    "ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ",
    "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ",
)  # fmt: skip

JUNG: tuple[str, ...] = (
    "ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅘ",
    "ㅙ", "ㅚ", "ㅛ", "ㅜ", "ㅝ", "ㅞ", "ㅟ", "ㅠ", "ㅡ", "ㅢ", "ㅣ",
)  # fmt: skip

# 첫 항목은 받침 없음이다. 겹받침은 낱자로 쪼개지 않는다 — ㄵ은 ㄵ 하나로 센다.
JONG: tuple[str, ...] = (
    "", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ",
    "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ",
    "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ",
)  # fmt: skip

# 정규화에서 버리는 유니코드 대분류: 구분자(Z), 문장부호(P), 제어문자(C).
_IGNORED_CATEGORIES = frozenset("ZPC")


def to_nfc(text: str) -> str:
    """비교 전에 합성형으로 맞춘다. 분해형 입력이 섞이면 정렬이 무너진다."""
    return unicodedata.normalize("NFC", text)


def is_syllable(ch: str) -> bool:
    return len(ch) == 1 and SYLLABLE_BASE <= ord(ch) <= SYLLABLE_LAST


def decompose_jamo(ch: str) -> list[str]:
    """한글 음절 한 글자를 초/중/종성으로 쪼갠다. 한글이 아니면 그대로 한 개."""
    if not is_syllable(ch):
        return [ch]
    code = ord(ch) - SYLLABLE_BASE
    jong_index = code % JONG_N
    jung_index = (code // JONG_N) % JUNG_N
    cho_index = code // (JONG_N * JUNG_N)
    jamo = [CHO[cho_index], JUNG[jung_index]]
    if jong_index:
        jamo.append(JONG[jong_index])
    return jamo


def is_ignorable(ch: str) -> bool:
    """공백·문장부호·제어문자는 정렬 잡음이라 정규화에서 뺀다."""
    return unicodedata.category(ch)[0] in _IGNORED_CATEGORIES


def normalize(text: str) -> tuple[str, list[int]]:
    """자모 시퀀스와 index map을 함께 돌려준다.

    index map의 i번째 값은 정규화 문자열 i번째 자모를 만들어낸 **원본** 문자의
    인덱스다. 항상 단조 증가하며 `len(norm) == len(imap)`이 성립한다.
    """
    chars: list[str] = []
    imap: list[int] = []
    for origin_index, ch in enumerate(text):
        if is_ignorable(ch):
            continue
        for jamo in decompose_jamo(ch):
            for folded in jamo.casefold():
                chars.append(folded)
                imap.append(origin_index)
    return "".join(chars), imap


def strip_for_compare(text: str) -> str:
    """CER 등 텍스트 비교용. 자모 분해 없이 공백·부호만 제거한다."""
    return "".join(ch for ch in to_nfc(text) if not is_ignorable(ch)).casefold()
