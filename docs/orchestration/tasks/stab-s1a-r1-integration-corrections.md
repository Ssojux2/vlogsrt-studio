# STAB-S1A-R1 — Required integration corrections before S1A acceptance

Status: prepared against the in-progress S1A draft; dispatch only after current worker settlement or explicit coordinator recovery. Do not duplicate the active writer. Read the main S1A brief and this correction brief; current checkout and protected-file boundaries still apply.

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
