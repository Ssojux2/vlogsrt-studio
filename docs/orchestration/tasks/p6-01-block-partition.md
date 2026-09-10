# Task P6-01: partition eligible unprotected runs into edit blocks

## Authority

Binding spec `Visual scoring and deterministic roughcut`; depends on integrated P4 and P5.
Pure geometry — it produces candidates/requests, not an edit decision and not a network call.
Coordinator/reviewer: Opus 5 (Run `run_a091d4869a98`). Offline only; Opus performs the merge.

**이 브리프는 Fable 5.1의 P6 사전 검토로 한 번 고쳐졌다** —
`docs/orchestration/reviews/fable-p6-prephase.md`. 아래 계약의 신원·레일 항목은 그 검토가
찾은 구멍을 막은 것이니 **임의로 단순화하지 마라.** 하류 세 태스크가 이 모양에 의존한다.

## Baseline and ownership

- Fresh `fcp-p6-blocks` worktree from the exact base named at dispatch (P0~P5 all integrated).
- Allowed: create `vlogsrt/project/edit.py` and `tests/project/test_block_partition.py`;
  add one import-direction check to `scripts/check_invariants.py` (see 규칙 4·9 below).
- Forbidden: XML/caption output, any model or network call, config changes, source mutation,
  `tests/fixtures/api/**`, `golden/`, `.env`, `.env.example`, every other file.
- No new dependency, no live call, no `--online`, no `VLOGSRT_OFFLINE=0`.
- P6는 이 프로젝트에서 **처음으로 구간을 실제 제거하는 계층**이다. CLAUDE.md 규칙 4와 9를
  먼저 읽어라.

## Contract

```python
@dataclass(frozen=True, slots=True)
class KeepCandidate:
    candidate_id: str
    source_frames: FrameInterval      # 정본이다
    timeline_frames: FrameInterval
    vision: VisionCandidate           # 모델에 보이는 블록 상대 초. 프레임에서 유도한다

@dataclass(frozen=True, slots=True)
class EditBlock:
    block_id: str
    instance_id: str
    source_id: str
    timeline_frames: FrameInterval
    source_frames: FrameInterval
    keep_candidates: tuple[KeepCandidate, ...]
    sample_requests: tuple[FrameSampleRequest, ...]

@dataclass(frozen=True, slots=True)
class BlockPartition:
    blocks: tuple[EditBlock, ...]
    kept_short_remainders: tuple[tuple[str, FrameInterval], ...]
    unavailable_instances: tuple[UnavailableInstance, ...]
    input_sha256: str                 # document.input_sha256
    config_sha256: str                # cfg.sha256()
    locked_instance_ids: frozenset[str]

def partition_blocks(document: ProjectDocument, media: Sequence[ResolvedMedia], guards: Sequence[InstanceGuard], locked_instance_ids: AbstractSet[str], unavailable_instances: Sequence[UnavailableInstance], cfg: ProjectConfig) -> BlockPartition: ...
```

### 신원과 레일 (Fable 사전 검토가 막은 구멍 — 없애지 마라)

**프레임이 정본이고 초는 파생이다.** `KeepCandidate.source_frames`가 정본이고
`vision.keep_start = (source_frames.start_frame − block.source_frames.start_frame) × frame_duration`
로 **프레임에서 초를 만든다.** 반대 방향으로 나누지 마라 — P6-02가 초를 프레임으로 되돌리게
만들면 그게 P4-04에서 무너진 바로 그 모양이다(생산자가 `FrameInterval`을 쥐고 있다가
이음매에서 버리는 것).

**신원은 내용에 묶는다.** 서수 id(`block-3`)를 쓰지 마라 — 다른 `keep_seconds`, 다른 문서
리비전, 다른 guard 세대에서 만들어진 판정이 "모르는 id" 검사를 통과해 **엉뚱한 프레임을
제거**한다. 공식을 브리프대로 박고 docstring에 적어라:

```
block_id     = sha256(domain, document.input_sha256, instance_id, source_id,
                      source_frames, timeline_frames)
candidate_id = sha256(domain, block_id, candidate.source_frames)
frame_id     = f"{source_id}:{grid_index}"  (또는 그 해시)
```

**`_require_config(cfg)`가 `partition_blocks`의 첫 관문이다.** `ProjectConfig`의 범위 검사는
`load_project_config`에서만 돈다 — 프로그램식 생성은 건너뛴다. 이 프로젝트가 **같은 우회를
세 번** 만났다(P5-05b·P5-03b·여기). 설정 결함은 **호출자 잘못이므로 `raise`**이고
keep+review로 세탁하지 않는다.

| 설정 | 막지 않으면 |
|---|---|
| `keep_seconds = 0` | 후보가 비어 **블록 전체가 제거**된다. 클립이 한 블록이면 클립이 통째로 사라진다 |
| `keep_seconds > block_min_seconds` | 후보가 블록보다 길어진다 |
| `block_min_seconds = 1/1000` | 1프레임 런이 블록이 된다 |
| `block_max_seconds = 600` | 10분을 이미지 16장으로 판정해 597초를 제거한다 |
| `vision_max_images < 5` | 끝점 2 + 중앙 3을 담을 수 없다 |
| `vision_sample_fps = 0` | 0으로 나누거나 표본이 0개가 된다 |

