# PROTO-4 방향 검토 — (A) 컨텍스트 분리 Opus

> 2026-09-16. `claude -p --model claude-opus-5 --effort high --permission-mode plan`, 읽기 전용.
>
> **판정 UNSOUND.** `request_vision`의 `online` 게이트를 표현할 칸이 브리프에 없어
> 어느 길로 가도 틀린다는 것을 실행으로 짚었다. 내 주장 넷을 반박했다 (브리프 v2 §9).

```
방향 판정: UNSOUND
```

근거는 한 가지 결함이 브리프의 공개 시그니처를 **작동 불가**로 만들고, 그 빈자리가
정확히 이 저장소가 여섯 번 겪은 "부재가 권한이 된다" 모양이기 때문이다. 나머지는 건전하다.

---

## 시그니처 충분성

### 【치명】 §4 입력에 `online`이 없다 — 비전 경계는 기본이 **닫혀** 있다

`vlogsrt/transcribe.py:441-461`:

```python
def request_vision(client, *, model, payload, online: bool = False) -> dict:
    if not online:
        raise RuntimeError("비전 호출은 명시적 online=True 없이는 닫혀 있다 …")
```

재현(저장소 미수정, `.venv` 그대로):

```
request_vision default RAISES: 비전 호출은 명시적 online=True 없이는 닫혀 있다 —
오프라인 기본값을 넘는 것은 사람의 승인이다
```

브리프 §4의 입력은 `analyzed` · `vision_client` · `destination` · `dtd_path` 넷뿐이다.
승인을 표현할 칸이 **없다.** 그래서 구현자가 고를 수 있는 길은 둘뿐이고 둘 다 틀린다:

1. `online` 기본값을 쓴다 → **모든 블록이 RuntimeError → 모든 블록이 `status="error"`
   → `final_accepted`가 영원히 거짓.** 실제 운영에서 이 러너는 한 번도 성공하지 못한다.
   그런데 브리프 테스트 1(정상 경로)은 이 사실을 **덮는다** — 가짜 클라이언트를
   `request_vision`보다 **아래**(client)에 꽂으면 게이트는 통과하고, 위(`request_vision`
   자체)에 꽂으면 계약 §4의 유일한 전송 경계를 우회한 것을 증명하는 셈이다.
   게이트는 전부 초록인데 계약이 소비자를 막는 P4-04 모양 그대로다.
2. 러너 안에 `online=True`를 박는다 → **"승인의 부재"가 "항상 승인"이 된다.**
   `transcribe.py:447`이 "`online=True`는 사람이 명시적으로 승인한 실행에서만 온다
   (기본은 닫힘)"라고 못 박은 fail-closed가, 러너에 그 값을 표현할 파라미터가 없다는
   이유 하나로 무조건 열린다. PROTO-5의 `commands.py`는 러너를 닫힌 채 부를 방법이
   없어진다. 이것이 일곱 번째 `protected=[]`다.

### 【중】 §2가 `request_vision`을 **이름으로 부르지 않는다**

§2는 "주입된 클라이언트를 부른다"고만 쓴다. `client.chat.completions.create(...)`를
러너에서 직접 부르는 읽기가 문법적으로 열려 있고, `scripts/check_invariants.py`의
`check_openai_boundary`는 **import만** 본다(`:122`) — 오리 타이핑 객체의 메서드 호출은
기계 검사가 잡지 못한다. 그 길로 가면 세 가지를 동시에 잃는다: `online` 게이트,
`model=`을 호출자가 고정한다는 계약(`transcribe.py:447-449`), 그리고 `_to_dict`
정규화(`transcribe.py:382-388`). 마지막 것은 조용하지 않게 터진다 — SDK 응답 객체는
`Mapping`이 아니라서 `_decision_fields`(`policy.py:362-363`)가 **전 블록을 오류 판정**으로
정규화한다. 즉 §2의 문구 하나가 1번 결함과 같은 결과를 낳는 두 번째 길이다.

`vlogsrt/project/vision/policy.py:4-5`와 `vision/__init__.py:6`이 이미 "유일한 비전 호출
지점은 `vlogsrt.transcribe.request_vision`"이라고 못 박아 두었다. 브리프가 그 이름을
쓰지 않은 것은 누락이다.

### 【중】 `sample_frames(media, …)`의 `media`가 어디서 오는지 브리프에 없다

