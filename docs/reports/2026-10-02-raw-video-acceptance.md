# Raw video and Gradio v2 — independent acceptance

Source: `docs/plans/2026-10-02-raw-video-browser-v2.md` (byte-identical user source).
Branch: `Ssojux2/r08-clip-input`; initial HEAD `ddd5efe`.
Coordinator: GPT-6.1-Sol high (actual Orca screen); implementation: exact Claude-GLM `glm-5.3[1m]` high wrapper, no fallback.
Scope: A0-A8. A0/A1 accepted; A2 ingest/order foundation, A3 native media primitives, A4 understanding library and A7-1 adapters accepted and merged. A3 offline vertical workflow is accepted; A4-A8 service/UI completion and full workflow acceptance remain pending. Only offline/synthetic product verification is authorized.

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

## Continued implementation — A3 accepted, 2026-10-02

A3 offline vertical workflow73309a4 accepted and no-ff merged after root independent
847affectedtests exit0 (58.42s), Ruff181/mypy75/all21 PASS, final feature diffcheck PASS.
Root actual multi-source stereo/delayed-audio+NTSCnoaudio sameplan creates sevenMP4
and sevenMOV files in separateimmutablepackages; originalSHA retained. Root injected
second extractor failure: no third/latercall, explicit samejobID resume completes
with sameclipIDs/numbers and unchanged firstpublishedbytes. Early missingendpoint and
package-unboundclipID bugs independently reproduced and closed. Source creationTime
binding uses valid container tag only, no mtime chronology. New DBv4 migrations/relative
path movement/cancel/process restart covered by affected suite, UI controller tests PASS.
Evidence/tmp/vlogsrt-raw-A3-runner-root-{final-tests,final-quality,post-ack}.log and
/tmp/vlogsrt-raw-A3-resume-root.log. Full final browser scenario remains A8.
Remaining fullworkflowA4-A8 continued; A7-2 candidate57e399c+b30d8ff notaccepted until
offline+approvedlive zero-calls and actualcachedsample-range identity probes PASS.
A5 pure librarya4539d1 root39tests/basicprojection PASS, finalaudioTailprojection F1 active.

A5 domain accepted a4539d1+5c134de, mergedf7c0fd3: root46tests/0.11s PASS;
independent split/selection/immutable-roundtrip/markupVTT/finalmeasuredaudio-tail
projection PASS. Whole-source frame grid retained, fakeextra frames refused;
metadata/selection-only change has no reextract impact. UI/DB wiring remains pending.

A7-2 engine accepted57e399c+b30d8ff+ac71857 and merged: root297affectedtests/10.55s,
Ruff180/mypy76/all21 PASS and focused cache/gate/publication diff reviewed.
Independent realstereo delayedAudio source: bothproviderroles shareexactwavehash/sample
ranges, correspondence-approved latesttext is actually adopted, normalized timing spine
projects once to finalfile clock, verifyoff/on yields identicalSRTbytes and4textcalls each.
Independent workflowoffline+envonline+matchingapproval knownLIVEtransports now zero
fake sends, successful emptyresponses stayreview_required. Reusedworkdir changedchunksize
0.8->1.6 now actualWAVframes25600/22869 match declaredranges (old12800 reusebug closed).
No paidAPIcalls, final userreview/manual publication remains A7-3.

Actual A3 browser on synthetic guardedloopback server: createproject, inspectcards valid
creationtimes, confirm+copy+globalorder, createfullcoverageplan, twoactualMP4clips published
chronologically with manifests/CSV/README, registeredopaqueID browser decode duration3.4
readyState4, seek1sec completedseekingfalse. Browsererrors empty, fourtabs render.
Ownscreenshots/tmp/vlogsrt-raw-A3-browser-{main,extracted}.png; serverlog/tmp/vlogsrt-raw-A3-browser-server.log.
Intermediate UI has staleplaceholder import-nextstage and headercount0 afterimport;
noted for nextownedUI edit. FullA8 browser acceptance remains pending.

A6 filesystem library70c5dd7 accepted and mergedf939478. Root19targetedtests/0.14s,
Ruff189/mypy79/all21 PASS; focused copy/readback/protectedcache/path/pubdiffreview.
Independent REAL A3browserpackage full+selectedexports verified, cache/previews purge
then bothconfirmed folders reverified and oldclippayloadSHA unchanged.
/tmp/vlogsrt-raw-A6-root-after-interface.log, root-quality.log supporting evidence.
Whole UI/DB reservation/registeredfiles integration pending W1; no claimA6fullstage done.
Worker lifecycle firstcapability-invalid rejection was NOT accepted; later valid
worker_done msg4a0bf9b0164c /ctx151066393a04 settledsucceeded, hostreceiptconfirmed.

