"""프로젝트 용어집(D59 19c) — 고유명사를 한 줄에 하나, 1단계 글자 경로의 `keywords`로만 간다.

용어집은 두 번째 귀(gpt-transcribe)에게만 간다. whisper에는 가지 않는다 — whisper는 힌트를 지시가
아니라 직전 자막으로 읽는다(규칙 6, D16). `RawCaptionContract.keywords`·`RawWorkflowConfig`에도
넣지 않는다: 내부 whisper 청크 열쇠가 그 설정 전체를 해시하므로(`understanding.asr_chunk_key`)
용어 하나를 고치는 것만으로 모든 원본의 whisper를 다시 사게 된다. 용어집은 글자 캐시 열쇠에
`glossary_sha`로만 실린다.

저장은 작업공간 DB의 작은 표에 리비전으로 덧붙이기만 한다 — 마지막 리비전이 이긴다. 표는 처음
저장할 때 만들어지고, 읽기는 표를 만들지 않는다(`asr_epoch`와 같은 모양). 파싱은
`vlogsrt.keywords.parse_keywords`와 같다: 빈 줄은 버리고 `#` 뒤는 메모다. 같은 말은 처음 한 번만
남는다. gpt-transcribe keywords에 `<`·`>`·줄바꿈·제어문자는 못 넣는다 — 하나라도 있으면 저장하지
않는다.

입출력은 작업공간 DB 하나뿐이다 — 네트워크·프로세스·파일을 모른다(`check_invariants` 검사).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vlogsrt.keywords import dedupe, parse_keywords
from vlogsrt.project.raw.models import RawWorkspaceError

TABLE = "project_glossary"
SHA_SCHEMA = "raw-glossary/v1"
FORBIDDEN_CHARACTERS = ("<", ">")
"""gpt-transcribe keywords가 받지 않는 글자 — 줄바꿈·제어문자는 따로 본다."""


@dataclass(frozen=True, slots=True)
class Glossary:
    """용어집의 한 리비전 — `sequence`가 None이면 저장한 적이 없다(빈 용어집)."""

    sequence: int | None = None
    terms: tuple[str, ...] = ()

    @property
    def sha(self) -> str:
        return glossary_sha(self.terms)

    def text(self) -> str:
        """편집 칸에 다시 보일 모양 — 한 줄에 하나."""
        return "\n".join(self.terms)


def glossary_sha(terms: Sequence[str]) -> str:
    """글자 캐시 열쇠에 실리는 용어집 지문 — 순서까지 묶는다(요청에 그 순서로 실린다)."""
    body = json.dumps([SHA_SCHEMA, list(terms)], ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def term_problems(terms: Sequence[str]) -> list[str]:
    """쓸 수 없는 용어 — 빈 말, `<`·`>`, 줄바꿈·제어문자(사람이 고칠 줄을 짚는다)."""
    problems: list[str] = []
    for term in terms:
        if not term.strip():
            problems.append("빈 용어가 있습니다")
        elif any(mark in term for mark in FORBIDDEN_CHARACTERS) or any(
            unicodedata.category(char).startswith("C") for char in term
        ):
            problems.append(
                f"「{_printable(term)}」에 쓸 수 없는 글자(<, >, 줄바꿈·제어문자)가 있습니다"
            )
    return problems


def _printable(term: str) -> str:
    return "".join("?" if unicodedata.category(char).startswith("C") else char for char in term)


def validated(terms: Sequence[str]) -> tuple[str, ...]:
    """검사를 지난 용어만 — 하나라도 어긋나면 아무것도 받지 않는다(조용히 빼지 않는다)."""
    problems = term_problems(terms)
    if problems:
        raise RawWorkspaceError(
            "용어집을 저장하지 않았습니다 — " + " · ".join(problems) + ". 그 줄을 고쳐 주세요."
        )
    return tuple(dedupe(terms))


def parse(text: str) -> tuple[str, ...]:
    """한 줄에 하나 — `parse_keywords`(빈 줄·`#` 메모 버림) 뒤 검사와 중복 제거."""
    return validated(parse_keywords(text))


def current(workspace: Any) -> Glossary:
    """마지막 리비전 — 표나 줄이 없으면 빈 용어집이다. 표를 만들지 않는다."""
    with workspace.connect() as db:
        if not _table_exists(db):
            return Glossary()
        row = db.execute(
            f"SELECT sequence, terms FROM {TABLE} ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return Glossary()
    stored = json.loads(str(row[1]))
    if not isinstance(stored, list) or not all(isinstance(term, str) for term in stored):
        raise RawWorkspaceError("저장된 용어집의 모양이 어긋났습니다 — 용어집을 다시 저장해 주세요")
    return Glossary(sequence=int(row[0]), terms=validated(stored))


def save(workspace: Any, text: str) -> Glossary:
    """새 리비전으로 덧붙인다 — 지금과 같으면 그대로 돌려준다(빈 용어집은 표를 만들지 않는다)."""
    terms = parse(text)
    now = current(workspace)
    if terms == now.terms:
        return now
    with workspace.connect() as db:
        _create(db)
        db.execute("BEGIN IMMEDIATE")
        top = db.execute(f"SELECT MAX(sequence) FROM {TABLE}").fetchone()
        sequence = 0 if top is None or top[0] is None else int(top[0]) + 1
        db.execute(
            f"INSERT INTO {TABLE}(sequence, terms, created_at) VALUES(?,?,?)",
            (sequence, json.dumps(list(terms), ensure_ascii=False), _now()),
        )
        db.commit()
    return Glossary(sequence=sequence, terms=terms)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _table_exists(db: Any) -> bool:
    found = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    return found is not None


def _create(db: Any) -> None:
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} (sequence INTEGER PRIMARY KEY, "
        "terms TEXT NOT NULL, created_at TEXT NOT NULL)"
    )


__all__ = [
    "FORBIDDEN_CHARACTERS",
    "SHA_SCHEMA",
    "TABLE",
    "Glossary",
    "current",
    "glossary_sha",
    "parse",
    "save",
    "term_problems",
    "validated",
]
