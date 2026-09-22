# Q3 — 글자는 gpt-transcribe, 시각은 whisper-1 (하이브리드) · 규칙 1·5 개정

> **v2 — Q3-1(엔진) 구현 브리프.** v1은 (A) Opus **UNSOUND**, (B) GLM SOUND_WITH_CONCERNS.
> 두 검토가 **같은 핵심 결함을 독립 재현**했다(§9). 전문: `../reviews/{opus,glm}-q3-direction.md`.
> 사용자 결정(2026-09-16): 자막 모델을 gpt-transcribe로 — 단독은 시각이 없으므로 **하이브리드**.

## 작업 환경 — 별도 worktree

- worktree: **`/Users/ssojux2/orca/workspaces/vlogsrt/q3-retext`**, 브랜치 **`Ssojux2/q3-retext`**,
  기준 `92c4ddf`(pytest 2500). 같은 시각 다른 구현자가 `fcp-prototype` worktree에서 PROTO-6b
  (`export.py`·`commands.py`·`tests/project/test_export.py`·`test_project_cli.py`·e2e·RUNBOOK)를
  고치고 있다 — **그 파일들은 건드리지 마라.** 머지는 코디네이터가 한다.
- 이 worktree에는 venv가 없다. **모든 명령에 `PYTHONPATH=/Users/ssojux2/orca/workspaces/vlogsrt/q3-retext`를
  붙이고** 파이썬은 `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python`.
  시작할 때 `python -c "import vlogsrt; print(vlogsrt.__file__)"`가 **q3-retext 경로**인지 확인해
  보고하라(아니면 다른 코드를 테스트하게 된다).
- 툴체인 shim 사망: git은 `/Library/Developer/CommandLineTools/usr/bin/git`, `make` 금지.
- `.env` 읽기·수정 금지, `--online`·유료 호출 금지, `tests/fixtures/api/**`·`golden/**` 수정 금지.
  `tests/fixtures/synthetic/**`는 만들고 고쳐도 된다.

---

## 1. 절대선

- **시각·세그먼트 경계는 whisper-1 `segments[]`에서만.** 바뀌는 것은 글자뿐이다.
- **whisper에 없는 gpt 글자에는 시각이 없다.** 그 글자를 어느 세그먼트에 붙이는 순간 그 세그먼트의
  시각을 준 것이다 — D24의 실패 모양. **세그먼트 사이 삽입은 출력하지 않고 기록만 한다.**
- **확신 없는 교체는 하지 않는다.** 교체는 세그먼트 단위 수락 게이트를 통과할 때만. 실패하면
  whisper 글자 + 기록.

**그러므로 이 기능의 정직한 이름은 "gpt-transcribe로 whisper 글자를 교정한다"이다.** whisper가 통째로
놓친 말은 되살리지 못한다(시각이 없다) — 기록에는 남는다. 사용자에게 이 한계를 보고한다.

## 2. 파이프라인

```
draft → repair.settle → spine(whisper 글자, 불변 — 증거)
      → gpt-transcribe 조각 응답 확보        ← retext_enabled면 verify 플래그와 무관하게 호출
      → retext.plan(...)  순수 함수: seg_id → 교체 글자 + 기록
      → spine_to_cues(spine, retext=계획)    글자만 교체, 시각·seg_ids 그대로
      → polish → refine → SRT 저장
      → verify(출력 큐 대 이미 받은 gpt 응답) ← 두 번 부르지 않는다
```

### 2-a. gpt 호출 소유 [두 검토 공통, 치명]

- 호출은 **`retext_enabled`에만** 의존한다. `pipeline.run`의 `if not verify_with_second_model: return`
  (`pipeline.py:218-219`) **밖**, SRT 저장 **앞**.
- 검증은 같은 응답을 재사용한다(retext 꺼짐 + 검증 켜짐이면 오늘처럼 검증이 호출).
- 불변식: **retext on 상태에서 검증 on/off SRT 바이트 동일.** 테스트로 못 박는다.
- gpt 호출 실패·응답 없음 → 전체 whisper 글자 + WARN + trace에 `status: "unavailable"`. SRT는 막지 않는다.

