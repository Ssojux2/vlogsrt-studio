from __future__ import annotations

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_selection_analysis import make_server, wait_until
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.selection_analysis import load_state


def test_missing_key_keeps_a_visible_dotenv_notice_and_starts_nothing(tmp_path, monkeypatch):
    """키가 없으면 견적 그대로 멈추고 `.env` 안내를 보인다 — 화면에는 키 칸이 없다(D102)."""
    source = tmp_path / "approval.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server._live_transport = None
    server._startup_credentials = None
    server._selection_auto_approval = None
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    quote = server._quick["quote"]
    server.quick_approve_view(server.current_signature())
    message = server._quick_status()
    assert "연결 설정 필요" in message and "아직 분석하지 않았습니다" in message
    assert "`.env`" in message and "OPENAI_API_KEY" in message
    assert server._quick["quote"] is quote
    assert server._quick["stage"] == "승인 대기"
    assert load_state(server.raw, server.workspace_root())["connection_required"] is True
    for _ in range(2):
        assert "연결 설정 필요" in server.capcut_tick_view(server.current_signature())[0]
    assert not sender.calls and not client.calls
    assert server.raw.analysis_overview(server.workspace_root()) is None


def test_a_started_environment_key_reaches_runtime_and_clears_the_notice(tmp_path, monkeypatch):
    """`.env` 키로 다시 띄운 서버를 흉내 낸다 — 시작 환경의 키만 쓰고 기록에 남기지 않는다."""
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
    server.quick_approve_view(server.current_signature())
    assert server._quick["connection_required"] is True
    server._startup_credentials = raw_service.EnvironmentCredentials("sk-test-only", None)
    server.quick_approve_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert server._quick["connection_required"] is False and not server._quick.get("error")
    assert len(sender.calls) == 1 and len(client.calls) == 1
    assert "sk-test-only" not in str(load_state(server.raw, server.workspace_root()))
