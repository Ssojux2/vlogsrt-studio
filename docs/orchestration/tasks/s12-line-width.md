# Task S12: 공백 없는 자막의 줄 폭을 16자 안에 둔다

## Authority

- 근거: `TASKS.md`의 `## S12 — 줄 폭 16자를 지키게 한다` 절.
- 이 계획은 확정됐다. 구현자는 새 설계를 더하거나 결정된 범위를 다시 논의하지 마라.
- 관리자: Sol. 검토와 최종 통합: 코디네이터. 제품 코드는 GLM만 수정한다.
- Orca task/dispatch ID는 구현 워커를 배정할 때 발급한다. 브리프 작성 시점에는 미발급이다.
- 이 작업은 규칙 9에 따라 폭 초과를 조용히 넘기지 않는다. 계약 변경의 근거는
  `docs/DECISIONS.md`에 새 결정으로 남긴다.

## Baseline and ownership

- exact base: `569aecb`.
- 새 Orca 자식 worktree:
  `/Users/ssojux2/orca/workspaces/vlogsrt/s12-line-width`.
- branch: `Ssojux2/s12-line-width`.
- 자식 worktree와 branch는 모두 exact base `569aecb`에서 만든다. 다른 branch의 미통합
  커밋을 바탕으로 만들지 마라.
- 허용 파일:
  - `vlogsrt/refine.py`
  - `vlogsrt/layout.py`
  - `vlogsrt/pipeline.py`
  - `tests/test_refine.py`
  - `tests/test_invariants.py`
  - `docs/DECISIONS.md`
- `vlogsrt/config.py`와 `vlogsrt/project/**`는 다른 레인이 소유한다. 읽을 수는 있지만
  수정하지 마라.
- `.env`는 읽거나 수정하지 마라. `tests/fixtures/api/**`와 `golden/**`는 수정하지 마라.
  `tests/fixtures/api/fr_day_1_2_fhd.whisper.json`과 같은 이름의 메타 픽스처는 아래
  오프라인 회귀 측정에서 읽기만 한다.
- `Config` 필드를 추가하지 마라. 필드가 늘면 `_config_sha256`이 바뀌어
  `vlogsrt/project/`의 분석 키가 전부 깨지고 다른 레인과 충돌한다.
- git은 `/Library/Developer/CommandLineTools/usr/bin/git`을 쓴다. Python은
  `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python`을 쓴다.
- `make`는 금지한다. 이 머신에서는 Xcode 라이선스 오류로 실패한다.

## Contract

- `max_chars_per_line = 16`, `max_lines = 2`를 그대로 둔다. 다른 임계값도 바꾸지 마라.
- `refine._char_split_index(text, ratio)`는 공백이 있으면 지금처럼 비율에 가장 가까운
  어절 경계를 돌려준다. 공백이 없으면 `None` 대신 같은 글자 비율의 유효한 내부
  인덱스를 돌려준다.
- `split_cue`의 나머지 계약은 바꾸지 마라. 시각은 글자 수에 비례해 나누고 원본 큐
  구간을 벗어나지 않는다. `max_split_depth`, `min_duration`, `min_gap`도 그대로 지킨다.
- `layout.break_text`에서 공백 없는 본문의 강제 분리 기준은 다음 식이다.

  ```text
  cut = min(max_chars_per_line, ceil(len(body) / 2))
  ```

  공백이 있는 본문의 후보·점수 경로와 가운데 균형은 그대로 둔다. 강제 분리에는
  기존 `forced_break` 플래그를 남긴다.
- `pipeline._make_cues`는 `refine.refine(...)`이 만든 줄 가운데 길이가
  `max_chars_per_line`을 넘는 줄을 센다. 하나 이상이면 줄 수를 담은 `WARN`을 정확히
  한 줄 남긴다. 큐 수가 아니라 줄 수를 센다.
- 16자는 목표 폭이다. 어절 경계 때문에 `17/14`가 되는 정상 문장을 두 큐로 쪼개
  절대 한도로 만들지 마라.
- 공백 없는 본문은 글자 중간에서 나뉘어도 이어 붙인 글자가 원문과 같아야 한다.
  병합이 새 공백을 넣거나 글자를 잃으면 안 된다.
- 계약 변경은 한 건뿐이다.
  `tests/test_refine.py::test_split_gives_up_on_a_single_long_word`가 `'가' * 40`을
  쪼개지 않고 `no_split_point`를 남기던 기대값을 새 분할 계약에 맞게 바꾼다.
  기대값 수정 금지 규칙의 유일한 예외이며, 이유를 `docs/DECISIONS.md`에 기록한다.

## Implementation steps

1. `tests/test_refine.py`에 실패 테스트를 먼저 쓴다.
   - 공백 없는 72자, 6.8초 큐가 여러 큐로 나뉘고 모든 줄이 16자 이하임.
   - 공백 없는 26자, 1.86초 큐가 13자씩 두 큐로 나뉨.
   - 공백 없는 20자, 3.0초 큐가 한 큐의 `10/10` 두 줄이 됨.
   - 분할 조각을 순서대로 이어 붙이면 원문과 정확히 같음.
   - 어절 경계가 맞지 않는 한글 31자 큐는 한 큐의 `17/14`로 그대로임.
   - 시간이 짧아 나눌 수 없는 큐의 폭 초과 줄에는 플래그가 남음.
