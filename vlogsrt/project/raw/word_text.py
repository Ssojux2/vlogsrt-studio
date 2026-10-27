"""Gemini 단어 → whisper 줄의 글자(W-35a, D95) — 시각은 whisper, 글자는 Gemini(순수).

같은 청크 WAV를 whisper와 Gemini 3.5 Transcribe가 따로 듣는다. 두 응답의 시각은 같은 청크 국소
시계다. 각 Gemini 단어의 **자리**는 가운데 시각((시작 + 끝) / 2)이고, 그 자리를 반열린 구간 `[줄
시작, 줄 끝)`에 담은 whisper 줄이 그 단어의 줄이다. 둘 이상이면(겹친 whisper 줄) 줄 가운데가 가장
가까운 줄, 같으면 앞 줄이다. 길이 0인 줄은 아무것도 담지 않는다.

단어 시각은 **어느 줄의 글자인지 정하는 데만** 쓴다 — 이 계획은 줄의 아이디·시작·끝·차례·수를 내지
않고 글자만 낸다(시각의 원천은 whisper `segments[]`, 규칙 1·3). 어느 줄도 담지 않은 단어(whisper가
놓친 말)와 시각 없는 단어는 어느 줄에도 붙이지 않고 앞뒤 줄 아이디와 함께 기록만 한다(D37과 같은
선). 단어가 없는 줄은 whisper 글자 그대로다 — 줄을 비우지 않는다(규칙 9).

줄 글자 = 배정된 단어의 `display`(해석기가 붙인 띄어쓰기·부호)를 응답 순서대로 이은 것의 앞뒤 공백만
정리한 것. 이 모듈은 전송·전사·다른 귀·서비스·DB·파일·조판 계층을
모른다(`check_word_text_stays_pure`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.raw.config import WORD_TEXT_VERSION
from vlogsrt.project.raw.gemini_transcribe_parse import ParsedWord

SOURCE_GEMINI = "gemini"
SOURCE_WHISPER = "whisper"


@dataclass(frozen=True, slots=True)
class TimedLine:
    """whisper 줄 하나 — 청크 국소 시계의 시작·끝과 whisper 글자."""

    line_id: str
    start: Fraction
    end: Fraction
    text: str


@dataclass(frozen=True, slots=True)
class LineText:
    """줄 하나의 글자 계획 — `words`는 배정된 단어의 응답 차례. 비면 whisper 글자 그대로다."""

    line_id: str
    text: str
    source: str
    words: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class UnplacedWord:
    """어느 줄에도 붙지 않은 단어 — 시각 없는 단어는 시각·앞뒤 줄이 None이다."""

    index: int
    text: str
    start: Fraction | None
    end: Fraction | None
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class WordTextPlan:
    version: str
    lines: tuple[LineText, ...]
    unplaced: tuple[UnplacedWord, ...]
    untimed: int


def place(lines: Sequence[TimedLine], words: Sequence[ParsedWord]) -> WordTextPlan:
    """whisper 줄(차례 그대로)과 Gemini 단어(응답 차례) → 줄마다 글자. 같은 입력이면 같은 결과다."""
    members: list[list[int]] = [[] for _ in lines]
    unplaced: list[UnplacedWord] = []
    for index, word in enumerate(words):
        if word.start is None or word.end is None:
            unplaced.append(UnplacedWord(index, word.text, None, None, None, None))
            continue
        middle = (word.start + word.end) / 2
        home = _home(lines, middle)
        if home is None:
            before, after = _neighbours(lines, middle)
            unplaced.append(UnplacedWord(index, word.text, word.start, word.end, before, after))
        else:
            members[home].append(index)
    return WordTextPlan(
        version=WORD_TEXT_VERSION,
        lines=tuple(_line_text(line, members[i], words) for i, line in enumerate(lines)),
        unplaced=tuple(unplaced),
        untimed=sum(1 for word in words if word.start is None or word.end is None),
    )


def _home(lines: Sequence[TimedLine], middle: Fraction) -> int | None:
    """자리를 담은 줄 — 겹치면 가운데가 가장 가까운 줄, 같으면 앞 줄(차례가 작은 쪽)."""
    holding = [i for i, line in enumerate(lines) if line.start <= middle < line.end]
    if not holding:
        return None
    return min(holding, key=lambda i: (abs((lines[i].start + lines[i].end) / 2 - middle), i))


def _neighbours(lines: Sequence[TimedLine], middle: Fraction) -> tuple[str | None, str | None]:
    """자리 앞에서 시작한 마지막 줄과 자리 뒤에서 시작하는 첫 줄의 아이디(시작 차례로)."""
    ordered = sorted(lines, key=lambda line: line.start)
    before = [line.line_id for line in ordered if line.start <= middle]
    after = [line.line_id for line in ordered if line.start > middle]
    return (before[-1] if before else None, after[0] if after else None)


def _line_text(line: TimedLine, members: list[int], words: Sequence[ParsedWord]) -> LineText:
    if not members:
        return LineText(line.line_id, line.text, SOURCE_WHISPER, ())
    text = "".join(words[index].display for index in members).strip()
    return LineText(line.line_id, text, SOURCE_GEMINI, tuple(members))


__all__ = [
    "SOURCE_GEMINI",
    "SOURCE_WHISPER",
    "LineText",
    "TimedLine",
    "UnplacedWord",
    "WordTextPlan",
    "place",
]
