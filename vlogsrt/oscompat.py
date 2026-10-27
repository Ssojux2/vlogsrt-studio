"""운영체제마다 다른 파일·프로세스 동작을 한 자리에 — macOS·Windows 공통 기반(D100 §1·§1b).

- `process_alive` — POSIX는 신호 0(`os.kill(pid, 0)`)으로 묻는다. Windows의 `os.kill`은 신호가
  아니라 `TerminateProcess`라서 **묻는 순간 그 프로세스를 끝낸다**. 그래서 Windows는
  `OpenProcess` + `GetExitCodeProcess`(STILL_ACTIVE)로 묻는다(ctypes, 표준 라이브러리).
- `fsync_directory` — 디렉터리 항목의 내구성. Windows는 디렉터리를 `os.open`으로 열 수 없어
  fsync 자체가 없다(NTFS는 메타데이터를 저널로 지킨다) — 아무것도 하지 않는다.
- `replace`·`unlink` — POSIX는 `os.replace`·`os.unlink` 그대로다. Windows는 누가 대상을 열어
  두면(화면이 읽는 작업 기록 · 서버가 내주는 프레임 · 백신·색인) `PermissionError`(WinError 5·32)로
  실패하므로 설정한 횟수·간격만큼 다시 시도하고, 그래도 안 되면 그 오류를 그대로 올린다(§1b).
- `publish_exclusive` — 다 쓴 임시 파일을 최종 이름으로 내되 **있으면 절대 덮어쓰지 않는다**.
  `os.link`가 그 일을 원자적으로 하지만 exFAT·FAT(SD 카드)에는 하드 링크가 없다. 그때는 옆
  임시 파일에 복사·fsync한 뒤 **덮지 않는 이름 바꾸기**(Windows `os.rename` · Linux
  `renameat2(RENAME_NOREPLACE)` · macOS `renamex_np(RENAME_EXCL)`)로 낸다 — 최종 이름은 다 쓴
  바이트로만 보인다. 그 셋이 모두 없는 체계에서만 예전처럼 최종 이름을 `O_CREAT|O_EXCL`로
  만들어 복사한다(원자성만 잃는다).
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vlogsrt import log

StrPath = str | os.PathLike[str]


@dataclass(frozen=True)
class OsCompatSettings:
    """Windows 교체·지우기의 다시 시도 — 외부 근거 없는 선택(D100 §1b).

    20번 × 0.05초 = 1초. 화면이 작업 기록을 읽거나 서버가 프레임 하나를 내주는 시간(수 ms)보다
    넉넉하고, 그래도 쥐고 있으면(백신 검사가 길다) 오류를 그대로 올려 숨기지 않는다. 간격은 파일
    잠금의 다시 시도 간격(`FileLockSettings.win32_poll_seconds`)과 같다.
    """

    win32_retry_attempts: int = 20
    win32_retry_seconds: float = 0.05

    def __post_init__(self) -> None:
        attempts = self.win32_retry_attempts
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError(f"win32_retry_attempts는 1 이상의 정수여야 한다: {attempts!r}")
        if not self.win32_retry_seconds > 0:
            raise ValueError(f"win32_retry_seconds는 양수여야 한다: {self.win32_retry_seconds!r}")


OS_COMPAT = OsCompatSettings()

_WIN32_QUERY_LIMITED_INFORMATION = 0x1000
"""`PROCESS_QUERY_LIMITED_INFORMATION` — 남의(권한 낮은) 프로세스도 이 권한으로는 열린다."""
_WIN32_STILL_ACTIVE = 259
_WIN32_INVALID_PARAMETER = 87
"""`ERROR_INVALID_PARAMETER` — 없는 pid에 `OpenProcess`가 내는 오류. 죽었다고 보는 것은 이것뿐."""
_WIN32_PID_MAX = 0xFFFFFFFF
"""Windows pid는 DWORD다 — 그 밖의 수는 이 체계의 pid가 될 수 없다."""

_NO_LINK_ERRNOS = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS}
)
"""하드 링크를 못 만드는 파일 시스템의 errno — Linux vfat은 EPERM, macOS exFAT·msdos는 ENOTSUP."""
_NO_LINK_WINERRORS = frozenset({1, 17, 50})
"""Windows의 같은 뜻 — ERROR_INVALID_FUNCTION(FAT·exFAT) · NOT_SAME_DEVICE · NOT_SUPPORTED."""

_NO_EXCL_RENAME_ERRNOS = frozenset({errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP})
"""덮지 않는 이름 바꾸기를 그 파일 시스템·커널이 모른다 — 그때만 배타 생성 + 복사로 물러난다."""
_LINUX_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1
_DARWIN_RENAME_EXCL = 0x00000004
"""`<linux/fs.h>`·`<stdio.h>`(macOS)의 상수 — 플래그 값이지 조정 대상이 아니다."""
_TEMP_TOKEN_BYTES = 4
_PRIVATE_FILE_MODE = 0o600


def process_alive(pid: int) -> bool:
    """그 pid가 살아 있는가 — 없으면 False, 물을 권한이 없으면(남의 프로세스) 살아 있다.

    모르면 산 것으로 본다 — 이 답으로 지우는 쪽(임시 폴더·준비 기록 청소)이 틀리지 않게.
    """
    if sys.platform == "win32":
        kernel32, last_error = _kernel32()
        return _win32_process_alive(pid, kernel32, last_error)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        # 이 체계의 pid가 될 수 없는 수 — 모르면 지우지 않는다.
        return True
    return True


def _kernel32() -> tuple[Any, Callable[[], int]]:
    """Windows `kernel32`와 마지막 오류 읽기 — 시험이 가짜로 바꾼다.

    인자·반환 형을 선언한다 — 선언이 없으면 ctypes가 HANDLE을 C `int`로 잘라 64비트 손잡이가
    깨질 수 있다.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32, ctypes.get_last_error
    raise OSError("kernel32는 Windows에만 있다")


