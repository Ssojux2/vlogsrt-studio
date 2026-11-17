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

## W-84a 첫 묶음(2026-11-17, main `74de4ac`의 Windows CI — 239 failed · 6061 passed)

ubuntu·macOS는 이미 초록이다. Windows 239개를 갈래마다 고쳤다 — 다음 CI(`b15247e`)에서 20개가 남았고 둘째 묶음이 이어 고쳤다(미디어 맵의 `C:\…`를 URI로 읽던 제품 버그 · 고정 sha 문서의 Windows URI · FIFO · ctypes 핸들 형 · 끝 공백 이름).

**제품 버그(Windows에서만 드러났다)**

- `fcp.py` 칸마다 링크 풀기 — 바탕 `Path`는 Windows에서도 `PureWindowsPath`의 하위 형이 아니어서 링크 표기를 POSIX로 읽었고,
  `\\?\` 앞머리가 칸(`?`)이 되어 **모든 외부 링크·연결 지점이 「링크 대상이 없다」로 끊겼다**(Final Cut 라이브러리 외부 원본 · CapCut ·
  참조 가져오기). `type(link)`로 고른다.
- `media.py` file URI 풀기 — `file:///C:/…`의 경로 칸 `/C:/…`가 Windows에서 루트 상대라 「상대 경로」로 거부됐다. 앞 `/`만 벗긴다.
- `xml_validation.py` — Windows xmllint가 명령 줄을 ANSI로 받아 한글 경로가 `??`가 됐다. Windows에서만 입력 칸을 ASCII
  `file:///` URI로 넘긴다(POSIX argv는 그대로 — 불변 검사 통과).
- `scripts/build_engine.py` — `--add-data` 「원본:자리」를 왼쪽 `:`에서 갈라 `D:\…`가 `D`가 됐다. 오른쪽에서 가른다.

**시험의 POSIX 가정** — 가짜 절대 경로(`os.path.abspath`) · `as_uri()` · `as_posix()` 비교 · 권한 비트(Windows는 0o666) ·
`os.getuid`/`getpriority` 없음(우선순위는 `GetPriorityClass`로 잰다) · `/dev/fd`(핸들 수로 잰다) · 폴더 잠금 파일
(`.vlogsrt-folder.lock`) · 열린 파일을 바꿔치거나 지울 수 없음(그 경쟁이 생기지 않음을 확인한다) · 이름에 못 쓰는 글자(`:` · `*` ·
끝 공백) · 단조 시계 눈금 15.6 ms · `write_text`의 CRLF.

**알려진 한계(고치지 않았다)** — Windows `st_ctime`은 만든 시각이라 같은 크기로 고쳐 쓰고 수정 시각까지 되돌린 원본은 조사 기억
(`PROBE_MEMO`)이 알아채지 못한다. 다른 파일(inode)로 바뀌면 잡는다.
