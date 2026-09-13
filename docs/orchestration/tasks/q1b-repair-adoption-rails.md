# Task Q1B: 재전사 채택 레일을 닫고 자동 접기를 기본 해제한다

## Authority

`CLAUDE.md` 규칙 1·4·5·9, D35의 규칙 4·9 파생 계층 조항,
`docs/PROJECT_CONTRACTS.md` §1과 불변식 18·19,
`docs/DECISIONS.md` D27·D28·D29·D35, 그리고 다음 검토가 권위다.

- `docs/orchestration/reviews/astra-q1-direction.md` — **UNSOUND, 5 findings**
- `docs/orchestration/reviews/sol-repair-arbiter-diagnosis.md`
- `docs/orchestration/reviews/opus-q1-similarity-evidence.md`와 문서 끝의 **2026-09-13 정정**

코디네이터 판정은 확정됐다. 다시 논의하거나 범위를 넓히지 마라. 모든 구현과 검증은
오프라인 전용이다.

## Baseline and ownership

- 저장소: `/Users/ssojux2/Working/vlogsrt`
- branch: `feat/final-cut-project`
- exact dispatch base: `a0c5be1`
- 이 브리프의 기준 HEAD: `a0c5be1e855372eb5e8b24f1fbed1fb37ea52394`

**이 태스크는 Q1B-1이다 — 레거시 재전사 레일만 닫는다.** 초안의 ownership은 25개
파일이었다. 이 저장소의 태스크 규범(소유 파일 2~3개)을 크게 넘고, `CLAUDE.md`·
`docs/DECISIONS.md`·`TASKS.md`까지 구현자에게 넘겼다 — 그 셋은 코디네이터 소유이고,
이 브리프 자신도 앞에서 "구현자는 ownership 밖 문서를 수정하지 않는다"고 적었다.
코디네이터가 쪼갰다.

허용 파일 (이것뿐이다):

- `vlogsrt/repair.py`
- `vlogsrt/transcribe.py` — arbiter 이음매만
- `vlogsrt/models.py` — `Repair`의 arbiter 상태만
- `vlogsrt/config.py` — 신규 `repair_*` 필드만
- `tests/test_repair.py`
- `tests/test_repair_pipeline.py`
- 필요한 경우에만 합성 픽스처 `tests/fixtures/synthetic/loopy*.{rescue,arbiter}.json`

**뒤로 미룬 것 (이 태스크에서 건드리지 마라):**

- `vlogsrt/project/speech.py`·`vlogsrt/project/boundary.py`와 그 테스트 → **Q1B-2**.
  계약 §10(project 래퍼가 arbiter rejection을 오류로 승격하지 않는다)은 Q1B-1이
  머지된 뒤 별도 태스크로 간다. 다만 Q1B-1이 그 두 파일의 기존 테스트를 깨뜨리면
  **고치지 말고 보고하라** — 깨지는 목록 자체가 Q1B-2의 입력이다.
- `CLAUDE.md`·`README.md`·`docs/RUNBOOK.md`·`docs/DECISIONS.md`·`TASKS.md`·
  `vlogsrt.toml`·완료된 태스크 브리프 → **코디네이터가 머지 시점에 쓴다.**
  신규 설정값의 근거는 완료 보고에 적어라. 직접 문서를 고치지 마라.

그 밖의 파일은 수정하지 마라. 특히 다음은 금지다.

- `.env`
- `.env.example`
- `tests/fixtures/api/**`
- `golden/**`
- `docs/PROJECT_CONTRACTS.md`
- `scripts/speech_onsets.py`
- 잠긴 임계값과 관련된 다른 테스트·문서

`.env.example`에는 사용자가 소유한 미커밋 변경이 이미 있다. 열람·정리·포맷·복원·
커밋하지 마라.

## 결함과 실측 위험

현재 repair는 첫 rescue candidate가 비어 있지 않고 자체 반복 검사만 통과하면 심판을
부르기 전에 채택한다.

```text
segments가 비어 있지 않음
repetitive_seconds <= repair_residual_seconds
repetitive_share <= loop_repetitive_share
```

그 결과 실제 실행에서 다음이 채택됐다.

- `"한 번 더"` ×5 → `"구독해주세요"`
- `"And here it was a hi…"` ×3 → `"내일도 영상/봐주세용"`
- `"치즈가 왔다!"` ×7 → `"감사합니다"`
- repair `[12]`는 plan `2918.32–3032.32` 중 첫 candidate가 `2978.34`에서 시작해
  약 60초가 비는데도 `replaced`

기존 브리프처럼 이 candidate들을 거절한 뒤 현재 collapse fallback으로 보내면 손실이
더 커진다. 실제 `out/fr_day_3`에서 그 경로에 노출되는 구간은 다음과 같다.

```text
[0]   93.24 s
[9]   46.79 s
[12] 112.00 s
합계 252.03 s
```

이 중 VAD가 발화로 점수화한 범위는 146.12초다. 특히 `37:18–38:33`의 75초 공백은
VAD 평균 발화 확률이 `0.730`이고, 대조군은 `0.846`·`0.805`였다.

반대로 KEEP가 품질상 strictly better인 것도 아니다. 음악 구간 `[0]`을 KEEP하면 현재
repair가 만든 12개 환각 대신 1차 전사의 `"맛있어?"` 계열 cue 103개가 돌아온다.
이번 판정은 **visible garbage가 invisible loss보다 낫고, 사용자가 보이는 쓰레기는 직접
지울 수 있다**는 보존 우선 결정이다.

## 확정 계약

### 1. adoption 거절은 절대로 collapse 권한이 아니다

다음은 plan 길이와 무관하게 모두 `REPAIR_KEPT + WARN`이다.