`sampling.py:219-223`은 `request.source_id == media.asset_content_id`를 강제한다.
블록은 `EditBlock.source_id`(`edit.py:126`) 하나를 들고, 그건 **내용 신원**이다
(`media.py:242,328` — `original_sha256`에서만 나온다). `AnalyzedAudio`가 싣는 것은
`prepared.media` **튜플**이고, `edit._media_by_resource`(`edit.py:262`)는 `scoped_id`로
키잡아 도움이 안 된다. 러너는 `asset_content_id → ResolvedMedia` 대응을 **스스로**
세워야 한다.

그냥 조회가 아니다: 서로 다른 xml_id가 같은 바이트를 가리키면(`runner.py:23-27`,
`_unique_references`의 `:469-485`가 이미 다루는 상황) 같은 `asset_content_id`에
`ResolvedMedia`가 **여럿** 온다. 원본 바이트는 같지만 **프록시는 다를 수 있다** —
어느 쪽을 고르느냐가 `used_proxy`·추출 스트림·`review` 사유를 바꾼다. 브리프가 선택
규칙을 못 박지 않으면 결정성이 구현자의 dict 순회에 맡겨진다.

### 【중】 `adjacent_context`의 출처가 브리프에 없고, 아무 값이나 넣어도 해시가 같다

`policy.request_sha256`(`:507-523`)의 재료는 도메인 · `block_id` · `frame_ids` · 후보
아이디와 창뿐이다 — `adjacent_context`는 **재료가 아니다.** 한편 `build_edit_plan`은
`_require_decision_linkage`(`edit.py:1025`)에서

```python
expected = request_sha256(vision_request_for(named, _PROJECTION_CONTEXT))   # _PROJECTION_CONTEXT = ""  (edit.py:720-721)
```

로 정본을 재유도해 대조한다. 러너가 이웃 자막 글월을 실어 보내도 해시는 **통과한다** —
저장된 판정의 `request_sha256`이 "이 프레임과 이 후보를 봤다"는 것은 증명하지만
"모델이 실제로 본 글월"은 증명하지 못한다. 브리프가 `adjacent_context`를 이름만 쓰고
값을 정하지 않아, 구현자가 무엇을 넣든 신원 증명은 침묵한다.
브리프가 `edit._PROJECTION_CONTEXT`(= `""`)로 못 박아야 러너가 보낸 요청과 검증자가
재유도하는 정본이 **글자까지** 같아진다.

### 【소】 오류 판정의 정본 생성자가 private이다

`policy._error_decision`(`:483-493`)이 정본 모양을 이미 안다: `choice=KEEP_ALL`,
`confidence=0.0`, `evidence=()`, `request_sha256=request_sha256(request)`, `status="error"`.
러너가 이 다섯을 손으로 다시 짜면, 하나라도 어긋날 때 `_require_decision_linkage`
(`edit.py:1016-1030`)가 **raise**해서 패키지가 아예 안 나온다 — "차단된 패키지"가
"패키지 없음"으로 바뀐다. private import는 이 저장소의 기존 관행이다
(`edit.py:76`, `:79-81`이 `_effective_max_images`·`_require_config`를 그렇게 쓴다).
브리프가 `_error_decision` 재사용을 지시하는 편이 안전하다.

---

## 부재가 권한이 되는 자리

위 1번(`online` 부재 → 무조건 승인)이 본체다. 추가로 하나:

### `__post_init__`의 세 단언이 **출판 뒤에** 돈다

§4의 출력 dataclass는 `package: ExportPackage`를 필드로 싣는다. 따라서
`publish_package`(`export.py:603`)가 `_switch_pointer`(`:642`)로 `current` 포인터를
**원자적으로 갈아끼운 뒤에야** `__post_init__`의 세 단언이 실행된다.
판정 수·순서·중복 결함이 실제로 났을 때, 단언은 터지지만 **디스크에는 이미 그 결함으로
만들어진 리비전이 공개되어 있다.** `publish_package`의 롤백(`:643-645`)은 자기 예외만
치우고 이 경우는 잡지 못한다.

세 단언은 `build_edit_plan` **앞**에서도 전부 잴 수 있다(파티션과 판정만 있으면 된다).
난간은 출판 앞에 세워야 난간이다.

