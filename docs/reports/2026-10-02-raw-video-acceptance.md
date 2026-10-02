# Raw video and Gradio v2 — independent acceptance

Source: `docs/plans/2026-10-02-raw-video-browser-v2.md` (byte-identical user source).
Branch: `Ssojux2/r08-clip-input`; initial HEAD `ddd5efe`.
Coordinator: GPT-6.1-Sol high (actual Orca screen); implementation: exact Claude-GLM `glm-5.3[1m]` high wrapper, no fallback.
Scope: A0-A8. A0/A1 accepted; A2 ingest/order foundation, A3 native media primitives, A4 understanding library and A7-1 adapters accepted and merged. Whole workflow and A3-A8 service/UI completion remain pending. Only offline/synthetic product verification is authorized.

## Baseline

Root `make check`, exit 0: 2,996 Python tests / Ruff and format 133 files / mypy 57 files / all 21 invariants.
Evidence: `/tmp/vlogsrt-raw-baseline.log`.

## A0 — accepted 2026-10-02

Eight owned files: verbatim v2 plan, historical 2026-09-13 partial-supersession notice, PROJECT_CONTRACTS,
exact import/literal checker, raw package/config, invariant tests, raw config tests. No product API,
media or macOS changes. No stage/commit; pre-existing user diffs remain intact.

Root independent evidence after functional edits:
- `.venv/bin/python -m pytest tests/test_invariants.py tests/raw`: exit 0, 318 passed (2.06s).
- `make lint types invariant`: final exit 0, 136 formatted / mypy 59 files / 21 invariants.
- `/tmp/vlogsrt-raw-A0-independent.py`: exit 0; JSON roundtrip, shutdown exact-date boundary,
  invalid text format/blank model/nonfinite price/shutdown-disable/unknown fields refusal,
  optional empty hints accepted. Repeated after final edits.
- `cmp` user v2 versus repository copy and owned `git diff --check`: exit 0.
- Logs: `/tmp/vlogsrt-raw-A0-root-tests.log`, `/tmp/vlogsrt-raw-A0-root-quality-final.log`.
- Worker report `/tmp/vlogsrt-raw-A0-glm.md` is supporting evidence; root tests decide acceptance.

Initial root quality snapshot caught test import/long-line issues while writer was editing; final
quality passed after fixes. Worker-reported 3,054 full-suite pass was before final corrections and
does not replace final root targeted evidence. Full final regression remains required at A8.

Skill use: exact installed High Agency 0.11.0 Read observed in A0 provider transcript.
Run `run_6242a7b16a42`; A0 task `task_02d16a02f57c` / Dispatch `ctx_525dea3834e3`, succeeded.
Next: compact idle GLM, reuse for A1 under fresh Dispatch; implement SQLite jobs/service and safe Gradio four-screen scaffold.

## Pending real acceptance

Paid API/account access and old/new quality comparison: NOT_RUN.
Personal source videos, physical SD ingest, HDR/Log quality and Final Cut import: PENDING_HUMAN.
Browser foundation: PASS; actual clip extraction pending A3; complete browser flow pending A8.

## A1 — accepted with A1F/A1F2, 2026-10-02

SQLite project/settings/media/jobs, explicit resumable jobs, local Gradio four-screen shell and guarded
ID-only Range serving implemented. UI-bound tests skip cleanly when optional UI extra is absent;
core policy tests remain UI-independent. Downstream ingestion/analysis/export/caption buttons are
honestly disabled until their stages are implemented.

Root final checks:
- Full root `make check` after checkpoint: 3,231 passed in 84.25s, exit 0; same149format/mypy64/21.
- Targeted raw/invariant/legacy UI-service suite: 503 passed, exit 0 (6.35s).
- Optional UI imports blocked in a separate process: 456 passed, 3 UI module skips, exit 0 (3.50s).
- Ruff/format 149 files, mypy 64 files, all 21 invariants: exit 0.
- Independent mutation/media/cancellation/concurrent-open and cross-project media binding probes PASS.
- Actual process interrupted after durable progress 1/3: open preserved state, explicit startup recovery
  marked interrupted, wrong/missing fingerprint refused, same-ID resume completed 3/3 exactly once.
