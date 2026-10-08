from __future__ import annotations

import json
import os
import threading

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_selection_analysis import make_server, wait_until
from vlogsrt.project.raw import analysis_workflow, browser_runtime


def test_browser_loads_dotenv_opaquely_and_keeps_explicit_offline(monkeypatch):
    from vlogsrt import env

    calls = []

    def load():
        calls.append(True)
        os.environ.setdefault("OPENAI_API_KEY", "sk-test-only")

    monkeypatch.setattr(env, "ensure_loaded", load)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VLOGSRT_OFFLINE", raising=False)
    credentials = browser_runtime.capture_browser_credentials()
    assert credentials and credentials.api_key == "sk-test-only"
    assert calls and "VLOGSRT_OFFLINE" not in os.environ
    browser_runtime.prepare_approved_browser_network()
    assert os.environ["VLOGSRT_OFFLINE"] == "0"
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    browser_runtime.prepare_approved_browser_network()
    assert os.environ["VLOGSRT_OFFLINE"] == "1"


def test_source_captions_visible_before_frames_and_luna(tmp_path, monkeypatch):
    source = tmp_path / "first-captions.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    entered, release = threading.Event(), threading.Event()
    original = analysis_workflow._scene_plan

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis_workflow, "_scene_plan", hold)
    server.capcut_choose_view(server.current_signature())
    try:
        assert entered.wait(20), server._quick
        assert len(sender.calls) == 1 and not client.calls
        status, _, workbench = server.capcut_tick_view(server.current_signature())
        assert "원본 자막 1개 표시" in status
        assert "영상 프레임 준비 중 (0/최대 8)" in status
        assert "합성 영상입니다." in workbench and "subtitles" in workbench
    finally:
        release.set()
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    text = " ".join(
        c["text"]
        for m in client.calls[0]["input"]
        for c in m["content"]
        if c["type"] == "input_text"
    )
    assert "합성 영상입니다." in text and "프레임 증거" in text


def test_reopen_offline_failure_prepares_quote_without_paid_resume(tmp_path, monkeypatch):
    from ui.workspace_service import RawWorkspaceUi
    from vlogsrt.project.raw.service import RawWorkspaceService

    root = tmp_path / "project"
    service = RawWorkspaceService()
    service.create_workspace(root, "saved project")
    state = tmp_path / "ui-state.json"
    state.write_text(json.dumps({"workspace_root": str(root)}))
    saved = {
        "asset_ids": ["selected-source"],
        "error": "VLOGSRT_OFFLINE이 오프라인이다",
        "stage": "분석 확인 필요",
        "token": "saved-token",
        "root": root,
    }
    monkeypatch.setattr(RawWorkspaceService, "load_selection_state", lambda *args: dict(saved))
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    prepared = []
    monkeypatch.setattr(
        RawWorkspaceUi,
        "_start_quick",
        # No `approved_quote` parameter: a paid resume would raise TypeError here.
        lambda self, selected_root, paths, asset_ids, retry=False: prepared.append(
            (selected_root, paths, asset_ids, retry)
        ),
    )
    ui = RawWorkspaceUi(state_path=state)
    ui.startup()
    assert prepared == [(root.resolve(), (), ("selected-source",), True)]
    assert ui._selection_auto_approval is None
    assert not service.list_jobs(root)


def test_explicit_offline_reopen_does_not_prepare_or_send(tmp_path, monkeypatch):
    from ui.workspace_service import RawWorkspaceUi
    from vlogsrt.project.raw.service import RawWorkspaceService

    root = tmp_path / "project"
    service = RawWorkspaceService()
    service.create_workspace(root, "offline project")
    state = tmp_path / "ui-state.json"
    state.write_text(json.dumps({"workspace_root": str(root)}))
    monkeypatch.setattr(
        RawWorkspaceService,
        "load_selection_state",
        lambda *args: {"asset_ids": ["selected-source"], "error": "VLOGSRT_OFFLINE이 오프라인이다"},
    )
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: True)
    prepared = []
    monkeypatch.setattr(
        RawWorkspaceUi, "_start_quick", lambda *args, **kwargs: prepared.append(True)
    )
    ui = RawWorkspaceUi(state_path=state)
    ui.startup()
    assert not prepared and not service.list_jobs(root)
