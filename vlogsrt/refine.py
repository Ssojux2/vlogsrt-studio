"""정제 엔진 — 자르고, 합치고, 시간을 다듬는다.

순서가 중요하다: 분할 → 병합 → 시간 보정 → 충돌 해소 → 줄바꿈 → 검사.
병합을 분할보다 먼저 돌리면 둘이 서로를 되돌린다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from . import log
from .config import Config
from .layout import FORCED_BREAK, break_lines, break_text
from .models import ORIGIN_MERGE, ORIGIN_SPLIT, Cue, Word
from .validate import validate

__all__ = [
    "FORCED_BREAK",
    "NO_SPLIT_POINT",
    "adjust_timing",
    "break_lines",
    "break_text",
    "merge_pass",
    "refine",
    "resolve_overlaps",
    "split_cue",
    "split_pass",
    "validate",
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


def _words_inside(cue: Cue, words: Sequence[Word]) -> list[Word]:
    return [word for word in words if word.start >= cue.start and word.end <= cue.end]


def _longest_pause(cue: Cue, words: Sequence[Word], cfg: Config) -> tuple[float, float] | None:
    """큐 안 단어 사이의 최대 휴지. `pause_split`보다 짧으면 쓸모없다."""
    inside = _words_inside(cue, words)
    if len(inside) < 2:
        return None
    best: tuple[float, float] | None = None
    best_length = cfg.pause_split
    for previous, following in zip(inside, inside[1:], strict=False):
        gap = following.start - previous.end
        if gap >= best_length:
            best_length = gap
            best = (previous.end, following.start)
    return best


def _char_split_index(text: str, ratio: float) -> int | None:
    """비율에 가장 가까운 어절 경계. 공백이 없으면 None."""
    spaces = [index for index, ch in enumerate(text) if ch == " " and 0 < index < len(text) - 1]
    if not spaces:
        return None
    target = len(text) * ratio
    return min(spaces, key=lambda index: abs(index - target))


def _slice_whisper(whisper_text: str, ratio: float) -> tuple[str, str]:
    """검토용 원문도 같은 비율로 나눈다. 안 나누면 diff 마킹이 큐 전체를 물들인다."""
    if not whisper_text:
        return "", ""
    index = _char_split_index(whisper_text, ratio)
    if index is None:
        index = max(1, int(len(whisper_text) * ratio))
    return whisper_text[:index].strip(), whisper_text[index:].strip()


def split_cue(cue: Cue, words: Sequence[Word], cfg: Config, depth: int = 0) -> list[Cue]:
    """자막으로 너무 길거나 빽빽한 큐를 자른다. 시각은 원본 큐 안에서만 움직인다."""
    if not needs_split(cue, cfg) or depth >= cfg.max_split_depth:
        return [cue]
    if cue.duration <= cfg.min_duration * 2:
        return [cue]

    text = cue.text
    pause = _longest_pause(cue, words, cfg)
    if pause is not None:
        ratio = (pause[0] - cue.start) / cue.duration
        index = _char_split_index(text, ratio)
        first_end, second_start = pause
    else:
        index = _char_split_index(text, 0.5)
        if index is None:
            return [cue.with_flag(NO_SPLIT_POINT)]
        ratio = index / len(text)
        boundary = cue.start + cue.duration * ratio
        first_end = boundary - cfg.min_gap / 2
        second_start = boundary + cfg.min_gap / 2

    if index is None:
        return [cue.with_flag(NO_SPLIT_POINT)]

    first_text = text[:index].strip()
    second_text = text[index:].strip()
    if not first_text or not second_text:
        return [cue.with_flag(NO_SPLIT_POINT)]

    first_end = min(max(first_end, cue.start + cfg.min_gap), cue.end - cfg.min_gap)
    second_start = min(max(second_start, first_end + cfg.min_gap), cue.end)
    first_whisper, second_whisper = _slice_whisper(cue.whisper_text, index / len(text))

    first = replace(
        cue,
        end=first_end,
        lines=(first_text,),
        origin=ORIGIN_SPLIT,
        whisper_text=first_whisper,
    )
    second = replace(
        cue,
        start=second_start,
        lines=(second_text,),
        origin=ORIGIN_SPLIT,
        whisper_text=second_whisper,
    )
    return [
        *split_cue(first, words, cfg, depth + 1),
        *split_cue(second, words, cfg, depth + 1),
    ]


def split_pass(cues: Sequence[Cue], words: Sequence[Word], cfg: Config) -> list[Cue]:
    result: list[Cue] = []
    for cue in cues:
        result.extend(split_cue(cue, words, cfg))
    return result


# ─────────────────────────────────────────────────────────────
#  병합
# ─────────────────────────────────────────────────────────────


def _merged(first: Cue, second: Cue) -> Cue:
    text = f"{first.text} {second.text}".strip()
    whisper = f"{first.whisper_text} {second.whisper_text}".strip()
    return replace(
        first,
        end=second.end,
        lines=(text,),
        seg_ids=tuple(dict.fromkeys((*first.seg_ids, *second.seg_ids))),
        origin=ORIGIN_MERGE,
        whisper_text=whisper,
        flags=tuple(dict.fromkeys((*first.flags, *second.flags))),
        conf=min(first.conf, second.conf),
    )


def can_merge(first: Cue, second: Cue, cfg: Config) -> bool:
    if first.duration >= cfg.min_duration:
        return False
    if second.start - first.end >= cfg.merge_gap:
        return False
    candidate = _merged(first, second)
    if len(candidate.text) > cfg.max_chars_per_cue:
        return False
    # 연쇄 병합에서는 매번 다시 계산해야 한다. 한 번 구한 CPS를 재사용하면 틀린다.
    return candidate.cps <= cfg.max_cps


def merge_pass(cues: Sequence[Cue], cfg: Config) -> list[Cue]:
    """짧은 큐를 이웃과 합친다. 브이로그는 감탄사가 독립 세그먼트로 나와 자막이 깜빡인다."""
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


def adjust_timing(cues: Sequence[Cue], cfg: Config, duration: float = 0.0) -> list[Cue]:
    """lead_in/out → 길이 클램프 순서. 충돌 해소를 먼저 하면 lead_out이 다시 겹친다."""
    adjusted: list[Cue] = []
    for cue in cues:
        start = max(0.0, cue.start - cfg.lead_in)
        end = cue.end + cfg.lead_out
        if duration > 0:
            end = min(end, duration)
        if end - start < cfg.min_duration:
            end = start + cfg.min_duration
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
    kept = [cue for cue in cues if cue.text.strip()]
    if len(kept) != len(cues):
        log.warn(f"빈 큐 {len(cues) - len(kept)}개를 버렸다.")
    return kept


def renumber(cues: Sequence[Cue]) -> list[Cue]:
    return [replace(cue, index=position) for position, cue in enumerate(cues, start=1)]


def refine(
    cues: Sequence[Cue], words: Sequence[Word], cfg: Config, duration: float = 0.0
) -> list[Cue]:
    """정제 전 과정. `redo`가 부르는 것도 이 함수다."""
    working = split_pass(cues, words, cfg)
    working = merge_pass(working, cfg)
    working = adjust_timing(working, cfg, duration)
    working = resolve_overlaps(working, cfg)
    working = break_lines(working, cfg)
    return renumber(drop_empty(working))
