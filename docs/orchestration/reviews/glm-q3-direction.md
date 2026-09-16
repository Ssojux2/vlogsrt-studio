# Q3 방향 검토 — (B) GLM 5.3

> 2026-09-16. 읽기 전용. **판정 SOUND_WITH_CONCERNS.** 합성 재현으로 조각 게이트 통과 뒤 82~165배 몰림. 반영 — 브리프 v2 §9.

조사·재현이 끝났다. 보고서 전문을 출력한다.

---

```
방향 판정: SOUND_WITH_CONCERNS
```

하이브리드의 축 자체(시각 whisper·글자 gpt·문자 정렬, 시각 유도 금지 유지)는 D24의
실패 축과 직교하고, 규칙 1·5 개정은 TASKS.md:37 Q3가 이미 전제로 둔 절차다. 그러나
브리프에는 **세그먼트 단위 안전선이 없다** — 없으면 D24의 실패 *모양*(글자 덩어리가
잘못된 큐에 몰림)을 재현한다(아래 실측) — 그리고 `redo`와 `--no-verify` 두 곳에서
P4-04형 갭이 코드로 확인된다.

---

## 시그니처 충분성: 두 곳에서 하류가 상위 정확값에 못 닿는다

**갭 1 — `vlogsrt redo`가 retext를 되감는다 (P4-04).** `pipeline.py:363`
`refine.refine(spine_to_cues(list(trace.spine)), cfg, trace.duration)` — redo는 큐를
spine에서 **다시 만든다**(최종 큐가 trace에 있어도 재사용하지 않는다,
`pipeline.py:364`). trace의 spine은 whisper 글자 그대로 저장되므로(`pipeline.py:208`),
Q3-1 후 `vlogsrt redo`는 매핑을 재적용하지 않는 한 **gpt 글자를 whisper 글자로 조용히
되돌린다**. 브리프는 redo를 한 번도 언급하지 않는다(§3·§7·§8 어디에도). 브리프의
“trace에 `retext[]` 기록”이 seg_id→교체글자를 담고 redo가 그것을 재적용해야 한다.

**갭 2 — 파생 계층의 직접 호출.** `project/speech.py:693`과 `project/boundary.py:1261`이
`pipeline.spine_to_cues`를 직접 부른다. Q3-1이 매핑 인자를 기본값 없이 추가하면 파생
계층이 깨지고, 기본값으로 추가하면 Q3-2 전까지 **조용히 whisper 글자**가 나간다(이중
어느 쪽인지 브리프가 명시해야 한다).

**충족되는 부분.** 픽스처 재생은 stem+kind 단일 키(`transcribe.py:78-79`)라 gpt 호출을
SRT 저장 앞으로 옮겨도 재생이 깨지지 않는다. gpt 응답에 `offset`이 있다
(`ChunkResponse`, `transcribe.py:30-40`) — 조각 짝짓기 가능. 단 `ChunkResponse`에
duration이 없어 조각 구간은 `verify.chunk_spans`(`verify.py:62-66`)와 같은 방식(연속
offset + 마지막 구간은 영상 끝까지)으로 유도해야 하는데, **retext는 verify를 import
못 하므로(§6) 이 도우미는 중립 모듈로 뽑거나 중복해야 한다** — 브리프가 이 충돌을
인지하지 않는다. 제외 키는 seg_id가 아니라 **시간 교차**여야 한다: repair가 세그먼트를
갈아끼우면 전체 재번호가 일어난다(`repair.py:549-551`), 그래서 `Repair.span`
(`models.py:196`)이 유일한 안정 키다.

**정규화 미세 갭.** verify의 정규화는 `hangul.strip_for_compare`(`hangul.py:24-26` —
공백·문장부호·제어 제거 후 casefold)다. 브리프 §4-3의 “공백 정리만” 대조 정규화는
문장부호 차이를 정면으로 마주친다 — 실측 `fr_day_1_2_fhd` chunk 0에서 부호 수
whisper 207자 대 gpt 777자, opcode 수 1771(raw) → 1151(부호 제거, 35% 감소).
정규화 문자열에서 원본 gpt 위치로 되돌릴 **인덱스 보존** 규격이 없으면 출력 글자가
정규화된 글자로 새어 나가거나(문장부호 소실·규칙 5 긴장) 대응이 어긋난다.

## 부재가 권한이 되는 자리: 세 곳 (일곱 번째 `protected=[]`의 후보)

