"""Durable per-attempt receipts. Unfinished sends remain explicitly uncertain."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_sink: ContextVar[tuple[Any, str] | None] = ContextVar("analysis_request_sink", default=None)


@contextmanager
def scope(workspace: Any, job_id: str, *, live: bool) -> Iterator[None]:
    if not live:
        yield
        return
    with workspace.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS analysis_requests (id TEXT PRIMARY KEY, "
            "job_id TEXT NOT NULL, "
            "stage TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, note TEXT, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.commit()
    token = _sink.set((workspace, job_id))
    try:
        yield
    finally:
        _sink.reset(token)


def begin(stage: str, fingerprint: str) -> str | None:
    sink = _sink.get()
    if sink is None:
        return None
    workspace, job_id = sink
    identifier = uuid.uuid4().hex
    with workspace.connect() as db:
        db.execute(
            "INSERT INTO analysis_requests(id,job_id,stage,fingerprint,status) VALUES(?,?,?,?,?)",
            (identifier, job_id, stage, fingerprint, "response_uncertain"),
        )
        db.commit()
    return identifier


def finish(identifier: str | None, *, received: bool, note: str = "") -> None:
    sink = _sink.get()
    if sink is None or identifier is None:
        return
    with sink[0].connect() as db:
        db.execute(
            "UPDATE analysis_requests SET status=?,note=? WHERE id=?",
            ("response_received" if received else "response_uncertain", note, identifier),
        )
        db.commit()
