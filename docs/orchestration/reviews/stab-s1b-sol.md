# STAB-S1B — Sol independent acceptance evidence

Decision: **S1B ACCEPTED at `a9db170` on 2026-09-27**, after rejecting the UI routing in `cd80d61` and independently verifying its Swift correction. S1A and S1B are now accepted offline; S2–S5 remain pending. No live API, real-media, Final Cut or packaged-release acceptance is claimed.

## Ownership and commits

- Sol wrote briefs, contract/checkpoint/review documentation and ran independent checks. Claude-GLM wrote every product/test change.
- Current checkout: `Ssojux2/r08-clip-input`; accepted S1A code `975fb9c`.
- Root standing-document commit `c224c08`; Sol boundary decisions `5e50f49`; R1 brief and exact mailbox acknowledgment loop `ee4b253`.
- GLM implementation `cd80d61bf6a027bdd9928cb74d2338731e064834`: 18 product/test files, matching the initial <=18-file estimate.
- GLM correction `a9db1705cb52b3e09afcb22e6d1e529fce1075f0`: five Swift files, within the <=6-file / <=3-module estimate.
- Briefs: [S1B](../tasks/stab-s1b-analysis-outcomes.md), [R1](../tasks/stab-s1b-r1-review-routing.md), [common contract](../tasks/stab-execution-contract.md).
- Final behavior: [ANALYSIS_OUTCOMES.md](../../ANALYSIS_OUTCOMES.md).

## Accepted behavior

1. Protocol 1.3 distinguishes successful keep/cut from error/skipped decisions, retains sanitized reasons and validates revisions. Legacy 1.1/1.2 decisions remain readable with old digests and unknown outcome semantics. New 1.3 records missing outcome fields are rejected.
2. Only successful valid decisions produce candidates. Failed/skipped coverage preserves footage. All-window failure is error despite successful upstream stages; valid no-audio/zero-window coverage remains distinct from all-audio/transcription/VAD failure.
3. Aggregate results carry attempt/input/estimate identity, window counts, consistent upstream totals/failed/skipped counts, usable source coverage and candidate counts. Partial results exit 0 into review with a warning and actual safe candidates. Complete errors exit nonzero, retain diagnostics and keep the job retryable.
4. Decisions persist per completed window. Preparation/analysis-stage exceptions retain sanitized failure evidence. Final cancellation precedes aggregate publication; accepted S1A cancellation paths remain covered.
5. Python entrypoint and Swift development sync expose diagnostics on nonzero exit while excluding candidate/export artifacts. Cancellation/drift checks still precede sync and failure never imports.
6. Swift requires a readable current estimate and complete attempt log, matching identities and terminal status agreement. Unknown/stale/running/cancelled/inconsistent history cannot enable review or implicitly rerun. Fresh jobs retain initial routing; valid failed jobs retain explicit retry. App and extension display honest outcome summaries.

## Final independent checks

| Command | Code under test | Exit | Observed result |
|---|---|---:|---|
| `make check` | `cd80d61` Python/tests/scripts | 0 | 2697 Python tests in 51.97s; Ruff check/format 120 files; mypy 54 source files; 21 invariants |
| `cd macos && swift test` | `a9db170` | 0 | 71 core + 1 extension = **72 tests**; app/core/extension compiled |
| `.venv/bin/python -m ruff check macos/scripts/vlogsrt-engine.py` | `cd80d61`, unchanged at `a9db170` | 0 | Passed |
| `.venv/bin/python -m ruff format --check macos/scripts/vlogsrt-engine.py` | Same | 0 | One file already formatted |
| `git diff --exit-code cd80d61 a9db170 -- vlogsrt tests macos/scripts` | Commit comparison | 0 | Python/tests/scripts byte-identical across R1 |
| `git diff --exit-code a9db170 -- vlogsrt tests macos/Sources macos/Tests macos/scripts` | Final working tree | 0 | Product/test tree matches accepted commit |

Logs: `/tmp/stab-s1b-sol-final-make.log`, `/tmp/stab-s1b-sol-final-swift.log`. Tests were redirected directly; exits came from the test command, not a trailing filter. The unchanged Python surface was not needlessly retested after the Swift-only correction. No assertion was weakened or skipped to obtain acceptance.

## Independent rejection and correction

The initial reader returned nil/unknown for stale or unbound results, but `shouldLoadCandidates(.legacyUnknown, true)` still returned true. The extension also fell through from unknown to engine invocation. Helper-level binding tests therefore passed while the actual consumer could review stale candidates or rerun ambiguous history.

R1 added a regression using the existing reader and routing helper together before production changes. Sol ran:

`cd macos && swift test --filter unboundHistoryMustNotEnableCandidateReviewOrImplicitRerun`

