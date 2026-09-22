# Task X-01A: 실제 Xcode 프로젝트와 unsigned 앱·appex를 만든다

## Authority

- Binding plan: `docs/plans/2026-09-21-final-cut-agent-workflow.md`의 Task 4와 이 브리프.
- 현재 단계는 Final Cut 실측 전의 packaging spike임. Python 분석 엔진은 고치지 않음.
- GPT 코디네이터가 계약과 검증을 소유함. Claude-GLM만 제품 코드와 테스트를 수정함.

## Baseline and ownership

- 작업 브랜치·worktree: 현재 `Ssojux2/model-context-window-length` / 현재 Orca worktree.
- 시작 전 `git status --short`에서 GPT가 만든 이 브리프 외 tracked 변경이 없어야 함.
- 수정 허용: `macos/**`, `.gitignore`, 이 태스크에 필요한 macOS 전용 검사 스크립트.
- 수정 금지: `vlogsrt/**`, `tests/fixtures/**`, `golden/**`, `.env*`, `context_compress_fcp-x01.md`.
- API 호출과 네트워크 모델 호출 금지. `--online`과 `VLOGSRT_OFFLINE=0` 금지.
- 한 Claude-GLM worker만 작성함. 다른 작성자와 병렬 작업하지 않음.

## Contract

### 정본과 생성물

- `macos/project.yml`이 Xcode 프로젝트의 정본임.
- XcodeGen 2.46.0으로 `macos/FinalCutAgent.xcodeproj`를 생성함.
- `project.yml`과 생성된 `.xcodeproj`를 모두 커밋함.
- 재생성 뒤 `.xcodeproj` diff가 없어야 함.
- `xcuserdata`, DerivedData, 로컬 signing 설정은 커밋하지 않음.

### target

1. `FinalCutAgentCore`
   - macOS static library.
   - `macos/Sources/FinalCutAgentCore/*.swift`만 컴파일함.
2. `FinalCutAgent`
   - macOS application.
   - `macos/Sources/FinalCutAgentApp/main.swift`를 컴파일함.
   - `FinalCutAgentCore`와 `FinalCutAgentWorkflowExtension`에 의존함.
   - extension을 `Contents/PlugIns`에 embed함.
3. `FinalCutAgentWorkflowExtension`
   - macOS app extension, wrapper extension `appex`, Mach-O executable.
   - `macos/Sources/WorkflowExtension/WorkflowViewController.swift`를 컴파일함.
   - `FinalCutAgentCore`에 의존함.

### 고정 설정

- 최소 macOS: 14.0.
- 앱 bundle ID: `com.ssojux2.vlogsrt`.
- extension bundle ID: `com.ssojux2.vlogsrt.workflow`.
- App Group: `group.com.ssojux2.vlogsrt`.
- extension point: `com.apple.FinalCut.WorkflowExtension`.
- `ADDITIONAL_SDKS=/Library/Developer/SDKs/WorkflowExtensionSDK.sdk`.
- `LD_ENTRY_POINT=_ProExtensionMain`.
- `OTHER_LDFLAGS=$(inherited) -fapplication-extension -lProExtension`.
- `APPLICATION_EXTENSION_API_ONLY=YES`.
- signing Team ID는 project에 넣지 않음. unsigned 검증은 `CODE_SIGNING_ALLOWED=NO`로 실행함.
- App/extension plist의 bundle identifier는 build setting과 어긋나지 않게 변수로 받음.
- 현재 `WorkfloWExtension` 오타를 반드시 고침.

## Implementation steps

1. 검사 스크립트를 먼저 추가하고 실행해 `.xcodeproj` 부재로 실패하는 RED를 확인함.
2. `.gitignore`에 Xcode 사용자·build 산출물과 `macos/LocalSigning.xcconfig`를 추가함.
3. `project.yml`과 필요한 shared build config를 작성함.
4. XcodeGen으로 project를 생성함.
5. Swift Package 테스트를 유지하면서 unsigned Xcode build를 통과시킴.
6. app bundle 안에 appex가 embed됐는지 검사함.
7. plist, executable, SDK/linker 설정을 검사함.
8. `macos/README.md`의 “SDK 미설치” 설명을 설치 완료 상태와 unsigned-first 흐름으로 고침.
9. 생성 결정성, scoped 검사, 전체 검사 순서로 실행함.
10. 모든 변경을 한 커밋으로 만들고 review-ready ask를 보냄. GPT 승인 전 worker_done 금지.

## Acceptance

### RED 증거

- macOS 검사 스크립트가 구현 전 `macos/FinalCutAgent.xcodeproj` 또는 app/appex 부재로 실패함.

### 필수 명령

```bash
xcodegen --version
xcodegen generate --spec macos/project.yml --project macos
git diff --exit-code -- macos/FinalCutAgent.xcodeproj
CLANG_MODULE_CACHE_PATH=/private/tmp/vlogsrt-x01-clang \
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/vlogsrt-x01-swift \
swift test --package-path macos --scratch-path /private/tmp/vlogsrt-x01-swift-build
xcodebuild -project macos/FinalCutAgent.xcodeproj \
  -scheme FinalCutAgent -configuration Debug \
  -derivedDataPath /private/tmp/vlogsrt-x01-derived \
  CODE_SIGNING_ALLOWED=NO clean build
make check PY=/Users/ssojux2/Working/vlogsrt/.venv/bin/python
```

### bundle 검사

- `/private/tmp/vlogsrt-x01-derived/Build/Products/Debug/FinalCutAgent.app` 존재.
- `Contents/PlugIns/FinalCutAgentWorkflowExtension.appex` 존재.
- app·appex executable이 존재하고 `file`이 Mach-O로 판정함.
- appex plist의 extension point가 정확히 `com.apple.FinalCut.WorkflowExtension`임.
- `xcodebuild -showBuildSettings`에서 SDK, entry point, linker flag, bundle ID가 위 계약과 일치함.
- `plutil -lint`가 plist·entitlements 모두 통과함.
- compiler warning과 linker warning이 0건임.

## Failure route

- 설정·build 실패는 로그 전문을 `/private/tmp/vlogsrt-x01-*.log`에 보존하고 마지막 80줄을 보고함.
- SDK 1.0.3와 Xcode 27 호환 결함을 발견하면 재현 명령과 실제 error를 GPT에 ask함.
- 서명 문제는 이 태스크 결함이 아님. `CODE_SIGNING_ALLOWED=NO`에서도 실패할 때만 수정함.
- `.appex`가 build되지만 embed되지 않으면 app target dependency/copy phase를 고침.
- 세 번 고쳐도 같은 오류가 남으면 더 수정하지 말고 GPT에 설계 재판정을 요청함.

## Report

- review-ready ask에 commit SHA, `git diff --stat`, 수정 파일을 적음.
- 위 명령마다 exit code와 실제 pass 수를 적음.
- app·appex 실제 경로와 plist 핵심값을 적음.
- 남은 warning, signed 단계 blocker, PENDING_HUMAN을 적음.
- GPT가 ACCEPT 또는 수정 지시를 reply할 때까지 코드 수정 없이 대기함.
- ACCEPT 뒤 `worker_done --outcome succeeded`를 정확히 한 번 보냄.
