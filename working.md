# Final Cut 프로젝트 작업 인계

마지막 갱신: 2026-09-13 (Asia/Seoul)

이 문서는 현재 구현 루프의 상태 기록이다. 기준 명세는 `docs/plans/2026-09-08-final-cut-project.md`
＋ **개정 `docs/plans/2026-09-13-cut-first-and-plugin.md`(컷→자막 순서·플러그인)**,
운영 규칙은 `docs/orchestration/README.md`, 상세 과거 기록은 `docs/orchestration/progress.md`를 따른다.

## 이번 세션의 운영 모델

사용자가 2026-09-10에 역할을 이렇게 못 박았다. 상세 규칙은
`docs/orchestration/verification-protocol.md`.

| 역할 | 모델 | 하는 일 |
|---|---|---|
| 오케스트레이션·머지·최종 판단 | Opus 5 (이 세션) | 워커 배치, Run inbox, 검토 충돌 판정, 머지와 통합 게이트 |
| **종합·브리프·ledger** | **`gpt-5.6-sol` xhigh** | **2026-09-12 복귀.** 두 검토 보고서 종합, 보정 브리프 초안, ledger 갱신, 다음 태스크 선검토 |
| 구현 | `claude-glm` `glm-5.3[1m]` max | 제품 코드와 테스트의 유일한 작성자 |
| 검증 — 토큰 무거운 부분 | `claude-glm` `glm-5.3[1m]` max | **구현자와 다른** fresh 워커 |
| 계획·방향성의 자세한 검토 | Fable 5.1 high | 브리프가 애초에 옳은지 |

- **2026-09-13: Opus 사용량 보전 이관.** 토큰 무거운 일(보고서 정독·문서 본문 작성·
  머지 준비·코드 조사)은 전부 `gpt-5.6-sol`로 넘겼다. Opus는 짧은 판정과 최종 머지만
  한다. 세 검토 층의 분리는 그대로다 — 상세는 `docs/orchestration/verification-protocol.md`.
- Orca Run: `run_a091d4869a98` (coordinator terminal `term_7c509adc-43bc-40e3-ab6c-0e374d516231`)
- 이 저장소에 `run_phase.sh`나 `--director` 플래그는 **없다** — 오케스트레이션은 Orca가 한다.
- 태스크 하나가 끝날 때마다: GLM 검증 → Fable 방향 검토 → Opus 머지 → `make check` →
  이 문서와 ledger 갱신 → 커밋.
- 세 층이 서로 다른 것을 본다. GLM 검증자는 "구현이 브리프대로인가", Fable은 "브리프가
  애초에 옳은가", Opus는 진행과 머지다. **P4-04가 가운데 층에서 걸렸다** — 게이트는 전부
  초록이고 구현은 브리프대로였는데 계약 자체가 결함이었다.
- 이전 Run(`run_73768f7c4dec` / `run_f296bd2c14fd`)과 Sol/Astra Dispatch는 정지된 채 둔다.

## 현재 제품 구현 기준점

- 통합 브랜치: `feat/final-cut-project`
- **제품 구현 기준 HEAD: `b1fbc45` (D34 비전 판정 모델 Gemini 전환)**
  — 그 앞의 페이즈 기준은 `97e1063` (Merge P6-03 EDIT caption projection through plan runs)
- 통합 게이트: Ruff lint/format, mypy, pytest **2,396개**, 구조 불변식 **20/20** 통과
  (P6-01이 20번째 검사 '제거 계층은 오디오를 듣지 않는다'를 더했다. D34가 금지 시험을
  두 비전 접두사로 parametrize해 +4개).
- **P0~P5 전 페이즈 완료.** 남은 것은 P6-04(내보내기), **R 페이즈(순서 역전)**,
  P7(재작성 — 영속화·러너·CLI·인수), **X 페이즈(Workflow Extension)**다.
  2026-09-13에 계획이 뒤집혔다 — 아래 「계획이 뒤집혔다」 절을 먼저 읽어라.
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

### P5-03 guard union + P5-03b 레일·신원 — 통합 완료 (`4e185f3`). **P5 페이즈 닫힘**

- 후보 `f2a798f` → 보정 **`5387c6c`** (branch `Ssojux2/fcp-p5-guard`, base `daadf29`)
- **새 3층 프로토콜의 첫 적용**이었고, 두 층이 같은 코드에 다른 답을 냈다.

| 층 | 판정 | 답한 질문 |
|---|---|---|
| GLM 검증자 (`ctx_38f921126576`) | **ACCEPT** 결함 0 | "구현이 브리프대로인가" → 그렇다 |
| Fable 5.1 (`ctx_b84bc98e8830`) | **SOUND_WITH_CONCERNS** | "브리프가 애초에 옳은가" → 아니다 |