### 2-b. verify는 **출력 큐**와 대조한다 (판정: A)

(B)는 whisper 기준 큐와 비교하자고 했고 (A)는 반대했다. **(A)를 따른다** — whisper 기준 큐를 따로
`refine`하면 번호가 SRT와 어긋나고(재현: 1304 vs 1342) `refine`을 두 번 돈다. verify 코드는
바꾸지 않는다. 교체된 세그먼트는 자명 일치가 되지만, **whisper로 남은 세그먼트의 불일치가 곧
"교정하지 못한 자리"의 신호다.** 두 모델의 원래 차이는 retext 기록(조각·세그먼트별 유사도)이 담는다.
D37에 "검증의 독립성은 retext 기록으로 옮겨 간다"를 적는다.

## 3. 정렬 — `vlogsrt/retext.py` (순수 함수)

**입력은 순수 값**: spine 세그먼트, 조각 `(index, offset, text)` 목록, 영상 길이, repairs, cfg.
`ChunkResponse`·`transcribe`·`verify`를 import하지 않는다(검사 추가). `hangul`은 import해도 된다.

조각 하나마다:

1. **조각 구간**: offset 순 정렬, 끝은 다음 offset(마지막은 영상 길이) — `verify.chunk_spans`와
   같은 규칙을 retext 안에 둔다(verify import 금지). 세그먼트는 `start`로 조각에 배정한다.
2. **정규화 + 인덱스 보존**: 대조는 `hangul.strip_for_compare` 규칙(공백·부호 제거, casefold)으로 하되,
   정규화 문자 → **원본 gpt 문자열 위치** 대응표를 유지한다. 출력 글자는 원본 gpt 문자열에서 자른다
   (gpt의 문장부호·띄어쓰기 보존, 이후 polish가 다듬는다).
3. `difflib.SequenceMatcher(None, W_norm, G_norm, autojunk=False)`의 opcode:
   - `equal`: 위치 1:1 대응.
   - `replace`: 블록 **안에서** W 쪽 위치를 G 쪽 위치로 선형 대응(문자 인덱스 비례 — 시간 아님).
     **잉여 글자를 버리지 마라**(B가 79자 유실을 재현) — 블록 전체가 블록이 걸친 세그먼트들에 빠짐없이
     나뉜다.
   - `delete`(whisper에만): 해당 세그먼트의 교체 글자에서 빠진다 → 게이트가 판단.
   - `insert`(gpt에만): **세그먼트 사이에 끼면 출력하지 않는다.** `unplaced`로 기록(글자 수·원문·
     앞뒤 seg_id). 세그먼트 문자 범위 **내부**에 끼는 삽입은 그 세그먼트 몫이다.
4. 세그먼트 `[a,b)`의 교체 글자 = 대응된 G 원본 범위. 어절 경계로 스냅.
5. **세그먼트 수락 게이트** — 셋 다 통과해야 교체 [두 검토 공통, 치명]:
   - 세그먼트 내부 일치율(equal 문자 수 / whisper 정규화 길이) ≥ `retext_segment_match_floor`
   - 길이 비 `len(G_norm_seg) / len(W_norm_seg)` ≤ `retext_length_ratio_max` —
     단, 차이가 `retext_length_slack_chars` 이하면 비율 무시(1~2자 세그먼트 폭발 방지)
   - 길이 비 ≥ `retext_length_ratio_min` — gpt가 말을 **지우는** 방향도 막는다(A 재현: 조각에서
     gpt 7343자 대 whisper 9624자)
   - 교체 글자가 비면 불통과.
   불통과 → whisper 글자, 기록에 사유.
6. **조각 게이트**: 조각 유사도 < `retext_chunk_similarity_floor` → 그 조각 전체 whisper. 이 값은
   보조다 — **안전선은 세그먼트 게이트가 진다**(B 재현: ratio 0.91~0.98로 통과한 조각에서 82~165배 몰림).
