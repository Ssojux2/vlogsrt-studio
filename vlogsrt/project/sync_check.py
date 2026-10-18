"""자막 시각 확인(D55) — 자막 큐와 말소리 칸을 대조해 **표시만** 한다. 순수 계산이다.

입력은 값이다: PCM 시계(표본 0 = 0초)의 큐와, VAD가 낸 말소리 칸(창 하나 = `hop`초)의
연속 구간. 이 모듈은 파일을 읽지도 쓰지도 않고, 프로세스·모델·전사·조판을 모른다.

규칙 4·5의 선: 자막을 고치거나 빼거나 고르지 않고, 어느 구간이 말인지 아닌지를 정하지
않는다. 말할 수 있는 것은 "이 자막 둘레에서 말소리를 **못 찾았다**(측정 한계일 수 있음)"
뿐이다 — Silero는 잡음 밑의 작은 말을 놓친다(실측 0018 466–472초). 판정은 화면의 표지일
뿐 자동 정리·선택·출력·분할 어디에도 들어가지 않는다. 등급만 좁은 예외다(D60·D65): 서비스가
지금 리비전 보고서의 판정·표지를 값으로 넘겨 녹색을 노랑으로 내리고, 초록이면서 확인 필요 줄이
적은 영상(「자막 확인됨」)에서만 표의 `usable` × 발화 많음 칸을 녹색으로 올린다 — 이 모듈은
그것을 모른다.

잴 재료가 모자라면 `skip`("측정 불가")이다 — 결코 초록이 아니다. 어긋남 추정은 최고점이
뚜렷하고(z) 0초보다 확실히 나을 때(이득)만 센다 — 말이 쉼 없이 이어지면 어디로 밀어도
겹침이 같아 잴 수 없다(실측 0009). 시각은 정확 유리수로 다루고 numpy를 쓰지 않는다.
"""

from __future__ import annotations

import json
import math
import sys
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.config import SyncCheckSettings

Run = tuple[int, int]
"""`[start, end)` 칸 번호 — 칸 하나는 VAD 창 하나(`hop`초)다."""

LEVEL_RED = "red"
LEVEL_AMBER = "amber"
LEVEL_GREEN = "green"
LEVEL_SKIP = "skip"

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_INFO = "info"

SHIFT_COUNTED = "counted"
SHIFT_LOW_CONFIDENCE = "low_confidence"
SHIFT_EDGE = "edge"
SHIFT_FEW_CUES = "too_few_cues"
SHIFT_LITTLE_SPEECH = "too_little_speech"

FLAG_NO_SPEECH = "no_speech"
FLAG_EARLY = "early"

NO_SPEECH_WORDING = "말소리를 못 찾음 (측정 한계일 수 있음)"
"""자막 둘레에 말소리 칸이 없을 때의 유일한 말 — '말이 아니다'라고 말하지 않는다(규칙 4)."""

REPORT_SCHEMA = "vlogsrt-sync-check/v1"


@dataclass(frozen=True, slots=True)
class Cue:
    """자막 한 장 — 시각은 PCM 시계의 정확 유리수 초다."""

    cue_id: str
    start: Fraction
    end: Fraction
    text: str


@dataclass(frozen=True, slots=True)
class SpeechTrack:
    """말소리 칸 — 채널 합집합의 연속 구간. 칸 수와 PCM 길이를 함께 싣는다."""

    hop: Fraction
    frame_count: int
    duration: Fraction
    runs: tuple[Run, ...]

    @property
    def speech_seconds(self) -> Fraction:
        return run_total(self.runs) * self.hop


@dataclass(frozen=True, slots=True)
class ShiftEstimate:
    """한 구간의 어긋남 추정 — `+`면 자막이 말보다 이르다. 믿을 때만 `counted`다."""

    status: str
    seconds: Fraction | None = None
    gain: Fraction | None = None
    peak_z: float | None = None

    @property
    def counted(self) -> bool:
        return self.status == SHIFT_COUNTED


@dataclass(frozen=True, slots=True)
class CueFlag:
    """표시용 큐 표지 — 화면이 그 자리로 이동할 수 있게 큐를 그대로 싣는다."""

    cue: Cue
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyncMetrics:
    """측정값 전부 — 몫은 정확 유리수, 잴 수 없으면 None."""

    cue_count: int
    duration: Fraction
    speech_seconds: Fraction
    precision: Fraction | None
    recall: Fraction | None
    offset: ShiftEstimate
    segments: tuple[ShiftEstimate, ...]
    drift: Fraction | None
    early_share: Fraction | None
    no_speech_share: Fraction | None
    whole_second_share: Fraction | None
    glued_share: Fraction | None
    flagged: tuple[CueFlag, ...]


