# Task P7-03: expose project inspect/analyze/plan/export/captions/run commands

## Authority

Binding spec `Persistence, CLI and UI`; depends on P7-02. CLI must be complete before UI and reproject work.

## Baseline and ownership

- Fresh `fcp-p7-cli` worktree from exact P7-02-integrated SHA.
- Modify `vlogsrt/cli.py`; create `vlogsrt/project/commands.py`, `tests/project/test_project_cli.py`.
- No UI, network clients beyond runner injection, fixture recording, or legacy command behavior changes.

## Contract

Command surface:

```text
vlogsrt project inspect INPUT --workspace DIR [--media-map JSON]
vlogsrt project analyze INPUT --workspace DIR [--media-map JSON] --online --yes
vlogsrt project plan --workspace DIR
vlogsrt project export --workspace DIR --out DIR
vlogsrt project captions INPUT --workspace DIR --out FILE
vlogsrt project run INPUT --workspace DIR --out DIR [--media-map JSON] --online --yes
```

Public in-process UI bridge owned by exact `vlogsrt/cli.py`:

```python
def run_project_action(
    action: str,
    *,
    input_path: Path | None,
    workspace: Path,
    output_dir: Path | None,
    media_map: Path | None = None,
    allow_network: bool = False,
    confirmed: bool = False,
    clients: Mapping[str, object] | None = None,
) -> Iterator[Mapping[str, object]]: ...
```

- The bridge imports only public `vlogsrt.project.commands` and delegates every action to that layer. It exists so `ui.service` can import `vlogsrt.cli` without importing `vlogsrt.project` and without duplicating logic or spawning a subprocess; emitted mappings contain only sanitized progress/status/result paths.

- `inspect` parses/resolves/estimates only and prints readiness/cost; no network.
- `inspect` reports every resource-scoped audio disposition: `no_audio: keep/review` for declared absence, retryable failed state for pre-PCM error, and the normal typed trace status after a real reference. It never labels pre-PCM error completed or billable, and unknown persisted variants/schema revisions exit before calls.
- `analyze` and `run` require both explicit `--online` and existing cost confirmation/`--yes`; absence refuses before any call. Tests inject fakes only.
- Only after both authorizations may the CLI composition resolve the separately configured vision credential names and pass explicit values into the vision client factory. It must not reuse `OPENAI_API_KEY`/`OPENAI_BASE_URL`, print/log secret values, or read any key for offline commands; tests use a sentinel mapping/loader rather than real `.env` or process secrets.
- `plan`, `export`, and `captions` force runner `allow_network=False` regardless of environment.
- `captions` returns explicit `needs_analysis` nonzero status for uncached boundary/new source. Draft warnings are never labeled final.
- Existing `run`, `redo`, `eval`, `record`, `probe`, config, and UI commands retain their behavior/options.
- Stable exits: 0 success, 2 usage/unsupported input, 3 needs_analysis/stale, 4 analysis/runtime failure, 130 cancellation.

## Implementation steps

1. Write failing parser/command tests for every command, exit code, offline gates, media-map error, cost refusal, cancellation, stale/needs-analysis, separate-credential-name resolution after authorization only, secret non-disclosure, and legacy parser regression.
2. Add thin commands module over runner/workspace plus the narrow in-process `vlogsrt.cli` bridge; no product logic duplication in argparse callbacks or bridge code.
3. Run focused CLI/legacy CLI/P7/invariant/full checks; commit owned files.

## Acceptance

Every offline command produces zero fake client calls; analyze/run cannot call without two explicit authorizations; legacy commands pass unchanged; `make check` passes. No live call is made.

## Failure route

Report argv, exit, expected/actual call count/status, base/tests/diff. Do not infer online authorization from env. Repeat failures follow diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, command/exit matrix, zero-live-call evidence, and `ready-to-integrate`.
