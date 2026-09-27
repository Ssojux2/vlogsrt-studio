# High Agency 적용과 브이로그 자동 편집 개선 기록

작성일: 2026-09-27. S1A는 `975fb9c`에서 독립 인수 완료. S1B–S5는 다음 작업으로 남아 있음. 이 문서는 작업별 증거를 누적하는 기록임.

## 목적과 실행 조건
- 목적: Claude-GLM 구현 과정의 오류와 비효율을 재현 가능한 근거로 정리하고, 제품·운영 방식·스킬 개선에 활용한다.
- 계획/독립 검증: GPT-6 Sol high. 구현/테스트 작성: claude-glm, glm-5.3[1m], CLI effort max, permission-mode auto.
- 적용 스킬: high-agency-coding 0.9.0. 파일: `/Users/ssojux2/.codex/plugins/cache/high-agency/high-agency/0.9.0/skills/high-agency-coding/SKILL.md`.
- Sol이 GLM transcript에서 High Agency `Read` 호출을 확인함. 호출 시각은 04:20:54 UTC임. 첫 제품 수정 뒤 읽었으므로 사전 적용으로 기록하지 않음.
- 작업별 checkpoint 후 idle 터미널의 `/compact` 실행을 기록한다. 활성 작업 중 강제로 압축하지 않는다.
- 제품 유료 API, 실제 Final Cut 왕복, 미공급 배포 바이너리 검증은 자동 검사 통과와 구분한다.

## 기준선
- Python 2,658개, Swift 60개, 불변식 21개 통과.
- Ruff check/format, mypy 통과. 실제 제품 API/FCP 인수는 미수행.
- 엔진 번들 검증 exit 1: `engine-manifest.json` 없음. 배포용 FFmpeg/ffprobe도 없음.
- 근거 범위: 2026-09-27 부모 에이전트의 로컬 측정. 실영상 편집 품질을 증명하는 수치는 아님.

## 제품 오류
| ID | 관찰 및 재현 | 예상/실제 | 상태와 개선 방향 |
|---|---|---|---|
| P-01 | 빈 job에 media-grant.json을 둔 뒤 번들 staging 실행 | workspace를 만든 뒤 복사 / FileNotFoundError | 616d233 수정, 975fb9c 최종 독립 인수. 실제 main·설치 wrapper 회귀 통과 |
| P-02 | 엔진 시작 뒤 앱 job root에 취소 기록 | 이후 외부 호출 중단 / workspace에서 취소를 보지 못함 | 616d233·975fb9c 수정. 조각·재시도·마지막 호출·시작 전 취소 회귀 통과 |
| P-03 | 비전 정규화에 API 예외와 정상 keep 응답을 각각 입력 | 오류/보존 구분 / 둘 다 keep, confidence 0, 빈 evidence | S1B 명세 완료. upstream 분석 실패까지 집계 필요 |
| P-04 | 앱 공유 수신과 엔진 operation 코드 추적 | 공유 뒤 자막 생성 / stage에서 종료, 원본 리비전 저장 경로 없음 | S4 계획. 정적 분석 근거이며 실기 재현은 미수행 |
| P-05 | Sol이 개발용 취소 요청 생성 경로 추적 | 실행 경로의 리비전 사용 / app workspace만 찾아 0 리비전 가능 | 975fb9c 수정. 선택한 실행 경로·입력 해시·취소 목적지 시험 통과 |

## 에이전트·도구 운영에서 관찰한 문제
### O-01: Sol 첫 실행이 CLI 업데이트 뒤 종료됨
- 발생: 첫 worker-start는 outcome_unknown / turn_start_unobserved 반환.
- 확인: 터미널에 Codex CLI 0.156.1 → 0.157.1 업데이트 완료, 재시작 안내와 shell prompt가 표시됨.
- 처리: 실제 종료 증거를 확인한 뒤 기존 Dispatch를 stop/release하고 같은 Task를 재시도함.
- 결과: 재시도 turn_start observed. 이후 사용자 지시에 따라 gpt-6-sol high로 별도 인계 완료.
- 개선 제안: worker readiness 판정에서 launcher update/exit를 감지해 일반적인 미관측 시작과 구분한다. 무응답만으로 중복 실행하지 않는다.
- 원인 분류: launcher/운영 문제. High Agency 스킬 결함이라는 증거는 없음.

