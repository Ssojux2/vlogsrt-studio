# Task P2-02: persist and slice exact PCM references

## Authority

Binding spec `Source analysis, clocks, identity` and `Boundary transcripts`; depends on P2-01. The interfaces here are consumed by P3, P4, and P5.

## Baseline and ownership

- Fresh `fcp-p2-pcm-reference` worktree from exact P2-01 integration SHA `5ea9719cb2fb3ece20ad228fa78481ae60bbdb3b`; abort on mismatch.
- Create `vlogsrt/project/pcm.py`, `tests/project/test_pcm.py`.
- Do not modify preprocess/media/time modules or external fixtures.

## Contract

```python
@dataclass(frozen=True, slots=True)
class AudioPolicy:
    stream_index: int = 0
    sample_rate: int = 16000
    sample_width: int = 2

@dataclass(frozen=True, slots=True)
class PcmReference:
    source_id: str
    original_stem: str
    original_sha256: str
    path: Path
    pcm_sha256: str
    analysis_key: str
    pcm_sample0_file_time: Fraction
    sample_rate: int
    channels: int
    sample_count: int

@dataclass(frozen=True, slots=True)
class SampleRange:
    start: int
    end: int

def build_reference(source_id: str, original_stem: str, original: Path, original_sha256: str, policy: AudioPolicy, destination: Path) -> PcmReference: ...
def retained_sample_range(reference: PcmReference, start: Fraction, end: Fraction) -> SampleRange: ...
def write_wav_slice(reference: PcmReference, samples: SampleRange, destination: Path) -> Path: ...
def quiet_sample(reference: PcmReference, target: int, search_samples: int, window_samples: int) -> int: ...
```

- `AudioPolicy.stream_index` is the zero-based **audio-stream ordinal** consumed by P2-01 (`0` means first audio), validated as a nonnegative integer with `bool` rejected. It is not a global container-stream index. The resulting `PcmReference` need not duplicate that ordinal because its extraction provenance/analysis key binds the policy, while P2-01's `AudioStreamInfo.stream_index` records the selected global ffprobe index.
- `AudioPolicy.__post_init__` raises `ValueError` at construction unless `stream_index` is a non-`bool` nonnegative `int`, `sample_rate` is the non-`bool` integer `16000`, and `sample_width` is the non-`bool` integer `2`. Downstream functions may defend again but cannot be the only validation point.
- `PcmReference` is a total record for an audio-capable input whose P2-01 extraction succeeded. Callers must not invoke `build_reference` for a resource declared without audio and must not fabricate a path, hash, sample count, or origin when probing/extraction failed; the future P3 source-analysis union owns those explicit resource-scoped unavailable dispositions.
- `analysis_key` hashes original full-content hash, exact audio policy (including audio ordinal), measured `pcm_sample0_file_time`, PCM hash, channel count/rate; paths/names alone are never keys.
- `source_id` is exactly P1's `ResolvedMedia.asset_content_id`; `original_stem` is retained only to address explicit per-source fixture/artifact namespaces. Validate both as nonempty and retain `original_sha256` so later joins never infer identity from `path`, filename, or parallel sequence position.
- `sample_count`, `SampleRange.start/end`, `target`, `search_samples`, and `window_samples` are frames per channel. Range conversion uses `ceil(start * sample_rate)` and `floor(end * sample_rate)`, clamped to real per-channel frame count; reject `end <= start` after conversion.
- Slice contains only `[start,end)` retained frames from the same stored PCM, interleaved channel-complete, with byte count `(end-start) * sample_width * channels` plus an ordinary WAV header. No removed-audio padding/context.
- `quiet_sample` searches only the bounded target window in per-channel frame units, measures channels separately and ranks a frame window by their maximum absolute amplitude; ties choose the candidate nearest target, then the earlier candidate. It chooses a cut location only and never labels speech/silence.
- `file_time = Fraction(sample_index, sample_rate) + pcm_sample0_file_time`; keep this as exact Fraction arithmetic and consume P2-01's proven decoded-clock origin rather than equating it to raw ffprobe `stream.start_time`.
- Before reuse or extraction, hash the actual original file in full and require equality with supplied `original_sha256`; a mismatch fails contextually before calling P2-01. Hash verification requires that a completed generation is reused only when original/PCM metadata, both hashes, and policy match.
- `destination` is a **logical cache address**, not the mutable pathname promised by a returned reference. A completed build writes a new immutable generation PCM and its immutable metadata under private sibling generation paths, verifies that pair completely, and only then publishes one manifest/pointer for `destination` with a same-directory atomic replace. `PcmReference.path` is the immutable generation PCM path selected by that pointer and is not required to equal `destination`.
- Never rename, remove, or overwrite an existing generation during replacement. Failed extraction, generation-metadata publication, verification, or pointer publication leaves the previous pointer and every prior generation untouched; an immediate process exit before pointer commit has the same property and may leave only unreferenced private generation material. Successful changed-source publication advances the pointer but retains old generations, so every already-issued `PcmReference` remains hash-valid and sliceable after both failed **and successful** updates at the same logical `destination`.
- A backup/rollback around a mutable destination is not an accepted substitute: `finally` cleanup cannot make that design crash-safe. The pointer may name only a private generation produced for the same logical address, and loading still validates the pointed immutable metadata, PCM size/hash, original identity, and exact policy before reuse.
- The public surface is exactly the dataclasses/functions in the contract block. The file-time formula is consumed directly or through private implementation helpers; do not add an unreviewed public convenience API.

## Implementation steps

1. Write failing byte-pattern tests for policy construction, original-file hash agreement/mismatch-before-call, mono/stereo, ceil/floor boundaries, negative/nonzero origin mapping, tail clamp, zero range, hash-key changes, quiet-cut tie behavior, phase-opposed channels, and deterministically injected atomic failure.
2. Add red-first publication tests that begin with a valid old `PcmReference`, request a different original/hash at the same logical `destination`, and prove: metadata or pointer failure preserves the old pair and old-reference slice with zero re-extraction; a child process exiting before pointer commit cannot invalidate the old reference; and a successful changed-source update returns a different immutable generation while the held old reference remains usable. Assert the logical pointer never exposes a partial pair and no test depends on `finally` rollback.
3. Implement using P2-01 extraction plus stdlib hash/wave/file operations; no production subprocess here.
4. Run focused/P2/full checks; commit owned files.

## Acceptance

Slices decode to exactly the expected channel frames; no sample outside the retained range appears. Same content/policy yields same key, any audio selection/config/hash change yields a different key. Prior returned references survive failure, abrupt pre-commit process exit, and successful replacement at the same logical address because their immutable generation paths remain present and unchanged. `make check` passes.

## Failure route

Report exact Fraction/sample indices, channels, byte counts, hash inputs (not media content), base/tests. Never round both endpoints the same way or add tolerance samples. Escalate after repeated failure per policy.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, measured sample evidence, and `ready-to-integrate`.
