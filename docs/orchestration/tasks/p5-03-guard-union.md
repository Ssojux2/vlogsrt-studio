# Task P5-03: build the conservative speech guard and project it to instances

## Authority

Binding spec `VAD guard`; depends on P5-02, P3 source traces, P4 boundary projection, and P1 frame math.
Ends the VAD branch for P5 integration. Coordinator/reviewer: Opus 5 (Run `run_a091d4869a98`).
Offline only; Opus performs the merge.

## Baseline and ownership

- Worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p5-guard`, branch `Ssojux2/fcp-p5-guard`,
  already created from exact base `daadf29`, which contains the whole integrated P4 phase
  (`cb6b039`) and P5-00/01/02/04/05. Do not rebase, reset, squash, or re-create the branch.
- Allowed files: create `vlogsrt/project/vad/guard.py` and `tests/project/test_guard.py`.
  You may add an export line to `vlogsrt/project/vad/__init__.py` if that package's existing
  convention requires it — check first and match it; touch nothing else in that file.
- Forbidden: VAD model execution, vision/edit decisions, source mutation, XML writes,
  `tests/fixtures/api/**`, `golden/**`, `.env`, `.env.example`, and every other file.
- No new dependency (`numpy`/`onnxruntime` are the existing optional project extra and stay
  optional — this module must import and be testable without them unless the existing
  `vlogsrt/project/vad/` modules already establish otherwise; follow their pattern exactly).
- No live/paid call, no `--online`, no `VLOGSRT_OFFLINE=0`.
- Read these before designing, and consume the real shipped types rather than re-deriving them:
  `vlogsrt/project/vad/inference.py` (P5-02 `VadResult`), `vlogsrt/project/boundary.py`
  (`BoundaryProjection`, `LogicalSlice`, `UnavailableInstance`), `vlogsrt/project/pcm.py`
  (`PcmReference`, `SampleRange`), `vlogsrt/project/speech.py` (`SourceTrace`), and the P1 frame
  helper that provides `interval_outward`.
- Note the P4-04b precedent: an exact upstream value is always preferred over reconstructing one
  downstream. Do not infer any time you can read.

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
- Convert each protected sample endpoint to common container/file time exactly once as `Fraction(sample_index, reference.sample_rate) + reference.pcm_sample0_file_time`; the measured PCM origin performs the audio-to-file-clock conversion and must not be omitted or added twice. Intersect that interval with the logical slice's file-time interval. Then snap it either as `interval_outward(asset_start + file_start, asset_start + file_end, frame_duration, asset_start)` or equivalently `interval_outward(file_start, file_end, frame_duration, Fraction(0))`, and retain the returned source-grid indices. Never feed raw file endpoints to `interval_outward(..., anchor=asset_start)`: for example, an XML asset epoch of 300 seconds and a file endpoint near 5 seconds must not produce a large negative frame index. Disabled/video-only instances are retained but create no audible guard removal authority.
- Declared-no-audio and pre-PCM-unavailable resources have no `PcmReference`, VAD call, `SourceGuard`, or `project_guard` call. Only declared-no-audio slices are authored `audible=False`; a declared-audio stream/PCM failure remains authored audible but is carried by P4's typed `UnavailableInstance`. All unavailable instances stay explicit downstream keep-all with zero removal authority; never fabricate a full-range PCM guard or call the projection using another equal-content resource's reference.

## Implementation steps

1. Write failing tests for every union source, 0.5 s padding, no short-run drop, tail, empty/error, VAD-only speech, repeated instances, mute/disabled/no-audio/pre-PCM-unavailable cases, zero-call no-reference behavior, and outward frame anchoring. Include positive and negative PCM origins, nonzero `asset_start`, the equivalent XML-clock/file-clock snapping formulas, a regression where raw file endpoints plus an asset anchor would produce a negative index, exact sample/rate-to-file-time mapping, source-ID and `total_samples == sample_count` rejection, origin-added-once evidence, and equal-content resources proving an unavailable resource cannot borrow the healthy resource's reference/guard.
2. Implement pure interval union/projection and protected proportion metrics.
3. Run focused P5/P4/P1 tests, invariants, full check; commit owned files.

## Acceptance

No protected sample/frame can fall outside the guard; all error modes keep all and review; short/faint/tail windows survive. Repeated instances receive separately anchored guards from the validated reference origin and each slice's asset epoch. Unavailable instances bypass source projection and remain typed keep-all evidence. `make check` passes.

## Failure route

Report source/instance IDs, input/output intervals, probability/status, frame anchor, base/tests/diff. Any protected-range removal is an immediate hard stop: report it and wait. No threshold tweak is allowed — `0.35`, `0.5 s`, and `250 ms` come from the binding spec and must live in project config or named module constants, never as literals inside a function body (CLAUDE.md rule 8). Two repetitions of the same failure -> stop and escalate to the coordinator (Opus 5), who routes to Fable 5.1.

## Report

Before `worker_done`, send the exact committed SHA, `git diff --stat daadf29..HEAD` (which must
list only the allowed files), the failing-first evidence for the new tests, and the green output of
every acceptance command with its test count and the invariant group count, plus confirmation that
zero network calls occurred. Then send `worker_done` exactly once.

Acceptance commands, run from the worktree root with
`/Users/ssojux2/Working/vlogsrt/.venv/bin/python`:

```
python -m pytest tests/project/test_guard.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

Expected: Ruff clean, mypy clean, **all 19** invariant groups `[OK]` — count them rather than
trusting a summary line — and the full suite passing at no fewer than the 2,058 recorded at
`cb6b039` plus your new tests.
