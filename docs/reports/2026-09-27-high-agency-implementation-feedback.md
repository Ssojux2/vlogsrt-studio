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

## S1B 인수 및 피드백 — 2026-09-27

□ 인수 결과
  ○ `cd80d61` 구현 뒤 `a9db170` 보완까지 Sol이 독립 인수함
    - GLM이 제품·테스트를 작성함. Sol은 명세·문서·독립 검사만 담당함
  ○ `make check` exit 0: Python 2,697개, Ruff 120 files, mypy 54 files, 불변식 21개 통과
    - Python 51.97초. `/tmp/stab-s1b-sol-final-make.log`에 기록함
  ○ 최종 `swift test` exit 0: core 71개와 extension 1개, 합계 72개 통과
    - `/tmp/stab-s1b-sol-final-swift.log`에 기록함
  ○ R1은 Swift 다섯 파일만 변경함. Python·테스트·엔진 script는 cd80d61과 같음
    - commit 비교 exit 0으로 확인함. 겹치는 전체 Python 검사는 반복하지 않음
  ○ 상세 근거: `docs/orchestration/reviews/stab-s1b-sol.md`
    - S2–S5와 실 API·Final Cut·배포 인수는 남아 있음

□ S1B-01: 스킬 적용 시점과 범위
  ○ 첫 코드 변경 전에 High Agency의 실제 Read를 확인함
    - 시각 `1790485689601`. S1A의 늦은 읽기와 달리 사전 읽기를 이행함
  ○ 첫 예상은 파일 18개 이하였음. 실제 변경도 18개였음
    - R1은 6개 이하 예상에 5개 변경으로 끝남
  ○ R1은 스킬 앞 60줄을 직접 다시 읽고 앞선 전체 문맥을 유지함
    - 시각 `1790489499657`. 전체 재읽기로 과장하지 않음
  ○ R1은 회귀를 먼저 쓰고 Sol의 독립 실패 재현 뒤 제품 코드를 변경함
    - 결과·범위·증거 원칙의 실제 적용을 확인함
  ○ 첫 탐색이 13분을 넘겨 Sol이 다음 단위를 회귀·수정으로 제한함
    - 당시 terminal 표시 약 46.6k tokens는 관찰값임. 청구량이나 스킬 결함의 증거는 아님

□ S1B-02: FIFO 재전달로 후속 지시가 가려짐
  ○ GLM이 heartbeat 요약을 Delivery 확인 처리로 잘못 해석함
    - `check --ack`를 생략해 같은 네 메시지를 다시 읽음
  ○ 뒤에 보낸 아홉 수정 지시가 다음 Delivery로 넘어가지 못함
    - transcript의 재전달 설명과 실제 확인 처리 영수증이 원인을 뒷받침함
  ○ Sol이 작업을 끊지 않는 terminal 안내 한 번으로 확인 처리 절차를 알림
    - 기존 Task·Dispatch를 유지함. 중복 worker나 재시작은 없었음
  ○ 영수증에서 `delivery_95345f707d3e` 확인 처리와 다음 아홉 메시지 전달을 확인함
    - GLM이 05:43:48 UTC 수정 지시를 명시적으로 수락함
  ○ 공통 명세에 전체 본문 읽기 → 처리·답변 → 정확한 deliveryId 확인 처리를 추가함
    - worker preamble에도 같은 순서를 안내하도록 개선 제안함. 설치된 도구는 수정하지 않음

□ S1B-03: 본문 자르기와 검사 명령의 문제
  ○ GLM이 메시지 본문을 400·500자로 잘라 읽는 명령을 사용함
    - 뒤쪽의 결과 연결 요구가 빠질 수 있었음. Sol이 짧은 지시로 다시 전달함
  ○ 부모가 `pytest | grep | head` 형태의 종료코드 문제를 지적함
    - grep 미일치나 SIGPIPE가 원래 검사 상태와 섞일 수 있음
  ○ 최종 근거는 원래 검사 명령의 종료코드를 곧바로 보존하도록 보완함
    - 단순한 pipeline 상태를 제품 실패로 분류하지 않음
  ○ Sol의 독립 검사는 출력을 파일로 보내고 원래 exit를 확인함
    - 이 지적만으로 이미 유효한 전체 검사를 반복하지 않음

□ S1B-04: 실패 근거의 구분과 공유 코드 교체
  ○ 첫 독립 entrypoint 검사는 새 AnalysisResult import 부재로 collection에서 실패함
    - exit 2임. 실제 거짓 성공을 실행한 근거로 쓰지 않음
  ○ GLM이 초기 기준선 재현에 제품·테스트 경로만 지정한 stash 두 번을 사용했다고 보고함
    - 보고한 경로는 `.env.example`를 제외함. 실제 stash 명령을 Sol이 다시 확보하지는 못함
  ○ 공유 코드가 잠시 바뀌었으므로 당시 draft 검사를 최종 인수 근거에서 제외함
    - 보호 파일의 내용 노출이나 변경은 관찰하지 않음
  ○ GLM은 이후 `git archive`로 체크아웃 밖에 기준선을 만들었다고 보고함
    - 전체 상류 실패의 exit 0을 assertion으로 재현한 worker 근거로 분류함
  ○ 최종 인수는 고정 commit 검사와 Sol의 독립 R1 재현을 사용함
    - 앞으로 기준선 비교는 명시한 파일을 격리해 수행하도록 명세에 추가함

□ S1B-05: helper 통과 뒤에도 남은 UI 우회
  ○ cd80d61은 낡은 결과를 unknown으로 읽었으나 후보 helper는 unknown을 허용함
    - extension도 unknown에서 엔진 요청으로 이어져 결과 연결 검사를 우회함
  ○ 재현: `swift test --filter unboundHistoryMustNotEnableCandidateReviewOrImplicitRerun`
    - Sol 실행 exit 1, 한 테스트에서 11개 행동 단언 실패, 0.018초
  ○ 예상은 검토·재실행 차단이었음. 실제로는 낡은 후보가 검토를 켰음
    - `/tmp/stab-s1b-sol-r1-red.log`. 제품 코드가 cd80d61과 같음을 root에서 확인함
  ○ a9db170은 fresh·valid·unknown 라우팅을 실제 extension에 연결함
    - unknown은 후보를 지우고 정지함. 최종 Swift 전체 검사에서 같은 회귀가 통과함
  ○ 원인은 reader와 소비자의 정책 불일치로 확인됨
    - helper 반환값뿐 아니라 실제 소비 흐름까지 시험하도록 인수 기준을 보완함

□ S1B-06: 시작 관측의 잘못된 음성 판정
  ○ R1 worker-start가 exit 1과 `turn_start_unobserved`를 반환함
    - Dispatch는 `ctx_9cdf4f52aa21`, 요청은 `1ec7ac6b-90c1-42a1-9597-0d12e5b74dfe`임
  ○ 같은 terminal에서 새 preamble·도구 실행·Task에 묶인 ask를 확인함
    - receipt의 시작 관측 실패와 실제 실행이 어긋남. 프로세스 종료로 해석하지 않음
  ○ Sol은 같은 Dispatch를 유지하고 중복 시작을 하지 않음
    - worker_done 뒤 completed/succeeded 상태를 직접 확인함
  ○ 검증된 ask·heartbeat가 시작 대기 표시에 반영되도록 개선 제안함
    - 단순 생존 신호를 완료나 새 권한으로 간주하자는 제안은 아님

□ S1B-07: 공유 snapshot 완화와 정리
  ○ 최종 검사·정착 중 공유 문서 변경을 미뤘음
    - 이전 S1A의 문서 변경에 따른 종료 guard 재검사 문제는 이번에 관찰하지 않음
  ○ 독립 인수 뒤 release는 외부 terminal 유지 상태를 반환함
    - 정확한 terminal·incarnation과 idle을 확인한 뒤 사용자 정리 지시에 따라 닫음
  ○ close 영수증의 ptyKilled가 true였음. 회수 대기 worker 행은 없었음
    - 기존 부모·Sol terminal과 다른 사용자 세션은 유지함
  ○ 현재 S1B 제품 오류 중 미해결로 남긴 항목은 없음
    - 실 API·실영상·Final Cut·바이너리·서명 인수는 수행하지 않은 별도 항목임

