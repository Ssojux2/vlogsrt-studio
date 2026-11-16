# vlogsrt-studio 성능 최적화 계획

## Context

사용자가 프로그램이 느리다고 했다. 느리게 열리는 곳, 오래 걸리는 단계, 시스템 환경에 맞는 병렬 처리를 분석해 달라고 했다.
분석은 Fable 에이전트 셋이 나눠 맡았다(Agent 도구의 `fable` — 이 환경에서 고를 수 있는 판은 Fable 5.2이고, 5.1은 고를 수 없다).
맡은 영역은 ① UI 로딩·응답, ② 로컬 미디어(ffmpeg)·병렬, ③ API·작업 조율이다.
**셋 다 코드를 읽기만 했고 아무것도 재지 않았다.** 그래서 아래 순위는 구조상의 추정이다. Phase 0 측정으로 확정한다.
핵심 주장은 직접 열어 확인했다(파일:줄 표시).

대상 기계: Apple M1 Pro(P코어 8 + E코어 2 = 10코어) · 32 GB · ffmpeg 9.0.1(videotoolbox 있음).

## 지금 구조 요약 (확인됨)

- **작업 스레드:** 작업 하나마다 데몬 스레드 하나를 띄우고 상한이 없다(`vlogsrt/project/raw/jobs.py:426`).
- **로컬 미디어 자리:** `LocalMediaConcurrency.limit` = `max(1, min(3, cpu//2))`라 이 기계에서 3이다(`raw/config.py:1873`).
  - 이 자리를 잡는 일은 프록시·미리보기·렌더뿐이다.
  - 장면 검출·프레임·Gemini 클립·PCM·썸네일·ffprobe는 자리 밖에서 돈다.
- **분석 동시 한도:** `parallel_analysis_limit = 3`이 따로 있다(`analysis_config.py:815`).
  - 로컬 미디어 자리와 별개라 두 한도가 겹쳐 쌓인다.
  - 영상 안에서는 모두 직렬이다(1단계 → 2단계).
  - 그래서 동시에 나가는 API 호출은 많아야 3개다.
- **하드웨어 가속:** VideoToolbox는 **인코드에만** 쓴다. `-hwaccel`은 코드 어디에도 없어 4K HEVC 디코드가 전부 소프트웨어다.
- **Gemini 의미 분석:** 창(15초)마다 하나씩 직렬로 보낸다(`understanding_luna.py:1141-1166`). 10분 영상이면 약 40회를 차례로 기다린다.
  - 창 격자 자체는 `timeline.extent` 고정 폭이다(`understanding.py:1463`).
  - 다만 장면 계획은 발화(`utterances`)를 받는다(`analysis_workflow.py:1821`). 그래서 「장면 검출을 전사와 동시에」는 계획에서 뺐다.
- **UI:**
  - FastAPI + uvicorn + Gradio 6.30이다.
  - `_visual_query`가 언제나 `include_all_sources=True`다(`ui/workspace_service.py:7520`). 그려질 때마다 모든 영상의 자막·제안·스냅·리필 뷰를 만들고, JSON 한 덩어리로 보낸다.
  - Gradio 큐 이벤트의 기본 동시 실행은 이벤트마다 1이다(gradio `blocks.py:2954`). 2초 그리기 틱이 2초를 넘기면 줄이 밀린다.
  - `/media`는 `Cache-Control: no-store`라(`:12381`) 다시 그릴 때마다 썸네일을 다시 받는다. — **실측으로 틀렸다(D133)**: 같은 문서 안에서는 브라우저가 받은 그림을 다시 쓰고, 다시 받는 것은 새로고침뿐이다.
- **이미 있는 측정 도구:**
  - `scripts/bench_workbench.py --profile`
  - `tests/raw/test_render_cost.py`(호출 수 상한)
  - `preprocess.run_process` 등록표(D61)

## Phase 0 — 측정 먼저 (코드 변경 최소)

1. 실제 작업공간의 **복사본**에 `scripts/bench_workbench.py --workspace <복사본> --profile`을 돌린다. 렌더·카드 클릭·틱의 시간, payload 바이트, 호출 수를 남긴다.
2. `preprocess.run_process` 등록표에 단계별 경과 시간 기록을 더한다(종류 · 원본 · argv 종류 · 초).
   - `/vl-activity`와 서버 로그에 보인다.
   - 표시만 하고 어떤 지문에도 들지 않는다.
