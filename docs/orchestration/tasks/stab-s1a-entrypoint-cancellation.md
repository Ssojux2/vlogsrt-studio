# STAB-S1A — 엔진 진입점과 실행 중 취소 연결

## 목표

새 작업의 실제 `vlogsrt-engine` 진입점이 스테이징 파일과 작업공간을 안전한 순서로 만들고, 앱에서 실행 중 기록한 취소가 Python 엔진의 다음 외부 호출을 막게 한다. 이 작업은 S1의 실행/취소 절반만 구현하며 비전 판정 상태 스키마는 후속 STAB-S1B에서 다룬다.

## 기준과 재현 결함

- 기준 HEAD는 `0d91da1`, 브랜치는 `Ssojux2/r08-clip-input`이다.
- `macos/scripts/vlogsrt-engine.py`는 새 job에서 `workspace`가 생기기 전에 `_propagate_staged`를 호출해 `media-grant.json` 복사 시 `FileNotFoundError`가 난다.
- 앱의 `CancellationStore`는 job root들에 기록하지만 Python `_check_cancellation`은 실행 중인 `workspace/cancellation.jsonl`만 읽는다. 시작 뒤 취소는 초기 복사만으로 전달되지 않는다.
- 기존 helper 단위 시험만으로 끝내지 말고 실제 엔진 진입점과 앱→엔진 경계 시험을 추가한다.

## 소유 범위

- 제품/테스트 코드만 수정한다. 예상 범위는 아래와 같으며 필요한 인접 파일은 최소한으로 추가할 수 있다.
  - `macos/scripts/vlogsrt-engine.py`
  - `vlogsrt/project/commands.py`
  - `macos/Sources/FinalCutAgentCore/CancellationStore.swift`
  - 관련 Python/Swift 테스트
- `docs/**`, `.env.example`, `.env`, API fixture, 원본 미디어는 읽거나 수정하지 않는다.
- push/deploy, 실 API 호출, 실 Final Cut 실행을 하지 않는다.

## 필수 동작

1. 새 job 진입점
   - `workspace`가 없는 상태에서도 staging 전에 안전하게 디렉터리를 만든다.
   - `media-grant.json`과 이미 존재하는 `cancellation.jsonl`은 바이트를 바꾸지 않고 workspace로 전달한다.
   - 기존 workspace 재개와 없는 선택 파일의 동작을 보존한다.
   - 개발/번들 모두 공유하는 `<operation> <job_id> <job_dir>` 진입점 계약을 깨지 않는다.
2. 공통 취소 관측
   - 앱이 번들 job 또는 개발 외부 job의 실행 중 취소를 기록하면 현재 Python 분석 시도가 이를 본다.
   - root/workspace 경로를 명시적으로 일치시키고, 디렉터리 생성 전/후 race에서도 취소가 유실되지 않아야 한다.
   - 기존 동일 바이트/동일 revision, wildcard attempt, 다른 revision/attempt 무시 계약을 보존한다.
3. 호출 직전 게이트
   - 분석 시작/prepare 경계뿐 아니라 각 전사 delegate 호출 직전과 각 Luna API 호출 직전에 취소를 재검사한다.
   - 취소가 확인된 뒤 추가 전사 조각/재시도나 Luna 호출이 없어야 하며 attempt와 workflow는 `cancelled`로 닫힌다.
   - 캐시와 이미 기록된 사용량은 삭제하지 않는다.
4. 호환성/안전
   - 승인 없는 삭제는 만들지 않는다. 취소/오류 구간은 그대로 보존한다.
   - 비밀을 argv, 로그, fixture에 넣지 않는다.

## 필수 회귀 시험

- Python: 실제 `macos/scripts/vlogsrt-engine.py` 모듈의 `main`을 새 임시 job/workspace에서 호출해 staging-before-workspace 결함을 방지한다. helper만 직접 호출하는 시험은 불충분하다.
- Python: 첫 전사 delegate 호출 뒤 root 쪽 취소를 기록하고 두 번째 전사 또는 이후 Luna 호출이 발생하지 않는 통합 시험을 둔다.
- Python: Luna 호출 직전 취소가 새 API 호출을 막는 시험과 다른 input revision/attempt 취소가 현재 실행을 막지 않는 기존 시험을 유지한다.
- Swift: `CancellationStore`가 앱 job/개발 job과 실행 중 엔진이 실제로 읽는 위치를 같은 payload로 연결하며, workspace가 아직 없는 경우도 안전한 시험을 둔다.
- Swift/Python 경계: 경로와 바이트 계약이 양쪽 테스트에서 같은 의미임을 못 박는다.

## 검증과 완료

- 관련 표적 테스트 뒤 최소 다음을 실행한다.
  - `pytest -q tests/project/test_workflow_cancellation.py tests/project/test_workflow_analysis.py tests/project/test_workflow_protocol.py`
  - 새 엔진 진입점 테스트 파일
  - `cd macos && swift test`
  - 변경 Python 파일/테스트에 `ruff check`, `ruff format --check`, 프로젝트 설정의 `mypy`
- 실패를 숨기거나 기대값만 느슨하게 만들지 않는다.
- 변경 파일을 명시한 로컬 commit을 만들고 push하지 않는다.
- 완료 보고에는 commit SHA, 파일 목록, 실행 명령/exit, 미실행 외부 인수를 적는다.

## Sol integration review additions (2026-09-27)
- Impact: propagating | files<=16 | modules<=5 | boundary=shared-api
- The installed development wrapper is a separate implementation in `macos/scripts/install-development-engine.sh`; cover its real installed invocation and staged media grant, not just the frozen Python entrypoint. Sharing the Python entrypoint is preferred to duplicate flows.
- `main.swift` cancellation currently resolves revision only in App Group workspace; development execution creates it in the external job. Resolve the actual running job revision and test pre-receive cancellation without treating unrelated revision requests as current cancellation.
- Check before each actual STT chunk, retry and rejected-parameter retry, not only delegate job calls. Hidden SDK retry paths must not bypass the gate. Propagate cancellation through broad exception adapters so the attempt/journal is cancelled rather than successful no-speech or ordinary failure.
- These adjacent product/app/transcriber files and their regression tests are authorized for S1A. Docs remain Sol-owned. Read required contract/brief documents; never read protected env/secrets or alter real API fixtures/media.
- The updated approved plan requires a checkpoint and idle compact boundary after each accepted task. GLM settles once with its evidence; Sol independently accepts/corrects, then the parent compacts idle Sol before the next task.
