# S2A-2 unit1 — credential foundation accepted offline

Accepted code: `f56545b57cb5eac55347fe00dbadc4dd08560e1c` (20 explicit product/test/config paths). This accepts the existing engine/STT credential foundation only. Separate vision credentials, final truthful preflight and multi-audio rejection remain pending; S2A-2 as a whole is not complete.

## Result and review

- `main.swift` uses a secure input field and the fake-testable credential model. Empty input is rejected, successful save clears submitted text, and storage failures use fixed status text. Existing Keychain service/account remain; update changes only the value attribute and preserves the prior item on failed replacement. No real Keychain operation was used for acceptance.
- Development uses the real `NSUserUnixTask` readable stdin handle. A bounded nonblocking prefill queues the frame before execution and closes each end once; partial writes/interruption/error cases are tested without assuming pipe capacity. Bundled invocation carries explicit presence/absence in its per-invocation environment. No parent-global environment change or key-valued argv is introduced.
- Python accepts legacy markerless calls, applies an authoritative carrier before SDK/config use, and distinguishes absence from an explicit empty value so stale inherited/dotenv keys cannot return. The frame bounds payload/header sizes, rejects NUL/invalid UTF-8/trailing input and uses one deadline through clean EOF.
- SDK 400 retry warnings, transcription/boundary failure captures and persisted analysis-attempt failure text redact the submitted engine key; configured vision failure text also has a bounded redaction helper. Separate vision-key transport and complete two-role error coverage belong to the next authorized unit.
- Both actual launcher classes cross the copied actual Python entrypoint and production carrier application before a fake SDK factory observes the key. Only downstream analysis is substituted; expected values are digests and evidence is boolean-only. Tests cover replacement, explicit empty, concurrent separate jobs, legacy status, and missing/broken guard refusal before payload.
- The existing request gate, cancellation, revision drift, duplicate status and diagnostic/import gates remain green. New Swift source registration is present in the checked-in Xcode project.

Apple's [script-attachment guidance](https://developer.apple.com/forums/thread/794355) explicitly supports stdin/stdout/stderr pipes for `NSUserUnixTask`. The tiny actual-API test proves this implementation's transport on the current runtime; neither the source nor later success uniquely proves the earlier exception's cause.

## Independent evidence

All Python commands used `env -i` with literal PATH `/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`, synthetic HOME `/tmp/stab-s2a2-sol-test-home`, TMPDIR `/tmp`, locale and the absolute repository `.venv/bin/python`. Swift used literal system PATH and the same synthetic HOME/TMPDIR, with an external 90–150-second watchdog on the owned test process group. Original exits were captured before summaries. Test cases use injected storage, synthetic media/configuration/keys and network refusal.

| Check | Original exit / result | Evidence |
|---|---|---|
| Carrier file first independent run | 1; 24 pass, malformed-envelope rejection failed | `/tmp/stab-s2a2-sol-carrier-first.log` |
| Carrier after buffer/fragment repair | 0; 26 passed, 1.25s | `/tmp/stab-s2a2-sol-carrier-final.log` |
| Tiny `credentialEnvelopeBytesCrossTheStdinPathWithoutPython` | 0; 1 test, 0.501s; real transport 0/2/legacy0 | `/tmp/stab-s2a2-sol-tiny-transport.log` |
| Initial guarded Swift selection | 1; 12 tests, 4 issues (partial writer and three guard canaries) | `/tmp/stab-s2a2-sol-swift-bridge-first.log` |
| Shared isolation + carrier after final header patch | 0; **94 passed, 7.53s** | `/tmp/stab-s2a2-sol-isolation-last.log` |
| Full Swift after wait-helper repair | 0; **94 core + 1 extension = 95**, core 7.362s | `/tmp/stab-s2a2-sol-swift-accepted.log` |
| Post-commit affected bridge refresh at `f56545b` | 0; **4 passed, 3.524s** | `/tmp/stab-s2a2-sol-committed-bridge.log` |
| Earlier bridge refresh | 1; negative-guard fixture installer wait timed out at 35.607s | `/tmp/stab-s2a2-sol-swift-bridge-last.log` |
| Unsigned native app/extension build | 0; BUILD SUCCEEDED | `/tmp/stab-s2a2-sol-native.log` |
| Engine-script Ruff check/format and installer `zsh -n` | 0 under fail-fast shell | Same final script state; no uncovered script error |