- Captured-root concurrency probe PASS (macOS /tmp and /private/tmp compared canonically).
- Actual browser: four tabs, create/open, folder listing, page refresh and server restart restore,
  create C from B then select/cancel C job WITHOUT reload; job persisted cancelled. Same-view state
  updates verified; other-tab stale selection rejection covered by request/event/race tests.
- Actual media: generated H.264/AAC seven-second source loaded (duration 7, readyState 4) via opaque ID;
  HTTP bytes=0-31 returned 206, Content-Range and exact 32 bytes; live browser seek completed
  at 3 seconds, seeking=false, readyState=4. No Gradio original-file duplication path used.
- Live Host/Origin/headerless mutation/Gradio direct-file refusals returned 403; valid root GET 200.
- Synthetic source SHA-256s unchanged. Guarded UI process exited with ZERO real dotenv probe/read events.
- Logs `/tmp/vlogsrt-raw-A1F2-root-tests.log`, `-quality.log`, `-no-ui.log`, `-root-server.log`;
  screenshot `/tmp/vlogsrt-raw-A1-final-browser.png`; probes `/tmp/vlogsrt-raw-A1-{independent,project-binding,restart,scope-race}.py`.

Pre-acceptance bugs independently found and fixed: private-file registration, headerless mutation,
cancel-overwrite, multiple first-open managers, cross-project ID retarget, missing same-view signature
update, and selected-root reread race. New UI boot preserves other running UI processes.
Worker reports are supporting evidence only. A1F2 settled ctx_90631395cd25/task_f3bd9ffc9007.

Latest user routing: implementation exact Claude-GLM glm-5.3[1m] max; root GPT-6.1-Sol high acceptance.
Next independent wave after explicit owned-file checkpoint: A2 ingest/UI/service, A3 preprocess media
boundaries, A7-1 transport/capabilities. Separate worktrees and exclusive file ownership, sequential integration.

## A7-1 foundations — accepted 2026-10-02 (full A7 still pending)

Worker884471a+217affb, integrated as b159bdb+85925b2. Root 632 affected tests exit0 (6.54s),
Ruff153/mypy66/21 invariants exit0, immutable diff check exit0. Independent synthetic WAV/fake sender
probe verifies shared binding, role fields, no TextTranscript times, object-array languages, malformed
range refusal, shutdown/replace-crafted guard refusal before send, and stale-byte binding zero calls.
Actual SDK Transcription.model_dump shape exposed one pre-acceptance defect; corrected and covered.
Raw config byte-identical; no paid calls. Final-video audio mapping/repair/retext/manual revisions and
full SRT UI not yet implemented. Next: isolated A4 understanding library on max worker, UI integration
after A2/A3; root owns acceptance.

## User-requested branch integration — 2026-10-02

Root runtime re-confirmed GPT-6.1-Sol high via Orca screen; implementation stays on
existing exact glm-5.3[1m] max workers, no fallback.
A2 c3ed5e4+72a0313 accepted and merged (actual no-ff merge efab5e9). Root raw/invariant
tests exit0; Ruff158/mypy67/all21 exit0; unchanged real multi-import SHA/provenance/repeated
invalid-ready retry probe PASS. Additional root real multi-batch global order and immutable
revision probe PASS; v2->v3 migration regression reviewed and passed.
A3P-F1 diagnostic correction independently passed146 tests+quality; before native acceptance,
root actual synthetic output-symlink alias overwrote synthetic source, then verification raised.
A3P-F2 dispatched task68aa0fc13069/ctx503344f07068 to same compacted max worker; no merge yet.
A4 library630a23b root552 affected tests+quality/all21 exit0. Independent fake transport probe
shows approved=False reaches sender when env online; replay annotation + config online emits
online_live. No paid requests; fixes required before merge.
User .env.example and historical dirty/untracked files retained unstaged; no push.

