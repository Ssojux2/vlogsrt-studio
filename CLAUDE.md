# CLAUDE.md

브이로그 자막 SRT 생성기. **whisper-1 하나가 자막의 시각과 텍스트를 모두 만들고,
`gpt-transcribe`는 완성된 자막을 검증만 한다** (의심 큐 플래그, 출력 불변).
설계 배경은 `docs/DECISIONS.md`(전환 결정은 D19), 태스크 보드는 `TASKS.md`.

---

## 아키텍처 불변 규칙

이 10개를 어기면 설계가 무너진다. 작업 전 반드시 확인하고, 어겨야 할 이유가 생기면
코드를 고치지 말고 **먼저 보고하라.**

1. **동기화가 1순위다.** 자막의 모든 타임스탬프와 모든 글자는 whisper-1
   세그먼트(`spine`)에서만 온다. 다른 출처는 없다.

2. **`spine[]`은 생성(판정·강등 포함) 후 절대 수정하지 않는다.** 모든 큐는
   `seg_ids`로 원본 세그먼트를 역추적할 수 있어야 한다.

3. **시각을 움직일 수 있는 곳은 `refine`뿐이다.** 이동량은 config의
   `lead_in`/`lead_out`/`min_gap`/`min_duration`/`max_duration` 한도 안이다.
   그 밖의 어떤 단계도 큐 시각을 만지지 않는다.

4. **`gpt-transcribe`는 검증 전용이다.** `verify.py`는 큐를 만들지도 고치지도
   못한다 — 읽고 플래그만 낸다. 검증을 껐다 켜도 SRT는 바이트 단위로 같아야
   하고, 검증 실패(API 오류 포함)는 SRT 산출을 막지 못한다.

5. **whisper에는 `prompt`를 절대 보내지 않는다.** 힌트 한 줄이 앞 9분을 날렸다
   (D13/D16). `prompt`와 `keywords`는 gpt(검증 기준측)에만 간다.

6. **API 호출은 `vlogsrt/transcribe.py`에서만.** 나머지 모듈은 전부 순수 함수여야
   한다. 테스트 가능성이 여기 걸려 있다.

7. **모든 임계값은 `config`에서 온다.** 코드에 숫자를 하드코딩하지 마라.
   `0.85`, `2.4`, `18` 같은 값이 함수 안에 리터럴로 나타나면 리뷰 실패다.

8. **의존성은 `openai`, `tomli-w`, `rich` 뿐이다.** `jamo`, `konlpy`, `pysrt`,
   `ffmpeg-python`, `pydantic` 등을 추가하지 마라. 필요한 건 표준 라이브러리로 짠다.
   (`difflib`, `unicodedata`, `bisect`, `subprocess`, `concurrent.futures`)

9. **`difflib.SequenceMatcher`는 항상 `autojunk=False`.** 기본값이면 한국어처럼
   고빈도 문자가 많은 시퀀스에서 잡음 처리로 비교가 무너진다.

10. **개발 중 실제 API를 호출하지 마라.** `tests/fixtures/api/`의 녹화된 응답을
    쓴다. 실제 호출은 `vlogsrt record` 커맨드로만, 사람이 명시적으로 실행한다.

---

## 파이프라인

```
영상 → ffmpeg 오디오 추출 → 무음 기준 분할(24MB/600s API 한도)
     → whisper-1 → 오프셋 리베이스 → spine (판정 + 되풀이 강등)
     → spine_to_cues → refine → SRT 저장
     → [검증] gpt-transcribe → 유사도 비교 → 의심 큐 플래그 (report/로그만)
```

SRT는 검증 **전에** 저장된다. 검증은 실패해도 경고 한 줄이 전부다.

---

## 개발 규율

### 픽스처 우선

API 키는 `.env`에 있고, **너는 이 파일을 읽을 수 없다**(`settings.json` deny).
읽으려 시도하지 마라. 키가 필요한 작업은 전부 사람이 한다.

`.env`의 `VLOGSRT_OFFLINE` 기본값은 `1`이다. 모든 테스트와 실행이 픽스처를 읽는다.

```bash
pytest                                    # 픽스처 기반, 네트워크 없음
vlogsrt run <video> --offline             # 픽스처 기반 전체 파이프라인
```

