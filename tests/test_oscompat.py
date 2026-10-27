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


@pytest.mark.parametrize("code", [0, 6, 8, 1450])
def test_windows_any_other_open_failure_is_alive(windows: Any, code: int) -> None:
    """죽었다고 보는 것은 ERROR_INVALID_PARAMETER 하나다 — 모르면 지우지 않는다(D100 §1b)."""
    windows(_FakeKernel32(handle=0), last_error=code)
    assert process_alive(4242)


def test_the_real_kernel32_declares_handle_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """선언이 없으면 ctypes가 64비트 HANDLE을 C int로 자른다 — 세 함수 모두 형을 단다."""
    from ctypes import wintypes

    class _Function:
        argtypes: tuple[Any, ...] | None = None
        restype: Any = None

    class _Library:
        def __init__(self) -> None:
            self.OpenProcess = _Function()  # noqa: N815
            self.GetExitCodeProcess = _Function()  # noqa: N815
            self.CloseHandle = _Function()  # noqa: N815

    library = _Library()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: library, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    kernel32, _last_error = oscompat._kernel32()
    assert kernel32 is library
    assert library.OpenProcess.argtypes == (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    assert library.OpenProcess.restype is wintypes.HANDLE
    assert library.GetExitCodeProcess.argtypes == (wintypes.HANDLE, wintypes.LPDWORD)
    assert library.GetExitCodeProcess.restype is wintypes.BOOL
    assert library.CloseHandle.argtypes == (wintypes.HANDLE,)
    assert library.CloseHandle.restype is wintypes.BOOL


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
    assert sorted(path.name for path in tmp_path.iterdir()) == [".staged.tmp"]  # 임시도 없다


def test_the_fallback_name_appears_only_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """링크가 없는 파일 시스템에서도 최종 이름은 복사가 끝난 뒤에만 보인다(D100 §1b)."""
    _no_links(monkeypatch)
    target = tmp_path / "final.mp4"
    real_copy = oscompat.shutil.copyfileobj
    seen_during_copy: list[bool] = []

    def watched(reader: Any, writer: Any) -> None:
        seen_during_copy.append(os.path.lexists(target))
        real_copy(reader, writer)

    monkeypatch.setattr(oscompat.shutil, "copyfileobj", watched)
    publish_exclusive(_staged(tmp_path), target)
    assert seen_during_copy == [False]
    assert target.read_bytes() == b"finished bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".staged.tmp", "final.mp4"]


def test_a_destination_that_appears_mid_copy_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """복사 중에 다른 쪽이 이름을 먼저 냈다 — 덮지 않고 거부하며, 임시 파일은 치운다."""
    _no_links(monkeypatch)
    target = tmp_path / "final.mp4"
    real_copy = oscompat.shutil.copyfileobj

    def racing(reader: Any, writer: Any) -> None:
        real_copy(reader, writer)
        target.write_bytes(b"winner")

    monkeypatch.setattr(oscompat.shutil, "copyfileobj", racing)
    with pytest.raises(FileExistsError):
        publish_exclusive(_staged(tmp_path), target)
    assert target.read_bytes() == b"winner"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".staged.tmp", "final.mp4"]


@pytest.mark.parametrize("unsupported", ["absent", "einval"])
def test_without_a_noreplace_rename_the_exclusive_copy_still_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported: str
) -> None:
    """덮지 않는 이름 바꾸기가 없는 체계(또는 파일 시스템이 모른다)는 예전 배타 복사로 물러난다."""
    _no_links(monkeypatch)
    if unsupported == "absent":
        monkeypatch.setattr(oscompat, "_libc_rename_call", lambda *_a: None)
    else:

        def refuse() -> int:
            ctypes.set_errno(errno.EINVAL)
            return -1

        monkeypatch.setattr(oscompat, "_libc_rename_call", lambda *_a: refuse)
    target = tmp_path / "final.mp4"
    publish_exclusive(_staged(tmp_path), target)
    assert target.read_bytes() == b"finished bytes"
    assert target.stat().st_mode & 0o777 == 0o640
    assert sorted(path.name for path in tmp_path.iterdir()) == [".staged.tmp", "final.mp4"]
    with pytest.raises(FileExistsError):
        publish_exclusive(_staged(tmp_path), target)
    assert sorted(path.name for path in tmp_path.iterdir()) == [".staged.tmp", "final.mp4"]


