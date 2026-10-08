"""라이브러리 일괄 승인 — 클릭은 즉시 돌아오고, 느린 확인은 백그라운드 작업이 맡는다."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from tests.raw.test_background_library import setup_library
from tests.raw.test_selection_analysis import wait_until
from vlogsrt.project.raw.jobs import JobStore
from vlogsrt.project.raw.models import ACTIVE_JOB_STATUSES
from vlogsrt.project.raw.workspace import RawWorkspace

SLOW_QUOTE_SECONDS = 0.6
CLICK_BUDGET_SECONDS = 0.4
ACCEPTED = "승인 접수"
AGAIN = "다시 승인"
STARTED = "승인한 영상들을 백그라운드에서"
QUICK_STARTED = "선택한 영상의 Luna 분석을 시작했습니다"
TICKS = 3
TICK_SECONDS = 0.2
GATE_SECONDS = 30
FAILED = "시작하지 못했습니다"
RESTART = "앱을 다시 시작하면"


def _slow_quotes(ui, calls: list, delay: float = SLOW_QUOTE_SECONDS) -> None:
    real = ui.raw.selection_quote

    def slow(root, ids):
        calls.append(tuple(ids))
        time.sleep(delay)
        return real(root, ids)

    ui.raw.selection_quote = slow


def _count_starts(ui, monkeypatch) -> list:
    starts: list = []
    real = ui._start_quick

    def counting(*args, **kwargs):
        starts.append(kwargs.get("asset_ids"))
        return real(*args, **kwargs)

    monkeypatch.setattr(ui, "_start_quick", counting)
    return starts


def _approval_jobs(ui, root) -> list:
    return [j for j in ui.raw.list_jobs(root, limit=100) if j.kind == "library_approval"]


def test_click_returns_before_the_slow_quote_and_status_shows_acceptance(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    calls: list = []
    _slow_quotes(ui, calls)
    signature = ui.current_signature()
    started = time.monotonic()
    reply = ui.library_analysis_view(signature)
    elapsed = time.monotonic() - started
    assert elapsed < CLICK_BUDGET_SECONDS, elapsed
    assert ACCEPTED in reply[0]
    assert ACCEPTED in ui.capcut_status_view(signature)
    wait_until(lambda: len(sender.calls) == 2 and len(client.calls) == 4)


def test_digest_change_in_background_starts_nothing_and_keeps_notice(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    real = ui.raw.selection_quote
    ui.raw.selection_quote = lambda r, a: replace(real(r, a), digest="changed")
    starts = _count_starts(ui, monkeypatch)
    signature = ui.current_signature()
    ui.library_analysis_view(signature)
    wait_until(lambda: AGAIN in str(ui.capcut_status_view(signature)))
    wait_until(lambda: not ui._approval_active(root))
    for _ in range(TICKS):
        assert AGAIN in ui.capcut_status_view(signature)
        time.sleep(TICK_SECONDS)
    assert not starts and not sender.calls and not client.calls
    assert ui._library_quotes[root].digest == "changed"
    assert ui.raw.settings(root).network_mode != "online"


def test_double_click_while_approval_is_active_submits_once(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    calls: list = []
    _slow_quotes(ui, calls)
    starts = _count_starts(ui, monkeypatch)
    signature = ui.current_signature()
    ui.library_analysis_view(signature)
    ui.library_analysis_view(signature)
    button = ui.library_analysis_button()
    assert button.value == "분석 진행 중" and not button.interactive
    wait_until(lambda: len(sender.calls) == 2 and len(client.calls) == 4)
    assert len(_approval_jobs(ui, root)) == 1
    assert sorted(starts) == sorted((i,) for i in ids.values())
    assert calls.count(tuple(ui._library_quotes[root].asset_ids)) == 1


def test_success_runs_one_selection_analysis_per_video(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    ui.library_analysis_view(ui.current_signature())
    wait_until(
        lambda: all(
            (ui._task_for(root, (i,)) or {}).get("stage") == "추천 구간 준비 완료"
            for i in ids.values()
        )
    )
    assert len(sender.calls) == 2 and len(client.calls) == 4
    assert ui.raw.settings(root).network_mode == "online"
    assert len({tuple(ui._task_for(root, (i,))["asset_ids"]) for i in ids.values()}) == 2


def test_status_timer_does_not_erase_the_approval_reply(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    ui._live_transport = None
    ui._startup_credentials = None
    signature = ui.current_signature()
    reply = ui.library_analysis_view(signature, "")[0]
    assert reply
    for _ in range(TICKS):
        assert reply in ui.capcut_status_view(signature)
        time.sleep(TICK_SECONDS)
    assert not sender.calls and not client.calls and not _approval_jobs(ui, root)


def test_rebound_quotes_match_fresh_quotes_across_the_online_switch(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    measured = ui._library_quotes[root]
    every = tuple(measured.asset_ids)
    assert ui.raw.selection_rebound_quote(root, measured, every) == measured
    for asset_id in ids.values():
        derived = ui.raw.selection_rebound_quote(root, measured, (asset_id,))
        assert derived == ui.raw.selection_quote(root, (asset_id,))
    # 승인 작업의 실제 순서: 오프라인 설정에서 잰 측정값을 온라인 전환 뒤에 다시 묶는다.
    ui.raw.update_settings(root, replace(measured.workflow, network_mode="online"))
    online = ui.raw.selection_quote(root, every)
    assert online.digest == measured.digest
    assert online.workflow == replace(measured.workflow, network_mode="online")
    assert ui.raw.selection_rebound_quote(root, measured, every) == online
    for asset_id in ids.values():
        derived = ui.raw.selection_rebound_quote(root, measured, (asset_id,))
        assert derived == ui.raw.selection_quote(root, (asset_id,))


def test_binding_change_after_measurement_starts_nothing(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    real = ui.raw.selection_rebound_quote

    def moved(r, measured, asset_ids):
        rebound = real(r, measured, asset_ids)
        if len(asset_ids) == len(measured.asset_ids):
            return replace(rebound, digest="moved")
        return rebound

    ui.raw.selection_rebound_quote = moved
    starts = _count_starts(ui, monkeypatch)
    signature = ui.current_signature()
    ui.library_analysis_view(signature)
    wait_until(lambda: AGAIN in str(ui.capcut_status_view(signature)))
    wait_until(lambda: not ui._approval_active(root))
    assert not starts and not sender.calls and not client.calls
    assert ui._library_quotes[root].digest == "moved"


def test_started_notice_stays_over_progress_until_analysis_begins(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    limit, ui._analysis_limit = ui._analysis_limit, 0
    signature = ui.current_signature()
    try:
        ui.library_analysis_view(signature)
        wait_until(lambda: STARTED in str(ui.capcut_status_view(signature)))
        for _ in range(TICKS):
            assert STARTED in ui.capcut_status_view(signature)
            time.sleep(TICK_SECONDS)
    finally:
        ui._analysis_limit = limit
    wait_until(lambda: len(sender.calls) == 2 and len(client.calls) == 4)
    assert STARTED not in ui.capcut_status_view(signature)


def test_single_video_approval_reply_survives_the_status_timer(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    limit, ui._analysis_limit = ui._analysis_limit, 0
    signature = ui.current_signature()
    try:
        reply = ui.quick_approve_view(signature, None)
        assert QUICK_STARTED in reply
        for _ in range(TICKS):
            assert QUICK_STARTED in ui.capcut_status_view(signature)
            time.sleep(TICK_SECONDS)
    finally:
        ui._analysis_limit = limit
    wait_until(lambda: ui._quick.get("stage") == "추천 구간 준비 완료")
    assert QUICK_STARTED not in ui.capcut_status_view(signature)


def _gated_quotes(ui, approval_gate, prepare_gate, calls: list) -> None:
    real = ui.raw.selection_quote

    def gated(root, ids):
        calls.append(tuple(ids))
        gate = approval_gate if len(ids) > 1 else prepare_gate
        assert gate.wait(GATE_SECONDS)
        return real(root, ids)

    ui.raw.selection_quote = gated


def _count_runs(ui) -> list:
    runs: list = []
    real = ui.raw.run_selection_analysis

    def counting(root, quote, *args, **kwargs):
        runs.append(tuple(quote.asset_ids))
        return real(root, quote, *args, **kwargs)

    ui.raw.run_selection_analysis = counting
    return runs


def test_click_during_approval_is_superseded_and_runs_once(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    approval_gate, prepare_gate = threading.Event(), threading.Event()
    calls: list = []
    _gated_quotes(ui, approval_gate, prepare_gate, calls)
    runs = _count_runs(ui)
    signature, b_id = ui.current_signature(), ids["b.mov"]
    try:
        ui.library_analysis_view(signature)
        ui.quick_source_view(signature, b_id)
        clicked = ui._quick
        wait_until(lambda: (b_id,) in calls)
        assert clicked["stage"] == "영상 준비" and clicked["view_source_id"] == b_id
        approval_gate.set()
        wait_until(lambda: (ui._task_for(root, (b_id,)) or clicked) is not clicked)
        # 옛 준비가 살아 있으면 자동 승인이 그것을 한 번 더 돌려 두 번 과금된다.
        ui._selection_auto_approval = lambda quote: True
    finally:
        approval_gate.set()
        prepare_gate.set()
    wait_until(lambda: ui.raw.get_job(root, clicked["job_id"]).status == "cancelled")
    wait_until(
        lambda: all(
            (ui._task_for(root, (i,)) or {}).get("stage") == "추천 구간 준비 완료"
            for i in ids.values()
        )
    )
    assert runs.count((b_id,)) == 1 and len(runs) == 2
    assert len(sender.calls) == 2 and len(client.calls) == 4
    assert ui._quick is ui._task_for(root, (b_id,)) and ui._quick["view_source_id"] == b_id


def test_cancelled_approval_is_not_reported_as_failure(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    gate = threading.Event()
    _gated_quotes(ui, gate, gate, [])
    starts = _count_starts(ui, monkeypatch)
    signature = ui.current_signature()
    try:
        ui.library_analysis_view(signature)
        ui.raw.cancel_job(root, ui._approval_jobs[root])
    finally:
        gate.set()
    job = wait_until(
        lambda: next(
            (j for j in _approval_jobs(ui, root) if j.status not in ACTIVE_JOB_STATUSES), None
        )
    )
    assert job.status == "cancelled"
    status = str(ui.capcut_status_view(signature))
    assert FAILED not in status and ACCEPTED not in status
    assert root not in ui._approval_notice
    assert not starts and not sender.calls and not client.calls


def test_reattach_to_a_dead_servers_approval_asks_for_restart(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    store = JobStore(RawWorkspace.open(root))
    record, _ = store.insert_deduplicated("library_approval", ui._library_quotes[root].digest, 0)
    store.update(record.job_id, status="running")
    starts = _count_starts(ui, monkeypatch)
    signature = ui.current_signature()
    ui.library_analysis_view(signature)
    assert root not in ui._approval_jobs
    for _ in range(TICKS):
        status = str(ui.capcut_status_view(signature))
        assert RESTART in status and ACCEPTED not in status
        time.sleep(TICK_SECONDS)
    button = ui.library_analysis_button()
    assert button.interactive and button.value != "분석 진행 중"
    assert not starts and not sender.calls and not client.calls
