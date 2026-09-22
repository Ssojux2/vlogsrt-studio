# PROTO-2 방향 검토 — (A) 컨텍스트 분리 Opus

> 2026-09-16. `claude -p --model claude-opus-5 --effort high --permission-mode plan`,
> 읽기 전용. codex 소진 후 `gpt-6-astra`의 자리를 잇는 첫 실행이다
> (`../verification-protocol.md`의 2026-09-16 절, `../direction-review-charter.md`).
>
> **판정 SOUND_WITH_CONCERNS. 코디네이터 주장 10건 중 3건을 반박했다**(C1·C7·C2).
> 같은 모델이라도 컨텍스트가 분리되면 반박이 나온다는 첫 증거다.


**Context.** 나는 이 브리프의 방향 검토자다. "구현이 브리프대로인가"가 아니라 "브리프가
애초에 옳은가"를 묻는다. 저장소는 고치지 않았다. 아래 모든 재현은
`/tmp/protorev/` 아래 합성 입력으로 돌렸고, `.venv/bin/python`을 썼다.

---

```
방향 판정: SOUND_WITH_CONCERNS
```

핵심 골격 — §2의 `tuple[AudioSourceInput, ...]`와 §3의 세 `reason_code` — 은 **옳고,
추측이 아니라 실행으로 확인했다.** 아래 아홉 건은 전부 그 골격 위의 빈칸이지 골격의
부정이 아니다. C1·C4가 머지 전에 반드시 닫혀야 할 둘이다.

---

## 시그니처 충분성

**하류 정확값에 닿는다 — 단, 두 자리가 비어 있다.**

닿는 것(확인함):

| 하류 호출 | 필요한 입력 | 브리프가 싣는가 |
|---|---|---|
| `logical_slices(document, media)` `boundary.py:226` | `ProjectDocument` ＋ `Sequence[ResolvedMedia]` | ✅ (문서 ＋ 쌍의 첫 원소) |
| `transcribe_unique_sources(items, cfg, transcriber, artifact_dir)` `speech.py:722` | 쌍 ＋ **`Config`** ＋ artifact 경로 | 쌍 ✅ · 경로 ✅ · **`Config` ❌ (C3)** |
| `plan_boundaries(slices, source, traces, references)` `boundary.py:948` | `Sequence[PcmReference]` | ✅ (쌍에서 `isinstance` 필터) |

`isinstance` 필터는 P4-04가 아니다 — 값을 **다시 유도**하는 게 아니라 이미 실린 값을
골라내는 것이다.

**비어 있는 자리 둘:**

- **`Config`·`ProjectConfig`를 싣지 않는다(C3).** 브리프 §4는 "`AudioPolicy`를 실어라 —
  PROTO-3이 기본값을 가정해 다시 만들면 안 된다"고 옳게 말해 놓고, **같은 논리가 그대로
  적용되는 두 config는 빼놓았다.** `_source_analysis_key(reference, cfg)`
  (`speech.py:1017-1032`)는 PCM 키에 `_config_sha256(cfg)`(`speech.py:1035-1038`)를 더해
  만들고, 그 키가 **완성물 출판 자리·스크래치·세그먼트 신원·중복 제거를 전부** 정한다
  (해당 docstring 그대로). PROTO-3이 자기 config를 따로 읽으면 §4가 `AudioPolicy`에
  대해 금지한 바로 그 행위다.
- **`AudioPolicy`의 출처가 없다(C2).** 아래 §부재 항목 참조.

---

## 부재가 권한이 되는 자리

**일곱 번째를 "빈 목록이 제한 없음으로 읽힌다"는 모양으로는 찾지 못했다.** 오히려 이
브리프가 닫는 쪽이다 — 아래 재현이 보여주듯 세 코드는 하류에서 실제로 문을 막는다.
다만 rule 9의 집행이 **타입이 아니라 테스트에만** 걸려 있는 것이 이 브리프가 남기는
가장 큰 구조적 빈틈이다(C4).

