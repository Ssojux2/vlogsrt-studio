# STAB-S1B-R1 — Enforce outcome binding at the UI boundary

Initial implementation `cd80d61` is not accepted. Sol found a concrete consumer bypass after its final mailbox check: `readAnalysisResult()` rejects stale/running/cancelled records as nil, but `shouldLoadCandidates(.legacyUnknown, true)` returns true, and the extension can fall through to a new engine request. Fix this bounded Swift boundary; do not redo Python implementation or discovery.

Impact: propagating | files<=6 | modules<=3 | boundary=shared-api

## Ownership and constraints
- Read `stab-execution-contract.md`, this brief and High Agency directly before edits; acknowledge the actual read and immutable estimate.
- GLM alone writes product/tests. Allowed files: `WorkflowProtocol.swift`, `RoundTripJob.swift`, `WorkflowViewController.swift`, app `main.swift`, `WorkflowProtocolTests.swift`, `RoundTripJobTests.swift`. Ask Sol before expanding beyond Swift.
- Same proven GLM terminal/model and current checkout, one writer. No stash/reset or historical source swaps. No protected environment/credential files, live APIs/FCP, real fixtures/media, push/deployment, docs edits or extra agents.
- Read every mailbox body in full. Process each Delivery, then `orca orchestration check --terminal <your live handle> --ack <the exact deliveryId> --json`; continue through any new batch. A heartbeat is not acknowledgment.

## Required decisions
1. Candidate review is allowed only for a validated, current `success` or `partial_error` result. Unknown/stale/mismatched/running/cancelled history cannot load candidates, leave previously loaded candidates actionable, or enable roughcut. Clear candidate UI and disable roughcut on those states and complete error.
2. Distinguish a fresh job with no attempt/result/candidate history from ambiguous existing history. Fresh jobs retain inspect/authorization/analyze routing; ambiguous history shows an honest unknown summary and stops. Do not silently rerun old records. Keep legacy JSON readers intact.
3. Result binding requires a readable current estimate, matching input/estimate revisions, and the actual latest attempt with matching identifiers. Missing/malformed estimate or attempt records cannot fall back to an older valid record. `success`/`partial_error` requires latest attempt `succeeded`; `error` requires `failed`. Running/pending/cancelled or inconsistent status blocks review/retry based on that result.
4. Preserve the explicit same-job retry for a valid failed result, plus cancellation/drift/authorization gates. Do not conflate unknown with retryable failure.
5. Main app completed-analyze text uses the aggregate summary for success, partial and unknown, not merely a generic completion label with a partial-only override.

## Behavior evidence and verification
- First write meaningful failing regressions using the current reader and the actual shared routing helper together: stale candidate file plus stale/latest-running/latest-cancelled result; missing or malformed estimate/attempt; failed attempt plus success result; fresh no-history route; valid partial candidate route; explicit failed retry and unknown no-rerun.
- These must fail on `cd80d61` for the consumer behavior, not imports or fixture setup. After RED, use `orca orchestration ask` to tell Sol the exact test filter/log and wait briefly for independent reproduction before production edits. Sol will answer through the same question ID.
- Implement, run targeted Swift tests, then final `cd macos && swift test` with direct exit capture. Sol is already running `make check` independently on unchanged Python at `cd80d61`; no duplicate broad Python run is needed if Python/scripts remain byte-identical. If code outside Swift must change, ask first.
- Compare actual scope to estimate, commit explicit owned paths, report SHA/commands/exits and any unresolved issues, read/process/ack final mailbox, then send one worker_done and idle. Sol owns independent acceptance and cleanup.
