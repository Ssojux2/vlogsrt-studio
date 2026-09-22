# PROTO-3 방향 검토 — (A) 컨텍스트 분리 Opus

> 2026-09-16. `claude -p --model claude-opus-5 --effort high --permission-mode plan`, 읽기 전용.
>
> **판정 SOUND_WITH_CONCERNS.** 내 안전 주장을 재현으로 반박했고, **A1** —
> `stream_unavailable`에 `success` 가드를 붙여도 `partition_blocks`가 막지 않는다는 것 —
> 을 혼자 찾았다. v1의 테스트 4가 승인 도장이 될 뻔했다 (브리프 v2 §9).

모든 확인을 끝냈다. 보고서 전문:

---

```
방향 판정: SOUND_WITH_CONCERNS
```

기준: `d8eed17`, 트리 깨끗, `pytest` **2459 passed in 40.93s**, `check_invariants.py` `[OK]` **20**줄 — 브리프의 기준선 주장은 전부 확인했다.

---

## 시그니처 충분성

**상류 정확값에는 닿는다 — P4-04 재발은 없다.** `PreparedAudio`가 `document`·`media`·`audio_inputs`·`config`·`project_config`·`artifact_dir`을 다 싣고(`runner.py:74-83`), `PcmReference`의 `analysis_key`·`pcm_sample0_file_time`이 `SourceTrace`·`VadResult`·`SourceGuard`를 타고 그대로 건너간다(`guard.py:492-565`, `inference.py:95-101`). 하류가 시각을 다시 유도해야 하는 자리는 **찾지 못했다.**

다만 **두 인자가 브리프에 비어 있고, 둘 다 틀리면 조용히 틀린다.**

### (a) `build_source_guard`의 `repairs` — 시계가 다른 기록을 넣을 자리가 열려 있다

`guard.py:168-174`가 명시적으로 경고한다:

> `repairs`는 **디코드 오디오 국소 시계(초)**의 재전사 기록만 받는다 — 경계 재전사 기록(`BoundaryTranscript.repairs`, 남긴 범위 국소 시계)을 여기 넣지 마라: 시계가 다른 기록은 엉뚱한 자리에 보호를 놓는다.

브리프 §1의 화살표는 `build_source_guard(...)`로만 적혀 있어 여섯 인자 중 이것만 출처가 없다. 그런데 이 단계는 바로 앞에서 `transcribe_boundaries`를 돌려 `BoundaryTranscript[]`를 손에 쥐고 있다 — 손 닿는 곳에 **틀린 값**이 있다. `_repair_spans`(`guard.py:294-305`)가 이미 `trace.repairs`를 합치므로 정답은 `repairs=()` 하나뿐이다. **브리프가 그 글자를 적어야 한다.**

### (b) 출력에 `SourceGuard`가 없다 — 재유도는 아니지만 진단이 죽는다

`InstanceGuard.review`는 `"말이 있는데 쓸 글자이 없는 소스 구간이 있다"`라는 **산문**만 나른다(`guard.py:467-468`). `vad_without_text`의 실제 프레임 수·`protected_samples`/`total_samples` 비율은 `SourceGuard`에만 있고 출력에서 사라진다. 하류 계산에는 필요 없으므로 P4-04는 아니지만, D35의 "검토 상태"를 보고하려면 재유도가 아니라 **복원 불가**다. `source_guards: tuple[SourceGuard, ...]` 한 칸을 권한다.

---

## 부재가 권한이 되는 자리

### ★ 일곱 번째는 **브리프가 지목한 자리가 아니다.** 브리프의 두 난간이 모두 존재하지 않는다.

재현(저장소 수정 없음, `/tmp/protoreview/repro.py`, 합성 25fps 문서 · 500프레임 클립 하나 · `protected_frames=()`):