### 재현 1 — 세 형태는 통과하고, 두 오라벨은 거부된다 ✅ 브리프가 옳다

```
PASS   has_audio=False + declared_no_audio/no_audio
PASS   has_audio=True + stream_unavailable/error
PASS   has_audio=True + pcm_unavailable/error
REJECT has_audio=False + pcm_unavailable/error     -> 0번째 기록: has_audio=False인 미디어에 pre-PCM 오류 기록이 왔다
REJECT has_audio=True + declared_no_audio/no_audio -> 0번째 기록: has_audio=True인 미디어에 선언된 무음(no_audio)이 왔다
```

`speech.py:800-830`(`_require_unavailable_pair`)이 삼태를 양방향으로 잠근다. §3의 표는
**정확하고 하중을 받는다.**

### 하류 결과 — 코드가 실제로 문을 막는다 ✅

- `export.py:84` `_RETRYABLE_UNAVAILABLE_REASONS = {"stream_unavailable","pcm_unavailable"}`
  → `export.py:733-734`에서 `blockers.append("retryable_unavailable_instances")`.
- `export.py:87` `_FINAL_REVIEW_CODES_ALLOWED = {"unavailable_declared_no_audio"}` — 최종
  수용을 통과하는 유일한 부재 코드.
- `export.py:305-320` `_require_unavailable_review`가 기록과 리뷰를 다중집합으로 맞춘다 —
  **생략된 리뷰로는 패키지가 안 나간다.**

즉 `declared_no_audio`를 잘못 찍으면 **자막 없는 애셋이 조용히 출판된다.** §3이 그 코드를
`has_audio is False`에만 묶은 것은 필수 안전장치다.

### C2 — `AudioPolicy`에 config 자리가 없다. §4와 §5가 서로 모순이다 · 중

- `ProjectConfig`(`config.py:62-104`)에 오디오·스트림·PCM 필드가 **하나도 없다**(grep
  `stream_index|AudioPolicy|audio_`: 가격 필드만 나온다).
- `Config`는 레거시 whisper 설정이고 `AudioPolicy`를 모른다.
- 기본값은 `pcm.py:94-96`(`stream_index=0, sample_rate=16000, sample_width=2`)에 산다.

그래서 §5의 "임계값은 전부 `Config`/`ProjectConfig`에서"는 **이 태스크가 골라야 하는 유일한
정책에 대해 만족 불가능**하다. 구현자는 (a) `AudioPolicy(stream_index=0)`을 함수 안에 박아
§5를 어기거나, (b) 파라미터로 받거나 둘 중 하나다. §4가 "**실제로 쓴** `AudioPolicy`를
실어라"고 말하는 것은 그것이 **입력**임을 전제하는데, 브리프는 그 입력이 어디서 오는지
끝내 말하지 않는다.

### C8 — `stream_index=0`은 나머지 오디오 트랙을 말없이 버린다 · 하

카메라 ＋ 랩마이크처럼 오디오가 둘 이상인 애셋에서 `a:0`만 듣고, **다른 트랙을 듣지
않았다는 기록이 없다.** 브리프가 정책을 결과에 싣게 한 것이 유일한 완화다. 한계를
명시적으로 적어 두는 편이 낫다. (이 한계는 기존 `pcm.py`의 성질이지 브리프가 만든 게
아니다.)

---

## 계약 · 불변식 · CLAUDE.md 규칙

**깨는 것 없음.** 확인한 것:

- **§1 방향** — `runner.py`가 `vlogsrt.preprocess`·`vlogsrt.project.*`를 import하는 것은
  project → 레거시 단방향. 레거시가 runner를 알 일은 없다. ✅
- **§4 openai / §5 subprocess / §6 numpy** — 이 태스크는 셋 다 import하지 않는다. ffmpeg는
  `preprocess.probe_audio_stream`(`preprocess.py:146`)과 `pcm.build_reference`
  (`pcm.py:158`, 내부적으로 `preprocess.extract_pcm_s16le`)를 통해서만 닿는다. ✅
