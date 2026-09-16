# PROTO-2 방향 검토 — (B) GLM 5.3

> 2026-09-16. `claude-glm --effort max --permission-mode plan`, 읽기 전용.
> 구현자·검증자와 **또 다른** fresh 워커다 (`../direction-review-charter.md`).
>
> **판정 SOUND_WITH_CONCERNS. 반박 0건.** (A)가 놓친 것 하나를 혼자 찾았다 —
> 미디어 오버라이드가 러너 입력 시그니처에 없다는 것. 주장 8에서는 (A)와 갈렸고
> 코디네이터가 (A)의 손을 들었다 (브리프 v2 §9).

검증이 끝났다. 저장소는 무결하다(`git status --porcelain` 빈 줄, CLT git 경로 확인). 보고서 전문을 아래에 출력한다.

---

```
방향 판정: SOUND_WITH_CONCERNS
```

핵심 설계(쌍 튜플 산출물, 세 사유코드 매핑, 문서 순서 보존, 부재의 타입화)는
전부 코드로 재현해 확인했다. 우려는 사양의 빈칸 네 곳뿐이고 하나도 차단급이 아니다.

---

**시그니처 충분성: 소비자는 상류 정확값에 닿는다 — 재현 완료**

- `speech.py:215`가 `AudioSourceInput`을, `speech.py:722-727`이
  `transcribe_unique_sources`를 정확히 그 모양대로 고정하고 있다. 브리프 인용은
  줄 단위까지 정확하다.
- 합성 쌍 3종(declared_no_audio·stream·pcm)을 만들어 실제 `_validated_pairs`
  (`speech.py:758-781`)와 `transcribe_unique_sources`에 통과시켰다 — unavailable만
  넣으면 전사기는 절대 불리지 않고 입력 순서·길이 그대로 3개가 돌아온다. 러너가
  브리프대로만 만들면 하류에서 재유도할 값은 하나도 없다.
- `asset_content_id ≠ reference.source_id`인 쌍은 소비자가 거부한다(재현 확인).
  두 리스트로 쪼개 돌려주는 모양이면 소비자가 다시 묶어야 하고, 그때 이 검사가
  쌍 짝짓기 오류를 흘려보낸다 — 브리프 §2의 P4-04 경고는 근거가 확인된다.
- `ProjectDocument`가 `input_sha256`을 이미 싣는다(`models.py:85`). 브리프의
  "확인하라" 항목 — 재노출 불필요, 문서를 통째로 실으면 값은 다 따라온다.
- `boundary.transcribe_boundaries`(`boundary.py:1116-1123`)는 참조와 트레이스를
  **별개의 시퀀스**로 받는다. 쌍 튜플에서 `[r for _, r in pairs if ...]`로 꺼내면
  되는 모양 투영이지 값 재유도가 아니다 — 충분하다.

**부재가 권한이 되는 자리: 이 브리프 안에는 없다. 일곱 번째는 PROTO-3 경계에 있다**

- `probe_audio_stream`은 실패에 `RuntimeError`를 던진다(preprocess.py:187-201).
  `None` 반환 경로가 없으므로 "None → 제한 없음" 틈이 없다.
- `resolve_media`는 리소스를 절대 빼지 않고 예외로 죽는다(재현: 원본 없는 리소스 →
  `UnsupportedProject`). resolve 실패를 unavailable 기록으로 **꾸밀 수도 없다** —
  기록의 신원 필드가 전부 `ResolvedMedia`에서 오는데(speech.py:807-821이 미디어와의
  일치를 검사) 미디어가 없으면 만들 재료가 없다. 계획 문서 R-01 절이 같은 판정을
  명문화하고 있다.
- 진짜 위험은 이 태스크가 아니라 소비 지점이다: `declared_no_audio`는 완료·무과금,
  `stream/pcm_unavailable`은 재시도 대기라는 **처분 차이**. 기존 소비자는 이미 옳게
  분기한다(`captions.py:326-347` — 기록 없는 리소스는 "지어내지 않는다"며 큰소리로
  죽는다). 러너가 리소스를 목록에서 빼면 하류가 잡아내는 구조라, 방어가 이중이다.

