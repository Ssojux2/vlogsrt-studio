# TASKS.md — vlogsrt 태스크 보드

> Claude Code가 `/task <ID>`로 하나씩 꺼내 실행하는 보드다.
> 설계 근거는 `docs/PLAN.md`, 실행 규칙은 `docs/CC-PLAN.md`, 상시 규칙은 `CLAUDE.md`.
>
> **한 세션에 태스크 하나.** DoD 통과 → 커밋 → 체크 → `/clear`.

---

## 진행 현황

| Phase | 태스크 | 완료 | 관문 |
|---|---|---|---|
| 0 하네스 | T0.0 ~ T0.4 | 0 / 5 | — |
| 1 골격 | T1.1 ~ T1.6 | 0 / 6 | — |
| 2 정제 | T2.1 ~ T2.7 | 0 / 7 | ★ 큐경계 F1 > 0.80 |
| 3 매핑 | T3.1 ~ T3.8 | 0 / 8 | ★ CER 절반 + 타이밍 불변 |
| 4 사전 | T4.1 ~ T4.3 | 0 / 3 | 고유명사 > 95% |
| 5 출력 | T5.1 ~ T5.5 | 0 / 5 | 검토시간 < 영상×0.3 |

**담당 표기**: 🤖 Claude Code / 🧑 사람 / 🤝 혼합

---

## 의존 그래프

```
T0.0🧑 ─────────────────────────────────┐
                                        │
T0.1 ─┬─ T0.2 ──────────────┐           │
      ├─ T0.3 ──────────┐   │           │
      ├─ T0.4 ◄─────────┴───┼───────────┘
      ├─ T1.1 ─ T1.2        │
      └─ T1.3🤝 ─ T1.4 ─ T1.5 ─ T1.6
                   │     ▲
                   │     └── T0.4
                   │
          T2.1 ─ T2.2 ─ T2.3 ─ T2.4 ─ T2.5 ─ T2.6 ─ T2.7 ★관문1
                   │                                   │
          T3.1🤝 ──┤                                    │
          T3.2 ────┴─ T3.3 ─ T3.4 ─ T3.5 ─ T3.6 ─ T3.7 ─ T3.8 ★관문2
                      ▲                                    │
                      └── T0.2                             │
                                                           │
                        T4.1 ─ T4.2 ─ T4.3🤝 ◄─────────────┤
                                                           │
                        T5.1 ─ T5.2 ─ T5.3 ─ T5.4 ─ T5.5🧑 ◄┘
```

---

# Phase 0 — 하네스

목표: **모든 판단을 숫자로 바꾼다.** 이 Phase가 끝나면 `vlogsrt eval`이 동작한다.

---

## [ ] T0.0 — 골든셋 3편 🧑

> **Claude Code에게 시키지 마라.** 정답을 만드는 일이다.

| | |
|---|---|
| 선행 | 없음 |
| 참조 | PLAN §9.1 |
| 예상 | 이틀 (타임박스) |

**작업**
1. 브이로그 3편 선정 — ① 실내 조용 ② 야외 소음 ③ BGM 많음
2. 각 편에서 3~5분 구간 잘라내기 → `golden/ep01.mp4` ~ `ep03.mp4`
3. 수동으로 완벽한 SRT 작성 → `golden/ep01.srt` ~ `ep03.srt`
   - 텍스트와 타이밍 **모두** 정확해야 한다
   - 자막 조판(줄바꿈, CPS)은 본인이 이상적이라 생각하는 형태로

**DoD**
```bash
ls golden/ep0{1,2,3}.mp4 golden/ep0{1,2,3}.srt
```

**주의**
- 완벽하지 않아도 이틀 넘기지 마라. 나중에 실패 사례를 발견하면 그때 보강한다
- 이 파일들은 이후 Claude Code가 **수정할 수 없다** (`settings.json` deny)

---

## [ ] T0.1 — 리포 부트스트랩 🤖

| | |
|---|---|
| 선행 | 없음 |
| 참조 | CC-PLAN §2, §4 |
| 예상 | 60분 |

**목표** 디렉토리 구조, 빌드 설정, Claude Code 설정, 환경변수 로더를 갖춘다.

**산출물**
- `pyproject.toml` — 의존성 정확히 3개 (`openai`, `tomli-w`, `rich`)
- `Makefile` — `check` / `test` / `lint` / `types` / `invariant` / `eval` / `fixtures`
- `.claude/settings.json`, `.claude/commands/{task,dod,invariant,eval}.md`
- `.gitignore`, `.env.example`
- `vlogsrt/env.py` — **의존성 없는 `.env` 파서** (아래 명세)
- 빈 모듈: `config.py` `preprocess.py` `transcribe.py` `hangul.py` `mapping.py` `refine.py` `output.py` `evaluate.py` `cli.py`
- `tests/conftest.py` — 네트워크 차단 fixture
- `docs/DASHBOARD.md` 빈 표, `docs/DECISIONS.md` 빈 파일

**`vlogsrt/env.py` 명세**

`python-dotenv`를 쓰지 마라 (불변 규칙 7). 15줄이면 된다.

