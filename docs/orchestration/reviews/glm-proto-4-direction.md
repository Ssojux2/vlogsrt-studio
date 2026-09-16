# PROTO-4 방향 검토 — (B) GLM 5.3

> 2026-09-16. `claude-glm --effort max --permission-mode plan`, 읽기 전용.
>
> **판정 SOUND_WITH_CONCERNS.** (A)와 **같은 `online` 구멍을 독립으로** 찾았고,
> `dtd_path=None`의 기록 소실을 혼자 재현했다. PROTO-1 난간이 무는 것도 재현했다 —
> 오류 판정만이면 `final_accepted=False`, blockers `['decision_error']`.

검증이 끝났다. 트리는 깨끃하고(커밋 없음, 수정 없음), 모든 재현은 `/tmp` 합성 입력으로만 이뤄졌다. 보고서 전문을 다음과 같이 출력한다.

---

```
방향 판정: SOUND_WITH_CONCERNS
```

브리프가 고정하는 파이프라인·시그니처·실패 규율은 실제 코드와 정확히 맞고,
PROTO-1의 난간이 오류 판정을 실제로 무는 것까지 재현으로 확인했다. 우려는 셋이다:
**`request_vision`의 `online=True` 게이트를 브리프가 다루지 않는 것**(비용 경계의
정책 결정이 구현자에게 떠넘겨짐), **`dtd_path=None` 기본값이 DTD 대조 건너뜀을
기록 없이 남기는 것**(재현됨), **`adjacent_context`의 출처를 브리프가 정하지 않는 것**.

---

## 시그니처 충분성: 축은 맞다. 소비자가 유도해야 하는 자리는 없다 — 단 정책 인자 하나가 비어 있다

브리프 §1이 참조하는 아홉 함수를 전부 열어 대조했다. 행번호까지 전부 정확하다:

| 브리프 주장 | 실제 | 판정 |
|---|---|---|
| `partition_blocks(document, media, guards, locked, unavailable, cfg)` edit.py:158 | edit.py:158-165 | 확인 |
| `vision_request_for(block, adjacent_context)` edit.py:197 | edit.py:197-211 | 확인 |
| `sample_frames(media, requests, directory, cfg)` sampling.py:133 | sampling.py:133-148 — `media`는 **블록 단위 1개**(블록마다 호출) | 확인 |
| `build_payload(request, samples, cfg)` policy.py:152 | policy.py:152-177 | 확인 |
| `parse_decision(request, raw, cfg)` policy.py:338 | policy.py:338-355 | 확인 |
| `build_edit_plan(...)` edit.py:829 | edit.py:829-870 | 확인 |
| `validate_edit_plan(...)` edit.py:873 | edit.py:873-902 → `tuple[str, ...]` | 확인 |
| `project_edit_captions(plan, boundary)` captions.py:536 | captions.py:536-565 | 확인 |
| `publish_package(...)` export.py:603 | export.py:603-646, `source: BoundaryProjection` | 확인 |

긍정적 발견 두 가지:

1. **하류가 상위 값을 다시 유도하지 않는 구조가 이미 서 있다.**
   `_require_decision_linkage`(edit.py:1025-1030)가 러너가 만든 모든 판정의
   `request_sha256`을 정본 투영과 대조한다. `adjacent_context`는 해시 재료가
   아니므로(policy.py:496-523의 재료 목록에 없음) 러너가 무슨 맥락을 줬든 신원
   검사는 성립한다. 러너는 `request_sha256()`(공개, policy.py:496)로 전송 실패용
   오류 판정의 신원까지 직접 채울 수 있다 — P4-04 모양의 재유도 지점이 없다.
2. **입력 충분성.** `AnalyzedAudio`(runner.py:305-316)가 §4가 요구하는 것 전부를
   이미 싣는다: `prepared`(→ document·media·config·project_config·frames_dir
   runner.py:102-111), `instance_guards`, `locked_instance_ids`,
   `unavailable_instances`, `boundary`. 블록의 `source_id` → `ResolvedMedia` 매핑은
   값 유도가 아니라 조회다(같은 content id의 행은 같은 바이트).

