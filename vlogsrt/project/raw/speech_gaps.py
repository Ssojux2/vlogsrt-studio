"""말소리 빈칸 고르기(D59 19f) — 자막이 덮지 않은 말소리를 **다시 물을** 구간으로 고른다.

순수 계산이다.

입력은 값이다: PCM 시계(표본 0 = 0초)의 큐, 말소리 칸(`SpeechTrack` — 자막 시각 확인 D55가 잰
칸 그대로), PCM 표본 수·주파수, 1단계가 실패한 청크의 표본 범위. 이 모듈은 파일·DB·프로세스·전사·
모델을 모른다 — 재료를 모으는 일과 묻는 일은 서비스(`refill_*`)와 `speech_refill`의 몫이다.

규칙 4의 선(D59 — D55 「표시만」의 예외 「다시 물을 구간 고르기」):

- 고르는 것은 **다시 묻기 위해서**다. 빼려고 고르지 않는다 — 기존 큐는 하나도 건드리지 않고 넓힌
  구간은 **통째로** 다시 묻는다. 길면 모델 한도로 나누되 빈틈 없이 잇는다(`request_pieces`).
- 「이 구간은 말이 아니다」라고 판정하지 않는다. 재료는 `sync_check.uncovered_speech` — 말소리
  측정이 짚은 칸 중 큐 τ 밖(Rτ의 나머지) 그대로다. 측정이 놓친 말은 고르지 못한다(측정 한계일 수
  있음, D55).
- 시각을 만들지 않는다. 고른 구간은 **묻는 범위**일 뿐이고, 넣을 자막의 시각은 whisper 세그먼트 +
  그 조각의 실제 표본 오프셋뿐이다(규칙 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

from vlogsrt.project import sync_check
from vlogsrt.project.config import GapRefillSettings, SyncCheckSettings, gap_refill_problems

SampleRange = tuple[int, int]
"""`[start, end)` PCM 표본 범위 — 채널별 프레임 번호다."""


@dataclass(frozen=True, slots=True)
class GapSpan:
    """다시 물을 구간 하나 — 넓힌 표본 범위와, 그 안에서 자막이 덮지 않은 말소리.

    `speech_runs`는 그 말소리 표본 구간(견적이 「이번에 묻는 조각 안의 말소리」만 세는 재료),
    `speech`는 그 합이다. `failed_speech`는 그 말소리 중 1단계 전사가 실패한 청크 안에 든 표본
    수다 — 화면이 「전사가 실패한 구간」과 「전사가 건너뛴 곳」을 나눠 보이는 재료일 뿐 고르기를
    바꾸지 않는다.
    """

    start: int
    end: int
    speech: int
    failed_speech: int
    speech_runs: tuple[SampleRange, ...] = ()


def select_gaps(
    cues: Sequence[sync_check.Cue],
    speech: sync_check.SpeechTrack,
    *,
    sample_count: int,
    sample_rate: int,
    failed: Sequence[SampleRange] = (),
    sync_settings: SyncCheckSettings,
    settings: GapRefillSettings,
) -> tuple[GapSpan, ...]:
    """다시 물을 구간 — 「말소리 − 큐 ±τ」를 표본으로 옮겨 잇고 넓힌 것. 큐가 다 덮으면 빈 튜플.

    순서: 칸 → 표본(시작은 내림·끝은 올림 — 줄이지 않는다) → `merge_seconds` 이하 틈 잇기(단
    **기존 큐 칸을 품은 틈은 잇지 않는다** — 이으면 whisper가 큐의 말과 새 말을 한 세그먼트로 들어
    넣지 못하는 줄이 된다) → `pad_seconds` 넓히기 → `[0, sample_count]`로 자르기 → 겹치거나 맞닿은
    것 잇기.
    """
    _require(settings, sample_count, sample_rate)
    marks = sync_check.cue_runs(cues, speech.hop, speech.frame_count)
    runs = sync_check.uncovered_speech(marks, speech, sync_settings)
    spoken = _join(_clip(_to_samples(runs, speech.hop, sample_rate), sample_count), 0)
    if not spoken:
        return ()
    walls = _to_samples(marks, speech.hop, sample_rate)
    joined = _join(spoken, _samples(settings.merge_seconds, sample_rate), walls)
    pad = _samples(settings.pad_seconds, sample_rate)
    widened = _join(_clip(((max(0, a - pad), b + pad) for a, b in joined), sample_count), 0)
    failed_runs = sync_check.merge_runs(failed)
    return tuple(_gap(span, spoken, failed_runs) for span in widened)


def request_pieces(spans: Iterable[GapSpan], bound: int) -> tuple[SampleRange, ...]:
    """구간마다 모델 한도(`bound` 표본) 이하의 조각으로 나눈다 — 빈틈·겹침 없이 구간을 덮는다.

    `plan_audio_chunks`와 같은 탐욕 분할이다(한도, 한도, …, 나머지). 조각을 줄이거나 버리지 않는다.
    """
    if isinstance(bound, bool) or not isinstance(bound, int) or bound < 1:
        raise ValueError(f"조각 한도는 1 이상의 표본 수다: {bound!r}")
    return tuple(
        (start, min(start + bound, span.end))
        for span in spans
        for start in range(span.start, span.end, bound)
    )


def _require(settings: GapRefillSettings, sample_count: int, sample_rate: int) -> None:
    problems = gap_refill_problems(settings)
    if sample_rate < 1 or sample_count < 0:
        problems.append(f"PCM 표본 수·주파수가 맞지 않다: {sample_count} / {sample_rate}Hz")
    if problems:
        raise ValueError("말소리 빈칸 고르기 설정이 계약을 어긴다: " + "; ".join(problems))


def _samples(value: Fraction, sample_rate: int) -> int:
    """초 → 표본 수(올림) — 넓힘·잇는 거리를 줄이는 쪽으로 반올림하지 않는다."""
    return ceil(value * sample_rate)


def _to_samples(
    runs: Iterable[sync_check.Run], hop: Fraction, sample_rate: int
) -> list[SampleRange]:
    """칸 구간 → 표본 구간. 시작은 내림, 끝은 올림이다 — 칸이 덮은 소리를 하나도 빼지 않는다."""
    width = hop * sample_rate
    return [(floor(start * width), ceil(end * width)) for start, end in runs]


def _clip(spans: Iterable[SampleRange], sample_count: int) -> list[SampleRange]:
    """`[0, sample_count]` 밖을 자른다 — 마지막 칸은 PCM 끝을 넘을 수 있다(칸 수는 올림)."""
    return [
        (max(0, start), min(end, sample_count))
        for start, end in spans
        if min(end, sample_count) > max(0, start)
    ]


def _join(
    spans: Sequence[SampleRange], gap: int, walls: Sequence[SampleRange] = ()
) -> list[SampleRange]:
    """정렬된 구간 중 틈이 `gap` 표본 이하이고 그 틈에 벽(기존 큐 칸)이 없는 것을 잇는다.

    `gap`이 0이면 겹치거나 맞닿은 것만 잇는다.
    """
    joined: list[list[int]] = []
    for start, end in sorted(spans):
        if joined and start - joined[-1][1] <= gap and not _walled(joined[-1][1], start, walls):
            joined[-1][1] = max(joined[-1][1], end)
        else:
            joined.append([start, end])
    return [(start, end) for start, end in joined]


def _walled(left: int, right: int, walls: Sequence[SampleRange]) -> bool:
    """`(left, right)` 틈에 벽이 하나라도 걸치는가."""
    return any(start < right and end > left for start, end in walls)


def _gap(
    span: SampleRange, spoken: Sequence[SampleRange], failed: Sequence[SampleRange]
) -> GapSpan:
    inside = sync_check.clip_runs(spoken, span[0], span[1])
    lost = sync_check.intersection_frames(inside, failed)
    return GapSpan(span[0], span[1], sync_check.run_total(inside), lost, inside)


__all__ = ["GapSpan", "SampleRange", "request_pieces", "select_gaps"]
