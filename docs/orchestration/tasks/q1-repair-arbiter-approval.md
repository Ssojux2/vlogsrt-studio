# Task Q1: 재전사 채택에 심판 승인과 커버리지를 요구한다

## Authority

`CLAUDE.md` 규칙 1·4·5·9, D35의 규칙 4·9 파생 계층 조항,
`docs/DECISIONS.md` D27·D28·D29, 그리고
`docs/orchestration/reviews/sol-repair-arbiter-diagnosis.md`.

코디네이터 판정은 확정됐다. 다시 논의하거나 범위를 넓히지 마라. 구현자는 GLM이며,
모든 작업과 검증은 오프라인 전용이다.

## Baseline and ownership

- 저장소: `/Users/ssojux2/Working/vlogsrt`
- branch: `feat/final-cut-project`
- exact base: `30660b6`
- 허용 파일:
  - `vlogsrt/repair.py`
  - `vlogsrt/models.py`
  - `vlogsrt/transcribe.py`
  - `vlogsrt/config.py`
  - `tests/test_repair.py`
- 그 밖의 파일은 수정하지 마라.
- 특히 `.env`, `.env.example`, `vlogsrt.toml`, `docs/DECISIONS.md`,
  `tests/fixtures/api/**`, `golden/**`는 수정하지 마라.
- `.env.example`에는 사용자가 소유한 미커밋 변경이 이미 있다. 열람·정리·포맷·복원·커밋하지
  마라.
- `vlogsrt/transcribe.py`는 `ARBITER_KIND`의 모델·응답 seam을 보존하거나 필요한 만큼만
  명시화한다. 새 네트워크 경계를 만들지 마라.
- `vlogsrt/config.py`는 아래 새 `repair_*` 필드가 실제로 필요할 때만 수정한다.

## 결함 — 첫 rescue가 심판보다 먼저 채택된다

실제 62분 실행 `out/fr_day_3`에는 repair가 14건 있고, 13건의
`arbiter_answered`가 `False`다. 이는 심판 호출 실패가 아니라 **호출되지 않았다는 뜻**이다.

현재 경로는 다음과 같다.

1. rescue 시도 시작: `vlogsrt/repair.py:351`
2. 응답이 비지 않고 자체 반복 검사만 통과하면 즉시 `found.segments`에 저장:
   `vlogsrt/repair.py:357–361`
3. `failed`가 비어 심판 분기 전에 `break`: `vlogsrt/repair.py:364–365`
4. `_apply`는 `rescued.segments[index]`가 있다는 이유만으로 교체:
   `vlogsrt/repair.py:487–496`
5. `arbiter_answered`는 채택 조건이 아니라 사후 기록일 뿐이다:
   `vlogsrt/repair.py:536–545`

현재 합격 조건은 다음 셋뿐이다.

```text
segments가 비어 있지 않음
repetitive_seconds <= repair_residual_seconds
repetitive_share <= loop_repetitive_share
```

근거는 `vlogsrt/repair.py:357–395`다. 독립 심판과의 일치, 심판 응답 여부, candidate의
시간 커버리지는 보지 않는다.

실제 결과로 다음 오채택이 확인됐다.

- `"한 번 더"` ×5 → `"구독해주세요"`
- `"And here it was a hi…"` ×3 → `"내일도 영상/봐주세용"`
- `"치즈가 왔다!"` ×7, 8 segments → `"감사합니다"`
- repair `[12]`의 plan span은 `2918.32–3032.32`인데 첫 replacement segment는
  `2978.34`에 시작한다. 약 60초의 leading hole이 있는데도 `replaced`다.

별도 VAD 조사에서 `04:36–06:13`은 평균 발화 확률 `0.005`이면서 진폭은 높았다. 음악
구간인데 1차 whisper는 `"맛있어?"` 103장을, repair는 다시 12개의 지어낸 cue를 만들었다.
이 VAD는 진단 증거일 뿐 이번 레거시 구현의 입력이 아니다.

## 확정 판정 — 그대로 구현한다