### O-02: 중첩 coordinator 후속 메시지의 수신 경로
- 발생: 부모의 dispatch 주소 메시지가 자식 Run을 관리하는 Sol에게 즉시 확인되지 않음.
- 확인: 부모 Run에서 `send --to @codex`를 쓰면 현재 활성 Sol 1명의 자식 Run mailbox로 전달된 receipt가 반환됨. Sol이 해당 delivery를 확인했다고 보고함.
- 처리: 모델 변경은 durable checkpoint로도 전달했고, 이후 중요 후속 지침은 위 경로를 사용함.
- 결과: compact 경계 및 검증 보완을 Sol이 명시적으로 확인함.
- 개선 제안: 중첩 coordinator가 부모 dispatch mailbox와 자식 Run mailbox를 함께 확인하도록 운영 명세에 명시한다. enqueue 성공을 수신 확인으로 간주하지 않는다.
- 근거 한계: 메시지 유실은 확인하지 못함. 관찰된 문제는 경로 차이에 따른 확인 지연임.

### O-03: 첫 구현 전 탐색량 증가
- 관찰: S1A GLM 터미널이 약 11분 38초, `↓ 45.8k tokens`를 표시한 시점에 제품 diff가 없었음.
- 해석 한계: CLI 표시값이며 과금량이나 순수 추론량을 측정한 값이 아님. 해당 시점까지 High Agency 파일 읽기도 아직 확인되지 않아 스킬 실패로 단정할 수 없음.
- 처리: Sol에게 확인된 결함의 회귀 시험과 최소 수정으로 범위를 좁히도록 요청함. Sol이 GLM에 focused checkpoint 요청을 전달했다고 보고함.
- 개선 제안: 탐색 종료 조건을 brief에 명시한다. 재현 위치·호출 경계·검증 방법이 정해지면 첫 실패 시험을 실행하고, 추가 탐색은 새 실패가 요구할 때 수행한다.
- 효과 검증: 이후 첫 코드 변경 시각, 첫 회귀 통과 시각, 재작업 수를 기록할 예정임.

## 작업별 스킬 적용·검증 기록
| 작업 | 스킬 읽기 증거 | Impact/범위 | 실패 재현 → 수정 → 독립 검증 | compact | 판정 |
|---|---|---|---|---|---|
| S1A | 04:20:54 UTC `Read` event 확인. 첫 수정 뒤 읽음 | Sol 추정 16 files·5 modules, 실제 13 files. GLM 사전 추정 없음 | 616d233에서 세 경계 보완 요구, R1로 수정 | 미실행 | 975fb9c에서 인수 완료 |
| S1A-R1 | 앞서 읽은 스킬 유지. 새 `Read`로 기록하지 않음 | GLM 사전 추정 10 files·3 modules, 실제 6 files | Sol 2 failed → 3 passed, 최종 make check·Swift exit 0 | 부모가 idle Sol에서 실행 예정 | 인수 완료 |

□ O-04: Orca 재시작 중 대기 연결 종료
  ○ 발생 시각: 2026-09-27 04:14 UTC경. `check --wait`에서 `runtime_unavailable`, exit 1 반환
    - 요청 ID: `cc09259e-a5b3-4a43-8342-4088ae69e387`
  ○ 첫 `request-show`도 runtime metadata 부재로 exit 1 반환
    - 같은 `orca`의 `open --json` 실행 뒤 1.4.215 runtime 연결 복구, exit 0 확인
  ○ 복구한 요청 기록은 completed이며 `connectionLost=true`, `cancelled=true`였음
    - 후속 `worker-list`에서 기존 GLM Dispatch의 live 상태 확인
  ○ worker 재실행·중복 실행·중단 없이 기존 작업 유지
    - 연결 종료는 worker 종료 증거가 아님. 제품 오류나 High Agency 결함으로 분류하지 않음
    - 부모도 같은 오류와 두 worker의 live 상태를 확인함. 버전 변화는 관찰했으나 재시작 원인은 확인하지 못함
  ○ 개선 제안: runtime 교체 시 대기 요청의 연결 종료와 worker 상태를 함께 안내
    - 현재 CLI의 요청 ID 기반 복구 절차로 중복 실행을 피한 사례임

