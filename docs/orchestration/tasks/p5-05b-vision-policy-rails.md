# Task P5-05b: close the programmatic-config bypass of the vision policy rails

## Authority

Binding spec `Visual scoring and deterministic roughcut`; correction of the P5-05 candidate
`693aa5b8dccb39a5baeec4d88a480b3f19b3d03`. Coordinator/reviewer: Opus 5 (`run_a091d4869a98`).
Offline only. This task fixes a reproduced acceptance bypass; it does not extend P5-05 scope.

## Baseline and ownership

- Existing worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p5-vision-policy`,
  branch `Ssojux2/fcp-p5-vision-policy`, exact base `693aa5b8dccb39a5baeec4d88a480b3f19b3d03`.
  Do not rebase, reset, squash, or re-create the branch. Commit on top of `693aa5b`.
- Allowed files: `vlogsrt/project/vision/policy.py`, `tests/project/test_vision_policy.py`.
- Forbidden: `vlogsrt/project/config.py` (P5-00 owns it and it is already integrated),
  `vlogsrt/transcribe.py`, `tests/test_transcribe.py`, `vlogsrt/project/vision/__init__.py`,
  fixtures, `golden/`, `.env`, `.env.example`, any other package file.
- No new dependency. No live/paid call. No `--online`. No `VLOGSRT_OFFLINE=0`.

## Reproduced defect (root reproduced this on exact `693aa5b`)

`ProjectConfig` validates its numeric ranges only in `load_project_config` / `_validate_table`.
A programmatically constructed config skips that validation entirely, and the two rail helpers
in `policy.py` are then defeated by IEEE-754 NaN semantics:

```python
cfg = ProjectConfig(..., vision_apply_confidence=float("nan"))
_effective_apply_confidence(cfg)      # max(nan, 0.8) -> nan
0.5 < float("nan")                    # False  -> the `confidence < threshold` gate passes
# result: a confidence-0.5 candidate returns status "accepted", choice "cand-mid"
```

The same shape defeats the frame cap: `min(float("nan"), 16)` is `nan`, and
`min(True, 16)` / a non-int float also slip past a cap that is documented as a hard ceiling.

Verified locally at exact `693aa5b`:

```
max(nan, 0.8) = nan
0.5 < max(nan, 0.8) = False
min(nan, 16)  = nan
```

## Contract

Config defects are programmer errors, not model defects. `policy.py` already draws that exact
line: request defects raise `VisionPolicyError` and close the boundary, while response defects
normalize into a `keep_all` error decision. Config defects join the **request** side.

- Add one private validator (for example `_require_config(cfg)`) that is evaluated **before**
  any payload is assembled and **before** any decision can be normalized — i.e. reached from
  both `build_payload` and `parse_decision`, and reached in `parse_decision` **outside** the
  `try/except VisionPolicyError` that produces `_error_decision`, so a bad config can never be
  laundered into a `keep_all` "the model misbehaved" record.
- `vision_apply_confidence` is admissible only when it is a real (non-`bool`) `int`/`float`,
  `math.isfinite` is true, and it lies in `[0.0, 1.0]`. Anything else raises `VisionPolicyError`
  naming the key and the offending value. The existing floor semantics stay: the effective
  threshold remains `max(cfg.vision_apply_confidence, 0.8)` for admissible values, so a stricter
  configured value still wins.
- `vision_max_images` is admissible only when it is a real (non-`bool`) `int` and `>= 1`.
  Anything else raises `VisionPolicyError` naming the key and the offending value. The existing
  ceiling semantics stay: the effective cap remains `min(cfg.vision_max_images, 16)`.
- The rails keep their direction. A config may only make the run *safer* (fewer frames, higher
  required confidence); it may never raise the frame cap above 16 or lower the accept floor
  below 0.8, and it may no longer disable either comparison by being non-finite.
- Fail closed means: raise. Do not clamp a NaN/inf/out-of-range value silently to the rail, and
  do not downgrade it to an `error`/`keep_all` decision — a silent clamp hides a caller bug and
  an error decision misattributes it to the model.
- No other behavior changes. Every currently passing test in `tests/project/test_vision_policy.py`
  and `tests/test_transcribe.py` must still pass unmodified. Do not weaken an existing assertion
  to accommodate the new check; if an existing test genuinely conflicts, stop and report it
  instead of editing it.
- Reuse the existing `_finite`-style guards already present in this codebase rather than adding a
  new numeric idiom, and keep `bool` excluded explicitly (`isinstance(True, int)` is `True`).
- All literals stay where they already live (`_FRAME_CAP_CEILING`, `_APPLY_CONFIDENCE_FLOOR`).
  Do not introduce a new hard-coded number inside a function body (CLAUDE.md rule 8).

## Implementation steps

1. **Red first.** Add failing regressions to `tests/project/test_vision_policy.py` that construct
   `ProjectConfig` programmatically and prove, at minimum:
   - `vision_apply_confidence=float("nan")` raises `VisionPolicyError` from `parse_decision`
     for a response that would otherwise be `accepted` at confidence 0.5, and the test asserts
     the decision is **not** `accepted` by asserting the raise — not by asserting a `keep_all`.
   - `float("inf")`, `float("-inf")`, `-0.5`, and `1.5` for `vision_apply_confidence` each raise.
   - `True` (bool) for `vision_apply_confidence` raises.
   - `vision_max_images=float("nan")`, `0`, `-1`, `True`, and `16.0` (float) each raise.
   - `build_payload` raises for the same bad configs, before any payload is assembled.
   - A stricter admissible config still wins: `vision_apply_confidence=0.95` keeps a
     confidence-0.9 repetitive-B-roll candidate at `keep_all`, and `vision_max_images=4`
     still caps a 16-frame request at 4.
   - An admissible config at exactly the rails (`0.8`, `16`) and at the extremes
     (`0.0`, `1.0`, `1`) does not raise.
   Run them and paste the failing output before implementing.
2. Implement the validator in `vlogsrt/project/vision/policy.py` and wire it into both entry
   points at the positions described in the Contract. Update the module docstring rails section
   so it states the fail-closed rule and why (the reproduced NaN bypass).
3. Green the new tests, then run the scoped file, then the full gate.
4. Commit only the two allowed files, on top of `693aa5b`, with a `fix(project):` subject.

## Acceptance

Exact commands, run from the worktree root:

```
python -m pytest tests/project/test_vision_policy.py
python -m pytest tests/test_transcribe.py
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

Expected: the new regressions fail before the fix and pass after; `tests/test_transcribe.py`
is unchanged and passing; Ruff clean; mypy clean; all invariant groups `[OK]`; the full suite
passes with a count no lower than the 1,896 recorded at `693aa5b` plus the new tests.
`git status` is clean and `git diff --stat 693aa5b..HEAD` lists exactly the two allowed files.

## Failure route

Report the exact config value, the entry point, the expected/actual decision or exception,
the failing command with exit code and short log, the base SHA, and the changed files.
Do not loosen the rails, do not edit `config.py`, and do not modify an existing passing test to
make the new check fit. Two repetitions of the same failure -> stop and escalate to the
coordinator (Opus 5), who routes to Fable 5.1.

## Report

Before `worker_done`, send the exact committed SHA, `git diff --stat 693aa5b..HEAD`, the red
output captured in step 1, the green output of every Acceptance command with its test count,
and confirmation that zero network calls occurred. Then send `worker_done` exactly once.
