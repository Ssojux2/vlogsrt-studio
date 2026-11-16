# vlogsrt — 브이로그 작업대

촬영한 영상 폴더를 넣으면 **쓸 만한 부분만 골라 이어붙인 초안 영상과 자막**을 만들어 준다.
편집은 Final Cut · Premiere · CapCut에서 그 초안부터 시작하면 된다.

1. **폴더 불러오기** — 촬영 영상이 든 폴더 하나를 고른다. 원본은 복사하지도 바꾸지도 않고, 촬영 시각 순으로 늘어선다.
2. **영상별 AI 분석** — **사람이 승인 단추를 누른 것만** 분석한다(견적 없이 확인 한 번, D110). 누르지 않으면 돈이 들지 않는다.
3. **추천 · 보통 · 비추천** — 분석 결과로 구간마다 등급이 붙는다(초록 · 노랑 · 빨강).
4. **기준과 범위 고치기** — 추천 기준을 폴더(작업공간)마다 바꾸고, 구간을 직접 추천·보통·제외로 표시한다. 사람이 정한 것이 자동 등급을 이긴다.
5. **내보내기** — **추천 부분만 이어붙인 초안 MP4 하나**와 그 타임라인에 맞춘 **SRT 자막**, 무엇을 넣고 뺐는지 적은 **보고서**가 나온다.
   확인하지 않은 비추천 구간도 막지 않고 내보내되, 빠진 구간은 모두 보고서와 화면 경고에 남긴다.
6. **영어 SRT로 변환** — 한국어 SRT 하나(비워 두면 방금 낸 `draft.srt`, 다른 곳에서 고친 SRT도 된다)를 Gemini로 옮겨
   그 옆에 같은 시각의 **영어 SRT**(`<이름>.en.srt`)를 쓴다. 같은 글은 다시 사지 않고, 같은 이름의 다른 파일은 덮지 않는다.

---

## 빠른 시작

### 준비물

| | macOS | Windows | Linux(예: Ubuntu) |
|---|---|---|---|
| Python 3.11 이상 | `brew install python@3.12` | `winget install Python.Python.3.12` | `sudo apt install python3.12 python3.12-venv` |
| ffmpeg · ffprobe | `brew install ffmpeg` | `winget install Gyan.FFmpeg` | `sudo apt install ffmpeg` |
| 자막 입히기(번인)용 libass ffmpeg — 선택 | `brew install ffmpeg-full` | `winget install Gyan.FFmpeg`(이미 든다) | 배포판 `ffmpeg`(대개 이미 든다) |
| 「찾아보기」 선택 창 — 선택 | 필요 없음 | python.org 설치의 tcl/tk | `sudo apt install python3-tk` |

Homebrew 기본 `ffmpeg`에는 libass가 없어 「자막 입히기」만 쓰지 못한다. `ffmpeg-full`은 PATH를 건드리지 않고
자막 입히기에만 쓰인다(D116).

**API 키**: 프로젝트 폴더의 `.env.example`을 복사해 `.env`를 만들고 키를 채운다
(`OPENAI_API_KEY` — 자막, `GEMINI_API_KEY` — 영상 분석 기본 모델, Claude를 고를 때만 `ANTHROPIC_API_KEY`).
`.env`는 이 컴퓨터에만 두고 아무에게도 보내지 않는다.

### 실행

- **Mac**: `실행.command`를 더블클릭한다. 처음 한 번은 「확인되지 않은 개발자」 경고가 뜰 수 있다 —
  파일을 **오른쪽 클릭 → 열기 → 열기**로 한 번 열면 다음부터는 더블클릭으로 열린다.
- **Windows**: `실행.bat`을 더블클릭한다. 「Windows의 PC 보호」가 뜨면 **추가 정보 → 실행**.
- **Linux**: 터미널에서 프로젝트 폴더로 가서 `./실행.sh`(W-84). 「찾아보기」 창이 없으면 경로를 직접 입력한다.

세 운영체제 모두 같은 확인(`scripts/launch_check.py`)을 같은 차례로 거친다. 저장소의 CI가 macOS·Windows·Linux에서
오프라인 검사를 돈다 — 지금은 macOS만 막는 게이트이고 Windows·Linux는 결과를 보고한다(`docs/plans/2026-11-16-cross-platform.md`).

