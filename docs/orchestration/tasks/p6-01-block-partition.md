# Task P6-01: partition eligible unprotected runs into edit blocks

## Authority

Binding spec `Visual scoring and deterministic roughcut`; depends on Astra-integrated P4 and P5. This task is pure geometry and produces candidates/requests, not an edit decision or network call.

## Baseline and ownership

- Fresh `fcp-p6-blocks` worktree from exact joint P4/P5 merge SHA.
- Create `vlogsrt/project/edit.py`, `tests/project/test_block_partition.py`.
- No XML/caption output, model call, config changes, or source mutation.

## Contract

```python
@dataclass(frozen=True, slots=True)
class EditBlock:
    block_id: str
    instance_id: str
    source_id: str
    timeline_frames: FrameInterval
    source_frames: FrameInterval
    candidates: tuple[VisionCandidate, ...]
    sample_requests: tuple[FrameSampleRequest, ...]

@dataclass(frozen=True, slots=True)
class BlockPartition:
    blocks: tuple[EditBlock, ...]
    kept_short_remainders: tuple[tuple[str, FrameInterval], ...]
    unavailable_instances: tuple[UnavailableInstance, ...]

def partition_blocks(document: ProjectDocument, media: Sequence[ResolvedMedia], guards: Sequence[InstanceGuard], locked_instance_ids: AbstractSet[str], unavailable_instances: Sequence[UnavailableInstance], cfg: ProjectConfig) -> BlockPartition: ...
```

- Eligible runs are complements of the instance guard inside each original audible clip, only when all required analysis succeeded and run length is at least frame-ceiled 6 seconds. Never cross a clip/gap/instance boundary.
- Validate `unavailable_instances` by exact document `resource_scoped_id` plus `instance_id` before content/guard lookup, require every such instance in `locked_instance_ids`, reject duplicates/unknown resources/instances/reason codes, and copy the typed records unchanged into `BlockPartition`. Resource-scoped unavailability wins over any equal-content healthy guard.
- Locked/broken/uncertain/muted/disabled/declared-no-audio/pre-PCM-unavailable instances yield no edit blocks. Only declared-no-audio is authored inaudible; a declared-audio pre-PCM failure is still excluded by its explicit unavailable record and lock. A video-only or unavailable slice is conservative keep-all, not analysis success or removable silence.
- For run length `L`, try `n=ceil(L/max_frames)` balanced integer-frame blocks whose sizes differ by <=1 frame. Use them only if every block >= min frames; otherwise take greedy max-frame blocks and keep the short remainder unchanged.
- `min_frames=ceil(6s/frame)`, `max_frames=floor(10s/frame)`, `keep_frames=nearest(3s/frame)`, all from config/timebase.
- `source_frames` use the source frame grid where index `n` denotes common container/file time `n * frame_duration`; `asset.start + n * frame_duration` is the corresponding XML asset-clock position. Derive their bounds from the retained XML clip/file intervals. Never treat `n` as a decoded-video ordinal or assume `probe_video_stream(...).frame_count` equals the XML source-grid end.
- Each block has beginning/middle/end keep candidates of exactly `keep_frames` when possible, deduplicated by exact frame interval; never a full-delete candidate.
- Every block and candidate interval is half-open `[start_frame, end_frame)`. Required endpoint samples are the first valid frame `start_frame` and last valid frame `end_frame - 1`, never `end_frame` (which may equal the asset frame count).
- Sample requests are nominal 1 fps plus the valid block endpoints and three candidate centers. Snap nominal times and centers with P1's deterministic exact-rational `frame_nearest` rule to integer source-grid indices, clamp inside the block, then emit `FrameSampleRequest.source_time = index * frame_duration` in the common file clock. Deduplicate with stable unique frame IDs and reduce to <=16 without dropping endpoints/candidate centers. Literal whole-second times at rational rates such as 29.97 are generally off-grid and must never be sent directly to P5-04, which requires on-grid requests; P5-04 separately maps that common-clock time through the original stream's presentation origin to a decoded frame index.

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

1. Write failing tests for 6/10/11/12/17/20/22-second runs at all eight supported frame rates, balanced/greedy cases, 30 vs 29.97, nonzero asset and stream PTS origins, retained XML bounds that differ from probed frame-count bounds, asset-last blocks whose end equals the retained source-grid end, first/last=`end-1` endpoints, rational 1-fps/center snapping and `source_time=index*frame_duration`, guard holes, clip boundaries, locks, analysis error, candidate/sample dedup and cap. Add declared-no-audio and both retryable pre-PCM reasons, exact typed carry-through, missing-lock/duplicate/unknown identity rejection, authored-audible pre-PCM failure, and equal-content healthy/unavailable resources proving only the unavailable instance yields zero blocks.
2. Implement frame-only partition/candidate/sample logic; no float/model access.
3. Run focused P6/P5/P1 tests, invariants, full check; commit owned files.

## Acceptance

Blocks cover only eligible frames, do not overlap, and leave short remainders explicitly kept. Candidate length is nearest 3 seconds and no clip can be fully deleted. Sample count is <=16 with mandatory samples present. Every unavailable instance is explicitly carried, locked, and absent from blocks without suppressing healthy equal-content instances. `make check` passes.

## Failure route

Report fps, run/guard frames, partition/candidate/sample frames, expected/actual, base/tests/diff. Never hide a short remainder or loosen the guard. Repeat failure follows diagnosis escalation.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, partition examples, and `ready-to-integrate`.
