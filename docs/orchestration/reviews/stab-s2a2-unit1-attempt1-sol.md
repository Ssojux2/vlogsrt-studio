# S2A-2 credential unit — first attempt rejected

2026-09-27. Baseline `cbde02a`; Sol brief `31b79fc`. No product commit or independent test acceptance was produced. Uncommitted changes remain for a fresh bounded recovery; S2A-2 preflight/unit2 and S2A-3–S5 remain pending.

## Outcome and lifecycle

GLM `task_418804c480c4` / `ctx_80c55eec0ac2` settled **failed** through `msg_c470d11f2387` at 09:28:43 UTC. Its report expressly says implementation is incomplete. Sol accepts that failure accounting, not its product behavior or every explanatory claim. Release returned external-terminal retention; exact terminal `term_a4733da0-e376-4b5b-a8de-0701a64f227e`, incarnation `0b3b896c-12b4-4584-a3c3-2223f5b7d852`, was proven idle and closed with `ptyKilled=true`.

Launch was the exact requested `claude-glm --model 'glm-5.3[1m]' --effort high --permission-mode auto`, readiness true then supervised adoption. Parent verified Sol high for complex planning/acceptance. Parent also reported prior client Luna max selection, standalone compact completed in 19 seconds, then restored Sol high and `/status` 15.3K/828K; this does not identify the hidden compaction model. Bare `/model` opens the picker; an argument-bearing slash input was plain text.

## Observed implementation defects and validation limits

- In-flight review found the pipe write descriptor assigned to task stdin, premature descriptor closure, inconsistent Swift/Python envelope limits, and an assumed pipe capacity. Worker reported three real `NSUserUnixTask` bridge crashes with signal 6 and `NSFileHandleOperationException` during XPC handle encoding. This is not a passed bridge or definitive proof of one root cause.
- Swift bridge command prefix: `swift test --package-path /Users/ssojux2/Working/vlogsrt/macos --filter developmentLauncherCarriesTheCredentialEnvelopeThroughTheRealWrapper`. Several runs piped output through grep/head; original process exits and exact per-run timestamps are not established by those filtered commands. Worker reports three attempts; no raw incident logs were opened by Sol.
- Other findings: Keychain update dictionary included query metadata; credential load errors forwarded arbitrary text; SDK 400 retry logging could print the original error before downstream redaction; bundled nil-key invocation omitted the authoritative empty carrier. Fixes are not yet independently verified.
- Worker reported safely guarded in-process carrier RED (exit 1, stale and empty-carrier failures), then carrier 9 tests and neighboring entrypoint/lock 30 tests green, later carrier 16 tests. These are worker evidence only; final draft changed afterward. Sol ran no independent tests against this attempt and no broad suite.
- Worker read High Agency directly (visible read), but full Impact reporting and scope/mail acknowledgment lagged edits. Earlier subject recorded only file cap22/shared-api; full reported estimate `propagating | files<=22 | modules<=7 | boundary=shared-api` was later supplied in scratch evidence. Do not claim complete durable pre-edit proof retroactively.

## Test-isolation violation

The new Swift bridge copied public Python sources, a byte-identical engine and synthetic dotenv, but omitted the explicitly required child guard/preflight/provenance. Sol observed its execution and prohibited further runs at 09:15:48 UTC (`msg_49cd9bf2f6b8`); a queued safety nudge was later observed consumed with a mailbox check at 09:16 UTC. The worker reports no subsequent bridge rerun under the hold. Protected-file access in earlier executions is unknown and was not investigated retrospectively. Copied sources and later fixes cannot establish earlier guard protection.

## Ambient-environment incident

At 09:21:35 UTC Sol observed a command creating `/tmp/nsuut-probe/dumpenv` with `env | sort > "$DUMP_TARGET"`. Sol sent a durable stop and supported combined interrupt at 09:21:56, escalated to parent, and prohibited dump reads or retrospective inspection. That receipt alone did not prove idle; a later interrupt-only command and exact idle wait did. An evidence-only reconciliation turn followed, then failed settlement and cleanup.

The worker self-report says it created the ambient dump, ran `grep -E "NSUUT_PROBE_MARKER|^PATH="` and `wc -l` on it, emitted one PATH line/count18, then removed its temporary probe directory. **grep/wc read the file, regardless of filtered output**; reject the worker's earlier “no other byte read”/“partially read” phrasing. Credential presence/content remains unknown. No dump content was inspected by Sol, and no recovery or retrospective secret investigation is authorized. The worker-reported compound probe exit0 is not proof of each nested operation; exact probe timing is unknown beyond observed screen/message chronology.

## Tooling and recovery

Worker status `--body` sends were rejected by the local Bash permission layer; reported generic text was “Permission to use Bash with command … has been denied.” Rule identity and static-policy versus automatic-review origin are unknown. Subject-only report references and canonical `worker_done` succeeded. Worker guesses 08:44/08:46 for denials precede its 08:50:27 dispatch and are not valid timestamps. No permission broadening or encoded/interpreter bypass is approved.

Parent commissioned an exceptional, narrow Astra high read-only review after repeated bridge failure; parent verified requested/effective model and live header and closed that reviewer. `/tmp/stab-s2a2-astra-review.md` contains source/SDK analysis only, not runtime proof. It identifies descriptor lifetime, fragmented/EOF framing, empty bundled carrier, guard/provenance and shell-startup gaps. Sol's [bounded recovery brief](../tasks/stab-s2a2-unit1-recovery.md) requires explicit allowlisted bootstrap and a read-only harness gate before child execution. Original media, real Keychain, paid APIs, FCP, signing, push/deployment remain unperformed.
