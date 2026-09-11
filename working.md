# Final Cut 프로젝트 작업 인계

마지막 갱신: 2026-09-10 (Asia/Seoul)

이 문서는 현재 구현 루프의 상태 기록이다. 기준 명세는 `docs/plans/2026-09-08-final-cut-project.md`,
운영 규칙은 `docs/orchestration/README.md`, 상세 과거 기록은 `docs/orchestration/progress.md`를 따른다.

## 이번 세션의 운영 모델

사용자가 2026-09-10에 역할을 이렇게 못 박았다. 상세 규칙은
`docs/orchestration/verification-protocol.md`.

| 역할 | 모델 | 하는 일 |
|---|---|---|
| 계획·오케스트레이션·머지 게이트 | Opus 5 (이 세션) | 태스크를 쪼개고 브리프를 쓰고 워커를 붙이고 머지를 결정한다 |
| 구현 | `claude-glm` `glm-5.3[1m]` max | 제품 코드와 테스트의 유일한 작성자 |
| 검증 — 토큰 무거운 부분 | `claude-glm` `glm-5.3[1m]` max | **구현자와 다른** fresh 워커. 게이트 실행·계약 한 줄씩 대조·적대적 재현·구현자 수치 재측정 |
| 계획·방향성의 자세한 검토 | **Fable 5.1 high** | **상시 역할.** 브리프가 명세와 맞는지, 시그니처가 필요한 값을 나르는지, 의존성 순서가 옳은지 |

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
- **제품 구현 기준 HEAD: `ad80db5` (Merge P6-02 deterministic EditPlan with a machine-checked removal validator)**
- 통합 게이트: Ruff lint/format 86개 파일, mypy 43개 소스 파일, pytest **2,368개**, 구조 불변식 **20/20** 통과
  (P6-01이 20번째 검사 '제거 계층은 오디오를 듣지 않는다'를 더했다).
- **P0~P5 전 페이즈 완료.** 남은 것은 P6(러프컷 생성·내보내기)와 P7(영속화·CLI·UI·인수)뿐이다.
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

## 진행 중

P6-03 EDIT 자막 투영 — GLM 구현 중 (`task_dd7768a2992c` / `ctx_ef227a36561f`).

## 남은 순서

1. **P6-03** EDIT 자막(진행 중) → **P6-04** 원자적 패키지 내보내기.
2. **P7-01** workspace → **P7-02** runner → **P7-03** CLI → [**P7-04** reproject · **P7-05** UI]
   → **P7-06** 오프라인 인수 검사.
3. 최종 오프라인 흐름은 동일 revision의 FCPXML, SOURCE SRT, EDIT SRT, review JSON, manifest를
   함께 내보내고 러프컷이 실제로 짧아졌음을 합성 데이터로 증명해야 한다.
4. 실제 Final Cut import/re-export, 원본 4K relink/playback, 실제 77분 미디어, 유료 STT/vision은
   계속 `PENDING_HUMAN`으로 남긴다.

## 보존 규칙

- 사용자 소유 `.env.example` 변경은 읽기·수정·스테이징·되돌리기·해시·복사하지 않는다.
- `.env`, 키, `golden/**`, 보호 fixture, 사용자 미디어와 Final Cut 라이브러리를 만지지 않는다.
- paid/live STT·vision 호출, `--online`, `VLOGSRT_OFFLINE=0`, push, deploy를 실행하지 않는다.
- 후보 브랜치와 worktree는 검토와 재개를 위해 보존한다.
- 각 머지 전후에는 정확한 SHA, diff 범위, focused/full 검사 결과를 확인하고 기록한다.
- 임계값·기대값을 고쳐 테스트를 통과시키지 않는다(`cut_*`·`verify_*`·`repair_*`·`polish_*` 예외는 근거를 남긴다).
