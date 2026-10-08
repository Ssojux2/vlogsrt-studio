# context_compress: finalcut-share-xml
갱신: 2026-10-02 09:37 KST | 계획: docs/plans/2026-09-27-vlog-automation-stabilization.md | 브랜치: Ssojux2/r08-clip-input | 진행: 1/3

## 완료
- [x] 1. 공유 대상 원인 분석 및 Apple Media Asset Protocol 구현 — commit ddd5efe — 루트 `swift test --package-path macos --scratch-path /private/tmp/vlogsrt-root-share-acceptance` 208 통과, signed xcodebuild 성공.
- [x] 2. Final Cut UI에서 `AI Rough Cut` 실제 대상 생성·등록 — File > Share 메뉴에 표시 확인, Finder로 내보낸 `/private/tmp/vlogsrt-share-export.sWjHnP/AI Rough Cut.fcpxdest`는 3.5KB NSKeyedArchiver binary plist. 큰 영상은 공유/출력하지 않음.

## 다음 행동
1. Orca task `task_fb6121320b6a` / dispatch `ctx_4a5967110472`의 구현 완료 확인. SDEF 등록(-192) 및 진짜 `.fcpxdest` 패키징 검사.
2. 독립 signed build, `sdef`, AppleScript smoke 재검증; 사용자 프로젝트/영상 내보내기 금지.
3. 검증 통과 시 현재 설치 앱 갱신·메뉴 연결을 안전하게 검토하고, 불가 시 정확한 차단점 보고.

## 핵심 사실
- Apple 공식 custom share 프로토콜은 `location info`에서 `has description=true`, `has media=false`를 허용한다. 따라서 XML만 받아 원본 미디어 경로를 참조 가능.
- Workflow Extension timeline proxy는 열린 프로젝트 메타/시간 등만 제공. 제자리 컷 수정 API는 없으며 승인 후 새 FCPXML 프로젝트로 가져오는 설계가 안전하다.
- 기존 `macos/Resources/*.fcpxdest`는 Info.plist만 있는 수작업 디렉터리로 Final Cut이 무시. 실제 FCP 생성 파일은 위 temp 경로.
- 현재 `/Applications/FinalCutAgent.app`는 구버전. signed build는 `/private/tmp/vlogsrt-share-signed-derived/Build/Products/Debug/FinalCutAgent.app`; `sdef`가 -192. App-Info.plist의 OSAScriptingDefinition 값이 확장자 포함이며 NSAppleScriptEnabled 없음.
- 사용자 원본 미디어·`.env`·`.env.example`·자격 증명 읽기/수정 금지. 유료 API 및 실제 프로젝트 공유 테스트 하지 않음.

## 실행 중
- Claude-GLM Orca task `task_fb6121320b6a`, dispatch `ctx_4a5967110472`, terminal `term_c4a48317-0be3-471e-b33d-629620a03cb8` — manifest/packaging 수정 및 테스트.

## 미해결 / 위험
- signed app AppleScript/Final Cut 실제 왕복 미검증. TCC 동의가 필요할 수 있음.
- `AI Rough Cut` 공유 대상의 Format UI는 Video and Audio로 보이나, 새 호스트는 XML-only로 응답하도록 구현됨. 실제 출력 전 꼭 왕복 확인.
- 유저 dirty `.env.example`, 보고서, 기존 context 파일 보존. 패키징 후 불필요한 거짓 대상 리소스 처리 필요.
