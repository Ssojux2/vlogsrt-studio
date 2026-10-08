from __future__ import annotations

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_selection_analysis import SuitabilityClient, make_server, wait_until
from ui.workspace_service import RawWorkspaceUi


def setup_library(tmp_path, monkeypatch):
    a, b = tmp_path / "a.mov", tmp_path / "b.mov"
    _make_media(a, audio=True, seconds="32")
    _make_media(b, audio=True, seconds="3")
    client = SuitabilityClient(pause_second=True)
    ui, sender, client = make_server(tmp_path, monkeypatch, a, client)
    ui._selection_auto_approval = None
    root = ui.workspace_root()
    for source in (a, b):
        h = ui.raw.import_selected_video(root, source)
        assert _wait_terminal(ui.raw, root, h.job_id).status == "succeeded"
    ui.raw.ensure_source_order(root)
    ids = {a.source_name: a.asset_id for a in ui.raw.list_source_assets(root)}
    ui._start_quick(root, (), asset_ids=(ids["a.mov"],))
    wait_until(lambda: ui._quick.get("stage") == "승인 대기")
    ui._prepare_library_quote(root)
    wait_until(lambda: root in ui._library_quotes)
    return ui, sender, client, root, ids


def test_one_library_approval_queues_sources_and_navigation_preserves_results(
    tmp_path, monkeypatch
):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    assert len(ui._library_quotes[root].asset_ids) == 2
    assert not sender.calls and not client.calls
    ui.library_analysis_view(ui.current_signature())
    assert client.second_entered.wait(20)
    a_task = ui._task_for(root, (ids["a.mov"],))
    ui.quick_source_view(ui.current_signature(), ids["b.mov"])
    b_task = ui._quick
    try:
        wait_until(lambda: b_task.get("stage") == "분석 대기")
        assert not ui.raw.get_job(root, a_task["job_id"]).cancellation_requested
        assert len(sender.calls) == 1
    finally:
        client.resume.set()
    wait_until(
        lambda: (
            a_task.get("stage") == "추천 구간 준비 완료"
            and b_task.get("stage") == "추천 구간 준비 완료"
        )
    )
    assert ui._quick is b_task and ui._quick["view_source_id"] == ids["b.mov"]
    assert len(sender.calls) == 2 and len(client.calls) == 4
    facade = ui.raw.review_workspace_facade(root)
    head = facade.load_head().revision
    b_segment = next(s for s in head.segments if s.source_id == ids["b.mov"])
    facade.set_selection(head.base_plan_id, b_segment.segment_id, "excluded")
    ui.quick_source_view(ui.current_signature(), ids["a.mov"])
    if ui._quick.get("review_job_id"):
        assert _wait_terminal(ui.raw, root, ui._quick["review_job_id"]).status == "succeeded"
    assert all(
        s.selection == "excluded"
        for s in facade.load_head().revision.segments
        if s.source_id == ids["b.mov"]
    )
    assert len(sender.calls) == 2 and len(client.calls) == 4
    reopened = RawWorkspaceUi(state_path=tmp_path / "reopened-state.json", live=ui._live_transport)
    reopened.open_workspace_view(str(root))
    reopened.quick_source_view(reopened.current_signature(), ids["a.mov"])
    assert reopened._quick["stage"] == "추천 구간 준비 완료"
    assert len(sender.calls) == 2 and len(client.calls) == 4


def test_cancel_queued_selected_source_keeps_other_source_running(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    ui.library_analysis_view(ui.current_signature())
    assert client.second_entered.wait(20)
    a_task = ui._task_for(root, (ids["a.mov"],))
    ui.quick_source_view(ui.current_signature(), ids["b.mov"])
    b_task = ui._quick
    try:
        wait_until(lambda: b_task.get("stage") == "분석 대기")
        ui.capcut_cancel_view(ui.current_signature())
        assert not ui.raw.get_job(root, a_task["job_id"]).cancellation_requested
    finally:
        client.resume.set()
    assert _wait_terminal(ui.raw, root, a_task["job_id"]).status == "succeeded"
    assert _wait_terminal(ui.raw, root, b_task["job_id"]).status == "cancelled"
    assert len(sender.calls) == 1 and len(client.calls) == 3
    assert a_task["stage"] == "추천 구간 준비 완료" and b_task["stage"] == "취소됨"
