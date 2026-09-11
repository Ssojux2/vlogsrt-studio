# Task P6-02b: 검증기의 조용한 통과 경로를 닫고 출력 스토리라인을 정본으로 만든다

## Authority

기준 명세 `Visual scoring and deterministic roughcut`, CLAUDE.md 규칙 9.
후보 `bde0368`의 보정. Fable 5.1 방향 검토
`docs/orchestration/reviews/fable-p6-02-direction.md`와 GLM 검증자 발견의 합집합.
코디네이터: Opus 5 (Run `run_a091d4869a98`). 오프라인 전용.

## 네 구현은 브리프대로였다 — 결함 일부는 내 브리프에 있다

두 검토가 붙었다. GLM 검증자는 내가 준 7종 위반 중 **6종이 실제로 문다**고 확인했고
(정상 계획은 findings가 빈다), 적용 경로의 raise/keep 선·결정성·실제 단축
(500f·20초 → 325f·13초, 제거 정확히 175f)·불변식 뮤테이션까지 전부 재측정해 일치시켰다.
**이 목록에 대해서는 구현이 옳다.**

Fable은 그 목록 **밖**을 찔러 조용히 통과하는 경로 다섯을 손으로 만든 플랜으로 재현했다.
그리고 내 브리프의 구멍 둘을 명시적으로 지적했다. 아래는 그 합집합이다.

**중요한 심각도 구분(Fable)**: 이 경로들로 **자막이 조용히 사라지지는 않는다** —
guard 안의 큐는 안전하고 guard 밖 큐는 P6-03에서 하드 실패한다. 사라지는 것은
**보호 밖 footage**다. 부풀리지 말고 정확히 이 범위로 이해하라.

## Baseline and ownership

