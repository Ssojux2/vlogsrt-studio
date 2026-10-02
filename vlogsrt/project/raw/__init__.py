"""원본 영상 브라우저 v2 — 파생 계층의 새 작업공간 계층.

A0(2026-10-02): 계약 선행 — `config.py`의 동결·검증 설정.
A1: `models.py`(데이터 계약)·`workspace.py`(SQLite 정본·미디어 등록)·
`jobs.py`(서버 소유 작업)·`service.py`(공개 서비스).
A2: `ingest.py`(SD/폴더 발견·조사·확정·검증 복사 — 촬영 시각은 유효할 때만
묶는다)·`source_order.py`(자연 정렬·불변 순서 리비전)·`commands.py`(진단 CLI).
A3: `segmentation.py`(결정적 격자 타일링 — 순수 계산)·`runner.py`(순차
추출·검증·게시·등록 작업 본문)·`package.py`(편집용 전달물 — 폴더·jsonl·CSV·
README).
A4: `analysis_config.py`·`understanding.py`·`understanding_luna.py`(받아들인
의미 분석 라이브러리 — 공통 파일 시간축 창 계획 포함)·`analysis_store.py`(분석
리비전·원본별 결과·성공 캐시)·`analysis_workflow.py`(재개 가능 작업 본문 — 재생
픽스처·승인 게이트·비디오 격자 조정).
A4-F1: `analysis_approval.py`(1단계 전사 승인·실제 호출 주입 경계)와 두
단계 실제 호출 — 1단계(전사) 승인 → 불변 전사 기록(`asr_phase.json`) → 의미
승인 대기 → 2단계 승인(오디오는 라이브러리 승인, 무성은 시각 승인)이 실측
지문을 검증받아 Luna만 재개한다(전사 0재호출).
A5~A7 통합(현재): `browser_runtime.py`(브라우저 생산 transport — 시작 환경
키만으로 정본 전송 경계를 만들고, 화면 견적에서 승인을 주조한다)과 검토·확정·
자막 화면의 공개 서비스 이음 — W3 `review_workspace`·W2 `caption_workspace`
백엔드를 파사드 handle로만 쓰고(병합 전 빌드는 정직한 오류), 중앙 DB는 v6로
올라가 양쪽 저장소의 덧붙이기 표를 한 트랜잭션으로 붙인다. 추출 캐시 키는 계획
**리비전 신원**까지 묶는다 — 같은 형상의 새 리비전(결정적 → 의미)도 새 package
에서 뽑고 옛 package는 불변이다.
검토(A5)·확정(A6)·최종 자막(A7)의 사업 논리는 각 라이브러리·백엔드 모듈에
산다 — `docs/plans/2026-10-02-raw-video-browser-v2.md` §16.

레거시(vlogsrt 최상위·ui)에서 이 아래를 import할 수 있는 곳은 정확히
`ui/workspace_service.py` → `service` 모듈 하나다(docs/PROJECT_CONTRACTS.md §1).
"""
