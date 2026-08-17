"""자를 자리 고르기 — **자리만 고르고, 구간을 고르지 않는다**(불변 규칙 4).

조각 수는 모델의 최대 입력이 정하고, 경계는 목표 지점 근처에서 가장 조용한 곳으로
옮긴다. **옮길 뿐 버리지 않는다** — 조각은 언제나 `[0, duration]`을 빈틈없이 덮는다.
D24는 "이 구간은 말이 아니다"를 판단하다가 초반·마지막 3~6분을 날렸다(D25).

조용함의 **임계값이 없다.** 창 안에서 가장 조용한 곳을 고를 뿐이라, 온통 시끄러워도
목표 지점에서 잘리고 아무것도 사라지지 않는다.

순수 함수만 둔다. ffmpeg 호출은 `preprocess.py`, API 호출은 `transcribe.py`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from .config import Config
from .models import Span


def chunk_count(size_bytes: int, cfg: Config) -> int:
    """조각 수. **유일한 기준은 모델의 최대 입력이다**(D25).

    낮추면 조각이 늘어난다 — whisper가 긴 조각에서 반복 루프에 빠질 때의 탈출구다(D13).
    """
    return max(1, math.ceil(size_bytes / cfg.chunk_max_bytes))


def target_boundaries(duration: float, count: int) -> list[float]:
    """조각 수만큼 균등하게 나눈 목표 경계. 양 끝(0, duration)은 경계가 아니다."""
    return [duration * index / count for index in range(1, count)]


def quietest(
    amplitudes: Sequence[float], hop_seconds: float, window_start: float, target: float
) -> float:
    """진폭이 가장 낮은 지점의 시각. 같은 값이 여럿이면 목표에 가장 가까운 곳.

    임계값을 두지 않는다. 온통 시끄러워도 "가장 덜 시끄러운 곳"은 언제나 있고, 그게
    목표 지점보다 나쁠 수는 없다. 못 찾아서 구간을 버리는 일이 생기지 않는다(규칙 4).
    """
    if not amplitudes:
        return target
    floor = min(amplitudes)
    times = (
        window_start + index * hop_seconds
        for index, value in enumerate(amplitudes)
        if value == floor
    )
    return min(times, key=lambda moment: abs(moment - target))


def plan_cuts(
    duration: float,
    size_bytes: int,
    cfg: Config,
    find_quiet: Callable[[float], float],
) -> list[float]:
    """자를 시각 목록. `find_quiet`는 목표 시각을 받아 실제로 자를 시각을 돌려준다.

    탐색 결과가 앞 경계를 넘거나 오디오 밖으로 나가면 **목표 지점을 그대로 쓴다.**
    자를 자리를 못 고르는 것이 구간을 잃을 이유는 되지 못한다.
    """
    cuts: list[float] = []
    previous = 0.0
    for target in target_boundaries(duration, chunk_count(size_bytes, cfg)):
        cut = find_quiet(target)
        if not previous < cut < duration:
            cut = target if previous < target < duration else (previous + duration) / 2
        cuts.append(cut)
        previous = cut
    return cuts


def chunks_from_cuts(cuts: Sequence[float], duration: float) -> list[Span]:
    """자를 시각을 조각 범위로 편다. **합치면 언제나 `[0, duration]`이다**(규칙 4)."""
    edges = [0.0, *cuts, duration]
    return [(edges[index], edges[index + 1]) for index in range(len(edges) - 1)]


def parse_segment_list(csv_text: str) -> list[tuple[str, float, float]]:
    """ffmpeg `-segment_list_type csv`의 출력: `파일명,시작,끝`.

    여기 적힌 시각이 **실제로 잘린 자리**다. 우리가 요청한 값이 아니라 이걸 오프셋으로
    써야 자막이 밀리지 않는다(D13).
    """
    rows: list[tuple[str, float, float]] = []
    for line in csv_text.splitlines():
        columns = line.strip().split(",")
        if len(columns) < 3:
            continue
        try:
            rows.append((columns[0], float(columns[1]), float(columns[2])))
        except ValueError:  # 헤더나 빈 줄. 형식이 어긋난 행은 조용히 넘긴다
            continue
    return rows