1. **`--no-verify`가 retext를 끈다.** `pipeline.py:218-219`
   `if not verify_with_second_model: return result` — gpt 호출은 그 게이트 **안쪽**
   `report_verification`의 `pipeline.py:264`에 있다. “그 호출을 SRT 저장 앞으로 옮긴다”
   는 구현이 게이트 안에 남으면 retext on + verify off에서 응답 부재가 retext를 조용히
   꺼고, 브리프 §6의 “검증 on/off에 SRT 바이트 동일(retext on)” 불변식이 깨진다.
   명세가 **“retext_enabled면 verify 플래그와 무관하게 항상 호출”**을 못 박아야 한다.
   부수: CLAUDE.md 명령어 절의 “`--no-verify` (비용 절반)”이 retext on에서 거짓이 된다.
2. **repairs 빈 목록 전달.** §4-6 제외는 배선이 전달하는 `Repair.span` 목록 전부다.
   배선 실수로 빈 목록이 가면 제외가 하나도 없어도 **경고 없이 지나간다** — 이 저장소의
   여섯 번 재발한 결함의 정확한 모양. “repair 발생 + retext on ⇒ 제외된 seg_id가
   존재한다”는 단언 테스트가 필요하다. 실측 재현(아래 시나리오 3)에서 제외 누락 시
   collapse 잔해 큐에 331자/1.8s = **183.9자/s**가 몰린다.
3. **`Repair.span` 기본값 `(0.0, 0.0)`**(`models.py:196`). 시간 교차 판정에서 빈 범위는
   아무것도 제외하지 않는다. span 미설정 경로가 있는지 확인하고, 있으면 길이 0 span은
   WARN으로.

긍정 축: 픽스처 누락은 `RuntimeError`(`transcribe.py:82-89`)로 조용한 통과가 아니다.

## 계약·불변식·CLAUDE.md 규칙: 위반 없음, 문서 갱신 부채 있음

- 계약서 §1·4·5·6·7·8과 충돌 없음. openai는 재사용 호출 경로(`transcribe.py:282-307`)를
  그대로 탄다. 모델 리터럴(§7)은 `verify_model` 재사용 시 새 리터럴 자체가 생기지
  않는다 — 재사용을 권한다.
- `check_invariants.py`의 `_sources()`(`:90-91`)가 `vlogsrt/**/*.py`를 이미 훑으므로
  openai 경계·금지 import·autojunk(`:135`)·one-call(`:1008`) 검사가 retext.py에 **수정
  없이 자동 적용**된다. retext 전용 검사는 `CHECKS`(`:1038-1059`, 현재 정확히 20개)에
  등록만 하면 된다. (계약서 `:154`는 “19군”이라 적어 문서-코드 불일치가 이미 있다.)
- 규칙 5 개정 문구 정밀화: “retext만 큐 글자를 바꿀 권한” — 실제 **적용** 주체는
  `spine_to_cues`(`pipeline.py:160-171`)다. “retext가 계산하고 spine_to_cues가 적용하며,
  그 밖에 글자를 다루는 계층은 polish뿐”으로.
- 규칙 9: “교체 글자가 빈 세그먼트 → whisper 유지”(§4-5)는 옳다. 그러나 **반대 방향
  (교체 글자 폭주)의 안전선이 없고**, polish는 글자를 못 줄이므로(D30) 하류 회복이
  불가능하다 — retext 안에서 잡아야 한다.
- 힌트/keywords가 gpt 호출에 실린다(`transcribe.py:371-382`, `_HINTED_KINDS :57`) —
  규칙 6 위반은 아니지만 지금까지 표시 전용이던 힌트가 이제 **출하되는 글자**를
  좌우한다. D37에 기록할 것.

## 의존성·순서

- **브리프 기준점이 부정확하다**: HEAD는 `f0702c8`(PROTO-5 커밋 자체)이지 `8ca214d`가
  아니고, pytest는 2488이 아니라 **2498**(46 파일)이다. PROTO-6은 미착수 — PROTO 보드는
  별도 분기(`feat/final-cut-project`의 `a156b29`)에 있고 HEAD의 TASKS.md는 PROTO를
  추적조차 하지 않는다. Q3-1이 엔진 한정이라 실질 의존은 없지만 기준점은 고쳐라.
- **D37 번호는 안전** — D36은 미사용 예약번호다(2026-09-13 계획 R-09,
  `docs/plans/2026-09-13-cut-first-and-plugin.md:302`).
- Q3-2의 완성물 재사용 우려(§9-5)는 **구조적으로 이미 풀려 있다**:
  `speech.py:1035-1038`과 `boundary.py:1360-1363`의 지문은 나열식이 아니라
  `json.dumps(cfg.to_dict(), sort_keys=True)` 전체 해시다 — retext 필드가 엔진
  `Config`에만 있어도 옛 완성물은 자동으로 재사용되지 않는다. 회귀 테스트만 추가하면
  된다.

## 소유 파일로 가능한가: 불가 — 목록에 빠진 것