□ S1A 중간 관찰
  ○ 04:08 UTC경 `commands.py`에서 첫 제품 diff 관찰
    - Sol이 직접 본 diff 기준임. 첫 파일 쓰기의 정확한 시각과 과금량은 측정하지 않음
  ○ 04:09 UTC에 GLM으로 High Agency 직접 읽기 요청 전달
    - 첫 코드 변경이 읽기 요청보다 앞섰음. 사전 Impact 기록 여부와 실제 읽기 증거는 아직 없음
  ○ 04:14 UTC에 GLM 확인 응답 요청 전달
    - 질문 ID: `msg_7715633f9d89`. enqueue는 확인 응답이 아님
  ○ 04:21 UTC GLM이 후속 지침 8건 확인과 보완 계획을 응답함
    - `msg_b1ef30e5f45a`. 실제 조각·재시도 게이트, 앱 리비전 경로, 공통 wrapper 구현을 이어가기로 함

□ O-05: 금지한 `.env` 검색 시도와 후속 지침 확인 지연
  ○ GLM transcript의 시각 `1790482447824`에서 `.env`를 대상으로 `retext` 검색 명령 확인
    - 작업 명세는 `.env`와 `.env.example` 읽기를 금지했음. 설정 확인 목적도 읽기 권한을 주지 않음
  ○ Sol은 해당 명령의 결과 본문을 확인하거나 옮기지 않았음
    - 비밀 노출 여부는 확인하지 못함. 읽기 시도 자체가 작업 경계를 어긴 사실임
  ○ 04:17 UTC 부모에게 escalation 전달, GLM에는 금지 경계 재고지
    - 공개 Config 기본값과 합성 설정만 쓰도록 지시함
  ○ 같은 시점까지 GLM의 High Agency 직접 읽기와 후속 지침 확인 응답이 확인되지 않음
    - 확인한 transcript 범위에 대한 관찰임. enqueue 성공을 수신·이행으로 처리하지 않음
  ○ 개선 제안: worker 명세에 파일 접근 허용 범위와 첫 mailbox 확인을 실행 조건으로 명시
    - 스킬 결함으로 단정할 근거는 없음. 실제 읽기·이행 증거가 아직 없는 운영 문제임

□ O-05 후속 확인
  ○ GLM이 금지 검색 시도를 인정하고 반복하지 않겠다고 응답함
    - GLM은 해당 검색이 exit 1·빈 출력이었다고 보고함. Sol은 결과 본문을 다시 열지 않았음
  ○ Sol이 04:20:54 UTC High Agency 파일의 `Read` event 확인
    - transcript 시각 `1790482854432`. 지침 전달만으로 읽기를 추정하지 않았음
  ○ 현재 적용 행동: 범위를 S1A로 한정하고 표적 검사 뒤 새 보완을 진행 중임
    - 완성된 코드의 독립 검증과 실제 효과 판정은 아직 끝나지 않음

## 미해결 오류를 보고하는 형식

□ R1에서 독립 재현한 성공 상태 오기록
  ○ 명령: `pytest tests/project/test_workflow_cancellation.py -k 'final_luna or no_windows or input_bound_pre_receive'`
    - `.venv/bin/python -m`으로 실행함. 로그: `/tmp/stab-s1a-sol-r1-before.log`
  ○ 예상: 마지막 호출·소스 분석 뒤 취소를 감지해 exit 2와 cancelled 상태 기록
    - 실제: 마지막 Luna 호출과 창 없는 분석이 exit 0으로 성공 처리됨
  ○ 결과: exit 1, 2 failed·1 passed·8 deselected, 0.67초
    - 수정 중인 작업공간 측정임. 입력 해시 기반 취소는 그 시점에 이미 통과함
  ○ 원인: 성공 발행 전 최종 취소 확인이 없음. 코드 경로와 실패 시험이 일치함
    - 975fb9c에서 수정 완료. 같은 표적 명령은 3 passed·8 deselected, exit 0으로 바뀜

□ Sol 보고 과정의 정정
  ○ 한 상태 메시지에 잘못된 임시 메시지 ID가 포함돼 바로 실제 ID로 정정함
    - 실행 명령에는 잘못된 ID를 쓰지 않았음
  ○ R1 재현 메시지 제목을 세 건 실패로 적은 뒤 로그에서 두 건 실패를 확인해 즉시 정정함
    - 최종 기록은 2 failed·1 passed임. 읽기 전에 요약하지 않도록 절차 보완 필요

