# PROTO-5 방향 검토 — (B) GLM 5.3

> 2026-09-16. `claude-glm --effort max --permission-mode plan`, 읽기 전용. 첫 실행은 네트워크 오류로 죽어 재디스패치.
>
> **판정 SOUND_WITH_CONCERNS.** 반영 완료 — 브리프 v2 §9.

방향 판정: SOUND_WITH_CONCERNS
```

브리프의 설계 자체(commands 단일 외관·비용 경계·자격증명 분리·정직한 오프라인 닫힘·종료코드 3단 분리)는 옳고 소유 파일 셋으로 구현 가능하다. 그러나 **브리프가 자기 자신의 규칙을 어기는 지점이 둘** 있다 — 둘 다 "구현이 브리프대로인데 결과가 브리프 어기기"가 되는, 정확히 P4-04의 모양이다. 아래 최소 수정 명세 1·2번.

---

## 시그니처 충분성

**한 곳 못 닿는다: `final_accepted`·blockers.**

- `ExportPackage`는 경로 다섯 개만 나른다 — `plan_revision, fcpxml, edit_srt, source_srt, project_trace, review`(export.py:107-116). `review`는 **review.json의 경로**이지 판정이 아니다.
- `PublishedProject`도 안 싣는다 — `analyzed, partition, decisions, plan, findings, edit_captions, package, dtd_path`(runner.py:678-685).
- 값은 `publish_package` 내부 `_review_payload`에서 **한 번만** 계산되어 파일로만 나간다(export.py:683-691). `final_blockers`의 입력 다섯 갈래(export.py:707-744)가 `plan.review`·`captions.review`·`source.unresolved_*`를 섞어 계산하므로, 소비자가 `decisions` 상태만으로 흉내 내면 **틀린 근사**다. `findings`로도 안 된다 — 성공 출판에서 언제나 `()`다(runner.py:670-672).
- 브리프 §4는 CLI가 `final_accepted`·blockers를 출력하고 종료코드 0/1을 그걸로 정하라고 요구하면서 **그 값을 어디서 얻는지 한 번도 말하지 않는다**. 구현자의 갈림길 셋: (a) `json.loads(published.package.review.read_text())` — 정기록을 읽는 것이므로 옳음. **기존 소비자가 이미 그렇게 한다**(test_runner_publish.py:493-494). (b) `export._final_blockers` import — 사/private 침범. (c) 근사 — 오답. 브리프가 (a)를 못 박아야 한다.

나머지는 다 닿는다: 리비전 경로 `package.fcpxml.parent`·다섯 경로, 판정 수 `len(published.decisions)`, 제거 수 `len(published.plan.removed)`(edit.py:823), DTD 여부 `published.dtd_path`(runner.py:685) 또는 CLI 자기 플래그.

## 부재가 권한이 되는 자리

**일곱 번째 `protected=[]`은 발견하지 못했다.** 브리프가 짚은 자리(키 없음 + `--online`)는 실제로 유일한 남은 자리였고, 봉쇄법도 올바르다:

- `env.get`은 없는 키에 `""`를 돌려준다(env.py:104-106) — `if not env.get(name).strip()`로 "빈 값=제한 없음" 경로가 안 생긴다.
- 오프라인 닫힘은 코드 추적으로 전 경로 확인: `request_vision(online=False)`가 클라이언트를 만지기 **전에** raise(transcribe.py:452-456) → 블록별 오류 판정으로 전환(runner.py:807, 812-822) → 리뷰 코드 `decision_error`(edit.py:717) → 허용 집합 판정에서 블로커(export.py:87-95, 735-743) → `final_accepted=False`. **기존 회귀 테스트가 이 정확한 동작을 이미 단언한다** — 판정 전부 error, `client.calls == []`, blockers `["decision_error"]`(test_runner_publish.py:583-598). `vision_client=None`이 안전한 이유도 여기 있다: 클라이언트는 결코 불리지 않는다.
- 파생 계층의 빈-권한 난간은 이미 타입으로 막혀 있다: `PreparedAudio.__post_init__`(runner.py:147-158), `AnalyzedAudio` 다섯 단언(349-422), `_final_blockers`의 허용 집합 판정(export.py:719-721는 스스로 "P4-04 모양"이라 부른다), 블록 0개는 정상(847-848).
- `--media-map` 부재: `overrides=()` → 해석기 기본 탐색 → 못 찾으면 `UnsupportedProject` 하드 실패로 올라온다(runner.py:21-24). 조용한 스킵 없음. ✓
- `--project-config` 부재: cwd 기본 경로 탐색(config.py:139-147) — 문서화된 동작이지만 **저장소 뿌리에 `vlogsrt.project.toml`이 실재한다**(확인함). pytest cwd가 뿌리이므로 테스트는 명시적 `--project-config` 또는 tmp cwd로 끊어야 한다.

## 계약·불변식·CLAUDE.md 규칙

브리프가 깨는 것은 없다. 인용 정확성 전부 확인:

- §1/불변식 18: 계약 문서 원문이 허용형을 정확히 못 박는다(PROJECT_CONTRACTS.md:27-39). **뮤테이션을 /tmp 사본에서 실제 재현**: `from .project import runner` 주입 → `[FAIL] 레거시는 project를 import하지 않는다` 1건; `from .project.commands import project_run` 주입 → `[OK]` 20·`[FAIL]` 0. 브리프의 ✅/❌ 표와 테스트 7의 근거 둘 다 성립한다. 검사는 순수 AST라 `commands.py` 부재 상태에서도 문다(check_invariants.py:339-374).
- §4: commands는 `get_transcriber`(transcribe.py:395-402)·`create_vision_client`(419-438)를 부를 뿐. §5: commands에 subprocess 없음. §6: commands는 numpy/onnxruntime를 모른다 — VAD 세션은 러너가 만들고(runner.py:519-534) 아티팩트는 꾸러미 자Resource라 네트워크가 없다(runtime.py:79-93, venv에 onnxruntime 1.30.0·numpy 2.5.3 설치 확인). §7: 모델명은 전부 config에서. 규칙 8: 종료코드 0/1/2는 임계값이 아니라 프로토콜 코드 — 하드코딩 금지와 충돌 없음.
- CLAUDE.md 규칙 4·5·9·D35: commands는 구간을 안 고르고, 검증 계층을 안 만들고, 조용한 소거 경로가 없다.

## 의존성·순서

문제 없다. PROTO-1..4 산출물이 HEAD에 전부 있다(러너 셋, runner.py:170-178·425-430·692-699). `commands.py`·`test_project_cli.py` 부재 확인(신규 태스크 전제 성립). P7-03이 나중에 표면을 갈아엎고 `run`을 폐기한다 — 브리프가 이를 `--help`·커밋 메시지에 남기라고 못 박은 것으로 충분하다. 잔여 긴장 하나: 확정 계획은 "`run` 단일 명령은 두지 않는다 — 사람 게이트를 우회한다"(plans/2026-09-13-cut-first-and-plugin.md:321-322)라고 못 박는데 PROTO-5는 `run --online`이 비전 판정을 자동 적용하는 길을 연다. 종료코드 1·눈에 띄는 알림·기록에 남는 미검토 상태가 완화지 제거는 아니다 — 프로토타입 브랜치에서 감수된 편차로 기록될 가치는 있다.

## 소유 파일로 가능한가

**가능.** 필요한 부품이 전부 존재한다: `load_config`(vlogsrt/config.py:248 — vlogsrt.toml 인식), `load_project_config`(project/config.py:136), `load_media_map`(media.py:78), 러너 셋, `transcribe.get_transcriber`·`create_vision_client`, `env.get`(env.py:104), `log`(rich). 테스트는 기존 패턴을 베낀다 — `_FakeVisionClient`·`_fake_frames`·`_default_whisper`·`_review_of`(test_runner_publish.py). 유료 호출 0회로 전 시나리오 조립 가능. 단 **주의**: `prepare_audio`의 `config=None`는 bare `Config()` 기본값이다(runner.py:196) — commands가 `load_config()`를 통과시켜야 사용자 vlogsrt.toml이 소스 분석 키의 설정 지문에 반영된다.

## 코디네이터 주장 검토

| 주장 | 판정 |
|---|---|
| 기준 `8ca214d`, 트리 깨끗, 2488 | **확인.** rev-parse `8ca214d6…`, porcelain 빈 줄, 수집 총계 2488(실행 아님 — 아래 참조) |
| Xcode 셸 shim exit 69(git·python3·make), 전체경로 git·`.venv` python 사용 | **확인.** 평범 `git --version` → exit 69 + 라이선스 문구; 전체경로 git 정상 작동 |
| 러너 셋 시그니처 | **확인.** runner.py:170-178, 425-430, 692-699 (브리프 표기와 동일) |
| 불변식 18 인용과 ✅/❌ 표 | **확인 + 재현.** /tmp 사본에서 금지형 [FAIL] 1건·허용형 [OK] 20 |
| `get_transcriber`가 비용 안전장치(transcribe.py:395) | **확인.** 395-402 |
| `create_vision_client` 독스트링(423-426)·STT 키 미상속 | **확인.** 419-438, 인용 문장은 424-425 |
| config.py:88-89 이름·기본값 | **확인.** |
| `cli.py:134-137` `_resolve_online` | **확인.** |
| PROTO-4 오프라인 = error 판정·`final_accepted=False`·`['decision_error']` | **확인.** 코드 추적 + 기존 회귀 테스트(test_runner_publish.py:583-598) |
| 레거시 명령 여덟 | **확인.** `_COMMANDS` 8개(cli.py:295-304) |
| 최종 표면 넷·`run` 없음 | **확인.** plans/2026-09-13-cut-first-and-plugin.md:314-322 |
| 불변식 `[OK]` 20 | **확인.** 실제 실행, 20 |
| (암묵) "레거시 `_resolve_online`을 **재사용**하거나"가 종료코드 표와 양립한다 | **반박.** 재현: `main(['run','nope.mp4','--online','--offline'])` → RuntimeError가 `main()`에 잡혀 **종료 코드 1**(cli.py:310-314). RuntimeError 외 예외(설정·미디어·자격증명 대부분)는 잡히지 않아 traceback으로 exit 1. 즉 브리프가 실패에 배정한 코드 2가 레거시 오류 경로로는 **불가능**하고, "막힌 패키지와 실패를 같은 코드로 묶지 마라"는 브리프 자신의 지시가 깨진다 |

## 최소 수정 명세

1. **(필수, 심각)** 종료코드 2의 집행 지점을 브리프가 못 박아라: 프로젝트 명령의 모든 실패 — 플래그 충돌·설정·미디어·자격증명·출판 예외 — 는 프로젝트 cmd 계층에서 잡아 2로 반환한다. `main()`의 `RuntimeError→1` 경로(cli.py:310-314)에 절대 흘러 들어가면 안 된다(재현 근거 위).
2. **(필수)** `final_accepted`·blockers의 출처를 못 박아라: 출판된 기록에서 읽는다 — `json.loads(published.package.review.read_text(encoding="utf-8"))`(기존 패턴 test_runner_publish.py:493-494). `export._final_blockers` 재유도·`findings`/`decisions` 근사를 명시적으로 금지하고, runner.py에 필드를 더하는 길(범위 밖)은 "러너가 부족하면 멈춰라"의 과발동이 아님을 밝혀라.
3. (권장) commands는 `load_config()`로 Config를 만든다 — `prepare_audio(config=None)`은 bare `Config()`다(runner.py:196).
4. (권장) `_resolve_online` "재사용"은 순환 import 위험(cli.py 최상단 `from .project import commands`와 꼬임) — 같은 문구 재구현 또는 cli가 `online: bool`을 계산해 넘기는 쪽으로.
5. (권장) CLI 테스트는 cwd 의존을 끊는다 — 저장소 뿌리에 `vlogsrt.project.toml`이 실재하므로 명시적 `--project-config` 또는 tmp cwd.
6. (정보) commands에 데이터클래스를 두면 frozen으로(§8의 문서 정신 — 기계 검사는 models.py 전용).

## 재현하지 못한 것

- **브리프 테스트 1·6의 CLI 종료코드 전체 경로** — 구현이 아직 없어 불가능. 대신 동일 동작의 하류 단언(test_runner_publish.py:583-598)과 코드 추적으로 갈음했다.
- **"pytest 2488 passed"(실행)** — 수집 총계 2488만 확인했다. 전체 실행은 돌리지 않았다(플랜 모드·시간 제약; 수정 코드가 없어 게이트로서 의미도 없다).
- **xmllint 정상 동작 주장** — 직접 돌려 보지 않았다.
- **ruff·mypy 게이트** — 이 검토에서 실행하지 않았다(대상 코드 부재).

---

재현에 쓴 /tmp 사본은 정리했다. 저장소는 시작 시와 동일하게 깨끗하다.
