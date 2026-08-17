# vlogsrt

브이로그 영상에서 SRT 자막을 만든다. OpenAI 모델 **하나**만 쓴다.

`gpt-4o-transcribe-diarize`가 자막의 **시각과 글자를 모두** 정하고, 이 프로그램은
그것을 읽기 좋은 SRT로 조판한다. 두 전사를 꿰매는 단계가 없어서 **소리와 자막의
동기화가 구조적으로 보장된다**.

검사도, 판정도, 재검증도 하지 않는다. 자막 품질은 사람이 눈과 귀로 판단한다
(설계 배경: `docs/DECISIONS.md` D21).

---

## 설치

```bash
make venv                     # .venv 생성 + 의존성 설치
source .venv/bin/activate
cp .env.example .env          # OPENAI_API_KEY 를 채운다

pip install -e ".[ui]"        # (선택) 웹 UI
```

`ffmpeg`와 `ffprobe`가 PATH에 있어야 한다 (`brew install ffmpeg`).

`.env`의 `VLOGSRT_OFFLINE` 기본값은 `1`이다. **아무것도 하지 않으면 API를 부르지 않는다.**

---

## 쓰기 — 웹 UI

```bash
make ui        # http://127.0.0.1:7860
```

화면은 넷뿐이다: **영상 경로 · 받아쓰기 힌트 · 실행 로그 · SRT.**
`찾아보기`를 누르면 macOS 기본 파일 대화상자가 열리고, 고른 파일의 **실제 경로**가
경로 칸에 들어간다. 경로를 직접 붙여넣어도 된다. **업로드도 복사도 하지 않는다.**

**누를 때마다 그 영상을 처음부터 새로 전사한다.** 지난 실행의 응답도, 오디오 캐시도
보지 않는다. 같은 영상을 두 번 돌리면 두 번 다 API를 부른다 — 낡은 기록이 조용히
섞여 엉뚱한 자막이 나오는 편보다 낫다(D13이 그 사고였다).

**남는 파일은 원본 영상 옆의 SRT 하나뿐이다.** 오디오·조각·`trace.json`은 임시
폴더에서 만들어지고 끝나면 함께 사라진다. `out/`에 아무것도 쌓이지 않는다.

> UI는 `VLOGSRT_OFFLINE` 값과 무관하게 언제나 실제 API를 부른다. 비용 안전장치는
> `VLOGSRT_COST_GUARD_SECONDS`(기본 7200 = 2시간)가 맡는다.

> 대화상자는 서버(=이 컴퓨터)에서 열린다. 브라우저는 보안상 실제 경로를 절대
> 넘겨주지 않기 때문이다(`C:\fakepath\...`). 그래서 macOS 전용이다.

### 받아쓰기 힌트

`힌트` 칸에는 **틀리기 쉬운 고유명사**를 쉼표로 나열하면 된다. 비워도 된다.

```
망원동, 블루펭귄, 아페로, 코나
```

힌트는 자막을 만드는 모델에게 그대로 전달된다. 모델이 이 인자를 거부하면
(계정·모델에 따라 다르다) 경고 한 줄을 찍고 힌트 없이 다시 부른다 — 힌트 하나
때문에 전사 전체가 날아가지 않는다.

> **주의:** `transcribe_model`을 `whisper-1`로 되돌린 경우에는 힌트를 비워라.
> whisper는 `prompt`를 지시가 아니라 **직전 자막 텍스트**로 받아서, 알아듣기
> 힘든 구간을 만나면 그 문장을 그대로 이어 쓴다. 실측 두 번 모두 그렇게 무너졌다:
> `"프랑스 여행 브이로그"` 한 줄이 `'한국 여행 브이로그'` 138회 · 564초를 낳았다(D16).

gradio는 **선택적 extra**이고 `ui/`에만 산다 — `vlogsrt/`의 의존성은 여전히 3개다.

## 쓰기 — CLI

```bash
# 픽스처 녹화 — 유료. 회차당 한 번만.
vlogsrt record golden/ep01.mp4

# 전체 파이프라인 (픽스처 사용, 무료)
vlogsrt run golden/ep01.mp4 --offline -o out/

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
전처리   ffmpeg → 16kHz 모노 mp3 (무가공) → 24MB / 25분 균등 분할
전사     gpt-4o-transcribe-diarize → 오프셋 리베이스 → spine
정제     골격 큐 → 분할 → 병합 → 시간 보정 → 충돌 해소 → 줄바꿈 → SRT 저장
```

큐 시각은 골격에서 정해지고 정제에서만 한도 안(lead_in/out·min_gap)에서 조정된다.
`tests/test_invariants.py`가 이걸 기계적으로 잡는다 — 모든 큐는 `seg_ids`로 원본
세그먼트까지 역추적되고, 그 세그먼트 시각의 한도 안에 있어야 한다.

**모델을 되돌리려면** 코드가 아니라 설정이다:

```toml
[transcribe]
transcribe_model = "whisper-1"
transcribe_format = "verbose_json"
chunking_strategy = ""      # diarize 전용 필수 인자. whisper는 이 인자를 모른다 (D22)
```

`diarized_json`과 `verbose_json` 둘 다 `segments[].{start,end,text}`를 주므로
파서가 같다. 단, whisper로 되돌려도 옛 판정·강등·검증은 돌아오지 않는다.

---

## 모듈

| 파일 | 하는 일 |
|---|---|
| `env.py` | 의존성 없는 `.env` 파서, 오프라인 판정 |
| `models.py` | `Segment` `Cue` `Trace` — 전부 frozen |
| `config.py` | **모든 임계값.** 코드에 숫자를 박지 않는다 |
| `hangul.py` | 비교용 텍스트 정규화 |
| `preprocess.py` | ffmpeg 호출 + 청크 분할 계획(순수 함수) |
| `transcribe.py` | **유일한 네트워크 경계.** `openai`를 import하는 파일은 여기뿐 |
| `refine.py` `layout.py` | 정제·조판 |
| `output.py` | SRT 직렬화 |
| `evaluate.py` | 품질 지표, 대시보드 표 |
| `pipeline.py` `cli.py` | 조립과 커맨드 |
| `ui/service.py` | UI 로직. **gradio를 import하지 않는다** |
| `ui/app.py` | gradio 화면 (경로·힌트·로그·SRT). 패키지 밖이라 의존성 규칙을 건드리지 않는다 |

---

## 개발

```bash
make check      # lint + types + test + invariant. 커밋 전 필수
make test
make invariant  # CLAUDE.md 불변 규칙을 기계적으로 검사
```

테스트는 **네트워크를 소켓 수준에서 차단한다**(`tests/conftest.py`).
합성 픽스처(`tests/fixtures/synthetic/`)만으로 전 과정이 돈다.

실제 영상으로 돌려보는 절차는 [docs/RUNBOOK.md](docs/RUNBOOK.md)에 있다.
작업 규칙은 `CLAUDE.md`, 태스크 보드는 `TASKS.md`, 설계 판단은 `docs/DECISIONS.md`에 있다.
