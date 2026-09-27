# S2A-2 unit2 — separate role credentials accepted offline

Accepted code: `c96e0d5f1d99d2ec073d1f44523d5841db8be138`, exactly 17 owned product/test/Xcode paths over credential foundation `f56545b`. This accepts separate role credentials and explicit endpoint setup. Truthful preflight, unsupported multi-audio rejection and final aggregate verification remain pending; S2A-2 is not complete.

## Result and review

- The actual app uses independent STT/vision secure fields, save/clear actions and status through injected-storage models. Existing Keychain service and `engine` account remain; `vision` and `vision-base-url` are distinct accounts. Endpoint configuration persists across restart through the same storage abstraction; tests use fakes exclusively. No endpoint preset or provider alias was introduced.
- The endpoint is validated on save and against current credentials before launch. Saving includes stored and unsaved key material and fails closed if credential reads fail. App-managed Python execution validates again before assignment and provider construction. Fixed errors reject malformed/non-HTTPS URLs, credential-bearing URL parts, invalid ports, controls/whitespace, and known raw/decoded/trimmed key material without echoing the endpoint. Legacy direct CLI/V1 endpoint semantics remain unchanged.
- V2 carries exactly `stt_key`, `vision_key`, `vision_base_url`, bounded to 8192 encoded payload bytes. Development retains the private bounded stdin frame; bundled execution has an explicit per-invocation carrier. Each empty field is authoritative absence. Duplicate/unknown/missing/wrong-type fields, invalid UTF-8/NUL, escaped lone surrogates, oversized frames and conflicting carriers fail closed. V1 and markerless readers, descriptor/deadline/EOF behavior and per-job ownership remain.
- Configured legal vision credential names remain supported, with ambiguous STT/colliding/execution-sensitive names rejected before assignment. Both raw and provider-trimmed key values are redacted longest-first. Configuration errors are controlled failures with submitted-value sanitization; status delivery, lock release and retry are covered.
- Actual development and bundled launcher tests cross copied production entrypoint/carrier application and production STT/vision factories. Only downstream SDK constructors and analysis are substituted; factory expectations use digests and results use booleans. Development tests prove independent role absence; bundled tests prove paired values and explicit all-empty refusal. Existing V1 and ownership/cancel/drift/diagnostic/import tests remain green.
- New Swift source is registered in the checked-in Xcode project. No dependency, progress, budget, import lifecycle or general media-preflight feature was added.

## Independent controlled evidence

Sol executed every acceptance command. Commands used `/usr/bin/env -i`, literal PATH, synthetic HOME `/tmp/stab-s2a2-role-sol-home`, TMPDIR `/tmp`, locale where needed, and the absolute repository `.venv/bin/python` or system Swift/Xcode tools. No environment dump/copy was used. Pytest retains the accepted conftest dotenv guard and network blocking. Swift child runs used an external 120–150-second watchdog on the owned test process group; this does not prove an independent XPC/launchd child shares that group.

| Check | Original exit / result | Evidence |
|---|---|---|
| Initial carrier/environment selection | 0; 106 passed, 1.46s | `/tmp/stab-s2a2-role-sol-python-first.log` |
| Initial affected entrypoint/lock/installer/project selection | 0; 52 passed, 7.18s | `/tmp/stab-s2a2-role-sol-python-affected.log` |
| Final combined Python selection | 0; **161 passed, 8.03s** | `/tmp/stab-s2a2-role-sol-python-final.log` |
| Initial Ruff | 1; three E501 and two B017 findings | `/tmp/stab-s2a2-role-sol-ruff-first.log` |
| Initial Swift test build | 1; assertion comment type and fake private-setter errors | `/tmp/stab-s2a2-role-sol-swift-build-first.log` |
| Corrected Swift test build | 0; 4.24s | `/tmp/stab-s2a2-role-sol-swift-build-final.log` |
| Pure Swift model selection | 1; 12 pass, one reversed rotation-fixture expectation | `/tmp/stab-s2a2-role-sol-swift-models.log` |
| Corrected rotation + missing/broken guard refusal | 0; 2 passed, 2.197s | `/tmp/stab-s2a2-role-sol-swift-guard.log` |
| Both actual V2 launcher/provider bridges | 0; 2 passed, 3.165s | `/tmp/stab-s2a2-role-sol-swift-bridges.log` |
| Full Swift suite | 0; **113 core + 1 extension = 114**, core 8.677s | `/tmp/stab-s2a2-role-sol-swift-final.log` |
| Fresh unsigned native app/extension build | 0; BUILD SUCCEEDED | `/tmp/stab-s2a2-role-sol-native.log` |
| Final Ruff/check-format, targeted mypy, py_compile, installer syntax and owned diff check | all 0 | Commands below |

The final Python command, after the clean-environment prefix, was:

```sh
/Users/ssojux2/Working/vlogsrt/.venv/bin/python -m pytest -o addopts= -q tests/test_env.py tests/project/test_engine_carrier.py tests/project/test_engine_entrypoint.py tests/project/test_engine_execution_lock.py tests/project/test_development_engine_installer.py tests/project/test_project_cli.py tests/project/test_project_e2e.py --basetemp=/tmp/stab-s2a2-role-sol-python-final
```

The only later Python edits were GLM's explicit-path Ruff formatting, with exit 0 and subsequent independent lint/format checks green. Behavioral evidence remains valid; the final real Swift bridges also imported those formatted Python sources. No repeated broad Python suite was needed.

