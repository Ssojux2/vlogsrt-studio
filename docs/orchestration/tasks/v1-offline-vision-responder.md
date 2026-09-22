# V1 — 비전 판정을 오프라인에서 재생한다 (무료로 러프컷을 처음 본다)

작성: Opus(코디네이터). 근거는 2026-09-20의 전체 검증
(`docs/orchestration/prototype-test-plan.md`와 이번 주기 검증).

기준 커밋: `401e2a5` (`feat/final-cut-project`).

**툴체인**: git은 `/Library/Developer/CommandLineTools/usr/bin/git`, 파이썬은
`/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype/.venv/bin/python`, **`make` 금지**
(이 머신의 `make`는 Xcode 라이선스로 죽어 있다).

---

## 1. 왜 하는가

□ 지금 **"영상이 실제로 짧아지는가"를 사람이 무료로 확인할 길이 없다**
  ○ `vision_client`는 `--online`일 때만 만들어진다(`project/commands.py:61`)
  ○ 오프라인이면 `request_vision`이 예외로 닫히고 블록마다 `decision_error` → 종료 1
  ○ 테스트의 mimic은 `monkeypatch`로만 닿는다(`tests/project/test_project_e2e.py:375`)
□ 그리고 **mimic이 증명하는 것이 생각보다 적다**
  ○ "이미지"가 JPEG이 아니라 `frame:37` 같은 텍스트 바이트다(`test_project_e2e.py:295`)
  ○ 가짜 모델은 안내 문장에서 첫 후보 아이디를 **문자열 파싱**한다. 이미지를 안 본다
□ 이 태스크가 닫는 것
  ○ 사람이 CLI 한 줄로 **진짜 JPEG을 뽑고 · 판정을 재생하고 · 짧아진 FCPXML을 본다**
  ○ 그래야 뒤따르는 PROTO-7(실물 Final Cut import)과 유료 1콜이 해석 가능해진다

□ **유료 1콜의 전제이기도 하다.** 지금은 블록이 0개라 `--online`을 줘도 비전이 한 번도
  안 불린다. 블록을 만드는 재료가 이 태스크에 들어 있다

---

## 2. 소유 파일

□ 고칠 수 있는 것
  ○ `vlogsrt/transcribe.py` — `VisionResponder` 두 구현을 더한다
  ○ `vlogsrt/project/runner.py` — `publish_project`·`_decide_block`의 주입 형태
  ○ `vlogsrt/project/commands.py` — 재생기 선택과 새 플래그
  ○ `vlogsrt/cli.py` — 새 플래그 하나 (`_add_project_command`만)
  ○ `scripts/smoke_project.py` — 블록이 나는 재료
  ○ 대응 테스트: `tests/project/test_project_e2e.py`, `test_runner_publish.py`,
    `tests/test_transcribe.py`(또는 그에 준하는 자리)
  ○ `docs/RUNBOOK.md`의 Final Cut 절, `docs/DECISIONS.md`(새 결정 하나)

□ 범위 밖. 읽기만 해라
  ○ `vlogsrt/project/vision/policy.py`·`sampling.py` — 조립·해석·표본은 그대로다
  ○ `vlogsrt/project/edit.py`·`export.py`·`captions.py`·`boundary.py`·`speech.py`
  ○ `vlogsrt/preprocess.py` — 프레임 추출은 이미 옳다
  ○ `vlogsrt/config.py`·`project/config.py` — **새 설정 필드를 더하지 마라**
    (`_config_sha256`이 바뀌면 파생 계층 캐시 키가 전부 깨진다)

□ 다른 테스트가 이 변경으로 빨개지면 고치되 **어느 파일의 어느 테스트를 왜 바꿨는지
  하나씩 보고하라.** 단언을 약화하지 마라

---

## 3. 계약

### 3-a. `VisionResponder` — `Transcriber`와 대칭으로

□ `vlogsrt/transcribe.py`에 넣는다. 이 파일은 이미 Protocol을 쓴다
  (`Transcriber`, `transcribe.py:66` — "구현체가 둘이라 실제로 필요하다")
□ 구현체가 둘이므로 `CLAUDE.md`의 "추상화를 미리 만들지 마라"에 걸리지 않는다

```python
class VisionResponder(Protocol):
    def decide(self, *, model: str, payload: Mapping[str, object],
               block_id: str) -> dict[str, Any]: ...
```

□ `LiveVisionResponder` — `create_vision_client`로 만든 클라이언트를 들고
  `request_vision(..., online=True)`를 부른다. **유료 경로는 여기 하나뿐이다**
