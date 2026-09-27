# S2A-2 final preflight — accepted offline

Accepted code: `37a7bc5fb9b319f8e09df1664cd1706462ee355f`, 25 explicitly owned product/test/Xcode paths, over credential foundation `f56545b` and role credentials `c96e0d5`. This completes the feasible offline S2A-2 implementation and validation. S2A-3, S2B/C and S3–S5 remain pending. The [final brief](../tasks/stab-s2a2-final-preflight.md) and [S2A-2 contract](../tasks/stab-s2a-2-credentials-preflight.md) define the scope.

## Result and independent review

- The actual `main.swift` caller uses `EngineRequestCoordinator` inside the existing per-job request gate. It validates input/selection, the selected executable, operation-specific credentials, granted media, workspace/output writability and actual child tool readiness. Release rejects a missing bundle before consulting development workspace settings; bundled execution does not stage an unused development workspace.
- Bookmark access separates URL resolution from explicit metadata validation/stale refresh. The latter runs after scope acquisition. Canonical scope deduplication, partial-acquisition cleanup, successful lifetime through execution/sync and exactly-once release are covered through the actual dependency assembly with fake bookmark/scope primitives. Native bookmark resolution itself is not claimed to be pure computation or externally accepted.
- Both launchers cross the real copied Python entrypoint for the narrow readiness operation. Its bounded invocation-owned report carries public interpreter/tool metadata and creates no job workflow/lock/workspace state. The bundled test preserves the production executable layout, PATH, cwd and V2 carrier; canonical tool identities must match the bundle. Missing tools, broken launches and malformed reports fail with corrective messages.
- Actual referenced media are probed for audio **streams**, including misleading `hasAudio=0` declarations. Multiple streams, malformed classifications and probe failures fail closed; zero and single-stream inputs remain supported. Stereo is not mistaken for multiple streams. Paid/resumed analysis rechecks prerequisites, and failed inputs cannot produce new destructive candidates or provider calls.
- V2 paid execution validates both role credentials and endpoint before provider construction and uses the separate vision values in the actual staged consumer. Markerless/V1 staged CLI execution preserves its existing shared-key/backend contract. Estimation and roughcut remain key-free. New preflight diagnostic failures use the existing secret redaction with a fixed safe fallback.
- Swift writes `grant_revision`; both languages retain legacy camelCase reads. Coverage and fingerprints preserve exact Unicode/space-containing root strings. Actual Swift-produced grant bytes cross the Python consumer. The inherited `env.py` → project-config dependency was removed without changing invariant checks: config resolution belongs to the engine boundary, and the low-level environment setter receives resolved names. Its internal helper signature changed coherently; actual default/custom-name, malformed-config and carrier behavior is tested.
- Existing ownership, cancellation, selection drift, diagnostic-only failure sync and import protections remain covered. The new Swift source is registered in the checked-in Xcode project. No progress, budget, caption or import lifecycle feature was added.

## Final evidence

Sol executed all runtime acceptance. Commands used literal `/usr/bin/env -i` environments, synthetic HOME `/tmp/stab-s2a2-preflight-sol-home`, TMPDIR `/tmp`, locale `en_US.UTF-8` and absolute interpreters/tools. Python retained the accepted dotenv/network guard. New child harnesses were reviewed before execution, with guard-negative checks preceding positives. Swift child tests used an external 150-second process-group watchdog.

| Check | Original result | Evidence |
|---|---|---|
| Final `make check` | **0; 2,868 Python passed, 66.70s**; Ruff/check-format, mypy 55 files, all 21 invariants | `/tmp/stab-s2a2-preflight-sol-make-check-final.log` |
| Final full Swift | **0; 141 core + 1 extension = 142 total**, core 10.932s | `/tmp/stab-s2a2-preflight-sol-swift-final.log` |
| Swift test-target build | 0; 3.06s after final test correction | `/tmp/stab-s2a2-preflight-sol-swift-build-last.log` |
| Corrected Swift readiness selection | 0; 5 passed, 1.121s | `/tmp/stab-s2a2-preflight-sol-swift-target-final.log` |
| Guard startup/refusal selection | 0; 1 Swift test, 2.079s; 3 Python wrapper tests, 0.24s | `/tmp/stab-s2a2-preflight-sol-swift-guard.log`, `/tmp/stab-s2a2-preflight-sol-guard.log` |
| Real installed-wrapper/topology selection | 0; 8 passed, 7.50s | `/tmp/stab-s2a2-preflight-sol-wrapper-second.log`; included again in aggregate |
| SDK-reached and redaction regression | 0; 1 passed, 0.54s | `/tmp/stab-s2a2-preflight-sol-redaction-reached.log` |
| Fresh unsigned native app/extension | 0; BUILD SUCCEEDED | `/tmp/stab-s2a2-preflight-sol-native-final.log` |
| Engine Ruff/check-format/py_compile; installer syntax | all 0 | `/tmp/stab-s2a2-preflight-sol-script-final.log` |
| Owned diff check and source pinning | 0; all 25 hashes matched before/after verification and after commit | `/tmp/stab-s2a2-preflight-sol-source.sha256` |

Reproduction after the clean-environment prefix:

