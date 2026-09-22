# Task X-01B: Swift-only FCPXML 왕복 경로를 앱·extension에 연결한다

## Authority

- Binding plan: `docs/plans/2026-09-21-final-cut-agent-workflow.md` Task 4와 이 브리프.
- 선행 X-01A commit: `b570df0`.
- 목표는 signed 실측 전에 왕복 코드와 자동 시험을 완성하는 것임.
- GPT가 계약과 검증을 소유함. Claude-GLM만 제품 코드와 테스트를 수정함.

## Baseline and ownership

- 제품 코드 기준은 `b570df0`임. dispatch 시작 HEAD는 이 브리프 commit을 포함한 현재 HEAD임.
- 수정 허용: `macos/**`, `scripts/check_macos_xcode.sh`.
- 수정 금지: `vlogsrt/**`, Python tests, API fixtures, `.env*`, `context_compress_fcp-x01.md`.
- 네트워크·모델 호출 금지. Python 엔진 호출도 이 태스크에서 금지함.
- 실제 Final Cut Pro 실행·앱 설치·서명은 이 태스크 범위 밖임.

## Contract

### App Group job

- root는 기존 `AppGroupWorkspace`를 사용함.
- 작업 위치는 `Jobs/<job-id>/`임.
- 입력: `input.fcpxml`.
- 요청: `roundtrip-request.jsonl`.
- 출력: `roundtrip.fcpxml`.
- 결과: `roundtrip-result.jsonl`.
- 상태 어휘: `received | roundtrip_requested | roundtrip_ready | failed`.
- job ID는 안전한 파일명 조각만 허용하고 App Group 밖으로 나갈 수 없음.
- 기존 입력·출력·결과를 조용히 덮어쓰지 않음.

### RoundTripRequest / RoundTripResult

- Swift `Codable`, `Equatable`, `Sendable` 값 타입임.
- 기존 `ProtocolCodec`의 protocol version 1.0 JSONL envelope를 사용함.
- Request는 job ID, 입력 파일명, 출력 파일명을 기록함.
- Result는 job ID, 상태, 출력 파일명 또는 오류 문장을 기록함.
- 성공 결과에 오류가 있거나 실패 결과에 오류가 없으면 생성 시 거부함.

### IdentityRoundTripService

- 입력 바이트를 읽고 새 파일만 씀. 원본을 수정하지 않음.
- FCPXML 안의 `project`가 정확히 하나여야 함. wrapper 위치는 가정하지 않음.
- 프로젝트 이름을 `AI Roundtrip · <원본 이름>`으로 바꿈.
- project의 `uid`만 제거함.
- 그 밖의 요소·속성·텍스트·미디어 URI·시각은 의미상 동일해야 함.
- DTD 선언은 허용하되 entity 선언과 외부 entity 로드는 거부함.
- XML 오류, 프로젝트 0개·2개 이상, 빈 이름은 실패로 기록함.
- 출력은 임시 파일 후 atomic replace로 공개함.

### extension → container

- extension은 FCP pasteboard 또는 수동 선택으로 받은 XML을 job에 저장함.
- 수신 뒤 상태를 `received`로 표시하고 `왕복 확인` 버튼을 활성화함.
- 버튼 클릭 시 request JSONL을 쓰고 상태를 `roundtrip_requested`로 바꿈.
- `vlogsrt-agent://roundtrip/<job-id>` URL을 `NSExtensionContext.open`으로 컨테이너 앱에 전달함.
- extension은 XML 변환이나 Final Cut 전송을 직접 하지 않음.

### container app

- App Info.plist에 URL scheme `vlogsrt-agent`를 등록함.
- AppDelegate는 custom URL과 기존 file URL을 명시적으로 분기함.
- custom URL은 scheme `vlogsrt-agent`, host `roundtrip`, path의 job ID만 허용함.
- container가 IdentityRoundTripService를 실행하고 Result를 씀.
- 성공 뒤 기존 `FinalCutImporter.sendOpenDocument`로 결과 FCPXML을 보냄.
- import 실패도 `failed` Result에 남기며 성공으로 세탁하지 않음.

## Implementation steps

1. 실제 동작 시험을 Swift Package tests에 먼저 추가하고 RED를 확인함.
2. job open/atomic file helpers와 request/result 타입을 최소 구현함.
3. IdentityRoundTripService를 구현하고 XML 보존 시험을 GREEN으로 만듦.
4. custom URL parser와 container coordinator를 fake importer로 시험함.
5. App Info.plist URL scheme과 AppDelegate routing을 구현함.
6. WorkflowViewController에 `왕복 확인` 버튼·상태·container open을 연결함.
7. `project.yml` 재생성 뒤 `.xcodeproj` diff를 커밋함.
8. macOS bundle 검사에 URL scheme·principal class·App Group 보존 검사를 추가함.
9. Swift tests, unsigned Xcode build, Python 전체 gate를 실행함.
10. 한 커밋으로 만들고 review-ready ask를 보냄. GPT 승인 전 worker_done 금지.

## Acceptance

### Swift tests

- library-wrapped FCPXML roundtrip 성공.
- browser/item 형태처럼 wrapper가 달라도 project 하나면 성공.
- 이름·project uid 외 의미 변경 0건.
- 원본 바이트 파일 변경 0건.
- project 0개·2개, 빈 이름, malformed XML, entity 선언 거부.
- `../` job ID와 App Group 밖 경로 거부.
- 기존 output/request/result 덮어쓰기 거부.
- 잘못된 custom URL 거부.
- fake importer 성공·실패 Result가 정확함.

### 필수 명령

```bash
xcodegen generate --spec macos/project.yml --project macos
git diff --exit-code -- macos/FinalCutAgent.xcodeproj
CLANG_MODULE_CACHE_PATH=/private/tmp/vlogsrt-x01b-clang \
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/vlogsrt-x01b-swift \
swift test --package-path macos --scratch-path /private/tmp/vlogsrt-x01b-swift-build
scripts/check_macos_xcode.sh
make check PY=/Users/ssojux2/Working/vlogsrt/.venv/bin/python
```

### bundle·source 검사

- app plist에 `vlogsrt-agent` URL scheme이 정확히 하나 있음.
- extension principal class는 `FinalCutAgentWorkflowExtension.WorkflowViewController`임.
- App Group entitlement는 앱·extension 모두 유지됨.
- extension source에 Python Process 실행이 없음.
- compiler/linker warning 0건임.

## Failure route

- XMLDocument 직렬화가 원본 의미를 바꾸면 직접 string 치환으로 우회하지 말고 변환 전후 구조 비교를 강화함.
- custom URL open API가 app-extension unavailable이면 실제 compiler 오류를 GPT에 ask함. extension에서 Process 실행으로 우회 금지.
- FinalCutImporter는 protocol로 감싸 테스트에서 실제 Final Cut를 열지 않음.
- 세 번의 같은 실패 뒤에는 코드를 더 붙이지 말고 GPT에 설계 재판정을 요청함.

## Report

- review-ready ask에 commit SHA, diff stat, 새 Swift test 수를 적음.
- RED 명령·실패 이유와 GREEN 명령·exit code를 적음.
- macOS bundle 검사 pass 수, Python pass 수, warning 수를 적음.
- 실제 Final Cut 미실행과 unsigned 상태를 PENDING_HUMAN으로 적음.
- GPT ACCEPT 뒤에만 worker_done을 보냄.
