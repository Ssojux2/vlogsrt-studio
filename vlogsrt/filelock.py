"""협조 파일 잠금 — POSIX는 `fcntl.flock`, Windows는 `msvcrt.locking`(D100 §1).

표준 라이브러리만 쓴다. 맨 위에서 `fcntl`·`msvcrt`를 import하지 않는다 — 한쪽 운영체제에는
다른 쪽 모듈이 없어서 import만으로 앱이 죽는다. 부를 때 그 플랫폼의 모듈을 연다.

POSIX의 의미는 예전 호출 자리와 **같다**: 배타 잠금만 있고(공유 잠금을 쓰는 곳은 없다),
`blocking=False`는 `LOCK_NB`이며 다른 쪽이 쥐고 있으면 `BlockingIOError`가 난다. 잠금이 없는
파일 시스템(네트워크 드라이브 등)은 그대로 `OSError`다.

Windows에는 flock이 없다. `msvcrt.locking`은 **바이트 범위**를 잠그고, 그 범위는 다른 핸들의
읽기·쓰기까지 막는다(권고가 아니라 강제다). 그래서 내용과 겹치지 않게 파일 끝보다 훨씬 먼 한
바이트(`win32_lock_offset`)를 잠근다 — Windows는 파일 끝 너머의 범위도 잠글 수 있다. 잠근 뒤
서술자의 위치는 되돌린다. `LK_LOCK`은 열 번(10초) 시도하고 포기하므로 기다리는 잠금은
`LK_NBLCK`을 짧은 간격으로 다시 시도한다 — flock처럼 끝없이 기다린다.

디렉터리에는 Windows가 잠글 서술자를 주지 않는다. 폴더 잠금(`open_directory_lock`)은 POSIX에서는
그 폴더 자체를, Windows에서는 폴더 안의 숨은 잠금 파일(`DIRECTORY_LOCK_NAME`)을 연다. 그 파일은
지우지 않는다 — 지우는 순간 두 쪽이 서로 다른 파일을 잠그는 틈이 생긴다.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

LockTarget = int | IO[Any]
"""잠글 대상 — 서술자 또는 `fileno()`가 있는 파일 객체(`fcntl.flock`이 받던 그대로)."""

DIRECTORY_LOCK_NAME = ".vlogsrt-folder.lock"
"""Windows 폴더 잠금이 여는 파일 — 점으로 시작해 스캔·초안 청소의 이름 규칙에 걸리지 않는다."""


@dataclass(frozen=True)
class FileLockSettings:
    """Windows 잠금의 자리와 다시 시도 간격 — 외부 근거 없는 선택(D100 §1).

    자리 1 GiB는 이 앱이 잠그는 파일(잠금 파일·원장·작업 기록, 많아야 수 MB)보다 훨씬 멀다.
    간격 0.05초는 UI 잠금의 포트 읽기 간격(`UiInstanceSettings`)과 같다.
    """

    win32_lock_offset: int = 1 << 30
    win32_poll_seconds: float = 0.05

    def __post_init__(self) -> None:
        offset = self.win32_lock_offset
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError(f"win32_lock_offset는 0 이상의 정수여야 한다: {offset!r}")
        if not self.win32_poll_seconds > 0:
            raise ValueError(f"win32_poll_seconds는 양수여야 한다: {self.win32_poll_seconds!r}")


FILE_LOCK = FileLockSettings()

_WIN32_CONTENDED = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)})
"""`msvcrt.locking`의 「다른 쪽이 쥐고 있다」 errno — LK_NBLCK은 EACCES, LK_LOCK은 EDEADLOCK."""


def lock_exclusive(target: LockTarget, *, blocking: bool = True) -> None:
    """배타 잠금 — `blocking=False`면 쥔 쪽이 있을 때 `BlockingIOError`."""
    descriptor = _descriptor(target)
    if sys.platform == "win32":
        import msvcrt

        _win32_lock(descriptor, blocking, msvcrt.locking, msvcrt.LK_NBLCK)
        return
    import fcntl

    operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(descriptor, operation)


def unlock(target: LockTarget) -> None:
    """잠금을 푼다 — 닫기도 풀지만, 닫기 전에 풀어야 하는 자리(쓰고 나서 푸는 원장)가 있다."""
    descriptor = _descriptor(target)
    if sys.platform == "win32":
        import msvcrt

        _at_lock_offset(descriptor, lambda: msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1))
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def open_directory_lock(folder: Path) -> int:
    """폴더 잠금에 쓸 서술자 — POSIX는 폴더 자체, Windows는 폴더 안의 잠금 파일. 호출자가 닫는다."""
    if sys.platform == "win32":
        return os.open(folder / DIRECTORY_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    return os.open(folder, os.O_RDONLY)


def _descriptor(target: LockTarget) -> int:
    return target if isinstance(target, int) else target.fileno()


def _win32_lock(
    descriptor: int,
    blocking: bool,
    locking: Callable[[int, int, int], None],
    non_blocking_mode: int,
    settings: FileLockSettings = FILE_LOCK,
) -> None:
    """먼 한 바이트를 LK_NBLCK으로 — 기다리는 잠금이면 될 때까지 다시 시도한다."""
    while True:
        try:
            _at_lock_offset(descriptor, lambda: locking(descriptor, non_blocking_mode, 1), settings)
            return
        except OSError as error:
            if error.errno not in _WIN32_CONTENDED:
                raise
            if not blocking:
                message = f"잠금을 다른 쪽이 쥐고 있다: {error}"
                raise BlockingIOError(errno.EAGAIN, message) from error
        time.sleep(settings.win32_poll_seconds)


def _at_lock_offset(
    descriptor: int, action: Callable[[], None], settings: FileLockSettings = FILE_LOCK
) -> None:
    """`msvcrt.locking`은 서술자의 지금 위치에서 잠근다 — 잠금 자리로 옮겼다가 위치를 되돌린다."""
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, settings.win32_lock_offset, os.SEEK_SET)
    try:
        action()
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)