3. 서버 미들웨어(`host_origin_guard`, `ui/workspace_service.py:12232`)에 `Server-Timing` 헤더와 응답 바이트를 기록한다.
4. 카운터를 단다: `_tier_report_for_row`·`_read_stage1_audio` 호출 수, 전체 해시 횟수·바이트(`source_verify`, `_sha256_file`).
5. 분석 한 편의 시간표를 남긴다.
   - 단계: 해시 → PCM → 전사 청크 → 장면 → 프레임 → 클립 → 창 요청.
   - 이 기록은 `request_log` 시각에서 오프라인으로 뽑는다. 유료 실행은 사람이 한 기록만 쓴다.

**DoD:** 측정 보고서(`docs/perf/2026-11-baseline.md`)를 쓰고, 아래 Phase들의 순위를 실측으로 다시 매긴다.

## Phase 1 — 시스템 환경 인지 자원 예산 (사용자 요청의 핵심)

병렬을 더 늘리기 **전에** 먼저 한다. 지금도 CPU가 넘친다.
- 한도 3 두 개가 겹친다.
- 자리 밖 ffmpeg가 각자 10스레드로 돈다.
- D-기록에 부하 80–100, 프록시 1800초 초과가 남아 있다.

- **새 순수 모듈 `vlogsrt/project/raw/system_profile.py`** (표준 라이브러리만)
  - macOS: `sysctl hw.perflevel0.physicalcpu`·`hw.perflevel1.physicalcpu`·`hw.memsize`를 `subprocess`로 읽는다.
    - 규칙 7을 지키려면 `preprocess.py`나 `oscompat.py` 같은 경계로 둔다. 자리는 구현 때 invariant 검사로 확정한다.
  - Linux: `os.sched_getaffinity`, `os.sysconf('SC_PHYS_PAGES')`
  - Windows: `os.cpu_count()`, 메모리는 모름(None)
  - 결과는 얼린 dataclass `SystemProfile(p_cores, e_cores, logical, mem_bytes, hw_decode: bool)`로, 프로세스당 한 번 잰다.
- **`LocalMediaConcurrency` 확장** (`raw/config.py`)
  - 새 칸(외부 근거 없는 조정 대상, D-기록과 CLAUDE.md 표 줄을 함께 남긴다):
    - `threads_per_4k_decode`
    - `mem_per_4k_job_bytes`
    - `use_performance_cores`
  - 한도 = `max(floor, min(ceiling, p_cores // threads_per_job, mem // mem_per_job))`
  - 지금 값으로 두면 결과가 예전과 같다. 이것을 시험으로 못 박는다.
- **공유 CPU 예산** — 분석 동시 한도(`RawAnalysisConcurrency`)와 로컬 미디어 자리를 한 예산 아래 둔다.
  - 지금 자리 밖인 장면·프레임·클립·PCM·썸네일 ffmpeg도 자리(또는 가벼운 자리)를 잡게 한다.
  - 자리를 잡아도 argv와 바이트는 그대로다.
- **ffmpeg 스레드 상한** — 자리를 잡는 디코드에 `-threads`를 주는 일은 바이트 영향이 없는 출력(미리보기·렌더)에만 한다.
  - 프록시·클립·프레임은 바이트가 요청 신원이라 손대지 않는다(아래 결정 D-4).
- **설정 노출** — `vlogsrt.project.toml`이나 설정 서랍에 「동시 작업 수: 자동 / N」을 둔다. `RawWorkflowConfig` 밖이라 견적 digest와 무관하다.

## Phase 2 — UI 로딩·클릭 지연

1. **모델 나누기** (가장 큼)
   - 모든 영상에는 가벼운 카드·레일 정보만 보낸다.
   - 무거운 상세(`subtitles`·`suggestions`·`refill`·`snap`·`relisten`)는 **선택된 영상만** 보낸다.
   - 위치: `ui/workspace_service.py:6981-6991`, `_annotate_transcript`(`:8088`), `ui/ui_facade.py:1319` `_model_holder`
   - 카드 클릭 때 그 영상 상세를 따로 받는다.
   - 기존 `test_render_cost.py` 상한을 낮추고 payload 바이트 상한 시험을 더한다.
2. **카드 클릭 한 번에 전체 렌더가 3–5번 일어나는 것 줄이기** (`vl_select_source` → `_start_quick` → `_capcut_result(upkeep=True)`)
   - 이미 분석된 영상은 클라이언트에서 바로 바꾼다.
   - `selection_*`·`source_preview` 작업 상태는 보이는 단계가 바뀔 때만 다시 그리기 열쇠(`_tick_key`, `:4446`)에 넣는다.
