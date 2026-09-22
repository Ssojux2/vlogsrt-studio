# S12·PROTO-6b 브리프 검토 (Opus 5, read-only) — 후보 `9fb6f08`, base `569aecb`

판정: **SOUND_WITH_ONE_FIX.** S12 브리프는 그대로 배정해도 된다. PROTO-6b 브리프에
숫자 하나가 낡았다.

## 검증한 것

□ 계약이 코드와 맞는가
  ○ `_char_split_index`는 `refine.py:52-58`, `split_cue`는 `61-94`. 공백이 없을 때
    `None`을 돌려주고 `split_cue`가 `NO_SPLIT_POINT`로 포기하는 경로가 실재한다
  ○ `break_text`는 `layout.py:60-80`. 공백 없는 본문을 `max_chars_per_line`에서
    한 번 끊고 나머지를 2줄에 붓는다. 브리프의 서술과 일치한다
  ○ `_make_cues`는 `pipeline.py:351-358`. 8줄짜리 얇은 래퍼라 집계 한 줄을 넣기 좋다

□ 기대 결과표가 자체 모순이 없는가 — 넷 다 현재 임계값으로 재현된다
  ○ `ㅋ` 72자: `max_chars_per_cue = 32`를 넘어 재귀 분할 → 18자 큐 4장 → 줄 `9/9`
  ○ `ㅋ` 26자 / 1.86초: 글자는 32 이내지만 `26 / 1.86 = 14.0`이 `max_cps = 12.0`을
    넘어 분할 → 13자씩 2장
  ○ `ㅋ` 20자 / 3.0초: 글자·속도·길이 모두 한도 안이라 분할 없음 → 한 장의 `10/10`
  ○ 한글 31자: 어절 경계가 맞아 기존 경로를 타고 `17/14` 유지

□ 브리프가 지목한 테스트가 실제로 그런가
  ○ `test_linebreak_forces_a_split_without_spaces`(`tests/test_refine.py:301-305`)는
    줄 수와 `forced_break` 플래그만 단언한다. `16/14`를 단언하지 않는다
  ○ 따라서 결과가 `15/15`로 바뀌어도 단언을 고칠 필요가 없다는 브리프의 4단계는 옳다
  ○ `test_split_gives_up_on_a_single_long_word`(`:57-62`)는 `'가' * 40`이 한 장으로
    남고 `no_split_point`가 붙는 것을 못 박는다. 이 포기가 고치려는 결함 자체이므로
    기대값 변경이 불가피하다. 브리프가 근거 기록을 요구한 것도 옳다

□ 픽스처 경로가 실재하는가
  ○ `tests/fixtures/api/fr_day_1_2_fhd.whisper.json` 존재. 읽기만 하라는 제한도 맞다

□ 레인 격리
  ○ `Config` 필드 추가 금지가 브리프에 명시됐다. 이것이 두 레인을 가르는 유일한
    의미 충돌이다. `_config_sha256`이 `cfg.to_dict()` 전체를 해시하므로 필드가 늘면
    `project/`의 분석 키가 전부 깨진다
  ○ 소유 파일에 `vlogsrt/config.py`와 `vlogsrt/project/**`가 빠져 있다. 옳다

## 고칠 것 1건

□ `docs/orchestration/tasks/proto-6b-failed-analysis-blocks.md` 2행
  ○ base를 `92c4ddf` → `569aecb`로 바꾸면서 같은 줄의 `pytest 2500 passed`를 남겼다
  ○ `569aecb`의 실측은 **2531 통과**다. 통합 직후 직접 쟀다
  ○ 구현 워커가 2500을 기준선으로 읽으면 31개 차이를 회귀로 오인한다
  ○ 그 숫자만 고친다. 다른 내용은 건드리지 않는다

## 남기는 관찰 — 결함은 아니다

□ `_candidates`(`layout.py:42-43`)와 `_char_split_index`(`refine.py:52-58`)는 같은
  공백 탐색을 두 번 구현하고 있다. 이번 태스크는 두 곳을 각각 고치므로 중복이 남는다
  ○ 합치는 것은 이번 범위 밖이다. 조판 계층과 정제 계층의 경계를 바꾸는 일이라
    별도 판단이 필요하다. 여기 적어 두고 넘어간다