1. **레거시 엔진은 Silero VAD를 쓰지 않는다.** `vlogsrt/*.py`는
   `vlogsrt.project.**`를 import할 수 없고, NumPy/ONNX Runtime은
   `vlogsrt/project/vad/` 밖에서 금지된다. 커버리지는 `plan.span`과 rescue candidate
   segment의 시각만으로 판정한다.

2. **시각은 계속 rescue whisper의 `segments[]`에서만 온다.** 보간, forced alignment,
   합성 시각, `mapping`/`gapfill` 부활은 금지다. Arbiter는 글자만 제공하며 시각을 만들지
   않는다.

3. **거절은 기존 fallback으로 간다.** `vlogsrt/repair.py:497–518`의
   KEEP/collapse 경로를 그대로 쓴다. 침묵이나 새 empty outcome을 만들지 마라.
   `repair_collapse_min_seconds` 미만의 짧은 되풀이는 D28대로 1차 전사를 KEEP한다.

4. **잠긴 임계값을 바꾸지 않는다.** `chunk_max_seconds`,
   `transcribe_temperature`, 모든 기존 `loop_*`, 기존 `repair_*` 값은 그대로다.

5. repair `[3]`과 `[6]`의 cue가 사라졌다는 이전 주장은 틀렸다. 두 cue는 SRT에 있으며
   `lead_in`/`lead_out`과 `merge_pass` 때문에 repair span 밖으로 밀렸을 뿐이다.
   이 비결함을 고치는 코드를 쓰지 마라.

## 구현 계약 1 — answered arbiter의 candidate 일치 승인이 필수다

`_ask_arbiter`는 `dict[int, bool]`을 반환하지 않는다. 각 plan에 대해 독립 전사의
정규화된 글자와 명시적 상태를 반환한다. 그 값을 `_Rescued`와 adoption 함수의
시그니처가 실제로 운반해야 한다.

처리 순서는 다음 계약을 만족해야 한다.

1. 첫 rescue candidate가 자체 비어 있음·반복 검사를 통과해도 provisional로만 둔다.
2. 해당 plan의 arbiter를 정확히 한 번 요청한다.
3. Arbiter가 유효한 비반복 글자로 답하고 candidate와 일치할 때만
   `found.segments[index]`에 넣는다.
4. 호출 실패, 응답 미매칭, 빈 답, 반복 답, candidate 불일치는 모두 replacement 거절이다.
5. Arbiter의 유효한 독립 전사는 다음 rescue attempt에도 재사용할 수 있다. 같은 plan에
   arbiter를 attempt마다 다시 결제하지 마라.
6. 최종 adoption은 반드시 `arbiter_state == "approved"`와 candidate 자체 조건 및 시간
   커버리지 조건을 모두 요구한다. 상태 부재나 빈 값이 permission이 되어서는 안 된다.

### “일치”의 정확한 정의

다음 비교를 사용한다.

```python
candidate_text = strip_for_compare(" ".join(segment.text for segment in candidate))
arbiter_text = strip_for_compare(independent_transcription)
similarity = SequenceMatcher(
    None,
    candidate_text,
    arbiter_text,
    autojunk=False,
).ratio()
approved = similarity >= cfg.repair_arbiter_similarity_floor
```

- candidate segment는 시간 순서로 읽는다.
- `strip_for_compare`는 `vlogsrt/hangul.py`의 기존 정규화를 재사용한다.
- `verify.py`를 import하지 마라. 규칙 5의 계층 경계를 유지한다.
- 빈 정규화 결과는 비교하지 않고 `answered_empty`다.
- Arbiter 자체가 반복이면 비교하지 않고 `answered_repetitive`다.
- 새 설정은 `repair_arbiter_similarity_floor: float = 0.60`이다.
- `0.60`은 이미 독립 전사와 cue를 대조하는 `verify_cue_match_floor`의 기본 경계와 같다.
  D28이 독립 한국어 전사에서 `0.80`은 지나치게 엄격하다고 기록했으므로
  `verify_chunk_sim_floor`을 그대로 채택하지 않는다.