2. `test_split_gives_up_on_a_single_long_word`의 기대값을 새 계약으로 바꾼다. 단언을
   삭제하거나 느슨하게 만들지 마라.
3. `refine._char_split_index`의 공백 없는 입력만 고친다. `split_cue`의 시각 계산과
   재귀·최소 길이 제한은 건드리지 마라.
4. `layout.break_text`의 공백 없는 강제 분리 기준만 위 식으로 바꾼다.
   기존 30자 입력은 `16/14`에서 `15/15`로 바뀐다.
   `test_linebreak_forces_a_split_without_spaces`는 기존 단언을 유지한다.
5. `pipeline._make_cues`에 폭 초과 줄 집계와 단일 `WARN`을 넣고 로그 회귀 테스트를
   추가한다.
6. `tests/test_invariants.py`에서 실측 픽스처 전체를 실행한다. 16자 초과 줄마다
   폭 초과 플래그가 있음을 단언한다.
7. `docs/DECISIONS.md`에 계약 변경의 근거를 새 결정으로 남긴다. 공백 없는 긴 본문을
   포기하던 동작이 화면 밖 줄을 만들었고, 임계값 변경 없이 글자 비율 분할로 고친다는
   사실을 기록한다.

## Acceptance

- 기대하는 대표 결과:

  | 입력 | 결과 |
  |---|---|
  | `ㅋ` 72자 / 6.8초 | 4큐, 각 큐 `9/9` |
  | `ㅋ` 26자 / 1.86초 | 2큐, 각 큐 13자 |
  | `ㅋ` 20자 / 3.0초 | 1큐, `10/10` |
  | 어절 경계가 맞지 않는 한글 31자 / 6.2초 | 1큐, `17/14` 유지 |

- 실측 회귀: exact base `569aecb`에서
  `fr_day_1_2_fhd.whisper.json`을 끝까지 처리한 큐의 시각과 글자를 저장소 밖에
  기준값으로 잡는다. 수정 뒤 같은 입력의 큐 수·순서·시각·글자가 모두 같아야 한다.
  네트워크를 부르거나 픽스처를 갱신하지 마라.
- 먼저 관련 검사를 통과시킨다.

  ```bash
  cd /Users/ssojux2/orca/workspaces/vlogsrt/s12-line-width
  PYTHONPATH=$PWD /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python -m pytest tests/test_refine.py tests/test_invariants.py
  ```

- 전체 게이트는 아래 다섯 개다. `make`로 대신하지 마라.

  ```bash
  PYTHONPATH=$PWD /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python -m ruff check vlogsrt ui tests scripts
  PYTHONPATH=$PWD /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python -m ruff format --check vlogsrt ui tests scripts
  PYTHONPATH=$PWD /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python -m mypy
  PYTHONPATH=$PWD /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python -m pytest
  PYTHONPATH=$PWD /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python scripts/check_invariants.py
  ```

- 통합 시점 기준선은 Ruff format 99개 파일, mypy 47개 파일, pytest 2531개 통과,
  불변식 21개 `[OK]`다. 새 테스트 때문에 pytest 수는 늘 수 있다. 각 명령의 실제
  결과와 종료 코드를 보고한다.
- `.env`를 읽거나 수정하지 마라. `--online`과 유료 API 호출은 금지한다.

## Failure route

- 실패하면 재현 명령, 종료 코드, 짧은 로그, 예상값과 실제값, exact base, 변경 파일,
  시도한 가설을 기록한다.
- 같은 실패가 두 번 반복되면 Sol이 Terra에는 구조·시각·타입 원인을, Luna에는
  최소 재현과 테스트 증거를 독립 요청한다.
- 두 진단을 합쳐 GLM 수정 태스크로 돌린다. 새 증거 없이 같은 프롬프트를 반복하지 마라.
- 그래도 해결되지 않으면 Opus5가 최소 수정 설계를 결정한다. 원본 불변 계약이나 제품
  범위의 판단이 필요하면 Astra에 올린다.
- 모든 제품 코드 수정은 GLM이 한다. 관리자와 진단 워커는 제품 코드를 수정하지 않는다.

## Report

- exact base, branch, Orca worktree, Orca task/dispatch ID를 적는다.
- 커밋 SHA와 변경 파일을 적는다. 기대값을 바꾼 테스트는 파일과 테스트 이름, 바꾼 이유를
  따로 적는다.
- `_char_split_index`, `break_text`, `_make_cues`의 변경 위치와 대표 입력별 실제 결과를
  적는다.
- 실측 픽스처의 수정 전후 큐 수와 시각·글자 비교 결과를 적는다.
- 다섯 게이트의 명령, 종료 코드, 실제 파일·테스트·불변식 수를 적는다.
- 남은 결함과 멈춘 항목을 적는다. 없으면 `없음`이라고 쓴다.
- 구현 커밋 뒤 정확한 SHA와 검증 증거를 review-ready 상태로 보고한다. Orca ask로 Sol의
  검토 판정을 받은 뒤 범위 안 지적을 같은 Dispatch에서 고치고 관련 검사를 다시 돌린다.
- 검토 승인 뒤에만 `worker_done`을 보낸다. branch는 ready-to-integrate로 보고하되 직접
  merge하거나 push하지 마라.