레일: `block_min_seconds`·`block_max_seconds`·`keep_seconds`·`vision_sample_fps`는
`bool`이 아닌 `Fraction > 0`, `vision_max_images`는 `bool`이 아닌 `int >= 5`,
**프레임 환산 뒤** `1 <= keep_frames <= min_frames <= max_frames`.
`vision_apply_confidence`는 `policy._require_config`를 재사용한다.
교차 레일 `block_max_seconds × vision_sample_fps <= vision_max_images`도 넣어라
(기본값 10 ≤ 16). 명세가 관계를 직접 말하지는 않지만 "성긴 이미지는 아무것도 증명하지
못한다"는 명세 문장의 기계적 표현이다.

**guard 연결은 엄격하다.** 문서의 클립 인스턴스마다 `InstanceGuard`가 **정확히 하나**여야
하고 `instance_id`와 `source_id == media.asset_content_id` **둘 다** 맞아야 한다.
없음·중복·남음·불일치는 전부 연결 오류로 **`raise`**다. 그러지 않으면 "guard 없음"이
조용히 "보호 없음"이 된다 — `protected=[]`가 옷만 갈아입은 것이다.
**`protected_frames`를 클립 자신의 소스 격자 구간과 교집합한 뒤** 여집합을 취하라
(합쳐진 슬라이스는 슬라이스 **전체**의 프레임을 싣는다).

**가청은 문서에서 계산한다.** P4-01과 정확히 같게
`media.has_audio and clip.enabled and clip.src_enable == "all"`이다.
**불가청 인스턴스에 `status="success"` guard가 붙어 있으면 연결 오류로 `raise`**이지
적격 판정이 아니다.

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

### 규칙 4·9 — 제거 계층에서 어떻게 지키는가

P6-01은 오디오·전사·VAD·`review` 산문을 **보지 않는다.** 타입 있는 guard의 여집합만
취한다. 그래서 규칙 4("이 구간은 말이 아니다라고 판단하는 코드를 쓰지 마라")의 판단이
상류의 보수적 "보호"로 남고 이 계층은 기하만 한다. **그 성질을 깨지 마라** —
`edit.py`에서 오디오를 열거나 전사 결과를 읽으면 그 순간 규칙 4 위반이다.

기계 검사를 `scripts/check_invariants.py`에 추가하라: `vlogsrt/project/edit.py`는
`subprocess`, `openai`, `numpy`, `onnxruntime`, `vlogsrt.transcribe`, `vlogsrt.preprocess`,
`vlogsrt.project.pcm`, `vlogsrt.project.speech`, `vlogsrt.project.vad.inference`,
`vlogsrt.project.vad.runtime`, `vlogsrt.project.vision.sampling`, `vlogsrt.repetition`,
`vlogsrt.repair`, `vlogsrt.refine`, `vlogsrt.output`, `vlogsrt.layout`을 **import하지 못한다.**
기존 검사들과 같은 방식으로 쓰고 `[OK]` 한 줄을 더한다(그러면 20개가 된다).

### 축소 규칙은 결정적이어야 한다

`<=16`으로 줄이는 규칙과 중앙 후보 중심의 반올림을 **브리프대로 명시하고 docstring에
적어라.** 같은 입력이 같은 표본 집합을 내야 한다 — P7이 분석을 재사용하므로
"대충 16개까지"는 리비전을 깨뜨린다. 필수 표본(블록 양 끝점, 후보 중앙 셋)은
**절대 떨어뜨리지 않는다**; 줄일 것은 1fps 표본뿐이고, 줄이는 순서도 결정적이어야 한다.

## Implementation steps

0. **레일과 신원 회귀를 먼저 쓴다**: 위 표의 각 설정값이 `raise`하는 것,
   guard 부재·중복·`source_id` 불일치가 `raise`하는 것, 음소거·영상 전용 클립에 붙은
   `success` guard가 `raise`하는 것, 합쳐진 두 칼날 슬라이스의 whole-slice
   `protected_frames`가 클립 구간과 교집합된 뒤 여집합이 되는 것,
   **같은 입력이 두 번 같은 id를 내고 `keep_seconds`가 다르면 id가 달라지는 것**,
   그리고 **정상 설정이 정상 블록을 낸다**는 것(이게 없으면 "전부 막혔다"가
   "모듈이 망가졌다"와 구별되지 않는다).
1. Write failing tests for 6/10/11/12/17/20/22-second runs at all eight supported frame rates, balanced/greedy cases, 30 vs 29.97, nonzero asset and stream PTS origins, retained XML bounds that differ from probed frame-count bounds, asset-last blocks whose end equals the retained source-grid end, first/last=`end-1` endpoints, rational 1-fps/center snapping and `source_time=index*frame_duration`, guard holes, clip boundaries, locks, analysis error, candidate/sample dedup and cap. Add declared-no-audio and both retryable pre-PCM reasons, exact typed carry-through, missing-lock/duplicate/unknown identity rejection, authored-audible pre-PCM failure, and equal-content healthy/unavailable resources proving only the unavailable instance yields zero blocks.
2. Implement frame-only partition/candidate/sample logic; no float/model access.
3. Run focused P6/P5/P1 tests, invariants, full check; commit owned files.

## Acceptance

Blocks cover only eligible frames, do not overlap, and leave short remainders explicitly kept. Candidate length is nearest 3 seconds and no clip can be fully deleted. Sample count is <=16 with mandatory samples present. Every unavailable instance is explicitly carried, locked, and absent from blocks without suppressing healthy equal-content instances. `make check` passes.

## Failure route

Report fps, run/guard frames, partition/candidate/sample frames, expected/actual, base/tests/diff. Never hide a short remainder or loosen the guard. Repeat failure follows diagnosis escalation.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, partition examples, and `ready-to-integrate`.