- arbiter 미요청 또는 응답 없음
- arbiter 호출 실패
- 응답 미매칭·누락·중복·잘못된 payload
- 빈 arbiter 글자
- 유효한 arbiter와 candidate의 불일치
- candidate가 비었거나 구조가 잘못됨
- confidence metadata 부재·비정상·문턱 실패
- candidate 시각 이상
- plan 밖 시각이 numeric tolerance를 넘음
- leading·trailing·interior coverage 실패

거절 시 `_apply`가 걷어내는 first-pass segment는 0개여야 한다. `settle` 경계에서 해당
plan의 1차 글자와 시각을 그대로 보존한다.

“downstream byte-equivalent”라고 과장하지 마라. repair가 보장하는 범위는 정확히
**`settle` 경계에서 first-pass 글자와 시각이 보존되는 것**이다. 이후 typesetting이
합치거나 시각을 움직이는 것까지 repair가 보장할 수는 없다.

### 2. 자동 collapse는 opt-in이며 기본 OFF다

`Config`에 다음을 추가한다.

```python
repair_collapse_enabled: bool = False
```

- `repair_enabled=True`는 계속 기본이다. 되풀이 검출과 rescue는 계속 돈다.
- `repair_collapse_enabled=False`이면 회복하지 못한 긴 되풀이도 전부 `REPAIR_KEPT`다.
- D27의 collapse capability는 삭제하지 않는다.
- 명시적으로 `repair_collapse_enabled=True`인 경우에만
  `repair_collapse_min_seconds`와 `repetition.collapse`가 작동할 수 있다.
- `repair_collapse_min_seconds=30.0`은 잠긴 값이며 바꾸지 않는다.

Opt-in collapse는 adoption 거절의 일반 fallback으로 구현하지 마라. 다음 조건을 모두
만족하는 별도 명시 경로에서만 허용한다.

1. `repair_collapse_enabled is True`
2. `plan.repeat.duration >= repair_collapse_min_seconds`
3. rescue 응답이 존재하고 시각·coverage·confidence metadata가 유효하다
4. 모든 rescue attempt가 기존 반복 조건 때문에만 회복 실패했다
5. arbiter도 명시적으로 반복 글자를 반환한 `answered_repetitive`다
6. call failure, unmatched, empty, disagreement, confidence rejection 또는 coverage
   rejection이 collapse 근거에 섞이지 않았다

이 조건에서 하나라도 불확실하면 KEEP한다. `dict.get(..., False)` 하나를 뒤집어 collapse
가능 여부를 결정하지 마라.

### 3. 독립 arbiter 결과와 attempt별 adoption을 분리한다

`_ask_arbiter`는 `dict[int, bool]`을 반환하지 않는다. candidate를 인자로 받지 않으므로
candidate 승인까지 판단해서도 안 된다.

최소한 다음 두 typed value를 분리한다.

```text
_ArbiterResult
  state
  raw_text
  normalized_text

_CandidateDecision
  state
  similarity
  uncovered_seconds
  confidence measurements
  collapse_eligible
```

`_ArbiterResult.state`는 독립 응답의 유효성만 표현한다.

```text
not_requested
call_failed
unmatched
invalid_response
answered_empty
answered_repetitive
answered_valid
legacy_unknown
```

`_CandidateDecision.state`는 attempt별 candidate 판정이다.

```text
not_evaluated
candidate_empty
candidate_invalid_metadata
candidate_repetitive
candidate_low_confidence
candidate_invalid_timing
candidate_uncovered
candidate_disagreed
approved
legacy_unknown
```

다음 흐름을 지켜라.

```text
_ask_arbiter
  → _ArbiterResult
  → _rescue가 plan별로 보존
  → candidate attempt마다 _CandidateDecision 생성
  → _Rescued가 둘을 함께 운반
  → _assemble이 둘을 _apply에 명시적으로 전달
  → _apply는 decision.state == "approved"일 때만 replacement
```

생산자가 값을 알고도 소비자 시그니처가 나르지 않는 P4-04 모양을 반복하지 마라.
`_apply`가 `rescued is not None`만 보고 replacement하는 현재 계약은 폐기한다.

첫 rescue가 clean이어도 해당 plan의 arbiter를 정확히 한 번 요청한다. 되풀이 plan이
없는 영상은 arbiter 비용이 계속 0이다. 같은 plan의 retry마다 arbiter를 다시 부르지
않는다.

유효한 arbiter가 첫 candidate와 불일치해도 그 arbiter 결과를 보존한 채 다음 rescue
attempt를 허용한다. 두 번째 candidate가 일치하면 채택할 수 있다.

### 4. `arbiter_clean`을 `approved`로 재정의하지 않는다

기존 표면은 호환을 위해 유지한다.

- `arbiter_answered=True`: 구조적으로 답한
  `answered_empty`, `answered_repetitive`, `answered_valid`
- `arbiter_clean=True`: arbiter가 비어 있지 않고 자체 반복이 아닌
  `answered_valid`
- `arbiter_clean=True`이면서 candidate와 불일치할 수 있다
- replacement 권위는 오직 `CandidateDecision.state == "approved"`다

즉 `arbiter_clean`은 계속 “독립 모델이 되살릴 비반복 글자를 들었다”는 기존 뜻이다.
candidate 승인이라는 새 뜻을 덮어쓰지 마라.

### 5. similarity는 미보정 가설 하나일 뿐이다

비교는 다음과 같다.

```python
candidate_text = strip_for_compare(
    " ".join(segment.text for segment in candidate)
)
arbiter_text = strip_for_compare(arbiter.raw_text)
similarity = SequenceMatcher(
    None,
    candidate_text,
    arbiter_text,
    autojunk=False,
).ratio()
```

