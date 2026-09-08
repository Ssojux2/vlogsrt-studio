# Task P3-02: transcribe each unique source through an explicit namespace

## Authority

Binding spec `Source analysis, clocks, identity`; depends on P3-01 plus P1 media and P2 PCM interfaces. Shared `SourceTrace` fields require Opus review before dispatch.

## Baseline and ownership

- Fresh `fcp-p3-source-adapter` worktree from exact P3-01-integrated SHA.
- Create `vlogsrt/project/speech.py`, `tests/project/test_speech.py`.
- Narrowly modify `vlogsrt/repair.py` and `tests/test_repair.py` only to inject a retained-PCM repair cutter while preserving the existing default cutter and legacy behavior. Do not modify PCM/media contracts, env state, fixtures/API, or CLI.

## Contract

```python
@dataclass(frozen=True, slots=True)
class SourceSegment:
    segment_id: str
    audio_start: Fraction
    audio_end: Fraction
    file_start: Fraction
    file_end: Fraction
    text: str
    compression_ratio: float
    avg_logprob: float
    no_speech_prob: float

@dataclass(frozen=True, slots=True)
class SourceTrace:
    source_id: str
    original_stem: str
    analysis_key: str
    pcm_analysis_key: str
    pcm_sample0_file_time: Fraction
    status: Literal["success", "empty", "error"]
    draft: tuple[SourceSegment, ...]
    settled_spine: tuple[SourceSegment, ...]
    repairs: tuple[Repair, ...]
    cues: tuple[Cue, ...]
    coverage: tuple[ChunkCoverage, ...]
    model: str
    config_sha256: str
    source_srt_relative_path: str | None
    source_srt_sha256: str | None
    error: str | None

def plan_source_ranges(reference: PcmReference, cfg: Config) -> tuple[SampleRange, ...]: ...
def transcribe_source(media: ResolvedMedia, reference: PcmReference, cfg: Config, transcriber: Transcriber, artifact_dir: Path) -> SourceTrace: ...
def transcribe_unique_sources(items: Sequence[tuple[ResolvedMedia, PcmReference]], cfg: Config, transcriber: Transcriber, artifact_dir: Path) -> tuple[SourceTrace, ...]: ...
```

- Full PCM `[0,sample_count)` is covered exactly once by ordered ranges respecting existing `chunk_max_seconds`/`chunk_max_bytes`; target cuts use `quiet_sample`, then exact sample slicing.
- `transcribe_unique_sources` deduplicates by `analysis_key`, not path/stem/resource ID. Every unique source gets one full analysis; repeated instances consume the same trace.
- `SourceTrace.source_id` is the media `asset_content_id`; XML-local resource IDs never enter the durable source identity.
- `SourceTrace.original_stem`, `pcm_analysis_key`, and `pcm_sample0_file_time` are copied from the validated matching `PcmReference`, including for empty traces. Require `media.asset_content_id == reference.source_id` and matching original hashes; do not infer linkage from path, list position, or segment data.
- Per-asset namespace is the directory `artifact_dir/<asset_content_id>/`; the existing media stem remains `TranscriptionJob.stem`, and every initial/repair/arbiter job carries that same explicit namespace. `FixtureTranscriber` therefore addresses `<namespace>/<stem>.<kind>.json`. No process-global env mutation, stem-only cache, or weak `pipeline.source_fingerprint` may choose or validate reuse.
- Convert legacy float segment boundaries with `Fraction(str(value))`; add the measured `reference.pcm_sample0_file_time` for `file_*`. Segment IDs hash analysis key, chunk index, legacy segment ID, exact source range.
- Preserve typed success/empty/error. Missing/malformed/call errors are recorded error, never successful silence. No prompt/forced alignment/word/ratio split.
- Successful empty analysis publishes the deterministic empty source-local SRT artifact and its hash; error publishes no SRT path/hash.
- A successful nonempty analysis must reuse the complete existing source engine in this exact order: transcriber responses -> `transcribe.build_spine` draft -> `repair.settle` -> immutable settled spine -> `pipeline.spine_to_cues` -> `refine.refine` -> source-local SRT. `refine.refine` already invokes polish; never call `polish_pass` separately or twice. The source-local artifact is exactly `<namespace>/<stem>.SOURCE.srt`, uses the existing legacy source serializer, and records a relative path plus SHA-256 in the trace.
- Repair retranscription still runs under existing defaults. Extend `repair.Source` with the optional explicit namespace and propagate it to every repair/arbiter `TranscriptionJob`. The project adapter injects a cutter backed by the same retained P2 PCM reference, so repair spans do not fall back to legacy MP3 `split_at`; the default `repair.settle` behavior for legacy callers remains byte-identical.
- Do not reject, delete, or normalize finite zero/reversed legacy segment durations in the adapter. P3-01 has already rejected missing/non-numeric/nonfinite values; existing build/repair/refine presentation semantics decide how the retained finite records appear, and every draft/spine/cue/repair record remains traceable.
- Source Trace, draft, settled spine, repairs, cues, and source-local SRT bytes are immutable once completed. Later project operations consume them but never edit them; a changed/missing source-local hash invalidates readiness rather than triggering silent regeneration from a derived timeline.

## Implementation steps

1. Write failing tests for repeated/same-name sources, full range coverage, quiet cut bounds, nonzero origin, exact per-asset-directory/stem namespace, successful empty, malformed/error, deterministic IDs, zero/reversed raw segments, repair retention, and immutable trace records.
2. Add a legacy-default-preserving repair-cutter injection seam, then implement orchestration around the injected `Transcriber` and P2 PCM cutter. Assert with spies that `build_spine`, `repair.settle`, `pipeline.spine_to_cues`, and `refine.refine` run in order and polish runs exactly once. No network is created here.
3. Run focused P3 tests, invariants, full check; commit owned files.

## Acceptance

Every unique `analysis_key` triggers one full source analysis and covers every sample once; repeated instances add zero calls. Repair calls are additional engine-owned calls only when existing repetition rules require them and use retained PCM. Empty and error traces remain distinguishable. Existing chunk, repair, refine, and polish defaults are used unchanged; P0-03 exact bytes remain locked. `make check` passes offline.

## Failure route

Report analysis key prefix, sample ranges, coverage/status, expected call count, base/tests/diff. Never mutate environment or add filename fallback. Repeated failures use Terra/Luna diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files, call/coverage matrix, focused/full results, and `ready-to-integrate`.
