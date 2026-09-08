# Bootstrap 리뷰 — Opus5 (co-manager) · **rev.2 (Astra disposition 반영)**

대상: `docs/plans/2026-09-08-final-cut-project.md`, `docs/orchestration/{README.md,workflow.json,task-template.md}`
기준 커밋: `c96e50a` (`feat/final-cut-project`, 기준 소스는 `de17d87`)
Run `run_73768f7c4dec` · Opus task `task_ccb2369295aa` · dispatch `ctx_82a105f90db9`

rev.1의 판정 중 **일부가 명세·증거와 충돌해 Astra가 기각·수정했다**
(`docs/orchestration/reviews/astra-bootstrap-disposition.md`). 이 문서는 그 처분을 반영한
**현행판**이다. rev.1의 철회된 요구를 GLM brief에 넣지 마라.

이 리뷰는 소스를 읽고 실제로 돌려서 확인한 것만 적는다. 코드는 한 줄도 바꾸지 않았다.
`.env.example`은 읽지도 건드리지도 않았다.

---

## 0. 처분 요약 (rev.1 → rev.2)

| # | rev.1 판정 | Astra 처분 | rev.2 상태 |
|---|---|---|---|
| C1 | Makefile `PY :=` 리팩터 **필수** | **부분 기각** — `make -n test PY=…` 이미 동작 | 요구 철회. 자식 환경 부트스트랩 증명만 남긴다 |
| C2 | worktree import 출처 증명 | 승인 | 유지 |
| C3 | 별도 `vlogsrt.project.toml`/`ProjectConfig` | 승인 | 유지 |
| C4 | 자산별 fixture 디렉터리, 전역 env 변이 금지 | 승인 | 유지 |
| C5/C6 | 소스 파일 변경 **0줄** 강제 | **수정** — 명세가 요구하는 추가적 변경은 허용 | 선호로 격하 + 시그니처는 Sol이 확정 |
| C7 | 프로젝트 전용 SRT 직렬화기 **금지** | **기각** — 반례 있음 | **뒤집힘**. 유리수 기반 정확 반올림 필수 |
| C8 | 명세 문장 오류 | 승인 | **해결됨** (명세 정정 확인) |
| C9 | 검사기 8건 개정 + "24군" | **수정** — openai 예외 불허, 투기적 검사 2건·개수 목표 철회 | 6건으로 축소 + exact-path 규칙 강화 |
| C10 | 이번 실행에서 S17 손대지 마라 | **수정** — 합성 오디오 오프라인 재현/수정은 허용 | 실제 미디어 인수만 보류 |
| C11 | contract 태스크가 공유 파일 선점 | 승인 | 유지 |
| C12 | import 그래프로 네트워크 0 강제 | **수정** — 전이적 import 금지는 비현실적 | **행위 기반 네트워크 테스트**로 교체 |
| C13 | "자동 회귀 게이트가 하나도 없다" | **부분 기각** — 기존 테스트가 실제 회귀 커버리지다 | 과장 철회. 바이트 잠금 **보강**으로 축소 |
| G-ALL-5 | 자식도 `.env.example`이 ` M`이어야 | **정정** | 자식은 **수정 없음이 정상**. 만들어내게 하지 마라 |
| P0-1 | 7단계 한 태스크 | **수정** — 작게 쪼개라 | 3개 직렬 태스크로 분할 |
| CLAUDE.md | 사용자 승인 루프 필요 | **불필요** — 명세·사용자 의도로 이미 승인 | 제안 철회. 경계 문서화로 갈음 |

---

## 1. 실측한 기준선

| 항목 | 실측 |
|---|---|
| `make check` | **PASS** (`.venv/bin/python` 3.12.14 · ruff · mypy 23파일 · **pytest 389건** · 불변 16군 OK) |
| `make -n test PY=/explicit/python` | `/explicit/python -m pytest` — **명령행 오버라이드가 이미 동작한다** |
| `.venv` | uv 생성. `pip` 없음(정상). `uv` = `/opt/homebrew/bin/uv` |
| Python | `.venv` 3.12.14 · `~/.local/bin/python3.12` 3.12.14 · `python3` = **3.9.6 (Xcode)** |
| ffmpeg / ffprobe | `/opt/homebrew/bin` · **9.0.1** |
| xmllint | `/usr/bin/xmllint` |
| `golden/` · `.vlogsrt-cache/` · `out/` | **없음** |
| API 픽스처 | `fr_day_1_2_fhd.{whisper,gpt,meta}.json` 3개. `rescue`/`arbiter` 없음 |
| 합성 픽스처 | `tests/fixtures/synthetic/` 18개 (`demo`·`loopy`·`loopyletter`·`loopylong`) |
| `make verify STEM=fr_day_1_2_fhd` | **한 줄 만에 종료** — "전처리 캐시가 없습니다". 검사가 하나도 돌지 않는다 |

