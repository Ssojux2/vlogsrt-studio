# P6 pre-phase brief verdict (Fable 5.1, read-only, before any implementation)

Integration HEAD `50b418f`. Nothing edited, staged, or committed. Evidence: the four P6 briefs,
the binding spec (`Visual scoring and deterministic roughcut`, `Input and output`,
`Persistence, CLI and UI`), CLAUDE.md, my P4-04 and P5-03 verdicts, and the **shipped** types
opened directly: `models.py` (`ProjectDocument`, `ClipInstance`, `GapInstance`, `AssetResource`),
`timebase.py` (`FrameInterval`, `frame_nearest`, `interval_outward`), `media.py` (`ResolvedMedia`),
`boundary.py` (`LogicalSlice`, `ProjectCue`, `BoundaryRequest`, `UnavailableInstance`,
`BoundaryPlan`, `BoundaryProjection`, `_boundary_project_cues`, `_in_scope`), `captions.py`
(`SourceCue`, `PlacedSourceCue`, `_place_instance`, `_split_by_representability`),
`vad/guard.py` (`SourceGuard`, `InstanceGuard`, `project_guard`, `_slice_frames`),
`vision/policy.py` (`VisionCandidate`, `VisionRequest`, `VisionDecision`, `_normalize`,
`_require_config`), `vision/sampling.py` (`FrameSampleRequest`), `config.py` (`ProjectConfig`,
`_validate_cross`, `_FIELD_PARSERS`), `fcpxml.py` (`_require_on_grid` sites),
`scripts/check_invariants.py` (`CHECKS`, `PURE_MODULES`, `BURIED_MODULES`).

## 페이즈 판정: READY_WITH_FIXES

The architecture of the four briefs is right: P6 removes only the complement of a fail-closed
guard, applies at most one typed `accepted` decision per block, and never touches SOURCE. Nothing
here needs a redirect. What is missing is, again, in the contracts: six places where a consumer
would have to reconstruct or infer what the producer already knew exactly, and four places where
a programmatic config or a persisted artifact can widen removal or narrow protection with
`status="success"`. All are field additions and rails; none changes the algorithm.

## 태스크별 판정

| Task | Verdict | Blocking fixes |
|---|---|---|
| P6-01 | READY_WITH_FIXES | candidate frame intervals on the block; content-bound block/candidate/frame ids; `input_sha256`/`config_sha256`/`locked_instance_ids` on `BlockPartition`; `_require_config`; strict guard linkage; audibility from the document |
| P6-02 | READY_WITH_FIXES | gaps in the plan; `locked_instance_ids` + `frame_duration` on `EditPlan`; apply on `status=="accepted"` only; identity→raise / content→keep line; frame conservation in `validate_edit_plan`; unambiguous restore location |
| P6-03 | READY_WITH_FIXES | multi-instance cue rule and no `cue_id` dedupe; `locked_instance_ids` equality; drop the loose `frame_duration` |
| P6-04 | READY_WITH_FIXES | typed `BoundaryProjection` instead of a bare `source_srt` str; build `review.json` itself and own `final_accepted`; call `validate_edit_plan` before rendering |

## 시그니처 충분성 — 재구성이 강요되는 자리

**S1. P6-02 must invert `VisionCandidate.keep_start/keep_end` to find the frames it retains.**
`VisionCandidate` (shipped P5-05, `policy.py:102`) carries block-relative *seconds*
(`keep_start >= 0`, `keep_end > keep_start`, exact Fractions), because it is the model-facing
shape. `EditBlock.candidates: tuple[VisionCandidate, ...]` is all P6-02 receives, so it must
compute `block.source_frames.start_frame + keep_start / frame_duration` and assert integrality.
That is exact and invertible today, and it is the P4-04 shape exactly: the producer (P6-01) had
the `FrameInterval` and threw it away at the seam. Fix: `EditBlock` carries a P6-local
`KeepCandidate(candidate_id, source_frames: FrameInterval, timeline_frames: FrameInterval,
vision: VisionCandidate)` tuple (or a parallel `candidate_frames` tuple keyed by id). P6-01
defines `vision.keep_start = (source_frames.start_frame − block.source_frames.start_frame) ×
frame_duration`; P6-02 looks the chosen id up and never divides.

