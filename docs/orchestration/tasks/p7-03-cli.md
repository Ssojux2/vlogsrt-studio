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

- `inspect` parses/resolves/estimates only and prints readiness plus measured quantities, currency/price when complete, `unpriced_components`, `unestimated_resource_ids`, and limitations when incomplete; no network. It may use explicit local media metadata to construct a provenance-tagged duration estimate but cannot fabricate a PCM reference or source trace and cannot assume retained XML duration is the full original duration.
- `inspect` reports every resource-scoped audio disposition: `no_audio: keep/review` for declared absence, retryable failed state for pre-PCM error, and the normal typed trace status after a real reference. It never labels pre-PCM error completed or billable, and unknown persisted variants/schema revisions exit before calls.
- `plan`, `export`, `captions`, and full `run` validate and display the same resource/instance/reason dispositions carried through P4/P6. They never derive state from an empty cue list: declared-no-audio retains its XML instances and may complete with keep/review, while `stream_unavailable` or `pcm_unavailable` produces draft/error status, zero removal for the affected instances, and no `final_accepted=true`. Missing or contradictory typed metadata is stale input, not a successful no-op.
- `analyze` and `run` require both explicit `--online` and existing cost confirmation/`--yes` for **every** online invocation; no duration/price threshold bypasses either gate. An incomplete/unpriced estimate is displayed as such rather than `$0`; explicit confirmation acknowledges that limitation but never substitutes for `--online`. Tests inject fakes only.
- `analyze` may prepare and measure local PCM before network authorization is consumed, then recomputes and displays the estimate before creating/invoking any client. It still makes zero client calls unless both authorization gates are present.
- Only after both authorizations may the CLI composition resolve the separately configured vision credential names and pass explicit values into the vision client factory. It must not reuse `OPENAI_API_KEY`/`OPENAI_BASE_URL`, print/log secret values, or read any key for offline commands; tests use a sentinel mapping/loader rather than real `.env` or process secrets.
- `plan`, `export`, and `captions` force runner `allow_network=False` regardless of environment.
- `captions` returns explicit `needs_analysis` nonzero status for uncached boundary/new source. Draft warnings are never labeled final.
- Existing `run`, `redo`, `eval`, `record`, `probe`, config, and UI commands retain their behavior/options.
- Stable exits: 0 success, 2 usage/unsupported input, 3 needs_analysis/stale, 4 analysis/runtime failure, 130 cancellation.

## Implementation steps

1. Write failing parser/command tests for every command, exit code, offline gates, cold/warm/unknown-duration/unpriced/partial estimate display, no threshold-based confirmation bypass, local PCM re-estimate before client use, media-map error, cost refusal, cancellation, stale/needs-analysis, separate-credential-name resolution after authorization only, secret non-disclosure, and legacy parser regression. Add all three unavailable reason displays across inspect/plan/export/captions/run, exact resource/instance identity, missing/contradictory metadata rejection, no-removal evidence, declared-no-audio completion, and retryable failure refusal of final-accepted output.
2. Add thin commands module over runner/workspace plus the narrow in-process `vlogsrt.cli` bridge; no product logic duplication in argparse callbacks or bridge code.
3. Run focused CLI/legacy CLI/P7/invariant/full checks; commit owned files.

## Acceptance

Every offline command produces zero fake client calls; analyze/run cannot call without two explicit authorizations; legacy commands pass unchanged; `make check` passes. No live call is made.

## Failure route

Report argv, exit, expected/actual call count/status, base/tests/diff. Do not infer online authorization from env. Repeat failures follow diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, command/exit matrix, zero-live-call evidence, and `ready-to-integrate`.
