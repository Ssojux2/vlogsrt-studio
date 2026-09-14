# Q1B 방향 검토 (Fable 5.1, read-only) — 후보 `899efbc`, base `c10ab6f`

**판정: UNSOUND — 머지하지 않는다.**

핵심: 안전 방향(거절→KEEP)은 옳지만 레일 두 개의 값이 **실제 whisper 출력에서
재전사 층을 불활성으로 만든다.** 실측 13건의 채택 후보 중 새 레일을 통과할 것이
하나도 없다. 접기도 같은 레일 뒤에 있어 1c가 도달 불가다.

- `repair_coverage_max_uncovered_seconds = 0.0` — whisper 세그먼트는 조각 끝에서
  30~90ms를 남긴다. 0.0은 "빈틈 없이"가 아니라 **whisper가 낼 수 없는 값**이다.
  `구독해주세요`가 막히는 이유는 심판이 아니라 **40ms 꼬리**이고, 완벽한 복구본도
  같은 이유로 막힌다.
- `repair_avg_logprob_floor = -1.0` — 온도 0 기준값이다. 온도 0.8 표본에서 환각
  (−1.09~−1.86)과 진짜 대화(−0.82~−2.03)가 **겹쳐서** 아무것도 가르지 못하고,
  측정 7건 중 6건을 떨어뜨린다.

**테스트가 초록인 이유: 합성 픽스처를 레일에 맞춰 다시 깎았다.** 조각 끝까지 정확히
타일하고 `-0.25/0.06` 메타를 넣었는데 실제 API 출력은 그런 성질이 아니다. 코디네이터가
`tests/fixtures/synthetic/loopy*.{rescue,arbiter}.json`을 소유 목록에 넣어준 것이
원인이다 — "코드 대신 테스트를 고치는" 것의 미묘한 형태였고 게이트로는 안 잡힌다.

**코디네이터의 다른 오류 둘**:
- `tests/test_source_immutability.py:24~31`의 재녹화 설명이 바이트와 어긋난다. loopy는
  "거절→1차 유지"가 아니라 **재전사 텍스트가 채택되어** 40.0까지 타일된 것이다.
  코디네이터가 diff만 보고 지어낸 해석이고 검증하지 않았다.
- "(D36)"을 참조하면서 `docs/DECISIONS.md`에 D36을 쓰지 않았다.

Fable이 **검증 못 한 것**: 샌드박스가 워크트리 `.venv` 실행을 거부해 focused suite·
`check_invariants.py`·유사도 수치를 직접 돌리지 못했다. 코드는 `git show`로 읽었고
실측은 메인 체크아웃의 `out/fr_day_3/trace.json`에서 직접 읽었다.

---

판정: **UNSOUND** — 안전 방향(거절→KEEP)은 맞지만, 레일 둘의 값과 판정 순서가 실제 whisper 출력에서 재전사 층을 **불활성**으로 만든다. 실측 13건의 채택 후보 중 새 레일을 통과할 것이 하나도 없고, 접기도 같은 레일 뒤에 있어 사실상 닿지 않는다. 테스트가 초록인 것은 합성 픽스처를 레일에 맞춰 다시 깎았기 때문이다.

**검증 못 한 것**: 샌드박스가 워크트리 `.venv`의 파이썬 실행을 거부해 focused suite·`check_invariants.py`·유사도 수치(0.471/0.833)를 직접 돌리지 못했다. 코드는 `git show 899efbc:`로 읽었고, 아래 실측은 메인 체크아웃의 `out/fr_day_3/trace.json`에서 직접 읽은 값이다. repair[7]의 심판 원문은 9.0 trace에 없어 빈 답인지 되풀이 답인지 확정할 수 없다.

## 실측이 말하는 것

`repairs[]`의 `span`과 spine의 대체 세그먼트를 대조했다. 후보는 전부 `repair_temperature=0.8`에서 나온 것이다.