**계약·불변식·CLAUDE.md 규칙: 어긋난 곳 없음**

- 계약 §5(브리프 §5와 동문): `check_invariants.py:957-983`·`_sources()`가
  `rglob("*.py")`로 훑으므로 신규 `runner.py`는 **자동으로** 검사에 들어온다.
  불변식 파일을 고칠 필요가 없다. §1(러너→레거시 단방향)·§4·§6·§7 모두 이 태스크와
  무관하거나 브리프가 이미 지킨다.
- 규칙 8: 러너에 필요한 임계값은 하나도 없다. `AudioPolicy`는
  `pcm.py:84-110`의 동결 기본값(오디오 순번 0·16kHz·s16le)을 그대로 쓰고 —
  `ProjectConfig`에는 오디오 정책 키가 없다(`config.py:388-415`의 파서 테이블에
  없음, 재확인) — 그래서 "실제로 쓴 AudioPolicy를 실어라"가 유일한 올바른 처방이다.
- 규칙 9·D35: 부재를 빈 성공이 아니라 타입으로 남기는 것이 브리프 §3의 전부이고,
  테스트 5(결과 길이 == 리소스 수)가 회귀 검사다.

**의존성·순서: 문제 없음**

- 선행 없이 시작 가능하다. 필요한 공개 함수 전부 존재한다: `parse_project`
  (fcpxml.py:141, xmllint 불필요 — `parse_project`는 ElementTree로만 읽는다),
  `resolve_media`(media.py:177), `probe_audio_stream`(preprocess.py:146),
  `build_reference`(pcm.py:158).
- PROTO-3/4 브리프는 아직 없다(docs/orchestration/tasks/에 proto-* 파일 없음) —
  이 브리프 §4가 곧 그 둘의 계약이 된다. R 페이즈가 나중에 speech.py를 고쳐도
  PCM·쌍 단계는 그대로 살아남는다(R-03은 전사 범위를 바꾸지 PCM 구축을 안 바꾼다).

**소유 파일로 가능한가: 가능. 추가 파일 불필요**

- `vlogsrt/project/__init__.py`는 문서 문자열뿐인 수출 목록 없는 패키지라 고칠 일이
  없다. mypy `files=["vlogsrt","ui"]`와 ruff가 신규 파일을 자동으로 포함한다.
- 테스트는 인라인 합성으로 충분하다: 문서 빌더 패턴(`test_media.py:40-105`),
  monkeypatch 패턴(`test_pcm.py:175`의 `setattr(pcm, "extract_pcm_s16le", ...)`,
  runner의 probe는 runner 모듈 네임스페이스를 patch). ruff 셀렉트(E,F,I,UP,B,C4,SIM,T20)
  에 SLF001이 없어 `_validated_pairs` 직접 import도 린트 무사하다.

**코디네이터 주장 검토 — 항목별**

| 주장 | 판정 |
|---|---|
| `speech.py:215`가 쌍 계약 고정 | **확인** — 줄 그대로 |
| docstring "P7이 문맥 있는 pre-PCM 실패를 잡아…"(speech.py:181-194) | **확인** — 인용 문장은 `speech.py:184-185`에 글자 그대로 |
| "이 타입을 만드는 제품 코드는 어디에도 없었다" | **확인** — `grep -rn "UnavailableSourceAudio(" vlogsrt/` 0건 |
| `_validated_pairs`는 신원으로 검사(경로·순서·이름 아님) | **확인 + 재현** — `speech.py:758-832` |
| 세 사유코드·상태 매핑표 | **확인 + 재현** — 타입 자체가 `no_audio↔declared_no_audio` 쌍을 강제(speech.py:207-211), 소비자가 `has_audio` 선언과의 일치를 강제(speech.py:822-832) |
| 같은 애셋 클립 여럿 → PCM 한 번(리소스 단위 해석) | **확인** — media.py:177-190이 `document.resources`를 돈다; 애셋 id 중복은 파서가 거부(fcpxml.py:366) |
| 22개 공개 진입점(parse_project … publish_package) | **확인** — 파이프라인 무대 함수 기준 정확히 22 (fcpxml1+media1+pcm1+speech3+boundary4+captions4+edit4+export4) |
| 불변식 20 / DoD 게이트 | **확인, 직접 실행** — `[OK]` 20, ruff check 통과, format 90파일 통과, mypy "Success: no issues found in 44 source files", pytest exit=0 (~2446건, 0 failed) |
| `/usr/bin` shim 전멸(exit 69) | **확인** — `git`이 Xcode 라이선스 오류로 죽는 걸 직접 봄. CLT 경로 git은 정상 |

