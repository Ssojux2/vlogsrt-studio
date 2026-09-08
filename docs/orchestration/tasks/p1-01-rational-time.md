# Task P1-01: implement exact rational and frame-grid time

## Authority

Binding spec `Source analysis, clocks, identity` and P1 acceptance. Depends on Astra's P0 integration SHA. This task freezes the time interface consumed by every later FCPXML, guard, edit, and caption task.

## Baseline and ownership

- Fresh branch/worktree `fcp-p1-time`, explicitly based on the approved P0 merge SHA.
- Create `vlogsrt/project/timebase.py`, `tests/project/test_timebase.py`; modify the existing project package export only if required.
- No other product/docs/test files. Do not add dependencies or read protected paths.

## Contract

```python
SUPPORTED_FRAME_DURATIONS: frozenset[Fraction]

@dataclass(frozen=True, slots=True)
class FrameInterval:
    start_frame: int
    end_frame: int

def parse_fcpx_time(value: str) -> Fraction: ...
def format_fcpx_time(value: Fraction) -> str: ...
def require_supported_frame_duration(value: Fraction) -> Fraction: ...
def frame_floor(value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()) -> int: ...
def frame_ceil(value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()) -> int: ...
def frame_nearest(value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()) -> int: ...
def interval_outward(start: Fraction, end: Fraction, frame_duration: Fraction, anchor: Fraction) -> FrameInterval: ...
def milliseconds_half_up(value: Fraction) -> int: ...
def format_srt_time(value: Fraction) -> str: ...
```

- Parse integer or `numerator/denominator` seconds with trailing `s`; reject missing suffix, zero denominator, and negative durations where the caller requests a duration.
- Supported frame durations are exactly `1/24`, `1/25`, `1/30`, `1/50`, `1/60`, `1001/24000`, `1001/30000`, `1001/60000` seconds.
- All calculations remain `Fraction`; `Fraction(str(float_value))` is the only legacy-float conversion rule.
- Frame intervals are half-open. Outward snapping floors start and ceils end relative to `anchor`. Nearest uses half-up ties for non-negative frame positions.
- Project SRT rounds exact `Fraction` values half-up to milliseconds once at serialization; equal adjacent boundaries serialize identically and no cumulative rounding is allowed. This is deliberately independent of the legacy float serializer.

## Implementation steps

1. Write parameterized failing tests for every rate, `30` versus `30000/1001`, nonzero/negative epochs, half-frame ties, outward intervals, adjacent SRT boundaries, and invalid/reversed values. Include the exact counterexample `format_srt_time(Fraction(1001, 2000)) == "00:00:00,501"` while existing `vlogsrt.output.format_timecode(0.5005) == "00:00:00,500"` remains unchanged.
2. Implement the minimal immutable model and pure functions above; no float arithmetic and no milliseconds stored in domain models.
3. Run focused tests, invariant checker, then full `make check`; commit owned paths only.

## Acceptance

```bash
.venv/bin/python -m pytest tests/project/test_timebase.py -q
.venv/bin/python scripts/check_invariants.py
make check
```

- Roundtrip `parse_fcpx_time(format_fcpx_time(x)) == x` for all fixture fractions.
- 30 fps and 29.97 fps produce different correct frame indices over long durations.
- Pure serialization error is at most 1 ms and adjacent end/start has exactly the same string.
- The Fraction counterexample passes without changing the legacy serializer or its established byte output.

## Failure route

Report the exact rational input, rate, anchor, expected/actual numerator/denominator, base SHA, commands, and diff. Never patch with epsilon floats. Repeated failure follows Sol -> Terra/Luna -> Opus -> Astra, with fixes by GLM.

## Report

Provide Orca IDs/launch receipt, branch/base/commit, files, failing-first test, focused/full results, and remaining findings; state `ready-to-integrate` only.