7. **중복 금지**: 한 G 위치가 두 세그먼트의 교체 글자에 들어가지 않는다. 게이트 불통과 세그먼트의
   whisper 글자와 이웃 교체 글자가 같은 말을 두 번 싣지 않도록 — 불통과 세그먼트에 대응된 G 범위는
   이웃에게도 주지 않는다(A 재현: 폴백 126·306개 → 중복 출력 경로).

### 3-a. 되풀이 구간 [판정: B 정밀화 채택]

- `Repair.outcome == "collapsed"` 또는 `"kept"`인 `Repair.span`과 **시간이 겹치는** 세그먼트는 교체하지
  않는다(seg_id는 repair가 재번호하므로 키가 아니다). 그 구간의 gpt 글자 수를 **WARN**과 기록으로 남긴다 —
  "접힌 구간에 실제 발화가 있었을 수 있다"는 신호(규칙 9: 경고하고 계속).
- `"replaced"`는 재전사로 받은 새 whisper 글자라 **교체 허용**(게이트는 그대로 적용).
- 길이 0 span(`(0.0, 0.0)` 기본값)이 오면 WARN.
- 테스트: repair가 있는 픽스처(loopy 계열)에서 retext on ⇒ **제외된 세그먼트가 실제로 존재한다**를 단언
  (빈 repairs가 배선되면 조용히 통과하는 모양을 막는다).

## 4. 기록 · redo · trace

- `Trace`에 `retext` 필드: 적용 여부/상태, 조각별(유사도·게이트 결과), **seg_id → 교체 글자 전체 매핑**,
  세그먼트별 불통과 사유, `unplaced`, 제외된 repair 구간과 gpt 글자 수.
- `TRACE_VERSION`을 올린다. 기존 정확 일치 게이트 관례를 따른다(옛 trace는 명확한 메시지로 거부).
- **`pipeline.redo`가 매핑을 재적용한다** [두 검토 공통] — 안 하면 `vlogsrt redo`가 gpt 글자를 조용히
  whisper로 되돌린다.
- 교체된 큐 `flags`에 `retext`.

## 5. 설정 (규칙 8) — 전부 조정 대상 `retext_*` 계열

`retext_enabled = True`, CLI `--no-retext`(레거시 `run`).
`retext_segment_match_floor`, `retext_length_ratio_max`, `retext_length_ratio_min`,
`retext_length_slack_chars`, `retext_chunk_similarity_floor`.

- 모델은 **`verify_model` 재사용**(새 리터럴 없음).
- **초기값은 잠정이다.** 유일한 실데이터 `fr_day_1_2_fhd`는 **38분 조각 2개 + 규칙 6 이전 prompt로
  녹화**돼(A 확인: prompt 메아리 103회) 분포 근거가 못 된다. 합성 픽스처(demo·loopy·loopylong·loopyletter)와
  §8의 재현 시나리오에서 **보수적으로**(교체를 덜 하는 쪽) 고르고, 값마다 근거를 D37에 남긴다.
  사람이 현재 설정(180초 조각)으로 재녹화하면 다시 정한다(PENDING_HUMAN).
- CLAUDE.md 임계값 표에 `retext_*`를 조정 대상 계열로 추가.

## 6. 파생 계층 — Q3-1에서는 명시적으로 whisper 전용

- `project/speech.py:693`, `project/boundary.py:1261`의 `pipeline.spine_to_cues` 호출에
  **명시적 whisper 전용 인자**를 준다(기본값으로 조용히 whisper가 되지 않게 — 인자는 필수 키워드로).
- 파생 계층 설정 지문은 `cfg.to_dict()` 전체 해시라 `retext_*`가 들어간다. **Q3-2가 켤 때 옛 whisper 글자
  완성물이 같은 키로 재사용되지 않도록** Q3-2에서 분석 키 버전을 올린다 — 이 사실을 D37과 TASKS.md Q3-2에 적어라.
  (파생 계층은 오늘 gpt-transcribe를 **호출하지 않는다** — Q3-2는 모든 소스 전사에 새 유료 호출을 더한다.)

