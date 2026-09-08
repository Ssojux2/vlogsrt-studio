# Task P3-03: project immutable source traces into the input-timeline SOURCE SRT draft

## Authority

Binding spec `Input and output`, `Source contracts and architecture`, and P3; depends on P3-02. Ends P3 for Opus review and Astra integration.

## Baseline and ownership

- Fresh `fcp-p3-source-srt` worktree from exact P3-02-integrated SHA.
- Create `vlogsrt/project/captions.py`, `tests/project/test_source_captions.py`.
- Do not modify source traces, legacy output/refine, project XML/media/PCM, or fixtures.

## Contract

```python
@dataclass(frozen=True, slots=True)
class SourceCue:
    cue_id: str
    source_id: str
    segment_ids: tuple[str, ...]
    start: Fraction
    end: Fraction
    text: str
    flags: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class PlacedSourceCue:
    cue_id: str
    instance_id: str
    source_id: str
    source_start: Fraction
    source_end: Fraction
    timeline_start: Fraction
    timeline_end: Fraction
    text: str
    flags: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SourceCaptionSet:
    input_sha256: str
    source_cues: tuple[SourceCue, ...]
    placed_cues: tuple[PlacedSourceCue, ...]
    srt_text: str
    boundary_instance_ids: tuple[str, ...]

def source_cues(trace: SourceTrace) -> tuple[SourceCue, ...]: ...
def serialize_srt(cues: Sequence[SourceCue | PlacedSourceCue]) -> str: ...
def project_source_captions(document: ProjectDocument, media: Sequence[ResolvedMedia], traces: Sequence[SourceTrace]) -> SourceCaptionSet: ...
```

- Per-source `SourceCue` is the immutable, already repaired/refined cue output from P3-02, in zero-based decoded-audio time. Do not rebuild cues from raw segments or run refine/polish again. The package-level `VLOG_AI_SOURCE.srt` is the supported input project timeline before automatic roughcut edits, elapsed from zero relative to `sequence.tcStart`, and assembled across every audible clip/source instance using exact file/XML clock mapping.
- Text and boundaries ultimately come only from Whisper segments through the reused source engine. Preserve its repairs, settled spine, cue flags, coverage, and source trace; do not synthesize, forced-align, ratio-split, or delete records here.
- Half-up millisecond formatting occurs exactly once via P1 timebase. `end > start` is mandatory; adjacent `end == next.start` is valid and serializes to identical boundary strings.
- A cue fully contained by a clip maps normally. A cue cut by an authored input edge is presentation-clamped, retains its entire original source record, gets `boundary_required`, and records the clip instance ID for P4; it is never silently omitted.
- Empty-success contributes no cues with explicit trace status; any error trace refuses ready publication. Each asset's local SOURCE SRT remains an addressable workspace artifact below its per-asset namespace; it is never overwritten or concatenated into a single per-source file. Atomic package writing belongs to P6.

## Implementation steps

1. Write failing tests for multiple sources/instances, repeated and muted/disabled instances, zero/nonzero audio origin and XML epochs, reuse of already-refined cues/flags without another polish, exact text retention, input-edge clamp/flag, adjacent boundaries, half-ms ties, empty success, and error refusal.
2. Implement immutable placement and exact Fraction serializer without importing or invoking legacy refine/polish/output/verify.
3. Run focused/all P3/invariant/full checks; commit owned files.

## Acceptance

All immutable per-source cues remain addressable in each source trace; placed cues reproduce them once per audible input instance or carry an explicit boundary flag. Muted/disabled instances and gaps emit no speech. The single SOURCE draft spans the pre-roughcut input project elapsed timeline with no cumulative drift and at most 1 ms serialization error. `make check` passes and no source trace/spine/cue/artifact byte is mutated.

## Failure route

Report cue/segment IDs, exact Fraction/ms values, expected/actual text, base/tests/diff. Never fix with early ms rounding or source mutation. Escalate shared semantics through Opus/Astra.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, empty/error behavior, source immutability evidence, and `ready-to-integrate`.