- candidate segment는 응답 순서대로 읽는다.
- `strip_for_compare`는 `vlogsrt/hangul.py`의 기존 함수를 재사용한다.
- `verify.py`를 import하지 않는다.
- 빈 정규화 결과는 비교하지 않는다.
- 새 설정:

```python
repair_arbiter_similarity_floor: float = 0.60
```

`0.60`은 **미보정 가설**이다. 검증된 문턱이라고 쓰지 마라.
`verify_cue_match_floor`은 matched chars / cue chars이고 여기의
`2M / (len(candidate) + len(arbiter))`와 다른 척도다. 그 값을 근거로 들지 마라.

다음 양방향 오류는 acceptance에 들어간다.

| candidate / arbiter | ratio | 의미 |
|---|---:|---|
| `이만 삼천 원이에요` / `23,000원이에요` | 0.471 | 같은 발화도 거절할 수 있다 |
| `구독해주세요` / `구경해주세요` | 0.833 | 다른 발화를 승인할 수 있다 |
| 같은 환각 / 같은 환각 | 1.000 | 같은 계열 모델이 함께 틀리면 통과한다 |

낮은 similarity로 참 rescue를 거절해도 귀결은 삭제가 아니라 KEEP다. 이것이 미보정
문턱을 v1에 허용할 수 있는 이유다.

### 6. candidate의 `no_speech_prob`과 `avg_logprob`을 채택 조건에 넣는다

repair가 이미 가진 Whisper segment metadata를 사용한다. Silero VAD나 새 모델 호출은
금지다.

새 설정:

```python
repair_no_speech_prob_ceiling: float = 0.60
repair_avg_logprob_floor: float = -1.0
```

둘 다 repair adoption에 대해 **미보정 값**이다. Whisper 응답 척도에서 가져온 초기
가설일 뿐, 이 회차의 precision/recall로 보정됐다고 주장하지 마라.

모든 candidate segment가 다음을 만족해야 한다.

```text
avg_logprob와 no_speech_prob 필드가 실제 응답에 존재한다
둘 다 유한 숫자다
0.0 <= no_speech_prob <= 1.0
no_speech_prob <= repair_no_speech_prob_ceiling
avg_logprob >= repair_avg_logprob_floor
```

하나라도 실패하면 candidate 전체가 `candidate_invalid_metadata` 또는
`candidate_low_confidence`이고 KEEP다. 높은 `no_speech_prob`은 candidate 모델 자신이
“말이 아닐 가능성이 높다”고 보고한 신호이므로, 두 fallible transcription의 글자
일치보다 강한 무료 거절 신호로 취급한다.

`Segment`의 기존 decode 기본값 `0.0` 때문에 metadata 부재가 조용히 합격으로 바뀌면
`protected=[]` 계보의 재발이다. Candidate parsing에서 원본 payload의 필드 존재 여부를
함께 운반하라. 기존 trace 호환을 깨려고 `Segment` 전체의 기본값을 제거하지 마라.

`check_confidence_meta_survives`가 이미 세 필드의 전달을 보호한다. 그 invariant를
약화하거나 이름만 만족시키지 마라.

### 7. coverage는 owned first-pass interval union과 비교한다

`plan.span` 양끝만 비교하지 마라. 다음 반례가 반드시 실패해야 한다.

```text
plan / owned union: [0, 120]
candidate:          [0, 1], [119, 120]
uncovered:          118 s
```

새 설정:

```python
repair_coverage_max_uncovered_seconds: float = 0.0
repair_time_tolerance_seconds: float = 0.05
```

두 값의 역할을 섞지 마라.

- `repair_coverage_max_uncovered_seconds=0.0`은 owned first-pass 구간의 실제 손실을
  허용하지 않는 정책이다.
- `repair_time_tolerance_seconds=0.05`는 containment·순서 비교에서 ffmpeg frame
  boundary와 float 표현 차이만 허용하는 numeric tolerance다.
- tolerance는 candidate 시각을 늘리거나 줄이는 값이 아니다.
- tolerance만큼 coverage가 된 것으로 간주하거나 segment 시각을 clamp하지 마라.

`_Plan`은 `_plan`이 이미 알고 있는 owned first-pass segment interval을 명시적으로
운반해야 한다. `plan.span` 하나에서 내부 구조를 다시 추정하지 마라.

검사는 다음 순서와 의미를 만족한다.

1. candidate segment가 하나 이상이다.
2. 모든 `start`, `end`와 confidence 값이 유한하다.
3. 모든 segment에 `end > start`가 성립한다.
4. 응답 순서는 시간 순이다.
5. 인접 segment의 역전·겹침이 tolerance보다 크면 거절한다.
6. plan 밖 초과가 tolerance보다 크면 거절한다.
7. owned first-pass interval들을 union한다.
8. candidate interval들을 union한다.
9. `owned_union - candidate_union`의 총 길이를 계산한다.
10. 양끝과 interior를 합친 `uncovered_seconds`가
    `repair_coverage_max_uncovered_seconds`보다 크면 거절한다.

실제 `[5]`의 candidate end는 plan보다 28ms 늦고, `[8]`의 start는
`3.05e-7`초 빠르다. 둘은 `0.05`초 containment tolerance 안이다. 이 값은 실제 발화
손실 허용량이 아니며 candidate 시각을 바꾸지 않는다.

coverage 실패 시 stretch, clamp, interpolation, synthetic segment를 만들지 않는다.
candidate 전체를 버리고 KEEP한다.

### 8. 시간은 계속 rescue Whisper segment에서만 온다

Rule 1은 그대로다.

