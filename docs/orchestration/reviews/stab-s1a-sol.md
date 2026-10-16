# STAB-S1A — Sol independent acceptance evidence

Decision: **S1A ACCEPTED at `975fb9c` on 2026-09-27**, after correction of `616d233` and independent Sol validation. S1B and S2–S5 remain pending. This is offline implementation acceptance, not real Final Cut or bundled-release acceptance.

## Scope and authority
- Approved plan: `docs/plans/2026-09-27-vlog-automation-stabilization.md`.
- Brief: `docs/orchestration/tasks/stab-s1a-entrypoint-cancellation.md`.
- Code baseline: `0d91da1`; parent S1A brief: `abbed48`; Sol integration addendum: `bd18b35`.
- GLM owns all product/test implementation; Sol has edited only briefs/checkpoint/review documentation.
- Nested Run `run_5426631698bf`; initial Task/Dispatch `task_713f37f60836` / `ctx_e1c3232736f7`; correction `task_9c1fc63e6acf` / `ctx_4c37d0b87a18`. Both settled; one serial GLM writer throughout.
- Exact custom launcher requested: `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`; terminal readiness satisfied and worker-start turn start observed. Adopted-terminal Orca metadata reports model null, so model selection evidence is the actual launcher command, not a fabricated effective-model receipt.

## Independent static findings sent to GLM
1. Fresh bundle entrypoint copies staged media grant before workspace creation (parent reproduction).
2. Root cancellation is copied once; running engine checks workspace only, losing later app cancellation.
3. App cancellation reads App Group workspace revision even when development engine writes external workspace; all-zero fallback may be ignored.
4. Installed development wrapper is a separate flow and omits staged media grant; bundle-helper-only tests cannot prove development behavior.
5. Per-job delegate gates cannot prevent later chunks, rejected-parameter retries or transient retries within the same delegate call; SDK-internal retry configuration also requires scrutiny.
6. Broad exception adapters in transcription/repair/boundary can convert cancellation into an ordinary error/empty result unless propagation is explicit.

## Final independent checks at 975fb9c
| Command | Exit | Observed result |
|---|---:|---|
| `make check` | 0 | 2675 Python tests passed in 55.97s; Ruff check/format 119 files; mypy 54 files; 21 invariants passed |
| `cd macos && swift test` | 0 | 67 tests passed: 66 core plus 1 extension in separate test runs; app/core/extension package compiled |
| `.venv/bin/python -m ruff check macos/scripts/vlogsrt-engine.py` | 0 | Passed |
| `.venv/bin/python -m ruff format --check macos/scripts/vlogsrt-engine.py` | 0 | Formatted |
| `zsh -n macos/scripts/install-development-engine.sh` | 0 | Passed |
| `.venv/bin/python scripts/build_engine.py --verify` | 1 | Missing `macos/Resources/Engine/engine-manifest.json`; known external bundle gap |
| `git diff --exit-code 975fb9c -- vlogsrt macos/Sources macos/Tests macos/scripts tests` | 0 | Product/tests unchanged after validated commit |

Logs: `/tmp/stab-s1a-sol-final-make.log`, `/tmp/stab-s1a-sol-final-swift.log`, `/tmp/stab-s1a-sol-final-bundle.log`. Exit codes came from the commands themselves, not trailing output filters. No tests were weakened/skipped to obtain acceptance.

## Earlier independent regression evidence
- Initial draft check: `.venv/bin/python -m pytest -q tests/project/test_engine_entrypoint.py tests/project/test_workflow_cancellation.py tests/project/test_development_engine_installer.py tests/project/test_workflow_analysis.py tests/project/test_workflow_protocol.py` — exit 0; log `/tmp/stab-s1a-sol-targeted-initial.log`. Run independently by Sol, output redirected without a pipe so the command exit is preserved.
- Committed request-gate check: `.venv/bin/python -m pytest tests/test_transcribe.py` at `616d233` — exit 0, **138 passed in 2.60s**; log `/tmp/stab-s1a-sol-transcribe-616d233.log`. This verifies the preserved transcriber boundary while R1 fixes separate workflow/app paths.
- R1 in-flight reproduction: `.venv/bin/python -m pytest tests/project/test_workflow_cancellation.py -k 'final_luna or no_windows or input_bound_pre_receive'` — exit 1, **2 failed, 1 passed, 8 deselected in 0.67s**; log `/tmp/stab-s1a-sol-r1-before.log`. Final-Luna and no-window cancellation returned 0 instead of 2; input-bound startup cancellation already passed because GLM had started that fix. This is not claimed as a pristine-HEAD three-failure run.
- Same R1 regression command after Python correction — exit 0, **3 passed, 8 deselected in 0.88s**; log `/tmp/stab-s1a-sol-r1-after.log`. The final aggregate checks above supersede the in-flight snapshot.

## First settlement and rejection
- GLM settled `ctx_e1c3232736f7` with commit `616d233` (13 product/test files) and reported 2672 Python / 63 Swift tests plus static checks. These counts are worker evidence, not Sol's final independent run.
- Final static review confirmed three remaining defects: running revision chooses App Group ahead of the actual external engine; pre-receive all-zero cancellation is explicitly ignored; last-call cancellation has no gate before success publication.
- Correction `975fb9c` resolves all three: actual launch-plan job provenance, exact input-hash fallback before receive, and a final cancellation gate before successful publication. The app captures launch-time destinations, and the shared cancellation service always includes the running root.
- Initial estimate was propagating / <=16 files / <=5 modules / shared API; total product/test scope is 13 files. R1's reported <=10-file estimate ended at 6 files. Scope matched; full checks were justified by the shared transcriber and Swift/Python boundary.
- The changed binding semantics are documented in [ENGINE_CANCELLATION.md](../../ENGINE_CANCELLATION.md). Gates preserve recorded evidence and wrong-revision/attempt filtering; no unapproved deletion behavior was added.

## Lifecycle and workflow evidence
- R1 `worker_done` was accepted, then `worker-release` returned `retained/external_terminal`. After matching the exact terminal incarnation and observing its completed final turn/idle prompt, the authorized close returned `ptyKilled: true` for `term_8d472175-1657-4d93-a2c0-e2aa88d36680`. The Run's reclaimable-worker query returned no rows.
- A High Agency stop hook briefly resumed the settled GLM because the shared Git snapshot changed from Sol documentation edits; the worker confirmed an empty product diff and reran only 16 targeted tests. Cleanup waited for that final idle state. This hook/lifecycle friction is recorded in the feedback report.
- High Agency was actually read at 04:20:54 UTC after initial implementation began; R1 applied the carried skill with regression-first checks. The earlier prohibited `.env` search attempt was disclosed and acknowledged; Sol did not inspect/reprint its result. See [implementation feedback](../../reports/2026-09-27-high-agency-implementation-feedback.md).

## External acceptance and task boundary
No paid STT/vision APIs, actual Final Cut operation, original-media processing, binary supply or distributable signing was performed. Missing bundle/FFmpeg supply, signing and 2-minute → 10–20-minute → 77-minute real-media/FCP acceptance remain external work; unit counts do not prove them.

Next: parent compacts the idle Sol terminal after its accepted checkpoint, then sends a fresh S1B Dispatch using `docs/orchestration/tasks/stab-s1b-analysis-outcomes.md` and the common execution contract. S1B must distinguish upstream audio/VAD failure from valid zero-window success. No compact has been claimed or executed by Sol in this task.
