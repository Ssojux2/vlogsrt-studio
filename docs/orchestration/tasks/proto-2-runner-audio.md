# PROTO-2 — 러너 1/3: 파싱 · 미디어 해석 · PCM, 그리고 부재의 기록

> **v2.** v1이 방향 검토 둘을 거쳤다(`../direction-review-charter.md`의 두 갈래).
> 둘 다 **SOUND_WITH_CONCERNS**. §9에 두 보고서의 합의·불일치와 뒤집힌 내 주장을
> 기록으로 남긴다. 전문: `../reviews/opus-proto-2-direction.md`.
>
> 프로토타입 라인. `docs/plans/2026-09-13-cut-first-and-plugin.md`의 R 페이즈는
> **미뤄져 있다** — 이 태스크는 **현재 순서(자막 → 컷)** 그대로 FCPXML 하나를 끝까지
> 돌리기 위한 러너의 첫 조각이다.

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`, 기준 `03e22ae` (트리 깨끗, pytest 2446 passed)
venv: `.venv/bin/python`

**이 머신의 `/usr/bin` shim이 Xcode 라이선스 미동의로 전부 죽어 있다**(exit 69).
`git`도 `python3`도 마찬가지다. 절대 경로를 써라:
`/Library/Developer/CommandLineTools/usr/bin/git`, 그리고 파이썬은 `.venv/bin/python`.

## 소유 파일 — 정확히 둘

- `vlogsrt/project/runner.py` (**신규**)
- `tests/project/test_runner_audio.py` (**신규**)

이 둘 밖의 무엇이든 고쳐야 한다고 판단되면 **멈추고 보고하라.** 특히
`vlogsrt/project/**`의 기존 모듈, `tests/fixtures/**`, `golden/**`, 임계값은 범위 밖이다.
**`AudioPolicy`를 `ProjectConfig`에 넣는 길은 고르지 마라** — `config.py`가 필요해진다.

---

## 1. 왜 이 태스크가 존재하는가

파이프라인의 **단계는 전부 있는데 아무도 잇지 않는다.** 22개 공개 진입점이 있고
(`parse_project` … `publish_package`) 러너가 없다. `vlogsrt/project/runner.py`는 **아직
존재하지 않는다.**

러너는 세 태스크로 나눠 쓴다. **같은 파일을 만지므로 병렬 불가, 순서대로 간다.**

| | 태스크 | 내용 |
|---|---|---|
| **1/3** | **이 태스크** | parse → media resolve → probe → PCM → **부재의 기록** |
| 2/3 | PROTO-3 | source STT → captions → boundary → VAD → guard |
| 3/3 | PROTO-4 | partition → 프레임 표본 → vision → edit → captions → export |

그 뒤 **PROTO-5(코디네이터)** 가 `vlogsrt/project/commands.py`와 `vlogsrt/cli.py`를 맡는다 —
계약 §1의 유일한 CLI 예외라 소유자를 명시해 둔다.

**이 태스크가 정하는 타입이 나머지 셋의 입력이다.** 그래서 이 브리프는 구현보다
**시그니처**에 무게를 둔다.

## 2. 하류가 정확히 무엇을 요구하는지 — 이미 정해져 있다

`speech.py:215`가 계약을 이미 고정해 놓았다:

```python
AudioSourceInput: TypeAlias = tuple[ResolvedMedia, PcmReference | UnavailableSourceAudio]

def transcribe_unique_sources(
    items: Sequence[AudioSourceInput], cfg: Config, transcriber: Transcriber, artifact_dir: Path
) -> tuple[SourceAnalysis, ...]: ...
```

**그러므로 이 단계의 산출물은 `tuple[AudioSourceInput, ...]`를 실어야 한다.**
새 모양을 발명하지 마라. `transcribe_unique_sources`는 쌍을 **경로·순서·이름으로 묶지
않고** 신원으로 검사한다(`_validated_pairs`, `speech.py:758-832`) — 쌍을 깨서 두 리스트로
돌려주면 소비자가 다시 묶어야 하고, **그때 이 신원 검사가 짝짓기 오류를 흘려보낸다.**
두 검토자가 이 점을 각각 재현했다.

## 3. `UnavailableSourceAudio`를 만드는 것이 바로 이 단계다

`speech.py:184-185`의 docstring이 명시한다 — *"P7이 문맥 있는 pre-PCM 실패를 잡아 이
기록을 만든다 — 여기서는 신원만 검사하고 그대로 흘려보낸다."* **그 P7이 이 러너다.**
`grep -rn "UnavailableSourceAudio(" vlogsrt/` 는 **0건**이다.

세 가지 `reason_code`를 각각 **어디서** 만드는지 못 박는다:

| `reason_code` | `status` | 언제 |
|---|---|---|
| `declared_no_audio` | `"no_audio"` | `ResolvedMedia.has_audio is False` — 리소스 선언 그대로. **probe하지 않는다** |
| `stream_unavailable` | `"error"` | `has_audio`는 참인데 `preprocess.probe_audio_stream`이 실패 |
| `pcm_unavailable` | `"error"` | probe는 됐는데 `pcm.build_reference`가 실패 |

`declared_no_audio`를 probe로 정하지 않는 것은 비용이 아니라 계약이다 — `media.py` 모듈
docstring이 *"`has_audio`는 정확히 그 리소스에서만 온다 — 확장자·바이트·프로빙으로
추론하지 않는다"* 고 못 박았다. `hasAudio` 누락은 파서가 이미 거부한다(`fcpxml.py:403`).

**`reason`에는 실제 예외 메시지를 실어라.** 빈 문자열이나 상수 문구는 진단을 죽인다.

세 형태는 `speech.py:800-832`가 **양방향으로 잠근다** — `has_audio=False`에 pre-PCM
오류를 붙이거나 `has_audio=True`에 `declared_no_audio`를 붙이면 그 자리에서 거부된다.
오라벨은 하류에서 잡히니, 여기서 옳게 짓는 것이 전부다.

### 3-a. 네 번째 코드를 지어내지 마라 [C5]

`resolve_media`가 내는 `UnsupportedProject`(원본 없음·일반 파일 아님 등,
`media.py:226`)는 **그대로 전파된다.** 세 코드는 **미디어 해석이 성공한 뒤에만** 시작한다.
이것은 `media.py`가 세운 **의도된 준비 게이트**다 — per-resource로 감싸면 하드 게이트가
retryable 소프트 기록으로 조용히 강등되고, 오프라인 미디어 하나가 재시도 대기로 위장된다.

### 3-b. 잡을 예외를 이름으로 못 박는다 [C6]

- `probe_audio_stream` 실패 → `RuntimeError` (`preprocess.py:173-201`)
- `build_reference` 실패 → `ValueError` / `RuntimeError` / `OSError`
- **`require_ffmpeg` 실패는 전파시켜라.**

**`except Exception` 금지.** 그러면 러너 자신의 버그와 **ffmpeg 미설치**까지 per-resource
`pcm_unavailable`이 되어, 환경 오류 하나가 N개의 부재 기록으로 번역되고 진단이 죽는다.

### 이것이 규칙 9와 D35의 자리다

> **어떤 리소스도 조용히 빠지지 않는다.** 실패한 리소스를 `continue`로 건너뛰거나
> 결과 목록에서 빼는 것은 금지다. 실패는 **타입으로** 남아 하류까지 간다.

이 저장소에서 `protected=[]` 계열 결함이 **여섯 번** 재발했다 — 전부 "없음"이
"제한 없음"으로 읽힌 것이다. 리소스를 목록에서 빼면 하류는 그 리소스가 **애초에
없었다고** 읽는다.

## 4. 시그니처 — 하류가 다시 유도하지 않게

### 입력 [GLM#3]

러너 함수는 최소한 다음을 **받아야** 한다:

- 입력 프로젝트 경로
- **`Sequence[MediaOverride]`** — 없으면 실전 remap 경로가 사라진다. `media.load_media_map`이
  이미 파일을 읽어 주므로 러너는 이미 읽힌 오버라이드를 받는다
- artifact 루트
- `AudioPolicy` — 기본 `AudioPolicy()` [C2]
- 레거시 `Config`와 `ProjectConfig`

### 출력

**동결 dataclass** 하나. 최소한 다음을 실어라 — 이름은 바꿔도 되지만 **값이 빠지면 안 된다.**

- 파싱된 `ProjectDocument` (`input_sha256`을 이미 싣는다 — `models.py:85`. 재노출 불필요)
- `tuple[AudioSourceInput, ...]` — §2 그대로, **문서 리소스 순서 유지**
- 실제로 쓴 `AudioPolicy`
- **실제로 쓴 `Config`와 `ProjectConfig`** [C3 / GLM#1] — 근거는 `AudioPolicy`와
  **글자 그대로 같다.** `_source_analysis_key(reference, cfg)`(`speech.py:1017-1032`)가
  `_config_sha256(cfg)`를 먹고, **그 키가 완성물 출판 자리·스크래치·세그먼트 신원·중복
  제거를 전부 정한다.** PROTO-3이 자기 config를 따로 읽으면 어긋난다
- artifact 하위 경로(PCM 목적지, source, boundary, frames)를 **만들어 놓고 경로로** 실어라
- `resource_scoped_id`로 찾는 매핑 — PROTO-3이 `for` 루프로 다시 찾게 만들지 마라

### 4-a. PCM `destination`은 `asset_content_id`로 키잡는다 [C1 — 내 v1 주장이 틀렸다]

v1은 *"같은 애셋을 클립 여럿이 써도 PCM 추출은 한 번 — `resolve_media`가 리소스 단위라
이미 그렇다"* 고 썼다. **틀렸고, 리소스 단위인 것이 바로 원인이다.**

클립 여럿 → 리소스 하나는 접히지만, **리소스 둘 → 내용 하나는 안 접힌다.**
`_reject_duplicate_id`(`fcpxml.py:366`)는 **중복 `xml_id`만** 거부한다 — 서로 다른 id를 가진
두 애셋이 같은 파일을 가리키는 것은 통과한다(검토자가 재현: scoped id 둘, content id 하나).
접는 것은 오직 `destination`이다 — `_reusable_reference`(`pcm.py:391-429`)가 그 포인터를
보고 재사용을 정하기 때문이다.

**그러므로:**

- PCM 포인터는 **`asset_content_id`로 키를 잡는다.** 아래 층이 이미 같은 축이다 —
  `speech.py:607`의 `namespace = artifact_dir / media.asset_content_id`.
- **다만 그 디렉터리를 그대로 쓰지 마라** [GLM#2]. `artifact_dir/<asset_content_id>` 아래는
  source(`speech.py:607`)와 boundary(`boundary.py:1161`)가 이미 쓴다.
  PCM은 별도 네임스페이스로: `artifact_dir / "pcm" / <asset_content_id>`.

### 4-b. frozen은 맞지만 기계가 지켜주지 않는다 [C7]

계약 §8(`check_project_models_frozen`)은 **정확 경로 `vlogsrt/project/models.py`에만**
걸린다 — `runner.py`는 그 검사 **밖**이다. frozen으로 만드는 건 여전히 옳지만
**게이트가 확인해 준다고 믿지 마라.**
(반대로 계약 §5의 subprocess 검사는 `_sources()`가 `rglob("*.py")`라 `runner.py`를
**자동으로** 검사한다 — 불변식 파일을 고칠 필요는 없다.)

### 4-c. 알려진 한계를 적어 둬라 [C8]

`AudioPolicy.stream_index = 0`은 **첫 오디오 트랙만** 듣는다. 카메라 + 랩마이크처럼
트랙이 둘 이상인 애셋에서 나머지는 듣지 않으며, **듣지 않았다는 기록이 없다.**
이것은 기존 `pcm.py`의 성질이지 이 태스크가 만든 것이 아니다 — 결과에 정책을 싣는 것이
유일한 완화이고, 한계를 docstring에 명시하라.

## 5. 지켜야 하는 계약

- **계약 §5 / 불변식**: `runner.py`는 `subprocess`를 import하지 **못한다.** ffmpeg·ffprobe는
  `preprocess.probe_audio_stream`(`preprocess.py:146`)과 `pcm.build_reference`(`pcm.py:158`)를
  통해서만 닿는다. 이 검사는 `runner.py`를 자동으로 본다(§4-b).
- **계약 §4**: openai는 `vlogsrt/transcribe.py`만. 이 태스크는 STT를 안 부른다.
- **계약 §6**: numpy/onnxruntime은 `project/vad/**`만. 이 태스크는 안 쓴다.
- **계약 §1**: 파생 계층은 레거시를 **한 방향으로만** 소비한다. `runner.py`가 레거시를
  import하는 것은 허용, 레거시가 `runner.py`를 아는 것은 금지.
- **계약 §7**: 모델 이름 리터럴 금지.
- **규칙 8(임계값 하드코딩 금지)** — **`AudioPolicy`는 예외다** [C2]. `Config`에도
  `ProjectConfig`에도 `stream_index`·`sample_width` 자리가 **없다**(`config.py`에는
  `vad_sample_rate`와 가격 필드뿐). 그래서 §4의 입력 파라미터로 받고 기본값은
  `pcm.py:94-96`의 동결 기본값을 그대로 쓰는 `AudioPolicy()`다. 그 밖의 숫자는 전부
  config에서 온다.
- **40줄 규칙**: 함수는 40줄 이내.
- **추상화 선행 금지**: `Protocol`·`ABC`·팩토리 금지.

## 6. 테스트 — 실패 우선

`tests/project/test_runner_audio.py`. 합성 입력만 쓴다 — `tmp_path` 아래 인라인으로
만들거나 기존 합성 헬퍼를 재사용한다(문서 빌더는 `test_media.py:40-105`, PCM 스텁은
`test_pcm.py:175`의 `setattr(pcm, "extract_pcm_s16le", ...)` 모양).
**`tests/fixtures/**` 를 만들거나 고치지 마라.**

반드시 덮을 것:

1. 정상 경로 — 오디오 있는 리소스가 `(ResolvedMedia, PcmReference)` 쌍이 된다.
2. `has_audio is False` → `declared_no_audio` / `no_audio`. **probe를 부르지 않았음을
   증명하라**(probe에 부르면 실패하는 스텁을 꽂아라).
3. probe 실패(`RuntimeError`) → `stream_unavailable` / `error`, `reason`에 원래 메시지.
4. `build_reference` 실패 → `pcm_unavailable` / `error`.
5. **혼합 프로젝트** — 정상 하나 + 실패 하나. 결과 길이가 **입력 리소스 수와 같다.**
   이것이 규칙 9 회귀다.
6. **[C1 개정] 서로 다른 `xml_id`를 가진 리소스 둘이 같은 바이트를 가리킬 때**
   PCM 목적지가 **하나로 접힌다**(추출이 한 번이다). v1의 "클립 여럿" 문장은
   `resolve_media`가 이미 접는 케이스만 봐서 이 결함을 못 잡았다.
7. 산출물이 `transcribe_unique_sources`의 `_validated_pairs`를 **실제로 통과한다**
   (그 함수를 직접 불러서 증명하라. 통과하는 모양이라고 *주장*하지 마라).
   unavailable 쌍만 넣으면 전사기가 한 번도 안 불리는 것까지 확인할 수 있다.
8. **[C4] 결과 dataclass의 `__post_init__`이** `len(audio_inputs) == len(document.resources)`와
   `audio_inputs[i][0].resource_scoped_id == document.resources[i].scoped_id`를 **단언한다.**
   그 단언을 깨는 위조 입력이 실제로 raise하는지 red-first로 증명하라.
   규칙 9를 *테스트가 기억해야 하는 것*에서 *타입으로 표현 불가능한 것*으로 옮긴다.
9. **[C5]** 해석 불가능한 미디어가 섞이면 `UnsupportedProject`가 **그대로 올라온다**
   (부재 기록으로 강등되지 않는다).

**단언을 약화해 통과시키지 마라.** 테스트가 빨가면 코드를 고친다.

## 7. DoD

각 명령을 **따로** 돌리고 **출력 그대로** 보고한다. 요약 줄을 믿지 않는다.

```bash
cd /Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype
.venv/bin/python -m pytest tests/project/test_runner_audio.py -q
.venv/bin/ruff check vlogsrt ui tests scripts
.venv/bin/ruff format --check vlogsrt ui tests scripts
.venv/bin/mypy
.venv/bin/python scripts/check_invariants.py | grep -c '^\[OK\]'   # 20 이어야 한다
.venv/bin/python -m pytest
```

불변식은 `[OK]` 줄을 **세어** 보고한다. 전체 pytest는 **2446 + 새 테스트 수**여야 하고
0 failed 여야 한다. **`-q`가 요약 줄을 삼키면 `-q` 없이 다시 돌려 줄을 직접 실어라.**

## 8. 보고 형식

```
커밋 SHA:
공개 시그니처: <실제로 고정한 입력·출력 타입. 그대로 붙여라>
세 reason_code가 만들어지는 자리: <file:line 셋>
PCM destination 키: <실제 경로 모양>
__post_init__ 단언: <file:line + red-first 증거>
게이트: <명령마다 실제 출력 숫자. 불변식은 센 값>
diff 범위: <git diff --stat. 소유 파일 둘뿐인가>
멈춰야 했던 것: <있으면. 없으면 "없음">
```

---

## 9. 방향 검토 기록 (v1 → v2)

두 검토자 모두 **SOUND_WITH_CONCERNS**. 핵심 골격(§2 쌍 튜플, §3 세 코드)은 **양쪽이
각각 실행으로 확인**했다.

### 뒤집힌 내 주장

| # | v1이 주장한 것 | 실제 |
|---|---|---|
| 8 | "같은 애셋을 클립 여럿이 써도 PCM 추출은 한 번 — `resolve_media`가 리소스 단위라 **이미 그렇다**" | **틀림.** 리소스 단위인 것이 원인이다. 리소스 둘 → 내용 하나는 안 접히고, 접는 것은 `destination` 키뿐이다 (§4-a) |
| 9 | §4 "동결 dataclass로(계약 §8)" | **틀림.** §8은 `models.py` 전용이라 `runner.py`는 기계 검사 밖이다 (§4-b) |
| 10 | §5 "임계값은 전부 `Config`/`ProjectConfig`에서" | **틀림.** 두 config 어디에도 `AudioPolicy` 자리가 없어 이 태스크가 골라야 하는 유일한 정책에 대해 만족 불가능하다 (§5) |

### 두 검토자가 갈린 지점 — 코디네이터 판정

주장 8에 대해 **(A)는 반박**(재현: scoped id 둘·content id 하나), **(B)는 확인**
("애셋 id 중복은 파서가 거부한다 — `fcpxml.py:366`")으로 갈렸다.

**(A)가 옳다.** `fcpxml.py:366`의 `_reject_duplicate_id`는 **중복 `xml_id`만** 거부한다.
서로 다른 id를 가진 두 애셋이 같은 파일을 가리키는 것은 거부 대상이 아니다 —
(B)가 다른 기제를 겨눴다. 직접 코드를 읽어 확인했다.

### 한쪽만 찾은 것

- **(A)만**: C4(`__post_init__`으로 규칙 9를 타입화), C5(네 번째 코드 금지),
  C6(예외를 이름으로), C7(§8 범위), C8(다중 트랙 한계).
- **(B)만**: **미디어 오버라이드가 입력 시그니처에 없다**(GLM#3) — remap 경로가 통째로
  사라질 뻔했다. 그리고 계약 §5 검사가 `rglob`이라 `runner.py`를 자동으로 본다는 확인.
- **둘 다**: `Config`를 결과에 실어야 한다(C3 / GLM#1), PCM 목적지 키(C1 / GLM#2).

**두 갈래로 돌린 값어치가 첫 실행에서 나왔다** — 한쪽만 돌렸으면 GLM#3이나 C4~C8 중
한 묶음을 통째로 놓쳤다.

### 두 검토자가 재현하지 못한 것

- 실제 ffmpeg/ffprobe로 `probe_audio_stream` 실패를 뽑지 못했다(읽기 전용 제약).
- **두 번의 독립 추출이 바이트 동일한 PCM을 내는지 확인하지 못했다.** 동일하면 C1은
  비용·디스크 문제이고, 동일하지 않으면 정합성 문제로 심각도가 오른다. 구현자가 §6
  테스트 6에서 이 갈림길을 닫아라.
- 다중 오디오 트랙 애셋이 없어 C8을 실측하지 못했다.