□ `FixtureVisionResponder` — `<namespace>/<block_id>.vision.json`을 읽는다.
  ○ **네트워크를 모른다.** `openai`를 import하지 않고 클라이언트를 만들지 않는다
  ○ 픽스처가 없으면 `FixtureTranscriber`와 같은 모양으로 **명확히 실패한다**
    (`_missing_fixture`의 문투를 따르되, 사람이 만들 수 있는 것이므로
    "`vlogsrt record`를 실행하라"가 아니라 그 경로에 파일을 놓으라고 안내한다)
  ○ 경로 조각 검사는 `_namespace_path`(`transcribe.py:124`)의 선을 그대로 쓴다
□ `block_id`를 받는 이유: 픽스처를 블록마다 고르기 위해서다. `LiveVisionResponder`는
  그 값을 쓰지 않아도 된다 — 계약의 대칭이 우선이다

### 3-b. 러너에서 `online`이라는 단어를 없앤다

□ 지금: `publish_project(analyzed, *, vision_client: object, online: bool, ...)`
□ 뒤: `publish_project(analyzed, *, vision: VisionResponder, ...)`
□ `_decide_block`(`runner.py:776`)은 `vision.decide(...)`만 부른다
□ **이것이 안전을 약화하지 않는 이유를 반드시 보고에 적어라**
  ○ `online=True`는 계속 "사람이 `--online`을 직접 친 실행"만 뜻한다
  ○ 그 단어가 러너에서 사라져 `commands.py`에만 남는다 —
    PROTO-5가 선언한 "비용 경계는 `commands.py`"가 오히려 **강해진다**
  ○ 재생기에 `online=True`를 먹이는 우회로가 **타입 수준에서 없어진다**
□ `transcribe.request_vision`의 `online` 게이트(`transcribe.py:447`)는 **그대로 둔다.**
  한 글자도 고치지 마라

### 3-c. 재생기 선택은 `commands.py`가 한다

□ `--vision-fixtures <디렉터리>` 하나를 더한다(이름은 네가 정해도 된다. 근거를 적어라)
□ 규칙
  ○ `--online`과 **동시 지정 금지.** argparse에서 막거나 명시적 오류로 닫는다
  ○ `--online`이면 `LiveVisionResponder`
  ○ `--vision-fixtures`면 `FixtureVisionResponder`
  ○ 둘 다 없으면 **지금과 똑같이 막힌다** — 블록마다 `decision_error`, 종료 1.
    기본값을 바꾸지 마라
□ **재생기가 쓰였다는 사실이 산출물에 남아야 한다**
  ○ stdout 한 줄과 `review.json`에 기록한다
  ○ 진짜 판정으로 위장하면 안 된다 — 이것이 이 태스크에서 가장 조심할 것이다
  ○ 실은 방법은 네가 정한다. `review.json`은 계약 기록물이니 필드를 더하면
    그 필드가 선택적임을 계약에 적어라

### 3-d. 페이로드 덤프

□ 조립된 payload를 `artifacts/vision/<block_id>.request.json`에 남긴다
□ base64 blob은 **그대로 쓰지 말고** `{frame_id, image_path, sha256, bytes}`로 치환한다
  ○ 프레임은 이미 `frames_dir`에 있다. 같은 바이트를 두 번 쓰지 않는다
  ○ 안내 문장(`_instruction`의 산물)은 **원문 그대로** 남긴다
□ 이것이 있어야 유료 1콜이 실패했을 때 "페이로드가 틀렸나 / 계정이 거부했나"를 가른다

### 3-e. 블록이 나는 재료 — `scripts/smoke_project.py`

□ 지금 블록이 0개인 **진짜 이유**는 클립이 10초여서가 아니다.
  합성 전사가 0.5~9.0초를 전부 보호해 여집합이 6초 미만이기 때문이다
□ 고치는 것
  ○ 클립을 30초로: 0~10초는 소리, 10~30초는 무음
    (이 머신 ffmpeg 9.0.1에 `drawtext`가 **없다**. `testsrc`/`testsrc2`/`sine`/
    `anullsrc`는 있다. 프레임 번호는 `testsrc` 자체 카운터로만 각인된다 —
    **`testsrc`를 써서 사람이 프레임 번호를 눈으로 읽을 수 있게 해라**)
  ○ 합성 전사를 0~10초 구간만 덮게 줄인다 → 여집합 20초 → 6~10초 블록 둘
  ○ 블록 둘에 대응하는 `*.vision.json` 픽스처를 만들어 스모크에 싣는다
□ **상수·픽스처 변경이다. 임계값(`block_min_seconds` 등)을 고치지 마라**
□ 스모크에 확인 줄을 더한다
  ○ `제거 ≥ 1건`
  ○ **출력 시퀀스 duration < 입력 duration** (기계로 읽어 비교하라)
  ○ `frames/*.jpg`가 **진짜 JPEG이다** (매직 바이트 `\xff\xd8\xff`로 확인)

