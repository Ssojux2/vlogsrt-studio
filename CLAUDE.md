# CLAUDE.md

브이로그 자막 SRT 생성기. 영상을 입력받아 OpenAI 두 모델만으로 SRT를 만든다.
설계 상세는 `docs/PLAN.md`에 있다. **필요한 섹션만 골라 읽어라.** 전문을 읽지 마라.

---

## 아키텍처 불변 규칙

이 8개를 어기면 설계가 무너진다. 작업 전 반드시 확인하고, 어겨야 할 이유가 생기면
코드를 고치지 말고 **먼저 보고하라.**

1. **골격은 whisper-1 세그먼트다.** `spine[]`은 생성 후 절대 수정하지 않는다.
   큐를 자르고 합쳐도 `seg_ids`로 원본을 역추적할 수 있어야 한다.

2. **최종 SRT에 whisper 텍스트가 들어가면 안 된다.** whisper 텍스트는 골격 정의,
   절단점 사상, 신뢰도 산출에만 쓴다. 출력 문자열은 전부 `gpt-transcribe` 유래여야 한다.

3. **큐 시각은 whisper 세그먼트 원본값에서 유래한다.** 절단점 사상(M3)이 큐의
   start/end를 바꾸면 그건 버그다. 텍스트만 바뀌고 타이밍은 불변이어야 한다.

4. **`difflib.SequenceMatcher`는 항상 `autojunk=False`.** 기본값이면 자모 시퀀스에서
   `ㅇ`, `ㅏ` 같은 고빈도 자모가 잡음으로 버려져 정렬이 무너진다.

5. **API 호출은 `vlogsrt/transcribe.py`에서만.** 나머지 모듈은 전부 순수 함수여야 한다.
   테스트 가능성이 여기 걸려 있다.

6. **모든 임계값은 `config`에서 온다.** 코드에 숫자를 하드코딩하지 마라.
   `0.85`, `2.4`, `18` 같은 값이 함수 안에 리터럴로 나타나면 리뷰 실패다.

7. **의존성은 `openai`, `tomli-w`, `rich` 뿐이다.** `jamo`, `konlpy`, `pysrt`,
   `ffmpeg-python`, `pydantic` 등을 추가하지 마라. 필요한 건 표준 라이브러리로 짠다.
   (`difflib`, `unicodedata`, `bisect`, `subprocess`, `concurrent.futures`)

8. **개발 중 실제 API를 호출하지 마라.** `tests/fixtures/api/`의 녹화된 응답을 쓴다.
   실제 호출은 `vlogsrt record` 커맨드로만, 사람이 명시적으로 실행한다.

---

## 개발 규율

### 픽스처 우선

API 키는 `.env`에 있고, **너는 이 파일을 읽을 수 없다**(`settings.json` deny).
읽으려 시도하지 마라. 키가 필요한 작업은 전부 사람이 한다.

`.env`의 `VLOGSRT_OFFLINE` 기본값은 `1`이다. 모든 테스트와 실행이 픽스처를 읽는다.

```bash
pytest                                    # 픽스처 기반, 네트워크 없음
vlogsrt run golden/ep01.mp4 --offline     # 픽스처 기반 전체 파이프라인
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

이 프로젝트는 임계값이 많다(`max_cps`, `min_duration`, `compression_ratio` 등).
테스트를 통과시키려고 이 값을 조정하는 것은 가장 흔하고 가장 해로운 실패다.
임계값 자체가 틀렸다고 판단되면 **수정하지 말고, 근거와 함께 보고하라.**

특히 다음 값들은 외부 근거가 있다. 임의로 바꾸지 마라.

| 값 | 출처 |
|---|---|
| `compression_ratio > 2.4` | OpenAI API 레퍼런스 |
| `avg_logprob < -1.0` | OpenAI API 레퍼런스 |
| `min_duration = 0.85` | Netflix TTSG 5/6초 |
| `min_gap = 0.08` | Netflix TTSG 2프레임 @24fps |

### 테스트 우선

새 모듈은 테스트를 먼저 쓴다. 특히 `mapping.py`는 index map 왕복 테스트를
구현보다 먼저 작성한다.

실패하는 테스트를 `skip`이나 `xfail`로 처리하지 마라. 고치거나 보고하라.

### 한 세션 한 태스크

`docs/TASKS.md`의 태스크 하나만 처리한다. DoD를 통과하면 커밋하고 세션을 끝낸다.
여러 태스크를 이어서 하지 마라. 컨텍스트가 오염된다.

---

## 명령어

```bash
# 검증 (가장 자주 쓴다)
make check              # lint + type + test + invariant 전부
pytest                  # 테스트만
pytest tests/test_mapping.py -x -q

# 파이프라인
vlogsrt run <video> --offline           # 픽스처로 전체 실행
vlogsrt redo out/trace.json             # 정제만 재실행 (0.1초)
vlogsrt baseline <video> --offline      # B0 (whisper srt)

# 평가
vlogsrt eval out/output.srt golden/ep01.srt
make eval               # 골든셋 3편 전체 → docs/DASHBOARD.md 갱신

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
| spine / 골격 | whisper 세그먼트 배열. 수정 불가 |
| canonical / 정본 | gpt-transcribe 텍스트 |
| cut point / 절단점 | gpt 텍스트를 세그먼트별로 나눌 위치 |
| orphan / 고아 | 환청 필터로 제외된 구간에 속한 gpt 텍스트 |
| verdict | whisper 세그먼트 품질 판정 (ok/low_logprob/compression_fail/silent) |

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
- 테스트 skip / xfail
- `docs/PLAN.md` 전문 읽기 (필요 섹션만)
- 한 세션에서 두 개 이상 태스크
- 커밋 없이 세션 종료

---

## 참조 맵

`docs/PLAN.md`에서 필요한 부분만 읽는다.

| 작업 | 읽을 섹션 |
|---|---|
| 전처리 (M1) | §5.1 |
| API 호출 (M2) | §2, §5.2 |
| 환청 필터 | §2.3, §5.2 M2c |
| 경계 매핑 (M3) | §6 전체 |
| 자모 분해 | §6.3 |
| 절단점 사상 | §6.4 |
| 어절 스냅 | §6.5 |
| 고아 처리 | §6.6 |
| 신뢰도 | §6.7 |
| 정제 엔진 (M4) | §5.4, §7 |
| 출력 (M5) | §5.5 |
| CLI (M6) | §5.6 |
| 데이터 모델 | §4 |
| 평가 지표 | §9.2 |
| 실패 모드 | §8 |
