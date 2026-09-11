# Task P6-04: publish the original-linked FCPXML and SRT package atomically

## Authority

Binding spec `Input and output`, P6 export, and `Evidence at handoff`; depends on P6-03, P1 roundtrip/validation, and P3 SOURCE SRT. Opus phase review precedes Astra's P6 merge.

## Baseline and ownership

- Worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p6-export`, branch
  `Ssojux2/fcp-p6-export`, from exact base `924b5d1` (P0~P5 + P6-01·02·03 통합됨).
  rebase·reset·squash 금지.
- 허용 파일: `vlogsrt/project/export.py`(신규), `tests/project/test_export.py`(신규),
  `vlogsrt/project/fcpxml.py`(EDIT 렌더에 필요한 만큼만),
  `scripts/check_invariants.py`(20번째 검사를 `export.py`까지 넓힌다).
- 금지: EditPlan·EditCaptionSet·SOURCE 변형, 모델·네트워크 호출, 설정 변경,
  `tests/fixtures/api/**`, `golden/`, `.env`, `.env.example`, 그 밖의 모든 파일.
- 새 의존성 금지. `xmllint` subprocess는 **기존 XML 검증 경계에서만** 허용된다
  (`vlogsrt/project/xml_validation.py`) — `export.py`에서 직접 부르지 마라.
- `.venv` 심링크를 만들지 마라. 인터프리터는
  `/Users/ssojux2/Working/vlogsrt/.venv/bin/python` 절대 경로를 써라.
- **P6의 마지막 태스크다.** 끝나면 FCPXML·SOURCE SRT·EDIT SRT·review·manifest가
  한 revision으로 함께 나와야 한다.

## Contract

```python
@dataclass(frozen=True, slots=True)
class ExportPackage:
    plan_revision: str
    fcpxml: Path
    edit_srt: Path
    source_srt: Path
    project_trace: Path
    review: Path

def render_edit_fcpxml(document: ProjectDocument, media: Sequence[ResolvedMedia], plan: EditPlan, captions: EditCaptionSet) -> bytes: ...
def publish_package(destination: Path, document: ProjectDocument, media: Sequence[ResolvedMedia], plan: EditPlan, edit_captions: EditCaptionSet, source_srt: str, review: Mapping[str, object], *, dtd_path: Path | None = None) -> ExportPackage: ...
```

- Output names exactly `VLOG_AI_EDIT.fcpxml`, `VLOG_AI_EDIT.srt`, `VLOG_AI_SOURCE.srt`, `project_trace.json`, `review.json`; every structured artifact carries the same plan revision. `VLOG_AI_SOURCE.srt` is `BoundaryProjection.source_srt`, the input project timeline before automatic edit removal, not a concatenation of per-asset zero-based files.
- Create new project/event identity, `tcStart=0`, original-linked media refs, source order, supported static attributes/resources/UID, existing gaps, and disabled durations. XML-local resource IDs may be preserved or deterministically regenerated only as references inside the new document; never treat input `r2`-style IDs as durable media identity or cross-document rebind keys. Never link output to proxy as original.
- Split/ripple clips only at exact plan frame boundaries; preserve asset-start anchored source coordinates and valid refs/order/non-overlap/durations.
- Add `<marker>` children only for generated, explicitly located `ReviewIssue` values; never parse human prose for coordinates. Create markers **after** splitting clips and select exactly one final owner with the same `instance_id` whose retained `source_frames` contain the same asset-start-anchored `source_frame` under half-open `[start, end)` semantics (a cut-boundary frame belongs to the later slice). The marker XML clock is `asset.start + source_frame * frame_duration`, not elapsed time since the trimmed/split clip starts; never subtract the final clip start to make a zero-relative marker timestamp, and require that XML coordinate to lie inside the final owner clip's half-open `[start, start + duration)`. Prefix value with `VLOG_AI_REVIEW:`, keep duration exactly one source frame and wholly within the owner, and insert it at the exact FCPXML 1.14 DTD child position. If the issue's source frame was deleted, emit no relocated marker; preserve the issue with its original provenance in `review.json`.
- Clock evidence: Apple's official [marker association example](https://developer.apple.com/documentation/professional-video-applications/associating-ratings-keywords-markers-and-metadata-with-media) places markers at source coordinates 6s/8s inside a clip whose `start` is 5s, and states that marker duration is one video frame. The installed FCPXML 1.14 DTD entries at lines 516 and 686–691 fix marker child order and require `start`/`value`; tests must exercise those exact local rules.
- Before publication, always run the existing supported-structure parser and P1-04 `validate_xml` on the generated FCPXML, passing `dtd_path` through when supplied. Invalid XML returns/fails publication with its bounded diagnostic; missing xmllint or requested DTD raises `XmlValidationUnavailable` and cannot publish a successful package.
- Write all five files into a new immutable revision directory, fsync/close, and validate its hashes/revisions before publication. Publish by one atomic replacement of a `current` symlink/pointer to that complete revision; returned `ExportPackage` paths resolve inside the selected immutable revision so one consumer cannot observe a mixed set while the pointer changes.
- First publish creates the managed revision root and pointer. If `destination` is an existing unmanaged file/directory or has an invalid pointer/layout, fail without renaming, deleting, or overwriting user content. Concurrent writers use exclusive revision creation plus an atomic exclusive publication lock covering the final pointer read/check/switch; a conflicting writer fails deterministically without touching `current`. Remove only a lock token owned by the current call. An existing/stale-looking lock is a conflict requiring explicit recovery — never steal it solely by age and never describe independent read plus replace as compare-and-swap. Never claim that replacing one nonempty directory with another is atomic.
- Draft unresolved boundary/analysis warnings, including unlocated `subms_collapsed` caption issues with exact cue IDs/endpoints, stay explicit and prevent a `final_accepted=true` field; unlocated caption issues never produce guessed XML markers.
- Serialize every `plan.unavailable_instances` record into both `project_trace.json` and `review.json` with exact resource/instance/reason identity, after checking the supplied review data does not omit or contradict it. Declared-no-audio is a completed keep/review disposition and does not alone prevent `final_accepted=true`; `stream_unavailable` and `pcm_unavailable` are retryable failures, preserve the original instance unchanged, and require `final_accepted` to be absent or false. No unavailable record may be converted into a marker coordinate, successful analysis, or silent deletion.

### 배포된 P6-02 타입과 신뢰 경계 (Fable 검토 — 이 절이 기존 시그니처를 대체한다)

브리프의 옛 시그니처(`source_srt: str`, `review: Mapping`)는 **폐기됐다.** 정본은 이것이다:

```python
def render_edit_fcpxml(document, media, plan: EditPlan, captions: EditCaptionSet,
                       guards: Sequence[InstanceGuard]) -> str: ...
def publish_package(destination, document, media, plan: EditPlan,
                    edit_captions: EditCaptionSet, source: BoundaryProjection,
                    guards: Sequence[InstanceGuard], *, dtd_path: Path | None = None) -> ...: ...
```

- **`source_srt: str`를 받지 마라.** 맨 문자열은 같은 `input_sha256`의 것인지 증명할 수 없다.
  `VLOG_AI_SOURCE.srt`는 `source.source_srt`에서 나오고
  `source.input_sha256 == plan.input_sha256 == document.input_sha256`을 요구한다.
- **`review` 매핑을 받지 마라.** `review.json`과 `project_trace.json`은 **여기서** 짓는다:
  `plan.review`, `edit_captions.review`, `source.unresolved_request_ids`/`unresolved_cue_ids`,
  `plan.unavailable_instances`에서. **`final_accepted`는 여기서만 계산한다** —
  호출자가 주거나 덮어쓸 수 없다(외부 매핑은 기껏해야 `notes: tuple[str, ...]`이다).
- **`render_edit_fcpxml`은 `validate_edit_plan`을 먼저 부르고 finding이 하나라도 있으면
  렌더를 거부한다.** 그래서 `guards`가 양쪽 시그니처에 필요하다.

**스토리라인은 조회만 한다 — 리플 산수를 다시 하지 마라.**
`asset-clip`은 **`plan.runs`마다 하나**(`retained`마다가 아니다 — `retained`는 자르지 않은
블록 경계마다 쪼개져서 through-edit이 생긴다). 갭은 `plan.gaps`에서 그대로,
offset은 `m × frame_duration`, `tcStart=0`. 마커 소유자는 **내보낸 run**이고 반개 구간이다 — `retained`가 아니다(조각나 있다).

**레코드 인코더를 여기서 한 쌍만 공개하라 — 인코딩을 둘로 만들 마지막 기회다.**
`EditPlan`·`EditCaptionSet`·`ProjectCue`·`ReviewIssue`가 전부 `Fraction`과 `FrameInterval`을
싣는데 패키지 안에 JSON writer가 설정 해시와 경계 기록뿐이다. P6-04가 사적으로 인코딩하면
**P7-01이 디코딩을 다시 발명한다.** 공개 쌍을 내라:
`plan_record(plan) -> dict`, `edit_captions_record(captions) -> dict`.
`Fraction`은 `"num/den"`, `FrameInterval`은 `[start, end]`. P7-01은 **이것으로만** 읽는다.

**빈 EDIT 이슈 목록을 "미해결 없음"으로 읽지 마라.** SOURCE에서 접힌 큐는 EDIT에서
표현 가능해져도 EDIT SRT에서 빠지고 EDIT 레벨 이슈를 남기지 않는다(P6-03b에서 확정).
그런 큐의 수락 차단은 `source.unresolved_cue_ids`에서만 온다 —
`final_accepted` 계산에 반드시 포함하라.

**신뢰 경계 — 프로세스 안에서 만든 플랜만 렌더한다.**
렌더하는 `EditPlan`은 **같은 호출 사슬에서 `build_edit_plan`이 만든 것**이어야 하고
`revision`을 대조한다. 저장된 플랜은 **기록이지 렌더의 입력이 아니다.**
재개는 저장된 분석에서 플랜을 **다시 만든다**(명세: plan/export는 저장된 분석에서 결정적).
`validate_edit_plan`은 **구조 관문**이라 출처를 볼 수 없다 — 출처는 이 규칙이 지킨다.

## Implementation steps

1. Write failing tests for original/proxy refs, new document identity/tcStart, reuse of raw IDs across unrelated inputs without cross-binding, repeated/disabled/gap/order, exact cuts, post-split marker ownership at both sides of a boundary, nonzero `asset.start` plus a trimmed final owner proving marker `start` stays in the asset-anchored source clock, deleted-location review without marker relocation, DTD child order, mixed revision, validation unavailable/invalid refusal, first publish, unmanaged destination refusal, failure before pointer switch, stale prior revision, live/stale-lock refusal, concurrent-writer conflict under the exclusive lock, reader consistency across a switch, and all five names. Add exact unavailable metadata in both JSON artifacts, omitted/contradictory review rejection, unchanged XML retention, no marker fabrication, declared-no-audio final readiness, and retryable stream/PCM failure refusal.
2. Implement tree-copy plan writer preserving untouched attributes/time strings and atomic package publication.
3. Reparse/validate generated XML, run all P6/P1 tests, invariants, full check; commit owned paths.

## Acceptance

Synthetic package reparses with zero dangling refs/overlap/zero-frame identity mismatch, uses originals, shares one revision, and actually shortens the safe fixture. Unavailable instances are unchanged and auditable in both structured records, with retryable reasons never final-accepted. Failure before switch leaves the prior pointer and complete revision intact; readers resolve all five returned paths from one revision, and an unmanaged destination is untouched. `make check` passes. Actual FCP import/relink/playback remains `PENDING_HUMAN`.

## Failure route

Report artifact/revision, XML element path/ref/time, validator diagnostic, staging state, base/tests/diff. Never publish a partial/mixed package or flatten unsupported input. Structural/source-loss errors escalate immediately.

## Report

Report Orca/launch IDs, branch/base/commit, files/tests, package hashes/revision/duration, DTD status, pending live gate, and `ready-to-integrate`.
