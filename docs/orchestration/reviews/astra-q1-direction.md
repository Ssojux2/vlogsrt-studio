# Q1 브리프 방향 검토 (gpt-6-astra, read-only)

- 일시: 2026-09-13 · 대상 `docs/orchestration/tasks/q1-repair-arbiter-approval.md` (HEAD `d025125`)
- **판정: UNSOUND** — P0 2건 · P1 3건. 그대로 디스패치 금지.
- Fable 5.1이 세션 한도에 걸려(오후 2시 리셋) Astra가 대신 봤다. 브리프를 쓴 Sol과
  다른 모델이므로 검토의 독립성은 유지된다.

## Opus의 근거 하나가 반증됐다

Opus는 문턱 0.60을 "환각 0.00~0.19 / 정상 0.78~1.00"으로 방어했다. **시험이 너무
쉬웠다** — 환각을 *무관한* 문장과 비교했다. 심판이 *그럴듯하게 빗나간* 출력을 낼 때가
진짜 경계다:

| candidate / 심판 | ratio | 결과 |
|---|---|---|
| `이만 삼천 원이에요` / `23,000원이에요` | 0.471 | 참 복구 거절 |
| `비 온다` / `비가 오네요` | 0.250 | 참 복구 거절 |
| `구독해주세요` / `구경해주세요` | 0.833 | 환각 승인 |
| `구독해주세요` / `그거 해주세요` | 0.667 | 환각 승인 |

그리고 **두 모델이 같은 환각을 내면 1.0도 통과한다.** 문턱 조정으로 풀리지 않는다.

---

**UNSOUND. 현 브리프는 dispatch하면 안 됩니다.** 승인 강화가 삭제 강화로 연결되며, ownership 안에서 전체 테스트를 통과하라는 계약도 모순입니다.

현재 HEAD는 `9313a84`가 아니라 `d025125`입니다. 지정한 검토 파일 중 두 커밋의 차이는 브리프와 `TASKS.md`뿐이었습니다. 저장소 수정·API 호출 없이 검토했습니다.

1. **P0 — “거절 → 기존 collapse”를 바꾸십시오. 우려한 trade는 실제입니다.**

   [브리프:82](/Users/ssojux2/Working/vlogsrt/docs/orchestration/tasks/q1-repair-arbiter-approval.md:82)의 “침묵을 만들지 마라”는 실제 폴백과 모순입니다. 기준은 **plan 길이가 아니라 `plan.repeat.duration`**이며, 30초 이상이면 첫 segment만 남깁니다. scratch에서 29초→29장 KEEP, 30초→1장·29초 삭제, 40초→1장·39초 삭제를 재현했습니다. [repair.py:497](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:497), [repetition.py:110](/Users/ssojux2/Working/vlogsrt/vlogsrt/repetition.py:110)

   실제 trace의 현재 `replaced` 중 추가 collapse에 노출되는 것은 `[0]` 93.24초, `[9]` 46.79초, `[12]` 112.00초, **합계 252.03초**입니다. 저장 PCM의 VAD 재추론에서 이 범위의 발화 구간은 **146.12초**였습니다. 이는 노출량이며 추가 누락 예상치가 아닙니다. 새 arbiter 답과 재시도 결과는 없습니다. `[12]`의 현재 candidate는 leading hole만으로도 거절됩니다. [trace:28495](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:28495), [trace:28902](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:28902), [trace:29059](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:29059)

   **최소 수정:** 호출 실패·미매칭·빈 답·불일치·coverage 실패를 collapse 권한으로 쓰지 말고 길이와 무관하게 KEEP+WARN으로 보내십시오. 현재 형태의 자동 collapse는 중단하는 편이 맞습니다. 조건부 존치하려면 별도의 반복 근거와 실패 상태를 요구해야 하며, 길이·두 모델의 동의만으로 실제 발화 부재를 증명할 수는 없습니다.

   단, **KEEP가 모든 품질 지표에서 strictly better는 아닙니다.** 음악 구간 `[0]`은 현재 12장 대신 1차 전사 103장을 되돌릴 수 있고, D27의 대량 반복도 다시 노출됩니다. 보존에는 유리하지만 정확한 대화를 복원하지는 않습니다. collapse 제거 시 관련 테스트·경고 문구·D27/D28 정책 기록도 함께 바꿔야 합니다. [trace:28593](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:28593), [DECISIONS.md:1373](/Users/ssojux2/Working/vlogsrt/docs/DECISIONS.md:1373)

