# PROTO-6b — 분석 실패가 "차단 없음"으로 나가는 결함

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 **`92c4ddf`** (트리 깨끗, pytest 2500 passed)

**툴체인 shim 사망**: git은 `/Library/Developer/CommandLineTools/usr/bin/git`, 파이썬은
`.venv/bin/python`, **`make` 금지**. ffmpeg·xmllint 정상.

## 소유 파일

- `vlogsrt/project/export.py`
- `vlogsrt/project/commands.py`
- `tests/project/test_export.py`, `tests/project/test_project_cli.py`, `tests/project/test_project_e2e.py`
  (필요한 만큼. 다른 테스트 파일이 이 변경으로 빨개지면 §4)
- `docs/RUNBOOK.md` (PROTO-6이 추가한 프로젝트 절 안에서만), `docs/orchestration/progress.md` (추가만)

`boundary.py`·`guard.py`·`edit.py`·`runner.py`는 **범위 밖**. 픽스처·golden·임계값·`.env` 금지.
`--online`·유료 호출 금지.

---

## 1. 결함 — 코디네이터가 재현했다

PROTO-6 로컬 스모크(`$TMPDIR/proto6-smoke.*`): 전사 픽스처가 없는 클립을 오프라인으로 돌리면
전사기가 실패한다. 그런데 출력은:

```
최종 판정: 차단 없음        EXIT=0
review.json: final_accepted True, final_blockers [], review [], unavailable_instances []
VLOG_AI_EDIT.srt / VLOG_AI_SOURCE.srt : 0 바이트
```

실패 이유는 review·trace·artifacts **어디에도 없다.** 온라인에서 STT가 실패해도(429·키 오류)
**같은 모양**이다 — 사람이 보기엔 "성공, 자막 0장"이다. 이 저장소의 `protected=[]` 계열 결함이다.

## 2. 원인 — 사라지는 자리

- `boundary._resolve_slices`(`boundary.py:622-652`)는 설계상 **post-PCM 실패를 unavailable로
  강등하지 않고 잠금만** 남긴다 → `plan.retained[].reasons == ['kept_locked']`. 컷은 안전하게 막힌다.
- 가드는 실패를 **타입으로** 기록한다: `guard._blocked_reviews`(`vad/guard.py:576-589`)가 전사
  트레이스 error·VAD error·VAD 입력 결함·빈 덮개를 모아 `_keep_all`(`:671`)로 닫고, 인스턴스 가드는
  `status="keep_all"`이 되며 `review`에 이유 문장이 남는다. `InstanceGuard` 독스트링: `keep_all`은
  **실패 닫힘**이고 `inaudible`이 더 강한 진술이라 둘이 겹치면 `inaudible`이 이긴다.
- **`export._final_blockers`(`export.py:707-744`)는 가드를 보지 않는다.** `publish_package`는 이미
  `guards`를 받는데(`export.py:610`) 렌더와 검증에만 쓴다.

## 3. 고칠 것

### 3-a. 최종 판정: `keep_all` 가드는 블로커다

- `_final_blockers`가 가드를 받는다. **`status == "keep_all"`인 인스턴스 가드가 하나라도 있으면**
  블로커 `analysis_failed_instances`를 더한다.
- 판정은 **타입(`status`)으로만.** 한국어 `review` 문장을 파싱하지 마라.
- `inaudible`(선언된 무음)은 블로커가 **아니다**(완결된 사실). `success`도 아니다.
- 합성 `stream_unavailable`/`pcm_unavailable` 가드도 `keep_all`이므로 기존
  `retryable_unavailable_instances`와 함께 뜬다 — 중복은 허용한다(둘은 다른 증거다).

### 3-b. 이유를 기록에 남긴다

`review.json`에 **`analysis_failures`**: `status == "keep_all"`인 가드마다
`{instance_id, source_id, review: [...]}` (가드의 `review` 문장 그대로). 문서 인스턴스 순서.
`kind` 버전 문자열을 올릴지는 기존 관례를 따르고 보고하라.

### 3-c. CLI 출력

`commands._report`가 `analysis_failures`가 있으면 **눈에 띄게** 개수와 각 이유를 출력한다
(`log.out` — PROTO-5가 초안 경고를 억제 불가 채널로 낸 것과 같은 이유). 종료 코드는 기존 표 그대로
**1**(출판됨, `final_accepted` 거짓).

## 4. 다른 테스트가 빨개지면

기존 export·runner·e2e 테스트 중 `keep_all` 가드를 두고 `final_accepted is True`를 단언하는 장면이
있으면 — PROTO-1b와 같은 구분을 한다: **그 장면이 정말 분석이 끝난 장면인가?**
- 실패 닫힘 가드를 "분석 완료" 장면에 쓴 것이면 **장면 구성**을 `success` 가드로 고친다.
- 실패를 다루는 장면이면 기대값이 틀린 것이 아니라 **새 블로커가 맞다** — 단언을 새 사실로 고치되,
  **어느 파일의 어느 테스트를 왜 바꿨는지 하나씩 보고하라.** 단언을 약화(삭제·느슨화)하지 마라.
- 소유 파일 밖 테스트를 고쳐야 하면 고치고 **반드시 목록으로 보고**하라.

## 5. 테스트 — 실패 우선

1. `keep_all` 가드 하나 → `final_accepted False`, blockers에 `analysis_failed_instances`,
   `analysis_failures`에 그 가드의 이유 문장.
2. `inaudible` 가드만 → 블로커 아님.
3. 전부 `success` → 기존과 같음(회귀).
4. CLI: 전사기가 실패하는 오프라인 실행(스모크와 같은 모양) → **종료 코드 1**, stdout에 분석 실패
   개수와 이유.
5. 뮤테이션: `_final_blockers`에서 새 줄을 지우면 1·4가 죽는다(보고용).

## 6. 로컬 스모크 재실행

PROTO-6의 스모크 디렉터리(`ls -d $TMPDIR/proto6-smoke.*`)에서 **새 OUT**으로 같은 명령을 다시 돌려
종료 코드·stdout·review.json을 기록한다. 기대: **종료 1**, 블로커 `analysis_failed_instances`,
이유에 `전사 트레이스가 error다`. `progress.md`에 `## PROTO-6b 스모크 재실행` 절로 남기고, RUNBOOK
프로젝트 절의 "오프라인 실제 모습"을 **이 새 동작으로 고친다**(자막 0장·종료 0 문구 제거).

## 7. DoD

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
.venv/bin/ruff check vlogsrt ui tests scripts
.venv/bin/ruff format --check vlogsrt ui tests scripts
.venv/bin/mypy
.venv/bin/python scripts/check_invariants.py | grep -c '^\[OK\]'   # 20
.venv/bin/python -m pytest
```

## 8. 보고 형식

```
커밋 SHA:
_final_blockers 변경: <file:line + diff>
analysis_failures 기록: <review.json 예시>
CLI 출력: <실제 stdout>
바꾼 기존 테스트: <파일::테스트 — 장면 수정인가 기대값 수정인가, 이유>
뮤테이션: <실제 출력>
스모크 재실행: <명령 · 종료 코드 · stdout · blockers>
게이트: <명령마다 실제 숫자>
diff 범위: <git diff --stat>
멈춰야 했던 것:
```
