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
    pcm_analysis_key: str
    original_stem: str
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
- Resolve each request, `PcmReference`, and `SourceTrace` by validated `source_id` plus `pcm_analysis_key`; require one unambiguous match and test shuffled input orders. Copy the reference's `original_stem` into the transcript and use it as the job stem; paths and list positions never establish linkage.
- Write input from the same stored PCM and exact request range; assert bytes outside the range are absent.
- Boundary jobs retain `PcmReference.original_stem` and use the explicit namespace `<artifact_dir>/<asset_content_id>/boundary/<cache_key>/`; initial, repair, and arbiter jobs propagate it without environment mutation. Actual initial/nested repair WAVs live beneath its ephemeral `work/` child, are never completed artifacts, and must be byte-inspected in synthetic tests even when fixtures ignore their paths.
- Use the same complete engine order as P3 source analysis: transcriber -> `build_spine` -> `repair.settle` -> immutable settled spine -> `pipeline.spine_to_cues` -> `refine.refine` (and therefore exactly one internal polish). Repetition repair remains active under existing defaults and every nested repair cut is clamped to the retained boundary PCM; add no removed context, forced alignment, or word timing.
- The engine and `refine.refine` operate in boundary-local decoded time with duration exactly `float(Fraction(sample_range.end - sample_range.start, sample_rate))`. A nested repair chunk's local `Chunk.offset` is `float(Fraction(actual_start_sample - sample_range.start, sample_rate))` and its duration uses the actual snapped sample count, never the unsnapped request span. After settle/refine, convert each local cue once into the P3 `SourceCue` decoded-audio-local clock by adding `Fraction(sample_range.start, sample_rate)` only. Add `reference.pcm_sample0_file_time` later during file/XML placement, exactly once; retain local engine and snapped-range provenance and do not round until SRT serialization.
- Mirror P3-02's private project-owned failure capture: injected cutter failures/count mismatches and repair/arbiter call/payload/duplicate/missing/unmatched requested coverage produce a typed boundary error with no completed artifact, while valid covered empty/repetitive/no-speech remains engine policy. Namespace-present fixture retry subsets use P3-01's exact chunk-identity selection; arbitrary live/unmatched responses are not silently filtered.
- Keep the capture validator job-kind aware exactly as in P3-02. `FIXTURE` and `RESCUE` require strict segment payloads with explicit string segment text. `ARBITER` and `VERIFY` require exact requested-response coverage plus either an explicit string top-level `text` (including valid `""`) or the unchanged `parse_text` fallback list of objects with string `text` fields; neither form requires or invents timestamps. Missing or malformed text produces a typed boundary error. Do not broaden public `transcribe_outcome` into text-to-`Segment` synthesis or change legacy `parse_text`/repair behavior.
- Empty/error remains explicit. `cut_truncated=True` is terminal boundary-planning metadata: this trace may never schedule another boundary request because of its own endpoints, but it still receives ordinary repetition repair and refine processing.
- Failure preserves the original source text for later unresolved projection; no silent missing captions.

## Implementation steps

1. Write failing fake-transcriber tests for exact initial+nested audio bytes/range/key and scratch paths, snapped chunk offset/duration, shuffled request/reference/trace order, missing/duplicate/mismatched linkage, original-stem fixture namespace/retry subset, full engine call order, repair/refine retention and exact duration, exactly one polish, local-to-decoded-to-file clock conversion at positive/negative origins, empty/error and operational capture, malformed/duplicate/missing response coverage, valid empty and positive arbiter text, missing/non-string arbiter text, the list-of-string-text fallback, cache identity changes, and terminal-boundary non-recursion.
2. Implement retained-slice runner with explicit namespace and atomic completed artifacts; never invoke live client.
3. Run focused/all P4/invariant/full checks; commit owned paths.

## Acceptance

Captured transcriber and repair inputs contain exactly retained samples; cache keys change for every specified identity component; successful traces preserve draft/spine/repairs/cues, and failure returns a record without deleting source text. No paid call occurs. `make check` passes.

## Failure route

Report request/key prefix, exact sample bounds/byte count, status/error, base/tests/diff. Any evidence of removed audio is a hard stop and Astra escalation. Other repeated failures follow normal diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files, byte-range and status tests, network-call count zero, and `ready-to-integrate`.
