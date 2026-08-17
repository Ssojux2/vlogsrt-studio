"""whisper가 놓친 구간의 정본 텍스트를 **그 구간으로 되돌린다.**

절단점 사상의 정의상 큐 하나가 받는 텍스트는 `[앞 큐가 끝난 시각, 이 큐가 끝난 시각]`
동안 나온 말이다. 보통 둘은 붙어 있어 문제가 없다. 그런데 그 사이에 환청으로
걸러낸 세그먼트가 있으면, 그 시간대에 나온 말까지 뒤 큐 하나에 얹힌다. 실측(D17):

    30~177초   whisper가 '오 오 오 …'만 반복 → 147초가 통째로 빔
    4045초     1.4초짜리 큐에 729자

버리면 안 된다. 그 시간대에 사람이 한 말이고, **놓인 자리만** 틀렸다.

시각은 어디서 오는가: whisper가 환청을 한 구간이라도 **세그먼트 경계 자체는
오디오를 따라간다.** 못 믿을 것은 텍스트뿐이고, 그 텍스트는 어차피 최종 SRT에
들어가지 않는다(불변 규칙 2). 그래서 제외된 세그먼트를 시각의 닻으로 쓴다.
whisper가 세그먼트조차 주지 않은 구간에서만 균등 분할로 떨어진다.

골격에서 온 큐의 start/end는 건드리지 않는다(불변 규칙 3). 새로 만드는 큐만
`time_estimated`를 달고 나가며, 신뢰도는 `orphan_conf_max`로 눌러 둔다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from . import log
from .config import Config
from .models import ORIGIN_ORPHAN, VERDICT_OK, VERDICT_SILENT, Cue, Segment

TIME_ESTIMATED = "time_estimated"
Span = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Filled:
    """되돌려 놓은 구간 하나."""

    start: float
    end: float
    chars: int
    pieces: int


@dataclass(frozen=True, slots=True)
class _Window:
    start: float
    end: float
    seg_id: int | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


def capacity(start: float, end: float, cfg: Config) -> int:
    """이 시간대가 무리 없이 담을 수 있는 글자 수.

    조판 한도와 읽기 속도 중 큰 쪽을 쓴다. 긴 큐는 그만큼 더 담을 수 있어야 하고,
    짧은 큐도 최소 한 화면은 담아야 한다.
    """
    by_layout = cfg.max_chars_per_cue
    by_speed = int(max(0.0, end - start) * cfg.max_cps)
    return int(max(by_layout, by_speed) * cfg.overflow_factor)


def fill_gaps(
    cues: Sequence[Cue],
    spine: Sequence[Segment],
    cfg: Config,
    silences: Sequence[Span] = (),
) -> tuple[list[Cue], list[Filled]]:
    """앞 공백에서 나온 말을 그 공백으로 되돌린다. **한 글자도 버리지 않는다.**

    `silent` 판정은 닻으로 쓰지 않는다. 그건 "그 시간에 발화가 없었다"는 판정이라
    거기로 글자를 옮기면 없는 말을 만들어내는 셈이 된다. 반면 `compression_fail`과
    `low_logprob`은 **소리는 들었는데 못 알아들었다**는 뜻이라 되돌릴 자리가 맞다.
    """
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    if not ordered:
        return [], []
    excluded = [
        s for s in spine if s.verdict not in (VERDICT_OK, VERDICT_SILENT) and s.duration > 0
    ]
    result: list[Cue] = []
    filled: list[Filled] = []

    floor = 0.0
    for run in _runs(ordered, cfg):
        windows = _gap_windows(floor, run[0].start, excluded, run, cfg)
        _emit(_allocate(run, windows, silences, cfg, tail=True), run, result, filled)
        floor = run[-1].end

    last = result[-1]
    trailing = [_clip(s, last.end, float("inf")) for s in excluded if s.start >= last.end]
    if trailing:
        result.pop()
        _emit(_allocate([last], trailing, silences, cfg, tail=False), [last], result, filled)
    return sorted(result, key=lambda cue: (cue.start, cue.end)), filled


def _runs(ordered: Sequence[Cue], cfg: Config) -> list[list[Cue]]:
    """붙어 있는 큐는 한 묶음으로 배분한다.

    큐 둘이 0.2초 간격으로 붙어 있으면 뒤엣것은 되돌릴 자리를 못 얻는다. 앞엣것이
    공백을 이미 다 썼기 때문이다. 실측(D17): 그래서 933자짜리 큐 하나가 살아남았다.
    사이가 큐 하나도 못 들어갈 만큼 좁으면 어차피 같은 시간대다. 함께 나눈다.
    """
    runs: list[list[Cue]] = []
    for cue in ordered:
        if runs and cue.start - runs[-1][-1].end < cfg.min_duration:
            runs[-1].append(cue)
            continue
        runs.append([cue])
    return runs


def _emit(pieces: list[Cue], run: Sequence[Cue], result: list[Cue], filled: list[Filled]) -> None:
    moved = [piece for piece in pieces if piece.origin == ORIGIN_ORPHAN]
    if not moved:
        result.extend(run)  # 되돌릴 자리가 없으면 그대로 둔다. 버리지는 않는다.
        return
    result.extend(pieces)
    filled.append(
        Filled(
            start=min(piece.start for piece in pieces),
            end=max(piece.end for piece in pieces),
            chars=sum(len(piece.text) for piece in moved),
            pieces=len(moved),
        )
    )


def _clip(segment: Segment, floor: float, ceiling: float) -> _Window:
    return _Window(max(floor, segment.start), min(ceiling, segment.end), segment.id)


def _gap_windows(
    floor: float, ceiling: float, excluded: Sequence[Segment], run: Sequence[Cue], cfg: Config
) -> list[_Window]:
    """되돌릴 자리. 걸러낸 세그먼트를 우선 쓰고, 없으면 넘칠 때만 균등 분할한다.

    걸러낸 세그먼트가 있다는 것은 **whisper가 그 시간에 소리를 듣긴 했다**는 뜻이다.
    그런 자리가 없는데 큐가 넘치지도 않으면, 앞의 빈 시간은 그냥 침묵이다. 건드리지 않는다.
    """
    if ceiling - floor < cfg.min_duration:
        return []
    anchors = [_clip(s, floor, ceiling) for s in excluded if s.end > floor and s.start < ceiling]
    usable = [window for window in anchors if window.duration >= cfg.min_duration]
    if usable:
        return usable
    if all(len(cue.text) <= capacity(cue.start, cue.end, cfg) for cue in run):
        return []
    return _even(floor, ceiling, cfg)


def _even(start: float, end: float, cfg: Config) -> list[_Window]:
    span = end - start
    count = max(1, int(span // cfg.max_duration))
    step = span / count
    return [_Window(start + i * step, start + (i + 1) * step) for i in range(count)]


def _allocate(
    run: Sequence[Cue],
    windows: Sequence[_Window],
    silences: Sequence[Span],
    cfg: Config,
    *,
    tail: bool,
) -> list[Cue]:
    """텍스트를 공백과 큐 자신들에게 시간에 비례해 나눈다.

    `tail=True`(앞 공백)면 큐가 **꼬리**를 갖는다. 앞부분은 앞 공백에서 나온
    말이고, 큐가 실제로 덮는 시간에 나온 말은 뒤쪽이기 때문이다. 반대로 하면
    큐마다 남의 말이 붙는다. `tail=False`(뒤 공백)면 큐가 머리를 갖는다.
    """
    text = " ".join(cue.text for cue in run if cue.text).strip()
    own = max(sum(cue.end - cue.start for cue in run), cfg.min_duration)
    total = sum(window.duration for window in windows) + own
    if not windows or total <= 0 or not text:
        return list(run)

    share = round(len(text) * (total - own) / total)
    if not tail:
        kept = _snap_back(text, max(0, len(text) - share))
        return [*_share_back(text[:kept], run), *_slice_into(text[kept:], windows, silences, cfg)]

    spill = _snap(text, min(len(text), share))
    moved = _slice_into(text[:spill], windows, silences, cfg)
    return [*moved, *_share_back(text[spill:], run)]


def _share_back(text: str, run: Sequence[Cue]) -> list[Cue]:
    """묶음에 남은 텍스트를 각 큐의 길이에 비례해 되돌려 놓는다."""
    body = text.strip()
    if len(run) == 1:
        return [replace(run[0], lines=(body,))]
    total = sum(max(cue.end - cue.start, 0.0) for cue in run)
    if total <= 0:
        return [replace(run[0], lines=(body,)), *(replace(c, lines=("",)) for c in run[1:])]
    filled: list[Cue] = []
    cursor = 0
    for index, cue in enumerate(run):
        last = index == len(run) - 1
        stop = (
            len(body)
            if last
            else _snap(
                body, min(len(body), cursor + round(len(body) * (cue.end - cue.start) / total))
            )
        )
        filled.append(replace(cue, lines=(body[cursor:stop].strip(),)))
        cursor = stop
    return filled


def _slice_into(
    text: str, windows: Sequence[_Window], silences: Sequence[Span], cfg: Config
) -> list[Cue]:
    """텍스트를 창들에 길이 비례로 나눠 담는다."""
    total = sum(window.duration for window in windows)
    if not text.strip() or total <= 0:
        return []
    pieces: list[Cue] = []
    cursor = 0
    for index, window in enumerate(windows):
        last = index == len(windows) - 1
        stop = (
            len(text)
            if last
            else _snap(text, min(len(text), cursor + round(len(text) * window.duration / total)))
        )
        body = text[cursor:stop].strip()
        cursor = stop
        if body:
            pieces.append(_estimated(window, body, silences, cfg))
        if cursor >= len(text):
            break
    return pieces


def _snap(text: str, index: int) -> int:
    """어절 경계로 뒤쪽으로 옮긴다. 단어를 반으로 자르지 않는다."""
    if index >= len(text):
        return len(text)
    space = text.find(" ", index)
    return space + 1 if space != -1 else len(text)


def _snap_back(text: str, index: int) -> int:
    """어절 경계로 앞쪽으로 옮긴다.

    뒤 공백에 나눠줄 때 앞으로 스냅해야 한다. 뒤로 스냅하면 짧은 텍스트에서
    경계를 끝까지 밀어버려 뒤 공백이 한 글자도 못 받는다.
    """
    if index <= 0:
        return 0
    space = text.rfind(" ", 0, min(index + 1, len(text)))
    return space + 1 if space != -1 else 0


def _estimated(window: _Window, body: str, silences: Sequence[Span], cfg: Config) -> Cue:
    start, end = _trim_to_speech(window.start, window.end, silences, cfg)
    return Cue(
        start=start,
        end=end,
        lines=(body,),
        seg_ids=(window.seg_id,) if window.seg_id is not None else (),
        origin=ORIGIN_ORPHAN,
        conf=cfg.orphan_conf_max,
        flags=(TIME_ESTIMATED,),
        whisper_text="",
    )


def _trim_to_speech(
    start: float, end: float, silences: Sequence[Span], cfg: Config
) -> tuple[float, float]:
    """앞뒤로 붙은 무음을 걷어낸다. 최소 길이는 지킨다."""
    trimmed_start, trimmed_end = start, end
    for silence_start, silence_end in silences:
        if silence_start <= trimmed_start < silence_end:
            trimmed_start = min(silence_end, trimmed_end)
        if silence_start < trimmed_end <= silence_end:
            trimmed_end = max(silence_start, trimmed_start)
    if trimmed_end - trimmed_start < cfg.min_duration:
        return start, max(end, start + cfg.min_duration)
    return trimmed_start, trimmed_end


def report(filled: Sequence[Filled]) -> list[str]:
    """무엇을 어디로 되돌렸는지. 시각이 추정값이라는 사실을 사람이 알아야 한다."""
    if not filled:
        return []
    chars = sum(item.chars for item in filled)
    notices = [
        f"whisper가 놓친 구간 {len(filled)}곳으로 정본 {chars}자를 되돌렸다. "
        f"그 자막의 시각은 추정값이라 어긋날 수 있다."
    ]
    for item in sorted(filled, key=lambda entry: -entry.chars)[:3]:
        notices.append(
            f"  {item.start:.0f}~{item.end:.0f}초 · {item.chars}자를 큐 {item.pieces}개로 폈다"
        )
    for notice in notices:
        log.warn(notice)
    return notices
