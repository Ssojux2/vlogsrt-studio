# Per-job execution ownership

S2A-1 is accepted offline at `853c031`. Python and Swift enforce separate boundaries of the same request.

## Request lifetime

`FinalCutAgentApp/main.swift` calls `EngineRequestGate.perform` before media-grant writes, staging, engine launch, result sync or import. The MainActor gate uses the resolved job directory as its key. Symlink aliases collide; different directories remain independent. The claim lasts through the whole request and releases with `defer` on returns and thrown errors, including cancellation. `EngineJobBoard` retains each running job's cancellation context; completing one job removes only that job. The cancel button targets the most recently started job still running.

The shared Python executable resolves the job directory and acquires a nonblocking `flock` before creating its workspace or staging/mutating execution state. `.vlogsrt-engine.lock` stays on disk; its existence does not mean the job is running. The open, non-inheritable descriptor owns the lock. Closing or process exit releases it, including crash recovery. The file is not unlinked, avoiding competing lock inodes. Only `EAGAIN`/`EWOULDBLOCK` mean contention; other OS errors remain failures.

## Direct executable contract

`vlogsrt-engine <operation> <job_id> <job_dir>` supports `analyze` and `roughcut`.

| Exit | Meaning |
|---:|---|
| 0 | Operation succeeded |
| 1 | Execution failed; existing S1 diagnostic propagation applies |
| 2 | Invocation contract rejected |
| 3 | Canonical job already running; no duplicate attempt, staging, sync or import |

Exit 3 prints `vlogsrt-engine: job already running (duplicate request rejected)`. The duplicate does not alter the legitimate attempt. Swift maps logical status 3 to `alreadyRunning` immediately after execution returns, before post-run cancellation/drift processing. All ordinary S1 cancellation, selection-drift and diagnostic gates retain their order.

## Development launcher transport

`NSUserUnixTask` does not expose a reliable numeric engine exit contract through localized error text. The installed development wrapper therefore forwards an optional fourth argument: a fresh invocation-owned status path outside the job directory. Three-argument callers, including the bundled path, keep the direct exit contract above.

The launcher creates a unique temporary directory and expects exactly one record:

```text
VLOGSRT-ENGINE-STATUS <invocation-token> <logical-engine-code>
```

The entrypoint validates the absolute destination, matching directory/token naming, token characters, absence of an existing file, and exclusion from the canonical job tree. It creates the final record exclusively and never replaces another file. In fourth-argument mode, process exit 0 means the logical status was delivered; the logical code, including 1 or 3, is in the record. Invalid destination or failed delivery remains a nonzero process exit. A duplicate may write only this invocation's external status record, never job state.

Swift accepts a record only if `NSUserUnixTask` reports no error. It reads at most 257 bytes, enforces a 256-byte record limit, validates the token and code, and cleans up its temporary directory. A missing, malformed, oversized or mismatched record, a signal, or a nonzero transport exit remains an error. There is no localized-error or OS-domain heuristic. Legacy installed wrappers without this transport fail conservatively and must be reinstalled with the current installer.

## Evidence and limits

The executable subprocess tests cover contention, aliases, independent jobs, failure/cancellation release, crash recovery and descriptor inheritance. Swift exercises the actual gate/runner composition, cancellation and thrown errors, per-job cleanup and status transport. The installed-wrapper integration uses copied public sources, test-owned configuration, paths with spaces and a real OS lock; it proves logical 0/1/3 through the actual installer, launcher and Python entrypoint.

See [Sol acceptance evidence](orchestration/reviews/stab-s2a-1-sol.md). An unsigned native Debug build passed. Packaged engine/FFmpeg supply, signing/notarization, live providers and real Final Cut/media acceptance remain separate. Existing Python test-harness configuration isolation must be addressed before another broad suite; the prior numerical test results do not prove protected-file isolation.
