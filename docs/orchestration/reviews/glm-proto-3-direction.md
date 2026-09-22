# PROTO-3 방향 검토 — (B) GLM 5.3

> 2026-09-16. `claude-glm --effort max --permission-mode plan`, 읽기 전용.
>
> **판정 SOUND_WITH_CONCERNS.** (A)와 **같은 주장을 독립으로 반박**했고 —
> 기존 테스트 실행으로 재현했다 — 무오디오 프로젝트의 세션 게이트를 혼자 찾았다.

검토가 끝났다. 요약 줄이 삼켜진 `-q`와 달리 수집 수 2459·실행 종료 코드 0으로 기준선도 확인했다. 보고한다.

---

```
방향 판정: SOUND_WITH_CONCERNS
```

**시그니처 충분성: 충분하다 — 하류 재유도 자리는 발견하지 못했다.**
`partition_blocks(document, media, guards, locked_instance_ids, unavailable_instances, cfg)`(`edit.py:158-165`)와 `build_edit_plan(document, partition, guards, decisions, cfg)`(`edit.py:829-835`)가 요구하는 전부를 제안 dataclass가 이미 싣는다: document·media·cfg는 `prepared`에, guards·locked·unavailable은 필드로. §1의 호출 열 개 시그니처를 전부 원문에서 대조했다 — `transcribe_unique_sources(items, cfg, transcriber, artifact_dir)`(speech.py:722-727, 전사기 주입 ✓), `project_source_captions(document, media, traces)`(captions.py:380-384), `logical_slices(document, media)`(boundary.py:226-228), `plan_boundaries(slices, source, traces, references)`(boundary.py:949-954), `transcribe_boundaries(requests, references, source_traces, cfg, transcriber, artifact_dir)`(boundary.py:1116-1123), `project_boundaries(plan, transcripts, source, source_traces)`(boundary.py:2228-2233), `bundled_artifact(cfg)`(runtime.py:79)·`create_cpu_session(artifact)`(runtime.py:115), `infer_vad(reference, source_id, cfg, session: object)`(inference.py:86-88, 가짜 세션 주입 가능 ✓), `build_source_guard(trace, reference, vad, boundary, repairs, cfg)`(guard.py:160-167)·`project_guard(source_guard, reference, slices, frame_duration)`(guard.py:212-217). `PreparedAudio`가 legacy `Config`와 `ProjectConfig` 둘 다 실으므로 전사(legacy cfg)와 VAD/가드(project cfg)의 이중 설정도 재독기 없이 흐른다. 브리프 인용 줄번호는 전부 정확(예외 아래: 코디네이터 주장 검토 첫 항).

**부재가 권한이 되는 자리: 브리프가 짚은 일곱 번째 자리는 실재하고 처방은 옳다. 여덟 번째 후보는 찾지 못했다.**
- guard.py 모듈 문서가 직접 말한다: “선언된 무음·pre-PCM 부재 자원은 애초에 참조가 없으므로 이 모듈의 어느 호출도 없다”(guard.py:27-30). 러너에서 합성하는 것이 **설계된 자리**다. `InstanceGuard.status`에 `"inaudible"` 리터럴이 있다(guard.py:137). 세 reason_code가 소스쪽(runner.py:222)과 인스턴스쪽(boundary.py:444-448, `edit.py:90`)에서 같은 셋으로 정확히 정의되어 브리프의 표는 완전하다.
- `partition_blocks`는 success가 아닌 가드에 제거 권한을 안 준다(`edit.py:383-389`) — `protected_frames=()`가 안전한 이유는 status가 막기 때문이라는 브리프의 서술 그대로.
- 내 쪽 추가 소인(스윕): `infer_vad`는 어떤 실패도 예외가 아니라 error 결과로 접는다(inference.py:96-113) → 참조 있는 소스의 VAD 부재는 불가능. error 트레이스는 `_blocked_reviews`에서 keep_all(guard.py:576-589), 빈 트레이스는 `_uncertain_spans`로 보호(guard.py:328-334), VAD 창 덮개 구멍도 keep_all(guard.py:592-623). `vad_by_source`의 빈 키는 권한이 아니고 가드가 합성되므로 구멍 아니다.

