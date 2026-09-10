# Final Cut 프로젝트 작업 인계

마지막 갱신: 2026-09-10 (Asia/Seoul)

이 문서는 사용량 회복 뒤 같은 구현 루프를 정확히 이어가기 위한 현재 상태 기록이다. 기준 명세는 `docs/plans/2026-09-08-final-cut-project.md`, 운영 규칙은 `docs/orchestration/README.md`, 상세 과거 기록은 `docs/orchestration/progress.md`를 따른다.

## 현재 제품 구현 기준점

- 통합 브랜치: `feat/final-cut-project`
- 제품 구현 기준 HEAD: `8742ebf63d6e5c65d28c9a237d98c01b160bf0ac` (이 문서 커밋은 그 다음의 문서 전용 커밋이다.)
- P4-01 최종 후보 `369a2ec59d25e02933ffb07db50bff2693fb3068`은 `f5be5bc45f6fdf4ab14d6ac270f2a026e4649061`로 통합됐다.
- P5-04 최종 후보 `ec4e2b2388134e3eb95ad9a0901c4ae9e2caf81e`은 `6a9a98f8787304812f87880b5557422a819006e9`로 통합됐다.
- P4-02 최종 후보 `092689ba437fc62afff50703d2cb1f4b5c500df1`은 현재 HEAD `8742ebf63d6e5c65d28c9a237d98c01b160bf0ac`로 통합됐다.
- 마지막 P4-02 통합 게이트는 Ruff/포맷 77개 파일, mypy 40개 소스 파일, pytest 1,844개, 구조 불변식 19/19를 모두 통과했다.
- P0, P1, P2, P3과 P5-00/P5-01/P5-02는 기존 ledger에 기록된 상태로 이미 통합돼 있다. P1/P2의 실제 Final Cut 및 77분 미디어 인수 검사는 `PENDING_HUMAN`이다.

## 일시 정지 직전 완료된 작업

### P4-03 BoundaryTranscript