반박할 주장은 없었다.

**최소 수정 명세 — 심각도순, 전부 낮음**

1. **결과에 실제로 쓴 `Config`·`ProjectConfig`(또는 지문)를 실으라.**
   PROTO-3은 `transcribe_unique_sources(..., cfg, ...)`에 넘길 레거시 `Config`가
   필요한데 브리프 §4는 그 담기를 요구하지 않는다. 러너가 받은 config를 PROTO-3이
   기본 경로에서 다시 로드하면 어긋날 수 있다. speech.py가 이미 `config_sha256`을
   trace에 싣는 선(speech.py:1008)과 같은 맥락이다.
2. **PCM 목적지 네이밍 제약을 한 줄 못 박아라.** `artifact_dir/<asset_content_id>`
   아래는 이미 source(speech.py:607)·boundary(boundary.py:1161, `<source_id>/boundary/…`)
   네임스페이스가 쓴다. PCM 포인터는 그 밖(예: `artifact_dir/pcm/<content_id>`)에.
   자원 둘이 같은 바이트를 가리킬 때 content_id 키 주소면 `_reusable_reference`
   (pcm.py:391-429)의 공짜 재사용도 따라온다.
3. **러너 함수 입력에 미디어 오버라이드를 명시하라.** 브리프는 결과물만 못 박고
   입력을 못 박는다. `Sequence[MediaOverride]`나 맵 경로 없이 file URI만 받는 함수이면
   실전 remap 경로가 사라진다(테스트만 보면 file URI로 충분하다 — test_media.py:161).
4. (정보) probe는 리소스마다 두 번 돈다 — 러너의 명시적 probe 1회 +
   `build_reference` 내부 `extract_pcm_s16le`의 probe 1회(preprocess.py:270).
   stream/pcm 분류를 위해 필요한 비용이니 그대로 두는 게 맞다.
5. (정보) 테스트 7은 `_validated_pairs`와 함께 공개 소비자
   `transcribe_unique_sources`까지 부를 수 있다 — unavailable 쌍만 넣으면 전사기가
   절대 불리지 않음을 재현으로 확인했다. 건전한 쌍까지 통과시키려면
   `speech.transcribe_source`를 스텁하면 된다.

**재현하지 못한 것**

- 실제 ffmpeg/ffprobe 바이너리로 `probe_audio_stream` 실패를 뽑아보지 못했다(읽기
  전용 검토 제약) — 실패가 `RuntimeError`임은 preprocess.py:173-201의 코드 경로로
  확인했고, 실패 분류 자체는 합성 입력 + 실제 소비자 함수로만 검증했다.
- P4-04 원사고의 직접 재현이 아니라, 그 실패 모양("소비자가 다시 묶는다")이
  소비자의 쌍 단위 신원 검사로 차단됨을 재현한 것이다.
- 전체 pytest의 요약 줄("-q" 출력)이 내 캡처에서 삼켜졌다. exit=0과 진행점 100%·
  F/E 부재로 0 failed를 판정했다 — 구현자 DoD에서 줄을 직접 보게 된다.
