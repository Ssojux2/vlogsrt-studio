# Task P5-04: produce deterministic proxy-first vision samples

## Authority

Binding spec `Visual scoring and deterministic roughcut`; depends on P5-00 plus P1 media/time. May run parallel with P5-01 because files are disjoint. This task accepts explicit sample times and does not choose editable blocks.

## Baseline and ownership

- Fresh `fcp-p5-vision-sampling` worktree from exact P5-00-integrated SHA.
- Create `vlogsrt/project/vision/__init__.py`, `vlogsrt/project/vision/sampling.py`, `tests/project/test_vision_sampling.py`; modify `vlogsrt/preprocess.py`, `tests/test_preprocess.py` only for the existing ffmpeg boundary frame extraction.
- No vision API/policy, VAD/guard/edit modules, or source fixture changes.

## Contract

```python
@dataclass(frozen=True, slots=True)
class VideoStreamInfo:
    stream_index: int
    presentation_origin: Fraction
    frame_duration: Fraction
    frame_count: int

@dataclass(frozen=True, slots=True)
class FrameCorrespondence:
    original: VideoStreamInfo
    proxy: VideoStreamInfo | None
    proxy_usable: bool
    review: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class FrameSampleRequest:
    frame_id: str
    source_id: str
    source_time: Fraction

@dataclass(frozen=True, slots=True)
class FrameSample:
    frame_id: str
    source_id: str
    source_time: Fraction
    image_path: Path
    image_sha256: str
    used_proxy: bool
    original_frame_index: int
    selected_frame_index: int
    selected_presentation_time: Fraction
    review: tuple[str, ...]

def probe_video_stream(source: Path, stream_index: int = 0) -> VideoStreamInfo: ...  # preprocess.py
def extract_frame_index(source: Path, stream_index: int, frame_index: int, destination: Path, *, long_edge: int) -> Path: ...  # preprocess.py
def establish_correspondence(media: ResolvedMedia) -> FrameCorrespondence: ...
def sample_frames(media: ResolvedMedia, requests: Sequence[FrameSampleRequest], directory: Path, cfg: ProjectConfig) -> tuple[FrameSample, ...]: ...
```

- Original/proxy pairing comes only from the FCP media-rep association or explicit media map carried by `ResolvedMedia`; filename similarity never establishes a pair. Original audio/STT/VAD identity remains unchanged.
- A request identifies an exact position on the original source frame grid. Derive `original_frame_index = (source_time - original.presentation_origin) / frame_duration` and require an integer in `[0, frame_count)`. Never pass the same numeric seek seconds to original and proxy by assumption.
- The supported proxy case requires the same exact CFR frame duration and frame-preserving equal usable frame count/range. Map original frame index to the same proxy frame index, whose selected PTS is `proxy.presentation_origin + index * frame_duration`; different/nonzero PTS origins are therefore explicit. Rate/count/range/missing-metadata/out-of-range incompatibility selects the original frame and records a stable review reason; never guess an offset.
- Probe and extract only through `preprocess.py`'s existing ffprobe/ffmpeg boundary. Select by exact stream/frame index, scale long edge to 768 preserving aspect ratio, never upscale, and emit deterministic image bytes/settings suitable for hashing.
- Deduplicate identical `(source_id, source_time)` while preserving all requested frame IDs through a deterministic mapping; reject duplicate frame IDs with different times.
- This task does not infer motion/blur/exposure and does not rank HDR/Log color/exposure.
- Timing/range checks establish supported correspondence, not cryptographic visual-content equality. Do not claim the proxy pixels were proven semantically identical to original.
- Frame extraction subprocess remains in `preprocess.py`, with exact argv tests and no shell/network. Failure to establish proxy correspondence falls back to original; failure to read/extract the original is an explicit analysis error.

## Implementation steps

1. Write failing fake-extractor tests for intended association, proxy preference/fallback reasons, dedup/order/IDs, hashes, exact rational frame mapping, different/nonzero PTS origins, first/last frame, rate/count/range mismatch, out-of-range request, and missing original; add mocked exact ffprobe/ffmpeg argv/scale tests.
2. Generate deterministic synthetic original/proxy videos under pytest `tmp_path` with known per-frame visual identities and differing/nonzero PTS origins. Prove the selected decoded proxy image is the corresponding requested original frame; mocked seek argv alone is insufficient.
3. Implement the narrow preprocess boundary and pure correspondence/sample orchestration.
4. Run focused P5/P1/preprocess tests, invariants, full check; commit owned files.

## Acceptance

Same inputs yield identical requests/order/hashes. Compatible synthetic pairs with different PTS choose the known corresponding frame; incompatible/unproven pairs use original and record why. Proxy absence uses original; original failure is explicit. No image is treated as scene-quality evidence by this task, and timing tests are not reported as visual content-identity proof. `make check` passes offline. Actual proxy/4K color fidelity is `PENDING_HUMAN`.

## Failure route

Report sanitized source kind, frame ID/time, expected/actual argv/hash/dedup, base/tests/diff. Do not add filename matching or quality inference. Repeat failures follow diagnosis policy.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, proxy/fallback counts, pending real-media gate, and `ready-to-integrate`.
