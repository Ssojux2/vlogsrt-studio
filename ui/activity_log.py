"""서버 터미널 줄을 작업대 「기록」 판에서도 읽게 한다 — 표준 라이브러리만 쓴다(D61).

긴 로컬 단계(분석 프록시·미리보기·썸네일)의 WARN·진행은 터미널에만 찍혀, 브라우저만 보는
사람은 멈춤과 느림을 가를 수 없었다. `sys.stdout`·`sys.stderr`를 감싸 **터미널로 가는 글은
바이트 그대로 내보내고**, 끝난 줄만 ANSI 표지를 걷고 키처럼 보이는 글을 가려 메모리의 고정
길이 버퍼에 남긴다. 디스크에는 쓰지 않는다. 감싸기는 `ui/app.py`의 서버 실행에서만 한다.
"""

from __future__ import annotations

import re
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TextIO

LOG_LINE_LIMIT = 2000
"""메모리에 남기는 줄 수 — 판은 열린 동안 2초마다 새 줄만 가져가므로 이만큼이면 몇 시간치 서버
기록이고, 며칠 켜 둔 서버에서도 메모리가 자라지 않는다(한 줄 상한과 곱해 몇 MB 안)."""

LINE_MAX_CHARS = 2000
"""한 줄 상한 — 줄바꿈 없이 쏟아지는 출력이 버퍼 하나를 끝없이 키우지 않게 이 길이에서 끊는다."""

_SOFT_CUT_FROM = LINE_MAX_CHARS // 2
"""긴 줄을 끊을 때 이 자리부터 상한 사이의 마지막 빈칸에서 끊는다 — 낱말(특히 키) 한가운데를
덜 자른다. 그 사이에 빈칸이 없을 때만 상한에서 바로 자른다."""

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
"""CSI(색·커서) · OSC(rich의 링크) · 그 밖의 두 글자 이스케이프."""

_SECRETS = (
    (re.compile(r"\bsk-(?:[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-]*$)"), "sk-…"),
    (re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE), r"\1 …"),
    (
        re.compile(r"((?:x-)?api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+", re.IGNORECASE),
        r"\1…",
    ),
)
"""키처럼 보이는 글 — 저장 줄에서만, **할 수 있는 만큼** 가린다. 터미널은 원래 그 글을 보던 곳이라
그대로 둔다. 줄 끝에 걸친 `sk-` 꼬리는 짧아도 가린다(나머지는 다음 줄로 접혔다)."""

_OPEN_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9_\-]*|\bBearer(?:\s+[A-Za-z0-9._~+/=\-]*)?"
    r"|(?:x-)?api[_-]?key[\"']?\s*[:=]\s*[\"']?[^\s\"',;]*)$",
    re.IGNORECASE,
)
"""줄이 키 한가운데서 끝났는가 — rich는 긴 낱말을 터미널 폭에서 접고(`fold`), 긴 줄은 상한에서
잘린다. 그러면 키의 나머지가 다음 줄 머리로 간다."""

_LEADING_TOKEN = re.compile(r"^[A-Za-z0-9_\-.~+/=]+")
"""앞 줄이 키 한가운데서 끝났을 때 이 줄 머리에서 가릴 글자 — 키에 들 수 있는 글자만."""

_ERROR_HEADS = ("ERR", "Traceback", "CRITICAL")
_WARN_HEADS = ("WARN",)


@dataclass(frozen=True, slots=True)
class LogLine:
    seq: int
    wall_time: float
    stream: str
    level: str
    text: str


def plain_line(raw: str) -> str:
    """ANSI 표지를 걷고, 캐리지 리턴으로 덮어 쓴 줄(진행 막대)은 마지막 모습만 남긴다."""
    text = _ANSI.sub("", raw).rstrip("\r")
    return text[text.rfind("\r") + 1 :]


def mask_line(text: str, *, continues_secret: bool = False) -> tuple[str, bool]:
    """(가린 줄, 키 한가운데서 끝났는가) — 앞 줄이 그랬으면 이 줄 머리의 키 글자도 가린다."""
    head = _LEADING_TOKEN.match(text) if continues_secret else None
    whole_line_token = head is not None and head.end() == len(text)
    open_tail = whole_line_token or _OPEN_SECRET.search(text) is not None
    # 줄 안의 키를 먼저 가린다 — 머리 가리기가 `api_key=` 같은 표지를 먼저 먹으면 그 값이 남는다.
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    if continues_secret:
        text = _LEADING_TOKEN.sub("…", text)
    return text, open_tail


