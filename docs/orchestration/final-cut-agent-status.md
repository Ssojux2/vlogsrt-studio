# context_compress: final-cut-agent

갱신: 2026-09-23 | 브랜치: `Ssojux2/model-context-window-length` | 워크트리: `/Users/ssojux2/orca/workspaces/vlogsrt/cory` | 기준 HEAD: `92fadce`

□ 이 문서의 범위
  ○ Final Cut Agent 납품 형태(X 페이즈)의 완료 상태와 다음 계획만 다룸
  ○ 자막 엔진(S·Q 시리즈)과 순서 역전(R 시리즈)은 `TASKS.md`와 `docs/orchestration/prototype-test-plan.md`가 정본임

---

## 1. 완료 — 코드

□ 워크플로 프로토콜
  ○ 분석 프로토콜 1.2 — 가격 snapshot·승인 drift·실사용량·취소·후보 preview
  ○ 자막 프로토콜 1.1 — 비용 추정·승인·WAV/FCPXML 검증·SRT 결과 계약
  ○ LegacyJobs — 1.1만 읽기 전용 복사, 1.2 오탐 방지

□ macOS 실행 경계
  ○ Release는 번들 `Engine/bin`만 사용하고 Homebrew 경로로 되돌아가지 않음
  ○ Debug fallback·Keychain·취소 후 sync/import 차단

□ 미디어 권한
  ○ 다중 bookmark·외부 경로 거부·symlink 거부·scope 유지·media grant revision

□ 공급 경계
  ○ PyInstaller onedir manifest·ffmpeg/ffprobe·Mach-O 서명 검사
  ○ 실물이 없으면 build/verify가 rc=1로 닫힘. 빈 엔진을 성공으로 가장하지 않음

□ Custom Share
  ○ fcpxdest·SDEF·NSScriptCommand make·KVC 자산 응답

## 2. 완료 — 커밋

| 커밋 | 내용 |
|---|---|
| `2f7330e` | 분석·자막 프로토콜, 가격 snapshot, 취소 경계 |
| `db1d053` | macOS 실행 경계·미디어 권한·Custom Share |
| `ec89024` | 엔진·ffmpeg 공급 계약 |
| `6f2ded2` | `.build/` 무시 |
| `ee61b8e` | D41 — SDK 실측 |

□ `macos/project.yml`과 재생성한 pbxproj를 `db1d053` 한 커밋에 함께 넣음
□ 미추적으로 남긴 것은 이 문서 하나뿐임

## 3. 완료 — 게이트 실측

| 게이트 | 결과 |
|---|---|
| `ruff check` | 통과 |
| `ruff format --check` | 115파일 통과 |
| `mypy` | 52파일 통과 |
| `pytest` | 2644 통과 |
| `scripts/check_invariants.py` | 21개 통과, rc=0 |
| `swift test` | 59개 통과 |
| `scripts/check_macos_xcode.sh` | 35/35 통과 |

□ 기준선 대비: pytest 2555 → 2644. 줄지 않았으므로 회귀 없음
□ macOS 검사의 기존 실패 1건은 pbxproj 미커밋이었고 커밋이 닫음

## 4. 완료 — 결정

□ D41 — Media Asset Protocol의 XPC 어댑터는 SDK에 자리가 없음
  ○ SDK는 `/Library/Developer/SDKs/WorkflowExtensionSDK.sdk`에 설치돼 있고 Headers·Modules를 갖춤
  ○ 이전 판단 「헤더 부재」는 오류였음. 본 것은 Final Cut Pro 번들 안의 복사본이었음
  ○ 그 SDK에 `asset`·`xpc`·`media` 선언이 0건임. `FCPXHost`는 타임라인 읽기와 재생헤드 이동만 노출함
  ○ 따라서 자산 표면은 Apple event·SDEF 하나뿐이고 `MediaAssetScripting.swift`가 이미 그 길임
  ○ 근거의 한계: 2024-10-30 배포 SDK 기준임. Apple이 자산 API를 더하면 결정을 다시 엶

□ 가격 — Luna 입력 $0.20/MTok·출력 $1.20/MTok, whisper $0.006/min, gpt-transcribe $0.0045/min
□ 이미지 — low detail, 512×512 이하, 장당 입력 308 tokens, 창당 출력 256, reasoning none
□ 취소 — 완료된 cache는 보존하고 이후 API 호출·동기화·import만 차단함
□ 자막 — SRT 자동 import 없이 `Relative to Timeline` 수동 import만 안내함

## 5. 완료 — 통합 확인

