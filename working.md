# Final Cut 프로젝트 작업 인계

마지막 갱신: 2026-09-10 (Asia/Seoul)

이 문서는 현재 구현 루프의 상태 기록이다. 기준 명세는 `docs/plans/2026-09-08-final-cut-project.md`,
운영 규칙은 `docs/orchestration/README.md`, 상세 과거 기록은 `docs/orchestration/progress.md`를 따른다.

## 이번 세션의 운영 모델

사용자가 2026-09-10에 관리자 계층을 단순화했다. Astra/Sol 2단 관리자 대신 **Opus 5가 직접 조율**한다.

| 역할 | 모델 | 하는 일 |
|---|---|---|
| 조율·검증·머지 | Opus 5 (이 세션) | 계획을 세부 태스크로 쪼개고, 브리프를 쓰고, 결과를 검증하고, 통합 브랜치에 머지한다 |
| 구현 | `claude-glm` `glm-5.3[1m]` effort `max`, `--permission-mode auto` | 제품 코드와 테스트의 유일한 작성자 |
| 상승 | Fable 5.1 | 검증에서 **문제가 났을 때만** 호출해 원인을 잡고 수정 명세/수정을 만든다 |

- Orca Run: `run_a091d4869a98` (coordinator terminal `term_7c509adc-43bc-40e3-ab6c-0e374d516231`)
- 태스크 하나가 끝날 때마다: 검증 → 머지 → `make check` → 이 문서와 ledger 갱신 → 커밋.
- 이전 Run(`run_73768f7c4dec` / `run_f296bd2c14fd`)과 Sol/Astra Dispatch는 사용량 절약을 위해 정지된
  상태로 남는다. 되살리지 않는다.

## 현재 제품 구현 기준점

- 통합 브랜치: `feat/final-cut-project`
- **제품 구현 기준 HEAD: `cb6b039` (Merge P4-04 instance-scoped boundary projection with exact slice anchors)**
- 통합 게이트: Ruff lint/format 79개 파일, mypy 41개 소스 파일, pytest **2,058개**, 구조 불변식 **19/19** 통과.
- **P4 페이즈 완료.** P0·P1·P2·P3·P4 전부와 P5-00·P5-01·P5-02·P5-04·P5-05가 통합돼 있다.
- P1/P2의 실제 Final Cut 및 77분 미디어 인수 검사는 `PENDING_HUMAN`이다.

## 완료된 단계

### P4-03 BoundaryTranscript — 통합 완료 (`f6af955`)

- 후보 `c7b6c4f346253e9c54151ed9826a65cf093e7469` (branch `Ssojux2/fcp-p4-boundary-transcribe`)
- 기준 `8742ebf` 대비 변경은 소유 파일 둘뿐: `vlogsrt/project/boundary.py`, `tests/project/test_boundary_transcribe.py` (+2,265줄)
- Opus 독립 검증(워크트리에서 직접 실행): Ruff lint/format 통과, mypy 40개 파일 통과,
  pytest 1,898개 통과, 구조 불변식 19/19 통과.
- Opus 계약 대조 검토에서 확인한 것:
  - 캐시 키 재료에 경로·stem·목록 위치가 없다 — 원본 SHA·PCM 분석 키·PCM SHA·채널·주파수·
    정확한 `sample_range`·소스 트레이스 분석 키·모델·설정 지문뿐이다.
  - 연결은 `(source_id, pcm_analysis_key)` 쌍으로만 닫히고 중복 색인을 거부하며,
    참조와 트레이스의 `pcm_sample0_file_time` 원점 일치를 따로 증명한다.
  - `_require_retainable`이 호출 **앞에서** 요청 범위를 저장 PCM 안으로 묶는다.
  - 재전사 구간은 `_clamped_repair_range`가 남긴 범위로 ceil-start/floor-end 클램프하고,
    비면 살리지 않고 죽는다 — 제거된 오디오의 문맥이 들어올 길이 없다.
  - 시계 변환이 정확히 한 번씩이다: 국소 → `decoded_origin` 한 번 → 파일 원점 한 번.
  - `_finite`/`_bad_count`가 NaN·무한대·`bool`을 숫자 자리에서 걷어낸다(P5-05의 우회와 같은 계열).
- 통합 후 `make check` 전체 통과.

### P5-05 비전 판정 정책 + 레일 fail-closed — 통합 완료 (`14b60bf`)