- Before: exit 1, one test with **11 behavioral issues** in 0.018s. Cases included stale aggregate, latest running/cancelled attempt, missing/malformed/truncated estimate, empty/garbage attempt history and inconsistent attempt/result status. Log: `/tmp/stab-s1b-sol-r1-red.log`.
- Sol separately ran the production diff check from the repository root against `cd80d61`: exit 0. An earlier relative-path diff inside `macos` was not used as evidence.
- Sol replied to the worker's task-bound ask before production edits resumed.
- After: the same regression passes within the final full Swift suite, with fresh/unknown/valid routing and explicit retry coverage added.
- R1 requires the current estimate, rejects a partially corrupt attempt log instead of compact-mapping away invalid rows, and uses the shared router in the actual extension handler. Unknown clears controls and returns; only a truly fresh job enters initial engine routing.

## Earlier evidence and its limits

- Initial entrypoint run stopped at collection: exit 2, missing new `AnalysisResult` import. `/tmp/stab-s1b-sol-entrypoint-red.log` is **not** a behavioral reproduction.
- A draft run of outcome + entrypoint files returned 21 passed / 1 failed, exit 1 in 1.14s. Invalid operation `transcribe` returned 1 instead of contractual 2; GLM restored the existing assertion. Log: `/tmp/stab-s1b-sol-python-draft.log`.
- Draft normalization selection: 1 passed / 9 deselected, exit 0; draft cancellation suite: 11 passed in 3.12s, exit 0. These unpinned observations are superseded by final checks.
- GLM disclosed two scoped stashes of product/test paths for its early baseline probe. The reported paths excluded `.env.example`, but shared code was temporarily swapped. Earlier draft checks are not used to prove an immutable S1B snapshot.
- GLM subsequently reported an isolated historical baseline from `git archive` at `5e50f49`: the pre-change-API probe failed on false-success exit 0, then passed on the implementation. This is worker-reported evidence; Sol's pinned final tests and independent R1 before/after are the acceptance basis.
- The real-command integration tests cover all-window failure with successful fake upstream boundaries, all-upstream failures, declared no-audio, usable zero-window coverage mixed with a failed source, stage exceptions, real proposed-cut partial retention, cancellation and engine-root diagnostic/partial propagation. Providers/media boundaries are synthetic; actual CLI/commands/entrypoint and shared Swift consumers are exercised.

## Lifecycle and workflow

- Parent Task/Dispatch: `task_1e5386ef6bff` / `ctx_0b54e2ea914e`; nested Run `run_5426631698bf`.
- Initial GLM: `task_97901274f94a` / `ctx_94de98b3b539`; R1: `task_8406c023d6ef` / `ctx_9cdf4f52aa21`. Both settled successfully; the first product result required correction.
- Exact launcher: `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`, terminal create/readiness/worker-start adoption. Adopted-terminal metadata reports model null; no effective-model receipt is invented.
- Initial High Agency Read verified at `1790485689601` before edits. R1 directly reread the first 60 lines at `1790489499657`, retaining the previously read full skill; actual application included bounded scope, regression-first work and independent reproduction before edits.
- FIFO replay hid later guidance because GLM treated a heartbeat summary as acknowledgment. A single non-interrupting operational nudge recovered exact `--ack` use. Receipt confirmed acknowledgment of `delivery_95345f707d3e` and delivery of nine queued corrections; GLM explicitly acknowledged them. Full-body reading and exact delivery acknowledgment are now in the common contract.
- R1 start returned exit 1 `turn_start_unobserved` despite accepted input. Exact terminal activity and a successful task-bound ask proved work was underway; the authoritative Dispatch was preserved without relaunch. After worker_done, Sol observed `completed/succeeded`.
- After independent acceptance, R1 release returned `retained/external_terminal`. Exact terminal incarnation and idle readiness were confirmed; authorized close of `term_db28848c-9607-4a8d-8504-8506ef6f70c1` returned `ptyKilled: true`. Reclaimable worker rows are empty. No worker remains active.
- Shared docs were deferred during final verification/settlement; no repeat of the prior shared-snapshot stop-hook problem was observed. Detailed feedback: [High Agency implementation feedback](../../reports/2026-09-27-high-agency-implementation-feedback.md).

## Remaining work and external acceptance

S2A runtime UX is next after the parent compacts this idle Sol terminal and assigns a fresh Dispatch. S2B cache/retry/budget accounting, S2C import lifecycle, S3 integrated candidates, S4 captions and S5 sampling/build work remain planned, not completed by S1B.

Known external build gap: the S1A command `.venv/bin/python scripts/build_engine.py --verify` returned exit 1 because `macos/Resources/Engine/engine-manifest.json` was absent. S1B did not change binary supply/build packaging; no fresh bundle acceptance is claimed. Supplied FFmpeg/engine binaries, signing/notarization, paid STT/vision, real Final Cut roundtrip and the 2-minute → 10–20-minute → 77-minute quality targets remain unperformed. This does not block the remaining authorized offline stages.