- 채택된 글자와 시각은 rescue Whisper의 `segments[]`에서만 온다.
- Arbiter는 글자만 제공하고 timing producer가 되지 않는다.
- coverage를 통과시키려고 시간을 늘리거나 옮기지 않는다.
- forced alignment, word timing, 보간, `mapping`, `gapfill`을 만들지 않는다.
- `ARBITER_KIND`는 계속 `verify_model`·`verify_format`을 사용하는 text-only seam이다.
- `prompt`·`keywords`의 기존 hinted-kind 경계를 넓히지 않는다.

### 9. 레거시 엔진에 Silero VAD를 넣지 않는다

`vlogsrt/repair.py`와 다른 레거시 `vlogsrt/*.py`는 다음을 할 수 없다.

- `vlogsrt.project.**` import
- `numpy` import
- `onnxruntime` import
- 저장 PCM을 새로 읽어 별도 발화 판정
- `vlogsrt/project/vad/` 구현 재사용

`no_speech_prob`, `avg_logprob`, rescue segment 시각, owned first-pass 시각처럼 repair가
이미 가진 값만 쓴다. 실물 VAD 수치는 진단·사람 확인용이며 제품 채택 입력이 아니다.

### 10. project wrapper도 arbiter rejection을 오류로 승격하지 않는다

현재 `vlogsrt/project/speech.py`와 `vlogsrt/project/boundary.py`의 사유 capture는
arbiter call/payload/coverage failure를 포획한 뒤 전체 project trace를 `error`로 만든다.
mandatory arbiter 이후에는 이것이 `KEEP + WARN` 계약과 충돌한다.

다음처럼 좁게 고친다.

- `FIXTURE_KIND` initial transcription, `RESCUE_KIND`, injected cutter의 운영 실패는 기존
  project typed-error 계약을 유지한다.
- `ARBITER_KIND`의 call failure, missing, duplicate, unmatched, malformed payload는
  repair의 explicit arbiter state로 전달하고 해당 plan을 KEEP한다.
- 이 arbiter 실패만으로 source/boundary transcript 전체를 `error`로 만들지 않는다.
- `VERIFY_KIND`의 기존 검증 정책은 바꾸지 않는다.
- project wrapper가 arbiter payload 검증을 없애지는 않는다. 실패를 명시적 repair
  상태로 넘기되 fatal capture에 섞지 않는 것이다.
- 다른 source, boundary, artifact, cache 계약은 바꾸지 않는다.

## Trace 계약과 9.0 호환

`TRACE_VERSION`은 올리지 않는다. 현재 `"9.0"`을 유지한다.

`Repair`에 최소한 다음 additive 감사 필드를 둔다. 정확한 내부 타입명은 바꿀 수 있지만
동일한 정보를 잃지 마라.

```text
arbiter_state
adoption_state
arbiter_text
arbiter_normalized_text
candidate_similarity
candidate_uncovered_seconds
candidate_max_no_speech_prob
candidate_min_avg_logprob
```

새 기록은 상태와 bool 사이에 모순이 없어야 한다.

기존 9.0 record에 새 필드가 없을 때:

- 기존 `arbiter_answered`·`arbiter_clean`은 그대로 decode한다.
- 새 `arbiter_state`와 `adoption_state`는 `legacy_unknown`으로 읽는다.
- 과거 `arbiter_answered=True`를 `approved`로 승격하지 않는다.
- 과거 `arbiter_answered=False`를 `not_requested`나 `call_failed`로 꾸며내지 않는다.
- 기존 trace load와 `vlogsrt redo`가 계속 동작해야 한다.

`Repair.verdict`는 독립 arbiter 결과와 adoption 결과를 구별해 표현해야 한다.
`arbiter_clean=True`를 곧바로 `"approved"`라고 출력하지 마라.

## Warning contract

다음 기존 문구는 새 기본값과 모순이므로 함께 고친다.

- `vlogsrt/repair.py`의
  `"심판 모델을 부르지 못했다: ... 더 시도하지 않는다."`
- `_ask_whisper` 주석과 docstring의 “실패는 접기로 이어진다”
- `_apply` docstring의 “실패면 길이로 나눈다”
- `_report`의
  `"짧아서 접지 않는다 — 1차 전사가 그대로 남는다."`
- `_report`의
  `"첫 장만 남기고 그 구간은 비운다."`
- 모듈 머리말의 “긴 실패는 collapsed, 짧은 실패는 kept”
- `vlogsrt/project/speech.py`와 `vlogsrt/project/boundary.py`의
  “arbiter 예외를 유지·접기 정책으로 보낸 뒤 project error로 만든다”는 설명

새 WARN은 최소한 다음을 사람이 구별할 수 있어야 한다.

```text
채택하지 않은 이유
arbiter_state
adoption_state
attempt 수
1차 글자·시각을 그대로 두었다는 사실
opt-in collapse가 실제 실행됐는지
```

권장 문구의 의미는 다음과 같다.

```text
… 재전사 대체본을 채택하지 않았다
(arbiter=<state>, adoption=<state>). 1차 전사의 글자와 시각을 그대로 둔다.
```

default-off 때문에 collapse eligible 결과를 KEEP할 때는 다음 의미도 드러낸다.

```text
… 자동 접기가 꺼져 있어 1차 전사를 그대로 둔다.
```

실제로 opt-in collapse가 실행될 때만 다음 의미를 출력한다.

```text
… repair_collapse_enabled=true에 따라 첫 장만 남기고 그 구간을 비운다.
```

`summarize`의 `접음 N` 항목은 capability 감사용으로 남겨도 되지만, default에서 실패
plan이 자동으로 그 수에 들어가서는 안 된다.

## 반드시 바꿀 기존 테스트

### `tests/test_repair.py`

