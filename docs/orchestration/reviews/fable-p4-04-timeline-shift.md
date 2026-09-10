# P4-04 verdict: `_slice_timeline_shift` (boundary.py:2023), candidate 0ef58b2

Read-only review. Nothing edited, staged, or committed. Evidence: code reading of
`boundary.py` (P4-01/02/03/04), `captions.py` placement, the spec and brief, plus a fuzz
harness run against the worktree package (`PYTHONPATH=<worktree>`): 6,000 random
frame-aligned worlds, 1–3 contiguous blades per slice, random cue/segment padding across both
slice edges and internal blade boundaries.

    checked=7191 requests   non-narrowing branch hit=1209   raises=0   wrong k=0

## Premise: is `k` constant within one `LogicalSlice`? YES, exactly.

Placement (`captions._place_instance`): `timeline = (clip.offset - tcStart - clip.start + asset.start) + file_time`,
so per blade `k_blade = clip.offset - tcStart - clip.start + asset.start`.
Blade (`boundary._blade`): `timeline_time - source_time = (clip.offset - tcStart) - (clip.start - resource.start)` = the same `k_blade`.
Coalescing (`_continues`) requires `previous.source_end == following.source_time`,
`previous.timeline_end == following.timeline_time`, and equal `resource.start`; since both `_end`
values are `_time + clip.duration`, this forces `k_prev == k_next`. Retime is rejected at ingest
(`fcpxml.py:106`), and the spec pins normal-speed matching CFR, so duration is the same on
both clocks. Therefore `k == slice.timeline_start - slice.source_start == slice.timeline_end - slice.source_end`
for every blade and every cue of the slice. The `timeline = file_time + k` model is right.
(A `conform-rate` element is not parsed anywhere; if one ever mattered, P4-01 slices and P3-03
placement would be wrong in the same way, so it is not a P4-04 concern.)

## Ground 1 — "it derives a time": CONFIRMED as a contract defect, not a literal rule-1 breach.

Rule 1 is about whisper times, and those are untouched: boundary cue file times are
`cue.start + trace.pcm_sample0_file_time`, exactly once, exactly as required. What the function
does is reconstruct an XML-derived editorial offset by interval intersection over downstream,
already-clamped artifacts, when the exact value sits one hop upstream in `LogicalSlice` and the
brief says "Timeline mapping uses exact P4 formula/Fractions". The module docstring even
frames it as inference ("두 시계에서 유도한다"). Its correctness rests on a chain of non-local
invariants (see below) that nothing in `project_boundaries` validates. That is the D24 shape
of risk even though the current instance happens to be sound.

## Ground 2 — "it can fail on legitimate input": REFUTED for plan-produced input.

Write `end_gap = timeline_end - source_end`, `start_gap = timeline_start - source_start`. From
placement, `end_gap <= k <= start_gap` always, with equality iff that endpoint was not clamped by
its blade. So `lo == hi == k` iff some placement has a free end AND some has a free start.
Non-narrowing therefore requires every placement to lack a free end (or every one to lack a free
start). A cue lacking a free end on every blade it touches must extend past the slice end; P3
cues of one source are non-overlapping (refine `min_gap`; `serialize_srt` rejects overlaps), so
at most one cue can do that. Hence non-narrowing ⇒ the slice holds exactly one cue id, and it
crosses the slice start and/or end. The "all cues clamped at both ends" shape in the concern is
impossible for more than one cue.

In that one-cue case the hypotheses are exact, not heuristic: crossing the start makes
`earliest == k + slice.source_start` and `_seed_span`/`_closure` (which includes the cue's own
span, then clamps to the slice) makes `request.source_start == slice.source_start`, so
`hyp1 == k`; symmetrically `hyp2 == k` when crossing the end; and the other hypothesis is either
`== k` (set dedupes) or strictly outside `[lo, hi]`. Fuzz: 1,209 non-narrowing cases, 0 raises.

## Ground 3 — "it can silently pick a wrong k": REFUTED for plan-produced input.

By the same algebra, `hyp1 = k + (shown_start - request.source_start)` and
`hyp2 = k + (shown_end - request.source_end)`. A wrong in-range value needs
`request.source_start < slice.source_start` or `request.source_end > slice.source_end`, i.e. a
request outside its slice, which `_sealed_request` cannot produce. With a hand-built request
narrower than its cue the function raises "유일하지 않다" rather than mis-picking (checked by
hand: request [4.0, 4.3] under cue [3.6, 4.4] gives two in-range hypotheses). Fuzz: 0 wrong.