3. **같은 그리기 안에서 두 번 읽지 않기**
   - `_tier_report_for_row`(`service.py:2391`)와 `_latest_analysis_document`를 기존 `read_scope` 안에서 `(root, revision, asset)`로 기억한다.
   - 등급은 여전히 매번 계산하고 저장하지 않는다(D51·D52 그대로). `check_invariants.py`의 `classify` 호출자 제약에 맞게 감싼다.
   - `tier_evidence_rows`의 리비전마다 질의를 질의 하나로 바꾼다.
4. **썸네일**
   - `<img loading="lazy" decoding="async">`
   - 썸네일 경로만 `Cache-Control: private, max-age` + ETag `(size, mtime_ns)`를 준다. 아이디가 내용 열쇠라 안전하다.
   - 카드 목록이 같으면 `innerHTML`을 다시 짓지 않는다.
5. **`refresh_outputs`** (`:11898`) — 출력 23개를 `read_scope.open()` 하나로 감싼다. 숨은 서랍 판은 처음 열 때 만든다.
6. **유휴 수다** — 토스트 틱을 상태 틱에 합친다. 도는 작업이 없으면 그리기 틱을 늦춘다.
7. **(선택)** jobs 색인 `jobs(created_at)`·`jobs(status)`. Phase 0 측정이 필요하다고 보일 때만 한다.

## Phase 3 — 로컬 미디어 (바이트 그대로인 것부터)

1. **같은 원본을 거듭 전체 해시하지 않기**
   - `analysis_workflow.py:1188`(두 단계 각각 `trust_stat=False`)과 `understanding.py:143` `select_audio`가 같은 파일을 다시 읽는다.
   - 방금 잰 digest를 넘겨받는다. 유료 단계마다 전체 해시 **한 번**은 그대로다(D62).
   - 불러오기(`reference_ingest.py:53`)는 SSD에서 2–3개를 병렬로 해시한다.
2. **패킷 스캔 기억 쓰기** — 검증을 마친 원본에 대해서는 `probe_extraction_clock_memo`를 쓴다.
   - 자리: `analysis_workflow.py:1189`, `media_refs.py:533`, `visual_review.py:1861`, `sync_report.py:532,1148`
   - `probe_audio_extents`에 `-select_streams a`를 붙인다. 출력이 같은지 시험한다.
3. **Gemini 클립**(`analysis_workflow.py:1992`)
   - 컨테이너 시작 ffprobe를 원본당 한 번으로 줄인다.
   - 작은 풀로 병렬 인코드하고, 각자 자리를 잡는다. argv가 같아 bitexact 바이트도 같다.
4. **불러오기 순서** — 해시가 끝난 파일부터 바로 프록시를 시작한다(`service.py:2700`). 미리보기는 원본마다 나눠 자리 수만큼 동시에 만든다(`visual_review.py:1705`).
5. **미리보기·렌더 조각 디코드에 `-hwaccel videotoolbox`** — macOS이고 `hw_decode`가 참일 때만 붙인다.
   - 이 바이트는 어떤 지문에도 없다.
   - 실패하면 소프트웨어로 한 번 더 하고, 실패를 (코덱·화소형·크기)로 기억해 다음부터 건너뛴다(`preprocess.py:4800` 재시도 개선).
6. **프록시 검증 표본**(`media_refs.py:139-183`) — 점마다 프로세스를 띄우던 것을 파일당 ffmpeg 하나로 묶는다. 결과 바이트가 같은지 시험한다.
7. **작은 순수 파이썬 개선** — `caption_workspace.py:1342` `_peak_bins`는 배열 조각 `max`/`min`으로, VAD 블록(`vad/inference.py:123`)은 `numpy.frombuffer`로 바꾼다. 측정에서 보일 때만 한다.
8. **스파이크(조사만)**
   - 4K 미리보기를 프록시 영상 스트림 복사 + 원본 소리로 만드는 안. 회전·`-copyts`·`video_origin`·화질이 걸린다.
   - 장면 + 프레임을 한 번에 디코드하는 안. 장면 계획이 발화에 기대므로 순서를 확인해야 한다.

## Phase 4 — 분석 파이프라인 (API 대기)