def test_windows_fallback_renames_without_overwrite_and_never_marks_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows는 `os.rename`(REPLACE_EXISTING 없음)이고, 읽기 전용으로 만들지 않는다."""
    _no_links(monkeypatch)
    renamed: list[tuple[str, str]] = []
    real_rename = os.rename

    def recording(source: Any, destination: Any) -> None:
        if os.path.lexists(destination):
            raise FileExistsError(errno.EEXIST, "Cannot create a file", str(destination))
        renamed.append((Path(source).name, Path(destination).name))
        real_rename(source, destination)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "rename", recording)
    monkeypatch.setattr(oscompat, "_libc_rename_call", lambda *_a: pytest.fail("libc 길이 아니다"))
    staged = _staged(tmp_path)
    staged.chmod(0o444)
    target = tmp_path / "final.mp4"
    publish_exclusive(staged, target)
    assert [name for _source, name in renamed] == ["final.mp4"]
    assert renamed[0][0].startswith(".final.mp4.") and renamed[0][0].endswith(".publish")
    assert target.stat().st_mode & 0o200  # 쓰기 가능 — 지울 수 있다
    with pytest.raises(FileExistsError):
        publish_exclusive(staged, target)
    assert sorted(path.name for path in tmp_path.iterdir()) == [".staged.tmp", "final.mp4"]


# ── Windows 교체·지우기의 다시 시도 ────────────────────────────────────────


def _held(times: int, real: Any) -> tuple[Any, list[int]]:
    """처음 `times`번은 다른 쪽이 쥐고 있다(WinError 5) — 그 뒤엔 진짜를 부른다."""
    calls: list[int] = []

    def action(*args: Any) -> Any:
        calls.append(1)
        if len(calls) <= times:
            raise PermissionError(errno.EACCES, "Access is denied")
        return real(*args)

    return action, calls


def _fast(attempts: int = 3) -> oscompat.OsCompatSettings:
    return oscompat.OsCompatSettings(win32_retry_attempts=attempts, win32_retry_seconds=0.001)


def test_windows_replace_retries_while_a_reader_holds_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    held, calls = _held(2, os.replace)
    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(sys, "platform", "win32")
    oscompat.replace(source, target, _fast(3))
    assert len(calls) == 3
    assert target.read_bytes() == b"new"


def test_windows_replace_gives_up_with_the_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, calls = _held(99, os.replace)
    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(PermissionError):
        oscompat.replace(tmp_path / "a", tmp_path / "b", _fast(3))
    assert len(calls) == 3


def test_posix_replace_and_unlink_do_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, calls = _held(1, os.replace)
    monkeypatch.setattr(os, "replace", held)
    with pytest.raises(PermissionError):
        oscompat.replace(tmp_path / "a", tmp_path / "b", _fast(3))
    assert len(calls) == 1
    gone, unlinks = _held(1, os.unlink)
    monkeypatch.setattr(os, "unlink", gone)
    with pytest.raises(PermissionError):
        oscompat.unlink(tmp_path / "a", settings=_fast(3))
    assert len(unlinks) == 1


def test_windows_unlink_retries_and_honours_missing_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = tmp_path / "frame_00000001.jpg"
    served.write_bytes(b"jpg")
    held, calls = _held(1, os.unlink)
    monkeypatch.setattr(os, "unlink", held)
    monkeypatch.setattr(sys, "platform", "win32")
    oscompat.unlink(served, settings=_fast(3))
    assert len(calls) == 2 and not served.exists()
    oscompat.unlink(served, missing_ok=True, settings=_fast(3))
    with pytest.raises(FileNotFoundError):
        oscompat.unlink(served, settings=_fast(3))


def test_other_errors_are_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(FileNotFoundError):
        oscompat.replace(tmp_path / "missing", tmp_path / "b", _fast(3))


@pytest.mark.parametrize(
    "fields", [{"win32_retry_attempts": 0}, {"win32_retry_attempts": True},
               {"win32_retry_seconds": 0}],
)  # fmt: skip
def test_retry_settings_are_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        oscompat.OsCompatSettings(**fields)


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