The 94-test command was:

```sh
.venv/bin/python -m pytest -o addopts= -q tests/test_env.py tests/project/test_engine_entrypoint.py tests/project/test_engine_execution_lock.py tests/project/test_development_engine_installer.py tests/project/test_engine_carrier.py --basetemp=/tmp/stab-s2a2-sol-isolation-last
```

Swift command: `/usr/bin/swift test --package-path macos --skip-build`; the worker had rebuilt tests after relevant edits. Native command: `/usr/bin/xcodebuild -project macos/FinalCutAgent.xcodeproj -scheme FinalCutAgent -configuration Debug -derivedDataPath /tmp/stab-s2a2-sol-derived CODE_SIGNING_ALLOWED=NO build`.

The last post-suite edit guards a test helper's termination-status read until the owned process has completed and corrects timeout comments. GLM rebuilt and passed the four affected bridges in 4.177s after that edit; Sol reviewed the exact final diff and independently passed the committed bridge selection in 3.524s. No aggregate `make check` has run for this unit; one aggregate remains scheduled after the final S2A-2 unit.

## Provenance, failures and confidence limits

The Python basetemp retains 17 proof files/52 provenance records. Positive children record exact synthetic cwd/copied package paths and read/probe/socket/connect refusals. The deliberate missing-package case records the public editable `vlogsrt/__init__.py` and rejects the payload; it is not a successful copied-package execution. Swift performs same-interpreter guard checks and exact cwd/import assertions before payload; its per-job proof files are temporary and cleaned by tests.

The shared guard's socket replacement changed from a function to a non-instantiable class so `ssl` can import without opening a socket; constructor and connection refusals remain tested. Sol approved this bounded compatibility adjustment and reran the shared isolation surface. It remains a cooperative Python harness, not an OS sandbox; existing descriptor/alias/native-consumer limitations remain. `zsh -f` suppresses ordinary startup files, but the system zshenv boundary is disclosed and uninspected.

Resolved findings include buffered trailing-data acceptance, incomplete-header rejection, missing completed-header bound, inverted/premature stdin-handle handling, a partial-write fake that wrote no bytes, physical path spelling, missing synthetic bundled cwd, SDK import compatibility, binary-safe durable-key scans, and a flaky test Process wait. The new wait helper installs its termination observer before launch; worker repetitions and Sol's full suite passed afterward. A unique Foundation root cause is not claimed. Swift task-group cancellation is not a hard XPC deadline; acceptance used an external process-group watchdog, and an XPC child is not thereby proven to share that process group.

The [first-attempt incident report](stab-s2a2-unit1-attempt1-sol.md) remains authoritative: unguarded bridge attempts and an ambient-environment dump/read occurred, with credential presence unknown. Later passing results do not establish earlier no-access. Retry carrier reruns initially used a bare command despite the requested clean environment; their launch provenance is not inferred from Sol's explicitly controlled runs. No retrospective protected-file/dump inspection was performed. Generic local Bash permission denials have an unknown policy source; later status bodies succeeded.

## Lifecycle and remaining work

Retry `task_418804c480c4` / `ctx_678b247aef31` settled succeeded via `msg_6f43d7203c7a` at 11:04:12 UTC. Exact terminal `term_20fc7eca-c998-4baf-9bdd-1a0a83bf2e62`, incarnation `15149761-53d4-40ee-8095-22f47beb1a64`, was released (external ownership), proven idle and closed with `ptyKilled=true`; reclaimable query was empty. No live GLM remains.

Parent approved separate role-specific Keychain credentials and a versioned private paired carrier via `msg_9aa93afe98b7`, preserving provider separation and key-free estimate/roughcut. The next unit must also account for the existing required vision base URL: `_vision_credentials` refuses an absent configured endpoint before analysis, preventing a Google key from silently going to the public OpenAI endpoint. Refine that configuration path without reading real settings/secrets or guessing a provider switch.

Packaged-engine/FFmpeg supply, actual Keychain/UI operation, signing/notarization, paid providers, original-media and real FCP acceptance are not established by these tests or the unsigned build. S2A-3, S2B/C and S3–S5 also remain pending. Per parent's explicit context-maintenance instruction, Sol's `task_35ab3e395efb` / `ctx_9dcc7d74611c` stays active; no parent `worker_done` is sent at this checkpoint.
