# 작업대 글꼴 (W-17, D57)

브라우저 작업대가 **이 폴더에서만** 읽는 글꼴이다. 실행 중에 CDN을 부르지 않는다 — 앱은
`/vl-fonts/<파일 이름>` 경로로 아래 다섯 파일만 내준다(`ui/design_tokens.py`의 허용 목록,
`ui/workspace_service.py`의 라우트). 글꼴이 없거나 못 읽으면 `Apple SD Gothic Neo` →
시스템 글꼴로 넘어간다(`font-display: swap`).

- 라이선스: SIL Open Font License 1.1 (`OFL.txt`, 두 꾸러미의 `LICENSE.txt`는 바이트가 같다).
  예약 글꼴 이름 "Plex" — 파일을 고치지 않고 그대로 싣는다.
- 출처: IBM 공식 npm 꾸러미를 jsDelivr에서 정확한 판으로 받았다(2026-10-10). npm 설치는
  하지 않았다 — 꾸러미의 `postinstall`(IBM 원격 측정)은 실행되지 않는다. 파이썬 의존성도 아니다.
- 각 파일은 받은 뒤 woff2 표지(`wOF2`)를 확인했다.

| 파일 | 글꼴 · 굵기 | 출처 URL | 바이트 | sha256 |
|---|---|---|---|---|
| `IBMPlexSansKR-Regular.woff2` | IBM Plex Sans KR 400 | https://cdn.jsdelivr.net/npm/@ibm/plex-sans-kr@1.1.0/fonts/complete/woff2/hinted/IBMPlexSansKR-Regular.woff2 | 438540 | `055a35664c3c3965161c92292504c5633ba9604ec904edc1ed799bf2a436276d` |
| `IBMPlexSansKR-Medium.woff2` | IBM Plex Sans KR 500 | https://cdn.jsdelivr.net/npm/@ibm/plex-sans-kr@1.1.0/fonts/complete/woff2/hinted/IBMPlexSansKR-Medium.woff2 | 439816 | `c00dcd8a9c32a6b6ab8c6b3119e68b3d7aa6eba6f4441d5493019cc019dcdd1e` |
| `IBMPlexSansKR-SemiBold.woff2` | IBM Plex Sans KR 600 | https://cdn.jsdelivr.net/npm/@ibm/plex-sans-kr@1.1.0/fonts/complete/woff2/hinted/IBMPlexSansKR-SemiBold.woff2 | 434484 | `5ad7db28ba74d59fe14c260205c62ddb701320f4f098d8a45ef2757bebc29666` |
| `IBMPlexMono-Regular.woff2` | IBM Plex Mono 400 | https://cdn.jsdelivr.net/npm/@ibm/plex-mono@2.5.0/fonts/complete/woff2/IBMPlexMono-Regular.woff2 | 49248 | `ba204497f16b6d334cee9d1e963a831b73e3a56e1d6300a8489d18df7214b350` |
| `IBMPlexMono-Medium.woff2` | IBM Plex Mono 500 | https://cdn.jsdelivr.net/npm/@ibm/plex-mono@2.5.0/fonts/complete/woff2/IBMPlexMono-Medium.woff2 | 50400 | `33faf307fa6031fb4062276d7320a6d632de890cbb347576fd80cfa01077bc25` |
| `OFL.txt` | 라이선스 원문 | https://cdn.jsdelivr.net/npm/@ibm/plex-sans-kr@1.1.0/LICENSE.txt | 4456 | `7e6b2818edbd8f6a01ae80641cc8f16a51080d08fb4e532be3a0b6f74adb07da` |

다섯 글꼴 합계 1,412,488바이트(약 1.35 MiB). 판: `@ibm/plex-sans-kr@1.1.0`(2024-11-11 공개),
`@ibm/plex-mono@2.5.0`(2026-06-11 공개). 한글 파일은 꾸러미의 "complete · hinted" woff2 그대로다 —
나눔(subset)을 지어 만들지 않았다.

판을 바꾸면 파일·표·`tests/raw/test_workbench_theme.py`의 해시 대조를 함께 고친다.
