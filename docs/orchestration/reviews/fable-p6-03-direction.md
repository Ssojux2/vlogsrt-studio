# P6-03 direction review (Fable 5.1, read-only) — candidate a022f96 on base 5396242

Evidence: `vlogsrt/project/captions.py` P6-03 section (lines 511–819) and
`tests/project/test_edit_captions.py` read in full; `edit.py` plan types, `_clip_grids`,
`_ripple_*`, `_applied_candidate`, `_runs_findings`; `boundary.py` `ProjectCue`,
`BoundaryProjection`, `_projection_srt`; `captions._place_instance`, `serialize_srt`;
`vad/guard.py` `_source_spans`/`_cue_spans`/`_boundary_spans`/`_slice_frames`; the binding spec
(`docs/plans/2026-09-08-final-cut-project.md` lines 7, 14–15, 40, 57); the P6-03, P6-04, P7-01,
P7-04 briefs; my P6 pre-phase asymmetry section and P6-02 review section 5.
93 tests in `test_edit_captions.py` + `test_edit_plan.py` and `check_invariants.py` pass on the
candidate (run with the main checkout's 3.12 venv, `PYTHONPATH` pointed at this worktree; the
worktree has no venv). Every claim marked *reproduced* was run with a scratch script
(`scratchpad/probe.py`) against the candidate. Nothing in the repo was edited.

## 방향 판정: SOUND_WITH_CONCERNS

The contract is right, every section-5 prediction landed, and P6-04 can consume
`EditCaptionSet` by lookup with no reconstruction. One concern is real and cheap: my own P6-02
safety claim is true only for plans that would pass `validate_edit_plan`, because P6-03 checks
run containment before the removal intersection. Swapping that order makes the claim
unconditional. Everything else is brief hygiene for P6-04 and P7.

## 예측 착지: 전부

| Predicted (P6-02 §5, P6-03 items 1–4) | Landed |
|---|---|
| `project_edit_captions(plan, source)`, grid from `plan.frame_duration`, no loose parameter | yes (`captions.py:534`, `fd = plan.frame_duration`) |
| Attach by **run**, shift `run.output.start − run.input.start` | yes (`_single_instance_shift`, `_run_shift`) |
| Anything else = cue∩removed hard failure, no partial cue | yes, with a second diagnostic for "no run and no removal" (mixed input) |
| Boundary cue: one whole-clip run per instance, output-consecutive, identical shift, cue inside union | yes, all three verified, never `instance_ids[0]` (`_whole_clip_shift`) |
| Identity `(cue_id, instance_ids)`, no dedupe by `cue_id`, collapse message names both | yes (no id indexing anywhere; message `cue X (인스턴스 A,B)`) |
| Three identity checks: `input_sha256`, `locked_instance_ids`, `unavailable_instances` tuple-exact | yes (`_require_edit_revision`) |
| Tests: uncut boundary survives / crossing removal fails / straddling → two adjacent placements / boundary cue stays one | all four present (lines 320–445) |

Nothing predicted is missing. The timeline clock is consistent end to end: `_place_instance`
uses `clip.offset − tcStart` and `_clip_grids` uses the same anchor, and ingest forces clip
offsets onto the grid, so a cue clamped to a clip end lands exactly on `run.input.end × fd`
(*reproduced*: clip-end cue ends at the retained endpoint, no gap). Touching cues on either side
of a removal come out adjacent, `end == next.start` (*reproduced*, case c).

## P6-04 공급 가능성: 재구성 강요 자리 없음

- **Marker owner is a lookup, not a reconstruction.** A located `ReviewIssue` carries
  `(instance_id, source_frame)` in the asset-anchored source grid; the owner is the unique
  `plan.runs` entry with that `instance_id` whose `source_frames` contains the frame
  (half-open). `runs` are disjoint per instance, so there is exactly one or none; none means
  "deleted → no marker, keep in review.json". Nothing in `EditCaptionSet` needs to carry runs.
- **There is a marker producer**, but only one: `_REVIEW_GUARD_OVERLAP` (`edit.py:1262`) is the
  sole located issue in the plan; decision-inconsistent and unavailable issues are unlocated by
  design, and every P6-03 issue is unlocated by contract. So markers appear only when a
  candidate's removal range collides with the guard and the block reverts to keep. That frame is
  retained by construction, so the owner lookup always succeeds for it. P6-04's test list should
  use this issue, not a hand-built one, for the "post-split ownership" cases.
