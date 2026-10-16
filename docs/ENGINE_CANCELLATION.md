# Engine cancellation contract

Scope: S1A stabilization in the approved 2026-09-27 plan. This documents the Python/Swift execution boundary; it does not certify a bundled binary or real Final Cut run.

- Both development and bundled launches use `macos/scripts/vlogsrt-engine.py` with `<operation> <job_id> <job_dir>`. The workspace is created before optional staging. The entrypoint exposes its job root through `VLOGSRT_ENGINE_JOB_DIR` so later app writes remain observable.
- The engine reads cancellation from the workspace and the current job root on every gate. Copying a request once at startup is insufficient. App writes are atomic and may occur before workspace creation.
- `CancellationRequest` retains its existing JSONL shape and digest. Its `input_revision` must equal either the running journal's full revision or its exact `input_sha256`. The latter is the SHA-256 of staged `input.fcpxml` bytes and permits cancellation before `revision.jsonl` exists.
- The input-hash alias is checked against this running input, within this job's cancellation files. It is not a global cross-job signal. Different input bytes do not match; identical input copies still live in separate job directories. All-zero is not a wildcard. `attempt_id` must still equal the current attempt or `*`.
- The app binds revision/hash lookup to the selected launch plan's actual job directory. A stale App Group workspace must not override a development run, and a saved development bookmark must not override a bundled run. Cancellation destinations come from the active launch context.
- Gates run before each transcription delegate call, each actual chunk/retry/rejected-parameter request, and each Luna call. SDK automatic STT retries are disabled so they cannot bypass the gate. Calls already in flight may return; cancellation prevents subsequent gated calls.
- A final gate runs after analysis evidence is recorded and before successful candidate publication. Cancellation during the last model call or a no-window analysis closes the attempt and workflow as `cancelled`, rather than `succeeded`/`review_required`.
- `WorkflowCancelled` intentionally derives from `BaseException`: existing source/boundary adapters convert ordinary exceptions to failure records, but cancellation must reach the workflow owner. The CLI catches it explicitly and returns a nonzero cancellation result.
- Cancellation preserves recorded usage, caches and already-persisted decision evidence. S1A does not promise live process termination or recovery of an interrupted network response. Per-window result persistence and aggregate failure/result propagation are addressed separately in S1B.

Evidence is maintained in [the S1A Sol review](orchestration/reviews/stab-s1a-sol.md). Regression coverage includes the real fresh entrypoint, installed development wrapper, Python request gates and workflow states, and Swift launch-context cancellation construction.
