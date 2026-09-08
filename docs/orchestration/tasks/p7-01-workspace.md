# Task P7-01: persist content-keyed project revisions

## Authority

Binding spec `Persistence, CLI and UI`; depends on Astra's P6 merge. This task supplies storage primitives only; no scheduling or CLI.

## Baseline and ownership

- Fresh `fcp-p7-workspace` worktree from exact P6 merge SHA.
- Create `vlogsrt/project/workspace.py`, `tests/project/test_workspace.py`.
- No SQLite, network, CLI/UI, analysis execution, or other module changes.

## Contract

```python
@dataclass(frozen=True, slots=True)
class ArtifactRef:
    kind: str
    revision: str
    path: Path
    sha256: str
    completed: bool

@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    schema_version: int
    input_sha256: str
    config_sha256: str
    source_analysis: tuple[ArtifactRef, ...]
    vision_analysis: tuple[ArtifactRef, ...]
    plans: tuple[ArtifactRef, ...]
    errors: tuple[dict[str, object], ...]
    usage: tuple[dict[str, object], ...]

class StaleRevision(RuntimeError): ...

def open_workspace(path: Path, input_sha256: str) -> WorkspaceManifest: ...
def write_json_atomic(path: Path, value: Mapping[str, object]) -> ArtifactRef: ...
def require_revision(refs: Sequence[ArtifactRef], revision: str) -> tuple[ArtifactRef, ...]: ...
```

- Workspace is explicit caller path; no implicit global/cross-workspace cache. Directories/artifacts are content/revision keyed and reusable across resume/reproject.
- JSON manifests record input/config/model/request/response/plan revisions, error state, usage/cost. Completed artifacts are atomically written; temp/incomplete artifacts never load as complete.
- Reject stale plan/config/input and mixed revisions with `StaleRevision`; never choose newest implicitly.
- Paths are workspace-relative in JSON; reject traversal/symlink escape. No SQLite.
- Reuse requires verified content/request hashes and completed metadata. Size, mtime, basename, or `pipeline.source_fingerprint` may be recorded as diagnostics but can never prove cache validity or hide changed content.

## Implementation steps

1. Write failing tests for create/reopen, content reuse, same-stat changed-content rejection, interrupted write, hash mismatch, stale/mixed revisions, path traversal/symlink escape, concurrent atomic replacement, error/usage persistence.
2. Implement immutable manifest and bounded atomic JSON helpers.
3. Run focused P7/P6/invariant/full checks; commit owned files.

## Acceptance

Interrupted/temp data is never returned complete; exact revision is required; separate workspaces cannot see each other's artifacts; `make check` passes.

## Failure route

Report workspace-relative paths, expected/actual revision/hash/state, base/tests/diff. Never recover by accepting a mismatched artifact. Repeat failures follow diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, revision/recovery cases, and `ready-to-integrate`.
