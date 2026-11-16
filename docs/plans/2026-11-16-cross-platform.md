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
