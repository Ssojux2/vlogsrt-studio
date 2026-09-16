# PROTO-4 — 러너 3/3: 블록 · 프레임 표본 · 비전 · EditPlan · EDIT 자막 · 패키지

> **v2.** v1은 (A)에서 **UNSOUND**, (B)에서 SOUND_WITH_CONCERNS를 받았다.
> **둘 다 같은 구멍을 찾았다** — `request_vision`의 `online` 게이트를 표현할 칸이
> 브리프에 없다는 것. §9에 기록. 전문: `../reviews/{opus,glm}-proto-4-direction.md`.

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 **`7cae54d`** (트리 깨끗, pytest 2477 passed)
venv: `.venv/bin/python`

**이 머신은 Xcode 라이선스 미동의로 툴체인 shim이 죽어 있다**(exit 69) —
`git`·`python3`·**`make`**. `make check`를 부르지 말고 §7의 여섯 명령을 개별로 돌려라.
(**`xmllint`는 정상이다** — `/usr/bin` 전체가 죽은 것이 아니다.)
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

## 2. 비전 호출 — 유일한 유료 호출이고, 경계는 하나이고, **기본이 닫혀 있다**

**계약 §4: openai는 `vlogsrt/transcribe.py`에서만 산다.** `transcribe.py`의
`create_vision_client(*, api_key, base_url)`이 이미 공급자 중립이라 D34의 Gemini
(`gemini-3.8-flash`, `GOOGLE_API_KEY`, `GEMINI_BASE_URL`)가 그대로 붙는다.

**러너는 클라이언트를 만들지 않는다 — 주입받는다.** PROTO-5의 `commands.py`가 만든다.
**모델 이름 리터럴을 러너에 쓰지 마라**(계약 §7). `project_config.vision_model`에서 온다.

### 2-a. `transcribe.request_vision`을 **이름으로** 부른다

```python
transcribe.request_vision(
    vision_client,
    model=analyzed.prepared.project_config.vision_model,
    payload=payload,
    online=online,
)
```

v1은 "주입된 클라이언트를 부른다"고만 썼다. 그 문구는 러너에서
`client.chat.completions.create(...)`를 직접 부르는 읽기를 열어 두고,
`check_openai_boundary`는 **import만** 보므로(`check_invariants.py:122`) 오리 타이핑
객체의 메서드 호출을 기계가 잡지 못한다. 직접 부르면 셋을 동시에 잃는다 —
`online` 게이트, 호출자가 모델을 고정한다는 계약, 그리고 `_to_dict` 정규화
(`transcribe.py:382-388`). **마지막 것이 조용하지 않게 터진다**: SDK 응답 객체는
`Mapping`이 아니라서 `_decision_fields`(`policy.py:362-363`)가 **전 블록을 오류
판정**으로 만든다. `vision/policy.py:4-5`와 `vision/__init__.py:6`이 이미
*"유일한 비전 호출 지점은 `vlogsrt.transcribe.request_vision`"* 이라고 못 박았다.

### 2-b. `online`은 러너의 인자다 — v1에 그 칸이 없었다 [두 검토자 공통, 치명]

```python
def request_vision(client, *, model, payload, online: bool = False) -> dict:
    if not online:
        raise RuntimeError("비전 호출은 명시적 online=True 없이는 닫혀 있다 — "
                           "오프라인 기본값을 넘는 것은 사람의 승인이다")
```

(`transcribe.py:441-456`. 직접 실행해 기본값이 raise하는 것을 확인했다.)

v1의 입력 넷에는 **승인을 표현할 칸이 없다.** 그러면 구현자가 고를 수 있는 길은
둘뿐이고 **둘 다 틀린다**:

1. 기본값을 쓴다 → 모든 블록이 `RuntimeError` → 전부 `status="error"` →
   **`final_accepted`가 영원히 거짓.** 실제 운영에서 한 번도 성공하지 못한다.
2. 러너에 `online=True`를 박는다 → **"승인의 부재"가 "항상 승인"이 된다.**
   `transcribe.py:445`가 세운 fail-closed가, 러너에 그 값을 표현할 파라미터가 없다는
   이유 하나로 무조건 열린다. PROTO-5는 러너를 **닫힌 채 부를 방법이 없어진다.**
   이것이 일곱 번째 `protected=[]`다.