모순이 아니다. 구현은 실제로 브리프대로였고 **브리프가 틀렸다.**

#### Fable이 재현한 결함 — 전부 `status="success"`로 나왔다

```
padding=-1/2         → protected=[]        ★ 보호 구간이 통째로 빈다
vad_positive=nan/1.5 → VAD 창이 전부 사라진다
prob=nan / 뒤집힌 창 / 범위 밖 창 → 조용히 드롭
windows가 일부만 덮음 → 나머지가 비speech 취급
stale origin 참조     → 프레임이 12→37 로 조용히 밀린다
```

`protected=[]`인데 `success`는 이 시스템 최악의 실패다 — guard는 말을 자르지 않으려
존재하는데 비면 전부 잘라도 된다는 뜻이 된다. 음수 패딩은 whisper 세그먼트 **자체**를
줄여서 합집합이 합집합이 아니게 된다.

두 결함 모두 **이미 배운 교훈의 재발이고 Opus 브리프의 책임이다**:
P4-04b의 **산술** 교훈만 넣고 **신원** 교훈을 빠뜨렸고, P5-05b의 config-bypass 교훈은
**같은 base에서 여섯 커밋 전에 닫아놓고도** 반영하지 않았다.
Fable은 내 브리프 문구도 명시적으로 반박했다 — `source_id`·`total_samples`·`sample_rate`
검증 셋으로는 연결이 유일해지지 않는다(probe f).

#### 검증 층의 구조적 한계가 드러난 지점

GLM 검증자는 같은 신원 구멍을 **보고도** "브리프 검사 목록 밖이라 계약 위반이 아니다"로
분류했다. 판단 기준이 브리프이기 때문이다. 같은 이유로 검증자의 적대적 재현
(무작위 200세트·퍼징 300)은 `padding=-1/2`를 **시도조차 하지 않았다** — 그 축이
검사 목록에 없었다. **이것이 검증을 두 층으로 나눈 이유의 실증이다.**

#### P5-03b 보정 (`5387c6c`) — 7항목

1. `_require_config`: 설정 결함은 `raise` (호출자 잘못)
2. VAD 입력 검증: 입력 결함은 `keep_all` (모델/데이터 잘못) — 둘의 선을 나눴다
3. `SourceGuard` += `analysis_key`, `pcm_sample0_file_time`; `project_guard`가 대조
4. `VadResult` += `analysis_key` (**P5-02 재개방 승인** — 소비자 3곳 확인 후)
5. `keep_all`이 inaudible과 같은 whole-slice 경로 사용
6. `InstanceGuard` += `status: Literal["success","keep_all","inaudible"]`, `source_id`
7. `repairs` 매개변수는 **유지하고 시계 명시** — 떨어뜨리면 수정 금지된 기존 테스트가
   깨지기 때문이다(워커 판단, 타당)

#### Opus 검증

Fable의 원본 `probe.py`를 **수정 없이 그대로** 고친 코드에 재실행했다. 위 결함이 전부
`raise` 또는 `keep_all 전체보호`로 닫혔고 **baseline 정상 경로는 그대로 살아 있다**.

probe (d)만 이전과 같은 `success`로 나와 파고들었더니, 워커가 수정한 테스트 헬퍼
`_build`가 `[0, sample_count)` 필러 창을 자동으로 덧붙여 **probe 입력이 타일링돼 무효화된
것**이었다 — 코드 결함이 아니다. 헬퍼 없이 `build_source_guard`를 직접 불러 확인했다:
부분 덮음·고확률 부분 덮음·가운데 구멍 전부 `keep_all` 전체보호, 정상 타일링만 `success`.
저장소 자체 테스트도 자동 필러가 없는 `_result()`로 직접 호출해 이 축을 덮는다.
기존 27개 테스트에서 **삭제된 assert 0개**.

- 독립 게이트: Ruff 83파일, mypy 42파일, 불변식 **19/19**(직접 셈), pytest 2,094개.
- 통합 후 `make check` 통과: pytest **2,094개**, 불변식 19/19.

### P5-06 요청 신원 + 생성자 레일 — 통합 완료 (`eeb8e7a`)

- 후보 **`3b33c53`** (branch `Ssojux2/fcp-p5-upstream`, base `2f9b9a8`)
- Fable의 P6 사전 검토가 지목한 **상류 부족 1·2**. P6 시작 전 상류를 고칠 마지막 기회였다.
- 항목 1 `VisionDecision.request_sha256`: 저장된 판정이 **이 프레임과 이 후보를 실제로
  봤는지** 증명한다. 세 경로(accepted·keep_all·error) 전부에 실린다.
