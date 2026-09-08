# Task P0-03: lock exact legacy SOURCE SRT bytes

## Authority

Binding source-immutability contract and Astra bootstrap disposition. This task runs only after P0-02 is integrated. It proves the existing source engine remains byte-identical before any legacy source module is modified in later phases.

## Baseline and ownership

- Baseline: exact corrected P0-02 integration SHA supplied by Astra; abort on mismatch. May run parallel with P0-04 from that same SHA.
- Fresh branch/worktree: `fcp-p0-source-bytes`, explicitly based on that SHA.
- Create only `tests/test_source_immutability.py`, using inline data, existing read-only offline fixture inputs where needed, and pytest `tmp_path` for generated output.
- Product runtime files are read-only. Existing `golden/**`, `tests/fixtures/api/**`, and `.env*` are forbidden.

## Contract

- Exercise the complete existing offline source presentation path for four deterministic synthetic transcript shapes: `demo`, `loopy`, `loopyletter`, and `loopylong`.
- The test must prevent environment-file discovery before invoking the pipeline (for example, monkeypatch `env.find_env_file` to return `None`, reset its cache, and set only non-secret fixture/offline variables). It must neither inspect `.env*` nor resolve/read any API key.
- Assert exact UTF-8 SRT bytes, including cue numbering, timestamps, line breaks, punctuation, and final newline. Do not normalize or regenerate expected bytes during the assertion.
- The lock covers the legacy serializer and source repair/refine presentation behavior. It does not bless a future project serializer or claim media/live-model coverage.
- Expected bytes must be independently reviewable inline byte literals; the test may not compute its expectation by calling the implementation under test. No fixture/golden file may be added or modified.

## Red/green procedure

1. Capture the current deterministic outputs through the forced-offline fixture path with environment-file discovery disabled, and review them against the existing source tests and serializer behavior.
2. Add exact byte assertions; prove the test detects a one-byte mutation of a copied expectation or actual value.
3. Run the focused regression, existing source/refine/pipeline tests, and `make check`.
4. Commit only `tests/test_source_immutability.py`. No product-code, fixture, golden, invariant-checker, or shared-test edit is permitted.

## Acceptance

- All four named shapes have exact byte locks with independent expectations.
- A deliberate one-byte mutation produces a precise failing diff before restoration.
- Existing behavior is unchanged; no skip/xfail and no protected fixture edit.
- Worker proves `vlogsrt.__file__` resolves inside its own worktree and reports focused/full test counts.

## Failure route

Report the named synthetic shape, first byte offset and expected/actual escaped bytes, exact base, commands/exits, and changed paths. Do not update an expectation to follow an unexplained behavior change; any source-byte discrepancy returns to Astra for disposition before a GLM correction.

## Report

Report launch receipt/model/effort, exact base/branch/worktree, commit SHA, owned paths, mutation-failure evidence, focused tests, and `make check`. State is `ready-to-integrate`, not merged.