1. **창 병렬 전송** (`understanding_luna.py:1141`)
   - 묶인 풀을 쓰고, 워커마다 `contextvars.copy_context`로 실행한다(`render_export.py:345` 패턴).
   - 결과는 차례 번호로 모은다. `on_annotations`는 **앞에서부터 이어진 접두만** 알린다(진행 의미 보존).
   - `CountingReservationGate`(`:1421`)에 잠금을 둔다.
   - 공급자별 동시 호출 세마포어 `RawProviderConcurrency`를 `RawAnalysisConcurrency` 옆에 두고 기본값을 정한다. **1이면 직렬과 바이트가 같다**는 시험을 둔다.
   - 승인된 창 수·요청 신원·비용은 그대로다.
2. **창 payload를 다섯 번 짓던 것을 한 번으로** (`understanding_luna.py:379`, `analysis_workflow.py:2077`) — 창마다 `(sha, cost bound)`를 한 번 재서 다시 쓰고, 보내기 직전에 한 번만 직렬화한다. GIL을 덜 잡아 다른 영상 스레드와 UI도 덜 막는다.
3. **1단계 청크 병렬** (`understanding.py:407`) — 같은 세마포어를 쓰고 `CountingAsrGate`(`analysis_workflow.py:463`)에 잠금을 둔다. 규칙 6은 그대로다(프롬프트를 더하지 않는다).
4. **클립 선인코드를 1단계 동안 하기** — 창 격자가 고정 폭이라 PCM 직후 범위를 안다.
   - 「전사만」 범위의 승인에서는 하지 않는다. 그 클립은 버리게 된다.
5. **캐시 열쇠 안정화**
   - 청크 열쇠의 `type(provider).__name__`(`understanding.py:455`)을 고정 신원 문자열로 바꾼다.
   - 창 캐시 폴더 열쇠를 분석 모델로만 좁힌다(`analysis_workflow.py:2062`).
   - 이 일은 **기존 캐시 열쇠를 바꾸므로** 옛 열쇠도 읽는 이행이 필요하다 → 결정 D-3.
6. **작은 것**
   - 일괄 승인에서 영상마다 라이브러리 전체 `unanalyzed` upsert가 O(N²)이다(`analysis_workflow.py:1008`). 그 영상 행만 쓴다.
   - 영어 트랙 묶음을 병렬로 보낸다(`english_track.py:211`).

## 결정이 필요한 것 (CLAUDE.md — 바꾸기 전에 보고)

- **D-1 Gemini 429/529 재시도** — **정함(2026-11-16, D141)**: 공급자가 대기를 말한 Gemini 분당 한도 429만 재시도한다(W-81). Claude는 그대로다.
  - 지금 Gemini·Claude는 한 번만 보내고 재시도하지 않는다(문서화된 결정, `gemini_messages.py` 머리).
  - 창 병렬을 켜면 429가 늘어 「부분 완료」가 늘 수 있다.
  - 제안: 과금되지 않는 429/529만, `Retry-After`를 지켜, 상수는 config에 두고 재시도한다. 503·타임아웃은 재시도하지 않는다.
- **D-2 요청 사이 캐시** — 등급 보고서·1단계 기록의 프로세스 기억(`(mtime_ns, size)` 열쇠)은 D63 G의 「기억은 요청을 넘지 않는다」와 부딪힌다.
- **D-3 캐시 열쇠 변경** — Phase 4-5는 한 번은 다시 사지 않도록 이행이 필요하다.
- **D-4 바이트 신원 입력** — 프록시·프레임·클립·썸네일에 하드웨어 디코드나 `-threads`를 쓰면 바이트가 바뀐다. 조리법 판을 올려 캐시가 무효가 되고 재과금된다. 기본 제안은 손대지 않는 것이다.

## 태스크 분할 (한 세션 한 태스크, 시험 먼저, DoD = `make check`)

**우선순위(사용자 답):** 가장 아픈 곳은 **UI 클릭·화면 로딩**과 **불러오기·미리보기 만들기**다. 사람이 실제 작업공간 복사본을 준다.
그래서 Phase 2와 Phase 1·3을 먼저 하고, Phase 4(분석 API 대기)는 뒤로 미룬다.