처음 실행하면 실행 환경(`.venv`)을 만들고 필요한 꾸러미를 깐다 — 인터넷이 필요하고 몇 분 걸린다.
그다음부터는 바로 뜨고, 프로그램이 업데이트되어 `pyproject.toml`이 바뀌면 그때만 다시 깐다.
준비가 끝나면 브라우저에 작업대(`http://127.0.0.1:7860`)가 열린다. 창(터미널)을 닫거나 **Ctrl+C**로 끝낸다.

### 파일이 생기는 자리

원본 폴더 안의 `_vlogsrt/` 폴더 하나에 모두 모인다. 원본 파일은 그대로다.

| 자리 | 무엇 |
|---|---|
| `<폴더>/_vlogsrt/cache/` | 분석용 프록시(4K 원본만 960×540으로 줄인 사본) · 프레임 캐시 |
| `<폴더>/_vlogsrt/previews/` | 브라우저에서 보는 미리보기 |
| `<폴더>/_vlogsrt/export/<시각>/` | 내보낸 초안 MP4 · SRT · 보고서 (영어로 옮기면 `draft.en.srt`가 그 옆에) |

SD 카드처럼 **쓸 수 없는 폴더**면 그 옆에 `<폴더>-vlogsrt/`를 만들어 쓴다. 이 폴더를 지워도 원본은 그대로지만,
돈을 내고 받은 분석 결과도 그 안의 작업공간 DB에 있으므로 함께 사라진다.

### Windows 첫 실행 확인 목록

- **ffmpeg가 PATH에 있는가** — `winget install Gyan.FFmpeg` 뒤에는 **새 창**을 열어야 PATH가 읽힌다.
  새 명령 프롬프트에서 `ffmpeg -version`이 나오면 된다. 실행 파일도 못 찾으면 그렇게 알려 주고 멈춘다.
- **긴 경로 켜기(LongPathsEnabled)** — 캐시 경로가 260자를 넘을 수 있다. 관리자 PowerShell에서
  `New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force`
  또는 그룹 정책 편집기(`gpedit.msc`) → 컴퓨터 구성 → 관리 템플릿 → 시스템 → 파일 시스템 → **Win32 긴 경로 사용**을 켜고 다시 로그인한다.
- **exFAT SD 카드도 된다** — 다만 카드에서 바로 읽으면 느리다. 로컬 디스크로 복사해 그 폴더를 불러오는 편이 빠르다.
- **Windows Defender** — 처음 폴더를 훑을 때 실시간 검사 때문에 느릴 수 있다. 두 번째부터는 빨라진다.
- 한글 안내가 깨져 보이면 같은 줄의 영어 안내를 읽으면 된다(실행 파일은 두 언어로 적는다).

### 문제가 생기면

| 증상 | 할 일 |
|---|---|
| `Python 3.11 이상이 필요합니다` | 위 표의 명령으로 Python을 깔고 **새 창**에서 다시 실행한다 |
| `ffmpeg가 필요합니다` | 위 표의 명령으로 ffmpeg를 깔고 **새 창**에서 다시 실행한다 |
| `.venv의 Python이 3.11보다 오래됐습니다` | 프로젝트 폴더의 `.venv` 폴더를 지우고 다시 실행한다 |
| `꾸러미 설치에 실패했습니다` | 인터넷 연결을 확인하고 다시 실행한다. 계속되면 `.venv`를 지우고 다시 |
| `7860 포트가 사용 중입니다` | 작업대가 이미 떠 있다 — 열린 창을 찾거나 브라우저에서 `http://127.0.0.1:7860`을 연다 |
| 브라우저가 안 열린다 | 창에 찍힌 주소(`http://127.0.0.1:…`)를 브라우저에 붙여 넣는다 |
| 분석이 시작되지 않는다 | `.env`에 키가 있는지 확인한다. **승인** 단추를 눌러야 분석이 돈다 |
| 돈이 드는 호출 없이 화면만 보고 싶다 | 터미널에서 `./실행.command --offline`(Windows는 `실행.bat --offline`, Linux는 `./실행.sh --offline`) — 이 실행의 유료 호출을 막는다 |
| 내 원본 없이 화면만 둘러보고 싶다 | `python scripts/preview_workbench.py` — 합성 원본 셋으로 오프라인 작업대를 7871에 띄운다(무료) |