- 작업: retained-audio-only 경계 전사, 캐시·출처 연결·시간 좌표·실패 결과 보존
- Worktree: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p4-boundary-transcribe`
- Branch: `Ssojux2/fcp-p4-boundary-transcribe`
- 기준 SHA: `8742ebf63d6e5c65d28c9a237d98c01b160bf0ac`
- Orca child Run: `run_f296bd2c14fd`
- Task/Dispatch: `task_2088f4a303e7` / `ctx_245e5eda0360`
- 모델: `claude-glm`, `glm-5.3[1m]`, effort `max`, auto mode
- 소유 파일: `vlogsrt/project/boundary.py`, `tests/project/test_boundary_transcribe.py`
- 완료 후보: `c7b6c4f346253e9c54151ed9826a65cf093e7469`
- 작업 트리는 깨끗하며 기준 `8742ebf` 이후 변경은 위 두 소유 파일뿐이다.
- GLM 검증: 새 전용 테스트 54개, Ruff lint/format, mypy, 전체 pytest 1,898개, 구조 불변식 전체 통과.
- 구현된 계약: 요청 sample range의 저장 PCM 바이트만 사용하는 경계 WAV, 8개 캐시 식별 요소, `source_id`와 `pcm_analysis_key`의 순서 독립 연결, 자산별 boundary namespace, 원자적 manifest, P3 엔진 순서와 polish 1회, decoded-local cue offset 1회, 양·음 PCM origin 예약, job-kind별 응답 검증, typed empty/error 보존, `cut_truncated` 비재귀 종료.
- Fable 검토와 메인 통합은 아직 실행하지 않았다. 사용량 회복 뒤 Sol 독립 검사와 새 Fable 5.1 high 읽기 전용 검토부터 재개한다.

## P5-05 미통합 후보와 차단 결함

- Worktree: `/Users/ssojux2/orca/workspaces/vlogsrt/fcp-p5-vision-policy`
- Branch: `Ssojux2/fcp-p5-vision-policy`
- 후보 체인: `2a25009dc2592201abaf313c376e37daa99783db` → `f3a49111c2eb3cd5fce562f833736916abbaf125` → `693aa5b68dccb39a5baeec4d88a480b3f19b3d03`
- `693aa5b`까지 반영된 보정:
  - 손상·거대·깊은 JSON 응답을 `error/keep_all`로 닫음
  - `base_url=None`을 공개 OpenAI 엔드포인트에 고정해 기존 STT 환경 변수 상속 차단
  - OpenAI SDK 내부 재시도를 `max_retries=0`으로 고정
  - 실효 프레임 수를 `min(config, 16)`으로 제한
  - 실효 승인 신뢰도를 `max(config, 0.8)`로 제한
- 독립 게이트: Ruff/포맷, mypy 41개 파일, pytest 1,896개, 구조 불변식 19/19 통과.
- **통합 금지:** root가 정확한 `693aa5b`에서 `ProjectConfig.vision_apply_confidence=float("nan")`을 주면 신뢰도 0.5가 `accepted/cand-mid`로 승인되는 우회를 재현했다. Python의 `max(NaN, 0.8)` 결과가 NaN이어서 비교가 거짓이 된다.
- 이전 Fable Task `task_b9f9feafb8bd`는 95/96 적대 검사를 수행했지만 NaN 생성자 경로와 전체 마무리 전에 Claude 사용량 제한에 도달했다. Orca에는 PASS로 기록됐어도 최종 승인으로 쓰지 않는다.
- 재개 후 첫 P5 작업은 정확한 `693aa5b`에서 새 GLM 보정 태스크를 만드는 것이다. 프로그램식 NaN/비유한 신뢰도 설정을 fail-closed로 만들고, 잘못된 프로그램식 프레임 한도도 16장 제한을 우회하지 못하게 검증한다. red/green 회귀, 전체 게이트, 보정 SHA의 새 Fable 5.1 high 완료 검토가 필요하다.

## 관리자와 모델 상태

- Root/Astra Run: `run_73768f7c4dec`, terminal `term_892e5ca1-805d-4d8d-b27f-fe7c90994848`
- Sol child Run: `run_f296bd2c14fd`
- 이전 Sol Dispatch `ctx_ce78c69a2297`은 요금 알림 처리 중 `gpt-5.6-luna medium`으로 잘못 전환돼 중단됐다.
- 복구 Sol Dispatch: `ctx_a25f6fa710fa`, terminal `term_d1ea1ffc-eeea-4f91-9984-045fe58ccd6c`
- 복구 실행의 실제 모델/effort는 `gpt-5.6-sol` / `xhigh`로 확인됐다.
- P4-03 완료 결과를 회수한 뒤 복구 Sol Dispatch도 사용량 보존을 위해 `operator_close`로 정지했다. 재개 시 같은 manager task를 `gpt-5.6-sol xhigh` 새 retry Dispatch로 다시 시작한다.
- 사용자의 일시 정지 지시: 당시 실행 중이던 P4-03 GLM만 현재 작업을 완료했다. P5-05 보정, 새 Fable 검토, 다음 DAG 태스크는 사용량 회복 전 시작하지 않는다.

## 사용량 회복 뒤 재개 순서

1. P4-03 후보 `c7b6c4f346253e9c54151ed9826a65cf093e7469`와 깨끗한 worktree를 이 문서 및 Orca 상태에서 확인한다.
2. P4-03 후보를 Sol xhigh가 독립 검사하고, 새 Claude Fable 5.1 high가 읽기 전용 최종 검토한다.
3. 두 검토가 통과하면 Astra/root만 현재 메인에 정확한 후보를 병합하고 새 통합 `make check`를 실행한다.
4. P5-05 `693aa5b`의 NaN/잘못된 설정 우회를 새 GLM max 태스크로 보정하고, Sol 및 새 Fable 검토 뒤 root가 병합한다.
5. P4-04 경계 투영을 완료한다.
6. 통합된 P4-04와 P5-02를 기준으로 P5-03 guard union을 구현한다.
7. P6-01 블록 분할 → P6-02 EditPlan → P6-03 EDIT 자막 → P6-04 원자적 패키지 내보내기를 진행한다.
8. P7-01 workspace → P7-02 runner → P7-03 CLI → P7-04 reproject와 P7-05 UI 병렬 작업 → P7-06 오프라인 인수 검사를 진행한다.
9. 최종 오프라인 흐름은 동일 revision의 FCPXML, SOURCE SRT, EDIT SRT, review JSON, manifest를 함께 내보내고 러프컷이 실제로 짧아졌음을 합성 데이터로 증명해야 한다.
10. 실제 Final Cut import/re-export, 원본 4K relink/playback, 실제 77분 미디어, 유료 STT/vision은 계속 `PENDING_HUMAN`으로 남긴다.

## 보존 규칙

- 사용자 소유 `.env.example` 변경은 읽기·수정·스테이징·되돌리기·해시·복사하지 않는다.
- `.env`, 키, `golden/**`, 보호 fixture, 사용자 미디어와 Final Cut 라이브러리를 만지지 않는다.
- paid/live STT·vision 호출, `--online`, push, deploy를 실행하지 않는다.
- 후보 브랜치와 worktree는 root 검토와 재개를 위해 보존한다.
- 각 병합 전후에는 정확한 SHA, diff 범위, focused/full 검사, Fable 판정을 확인한다.
