# Task P4-04b: place boundary cues from the exact slice anchor, not an inferred shift

## Authority

Binding spec `Source analysis, clocks, identity` (the `input_project_elapsed` formula) and the
P4-04 brief's "Timeline mapping uses exact P4 formula/Fractions" clause. Correction of candidate
`0ef58b27f88a2fe0ec10cd0466a489f843f24715`. Coordinator: Opus 5 (Run `run_a091d4869a98`).
Adjudicated by Fable 5.1 high; full verdict at `docs/orchestration/reviews/fable-p4-04-timeline-shift.md`
(read it in the main repo at `/Users/ssojux2/Working/vlogsrt`). Offline only.

## Why this is a hardening, not a bug fix — read this before you start

Your `_slice_timeline_shift` was independently adjudicated and found **correct today**. An
algebraic argument plus a 6,000-world fuzz (7,191 requests, 1,209 landing in the non-narrowing
branch) produced zero raises and zero wrong shifts. Do not treat this as a defect report about
your reasoning, and do not "fix" anything else on the strength of it.

What is wrong is narrower: the function reconstructs, by interval intersection over already
clamped downstream artifacts, an XML-derived editorial offset that `LogicalSlice` already carries
exactly. Its totality rests on four invariants owned by other layers — per-blade clamping
semantics, P3 cue non-overlap, `_closure` ⊇ cue span, and request ⊆ slice — none of which
`project_boundaries` validates. Any later change to P4-02 closure or P3-03 placement could make
the `ValueError` branch or the hypothesis branch live, with no test to catch it. The exact anchor
is one hop upstream and free. Take it.

The per-slice constant-`k` premise is confirmed exactly: `_continues` requires equal source and
timeline end/start pairs across coalesced blades, which forces `k` to match, and retime is
rejected at ingest. So `k == slice.timeline_start - slice.source_start` for every cue of a slice.

## Baseline and ownership

- Same worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p4-boundary-projection`, branch
  `Ssojux2/fcp-p4-boundary-projection`. Commit on top of your existing `0ef58b2`. Do not rebase,
  reset, squash, amend `0ef58b2`, or re-create the branch.
- Allowed files, **extended** from the P4-04 list for this correction:
  `vlogsrt/project/boundary.py`, `tests/project/test_boundary_projection.py`,
  `tests/project/test_boundary_transcribe.py`, `tests/project/test_boundary_planning.py`.
  The last two are extended only because `BoundaryRequest` gains a field and those files build one
  by hand; make the minimum mechanical change there and nothing else.
- Forbidden: everything else, including `vlogsrt/project/captions.py`, fixtures, `golden/`,
  `.env`, `.env.example`. No new dependency, no live call, no `--online`, no `VLOGSRT_OFFLINE=0`.

## Contract

1. `BoundaryRequest` gains one field `timeline_start: Fraction`, placed after `source_end`. It is
   the request's own input-timeline start.
2. **Do not add it to `_request_id` material.** `slice_id` already hashes the slice's four exact
   endpoints, and `_boundary_cache_key` hashes only `sample_range`, so request ids, boundary ids,
   and P4-03 cache keys must stay byte-identical across this change. Add a test that pins that:
   build the same plan before/after-shaped input and assert the request id is unchanged against a
   hard-coded expected digest, or equivalently assert two requests differing only in
   `timeline_start` share a `request_id`.
3. `_sealed_request` sets
   `timeline_start = bucket.slice_.timeline_start + (start - bucket.slice_.source_start)`,
   which is the binding formula verbatim.
4. `_boundary_project_cues` drops its `shift` parameter and computes
   `shift = request.timeline_start - request.source_start` locally. Delete `_slice_timeline_shift`
   and the `by_slice`/`shifts` bookkeeping in `_replaced_cues`.
5. Add the cheap non-deriving consistency guard in `_replaced_cues`: for each doomed in-scope cue
   require `cue.timeline_end - cue.source_end <= shift <= cue.timeline_start - cue.source_start`,
   and raise a typed `ValueError` naming the cue and slice otherwise. This keeps the brief's
   "reject inconsistent linkage" line without inferring anything. It must be a check, never a
   fallback that computes a shift.
6. Rewrite the module-docstring bullet that currently says the timeline shift is derived from the
   two clocks of the clamped cues ("두 시계에서 유도한다") so it states that the shift is the
   request's exact slice anchor, and say briefly why inference was removed.
7. No other behavior changes. Every currently passing test must still pass. Do not weaken an
   existing assertion; if one genuinely conflicts, stop and report instead of editing it.

## Test impact (already surveyed — confirm, do not take on faith)

- `tests/project/test_boundary_transcribe.py` `_request` helper builds `BoundaryRequest` by hand:
  add the new keyword. Any `Fraction` value; it is irrelevant there.
- `tests/project/test_boundary_planning.py`: add one assertion that each planned request's
  `timeline_start` equals `slice.timeline_start + (request.source_start - slice.source_start)`.
- `tests/project/test_boundary_projection.py`: no assertion changes expected. No test references
  `_slice_timeline_shift` or its error strings. `test_trailing_cut_replacement_maps_through_the_slice_shift`
  asserts the true `k` and should keep passing; update only its docstring where it describes the
  anchor heuristic.
- Add a regression that the anchor path handles the shape the old inference could not have
  narrowed: a slice holding exactly one cue that crosses a slice edge. It must now place from the
  anchor with no hypothesis branch involved.

## Acceptance

Run from the worktree root with `/Users/ssojux2/Working/vlogsrt/.venv/bin/python`:

```
python -m pytest tests/project/test_boundary_projection.py
python -m pytest tests/project/test_boundary_planning.py tests/project/test_boundary_transcribe.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

Expected: `_slice_timeline_shift` no longer exists (`grep -n "_slice_timeline_shift" vlogsrt tests`
returns nothing); request ids are provably unchanged; Ruff clean; mypy clean; **all 19** invariant
groups `[OK]` — count them, the previous report of "15/15" on a sibling task was a miscount; the
full suite passes at no fewer than 1,931 tests plus whatever you add.
`git diff --stat 9747988..HEAD` lists only the four allowed files.

## Failure route

Report the exact request/slice/cue ids, the expected and actual shift as exact `Fraction`s, the
failing command with exit code and short log, and the base SHA. Do not reintroduce inference as a
fallback. Two repetitions of the same failure -> stop and escalate to the coordinator.

## Report

Before `worker_done`, send the exact new committed SHA, `git diff --stat 0ef58b2..HEAD`, evidence
that request ids are byte-identical, and the green output of every acceptance command with its
test count and the invariant group count. Then send `worker_done` exactly once.