다음 테스트는 새 계약과 직접 충돌하거나, 새 adoption 조건을 명시하도록 고쳐야 한다.

- `test_the_arbiter_is_not_paid_for_when_whisper_recovers`
  - 삭제 후 mandatory-arbiter 테스트로 교체
  - clean candidate에도 `RESCUE_KIND`, `ARBITER_KIND`가 plan당 한 번씩 호출돼야 한다
- `test_a_long_span_that_loops_again_is_collapsed`
  - default `REPAIR_KEPT`로 변경
  - 별도 opt-in case에서만 `REPAIR_COLLAPSED`
- `test_a_dead_api_collapses_instead_of_killing_the_run`
  - default KEEP와 first-pass 보존으로 변경
- `test_a_letter_run_that_stays_letters_is_not_recovered`
  - default KEEP
  - 필요하면 opt-in confirmed-repeat case로 collapse capability를 별도 확인
- `test_a_residual_loop_fails_even_when_the_share_passes`
  - 실패 결과를 default KEEP로 변경
- `test_one_piece_recovers_while_its_neighbours_collapse`
  - default는 `REPLACED, KEPT, KEPT`
  - opt-in capability는 별도 테스트
- `test_a_silent_arbiter_is_not_reported_as_a_verdict`
- `test_the_arbiter_buys_another_attempt_when_it_hears_speech`
- `test_the_arbiter_stops_the_retry_when_it_hears_the_same_loop`
- `test_an_empty_arbiter_answer_does_not_buy_another_attempt`
  - 새 arbiter/adoption state를 검증
- replacement를 기대하는 다음 테스트:
  - `test_a_recovered_span_is_replaced`
  - `test_replacement_times_stay_on_the_original_timeline`
  - `test_a_letter_run_can_be_rescued_like_anything_else`
  - `test_speech_straddling_the_span_is_not_duplicated`
  - `test_a_long_recovery_is_not_collapsed_for_being_long`
  - `test_the_repair_record_counts_what_changed`
  - `test_the_same_result_passes_once_the_absolute_ceiling_is_raised`
  - `test_an_injected_cutter_replaces_the_audio_requirement`
  - `test_the_default_cutter_keeps_the_legacy_catch_path`

위 replacement 테스트는 우연한 기본 `CLEAN`이나 짧은 partial segment에 기대지 말고,
각 테스트의 owned interval을 실제로 덮는 candidate, 명시적 confidence metadata,
candidate 전체 글자와 일치하는 arbiter를 제공해야 한다.

### `tests/test_repair_pipeline.py`

다음 기대를 새 default와 맞춘다.

- `test_a_missing_rescue_fixture_collapses_the_span` → KEEP
- `test_a_collapsed_span_still_leaves_the_rest_of_the_video`
  → default KEEP 보존 테스트로 이름과 assertion 변경
- `test_one_piece_recovers_while_the_others_collapse`
  → default `REPLACED, KEPT, KEPT`
- `test_a_letter_run_no_longer_walks_into_the_srt`
  → default에서 letter run은 보이게 남고 WARN/trace가 있어야 한다
- `test_the_letter_run_is_reported_not_swallowed`
  → `REPAIR_KEPT`와 잔존분 기록
- `test_the_arbiter_is_only_asked_once_when_it_hears_nothing`
  → `answered_empty`, KEEP, attempt 1
- 정상 replacement 경로 테스트는 matching arbiter, confidence, union coverage를 명시한다.

합성 `*.rescue.json`의 segment timing·metadata와 `*.arbiter.json`의 글자는 새 조건을
실제로 시험하도록 고친다. `tests/fixtures/api/**`는 건드리지 않는다.

### `tests/project/test_speech.py`

> **Q1B-2로 미룸.** 이 파일은 Q1B-1의 ownership이 아니다. Q1B-1이 여기를
> 깨뜨리면 고치지 말고 깨지는 목록을 보고하라 — 그것이 Q1B-2의 입력이다.

다음 테스트의 `arbiter_payload=None`은 mandatory arbiter와 양립하지 않는다.

- `test_a_refine_failure_preserves_the_settled_stages`
- `test_an_empty_refine_result_preserves_the_settled_stages`
- `test_a_publication_conflict_after_repair_preserves_every_completed_stage`
- `test_a_recovered_repeat_is_replaced_using_retained_pcm`
- `test_extra_namespace_records_outside_the_retry_subset_are_filtered`

이 테스트들이 replacement 자체를 검증하려면 matching arbiter와 full candidate metadata/
coverage를 넣는다.

다음 operational expectation은 바꾼다.

- `test_a_dead_arbiter_call_is_captured`
  - source 전체 error가 아니라 `REPAIR_KEPT + call_failed`
- `test_a_malformed_arbiter_payload_is_a_captured_operational_error`
  - source 전체 error가 아니라 `REPAIR_KEPT + invalid_response`
- `test_a_dead_arbiter_handler_is_still_the_only_extra_call`
  - 호출 수는 유지하되 성공 trace의 KEEP/WARN을 검증
- `test_a_long_unrecovered_repeat_is_collapsed`
  - default KEEP, 별도 opt-in collapse
- empty/repetitive/list-text arbiter 테스트는 독립 arbiter state와
  `arbiter_clean`의 기존 의미를 함께 검증한다.

Rescue·cutter 자체의 typed-error 테스트는 그대로 유지한다.

### `tests/project/test_boundary_transcribe.py`

> **Q1B-2로 미룸.** 이 파일은 Q1B-1의 ownership이 아니다. Q1B-1이 여기를
> 깨뜨리면 고치지 말고 깨지는 목록을 보고하라 — 그것이 Q1B-2의 입력이다.

