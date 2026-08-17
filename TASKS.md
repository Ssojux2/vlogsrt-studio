# TASKS.md — vlogsrt 태스크 보드

> **전환 계획: whisper 단독 SRT + gpt 검증** (근거: `docs/DECISIONS.md` D19)
> 한 세션에 태스크 하나. DoD = `make check` 통과 → 커밋.
> 옛 보드(T0~T5, 2모델 사상 설계)는 git 히스토리에 있다.

---

## 진행 현황

| 태스크 | 내용 | 상태 |
|---|---|---|
| P0 | 문서 전환 (CLAUDE.md · TASKS.md · D19) | ✅ |
| P1 | `vlogsrt/verify.py` 신규 (TDD, 오프라인) | ☐ |
| P2 | 파이프라인 전환 (STAGES 개편, baseline 삭제) | ☐ |
| P3 | report/review 재작업 (의심 큐 표) | ☐ |
| P4 | 사상 계열 삭제 (mapping · gapfill) + 모델 슬림 | ☐ |
| P5 | 가드 재정비 (invariants · 싱크 게이트) | ☐ |
| P6 | M1-lite: 음질 필터 제거 (D20) | ☐ |
| P7 | 최종 청소 + 인수 | ☐ |
| P8 | 사람 인수: UI로 짧은 영상 1편 온라인 스모크 🧑 | ☐ |

---

## P1 — `vlogsrt/verify.py` 신규

**테스트 먼저** (`tests/test_verify.py`): 동일 텍스트 coverage 1.0 / 상이 ~0 /
짧은 감탄사 면제 / `strip_for_compare` 정규화 불변 / 실측 픽스처(whisper+gpt)로
의심 큐 ⊆ 전체 / 순수성(입력 불변, 플래그만 반환).

공개 API:

```python
def chunk_spans(responses, duration) -> list[Span]
def verify(cues, responses, duration, cfg) -> Verification   # 순수 함수
def summarize(verification) -> str   # "검증: 큐 918개 중 41개 의심 (4.5%)"
```

- 청크 유사도: `SequenceMatcher(autojunk=False).ratio()` — 구간 내 큐 텍스트 join
  vs gpt 청크 텍스트 (양쪽 `strip_for_compare`)
- 큐 coverage: matching-block 합 / 큐 길이. `set_seq2` 캐시 재사용
- config 신규 3개: `verify_chunk_sim_floor=0.80`, `verify_cue_match_floor=0.60`,
  `verify_short_cue_chars=4`

DoD: 순수 추가(기존 테스트 무변경), `make check`.

## P2 — 파이프라인 전환 (핵심)

- `pipeline.py`: `STAGES = ("spine","refine","output","verify")`, 기본
  `stop_after="verify"`. run = prepare → fetch_spine → `spine_to_cues`(mapping에서
  이주) → `refine.refine` → validate → **SRT 저장** → try: gpt → verify → 로그/report.
  `baseline()`·`_map_stage` 삭제, `RunResult.verification` 추가.
- `cli.py`: baseline 서브커맨드 제거.
- `ui/service.py`: `_summary`가 conf 대신 의심 큐 수. **`pipeline.run` 시그니처와
  `outputs["srt"]` 계약 불변.**
- 테스트: gpt 텍스트 검증류 삭제, `stop_after=output이면 gpt 무호출`,
  `gpt 픽스처 없어도 SRT`, `검증 on/off SRT 바이트 동일`.

## P3 — report/review 재작업

`output.py` 리뷰 킷(diff_ranges/mark_review_text/to_review_cues) 삭제,
`report.py` conf 우선순위 표 → 검증 의심 큐 표(`| # | 시각 | 자막 | 일치율 | 사유 |`),
`*.review.srt` 산출 중단.

## P4 — 사상 계열 삭제

삭제: `vlogsrt/mapping.py`, `vlogsrt/gapfill.py`, `tests/test_mapping.py`,
`tests/test_gapfill.py`. 슬림: `hangul.py`(→ to_nfc/strip_for_compare),
`models.py`(Cue의 whisper_text·conf 제거, `seg_ids` 유지, TRACE_VERSION 인상),
`refine.py` whisper_text 배관, `validate.py` low_conf/orphan,
`config.py` 사상 블록 10필드, `evaluate.py` 스텁 2개.
픽스처 디렉터리는 손대지 않는다.

## P5 — 가드 재정비

`scripts/check_invariants.py`: 기존 유지 + mapping/gapfill 부재 + verify.py 순수성
+ whisper() 본문 `prompt=` 부재. `tests/test_invariants.py`: 큐 시각 refine 한도 내
(seg_ids 대조), `max_chars_per_cue` 초과 큐 없음. `verify_srt_equivalence.py` 삭제.

## P6 — M1-lite: 필터 제거

`preprocess.py`: `-af` 체인 제거 → `-vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 64k`.
무음 탐지·분할 규칙 유지. `config.py` 필터 6필드 제거. D20 기록
(감지 = verify_timing + 의심률 추이, 복귀 = `-af` 한 줄, `.vlogsrt-cache` 정리 안내).

## P7 — 최종 청소 + 인수

잔존 용어 grep(canonical/절단점/orphan/baseline) → README·RUNBOOK·프리셋 정리.
인수: `make check` / 오프라인 E2E(초과 큐 0, 겹침 0) /
`make verify STEM=fr_day_1_2_fhd` green / `pytest tests/test_ui_service.py`.

## P8 — 사람 인수 🧑

> **Claude Code에게 시키지 마라.** UI로 짧은 영상 1편 온라인 실행 → 귀 테스트
> (싱크), 의심 큐 표 확인. 문제 있으면 로그와 함께 새 태스크로.
