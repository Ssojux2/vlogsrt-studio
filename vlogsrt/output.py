"""SRT 파싱·직렬화.

출력 개행은 항상 `\\n`이다. 파싱은 CRLF/LF와 BOM을 모두 받아들인다.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Cue

_BOM = "﻿"
_INDEX_RE = re.compile(r"^\s*\d+\s*$")
_TIME = r"(\d+):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
_TIMELINE_RE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")


def format_timecode(seconds: float) -> str:
    """`HH:MM:SS,mmm`. 밀리초는 반올림(half-up)한다 — 12.4105 → 00:00:12,411."""
    total_ms = int(max(0.0, seconds) * 1000 + 0.5)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_timecode(value: str) -> float:
    match = re.fullmatch(rf"\s*{_TIME}\s*", value)
    if not match:
        raise RuntimeError(f"타임코드 형식이 아닙니다: {value!r}")
    hours, minutes, secs, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis.ljust(3, "0")) / 1000.0


def _normalize_newlines(text: str) -> list[str]:
    body = text.lstrip(_BOM).replace("\r\n", "\n").replace("\r", "\n")
    return body.split("\n")


def _starts_cue(lines: list[str], pos: int) -> bool:
    """빈 줄 뒤가 새 큐인지 판단한다. 큐 본문에 빈 줄이 있는 파일 때문에 필요하다."""
    if pos >= len(lines):
        return False
    if _TIMELINE_RE.match(lines[pos]):
        return True
    if _INDEX_RE.match(lines[pos]):
        return pos + 1 < len(lines) and bool(_TIMELINE_RE.match(lines[pos + 1]))
    return False


def parse_srt(text: str) -> list[Cue]:
    lines = _normalize_newlines(text)
    cues: list[Cue] = []
    pos = 0
    while pos < len(lines):
        if not lines[pos].strip():
            pos += 1
            continue
        if _INDEX_RE.match(lines[pos]) and _starts_cue(lines, pos):
            pos += 1
        timeline = _TIMELINE_RE.match(lines[pos]) if pos < len(lines) else None
        if not timeline:
            raise RuntimeError(
                f"SRT {pos + 1}번째 줄에서 타임코드를 찾지 못했습니다: {lines[pos]!r}"
            )
        start_raw, end_raw = lines[pos].split("-->")[:2]
        start = parse_timecode(start_raw)
        end = parse_timecode(end_raw.split()[0] if end_raw.split() else end_raw)
        pos += 1
        body, pos = _collect_body(lines, pos)
        cues.append(Cue(start=start, end=end, lines=tuple(body), index=len(cues) + 1))
    return cues


def _collect_body(lines: list[str], pos: int) -> tuple[list[str], int]:
    body: list[str] = []
    while pos < len(lines):
        if lines[pos].strip():
            body.append(lines[pos])
            pos += 1
            continue
        peek = pos
        while peek < len(lines) and not lines[peek].strip():
            peek += 1
        if peek >= len(lines) or _starts_cue(lines, peek):
            pos = peek
            break
        body.append("")
        pos += 1
    while body and not body[-1].strip():
        body.pop()
    return body, pos


def write_srt(cues: list[Cue]) -> str:
    blocks = []
    for number, cue in enumerate(cues, start=1):
        timeline = f"{format_timecode(cue.start)} --> {format_timecode(cue.end)}"
        body = "\n".join(cue.lines) if cue.lines else ""
        blocks.append(f"{number}\n{timeline}\n{body}\n")
    return "\n".join(blocks)


def save_srt(cues: list[Cue], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(write_srt(cues), encoding="utf-8")
    return path
