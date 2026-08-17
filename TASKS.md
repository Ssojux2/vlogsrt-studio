# TASKS.md — vlogsrt 태스크 보드

> **현재 계획: 단일 모델 전사 + 검사 계층 제거** (근거: `docs/DECISIONS.md` D21)
> 한 세션에 태스크 하나. DoD = `make check` 통과 → 커밋.
> 옛 보드(T0~T5 2모델 사상, P0~P8 whisper 단독+검증)는 git 히스토리에 있다.

---

## 진행 현황

| 태스크 | 내용 | 상태 |
|---|---|---|
| Q0 | 문서 전환 (CLAUDE.md · TASKS.md · D21) | ✅ |
| Q1 | 코드 전환 (diarize 전사, 검사 계층 삭제) | ✅ |
| Q2 | 문서 마무리 (README · RUNBOOK · DASHBOARD) | ✅ |
| Q3 | 사람 인수: 짧은 영상 1편 온라인 실행 🧑 | ☐ |

---

## Q1 — 코드 전환

**삭제**: `verify.py`, `loops.py`, `validate.py`, `report.py`(대시보드 함수는
`evaluate.py`로 이주) + 각 테스트.

**전사**: `transcribe.py`의 `whisper()`/`gpt()` → `transcribe()` 하나.
`response_format`은 config(`transcribe_format`, 기본 `diarized_json`).
`segments[].{start,end,text}`만 읽고 `id`는 우리가 위치로 부여한다
(diarize는 `"seg_0"` 같은 문자열 id를 줄 수 있다).
부가 인자(`prompt`/`keywords`/`languages`)가 400을 받으면 떼고 한 번 더 부르는
폴백은 유지 — 미검증 모델에 대한 방어다.

**전처리**: 무음 검출 삭제, 균등 분할. `chunk_max_seconds` 600 → 1500.

**모델**: `Segment`는 `id/start/end/text`만. `Word`·`Issue`·`VERDICT_*` 삭제.
`Trace`에서 `words`·`silences` 제거, `TRACE_VERSION` 4.0.

## Q2 — 문서 마무리

README 파이프라인·모듈표, RUNBOOK 절차(무음·리포트·검증 언급 제거),
DASHBOARD 변형 라벨.

## Q3 — 사람 인수 🧑

> **Claude Code에게 시키지 마라.** 유료 호출이다.

1. `make clean` (캐시에 옛 오디오가 남아 있을 수 있다)
2. 짧은 영상(3~5분) 1편을 UI로 실행
3. 확인할 것:
   - 모델이 계정에 있는가 (없으면 404 — 과금 없이 즉시 실패)
   - 자막이 소리와 맞는가 (귀 테스트)
   - 첫 자막이 영상 초반에 서는가
4. 잘 돌면 `vlogsrt record`로 픽스처를 만들어두면 이후 오프라인 회귀가 살아난다.

문제가 있으면 로그와 함께 새 태스크로.