- `/usr/bin/make check`, with PATH `/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin` and `PYTEST_ADDOPTS=--basetemp=/tmp/stab-s2a2-preflight-sol-aggregate-final`.
- `/usr/bin/swift build --package-path macos --build-tests`, then `/usr/bin/swift test --package-path macos --skip-build` under the watchdog.
- `/usr/bin/xcodebuild -project macos/FinalCutAgent.xcodeproj -scheme FinalCutAgent -configuration Debug -derivedDataPath /tmp/stab-s2a2-preflight-sol-derived-final CODE_SIGNING_ALLOWED=NO build`.
- `.venv/bin/ruff check macos/scripts/vlogsrt-engine.py`; `.venv/bin/ruff format --check macos/scripts/vlogsrt-engine.py`; `.venv/bin/python -m py_compile macos/scripts/vlogsrt-engine.py`; `/bin/zsh -n macos/scripts/install-development-engine.sh`.

Final aggregate artifacts contain 21 guard-proof files / 61 records, including intentional refusal cases. A positive copied-source example has cwd `/private/tmp/stab-s2a2-preflight-sol-aggregate-final/test_real_engine_probe_failure0/synthetic repo` and imports its own `vlogsrt/__init__.py`, with protected-read/socket/connect refusals true. Swift asserts per-payload provenance before deleting its synthetic fixtures. Summary: `/tmp/stab-s2a2-preflight-sol-provenance.json`. Counts alone do not prove every record is a successful payload: editable-fallback records intentionally demonstrate launch refusal.

## Failures resolved before acceptance

| Stage / repro | Expected versus observed / original exit | Correction and outcome |
|---|---|---|
| Initial Swift filter `debugFallbackRequiresAnExecutableDevelopmentWrapper\|mediaGrantDocumentWireJSONCarriesThePythonConsumedKey` | Build succeeded; both behaviors failed, exit 1: absent wrapper produced development plan; actual grant lacked snake_case key | Meaningful pre-edit RED, then product fixes; final suite passes |
| Swift builds, same `--build-tests` command | Exits 1: duplicate arguments; non-Sendable store capture; actor-crossing context; modifier position; merged statements; explicit self; test locking/URL/capture/encoding errors | Corrected serially; explicit caller isolation follows [Swift SE-0461](https://github.com/swiftlang/swift-evolution/blob/main/proposals/0461-async-function-isolation.md), preserving asynchronous backend execution; build/full suite pass |
| Combined six-module Python selection | 77 pass / 31 fail, exit 1: V2 test leaked process role marker; next round 110 pass / 2 fail from overwritten synthetic endpoint and obsolete fake grant | Restore test-owned state and fix fixtures, preserving assertions; corrected selectors and aggregate pass |
| First full Swift | 141 core + 1 extension ran; exit 1 with five failing tests / 14 issues | Fixed Release sequencing, readiness error wrapping, valid bundled tools, explicit-nil fixture and physical-path assertions; final 142 pass |
| First `make check` | make exit 2 / pytest exit 1; 2,862 pass / 5 fail, 65.75s; three carrier prerequisites and two architecture invariant failures | Explicitly approved scope increase; fixed dependency direction and synthetic prerequisites, no invariant exemption; final aggregate passes |
| Targeted env/carrier/invariant selection | 319 pass / 12 fail, exit 1: missed renamed variable plus SDK fixture stopped at preparation; then 330 pass / 1 fail | Correct identifier, use synthetic preparation, retain redaction checks and add SDK-reached assertion; final regression passes |

Logs use `/tmp/stab-s2a2-preflight-sol-` prefixes: `red`, `swift-build` through `swift-build-eleventh`, `python-second`, `python-third`, `swift-full-first`, `make-check`, `aggregate-corrections` and `aggregate-corrections-second`. The failed aggregate justified one retry after corrections; no unchanged broad suite was repeated. Initial failure evidence is not a claim that it remains reproducible on the accepted commit.

## Scope, lifecycle and limits

Immutable Impact was propagating / 22 files / 6 modules / shared API. Sol approved a bounded increase to 25 files / 7 modules in `msg_789f35095768` for `vlogsrt/env.py`, carrier tests and env tests. Final commit contains exactly those 25 paths (4,993 insertions / 275 deletions); protected files and Sol docs are absent from it. One exact GLM 5.3[1m] high writer supplied source; Sol supplied briefs, review and every runtime command. Process issues and actual High Agency use are recorded in the [feedback report](../../reports/2026-09-27-high-agency-implementation-feedback.md).

Child `task_8d2591fb88d9` / `ctx_daff7e378c65` settled succeeded through `msg_ea85e9c57aec` at 14:26:52 UTC. Release returned external retention; exact terminal `term_0c56a97a-387c-4fd7-9e2d-322547da1af5`, incarnation `587fd828-9004-4ebe-8b9d-ac7348464556`, was idle-verified and closed (`ptyKilled=true`). Reclaimable workers are empty. Final shared docs were written only after closure.

No unresolved scoped implementation/test error remains. Native UI, real Keychain replacement, native bookmark/stale-refresh behavior in the signed sandbox, packaged binary supply, paid providers, original media, Final Cut roundtrip and signing/notarization remain unperformed external acceptance. Cooperative Python guards are not an OS sandbox; descriptor/alias/native-consumer/system-zshenv limits remain, and the watchdog does not prove control over independent XPC children. Earlier unit1/role/isolation incidents retain their recorded uncertainty; later green tests do not establish earlier no-access. Next is a fresh, bounded S2A-3 dispatch after parent compact; this acceptance does not complete the overall plan.
