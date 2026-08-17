"""사람이 읽는 산출물 — `report.md`.

리포트의 목적은 하나다: 사람이 어디를 봐야 하는지 20초 안에 알게 하는 것.
핵심은 검증 결과다 — 의심 큐(일치율 낮은 순)와 자막이 발화를 놓쳤을 수 있는 구간.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import Config
from .models import VERDICT_OK, Cue, Issue, Segment
from .output import format_timecode
from .validate import ERR, INFO, WARN
from .verify import Verification


def _summary(cues: Sequence[Cue], spine: Sequence[Segment], duration: float) -> list[str]:
    excluded = sum(1 for segment in spine if segment.verdict != VERDICT_OK)
    total_chars = sum(len(cue.text) for cue in cues)
    return [
        "## 요약",
        "",
        f"- 영상 길이: {duration:.1f}초",
        f"- 골격 세그먼트: {len(spine)}개 (환청 판정 제외 {excluded}개)",
        f"- 최종 큐: {len(cues)}개, 총 {total_chars}자",
    ]


def _verification_section(verification: Verification | None, cfg: Config) -> list[str]:
    lines = ["", "## 검증 — gpt 대조", ""]
    if verification is None:
        lines.append("검증 생략 (gpt 응답 없음). SRT는 whisper 단독 결과다.")
        return lines

    suspects = sorted(verification.suspects, key=lambda item: (item.coverage, item.span[0]))
    estimate = len(suspects) * cfg.review_seconds_per_flag
    lines.append(
        f"의심 큐 {len(suspects)}개 / 전체 {verification.cue_total}개. "
        f"예상 검토 시간 약 {estimate / 60:.1f}분."
    )
    if suspects:
        lines.extend(["", "| # | 시각 | 자막 | 일치율 |", "|---|---|---|---|"])
        for suspect in suspects:
            lines.append(
                f"| {suspect.cue_index} | {format_timecode(suspect.span[0])} "
                f"| {_escape(suspect.text)} | {suspect.coverage:.0%} |"
            )

    flagged = [chunk for chunk in verification.chunks if chunk.flagged]
    if flagged:
        lines.extend(["", "자막이 발화를 놓쳤을 수 있는 구간 (gpt에만 있는 말이 많다):", ""])
        for chunk in flagged:
            lines.append(
                f"- {format_timecode(chunk.span[0])} ~ {format_timecode(chunk.span[1])}"
                f" · 유사도 {chunk.similarity:.0%} · 큐 {chunk.cue_count}개"
            )

    if not suspects and not flagged:
        lines.extend(["", "전 구간이 gpt 전사와 일치한다."])
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


def build_report(
    source: str,
    cues: Sequence[Cue],
    spine: Sequence[Segment],
    issues: Sequence[Issue],
    duration: float,
    cfg: Config,
    verification: Verification | None,
) -> str:
    sections = [
        f"# 검토 리포트 — {source}",
        "",
        *_summary(cues, spine, duration),
        *_verification_section(verification, cfg),
        *_issues_section(issues),
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