```
== A. stream_unavailable 계열 (문서는 hasAudio=1, PCM만 실패) ==
[A1 success + 잠금·부재 없음]        통과 — 블록 2개, 제거 후보 500프레임
[A2 keep_all (브리프 처방)]          통과 — 블록 0개, 제거 후보 0프레임
[A3 success + 잠금·부재 전달]        통과 — 블록 0개, 제거 후보 0프레임

== B. declared_no_audio 계열 (문서가 hasAudio=0) ==
[B1 success]                         raise ValueError: 불가청 인스턴스에 status=success guard가 붙었다
[B2 inaudible (브리프 처방)]         통과 — 블록 0개, 제거 후보 0프레임
[B3 keep_all]                        통과 — 블록 0개, 제거 후보 0프레임
```

**A1이 결론이다.** `stream_unavailable`·`pcm_unavailable` 소스에 `status="success"` · `protected=()`를 붙이면 `partition_blocks`는 **거부하지 않고**, 20초 클립 전체가 제거 후보가 된다. 이유는 `edit.py:380`의 `_audible(clip, media)`가 `media.has_audio`를 보기 때문이다 — 문서가 `hasAudio=1`로 선언했으므로 그 클립은 **가청**이고, `_require_audible_link`(`edit.py:409-419`)는 애초에 불린 적이 없다. 그 둘을 막는 진짜 난간은 가드가 아니라 **잠금·부재 쌍**이다(`edit.py:383-387`, A3가 그 경로다).

즉 브리프 §2의 경고문 — *"가드를 빈 `protected`로 `status="success"`로 만들면 그 인스턴스는 제거 권한이 생긴다"* — 은 **옳다**. 틀린 것은 그 다음에 붙인 안전망의 주소다.

---

## 코디네이터 주장 검토

### 1. **반박** — "`declared_no_audio`를 `keep_all`로 잘못 찍으면 그 자리에서 터진다" (§2)

**거짓이다.** 재현 B3: 조용히 통과한다. `_require_audible_link`는 `success`만 거부하고(`edit.py:415`), 자신의 docstring이 그 이유를 적어 놓았다:

> `edit.py:412-413` — *"이 방향만 닫는다: 제거를 **넓히는** 모순이므로. keep_all·inaudible이 불가청 클립에 붙는 것은 보수 방향이라 견딘다(블록만 없어진다)."*

`grep`으로 확인한 결과 `inaudible`과 `keep_all`을 **구별해서** 다루는 자리는 저장소 전체에 하나도 없다 — `edit.py:412`·`edit.py:971`·`edit.py:1681` 셋 다 둘을 한 덩어리로 묶어 부른다. 따라서 §2 표의 `declared_no_audio → inaudible` / `stream·pcm → keep_all` 구분은 **하류 집행이 0이다.** 옳은 구분이지만, 그것을 지키는 것은 오직 이 태스크가 새로 쓸 `__post_init__`뿐이다. 브리프는 없는 난간을 근거로 대는 대신 **그 난간을 여기서 만들라**고 말해야 한다.

### 2. **반박** — 테스트 4 "합성 가드에 `status="success"`를 주면 `partition_blocks`가 거부한다"

**세 reason_code 중 하나에서만 참이다.** 위 A1/B1 참조. 이대로 두면 구현자는 `declared_no_audio`로 테스트를 써서 red-first를 통과시키고 — 그 케이스는 진짜로 raise하니까 — **정작 위험한 두 코드는 한 번도 증명하지 못한 채** "이 저장소가 여섯 번 당한 결함이 여기서 막힌다"고 보고하게 된다. 헌장이 말한 "승인 도장"이 정확히 이 모양이다.

### 3. **확인** — "`build_source_guard`는 `UnavailableSourceAudio` 소스의 가드를 만들 수 없다"

맞다. `guard.py:160-167`이 `SourceTrace`·`PcmReference`를 필수로 받고, `guard.py:28-30`이 같은 사실을 적어 놓았다. 합성이 필요하다는 판단은 옳다.

### 4. **확인** — "VAD 실패 경로를 우회·재구현하지 마라"

맞고, 생각보다 더 튼튼하다. `infer_vad`는 어떤 예외도 밖으로 내지 않고 `status="error"`로 닫으며(`inference.py:98-114`), `_blocked_reviews`→`_keep_all`(`guard.py:576-589`, `671-684`)이 전 구간을 보호한다. 나아가 **위조된** `VadResult(status="success", windows=())`를 넣어도 `_vad_input_defect`의 `_first_gap`이 `[0, total)` 구멍을 잡아 `keep_all`로 닫는다(`guard.py:617-622`). 우회할 이유가 없다.

