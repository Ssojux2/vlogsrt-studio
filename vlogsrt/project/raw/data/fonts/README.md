# 자막 입히기 글꼴 (W-65, D116)

「자막 입혀 내보내기」가 libass에 **이 폴더의 글꼴만** 건넨다(`subtitles=…:fontsdir=fonts`).
시스템 글꼴을 찾지 않는다 — libass가 같은 이름을 못 찾으면 다른 글꼴로 바꿔 그리므로, 파일 이름과
`BurnSettings.font_name`이 함께 움직여야 한다. 강의자료(`bp_music_lecture` 등)가 쓰는 Pretendard다.

- 라이선스: SIL Open Font License 1.1 (`OFL-Pretendard.txt`), 예약 글꼴 이름 "Pretendard".
  파일을 고치지 않고 그대로 싣는다. 파이썬 의존성이 아니다(규칙 10).
- 출처: 이 기계에 이미 있던 Pretendard 1.3.9 npm 꾸러미(`pretendard@1.3.9`, `dist/public/static/`)의
  OTF를 그대로 옮겼다(2026-11-12). 그 꾸러미에는 라이선스 파일이 없어, 라이선스 원문은 같은 기계의 다른
  프로젝트가 싣고 있던 `LICENSE-Pretendard.txt`(Pretendard 저작권 줄 + SIL OFL 1.1 전문)를 옮겼다 —
  공식 배포본과 바이트 대조는 하지 않았다.
- 실측: libass 0.17.5(Homebrew `ffmpeg-full` 9.0.2)가
  `fontselect: (Pretendard SemiBold, 400, 0) -> Pretendard-SemiBold`로 이 파일을 골랐다.

| 파일 | 글꼴 · 굵기 | 바이트 | sha256 |
|---|---|---|---|
| `Pretendard-SemiBold.otf` | Pretendard 600 | 1583704 | `c89bc43027dc7cde5726e96223376f8eec09302b2fc1f8147fd5b57cfc376118` |
| `OFL-Pretendard.txt` | 라이선스 원문 | — | `b04538c9abec39a3db75108cf0af0fd9c77032fe8aa2cf38345b4d250e98e38e` |

판을 바꾸면 파일·표·`tests/raw/test_burn_export.py`의 해시 대조를 함께 고친다.