- 기존 `verify_cue_match_floor`을 직접 재사용하지 않는다. 검증 표시와 repair 채택은 권한과
  조정 수명이 다르므로 별도의 `repair_*` 이름이어야 한다.
- 이 새 값의 근거를 완료 보고에 그대로 적어 코디네이터가 `docs/DECISIONS.md`에 기록할 수
  있게 하라. 구현자는 ownership 밖의 문서를 직접 수정하지 않는다.

심판을 모든 repair candidate의 채택 문턱으로 올리므로 **repaired region마다 arbiter
호출 한 건이 추가된다.** 되풀이가 없는 영상의 비용은 계속 0이며, 같은 region의 재시도
횟수만큼 arbiter를 중복 호출하지 않는다.

## 구현 계약 2 — candidate 시간 커버리지

자체 반복 검사를 통과한 candidate도 다음 조건을 모두 만족해야 한다.

- candidate segment가 하나 이상이다.
- 모든 segment에 `end > start`가 성립한다.
- candidate 순서가 시간 순이고 segment끼리 뒤집히거나 겹치지 않는다.
- 모든 segment가 `plan.span` 안에 완전히 들어간다. 하나라도 plan 밖이면 candidate 전체를
  거절한다.
- 첫 candidate start와 plan start 사이의 leading hole이
  `cfg.repair_coverage_edge_seconds` 이하이다.
- plan end와 마지막 candidate end 사이의 trailing hole이
  `cfg.repair_coverage_edge_seconds` 이하이다.

새 설정은 `repair_coverage_edge_seconds: float = 2.0`이다. 계획 경계는 이미 owned
first-pass segment의 실제 시작·끝에서 나오며, 현재 `repair_margin = 2.0`도 경계 어절을
보호하는 context 폭이다. 같은 2초를 독립 이름으로 두는 이유는 planning margin과
replacement 손실 허용량을 하나의 설정으로 결합하지 않기 위해서다. 이 값보다 큰 edge
hole은 context 수준을 넘어 실제 owned 발화가 덮이지 않았다는 보수적 신호다.

이 값도 기존 임계값 변경이 아니라 새 recovery invariant의 설정이다. 근거를 완료 보고에
남겨 코디네이터가 `docs/DECISIONS.md`에 기록하게 하라.

커버리지 실패 시 시각을 늘리거나 옮기거나 채우지 않는다. candidate 전체를 버리고
`vlogsrt/repair.py:497`의 기존 KEEP/collapse 경로로 보낸다.

## 구현 계약 3 — arbiter 상태를 trace에 명시한다

`arbiter_answered: bool` 하나로 다음을 섞지 마라. 새 trace가 최소한 다음 상태를
구별해야 한다.

```text
not_requested
call_failed
unmatched
answered_empty
answered_repetitive
answered_disagreed
approved
```

의미는 다음과 같다.

| 상태 | 의미 | replacement |
|---|---|---|
| `not_requested` | 아직 요청하지 않았거나 실제로 요청 경로에 진입하지 않음 | 금지 |
| `call_failed` | 호출 또는 arbiter payload 해석 실패 | 금지 |
| `unmatched` | 응답이 요청 plan/chunk와 매칭되지 않음 | 금지 |
| `answered_empty` | 정상 응답이지만 정규화된 글자가 없음 | 금지 |
| `answered_repetitive` | 정상 응답이지만 arbiter 글자 자체가 반복 | 금지 |
| `answered_disagreed` | 유효한 독립 전사지만 candidate와 유사도 기준 미달 | 금지 |
| `approved` | 유효한 독립 전사와 candidate가 기준 이상으로 일치 | 허용 가능 |

`approved`는 반복·커버리지 조건까지 통과했다는 뜻으로 과적재하지 마라. 최종 채택은
`approved` **그리고** candidate 자체 반복 조건 **그리고** 시간 커버리지 조건의 교집합이다.

기존 project 테스트와 caller가 읽는 `Repair.arbiter_answered`와 `Repair.arbiter_clean`
표면은 이번 ownership 밖 파일을 깨지 않도록 호환시켜라. 다만 새 코드의 권위값은 명시적
상태이며, 두 bool을 독립 채택 근거로 사용하지 않는다. 모순된 상태를 만들 수 있는 이중
권위도 두지 마라.