다음 두 직접 충돌을 반드시 교체한다.

- `test_recovered_repeat_is_replaced_from_the_retained_pcm`
  (`tests/project/test_boundary_transcribe.py:779`)
- `test_reference_stem_fixture_namespace_reuse_and_retry_subset`
  (`tests/project/test_boundary_transcribe.py:895`)

`arbiter_payload=None`으로 `REPAIR_REPLACED`와 arbiter 호출 없음이 나와서는 안 된다.
replacement를 시험하려면 matching arbiter를 넣고 call order에 `ARBITER_KIND`를 포함한다.

함께 영향받는 테스트:

- `test_nested_repair_cut_is_clamped_to_the_retained_boundary`
- `test_long_unrecovered_repeat_is_collapsed`
  - default KEEP, 별도 opt-in collapse
- `test_a_dead_arbiter_call_is_a_typed_error`
  - boundary 전체 error가 아니라 KEEP
- `test_a_malformed_arbiter_payload_is_a_typed_boundary_error`
  - boundary 전체 error가 아니라 KEEP
- empty/repetitive/list-text arbiter와 hotter retry 테스트
  - 새 두 상태와 retry 재사용을 검증

Initial·rescue·cutter의 project typed-error 계약은 바꾸지 않는다.

## 문서 정합성 — 반드시 같은 변경에 포함한다

### 현재 사용자 계약

- `CLAUDE.md:6–9`
  - repair는 기본 켜짐이지만 automatic collapse는 기본 꺼짐이라고 쓴다.
- `CLAUDE.md:84–93`
  - 규칙 9의 자동 긴-repeat 예외를 opt-in 예외로 좁히고 default KEEP를 명시한다.
- `CLAUDE.md:122–130`
  - mandatory arbiter, confidence, union coverage, default KEEP 흐름으로 갱신한다.
- `CLAUDE.md:292–296`
  - `collapse`는 opt-in, `keep`은 길이와 무관한 default fallback으로 고친다.
- `README.md:173–181`
  - clean rescue에도 arbiter가 필요하고 실패는 default KEEP라고 고친다.
- `docs/RUNBOOK.md:141–153`
  - 긴 실패가 자동 공백을 만든다는 설명을 opt-in으로 바꾸고, visible 반복과 rejection
    state를 검사하도록 한다.
- `vlogsrt.toml:69–84`
  - `repair_collapse_enabled = false`와 새 adoption 설정 예시를 추가하고 기존 “길이로
    갈린다” 설명을 고친다.
- `TASKS.md:61–74`
  - S8의 역사적 결과는 보존하되 Q1B에서 default가 뒤집혔음을 명시한다.

### D27·D28·D29 기록

역사적 실측을 삭제하거나 과거에 그런 결정이 없었던 것처럼 다시 쓰지 마라. 대신
`docs/DECISIONS.md`에서 다음 문장들이 Q1B에 의해 **default만 superseded**됐음을
명시한다.

- D27 `1003–1004`
- D27 `1096–1113`
- D27 `1124–1129`
- D27 `1131–1138`
- D27 `1235–1256`
- D28 `1366–1377`
- D28 `1379–1387`
- D28 `1404–1415`
- D28 `1422–1434`
- D29 `1542–1565`
- D29 `1574–1583`

기록할 새 사실:

- collapse capability는 남지만 `repair_collapse_enabled=False`가 기본
- arbiter·confidence·coverage rejection은 길이와 무관하게 KEEP
- similarity와 confidence 문턱은 미보정 가설
- 음악 구간의 103개 first-pass cue가 되돌아올 수 있다는 정직한 tradeoff
- 이 변경은 D27/D28의 삭제 기본값을 뒤집지만, 반복 검출·재시도·명시적 opt-in
  collapse capability는 보존

### project 계약 문서

- `docs/orchestration/tasks/p3-02-source-adapter.md:89–97,104`
- `docs/orchestration/tasks/p4-03-boundary-transcribe.md:42–51`

이 두 문서의 “arbiter operational failure가 project 전체 typed error” 및 “legacy
repair semantics unchanged” 문장을 Q1B 예외로 정정한다. Initial/rescue/cutter failure
계약까지 느슨하게 만들지 마라.

새 임계값의 추가 설명을 위 ownership 밖 문서에 퍼뜨리지 마라. 완료 보고에 값과 근거를
정확히 남긴다.

## Red-first

먼저 아래 테스트를 작성하고 각각 현재 코드에서 실패하는 것을 확인한다.

### 1. rejection은 길이와 무관하게 KEEP다

```text
test_repair_rejection_never_collapses_by_default
```

40초 이상 repeat에 다음을 parameterize한다.

```text
call_failed
unmatched
invalid_response
answered_empty
candidate_disagreed
candidate_invalid_metadata
candidate_low_confidence
candidate_invalid_timing
candidate_uncovered
```

모두 다음을 요구한다.

```text
REPAIR_KEPT
first-pass text/times preserved at settle
replacement count == 0
collapse count == 0
WARN contains explicit states
```

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_repair_rejection_never_collapses_by_default -q
```

### 2. clean candidate도 arbiter 승인 없이는 채택하지 않는다

```text
test_clean_rescue_requires_one_matching_arbiter_call
```

- clean·covered·confident candidate
- arbiter call failure → KEEP
- matching arbiter → REPLACED
- retry가 있어도 arbiter call은 plan당 정확히 1회

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_clean_rescue_requires_one_matching_arbiter_call -q
```

### 3. valid arbiter disagreement 뒤 다음 retry가 살아 있다

```text
test_valid_disagreeing_arbiter_can_approve_the_next_retry
```

