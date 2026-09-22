# Task P5-06: 판정에 요청 신원을 싣고 설정 우회를 생성자에서 닫는다

## Authority

기준 명세 `Visual scoring and deterministic roughcut`, `Persistence, CLI and UI`.
Fable 5.1의 P6 사전 검토 `docs/orchestration/reviews/fable-p6-prephase.md`의 **상류 부족 1·2**.
코디네이터: Opus 5 (Run `run_a091d4869a98`). 오프라인 전용.

P6가 시작되기 전에 상류를 고칠 **마지막 기회**다. 지금 안 하면 P6 네 태스크 전부가
우회 방어를 각자 다시 구현해야 한다.

## 왜 지금인가

이 프로젝트는 **같은 결함을 세 번** 만났다.

- P5-05b: `ProjectConfig`를 프로그램식으로 만들면 `load_project_config`의 검증을 건너뛴다.
  `max(nan, 0.8)`가 nan이라 신뢰도 0.5가 `accepted`로 승인됐다. → `policy.py`에 `_require_config`.
- P5-03b: 같은 우회. `guard_padding_seconds=-1/2`가 `protected=[]`를 `status="success"`로
  냈다. → `guard.py`에 또 `_require_config`.
- P6: 사전 검토가 `keep_seconds=0`이면 **클립이 통째로 삭제**됨을 확인했다. → 또 필요.

**세 계층이 같은 검사를 각자 다시 짜고 있다.** 생성자에서 닫으면 한 번에 끝난다.

## Baseline and ownership

- 새 worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p5-upstream`,
  branch `Ssojux2/fcp-p5-upstream`, 기준은 이 태스크 디스패치 시점의 `feat/final-cut-project` 팁.
- 허용 파일: `vlogsrt/project/config.py`, `tests/project/test_project_config.py`,
  `vlogsrt/project/vision/policy.py`, `tests/project/test_vision_policy.py`.
  **항목 2의 파급으로 `tests/project/test_guard.py`가 깨지면 그 파일도 허용한다** —
  단 **기존 단언의 의미를 바꾸지 말고**, 어쩔 수 없이 의미가 바뀌어야 하면
  **고치지 말고 멈추고 보고하라.**
- 금지: 그 밖의 모든 파일, fixtures, `golden/`, `.env`, `.env.example`.
  새 의존성·live 호출·`--online`·`VLOGSRT_OFFLINE=0` 금지.

## 항목 1 — `VisionDecision.request_sha256`

`parse_decision(request, raw, cfg)`는 `request`를 손에 쥐고 있다. `VisionDecision`에
`request_sha256: str`를 실어라. 재료는 `block_id`, `frame_ids`(순서 포함),
후보 `candidate_id`와 그 창(`keep_start`/`keep_end`를 `_exact` 같은 정확 직렬로).

- **모든 경로에서 채운다** — `_normalize`(accepted·keep_all)와 `_error_decision` 둘 다.
- 도메인 구분자를 두고 이미 있는 해시 idiom을 따른다.
- 왜: P6-02가 저장된 판정이 **이 프레임과 이 후보를 실제로 봤는지** 증명할 수 있어야 한다.
  지금은 `block_id`와 `choice`뿐이라, 다른 기하에서 만들어진 판정이 id 규율만 맞으면
  통과한다. P7이 분석을 재사용해 resume하므로 실재하는 경로다.

## 항목 2 — `ProjectConfig.__post_init__`

`ProjectConfig`는 frozen dataclass인데 검증이 `load_project_config` 경로에만 있다.
`__post_init__`에서 **필드별 범위 파서와 `_validate_cross`를 그대로 돌려라** — 새 규칙을
발명하지 말고 이미 있는 것을 재사용한다. 위반은 키와 값을 대고 `ValueError`.

**주의해서 볼 것:**

- 기존 소비자 테스트(`test_vision_policy.py`, `test_guard.py`)는 나쁜 설정을 만들어
  **소비자가** 거부하는 것을 확인한다. 이제 생성 시점에 터진다. 대부분은 `replace(...)`가
  `pytest.raises` 블록 **안**에서 평가되고 메시지가 키를 담으므로 그대로 통과할 것이다.
  통과하지 않는 것이 있으면 **의미를 바꾸지 말고 보고하라.**
- 소비자의 `_require_config`는 **지우지 마라.** 프레임 환산된 값까지 보는 검사가 있고,
  방어는 겹쳐도 된다. 이 태스크는 층을 하나 더 까는 것이지 기존 층을 걷는 게 아니다.

## 테스트

- 항목 1: 같은 요청·다른 요청이 각각 같은/다른 `request_sha256`를 내는 것,
  `frame_ids` 순서가 바뀌면 달라지는 것, 후보 창이 바뀌면 달라지는 것,
  **accepted·keep_all·error 세 경로 전부**에 실리는 것.
- 항목 2: 각 필드의 NaN·무한대·범위 밖·`bool`·잘못된 타입이 생성에서 닫히는 것,
  교차 검사 위반이 닫히는 것, **정상 설정은 그대로 만들어지는 것**(이게 없으면
  "전부 막혔다"가 "생성자가 망가졌다"와 구별되지 않는다).
- 기존 테스트는 하나도 의미가 바뀌면 안 된다.

## Acceptance

worktree 루트에서 `/Users/ssojux2/Working/vlogsrt/.venv/bin/python`으로:

```
python -m pytest tests/project/test_project_config.py
python -m pytest tests/project/test_vision_policy.py tests/project/test_guard.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

불변식은 요약을 믿지 말고 **`[OK]` 줄을 세어** 숫자로 보고하라 — **19개**여야 한다.
전체 스위트는 기준의 2,094개 + 새 테스트 이상이어야 한다.

## Failure route

정확한 필드·값·기대/실제를 보고하라. 기존 테스트를 고쳐 맞추지 마라.
같은 실패 2회면 멈추고 코디네이터(Opus 5)에게 상승하라.

## Report

`worker_done` 전에 SHA, `git diff --stat <base>..HEAD`, 항목 1·2 각각의 처리 결과,
**기존 테스트가 깨졌는지와 어떻게 다뤘는지**, red-first 증거, 모든 acceptance 명령의
초록 출력과 테스트 수·불변식 센 값, 네트워크 호출 0을 보내라. 그다음 `worker_done`을
정확히 한 번 보낸다.
