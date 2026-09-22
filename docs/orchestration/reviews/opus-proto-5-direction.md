# PROTO-5 방향 검토 — (A) 컨텍스트 분리 Opus

> 2026-09-16. `claude -p --model claude-opus-5 --effort high --permission-mode plan`, 읽기 전용.
>
> **판정 UNSOUND.** 브리프 §3-a의 처방이 `--offline`을 지키지 못하고,
> 종료 코드 표에 필요한 값이 러너 반환 타입에 없다고 지적했다.
> **반영 완료 — 브리프 v2 §9.**

Permission deny rule (.claude/settings.json): Write(./golden/**) is not matched by file permission checks — only Edit(path) rules are. Use Edit(./golden/**) instead (Edit rules cover all file-editing tools).
Permission deny rule (.claude/settings.json): Write(./tests/fixtures/api/**) is not matched by file permission checks — only Edit(path) rules are. Use Edit(./tests/fixtures/api/**) instead (Edit rules cover all file-editing tools).
방향 판정: **UNSOUND** (브리프 그대로라면). 구조(단일 모듈 `commands.py`, `cli.py`는 파싱과 출력만)는 옳다. 다만 비용 경계에 대한 브리프의 핵심 처방(§3-a)이 `--offline`을 지키지 못하고, 종료 코드 표를 만드는 데 필요한 값이 러너 반환 타입에 없다. 둘 다 착수 전에 브리프 문구를 고치면 해결된다.

```
방향 판정: UNSOUND
```

**시그니처 충분성**
- **`final_accepted`와 blockers가 러너 반환값에 없다.** 계산은 `export.py:685` `_review_payload`(비공개 `_final_blockers`, `:707`) 한 곳에서만 하고, 결과는 `review.json` 파일에만 쓴다(`:690-691`). `PublishedProject`(`runner.py:670` 부근)와 `ExportPackage`(`export.py:108-116`)에는 경로와 리비전만 있다. 그러니 `commands.py`가 종료 코드 0/1을 정하려면 둘 중 하나밖에 없다.
  - (a) `_final_blockers`를 import해서 다시 계산한다. 비공개 함수를 쓰는 재유도이고, P4-04와 같은 모양이다.
  - (b) `package.review`의 JSON을 다시 읽는다.
- 브리프는 둘 중 무엇을 쓸지 말하지 않는다. 러너는 범위 밖이므로 **(b)를 명시하고 (a)를 금지해야 한다.** 판정 수(`decisions`)와 제거 구간 수(`plan.removed`)는 반환값으로 닿는다.
- **`-o OUT` 하나로 인자 두 개를 채워야 한다.** `prepare_audio`의 `artifact_dir`(PCM·프레임, `runner.py:207-214`)와 `publish_project`의 `destination`(리비전과 `current` 포인터, `export.py:621`)이다. 같은 디렉터리를 쓸지 하위 디렉터리로 나눌지 브리프에 없다. 구현자가 임의로 정하게 된다.

**부재가 권한이 되는 자리** — 일곱 번째 후보 둘

1. **base URL이 없으면 공급자가 조용히 바뀐다(재현함).** 기본 설정은 `vision_model="gemini-3.8-flash"`, `vision_base_url_env="GEMINI_BASE_URL"`이다(`config.py:87-89`). `GEMINI_BASE_URL`이 비어 있으면 `create_vision_client(base_url=None)`이 공개 OpenAI 주소로 간다(`transcribe.py:459-461`).
   - 재현 출력: `R3 base_url=None -> https://api.openai.com/v1/`
   - 결과: `--online`이면 Google 키가 OpenAI로 전송된다. "base URL이 없다"가 "OpenAI를 쓴다"로 읽히는 것이다.
   - 브리프 §3-c는 키 이름만 검사하고 base URL이 없는 경우를 정의하지 않는다.
2. **`--offline`과 무플래그가 유료 STT를 열 수 있다(재현 못 함, 코드로만 확인).** 브리프 §3-a는 `get_transcriber(cfg, online=online)`을 "그대로 써라"라고 한다. 그런데 `transcribe.py:399`는 `force_offline or (not online and env.is_offline())`이다. 셸에 `VLOGSRT_OFFLINE=0`이 있으면 `--offline`을 쳐도 `online=False`가 되고, `require_api_key(force=False)`가 키를 돌려준다(`env.py`). 결과는 `LiveTranscriber`, 즉 유료 전사다. 비전은 닫혀 있는데 전사만 유료로 돈다.
   - 레거시는 이 구멍을 `force_offline`으로 막는다(`cli.py:185`, `pipeline.py:196-199`).
   - 브리프가 "이 함수가 비용 안전장치다"라고 단정한 부분이 바로 틀린 부분이다.

**계약·불변식·CLAUDE.md 규칙**
- **D35와 규칙 9에 긴장이 있다.**
  - 자동 컷은 `status=="accepted"`이고 `confidence ≥ vision_apply_confidence`이면 사람 검토 없이 적용된다(`edit.py:1252`). 이것은 blockers에 걸리지 않는다. 허용 코드 집합은 `{"unavailable_declared_no_audio"}` 하나이고(`export.py:87`), 미검토 컷 코드는 애초에 없다.
  - 그래서 브리프의 "종료 코드 0 = 성공"은 모델이 자른 컷 위에 EDIT 자막을 얹고 사람 확인 없이 초록을 내는 경로가 된다. 규칙 9의 "자동 컷의 미검토 상태는 후속 자막 생성으로 해제되지 않는다"에 반한다.
  - 계획 문서 §C(`plans/…:171`, "`review_required`에서 멈추거나 명시적 draft만")와 §4(`:321`, "`--draft`로 명시적 초안만")와도 어긋난다. `run`이 임시라고 적는 것만으로는 부족하다. 출력과 종료 코드의 의미가 그 사실을 실어야 한다.
- **계약 §1 / 불변식 18은 문제없다(재현함).** 분류기 결과:
  - 통과: `from .project import commands`, `from .project.commands import project_run`, `import vlogsrt.project.commands as c`
  - 거부: `from .project import runner`, `from .project.commands.parser import x`, `from .project import commands, runner`
  - 기준 `[OK]` 20줄.
- **§4·§5·§7:** 브리프를 따르면 깨지지 않는다.

**의존성·순서**
- **종료 코드 표가 기존 `main`과 충돌한다.**
  - `main`은 `RuntimeError`를 받아 1을 돌려준다(`cli.py` 끝).
  - 파생 계층 예외 대부분이 `RuntimeError` 하위다. `UnsupportedProject`, `VadUnavailable`, `XmlValidationUnavailable`, `FrameSamplingError`, `MalformedTranscript`, `EmptyTranscript`가 모두 그렇다(재현: MRO 출력).
  - `ProjectConfigError`는 `ValueError`이고, 잡히지 않은 `ValueError`의 프로세스 종료 코드는 1이다(재현: `exit=1`).
  - 따라서 project 핸들러가 예외를 스스로 2로 바꾸지 않으면 **실패와 막힌 패키지가 둘 다 1이 된다.** 브리프가 금지한 바로 그 상황이다. `main`을 고치면 레거시 동작이 바뀌므로, 핸들러 안에서 처리해야 한다.
- **`--online --offline` 거절도 같은 문제다.** `_resolve_online`은 `RuntimeError`를 던지므로 1이 된다. 그런데 `commands.py`는 `cli.py`를 import할 수 없다(계약 §1 목록에 `cli`가 없다). 그러면 이 검사는 `cli.py`에 있어야 하는데, 종료 코드는 브리프 표와 맞지 않는다.
- **순서:** STT 자격증명 실패(`get_transcriber`)도 `prepare_audio` 앞에서 나야 한다. 브리프는 비전 키만 "시작 전"으로 못 박았다.
- **비용 확인이 빠졌다.** 레거시 `run`에는 `_confirm_cost`와 `--yes`가 있다(`cli.py:140-154`). 브리프의 `--online` 경로에는 길이 안전장치가 없는데, 그 사실이 명시되어 있지 않다. 계획상 비용 추정은 `inspect`의 몫이다(`plans/…:315`). 그렇다면 브리프에 "안전장치 없음"을 적거나 `cost_guard_seconds`를 적용해야 한다.

**소유 파일로 가능한가:** 가능하다. 단, `final_accepted`를 `package.review` JSON에서 읽는다는 조건이 붙는다. 러너나 export를 고칠 필요는 없다.

**코디네이터 주장 검토**
- §3-a "`get_transcriber` 그대로 써라, 이것이 비용 안전장치다" — **반박.** `transcribe.py:399` 때문에 `--offline`이 무시될 수 있다. `force_offline=not online`을 함께 넘겨야 한다.
- §3-c "`vision_api_key_env`가 빈 값이면" — **반박.** 이 조건은 도달 불가능하다. 재현: `ProjectConfig(vision_api_key_env="")`는 `ProjectConfigError`("비어 있으면 안 된다")를 던지고, `__post_init__`이 파서를 돌린다(`config.py:313`). 실제로 검사할 것은 `env.get(name).strip() == ""`이다. 테스트 2는 이름은 그대로 두고 그 환경 변수를 비우는 형태여야 한다.
- §6 테스트 4 "`env.require_api_key`를 부르지 않는다(부르면 실패하는 스텁)" — **반박(자기모순).** `--online` 경로의 `get_transcriber(online=True)`가 STT용으로 `require_api_key`를 **반드시** 부른다(`transcribe.py:402`). 이 단언은 비전 자격증명 해결 함수로 범위를 좁혀야 한다. 아니면 전사기를 스텁으로 갈아끼운 상태에서만 성립한다.
- §3-d "오프라인이면 blockers `['decision_error']`" — **부분 반박(재현 못 함).** 블록이 0개인 프로젝트는 판정이 없으므로 오프라인이어도 `final_accepted=True`, 종료 코드 0이다. `export.py:716-719`가 "블록 없는 프로젝트도 정상"이라고 스스로 적고 있다. 테스트 1의 합성 입력에는 블록이 하나 이상 있어야 한다.
- §4 "DTD 경로가 `None`인 경우가 유일한 신호" — **확인.** `validate_xml`에서 `dtd_checked`는 곧 `dtd_path is not None`이다(`xml_validation.py:71-76`).
- §2 불변식 18 표 — **확인**(위 재현 참고).

**최소 수정 명세** (심각도 순)
1. §3-a를 `online = <--online 여부>; transcriber = get_transcriber(cfg, online=online, force_offline=not online)`로 바꾼다. 테스트를 하나 추가한다: `VLOGSRT_OFFLINE`이 거짓이라는 조건을 **스텁으로** 흉내 내고(`env.is_offline`을 몽키패치해 False 반환), `--offline`과 무플래그 둘 다 `FixtureTranscriber`가 나오는지 본다. 환경 변수를 실제로 0으로 두면 안 된다.
2. `--online`이면 `vision_base_url_env`의 **값**도 비어 있으면 안 된다. 비어 있으면 파이프라인 전에 종료 코드 2로 멈추고, 오류 문구에는 이름만 싣는다. 이 조건으로 테스트를 추가한다.
3. `final_accepted`와 blockers는 `package.review` JSON에서만 읽는다. `_final_blockers`를 import하거나 다시 계산하는 것은 금지한다.
4. project 핸들러가 `Exception`을 잡아 `log.error`를 남기고 2를 돌려준다(레거시 `main`은 건드리지 않는다). `--online/--offline` 거절은 `run` 서브파서의 `add_mutually_exclusive_group`으로 처리한다. argparse 종료 코드가 2이므로 실패 코드와 맞는다.
5. `plan.removed`가 비어 있지 않으면 종료 코드와 상관없이 "자동 컷 N건 — 사람 검토 없음(초안)"을 눈에 띄게 출력한다. `final_accepted`를 "승인"이라고 번역하지 않는다. 계획 §4에 맞추려면 `--draft` 플래그를 필수로 하는 방안도 검토한다.
6. `-o`를 `artifact_dir`과 `destination` 중 어디에 쓸지(예: `OUT/artifacts`와 `OUT`) 명시한다.
7. 온라인 비용 안전장치를 둘지, 없다는 사실을 브리프에 적을지 결정한다.

**재현하지 못한 것**
- `VLOGSRT_OFFLINE=0` 아래에서 `--offline`이 `LiveTranscriber`를 만드는지. 헌장이 그 설정을 금지하고, 시도도 거부됐다. 코드 읽기(`transcribe.py:399-402`)로만 주장한다.
- 블록 0개 오프라인 실행의 종료 코드 0. `export.py` 문서와 blockers 로직으로 추론했을 뿐 실제로 돌리지 않았다.
- 비전 인증 실패 시 SDK 예외 문구에 키 일부가 섞여 `_failure_decision`의 reason(`runner.py`의 `_failure_decision`)을 거쳐 `review.json`이나 trace에 남는지. 네트워크가 필요해서 확인하지 못했다. 이 경로는 러너 쪽이라 이 태스크 범위 밖이지만, "키 값은 trace에 싣지 않는다"는 요구와 직접 닿는다.
- 불변식 18 뮤테이션을 실제 `cli.py`에 넣는 것. 저장소 수정이 금지돼 있어 분류기 함수만 문자열로 검사했다.