### TRACE_VERSION 판정

이번 작업에서는 `TRACE_VERSION`을 올리지 않는다.

- `TRACE_VERSION`은 현재 `vlogsrt/__init__.py`의 `"9.0"`이며 그 파일은 ownership 밖이다.
- 새 상태는 additive하게 직렬화하고, `Repair.from_dict`가 기존 9.0 repair record를 계속
  읽게 한다.
- 기존 `arbiter_answered=False`는 `not_requested`나 `call_failed`를 구별할 증거가 없으므로
  새 상태로 거짓 변환하지 말고 `legacy_unknown` 같은 명시적 호환 상태로 읽는다.
- 기존 answered bool도 candidate 일치를 검사한 기록이 아니므로 `approved`로 승격하지
  않는다.
- `vlogsrt redo`는 `vlogsrt/pipeline.py:357–366`에서 기존 spine을 다시 조판하고 repair
  기록은 보존만 한다. 위 호환 decode를 유지하면 기존 trace의 redo가 깨지지 않는다.

`TRACE_VERSION`을 10.0으로 올리면 기존 9.0 trace는 `Trace.from_dict`의 version 검사
때문에 즉시 거절되어 `vlogsrt redo`가 깨진다. 그런 breaking migration은 이번 task의
권한이 아니다. 새 상태를 호환 decode로 표현할 수 없다고 판단하면 임의로 버전을 올리지
말고 코디네이터에게 ownership 확대를 요청하라.

## Red-first

먼저 아래 세 테스트를 작성하고, 각각 현재 코드에서 실패하는 것을 확인한다.

### 1. 심판이 없거나 불일치하면 clean rescue도 채택하지 않는다

```text
test_clean_rescue_is_not_adopted_when_arbiter_is_unanswered_or_disagrees
```

짧은 `"한 번 더"` 반복에 rescue `"구독해주세요"`를 주고 다음 두 경우를 parameterize한다.

- Arbiter 호출 실패
- Arbiter 정상 응답 `"한 번 더"`

둘 다 `REPAIR_KEPT`, first-pass 글자·시각 보존, replacement 0을 요구한다. 호출 실패와
불일치는 서로 다른 arbiter 상태여야 한다.

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_clean_rescue_is_not_adopted_when_arbiter_is_unanswered_or_disagrees -q
```

### 2. 큰 leading/trailing hole 또는 plan 밖 segment를 채택하지 않는다

```text
test_nonrepetitive_rescue_with_a_large_uncovered_leading_span_is_not_recovered
```

8초짜리 short repeat에 마지막 2초만 덮는 비반복 clean candidate와 일치하는 arbiter
글자를 제공한다. 텍스트 일치가 있어도 edge coverage가 실패하므로 `REPAIR_KEPT`와
first-pass 보존을 요구한다.

같은 테스트를 parameterize하거나 인접 assertion으로 다음도 고정한다.

- 큰 trailing hole
- segment start가 plan start보다 앞섬
- segment end가 plan end보다 뒤섬
- 정확히 `repair_coverage_edge_seconds`인 경계는 허용되고 그보다 큰 값은 거절됨

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_nonrepetitive_rescue_with_a_large_uncovered_leading_span_is_not_recovered -q
```

### 3. trace가 호출 안 함과 호출 실패를 구별한다

```text
test_trace_distinguishes_arbiter_not_requested_from_call_failed
```

최소한 `not_requested`와 `call_failed`가 `Repair` 직렬화 왕복 뒤에도 구별됨을 요구한다.
가능하면 같은 parameterized matrix에서 `unmatched`, `answered_empty`,
`answered_repetitive`, `answered_disagreed`, `approved`도 모두 왕복시켜라.

