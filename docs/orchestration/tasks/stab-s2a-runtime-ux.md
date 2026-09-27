# STAB-S2A — Single execution, preflight, credentials and progress

## Target and ownership
After S1 acceptance, GLM implements runtime/UX in Python engine entrypoint and Swift core, app and extension, plus tests. Sol owns documentation and validation. Scope is S2 runtime UX; budget/cache accounting is S2B and import lifecycle is S2C. Read the approved stabilization plan and this brief. Work serially in the current checkout.

Impact: propagating | files<=18 | modules<=5 | boundary=shared-api

## Decisions and required behavior
1. Acquire one per-job execution lock before staging or mutating state. An app actor/main-thread gate prevents duplicate UI/URL requests, while an OS-backed Python job lock prevents a second process bypassing the app. Prefer automatically released locks to stale sentinel files. Different jobs can run independently; job identity must be canonical so aliases cannot bypass the gate. Duplicate calls yield an explicit already-running state and never duplicate API calls or import.
2. Release gates on every exit, thrown error and cancellation; a crashed engine must not permanently lock the job. Avoid terminating the first legitimate process merely because a duplicate arrives. Do not hold a global lock across all jobs.
3. Add a secure API-key input and replace action backed by existing KeychainStore, with empty-input handling, clear save/error status, and no key in argv, logs, defaults, JSONL or test fixture. Do not read/write a real key in tests; use injected storage operations. Key replacement must reach the next child invocation rather than an inherited stale environment key.
4. Preflight checks the operation's actual prerequisites before starting: valid input/job, executable selected engine, required ffmpeg/ffprobe/tool paths, media grant/scopes and writable workspace. Cost estimation stays available without an API key; only an approved paid execution requires a key. Errors must name the missing prerequisite and corrective action. Share the checks with execution so UI availability is truthful for both development and bundled paths.
5. Extension refreshes job progress/result automatically while an engine request runs. Poll/read durable atomic snapshots on a bounded timer or equivalent; stop on terminal state, replaced job, view teardown or cancellation. Do not issue repeated engine launches as a refresh mechanism. Preserve checkbox choices when refreshing the same candidate revision, reset them on new revision, and display partial/error states from S1.
6. Progress records identify job/input/attempt and current stage plus known completed/total work; do not fabricate percentage for unknown totals. Read old workspaces conservatively; no automatic reexecution of ambiguous legacy jobs.

## Observable acceptance
- Python subprocess-level lock test: two actual entrypoint invocations for one temporary job produce exactly one execution; release permits a later invocation after failure/exit. Separate job is unaffected.
- Swift tests exercise app/core request gate with concurrent duplicate requests and executor failure/cancellation; assert one executor call and gate release. A helper never used by `main.swift` is insufficient.
- Credential tests use fake key storage, verify empty/replacement behavior and environment carrier, and assert absence of secrets in persisted state/argv.
- Preflight matrix covers development/bundled, missing executable/tools/input/grant/key and estimate without key. Automatic refresh test updates a temporary result while the request runs and observes terminal UI model state without reexecution.
- Run affected Python/Swift tests, Ruff/mypy and `make check`; report exact commands/exits and explicit-path commit SHA.

## Boundaries
No `.env.example`/`.env` reads or writes, real keychain mutation, live paid APIs, original media changes, real fixture changes, FCP launch, push or deployment. No dependencies without separate authorization. Product/tests only; docs belong to Sol. Preserve legacy subtitle and approval contracts. Send exactly one worker_done after committing the scoped implementation.
