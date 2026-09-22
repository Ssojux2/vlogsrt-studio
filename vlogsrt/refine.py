"""정제 엔진 — 자르고, 합치고, 시간을 다듬는다.

순서가 중요하다: 정리 → 분할 → 병합 → 시간 보정 → 충돌 해소 → 줄바꿈.
병합을 분할보다 먼저 돌리면 둘이 서로를 되돌린다. 정리가 맨 앞인 것도 같은 이유다 —
부호를 지우면 글자 수가 줄어 분할 판정(`max_chars_per_cue`·`max_cps`)이 바뀐다.

이 단계는 큐를 **버리지 않는다.** 들어온 글자는 전부 나간다(규칙 9).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from . import log
from .config import Config
from .layout import FORCED_BREAK, break_lines, break_text
from .models import ORIGIN_MERGE, ORIGIN_SPLIT, Cue
from .polish import POLISHED, polish_pass

__all__ = [
    "FORCED_BREAK",
    "NO_SPLIT_POINT",
    "POLISHED",
    "adjust_timing",
    "break_lines",
    "break_text",
    "merge_pass",
    "polish_pass",
    "refine",
    "resolve_overlaps",
    "split_cue",
    "split_pass",
]

NO_SPLIT_POINT = "no_split_point"


# ─────────────────────────────────────────────────────────────
#  분할
# ─────────────────────────────────────────────────────────────


def needs_split(cue: Cue, cfg: Config) -> bool:
    return (
        len(cue.text) > cfg.max_chars_per_cue
        or cue.cps > cfg.max_cps
        or cue.duration > cfg.max_duration
    )


def _char_split_index(text: str, ratio: float) -> int | None:
    """비율에 가장 가까운 어절 경계. 공백이 없으면 같은 비율의 글자 자리다(D38).

    공백이 없다고 포기하면 덩어리 전체가 한 줄로 화면 밖으로 나갔다(S12). 어절이
    없을 뿐 글자는 있으니 같은 비율의 글자 중간에서 자른다 — 시각은 어절 경계에서
    잘랐을 때와 같은 식으로 글자 수에 비례한다.
    """
    target = len(text) * ratio
    spaces = [index for index, ch in enumerate(text) if ch == " " and 0 < index < len(text) - 1]
    if spaces:
        return min(spaces, key=lambda index: abs(index - target))
    if len(text) < 2:
        return None
    return min(range(1, len(text)), key=lambda index: abs(index - target))


def split_cue(cue: Cue, cfg: Config, depth: int = 0) -> list[Cue]:
    """자막으로 너무 길거나 빽빽한 큐를 자른다. 시각은 원본 큐 안에서만 움직인다.

    자르는 지점은 글자 수 비례다. 큐 하나는 발화 구간 하나이고 그 안에는 더 쪼갤
    무음이 없으므로(D24), 여기서 기댈 것은 글자 수뿐이다. 말이 고르지 않으면 분할
    지점이 실제 발화보다 앞뒤로 밀릴 수 있다 — 다만 원본 큐 범위는 벗어나지 않는다.
    """
    if not needs_split(cue, cfg) or depth >= cfg.max_split_depth:
        return [cue]
    if cue.duration <= cfg.min_duration * 2:
        return [cue]

    text = cue.text
    index = _char_split_index(text, 0.5)
    if index is None:
        return [cue.with_flag(NO_SPLIT_POINT)]
    boundary = cue.start + cue.duration * (index / len(text))
    first_end = boundary - cfg.min_gap / 2
    second_start = boundary + cfg.min_gap / 2

    first_text = text[:index].strip()
    second_text = text[index:].strip()
    if not first_text or not second_text:
        return [cue.with_flag(NO_SPLIT_POINT)]

    first_end = min(max(first_end, cue.start + cfg.min_gap), cue.end - cfg.min_gap)
    second_start = min(max(second_start, first_end + cfg.min_gap), cue.end)

    first = replace(cue, end=first_end, lines=(first_text,), origin=ORIGIN_SPLIT)
    second = replace(cue, start=second_start, lines=(second_text,), origin=ORIGIN_SPLIT)
    return [
        *split_cue(first, cfg, depth + 1),
        *split_cue(second, cfg, depth + 1),
    ]


def split_pass(cues: Sequence[Cue], cfg: Config) -> list[Cue]:
    result: list[Cue] = []
    for cue in cues:
        result.extend(split_cue(cue, cfg))
    return result


# ─────────────────────────────────────────────────────────────
#  병합
# ─────────────────────────────────────────────────────────────


def _merged(first: Cue, second: Cue) -> Cue:
    text = f"{first.text} {second.text}".strip()
    return replace(
        first,
        end=second.end,
        lines=(text,),
        seg_ids=tuple(dict.fromkeys((*first.seg_ids, *second.seg_ids))),
        origin=ORIGIN_MERGE,
        flags=tuple(dict.fromkeys((*first.flags, *second.flags))),
    )


def can_merge(first: Cue, second: Cue, cfg: Config) -> bool:
    """합칠 이유가 둘이다: 깜빡이는 큐를 없애거나, 한 줄을 채우거나(D31).

    이유가 다르면 허용 폭도 다르다. 깜빡임은 `min_duration`에 못 미치는 큐를 살리는
    일이라 두 줄(`max_chars_per_cue`)까지 허용하고, 한 줄 채우기는 말 그대로 한 줄
    (`max_chars_per_line`)에서 멈춘다. 넘어가면 두 장으로 둘 이유가 있다는 뜻이다.
    """
    if second.start - first.end >= cfg.merge_gap:
        return False
    candidate = _merged(first, second)
    # **분할을 되돌리면 안 된다.** 길이 때문에 자른 큐를 도로 붙이면 둘이 서로를
    # 되돌리고, 시간 보정이 끝을 잘라내 뒷말이 화면에서 사라진다.
    if candidate.duration > cfg.max_duration:
        return False
    # 연쇄 병합에서는 매번 다시 계산해야 한다. 한 번 구한 CPS를 재사용하면 틀린다.
    if candidate.cps > cfg.max_cps:
        return False
    limit = cfg.max_chars_per_cue if first.duration < cfg.min_duration else cfg.max_chars_per_line
    return len(candidate.text) <= limit


def merge_pass(cues: Sequence[Cue], cfg: Config) -> list[Cue]:
    """짧은 큐를 이웃과 합쳐 한 줄을 채운다. 브이로그는 감탄사가 독립 세그먼트로 나와
    자막이 깜빡이고, 그 조각들이 한 줄을 다 못 채운 채 따로 선다(D31)."""
    if not cues:
        return []
    result: list[Cue] = [cues[0]]
    for cue in cues[1:]:
        if can_merge(result[-1], cue, cfg):
            result[-1] = _merged(result[-1], cue)
        else:
            result.append(cue)
    return result


# ─────────────────────────────────────────────────────────────
#  시간 보정
# ─────────────────────────────────────────────────────────────


def _lead_in_room(cues: Sequence[Cue], index: int, cfg: Config) -> float:
    """앞 큐와의 실제 무음. 첫 큐 앞은 0초부터가 전부 무음이다."""
    if index == 0:
        return cues[0].start
    return max(0.0, cues[index].start - cues[index - 1].end - cfg.min_gap)


def _lead_out_room(cues: Sequence[Cue], index: int, cfg: Config, duration: float) -> float:
    """뒤 큐가 `lead_in`으로 가져갈 몫과 `min_gap`을 빼고 **남은** 무음.

    말이 시작되는 시각은 사실이고 꼬리 연장은 편의다. 무음이 모자라면 `lead_in`이 먼저
    가져간다 — 그래야 없는 무음을 만들어 뒤 큐의 시작을 미는 일이 생기지 않는다.
    """
    if index + 1 >= len(cues):
        return max(0.0, duration - cues[index].end) if duration > 0 else cfg.lead_out
    silence = max(0.0, cues[index + 1].start - cues[index].end)
    taken = min(cfg.lead_in, max(0.0, silence - cfg.min_gap))
    return max(0.0, silence - taken - cfg.min_gap)


def _stretch_ceiling(cues: Sequence[Cue], index: int, cfg: Config, duration: float) -> float | None:
    """`min_duration`으로 늘릴 때의 천장. `_restore_min_duration`이 쓰는 것과 같은 선이다."""
    if index + 1 >= len(cues):
        return duration if duration > 0 else None
    return cues[index + 1].start - cfg.min_gap


def adjust_timing(cues: Sequence[Cue], cfg: Config, duration: float = 0.0) -> list[Cue]:
    """lead_in/lead_out은 **실제로 있는 무음만** 먹는다. 그다음이 길이 클램프다.

    없는 무음을 만들면 `resolve_overlaps`가 그 가짜 겹침을 중점에서 갈라 뒤 큐의 시작을
    **뒤로** 민다 — whisper가 세그먼트를 간격 0으로 붙여 내놓는 구간에서 큐 17%가
    `+0.14초` 늦었다(D33). `min_duration` 확장도 같은 이유로 다음 큐를 넘지 않는다.
    """
    adjusted: list[Cue] = []
    for index, cue in enumerate(cues):
        start = max(0.0, cue.start - min(cfg.lead_in, _lead_in_room(cues, index, cfg)))
        end = cue.end + min(cfg.lead_out, _lead_out_room(cues, index, cfg, duration))
        if duration > 0:
            end = min(end, duration)
        if end - start < cfg.min_duration:
            ceiling = _stretch_ceiling(cues, index, cfg, duration)
            wanted = start + cfg.min_duration
            end = max(cue.end, wanted if ceiling is None else min(wanted, ceiling))
            if duration > 0 and end > duration:
                end = duration
                start = max(0.0, end - cfg.min_duration)
        if end - start > cfg.max_duration:
            end = start + cfg.max_duration
        adjusted.append(replace(cue, start=start, end=end))
    return adjusted


def resolve_overlaps(cues: Sequence[Cue], cfg: Config) -> list[Cue]:
    """`min_gap`을 확보한다. 보정 후 모든 큐는 단조 증가한다."""
    if not cues:
        return []
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    result = [ordered[0]]
    for cue in ordered[1:]:
        previous = result[-1]
        gap = cue.start - previous.end
        if gap >= cfg.min_gap:
            result.append(cue)
            continue
        if cue.start < previous.end:
            midpoint = (previous.end + cue.start) / 2
            new_previous_end = midpoint - cfg.min_gap / 2
            new_start = midpoint + cfg.min_gap / 2
        else:
            new_previous_end = cue.start - cfg.min_gap
            new_start = cue.start
        if new_previous_end <= previous.start:
            new_previous_end = previous.start + cfg.min_gap
            new_start = max(new_start, new_previous_end + cfg.min_gap)
        result[-1] = replace(previous, end=new_previous_end)
        result.append(replace(cue, start=min(new_start, cue.end)))
    return _restore_min_duration(result, cfg)


def _restore_min_duration(cues: list[Cue], cfg: Config) -> list[Cue]:
    """충돌 해소로 짧아진 큐를 뒤쪽 여유만큼 늘린다. 여유가 없으면 그대로 둔다."""
    result = list(cues)
    for index, cue in enumerate(result):
        if cue.duration >= cfg.min_duration:
            continue
        ceiling = result[index + 1].start - cfg.min_gap if index + 1 < len(result) else None
        wanted = cue.start + cfg.min_duration
        new_end = wanted if ceiling is None else min(wanted, ceiling)
        if new_end > cue.end:
            result[index] = replace(cue, end=new_end)
    return result


def drop_empty(cues: Sequence[Cue]) -> list[Cue]:
    """빈 큐가 생겼다면 정제 어딘가가 글자를 잃었다는 뜻이다. **조용히** 버리지 않는다.

    골격에는 글자 없는 세그먼트가 들어오지 않고(`align.allocate`), 분할도 양쪽이
    비면 자르지 않는다. 그래도 나왔다면 버그다 — 멈추지는 않되(규칙 9) 어느
    세그먼트에서 났는지는 반드시 남긴다.
    """
    kept = [cue for cue in cues if cue.text.strip()]
    lost = [cue.seg_ids for cue in cues if not cue.text.strip()]
    if lost:
        log.warn(f"글자가 없는 큐 {len(lost)}개를 버렸다 (세그먼트 {lost}). 정제 버그다.")
    return kept


def renumber(cues: Sequence[Cue]) -> list[Cue]:
    return [replace(cue, index=position) for position, cue in enumerate(cues, start=1)]


def refine(cues: Sequence[Cue], cfg: Config, duration: float = 0.0) -> list[Cue]:
    """정제 전 과정. `redo`가 부르는 것도 이 함수다."""
    working = polish_pass(cues, cfg)
    touched = sum(1 for cue in working if POLISHED in cue.flags)
    if touched:
        log.info(f"조판 정리 {touched}장")
    working = split_pass(working, cfg)
    working = merge_pass(working, cfg)
    working = adjust_timing(working, cfg, duration)
    working = resolve_overlaps(working, cfg)
    working = break_lines(working, cfg)
    return renumber(drop_empty(working))
