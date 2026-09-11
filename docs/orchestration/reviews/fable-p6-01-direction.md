# P6-01 direction review (Fable 5.1, read-only) — candidate be2c570 on base 371a020

방향 판정: SOUND_WITH_CONCERNS

## 1. import 허용목록 재결 검토: 타당. 실질 구멍 없음. 검사기에 별명 우회 1건. 더 나은 형태는 후속 태스크.

- The contradiction was mine: the pre-phase banned `vision.sampling` wholesale and required `EditBlock.sample_requests: tuple[FrameSampleRequest, ...]`. The ruling ("ban behaviour, not the data type; one form, one name") is the right resolution.
- It is not a hole in substance. `policy.py` (allowed) already does `from vlogsrt.project.vision.sampling import FrameSample`, so edit.py -> policy -> sampling -> preprocess was already the transitive graph. The single-name import adds no reach. The checker guards name bindings, the same standard as `check_repair_stays_upstream`; `sys.modules`-style evasion is outside any AST check in this repo.
- Verified evasion in the checker: `import vlogsrt.project.vision.sampling as FrameSampleRequest` passes (`_edit_imports` records the **asname** for `ast.Import` rows, so the row looks like the sanctioned form). `from vlogsrt.project.vision import sampling as FrameSampleRequest` is correctly rejected. Fix: record `alias.name` (module path) for `ast.Import` rows; the row then fails `names != ("FrameSampleRequest",)`. One line, one parametrize case.
- Better form (not blocking, outside P6-01's allowed files): move `FrameSampleRequest` and `FrameSample` to a behaviour-free module (e.g. `vlogsrt/project/vision/frames.py`); `sampling` and `policy` import from it; the sampling ban becomes absolute and the allowlist special case plus its ~70 test lines disappear. No import cycle (sampling does not import policy).

## 2. P6-02 공급 가능성: 예측한 모양 그대로 착지. 재구성 강요 없음. 브리프 쪽 빈칸 1건, 구현 결함 1건.

Landed as specified: `KeepCandidate(candidate_id, source_frames, timeline_frames, vision)` with `vision.keep_start = shift × fd` derived from frames (checked: 25fps default gives (0,3), (88/25,163/25), (7,10)); `BlockPartition.input_sha256 / config_sha256 / locked_instance_ids`; content-bound ids with the exact formula and NUL-joined domain-prefixed material; deterministic thinning stated.

P6-02 consumption by lookup only: `decision.block_id -> block`, `choice -> KeepCandidate.source_frames/timeline_frames`, never a division. Whole-clip retained tiling needs the clip's full grid; `_clip_grids` lives in the same module P6-02 edits, so that is not a seam reconstruction.

Not in the P6-02 brief but needed (the reason P5-06 item 1 was done): verifying `decision.request_sha256`. All material is on `EditBlock` (`block_id`, ordered `sample_requests[*].frame_id`, `keep_candidates[*].vision`), but the projection EditBlock -> VisionRequest must be defined once so the runner (P7) and the verifier (P6-02) hash identical material. Spec: `vision_request_for(block, adjacent_context) -> VisionRequest` in edit.py with `frame_ids = tuple(r.frame_id for r in block.sample_requests)` and `candidates = tuple(k.vision for k in block.keep_candidates)`; `policy` exposes `request_sha256(request)` publicly; P6-02 raises on mismatch (mixed-revision line, next to the partition/config hash checks).

Implementation defect (reproduced): with `keep_frames == 1` (`keep_seconds = 1/25` at 25fps passes every rail), the end candidate's centre sample is `start + (1+1)//2 = end_frame`. Samples emitted at index 250 for block [0,250) and 500 for [250,500). That violates the brief's "never end_frame" and P5-04 raises out-of-range when the block ends at the asset end. The brief says centres are clamped inside the block; only nominal samples are clamped. Fix: `min(centre, length - 1)`. frame_ids for keep_frames >= 2 are unchanged, so no downstream identity churn.

Minor, not blocking: `kept_short_remainders` carries source frames only (no timeline frames), asymmetric with every other interval. P6-02 does not need it (remainders are retained wholesale); P6-04 trace may.

## 3. P5-06 정합: 해시 재료 정합. 레일 중첩 정상. 교차 레일이 정책 천장을 안 본다.

- `_request_sha256` material = block_id, ordered frame_ids, per candidate (candidate_id, exact keep_start, exact keep_end). P6-01 supplies exactly those; windows are exact Fractions from frames; candidate order is fixed (begin/middle/end). Consistent, provided the projection above is canonical.
- Constructor vs local rails: layered, not weakened. The partition-level rail tests use configs the constructor accepts (frame-converted min > max at 5997/5999 ms, `vision_max_images=4`, `block_max_seconds=600`), so the local layer is exercised, not masked by `__post_init__`.
- Rail defect (reproduced): the cross rail compares against `cfg.vision_max_images`, but policy caps at `_effective_max_images(cfg) = min(cfg, 16)`. `ProjectConfig(block_max_seconds=20, vision_max_images=32)` passes the rail and yields a 20-second block with 15 samples, which is precisely the "sparse images prove nothing" case the rail exists for. Fix: compare against `_effective_max_images(cfg)` (already imported).

## 최소 수정 명세 (all inside P6-01's allowed files unless marked)

1. `edit.py::_sample_requests` — clamp candidate centres: `min(offset + (keep + 1) // 2, length - 1)`. Test: `keep_seconds` = one frame, every sample index `< end_frame`.
2. `edit.py::_require_config` — cross rail `cfg.block_max_seconds * cfg.vision_sample_fps > _effective_max_images(cfg)`. Test: `block_max_seconds=20, vision_max_images=32` raises.
3. `scripts/check_invariants.py::_edit_imports` — for `ast.Import` rows use `(alias.name,)`, not the asname. Test: `import vlogsrt.project.vision.sampling as FrameSampleRequest` is rejected.
4. P6-02 brief (docs) — add the `request_sha256` mismatch -> raise rule and the canonical `vision_request_for` projection; `policy.request_sha256` made public (P5-05 file, one rename).
5. Optional follow-up task — move the two frame dataclasses to a behaviour-free module and delete the sampling allowlist exception.

None of 1–3 changes ids under default config; all are one-line code changes plus one test each.
