# vlogsrt 다중 모델 구현 운영 규칙
기준 명세: ../plans/2026-09-08-final-cut-project.md
기계 판독 설정: workflow.json
사용자가 2026-09-08에 모델 역할, 구현 착수, 독립 작업 병렬 브랜치와 페이즈별 Astra 머지를 명시적으로 승인했다.

## 역할과 코드 소유권
- Astra gpt-6-astra high: 설계/최종 판단, 페이즈 통합과 머지.
- Sol gpt-5.6-sol xhigh: 중간 관리자. 작은 구현 명세, 의존성 DAG, 워커 할당, 검증과 연속 실행을 소유.
- Opus claude-opus-5 high: Sol의 공동 관리자. 시작부터 페이즈 설계와 복잡한 태스크를 검토하고 구현 루프를 돕는다. Sol이 막힌 문제의 상위 진단도 맡는다.
- claude-glm glm-5.3[1m] CLI max: 실제 제품 코드와 테스트의 유일한 작성자. 최초 xhigh 요청은 사용자가 max로 변경했다.
- Terra/Luna high: 재현과 원인 분석, 작은 수정 명세만 작성. 코드 변경은 GLM에 되돌린다.
- 관리자들은 docs 계획/리뷰/ledger와 실행 메타데이터만 수정한다. 개발 환경 설치/검증은 허용. 역할 변경을 몰래 하지 않는다.

## 병렬 구현과 통합
- 같은 페이즈의 독립 태스크는 별도 GLM worktree/branch로 최대2개 병렬 실행한다. 사용자가 병렬·브랜치 분리를 승인했다.
- 공유 모델/함수계약을 먼저 확정한다. 파일 소유권, 소비/생산 인터페이스, 의존성이 겹치면 선행 태스크를 먼저 끝내거나 직렬화한다.
- 작업 branch는 승인된 페이즈 시작 integration HEAD에서 만든다. Orca child worktree + 명시적 base-branch를 사용한다; main/default base로 잘못 분기하지 않는다. setup run; 현재 repo hook은 비어 있음.
- GLM은 태스크 브랜치에서만 커밋하며 직접 머지/푸시하지 않는다. 사용자 .env.example 변경을 건드리거나 커밋하지 않는다.
- Sol이 ready-to-integrate 보고서(브랜치/커밋/기준HEAD/테스트/리뷰/미해결)를 제출한다. Astra가 통합 테스트와 충돌 검토 후 feat/final-cut-project에 머지한다.
- 충돌은 자동 ours/theirs로 처리하지 않는다. Sol/Opus가 합성 수정 명세를 주고 GLM 통합 워커가 고친 뒤 Astra가 검증·머지한다.
- 다음 페이즈는 해당 통합 완료를 선행조건으로 삼는다. 기존 사용자 변경은 모든 merge 전후 해시/상태로 보존한다.
- 푸시/배포는 이번 실행 범위가 아니다.

## 연속 구현 루프
1. Sol이 docs/orchestration/tasks/에 태스크 brief를 작성한다. task-template.md의 필드를 빠짐없이 채운다.
2. Opus는 페이즈 공통계약/위험 태스크를 사전 검토한다. 판단은 기록하고 해결 없이 GLM에 모호함을 넘기지 않는다.
3. Orca Run/Task/Dispatch 실제 상태로 GLM을 할당한다. 각 fresh GLM은 한 태스크만 구현한다.
4. GLM은 실패 테스트→최소 구현→관련 검사→make check→태스크 커밋 후, 정확한 SHA와 검사 결과를 review-ready 상태로 보고하고 Orca ask로 Sol의 검토 결과를 기다린다. Sol/Opus의 범위 내 수정 사항은 같은 Dispatch에서 닫고 검사를 다시 통과한 뒤 실제 worker_done을 보낸다.
   이미 worker_done이 전송된 같은 태스크의 후속 수정은 기존 GLM 터미널을 즉시 새 Dispatch에 재사용할 수 있다. 이는 같은 문제의 수정 연속 작업이며, 독립 태스크는 fresh 세션 원칙을 유지한다. 완료 터미널은 Orca의 reuse/release 규칙으로 처리하고 retained 판정을 수동 종료로 우회하지 않는다.
