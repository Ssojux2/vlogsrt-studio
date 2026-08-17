# TASKS.md — vlogsrt 태스크 보드

> **현재 계획: gpt-transcribe + 오디오 기반 타이밍** (근거: `docs/DECISIONS.md` D24)
> 한 세션에 태스크 하나. DoD = `make check` 통과 → 커밋.
> 옛 보드(T0~T5 2모델 사상, P0~P8 whisper 단독+검증, Q0~Q5 diarize 단일 모델)는
> git 히스토리에 있다.

---

## 진행 현황

| 태스크 | 내용 | 상태 |
|---|---|---|
| R0 | 문서 전환 (CLAUDE.md · D24 · 이 보드) | ☐ |
| R1 | 순수 계층 — `spans.py` · `align.py` (TDD) | ☐ |
| R2 | 전처리 경계 — 무음 검출 · 묶음 오디오 추출 | ☐ |
| R3 | 전사 전환 — text 응답, diarize 잔재 삭제 | ☐ |
| R4 | 파이프라인 + 픽스처 재작성 | ☐ |
| R5 | 손실 정책(경고) + 가드 갱신 | ☐ |
| R6 | 문서 마무리 (README · RUNBOOK · DASHBOARD) | ☐ |
| R7 | 사람 인수: 짧은 영상 1편 온라인 실행 🧑 | ☐ |

---

## R1 — 순수 계층 (TDD)

`tests/test_spans.py` · `tests/test_align.py`를 **먼저** 쓴다. ffmpeg도 네트워크도
쓰지 않는다.

`vlogsrt/spans.py`
- `parse_silence_output(stderr, duration)` — `git show ae861ef^:vlogsrt/preprocess.py`에서 이식
- `speech_spans(silences, duration, cfg)` — 무음의 여집합. `min_speech_duration`보다 짧으면 버린다
- `group_spans(spans, cfg)` — `batch_max_seconds`/`chunk_max_bytes` 안에서 묶는다. 자르는 자리는 항상 무음 한가운데

`vlogsrt/align.py`
- `allocate(text, spans, seg_id_base)` — 누적 목표 글자 수로 어절을 배분. 남으면 마지막 구간에

검증할 것: 어절 유실 0, 구간 유실 0, 시각이 구간 값과 정확히 일치,
누적 오차가 뒤로 쌓이지 않음, 구간 0개·텍스트 빈 경우.

## R2 — 전처리 경계

`detect_silence` 이식, `cut_batches`(ffmpeg `-ss`/`-to`, ThreadPool 병렬),
`models.Chunk`에 `spans` 추가. `plan_chunks`/`chunk_count` 정리.

## R3 — 전사 전환

`parse_segments` → `parse_text`. `build_spine`이 `align.allocate`를 부른다.
`transcribe_model = "gpt-4o-transcribe"`, `transcribe_format = "json"`,
`chunking_strategy = ""`(우리가 직접 자른다).
**필수/부가 인자 분리와 400 정밀 폴백, 첫 조각으로 인자 확정하는 구조는 그대로 둔다**
— D22·D23에서 실측으로 얻은 것들이다.
`TRACE_VERSION` 5.0.

## R4 — 파이프라인 + 픽스처

`pipeline.prepare`가 무음→구간→묶음을 거친다. 합성 픽스처를 새 모양으로:

```json
{"chunks": [{"index": 0, "offset": 0.0, "end": 12.0,
             "spans": [[0.4, 2.6], [3.1, 5.9]],
             "response": {"text": "안녕하세요 오늘은 망원동에 왔습니다"}}]}
```

## R5 — 손실 정책 + 가드

멈춤 → 경고: 빈 묶음 텍스트, 짧은 조각, 빈 큐.
`scripts/check_invariants.py`에 **`spans.py`·`align.py` 순수성** 검사 추가.
`scripts/verify_timing.py`를 새 픽스처 모양에 맞춘다.

## R6 — 문서 마무리

README 파이프라인 그림·모듈표, RUNBOOK 막히면 표, DASHBOARD, `vlogsrt.toml`.

## R7 — 사람 인수 🧑

> **Claude Code에게 시키지 마라.** 유료 호출이다.

1. `make clean` (캐시에 옛 오디오가 남아 있을 수 있다)
2. 짧은 영상(3~5분) 1편을 UI로 실행
3. 확인할 것:
   - 모델이 계정에 있는가 (없으면 404 — 과금 없이 즉시 실패)
   - **묶음당 발화 구간 수** 로그. 1~2개면 무음이 안 잡힌 것 →
     `silence_threshold_db`를 올린다
   - 자막이 소리와 맞는가 (귀 테스트 — 배분 품질은 이걸로만 판단한다)
   - 로그에 `API가 […]를 거부했다` 경고가 뜨는지, 뜬 인자 이름
4. 잘 돌면 `vlogsrt record`로 픽스처를 만들어두면 이후 오프라인 회귀가 살아난다.

문제가 있으면 로그와 함께 새 태스크로.