- **§7 모델 리터럴** — STT를 안 부른다. ✅
- **불변식 20개** — 실제로 세었다:

```
$ .venv/bin/python scripts/check_invariants.py | grep -c '^\[OK\]'
20
```

  브리프의 `20`이 맞다. **낡은 쪽은 `docs/PROJECT_CONTRACTS.md`의 "(19군)"이다** —
  브리프가 아니다. (이건 반박이 아니라 코디네이터 손을 들어주는 확인이다.)

- **규칙 4·5·9** — 이 단계는 구간을 고르지도, 검증하지도 않는다. rule 9는 §3의 타입으로
  지켜진다 — 다만 C4를 보라.

### C7 — §4의 "동결 dataclass로(계약 §8)" 인용이 부정확하다 · 하

계약 §8은 **정확 경로 `vlogsrt/project/models.py`**에만 걸린다. `check_invariants.py`의
`[OK] 파생 frozen 모델`은 `runner.py`를 **보지 않는다.** frozen으로 만드는 건 여전히
옳지만, **기계 게이트가 지켜준다고 믿지 마라.** 브리프가 "§8이 검사한다"로 읽히면
구현자가 확인을 생략한다.

---

## 의존성 · 순서

### C1 — §6 테스트 6의 괄호 주장이 틀렸다 · **중 (반박)**

> "같은 애셋을 클립 여럿이 써도 PCM 추출은 한 번(`resolve_media`가 리소스 단위라 **이미
> 그렇다**)"

**재현했다.** 같은 바이트를 가리키는 애셋 리소스 둘:

```
$ .venv/bin/python /tmp/protorev/dupe.py
resources: 2
resolved : 2
  scoped= c34ba6dba024 content= b67fe365fad8 path= same.mp4
  scoped= 29a6b6dd73a1 content= b67fe365fad8 path= same.mp4
distinct content ids: 1
```

`resolve_media`가 **리소스 단위인 것이 바로 원인이다.** 클립 여럿 → 리소스 하나는 맞지만,
**리소스 둘 → 내용 하나**는 접히지 않는다. 그래서 `ResolvedMedia`마다
`build_reference`를 부르면 같은 파일을 두 번 만난다.

두 번 **추출**되는지는 오직 `destination`이 정한다 —
`_reusable_reference`(`pcm.py:391`)가 `destination`의 포인터를 보고 재사용을 결정하기
때문이다. 그런데 **브리프 §4는 "PCM 목적지"라고만 쓰고 그 키를 말하지 않는다.**
`resource_scoped_id`로 키를 잡으면 같은 바이트를 ffmpeg가 두 번 디코딩하고 세대가 둘
남는다. 그리고 **테스트 6은 그래도 초록이다** — 클립 케이스만 보기 때문에.

최소 수정:
1. `destination`을 **`asset_content_id`로 키잡는다**고 §4에 못 박아라
   (`speech.py:607`의 `artifact_dir / media.asset_content_id`와 같은 축).
2. 테스트 6을 **리소스 둘이 같은 바이트를 가리키는** 케이스로 바꿔라. 지금 문장은
   `resolve_media`가 이미 접는 케이스만 본다.

### C9 — 보드에 PROTO-* 가 없다 · 하

`grep -n "PROTO-1\|PROTO-2\|PROTO-3\|PROTO-4\|runner" TASKS.md` → **출력 없음.** 브리프가
인용하는 세 태스크 보드가 저장소에 아직 없다. 또 `vlogsrt/project/commands.py`가 없고
`grep -n project vlogsrt/cli.py` → **출력 없음** — `runner.py`는 CLI에서 닿지 않는 채로
착지한다. 프로토타입 라인이니 그 자체는 괜찮지만, **계약 §1의 유일한 CLI 예외인
`commands.py`를 소유한 태스크가 셋 중 아무 데도 없다.** PROTO-4가 떠안거나 넷째가 필요하다.

---

## 소유 파일로 가능한가

