# Task P0-02: codify the source/derived project contract

## Authority

Binding spec `Source contracts and architecture` and P0 acceptance; depends on P0-01 environment-ready evidence and Opus approval of this shared boundary. Sol owns the brief, GLM owns code/tests, Astra reviews and merges P0.

## Baseline and ownership

- Baseline: the exact P0 phase base SHA recorded by Astra after bootstrap; worker HEAD must equal it.
- Fresh branch/worktree: `fcp-p0-contract` / `fcp-p0-contract`, explicit approved base.
- Create: `docs/PROJECT_CONTRACTS.md`.
- Modify: `pyproject.toml`, `scripts/check_invariants.py`, `tests/test_invariants.py`.
- Do not create `vlogsrt/project/**` yet. Do not modify legacy runtime modules, fixtures, root orchestration authority docs, or protected paths.

## Contract

- Core direct dependencies remain exactly `openai`, `tomli-w`, `rich`.
- Add optional extra `project = ["numpy>=2.0", "onnxruntime>=1.19"]`; optional libraries are allowed only below the exact VAD implementation subtree `vlogsrt/project/vad/**`. They must not become imports of other project modules, legacy top-level `vlogsrt/*.py`, or `ui/**`. Add the narrow mypy missing-import override needed for ONNX Runtime rather than weakening global type checking.
- The derived package name is frozen as `vlogsrt.project`. Legacy modules may never import `vlogsrt.project` or relative `.project`, except exact composition root `vlogsrt/cli.py` may import only the public `vlogsrt.project.commands` entry point. Dependency direction is otherwise project -> legacy public boundaries only.
- OpenAI-compatible network calls remain physically in `vlogsrt/transcribe.py`. ffmpeg/ffprobe subprocesses remain physically in `vlogsrt/preprocess.py`.
- The sole additional subprocess boundary is exact path `vlogsrt/project/xml_validation.py`, and calls from it are restricted to `xmllint`; renaming an import or calling `subprocess.run`/`Popen`/`check_*` through an alias must still be detected.
- Path exceptions are matched by exact repository-relative path, never basename. A nested spoof such as `vlogsrt/project/nested/transcribe.py` or `vlogsrt/project/nested/preprocess.py` receives no network/subprocess privilege.
- Project orchestration must reject hardcoded model identifiers matching `glm-*`; model names belong in project configuration.
- Dataclasses in exact `vlogsrt/project/models.py` are FCPXML transport models only and must be frozen. Other phase models live in their owning modules/subpackages.
- `verify.py` remains output-read-only and project protection must not depend on verification being enabled.

Required checker-facing interfaces:

```python
PROJECT_PACKAGE = PACKAGE / "project"
PROJECT_OPTIONAL_DEPENDENCIES = {"numpy", "onnxruntime"}
XML_VALIDATION_MODULE = PROJECT_PACKAGE / "xml_validation.py"

def _legacy_sources() -> list[Path]: ...
def _project_sources() -> list[Path]: ...
def check_source_does_not_import_project() -> list[str]: ...
def check_project_dependency_scope() -> list[str]: ...
def check_subprocess_boundaries() -> list[str]: ...
def check_no_hardcoded_project_models() -> list[str]: ...
def check_project_models_are_frozen() -> list[str]: ...
```

Existing public check function names may delegate to these helpers, but all 16 existing invariant groups must remain active and their behavior must not be weakened.

## Implementation steps

1. Add failing tests using temporary Python modules/AST inputs: legacy absolute and relative project imports fail; the exact CLI composition exception passes but wider project imports fail; aliased NumPy/ONNX imports outside the exact VAD subtree fail; nested basename spoofs fail; aliased subprocess use outside approved files fails; non-`xmllint` commands in the XML boundary fail; the approved fixed `xmllint` call shape passes; hardcoded `glm-*` values fail; mutable FCPXML dataclasses fail.
2. Refactor the checker to traverse legacy and project sources separately and inspect imports/calls with `ast`, not filename-changing regex loopholes.
3. Add the `project` optional extra without altering Python `>=3.11`, core deps, dev, or UI extras.
4. Write `docs/PROJECT_CONTRACTS.md` with a dependency-direction table, immutable source Trace/spine/cue rule, source-vs-derived timestamp rule, exact network/subprocess exceptions, optional dependency scope, and explicit statement that project deletion/editing never mutates source records.
5. Run focused tests, the invariant script, then `make check`; commit only the four owned paths.
6. Before tests, prove the environment resolves this worker checkout:

   ```bash
   .venv/bin/python -c 'import sys,vlogsrt; print(sys.version, vlogsrt.__file__)'
   ```

## Acceptance

```bash
.venv/bin/python -m pytest tests/test_invariants.py -q
.venv/bin/python scripts/check_invariants.py
make check
git diff --check HEAD^
```

- Tests demonstrate each forbidden direction fails and each narrow exception passes; no test merely asserts constants exist.
- Existing baseline tests remain `389` plus new tests; no skip/xfail and no fixture/golden edits. `vlogsrt.__file__` is inside the worker worktree.
- Final commit contains only owned paths and excludes every protected/user-dirty path.

## Failure route

Report AST case, expected/actual diagnostic, exact base/commit, commands/exits, and changed files. Do not widen exceptions to resolve a checker failure. Two repeats route to Terra/Luna; unresolved contract decisions route Opus then Astra; GLM implements the resulting fix.

## Report

Initial assignment completed: Run `run_b6d40d052690`, Task `task_f7788389960b`, Dispatch `ctx_df9c602e28a2`, terminal `term_ea415fa1-9b4f-402d-922f-4d6dbe039b22`, branch `Ssojux2/fcp-p0-contract`, worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p0-contract`, base `676460111bad5ef04cbc9d5c776940611fa938a7`, commit `df489bb0a8c077ed6c74c9709dfa929432129d59`. Focused 80 and full 453 tests passed, but Astra held it for the binding revision recorded at integration commit `9727b2b`.

Live correction assignment: Run `run_b6d40d052690`, Task `task_7d03430673e1`, Dispatch `ctx_51f56ba3df5c`, terminal `term_7a94892e-6f38-4421-8273-51b889ce2619`, same branch/worktree, base `df489bb0a8c077ed6c74c9709dfa929432129d59`, launcher `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`. Opus owns supervision/review; do not duplicate or merge until the corrected commit and evidence are reported.

Worker reports Orca task/dispatch/launch receipt, branch/worktree/base SHA, commit SHA, changed files, failing-first evidence, focused and full checks, and remaining findings. Mark `ready-to-integrate`, not merged.