| 순서 | 태스크 | 비고 |
|---|---|---|
| 1 | Phase 0 측정 도구 + 기준 보고서 | 사람이 준 실제 작업공간 **복사본**에서 잰다(원본 작업공간은 건드리지 않는다) |
| 2 | UI 모델 나누기 | payload 바이트 시험 |
| 3 | 카드 클릭 재렌더 줄이기 + 그리기 안 기억 | |
| 4 | 썸네일 캐시·lazy + `refresh_outputs` scope | |
| 5 | `SystemProfile` + `LocalMediaConcurrency` 확장 + 공유 예산 | 기본값이면 예전과 같음 |
| 6 | 불러오기 순서(해시 끝난 것부터 프록시) + 미리보기 원본별 병렬 + 병렬 해시 | |
| 7 | 미리보기·렌더 hwaccel (macOS 조건부) + 실패 기억 | |
| 8 | 해시·패킷 스캔 중복 제거 + 프록시 검증 표본 묶기 | |
| 9 | 클립 풀 · 창 payload 한 번 짓기 | |
| 10 | 창·청크 병렬 + 공급자 세마포어 | D-1과 함께 |
| 11 | 클립 선인코드 · 캐시 열쇠 안정화 | D-3 |

Phase 0 측정 결과가 위 순서와 다르게 나오면 2–8번의 순서는 실측대로 다시 매긴다.

**2026-11-13 Phase 0 완료(W-69 · D120)** — 실측 순위는 `docs/perf/2026-11-baseline.md` §5다. 가장 먼저는 원본 조사 기억 크기(카드 누르기마다 ffprobe 약 1000번)이고 그다음이 UI 모델 나누기다. TASKS 번호는 W-58이 아니라 W-69부터 이어진다.

**진행(2026-11-13)** — 1순위 원본 조사 기억(W-70 · D121) · 2순위 UI 모델 나누기(W-71 · D122) · 3순위 그리기 안 기억(W-72 · D123) · 4순위 카드 누르기(W-73 · D124 — 재그리기보다 큰 몫이던 배경 다시 게시를 원본별 근거 기록으로 없앴다) 끝. 5순위(W-74 · D133 — 썸네일 검증자·lazy만 하고 `refresh_outputs` 범위·카드 목록 재사용은 실측으로 접었다) 끝. 6순위는 첫 걸음(W-76 · D135 — 계획의 원본 조사를 함께, 첫 미리보기 쪽 앞 시간을 반으로)을 마쳤고, 둘째 걸음(W-77 · D136 — 쪽 준비를 원본마다 함께 · 화면 일이 자리를 먼저)도 마쳤다. 셋째 걸음(W-78 · D137 — 프록시 검증의 프록시 쪽 표본을 한 프로세스로, Phase 3-6)도 마쳤다. Phase 1은 측정으로 정리했다(W-79 · D139 — 자리 셋에서 CPU 94%, 넷·다섯은 빨라지지 않아 새 계층을 만들지 않는다; 분석 중 겹침은 유료라 사람 측정). 렌더·번인 자리 우선순위도 마쳤다(W-80 · D140). 결정 D-1은 「공급자가 말한 대기만 재시도」로 정해져 Gemini 분당 한도 429 재시도를 넣었다(W-81 · D141 — Claude 그대로). 남은 것은 §5 7순위(의미 분석 창 병렬 전송 — 기본 동시 수는 사용자가 정한다)와 분석 중 겹침의 사람 측정(D139)이다.

새 config 값마다 `docs/DECISIONS.md`에 D-기록을 남기고, CLAUDE.md 임계값 표에 줄을 더한다. TASKS.md에는 W-58부터 올린다.

## 검증

- **매 태스크:** `make check`(lint·type·test·invariant). 병렬 손잡이는 1일 때 바이트가 같다는 시험, `test_render_cost.py` 상한 시험을 둔다.
- **UI:**
  - 실제 작업공간 복사본에 `scripts/bench_workbench.py --profile`로 전후를 비교한다.
  - 브라우저 패널(`.claude/launch.json`, **오프라인** 앱)에서 카드 클릭 네트워크 payload, 썸네일 재요청 수를 확인한다.
- **로컬 미디어:**
  - Phase 0의 `run_process` 경과 기록으로 불러오기·미리보기·프록시 전후를 비교한다(무료 로컬 단계라 에이전트도 오프라인으로 잴 수 있다).
  - 부하는 `top`으로 본다.
- **분석 API 지연:** 온라인 실행이 필요해 **사람만 잴 수 있다**. 에이전트는 픽스처 기반 시험으로 순서·바이트·호출 수가 같음만 증명한다.
