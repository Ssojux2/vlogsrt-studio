"""운영체제마다 다른 파일·프로세스 동작(D100 §1) — 프로세스 확인·디렉터리 fsync·덮지 않는 게시.

Windows 길은 `sys.platform`을 바꾸고 가짜 kernel32로 본다 — 여기서는 돌려 볼 수 없다.
"""

from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import oscompat
from vlogsrt.oscompat import fsync_directory, process_alive, publish_exclusive

# ── 프로세스 확인 ──────────────────────────────────────────────────────────


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_posix_liveness_is_signal_zero() -> None:
    assert process_alive(os.getpid())
    assert not process_alive(_dead_pid())
    assert process_alive(2**80)  # 이 체계의 pid가 될 수 없는 수 — 모르면 산 것


class _FakeKernel32:
    """OpenProcess·GetExitCodeProcess·CloseHandle만 — 열린 손잡이는 닫혔는지 센다."""

    def __init__(self, *, handle: int, exit_code: int = 259, query_ok: bool = True) -> None:
        self.handle = handle
        self.exit_code = exit_code
        self.query_ok = query_ok
        self.opened: list[tuple[int, bool, int]] = []
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802
        self.opened.append((access, inherit, pid))
        return self.handle

    def GetExitCodeProcess(self, handle: int, code: Any) -> int:  # noqa: N802
        if not self.query_ok:
            return 0
        code._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle: int) -> int:  # noqa: N802
        self.closed.append(handle)
        return 1


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> Any:
    def no_signals(*_args: object) -> None:
        pytest.fail("Windows의 os.kill은 묻지 않고 끝낸다 — 부르면 안 된다")

    monkeypatch.setattr(os, "kill", no_signals)
    monkeypatch.setattr(sys, "platform", "win32")

    def install(kernel32: _FakeKernel32, last_error: int = 0) -> _FakeKernel32:
        monkeypatch.setattr(oscompat, "_kernel32", lambda: (kernel32, lambda: last_error))
        return kernel32

    return install


def test_windows_running_process_is_alive_and_the_handle_is_closed(windows: Any) -> None:
    kernel32 = windows(_FakeKernel32(handle=77, exit_code=259))
    assert process_alive(4242)
    assert kernel32.opened == [(0x1000, False, 4242)]
    assert kernel32.closed == [77]


def test_windows_exited_process_is_dead(windows: Any) -> None:
    kernel32 = windows(_FakeKernel32(handle=77, exit_code=0))
    assert not process_alive(4242)
    assert kernel32.closed == [77]


def test_windows_unknown_pid_is_dead_and_access_denied_is_alive(windows: Any) -> None:
    windows(_FakeKernel32(handle=0), last_error=87)  # ERROR_INVALID_PARAMETER — 없는 pid
    assert not process_alive(4242)
    windows(_FakeKernel32(handle=0), last_error=5)  # ERROR_ACCESS_DENIED — 남의 프로세스
    assert process_alive(4242)


def test_windows_doubt_is_alive(windows: Any) -> None:
    kernel32 = windows(_FakeKernel32(handle=77, query_ok=False))
    assert process_alive(4242)
    assert kernel32.closed == [77]
    untouched = windows(_FakeKernel32(handle=77))
    assert process_alive(2**33)  # DWORD 밖 — 묻지도 않는다
    assert untouched.opened == []


def test_visual_review_and_scratch_share_the_one_helper() -> None:
    from vlogsrt import scratch_dirs
    from vlogsrt.project.raw import visual_review

    assert scratch_dirs.process_alive is process_alive
    assert visual_review.process_alive is process_alive


# ── 디렉터리 fsync ─────────────────────────────────────────────────────────


def test_directory_fsync_runs_on_posix_and_is_skipped_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    fsync_directory(tmp_path)
    assert len(synced) == 1
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "open", lambda *_a: pytest.fail("Windows는 디렉터리를 열지 않는다"))
    fsync_directory(tmp_path)
    assert len(synced) == 1


# ── 덮지 않는 게시 ─────────────────────────────────────────────────────────


def _staged(tmp_path: Path, body: bytes = b"finished bytes") -> Path:
    staged = tmp_path / ".staged.tmp"
    staged.write_bytes(body)
    staged.chmod(0o640)
    return staged


def _no_links(monkeypatch: pytest.MonkeyPatch, code: int = errno.ENOTSUP) -> None:
    def refuse(*_args: object) -> None:
        raise OSError(code, "Operation not supported")

    monkeypatch.setattr(os, "link", refuse)


def test_publish_links_when_the_filesystem_can(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    target = tmp_path / "final.mp4"
    publish_exclusive(staged, target)
    assert os.path.samefile(staged, target)


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.EPERM, errno.EXDEV])
def test_publish_copies_when_links_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    staged = _staged(tmp_path)
    target = tmp_path / "final.mp4"
    _no_links(monkeypatch, code)
    publish_exclusive(staged, target)
    assert target.read_bytes() == b"finished bytes"
    assert not os.path.samefile(staged, target)
    assert staged.exists()  # 원본 쪽은 호출자가 치운다 — 링크와 같다
    assert target.stat().st_mode & 0o777 == 0o640


def test_windows_fat_link_error_also_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WinError(OSError):
        winerror = 1  # ERROR_INVALID_FUNCTION — FAT·exFAT

    def refuse(*_args: object) -> None:
        raise WinError(errno.EINVAL, "Incorrect function")

    monkeypatch.setattr(os, "link", refuse)
    target = tmp_path / "final.mp4"
    publish_exclusive(_staged(tmp_path), target)
    assert target.read_bytes() == b"finished bytes"


@pytest.mark.parametrize("linkable", [True, False])
def test_publish_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linkable: bool
) -> None:
    if not linkable:
        _no_links(monkeypatch)
    target = tmp_path / "final.mp4"
    target.write_bytes(b"winner")
    with pytest.raises(FileExistsError):
        publish_exclusive(_staged(tmp_path), target)
    assert target.read_bytes() == b"winner"


def test_the_copy_fallback_refuses_a_planted_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_links(monkeypatch)
    victim = tmp_path / "victim"
    target = tmp_path / "final.mp4"
    target.symlink_to(victim)  # 매달린 링크도 이미 있는 이름이다
    with pytest.raises(FileExistsError):
        publish_exclusive(_staged(tmp_path), target)
    assert not victim.exists()


def test_a_failed_copy_leaves_no_half_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_links(monkeypatch)

    def broken(*_args: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(oscompat.shutil, "copyfileobj", broken)
    target = tmp_path / "final.mp4"
    with pytest.raises(OSError, match="No space"):
        publish_exclusive(_staged(tmp_path), target)
    assert not target.exists()


def test_other_link_failures_are_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_links(monkeypatch, errno.EACCES)
    with pytest.raises(PermissionError):
        publish_exclusive(_staged(tmp_path), tmp_path / "final.mp4")
    assert not (tmp_path / "final.mp4").exists()


def test_ctypes_byref_carries_the_out_value() -> None:
    """가짜 GetExitCodeProcess가 쓰는 길(`byref(...)._obj`)이 진짜 ctypes에 있다."""
    code = ctypes.c_ulong()
    ctypes.byref(code)._obj.value = 259  # type: ignore[attr-defined]
    assert code.value == 259
