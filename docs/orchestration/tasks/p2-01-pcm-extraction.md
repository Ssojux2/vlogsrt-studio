# Task P2-01: extract measured original 16 kHz PCM

## Authority

Binding spec `Source analysis, clocks, identity` and P2; depends on Astra's P0 merge only and may proceed independently of P1. This task extends the existing sole ffmpeg/ffprobe boundary without moving subprocess elsewhere. Astra's measured offline evidence in `docs/orchestration/reviews/astra-pcm-origin-evidence.md` is required pre-read evidence, not an implementation or a real-media acceptance pass.

## Baseline and ownership

- Fresh `fcp-p2-pcm-extract` worktree from exact approved P0 merge SHA `cd411c4376ccf548151dbd42a9e11c78bbfb4067`; abort on mismatch.
- Modify `vlogsrt/preprocess.py`, `tests/test_preprocess.py` only.
- Do not alter existing MP3 subtitle defaults/limits, `split_at`, fixtures, or project package.

## Contract

```python
@dataclass(frozen=True, slots=True)
class AudioStreamInfo:
    stream_index: int
    start_time: Fraction
    sample_rate: int
    channels: int

@dataclass(frozen=True, slots=True)
class PcmExtraction:
    path: Path
    stream: AudioStreamInfo
    sample_count: int
    sample_width: int
    pcm_sample0_file_time: Fraction

def probe_audio_stream(source: Path, stream_index: int) -> AudioStreamInfo: ...
def extract_pcm_s16le(source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000) -> PcmExtraction: ...
```

- `probe_audio_stream` uses the existing `_run` ffprobe boundary with JSON and parses times via `Fraction(str(value))`; missing/malformed stream fields fail distinctly.
- Extraction maps the exact audio stream, preserves its channel count, produces little-endian signed 16-bit raw PCM at 16 kHz, and computes `sample_count` as frames per channel from exact byte length: `bytes == sample_count * sample_width * channels`; partial channel frames fail.
- ffprobe `stream.start_time` is recorded but is not by itself proof of the decoded PCM origin. `pcm_sample0_file_time` defines the mapping of decoded PCM frame zero into the asset/video presentation epoch after decoder priming/skip-sample and resampling behavior. No 0.2-second constant is allowed.
- Preserve the measured post-conversion clock using the equivalent of `-copyts`, `aresample=16000`, `aformat=sample_fmts=s16`, `asettb=1/16000`, and `ashowinfo`, without `-start_at_zero` or a timestamp-normalizing `asetpts`. Parse the first post-conversion integer PTS as the candidate sample-zero epoch, and reject missing or discontinuous frame timing instead of silently treating the whole output as one clock.
- Existing `extract_audio`, chunk limits, temperature, and legacy outputs remain byte/behavior compatible under existing tests.

## Implementation steps

1. Write failing mocked-command tests for stream selection, nonzero/negative origins, stereo preservation, malformed probe JSON, missing stream, missing/malformed/discontinuous converted-frame PTS, partial PCM frame, and exact argv. Also generate small synthetic media with nonzero and negative audio/video PTS plus known per-channel impulses; decode it and prove which PCM frame corresponds to the asset/video presentation epoch. Mock argv evidence alone is insufficient for the origin contract.
2. Reproduce a representative subset of the committed origin experiment in tests: include the decisive negative AAC/MKV case where raw stream start is `-0.264` but decoded sample zero is `-1/5`, and a positive case demonstrating retained priming (the recorded AAC/MOV result has sample zero `242/125` and peak frame 5024 mapping to `9/4`). Add 44.1/48 kHz resampling and continuity rejection coverage; do not bake codec delay into a correction.
3. Implement frozen results and small helpers inside `preprocess.py`; no imports from `vlogsrt.project`.
4. Run focused preprocess tests, invariants, full check; commit two owned files.

## Acceptance

Focused tests verify exact origin/channel/sample arithmetic, stereo frame counting, decoder priming/skip-sample handling, 44.1/48 kHz resampling, continuity rejection, and no magic offset. The synthetic impulse cases prove PCM sample-zero mapping for nonzero and negative PTS. The committed five-case table is evidence only: uncommon edit lists, variable layouts, and real camera/S17 media remain unproven or `PENDING_HUMAN`. All existing preprocess and source tests remain unchanged and pass; P0-03 exact source bytes remain locked. `make check` passes offline.

## Failure route

Report probe JSON fragment without personal paths, exact argv/exit/tail, expected/actual Fraction, base SHA and diff. Repeated origin failures route to Terra/Luna. No constant compensation is permitted.

## Report

After committing, report Orca/launch IDs, branch/base/immutable commit, files, tests, and `ready-to-integrate` as a review-ready checkpoint; real source-media extraction remains pending unless a non-protected synthetic file is generated during test. Ask Sol and wait; a Sol pass routes the SHA to Astra but the same Dispatch remains active for Astra's final acceptance or bounded findings before `worker_done`.
