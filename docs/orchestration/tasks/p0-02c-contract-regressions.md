# Task P0-02c: close the final optional-import and subprocess-alias regressions

## Authority and placement

This is Astra's bounded correction after direct AST/temp-tree review of P0-02b commit `972a81412756b89fd330da622f22f14a067065e5`. Work only in `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract` on `Ssojux2/fcp-p0-contract`; required clean starting HEAD is exactly that commit. Use a fresh `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto` session.

Modify only `scripts/check_invariants.py`, `tests/test_invariants.py`, and, only if wording must be corrected, `docs/PROJECT_CONTRACTS.md`. Do not touch runtime modules, orchestration docs, `.env*`, `golden/**`, `tests/fixtures/api/**`, fixtures, or any other file. No paid/live calls, push, deploy, merge, bypass, or general static-analysis expansion.

## Reproduced defects

1. A temp source at `vlogsrt/project/vision/policy.py` containing `import onnxruntime as ort` currently returns no failures. `BANNED_IMPORTS` includes NumPy but omits ONNX Runtime, while `check_project_dependency_scope()` scans only legacy sources. Both `numpy` and `onnxruntime` imports must be inspected across all legacy and project source paths and allowed only strictly below directory `vlogsrt/project/vad/`; exact file `vlogsrt/project/vad.py`, `project/vision/**`, other project subtrees, legacy modules, and UI all fail.
2. A temp XML boundary with `from subprocess import run as launch`, a legitimate resolved `shutil.which("xmllint")` executable, and `launch([exe, "--nonet", "--noout", path], shell=True)` currently returns no failure. Bare statically resolved aliases of `subprocess.run` must pass through the same keyword/argv/provenance validation as `subprocess.run`; aliases of forbidden APIs such as `Popen`, `check_call`, `check_output`, and `call` must fail. Shadowed/unrelated aliases remain conservative failures.

## Test-first procedure and DoD

1. Prove the exact starting SHA, clean status, branch, and worktree-local `.venv` import.
2. Add isolated temp-tree/AST negative tests first and run them red against `972a814`: at minimum ONNX Runtime in vision/other project/legacy/UI/vad.py; NumPy outside VAD; aliased `run` with `shell=True`, opaque kwargs, executable override, wrong/missing/reordered flags, extra positional args; and aliases of every forbidden subprocess API. Preserve positive cases for both optional imports under `project/vad/**`, ordinary/aliased `run`, ordinary/aliased `shutil.which`, both exact argv shapes, shell omitted or literal `False`, and dynamic final path/DTD expressions.
3. Implement the smallest fix and keep all prior P0-02/P0-02b tests green, including exact commands-module semantics, glm guard, dataclass aliases, and prior executable/shell/provenance checks.
4. Run `.venv/bin/python -m pytest tests/test_invariants.py -q`, `.venv/bin/python scripts/check_invariants.py`, `make check`, and `git diff --check HEAD^`; commit only the owned paths and finish with a clean worktree.
5. Report exact red/green commands and counts, branch/base/commit, changed paths, and limitations. Mark ready for independent Opus review, not merged.