**가능.** `vlogsrt/project/runner.py` ＋ `tests/project/test_runner_audio.py` 둘로 충분하다.
C1·C3·C4·C5·C6의 수정이 전부 `runner.py` 안에서 끝난다. C2도 `AudioPolicy`를 **러너
파라미터**(기본 `AudioPolicy()`)로 받으면 소유 파일 안이다.

**단, `AudioPolicy`를 `ProjectConfig`에 넣는 길을 고르면 `vlogsrt/project/config.py`가
필요해진다 — 범위 밖이다. 고르지 마라.** 파라미터 쪽이 맞다.

---

## 코디네이터 주장 검토

| # | 주장 | 판정 |
|---|---|---|
| 1 | `speech.py:215`가 계약을 이미 고정했다 → 쌍을 실어라 | **확인.** 쪼개면 `_validated_pairs`(`speech.py:758`)가 신원으로 검사하는 짝을 소비자가 다시 묶어야 한다 — P4-04 모양이 맞다 |
| 2 | 세 `reason_code` 표 | **확인.** 셋 다 통과, 두 오라벨 거부 (재현 1) |
| 3 | `declared_no_audio`는 **probe하지 않는다** | **확인, 그리고 필수다.** `media.py` 모듈 docstring이 "`has_audio`는 정확히 그 리소스에서만 온다 — 확장자·바이트·**프로빙**으로 추론하지 않는다"고 못 박았다. 브리프는 기존 계약을 따르는 것이지 비용을 아끼는 게 아니다 |
| 4 | "P1-02가 명시적 0/1만 통과시킨다" | **확인.** `fcpxml.py:403` `_required_bool_attribute(element, "hasAudio", where)` — 생략은 파싱에서 거부된다. `hasAudio` 누락이 `declared_no_audio`로 새는 길은 닫혀 있다 |
| 5 | "이 타입을 만드는 제품 코드는 어디에도 없었다" | **확인.** `UnavailableSourceAudio(` 생성은 `vlogsrt/project/**` 제품 코드에 없다 |
| 6 | 불변식 `[OK]` 20줄 | **확인.** 실행값 20. 계약 문서의 "19군"이 낡았다 |
| 7 | `ProjectDocument`가 `input_sha256`을 이미 싣는가 (확인하라) | **싣는다.** `models.py` `ProjectDocument.input_sha256`. 재노출 불필요 — 브리프의 물음에 답이 나왔다 |
| 8 | 테스트 6: "`resolve_media`가 리소스 단위라 **이미 그렇다**" | **반박 (C1).** 재현했다. 리소스 단위인 것이 원인이고, 접는 것은 `destination` 키다 |
| 9 | §4 "동결 dataclass로(계약 §8)" | **반박 (C7).** §8은 `models.py` 전용. `runner.py`는 기계 검사 밖이다 |
| 10 | §5 "임계값은 전부 `Config`/`ProjectConfig`에서" | **반박 (C2).** 두 config 어디에도 `AudioPolicy` 필드가 없다 — 이 태스크가 골라야 하는 유일한 정책에 대해 만족 불가능하다 |

---

## 최소 수정 명세 (심각도 순)

1. **C1 — §4에 한 줄:** PCM `destination`은 **`asset_content_id`로 키잡는다.** §6 테스트
   6을 "같은 바이트를 가리키는 **리소스 둘**"로 바꾼다. (지금 문장은 접히는 케이스만 본다.)
2. **C4 — §4에 한 줄:** 결과 dataclass의 `__post_init__`이
   `len(audio_inputs) == len(document.resources)`와 `audio_inputs[i][0].resource_scoped_id
   == document.resources[i].scoped_id`를 단언한다. rule 9를 **테스트가 기억해야 하는 것**에서
   **타입으로 표현 불가능한 것**으로 옮긴다. `protected=[]`가 여섯 번 재발한 저장소에서
   이 하나가 제일 값싸고 제일 오래 간다.