def clean_line(raw: str) -> str:
    """저장할 줄 하나 — 앞뒤 줄을 모를 때(한 줄짜리)의 걷기와 가리기."""
    return mask_line(plain_line(raw))[0]


def split_long(line: str) -> list[str]:
    """한 줄을 상한 이하 조각으로 — 되도록 빈칸에서 끊는다(`_SOFT_CUT_FROM`)."""
    pieces: list[str] = []
    while len(line) > LINE_MAX_CHARS:
        space = line.rfind(" ", _SOFT_CUT_FROM, LINE_MAX_CHARS)
        cut = space + 1 if space >= 0 else LINE_MAX_CHARS
        pieces.append(line[:cut])
        line = line[cut:]
    pieces.append(line)
    return pieces


def line_level(text: str) -> str:
    """rich 로그(`WARN`·`ERR`)와 uvicorn·파이썬 머리(`WARNING:`·`ERROR:`·`Traceback`)로 가른다."""
    head = text.lstrip()
    if head.startswith(_ERROR_HEADS):
        return "error"
    if head.startswith(_WARN_HEADS):
        return "warn"
    return "info"


class ActivityLog:
    """끝난 줄의 고정 길이 버퍼 — 번호(`seq`)는 서버가 도는 동안 1부터 줄곧 늘어난다.

    `boot_id`는 버퍼(=서버 프로세스)마다 새로 뽑는다 — 화면은 이것이 바뀌면 서버가 다시 떴다고 보고
    번호를 처음부터 다시 받는다(번호 크기만으로는 다시 뜬 서버의 번호가 앞질렀을 때를 모른다).
    """

    def __init__(self, limit: int = LOG_LINE_LIMIT) -> None:
        self._lines: deque[LogLine] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._seq = 0
        self._open_secret: dict[str, bool] = {}
        self.boot_id = secrets.token_hex(8)

    def record(self, stream: str, raw: str) -> None:
        plain = plain_line(raw)
        level = line_level(plain)  # 가리기 전에 — 머리 낱말(WARN)이 가려져도 색은 그대로
        with self._lock:
            text, open_tail = mask_line(
                plain, continues_secret=self._open_secret.get(stream, False)
            )
            self._open_secret[stream] = open_tail
            self._seq += 1
            self._lines.append(LogLine(self._seq, time.time(), stream, level, text))

    def lines_since(self, seq: int) -> tuple[LogLine, ...]:
        with self._lock:
            return tuple(line for line in self._lines if line.seq > seq)

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq


SERVER_LOG = ActivityLog()
"""이 서버 프로세스의 버퍼 — 진입점이 감싸고 작업대 경로(`/vl-activity`)가 읽는다."""


class TeeStream:
    """글은 먼저 원래 흐름으로 그대로 — 그다음 끝난 줄만 버퍼에. 나머지 흐름 규약은 원래 것이다."""

    def __init__(self, target: TextIO, stream: str, buffer: ActivityLog) -> None:
        self._target = target
        self._stream = stream
        self._buffer = buffer
        self._pending = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        written = self._target.write(text)
        self._capture(text)
        return written

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._target.flush()

    def __getattr__(self, name: str) -> Any:
        # rich·uvicorn이 묻는 isatty·fileno·encoding 등은 원래 흐름이 답한다 — 터미널 판정이 같다.
        return getattr(self._target, name)

    def _capture(self, text: str) -> None:
        with self._lock:
            self._pending += text
            *complete, pending = self._pending.split("\n")
            # 진행 막대(tqdm 꼴)는 CR로 같은 줄을 덮어 쓴다 — 쌓지 않고 마지막 모습만 둔다.
            # 맨 끝의 CR은 남긴다: 다음 쓰기가 `\n`이면 CRLF 줄바꿈이다.
            overwrite = pending.rfind("\r", 0, len(pending) - 1)
            pending = pending[overwrite + 1 :]
            *overflow, self._pending = split_long(pending)
            complete += overflow
            for line in complete:
                for piece in split_long(line):
                    self._buffer.record(self._stream, piece)


@contextmanager
def captured(buffer: ActivityLog = SERVER_LOG) -> Iterator[ActivityLog]:
    """이 안에서 `sys.stdout`·`sys.stderr`를 감싼다 — 나갈 때 원래 흐름으로 되돌린다."""
    original_out, original_err = sys.stdout, sys.stderr
    if original_out is not None:
        sys.stdout = TeeStream(original_out, "stdout", buffer)
    if original_err is not None:
        sys.stderr = TeeStream(original_err, "stderr", buffer)
    try:
        yield buffer
    finally:
        sys.stdout, sys.stderr = original_out, original_err