**남는 게이트 사실**: `make verify`·`make eval`은 실제 미디어와 `golden/`이 없어 이번 실행에서
통과시킬 수 없다. 이것을 GLM DoD에 넣지 마라 — 통과 불가능한 게이트 앞에서 GLM은 오프셋에
상수를 더하는 쪽으로 "해결"하려 하고, 그게 D24가 무너진 방식이다. **단, 이것이 오프라인
합성 재현까지 막지는 않는다**(C10 참조).

---

## 2. 유효한 결함 (구현 착수 전 확정 사항)

### C1. 자식 환경 부트스트랩 — **Makefile 리팩터는 필요 없다**

정정: `make -n test PY=/explicit/python` → `/explicit/python -m pytest`. make의 명령행 변수
할당은 `:=`도 이긴다. rev.1의 "`PY ?=`로 바꿔라"는 **철회한다.**

남는 실질 위험은 그대로다: `.venv/`는 `.gitignore`에 있으므로 새 worktree에는 없고,
`PY`를 주지 않으면 `python3` = **3.9.6**으로 떨어져 `No module named pytest` / `tomllib`로 즉사한다.
이것은 Makefile 결함이 아니라 **부트스트랩 미이행**이다.

요구(각 자식 태스크 진입 조건):
```
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
uv pip check
```
`uv venv`에 `pip`가 없는 것은 정상이며 결함이 아니다. 이후 `make check`는 자식의 `.venv`가
자동으로 잡히거나, 명시적으로 `make check PY=<자식 .venv>/bin/python`을 쓴다.
런타임 하한은 **3.11**이다(`requires-python`·ruff `py311`·mypy `python_version` 셋 다 유지).
개발 환경이 3.12여도 3.12 전용 문법을 쓰지 마라.

### C2. worktree가 자기 소스를 import하는지 증명하게 하라 — 유지

`vlogsrt/env.py`의 `find_env_file()`은 cwd에서 위로 올라가다 못 찾으면 `PACKAGE_ROOT/.env`
(= **import된 패키지의** 저장소 루트)로 폴백한다. `project_root()`가 그 디렉터리를 돌려주고
`env.fixture_dir()`·`work_dir()`·`golden_dir()`가 전부 거기 매달린다.

즉 자식이 자기 venv 없이 메인 체크아웃의 인터프리터(editable → 메인 소스)를 쓰면 **자식의
테스트가 메인 저장소의 픽스처를 읽고 메인 저장소의 `.vlogsrt-cache`에 쓴다.** 회귀가 가려지고
두 병렬 자식이 같은 작업 디렉터리를 두고 경합한다.

강제 수단: 모든 태스크 보고에
`python -c "import sys,vlogsrt;print(sys.version, vlogsrt.__file__)"` 출력을 넣고,
**경로가 자기 체크아웃 아래가 아니면 그 보고는 무효다.**

### C3. `vlogsrt.toml`에 프로젝트 섹션을 넣으면 즉시 예외가 난다 — 유지 (승인됨)

`config.py:_put()`은 `Config` 필드에 없는 키를 만나면 `RuntimeError("알 수 없는 설정 키")`를
던진다. 섹션 이름은 무시되고 키만 평평해지므로 `[fcpx] frame_rate = …`도 **터진다.**

확정:
- 프로젝트 설정은 `vlogsrt.project.toml` + `vlogsrt/project/config.py`의 `ProjectConfig`(frozen).
- `Config`에는 키를 **하나도 추가하지 않는다.** `Trace.config_snapshot`도 건드리지 않는다.
- 프로젝트 스냅샷은 `project_trace.json`에만 들어간다.

### C4. per-asset namespace는 stem이 아니라 **fixture 디렉터리** — 유지 (승인됨)

현재 픽스처 키는 `transcribe._fixture_path()` 하나: `{fixture_dir}/{stem}.{kind}.json`.
`stem`은 `pipeline.prepare()`의 `video.stem`이다. 인수 조건에 **"same-name asset"**이 있으므로
stem은 자산 식별자가 될 수 없다. 동시에 stem을 해시로 바꾸면 사람이 `vlogsrt record <video>`로
픽스처를 녹화하는 절차가 깨진다.

