# Analysis outcomes — S1B contract

Accepted implementation: `cd80d61` plus Swift correction `a9db170`. This contract describes offline-verified behavior; it does not establish live provider, Final Cut, or packaged-release acceptance.

## Records and compatibility

- Analysis messages use protocol `1.3`. Existing `1.1` and `1.2` analysis records remain readable through legacy readers; caption protocol is unchanged.
- `VisionWindowDecision` carries `status` and a sanitized `reason_code`. New records require outcome fields. Older decisions lacking them decode as `legacy`, with their old digest formula preserved, and cannot produce new cut candidates.
- A valid keep or proposed cut is `success`. Provider exceptions, malformed output, invalid candidate/frame identity, or missing cut evidence become `error`. Intentionally unprocessed windows are `skipped`.
- Only a successful valid decision can produce a candidate. Error/skipped/legacy decisions preserve footage. Confidence, frame timing and explicit approval requirements remain unchanged.
- `analysis-result.jsonl` contains the current attempt's `AnalysisResult`, bound to input revision, estimate revision and attempt ID, with a digest, status, candidate count, window counts, upstream counts and sanitized reasons.
- Upstream stage totals include planned/not-applicable items; successful count is `total - failed - skipped`. Usable-source coverage is tracked separately. Declared no-audio is visible as skipped/non-applicable work, not provider failure or attempted transcription success.

## Outcome and publication

| Aggregate | Meaning | CLI / engine entrypoint | Attempt / journal | App behavior |
|---|---|---|---|---|
| `success` | No failed/unprocessed required coverage; valid zero-window analysis is possible | 0 / 0 | succeeded / review_required | Normal summary, including zero candidates |
| `partial_error` | Some usable coverage plus failures or skipped windows | 0 / 0 | succeeded / review_required | Explicit warning and safe candidates available for review |
| `error` | No usable successful coverage, or a failed stage before windows | 2 / 1 | failed / authorized | Failure evidence visible; explicit same-job retry |

All attempted vision windows failing remains `error`, even when upstream stages succeeded. With zero windows, independently usable source coverage can distinguish partial failure from complete failure. Successful zero-window/no-audio results are not provider failures.

Cancellation remains separate: the attempt/journal is cancelled and no new successful aggregate is published. The final cancellation gate precedes aggregate publication. Existing cancellation and active-selection checks still prevent applying results or importing a roughcut.

Window decisions are appended as each window finishes. A later window failure retains completed decisions. Preparation/analysis-stage exceptions produce sanitized failure evidence. Attempts and usage remain diagnostic records; a new attempt replaces the current aggregate/window snapshot. Full cache/retry accounting and broader resume checkpoints remain S2B/S5 work.

## Failure propagation and UI binding

The Python entrypoint and Swift development stager both propagate diagnostics after a nonzero exit: attempts, usage, decisions and aggregate result. Failure sync excludes candidate and roughcut/export artifacts. Swift applies cancellation and selection-drift gates before sync and never imports on failure.

The app reader requires a readable current estimate and a fully readable attempt log. Input/estimate revisions and latest-attempt identity must match the aggregate. Success/partial requires a succeeded attempt; error requires a failed attempt. Missing, malformed, inconsistent, running, pending, cancelled or stale history becomes unknown.

The shared review router distinguishes:

- Fresh job: no attempt/result/candidate history; retain initial inspect/authorization/analyze flow.
- Valid success/partial: show the aggregate summary and permit candidate review.
- Valid error: clear candidate controls, show failure, and allow explicit same-job retry through existing engine gates.
- Unknown history: clear candidate controls, disable roughcut, show unknown, and stop without requesting the engine.

Legacy records remain readable; ambiguous history is not automatically rerun or treated as verified successful analysis. Main-app completed-analysis text also uses the aggregate summary.

Evidence and limitations: [S1B independent review](orchestration/reviews/stab-s1b-sol.md).
