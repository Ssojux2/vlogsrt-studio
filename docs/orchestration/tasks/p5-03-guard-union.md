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
def project_guard(source_guard: SourceGuard, slices: Sequence[LogicalSlice], frame_duration: Fraction) -> tuple[InstanceGuard, ...]: ...
```

- Protect the union of every Whisper segment/source cue, successful boundary cue, VAD window with `probability >= 0.35`, repair range, and failed/uncertain coverage, then pad outward by 0.5 seconds and clamp.
- Never drop positive runs shorter than 250 ms or any other minimum. Adjacent/overlapping ranges merge exactly.
- Empty STT alone is not silence. If VAD/dependency/runtime/coverage is error or missing, protect the entire audible retained source and return `keep_all` with review.
- VAD-positive audio with no usable STT is protected and listed in `vad_without_text`/review.
- Map into each audible instance and snap outward to the asset-start-anchored frame grid. Disabled/video-only instances are retained but create no audible guard removal authority.
- Declared-no-audio and pre-PCM-unavailable resources have no `PcmReference`, VAD call, or `SourceGuard`. Their P4 slices are already inaudible; retain the instances with explicit review/keep-all disposition and never fabricate a full-range PCM guard.

## Implementation steps

1. Write failing tests for every union source, 0.5 s padding, no short-run drop, tail, empty/error, VAD-only speech, repeated instances, mute/disabled/no-audio/pre-PCM-unavailable cases, zero-call no-reference behavior, and outward frame anchoring.
2. Implement pure interval union/projection and protected proportion metrics.
3. Run focused P5/P4/P1 tests, invariants, full check; commit owned files.

## Acceptance

No protected sample/frame can fall outside the guard; all error modes keep all and review; short/faint/tail windows survive. Repeated instances receive separately anchored guards. `make check` passes.

## Failure route

Report source/instance IDs, input/output intervals, probability/status, frame anchor, base/tests/diff. Any protected-range removal is a hard Astra escalation; no threshold tweak is allowed.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, protected proportions/review cases, and `ready-to-integrate`.
