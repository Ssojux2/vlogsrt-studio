# STAB-S2B-2b — Connect durable accounting to approved analysis

## Ownership and outcome

Read AGENTS.md, CLAUDE.md, `stab-execution-contract.md`, `stab-s2b-budget-cache.md`, and the installed High Agency skill before mutation. Use Claude-GLM `glm-5.3[1m]` high, source only. **Sol executes all runtime tests/probes**; GLM may read/edit source, run static checks and commit explicit owned paths only after acceptance. No additional agents or ROOT model/compact control.

Impact: high | files<=8 | modules<=8 | boundary=shared-api

Owned paths:
- `vlogsrt/project/request_accounting.py` (new)
- `vlogsrt/project/commands.py`
- `vlogsrt/project/workflow_analysis.py`
- `tests/project/test_request_accounting.py` (new)
- `tests/project/test_workflow_analysis.py`
- `tests/project/test_workflow_cancellation.py`
- `tests/project/test_analysis_outcomes.py`
- `tests/project/test_review_workflow.py`

Ask before expanding paths. Do not edit accepted `budget_ledger.py`/`transcribe.py`, wire protocol, pricing snapshot values/schema, Swift, engine installer or test guards. No dotenv/credential/wrapper/original-media/real-fixture reads or changes; no live providers, FCP, push/deploy, dependencies or unrelated editing thresholds. Sol owns docs/checkpoint.

The observable result is **real approved-analysis command paths using per-actual-request durable reservation and model-specific accounting**, with truthful conservative quotes. This is not another unused primitive. Successful-cache/retry-operation and Swift cost UI remain S2B-3; do not claim those completed.

## Inspected contracts and locations

- Ledger foundation `99e0202`: `BudgetLedger.open/reserve/reconcile/mark_unknown/snapshot`, `LedgerIdentity(input_revision,policy_digest,pricing_revision,authorization_id)`. Existing durable parent required; duplicate request IDs never grant another send; pending/unknown reserves consume budget; reconciliation idempotent; storage faults poison the object. Numeric domain is value-based 30 significant digits, exponent floor -42, adjusted ceiling 24, exact trapped context80.
- Request boundary `db32c85`: `transcribe.RequestObserver.authorize(meta)/on_response(ticket,meta,raw)/on_error(ticket,meta,error)`. RequestAttemptMeta.attempt_id is a per-SDK-request UUID, separate from project AnalysisAttempt. Metadata carries operation/model/kind/source/chunk coordinates and defensive effective options. ObserverFailure wraps callback faults with causal exceptions. Cancellation checks surround admission/send; SDK internal retries are disabled.
- `commands._current_analysis_estimate` currently binds config/source hashes but not resolved endpoint values. `_project_analyze` validates price/estimate/authorization, appends execution attempts and calls `_produce_analysis_outcome`. Existing authorized/analyzing resume is in scope; explicit failed-state retry operation belongs to S2B-3.
- `_produce_analysis_outcome` currently creates an in-memory UsageLedger and `_BudgetedTranscriber`, then bills its aggregate duration as Whisper. `_decide_window` separately appends Luna usage after response/cache handling. Replace production paid accounting with the shared durable observer; no double recording.
- `commands._staged_vision_credentials`: V2 role marker selects separate vision credentials/endpoint; unmarked direct CLI retains shared legacy STT key/base URL. Quote endpoint identity must follow this exact routing without requiring keys just to estimate. Do not change the role compatibility contract.
- `DevelopmentJobStager.stage` in `DevelopmentEngineLauncher.swift` retains existing job directories and copies stable inputs; engine workspace mkdir retains existing contents. Keep ledger in persistent job workspace. Current usage JSONL already syncs on failures; new uncertainty UI/transport is later scope.
- Installed public SDK source (read only) has audio UsageDuration(type=duration,seconds), UsageTokens(type=tokens,input_tokens,output_tokens,total_tokens), and verbose transcription duration/optional usage. PricingSnapshot currently supplies per-minute STT rates and Luna per-token rates. Do not invent token-to-duration conversion or new prices.

## Accounting requirements

