"""Durable per-attempt receipts. Unfinished sends remain explicitly uncertain.

Each row may also carry the measured usage and the priced cost of its response
(`record_usage`). Cost is computed from the pricing snapshot, not the provider's invoice.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vlogsrt.project.request_accounting import MeasuredUsage

_sink: ContextVar[tuple[Any, str] | None] = ContextVar("analysis_request_sink", default=None)
_open: ContextVar[str | None] = ContextVar("analysis_request_open", default=None)

_USAGE_COLUMNS = (
    ("model", "TEXT"),
    ("billed_seconds", "TEXT"),
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cost_usd", "TEXT"),
    ("unpriced_reason", "TEXT"),
    ("pricing_revision", "TEXT"),
)
"""Usage columns appended to the original table; existing workspaces gain them in place."""


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create the table, then add any usage column an older workspace lacks (idempotent)."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS analysis_requests (id TEXT PRIMARY KEY, "
        "job_id TEXT NOT NULL, "
        "stage TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, note TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    present = {row[1] for row in db.execute("PRAGMA table_info(analysis_requests)")}
    for name, kind in _USAGE_COLUMNS:
        if name not in present:
            db.execute(f"ALTER TABLE analysis_requests ADD COLUMN {name} {kind}")
    db.commit()


@contextmanager
def scope(workspace: Any, job_id: str, *, live: bool) -> Iterator[None]:
    if not live:
        yield
        return
    with workspace.connect() as db:
        ensure_schema(db)
    sink_token = _sink.set((workspace, job_id))
    open_token = _open.set(None)
    try:
        yield
    finally:
        _open.reset(open_token)
        _sink.reset(sink_token)


def recording() -> bool:
    """True while a live scope is open — outside one there is nowhere to write."""
    return _sink.get() is not None


def open_attempt() -> str | None:
    """The attempt opened by `begin` that has not finished yet, if any."""
    return _open.get()


def begin(stage: str, fingerprint: str, *, bind: bool = True) -> str | None:
    """Record a send as uncertain. `bind` makes it the attempt that usage attaches to."""
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
    if bind:
        _open.set(identifier)
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
    if _open.get() == identifier:
        _open.set(None)


def record_usage(usage: MeasuredUsage, identifier: str | None = None) -> None:
    """Attach measured usage to an attempt row (default: the open attempt).

    A second response on the same attempt adds to the first instead of replacing it:
    both were billed, and a replaced row would under-report.
    """
    sink = _sink.get()
    target = identifier if identifier is not None else _open.get()
    if sink is None or target is None:
        return
    with sink[0].connect() as db:
        prior = db.execute(
            "SELECT model,billed_seconds,input_tokens,output_tokens,cost_usd,unpriced_reason "
            "FROM analysis_requests WHERE id=?",
            (target,),
        ).fetchone()
        merged = usage if prior is None or prior[0] is None else _merged(prior, usage)
        db.execute(
            "UPDATE analysis_requests SET model=?,billed_seconds=?,input_tokens=?,"
            "output_tokens=?,cost_usd=?,unpriced_reason=?,pricing_revision=? WHERE id=?",
            (
                merged.model,
                merged.billed_seconds,
                merged.input_tokens,
                merged.output_tokens,
                merged.cost_usd,
                merged.unpriced_reason,
                merged.pricing_revision,
                target,
            ),
        )
        db.commit()


def _merged(prior: Any, usage: MeasuredUsage) -> MeasuredUsage:
    """Sum two responses' usage; any part that could not be priced leaves the sum unpriced."""

    def add_int(left: int | None, right: int | None) -> int | None:
        return right if left is None else left if right is None else left + right

    def add_text(left: str | None, right: str | None) -> str | None:
        if left is None or right is None:
            return right if left is None else left
        return format(Decimal(left) + Decimal(right), "f")

    priced = prior["cost_usd"] is not None and usage.cost_usd is not None
    return replace(
        usage,
        billed_seconds=add_text(prior["billed_seconds"], usage.billed_seconds),
        input_tokens=add_int(prior["input_tokens"], usage.input_tokens),
        output_tokens=add_int(prior["output_tokens"], usage.output_tokens),
        cost_usd=add_text(prior["cost_usd"], usage.cost_usd) if priced else None,
        unpriced_reason=None if priced else usage.unpriced_reason or prior["unpriced_reason"],
    )
