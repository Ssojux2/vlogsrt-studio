# STAB-TEST-ISOLATION — Sol independent acceptance

Accepted offline on 2026-09-27 at **01f8616**, over **69790b4**. The Python test-isolation prerequisite is complete for the covered loader and CLI paths. S2A-2 has not started. The initial unsafe RED remains a disclosed instruction violation; subsequent guarded results do not establish that earlier run was isolated.

## Change and ownership

- Brief: [stab-test-isolation.md](../tasks/stab-test-isolation.md), decision gate committed at `2c83363`.
- Claude-GLM wrote six test files only: `tests/_dotenv_guard.py`, `tests/conftest.py`, `tests/test_env.py`, and the three project test modules for entrypoint, execution lock and development installer.
- `69790b4` installs the test guard and isolated subprocess setup; `01f8616` gates both installed-wrapper runs and adds missing/bootstrap-syntax/editable-fallback rejection proofs.
- Sol wrote the brief, reviewed corrections, independently ran acceptance checks, and owns this report and the feedback. No production bypass flag, loader/default change, dependency, credential feature, or product-code edit was introduced.
- Explicit comparison `git diff 853c031..HEAD -- vlogsrt macos/Sources macos/scripts macos/FinalCutAgent.xcodeproj/project.pbxproj scripts Makefile pyproject.toml` returned exit 0 with no changes at acceptance. Final owned-test diff and whitespace checks also returned exit 0, empty.

## Verified boundary

The parent guard installs at conftest import before project imports and survives ordinary fixture monkeypatch teardown. It classifies `.env` and `.env.example` lexically before the protected operation. Registered test directories permit synthetic fixtures. Blocked probes return missing semantics; blocked reads raise. The real dotenv loader still performs search, parsing and shell-precedence behavior against synthetic files, including an explicit synthetic `PACKAGE_ROOT` fallback.

Parent coverage includes `Path.is_file/exists/stat/read_text/read_bytes/open/resolve`, `os.stat/lstat/open`, `os.path.exists/isfile/lexists`, and `builtins.open`/`io.open`. String, bytes, Path and PathLike spellings are handled. Classification does not stat or read the protected candidate. Registering a known synthetic directory also records its resolved directory spelling for macOS `/tmp` aliases.

Child Python guards cover the tested probe/read APIs, excluding the parent's `Path.resolve` wrapper, and block `socket.socket` and `socket.create_connection`. Startup attempts the protected read routes against a test-owned canary and observes socket refusals. Proof records contain booleans and paths, not the canary value. A bootstrap or proof-write failure terminates with exit 125 before the engine payload. A test-owned preflight process detects missing or syntactically invalid startup protection before payload launch.

The lock tests retain the actual executable engine script and controlled fake downstream CLI. The installed-wrapper tests run the real installer, its generated zsh wrapper, the real interpreter through an exec shim, the byte-identical engine script, and copied public `vlogsrt/**/*.py` sources. Symlinked sources are rejected; no source links point back to the checkout. Each sandbox has one test-authored offline dotenv. First run and resume share the same preflight gate. Rejected launches preserve the complete job tree and create neither workspace nor lock/attempt state.

Normal child imports and cwd were independently read from proof artifacts under `/tmp/stab-isolation-sol-final-targeted-20260927`: both resolve inside the synthetic repository. In the deliberate missing-package test, the active guard permits importing the public editable `vlogsrt/__init__.py`, records that outside provenance, and the parent gate rejects the payload. This negative case is intentionally different from normal copied-package execution. No protected file is inspected to establish fallback behavior.

## Independent commands and exits

All original command exits were captured before output filtering. No broad unisolated suite was run by Sol in this unit.