2. **P0 — ownership과 상태 전달 계약을 수정하십시오.**

   허용 파일은 다섯 개인데, ownership 밖 테스트가 `arbiter_payload=None`으로 `REPAIR_REPLACED`와 **arbiter 호출 없음**을 요구합니다. mandatory arbiter와 동시에 만족할 수 없습니다. bool 호환만으로 해결되지 않습니다. 해당 project 테스트와 영향받는 pipeline 테스트·합성 입력을 ownership에 포함하십시오. [브리프:17](/Users/ssojux2/Working/vlogsrt/docs/orchestration/tasks/q1-repair-arbiter-approval.md:17), [test_speech.py:893](/Users/ssojux2/Working/vlogsrt/tests/project/test_speech.py:893), [test_boundary_transcribe.py:779](/Users/ssojux2/Working/vlogsrt/tests/project/test_boundary_transcribe.py:779)

   `_ask_arbiter`는 **`repair.py`에 있고 직접 caller는 `_rescue` 하나**입니다. 반환형 변경 자체는 가능합니다. 다만 현재 인자에는 candidate가 없어 이 함수에서 `approved/disagreed`를 판정할 수 없습니다. **독립 응답의 유효성·원문/정규화문**과 **attempt별 candidate 승인**을 분리하고, `_rescue → _assemble → _apply`까지 전달하도록 명시하십시오. 유효한 arbiter가 불일치 candidate의 다음 재시도를 허용하는 상태도 필요합니다. `arbiter_clean = approved`로 바꾸면 기존 의미까지 깨집니다. [repair.py:301](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:301), [repair.py:369](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:369), [repair.py:530](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:530)

3. **P1 — `0.60`을 검증된 승인 경계로 확정하지 마십시오.**

   근거가 다른 척도입니다. `verify_cue_match_floor`는 **일치 글자 수 / cue 글자 수**이고, 브리프는 **`2M/(후보 길이+심판 길이)`**입니다. verify는 4자 미만도 제외합니다. D28의 “0.80이 엄격하다”는 기록은 0.60의 타당성을 입증하지 않습니다. [verify.py:151](/Users/ssojux2/Working/vlogsrt/vlogsrt/verify.py:151), [DECISIONS.md:1398](/Users/ssojux2/Working/vlogsrt/docs/DECISIONS.md:1398)

   실제 `strip_for_compare`와 `autojunk=False`로 계산한 **구성 반례**입니다.

   | candidate / arbiter | ratio | 문제 |
   |---|---:|---|
   | `이만 삼천 원이에요` / `23,000원이에요` | 0.471 | 같은 발화의 숫자 표기만 달라도 참 rescue 거절 |
   | `비 온다` / `비가 오네요` | 0.250 | fallible arbiter의 표현 변화로 거절 |
   | `구독해주세요` / `구경해주세요` | 0.833 | 실제 “구경해주세요” 위의 환각 승인 |
   | `구독해주세요` / `그거 해주세요` | 0.667 | 공통 어미만으로 환각 승인 |

   **최소 수정:** 0.60은 미보정 가설이라고 명시하고 위 양방향 오류를 acceptance에 넣으십시오. 단순 상향·하향으로 해결되지 않습니다. 승인 실패가 삭제로 이어지지 않는 것이 선행 조건입니다. 두 모델이 같은 환각을 내면 1.0도 통과합니다.

