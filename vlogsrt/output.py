"""SRT 파싱·직렬화와 검토용 출력.

출력 개행은 항상 `\\n`이다. 파싱은 CRLF/LF와 BOM을 모두 받아들인다.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from .models import Cue

_BOM = "﻿"
_INDEX_RE = re.compile(r"^\s*\d+\s*$")
_TIME = r"(\d+):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
_TIMELINE_RE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
REVIEW_OPEN = "[?"
REVIEW_CLOSE = "]"


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


def diff_ranges(reference: str, hypothesis: str) -> list[tuple[int, int]]:
    """`hypothesis` 기준으로 두 문자열이 갈린 구간을 돌려준다. 인접 구간은 합친다."""
    matcher = SequenceMatcher(None, reference, hypothesis, autojunk=False)
    ranges: list[tuple[int, int]] = []
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or j1 == j2:
            continue
        if ranges and ranges[-1][1] == j1:
            ranges[-1] = (ranges[-1][0], j2)
        else:
            ranges.append((j1, j2))
    return ranges


def mark_review_text(whisper_text: str, canonical_text: str) -> str:
    """갈린 글자만 `[?...]`로 감싼다. 큐 전체를 감싸면 검토 속도가 떨어진다."""
    if not whisper_text or whisper_text == canonical_text:
        return canonical_text
    pieces: list[str] = []
    cursor = 0
    for start, end in diff_ranges(whisper_text, canonical_text):
        pieces.append(canonical_text[cursor:start])
        pieces.append(f"{REVIEW_OPEN}{canonical_text[start:end]}{REVIEW_CLOSE}")
        cursor = end
    pieces.append(canonical_text[cursor:])
    return "".join(pieces)


def to_review_cues(cues: list[Cue]) -> list[Cue]:
    """줄 구조를 유지한 채 마커만 얹는다. 마커가 줄바꿈을 넘지 않도록 줄별로 처리한다."""
    reviewed: list[Cue] = []
    for cue in cues:
        marked = mark_review_text(cue.whisper_text, cue.text)
        reviewed.append(
            Cue(
                start=cue.start,
                end=cue.end,
                lines=tuple(_rewrap(marked, cue.lines)),
                seg_ids=cue.seg_ids,
                origin=cue.origin,
                conf=cue.conf,
                flags=cue.flags,
                whisper_text=cue.whisper_text,
                index=cue.index,
            )
        )
    return reviewed


def _rewrap(marked: str, original_lines: tuple[str, ...]) -> list[str]:
    """마커가 붙은 문자열을 원래 줄 수에 맞춰 다시 나눈다."""
    if len(original_lines) <= 1:
        return [marked]
    words = marked.split(" ")
    target = len(original_lines[0].split(" "))
    head = " ".join(words[:target])
    tail = " ".join(words[target:])
    return [head, tail] if tail else [head]