> 이전 Final Cut 라이브러리 흐름(Final Cut으로 보내기, D56)도 설정 서랍에 남아 있다.
> 그 절차는 `docs/raw-video-workflow.md`에 있다.

---

## 자막 엔진

작업대 아래에는 브이로그 영상에서 SRT 자막을 만드는 엔진이 있다.

**`whisper-1`이 자막의 시각과 글자를 모두 만들고, 시각은 원본 오디오 타임라인
그대로 들어간다.** 세그먼트 시각에 조각 오프셋을 더하는 것이 시각에 가해지는 유일한
계산이다. 이 프로그램은 그것을 읽기 좋은 SRT로 조판할 뿐이다.

`gpt-transcribe`가 같은 오디오를 한 번 더 받아 대조하지만 **표시만 한다.** 자막을
고치지도 빼지도 못하므로 켜 두는 것이 기본이다 — 의심스러운 자리를 로그로 알려줄 뿐,
판단은 사람이 눈과 귀로 한다 (설계 배경: `docs/DECISIONS.md` D25).

`.env`의 `VLOGSRT_OFFLINE` 기본값은 `1`이다. **CLI와 테스트는 아무것도 하지 않으면 API를 부르지 않는다.**
작업대는 사람이 띄우는 앱이라 온라인으로 뜨고, 비용 문은 사람이 누르는 승인이다(D44 · D110 — 견적 없이 확인 한 번).

## 쓰기 — CLI

```bash
# 픽스처 녹화 — 유료. 회차당 한 번만.
vlogsrt record golden/ep01.mp4

# 전체 파이프라인 (픽스처 사용, 무료)
vlogsrt run golden/ep01.mp4 --offline -o out/

# 길이와 조각 수만 확인 — API 없이, 유료 호출 전에
vlogsrt probe golden/ep01.mp4

# 교차 확인을 끄고 절반 비용으로
vlogsrt run golden/ep01.mp4 --no-verify

# 되풀이 재전사를 끈다 (되풀이가 자막에 그대로 남는다)
vlogsrt run golden/ep01.mp4 --no-repair

# 조판 정리를 끈다 (온점과 부호가 그대로 남는다)
vlogsrt run golden/ep01.mp4 --no-polish

# 조판만 다시 — API 없이 0.1초
vlogsrt redo out/trace.json --max-chars 14

# 품질 측정
vlogsrt eval out/ep01.srt golden/ep01.srt
make eval                      # 골든셋 전체 → docs/DASHBOARD.md
```

출력물 (`-o` 디렉토리):

| 파일 | 쓰임 |
|---|---|
| `<회차>.srt` | 최종 자막 |
| `trace.json` | 골격과 큐 스냅샷. `redo`가 이것만 읽는다 |

`output.srt`는 같은 내용의 고정 이름 별칭이다.

---

## 파이프라인

```
전처리   ffmpeg → 16kHz 모노 mp3 (무가공)
분할     바이트·초 한도 중 작은 쪽으로 조각 수 → 목표 경계 ±5초에서 가장 조용한 지점
         ffmpeg 세그먼트 분할 1회 (-c copy). 오프셋은 ffmpeg가 알려준 실제값
전사     조각마다 whisper-1 (verbose_json, prompt 없음)
시각     segment.start + chunk.offset ← 원본 오디오 시각 그대로. 그게 전부다
재전사   되풀이 구간을 조각 한도 안으로 쪼개 다시 묻는다 → 회복하면 갈아끼운다 (기본 켜짐)
정제     골격 큐 → 분할 → 병합 → 시간 보정 → 충돌 해소 → 줄바꿈 → SRT 저장
확인     gpt-transcribe로 대조 → 로그 표시만 (SRT는 이미 저장돼 있다)
```

77분 영상이면 조각 26개. 비용은 분당 과금이라 조각 수와 무관하다
(자막 $0.46 + 교차 확인 $0.46). 재전사는 **되풀이 구간에만** 붙는다 — 되풀이가
없으면 0원이고, 26%가 되풀이였던 실측 픽스처 기준으로는 $0.24다. **재전사 비용이
크다는 것은 앞단이 실패하고 있다는 신호다.**

**분할은 자를 자리만 고른다 — 구간을 고르지 않는다.** 조각은 언제나
`[0, duration]`을 빈틈없이 덮으므로 사라지는 구간이 없다. 조용한 지점을 못 찾으면
목표 지점에서 그냥 자른다. `tests/test_invariants.py`가 이걸 기계적으로 잡는다.