### 5. **부분 반박** — "ONNX 세션 생성 실패는 부재 기록 N개가 된다"

그 실패 모양은 이 코드로는 **도달 불가**다. `bundled_artifact`/`create_cpu_session`은 `VadUnavailable`을 raise하고, `infer_vad`는 error를 반환하며 — 둘 중 어느 쪽도 `UnavailableSourceAudio`를 낳지 못한다. 하지만 **처방(한 번 하드 실패)은 유지하라.** 문제는 다른 데 있다 → 아래 "의존성·순서".

### 6. **확인(정정 없음)** — 인용 줄 번호

`edit.py:415-417`(raise 본문), `edit.py:968-971`, `guard.py:160-167`, `runner.py:85-99`는 전부 정확하다. `_require_guard_linkage(edit.py:158)`만 주소가 `partition_blocks`의 def 줄이고 함수 자체는 `edit.py:327`이지만, 인용한 docstring은 `edit.py:167-169`라 실질 오류가 아니다.

---

## 계약·불변식·CLAUDE.md 규칙

**위반 없음.** 확인한 것:

- **§1·§4·§5·§6** — 러너가 `vad.runtime`·`vad.inference`의 공개 함수만 부르면 된다. numpy·onnxruntime import는 전부 함수 안에 있다(`inference.py:139`, `runtime.py`의 지연 import). `runner.py`는 `REMOVAL_LAYER_MODULES = ("edit.py", "export.py")`(`scripts/check_invariants.py:52`)에 **없으므로** `speech`·`boundary`·`vad` import가 막히지 않는다.
- **§7·§8** — 러너에 모델 리터럴이 들어갈 이유가 없다. `models.py` 전용 frozen 검사는 `runner.py`에 걸리지 않지만 PROTO-2가 이미 문서로 선을 그었다(`runner.py:67-69`).
- **규칙 4 / D35** — §3의 판단이 옳다. `plan_source_ranges`(`speech.py:223-254`)는 `[0, sample_count)`를 빈틈없이 덮고 docstring이 규칙 4를 명시한다. VAD는 보호만 만들고 전사 입력을 고르지 않는다 — 브리프가 그 선을 정확히 지켰다.
- **규칙 5** — `verify`는 이 경로에 없다. 맞다.
- **규칙 9 / D35** — `trace.status == "empty"`도 조용히 사라지지 않는다(`guard.py:266-269`, `328-334`의 글자 없는 덮개가 씨앗으로 들어간다). 러너가 새로 깨뜨릴 자리는 §2의 합성 가드 하나뿐이다.

---

## 의존성·순서

### ★ ONNX 게이트가 유료 호출 **뒤**에 있다 — 브리프가 자기 유비를 어겼다

§1의 파이프라인은 `create_cpu_session(bundled_artifact(cfg))`를 `transcribe_unique_sources`·`transcribe_boundaries` **다음에** 놓는다. 그 둘이 이 태스크의 **유일한 유료 호출**이다.

그런데 §2는 *"PROTO-2의 `require_ffmpeg`와 같은 자리에서 한 번 하드 실패시켜라"*라고 적었고, `_require_audio_tools`는 **자원 작업 앞**에 있다 — 그 이유를 PROTO-2가 직접 써 놓았다:

> `runner.py:159-163` — *"도구가 없는 것은 자원의 결함이 아니라 환경의 결함이다. 자원 게이트 안에서 죽으면 부재 기록 N개로 번역되어 진단이 죽는다 — 게이트를 앞으로 뺀 것이 그 길을 끊는다."*

§1의 순서대로면 아티팩트가 깨진 환경에서 **77분 회차 전사비를 다 치르고 나서** `VadUnavailable`로 죽고, 이미 만든 SOURCE SRT까지 버린다. `bundled_artifact`는 해시 검증 하나라 공짜다.