- **Captions never enter the XML.** Spec line 7: "No embedded captions". So P6-04 needs no
  per-cue clip owner or clip-relative offset; it needs `srt_text` (sidecar), `review`, and
  `plan_revision` — all present. `cues` are ProjectCues with source provenance untouched.
- **SRT format is byte-compatible** with SOURCE: `_edit_srt`, `_projection_srt`, and
  `serialize_srt` all emit `f"{n}\n{timeline}\n{text}\n"` joined by `"\n"`. Third copy of the
  serializer, but the import direction (`boundary → captions`) forbids reuse without a
  cycle; acceptable.
- **Brief drift to fix in P6-04**: the main body's marker rule still says the owner is the
  clip "whose retained `source_frames` contain" the frame; the appended Fable section says
  *run*. Same answer for a deleted-or-not decision, wrong answer for which `asset-clip` element
  owns it when `retained` is fragmented at uncut block boundaries. Change the word.

## 자막 소실 경로: P6-02의 안전 논증은 **조건부로만 참**

The P6-02 claim was "cues outside the guard hit P6-03's cue∩removed hard failure".

- **Honest plans: true.** The guard seeds from every source cue and every boundary cue
  (`_cue_spans`, `_boundary_spans`, including unresolved ones) with outward frame rounding, so
  an honest plan never removes under a cue; and for a plan built by `build_edit_plan`, `runs`
  and `removed` are disjoint, so any cue over a removal is outside every run and fails
  (*reproduced*, cases a/b: empty guard, honest decision, cue over the removal head and a
  removal strictly inside a cue both raise `큐가 제거 구간과 겹친다`). Boundary cue over a
  forged head removal in the first locked instance also fails, on the union check
  (*reproduced*, case e).
- **Forged plan whose `runs` overlap its `removed`: false.** `_single_instance_shift` tries run
  containment first and returns the shift; `_require_not_removed` runs only when no run
  contains the cue. *Reproduced* (case g): take the honest plan, replace `runs` with one
  `[0,500)` run, keep `removed=[75,250)`; `validate_edit_plan` reports 1 finding, but
  `project_edit_captions` **emits** `00:00:02,000 --> 00:00:11,000` for a cue that the plan's
  own `removed` says was cut. No raise, no flag, no review issue.
- Why this matters even under the in-process rule: `project_edit_captions` cannot call
  `validate_edit_plan` (no document, no guards in its signature) and does not. So P6-03's
  guarantee is "no cue is lost **given a consistent plan**", and consistency is proven only
  later, in `render_edit_fcpxml`. The P6-02 argument as I wrote it implied P6-03 alone was the
  backstop. It is not, today. The fix is free (see minimal spec): check the removal
  intersection **before** run containment, unconditionally. Then the hard failure is
  independent of `runs` consistency and the P6-02 sentence becomes literally true.
- Everything else is loss-free: every cue in `source.cues` either shifts or raises; nothing is
  filtered; `_ordered_edit_cues` re-checks rather than drops; collapse keeps the cue in `cues`
  and omits only the SRT row with a review issue; an unknown instance raises rather than
  skipping.

## 접힘 시계: 옳다, 그리고 SOURCE 증거와 충돌하지 않는다

- Re-measuring on the EDIT clock is necessary, not optional: the shift is `k × 1001/30000 s`
  on an NTSC grid, not an integer millisecond, so representability is not invariant under
  ripple. *Reproduced* (case d): a 0.6 ms cue collapsed on the SOURCE clock becomes
  3003 → 3004 ms after a −5338.667 ms shift; the reverse direction is the brief's
  "collapse introduced only after ripple" test. Measuring once, on the clock that is
  serialized, is the P3-03/P4-04 line ("re-run the check only after the exact transform").
