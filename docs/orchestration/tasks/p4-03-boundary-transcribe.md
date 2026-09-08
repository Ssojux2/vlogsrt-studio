# Task P4-03: transcribe only retained boundary audio

## Authority

Binding spec `Boundary transcripts`; depends on P4-02, P2 PCM slicing, and P3 typed transcription outcome. This task must remain offline in development.

## Baseline and ownership

- Fresh `fcp-p4-boundary-transcribe` worktree from exact P4-02-integrated SHA.
- Modify `vlogsrt/project/boundary.py`; create `tests/project/test_boundary_transcribe.py`. Synthetic data is inline or generated under pytest `tmp_path`; no fixture/golden file is added or modified.
- Do not touch legacy/API fixtures, transcribe network implementation, or projection/export.

## Contract

```python
@dataclass(frozen=True, slots=True)
class BoundaryTranscript:
    boundary_id: str
    cache_key: str
    request_id: str
    source_id: str
    source_start: Fraction
    source_end: Fraction
    sample_range: SampleRange
    status: Literal["success", "empty", "error"]
    draft: tuple[SourceSegment, ...]
    settled_spine: tuple[SourceSegment, ...]
    repairs: tuple[Repair, ...]
    cues: tuple[SourceCue, ...]
    error: str | None
    cut_truncated: bool = True

def transcribe_boundaries(requests: Sequence[BoundaryRequest], references: Sequence[PcmReference], source_traces: Sequence[SourceTrace], cfg: Config, transcriber: Transcriber, artifact_dir: Path) -> tuple[BoundaryTranscript, ...]: ...
```

- Cache key hashes original/PCM identity, exact retained sample range, channel policy, source Trace analysis key, model/config. No path/stem-only key.
- Write input from the same stored PCM and exact request range; assert bytes outside the range are absent.
- Boundary jobs retain the original media stem and use an explicit namespace below that asset's content directory, for example `<artifact_dir>/<asset_content_id>/boundary/<cache_key>/`; initial, repair, and arbiter jobs propagate it without environment mutation.
- Use the same complete engine order as P3 source analysis: transcriber -> `build_spine` -> `repair.settle` -> immutable settled spine -> `pipeline.spine_to_cues` -> `refine.refine` (and therefore exactly one internal polish). Repetition repair remains active under existing defaults and every nested repair cut is clamped to the retained boundary PCM; add no removed context, forced alignment, or word timing.
- The engine operates in boundary-local decoded time with duration exactly `(sample_range.end-sample_range.start)/sample_rate`. After it settles/refines, convert each local boundary once to exact source-file time by adding `Fraction(sample_range.start, sample_rate) + reference.pcm_sample0_file_time`; retain both local engine records and projected exact cue provenance. Do not round to milliseconds until SRT serialization.
- Empty/error remains explicit. `cut_truncated=True` is terminal boundary-planning metadata: this trace may never schedule another boundary request because of its own endpoints, but it still receives ordinary repetition repair and refine processing.
- Failure preserves the original source text for later unresolved projection; no silent missing captions.

## Implementation steps

1. Write failing fake-transcriber tests for exact audio bytes/range/key, full engine call order, repair/refine retention, exactly one polish, deterministic response conversion, empty/error, malformed response, cache identity changes, and terminal-boundary non-recursion.
2. Implement retained-slice runner with explicit namespace and atomic completed artifacts; never invoke live client.
3. Run focused/all P4/invariant/full checks; commit owned paths.

## Acceptance

Captured transcriber and repair inputs contain exactly retained samples; cache keys change for every specified identity component; successful traces preserve draft/spine/repairs/cues, and failure returns a record without deleting source text. No paid call occurs. `make check` passes.

## Failure route

Report request/key prefix, exact sample bounds/byte count, status/error, base/tests/diff. Any evidence of removed audio is a hard stop and Astra escalation. Other repeated failures follow normal diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files, byte-range and status tests, network-call count zero, and `ready-to-integrate`.