**S2. Block, candidate, and frame identity is not content-bound, so a saved decision can be
applied to a different geometry.** `VisionDecision` carries only `block_id`, `choice`, and
`evidence_frame_ids` (`policy.py:126`). `build_edit_plan(document, partition, guards, decisions,
cfg)` has no way to tie `decisions` to *this* partition except id equality, and the P6-01 brief
never says how `block_id`/`candidate_id`/`frame_id` are formed. Ordinal ids (`block-3`) would
let a decision recorded against a partition from another `keep_seconds`, another document
revision, or another guard generation pass the "unknown IDs" check and remove the wrong frames.
This is the P5-03 item-2 hole (`VadResult` without `analysis_key`) one phase later. Fix:
`block_id = sha256(document.input_sha256, instance_id, source_id, source_frames,
timeline_frames)`, `candidate_id = sha256(block_id, candidate source_frames)`,
`frame_id = f"{source_id}:{grid_index}"` (or its hash). `BlockPartition` gains `input_sha256`
and `config_sha256`; `build_edit_plan` requires `partition.input_sha256 == document.input_sha256`
and `partition.config_sha256 == cfg.sha256()` before reading a single block. With content-bound
ids, the existing "reject unknown IDs" clause becomes sufficient. (`ProjectDocument.input_sha256`
already exists — `fcpxml.py:140` computes it from `raw_xml`; `ProjectConfig.sha256()` exists.)