## Is a fix warranted? Yes, but small and non-urgent — a hardening, not a bug fix.

The inference is total and exact today only because of four invariants owned by other layers
(per-blade clamping semantics, P3 cue non-overlap, closure ⊇ cue span, request ⊆ slice). None
is checked in P4-04; any future change to P4-02's closure or P3-03's placement could turn the
`ValueError` branch or the hypothesis branch into a live path with no test to catch it. The
exact anchor is available at zero cost.

### Minimal correction spec (files: `vlogsrt/project/boundary.py`, `tests/project/test_boundary_transcribe.py`, `tests/project/test_boundary_planning.py`)

1. `BoundaryRequest`: add one field `timeline_start: Fraction` (the request's own input-timeline
   start; place it after `source_end`). Do NOT add it to `_request_id` material: `slice_id`
   already hashes the four slice endpoints, so request ids and P4-03 cache keys
   (`_boundary_cache_key` hashes only `sample_range`) stay byte-identical.
2. `_sealed_request`: `timeline_start = bucket.slice_.timeline_start + (start - bucket.slice_.source_start)`.
   That is the binding formula verbatim (slice.timeline_start = clip.offset − tcStart,
   slice.source_start = clip.start − asset.start).
3. `_boundary_project_cues`: drop the `shift` parameter; compute
   `shift = request.timeline_start - request.source_start` locally. Delete
   `_slice_timeline_shift` and the `by_slice`/`shifts` bookkeeping in `_replaced_cues`.
4. Optional, cheap, non-deriving guard in `_replaced_cues`: for each doomed in-scope cue require
   `cue.timeline_end - cue.source_end <= shift <= cue.timeline_start - cue.source_start`, else
   raise (contradiction between plan cue placement and slice anchor). This keeps the brief's
   "reject inconsistent linkage" line without inferring anything.
5. Rewrite the module-docstring bullet "타임라인 이동량은 묶인 큐의 두 시계에서 유도한다" to say the
   shift is the request's exact slice anchor.

### Test impact

- `tests/project/test_boundary_transcribe.py:173` `_request` helper constructs `BoundaryRequest`
  by hand → add the new keyword (any Fraction; value is irrelevant there).
- `tests/project/test_boundary_planning.py` → add one assertion that each request's
  `timeline_start` equals `slice.timeline_start + (request.source_start - slice.source_start)`.
- `tests/project/test_boundary_projection.py` → no assertion changes. No test references
  `_slice_timeline_shift` or its error strings ("타임라인 닻…"); the `match=` patterns at lines
  673/709/960 target linkage, span, and lock errors. `test_trailing_cut_replacement_maps_through_the_slice_shift`
  asserts the true k (33/20, 39/20) and keeps passing; only its docstring mentions the anchor
  heuristic. **No currently passing test pins the inference.**

## Other observations in the P4-04 diff (none blocking)

- Success-with-zero-cues is not a silent-deletion path: `BoundaryTranscript.__post_init__`
  raises "성공인데 큐가 하나도 없다" (verified by constructing one). Good.
- Instance scoping is sound given closure: `_in_scope` tests the cue's unclamped file span, but
  `_closure` grows every request over all cue/segment ranges of the trace, so any cue that
  overlaps a request lies wholly inside it (clamped to the slice) and both placements of a cue
  straddling an internal blade boundary are doomed together; touching requests that would share
  a cue are forced to merge. No cross-instance deletion found.
- Asymmetry to note for P6, not a P4-04 defect: source cues straddling an internal blade
  boundary are two `ProjectCue`s (one per instance, same `cue_id`), while a boundary cue
  spanning several blades is one `ProjectCue` carrying all `request.instance_ids`.
- Millisecond collapse: re-derived from current cues, dedupes ids, `_with_flag` avoids
  duplicate flags, SRT omits only flagged cues. Matches the brief.
- Linkage: transcript/request field equality, origin equality on every draft/spine segment, and
  trace pairing by (source_id, pcm_analysis_key) all reject rather than guess. Matches the brief.
