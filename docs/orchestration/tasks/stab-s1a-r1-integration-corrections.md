# STAB-S1A-R1 — Required integration corrections before S1A acceptance

Status: **completed and independently accepted at `975fb9c`**. This brief records the correction required after `616d233`; all three findings below were resolved and verified. See `../reviews/stab-s1a-sol.md` for final evidence. Current checkout and protected-file boundaries still apply to follow-up work.

## Remaining acceptance findings at 616d233
1. `runningInputRevision` prefers App Group even when the external workspace is the running engine; choose the actual running path and test both differing revisions.
2. Pre-receive cancellation still uses all-zero revision that Python ignores; add a safe job/input-bound pending cancellation path with zero subsequent model calls, without weakening wrong-revision/attempt filtering.
3. No final `_check_cancellation` after last source/Luna call permits succeeded/review_required despite a cancellation during that final call; test no-window and last-window cases and preserve recorded evidence.

Task `task_9c1fc63e6acf` uses the same GLM terminal after settlement; no duplicate writer. The detailed sections below preserve the rationale, but already-completed inner gates and wrapper behavior need regression preservation rather than reimplementation.

## First actions and ownership
1. Read the live lifecycle preamble, consume all same-Dispatch follow-ups, and acknowledge exact current scope before edits.
2. Read `/Users/ssojux2/.codex/plugins/cache/high-agency/high-agency/0.9.0/skills/high-agency-coding/SKILL.md` directly. State the compact Impact estimate and bounded regression to run. Record actual timing; do not claim this happened before earlier edits.
3. GLM alone changes product/tests. Never inspect `.env` or `.env.example`, including filtered grep, metadata-driven debugging or configuration searches. Use `Config` defaults and explicit synthetic settings. Do not output any previous env search result. No new agents, model swaps, paid APIs, real fixtures/media, push or deployment.

## Concrete remaining gaps in the inspected draft
### A. Actual request gates
`_BudgetedTranscriber.transcribe` checks once per job, but `LiveTranscriber.transcribe` sends a head chunk then parallel tail chunks. `_retry` and rejected-parameter fallback can send additional requests inside that one job. Implement a backward-compatible callback at each actual request boundary, propagated from the approved workflow. Ensure SDK automatic retries cannot bypass that boundary. Preserve unrelated legacy behavior where no callback is supplied. Cancellation must escape all broad exception-to-status adapters and reach a cancelled attempt/journal.

Regression: a fake SDK records calls for a multi-chunk job; first request records a matching root cancellation, then no tail request is sent. A retryable first failure records cancellation; neither retry nor fallback issues another SDK call. Assert disabled/controlled SDK retries and unchanged behavior without a gate. Also cover a cancellation raised during the last source call: no successful review-ready finish.

### B. Real app cancellation revision
`main.swift.cancelCurrentJob` resolves only App Group `workspace/revision.jsonl`, while development mode executes in external `Jobs/<id>/workspace`. Fix the actual caller to use the running engine location, and test this route through the shared app/core service. A standalone `engineObservationPaths` helper unused by the app does not satisfy integration.

Regression: external workspace alone has a valid revision; app-generated cancellation carries it identically to both roots and the Python workflow observes it. Cancellation before receive/revision creation must stop this job before model calls without treating an unrelated revision/attempt as cancelled. Bind any pending cancellation mechanism to the correct job/input; do not globally interpret arbitrary zero revisions as wildcard cancellation.

### C. Installed development entrypoint
The development shell wrapper duplicates the Python flow. Validate the installed wrapper with actual offline `receive`/`inspect` and staged media grant, not only a fake Python command logger. Preserve optional-file and existing-workspace behavior, common root observation, and carrier/tool environment contracts. Sharing the Python entrypoint is acceptable but preserve installer tests coherently.

## Verification and reporting
- Run failing regressions before fixes where the remaining draft permits a clear failure, then targeted Python tests, Swift suite, Ruff check/format, mypy, `make check` and script checks after edits.
- Capture real command exits; `pytest | tail` without pipefail masks failures and is not an exit-code receipt. Redirect output to a log, retain command exit, then tail the log.
- At natural checkpoints and immediately before worker_done, consume follow-ups. Report actual skill-read evidence, corrected coverage, files, explicit-path commit SHA, commands/exits and remaining external acceptance.
- Do not claim S1A accepted; Sol independently validates. If an error remains, provide reproduction, expected/actual, sanitized error/exit, attempts/results, root-cause confidence and impact.
