# P0 contract checkpoint accepted and integrated

Final implementation: `c7af1593e659f290323bdcde12c365e6740dabaf` on
`Ssojux2/fcp-p0-contract`. Astra integration commit:
`45d1393f7e2c2e200868f9c5972ceb20d3409d95` on `feat/final-cut-project`.
Verified 2026-09-09 KST / 2026-09-08 15:06 UTC.

The implementation covers exact source/project import boundaries, VAD-only NumPy
and ONNX Runtime imports, the exact CLI commands composition exception, model
configuration boundaries, frozen FCPXML dataclass declarations, and the two fixed
xmllint argv forms with actual subprocess/shutil alias handling. Source timing and
immutability wording distinguishes asset-local cache records from input/edited
timeline SRT projections. Core runtime modules were not changed by this checkpoint.

## Review and verification

- Sol independently reviewed the exact final diff, 176 focused invariant tests,
  19 invariant groups, clean worktree and owned paths.
- Astra independently read the final implementation and executed 20 additional
  AST/temporary-tree probes covering every ratified requirement, including the
  previously missed ONNX scope and aliased subprocess shell/executable/kwargs cases.
  All passed; modeled subprocess commands were never executed.
- Astra ran `make check` both in the final worker checkout and after integration:
  **549 tests passed**, Ruff lint/format passed, mypy passed for 23 source files,
  and **19 invariant groups passed**. Integration test time was 2.68 seconds.
- Diff checks passed. Relative to the original worker base, only
  `docs/PROJECT_CONTRACTS.md`, `pyproject.toml`, `scripts/check_invariants.py`, and
  `tests/test_invariants.py` changed. The user's unstaged `.env.example` was excluded.

Opus supplied earlier critical review, but its actual session hit a usage limit
before final `c7af159` review (reported reset 01:00 Asia/Seoul). Astra therefore used
the user's designated final escalation role to complete this review and integrate.
Do not describe the final SHA as independently approved by Opus. Its broader
review Dispatch remains service-limited, not falsely completed.

## P0-03 source byte lock integration

Astra reviewed the complete inline expectations and offline fixture setup at
`2a1034beaaea9cd15edd847e51f2d43444980dc0`, then merged that one-file change in
`9b7d4e2295729589805574e3f0ab9a947ce97af5`. After integration, `make check`
passed **553 tests**, Ruff lint/format, mypy, and all 19 invariant groups.
The test uses default legacy Config and normal verification, prevents env-file
discovery, and compares exact UTF-8 bytes for four synthetic fixture shapes.

## Remaining P0 work

P0 is not complete yet. P0-04 package/config skeleton runs from approved
`45d1393f7e2c2e200868f9c5972ceb20d3409d95` and must pass review and integration
checks before the P0 phase join. Real Final Cut/media/live-model acceptance remains
pending; none is implied by static contracts or synthetic tests.
