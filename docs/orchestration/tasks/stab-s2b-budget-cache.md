# STAB-S2B — Durable per-call budgets, actual usage and successful-cache reuse

Read [the common execution contract](stab-execution-contract.md) before edits; it fixes the required High Agency, mailbox, protected-file and verification workflow.

## Scope
GLM implements cost/cache behavior in workflow analysis/protocol/commands and the existing transcription request boundary, Swift cost readers/UI and tests. S1 and S2A must be accepted first. Sol owns docs and independent validation. Retain existing pricing snapshot values; this is accounting correctness, not a pricing update. Read approved stabilization plan and this brief.

Impact: propagating | files<=18 | modules<=5 | boundary=shared-api

## Decisions
1. Reserve a conservative upper bound immediately before every actual paid request, including each transcription chunk, repair/arbiter/retext/verification call, Luna request and retry. Cancellation check runs before reservation/request. A request whose bound exceeds remaining approved budget must not be sent. SDK-internal automatic retries must not bypass accounting; explicitly disable or route them through the same boundary.
2. Durable ledger keys include input/config/pricing/authorization identity and unique request/attempt identity. Reopen with existing actual charges and outstanding reservations; retries/resumes never reset spending. Persist reservation before request, reconcile it once with actual usage, and avoid double counting an already reconciled request. Crash/unknown outcome conservatively retains the reservation until explicitly reconciled; do not invent zero cost.
3. Record each model's real metering independently: whisper audio seconds at whisper rate, GPT transcription/arbiter model at its own snapshot rate, Luna actual input/output tokens. Cache hits cause no new paid usage. Preserve charges/evidence even when a later call fails or cancellation occurs. Failed calls with no usage leave bounded uncertainty, visibly distinguished from measured actual cost.
4. Estimate includes prompt/transcript/text tokens as well as image and output tokens, all configured transcription passes and retry policy. Use a deterministic conservative bound on serialized request text without adding a tokenizer dependency. Enforce response limits and request dimensions used by the estimate; no claim of an upper bound if an unbounded request remains.
5. Only a successfully normalized and identity-matching analysis/transcription result is reusable. Failed/invalid/partial raw cache entries must be retried after explicit user retry, not replayed forever as success. Keep successful windows/chunks when retrying failed ones, and do not charge cached data again. Settings, media/hash, prompt/model, pricing and input revision mismatches invalidate reuse conservatively.
6. Retry failed analysis from the actual app/engine operation without requiring a new input job, preserving successful results and cumulative budget. Ambiguous legacy entries require explicit action. App displays actual measured cost, reserved/uncertain cost and remaining approved bound separately where relevant; no misleading 0-cost success.
7. Update Python/Swift protocol and digest/version contracts coherently, preserving older reads. Ledger corruption, negative values and nonfinite values fail closed before calls. A reservation/usage write failure prevents the next paid request.

## Acceptance
- Fake SDK at actual request boundary verifies model-specific usage for whisper/GPT/Luna and per-chunk/per-retry reservations; no network allowed.
- Insufficient budget blocks the first unaffordable request, including retry; exact-boundary requests work. Resume restores prior charges and pending reservations and prevents duplicate reconciliation.
- Simulated crash after reservation/before response and cancellation after one successful call preserve durable accounting. Missing usage remains uncertainty, not zero.
- Valid keep cache is reused; API error/malformed cache is retried explicitly; successful windows survive a partial retry; changed settings/media/prompt invalidate cache. Existing legacy fixtures remain untouched.
- Real entrypoint retry and Swift result decoding exercise the app-engine boundary, not only ledger helpers.
- Run affected Python/Swift tests, Ruff/mypy and `make check`, then commit explicit paths and report SHA/commands/exits.

## Boundaries
Product/tests only. Never read/change `.env.example`, `.env`, secrets, real API fixtures, original media, or make live API/FCP calls. No pushes/deployments. Preserve approval defaults, frame timing, thresholds and original subtitle contracts. Send exactly one worker_done when scoped work is complete.

## Status after the S2B-1 foundation — 2026-09-28

- S2B-1's durable ledger primitive was accepted offline in commit `99e0202a6e7cab38ee16a081ebbf9dc30fa23ed6`; exact two-path hashes, baseline/v1 corrections, exact commands, results and limits are in [the Sol review](../reviews/stab-s2b1-durable-ledger-sol.md). This is a foundation only: existing production requests do not yet reserve or reconcile against it.
- Final S2B-1 evidence was 46 targeted ledger cases, 22 affected workflow protocol/analysis cases, mypy on one file, Ruff check/format on two files, and 21 invariants. The initial missing-module collection RED was expected and distinct from v1's two test failures/four mypy errors. No full `make check`, Swift or native gates ran in that isolated unit; integrated S2B owns the aggregate gates.
- **Planned next boundary (not yet a dispatched implementation brief):** refine S2B-2a around generic per-actual-request observer hooks in `transcribe.py`, carrying primitive identity/model/chunk/response data so legacy code never imports project code. Give Sol a small functional source checkpoint before the extended matrix. Then limit S2B-2b to the project ledger adapter and conservative estimates against the accepted API. Cache/retry and Python/Swift cost UI remain S2B-3. Root owns final file scope and acceptance for the fresh bounded GLM source-only Task.
- Use `/tmp/stab-s2b-next-integration-notes.md` as an integration-design pointer when refining those units; it is not itself an implementation brief. Preserve the estimate's actual request text/dimension/output bounds, endpoint/schema/options identity, measured-usage reconciliation and successful-normalization-before-cache-publication requirements.

## Status after S2B-2b — 2026-09-28

- S2B-2a's generic SDK request observer was accepted at `db32c8503db2d0581a87607a3f7ab4f3a0aa10cc`.
- S2B-2b's project accounting integration was accepted offline at `e76df91f951c0b28e7fe60c29d35929f0a56f06b`. Actual paid analysis paths now reserve before each SDK call and reconcile measured usage into a durable ledger; endpoint and workspace identity guard authorization. The Sol review is [here](../reviews/stab-s2b2b-accounting-integration-sol.md).
- Final clean-environment `make check` exited 0: 2,978 Python tests passed, Ruff check/format and mypy passed, and all 21 invariants passed. The successful evidence uses fake SDK methods at the actual call boundary, not a live provider, real media, or Final Cut Pro.
- The transcription quote is an approved finite work allowance. It does not guarantee that every configured repair, retry and verification pass will complete before the budget is exhausted. Exhaustion is recorded and blocks later paid calls.
- S2B-3 remains: only normalized successful cache entries may be reused; failed/malformed cached results need an explicit retry operation that preserves prior successes and cumulative spending; the app must distinguish actual charges, uncertain reservations and remaining approval. The internal unpriced-evidence sidecar and ledger snapshot are not yet exposed by Swift. Keep model prices and existing protocol fields stable unless a fresh reviewed S2B-3 brief explicitly expands them.
