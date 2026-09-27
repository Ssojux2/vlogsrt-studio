# STAB-S4A — Final-audio caption estimate and authorized engine execution

## Scope
After S3 acceptance, GLM implements caption operations in Python commands/final_caption/protocol/engine entrypoint and Swift invocation/codec/staging plus tests. Sol owns docs/review. Read approved plan and prior accepted lifecycle/accounting contracts.

Impact: propagating | files<=16 | modules<=5 | boundary=shared-api

## Required decisions
1. Expose distinct caption-estimate and caption-run operations through the common development/bundled `<operation> <job_id> <job_dir>` entrypoint. Estimate validates staged final WAV/FCPXML and returns independent cost without transcribing or needing an API key. Run requires explicit matching caption authorization; roughcut authorization never authorizes captions.
2. Use the final rendered edit audio exclusively. Validate PCM format, audio hash, XML hash/result-project UID, duration/sample correspondence and source job/origin mapping before estimate and immediately before run. Reuse existing final-caption validation; no fallback to original source audio on missing/failed final export.
3. Stable estimate identity binds input, settings, pricing and final edit revision. Changed audio/XML/settings invalidates authorization. Stage final WAV/FCPXML in development and bundled jobs correctly; the fresh caption operation must not require nonexistent original `input.fcpxml` or accidentally receive a new roughcut workflow.
4. Persist caption attempt/progress/result and warnings, including successful no-speech vs failed transcription. Failure is retryable under the same valid request, retaining S2 cumulative budget and successful cache. Never leave the journal permanently stuck at caption_authorized after an exception.
5. Copy/sync SRT and warning/review artifacts plus JSONL on success and failure; result paths must resolve in the app-visible job, not an inaccessible development workspace. Atomic writes prevent half-visible SRT/results.
6. Honor cancellation before each external call, apply S2 budget/usage accounting to actual caption passes/retries, and never automatically import SRT. Preserve existing whisper timing/repair/refine/retext rules.

## Acceptance
- Actual entrypoint on fresh staged synthetic final export: estimate produces no transcriber calls, authorization then run produces SRT and warnings; no original input requirement.
- Missing/mismatched approval, changed hash/duration/UID/settings, no-speech, transcription failure and retry, cancellation and cumulative-budget exhaustion have truthful persisted states.
- Fake final-audio transcriber proves the final WAV (not original audio) supplies all requests. Swift development/bundle invocation and result path sync tests read produced files.
- Run affected Python/Swift suites, Ruff/mypy and `make check`; explicit-path commit and exact commands/exits. No live API/FCP, real fixture/media/env access, push/deploy. Product/tests only; one worker_done.