### 되풀이를 어떻게 다루나

whisper는 30초 창을 이어 디코딩하면서 **직전 창의 글자를 다음 창의 prompt로 넣는다**
(`condition_on_previous_text`). 그 글자가 되풀이면 다음 창도 되풀이다 — 되풀이가
번지는 통로가 그것 하나뿐이고, 한 번 빠지면 스스로 못 나온다. 실측으로 448초 동안
`seek`가 정확히 30초씩 뛰며 같은 말만 나왔다(D28). 세 겹으로 막는다.

**1. 온도** — 유일하게 원인을 건드리는 수단이다. whisper 레퍼런스 구현은 온도가 0.5를
넘으면 직전 글자를 넘기지 않는다. 그래서 1차 전사를 0.6으로 부른다. **대가는
비결정성이다** — 같은 영상이 실행마다 조금 다르게 나온다.

**2. 조각 길이 상한** — 되풀이는 한 API 호출 안에서만 번지므로 **조각 길이가 곧
되풀이가 자랄 수 있는 최대 길이다.** 한도가 둘이고 작은 쪽이 이긴다.

```toml
[transcribe]
# transcribe_temperature = 0.6   # 0으로 내리지 마라. greedy가 되풀이를 부른다
# chunk_max_seconds = 180        # 길이 상한 = 되풀이 상한. 올리지 마라
# chunk_max_bytes = 24_000_000   # 모델 입력 한도(25MB − 여유)
```

**3. 재전사** — 남은 것을 걷어낸다. 검출 신호가 셋이다.

| 신호 | 무엇을 보나 |
|---|---|
| 메아리 | 연속 세그먼트가 글자 그대로 같다 |
| 압축률 | whisper가 응답에 실어 보내는 `compression_ratio`가 2.4를 넘는다 |
| 낱자 | 세그먼트마다 글자가 하나뿐인 상태가 이어진다. **글자가 달라도 한 런이다** |

압축률이 중요한 이유 — OpenAI API의 자동 온도 상승은 **로그 확률**을 보고 발동하는데,
되풀이는 모델이 확신하면서 무너지는 병리라 로그 확률이 오히려 좋다. 실측에서
`compression_ratio > 2.4`인 세그먼트 676개 중 로그 확률이 나쁜 것은 **0개**였고
`temperature`는 전부 0.0이었다 — 호스팅 폴백이 원리적으로 못 잡는다(D27·D28).

낱자가 필요한 이유 — 앞의 둘이 **1글자에 나란히 눈이 멀었다.** 메아리는 하한
(`loop_min_chars = 2`)이 런을 통째로 버리고, 압축률은 그 30초 창의 텍스트가 29바이트뿐이라
zlib 헤더에 눌려 임계값에 걸터앉는다. 실측에서 88초·44장이 전부 한 글자(`'S'`,`'F'`)였는데
검출이 아무것도 못 짚어 경고 하나 없이 SRT로 나갔다(D29).

짚은 구간은 **조각 한도(120초) 안의 계획 여럿으로 쪼개** 하나씩 다시 자르고,
1차보다 뜨거운 온도(0.8)로 다시 묻는다. `gpt-transcribe`가 "여기 되살릴 말이 있나"를
심판하고, 그렇다고 하면 온도를 올려 한 번 더 산다(1.0). **조각마다 따로 판정하므로
한 되풀이 안에서도 앞은 되살아나고 뒤는 못 되살아날 수 있다.**

회복 못 하면 **길이로 갈린다**: 30초 넘는 되풀이는 첫 장만 남기고 그 구간을 비우고,
그보다 짧으면 1차 전사를 그대로 둔다 — 사진 찍으며 아홉 번 말한 '치즈'까지 지울
이유는 없다. 어느 쪽이든 `WARN`으로 뜨고 `trace.json`의 `repairs[]`에 남는다.
마지막 줄의 `남은 되풀이 N초`가 갈아끼운 뒤에도 남은 것을 드러낸다.

끄려면 `vlogsrt run <video> --no-repair`. 그러면 되풀이가 그대로 나온다.

### 조판 정리 (기본으로 켜져 있다)