확정:
- **stem은 파일 stem 그대로.** namespace는 `<fixture base>/<asset_content_id>/<stem>.whisper.json`.
- `asset_content_id`는 명세가 요구한 강한 캐시 키(원본 내용 해시 + 오디오 스트림/채널 선택 +
  모델/설정)에서 파생한다.
- **`pipeline.source_fingerprint()`를 재사용하지 마라** — 이름·크기·mtime·앞 1MB만 보므로 내용
  동일성 보장이 아니다(장편 영상에서 빠르라고 그렇게 만든 것이다).
- `os.environ`을 건드리지 않는다. `FixtureTranscriber(fixture_dir=…)`를 **직접 생성해서** 넘긴다.

### C5/C6. 공개 API 조립을 **선호**한다 — 그러나 추가적 소스 변경을 금지하지는 않는다 (수정됨)

rev.1은 "소스 파일 0줄 변경"을 강제했다. Astra가 수정했다: **명세가 이미 요구하는 추가적
(additive) 소스 API 변경 두 가지는 정당하다** — (a) 타입 있는 empty 결과, (b) preprocess PCM 경계.
**Sol이 구현 전에 정확한 시그니처를 확정한다.** GLM에게 "알아서 정하라"고 넘기지 마라.

여전히 참인 조립 경로(기본값으로 삼되, 위 두 가지는 예외로 허용):
```
preprocess.extract_audio / probe_duration / make_chunks   # ffmpeg 경계는 preprocess.py
transcribe.FixtureTranscriber(fixture_dir=<per-asset>)    # 또는 LiveTranscriber
transcribe.TranscriptionJob(stem=…, chunks=…, source=…, kind=FIXTURE_KIND)
transcribe.build_spine(transcriber.transcribe(job, cfg))  # draft
repair.settle(draft, repair.Source(…), cfg, transcriber)  # spine 확정 (규칙 2의 경계)
pipeline.spine_to_cues(spine) → refine.refine(cues, cfg, duration)
```

**여전히 강제되는 것 3가지:**
1. **성공한 빈 응답과 실패/이상 응답을 뭉개지 마라.** 현재 코드가 이미 셋을 구별한다 —
   `"segments": []` → `build_spine()`이 `[]`(경고만) = **empty(정상)**;
   `segments` 키 없음 → `parse_segments()`가 `RuntimeError` = **error**;
   호출 실패 → transcriber가 던짐 = **error**.
   `pipeline.fetch_draft()`는 빈 결과에 맨 `RuntimeError`를 던져 이 구별을 **뭉갠다** — 어댑터가
   그대로 쓰면 안 된다. 타입 있는 결과를 어디에 둘지(프로젝트 쪽 / 소스 쪽 추가 API)는 Sol이
   시그니처와 함께 정한다. 합성 픽스처 3종(정상 / `segments: []` / `segments` 키 없음)으로
   시험하고, **error를 empty로 강등하지 않는다**를 테스트로 못 박는다.
2. **`polish_pass`를 따로 부르지 마라.** `refine.refine()`이 내부에서 이미 부른다
   (`vlogsrt/refine.py:275`). 두 번 부르면 부호가 두 번 정리된다.
3. 레거시 소스 출력 동작을 바꾸지 마라. 추가 파라미터는 **기본값에서 기존 동작과 동일**해야 하고,
   그 사실을 회귀 테스트가 증명해야 한다(C13).

### C7. **뒤집힘** — 프로젝트 export는 유리수에서 직접 반올림해야 한다

rev.1은 "`output.format_timecode()`가 이미 half-up이므로 프로젝트 전용 직렬화기를 금지"했다.
**틀렸다.** Astra의 반례를 실측으로 확인했다:

```
Fraction(1001, 2000) = 0.5005초  →  정확한 half-up = 501ms
float(0.5005) * 1000 + 0.5      =  500.99999999999994
int(...)                         =  500
format_timecode(float(1001/2000)) = 00:00:00,500      ← 501이 아니다
```

`format_timecode`는 **float 경유** half-up이라 이진수로 정확히 표현되지 않는 유리수 경계에서
아래로 떨어진다. 명세가 요구하는 것은 "round half-up to ms ONCE at final serialization"이고,
`<=1ms` 한도는 **오차 허용치이지 half-up 규정의 면제가 아니다**.

