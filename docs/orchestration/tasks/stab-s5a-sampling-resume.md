# STAB-S5A — Sequential source sampling and verified stage resume

Read [the common execution contract](stab-execution-contract.md) before edits; it fixes the required High Agency, mailbox, protected-file and verification workflow.

## Scope
After S4 acceptance, GLM changes `vlogsrt/preprocess.py`, project vision sampling, approved analysis orchestration/checkpoints and tests. Sol owns docs/review. Read approved plan and existing frame-correspondence contracts. Keep media decoder subprocesses in preprocess boundary; no new dependencies.

Impact: propagating | files<=14 | modules<=4 | boundary=shared-api

## Decisions and requirements
1. Plan needed frames across all candidate windows per source before extraction. Deduplicate source/frame/config requests, sort indices and extract in sequential decoder passes per source instead of restarting ffmpeg from frame zero for each frame/window. Bound command/output size for large plans with explicit chunking if necessary; preserve deterministic mapping.
2. Preserve exact original frame index, stream identity, rational presentation time, proxy correspondence, source/timeline mapping, deterministic image bytes/hash and request order. Duplicate requested frames share an artifact while retaining their frame IDs. Do not replace index-accurate extraction with approximate seeks.
3. Validate decoded output count and each planned output mapping before publishing cache/checkpoint. Missing/corrupt/partial extraction fails closed; temp images never become trusted artifacts. Cancellation between passes prevents later decoding/model calls.
4. Durable stage checkpoints cover preparation, transcript/VAD, frame plan/extraction, window decisions and candidate/result publication. Reuse only successful complete stages whose input/media/config/model/prompt/tool/schema identity matches. Changed source, proxy, config or artifact hash invalidates dependent stages without deleting unrelated valid source stages.
5. Resume from interruption uses successful checkpoints and S2 accounting, does not repeat valid paid requests, and retries errors only on explicit retry. Corrupt/incomplete checkpoints must not appear as success. Do not cache hidden live failures or reset budgets.

## Observable acceptance
- Generated synthetic constant-rate media with distinct frames: unsorted/duplicate/boundary frame requests from original and compatible proxy match legacy single-frame extraction bytes or decoded pixels exactly, with identical indices/timestamps.
- Assert subprocess/decoder invocation count scales with sources or bounded batches, not requested frame count; many windows sharing a source use one planned sampling batch. Benchmark synthetic short vs longer input and report commands/timing without pretending to measure the real 77-minute job.
- Interruption/resume invokes only missing valid stages; corruption and input/settings/proxy changes invalidate correct dependencies. No API network calls; fake clients count requests.
- Run affected/full Python tests, Ruff/mypy/invariants and Swift regressions if protocol changes; commit explicit paths and report SHA/exits. Preserve original media/env/API fixtures. No FCP, push or deployment.