| 필요한 파일 | 이유 |
|---|---|
| `vlogsrt/models.py` (+`__init__.py`) | trace에 `retext[]` 필드(`:244-288`)와 TRACE_VERSION 게이트(`:277-278`, 현재 "9.0" 정확 일치) — 버전을 올리면 옛 trace 로드가 거부되고, 그대로 두면 from_dict 기본값 처리가 필요하다 |
| `vlogsrt/hangul.py` 또는 신규 중립 모듈 | 정규화·`chunk_spans` 공유(verify import 금지와 충돌) |
| `CLAUDE.md` | 머리말(“whisper-1이 시각과 글자를 모두 만들고”)·파이프라인 도표·명령어 절(“비용 절반”)이 Q3-1 후 거짓이 된다 |
| 기존 테스트 일괄 재갱신 | 14개 파일 약 330개 테스트가 하드코딩 글자·SRT 바이트를 단언(대표 `tests/test_repair_pipeline.py:142`). demo 스템은 whisper==gpt(실측 ratio 1.0)라 무사하지만 loopy 계열은 재갱신 대상 |

## 코디네이터 주장 검토

1. **“gpt-transcribe는 타임스탬프를 주지 않는다”** — 확인. 내가 직접 로드했다:
   응답 키 `['languages','logprobs','text','usage']`.
2. **“오늘 검증은 SRT 저장 뒤에 부른다”** — 확인. `pipeline.py:218-221`(주석 `:220`).
3. **“추가 API 비용 없음”** — 조건부 반박. 검증 기본 켜짐 기준으로만 참. retext on +
   `--no-verify`에서는 불변식을 지키려면 호출이 게이트 밖에 있어야 하므로 비용이 그대로
   남는다 — “비용 절반” 주석과 함께 갱신 필요.
4. **“검증 on/off에 SRT 바이트 동일(retext on)”** — 성립 조건부. §3의 “결과를 검증이
   재사용한다”는 문구만으로는 구현자가 게이트 안에 둘 유혹을 막지 못한다(위 1번).
5. **“spine은 whisper 글자 그대로 증거로 남는다”** — 확인. 그리고 그렇기에 redo 갭이
   재현된다(갭 1).
6. **§9-1 “D24를 되풀이하지 않는가”** — 부분 반박. 배분이 아니므로 **시각은 오염되지
   않는다**. 그러나 조각 단위 ratio는 국소 이격에 눈이 멀다. 브리프 §4 알고리즘을
   그대로 구현해 합성 재현(임시 경로, 저장소 무손상):

   | 시나리오 | 조각 ratio | 피해 큐 |
   |---|---|---|
   | 과소전사(whisper “음” 1자/2s, gpt 80자) | **0.9778** 통과 | 82자, 41.0자/s, **82배** |
   | 환각("..." 3자/2s ×5, gpt 실제 발화) | **0.9122** 통과 | 152자, 76.0자/s, 50.7배 |
   | collapse 잔해(“치즈” 2자/1.8s, gpt 300자) | **0.9166** 통과 | 331자, 183.9자/s, 165.5배 |
   | 대조군(완전 일치) | 0.9995 | 배율 1.0 |

   세그먼트별 배율 상한 가드(3배, 최소 8자)를 얹으면 셋 다 잡힌다(재현으로 확인).
   추가로, 첫 구현에서 `replace` opcode 양측 길이가 다를 때 긴 쪽 나머지 **79자가
   통째로 유실됐다** — 브리프 §4는 `insert`만 규정하고 `replace` 잉여 분배를 규정하지
   않는다. 명문화가 필요하다.
7. **실측 바닥값** — `fr_day_1_2_fhd`를 verify 자체 정규화로 재면 조각 유사도
   **0.6159 / 0.5943**. `verify_chunk_sim_floor=0.80`보다 한참 아래다. §5의 계획대로
   이 분포에서 초기값을 정하려면 ≤0.59여야 retext가 작동한다 — 그 값의 보호 가치는
   사실상 0이고, 안전선은 전적으로 세그먼트 가드가 진다. 이 사실을 D37에 명시하라.
8. **§9-2 되풀이 제외가 옳은가** — 원칙 동의 + 정밀화. `outcome`이 `replaced`인 구간의
   whisper 글자는 재전사로 새로 받은 깨끗한 글자다(`models.py:22-24`, 판정
   `repair.py:479-518`) — 교체해도 안전하고 이득이다. 제외는 `collapsed`/`kept`로
   좁혀라. 제외 시 gpt 쪽 글자 수를 WARN으로 남겨라 — collapse가 실제 발화를 잃었다는
   신호이자 규칙 9의 “경고를 남기고 계속한다” 정신에 맞는다.
