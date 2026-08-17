"""세그먼트를 가로지르는 whisper 반복 환청 검출.

`compression_ratio`는 **세그먼트 하나 안의** 반복만 본다. whisper가 같은 문장을
세그먼트마다 한 번씩 끊어 내보내면 각 세그먼트의 압축률은 정상이라 환청 필터를
그대로 통과한다. 실측: 77분 영상에서 `'구어체 한국어에서 한국어 자막을 사용합니다.'`가
103개 세그먼트에 걸쳐 570초 동안 반복됐고, 압축률만으로는 그 구조를 볼 수 없었다.

이 모듈은 판정을 바꾸지 않는다. 골격을 읽고 반복 구간을 **보고**만 한다(불변 규칙 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config
from .hangul import to_nfc
from .models import Segment


@dataclass(frozen=True, slots=True)
class Loop:
    """같은 텍스트가 연속된 세그먼트에서 반복된 구간."""

    start: float
    end: float
    count: int
    text: str
    seg_ids: tuple[int, ...]

    @property
    def duration(self) -> float:
        return self.end - self.start


def _key(segment: Segment) -> str:
    """비교용 표준형. 공백과 유니코드 정규화 차이로 반복을 놓치지 않게 한다."""
    return " ".join(to_nfc(segment.text).split())


def find_loops(spine: Sequence[Segment], cfg: Config) -> list[Loop]:
    """연속 세그먼트가 같은 말을 되풀이하는 구간을 찾는다.

    `loop_min_repeats` 회 이상 이어지고 `loop_min_seconds` 이상 지속될 때만 센다.
    짧은 맞장구("응", "그니까")가 두어 번 겹치는 것은 환청이 아니다.
    """
    loops: list[Loop] = []
    position = 0
    while position < len(spine):
        text = _key(spine[position])
        last = position
        while last + 1 < len(spine) and text and _key(spine[last + 1]) == text:
            last += 1
        run = spine[position : last + 1]
        if _qualifies(run, text, cfg):
            loops.append(
                Loop(
                    start=run[0].start,
                    end=run[-1].end,
                    count=len(run),
                    text=text,
                    seg_ids=tuple(segment.id for segment in run),
                )
            )
        position = last + 1
    return loops


def _qualifies(run: Sequence[Segment], text: str, cfg: Config) -> bool:
    if not text or len(run) < cfg.loop_min_repeats:
        return False
    if len(text) < cfg.loop_min_chars:
        return False
    return run[-1].end - run[0].start >= cfg.loop_min_seconds


def looped_seconds(loops: Sequence[Loop]) -> float:
    return sum(loop.duration for loop in loops)


def undetected(loops: Sequence[Loop], spine: Sequence[Segment]) -> list[Segment]:
    """반복 구간에 속하면서도 `ok` 판정을 받아 골격에 남은 세그먼트.

    이것이 최종 SRT로 새어 나가는 환청이다. 비면 압축률 필터가 반복을 다 걸렀다는 뜻이다.
    """
    looped_ids = {seg_id for loop in loops for seg_id in loop.seg_ids}
    return [s for s in spine if s.id in looped_ids and s.verdict == "ok"]