## 7. 규칙 · 문서

- **D37** (`docs/DECISIONS.md`): 결정, 하이브리드 근거, D24와 다른 점(시각 무배분·삽입 미출력·세그먼트
  게이트), **retext는 한글·영문·숫자를 바꾸고 지울 수 있는 새 권한**, 검증 독립성의 이동, 힌트/keywords가
  이제 출하 글자에 영향, 조각 게이트만으론 부족하다는 실측, 임계값 잠정성, 한계(놓친 말은 못 되살림), Q3-2 키 버전.
- **CLAUDE.md**: 머리말("whisper-1이 시각과 글자를 모두 만들고"), 규칙 1·5 개정문, 파이프라인 도표,
  명령어 절(`--no-retext`, `--no-verify` "비용 절반"이 retext on에서는 더 이상 사실이 아님), 임계값 표,
  도메인 용어 `retext / 글자 교정`, 절대 금지 목록(필요시). **레거시 규칙의 다른 문장은 건드리지 마라.**
- `scripts/check_invariants.py`: `retext.py`가 `transcribe`·`preprocess`·`verify`·`refine`·`output`·`layout`·
  `repair`를 import하지 않는다. 기존 autojunk 검사가 retext.py에도 걸리는지 확인. **불변식 수가 늘면
  보고하라**(현재 20).

## 8. 테스트 — 실패 우선

합성 단위(순수 함수):
1. **폭주 재현 셋이 게이트에 막힌다**: 과소전사("음" 1자 vs gpt 80자), 환각("..." ×5 vs 실제 발화),
   collapse 잔해("치즈" vs 300자) → 교체 안 됨, 기록에 사유.
2. 삭제 방향(gpt가 whisper 말을 절반 지움) → 교체 안 됨.
3. `replace` 잉여가 유실되지 않는다(블록 글자 합 보존), 중복 배정 없다.
4. 세그먼트 사이 `insert` → 출력 안 됨, `unplaced`에 기록.
5. 부호 차이만 있는 조각 → 전부 교체, 출력에 gpt 부호 보존.
6. 조각 배정: offset 경계, 마지막 조각은 영상 끝까지.

파이프라인(오프라인 합성 픽스처):
7. `spine_to_cues` 직후 큐 개수·시각·seg_ids가 retext on/off 동일. **refine 후 개수 동일은 요구하지 않는다**
   (A 재현: 글자 수가 refine 분할을 바꾼다) — 대신 모든 큐의 seg_ids·시각이 whisper 세그먼트 범위 안.
8. retext on에서 검증 on/off SRT 바이트 동일. `--no-verify`여도 gpt 응답이 쓰인다.
9. gpt 응답 없음/예외 → whisper 글자로 SRT 정상, WARN, trace `unavailable`.
10. loopy 계열: repair 제외 세그먼트가 실제로 존재하고 WARN.
11. redo가 매핑을 재적용해 같은 SRT를 낸다.
12. 파생 계층 호출 두 곳이 명시적 whisper 전용이라 기존 project 테스트 무변경.

**기존 테스트가 글자 변화로 빨개지면**: 그 테스트가 whisper 동작(되풀이·조판 등)을 검증하는 장면이면
**설정에 `retext_enabled=False`를 명시**해 장면을 분명히 한다(단언 약화 아님). 파일::테스트마다 이유를 보고.
retext 동작을 검증하는 장면이면 새 기대값이 옳은지 설명과 함께 고친다. 단언 삭제·느슨화 금지.

## 9. 방향 검토 기록 (v1 → v2)

### 두 검토가 같이 재현한 것
- **v1 §4의 "삽입은 앞 세그먼트에 붙인다" + 조각 단위 게이트가 D24 모양을 되풀이한다.**
  (A) 실픽스처: 0.4초 세그먼트에 gpt 929자, refine 후 412자·1.95초 큐, cps 884 큐. (B) 합성: 조각 ratio
  0.91~0.98로 **통과**한 채 큐 하나에 82~165배.
