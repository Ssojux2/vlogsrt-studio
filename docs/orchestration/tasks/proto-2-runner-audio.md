# PROTO-2 — 러너 1/3: 파싱 · 미디어 해석 · PCM, 그리고 부재의 기록

> 프로토타입 라인. `docs/plans/2026-09-13-cut-first-and-plugin.md`의 R 페이즈는
> **미뤄져 있다** — 이 태스크는 **현재 순서(자막 → 컷)** 그대로 FCPXML 하나를 끝까지
> 돌리기 위한 러너의 첫 조각이다.

작업 디렉터리: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`
브랜치: `Ssojux2/fcp-prototype`
venv: `.venv/bin/python`

**이 머신의 `/usr/bin` shim이 Xcode 라이선스 미동의로 전부 죽어 있다**(exit 69).
`git`도 `python3`도 마찬가지다. 절대 경로를 써라:
`/Library/Developer/CommandLineTools/usr/bin/git`, 그리고 파이썬은 `.venv/bin/python`.

## 소유 파일 — 정확히 둘

- `vlogsrt/project/runner.py` (**신규**)
- `tests/project/test_runner_audio.py` (**신규**)

이 둘 밖의 무엇이든 고쳐야 한다고 판단되면 **멈추고 보고하라.** 특히
`vlogsrt/project/**`의 기존 모듈, `tests/fixtures/**`, `golden/**`, 임계값은 범위 밖이다.

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

**이 태스크가 정하는 타입이 나머지 둘의 입력이다.** 그래서 이 브리프는 구현보다
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
않고** 신원으로 검사한다(`_validated_pairs`) — 쌍을 깨서 두 리스트로 돌려주면
소비자가 다시 묶어야 하고, 그게 이 저장소의 **P4-04 실패 모양**이다.

## 3. `UnavailableSourceAudio`를 만드는 것이 바로 이 단계다

`speech.py:181-194`의 docstring이 명시한다 — *"P7이 문맥 있는 pre-PCM 실패를 잡아 이
기록을 만든다 — 여기서는 신원만 검사하고 그대로 흘려보낸다."* **그 P7이 이 러너다.**
지금까지 이 타입을 만드는 제품 코드는 **어디에도 없었다.**

세 가지 `reason_code`를 각각 **어디서** 만드는지 브리프가 못 박는다:

| `reason_code` | `status` | 언제 |
|---|---|---|
| `declared_no_audio` | `"no_audio"` | `ResolvedMedia.has_audio is False` — 리소스 선언 그대로. **probe하지 않는다** |
| `stream_unavailable` | `"error"` | `has_audio`는 참인데 `preprocess.probe_audio_stream`이 실패 |
| `pcm_unavailable` | `"error"` | probe는 됐는데 `pcm.build_reference`가 실패 |

**`reason`에는 실제 예외 메시지를 실어라.** 빈 문자열이나 상수 문구는 진단을 죽인다.

### 이것이 규칙 9와 D35의 자리다

> **어떤 리소스도 조용히 빠지지 않는다.** 실패한 리소스를 `continue`로 건너뛰거나
> 결과 목록에서 빼는 것은 금지다. 실패는 **타입으로** 남아 하류까지 간다.
> 부재를 빈 성공으로 바꾸지 않는다.

이 저장소에서 `protected=[]` 계열 결함이 **여섯 번** 재발했다 — 전부 "없음"이
"제한 없음"으로 읽힌 것이다. 리소스를 목록에서 빼면 하류는 그 리소스가 **애초에
없었다고** 읽는다. 그게 일곱 번째다.

## 4. 시그니처 — 하류가 다시 유도하지 않게

최소한 다음을 실어라. 이름은 바꿔도 되지만 **값이 빠지면 안 된다.**

- 파싱된 `ProjectDocument` (PROTO-3의 boundary/logical_slices가 쓴다)
- `tuple[AudioSourceInput, ...]` — §2 그대로, **문서 리소스 순서 유지**
- 실제로 쓴 `AudioPolicy` — PROTO-3이 기본값을 가정해 다시 만들면 안 된다
- artifact 루트와 그 아래 하위 경로(PCM 목적지, source, boundary, frames)를
  **만들어 놓고 경로로 실어라.** 소비자가 문자열을 조립하게 두지 마라
- 입력 XML의 `input_sha256` (`ProjectDocument`가 이미 싣는다면 재노출 불필요 — 확인하라)

**동결 dataclass**로(계약 §8). `resource_scoped_id`로 찾는 매핑이 필요하면 그것도 실어라 —
PROTO-3이 `for` 루프로 다시 찾게 만들지 마라.

## 5. 지켜야 하는 계약

- **계약 §5 / 불변식**: `runner.py`는 `subprocess`를 import하지 **못한다.** ffmpeg·ffprobe는
  `vlogsrt/preprocess.py`의 공개 함수(`probe_audio_stream`, 그리고 `pcm.build_reference`가
  내부적으로 쓰는 경로)를 통해서만 닿는다.
- **계약 §4**: openai는 `vlogsrt/transcribe.py`만. 이 태스크는 어차피 STT를 안 부른다.
- **계약 §6**: numpy/onnxruntime은 `project/vad/**`만. 이 태스크는 안 쓴다.
- **계약 §1**: 파생 계층은 레거시를 **한 방향으로만** 소비한다. `runner.py`가 레거시를
  import하는 것은 허용이고, 레거시가 `runner.py`를 아는 것은 금지다.
- **계약 §7**: 모델 이름 리터럴 금지 — 설정에서 온다.
- **규칙 8**: 임계값 하드코딩 금지. 전부 `Config`/`ProjectConfig`에서.
- **40줄 규칙**: 함수는 40줄 이내.
- **추상화 선행 금지**: `Protocol`·`ABC`·팩토리 금지. 구현체가 둘 생기면 그때.

## 6. 테스트 — 실패 우선

`tests/project/test_runner_audio.py`. 합성 입력만 쓴다 — `tmp_path` 아래 인라인으로
만들거나 기존 합성 헬퍼를 재사용한다. **`tests/fixtures/**` 를 만들거나 고치지 마라.**

반드시 덮을 것:

1. 정상 경로 — 오디오 있는 리소스가 `(ResolvedMedia, PcmReference)` 쌍이 된다.
2. `has_audio is False` → `declared_no_audio` / `no_audio`. **probe를 부르지 않았음을
   증명하라**(probe에 부르면 실패하는 스텁을 꽂아라).
3. probe 실패 → `stream_unavailable` / `error`, `reason`에 원래 메시지.
4. `build_reference` 실패 → `pcm_unavailable` / `error`.
5. **혼합 프로젝트** — 정상 하나 + 실패 하나. 결과 길이가 **입력 리소스 수와 같다.**
   이것이 규칙 9 회귀다.
6. 같은 애셋을 클립 여럿이 써도 **PCM 추출은 한 번**(`resolve_media`가 리소스 단위라
   이미 그렇다 — 러너가 그것을 깨지 않는지 확인).
7. 산출물이 `transcribe_unique_sources`의 `_validated_pairs`를 **실제로 통과한다**
   (그 함수를 직접 불러서 증명하라. 통과하는 모양이라고 *주장*하지 마라).

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

불변식은 `[OK]` 줄을 **세어** 보고한다. 전체 pytest는 0 failed 여야 한다.

## 8. 보고 형식

```
커밋 SHA:
공개 시그니처: <실제로 고정한 타입과 함수. 그대로 붙여라>
세 reason_code가 만들어지는 자리: <file:line 셋>
게이트: <명령마다 실제 출력 숫자. 불변식은 센 값>
diff 범위: <git diff --stat. 소유 파일 둘뿐인가>
멈춰야 했던 것: <있으면. 없으면 "없음">
```
