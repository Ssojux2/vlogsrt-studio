# Task P7-06: run the complete offline acceptance matrix

## Authority

Binding spec `Phases and acceptance` and `Evidence at handoff`; depends on integrated P7-04 and P7-05. This is the last GLM code/test task before Opus review and Astra's P7 integration; it must not invent live acceptance.

## Baseline and ownership

- Fresh `fcp-p7-acceptance` worktree from exact SHA containing P7-04/P7-05.
- Create `tests/project/test_acceptance.py`, `docs/PROJECT_RUNBOOK.md`. Synthetic input/media is inline or generated under pytest `tmp_path`; no fixture/golden file is added or modified.
- Product fixes discovered here require a separate bounded GLM fix task; do not expand this acceptance task into cross-module implementation.

## Contract

- One offline end-to-end safe fixture must inspect -> fake analyze -> plan -> export -> captions reproject and actually shorten while preserving source and producing all five same-revision artifacts.
- Matrix covers same-name/repeated asset, nonzero epochs, muted/disabled, gaps, all eight fps including 30/29.97, correct PTS/sample origin, empty/error distinction, continuous blade/no extra STT, instance boundary replacement/no recursion, zero-frame mismatch, <=1 ms serialization/no drift, no guard removal/new partial cues, malformed vision/model missing/429/timeout/cancel/resume/stale plan.
- Runbook documents local project extra installation, pinned VAD hashes/license, offline commands, explicit online authorization/cost step, output/review interpretation, SRT import `Relative to Timeline`, supported recut/reproject, and honest external gates.
- External gates remain exactly: actual 2-minute then 10–20-minute FCP import/re-export; original 4K relink/playback; faint/short/noisy speech removal audit; production STT/GLM-Flash capability; DTD if unavailable. Mark each `PENDING_HUMAN` or actual evidence, never PASS without execution.

## Implementation steps

1. Add the end-to-end acceptance test and parameter/case matrix using only synthetic media/XML and fake saved model responses.
2. Run it failing first; if failure is product code, stop and report a fix brief rather than editing unowned modules.
3. Once product fixes are integrated, rerun focused acceptance, full project suite, invariants, and `make check`.
4. Write the runbook with exact commands/status meanings; commit owned files only.

## Acceptance

```bash
.venv/bin/python -m pytest tests/project/test_acceptance.py -q
.venv/bin/python -m pytest tests/project -q
.venv/bin/python scripts/check_invariants.py
make check
```

All commands exit 0, the safe fixture duration decreases, source hashes/records remain identical, five artifacts share one revision, and every unexecuted live gate reads `PENDING_HUMAN`. No paid/network call or protected fixture change occurs.

## Failure route

Report failing matrix row, exact stage/command/exit, expected/actual artifact/status, base and changed files. Produce a one-task GLM fix brief; after two same failures request Terra/Luna diagnostics, then Opus/Astra. Do not weaken the matrix.

## Report

Report Orca/launch IDs, branch/base/commit, files, full command/test totals, package hashes/duration, all pending live gates, and `ready-to-integrate` for Astra.
