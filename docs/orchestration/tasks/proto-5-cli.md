# PROTO-5 — `project/commands.py` + `cli.py`: 비용 경계가 사는 자리

> **v2.** v1은 (A) Opus **UNSOUND**, (B) GLM SOUND_WITH_CONCERNS. 두 보고서는
> `../reviews/{opus,glm}-proto-5-direction.md`. 뒤집힌 내 주장은 §9.

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 **`8ca214d`** (트리 깨끗, pytest 2488 passed)

**Xcode 라이선스 미동의로 툴체인 shim이 죽어 있다**(exit 69) — `git`·`python3`·`make`.
`make check` 금지, §7의 여섯 명령을 개별로. git은
`/Library/Developer/CommandLineTools/usr/bin/git`, 파이썬은 `.venv/bin/python`. `xmllint`는 정상.

## 소유 파일 — 정확히 셋

- `vlogsrt/project/commands.py` (**신규, 단일 모듈** — 패키지로 만들지 마라)
- `vlogsrt/cli.py` (**수정** — 레거시 명령 여덟의 동작은 한 바이트도 안 바뀐다)
- `tests/project/test_project_cli.py` (**신규**)

`runner.py`·`export.py`를 포함해 그 밖은 범위 밖이다. 부족하면 멈추고 보고하라.

---

## 1. 무엇을 잇는가

```python
prepare_audio(project_path, artifact_dir, *, overrides, policy, config, project_config)
analyze_audio(prepared, *, transcriber, session=None)
publish_project(analyzed, *, vision_client, destination, online=False, dtd_path=None)
```

`commands.py`가 설정을 읽고, 자격증명을 풀고, 클라이언트를 만들어 셋을 순서대로 부른다.
**이 프로젝트의 비용 경계가 정확히 거기 산다.**

## 2. 불변식 18 — `cli.py`가 보는 것은 `project.commands` 하나다

```python
from .project import commands                  # OK
from .project.commands import project_run      # OK
from .project import runner                    # FAIL
from .project.commands.parser import x         # FAIL
```

`cli.py`는 파싱과 출력만. 계산·설정·자격증명·예외 변환은 전부 `commands.py`.
**`commands.py`는 `cli.py`를 import하지 못한다**(계약 §1). 그러므로 레거시
`_resolve_online`을 재사용하지 마라 — §3-e.

## 3. 비용 경계

### 3-a. 전사기 — `force_offline`을 **반드시** 같이 넘긴다

```python
transcriber = transcribe.get_transcriber(cfg, online=online, force_offline=not online)
```

v1은 `get_transcriber(cfg, online=online)`을 "이 함수가 비용 안전장치다"라며 그대로 쓰라고
했다. **틀렸다.** `transcribe.py:399`는 `force_offline or (not online and env.is_offline())`이라,
셸에 `VLOGSRT_OFFLINE=0`이 있으면 `--offline`을 쳐도 `LiveTranscriber` — **유료 전사**다.
레거시가 이 구멍을 이미 막고 있다(`cli.py:185`, `pipeline.py:196-199`). 같은 선을 긋는다.

### 3-b. 비전 자격증명은 `project_config`가 지정한 **이름**에서만

```python
key_name = project_config.vision_api_key_env        # 기본 "GOOGLE_API_KEY"
url_name = project_config.vision_base_url_env       # 기본 "GEMINI_BASE_URL"
api_key  = env.get(key_name).strip()
base_url = env.get(url_name).strip()
```

**`env.require_api_key()`·`env.base_url()`를 비전에 쓰지 마라** — `create_vision_client`
독스트링: *"한쪽 키가 양쪽 공급자를 열면 안 된다."*

### 3-c. `--online`이면 **키 값과 base URL 값이 둘 다** 있어야 한다 — 파이프라인 시작 전에

- 키 값이 비었다 → 멈춘다.
- **base URL 값이 비었다 → 멈춘다.** `create_vision_client(base_url=None)`은 **공개 OpenAI
  주소**로 간다(`transcribe.py:459-461`). 기본 모델이 Gemini이므로 **Google 키가 OpenAI로
  전송된다.** v1은 이 경우를 정의하지 않았다.
- STT 자격증명(`get_transcriber`)도 `prepare_audio` **앞**에서 해결한다.

