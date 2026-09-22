# 재전사 채택 경로 진단 — 심판은 실패한 게 아니라 호출되지 않았다

- 일시: 2026-09-13
- 조사자: `codex --model gpt-5.6-sol` (xhigh, `--sandbox read-only`)
- 대상: 실제 62분 실행 산출물 `out/fr_day_3` (1,153큐 · 재전사 14건)
- 저장소는 수정되지 않았다.

## 한 줄

**`arbiter_answered`가 채택 조건에 들어 있지 않다.** 첫 rescue 응답이 비어 있지 않고
자체 반복 검사만 통과하면 심판 분기 **전에** 채택하고 `break` 한다. 14건 중 13건이
이 지름길로 갔다.

## 함께 확인된 것

- 채택이 보는 것은 셋뿐이다: segments 비지 않음, 반복 초 ≤ `repair_residual_seconds`,
  반복 비율 ≤ `loop_repetitive_share`. 1차 전사와의 관계·독립 전사와의 일치·
  `avg_logprob`·`no_speech_prob`·시간 커버리지는 **아무도 안 본다**.
- 그래서 `"한 번 더"`×5 → `"구독해주세요"`, `"치즈가 왔다!"`×7(8장) → `"감사합니다"`가
  첫 시도에 합격한다.
- repair[12]의 계획 구간은 `2918.32–3032.32`인데 첫 교체 세그먼트가 `2978.34`에서
  시작한다. 앞 60초가 안 덮였는데 `replaced`다 — **59.6초 공백의 정체**.
- `tests/test_repair.py:168`이 현재의 잘못된 동작을 **의도적으로 고정**하고 있다.
- `arbiter_*` 설정은 하나도 없다. 심판은 `verify_*`를 재사용한다.

## Opus의 주장 하나가 반증됐다

Opus는 repair `[3]`·`[6]`이 "자막 0장"이라 규칙 9가 깨졌다고 했다. **틀렸다.**
두 큐 모두 SRT에 있다(`fr_day_3.srt:3020`, `:3194`). Opus의 집계가 "repair 구간에
**완전히 포함된** 큐"만 셌고, `lead_in`/`lead_out`(`refine.py:188`)과
`merge_pass`(`refine.py:142`)가 큐를 구간 밖으로 밀어낸 것뿐이다. 커버리지 감사는
포함이 아니라 **겹침**으로 해야 한다.

## 독립 증거 — VAD (`scripts/speech_onsets.py`)

| 구간 | VAD 평균 발화 확률 | 판정 |
|---|---|---|
| 10:00 · 30:00 (대조군) | 0.846 · 0.805 | 발화 |
| 37:18~38:33 (collapse된 75초) | **0.730** | **진짜 대화였다** |
| 04:36~06:13 (repair[0]) | **0.005** (진폭 15437) | 음악. 자막 전체가 지어낸 것 |

전체: 말이 있는데 자막이 없는 구간 **270초·92곳**, 자막은 있는데 말이 없는 큐 **30개**.

---

## 진단

### 1. `arbiter_answered=False`의 실제 원인

13건은 arbiter 호출 실패가 아니라 **호출 자체가 없었습니다.**

실제 경로:

1. 첫 rescue 응답을 받음: [vlogsrt/repair.py:351](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:351)
2. `segments`가 있고 `_recovered(...)`가 참이면 즉시 `found.segments`에 저장: [vlogsrt/repair.py:357](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:357)
3. `failed`가 비어 있으므로 arbiter 분기 전에 `break`: [vlogsrt/repair.py:364](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:364)
4. `arbiter_answered`는 `index in rescued.arbiter`로 기록되므로 `False`: [vlogsrt/repair.py:539](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:539)

trace상 13건이 `replaced + attempts=1 + arbiter_answered=false`인 것은 이 경로를 확정한다. 유일한 예외인 repair index 7은 `collapsed + arbiter_answered=true + arbiter_clean=false`다: [trace.json:28863](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:28863).

구분하면:

- **호출 안 함 — 실제 13건:** 위 성공 단축 분기.
- **호출 실패:** `_ask_arbiter`가 예외를 잡아 `{}` 반환: [vlogsrt/repair.py:317](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:317). 이번 13건 경로가 아니다.
- **응답 parse 실패:** `LiveTranscriber._to_dict` 실패는 호출 예외로 잡힌다. 반면 `parse_text`는 `try` 밖이라 구조적 예외면 실행이 중단된다; `False` trace를 만들지 않는다: [vlogsrt/repair.py:322](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:322), [vlogsrt/transcribe.py:385](/Users/ssojux2/Working/vlogsrt/vlogsrt/transcribe.py:385).
- **정상적으로 빈 답/반복 답:** map에는 항목이 생기므로 `arbiter_answered=true`, `arbiter_clean=false`다: [vlogsrt/repair.py:329](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:329). index 7이 이것이며, raw 답이 저장되지 않아 빈 답인지 반복 답인지는 확정 불가.
- **응답 offset 불일치:** `_by_span`이 버리면 호출됐어도 unanswered가 될 수 있다: [vlogsrt/repair.py:424](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:424). 이번 trace 증거는 없다.
- **offline/fixture:** missing arbiter fixture라면 `FixtureTranscriber`가 예외를 내고 위 호출 실패 분기로 간다: [vlogsrt/transcribe.py:119](/Users/ssojux2/Working/vlogsrt/vlogsrt/transcribe.py:119). 13건은 호출조차 안 했으므로 fixture 부재와 무관하다.

