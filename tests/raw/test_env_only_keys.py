"""W-49 / D102 — API 키는 `.env`에서만 읽는다. 화면에는 키 입력 칸이 없다(유료 호출 0회).

2026-10-28 사용자 지시: 아직 상용으로 내놓지 않으므로 키는 사람이 `.env`에 직접 넣는다. 그래서
설정 서랍의 키 칸 · 「API 연결 열기」 단추 · 키 칸에 초점을 주는 흐름이 없고, 승인 문은 화면이 보낸
키를 받지 않는다. 키가 없으면 견적 → 승인 문(D44·D54)은 그대로 멈추고 `.env` 안내를 보인다.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import gradio as gr
import pytest

from tests.raw.test_analysis_model_ui import bare_server
from tests.raw.test_background_library import setup_library
from ui import ui_facade, workspace_service
from ui.workspace_service import RawWorkspaceUi
from vlogsrt.project.raw import service as raw_service

OPENAI_ENV = "OPENAI_API_KEY"
SECRET = "sk-openai-synthetic-never-shown"
ENV_HINT = "`.env`"


def _ids(app: gr.Blocks) -> dict:
    return {getattr(block, "elem_id", None): block for block in app.blocks.values()}


def test_the_screen_has_no_old_key_input(tmp_path: Path, local_socket: None) -> None:
    """옛 「이 실행에서만」 키 칸은 없다 — 키 칸은 `.env`가 없을 때의 「API 키」 판뿐이다(D183)."""
    app = bare_server(tmp_path).build_blocks()
    by_id = _ids(app)
    assert "quick-api-key" not in by_id and "quick-api-panel" not in by_id
    # 분석 모델 · 전사 엔진 칸은 그대로 서랍에 남는다 — 키 칸이 있던 판의 이름만 바뀐다.
    panel = by_id["analysis-model-panel"]
    assert isinstance(panel, gr.Accordion) and "API" not in str(panel.label)
    assert by_id["analysis-model"].parent.parent is panel
    assert ENV_HINT in str(by_id["analysis-model-status"].value)


def test_the_approval_click_sends_no_key(tmp_path: Path, local_socket: None) -> None:
    server = bare_server(tmp_path)
    app = server.build_blocks()
    button = server._library_approve_button
    [dep] = [
        dep
        for dep in app.config["dependencies"]
        if any(target == button._id for target, _ in dep["targets"])
    ]
    inputs = [app.blocks[i] for i in dep["inputs"]]
    assert not any(isinstance(block, gr.Textbox) for block in inputs)
    assert len(inputs) == 1  # 작업공간 서명뿐 — 견적 문구가 없다(D110)


def test_handlers_take_no_key_argument() -> None:
    for handler in (RawWorkspaceUi.library_analysis_view, RawWorkspaceUi.quick_approve_view):
        assert not any("key" in name for name in inspect.signature(handler).parameters)
    assert not hasattr(RawWorkspaceUi, "quick_approve_panel_view")


def test_the_shell_never_opens_or_focuses_a_key_field() -> None:
    assert not hasattr(ui_facade, "KEY_NOTICE_JS")
    assert "vl-activity-key" not in ui_facade.activity_html()
    for script in (ui_facade.SHELL_JS, ui_facade.APPROVE_WAKE_JS):
        assert "openKey" not in script and "quick-api" not in script
        assert "apiKey" not in script
    assert ui_facade.APPROVE_WAKE_JS.startswith("(signature) =>")


def test_no_screen_text_points_at_an_api_panel() -> None:
    for module in (workspace_service, ui_facade):
        assert "API 연결" not in inspect.getsource(module)


def test_a_missing_key_says_to_fill_dotenv_and_starts_nothing(tmp_path, monkeypatch) -> None:
    ui, sender, client, root, _ids_ = setup_library(tmp_path, monkeypatch)
    ui._live_transport = None
    ui._startup_credentials = None
    reply = ui.library_analysis_view(ui.current_signature())
    assert isinstance(reply, str)
    assert "연결 설정 필요" in reply and ENV_HINT in reply and OPENAI_ENV in reply
    assert "아직 분석하지 않았습니다" in reply
    assert ENV_HINT in ui.capcut_status_view(ui.current_signature())
    assert not sender.calls and not client.calls


def test_the_model_status_names_the_openai_variable_but_never_the_value(tmp_path: Path) -> None:
    server = bare_server(tmp_path)
    server._startup_credentials = raw_service.EnvironmentCredentials(SECRET, None)
    present = server.analysis_model_status()
    assert OPENAI_ENV in present and "확인됨" in present and SECRET not in present
    server._startup_credentials = None
    missing = server.analysis_model_status()
    assert OPENAI_ENV in missing and "없음" in missing and ENV_HINT in missing


# ─── `.env`가 없을 때만 화면에서 키를 넣는다(W-109, D183 — D102를 이 경우만 뒤집는다) ─────────


def _ancestors(block: object) -> list[object]:
    out = []
    node = getattr(block, "parent", None)
    while node is not None:
        out.append(node)
        node = getattr(node, "parent", None)
    return out


def test_key_inputs_live_only_in_the_api_key_panel_shown_without_dotenv(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키 칸은 「API 키」 판 안에만 있고, 그 판은 `.env`가 없을 때만 보인다
    (있으면 그 파일을 쓴다)."""
    monkeypatch.setattr(RawWorkspaceUi, "_env_file_missing", lambda self: False)
    by_id = _ids(bare_server(tmp_path).build_blocks())
    panel = by_id[workspace_service.API_KEY_PANEL_ID]
    assert isinstance(panel, gr.Accordion) and panel.visible is False
    passwords = [
        block
        for block in by_id.values()
        if isinstance(block, gr.Textbox) and block.type == "password"
    ]
    assert len(passwords) == len(workspace_service.API_KEY_NAMES)
    assert all(panel in _ancestors(block) for block in passwords)
    assert all(not block.value for block in passwords)  # 값을 화면에 싣지 않는다
    monkeypatch.setattr(RawWorkspaceUi, "_env_file_missing", lambda self: True)
    shown = _ids(bare_server(tmp_path / "again").build_blocks())
    assert shown[workspace_service.API_KEY_PANEL_ID].visible is True


