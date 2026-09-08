# Task P0-04: establish the isolated project package and config skeleton

## Authority

Binding source/derived separation contract and Astra bootstrap disposition. This is a frozen shared-contract P0 task after corrected P0-02; it may run parallel with the disjoint source byte-lock task P0-03.

## Baseline and ownership

- Baseline: exact corrected P0-02 integration SHA supplied by Astra; abort on mismatch. May run parallel with P0-03 from that same SHA.
- Fresh branch/worktree: `fcp-p0-project-skeleton`, explicitly based on that SHA.
- Create `vlogsrt/project/__init__.py`, `vlogsrt/project/config.py`, `vlogsrt/project/models.py`, top-level `vlogsrt.project.toml`, and `tests/project/test_project_config.py`.
- Do not modify legacy source modules, CLI composition, FCPXML models, VAD/vision modules, or any protected path.

## Contract

- `ProjectConfig` is separate from legacy `vlogsrt.config.Config`; neither imports or subclasses the other.
- `vlogsrt.project.toml` is the single project-config root. The initial schema contains a version marker and no speculative P5 thresholds/model names; P5-00 extends it in a separately reviewed shared-contract task.
- Loading is deterministic and side-effect free. Unknown keys and unsupported schema versions fail with stable, actionable errors; a missing file follows one documented default policy.
- Importing `vlogsrt.project` or `vlogsrt.project.config` performs no network, subprocess, filesystem mutation, model loading, or legacy configuration mutation.
- `vlogsrt/project/models.py` establishes an explicitly FCPXML-only transport-model namespace. Do not invent speculative domain fields in P0; P1 fills the reviewed parser models. The checker must inspect every dataclass later added here and require `frozen=True`; P5 shared analysis models use separately owned `analysis_models.py`.

## Red/green procedure

1. Add failing tests for default/missing-file behavior, explicit file load, unknown key, unsupported version, separation from the legacy config type, and the FCPXML-only model namespace. P0-02 already owns mutation coverage for checker rejection of a mutable project dataclass; do not edit its invariant test file here.
2. Implement the minimum typed/frozen configuration object and loader required by those tests.
3. Run focused tests, invariant checker, and `make check`; assert the corrected project-model invariant passes on the new actual package.
4. Commit only the five owned paths; do not edit `tests/test_invariants.py`, `scripts/check_invariants.py`, any fixture/golden, or P0-03's test.

## Acceptance

- Project and legacy configuration types are distinct and do not import one another.
- Project model namespace is present, documented FCPXML-only, and guarded against mutable dataclasses without adding speculative P1 fields.
- All errors are deterministic and name the path/key/version that failed.
- Import-side-effect tests block network/subprocess calls and observe no write.
- `vlogsrt.__file__` resolves inside the worker worktree; no skip/xfail or protected path edit.

## Failure route

Report config path/key/version, expected/actual diagnostic, project/legacy import edge, exact base, commands/exits, and changed paths. Do not merge config types, search env files, add speculative fields, or weaken the frozen-model invariant to make a test pass.

## Report

Report launch receipt/model/effort, exact base/branch/worktree, commit SHA, owned paths, failing-first evidence, focused/full checks, and remaining findings. State is `ready-to-integrate`, not merged.