- 항목 2 `ProjectConfig.__post_init__`: 범위 검사가 `load_project_config`에만 있어서
  프로그램식 생성이 건너뛰던 우회를 **생성자에서** 닫았다. 이 저장소가 같은 우회를
  P5-05b·P5-03b·P6에서 **세 번** 만났고, 세 계층이 같은 검사를 각자 다시 짜고 있었다.

#### 워커가 멈추고 물었다 — 그리고 Opus가 놓친 것을 찾았다

항목 2가 `test_vision_policy.py`의 4개 테스트(24 인스턴스)를 깨뜨렸다. `replace(CFG, ...)`가
`pytest.raises` 블록 **밖**에 있어서 생성자가 그 줄에서 터진다. 워커는 브리프대로
**고치지 않고 멈춰서 물었다.**

Opus가 확인하다 더 위험한 것을 찾았다: `test_guard.py`는 "깨지지 않았다"고 보고됐지만
`replace()`가 `raises` 블록 **안**이라 초록인 채로 **의미를 잃었다** — `ValueError`가
`guard._require_config`가 아니라 생성자에서 왔다. 시끄러운 실패는 잡혔고 조용한 침식은
안 잡혔다. **검사받지 않는 방어층은 썩는다** — 이 저장소가 같은 우회를 세 번 만난 이유다.

Opus 결정: 워커가 추천한 (A)를 거절했다. `_require_config`가 공개 생성 경로로 검사
불가능해지면 죽은 코드가 되고 누가 지워도 아무 테스트도 실패하지 않는다.
대신 **위조 인스턴스**(`object.__new__`로 `__init__` 우회) 회귀를 **두 파일 모두**에
넣고, 적응시킨 테스트는 **이름도 바꿔** 의미 변화가 리뷰에 보이게 했다.

#### Opus 검증 — mutation을 직접 재현했다

워커가 지시를 넘어 각 레일을 일부러 망가뜨려 빨개지는지 확인하고 복원했다. Opus가 같은
것을 독립적으로 다시 돌렸다:

| 무력화 | 결과 | 복원 |
|---|---|---|
| `policy._require_config` | test_vision_policy.py **24 failed** | 바이트 그대로 |
| `guard._require_config` | test_guard.py **1 failed** | 바이트 그대로 |
| `ProjectConfig.__post_init__` | test_project_config.py **45 failed** | 바이트 그대로 |

가운데 줄이 위 구멍의 답이다 — 재위조를 거쳐 그 테스트가 다시 `guard._require_config`를
실제로 검사한다. `_forged` 헬퍼는 테스트에만 있고 제품 코드에 새지 않는다(grep 확인).

- 독립 게이트: Ruff 83파일, mypy 42파일, 불변식 **19/19**(직접 셈), pytest 2,152개.
- 통합 후 `make check` 통과: pytest **2,152개**, 불변식 19/19.

### P6-01 블록 분할 + P6-01b — 통합 완료 (`8a1135f`)

- 후보 `be2c570` → 보정 **`d9cfd5c`**. **Fable 사전 검토를 처음 붙인 태스크**다.
- 사전 검토(`reviews/fable-p6-prephase.md`)가 **시그니처 구멍 7 + 레일 공백 4**를 찾아
  브리프를 다시 쓰게 했다. 그대로 디스패치했으면 잘못된 타입 모양 위에 구현 넷을 태웠다.
  가장 위험했던 것: `keep_seconds=0`이면 **클립이 통째로 삭제**된다.
- 후보 검토에서 두 층이 **겹치지 않는 것**을 찾았다:
  GLM 검증자는 **ACCEPT**(뮤테이션 5형태·독립 해시 재계산·8레이트 기하 재계산까지 했다),
  Fable은 실제 결함 셋 — `keep_frames==1`에서 표본이 `end_frame`으로 나감(인덱스 250/500),
  교차 레일이 정책 천장 `min(cfg,16)`을 안 봄, 그리고 **내가 설계한 import 검사기의
  별명 우회**(`import … as FrameSampleRequest`가 통과).
- 검증자가 놓친 이유가 구조적이다 — 판단 기준이 브리프이고, 그 세 축은 브리프에 없었다.
- Opus 검증: 별명 우회가 닫혔는지 검사기 행을 직접 확인, 기본 설정 id 불변 확인.