def _win32_process_alive(pid: int, kernel32: Any, last_error: Callable[[], int]) -> bool:
    import ctypes

    if pid < 0 or pid > _WIN32_PID_MAX:
        return True  # 이 체계의 pid가 될 수 없는 수 — 모르면 지우지 않는다
    handle = kernel32.OpenProcess(_WIN32_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 없는 pid만 ERROR_INVALID_PARAMETER다. 권한 없음·그 밖의 오류는 모르는 것 — 산 것이다.
        return last_error() != _WIN32_INVALID_PARAMETER
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # 끝났는지 모른다 — 산 것으로 둔다
        return code.value == _WIN32_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def fsync_directory(path: Path) -> None:
    """디렉터리 항목의 내구성 — 파일 fsync만으로는 이름이 살아남지 않는다. Windows는 하지 않는다."""
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace(source: StrPath, destination: StrPath, settings: OsCompatSettings = OS_COMPAT) -> None:
    """`os.replace` — Windows에서는 대상을 누가 열어 둔 동안의 `PermissionError`만 다시 시도한다."""
    if sys.platform != "win32":
        os.replace(source, destination)
        return
    _retry_while_held(lambda: os.replace(source, destination), settings)


def unlink(
    path: StrPath, *, missing_ok: bool = False, settings: OsCompatSettings = OS_COMPAT
) -> None:
    """`os.unlink` — 내주는 중일 수 있는 파일. Windows에서만 `PermissionError`를 다시 시도한다."""

    def remove() -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            if not missing_ok:
                raise

    if sys.platform != "win32":
        remove()
        return
    _retry_while_held(remove, settings)


def _retry_while_held(action: Callable[[], None], settings: OsCompatSettings) -> None:
    """`PermissionError`만 정해진 횟수까지 다시 한다 — 마지막 실패는 그대로 올린다."""
    for attempt in range(settings.win32_retry_attempts):
        try:
            action()
            return
        except PermissionError as error:
            if attempt + 1 >= settings.win32_retry_attempts:
                raise
            log.debug(f"다른 쪽이 파일을 쥐고 있어 다시 시도합니다: {error}")
        time.sleep(settings.win32_retry_seconds)


def publish_exclusive(source: Path, destination: Path) -> None:
    """`source`의 바이트를 `destination`에 낸다 — 있으면 `FileExistsError`, 덮어쓰지 않는다.

    `source`는 그대로 남는다(지우기는 호출자 몫 — 링크와 같다). 하드 링크를 못 만드는 파일
    시스템에서만 옆 임시 파일 + 덮지 않는 이름 바꾸기로 물러난다(`_copy_then_rename`).
    """
    try:
        os.link(source, destination)
        return
    except FileExistsError:
        raise
    except OSError as error:
        if not _links_unsupported(error):
            raise
        log.debug(f"하드 링크를 못 만드는 파일 시스템 — 복사로 게시합니다: {destination} ({error})")
    _copy_then_rename(source, destination)


def _links_unsupported(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    return error.errno in _NO_LINK_ERRNOS or winerror in _NO_LINK_WINERRORS


def _copy_then_rename(source: Path, destination: Path) -> None:
    """옆 임시 파일에 다 쓰고 fsync한 뒤 덮지 않는 이름 바꾸기 — 최종 이름은 완성본으로만 보인다.

    실패하면(대상이 그 사이 생겼어도) 임시 파일을 지운다. 그 이름 바꾸기가 없는 체계에서만
    예전의 배타 생성 + 복사(`_copy_exclusive`)로 물러난다.
    """
    temporary = _sibling_temp(destination)
    with source.open("rb") as reader:
        mode = stat.S_IMODE(os.fstat(reader.fileno()).st_mode)
        descriptor = os.open(temporary, _NEW_FILE_FLAGS, _creation_mode(mode))
        try:
            _fill(descriptor, reader)
            if not _rename_noreplace(temporary, destination):
                _copy_exclusive(temporary, destination, mode)
        finally:
            unlink(temporary, missing_ok=True)  # 이름 바꾸기가 됐으면 이미 없다


_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _creation_mode(mode: int) -> int:
    """POSIX는 원본 모드 그대로(예전처럼 umask가 걸린다). Windows는 쓰기 가능으로만 만든다.

    Windows에서 읽기 전용 속성은 지우기를 막는다 — 실패한 반쪽을 치우지 못하게 된다.
    """
    return _PRIVATE_FILE_MODE if sys.platform == "win32" else mode


def _sibling_temp(destination: Path) -> Path:
    """대상 옆의 새 임시 이름 — 같은 폴더라야 이름 바꾸기가 한 파일 시스템 안이다."""
    token = secrets.token_hex(_TEMP_TOKEN_BYTES)
    return destination.with_name(f".{destination.name}.{os.getpid()}.{token}.publish")


def _fill(descriptor: int, reader: Any) -> None:
    """열린 새 파일에 다 쓰고 fsync한 뒤 닫는다."""
    with os.fdopen(descriptor, "wb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())


def _rename_noreplace(source: Path, destination: Path) -> bool:
    """덮지 않는 이름 바꾸기 — 됐으면 참, 이 체계에 없으면 거짓. 대상이 있으면 `FileExistsError`."""
    if sys.platform == "win32":
        os.rename(source, destination)  # MoveFileExW(REPLACE_EXISTING 없음) — 있으면 실패한다
        return True
    call = _libc_rename_call(source, destination)
    if call is None:
        return False
    import ctypes

    if call() == 0:
        return True
    code = ctypes.get_errno()
    if code in _NO_EXCL_RENAME_ERRNOS:
        return False
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), str(destination))
    raise OSError(code, os.strerror(code), str(source))


def _libc_rename_call(source: Path, destination: Path) -> Callable[[], int] | None:
    """Linux `renameat2` · macOS `renamex_np` 호출 하나 — 없는 체계면 None. 시험이 바꾼다."""
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    old, new = os.fsencode(source), os.fsencode(destination)
    if sys.platform == "darwin":
        renamex = getattr(libc, "renamex_np", None)
        if renamex is None:
            return None
        return lambda: int(renamex(old, new, _DARWIN_RENAME_EXCL))
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return None
    return lambda: int(
        renameat2(_LINUX_AT_FDCWD, old, _LINUX_AT_FDCWD, new, _LINUX_RENAME_NOREPLACE)
    )


def _copy_exclusive(source: Path, destination: Path, mode: int) -> None:
    """최종 이름을 O_EXCL로 만들어 복사한다 — 있으면(심볼릭 링크여도) 만들기부터 실패한다.

    덮지 않는 이름 바꾸기가 없는 체계의 마지막 길이다 — 복사 중에는 이름이 먼저 보인다.
    """
    with source.open("rb") as reader:
        descriptor = os.open(destination, _NEW_FILE_FLAGS, _creation_mode(mode))
        try:
            _fill(descriptor, reader)
        except BaseException:
            unlink(destination, missing_ok=True)  # 우리가 만든 반쪽 — 남기면 완성본처럼 보인다
            raise
