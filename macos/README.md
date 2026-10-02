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
- `FinalCutTimelineBridge`: 타임라인에 열린 프로젝트 UID·이름·시간 표시와 playhead 이동
- `FCPXMLProjectGate`: 드롭·직접 선택 FCPXML의 단일 프로젝트 UID 일치 강제
- `AppGroupWorkspace`: `5955379R8P.com.ssojux2.vlogsrt`의 작업 디렉터리 —
  macOS Personal Team 형식(`<TeamID>.<group name>`). 프로비저닝된 `group.`
  식별자도 macOS에서 지원되지만, 이 Personal Team 빌드의 embedded profile에는
  com.apple.security.application-groups가 없어 containermanagerd가 그 group을
  TCC 보호 컨테이너 요청으로 거부했다. Team ID 접두사 형식은 profile 없이 쓴다
- `KeychainStore`: API 키 값 보관. 키 값은 JSONL·로그에 넣지 않음
- `SecurityScopedBookmarkStore`: 사용자가 고른 미디어/도구 경로 bookmark
- `DevelopmentEngineLauncher`: Application Scripts의 `vlogsrt-engine`를 `NSUserUnixTask`로 실행
- `DevelopmentJobStager`: App Group 입력을 bookmark 작업 폴더로 복사하고 허용된 결과만 동기화
- `FinalCutImporter`: `NSWorkspace` Open Document로 새 FCPXML을 Final Cut Pro에 전송
- `ShareInbox`: 공유 대상의 WAV+FCPXML을 함께 복사하고 `CaptionRequest` JSONL 생성
- `EngineProcess`: 컨테이너 앱에서 장시간 Python 작업을 실행하는 경계

## 빌드 검증 흐름

Team ID는 project에 넣지 않고 로컬 `LocalSigning.xcconfig`에만 둔다.
unsigned 자동 검사는 한 줄로 실행함:

```bash
scripts/check_macos_xcode.sh
```

내용(35건): XcodeGen 재생성 결정성 · plist/entitlements 문법 · App Group
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

## 로컬 개발 서명

`project.yml`은 `LocalSigning.xcconfig`를 include하지 않는다 — 빌드할 때
직접 넘겨야 효력이 생긴다.

1. `macos/LocalSigning.xcconfig`(gitignored)에 `DEVELOPMENT_TEAM`을 적는다:
   `DEVELOPMENT_TEAM = XXXXXXXXXX`
2. 그 파일을 실제로 적용해 빌드한다:
   `xcodebuild -project macos/FinalCutAgent.xcodeproj -scheme FinalCutAgent -configuration Debug -xcconfig macos/LocalSigning.xcconfig -allowProvisioningUpdates build`
3. app은 App Group·user-selected read-write가, appex는 App Group·user-selected
   read-only가 서명 결과에 남아야 함.
4. `macos/scripts/install-development-engine.sh`를 실행해 Application Scripts 런처를 설치함.

공증, 배포 서명, 자동 업데이트는 첫 개인용 버전 범위 밖이다.

## 사용자 지정 공유 대상 — Apple Media Asset Protocol

`FinalCutAgent.sdef`와 `MediaAssetScripting.swift`는 Apple의 정본 문서
("Receiving Media and Data Through a Custom Share Destination" ·
"Creating Scripting Definitions for Custom Share Destinations" ·
"Responding to Apple Events from Final Cut Pro")가 정의하는 Apple event
생명주기를 따른다:

1. **make(corecrel)** — `make new asset with properties {name, metadata,
   data options}`로 자리표시자 자산을 만들고 그 지시자
   (application의 `assets` 요소 안 id)를 돌려준다.
2. **get location info** — asset location 레코드. AI Rough Cut은 **XML
   전용**이다: `has description: true`·`has media: false`, 폴더는 App Group의
   `ShareStaging/<자산 id>`(없으면 임시 폴더)다.
3. **get library info** — 라이브러리 보관을 원하지 않는다는 답(두 플래그 모두
   거짓).
4. **get metadata** — 받은 공유 메타데이터 + `com.apple.proapps.share.id`.
5. **get data options** — 후보에서 고른 답: 보기는 "None", DTD 버전은 엔진
   계약(1.14)에 가장 가까운 것.
6. **open** — 내보낸 파일이 도착하면 기존 인박스 스테이징
   (`RoughCutInbox`·`CaptionInbox`)이 그대로 받는다. 원본 미디어는 건드리지
   않는다.

sandbox Final Cut이 이 객체·명령에 닿으려면 access group
(`com.apple.proapps.assetmanagement.import`)이 자산 클래스·컨테이너의 asset
요소·make 명령에 붙어 있어야 한다(문서 요구사항).

**`.fcpxdest`는 Final Cut이 만들어야 한다.** `Resources/`의 손수 만든
Info.plist뿐인 `.fcpxdest` 번들은 Final Cut이 **무시한다** — 끌어다 놔도
설치되지 않는다(실측). 정본은 Final Cut UI에서 만든다:

1. Final Cut → File > Share > Add Destination에서 Export File을 복제해 이름을
   정한다(예: AI Rough Cut).
2. "Open with" 메뉴에서 Other → 이 앱을 고른다.
3. Final Cut > Preferences > Destinations에서 그 대상을 Finder로 끌어내면
   진짜 `.fcpxdest` 파일이 생긴다 — 그것을 배포·설치한다.

Apple event 왕복은 `scripts/apple-event-smoke.applescript`로 누른다(설치본
대상, 첫 실행의 Automation 동의 필요). 2026-10 실측 참고: 이 시스템
(macOS 26.6)은 로컬 unsigned/개발 서명 번들에 scripting terminology를
실어주지 않는다 — smoke는 제대로 서명·설치된 앱에서만 용어 형태가 컴파일된다
(원시 이벤트 형태는 그 게이트를 우회해 계약 자체를 잡는다). 그 왕복을 실측하기
전까지 이 경계를 완성됐다고 보지 않는다.
