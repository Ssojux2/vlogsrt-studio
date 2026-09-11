# Task P6-03: project EDIT captions through the EditPlan

## Authority

Binding spec `Input and output`, clock/SRT rules, and P6; depends on P6-02 and P4 boundary projection. This task must keep SOURCE SRT immutable.

## Baseline and ownership

- Worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p6-captions`, branch
  `Ssojux2/fcp-p6-captions`, from the exact base named at dispatch. rebase·reset·squash 금지.
- 허용 파일: `vlogsrt/project/captions.py`, `tests/project/test_edit_captions.py`(신규).
- 금지: EditPlan 변형, XML·패키지 출력, 소스 트레이스·SOURCE SRT 변경, 네트워크,
  `tests/fixtures/api/**`, `golden/`, `.env`, `.env.example`, 그 밖의 모든 파일.
- 새 의존성·live 호출·`--online`·`VLOGSRT_OFFLINE=0` 금지.

## Contract

```python
@dataclass(frozen=True, slots=True)
class EditCaptionSet:
    plan_revision: str
    cues: tuple[ProjectCue, ...]
    srt_text: str
    review: tuple[ReviewIssue, ...]

def project_edit_captions(plan: EditPlan, source: BoundaryProjection) -> EditCaptionSet: ...
```

- Reject plan/source mixed revisions or a cue intersecting a removed interval; guard correctness means no new partial cue should need text synthesis/retranscription here.
- Map retained input timeline frames to ripple output frames exactly. EDIT SRT is elapsed zero-based and uses one half-up millisecond serialization at the end.
- Boundary/source text and provenance remain unchanged. Clip-end cue ends at the exact retained slice endpoint; do not invent a one-frame gap.
- Reuse the authoritative exact `SourceCue`/`ProjectCue` representation produced from the immutable refined cue plus its exact input-timeline transform. EDIT applies only the ripple-output transform; it never recomputes the same cue from raw segments, settled-spine floats, or another serialization path.
- Adjacent cue end/start may be equal; each cue must have positive exact duration and be ordered/non-overlapping.
- After the exact ripple transform and final clamp, require `half_up_ms(end) > half_up_ms(start)` before SRT emission. If a positive exact cue collapses at millisecond precision, retain the exact derived cue in `EditCaptionSet.cues`, append `subms_collapsed` to its flags, omit only it from `srt_text`, and append a deterministic unlocated `ReviewIssue(code="subms_collapsed", ...)` whose message names the cue ID and exact Fraction endpoints. Its `instance_id` and `source_frame` are both `None`, so P6-04 cannot invent a marker location; this unresolved issue blocks `final_accepted=true`. Preserve any upstream `subms_collapsed` cue/unresolved-ID evidence without duplication. The direct serializer still rejects unrepresentable input; never stretch/merge endpoints, invent a frame gap, or silently discard a cue.
- SOURCE cue objects/text/SRT remain byte-identical before/after this function.
- Preserve the plan's structured `ReviewIssue` values byte-for-byte and in order; never parse `message` prose for an instance or timestamp. Caption-specific review evidence uses the same paired location rule (`instance_id` plus the exact asset-start-anchored `source_frame` used by retained intervals, or both `None`) so P6-04 receives explicit marker provenance.
- Require `plan.unavailable_instances == source.unavailable_instances` exactly before projection; a stale/missing/reordered disposition is mixed input and must fail rather than silently caption or edit an unavailable instance. Preserve the plan's typed records and their unlocated review semantics without inventing cues: declared-no-audio may complete, while retryable stream/PCM absence still blocks final acceptance.

### 배포된 P6-02 타입 — 이 모양을 그대로 소비하라 (Fable 검토)

**격자는 `plan.frame_duration`에서 온다.** `frame_duration` 매개변수는 계약에서 **삭제됐다** —
느슨하게 받으면 플랜과 다른 격자로 투영할 수 있고 그걸 막을 방법이 없다.

**큐 부착은 `RetainedInterval`이 아니라 `EditPlan.runs` 단위다.** 이게 핵심이다.
`retained`는 **제거가 없는 블록 경계마다 쪼개진다**(판정 없는 500프레임 클립이
`kept_block` 둘로 나온다). `RetainedInterval` 하나 안에 들어가야 한다고 요구하면
**자르지 않은 경계를 건너는 큐가 하드 실패**한다 — 실제로는 아무것도 잘리지 않았는데도.
`runs`는 소스와 출력이 **둘 다 연속**인 구간을 합친 정본이다.

- **단일 인스턴스 큐**: 그 인스턴스의 **run 하나** 안에 있어야 한다.
  이동량은 `run.output.start − run.input.start`.
  그 밖은 전부 `cue ∩ removed` 하드 실패다.
- **다중 인스턴스(경계) 큐**: 모든 인스턴스가 잠겨 있으므로 인스턴스마다 whole-clip run이
  하나씩이고, 그것들이 출력에서 **연속이며 같은 이동량**이어야 하고 큐가 그 합집합 안에
  있어야 한다. 아니면 실패다.

**EDIT 신원은 `(cue_id, instance_ids)`다. `cue_id` 단독으로 dedupe하지 마라.**
내부 칼날을 걸친 소스 큐는 **같은 `cue_id`의 `ProjectCue`가 인스턴스마다 하나씩 둘**이고,
여러 칼날을 덮는 경계 큐는 `instance_ids`를 다 실은 **하나**다(P4-04의 비대칭).
`cue_id`로 합치면 전자가 한 장으로 뭉개진다. 밀리초 접힘 메시지는 **둘 다 이름을 댄다.**

**신원 대조를 셋으로 늘려라.** 이미 있는 `unavailable_instances` 동등성 옆에:
- `plan.locked_instance_ids == source.locked_instance_ids`
- `plan.input_sha256 == source.input_sha256`

## Implementation steps

0. **먼저 실패 테스트로 이 넷을 박아라**: (a) **자르지 않은 블록 경계를 건너는 큐가 살아남는지**
   — `retained` 기준으로 짜면 여기서 실패한다; (b) **제거를 건너는 큐가 하드 실패하는지**;
   (c) 내부 칼날을 걸친 소스 큐가 앞쪽 인스턴스의 제거 뒤 **두 장으로 남고 리플 뒤 인접**한지;
   (d) 잠긴 두 칼날을 덮는 경계 큐가 한 장으로 남는지.
1. Write failing tests for ripple across multiple blocks/gaps, exact clip end, adjacent boundaries, removed-cue rejection, repeated instances, boundary provenance, mixed revision, structured review preservation, and source immutability. Include `Fraction(133, 8000)` to `+ Fraction(1, 24000)`, whose endpoints both half-up round to 17 ms, plus a collapse introduced only after clamp/ripple; assert retained flagged cue, exact-endpoint review, omitted SRT row, no marker coordinates, and final-acceptance refusal. Add exact unavailable-metadata equality, stale/missing/reordered rejection, zero-cue preservation, and declared-no-audio versus retryable readiness behavior.
2. Implement pure mapping/serialization via existing timebase/caption functions.
3. Run focused P6/P4/P3 tests, invariants, full check; commit owned files.

## Acceptance

EDIT cues match exact output frames and plan revision; every emitted SRT cue has strictly increasing millisecond endpoints, while any exact-positive millisecond collapse remains visible in structured cues/review and blocks final acceptance. There is no cue/removed overlap or cumulative ms drift; SOURCE output hash is unchanged. `make check` passes.

## Failure route

Report plan revision, cue/instance and exact frame/Fraction mapping, expected/actual SRT boundary, base/tests/diff. Never trim text or add a frame gap. Silent cue loss is a hard escalation.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, SOURCE before/after hash, EDIT cue/duration evidence, and `ready-to-integrate`.
