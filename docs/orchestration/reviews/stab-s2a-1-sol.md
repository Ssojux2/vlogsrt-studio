# STAB-S2A-1 — Sol independent acceptance evidence

Decision: **S2A-1 ACCEPTED offline at `853c031` on 2026-09-27**. Same-job execution ownership, duplicate outcomes and native target wiring are implemented and tested. S2A-2/S2A-3 and the remaining S2–S5 work are pending. Protected-configuration isolation of earlier broad test runs is not claimed; see the explicit limitation below.

## Scope, ownership and commits

- Baseline product `a9db170`, docs `933de7c`. Sol wrote briefs/docs and independently reviewed/tested; Claude-GLM wrote all product and implementation tests.
- Sol split the complete S2A umbrella into ownership, credentials/preflight and progress/refresh in `c9cf8e6`. Only ownership executed. R1 test brief: `e228fe1`.
- GLM `1294ce87fef1d304a4660e745ddbf1ea372b554b`: ownership and first status transport; `7d3fe93`: exclusive publication and transport-success semantics; `968d919`: four XcodeGen-generated project lines including the new Swift file; `853c031`: isolated actual-installed-wrapper proof.
- Initial immutable Impact: propagating, files<=8, modules<=4, shared-api. Actual initial unit: 11 files. Sol explicitly approved the ninth entrypoint cancellation test, tenth installer and parent-required eleventh generated project. The estimate was not rewritten. R1 stayed within local/files<=1/modules<=1/private, test-only.
- Runtime contract: [EXECUTION_OWNERSHIP.md](../../EXECUTION_OWNERSHIP.md). Briefs: [S2A-1](../tasks/stab-s2a-1-execution-ownership.md), [R1](../tasks/stab-s2a-1-r1-installed-wrapper-proof.md).

## Independent evidence

| Command | Code under test | Exit | Result |
|---|---|---:|---|
| `.venv/bin/python -m pytest tests/project/test_engine_execution_lock.py::test_second_request_for_a_held_job_exits_three_without_touching_it -q` | Unchanged `a9db170` product | 1 | Behavioral RED: duplicate alias entered the engine instead of rejecting, then hit the 30-second timeout |
| `cd macos && swift test --filter engineExitThreeMeansDuplicateOwnershipNotFailure` | Unchanged `a9db170` product | 1 | Behavioral RED: one test, two issues, 0.008 seconds; duplicate synced and became `engineFailed(3)` |
| `make check` | `7d3fe93` | 0 | 2715 Python tests in 56.80s; Ruff check/format 121 files; mypy 54 files; 21 invariants |
| `cd macos && swift test` | `853c031` | 0 | 82 core + 1 extension = 83 tests; final core run 2.844s |
| `.venv/bin/python -m ruff check macos/scripts/vlogsrt-engine.py` | `7d3fe93` | 0 | Uncovered script check passed |
| `.venv/bin/python -m ruff format --check macos/scripts/vlogsrt-engine.py` | `7d3fe93` | 0 | One file already formatted |
| `zsh -n macos/scripts/install-development-engine.sh` | `7d3fe93` | 0 | Installer syntax passed |
| `xcodebuild -project macos/FinalCutAgent.xcodeproj -scheme FinalCutAgent -configuration Debug -derivedDataPath /tmp/stab-s2a1-sol-derived CODE_SIGNING_ALLOWED=NO build` | `968d919` | 0 | Fresh derived-data unsigned native app/extension build: BUILD SUCCEEDED |

Original command exits were captured before reading logs. Python and Swift ran independently; no pipeline-tail exit was used for Sol acceptance. Logs: `/tmp/stab-s2a1-sol-red-python.log`, `/tmp/stab-s2a1-sol-red-swift.log`, `/tmp/stab-s2a1-sol-final-make.log`, `/tmp/stab-s2a1-sol-final-r1-swift.log`, `/tmp/stab-s2a1-sol-final-xcode.log`.

Explicit-path `git diff --exit-code` verified no product/test change before RED, no Python/product/script difference from `7d3fe93` to `853c031`, no native-project difference from `968d919` to `853c031`, and no uncommitted product changes after acceptance. Those exits were 0, so unchanged-surface evidence was reused. An earlier independent draft run passed 15 Python tests; it is superseded by the aggregate result. The pre-R1 Swift83 run is superseded by final R1 Swift83.

## Boundary review and corrections

- Python locks before workspace/staging, keys by canonical directory rather than operation/job-id spelling, distinguishes contention from OS failures, closes descriptors on failed acquisition/unlock, and never unlinks the lock inode. Real subprocesses prove same-job/alias rejection, distinct-job overlap, post-failure/cancellation release, SIGKILL recovery and non-inheritance to an outliving child.
- Swift's actual `main.swift` request uses the gate before grant/staging and retains ownership through runner sync/import. Tests cover concurrent duplicate, thrown executor, actual Task cancellation and independent jobs. A per-job board fixes the earlier scalar cleanup that erased another job's cancellation context.
- Draft status3 handling came after drift writes. It now returns `alreadyRunning` immediately after executor completion, including concurrent cancellation, without sync/import or drift cancellation mutation. Existing generic failure assertions moved from reserved status3 to4 without losing diagnostics assertions.
- An assumed Foundation error domain was disproved by a real synthetic launcher probe. English localized-text parsing was rejected, then numeric-domain classification was removed too. The owned file transport binds invocation token/path, reads at most257bytes, rejects malformed/oversized/missing/foreign records, and accepts records only after successful transport completion. Real synthetic scripts include output beyond pipe capacity and valid-record-then-nonzero/SIGKILL cases.
- Initial status publication could overwrite an arbitrary destination, including job files, or a file created after validation. Final code rejects invalid/inside-job targets and uses exclusive final creation. Synthetic tests preserve existing bytes and ensure invalid arguments produce no record/workspace mutation.
- Test review corrected call-count synchronization, fail-before-release ordering, invalid generic-cancellation evidence, descriptor-inheritance coverage, fd-close assertions and both orders of per-job completion. No assertion was weakened merely to pass.
- Parent found missing native Xcode source membership. XcodeGen added the new file; Sol independently built the actual unsigned application and extension. SwiftPM alone was not treated as native build evidence.
- R1 replaces a handwritten wrapper with the actual installer output in paths containing spaces. It uses copied public sources, byte-identical entrypoint, an exec shim to the real venv, synthetic offline config and a real held lock. The final independent suite proves logical statuses0/1/3 through installer → generated wrapper → Python → launcher. A symlinked interpreter initially lost venv site-packages (`rich` unavailable); the exec shim fixed this test setup error.

