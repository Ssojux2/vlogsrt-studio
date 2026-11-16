# 세 운영체제 — CI 결과와 남은 일 (W-84, D144)

정본 계획: `docs/plans/2026-11-16-fcp-window.md`. 이 문서는 CI가 보고한 운영체제별 실패와 사람의 첫 실행 확인을 모은다.

## 게이트

| 운영체제 | CI | 실행기 | 상태 |
|---|---|---|---|
| macOS | 막는다 | `실행.command` | 로컬 `make check` 통과 |
| Windows | 보고만(`continue-on-error`) | `실행.bat` | 첫 CI 결과를 아래에 적는다 |
| Linux(ubuntu) | 보고만(`continue-on-error`) | `실행.sh` | 첫 CI 결과를 아래에 적는다 |

다 초록이면 `continue-on-error`를 지워 막는 게이트로 올린다(Windows 먼저). 실패는 skip·xfail하지 않는다 — W-84a…로 나눠 고친다.

## 첫 CI 결과

(PR CI가 끝나면 여기에 운영체제별 실패 목록을 적는다.)

## 사람 첫 실행 확인 🧑

- [ ] Windows: README 「Windows 첫 실행 확인 목록」 — `실행.bat` 더블클릭 → 설치 → 브라우저 작업대 → `--offline`로 폴더 불러오기 하나.
- [ ] Linux: `./실행.sh --offline` → 폴더 불러오기 하나 · 「찾아보기」(python3-tk) · 초안 내보내기 · 자막 입히기(배포판 ffmpeg의 libass).
- [ ] 키 가로채기 측정(`scripts/key_intercept_probe.html`) — mac Chrome·Safari · win Chrome·Edge · linux Chrome·Firefox. 결과는
      `docs/plans/2026-11-16-fcp-window.md` 끝의 측정표에.

## Windows CI 첫 전체 결과(2026-11-16, PR #13 머리 `d3d7958`) — W-84a 할 일

409 failed · 86 errors · 7598 passed. 갈래(대략):

| 갈래 | 수 | 무엇 | 처리 |
|---|---|---|---|
| POSIX 전용 개발 엔진(`macos/scripts/` — `fcntl` · zsh 설치 스크립트) | ≈110 | 시험이 macOS 엔진을 띄운다 | **했다** — 그 시험 모듈 일곱은 POSIX에서만(Linux·macOS는 그대로 돈다) |
| Final Cut 타임라인(`finalcut_*`) | ≈130 | 시험이 `/media/a.mov` 같은 POSIX 절대 경로를 쓰는데 Windows에서는 절대 경로가 아니다(제품의 검사는 맞다) | 시험의 가짜 경로를 운영체제 절대 경로로 |
| 옛 project 계층(`test_export`·`runner_publish`·`test_media`·`runner_audio`) | ≈90 | file URI ↔ 경로(`relative path can't be expressed as a file URI` · `c%3A%5C…` 호스트) | 제품 코드의 URI 변환을 운영체제 경로로 — 실제 버그일 수 있다 |
| project 작업 흐름 `assert 2 == 0` | ≈40 | 아직 모름(경고 · 오류 수?) | 조사 |
| 그 밖 | ≈30 | `os.getuid`(UI 인스턴스 · 임시 폴더) · 권한 비트(`0o640`) · 폴더 잠금 파일이 남음 · `WinError 123`(자막 작업공간 경로) | 하나씩 |