@dataclass(frozen=True, slots=True)
class Reason:
    """판정 이유 한 줄 — 화면에 그대로 나간다."""

    code: str
    level: str
    text: str


@dataclass(frozen=True, slots=True)
class Check:
    """검사 한 줄 — pass/warn/fail/skip/info. `skip`은 아무것도 안 봤다는 뜻이다(D32)."""

    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class Verdict:
    level: str
    reasons: tuple[Reason, ...]
    checks: tuple[Check, ...]


# ─────────────────────────────────────────────────────────────
#  구간 산술 — 칸 번호의 연속 구간
# ─────────────────────────────────────────────────────────────


def speech_runs(flags: Iterable[bool]) -> tuple[Run, ...]:
    """참 칸의 연속 구간."""
    runs: list[Run] = []
    start: int | None = None
    index = -1
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, index + 1))
    return tuple(runs)


def merge_runs(runs: Iterable[Run]) -> tuple[Run, ...]:
    """겹치거나 맞닿은 구간을 합친다 — 빈 구간은 버린다(칸이 없으니 셀 것도 없다)."""
    merged: list[list[int]] = []
    for start, end in sorted(run for run in runs if run[1] > run[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def clip_runs(runs: Iterable[Run], low: int, high: int) -> tuple[Run, ...]:
    return tuple(
        (max(start, low), min(end, high)) for start, end in runs if min(end, high) > max(start, low)
    )


def dilate_runs(runs: Iterable[Run], reach: int, frame_count: int) -> tuple[Run, ...]:
    """구간을 양쪽으로 `reach`칸 넓힌다 — `[0, frame_count)` 밖은 자른다."""
    widened = ((start - reach, end + reach) for start, end in runs)
    return merge_runs(clip_runs(widened, 0, frame_count))


def run_total(runs: Iterable[Run]) -> int:
    return sum(end - start for start, end in runs)


def intersection_frames(left: Sequence[Run], right: Sequence[Run]) -> int:
    """정렬·합쳐진 두 구간열의 겹친 칸 수 — 두 손가락으로 한 번 훑는다."""
    total = left_at = right_at = 0
    while left_at < len(left) and right_at < len(right):
        start = max(left[left_at][0], right[right_at][0])
        end = min(left[left_at][1], right[right_at][1])
        total += max(0, end - start)
        if left[left_at][1] < right[right_at][1]:
            left_at += 1
        else:
            right_at += 1
    return total


def contains(runs: Sequence[Run], frame: int) -> bool:
    position = bisect_right(runs, (frame, sys.maxsize)) - 1
    return position >= 0 and runs[position][0] <= frame < runs[position][1]


# ─────────────────────────────────────────────────────────────
#  큐 → 칸
# ─────────────────────────────────────────────────────────────


def _frames(seconds: Fraction, hop: Fraction) -> int:
    """반올림 칸 번호 — 정확 유리수라 반올림 규칙(짝수 쪽)만이 유일한 손질이다."""
    return round(seconds / hop)


def _floor_frames(seconds: Fraction, hop: Fraction) -> int:
    """0 쪽으로 자른 칸 번호 — 큐 머리가 든 칸."""
    return int(seconds / hop)


def cue_runs(cues: Iterable[Cue], hop: Fraction, frame_count: int) -> tuple[Run, ...]:
    """큐가 차지한 칸 — 길이 0·뒤집힌 큐도 머리 칸 하나는 차지한다(글자는 남는다, 규칙 9)."""
    spans: list[Run] = []
    for cue in cues:
        low = max(0, _frames(cue.start, hop))
        high = min(frame_count, max(low + 1, _frames(cue.end, hop)))
        spans.append((low, high))
    return merge_runs(spans)


# ─────────────────────────────────────────────────────────────
#  겹침 곡선 — 자막을 밀어 보며 말소리와 겹친 칸을 센다
# ─────────────────────────────────────────────────────────────


def overlap_curve(speech: Sequence[Run], cues: Sequence[Run], max_lag: int) -> list[int]:
    """`c[k + max_lag] = Σ_t speech[t + k] · cue[t]`, k ∈ [-max_lag, max_lag].

    구간 쌍마다 겹침은 k에 대한 사다리꼴이라 기울기 변화 네 점만 적고 두 번 누적한다 —
    칸마다 곱하지 않는다(77분이면 칸 14만 × 지연 625). 결과는 칸을 하나하나 곱한 값과
    정확히 같다.
    """
    pairs = list(_curve_pairs(speech, cues, max_lag))
    base = min([-max_lag, *(a0 - b1 + 1 for a0, _, _, b1 in pairs)])
    bends = [0] * (max_lag - base + 2)
    for a0, a1, b0, b1 in pairs:
        rise, fall = sorted((a0 - b0, a1 - b1))
        for at, step in ((a0 - b1 + 1, 1), (rise + 1, -1), (fall + 1, -1), (a1 - b0 + 1, 1)):
            if at <= max_lag:
                bends[at - base] += step
    curve: list[int] = []
    slope = value = 0
    for offset, bend in enumerate(bends[:-1]):
        slope += bend
        value += slope
        if offset + base >= -max_lag:
            curve.append(value)
    return curve


def _curve_pairs(
    speech: Sequence[Run], cues: Sequence[Run], max_lag: int
) -> Iterable[tuple[int, int, int, int]]:
    """겹침이 [-max_lag, max_lag]에 닿는 (말소리, 큐) 구간 쌍만."""
    starts = [run[0] for run in speech]
    ends = [run[1] for run in speech]
    for b0, b1 in cues:
        first = bisect_right(ends, b0 - max_lag)
        last = bisect_left(starts, b1 + max_lag)
        for a0, a1 in speech[first:last]:
            yield a0, a1, b0, b1


def _peak_z(curve: Sequence[int], peak: int) -> float:
    """최고점이 곡선의 가운데값에서 몇 표준편차 위인가 — 평평하면 0(잴 수 없음)."""
    count = len(curve)
    total = sum(curve)
    variance = Fraction(sum(value * value for value in curve), count) - Fraction(total, count) ** 2
    if variance <= 0:
        return 0.0
    ordered = sorted(curve)
    middle = count // 2
    median = (
        Fraction(ordered[middle])
        if count % 2
        else Fraction(ordered[middle - 1] + ordered[middle], 2)
    )
    return float((curve[peak] - median) / Fraction(math.sqrt(variance)))


def scan_shift(
    cues: Sequence[Cue],
    speech: SpeechTrack,
    low: Fraction,
    high: Fraction,
    settings: SyncCheckSettings,
) -> ShiftEstimate:
    """`[low, high)`에서 시작하는 큐의 어긋남 — 같은 길이의 창을 양쪽으로 밀어 본다.

    말소리는 구간 ± 찾는 폭만, 정규화는 큐 칸 + 구간 안 말소리 칸(F1 꼴)이다. 최고점이
    찾는 폭 끝에 걸리면 진짜 어긋남이 폭 밖일 수 있으니 '잴 수 없음'이다.
    """
    hop, count = speech.hop, speech.frame_count
    chosen = [cue for cue in cues if low <= cue.start < high]
    if len(chosen) < settings.min_cues:
        return ShiftEstimate(SHIFT_FEW_CUES)
    reach = settings.shift_search_seconds
    window = clip_runs(
        speech.runs,
        max(0, _floor_frames(low - reach, hop)),
        min(count, _floor_frames(high + reach, hop)),
    )
    if run_total(window) * hop < settings.min_speech_seconds:
        return ShiftEstimate(SHIFT_LITTLE_SPEECH)
    marks = cue_runs(chosen, hop, count)
    max_lag = _floor_frames(reach, hop)
    curve = overlap_curve(window, marks, max_lag)
    inside = clip_runs(speech.runs, _floor_frames(low, hop), _floor_frames(high, hop))
    denominator = max(1, run_total(marks) + run_total(inside))
    return _estimate(curve, peak_index(curve, max_lag), max_lag, denominator, hop, settings)


def peak_index(curve: Sequence[int], max_lag: int) -> int:
    """최고점 자리 — 같은 최고점이 여럿이면 0초에 가장 가까운 것(어긋남을 키우지 않는 쪽)."""
    top = max(curve)
    return min(
        (index for index, value in enumerate(curve) if value == top),
        key=lambda index: (abs(index - max_lag), index),
    )


def _estimate(
    curve: Sequence[int],
    peak: int,
    max_lag: int,
    denominator: int,
    hop: Fraction,
    settings: SyncCheckSettings,
) -> ShiftEstimate:
    lag = peak - max_lag
    gain = Fraction(2 * (curve[peak] - curve[max_lag]), denominator)
    z = _peak_z(curve, peak)
    if abs(lag) == max_lag:
        status = SHIFT_EDGE
    elif z >= settings.offset_min_peak_z and gain >= settings.offset_min_gain:
        status = SHIFT_COUNTED
    else:
        status = SHIFT_LOW_CONFIDENCE
    return ShiftEstimate(status, lag * hop, gain, z)


# ─────────────────────────────────────────────────────────────
#  측정
# ─────────────────────────────────────────────────────────────


def _near(runs: Iterable[Run], speech: SpeechTrack, settings: SyncCheckSettings) -> tuple[Run, ...]:
    """구간을 τ(`speech_reach_seconds`)만큼 넓힌 칸 — Pτ·Rτ와 빈칸 고르기가 같은 τ를 쓴다."""
    reach = _frames(settings.speech_reach_seconds, speech.hop)
    return dilate_runs(runs, reach, speech.frame_count)


def subtract_runs(left: Iterable[Run], right: Iterable[Run]) -> tuple[Run, ...]:
    """`left − right` — 두 구간열을 정렬·합친 뒤 두 손가락으로 한 번 훑는다."""
    cut = merge_runs(right)
    kept: list[Run] = []
    at = 0
    for start, end in merge_runs(left):
        cursor = start
        while at < len(cut) and cut[at][1] <= cursor:
            at += 1
        probe = at
        while probe < len(cut) and cut[probe][0] < end:
            if cut[probe][0] > cursor:
                kept.append((cursor, cut[probe][0]))
            cursor = max(cursor, cut[probe][1])
            probe += 1
        if cursor < end:
            kept.append((cursor, end))
    return tuple(kept)


def uncovered_speech(
    marks: Sequence[Run], speech: SpeechTrack, settings: SyncCheckSettings
) -> tuple[Run, ...]:
    """말소리 칸 중 큐 τ **밖** — Rτ가 세지 않은 칸 그대로(D59 19f, 다시 물을 구간의 재료).

    판정이 아니다: 「말이 아니다」도 「말이다」도 정하지 않고, Rτ의 나머지를 칸으로 돌려줄 뿐이다.
    합은 언제나 `(1 − Rτ) × 말소리 칸`이다(시험이 못 박는다).
    """
    return subtract_runs(speech.runs, _near(marks, speech, settings))


def coverage(
    marks: Sequence[Run], speech: SpeechTrack, settings: SyncCheckSettings
) -> tuple[Fraction | None, Fraction | None]:
    """(Pτ, Rτ) — 큐 칸 중 말소리 τ 안의 몫, 말소리 칸 중 큐 τ 안의 몫."""
    near_speech = _near(speech.runs, speech, settings)
    near_cues = _near(marks, speech, settings)
    cue_frames, spoken = run_total(marks), run_total(speech.runs)
    precision = (
        Fraction(intersection_frames(marks, near_speech), cue_frames) if cue_frames else None
    )
    recall = Fraction(intersection_frames(speech.runs, near_cues), spoken) if spoken else None
    return precision, recall


def cue_flags(
    cues: Sequence[Cue], speech: SpeechTrack, settings: SyncCheckSettings
) -> tuple[tuple[CueFlag, ...], Fraction | None, Fraction | None]:
    """큐마다 '말소리를 못 찾음'·'이른 시작' 표지와 두 몫 — 표시만 한다."""
    hop, count = speech.hop, speech.frame_count
    reach = dilate_runs(speech.runs, _frames(settings.no_speech_reach_seconds, hop), count)
    onsets = [start for start, _ in speech.runs if start > 0]
    flagged: list[CueFlag] = []
    silent = early = judged = 0
    for cue in cues:
        flags: list[str] = []
        head = _floor_frames(cue.start, hop)
        span = clip_runs(((head, max(head + 1, _floor_frames(cue.end, hop))),), 0, count)
        if intersection_frames(span, reach) == 0:
            flags.append(FLAG_NO_SPEECH)
            silent += 1
        lead = _early_lead(cue, head, onsets, speech, settings)
        if lead is not None:
            judged += 1
            if lead:
                flags.append(FLAG_EARLY)
                early += 1
        if flags:
            flagged.append(CueFlag(cue, tuple(flags)))
    no_speech = Fraction(silent, len(cues)) if cues else None
    return tuple(flagged), (Fraction(early, judged) if judged else None), no_speech


def _early_lead(
    cue: Cue, head: int, onsets: Sequence[int], speech: SpeechTrack, settings: SyncCheckSettings
) -> bool | None:
    """큐가 말보다 `early_lead`초 넘게 먼저 뜨는가 — 잴 다음 말이 없으면 None(세지 않음)."""
    head = max(0, head)
    if head < speech.frame_count and contains(speech.runs, head):
        return False
    following = bisect_right(onsets, head)
    if following == len(onsets):
        return None
    onset = onsets[following]
    lead = (onset - head) * speech.hop
    return lead > settings.early_lead_seconds and (
        onset * speech.hop < cue.end + settings.early_onset_reach_seconds
    )


def timestamp_fingerprint(
    cues: Sequence[Cue], settings: SyncCheckSettings
) -> tuple[Fraction | None, Fraction | None]:
    """(정수 초 길이 몫, 붙은 시각 몫) — whisper가 시각을 재지 않고 이은 지문. 표시 전용."""
    tolerance = settings.whole_second_tolerance_seconds
    lengths = [cue.end - cue.start for cue in cues]
    whole = sum(1 for length in lengths if length > 0 and abs(length - round(length)) <= tolerance)
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    glued = sum(
        1
        for left, right in zip(ordered, ordered[1:], strict=False)
        if abs(right.start - left.end) <= tolerance
    )
    whole_share = Fraction(whole, len(cues)) if cues else None
    glued_share = Fraction(glued, len(cues) - 1) if len(cues) > 1 else None
    return whole_share, glued_share


def measure(cues: Sequence[Cue], speech: SpeechTrack, settings: SyncCheckSettings) -> SyncMetrics:
    """전부 잰다 — 판정은 `judge`의 몫이다."""
    marks = cue_runs(cues, speech.hop, speech.frame_count)
    precision, recall = coverage(marks, speech, settings)
    offset = scan_shift(cues, speech, Fraction(0), speech.duration, settings)
    parts = settings.segments
    segments = tuple(
        scan_shift(
            cues, speech, speech.duration * i / parts, speech.duration * (i + 1) / parts, settings
        )
        for i in range(parts)
    )
    counted = [part.seconds for part in segments if part.counted and part.seconds is not None]
    flagged, early, no_speech = cue_flags(cues, speech, settings)
    whole, glued = timestamp_fingerprint(cues, settings)
    return SyncMetrics(
        cue_count=len(cues),
        duration=speech.duration,
        speech_seconds=speech.speech_seconds,
        precision=precision,
        recall=recall,
        offset=offset,
        segments=segments,
        drift=max(counted) - min(counted) if len(counted) >= 2 else None,
        early_share=early,
        no_speech_share=no_speech,
        whole_second_share=whole,
        glued_share=glued,
        flagged=flagged,
    )


# ─────────────────────────────────────────────────────────────
#  판정 — 빨강·노랑·초록·측정 불가
# ─────────────────────────────────────────────────────────────


def _share(value: Fraction) -> str:
    return f"{float(value):.0%}"


def _seconds(value: Fraction) -> str:
    return f"{float(value):.1f}초"


def skip_reasons(metrics: SyncMetrics, settings: SyncCheckSettings) -> tuple[Reason, ...]:
    """잴 재료가 모자란 이유 — 하나라도 있으면 '측정 불가'다(초록이 아니다)."""
    reasons: list[Reason] = []
    if metrics.cue_count < settings.min_cues:
        reasons.append(
            Reason("few_cues", LEVEL_SKIP, f"자막이 {metrics.cue_count}개뿐이라 잴 수 없음")
        )
    if metrics.speech_seconds < settings.min_speech_seconds:
        reasons.append(
            Reason(
                "little_speech",
                LEVEL_SKIP,
                f"찾은 말소리가 {_seconds(metrics.speech_seconds)}뿐이라 잴 수 없음"
                " (측정 한계일 수 있음)",
            )
        )
    return tuple(reasons)


def red_reasons(metrics: SyncMetrics, settings: SyncCheckSettings) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    offset = metrics.offset
    seconds = offset.seconds if offset.counted else None
    if seconds is not None and abs(seconds) >= settings.red_offset_seconds:
        side = "빠름" if seconds > 0 else "늦음"
        reasons.append(
            Reason("offset", LEVEL_RED, f"자막이 말보다 {_seconds(abs(seconds))} {side}")
        )
    if metrics.precision is not None and metrics.precision < settings.red_precision_below:
        text = f"자막 칸의 {_share(metrics.precision)}만 말소리 가까이에 있음"
        reasons.append(Reason("precision", LEVEL_RED, text))
    if metrics.recall is not None and metrics.recall < settings.red_recall_below:
        reasons.append(
            Reason("recall", LEVEL_RED, f"말소리의 {_share(metrics.recall)}만 자막이 덮음")
        )
    early, silent = metrics.early_share, metrics.no_speech_share
    if (
        early is not None
        and silent is not None
        and early >= settings.red_early_at_least
        and silent >= settings.red_no_speech_at_least
    ):
        text = f"자막의 {_share(early)}가 말보다 먼저 뜨고 {_share(silent)}는 {NO_SPEECH_WORDING}"
        reasons.append(Reason("early_and_no_speech", LEVEL_RED, text))
    return tuple(reasons)


def amber_reasons(metrics: SyncMetrics, settings: SyncCheckSettings) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    if metrics.precision is not None and metrics.precision < settings.amber_precision_below:
        text = f"자막 칸의 {_share(metrics.precision)}만 말소리 가까이에 있음"
        reasons.append(Reason("precision", LEVEL_AMBER, text))
    if metrics.recall is not None and metrics.recall < settings.amber_recall_below:
        reasons.append(
            Reason("recall", LEVEL_AMBER, f"말소리의 {_share(metrics.recall)}만 자막이 덮음")
        )
    if metrics.early_share is not None and metrics.early_share >= settings.amber_early_at_least:
        lead = _seconds(settings.early_lead_seconds)
        text = f"자막의 {_share(metrics.early_share)}가 말보다 {lead} 넘게 먼저 뜸"
        reasons.append(Reason("early", LEVEL_AMBER, text))
    silent = metrics.no_speech_share
    if silent is not None and silent >= settings.amber_no_speech_at_least:
        text = f"자막의 {_share(silent)} 둘레에서 {NO_SPEECH_WORDING}"
        reasons.append(Reason("no_speech", LEVEL_AMBER, text))
    if metrics.drift is not None and metrics.drift >= settings.amber_drift_seconds:
        text = f"머리·가운데·꼬리의 어긋남이 {_seconds(metrics.drift)} 벌어짐 (점점 밀림)"
        reasons.append(Reason("drift", LEVEL_AMBER, text))
    return tuple(reasons)


def judge(
    metrics: SyncMetrics,
    settings: SyncCheckSettings,
    *,
    blockers: Sequence[Reason] = (),
    extra_checks: Sequence[Check] = (),
    extra_reasons: Sequence[Reason] = (),
) -> Verdict:
    """판정 — 막는 이유·재료 부족이면 측정 불가, 아니면 빨강 → 노랑 → 초록 순.

    정수 초·붙은 시각 지문은 판정에 쓰지 않는다(표시 전용). `extra_reasons`는 측정 밖에서
    온 이유(예: SRT가 오디오 원점을 빼먹은 모양)다 — 제 수준만큼 판정을 올린다.
    """
    checks = (*metric_checks(metrics, settings), *extra_checks)
    skipped = (*blockers, *skip_reasons(metrics, settings))
    if skipped:
        return Verdict(LEVEL_SKIP, tuple(skipped), checks)
    red = (*red_reasons(metrics, settings), *_at_level(extra_reasons, LEVEL_RED))
    amber = (*amber_reasons(metrics, settings), *_at_level(extra_reasons, LEVEL_AMBER))
    if red:
        return Verdict(LEVEL_RED, (*red, *_not_repeated(amber, red)), checks)
    return Verdict(LEVEL_AMBER if amber else LEVEL_GREEN, amber, checks)


def _at_level(reasons: Sequence[Reason], level: str) -> tuple[Reason, ...]:
    return tuple(reason for reason in reasons if reason.level == level)


def _not_repeated(amber: Sequence[Reason], red: Sequence[Reason]) -> tuple[Reason, ...]:
    """빨강이 이미 말한 측정을 노랑이 되풀이하지 않는다 — 같은 줄이 두 번 뜨지 않게."""
    said = {reason.code for reason in red}
    if "early_and_no_speech" in said:
        said |= {"early", "no_speech"}
    return tuple(reason for reason in amber if reason.code not in said)


def skipped_verdict(blockers: Sequence[Reason], extra_checks: Sequence[Check] = ()) -> Verdict:
    """잴 재료조차 못 만든 경우(오디오 없음·VAD 없음·PCM 확인 불가)."""
    return Verdict(LEVEL_SKIP, tuple(blockers), tuple(extra_checks))


def metric_checks(metrics: SyncMetrics, settings: SyncCheckSettings) -> tuple[Check, ...]:
    """화면의 검사 표 — 잴 수 없었던 것은 `skip`이다(통과가 아니다, D32)."""
    return (
        _coverage_check(metrics, settings),
        _offset_check(metrics, settings),
        _drift_check(metrics, settings),
        _share_check(
            "이른 시작",
            metrics.early_share,
            settings.red_early_at_least,
            settings.amber_early_at_least,
        ),
        _share_check(
            "말소리를 못 찾음",
            metrics.no_speech_share,
            settings.red_no_speech_at_least,
            settings.amber_no_speech_at_least,
        ),
        _fingerprint_check(metrics),
    )


def _coverage_check(metrics: SyncMetrics, settings: SyncCheckSettings) -> Check:
    name = "말소리 덮기 (Pτ·Rτ)"
    precision, recall = metrics.precision, metrics.recall
    if precision is None or recall is None:
        return Check(name, STATUS_SKIP, "큐나 말소리가 없어 재지 않음")
    detail = f"Pτ {float(precision):.2f} · Rτ {float(recall):.2f}"
    if precision < settings.red_precision_below or recall < settings.red_recall_below:
        return Check(name, STATUS_FAIL, detail)
    if precision < settings.amber_precision_below or recall < settings.amber_recall_below:
        return Check(name, STATUS_WARN, detail)
    return Check(name, STATUS_PASS, detail)


def _offset_check(metrics: SyncMetrics, settings: SyncCheckSettings) -> Check:
    name = "전체 어긋남"
    offset = metrics.offset
    if not offset.counted or offset.seconds is None:
        why = {
            SHIFT_EDGE: "최고점이 찾는 폭 끝에 걸림",
            SHIFT_FEW_CUES: "큐가 모자람",
            SHIFT_LITTLE_SPEECH: "말소리가 모자람",
        }.get(offset.status, "말이 이어지거나 최고점이 뚜렷하지 않음")
        return Check(name, STATUS_SKIP, f"잴 수 없음 — {why}{_shift_note(offset)}")
    detail = f"{float(offset.seconds):+.2f}초{_shift_note(offset)}"
    failing = abs(offset.seconds) >= settings.red_offset_seconds
    return Check(name, STATUS_FAIL if failing else STATUS_PASS, detail)


def _shift_note(estimate: ShiftEstimate) -> str:
    if estimate.peak_z is None or estimate.gain is None or estimate.seconds is None:
        return ""
    return (
        f" (최고점 {float(estimate.seconds):+.2f}초 · z {estimate.peak_z:.1f}"
        f" · 이득 {float(estimate.gain):.2f})"
    )


def _drift_check(metrics: SyncMetrics, settings: SyncCheckSettings) -> Check:
    name = "점점 밀림 (머리·가운데·꼬리)"
    parts = " / ".join(
        f"{float(part.seconds):+.2f}" if part.counted and part.seconds is not None else "—"
        for part in metrics.segments
    )
    if metrics.drift is None:
        return Check(name, STATUS_SKIP, f"잴 수 있는 구간이 둘 미만 — {parts}")
    status = STATUS_WARN if metrics.drift >= settings.amber_drift_seconds else STATUS_PASS
    return Check(name, status, f"폭 {float(metrics.drift):.2f}초 — {parts}")


def _share_check(name: str, share: Fraction | None, red: Fraction, amber: Fraction) -> Check:
    if share is None:
        return Check(name, STATUS_SKIP, "잴 큐가 없음")
    status = STATUS_WARN if share >= amber else STATUS_PASS
    return Check(name, status, f"{_share(share)} (노랑 {_share(amber)} · 빨강 조합 {_share(red)})")


def _fingerprint_check(metrics: SyncMetrics) -> Check:
    whole, glued = metrics.whole_second_share, metrics.glued_share
    detail = (
        f"정수 초 길이 {_share(whole) if whole is not None else '—'}"
        f" · 붙은 시각 {_share(glued) if glued is not None else '—'} (판정에 쓰지 않음)"
    )
    return Check("시각 지문", STATUS_INFO, detail)


# ─────────────────────────────────────────────────────────────
#  저장 PCM ↔ 원본 표본 대조 — 포락선(순수)
# ─────────────────────────────────────────────────────────────


def pcm_envelope(data: bytes, channels: int, hop_samples: int) -> list[float]:
    """s16le 교차 표본의 칸별 평균 절댓값(채널 평균) — 끝의 모자란 칸은 버린다."""
    samples = array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    width = hop_samples * channels
    return [
        sum(abs(value) for value in samples[start : start + width]) / width
        for start in range(0, len(samples) - width + 1, width)
    ]


def envelope_alignment(
    reference: Sequence[float], probe: Sequence[float], max_lag: int
) -> tuple[int, float]:
    """(지연 칸, 상관) — `probe[i + lag]`가 `reference[i]`와 가장 닮은 지연. 같으면 (0, 1)."""
    if list(reference) == list(probe):
        return 0, 1.0
    best = (0, -1.0)
    for lag in range(-max_lag, max_lag + 1):
        low, high = max(0, -lag), min(len(reference), len(probe) - lag)
        if high - low < 2:
            continue
        score = _correlation(reference[low:high], probe[low + lag : high + lag])
        if score > best[1]:
            best = (lag, score)
    return best


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    count = len(left)
    left_mean, right_mean = sum(left) / count, sum(right) / count
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_spread = math.sqrt(sum((a - left_mean) ** 2 for a in left))
    right_spread = math.sqrt(sum((b - right_mean) ** 2 for b in right))
    if left_spread == 0 or right_spread == 0:
        return 0.0
    return covariance / (left_spread * right_spread)


# ─────────────────────────────────────────────────────────────
#  보고서 — 정확 유리수의 안정 JSON
# ─────────────────────────────────────────────────────────────


def fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _optional(value: Fraction | None) -> str | None:
    return None if value is None else fraction_text(value)


def _estimate_document(estimate: ShiftEstimate) -> dict[str, object]:
    return {
        "status": estimate.status,
        "seconds": _optional(estimate.seconds),
        "gain": _optional(estimate.gain),
        "peak_z": None if estimate.peak_z is None else round(estimate.peak_z, 6),
    }


def metrics_document(metrics: SyncMetrics) -> dict[str, object]:
    return {
        "cue_count": metrics.cue_count,
        "duration": fraction_text(metrics.duration),
        "speech_seconds": fraction_text(metrics.speech_seconds),
        "precision": _optional(metrics.precision),
        "recall": _optional(metrics.recall),
        "offset": _estimate_document(metrics.offset),
        "segments": [_estimate_document(part) for part in metrics.segments],
        "drift": _optional(metrics.drift),
        "early_share": _optional(metrics.early_share),
        "no_speech_share": _optional(metrics.no_speech_share),
        "whole_second_share": _optional(metrics.whole_second_share),
        "glued_share": _optional(metrics.glued_share),
    }


def flagged_document(flagged: Sequence[CueFlag]) -> list[dict[str, object]]:
    return [
        {
            "id": item.cue.cue_id,
            "start": fraction_text(item.cue.start),
            "end": fraction_text(item.cue.end),
            "text": item.cue.text,
            "flags": list(item.flags),
        }
        for item in flagged
    ]


def verdict_document(verdict: Verdict) -> dict[str, object]:
    return {
        "verdict": verdict.level,
        "reasons": [
            {"code": item.code, "level": item.level, "text": item.text} for item in verdict.reasons
        ],
        "checks": [
            {"name": item.name, "status": item.status, "detail": item.detail}
            for item in verdict.checks
        ],
    }


def report_json(document: Mapping[str, object]) -> str:
    """정렬된 키·정확 유리수 문자열 — 같은 측정이면 같은 바이트다."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