---

## 4. 임계값

□ 어떤 임계값도 바꾸지 마라. `block_min_seconds`·`block_max_seconds`·`keep_seconds`·
  `vision_apply_confidence`·`vision_max_images`·`vad_positive`·`guard_padding_seconds`
□ `policy.py`의 레일(`min(cfg.vision_max_images, 16)`,
  `max(cfg.vision_apply_confidence, 0.8)`)도 그대로다
□ **`Config`·`ProjectConfig`에 새 필드를 더하지 마라** — 설정 지문이 바뀌면
  파생 계층의 분석 캐시 키가 전부 깨진다

---

## 5. 테스트 — 실패 우선

□ 반드시 덮을 것
  ○ 재생기로 오프라인 전 구간이 **종료 0**이고 **시퀀스가 짧아진다**
  ○ 재생기가 `openai`를 import하지 않고 클라이언트를 **한 번도 만들지 않는다**
    (기존 `test_runner_publish.py:587`의 `client.calls == []` 계열을 따라가라)
  ○ 둘 다 없으면 지금과 같이 `decision_error`·종료 1이다 — **기본값 회귀 금지**
  ○ `--online`과 `--vision-fixtures` 동시 지정이 막힌다
  ○ 픽스처가 없으면 조용히 통과하지 않고 **명확히 실패한다**
  ○ 페이로드 덤프에 base64 blob이 **들어 있지 않다**
  ○ 재생기가 쓰였다는 사실이 산출물에 남는다
□ 기존 e2e 단언과 임계값은 **한 글자도 바꾸지 마라**.
  바꿔야 하면 §2의 보고 규칙을 따른다
□ 죽은 검사를 만들지 마라 — 단언 대상이 비어 있으면 먼저 그것을 막아라(D32의 교훈)

---

## 6. 절대 금지

□ `.env` 읽기·수정, `--online` 실행, 유료 API 호출
□ `tests/fixtures/api/**`·`golden/**` 수정. 읽기만 해라
□ 임계값·기대값 수정. 새 설정 필드 추가
□ 테스트 `skip`·`xfail`
□ 새 의존성 추가
□ `request_vision`의 `online` 게이트 완화
□ 재생 판정을 진짜 판정처럼 보이게 하는 것
□ `vision/policy.py`·`sampling.py`·`edit.py`·`export.py` 수정

---

## 7. 게이트

```bash
PYTHONPATH=$PWD <python> -m ruff check vlogsrt ui tests scripts
PYTHONPATH=$PWD <python> -m ruff format --check vlogsrt ui tests scripts
PYTHONPATH=$PWD <python> -m mypy
PYTHONPATH=$PWD <python> -m pytest
PYTHONPATH=$PWD <python> scripts/check_invariants.py
PYTHONPATH=$PWD <python> scripts/smoke_project.py
```

□ `401e2a5` 기준선: ruff format **100개 파일**, 불변식 **21개 `[OK]`**,
  pytest **2555 통과**. 새 테스트로 늘 수 있다. **줄면 회귀다**

---

## 8. 보고

□ exact base, 브랜치, 워크트리, 커밋 SHA, 변경 파일
□ `publish_project`·`_decide_block` 시그니처 변경을 **diff 그대로** 인용하라
□ §3-b의 "안전이 약해지지 않는 이유"를 네 말로 적어라
□ 플래그 이름을 그렇게 정한 근거
□ 재생 사실을 산출물에 싣는 방법을 왜 그렇게 정했는지 — **이건 네가 정한다**
□ 스모크 출력 전문. 특히 `제거 N건`과 입력·출력 duration 값
□ 여섯 게이트의 명령·종료 코드·실제 수치
□ 기대값을 바꾼 테스트가 있으면 파일·이름·이유를 하나씩
□ 남은 결함과 멈춘 항목. 없으면 `없음`

□ 막히면 재현 명령, 종료 코드, 짧은 로그, 예상값과 실제값을 적어 `ask`로 올려라.
  같은 실패를 새 증거 없이 두 번 반복하지 마라
□ 설계 판단이 갈리면 조용히 고르지 말고 선택지를 들고 `ask`로 올려라
□ 구현이 끝나면 `ask`로 코디네이터 판정을 받고, 범위 안 지적을 같은 Dispatch에서
  고친 뒤 검사를 다시 돌려라
□ 승인 뒤에만 `worker_done`을 정확히 한 번. 머지도 push도 하지 마라 — 통합은 코디네이터가 한다
