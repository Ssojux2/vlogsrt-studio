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

def partition_blocks(document: ProjectDocument, media: Sequence[ResolvedMedia], guards: Sequence[InstanceGuard], locked_instance_ids: AbstractSet[str], cfg: ProjectConfig) -> BlockPartition: ...
```

- Eligible runs are complements of the instance guard inside each original audible clip, only when all required analysis succeeded and run length is at least frame-ceiled 6 seconds. Never cross a clip/gap/instance boundary.
- Locked/broken/uncertain/muted/disabled instances yield no edit blocks.
- For run length `L`, try `n=ceil(L/max_frames)` balanced integer-frame blocks whose sizes differ by <=1 frame. Use them only if every block >= min frames; otherwise take greedy max-frame blocks and keep the short remainder unchanged.
- `min_frames=ceil(6s/frame)`, `max_frames=floor(10s/frame)`, `keep_frames=nearest(3s/frame)`, all from config/timebase.
- Each block has beginning/middle/end keep candidates of exactly `keep_frames` when possible, deduplicated by exact frame interval; never a full-delete candidate.
- Sample requests are 1 fps plus block endpoints and three candidate centers, unique frame IDs, deterministically reduced to <=16 without dropping endpoints/candidate centers.

## Implementation steps

1. Write failing tests for 6/10/11/12/17/20/22-second runs at all fps, balanced/greedy cases, 30 vs 29.97, guard holes, clip boundaries, locks, analysis error, candidate/sample dedup and cap.
2. Implement frame-only partition/candidate/sample logic; no float/model access.
3. Run focused P6/P5/P1 tests, invariants, full check; commit owned files.

## Acceptance

Blocks cover only eligible frames, do not overlap, and leave short remainders explicitly kept. Candidate length is nearest 3 seconds and no clip can be fully deleted. Sample count is <=16 with mandatory samples present. `make check` passes.

## Failure route

Report fps, run/guard frames, partition/candidate/sample frames, expected/actual, base/tests/diff. Never hide a short remainder or loosen the guard. Repeat failure follows diagnosis escalation.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, partition examples, and `ready-to-integrate`.