재현: 하지 못했다 — 합성 FCPXML로 `partition_blocks → build_edit_plan → publish_package`
전 구간을 실제로 돌리지 않았다. 필드 계산 순서에서 나오는 정적 판단이다.

---

## 계약·불변식·CLAUDE.md 규칙

- **계약 §4** — 위반은 아니지만 §2의 문구가 우회를 **열어 둔다**(위 참조).
  `check_openai_boundary`는 import만 보므로 기계 검사가 막지 못한다.
- **계약 §5·§6·§7·§8, §1** — 브리프가 깨뜨리는 곳 없음. `vision_model`을
  `project_config`에서 읽으라는 지시는 `check_no_hardcoded_model`
  (`check_invariants.py:1017`)과 정확히 맞는다.
- **20개 불변식** — 브리프가 건드리는 것 없음. 확인: 기준 `7cae54d`에서 `[OK]` **20**줄.
- **규칙 4** — 지켜진다. 러너는 구간을 고르지 않고 블록은 `partition_blocks`가 낸다.
- **규칙 9 / D35** — §3의 "블록을 건너뛰지 마라 · 실패를 error 판정으로"는 정확히 옳고,
  `edit.py:1244-1245 → :1330-1341 → export.py:735-743`으로 실제로 차단된다(아래 확인).
- **규칙 5(원본 불변)** — 컷 → 자막 순서가 D35의 "자동 컷의 미검토 상태는 후속 자막
  생성으로 해제되지 않는다"에 맞다. `project_edit_captions`는 이미 만들어진 전사를
  편집 타임라인에 **투영**할 뿐 재전사가 아니다(`captions.py:536`).

---

## 계약 밖의 실제 구멍 — `build_payload` 실패가 §3 표에 없다

§3의 표는 네 줄이다: 표본 실패 · 전송 실패 · 응답 결함 · 설정 결함.
**`build_payload`(`policy.py:152`) 실패가 빠져 있다.** 그 함수는 `VisionPolicyError`를
세 자리에서 던진다:

- `_samples_by_frame_id` — 요청 프레임과 샘플이 안 맞을 때(`:277-282`)
- `_image_data_url` — 이미지 읽기 OSError(`:288-294`)
- `_image_data_url` — 바이트 해시가 샘플 해시와 다를 때(`:295-299`)

브리프가 정하지 않았으므로 구현자는 둘 중 하나를 고른다:

- 잡는다 → 오류 판정. 안전하지만 §3의 마지막 줄("설정 결함은 전파")과 **같은 예외 타입**
  이라 구분이 불가능하다. 지금은 우연히 무해하다(아래 참조) — 그러나 그 무해함은
  브리프가 설계한 것이 아니라 상류가 우연히 막아 주는 것이다.
- 안 잡는다 → JPEG 한 장을 못 읽었다는 이유로 **프로젝트 전체 출판이 죽는다.**
  조용하지는 않지만, 나머지 블록의 판정까지 전부 버린다.

브리프가 이 줄을 명시해야 한다. 권고는 "잡아서 오류 판정 — 단, `_require_config`를
블록 루프 **앞에서 한 번** 미리 통과시킨 뒤에" 다.

---

## 의존성·순서

선행은 다 있다(PROTO-2 `d8eed17`, PROTO-3 `7cae54d`). 다음 단계(PROTO-5 `commands.py`)가
이 단계 산출물을 다시 쓰게 만드는 곳은 **하나** — 위 1번이다. `online`을 지금 넣지 않으면
PROTO-5가 러너 시그니처를 고쳐야 하고, 그러면 `__post_init__` 테스트까지 다시 쓴다.

---

## 소유 파일로 가능한가

**가능하다. 추가 파일 불필요.** 다만 브리프가 말하지 않은 비용이 둘 있다.

1. **테스트 1·2·4는 ffmpeg 경계를 갈아끼워야 한다.** `sample_frames`는
   `probe_video_stream`·`extract_frame_index`(`sampling.py:41`)를 실제로 부른다.
   `test_runner_analysis.py`의 합성 재료는 PCM 바이트뿐이고 **진짜 비디오가 아니다**
   (`_pattern_pcm`, `:159`). `tests/project/test_vision_sampling.py`가 이미 쓰는 기법 —
   "probe/extract를 sampling 이름공간에서 갈아끼운다"(그 파일 머리말) — 을 써야 하고,
   가짜 `extract_frame_index`는 `build_payload`의 해시 대조(`policy.py:295`)를 통과하도록
   **실제 바이트를 써야** 한다. 브리프가 "합성 입력만"이라고만 써서 이 제약이 안 보인다.