검증 한계: trace는 실행 모드를 저장하지 않는다. 실제 key가 있어도 그것만으로 `LiveTranscriber`가 선택되지는 않는다: [vlogsrt/transcribe.py:395](/Users/ssojux2/Working/vlogsrt/vlogsrt/transcribe.py:395). 다만 fixture가 실행 당시에도 없었다면, index 7의 `arbiter_answered=true`는 ordinary pipeline에서 live 호출이었다는 강한 증거다.

### 2. Arbiter가 답하지 않아도 replacement가 채택되는가

**현재 13건은 그렇다.** `arbiter_answered`가 adoption 조건에 전혀 들어가지 않는다.

`rescued.segments[index]`만 있으면 `_apply`가 owned first-pass segments를 모두 걷고 rescue를 넣는다: [vlogsrt/repair.py:487](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:487). 이것이 닫아야 할 fail-open이다.

단, arbiter를 실제 호출한 뒤 호출이 실패한 경우는 다르다. 그때는 candidate가 이미 recovery 실패 상태이므로 replacement하지 않고:

- `< repair_collapse_min_seconds`: `REPAIR_KEPT`
- 그 이상: `REPAIR_COLLAPSED`

로 간다: [vlogsrt/repair.py:497](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:497).

즉 문제는 “arbiter 실패 후 채택”이 아니라 **“첫 rescue가 자체 반복 검사만 통과하면 arbiter 승인 없이 채택”**이다. 기존 테스트도 이를 의도적으로 고정하고 있다: [tests/test_repair.py:168](/Users/ssojux2/Working/vlogsrt/tests/test_repair.py:168).

### 3. `[3]`, `[6]`의 ZERO cues

실제 소실이 아니다. “repair span 안에 완전히 포함된 cue”만 센 결과가 0이다.

| repair | trace 결과 | 최종 결과 |
|---|---|---|
| `[3]` 34:27 | `replaced`, `before=3`, `after=1` | spine `2067.768–2070.608 "비 온다"`; cue #715 `2067.668–2070.908` |
| `[6]` 37:12 | `replaced`, `before=4`, `after=2` | spine `"하하하하"`, `"아우 창피해"`; cue #757·758이 양쪽 이웃과 병합 |

trace: [trace.json:28669](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:28669), [trace.json:28751](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:28751). 실제 SRT에도 존재한다: [fr_day_3.srt:3020](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/fr_day_3.srt:3020), [fr_day_3.srt:3194](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/fr_day_3.srt:3194).

원인:

- `[3]`: `lead_in/lead_out`이 cue를 span 밖으로 각각 넓힘: [vlogsrt/refine.py:188](/Users/ssojux2/Working/vlogsrt/vlogsrt/refine.py:188).
- `[6]`: `merge_pass`가 앞·뒤 segment와 합침: [vlogsrt/refine.py:142](/Users/ssojux2/Working/vlogsrt/vlogsrt/refine.py:142).
- `drop_empty`는 빈 문자열만 제거하며 이 두 cue는 비어 있지 않다: [vlogsrt/refine.py:255](/Users/ssojux2/Working/vlogsrt/vlogsrt/refine.py:255).

따라서 collapse도, near-empty 삭제도, `drop_empty`도 아니다. Coverage 감사 시 조건은 완전 포함이 아니라 interval overlap이어야 한다.

별개로 59.6초 gap은 진짜 acceptance 결함이다. repair index 12 span은 `2918.32–3032.32`인데 첫 replacement segment가 `2978.34`에서 시작한다. 그런데도 `replaced`다. Candidate의 시간 범위/coverage를 검사하지 않기 때문이다.

### 4. Replacement와 원래 audio의 관련성 검사

없다. 확인하는 것은 다음 두 가지뿐이다:

```python
segments가 비어 있지 않음
and repetitive_seconds <= repair_residual_seconds
and repetitive_share <= loop_repetitive_share
```

[vlogsrt/repair.py:357](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:357), [vlogsrt/repair.py:386](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:386).

검사하지 않는 것:

- first-pass text와의 관계
- 독립 arbiter transcription과 candidate의 일치
- `arbiter_answered`/`arbiter_clean`
- 발화 의미·언어
- `avg_logprob`, `no_speech_prob`
- before/after 감소 폭
- candidate가 repair span의 발화를 덮는지
- 개별 segment 시간이 plan span 안인지