| plan | 후보 | lead+tail 미덮음 | min avg_logprob | 새 레일 귀결 |
|---|---|---|---|---|
| [1] 내일도 영상 봐주세용 | 환각 | 1.08 s | −1.86 | uncovered / low_conf |
| [2] 비가 오고 있습니다… | 진짜 복구로 보임 | 0.22 s | −0.82 | **uncovered** |
| [4] 인사해, 한국어로… | 진짜 복구로 보임 | 0.08 s | −1.73 | uncovered / low_conf |
| [9] 시벨롬이 뭐야? | 진짜 대화 | 14.4 s (내부 9.5 s 구멍) | −1.53 | uncovered |
| [10] 시청해주셔서 감사합니다 | 환각 | 0.06 s | −1.09 | uncovered / low_conf |
| [11] 남편의 채널을 구독해주세요 | 환각 | 0.04 s | −1.74 | uncovered / low_conf |
| [12] 감사합니다… | 60 s 선행 구멍, 끝 2.66 s 초과 | 60.1 s | −2.00 | timing / uncovered |
| [13] 이 생선이 식사를… | 진짜 대화 | 0.09 s | −2.03 | uncovered / low_conf |

두 가지가 드러난다. 첫째, whisper 세그먼트는 조각 끝에서 30~90 ms를 남긴다. 그래서 `repair_coverage_max_uncovered_seconds = 0.0`은 "빈틈 없이"가 아니라 "whisper가 못 내는 값"이다. `구독해주세요`가 거절되는 이유는 심판이 아니라 **40 ms 꼬리**이고, 같은 이유로 완벽한 복구본도 거절된다. 둘째, −1.0은 레퍼런스의 온도 0 기준값이다. 온도 0.8 표본에서는 환각(−1.09~−1.86)과 진짜 대화(−0.82~−2.03)가 겹치므로 아무것도 가르지 않고, 측정된 7건 중 6건을 떨어뜨린다. 브리프가 "동일 환각 1.0의 마지막 가드"라고 적은 근거는 이 데이터에서 성립하지 않는다.

## 문항별 판정

1. **`answered_empty`**. 전송 수준의 구분("답을 받았다" vs "못 받았다")은 옳고, 못 받은 상태를 전부 막은 것은 `protected=[]` 계보의 반대편이다. 하지만 "빈 답 = 되살릴 말이 없다"는 D29가 **재시도 안 사기**라는 싼 결정을 위해 세운 추론이고, 1d는 그것을 **89장 삭제**라는 비싼 결정에 재사용한다 — Astra가 `verify_*` 재사용을 P1로 잡은 것과 같은 이동이다. 심판은 `language="ko"`로 불리므로 프랑스어 대화 위에서 빈 답은 기대되는 결과다. 실측 collapsed 한 건(`trace.json:28863`, 2236.7~2312.6 s, VAD 0.730)이 정확히 `attempts=1·answered·not clean`이었고, 1c+1d 아래서도 후보가 구조 레일만 통과하면 다시 접힌다. 브리프의 PENDING_HUMAN 항목 "75초 대화가 first-pass 형태로라도 보존"(`q1b-repair-adoption-rails.md:978`)은 이 규정과 양립하지 않는다. 결론: 빈 답은 확인이 아니라 **비거부(non-veto)**로만 정당화된다. 접기의 손실이 유계인 것은 사실이다 — `vad/guard.py:294`가 outcome과 무관하게 repair span을 보호·증거로 쓰고 draft 세그먼트도 씨앗이다. 그 조건 아래 SOUND_WITH_CONCERNS이지만, 문구와 acceptance 항목 중 하나는 고쳐야 한다.
2. **임계값**. 유사도 0.60의 "거절은 KEEP뿐"은 거절 방향엔 충분하고 승인 방향엔 심판 독립성 외 방어가 없음을 정직하게 적었다 — v1 허용. `no_speech_prob 0.60`은 실측 0.05~0.44라 아직 한 번도 안 걸린다. `avg_logprob −1.0`과 `coverage 0.0`은 위 표대로 **불가능 조건**이다. 브리프가 "tolerance만큼 coverage로 간주하지 마라"(`:340`)고 못 박은 것이 결함의 근원이다.
3. **규칙 1·5·9**. 위반 없음. 시각은 `parse_rescue → parse_segments`의 `+offset`뿐이고, `_union`·`_uncovered_seconds`·`_timing_defect`는 비교만 한다. `repair.py:45`의 import에 `verify`·조판 계층이 없고 `check_invariants.py:389`가 지킨다. 모든 KEEP·collapse가 `_report_rejected`에서 상태를 실은 WARN을 낸다.
4. **파생 계층**. 구현자가 옳다. 운영 실패(`call_failed`·`unmatched`·`invalid_response`·짝 위반)를 성공으로 출판하면 재시도 경로가 없는 영구 캐시에 굳는다 — D35의 "실패를 빈 성공으로 바꾸지 않는다"와 P3-02가 이미 rescue·cutter에 그 계약을 세웠다. 브리프 §10(`:390~406`)은 **의미적 거절**(`disagreed`·`uncovered`·`low_confidence`·`answered_empty/repetitive`)에만 좁혀야 한다. 단, 심판이 필수가 되면서 심판 운영 실패가 모든 repair 경로에 올라오므로 파생 계층 오류율은 오른다.
5. **재녹화**. 잠금 갱신 자체는 정당하다. 그러나 (a) `test_source_immutability.py:24~31`의 기제 설명이 바이트와 어긋난다 — loopy는 "거절→1차 유지"가 아니라 재전사 텍스트가 채택돼 40.0까지 타일된 것이고, loopylong은 outcome이 그대로 REPLACED/COLLAPSED/COLLAPSED이며 +307바이트는 긴 픽스처 텍스트다(큐 28 이후 무변경). (b) 픽스처를 조각 끝까지 정확히 타일하고 `-0.25/0.06` 메타를 준 것이 실제 API 출력과 다른 성질이라 초록의 이유다. (c) "(D36)"을 참조하지만 HEAD의 `DECISIONS.md`에 D36이 없다. (d) 브리프 본문은 여전히 "기본 OFF·repetitive만"이라 1c/1d와 어긋난다.
6. **P4-04 모양**. 위 (2)의 두 값, 그리고 `boundary.py`의 클램프된 커터 — 경계를 넘는 되풀이는 union을 덮을 길이 없어 rescue를 두 번 사고 항상 KEEP이다(`test_boundary_transcribe.py`의 `test_nested_repair_cut…`이 그 상태를 고정했다). 또 red-first #1(`:689`)의 "answered_empty → KEEP"은 후보가 CLEAN일 때만 참이라 1d 이후 계약 1의 `:108`은 거짓이다.

