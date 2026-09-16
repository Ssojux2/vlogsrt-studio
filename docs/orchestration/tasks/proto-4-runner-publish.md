# PROTO-4 — 러너 3/3: 블록 · 프레임 표본 · 비전 · EditPlan · EDIT 자막 · 패키지

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 **`7cae54d`** (트리 깨끗, pytest 2477 passed)
venv: `.venv/bin/python`

**이 머신은 Xcode 라이선스 미동의로 `/usr/bin` shim이 전부 죽어 있다**(exit 69) —
`git`·`python3`·**`make`까지**. `make check`를 부르지 말고 §7의 여섯 명령을 개별로 돌려라.
git은 `/Library/Developer/CommandLineTools/usr/bin/git`, 파이썬은 `.venv/bin/python`.

## 소유 파일 — 정확히 둘

- `vlogsrt/project/runner.py` (**기존 파일에 추가**. `prepare_audio`/`PreparedAudio`/
  `pcm_destination`/`analyze_audio`/`AnalyzedAudio`는 **고치지 마라**)
- `tests/project/test_runner_publish.py` (**신규**)

이 둘 밖은 범위 밖이다. 기존 `vlogsrt/project/**` 모듈, `tests/fixtures/**`, `golden/**`,
임계값, 그리고 `test_runner_audio.py`·`test_runner_analysis.py`.

---

## 1. 이 단계가 하는 일

`AnalyzedAudio`를 받아 **패키지를 출판**한다. 러너의 마지막 조각이다.

```
AnalyzedAudio
  → partition_blocks(document, media, guards, locked, unavailable, cfg)   edit.py:158
  → 블록마다:
       vision_request_for(block, adjacent_context)                        edit.py:197
       sample_frames(media, requests, directory, cfg)                     sampling.py:133
       build_payload(request, samples, cfg)                               policy.py:152
       ★ 비전 호출 (주입된 클라이언트)                                     §2
       parse_decision(request, raw, cfg)                                  policy.py:338
  → build_edit_plan(document, partition, guards, decisions, cfg)          edit.py:829
  → validate_edit_plan(...)                                              edit.py:873
  → project_edit_captions(plan, boundary)                                captions.py:536
  → publish_package(destination, document, media, plan, edit_captions,
                    source=boundary, guards, dtd_path=...)                export.py:603
```

## 2. 비전 호출 — 유일한 유료 호출이고 경계는 하나다

**계약 §4: openai는 `vlogsrt/transcribe.py`에서만 산다.** `transcribe.py`의
`create_vision_client(*, api_key, base_url)`이 이미 공급자 중립이라 D34의 Gemini
(`gemini-3.8-flash`, `GOOGLE_API_KEY`, `GEMINI_BASE_URL`)가 그대로 붙는다.

**러너는 클라이언트를 만들지 않는다 — 주입받는다.** PROTO-5의 `commands.py`가 만든다.
`analyze_audio`가 `transcriber`를 주입받는 것과 **같은 축**이다.

**모델 이름 리터럴을 러너에 쓰지 마라**(계약 §7). `project_config.vision_model`에서 온다.

## 3. 이 태스크의 핵심 위험 — 실패가 "판정 불필요"로 세탁되는 것

PROTO-1(`83dcc57`)이 export에 난간을 세웠다: **판정이 없거나(`decision_missing`)
오류이거나(`decision_error`) 모순이면 `final_accepted`가 참이 되지 않는다.**
그 난간의 값어치는 **이 단계가 정직할 때만** 나온다.

| 실패 | 반드시 이렇게 |
|---|---|
| 프레임 표본 실패 (`sample_frames` raise) | 그 블록에 **`status="error"` 판정**을 남긴다. 블록을 건너뛰지 마라 |
| 비전 전송 실패 (429·타임아웃·네트워크) | 같다 — **`status="error"` 판정.** 예외를 삼키지도, 밖으로 던지지도 마라 |
| 모델 응답 결함 | `parse_decision`이 **이미** 오류 판정으로 정규화한다(`policy.py:338`). 그 경로를 재구현하지 마라 |
| 설정 결함 | `parse_decision`의 `_require_config`가 **`try` 바깥**에서 raise한다 — 그대로 전파시켜라. 나쁜 설정이 "모델이 어겼다"로 세탁되면 안 된다(`policy.py:342-346`) |

> **블록을 건너뛰면 그 블록은 `decision_missing`이 되고, 그것도 차단이긴 하다.
> 그러나 "판정을 못 받았다"와 "물어봤는데 실패했다"는 다른 사실이다.**
> `reason`에 실제 오류 메시지를 실어 진단을 살려라. PROTO-2의 `_from_exception`과 같은 축.

**판정 목록에서 블록을 빼지 마라.** 파티션의 블록마다 판정이 정확히 하나 있어야 한다.

## 4. 시그니처

### 입력

- `analyzed: AnalyzedAudio`
- `vision_client` — 주입. 러너가 만들지 않는다
- `destination: Path` — 패키지 출판 뿌리
- `dtd_path: Path | None = None`

