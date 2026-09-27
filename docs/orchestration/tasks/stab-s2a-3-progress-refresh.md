# STAB-S2A-3 — Progress and automatic refresh (pending)

This reserved unit retains S2A umbrella requirements 5–6 and corresponding acceptance/boundaries; read [umbrella](stab-s2a-runtime-ux.md) and [common contract](stab-execution-contract.md). Execute only under a fresh Dispatch after ownership and preflight acceptance. Sol must refine the bounded brief against accepted APIs before launching GLM.

- Extension automatically refreshes durable atomic job progress/results during a single engine request with a bounded timer or equivalent. Stop on terminal state, job replacement, teardown or cancellation; never launch the engine to refresh.
- Preserve checkbox choices for the same candidate revision, reset for a new revision, retain S1 partial/error/unknown behavior and S2A-1 duplicate ownership.
- Progress binds job/input/attempt and stage with known completed/total work; show no fabricated percentage for unknown totals. Read legacy workspaces conservatively without automatic ambiguous reexecution.
- Tests update a synthetic result while execution blocks and prove UI model refresh/terminal state without reexecution, lifecycle stop conditions, revision-aware selections and invalid/legacy snapshots. Update Python/Swift contracts coherently with legacy reads.
- GLM product/tests, Sol docs/independent acceptance; targeted checks then one final aggregate gate plus uncovered Swift/scripts. Protected credentials/media/fixtures remain untouched; no APIs/FCP/push/deploy. Budget/cache is S2B and import lifecycle S2C.