### P6-02 EditPlan + P6-02b — 통합 완료 (`ad80db5`). **러프컷이 실제로 짧아진다**

- 후보 `bde0368` → 보정 **`0422987`**
- 합성 픽스처가 **500프레임/20초 → 325프레임/13초**, 제거 구간 175프레임 하나.
  이 프로젝트가 처음으로 구간을 실제로 제거한 지점이다.
- GLM 검증자: 내가 준 7종 위반 중 **6종이 실제로 문다**고 확인하고 정상 계획은 findings가
  빈다는 것, 적용 경로 raise/keep 선, 결정성, 실제 단축, 불변식 뮤테이션까지 재측정했다.
- Fable은 그 목록 **밖**에서 조용히 통과하는 경로 다섯을 손으로 만든 플랜으로 재현했다:
  (A) retained 출력 길이 ≠ 소스 길이 — 보존은 소스를, 타일링은 출력을 세는데 둘을 안 묶었다
  (B) 길이 0 타일을 타일링이 건너뜀 (C) `restore_at_output_frame=9999` 통과
  (D) **`protected=()` 인 `keep_all` guard로 제거 통과** (E) inaudible도 같음.
- **(D)가 `protected=[]`의 다섯 번째 재발**이다 — P5-03b에서 guard 계층에 닫은 결함이
  소비자 계층에서 옷을 갈아입었다. 빈 `protected`를 "보호할 게 없다"로 읽으면
  "제거 권한이 없다"의 정반대가 된다.
- **심각도는 정확히**: 자막이 조용히 사라지지는 않는다(guard 안 큐는 안전, 밖 큐는 P6-03에서
  하드 실패). 사라지는 것은 **보호 밖 footage**다.
- 시그니처 부족 하나 — `retained`가 제거 없는 블록 경계마다 쪼개져서 P6-03·P6-04가
  **각자 같은 병합을 구현**하게 된다. `EditPlan.runs`로 닫았다.
- **내 브리프가 틀린 곳**: "모든 removed는 한 블록 안" 검사를 요구하면서 파티션 없는
  시그니처를 고정했다. 코드가 아니라 **계약을 정직하게** 고쳤다 — 검증기는 구조 관문이고
  출처는 재빌드로 증명한다. P6-04는 같은 프로세스에서 만든 플랜만 렌더한다.
- Opus 검증: **Fable의 원본 probe 스크립트를 수정 없이 재실행**해 A~E·G·H가 전부 닫힌 것을
  확인했고, `runs`가 조각 2개를 1개로 병합하는 것과 정직한 입력의 revision이
  `c62669923df0…435`로 **바이트 동일**한 것을 확인했다.

### P6-03 EDIT 자막 투영 + P6-03b — 통합 완료 (`97e1063`)

- 후보 `a022f96` → 보정 **`d52c809`** (branch `Ssojux2/fcp-p6-captions`, base `5396242`)
- 이 계층은 P6-02가 제거한 footage 위에 **자막을 얹는 곳**이라, 틀리면 자막이 실제로 사라진다.
- 네 핵심 케이스가 전부 닫혔다(두 검토 모두 확인): 자르지 않은 블록 경계를 건너는 큐가
  `runs` 덕에 살아남고, 제거를 건너는 큐는 하드 실패하고, 칼날을 걸친 소스 큐는 두 장으로
  리플 뒤 인접하고, 잠긴 두 칼날의 경계 큐는 한 장으로 남는다.
- GLM 검증자 **ACCEPT_WITH_FINDINGS**(코드 결함 0). 신원 삼중 대조·격자·SOURCE 바이트
  불변·EDIT 시계 접힘 재측정을 전부 독립 재현했다.

#### Fable이 자기 주장을 뒤집었다

P6-02 검토에서 Fable은 "guard 밖 큐는 P6-03에서 하드 실패한다"고 썼고 나는 그걸 P6-02
안전 논증의 근거로 받았다. 이번에 **그 문장이 조건부로만 참**임을 자기가 재현했다:

```
정직한 플랜에서 runs만 [0,500) 하나로 바꾸고 removed=[75,250)은 그대로 →
  validate_edit_plan: finding 1개
  project_edit_captions: 큐를 그냥 내보냄 (00:00:02,000 --> 00:00:11,000)
```

원인은 `_single_instance_shift`가 **run 포함을 먼저** 보고 `_require_not_removed`를
fallback으로 둔 것이고, `project_edit_captions`는 시그니처에 문서도 guard도 없어
`validate_edit_plan`을 **부를 수 없다.** 그래서 보증이 "일관된 플랜이 주어졌을 때"였다.