Swift: `/usr/bin/swift build --package-path macos --build-tests`, then `/usr/bin/swift test --package-path macos --skip-build`. Before the full suite, filters selected `unguardedChildrenNeverReachThePayload|roleCredentialStoreRevalidatesTheEndpointAgainstCurrentKeys` and `roleCredentialEnvelopeCrossesTheRealDevelopmentBridge|bundledRoleCarrierReachesTheLaunchedConsumer`.

Native: `/usr/bin/xcodebuild -project macos/FinalCutAgent.xcodeproj -scheme FinalCutAgent -configuration Debug -derivedDataPath /tmp/stab-s2a2-role-sol-derived CODE_SIGNING_ALLOWED=NO build`.

Static/script commands: `.venv/bin/ruff check` and `.venv/bin/ruff format --check` on `vlogsrt/env.py`, `vlogsrt/project/commands.py`, `macos/scripts/vlogsrt-engine.py`, `tests/test_env.py`, `tests/project/test_engine_carrier.py`; `.venv/bin/mypy vlogsrt/env.py vlogsrt/project/commands.py`; `.venv/bin/python -m py_compile macos/scripts/vlogsrt-engine.py`; `/bin/zsh -n macos/scripts/install-development-engine.sh`; explicit-owned-path `git diff --check`. One final aggregate `make check` remains reserved for final S2A-2 preflight acceptance.

## Provenance and resolved findings

The final Python basetemp retains **17 proof files / 53 records**. A positive record identifies cwd `/private/tmp/stab-s2a2-role-sol-python-final/test_children_run_guarded_from0/control` and imported package `/private/tmp/stab-s2a2-role-sol-python-final/test_children_run_guarded_from0/control/vlogsrt/__init__.py`, with socket/connect refusal true. Proof records also cover blocked read/probe APIs and deliberate bootstrap failure. Swift asserts the same-interpreter guard, exact synthetic cwd/copied import and socket refusal before payload, then deletes its synthetic per-job fixtures. The new driver preserved this gate; Sol reviewed it before execution and ran refusal tests before V2 positives.

Review rejected helper-only V2 request evidence, a nested-JSON digest string/object mismatch, and replacing the vision factory itself. The accepted tests invoke both production factories with only the SDK constructors faked. Other corrections covered URL parsing/delimiters/ports, accidental application of app policy to direct CLI, pre-assignment malformed-config failures, ignored storage-read errors, unsaved key material, trimmed-key redaction and invalid decoded Unicode. A broad `raises(Exception)` had hidden an incidental string-versus-Path AttributeError; it now asserts the real ProjectConfigError. The reversed Swift fixture was changed to a real old-key/new-key scenario, preserving the blocker/no-echo assertions rather than inventing partial-key heuristics.

## Deviations, limits and lifecycle

- GLM directly reported reading High Agency 0.10.1 at its installed `.claude/plugins` path and supplied immutable `propagating | files<=18 | modules<=6 | boundary=shared-api` before edits. Actual 17 paths match the ceiling. The requested exact `claude-glm --model 'glm-5.3[1m]' --effort high --permission-mode auto` was launched through readiness/adoption; no substitute writer or model escalation was used.
- GLM disclosed that no executable pre-edit RED ran. A later historical synthetic demonstration was reported, not independently accepted as pre-edit evidence. Early commands included bare pytest piped through sed/tail; ambient credential presence/access and original test exits are not inferred. No potentially unsafe raw output, dumps, credentials or protected paths were investigated retrospectively.
- Parent correction `msg_554f6d4b4fb7` made GLM source-only and Sol the executor of **all runtime tests/probes**. GLM explicitly acknowledged this, and the common/role briefs were updated at `74395e7`. Afterward GLM performed source edits/static formatting; Sol supplied the independent results above. Skill reading did not by itself prevent execution-discipline mistakes; no skill-causation claim is made.
- Sol tooling errors: `worker-read --lines` was rejected with exit 1 and corrected to supported `--limit`; installer syntax check initially used nonexistent `.zsh` suffix and exited 127, then the actual `.sh` path passed with exit 0. Both errors were read-only and left no unresolved implementation issue.
- Cooperative Python guarding is not OS confinement. Existing fd/alias/native-consumer limitations and the uninspected system zshenv boundary remain. Earlier unit1/isolation incidents retain their uncertainty. No real Keychain, paid API, original-media/FCP or signing/notarization acceptance is established by fake tests and an unsigned build.
- GLM `task_12f6e90b11ea` / `ctx_02bac39b785a` settled succeeded via `msg_ed5ecdd8d5de` at 12:11:51 UTC. The worker's phrase “Swift 114+extension” is corrected here: **114 total**, including the extension. After accepted settlement, release reported external retention; exact idle terminal `term_fcf36e6f-06cd-46c8-a649-e08505fac17f`, incarnation `249eaff0-6f72-48c2-841a-bb5ca81aab39`, was closed (`ptyKilled=true`). Reclaimable query is empty.
- No unresolved failure remains within this role unit. Next: truthful readiness for input/job/executable/tools/grants/security scopes/writability, operation-specific credentials, and actual multi-audio rejection, followed by the aggregate gate. Packaged binary supply, live integrations and S2A-3/S2B/C/S3–S5 remain pending. Per parent instruction `msg_0694b03eee86`, the overall Sol Task stays active across the intentional compact/idle boundary; no parent `worker_done` is sent here.
