"""원본 영상 브라우저 v2 — 파생 계층의 새 작업공간 계층.

A0(2026-10-02): 계약 선행 — `config.py`의 동결·검증 설정.
A1: `models.py`(데이터 계약)·`workspace.py`(SQLite 정본·미디어 등록)·
`jobs.py`(서버 소유 작업)·`service.py`(공개 서비스).
A2(현재): `ingest.py`(SD/폴더 발견·조사·확정·검증 복사)·`source_order.py`
(자연 정렬·불변 순서 리비전)·`commands.py`(진단 CLI). 분할·분석·최종 자막은
이후 단계다 — `docs/plans/2026-10-02-raw-video-browser-v2.md` §16.

레거시(vlogsrt 최상위·ui)에서 이 아래를 import할 수 있는 곳은 정확히
`ui/workspace_service.py` → `service` 모듈 하나다(docs/PROJECT_CONTRACTS.md §1).
"""