**내 브리프가 원인이다** — "제거와 겹치는 큐를 거부한다"고만 쓰고
"무조건, run 포함보다 먼저"를 안 적었다. 구현자는 fallback으로 읽었고 자연스러운 독해다.

#### 두 검토자가 갈린 지점 — Fable이 옳았다

GLM 검증자는 "SOURCE에서 접힌 큐가 EDIT에서 표현 가능해져도 여전히 빠진다"를 LOW로 올렸다.
Fable은 **그게 옳다**고 판단했다 — 정확한 길이가 여전히 1ms 미만이고 SOURCE 판정이
durable하다. 대신 수락 차단이 `source.unresolved_cue_ids` 하나뿐이 되므로
**P6-04·P7-04가 빈 EDIT 이슈 목록을 "미해결 없음"으로 읽으면 안 된다**고 기록하게 했다.
코드를 고치지 않고 브리프 양쪽에 못 박았다.

#### 보정과 검증

`d52c809`는 `_require_not_removed`를 run 루프 **위로** 한 줄 올리고 docstring에 왜인지
적었다. Opus 검증: 위조 플랜이 이제 raise하고(red-first `DID NOT RAISE` 확인),
**정직한 입력 7개 시나리오의 EDIT SRT sha256이 수정 전후 동일**하다.
통합 후 `make check` 통과: pytest 2,392개, 불변식 20/20.

#### P7을 위해 미리 채운 것

Fable이 "인코딩을 둘로 만들 마지막 기회"라고 지적했다 — `EditPlan`·`EditCaptionSet`이
`Fraction`·`FrameInterval`을 싣는데 패키지에 JSON writer가 없어서, P6-04가 사적으로
인코딩하면 P7-01이 디코딩을 다시 발명한다. P6-04 브리프에 공개 쌍
(`plan_record`/`edit_captions_record`)을 박았다.

## 2026-09-13 — 계획이 뒤집혔다: 컷 → 자막, 그리고 플러그인

사용자가 방향을 바꿨다. **자막 → 컷편집**이 아니라 **컷편집 → 자막**이고, 납품 형태는
CLI가 아니라 **Final Cut Workflow Extension**이다. 확정 계획:
`docs/plans/2026-09-13-cut-first-and-plugin.md` (2026-09-08 명세를 대체하지 않고 개정한다).

### 검토 기록 — Astra가 v1을 **UNSOUND**로 돌려보냈다

`codex --model gpt-6-astra` xhigh read-only. 차단 6 · 높음 5 · 중간 1.
전문: `docs/orchestration/reviews/astra-replan-cut-first.md`.

세 가지는 **합성 호출로 직접 재현**됐다(파일은 쓰지 않았다):

- 떨어진 `[10,11)`·`[40,41)`·`[70,71)`의 '치즈' 셋이 **61초짜리 되풀이 하나**로 묶여
  repair 요청이 `[10,71)`이 된다 — 빼놓은 58초를 다시 듣는다. 검출기가 시간 간격을
  보지 않고 이웃 텍스트를 묶기 때문이다(`repetition.py:124`, `repair.py:498`).
- 부분 coverage를 가진 `SourceTrace`가 **미해결 표시 없이 SRT를 만든다**.
  `ChunkCoverage`는 "요청한 조각에 응답이 왔는지"만 보고 "필요한 구간을 다 요청했는지"는
  아무도 안 본다(`transcribe.py:648`, `speech.py:151`).
- 그 부분 trace를 나중에 가드 증거로 재사용하면 **미전사 영역이 다시 무보호가 된다**
  (`guard.py:587`) — `protected=[]` 계열의 **여섯 번째 얼굴**이다.

Opus 주장 12건이 뒤집혔다(확정 계획 §7). 특히 **"사람 검토가 전사 보호선보다 강하다"는
틀렸다** — 편집본 재생으로는 이미 사라진 대사를 못 찾고, 오검출을 **먼저 지우면** 그 뒤
전사에서 발견할 기회까지 없어진다. 그래서 안전 수준 A(후보 검증 전사)를 택했다.

### 완료 — D34 비전 판정 모델을 Gemini로 (`b1fbc45`)

- `gemini-3.8-flash` · `GOOGLE_API_KEY` · `GEMINI_BASE_URL`. OpenAI 호환 엔드포인트라
  **코드는 한 줄도 안 바뀌었다** — `create_vision_client`가 이미 공급자 중립이다.
