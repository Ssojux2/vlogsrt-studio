"""운영체제마다 다른 파일·프로세스 동작을 한 자리에 — macOS·Windows 공통 기반(D100 §1).

셋이다.

- `process_alive` — POSIX는 신호 0(`os.kill(pid, 0)`)으로 묻는다. Windows의 `os.kill`은 신호가
  아니라 `TerminateProcess`라서 **묻는 순간 그 프로세스를 끝낸다**. 그래서 Windows는
  `OpenProcess` + `GetExitCodeProcess`(STILL_ACTIVE)로 묻는다(ctypes, 표준 라이브러리).
- `fsync_directory` — 디렉터리 항목의 내구성. Windows는 디렉터리를 `os.open`으로 열 수 없어
  fsync 자체가 없다(NTFS는 메타데이터를 저널로 지킨다) — 아무것도 하지 않는다.
- `publish_exclusive` — 다 쓴 임시 파일을 최종 이름으로 내되 **있으면 절대 덮어쓰지 않는다**.
  `os.link`가 그 일을 원자적으로 하지만 exFAT·FAT(SD 카드)에는 하드 링크가 없다. 그때만 최종
  이름을 `O_CREAT|O_EXCL`로 만들어 복사하고 fsync한다 — 덮어쓰지 않는다는 약속은 그대로이고,
  원자성만 잃는다(복사가 끝나기 전에 이름이 먼저 보인다. 복사가 실패하면 만든 파일을 지운다).
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vlogsrt import log

_WIN32_QUERY_LIMITED_INFORMATION = 0x1000
"""`PROCESS_QUERY_LIMITED_INFORMATION` — 남의(권한 낮은) 프로세스도 이 권한으로는 열린다."""
_WIN32_STILL_ACTIVE = 259
_WIN32_ACCESS_DENIED = 5
_WIN32_PID_MAX = 0xFFFFFFFF
"""Windows pid는 DWORD다 — 그 밖의 수는 이 체계의 pid가 될 수 없다."""

_NO_LINK_ERRNOS = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS}
)
"""하드 링크를 못 만드는 파일 시스템의 errno — Linux vfat은 EPERM, macOS exFAT·msdos는 ENOTSUP."""
_NO_LINK_WINERRORS = frozenset({1, 17, 50})
"""Windows의 같은 뜻 — ERROR_INVALID_FUNCTION(FAT·exFAT) · NOT_SAME_DEVICE · NOT_SUPPORTED."""


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
    """Windows `kernel32`와 마지막 오류 읽기 — 시험이 가짜로 바꾼다."""
    if sys.platform == "win32":
        import ctypes

        return ctypes.WinDLL("kernel32", use_last_error=True), ctypes.get_last_error
    raise OSError("kernel32는 Windows에만 있다")


def _win32_process_alive(pid: int, kernel32: Any, last_error: Callable[[], int]) -> bool:
    import ctypes

    if pid < 0 or pid > _WIN32_PID_MAX:
        return True  # 이 체계의 pid가 될 수 없는 수 — 모르면 지우지 않는다
    handle = kernel32.OpenProcess(_WIN32_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 없는 pid는 ERROR_INVALID_PARAMETER다. 권한이 없으면 있는 것이다.
        return last_error() == _WIN32_ACCESS_DENIED
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


def publish_exclusive(source: Path, destination: Path) -> None:
    """`source`의 바이트를 `destination`에 낸다 — 있으면 `FileExistsError`, 덮어쓰지 않는다.

    `source`는 그대로 남는다(지우기는 호출자 몫 — 링크와 같다). 하드 링크를 못 만드는 파일
    시스템에서만 배타 생성 + 복사 + fsync로 물러난다.
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
    _copy_exclusive(source, destination)


def _links_unsupported(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    return error.errno in _NO_LINK_ERRNOS or winerror in _NO_LINK_WINERRORS


def _copy_exclusive(source: Path, destination: Path) -> None:
    """최종 이름을 O_EXCL로 만들어 복사한다 — 있으면(심볼릭 링크여도) 만들기부터 실패한다."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with source.open("rb") as reader:
        mode = stat.S_IMODE(os.fstat(reader.fileno()).st_mode)
        descriptor = os.open(destination, flags, mode)
        try:
            with os.fdopen(descriptor, "wb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)  # 우리가 만든 반쪽 — 남기면 완성본처럼 보인다
            raise
