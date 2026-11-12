#!/usr/bin/env python3
"""작업공간 DB만 읽어 일괄 작업·분석 요청의 시간표를 뽑는다(성능 Phase 0, D120) — 유료 호출 0.

    python scripts/perf_baseline.py --workspace <작업공간 사본>/_vlogsrt [--gap 60]

`jobs`는 만든 시각과 마지막 갱신뿐이라 한 작업의 길이에 **줄 서서 기다린 시간**이 섞인다 — 169개를
한꺼번에 걸고 셋씩 도는 프록시는 「평균 500초」로 보이지만 실제로는 일괄 전체가 975초다. 그래서
같은 종류가 `--gap`초 안에 이어 걸린 작업을 한 묶음으로 보고 묶음의 벽시계(첫 만듦 → 마지막 갱신)를
잰다. 분석 요청은 작업마다 단계(asr·luna)의 첫 응답 → 마지막 응답과 응답 사이 간격을 잰다
(요청을 보낸 시각이 아니라 기록한 시각이다). DB는 읽기 전용(`mode=ro`)으로 연다.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt import log  # noqa: E402

DEFAULT_GAP_SECONDS = 60.0
"""같은 종류의 작업을 한 묶음으로 보는 만듦 간격 — 일괄 승인·불러오기는 1초 안에 수백 개를 건다."""


@dataclass(frozen=True)
class Burst:
    kind: str
    count: int
    first_created: datetime
    last_updated: datetime

    @property
    def wall_seconds(self) -> float:
        return (self.last_updated - self.first_created).total_seconds()


@dataclass(frozen=True)
class StageSpan:
    stage: str
    jobs: int
    requests: int
    median_span_seconds: float
    max_span_seconds: float
    median_gap_seconds: float | None


def _moment(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace(" ", "T"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def bursts(rows: Iterable[tuple[str, str, str]], gap_seconds: float) -> list[Burst]:
    """(종류, 만듦, 갱신) → 묶음, 첫 만듦 순.

    묶음 안 마지막 만듦에서 `gap`초 넘게 떨어지면 새 묶음이다.
    """
    by_kind: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for kind, created, updated in rows:
        by_kind[kind].append((_moment(created), _moment(updated)))
    found: list[Burst] = []
    for kind, times in by_kind.items():
        times.sort()
        start, last_created, last_updated, count = times[0][0], times[0][0], times[0][1], 1
        for created, updated in times[1:]:
            if (created - last_created).total_seconds() > gap_seconds:
                found.append(Burst(kind, count, start, last_updated))
                start, last_updated, count = created, updated, 0
            last_created = created
            last_updated = max(last_updated, updated)
            count += 1
        found.append(Burst(kind, count, start, last_updated))
    return sorted(found, key=lambda burst: (burst.first_created, burst.kind))


def request_spans(rows: Iterable[tuple[str, str, str]]) -> list[StageSpan]:
    """(작업, 단계, 기록 시각) → 단계마다 작업 수·요청 수와 작업별 첫→마지막·응답 간격.

    첫→마지막은 중앙·최대, 응답 간격은 중앙이다.
    """
    grouped: dict[str, dict[str, list[datetime]]] = defaultdict(lambda: defaultdict(list))
    for job_id, stage, created in rows:
        grouped[stage][job_id].append(_moment(created))
    spans: list[StageSpan] = []
    for stage, jobs in sorted(grouped.items()):
        lengths, gaps = [], []
        for moments in jobs.values():
            moments.sort()
            lengths.append((moments[-1] - moments[0]).total_seconds())
            gaps.extend((b - a).total_seconds() for a, b in zip(moments, moments[1:], strict=False))
        spans.append(
            StageSpan(
                stage=stage,
                jobs=len(jobs),
                requests=sum(len(moments) for moments in jobs.values()),
                median_span_seconds=statistics.median(lengths),
                max_span_seconds=max(lengths),
                median_gap_seconds=statistics.median(gaps) if gaps else None,
            )
        )
    return spans


def _connect(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)


def _burst_table(found: list[Burst]) -> list[str]:
    lines = ["| 시작(UTC) | 종류 | 작업 수 | 묶음 벽시계(초) |", "|---|---|---:|---:|"]
    for burst in found:
        started = burst.first_created.strftime("%m-%d %H:%M")
        lines.append(f"| {started} | {burst.kind} | {burst.count} | {burst.wall_seconds:.0f} |")
    return lines


def _span_table(spans: list[StageSpan]) -> list[str]:
    lines = [
        "| 단계 | 작업 수 | 응답 수 | 작업별 첫→마지막 중앙(초) | 최대(초) | 응답 간격 중앙(초) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for span in spans:
        gap = "—" if span.median_gap_seconds is None else f"{span.median_gap_seconds:.1f}"
        lines.append(
            f"| {span.stage} | {span.jobs} | {span.requests} | {span.median_span_seconds:.0f}"
            f" | {span.max_span_seconds:.0f} | {gap} |"
        )
    return lines


def report(database: Path, gap_seconds: float) -> str:
    connection = _connect(database)
    try:
        jobs = connection.execute(
            "SELECT kind, created_at, updated_at FROM jobs WHERE status = 'succeeded'"
        ).fetchall()
        requests = connection.execute(
            "SELECT job_id, stage, created_at FROM analysis_requests"
        ).fetchall()
    finally:
        connection.close()
    lines = [f"## 성공한 작업 묶음(같은 종류 · 만듦 간격 {gap_seconds:g}초 안)", ""]
    lines += _burst_table(bursts(jobs, gap_seconds))
    if requests:
        lines += ["", "## 분석 요청(기록 시각 기준)", ""]
        lines += _span_table(request_spans(requests))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, required=True, help="작업공간 폴더(사본 권장)")
    parser.add_argument("--gap", type=float, default=DEFAULT_GAP_SECONDS)
    args = parser.parse_args()
    log.out(report(args.workspace / "project.sqlite3", args.gap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