**최소 수정**: 게이트를 `transcribe_unique_sources` **앞**으로 옮기고, `_require_audio_tools`가 `any(item.has_audio)`로 조건 붙듯 "PcmReference를 가진 소스가 하나라도 있을 때만" 묻게 하라(오디오 0개 프로젝트가 ONNX 때문에 죽지 않게).

### ★ 중복 `PcmReference` — 정상 경로가 그냥 터진다

재현(`/tmp/protoreview/dup.py`):

```
plan_boundaries 경로(_Linkage):        소스 하나에 PCM 참조가 둘 이상이다: 'acid-a1'
transcribe_boundaries 경로(_BoundaryLinkage): 소스와 PCM 키 하나에 참조가 둘 이상이다: 'acid-a1\x00k'
```

`_index_by`(`boundary.py:587-596`)는 **같은 값이어도** 신원이 두 번 나오면 거부한다. 그런데 `prepared.audio_inputs`는 **문서 리소스마다** 한 칸이고, PROTO-2가 자기 docstring에서 그 상황을 자랑한다:

> `runner.py:21-25` — *"서로 다른 xml_id를 가진 리소스 둘이 같은 바이트를 가리키면(파서는 중복 id만 거부한다) 주소가 하나로 접혀 추출은 한 번이다."*

접히는 것은 **주소**지 목록이 아니다. 그 문서에서 러너는 같은 `source_id`의 참조 **둘**을 쥔다. `transcribe_unique_sources`는 트레이스를 이미 정규화해 주지만(`speech.py:734-749`) **참조를 정규화해 주는 곳은 없다.** 브리프가 `analyses`는 "결과 그대로"라고만 적고 참조 쪽은 침묵해서, 구현자는 `audio_inputs`에서 뽑은 리스트를 그대로 넘기게 된다.

**최소 수정**: "`plan_boundaries`·`transcribe_boundaries`에 넘기는 `references`는 `source_id`로 **유일화한** 것이다"를 §4에 명시하라.

### 순서 관련 (낮음)

`project_guard`는 **슬라이스 × 인스턴스** 순서로 내놓고(`guard.py:224-239`), `_require_projection_linkage`가 "이 소스의 슬라이스만" 넘기도록 강제한다(`guard.py:568-573`). 그러므로 소스마다 부른 결과를 이어 붙이면 **소스별로 뭉친 순서**이지 문서 순서가 아니다. `_require_guard_linkage`(`edit.py:327`)는 순서를 보지 않으므로 이어 붙여도 하류는 통과한다 — `__post_init__`만 터진다. 브리프가 "재정렬이 필요하다"고 한 줄 적어 두는 편이 낫다.

---

## 소유 파일로 가능한가

**가능하다. 다만 테스트 파일이 무겁다.**

`tests/project/`에는 **`conftest.py`가 없고**, 그 디렉터리의 어느 파일도 다른 테스트 모듈에서 헬퍼를 import하지 않는다(`grep` 확인). `test_runner_analysis.py`는 합성 FCPXML 생성기 · 손으로 쓴 s16le PCM + sha256 · 스크립트된 가짜 전사기 · 가짜 ONNX 세션을 **전부 자기 안에** 지어야 한다. 이웃들의 규모: `test_speech.py` 1666줄, `test_boundary_projection.py` 1069줄, `test_guard.py` 880줄, `test_vad_inference.py` 846줄. 9개 시나리오면 비슷한 자릿수를 각오해야 한다. 재료 패턴은 `test_block_partition.py:86-185`(합성 XML·손 미디어·손 가드), `test_vad_inference.py:56-80`(가짜 numpy·세션)에 다 있다.

**주의 하나**: 테스트는 `prepare_audio`를 부를 수 없다 — ffmpeg가 필요하고 이 머신의 `/usr/bin` shim이 전부 죽어 있다. `PreparedAudio`를 손으로 조립해야 하며, 브리프가 입력을 `PreparedAudio` 하나로 고정한 덕에 그게 가능하다. 그 점은 설계가 옳다.

추가 파일은 필요 없다.

---

## 최소 수정 명세 (심각도 순)

