"""Gemini 단어 → 자막 줄(W-31b, D92) — 큐 경계는 엔진이 준 단어 경계에서만 고른다(순수).

큐 시작 = 그 큐 첫 시각 단어의 시작, 큐 끝 = 그 큐 시각 단어 끝의 최대값이다 — 정확 유리수 그대로,
반올림·보간·배분 없음(규칙 1, D92). 프로그램이 고르는 것은 어느 단어 경계에서 끊을지 하나뿐이다
(규칙 4의 자를 자리 고르기와 같은 종류). 단어를 쪼개지 않고, 단어를 버리지 않는다(규칙 9).

끊는 자리(`WordCueSettings`): 문장 끝 부호로 끝난 단어 뒤 · 앞 시각 단어 끝과의 쉼이 한도 이상인
시각 단어 앞 · 글자·길이 한도를 넘기 전. 끊기는 시각 단어 앞에서만, 그리고 지금 큐에 시각 단어가
있을 때만 한다 — 그래서 모든 큐에 시각 단어가 적어도 하나 있다. 시각 없는 단어는 지금 큐에 글자째
남고 세어진다(시각을 주지 않는다). 응답 순서를 바꾸지 않는다 — 앞 단어보다 일찍 시작한 단어는 세기만
한다(겹침은 편집기·시각 확인이 본다).

이 모듈은 전송·전사·다른 귀 글자·서비스·DB·파일을 모른다 — 다른 귀의 글자가 경계에 끼지 못한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from vlogsrt.project.raw.config import WORD_CUE_SETTINGS, WordCueSettings
from vlogsrt.project.raw.gemini_transcribe_parse import FALLBACK, ParsedWord

NOTE_UNTIMED = "untimed_words"
NOTE_UNPLACED = "unplaced_chars"
NOTE_UNORDERED = "out_of_order"
NOTE_FALLBACK = "fallback_alignment"
WORD_NOTES = (NOTE_UNTIMED, NOTE_UNPLACED, NOTE_UNORDERED, NOTE_FALLBACK)
"""줄마다 센 것의 이름(W-52b1) — 「확인 필요」 표시의 재료일 뿐 줄·시각·글자를 바꾸지 않는다."""


@dataclass(frozen=True, slots=True)
class WordCue:
    """자막 줄 하나 — 시각은 청크 국소 초(엔진 단어 시각 그대로), 글은 단어 표시를 이은 것.

    `dropped_chars`는 이 줄 단어 사이(뒤)에서 싣지 못한 자리 없는 글자 수, `out_of_order`는 이
    줄에서 앞 단어보다 일찍 시작한 단어 수다 — 센 것일 뿐 글자·시각에 들지 않는다."""

    start: Fraction
    end: Fraction
    text: str
    words: int
    untimed_words: int
    dropped_chars: int = 0
    out_of_order: int = 0


@dataclass(frozen=True, slots=True)
class CueGroup:
    cues: tuple[WordCue, ...]
    untimed_words: int = 0
    out_of_order: int = 0


@dataclass(slots=True)
class _Open:
    words: list[ParsedWord] = field(default_factory=list)
    out_of_order: int = 0

    @property
    def timed(self) -> list[ParsedWord]:
        return [word for word in self.words if word.timed]

    def text(self, extra: ParsedWord | None = None) -> str:
        shown = self.words if extra is None else [*self.words, extra]
        return "".join(word.display for word in shown).strip()


def group(words: Sequence[ParsedWord], settings: WordCueSettings = WORD_CUE_SETTINGS) -> CueGroup:
    """단어(응답 순서) → 자막 줄. 같은 입력이면 같은 결과다."""
    cues: list[WordCue] = []
    current = _Open()
    out_of_order = 0
    last_start: Fraction | None = None
    for word in words:
        late = False
        if word.timed:
            assert word.start is not None
            late = last_start is not None and word.start < last_start
            last_start = word.start
            if current.timed and _breaks_before(current, word, settings):
                cues.append(_closed(current))
                current = _Open()
        out_of_order += late
        current.out_of_order += late
        current.words.append(word)
    if current.words:
        cues.append(_closed(current))
    untimed = sum(not word.timed for word in words)
    return CueGroup(cues=tuple(cues), untimed_words=untimed, out_of_order=out_of_order)


def _breaks_before(current: _Open, word: ParsedWord, settings: WordCueSettings) -> bool:
    """시각 단어 `word` 앞에서 끊는가 — 문장 끝 · 쉼 · 글자 한도 · 길이 한도."""
    assert word.start is not None and word.end is not None
    timed = current.timed
    previous = current.words[-1].display.strip()
    if previous.endswith(settings.sentence_end_marks):
        return True
    last_end = max(item.end for item in timed if item.end is not None)
    if word.start - last_end >= settings.pause_break_seconds:
        return True
    if len(current.text(word)) > settings.max_cue_chars:
        return True
    first_start = timed[0].start
    assert first_start is not None
    return word.end - first_start > settings.max_cue_seconds


def _closed(current: _Open) -> WordCue:
    timed = current.timed
    if not timed:
        # 끊기는 시각 단어가 있는 큐에서만 하므로, 여기 오는 것은 시각 단어가 하나도 없는 입력뿐이다
        raise ValueError("시각 단어가 하나도 없다 — 시각 없는 글자로 자막 줄을 만들지 않는다 (D92)")
    start, end = timed[0].start, max(word.end for word in timed if word.end is not None)
    assert start is not None
    return WordCue(
        start=start,
        end=end,
        text=current.text(),
        words=len(current.words),
        untimed_words=len(current.words) - len(timed),
        dropped_chars=sum(word.dropped_after for word in current.words),
        out_of_order=current.out_of_order,
    )


def word_notes(group: CueGroup, alignment: str) -> tuple[tuple[int, str], ...]:
    """(줄 위치, 센 것의 이름) — 센 것이 있는 줄만, 줄 순서·`WORD_NOTES` 순서대로.

    정렬에 실패한 응답(`FALLBACK`)은 그 조각의 모든 줄이 「띄어 이었다」를 받는다(글자를 단어만으로
    이었으므로)."""
    notes: list[tuple[int, str]] = []
    for index, cue in enumerate(group.cues):
        counted = (cue.untimed_words, cue.dropped_chars, cue.out_of_order, alignment == FALLBACK)
        notes.extend(
            (index, name) for name, count in zip(WORD_NOTES, counted, strict=True) if count
        )
    return tuple(notes)
