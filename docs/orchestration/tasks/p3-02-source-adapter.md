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
    variant: Literal["source_trace"]
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

@dataclass(frozen=True, slots=True)
class UnavailableSourceAudio:
    variant: Literal["unavailable_source_audio"]
    resource_scoped_id: str
    source_id: str
    original_sha256: str
    status: Literal["no_audio", "error"]
    reason_code: Literal["declared_no_audio", "stream_unavailable", "pcm_unavailable"]
    reason: str

SourceAnalysis = SourceTrace | UnavailableSourceAudio
AudioSourceInput = tuple[ResolvedMedia, PcmReference | UnavailableSourceAudio]

def plan_source_ranges(reference: PcmReference, cfg: Config) -> tuple[SampleRange, ...]: ...
def transcribe_source(media: ResolvedMedia, reference: PcmReference, cfg: Config, transcriber: Transcriber, artifact_dir: Path) -> SourceTrace: ...
def transcribe_unique_sources(items: Sequence[AudioSourceInput], cfg: Config, transcriber: Transcriber, artifact_dir: Path) -> tuple[SourceAnalysis, ...]: ...
```

Additive legacy seam in `vlogsrt/repair.py`, owned by this task:

```python
@dataclass(frozen=True, slots=True)
class Source:
    stem: str
    duration: float
    audio: Path | None = None
    workdir: Path | None = None
    namespace: Path | None = None
    cutter: Callable[[Sequence[Span]], list[Chunk]] | None = None
