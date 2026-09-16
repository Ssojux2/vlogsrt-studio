# PROTO-3 — 러너 2/3: 전사 · 자막 투영 · 경계 · VAD · 가드

> **v2.** v1이 방향 검토 둘을 거쳤다. 둘 다 **SOUND_WITH_CONCERNS**이고 **둘 다 내
> 안전 주장 하나를 반박했다** — 같은 것을 독립으로 재현한 수렴이라 가장 강한 신호다.
> §9에 기록. 전문: `../reviews/opus-proto-3-direction.md`, `../reviews/glm-proto-3-direction.md`.

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 **`d8eed17`** (트리 깨끗, pytest 2459 passed)
venv: `.venv/bin/python`

**이 머신의 `/usr/bin` shim이 Xcode 라이선스 미동의로 전부 죽어 있다**(exit 69).
`git`도 `python3`도 마찬가지다: `/Library/Developer/CommandLineTools/usr/bin/git`,
파이썬은 `.venv/bin/python`.

## 소유 파일 — 정확히 둘

- `vlogsrt/project/runner.py` (**기존 파일에 추가**. PROTO-2의 `prepare_audio`·
  `PreparedAudio`·`pcm_destination`은 **고치지 마라** — 하류 계약이다)
- `tests/project/test_runner_analysis.py` (**신규**)

이 둘 밖은 범위 밖이다. 특히 `vlogsrt/project/**`의 기존 모듈, `tests/fixtures/**`,
`golden/**`, 임계값, 그리고 `tests/project/test_runner_audio.py`.

---

## 1. 이 단계가 하는 일

`PreparedAudio`를 받아 **분석이 끝난 상태**를 만든다.

```
PreparedAudio
  → ★ ONNX 게이트 — 유료 호출 **앞**이다 (§1-a)
      create_cpu_session(bundled_artifact(project_config))   runtime.py:79,115
  → ★ references = source_id로 유일화        (§4-a)
  → transcribe_unique_sources(...)         speech.py:722    소스 전사
  → project_source_captions(...)           captions.py:380  SOURCE SRT 초안
  → logical_slices(document, media)        boundary.py:226
  → plan_boundaries(...)                   boundary.py:949
  → transcribe_boundaries(...)             boundary.py:1116 경계 재전사
  → project_boundaries(...)                boundary.py:2228 BoundaryProjection
  → infer_vad(reference, source_id, cfg, session)  vad/inference.py:86
  → build_source_guard(trace, reference, vad, boundary, repairs=(), cfg=...)
                                           vad/guard.py:160  ← `repairs=()` (§1-b)
  → project_guard(...)                     vad/guard.py:212  InstanceGuard들
  → ★ 문서 클립 순서로 재배열               (§4-c)
```

### 1-a. ONNX 게이트는 유료 호출 **앞**이다

v1은 세션 생성을 전사 **뒤**에 놓았다 — 그러면 아티팩트가 깨진 환경에서 **77분 회차
전사비를 다 치르고 나서** `VadUnavailable`로 죽고 만든 SOURCE SRT까지 버린다.
`bundled_artifact`는 해시 검증 하나라 공짜다. PROTO-2가 `require_ffmpeg`를 자원 루프
앞으로 뺀 것과 **같은 논리**인데 v1이 자기 유비를 어겼다.

조건도 같은 모양으로: **`PcmReference`를 가진 소스가 하나라도 있을 때만** 묻는다.
전 리소스가 `declared_no_audio`인 프로젝트는 VAD를 한 번도 안 쓰므로,
무조건 만들면 `numpy`/`onnxruntime` extra가 없는 환경에서 쓰지도 않을 이유로 죽는다.

### 1-b. `build_source_guard`의 `repairs`는 `()`다

`guard.py:168-174`가 경고한다 — *"`repairs`는 **디코드 오디오 국소 시계(초)**의 재전사
기록만 받는다. 경계 재전사 기록(`BoundaryTranscript.repairs`, 남긴 범위 국소 시계)을
여기 넣지 마라: 시계가 다른 기록은 엉뚱한 자리에 보호를 놓는다."*