- 후보 체인 `2a25009` → `f3a4911` → `693aa5b` → **`05a7455`** (branch `Ssojux2/fcp-p5-vision-policy`)
- P5-05b 브리프: `docs/orchestration/tasks/p5-05b-vision-policy-rails.md`
- Orca Task `task_35a3d06e8b32`, Dispatch `ctx_b488232db344`, GLM terminal `term_d8dc9849-86d4-4bda-961c-d7a82f8b8428`
- 닫은 결함: `ProjectConfig`의 수치 검증은 `load_project_config`에만 있어서 프로그램식 생성이
  그것을 건너뛴다. 그러면 `max(NaN, 0.8) == NaN`이고 `0.5 < NaN`이 거짓이라 신뢰도 0.5짜리
  후보가 `accepted`로 승인됐다. 프레임 천장도 `min(NaN, 16) == NaN`으로 같이 뚫렸다.
- 구현: `_require_config(cfg)`가 `build_payload`와 `parse_decision` **양쪽의 첫 관문**이고,
  `parse_decision`에서는 `_error_decision`을 만드는 `try` **바깥**에 있다 — 호출자의 버그가
  "모델이 계약을 어겼다" 기록으로 세탁되지 않는다. `bool` 제외·유한·값역을 한 번에 본다.
- Opus 독립 검증: Ruff lint/format 통과, mypy 41개 파일 통과, pytest 1,928개 통과,
  구조 불변식 **19/19** 통과. GLM 보고서의 "15/15"는 보고상의 오기였고 실제 게이트는 19개 전부 통과다.
- Opus 적대적 재현(고친 트리에서 직접 실행): `vision_apply_confidence`의 NaN·±inf·-0.5·`True`와
  `vision_max_images`의 NaN·0·`True`가 전부 `VisionPolicyError`로 닫혔다. 같은 값들이
  `build_payload`에서도 닫혔다. 레일 방향도 그대로다 — 0.95로 조이면 0.93이 `keep_all`이 되고
  0.10으로 풀어도 바닥은 0.8 아래로 내려가지 않는다. `accepted`가 여전히 도달 가능함을 함께
  확인해 위 결과가 "전부 막혀서 통과한 것"이 아님을 증명했다.
- 통합 후 `make check` 전체 통과: pytest 2,022개, 구조 불변식 19/19.

### P4-04 경계 투영 + P4-04b 정확한 슬라이스 닻 — 통합 완료 (`cb6b039`)

- 후보 `0ef58b2` → 보정 **`f3475f6`** (branch `Ssojux2/fcp-p4-boundary-projection`, base `9747988`)
- Orca Task `task_2607661f58fe`/`ctx_fc4e96c3bf70`, 보정 `task_777a4117d4f5`/`ctx_5e43ee83e4a0`
- 1차 후보는 게이트가 전부 초록이었지만(1,931개) 검토에서 계약 결함이 나왔다:
  `project_boundaries`가 타임라인 이동량 `k`를 **묶인 큐의 두 시계에서 유도**했다.
  `BoundaryPlan`이 `LogicalSlice`를 싣지 않아서 정확한 닻에 손이 닿지 않았기 때문이다 —
  브리프의 계약(`project_boundaries(plan, ...)`)이 만든 결함이지 GLM의 실수가 아니다.

#### Fable 5.1 판정 (`docs/orchestration/reviews/fable-p4-04-timeline-shift.md`)

Opus가 세 가지를 주장했고 Fable이 독립 검증해 **둘을 반박**했다. 기록으로 남긴다:

| 주장 | 판정 | 근거 |
|---|---|---|
| 전제: `k`는 슬라이스 안에서 상수다 | **확인** | `_continues`가 합쳐진 칼날의 두 시계 끝점을 같게 강제하고 retime은 ingest에서 거부된다 |
| ① 시각을 유도한다 | **확인** (계약 결함) | 정확한 값이 `LogicalSlice`에 있는데 하류 산출물에서 재구성한다. 다만 whisper 시각은 건드리지 않으므로 규칙 1의 문자적 위반은 아니다 |
| ② 정상 입력에서 실패할 수 있다 | **반박** | 좁혀지지 않는 경우는 슬라이스에 경계를 걸친 큐가 정확히 하나인 세계뿐이고, 그때 두 가설은 `k`와 같거나 범위 밖이다 |
| ③ 조용히 틀린 `k`를 고를 수 있다 | **반박** | 틀린 값이 범위에 들려면 요청이 슬라이스 밖이어야 하는데 `_sealed_request`가 그런 요청을 못 만든다 |

