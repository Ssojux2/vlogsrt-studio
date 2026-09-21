# Final Cut Agent macOS 골격

이 디렉터리는 컨테이너 앱, Workflow Extension principal view controller, 두 프로세스가
공유하는 코어를 담는다. Apple의 **Workflow Extension SDK 1.0.3**이
`/Library/Developer/SDKs/WorkflowExtensionSDK.sdk`에 설치되어 있고,
`project.yml`이 Xcode 프로젝트의 정본이다. `FinalCutAgent.xcodeproj`는
XcodeGen 2.46.0의 생성물이며 재생성 검사를 위해 함께 커밋한다 — 프로젝트 설정을
고칠 때는 `.xcodeproj`를 직접 만지지 말고 `project.yml`을 고치고 다시 생성한다.

## 포함된 경계

- `FinalCutPasteboard`: `com.apple.finalcutpro.xml.v1-14`, 이전 버전, generic XML 순으로 수신
- `WorkflowViewController`: 패널 드롭과 수동 `.fcpxml/.fcpxmld` 선택 fallback
- `AppGroupWorkspace`: `5955379R8P.com.ssojux2.vlogsrt`의 작업 디렉터리 —
  macOS Personal Team 형식(`<TeamID>.<group name>`). iOS식 `group.` 접두사는
  containermanagerd가 TCC 보호 컨테이너 요청으로 거부한다
- `KeychainStore`: API 키 값 보관. 키 값은 JSONL·로그에 넣지 않음
- `SecurityScopedBookmarkStore`: 사용자가 고른 미디어/도구 경로 bookmark
- `FinalCutImporter`: `NSWorkspace` Open Document로 새 FCPXML을 Final Cut Pro에 전송
- `ShareInbox`: 공유 대상의 WAV+FCPXML을 함께 복사하고 `CaptionRequest` JSONL 생성
- `EngineProcess`: 컨테이너 앱에서 장시간 Python 작업을 실행하는 경계

## unsigned-first 빌드 흐름

서명은 아직 단계에 없다. 검증은 `CODE_SIGNING_ALLOWED=NO`로 하고, Team ID는
project에 넣지 않는다. 전 검사는 한 줄로:

```bash
scripts/check_macos_xcode.sh
```

내용(35건): xcodegen 재생성 결정성 · plist/entitlements 문법 · App Group
보존(재생성이 entitlements를 비우지 않는다) · extension source 무 Process ·
unsigned clean build · compiler/linker warning 0건 · `.app` 안
`Contents/PlugIns`의 `.appex` embed · 실행 파일 Mach-O · 빌드된 plist 값
(bundle id · extension point · principal class · `vlogsrt-agent` URL scheme 1개) ·
`-showBuildSettings` 계약(SDK·entry point·linker flag·bundle ID).

수동으로 같은 일을 하려면:

```bash
xcodegen generate --spec macos/project.yml --project macos
xcodebuild -project macos/FinalCutAgent.xcodeproj \
  -scheme FinalCutAgent -configuration Debug \
  -derivedDataPath /private/tmp/vlogsrt-x01-derived \
  CODE_SIGNING_ALLOWED=NO clean build
```

코어 단위 테스트는 Swift Package로 그대로 유지된다:

```bash
CLANG_MODULE_CACHE_PATH=/private/tmp/vlogsrt-x01-clang \
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/vlogsrt-x01-swift \
swift test --package-path macos --scratch-path /private/tmp/vlogsrt-x01-swift-build
```

## 알려진 기계 의존 동작

- **Xcode 27은 `ADDITIONAL_SDKS`를 링크 검색경로에 반영하지 않는다.**
  `SDK_NAMES`에는 잡히지만 ld 명령에 SDK 경로가 붙지 않아
  `-lProExtension`이 `library not found`로 죽는다. appex target의
  `LIBRARY_SEARCH_PATHS`에 SDK의 `usr/lib`을 직접 지정해서 템플릿 동작을
  복원했다. `ADDITIONAL_SDKS`는 계약값 그대로 둔다.
- **`main.swift`에 `@main`을 쓸 수 없다.** Xcode는 "module에 top-level code가
  있다"며 거부하고, SPM은 top-level 진입점을 기대한다. `main.swift`는 두
  빌드가 함께 쓰는 top-level 형태다.
- `appintentsmetadataprocessor`가 "Metadata extraction skipped" 경고를
  남기지만 compiler/linker 경고가 아니며 끄는 build setting도 없다.

## 서명 단계 (아직 범위 밖)

`project.yml`은 `LocalSigning.xcconfig`를 include하지 않는다 — 빌드할 때
직접 넘겨야 효력이 생긴다.

1. `macos/LocalSigning.xcconfig`(gitignored)에 `DEVELOPMENT_TEAM`을 적는다:
   `DEVELOPMENT_TEAM = XXXXXXXXXX`
2. 그 파일을 실제로 적용해 빌드한다:
   `xcodebuild -project macos/FinalCutAgent.xcodeproj -scheme FinalCutAgent -configuration Debug -xcconfig macos/LocalSigning.xcconfig -allowProvisioningUpdates build`
3. App Group entitlements가 서명 시 적용되는지 확인한다.
4. extension은 Final Cut Pro가 로드하므로 개발 서명 검증은 Final Cut 실측과
   함께 한다.

공증, 배포 서명, 자동 업데이트는 첫 개인용 버전 범위 밖이다. 사용자 지정 공유 대상의
Create Asset/Get Property Apple-event scripting target은 실제 Final Cut 왕복 스파이크에서
Apple의 Asset Management Suite 템플릿으로 생성해야 한다. capability와 Open Document
수신 경계는 준비되어 있지만, 실측 전에는 완성됐다고 보지 않는다.