**우려 1 — `request_vision`의 `online` 인자를 브리프가 침묵한다(중간).**
러너가 부를 유일한 전송 경계는 `transcribe.request_vision(client, *, model,
payload, online=False)`이고(transcribe.py:441-458), 이 함수는 **명시적
`online=True` 없으면 클라이언트를 만지기 전에 `RuntimeError`로 닫힌다**
(transcribe.py:452-456 — `test_vision_requests_fail_closed_without_explicit_online`
tests/test_transcribe.py:946-953, 이번 검토에서 재실행해 2 passed로 확인).
즉 러너는 가짜 클라이언트에조차 닿으려면 `online=True`를 넘겨야 하는데, 브리프 §4의
입력 목록엔 이 인자가 없고 §2에서도 언급이 없다. "analyze_audio가 transcriber를
주입받는 것과 **같은 축**이다"(§2)라는 단정은 **반박한다**: transcriber 축에는
이런 못 박는 게이트가 없다(`Transcriber.transcribe`에 online 인자가 없다,
transcribe.py:70). 구현자는 인도 없이 둘 중 하나를 골라야 한다 — 하드코딩
`True`(비용 게이트가 전부 클라이언트 생성의 PROTO-5로 이동) 아니면 러너 시그니처에
`online` 추가(브리프가 정하지 않은 매개변수). 기존 테스트 관습(tests/test_transcribe.py:958,
978 — 가짜 클라이언트에 `online=True`)은 후자와 이미 양립한다. 어느 쪽이든
**브리프가 결정해야 한다** — 최소 수정 명세 1번.

**우려 3 — `adjacent_context`의 출처를 브리프가 정하지 않는다(낮음).**
`vision_request_for(block, adjacent_context)`의 둘째 인자가 어디서 오는지 브리프에
없다. 계획 쪽 정본 투영은 빈 문자열로 못 박는다(`_PROJECTION_CONTEXT = ""`
edit.py:720). 해시가 이를 무시하므로 기계적으로는 무엇을 넣어도 통과하지만, 모델이
보는 안내 문장(`_instruction`, policy.py:311 "이웃 맥락: {context}")은 달라진다.
구현자가 재량으로 맥락을 지어내는 것을 브리프가 승인한 적은 없다 — 최소 수정 명세 3번.

## 부재가 권한이 되는 자리: 하나 재현했다 — `dtd_path=None`

**재현**(합성 입력, `/tmp`, 저장소 무수정): test_export.py의 `_publish` 헬퍼를
`dtd_path=None`(= 브리프 기본값)으로 출판한 뒤 review.json·trace.json의 모든 키를
재귀 순회했다:

```
review.json 최상위 키: [collapsed_cues, final_accepted, final_blockers, input_sha256,
  kind, plan_revision, review, unavailable_instances, unresolved_cue_ids,
  unresolved_cues, unresolved_request_ids]
trace.json 최상위 키: [artifacts, config_sha256, edit_captions, frame_duration,
  input_sha256, kind, plan, plan_revision, source]
dtd/valid/xmllint 관련 키: 없음
```

`validate_xml`은 `dtd_path=None`이면 잘 형성됨 검사만 하고 `dtd_checked=False`를
돌려주는데(xml_validation.py:71-73), `_write_revision`은 그 기록을 `valid` 게이트에만
쓰고 **버린다**(export.py:822-828). 즉 DTD 대조를 했는지 여부는 패키지 기록
어디에서도 회복 불가다. 브리프 §4가 러너 기본값을 `dtd_path: Path | None = None`으로
정하면, PROTO-5가 DTD 경로를 깜빡해도 **모든 패키지가 아무 신호 없이 약한 검사로
나가는 길이 상수가 된다**. `protected=[]` 계열의 일곱 번째는 아니지만(제거 권한이
풀리지는 않는다) 같은 뼈다 — "검사할 것이 없다"와 "검사가 약해졌다"가 기록상
구분되지 않는다. 러너의 소유 파일로는 export.py를 고칠 수 없으니 **러너 출력에
실제로 쓴 `dtd_path`(또는 검증 요약)를 노출**하는 것이 유일한 완화다 — 최소 수정
명세 2번.

덧붙여 확인한 안전한 쪽들: 블록의 `sample_requests`는 필수 표본 `{0, length−1}`을
항상 보장해(edit.py:623-633) 빈 `frame_ids` 블록이 파티션에서 나올 수 없고,
`publish_package`는 `_require_renderable` 안에서 `validate_edit_plan`을 **스스로
재돌아** findings가 있으면 거부한다(export.py:288-296) — 러너가 검사를 빼먹어도
출판이 통과하지 않는다. `decisions == ()` 금지의 근거도 확인했다: `EditPlan`
(edit.py:806-826)에 원래 블록 수가 없고 `_final_blockers`는 개수 검사를 하지
않는다고 못 박는다(export.py:718-721).

## 계약 · 불변식 · CLAUDE.md 규칙: 어긋난 곳 없음

- **§4(openai는 transcribe.py만)**: 러너는 주입받은 클라이언트로
  `transcribe.request_vision`을 부를 뿐 import가 없다. `create_vision_client`는
  값은 받은 것만 쓰고 공급자 중립이다(transcribe.py:419-438) — 브리프 주장 확인.
  D34 기본값 `gemini-3.8-flash`·`GOOGLE_API_KEY`·`GEMINI_BASE_URL`는
  project/config.py:87-89에 있다(§7 경로 안).
