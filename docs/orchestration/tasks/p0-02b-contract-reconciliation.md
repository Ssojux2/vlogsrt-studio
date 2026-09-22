# Task P0-02b: reconcile the ratified source/derived contract

## Authority

This is the bounded correction to P0-02 ordered by Astra after independent review of `51319b99635f86a68543d02783674d4bf32eb06b`. Sol owns this brief and verification, GLM owns all code/tests/doc changes, Opus independently reviews the finished diff, and Astra alone merges.

## Exact placement and ownership

- Existing worktree: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract`.
- Existing branch: `Ssojux2/fcp-p0-contract`.
- Required starting HEAD: exactly `51319b99635f86a68543d02783674d4bf32eb06b`; stop and report if it differs or the worktree is dirty.
- Fresh agent/session: `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`.
- Modify only `scripts/check_invariants.py`, `tests/test_invariants.py`, and `docs/PROJECT_CONTRACTS.md`.
- Do not modify runtime modules, `pyproject.toml`, orchestration files, `.env*`, `golden/**`, `tests/fixtures/api/**`, or any other path. Do not read secrets, make paid/live calls, push, deploy, or merge.

## Astra reconciliation requirements (verbatim)

- NumPy and ONNX Runtime imports are allowed only under `vlogsrt/project/vad/`.
  The proposed VAD and vision ownership already matches this boundary.
- Exact `vlogsrt/cli.py` may import only `vlogsrt.project.commands` from the project
  package, including equivalent relative forms; other project imports still fail.
- Add the brief's glm model-literal guard now, with exact project config exception.
- Check dataclasses in exact `vlogsrt/project/models.py` require `frozen=True`;
  the absent file remains valid until P0-04. Avoid claiming deep immutability.
- Freeze the XML subprocess API to `subprocess.run` (including resolved aliases)
  and these two argv forms, with normal dynamic path expressions in the final
  slots: `[exe, "--nonet", "--noout", path]` or
  `[exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, path]`.
  Other methods/flags fail. P1-04 separately behavior-tests DTD URI conversion,
  path escaping, errors, and actual XML validation; do not implement P1 here.

## Previously required defects and wording that must remain fixed

- Reject an `executable` override and opaque keyword expansion in subprocess calls.
- Keep argv as the sole positional argument; reject extra positional arguments that could supply Popen's shell or executable parameters implicitly.
- Resolve `shutil` module imports and `which`-function imports, including aliases, rather than accepting any attribute named `which`. Reject unsupported or shadowed executable provenance conservatively.
- Preserve approved ordinary and aliased `subprocess`/`shutil` patterns, `shell` omitted or literal `False`, and dynamic path arguments in the literal argv list.
- Only `subprocess.run` or a statically resolved alias of that function is valid. `Popen`, `check_call`, `check_output`, `call`, `run` obtained from an unrelated module, and unresolvable/shadowed aliases fail.
- The CLI exception is the exact module `vlogsrt.project.commands`, not an arbitrary `commands` subtree. Exact module imports and symbols imported from that module pass (including equivalent relative forms); an import such as `vlogsrt.project.commands.parser` fails.
- Frozen-dataclass declaration detection recognizes ordinary aliases such as `import dataclasses as dc; @dc.dataclass(...)` and `from dataclasses import dataclass as dc; @dc(...)`, without becoming a general name-resolution engine.
- Per-asset cached source SRT is immutable asset-local time. Packaged SOURCE SRT projects those cues onto the input project's elapsed timeline before automatic cuts, including repeated instances. EDIT SRT projects onto the edited timeline. No source artifact is rewritten.
- Describe the checker as enforcement of listed syntactic boundaries only. Source immutability and projection require behavioral tests; do not claim a general static or Python security sandbox.

## Test-first procedure

1. Before editing, prove `.venv/bin/python` imports `vlogsrt` from this worktree and record the output.
2. Add isolated AST/temp-tree tests first and run them red. Cover positive and negative cases for every reconciliation requirement and every preserved defect above. Tests must use inline source or pytest `tmp_path`; no fixture/golden additions or edits.
3. Implement the smallest checker repair. Exact-path semantics matter: nested basename spoofs receive no exception, and equivalent relative CLI imports are normalized to the same narrow `project.commands` boundary.
4. Update `docs/PROJECT_CONTRACTS.md` so it states the exact import/model/dataclass/subprocess boundaries and corrected SOURCE/EDIT timing language without overclaiming semantic or runtime enforcement.
5. Run focused tests, the invariant program, and full offline checks. Commit only the three owned paths.

## Required evidence and definition of done

- Red evidence names tests that failed against `51319b9`, then green evidence shows those same tests passing.
- Positive cases include ordinary and aliased `subprocess.run`, ordinary and aliased `shutil.which`, both exact allowed argv shapes, dynamic final path/DTD expressions, exact CLI `project.commands` absolute and equivalent relative forms, optional imports under `project/vad/**`, no `project/models.py`, `frozen=True` dataclasses, and model literals inside exact `vlogsrt/project/config.py`.
- Negative cases include optional imports outside VAD (including `project/vision`), any other CLI project import, `glm-*` literals outside exact project config, mutable or undecorated dataclasses in exact project models, unsupported subprocess methods/aliases, bad/missing/reordered flags, extra positional args, `shell=True`, opaque kwargs, executable overrides, unrelated/unsupported/shadowed `which` provenance, and nested filename spoofs.
- `.venv/bin/python -m pytest tests/test_invariants.py -q` passes with no skip/xfail.
- `.venv/bin/python scripts/check_invariants.py` passes all invariant groups.
- `make check` passes offline.
- `git diff --check HEAD^` passes; `git status --short` is clean after the commit; the commit diff contains exactly the three owned paths.
- Report exact branch, base SHA, commit SHA, changed paths, red/green commands and results, focused/full counts, and any remaining limitation. Mark ready for Opus review, not merged.

## Stop conditions

Do not broaden this into a general static-analysis engine, modify runtime code, implement P1 behavior, change protected fixtures, or weaken existing invariant groups. On an ambiguity or blocked permission, ask the coordinator through the live Orca dispatch; do not bypass approval or continue into another task.
