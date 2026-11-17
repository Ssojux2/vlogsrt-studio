"""라이브러리 동시 분석 — 한 번의 승인으로 남은 영상을 몇 개씩 함께 분석한다(D49).

동시에 도는 수만 바뀌고, 영상마다의 승인 견적·유료 호출 수는 직렬일 때와 같아야 한다.
"""

from __future__ import annotations

import threading
import time

import pytest

from tests.raw.test_background_library import setup_library
from tests.raw.test_selection_analysis import wait_until
from ui.workspace_service import RawWorkspaceUi
from vlogsrt.project.raw.analysis_config import (
    ANALYSIS_CONCURRENCY,
    RawAnalysisConcurrency,
    validate_analysis_concurrency,
)
from vlogsrt.project.raw.models import ACTIVE_JOB_STATUSES
from vlogsrt.project.raw.service import RawWorkspaceError

GATE_SECONDS = 30
TICKS = 4
TICK_SECONDS = 0.15
DONE = "추천 구간 준비 완료"
FAILED = "분석 확인 필요"
CANCELLED = "취소됨"
WAITING = "분석 대기"
EXTRA = ("c.mov", "d.mov")
# 32초 a는 전사 1조각·Luna 창 3개, 3–5초짜리 b·c·d는 각각 1조각·1창이다.
FOUR_VIDEO_LUNA_CALLS = 3 + 1 + 1 + 1


class Gate:
    """선택 분석 실행 앞의 문 — 들어온 순서를 적고, 문이 열릴 때까지 붙잡는다."""

    def __init__(self, ui, fail: tuple[str, ...] = (), closed: bool = True) -> None:
        self.entered: list[str] = []
        self.quotes: list = []
        self.open = threading.Event()
        if not closed:
            self.open.set()
        self._fail = set(fail)
        real = ui.raw.run_selection_analysis

        def gated(root, quote, transport, context, report):
            self.entered.append(quote.asset_ids[0])
            self.quotes.append(quote)
            assert self.open.wait(GATE_SECONDS), "test did not open the gate"
            if quote.asset_ids[0] in self._fail:
                raise RawWorkspaceError("합성 실패")
            return real(root, quote, transport, context, report)

        ui.raw.run_selection_analysis = gated


def _stage(ui, root, source_id: str) -> str | None:
    return (ui._task_for(root, (source_id,)) or {}).get("stage")


def _all_at(ui, root, ids, stage: str) -> bool:
    return all(_stage(ui, root, i) == stage for i in ids)


def _settled(ui, root, ids) -> bool:
    return all(_stage(ui, root, i) in (DONE, FAILED, CANCELLED) for i in ids)


def _approval_jobs(ui, root) -> list:
    return [j for j in ui.raw.list_jobs(root, limit=100) if j.kind == "library_approval"]


def test_admission_is_bounded_by_the_limit(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=EXTRA, parallel_analysis_limit=2
    )
    client.resume.set()
    gate = Gate(ui)
    try:
        ui.library_analysis_view(ui.current_signature())
        wait_until(lambda: len(gate.entered) == 2 and len(ui._analysis_queue) == 2)
        for _ in range(TICKS):
            assert len(gate.entered) == 2 and len(ui._analysis_active) == 2
            assert len(ui._analysis_queue) == 2
            time.sleep(TICK_SECONDS)
    finally:
        gate.open.set()
    wait_until(lambda: _all_at(ui, root, ids.values(), DONE))
    assert len(sender.calls) == 4 and len(client.calls) == FOUR_VIDEO_LUNA_CALLS
    assert sorted(gate.entered) == sorted(ids.values())


def test_admission_is_fifo_in_fan_out_order(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=("c.mov",), parallel_analysis_limit=1
    )
    client.resume.set()
    gate = Gate(ui, closed=False)
    ui.library_analysis_view(ui.current_signature())
    wait_until(lambda: _all_at(ui, root, ids.values(), DONE))
    assert tuple(gate.entered) == ui._library_ids(root)


