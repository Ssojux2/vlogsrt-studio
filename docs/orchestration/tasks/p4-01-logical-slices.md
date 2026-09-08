# Task P4-01: derive retained logical slices without losing authored blades

## Authority

Binding spec `Boundary transcripts` and clock formula; depends on Astra's P3 merge. This task defines the slice identity consumed by all boundary tasks.

## Baseline and ownership

- Fresh `fcp-p4-logical-slices` worktree from exact P3 merge SHA.
- Create `vlogsrt/project/boundary.py`, `tests/project/test_logical_slices.py`.
- No transcription, PCM writes, cue replacement, edit shortening, or XML output.

## Contract

```python
@dataclass(frozen=True, slots=True)
class AuthoredBlade:
    instance_id: str
    source_time: Fraction
    timeline_time: Fraction

@dataclass(frozen=True, slots=True)
class LogicalSlice:
    slice_id: str
    instance_ids: tuple[str, ...]
    source_id: str
    source_start: Fraction
    source_end: Fraction
    timeline_start: Fraction
    timeline_end: Fraction
    audible: bool
    blades: tuple[AuthoredBlade, ...]

def logical_slices(document: ProjectDocument, media: Sequence[ResolvedMedia]) -> tuple[LogicalSlice, ...]: ...
```

- For a clip edge, source file start is `clip.start - asset.start`; project elapsed start is `clip.offset - sequence.tcStart`. The full mapping must satisfy `input_project_elapsed = clip.offset - sequence.tcStart + asset.start + file_time - clip.start`.
- Audibility is exactly `media.has_audio and clip.enabled and clip.src_enable == "all"`. A declared-no-audio, disabled, or video-only slice retains identity/blades with `audible=False`, creates no boundary/audio request, and grants no automatic-shortening authority. Join the resource-scoped media record before content coalescing so equal bytes with different `has_audio` declarations cannot share disposition.
- Coalesce adjacent items only when same source content, contiguous source and timeline ranges, and identical audible policy. Preserve every authored XML blade in `blades` even after logical coalescing.
- Gaps break coalescing and remain represented by the original document, not as speech slices.
- Slice IDs hash ordered instance IDs and exact ranges; repeated source instances remain distinct.

## Implementation steps

1. Write failing tests for nonzero epochs, repeated assets, continuous blade coalescing, source-only/timeline-only discontinuity, gaps, muted/disabled/no-audio clips, same filename/different content, and same-content resources with different `has_audio` values.
2. Implement pure derivation in `boundary.py` using Fraction only.
3. Run focused P4/P1 integration tests, invariants, full check; commit owned paths.

## Acceptance

Formula roundtrips at both endpoints for every supported fps; continuous authored blade yields one logical slice with two blade records and causes no extra STT by itself. Muted/gap/repeated cases remain addressable. `make check` passes.

## Failure route

Report item/instance/resource IDs, exact source/timeline Fractions, expected/actual coalescing, base/tests/diff. Do not add tolerance floats. Shared model ambiguity escalates Opus/Astra.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, slice/blade cases, and `ready-to-integrate`.