오류 문구에는 **환경 변수 이름만**. **키 값은 로그·오류·출력 어디에도 싣지 마라.**

### 3-d. 오프라인은 비전 클라이언트를 **만들지 않는다**

`vision_client=None`, `online=False`로 러너를 부른다. PROTO-4가 이미 정의했다 — 블록마다
`status="error"` 판정 → `final_accepted=False`, blockers `['decision_error']`
(`test_runner_publish.py:583-598`가 단언한다). 클라이언트는 결코 불리지 않는다.

### 3-e. `--online`·`--offline`은 argparse **상호 배타 그룹**

`add_mutually_exclusive_group`. argparse가 종료 코드 **2**로 거절하므로 §4 표와 맞는다.
레거시 `_resolve_online`은 `RuntimeError`→`main()`에서 **1**이 된다(재현됨) — 쓰지 마라.

### 3-f. 비용 길이 안전장치는 **없다** — 그 사실을 적는다

레거시 `run`의 `_confirm_cost`/`--yes`에 해당하는 것을 이 프로토타입은 두지 않는다
(계획상 비용 추정은 `inspect`의 몫). `--help`의 `--online` 설명에 "유료. 길이 안전장치
없음"을 적어라.

## 4. 명령 표면

```text
vlogsrt project run INPUT -o OUT
    [--media-map PATH]        # media.load_media_map
    [--project-config PATH]   # load_project_config
    [--dtd PATH]
    [--offline | --online]    # 기본 오프라인
```

### 4-a. `-o OUT`의 배치 — 형제 둘

- `artifact_dir  = OUT / "artifacts"`
- `destination   = OUT / "package"`

**`destination`을 `OUT` 자체로 두지 마라.** `publish_package`의 `_require_managed_root`
(`export.py:916-936`)는 관리 이름(`revisions`·`current`·잠금) 밖의 항목이 하나라도 있으면
**거부한다** — `OUT/artifacts`가 그 안에 있으면 두 번째 실행부터 죽는다.

### 4-b. 설정

- 레거시 `Config`는 **`load_config()`**로 만든다. `prepare_audio(config=None)`은 bare
  `Config()`라 사용자 `vlogsrt.toml`이 소스 분석 키의 설정 지문에서 빠진다.
- `ProjectConfig`는 `load_project_config(--project-config)`.

### 4-c. `final_accepted`·blockers는 **출판된 기록에서** 읽는다

```python
review = json.loads(published.package.review.read_text(encoding="utf-8"))
```

`ExportPackage`·`PublishedProject`에는 그 값이 **없다**(경로뿐). **금지**:
`export._final_blockers` import(재유도, P4-04 모양), `decisions`·`findings`로 근사
(`findings`는 성공 출판에서 언제나 `()`). 기존 소비자가 이미 이 패턴을 쓴다
(`test_runner_publish.py:493-494`).

### 4-d. 출력 — **"성공"이 아니라 "초안"이다** [D35]

`EditPlan`에는 **사람 검토 상태가 없다**(R-06의 일). `accepted` 판정이 임계값을 넘으면
**사람 확인 없이 컷이 적용된다.** 규칙 9 파생 조항: *"자동 컷의 미검토 상태는 후속 자막
생성으로 해제되지 않는다."* 그러므로:

- `plan.removed`가 비어 있지 않으면 종료 코드와 무관하게 **눈에 띄게**:
  `자동 컷 N건 — 사람 검토 없음(초안)`.
- **`final_accepted`를 "승인"으로 번역하지 마라.** "차단 없음"이라고 쓴다.
- `final_accepted`가 거짓이면 blockers 목록과 함께 눈에 띄게 알린다.
- `dtd_path`가 `None`이면 `DTD 대조 안 함`을 출력한다(패키지 기록에는 안 남는다).
- 리비전 경로(`package.fcpxml.parent`), 판정 수, 제거 구간 수.
- `rich`(`vlogsrt.log`)로. `print` 금지.

`run`은 임시다 — 최종 표면은 `inspect | cut | approve | caption`이고 `run`을 두지 않는다
(사람 게이트 우회). **`--help`와 커밋 메시지에 적어라.**

### 4-e. 종료 코드 — 집행 지점은 **project 핸들러 안**이다