```python
def load_env(path: Path = Path(".env")) -> dict[str, str]:
    """
    - `KEY=value` 형식만 지원
    - `#`으로 시작하는 줄과 빈 줄 무시
    - 값의 앞뒤 따옴표 제거
    - 이미 os.environ에 있는 키는 덮어쓰지 않는다 (셸 우선)
    - 파일이 없으면 조용히 빈 dict 반환
    """

def is_offline() -> bool:
    """VLOGSRT_OFFLINE이 '1'/'true'/'yes'면 True. 기본값 True."""

def require_api_key() -> str:
    """
    오프라인이면 RuntimeError.
    키가 없으면 안내 메시지와 함께 RuntimeError.
    LiveTranscriber 생성 시에만 호출된다.
    """
```

**DoD**
```bash
make check                      # 테스트 0개여도 통과
python -c "import vlogsrt; import vlogsrt.env"
python -c "from vlogsrt.env import is_offline; assert is_offline()"
test -f CLAUDE.md
test -f .claude/settings.json
test -f .env.example
grep -q '^\.env$' .gitignore
python -c "
import tomllib
d = tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']
assert len(d) == 3, d
"
```

**주의**
- `.env`는 절대 커밋되면 안 된다. `.gitignore` 확인이 DoD에 포함된 이유다
- `VLOGSRT_OFFLINE`의 **기본값은 True**다. 명시적으로 끄지 않는 한 오프라인이다

---

## [ ] T0.2 — `hangul.py` 자모 분해 🤖

| | |
|---|---|
| 선행 | T0.1 |
| 참조 | PLAN §6.3 |
| 예상 | 60분 |

**목표** 한글 음절을 자모로 분해하고, 정규화하면서 원본 인덱스 맵을 보존한다.

**산출물**
- `decompose_jamo(ch: str) -> list[str]`
- `normalize(text: str) -> tuple[str, list[int]]`
- `CHO`, `JUNG`, `JONG` 상수 테이블

**DoD**
```bash
pytest tests/test_hangul.py -q
```

**필수 테스트** — 구현보다 **먼저** 작성한다

| 케이스 | 입력 | 기대 |
|---|---|---|
| 받침 없음 | `가` | `[ㄱ, ㅏ]` |
| 받침 있음 | `강` | `[ㄱ, ㅏ, ㅇ]` |
| 겹받침 | `앉` | `[ㅇ, ㅏ, ㄵ]` |
| 한글 아님 | `A`, `1`, `漢`, `🍺` | 그대로 1개 |
| 공백·부호 | `" "`, `","` | normalize에서 제외 |
| 빈 문자열 | `""` | `("", [])` |

**index map 왕복 불변식** (이게 핵심이다)
```python
def test_imap_roundtrip(text):
    norm, imap = normalize(text)
    assert len(norm) == len(imap)
    assert all(0 <= i < len(text) for i in imap)
    assert imap == sorted(imap)              # 단조 증가
    # 같은 원본 인덱스를 가리키는 자모들은 연속해야 한다
    for i in range(1, len(imap)):
        assert imap[i] - imap[i-1] >= 0
```

**주의**
- **이후 모든 모듈이 여기에 의존한다.** 여기 버그가 있으면 T3.3에서 원인 불명의
  좌표 오류로 나타난다. 테스트를 아끼지 마라
- 분해 공식은 PLAN §6.3에 있다. `BASE=0xAC00`, `JUNG_N=21`, `JONG_N=28`

---

## [ ] T0.3 — SRT 파서 / 직렬화 🤖

| | |
|---|---|
| 선행 | T0.1 |
| 참조 | PLAN §5.5 |
| 예상 | 45분 |

**산출물**
- `output.parse_srt(text: str) -> list[Cue]`
- `output.write_srt(cues: list[Cue]) -> str`
- `output.format_timecode(sec: float) -> str`

**DoD**
```bash
pytest tests/test_output.py -q
```

**필수 테스트**
- 타임코드 포맷 정확히 `HH:MM:SS,mmm` (쉼표. 마침표 아님)
- 반올림: `12.4105` → `00:00:12,411` (밀리초 반올림 규칙 명시)
- CRLF / LF 양쪽 파싱
- BOM 있는 파일 파싱
- 왕복 무결성: `parse_srt(write_srt(cues)) == cues`
- 2줄 큐, 빈 줄 포함 큐
- 1시간 초과 (`01:23:45,678`)

**주의**
- SRT 인덱스는 1부터 시작한다
- 출력 개행은 `\n`으로 통일. 편집 프로그램 호환성 문제가 생기면 그때 옵션화

---

## [ ] T0.4 — `evaluate.py` 지표 8종 🤖

| | |
|---|---|
| 선행 | T0.3 |
| 참조 | PLAN §9.2 |
| 예상 | 120분 |

**목표** SRT 두 개를 비교해 8개 지표를 낸다. 여기서부터 모든 판단이 숫자가 된다.

**산출물**

| 지표 | 함수 |
|---|---|
| CER | `char_error_rate(hyp, ref)` |
| 고유명사 정확도 | `proper_noun_accuracy(hyp, ref, keywords)` |
| 큐 시작 MAE | `cue_start_mae(hyp, ref)` |
| ±200ms 적중률 | `cue_start_hit_rate(hyp, ref, tol=0.2)` |
| 큐 경계 F1 | `boundary_f1(hyp, ref, tol=0.3)` |
| 정제 위반율 | `violation_rate(cues, config)` |
| 플래그 정밀도 | `flag_precision(hyp, ref)` |
| 플래그 재현율 | `flag_recall(hyp, ref)` |

- `vlogsrt eval <hyp.srt> <ref.srt> [--json]`
- `make eval` → `docs/DASHBOARD.md` 갱신

**DoD**
```bash
pytest tests/test_evaluate.py -q
vlogsrt eval golden/ep01.srt golden/ep01.srt
# → CER 0.0%, MAE 0ms, 큐경계 F1 1.000 이어야 한다
```

**주의**
- **CER은 정규화 후 계산한다.** 공백·문장부호 제거, NFC 정규화. 그렇지 않으면
  조판 차이가 텍스트 오류로 잡힌다
- **큐 경계 F1**: hyp 경계와 ref 경계를 ±300ms 안에서 매칭. 헝가리안 알고리즘
  쓰지 마라. 정렬 후 탐욕 매칭으로 충분하고 훨씬 읽기 쉽다
- 플래그 정밀도/재현율은 아직 계산할 수 없다(플래그가 T5.1에 생김).
  함수 시그니처만 만들고 `NotImplementedError`를 던져라. **skip 처리하지 마라**
- 편집거리는 `difflib` 말고 직접 구현한 Levenshtein을 써라.
  `SequenceMatcher.ratio()`는 편집거리가 아니다

---

# Phase 1 — 골격 확보

목표: **whisper SRT를 손에 쥔다.** 이 Phase가 끝나면 이미 쓸 수 있는 자막이 나온다.

---

## [ ] T1.1 — 전처리: 오디오 추출 + 무음 검출 🤖

| | |
|---|---|
| 선행 | T0.1 |
| 참조 | PLAN §5.1 |
| 예상 | 75분 |

**산출물**
- `preprocess.probe_duration(path) -> float`
- `preprocess.extract_audio(src, dst, cfg) -> Path`
- `preprocess.detect_silence(path, cfg) -> list[tuple[float, float]]`
- `vlogsrt probe <video>` — duration과 무음 구간 수 출력

**DoD**
```bash
pytest tests/test_preprocess.py -q
vlogsrt probe golden/ep01.mp4
# → duration, 무음 구간 개수, 추출된 오디오 크기 출력
```

**주의**
- `ffmpeg`는 `subprocess`로 직접 호출. `ffmpeg-python` 금지 (불변 규칙 7)
- **`silencedetect` 결과는 stderr에 나온다.** stdout이 아니다
- `loudnorm=I=-18:TP=-2:LRA=11` 필터를 빼먹지 마라. 브이로그 볼륨 편차 대응이다
- ffmpeg/ffprobe가 없는 환경에서 테스트가 깨지지 않도록,
  실제 호출 테스트는 `@pytest.mark.skipif(shutil.which("ffmpeg") is None)`으로 감싼다
  (이건 허용되는 skip이다. 로직 테스트는 별도로 만들 것)

---

## [ ] T1.2 — 청크 분할 + offset 재기준화 🤖

| | |
|---|---|
| 선행 | T1.1 |
| 참조 | PLAN §5.1 분할 알고리즘 |
| 예상 | 90분 |

**산출물**
- `preprocess.make_chunks(audio, cfg, workdir) -> list[Chunk]`
- `Chunk` 데이터클래스 (`path`, `offset`, `duration`, `index`)
- `transcribe.rebase_timestamps(result, offset, seg_id_base)` — 스텁이라도

**DoD**
```bash
pytest tests/test_preprocess.py::test_chunking -q
```

**필수 테스트**
- 24MB 이하 → 단일 청크, `offset == 0`
- 초과 → `n = ceil(size / 24MB)` 개, 각 offset이 단조 증가
- **offset + duration 합이 원본 duration과 일치** (오차 < 0.1초)
- 목표 지점 ±25% 안에 무음이 있으면 그 중앙에서 분할
- 적당한 무음이 없으면 목표 지점 강제 + 경고 로그
- 무음이 하나도 없는 오디오 (전체 발화)

**주의**
- **이 프로젝트의 조용한 버그 온상이다.** 재기준화 시 다음을 **전부** 처리해야 한다
  - `segments[].start`, `segments[].end` ← offset 더하기
  - `words[].start`, `words[].end` ← offset 더하기
  - `segments[].id` ← 전역 재번호 (청크마다 0부터 시작하므로)
  - `segments[].seek` ← 그대로 두거나 재계산 (쓰지 않으면 무시)
- 청크 경계 ±2초 구간은 나중에 육안 검증 대상이다. 로그에 경계 시각을 남겨라

---

## [ ] T1.3 — `transcribe.py` + 픽스처 구조 🤝

| | |
|---|---|
| 선행 | T0.1 |
| 참조 | PLAN §2, §5.2 / CC-PLAN §3 |
| 예상 | 90분 (🤖) + 녹화 10분 (🧑) |

**목표** 유일한 네트워크 경계를 만들고, 이후 모든 개발이 오프라인으로 돌아가게 한다.

**산출물**
- `Transcriber` Protocol — **이 파일에서만 Protocol 사용 허용**
- `LiveTranscriber` — 실제 API. `require_api_key()` 호출
- `FixtureTranscriber` — `tests/fixtures/api/`에서 로드. **기본값**
- `get_transcriber(cfg) -> Transcriber` — 오프라인이면 Fixture 반환
- `vlogsrt record <video>` — 실제 호출 후 픽스처 저장
- `tests/conftest.py` — 네트워크 차단 autouse fixture

**whisper 호출 파라미터** (PLAN §2.2 표 준수)
```python
model="whisper-1"
language=cfg.language
prompt=cfg.prompt
temperature=0
response_format="verbose_json"
timestamp_granularities=["segment", "word"]   # cfg.word_timestamps=False면 ["segment"]
```

**픽스처 저장 형식**
```
tests/fixtures/api/
  ep01.whisper.json     # 응답 전문 (재기준화 전 원본)
  ep01.gpt.json         # T3.1에서 추가
  ep01.meta.json        # {recorded_at, model, params, chunk_count, cost_estimate}
```

**DoD**
```bash
pytest tests/test_transcribe.py -q          # 네트워크 없이 통과
python -c "
from vlogsrt.env import is_offline
from vlogsrt.transcribe import get_transcriber
assert type(get_transcriber(None)).__name__ == 'FixtureTranscriber'
"
grep -rn 'import openai' vlogsrt/ | grep -v transcribe.py
# → 출력이 없어야 한다 (불변 규칙 5)
```

**🧑 사람이 할 일** — Claude Code가 T1.3을 끝낸 직후
```bash
# .env에 키 넣고 오프라인 해제
vlogsrt record golden/ep01.mp4
vlogsrt record golden/ep02.mp4
vlogsrt record golden/ep03.mp4
# 약 $0.18. 이후 Phase 3 전까지 API 호출 없음
```

**주의**
- `FixtureTranscriber`는 파일이 없으면 **명확한 에러**를 던져라.
  "픽스처가 없습니다. 사람이 `vlogsrt record golden/ep01.mp4`를 실행해야 합니다."
- 픽스처를 **생성하려 하지 마라** (`settings.json`에서 deny)
- 재시도 로직(429/5xx 지수 백오프 4회)은 `LiveTranscriber`에만

---

## [ ] T1.4 — spine 파싱 + `trace.json` 스키마 🤖

| | |
|---|---|
| 선행 | T1.3 |
| 참조 | PLAN §2.3, §4 |
| 예상 | 75분 |

**산출물**
- `Segment`, `Word`, `Cue`, `Trace` 데이터클래스 (PLAN §4 스키마 준수)
- `transcribe.parse_spine(raw, offset, seg_id_base) -> list[Segment]`
- `Trace.save(path)` / `Trace.load(path)`
- `vlogsrt run <video> --offline --stop-after spine`

**DoD**
```bash
pytest tests/test_transcribe.py::test_spine -q
vlogsrt run golden/ep01.mp4 --offline --stop-after spine -o out/
python -c "
import json; t = json.load(open('out/trace.json'))
assert t['version'] == '2.0'
assert len(t['spine']) > 0
assert all(s['verdict'] == 'ok' for s in t['spine'])   # 필터는 T4.2
assert all(s['start'] < s['end'] for s in t['spine'])
"
```

**주의**
- `verdict`는 지금 전부 `"ok"`로 둔다. 환청 필터는 T4.2다
- `spine`은 이 시점 이후 **절대 수정되지 않는다** (불변 규칙 1).
  데이터클래스를 `frozen=True`로 만드는 것을 검토하라

---

## [ ] T1.5 — B0 베이스라인 + 첫 측정 🤖

| | |
|---|---|
| 선행 | T1.4, T0.4 |
| 참조 | PLAN §9.3 |
| 예상 | 45분 |

**목표** whisper 세그먼트를 그대로 SRT로 뽑아 대시보드 첫 행을 채운다.

**산출물**
- `vlogsrt baseline <video> --offline -o <dir>`
- `docs/DASHBOARD.md` B0 행

**DoD**
```bash
vlogsrt baseline golden/ep01.mp4 --offline -o eval/b0/
vlogsrt eval eval/b0/ep01.srt golden/ep01.srt
make eval        # 3편 전체 → DASHBOARD.md
```

**주의**
- **여기서 나온 SRT를 실제 영상에 얹어 눈으로 확인하라.** 숫자만 보지 마라.
  이 시점의 체감이 이후 모든 튜닝의 기준점이 된다
- 대시보드 첫 행이 채워지면 Phase 1의 실질적 목적은 달성이다

---

## [ ] T1.6 — `srt` vs `segments[]` 동등성 검증 🤖

| | |
|---|---|
| 선행 | T1.5 |
| 참조 | PLAN §2.4 |
| 예상 | 30분 |

**목표** "whisper의 `srt` 출력 = `segments[]`를 직렬화한 것"이라는 전제를 실측한다.

**산출물**
- `scripts/verify_srt_equivalence.py` — **일회성 검증. `vlogsrt/`에 넣지 마라**

**DoD**
```bash
python scripts/verify_srt_equivalence.py tests/fixtures/api/ep01.whisper.json
# → "IDENTICAL" 또는 차이점 목록
```

**주의**
- 이 스크립트는 `response_format="srt"` 픽스처가 추가로 필요하다.
  T1.3 녹화 시 함께 받아두거나, 이 태스크에서 사람에게 요청하라
- **차이가 발견되면 즉시 중단하고 보고하라.** PLAN §2.4 전제가 무너지는 것이고,
  골격 정의를 재검토해야 한다
- 결과를 `docs/DECISIONS.md`에 기록하라

---

# Phase 2 — 정제 엔진

목표: **읽기 좋은 자막으로 다듬는다.** 텍스트 품질과 무관하게 타이밍·조판을 먼저 해결한다.

---

## [ ] T2.1 — `config.py` + 프리셋 🤖

| | |
|---|---|
| 선행 | T0.1 |
| 참조 | PLAN §7 |
| 예상 | 60분 |

**산출물**
- `Config` 데이터클래스 — PLAN §7 TOML의 모든 키
- `load_config(preset, overrides) -> Config`
- `vlogsrt.toml`, `presets/{vlog,shorts,lecture}.toml`
- `vlogsrt config show [--preset X]`

**우선순위**: CLI 옵션 > `--preset` > `vlogsrt.toml` > 코드 기본값

**DoD**
```bash
pytest tests/test_config.py -q
vlogsrt config show --preset shorts | grep max_chars_per_line   # 12
vlogsrt config show --preset shorts --max-chars 14 | grep 14
```

**주의**
- **모든 임계값이 여기 모여야 한다** (불변 규칙 6). 이후 태스크에서 코드에
  숫자 리터럴이 나타나면 여기로 옮겨라
- 근거 있는 4개 값(`2.4`, `-1.0`, `0.85`, `0.08`)은 주석에 출처를 남겨라

---

## [ ] T2.2 — 분할 엔진 🤖

| | |
|---|---|
| 선행 | T2.1, T1.4 |
| 참조 | PLAN §5.4-1 |
| 예상 | 90분 |

**목표** 자막으로 너무 길거나 빽빽한 큐를 자른다. whisper 세그먼트는 디코딩용이라
자막으로는 긴 경우가 흔하다.

**산출물**
- `refine.split_cue(cue, words, cfg) -> list[Cue]`
- `refine.split_pass(cues, words, cfg) -> list[Cue]`

**분할 판단**: `길이 > max_chars×max_lines` OR `cps > max_cps` OR `duration > max_duration`

**분할 지점**: ① 큐 내부 단어 간 최대 휴지(≥ `pause_split`) ② 없으면 중앙에 가장 가까운 어절 경계

**DoD**
```bash
pytest tests/test_refine.py::test_split -q
```

**필수 테스트**
- 분할 불필요 → 그대로 1개 반환
- 휴지 있음 → 최대 휴지에서 분할
- 휴지 없음 → 어절 경계 폴백 (시간은 문자 수 비례 배분)
- 어절 경계도 없음(한 단어) → 분할 포기 + 플래그
- 재귀 4회 제한 (`max_split_depth`)
- `words=[]` (`--fast` 모드) → 어절 경계만 사용
- **분할 조각들의 시간 합 == 원본 큐 시간** (경계 오차 제외)
- 분할된 큐의 `origin == "split"`, `seg_ids`는 원본 유지

**주의**
- 시간을 배분할 때 `min_gap`을 넣어라. 붙여놓으면 T2.4에서 다시 벌려야 한다

---

## [ ] T2.3 — 병합 엔진 🤖

| | |
|---|---|
| 선행 | T2.2 |
| 참조 | PLAN §5.4-2 |
| 예상 | 60분 |

**목표** 너무 짧은 큐를 이웃과 합친다. 브이로그는 감탄사("어", "오", "음")가
독립 세그먼트로 나오는 일이 잦아 병합 없이는 자막이 깜빡인다.

**병합 조건 (AND)**: `a.duration < min_duration` / `b.start - a.end < merge_gap` /
합친 길이 ≤ `max_chars×max_lines` / 합친 CPS ≤ `max_cps`

**DoD**
```bash
pytest tests/test_refine.py::test_merge -q
```

**필수 테스트**
- 조건 충족 → 병합, `origin == "merge"`, `seg_ids`가 합쳐짐
- 간격이 멀면 병합 안 함
- CPS 초과하면 병합 안 함
- **연쇄 병합**: 짧은 큐 3개 연속 → 둘만 합치고 세 번째는 CPS 초과로 멈추는지
- 병합 후 텍스트 사이에 공백이 정확히 하나

**주의**
- 연쇄 병합 시 **CPS를 매번 재계산**해야 한다. 한 번 계산해두고 재사용하면 틀린다
- 병합과 분할이 서로를 되돌리지 않도록, 병합은 분할 **이후**에 한 번만 돈다

---

## [ ] T2.4 — 시간 보정 + 충돌 해소 🤖

| | |
|---|---|
| 선행 | T2.3 |
| 참조 | PLAN §5.4-3, §5.4-4 |
| 예상 | 60분 |

**산출물**
- `refine.adjust_timing(cues, cfg)` — lead_in/out, min/max duration
- `refine.resolve_overlaps(cues, cfg)` — min_gap 확보

**DoD**
```bash
pytest tests/test_refine.py::test_timing -q
```

**필수 테스트**
- 첫 큐 `start - lead_in < 0` → 0으로 클램프
- 마지막 큐 `end + lead_out > duration` → duration으로 클램프
- 겹침 → 중간점에서 분리
- 간격 부족 → 앞 큐 끝을 당김
- 보정 후 **모든 큐가 단조 증가**
- 보정 후 모든 큐가 `min_duration` 이상

**주의**
- 순서가 중요하다. lead_in/out → duration 클램프 → 충돌 해소.
  충돌 해소를 먼저 하면 lead_out이 다시 겹침을 만든다

---

## [ ] T2.5 — 줄바꿈 🤖

| | |
|---|---|
| 선행 | T2.4 |
| 참조 | PLAN §5.4-5 |
| 예상 | 60분 |

**규칙**
1. 전체가 `max_chars_per_line` 이하 → 1줄
2. 아니면 중앙에 가장 가까운 어절 경계에서 분리
3. 조사·어미가 줄 머리에 오지 않도록 보정
4. 위쪽 줄이 아래쪽보다 길거나 같게 (bottom-light)

**DoD**
```bash
pytest tests/test_refine.py::test_linebreak -q
```

**필수 테스트**
- 짧은 큐 → 1줄
- 긴 큐 → 2줄, 각 줄이 `max_chars` 이하
- 공백 없는 긴 문자열 → 강제 분리 + 플래그
- bottom-light 검증: `len(line[0]) >= len(line[1])`
- 조사 시작 방지: `["오늘은 블루펭귄", "에서 낮술"]` 같은 결과가 나오면 안 됨

**주의**
- 조사 목록을 하드코딩하지 마라. `config`에 `particles` 리스트로 둔다
- 3줄 이상이 필요한 큐는 T2.2에서 걸러졌어야 한다. 여기 오면 플래그

---

## [ ] T2.6 — 이상치 검사 🤖

| | |
|---|---|
| 선행 | T2.5 |
| 참조 | PLAN §6.8 |
| 예상 | 45분 |

**산출물**
- `refine.validate(cues, spine, cfg) -> list[Issue]`
- `Issue(level, code, cue_id, message)` — level: `ERR` / `WARN` / `INFO`

**검사 항목** (PLAN §6.8 목록 전부)

**DoD**
```bash
pytest tests/test_refine.py::test_validate -q
vlogsrt run golden/ep01.mp4 --offline -o out/ 2>&1 | grep -c ERR
# → 0 이어야 한다
```

**주의**
- `ERR`은 수정하되 반드시 로그에 남긴다. `WARN`은 수정하지 않고 기록만
- 위반율 계산(T0.4)이 이 결과를 쓴다

---

## [ ] T2.7 — `redo` + B1 측정 ★관문1 🤖

| | |
|---|---|
| 선행 | T2.6 |
| 참조 | PLAN §3.3, §9.3 |
| 예상 | 60분 |

**산출물**
- `vlogsrt redo <trace.json> [옵션]` — API 없이 M4~M5만 재실행
- `docs/DASHBOARD.md` B1 행

**DoD**
```bash
vlogsrt run golden/ep01.mp4 --offline -o eval/b1/
time vlogsrt redo eval/b1/trace.json --max-chars 16    # 0.5초 미만
make eval
```

**★ 관문 1 — 통과 조건**
```
큐 경계 F1 > 0.80    (목표)
위반율 < 2%
```

**F1이 0.70 미만이면 다음 Phase로 가지 말고 보고하라.**
PLAN §13 R2(whisper 세그먼트가 자막에 부적합)가 현실화된 것이다.
분할 임계(`pause_split`, `max_duration`)를 공격적으로 조정해 재측정한다.

**주의**
- `redo`는 `trace.json`의 `spine`과 `cues` 초안에서 다시 시작한다.
  API 응답 캐시를 다시 읽지 않는다
- 격자 탐색: `max_chars` 12~20 × `max_cps` 10~14 → F1 최대 조합을 찾아
  `presets/vlog.toml`에 기록

---

# Phase 3 — 경계 매핑 ★ 최대 난이도

목표: **텍스트를 gpt 것으로 갈아 끼우되 타이밍은 건드리지 않는다.**

---

## [ ] T3.1 — gpt-transcribe 호출 + 픽스처 🤝

| | |
|---|---|
| 선행 | T1.3 |
| 참조 | PLAN §2.2, §5.2 M2b |
| 예상 | 45분 (🤖) + 녹화 10분 (🧑) |

**호출 파라미터**
```python
model="gpt-transcribe"
language=cfg.language
languages=cfg.extra_languages     # ["ko", "en"]
keywords=cfg.keywords             # 지금은 빈 배열. 사전은 T4.1
temperature=0
response_format="json"
```

**DoD**
```bash
pytest tests/test_transcribe.py::test_gpt -q
```

**🧑 사람**: `vlogsrt record` 재실행해 `*.gpt.json` 3개 확보 (약 $0.14)

**주의**
- `timestamp_granularities`를 넣지 마라. `whisper-1` 전용이다
- `include=["logprobs"]`도 넣지 마라. `gpt-transcribe` 미지원이다

---

## [ ] T3.2 — W 구성 + 절단점 기록 🤖

| | |
|---|---|
| 선행 | T1.4 |
| 참조 | PLAN §6.2 |
| 예상 | 45분 |

**산출물**
- `mapping.build_w(segments) -> tuple[str, list[int], list[int]]`
  (W 문자열, 절단점 목록, 문자별 소유 seg_id)

**DoD**
```bash
pytest tests/test_mapping.py::test_build_w -q
```

**필수 테스트**
- `cuts[-1] == len(W)`
- `cuts`가 단조 증가
- `verdict != "ok"`인 세그먼트는 제외됨
- 빈 텍스트 세그먼트 처리
- `len(owner) == len(W)`

---

## [ ] T3.3 — `map_position` ★ 가장 중요 🤖

| | |
|---|---|
| 선행 | T3.2, T0.2 |
| 참조 | PLAN §6.4 |
| 예상 | 120분 |

**목표** W 좌표의 절단점을 A 좌표로 사상한다. **이 함수가 프로젝트의 심장이다.**

**산출물**
- `mapping.map_position(pos_w, opcodes, imap_w, imap_a) -> int`
- `mapping.align(w, a) -> list[Opcode]` — `autojunk=False` 필수

**좌표계가 넷이다. 혼동하지 마라.**
```
원본 W ──normalize──> 정규화 W ──SequenceMatcher──> 정규화 A ──imap_a──> 원본 A
   ↑                      ↑                            ↑                    ↑
 pos_w                  nw (bisect)                    na              반환값
```

**DoD**
```bash
pytest tests/test_mapping.py::test_map_position -q
```

**필수 테스트** — `tests/fixtures/synthetic/` 12케이스

| opcode | 케이스 |
|---|---|
| `equal` | 시작 / 중간 / 끝 / 단일 문자 |
| `replace` | 같은 길이 / W가 김 / A가 김 / 전체 교체 |
| `insert` | 중간 / 끝 |
| `delete` | 중간 / 끝 |

경계값: `pos_w == 0`, `pos_w == len(W)`, 빈 문자열

**주의**
- **`autojunk=False` 필수** (불변 규칙 4). 없으면 자모 시퀀스에서 `ㅇ`, `ㅏ`가
  잡음으로 버려져 정렬이 무너진다
- 정규화 좌표 ↔ 원본 좌표 변환은 `bisect_left` 사용
- **이 함수는 큐 시각을 반환하지 않는다.** 텍스트 인덱스만 반환한다.
  시간 관련 값이 이 함수에 들어오거나 나가면 설계 위반이다
- `replace`의 비율 배분에서 `i2 - i1 == 0`인 경우 ZeroDivision 방어

---

## [ ] T3.4 — `enforce_monotonic` 🤖

| | |
|---|---|
| 선행 | T3.3 |
| 참조 | PLAN §6.4 |
| 예상 | 30분 |

**목표** `replace` 블록의 비율 배분에서 드물게 절단점이 역전된다. 이걸 막는다.

**DoD**
```bash
pytest tests/test_mapping.py::test_monotonic -q
```

**필수 테스트**
- 이미 단조 → 그대로
- 1개 역전 → 앞 값으로 강제
- 연속 역전 → 전부 처리
- 결과가 항상 `0 <= cuts[i] <= len(A)` 범위

**주의**
- 역전이 발생하면 `WARN` 로그를 남겨라. 자주 발생하면 T3.3에 버그가 있다는 신호다
- 텍스트 슬라이스가 음수 길이가 되는 것을 막는 게 목적이다

---

## [ ] T3.5 — 어절 경계 스냅 🤖

| | |
|---|---|
| 선행 | T3.4 |
| 참조 | PLAN §6.5 |
| 예상 | 75분 |

**목표** 사상된 절단점이 어절 중간에 떨어지면 가장 가까운 공백으로 옮긴다.

**산출물**
- `mapping.snap_to_word_boundary(cut_a, text_a, words, boundary_time) -> tuple[int, list[str]]`

**DoD**
```bash
pytest tests/test_mapping.py::test_snap -q
```

**필수 테스트**
- 이미 공백 위 → 그대로
- 왼쪽이 가까움 → 왼쪽
- 오른쪽이 가까움 → 오른쪽
- 양쪽 다 6자 초과 → 원래 위치 + `snap_failed` 플래그
- `words=[]` (`--fast`) → 무조건 왼쪽
- 문자열 시작/끝 경계

**주의**
- **여기가 `words[]`의 주 용도다.** 경계에 걸친 어절이 어느 세그먼트 소유인지
  판단하는 데만 쓴다. 큐 시각을 정하는 데 쓰지 마라
- `--fast`에서 왼쪽을 택하는 이유: 한국어는 어절 끝에 조사가 붙어
  앞 세그먼트에 속하는 경우가 많다

---

## [ ] T3.6 — 고아 텍스트 처리 🤖

| | |
|---|---|
| 선행 | T3.5 |
| 참조 | PLAN §6.6 |
| 예상 | 75분 |

**목표** 환청 필터로 제외된 구간에 속한 gpt 텍스트를 버리지 않는다.

**산출물**
- `mapping.rescue_orphans(cuts_a, spine, cues, silences, text_a, cfg) -> list[Cue]`

**DoD**
```bash
pytest tests/test_mapping.py::test_orphan -q
```

**필수 테스트**
- 제외 세그먼트 없음 → 고아 0개
- 중간 제외 → 앞뒤 큐 사이에 고아 큐 생성
- 맨 앞 제외 / 맨 뒤 제외
- 고아 텍스트가 2자 미만 → 생성 안 함
- 생성된 고아의 `conf <= 0.30`, `origin == "orphan"`, 플래그에 `time_estimated`

**주의**
- **텍스트를 버리지 않는 것이 원칙이다.** whisper가 헛소리했다는 게
  그 시간에 발화가 없었다는 뜻은 아니다
- 고아 큐의 시각은 추정이다. `conf`를 반드시 0.3 이하로 고정해 리포트 최상단에 올린다
- T4.2 전에는 모든 `verdict`가 `"ok"`라 고아가 발생하지 않는다.
  합성 픽스처로 테스트하라

---

## [ ] T3.7 — 큐별 신뢰도 🤖

| | |
|---|---|
| 선행 | T3.6 |
| 참조 | PLAN §6.7 |
| 예상 | 45분 |

**산출물**
- `mapping.cue_confidence(cue, seg, cfg) -> float`

**계산**: `sim(자모 정규화된 whisper_text, gpt_text)` → verdict 감점 → 정제 이력 감점 → 구조 위반 시 0

**DoD**
```bash
pytest tests/test_mapping.py::test_confidence -q
```

**필수 테스트**
- 완전 일치 → 1.0에 가까움
- 1자 차이(자모 1개) → 0.9 이상 (자모 분해 효과 확인)
- 완전 불일치 → 0.3 이하
- `origin == "orphan"` → 0.30 이하 강제
- `end <= start` → 0.0
- `low_logprob` → 0.85배

**주의**
- 여기서도 `autojunk=False`
- 두 모델이 **똑같이 틀린** 경우 sim이 높아 놓친다. 이건 구조적 한계이고
  `keywords` 사전(T4.1)이 유일한 방어선이다. 주석에 남겨라

---

## [ ] T3.8 — B2 측정 + 타이밍 불변 검증 ★관문2 🤖

| | |
|---|---|
| 선행 | T3.7 |
| 참조 | PLAN §9.3, CC-PLAN §5.1 |
| 예상 | 60분 |

**산출물**
- 전체 파이프라인 연결
- `tests/test_invariants.py::test_timing_unchanged_by_mapping`
- `docs/DASHBOARD.md` B2 행

**DoD**
```bash
vlogsrt run golden/ep01.mp4 --offline -o eval/b2/
pytest tests/test_invariants.py -q
make eval
```

**★ 관문 2 — 두 조건 모두 충족해야 통과**
```
① B1 대비 CER이 절반 이하
② B1 대비 큐 시작 MAE와 ±200ms 적중률이 ±5% 이내로 불변
```

**②가 실패하면 CER이 아무리 좋아져도 통과가 아니다.**
경계 매핑이 큐 시각에 영향을 주고 있다는 뜻이고, 이는 v2 아키텍처의
근본 가정 위반이다. T3.3~T3.5를 다시 본다.

**추가 확인**
- F13 폴백: 전역 sim < 0.4이면 whisper 골격만으로 출력 + 경고.
  합성 케이스로 검증하라

---

# Phase 4 — 사전 + 환청 필터

---

## [ ] T4.1 — `keywords` 3계층 로더 🤖

| | |
|---|---|
| 선행 | T3.8 |
| 참조 | PLAN §5.2 |
| 예상 | 45분 |

**계층**: 고정(`keywords/base.txt`) + 회차(`--keywords-file`) + CLI(`--keywords`)

**DoD**
```bash
pytest tests/test_config.py::test_keywords -q
vlogsrt keywords show --file keywords/ep01.txt
```

**주의**
- 중복 제거, 공백 정리, 빈 줄·주석(`#`) 무시
- 순서는 유지 (API가 순서를 참고할 수 있음)

---

## [ ] T4.2 — `verdict` 판정 + 무음 교차검증 🤖

| | |
|---|---|
| 선행 | T4.1 |
| 참조 | PLAN §2.3, §5.2 M2c |
| 예상 | 60분 |

**산출물**
- `transcribe.judge_verdict(seg, silences, cfg) -> str`

**판정 순서** (PLAN §5.2 M2c 코드 그대로)

**DoD**
```bash
pytest tests/test_transcribe.py::test_verdict -q
```

**필수 테스트**
- `compression_ratio 2.81` → `compression_fail`
- `avg_logprob -1.5` + `no_speech_prob 0.8` → `silent`
- `avg_logprob -1.5` + `no_speech_prob 0.1` → `low_logprob`
- 무음 구간과 85% 겹침 → `silent`
- 정상 → `ok`

**주의**
- **임계값 `2.4`, `-1.0`, `0.6`은 OpenAI 문서 근거값이다. 바꾸지 마라.**
  테스트가 실패하면 판정 로직을 고쳐라
- 판정 순서가 중요하다. `compression_ratio`를 먼저 본다

---

## [ ] T4.3 — 고아 연동 + 사전 효과 측정 🤝

| | |
|---|---|
| 선행 | T4.2 |
| 참조 | PLAN §6.6, §9.2 |
| 예상 | 45분 (🤖) + 녹화 10분 (🧑) |

**🧑 사람**: `keywords`를 넣어 픽스처 **재녹화** (약 $0.15).
`keywords`는 API 요청 파라미터라 기존 픽스처로는 효과를 측정할 수 없다.

**DoD**
```bash
make eval
# → 고유명사 정확도 > 95%
```

**★ 관문 3**: 고유명사 정확도가 90% 미만이면 보고하라.
PLAN §13 R3(사전 효과 미달)이다. whisper `prompt`에도 중복 주입해 재측정한다.

---

# Phase 5 — 출력 · 운영

---

## [ ] T5.1 — `review.srt` diff 마킹 🤖

| | |
|---|---|
| 선행 | T4.3 |
| 참조 | PLAN §5.5 ② |
| 예상 | 60분 |

**목표** 큐 전체가 아니라 **whisper와 gpt가 실제로 갈린 글자만** `[?]`로 감싼다.

**DoD**
```bash
pytest tests/test_output.py::test_review_srt -q
vlogsrt run golden/ep01.mp4 --offline -o out/
grep -c '\[?' out/output.review.srt
```

**주의**
- `cue.whisper_text`와 `cue.text_raw`의 diff에서 마커 범위를 얻는다
- 연속된 불일치 문자는 하나의 `[?...]`로 묶어라

---

## [ ] T5.2 — `report.md` 생성 🤖

| | |
|---|---|
| 선행 | T5.1 |
| 참조 | PLAN §5.5 ③ |
| 예상 | 75분 |

**섹션**: 요약 통계 / 우선 확인(신뢰도 낮은 순) / 정제 경고 / 사전 갱신 후보

**DoD**
```bash
vlogsrt run golden/ep0{1,2,3}.mp4 --offline -o out/
# 3편 모두 report.md 생성. 사람이 육안 검토
```

**주의**
- 우선 확인 항목에는 whisper/gpt 텍스트를 **나란히** 보여줘라.
  사람의 판단 속도가 여기서 결정된다
- 예상 검토 시간 추정치를 넣어라 (플래그 큐 수 × 20초 정도)

---

## [ ] T5.3 — 사전 갱신 후보 추출 🤖

| | |
|---|---|
| 선행 | T5.2 |
| 참조 | PLAN §5.2 누적 사전 |
| 예상 | 45분 |

**목표** 불일치 구간에서 반복 등장한 단어를 모아 다음 회차 `keywords` 후보로 제시.
**이게 있어야 파이프라인이 회차를 거듭하며 개선된다.**

**DoD**
```bash
pytest tests/test_output.py::test_dict_candidates -q
grep -A5 '사전 갱신 후보' out/output.report.md
```

**주의**
- 2회 이상 등장한 것만. 1회는 노이즈
- whisper가 어떻게 잘못 들었는지도 함께 표시 (`블루펭귄 ← 블루팽귄 4회`)

---

## [ ] T5.4 — CLI 완성 + 캐시 + `--fast` 🤖

| | |
|---|---|
| 선행 | T5.3 |
| 참조 | PLAN §5.6 |
| 예상 | 75분 |

**산출물**
- 전 옵션 동작 (PLAN §5.6 표)
- 오디오 해시 캐시 — 같은 파일 재실행 시 전처리 생략
- `--fast` — `timestamp_granularities=["segment"]`
- `--dry-run` — 비용 추정만

**DoD**
```bash
vlogsrt --help
vlogsrt run golden/ep01.mp4 --offline --fast -o out/fast/
vlogsrt run golden/ep01.mp4 --dry-run
make check
make eval        # --fast 결과도 대시보드에 별도 행으로
```

---

## [ ] T5.5 — 실사용 + 스톱워치 🧑

| | |
|---|---|
| 선행 | T5.4 |
| 참조 | PLAN §13 R8 |
| 예상 | 1회차 분량 |

> **사람이 한다.** 이게 프로젝트의 최종 판정이다.

**작업**
1. 신규 브이로그 1편으로 실행 (실제 API, 오프라인 아님)
2. `output.srt` + `report.md`로 검토·수정
3. **스톱워치로 검토 시간 측정**
4. 실제 업로드에 사용

**★ 최종 관문**
```
검토 시간 < 영상 길이 × 0.3
```

초과하면 PLAN §13 R8이다. `conf_threshold`를 조정해 플래그 수를 줄이거나,
리포트 구성을 바꾼다. **"도구를 쓰는 게 안 쓰는 것보다 느리다"면 전부 무의미하다.**

**기록**
- `docs/DASHBOARD.md`에 실측 검토 시간
- `docs/DECISIONS.md`에 개선점

---

## 부록 — 태스크 착수 프롬프트

```
/task T3.3
```

`/task` 커맨드가 없다면 수동으로:

```
docs/TASKS.md 에서 T3.3 블록만 읽어라.
참조에 적힌 docs/PLAN.md §6.4 만 읽어라. 전문을 읽지 마라.
선행 태스크(T3.2, T0.2)가 체크되어 있는지 확인하라.
산출물과 DoD를 먼저 복창하라.
테스트를 먼저 작성하고, 그 다음 구현하라.
DoD 커맨드가 통과할 때까지 반복하되 임계값과 기대값은 절대 수정하지 마라.
통과하면 `make check` 후 `[T3.3] 요약` 형식으로 커밋하고, 이 파일의 체크박스를 채워라.
이 태스크만 처리한다. 다음으로 넘어가지 마라.
```