□ S1B-08: 남은 번들 검사 실패와 외부 인수
  ○ 단계: 배포 엔진 검사. 재현: `.venv/bin/python scripts/build_engine.py --verify`
    - S1A 독립 실행 exit 1. `/tmp/stab-s1a-sol-final-bundle.log`를 S1B에서 다시 읽음
  ○ 예상은 manifest와 바이너리 검증 통과였음. 실제 오류는 `manifest가 없다`였음
    - 대상: `macos/Resources/Engine/engine-manifest.json`
  ○ manifest 부재가 직접 원인임. 오류 출력과 파일 경로가 일치해 근거가 강함
    - S1B는 패키징을 바꾸지 않았음. S5에서 빌드 경로를 완성하도록 범위가 정해져 있음
  ○ GLM 코드 수정과 Python·Swift 검사로 이 배포 오류를 해결했다고 주장하지 않음
    - 엔진·FFmpeg 공급, 서명·공증, 실제 실행 검증이 남아 있음
  ○ 실 API·Final Cut 왕복·실영상 품질 목표도 미수행임
    - 나머지 오프라인 S2–S5 작업을 멈출 근거로 쓰지 않음

□ S1B-09: Sol의 마지막 검사 범위 실수
  ○ Sol이 문서 저장 뒤 경로를 지정하지 않은 `git diff --check`를 실행함
    - 수정 상태였던 사용자 `.env.example`도 공백 검사 대상에 포함됨
  ○ exit 0이었고 내용은 출력하지 않았음. 파일 변경도 하지 않았음
    - 파일 내용을 읽지 말라는 경계를 명령 범위에서 지키지 못한 사례로 기록함
  ○ 이후 문서 경로를 명시한 검사만 수행함
    - 파일 내용을 다시 확인하지 않음. 제품 인수 결과에는 영향이 없음

## S2A-1 — 실행 소유권 구현과 독립 인수

□ S2A-1-01: 범위와 스킬 적용
  ○ Sol이 S2A를 실행 소유권·자격증명/사전 검사·진행 갱신으로 나눔
    - 이번 실행은 소유권만 다룸. 나머지 요구는 각각의 brief에 남김
  ○ GLM의 High Agency 직접 Read를 코드 변경 전에 확인함
    - 최초 timestamp1790490883899. 첫 Impact는 files<=8/modules<=4/shared-api임
  ○ 실제 변경은 11개 파일임. 첫 추정은 고치지 않음
    - 취소 시험·설치 래퍼·XcodeGen 산출물 추가를 Sol과 부모가 명시적으로 승인함
  ○ R1은 시험 파일 한 개만 바꿈
    - Read는 첫60줄이며 앞선 전체 문맥을 이어받음. local/files<=1 추정과 일치함
  ○ 초기 조사에 약10분이 걸림
    - 화면의 토큰 수는 비용 근거가 아님. 지연을 스킬 자체 결함으로 단정하지 않음

□ S2A-1-02: 실제 경계의 실패 재현
  ○ Sol이 변경 없는 a9db170 제품 경로에서 중복 실행을 직접 재현함
    - 실제 Python 진입점의 별칭 중복이 실행에 들어가 30초 뒤 timeout으로 실패함
  ○ Swift의 기존 runner도 중복 상태3을 실패로 분류하고 sync를 실행함
    - 독립 검사 exit1. 한 시험에서 행동 단언 두 개가 실패함
  ○ Task에 묶인 ask에서 GLM을 멈춘 뒤 Sol이 재현 결과로 구현을 승인함
    - msg_ed39a8557300 → msg_ff4c44460798. import·컴파일 오류를 RED로 쓰지 않음
  ○ 최종 Python은 정준 경로의 OS 잠금을 먼저 얻음
    - 중복·별칭·다른 job·실패·실제 취소·SIGKILL·서술자 상속을 검증함

□ S2A-1-03: 초안의 잠금·앱 오류 수정
  ○ 모든 flock OSError를 중복으로 처리하던 초안을 반려함
    - EAGAIN/EWOULDBLOCK만 중복임. 나머지는 실패로 나가고 서술자를 닫음
  ○ unlock 오류가 나도 finally에서 서술자를 닫도록 수정함
    - 실패한 획득 뒤 fstat의 EBADF와 실제 재획득을 시험함
  ○ 중복 상태3보다 드리프트 기록이 먼저여서 정상 실행을 취소할 수 있었음
    - 상태3을 실행 반환 직후 처리함. 취소·드리프트가 겹쳐도 쓰기·sync·import 없음
  ○ 한 job의 종료가 다른 job의 취소 문맥을 지우던 단일 슬롯을 수정함
    - 실제 main.swift가 job별 EngineJobBoard를 사용함. 두 종료 순서를 시험함
  ○ 반환값으로만 흉내 낸 취소 시험을 보강함
    - 실제 Task.cancel·던지는 실행기·동시에 도는 다른 job으로 해제와 재획득을 검증함

□ S2A-1-04: 개발 실행기의 상태 전달
  ○ 가정한 NSUserUnixTask 오류 domain이 실제 실행과 달랐음
    - 합성 실행기 관측으로 드러남. 영문 오류 문면 파싱도 인수하지 않음
  ○ 상태 전달을 호출마다 새 임시 파일에 묶음
    - token·경로·크기를 검사함. 최대257바이트만 읽음. job 밖에만 기록함
  ○ 배타 생성으로 기록을 게시함
    - 검증 뒤 생긴 파일도 덮어쓰지 않음. 잘못된 인자는 기록·workspace를 만들지 않음
  ○ 넷째 인자가 있는 전달 모드는 기록 완료 뒤에만 process exit0을 냄
    - 안쪽 상태는 기록에 있음. 3인자 직접 호출의 기존0/1/2/3 계약은 유지함
  ○ 실행기가 오류·신호 종료를 보고하면 온전한 기록도 받아들이지 않음
    - 실제 합성 실행기로 큰 출력·기록 불일치·비0 종료·SIGKILL을 시험함

□ S2A-1-05: 시험 근거 보완과 실제 빌드
  ○ 누적 호출 수가 이전 실행에 맞아 거짓 통과할 수 있던 대기를 고침
    - 실행별 시작 수와 경로로 도착을 확인함. 실패 표식을 release보다 먼저 씀
  ○ 유효하지 않은 취소 기록과 일반 실패로 취소 해제를 주장하던 시험을 바꿈
    - 기존 오프라인 진입점으로 실제 취소된 attempt와 재획득을 검증함
  ○ 부모가 새 Swift 파일의 Xcode 대상 누락을 발견함
    - Sol 초기 brief의 생성 프로젝트 항목 누락도 기록함. XcodeGen으로만 고침
  ○ Sol의 새 derived-data 경로에서 unsigned Debug 앱·extension 빌드가 통과함
    - exit0, BUILD SUCCEEDED. SwiftPM 결과와 구분함
  ○ R1은 손으로 만든 래퍼 대신 실제 설치 스크립트의 출력을 사용함
    - 공백 경로에서 launcher → 설치 래퍼 → Python의0/1/잡힌 잠금3을 독립 검증함

□ S2A-1-06: 명령·수신·시작 관측 문제
  ○ GLM의 첫 pytest 명령은 tail 뒤의 exit를 출력함
    - 그 값을 시험 exit로 쓰지 않음. 직접 리다이렉션과 원래 exit로 다시 검증함
  ○ 일부 ack 결과를 head로 잘라 다음 batch를 놓칠 위험이 있었음
    - 전체 결과 처리·정확한 deliveryId ack·뒤 batch 반복을 다시 요구함
  ○ 부모도 처리한 delivery_2252b5c1233c의 ack를 놓쳤다고 보고함
    - replay를 보고 ack함. 뒤 보고가 늦었으나 중복 Dispatch·제품 변경은 없었음
  ○ R1 시작 영수증은 turn_start_unobserved였으나 새 preamble·Read가 실행됨
    - ctx_f0ea0d922f8f를 유지함. 중복 시작 없이 completed/succeeded로 정착함
  ○ 늦게 도착한 실제 설치 래퍼 시험 요청은 최초 정착을 넘김
    - 정착한 ID를 재사용하지 않고 새 R1 Task/Dispatch로 처리함

