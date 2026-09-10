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
    unavailable_instances: tuple[UnavailableInstance, ...]
    review: tuple[ReviewIssue, ...]

def build_edit_plan(document: ProjectDocument, partition: BlockPartition, guards: Sequence[InstanceGuard], decisions: Sequence[VisionDecision], cfg: ProjectConfig) -> EditPlan: ...
def validate_edit_plan(plan: EditPlan, document: ProjectDocument, guards: Sequence[InstanceGuard]) -> tuple[str, ...]: ...
```

- Apply at most one known accepted candidate per block; keep all for missing/error/uncertain/low-confidence/unique-event decisions.
- Retain the chosen 3-second candidate and ripple-remove the rest of that block. Preserve source audio/order/existing gaps, never remove a whole clip, never shorten dialogue/locked instances.
- Copy `partition.unavailable_instances` unchanged into the plan and require every listed instance to have no block and no removed interval. Emit a deterministic unlocated keep/review issue per record: declared-no-audio is completed absence and does not alone block final acceptance, while `stream_unavailable` and `pcm_unavailable` remain retryable readiness blockers. Resource-scoped records must not suppress or relabel an equal-content healthy instance.
- Revalidate snapped cuts against guards; any overlap rejects that decision to keep-all plus review, never removes protected frames.
- Review evidence is structured, not timestamp prose. A location-bearing `ReviewIssue` carries both `instance_id` and `source_frame`, the same exact asset-start-anchored source-frame index used by each retained interval's `source_frames`; this is file time before adding FCPXML `asset.start`, never project/output elapsed time. An unlocated issue carries both location fields as `None`; reject half-populated coordinates. `message` is for humans and is never parsed to recover marker timing; `code` is deterministic machine-readable evidence. P6-04 may turn only located generated issues into XML markers and keeps all issues in `review.json`.
- Output frames are contiguous after ripple within original storyline semantics; existing gaps remain with their durations/order. Restoration mapping identifies exact removed source/input range and insertion location.
- `revision` hashes the input/config/partition/full saved decisions, typed unavailable metadata, and resulting intervals/review. Same saved inputs produce byte-identical plan; no temperature/dict-order promise.
- Safe synthetic fixture must produce at least one nonempty removed interval; a no-op plan is not adequate acceptance.

### 배포된 guard 타입 (P5-03b 이후 — 이 모양을 그대로 소비하라)

```python
@dataclass(frozen=True, slots=True)
class InstanceGuard:
    instance_id: str
    source_id: str
    status: Literal["success", "keep_all", "inaudible"]
    protected_frames: tuple[FrameInterval, ...]   # 소스 격자, 반개 [start,end), 정렬·병합됨
    review: tuple[str, ...]
```

- **`status`를 쓰고 `review` 산문을 파싱하지 마라.** `review`는 사람이 읽는 기록이고
  한국어다. 분기 판단은 `status`로만 한다(P5-03b 항목 6이 이걸 위해 추가됐다).
  - `success` — 분석이 성공했다. 여집합에서 자를 후보를 고를 수 있다.
  - `keep_all` — 실패 닫힘. 그 인스턴스는 통째로 보호이고 **제거 권한이 없다.**
  - `inaudible` — 선언된 무음·비활성·영상 전용. 역시 제거 권한이 없다.
    (둘이 겹치면 `inaudible`이 이긴다 — 더 강한 진술이다.)
- **`status`를 슬라이스 길이나 `protected_frames` 범위로 추측하지 마라.**
  `keep_all`과 `inaudible`은 둘 다 슬라이스 전체를 덮으므로 기하로는 구분되지 않는다.
- 합쳐진 슬라이스의 모든 인스턴스는 **슬라이스 전체**의 프레임을 받는다. 맞는 동작이니
  **클립 경계는 문서(`ProjectDocument`)에서 가져오고 guard 범위에서 유도하지 마라.**
- `source_id`가 실렸으니 인스턴스 매칭을 `instance_id` 하나로 하지 말고 둘 다 대조하라.

`SourceGuard`도 `analysis_key`와 `pcm_sample0_file_time`을 싣는다(P5-03b 항목 3).
P6가 직접 쓸 일은 없지만, 세대가 다른 guard를 섞어 받지 않도록 상류에서 검증된다.

## Implementation steps

1. Write failing tests for accepted/low/error/unique decisions, unknown IDs, one-per-block, guard collision fallback, lock, full-clip prevention, ripple/gaps/order, deterministic revision, structured located/unlocated review issues, rejection of half-populated marker provenance, and actual shortening. Add all three unavailable reasons, exact typed carry-through, zero removal/no-block enforcement, retryable-versus-declared readiness review semantics, duplicate/unknown resource-instance rejection, and an equal-content healthy sibling that remains independently eligible.
2. Implement pure plan/validator; feed only saved decisions and config.
3. Run focused/all P6 tests, invariants, full check; commit owned paths.

## Acceptance

No removed frame intersects a guard or unavailable instance; no source/clip/gap ordering changes; safe fixture actually shortens; identical saved inputs yield identical revision/serialization data including unavailable metadata. Declared absence may complete with review, but retryable pre-PCM failure cannot be final-accepted. `make check` passes with zero network.

## Failure route

Report block/candidate/instance IDs, input/output/guard frames, decision normalization, base/tests/diff. Guard removal or full-clip deletion is a hard Astra escalation. Other repeats use diagnostics.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, before/after duration, revision, review cases, and `ready-to-integrate`.