```

- Full PCM `[0,sample_count)` is covered exactly once by ordered ranges respecting existing `chunk_max_seconds`/`chunk_max_bytes`; target cuts use `quiet_sample`, then exact sample slicing.
- `transcribe_unique_sources` deduplicates by `analysis_key`, not path/stem/resource ID. Every unique source gets one full analysis; repeated instances consume the same trace.
- `PcmReference` and `transcribe_source` remain total and audio-only. P2 imports no P3 type and constructs no unavailable record. P7 later owns catching contextual pre-PCM failures and constructing `UnavailableSourceAudio`; direct P3 tests may construct the immutable input variant without simulating P2.
- `UnavailableSourceAudio` has no PCM path/hash/origin/key, transcript, or coverage. Its `__post_init__` raises `ValueError` unless `status == "no_audio"` iff `reason_code == "declared_no_audio"`; `stream_unavailable` and `pcm_unavailable` require `status == "error"`. `SourceTrace.__post_init__` likewise raises on a wrong discriminator or inconsistent typed outcome; never use `assert` for persisted-data validation.
- Validate every input pair by exact `resource_scoped_id`, `source_id`, and original hash before any transcription call. A valid unavailable record passes through once for its resource with zero STT calls; real references alone are deduplicated by `analysis_key`. Malformed identity, discriminator, or schema input rejects before calls.
- `SourceTrace.source_id` is the media `asset_content_id`; XML-local resource IDs never enter the durable source identity.
- `SourceTrace.original_stem`, `pcm_analysis_key`, and `pcm_sample0_file_time` are copied from the validated matching `PcmReference`, including for empty traces. Require `media.asset_content_id == reference.source_id` and matching original hashes; do not infer linkage from path, list position, or segment data.
- Per-asset namespace is the directory `artifact_dir/<asset_content_id>/`; the existing media stem remains `TranscriptionJob.stem`, and every initial/repair/arbiter job carries that same explicit namespace. `FixtureTranscriber` therefore addresses `<namespace>/<stem>.<kind>.json`. No process-global env mutation, stem-only cache, or weak `pipeline.source_fingerprint` may choose or validate reuse.
- Initial and nested repair WAVs live beneath the explicit ephemeral scratch directory `<artifact_dir>/<asset_content_id>/work/<analysis_key>/`. They are real P2 byte slices, never durable completed artifacts. Tests must inspect their exact frames/channels/bytes even though `FixtureTranscriber` does not open `Chunk.path`.
- Convert legacy float segment boundaries with `Fraction(str(value))`; add the measured `reference.pcm_sample0_file_time` for `file_*`. Segment IDs hash analysis key, chunk index, legacy segment ID, exact source range.
- Preserve typed success/empty/error. Missing/malformed/call errors are recorded error, never successful silence. No prompt/forced alignment/word/ratio split. `repair.Source.cutter=None` preserves the complete legacy cut/catch/fallback behavior. When injected, the project cutter must return exactly one `Chunk` per nonempty span or raise contextually; it validates each snapped chunk as `offset == float(Fraction(actual_start_sample, rate))` and `duration == float(Fraction(actual_end_sample - actual_start_sample, rate))`, never against unsnapped raw float span endpoints.
- Capture repair/arbiter operational failures with a private project-owned wrapper around the injected transcriber/cutter, not by adding fields to `Repair`, `Segment`, or `Cue`. The wrapper records before rethrowing call/payload/duplicate/missing/unmatched-coverage failures; after the unchanged legacy catch path returns, any captured failure forces an error trace with real reference provenance and no completed SRT. Valid covered empty/repetitive/arbiter-no-speech responses retain existing kept/collapsed policy. Namespace-present fixture subset selection follows P3-01; arbitrary live/unmatched responses are never silently filtered.
- Make that private capture validation job-kind aware. `FIXTURE` and `RESCUE` responses require the strict P3-01 segment payload, including an explicit string `text` for every segment. `ARBITER` and `VERIFY` responses require exact requested-response coverage and a dict payload containing either an explicit string top-level `text` (the empty string is valid no-speech evidence) or the existing `parse_text` fallback key `segments`, whose value is a list of objects with string `text` members; an explicit empty `segments` list is valid no-text evidence. A top-level JSON list is not the fallback. A payload with neither form (including `{}`), or malformed/non-string text, is an operational capture error. These text-only jobs require no timestamps. Do not synthesize text-only results into public `TranscriptionOutcome`/`Segment` values, broaden `transcribe_outcome`, or change legacy `parse_text` and repair semantics.
- A failure after a real PCM reference exists is an error `SourceTrace` retaining the real reference identity, hashes, origin, and analysis key. It is never downgraded to `UnavailableSourceAudio`, and unrelated source analyses still proceed.
- Successful empty analysis publishes the deterministic empty source-local SRT artifact and its hash; error publishes no SRT path/hash.
- A successful nonempty analysis must reuse the complete existing source engine in this exact order: transcriber responses -> `transcribe.build_spine` draft -> `repair.settle` -> immutable settled spine -> `pipeline.spine_to_cues` -> `refine.refine` -> source-local SRT. `refine.refine` already invokes polish; never call `polish_pass` separately or twice. The durable source-local artifact is exactly `<artifact_dir>/<asset_content_id>/<analysis_key>/<stem>.SOURCE.srt`, uses the existing legacy source serializer, and records that relative path plus SHA-256 in the trace. This keyed durable directory is separate from both the unchanged fixture namespace `<artifact_dir>/<asset_content_id>/` used by every `TranscriptionJob` and the unchanged ephemeral scratch path `<artifact_dir>/<asset_content_id>/work/<analysis_key>/`.
- Publish a source-local SRT for one `analysis_key` atomically and exclusively. If that exact completed path already contains byte-identical output, reuse it idempotently. If different bytes already exist, return an explicit error `SourceTrace` with real PCM/reference provenance and no successful replacement while preserving the existing artifact byte-for-byte. Do not implement a non-atomic existence-check-then-overwrite sequence, delete another generation, or add garbage collection.
- `repair.Source.duration` and `refine.refine(..., duration=...)` both receive exactly `float(Fraction(reference.sample_count, reference.sample_rate))`. The project output is not required to be byte-identical to the legacy MP3 path; P0's lock protects unchanged legacy inputs/behavior.
- Repair retranscription still runs under existing defaults. Propagate the optional namespace to every repair/arbiter `TranscriptionJob`. The project adapter injects a cutter backed by the same retained P2 PCM reference, so repair spans do not fall back to legacy MP3 `split_at`; the default `repair.settle` behavior for legacy callers remains byte-identical.
- Legacy `Segment`, `Cue`, and `Repair` times in a trace are decoded-audio-local floats and remain immutable provenance. Build each `SourceSegment.audio_*` as `Fraction(str(legacy_value))`, then add `reference.pcm_sample0_file_time` exactly once for its `file_*` fields. Do not rewrite `parse_segments`/`build_spine` float compatibility. P3-03 projects presentation from the already-refined trace cues, not settled-spine boundaries.
- Do not reject, delete, or normalize finite zero/reversed legacy segment durations in the adapter. P3-01 has already rejected missing/non-numeric/nonfinite values; existing build/repair/refine presentation semantics decide how the retained finite records appear, and every draft/spine/cue/repair record remains traceable.
- Source Trace, draft, settled spine, repairs, cues, and source-local SRT bytes are immutable once completed. Later project operations consume them but never edit them; a changed/missing source-local hash invalidates readiness rather than triggering silent regeneration from a derived timeline.

## Implementation steps

1. Write failing tests for repeated/same-name sources, full range coverage, quiet cut bounds and snapped chunk metadata, nonzero origin, exact namespace/scratch paths and real initial+nested WAV bytes, successful empty, malformed/error and repair/arbiter operational capture, requested subset/duplicate/missing response coverage, valid empty and positive arbiter text, missing/non-string arbiter text, the dict payload's `segments` string-text fallback, deterministic IDs, zero/reversed raw segments, repair kept/collapsed retention, immutable trace records, exact refine duration, both union variants, runtime invariant rejection, zero-call unavailable pass-through, pre-call identity rejection, post-PCM error provenance, and same-content resources with different `has_audio` dispositions. Prove two analysis keys for the same source may retain different SRT bytes with both recorded hashes valid, same-key identical output reuses the completed artifact, and same-key conflicting output returns an error while preserving the old bytes.
2. Add a legacy-default-preserving repair-cutter injection seam, then implement orchestration around the injected `Transcriber` and P2 PCM cutter. Assert with spies that `build_spine`, `repair.settle`, `pipeline.spine_to_cues`, and `refine.refine` run in order and polish runs exactly once. No network is created here.
3. Run focused P3 tests, invariants, full check; commit owned files.

## Acceptance

Every unique real `analysis_key` triggers one full source analysis and covers every sample once; repeated instances and unavailable resources add zero calls. Repair calls are additional engine-owned calls only when existing repetition rules require them and use retained PCM. Empty, post-PCM error, declared-no-audio, and pre-PCM error remain distinguishable. Distinct analysis generations coexist, and a same-key publication conflict cannot overwrite completed bytes. Existing chunk, repair, refine, and polish defaults are used unchanged; P0-03 exact bytes remain locked. `make check` passes offline.

## Failure route

Report analysis key prefix, sample ranges, coverage/status, expected call count, base/tests/diff. Never mutate environment or add filename fallback. Repeated failures use Terra/Luna diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files, call/coverage matrix, focused/full results, and `ready-to-integrate`.
