"""원본 영상 브라우저 v2 — 파생 계층의 새 작업공간 계층.

A0(2026-10-02): 계약 선행 — `config.py`의 동결·검증 설정.
A1: `models.py`(데이터 계약)·`workspace.py`(SQLite 정본·미디어 등록)·
`jobs.py`(서버 소유 작업)·`service.py`(공개 서비스).
A2: `ingest.py`(SD/폴더 발견·조사·확정·검증 복사 — 촬영 시각은 유효할 때만
묶는다)·`source_order.py`(자연 정렬·불변 순서 리비전)·`commands.py`(진단 CLI).
A3(현재): `segmentation.py`(결정적 격자 타일링 — 순수 계산)·`runner.py`(순차
추출·검증·게시·등록 작업 본문)·`package.py`(편집용 전달물 — 폴더·jsonl·CSV·
README). 의미 분석(A4)·검토 UI(A5)·package 확정(A6)·최종 자막(A7)은 이후
단계다 — `docs/plans/2026-10-02-raw-video-browser-v2.md` §16.

레거시(vlogsrt 최상위·ui)에서 이 아래를 import할 수 있는 곳은 정확히
`ui/workspace_service.py` → `service` 모듈 하나다(docs/PROJECT_CONTRACTS.md §1).
"""