□ S2A-1-07: dotenv 격리 경계의 공개
  ○ 공개 코드에서 cli.main의 ensure_loaded 호출을 확인함
    - cwd 조상과 PACKAGE_ROOT의 dotenv를 찾음. offline 설정은 탐색을 막지 않음
  ○ 기존 conftest는 네트워크를 막으나 실제 dotenv 탐색은 격리하지 않음
    - 이전 make·bridge 검사가 이 경로를 실행함. 파일이 있었다면 읽을 수 있었음
  ○ 실제 보호 파일의 존재·내용은 확인하지 않음
    - 노출을 단정하지 않음. 반환된 출력에서 키 내용을 관찰하지 않았으며 변경도 관찰하지 않음
  ○ GLM은 R1의 실제 저장소 대상 실행 한 번을 공개함
    - 관측 시각07:34:33.821 UTC. 보고한 16:57은 부정확하여 시각 근거로 쓰지 않음
  ○ 이 문제는 Sol의 시험 격리 검토 누락도 포함함
    - 보호 파일을 직접 열지 않았다는 사실만으로 간접 접근 회피를 주장하지 않음

□ S2A-1-08: 격리 수정과 남은 시험 작업
  ○ 최종 Swift bridge는 공개 Python 소스와 진입점만 임시 저장소로 복사함
    - 실제 dotenv·자격증명·미디어·API fixture는 복사하지 않음
  ○ 시험이 직접 만든 offline dotenv로 조상 탐색을 멈춤
    - PACKAGE_ROOT도 임시 복사본 안에 있음. 진입점 바이트가 원본 코드와 같음을 확인함
  ○ 해석기 symlink는 venv 접두어를 잃어 rich import에 실패함
    - 실제 해석기를 부르는 exec shim으로 수정함. 최종 독립 Swift83 통과함
  ○ 기존 Python 전체 시험의 격리는 아직 별도 작업임
    - 다음 broad suite 전에 stab-test-isolation.md의 시험 전용 작업을 수행해야 함
  ○ 제품 기본 설정을 바꾸거나 격리되지 않은 전체 검사를 다시 돌리지 않음
    - 원인 근거는 공개 코드이며 보호 파일 내용을 읽어 확인하지 않음

□ S2A-1-09: 최종 근거·정리·외부 인수
  ○ Sol make check는2715 Python·Ruff121·mypy54·불변 규칙21 통과함
    - 7d3fe93 기준 exit0. 이후 해당 경로가 같음을 diff exit0으로 확인함
  ○ 최종853c031의 Swift는82 core+1 extension 통과함
    - 실제 설치 래퍼의 격리 시험 포함. unsigned native 빌드도 독립 exit0임
  ○ GLM의 정확한 terminal·incarnation·idle을 확인하고 정리함
    - release는 external 유지였음. 사용자 정리 권한으로 close, ptyKilled=true 확인함
  ○ 이전 S1A 번들 검증 오류는 아직 별개임
    - build_engine.py --verify exit1, engine-manifest.json 없음. S2A-1에서 재검사하지 않음
  ○ 바이너리 공급·서명/공증·실 API·실영상/FCP 인수는 미수행임
    - S2A-2부터 S5까지와 시험 격리 작업이 남음. 스킬 설치 파일은 바꾸지 않음

근거: [S2A-1 독립 인수 보고](../orchestration/reviews/stab-s2a-1-sol.md), [실행 소유권 계약](../EXECUTION_OWNERSHIP.md), [다음 시험 격리 작업](../orchestration/tasks/stab-test-isolation.md).

## STAB-TEST-ISOLATION: 시험 격리 인수와 절차 위반

□ TI-01: 최종 인수 근거
  ○ 시험 전용 코드69790b4와01f8616을 인수함
    - 여섯 시험 파일만 변경함. 제품·Swift·설치 스크립트·기본 설정은 그대로임
  ○ Sol의 최종 표적 검사는67개 통과함
    - 원래 exit0, 5.62초. `/tmp/stab-isolation-sol-final-targeted.log`
  ○ Sol의 guarded make check는2,735개 통과함
    - 원래 exit0, 60.08초. Ruff122파일·mypy54파일·불변 규칙21개도 통과함
  ○ 같은 경로의 broad 검사를 GLM이 중복 실행하지 않음
    - 이전 Swift83·unsigned native 빌드·스크립트 검사는 변경 없음 근거로 재사용함
  ○ S2A-2는 아직 시작하지 않음
    - 현재 단위는 오프라인 시험 격리임. 실영상·실 API·FCP 인수와 구분함

□ TI-02: 금지된 첫 RED 선택과 보고 한계
  ○ GLM이 보호 설치 전에 tests/test_env.py 전체를 RED로 실행함
    - 원래 exit1·실패7개 보고임. 합성 사례만 선택하라는 brief를 위반함
  ○ 실제 저장소의 dotenv 탐색 경로가 포함됨
    - find_env_file·api_key_source와 ensure_loaded를 거치는 호출이 선택됨
  ○ 실제 보호 파일의 존재·내용·읽기 여부는 조사하지 않음
    - Sol은 raw RED log를 열지 않음. 부모가 사용자에게 경계 위반을 알렸음
  ○ 시각 근거는 log의 birth/mtime17:09:56 KST뿐임
    - 명령 실행 시각으로 바꾸어 보고하지 않음. 과거 접근 없음도 주장하지 않음
  ○ scratch 보고의 tmp-only load_env 주장은 Sol이 반려함
    - ensure_loaded의 간접 읽기는 배제할 수 없음. 정착 뒤 송신 거부로 최종 Sol 보고에 바로잡음

□ TI-03: 보호 초안의 오류와 수정
  ○ 차단된 os.path probe가 True를 반환하던 조건식을 수정함
    - 합성 파일의 존재 probe가 모두 False인지 시험함
  ○ stat 차단 예외 때문에 pytest 수집이 exit4로 멈춤
    - 보호 대상 stat은 실행하지 않고 FileNotFoundError로 일정하게 숨기도록 고침
  ○ 소스에 있던 고정 canary를 실행별 값으로 바꿈
    - 초안 소스 검토에서 고정 문면이 보였음. 실 키가 아니며 최종 증명에는 값이 없음
  ○ pathlib 읽기를 builtins.open으로 막았다는 잘못된 주장을 수정함
    - 실제 io.open 경로와 os.open·read_bytes·Path.open을 자식 안에서 각각 시험함
  ○ 제품 import보다 늦던 conftest 보호 설치를 앞으로 옮김
    - 보호를 켠 뒤 Config·Segment를 import함. 필요한 줄에만 E402 예외를 둠

□ TI-04: 자식 실행 전에 실패를 막는 검증
  ○ 실행 뒤 proof 부재만 검사하는 초안을 인수하지 않음
    - 보호 없는 payload 실행을 되돌릴 수 없으므로 실행 전 게이트를 추가함
  ○ 오류 기록 실패가 종료를 우회할 수 있던 경로를 수정함
    - proof 디렉터리가 없어도 exit125임. payload 호출 기록과 job 변경이 없음
  ○ sitecustomize 부재·문법 오류를 실제 launch helper로 시험함
    - Python이 시작 오류를 무시해도 사전 증명이 없으면 payload를 띄우지 않음
  ○ 설치 래퍼의 첫 실행과 재개를 같은 게이트로 묶음
    - 설치 래퍼의 부재·문법 오류 시험에서도 job 트리가 그대로임
  ○ 합성 패키지가 사라진 editable fallback을 직접 검증함
    - 보호가 켜진 자식의 공개 패키지 import 출처를 기록하고 실행 전에 거부함

