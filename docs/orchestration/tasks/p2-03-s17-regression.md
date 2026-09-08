# Task P2-03: close the S17 chunk-origin drift regression

## Authority

Binding spec S17/P2 and acceptance `correct PTS/sample origins`; depends on P2-02. This task proves project full/intermediate/repair audio uses sample-derived origins and never the drifting MP3 copy-segment coordinate. Astra's repeatable offline diagnostic in `docs/orchestration/reviews/astra-s17-synthetic-evidence.md` is required pre-read evidence; it reproduces a synthetic legacy mismatch but explicitly does not prove the historical 77-minute incident's exact cause or magnitude.

## Baseline and ownership

- Fresh `fcp-p2-s17` worktree from exact P2-02-integrated SHA.
- Modify only `vlogsrt/project/pcm.py`, `tests/project/test_pcm.py`, and, if a failing legacy helper is demonstrably on the new project call path, `vlogsrt/preprocess.py` plus `tests/test_preprocess.py`.
- No broad legacy chunk rewrite, magic offset, threshold change, or fixture modification.

## Contract

```python
def split_retained_ranges(reference: PcmReference, ranges: Sequence[tuple[Fraction, Fraction]], directory: Path) -> tuple[tuple[SampleRange, Path], ...]: ...
```

- Full, intermediate, final, and repair ranges all derive offsets as `Fraction(sample_index, sample_rate)` from the same PCM reference.
- Ranges are ordered, non-overlapping, channel-frame complete, and compare byte-for-byte with the corresponding full PCM region.
- The final slice may end at the real last sample; no LAME/Xing/CSV offset or `+0.2` compensation enters the project path.
- Recorded control: a 12-second mono 16 kHz signed-16 impulse pattern encoded with legacy `libmp3lame` 64k defaults and split at `[2,4,6,8,10]` mapped every nonzero chunk `+576` samples (`+36 ms`) late, while direct full-PCM slicing was exact. The same CSV offset `4.032` had the same displacement when intermediate and final, so a historical final-versus-intermediate differential was **not reproduced**. These numbers are regression evidence only and must never become correction constants.
- Existing legacy source behavior remains compatible; the project adapter must be able to avoid `preprocess.split_at` entirely.

## Implementation steps

1. Add a deterministic synthetic PCM pattern and a failing regression covering six ranges, the same offset once intermediate and once final, a final short tail, and repair subranges.
2. Preserve or independently rerun the committed deterministic MP3/PCM diagnostic as a test/report boundary: the synthetic legacy coordinate mismatch is reproduced at `+576` samples, but the historical magnitude/cause and final-versus-intermediate distinction remain `not reproduced / PENDING_HUMAN`. A normal TDD failure because the new retained-PCM helper is absent proves only the new contract, not S17's historical root cause.
3. Add an assertion that project range extraction does not call the legacy MP3 `split_at` path.
4. Run all P2 tests, baseline timing-related tests, invariants, and `make check`; commit owned paths.

## Acceptance

All new retained-PCM positions show zero-sample mismatch, including last and repair slices; decoded lengths equal exact per-channel sample counts. Reports state the precise boundary: synthetic legacy MP3 mismatch reproduced, but historical final/intermediate difference and 77-minute cause/magnitude not reproduced. No `36 ms`, `576`-sample, `0.2`-second, or other literal/computed correction exists. `make check` passes. Actual 77-minute media verification is recorded `PENDING_HUMAN`, not PASS.

## Failure route

Report range/index, expected/actual first differing sample, origin Fraction, command/test, base/diff. Two identical failures request Terra root cause and Luna minimal repro before a GLM repair; unresolved goes to Opus/Astra.

## Report

Report Orca/launch IDs, branch/base/commit, files, synthetic diagnostic result (including `not reproduced` when applicable), retained-PCM contract evidence, tests, pending real-media gate, and `ready-to-integrate`.
