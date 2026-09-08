# Task P6-02: build an immutable deterministic EditPlan

## Authority

Binding spec `Visual scoring and deterministic roughcut` plus `Input and output`; depends on P6-01 and saved P5 vision decisions. The plan is the single revision consumed by FCPXML and EDIT SRT.

## Baseline and ownership

- Fresh `fcp-p6-edit-plan` worktree from exact P6-01-integrated SHA.
- Modify `vlogsrt/project/edit.py`; create `tests/project/test_edit_plan.py`. Safe synthetic project data is inline or generated under pytest `tmp_path`; no fixture/golden file is added or modified.
- No XML/SRT serialization, network, source trace mutation, or fixture/API edits.

## Contract

```python
@dataclass(frozen=True, slots=True)
class RetainedInterval:
    instance_id: str
    source_frames: FrameInterval
    input_timeline_frames: FrameInterval
    output_timeline_frames: FrameInterval
    reason: str

@dataclass(frozen=True, slots=True)
class RemovedInterval:
    instance_id: str
    source_frames: FrameInterval
    input_timeline_frames: FrameInterval
    restore_before_instance_id: str

@dataclass(frozen=True, slots=True)
class ReviewIssue:
    code: str
    message: str
    instance_id: str | None
    source_frame: int | None

@dataclass(frozen=True, slots=True)
class EditPlan:
    revision: str
    input_sha256: str
    config_sha256: str
    retained: tuple[RetainedInterval, ...]
    removed: tuple[RemovedInterval, ...]
    decisions: tuple[VisionDecision, ...]
    review: tuple[ReviewIssue, ...]

def build_edit_plan(document: ProjectDocument, partition: BlockPartition, guards: Sequence[InstanceGuard], decisions: Sequence[VisionDecision], cfg: ProjectConfig) -> EditPlan: ...
def validate_edit_plan(plan: EditPlan, document: ProjectDocument, guards: Sequence[InstanceGuard]) -> tuple[str, ...]: ...
```

- Apply at most one known accepted candidate per block; keep all for missing/error/uncertain/low-confidence/unique-event decisions.
- Retain the chosen 3-second candidate and ripple-remove the rest of that block. Preserve source audio/order/existing gaps, never remove a whole clip, never shorten dialogue/locked instances.
- Revalidate snapped cuts against guards; any overlap rejects that decision to keep-all plus review, never removes protected frames.
- Review evidence is structured, not timestamp prose. A location-bearing `ReviewIssue` carries both `instance_id` and `source_frame`, the same exact asset-start-anchored source-frame index used by each retained interval's `source_frames`; this is file time before adding FCPXML `asset.start`, never project/output elapsed time. An unlocated issue carries both location fields as `None`; reject half-populated coordinates. `message` is for humans and is never parsed to recover marker timing; `code` is deterministic machine-readable evidence. P6-04 may turn only located generated issues into XML markers and keeps all issues in `review.json`.
- Output frames are contiguous after ripple within original storyline semantics; existing gaps remain with their durations/order. Restoration mapping identifies exact removed source/input range and insertion location.
- `revision` hashes the input/config/partition/full saved decisions and resulting intervals. Same saved inputs produce byte-identical plan; no temperature/dict-order promise.
- Safe synthetic fixture must produce at least one nonempty removed interval; a no-op plan is not adequate acceptance.

## Implementation steps

1. Write failing tests for accepted/low/error/unique decisions, unknown IDs, one-per-block, guard collision fallback, lock, full-clip prevention, ripple/gaps/order, deterministic revision, structured located/unlocated review issues, rejection of half-populated marker provenance, and actual shortening.
2. Implement pure plan/validator; feed only saved decisions and config.
3. Run focused/all P6 tests, invariants, full check; commit owned paths.

## Acceptance

No removed frame intersects a guard; no source/clip/gap ordering changes; safe fixture actually shortens; identical saved inputs yield identical revision/serialization data. `make check` passes with zero network.

## Failure route

Report block/candidate/instance IDs, input/output/guard frames, decision normalization, base/tests/diff. Guard removal or full-clip deletion is a hard Astra escalation. Other repeats use diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, before/after duration, revision, review cases, and `ready-to-integrate`.