**계약·불변식·CLAUDE.md 규칙: 어긋난 곳 없음.**
§1 — 러너의 `vlogsrt.transcribe` import는 project→레거시 공개 경계이고 선례가 있다(speech.py:51). §4 — openai 없음. §5 — subprocess 없음. §6 — numpy/onnxruntime는 vad 안에서만 import되고(runtime.py:141-142, 258, 281; inference.py:144 — 전부 함수 몸통 안) 러너는 공개 함수만 부른다. §7 — 모델 리터럴 없음. §8 — 기계 검사는 `models.py` 전용이므로(계약 문서 137-147행, `PreparedAudio` 주석 runner.py:66-68이 같은 사실을 말한다) 러너의 새 dataclass는 문서로만 동결을 지킨다 — 브리프가 “동결 dataclass”를 요구한 것과 정합. `check_invariants.py` 실행: **`[OK]` 20줄** 읽음. 규칙 4(`plan_source_ranges` 불가침, speech.py:223-255는 `[0, sample_count)` 무결)·5(verify 부재)·8(새 임계값 없음)·9·D35 모두 존중된다. 슬라이스가 `_continues`의 여섯 조건에 가청 동질성을 포함하므로(boundary.py:316) guard의 슬라이스 단위 상태와 edit의 클립 단위 `_audible`(edit.py:404-406)이 어긋나지 않는다.

**의존성·순서: 문제 없음.**
기준선을 직접 확인했다: HEAD `d8eed17`(PROTO-2), 트리 깨끗함, 전체 pytest 종료 코드 0, 수집 2459(요약 줄을 `-q`가 삼켜 `--collect-only`로 셈 — 브리프 DoD 7절의 경고 그대로). PROTO-2 공개 API 세 셋 모두 존재(runner.py:63-108, 102-108, 111-145). PROTO-5 의존 없음 — 전사기는 주입이고 테스트용 가짜는 `FixtureTranscriber`(transcribe.py:114) 패턴이 이미 test_speech.py:275에서 돈다. PROTO-4는 이 dataclass를 있는 그대로 먹는다.

**소유 파일로 가능한가: 가능.** 필요한 함수가 전부 기존 공개 심볼이고, 테스트 재료의 선례가 다 있다 — 합성 `PcmReference`·PCM·미디어(test_speech.py:84-140), 문서·가드·파티션 빌더(test_block_partition.py:83-213), 가짜 numpy/세션(test_vad_inference.py:83-198), monkeypatch 게이트(test_runner_audio.py:102-185). 추가 파일 불필요.

**코디네이터 주장 검토:**
1. **반박 — “`declared_no_audio`를 `keep_all`로 잘못 찍으면 그 자리에서 터진다”: 거짓.** `_require_audible_link`(edit.py:409-419)이 거부하는 것은 **success뿐**이고, docstring이 명시적으로 “keep_all·inaudible이 불가청 클립에 붙는 것은 보수 방향이라 견딘다(블록만 없어진다)”고 쓴다(edit.py:412-414). 재현: `pytest tests/project/test_block_partition.py::test_success_guard_on_inaudible_clip_raises ::test_ineligible_statuses_on_inaudible_clips_stay_silent` → **4 passed** — 후자는 `keep_all` 파라미터를 포함한다. 즉 inaudible 대 keep_all 구분은 **어디서도 기계 집행되지 않는다**. 브리프의 처방 표(inaudible) 자체는 옳고 더 강한 진술이지만, 그 구분을 지키는 것은 러너의 매핑과 테스트 #2·#3뿐이다. 테스트 #4(success 거부)는 유효하고 동등한 파티션 수준 테스트가 이미 있으므로(test_block_partition.py:380) 러너 seam에서의 회귀로 그대로 두면 된다.
2. 확인 — “기준 d8eed17, 2459 passed”(위). “`/usr/bin` shim 사망”(exit 69로 재현, CLT git 사용).
3. 확인 — `edit.py:968-971`("keep_all·inaudible은 빈 protected여도 제거 권한이 없다", 실제 968-972), `guard.py:160-167`, VadResult error → `_keep_all`(guard.py:178-180 → 671-684), “잠금·부재는 계획의 것을 그대로”(boundary.py:2239, 2250-2251).
4. **인용 오류(경미)** — “`partition_blocks`의 `_require_guard_linkage`(`edit.py:158`)”: 158은 `partition_blocks` 정의줄이고 `_require_guard_linkage`는 edit.py:327이며, 인용된 docstring 문장은 partition_blocks 것(edit.py:168-169)이다. 실질은 맞다.