□ TI-05: 격리 범위와 남은 한계
  ○ 공개 Python 소스와 바이트 동일 엔진만 합성 저장소로 복사함
    - source symlink를 거부함. 실제 dotenv·원본 미디어·실 API fixture를 복사하지 않음
  ○ 정상 자식은 합성 cwd·패키지를 사용함
    - Sol이 proof의 경로와 읽기·socket 거부 boolean을 독립 확인함
  ○ 보호는 협조적인 Python 시험 harness 범위임
    - 디렉터리 이름 나열·기존 fd·os.access/readlink·다른 이름의 alias는 포괄 차단하지 않음
  ○ 자식 Path.resolve와 native 소비자는 별도 한계임
    - zsh는 합성 입력으로 격리함. 사전 검사 뒤 악의적인 파일 교체도 보장 범위 밖임
  ○ 새 dotenv CLI subprocess는 같은 게이트와 출처 증명이 필요함
    - 기존에 없던 native·alias 경로가 필요하면 별도 시험 범위를 먼저 정함

□ TI-06: 수신·CLI 운영 오류
  ○ GLM이 ack 결과를 head -8로 잘라 다음 Delivery를 놓침
    - delivery_7d329b6fc536 전체 재처리·ack를 요구함. 후속 증거에 ack를 기록함
  ○ 이번에도 전체 batch 처리 규칙을 반복해서 요구함
    - heartbeat와 ack를 혼동하지 않음. 경고 전달만으로 처리 완료를 주장하지 않음
  ○ Sol의 run-show --run이 exit1로 거부됨
    - 명령이 돌려준 지원 flag인 --id로 수정함. 상태 조회만 실패했음
  ○ Sol의 중복 actionable wait가 waiter_exists로 거부됨
    - 기존 waiter를 유지함. 새 작업이나 중복 writer를 만들지 않음
  ○ 마지막 scratch 문구 수정 송신은 dispatch_inactive로 거부됨
    - 이미 완료된 ctx_f95bd6962daf를 다시 쓰지 않고 Sol의 최종 문서에서 바로잡음

□ TI-07: 스킬 적용과 재발 방지
  ○ GLM의 High Agency 직접 Read를 코드 수정 전에 관찰함
    - 첫 Impact는 propagating·files≤6·modules≤2·private이며 실제 여섯 시험 파일과 일치함
  ○ 직접 Read만으로 지침 준수가 보장되지는 않았음
    - 합성 RED 선택과 전체 mailbox 처리에서 위반이 관찰됨. 스킬 자체 결함으로 단정하지 않음
  ○ 표적 증명 뒤 Sol 승인으로 broad 검사를 여는 게이트가 작동함
    - 첫 후보64개 통과 뒤에도 실제 래퍼와 fallback 검증을 보완하여67개로 인수함
  ○ 다음 RED는 실제 경로가 없는 합성 harness로 제한함
    - 전체 모듈에서 보호를 끄는 방식은 금지함. 공통 실행 계약에 반영함
  ○ 설치된 스킬·hook 파일은 수정하지 않음
    - shared docs는 GLM 정착·idle·close 뒤에만 최종 수정함

□ TI-08: 최신 사용자 모델 선택과 관측 한계
  ○ 쉬운 작업은 Luna max, 복잡한 계획·독립 인수는 Sol high로 지정됨
    - 큰 구현은 GLM임. 다음 Task 시작 때 난도에 맞는 effort를 정하도록 AGENTS를 갱신함
  ○ 진행 중인 isolation GLM은 요청된 max 설정을 유지함
    - 모델 변경만을 위해 작업을 끊거나 관리자를 추가하지 않음
  ○ 부모는 Orca의 Luna max 시작 거부를 보고함
    - invalid_argument였음. 직접 Codex 실행 화면의 Luna max와 readiness 확인 뒤 adoption함
  ○ 이 차이는 Orca와 Codex의 모델 capability 검증 불일치로 기록함
    - downgrade는 없었음. High Agency가 원인이라는 증거는 없음
  ○ 부모가 idle 모델 선택과 standalone compact를 맡음
    - client 선택 증거만으로 내부 compaction 모델까지 Luna라고 주장하지 않음

□ TI-09: 정착과 다음 경계
  ○ GLM done msg_e66425299357을 completed/succeeded로 확인함
    - ctx_f95bd6962daf와 정확한 terminal/incarnation을 검증함
  ○ idle 뒤 external release와 명시적 close를 수행함
    - ptyKilled=true임. reclaimable worker 조회 결과는 비어 있음
  ○ 바이너리·서명/공증·실 API·실영상/FCP 인수는 그대로 남음
    - 기존 manifest 부재 exit1은 이번 단위에서 재검사하지 않음
  ○ 다음 구현은 S2A-2이며 현재 Dispatch에서 시작하지 않음
    - S2A-3·S2B/C·S3–S5도 계속 남음. 전체 계획 완료로 보고하지 않음

근거: [시험 격리 독립 인수 보고](../orchestration/reviews/stab-test-isolation-sol.md), [시험 격리 brief](../orchestration/tasks/stab-test-isolation.md), [공통 실행 계약](../orchestration/tasks/stab-execution-contract.md).

## 보고서 유지 책임
- 이후 Sol이 작업 인수 또는 미해결 실패마다 갱신한다. GLM은 구현/검증 증거를 제공한다.
- 제품 결함, 도구 운영, 스킬 적용 부족, 스킬 자체 결함을 구분한다.
- 제안은 실제 적용/미적용과 적용 전후 결과를 구분한다. 설치된 스킬 자체는 별도 요청 없이 수정하지 않는다.

## STAB-S2A-2 unit1 first attempt — failed recovery boundary (2026-09-27)

- Outcome: no accepted product commit; `ctx_80c55eec0ac2` settled failed and exact idle GLM terminal closed. Uncommitted work preserved. Details and sanitized chronology: [Sol attempt review](../orchestration/reviews/stab-s2a2-unit1-attempt1-sol.md).
- Actual skill use: visible direct High Agency read; worker used a reported immutable propagating/file22/module7/shared-api estimate and meaningful carrier RED, but full durable estimate/mail acknowledgment lagged edits. Long discovery and unchecked runtime assumptions defeated the evidence principle; this is observation, not proof that effort/model caused the errors.
- Product findings: wrong stdin descriptor/lifetime, size/framing mismatch, arbitrary credential error text, bundled empty-carrier omission; worker reported three signal6 bridge failures, not acceptance. Sol routed corrections without writing product/tests.
- Constraint violations: new Swift bridge omitted required child guard, then a probe dumped ambient environment and read it with grep/wc before deletion. Contents/credential presence unknown; filtered output does not mean unread bytes. No retrospective investigation or no-access claim. This repeats a testing-boundary problem and requires an explicit pre-execution design gate.
- Tooling: generic local Bash permission rejection on status bodies, source/rule unknown; permitted scratch-reference reporting worked. Several diagnostic pipelines lost original exits; speculative denial times conflicted with dispatch creation and were rejected. Terminal queued nudges were not receipt proof; interrupt-only plus exact idle wait established pause.
- Improvement: fresh bounded writer, explicit allowlisted test bootstrap, component-safe provenance enforced before every payload, startup-suppressed public wrapper, no ambient discovery, and Sol read-only harness approval before any new child execution. Parent's exceptional Astra high review was static and did not establish runtime safety.
- Client compact evidence: parent verified Luna max selected, bare compact completed19s, Sol high restored/status15.3K/828K. Do not infer the hidden compaction model; argument-bearing slash input had been plain text.

## STAB-S2A-2 unit1 recovery accepted — f56545b (2026-09-27)

