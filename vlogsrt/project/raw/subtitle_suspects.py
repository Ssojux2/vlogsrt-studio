"""원본 자막의 무료 「확인 필요」 표시(D59 19e) — 값만 받아 큐마다 이유를 돌려준다. **표시만** 한다.

규칙은 넷이다. 기준은 `SubtitleSuspectSettings`(설정 밖 — 어느 지문에도 없다)에서 온다.

- **같은 말이 이어진다**: 접은 글자(공백·부호·대소문자, `hangul.strip_for_compare` —
  retext·교차 확인과 같은 접기)가 같은 큐가 `repeat_run_min`개 이상 잇따른다. whisper의
  메아리(「개좋아」×35)를 짚는다.
- **영상 끝 인사말**: 설정의 인사말을 접은 글자가 큐 안에 들어 있다(whisper가 무음에서
  지어내곤 한다).
- **다른 문자**: 이름에 허용 낱말(한글·라틴)이 없는 글자가 섞였다(키릴 `усhod`, 일본어
  `水が少し`).
- **말소리 측정상 없음**: 자막 시각 확인(D55)이 짚은 큐 — 부른 쪽이 **값으로** 넘긴다. 이
  모듈은 확인 계층을 모른다(`check_invariants`).

이 모듈은 「이 줄은 말이 아니다」라고 판정하지 않는다(규칙 4). 큐를 지우거나 숨기거나 고치지
않고 이유만 돌려준다(규칙 9) — 사람이 듣고 정한다. 「치즈」×9도 표시될 뿐이다. 입출력·프로세스·
서비스·확인 계층을 모른다(`check_invariants`의 「자막 의심 표시는 순수하고 표시만 한다」).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass

from vlogsrt.hangul import strip_for_compare, to_nfc
from vlogsrt.project.raw.config import (
    SUBTITLE_SUSPECT_SETTINGS,
    SubtitleSuspectSettings,
    subtitle_suspect_problems,
)

REPEAT = "repeat"
BOILERPLATE = "boilerplate"
OTHER_SCRIPT = "other_script"
NO_SPEECH = "no_speech"

BOILERPLATE_TEXT = "영상 끝 인사말이 끼었을 수 있습니다"
NO_SPEECH_TEXT = "말소리를 못 찾음 (측정 한계일 수 있음)"
"""D55의 문구 그대로 — 「말이 아니다」라고 말하지 않는다."""

MODIFIER_LETTER = "Lm"
"""유니코드 수식 글자 범주 — 발음·생략 표시(`ʼ`·`ˈ`)라 다른 문자로 보지 않는다."""

SHOWN_LETTERS = 3
"""「다른 문자가 섞였습니다(…)」 괄호에 이름 대는 글자 수 — 문구의 길이일 뿐 판정이 아니다."""


@dataclass(frozen=True)
class Suspect:
    """큐 하나의 「확인 필요」 이유 — 규칙 이름과 사람이 읽는 말."""

    code: str
    text: str


def suspects(
    cues: Sequence[tuple[str, str]],
    no_speech: Collection[str] = (),
    settings: SubtitleSuspectSettings = SUBTITLE_SUSPECT_SETTINGS,
) -> dict[str, tuple[Suspect, ...]]:
    """`(큐 아이디, 글자)`마다 이유 — 이유가 있는 큐만, 큐 순서대로, 규칙 순서대로 싣는다.

    `no_speech`는 말소리 측정이 짚은 큐 아이디다(값). 입력을 바꾸지 않는다.
    """
    problems = subtitle_suspect_problems(settings)
    if problems:
        raise ValueError("확인 필요 표시 설정이 계약을 어긴다: " + "; ".join(problems))
    found: dict[str, list[Suspect]] = {}
    for cue_id, reason in (
        *_repeats(cues, settings.repeat_run_min),
        *_boilerplates(cues, settings.boilerplate_phrases),
        *_other_scripts(cues, settings),
        *_silences(cues, no_speech),
    ):
        found.setdefault(cue_id, []).append(reason)
    return {cue_id: tuple(found[cue_id]) for cue_id, _text in cues if cue_id in found}


def _repeats(cues: Sequence[tuple[str, str]], run_min: int) -> Iterator[tuple[str, Suspect]]:
    """접은 글자가 같은 큐가 잇따른 런 — 길이가 문턱 이상이면 런의 모든 큐에 그 길이를 단다.

    접어서 비는 줄(부호뿐)은 비교할 글자가 없으니 런을 만들지도 잇지도 않는다.
    """
    folded = [strip_for_compare(text) for _cue_id, text in cues]
    start = 0
    while start < len(cues):
        end = start + 1
        while folded[start] and end < len(cues) and folded[end] == folded[start]:
            end += 1
        if folded[start] and end - start >= run_min:
            reason = Suspect(REPEAT, f"같은 말이 {end - start}번 이어집니다")
            yield from ((cue_id, reason) for cue_id, _text in cues[start:end])
        start = end


def _boilerplates(
    cues: Sequence[tuple[str, str]], phrases: Sequence[str]
) -> Iterator[tuple[str, Suspect]]:
    folded_phrases = tuple(strip_for_compare(phrase) for phrase in phrases)
    for cue_id, text in cues:
        folded = strip_for_compare(text)
        if any(phrase in folded for phrase in folded_phrases):
            yield cue_id, Suspect(BOILERPLATE, BOILERPLATE_TEXT)


def _other_scripts(
    cues: Sequence[tuple[str, str]], settings: SubtitleSuspectSettings
) -> Iterator[tuple[str, Suspect]]:
    for cue_id, text in cues:
        letters = _foreign_letters(text, settings)
        if letters:
            named = "·".join(letters[:SHOWN_LETTERS])
            yield cue_id, Suspect(OTHER_SCRIPT, f"다른 문자가 섞였습니다({named})")


def _foreign_letters(text: str, settings: SubtitleSuspectSettings) -> list[str]:
    """허용 낱말이 이름에 없는 글자(L 범주) — 처음 나온 순서로 한 번씩.

    숫자·부호·기호는 보지 않는다 — 다른 **글자**가 섞였는지만 본다. 수식 글자(Lm — `ʼ`·`ˈ`)는
    발음·생략 표시라 보지 않고, 설정의 허용 글자와 NFKC로 허용 낱말이 되는 호환 글자는 받는다.
    """
    seen: list[str] = []
    for char in to_nfc(text):
        category = unicodedata.category(char)
        if not category.startswith("L") or category == MODIFIER_LETTER or char in seen:
            continue
        if char in settings.allowed_letters:
            continue
        if not _named_allowed(char, settings.allowed_letter_scripts):
            seen.append(char)
    return seen


def _named_allowed(char: str, allowed: Sequence[str]) -> bool:
    """글자 이름(또는 NFKC로 접은 글자들의 이름)에 허용 낱말이 있는가 — `º`→`o`, `ℓ`→`l`."""
    if any(script in unicodedata.name(char, "") for script in allowed):
        return True
    folded = unicodedata.normalize("NFKC", char)
    letters = [part for part in folded if unicodedata.category(part).startswith("L")]
    return (
        folded != char
        and bool(letters)
        and all(any(script in unicodedata.name(part, "") for script in allowed) for part in letters)
    )


def _silences(
    cues: Sequence[tuple[str, str]], no_speech: Collection[str]
) -> Iterator[tuple[str, Suspect]]:
    silent = frozenset(no_speech)
    for cue_id, _text in cues:
        if cue_id in silent:
            yield cue_id, Suspect(NO_SPEECH, NO_SPEECH_TEXT)


__all__ = [
    "BOILERPLATE",
    "BOILERPLATE_TEXT",
    "NO_SPEECH",
    "NO_SPEECH_TEXT",
    "OTHER_SCRIPT",
    "REPEAT",
    "Suspect",
    "suspects",
]