def test_one_failure_is_isolated(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=EXTRA, parallel_analysis_limit=2
    )
    client.resume.set()
    gate = Gate(ui, fail=(ids["b.mov"],), closed=False)
    ui.library_analysis_view(ui.current_signature())
    wait_until(lambda: _settled(ui, root, ids.values()))
    b_task = ui._task_for(root, (ids["b.mov"],))
    assert b_task["stage"] == FAILED and b_task.get("error")
    assert _all_at(ui, root, (i for n, i in ids.items() if n != "b.mov"), DONE)
    assert sorted(gate.entered) == sorted(ids.values())
    wait_until(lambda: not ui._analysis_active and not ui._analysis_queue)
    button = ui.library_analysis_button()
    assert button.interactive and button.value != "분석 진행 중"


def test_cancel_all_stops_running_and_waiting(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=("c.mov",), parallel_analysis_limit=1
    )
    client.resume.set()
    gate = Gate(ui)
    signature = ui.current_signature()
    try:
        ui.library_analysis_view(signature)
        wait_until(lambda: len(gate.entered) == 1)
        waiting = [i for i in ids.values() if i not in gate.entered]
        wait_until(lambda: _all_at(ui, root, waiting, WAITING))
        tasks = [ui._task_for(root, (i,)) for i in ids.values()]
        reply = ui.capcut_cancel_all_view(signature)
        assert "전체 분석 취소" in reply
    finally:
        gate.open.set()
    for task in tasks:
        wait_until(lambda task=task: ui.raw.get_job(root, task["job_id"]).status == "cancelled")
        assert task["stage"] == CANCELLED
    assert len(sender.calls) <= 1
    wait_until(lambda: not ui._analysis_active and not ui._analysis_queue)


def _calls_for_one_run(tmp_path, monkeypatch, limit: int) -> tuple[int, int, list, object]:
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=("c.mov",), parallel_analysis_limit=limit
    )
    client.resume.set()
    measured = ui.raw.selection_quote(root, ui._library_ids(root))
    gate = Gate(ui, closed=False)
    ui.library_analysis_view(ui.current_signature())
    wait_until(lambda: _all_at(ui, root, ids.values(), DONE))
    digests = [
        (q, ui.raw.selection_rebound_quote(root, measured, tuple(q.asset_ids)).digest)
        for q in gate.quotes
    ]
    return len(sender.calls), len(client.calls), digests, ids


def test_parallel_keeps_the_per_video_cost_gate(tmp_path, monkeypatch):
    serial = _calls_for_one_run(tmp_path / "serial", monkeypatch, 1)
    parallel = _calls_for_one_run(tmp_path / "parallel", monkeypatch, 3)
    assert parallel[:2] == serial[:2]
    quotes = parallel[2]
    assert len(quotes) == len(parallel[3])
    for quote, rebound_digest in quotes:
        assert len(quote.asset_ids) == 1 and quote.approved
        assert quote.digest == rebound_digest


def test_status_line_counts_running_waiting_done(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=("c.mov",), parallel_analysis_limit=2
    )
    client.resume.set()
    gate = Gate(ui)
    signature = ui.current_signature()
    try:
        ui.library_analysis_view(signature)
        wait_until(lambda: len(gate.entered) == 2 and len(ui._analysis_queue) == 1)
        status = str(ui.capcut_status_view(signature))
        assert "2개 동시 분석 중" in status and "대기 1" in status and "완료 0/3" in status
    finally:
        gate.open.set()
    wait_until(lambda: _all_at(ui, root, ids.values(), DONE))
    wait_until(lambda: "동시 분석 중" not in str(ui.capcut_status_view(signature)))


def test_button_states_during_parallel_run(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch, parallel_analysis_limit=2)
    client.resume.set()
    idle = ui.library_analysis_button()
    assert idle.value == "전체 영상 분석 승인" and idle.interactive
    gate = Gate(ui)
    try:
        ui.library_analysis_view(ui.current_signature())
        wait_until(lambda: len(gate.entered) == 2)
        running = ui.library_analysis_button()
        assert running.value == "분석 진행 중" and not running.interactive
    finally:
        gate.open.set()
    wait_until(lambda: _all_at(ui, root, ids.values(), DONE))
    done = wait_until(lambda: (b := ui.library_analysis_button()).value == "분석 완료" and b)
    assert not done.interactive


