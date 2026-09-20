# Final Cut Pro 실기 인수표

□ 판정 상태

  ○ 자동 시험 범위

  - Python 엔진, JSONL, Swift 코어의 동작 확인 완료
  - 실제 Final Cut Pro UI와 `.appex` 등록은 `PENDING_HUMAN`임

  ○ 실기 시작 조건

  - Apple Workflow Extension SDK와 Xcode 템플릿 설치 필요
  - 컨테이너 앱과 확장에 같은 App Group 개발 서명 필요

□ 1단계: 프로젝트 받기

  ○ 2분 합성 프로젝트 드롭

  - 패널에 프로젝트를 드롭해 `input.fcpxml` 생성 확인
  - 실패하면 같은 패널의 `FCPXML 직접 선택`으로 수신 확인
  - 증거: 입력 XML SHA-256, Final Cut Pro 버전, 화면 녹화 경로 기록

  ○ 10~20분 실제 프로젝트 드롭

  - 관리 미디어와 외부 미디어 프로젝트를 각각 확인
  - 연결 클립, 전환, 리타임, multicam이 붙은 클립의 잠금 확인
  - 통과 기준: 지원하지 않는 요소의 삭제·평탄화 0건

□ 2단계: 러프컷 보내기

  ○ Open Document 왕복

  - 승인 후보 1건만 고른 뒤 `AI Rough Cut · 원본 이름 · 리비전` 생성 확인
  - 원본 프로젝트 이름, 길이, 클립 수를 전후로 기록
  - 통과 기준: 원본 프로젝트·라이브러리·미디어 변경 0건

  ○ relink와 재생

  - 원본 4K와 proxy를 번갈아 재생 확인
  - 각 컷 지점의 후보 ID·사유 marker 확인
  - 통과 기준: 출력 길이와 `EditPlan.expected_output_duration` 일치

□ 3단계: 사용자 지정 공유 대상

  ○ `AI Captions` 공유

  - 최종 프로젝트에서 48 kHz PCM WAV와 FCPXML 동시 수신 확인
  - `caption-request.jsonl`의 두 경로와 최종 XML SHA-256 확인
  - 통과 기준: 공유 대상의 Create Asset → Open Document 흐름 완료

  ○ 실패 경로

  - WAV 또는 FCPXML 하나만 전달했을 때 실패 표시 확인
  - 전사 실패와 0바이트 SRT가 `caption_ready`로 바뀌지 않는지 확인
  - 발화 없음일 때 SRT가 없고 `발화 없음` 상태인지 확인

□ 4단계: SRT 가져오기

  ○ Final Cut Pro 가져오기

  - `File > Import > Captions` 실행
  - 배치 방식 `Relative to Timeline` 선택
  - 시작·중간·끝 자막의 음성 오차를 각각 기록

  ○ 통과 기준

  - 시작·중간·끝 오차 각각 0.2초 이내
  - 전체 프로젝트에서 자막 이탈 0건
  - 최종 FCPXML SHA-256과 `CaptionResult.final_revision` 일치

□ 5단계: 삭제 안전성

  ○ 사람 청취

  - 승인한 삭제 구간을 원본에서 모두 청취
  - 통과 기준: 고유 발화 손실 0건

  ○ 장편 최종 확인

  - 위 항목 통과 뒤에만 77분 실제 프로젝트 실행
  - 비용, 실행 시간, 취소·재개 결과를 함께 기록