**그러므로 §4 입력에 `online: bool = False`를 둔다.** 러너는 그 값을 그대로
`request_vision`에 넘긴다. `online=False`로 부른 러너는 블록마다 `status="error"`
판정을 남기고 `final_accepted`를 막는다 — **그것이 정직한 닫힘이다.**

v1은 이것을 *"`analyze_audio`가 `transcriber`를 주입받는 것과 같은 축"* 이라고 썼는데
**두 검토자가 함께 반박했다**: `Transcriber.transcribe`에는 이런 게이트가 없다
(`transcribe.py:70`). 비전 축에는 승인 게이트가 **하나 더** 있다.

## 3. 이 태스크의 핵심 위험 — 실패가 "판정 불필요"로 세탁되는 것

PROTO-1(`83dcc57`)이 export에 난간을 세웠다: **판정이 없거나(`decision_missing`)
오류이거나(`decision_error`) 모순이면 `final_accepted`가 참이 되지 않는다.**
그 난간의 값어치는 **이 단계가 정직할 때만** 나온다.

| 실패 | 반드시 이렇게 |
|---|---|
| 프레임 표본 실패 (`sample_frames` raise) | 그 블록에 **`status="error"` 판정**을 남긴다. 블록을 건너뛰지 마라 |
| 비전 전송 실패 (429·타임아웃·네트워크) | 같다 — **`status="error"` 판정.** 예외를 삼키지도, 밖으로 던지지도 마라 |
| 모델 응답 결함 | `parse_decision`이 **이미** 오류 판정으로 정규화한다(`policy.py:338`). 그 경로를 재구현하지 마라 |
| **`build_payload` 실패**(`policy.py:152`) | **잡아서 `status="error"` 판정.** 세 자리에서 `VisionPolicyError`가 난다 — 요청·샘플 불일치(`:277-282`), 이미지 읽기 `OSError`(`:288-294`), **바이트 해시 불일치**(`:295-299`). 안 잡으면 JPEG 한 장 때문에 프로젝트 전체 출판이 죽고 나머지 블록의 판정까지 버린다 |
| 설정 결함 | `parse_decision`의 `_require_config`가 **`try`(`policy.py:351`) 바깥**(`:349-350`)에서 raise한다 — 그대로 전파시켜라 |

`build_payload`와 `parse_decision`의 설정 검사는 **같은 예외 타입**이라 블록 루프
안에서는 구분할 수 없다. 그래서 **`policy._require_config(project_config)`를 블록 루프
앞에서 한 번 통과시켜라.** 그 뒤 루프 안의 `VisionPolicyError`는 전부 그 블록의 사실이다.

**오류 판정은 `policy._error_decision`(`:483-493`)을 재사용해 만들어라.** 그 함수가 정본
모양을 이미 안다 — `choice=KEEP_ALL`·`confidence=0.0`·`evidence=()`·
`request_sha256=request_sha256(request)`·`status="error"`. 손으로 다시 짜다 하나라도
어긋나면 `_require_decision_linkage`(`edit.py:1016-1030`)가 **raise해서 패키지가 아예 안
나온다** — "차단된 패키지"가 "패키지 없음"으로 바뀐다. private import는 이 저장소의
기존 관행이다(`edit.py:76`, `:79-81`).

> **블록을 건너뛰면 그 블록은 `decision_missing`이 되고, 그것도 차단이긴 하다.
> 그러나 "판정을 못 받았다"와 "물어봤는데 실패했다"는 다른 사실이다.**
> `reason`에 실제 오류 메시지를 실어 진단을 살려라. PROTO-2의 `_from_exception`과 같은 축.

**판정 목록에서 블록을 빼지 마라.** 파티션의 블록마다 판정이 정확히 하나 있어야 한다.

## 4. 시그니처

### 입력

- `analyzed: AnalyzedAudio`
- `vision_client` — 주입. 러너가 만들지 않는다
- `destination: Path` — 패키지 출판 뿌리
- **`online: bool = False`** — §2-b. 없으면 어느 길로 가도 틀린다
- `dtd_path: Path | None = None`

`Config`·`ProjectConfig`·문서·미디어·가드·경계는 **`AnalyzedAudio`가 이미 싣는다.**
프레임 디렉터리는 `PreparedAudio.frames_dir`에 있다. 다시 만들지 마라.