## Test-isolation disclosure and pending prerequisite

Public source inspection established that `cli.main` unconditionally calls `env.ensure_loaded()`. Lookup searches cwd ancestors and resolved package root; offline mode does not disable loading. Existing pytest fixtures block networking and set offline values but do not prevent real-root dotenv lookup. Earlier broad Python and real-package bridge tests therefore invoked paths that could read a protected `.env` if present. Neither its existence nor contents were inspected to establish an actual read; no credential contents were intentionally inspected or observed in returned output, and no protected-file mutation was observed. Numerical test passes are behavioral evidence, not proof of protected-file isolation.

GLM explicitly disclosed an R1 targeted invocation of the generated wrapper against the real repository before processing the isolation message. Exact observed command: `swift test --filter launcherReadsTheRealPythonEntrypointStatusRecord`, 2026-09-27 **07:34:33.821 UTC**, exit0. Its prose estimate “16:57” was inaccurate and is not used as timing evidence. Investigation read only public loader/test code, never protected content. The final R1 test isolates cwd, package fallback and ancestor search with copied public sources and a test-authored offline dotenv fixture. The final independent Swift run uses that corrected test.

Before another broad suite, execute the separately bounded [test-isolation prerequisite](../tasks/stab-test-isolation.md). It must cover existing in-process and subprocess paths with synthetic config/read guards while preserving production defaults and dotenv tests. Parent requested this next unit; it was not silently added to product scope here. Two introductory comments in the final Swift test still describe its earlier symlink/no-synthetic-dotenv setup; the executable setup uses an exec shim and test-authored dotenv as described above. Clarify those comments in the test-isolation unit.

## Lifecycle and High Agency evidence

- Sol live task `task_d8a64c12c905` / `ctx_593a6e487ad0`; child Run `run_5426631698bf`, parent Run `run_85984ee1e6c3`.
- Initial GLM `task_8e3879a67ce3` / `ctx_9a56f0c7ba72`, done `msg_fa70b80ffc69`; R1 `task_a4220730b5e7` / `ctx_f0ea0d922f8f`, done `msg_39516ae4ed97`. Both completed/succeeded were independently confirmed.
- Exact launcher: `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`. Terminal-create/readiness/adoption used real Orca1.4.215 state. Adoption metadata has model:null; no effective-model telemetry claim is made.
- Initial High Agency direct Read at1790490883899 preceded edits. R1 Read at1790494395817 covered the first60lines with prior full context. Initial Impact at1790491482855; R1 local Impact at1790494410822. Initial discovery took about10minutes; visible token counters are not billing or causal proof of a skill/model defect.
- Initial meaningful RED was independently reproduced before product edits via task-bound ask `msg_ed39a8557300` and reply `msg_ff4c44460798`. R1 was a coverage correction, not fabricated product RED. Scope and gate evidence were preserved across commits.
- R1 start reported `turn_start_unobserved`, request596b077d-ef7a-4c09-accb-649b57c691ce, despite the exact terminal displaying new preamble/brief Read. Same authoritative Dispatch was retained; no duplicate launch/stop.
- Mailbox delays and truncated ack output required correction. Parent separately disclosed its missed ack of delivery_2252b5c1233c. Durable messages were preserved; do not attribute every delay to FIFO replay without evidence. A late installed-wrapper request crossed initial settlement and was handled by fresh R1, never by reusing settled IDs.
- Final exact GLM terminal `term_c52ca245-8c90-4a03-8cf8-24332851df43`, incarnation3d36f700-2653-4871-afce-63aa06690136, was proven idle. Release returned retained/external_terminal; authorized close returned ptyKilled:true. Reclaimable worker rows were empty. No other terminal was closed.
- Shared docs were deferred through final verification/settlement and written only after idle/close. Installed skills/plugins were unchanged. Feedback: [High Agency implementation feedback](../../reports/2026-09-27-high-agency-implementation-feedback.md).

## Remaining external acceptance

No unresolved S2A-1 product failure was observed after final checks. Existing broad-test isolation remains the explicit next verification prerequisite above. The prior S1A `.venv/bin/python scripts/build_engine.py --verify` failure remains: exit1, missing `macos/Resources/Engine/engine-manifest.json`, log `/tmp/stab-s1a-sol-final-bundle.log`. S2A-1 did not supply engine/FFmpeg binaries or repair release packaging; no new packaged-binary verification is claimed. The unsigned Debug build does not prove signing/notarization, release bundle operation, live STT/vision, real-media quality or Final Cut roundtrip. Those and S2A-2/S2A-3/S2B/S2C/S3–S5 remain pending; no push/deployment/FCP or paid product call was performed.