**최소 수정 명세(심각도 순):**
1. **(중) 참조 중복 제거를 명세에 넣어라.** PROTO-2는 내용 신원 키잉으로 같은 바이트의 리소스 둘을 의도적으로 접는다(runner.py:21-25). 그런 프로젝트에서 `audio_inputs`의 참조들은 같은 `source_id`를 가진 채 둘 들어오고, `plan_boundaries`의 `_Linkage`가 `_index_by`로 **중복 참조를 raise한다**(boundary.py:546-548 “소스 하나에 PCM 참조가 둘 이상이다”; `transcribe_boundaries`의 `_BoundaryLinkage`도 같은 선). 러너는 references를 `source_id`로, VAD 루프도 `source_id`로 1회 중복 제거해야 한다 — 조용한 손실이 아니라 크래시라 결함 등급은 낮지만 정당한 입력에서 터진다. 브리프에 이 줄이 없다.
2. **(중) §2의 keep_all 폭발 문장을 정정하라**(위 반박). 구현에는 영향 없으나, 없는 안전망에 기대게 두면 나중에 누가 표를 keep_all로 “단순화”해도 아무 게이트가 울리지 않는다.
3. **(하) 세션 생성 게이트.** 무오디오 프로젝트(전 리소스 `declared_no_audio`)는 다른 모든 단계를 끝까지 흐른다 — `prepare_audio`의 도구 게이트가 오디오 없으면 건너뛰고(runner.py:158-168), 전사는 전원 부재 기록, 가드는 전원 합성 inaudible. 그런데 세션을 무조건 만들면 안 쓸 VAD 때문에 numpy/onnxruntime extra가 없는 환경에서 죽는다. `_require_audio_tools`와 같은 축으로 “PCM 참조가 하나 이상일 때만 생성”을 권한다. 테스트 #6(생성 실패 = 한 번의 하드 실패)과 양립한다.
4. **(하) `build_source_guard`의 `repairs` 인자 명세 한 줄.** 브리프 흐름도에 없다. `trace.repairs`를 넘기거나 `()` — `_repair_spans`이 어차피 `trace.repairs`를 포함한다(guard.py:294-305). 경계 재전사 기록은 시계가 달라 금지라는 경계를 guard.py:170-174가 이미 그었으니 러너 문서에도 한 줄 남겨 실수를 막아라.
5. **(선택) 산출물에 `transcripts: tuple[BoundaryTranscript, ...]` 추가 여부.** “최소한” 목록에는 없고 PROTO-4는 필요 없다. 다만 이 단계가 아니면 디스크 캐시 재독으로만 회수되는 유일한 값이므로, 이후 trace·검토 단계의 요구가 예상되면 지금 넣는 편이 싸다.
6. **(참고)** 가드 순서: `project_guard`는 소스별·슬라이스 순으로 내놓으므로(guard.py:225-239), 소스가 인터리브된 문서에서 러너가 문서 순서로 재배열해야 `__post_init__` 요구가 성립한다 — dataclass에 dict(instance_id→guard)를 만들어 문서 클립 순으로 내는 것이 기계적이다. 구현 노트 수준.

**재현하지 못한 것:** 없다. 반박(keepall 관용)은 기존 테스트 실행으로 재현했고(4 passed), 나머지는 코드 원문 대조로 확증했다. 단 두 가지는 정적 추적만으로 확인했다 — (a) PROTO-3 러너 전체 경로의 종단 동작(구현물이 아직 없어 실행 불가), (b) 무오디오 프로젝트가 세션 없이 종단 흐른다는 것(각 단계의 게이트 조건을 읽어서 따랐을 뿐 실행 재현 안 함).