def test_double_approval_and_click_during_parallel_run_runs_each_video_once(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=EXTRA, parallel_analysis_limit=2
    )
    client.resume.set()
    gate = Gate(ui)
    signature = ui.current_signature()
    try:
        ui.library_analysis_view(signature)
        ui.library_analysis_view(signature)
        wait_until(lambda: len(gate.entered) == 2)
        waiting = [i for i in ids.values() if i not in gate.entered]
        wait_until(lambda: _all_at(ui, root, waiting, WAITING))
        ui.quick_source_view(signature, waiting[-1])
        assert ui._quick["stage"] == WAITING
    finally:
        gate.open.set()
    wait_until(lambda: _all_at(ui, root, ids.values(), DONE))
    assert sorted(gate.entered) == sorted(ids.values())
    assert len(sender.calls) == 4 and len(client.calls) == FOUR_VIDEO_LUNA_CALLS
    assert len(_approval_jobs(ui, root)) == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "3"])
def test_concurrency_setting_rejects_non_positive_or_non_int(limit):
    with pytest.raises(ValueError):
        validate_analysis_concurrency(RawAnalysisConcurrency(parallel_analysis_limit=limit))


def test_default_concurrency_is_three_and_valid():
    assert ANALYSIS_CONCURRENCY.parallel_analysis_limit == 3
    validate_analysis_concurrency(ANALYSIS_CONCURRENCY)
    validate_analysis_concurrency(RawAnalysisConcurrency(parallel_analysis_limit=1))


def test_constructor_rejects_zero_limit(tmp_path):
    with pytest.raises(ValueError):
        RawWorkspaceUi(state_path=tmp_path / "state.json", parallel_analysis_limit=0)


def test_cancel_during_fan_out_starts_no_further_videos(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(
        tmp_path, monkeypatch, extra=EXTRA, parallel_analysis_limit=1
    )
    client.resume.set()
    starts: list = []
    real = ui._start_quick

    def cancelling(*args, **kwargs):
        # 첫 영상을 띄우는 순간 승인 작업을 취소한다 — 나머지 영상은 띄워지면 안 된다.
        if kwargs.get("approved_quote") is not None and not starts:
            ui.raw.cancel_job(root, wait_until(lambda: ui._approval_jobs.get(root)))
        starts.append(kwargs.get("asset_ids"))
        return real(*args, **kwargs)

    monkeypatch.setattr(ui, "_start_quick", cancelling)
    signature = ui.current_signature()
    ui.library_analysis_view(signature)
    job = wait_until(
        lambda: next(
            (j for j in _approval_jobs(ui, root) if j.status not in ACTIVE_JOB_STATUSES), None
        )
    )
    assert job.status == "cancelled"
    first = ui._library_ids(root)[0]
    assert starts == [(first,)]
    status = str(ui.capcut_status_view(signature))
    assert "시작하지 못했습니다" not in status and "승인 접수" not in status
    wait_until(lambda: _stage(ui, root, first) == DONE)
    assert len(sender.calls) == 1


def test_failed_submit_does_not_leave_a_stuck_queue_head(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch, parallel_analysis_limit=1)
    client.resume.set()
    real = ui.raw.submit_job

    def broken(job_root, kind, *args, **kwargs):
        if kind == "selection_analysis":
            raise RawWorkspaceError("합성 기록 실패")
        return real(job_root, kind, *args, **kwargs)

    ui.raw.submit_job = broken
    with pytest.raises(RawWorkspaceError):
        ui._start_quick(
            root,
            (),
            asset_ids=(ids["a.mov"],),
            approved_quote=ui.raw.selection_quote(root, ui._library_ids(root)),
            activate=False,
        )
    assert not ui._analysis_queue
    ui.raw.submit_job = real
    ui.library_analysis_view(ui.current_signature())
    wait_until(lambda: _all_at(ui, root, ids.values(), DONE))
    assert len(sender.calls) == 2
