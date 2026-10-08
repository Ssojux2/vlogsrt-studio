from __future__ import annotations

import pytest

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_selection_analysis import make_server, wait_until
from vlogsrt.project.raw.selection_analysis import load_state


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_key_keeps_visible_actionable_error_and_opens_input(tmp_path, monkeypatch, key):
    source = tmp_path / "approval.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server._live_transport = None
    server._startup_credentials = None
    server._selection_auto_approval = None
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    quote = server._quick["quote"]
    message, panel, key_input = server.quick_approve_panel_view(server.current_signature(), key)
    assert "연결 설정 필요" in message and "아직 분석하지 않았습니다" in message
    assert panel.open is True and key_input.autofocus is True and key_input.value == ""
    assert server._quick["quote"] is quote
    assert server._quick["stage"] == "승인 대기"
    assert load_state(server.raw, server.workspace_root())["connection_required"] is True
    for _ in range(2):
        assert "연결 설정 필요" in server.capcut_tick_view(server.current_signature())[0]
    assert not sender.calls and not client.calls
    assert server.raw.analysis_overview(server.workspace_root()) is None


def test_synthetic_key_reaches_runtime_and_clears_missing_key_state(tmp_path, monkeypatch):
    source = tmp_path / "approval.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    transport = server._live_transport
    server._live_transport = None
    server._startup_credentials = None
    server._selection_auto_approval = None
    monkeypatch.setattr(server, "_live_transport_for_action", lambda: transport)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    server.quick_approve_panel_view(server.current_signature(), None)
    _, panel, key_input = server.quick_approve_panel_view(
        server.current_signature(), " sk-test-only "
    )
    assert server._startup_credentials.api_key == "sk-test-only"  # explicit synthetic value
    assert panel.open is False and key_input.value == ""
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert server._quick["connection_required"] is False and not server._quick.get("error")
    assert len(sender.calls) == 1 and len(client.calls) == 1
    assert "sk-test-only" not in str(load_state(server.raw, server.workspace_root()))
