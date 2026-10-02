# Raw video and Gradio v2 — independent acceptance

Source: `docs/plans/2026-10-02-raw-video-browser-v2.md` (byte-identical user source).
Branch: `Ssojux2/r08-clip-input`; initial HEAD `ddd5efe`.
Coordinator: GPT-6.1-Sol high (actual Orca screen); implementation: exact Claude-GLM `glm-5.3[1m]` high wrapper, no fallback.
Scope: A0-A8. A0/A1 accepted; A2-A8 pending. Only offline/synthetic product verification is authorized.

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