1. **§4 — 참조 유일화 명시.** "`plan_boundaries`·`transcribe_boundaries`에 넘기는 `references`는 `source_id`로 유일화한다(같은 `asset_content_id`를 가진 리소스 둘이 있으면 `audio_inputs`에는 참조가 둘 있다)." 없으면 정상 경로가 `ValueError`로 죽는다.

2. **§6 테스트 4 — 셋 다 쓰게 고쳐라.** 현행 문장을 세 줄로 나눈다:
   - `declared_no_audio` + `success` → **raise**(`edit.py:415-417`). red-first 가능.
   - `stream_unavailable` + `success` + 잠금·부재 **누락** → **raise하지 않고 클립 전체가 블록이 된다.** 이 사실을 테스트로 **못 박아라** — 그것이 이 태스크가 지켜야 할 선이 어디인지를 보여준다.
   - `stream_unavailable` + `keep_all`, 또는 잠금·부재 동반 → 블록 0개.

3. **§4 `__post_init__` — 브리프가 지목한 결함을 실제로 표현 불가능하게 만들어라.** 현행 세 단언(개수·순서·중복)에 더해, `prepared.inputs_by_resource`만으로 전부 검사 가능하다:
   - 짝의 둘째 칸이 `UnavailableSourceAudio`인 리소스의 인스턴스는 `status != "success"`이고, §2 표의 `reason_code → status` 대응이 **정확히** 성립한다. ← `keep_all`/`inaudible` 오기를 잡는 **유일한** 자리다(B3 재현 참조).
   - 그 인스턴스는 `locked_instance_ids`에 있고 `unavailable_instances`에도 있다. ← A1을 표현 불가능하게 만든다.
   - `vad_by_source`의 키 집합 == `PcmReference`를 가진 `source_id` 집합. ← "키가 없다"가 "검사할 것이 없다"로 읽히는 길을 끊는다.

4. **§1 — ONNX 게이트를 전사 앞으로.** 유료 호출 전에 한 번. 조건은 `_require_audio_tools`와 같은 모양(참조가 하나라도 있을 때만).

5. **§1 — `build_source_guard(..., repairs=(), ...)`를 글자로 적어라.** `BoundaryTranscript.repairs`가 손 닿는 곳에 있고 시계가 다르다(`guard.py:168-174`).

6. **§2 — 거짓 근거 삭제.** *"`declared_no_audio`를 `keep_all`로 잘못 찍으면 그 자리에서 터진다"* 를 지우고 *"둘의 구분은 하류가 집행하지 않는다 — `__post_init__`이 유일한 난간이다"* 로 바꿔라.

7. **§4 — `source_guards` 추가 권고(선택).** `vad_without_text` 프레임 수·보호 비율이 출력에서 사라진다.

8. **§4 — 가드 재정렬 필요 명시(선택).** `project_guard`의 출력 순서는 문서 순서가 아니다.

---

## 재현하지 못한 것

- **실제 ONNX 세션 실행.** numpy 2.5.3 · onnxruntime 1.30.0이 설치돼 있고 `vlogsrt/project/vad/data/silero_vad_v6.2.1.onnx`는 2,327,524바이트의 진짜 ONNX다(LFS 포인터가 아님을 첫 바이트로 확인). 그러므로 이 환경에서 하드 실패 경로는 잘 안 나겠지만, `create_cpu_session`을 **실행해 보지는 않았다.**
- **러너 자체.** 아직 없다 — A1·B3·중복 참조는 전부 하류 함수를 직접 불러 재현했고, 러너를 통과시킨 것이 아니다.
- **`mypy`·`ruff`.** 존재하지 않는 코드에는 돌릴 것이 없다.
- **같은 내용 두 리소스의 전체 경로.** `_index_by`/`_Linkage`/`_BoundaryLinkage` 수준에서 거부를 재현했고, `prepare_audio`가 실제로 그 문서에서 참조 둘을 내놓는 것은 ffmpeg가 죽어 있어 **코드 독해로만** 확인했다(`runner.py:131`이 리소스마다 `_audio_input`을 부르고 `142`가 리소스 신원으로 색인한다 — 접히는 것은 PCM 주소뿐이다).
