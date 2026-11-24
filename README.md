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

작업대 1단계는 원본 영상의 소리를 **Gemini 3.5 Transcribe**에 보내 글자와 단어 시각을 한 번에 받는다(D92·D104). 자막의
시각은 그 단어 시각 그대로이고(조각 오프셋만 더한다), 프로그램은 엔진이 준 단어 경계에서 줄을 끊어 읽기 좋게 조판할
뿐이다(`word_cues.py`).

**whisper-1·gpt-transcribe는 2026-11-16에 은퇴했다(D163).** 옛 CLI(`vlogsrt run/record/eval`)·옛 엔진(되풀이 재전사·글자 교정·
교차 검증)은 지웠다. 이미 whisper로 들은 영상의 자막은 그대로 읽고 고치고 내보낼 수 있고, 다시 들으면 Gemini로 듣는다.

남은 CLI는 진단용이다.

```bash
vlogsrt media --help                                     # 작업대 진단(가져오기·분석 재생·시각 확인 등)
vlogsrt project sync-check --media <video> --srt <srt>   # SRT 시각을 말소리와 대조 (표시만)
```

---

## 모듈

| 파일 | 하는 일 |
|---|---|
| `env.py` | 의존성 없는 `.env` 파서, 오프라인 판정 |
| `config.py` | **모든 임계값.** 코드에 숫자를 박지 않는다 |
| `hangul.py` | 비교용 텍스트 정규화 |
| `preprocess.py` | **유일한 ffmpeg 경계.** 추출·진폭 측정·미리보기·렌더 |
| `transcribe.py` | **유일한 API 문.** Gemini(시간 엔진·의미 분석)·Claude·Luna — 승인 문·관찰·취소 |
| `polish.py` | 영화 자막 관행으로 **문장부호만** 다듬는다. 글자는 못 지운다 (순수 함수) |
| `output.py` | SRT 직렬화 |
| `cli.py` | 진단 커맨드(`project` · `media`) |
| `ui/workspace_service.py` | 작업대 서버 |
| `ui/app.py` | 작업대 진입점 — 서버를 띄우고 기본 브라우저를 연다(`--no-browser` · `--port` · `--offline`) |
| `vlogsrt/project/raw/` | 폴더 작업공간 · 분석 · 등급 · 타임라인 · 렌더 · 번인 |
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
