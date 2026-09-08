# Task P4-02: compute fixed-point boundary transcription requests

## Authority

Binding spec `Boundary transcripts`; depends on P4-01 and P3 source trace. This is pure request planning and must not call a model.

## Baseline and ownership

- Fresh `fcp-p4-boundary-closure` worktree from exact P4-01-integrated SHA.
- Modify `vlogsrt/project/boundary.py`; create `tests/project/test_boundary_planning.py`.
- No PCM extraction/transcription, cue replacement, network, or config threshold changes.

## Contract

```python
@dataclass(frozen=True, slots=True)
class ProjectCue:
    cue_id: str
    slice_id: str
    instance_ids: tuple[str, ...]
    source_id: str
    source_start: Fraction
    source_end: Fraction
    timeline_start: Fraction
    timeline_end: Fraction
    text: str
    provenance: Literal["source", "boundary"]
    flags: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class BoundaryRequest:
    request_id: str
    slice_id: str
    instance_ids: tuple[str, ...]
    source_id: str
    source_start: Fraction
    source_end: Fraction
    sample_range: SampleRange
    source_segment_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class BoundaryPlan:
    clamped_cues: tuple[ProjectCue, ...]
    requests: tuple[BoundaryRequest, ...]
    locked_instance_ids: frozenset[str]

def plan_boundaries(slices: Sequence[LogicalSlice], source: SourceCaptionSet, traces: Sequence[SourceTrace], references: Sequence[PcmReference]) -> BoundaryPlan: ...
```

- Convert the P3 already repaired/refined `PlacedSourceCue` records to logical-slice `ProjectCue` records while retaining their source segment provenance. If all referenced Whisper dependencies are fully contained by a slice, clamp presentation only; do not request retranscription or rerun refine.
- Any true segment truncation at an existing input cut starts a request. Repeatedly include every overlapping cue/segment dependency until no range grows, bounded by the retained logical slice.
- Convert final request endpoints with P2 ceil-start/floor-end rules. Merge requests only when source, logical slice/instances, audible policy, and ranges are compatible.
- Continuous authored blades already coalesced by P4-01 cause zero boundary requests. No guessed tolerance and no word timing.
- Every request locks all participating instance IDs against further automatic shortening for this run.

## Implementation steps

1. Write failing tests for padding-only clamp, true leading/trailing truncation, transitive overlap closure, retained-slice bounds, compatible/incompatible merge, repeated instances, continuous blade, and zero-sample range failure.
2. Implement deterministic interval closure without mutation or model access.
3. Run focused/all P4/invariant/full checks; commit owned files.

## Acceptance

Closure is idempotent; requests never include removed audio; continuous blades produce no extra task; repeated instance request names only that instance/slice. `make check` passes.

## Failure route

Report slice/request/segment IDs, interval evolution per fixed-point iteration, sample indices, base/tests/diff. Never introduce a tolerance or pull context outside retained bounds. Escalate unresolved semantics.

## Report

Report Orca/launch IDs, branch/base/commit, files, closure cases/tests, lock set, and `ready-to-integrate`.
