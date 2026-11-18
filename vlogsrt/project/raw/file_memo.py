"""그리기 사이의 파일 바이트 기억(D167) — 바뀌지 않은 JSON을 다시 열지 않는다.

작업대 그리기 한 번이 원본마다 같은 JSON(시각 확인 보고서 · 1단계 기록 · 분석 문서 · 영상 검토
기록)을 약 200번 열었다 — 작은 파일은 푸는 것보다 여는 시스템 호출이 더 비쌌다(D167 실측).
읽기 범위(`read_scope`, D63)는 그리기 한 번만 살아서 그리기 사이에는 매번 처음부터 열었다.

- 기억하는 것은 **바이트**뿐이다. 부르는 쪽은 매번 새로 푼다 — 사전을 나눠 써서 한쪽의 고침이
  다른 쪽에 새는 일이 없다. 등급 같은 계산 결과는 여기 들지 않는다(D51·D52 — 읽을 때마다
  다시 계산한다).
- 열쇠는 파일 신원(장치 · inode · 크기 · 수정 · 변경 시각, 나노초)이다 — 쓰기·원자적 바꿔치기·지우기
  뒤에는 처음부터 읽는다. 신원을 잰 `stat` 하나가 남는 비용이다(여는 것의 약 1/10).
- 읽는 사이에 파일이 바뀌어 크기가 어긋나면 기억하지 않는다. 기억은 프로세스 안에서만 산다.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

from vlogsrt.project.raw.config import FILE_MEMO, FileMemoSettings

_Identity = tuple[int, int, int, int, int]


def _identity(path: Path) -> _Identity:
    status = os.stat(path)
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


class FileMemo:
    """경로 → (신원, 바이트) — 바이트 합이 상한을 넘으면 가장 오래 안 쓴 것부터 뺀다."""

    def __init__(self, settings: FileMemoSettings = FILE_MEMO) -> None:
        self._settings = settings
        self._entries: OrderedDict[str, tuple[_Identity, bytes]] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()

    def read_bytes(self, path: Path) -> bytes:
        identity = _identity(path)  # 없으면 FileNotFoundError — 읽는 것과 같다
        key = os.fspath(path)
        with self._lock:
            held = self._entries.get(key)
            if held is not None and held[0] == identity:
                self._entries.move_to_end(key)
                return held[1]
        data = path.read_bytes()
        if len(data) == identity[2] and len(data) <= self._settings.max_file_bytes:
            self._keep(key, identity, data)
        return data

    def _keep(self, key: str, identity: _Identity, data: bytes) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._total -= len(old[1])
            self._entries[key] = (identity, data)
            self._total += len(data)
            while self._total > self._settings.max_bytes:
                _key, (_identity, dropped) = self._entries.popitem(last=False)
                self._total -= len(dropped)

    def holds(self, path: Path) -> bool:
        with self._lock:
            return os.fspath(path) in self._entries

    def cached_bytes(self) -> int:
        with self._lock:
            return self._total


_MEMO = FileMemo()


def read_bytes(path: Path) -> bytes:
    """파일 바이트 — 신원이 같으면 기억한 것, 아니면 읽어 기억한다."""
    return _MEMO.read_bytes(path)


def read_text(path: Path) -> str:
    """UTF-8 글 — JSON을 풀 때만 쓴다(`Path.read_text`와 달리 줄 끝 `\\r\\n`을 바꾸지 않는다 —
    JSON에서는 공백일 뿐이다)."""
    return read_bytes(path).decode("utf-8")
