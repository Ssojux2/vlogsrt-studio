# STAB-S2B-1 — Durable request reservation foundation

## Ownership and routing

Read `stab-execution-contract.md`, `stab-s2b-budget-cache.md`, the approved stabilization plan, AGENTS.md, CLAUDE.md and the installed High Agency skill. This is the first bounded unit of S2B. Claude-GLM (`glm-5.3[1m]`, high) writes product/test source only; Sol runs **all** runtime tests/probes and accepts the result. Do not import/evaluate project code, run tests or SDK/app probes. Static reads, edits, formatting and explicit-path git are allowed. No further delegation.

Impact: propagating | files<=2 | modules<=1 | boundary=shared-api

Owned paths (new files only):
- `vlogsrt/project/budget_ledger.py`
- `tests/project/test_budget_ledger.py`

No other source or docs changes without a blocking ask. The parent checkpoint stays uncommitted. Protect dotenv, credentials, original media and real fixtures. No live providers, FCP, push or deploy.

## Staged S2B sequence

1. **This Task:** independently verified durable ledger primitive and contract.
2. S2B-2: generic request observer in `transcribe.py`, real per-chunk/retry accounting, conservative bounded estimates, workflow integration. Legacy modules must never import project modules.
3. S2B-3: successful cache identity/retry operation, cumulative state, Python/Swift cost protocol and UI, final cross-boundary acceptance.

Units 2/3 will be refined against the accepted API. This foundation alone does not enforce budgets on current production calls and must not be reported as complete S2B.

## Inspected baseline

`workflow_analysis.UsageLedger` is in-memory and only checks after usage; `commands._produce_analysis_outcome` resets it on each run. `commands._BudgetedTranscriber` aggregates job duration across models and does not see retries. `LiveTranscriber._retry` already disables SDK retries and gates cancellation, but tail chunks use threads. Existing `ActualUsage` has an input revision, attempt ID, optional window, model, tokens, audio seconds, cost and digest; `AnalysisAuthorization` binds input/estimate/pricing/media-grant and timestamp.

## Concrete contract

- Provide typed, documented ledger identity, reservation, reconciliation and snapshot APIs for the next unit. Identity binds input revision, effective config/policy/endpoint digest, pricing revision and authorization identity; never persist secrets or raw endpoint credentials. Accept caller-computed opaque SHA-256 identities. Approval limit is finite, nonnegative Decimal and bound to the persisted identity. Reopening with a different identity/limit rejects; never truncate/reinitialize a ledger implicitly.
- Every actual request gets a distinct caller-provided request ID and execution attempt ID, request digest, model and conservative bound. Reserve atomically **before** returning permission to send. Exact budget fits succeed; insufficient funds reject without creating a record. Reusing a request ID must never grant another send, even if identical; conflicting identity fails closed.
- Snapshot exposes measured actual, unresolved reserved bound, consumed bound, remaining bound and records. Initial reservations and explicit unknown outcomes both consume the full bound. Reopen restores them. Missing usage/errors/cancellation do not imply zero, expiration or refunds.
- Reconcile a reservation once using validated `ActualUsage` and a matching input/attempt/model plus stable usage digest. Exact duplicate reconciliation is idempotent; conflicting reconciliation rejects. Unknown request rejects. Preserve actual charge even if it unexpectedly exceeds its bound/approval, block further positive requests and expose nonnegative remaining bound. Do not discard overrun evidence or reset spending.
- Validate strict shapes/types, IDs, finite/nonnegative amounts and usage fields/digest on disk and API entry. Reject malformed/truncated/unknown-version/conflicting/corrupt ledgers before any further reservation. Do not trust cached totals; recompute from validated records. Legacy/missing-identity data must fail closed, never migrate to an empty balance silently.
- Use standard-library storage with atomic durable commits. A write/flush/fsync/replace failure must not grant permission. Handle failure after replacement conservatively (the caller must not be able to send using uncommitted/uncertain memory state). Protect concurrent reservations/reconciliations across separate ledger instances and threads; persisted updates must not be lost or overspend. Document the local-filesystem/lock assumptions and use a stable lock target across atomic replacement. A failed persistence path blocks that object until reopen/validation. No orphan temporary file may be interpreted as approval.
- Keep the API separate from existing wire protocol, pricing and live calls in this unit. Reuse existing protocol validation where sound, but account for direct dataclass construction and booleans masquerading as integers. No new dependencies or price changes.

## Source-ready checkpoints and independent proof

First supply a focused regression test source for the missing durable-reservation behavior, report it to Sol and wait with a blocking ask. Sol will inspect the harness and run the baseline RED (a missing module is acceptable for this genuinely new API, explicitly label collection RED). After Sol replies, implement and complete the test matrix; do not execute it yourself.

Tests use only tmp_path synthetic JSON/identities and existing protected-file/network guards. No subprocess/native/SDK probes in this unit. Cover: exact/insufficient budgets; cumulative reopen; request replay prevention; idempotent/conflicting reconciliation; unknown outcomes; crash-equivalent reopen after reservation; wrong identity/limit/model/attempt; corrupted storage and invalid usage; actual overrun; injected persistence failures before/after replace; competing ledger instances/threads without lost updates. Make assertion failures meaningful and keep fixtures deterministic.

At a stable source boundary, send a blocking ask for Sol validation and stop editing. Sol runs targeted tests first, affected protocol/analysis tests and static gates; returns sanitized failures for correction. Commit only the two owned files after Sol's acceptance instruction. Report API summary, exact owned paths/SHA, skill path/preflight evidence and remaining limitations. Send exactly one worker_done with live lifecycle IDs, then idle.