2. **`tests/project/`에 `__init__.py`가 없다.** `test_runner_analysis.py`의 `_document`·
   `_media`·`_prepared`(`:113`, `:142`, `:205`) 약 230줄 상당을 새 파일에 **복제**해야
   한다. 그 파일은 수정 금지이므로 공유 헬퍼로 뽑는 길이 없다.

환경 확인(브리프와 다름): `xmllint`는 **살아 있다** — `/usr/bin/xmllint`, `--version` rc 0,
libxml 20913. `/usr/bin/make`·`/usr/bin/git`만 Xcode 라이선스로 죽어 있다. 출판 테스트가
`validate_xml`을 지나므로 이 사실이 중요하다.

---

## 코디네이터 주장 검토

| 주장 | 판정 |
|---|---|
| `_require_config`가 `parse_decision`의 `try` **바깥**에서 raise한다 (`policy.py:342-346`) | **확인.** `:349`가 `try`(`:351`) 앞이다 |
| "설정 결함은 전파시켜라" = 테스트 5가 그것을 증명한다 | **반박.** 아래 별도 항목 |
| `parse_decision`이 응답 결함을 이미 오류 판정으로 정규화한다(`policy.py:338`) | **확인.** `:351-354` |
| `decision_error`가 차단 코드다 | **확인.** `edit.py:1244-1245 → :1337` → `export.py:740` (`_FINAL_REVIEW_CODES_ALLOWED = {"unavailable_declared_no_audio"}`, `:87`). 허용 집합 방식이라 자동으로 블로커다 |
| 블록 0개 프로젝트는 정상, `decisions == ()`를 결함으로 읽지 마라 | **확인.** `export.py:718-722`가 같은 이유를 명시한다 |
| `Config`·`ProjectConfig`·문서·미디어·가드·경계를 `AnalyzedAudio`가 이미 싣는다 | **대체로 확인.** 단 `source_id → ResolvedMedia`는 안 싣는다(위 참조) |
| 프레임 디렉터리는 `PreparedAudio.frames_dir`에 있다 | **확인.** `runner.py:110`, `:176-183`이 미리 mkdir한다 |
| 인용된 모든 `file:line` | **확인.** 경로만 축약형이다 — 실제는 `vision/sampling.py:133`, `vision/policy.py:152·338` |
| 기준 `7cae54d`에서 2477 passed | **확인.** 2477 collected |
| 불변식 `[OK]` 20줄 | **확인.** 20 |
| "`/usr/bin` shim이 전부 죽어 있다" | **반박.** `make`·`git`은 죽었지만 `xmllint`는 정상이다(rc 0) |

### 반박 상세 — 테스트 5는 **공허하다**

브리프 테스트 5는 "설정 결함은 전파된다(오류 판정으로 세탁되지 않는다)"를 요구한다.
그 테스트는 초록이 되겠지만 **주장하는 것을 재지 못한다.** 세 겹으로 막혀 있다:

1. `ProjectConfig.__post_init__`(`config.py:119-133`)이 모든 필드에 파일 경로와 같은
   파서를 돌린다. `vision_apply_confidence`는 `_parse_unit_float`(`:281-289`),
   `vision_max_images`는 `_parse_positive_int`(`:273-278`).
   **재현:**
   ```
   ProjectConfig(vision_apply_confidence=nan) -> ProjectConfigError … 유한해야 한다
   ProjectConfig(vision_max_images=0)         -> ProjectConfigError … 양수여야 한다
   ```
2. 그 두 파서는 `policy._require_config`(`:180-203`)가 보는 조건의 **진부분집합이
   아니라 상위집합**이다 — 유한·[0,1]·bool 제외·양의 정수. 따라서 **`policy._require_config`을
   실패시키는 `ProjectConfig`는 존재할 수 없다.**
3. 설사 `object.__setattr__`로 위조해도, `partition_blocks`가 **첫 호출**에서
   `edit._require_config`(`:172` → `:238`)로 `_require_policy_config`를 이미 돌린다.
   블록 루프에 닿기 전에 raise한다.

