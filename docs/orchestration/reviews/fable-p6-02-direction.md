# P6-02 direction review (Fable 5.1, read-only) — candidate bde0368 on base 2f45e6a

Evidence: `vlogsrt/project/edit.py` (1690 lines) and `tests/project/test_edit_plan.py` read in full;
`vad/guard.py` (`InstanceGuard`, `_keep_all`, `project_guard`), `vision/policy.py`
(`request_sha256`, `VisionDecision`), `boundary.py` (lock source, `ProjectCue`), the three P6
briefs, and my P6 pre-phase and P6-01 reviews. All 163 P6 tests and `check_invariants.py` pass on
the candidate. Every claim below marked *reproduced* was run with a scratch script against the
candidate; nothing in the repo was edited.

## 방향 판정: SOUND_WITH_CONCERNS

The contract is the right one and P6-03/P6-04 can consume it by lookup. The concerns are all in
`validate_edit_plan`'s reach and in one shape P6-02 exposes that both downstream tasks would have
to reconstruct. None changes the algorithm; all are additive.

## 1. 예측 착지

Everything from the pre-phase landed in the predicted shape: `EditPlan.frame_duration /
locked_instance_ids / gaps`, `RemovedInterval.restore_at_output_frame` (replacing the ambiguous
instance reference), hash precheck before any block is read, apply on `status=="accepted"` only
with narrowing-only re-checks, the identity→raise / content→keep line, per-instance frame
conservation, output tiling of `retained ∪ gaps`, ≥1 frame per instance, and the timeline-grid
anchor stated once in the module docstring. The P6-01 follow-up also landed:
`vision_request_for` is the single projection, `policy.request_sha256` is public, and the plan
raises on mismatch.

Predicted but not present: **"every removed range inside one block."** The implementation drops
it with a stated reason (the plan carries no blocks, so the validator cannot see them). The reason
is true and the consequence is item 3-H below: `validate_edit_plan` is a structural gate, not a
provenance gate. That has to be said in the P6-04 brief, or the check has to come back in another
form.

## 2. 시그니처 충분성 — where P6-03/P6-04 would reconstruct

**One place, and it is the P4-04 shape.** `retained` is fragmented at every block boundary even
where nothing was removed. *Reproduced*: a 500-frame clip with no decisions yields two
`kept_block` intervals `[0,250)` and `[250,500)` with identical shift; a clip with a 100-frame
protected prefix and no decisions yields three (`kept_outside_blocks | kept_block | kept_block`),
all shift 0. Consequences:

- P6-03: my pre-phase rule "a single-instance cue must lie wholly inside exactly one
  `RetainedInterval`" is **wrong against this plan** — a cue crossing frame 250 of an uncut clip
  would hard-fail. The correct unit is the maximal run of same-instance intervals contiguous in
  both source and output.
- P6-04: one `asset-clip` per `RetainedInterval` emits a through-edit at every block boundary
  (hundreds on a 77-minute vlog with ≤10 s blocks), and the marker rule "a cut-boundary frame
  belongs to the later slice" then assigns owners to cuts that do not exist.

Both consumers would implement the same merge. Fix in P6-02: add `EditPlan.runs:
tuple[RetainedRun, ...]` (`instance_id, source_frames, input_timeline_frames,
output_timeline_frames, reasons: tuple[str, ...]`) built by coalescing `retained` where the next
interval of the same instance starts exactly at the previous source end **and** output end; keep
the granular `retained` for the trace; `validate_edit_plan` checks `runs == coalesce(retained)`.
A pure public helper `storyline_runs(plan)` is the acceptable minimum if the field is refused.

Everything else P6-03/P6-04 need is present by lookup: grid from `plan.frame_duration`, gaps
with output positions, `instance_id → document clip → resource` for refs and static attributes,
unavailable records typed, restore points exact. P6-04's `tcStart=0` matches the plan's output
anchor (`m × frame_duration` from 0).

## 3. 규칙 9 기계화 — paths that pass `validate_edit_plan` yet lose footage

All *reproduced* on hand-built plans/guards (the artefact P7 would persist and P6-04 would gate):

- **A. Retained output shorter than source.** `retained[0]` source `[0,75)`, output `[0,50)`,
  next tile starts at 50 → zero findings. Conservation counts source frames, tiling counts output
  frames, and nothing ties the two lengths. Rendering must then truncate source or overlap output.
- **B. Zero-length output tile.** Output `[0,0)` for a 75-frame retained interval → zero findings;
  `_tiling_findings` skips zero-length tiles. Same class as A.
- **C. `restore_at_output_frame` is never checked.** `9999` passes. The trace would state a false
  restore location.
- **D. Guard `status` is never consulted in build or validate.** A hand-built `keep_all` guard
  with `protected_frames=()` passed to `build_edit_plan` with a `success`-built partition removes
  `[75,250)` and validates clean. **E.** A forged partition on a video-only (`srcEnable=video`)
  clip with an `inaudible` guard of empty protection likewise removes and validates clean. Honest
  `project_guard` output covers the whole slice for both statuses, so this is belt-and-braces —
  but the brief's own rule is "branch on status", and P6-02 branches only on content.
- **H. Decision-less removal.** A hand-built plan retaining 10 frames and removing 490
  unprotected frames of an uncut clip validates clean. This is the dropped "inside one block"
  check: the validator bounds removal only by `complement(guard)`, locked, unavailable, and ≥1
  frame — not by blocks or decisions. Subtitles are still not lost silently (cues in the guard are
  inside `complement`'s complement; cues outside the guard hit P6-03's cue∩removed hard failure),
  but footage is, within the unprotected envelope.

