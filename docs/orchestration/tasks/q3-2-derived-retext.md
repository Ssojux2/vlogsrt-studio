# Q3-2 — 글자 교정을 파생 계층에 켠다

작성: Opus(코디네이터). **Sol이 아니다** — Sol 워커가 `unverifiable`로 멎어 있어
대신 썼다. 역할을 몰래 바꾸지 않는다는 규칙에 따라 여기에 적는다.

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/q3-2-derived-retext`
브랜치: `Ssojux2/q3-2-derived-retext`, 기준 **`3c57ba5`**

**툴체인**: git은 `/Library/Developer/CommandLineTools/usr/bin/git`, 파이썬은
`/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python`, **`make` 금지**.

---

## 1. 무엇을

Q3-1이 레거시 자막 엔진에 글자 교정을 넣었다(D37). 시각과 세그먼트 경계는 whisper
그대로 두고, 게이트를 통과한 세그먼트의 **글자만** gpt-transcribe 글자로 바꾼다.

파생 계층(`vlogsrt/project/`)은 아직 whisper 글자만 쓴다. 그것을 켠다.

□ 오늘의 자리 — 두 곳이 `pipeline.spine_to_cues`(whisper 전용)만 부른다
  ○ `vlogsrt/project/speech.py:693`
  ○ `vlogsrt/project/boundary.py:1261`
□ 레거시의 대응 자리는 `pipeline._make_cues`이고, 거기서는
  `apply_retext(spine_to_cues(...), record)`로 기록을 얹는다

## 2. 소유 파일

□ 고칠 수 있는 것
  ○ `vlogsrt/project/speech.py`
  ○ `vlogsrt/project/boundary.py`
  ○ `tests/project/test_speech.py`, `tests/project/test_boundary_transcribe.py`
  ○ `docs/DECISIONS.md`(새 결정 하나), `docs/PROJECT_CONTRACTS.md`(필요한 만큼)
□ 범위 밖. 읽기만 해라
  ○ `vlogsrt/retext.py`, `vlogsrt/pipeline.py`, `vlogsrt/config.py` — Q3-1이 확정했다
  ○ `vlogsrt/project/export.py`, `commands.py`, `runner.py` — PROTO-6b가 방금 고쳤다
  ○ `vlogsrt/project/edit.py`, `vad/**`, `vision/**`
□ 다른 테스트가 이 변경으로 빨개지면 고치되 **어느 파일의 어느 테스트를 왜 바꿨는지
  하나씩 보고하라.** 단언을 약화하지 마라

## 3. 계약

### 3-a. 배선

□ 두 호출부가 `retext.plan`으로 기록을 만들고 `pipeline.apply_retext`로 얹는다
  ○ `retext.plan(spine, chunks, duration, repairs, cfg) -> RetextRecord`
  ○ `apply_retext(cues, record)`는 **글자와 플래그만** 바꾼다. 시각은 안 건드린다
□ 순서는 레거시와 같다 — `spine_to_cues` → `apply_retext` → `refine.refine`
  ○ 레거시가 `pipeline.py:355`에서 그렇게 한다. 다르게 하지 마라

### 3-b. 두 번째 모델 호출

□ **새 유료 호출이다.** 파생 계층은 오늘 gpt-transcribe를 부르지 않는다
□ 소스 전사마다 두 번째 모델 호출이 하나씩 더해진다
□ 호출은 `transcribe.py` 경계 안에서만 한다(규칙 7). `speech.py`가 직접 HTTP를 치지 마라
□ 레거시는 `pipeline._SecondModel.ask`가 `build_job(prepared, VERIFY_KIND)`로 묶는다.
  파생 계층의 대응물은 `_first_pass`가 만드는 `TranscriptionJob`과 같은 조각을 써야
  한다 — **두 모델이 같은 경계를 봐야 대조가 성립한다**
□ 두 번째 모델이 죽어도 whisper 자막은 나간다. `retext.unavailable(이유)`로 기록하고
  `WARN`을 남긴다. 실패를 빈 성공으로 바꾸지 마라(규칙 9)
□ `cfg.retext_enabled`가 거짓이면 호출하지 않고 `retext.switched_off()`다

### 3-c. 분석 키를 올려라 — 이것이 가장 중요하다

□ 설정 지문(`_config_sha256`)은 `cfg.to_dict()` 전체 해시라 `retext_*`가 **이미 들어
  있다.** 값이 그대로인 채 동작만 켜지면 **옛 whisper 글자 완성물이 같은 키로
  재사용된다**
□ 그러니 키 도메인 문자열의 버전을 올린다
  ○ `vlogsrt/project/speech.py:72` `_SOURCE_KEY_DOMAIN = "vlogsrt-source-analysis-v1"`
  ○ `vlogsrt/project/boundary.py:115` `_BOUNDARY_KEY_DOMAIN = "vlogsrt-boundary-analysis-v1"`
□ 두 도메인이 **함께** 올라가야 한다. 한쪽만 올리면 경계 재전사가 옛 글자를 물어온다
□ 올린 이유를 주석과 `docs/DECISIONS.md`에 남긴다

### 3-d. 기록

□ 되풀이 제외와 `unplaced`를 `SourceTrace`에 싣는 방법은 **네가 정한다.** 정하고
  근거를 보고하라. 정할 때 지킬 것
  ○ `SourceTrace`는 완성되면 불변이고 캐시 신원이 `analysis_key`다
  ○ 조용히 버리지 마라 — `unplaced`는 출력되지 않는 글자이므로 반드시 남는다(규칙 9)
  ○ 새 필드를 더하면 그 필드가 선택적임을 계약에 적어라

### 3-e. 비용

□ TASKS.md는 "비용 추정(`inspect`)도 함께 고친다"고 적었다. **`inspect` 명령은 아직
  없다** — P7-03의 계획이다. 내가 확인했다
□ 그러니 고칠 추정기가 없다. 대신 늘어난 호출을 **문서에 적어라**
  ○ `docs/PROJECT_CONTRACTS.md`에 소스 전사마다 두 번째 모델 호출이 하나 붙는다는
    사실과 그 비용 성격을 적는다
  ○ 추정기 자체는 P7-03의 몫으로 남긴다. 여기서 만들지 마라

## 4. 임계값

□ `retext_*` 다섯 값은 **잠정이다.** Q3-H 재녹화가 끝나야 확정된다
□ 값을 바꾸지 마라. 배선만 한다. 값은 `config`에서 읽는다(규칙 8)
□ 다른 임계값도 바꾸지 마라

## 5. 테스트 — 실패 우선

□ 반드시 덮을 것
  ○ 게이트를 통과한 세그먼트의 글자가 바뀌고 **시각은 한 밀리초도 안 바뀐다**
  ○ 두 번째 모델이 죽으면 whisper 글자로 나가고 `WARN`이 남는다
  ○ `retext_enabled`가 거짓이면 두 번째 모델을 **부르지 않는다**(호출 수를 세라)
  ○ 분석 키가 켜기 전후로 **다르다** — 옛 완성물이 재사용되지 않는다
  ○ `unplaced`와 되풀이 제외가 기록에 남는다
□ 죽은 검사를 만들지 마라. 단언 대상이 비어 있으면 먼저 그것을 막아라(D32의 교훈)

## 6. 절대 금지

□ `.env` 읽기·수정, `--online`, 유료 API 호출
□ `tests/fixtures/api/**`, `golden/**` 수정. 읽기만 해라
□ 임계값·기대값 수정. 기대값을 바꿔야 하면 §2의 보고 규칙을 따른다
□ 테스트 `skip`·`xfail`
□ 새 의존성 추가
□ 조립된 `spine[]` 변경. `apply_retext`는 큐의 글자만 만진다

## 7. 게이트

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/q3-2-derived-retext
PYTHONPATH=$PWD <python> -m ruff check vlogsrt ui tests scripts
PYTHONPATH=$PWD <python> -m ruff format --check vlogsrt ui tests scripts
PYTHONPATH=$PWD <python> -m mypy
PYTHONPATH=$PWD <python> -m pytest
PYTHONPATH=$PWD <python> scripts/check_invariants.py
```

□ `3c57ba5` 기준선: ruff format 99개 파일, mypy 47개 파일, **pytest 2543 통과**,
  불변식 21개 `[OK]`
□ 새 테스트로 pytest 수는 늘 수 있다. 줄면 회귀다

## 8. 보고

□ exact base, 브랜치, 워크트리, Orca task·dispatch ID, 커밋 SHA, 변경 파일
□ 두 호출부의 변경을 diff 그대로 인용하라
□ 분석 키가 실제로 달라지는 것을 **값으로** 보여라 — 켜기 전 키와 켠 뒤 키
□ `SourceTrace`에 기록을 싣는 방법을 왜 그렇게 정했는지
□ 다섯 게이트의 명령·종료 코드·실제 수치
□ 기대값을 바꾼 테스트가 있으면 파일·이름·이유를 하나씩
□ 남은 결함과 멈춘 항목. 없으면 `없음`

□ 막히면 재현 명령, 종료 코드, 짧은 로그, 예상값과 실제값을 적어 `ask`로 올려라
□ 구현이 끝나면 `ask`로 코디네이터 판정을 받고, 범위 안 지적을 같은 Dispatch에서
  고친 뒤 검사를 다시 돌려라
□ 승인 뒤에만 `worker_done`을 정확히 한 번. `--outcome`과 `--files-modified`를 붙여라
□ 머지도 push도 하지 마라 — 통합은 코디네이터가 한다
