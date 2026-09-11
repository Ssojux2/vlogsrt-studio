# Task P6-03b: 제거 검사를 run 포함보다 **먼저** 돌린다

## Authority

CLAUDE.md 규칙 9. 후보 `a022f96`의 보정. Fable 5.1 방향 검토
`docs/orchestration/reviews/fable-p6-03-direction.md`. 코디네이터: Opus 5. 오프라인 전용.

## 네 구현은 옳다 — 브리프의 한 단어가 빠졌다

두 검토가 붙었고 **둘 다 네 네 가지 핵심 케이스가 전부 닫힌다**고 확인했다:
자르지 않은 경계를 건너는 큐가 `runs` 덕에 살아남고, 제거를 건너는 큐는 하드 실패하고,
칼날을 걸친 소스 큐는 두 장으로 리플 뒤 인접하고, 잠긴 두 칼날의 경계 큐는 한 장으로 남는다.
신원 삼중 대조·격자·SOURCE 바이트 불변·EDIT 시계 접힘 재측정도 전부 확인됐다.
GLM 검증자는 **ACCEPT_WITH_FINDINGS**(코드 결함 0)를 냈다.

Fable이 찾은 것은 **검사 순서** 하나다. 그리고 그건 내 브리프가 안 적은 것이다.

## 결함 — 안전 논증이 조건부로만 참이다 (재현됨)

`_single_instance_shift`가 **run 포함을 먼저** 시도해 이동량을 반환하고,
`_require_not_removed`는 **어떤 run도 큐를 덮지 않을 때만** 돈다.

재현(Fable case g): 정직한 플랜에서 `runs`만 `[0,500)` 하나로 바꾸고 `removed=[75,250)`은
그대로 두면 — `validate_edit_plan`은 finding 1개를 내지만
`project_edit_captions`는 그 큐를 **그냥 내보낸다**(`00:00:02,000 --> 00:00:11,000`).
raise도, 깃발도, 리뷰도 없다.

**왜 중요한가**: `project_edit_captions`는 `validate_edit_plan`을 부를 수 없다
(시그니처에 document도 guards도 없다). 그래서 지금 P6-03의 보증은
**"일관된 플랜이 주어졌을 때 큐를 잃지 않는다"**이고, 일관성은 나중에
`render_edit_fcpxml`에서야 증명된다. 내가 P6-02 검토에서 "guard 밖 큐는 P6-03에서
하드 실패한다"고 적은 문장은 **P6-03 혼자가 최후 방어선인 것처럼** 읽히는데 지금은 아니다.

**정직한 플랜에서는 참이다** — guard가 모든 소스·경계 큐에서 바깥 반올림으로 씨를 뿌리므로
정직한 플랜은 큐 아래를 제거하지 않고, `build_edit_plan`이 만든 플랜은 `runs`와 `removed`가
서로소다. 고치는 값은 **정직한 입력에서 0**이다.

## Baseline and ownership

- 같은 worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p6-captions`,
  branch `Ssojux2/fcp-p6-captions`. **기존 `a022f96` 위에 커밋한다.** rebase·amend 금지.
- 허용 파일: `vlogsrt/project/captions.py`, `tests/project/test_edit_captions.py`.
- 금지: 그 밖의 모든 파일. `.venv` 심링크를 다시 만들지 마라 —
  인터프리터는 `/Users/ssojux2/Working/vlogsrt/.venv/bin/python` 절대 경로를 써라.
- **기존 93개 P6 테스트의 의미를 바꾸지 마라.**

## 고칠 것 — 둘뿐이다

1. **`_single_instance_shift`가 `_require_not_removed(...)`를 run 루프 *앞에서*
   무조건 부른다.** "어떤 run도 이 큐를 덮지 않는다" 진단은 **두 번째** 실패로 남긴다.
   그 밖의 동작 변화는 없다 — 일관된 플랜에서 제거 검사는 "어떤 run에도 안 들어감"의
   **부분집합**이라 기존 93개 테스트는 그대로 초록이어야 한다.

2. **위조 플랜 회귀**: `replace(plan, runs=(클립 전체 run 하나,))`로 `removed`는 그대로 둔 뒤
   제거 구간 위의 큐를 투영하면 `제거 구간과 겹친다`를 담은 `ValueError`가 나야 한다.
   이게 P6-02의 안전 문장을 **기계로 박는** 형태다.

docstring에 **왜**를 적어라: P6-03은 `validate_edit_plan`을 부를 수 없으므로
`runs`와 `removed`가 서로소라고 **가정하지 않는다**. 제거는 모든 큐에 대해 먼저 본다.

## 함께 기록할 것 (코드 아님 — 내가 한다)

접힘 비대칭을 문서에 못 박는다: SOURCE에서 접혔지만 EDIT에서 표현 가능해진 큐는
**EDIT SRT에서 빠진 채로 두고 EDIT 레벨 이슈를 남기지 않는다.** 그 판단이 옳다 —
정확한 길이가 여전히 1ms 미만이고 SOURCE 판정이 durable하다. 다만 그러면 수락을 막는 것이
`source.unresolved_cue_ids` **하나뿐**이므로, P6-04와 P7-04가 빈 EDIT 이슈 목록을
"미해결 없음"으로 읽으면 안 된다. 이건 브리프가 정하지 않았던 것이고 GLM 검증자가
LOW로 올린 긴장도 이것인데, **Fable의 판단이 옳아서 코드를 고치지 않고 기록으로 닫는다.**

## Acceptance

worktree 루트에서 `/Users/ssojux2/Working/vlogsrt/.venv/bin/python`으로:

```
python -m pytest tests/project/test_edit_captions.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

불변식은 `[OK]` 줄을 **세어** 보고하라 — **20개**. 전체는 `a022f96`의 2,391개 + 새 테스트.
`git diff --stat a022f96..HEAD`는 두 파일만, `git status`는 깨끗해야 한다(`.venv` 없이).

**정직한 입력에서 EDIT SRT 바이트가 그대로인 것을 증명하라** — 검사 순서만 바뀌므로
정직한 플랜의 출력은 하나도 달라지면 안 된다.

## Report

`worker_done` 전에 새 SHA, `git diff --stat a022f96..HEAD`, 두 항목 결과,
**위조 플랜이 이제 raise하는 출력**, 정직한 입력의 EDIT SRT 불변 증명, red-first 증거,
acceptance 초록 출력과 테스트 수·불변식 센 값, 네트워크 0을 보내라.