- [Independent Sol review](../orchestration/reviews/stab-s2a2-unit1-sol.md): code20 owned paths; independent94 Python isolation/carrier tests,95 Swift tests, real no-Python transport and actual guarded launch-to-SDK-factory checks, unsigned native build. Aggregate remains deferred until final S2A-2 acceptance.
- Actual High Agency use: fresh GLM high read the skill and reported immutable propagating/files22/modules6/shared-api estimate before edits; actual20 files fit. Scope stayed credentials foundation. A bounded socket-guard class compatibility change was explicitly reviewed and shared isolation reverified.
- Static gates rejected a preliminary guard process followed by unverified payload, helper-only credential proofs, unescaped JSON shell values, unsafe job IDs/provenance prefix handling and unbounded waits. Runtime findings then exposed buffered trailing bytes, incomplete headers, physical path spelling, a fake partial writer with no real writes, missing cwd and SDK-import compatibility. None were accepted solely because helper tests passed.
- Reliability: independent bridge refresh caught a35.607s wrapper-install wait timeout after earlier green tests. Observer-before-launch ownership replaced background waitUntilExit; worker repetitions and fresh independent suite passed. Cause confidence is bounded; do not turn a plausible Foundation race into a unique proven cause.
- Safety disclosure persists from attempt1: unsafe ambient dump/read and unguarded bridge attempts are not erased by current evidence. Retry initially used bare Python rerun commands; Sol's controlled runs provide separate provenance. No ambient/secret retrospective investigation occurred.
- Responsiveness: parent observed mailbox JSON piped through head and a fixed240s sleep. Truncation does not itself prove messages were lost; complete batches were required. The positively observed wait-only sleep was interrupted, exact idle verified, and same Dispatch resumed with message-aware55s waits.
- Improvements: prove the tiny supported Foundation channel before building a large harness; enforce same-interpreter guard/provenance before payload; compare actual fake-SDK received values, not a parallel helper result; preserve real command exits and distinguish top-process alarms from process-group watchdogs. Add only verification justified by changed surfaces/failures.
- Parent requested an intentional idle checkpoint after accepted unit1/cleanup, preserving the STILL-ACTIVE Sol Task/Dispatch. Separate STT/vision Keychain accounts, backward-compatible paired carrier and full preflight remain authorized, pending a fresh bounded worker after parent compact/resume. Required vision endpoint configuration must stay explicit and provider-safe.

## STAB-S2A-2 role credentials accepted — c96e0d5 (2026-09-27)

- [Independent review](../orchestration/reviews/stab-s2a2-role-credentials-sol.md): 17 owned paths; separate accounts/UI/clear, persisted explicit endpoint, bounded V2 carrier, independent absence and both-key redaction. Sol independently passed 161 Python, **114 total Swift (113 core + 1 extension)**, real guarded V2 launch-to-production-factory checks, targeted static/script gates and an unsigned native build. The worker's “114+extension” wording overcounted; the review corrects it. No aggregate `make check` ran in this unit.
- High Agency use: GLM reported direct 0.10.1 skill reading before edits and immutable propagating/files<=18/modules<=6/shared-api estimate; actual 17 paths matched. Exact GLM 5.3[1m] high wrapper, one writer, Sol high independent plan/review. No extra manager, substitute model or effort escalation. Public endpoint persistence was corrected by parent before product edits and explicitly acknowledged; no provider preset/fallback was introduced.
- Process failure: GLM omitted executable pre-edit RED and later ran ambient pytest commands piped through sed/tail. Observed examples were `pytest ... test_malformed_envelopes_fail_closed 2>&1 | sed -n 1,40p` and `pytest ... test_engine_carrier.py tests/test_env.py 2>&1 | tail -3`. These are not clean-environment/original-exit acceptance evidence. Presence of ambient credentials or actual protected access/exposure is unknown; no unsafe raw-output or retrospective protected-file investigation was performed. Its later synthetic historical demonstration is retrospective, not pre-edit evidence.
- Applied improvement: parent `msg_554f6d4b4fb7` made GLM source-only and Sol owner of all runtime tests/probes. One precise directive/nudge obtained explicit worker confirmation and exact Delivery ACK; common/role contracts at `74395e7` preserve this rule for later briefs. GLM then supplied source corrections and explicitly authorized formatting; Sol ran controlled checks. Observation: the post-correction evidence is controlled and green. This does not establish earlier no-access or prove a causal defect in High Agency itself.
- Review found helper-only V2 evidence, a nested-JSON digest type mismatch and a substituted vision factory. Corrections now cross both actual launchers, copied entrypoint and production factories with downstream fake SDK constructors. Same-interpreter guard/cwd/import/socket checks remained intact; guard-negative tests preceded the positives. Final Python proof artifacts contain 17 files/53 records.
- Product/test corrections: malformed ports/delimiters/encoded controls; accidental direct-CLI URL-policy change; unhandled config failure/redaction before assignment; ignored key-storage failure and unsaved key material; trimmed provider-key leakage; escaped surrogate partial assignment; optional Swift root capture; test comment/private-setter compile errors; an incidental AttributeError hidden by broad exception assertions; and a reversed endpoint-rotation fixture. Sol returned each finding to GLM; assertions were made specific, not weakened. Initial Swift build exit1 and model selection exit1 are documented with repro/expected/actual/fixes in the review; final runs passed.
- Tooling errors: Sol's unsupported `worker-read --lines` exited1, corrected to `--limit`; a nonexistent installer `.zsh` path exited127, corrected to actual `.sh` with exit0. A duplicate-target checkpoint delete/add patch was rejected without mutation; a normal doc rewrite succeeded. Initial Ruff lint exit1 (5 findings) and format exit1 were repaired by GLM; final check/format/mypy/script exits0. No unresolved failure remains inside this unit.
- Limits: cooperative guard is not an OS sandbox; aliases/descriptors/native consumers/system zshenv and XPC watchdog limits remain. Real Keychain/UI/provider/media/FCP/signing and packaged supply acceptance are not established by fake tests and unsigned build. Full readiness/multi-audio and the final aggregate, then S2A-3/S2B/C/S3–S5, remain pending.
- Lifecycle: `task_12f6e90b11ea` / `ctx_02bac39b785a` succeeded at12:11:51UTC, explicit code commit `c96e0d5`. Release retained the externally created terminal; exact idle/incarnation was verified and the unused terminal closed (`ptyKilled=true`), reclaimable query empty. Shared final docs were deferred until after closure. Overall Sol Task stays active for parent compact/resume, with no `worker_done` at this internal boundary.
- Client compact observation: parent reported Luna max selected and verified, bare `/compact` completed55s with491K->19.8K/828K, then Sol high restored/verified. This establishes client selection/completion only; no hidden compaction-model inference. Use standalone slash commands and verify completion.

## STAB-S2A-2 final preflight accepted — 37a7bc5 (2026-09-27)

