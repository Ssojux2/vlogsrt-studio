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
    resource_scoped_id: str
    source_id: str
    asset_start: Fraction
    source_start: Fraction
    source_end: Fraction
    timeline_start: Fraction
    timeline_end: Fraction
    audible: bool
    blades: tuple[AuthoredBlade, ...]

def logical_slices(document: ProjectDocument, media: Sequence[ResolvedMedia]) -> tuple[LogicalSlice, ...]: ...
```

- For a clip edge, source file start is `clip.start - asset.start`; project elapsed start is `clip.offset - sequence.tcStart`. The full mapping must satisfy `input_project_elapsed = clip.offset - sequence.tcStart + asset.start + file_time - clip.start`.
- `resource_scoped_id` is the exact document-scoped resource join, `source_id` is exactly the joined `ResolvedMedia.asset_content_id`, and `asset_start` is that resource's exact XML epoch. Join by identity rather than sequence order: every referenced clip resource must resolve to exactly one document resource and exactly one `ResolvedMedia`; reject missing, duplicate, unknown-resource, and authored `has_audio` mismatch before deriving any slice. Never zip document and media sequences.
- Audibility is authored input state only: exactly `media.has_audio and clip.enabled and clip.src_enable == "all"`, never the later availability of a source trace, PCM reference, or VAD result. A declared-no-audio, disabled, or video-only slice retains identity/blades with `audible=False`, creates no boundary/audio request, and grants no automatic-shortening authority. A declared-audio resource whose PCM later becomes unavailable remains `audible=True`; P4-02 carries its explicit unavailable disposition and locks it.
- Coalesce adjacent items only when they have the same `resource_scoped_id`, source content, `asset_start`, contiguous source and timeline ranges, and identical audible policy. Preserve every authored XML blade in `blades` even after logical coalescing. Equal-content resources never coalesce across resource identity, even when their current audibility happens to match.
- Gaps break coalescing and produce no `LogicalSlice`; they remain represented only by the original document.
- Slice IDs hash ordered instance IDs and exact ranges; repeated source instances remain distinct.

## Implementation steps

1. Write failing tests for nonzero `asset_start` epochs, exact `resource_scoped_id`/`asset_content_id` propagation, shuffled media order, missing/duplicate/unknown resource joins, authored `has_audio` mismatch, repeated assets, continuous same-resource blade coalescing, cross-resource non-coalescing, source-only/timeline-only discontinuity, gaps producing no slice, muted/disabled/no-audio clips, a declared-audio slice that stays audible when later analysis is unavailable, same filename/different content, and same-content resources with equal or different `has_audio` values.
2. Implement pure derivation in `boundary.py` using Fraction only.
3. Run focused P4/P1 integration tests, invariants, full check; commit owned paths.

## Acceptance

Formula roundtrips at both endpoints for every supported fps; continuous same-resource authored blades yield one logical slice with two blade records and cause no extra STT by themselves, while a resource boundary always prevents coalescing. Identity joins are order-independent and fail closed before partial derivation; gaps yield no speech slice. Muted/repeated/unavailable-later cases remain addressable with authored audibility intact. `make check` passes.

## Failure route

Report item/instance/resource IDs, exact source/timeline Fractions, expected/actual coalescing, base/tests/diff. Do not add tolerance floats. Shared model ambiguity escalates Opus/Astra.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, slice/blade cases, and `ready-to-integrate`.