- 첫 candidate는 valid arbiter와 불일치
- 같은 arbiter 결과를 재사용
- 두 번째 candidate는 일치
- arbiter 1회, rescue 2회, 최종 `approved + REPAIR_REPLACED`

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_valid_disagreeing_arbiter_can_approve_the_next_retry -q
```

### 4. similarity 양방향 오류와 confidence rail

```text
test_similarity_counterexamples_fail_safe_to_keep
```

최소 세 경우:

1. `이만 삼천 원이에요` / `23,000원이에요` → 0.471, KEEP
2. `구독해주세요` / `구경해주세요` → 0.833이지만 높은 `no_speech_prob`, KEEP
3. 동일 환각 / 동일 환각 → 1.0이지만 낮은 `avg_logprob`, KEEP

`no_speech_prob`과 `avg_logprob`을 각각 독립적으로 바꿔 둘 다 실제 adoption 입력임을
증명한다. metadata 누락도 KEEP여야 한다.

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_similarity_counterexamples_fail_safe_to_keep -q
```

### 5. interior coverage와 numeric tolerance를 분리한다

```text
test_candidate_coverage_uses_the_owned_interval_union
```

필수 cases:

- owned `[0,120]`, candidate `[0,1]`·`[119,120]` → uncovered 118초, KEEP
- 60초 leading hole → KEEP
- trailing hole → KEEP
- owned interior interval을 비움 → KEEP
- plan end보다 28ms 늦음 → containment 허용, 시간을 고치지 않음
- plan start보다 `3.05e-7`초 빠름 → containment 허용, 시간을 고치지 않음
- tolerance를 넘는 plan 밖 시각 → KEEP
- full owned union coverage → 다른 조건이 모두 맞으면 REPLACED

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_candidate_coverage_uses_the_owned_interval_union -q
```

### 6. collapse capability는 명시적으로만 열린다

```text
test_confirmed_repeat_collapses_only_when_explicitly_enabled
```

같은 입력을 두 설정으로 실행한다.

```text
repair_collapse_enabled=False → REPAIR_KEPT
repair_collapse_enabled=True  → REPAIR_COLLAPSED
```

call failure, empty, disagreement, confidence failure, coverage failure는
`repair_collapse_enabled=True`여도 KEEP임을 함께 고정한다.

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_confirmed_repeat_collapses_only_when_explicitly_enabled -q
```

### 7. trace가 독립 arbiter와 adoption을 분리한다

```text
test_trace_separates_arbiter_validity_from_candidate_approval
```

최소한 다음 왕복을 확인한다.

```text
answered_valid + candidate_disagreed + arbiter_clean=True
answered_valid + approved + arbiter_clean=True
answered_empty + not_evaluated + arbiter_clean=False
call_failed + not_evaluated + arbiter_answered=False
legacy 9.0 missing fields → legacy_unknown, never approved
```

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_trace_separates_arbiter_validity_from_candidate_approval -q
```

### 8. project 경로도 arbiter failure를 KEEP로 보낸다 — **Q1B-2로 미룸, 지금 쓰지 마라**

```text
test_project_arbiter_failure_keeps_the_first_pass
```

`tests/project/test_speech.py`와 `tests/project/test_boundary_transcribe.py` 양쪽에 둔다.
arbiter handler absence·exception·malformed payload를 parameterize하고 다음을 요구한다.

```text
project transcript remains successful
REPAIR_KEPT
first-pass settle text/times preserved
explicit arbiter state
no replacement
no collapse
```

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/project/test_speech.py::test_project_arbiter_failure_keeps_the_first_pass tests/project/test_boundary_transcribe.py::test_project_arbiter_failure_keeps_the_first_pass -q
```

각 red command의 실제 실패 출력과 수정 후 성공 출력을 완료 보고에 남긴다.

## 구현 레일

- `chunk_max_seconds`, `transcribe_temperature`, 모든 기존 `loop_*`, 기존 `repair_*`
  값을 바꾸지 마라.
- 허용된 것은 위 새 `repair_*` 필드뿐이다.
- `_Plan`이 owned interval을 운반한다. coverage가 span에서 owned를 추정하지 않는다.
- `_ArbiterResult`와 `_CandidateDecision`을 하나의 bool로 합치지 않는다.
- raw/normalized arbiter text와 attempt decision이 `_rescue → _assemble → _apply`를
  실제로 지난다.
- plan index, chunk index, response index를 위치만으로 섞지 않는다.
- absence, empty dict, falsey string이 replacement 또는 collapse permission이 되지 않는다.
- `repair.py`에서 `verify`, `refine`, `output`, `layout`, `vlogsrt.project`를 import하지
  않는다.
- `mapping`·`gapfill`을 되살리지 않는다.
- 새 dependency를 추가하지 않는다.
- 시간은 늘리거나 clamp하거나 합성하지 않는다.
- `SequenceMatcher(..., autojunk=False)`를 명시한다.
- `ARBITER_KIND`를 timing producer로 바꾸지 않는다.
- 실제 VAD 결과를 제품 코드에 복사하지 않는다.

## Offline and cost safety

- 모든 명령에서 `VLOGSRT_OFFLINE=1`을 유지한다.
- `--online`을 쓰지 않는다.
- API·유료 호출을 하지 않는다.
- `.env`, `.env.example`을 읽거나 수정하지 않는다.
- API fixture를 녹화하거나 수정하지 않는다.
- 테스트는 fake 또는 기존 synthetic fixture만 사용한다.
- 네트워크 호출 수 보고는 `0`이어야 한다.

제품 비용 변화는 정직하게 보고한다.

- 되풀이가 없는 영상: 추가 호출 0
- 되풀이 plan: clean 첫 candidate도 arbiter 1회 필요
- 같은 plan의 retry: arbiter 결과 재사용, 중복 결제 없음

