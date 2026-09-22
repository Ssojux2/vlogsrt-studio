# PROTO-6 — 전 구간 테스트 · 로컬 실측 스모크 · 실행 안내서

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 **`f0702c8`** (트리 깨끗, pytest 2498 passed)

**툴체인 shim 사망**(exit 69): git은 `/Library/Developer/CommandLineTools/usr/bin/git`,
파이썬은 `.venv/bin/python`, **`make` 금지**. `xmllint`(/usr/bin)·`ffmpeg`/`ffprobe`
(/opt/homebrew/bin)는 정상. venv에 numpy 2.5.3·onnxruntime 1.30.0 설치됨.

## 소유 파일

- `tests/project/test_project_e2e.py` (**신규**)
- `tests/project/test_project_cli.py` (**수정은 테스트 9 하나만** — 아래 §3)
- `docs/RUNBOOK.md` (**섹션 추가만** — 기존 레거시 절은 한 글자도 안 바꾼다)
- `docs/orchestration/progress.md` (**추가만** — §2 스모크 결과 기록. 없으면 만든다)

제품 코드(`vlogsrt/**`)는 **범위 밖**이다. 막히면 멈추고 보고하라. 픽스처
(`tests/fixtures/api/**`)·`golden/**`·임계값 수정 금지. `.env` 읽기·수정 금지.
**`--online` 금지, 유료 호출 금지.**

---

## 1. 전 구간 테스트 — 내부 단계는 진짜, 바깥 경계만 가짜

`cli.main(["project", "run", ...])` 한 번으로 **parse → media → PCM → STT → captions →
boundary → VAD → guard → partition → sampling → vision → edit → captions → export**가
다 돈다. PROTO-5 테스트는 러너 셋을 몽키패치했다 — 이 테스트는 **러너를 갈아끼우지 않는다.**

가짜로 바꿔도 되는 것은 **경계뿐**이다:
- ffmpeg/ffprobe 경계(`preprocess`의 probe·PCM 추출·프레임 추출) — 기존 테스트 기법을 따른다
  (`test_runner_audio.py`의 PCM 스텁, `test_runner_publish.py`의 frame 스텁. **복제**하라)
- 전사기(`transcribe.get_transcriber`가 돌려주는 객체) — 스크립트된 가짜
- 비전 클라이언트 생성(`transcribe.create_vision_client`) — 가짜 클라이언트
- ONNX 세션은 진짜(번들 아티팩트)를 쓰거나 기존 가짜 세션을 쓴다 — 결정적이면 된다

시나리오 둘:
1. **온라인 경로 흉내**(`--online` + 환경 변수는 `monkeypatch.setenv`로 가짜 값, 모든 판정
   accepted) → 종료 코드 0, `package/current`가 가리키는 리비전에 **다섯 파일**, FCPXML이
   `xmllint --noout`을 통과(실제 xmllint를 불러도 된다), 출력 시퀀스가 **실제로 짧아졌다**,
   stdout에 `사람 검토 없음(초안)`.
2. **오프라인**(무플래그) → 종료 코드 1, blockers `decision_error`, 비전 생성자 0회.

**유료 호출 0회**를 단언하라(실제 `openai` 클라이언트가 만들어지지 않는다).

## 2. 로컬 실측 스모크 — 테스트가 아니라 **이 머신에서 실제로 한 번** 돌린다

사람이 실물 FCPXML로 돌리기 전에, 우리가 실제 ffmpeg·xmllint·ONNX로 **무료** 경로를
끝까지 밟아 본다. 스크래치 디렉터리(`$TMPDIR` 아래)에서:

1. `ffmpeg -f lavfi`로 **10초 안팎**의 합성 영상+오디오 파일을 만든다(색 화면 + 사인파 등).
2. 지원 부분집합 안의 최소 FCPXML(`version="1.14"`, 에셋 하나·클립 하나)을 손으로 쓴다.
   파서가 거부하면 거부 문구를 기록하고 고친다 — 지원 범위는 `vlogsrt/project/fcpxml.py`와
   기존 테스트의 합성 문서가 정본이다.
3. `.venv/bin/python -m vlogsrt.cli project run <fcpxml> -o <out> --project-config <tmp toml>`
   (**`--offline`**, 기본).
4. **무엇이 일어났는지 그대로 기록한다.** 특히 사람이 반드시 알아야 할 것:
   - 이 클립에는 전사 픽스처가 없다 — 오프라인 전사기가 무엇을 하는가
     (예외 → 종료 코드 2? 소스 error 기록 → 막힌 패키지?)
   - VAD(실제 ONNX)·프레임 표본(실제 ffmpeg)·xmllint가 실제로 돌았는가
   - 종료 코드, stdout 전문, `package/current/` 파일 목록, review.json의 `final_blockers`
