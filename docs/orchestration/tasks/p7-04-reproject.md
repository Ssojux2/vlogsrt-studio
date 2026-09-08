# Task P7-04: reproject captions after a supported user recut

## Authority

Binding spec `Persistence, CLI and UI` captions/reproject; depends on P7-03, P4 boundaries, and P1 identity. May run parallel with P7-05 only if branches start at the same P7-03 SHA and own disjoint files.

## Baseline and ownership

- Fresh `fcp-p7-reproject` worktree from exact P7-03-integrated SHA.
- Create `vlogsrt/project/reproject.py`, `tests/project/test_reproject.py`; modify `vlogsrt/project/commands.py` only to wire captions result.
- No UI, vision/edit rerun, new network calls, or source analysis mutation.

## Contract

```python
@dataclass(frozen=True, slots=True)
class ReprojectResult:
    status: Literal["success", "needs_analysis", "unsupported"]
    cues: tuple[ProjectCue, ...]
    notices: tuple[str, ...]
    missing_analysis_keys: tuple[str, ...]

def reproject_captions(updated: ProjectDocument, workspace: WorkspaceManifest, media_map: Sequence[MediaOverride] = ()) -> ReprojectResult: ...
```

- Rebind resources by verified resource identity/content hash, never XML id `r2`, basename, or filename alone.
- Reuse immutable source traces and cached exact-key BoundaryTranscripts. Do not rerun vision/edit policy.
- New source or uncached true boundary returns `needs_analysis` with keys and no partial final SRT.
- Existing imported captions may be ignored only with `replace-existing-SRT` notice. Connected spoken audio, transitions, compound/sync/multicam, retime, or unsupported effects return unsupported before projection.
- Output is elapsed zero-based, exact Fraction/frame mapping with one final ms rounding; source records unchanged.

## Implementation steps

1. Write failing tests for reordered XML IDs, repeated assets, same filename/different content, user cuts with cached/uncached boundaries, new source, existing captions notice, connected audio/transition rejection, and no vision calls.
2. Implement strict identity rebind and cached projection; wire CLI result/exit 3.
3. Run focused P7/P4/P1/CLI tests, invariants, full check; commit owned files.

## Acceptance

Corrected supported recut produces synchronized SRT with zero client calls; `r2`/filename-only mismatches refuse; uncached work returns needs_analysis rather than guessed captions. `make check` passes.

## Failure route

Report old/new scoped/content IDs, cache keys, unsupported construct location/reason, expected/actual status, base/tests/diff. Identity mismatch or silent partial final output escalates immediately.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, rebind/cache/status matrix, and `ready-to-integrate`.
