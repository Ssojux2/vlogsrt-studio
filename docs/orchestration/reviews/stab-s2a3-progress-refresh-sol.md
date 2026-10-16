# S2A-3 progress refresh — accepted offline

Accepted code: `320dff4aef31643a4c3bbeb05779f3f207f53786`. This completes S2A-3 progress transport and extension refresh for offline acceptance. S2B budget/cache, S2C import lifecycle and S3–S5 remain pending. The refined [S2A-3 brief](../tasks/stab-s2a-3-progress-refresh.md) defines the bounded scope.

## Result and independent review

- The implementation stayed within the original propagating / 18-file / 5-module / shared-API limit: 14 files, 3,366 insertions and 36 deletions. The development installer was explicitly allowed. No scope increase was needed.
- The Python producer publishes atomic progress snapshots at actual analysis boundaries. A targeted golden test passed against the Swift fixture digest `d2df…`; it exercises the producer output, not a parallel helper.
- Both engine transports relay progress during the existing invocation: the bundled entrypoint and the development `EngineSessionRunner` path. Relay ownership is tied to the invocation, matching bytes are handled across the relay race, and candidate/roughcut artifacts remain behind existing final-sync and validation gates.
- The Swift wire reader validates typed progress and digest data, binds terminal state to the active job/input/attempt/estimate, and keeps the legacy retry baseline. The actual extension refresh model is connected to `WorkflowViewController`; late open/approval callbacks are bound to the current generation, and selection state follows the accepted candidate revision.
- Refresh uses cadence-limited read-only polling. It continues to display the last known running stage while the view is active during a long unchanged stage. A matching terminal record takes precedence. Missing or finalizing records wait only for the bounded interval. There is no heartbeat or other proof of actual process liveness.
- No new engine invocation is used to refresh state. Estimate-only completion can become ready for approval without approving or starting paid work. Legacy, missing and malformed records remain conservative.

## Final evidence

| Check | Result | Evidence |
|---|---|---|
| Final `make check` | Exit 0; 2,885 Python passed in 68.89s; Ruff/format on 127 files; mypy on 55 files; all 21 invariants | `/tmp/stab-s2a3-sol-make-check.log` |
| Full Swift suite | Exit 0; 167 core + 1 extension = 168 tests; core 10.837s | `/tmp/stab-s2a3-sol-swift-full.log` |
| Unsigned native build | Exit 0; `BUILD SUCCEEDED` | `/tmp/stab-s2a3-sol-native.log` |
| Uncovered script checks | Engine Ruff, format, `py_compile` and installer `zsh -n`: all exit 0 | Sol’s final-gate record |
| Python producer golden test | 1 passed in 0.07s; actual producer output matches the Swift fixture | `/tmp/stab-s2a3-sol-wire.log` |
| Owned source hashes | All 14 matched before and after gates and after commit | `/tmp/stab-s2a3-sol-source-final.sha256` |

The guard summary covers 21 files and 61 records, including intentional refusal cases. It is not universal proof of no access. Final gates and the committed source hashes are Sol-owned evidence; no runtime checks were repeated for this documentation task.

### Reproduction commands

Sol used this literal clean-environment prefix for the controlled commands below:

```sh
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8
```

The aggregate, Swift and native commands were:

```sh
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 VLOGSRT_OFFLINE=1 PYTEST_ADDOPTS=--basetemp=/tmp/stab-s2a3-sol-aggregate /usr/bin/make check
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /usr/bin/swift build --package-path macos --build-tests
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -I /tmp/stab-s2a3-sol-watchdog.py 150 /usr/bin/swift test --package-path macos --skip-build
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /usr/bin/xcodebuild -project macos/FinalCutAgent.xcodeproj -scheme FinalCutAgent -configuration Debug -derivedDataPath /tmp/stab-s2a3-sol-derived CODE_SIGNING_ALLOWED=NO build
```

The four uncovered script checks were separate calls under the same prefix. The Python golden test also used the prefix and offline flag:

```sh
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/ruff check macos/scripts/vlogsrt-engine.py
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/ruff format --check macos/scripts/vlogsrt-engine.py
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -m py_compile macos/scripts/vlogsrt-engine.py
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /bin/zsh -n macos/scripts/install-development-engine.sh
/usr/bin/env -i HOME=/tmp/stab-s2a3-sol-home TMPDIR=/tmp/stab-s2a3-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 VLOGSRT_OFFLINE=1 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -m pytest -p tests.conftest /tmp/test_stab_s2a3_sol_wire.py -q --basetemp=/tmp/stab-s2a3-sol-wire
```

