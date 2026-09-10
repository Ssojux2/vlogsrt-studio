# P5-03 direction verdict: guard union + instance projection, candidate f2a798f on base daadf29

Read-only. Nothing edited, staged, or committed. Evidence: spec `VAD guard`, the P5-03 brief,
the P4-04 verdict, the P5-05b brief, the P6-01/P6-02 briefs, `guard.py`, `test_guard.py`, and
the shipped upstream types (`VadResult`, `PcmReference`, `SourceTrace`, `LogicalSlice`,
`BoundaryProjection`, `interval_outward`, `ProjectConfig`). Plus a probe script run against the
worktree package (`PYTHONPATH=<worktree>`), whose raw output is reproduced verbatim below.
The 27 tests in `tests/project/test_guard.py` pass in this worktree.

## 방향 판정: SOUND_WITH_CONCERNS

The direction is right and matches the spec: a union that only grows, `keep_all` on every
failure, the origin added exactly once, the anchor-0 source grid, one guard per instance. I found
**no derived time anywhere** — the arithmetic side of the P4-04b lesson was applied correctly.

What the brief missed is the **identity side** of the same lesson and the **P5-05b lesson**,
which was closed six commits earlier on this very base:

1. The named linkage validations (`source_id`, `total_samples == sample_count`, positive
   `sample_rate`) do not pin the PCM generation or its origin. `project_guard` accepts a
   reference with a different `pcm_sample0_file_time` and silently shifts every frame (probe f:
   a 1 s origin change moves frames 12→37 with no error).
2. `VadResult` carries only `source_id`, so `build_source_guard` **cannot** verify the VAD
   windows came from this PCM generation (probe g). This is the P4-04 shape exactly: the
   consumer needs an upstream exact value that the contract does not carry.
3. Config and input can silently **narrow** the guard with `status="success"` (probes a–d3).
   A negative `guard_padding_seconds` erases even the Whisper segments (`protected=[]`).

None of these is a bug against the brief — the implementation does what the brief says. They
are contract holes. Fix before the P5 merge, because P6-01's brief hard-codes the
`InstanceGuard` shape.

## 계약 대 명세

- Spec "union of ALL Whisper segments" vs brief "successful boundary cue": `BoundaryProjection`
  carries boundary **cues** only, not boundary drafts/spines/repairs. **Not material, by
  construction**: every boundary request is `_seed_span`/`_closure` over source cue/segment
  spans clamped to the slice, so request ⊆ union(source segments ∪ source cues), and every
  boundary cue/segment lies inside its request's retained audio. The boundary input therefore
  adds nothing to the union; its only real use is the `BOUNDARY_UNRESOLVED` review count.
  `boundary: BoundaryProjection` is sufficient — refuting my own initial suspicion here.
- Spec "outward frame floor/ceil", "origin exactly once", "anchor 0 ≡ asset anchor": all
  matched. The two-formula equivalence in the spec is an algebraic identity
  (`(asset_start + f − asset_start)/fd == f/fd`), so `asset_start` is not needed by the guard
  at all; the brief's worry about it was about a *wrong* formula, which the code does not use.
- Spec "quantify protected proportion": `protected_samples/total_samples` present.
- Spec "keep_all on failure": trace error, VAD error, empty VAD, empty coverage → keep_all. OK.
- Spec "Disabled/video-only … no removal authority": `audible = has_audio and enabled and
  src_enable == "all"` (boundary.py:304) → whole-slice protection. Consistent with P6-01.
- Not a P5-03 defect, noted for the pilot: the spec's flat `>= 0.35` per 32 ms window drops
  Silero's own hysteresis (`neg_threshold = 0.20`). With 0.5 s padding both sides a hole needs
  ≥ 1.0 s of continuous sub-0.35 windows, so the practical exposure is faint speech hovering in
  0.20–0.35 for over a second mid-utterance. Spec-level choice; audit it on the pilot as planned.

## 시그니처 충분성

- `frame_duration: Fraction` loose: acceptable. Ingest rejects any asset format whose
  `frameDuration` differs from the sequence's (fcpxml.py:399), so there is one grid per
  document. But `InstanceGuard` does not record which grid it is on, so P6 cannot reject a guard
  built on another grid. Cheap to carry; optional.