| Command / evidence | Result | Log |
|---|---|---|
| `.venv/bin/python -m pytest tests/test_env.py -q` on guarded draft | exit 0; initial in-process check, superseded by final run | `/tmp/stab-isolation-sol-env-initial.log` |
| Four targeted modules at `69790b4`, `-o addopts= -q` | exit 0; 64 passed in 6.00s | `/tmp/stab-isolation-sol-targeted.log` |
| Four targeted modules at `01f8616`, `-o addopts= -q` | exit 0; **67 passed in 5.62s** | `/tmp/stab-isolation-sol-final-targeted.log` |
| **`make check` at `01f8616`**, after targeted approval | **exit 0; 2,735 passed in 60.08s**; Ruff check/format 122 files; mypy 54 source files; 21 invariants | `/tmp/stab-isolation-sol-final-make.log` |

Final targeted command:

```sh
.venv/bin/python -m pytest -o addopts= -q tests/test_env.py tests/project/test_engine_entrypoint.py tests/project/test_engine_execution_lock.py tests/project/test_development_engine_installer.py --basetemp=/tmp/stab-isolation-sol-final-targeted-20260927
```

The first approval question (`msg_976b267b0c9b`) received bounded corrections, not aggregate approval. Final approval (`msg_e0d569094ee9`) followed independent 67-test verification. Sol ran one aggregate; GLM did not duplicate it. No further code change followed that aggregate.

Unchanged-surface evidence is reused from [S2A-1](stab-s2a-1-sol.md): 83 Swift tests at `853c031`, engine-script Ruff/format and installer `zsh -n`, and unsigned native Debug app/extension build at `968d919`. These checks were not rerun in this test-only unit. They retain their previously documented limits.

## Review corrections

| Observed issue | Correction and proof |
|---|---|
| Blocked `os.path` probes returned True through boolean precedence | Explicit false branches; synthetic probe tests |
| Initial source contained a fixed canary literal | Runtime-generated canary; proofs assert value absent; no real key involved |
| Raising the draft stat refusal broke pytest collection via `is_dir → stat` (exit 4) | Uniform `FileNotFoundError` semantics without executing the protected stat |
| Child coverage incorrectly attributed pathlib reads to `builtins.open` | Added `io.open`, `os.open`, pathlib routes and attempted per-route proof |
| Child protection/network status was asserted only after payload execution | Observed refusals, unconditional exit 125, and preflight gate before payload |
| Failed diagnostic writing could bypass abort | Logging failure is caught; absent proof-directory injection still exits 125 with no payload |
| Missing or invalid sitecustomize is ignored by Python | Preflight absence/syntax tests reject launch through actual launch helpers |
| Installer preflight initially missed the wrapper's PYTHONPATH (targeted exit 1) | Mirrored wrapper import environment; installed-wrapper targeted exit 0 |
| Initial wrapper gate covered first run but not resume | Shared launch helper and independent wrapper negative tests |
| Missing copied package could fall back to editable checkout | Active-guard provenance mismatch rejects before wrapper/job mutation |
| Changed child cwd made a relative-status absence assertion vacuous | Assert absence in actual child cwd and prior location |
| Guard installation followed project imports | Installation now precedes those imports; localized E402 annotations |

## Unsafe initial RED: exact disclosure and confidence limit

GLM's first RED command was the whole module, before the conftest guard was wired:

```text
.venv/bin/python -m pytest tests/test_env.py -q > /tmp/stab-test-isolation-red-parent.log 2>&1
```

This is a historical reproduction record, **not a command to rerun**. Cwd was the live checkout. The worker reported original exit 1 and seven failures. Its selected module included real-root lookup tests and `api_key_source`, as well as calls through `get`/`is_offline`/`require_api_key` to `ensure_loaded`. These can search cwd ancestors and the real package fallback and can call `load_env` on a discovered file. This violated the brief's synthetic-only pre-guard RED requirement.

Exact invocation time is unavailable in the accepted evidence. Worker-reported log birth/mtime was **2026-09-27 17:09:56 KST**; file metadata is not an execution timestamp. Sol did not open the raw RED log or investigate real protected-file existence/content. Actual protected reads remain unknown. Do not use this run as protected baseline evidence, and do not infer absence of access from the later guarded suite.

