# Task P6-02: build an immutable deterministic EditPlan

## Authority

Binding spec `Visual scoring and deterministic roughcut`; depends on P6-01.
Coordinator/reviewer: Opus 5 (Run `run_a091d4869a98`). Offline only; Opus performs the merge.

**이 브리프는 Fable 5.1의 P6 사전 검토로 한 번 고쳐졌다** —
`docs/orchestration/reviews/fable-p6-prephase.md`. 신원·레일·검증 항목을 단순화하지 마라.
P6는 **처음으로 구간을 실제 제거하는 계층**이다. CLAUDE.md 규칙 4와 9를 먼저 읽어라.

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
    restore_at_output_frame: int      # 이 구간이 되돌아갈 출력 타임라인 프레임

@dataclass(frozen=True, slots=True)
class RetainedGap:
    instance_id: str
    input_timeline_frames: FrameInterval
    output_timeline_frames: FrameInterval

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
    frame_duration: Fraction               # document.frame_duration. P6-03이 격자를 되묻지 않는다
    locked_instance_ids: frozenset[str]    # 검증자와 P6-03이 대조할 수 있게
    retained: tuple[RetainedInterval, ...]
    gaps: tuple[RetainedGap, ...]          # 출력 스토리라인. P6-04가 리플을 재계산하지 않는다
    removed: tuple[RemovedInterval, ...]
    decisions: tuple[VisionDecision, ...]
    unavailable_instances: tuple[UnavailableInstance, ...]
    review: tuple[ReviewIssue, ...]

def build_edit_plan(document: ProjectDocument, partition: BlockPartition, guards: Sequence[InstanceGuard], decisions: Sequence[VisionDecision], cfg: ProjectConfig) -> EditPlan: ...
def validate_edit_plan(plan: EditPlan, document: ProjectDocument, guards: Sequence[InstanceGuard]) -> tuple[str, ...]: ...
```

### 신원·레일·적용 규칙 (Fable 사전 검토)

**해시 선검사가 첫 관문이다.** `build_edit_plan`은 블록을 하나도 읽기 전에
`partition.input_sha256 == document.input_sha256`과 `partition.config_sha256 == cfg.sha256()`를
요구한다. 어긋나면 **`raise`** — 그건 모델 결함이 아니라 **섞인 리비전**이다.
그다음 `_require_config(cfg)`(P6-01과 같은 레일).

**판정이 이 블록을 실제로 봤는지 증명하라 — P5-06과 P6-01b가 이걸 위해 있었다.**
`VisionDecision.request_sha256`(P5-06)과 `edit.vision_request_for(block, adjacent_context)`
(P6-01b의 정본 투영)이 이미 배포돼 있다. `build_edit_plan`은 후보를 적용하기 전에
그 블록의 투영으로 만든 요청의 `policy.request_sha256(...)`가 판정의 `request_sha256`과
같은지 요구한다. 다르면 **`raise`** — 파티션·설정 해시 불일치와 같은 줄(섞인 리비전)이다.

`vision_request_for`를 **반드시 거쳐라.** 재료를 직접 모아 해시하면 러너(P7)와
검증자(여기)가 다른 것을 해시하게 되고, 그 순간 이 증명은 아무것도 증명하지 않는다.
id 일치만으로는 부족하다 — 같은 `block_id`라도 다른 기하에서 온 판정이 있을 수 있고,
`request_sha256`은 **프레임과 후보 창까지** 묶는다.

**적용은 `status`로만 한다.** 후보를 적용하는 조건은 셋의 교집합이다:
`decision.status == "accepted"` **그리고** `choice`가 **그 블록의** 후보 id **그리고**
`math.isfinite(confidence) and confidence >= cfg.vision_apply_confidence`.
마지막 재검사는 **좁히기만** 한다 — `status`는 파싱 시점에 `_normalize`가 확정했다.
**`confidence`나 `reason`에서 승인을 다시 유도하지 마라.** 그러면 NaN이 비교를 무력화하는
P5-05b가 그대로 재현된다.

**raise와 keep의 선:**

| 상황 | 처리 |
|---|---|
| 설정 결함, 파티션/문서/설정 해시 불일치 | **`raise`** (섞인 리비전·호출자 잘못) |
| 모르는 `block_id`, 남의 블록 후보를 가리키는 `choice` | **`raise`** (같은 이유) |
| `status="accepted"`인데 `choice`가 `keep_all`/`uncertain` | keep_all + `ReviewIssue(code="decision_inconsistent")` |
| 비유한 `confidence`, 한 블록에 판정 둘 | 같음 |
| guard와 겹치는 스냅 결과 | 그 판정만 keep_all + review. **보호 프레임은 절대 제거하지 않는다** |

**타임라인 격자 앵커를 한 번 명시하라.** 인덱스 `m`은 입력 경과 `m × frame_duration`이고
`sequence.tcStart`는 이미 빠져 있다(ingest가 모든 `offset`을 그 격자에, 모든 클립 `start`를
애셋 격자에 강제한다).

### `validate_edit_plan`은 규칙 9를 기계로 지킨다

반환은 브리프대로 `tuple[str, ...]`이고 **비어 있지 않으면 P6-04가 렌더를 거부한다.**
최소한 이것들을 검사하라:

- **프레임 보존**: 인스턴스마다 `Σretained + Σremoved == 클립 프레임 수`
- **출력 타일링**: `retained ∪ gaps`의 출력 구간이 `[0, output_end)`를 정확히 덮고
  겹치지 않으며 입력 순서를 지킨다
- `removed ∩ protected == ∅`
- 잠긴 인스턴스와 `unavailable_instances`에는 `RemovedInterval`이 **하나도 없다**
- 모든 `removed`는 **한 블록 안**에 있다
- 인스턴스마다 **최소 1프레임은 남는다** (클립 통째 삭제 금지의 기계적 표현)

`guards`가 이 검증에 필요하므로 `render_edit_fcpxml`도 같은 `guards`를 받아야 한다
(P6-04에서 다룬다).

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