4. **P1 — coverage를 다시 정의하십시오. `2.0`의 근거도 부족합니다.**

   `repair_margin`은 이웃 segment를 **owned에 포함하는 데** 쓰이며, `plan.span`은 이미 그 전체 시작·끝입니다. 따라서 context 때문에 무조건 `plan.span ± 2초`를 허용해야 한다는 해석은 틀립니다. 반대로 context 폭이 곧 발화 손실 허용량이라는 근거도 없습니다. [repair.py:159](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:159), [repair.py:239](/Users/ssojux2/Working/vlogsrt/vlogsrt/repair.py:239)

   엄격한 containment는 실제 절단·부동소수점 차이를 거절합니다. `[5]`는 끝이 plan보다 **28ms** 늦고, `[8]`은 시작이 **0.000000305초** 빠릅니다. 전자는 ffmpeg 프레임 경계 차이 규모입니다. [trace:7848](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:7848), [trace:8270](/Users/ssojux2/Working/vlogsrt/out/fr_day_3/trace.json:8270), [preprocess.py:435](/Users/ssojux2/Working/vlogsrt/vlogsrt/preprocess.py:435)

   더 큰 문제는 **내부 구멍을 전혀 검사하지 않는 것**입니다. plan `[0,120]`, candidate `[0,1]`·`[119,120]`은 글자 일치 1.0·반복 없음으로 모든 조건을 통과하면서 **118초를 비웁니다**.

   **최소 수정:** owned first-pass와 candidate의 interval union 차이로 내부·양끝 손실을 함께 검사하고, 숫자 오차와 실제 chunk 경계 차이를 별도로 다루십시오. 시간을 늘리거나 clamp하지 말고 불확실하면 KEEP하십시오. 이것도 발화 존재의 증명은 아닙니다. 필요한 값은 repair가 이미 보유합니다.

5. **P1 — 성공 기준과 보존 범위를 바로잡으십시오.**

   `speech_onsets.py`의 **270초는 정확한 누락 초수가 아닙니다.** cue가 절반 미만 덮는 발화 span의 **전체 길이 합**입니다. 재추론으로 `269.952초·92구간·30 phantom 후보`를 재현했지만, 같은 VAD span에서 실제 미덮인 시간을 합하면 **458.092초**였습니다. 30개 역시 텍스트 정답 판정이 아닌 발화 overlap 부족 후보입니다. [speech_onsets.py:199](/Users/ssojux2/Working/vlogsrt/scripts/speech_onsets.py:199)

   기존 지표는 유지하되 정확한 미커버 시간과 구간별 회귀도 gate에 추가하십시오. “downstream byte-equivalent”는 **`settle` 경계의 원본 글자·시각 보존**으로 좁혀야 합니다. 이후 조판의 병합·시각 이동까지 repair가 보장할 수 없습니다. [브리프:318](/Users/ssojux2/Working/vlogsrt/docs/orchestration/tasks/q1-repair-arbiter-approval.md:318), [진단:98](/Users/ssojux2/Working/vlogsrt/docs/orchestration/reviews/sol-repair-arbiter-diagnosis.md:98)

   base도 dispatch SHA로 갱신하십시오. 현재 `30660b6..HEAD`에는 이미 문서 세 파일이 포함되어 “다섯 파일만 diff” 조건과 충돌합니다. [브리프:429](/Users/ssojux2/Working/vlogsrt/docs/orchestration/tasks/q1-repair-arbiter-approval.md:429)

규칙 판정: **1·5의 직접 위반은 없습니다.** arbiter는 글자 비교만 하고 출력은 rescue Whisper segment에서 오면 됩니다. **9는 형식상 기존 긴 반복 예외를 따르지만, 새로운 불확실성을 삭제 권한으로 바꾸는 방향이 문제**입니다. D35는 레거시에 VAD 삭제 권한을 주지 않습니다. 위 수정에는 `project` import나 numpy/onnxruntime 도입이 필요 없습니다. [CLAUDE.md:37](/Users/ssojux2/Working/vlogsrt/CLAUDE.md:37), [CLAUDE.md:62](/Users/ssojux2/Working/vlogsrt/CLAUDE.md:62), [CLAUDE.md:84](/Users/ssojux2/Working/vlogsrt/CLAUDE.md:84)

실제 음성의 정답 전사, 새 arbiter의 오판율, 변경 후 유료 실행 결과는 확인하지 못했습니다. VAD `0.730`은 재현했지만, 그것만으로 모든 단어의 실제 발화를 확정하지는 않았습니다.