- 난간을 **넓혔다**: `PROJECT_MODEL_LITERAL`이 `glm-`만 잡고 있어서, 공급자를 바꾸면
  `"gemini-…"` 하드코딩을 못 잡을 참이었다. `(?:glm|gemini)-`로 넓히고 glm은 남겼다.
  뮤테이션으로 확인 — `policy.py`에 박으니 `[FAIL]`, 복원하니 20/20.
- 게이트: pytest **2,396개**(+4), 불변식 **20/20**(`[OK]` 줄 직접 셈), ruff·mypy 통과.

## 남은 순서 (개정)

실행 순서: **X-01(사람) · P6-04b → R-00 → R 나머지 → P7(재작성) → X 나머지**

1. **X-01** 스파이크 — 사람만 할 수 있다(Xcode + 설치된 FCP). 입출력 계약을 정하므로
   **선행**이다: 브라우저 드래그로 무엇이 오는가, Apple events로 FCPXML을 돌려보낼 수
   있는가, `FCPXTimeline`이 실제로 주는 것.
2. **P6-04b** 패키지 내보내기 보정 — 후보 `defe23f`는 `ruff format --check` 실패와
   Fable의 P7 블로커 둘을 안고 있다. 보정 브리프는 **아직 커밋되지 않았다**.
   v1의 "R과 무관"은 철회했다 — `publish_package`가 `EditCaptionSet`·`BoundaryProjection`을
   **필수**로 받아서 STT 0회의 컷 패키지를 만들 수 없다(R-07이 다시 연다).
3. **R-00** 계약 결정(규칙 4·9 개정, D35) → **R-01** 가용성 잠금 → **R-02** 증거 있는 가드 →
   **R-03** 범위별 독립 전사 → **R-04** 타임라인 자막 → **R-05** 후보 검증 전사 →
   **R-06** 검토 상태 기계 → **R-07** 컷 전용 패키지 → **R-08** 입력 어댑터.
   **R-09** Gradio 철거(D36)는 의존성이 없어 아무 때나 병행한다.
4. **P7 재작성** — 이름만 바꾸는 것이 아니다. P7-01/02는 부분 요청 키·컷/자막 revision
   관계·범위별 비용으로, P7-03은 `inspect|cut|approve|caption` 표면으로. **`run` 단일
   명령은 두지 않는다**(사람 게이트를 우회한다). **P7-05는 폐기**(R-09).
5. 실제 Final Cut import/re-export, 원본 4K relink/playback, 실제 77분 미디어,
   유료 STT/vision, 공증 앱 설치, **희미한/음악 아래 발화의 실제 검출률**은
   계속 `PENDING_HUMAN`이다.

## 2026-09-16 — 프로토타입 우선, 그리고 codex 소진

### 방향: 프로토타입을 먼저 돌린다

사용자가 "우선 프로그램을 사용할 수 있도록 완성"을 최우선으로 지시했다. **R 페이즈(순서
역전)와 P7 전체는 미룬다.** 현재 순서(자막 → 컷) 그대로 FCPXML 하나를 끝까지 돌려
**실데이터에서 실제로 무엇이 깨지는지 보고** 거기서 고친다.

작업 라인은 worktree `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-prototype`,
브랜치 `Ssojux2/fcp-prototype`.

- `ebfa367` — `Ssojux2/fcp-p6-export`(`defe23f`)를 프로토타입 라인에 머지. **충돌 0.**
  D34(`gemini-3.8-flash`·`GOOGLE_API_KEY`·넓힌 정규식) 보존 확인, 불변식 20/20,
  pytest 2,442개, mypy 44파일. `ruff format --check`만 1파일 실패.
- `83dcc57` — **PROTO-1. 거짓 성공을 막았다.** 그 전까지 export는 비전 판정이 하나도
  없거나 전부 error여도 `final_accepted=true`를 냈다. `edit.py`가 `decision_missing`·
  `decision_error`를 리뷰 이슈로 내고, `export.py`가 `unavailable_declared_no_audio`
  **하나만 통과시키는 허용 목록**으로 차단한다. 차단 목록이면 새 리뷰 코드가 조용히
  문을 여는 구조가 되므로 방향을 뒤집었다.
  `EditPlan`에 원래 블록 수가 없으므로 `decisions == ()` 검사는 **금지**했다 — P4-04
  모양(시그니처가 값을 안 나르면 소비자가 유도한다)이고, 블록이 정말 0개인 프로젝트도
  정상이다.
- **`make check`가 아직 빨갛다** — `tests/project/test_edit_captions.py` 6개. 원인은
  제품 결함이 아니라 **장면이 "분석이 끝났다"고 거짓말하던 것**이다(500프레임 = 블록 2개에
  판정을 안 주고 `review == ()`를 단언). PROTO-1b로 분리해 디스패치했다.

