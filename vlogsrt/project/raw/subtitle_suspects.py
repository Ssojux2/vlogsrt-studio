"""원본 자막의 무료 「확인 필요」 표시(D59 19e) — 값만 받아 큐마다 이유를 돌려준다. **표시만** 한다.

규칙은 일곱이다. 기준은 `SubtitleSuspectSettings`(설정 밖 — 어느 지문에도 없다)에서 온다.

- **같은 말이 이어진다**: 접은 글자(공백·부호·대소문자, `hangul.strip_for_compare` —
  retext·교차 확인과 같은 접기)가 같은 큐가 `repeat_run_min`개 이상 잇따른다. whisper의
  메아리(「개좋아」×35)를 짚는다.
- **영상 끝 인사말**: 설정의 인사말을 접은 글자가 큐 안에 들어 있다(whisper가 무음에서
  지어내곤 한다).
- **다른 문자**: 이름에 허용 낱말(한글·라틴)이 없는 글자가 섞였다(키릴 `усhod`, 일본어
  `水が少し`).
- **말소리 측정상 없음**: 자막 시각 확인(D55)이 짚은 큐 — 부른 쪽이 **값으로** 넘긴다. 이
  모듈은 확인 계층을 모른다(`check_invariants`).

시각 표시 셋(W-25f, D76)은 1단계 기록의 재료를 부른 쪽이 `CueTiming`으로 **값으로** 넘길 때만 돈다 —
없으면 위 넷 그대로다(등급의 「자막 확인됨」 몫, D65는 이 재료 없이 센다).

- **조각 끝 잘림**: whisper 세그먼트가 전사 조각 끝을 넘어 조각 끝에 맞춘 줄(D55) — 말이 조각
  경계에서 잘렸을 수 있다.
- **정수 초**: 시작·끝이 **둘 다** 전사 조각 시계의 정수 초다 — 온도 0 시절 whisper의 거친 시각
  (W-25 실측 경계의 79%)이라 어긋났을 수 있다.
- **낮은 확신**: whisper 세그먼트의 `avg_logprob`가 기준보다 낮거나 `no_speech_prob`가 기준 이상이다
  (whisper 레퍼런스 기본값). 「말소리를 못 찾음」과 겹쳐 무음 확률이 높으면 그 표시 하나가 더 센
  말로 바뀐다.

이 모듈은 「이 줄은 말이 아니다」라고 판정하지 않는다(규칙 4). 큐를 지우거나 숨기거나 고치지
않고 이유만 돌려준다(규칙 9) — 사람이 듣고 정한다. 「치즈」×9도 표시될 뿐이다. 입출력·프로세스·
서비스·확인 계층을 모른다(`check_invariants`의 「자막 의심 표시는 순수하고 표시만 한다」).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

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
CHUNK_END_CLAMPED = "chunk_end_clamped"
WHOLE_SECOND = "whole_second"
LOW_CONFIDENCE = "low_confidence"

BOILERPLATE_TEXT = "영상 끝 인사말이 끼었을 수 있습니다"
NO_SPEECH_TEXT = "말소리를 못 찾음 (측정 한계일 수 있음)"
"""D55의 문구 그대로 — 「말이 아니다」라고 말하지 않는다."""
NO_SPEECH_STRONG_TEXT = (
    "말소리를 못 찾음 — 전사도 무음일 확률을 높게 봤습니다 (측정 한계일 수 있음)"
)
"""「말소리를 못 찾음」과 높은 무음 확률이 겹친 줄(D76) — 둘을 따로 달지 않고 더 센 표시 하나로."""
CHUNK_END_CLAMPED_TEXT = "전사 조각 끝에서 잘렸을 수 있습니다"
WHOLE_SECOND_TEXT = "시작·끝이 정수 초입니다 — 시각이 거칠 수 있습니다"
LOW_LOGPROB_TEXT = "전사 확신이 낮습니다"
HIGH_NO_SPEECH_TEXT = "전사가 무음일 확률을 높게 봤습니다 (측정 한계일 수 있음)"

MODIFIER_LETTER = "Lm"
"""유니코드 수식 글자 범주 — 발음·생략 표시(`ʼ`·`ˈ`)라 다른 문자로 보지 않는다."""

SHOWN_LETTERS = 3
"""「다른 문자가 섞였습니다(…)」 괄호에 이름 대는 글자 수 — 문구의 길이일 뿐 판정이 아니다."""


@dataclass(frozen=True)
class CueTiming:
    """시각 표시 셋의 재료(W-25f, D76) — 큐 아이디마다 값. 부른 쪽(서비스)이 1단계 기록에서 만든다.

    `local_times`는 그 줄을 낸 전사 조각의 시계(조각 시작 = 0)의 (시작, 끝), `scores`는 whisper
    세그먼트의 (no_speech_prob, avg_logprob)이다(없는 값은 None)."""

    clamped: frozenset[str] = frozenset()
    local_times: Mapping[str, tuple[Fraction, Fraction]] = field(default_factory=dict)
    scores: Mapping[str, tuple[float | None, float | None]] = field(default_factory=dict)


@dataclass(frozen=True)
class Suspect:
    """큐 하나의 「확인 필요」 이유 — 규칙 이름과 사람이 읽는 말."""

    code: str
    text: str


def suspects(
    cues: Sequence[tuple[str, str]],
    no_speech: Collection[str] = (),
    settings: SubtitleSuspectSettings = SUBTITLE_SUSPECT_SETTINGS,
    timing: CueTiming | None = None,
) -> dict[str, tuple[Suspect, ...]]:
    """`(큐 아이디, 글자)`마다 이유 — 이유가 있는 큐만, 큐 순서대로, 규칙 순서대로 싣는다.

    `no_speech`는 말소리 측정이 짚은 큐 아이디, `timing`은 시각 표시 셋의 재료다(값, 없으면 그
    셋을 돌리지 않는다). 입력을 바꾸지 않는다.
    """
    problems = subtitle_suspect_problems(settings)
    if problems:
        raise ValueError("확인 필요 표시 설정이 계약을 어긴다: " + "; ".join(problems))
    timing = timing or CueTiming()
    loud = _high_no_speech(timing, settings)
    found: dict[str, list[Suspect]] = {}
    for cue_id, reason in (
        *_repeats(cues, settings.repeat_run_min),
        *_boilerplates(cues, settings.boilerplate_phrases),
        *_other_scripts(cues, settings),
        *_silences(cues, no_speech, loud),
        *_clamped(cues, timing.clamped),
        *_whole_seconds(cues, timing.local_times),
        *_low_confidence(cues, timing, settings, frozenset(no_speech) & loud),
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
    cues: Sequence[tuple[str, str]], no_speech: Collection[str], loud: frozenset[str] = frozenset()
) -> Iterator[tuple[str, Suspect]]:
    """말소리 측정상 없음 — 전사도 무음 확률을 높게 본 줄(`loud`)이면 더 센 말 하나로."""
    silent = frozenset(no_speech)
    for cue_id, _text in cues:
        if cue_id in silent:
            text = NO_SPEECH_STRONG_TEXT if cue_id in loud else NO_SPEECH_TEXT
            yield cue_id, Suspect(NO_SPEECH, text)


def _high_no_speech(timing: CueTiming, settings: SubtitleSuspectSettings) -> frozenset[str]:
    return frozenset(
        cue_id
        for cue_id, (no_speech, _logprob) in timing.scores.items()
        if no_speech is not None and no_speech >= settings.low_confidence_no_speech_prob
    )


def _clamped(
    cues: Sequence[tuple[str, str]], clamped: frozenset[str]
) -> Iterator[tuple[str, Suspect]]:
    for cue_id, _text in cues:
        if cue_id in clamped:
            yield cue_id, Suspect(CHUNK_END_CLAMPED, CHUNK_END_CLAMPED_TEXT)


def _whole_seconds(
    cues: Sequence[tuple[str, str]], local_times: Mapping[str, tuple[Fraction, Fraction]]
) -> Iterator[tuple[str, Suspect]]:
    """시작·끝이 둘 다 조각 시계의 정수 초 — 한쪽만이면 우연일 수 있어 달지 않는다."""
    for cue_id, _text in cues:
        edges = local_times.get(cue_id)
        if edges is not None and all(edge.denominator == 1 for edge in edges):
            yield cue_id, Suspect(WHOLE_SECOND, WHOLE_SECOND_TEXT)


def _low_confidence(
    cues: Sequence[tuple[str, str]],
    timing: CueTiming,
    settings: SubtitleSuspectSettings,
    merged: frozenset[str],
) -> Iterator[tuple[str, Suspect]]:
    """확신이 낮다 · 무음 확률이 높다 — 무음 확률은 「말소리를 못 찾음」에 합친 줄(`merged`)이면
    뺀다."""
    for cue_id, _text in cues:
        no_speech, logprob = timing.scores.get(cue_id, (None, None))
        parts = []
        if logprob is not None and logprob < settings.low_confidence_avg_logprob:
            parts.append(LOW_LOGPROB_TEXT)
        loud = no_speech is not None and no_speech >= settings.low_confidence_no_speech_prob
        if loud and cue_id not in merged:
            parts.append(HIGH_NO_SPEECH_TEXT)
        if parts:
            yield cue_id, Suspect(LOW_CONFIDENCE, " · ".join(parts))


__all__ = [
    "BOILERPLATE",
    "BOILERPLATE_TEXT",
    "CHUNK_END_CLAMPED",
    "CHUNK_END_CLAMPED_TEXT",
    "HIGH_NO_SPEECH_TEXT",
    "LOW_CONFIDENCE",
    "LOW_LOGPROB_TEXT",
    "NO_SPEECH",
    "NO_SPEECH_STRONG_TEXT",
    "NO_SPEECH_TEXT",
    "OTHER_SCRIPT",
    "REPEAT",
    "WHOLE_SECOND",
    "WHOLE_SECOND_TEXT",
    "CueTiming",
    "Suspect",
    "suspects",
]
