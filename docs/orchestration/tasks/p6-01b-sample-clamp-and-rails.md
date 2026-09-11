# Task P6-01b: 표본 클램프·천장 레일·검사기 별명 구멍을 닫고 요청 투영을 못 박는다

## Authority

기준 명세 `Visual scoring and deterministic roughcut`. 후보 `be2c570`의 보정.
Fable 5.1 방향 검토 `docs/orchestration/reviews/fable-p6-01-direction.md`의 최소 수정 명세.
코디네이터: Opus 5 (Run `run_a091d4869a98`). 오프라인 전용.

## 네 구현은 브리프대로였다 — 셋은 진짜 결함이고 하나는 내 검사기 설계 구멍이다

방향 검토가 **SOUND_WITH_CONCERNS**를 냈다. 사전 검토가 명세한 모양
(`KeepCandidate`·`BlockPartition` 세 필드·내용 결속 id)은 **전부 그대로 착지했고**
P6-02는 나눗셈 없이 조회만 하면 된다. 재구성이 강요되는 자리는 없다.
아래는 그 위에 남은 네 가지다.

## Baseline and ownership

- 같은 worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p6-blocks`,
  branch `Ssojux2/fcp-p6-blocks`. **기존 `be2c570` 위에 커밋한다.** rebase·reset·squash·amend 금지.
- 허용 파일: `vlogsrt/project/edit.py`, `tests/project/test_block_partition.py`,
  `scripts/check_invariants.py`, `tests/test_invariants.py`,
  그리고 항목 4 때문에 `vlogsrt/project/vision/policy.py`와 `tests/project/test_vision_policy.py`
  (**P5-05 재개방을 명시적으로 승인한다** — 이름 하나를 공개로 바꾸는 것뿐이다).
- 금지: 그 밖의 모든 파일, fixtures, `golden/`, `.env`, `.env.example`.
  새 의존성·live 호출·`--online`·`VLOGSRT_OFFLINE=0` 금지.
- **기존 105개 테스트의 의미를 바꾸지 마라.** 바꿔야 하면 멈추고 보고하라.

## 항목 1 — 후보 중앙 표본이 블록 밖으로 나간다 (실제 결함, 재현됨)

`keep_frames == 1`이면(`keep_seconds = 1/25` @25fps는 모든 레일을 통과한다)
끝 후보의 중앙 표본이 `start + (1+1)//2 == end_frame`이 된다.
블록 `[0,250)`에서 인덱스 **250**, `[250,500)`에서 **500**이 나간다.

브리프가 명시적으로 말한 것을 어긴다: *"마지막 유효 프레임은 `end_frame - 1`이고
`end_frame`은 절대 아니다"*. 블록이 애셋 끝에 닿으면 P5-04가 범위 밖으로 거부한다.
지금은 **공칭(1fps) 표본만 클램프되고 후보 중앙은 안 된다.**

고칠 것: `edit.py::_sample_requests`에서 중앙을 `min(offset + (keep + 1) // 2, length - 1)`로 묶는다.
`keep_frames >= 2`의 `frame_id`는 바뀌지 않으므로 하류 신원은 흔들리지 않는다.

테스트: `keep_seconds`가 한 프레임일 때 **모든 표본 인덱스가 `end_frame` 미만**인 것.

## 항목 2 — 교차 레일이 정책의 실효 천장을 안 본다 (실제 결함, 재현됨)

교차 레일이 `cfg.vision_max_images`와 비교하는데 정책은 `_effective_max_images(cfg) = min(cfg, 16)`로
깎는다. 그래서 `ProjectConfig(block_max_seconds=20, vision_max_images=32)`가 레일을 통과한 뒤
**20초 블록이 표본 15장으로 판정**된다 — 레일이 존재하는 이유인 "성긴 이미지는 아무것도
증명하지 못한다" 바로 그 경우다.

고칠 것: `_effective_max_images(cfg)`와 비교한다(이미 import돼 있다).

테스트: `block_max_seconds=20, vision_max_images=32`가 `raise`하는 것.

