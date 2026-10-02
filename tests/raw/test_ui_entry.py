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


def test_the_entrypoint_never_kills_another_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    """포트가 차 있어도 죽이지 않고 옆 포트로 띄운다 — 작업 소유 서버 보존."""
    world = _patch_launch(monkeypatch, chosen_port=7861)
    code = ui_app.main(["--port", "7860", "--no-browser"])
    assert code == 0
    assert world["killed"] == []  # 자동 종료 경로는 아예 없다
    assert world["launched"]["host"] == "127.0.0.1"
    assert world["launched"]["port"] == 7861  # 비켜 간 포트
    assert _StubUi.built[-1]["port"] == 7861


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