Additional root A3 real repro: source7sec25fps, requested frames[0,25) =>25 frames
with core[0,1) but actual media duration7.0sec (all remaining original audio copied).
Cause: audio_tail_local used full-source last_pts for every interior fragment (read+actual
repro, high confidence). F2 follow-up msg_e2f35952669a requires interval-specific duration
with source-final audio tail preserved by last fragment only; same exclusive writer.
A4F1 task7c79e56a23bd/ctx529fb133db21 started after actual idle compact; same max model.
A2 own released external terminal exact incarnation verified and closed after accepted
merge; ptyKilled=true, no unrelated user terminal/worktree closed.

A4F1 49cbf88 root96 affected A4 tests + original approval/replay probe PASS, but
NOT accepted: direct OnlineLunaExecutionContext construction with mismatched source,
stream, extent, fingerprint and ASR still reaches fake transport once. Root repro
/tmp/vlogsrt-raw-A4-root-context.py; code checks approved + id-set only.
msg_e75c8971b4a4 enqueued before worker_done but unaddressed in delivered commit; durable
message receipt alone is not read/implementation proof. Fresh bounded follow-up required.
Worker validation observation: pipes to tail/grep can mask underlying check failure or
trigger redundant reruns solely to count tests. Root requested raw exit capture and
no repeated broad suite after unchanged code; final merged make check remains authoritative.

A3 native primitives accepted aa123ba after root218 affected tests exit0 (16.95s),
focused final diff review and actual alias/interval probes PASS. Runtime blob
346be9a71dc6e8a063ceed8363792a44a8c6e326 exactly matches final committed helper.
Same/symlink/hardlink/existing outputs refused without source/output mutation, valid25frame
extraction succeeded; independent video3sec/audio5sec clips measured1.002667/1.005417/3.000083
seconds (25frames each), final audio tail retained and source SHA unchanged.
No-ff branch merged without conflicts; full A3 runner/coverage publication still pending.
Diagnostic smoke can replace its own deterministic output on rerun; immutable package
revisions belong to A6 and are not supplied by this diagnostic.

A4 library630a23b+49cbf88+2d1345f accepted and no-ff merged03ba307. Root101 A4 tests
exit0 (2.33s); both independent approval/replay and direct invalid-context probes PASS
with zero fake sends. Final context diff reviewed: self-validates recomputed fingerprint,
source/stream/extent/model approval, exact plan/evidence windows, recomputed payload hash
and frozen mapping; responder/context approval and reservation identity must match.
All three source-branch tips are ancestors of current HEAD; no conflicts, no push.
A2 mergeefab5e9, A3 merge0c020f4, A4/A7 merge03ba307. Accepted owned workers released,
exact terminal incarnations checked idle and closed; branch/worktree histories retained.
Impact drift: baseline files<=45; actual committed feature spread53files fromddd5efe.
Boundary remains shared-api with existing API/ffmpeg/import invariants; root broad merged
make check running at /tmp/vlogsrt-raw-merged-full-check.log, final exit/count pending.

## Final merged checkpoint — PASS, 2026-10-02

Root `env VLOGSRT_OFFLINE=1 PYTHONPATH=$PWD make check`, real process exit0:
**3,572 passed in99.55s**, Ruff/format/mypy PASS, all21 invariants PASS.
Final evidence log: /tmp/vlogsrt-raw-merged-full-check.log. No further product edits
after this check; subsequent changes are acceptance documentation only.
All three `git merge-base --is-ancestor <source branch> HEAD` checks independently exit0;
committed feature diff whitespace check PASS and both synthetic original SHA256s PASS.
All own completed worker terminals closed after settled releases; reclaimable worker
list empty. Unrelated user sessions, original media, .env/.env.example and existing
user diffs were neither staged nor modified by integration. No remote push performed.

Remaining implementation: richer creation-time ingest binding; full-project A3 sequential
coverage/extraction/publication/resume runner; A4 raw-service/UI integration and audio/video
extent reconciliation; A5 review/boundary edits/use choices/VTT; A6 immutable packages;
A7 final-video repair/retext/manual/draft/final caption revisions; A8 full browser flow
and execution documentation. This checkpoint accepts merged foundations, not those stages.
Paid product API, personal original/physical SD, HDR/Log and Final Cut remain
NOT_RUN/PENDING_HUMAN. No paid API or deletion approval inferred from merge authorization.
