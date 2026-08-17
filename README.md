# vlogsrt

브이로그 영상에서 SRT 자막을 만든다. OpenAI 두 모델만 쓴다.

- **골격(spine)**: `whisper-1`의 세그먼트 — 큐의 **시각**을 정한다. 생성 후 수정되지 않는다.
- **정본(canonical)**: `gpt-transcribe`의 텍스트 — 큐의 **글자**를 정한다.

whisper는 시간을 잘 찍고 gpt-transcribe는 글자를 잘 맞힌다. 둘을 각자 잘하는 일에만 쓰고,
경계 매핑으로 이어 붙인다. **타이밍은 whisper에서, 텍스트는 gpt에서** — 이게 전부다.

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

화면은 셋뿐이다: **영상 경로 · 실행 로그 · SRT.**
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

gradio는 **선택적 extra**이고 `ui/`에만 산다 — `vlogsrt/`의 의존성은 여전히 3개다.

## 쓰기 — CLI

```bash
# 픽스처 녹화 — 유료. 회차당 한 번만.
vlogsrt record golden/ep01.mp4

# 전체 파이프라인 (픽스처 사용, 무료)
vlogsrt run golden/ep01.mp4 --offline -o out/

# 정제만 다시 — API 없이 0.1초
vlogsrt redo out/trace.json --max-chars 14

# 품질 측정
vlogsrt eval out/ep01.srt golden/ep01.srt
make eval                      # 골든셋 전체 → docs/DASHBOARD.md
```

출력물 (`-o` 디렉토리):

| 파일 | 쓰임 |
|---|---|
| `<회차>.srt` | 최종 자막 |
| `<회차>.review.srt` | whisper와 gpt가 **갈린 글자만** `[?…]`로 감싼 검토용 |
| `<회차>.report.md` | 우선 확인 목록, 정제 경고, 다음 회차 사전 후보 |
| `trace.json` | 전 과정 스냅샷. `redo`가 이것만 읽는다 |

`output.srt` / `output.review.srt` / `output.report.md`는 같은 내용의 고정 이름 별칭이다.

---

## 파이프라인

```
M1 전처리   ffmpeg → 16kHz 모노 + loudnorm → 무음 검출 → 24MB 청크 분할
M2 전사     whisper-1 (골격 + word 타임스탬프) ∥ gpt-transcribe (정본 텍스트)
M2c 판정    compression_ratio / avg_logprob / no_speech_prob / 무음 교차검증 → verdict
M3 매핑     자모 정규화 → SequenceMatcher(autojunk=False) → 절단점 사상 → 어절 스냅 → 고아 복구
M4 정제     분할 → 병합 → 시간 보정 → 충돌 해소 → 줄바꿈 → 이상치 검사
M5 출력     SRT + review.srt + report.md + trace.json
```

**M3은 텍스트 인덱스만 다룬다.** 큐 시각은 M1~M2에서 정해지고 M4에서만 조정된다.
매핑이 시각을 바꾸면 그건 버그다 (`tests/test_invariants.py`가 이걸 잡는다).

---

## 모듈

| 파일 | 하는 일 |
|---|---|
| `env.py` | 의존성 없는 `.env` 파서, 오프라인 판정 |
| `models.py` | `Segment` `Word` `Cue` `Trace` — 전부 frozen |
| `config.py` | **모든 임계값.** 코드에 숫자를 박지 않는다 |
| `hangul.py` | 자모 분해와 index map |
| `preprocess.py` | ffmpeg 호출 + 청크 분할 계획(순수 함수) |
| `transcribe.py` | **유일한 네트워크 경계.** `openai`를 import하는 파일은 여기뿐 |
| `mapping.py` | 경계 매핑. 프로젝트의 심장 |
| `refine.py` `layout.py` `validate.py` | 정제·조판·검사 |
| `output.py` `report.py` | SRT 직렬화, 검토 산출물 |
| `evaluate.py` | 지표 8종 |
| `pipeline.py` `cli.py` | 조립과 커맨드 |
| `ui/service.py` | UI 로직. **gradio를 import하지 않는다** |
| `ui/app.py` | gradio 화면 (경로·로그·SRT). 패키지 밖이라 규칙 7을 건드리지 않는다 |

---

## 개발

```bash
make check      # lint + types + test + invariant. 커밋 전 필수
make test
make invariant  # CLAUDE.md 불변 규칙 8개를 기계적으로 검사
```

테스트는 **네트워크를 소켓 수준에서 차단한다**(`tests/conftest.py`).
합성 픽스처(`tests/fixtures/synthetic/`)만으로 전 과정이 돈다.

실제 영상으로 돌려보는 절차는 [docs/RUNBOOK.md](docs/RUNBOOK.md)에 있다.
작업 규칙은 `CLAUDE.md`, 태스크 보드는 `TASKS.md`, 설계 판단은 `docs/DECISIONS.md`에 있다.
