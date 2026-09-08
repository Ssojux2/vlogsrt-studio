# Task P4-04: replace boundary cues by slice and source interval

## Authority

Binding spec `Boundary transcripts` and P4 acceptance; depends on P4-03. Ends P4 for Opus phase review and Astra integration.

## Baseline and ownership

- Fresh `fcp-p4-boundary-projection` worktree from exact P4-03-integrated SHA.
- Modify `vlogsrt/project/captions.py`, `vlogsrt/project/boundary.py`; create `tests/project/test_boundary_projection.py`.
- No XML/edit shortening, transcription calls, source trace mutation, or fixture edits.

## Contract

```python
@dataclass(frozen=True, slots=True)
class BoundaryProjection:
    input_sha256: str
    cues: tuple[ProjectCue, ...]
    source_srt: str
    locked_instance_ids: frozenset[str]
    unresolved_request_ids: tuple[str, ...]

def project_boundaries(plan: BoundaryPlan, transcripts: Sequence[BoundaryTranscript], source: SourceCaptionSet, source_traces: Sequence[SourceTrace]) -> BoundaryProjection: ...
```

- Successful boundary trace's already repaired/refined cues are used instead of overlapping source cues for exactly `(slice_id, instance_ids, source interval)`; never append both and never refine/polish either cue set again.
- Join plan requests, boundary transcripts, and source traces by validated request/source/PCM keys, independent of input sequence order. Reject missing, duplicate, ambiguous, or inconsistent linkage and test shuffled transcript/trace order; never pair records with `zip` or infer identity from paths/stems.
- Repeated source instances outside that tuple are unchanged. Every source/boundary segment remains in its immutable trace.
- Empty/error boundary keeps clamped original text, adds `boundary_unresolved` and request ID to review, and remains draft-only until human resolution. Successful replacement regenerates the single pre-roughcut input-timeline SOURCE SRT; per-asset source-local artifacts and both source/boundary traces remain immutable.
- Timeline mapping uses exact P4 formula/Fractions. Output cues are ordered, non-overlapping within the flat storyline, and have `end > start`.
- Locked instances from the plan are returned unchanged; later edit policy must not shorten them again in the same run.

## Implementation steps

1. Write failing tests for one of two repeated instances, exact replace-not-append, shuffled transcript/trace order, missing/duplicate/mismatched linkage, leading/trailing cuts, empty/error fallback, lock propagation, no recursive request, and interval/order validation.
2. Implement pure projection without modifying trace objects.
3. Run all P4/P3 tests, invariants, full check; commit owned files.

## Acceptance

Instance-scoped tests prove no deletion/replacement in the sibling instance; each overlap has exactly one provenance; failed boundaries keep original text and explicit unresolved marker. Continuous blade has zero extra STT. `make check` passes.

## Failure route

Report slice/instance/request/cue IDs and exact intervals, expected/actual provenance, base/tests/diff. Cross-instance deletion or silent missing text is an immediate Astra escalation; fixes stay GLM.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, unresolved cases/locks, source immutability evidence, and `ready-to-integrate`.