Fable의 퍼즈: 프레임 정렬 6,000 세계 / 요청 7,191개 / 비수렴 분기 1,209회 → **raise 0, 틀린 k 0**.
즉 코드는 **오늘 옳다**. 고친 이유는 그 온전함이 다른 계층의 검증되지 않은 불변 넷
(칼날별 묶음 의미, P3 큐 비겹침, `_closure` ⊇ 큐 span, 요청 ⊆ 슬라이스) 위에 서 있어서,
P4-02 폐쇄나 P3-03 배치가 바뀌면 죽은 분기가 살아나는데 그걸 잡을 테스트가 없기 때문이다.

#### P4-04b 보정 (`f3475f6`)

- `BoundaryRequest`에 `timeline_start` 추가, `_sealed_request`가
  `slice.timeline_start + (start − slice.source_start)`로 채운다 — 명세의 공식 그대로다.
- `_boundary_project_cues`가 `request.timeline_start − request.source_start`를 쓰고
  `_slice_timeline_shift`와 `by_slice`/`shifts` 장부는 삭제됐다.
- `_require_anchor_matches_cue`는 **검사지 대체 계산이 아니다** — 닻이 묶인 큐의 두 시계
  틈 밖이면 유도하지 않고 닫는다.
- **신원 보존**: `timeline_start`는 `_request_id` 재료에 없다. Opus가 `_request_id`와
  `_boundary_cache_key` 함수 본문이 변경 전후 바이트 그대로임을 diff로 확인했고,
  GLM이 박은 다이제스트 `b9bd7014…`를 **변경 전 트리(`0ef58b2`)에 임시 워크트리를 만들어
  직접 돌려** 통과시켰다 — 순환 논증이 아님을 증명했다.
- Opus 독립 게이트: Ruff, mypy 40개 파일, 구조 불변식 **19/19**, pytest 1,934개 통과.
- 통합 후 `make check` 전체 통과: pytest 2,058개, 구조 불변식 19/19.
- Fable이 함께 확인: 인스턴스 스코프·밀리초 접힘·연결 검증에 다른 결함 없음.
  P6를 위한 비대칭 하나를 남겼다 — 내부 칼날을 걸친 소스 큐는 인스턴스마다 `ProjectCue`가
  둘인데 여러 칼날을 덮는 경계 큐는 `instance_ids`를 다 실은 하나다.

## 진행 중

(없음 — 다음 태스크 대기)

## 남은 순서

1. **P5-03** guard union — 통합된 P4-04와 P5-02가 선행조건이다. **지금 착수 가능하다.**
2. **P6-01** 블록 분할 → **P6-02** EditPlan → **P6-03** EDIT 자막 → **P6-04** 원자적 패키지 내보내기.
3. **P7-01** workspace → **P7-02** runner → **P7-03** CLI → [**P7-04** reproject · **P7-05** UI]
   → **P7-06** 오프라인 인수 검사.
4. 최종 오프라인 흐름은 동일 revision의 FCPXML, SOURCE SRT, EDIT SRT, review JSON, manifest를
   함께 내보내고 러프컷이 실제로 짧아졌음을 합성 데이터로 증명해야 한다.
5. 실제 Final Cut import/re-export, 원본 4K relink/playback, 실제 77분 미디어, 유료 STT/vision은
   계속 `PENDING_HUMAN`으로 남긴다.

## 보존 규칙

- 사용자 소유 `.env.example` 변경은 읽기·수정·스테이징·되돌리기·해시·복사하지 않는다.
- `.env`, 키, `golden/**`, 보호 fixture, 사용자 미디어와 Final Cut 라이브러리를 만지지 않는다.
- paid/live STT·vision 호출, `--online`, `VLOGSRT_OFFLINE=0`, push, deploy를 실행하지 않는다.
- 후보 브랜치와 worktree는 검토와 재개를 위해 보존한다.
- 각 머지 전후에는 정확한 SHA, diff 범위, focused/full 검사 결과를 확인하고 기록한다.
- 임계값·기대값을 고쳐 테스트를 통과시키지 않는다(`cut_*`·`verify_*`·`repair_*`·`polish_*` 예외는 근거를 남긴다).
