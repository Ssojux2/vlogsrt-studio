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
- Each request `source_id` must equal `media.asset_content_id`. `source_time` is the common container/file-clock position produced from source-grid index `n` as `n * frame_duration`; `n` is not a decoded-video ordinal, and `asset.start` belongs only to the XML asset-clock transform. Derive `original_frame_index = (source_time - original.presentation_origin) / frame_duration` and require an exact integer in `[0, frame_count)`. A failure of this original mapping, including an off-grid or out-of-range request, is an explicit analysis error; falling back to the original cannot repair an invalid original request.
- The supported proxy case requires the same exact CFR frame duration and frame-preserving equal usable frame count/range. Map the decoded original frame index to the same proxy frame index, whose selected PTS is `proxy.presentation_origin + index * frame_duration`; different/nonzero PTS origins are therefore explicit. Proxy-only rate/count/range/missing-metadata/readability incompatibility selects the original frame and records a stable review reason; never guess an offset. Do not add a redundant request-index field or reject nonzero origins globally: the exact request-to-original integer check is the mapping authority.
- Matching nominal `r_frame_rate`/`avg_frame_rate` and frame count is necessary but not sufficient to prove the supported uniform CFR correspondence. `probe_video_stream` must inspect sufficient decoded presentation-timestamp evidence to establish that every usable decoded frame follows the declared origin-plus-ordinal grid and that count/range are consistent. Use integer PTS multiplied by the declared exact stream `time_base`, never rounded `pts_time` strings or an arbitrary seconds epsilon. The supported quantized-clock proof permits at most one declared timestamp tick of exact-rational error from `first_pts * time_base + ordinal * frame_duration` (accounting for independent quantization of the measured origin and frame timestamp), while requiring strictly increasing PTS and no missing/jumped ordinal; preserve the actual measured origin and PTS rather than correcting them. Missing/unknown/nonuniform evidence for the original is an explicit analysis error; the same failure for a proxy makes it unusable and falls back to the proven original.
- Clock example: with `asset.start=300`, original PTS origin 5, proxy PTS origin 11, `clip.start=305`, and 25 fps, source-grid index 125 means file/request time 5, decoded original/proxy index 0, and selected proxy PTS 11. Cover a three-frame offset as well. An audio first PTS such as 5.064 is an independent measured origin converted into the same file clock; it does not rebase the video grid.
- Probe and extract only through `preprocess.py`'s existing ffprobe/ffmpeg boundary. Select by exact stream/frame index, scale long edge to 768 preserving aspect ratio, never upscale, and emit deterministic image bytes/settings suitable for hashing.
- Deduplicate extraction by identical `(source_id, source_time)`, but return one `FrameSample` per unique `frame_id` in first-seen order; requests that share a time reuse the same `image_path` and hash. Collapse an identical duplicate `frame_id` plus time, and reject a duplicate frame ID with a different time.
- This task does not infer motion/blur/exposure and does not rank HDR/Log color/exposure.
- Timing/range checks establish supported correspondence, not cryptographic visual-content equality. Do not claim the proxy pixels were proven semantically identical to original.
- Frame extraction subprocess remains in `preprocess.py`, with exact argv tests and no shell/network. Failure to establish proxy correspondence falls back to original; failure to map, read, or extract the original is an explicit analysis error.

## Implementation steps

1. Write failing fake-extractor tests for intended association, exact `source_id`, proxy preference/fallback reasons, one-result-per-frame-ID dedup/order/hash reuse, conflicting duplicate IDs, exact rational common-clock mapping, original off-grid/out-of-range errors, independent nonzero original/proxy/audio PTS origins, first/last frame, proxy rate/count/range mismatch, and missing original; add mocked exact ffprobe/ffmpeg argv/scale tests. Include equal nominal rate/count with a real frame-timestamp jump: it is an original analysis error or proxy fallback according to which stream is unproven.
2. Generate deterministic synthetic original/proxy videos under pytest `tmp_path` with known per-frame visual identities and differing/nonzero PTS origins. Prove the selected decoded proxy image is the corresponding requested original frame for the 300/5/11-second epoch example and a three-frame offset; mocked seek argv alone is insufficient. Also cover a quantized-tick CFR stream such as 30000/1001 fps with `time_base=1/1000` and integer PTS `0,33,67,100,133,167,200,234`, which must pass despite literal timestamp deltas differing from `frame_duration`; contrast a genuine frame jump that must fail proof. Do not use skipped decoding as CFR evidence.
3. Implement the narrow preprocess boundary and pure correspondence/sample orchestration.
4. Run focused P5/P1/preprocess tests, invariants, full check; commit owned files.

## Acceptance

Same inputs yield identical requests/order/hashes, with one result per first-seen unique frame ID and shared image artifacts for equal source/time requests. Compatible synthetic pairs with different PTS choose the known corresponding decoded frame. Exact integer-PTS evidence distinguishes declared-tick-quantized CFR from a genuinely nonuniform/jumped grid; proxy-only incompatibility uses original and records why, while an invalid or unproven original mapping fails explicitly. Proxy absence uses original; original failure is explicit. No image is treated as scene-quality evidence by this task, and timing tests are not reported as visual content-identity proof. `make check` passes offline. Actual proxy/4K color fidelity is `PENDING_HUMAN`.

## Failure route

Report sanitized source kind, frame ID/time, expected/actual argv/hash/dedup, base/tests/diff. Do not add filename matching or quality inference. Repeat failures follow diagnosis policy.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, proxy/fallback counts, pending real-media gate, and `ready-to-integrate`.