- SOURCE evidence is untouched: the upstream flag is kept (idempotent), SOURCE cues/SRT are
  never rewritten (test 700), `source.unresolved_cue_ids` stays the durable record of the
  SOURCE collapse, exactly as P4-04 fixed it ("the immutable SourceCaptionSet remains the
  evidence").
- One asymmetry to **pin in the record**, because the brief does not decide it: a cue that
  collapsed in SOURCE but is representable in EDIT stays **out** of the EDIT SRT with no
  EDIT-level issue (case d: `srt_empty=True`, `issues=[]`). That is the right line — its exact
  duration is still under a millisecond and the SOURCE verdict is the durable one — but the
  block on `final_accepted` for it then comes only from `source.unresolved_cue_ids`, which
  P6-04 must therefore treat as a blocker (its brief does, in the "draft unresolved …
  warnings" bullet). State it in one sentence in the P6-03 brief or DECISIONS so P6-04 and
  P7-04 do not read the empty EDIT issue list as "nothing unresolved".
- Not a P6-03 defect but worth knowing: P6-03 does not cross-check
  `SUBMS_COLLAPSED ∈ flags ⇒ cue_id ∈ source.unresolved_cue_ids` (the tests hand-build the
  inconsistent pair). It is the source's internal invariant, not the plan/source seam; optional
  fourth identity check.

## P7 빈칸: 지금 채울 것 하나, 나중 것 둘

1. **No record encoder for `EditPlan` / `EditCaptionSet` / `ProjectCue` / `ReviewIssue`.**
   All carry `Fraction` and `FrameInterval`; the only JSON writers in the package are the
   config hash and the boundary transcript. P6-04 must write `project_trace.json` and
   `review.json` from these; P7-01 must read the revision and the typed
   `unavailable_instances` back and validate them; P7-02/P7-03 will surface them. If P6-04
   encodes privately, P7-01 reinvents the decoding. Fill now, in the P6-04 brief: one public
   pair, e.g. `plan_record(plan) -> dict` and `edit_captions_record(captions) -> dict`, with
   `Fraction` as `"num/den"` and `FrameInterval` as `[start, end]`, and P7-01 decodes only
   through them. This is the last chance to make one encoding, not two.
2. **Determinism for P7-01's revision reuse is already secured**: P6-02b canonicalises
   `plan.decisions` in partition block order (`_plan_revision`, `edit.py:1385`), so "same saved
   inputs → same revision" holds regardless of how P7 persists decision order. Nothing to add.
3. **P7-04 does not consume `EditCaptionSet`.** Reproject re-runs the SOURCE projection on the
   user's recut document (P4 machinery, `ProjectCue`), never the EDIT projection; spec line 57
   ("never re-runs vision/edit policy"). So P6-03 leaves no P7-04 blank. `EditCaptionSet` needs
   no `input_sha256`: P6-04 already pins `source.input_sha256 == plan.input_sha256 ==
   document.input_sha256` and P6-03 pinned plan against source.

## Opus 브리프 검토: 충분, 놓친 구멍 둘

The brief's four failing-first tests and the Fable section were sufficient for the
implementation to land the run rule, the identity triple, and the boundary asymmetry
correctly; refuting nothing there. Missed:

1. **Check order.** The brief says "reject a cue intersecting a removed interval" and "run 밖은
   전부 cue ∩ removed 하드 실패" but does not say the removal check must run
   **unconditionally, before** run containment. The implementation read it as a fallback and
   that is exactly the conditional-truth hole above. Add: "`removed` is consulted for every cue
   first; run containment second; P6-03 never assumes `runs` and `removed` are disjoint
   because it cannot call `validate_edit_plan`."
2. **The collapsed-in-SOURCE, representable-in-EDIT case** is undecided in the brief ("preserve
   without duplication" says what not to do, not what to emit). The implementation's choice
   (omit, no EDIT issue, SOURCE evidence blocks acceptance) is right and should be written down.

## 최소 수정 명세 (P6-03b, `captions.py` + `test_edit_captions.py`, additive)

1. `_single_instance_shift`: call `_require_not_removed(...)` **before** the run loop. The
   "no run covers this cue" diagnostic stays as the second failure. No other behaviour change;
   all 93 tests stay green (the removed check is strictly a subset of "not inside any run" for
   consistent plans).
2. Test: forged plan (`replace(plan, runs=(one whole-clip run,))`, `removed` untouched) with a
   cue over the removal → `ValueError` mentioning `제거 구간과 겹친다`. This is the
   machine-checked form of the P6-02 sentence.
3. Docs only: P6-04 brief marker owner "retained" → "run"; add the record-encoder pair (P7
   item 1); one sentence in the P6-03 brief or DECISIONS pinning the collapse asymmetry.

Nothing above touches the plan, the SOURCE projection, ids, or thresholds.