Also noted: revision is sensitive to the caller's `decisions` order (*reproduced*: same two
decisions reversed → different `revision`), so "same saved inputs → same plan" only holds if P7
persists decisions in one canonical order.

## 4. 적대적 경로

- **Config** cannot widen: partition/config hash precheck, `_require_config` rails, and P5-06's
  `__post_init__` are layered; no bypass found.
- **Decisions** can only select a P6-01 candidate of the block they name; forging every decision
  as "accept the front window" removes exactly what an honest model could — the envelope is
  `complement(guard) ∩ blocks`, by construction. `request_sha256` proves **consistency** (this
  decision saw this exact geometry, samples, and windows), not **provenance** — anyone who can
  write a decision can compute it from the block. That is the right claim for a revision key and
  the coordinator's rule is sufficient for it. One refinement: the check runs only on the apply
  path. *Reproduced*: a `keep_all`-status decision, or an accepted one below threshold, with a
  garbage `request_sha256` is recorded into `plan.decisions` (and into `revision` and the trace)
  with no review mark and no raise. Mixed revision is mixed regardless of status; move the check
  into `_require_decision_linkage` so every decision is proven.
- **Partition** is bound by document and config hash but not by guard generation; a forged
  partition is bounded by guard *content* at apply time and in validate, not by guard *status* or
  audibility (D/E above).
- **Plan** (the persisted artefact) is bounded only by A–H above. The trust statement the phase
  needs: **P6-04 renders only a plan built in-process by `build_edit_plan` from a persisted
  partition and decisions, and compares `revision`; it never deserializes an `EditPlan` for
  rendering.** With that rule, H is closed by construction and A–C are defence in depth.

## 5. P6-03 / P6-04 브리프에 지금 넣을 것

P6-03 (`docs/orchestration/tasks/p6-03-edit-captions.md`):
1. Signature `project_edit_captions(plan: EditPlan, source: BoundaryProjection)`; the loose
   `frame_duration` parameter is still in the brief and must go (`plan.frame_duration`).
2. Cue attachment is by **run** (item 2), not by `RetainedInterval`: a single-instance cue must
   lie inside one run of its instance; shift = `run.output.start − run.input.start`. Anything
   else is the cue∩removed hard failure. Multi-instance (boundary) cue: every instance locked →
   one whole-clip run each, consecutive in output with identical shift, cue inside their union.
3. EDIT identity `(cue_id, instance_ids)`; never dedupe by `cue_id`; the collapse message names
   both. Require `plan.locked_instance_ids == source.locked_instance_ids` and
   `plan.input_sha256 == source.input_sha256` next to the unavailable equality already there.
4. Tests: cue crossing an uncut block boundary (must survive), cue crossing a removal (must fail),
   source cue straddling a blade with an earlier removal (two placements, adjacent after ripple).

P6-04 (`docs/orchestration/tasks/p6-04-package-export.md`) — the brief still carries the
pre-phase's untyped shapes:
1. `render_edit_fcpxml(document, media, plan, captions, guards)` and
   `publish_package(destination, document, media, plan, edit_captions, source:
   BoundaryProjection, guards, *, dtd_path)`; no `source_srt: str`, no `review: Mapping`;
   `final_accepted` computed only here. `render_edit_fcpxml` calls `validate_edit_plan` first and
   refuses on any finding.
2. One `asset-clip` per **run**, gaps from `plan.gaps`, offsets `m × frame_duration` with
   `tcStart=0`; no ripple arithmetic. Marker owner = the emitted run, half-open.
3. The in-process-only rule from item 4: the rendered plan comes from `build_edit_plan` in the
   same call chain; a persisted plan is a record, never an input to rendering.
4. Test: a plan whose retained output length ≠ source length is refused (needs fix A first).

## 6. Opus 브리프 검토

The `request_sha256` comparison rule is sufficient for what it claims and the implementation
follows it exactly through `vision_request_for` with an empty context (correct: context is not
hash material, and if it ever became material the verifier would fail closed). Two holes in the
brief, not in the rule:
- It lists "every removed inside one block" as a validator check but fixes a signature that
  cannot perform it; it should have chosen between "plan carries block ids and P6-04 receives the
  partition" and "validator is structural; provenance is by rebuild". Recommend the latter, stated.
- It says to branch on guard `status` when reading a guard, but P6-02's build and validate never
  read `status` (D/E). The brief should say "no block and no removal in an instance whose guard
  status is not `success`", in both functions.

## 최소 수정 명세 (P6-02b, `edit.py` + `test_edit_plan.py`, all additive)

1. `_instance_findings`: for every `RetainedInterval`, `len(output) == len(source)` and
   `len(source) ≥ 1`; `_tiling_findings` treats a zero-length tile as a finding instead of
   skipping it. Tests A, B.
2. `_tiling_findings` (or a new `_restore_findings`): each `RemovedInterval.restore_at_output_frame`
   equals the output start of the first retained/gap tile whose input start ≥ the removal's input
   end, else the output end. Test C.
3. `_require_plan_blocks`: raise when the block's guard `status != "success"` (same line as
   locked/unavailable). `_protection_findings`: finding for any removal in an instance whose guard
   `status != "success"`. Tests D, E.
4. `_require_decision_linkage`: verify `request_sha256` for every decision, not only on the apply
   path. Tests I (non-accepted stale), J (accepted below threshold stale).
5. `EditPlan.runs` per item 2 with `validate_edit_plan` consistency check; or the public helper.
   Tests: uncut clip → one run; protected prefix → one run; one removal → two runs with different
   shifts.
6. Minor: canonicalise `plan.decisions` order (partition block order, stable) before hashing, or
   document that P7 must persist in that order. Test G.

Nothing above changes ids, candidate windows, or the removal envelope under honest inputs; the
existing 163 tests should stay green.
