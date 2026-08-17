"""사람이 읽는 산출물 — `report.md`와 사전 갱신 후보.

리포트의 목적은 하나다: 사람이 어디를 봐야 하는지 20초 안에 알게 하는 것.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import Config
from .models import ORIGIN_ORPHAN, VERDICT_OK, Cue, Issue, Segment
from .output import format_timecode
from .validate import ERR, INFO, WARN


@dataclass(frozen=True, slots=True)
class DictCandidate:
    """다음 회차 `keywords` 후보. whisper가 어떻게 잘못 들었는지 함께 보여준다."""

    canonical: str
    heard: str
    count: int

    def render(self) -> str:
        return f"{self.canonical} ← {self.heard} {self.count}회"


def _word_span(text: str, start: int, end: int) -> str:
    """불일치 구간을 어절 단위로 넓힌다. 조각난 글자만 모으면 사전이 못 쓴다."""
    left = text.rfind(" ", 0, start) + 1
    right = text.find(" ", end)
    if right == -1:
        right = len(text)
    return text[left:right].strip()


def dictionary_candidates(cues: Sequence[Cue], cfg: Config) -> list[DictCandidate]:
    pairs: Counter[tuple[str, str]] = Counter()
    for cue in cues:
        if not cue.whisper_text or cue.origin == ORIGIN_ORPHAN:
            continue
        matcher = SequenceMatcher(None, cue.whisper_text, cue.text, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "replace":
                continue
            heard = _word_span(cue.whisper_text, i1, i2)
            canonical = _word_span(cue.text, j1, j2)
            if not heard or not canonical or heard == canonical:
                continue
            if " " in canonical or " " in heard:
                continue
            pairs[(canonical, heard)] += 1
    candidates = [
        DictCandidate(canonical=canonical, heard=heard, count=count)
        for (canonical, heard), count in pairs.items()
        if count >= cfg.dict_min_occurrences
    ]
    return sorted(candidates, key=lambda c: (-c.count, c.canonical))


def _summary(cues: Sequence[Cue], spine: Sequence[Segment], duration: float) -> list[str]:
    excluded = sum(1 for segment in spine if segment.verdict != VERDICT_OK)
    orphans = sum(1 for cue in cues if cue.origin == ORIGIN_ORPHAN)
    total_chars = sum(len(cue.text) for cue in cues)
    return [
        "## 요약",
        "",
        f"- 영상 길이: {duration:.1f}초",
        f"- 골격 세그먼트: {len(spine)}개 (환청 판정 제외 {excluded}개)",
        f"- 최종 큐: {len(cues)}개, 총 {total_chars}자",
        f"- 되살린 고아 큐: {orphans}개",
    ]


def _priority(cues: Sequence[Cue], cfg: Config) -> list[str]:
    flagged = sorted(
        (cue for cue in cues if cue.conf < cfg.conf_threshold or cue.flags),
        key=lambda cue: (cue.conf, cue.start),
    )
    estimate = len(flagged) * cfg.review_seconds_per_flag
    lines = [
        "",
        "## 우선 확인",
        "",
        f"확인 대상 {len(flagged)}개. 예상 검토 시간 약 {estimate / 60:.1f}분.",
        "",
    ]
    if not flagged:
        lines.append("확인할 큐가 없다.")
        return lines
    lines.extend(
        [
            "| # | 시각 | 신뢰도 | whisper | gpt-transcribe | 표시 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for cue in flagged:
        flags = ", ".join(cue.flags) if cue.flags else "-"
        heard = cue.whisper_text or "-"
        lines.append(
            f"| {cue.index} | {format_timecode(cue.start)} | {cue.conf:.2f} "
            f"| {_escape(heard)} | {_escape(cue.text)} | {flags} |"
        )
    return lines


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _issues_section(issues: Sequence[Issue]) -> list[str]:
    lines = ["", "## 정제 경고", ""]
    shown = [issue for issue in issues if issue.level in (ERR, WARN)]
    if not shown:
        lines.append("경고 없음.")
        return lines
    lines.extend(["| 수준 | 코드 | 큐 | 내용 |", "|---|---|---|---|"])
    for issue in shown:
        lines.append(
            f"| {issue.level} | {issue.code} | {issue.cue_id} | {_escape(issue.message)} |"
        )
    info_count = sum(1 for issue in issues if issue.level == INFO)
    if info_count:
        lines.extend(["", f"INFO {info_count}건은 생략했다."])
    return lines


def _dictionary_section(candidates: Sequence[DictCandidate]) -> list[str]:
    lines = ["", "## 사전 갱신 후보", ""]
    if not candidates:
        lines.append("2회 이상 반복된 불일치 없음.")
        return lines
    lines.append("다음 회차 `--keywords-file`에 넣을 후보다.")
    lines.append("")
    for candidate in candidates:
        lines.append(f"- {candidate.render()}")
    return lines


def build_report(
    source: str,
    cues: Sequence[Cue],
    spine: Sequence[Segment],
    issues: Sequence[Issue],
    duration: float,
    cfg: Config,
) -> str:
    sections = [
        f"# 검토 리포트 — {source}",
        "",
        *_summary(cues, spine, duration),
        *_priority(cues, cfg),
        *_issues_section(issues),
        *_dictionary_section(dictionary_candidates(cues, cfg)),
        "",
    ]
    return "\n".join(sections)


def build_dashboard_row(name: str, label: str, metrics: dict[str, float | int]) -> str:
    mae = metrics["cue_start_mae"]
    mae_text = "inf" if mae == float("inf") else f"{float(mae) * 1000:.0f}ms"
    return (
        f"| {label} | {name} | {float(metrics['cer']) * 100:.1f}% "
        f"| {float(metrics['proper_noun_accuracy']) * 100:.1f}% "
        f"| {mae_text} | {float(metrics['cue_start_hit_rate']) * 100:.1f}% "
        f"| {float(metrics['boundary_f1']):.3f} "
        f"| {float(metrics['violation_rate']) * 100:.1f}% | {metrics['hyp_cues']} |"
    )


DASHBOARD_HEADER = (
    "| 버전 | 소스 | CER | 고유명사 | 큐시작 MAE | ±200ms | 큐경계 F1 | 위반율 | 큐 수 |\n"
    "|---|---|---|---|---|---|---|---|---|"
)
