"""preprocess의 운영체제 분기(D100 §1) — 우선순위·도구 이름·설치 안내.

Windows 길은 `sys.platform`을 바꿔 본다 — 여기서는 돌려 볼 수 없다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import preprocess


def test_lowering_without_setpriority_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(preprocess.os, "setpriority")
    monkeypatch.setattr(
        preprocess.os, "getpriority", lambda *_: pytest.fail("nice가 없으면 묻지도 않는다")
    )
    preprocess._lower_priority(1234, 10)  # AttributeError가 새지 않는다


def test_lowering_without_prio_process_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(preprocess.os, "PRIO_PROCESS")
    preprocess._lower_priority(1234, 10)


def _recording_popen(monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]]) -> None:
    real_popen = subprocess.Popen

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        seen.append(dict(kwargs))
        kwargs.pop("creationflags", None)  # POSIX의 진짜 Popen은 이 플래그를 받지 않는다
        return real_popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(preprocess.subprocess, "Popen", popen)


def test_windows_priority_is_a_below_normal_creation_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    _recording_popen(monkeypatch, seen)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
    monkeypatch.setattr(
        preprocess, "_lower_priority", lambda *_: pytest.fail("Windows에는 nice가 없다")
    )
    completed = preprocess.run_process(
        ["ffmpeg.exe", "-version"], capture_output=True, priority=10, creationflags=0x8
    )
    assert completed.returncode == 0
    assert seen == [
        {"creationflags": 0x8 | 0x4000, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    ]


def test_windows_without_priority_adds_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    _recording_popen(monkeypatch, seen)
    monkeypatch.setattr(sys, "platform", "win32")
    preprocess.run_process(["ffmpeg.exe", "-version"], on_poll=lambda: None)
    assert "creationflags" not in seen[0]


def test_posix_priority_still_takes_nice(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    lowered: list[tuple[int, int]] = []
    _recording_popen(monkeypatch, seen)
    monkeypatch.setattr(
        preprocess, "_lower_priority", lambda pid, nice: lowered.append((pid, nice))
    )
    preprocess.run_process(["ffmpeg", "-version"], priority=10)
    assert "creationflags" not in seen[0]
    assert [nice for _, nice in lowered] == [10]


@pytest.mark.parametrize(
    "executable", ["ffprobe", "ffprobe.exe", "FFPROBE.EXE", "/opt/bin/ffprobe"]
)
def test_ffprobe_is_recognised_with_or_without_exe(executable: str) -> None:
    source, target, _ = preprocess._describe([executable, "-v", "error", "/media/in.mov"])
    assert (source, target) == ("/media/in.mov", None)


@pytest.mark.parametrize("executable", ["ffmpeg", "ffmpeg.exe", "/usr/local/bin/ffmpeg"])
def test_progress_attaches_to_ffmpeg_exe_too(executable: str, tmp_path: Path) -> None:
    preprocess.enable_process_progress(tmp_path / "progress")
    try:
        argv, progress = preprocess._with_progress([executable, "-i", "in.mov", "out.mp4"], 7)
    finally:
        preprocess.disable_process_progress()
    assert progress == tmp_path / "progress" / "7.txt"
    assert argv[:3] == [executable, "-progress", f"file:{progress}"]


def test_a_lookalike_tool_is_not_ffmpeg(tmp_path: Path) -> None:
    preprocess.enable_process_progress(tmp_path / "progress")
    try:
        argv, progress = preprocess._with_progress(["ffmpegx", "-i", "in.mov", "out.mp4"], 7)
    finally:
        preprocess.disable_process_progress()
    assert progress is None and argv[0] == "ffmpegx"


@pytest.mark.parametrize(
    ("platform", "hint"),
    [
        ("darwin", "brew install ffmpeg"),
        ("win32", "winget install Gyan.FFmpeg"),
        ("linux", "sudo apt install ffmpeg"),
    ],
)
def test_missing_ffmpeg_names_the_install_command_for_the_os(
    monkeypatch: pytest.MonkeyPatch, platform: str, hint: str
) -> None:
    monkeypatch.setattr(preprocess.shutil, "which", lambda _tool: None)
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(RuntimeError) as caught:
        preprocess.require_ffmpeg("ffprobe")
    assert str(caught.value) == f"ffprobe 를 찾지 못했습니다. `{hint}` 후 다시 실행하세요."
