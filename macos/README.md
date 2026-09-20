# Final Cut Agent macOS 골격

이 디렉터리는 컨테이너 앱, Workflow Extension principal view controller, 두 프로세스가
공유하는 코어를 한 번에 컴파일하는 Swift Package다. 현재 머신에는 Final Cut Pro 앱의
런타임 프레임워크는 있지만 Apple의 **Workflow Extension SDK/Xcode 템플릿은 설치되어
있지 않다**. 따라서 Swift 코드는 빌드·테스트되지만 `.appex` 번들 생성과 개발 서명은
SDK 설치 뒤 Xcode 템플릿에서 해야 한다.

## 포함된 경계

- `FinalCutPasteboard`: `com.apple.finalcutpro.xml.v1-14`, 이전 버전, generic XML 순으로 수신
- `WorkflowViewController`: 패널 드롭과 수동 `.fcpxml/.fcpxmld` 선택 fallback
- `AppGroupWorkspace`: `group.com.ssojux2.vlogsrt`의 작업 디렉터리
- `KeychainStore`: API 키 값 보관. 키 값은 JSONL·로그에 넣지 않음
- `SecurityScopedBookmarkStore`: 사용자가 고른 미디어/도구 경로 bookmark
- `FinalCutImporter`: `NSWorkspace` Open Document로 새 FCPXML을 Final Cut Pro에 전송
- `ShareInbox`: 공유 대상의 WAV+FCPXML을 함께 복사하고 `CaptionRequest` JSONL 생성
- `EngineProcess`: 컨테이너 앱에서 장시간 Python 작업을 실행하는 경계

## 로컬 빌드

```bash
CLANG_MODULE_CACHE_PATH=/private/tmp/vlogsrt-clang \
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/vlogsrt-swift-module \
swift test --package-path macos --scratch-path /private/tmp/vlogsrt-swift-build
```

## Xcode 개발 서명 연결

1. Apple Workflow Extension SDK를 설치한다.
2. macOS App 프로젝트를 만들고 `FinalCutAgentCore`를 local package로 추가한다.
3. Final Cut Pro Workflow Extension 템플릿 target을 컨테이너 앱에 embed한다.
4. `WorkflowViewController.swift`, `Resources/WorkflowExtension-Info.plist`, 두 entitlements의
   값을 target에 적용한다.
5. 컨테이너 앱 `Info.plist`에 `Resources/App-Info.plist`의 document types와
   `com.apple.proapps.MediaAssetProtocol` 빈 dictionary를 적용한다.
6. 개인 Team의 Development 서명을 두 target에 지정하고 App Group을 동일하게 설정한다.

공증, 배포 서명, 자동 업데이트는 첫 개인용 버전 범위 밖이다. 사용자 지정 공유 대상의
Create Asset/Get Property Apple-event scripting target은 실제 Final Cut 왕복 스파이크에서
Apple의 Asset Management Suite 템플릿으로 생성해야 한다. capability와 Open Document
수신 경계는 준비되어 있지만, 실측 전에는 완성됐다고 보지 않는다.

