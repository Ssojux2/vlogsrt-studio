# CLAUDE.md

브이로그 자막 SRT 생성기. **모델 하나(`gpt-4o-transcribe-diarize`)가 자막의 시각과
글자를 모두 만들고, 프로그램은 그것을 SRT로 조판할 뿐이다.** 검사도, 판정도,
재검증도 하지 않는다. 설계 배경은 `docs/DECISIONS.md`(현재 구조는 D21), 태스크
보드는 `TASKS.md`.

---

## 아키텍처 불변 규칙

이 9개를 어기면 설계가 무너진다. 작업 전 반드시 확인하고, 어겨야 할 이유가 생기면
코드를 고치지 말고 **먼저 보고하라.**

1. **동기화가 1순위다.** 자막의 모든 타임스탬프와 모든 글자는 전사 응답의
   `segments[]`(= `spine`)에서만 온다. 다른 출처는 없다.

2. **`spine[]`은 생성 후 절대 수정하지 않는다.** 모든 큐는 `seg_ids`로 원본
   세그먼트를 역추적할 수 있어야 한다.

3. **시각을 움직일 수 있는 곳은 `refine`뿐이다.** 이동량은 config의
   `lead_in`/`lead_out`/`min_gap`/`min_duration`/`max_duration` 한도 안이다.
   그 밖의 어떤 단계도 큐 시각을 만지지 않는다.

4. **검사·재검증 계층을 되살리지 마라(D21).** 판정(verdict), 되풀이 강등,
   교차 검증, 정제 결과 검사, 무음 검출 — 전부 의도적으로 삭제했다. "자막 품질이
   의심스러우니 경고를 붙이자"는 제안이 떠오르면 **코드를 쓰지 말고 보고하라.**
   품질 판단은 사람이 눈과 귀로 한다.

5. **API 호출은 `vlogsrt/transcribe.py`에서만.** 나머지 모듈은 전부 순수 함수여야
   한다. 테스트 가능성이 여기 걸려 있다.

6. **모든 임계값은 `config`에서 온다.** 코드에 숫자를 하드코딩하지 마라.
   `0.85`, `12.0`, `16` 같은 값이 함수 안에 리터럴로 나타나면 리뷰 실패다.

7. **의존성은 `openai`, `tomli-w`, `rich` 뿐이다.** `jamo`, `konlpy`, `pysrt`,
   `ffmpeg-python`, `pydantic` 등을 추가하지 마라. 필요한 건 표준 라이브러리로 짠다.
   (`difflib`, `unicodedata`, `bisect`, `subprocess`, `concurrent.futures`)

8. **개발 중 실제 API를 호출하지 마라.** `tests/fixtures/`의 응답을 쓴다. 실제
   호출은 `vlogsrt record`로만, 사람이 명시적으로 실행한다.

9. **어떤 구간도 조용히 버리지 마라(D23).** 청크 하나를 건너뛰거나, 세그먼트를
   떨구거나, 빈 큐를 지우고 넘어가면 자막에 구멍이 뚫리고 **아무도 모른다.**
   버릴 수 있는 것은 글자가 아예 없는 세그먼트뿐이다(화면에 띄울 게 없다).
   그 밖의 손실은 전부 **멈춤**이다 — 경고가 아니라 `RuntimeError`.
   시각이 길이 0이거나 뒤집혀도 글자는 남긴다. 정제의 `min_duration`이 늘려준다.

부수 규칙: `difflib.SequenceMatcher`는 항상 `autojunk=False`(기본값이면 한국어처럼
고빈도 문자가 많은 시퀀스에서 비교가 무너진다).

---

## 파이프라인

```
영상 → ffmpeg 오디오 추출(필터 없음) → 한도 분할(24MB/1500s)
     → gpt-4o-transcribe-diarize → 오프셋 리베이스 → spine
     → spine_to_cues → refine → SRT 저장
```

끝이다. 이 뒤에 아무 단계도 없다.

전사 응답의 `segments[].{start,end,text}`가 전부다. 세그먼트를 버리는 판정도 없고
(빈 텍스트와 `end <= start`만 제외), 결과를 다시 재는 단계도 없다.

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
예상 비용: 약 $0.03 (5분 영상 기준)
```

`tests/fixtures/api/`의 `*.whisper.json` / `*.gpt.json`은 옛 2모델 시절의 고아다.
읽지도 지우지도 마라(픽스처 수정 금지).

### 임계값을 고치지 마라

**테스트가 실패하면 코드를 고쳐라. 기대값이나 임계값을 고치지 마라.**

임계값 자체가 틀렸다고 판단되면 **수정하지 말고, 근거와 함께 보고하라.**
다음 두 값은 외부 근거가 있다.

| 값 | 출처 |
|---|---|
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
vlogsrt run <video> --offline     # 전체 실행
vlogsrt redo out/trace.json       # 조판만 재실행 (API 없음)

# 평가·싱크 게이트
vlogsrt eval out/output.srt golden/ep01.srt
make eval               # 골든셋 전체 → docs/DASHBOARD.md 갱신
make verify STEM=<stem> # 오디오 포락선 대조 (파이프라인 밖의 유일한 자동 게이트)

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
| spine / 골격 | 전사 응답의 세그먼트 배열. 생성 후 수정 불가 |
| cue / 큐 | 자막 한 장. 시각과 줄이 들어 있다 |
| refine / 정제 | 큐를 자르고 합치고 시간을 다듬는 유일한 단계 |
| chunk / 청크 | API 한도(24MB/1500s)로 쪼갠 오디오 조각 |

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
- **검사·판정·재검증 계층 부활** (규칙 4)
- 테스트 skip / xfail
- 한 세션에서 두 개 이상 태스크
- 커밋 없이 세션 종료