## 항목 3 — 내가 설계한 검사기에 별명 우회가 있다

```python
import vlogsrt.project.vision.sampling as FrameSampleRequest   # 지금 통과한다
```

`_edit_imports`가 `ast.Import` 행에 **asname**을 기록해서 행이 허용 형태처럼 보인다.
(`from vlogsrt.project.vision import sampling as FrameSampleRequest`는 올바르게 거부된다.)

고칠 것: `ast.Import` 행에는 `alias.name`(모듈 경로)을 기록한다. 그러면
`names != ("FrameSampleRequest",)`에서 걸린다.

테스트: 위 형태가 **거부되는지**. 기존 허용목록 회귀 셋은 그대로 둔다.

## 항목 4 — 요청 투영을 한 곳에 못 박는다 (P5-06이 항목 1을 한 이유)

P5-06이 `VisionDecision.request_sha256`을 실었다. P6-02가 그걸 대조하려면
**`EditBlock` → `VisionRequest` 투영이 정본으로 한 번만 정의돼야** 한다 —
러너(P7)와 검증자(P6-02)가 **같은 재료를 해시해야** 하기 때문이다.

- `edit.py`에 `vision_request_for(block, adjacent_context) -> VisionRequest`를 둔다:
  `frame_ids = tuple(r.frame_id for r in block.sample_requests)`,
  `candidates = tuple(k.vision for k in block.keep_candidates)`.
- `policy.py`의 `_request_sha256`을 **공개 `request_sha256`으로 바꾼다**(이름 변경뿐,
  재료·계산은 그대로). 내부 호출부도 함께 고친다.
- 테스트: `vision_request_for`가 만든 요청의 `request_sha256`이 같은 블록에 대해
  **결정적으로 같은 값**을 내는 것, 그리고 블록이 달라지면 달라지는 것.

P6-02 브리프에 "불일치 → `raise`" 규칙을 넣는 것은 **내가 한다**. 너는 투영과 공개 이름만 낸다.

## 하지 않을 것

Fable이 제안한 "두 프레임 dataclass를 행동 없는 모듈로 옮겨 허용목록 예외를 지우는 것"은
**이번 태스크가 아니다.** 후속 태스크로 따로 다룬다 — 지금 하면 P6-01의 범위를 넘고
`policy`·`sampling` 양쪽 import를 건드린다.

## Acceptance

worktree 루트에서 `/Users/ssojux2/Working/vlogsrt/.venv/bin/python`으로:

```
python -m pytest tests/project/test_block_partition.py
python -m pytest tests/test_invariants.py tests/project/test_vision_policy.py
python -m pytest tests/project
python -m ruff check vlogsrt ui tests scripts
python -m ruff format --check vlogsrt ui tests scripts
python -m mypy
python scripts/check_invariants.py
python -m pytest
```

불변식은 요약을 믿지 말고 `[OK]` 줄을 **세어** 숫자로 보고하라 — **20개**여야 한다.
전체 스위트는 `be2c570`의 2,293개 + 새 테스트 이상이어야 한다.
`git diff --stat be2c570..HEAD`는 허용된 파일만 나열해야 한다.

**기본 설정에서 `block_id`·`candidate_id`·`frame_id`가 바뀌지 않는 것을 증명하라** —
항목 1~3은 기본값 경로를 건드리지 않아야 한다.

## Failure route

정확한 설정값·프레임 인덱스·기대/실제를 보고하라. 기존 테스트를 고쳐 맞추지 마라.
같은 실패 2회면 멈추고 코디네이터(Opus 5)에게 상승하라.

## Report

`worker_done` 전에 새 SHA, `git diff --stat be2c570..HEAD`, 항목 1~4 각각의 결과,
기본 설정 id 불변 증명, red-first 증거, 모든 acceptance 명령의 초록 출력과
테스트 수·불변식 센 값, 네트워크 호출 0을 보내라. 그다음 `worker_done`을 정확히 한 번 보낸다.
