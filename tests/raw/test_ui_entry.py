"""새 진입점(ui/app.py)의 실행 계약 — 다른 UI를 죽이지 않고 옆으로 비켜 나간다.

이전 단일 화면 시대의 `stop_stale_ui` 자동 정리는 작업의 정본을 소유하는 이
서버에는 맞지 않는다(코디네이터 재결) — 실행 중인 다른 UI가 진행 중 작업을
들고 있을 수 있다. 레거시 도우미 자체는 `ui/service.py`에 그대로 남아 있다.
"""

from __future__ import annotations

import os
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
def private_tmpdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """잠금 파일·진행 폴더·시작 청소가 모두 `$TMPDIR`을 본다.

    진짜 것(살아 있는 서버의 잠금·진행 폴더)은 만지지 않는다.
    """
    import tempfile

    directory = tmp_path / "tmpdir"
    directory.mkdir()
    monkeypatch.setenv("TMPDIR", str(directory))
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


@pytest.fixture(autouse=True)
def synthetic_ui_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_tmpdir: Path
) -> Path:
    from ui.instance import UiInstance

    monkeypatch.setattr(ui_app, "UiInstance", lambda project, port: UiInstance(tmp_path, port))
    state = tmp_path / "ui-state.json"
    monkeypatch.setenv("VLOGSRT_RAW_UI_STATE", str(state))
    return state


@pytest.fixture(autouse=True)
def warmed_pickers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """서버 시작의 선택 창 미리 만들기(D106)를 센다 — 시험은 실제 osacompile을 띄우지 않는다."""
    calls: list[str] = []
    monkeypatch.setattr(ui_app, "_start_picker_warmup", lambda: calls.append("warm"))
    return calls


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
    monkeypatch: pytest.MonkeyPatch, warmed_pickers: list[str]
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
    assert warmed_pickers == []


def test_the_entrypoint_uses_the_preferred_port_when_free(
    monkeypatch: pytest.MonkeyPatch, warmed_pickers: list[str]
) -> None:
    world = _patch_launch(monkeypatch, chosen_port=7860)
    assert ui_app.main(["--port", "7860", "--no-browser"]) == 0
    assert world["launched"]["port"] == 7860
    assert world["killed"] == []
    assert warmed_pickers == ["warm"]  # 서버를 띄울 때 선택 창 앱을 미리 만든다(D106)


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


def test_the_server_run_tees_the_terminal_and_turns_progress_on_only_while_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D61 — 서버가 도는 동안만 터미널 줄을 「기록」 판에 남기고 ffmpeg 진행 파일을 켠다."""
    import sys

    from ui import activity_log
    from vlogsrt import preprocess

    _patch_launch(monkeypatch, chosen_port=7993)
    seen: dict[str, Any] = {}

    def serve(app: object, **kwargs: Any) -> None:
        seen.update(stdout=sys.stdout, stderr=sys.stderr, progress=preprocess.progress_directory())
        seen["progress_exists"] = seen["progress"] is not None and seen["progress"].is_dir()
        sys.stderr.write("WARN 진입점 시험 줄\n")

    monkeypatch.setattr(ui_app.uvicorn, "run", serve)
    original = (sys.stdout, sys.stderr)
    assert ui_app.main(["--port", "7993", "--no-browser"]) == 0
    assert isinstance(seen["stdout"], activity_log.TeeStream)
    assert isinstance(seen["stderr"], activity_log.TeeStream)
    assert seen["progress_exists"] is True
    assert (sys.stdout, sys.stderr) == original  # 끝나면 되돌린다
    assert preprocess.progress_directory() is None
    assert not seen["progress"].exists()  # 진행 파일 폴더도 치운다
    assert seen["progress"].name.startswith(f"vlogsrt-progress-{os.getpid()}-")  # D63 H
    texts = [line.text for line in activity_log.SERVER_LOG.lines_since(0)]
    assert "WARN 진입점 시험 줄" in texts


def test_the_server_start_sweeps_only_dead_pid_scratch_dirs(
    monkeypatch: pytest.MonkeyPatch, private_tmpdir: Path
) -> None:
    """D63 H — 서버 시작이 죽은 pid의 진행·시각 확인 폴더만 지운다.

    옛 이름·살아 있는 pid·다른 종류는 남긴다.
    """
    dead_pid = _dead_pid()
    dead = [
        private_tmpdir / f"vlogsrt-progress-{dead_pid}-abc12345",
        private_tmpdir / f"vlogsrt-sync-{dead_pid}-def67890",
    ]
    kept = [
        private_tmpdir / f"vlogsrt-progress-{os.getpid()}-live0000",
        private_tmpdir / "vlogsrt-progress-oldfmt12",
        private_tmpdir / "vlogsrt-status-0F0E2C1B-0000-4000-8000-000000000000",
    ]
    for path in (*dead, *kept):
        path.mkdir()
    (dead[1] / "audio.pcm").write_bytes(b"\0" * 64)
    _patch_launch(monkeypatch, chosen_port=7994)
    assert ui_app.main(["--port", "7994", "--no-browser"]) == 0
    assert [path.exists() for path in dead] == [False, False]
    assert all(path.is_dir() for path in kept)


def _dead_pid() -> int:
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid
