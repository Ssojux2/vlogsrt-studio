# P7-01~03 브리프 1차 선검토 — shipped dataclass/signature audit

검토일: 2026-09-12  
범위: P7-01·P7-02·P7-03 브리프, 현재 통합된 `vlogsrt/project/` 타입, P6-04 후보
`defe23f`의 공개 기록 인코더. 제품 코드는 바꾸지 않았다. P6-04의 GLM/Fable 보고는 이
Dispatch에 아직 오지 않았으므로 P6-04 종합·보정 브리프·ledger는 하지 않았다.

## 판정

**브리프를 그대로 디스패치하면 안 된다.** P7-01에는 저장 뿌리·레코드 신원·typed decode를
나르는 시그니처가 없고, P7-02에는 완료된 vision 요청과 request-level 취소/체크포인트를
나르는 길이 없다. P7-03의 CLI 명령 모양은 대체로 충분하지만 UI bridge가 취소와 출력
파일 종류, 이벤트 스키마를 나르지 않는다.

이것들은 구현자 결함이 아니다. 아직 실행하지 않은 P7 브리프의 계약 구멍이다.

## 빠른 결과표

| 브리프 | 시그니처 부족 | 레일 공백 | 지금 고칠 우선순위 |
|---|---|---|---|
| P7-01 | workspace root 없는 writer, kind/revision 없는 writer, typed reader/decoder 없음, manifest가 재개 재료를 다 못 실음, 기대 config를 받지 않음 | `completed`가 write 완결과 재사용 가능을 섞음 | 디스패치 전 차단 |
| P7-02 | estimate에 완료 `VisionDecision`/request state 없음, runner에 두 config 세대가 없음, upstream request checkpoint/cancel seam 없음 | 평평한 completed ID가 retryable error를 성공으로 만들 수 있음 | 디스패치 전 차단 |
| P7-03 | UI bridge에 cancellation 없음, `output_dir` 하나로 export 디렉터리와 captions 파일을 겸함, event mapping 스키마 없음 | UI가 산문/임의 mapping에서 상태를 재구성하게 됨 | P7-03 디스패치 전 보정 |

## P7-01 — workspace 계약

### 1. `write_json_atomic(path, value)`는 자기 workspace를 알 수 없다

브리프는 경로를 workspace-relative로 기록하고 traversal/symlink escape를 거부하라고
요구하지만, writer는 `path` 하나만 받는다. 이 시그니처로는 `/tmp/other/result.json`이
의도한 workspace 밖인지 정상 뿌리인지 판정할 근거가 없다. `ArtifactRef`에 필요한
`kind`와 `revision`도 인자로 받지 않는다. 더구나 P6-04의 `plan_record`에는
`revision`은 있지만 `kind`가 없고, `edit_captions_record`에는 `plan_revision`만 있다.
파일명이나 레코드 키로 유도하면 P4-04 모양이다.

최소 보정:

```python
def write_json_atomic(
    workspace: Path,
    relative_path: Path,
    value: Mapping[str, object],
    *,
    kind: ArtifactKind,
    revision: str,
    schema_version: int,
) -> ArtifactRef: ...
```

테스트는 같은 `relative_path`에 같은 바이트면 idempotent, 다른 바이트면 기존 파일 보존,
`..`·절대 경로·중간 symlink 교체를 모두 거부해야 한다.

### 2. 요구된 decode/reopen 동작에 공개 읽기 면이 없다

`open_workspace`는 `WorkspaceManifest`의 `ArtifactRef`만 돌려주고, `require_revision`도 ref만
고른다. 그런데 같은 브리프는 `SourceAnalysis` discriminator/schema 검증, `EditPlan`과
`EditCaptionSet` 복원, P4/P6 `unavailable_instances`의 typed agreement를 요구한다. 실제
`EditPlan`은 retained/runs/gaps/removed/decisions/unavailable/review를, `EditCaptionSet`은
`ProjectCue`와 `ReviewIssue`를 싣는다. ref만 받은 P7-02가 파일을 직접 열어 이 타입들을
재조립하면 소비자가 저장 형식을 발명하게 된다.

또한 manifest의 세 artifact 묶음(`source_analysis`, `vision_analysis`, `plans`)에는
`PcmReference`, `BoundaryRequest`/`BoundaryTranscript`/`BoundaryProjection`, VAD/guard,
frame sample, edit captions, package, checkpoint가 들어갈 명시적 자리가 없다. 이들은 P7-02
resume와 P7-04 reproject가 실제로 다시 읽어야 하는 재료다. 디렉터리를 훑어 최신 것을
고르는 우회는 브리프 자신이 금지한다.

최소 보정은 다음 둘 중 하나를 명시적으로 고르는 것이다.

1. manifest에 모든 durable 종류를 담는 generic `artifacts: tuple[ArtifactRef, ...]`와
   닫힌 `ArtifactKind`를 두고, verified JSON reader 및 타입별 decoder를 공개한다.
2. 필요한 종류를 explicit field로 모두 열거하고, 같은 reader/decoder를 공개한다.

어느 쪽이든 `decode_source_analysis`, `decode_edit_plan`, `decode_edit_captions`와
projection/request/checkpoint decoder의 소유자를 P7-01로 못 박아야 한다. 알 수 없는 키,
variant, schema version, 비정규 Fraction/interval은 client 생성 전에 거부한다.

### 3. `completed`는 `protected=[]` 계보와 같은 상태 반전 구멍이다

JSON 파일이 원자적으로 끝까지 써졌다는 뜻과 분석이 재사용 가능한 성공이라는 뜻은 다르다.
`UnavailableSourceAudio(status="error")`와 `SourceTrace(status="error")`도 오류 증거로는
완전하게 써져야 하지만, 재개에서는 pending/retryable이다. `ArtifactRef.completed: bool` 하나와
항상 완성 파일을 내는 `write_json_atomic`을 결합하면 “완전히 기록된 실패”가 “완료된 분석”으로
읽힐 수 있다. P7-01/P7-02 본문은 이를 금지하지만 타입이 반대를 허용한다.

최소 보정: byte publication 상태(`committed`)와 pipeline disposition
(`reusable_success | reusable_no_audio | retryable_error | terminal_error | draft`)을 분리한다.
적어도 `completed`를 caller가 임의로 쓰거나 writer가 무조건 `True`로 만들지 못하게 하고,
typed payload에서 재사용 상태를 검증해 만든다. retryable error JSON roundtrip 뒤
`require_revision`/resume가 그것을 pending으로 돌리는 테스트가 필요하다.

### 4. 하나의 `config_sha256`로 실제 두 설정 세대를 표현하지 못한다

현재 소스·경계 전사는 legacy `vlogsrt.config.Config`를 받고 `SourceTrace.config_sha256`에 그
지문을 남긴다. VAD·vision·edit와 가격은 `vlogsrt.project.config.ProjectConfig`를 받고
`EditPlan.config_sha256`은 그 지문을 남긴다. 그런데 `WorkspaceManifest`에는
`config_sha256` 하나뿐이고 `open_workspace(path, input_sha256)`는 기대 config를 받지도 않는다.
따라서 “stale config를 open에서 거부”하거나 같은 PCM 위 두 SOURCE config 세대를 정확히
선택할 입력이 없다.

최소 보정: source config와 project config의 정규 snapshot/ref 및 각 SHA를 따로 싣거나,
둘을 명시적으로 결합한 workspace revision을 정의한다. `open_workspace`/typed load는 현재
기대 SHA들을 받아 대조해야 한다. CWD의 `vlogsrt.toml`을 다시 찾아 복원하는 것은 허용하지
않는다고 적는다.

### P6-04 `plan_record` / `edit_captions_record` 충분성

**현재 dataclass의 값을 잃지 않고 옮기는 데는 충분하다.** 후보 `defe23f`의
`plan_record`는 `EditPlan`의 모든 필드를 싣고, `edit_captions_record`는
`EditCaptionSet`의 `plan_revision/cues/srt_text/review`와 각 `ProjectCue` 필드를 싣는다.
Fraction은 `num/den`, frame interval은 `[start, end]`여서 정확성도 보존된다.

**안전한 재개 계약으로는 충분하지 않다.** 두 record 자체에는 kind/schema discriminator가
없고 역함수가 없다. kind와 바깥 input/config hash는 P6-04의 `project_trace.json` envelope에만
있다. 따라서 P7-01이 개별 record를 저장할 때 schema를 외부에서 붙이고, 유일한 public
decoder로 strict하게 복원해야 한다. `EditCaptionSet` 단독에는 input/config hash가 없으므로
반드시 같은 revision의 decoded `EditPlan`과 묶어서만 돌려준다.

## P7-02 — budget/cancel/resume runner

### 1. `estimate_cost`는 남은 vision work를 계산할 수 없다

시그니처는 `vision_requests`는 받지만 완료된 `VisionDecision`이나 checkpoint/request state는
받지 않는다. shipped `VisionDecision.request_sha256`와 공개 `request_sha256(VisionRequest)`가
정확한 재사용 대조 수단인데, 둘을 함께 전달할 길이 없다. `analyses`는
`SourceTrace | UnavailableSourceAudio`일 뿐 vision 결과가 아니다. 그래서 cold image 수는
셀 수 있어도 warm/partial에서 완료 이미지를 빼는 것은 불가능하다.

최소 보정: `decisions: Sequence[VisionDecision]` 또는 더 일반적인 typed completed-request
records를 `estimate_cost`에 넣고, `request_sha256(request)`가 같은 non-error 완료만 0 remaining
work로 친다. stale/error/중복 decision은 0으로 만들지 않는 테스트를 추가한다.

### 2. request-level 취소와 체크포인트는 P7-02 소유 파일만으로 구현할 수 없다

shipped `transcribe_source`와 `transcribe_boundaries`는 동기 함수이고 cancellation/progress
callback을 받지 않는다. 내부에서 첫 전사 뒤 repair/arbiter 요청을 더 스케줄한다. 더구나
`transcribe_source`는 모든 `Exception`을 잡아 error `SourceTrace`로 바꾸고, boundary도 같은
모양으로 error record를 돌려준다. runner가 client wrapper에서 cancellation 예외를 던져도
`cancelled` checkpoint로 올라오지 않고 분석 오류로 흡수된다. 성공한 개별 응답을 durable
checkpoint하는 hook도 없다.

따라서 “in-flight 결과는 checkpoint하고 이후 요청은 스케줄하지 않음”과 “resume는 pending만”을
runner.py 하나로는 증명할 수 없다. P7-02 ownership을 좁게 upstream seam까지 넓혀
source/boundary 실행기에 `should_cancel`과 `on_request_complete`를 주입하거나, 취소 보장을
source/boundary 단위로 낮춰야 한다. 후자는 현재 브리프의 request-level acceptance와 다르다.

### 3. checkpoint가 요청 종류·상태를 잃는다

`completed_request_ids`/`pending_request_ids`는 평평한 문자열 tuple이다. source analysis key,
boundary request/cache key, vision request hash, local preprocessing 작업 중 무엇인지, 완료가
success/no_audio/error 중 무엇인지 싣지 않는다. P7-01 manifest에도 checkpoint 자리가 없다.
이 모양에서는 retryable pre-PCM error를 completed 집합에 넣는 순간 다시는 재시도하지 않는
상태 반전이 생긴다.

최소 보정: `(kind, request_id, status, retryable, artifact_revision)`을 싣는 typed request
record를 manifest와 checkpoint가 공유하고, checkpoint 자체를 atomically persist하는 경로를
P7-01 계약에 추가한다. `complete`는 pending 0뿐 아니라 typed retryable error 0도 요구한다.

### 4. 실제 분석 설정 입력이 runner 경계에 없다

`run_stage`는 workspace와 opaque clients만 받고, P7-01 manifest는 config body를 싣지 않는다.
하지만 source와 boundary 함수는 legacy `Config`, 나머지는 `ProjectConfig`가 필요하다. 현재
시그니처는 caller가 CWD/env에서 다시 읽거나 기본값으로 재구성하도록 몰며, 그 경우 저장된
analysis/cache key와 다른 세대가 된다. P7-01의 두-config 보정과 함께 `run_stage`가 verified
typed snapshots만 받도록 명시해야 한다.

### 확인되어 충분한 부분

- `PcmReference.sample_count/sample_rate`는 full-source seconds를 정확히 잴 수 있다.
- `SourceDurationEstimate(resource_scoped_id, source_id, seconds, provenance)`의 `source_id`는
  실제로 원본 SHA에서 파생된 `asset_content_id`이므로 두-key join이면 strong identity가 된다.
- `BoundaryRequest.sample_range`는 matching `PcmReference.sample_rate`와 함께 exact boundary
  seconds를 낸다.
- `VisionRequest.frame_ids`와 공개 request hash는 완료 결과가 추가로 전달되기만 하면 exact
  image/request dedup에 충분하다.

## P7-03 — CLI / UI bridge

### 1. bridge가 cancellation을 나르지 않는다

P7-02는 `CancellationToken`을 필수로 받는데 `run_project_action`에는 token도 cancel callback도
없다. downstream P7-05는 project core를 import하지 못하면서 cancellation/error 테스트를
요구한다. UI가 workspace 내부의 숨은 파일명을 추측하게 두면 다시 P4-04 모양이다.

최소 보정: bridge에 `cancellation: CancellationToken | None`을 그대로 노출하지 않으려면 CLI
소유의 좁은 public cancel handle/protocol을 정의해 받는다. UI 취소가 token을 바꾸고 runner가
관측하는 end-to-end 테스트를 P7-03에 먼저 둔다.

### 2. `output_dir`가 두 의미를 겸한다

명령 계약에서 `export/run --out`은 디렉터리지만 `captions --out`은 파일이다. bridge는
`output_dir: Path | None` 하나뿐이다. captions에서 파일을 디렉터리 이름의 변수로 전달하거나
bridge가 action을 보고 의미를 바꾸게 된다.

최소 보정: `output_path: Path | None`로 중립화하고 action별 path kind를 검증하거나,
`package_dir`와 `captions_file`을 분리한다. directory/file mismatch가 client 생성 전에
usage exit 2가 되는 테스트를 둔다.