- `asset_start`: correctly **not** consumed (see identity above). Nothing reconstructed.
- `boundary: BoundaryProjection`: sufficient (closure argument above).
- `repairs: Sequence[Repair]`: redundant with `trace.repairs` and has **no declared clock**.
  `SourceTrace.repairs` are decoded-audio-local; `BoundaryTranscript.repairs` are
  retained-range-local (repair.settle ran with `duration=run.local_duration`). If a caller ever
  passes boundary repairs here they are placed at the wrong offset. Cannot narrow (extra
  protection only; the true region is inside the closure anyway), but it is an ambiguous slot.
  Drop it or state the clock.
- **`VadResult` lacks `analysis_key`/`pcm_sha256`/`sample_count`** → only `source_id` can be
  matched. P4 pairs traces by `(source_id, pcm_analysis_key)`; P5-02 left the key out. Probe g:
  a hand `VadResult` with `source_id` only is accepted.
- **`SourceGuard` lacks `analysis_key` and `pcm_sample0_file_time`** → `project_guard` cannot
  detect a stale/foreign reference with equal `sample_count`. Probe f: accepted, frames shifted.
  This is the direct analog of `_slice_timeline_shift`: the value that determines timing is not
  carried across the seam.
- No derivation or estimation of any time found. Origin is subtracted once for boundary cues
  (`_boundary_spans`) and added once in projection (`_slice_frames`), on exact Fractions.

## 보수성 — guard를 좁힐 수 있는 경로 (all reproduced, all `status="success"`)

```
baseline (spine [1,2) + vad [4,5)): status=success protected=[(8000, 40000), (56000, 88000)] review=1
(a)  vad_positive=nan:           status=success protected=[(8000, 40000)] review=0
(a2) vad_positive=1.5:           status=success protected=[(8000, 40000)] review=0
(b)  guard_padding_seconds=0:    status=success protected=[(16000, 32000), (64000, 80000)] review=1
(b)  guard_padding_seconds=-1/2: status=success protected=[] review=1
(b)  guard_padding_seconds=-2:   status=success protected=[] review=1
(c)  window prob=nan:            status=success protected=[(8000, 40000)] review=0
(d)  windows cover [0,2s) only:  status=success protected=[(8000, 40000)] review=0
(d2) inverted window:            status=success protected=[(8000, 40000)] review=0
(d3) window past end:            status=success protected=[(8000, 40000)] review=0
(e)  keep_all slice [9,12) on 10s PCM -> (225, 250)   (slice frames 225..300)
(e2) inaudible same slice        -> (225, 300)
(f)  true origin frames:          (12, 63), (87, 138)
(f)  stale-origin reference accepted -> (37, 88), (112, 163)
(g)  vad with no key linkage:    status=success protected=[(8000, 40000)] review=0
(h)  coverage records cover only [0,5s): status=success (gap [5,10) unprotected)
```

- (a)(a2)(b): programmatic `ProjectConfig` skips `_validate_table`, exactly the P5-05b defect.
  `policy.py` already has `_require_config`; `guard.py` has none. Negative padding **shrinks
  Whisper segments themselves** — the union stops being a union.
- (c)(d)(d2)(d3)(g): `_vad_runs` only tests `probability >= vad_positive`; NaN fails the
  comparison, an inverted window is dropped by `_merge`, an out-of-range window is clamped,
  and a window set covering only part of the PCM treats the rest as non-speech. Inference
  guarantees none of these for a live result, but a persisted/reloaded result (P7 reuses
  analyses across resumes) or a stale generation has no defence.
- (e): `keep_all` protects `PCM ∩ slice`, inaudible protects the whole slice. A slice that
  extends past the measured PCM (video longer than audio, or duration mismatch) leaves that
  tail cuttable under `keep_all`. Inconsistent with the word keep-all; cheap to align.
- (h): P3 rule 4 guarantees chunk coverage tiles the audio, and `SourceTrace` does not validate
  it. Low risk; optional tiling check.

## 하류 영향 (P6-01)

`InstanceGuard.protected_frames` is what P6 needs: per instance, source grid, half-open,
sorted, merged, clipped to the slice. P6 can take the complement inside the clip's own retained
interval directly, no reconversion. Three gaps, none blocking geometry:

