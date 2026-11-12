"""W-49 / D102 — API 키는 `.env`에서만 읽는다. 화면에는 키 입력 칸이 없다(유료 호출 0회).

2026-10-28 사용자 지시: 아직 상용으로 내놓지 않으므로 키는 사람이 `.env`에 직접 넣는다. 그래서
설정 서랍의 키 칸 · 「API 연결 열기」 단추 · 키 칸에 초점을 주는 흐름이 없고, 승인 문은 화면이 보낸
키를 받지 않는다. 키가 없으면 견적 → 승인 문(D44·D54)은 그대로 멈추고 `.env` 안내를 보인다.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import gradio as gr

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


def test_the_screen_has_no_key_input(tmp_path: Path, local_socket: None) -> None:
    app = bare_server(tmp_path).build_blocks()
    by_id = _ids(app)
    assert "quick-api-key" not in by_id and "quick-api-panel" not in by_id
    passwords = [
        block
        for block in app.blocks.values()
        if isinstance(block, gr.Textbox) and block.type == "password"
    ]
    assert passwords == []
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