- **§5(subprocess 두 파일)**: `sampling.py`는 ffmpeg를
  `vlogsrt.preprocess`의 `probe_video_stream`·`extract_frame_index`로만 닿는다
  (sampling.py:41). xmllint는 `publish_package`가 `xml_validation` 경계로 부른다.
  러너 자체는 subprocess를 모른다.
- **§7(모델 리터럴)**: `project_config.vision_model` 사용 — 러너에 리터럴 없음.
- **§8(frozen)**: 새 출력 dataclass는 "동결"이라 브리프가 이미 못 박는다. 기계
  검사는 models.py 전용이므로(계약 §8) runner.py의 관습적 frozen과 같은 선이다.
- **§1·§6**: 러너의 새 import는 project 내부 + 레거시 공개 경계뿐 — 기존 패턴과 동일.
- **규칙 4·9·D35**: 파생 제거는 오직 `EditPlan`으로 표현되고, 이 단계의 실패
  처리(오류 판정·블록 유지)는 D35의 "빈 성공 금지"를 정확히 지킨다. 규칙 5는
  이 계층의 비전이 검증이 아니라 편집 판정 그 자체이므로 적용 밖이다.
- **불변식 20개**: 이번 검토에서 `scripts/check_invariants.py`를 직접 돌려
  `[OK]` 줄을 세었다 — **20**. 브리프 숫자와 일치한다(참고: 계약 문서의 "19군"
  문구가 하나 뒤져 있다 — 브리프가 옳다).

**PROTO-1 난간 재현(브리프 §3·§6 테스트 4의 핵심 의존)**: 합성 500프레임 클립으로
두 판을 출판했다. 승인 판정 → removed 1개 구간, `sequence duration` 20s→**13s**
(패키지가 실제로 짧아진다 — 테스트 1 메커니즘). 오류 판정만 → removed 0개,
길이 20s 유지, `final_accepted = False`, blockers 정확히 `['decision_error']`.
사슬: `_applied_candidate`이 status="error"를 만나 블록을 남기고
`_error_decision_review`(edit.py:1244-1245, 코드 정의 edit.py:717)를 내고,
허용 집합이 `{"unavailable_declared_no_audio"}`뿐인(export.py:87)
`_final_blockers`(export.py:735-743)가 이를 차단한다. 첫 재현에서
`decision_inconsistent`가 함께 뜬 것은 `_publish` 헬퍼의 기본 깃발
(`front_on_first_block=True`, test_export.py:799)과 겹친 **내 호출 부주의**다 —
깃발을 끄고 다시 돌려 위의 깨끗한 결과로 정정했다.

`parse_decision`의 설정 결함 전파 주장도 확인: `_require_config(cfg)`·
`_require_request(request, cfg)`는 오류 판정을 만드는 `try`(**policy.py:351**)
바깥(**policy.py:349-350**)에서 raise한다. 주장은 참이고 브리프의 인용
"policy.py:342-346"은 실제 자리에서 7줄 어긋난(독스트링을 가리키는) 인용이다.

## 의존성 · 순서: 문제 없음

- 선행 전부 HEAD에 있다: `AnalyzedAudio`(PROTO-3, 7cae54d), 나란 함수들, 그리고
  `request_vision`·`create_vision_client`(P5-05) — **`request_vision`의 최초
  프로덕션 소비자가 이 러너다**(지금 호출자는 테스트뿐).
- PROTO-5(commands.py)는 클라이언트 생성이라는 올바른 이음매에서 이 결과물을
  소비한다. 폐기 예정인 옛 `p7-02-runner.md`의 `run_stage(..., allow_network...)`
  개념과 새로운 주입 축이 충돌하는 흔적은 없다 — 다만 그 옛 브리프의 `allow_network`
  가 `online` 문제의 뿌리 개념이었고, 새 브리프는 그 결정을 내리지 않았다(우려 1).
- 기준선을 전부 재검증했다: `pytest` **2477 passed in 41.14s**, ruff "All checks
  passed!", format "93 files already formatted", mypy "Success: no issues found in
  45 source files", 불변식 20. 트리 깨끗함 유지.

## 소유 파일 안에서 가능한가: 가능

`runner.py` 추가 + `tests/project/test_runner_publish.py` 신규만으로 §1·§6 전부
실현 가능하다. 테스트 재료 패턴도 갖춰져 있다 — `test_runner_analysis.py`의
`_prepared`/`_analyze`(205-236, 364-372)가 `AnalyzedAudio`를 손조립/생성하며,
블록 0개·비전 0회 경로(테스트 8)는 같은 파일의 declared_no_audio 재료로 이미
닫힌 적 있다. **예외**: dtd 기록 문제의 본체는 export.py(비소유)에 있으므로
러너에서는 노출만 가능하다(위).

