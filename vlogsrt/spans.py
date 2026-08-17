"""발화 구간과 묶음 — **자막 시각의 유일한 출처**(불변 규칙 1).

여기 있는 값은 전부 ffmpeg가 실제 오디오에서 잰 것에서 나온다. 모델은 글자만
만들고 시각은 만들지 않는다(D24).

순수 함수만 둔다. ffmpeg 호출은 `preprocess.py`, API 호출은 `transcribe.py`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config

Span = tuple[float, float]

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass(frozen=True, slots=True)
class Batch:
    """한 번에 전사할 발화 구간 묶음.

    `start`/`end`는 잘라낼 오디오 범위이고, 자르는 자리는 항상 무음이다.
    `spans`는 원본 오디오 좌표의 발화 구간이며, 받은 글자가 여기에 배분된다.
    """

    start: float
    end: float
    index: int
    spans: tuple[Span, ...]

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_silence_output(stderr: str, duration: float) -> list[Span]:
    """`silencedetect` 결과는 stdout이 아니라 stderr에 나온다."""
    spans: list[Span] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            pending = max(0.0, float(start_match.group(1)))
            continue
        end_match = _SILENCE_END_RE.search(line)
        if end_match and pending is not None:
            end = float(end_match.group(1))
            if end > pending:
                spans.append((pending, end))
            pending = None
    if pending is not None and duration > pending:
        spans.append((pending, duration))
    return spans


def speech_spans(silences: Sequence[Span], duration: float, cfg: Config) -> list[Span]:
    """무음의 여집합. **이 값이 모든 자막 시각의 뿌리다.**

    `min_speech_duration`보다 짧은 조각은 말이 아니라 잡음으로 본다. 글자는
    이웃 구간이 받으므로 어절이 사라지지는 않는다(규칙 9).
    """
    spans: list[Span] = []
    cursor = 0.0
    for start, end in silences:
        if start - cursor >= cfg.min_speech_duration:
            spans.append((cursor, min(start, duration)))
        cursor = max(cursor, end)  # 겹친 무음을 줘도 시각이 뒤집히지 않는다
    if duration - cursor >= cfg.min_speech_duration:
        spans.append((cursor, duration))
    return spans


def group_spans(
    spans: Sequence[Span], size_bytes: int, duration: float, cfg: Config
) -> list[Batch]:
    """발화 구간을 요청 크기 묶음으로 모은다. 자르는 자리는 항상 무음 한가운데다.

    묶음이 클수록 요청은 적지만 배분 오차가 커진다. 한도는 길이(`batch_max_seconds`)와
    업로드 크기(`chunk_max_bytes`) **둘 다** 지킨다.
    """
    if not spans:
        return []
    budget = _budget(size_bytes, duration, cfg)
    starts: list[int] = []  # 각 묶음이 시작하는 구간의 위치
    for position, span in enumerate(spans):
        # 첫 구간은 한도를 넘겨서라도 받는다. 자를 무음이 없으니 버릴 수는 없다(규칙 9).
        if not starts or span[1] - spans[starts[-1]][0] > budget:
            starts.append(position)
    bounds = [*starts, len(spans)]
    return [
        _batch(spans, bounds[index], bounds[index + 1], index, duration, cfg)
        for index in range(len(starts))
    ]


def _budget(size_bytes: int, duration: float, cfg: Config) -> float:
    """묶음 하나가 담을 수 있는 발화 범위(초). 여백(`_pad`) 몫을 미리 뺀다."""
    limit = cfg.batch_max_seconds
    if duration > 0 and size_bytes > 0:
        per_second = size_bytes / duration
        limit = min(limit, cfg.chunk_max_bytes / per_second)
    return max(0.0, limit - 2 * cfg.silence_min_duration)


def _batch(
    spans: Sequence[Span], first: int, stop: int, index: int, duration: float, cfg: Config
) -> Batch:
    """`spans[first:stop]`을 묶음 하나로 만든다. 앞뒤 여백은 무음 안에서만 준다.

    말머리가 잘리면 모델이 첫 음절을 놓친다. 그렇다고 여백이 이웃 발화까지 거리의
    절반을 넘으면 옆 묶음의 말을 삼킨다.
    """
    group = spans[first:stop]
    previous_end = spans[first - 1][1] if first > 0 else 0.0
    following = spans[stop][0] if stop < len(spans) else duration
    return Batch(
        start=group[0][0] - _pad(group[0][0] - previous_end, cfg),
        end=group[-1][1] + _pad(following - group[-1][1], cfg),
        index=index,
        spans=tuple(group),
    )


def _pad(gap: float, cfg: Config) -> float:
    return max(0.0, min(cfg.silence_min_duration, gap / 2))