Latest combined main f939478: make check exit0, 3746passed/140.98s plus Ruff/format/mypy/all21; /tmp/vlogsrt-raw-integration-wave-check.log.
A4 f431d4a root34testsPASS but NOTaccepted; two-phase ASR→explicitsemanticapproval and noaudio livevisual approval unfinished, W1F1active.
A7-3 13ad043 root46testsPASS but NOTaccepted: independent REAL delayedstereo finalreparse falsely rejects fractional3.183; samevideohash differentvalidPCM accepted staleAuto; final/key symlink writesoutside. Rootprobe /tmp/vlogsrt-raw-A7-3-root.py exit1, W2F1active.
User requested installed AI Rough Cut removal: pluginkit ignore IDcom.ssojux2.vlogsrt.workflow then -r all5 paths; repeatedmatch0. Installedhost safelymoved /Users/ssojux2/.Trash/FinalCutAgent-disabled-20261002-220737.app; runningFinalCut notquit, projectdata/keychain/sourcecode untouched. Do not reinstall.

Accepted caption review 13ad043+0180354 mergedbd0324a: root50tests and both unchanged independent actualvideo probes exit0; fractional SRT equality/audio identity/symlink confinement/result-plan binding defects closed.
Accepted analysis backend f431d4a+49bd624 merged1b7283d: root43tests and REAL stereo-offset+NTSC-noaudio two-phase probe exit0; ASR once, awaitingstate survivesreopen, wrongsemantic0sends, Luna-onlysecondphase, frozenASRbytes/originalhashes stable, no stage1SRT. Productionbrowser transport still W1pending.
Full main1b7283d makecheck:3839passed/170.61s, Ruff/format/mypy89/all21 exit0, /tmp/vlogsrt-raw-post-A4-A7-review-check.log. Scope87featurefiles versus initial45 bound recorded; finalchecks needed afterremainingintegration.
Actual A4 browser own synthetic guardedserver: load4tabs/errors0/headerregistered4/importhelpfixed, localestimate, offline replay2/2 with speech/noaudio status+tags, newsemanticplan created. Then extract raises existing-completedpackage error although plan revision is new (samegeometry). W1 owns targeted fix; /tmp/vlogsrt-raw-A4-browser-server.log and browser-extract-error.png. This UI-through-extraction gate is NOTPASS yet.

2026-10-03 KST: caption workspacec6a0f6b+eff0f9e independently accepted/mergedcd16d80. Root30tests and unchanged offline0sends + REAL multitrack/offset/PCMwaveform/sharedchunks/reopen/manualstalehead/explicitreview/oldfinal+automaticbytes/frozenrerun0calls/sourceSHA probes PASS. New cache transport-provenance and temperature recovery guards validated; partial_failure staysfailed with evidence.
Review workspace313f24d+648b5d2 independently accepted/merged3cbf235. Root16tests, unchanged REALSQLite headUPDATE failure rollback/retry probe and replay→metadata0reextract→stalehead→split/remap/newextract→VTT→selected/full export→purge/reopen/reverify + originals/oldpackageSHA probes PASS. Revision/head publication atomically rolls back.
Full UI candidateb910fcc rootruntime/extraction subset9testsPASS but NOTaccepted: real-backend returnfields and screen regressions pending W1F1. A8 finaldocs now separateownedworker; root actual ALL4screen browser + final main broadchecks stillrequired. Accepted W2 settledownidle worker closed after exactincarnation check, release intentionalexternal-retained receipt, userdirected unusedagent cleanup; no unrelatedsession touched.

Final allowed implementation A0–A8 accepted2026-10-03 KST. UI b910fcc/72d98bf/e47ae95/80a3a9d merged9ce8598; root targeted9/19/149/14 and actual guarded4screen synthetic flow PASS. Own project create/import2/order/replay2/2→note/split/exclude/new3MP4/VTT→full3-selected2 immutableexports→purge/reverify→noaudio/multiaudio selection→sharedWAV timing1/text1→latestadoption/measuredorigin→manualtext/explicitreview/finalSRTVTT200+Range206/file=403. Restart restored review/caption data; visible selectedtrack video3s ready4/waveSVG/three-layertext/seek/loop/pause/first/blankreset PASS. Headless repeat clock progressed after native mute; actual audible listening/paid modelquality/personalSD/HDRLog/FCP NOT_RUN/PENDING_HUMAN. All servers/browser closed, dotenv0, final reopen0providercalls, original/oldmedia/finalhashes stable.
Root final MAIN9ce8598 makecheck EXIT0:3909passed/206.67s + Ruffcheck/format224 + mypy96 + all21 invariants; /tmp/vlogsrt-raw-final-main-check.log. Feature spread100 files vs original45 immutable bound; expanded surfaces received full verification. Final execution guide+manual matrix accepted from owned docsworker c9ebcd4; diagram/data statements grounded in actualcode/root evidence. No paidproductAPI/push/FCP reinstallation/original deletion.
