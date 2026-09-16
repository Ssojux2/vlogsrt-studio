# PROTO-5 — `project/commands.py` + `cli.py`: 비용 경계가 사는 자리

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 **`8ca214d`** (트리 깨끗, pytest 2488 passed)
venv: `.venv/bin/python`

**Xcode 라이선스 미동의로 툴체인 shim이 죽어 있다**(exit 69) — `git`·`python3`·`make`.
`make check`를 부르지 말고 §7의 여섯 명령을 개별로 돌려라. git은
`/Library/Developer/CommandLineTools/usr/bin/git`, 파이썬은 `.venv/bin/python`.
(`xmllint`는 정상이다.)

## 소유 파일 — 정확히 셋

- `vlogsrt/project/commands.py` (**신규**)
- `vlogsrt/cli.py` (**수정** — 기존 명령 여덟의 동작은 한 바이트도 바뀌면 안 된다)
- `tests/project/test_project_cli.py` (**신규**)

`vlogsrt/project/runner.py`를 포함해 이 셋 밖은 범위 밖이다. 러너가 부족하면
**멈추고 보고하라** — 고치지 마라.

---

## 1. 이 태스크가 존재하는 이유

러너 셋이 다 섰다(`8ca214d`). 그런데 **아무도 부를 수 없다.**

```
prepare_audio(project_path, artifact_dir, *, overrides, policy, config, project_config)
analyze_audio(prepared, *, transcriber, session=None)
publish_project(analyzed, *, vision_client, destination, online=False, dtd_path=None)
```

셋을 잇고, 설정을 읽고, **자격증명을 풀고**, 클라이언트를 만들어 넘기는 층이 없다.
그 층이 `commands.py`이고, **이 프로젝트의 비용 경계가 정확히 거기 산다.**

## 2. 계약 §1 / 불변식 18 — CLI가 볼 수 있는 것은 정확히 하나다

`check_invariants.py:302-313`: *"CLI 예외는 `project.commands`로 좁다 — 하위 모듈
(`commands.parser`)도 안 된다."*

```python
from .project import commands          # ✅
from .project.commands import project_run   # ✅ (그 모듈 자체를 쓰는 형태)
from .project import runner            # ❌ 불변식이 잡는다
from .project.commands.parser import x # ❌ 하위 모듈
```

`commands.py`는 **단일 모듈**이다. 패키지로 만들지 마라.
`cli.py`는 **파싱과 출력만** 한다(모듈 독스트링 그대로) — 계산은 `commands.py`가 한다.

## 3. 비용 경계 — 이 태스크의 핵심 위험

### 3-a. 전사기의 안전장치는 이미 있다. 그대로 써라

```python
transcribe.get_transcriber(cfg, online=online)   # transcribe.py:395
```

*"오프라인이면 언제나 픽스처를 돌려준다. **이 함수가 비용 안전장치다.**"*
직접 `LiveTranscriber`를 만들지 마라.

### 3-b. 비전 자격증명은 **레거시 STT 키를 물려받지 않는다**

`create_vision_client`의 독스트링이 못 박는다(`transcribe.py:423-426`):

> *"레거시 STT helpers(`env.require_api_key`·`env.base_url`)를 물려받지 않는다 —
> **한쪽 키가 양쪽 공급자를 열면 안 된다.**"*

그러므로 비전 키는 **`project_config`가 이름으로 지정한 환경 변수**에서만 온다:

```python
project_config.vision_api_key_env    # 기본 "GOOGLE_API_KEY"   (config.py:88)
project_config.vision_base_url_env   # 기본 "GEMINI_BASE_URL"  (config.py:89)
```

`env.get(...)`으로 읽는다. **`env.require_api_key()`를 비전에 쓰지 마라.**

### 3-c. 키가 없는데 `--online`이면 **유료 호출 앞에서 멈춘다**

> **"키가 없다"가 "오프라인으로 돌린다"가 되면 안 된다.**
> 그것이 이 태스크의 `protected=[]` 자리다 — 부재가 조용한 성공이 되는 모양.

`--online`인데 `project_config.vision_api_key_env`가 빈 값이면 **파이프라인을 시작하기
전에** 실패한다. 오류 문구에는 **환경 변수 이름만** 쓴다.

> **키 값을 로그·오류·trace에 절대 싣지 마라.** 이름만.

### 3-d. 오프라인 실행은 **정직하게 막힌 패키지**를 낸다

오프라인이면 비전 클라이언트를 **아예 만들지 않고**(`None`) `online=False`로 러너를
부른다. PROTO-4가 그 경우를 이미 정의했다 — 블록마다 `status="error"` 판정,
`final_accepted=False`, blockers `['decision_error']`.

**그것이 옳은 동작이다.** 오프라인 실행을 성공으로 위장하지 마라.
전사는 픽스처로 돌고 컷은 판정을 못 받는다 — 그 사실이 패키지에 남는다.

### 3-e. `--online`과 `--offline`을 함께 쓰면 오류

레거시 `_resolve_online`(`cli.py:134-137`)과 **같은 규칙**이다. 그 함수를 재사용하거나
같은 문구로 거절하라.

## 4. 명령 표면

