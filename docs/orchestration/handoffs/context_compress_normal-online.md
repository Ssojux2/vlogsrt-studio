# context_compress: normal-online — SOURCE FIX VERIFIED, USER APP RESTART REQUIRED
갱신: 2026-10-08 | 진행: 일반 앱 기본온라인 수정/표적 인수 완료

## 실제 대상/원인
- 일반앱 http://127.0.0.1:7860, 인스턴스PID56176.
- workspace /Users/ssojux2/Movies/test_dir/260810_wangbang-vlogsrt (이 세션에서 읽기전용).
- 선택0009 64.731667초, source_33ac7a594019c477, job000027 offline검사 실패, 분석리비전0.
- 이전8791의0014 성공과 별개. 일반 실행은 --online 미지정이면 .env offline=True가 여전히 이겼음.

## 완료
- ui/app.py: GUI 기본online, 명시--offline 차단. dotenv 로딩 전 프로세스 모드 확정.
- 실행.command 수정 불필요: 인자 없이 ui.app 실행하므로 자동으로 새 기본값 적용.
- raw/browser_runtime.py + service.py: 비밀 없는 실제 프로세스 모드 조회 façade.
- ui/workspace_service.py: 모드 표시, 이전offline실패 선택은 로컬견적만 복구(유료자동재개0).
- tests/raw/test_ui_entry.py + test_live_app_bootstrap.py: argv/dotenv우선순위·파일불변·게이트검증.
- 표적24passed(22.63s), ruff/invariant통과, --help 실제출력 확인.
- 제품4+테스트2=6파일, preflight≤7코드파일/≤3모듈 안.
- .env/.env.example/키/원본/실APIfixture 변경 없음. 커밋/스테이징/푸시 없음.

## 다음 행동
1. 사용자 기존 실행 터미널에서 Ctrl+C로 일반7860 서버 종료.
2. /Users/ssojux2/Working/vlogsrt/실행.command 다시 더블클릭(추가 --online 불필요).
3. '온라인 모드'와 선택영상 견적 확인 후 앱에서 해당 범위 승인. 전체라이브러리 자동분석 없음.

## 검증 한계/권한
- 현세션 workspace-write/network-restricted/approval-never.
- ps/프로세스신호 PermissionError, Playwright approval-never거절, localhost curl exit7.
- 우회/권한상승/대체브라우저/새임시서버 안 함. 실행중7860 교체했다고 주장하지 않음.
- 마지막 read-only DB: 로컬preview 작업2개 running, 실제분석리비전0.
- High Agency activation 요청, trusted-hook UNVERIFIED, served모델 신원은 미확인.

## 증거
- docs/reports/2026-09-27-high-agency-implementation-feedback.md: 일반 실행 앱의 온라인 기본값 수정 절.
- /tmp/vlogsrt-normal-online-final.log, /tmp/vlogsrt-normal-online-invariants.log
