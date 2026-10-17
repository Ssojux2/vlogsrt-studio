"""One UI server per checkout; opening the launcher again reuses its URL.

The lock file lives in `$TMPDIR/vlogsrt-ui-<uid>/<sha20 of the checkout>.json`. The owner removes
its own file on exit while still holding the lock, and sweeps siblings nobody holds (D63 H) —
before this, every test that made an instance for a temporary checkout left one file behind.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from vlogsrt import log
from vlogsrt.scratch_dirs import SCRATCH_SWEEP, ScratchSweepSettings

LOCK_TOKEN_LENGTH = 20
"""Hex digits of the checkout hash in the lock file name — the sweep pattern reads it too."""
LOCK_FILE_NAME = re.compile(rf"[0-9a-f]{{{LOCK_TOKEN_LENGTH}}}\.json")
"""Only files this class names are swept — anything else in the directory is left alone."""


@dataclass(frozen=True)
class UiInstanceSettings:
    """Launcher timings — 외부 근거 없는 조정 대상(D63 H).

    `port_read_attempts` × `port_read_retry_seconds` (1 s) is how long a second launcher waits for
    the owner to publish its port right after taking the lock. `lock_open_attempts` bounds the
    retry when the opened file was unlinked or replaced before our lock landed (an exiting owner
    or a sweeper removed it) — each retry opens the path afresh.
    """

    port_read_attempts: int = 20
    port_read_retry_seconds: float = 0.05
    lock_open_attempts: int = 5
    failure_examples: int = 3
    """How many lock files that could not be swept are named in the warning."""

    def __post_init__(self) -> None:
        counts = (self.port_read_attempts, self.lock_open_attempts, self.failure_examples)
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in counts):
            raise ValueError(f"UI launcher counts must be positive integers: {self}")
        if not self.port_read_retry_seconds > 0:
            raise ValueError(f"port_read_retry_seconds must be positive: {self}")


UI_INSTANCE = UiInstanceSettings()


def same_file(descriptor: int, path: Path) -> bool:
    """Is the open descriptor still the file at `path`? False once it was unlinked or replaced."""
    opened = os.fstat(descriptor)
    try:
        current = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


class UiInstance:
    def __init__(
        self,
        project: Path,
        port: int,
        settings: UiInstanceSettings = UI_INSTANCE,
        sweep: ScratchSweepSettings = SCRATCH_SWEEP,
    ) -> None:
        token = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:LOCK_TOKEN_LENGTH]
        directory = Path(tempfile.gettempdir()) / f"vlogsrt-ui-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / f"{token}.json"
        self.port = port
        self.handle: TextIO | None = None
        self.owned = False
        self._settings = settings
        self._sweep = sweep

    def __enter__(self) -> UiInstance:
        for _ in range(self._settings.lock_open_attempts):
            handle = self._open()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                port = self._published_port(handle)
                if same_file(handle.fileno(), self.path):
                    self.handle = handle
                    self.port = self.port if port is None else port
                    return self
                handle.close()  # the holder removed it meanwhile — look again
                continue
            if not same_file(handle.fileno(), self.path):
                # We locked an inode that an exiting owner or a sweeper already unlinked.
                handle.close()
                continue
            self.handle = handle
            self.owned = True
            self._publish()
            try:
                sweep_stale_locks(
                    self.path.parent,
                    keep=self.path,
                    limit=self._sweep.entry_limit,
                    examples=self._settings.failure_examples,
                )
            except BaseException:
                self.__exit__()  # `with` never reaches __exit__ when __enter__ raises
                raise
            return self
        raise OSError(f"UI lock file kept changing under us: {self.path}")

    def _open(self) -> TextIO:
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        return os.fdopen(descriptor, "r+", encoding="utf-8")

    def _published_port(self, handle: TextIO) -> int | None:
        # The first launch publishes its port immediately after taking the lock.
        for _ in range(self._settings.port_read_attempts):
            handle.seek(0)
            try:
                return int(json.load(handle)["port"])
            except (ValueError, KeyError):
                time.sleep(self._settings.port_read_retry_seconds)
        return None

    def _publish(self) -> None:
        assert self.handle is not None
        self.handle.seek(0)
        self.handle.truncate()
        json.dump({"pid": os.getpid(), "port": self.port}, self.handle)
        self.handle.flush()

    def wait_until_listening(self, timeout: float = 8.0) -> bool:
        """A held lock can belong to a starting or shutting-down server."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/config", timeout=0.5
                ) as response:
                    payload = json.load(response)
                    if (
                        isinstance(payload, dict)
                        and payload.get("title") == "vlogsrt 원본 브라우저"
                    ):
                        return True
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        return False

    def __exit__(self, *_: object) -> None:
        if self.handle is None:
            return
        try:
            if self.owned:
                # Unlink while still locked: a launcher that opened this inode either fails
                # its lock (and sees the path gone) or locks it afterwards and retries afresh.
                if same_file(self.handle.fileno(), self.path):
                    self.path.unlink(missing_ok=True)
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def sweep_stale_locks(
    directory: Path, *, keep: Path, limit: int, examples: int = UI_INSTANCE.failure_examples
) -> int:
    """Remove sibling lock files nobody holds — never one we could not lock ourselves.

    A file is stale only if a non-blocking exclusive lock succeeds on it (no live holder). While
    holding that lock we re-check that the path is still the inode we locked, then unlink it.
    """
    removed = 0
    failed: list[str] = []
    try:
        entries = sorted(name for name in os.listdir(directory) if LOCK_FILE_NAME.fullmatch(name))
    except OSError as error:
        log.warn(f"UI 잠금 폴더를 훑지 못했다: {directory} ({error})")
        return 0
    for name in entries[:limit]:
        path = directory / name
        if path == keep:
            continue
        try:
            removed += _remove_if_unheld(path)
        except OSError as error:
            failed.append(f"{name} ({error})")
    if failed:
        log.warn(f"UI 잠금 파일 {len(failed)}개를 치우지 못했다: {', '.join(failed[:examples])}")
    if removed:
        log.info(f"주인 없는 UI 잠금 파일 {removed}개를 지웠다({directory})")
    return removed


def _remove_if_unheld(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # a live server (or launcher) holds it
        if not same_file(descriptor, path):
            return 0
        path.unlink()
        return 1
    finally:
        os.close(descriptor)  # closing drops our lock