확정:
- **레거시 소스 SRT 출력은 그대로 둔다.** `format_timecode`를 고치면 기존 출력이 회귀한다.
- **프로젝트 export(EDIT/SOURCE SRT)는 `Fraction`에서 직접 ms로 half-up**한다. float를 거치지 마라.
- 텍스트·블록 포맷(번호·`-->`·줄바꿈·인코딩)은 `output`의 것을 실용적인 범위에서 재사용하되,
  **타이밍 계산 경로는 분리한다.**
- "같은 경계는 같은 값"을 테스트로 못 박는다 — 인접 큐의 공유 경계가 두 큐에서 동일 ms여야 한다.

### C8. **해결됨** — 명세가 정정되었다

현재 명세(line 28) 확인:
> Adjacent cues may satisfy `previous.end == next.start`; reject `previous.end > next.start` overlaps
> and individual `cue.end <= cue.start` invalid durations.

인접 경계 규칙과 개별 큐 규칙이 분리되어 모호함이 없다. 추가 조치 없음.

### C9. 검사기 개정 — **6건**, exact-path·AST 기반, openai 예외 없음 (수정됨)

`scripts/check_invariants.py`의 `_sources()`는 `PACKAGE.rglob("*.py")` = `vlogsrt/**`를 재귀로 훑는다.
**`vlogsrt/project/`(패키지 안) 유지**가 확정이다 — 16군이 자동 적용된다.

**rev.1에서 철회하는 것:**
- `project/vision/client.py`에 openai import 예외를 주자는 제안 → **불허**. API 경계는
  `vlogsrt/transcribe.py` 하나로 남는다. vision 전송이 필요하면 거기에 **좁은 범용 전송**을 더한다.
  검사기 `check_openai_boundary`는 **예외를 추가하지 않는다**.
- 투기적 regex 검사 2건(`no-SRT-writer`, `offline gate`) → 철회. 정규식으로 흉내 낼 계약이 아니다.
- "24군을 만든다" 같은 **개수 목표** → 철회. 개수가 아니라 계약이 기준이다.

**남는 개정(6건):**

| 검사 | 개정 |
|---|---|
| `check_banned_imports` | `numpy`·`onnxruntime`은 `vlogsrt/project/vad/**`에서만 (exact path) |
| `check_ffmpeg_boundary` | `subprocess`는 `vlogsrt/preprocess.py` + **지정된 XML validator 파일 하나**에서만. validator는 고정 실행파일·고정 argv, `shell=True` 금지, xmllint 외 호출 금지 |
| `check_no_hardcoded_model` | 모델 이름 패턴에 `glm-[\w.-]*` 추가 |
| **신규** `check_legacy_does_not_import_project` | `vlogsrt/` 최상위는 `project`를 import 못 한다. **예외는 문서화된 CLI 조립 진입점 하나**(`vlogsrt/cli.py`). 소스 엔진과 `verify`는 여전히 불가 |
| **신규** `check_project_models_frozen` | `vlogsrt/project/models.py`의 dataclass는 전부 `frozen=True` |
| **개정(전역)** basename → **exact relative path** | 아래 참조 |

**basename 예외는 실제 우회 구멍이다** (명세의 "No bypass via renaming"과 직결). 실측:
```
scripts/check_invariants.py:34    if path.name == "transcribe.py":   continue   # openai 허용
scripts/check_invariants.py:265   if path.name == "preprocess.py":   continue   # subprocess 허용
scripts/check_invariants.py:303   if path.name == "config.py":       continue   # 모델 리터럴 허용
```
파일 **이름만** 본다. `vlogsrt/project/preprocess.py`를 만들면 subprocess가 무제한으로 열리고,
`vlogsrt/project/transcribe.py`를 만들면 openai가 열린다. 세 곳 모두
`path.relative_to(ROOT)` 기준 **정확 경로 화이트리스트**로 바꾼다
(`vlogsrt/transcribe.py`, `vlogsrt/preprocess.py`, `vlogsrt/config.py` + `vlogsrt/project/config.py`).
import 판정은 이미 있는 AST 헬퍼(`_imported_names`)를 쓴다 — 정규식으로 세지 마라.

**건드리지 않는 것:** `check_dependencies`. `[project.optional-dependencies]`는 검사 대상이 아니므로
`project = ["numpy","onnxruntime"]` extra는 그대로 통과한다(확인함). 대신 mypy의
`ignore_missing_imports` override에 `numpy.*`·`onnxruntime.*`를 추가해야 한다
(`no_site_packages = true`라 스텁이 없다).