**S3. `EditPlan` cannot express the output storyline: gaps are missing, so P6-04 must recompute
the ripple.** `RetainedInterval.source_frames: FrameInterval` is total, and gaps
(`GapInstance`, `models.py:66`) have no source frames, so gaps cannot be `RetainedInterval`s.
The P6-04 brief says "existing gaps ... preserved" and needs an output `offset` for every
storyline item, which means P6-04 would interleave document gaps and re-derive their output
positions — a second implementation of the ripple next to P6-02's. Fix: `EditPlan.gaps:
tuple[RetainedGap(instance_id, input_timeline_frames, output_timeline_frames), ...]`, and
`validate_edit_plan` requires `retained ∪ gaps` output intervals to tile `[0, output_end)`
exactly, non-overlapping, in input order. P6-04 then only looks up.

**S4. `locked_instance_ids` are dropped after P6-01 and cannot be checked downstream.**
`partition_blocks` consumes them; `BlockPartition`, `EditPlan`, and `validate_edit_plan` do not
carry them. So "never shorten ... locked instances" (P6-02) is unverifiable by the validator, and
P6-03 has `source.locked_instance_ids` (`BoundaryProjection`, `boundary.py:1934`) with nothing
on the plan side to compare. Fix: `BlockPartition.locked_instance_ids: frozenset[str]` and
`EditPlan.locked_instance_ids: frozenset[str]` copied through; `validate_edit_plan` rejects any
`RemovedInterval` whose `instance_id` is locked; P6-03 requires
`plan.locked_instance_ids == source.locked_instance_ids` next to the existing
`unavailable_instances` equality.

**S5. `project_edit_captions(plan, source, frame_duration)` takes the grid loose and nothing
records it.** Neither `EditPlan` nor `BoundaryProjection` carries `frame_duration`. Fix:
`EditPlan.frame_duration: Fraction` (from `document.frame_duration`), and P6-03 drops the
parameter. State the timeline-grid anchor once in the P6-02 brief: index `m` means input
elapsed `m × frame_duration` with `sequence.tcStart` already subtracted (ingest enforces every
`offset` on that grid, `fcpxml.py:575/612`, and every clip `start` on the asset grid, `:577`).

**S6. `publish_package(..., source_srt: str, review: Mapping[str, object])` is untyped where it
matters most.** A bare `str` cannot be verified as `BoundaryProjection.source_srt` of the same
`input_sha256`; and the brief asks P6-04 to check an arbitrary caller mapping "does not omit or
contradict" typed plan data — that mapping is where a `final_accepted: True` or a dropped
unavailable record would enter. Fix: `publish_package(destination, document, media, plan,
edit_captions, source: BoundaryProjection, *, dtd_path)`; P6-04 builds `review.json` and
`project_trace.json` itself from `plan.review`, `edit_captions.review`,
`source.unresolved_request_ids/unresolved_cue_ids`, and `plan.unavailable_instances`; computes
`final_accepted` itself; accepts no external review mapping (at most `notes: tuple[str, ...]`).
`render_edit_fcpxml` must call `validate_edit_plan` first.

**S7. `RemovedInterval.restore_before_instance_id` is ambiguous when one instance holds several
removed ranges** (the normal case: one block per ≤10 s of a long clip). Fix: replace with or add
`restore_at_output_frame: int` — the output-timeline frame at which the removed range re-inserts
(= output start of the retained interval that follows it). Exact, unique, and what
`project_trace.json` needs.

## 적대적 설정·입력 — 넓히거나 좁힐 수 있는 경로. raise/keep 선의 위치

Reproduced by reading the shipped validators, not by running P6 (it does not exist yet).
`ProjectConfig` is a frozen dataclass whose range and cross checks (`_validate_table`,
`_validate_cross`, `config.py:163–200`) run **only in `load_project_config`**. A programmatic
`ProjectConfig(...)` skips all of them — the P5-05b/P5-03b bypass, unchanged. No P6 brief asks
for a `_require_config`. The paths that widen removal:

| Config value | Effect in P6-01/P6-02 as briefed |
|---|---|
| `keep_seconds = Fraction(0)` → `keep_frames = 0` | candidate is empty; applying it removes the **whole block**, and for a clip that is one block the whole clip. "Never a full-delete candidate" is asserted, not enforced |
| `keep_seconds > block_min_seconds` (cross check bypassed) | candidate longer than block; the brief's "exactly `keep_frames` when possible" clause becomes the failure path |
| `block_min_seconds = Fraction(1, 1000)` | one-frame runs become blocks |
| `block_max_seconds = Fraction(600)` | 10-minute blocks judged on ≤16 images; the spec says sparse images prove nothing, and the decision then removes 597 s |
| `vision_max_images < 5` | cannot hold 2 endpoints + 3 centers; "reduce without dropping mandatory samples" is unsatisfiable |
| `vision_sample_fps = Fraction(0)` | division by zero or zero nominal samples |
| `vision_apply_confidence` NaN at plan time | if P6-02 re-derives acceptance from `confidence >= cfg…` instead of `status`, NaN disables the comparison exactly as in P5-05b |

The rails, as `_require_config(cfg)` first in both `partition_blocks` and `build_edit_plan`
(config defect = programmer error → `ValueError`/`ProjectConfigError`, never keep+review, the
P5-05b line): `block_min_seconds`, `block_max_seconds`, `keep_seconds`, `vision_sample_fps` are
non-bool `Fraction > 0`; `vision_max_images` non-bool `int >= 5`; after frame conversion
`1 <= keep_frames <= min_frames <= max_frames`; `vision_apply_confidence` finite in `[0, 1]`
(reuse `policy._require_config`); and one cross rail against sparse evidence,
`block_max_seconds × vision_sample_fps <= vision_max_images` (defaults: 10 ≤ 16). That last one
is a spec-level judgment — the spec fixes ≤10 s and ≤16 images without stating the relation —
so the coordinator may keep it or drop it; the other rails are not optional.

Input side (persisted analysis = input defect → keep + review, never raise, the P5-03b line),
with one deliberate exception:

- **Decisions.** Apply a candidate **iff** `decision.status == "accepted"` **and** `choice` is a
  candidate id of *that* block **and** `math.isfinite(confidence) and confidence >=
  cfg.vision_apply_confidence`. The re-check can only narrow (status was fixed at parse time by
  `_normalize`, `policy.py:424–457`); never re-derive acceptance from confidence or reason alone.
  `status="accepted"` with `choice in {keep_all, uncertain}`, a non-finite confidence, or two
  decisions for one block → keep-all + `ReviewIssue(code="decision_inconsistent")`.
- **The exception — identity mismatch raises.** A decision whose `block_id` is not in the
  partition, or whose `choice` is a candidate of another block, or a partition whose
  `input_sha256`/`config_sha256` differs from `document`/`cfg`, is a *mixed revision*, not a
  model defect. Raise, the way P7-01 raises `StaleRevision`. Laundering that into keep+review
  would let a stale decision set produce a plausible "successful" plan.
- **Guards (P6-01).** Exactly one `InstanceGuard` per document clip instance, matched on both
  `instance_id` and `source_id == media.asset_content_id` of the clip's resource; missing,
  duplicate, or extra guards, and a guard whose `source_id` disagrees with the document, are
  linkage errors → raise. Otherwise "no guard" silently means "no protection", the
  `protected=[]` failure in a new coat. Intersect `protected_frames` with the clip's own
  source-grid interval before complementing (coalesced slices carry whole-slice frames).
- **Audibility from the document, not the guard.** P6-01 must compute audible exactly as P4-01
  does (`media.has_audio and clip.enabled and clip.src_enable == "all"`, `boundary.py` module
  docstring) and treat a `status="success"` guard on an inaudible instance as a linkage error,
  never as eligibility. The brief's "only declared-no-audio is authored inaudible" is about
  `UnavailableInstance`, not about this check.
- **`review` mapping in P6-04** — removed by S6. `final_accepted` is computed only by P6-04,
  from typed inputs.

## 페이즈 내 이음매 — 재변환·신원 단절

- P6-01 → P6-02: seconds→frames inversion (S1); no partition hash (S2); locked set dropped (S4).
- P6-02 → P6-03: no `frame_duration` on the plan (S5); no locked set (S4); the rule for
  assigning a cue to a retained interval is unstated (next section).
- P6-03 → P6-04: gaps absent so the ripple would be recomputed (S3); `source_srt` as a bare str
  (S6); `restore_before_instance_id` ambiguous (S7).
- Revision chain: `EditPlan.revision` → `EditCaptionSet.plan_revision` → `ExportPackage.
  plan_revision` is intact once the inputs are bound. `input_sha256` exists on
  `ProjectDocument`, `SourceCaptionSet`, and `BoundaryProjection`; the P6-03 mixed-revision check
  (`plan.input_sha256 == source.input_sha256`) is well-founded.
- Sample reduction to ≤16 needs a stated deterministic thinning rule (which nominal samples go,
  in which order); otherwise two correct implementations yield different `frame_ids` and
  different revisions.

## 상류 부족 — P4/P5가 안 싣는 값. 지금 고칠 것

1. **`VisionDecision` has no request identity.** `parse_decision(request, raw, cfg)` holds the
   request; adding `request_sha256: str` (hash of `block_id`, `frame_ids`, candidate ids and
   windows) to `VisionDecision` on every path is a one-line P5-05 reopen of the P5-03b item-4
   kind. With it, P6-02 can prove a decision saw *these* frames and candidates rather than trust
   id discipline. Recommended now; S2's content-bound ids are the fallback if refused.
2. **`ProjectConfig` has no `__post_init__`.** Three layers (`policy.py`, `guard.py`, and now
   `edit.py`) each re-implement the range/cross checks because the frozen dataclass can be built
   around `load_project_config`. Running `_validate_cross` plus the per-field range parsers in
   `__post_init__` closes the bypass for every consumer at once. Recommended; the P6 rails above
   remain required either way (they also cover frame-converted values).
3. `InstanceGuard` still carries no generation key (P5-03 verdict, optional then, optional now):
   the strict one-guard-per-instance linkage in P6-01 plus P7's revision keys cover it.
4. Nothing else is missing: `ProjectDocument.input_sha256`, `ProjectConfig.sha256()`,
   `BoundaryProjection.locked_instance_ids/unavailable_instances/unresolved_*`,
   `InstanceGuard.status/source_id`, `UnavailableInstance` (`boundary.py:435`), and on-grid clip
   coordinates from ingest are all present and sufficient.

## 비대칭 처리 — P6-03이 straddling cue를 다루는가

Confirmed against the shipped code. A **source** cue straddling an internal blade is two
`ProjectCue`s with the same `cue_id`, `instance_ids=(A,)` and `(B,)`, timeline clamped per
blade (`captions._place_instance`, `boundary._convert_placed` line 688). A **boundary** cue
spanning blades is one `ProjectCue` with `instance_ids=request.instance_ids` and an unclamped
timeline (`_boundary_project_cues`, `boundary.py:2120`). The P6-03 brief handles neither: it
says "map retained input timeline frames to ripple output frames" without saying how a cue is
attached to a retained interval, and its `subms_collapsed` message "names the cue ID", which is
ambiguous for the two-placement case. Required in the brief:

- EDIT identity is `(cue_id, instance_ids)`. Never dedupe or index by `cue_id` alone
  (`BoundaryProjection.unresolved_cue_ids` dedupes by id — fine for an id list, wrong for cues).
  The collapse review message names both.
- Single-instance cue: it must lie wholly inside exactly one `RetainedInterval` of that
  instance (guard ⊆ retained guarantees this); attach by containment; shift by
  `(output.start − input.start) × frame_duration`. Any other outcome is the existing
  "cue intersects a removed interval" hard failure.
- Multi-instance cue: every instance in `instance_ids` is locked (`boundary.py:973` locks all
  request instances), so each must have exactly one whole-instance retained interval; those
  intervals must be consecutive with an identical shift and the cue inside their union. Verify
  all three and fail otherwise — never attach by `instance_ids[0]`.
- The "clip-end cue ends at the exact retained slice endpoint" rule applies per retained
  interval, so a source cue's second placement starts exactly where the first ends after ripple.

## 규칙 4·9 준수 — 제거 계층에서 어떻게 지켜지는가

P6 is the first layer that removes audio, and CLAUDE.md rule 4 forbids code that decides "this
span is not speech". The briefs keep that line, but only implicitly: P6-01 never reads audio,
transcripts, VAD, or `review` prose; it takes the complement of a typed, fail-closed
`InstanceGuard` inside document clip bounds. The judgment "might be speech" was made upstream as
"protected", conservatively (union + padding, keep_all on any failure). P6 removes only what the
guard did not claim and only where a typed decision says the pictures repeat. Rule 9 holds
because every removed frame lands in `plan.removed` with a restore location, in `review.json`,
and in the trace; no cue is ever dropped (cue ∩ removed is a hard failure; SOURCE SRT is
byte-identical); nothing is emptied. Two additions make this checkable instead of asserted:

- **Runtime, in `validate_edit_plan`: frame conservation.** For every clip instance,
  `Σ retained + Σ removed == clip input frames`, retained/removed disjoint, removed ⊆ some
  block's frames, removed ∩ protected = ∅, no removed frame in a locked or unavailable instance,
  every instance retains ≥ 1 frame. Nothing disappears unaccounted.
- **Static, in `check_invariants.py` (new check, "제거 계층은 오디오를 듣지 않는다"):**
  `vlogsrt/project/edit.py` and `vlogsrt/project/export.py` must not import `subprocess`,
  `openai`, `numpy`, `onnxruntime`, `vlogsrt.transcribe`, `vlogsrt.preprocess`,
  `vlogsrt.project.pcm`, `vlogsrt.project.speech`, `vlogsrt.project.vad.inference`,
  `vlogsrt.project.vad.runtime`, `vlogsrt.project.vision.sampling`, or the legacy judgment
  modules `vlogsrt.repetition`/`repair`/`refine`/`output`/`layout`. Type imports from
  `vad.guard` and `vision.policy` stay allowed. Same exact-path idiom as
  `check_repair_stays_upstream`. `export.py` additionally must not import `vad` or `vision`.
  Existing checks (`check_project_models_frozen`, subprocess boundaries, model literals) already
  cover P6's new dataclasses and its xmllint use through `xml_validation`.

## Opus 브리프 검토 — 충분 / 놓친 구멍

The inserted `InstanceGuard` block is right and sufficient for what it covers: branch on
`status`, never parse `review`, take clip bounds from the document, match on both ids. Refuting
nothing there. Missed, in order of consequence:

1. It says how to read a guard but not what to do when one is **absent, duplicated, extra, or
   contradicts the document's audibility** — the `protected=[]` failure's successor is "no guard
   for this instance" (above, guards bullet).
2. It does not say to **intersect** `protected_frames` with the clip's own interval before
   complementing; "derive clip bounds from the document" is necessary but not the same statement.
3. It hard-codes the guard shape into both briefs but leaves `EditBlock`/`BlockPartition`
   without the identity fields (S2, S4) that would let P6-02 refuse a guard-generation mismatch
   the way P5-03b taught P5 to.

## 최소 수정 명세

**P6-01 (`edit.py`, `test_block_partition.py`)**
1. `EditBlock` gains `keep_candidates: tuple[KeepCandidate, ...]` where
   `KeepCandidate(candidate_id, source_frames, timeline_frames, vision: VisionCandidate)`;
   `vision.keep_start/keep_end` are block-relative seconds derived from the frame interval, never
   the reverse. `candidates` may remain as the projection `tuple(k.vision for k in
   keep_candidates)` or be dropped.
2. `BlockPartition` gains `input_sha256: str`, `config_sha256: str`,
   `locked_instance_ids: frozenset[str]`.
3. Ids are content-bound: `block_id`, `candidate_id`, `frame_id` as in S2. State the formula.
4. `_require_config(cfg)` first, with the rails listed above; config defects raise.
5. Guard linkage: exactly one guard per clip instance, matched on `instance_id` and
   `source_id`; missing/duplicate/extra/mismatched → raise. Audibility computed from the
   document; `success` on an inaudible instance → raise. Intersect protected with the clip
   interval before complementing.
6. State the deterministic ≤16 thinning rule and the middle-candidate center rounding.
7. Tests add: each rail (programmatic `ProjectConfig`, one per row of the table), missing and
   duplicate guard, guard `source_id` mismatch, `success` guard on a muted/video-only clip,
   whole-slice protected frames on a coalesced two-blade slice, id stability across two runs and
   id change under a different `keep_seconds`.

**P6-02 (`edit.py`, `test_edit_plan.py`)**
1. `EditPlan` gains `frame_duration: Fraction`, `locked_instance_ids: frozenset[str]`,
   `gaps: tuple[RetainedGap, ...]`; `RemovedInterval` gains `restore_at_output_frame: int`
   (replace `restore_before_instance_id` or keep both).
2. `build_edit_plan` first requires `partition.input_sha256 == document.input_sha256`,
   `partition.config_sha256 == cfg.sha256()`, then `_require_config(cfg)`.
3. Apply rule and raise/keep line exactly as in the input section. Never re-derive acceptance
   from `confidence`/`reason` alone.
4. `validate_edit_plan` adds: frame conservation per instance, output tiling of
   `retained ∪ gaps`, no removal in locked/unavailable instances, removed ∩ protected = ∅,
   every removed range inside one block, every instance retains ≥ 1 frame, and returns findings
   as `tuple[str, ...]` as briefed (P6-04 treats non-empty as refusal).
5. State the timeline-grid anchor (S5).
6. Tests add: stale partition hash, stale config hash, decision for an unknown block (raise),
   candidate from another block (raise), `accepted` with `keep_all` choice (keep+review),
   NaN confidence in a persisted decision (keep+review), duplicate decisions (keep+review),
   plan-time threshold lower than parse-time (still keep), gap tiling, multi-removal restore
   frames, frame conservation.

**P6-03 (`captions.py`, `test_edit_captions.py`)**
1. Signature `project_edit_captions(plan: EditPlan, source: BoundaryProjection) ->
   EditCaptionSet`; grid from `plan.frame_duration`.
2. Require `plan.locked_instance_ids == source.locked_instance_ids` and
   `plan.input_sha256 == source.input_sha256` next to the existing unavailable equality.
3. Cue attachment rule for single- and multi-instance cues as in the asymmetry section; EDIT
   identity `(cue_id, instance_ids)`; collapse review names both.
4. Tests add: source cue straddling a blade with a removal earlier in the first instance (two
   placements, same `cue_id`, adjacent after ripple, both emitted); boundary cue over two locked
   blades; a hand-built plan that removes inside a locked instance (rejected); mismatched locked
   sets (rejected).

**P6-04 (`export.py`, `fcpxml.py`, `test_export.py`)**
1. `publish_package(destination, document, media, plan, edit_captions,
   source: BoundaryProjection, *, dtd_path=None)`; no `source_srt: str`, no `review` mapping.
   `VLOG_AI_SOURCE.srt` is `source.source_srt`; require `source.input_sha256 ==
   plan.input_sha256 == document.input_sha256`.
2. P6-04 composes `review.json`/`project_trace.json` and computes `final_accepted` itself from
   `plan.review`, `edit_captions.review`, `source.unresolved_*`, and
   `plan.unavailable_instances`; a caller cannot supply or override it.
3. `render_edit_fcpxml` calls `validate_edit_plan(plan, document, guards)` first — which means
   `guards: Sequence[InstanceGuard]` must be a parameter of both functions, or the plan must
   carry the protected frames it was validated against. Prefer the parameter.
4. Output storyline is written from `plan.retained ∪ plan.gaps` by lookup; no ripple
   arithmetic in `export.py`. Test: a gap between two shortened clips keeps its duration and its
   output offset equals the plan's gap `output_timeline_frames.start × frame_duration`.
5. Tests add: caller-supplied `final_accepted` impossible by construction (no parameter), a
   `validate_edit_plan` finding refuses rendering, `source` with a different `input_sha256`
   refused.

**Upstream (optional but recommended now, same base): `VisionDecision.request_sha256` (P5-05
reopen, both `_normalize` and `_error_decision` paths); `ProjectConfig.__post_init__` running
the existing per-field range parsers and `_validate_cross` (P5-00 reopen).**

**`scripts/check_invariants.py`**: add the import-direction check for `edit.py`/`export.py`
described under rules 4·9. It can land with P6-01 (the file exists from then on) and extend to
`export.py` with P6-04.

## 우선순위 — P6-01 디스패치까지

1. P6-01 brief: items 1–6 above. These define `EditBlock`/`BlockPartition`, so every downstream
   fix depends on them; nothing in P6-01 can be dispatched before they are in the brief.
2. P6-02 brief items 1–5 — written now so P6-01's types are shaped for them, even though P6-02
   is dispatched later.
3. Upstream one-liners (`VisionDecision.request_sha256`, `ProjectConfig.__post_init__`) — either
   before P6-01 on the same base or explicitly deferred; if deferred, S2's content-bound ids and
   P6's local `_require_config` are the required fallback and must stay in the briefs.
4. P6-03 and P6-04 brief fixes can follow after P6-01 is dispatched; none changes P6-01's
   contract beyond the fields already in step 1.
