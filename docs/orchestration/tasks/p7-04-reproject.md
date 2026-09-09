# Task P7-04: reproject captions after a supported user recut

## Authority

Binding spec `Persistence, CLI and UI` captions/reproject; depends on P7-03, P4 boundaries, and P1 identity. May run parallel with P7-05 only if branches start at the same P7-03 SHA and own disjoint files.

## Baseline and ownership

- Fresh `fcp-p7-reproject` worktree from exact P7-03-integrated SHA.
- Create `vlogsrt/project/reproject.py`, `tests/project/test_reproject.py`; modify `vlogsrt/project/commands.py` only to wire captions result, and narrowly extend `vlogsrt/project/fcpxml.py` plus `tests/project/test_fcpxml.py` with the explicit recaption parser seam below.
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

def parse_project_for_recaption(path: Path) -> tuple[ProjectDocument, tuple[str, ...]]: ...
```

- Rebind resources by verified resource identity/content hash, never XML id `r2`, basename, or filename alone. Resolve each updated resource's persisted `UnavailableSourceAudio` and derived unavailable-instance metadata by exact resource identity before any content-key trace reuse, so an equal-content healthy resource cannot donate analysis or suppress a failure.
- Reuse immutable source traces and cached exact-key BoundaryTranscripts. Do not rerun vision/edit policy.
- New source or uncached true boundary returns `needs_analysis` with keys and no partial final SRT.
- Declared-no-audio retains every matching instance, emits no cue/request, and may return success with an explicit keep/review notice. Persisted `stream_unavailable` or `pcm_unavailable` returns `needs_analysis` for that exact resource, preserves unrelated draft evidence internally but emits no partial final SRT, and cannot be relabeled as a new source, empty transcript, or completed analysis. Unknown/mismatched unavailable metadata is `unsupported`/stale before reuse.
- P1's `parse_project(path)` remains strict and unchanged by default. `parse_project_for_recaption` shares the same parser but may ignore only the explicitly supported imported caption elements, returning a `replace-existing-SRT` notice; it must not ignore connected spoken audio, titles, transitions, compound/sync/multicam, retime, or other unsupported effects wholesale. Those constructs still produce deterministic unsupported results before projection.
- Output is elapsed zero-based, exact Fraction/frame mapping with one final ms rounding; source records unchanged.

## Implementation steps

1. Write failing tests for reordered XML IDs, repeated assets, same filename/different content, user cuts with cached/uncached boundaries, new source, strict-default caption rejection, caption-only recaption parsing/notice, noncaption construct rejection in recaption mode, connected audio/transition rejection, and no vision calls. Add declared-no-audio success/retention, both retryable pre-PCM reasons returning `needs_analysis`, exact unavailable resource-instance rebind, missing/contradictory metadata refusal, and equal-content healthy/unavailable resources in shuffled order.
2. Implement strict identity rebind and cached projection; wire CLI result/exit 3.
3. Run focused P7/P4/P1/CLI tests, invariants, full check; commit owned files.

## Acceptance

Corrected supported recut produces synchronized SRT with zero client calls; `r2`/filename-only and unavailable-disposition mismatches refuse; uncached or retryable-unavailable work returns needs_analysis rather than guessed captions. Declared-no-audio instances remain unchanged without analysis. `make check` passes.

## Failure route

Report old/new scoped/content IDs, cache keys, unsupported construct location/reason, expected/actual status, base/tests/diff. Identity mismatch or silent partial final output escalates immediately.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, rebind/cache/status matrix, and `ready-to-integrate`.