### 4-a. 블록의 `source_id`로 `ResolvedMedia`를 찾는 규칙을 못 박는다

`sample_frames`(`sampling.py:219-223`)는 `request.source_id == media.asset_content_id`를
강제한다. 블록이 드는 `EditBlock.source_id`(`edit.py:126`)는 **내용 신원**인데
`AnalyzedAudio`가 싣는 것은 `prepared.media` 튜플이고 `edit._media_by_resource`
(`edit.py:262`)는 `scoped_id`로 키잡아 도움이 안 된다.

그냥 조회가 아니다 — **서로 다른 xml_id가 같은 바이트를 가리키면 같은
`asset_content_id`에 `ResolvedMedia`가 여럿 온다**(PROTO-2가 이미 다루는 상황,
`runner.py:23-27`). 원본 바이트는 같아도 **프록시는 다를 수 있고**, 어느 쪽을 고르느냐가
`used_proxy`·추출 스트림·`review` 사유를 바꾼다.

> **규칙: `prepared.media`를 문서 순서로 훑어 `asset_content_id`가 같은 첫 항목.**
> 결정성이 이 한 줄에 달렸다.

### 4-b. `adjacent_context`는 `edit._PROJECTION_CONTEXT`(`""`)다

`request_sha256`(`policy.py:507-523`)의 재료는 도메인·`block_id`·`frame_ids`·후보뿐이라
`adjacent_context`는 **해시 재료가 아니다.** 그래서 러너가 무엇을 넣든
`_require_decision_linkage`(`edit.py:1025`)는 침묵한다 — 정본은 `_PROJECTION_CONTEXT`
(`edit.py:720-721`, `""`)로 재유도하기 때문이다.

하지만 모델이 보는 안내 문장은 달라진다(`_instruction`, `policy.py:311`
"이웃 맥락: {context}"). **구현자가 맥락을 재량으로 지어내는 것을 승인한 적이 없다.**
`""`로 고정해 러너가 보낸 요청과 검증자가 재유도하는 정본이 글자까지 같게 만들어라.

### 출력 — 동결 dataclass

- `analyzed: AnalyzedAudio`
- `partition: BlockPartition`
- `decisions: tuple[VisionDecision, ...]` — **블록마다 정확히 하나, 블록 순서 그대로**
- `plan: EditPlan`
- `findings` — `validate_edit_plan`의 결과. **성공한 결과에서는 언제나 `()`다**
  (`publish_package`가 `_require_renderable` 안에서 스스로 재검사해 findings가 있으면
  거부한다, `export.py:288-296`). "검사했음"의 기록으로만 유효하다
- `edit_captions: EditCaptionSet`
- `package: ExportPackage`
- **`dtd_path: Path | None`** — 실제로 쓴 값. `validate_xml`은 `None`이면 잘 형성됨만
  보고 `dtd_checked=False`를 돌려주는데 `_write_revision`이 그것을 **버린다**
  (`export.py:822-828`). 패키지 기록 어디에도 "DTD를 대조했는가"가 남지 않는다 —
  러너가 노출하는 것이 유일한 완화다. export 쪽 수정은 이 태스크 범위 밖이다

### 4-c. 판정 검사는 **출판 앞**에 선다

세 단언(개수·순서·중복)을 `__post_init__`에만 두면 **`publish_package`가 `current`
포인터를 원자적으로 갈아끼운 뒤에야** 돈다(`export.py:642`). 결함이 실제로 났을 때
단언은 터지지만 **디스크에는 이미 그 결함으로 만들어진 리비전이 공개되어 있고**,
`publish_package`의 롤백(`:643-645`)은 자기 예외만 치운다.

**그러므로 같은 검사를 `build_edit_plan` 호출 앞에서 먼저 돌려라.** 파티션과 판정만
있으면 전부 잴 수 있다. `__post_init__`에도 남겨 두는 것은 좋지만, **난간은 출판 앞에
서야 난간이다.**

1. `len(decisions) == len(partition.blocks)`
2. `decisions[i].block_id == partition.blocks[i].block_id` (순서·신원)
3. 같은 `block_id`가 두 번 나오지 않는다 (1+2가 함의하지만 무해한 중복 난간이다)