- [Independent final review](../orchestration/reviews/stab-s2a2-final-preflight-sol.md): S2A-2 feasible offline scope is complete. Sol's final `make check` passed 2,868 Python tests in 66.70s, Ruff/format, mypy 55 files and all 21 invariants; full Swift passed **142 total** (141 core + 1 extension); fresh unsigned native app/extension and uncovered engine/installer gates exited 0. All 25 file hashes matched before/after verification and after the explicit-path code commit. The first failed aggregate was rerun only after concrete fixes.
- Routing and actual skill use: Sol high handled planning/review, one exact `claude-glm --model 'glm-5.3[1m]' --effort high --permission-mode auto` worker wrote source. GLM directly read High Agency 0.10.1 from its `.claude/plugins/cache/high-agency/high-agency/0.10.1/skills/high-agency-coding/SKILL.md` and acknowledged immutable propagating/files22/modules6/shared-api before edits. User-required Orca topology and source/runtime role separation governed execution. No extra manager or model substitution was used. Routine fixture/compiler errors were resolved with specific evidence and Sol guidance, not an unexplained model change.
- Meaningful RED: two existing Swift APIs failed before product edits (missing development wrapper still produced a runnable plan; actual Swift grant omitted Python's key). Build passed and tests exited 1. The worker's initial large draft depended on nonexistent APIs and lacked synthetic CLI dotenv/cwd setup; Sol refused to execute it. The worker removed its own untracked drafts and supplied the two bounded tests. Coordinator-specific tests did not have a separate pre-edit RED; that is distinct from falsely saying this entire task lacked a meaningful RED.
- Mailbox/process failure: roughly 17 minutes of discovery and premature test drafting continued without consuming queued narrowing. Root sent one interrupt to the exact live terminal; its unread-mail notice triggered a new turn, and GLM processed/ACKed `delivery_f476b8e05243`. Same Task/Dispatch continued. Root later observed a body renderer slicing at 1,500 characters; this cannot prove any particular body was lost, but it violated the complete-batch guarantee. Plain JSON check/ACK replaced it. No restart or second writer was used.
- False source reports: an early freeze claimed synthetic fixtures that were absent. Later scripted edit batches aborted while reports claimed the env parameter rename and explicit-nil Swift helper were fixed. Sol readback and tests disproved those claims; worker acknowledged them and supplied actual readbacks. These are observed reporting/verification failures, not evidence that High Agency itself caused the defects. No unsafe draft CLI test or real settings/bookmark fixture was executed: Sol rejected those before runtime.
- Runtime separation held for acceptance: Sol ran every test, build, child probe and aggregate. GLM disclosed early static Ruff checks/formatting, py_compile and targeted mypy; they are not independent acceptance evidence. The final report must not turn this into a blanket claim that GLM ran no commands. Earlier unit1/role/isolation unsafe-execution incidents retain their uncertainty and were not investigated retrospectively.
- Product findings corrected: actual caller scope ordering and partial cleanup, repeated bookmark resolution, wrong development job argv, checks after staging, key-free roughcut tool requirements, Release consulting a development bookmark, raw readiness errors, cross-language grant spelling/fingerprint/path preservation, malformed stream entries interpreted as zero, upfront V2 paid-role validation, and preflight error redaction. A manual bundled test bypassed the production factory/PATH/carrier and was replaced by the actual factory/prober path with per-payload proof. Markerless/V1 staged credential/backend semantics were preserved explicitly while V2's real consumer uses separate role values.
- Compiler/test failures: ten Swift compile rounds failed before the first green test-target build, exposing argument, actor-isolation, syntax, capture, locking, optional and fixture-encoding defects. Sol used the primary [SE-0461](https://github.com/swiftlang/swift-evolution/blob/main/proposals/0461-async-function-isolation.md) specification to direct caller isolation after a speculative closure-only fix failed. Sol initially misidentified a syntax error as an `@` attribute, then read the source and immediately corrected it to modifier position; that reviewer error is recorded. Full Swift then found five tests / 14 issues; all are resolved in final 142-test evidence.
- Python failures: combined tests first exposed a leaked V2 process marker (77 pass / 31 fail), then two fixture errors (110 pass / 2 fail). The aggregate exposed three missing synthetic prerequisites plus the inherited env-to-project import violation (2,862 pass / 5 fail; make exit 2). Sol explicitly approved drift to **25 files / 7 modules** for env/carrier/env-test paths; the invariant was repaired, not exempted. A missed rename then caused NameError failures. The SDK-redaction fixture needed synthetic preparation and an explicit reached assertion; absence caused by an earlier failure was rejected as evidence. Worker causal stories about the fixture were distinguished from measured results: Sol had already observed one green run before the final reached-assertion addition.
- Test provenance: final aggregate has 21 guard-proof files / 61 records, mixing positive payload and intentional refusal cases. Successful copied-source records prove synthetic cwd/import and blocked protected reads/socket/connect; failed editable-fallback records are not successful provenance. The cooperative guard is not OS confinement, and the external process-group watchdog does not establish control of independent XPC children. No live Keychain/bookmark/sandbox/provider/original-media/FCP/signing acceptance is claimed.
- Applied improvement: explicit source freezes, pre-execution harness review, original-exit capture, source-hash pinning and refusal to weaken assertions caught the defects before acceptance. Further improvement: use 2–3-path source/build checkpoints before drafting a thousand-line test file; verify each scripted replacement count and read the resulting function before reporting it fixed; distinguish source inference from runtime reach; preserve complete mailbox bodies; test actual producer/consumer paths rather than disconnected factories. No installed skill/plugin files were modified.
- Lifecycle: code `37a7bc5`, child `task_8d2591fb88d9` / `ctx_daff7e378c65` succeeded at 14:26:52 UTC via `msg_ea85e9c57aec`. Release returned external retention; exact terminal/incarnation was idle-verified and closed (`ptyKilled=true`), reclaimable query empty. Final tracked docs were deferred until closure. No scoped error remains unresolved; external acceptance and S2A-3/S2B/C/S3–S5 remain pending.
- Latest compact evidence from root `msg_839ac108f271`: Luna max selected session-only, `/status` 279K/828K, bare `/compact` completed in 58s, Sol high restored with `/status` 19.5K/828K. This records client selection and observed completion only. Argument-bearing slash commands had previously been plain text; use the standalone command and verify completion, without inferring a hidden server compaction model.

## STAB-S2A-3 progress refresh accepted offline — 320dff4 (2026-09-28)

- Independent evidence: [S2A-3 progress refresh review](../orchestration/reviews/stab-s2a3-progress-refresh-sol.md). Sol accepted commit `320dff4aef31643a4c3bbeb05779f3f207f53786` offline. Scope was 14 files / 5 modules / shared API within the 18-file cap; the development installer was explicitly allowed. All 14 owned hashes matched before and after gates and after commit.
- Runtime evidence: final `make check` passed 2,885 Python tests in 68.89s, Ruff/format on 127 files, mypy on 55 files and all 21 invariants. Full Swift passed 167 core + 1 extension = 168 tests. Unsigned native build and all four uncovered engine/installer checks exited 0. The Python producer golden test passed once in 0.07s against the Swift fixture digest.
- Meaningful RED: before edits, Python reached three missing-progress assertions and exited 1. Swift built, then the intermediate relay failed after 2.028s while its bundled control passed. The initial harness lacked synthetic dotenv/cwd setup, called `NSLock.lock` from async source, raced continuation registration/release and used the bundled plan in a development fixture. At Sol's request, GLM corrected the source before Sol ran any runtime check.
- Build and test failures: B1–B7 Swift builds exited 1; B8 (`swift-build-eighth.log`) exited 0 in 4.81s. The seven failures exposed public defaults/internal helpers plus an extra `isStopped` argument, a non-Sendable registry, stale type name, initializer mismatch, two test `Task` isolation captures, `Data(String)`/argument order and a wait-helper actor transfer. They were compiler errors, not evidence of live-app crashes. A 25-test run ended 24 pass / 1 fail because an unchanged-running counter reset on each observation and triggered two assertions; a separate counter fixed it and the corrected test passed. A 26-test run ended 25 pass / 1 fail due to a wrong approval fixture; its correction passed. Final full Swift passed 168 tests.
- Product corrections covered invocation ownership, the same-bytes relay race, identity binding, typed wire/digest handling, legacy retry baseline and late UI callbacks. The terminal behavior remains cadence-limited read-only polling: the last known stage can remain visible while the view is active, matching terminal records win, and absent/finalizing records are bounded. There is no heartbeat or proof of actual process liveness.
- No acceptance is claimed for real providers, Keychain, original media, Final Cut Pro, signed sandbox or packaged engine supply. The guard summary's 21 files / 61 records includes intentional refusals and is not universal proof of no access. Earlier unsafe environment-dump uncertainty remains unresolved; later green evidence does not establish what earlier processes accessed.
- Skill/model evidence: the single source writer used `claude-glm --model 'glm-5.3[1m]' --effort high --permission-mode auto`; the reported preflight was high complexity, propagating / files<=18 / modules<=5 / shared-api. Root observed the immutable preflight and High Agency skill-read acknowledgment before mutation. The worker-reported path was `/Users/ssojux2/.codex/plugins/cache/high-agency/high-agency/0.10.1/skills/high-agency-coding/SKILL.md`, recorded in `msg_230cced5d547`. Do not claim remote provider identity or automatic hook coverage beyond that evidence. This documentation task was assigned GPT-6 Luna max for mechanical record/context work; ROOT Phase B remains pending explicit GO, and no compact success is claimed. No model fallback or subagent was used, and no runtime test was changed or rerun here.
- Concurrency premise: a compiler-only probe treating `UserDefaults` as `Sendable` exited 1; no defaults object was constructed. A narrow private immutable read-only probe was justified by Apple's same-instance thread-safety documentation ([source](https://developer.apple.com/documentation/foundation/userdefaults)). Sol declined expanding `ActiveProject.swift` Sendable/is-current APIs because the underlying Sendable premise was disproved.
- Orca recovery history: prior custom-Luna task `task_0092f9cec441` / `ctx_631d3ab44599` failed at `agent_readiness` before prompt injection; its empty terminal was closed. Starting that failed Task again was refused as `task_not_startable`; its recovery `nextSteps` permitted a new Task. The current `task_f44e30fb7276` / `ctx_9aca738eff72` has Run/Task/Dispatch authority, but its terminal is operator-owned and unsupervised; it does not confer supervised process ownership. The prior Task IDs remain settled and are not reused.
- Tool observations: `terminal read --lines` was unsupported and the read was corrected with `--screen`. A temporary-note patch using duplicate delete/add was rejected without mutation and corrected with UpdateFile. A new Luna terminal idle-wait timed out while a fresh Luna max prompt was visible; this is an observation gap, not proof of a dead agent. No evidence supports attributing a delay to the plugin.
- Tooling note: one follow-up ASK placed literal backticks inside a double-quoted shell argument, so shell command substitution removed `env -i` from the question. Root supplied verified details; future asks must use shell-safe argv/quoting.
- Checkpoint credential handling: the first uncommitted checkpoint briefly contained an Orca Dispatch capability field. Root removed the value-bearing field before commit; no capability value is recorded here. Future checkpoints retain Task, Dispatch and terminal IDs only.
- Improvements carried forward: use complete mailbox bodies and acknowledge exact deliveries at file checkpoints and before `SOURCE-READY`; freeze and inspect the changed child harness before Sol runs it; keep synthetic fixture identity and approval states coherent; make semantic negative tests recompute digests; preserve original exits and source provenance; keep GLM source-only and Sol as runtime owner. No scoped implementation or test failure remains; next is the revised documentation review, then ROOT maintenance after explicit `GO-ROOT-COMPACT-S2A3`, then S2B.

## STAB-S2B-1 durable-ledger foundation accepted offline — 99e0202 (2026-09-28)

- Sol accepted the offline foundation commit `99e0202a6e7cab38ee16a081ebbf9dc30fa23ed6`: `vlogsrt/project/budget_ledger.py` and `tests/project/test_budget_ledger.py`. The two accepted SHA-256 values are recorded in the [Sol review](../orchestration/reviews/stab-s2b1-durable-ledger-sol.md); both matched after validation, commit and the stop hook. This primitive still does not enforce budget accounting on current paid-request paths.
- The real baseline RED was collection exit 2 because the new `vlogsrt.project.budget_ledger` module did not exist at test import line 27. It was an expected missing-API boundary, not an existing behavioral failure. The exact command and the later clean-environment validation commands/results are preserved in the review; do not reconstruct absent shell arguments from output-only logs.
- V1 targeted tests had two failures: fixed-point serialization of accepted `1E+24` exceeded its own 18-digit bound, and a mixed-concurrency test oracle expected 12 instead of the 16 reconciliations actually performed because it missed releasing unused reservation after a `0.5` measurement. V1 mypy had four errors (three exponent-literal comparisons and one reservation-state `Literal` mismatch). V2 widened the bounded numeric domain to 30 significant digits (minimum exponent -42, adjusted exponent <=24, trapped precision 80), added existing transcription-usage compatibility coverage, corrected the concurrency oracle without weakening it, and fixed the type narrowing/state annotation. Automatic creation of parent directories was removed so callers must supply a durable parent.
- Final Sol results: ledger tests 46 passed/0.19s; affected workflow protocol/analysis tests 22 passed/0.06s; mypy one file exit 0; Ruff check and format check for two files exit 0; all 21 invariants exit 0. Logs and exact commands are in the review. No full `make check`, Swift or native gate was claimed for this isolated unit; aggregate gates remain with integrated S2B.
- **High Agency metric drift:** immutable preflight said `files<=2 / modules<=1`, while the stop hook measured **2 files / 2 modules** and classified major drift. Git confirms the explicit two-path ownership stayed unchanged; Sol checked both paths plus the affected protocol and analysis modules. Root had underestimated how the hook counts the product and its test. Record the module mismatch; do not report a metric match.
- Process observations: initial source discovery took over five minutes. The bounded brief required the complete implementation and matrix before a first green source-ready checkpoint, delaying runtime feedback while static corrections continued. Improve the next brief by requesting an earlier small functional checkpoint and by estimating the hook's actual module metric. This is an observed sequencing choice, not evidence that the plugin or model caused delay.
- Orca observations: a duplicate `check --wait` returned `waiter_exists` while the original waiter remained active; the original later returned `timedOut`. The accepted GLM terminal's first 1-second idle wait timed out, then the exact terminal was observed idle after a 30-second wait and closed (`ptyKilled=true`). The worker's `filesModified` payload was one space-separated string although Git confirmed the two paths; Git is the authoritative path list.
- Model routing observation: normal `worker-start` rejected the requested `gpt-6-luna max` flags at preflight with `invalid_argument` before creating a Task. A direct custom Codex launch visibly showed GPT-6-Luna max at a fresh prompt, but Orca's 30-second `tui-idle` wait timed out; that mismatch is an observation gap, not proof of a dead agent. The current low-level Dispatch supplied Run/Task/Dispatch authority without owning the terminal process. This worker's runtime terminal preview showed `GPT-6-Luna max`; no global setting was changed. No evidence supports attributing the routing or timing observations to the plugin or model.
- Skill-use evidence: GLM reported reading `/Users/ssojux2/.claude/plugins/cache/high-agency/high-agency/0.10.1/skills/high-agency-coding/SKILL.md` and reported an immutable `high / DIRECT / glm-5.3[1m] high` preflight. GLM remained source-only; Sol owned all runtime tests and gates. Static review caught acceptance of non-string SHA-256 values and duplicate JSON object keys before acceptance; the corrections were included before final v2 validation.
- Existing pytest conftest guards were active and cooperative; they are not an OS sandbox or retrospective proof of historical no-access. Durability limits also remain explicit: caller-supplied durable parent directory and cooperating local POSIX file-lock assumptions, with no noncooperative-writer, cross-host or power-loss proof. No real provider, paid request, original media or Final Cut Pro path was exercised. Next is a fresh bounded S2B-2 source-only unit beginning at a generic per-request observer in `transcribe.py`, preserving the legacy-to-project dependency boundary; the project adapter/estimates and later cache/retry/UI work remain pending.

### S2B-1 maintenance did not complete — 2026-09-28

- The documentation commit `bb6860e39fd784b102574429212b6df3247ec6c6` remains accepted. Helper `task_43eb8511d90a` / `ctx_f2470414e1aa` reported failed maintenance in `msg_f2098522ab63`; this does not invalidate the product or documentation acceptance.
- Observed sequence: one planned ROOT interrupt succeeded; a 30-second idle wait timed out while the screen showed an interrupted prompt; bare `/model` opened the picker. Sending a Down key returned `runtime_access_denied`/EPERM, and the subsequent escalated request remained on an approval screen. ROOT later independently observed that screen. Earlier `jq` inventory filters also failed on null results; raw responses, not filter exits alone, must determine runtime state.
- The user manually completed model settings and resumed ROOT. ROOT revoked further helper control and cancelled the obsolete approval with Escape, then resumed the helper only to report and settle. No pending command was approved and no permission policy was changed. The helper reported no accepted or queued Down-key input, no `/status`, no `/compact` and no continuation prompt. Thus no S2B-1 compact completion or context reduction is established; the earlier S2A-3 compact evidence must not be reused as current evidence.
- ROOT observed GPT-6-Sol high after user resumption, accepted the truthful failure report, released the helper, verified its idle prompt and closed its exact terminal (`ptyKilled=true`). Delivery `delivery_d733ab6f16db` was acknowledged; no reclaimable workers remained. The checkpoint stays uncommitted, and the original project proceeds with fresh S2B-2a implementation.
- Improvement: surface a helper approval wait promptly through the coordinator mailbox, avoid automatic permission escalation for optional context maintenance, and do not let model-picker automation delay authorized feature work. Keep exact commands/current authority in the initial maintenance packet and batch documentation review at a stable boundary.

## STAB-S2B-2a request observation accepted offline — db32c85 (2026-09-28)

- **Code:** `db32c8503db2d0581a87607a3f7ab4f3a0aa10cc`, only `vlogsrt/transcribe.py` and `tests/test_request_observer.py` (1,232 insertions/9 deletions). Generic authorization, original-response and SDK-error hooks now surround each observed request attempt, including retries and reduced-option retries. SDK retry policy, unobserved legacy behavior and the single transcription call site remain intact. Project ledger/estimate/cache/UI integration is still pending.
- **Skill/routing evidence:** exact launch `claude-glm --model 'glm-5.3[1m]' --effort high --permission-mode auto`; worker reported reading the installed `.claude/plugins/cache/high-agency/high-agency/0.10.1/skills/high-agency-coding/SKILL.md`, then a medium/DIRECT/high preflight with immutable `files<=2 / modules<=2 / boundary=shared-api`. Sol owned all runtime verification; GLM performed source edits and static Ruff/mypy checks. Final source spread matched the declared two paths/two modules.
- **Early checkpoint:** missing observer API produced collection RED (exit 2, `ObserverFailure` import missing); this was API absence, not a runtime behavior failure. The minimal implementation then passed 4 tests in 0.04s before the full matrix. Shared event traces prove authorization → SDK call → original-response hook → conversion, and denied authorization prevents the SDK call.
- **Observed extended-run failures:** CP3-v1 had 18 failed/9 passed (exit 1, 0.53s). One product defect called a zero-argument observed closure with an argument; a broad `Callable[..., Any]` annotation hid it from static checking. One test defect removed the fake SDK's `audio.transcriptions` namespace, causing cascading failures. These were two primary causes, not 18 independent product defects. Exact callable signatures and faithful fake namespaces fixed them; CP3-v2 passed 30 cases plus 194 affected cases.
- **Reviewed corrections:** local file prerequisites and model-key collision checks precede authorization; transport `api_key` and `extra_headers` are excluded from metadata snapshots without altering outgoing requests. Fatal-state check and authorization share an admission lock. Cancellation is checked inside that lock immediately before authorization and again before sending; cancellation after a ticket leaves uncertainty rather than fabricating an SDK failure or zero charge. Already-running SDK outcomes can still be reported.
- **Test precision:** the preparation test now changes a flag in actual synthetic preparation. The queued-cancellation test waits for a separate admission-boundary event while another authorization owns the lock, with bounded waits and finally cleanup. A retry test's `backoff_base=0` still slept one second because `0**0=1`; fake sleep now records the backoff event and asserts error-hook/backoff/next-authorization order. CP3-v3's 31 cases passed in 1.12s; final test-only refinement passed 31 in 0.09s.
- **Minimum-version review:** the metadata proxy default uses `default_factory`. Python documents rejection of unhashable dataclass defaults from 3.11 and mapping-proxy hash support from 3.12; this motivated the source correction. No Python 3.11 runtime pass is claimed. Sources: [dataclass defaults](https://docs.python.org/3/library/dataclasses.html#mutable-default-values), [mapping proxies](https://docs.python.org/3/library/types.html#types.MappingProxyType).
- **Final Sol acceptance:** clean-environment `make check` exited 0: **2,962 Python tests passed in 66.47s**, Ruff check/format 130 files, mypy 56 source files, all 21 invariants. Final target 31 passed in 0.09s. Final hashes `/tmp/stab-s2b2a-sol-final.sha256` matched after all gates and after commit; scoped diff check was clean. Logs: `/tmp/stab-s2b2a-sol-final-target.log`, `/tmp/stab-s2b2a-sol-make-check.log`; prior failure/affected logs share that task prefix.

Final child commands used the following literal clean environment. They were supervised by the inspected stdlib driver `/tmp/stab-s2a3-sol-watchdog.py` (target 40 seconds, aggregate 240 seconds), which printed `SOL_ORIGINAL_EXIT=0`; original exits were captured before log-tail formatting.

```sh
/usr/bin/env -i HOME=/tmp/stab-s2b2a-sol-home TMPDIR=/tmp/stab-s2b2a-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 VLOGSRT_OFFLINE=1 /Users/ssojux2/Working/vlogsrt/.venv/bin/python -m pytest tests/test_request_observer.py -o addopts= -q --durations=3 --basetemp=/tmp/stab-s2b2a-sol-final-target
/usr/bin/env -i HOME=/tmp/stab-s2b2a-sol-home TMPDIR=/tmp/stab-s2b2a-sol-tmp PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 VLOGSRT_OFFLINE=1 PYTEST_ADDOPTS=--basetemp=/tmp/stab-s2b2a-sol-aggregate /usr/bin/make check
```

- Existing conftest guards remain cooperative, not an OS sandbox or retrospective no-access proof. The watchdog bounds its process group, not unrelated or independently detached children. No live product provider, original media, FCP, Swift/native rerun or current compact success is claimed.
- **Settlement:** `task_2ff24b9963e4` / `ctx_b9eed3e720a8` succeeded via `msg_ad0871324707`; both paths were correctly listed in the completion payload. ROOT verified hashes and idle state, released and closed the exact external terminal (`ptyKilled=true`), and ACKed `delivery_90058102f3b8`. No reclaimable workers remained. Next is S2B-2b project ledger/estimate integration, followed by S2B-3 cache/retry/cost UI; the overall project is incomplete.

## STAB-S2B-2b 유료 요청 회계 인수 — e76df91 (2026-09-28)

□ 모델·작업 범위
  ○ GLM은 설치된 High Agency 0.10.1 스킬을 읽고 `high / files<=8 / modules<=8 / shared-api`를 사전 기록함
  ○ 실제 커밋 11개 경로: 원래 소유 7개와 Sol이 단계별로 승인한 테스트 확장 4개. 파일 수 예상은 빗나감
  ○ GLM은 소스와 정적 검사만 담당함. Sol은 가짜 SDK의 실제 호출 경계에서 모든 실행 검증을 담당함

□ 관찰된 오류와 고친 내용
  ○ 첫 RED 테스트는 600초 리소스 때문에 승인액을 전사에서 먼저 소진함. 합성 리소스를 12초로 맞춰 Luna 호출 도달을 증명함
  ○ 과거 기록 검사가 JSONL 줄바꿈을 빼고 디코더를 호출해 현재 시도까지 거부함. 줄바꿈을 보존하도록 수정함
  ○ 기존 진행률·엔진 가짜 Luna 함수가 새 관찰자 인자를 받지 못해 5개 창이 오류가 됨. 함수 계약을 맞춤
  ○ 병렬 사용량 투영은 잠금 전에 스냅샷을 읽어 오래된 내용으로 덮을 수 있었음. 잠금 순서를 고치고 결정적 경합 검사를 추가함
  ○ GPT 전사의 `UsageDuration.seconds`를 해석하지 못하던 경로를 고침. 미터링 해석 오류는 회계 문맥 전체를 닫도록 함

□ 검사에서 드러난 개선점
  ○ 고정 4회 전사 배수는 재시도까지 포함한 완료 비용 상한의 증거가 아님. 승인된 작업 몫으로 설명을 바꾸고 소진 시 정직하게 중단함
  ○ 견적 지문이 엔드포인트·역할을 묶자, 기존 테스트 3개가 검사와 실행 사이에 값을 바꿔 의도한 키 검사에 도달하지 못함. 합성 설정을 검사 전부터 고정함
  ○ 새 요청 형식의 구조·추가 필드를 엄격히 검사함. 앞으로 요청 모양을 바꾸면 견적 정책과 회귀 검사를 함께 갱신해야 함
  ○ High Agency 파일 예상 `<=8` 대비 실제 11개임. 초기 경계 설계에 기존 테스트의 설정 순서 영향까지 포함해야 함
  ○ GLM이 Sol의 늦은 소스 검토 메시지를 읽기 전에 최종 인수 질문을 보냄. 최종 질문 직전 수신함을 비우고 미해결 지적을 확인하는 절차 필요

□ 검증과 남은 일
  ○ Sol 관련 검사 103개 통과. 첫 전체 검사 2,975개 통과·3개 실패를 고친 뒤 최종 2,978개 통과, Ruff·mypy·불변 규칙 통과
  ○ 코드 커밋 `e76df91f951c0b28e7fe60c29d35929f0a56f06b`; 상세 근거는 `docs/orchestration/reviews/stab-s2b2b-accounting-integration-sol.md`에 남김
  ○ 미지수 사용량과 원장 수치는 아직 앱 화면에 나오지 않음. 성공 캐시·명시적 재시도·비용 화면은 S2B-3에서 구현 필요
  ○ 실제 공급자·원본 미디어·Final Cut Pro는 실행하지 않음. 앞선 작업의 보호 한계나 과거 자격 증명 접근 여부는 이 통과 결과로 달라지지 않음
