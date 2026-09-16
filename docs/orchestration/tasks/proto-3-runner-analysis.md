# PROTO-3 — 러너 2/3: 전사 · 자막 투영 · 경계 · VAD · 가드

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
  → transcribe_unique_sources(...)         speech.py:722    소스 전사
  → project_source_captions(...)           captions.py:380  SOURCE SRT 초안
  → logical_slices(document, media)        boundary.py:226
  → plan_boundaries(...)                   boundary.py:949
  → transcribe_boundaries(...)             boundary.py:1116 경계 재전사
  → project_boundaries(...)                boundary.py:2228 BoundaryProjection
  → create_cpu_session(bundled_artifact(cfg))   vad/runtime.py:79,115
  → infer_vad(reference, source_id, cfg, session)  vad/inference.py:86
  → build_source_guard(...)                vad/guard.py:160
  → project_guard(...)                     vad/guard.py:212  InstanceGuard들
```

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
막기 때문이지 `protected`가 비어서가 아니다.** 하류가 그 선을 이미 쥐고 있다:

- `edit.py:968-971` — *"keep_all·inaudible은 빈 protected여도 제거 권한이 없다"*
- `edit.py:415-417` — **불가청 인스턴스에 `status="success"` 가드를 붙이면 거부한다.**
  그러니 `declared_no_audio`를 `keep_all`로 잘못 찍으면 그 자리에서 터진다.

**`status="success"`인 합성 가드를 만들지 마라. 어떤 이유로도.**

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

### `__post_init__`이 규칙 9를 타입으로 지킨다

PROTO-2의 `PreparedAudio.__post_init__`(`runner.py:85-99`)과 같은 축으로:

- `instance_guards`의 개수 == 문서의 클립 인스턴스 개수
- 순서와 `instance_id`가 문서 순서와 정확히 일치
- 같은 `instance_id`가 두 번 나오지 않는다

**`partition_blocks`가 어차피 검사하지 않느냐고 묻지 마라** — 하류가 잡는 것과 여기서
표현 불가능하게 만드는 것은 다르다. 후자는 잊을 수 없다.

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
4. **합성 가드에 `status="success"`를 주면 `partition_blocks`가 거부한다** —
   `edit.py:415-417`을 실제로 불러 red-first로 증명하라. 이 저장소가 여섯 번 당한
   결함이 여기서 막히는지 **직접** 보여라.
5. VAD가 `status="error"`를 내면 그 소스의 가드가 **전 구간 보호**다
   (`build_source_guard`의 기존 경로가 그대로 탄다).
6. ONNX 세션 생성 실패는 **한 번의 하드 실패**이지 부재 기록 N개가 아니다.
7. 혼합 프로젝트 — 정상 + 부재 섞여도 **가드 수 == 클립 인스턴스 수**.
8. `__post_init__` 단언이 실제로 문다(가드를 하나 빼거나 순서를 뒤집은 위조 입력이
   raise하는지 red-first).
9. 산출물이 `partition_blocks`를 **실제로 통과한다**(그 함수를 직접 불러서 증명하라).

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
"success 합성 가드는 거부된다" 증거: <red-first 출력>
__post_init__ 단언: <file:line + red-first 증거>
게이트: <명령마다 실제 출력 숫자. 불변식은 센 값>
diff 범위: <git diff --stat. 소유 파일 둘뿐인가. PROTO-2 공개 API 무수정인가>
멈춰야 했던 것: <있으면. 없으면 "없음">
```
