"""줄바꿈 조판.

가중치 상수는 임계값이 아니라 선호도다. 조판 규칙 사이의 우선순위를 정할 뿐이며,
실제 임계값(`max_chars_per_line`, `particles`)은 전부 config에서 온다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .config import Config
from .models import Cue

FORCED_BREAK = "forced_break"
TOO_LONG = "too_long"

_PENALTY_BOTTOM_HEAVY = 1  # 아래 줄이 더 길면 살짝 손해
_PENALTY_PARTICLE_HEAD = 100  # 조사로 줄이 시작하면 크게 손해
_PENALTY_OVER_WIDTH = 1000  # 줄 폭 초과는 사실상 금지


def starts_with_particle(text: str, cfg: Config) -> bool:
    """줄 머리가 조사 하나로 시작하는지.

    어절 전체가 조사일 때만 참이다. 접두 일치로 보면 `고양이`, `나는` 같은
    멀쩡한 단어가 전부 걸린다.
    """
    head = text.lstrip()
    if not head:
        return False
    first_word = head.split(" ", 1)[0].strip(".,!?…\"'”’)]}")
    return first_word in cfg.particles


def _breaks_particle(text: str, cfg: Config) -> bool:
    """어절 중간에서 끊겨 조사가 다음 줄 머리로 넘어간 경우. 강제 분리 전용."""
    head = text.lstrip()
    return any(head.startswith(particle) for particle in cfg.particles)


def _candidates(text: str) -> list[int]:
    return [index for index, ch in enumerate(text) if ch == " " and 0 < index < len(text) - 1]


def _score(text: str, position: int, cfg: Config) -> int:
    first = text[:position].strip()
    second = text[position:].strip()
    center = len(text) / 2
    penalty = int(abs(position - center))
    if len(first) < len(second):
        penalty += _PENALTY_BOTTOM_HEAVY
    if starts_with_particle(second, cfg):
        penalty += _PENALTY_PARTICLE_HEAD
    if len(first) > cfg.max_chars_per_line or len(second) > cfg.max_chars_per_line:
        penalty += _PENALTY_OVER_WIDTH
    return penalty


def break_text(text: str, cfg: Config) -> tuple[list[str], list[str]]:
    """한 큐의 텍스트를 줄로 나눈다. 반환값은 (줄 목록, 플래그)."""
    body = " ".join(text.split())
    if not body:
        return [""], []
    if len(body) <= cfg.max_chars_per_line:
        return [body], []

    positions = _candidates(body)
    if positions:
        best = min(positions, key=lambda position: _score(body, position, cfg))
        first = body[:best].strip()
        second = body[best:].strip()
        flags = []
        if len(first) > cfg.max_chars_per_line or len(second) > cfg.max_chars_per_line:
            flags.append(TOO_LONG)
        return [first, second], flags

    # 공백이 없는 긴 문자열. 자를 곳이 없으니 폭에서 끊고 표시를 남긴다.
    cut = _shift_off_particle(body, cfg.max_chars_per_line, cfg)
    return [body[:cut], body[cut:]], [FORCED_BREAK]


def _shift_off_particle(body: str, cut: int, cfg: Config) -> int:
    """강제 분리 지점 뒤가 조사로 시작하면 끊는 곳을 앞으로 당긴다.

    폭을 넘기지 않으려면 오른쪽이 아니라 왼쪽으로 옮겨야 한다. 최대 두 글자.
    """
    for shift in range(3):
        candidate = cut - shift
        if candidate <= 0:
            break
        if not _breaks_particle(body[candidate:], cfg):
            return candidate
    return cut


def break_lines(cues: Sequence[Cue], cfg: Config) -> list[Cue]:
    laid_out: list[Cue] = []
    for cue in cues:
        lines, flags = break_text(cue.text, cfg)
        updated = replace(cue, lines=tuple(lines))
        for flag in flags:
            updated = updated.with_flag(flag)
        laid_out.append(updated)
    return laid_out
