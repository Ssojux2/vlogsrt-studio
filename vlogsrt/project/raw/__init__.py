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
A4-F1(현재): `analysis_approval.py`(1단계 전사 승인·실제 호출 주입 경계)와 두
단계 실제 호출 — 1단계(전사) 승인 → 불변 전사 기록(`asr_phase.json`) → 의미
승인 대기 → 2단계 승인(오디오는 라이브러리 승인, 무성은 시각 승인)이 실측
지문을 검증받아 Luna만 재개한다(전사 0재호출). 검토 UI(A5)·package 확정(A6)·
최종 자막(A7)은 이후 단계다 — `docs/plans/2026-10-02-raw-video-browser-v2.md` §16.

레거시(vlogsrt 최상위·ui)에서 이 아래를 import할 수 있는 곳은 정확히
`ui/workspace_service.py` → `service` 모듈 하나다(docs/PROJECT_CONTRACTS.md §1).
"""