def _isolated_keys(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """진짜 `.env`·환경을 건드리지 않게 — 찾기는 이 시험의 파일만, 키 변수는 끝나면 되돌린다."""
    from tests._dotenv_guard import allow_dotenv_under
    from vlogsrt import env

    allow_dotenv_under(target.parent)  # 이 시험의 합성 `.env`만 — 진짜 파일은 보호 그대로
    monkeypatch.setattr(
        env, "find_env_file", lambda start=None: target if target.exists() else None
    )
    monkeypatch.setattr(env, "_loaded_from", None)
    monkeypatch.setattr(env, "_root", None)
    for name in workspace_service.API_KEY_NAMES:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def test_saving_keys_writes_a_new_private_dotenv_and_connects_now(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """저장하면 `.env`를 새로 만들고(덮지 않는다 · 이 사용자만 읽는 0600) 다시 띄우지 않아도 바로
    연결한다. 값은 상태 줄·알림 어디에도 나오지 않는다. 그 뒤로는 `.env`가 있으니 다시 받지
    않는다."""
    import os
    import stat

    target = tmp_path / ".env"
    _isolated_keys(monkeypatch, target)
    server = bare_server(tmp_path)
    server.build_blocks()
    monkeypatch.setattr(server, "_new_env_path", lambda: target)
    server._startup_credentials = server._startup_gemini = server._startup_claude = None
    hidden, *cleared, status = server.api_keys_save_view(f" {SECRET} ", "AIza-synthetic", "")
    assert target.read_text(encoding="utf-8") == (
        f"OPENAI_API_KEY={SECRET}\nGEMINI_API_KEY=AIza-synthetic\n"
    )
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert server._startup_credentials is not None and server._startup_gemini is not None
    assert server._startup_claude is None
    assert hidden["visible"] is False and cleared == ["", "", ""]
    assert SECRET not in status and "확인됨" in status
    with pytest.raises(gr.Error, match="`.env`"):
        server.api_keys_save_view("sk-other", "", "")
    assert SECRET in target.read_text(encoding="utf-8")  # 덮지 않았다


def test_bad_or_empty_keys_write_nothing(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".env"
    _isolated_keys(monkeypatch, target)
    server = bare_server(tmp_path)
    monkeypatch.setattr(server, "_new_env_path", lambda: target)
    for values in (("", "", ""), ("sk-has space", "", ""), ('sk-"quoted"', "", "")):
        with pytest.raises(gr.Error, match="저장하지 못했습니다"):
            server.api_keys_save_view(*values)
    assert not target.exists()


def test_the_key_status_points_at_the_panel_only_without_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = bare_server(tmp_path)
    monkeypatch.setattr(server, "_env_file_missing", lambda: True)
    assert workspace_service.API_KEY_MISSING_NOTE in server.analysis_model_status()
    monkeypatch.setattr(server, "_env_file_missing", lambda: False)
    assert workspace_service.API_KEY_MISSING_NOTE not in server.analysis_model_status()