`_by_span`은 response의 chunk offset만 맞춘다. 텍스트나 개별 segment 시간을 검증하지 않는다. 따라서 `"구독해주세요"`처럼 비반복인 한 줄은 첫 시도에 그대로 합격한다. `attempts=1`은 정확히 이 결과다.

또한 현재 arbiter는 후보를 심사하지 않는다. `ARBITER_KIND`로 같은 audio를 `verify_model`에 전사하고, 결과가 비어 있지 않고 자체 반복이 아닌지만 본다. 후보와 비교하지 않는다: [vlogsrt/transcribe.py:320](/Users/ssojux2/Working/vlogsrt/vlogsrt/transcribe.py:320).

### 5. 관련 설정

`vlogsrt.toml:74–85`는 모두 주석 예시라 active override가 없다. 유효값은 `Config` 기본값이며 trace snapshot도 일치한다: [vlogsrt/config.py:41](/Users/ssojux2/Working/vlogsrt/vlogsrt/config.py:41), [trace.json:28377](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:28377).

- `repair_enabled=true`
- `repair_temperature=0.8`
- `repair_temperature_step=0.2`
- `repair_max_attempts=2`
- `repair_margin=2.0`
- `repair_min_seconds=3.0`
- `repair_chunk_seconds=120.0`
- `repair_residual_seconds=10.0`
- `repair_collapse_min_seconds=30.0`

`arbiter_*` 설정은 **하나도 없다**. Arbiter는 다음을 재사용한다:

- `verify_model="gpt-transcribe"`, `verify_format="json"`
- `language="ko"`, `transcribe_temperature=0.6`
- `prompt=""`, `keywords=[]`
- `api_max_retries=4`, `api_backoff_base=2.0`

Acceptance를 직접 바꾸는 값은 `repair_residual_seconds`와 `loop_repetitive_share`다. `loop_min_repeats`, `loop_min_seconds`, `loop_min_chars`, `loop_compression_ratio`, `loop_ratio_window_words`는 residual 계산을 통해 간접 영향한다. 나머지 `repair_*`는 진입, 입력 구간, 생성 결과, 재시도 횟수 또는 실패 fallback을 바꾸지만 첫 clean candidate의 승인 조건은 아니다. 임계값 변경은 권고하지 않는다.

## 최소 수정 우선순위

1. **모든 replacement에 answered arbiter의 candidate 일치 승인을 필수화한다.**

   대상: `vlogsrt/repair.py:301–370`. `_ask_arbiter`가 bool 대신 독립 transcription/상태를 반환하고, candidate가 audio transcription과 일치할 때만 `found.segments`에 넣는다. unanswered 또는 불일치는 rejected로 처리한다. 시간은 계속 rescue Whisper segment에서만 가져온다.

   먼저 쓸 실패 테스트:  
   `test_clean_rescue_is_not_adopted_when_arbiter_is_unanswered_or_disagrees`  
   짧은 `"한 번 더"` 반복에 rescue `"구독해주세요"`, arbiter failure/`"한 번 더"`를 주고 `REPAIR_KEPT` 및 first-pass 보존을 요구한다. 현재는 arbiter를 호출하지 않고 `REPAIR_REPLACED`라 실패한다. 기존 [tests/test_repair.py:168](/Users/ssojux2/Working/vlogsrt/tests/test_repair.py:168)의 반대 기대도 제거해야 한다.

2. **Candidate 시간 coverage를 recovery 불변조건으로 추가한다.**

   대상: `vlogsrt/repair.py:357–395`. 큰 leading/trailing hole 또는 plan 밖 segment가 있으면 전체 candidate를 불합격 처리한다. 시간을 보정·생성하지 않고 기존 KEEP/collapse 경로로 보낸다. `mapping`/`gapfill`은 필요 없다.

   먼저 쓸 실패 테스트:  
   `test_nonrepetitive_rescue_with_a_large_uncovered_leading_span_is_not_recovered`  
   8초 short repeat에 마지막 2초짜리 clean segment만 반환하고 first-pass가 유지됨을 요구한다. 현재는 반복이 없다는 이유만으로 replacement한다.

3. **`arbiter_answered` 두 상태를 명시적 상태로 분리한다.**

   대상: `vlogsrt/repair.py:99–106`, `vlogsrt/repair.py:301–326`, `vlogsrt/models.py:184–226`. 최소한 `not_requested`, `call_failed`, `unmatched`, `answered_empty`, `answered_repetitive`, `approved`를 trace에 남긴다.

   먼저 쓸 실패 테스트:  
   `test_trace_distinguishes_arbiter_not_requested_from_call_failed`  
   현재는 두 경우 모두 `arbiter_answered=false`라 실패한다.

저장소 수정, `.env` 열람, API 호출은 하지 않았다.