□ V1 비전 재생기는 이미 `92fadce`에 들어 있음. 별도 검토가 필요 없음
  ○ `FixtureVisionResponder`가 `commands.py` 2건·`transcribe.py` 1건으로 잡히고 D40이 문서에 있음
  ○ `92fadce`가 `da000eb`보다 앞서고 `request_luna_vision`·`ConfirmedNoSpeech`까지 더 가짐
  ○ `v1-vision-responder` 워크트리는 낡은 상태임

---

## 6. 다음 계획

### 6-1. 사람 몫 — 공급 (막고 있는 것)

□ LGPL arm64 ffmpeg·ffprobe 확보
  ○ 이 머신 ffmpeg 9.0.1은 `enable-gpl`·`enable-version3`이라 GPLv3임. manifest의 LGPL 계약에 못 넣음
  ○ 절차: 바이너리를 `macos/Resources/FFmpeg/`에 두고 `presets/ffmpeg-manifest.json`의 sha256 둘을 기입
  ○ 확인: `python3 scripts/verify_ffmpeg.py`가 rc=0
□ PyInstaller 설치 승인
  ○ `.venv`에 없음. 런타임 의존성이 아니므로 규칙 10과 부딪히지 않음
  ○ 확인: `python3 scripts/build_engine.py --sign-identity <identity>`가 rc=0

### 6-2. 사람 몫 — 실기 (Final Cut 필요)

□ 개발 서명 신원 확보 후 서명 앱으로 체크리스트 수행
□ 2분 / 10~20분 / 77분 인수와 원본 해시 확인
□ KVC Apple event 계층의 실기 확인 — D41이 「남는 것은 구현이 아니라 실기 확인」이라고 못 박음
□ PROTO-7 — 내보낸 FCPXML을 실제 Final Cut에 가져오고 첫 실패를 기록함. 고치지 않고 적음

### 6-3. 무료·코드 — 실기 뒤에 열리는 것

□ KVC Apple event 계층 전용 runtime test 추가
  ○ 실기에서 받은 응답을 픽스처로 넣어야 회귀가 무료로 닫힘
□ gpt-transcribe 실사용량 분리 기록
  ○ 지금은 비용 상한만 포함하고 실제 usage를 내부 러너에서 갈라내지 못함

### 6-4. 이 작업선 밖 — 순서만 밀려 있는 것

□ V4 — 비전 호환성 유료 1콜로 `vision/policy.py:32`의 PENDING_HUMAN을 닫음
□ V5 — 제품 표면 분리: R-06 검토 상태 기계 · R-07 컷 전용 패키지 · `cut|approve|caption`
□ R-09 Gradio 철거와 D36 작성 — 의존성이 없어 아무 때나 가능함
□ 보드 통합 — `TASKS.md`에 R·P7·X 행이 없어 원장과 갈라져 있음
□ 유료 재녹화 1회 — S11·S16·Q1·Q2·Q3-H가 한꺼번에 풀림
□ 워크트리 22개 정리 — 지우는 작업이므로 사용자 확인 뒤에 함

---

## 7. 핵심 파일

| 경로 | 무엇 |
|---|---|
| `vlogsrt/project/workflow_protocol.py` | 1.2/1.1/1.0 JSONL 계약과 상태 원장 |
| `vlogsrt/project/workflow_analysis.py` | Luna 비용·frame evidence·usage·preview |
| `vlogsrt/project/pricing.py` | 2026-09-21 USD Standard 가격 snapshot |
| `vlogsrt/project/commands.py` | 승인·취소·media grant·caption 실행 게이트 |
| `macos/Sources/FinalCutAgentCore/EngineRuntime.swift` | 실행 전·sync 전·import 전 게이트 |
| `macos/Sources/FinalCutAgentCore/MediaAssetScripting.swift` | Apple event make·KVC 응답 |
| `macos/Sources/WorkflowExtension/FinalCutTimelineBridge.swift` | ProExtensionHost 타임라인 읽기 |
| `macos/Sources/FinalCutAgentApp/main.swift` | UI·bookmark·Share·엔진 실행 연결 |
| `scripts/build_engine.py` · `scripts/verify_ffmpeg.py` | 번들 생성·공급 검증 |
| `presets/ffmpeg-manifest.json` | ffmpeg·ffprobe 공급 정본 |

## 8. 재개 절차

1. `git -C <워크트리> log --oneline -6`으로 `ee61b8e`가 HEAD인지 확인함
2. 게이트 5개와 `swift test`·`check_macos_xcode.sh`를 돌려 기준선을 다시 잡음
3. 6-1이 닫히지 않았으면 코드로 할 일이 없음. 6-3은 6-2 뒤에만 열림
4. `make`를 쓰지 않음. 이 머신의 `make`는 Xcode 라이선스로 죽어 있음