## 코디네이터 주장 검토

| 주장 | 판정 |
|---|---|
| §1 아홉 함수 시그니처·행번호 | 확인(전부 정확) |
| `parse_decision`이 이미 오류 판정으로 정규화 | 확인(policy.py:338-355) |
| `_require_config`가 try 바깥에서 raise | 확인 — 실제 자리 policy.py:349-350(인용 342-346은 7줄 어긋남) |
| 블록 건너뛰기=decision_missing도 차단 | 확인(edit.py:1237-1238 → export.py:87) |
| `decisions == ()` 검사 금지 — EditPlan에 블록 수 없음 | 확인(edit.py:806-826, export.py:718-721) |
| "analyze_audio의 transcriber 주입과 같은 축" | **반박** — 비전 축에는 `online=True`라는 못 박는 게이트가 하나 더 있고(transcribe.py:452-456) 브리프가 그 결정을 내리지 않는다 |
| `create_vision_client` 공급자 중립·D34 접속 | 확인(transcribe.py:419-438, config.py:87-89) |
| frames_dir은 PreparedAudio에 | 확인(runner.py:110, 169, 234) |
| 블록 0개 프로젝트 정상·`decision_missing` 아님 | 확인 — 개수 검사 자체가 없다(재현: 오류 판정 사례의 blockers에 개수 코드 없음) |
| 비전이 유일한 유료 호출 | 확인(ffmpeg·xmllint 무료) |
| 기준 7cae54d · 2477 passed · 불변식 20 | 재현 확인 |
| §6 테스트 8 실현 가능 | 확인 — `test_runner_analysis.py`의 무음 프로젝트 재료 그대로 |

사소한 관찰: `__post_init__` 단언 3(중복 금지)은 단언 1+2로 수학적으로 함의된다 —
결함은 아니고 무해한 중복 난간이다. 또한 `publish_package`가 findings 비었음을
스스로 강제하므로(export.py:288-296) 성공한 결과의 `findings` 필드는 언제나 `()`
이다 — "검사했음"의 기록으로는 유효하다.

## 최소 수정 명세 (심각도 순)

1. **`online` 결정을 브리프가 명시하라(중간).** 러너 시그니처에 `online: bool`
   키워드를 추가하거나(권장 — test_transcribe의 가짜 클라이언트 관습과 양립),
   "러너는 `request_vision`에 `online=True`를 못 박는다 — 비용 게이트는
   클라이언트 생성(PROTO-5)에 있다"를 근거와 함께 못 박는다. 인도 없으면 구현자가
   비용 경계의 정책을 임의로 정하게 된다.
2. **`dtd_path=None` 기본값의 기록 노출(낮음-중간, 재현됨).** 출력 dataclass에
   실제로 쓴 `dtd_path`(또는 출판 후 `validate_xml` 요약)을 실어 PROTO-5가
   "DTD 대조 없이 출판됨"을 보고할 수 있게 하라. export.py의 `dtd_checked` 폐기
   수정은 이 브리프 범위 밖임을 함께 적어라.
3. **`adjacent_context`를 못 박아라(낮음).** 정본 투영과 같은 빈 문자열
   (edit.py:720)로 고정하거나 출처를 지정하라. 재량 지어내기를 승인한 적 없다.
4. **(안내) findings 비었음은 publish가 강제한다** — 러너는 전파만 하고 성공
   결과의 `findings`는 언제나 `()`임을 브리프에 한 줄 남겨 혼선을 끊는다.
5. **(선택) 블록마다 `sample_frames`를 부르면 같은 미디어의 프로브가 블록 수만큼
   반복된다**(sampling.py:145 `establish_correspondence` per call). 오류 고립
   규율(테스트 2)과 충돌하지 않는 선에서 소스별 묶음도 고려할 수 있으나 현 설계
   그대로도 정합하다.

## 재현하지 못한 것

- **실제 openai SDK의 429·타임아웃 예외 유형** — 오프라인 환경이라 가짜
  클라이언트 관습(tests/test_transcribe.py:982-994)만으로 확인했다. 러너가 어떤
  예외 타입을 잡아 오류 판정으로 돌릴지(SDK 타입을 import 없이 분간하는 문제)는
  구현에서 결정해야 하며, 브리프도 이를 지정하지 않는다(명세 1과 같이 보라).
- **실제 Final Cut Pro 1.14 DTD 대조** — 이 머신에 DTD가 없다(`INSTALLED_DTD`
  부재, test_export.py:79-84의 skip 조건). dtd 재현은 "기록에 필드가 없다"의
  단문 증명으로 대신했다.
- **PROTO-5(commands.py)의 최종 소비 형태** — 아직 존재하지 않아 문서와
  구멍(옛 p7-02)만으로 판단했다.
