"""검토 메모의 두 목소리 — 조립이 쓰는 기계 줄과 사람이 쓰는 메모(D57 C 고침 2).

검토 조각의 `notes`는 글자 한 칸이다. 조립은 거기에 근거를 쓴다 — 자동 제외(`자동 제외: ` +
근거 JSON)와 확인 필요(`확인 필요: …`). 사람이 사용·제외를 고르면 앞에 `수동 선택 · `가 붙고,
합치기는 두 메모를 줄바꿈으로 잇는다. 사람 메모도 같은 칸에 산다.

이 모듈은 그 칸을 읽고(기계 줄 · 사람 메모) 사람 메모만 갈아 끼운다. 새 형식을 만들지 않는다 —
머리말과 근거 JSON은 자동 정리(`review_workspace._auto_excluded`)가 쓰는 그대로이고, 사람 메모는
합치기와 같은 줄바꿈으로 잇는다. 순수 함수만 있다(입출력 없음).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

AUTO_NOTE_PREFIX = "자동 제외: "
"""자동 제외 근거 메모의 머리 — 뒤에 근거 JSON이 붙는다(작업대도 이 머리로 찾는다)."""

REVIEW_NOTE_PREFIX = "확인 필요: "
"""조립이 분석 미완료·검토 사유를 적는 메모의 머리."""

MANUAL_SELECTION_PREFIX = "수동 선택 · "
"""자동 제외 근거가 남은 조각을 사람이 사용/제외로 고른 표지 — `apply_edit`이 단다."""

AUTO_PROVENANCE_KEYS = frozenset(
    ("analysis_revision_id", "end", "reason", "segment_id", "source_id", "start")
)
"""자동 정리가 근거 JSON에 늘 싣는 칸 — 머리말만 같은 사람 글과 가른다(`_auto_excluded`)."""

_DECODER = json.JSONDecoder()


@dataclass(frozen=True, slots=True)
class AutoNoteLine:
    """자동 제외 근거 한 줄 — 기계 부분(쓴 글자 그대로)·근거·그 뒤에 붙은 사람 글."""

    machine: str
    provenance: Mapping[str, object]
    trailing: str


@dataclass(frozen=True, slots=True)
class ReviewNoteParts:
    """메모를 가른 결과 — 기계 줄(쓴 글자 그대로)·사람 메모·사람 글이 앞에 있었나."""

    machine: tuple[str, ...]
    human: str
    human_first: bool


def auto_note_line(line: str) -> AutoNoteLine | None:
    """한 줄이 자동 정리가 쓴 근거면 그 근거 — 아니면 None.

    줄 맨 앞이 머리말(`수동 선택 · `이 앞에 붙어도 된다)이고 그 뒤가 자동 정리가 쓰는 칸을 다 가진
    JSON 객체일 때만이다. 옛 작업대는 메모 칸을 이 근거로 미리 채워, 사람이 그 뒤에 말을 덧붙인
    줄이 남아 있다 — JSON 객체가 끝난 자리부터는 사람 글이다(`trailing`).
    """
    body_at = len(MANUAL_SELECTION_PREFIX) if line.startswith(MANUAL_SELECTION_PREFIX) else 0
    if not line.startswith(AUTO_NOTE_PREFIX, body_at):
        return None
    try:
        provenance, end = _DECODER.raw_decode(line, body_at + len(AUTO_NOTE_PREFIX))
    except ValueError:
        return None
    if not isinstance(provenance, Mapping) or not provenance.keys() >= AUTO_PROVENANCE_KEYS:
        return None
    return AutoNoteLine(line[:end], provenance, line[end:])


def has_auto_note(notes: str) -> bool:
    """자동 정리가 쓴 근거가 한 줄이라도 남았나 — 결정한 제외 제안을 세는 표지(N)."""
    return any(auto_note_line(line) is not None for line in notes.split("\n"))


def drop_auto_note_lines(notes: str) -> str:
    """자동 제외 근거 줄만 거둔다 — 줄 뒤에 붙은 사람 글과 다른 줄은 그대로다.

    범위 표시를 지워 자동으로 돌릴 때(D100 §4) 쓴다: 사용으로 둔 조각에 근거 줄이 남으면
    「자동 제외 전체 복원」과 같은 사람 결정으로 읽힌다(`_release_stale_tier_proposal`과 같은 까닭).
    """
    if not has_auto_note(notes):
        return notes
    kept: list[str] = []
    for line in notes.split("\n"):
        auto = auto_note_line(line)
        if auto is None:
            kept.append(line)
        elif auto.trailing.strip():
            kept.append(auto.trailing.strip())
    return "\n".join(kept).strip("\n")


def split_review_notes(notes: str) -> ReviewNoteParts:
    """기계 줄과 사람 메모로 가른다 — 작업대 메모 칸은 사람 메모만 보이고 고친다.

    기계 줄은 조립이 쓰는 두 머리다(자동 제외 근거 · 확인 필요). 그 밖의 줄은 모두 사람 글이다 —
    머리말만 같고 근거가 아닌 줄(사람이 적은 `자동 제외: …`)도 사람 글이다.
    """
    machine: list[str] = []
    human: list[str] = []
    first: bool | None = None
    for line in notes.split("\n") if notes else ():
        auto = auto_note_line(line)
        if auto is not None:
            machine.append(auto.machine)
            text, is_machine = auto.trailing.strip(), True
        elif line.startswith(REVIEW_NOTE_PREFIX):
            machine.append(line)
            text, is_machine = "", True
        else:
            text, is_machine = line, False
        if text or not is_machine:  # 사람 글 안의 빈 줄(문단)은 그대로 둔다
            human.append(text)
        if first is None and (is_machine or text):
            first = not is_machine
    return ReviewNoteParts(tuple(machine), "\n".join(human).strip("\n"), bool(first))


def compose_review_notes(notes: str, human: str) -> str:
    """사람 메모만 갈아 끼운 메모 — 기계 줄은 글자 그대로, 사람 글은 원래 있던 쪽(앞·뒤)에 둔다.

    사람 메모가 그대로면 메모를 한 글자도 바꾸지 않는다. 비운 칸은 사람 메모를 지울 뿐이다. 여러
    줄 칸의 줄바꿈(CRLF 포함)은 합치기와 같은 `\\n`으로 맞춘다.
    """
    parts = split_review_notes(notes)
    text = human.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text == parts.human.strip():
        return notes
    blocks = (text, *parts.machine) if parts.human_first else (*parts.machine, text)
    return "\n".join(block for block in blocks if block)
