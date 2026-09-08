# Task P2-02: persist and slice exact PCM references

## Authority

Binding spec `Source analysis, clocks, identity` and `Boundary transcripts`; depends on P2-01. The interfaces here are consumed by P3, P4, and P5.

## Baseline and ownership

- Fresh `fcp-p2-pcm-reference` worktree from exact P2-01-integrated SHA.
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
- `analysis_key` hashes original full-content hash, exact audio policy (including audio ordinal), measured `pcm_sample0_file_time`, PCM hash, channel count/rate; paths/names alone are never keys.
- `source_id` is exactly P1's `ResolvedMedia.asset_content_id`; `original_stem` is retained only to address explicit per-source fixture/artifact namespaces. Validate both as nonempty and retain `original_sha256` so later joins never infer identity from `path`, filename, or parallel sequence position.
- `sample_count`, `SampleRange.start/end`, `target`, `search_samples`, and `window_samples` are frames per channel. Range conversion uses `ceil(start * sample_rate)` and `floor(end * sample_rate)`, clamped to real per-channel frame count; reject `end <= start` after conversion.
- Slice contains only `[start,end)` retained frames from the same stored PCM, interleaved channel-complete, with byte count `(end-start) * sample_width * channels` plus an ordinary WAV header. No removed-audio padding/context.
- `quiet_sample` searches only the bounded target window in per-channel frame units, measures channels separately and ranks a frame window by their maximum absolute amplitude; ties choose the candidate nearest target, then the earlier candidate. It chooses a cut location only and never labels speech/silence.
- `file_time = Fraction(sample_index, sample_rate) + pcm_sample0_file_time`; keep this as exact Fraction arithmetic and consume P2-01's proven decoded-clock origin rather than equating it to raw ffprobe `stream.start_time`.
- Atomic creation and hash verification: a completed destination is reused only when metadata/hash/policy match.

## Implementation steps

1. Write failing byte-pattern tests for mono/stereo, ceil/floor boundaries, negative/nonzero origin mapping, tail clamp, zero range, hash-key changes, quiet-cut tie behavior, phase-opposed channels, and atomic failure.
2. Implement using P2-01 extraction plus stdlib hash/wave/file operations; no subprocess here.
3. Run focused/P2/full checks; commit owned files.

## Acceptance

Slices decode to exactly the expected channel frames; no sample outside the retained range appears. Same content/policy yields same key, any audio selection/config/hash change yields a different key. `make check` passes.

## Failure route

Report exact Fraction/sample indices, channels, byte counts, hash inputs (not media content), base/tests. Never round both endpoints the same way or add tolerance samples. Escalate after repeated failure per policy.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, measured sample evidence, and `ready-to-integrate`.
