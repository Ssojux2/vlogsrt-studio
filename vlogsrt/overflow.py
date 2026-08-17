"""정렬이 무너진 자리에서 큐 하나가 텍스트를 통째로 삼키는 것을 막는다.

whisper가 어느 구간을 통째로 놓치면 그 구간의 정본 텍스트는 갈 곳이 없다.
`map_cuts`는 남은 글자를 다음 절단점까지 몰아주므로, 1초짜리 큐에 900자가 들어간다.
읽을 수 없는 자막이 되는 것으로 끝나지 않고, **멀쩡하던 이웃 큐까지** 밀려서
못 쓰게 된다(D16, 실측: 9분 공백이 4분치 정상 자막을 잡아먹었다).

넘치는 글자는 버린다. 버렸다는 사실과 양은 반드시 알린다 — 조용히 버리면
사람은 자막이 왜 비었는지 영영 모른다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from . import log
from .config import Config
from .models import Cue

OVERFLOW = "overflow_trimmed"


@dataclass(frozen=True, slots=True)
class Overflow:
    """한 큐에서 넘쳐 버려진 글자."""

    start: float
    end: float
    kept: int
    dropped: int


def limit(cue: Cue, cfg: Config) -> int:
    """이 큐가 담을 수 있는 최대 글자 수.

    조판 한도(`max_chars_per_cue`)와 읽기 속도(`max_cps`) 중 **큰 쪽**을 쓴다.
    긴 큐는 그만큼 더 담을 수 있어야 하고, 짧은 큐도 최소 한 줄은 담아야 한다.
    """
    by_layout = cfg.max_chars_per_cue
    by_speed = int(max(0.0, cue.end - cue.start) * cfg.max_cps)
    return int(max(by_layout, by_speed) * cfg.overflow_factor)


def _trim(text: str, budget: int) -> str:
    """어절 경계에서 자른다. 중간에 끊긴 단어를 남기지 않는다."""
    if len(text) <= budget:
        return text
    head = text[:budget]
    spaced = head.rsplit(" ", 1)[0]
    return (spaced if spaced else head).strip()


def cap(cues: Sequence[Cue], cfg: Config) -> tuple[list[Cue], list[Overflow]]:
    """넘치는 큐를 잘라내고 무엇을 얼마나 버렸는지 돌려준다."""
    trimmed: list[Cue] = []
    spills: list[Overflow] = []
    for cue in cues:
        budget = limit(cue, cfg)
        text = cue.text
        if len(text) <= budget:
            trimmed.append(cue)
            continue
        kept = _trim(text, budget)
        spills.append(
            Overflow(start=cue.start, end=cue.end, kept=len(kept), dropped=len(text) - len(kept))
        )
        trimmed.append(replace(cue, lines=(kept,)).with_flag(OVERFLOW))
    return trimmed, spills


def report(spills: Sequence[Overflow], cfg: Config) -> list[str]:
    """사람이 볼 요약. 넘친 자리는 whisper가 그 구간을 놓쳤다는 뜻이다."""
    del cfg
    if not spills:
        return []
    dropped = sum(spill.dropped for spill in spills)
    notices = [
        f"정렬이 무너진 큐 {len(spills)}개에서 정본 {dropped}자를 버렸다. "
        f"그 자리는 whisper가 발화를 놓쳐 얹을 시각이 없었다."
    ]
    for spill in sorted(spills, key=lambda item: -item.dropped)[:3]:
        notices.append(
            f"  {spill.start:.0f}~{spill.end:.0f}초 · {spill.kept}자 남기고 {spill.dropped}자 버림"
        )
    for notice in notices:
        log.warn(notice)
    return notices
