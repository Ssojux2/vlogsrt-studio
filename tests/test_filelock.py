"""협조 파일 잠금(D100 §1) — POSIX는 진짜 flock으로, Windows 길은 가짜 `msvcrt`로 본다.

Windows는 여기서 돌려 볼 수 없다. `sys.platform`을 바꾸고 `msvcrt`를 가짜로 끼워 넣어 분기와
잠금 자리·위치 되돌리기·다시 시도를 확인한다(정적 검사는 `make types-win`).
"""

from __future__ import annotations

import ast
import errno
import os
import sys
import types
from pathlib import Path

import pytest

from vlogsrt import filelock
from vlogsrt.filelock import DIRECTORY_LOCK_NAME, FILE_LOCK, FileLockSettings

# ── POSIX — 진짜 flock ─────────────────────────────────────────────────────


def _two_descriptors(path: Path) -> tuple[int, int]:
    path.touch()
    return os.open(path, os.O_RDWR), os.open(path, os.O_RDWR)


def test_a_second_non_blocking_locker_is_refused_until_the_first_lets_go(tmp_path: Path) -> None:
    first, second = _two_descriptors(tmp_path / "x.lock")
    try:
        filelock.lock_exclusive(first, blocking=False)
        with pytest.raises(BlockingIOError):
            filelock.lock_exclusive(second, blocking=False)
        filelock.unlock(first)
        filelock.lock_exclusive(second, blocking=False)
        with pytest.raises(BlockingIOError):
            filelock.lock_exclusive(first, blocking=False)
    finally:
        os.close(first)
        os.close(second)


def test_closing_releases_and_file_objects_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with path.open("a", encoding="utf-8") as holder:
        filelock.lock_exclusive(holder)  # 기다리는 잠금 — 쥔 쪽이 없으니 바로 된다
        with path.open("a", encoding="utf-8") as rival, pytest.raises(BlockingIOError):
            filelock.lock_exclusive(rival, blocking=False)
    with path.open("a", encoding="utf-8") as later:
        filelock.lock_exclusive(later, blocking=False)


def test_a_posix_directory_lock_is_the_folder_itself(tmp_path: Path) -> None:
    first = filelock.open_directory_lock(tmp_path)
    second = filelock.open_directory_lock(tmp_path)
    try:
        assert os.path.samestat(os.fstat(first), os.stat(tmp_path))
        filelock.lock_exclusive(first, blocking=False)
        with pytest.raises(BlockingIOError):
            filelock.lock_exclusive(second, blocking=False)
    finally:
        os.close(first)
        os.close(second)
    assert not (tmp_path / DIRECTORY_LOCK_NAME).exists()  # POSIX는 파일을 만들지 않는다


def test_no_platform_lock_module_is_imported_at_the_top() -> None:
    tree = ast.parse(Path(filelock.__file__).read_text(encoding="utf-8"))
    top = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    } | {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
    assert not top & {"fcntl", "msvcrt"}


# ── Windows — 가짜 msvcrt ──────────────────────────────────────────────────


class _FakeMsvcrt(types.ModuleType):
    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self, busy: int = 0, failure: int | None = None) -> None:
        super().__init__("msvcrt")
        self.busy = busy
        self.failure = failure
        self.calls: list[tuple[int, int, int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((fd, mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if self.failure is not None:
            raise OSError(self.failure, "injected")
        if mode == self.LK_NBLCK and self.busy:
            self.busy -= 1
            raise OSError(errno.EACCES, "Permission denied")


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> _FakeMsvcrt:
    fake = _FakeMsvcrt()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    return fake


def _positioned(tmp_path: Path, position: int) -> int:
    path = tmp_path / "x.lock"
    path.write_bytes(b"0123456789")
    descriptor = os.open(path, os.O_RDWR)
    os.lseek(descriptor, position, os.SEEK_SET)
    return descriptor


def test_windows_locks_one_far_byte_and_restores_the_position(
    tmp_path: Path, windows: _FakeMsvcrt
) -> None:
    descriptor = _positioned(tmp_path, 3)
    try:
        filelock.lock_exclusive(descriptor, blocking=False)
        filelock.unlock(descriptor)
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 3
    finally:
        os.close(descriptor)
    offset = FILE_LOCK.win32_lock_offset
    assert windows.calls == [
        (descriptor, windows.LK_NBLCK, 1, offset),
        (descriptor, windows.LK_UNLCK, 1, offset),
    ]


def test_windows_contention_is_a_blocking_io_error(tmp_path: Path, windows: _FakeMsvcrt) -> None:
    windows.busy = 1
    descriptor = _positioned(tmp_path, 0)
    try:
        with pytest.raises(BlockingIOError):
            filelock.lock_exclusive(descriptor, blocking=False)
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
    finally:
        os.close(descriptor)


def test_windows_blocking_lock_retries_until_free(
    tmp_path: Path, windows: _FakeMsvcrt, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows.busy = 2
    sleeps: list[float] = []
    monkeypatch.setattr(filelock.time, "sleep", sleeps.append)
    descriptor = _positioned(tmp_path, 0)
    try:
        filelock.lock_exclusive(descriptor)
    finally:
        os.close(descriptor)
    assert len(windows.calls) == 3
    assert sleeps == [FILE_LOCK.win32_poll_seconds] * 2


def test_windows_other_lock_failures_are_not_contention(
    tmp_path: Path, windows: _FakeMsvcrt
) -> None:
    windows.failure = errno.EINVAL
    descriptor = _positioned(tmp_path, 0)
    try:
        with pytest.raises(OSError) as caught:
            filelock.lock_exclusive(descriptor, blocking=False)
    finally:
        os.close(descriptor)
    assert not isinstance(caught.value, BlockingIOError)
    assert caught.value.errno == errno.EINVAL


def test_windows_directory_lock_is_a_hidden_file_inside(
    tmp_path: Path, windows: _FakeMsvcrt
) -> None:
    descriptor = filelock.open_directory_lock(tmp_path)
    try:
        filelock.lock_exclusive(descriptor, blocking=False)
    finally:
        os.close(descriptor)
    assert (tmp_path / DIRECTORY_LOCK_NAME).is_file()
    assert windows.calls[0][1] == windows.LK_NBLCK


def test_lock_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="offset"):
        FileLockSettings(win32_lock_offset=-1)
    with pytest.raises(ValueError, match="poll"):
        FileLockSettings(win32_poll_seconds=0)
