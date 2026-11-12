"""성능 계획 Phase 0(W-69 · D120) — 작업공간 DB만 읽어 작업·분석 요청 시간표를 뽑는 스크립트."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "perf_baseline.py"


def _module():
    spec = importlib.util.spec_from_file_location("perf_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass가 제 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


perf = _module()


def test_jobs_queued_together_form_one_burst_and_its_wall_clock() -> None:
    rows = [
        ("analysis_proxy", "2026-11-08T09:00:00+00:00", "2026-11-08T09:05:00+00:00"),
        ("analysis_proxy", "2026-11-08T09:00:01+00:00", "2026-11-08T09:16:15+00:00"),
        ("analysis_proxy", "2026-11-08T12:00:00+00:00", "2026-11-08T12:00:30+00:00"),
        ("render_export", "2026-11-08T09:00:00+00:00", "2026-11-08T09:12:25+00:00"),
    ]
    bursts = perf.bursts(rows, gap_seconds=60)
    assert [(b.kind, b.count, b.wall_seconds) for b in bursts] == [
        ("analysis_proxy", 2, 975.0),
        ("render_export", 1, 745.0),
        ("analysis_proxy", 1, 30.0),
    ]


def test_a_job_created_inside_the_gap_extends_the_burst() -> None:
    rows = [
        ("visual_prepare", "2026-11-08T09:00:00+00:00", "2026-11-08T09:00:10+00:00"),
        ("visual_prepare", "2026-11-08T09:00:50+00:00", "2026-11-08T09:01:00+00:00"),
        ("visual_prepare", "2026-11-08T09:01:40+00:00", "2026-11-08T09:02:00+00:00"),
    ]
    (burst,) = perf.bursts(rows, gap_seconds=60)
    assert (burst.count, burst.wall_seconds) == (3, 120.0)


def test_request_spans_measure_each_jobs_stage_from_first_to_last_response() -> None:
    rows = [
        ("job_1", "luna", "2026-11-08 09:47:30"),
        ("job_1", "luna", "2026-11-08 09:47:40"),
        ("job_1", "luna", "2026-11-08 09:48:00"),
        ("job_1", "asr", "2026-11-08 09:47:04"),
        ("job_2", "luna", "2026-11-08 10:00:00"),
    ]
    spans = {s.stage: s for s in perf.request_spans(rows)}
    luna = spans["luna"]
    assert (luna.jobs, luna.requests) == (2, 4)
    assert luna.median_gap_seconds == 15.0  # job_1의 간격 10·20초
    assert luna.max_span_seconds == 30.0
    assert spans["asr"].jobs == 1 and spans["asr"].median_gap_seconds is None


def test_the_report_reads_the_database_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "project.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs (job_id TEXT, kind TEXT, status TEXT, created_at TEXT,"
            " updated_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE analysis_requests (job_id TEXT, stage TEXT, created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('j1', 'render_export', 'succeeded',"
            " '2026-11-08T09:00:00+00:00', '2026-11-08T09:12:25+00:00')"
        )
    before = database.read_bytes()
    text = perf.report(database, gap_seconds=60)
    assert "render_export" in text and "745" in text
    assert database.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        perf._connect(database).execute("CREATE TABLE nope (x)")