- 같은 worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p6-plan`,
  branch `Ssojux2/fcp-p6-plan`. **기존 `bde0368` 위에 커밋한다.** rebase·reset·squash·amend 금지.
- 허용 파일: `vlogsrt/project/edit.py`, `tests/project/test_edit_plan.py`.
- 금지: 그 밖의 모든 파일. 새 의존성·live 호출·`--online`·`VLOGSRT_OFFLINE=0` 금지.
- **기존 163개 P6 테스트의 의미를 바꾸지 마라.** 정직한 입력에서 id·후보 창·제거 봉투는
  하나도 바뀌지 않아야 한다. 바뀌면 멈추고 보고하라.

## 항목 1 — 보존과 타일링이 서로를 안 본다 (재현 A·B)

보존 검사는 **소스** 프레임을 세고 타일링 검사는 **출력**을 센다. 둘을 묶지 않아서
`RetainedInterval`의 소스 `[0,75)`가 출력 `[0,50)`으로 나가도 통과한다. 길이 0 타일은
타일링이 아예 건너뛴다.

고칠 것:
- `_instance_findings`: `RetainedInterval`마다 `len(output) == len(source)`이고 `len(source) >= 1`
- `_tiling_findings`: 길이 0 타일을 **건너뛰지 말고 finding으로**

## 항목 2 — `restore_at_output_frame`이 아무 값이나 된다 (재현 C)

`9999`를 넣어도 통과한다. 이 값은 `project_trace.json`이 복원 지점을 말하는 근거이므로
틀리면 복원 기록이 거짓이 된다.

고칠 것: 제거의 **입력 끝 이후** 첫 retained/gap 타일의 **출력 시작**과 같아야 한다.
그런 타일이 없으면 출력 끝이다.

## 항목 3 — `guard.status`를 아무도 안 본다 (재현 D·E) ★ 가장 중요

`protected=()`인 **`keep_all`** guard를 플랜 시점에 넘기면 `[75,250)`이 제거되고 검증이
통과한다. 비디오 전용 클립에 위조 파티션 + `protected=()` **`inaudible`** guard도 같다.
`build_edit_plan`도 `validate_edit_plan`도 `guard.status`를 읽지 않는다.

**이것은 `protected=[]`가 옷을 갈아입은 것이다.** P5-03b에서 guard 계층에 닫은 결함이
소비자 계층에서 재발했다 — 같은 계보의 다섯 번째다. `keep_all`과 `inaudible`은
**제거 권한이 없다**는 진술인데, 빈 `protected`를 "보호할 게 없다"로 읽으면 정반대가 된다.

고칠 것:
- `_require_plan_blocks`: 블록 인스턴스의 guard `status != "success"`면 **`raise`**
  (잠금·부재와 같은 줄이다)
- `_protection_findings`: `status != "success"`인 인스턴스에 제거가 있으면 **finding**

## 항목 4 — `request_sha256`을 적용 경로에서만 본다

`status`가 `keep_all`이거나 임계 미만인 판정에 **쓰레기 해시**를 넣어도 raise도 리뷰도 없이
`plan.decisions`·`revision`·`trace`에 들어간다. 섞인 리비전의 증거가 기록에 남는다.

고칠 것: `_require_decision_linkage`에서 **모든 판정**의 `request_sha256`을 검증한다.

## 항목 5 — `EditPlan.runs` (시그니처 부족, 또 P4-04 모양)

`retained`가 **제거가 없는 블록 경계마다 쪼개진다**(재현: 판정 없는 500프레임 클립 →
`kept_block` 2개, 보호 접두 100프레임 클립 → 3개, 전부 shift 0). 그러면 P6-03과 P6-04가
**각자 같은 병합을 구현**하게 된다 — 생산자가 쥔 것을 이음매에서 버리는 바로 그 모양이다.

고칠 것: `EditPlan.runs: tuple[RetainedRun, ...]` — 소스와 출력이 **둘 다 연속**인 같은
인스턴스 구간을 합친 것(`reasons` 튜플 보존). `validate_edit_plan`이
`runs == coalesce(retained)` 일관성을 검사한다.

테스트: 자르지 않은 클립 → run 하나; 보호 접두 → run 하나; 제거 하나 → shift가 다른 run 둘.

## 항목 6 — 판정 순서가 revision을 바꾼다

같은 내용인데 `decisions` 순서만 다르면 revision이 달라진다(재현됨).

고칠 것: 해시 전에 `plan.decisions`를 **파티션 블록 순서**로 정규화한다(안정 정렬).

## 항목 7 — "한 블록 안" 검사: 내 브리프가 틀렸다

브리프 116행이 `validate_edit_plan`의 최소 검사 목록에 "모든 removed는 한 블록 안에 있다"를
넣었는데, **내가 고정한 시그니처에 파티션이 없어 그 검사가 불가능하다.** 구현자는 build의
구조적 보증으로 대체하고 docstring에 공개 선언했다 — 그 판단이 옳다.

고칠 것은 코드가 아니라 **계약의 정직성**이다:
- `validate_edit_plan`의 docstring에 **검증기의 범위**를 명시하라: 이것은 **구조 관문**이고
  관찰 가능한 결과만 측정한다. 출처(어느 블록에서 나왔는가)는 검증기가 아니라
  **재빌드로 증명**한다.
- 그 선언 옆에 **신뢰 진술**을 적어라: 하류(P6-04)는 **같은 프로세스에서
  `build_edit_plan`이 만든 플랜만** 렌더하고 `revision`을 대조한다. 저장된 `EditPlan`을
  역직렬화해 렌더하지 않는다. 재개는 **저장된 분석에서 플랜을 다시 만든다**(명세가
  "plan/export는 저장된 분석에서 결정적"이라고 못 박았다).

브리프 116행은 **내가 고쳐 커밋한다.** 너는 docstring과 신뢰 진술만 쓰면 된다.

## Acceptance

worktree 루트에서 `/Users/ssojux2/Working/vlogsrt/.venv/bin/python`으로:

```
python -m pytest tests/project/test_edit_plan.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

불변식은 `[OK]` 줄을 **세어** 숫자로 보고하라 — **20개**여야 한다.
전체는 `bde0368`의 2,355개 + 새 테스트 이상.
`git diff --stat bde0368..HEAD`는 두 파일만.

**정직한 입력에서 revision·id·후보 창·제거 봉투가 그대로인 것을 증명하라** —
항목 6이 순서 정규화를 넣으므로 기존 픽스처의 revision이 바뀌는지 반드시 확인하고,
바뀐다면 왜 바뀌는 것이 옳은지 설명하라(못 하면 멈추고 보고하라).

## Failure route

정확한 플랜·guard·프레임 값과 기대/실제 findings를 보고하라. 기존 테스트를 고쳐 맞추지 마라.
같은 실패 2회면 멈추고 코디네이터에게 상승하라.

## Report

`worker_done` 전에 새 SHA, `git diff --stat bde0368..HEAD`, 항목 1~7 각각의 결과,
**Fable의 재현 A·B·C·D·E가 이제 닫히는지 각각 직접 확인한 출력**,
정직한 입력의 불변 증명, red-first 증거, 모든 acceptance 명령의 초록 출력과
테스트 수·불변식 센 값, 네트워크 호출 0을 보내라. 그다음 `worker_done`을 한 번 보낸다.