이 단계는 바로 앞에서 `transcribe_boundaries`를 돌려 **손 닿는 곳에 틀린 값**을 쥐고 있다.
`_repair_spans`(`guard.py:294-305`)가 이미 `trace.repairs`를 합치므로 **정답은 `()` 하나뿐이다.**

산출물은 PROTO-4(`partition_blocks` 이후)가 그대로 먹는 동결 dataclass 하나다.

## 2. 이 태스크의 핵심 위험 — 가드가 없는 인스턴스

**`build_source_guard`는 `SourceTrace`와 `BoundaryProjection`을 필수로 받는다**
(`guard.py:160-167`). 그러므로 **`UnavailableSourceAudio`인 소스는 가드를 만들 수 없다.**

그런데 하류는 **인스턴스마다 가드가 정확히 하나**일 것을 요구한다 —
`partition_blocks`의 `_require_guard_linkage`(`edit.py:158`, docstring: *"문서의 클립
인스턴스마다 guard가 정확히 하나여야 하고"*).

> **여기가 `protected=[]` 계열의 일곱 번째가 태어날 자리다.**
> 가드를 **안 만들고 넘기면** 하류가 죽거나(운이 좋을 때), 가드를 **빈 `protected`로
> `status="success"`로 만들면** 그 인스턴스는 **제거 권한이 생긴다.**
> 소리를 못 들은 클립이 "보호할 것이 없는 클립"이 되는 것 —
> 이 저장소에서 여섯 번 재발한 바로 그 결함이다.

### 반드시 이렇게 만든다

부재 소스의 인스턴스마다 **합성 `InstanceGuard`를 명시적으로 만든다.**

| 부재 `reason_code` | `InstanceGuard.status` | 왜 |
|---|---|---|
| `declared_no_audio` | **`"inaudible"`** | 문서가 소리 없다고 선언했다. 가청이 아니므로 블록을 안 낸다 |
| `stream_unavailable` | **`"keep_all"`** | 들을 수 **있었어야 하는데** 못 들었다. 실패지 무음이 아니다 |
| `pcm_unavailable` | **`"keep_all"`** | 위와 같다 |

`protected_frames`는 셋 다 `()`로 둔다 — **그리고 그것이 안전한 이유는 `status`가
막기 때문이지 `protected`가 비어서가 아니다**(`edit.py:968-972`: *"keep_all·inaudible은
빈 protected여도 제거 권한이 없다"*).

**`status="success"`인 합성 가드를 만들지 마라. 어떤 이유로도.**

### 2-a. 하류가 지켜준다고 믿지 마라 — v1이 든 근거는 셋 중 하나에서만 참이다

v1은 *"`declared_no_audio`를 `keep_all`로 잘못 찍으면 그 자리에서 터진다"* 고 썼다.
**두 검토자가 독립으로 반박했고 둘 다 재현했다.** `_require_audible_link`
(`edit.py:409-419`)는 **`success`만** 거부하고, 자기 docstring이 이유를 적어 놓았다 —
*"이 방향만 닫는다: 제거를 **넓히는** 모순이므로. keep_all·inaudible이 불가청 클립에
붙는 것은 보수 방향이라 견딘다."* 저장소 전체에서 `inaudible`과 `keep_all`을
**구별해 다루는 자리는 하나도 없다**(`edit.py:412`·`:971`·`:1681` 셋 다 한 덩어리).

**더 위험한 쪽은 그 반대다.** (A)가 재현한 결과:

```
== A. stream_unavailable 계열 (문서는 hasAudio=1, PCM만 실패) ==
[A1 success + 잠금·부재 없음]   통과 — 블록 2개, 제거 후보 500프레임   ← 막히지 않는다
[A2 keep_all (처방)]            통과 — 블록 0개
[A3 success + 잠금·부재 전달]   통과 — 블록 0개

== B. declared_no_audio 계열 (문서가 hasAudio=0) ==
[B1 success]                    raise ValueError   ← 여기서만 막힌다
[B2 inaudible (처방)]           통과 — 블록 0개
[B3 keep_all]                   통과 — 블록 0개    ← 조용히 통과한다
```

**A1을 읽어라.** `stream_unavailable`·`pcm_unavailable` 소스에 `status="success"`·
`protected=()`를 붙이면 `partition_blocks`가 **거부하지 않고 클립 전체가 제거 후보가
된다.** `_audible(clip, media)`(`edit.py:380`)가 `media.has_audio`를 보는데 문서가
`hasAudio=1`로 선언했으므로 그 클립은 **가청**이고, `_require_audible_link`는 애초에
불린 적이 없다. 그 둘을 막는 진짜 난간은 가드가 아니라 **잠금·부재 쌍**이다(A3).

**그러므로 §2 표의 구분을 지키는 것은 하류가 아니라 이 태스크가 새로 쓸
`__post_init__`뿐이다**(§4-b). 없는 난간을 근거로 대지 말고, **그 난간을 여기서 만들어라.**

### VAD가 실패할 때는 손대지 마라

`VadResult.status != "success"`이면 `build_source_guard`가 이미
`_keep_all(reference, blocked)`로 **전 구간 보호**를 낸다(`guard.py`의 `_blocked_reviews`
→ `_keep_all`). **그 경로를 우회하거나 재구현하지 마라.** ONNX 아티팩트가 없어
`bundled_artifact`/`create_cpu_session`이 실패하면 그것은 **환경 실패**이므로
PROTO-2의 `require_ffmpeg`와 같은 자리에서 **한 번 하드 실패**시켜라 — 소스마다
부재 기록 N개로 번역하지 마라.

## 3. 전사 입력의 범위 — 규칙 4를 건드리지 않는다

이 프로토타입은 **현재 순서(자막 → 컷)** 그대로다. R 페이즈의 부분 전사는 **미뤄져
있다.** 그러므로:

- `plan_source_ranges`(`speech.py:223`)가 `[0, sample_count)`를 빈틈없이 덮는 지금 동작을
  **그대로 쓴다.** 범위를 좁히지 마라.
- **"이 구간은 말이 아니다"라고 판단하는 코드를 쓰지 마라**(규칙 4). VAD는 여기서
  **보호를 만들 뿐** 전사 입력을 고르지 않는다.

## 4. 시그니처

### 입력

`PreparedAudio` 하나 ＋ 주입되는 협력자:

- `transcriber: transcribe.Transcriber` — **러너가 직접 만들지 않는다.** openai 경계는
  `vlogsrt/transcribe.py`뿐이다(계약 §4). PROTO-5의 `commands.py`가 만들어 넘긴다.
- ONNX 세션은 러너가 `create_cpu_session(bundled_artifact(project_config))`로 만들되
  **주입도 받을 수 있게** 하라(테스트가 가짜 세션을 꽂는다 — `test_vad_inference.py` 참고).

`Config`·`ProjectConfig`·artifact 경로는 **`PreparedAudio`가 이미 싣는다.** 다시 읽지 마라.

### 출력 — 동결 dataclass

최소한:

- `prepared: PreparedAudio` (문서·미디어·쌍이 여기 다 있다)
- `analyses: tuple[SourceAnalysis, ...]` — `transcribe_unique_sources`의 결과 그대로
- `source_captions: SourceCaptionSet`
- `slices: tuple[LogicalSlice, ...]`
- `boundary_plan: BoundaryPlan`, `boundary: BoundaryProjection`
- `vad_by_source: Mapping[str, VadResult]` — 실패한 것도 **남긴다**
- `instance_guards: tuple[InstanceGuard, ...]` — **문서의 클립 인스턴스 순서 그대로,
  인스턴스마다 정확히 하나**
- `locked_instance_ids: frozenset[str]`, `unavailable_instances: tuple[UnavailableInstance, ...]`
  — **`BoundaryProjection`이 내놓는 것을 그대로 옮긴다.** 다시 계산하지 마라
  (`project_boundaries` docstring: *"잠금·부재는 계획의 것을 그대로 내놓는다"*)
- `source_guards: tuple[SourceGuard, ...]` — `InstanceGuard.review`는 산문만 나른다
  (`guard.py:467-468`). `vad_without_text`의 실제 프레임 수와 보호 비율은 `SourceGuard`에만
  있고, 버리면 **재유도가 아니라 복원 불가**다. D35의 "검토 상태"가 그것을 요구한다
- `transcripts: tuple[BoundaryTranscript, ...]` — 여기서 안 실으면 디스크 캐시 재독으로만
  회수된다

### 4-a. `references`는 `source_id`로 유일화한다 [두 검토자 공통, 최우선]

**정상 경로가 그냥 터진다.** `_index_by`(`boundary.py:587-596`)는 **같은 값이어도** 신원이
두 번 나오면 거부한다:

```
plan_boundaries 경로(_Linkage):                소스 하나에 PCM 참조가 둘 이상이다: 'acid-a1'
transcribe_boundaries 경로(_BoundaryLinkage):  소스와 PCM 키 하나에 참조가 둘 이상이다: 'acid-a1\x00k'
```

`prepared.audio_inputs`는 **문서 리소스마다** 한 칸이고, PROTO-2가 접는 것은 **PCM 주소**지
목록이 아니다(`runner.py:21-25`). 서로 다른 `xml_id`가 같은 바이트를 가리키는 문서에서
러너는 같은 `source_id`의 참조를 **둘** 쥔다. `transcribe_unique_sources`는 트레이스를
정규화해 주지만(`speech.py:734-749`) **참조를 정규화해 주는 곳은 없다.**

`plan_boundaries`·`transcribe_boundaries`에 넘기는 `references`와 **VAD 루프**를 둘 다
`source_id`로 한 번 유일화하라.

### 4-b. `__post_init__`이 규칙 9를 타입으로 지킨다

PROTO-2의 `PreparedAudio.__post_init__`(`runner.py:85-99`)과 같은 축이되, §2-a가 보여준
대로 **하류에 난간이 없으므로 여기가 유일한 난간이다.** 전부 `prepared.inputs_by_resource`
만으로 검사할 수 있다:

1. `instance_guards`의 개수 == 문서의 클립 인스턴스 개수
2. 순서와 `instance_id`가 **문서 순서와 정확히** 일치, 중복 없음
3. **짝의 둘째 칸이 `UnavailableSourceAudio`인 리소스의 인스턴스는 `status != "success"`이고,
   §2 표의 `reason_code → status` 대응이 정확히 성립한다.**
   ← `keep_all`/`inaudible` 오기를 잡는 **유일한** 자리다(B3가 조용히 통과하므로)
4. **그 인스턴스는 `locked_instance_ids`에 있고 `unavailable_instances`에도 있다.**
   ← **A1을 표현 불가능하게 만든다.** 이것이 §2-a가 드러낸 진짜 난간이다
5. `vad_by_source`의 키 집합 == `PcmReference`를 가진 `source_id` 집합
   ← "키가 없다"가 "검사할 것이 없다"로 읽히는 길을 끊는다

**`partition_blocks`가 어차피 검사하지 않느냐고 묻지 마라** — §2-a가 답이다. 검사하지 않는다.

### 4-c. 가드는 문서 순서로 재배열한다

`project_guard`는 **슬라이스 × 인스턴스** 순서로 내놓고(`guard.py:224-239`),
`_require_projection_linkage`가 "이 소스의 슬라이스만" 넘기도록 강제한다(`guard.py:568-573`).
소스마다 부른 결과를 이어 붙이면 **소스별로 뭉친 순서**이지 문서 순서가 아니다.
`_require_guard_linkage`(`edit.py:327`)는 순서를 보지 않으므로 하류는 통과하고
`__post_init__`만 터진다 — `dict[instance_id → guard]`를 만들어 문서 클립 순으로 내라.

## 5. 지켜야 하는 계약

- **계약 §4**: openai는 `vlogsrt/transcribe.py`만. 러너는 **주입된** `Transcriber`를 부른다.
- **계약 §5**: `runner.py`는 `subprocess`를 import하지 못한다. 이 검사는 `rglob`이라
  `runner.py`를 자동으로 본다.
- **계약 §6**: numpy/onnxruntime은 `project/vad/**`만. 러너는 `vad.runtime`·`vad.inference`의
  공개 함수만 부른다 — **`import onnxruntime` 금지.**
- **계약 §1·§7**, **규칙 4·5·8·9**, **40줄 규칙**, **추상화 선행 금지**.
- **규칙 5**: 검증(`verify`)은 이 경로에 없다. 부르지 마라.

## 6. 테스트 — 실패 우선

`tests/project/test_runner_analysis.py`. 합성 입력만. 전사기는 스크립트된 가짜,
ONNX는 가짜 세션. **유료 호출 금지, `tests/fixtures/**` 수정 금지.**

반드시 덮을 것:

1. 정상 경로 — 오디오 있는 소스 하나가 전사·자막·경계·VAD·가드를 통과한다.
2. **`declared_no_audio` 인스턴스에 `status="inaudible"` 가드가 정확히 하나** 붙는다.
3. **`stream_unavailable` 인스턴스에 `status="keep_all"` 가드**가 붙는다.
4. **`status="success"` 합성 가드 — 세 코드를 전부 써라.** v1은 한 줄이었는데, §2-a가
   보여주듯 그러면 구현자가 `declared_no_audio`로만 red-first를 통과시키고 **정작 위험한
   두 코드는 한 번도 증명하지 못한 채** "막힌다"고 보고하게 된다. 세 줄로 나눈다:
   - `declared_no_audio` + `success` → **raise**(`edit.py:415-417`). red-first 가능.
   - `stream_unavailable` + `success` + **잠금·부재 누락** → **raise하지 않고 클립 전체가
     블록이 된다.** 이 사실을 테스트로 **못 박아라** — 선이 어디인지 보여주는 것이
     이 태스크의 핵심이다.
   - `stream_unavailable` + `keep_all`(처방) 또는 잠금·부재 동반 → **블록 0개.**
5. VAD가 `status="error"`를 내면 그 소스의 가드가 **전 구간 보호**다
   (`build_source_guard`의 기존 경로가 그대로 탄다).
6. ONNX 세션 생성 실패는 **한 번의 하드 실패**이지 부재 기록 N개가 아니다.
   **그리고 전 리소스가 `declared_no_audio`인 프로젝트는 세션을 아예 안 만든다**(§1-a) —
   `numpy`/`onnxruntime` 없이도 그 프로젝트가 끝까지 흐르는지 확인하라.
7. 혼합 프로젝트 — 정상 + 부재 섞여도 **가드 수 == 클립 인스턴스 수**.
8. `__post_init__`의 **다섯 단언이 각각 문다**(§4-b). 특히 3번(오기)과 4번(A1)은
   위조 입력으로 red-first를 보여라. 나머지도 하나씩.
9. **서로 다른 `xml_id`가 같은 바이트를 가리키는 문서가 끝까지 흐른다**(§4-a).
   유일화를 빼면 `plan_boundaries`가 *"소스 하나에 PCM 참조가 둘 이상이다"*로 죽는 것을
   red-first로 보여라.
10. 산출물이 `partition_blocks`를 **실제로 통과한다**(그 함수를 직접 불러서 증명하라).

**테스트는 `prepare_audio`를 부를 수 없다** — ffmpeg가 필요하고 이 머신의 shim이 죽어 있다.
`PreparedAudio`를 손으로 조립하라(입력이 그 하나로 고정된 덕에 가능하다). 재료 패턴:
합성 `PcmReference`·PCM·미디어는 `test_speech.py:84-140`, 문서·가드·파티션 빌더는
`test_block_partition.py:83-213`, 가짜 numpy·세션은 `test_vad_inference.py:83-198`,
monkeypatch 게이트는 `test_runner_audio.py:102-185`.

**단언을 약화해 통과시키지 마라. 임계값을 고치지 마라.**

## 7. DoD

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
.venv/bin/python -m pytest tests/project/test_runner_analysis.py
.venv/bin/ruff check vlogsrt ui tests scripts
.venv/bin/ruff format --check vlogsrt ui tests scripts
.venv/bin/mypy
.venv/bin/python scripts/check_invariants.py | grep -c '^\[OK\]'   # 20
.venv/bin/python -m pytest
```

전체 pytest는 **2459 + 새 테스트 수**, 0 failed. 불변식은 `[OK]` 줄을 **세어** 보고한다.
**`-q`가 요약 줄을 삼키면 `-q` 없이 다시 돌려 줄을 직접 실어라.**

## 8. 보고 형식

```
커밋 SHA:
공개 시그니처: <입력·출력 타입 그대로>
합성 가드 표: <세 reason_code → status. file:line>
테스트 4의 세 줄: <각 줄의 실제 결과. A1이 raise하지 않는다는 것을 확인했는가>
__post_init__ 다섯 단언: <file:line + 각각의 red-first 증거>
references 유일화: <file:line + 유일화 뺐을 때의 red-first 출력>
게이트: <명령마다 실제 출력 숫자. 불변식은 센 값>
diff 범위: <git diff --stat. 소유 파일 둘뿐인가. PROTO-2 공개 API 무수정인가>
멈춰야 했던 것: <있으면. 없으면 "없음">
```

---

## 9. 방향 검토 기록 (v1 → v2)

두 검토자 모두 **SOUND_WITH_CONCERNS**. 이번에는 **둘이 같은 주장을 독립으로 반박했다** —
수렴이라 가장 강한 신호다.

### 뒤집힌 내 주장 — 둘 다 재현했다

> v1 §2: *"`edit.py:415-417` — 불가청 인스턴스에 `status="success"` 가드를 붙이면
> 거부한다. 그러니 `declared_no_audio`를 `keep_all`로 잘못 찍으면 그 자리에서 터진다."*

**거짓이다.** `_require_audible_link`는 **`success`만** 거부하고, docstring이 이유를
적어 놓았다 — *"keep_all·inaudible이 불가청 클립에 붙는 것은 보수 방향이라 견딘다."*
(B)는 기존 테스트로 재현했다(`test_ineligible_statuses_on_inaudible_clips_stay_silent`
→ 4 passed). 저장소 전체에서 `inaudible`과 `keep_all`을 구별해 다루는 자리는 없다.

### (A)만 찾은 것 — 이 세션에서 가장 값진 발견

**A1**: `stream_unavailable`·`pcm_unavailable` 소스에 `status="success"`·`protected=()`를
붙이면 `partition_blocks`가 **거부하지 않고 클립 전체가 제거 후보가 된다.**
`_audible`이 `media.has_audio`를 보는데 문서는 `hasAudio=1`이므로 그 클립은 **가청**이고
`_require_audible_link`는 불린 적이 없다. 진짜 난간은 가드가 아니라 **잠금·부재 쌍**이다.

**그래서 v1의 테스트 4는 승인 도장이었다.** 구현자가 `declared_no_audio`(진짜로 raise한다)로
red-first를 통과시키고 "여섯 번 당한 결함이 막힌다"고 보고하면, **정작 위험한 두 코드는
한 번도 증명되지 않는다.** 헌장이 말한 "재현 없는 동의"가 정확히 이 모양이다.

(A)는 또 v1이 **자기 유비를 어긴 것**을 짚었다 — ONNX 게이트를 유료 호출 뒤에 놓아,
아티팩트가 깨진 환경에서 77분 회차 전사비를 다 치르고 죽는다. PROTO-2가 `require_ffmpeg`를
자원 루프 앞으로 뺀 바로 그 논리다.

### (B)만 찾은 것

전 리소스가 `declared_no_audio`인 프로젝트는 VAD를 한 번도 안 쓰고 끝까지 흐른다 —
세션을 무조건 만들면 `numpy`/`onnxruntime` 없는 환경에서 **쓰지도 않을 이유로** 죽는다.
그리고 테스트 4의 파티션 수준 등가물이 이미 있다(`test_block_partition.py:380`)는 확인.

### 둘 다 찾은 것

- **참조 중복**(§4-a) — 둘 다 최우선으로 꼽았다. 정상 입력이 `ValueError`로 죽는다.
- `build_source_guard`의 `repairs` 인자가 명세에 없다(§1-b).
- `project_guard`의 출력 순서가 문서 순서가 아니다(§4-c).
- 세션 게이트를 참조 유무로 조건화해야 한다(§1-a).

### 두 검토자가 재현하지 못한 것

- **러너 자체.** 아직 없다 — A1·B3·참조 중복은 전부 하류 함수를 직접 불러 재현했다.
- **실제 ONNX 세션 실행.** 아티팩트는 진짜 ONNX 2,327,524바이트임을 확인했지만
  `create_cpu_session`을 돌려보지는 않았다.
- **같은 내용 두 리소스의 전 경로** — `prepare_audio`가 참조 둘을 내놓는 것은 ffmpeg가
  죽어 있어 코드 독해로만 확인했다. 구현자가 §6 테스트 9에서 닫아라.