### 3. `Iterator[Mapping[str, object]]`가 P7-05 소비 필드를 고정하지 않는다

P7-05의 `ProjectUiReport(status, log, workspace, result_paths, needs_analysis)`가 bridge의 어떤
키를 읽는지 계약이 없다. “sanitized progress/status/result paths”라는 산문만으로는 status와
final/draft, result paths와 needs-analysis keys를 UI가 유도한다.

최소 보정: CLI 모듈에 dependency-light `ProjectActionEvent` dataclass를 두거나 mapping의
필수 키·Literal event kind를 명시한다. `status`, `message`, `workspace`, `result_paths`,
`needs_analysis`, `final_accepted`의 의미와 final-accepted가 허용되는 상태를 고정한다. P7-05는
그 타입/스키마만 단순 변환한다.

### 확인되어 충분한 부분

- `project` 하위 명령은 기존 top-level `run`과 argparse에서 공존할 수 있다.
- `--online`과 `--yes/confirmed`의 이중 gate, offline action의 강제 `allow_network=False`,
  별도 vision credential-name 해석 시점은 브리프에 충분히 명시돼 있다.
- exit 0/2/3/4/130 구분도 구현 전에 테스트 표로 고정할 수 있다.

## 두 실패 계보에 대한 판정

1. **`protected=[]` 계보:** P7에서는 `ArtifactRef.completed=True` 또는 checkpoint completed ID가
   retryable error를 덮어 “할 일 없음”으로 만드는 옷으로 재발할 위험이 가장 크다. payload
   status가 authority이고 storage write 완료는 authority가 아니라고 타입에서 갈라야 한다.
2. **P4-04 모양:** P7-01 writer가 root/kind/revision을, loader가 typed record를, P7-02 estimate가
   completed vision evidence를, P7-03 bridge가 cancel/event schema를 싣지 않는다. 전부 소비자
   유도·재구성을 요구하는 같은 결함이다.

## Opus가 판정해야 할 열린 질문

1. P6-04 후보는 encoder payload는 충분하다. merge 전에 record kind/schema를 P6-04 envelope
   밖 개별 record에도 추가할지, P7-01 writer metadata로만 붙일지 결정해야 한다.
2. P7-01 manifest를 generic typed artifact index로 바꿀지, stage별 explicit fields로 늘릴지
   결정해야 한다. 현재 세 field는 resume/reproject에 부족하다.
3. `completed`를 없애고 committed/disposition을 분리할지, 유지하되 typed decoder가 계산한
   read-only 값으로 제한할지 결정해야 한다.
4. request-level cancellation을 지키기 위해 P7-02 ownership을 `speech.py`/`boundary.py`의 좁은
   callback seam까지 넓힐지, acceptance를 stage/source 단위로 낮출지 결정해야 한다.
5. project 경로에서 second-model verify를 실제로 실행할지 결정해야 한다. 현재
   `ProjectConfig`는 repair/verify 가격을 묶지만 shipped `SourceTrace`에는 verification 결과가
   없다. 실행한다면 request/result type과 persistence가 추가로 필요하고, 실행하지 않는다면
   예산 필드/문구가 phantom work를 세지 않게 분리돼야 한다.
6. P7-03 bridge의 public event를 dataclass로 고정할지, 닫힌 mapping schema로 고정할지 결정해야
   한다. 어느 쪽이든 P7-05가 상태를 산문에서 유도하지 않게 해야 한다.

## 근거 위치

- P7-01 계약: `docs/orchestration/tasks/p7-01-workspace.md:15-59`
- P7-02 계약: `docs/orchestration/tasks/p7-02-runner.md:15-72`
- P7-03 bridge/commands: `docs/orchestration/tasks/p7-03-cli.md:15-63`
- shipped `SourceTrace`/`UnavailableSourceAudio`: `vlogsrt/project/speech.py:125-214`
- shipped sync source execution/catch: `vlogsrt/project/speech.py:587-615`
- shipped sync boundary execution/catch: `vlogsrt/project/boundary.py:1116-1191`
- shipped `EditPlan`: `vlogsrt/project/edit.py:722-824`
- shipped `EditCaptionSet`: `vlogsrt/project/captions.py:521-536`
- shipped `BoundaryProjection`: `vlogsrt/project/boundary.py:1922-1937`
- shipped `VisionRequest`/`VisionDecision` and public request hash:
  `vlogsrt/project/vision/policy.py:104-144,496-523`
- 두 설정 도메인: `vlogsrt/config.py:19-85`, `vlogsrt/project/config.py:61-115`
- P6-04 candidate encoders/envelope:
  `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p6-export/vlogsrt/project/export.py:111-215,728-753`