- **No typed status.** P6-01 says "only when all required analysis succeeded", P6-02 wants
  structured `ReviewIssue.code`s, but `InstanceGuard` exposes `keep_all`/inaudible only as
  whole-slice geometry plus Korean prose in `review`. P6 would parse prose or infer from
  extent. Add `status: Literal["success", "keep_all", "inaudible"]`.
- Coalesced slices: every instance of a slice carries the **whole slice's** frames, not its
  blade's. Correct, but P6 must derive clip bounds from the document, never from guard extent.
  P6-01 already says so; keep it that way.
- No `source_id` on `InstanceGuard`; matching is by `instance_id` alone. Free evidence to carry.

## 의존성·순서

Base `daadf29` contains P4 closed (`cb6b039`, `93e62b7`) and P5-00/01/02/04/05/05b
(`05a7455`, `14b60bf`). VAD branch 01→02→03 and vision branch 04→05→05b both end here, so
"P5-03 integration closes P5" is correct **as a dependency statement**. But the fixes below
touch `InstanceGuard`/`SourceGuard` shapes that P6-01/P6-02 briefs hard-code, and one touches
P5-02's `inference.py`. Do them as P5-03b **before** the P5 merge and before P6-01 dispatch;
otherwise P6 pays for a re-cut.

## Opus 브리프 검토

- Sufficient: the arithmetic clauses (origin once, anchor 0, exact Fractions, no raw-endpoint
  asset anchor, no literals in bodies). All honoured; the negative-index regression test exists.
- Refuted: "Validate `source_id`, `total_samples == sample_count`, positive `sample_rate` …
  reject missing/ambiguous linkage." Those three do not make linkage unambiguous (probe f).
- Missed: the P5-05b lesson. The brief demands thresholds live in config but not that the guard
  refuse a config or input that disables the comparison. The bypass is identical in shape.
- Missed: `VadResult` cannot be pinned to a PCM generation; the brief could not ask for a
  check P5-02's contract does not support. That is the P4-04 hole one layer up.
- Agreed: `frame_duration` as a loose parameter is fine; `BoundaryProjection` alone is enough.

## 최소 수정 명세 (P5-03b; files: `guard.py`, `test_guard.py`, plus `inference.py` + its test for item 4)

1. `guard.py` `_require_config(cfg)`, called first in `build_source_guard`: `vad_positive` a
   real non-`bool` number, `math.isfinite`, in `[0, 1]`; `guard_padding_seconds` a `Fraction`
   `> 0`. Raise `ValueError` naming key and value (config defect = programmer error, the
   P5-05b line). Reuse the existing `_finite`/`_bad_count` idiom.
2. `guard.py` VAD input validation → **keep_all with review** (input defect fails closed, never
   raises): every window has non-`bool` int endpoints with `0 <= start < end <= sample_count`
   and a real finite probability in `[0, 1]`; the union of all windows across channels must
   tile `[0, sample_count)` exactly. Replace the bare `not vad.windows` check with this.
3. `SourceGuard` gains `analysis_key: str` and `pcm_sample0_file_time: Fraction`, copied from
   the reference in both `build_source_guard` and `_keep_all`. `project_guard` requires both to
   equal the reference's. Regression: probe f must raise.
4. `VadResult` (P5-02, `inference.py`) gains `analysis_key: str`, set from
   `reference.analysis_key` on both success and error paths (the reference is in hand in
   `infer_vad`). `_require_vad_linkage` compares it. If reopening P5-02 is refused, say so
   explicitly: item 2's tiling check then closes only length mismatch, not policy/origin
   mismatch.
5. `_slice_frames`: `keep_all` takes the whole-slice path used for inaudible slices.
6. `InstanceGuard` gains `status: Literal["success", "keep_all", "inaudible"]` and
   `source_id: str`; update the P6-01/P6-02 briefs to consume `status` instead of prose.
7. Optional: drop the `repairs` parameter (trace already carries them) or document its clock in
   the signature; add a coverage-tiling check → keep_all.

Tests: one regression per probe line above; the existing 27 tests stay unmodified.