**블록이 0개인 프로젝트는 정상이다** — 판정도 0개다. `decisions == ()`를 결함으로 읽지
마라(`export.py:718-722`가 같은 이유를 명시한다: `EditPlan`에 원래 블록 수가 없다).

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
5. **`online=False`로 정상 프로젝트를 돌리면 판정이 전부 error이고 `final_accepted`가
   거짓이다.** §2-b의 회귀를 영구히 막는 테스트다. v1에 있던 "설정 결함은 전파된다"
   테스트는 **공허해서 지웠다** — §9 참조.
6. **`build_payload` 실패**(샘플 이미지를 지우거나 바이트를 바꿔 해시를 깨라) →
   **그 블록만** error가 되고 나머지 블록은 그대로 흐른다.
7. 블록 0개 프로젝트 — 판정 0개로 정상 출판된다.
8. 세 단언이 각각 문다 — **`build_edit_plan` 앞의 검사와 `__post_init__` 양쪽에서**
   (위조 입력 red-first).
9. 전 리소스가 `declared_no_audio`인 프로젝트가 **끝까지 흐른다**(블록 0개, 비전 호출 0회).
   가짜 클라이언트를 "부르면 실패"로 꽂아 증명하라.
10. 같은 `asset_content_id`에 `ResolvedMedia`가 둘인 문서에서 **문서 순서 첫 항목**이
    쓰인다(§4-a). 결정적인지 확인하라.

### 테스트 재료 — 브리프가 말하지 않으면 여기서 막힌다

- **`sample_frames`는 진짜 ffmpeg를 부른다**(`probe_video_stream`·`extract_frame_index`,
  `sampling.py:41`). `test_runner_analysis.py`의 합성 재료는 PCM 바이트일 뿐 비디오가
  아니다. `tests/project/test_vision_sampling.py`가 쓰는 기법 그대로
  **`vlogsrt.project.vision.sampling` 이름공간에서 probe/extract를 갈아끼워라.**
- **가짜 `extract_frame_index`는 실제 바이트를 써야 한다** — `build_payload`가
  샘플 해시를 대조한다(`policy.py:295-299`).
- `tests/project/`에는 `__init__.py`도 `conftest.py`도 없고 `test_runner_analysis.py`는
  **수정 금지**다. 필요한 헬퍼는 새 파일에 **복제**하라. 공유로 뽑는 길은 없다.