`Config`·`ProjectConfig`·문서·미디어·가드·경계는 **`AnalyzedAudio`가 이미 싣는다.**
프레임 디렉터리는 `PreparedAudio.frames_dir`에 있다. 다시 만들지 마라.

### 출력 — 동결 dataclass

- `analyzed: AnalyzedAudio`
- `partition: BlockPartition`
- `decisions: tuple[VisionDecision, ...]` — **블록마다 정확히 하나, 블록 순서 그대로**
- `plan: EditPlan`
- `findings` — `validate_edit_plan`의 결과
- `edit_captions: EditCaptionSet`
- `package: ExportPackage`

### `__post_init__`

PROTO-2·PROTO-3과 같은 축으로:

1. `len(decisions) == len(partition.blocks)`
2. `decisions[i].block_id == partition.blocks[i].block_id` (순서·신원)
3. 같은 `block_id`가 두 번 나오지 않는다

**블록이 0개인 프로젝트는 정상이다** — 판정도 0개다. `decisions == ()`를 결함으로 읽지
마라(PROTO-1 브리프가 같은 이유로 `decisions == ()` 검사를 금지했다: `EditPlan`에 원래
블록 수가 없어 그 검사는 정상 프로젝트를 죽인다).

## 5. 지켜야 하는 계약

- **계약 §4**: openai는 `transcribe.py`만. **주입된** 클라이언트를 부른다.
- **계약 §5**: `subprocess` import 금지. ffmpeg는 `sampling.sample_frames`가, xmllint는
  `publish_package`가 각자 경계를 통해 부른다.
- **계약 §7**: 모델 리터럴 금지 — `project_config.vision_model`.
- **계약 §1·§6**, **규칙 4·5·8·9**, **40줄 규칙**, **추상화 선행 금지**.
- **규칙 9 / D35**: 파생 영상의 제거는 `EditPlan`으로만 표현된다. 미처리·실패·경계
  잘림을 **빈 성공으로 바꾸지 않는다.**

## 6. 테스트 — 실패 우선

`tests/project/test_runner_publish.py`. 합성 입력만. **비전 클라이언트는 가짜, 유료 호출 0회.**
`AnalyzedAudio`를 손으로 조립하라(`test_runner_analysis.py`의 재료 패턴을 참고하되
그 파일을 수정하지 마라).

반드시 덮을 것:

1. 정상 경로 — 블록이 있고 판정이 `accepted`면 **패키지가 실제로 짧아진다**.
2. **프레임 표본 실패 → 그 블록에 `status="error"` 판정**이 남고 `reason`에 실제 메시지.
   블록이 목록에서 빠지지 않는다.
3. **비전 전송 실패(429 모양) → 같은 처리.** 예외가 밖으로 새지 않는다.
4. **그 두 경우 모두 `publish_package`의 `final_accepted`가 참이 아니다** —
   PROTO-1의 난간이 실제로 무는지 여기서 확인한다(`decision_error`가 차단 코드다).
5. 설정 결함은 **전파된다**(오류 판정으로 세탁되지 않는다).
6. 블록 0개 프로젝트 — 판정 0개로 정상 출판된다.
7. `__post_init__`의 세 단언이 각각 문다(위조 입력 red-first).
8. 전 리소스가 `declared_no_audio`인 프로젝트가 **끝까지 흐른다**(블록 0개, 비전 호출 0회).
   가짜 클라이언트를 "부르면 실패"로 꽂아 증명하라.

**단언을 약화해 통과시키지 마라. 임계값을 고치지 마라. 픽스처를 만들거나 고치지 마라.**

## 7. DoD

`make`가 죽어 있으므로 개별로 돌린다. 각 명령을 **따로**, **출력 그대로** 보고한다.

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
.venv/bin/python -m pytest tests/project/test_runner_publish.py
.venv/bin/ruff check vlogsrt ui tests scripts
.venv/bin/ruff format --check vlogsrt ui tests scripts
.venv/bin/mypy
.venv/bin/python scripts/check_invariants.py | grep -c '^\[OK\]'   # 20
.venv/bin/python -m pytest
```

전체 pytest는 **2477 + 새 테스트 수**, 0 failed. 불변식은 `[OK]` 줄을 **세어** 보고한다.

## 8. 보고 형식

```
커밋 SHA:
공개 시그니처: <입력·출력 타입 그대로>
실패 → error 판정이 만들어지는 자리: <file:line 둘(표본·전송)>
final_accepted 차단 증거: <테스트 4의 실제 출력>
__post_init__ 세 단언: <file:line + 각각의 red-first 증거>
게이트: <명령마다 실제 출력 숫자. 불변식은 센 값>
diff 범위: <git diff --stat. 소유 파일 둘뿐인가. PROTO-2·3 공개 API 무수정인가>
멈춰야 했던 것: <있으면. 없으면 "없음">
```
