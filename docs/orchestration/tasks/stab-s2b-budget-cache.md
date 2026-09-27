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
