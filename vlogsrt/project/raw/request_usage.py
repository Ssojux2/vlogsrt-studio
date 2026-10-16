"""Measured usage and computed cost of paid attempts in the browser workflow (W-7, D46).

`UsageObserver` is the `RequestObserver` the live senders carry. It only writes receipts:
it never changes a request, a retry, an approval gate or a cache. Cost comes from the
2026-09-21 pricing snapshot (Claude: the registered 2026-10-09 profile, D54) — it is a
computation, not the provider's invoice. An attempt
whose response never arrived stays uncertain and is not priced (it may have been billed).
A recorder failure is not swallowed: `transcribe.py` raises it as `ObserverFailure`.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from vlogsrt.project.pricing import official_pricing_snapshot
from vlogsrt.project.raw import request_log
from vlogsrt.project.raw.claude_cost import measure_claude_usage
from vlogsrt.project.request_accounting import MeasuredUsage, measure_response_usage
from vlogsrt.project.workflow_protocol import PricingSnapshot
from vlogsrt.task_runtime import provider_error
from vlogsrt.transcribe import MESSAGES_OPERATION, RESPONSES_OPERATION

_SEMANTIC_OPERATIONS = frozenset({RESPONSES_OPERATION, MESSAGES_OPERATION})
"""의미 분석 단계의 연산 — Luna(Responses)와 Claude(Messages, D54)가 같은 `luna` 단계다."""

_CHUNK = 500
"""Bound for one SQL `IN (...)` list — SQLite caps bound variables."""


@functools.cache
def _official_pricing() -> PricingSnapshot:
    return official_pricing_snapshot()


@dataclass(frozen=True, slots=True)
class UsageObserver:
    """Writes one usage record per live response, onto the open attempt or its own row."""

    pricing: PricingSnapshot | None = None

    def authorize(self, meta: Any) -> str | None:
        """Open a receipt only when no caller did — the caption path never calls `begin`."""
        if not request_log.recording() or request_log.open_attempt() is not None:
            return None
        stage = "luna" if meta.operation in _SEMANTIC_OPERATIONS else (meta.kind or "asr")
        return request_log.begin(stage, meta.attempt_id, bind=False)

    def on_response(self, ticket: str | None, meta: Any, response: object) -> None:
        if not request_log.recording():
            return
        request_log.record_usage(self._measure(meta, response), ticket)
        request_log.finish(ticket, received=True)

    def _measure(self, meta: Any, response: object) -> MeasuredUsage:
        """Claude는 등록된 프로파일의 공식 단가로, 나머지는 OpenAI 가격 스냅샷으로 잰다."""
        if meta.operation == MESSAGES_OPERATION:
            return measure_claude_usage(meta, response)
        pricing = self.pricing if self.pricing is not None else _official_pricing()
        return measure_response_usage(meta, response, pricing)

    def on_error(self, ticket: str | None, meta: Any, error: Exception) -> None:
        """Finish only receipts this observer opened; `begin` callers finish their own."""
        category, _message, uncertain = provider_error(error)
        request_log.finish(ticket, received=not uncertain, note=category)


def observer() -> UsageObserver:
    return UsageObserver()


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """Totals over a set of attempts. `unpriced` counts received attempts without a cost."""

    attempts: int = 0
    received: int = 0
    uncertain: int = 0
    billed_seconds: Decimal = Decimal(0)
    input_tokens: int = 0
    output_tokens: int = 0
    luna_input_tokens: int = 0
    luna_output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    unpriced: int = 0
    ceiling_usd: Decimal | None = None
    """Approved USD ceiling. The browser approvals bound request counts, so this is
    None unless a caller supplies one."""


def summarize(workspace: Any, job_ids: Sequence[str] | None = None) -> UsageSummary:
    """Totals for `job_ids` (all jobs when None). A workspace without receipts is empty."""
    if job_ids is not None and not job_ids:
        return UsageSummary()
    with workspace.connect() as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_requests'"
        ).fetchone()
        if exists is None:
            return UsageSummary()
        request_log.ensure_schema(db)
        rows = _fetch(db, job_ids)
    return _reduce(rows)


def _fetch(db: Any, job_ids: Sequence[str] | None) -> list[Any]:
    select = "SELECT stage,status,billed_seconds,input_tokens,output_tokens,cost_usd FROM "
    if job_ids is None:
        return list(db.execute(select + "analysis_requests").fetchall())
    rows: list[Any] = []
    for start in range(0, len(job_ids), _CHUNK):
        part = tuple(job_ids[start : start + _CHUNK])
        marks = ",".join("?" * len(part))
        rows += db.execute(select + f"analysis_requests WHERE job_id IN ({marks})", part).fetchall()
    return rows


def _reduce(rows: Sequence[Any]) -> UsageSummary:
    seconds, cost = Decimal(0), Decimal(0)
    tokens = [0, 0, 0, 0]
    received = unpriced = 0
    for row in rows:
        if row["billed_seconds"] is not None:
            seconds += Decimal(row["billed_seconds"])
        if row["cost_usd"] is not None:
            cost += Decimal(row["cost_usd"])
        for slot, name in ((0, "input_tokens"), (1, "output_tokens")):
            tokens[slot] += row[name] or 0
            if row["stage"] == "luna":
                tokens[slot + 2] += row[name] or 0
        if row["status"] == "response_received":
            received += 1
            unpriced += row["cost_usd"] is None
    return UsageSummary(
        attempts=len(rows),
        received=received,
        uncertain=len(rows) - received,
        billed_seconds=seconds,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        luna_input_tokens=tokens[2],
        luna_output_tokens=tokens[3],
        cost_usd=cost,
        unpriced=unpriced,
    )


def _usd(value: Decimal, places: str) -> str:
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")


def format_usage_line(summary: UsageSummary) -> str:
    """One compact status line; empty when no paid attempt was recorded."""
    if summary.attempts == 0:
        return ""
    parts = [f"실사용 ${_usd(summary.cost_usd, '0.0001')}"]
    if summary.billed_seconds > 0:
        parts.append(f"전사 {_usd(summary.billed_seconds, '1')}초")
    if summary.luna_input_tokens or summary.luna_output_tokens:
        parts.append(
            f"Luna 입력 {summary.luna_input_tokens:,}/출력 {summary.luna_output_tokens:,} 토큰"
        )
    if summary.ceiling_usd is not None:
        parts.append(f"승인 상한 ${format(summary.ceiling_usd, 'f')}")
    return " · ".join(parts) + f" (불확실 {summary.uncertain}건·가격 미정 {summary.unpriced}건)"