| 코드 | 언제 |
|---|---|
| 0 | 출판됨, blockers 없음 (**초안**일 수 있다 — §4-d 출력이 그것을 말한다) |
| 1 | 출판됨, `final_accepted` 거짓 |
| 2 | 실패 — 플래그 충돌(argparse)·설정·미디어·자격증명·파이프라인·출판 예외 |

**project 핸들러가 `Exception`을 잡아 `log.error` 후 2를 돌려준다.** 레거시 `main()`의
`RuntimeError→1`(`cli.py:310-314`)로 흘러가면 파생 예외 대부분(`UnsupportedProject`·
`VadUnavailable`·`FrameSamplingError`…가 `RuntimeError` 하위다)이 **1**이 되어 막힌 패키지와
구분이 사라진다. `main()`은 고치지 마라(레거시 동작). `KeyboardInterrupt`는 잡지 마라.

## 5. 계약

§1/불변식 18 · §4 openai는 `transcribe.py`만 · §5 subprocess 금지 · §7 모델 리터럴 금지 ·
규칙 8 · 40줄 · 추상화 선행 금지 · 레거시 명령 여덟 무변경. `commands.py`에 dataclass를 두면 frozen.

## 6. 테스트 — 실패 우선, 유료 호출 0회

`tests/project/test_project_cli.py`. **cwd 의존을 끊어라** — 저장소 뿌리에
`vlogsrt.project.toml`이 실재한다. 항상 명시적 `--project-config`(tmp)를 준다.
러너 셋은 `monkeypatch`로 갈아끼워도 되고, 기존 재료(`test_runner_publish.py`의
`_FakeVisionClient`·`_fake_frames` 등)를 **복제**해 끝까지 돌려도 된다(그 파일은 수정 금지).

1. **오프라인 실행** — 합성 프로젝트에 **블록이 1개 이상** 있어야 한다(0개면 판정이 없어
   `final_accepted=True`라 이 테스트가 아무것도 못 잰다). 끝까지 흐르고 blockers
   `['decision_error']`, **종료 코드 1**, 비전 클라이언트 생성자가 **불리지 않는다**
   ("부르면 실패" 스텁).
2. **`VLOGSRT_OFFLINE`이 꺼진 환경에서도 `--offline`과 무플래그가 픽스처 전사기다** —
   `env.is_offline`을 **몽키패치로 `False`**(실제 환경 변수를 0으로 두지 마라).
   `get_transcriber`가 `force_offline=True`로 불리는지 확인한다. `force_offline`을 빼는
   뮤테이션에서 이 테스트가 죽어야 한다.
3. `--online` + 키 **환경 변수 값**이 빈 → 파이프라인 시작 전 종료 코드 2, 문구에 이름은 있고
   값은 없다. (설정의 **이름**을 비우는 것은 `ProjectConfigError`로 도달 불가다 — 값을 비워라.)
4. `--online` + 키는 있고 **base URL 값이 빈** → 시작 전 종료 코드 2. `create_vision_client`가
   **불리지 않는다.**
5. 비전 자격증명이 `project_config`의 이름에서 온다 — 다른 이름을 지정한 설정을 주면 그 이름을
   읽는다. **비전 자격증명 해결 함수는 `env.require_api_key`를 부르지 않는다**(전사기를 스텁으로
   갈아끼운 상태에서 증명 — STT 경로는 원래 그 함수를 부른다).
6. `--online --offline` → argparse 거절, 종료 코드 2.
7. 파이프라인 예외(예: 미디어 없음 `UnsupportedProject`) → **종료 코드 2**(1이 아니다).
8. 성공 경로(전부 accepted, 컷 있음) → 종료 코드 0, 출력에 `자동 컷 N건 — 사람 검토 없음(초안)`,
   리비전 경로, `DTD 대조 안 함`.
9. `-o OUT`로 **두 번 연속** 실행해도 두 번째가 `_require_managed_root`에서 죽지 않는다(§4-a).
10. **불변식 18 뮤테이션** — `cli.py`에 `from .project import runner`를 임시로 넣으면
    `check_invariants.py`가 `[FAIL]`. 보이고 복원하라(보고용; 테스트 파일에 넣지 않아도 된다).

**단언 약화 금지. 임계값·픽스처 수정 금지.**