Red 명령:

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py::test_trace_distinguishes_arbiter_not_requested_from_call_failed -q
```

### 잘못된 기존 테스트를 반드시 교체한다

`tests/test_repair.py:168`의
`test_the_arbiter_is_not_paid_for_when_whisper_recovers`는 현재 fail-open을 옳은 동작으로
고정한다. 이 테스트는 **삭제 후 위 mandatory-arbiter 기대를 검증하는 테스트로 교체**한다.

이 테스트를 살리려고 구현을 우회하거나 이름만 바꾸지 마라. 이제 첫 rescue가 clean이어도
replacement 전에 arbiter가 정확히 한 번 호출되어야 한다.

기존 테스트 중 arbiter 기본 응답이 우연히 `CLEAN`이라 통과하던 것은 테스트가 검사하려는
차원을 명시하도록 정리한다. 예를 들어 residual ceiling만 검사하는 테스트는 rescue 전체
글자와 일치하는 arbiter 글자를 제공해야 한다. 기대값이나 기존 잠긴 임계값을 바꿔 green을
만들지 마라.

## 구현 레일

- `_ask_arbiter`의 독립 전사와 상태가 `_Rescued`를 거쳐 실제 adoption 조건까지 전달되어야
  한다.
- candidate text, arbiter text, similarity, coverage 결과를 서로 다른 plan index에
  위치로 잘못 결합하지 마라.
- Arbiter 응답은 시각을 제공하지 않는다. `ARBITER_KIND`를 verbose segment/timing
  producer로 바꾸지 마라.
- `ARBITER_KIND`는 계속 `verify_model`/`verify_format`을 사용하는 text-only seam이다:
  `vlogsrt/transcribe.py:320–328`.
- `prompt`/`keywords`의 기존 hinted-kind 규칙을 넓히거나 rescue whisper에 보내지 마라.
- `repair.py`에서 `verify`, `refine`, `output`, `layout`, `vlogsrt.project`를 import하지
  마라.
- 새 dependency를 추가하지 마라. `difflib`, `vlogsrt.hangul`이면 충분하다.
- `SequenceMatcher`에는 반드시 `autojunk=False`를 명시한다.
- 실패 상태와 거절 사유는 WARN/trace에서 사람이 구별할 수 있어야 한다.
- 거절된 short repeat는 원본 segment의 글자와 시각이 byte-equivalent한 downstream
  결과로 남아야 한다.

## 반복 금지할 결함 계보

### `protected=[]` 계보

이 프로젝트에서 다섯 번 반복된 모양은 **부재가 조용히 권한으로 뒤집히는 것**이다.

여기서는 다음이 같은 재발이다.

```text
arbiter state 없음
arbiter 응답 없음
빈 arbiter 글자
매칭되지 않은 arbiter 응답
```

이 중 어느 것도 replacement 허가가 아니다. 오직 명시적 `approved`만 채택 가능성이 있다.
`dict.get(index, False)` 같은 축약을 새 상태 모델의 권위로 남기지 마라.

### P4-04 모양

생산자는 정확한 값을 알지만 소비자 시그니처가 그것을 나르지 않아 하류가 값이나 권한을
다시 추정하는 결함이다.

- `_ask_arbiter`가 독립 전사를 알고도 bool만 반환하면 같은 결함이다.
- Adoption 함수가 arbiter text/state를 받지 않아 전역값·candidate·로그에서 재구성하면
  같은 결함이다.
- Coverage 함수가 `plan.span`을 받지 않아 chunk duration이나 candidate 길이로 plan을
  추정하면 같은 결함이다.

시그니처가 `plan.span`, candidate segments, arbiter transcription/state를 실제 판정
지점까지 운반하는지 구현 전에 확인하라.

## Offline and cost safety

- 모든 명령에서 `VLOGSRT_OFFLINE=1`을 유지한다.
- `--online`을 쓰지 마라.
- API·유료 호출을 하지 마라.
- `.env`를 읽거나 수정하지 마라.
- `.env.example`을 읽거나 수정하거나 사용자 변경에 포함하지 마라.
- API fixture가 없거나 낡아도 녹화하지 마라.
- 테스트는 `FakeTranscriber`만 사용한다.
- 네트워크 호출 수 보고는 `0`이어야 한다.
- 제품 비용 변화는 명시적으로 보고한다: mandatory arbiter 때문에 repaired region마다
  한 호출이 추가되며, 같은 region의 retry에서는 재사용한다.

## Acceptance

Red-first 증거를 남긴 뒤 다음을 실행한다.

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python -m pytest tests/test_repair.py
VLOGSRT_OFFLINE=1 make check
```