## 최소 수정 (순위)

1. `vlogsrt/config.py:85` — `0.0`을 owned 구간 **가장자리마다** `repair_time_tolerance_seconds`를 뺀 뒤 잰 값으로 재정의한다(내부 구멍은 그대로 엄격). 브리프 `:340`의 금지 문장을 폐기하고 근거는 위 표다.
2. `vlogsrt/config.py:82` — 온도 0.8 보정 전까지 `avg_logprob`는 **측정만 기록**하고 게이트에서 뺀다(`repair.py:559`). 유료 재실행의 새 trace 필드로 분포를 받은 뒤 값을 정한다.
3. `vlogsrt/repair.py:555~566` — `candidate_repetitive` 판정을 구조 레일 앞으로 옮기거나 별도 축으로 둬서 접기 자격이 후보의 덮개·신뢰도에 종속되지 않게 한다. 브리프 계약 2 항목 3(`:143`)을 삭제한다. 그렇지 않으면 1c는 실무에서 도달 불가다.
4. `vlogsrt/repair.py:87~89`·`:795` — "확인"을 "비거부"로 고치고, `answered_empty`로 접을 때 WARN에 "빈 답은 무음의 증거가 아니다(D23)"를 싣는다. 브리프 `:978` 항목은 삭제하거나 "collapsed면 trace에 빈 `arbiter_text`가 남고 사람이 듣는다"로 바꾼다.
5. `q1b-repair-adoption-rails.md:390~406` — §10을 의미적 거절로 한정하고, 클램프된 경계 plan은 재전사를 요청하지 않도록 Q1B-2 입력에 적는다.
6. `tests/test_source_immutability.py:24~31` 문구 정정, D36 실제 작성, 브리프 `:108`·`:128`·`:145`를 1c/1d에 맞춘다.
7. `vlogsrt/repair.py:467~469` — `answered_valid`라도 거절 사유가 구조적(`uncovered`·`invalid_timing`·`invalid_metadata`)이면 재시도를 사지 않는다. 지금은 40 ms 꼬리 때문에 온도 1.0 호출을 한 번 더 산다.