3. **C2 — §4·§5 조정:** `AudioPolicy`를 러너 파라미터로 받되 기본은 `AudioPolicy()`.
   §5의 rule-8 문장에 "`AudioPolicy`는 예외 — 두 config에 자리가 없다"를 덧붙여 모순을 없앤다.
4. **C3 — §4에 한 줄:** 쓴 `Config`와 `ProjectConfig`도 결과에 싣는다. 근거는 §4가
   `AudioPolicy`에 댄 것과 **글자 그대로 같다** — `_source_analysis_key`가
   `_config_sha256(cfg)`를 먹고, 그 키가 출판 자리를 정한다.
5. **C5 — §3에 한 줄:** `resolve_media`의 `UnsupportedProject`는 **그대로 전파된다.**
   세 코드는 미디어 해석이 **성공한 뒤에만** 시작한다. 재현:

   ```
   $ .venv/bin/python /tmp/protorev/offline.py
   assets: 2
   ABORTS ENTIRE RUN -> UnsupportedProject | /fcpxml[1]/resources[1]/asset[2] | 원본 미디어가 일반 파일이 아니다: …
   ```

   오프라인 미디어 하나가 전체 실행을 세운다(`media.py:226`). 이것은 `media.py` 모듈
   docstring이 세운 **의도된 준비 게이트**이고 그대로 둬야 한다. 그러나 "부재의 기록"이라는
   브리프의 제목은 구현자가 이것도 per-resource로 감싸 없는 코드를 지어내게 유혹한다 —
   그러면 **하드 게이트가 retryable 소프트 기록으로 조용히 강등된다.**
6. **C6 — §3에 한 줄:** 잡을 예외를 이름으로 못 박아라. `probe_audio_stream`은
   `RuntimeError`, `build_reference`는 `ValueError`/`RuntimeError`/`OSError`를 낸다.
   `except Exception`이면 러너 자신의 버그와 **ffmpeg 미설치**까지 per-resource
   `pcm_unavailable`이 된다 — 출판은 막히니(C의 재시도 블로커) 조용하진 않지만, 환경
   오류 하나가 N개의 부재 기록으로 번역되어 진단이 죽는다. `require_ffmpeg` 실패는
   전파시켜라.
7. **C7 — §4 문구:** "§8이 검사한다"로 읽히지 않게. frozen은 맞지만 기계 게이트 밖이다.
8. **C8 — §4에 한 줄:** `stream_index=0`이 다른 오디오 트랙을 듣지 않는다는 한계를 명시.
9. **C9 — 순서:** `commands.py` 소유자를 정하라(PROTO-4 또는 넷째). 지금 셋 중 아무도 없다.

---

## 재현하지 못한 것

반드시 적는다.

- **`build_reference`·`probe_audio_stream`을 실제로 돌리지 못했다.** 실미디어와 ffmpeg가
  필요하다. C1의 "두 번 추출된다" 중 **`resolve_media`가 쌍을 둘 내놓는다는 절반은
  재현했고**, `_reusable_reference`가 `destination` 키로만 접는다는 절반은 `pcm.py:391-431`
  **코드 독해**다 — 실행 확인이 아니다.
- **두 번의 독립 ffmpeg 추출이 바이트 동일한 PCM을 내는지 확인하지 못했다.** 동일하다면
  `analysis_key`가 같아져 `transcribe_unique_sources`가 트레이스를 접으므로 C1은
  **비용·디스크 문제이지 정합성 문제가 아니다.** 동일하지 않다면 C1의 심각도는 올라간다.
  이 갈림길을 닫지 못했다 — C1을 "중"으로 둔 이유다.
- **`pytest` 전체·`mypy`·`ruff`를 돌리지 않았다.** 이 검토는 브리프의 방향을 보는 것이고
  구현물이 없다.
- **C8(다중 오디오 트랙 누락)을 실측하지 못했다.** 다중 트랙 애셋이 없다. `pcm.py`의
  `stream_index` 의미(`-map 0:<전역번호>`, `preprocess.py:275-283`) 독해에 근거한다.
