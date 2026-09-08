# Task <phase>-<number>: <single outcome>
## Authority
Binding spec section; approved phase; manager; reviewer; Orca task/dispatch IDs.
## Baseline and ownership
Integration baseline commit; exact worker branch/worktree; allowed files; forbidden/shared files.
## Contract
Exact consumed/produced APIs/types/units; frozen decisions; dependencies and consumer tasks.
## Implementation steps
Concrete small steps, tests first for behavior; examples/edge cases; no vague choices passed to GLM.
## Acceptance
Exact scoped commands and expected assertions; make check; offline-only constraints; real-media gates explicitly separate.
## Failure route
Reproduction/evidence to collect; Sol -> Terra/Luna -> Opus5 -> Astra; code changes remain GLM.
## Report
Commits, files, commands+exit/results, remaining findings, status, branch ready-to-integrate (not merged).
Before worker_done, send the exact committed SHA and verification evidence as review-ready status,
then use Orca ask to receive Sol's review disposition. Fix scoped findings in the same Dispatch and
rerun affected checks; report worker_done only after review is accepted. Do not edit while awaiting
review. An immediate correction after settlement may reuse the same GLM session through a fresh
Dispatch; unrelated/independent tasks start fresh. Follow Orca reuse/release lifecycle receipts.
