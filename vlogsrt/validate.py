"""정제 결과 이상치 검사.

`ERR`은 파이프라인이 고치되 반드시 남긴다. `WARN`은 고치지 않고 기록만 한다.
위반율 계산(evaluate)이 이 결과와 같은 기준을 쓴다.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import Config
from .models import ORIGIN_ORPHAN, VERDICT_OK, Cue, Issue, Segment

# 부동소수 잡음이 임계값 비교를 흔들지 않게 하는 여유값. 임계값이 아니다.
EPSILON = 1e-6

ERR = "ERR"
WARN = "WARN"
INFO = "INFO"


def validate(cues: Sequence[Cue], spine: Sequence[Segment], cfg: Config) -> list[Issue]:
    issues: list[Issue] = []
    issues.extend(_structural(cues))
    issues.extend(_typographic(cues, cfg))
    issues.extend(_timing(cues, cfg))
    issues.extend(_provenance(cues, spine, cfg))
    return sorted(issues, key=lambda issue: (_severity(issue.level), issue.cue_id))


def _severity(level: str) -> int:
    return {ERR: 0, WARN: 1, INFO: 2}.get(level, 3)


def _structural(cues: Sequence[Cue]) -> list[Issue]:
    issues: list[Issue] = []
    for cue in cues:
        if cue.end <= cue.start:
            issues.append(
                Issue(
                    ERR,
                    "bad_range",
                    cue.index,
                    f"끝이 시작보다 앞이다: {cue.start:.3f}~{cue.end:.3f}",
                )
            )
        if not cue.text.strip():
            issues.append(Issue(ERR, "empty_text", cue.index, "텍스트가 비었다"))
    for previous, following in zip(cues, cues[1:], strict=False):
        if following.start < previous.end:
            issues.append(
                Issue(
                    ERR,
                    "overlap",
                    following.index,
                    f"앞 큐와 {previous.end - following.start:.3f}초 겹친다",
                )
            )
    return issues


def _typographic(cues: Sequence[Cue], cfg: Config) -> list[Issue]:
    issues: list[Issue] = []
    for cue in cues:
        if len(cue.lines) > cfg.max_lines:
            issues.append(
                Issue(
                    ERR, "too_many_lines", cue.index, f"{len(cue.lines)}줄 (최대 {cfg.max_lines})"
                )
            )
        for line in cue.lines:
            if len(line) > cfg.max_chars_per_line:
                issues.append(
                    Issue(
                        WARN,
                        "line_too_wide",
                        cue.index,
                        f"{len(line)}자 (최대 {cfg.max_chars_per_line}): {line}",
                    )
                )
    return issues


def _timing(cues: Sequence[Cue], cfg: Config) -> list[Issue]:
    issues: list[Issue] = []
    for cue in cues:
        if cue.duration < cfg.min_duration - EPSILON:
            issues.append(
                Issue(
                    WARN, "too_short", cue.index, f"{cue.duration:.2f}초 (최소 {cfg.min_duration})"
                )
            )
        if cue.duration > cfg.max_duration + EPSILON:
            issues.append(
                Issue(
                    WARN, "too_long", cue.index, f"{cue.duration:.2f}초 (최대 {cfg.max_duration})"
                )
            )
        if cue.cps > cfg.max_cps + EPSILON:
            issues.append(Issue(WARN, "cps", cue.index, f"{cue.cps:.1f}자/초 (최대 {cfg.max_cps})"))
    for previous, following in zip(cues, cues[1:], strict=False):
        gap = following.start - previous.end
        if 0 <= gap < cfg.min_gap - EPSILON:
            issues.append(
                Issue(WARN, "tight_gap", following.index, f"앞 큐와 간격 {gap * 1000:.0f}ms")
            )
    return issues


def _provenance(cues: Sequence[Cue], spine: Sequence[Segment], cfg: Config) -> list[Issue]:
    issues: list[Issue] = []
    known = {segment.id for segment in spine}
    speech_start = min((s.start for s in spine if s.verdict == VERDICT_OK), default=0.0)
    speech_end = max((s.end for s in spine if s.verdict == VERDICT_OK), default=0.0)
    for cue in cues:
        if not cue.seg_ids:
            issues.append(Issue(WARN, "no_origin", cue.index, "골격 세그먼트를 잃었다"))
        elif any(seg_id not in known for seg_id in cue.seg_ids):
            issues.append(Issue(ERR, "unknown_seg", cue.index, f"모르는 seg_id {cue.seg_ids}"))
        if cue.conf < cfg.conf_threshold:
            issues.append(Issue(WARN, "low_conf", cue.index, f"신뢰도 {cue.conf:.2f}"))
        if cue.origin == ORIGIN_ORPHAN:
            issues.append(Issue(INFO, "orphan", cue.index, "환청 구간에서 되살린 텍스트"))
        for flag in cue.flags:
            issues.append(Issue(INFO, f"flag:{flag}", cue.index, flag))
        if spine and (cue.end < speech_start or cue.start > speech_end):
            issues.append(Issue(WARN, "out_of_spine", cue.index, "골격 시간 범위 밖이다"))
    return issues


def count_by_level(issues: Sequence[Issue]) -> dict[str, int]:
    counts = {ERR: 0, WARN: 0, INFO: 0}
    for issue in issues:
        counts[issue.level] = counts.get(issue.level, 0) + 1
    return counts
