# Astra interface decisions during continuous execution

These clarify existing requirements before their owning tasks are dispatched. Sol
updates the task briefs; GLM implements. None expands the current P0-02 correction.

## P0 configuration and source byte lock

- Dependency correction at 2026-09-08 14:58 UTC: P0-03 consumes only the unchanged
  legacy source engine and owns one independent test file. It may start alongside
  P0-02c from exact `58bb25347319301391064d6ba9b6aad33a58f51c`; it does not consume
  the checker or project package, so their completion is not a prerequisite.
  Sol updates the brief/DAG accordingly. Maximum two coding workers remains.
  P0-04 still waits for the corrected contract; the P0 join requires all results.
- P0-03 uses `Config()` defaults, disables env-file discovery, and keeps all four
  independently reviewable inline expected byte strings, including the long one.
  Preserve ordinary verification behavior; verification may not change SRT bytes.
- P0-04 defines frozen `ProjectConfig(schema_version: int = 1)`,
  `ProjectConfigError(ValueError)`,
  `DEFAULT_PROJECT_CONFIG_PATH = Path("vlogsrt.project.toml")`, and
  `load_project_config(path: Path | None = None) -> ProjectConfig`.
- `None` loads the default relative to cwd; a missing default uses code defaults.
  An explicitly supplied missing path errors. Present malformed TOML, unknown keys,
  unsupported versions, non-integer versions (including bool) error with context.
  The TOML key is top-level `schema_version`; no parent/env-file discovery.
- P5-00 extends the same API/schema and existing config test file, preserving the
  version marker. P7-03 already owns `project/commands.py`; no P0 commands stub.

## P2/P3/P4 identity and timing seams

- Freeze an explicit source identity, original stem, PCM reference, and decoded
  origin linkage in the owning models/interfaces. Boundary and caption consumers
  must not infer these from parallel sequence positions or arbitrary paths.
- P4 currently receives independently ordered references/traces. Join by validated
  keys, reject missing/ambiguous linkage, and test shuffled reference order.
- P3 SOURCE placement needs the precise PCM origin independently of segment data,
  including empty traces. Boundary fixture namespaces need the original stem.
- P3-01 rejects missing/non-numeric/nonfinite segment values and response coverage
  mismatches. Finite zero/reversed legacy segment records remain available to the
  existing build/repair/refine engine as required by P3-02; they are not silently
  normalized, discarded, or conflated with successful empty responses.

## P6 atomic publication

Astra measured `os.replace(staged_nonempty_dir, prior_nonempty_dir)` in temporary
directories on the development Mac: `OSError`, errno 66, `Directory not empty`.
Do not implement a backup rename plus new rename and describe the pair as atomic.

Use complete immutable version directories and one atomically replaced current
symlink/pointer, or present another measured mechanism before implementation.
Returned `ExportPackage` paths resolve to one immutable revision, so consumers can
read all five artifacts consistently while the active pointer changes. State the
first-publish and existing-unmanaged-directory policies; leave user directories
untouched. Verify failure before switch preserves the prior complete version,
idempotence, and concurrent-writer conflict handling. Publication must never expose
an incomplete or mixed artifact set as complete.

## P7 UI and recaption parsing

- `ui/**` cannot import project implementation under the approved P0 invariant.
  P7-03 should own a narrow public Python bridge in `vlogsrt/cli.py` consumed by
  `ui.service`, or seek an explicit composition-rule decision before writing code.
  No duplicate product logic or subprocess-based UI bridge.
- P7-04 receives an already parsed document yet promises ignoring existing imported
  captions. The strict P1 parser must have an explicitly owned recaption parsing
  mode/seam, or P7-04 must own its later extension. Default remains strict.
  Ignore only supported imported caption elements with an explicit notice, never
  connected spoken audio, titles, transitions, or unsupported effects wholesale.
  Test strict default, caption-only recaption behavior, and noncaption rejection.

## Execution ownership

Sol task `task_8f65a57496f6`, dispatch `ctx_bfbd07ec7a4c` owns GLM launches and the
continuous P0-P7 implementation loop. Opus task `task_b15d7dad7f7b`, dispatch
`ctx_f925bd9d4a52` independently reviews requirements and commits. Astra owns
integration and remains active for merge/decision gates. The prior P0-02 branch at
`51319b99635f86a68543d02783674d4bf32eb06b` is held for the five ratified checks in
`astra-p0-contract-revision.md`; it is not complete merely because 461 tests pass.