- `3b7337b`+`03e22ae` — **PROTO-1b. 기준선이 초록이 됐다** (pytest 2446). 낡은 장면 6개는
  단언을 한 줄도 안 고치고 **장면 구성만** 바꿔 닫았다(판정 없던 블록에 `keep_all` 부여).
  딸려온 `# type: ignore[arg-type]`은 걷어냈다 — `status`는 **권한을 뜻하는 필드**라
  (`policy.py:130`) 거기 억제를 걸면 프로덕션 Literal이 거부할 값을 테스트가 만든다.
- `d8eed17` — **PROTO-2. 러너 1/3.** `prepare_audio` → `PreparedAudio`.
  `UnavailableSourceAudio`를 **처음으로 만드는 제품 코드**다(`speech.py:184`가 "P7이 만든다"고
  써 두고 여섯 달 비어 있던 자리). pytest **2459**, 불변식 20.

### PROTO-2에서 배운 것

**방향 검토를 둘로 쪼갠 것이 첫 실행에서 값을 했다.** 두 검토자가 겹치지 않는 것을 찾았고
내 주장 셋이 뒤집혔다(브리프 v2 §9에 기록). 가장 무거운 것:

> "같은 애셋을 클립 여럿이 써도 PCM 추출은 한 번 — `resolve_media`가 리소스 단위라
> **이미 그렇다**" → **틀렸다.** 리소스 단위인 것이 원인이다. 클립 여럿 → 리소스 하나는
> 접히지만 **리소스 둘 → 내용 하나는 안 접힌다.** `_reject_duplicate_id`(`fcpxml.py:366`)는
> **중복 `xml_id`만** 거부한다. 접는 것은 `destination` 키뿐이다.

두 검토자가 그 주장에서 **갈렸고**((A) 반박 / (B) 확인) 내가 코드를 읽어 (A)의 손을 들었다 —
(B)는 다른 기제를 겨눴다. **(B)만 찾은 것도 있다**: 미디어 오버라이드가 러너 입력
시그니처에 없어서 실전 remap 경로가 통째로 사라질 뻔했다.

구현자가 브리프를 한 군데 **개선**했다: probe 실패와 `require_ffmpeg` 실패가 같은
`RuntimeError`라 사후 구별이 안 되므로, 도구 게이트를 자원 루프 **앞으로** 뺐다 —
ffmpeg 미설치가 부재 기록 N개가 아니라 한 번의 하드 실패가 된다.

**내가 직접 뮤테이션으로 확인했다**: `pcm_destination`을 `resource_scoped_id`로 키잡게
바꾸니 `test_two_ids_over_the_same_bytes_extract_once`가 `assert 2 == 1`로 죽는다.
레일이 우연히 초록인 게 아니라 실제로 문다.

- `7cae54d` — **PROTO-3. 러너 2/3.** `analyze_audio` → `AnalyzedAudio`.
  부재 소스의 **합성 가드**가 여기서 난다. pytest **2477**, 불변식 20.

### PROTO-3에서 배운 것 — 난간의 주소를 틀렸다

**두 검토자가 같은 주장을 독립으로 반박했다**(수렴이라 판정이 필요 없었다). 내가
*"`edit.py:415`가 불가청 인스턴스의 `success` 가드를 거부하니 `declared_no_audio`를
`keep_all`로 잘못 찍으면 그 자리에서 터진다"* 고 썼는데 **안 터진다** —
`_require_audible_link`는 `success`만 거부하고 docstring이 이유를 적어 놓았다.
저장소 전체에서 `inaudible`과 `keep_all`을 구별해 다루는 자리는 **없다.**

**(A)가 진짜 위험을 혼자 찾았다.** `stream_unavailable`·`pcm_unavailable`은 문서가
`hasAudio=1`로 선언하므로 `_audible`이 그 클립을 **가청**으로 보고
`_require_audible_link`가 불린 적도 없다 — `success`·`protected=()` 합성 가드가
**통과하고 클립 전체가 제거 후보가 된다**(재현: 블록 2개, 500프레임).

> **그래서 v1의 테스트 4는 승인 도장이 될 참이었다.** 구현자가 `declared_no_audio`
> (진짜로 raise한다)로 red-first를 통과시키고 "여섯 번 당한 결함이 막힌다"고 보고하면
> 정작 위험한 두 코드는 한 번도 증명되지 않는다.

