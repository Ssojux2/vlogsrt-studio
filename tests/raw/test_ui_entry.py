"""새 진입점(ui/app.py)의 실행 계약 — 다른 UI를 죽이지 않고 옆으로 비켜 나간다.

이전 단일 화면 시대의 `stop_stale_ui` 자동 정리는 작업의 정본을 소유하는 이
서버에는 맞지 않는다(코디네이터 재결) — 실행 중인 다른 UI가 진행 중 작업을
들고 있을 수 있다. 레거시 도우미 자체는 `ui/service.py`에 그대로 남아 있다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,  # 진짜 부재(ModuleNotFoundError)와 차단 수입(ImportError) 둘 다 건너뛴다
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)
pytest.importorskip(
    "uvicorn",
    reason="웹 UI extra(uvicorn)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from ui import app as ui_app  # noqa: E402
from ui import service as legacy_service  # noqa: E402


@pytest.fixture(autouse=True)
def synthetic_ui_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from ui.instance import UiInstance

    monkeypatch.setattr(ui_app, "UiInstance", lambda project, port: UiInstance(tmp_path, port))
    state = tmp_path / "ui-state.json"
    monkeypatch.setenv("VLOGSRT_RAW_UI_STATE", str(state))
    return state


class _StubUi:
    """uvicorn·gradio 없이 진입점 자체의 계약만 본다."""

    built: list[dict[str, int]] = []

    def __init__(self, port: int) -> None:
        self.port = port

    def startup(self) -> str:
        return "stub-startup"

    def build_fastapi(self) -> object:
        _StubUi.built.append({"port": self.port})
        return object()


def _patch_launch(monkeypatch: pytest.MonkeyPatch, chosen_port: int) -> dict[str, Any]:
    """진입점 밖의 세계를 가린다 — 죽임 여부만 실물 서명으로 지켜본다."""
    killed: list[int] = []
    monkeypatch.setattr(
        legacy_service,
        "stop_stale_ui",
        lambda port, wait_seconds=3.0: killed.append(port) or [],
    )
    monkeypatch.setattr(legacy_service, "pick_port", lambda preferred, attempts=20: chosen_port)
    launched: dict[str, Any] = {}
    monkeypatch.setattr(
        ui_app.uvicorn,
        "run",
        lambda app, **kwargs: launched.update(kwargs) or launched.update({"app_object": app}),
    )
    monkeypatch.setattr(ui_app, "webbrowser", SimpleNamespace(open=lambda url: None))
    monkeypatch.setattr(ui_app.workspace_service, "RawWorkspaceUi", _StubUi)
    return {"killed": killed, "launched": launched}


def test_the_entrypoint_refuses_an_occupied_port_without_killing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _patch_launch(monkeypatch, chosen_port=7860)
    attempts_seen = []

    def occupied(preferred: int, attempts: int = 20):
        attempts_seen.append(attempts)
        raise legacy_service.UiError("busy")

    monkeypatch.setattr(legacy_service, "pick_port", occupied)
    assert ui_app.main(["--port", "7860", "--no-browser"]) == 1
    assert attempts_seen == [1]
    assert world["killed"] == []
    assert world["launched"] == {}


def test_the_entrypoint_uses_the_preferred_port_when_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _patch_launch(monkeypatch, chosen_port=7860)
    assert ui_app.main(["--port", "7860", "--no-browser"]) == 0
    assert world["launched"]["port"] == 7860
    assert world["killed"] == []


def test_share_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_launch(monkeypatch, chosen_port=7860)
    assert ui_app.main(["--share"]) == 1


def test_shutting_down_instance_does_not_report_reuse_success(monkeypatch, tmp_path):
    from ui.instance import UiInstance

    world = _patch_launch(monkeypatch, chosen_port=7860)
    with UiInstance(tmp_path, 7860):
        monkeypatch.setattr(UiInstance, "wait_until_listening", lambda self: False)
        assert ui_app.main(["--no-browser"]) == 1
    assert world["launched"] == {}
    assert world["killed"] == []


@pytest.mark.parametrize(
    "arguments,expected", [([], "0"), (["--online"], "0"), (["--offline"], "1")]
)
def test_normal_launcher_mode_precedes_dotenv_without_changing_file(
    monkeypatch, synthetic_dotenv, arguments, expected
):
    import os

    from vlogsrt import env

    world = _patch_launch(monkeypatch, chosen_port=7860)
    dotenv = synthetic_dotenv
    dotenv.write_text("VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=sk-test-only\n")
    before = dotenv.read_bytes()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    modes = []
    original_init = _StubUi.__init__

    def capture(self, port):
        env.load_env(dotenv)  # normal loader; the startup override must win
        modes.append(os.environ["VLOGSRT_OFFLINE"])
        original_init(self, port)

    monkeypatch.setattr(_StubUi, "__init__", capture)
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    assert ui_app.main([*arguments, "--no-browser"]) == 0
    assert modes == [expected]
    assert dotenv.read_bytes() == before
    assert world["launched"]["port"] == 7860
    assert world["killed"] == []