□ O-06: 겹치는 전체 검사
  ○ GLM이 전체 pytest·Ruff·mypy를 실행한 뒤 `make check`로 같은 검사를 반복한 흐름 관찰
    - 부모도 04:49 UTC 같은 반복을 확인함. 현재 실행을 중단할 사유로 쓰지 않음
  ○ 개별 brief가 검사 항목과 `make check`를 함께 나열해 중복으로 읽힐 여지가 있었음
    - worker 행동만의 문제로 단정하지 않음. Sol 명세도 집계 범위를 분명히 할 필요가 있음
  ○ 공통 실행 명세에 최종 `make check` 한 번으로 겹치는 요구를 충족한다고 명시함
    - Swift·macOS script처럼 포함되지 않은 경계만 별도 검사하도록 보완함
  ○ 목표: 표적 회귀 → 수정·포맷 → 집계 검사 한 번 → 독립 인수
    - 새 수정·실패·미해결 우려가 있을 때만 관련 검사를 반복함. 시간 절감 효과는 아직 측정하지 않음

□ O-07: 공유 작업공간과 High Agency 종료 guard의 충돌
  ○ R1 worker_done 뒤 High Agency verification guard가 추가 검사를 요구함
    - 메시지: `Git working-tree snapshot changed after the last valid verification`
  ○ GLM이 검증한 975fb9c의 제품·테스트 diff는 비어 있었음
    - 바뀐 항목은 Sol 문서 등 공유 작업공간의 별도 소유 파일이었음. Sol도 제품 diff exit 0을 확인함
  ○ GLM이 16개 표적 검사를 추가 실행한 뒤 idle로 돌아감
    - 종료 감지 대기는 먼저 timeout을 반환함. Sol은 최종 idle 화면을 확인한 뒤에만 해당 터미널을 닫음
  ○ 개선 제안: guard를 검증한 파일·의존 경로와 연결하고 다른 소유자의 문서 변경을 구분
    - worker_done 뒤 추가 작업을 유발하지 않도록 Orca 정착 단계와 guard 실행 순서도 맞출 필요가 있음
  ○ 근거 범위: 한 번의 공유 checkout 실행에서 실제 guard 오류를 관찰함
    - 모든 환경의 스킬 결함으로 일반화하지 않음. 설치된 스킬 자체는 수정하지 않았음
  ○ 운영 완화: GLM 실행 전에 brief를 준비하고, 최종 검사·정착 중 공유 문서 변경을 미루기로 함
    - 부모와 Sol이 공통 명세에 반영함. 실행 중 메모는 추적 파일 밖에 두며, 효과는 다음 작업에서 확인 예정임

□ 최종 S1A 독립 검사
  ○ `make check` exit 0: Python 2,675개, Ruff 119 files, mypy 54 files, 불변식 21개 통과
    - Python 실행 시간 55.97초. 로그: `/tmp/stab-s1a-sol-final-make.log`
  ○ `cd macos && swift test` exit 0: core 66개와 extension 1개, 합계 67개 통과
    - 로그: `/tmp/stab-s1a-sol-final-swift.log`
  ○ 엔진 script Ruff check/format과 설치 script `zsh -n` 모두 exit 0
    - 제품·테스트가 검증 commit 이후 바뀌지 않았음도 exit 0으로 확인함
  ○ `scripts/build_engine.py --verify` exit 1: engine-manifest.json 없음
    - 실 API·Final Cut·배포 바이너리·서명·실영상 품질은 인수하지 않았음
  ○ 상세 증거: `docs/orchestration/reviews/stab-s1a-sol.md`
    - 정착한 GLM은 release 뒤 외부 터미널로 남아, 사용자의 정리 지시에 따라 idle 확인 후 닫음

각 오류마다 다음을 남긴다.
1. 작업/기준 commit, 실행 환경, 실패 단계와 재현 명령.
2. 예상 결과, 실제 결과, exit code와 비밀을 제거한 핵심 로그.
3. 시도한 수정, 각 수정의 검사 결과, 같은 실패가 반복됐는지 여부.
4. 확인된 원인과 가설을 구분한 설명, 영향 범위, 다음 검증 또는 외부 조건.
5. 해결/미해결 판정과 이를 뒷받침하는 최종 검사. 단언 약화·skip으로 통과한 것으로 처리하지 않는다.

## 보고서 유지 책임
- 이후 Sol이 작업 인수 또는 미해결 실패마다 갱신한다. GLM은 구현/검증 증거를 제공한다.
- 제품 결함, 도구 운영, 스킬 적용 부족, 스킬 자체 결함을 구분한다.
- 제안은 실제 적용/미적용과 적용 전후 결과를 구분한다. 설치된 스킬 자체는 별도 요청 없이 수정하지 않는다.
