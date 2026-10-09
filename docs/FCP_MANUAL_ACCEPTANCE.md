# Final Cut Pro 실기 인수표

□ 판정 상태

  ○ 자동 시험 범위

  - Python 2,620건, 불변식 21건, Swift 38건 통과
  - XcodeGen 무차이, unsigned 35건, signed app/appex 검증 통과
  - 설치 경로: `/Applications/FinalCutAgent.app`

  ○ 실기 미확인

  - Final Cut 창의 AX 읽기가 차단되어 패널 화면 확인 불가
  - 근거: Orca Accessibility·화면 캡처 권한은 granted, Final Cut AX 응답만 timeout

  ○ 실기 시작 조건

  - Apple Workflow Extension SDK 1.0.3 설치 완료
  - 컨테이너 앱과 확장의 Team-ID App Group 개발 서명 완료

□ 1단계: 프로젝트 받기

  ○ 2분 합성 프로젝트 드롭

  - 패널에 활성 프로젝트 UID·이름·시작·길이·프레임 길이 표시 확인
  - 패널에 프로젝트를 드롭해 `input.fcpxml` 생성 확인
  - 실패하면 같은 패널의 `FCPXML 직접 선택`으로 수신 확인
  - 같은 이름·다른 UID와 다중 프로젝트 FCPXML 거부 확인
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

□ 전체 타임라인 보내기 (W-16, D56)

  ○ 준비

  - Final Cut 라이브러리를 백업하고 `Original Media`의 파일 수·전체 크기를 기록
  - 작업대에서 「자동 제외 확인」을 누를지 정한다 — 누르면 확인한 제외가 꺼진 채, 안 누르면 켠 채
    `제외 제안(미검토)` 키워드로 간다

  ○ 보내기

  - 「Final Cut으로 보내기」 한 번 — 상태 줄에 원본·스파인 클립·꺼진 클립·넣지 않은 원본 수가 뜬다
  - `review/finalcut/vlogsrt_<UTC>.fcpxml`과 같은 이름의 `.json` 리포트가 생겼는지(`.fcpxml`이 있어야
    끝난 보내기다)

  ○ Final Cut에서 확인

  - 원래 라이브러리에 새 이벤트·프로젝트 `vlogsrt · <작업공간> · <시각>`이 생긴다 — 다른 라이브러리를
    묻거나 새 라이브러리를 만들지 않는다(`library location`이 끝 `/`가 붙은 번들 URI
    `file:///…/260810_wangbang.fcpbundle/`로 들어간다)
  - 중복 미디어·누락(빨간) 아이콘 없음 · `Original Media`의 파일 수·크기가 그대로
  - 18개 원본이 촬영 순 · 0008의 시작 타임코드가 `12:56:29;10`
  - 확정한 제외는 흐리게 보이고 V로 되살아난다 · 타임라인 인덱스 › 태그에서 `제외 확정`·`추천`·`보통`·
    `제외 제안`·`판정 보류`·`미분석`으로 골라진다
  - 브라우저에 키워드 컬렉션(추천·보통·제외 제안·판정 보류)과 초록 구간의 즐겨찾기가 있다 · reject 없음
  - 캡션이 한국어 iTT 역할(`iTT?captionFormat=ITT.ko`)로 들어가고 오디오와 대조해 맞는다
  - 컷 경계에서 영상·소리가 튀지 않는다 · 다시 보내면 새 프로젝트가 생긴다(앞 프로젝트는 그대로)
  - 25p·24p(드롭프레임 아닌) 라이브러리가 있으면 같은 확인을 한 번 더 — 시작 타임코드와 `NDF` 표시

  ○ 통과 기준

  - 원래 라이브러리·`Original Media` 변경 0건(새 이벤트·프로젝트만 더해진다)
  - 시작·중간·끝 캡션의 음성 오차가 각각 0.2초 이내
  - 리포트의 `not_exported`·`library.warnings`가 화면 안내와 같다