**`VLOGSRT_OFFLINE`을 `0`으로 바꾸지 마라.** `.env`를 수정하지 마라.
`--online` 플래그를 쓰지 마라. 이 셋은 전부 유료 호출로 이어진다.

픽스처가 없거나 낡았다고 판단되면 **직접 갱신하지 말고 사람에게 요청하라.**
요청 형식:

```
픽스처가 필요합니다. 다음을 직접 실행해주세요:
  vlogsrt record golden/ep01.mp4
예상 비용: 약 $0.06 (5분 영상 기준)
```

### 임계값을 고치지 마라

**테스트가 실패하면 코드를 고쳐라. 기대값이나 임계값을 고치지 마라.**

임계값 자체가 틀렸다고 판단되면 **수정하지 말고, 근거와 함께 보고하라.**
특히 다음 값들은 외부 근거가 있다.

| 값 | 출처 |
|---|---|
| `compression_ratio > 2.4` | OpenAI API 레퍼런스 |
| `avg_logprob < -1.0` | OpenAI API 레퍼런스 |
| `min_duration = 0.85` | Netflix TTSG 5/6초 |
| `min_gap = 0.08` | Netflix TTSG 2프레임 @24fps |

### 테스트 우선

새 모듈은 테스트를 먼저 쓴다. 실패하는 테스트를 `skip`이나 `xfail`로 처리하지
마라. 고치거나 보고하라.

### 한 세션 한 태스크

`TASKS.md`의 태스크 하나만 처리한다. DoD를 통과하면 커밋하고 세션을 끝낸다.

---

## 명령어

```bash
# 검증 (가장 자주 쓴다)
make check              # lint + type + test + invariant 전부
pytest                  # 테스트만

# 파이프라인
vlogsrt run <video> --offline                     # 전체 실행 (검증 포함)
vlogsrt run <video> --offline --stop-after output # whisper만 (검증 생략, 비용 절반)
vlogsrt redo out/trace.json                       # 정제만 재실행

# 평가·싱크 게이트
vlogsrt eval out/output.srt golden/ep01.srt
make eval               # 골든셋 전체 → docs/DASHBOARD.md 갱신
make verify STEM=<stem> # 오디오 포락선 대조 (싱크 게이트)

# 픽스처 (사람만 실행)
vlogsrt record golden/ep01.mp4          # 실제 API 호출. 유료.
```

`make check`가 통과하지 않으면 커밋하지 않는다.

---

## 코딩 규약

- Python 3.11+, 타입 힌트 필수
- 함수는 40줄 이내. 넘으면 분리
- 추상화를 미리 만들지 마라. `Protocol`, `ABC`, 팩토리 패턴 금지.
  구현체가 두 개 이상 실제로 생겼을 때 그때 추상화한다
- 예외는 삼키지 마라. 로그만 찍고 `pass` 하는 코드 금지
- 주석은 "왜"만 쓴다. "무엇"은 코드가 말한다
- 로그는 `rich`로. `print` 금지

### 이 프로젝트의 도메인 용어

| 용어 | 의미 |
|---|---|
| spine / 골격 | whisper 세그먼트 배열. 생성 후 수정 불가 |
| verdict | 세그먼트 품질 판정 (ok/low_logprob/compression_fail/silent) |
| demote / 강등 | 되풀이 구간의 ok 세그먼트를 compression_fail로 바꿔 큐에서 제외 |
| suspect / 의심 큐 | 검증에서 gpt 텍스트와 일치율이 낮아 사람 확인이 필요한 큐 |
| coverage / 일치율 | 큐 텍스트가 gpt 청크 텍스트 안에서 매칭되는 비율 |

변수명에 이 용어를 쓴다. `data`, `result`, `temp` 같은 이름 금지.

---

## 절대 금지

- **`.env` 읽기·수정** (키가 컨텍스트에 들어가면 안 된다)
- **`VLOGSRT_OFFLINE=0` 설정 또는 `--online` 사용**
- 실제 API 호출 (`vlogsrt record`는 사람만)
- 새 의존성 추가
- 임계값·기대값 수정
- `golden/` 수정 (정답을 고쳐 점수를 올리는 것)
- `tests/fixtures/api/` 수정
- `spine[]` 변경
- 검증 코드(`verify.py`)가 큐·SRT에 손대는 것
- whisper 호출에 `prompt` 추가
- 테스트 skip / xfail
- 한 세션에서 두 개 이상 태스크
- 커밋 없이 세션 종료