1. Implement a project-owned observer/context over the accepted ledger and wire it into the **actual online** get_transcriber/LiveTranscriber and request_luna_vision paths. Cover primary/rescue/arbiter/retext/verification, every chunk/retry/option-reduced attempt and every actual Luna request. Fixture replay, cache hits and fake whole-Transcriber returns are not paid SDK calls and must not create paid charges.
2. Bind one stable workspace ledger to input revision, effective config/analysis-policy/endpoint identities, pricing and the complete authorization identity. Per-call ticket binds the SDK request UUID to the project execution attempt, model, optional window and request digest. Hash identities; persist no keys, raw credential-bearing URLs, prompts, frames or arbitrary provider error bodies.
3. Reserve the conservative request bound durably before authorization returns. Reconcile once with valid metering priced at the matching snapshot model; retain full reservation for missing, invalid, unsupported or unknown usage. Reopen restores all measured charges and outstanding reserves across execution attempts. No new file name/authorization timestamp trick may silently reset an existing job's spending; identity mismatch fails closed.
4. Treat legacy work conservatively: a missing ledger alongside earlier usage or potentially paid execution history is not evidence of zero spend. Refuse implicit migration/replay and request explicit action through a clear error. Do not mistake the newly appended current attempt for historical paid work; initialize at an appropriate boundary or distinguish its ID.
5. Parse mapping and actual SDK-object metering without depending on successful transcript/decision normalization. Audio duration usage (or valid verbose response duration) uses its own Whisper/GPT snapshot rate; Luna uses actual input/output tokens. Preserve useful reported quantities for unsupported meter types as explicitly unpriced/uncertain evidence if needed, using only whitelisted primitive fields. A token-based audio response cannot be priced with invented token rates or converted into fabricated audio seconds. Missing usage is never zero-cost success.
6. Persist charges/evidence even if later calls, normalization, cache publication or cancellation fail. Actual usage exceeding a reservation/approval is recorded honestly and closes further paid admission. Callback/ledger/projection failures must close the analysis-wide paid gate, not just one LiveTranscriber invocation; the Luna per-window catch must not hide a fatal accounting failure and continue paid requests.
7. Make `analysis-usage.jsonl` a recoverable, idempotent projection of reconciled ledger records, preserving earlier execution attempts. Prevent duplicate or lost rows under parallel chunks. Projection failure blocks further paid requests; reopening rebuilds the projection from authoritative ledger state before spending. Never retain the old aggregate-Whisper charge or the post-response Luna charge alongside observer charges. Document internal uncertainty artifacts not yet exposed by the UI.

## Quote and request-bound requirements

- Retain numeric prices in PricingSnapshot. Validate supported model/rate mappings and finite/nonnegative inputs; unsupported pricing or request shapes stop before paid calls.
- Include serialized prompt/transcript/text input, images, bounded output, configured transcription/repair/arbiter/retext/verification passes, SDK retry count and optional-argument fallback attempts. Inspect actual source and boundary-retranscription work; do not assume the old fixed multiplier proves the bound.
- Define explicit finite request limits needed for a defensible quote (text bytes, image count/detail/dimensions, output cap, audio quantities/attempt policy). Store new configurable policy values in AnalysisSettings and bind them to settings identity; do not change subtitle text/timing, scene thresholds, frame rates or original media. Enforce the same limits at actual request authorization. Oversized input must fail safely rather than be charged with an underestimated bound or silently drop content.
- A deterministic UTF-8/serialized-text bound is acceptable without a tokenizer dependency; document treatment of image data and structural overhead. Do not charge base64 image data as if it were ordinary transcript text without explaining the bound. If the current pipeline cannot support a proven finite bound, define an enforced quote-bound work envelope and report exhaustion honestly; do not claim unsupported unlimited completion.
- Endpoint changes must invalidate the quote/ledger policy binding. Quote inspection remains key-free; execution captures the same effective endpoint routing used by the SDK factories. Do not rely solely on environment variable names or allow SDK environment inheritance to silently select a different approved endpoint. Preserve V2 role separation and direct CLI compatibility.
- Keep existing protocol fields and digests valid; use current settings/estimate identities rather than silently changing the Python/Swift wire contract. S2B-3 will extend user-facing cost/uncertainty transport coherently.

## Verification checkpoints

1. After bounded source inspection, report the chosen metering rules, quote-bound derivation, durable identity/legacy handling and actual wiring points (concise, with paths). Resolve a concrete ambiguity with a blocking ask; do not perform broad discovery indefinitely. Supply a minimal regression source proving the current command path lacks per-request reservation; Sol runs the baseline RED.
2. Implement the first usable command/observer/ledger slice and ask Sol for early targeted verification before the full matrix. A fake SDK must sit at the actual SDK call boundary; mocking request_luna_vision or an entire Transcriber does not prove accounting. Existing tests that assert charges from such stubs must be migrated to faithful synthetic SDK boundaries, not weakened.
3. Complete offline cases: Whisper/GPT/Luna rates; retries and chunks; exact/insufficient budgets; missing/invalid/unpriced usage; crash-equivalent reopen after reservation; successful call followed by failure/cancellation; repeated reconciliation; legacy ambiguity; identity/endpoint/config change; ledger/projection I/O failure; concurrent calls; prompt and request-dimension bounds. Exercise the real analyze command entrypoint in-process with synthetic preparation/provider doubles. Do not add subprocess harnesses or read real fixtures/media.
4. Stop edits at final source-ready ask. Sol reviews harness and scoped diff, executes target/affected checks, then one aggregate make check. Return original exits and fixed causes; no assertion weakening or skips for green. GLM performs no runtime execution.

After explicit Sol acceptance, commit only owned paths, report full SHA/skill/preflight/scope/errors/limits, consume and ACK full mailbox bodies, send one worker_done with correct CSV paths and both live IDs, then idle for cleanup. Overall S2B and the project remain incomplete until later integration and external acceptance.
