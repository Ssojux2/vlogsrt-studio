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
    pcm_analysis_key: str
    source_start: Fraction
    source_end: Fraction
    sample_range: SampleRange
    source_segment_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class UnavailableInstance:
    resource_scoped_id: str
    instance_id: str
    reason_code: Literal[
        "declared_no_audio",
        "stream_unavailable",
        "pcm_unavailable",
    ]

@dataclass(frozen=True, slots=True)
class BoundaryPlan:
    clamped_cues: tuple[ProjectCue, ...]
    requests: tuple[BoundaryRequest, ...]
    locked_instance_ids: frozenset[str]
    unavailable_instances: tuple[UnavailableInstance, ...]

def plan_boundaries(slices: Sequence[LogicalSlice], source: SourceCaptionSet, traces: Sequence[SourceTrace], references: Sequence[PcmReference]) -> BoundaryPlan: ...
```

- Convert the P3 already repaired/refined `PlacedSourceCue` records to logical-slice `ProjectCue` records while retaining their source segment provenance. If all referenced Whisper dependencies are fully contained by a slice, clamp presentation only; do not request retranscription or rerun refine.
- Resolve each slice by `LogicalSlice.resource_scoped_id` before any content-key trace/reference lookup. An exact record in `source.unavailable_audio` produces one ordered `UnavailableInstance` per participating instance, no clamped cue or request for that resource, and locks those instance IDs. The resource-scoped record wins even when another resource with equal content has a valid shared trace; reject duplicate, unknown, or identity/reason mismatches rather than falling through to that trace.
- Only where no resource-scoped unavailable record exists, index traces and PCM references by validated durable `source_id`, require exactly one matching reference whose `analysis_key` equals the trace's `pcm_analysis_key`, and copy that key into every request. Reject missing, duplicate, ambiguous, or inconsistent linkage; never `zip` independently ordered sequences or infer from paths/stems. A post-PCM `SourceTrace(status="error")` remains that real trace with its reference provenance, emits no boundary request, and locks affected instances; never rewrite it as pre-PCM unavailability.
- Any true segment truncation at an existing input cut starts a request. Repeatedly include every overlapping cue/segment dependency until no range grows, bounded by the retained logical slice.
- `BoundaryRequest.source_start/source_end` and closure/slice intervals are source-file-clock times. P2 `retained_sample_range` accepts decoded-audio-local seconds, so subtract the matching `reference.pcm_sample0_file_time` from both file endpoints before its ceil-start/floor-end conversion. Map actual snapped endpoints back as `Fraction(sample_index, sample_rate) + pcm_sample0_file_time` for retained-byte provenance; keep requested closure bounds distinct from measured retained sample bounds and never feed file times directly to `start * rate` or add origin twice. Merge requests only when source, logical slice/instances, audible policy, and ranges are compatible.
- Continuous authored blades already coalesced by P4-01 cause zero boundary requests. No guessed tolerance and no word timing.
- Every request locks all participating instance IDs against further automatic shortening for this run.
- `UnavailableInstance(reason_code="declared_no_audio")` is completed/non-billable absence. `stream_unavailable` and `pcm_unavailable` are retryable failures that keep unrelated healthy drafts but block final acceptance downstream. All three grant zero removal authority and must remain typed metadata, not prose reconstructed from a missing cue.

## Implementation steps

1. Write failing tests for padding-only clamp, true leading/trailing truncation, transitive overlap closure, retained-slice bounds, compatible/incompatible merge, repeated instances, continuous blade, shuffled trace/reference order, missing/duplicate/mismatched linkage, positive and negative PCM origins with exact snapped roundtrip, requested-versus-measured bounds, and zero-sample range failure. Add resource-first declared-no-audio and retryable pre-PCM unavailable cases, equal-content healthy/unavailable resources in both input orders, duplicate/unknown unavailable rejection, exact typed propagation/locking with zero request, and a genuine post-PCM error trace that retains reference linkage without being converted to `UnavailableInstance`.
2. Implement deterministic interval closure without mutation or model access.
3. Run focused/all P4/invariant/full checks; commit owned files.

## Acceptance

Closure is idempotent; requests never include removed audio; continuous blades produce no extra task; repeated instance request names only that instance/slice. Every resource-scoped unavailable instance is locked and carried exactly once without suppressing an equal-content healthy resource, while post-PCM error evidence remains distinct. `make check` passes.

## Failure route

Report slice/request/segment IDs, interval evolution per fixed-point iteration, sample indices, base/tests/diff. Never introduce a tolerance or pull context outside retained bounds. Escalate unresolved semantics.

## Report

Report Orca/launch IDs, branch/base/commit, files, closure cases/tests, lock set, and `ready-to-integrate`.