5. Sol은 범위/회귀 증거를 확인하고 Opus는 복잡한 diff와 페이즈 전체를 검토한다.
6. 모든 수정 요청은 GLM에 전달한다. 중요 결함이 열려 있으면 완료로 표시하지 않는다.
7. 페이즈가 준비되면 Astra에 통합 요청. 승인된 태스크를 이어서 실행하며 매 태스크 사용자 재확인을 요구하지 않는다.

## 실패와 상승 경로
- 실패 보고에 재현 명령, exit code, 짧은 로그, 예상/실제, baseline commit, 변경파일, 시도한 가설을 포함한다.
- 동일 실패가2회 반복되거나 설계/통합 오류면 Sol이 Terra(구조/시각/타입)와 Luna(최소재현/테스트/fixture)에 독립 진단을 요청한다.
- Sol이 진단을 합쳐 한 GLM 수정 태스크로 만든다. 새 증거 없는 같은 프롬프트 반복 금지.
- Sol 진단 후 수정에도 같은 핵심 문제가 남으면 Opus5가 최소 수정 설계를 결정한다.
- Opus 설계로도 해결되지 않거나 원본 불변계약/제품 범위 판단이면 Astra에 최종 escalation한다. Astra 판단도 GLM 구현 태스크로 전달한다.
- 모델 인증·권한·서버 장애는 다른 모델의 코드 수정으로 해결된 척하지 않는다. 실제 승인 거절/오류를 보고하고 독립 작업만 계속한다.
- 원본 미디어/실제 FCP 인수 자료가 없으면 offline 구현은 계속하되 해당 인수만 미수행으로 기록한다. SKIP!=PASS.
- worker depth 제한을 우회하거나 다른 Run으로 숨기지 않는다. nested dispatch가 거부되면 manager가 root에게 정확한 brief/launch 요청을 전달하고 root가 허용된 깊이에서 할당한다.

## 실행 명령과 검증
Codex 관리자/진단은 Orca worker-start --agent codex --model <정확한 ID> --effort <level> 사용.
Opus는 일반 claude --model claude-opus-5 --effort high. GLM wrapper를 사용하면 안 된다.
GLM은 claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto. 사용자가 P0 GLM 터미널에서 auto 모드를 직접 선택했고, 같은 프로젝트의 후속 워커도 이 선호를 이어 적용한다. 기존 deny 규칙은 유지한다. 기존 wrapper가 endpoint/key를 프로세스 한정으로 관리한다. 키 파일/.env를 읽어 출력하지 않는다.
claude-glm fresh terminal이 필요하면 Orca terminal create --command에서 실행하고, task를 worker-start --terminal 또는 dispatch --inject로 연결한다. 일반 claude만 실행해 GLM인 척하지 않는다.
프로젝트 .claude/settings.json의 보호 규칙을 유지한다. 안전 장치 bypass 플래그를 추가하지 않는다.
사용자가 GLM effort를 max로 변경했고, glm-5.3[1m] --effort max 실행 시험이 GLM_MAX_READY 응답과 contextWindow=1000000으로 통과했다. 새 워커도 max로 실행하고 launch receipt를 확인한다. 서버 내부 effort 매핑이나 실제 1M 입력 부하 시험까지 확인한 것은 아니다.
관리자는 launch.requested/effective, GLM 실제 모델/컨텍스트와 task/dispatch ID를 ledger에 남긴다.
전사/vision 실제 API fixture는 기존 정책상 사람이 녹화한다. 이번에 명시적으로 요청한 코딩/리뷰 모델 호출과 혼동하지 않는다.

## 실행 기록
- 관리자 ledger: docs/orchestration/progress.md (Sol 소유)
- 태스크 briefs: docs/orchestration/tasks/ (Sol 소유)
- 리뷰: docs/orchestration/reviews/ (Opus 소유)
- 런/디스패치 receipts: docs/orchestration/runtime.json (Astra 소유)
- 구현 상세 로그와 diff는 git-ignored work/orchestration/ 사용. 커밋에 키/실제 미디어/개인 경로 응답을 넣지 않는다.
- 관리자/worker가 완료되면 실제 Orca lifecycle에 따라 재사용 또는 release한다. 종료/timeout만으로 완료 취급하지 않는다.
