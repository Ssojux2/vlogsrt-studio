# Task P4-04: replace boundary cues by slice and source interval

## Authority

Binding spec `Boundary transcripts` and P4 acceptance; depends on P4-03. Ends P4.
Coordinator/reviewer: Opus 5 (Run `run_a091d4869a98`). Offline only; Opus performs the merge.

## Baseline and ownership

- Worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p4-boundary-projection`, branch
  `Ssojux2/fcp-p4-boundary-projection`, already created from exact base `9747988` (which contains the
  P4-03 integration `f6af955`). Do not rebase, reset, squash, or re-create the branch.
- Allowed files: `vlogsrt/project/captions.py`, `vlogsrt/project/boundary.py`, and a new
  `tests/project/test_boundary_projection.py`. Nothing else.
- Forbidden: any XML/edit shortening, any transcription or network call, any source trace mutation,
  `tests/fixtures/api/**`, `golden/**`, `.env`, `.env.example`, and every file outside the allowed list.
- No new dependency. No live/paid call. No `--online`. No `VLOGSRT_OFFLINE=0`.
- P4-03 landed `BoundaryTranscript`, `transcribe_boundaries`, and the boundary clock helpers in
  `vlogsrt/project/boundary.py`; read that module before designing the projection so you consume the
  real shipped types instead of re-deriving them.

## Contract

```python
@dataclass(frozen=True, slots=True)
class BoundaryProjection:
    input_sha256: str
    cues: tuple[ProjectCue, ...]
    source_srt: str
    locked_instance_ids: frozenset[str]
    unavailable_instances: tuple[UnavailableInstance, ...]
    unresolved_request_ids: tuple[str, ...]
    unresolved_cue_ids: tuple[str, ...]

def project_boundaries(plan: BoundaryPlan, transcripts: Sequence[BoundaryTranscript], source: SourceCaptionSet, source_traces: Sequence[SourceTrace]) -> BoundaryProjection: ...
```

- Successful boundary trace's already repaired/refined cues are used instead of overlapping source cues for exactly `(slice_id, instance_ids, source interval)`; never append both and never refine/polish either cue set again.
- Join plan requests, boundary transcripts, and source traces by validated request/source/PCM keys, independent of input sequence order. Reject missing, duplicate, ambiguous, or inconsistent linkage and test shuffled transcript/trace order; never pair records with `zip` or infer identity from paths/stems.
- Repeated source instances outside that tuple are unchanged. Every source/boundary segment remains in its immutable trace.
- Empty/error boundary keeps clamped original text, adds `boundary_unresolved` and request ID to review, and remains draft-only until human resolution. Successful replacement regenerates the single pre-roughcut input-timeline SOURCE SRT; per-asset source-local artifacts and both source/boundary traces remain immutable.
- Timeline mapping uses exact P4 formula/Fractions. Output cues are ordered, non-overlapping within the flat storyline, and have `end > start`.
- Re-run the P3-03 millisecond-representability check only after exact boundary replacement/clamp. If `half_up_ms(end) <= half_up_ms(start)`, retain the exact derived cue, append `subms_collapsed`, add its cue ID to `unresolved_cue_ids`, and omit only that cue from regenerated `source_srt`. Derive current unresolved cue IDs from the current projected cues in deterministic order: retain unrelated/fallback failures, but when a successful exact boundary replacement removes an old placed cue and substitutes a representable cue for that same instance, do not carry the replaced cue's stale unresolved ID into `BoundaryProjection`. The immutable `SourceCaptionSet` remains the evidence for that earlier SOURCE collapse, including when SOURCE itself is still unrepresentable. The downstream review names cue ID and exact endpoints and blocks final acceptance; never mutate a trace, stretch/merge endpoints, invent a frame gap, silently discard the record, or guess a `source_frame` for an XML marker.
- Locked instances from the plan are returned unchanged; later edit policy must not shorten them again in the same run.
- `unavailable_instances` is copied from the plan byte-for-byte and in order after validating that every record's instance is locked. Declared-no-audio remains completed/non-billable and does not alone refuse readiness; `stream_unavailable` or `pcm_unavailable` remains retryable, keeps unrelated valid cues in an explicit draft, and blocks final acceptance. Never infer either state from an empty cue list or collapse a genuine post-PCM error `SourceTrace` into this pre-PCM record.

## Implementation steps

1. Write failing tests for one of two repeated instances, exact replace-not-append, shuffled transcript/trace order, missing/duplicate/mismatched linkage, leading/trailing cuts, empty/error fallback, lock propagation, no recursive request, and interval/order validation. Include the exact `Fraction(133, 8000)` to `+ Fraction(1, 24000)` 17-ms collapse and a collapse created by boundary clamp; both retain flagged exact cues and unresolved IDs while omitting only SRT emission. Add collapse-then-successful-representable-boundary replacement: it clears only the replaced instance's old unresolved cue ID while preserving unrelated/fallback failures and the immutable upstream SOURCE evidence. Add unchanged typed propagation for declared-no-audio and both retryable pre-PCM reason codes, locked-instance validation, equal-content healthy/unavailable resources, and proof that only retryable reasons block final acceptance.
2. Implement pure projection without modifying trace objects.
3. Run all P4/P3 tests, invariants, full check; commit owned files.

## Acceptance

Instance-scoped tests prove no deletion/replacement in the sibling instance; each overlap has exactly one provenance; failed boundaries keep original text and explicit unresolved marker. Millisecond-collapsed cues remain structured and visible, never appear as invalid zero-millisecond SRT cues, and prevent final-ready state. Typed unavailable metadata survives unchanged, prevents removal authority, and distinguishes completed declared absence from retryable readiness blockers. Continuous blade has zero extra STT. `make check` passes.

## Failure route

Report slice/instance/request/cue IDs and exact intervals, expected/actual provenance, base/tests/diff. Cross-instance deletion or silent missing text is an immediate hard stop: report it and wait. Two repetitions of the same failure -> stop and escalate to the coordinator (Opus 5), who routes to Fable 5.1. Code changes stay with GLM.

## Report

Before `worker_done`, send the exact committed SHA, `git diff --stat 9747988..HEAD` (which must list
exactly the three allowed files), the failing-first evidence for the new tests, and the green output of
every acceptance command with its test count, plus confirmation that zero network calls occurred.
Then send `worker_done` exactly once.

Acceptance commands, run from the worktree root with the main repo's interpreter
`/Users/ssojux2/Working/vlogsrt/.venv/bin/python`:

```
python -m pytest tests/project/test_boundary_projection.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

The full suite must pass with a count no lower than the 1,898 recorded at `f6af955` plus the new tests,
and every invariant group must print `[OK]`.