테스트 4를 세 줄로 쪼개 **위험한 조합이 raise하지 *않는다*는 것 자체를 못 박게** 했고,
`__post_init__` 다섯 단언으로 A1을 **표현 불가능**하게 만들었다. 내가 단언 3·4를
무력화하는 뮤테이션으로 직접 확인했다 — 대응 테스트 둘이 정확히 죽는다.

(A)는 v1이 **자기 유비를 어긴 것**도 짚었다: ONNX 게이트가 유료 호출 **뒤**에 있어
아티팩트가 깨진 환경에서 77분 회차 전사비를 다 치르고 죽는다. (B)는 나머지 반쪽 —
전 리소스가 `declared_no_audio`인 프로젝트는 VAD를 안 쓰므로 세션을 무조건 만들면
numpy 없는 환경에서 쓰지도 않을 이유로 죽는다.

**둘 다 최우선으로 꼽은 것**: 같은 바이트를 가리키는 리소스 둘 → 같은 `source_id`의
참조가 둘 → `_index_by`가 **값이 같아도** 거부해 정상 입력이 죽는다.
PROTO-2가 접는 것은 **PCM 주소**지 목록이 아니었다.

### DoD는 더 이상 `make check`가 아니다

`make`도 같은 이유로 죽어 있다 — 각 브리프 §7의 여섯 명령을 개별로 돌린다.

남은 순서: **PROTO-4**(블록·비전·edit·export) → **PROTO-5** `commands.py`+CLI →
**PROTO-6** e2e+RUNBOOK → **PROTO-7 실물 FCPXML 실행**(사람과 함께).
마지막이 진짜 관문이다. 보드는 `TASKS.md`의 PROTO 시리즈.

### Xcode 라이선스 미동의로 툴체인 shim이 죽었다

`xcode-select -p`가 `/Applications/Xcode.app/...`을 가리키는데 **그 라이선스가
미동의**라, 그리로 넘기는 shim이 exit 69로 죽는다. `git`·`python3`, 그리고 **`make`까지**.

**"`/usr/bin`이 전부 죽었다"는 내 진술은 너무 넓었다**(PROTO-4 검토가 반박했다) —
`xmllint`는 정상이다(rc 0, libxml 20913). 죽은 것은 Xcode 툴체인 shim뿐이다.
출판 경로가 `validate_xml`을 지나므로 이 구분이 중요하다.

```
You have not agreed to the Xcode license agreements.
```

- **우회로(확인됨)**: `/Library/Developer/CommandLineTools/usr/bin/git` (2.50.1) 정상.
- **사람이 할 일**: `sudo xcodebuild -license accept` 또는
  `sudo xcode-select -s /Library/Developer/CommandLineTools`.
- 그때까지 모든 워커 브리프에 절대 경로를 명시한다.

### codex 소진 — 역할 재배치

`gpt-5.6-sol`과 `gpt-6-astra` 두 자리가 비었다. 전문은
`docs/orchestration/verification-protocol.md`의 2026-09-16 절.

핵심만: **Sol의 자리는 Opus가 회수하고, Astra의 자리는 둘로 쪼갠다.** Astra의 직무는
"코디네이터의 브리프가 틀렸다고 말하는 것"이라 같은 세션의 Opus가 대신할 수 없다.
**(A)** 컨텍스트가 완전히 분리된 `claude -p --model claude-opus-5 --effort high` 워커와
**(B)** 구현자·검증자와 또 다른 `claude-glm` 워커에게 같은 헌장을 주고, **두 보고서의
불일치**를 신호로 쓴다. (B)가 모델 다양성을 Claude 사용량 0으로 복원한다.

정직하게: 이 배치는 Sol·Astra 시절보다 **Claude 사용량을 늘린다.** 그래서 읽는 일은
전부 GLM으로 보내고, 브리프를 짧게 쓰고, 방향 검토를 새 계약·새 타입·새 모듈에만 붙인다.

## 보존 규칙

- 사용자 소유 `.env.example` 변경은 읽기·수정·스테이징·되돌리기·해시·복사하지 않는다.
- `.env`, 키, `golden/**`, 보호 fixture, 사용자 미디어와 Final Cut 라이브러리를 만지지 않는다.
- paid/live STT·vision 호출, `--online`, `VLOGSRT_OFFLINE=0`, push, deploy를 실행하지 않는다.
- 후보 브랜치와 worktree는 검토와 재개를 위해 보존한다.
- 각 머지 전후에는 정확한 SHA, diff 범위, focused/full 검사 결과를 확인하고 기록한다.
- 임계값·기대값을 고쳐 테스트를 통과시키지 않는다(`cut_*`·`verify_*`·`repair_*`·`polish_*` 예외는 근거를 남긴다).