```text
vlogsrt project run INPUT -o OUT
    [--media-map PATH]        # media.load_media_map으로 읽는다
    [--project-config PATH]   # ProjectConfig
    [--dtd PATH]              # 없으면 DTD 대조를 건너뛴다 — 그 사실이 출력에 뜬다
    [--offline] [--online]
```

**`run`은 임시다.** 확정 계획(`docs/plans/2026-09-13-cut-first-and-plugin.md` §4)의 최종
표면은 `inspect | cut | approve | caption` 넷이고 **`run`을 두지 않는다** — 사람 검토
게이트를 우회하기 때문이다. R-06의 검토 상태 기계가 들어오면 이 명령은 폐기된다.
**그 사실을 `--help` 문구와 커밋 메시지에 적어라.**

### 출력

- 성공 경로: 리비전 경로, `final_accepted`, blockers, 판정 수, 제거 구간 수를 사람이
  읽을 수 있게. `rich`로(규칙: `print` 금지).
- **DTD를 대조하지 않았으면 그 사실을 출력한다** — `publish_project`가 실은
  `dtd_path`가 `None`인 경우. 패키지 기록에는 안 남으므로 여기가 유일한 신호다.
- **`final_accepted`가 거짓이면 그것을 눈에 띄게 알린다.** 조용히 성공으로 보이면 안 된다.

### 종료 코드

| 코드 | 언제 |
|---|---|
| 0 | 출판 성공 **그리고** `final_accepted`가 참 |
| 1 | 출판은 됐지만 `final_accepted`가 거짓 (막힌 패키지) |
| 2 | 파이프라인 실패 (파싱·미디어·설정·자격증명·출판 예외) |

**막힌 패키지와 실패를 같은 코드로 묶지 마라.** 둘은 다른 사실이다.

## 5. 지켜야 하는 계약

- **계약 §1 / 불변식 18**: `cli.py`는 `vlogsrt.project.commands`만.
- **계약 §4**: openai는 `transcribe.py`만. `commands.py`는
  `transcribe.get_transcriber`·`transcribe.create_vision_client`를 **부를** 뿐이다.
- **계약 §5**: subprocess 금지. **계약 §7**: 모델 리터럴 금지 — 전부 config에서.
- **규칙 8**: 임계값 하드코딩 금지. **40줄 규칙.** **추상화 선행 금지.**
- **레거시 보존**: `vlogsrt run|redo|eval|evalset|probe|record|config|keywords`의 관측
  가능한 동작과 SRT 바이트가 그대로여야 한다. 기존 테스트가 증인이다.

## 6. 테스트 — 실패 우선

`tests/project/test_project_cli.py`. **유료 호출 0회.** `monkeypatch`로 러너 셋을
갈아끼우거나 합성 프로젝트로 끝까지 돌린다.

1. 오프라인 실행이 **끝까지 흐르고** `final_accepted=False`·blockers `['decision_error']`,
   **종료 코드 1**. 비전 클라이언트가 **만들어지지 않았다**(생성자를 "부르면 실패"로 꽂아라).
2. `--online`인데 `vision_api_key_env`가 빈 값 → **파이프라인 시작 전에** 실패, 종료 코드 2.
   **오류 문구에 환경 변수 이름이 있고 키 값은 없다.**
3. `--online --offline` → 거절.
4. 비전 자격증명이 **`project_config`의 이름에서** 온다 — `vision_api_key_env`를
   다른 이름으로 바꾼 설정을 주면 그 이름을 읽는다. **`env.require_api_key`를 부르지
   않는다**(부르면 실패하는 스텁으로 증명하라).
5. `--dtd` 없이 돌리면 출력에 **"DTD 대조 안 함"** 신호가 뜬다.
6. 성공 경로(모두 승인 판정) → 종료 코드 0, 리비전 경로가 출력에 있다.
7. **불변식 18이 실제로 문다** — `cli.py`에 `from .project import runner`를 임시로 넣으면
   `check_invariants.py`가 `[FAIL]`을 낸다. 뮤테이션으로 보이고 복원하라.
8. 레거시 명령이 그대로다 — 기존 CLI 테스트가 전부 통과한다(전체 pytest가 증인).

**단언을 약화해 통과시키지 마라. 임계값·픽스처를 건드리지 마라.**

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

전체 pytest는 **2488 + 새 테스트 수**, 0 failed. 불변식은 `[OK]` 줄을 **세어** 보고한다.

## 8. 보고 형식

```
커밋 SHA:
명령 표면: <실제 --help 출력>
commands.py 공개 함수: <시그니처>
비전 자격증명 해결 자리: <file:line. 어느 env 이름을 읽는가>
키 없음 + --online 처리: <file:line + 실제 오류 문구(값이 안 들어갔는지 확인)>
종료 코드 셋: <각각의 테스트 출력>
불변식 18 뮤테이션: <[FAIL] 실제 출력>
게이트: <명령마다 실제 출력 숫자. 불변식은 센 값>
diff 범위: <git diff --stat. 소유 파일 셋뿐인가. 레거시 명령 동작 무변경인가>
멈춰야 했던 것: <있으면. 없으면 "없음">
```