## Acceptance

Focused suite:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py tests/test_repair_pipeline.py tests/project/test_speech.py tests/project/test_boundary_transcribe.py
```

전체 검사:

```bash
VLOGSRT_OFFLINE=1 make check
```

불변식과 실제 `[OK]` 수:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python scripts/check_invariants.py | tee /tmp/q1b-invariants.txt
rg -c '^\[OK\]' /tmp/q1b-invariants.txt
```

구현자가 출력에서 직접 센 실제 숫자를 보고한다. 예상 숫자나 과거 보고를 복사하지 마라.
이 프로젝트에서 `[OK]` 수의 부정확한 자기 보고가 이미 다섯 번 있었다.

Diff 검사:

```bash
git diff --check
git diff --stat a0c5be1..HEAD
git diff --name-only a0c5be1..HEAD
git status --short
```

완료 조건:

- 모든 replacement가 explicit `approved`를 요구한다.
- valid disagreement 뒤 다음 retry가 가능하다.
- arbiter는 plan당 최대 1회다.
- similarity의 양방향 오류가 테스트에 고정된다.
- high `no_speech_prob`, low `avg_logprob`, missing metadata가 candidate를 거절한다.
- rejection은 길이와 `repair_collapse_enabled` 값에 상관없이 KEEP한다.
- default의 긴 unrecovered repeat도 KEEP한다.
- opt-in confirmed-repeat case만 collapse capability에 도달한다.
- interior hole을 포함한 exact uncovered total을 계산한다.
- 28ms·`3.05e-7`초 경계 초과는 tolerance 안에서 비교만 허용하고 시각은 바꾸지 않는다.
- `settle` 경계에서 rejected plan의 first-pass 글자·시각이 보존된다.
- project source/boundary의 arbiter failure도 KEEP이며 전체 transcript를 error로 만들지 않는다.
- initial/rescue/cutter project failures의 기존 typed-error 계약은 유지된다.
- 기존 trace 9.0이 load/redo 가능하다.
- 새 trace가 arbiter validity와 candidate adoption을 분리한다.
- `make check`가 통과한다.
- 불변식 `[OK]`의 실제 개수를 직접 센다.
- 네트워크·유료 호출은 0이다.
- `.env`와 사용자 소유 `.env.example`을 건드리지 않는다.
- diff는 ownership 안에만 있다.

## 실물 확인 — `PENDING_HUMAN`

이번 구현자는 실제 미디어를 유료 재실행하지 않는다. `out/fr_day_3` 성공 판정은
`PENDING_HUMAN`으로 남긴다.

현재 baseline은 서로 다른 두 지표를 정확히 구별해 기록한다.

```text
speech_onsets.py의 “절반 미만 덮인 speech span 전체 길이”:
269.952 s · 92 spans

같은 VAD 분석에서 실제로 자막이 덮지 않은 총 시간:
458.092 s

phantom cue 후보:
30
```

`269.952초`를 “정확한 missed seconds”라고 부르지 마라. 그것은 절반 미만 덮인 speech
span의 전체 길이 합이다. 정확한 uncovered total은 `458.092초`다.

사람이 paid re-run 뒤 확인할 것:

- 승인 없는 `REPAIR_REPLACED`가 0인지
- rejection이 `REPAIR_COLLAPSED`로 흐른 건수가 0인지
- default 실행의 automatic collapse가 0인지
- `37:18–38:33`의 75초 대화가 first-pass 형태로라도 보존되는지
- 모든 replacement의 owned-union uncovered total이 0인지
- confidence metadata와 trace state가 실제 candidate와 일치하는지
- `04:36–06:13` 음악 구간에서 103개의 `"맛있어?"` 계열 first-pass cue가 다시 보일
  수 있다는 tradeoff가 로그·검토에서 숨겨지지 않는지
- 약한 speech-span 지표 `269.952초`와 exact uncovered `458.092초`를 각각 다시 측정했는지

실제 paid re-run 전에는 이 항목을 PASS로 보고하지 마라.

## Report

완료 보고에는 다음을 포함한다.

- base `a0c5be1`와 최종 SHA
- `git diff --stat a0c5be1..HEAD`
- 수정 파일 목록
- 모든 red-first command의 수정 전 실패와 수정 후 성공
- focused suite와 `make check`의 실제 test 수
- 직접 센 `[OK]` 줄의 실제 개수
- arbiter state와 adoption state 전이표
- `_rescue → _assemble → _apply`의 실제 typed-value 전달 경로
- `arbiter_clean`의 기존 의미를 보존한 증거
- trace 9.0 load/redo 호환 증거
- default KEEP와 opt-in collapse의 대조 출력
- 모든 rejection reason이 KEEP로 간 테스트 출력
- interior 118초 hole이 거절된 출력
- 28ms·`3.05e-7`초 tolerance case가 시각 변경 없이 처리된 출력
- similarity 양방향 반례 결과
- `repair_arbiter_similarity_floor = 0.60`이 미보정 가설이라는 기록
- `repair_no_speech_prob_ceiling = 0.60`이 미보정 가설이라는 기록
- `repair_avg_logprob_floor = -1.0`이 미보정 가설이라는 기록
- `repair_coverage_max_uncovered_seconds = 0.0`과
  `repair_time_tolerance_seconds = 0.05`의 서로 다른 역할
- repaired plan당 arbiter 1회, retry 재사용 증거
- 네트워크·유료 호출 `0`
- 실물 재실행 `PENDING_HUMAN`
- `.env`와 `.env.example`을 건드리지 않았다는 확인
- 사용자 소유 `.env.example` 변경이 diff/stat/commit에 섞이지 않았다는 확인