- **`xmllint`는 이 머신에서 정상 동작한다**(rc 0, libxml 20913). 죽은 것은 Xcode
  툴체인 shim(`make`·`git`·`python3`)뿐이다. 출판 테스트가 `validate_xml`을 지난다.

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
실패 → error 판정이 만들어지는 자리: <file:line 셋(표본·전송·build_payload)>
request_vision 호출 자리: <file:line. online을 어떻게 넘기는가>
online=False 테스트: <실제 출력. final_accepted가 거짓인가>
final_accepted 차단 증거: <테스트 4의 실제 출력>
세 단언: <build_edit_plan 앞 + __post_init__ 양쪽의 file:line + red-first 증거>
게이트: <명령마다 실제 출력 숫자. 불변식은 센 값>
diff 범위: <git diff --stat. 소유 파일 둘뿐인가. PROTO-2·3 공개 API 무수정인가>
멈춰야 했던 것: <있으면. 없으면 "없음">
```

---

## 9. 방향 검토 기록 (v1 → v2)

(A) **UNSOUND** · (B) SOUND_WITH_CONCERNS. **둘 다 같은 구멍을 찾았고** (A)가 그것을
차단급으로 올렸다 — 옳다. 승인을 표현할 칸이 없으면 어느 길로 가도 틀리고, 그중 하나는
fail-closed 게이트를 영구히 연다.

### 뒤집힌 내 주장

| # | v1이 주장한 것 | 실제 |
|---|---|---|
| 1 | *"`analyze_audio`가 `transcriber`를 주입받는 것과 **같은 축**이다"* | **틀림. 두 검토자 공통.** `Transcriber.transcribe`에는 `online` 게이트가 없다(`transcribe.py:70`). 비전 축에는 승인 게이트가 **하나 더** 있고 v1은 그것을 표현할 칸을 안 뒀다 |
| 2 | 테스트 5 *"설정 결함은 전파된다"* | **공허하다.** (A)가 재현했다 — `ProjectConfig.__post_init__`의 파서(`config.py:119-133`)가 `policy._require_config`의 **상위집합**이라 그 검사를 실패시키는 `ProjectConfig`는 **존재할 수 없다.** 위조해도 `partition_blocks`가 첫 호출에서 먼저 raise한다(`edit.py:172`). 그 테스트는 초록이 되지만 아무것도 재지 못한다 — **§6에서 지웠다** |
| 3 | *"`/usr/bin` shim이 전부 죽어 있다"* | **너무 넓다.** `xmllint`는 정상이다(rc 0, libxml 20913). 죽은 것은 Xcode 툴체인 shim뿐이다 |
| 4 | 인용 `policy.py:342-346` | 독스트링을 가리켰다. 실제 자리는 `:349-350`(`try`는 `:351`) |

### (A)만 찾은 것

- **§2-a** — "주입된 클라이언트를 부른다"가 `client.chat.completions.create(...)` 직접
  호출을 열어 두고, `check_openai_boundary`는 import만 보므로 기계가 못 잡는다.
  그 길로 가면 SDK 응답이 `Mapping`이 아니라 **전 블록이 오류 판정**이 된다.
- **§4-a** — 블록의 `source_id`(내용 신원)로 `ResolvedMedia`를 찾는 규칙이 없다.
  같은 바이트의 리소스가 둘이면 **프록시가 다를 수 있어** 선택이 결과를 바꾼다.
- **§4-c** — 세 단언이 `__post_init__`에만 있으면 **출판 뒤에** 돈다. 결함 리비전이
  이미 `current`로 공개된 채 단언이 터진다.
- **§3** — `build_payload` 실패가 표에 없었다. 안 잡으면 JPEG 한 장에 프로젝트 전체가 죽는다.
- **§3** — `policy._error_decision` 재사용. 손으로 짜다 어긋나면 `_require_decision_linkage`가
  raise해 **"차단된 패키지"가 "패키지 없음"으로** 바뀐다.
- **§6** — 테스트 재료의 실제 비용(probe/extract 교체, 해시 때문에 실제 바이트 필요,
  헬퍼 복제 불가피).

### (B)만 찾은 것

- **`dtd_path=None`의 기록 소실** — 재현했다. `validate_xml`이 `dtd_checked=False`를
  돌려주는데 `_write_revision`이 **버린다**(`export.py:822-828`). review.json·trace.json
  어디에도 dtd 관련 키가 없다. "검사할 것이 없다"와 "검사가 약해졌다"가 기록상 구분되지
  않는다 — §4 출력에 실제로 쓴 `dtd_path`를 실어 완화한다.
- `findings`는 성공한 결과에서 **언제나 `()`** — `publish_package`가 스스로 재검사한다.
- 단언 3은 1+2가 함의한다(무해한 중복).
- `sample_frames`를 블록마다 부르면 같은 미디어를 블록 수만큼 다시 probe한다(현 설계로도 정합).

### (B)가 직접 재현한 PROTO-1 난간

합성 500프레임 클립으로 두 판을 출판했다. 승인 판정 → removed 1개, sequence duration
**20s → 13s**. 오류 판정만 → removed 0개, 20s 유지, **`final_accepted = False`,
blockers 정확히 `['decision_error']`.** §3·§6 테스트 4가 기대는 사슬이 실제로 문다.

### 두 검토자가 재현하지 못한 것

- **전 구간 실행.** `partition_blocks → build_edit_plan → publish_package`를 합성
  FCPXML로 끝까지 돌리지 않았다. (A)의 §4-c(출판 뒤 단언)·`request_sha256` 위조 raise·
  테스트 재료 제약은 전부 **정적 판단**이다.
- **실제 SDK의 429·타임아웃 예외 타입.** 오프라인이라 가짜 클라이언트 관습으로만 봤다.
  러너가 **어떤 예외를 잡을지**는 구현자가 정해야 한다 — SDK 타입을 import 없이
  분간해야 하므로 넓게 잡되 §3의 "설정 결함은 루프 앞에서 걸러진다"를 지켜라.
- **실제 FCP 1.14 DTD 대조.** 이 머신에 DTD가 없다.
