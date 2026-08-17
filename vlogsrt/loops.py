"""세그먼트를 가로지르는 whisper 반복 환청 검출.

`compression_ratio`는 **세그먼트 하나 안의** 반복만 본다. whisper가 같은 문장을
세그먼트마다 한 번씩 끊어 내보내면 각 세그먼트의 압축률은 정상이라 환청 필터를
그대로 통과한다. 실측: 77분 영상에서 `'구어체 한국어에서 한국어 자막을 사용합니다.'`가
103개 세그먼트에 걸쳐 570초 동안 반복됐고, 압축률만으로는 그 구조를 볼 수 없었다.

검출(`find_loops`)은 어디서든 불러도 되는 보고용이고, 강등(`demote`)은 골격 생성
단계(fetch_spine)에서만 부른다. 생성이 끝난 골격은 불변이다(불변 규칙 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .config import Config
from .hangul import to_nfc
from .models import VERDICT_COMPRESSION_FAIL, VERDICT_OK, Segment


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


def demote(spine: Sequence[Segment], cfg: Config) -> list[Segment]:
    """반복 구간의 `ok` 세그먼트를 `compression_fail`로 강등한 새 골격을 돌려준다.

    ok로 남겨두면 두 군데서 무너진다. 실측(D18): '오 오 오' 구간 147초가 (1) 큐가
    되어 시간축을 차지하고 (2) 정렬 기준 문자열에 들어가 절단점을 무너뜨렸다.
    그 결과 빈 큐 538개가 쏟아졌고, 3분 이전 자막이 통째로 사라졌다. 강등하면
    큐도 정렬 재료도 아니게 되고, 공백 채우기의 시각 닻으로만 남는다 — 반복
    환청이라도 세그먼트 **경계**는 오디오를 따라가기 때문이다.

    `silent`는 건드리지 않는다. "발화가 없었다"는 판정을 닻으로 바꾸면 발화가
    없던 자리에 자막을 만들게 된다. `low_logprob`도 이미 제외라 그대로 둔다.
    """
    looped = {seg_id for loop in find_loops(spine, cfg) for seg_id in loop.seg_ids}
    return [
        replace(segment, verdict=VERDICT_COMPRESSION_FAIL)
        if segment.id in looped and segment.verdict == VERDICT_OK
        else segment
        for segment in spine
    ]


def undetected(loops: Sequence[Loop], spine: Sequence[Segment]) -> list[Segment]:
    """반복 구간에 속하면서도 `ok` 판정을 받아 골격에 남은 세그먼트.

    이것이 최종 SRT로 새어 나가는 환청이다. `demote`를 거친 골격이라면 비어야 한다.
    """
    looped_ids = {seg_id for loop in loops for seg_id in loop.seg_ids}
    return [s for s in spine if s.id in looped_ids and s.verdict == VERDICT_OK]