- `--no-verify`가 gpt 호출을 막으면 retext가 조용히 꺼지고 SRT가 검증 플래그에 따라 달라진다.
- `redo`가 trace spine에서 큐를 다시 만들어 교체를 조용히 되돌린다.
- 파생 계층 `speech.py:693`·`boundary.py:1261`이 `spine_to_cues`를 직접 부른다.
- 소유 파일 목록 부족(`models.py`·`CLAUDE.md`·`check_invariants.py`·기존 테스트).

### 뒤집힌 내 주장
| v1 | 실제 |
|---|---|
| "추가 API 비용 없음" | 엔진·검증 켜짐에서만. 파생 계층은 오늘 gpt를 **안 부른다** — Q3-2는 모든 소스에 새 유료 호출 (A) |
| "큐 개수가 retext on/off에 완전히 같다" | refine 후 거짓. 1304 vs 1342 재현 (A) |
| "초기값은 `fr_day_1_2_fhd` 조각별 분포로" | 조각 2개(38분), prompt 메아리 픽스처. 유사도 0.59 — 근거 불가 (A·B) |
| "retext.py 입력 = ChunkResponse" + "transcribe를 모른다" | 모순 (A). 순수 값으로 받는다 |
| 기준 `8ca214d`·2488 | 브리프 작성 시점 HEAD는 `f0702c8`·2498 (A·B) |
| 정규화 "공백 정리만" | 부호 차이가 opcode를 35% 부풀린다. 부호 제거 + 인덱스 보존 (B) |

### 갈린 지점 — 판정
- **삽입 처리**: (A) 출력 안 함 / (B) 앞 세그먼트 + 배율 상한. **(A)** — 시각 없는 글자에 시각을 주는 것 자체가
  규칙 1 개정안의 금지와 부딪친다. (B)의 배율 상한은 세그먼트 게이트로 채택.
- **verify 대조 대상**: (A) 출력 큐 / (B) whisper 기준. **(A)** — 번호 어긋남과 refine 이중 실행(§2-b).
- **재녹화 선행 여부**: (A) Q3-1 전 필수 / (B) 불필요. **구현·테스트는 불필요(B), 임계값의 실측 근거는
  필요(A)** — 잠정 보수값으로 구현하고 재녹화를 PENDING_HUMAN으로 둔다.
- **되풀이 구간**: 둘 다 제외 동의. (B)의 정밀화(`replaced`는 허용)를 채택.

## 10. DoD

(모든 명령 `cd /Users/ssojux2/orca/workspaces/vlogsrt/q3-retext` + `PYTHONPATH=$PWD`, 파이썬은 fcp-prototype의 `.venv`)
```bash
PY=/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin
PYTHONPATH=$PWD $PY/python -m pytest tests/test_retext.py
PYTHONPATH=$PWD $PY/ruff check vlogsrt ui tests scripts
PYTHONPATH=$PWD $PY/ruff format --check vlogsrt ui tests scripts
PYTHONPATH=$PWD $PY/mypy
PYTHONPATH=$PWD $PY/python scripts/check_invariants.py | grep -c '^\[OK\]'
PYTHONPATH=$PWD $PY/python -m pytest
```

## 11. 보고 형식

```
커밋 SHA:
import 경로 확인: <vlogsrt.__file__>
retext 공개 함수: <시그니처>
gpt 호출 위치: <file:line — verify 게이트 밖임을 보여라>
세그먼트 게이트: <file:line + 설정 키와 잠정값, 값마다 근거>
폭주 재현 셋 결과: <테스트 출력>
fr_day_1_2_fhd 오프라인 실측: <조각 유사도, 교체/불통과/unplaced/제외 수, 최대 cps 큐 — 참고용>
redo 재적용: <테스트>
바꾼 기존 테스트: <파일::테스트 — retext_enabled=False 명시인가 기대값 변경인가, 이유>
불변식: <[OK] 개수, 추가 검사>
게이트: <명령마다 실제 숫자>
diff 범위: <git diff --stat — PROTO-6b 파일 무접촉인가>
멈춰야 했던 것:
```
