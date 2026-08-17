"""SRT 두 개를 비교하는 지표 8종. 여기서부터 모든 판단이 숫자가 된다."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config
from .hangul import strip_for_compare
from .models import Cue
from .validate import EPSILON

_DEFAULTS = Config()

TextLike = Sequence[Cue] | str


def _as_text(value: TextLike) -> str:
    if isinstance(value, str):
        return value
    return " ".join(cue.text for cue in value)


def _compare_text(value: TextLike) -> str:
    """CER은 정규화 후 계산한다. 조판 차이가 텍스트 오류로 잡히면 안 된다."""
    return strip_for_compare(_as_text(value))


def levenshtein(source: str, target: str) -> int:
    """편집거리. `SequenceMatcher.ratio()`는 편집거리가 아니라서 직접 쓴다."""
    if source == target:
        return 0
    if not source:
        return len(target)
    if not target:
        return len(source)
    previous = list(range(len(target) + 1))
    for i, source_char in enumerate(source, start=1):
        current = [i]
        for j, target_char in enumerate(target, start=1):
            cost = 0 if source_char == target_char else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def char_error_rate(hyp: TextLike, ref: TextLike) -> float:
    reference = _compare_text(ref)
    hypothesis = _compare_text(hyp)
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return levenshtein(hypothesis, reference) / len(reference)


def proper_noun_accuracy(hyp: TextLike, ref: TextLike, keywords: Sequence[str]) -> float:
    """정답에 실제로 등장한 고유명사만 센다. 등장하지 않은 사전 항목은 채점 대상이 아니다."""
    reference = _compare_text(ref)
    hypothesis = _compare_text(hyp)
    expected = 0
    matched = 0
    for keyword in keywords:
        needle = strip_for_compare(keyword)
        if not needle:
            continue
        ref_count = reference.count(needle)
        if ref_count == 0:
            continue
        expected += ref_count
        matched += min(hypothesis.count(needle), ref_count)
    if expected == 0:
        return 1.0
    return matched / expected


def greedy_match(
    hyp_times: Sequence[float], ref_times: Sequence[float], tol: float
) -> list[tuple[int, int]]:
    """정렬 후 탐욕 매칭. 헝가리안은 이 문제에 과하다."""
    candidates = sorted(
        (
            (abs(h - r), hi, ri)
            for hi, h in enumerate(hyp_times)
            for ri, r in enumerate(ref_times)
            if abs(h - r) <= tol
        )
    )
    used_hyp: set[int] = set()
    used_ref: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _, hyp_index, ref_index in candidates:
        if hyp_index in used_hyp or ref_index in used_ref:
            continue
        used_hyp.add(hyp_index)
        used_ref.add(ref_index)
        pairs.append((hyp_index, ref_index))
    return sorted(pairs)


def cue_start_mae(hyp: Sequence[Cue], ref: Sequence[Cue], tol: float | None = None) -> float:
    """매칭된 큐 쌍의 시작 시각 평균 절대 오차(초). 매칭이 없으면 무한대."""
    window = _DEFAULTS.mae_match_tol if tol is None else tol
    hyp_starts = [cue.start for cue in hyp]
    ref_starts = [cue.start for cue in ref]
    pairs = greedy_match(hyp_starts, ref_starts, window)
    if not pairs:
        return float("inf")
    total = sum(abs(hyp_starts[h] - ref_starts[r]) for h, r in pairs)
    return total / len(pairs)


def cue_start_hit_rate(hyp: Sequence[Cue], ref: Sequence[Cue], tol: float | None = None) -> float:
    """정답 큐 중 시작 시각이 tol 안에 들어온 비율."""
    window = _DEFAULTS.start_tol if tol is None else tol
    if not ref:
        return 0.0
    pairs = greedy_match([c.start for c in hyp], [c.start for c in ref], window)
    return len(pairs) / len(ref)


def _boundaries(cues: Sequence[Cue]) -> list[float]:
    times: list[float] = []
    for cue in cues:
        times.append(cue.start)
        times.append(cue.end)
    return sorted(times)


def boundary_f1(hyp: Sequence[Cue], ref: Sequence[Cue], tol: float | None = None) -> float:
    window = _DEFAULTS.boundary_tol if tol is None else tol
    hyp_times = _boundaries(hyp)
    ref_times = _boundaries(ref)
    if not hyp_times or not ref_times:
        return 0.0
    matched = len(greedy_match(hyp_times, ref_times, window))
    if matched == 0:
        return 0.0
    precision = matched / len(hyp_times)
    recall = matched / len(ref_times)
    return 2 * precision * recall / (precision + recall)


def cue_violations(cue: Cue, cfg: Config) -> list[str]:
    """정제 규칙 위반 목록. 빈 리스트면 통과."""
    violations: list[str] = []
    if cue.duration < cfg.min_duration - EPSILON:
        violations.append("too_short")
    if cue.duration > cfg.max_duration + EPSILON:
        violations.append("too_long")
    if cue.cps > cfg.max_cps + EPSILON:
        violations.append("cps")
    if len(cue.lines) > cfg.max_lines:
        violations.append("too_many_lines")
    if any(len(line) > cfg.max_chars_per_line for line in cue.lines):
        violations.append("line_too_wide")
    return violations


def violation_rate(cues: Sequence[Cue], cfg: Config) -> float:
    if not cues:
        return 0.0
    offending = sum(1 for cue in cues if cue_violations(cue, cfg))
    return offending / len(cues)


@dataclass(frozen=True, slots=True)
class Metrics:
    cer: float
    proper_noun_accuracy: float
    cue_start_mae: float
    cue_start_hit_rate: float
    boundary_f1: float
    violation_rate: float
    hyp_cues: int
    ref_cues: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "cer": self.cer,
            "proper_noun_accuracy": self.proper_noun_accuracy,
            "cue_start_mae": self.cue_start_mae,
            "cue_start_hit_rate": self.cue_start_hit_rate,
            "boundary_f1": self.boundary_f1,
            "violation_rate": self.violation_rate,
            "hyp_cues": self.hyp_cues,
            "ref_cues": self.ref_cues,
        }


def evaluate_cues(hyp: Sequence[Cue], ref: Sequence[Cue], cfg: Config) -> Metrics:
    return Metrics(
        cer=char_error_rate(hyp, ref),
        proper_noun_accuracy=proper_noun_accuracy(hyp, ref, cfg.keywords),
        cue_start_mae=cue_start_mae(hyp, ref),
        cue_start_hit_rate=cue_start_hit_rate(hyp, ref, cfg.start_tol),
        boundary_f1=boundary_f1(hyp, ref, cfg.boundary_tol),
        violation_rate=violation_rate(hyp, cfg),
        hyp_cues=len(hyp),
        ref_cues=len(ref),
    )


def format_metrics(metrics: Metrics) -> str:
    mae_ms = (
        "inf" if metrics.cue_start_mae == float("inf") else f"{metrics.cue_start_mae * 1000:.0f}"
    )
    return "\n".join(
        [
            f"CER              {metrics.cer * 100:.1f}%",
            f"고유명사 정확도  {metrics.proper_noun_accuracy * 100:.1f}%",
            f"큐 시작 MAE      {mae_ms}ms",
            f"±200ms 적중률    {metrics.cue_start_hit_rate * 100:.1f}%",
            f"큐 경계 F1       {metrics.boundary_f1:.3f}",
            f"정제 위반율      {metrics.violation_rate * 100:.1f}%",
            f"큐 수            {metrics.hyp_cues} (정답 {metrics.ref_cues})",
        ]
    )