5. 같은 명령을 **한 번 더** 돌려 재실행 동작(§3)을 실측한다.

결과를 `docs/orchestration/progress.md`에 `## 2026-09-16 PROTO-6 로컬 스모크` 절로 남긴다.
**실패도 산출물이다** — 제품 코드를 고치지 말고 관측한 그대로 적는다.

## 3. 재실행 동작을 정직하게 — 테스트 9 수정

PROTO-5의 `test_a_second_run_into_the_same_out_passes_the_managed_root_gate`는 같은 명령의
**두 번째 종료 코드를 확인하지 않고 버린다.** 실제로는 `export._create_revision`이
`"리비전 디렉터리가 이미 있다"`로 `ValueError` → **종료 코드 2**다(의도된 불변 리비전 설계).

- 두 번째 실행의 **종료 코드가 2**이고 오류에 `리비전 디렉터리가 이미 있다`가 있다는 것을
  **단언으로 못 박아라.** 관리 뿌리 검사 통과 단언은 그대로 둔다.
- 이 동작을 RUNBOOK에 적는다(§4).

## 4. RUNBOOK — `docs/RUNBOOK.md`에 새 절 추가

제목: `## Final Cut 프로젝트 러프컷 (프로토타입)`. 레거시 절 뒤에 붙인다. 사람이 **처음부터
끝까지 따라 할 수 있게**, 추측이 아니라 §2에서 실측한 사실로 쓴다.

반드시 담을 것:
- **준비**: `.venv/bin/pip install -e '.[project]'`, ffmpeg·ffprobe·xmllint, Final Cut에서
  내보낸 `.fcpxml`(`version="1.14"`, 지원 부분집합 — 거부되는 것: connected audio·title·
  transition·compound·retime 등. 파서 문구 기준으로), 원본 미디어가 XML의 `file:` 경로에
  있어야 함(아니면 `--media-map` — JSON 형식 예시 포함, `media.load_media_map` 기준).
- **키 (이름만, 값은 `.env`에)**: `OPENAI_API_KEY`(STT), `GOOGLE_API_KEY`+`GEMINI_BASE_URL`
  (비전). **base URL이 비면 거부된다**는 이유 한 줄.
- **명령**: 오프라인 먼저 → 온라인. `--online`은 유료이고 **길이 안전장치가 없다.**
- **산출**: `OUT/artifacts/`·`OUT/package/current/`의 다섯 파일이 각각 무엇인지.
- **종료 코드 0/1/2**의 뜻. **0도 "초안"** — 자동 컷은 사람 검토를 거치지 않았다.
- **재실행**: 같은 입력·같은 설정으로 같은 `OUT`에 다시 돌리면 종료 코드 2(리비전 불변).
  새 `OUT`을 쓰거나 결과를 보존할 필요가 없으면 `OUT/package`를 옮기라.
- **오프라인의 실제 모습**: §2에서 관측한 그대로(픽스처 없는 클립에서 무엇이 나오는지).
- **Final Cut에서 열기**: `package/current/*.fcpxml`을 File → Import → XML. relink 필요 시.
- **비용 가늠(1분 클립)**: STT 약 `$0.006` + 비전 약 `$0.10` → 보수적으로 `$0.10~0.25`.
  비전은 블록 수에 비례한다는 사실.
- **아직 아닌 것**: 자막 글자는 현재 **whisper-1**이다. gpt-transcribe 하이브리드(Q3)는
  진행 중이며 적용되면 이 절을 갱신한다.
- **첫 실행 뒤 할 일**: 실패·이상을 `docs/orchestration/progress.md`에 기록.

## 5. DoD

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
.venv/bin/python -m pytest tests/project/test_project_e2e.py tests/project/test_project_cli.py
.venv/bin/ruff check vlogsrt ui tests scripts
.venv/bin/ruff format --check vlogsrt ui tests scripts
.venv/bin/mypy
.venv/bin/python scripts/check_invariants.py | grep -c '^\[OK\]'   # 20
.venv/bin/python -m pytest
```

전체 pytest **2498 + 새 테스트 수**, 0 failed.

## 6. 보고 형식

```
커밋 SHA:
e2e 시나리오 둘: <테스트 이름 + 실제 결과>
러너를 몽키패치하지 않았음의 증거: <가짜로 바꾼 경계의 목록>
로컬 스모크: <실제 명령 · 종료 코드 · stdout 전문 · 산출 파일 목록 · 재실행 결과>
오프라인에서 픽스처 없는 클립의 실제 동작: <관측 그대로>
테스트 9 수정: <단언 diff>
RUNBOOK 추가 분량: <줄 수, 목차>
게이트: <명령마다 실제 숫자>
diff 범위: <git diff --stat. vlogsrt/** 무변경인가>
멈춰야 했던 것:
```
