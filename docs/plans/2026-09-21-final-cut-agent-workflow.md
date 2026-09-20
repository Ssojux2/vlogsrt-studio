# Final Cut Pro 에이전트 러프컷·자막 워크플로 구현

사용자 제공 계획(2026-09-21)을 실행 가능한 단위로 옮긴 문서다. 기존
`feat/final-cut-project`의 FCPXML·시간·미디어·VAD·내보내기 계층과 레거시
`vlogsrt run|redo|eval|probe|record`를 보존한다.

## 전역 제약

- 원본 FCPXML·라이브러리·미디어는 읽기만 한다.
- 승인되지 않은 후보는 편집 결과에 반영하지 않는다.
- 분석 실패·지원하지 않는 요소·리비전 불일치는 삭제 권한으로 바뀌지 않는다.
- 비전은 삭제 권한을 만들지 않고 후보를 취소할 수만 있다.
- 자막은 최종 공유 오디오를 새로 전사해 만들며 0바이트 성공을 금지한다.
- 기존 공개 자막 명령과 SRT 결과를 유지한다.

## Task 1 — 버전 JSONL 계약과 상태 저장소

- `ProjectRevision`, `CutCandidate`, `EditPlan`, `CutResult`, `CaptionRequest`,
  `CaptionResult`의 버전 계약을 만든다.
- `received → analyzing → review_required → roughcut_imported →
  caption_requested → caption_ready` 전이만 허용한다.
- 입력 XML 해시가 다른 승인·계획·자막 요청은 거부한다.
- JSONL 직렬화는 결정적이어야 한다.

## Task 2 — 보수적 후보·사람 승인·러프컷

- 내부 무음 2초 이상, 양쪽 0.3초 호흡 여유, 프레임 격자 정렬을 구현한다.
- 잠긴·분석 실패·지원하지 않는 인스턴스는 후보를 만들지 않는다.
- 고유 발화를 포함하거나 비전이 연속 동작을 찾은 후보는 취소한다.
- 신뢰도 0.90 이상만 기본 선택하지만 실제 적용은 승인된 ID만 한다.
- 새 이름은 `AI Rough Cut · <원본 이름> · <리비전>`이며 컷 marker에 후보 ID와
  사유를 남긴다.

## Task 3 — 단계형 CLI와 최종 렌더 자막

- 진단용 `project run`은 숨기고 `receive`, `analyze`, `review`, `roughcut`,
  `caption` 단계로 분리한다.
- 분석 전에 비용 추정을 출력하고 온라인 분석은 명시적 확인 뒤에만 시작한다.
- caption은 48 kHz PCM과 최종 FCPXML을 입력으로 받고 해당 리비전을 명시적으로
  승인한 것으로 기록한다.
- 발화가 없으면 SRT를 만들지 않고, 실패·불완전 coverage·0바이트 SRT는 실패한다.
- 성공 파일명은 `<프로젝트명>.<리비전>.ko.srt`다.

## Task 4 — macOS 컨테이너·Workflow Extension 골격

- Swift 컨테이너와 Workflow Extension의 최소 빌드 가능한 골격을 만든다.
- 드롭 FCPXML, 수동 파일 선택 fallback, App Group 작업 공간, Keychain 키 저장,
  security-scoped bookmark, Apple event import 요청을 경계별 서비스로 둔다.
- 사용자 지정 공유 대상이 PCM 오디오와 FCPXML을 함께 작업 공간에 복사해
  `CaptionRequest`를 생성하도록 한다.
- Xcode 개발 서명까지만 대상으로 하고 공증·자동 업데이트는 제외한다.

## Task 5 — 회귀·통합 검증과 실기 인수 안내

- 프레임레이트·리비전 충돌·승인 누락·분석 실패·결정성·빈 SRT 회귀를 자동 시험한다.
- 기존 전체 검사와 오프라인 비전 재생 E2E를 실행한다.
- 실제 Final Cut Pro가 필요한 드래그·Apple event·공유 대상·SRT import 항목은
  `PENDING_HUMAN` 체크리스트로 남기고 자동 통과라고 주장하지 않는다.

## 인터페이스 사전 점검

- Task 1의 JSONL 리비전은 Task 2의 승인 입력과 Task 3/4의 앱 경계에서 동일한
  `input_sha256`와 프로토콜 버전을 사용한다.
- Task 2의 `CutResult.output_fcpxml`은 Task 3의 roughcut 산출이며, 최종 caption은
  이 파일을 재사용하지 않고 공유 대상이 보낸 최종 FCPXML을 새 리비전으로 계산한다.
- Task 3의 작업 디렉터리 레이아웃을 Task 4 Swift 앱도 그대로 사용한다.

