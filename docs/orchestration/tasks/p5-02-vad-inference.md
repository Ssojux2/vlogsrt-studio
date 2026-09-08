# Task P5-02: run channel-wise continuous-state Silero inference

## Authority

Binding spec `VAD guard`; depends on P5-01 and P2 PCM. This task produces probability windows only, not a removal decision.

## Baseline and ownership

- Fresh `fcp-p5-vad-inference` worktree from exact P5-01-integrated SHA.
- Create `vlogsrt/project/vad/inference.py`; modify only the VAD package export if needed; create `tests/project/test_vad_inference.py`.
- No guard/caption/edit logic, artifact replacement, threshold changes, or other modules.

## Contract

```python
@dataclass(frozen=True, slots=True)
class VadWindow:
    channel: int
    start_sample: int
    end_sample: int
    probability: float

@dataclass(frozen=True, slots=True)
class VadResult:
    source_id: str
    status: Literal["success", "error"]
    windows: tuple[VadWindow, ...]
    error: str | None

def infer_vad(reference: PcmReference, source_id: str, cfg: ProjectConfig, session: object) -> VadResult: ...
```

- Normalize int16 to float32 per channel, never mix channels. Run each channel separately and union later to avoid phase cancellation.
- For each channel/asset, initial state is zeros shape `(2,1,128)` and context zeros length 64. Each call input is previous 64 real/padded samples plus the next 512-sample window; returned state feeds the next call.
- Reset state/context between channels and assets, never between windows.
- Zero-pad the last partial window only for inference; `end_sample` excludes padding and never exceeds real sample count.
- Preserve every finite probability; NaN/inf/wrong output/runtime error makes the whole asset `error`, not partial success.

## Implementation steps

1. Write a recording fake session and failing tests for exact input/state/context sequence, stereo independence, reset points, final padding exclusion, zero length, and malformed/runtime errors.
2. Implement streaming NumPy/ONNX calls with config values only.
3. Run focused VAD/P2 tests, invariants, full check; commit owned files.

## Acceptance

Fake-session assertions prove state continuity and reset boundaries; phase-opposed stereo still yields two independent series; final real tail is represented once and no padded sample is timed. `make check` passes.

## Failure route

Report channel/window/sample indices, input/state shapes, expected/actual probability/status, base/tests/diff. Do not reset per window or average channels. Repeat failures follow diagnostic escalation.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, window/state evidence, and `ready-to-integrate`.