## 7. DoD

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
.venv/bin/python -m pytest tests/project/test_project_cli.py
.venv/bin/ruff check vlogsrt ui tests scripts
.venv/bin/ruff format --check vlogsrt ui tests scripts
.venv/bin/mypy
.venv/bin/python scripts/check_invariants.py | grep -c '^\[OK\]'   # 20
.venv/bin/python -m pytest
```

전체 pytest **2488 + 새 테스트 수**, 0 failed.

## 8. 보고 형식

```
커밋 SHA:
--help 실제 출력: <vlogsrt project run --help>
commands.py 공개 함수: <시그니처>
get_transcriber 호출 자리: <file:line, force_offline 인자>
비전 자격증명 해결 + 키/URL 빈 값 처리: <file:line + 실제 오류 문구>
final_accepted 읽는 자리: <file:line>
예외→2 변환 자리: <file:line>
종료 코드 0/1/2 증거: <테스트 출력>
불변식 18 뮤테이션: <[FAIL] 실제 출력>
게이트: <명령마다 실제 숫자>
diff 범위: <git diff --stat. 소유 파일 셋뿐인가>
멈춰야 했던 것:
```

---

## 9. 방향 검토 기록 (v1 → v2)

### 뒤집힌 내 주장

| # | v1 | 실제 |
|---|---|---|
| 1 | §3-a *"`get_transcriber(cfg, online=online)` 그대로 — 이 함수가 비용 안전장치다"* | **틀림 (A).** `VLOGSRT_OFFLINE=0`이면 `--offline`이 유료 전사로 샌다. 레거시는 `force_offline`으로 막는다 |
| 2 | §3-c *"`vision_api_key_env`가 빈 값이면"* | **도달 불가 (A).** 설정 파서가 빈 이름을 거부한다. 검사할 것은 환경 변수 **값**이다 |
| 3 | §3-e *"`_resolve_online`을 재사용하거나"* | **틀림 (A·B 공통, B가 재현).** `RuntimeError`→`main()`→종료 코드 **1**. 게다가 `commands.py`는 `cli.py`를 import할 수 없다 |
| 4 | 종료 코드 표 | **집행 지점이 없었다 (A·B 공통).** 핸들러가 예외를 2로 바꾸지 않으면 실패와 막힌 패키지가 둘 다 1 |
| 5 | §4 "`final_accepted`를 출력하고 종료 코드를 정하라" | **출처가 없었다 (A·B 공통).** 반환값에 없다. review.json에서 읽는다 |
| 6 | 테스트 4 "`env.require_api_key`를 부르지 않는다" | **자기모순 (A).** `--online` STT 경로가 그 함수를 반드시 부른다. 비전 해결 함수로 범위를 좁힌다 |
| 7 | 테스트 1 "오프라인이면 blockers `['decision_error']`" | **블록 0개면 거짓 (A).** 입력에 블록이 있어야 한다 |
| 8 | 종료 코드 0 = 성공 | **D35 긴장 (A).** 미검토 자동 컷 위의 초록. 출력이 "초안"을 말해야 한다 |

### (A)만 찾은 것
- **base URL 빈 값 → Google 키가 OpenAI로 간다.**
- `-o` 하나로 `artifact_dir`·`destination` 둘을 채워야 하는데 규칙이 없었다.
  (코디네이터가 확인: `_require_managed_root`가 관리 이름 밖 항목을 거부하므로 **형제 배치**여야 한다.)
- 비용 길이 안전장치의 부재가 명시되지 않았다.

### (B)만 찾은 것
- `prepare_audio(config=None)`은 bare `Config()` — `load_config()`를 넘겨야 한다.
- 저장소 뿌리의 `vlogsrt.project.toml` 때문에 테스트가 cwd에 의존한다.
- 불변식 18 뮤테이션을 /tmp 사본에서 실제 재현했다(`[FAIL]` 1건).

### 판정 차이
(A) UNSOUND / (B) SOUND_WITH_CONCERNS. (B)는 "일곱 번째 `protected=[]`은 없다"고 했지만
(A)의 §3-a(`--offline`이 유료로 샌다)와 base URL 건은 **"부재가 권한이 된다"의 정확한 모양**
(환경 변수가 없다 → 유료 호출 / URL이 없다 → 다른 공급자)이다. **(A)가 옳다.** 코디네이터가
`transcribe.py:399`·`cli.py:185`·`pipeline.py:196-199`·`create_vision_client`를 직접 읽어 확인했다.
