"""Hydrogen 글자 귀(W-114a, D187) — 시각·줄은 Transcribe, 줄 글자는 Hydrogen(순수).

Gemini 3.5 Transcribe가 들은 청크의 줄(아이디 · 초안 글자)을 Gemini 4 Hydrogen이
같은 청크 WAV로 다시 듣는다. Hydrogen은 단어 시각을 주지 않으므로 줄은 **아이디로만**
맞춘다 — 번역 `translation.parse`와 같은 계약이다: 보낸 줄 아이디와 같은 아이디가 꼭
한 번씩 와야 받는다. 응답 모양이 틀리거나, 모르는 아이디·겹친 아이디·빠진 줄·빈 글자가
있으면 **그 청크 전체**가 Transcribe 초안 글자 그대로이고 까닭이 남는다(맞는 줄만 골라
받지 않는다 — 어긋난 응답은 줄 경계를 다르게 들었다는 뜻이다. 빈 성공도 없다, 규칙 9).

이 계획은 줄의 아이디·차례·수를 보낸 그대로 두고 글자만 낸다 — 어느 클래스에도 시각
칸이 없다(시각의 원천은 Transcribe `word_info`, 규칙 1). `speech=false`(그 줄 시간에
말이 없다)는 초안 글자를 두고 표시만 한다 — 줄을 지우거나 비우지 않고, 그 줄의 `ko`는
읽지 않는다. `extra`(줄이 덮지 않은 시간에 들린 말)는 시각이 없어 어느 줄에도 붙이지
않고 기록만 한다. 거부된 응답의 `extra`는 남기지 않는다.

이 모듈은 전송·전사·서비스·DB·파일·조판 계층을 모른다(`check_hydrogen_text_stays_pure`).
요청 글과 부르기는 W-114b(`hydrogen_text_io`), 1단계 연결은 W-114c의 몫이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from vlogsrt.project.raw.config import HYDROGEN_TEXT_PLAN_VERSION

SOURCE_HYDROGEN = "hydrogen"
SOURCE_DRAFT = "draft"

REASON_SHAPE = "shape"
"""응답이 `{items: [{id, ko, speech}], extra?: [{after_id, ko}]}` 모양이 아니다."""
REASON_UNKNOWN_ID = "unknown-id"
"""보내지 않은 줄 아이디가 왔다(`extra`의 `after_id` 포함)."""
REASON_DUPLICATE_ID = "duplicate-id"
REASON_MISSING_ID = "missing-id"
REASON_EMPTY_TEXT = "empty-text"
"""말이 있다(`speech=true`)는 줄의 글자가 비었다."""


@dataclass(frozen=True, slots=True)
class DraftLine:
    """보낸 줄 하나 — Transcribe 줄 아이디와 그 초안 글자. 요청에 실을 시각은 W-114b가 따로 든다."""

    line_id: str
    text: str


@dataclass(frozen=True, slots=True)
class PlannedLine:
    """줄 하나의 글자 계획 — `source`가 `draft`면 Transcribe 초안 글자 그대로다."""

    line_id: str
    text: str
    source: str
    no_speech: bool
    """Hydrogen이 그 줄 시간에 말이 없다고 했다 — 「확인 필요」 재료일 뿐 줄은 그대로다."""


@dataclass(frozen=True, slots=True)
class ExtraSpeech:
    """줄이 덮지 않은 시간에 들린 말 — `after_id`가 None이면 첫 줄 앞이다. 기록만 한다."""

    after_id: str | None
    text: str


@dataclass(frozen=True, slots=True)
class LineTextPlan:
    version: str
    accepted: bool
    reason: str | None
    detail: str | None
    lines: tuple[PlannedLine, ...]
    extra: tuple[ExtraSpeech, ...]


class _Rejected(ValueError):
    """응답이 보낸 줄과 맞지 않는다 — `plan`이 받아 그 청크를 초안 그대로 둔다."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _Heard:
    text: str
    speech: bool


def plan(lines: Sequence[DraftLine], reply: object) -> LineTextPlan:
    """보낸 줄(차례 그대로)과 Hydrogen 응답 본문 → 줄마다 글자. 같은 입력이면 같은 결과다."""
    wanted = _sent_ids(lines)
    try:
        heard = _items(reply, wanted)
        extra = _extra(reply, wanted)
    except _Rejected as rejected:
        return _draft_plan(lines, rejected)
    return LineTextPlan(
        version=HYDROGEN_TEXT_PLAN_VERSION,
        accepted=True,
        reason=None,
        detail=None,
        lines=tuple(_planned(line, heard[line.line_id]) for line in lines),
        extra=extra,
    )


