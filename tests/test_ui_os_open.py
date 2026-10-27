"""파일 관리자 열기 — macOS Finder · Windows 탐색기 · 그 밖 xdg-open(D100 §1b).

진짜 창은 열지 않는다 — `subprocess.run`·`os.startfile`을 가짜로 바꿔 argv만 본다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ui import os_open


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(os_open.subprocess, "run", run)
    monkeypatch.setattr(os, "startfile", lambda path: calls.append(["startfile", str(path)]),
                        raising=False)  # fmt: skip
    return calls


def test_macos_reveals_and_opens_with_open(
    launched: list[list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    os_open.reveal(tmp_path / "export")
    os_open.open_folder(tmp_path / "export")
    assert launched == [
        ["/usr/bin/open", "-R", str(tmp_path / "export")],
        ["/usr/bin/open", str(tmp_path / "export")],
    ]


def test_windows_uses_the_system_explorer_and_startfile(
    launched: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    target = Path("C:/Users/me/여행 영상/export")
    os_open.reveal(target)
    os_open.open_folder(target)
    explorer = os.path.join("C:\\Windows", "explorer.exe")
    assert launched == [[explorer, "/select,", str(target)], ["startfile", str(target)]]


def test_windows_explorer_exit_code_one_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """탐색기는 성공해도 1을 낸다 — 그 코드로 실패를 만들지 않는다."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        os_open.subprocess, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "")
    )
    os_open.reveal(Path("C:/x"))


def test_other_systems_use_xdg_open(
    launched: list[list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    os_open.reveal(tmp_path / "export" / "a.mp4")
    os_open.open_folder(tmp_path / "export")
    assert launched == [
        ["xdg-open", str(tmp_path / "export")],
        ["xdg-open", str(tmp_path / "export")],
    ]


def test_failures_carry_the_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        os_open.subprocess,
        "run",
        lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "  no such file  \n"),
    )
    with pytest.raises(OSError, match="^no such file$"):
        os_open.reveal(tmp_path)

    def missing(argv: list[str], **_k: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os_open.subprocess, "run", missing)
    with pytest.raises(OSError, match="xdg-open"):
        os_open.open_folder(tmp_path)
