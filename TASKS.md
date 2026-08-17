# TASKS.md — vlogsrt 태스크 보드

> **현재 계획: whisper가 자막을 만들고, 시각은 원본 오디오 그대로, gpt-transcribe는
> 표시만 한다** (근거: `docs/DECISIONS.md` D25)
> 한 세션에 태스크 하나. DoD = `make check` 통과 → 커밋.
> 옛 보드(T·P·Q·R 시리즈)는 git 히스토리에 있다.

---

## 진행 현황

| 태스크 | 내용 | 상태 |
|---|---|---|
| S0 | 문서 전환 (CLAUDE.md · D25 · 이 보드) | ✅ |
| S1 | 분할을 한도 + 진폭 0 탐색으로 — **초반·마지막 사고 해결** | ✅ |
| S2 | 시각을 whisper로 — 원본 오디오 시각 그대로 | ✅ (S1과 한 커밋) |
| S3 | 검증 계층 — gpt-transcribe, **기본 켜짐** | ✅ |
| S4 | 인자 분리 · 가드 · 문서 | ✅ |
| S5 | 사람 인수 — 77분 실행 완료. 반복 환청 실측 → D26 | ⚠️ 조각 재조정 후 재실행 필요 |

---

## 무엇이 왜 깨졌나 (S1이 고치는 것)

D24의 `speech_spans`가 `cursor = 0.0`에서 시작해 **무음의 여집합만** 발화 구간으로
삼았다. `silencedetect`가 "0~200초는 무음"이라 하면 그 구간은 자막에 아예 안 들어간다.
끝도 같다. **조용한 인트로·아웃트로가 통째로 버려졌다.**

고치는 방법은 그 판단을 없애는 것이다. 새 분할은 **자를 자리만** 고르고, 조각은 언제나
`[0, duration]`을 빈틈없이 덮는다.

## S1 — 분할 (TDD)

`spans.py`·`align.py` 삭제. `cutpoints.py` 신규 — 테스트를 **먼저** 쓴다.
`preprocess.amplitudes`(`scripts/verify_timing.py:53`의 `envelope`에서 이주),
`preprocess.split_at`(ffmpeg `-f segment -segment_times … -c copy`, 오프셋은 CSV
실제값). `Chunk.spans` 제거.

**회귀 테스트 4개:**
1. 자를 시각이 단조 증가하고 `[0, duration]` 안에 있다
2. 조각이 `[0, duration]`을 빈틈없이 덮는다 (앞 조각 끝 = 뒤 조각 시작)
3. 진폭에 조용한 곳이 하나도 없어도 조각 수·커버리지가 그대로다 (강제 분할)
4. **맨 앞이 전부 무음이어도 첫 조각은 0초에서 시작한다** ← 이번 사고의 직접 회귀

## S2 — 시각을 whisper로

`transcribe.parse_segments`/`build_spine` 복원(`git show 1ab3d61:vlogsrt/transcribe.py`).
`transcribe_model = "whisper-1"`, `transcribe_format = "verbose_json"`.
`TRACE_VERSION` 6.0. 합성 픽스처를 whisper 모양으로 되돌리되 **0.0초에서 시작하는
세그먼트와 duration에서 끝나는 세그먼트를 반드시 포함**시킨다.

불변 테스트: 큐 시각이 `세그먼트 시각 + 오프셋`에서 `refine` 한도 이상 벗어나지 않는다.

## S3 — 검증 (기본 켜짐)

`verify.py` 복원(`git show ae861ef^:vlogsrt/verify.py`) + `loops.find_loops` 흡수
(표시 전용, `demote`는 가져오지 않는다). 픽스처 두 종류:
`<stem>.whisper.json` / `<stem>.gpt.json`.
`pipeline.run(..., verify_with_second_model=True)`, **SRT 저장 후** `try/except`로 검증.
끄려면 `--no-verify`.

불변 테스트: **검증 on/off SRT 바이트 동일**, 검증이 실패해도 SRT는 나온다.

## S4 — 인자 분리 · 가드 · 문서

`_extras` → `whisper_extras`(language만) / `verify_extras`(prompt·keywords·language).
`check_invariants.py`: whisper 호출 인자에 `prompt` 부재, `spans.py`·`align.py` 부재,
`cutpoints.py` 순수성, `verify.py`가 `refine`/`output`을 import하지 않음.
`--verify` CLI·UI 체크박스·비용 표시. README·RUNBOOK·DASHBOARD·`vlogsrt.toml`.

## S5 — 사람 인수 🧑

> **Claude Code에게 시키지 마라.** 유료 호출이다.

1. `make clean` — D24 시절 조각(`*.batch*.mp3`)이 캐시에 남아 있다
2. 짧은 영상(3~5분) 1편
3. 확인할 것:
   - **초반·마지막에 자막이 있는가** ← 이번 사고의 확인 지점. 제일 먼저 본다
   - 자막이 소리와 맞는가 (귀 테스트)
4. 잘 돌면 `vlogsrt record`로 픽스처를 만들어두면 오프라인 회귀가 살아난다
5. 그다음 긴 영상 1편 — 38분 조각에서 반복 환청이 재발하는지 교차 확인 로그로 본다.
   재발하면 `vlogsrt.toml`의 `chunk_max_bytes`를 `4_800_000`(≈10분)으로 낮춰 재실행

문제가 있으면 로그와 함께 새 태스크로.
