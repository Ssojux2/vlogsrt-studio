# STAB-S2B-2a — Observe each actual request at the shared boundary

## Ownership

Read AGENTS.md, CLAUDE.md, `stab-execution-contract.md`, `stab-s2b-budget-cache.md`, and the installed High Agency skill. Claude-GLM `glm-5.3[1m]` high writes source only; **Sol executes every runtime test/probe** in clean synthetic environments. No project import/evaluation, tests, fixtures, SDK/app/native probes by GLM. Static reads/edits/formatting and explicit-path git are allowed. No additional agents.

Impact: propagating | files<=2 | modules<=2 | boundary=shared-api

Owned paths only:
- `vlogsrt/transcribe.py`
- `tests/test_request_observer.py` (new)

Sol owns briefs/reports/checkpoint. Never read or change dotenv, `.env.example`, credentials/wrappers, original media or real API fixtures. No live providers, FCP, push/deploy, dependency additions or model/price/threshold changes. Preserve the legacy-to-project import boundary and subtitle timing/text contracts.

## Goal and inspected baseline

Expose optional generic observation around each actual transcription SDK attempt and each `request_luna_vision` Responses request. This is the request boundary foundation, **not yet project budget enforcement**. S2B-1 ledger `99e0202` and docs `bb6860e` are accepted. The project adapter, conservative estimates, cache/retry and Swift cost UI remain separate later tasks; do not integrate them here.

`LiveTranscriber._retry` already gates cancellation and disables SDK retries via `max_retries=0`. `_one_chunk` may retry with reduced optional arguments after 400 errors. `transcribe` processes the head chunk then parallel tail chunks. `_create` opens the chunk file and calls `client.audio.transcriptions.create`; `request_luna_vision` calls `client.responses.create`, then converts the result. Existing fixture paths and ordinary callers must retain their behavior when observation is absent.

## Required API and behavior

1. Add a small typed observer protocol and immutable request metadata in `transcribe.py`, optional on `LiveTranscriber`, forwarded by `get_transcriber`, and optional on `request_luna_vision`. Use project-independent primitives/core value types only. Expose before-request authorization/reservation callback returning an opaque ticket, response callback with the same ticket and original SDK response, and request-error callback with the same ticket and SDK exception. Names may follow local style; explain the chosen API in the first checkpoint.
2. Metadata identifies a unique request attempt, actual model/operation, and for audio the job kind/source identity and chunk index/offset/duration. Include a defensive snapshot of effective request options/payload so reduced-option retries are distinguishable and an observer cannot mutate the outbound request. Do not include API keys, authorization headers or persist/log request payloads. A later adapter must be able to bind endpoint/config identity without a project import; document what remains caller-owned. Do not add credential/environment discovery to observation.
3. Call the before hook exactly once immediately before each actual SDK call, after cancellation and local request prerequisites. Every retry and each 400 option-reduced re-request has a fresh request identity/ticket. An absent observer preserves current behavior and does not introduce new file reads or serialization restrictions. Invalid local input/offline gates must not authorize a paid attempt. No observer callbacks for fixture/cache replay or an empty job.
4. A before-hook failure prevents its SDK call. A response hook receives the original successful SDK response **before** conversion/normalization can fail, preserving the adapter's chance to record metering. An SDK failure invokes its error hook before retry classification/backoff; propagate BaseException cancellation correctly. Missing usage is not invented here; pass through evidence for the adapter.
5. Observer failures are fatal infrastructure/accounting failures: they must never be treated as SDK retries or 400 option-repair, even if the thrown exception happens to carry status_code=429/400 or an SDK-like class name. Do not call the SDK-error hook for an observer's own failure. Preserve causal exceptions. Already authorized/in-flight parallel work can finish and report its outcome; stop new authorization for queued attempts in the observed transcription invocation after a fatal observer failure. State this concurrency boundary precisely rather than claiming control of already in-flight requests.
6. Retain `max_retries=0` on actual SDK client constructors. Preserve existing retry counts/backoff, accepted optional-argument fallback, return payload/order/offsets and cancellation with no observer. Do not change legacy `request_vision`/Chat Completions behavior or add new retry policy to Luna.
7. Keep the implementation small. A bounded private wrapper may isolate SDK call exceptions from observation failures. Avoid a general event bus, storage layer or new provider framework. The durable project ledger remains untouched in this unit.

## Early source checkpoints and independent acceptance

**Checkpoint 1:** after enough targeted reads, supply a few synthetic fake-SDK tests for missing hooks and send a blocking ask. Sol inspects source and runs a clearly labelled baseline RED. No broad repository discovery once the missing behavior and boundary are known.

**Checkpoint 2:** implement the smallest working vertical slice (one audio attempt and one Responses attempt with before/success order and before-hook denial), then stop edits and ask Sol to run those tests. Do this before expanding the full matrix. Sol returns failures; GLM alone corrects source.

**Checkpoint 3:** extend tests to actual retry, 400 argument removal, all relevant transcription kinds/model choice, callback-failure isolation, response-before-conversion failure, factory forwarding, no-observer compatibility, cancellation, concurrent tail chunks and no callbacks for offline fixtures. Fake SDKs replace the actual SDK methods/client construction, not only `Transcriber.transcribe`. Use tmp_path-created audio bytes, explicit synthetic settings and existing conftest guards; no subprocess harness, real SDK network, real media/fixtures or sleep delays. Controlled concurrency must avoid deadlocks and assert both no duplicate IDs and correct recorded outcomes.

At the final stable boundary, stop edits and send a blocking ask. Sol will run targeted tests, existing transcribe/cancellation/affected boundary tests, Ruff/mypy/invariants and broader gates if the final shared-boundary impact requires them. No runtime verification by GLM. Do not weaken tests to obtain green.

Commit only the two owned paths after Sol's explicit acceptance. Report SHA, exact paths, skill-read/preflight evidence, all observed failures/corrections and remaining integration limits. Consume and ACK full mailbox deliveries at natural checkpoints and before the single worker_done. Then idle for cleanup; do not compact or change ROOT's model.

## Accepted offline — 2026-09-28

- Code `db32c8503db2d0581a87607a3f7ab4f3a0aa10cc`, exactly the two owned paths; immutable 2-file/2-module/shared-API scope retained. Accepted hashes matched after final gates and commit.
- Sol final target: 31 passed in 0.09s. Final `make check`: 2,962 passed in 66.47s, Ruff check/format 130 files, mypy 56 sources, all 21 invariants; original exit 0. The earlier affected subset passed 194 cases; the final aggregate covers the accepted source.
- The missing-API collection RED, one product callable-arity defect and fake-SDK namespace defect (18 failing cases in the first extended run), cancellation/admission corrections and stronger deterministic test ordering are recorded in the [feedback report](../../reports/2026-09-27-high-agency-implementation-feedback.md). No scoped failure remains.
- Generic hooks are available; the project does not yet pass a durable ledger observer into production analysis. S2B-2b must bind actual endpoint/config/pricing/authorization identity, per-request bounds and metering to the accepted ledger. S2B-3 owns successful cache/retry and cost protocol/UI integration. Python 3.11 compatibility was reviewed from source/docs; runtime validation used 3.12. No real provider/media/FCP or unchanged Swift/native gate was exercised.
- Task `task_2ff24b9963e4` / Dispatch `ctx_b9eed3e720a8` succeeded; exact idle GLM terminal closed, delivery `delivery_90058102f3b8` ACKed. Preserve these settled IDs.
