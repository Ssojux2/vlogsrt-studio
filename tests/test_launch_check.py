"""더블클릭 실행 파일의 공유 확인(`scripts/launch_check.py`, D100 §8).

실행 파일 자체는 여기서 돌리지 않는다(앱을 띄운다). 판단은 모듈 함수로,
실행 파일과의 약속(extra 목록·명령 이름·레이블·줄바꿈)은 파일 글자로 잠근다.
`실행.bat`은 이 컴퓨터에서 실행해 보지 못했다.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "launch_check.py"
MAC_LAUNCHER = ROOT / "실행.command"
WIN_LAUNCHER = ROOT / "실행.bat"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("launch_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch_check = _load()


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ((3, 9, 6), False),
        ((3, 10, 14), False),
        ((3, 11, 0), True),
        ((3, 13, 1), True),
        ((4, 0), True),
    ],
)
def test_python_version_floor_is_3_11(version: tuple[int, ...], supported: bool) -> None:
    assert launch_check.python_supported(version) is supported


def test_pyproject_floor_matches_launcher_floor() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11"' in text
    assert launch_check.MIN_PYTHON == (3, 11)


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".venv").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    return tmp_path


def test_stamp_is_missing_until_written(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert launch_check.stamp_current(project) is False
    path = launch_check.write_stamp(project)
    assert path == project / ".venv" / launch_check.STAMP_NAME
    assert launch_check.stamp_current(project) is True


def test_stamp_goes_stale_when_pyproject_changes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = launch_check.install_stamp(project / "pyproject.toml")
    launch_check.write_stamp(project)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["y"]\n', encoding="utf-8"
    )
    assert launch_check.install_stamp(project / "pyproject.toml") != before
    assert launch_check.stamp_current(project) is False


def test_stamp_depends_on_extras(tmp_path: Path) -> None:
    pyproject = _project(tmp_path) / "pyproject.toml"
    assert launch_check.install_stamp(pyproject, "ui") != launch_check.install_stamp(pyproject)


def test_stamp_is_deterministic(tmp_path: Path) -> None:
    pyproject = _project(tmp_path) / "pyproject.toml"
    assert launch_check.install_stamp(pyproject) == launch_check.install_stamp(pyproject)


def test_tools_present_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch_check.shutil, "which", lambda tool: f"/bin/{tool}")
    assert launch_check.missing_tools() == []
    assert launch_check.main(["tools"]) == 0


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", ["ffmpeg가 필요합니다: brew install ffmpeg"]),
        ("win32", ["ffmpeg가 필요합니다: winget install Gyan.FFmpeg", "새 창"]),
        ("linux", ["ffmpeg가 필요합니다:", "apt install ffmpeg"]),
    ],
)
def test_missing_ffmpeg_message_per_os(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    expected: list[str],
) -> None:
    monkeypatch.setattr(
        launch_check.shutil, "which", lambda tool: None if tool == "ffprobe" else f"/bin/{tool}"
    )
    monkeypatch.setattr(sys, "platform", platform)
    assert launch_check.main(["tools"]) == 1
    err = capsys.readouterr().err
    assert "찾지 못한 것: ffprobe" in err
    for fragment in expected:
        assert fragment in err
    if platform != "darwin":
        assert "brew" not in err


def test_importable_reports_missing_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        launch_check.importlib.util,
        "find_spec",
        lambda name: None if name == "gradio" else object(),
    )
    assert launch_check.main(["importable"]) == 1
    assert "gradio" in capsys.readouterr().err


def test_unknown_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert launch_check.main(["nope"]) == 2
    assert "사용법" in capsys.readouterr().err


def test_module_stays_stdlib_only() -> None:
    """첫 실행엔 꾸러미가 없다 — 앱이나 서드파티를 import하면 확인 자체가 못 돈다."""
    text = SCRIPT.read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w]*)", text, flags=re.MULTILINE))
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
    assert "subprocess" not in imported


@pytest.mark.parametrize("launcher", [MAC_LAUNCHER, WIN_LAUNCHER], ids=["mac", "win"])
def test_launchers_install_the_stamped_extras(launcher: Path) -> None:
    text = launcher.read_text(encoding="utf-8")
    assert f'pip install -e ".[{launch_check.INSTALL_EXTRAS}]"' in text
    for command in ("stamp-ok", "write-stamp", "importable", "tools"):
        assert f"launch_check.py {command}" in text
    assert "-u -m ui.app" in text


def test_windows_launcher_is_crlf_utf8_without_bom() -> None:
    raw = WIN_LAUNCHER.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("utf-8")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.count(b"\r\n") > 10
    assert b"chcp 65001" in raw


def test_windows_launcher_labels_exist() -> None:
    text = WIN_LAUNCHER.read_text(encoding="utf-8")
    labels = set(re.findall(r"^:(\w+)", text, flags=re.MULTILINE))
    targets = set(re.findall(r"(?:goto|call)\s+:(\w+)", text))
    assert targets <= labels


@pytest.mark.skipif(os.name == "nt", reason="실행 비트는 POSIX에만 있다")
def test_mac_launcher_is_executable() -> None:
    assert os.access(MAC_LAUNCHER, os.X_OK)