즉 §3 표의 마지막 줄이 가리키는 경로는 **도달 불가능**하다. 실제로 전파 가능한 설정
결함은 다른 것이다 — `vision_max_images=3`은 `ProjectConfig`가 **받아들이지만**
(재현: `-> constructed`) `edit._require_config`의 `>= 5` 레일(`edit.py:232-237`)이 막는다.
그것은 `partition_blocks`의 난간이지 러너의 핸들러가 아니다.

**결과**: 테스트 5는 러너가 설정 결함을 세탁하지 않는다는 것을 증명하지 못하고,
진짜 세탁 위험(위 "`build_payload` 실패가 표에 없다")은 **테스트되지 않은 채** 남는다.

---

## 최소 수정 명세 (심각도 순)

1. **§4 입력에 `online: bool`을 추가한다** — 기본값은 `False`. 러너는
   `transcribe.request_vision(vision_client, model=analyzed.prepared.project_config.vision_model,
   payload=payload, online=online)`을 부른다. `online=False`로 부른 러너는 블록마다
   `status="error"` 판정을 남기고 `final_accepted`를 막아야 한다 — 그것이 정직한 닫힘이다.
   테스트를 하나 더 요구하라: **`online=False`로 정상 프로젝트를 돌리면 판정이 전부
   error이고 `final_accepted`가 거짓이다.** 그 테스트가 1번 결함의 회귀를 영구히 막는다.
2. **§2에 `transcribe.request_vision`을 이름으로 못 박는다.** "주입된 클라이언트를
   부른다"를 "주입된 클라이언트를 `request_vision`에 넘긴다"로 고친다. 이유를 한 줄
   덧붙여라: 직접 호출은 `_to_dict` 정규화를 잃어 전 블록이 오류 판정이 된다.
3. **§3 표에 `build_payload` 줄을 추가한다** — "잡아서 `status="error"` 판정.
   단 `policy._require_config(project_config)`를 블록 루프 **앞에서 한 번** 통과시킨 뒤에."
   그리고 테스트 5를 "이미지 파일을 지우면 그 블록만 error가 되고 나머지는 흐른다"로
   바꿔라 — 현재 문구는 도달 불가능한 경로를 잰다.
4. **§4에 `source_id → ResolvedMedia` 선택 규칙을 못 박는다**: `prepared.media`를
   문서 순서로 훑어 `asset_content_id`가 같은 **첫** 항목. 결정성이 이 한 줄에 달렸다.
5. **§4에 `adjacent_context = edit._PROJECTION_CONTEXT`(`""`)를 못 박는다.**
   러너가 보낸 요청과 `_require_decision_linkage`가 재유도하는 정본이 글자까지 같아진다.
6. **세 단언을 `__post_init__`**에 더해 **`build_edit_plan` 호출 앞에서도** 돌린다
   (또는 그 앞으로 옮긴다). 지금 배치는 잘못된 리비전이 `current`로 공개된 **뒤**에 문다.
7. **§3에 `policy._error_decision` 재사용을 지시한다**(private import 관행은 `edit.py:76,79-81`).
8. §6에 명시하라: probe/extract는 `vlogsrt.project.vision.sampling` 이름공간에서
   갈아끼우고, 가짜 extract는 실제 바이트를 쓴다(`build_payload`가 해시를 대조한다).
9. §7의 환경 주석 정정: `xmllint`는 정상이다. `make`·`git`만 죽었다.

---

## 재현하지 못한 것

- 합성 FCPXML로 `partition_blocks → build_edit_plan → validate_edit_plan →
  project_edit_captions → publish_package` 전 구간을 실제로 돌리지 않았다. 따라서
  **`__post_init__`이 출판 뒤에 돈다**는 지적, **위조 `request_sha256`이
  `edit.py:1026`에서 raise한다**는 지적, **테스트 1이 probe/extract 교체 없이는 불가능하다**는
  지적은 전부 코드를 읽어 얻은 정적 판단이지 실행 증거가 아니다.
- `vision_max_images=3`이 `partition_blocks`에서 raise하는 것을 실행으로 확인하지
  않았다(`ProjectConfig`가 그 값을 **받아들인다**는 것만 실행으로 확인했다).
  `edit.py:232-237`의 정적 읽기다.
- 실제 비전 공급자 호출·픽스처 갱신은 하지 않았다(유료). `.env`는 읽지 않았고
  `VLOGSRT_OFFLINE`을 건드리지 않았으며 저장소를 수정하지 않았다.