자막이 조판되기 직전에 문장부호를 영화 자막 관행으로 다듬는다(D30). 문장 끝 온점을
지우고(`1.5초` 같은 숫자 사이는 예외), `...`을 `…` 한 글자로 모으고, `!!!`처럼 겹친
부호를 하나로 줄이고, 부호 앞뒤 공백과 큐 끝 쉼표를 정리한다. 물음표·느낌표·문장
중간 쉼표는 그대로 둔다 — 뜻이 다르다.

**글자는 한 자도 지우지 않는다.** 지우는 것은 설정에 든 부호뿐이고, 정리 결과가 비면
원문을 그대로 되돌린다. 시각도 안 건드린다. 골격에는 부호가 살아 있는 원문이 남으므로
`vlogsrt redo <trace> --no-polish`로 언제든 부호 있는 판본을 다시 뽑을 수 있다.

끄려면 `vlogsrt run <video> --no-polish`.

`vlogsrt probe <video>`가 API를 부르지 않고 길이·조각 수를 보여준다(비용은 재지 않는다, D110).

---

## 모듈

| 파일 | 하는 일 |
|---|---|
| `env.py` | 의존성 없는 `.env` 파서, 오프라인 판정 |
| `models.py` | `Segment` `Cue` `Chunk` `Trace` — 전부 frozen |
| `config.py` | **모든 임계값.** 코드에 숫자를 박지 않는다 |
| `hangul.py` | 비교용 텍스트 정규화 |
| `cutpoints.py` | **자를 자리만 고른다.** 조각 수·목표 경계·가장 조용한 지점 (순수 함수) |
| `preprocess.py` | **유일한 ffmpeg 경계.** 추출·진폭 측정·조각 나누기 |
| `repetition.py` | 되풀이 검출. **자리만 짚는다** (순수 함수) |
| `repair.py` | 되풀이 구간만 다시 묻고 갈아끼운다. **시각을 바꾸는 유일한 계층** |
| `verify.py` | 두 번째 모델과 대조해 **표시만** 한다. 출력을 못 건드린다 |
| `transcribe.py` | **유일한 네트워크 경계.** `openai`를 import하는 파일은 여기뿐 |
| `polish.py` | 영화 자막 관행으로 **문장부호만** 다듬는다. 글자는 못 지운다 (순수 함수) |
| `refine.py` `layout.py` | 정제·조판 |
| `output.py` | SRT 직렬화 |
| `evaluate.py` | 품질 지표, 대시보드 표 |
| `pipeline.py` `cli.py` | 조립과 커맨드 |
| `ui/workspace_service.py` `ui/service.py` | 작업대 서버 · UI 로직 |
| `ui/app.py` | 작업대 진입점 — 서버를 띄우고 기본 브라우저를 연다(`--no-browser` · `--port` · `--offline`) |
| `vlogsrt/project/raw/` | 폴더 작업공간 · 분석 · 등급 · 렌더 계획 |
| `scripts/launch_check.py` | 실행 파일 공통 확인(설치 표지 · 꾸러미 · ffmpeg). 표준 라이브러리만 |

---

## 개발

```bash
make venv       # .venv + 의존성(.[dev,ui,project]) — 실행 파일과 같은 꾸러미에 개발 도구를 더한다
make check      # lint + types + test + invariant. 커밋 전 필수
make test
make invariant  # CLAUDE.md 불변 규칙을 기계적으로 검사
make types-win  # Windows 분기 정적 검사(mypy --platform win32). check에는 들지 않는다
make ui         # 작업대를 터미널에서 띄운다(실행 파일과 같다, 설치 확인은 하지 않는다)
```

`make venv`는 실행 파일의 설치 표지(`.venv/.vlogsrt-install-stamp`)도 남겨 `실행.command`가 다시 깔지 않는다.
두 실행 파일의 공통 판단(설치 표지 · 꾸러미 · ffmpeg)은 `scripts/launch_check.py`(표준 라이브러리만)에 있다.

테스트는 **네트워크를 소켓 수준에서 차단한다**(`tests/conftest.py`).
합성 픽스처(`tests/fixtures/synthetic/`)만으로 전 과정이 돈다.

실제 영상으로 돌려보는 절차는 [docs/RUNBOOK.md](docs/RUNBOOK.md)에 있다.
작업 규칙은 `CLAUDE.md`, 태스크 보드는 `TASKS.md`, 설계 판단은 `docs/DECISIONS.md`에 있다.