def _sent_ids(lines: Sequence[DraftLine]) -> tuple[str, ...]:
    seen: set[str] = set()
    for line in lines:
        if line.line_id in seen:
            raise ValueError(f"보낸 줄 아이디가 겹친다: {line.line_id}")
        seen.add(line.line_id)
    return tuple(line.line_id for line in lines)


def _items(reply: object, wanted: tuple[str, ...]) -> dict[str, _Heard]:
    items = reply.get("items") if isinstance(reply, Mapping) else None
    if not isinstance(items, list):
        raise _Rejected(REASON_SHAPE, "응답에 items 목록이 없다")
    heard: dict[str, _Heard] = {}
    for item in items:
        key, row = _item(item)
        if key not in wanted:
            raise _Rejected(REASON_UNKNOWN_ID, f"모르는 줄 아이디가 왔다: {key}")
        if key in heard:
            raise _Rejected(REASON_DUPLICATE_ID, f"같은 줄이 두 번 왔다: {key}")
        if row.speech and not row.text:
            raise _Rejected(REASON_EMPTY_TEXT, f"말이 있다는 줄의 글자가 비었다: {key}")
        heard[key] = row
    missing = [key for key in wanted if key not in heard]
    if missing:
        raise _Rejected(REASON_MISSING_ID, f"글자가 빠졌다: {len(missing)}줄 (첫 줄 {missing[0]})")
    return heard


def _item(item: object) -> tuple[str, _Heard]:
    if not isinstance(item, Mapping):
        raise _Rejected(REASON_SHAPE, "응답 줄의 모양이 다르다")
    key, text, speech = item.get("id"), item.get("ko"), item.get("speech")
    if not isinstance(key, str) or not isinstance(text, str) or not isinstance(speech, bool):
        raise _Rejected(REASON_SHAPE, "응답 줄의 모양이 다르다 (id·ko는 글자, speech는 참거짓)")
    return key, _Heard(" ".join(text.split()), speech)


def _extra(reply: object, wanted: tuple[str, ...]) -> tuple[ExtraSpeech, ...]:
    rows = reply.get("extra", []) if isinstance(reply, Mapping) else []
    if not isinstance(rows, list):
        raise _Rejected(REASON_SHAPE, "응답의 extra가 목록이 아니다")
    heard = (_extra_row(row, wanted) for row in rows)
    return tuple(row for row in heard if row.text)


def _extra_row(row: object, wanted: tuple[str, ...]) -> ExtraSpeech:
    """extra 한 줄 — 글자가 비면 들은 것이 없다(호출자가 거른다)."""
    if not isinstance(row, Mapping):
        raise _Rejected(REASON_SHAPE, "extra 줄의 모양이 다르다")
    after, text = row.get("after_id"), row.get("ko")
    if (after is not None and not isinstance(after, str)) or not isinstance(text, str):
        raise _Rejected(REASON_SHAPE, "extra 줄의 모양이 다르다 (after_id는 글자·null, ko는 글자)")
    if after is not None and after not in wanted:
        raise _Rejected(REASON_UNKNOWN_ID, f"extra가 모르는 줄 뒤에 있다: {after}")
    return ExtraSpeech(after, " ".join(text.split()))


def _planned(line: DraftLine, heard: _Heard) -> PlannedLine:
    if not heard.speech:
        return PlannedLine(line.line_id, line.text, SOURCE_DRAFT, True)
    return PlannedLine(line.line_id, heard.text, SOURCE_HYDROGEN, False)


def _draft_plan(lines: Sequence[DraftLine], rejected: _Rejected) -> LineTextPlan:
    return LineTextPlan(
        version=HYDROGEN_TEXT_PLAN_VERSION,
        accepted=False,
        reason=rejected.reason,
        detail=rejected.detail,
        lines=tuple(PlannedLine(line.line_id, line.text, SOURCE_DRAFT, False) for line in lines),
        extra=(),
    )


__all__ = [
    "REASON_DUPLICATE_ID",
    "REASON_EMPTY_TEXT",
    "REASON_MISSING_ID",
    "REASON_SHAPE",
    "REASON_UNKNOWN_ID",
    "SOURCE_DRAFT",
    "SOURCE_HYDROGEN",
    "DraftLine",
    "ExtraSpeech",
    "LineTextPlan",
    "PlannedLine",
    "plan",
]