불변식의 `[OK]` 줄은 구현자가 직접 다시 실행해 센다.

```bash
VLOGSRT_OFFLINE=1 .venv/bin/python scripts/check_invariants.py | tee /tmp/q1-invariants.txt
rg -c '^\[OK\]' /tmp/q1-invariants.txt
```

예상 숫자를 복사하거나 과거 보고를 인용하지 마라. 출력에서 직접 센 **실제 숫자**를
보고한다. 이 프로젝트에서 `[OK]` 개수의 부정확한 자기 보고가 이미 다섯 번 있었다.

완료 조건:

- 세 red-first 테스트가 수정 전 실패하고 수정 후 통과한다.
- clean rescue도 arbiter가 없거나 불일치하면 replacement되지 않는다.
- `approved`가 아닌 상태에서는 replacement가 0이다.
- 큰 leading/trailing hole과 plan 밖 segment가 candidate 전체를 거절한다.
- 거절된 short repeat는 1차 전사를 KEEP한다.
- 긴 실패는 기존 collapse 정책만 따른다.
- 기존 trace 9.0이 계속 load/redo 가능하다.
- 새 trace는 arbiter 상태를 명시적으로 구별한다.
- `make check`가 통과한다.
- `[OK]` 줄의 실제 개수를 직접 세어 보고한다.
- 네트워크·유료 호출은 0이다.
- diff에는 허용된 다섯 파일 이외가 없어야 하며, 실제로 필요하지 않은 허용 파일은
  억지로 수정하지 않는다.
- 사용자 소유 `.env.example` 변경은 diff/stat/commit에 섞지 않는다.

## 실물 확인 — `PENDING_HUMAN`

이번 구현자는 실제 미디어를 유료 재실행하지 않는다. 따라서 `out/fr_day_3`에 대한
실물 성공 판정은 `PENDING_HUMAN`으로 남긴다.

사람이 새 코드로 paid re-run을 완료한 뒤 `scripts/speech_onsets.py`로 다시 측정한다.
현재 baseline은 다음과 같다.

```text
자막은 있는데 말이 없는 phantom cue: 30개
말이 있는데 자막이 없는 시간: 270초
```

확인할 것은 다음이다.

- 기존의 승인 없는 `replaced`가 0인지
- 알려진 음악 구간 `04:36–06:13`의 phantom cue가 제거됐는지
- 모든 `replaced` candidate의 edge hole이 새 한도 안인지
- KEEP/collapse fallback 때문에 실제 발화 누락 270초가 악화되지 않았는지
- trace의 arbiter 상태와 호출 로그가 일치하는지

실제 paid re-run 전에는 이 항목을 PASS로 보고하지 마라.

## Report

완료 보고에는 다음을 포함한다.

- base `30660b6`와 최종 SHA
- `git diff --stat 30660b6..HEAD`
- 수정된 파일 목록
- 세 red-first 테스트 각각의 실패 전·성공 후 결과
- arbiter 상태 전이표와 직렬화 호환 방식
- `repair_arbiter_similarity_floor = 0.60`의 근거
- `repair_coverage_edge_seconds = 2.0`의 근거
- unanswered/disagreed candidate가 KEEP으로 간 실제 테스트 출력
- large leading hole candidate가 KEEP으로 간 실제 테스트 출력
- focused test 수와 전체 `make check` test 수
- 직접 센 `[OK]` 줄의 실제 개수
- 기존 trace 9.0 load/redo 호환 증거
- 네트워크·유료 호출 `0`
- 실물 재실행은 `PENDING_HUMAN`
- `.env`와 사용자 소유 `.env.example`을 건드리지 않았다는 확인
