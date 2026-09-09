# Task P5-03: build the conservative speech guard and project it to instances

## Authority

Binding spec `VAD guard`; depends on P5-02, P3 source traces, P4 boundary projection, and P1 frame math. Ends the VAD branch for P5 integration.

## Baseline and ownership

- Fresh `fcp-p5-guard` worktree from exact SHA containing P5-02 and required integrated contracts.
- Create `vlogsrt/project/vad/guard.py`, `tests/project/test_guard.py`.
- No VAD model execution, vision/edit decisions, source mutation, or XML writes.

## Contract

```python
@dataclass(frozen=True, slots=True)
class SourceGuard:
    source_id: str
    protected: tuple[SampleRange, ...]
    vad_without_text: tuple[SampleRange, ...]
    protected_samples: int
    total_samples: int
    status: Literal["success", "keep_all"]
    review: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class InstanceGuard:
    instance_id: str
    protected_frames: tuple[FrameInterval, ...]
    review: tuple[str, ...]

def build_source_guard(trace: SourceTrace, reference: PcmReference, vad: VadResult, boundary: BoundaryProjection, repairs: Sequence[Repair], cfg: ProjectConfig) -> SourceGuard: ...
def project_guard(source_guard: SourceGuard, reference: PcmReference, slices: Sequence[LogicalSlice], frame_duration: Fraction) -> tuple[InstanceGuard, ...]: ...
```

- Protect the union of every Whisper segment/source cue, successful boundary cue, VAD window with `probability >= 0.35`, repair range, and failed/uncertain coverage, then pad outward by 0.5 seconds and clamp.
- Never drop positive runs shorter than 250 ms or any other minimum. Adjacent/overlapping ranges merge exactly.
- Empty STT alone is not silence. If VAD/dependency/runtime/coverage is error or missing, protect the entire audible retained source and return `keep_all` with review.
- VAD-positive audio with no usable STT is protected and listed in `vad_without_text`/review.
- `project_guard` requires the real `PcmReference` used by `build_source_guard`. Validate `source_guard.source_id == reference.source_id`, `source_guard.total_samples == reference.sample_count`, a positive exact `reference.sample_rate`, and matching slice `source_id` values before projection; use these actual field names and reject missing/ambiguous linkage.
- Convert each protected sample endpoint to decoded source-file time exactly once as `Fraction(sample_index, reference.sample_rate) + reference.pcm_sample0_file_time`, intersect it with the logical slice's file-time interval, then snap outward on the frame grid anchored at that slice's exact `asset_start`. Never treat a sample index as a frame index, omit/add the PCM origin twice, or use zero/timeline position as the source-grid anchor. Disabled/video-only instances are retained but create no audible guard removal authority.
- Declared-no-audio and pre-PCM-unavailable resources have no `PcmReference`, VAD call, `SourceGuard`, or `project_guard` call. Only declared-no-audio slices are authored `audible=False`; a declared-audio stream/PCM failure remains authored audible but is carried by P4's typed `UnavailableInstance`. All unavailable instances stay explicit downstream keep-all with zero removal authority; never fabricate a full-range PCM guard or call the projection using another equal-content resource's reference.

## Implementation steps

1. Write failing tests for every union source, 0.5 s padding, no short-run drop, tail, empty/error, VAD-only speech, repeated instances, mute/disabled/no-audio/pre-PCM-unavailable cases, zero-call no-reference behavior, and outward frame anchoring. Include positive and negative PCM origins, nonzero `asset_start`, exact sample/rate-to-file-time mapping, source-ID and `total_samples == sample_count` rejection, origin-added-once evidence, and equal-content resources proving an unavailable resource cannot borrow the healthy resource's reference/guard.
2. Implement pure interval union/projection and protected proportion metrics.
3. Run focused P5/P4/P1 tests, invariants, full check; commit owned files.

## Acceptance

No protected sample/frame can fall outside the guard; all error modes keep all and review; short/faint/tail windows survive. Repeated instances receive separately anchored guards from the validated reference origin and each slice's asset epoch. Unavailable instances bypass source projection and remain typed keep-all evidence. `make check` passes.

## Failure route

Report source/instance IDs, input/output intervals, probability/status, frame anchor, base/tests/diff. Any protected-range removal is a hard Astra escalation; no threshold tweak is allowed.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, protected proportions/review cases, and `ready-to-integrate`.
