# Task P0-01: record the Python 3.12 development environment

## Authority

Binding spec `Phases and acceptance` P0 and `Evidence at handoff`; manager Sol; reviewer Opus for evidence shape; Astra owns the Orca task/dispatch and phase gate. This is environment validation, not product implementation.

## Baseline and ownership

- Baseline is the exact bootstrap-doc commit SHA supplied by Astra on `feat/final-cut-project`; abort if the worker HEAD differs. Do not substitute `origin/main` or the pre-bootstrap `de17d875...` SHA.
- Branch/worktree: fresh Orca child worktree named `fcp-p0-env`, explicitly based on the approved SHA.
- Allowed writes: ignored `.venv/**` and ordinary ignored tool caches produced by checks.
- Tracked files: none. Do not edit or commit a tracked file; report `commit: none (evidence-only task)`.
- Forbidden: `.env`, `.env.*`, `golden/**`, `tests/fixtures/api/**`, fixture recording, online flags, push, merge.

## Contract

- Consume `/Users/ssojux2/.local/bin/python3.12`, which must report Python `3.12.14` or another `3.12.x` accepted by Astra.
- Produce a local `.venv/bin/python` whose `sys.version_info[:2] == (3, 12)`, whose installed packages pass `uv pip check --python .venv/bin/python`, and which can run `make check` with no network/API use.
- Preserve project runtime metadata `requires-python = ">=3.11"`; this task does not change it.
- A uv-created venv is valid without an importable `pip` module. Do not diagnose that state as a defect and do not add pip or change the Makefile merely to make `python -m pip` available.

## Implementation steps

1. Record `git rev-parse HEAD`, `git status --short`, `command -v python3.12`, `python3.12 --version`, and the existing venv version/pip result. Do not open any env file.
2. If recreation is needed, recreate only the worktree-local ignored venv with uv:

   ```bash
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python --cache-dir /private/tmp/vlogsrt-uv-cache -e ".[dev]"
   ```

3. Verify interpreter and packaging state:

   ```bash
   .venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12); print(sys.version)'
   .venv/bin/python -c 'import sys,vlogsrt; print(sys.version, vlogsrt.__file__)'
   uv pip check --python .venv/bin/python
   ```

4. Run `make check`; retain the four command sections and test count in the worker report.
5. Confirm `git status --short` contains no worker-created tracked or untracked source change. Do not commit an empty commit.

## Acceptance

- `python3.12 --version` and `.venv/bin/python --version` are Python 3.12.x.
- `uv pip check --python .venv/bin/python` exits 0.
- `vlogsrt.__file__` resolves inside the current worktree, not the root checkout or another worker worktree.
- `make check` exits 0: ruff lint and format, mypy, pytest, and invariant checker all run (no SKIP substituted for a check).
- Git diff is empty for tracked files; protected paths were neither read nor changed.
- Any package-download/network failure is reported as an environment blocker, not hidden by using the system Python.

## Failure route

Report the exact command, exit code, last 30 relevant lines, interpreter path/version, approved base SHA, and whether failure is resolver/network/tooling/test. Sol may narrow the environment brief; repeated identical failure goes to Terra/Luna diagnostics, then Opus/Astra. No product-code workaround belongs in this task.

## Report

This evidence has been accepted from the root checkout: Python 3.12.14 and baseline `make check` with 389 passing tests. No standalone P0-01 worker should be dispatched. If a later worktree fails the interpreter/import-path preflight, that worker reports the local environment failure rather than reopening this task.