9. **§9-3 verify의 whisper 기준 비교** — 의미 상실을 명시적으로 받아들여야 한다:
   retext 후 큐 글자는 gpt 글자이므로 현 `_check_chunk`(`verify.py:134-148`, 큐 대 gpt)
   는 자명 일치가 된다 — 검증은 더 이상 출하물의 독립 대조가 아니다. 다행히 verify는
   이미 spine을 받는다(`verify.py:71`) — whisper 기준 비교는 받은 데이터만으로 가능하고
   refine/pipeline import 없이 규칙 5를 지킨다. “검증의 독립성은 모델 불일치 보고로
   대체된다”고 D37에 기록하라.
10. **§9-4 polish 이전 gpt 글자** — 규칙 5의 “한 자도 못 지운다”는 글자 소스와 무관하게
    여전히 유효. 감사 구분은 `Cue.flags`로 가능 — 필드와 `with_flag`가 이미 있다
    (`models.py:74`, `:95-98`).
11. **§9-5 옛 완성물 재사용** — 반박(이미 해결). 전체 config 해시 설계가 자동 차단한다.
12. **§9-6 픽스처** — 답: **Q3-1은 새 녹화가 필요 없다.** 전 스템(demo·loopy·loopylong·
    loopyletter·fr_day_1_2_fhd)이 `.gpt.json`을 갖고 있다. 특히 fr_day_1_2_fhd는
    rescue/arbiter 픽스처가 없어 오프라인 repair가 collapse/keep로 떨어진다 — §4-6
    제외 경로의 실측 커버가 공짜로 따라온다. Q3-2도 `_Scripted` 패턴
    (`tests/project/test_boundary_transcribe.py:214-236`)으로 합성 커버 가능하다. 실물
    경계 gpt 녹화는 선택적.

## 최소 수정 명세 (심각도 순)

1. **[치명] 세그먼트별 폭주 가드.** 교체 글자 길이가 whisper 세그먼트 길이 × 상한(예:
   3배, 최소 8자)을 넘으면 그 세그먼트 whisper 유지 + WARN + trace 기록. config에
   등록(규칙 8, 조정 대상 계열). 근거: 위 재현 3본 + “polish는 회복 불가”.
2. **[치명] gpt 호출 소유 명세.** 호출은 `retext_enabled`에만 의존하고
   `verify_with_second_model` 게이트(`pipeline.py:218-219`) 밖에서 실행. 불변식 테스트:
   retext on + `--no-verify` ⇒ SRT 바이트 동일. CLAUDE.md 명령어 주석 갱신.
3. **[치명] redo 재적용.** trace `retext[]`에 seg_id→교체글자 저장, `pipeline.redo`
   (`pipeline.py:357-366`)가 재적용. TRACE_VERSION 게이트 처리 포함.
4. **[높음] 제외 범위 정밀화.** `collapsed`/`kept`만 제외, `replaced`는 교체 허용, 제외 시
   gpt 글자수 WARN. “빈 repairs 목록 배선” 단언 테스트 추가.
5. **[높음] 정규화·경로 규격.** `strip_for_compare` 재사용 여부 결정, 정규화→원본 gpt
   위치 인덱스 보존, `chunk_spans`의 중립 모듈화(또는 중복 결정).
6. **[중간] `replace` 잉여 분배 규칙 명문화** — 삽입 규칙과 같은 “앞 세그먼트에” 규칙으로
   확장. 재현: 미규정 상태의 자연스러운 구현이 79자를 유실했다.
7. **[중간] 소유 파일 목록에 `models.py`(+`__init__.py`)·`CLAUDE.md`·기존 테스트 재갱신
   추가.**
8. **[낮음] 브리프 기준점 수정**(`f0702c8` / pytest 2498). D37에 힌트가 출하 글자에
   미치는 영향 기록.

## 재현하지 못한 것

- **verify-off 배선 결함** — 구현이 없어 실행 재현 불가. `pipeline.py:218-221`의 현
  구조가 그 유혹을 만든다는 구조적 사실로만 보고한다.
- **Q3-2 재사용 경로의 옛 글자 유출** — 구현 전이라 재현 불가. 자동 차단은 해시 설계
  코드 확인(`speech.py:1035-1038`)로만 뒷받침한다.
- **loopy 계열 기존 테스트의 실제 깨짐 목록** — 재전사 후 spine 대 gpt 정렬을 돌려봐야
  안다. 합성 측정은 raw whisper 기준(loopy 0.4176, loopylong 0.2038)이라 상한이다.
- **어절 경계 밀기(§4-4)**의 효과 — 재현이 그것을 모델링하지 않았다. 경계 미세 조정이라
  몰림의 크기는 바꾸지 않으리라 보나 검증은 안 됐다.
- **실물 API에서의 retext 품질** — 유료 호출 금지로 불가.