## Failures resolved before acceptance

| Stage | Observed result | Resolution and final state |
|---|---|---|
| Meaningful baseline RED | Python reached three missing-progress assertions and exited 1. Swift built, then the intermediate relay failed after 2.028s; bundled control passed. | These failures established the missing transport behavior before edits. Both transport paths are covered by final evidence. |
| Initial test-harness draft | It lacked synthetic dotenv/cwd setup, used `NSLock.lock` from async source, had a continuation registration/release race, and used a bundled plan in a development fixture. | At Sol’s request, GLM corrected the source before Sol executed any runtime check. These were harness defects, not observed application crashes. |
| Swift builds 1–7 | B1: public default arguments exposed internal helpers and an extra `isStopped` argument. B2: non-Sendable registry capture. B3: stale `WorkflowRefreshTransition` type name. B4: `Configuration` initializer mismatch. B5: two test `Task` isolation captures. B6: `Data(String)` and argument order. B7: wait-helper actor transfer. | Each build exited 1. Build 8 (`/tmp/stab-s2a3-sol-swift-build-eighth.log`) exited 0 in 4.81s. B1–B7 logs are `swift-build-first.log` through `swift-build-seventh.log`. These were compiler/build failures, not live-app crashes. |
| Intermediate 25-test run | 24 passed, 1 failed because the unchanged-running counter reset on every observation, triggering two assertions. | A separate unchanged-running counter fixed the state; the corrected test passed. |
| Subsequent 26-test run | 25 passed, 1 failed because the approval fixture was wrong. | The corrected approval test passed. The full 168-test Swift run later passed. |

Static corrections covered invocation ownership, a same-bytes relay race, identity binding, typed wire/digest handling, legacy retry behavior and late UI callbacks. No unresolved scoped implementation or test failure remains in the supplied final evidence.

## Skill, model and concurrency evidence

The source worker used `claude-glm --model 'glm-5.3[1m]' --effort high --permission-mode auto` as the single direct writer for the substantial multi-module implementation. Its reported preflight was high complexity, propagating / files<=18 / modules<=5 / shared-api. Root observed the immutable preflight and High Agency skill-read acknowledgment before product mutation. Message `msg_230cced5d547` reported the path `/Users/ssojux2/.codex/plugins/cache/high-agency/high-agency/0.10.1/skills/high-agency-coding/SKILL.md` and the Impact. This records the observed worker report; it does not prove remote provider identity or automatic hook coverage.

The compiler-only `UserDefaults: Sendable` probe exited 1 because the type is not Sendable. It did not construct a `UserDefaults` object. The narrow private immutable read-only probe was justified by Apple's documented same-instance thread safety ([UserDefaults documentation](https://developer.apple.com/documentation/foundation/userdefaults)). Sol declined expanding `ActiveProject.swift` Sendable/is-current APIs; the probe disproved the original Sendable premise.

## Scope, lifecycle and limits

The exact GLM source commit is `320dff4aef31643a4c3bbeb05779f3f207f53786`. Sol owned runtime validation and independent acceptance; GLM supplied source and static tooling. The 14 source hashes matched the recorded values after gates and commit.

Implementation task `task_d1e19e973e52` / Dispatch `ctx_31aa6dd40b0d` settled succeeded through `msg_e50f1d4fe9a9`; delivery `delivery_879fa8418439` was acknowledged. Release retained `external_terminal`. Terminal `term_80f8f7d9-60d4-4a22-be0d-85ea765ff4e3`, incarnation `9edd3f78-8b72-461c-8c7c-96ff06e5b9b4`, was verified idle and closed with `ptyKilled=true`.

This is offline acceptance only. No real provider, original media, Keychain, Final Cut Pro, signed sandbox or packaged engine supply was exercised or accepted. The earlier unsafe environment-dump uncertainty remains unresolved; later green evidence does not establish what was accessed earlier. The next authorized stage is [S2B budget/cache](../tasks/stab-s2b-budget-cache.md), after this documentation checkpoint and ROOT maintenance gate.
