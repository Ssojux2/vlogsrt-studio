# Task P6-03: project EDIT captions through the EditPlan

## Authority

Binding spec `Input and output`, clock/SRT rules, and P6; depends on P6-02 and P4 boundary projection. This task must keep SOURCE SRT immutable.

## Baseline and ownership

- Fresh `fcp-p6-edit-captions` worktree from exact P6-02-integrated SHA.
- Modify `vlogsrt/project/captions.py`; create `tests/project/test_edit_captions.py`.
- No edit-plan mutation, XML/package output, source trace/SOURCE SRT changes, or network.

## Contract

```python
@dataclass(frozen=True, slots=True)
class EditCaptionSet:
    plan_revision: str
    cues: tuple[ProjectCue, ...]
    srt_text: str
    review: tuple[ReviewIssue, ...]

def project_edit_captions(plan: EditPlan, source: BoundaryProjection, frame_duration: Fraction) -> EditCaptionSet: ...
```

- Reject plan/source mixed revisions or a cue intersecting a removed interval; guard correctness means no new partial cue should need text synthesis/retranscription here.
- Map retained input timeline frames to ripple output frames exactly. EDIT SRT is elapsed zero-based and uses one half-up millisecond serialization at the end.
- Boundary/source text and provenance remain unchanged. Clip-end cue ends at the exact retained slice endpoint; do not invent a one-frame gap.
- Reuse the authoritative exact `SourceCue`/`ProjectCue` representation produced from the immutable refined cue plus its exact input-timeline transform. EDIT applies only the ripple-output transform; it never recomputes the same cue from raw segments, settled-spine floats, or another serialization path.
- Adjacent cue end/start may be equal; each cue must have positive exact duration and be ordered/non-overlapping.
- SOURCE cue objects/text/SRT remain byte-identical before/after this function.
- Preserve the plan's structured `ReviewIssue` values byte-for-byte and in order; never parse `message` prose for an instance or timestamp. Caption-specific review evidence uses the same paired location rule (`instance_id` plus exact source frame, or both `None`) so P6-04 receives explicit marker provenance.

## Implementation steps

1. Write failing tests for ripple across multiple blocks/gaps, exact clip end, adjacent boundaries, removed-cue rejection, repeated instances, boundary provenance, mixed revision, structured review preservation, and source immutability.
2. Implement pure mapping/serialization via existing timebase/caption functions.
3. Run focused P6/P4/P3 tests, invariants, full check; commit owned files.

## Acceptance

EDIT cues match exact output frames and plan revision; there is no cue/removed overlap or cumulative ms drift; SOURCE output hash is unchanged. `make check` passes.

## Failure route

Report plan revision, cue/instance and exact frame/Fraction mapping, expected/actual SRT boundary, base/tests/diff. Never trim text or add a frame gap. Silent cue loss is a hard escalation.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, SOURCE before/after hash, EDIT cue/duration evidence, and `ready-to-integrate`.