**CLAUDE.md**: 이 프로젝트 확장을 위한 로컬 예외는 명세와 사용자 의도로 이미 승인되어 있다.
새 승인 루프는 필요 없다. 대신 **source ↔ project 경계를 `docs/DECISIONS.md`에 정확히 기록한다.**

### C10. S17 — 오프라인 합성 재현·수정은 **허용**, 실제 미디어 인수만 보류 (수정됨)

rev.1은 "이번 실행에서 S17을 고치지 마라"라고 했다. **과했다.** 알려진 임펄스/파형의 합성
오디오로 청크 디코딩과 PCM origin을 유료 STT 없이 시험할 수 있고(ffmpeg 9.0.1 로컬 사용 가능),
**측정된 오프라인 재현과 수정은 금지되지 않는다.**

여전히 강제되는 것:
- **`audio_origin`을 검증 없는 사용자 상수로 격하하지 마라.** rev.1의 P2a 문구("origin은 입력으로
  받아 전파만")는 이 점에서 정정한다 — 측정하고 검증한다.
- **상수 보정 금지는 그대로다.** `+0.2` 같은 magic offset을 코드에 넣는 순간 규칙 1과 D24 위반이다.
  차이를 **재고 기록하고 근거와 함께** 고쳐야 한다.
- 실제 77분 재현과 `make verify` 마지막 조각 0.0 인수는 **미디어 대기**다. `SKIP`을 `PASS`로
  기록하지 않는다.
- P4는 **검증된 오프라인 PCM 계약** 위에서 진행할 수 있다. 실제 미디어 인수를 P4의 선행조건으로
  걸지 마라.

### C11. VAD·vision 병렬은 공유 파일 두 개에서 충돌한다 — 유지 (승인됨)

둘 다 (a) 프로젝트 config 키와 (b) 프로젝트 `models.py`의 frozen 타입이 필요하다.

**규칙: 페이즈를 여는 contract 태스크가 `models.py`와 `config.py`를 혼자 쓰고, 그 페이즈 동안
두 파일은 동결된다.** 병렬 워커는 자기 하위 디렉터리(`vad/`, `vision/`)와 자기 테스트만 쓴다.
동결된 파일에 키가 하나라도 더 필요하면 병렬을 멈추고 contract 태스크를 다시 연다.
이번 실행에서 가장 확실한 머지 충돌 원인이다.

### C12. 네트워크 0은 **행위 기반 테스트**로 강제한다 — import 그래프가 아니다 (수정됨)

rev.1은 "`plan`/`export`/`captions` 경로가 vision·live 클라이언트를 import조차 하지 않게 하라"고
했다. **비현실적이다** — 소스 `transcribe.py`가 이미 SDK를 import하므로 전이적 import 금지는
성립하지 않는다.

교체:
- **행위 기반 네트워크 테스트.** `tests/conftest.py`가 이미 `socket.socket`·`create_connection`을
  autouse로 차단하고 위반 시 `NetworkBlocked`를 던진다(확인함). `plan`/`export`/`captions`를
  그 아래에서 끝까지 돌려 **소켓이 한 번도 열리지 않음**을 시험한다.
- 자격증명 분리: vision은 `OPENAI_API_KEY`/`OPENAI_BASE_URL`을 **재사용하지 않는다**(다른 서비스다).
- 명시적 오프라인 게이트: vision 경로도 `env.is_offline()`을 확인하고, `online=True`가 명시된
  경로에서만 실제 호출한다. fixture/live 2구현은 `get_transcriber`와 같은 모양으로 간다.

### C13. **부분 기각** — 회귀 커버리지는 이미 있다. 바이트 잠금을 **보강**하는 것이다

rev.1의 "소스 회귀를 잡을 자동 장치가 하나도 없다"는 **과장이었고 기각한다.** 실제로 존재하는 것:

| 테스트 | 무엇을 잡는가 |
|---|---|
| `test_pipeline.py::test_run_end_to_end` | 오프라인 전체 실행 → SRT 파싱·큐 수·번호 |
| `…::test_output_text_comes_straight_from_the_transcription` | 규칙 1 — 글자가 응답에서만 온다 |
| `…::test_every_segment_becomes_a_cue` | 규칙 9 — 세그먼트 누락 0 |
| `…::test_chunk_offsets_survive_into_the_srt` | 조각 오프셋이 SRT 시각까지 살아남는다 |
| `test_output.py::test_millisecond_rounds_half_up` · `test_roundtrip` | 직렬화 규칙 |
| 전체 | **389건, 전부 통과** |

남는 **유효한** 부분만 취한다:
- **바이트 단위 SRT 잠금이 없다.** 합성 픽스처(`demo`·`loopy`·`loopyletter`·`loopylong`)로
  오프라인 실행 결과 SRT를 커밋된 기대 파일과 **바이트로** 비교하는 테스트를 더한다.
  오프라인이라 결정적이다(온도 비결정성은 라이브 호출에만 있다).
- **"source immutability"의 나머지 절반**: 프로젝트 연산이 입력 `Trace`/`spine`/`cues`/산출물을
  변형하지 않는다는 것. 이건 각 페이즈에서 해당 기능이 도입될 때 **별도로** 시험한다.
  P0에서 전부 만들려 하지 마라.

---

## 3. 페이즈 게이트

모든 페이즈 공통 종료 조건:

```
G-ALL-1  make check PASS  (자식의 Python >= 3.11, 실제로는 3.12.x)
G-ALL-2  scripts/check_invariants.py 전 군 OK   (SKIP·미실행은 통과가 아니다)
G-ALL-3  SRT 바이트 회귀 테스트 PASS + 기존 389건 유지
G-ALL-4  python -c "import sys,vlogsrt;print(sys.version, vlogsrt.__file__)" 가
         해당 체크아웃 경로를 가리킨다
G-ALL-5  커밋 diff에 .env*, golden/**, tests/fixtures/api/** 가 없다.
         ※ 루트의 .env.example ' M'은 사용자의 기존 미스테이지 편집이다.
           자식 체크아웃에는 그 수정이 **없는 것이 정상**이며, 만들어내게 요구하지 않는다.
           내용은 누구도 열람·변경하지 않는다.
G-ALL-6  변경 파일이 brief의 allowed 목록 밖으로 한 개도 나가지 않았다
```

| 페이즈 | 진입 | 고유 종료 조건 | 병렬 |
|---|---|---|---|
| **P0** | 기준선 green | C9 검사기 개정 6건(+심어둔 위반 테스트), optional extra + mypy override, SRT 바이트 회귀, `vlogsrt/project/` config·types 골격, D34에 source↔project 경계 기록 | **직렬. 3개 태스크로 분할(§4)** |
| **P1** FCPXML | P0 머지 | `.fcpxml`/`.fcpxmld` 왕복이 untouched XML time string까지 보존; 미지원 구성(connected/compound/multicam/retime/transition/audio override/time-varying effect)은 XML 위치+사유로 **거부**(조용한 flatten 0건); resource ID가 입력 XML 해시로 스코프됨; fps 8종 합성 픽스처(30 vs 29.97 포함) 프레임 0 오차; `xmllint --dtdvalid` 통과 | **P2a와 병렬** |
| **P2a** PCM | P0 머지 | 16k PCM 저장소 + 해시 + 채널 정책; `file_time = audio_time + origin` 왕복; 샘플 경계 `ceil/floor`; **`audio_origin`은 측정·검증된 값**(무검증 상수 금지, magic offset 금지); 알려진 임펄스의 합성 오디오로 청크 디코딩/원점 시험 | **P1과 병렬** |
| **P2b** S17 실제 인수 | 사람이 미디어 + 유료 실행 제공 | 실제 77분 재현, `make verify` 마지막 조각 0.0, 원인이 D 문서에 기재 | **미디어 대기. SKIP≠PASS** |
| **P3** 어댑터 | P1+P2a 머지 | 자산별 fixture/work namespace(C4), 전역 env 변이 0건; success/empty/error 3상태 구별(C5/C6); 청크 커버리지 기록; 동일 파일명 자산 2개가 서로 덮어쓰지 않음; SOURCE SRT가 **유리수 반올림**(C7) | 직렬 |
| **P4** 경계 | P3 머지 + **검증된 오프라인 PCM 계약** | padding-only는 재전사 없이 clamp; 진짜 절단만 boundary task; **같은 저장 PCM에서만** 추출; `BoundaryTranscript` 불변 + 키에 (원본 해시, 유지 샘플 범위, 채널 정책, source Trace ref, model/config); 인스턴스 스코프 교체(다른 반복 인스턴스 무삭제); **자기 endpoint로 재귀 재전사 0건**; 실패 시 원문 유지 + 마커 | 직렬 |
| **P5a** VAD / **P5b** vision | P4 머지 + **contract 태스크 완료(C11)** | P5a: Silero v6.2.1 ONNX + SHA256, 채널 분리 후 union, 512/64 스텝, 상태 리셋, 마지막 창 zero-pad를 시간에서 제외, 의존성/모델 부재는 **차단**(무음 강등 금지), 보호 비율 정량 보고. P5b: 후보 ID/`keep_all`/`uncertain`만 수용, 미지 ID·NaN·구조 이상 거부, 모델에 XML/시간/경로/mute 권한 없음, 자격증명 분리 + 행위 기반 네트워크 테스트(C12) | **2워커 병렬** |
| **P6** 편집/export | P5 머지 | 저장된 분석+config에서 **순수·결정적**; guard 제거 0건; 새 부분 큐 0건; 블록당 후보 1개; ripple 후 실제 스냅된 컷을 guard와 재검증; API 실패 블록은 보존+리뷰; EDIT SRT 유리수 반올림 + 공유 경계 동일값 | 직렬 |
| **P7** 영속/CLI/UI | P6 머지 | 콘텐츠 키 워크스페이스, 원자적 발행, stale plan 거부, cancel/resume, `plan`/`export`/`captions` **행위 기반 네트워크 0**, 사용자 재컷 reproject | 부분 병렬 |
| **인수** | P7 + 사람 미디어 | 실제 FCP import→reexport, 4K relink/재생, 제거된 모든 오디오 구간 감사 | **미디어 대기** |

---

## 4. P0 — 3개 직렬 태스크 (한 태스크에 13건을 몰지 마라)

공통: base `feat/final-cut-project`, 자식 worktree, **워커 1명**, 병렬 금지.
진입 시 `uv venv --python 3.12 .venv && uv pip install -e ".[dev]" && uv pip check`.
보고에 `sys.version` + `vlogsrt.__file__`, `git show --stat HEAD`, `git status --porcelain` 3줄 필수.

### P0-1 계약 검사기 + optional extra
허용 파일: `scripts/check_invariants.py`, `tests/test_invariants.py`, `pyproject.toml`,
`vlogsrt/project/__init__.py`, `docs/DECISIONS.md`(D34 추가만).
- C9의 개정 6건 + basename→exact-path 전환. AST 헬퍼 재사용.
- 각 규칙마다 **심어둔 위반이 잡히는지** 먼저 실패하는 테스트를 쓴다(`tmp_path`에 가짜 트리;
  실제 소스를 더럽히지 마라).
- `[project.optional-dependencies] project = [numpy, onnxruntime]`, mypy override 추가.
  **`[project] dependencies`는 건드리지 마라** (`check_dependencies`가 정확히 3개를 요구한다).
- D34에 source↔project 경계와 예외 근거를 기록한다.
수용: `make check` PASS · `check_invariants.py` 전 군 `[OK]` exit 0 · 심어둔 위반 테스트 통과 ·
기존 389건 유지.

### P0-2 SRT 바이트 회귀 잠금
허용 파일: `tests/test_source_immutability.py`(신규), `tests/fixtures/synthetic/**`(기대 SRT 추가만).
- 합성 픽스처 4종으로 오프라인 실행 → SRT 바이트를 커밋된 기대 파일과 비교. 기대 파일은
  **현 기준선 출력**으로 생성한다.
- 기대 파일이 없으므로 **먼저 실패해야 한다.**
수용: 신규 테스트 통과 · 기존 389건 유지 · 소스 코드 변경 0.
이후 이 파일이 바뀌는 커밋은 자동 반려이며, 바꿔야 할 근거는 Astra 결정 + `DECISIONS.md` 기재가 선행.

### P0-3 프로젝트 config·types 골격
허용 파일: `vlogsrt/project/{config,models}.py`, 대응 테스트, `vlogsrt.project.toml`(신규).
- `ProjectConfig`(frozen) + `vlogsrt.project.toml` 로더. **`Config`에 키를 추가하지 마라.**
- `vlogsrt/project/models.py`의 dataclass는 전부 `frozen=True`(P0-1의 검사가 이를 강제한다).
수용: `make check` PASS · 알 수 없는 키에 대한 명시적 오류 테스트 · 기존 389건 유지.

**전 태스크 공통 반려 사유:** 기존 테스트 기대값 변경 / 임계값 변경 / `Config` 필드 추가 /
`[project] dependencies` 변경 / 검사기 화이트리스트를 지정 경로보다 넓게 여는 것 /
`.env*`·`golden/**`·`tests/fixtures/api/**` 접근 / 테스트 skip·xfail.

**실패 경로:** 재현 명령 + exit code + 짧은 로그 + 기준 커밋 + 변경 파일 + 시도한 가설을 붙여
Sol에게. Sol → Terra/Luna → Opus5 → Astra. 코드 수정은 언제나 GLM.

---

## 5. 병렬 경계 (파일 소유권)

```
vlogsrt/project/
  __init__.py          P0-1
  models.py            페이즈 여는 contract 태스크 전용. 그 뒤 페이즈 동안 동결
  config.py            동상 (P0-3에서 최초 생성)
  fcpxml/              P1  read.py  write.py  validate.py(지정된 xmllint 호출 지점, 고정 argv)
  timing.py            P1  Fraction · 프레임 그리드 · 시간 대수 · **유리수 ms 반올림**(C7)
  audio.py             P2a PCM 저장소 · 해시 · 측정된 audio_origin
  source.py            P3  자산별 어댑터 → source Trace + SOURCE SRT
  boundary.py          P4  경계 전사 · 투영
  vad/                 P5a numpy/onnxruntime 유일 허용 지점
  vision/              P5b **openai를 직접 import하지 않는다** — 전송은 vlogsrt/transcribe.py
  editplan.py          P6
  export.py            P6
  store.py             P7
  cli.py               P7  (레거시 `vlogsrt/cli.py`가 project를 import할 수 있는 유일한 진입점)
```

안전한 동시 실행 쌍: **(P1 `fcpxml/`·`timing.py`) ↔ (P2a `audio.py`)**,
**(P5a `vad/`) ↔ (P5b `vision/`)** — 단 contract 태스크가 `models.py`·`config.py`를 닫은 뒤에만.

동시 불가: P0(공유 인프라), P3(P4의 계약 생산자), P6(둘의 소비자).
`tests/test_invariants.py`·`test_source_immutability.py`는 P0 이후 **공유 자산**이므로 이후
페이즈에서 손대려면 Sol 승인 + 직렬화가 필요하다.

---

## 6. 실행 정책 제안 (rev.2)

1. **P0는 직렬**, `max_concurrent_coding_workers: 2`는 P1부터 적용.
2. **`blocked_acceptance`를 `workflow.json`에 추가한다** — 실제 미디어가 필요한 항목만:
   `make verify`(캐시 없음), `make eval`(golden 없음), S17 실제 77분 재현/인수,
   실제 FCP import-reexport, 4K relink, 파일럿 감사, 라이브 STT/vision 픽스처 녹화.
   **이 항목이 DoD에 들어간 brief는 GLM에 디스패치하지 않는다.** SKIP≠PASS를 디스패치 규칙으로 만든다.
   ※ 합성 오디오 기반 오프라인 S17 작업은 이 목록에 **넣지 않는다**(C10).
3. **`toolchain` 블록 추가**: `python_dev: 3.12.14`, `runtime_floor: 3.11`,
   `bootstrap: "uv venv --python 3.12 .venv && uv pip install -e '.[dev]' && uv pip check"`,
   `ffmpeg: 9.0.1`, `xmllint: /usr/bin/xmllint`,
   `note: "make는 명령행 PY 오버라이드를 이미 지원한다 (make -n test PY=… 확인)"`.
4. **모든 GLM 보고에 3줄 강제**: `sys.version` + `vlogsrt.__file__`, `git show --stat HEAD`,
   `git status --porcelain`. C2·G-ALL-5의 증거다.
5. `docs/orchestration/reviews/`는 Opus 소유. Sol과 같은 체크아웃을 공유하므로
   `tasks/`·`progress.md`는 건드리지 않는다. 이 리뷰 문서의 커밋은 root가 소유한다.
6. **철회**(rev.1의 5·6번): Makefile 리팩터 요구, CLAUDE.md 사용자 승인 루프.
   후자는 명세·사용자 의도로 이미 승인되어 있고, `DECISIONS.md`의 경계 기록으로 갈음한다.

---

## 7. 미확인으로 남긴 것

- `claude-glm` wrapper가 자식의 `.claude/settings.json`(deny 규칙 + `VLOGSRT_OFFLINE=1`)을 실제로
  적용하는지. `.claude/settings.json`은 git 추적 중이라 자식에 **파일은 존재한다**(확인함).
  첫 GLM launch receipt에서 확인할 것.
- `glm-5.3-flash`의 실제 계정 권한과 vision 응답 형태. 명세도 미검증으로 적었다.
- Silero v6.2.1 ONNX 아티팩트와 SHA256. 저장소에 없다.
- ffmpeg 9.0.1에서 S17의 `-c copy` 마지막 조각 +0.2초가 재현되는지. **합성 오디오로 시험 가능하며
  P2a의 일이다** — 실제 77분 재현만 미디어 대기다.