The worker scratch evidence incorrectly said `load_env` reads were only against temporary fixtures. That categorical statement is rejected here: direct fixture calls were synthetic, but indirect `ensure_loaded → load_env` calls are not thereby excluded. Sol's correction crossed settlement and the send was rejected with `dispatch_inactive`; this durable Sol review is authoritative. No fresh Dispatch or test run was needed for that documentation correction.

The later worker-authored child demonstration used a synthetic fake CLI and reported `pre_fix_reached: true`, `post_fix_reached: false`, exit 0. It does not erase the unsafe selection. Final acceptance rests on the independently executed guarded regression suite, bootstrap failure injection, provenance records and aggregate.

Parent was notified in `msg_e4f0765a6c12`; it confirmed user disclosure in `msg_54ea56de0b88`. Scope violations and mailbox handling are recorded in the [feedback report](../../reports/2026-09-27-high-agency-implementation-feedback.md).

## Residual limits and next actions

This is a cooperative Python test harness, not an OS security sandbox. Directory enumeration can reveal names; `os.access`/`os.readlink`, existing file descriptors, innocent-named aliases/hardlinks, child `Path.resolve`, and arbitrary native consumers are not comprehensively mediated. Synthetic copies avoid source links. Zsh wrappers are controlled structurally. Preflight assumes its test-owned files/environment are not maliciously changed between verification and payload launch. The suite's other subprocess tests cover native tools or public module imports, not the targeted dotenv-loading CLI path; they do not inherit a universal process sandbox.

These limits do not block the covered prerequisite. A new dotenv-loading subprocess must use this guarded launch setup and prove import/cwd/config provenance before broad testing. If future tests need alias, native, or concurrent-mutator coverage, plan that bounded harness work first; do not infer protection from this report. Earlier S1/S2A-1 runs retain their prior access uncertainty. The stale explanatory comments in the unchanged Swift bridge remain comment debt.

External acceptance remains unperformed: packaged engine/FFmpeg supply, signing/notarization, paid provider calls, real-media timing/retention targets, and Final Cut roundtrip. The prior `scripts/build_engine.py --verify` exit 1 for missing `macos/Resources/Engine/engine-manifest.json` was not rerun. Remaining authorized implementation is S2A-2/3, S2B/C, S3–S5; this Dispatch stops before S2A-2.

## Lifecycle and skill evidence

- Sol: `task_ad2785c1b0f5` / `ctx_debf6903938c`; owned child Run `run_5426631698bf`.
- GLM: `task_4868e56aba84` / `ctx_f95bd6962daf`, exact launcher `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`, terminal `term_a0468c83-d4e2-46ce-9927-b9af836691a3`, incarnation `a6a4a7c8-e2ed-41b4-aac8-2ceff688d6c3`.
- Direct High Agency Read observed at transcript timestamp `1790495802085`, before edits. Immutable estimate `propagating | files<=6 | modules<=2 | boundary=private`; actual six test files, same private boundary. The user-required two-role workflow remained in force.
- Done `msg_e66425299357` at 08:36:54 UTC; worker-show confirmed completed/succeeded. Exact TUI idle was proven. Release retained the external terminal without process action; authorized exact close returned `ptyKilled:true`. Reclaimable query returned no workers.
- One worker ack was truncated with `head -8`, hiding the newly returned delivery `delivery_7d329b6fc536`. Sol required a complete replay/process/ack; later evidence records acknowledgment. Heartbeats were not treated as acknowledgments.
- Sol corrected `run-show --run` to supported `--id` after exit 1, and a duplicate actionable waiter returned `waiter_exists`; the existing waiter was preserved. No duplicate worker launched.
- Latest user routing now assigns easy work to Luna max, complex planning/acceptance to Sol high, and substantial implementation to GLM at task-appropriate effort. This active GLM run was not interrupted or changed. Parent owns idle model selection/standalone compact after settlement; selected client model does not prove a hidden compaction model.
