# STAB-S1B — Explicit analysis outcomes across engine and app

Read [the common execution contract](stab-execution-contract.md) before edits; it fixes the required High Agency, mailbox, protected-file and verification workflow.

## Target and ownership
Implement the failure-reporting half of S1 after S1A is accepted. GLM alone edits product code/tests; Sol owns this brief, contract documentation updates, and independent acceptance. Work in the current checkout, using the approved stabilization plan. Expected files: `vlogsrt/project/workflow_protocol.py`, `workflow_analysis.py`, `commands.py`, engine entrypoint/stager, Swift workflow protocol/job/runtime/extension consumers, and their tests. Adjacent files are allowed only to complete this boundary.

Impact: propagating | files<=18 | modules<=5 | boundary=shared-api

## Required behavior and decisions
1. Add explicit `success`, `error`, and `skipped` status plus a stable sanitized reason code to vision decisions. A valid model `keep` is success. API exceptions, missing/invalid output, invalid identities/evidence, and malformed decisions are errors. Intentionally unprocessed windows are skipped. Do not include raw exception text, secrets, or response body in user-visible reasons; preserve existing hashes.
2. Status is authoritative for safety: only successful, valid decisions can produce a cut candidate. Error/skipped windows preserve all footage. Preserve the existing confidence/guard thresholds and all approval requirements.
3. Persist an aggregate analysis result with input revision, attempt ID, status (`success`, `partial_error`, `error`), counts for successful/failed/skipped windows and upstream audio/transcription/VAD stages, candidate count, and sanitized reasons. Zero windows is success only when upstream analysis actually succeeded; all failed audio/VAD inputs must not become a successful zero-window result. One or more failures with some successful coverage is partial_error; all attempted coverage failing is error. Skipped/unprocessed windows must never be silently counted as successful.
4. All-window failure must not finish as a successful zero-candidate attempt. Persist the result and decisions before returning failure; retain safe candidates for partial results and show the warning when review is available. Failed analysis remains retryable without replacing the input. Keep cancellation a distinct cancelled outcome, never normalize it as vision error.
5. Copy/sync results, attempts, usage and decisions back to the app even when the engine returns a nonzero exit. The app/extension must display failed/partial analysis distinctly from successful no-candidate analysis. Do not require a second successful engine run merely to expose failure evidence.
6. Update Python and Swift JSONL readers/writers coherently. Retain reading old analysis protocol versions. Old records lacking outcome fields are legacy/unknown, not new successful-cache evidence. Do not auto-rerun ambiguous records. Keep digest validation and version identity correct; add a protocol version when payload digests change.
7. Persist per-window progress/results as work completes so an exception at a later window does not erase completed evidence. Full cache retry/budget accounting is S2B; do not expand this task into that redesign.

## Acceptance (offline only)
- Normalization tests distinguish valid keep, valid proposed cut, API exception, malformed/empty response, unknown candidate/frame and skipped state; every error/skipped case yields no cut.
- Command/real-entrypoint integration covers valid zero-window success, all upstream audio/transcription/VAD failure with zero windows, all-window failure, mixed success/failure, cancellation, and nonzero-exit result propagation. Assert persisted aggregate, attempt status, exit and app-visible files, not just helper return values.
- Swift tests decode new and legacy messages and show distinct user-facing summaries for success with zero candidates, partial error, complete error, and unknown legacy state.
- Existing review invariants and exact rational timing remain unchanged. No approved cut can include an error/skipped window.
- Run affected Python tests, `cd macos && swift test`, changed-file Ruff, `python -m mypy`, and `make check` before explicit-path commit; record commands and exits.

## Boundaries and settlement
Do not read/change `.env.example`, `.env`, secret wrappers, real API fixtures, or original media. No paid/live APIs, FCP execution, push or deployment. Synthetic fixtures/fake clients are permitted. Read this brief and the approved plan despite any earlier S1A docs-read prohibition; do not edit docs. Commit only product/tests using explicit paths, report SHA/files/commands/exits and remaining external acceptance, then send exactly one lifecycle worker_done. Sol reviews and dispatches corrections before S1 acceptance.
