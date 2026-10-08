"""One UI server per checkout; opening the launcher again reuses its URL."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import TextIO


class UiInstance:
    def __init__(self, project: Path, port: int) -> None:
        token = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:20]
        directory = Path(tempfile.gettempdir()) / f"vlogsrt-ui-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / f"{token}.json"
        self.port = port
        self.handle: TextIO | None = None
        self.owned = False

    def __enter__(self) -> UiInstance:
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The first launch publishes its port immediately after taking the lock.
            for _ in range(20):
                self.handle.seek(0)
                try:
                    payload = json.load(self.handle)
                    self.port = int(payload["port"])
                    break
                except (ValueError, KeyError):
                    time.sleep(0.05)
            return self
        self.owned = True
        self.handle.seek(0)
        self.handle.truncate()
        json.dump({"pid": os.getpid(), "port": self.port}, self.handle)
        self.handle.flush()
        return self

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
        if self.handle is not None:
            if self.owned:
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
