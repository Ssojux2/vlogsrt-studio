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


_CREATE_NO_WINDOW = 0x08000000


def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW, raising=False)


def test_windows_priority_is_a_below_normal_creation_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    _recording_popen(monkeypatch, seen)
    _windows(monkeypatch)
    monkeypatch.setattr(
        preprocess, "_lower_priority", lambda *_: pytest.fail("Windows에는 nice가 없다")
    )
    completed = preprocess.run_process(
        ["ffmpeg.exe", "-version"], capture_output=True, priority=10, creationflags=0x8
    )
    assert completed.returncode == 0
    flags = 0x8 | _CREATE_NO_WINDOW | 0x4000
    assert seen == [{"creationflags": flags, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}]


def test_windows_without_priority_only_hides_the_console(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    _recording_popen(monkeypatch, seen)
    _windows(monkeypatch)
    preprocess.run_process(["ffmpeg.exe", "-version"], on_poll=lambda: None)
    assert seen[0]["creationflags"] == _CREATE_NO_WINDOW


def test_windows_fast_path_hides_the_console_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """취소를 듣지 않는 빠른 길(`subprocess.run`)도 콘솔 창을 띄우지 않는다(D100 §1b)."""
    seen: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(dict(kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(preprocess.subprocess, "run", run)
    _windows(monkeypatch)
    preprocess.run_process(["ffprobe.exe", "-version"], capture_output=True, text=True)
    assert seen == [
        {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "creationflags": _CREATE_NO_WINDOW,
        }
    ]


def test_posix_launch_gets_no_creation_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    _recording_popen(monkeypatch, seen)
    preprocess.run_process(["ffmpeg", "-version"], on_poll=lambda: None)
    assert "creationflags" not in seen[0]


# ── 텍스트 출력은 UTF-8 ────────────────────────────────────────────────────


def _fake_ffmpeg(tmp_path: Path, stderr: bytes, code: int) -> list[str]:
    """stderr에 정해진 바이트를 내고 끝나는 가짜 ffmpeg — 한글 경로·깨진 바이트."""
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.buffer.write({stderr!r})\n"
        f"sys.stdout.buffer.write({stderr!r})\n"
        f"sys.exit({code})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


_HANGUL_AND_BROKEN = "입력: /영상/여행.mov\n".encode() + b"\xff\xfe\xcd broken\n"


def test_broken_utf8_from_ffmpeg_does_not_escape_as_a_decode_error(tmp_path: Path) -> None:
    command = _fake_ffmpeg(tmp_path, _HANGUL_AND_BROKEN, 1)
    with pytest.raises(RuntimeError) as caught:
        preprocess._run(command)
    assert "/영상/여행.mov" in str(caught.value)
    assert "\ufffd" in str(caught.value)


@pytest.mark.parametrize("on_poll", [None, lambda: None])
def test_text_output_is_utf8_on_both_paths(tmp_path: Path, on_poll: Any) -> None:
    command = _fake_ffmpeg(tmp_path, _HANGUL_AND_BROKEN, 0)
    completed = preprocess.run_process(
        command, capture_output=True, text=True, check=False, on_poll=on_poll
    )
    assert completed.stdout.startswith("입력: /영상/여행.mov\n")
    assert "\ufffd" in completed.stdout and "\ufffd" in completed.stderr


def test_a_caller_encoding_is_kept_and_bytes_stay_bytes(tmp_path: Path) -> None:
    command = _fake_ffmpeg(tmp_path, b"\xff", 0)
    raw = preprocess.run_process(command, capture_output=True, check=False)
    assert raw.stdout == b"\xff"
    latin = preprocess.run_process(
        command, capture_output=True, text=True, encoding="latin-1", check=False
    )
    assert latin.stdout == "\xff"


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
