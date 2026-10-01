# 브이로그 자동 편집 안정화 — 승인된 실행 범위

## 사용자 결정
- Final Cut 안에서 실행하는 검토 가능한 러프컷을 완성한다.
- 단순 컷 타임라인과 SRT만 사용한다. 다른 효과는 사용하지 않는다.
- 같은 프레임레이트 위주다. 혼합 레이트 자동 변환은 이번 범위 밖이다.
- 대화 무음/NG/재촬영 정리와 B-roll 압축이 모두 중요하다.
- B-roll은 분위기를 보존하고 반복/대기만 줄인다. 강제 삭제 비율은 없다.
- Sol이 세부 구현 명세를 Claude-GLM에게 전달하고, GLM 구현 후 Sol이 계획 준수와 동작을 직접 테스트한다. 단계별로 진행한다.

## 역할과 실행
- 사용자 최신 원칙: 작업량이 큰 구현·반복 수정·대량 분석은 항상 Claude-GLM에 이관한다. Sol은 세부 계획·분할·검토·독립 인수 테스트를 담당한다. 프로젝트 AGENTS.md에 지속 지침으로 기록한다.
- 현재 사용자 요청의 두 역할을 사용한다: Sol 계획/검증, Claude-GLM 제품 코드/테스트 구현. 관리자는 제품 코드를 대신 수정하지 않는다.
- 사용자 최신 모델 설정: Sol `gpt-6-sol` high, `claude-glm --model 'glm-5.3[1m]' --effort max --permission-mode auto`.
- Orca의 실제 Run/Task/Dispatch로 감독한다. 한 번에 한 GLM 구현 작업만 실행한다.
- 이번 통합 기준은 현재 체크아웃 `/Users/ssojux2/Working/vlogsrt`, 브랜치 `Ssojux2/r08-clip-input`, 시작 HEAD `0d91da1`이다. 옛 문서의 feat/final-cut-project/중단 상태를 새 작업에 적용하지 않는다.
- 순차 작업이므로 현재 체크아웃을 공유할 수 있다. Sol은 문서만, GLM은 해당 단계의 코드와 테스트만 쓴다. 변경 파일을 명시해서만 커밋한다. 푸시/배포하지 않는다.
- 사용자 변경 `.env.example`는 읽거나 수정하거나 커밋하지 않는다. `.env`, 키 파일, 실 API fixture와 원본 영상도 보호한다.
- 제품 유료 STT/vision 호출은 이번 코딩 모델 사용 승인과 별개다. 오프라인 구현/검증은 계속하고 실 API/FCP 인수는 별도로 미수행 표시한다.

## 분석 기준선 (2026-09-27 직접 측정)
- Python 2,658 tests, Swift 59 core + 1 extension tests 통과.
- ruff check/format 118 files, mypy 54 files, invariants 21 통과.
- `scripts/build_engine.py --verify` exit 1: engine manifest 없음. Resources/Engine에는 README만 있고 Resources/FFmpeg는 없다.
- 실제 FCP 왕복과 제품 유료 분석은 실행하지 않았다.

## 확인한 결함
1. `macos/scripts/vlogsrt-engine.py`: `_propagate_staged`가 workspace 생성보다 먼저 실행되어 media-grant.json이 있는 새 job에서 FileNotFoundError 재현.
2. 앱 CancellationStore는 job root에 기록, Python `_check_cancellation`은 job/workspace를 읽는다. 시작 뒤 취소는 초기 복사로 전달되지 않는다. 전사 조각/재시도 중 확인도 필요하다.
3. `main.swift`의 handleSharedFiles는 stage만 한다. activeOriginRevision은 읽기만 있고 쓰는 코드가 없다. 엔진 operation도 analyze/roughcut뿐이다.
4. API 키 저장 UI 없음. 번들 엔진/FFmpeg 실물 없음.
5. `normalize_luna_response`는 API 오류를 정상 keep과 같은 choice/confidence/evidence로 만든다. 상태 필드가 없어서 모든 비전 실패도 후보 0건 성공으로 보일 수 있다.
6. 승인형 분석과 legacy B-roll 압축은 분리돼 있다. `_internal_gaps`는 보호 구간이 둘 미만이면 후보가 없어 무발화 전체 클립과 앞뒤를 다루지 않는다.
7. 프레임별 ffmpeg select=eq(n,index)는 매번 처음부터 디코딩한다. 장편 성능 위험이다.
8. S2B-2b에서 실제 호출별 모델 미터링·내구 예약·재개 누적을 연결했다. 정확한 총비용 예측은 프로토타입 완료의 선행 조건이 아니다.
9. `_project_roughcut`이 XML 파일 저장만 하고 roughcut_imported로 전이한다.
10. 최종 자막 경고 전달, caption 추정만 하는 단계, 실패 재시도 상태가 부족하다.

## 2026-10-01 우선순위 조정 — 프로토타입 먼저
- 비용 예측의 정확도와 상세 비용 화면은 초기 프로토타입의 완료 조건에서 뺀다.
- 호출 전 예산 한도, 실제 사용량 기록, 불확실한 예약 보존은 유지한다. 이 안전장치는 실제 공급자를 쓸 때도 필요하다.
- 기존 앱 경로를 짧은 한 클립으로 먼저 검증한다: FCPXML 수신 → 사전 검사 → 간단한 예산 한도 승인 → 분석 → 후보 검토 → 러프컷 요청 → Final Cut 가져오기.
- 프로토타입은 우선 합성 FCPXML·가짜 SDK·가짜 가져오기 경계로 반복 검증한다. 이후 사용자가 선택한 짧은 프로젝트와 실제 Final Cut 가져오기는 별도의 수동 인수로 확인한다.
- 첫 세로 흐름의 완료 기준은 승인하지 않은 컷 0건, 요청한 컷의 프레임·길이 일치, 가져오기 성공·실패의 정확한 표시, 분석 재시도 뒤 결과 보존이다.
- 상세 비용 예측·분석별 비용 내역·예약액 화면은 프로토타입을 확인한 뒤 필요성을 다시 판단한다.

## 단계별 구현
### S1: 실행/취소/실패 연결 (첫 묶음)
- workspace 생성과 스테이징 순서 수정, 개발/번들 실행 경로 검증.
- 앱/엔진 공통 취소 경로, 각 외부 호출 직전 취소, 실행 중 취소가 이후 호출을 중단.
- 비전 success/error/skipped와 사유 보존. 정상 후보 없음과 전체/부분 실패 구분. 실패 구간은 보존.

### S2: 상태/비용/실행 UX
- 작업별 중복 실행 잠금, API 키 입력/교체, 사전 입력/도구/권한 검사, 자동 진행 갱신.
- 성공 결과만 재사용. 오류 캐시 재시도. 모델별 실제 사용량 기록, 호출 전 예산 한도, 재개 때 누적 유지.
- roughcut 생성/가져오기 요청/확인 구분. 실패 import는 재분석 없이 재시도.

#### S2B-3 프로토타입 우선 순서
- 3a. 한 클립을 반복 실행할 수 있도록 성공 캐시 재사용과 실패 분석 재시도를 완성한다. 성공 창·원장 기록은 재시도 뒤에도 보존한다.
- 3b. 러프컷 생성·Final Cut 가져오기 완료/실패를 실제 상태에 맞춰 표시한다. 가져오기가 끝나기 전에 완료 상태를 기록하지 않는다.
- 3c. 예산 한도와 실패 시 호출 차단은 유지한다. 상세 비용 예측과 비용 breakdown UI는 프로토타입 인수 뒤로 미룬다.
- 단계 3a·3b 인수는 짧은 합성 프로젝트의 전체 앱·엔진 경로 통과와 경계 실패 재시도로 한다. 실제 사용자 미디어/Final Cut 인수는 따로 표시한다.

### S3: 대화/B-roll 통합
- 승인형 Final Cut 분석을 주 경로로, 기존 보호/블록 로직을 재사용.
- 무음, NG/재촬영, B-roll 후보 종류. 초기 선택 해제.
- 정상 무발화와 분석 실패 구분. 무발화 클립 B-roll을 분석하고 실패 구간은 보존.
- 앞뒤 장면 맥락, 고유 사건/행동/불확실 보존. 기존 임계값은 근거 없이 변경하지 않는다.
- 후보 사유/시간/삭제 길이/전사/대표 이미지/playhead, 충돌 표시, B-roll 양쪽 삭제 묶음 승인.

### S4: 최종 SRT 완결
- 러프컷 원본 job과 결과 project UID 연결을 저장하고 공유 수신을 job별로 연결.
- WAV/FCPXML 수신 -> 기본 예산 한도 확인 -> 승인 -> caption 엔진 -> SRT/경고 UI. 상세 비용 예측은 프로토타입 뒤로 미룬다.
- 최종 편집 오디오를 사용, 해시/길이 검증, 실패 재시도.
- SRT 자동 import는 하지 않고 Relative to Timeline을 안내.

### S5: 성능/배포/문서
- 소스별 필요한 프레임을 모아 순차 디코딩으로 추출, 정확한 프레임/시간 대응 유지.
- 단계별 cache/checkpoint와 설정/입력 동일성에 근거한 재개.
- 엔진/FFmpeg 포함 앱 빌드 경로 완성 및 검증. 바이너리 공급/서명/실 FCP 검증은 없는 경우 정확히 외부 인수로 남긴다.
- README/보드/인수표 통일. 구현/자동 검증/실기 통과 분리.

## 프로토콜 및 인수
- Python/Swift JSONL 양쪽을 함께 변경: 결과 status/error/counts, 후보 종류/group, 진행/재개/import 상태, caption estimate/run operation.
- 구버전 읽기는 유지하되 불명확한 작업을 자동 재실행하지 않는다.
- 회귀: 실제 entrypoint 새 job, live cancel, 중복 요청, all/partial model error, import 실패 재시도, caption stage-to-result, 비용/재개.
- 편집: 승인하지 않은 삭제 0, 출력 길이 프레임 정확, 실패 구간 보존, 무발화 B-roll 후보, 그룹 승인/충돌.
- 실기 인수는 2분 -> 10~20분 -> 77분. 고유 발화/중요 사건 손실 0, 후보 채택률 각 유형 80% 이상 목표, SRT 처음/중간/끝 0.2초 이내. 이는 미달성 목표이며 테스트로 달성한 척하지 않는다.

## 보고 및 완료
- 사용자 추가 지침: GLM이 high-agency-coding 스킬을 직접 읽고 결과·범위·증거·검증·실패 대응 원칙을 적용한다. 기존 모델 역할은 유지한다.
- 오류와 개선사항은 `docs/reports/2026-09-27-high-agency-implementation-feedback.md`에 누적하고 Sol이 작업마다 갱신한다. 실제 적용 증거와 개선 전후 결과를 기록한다.
- 사용자 추가 지침: 해결되지 않는 오류는 발생 단계, 재현 명령, 예상/실제, 오류/exit와 핵심 로그, 시도한 수정/결과, 원인 가설, 영향 범위와 남은 막힘을 자세히 보고한다.
- 각 작은 작업 완료마다 증거와 다음 행동을 checkpoint에 저장한다. 실행 중인 turn/ask에는 compact하지 않는다.
- GLM은 정착 뒤 idle 상태에서 `/compact` 후 재사용할 수 있다. Sol도 각 작업 인수 완료 시 부모에게 checkpoint를 보고하고 정착한다. 부모가 idle Sol 터미널에서 `/compact`를 실행한 뒤 다음 작업을 새 Dispatch로 전달한다.
- 부모 대화 자체의 `/compact`는 자동 실행할 수 없으므로 필요한 시점에 사용자용 명령을 제공한다. compact를 실행하지 않았으면 실행했다고 주장하지 않는다.
- Sol은 단계별 brief, GLM 구현 SHA/파일, 직접 실행한 검사, 차이/수정 요청, 남은 외부 인수를 기록한다.
- 발견한 결함은 Sol이 GLM에 돌려보내 수정/재검증한다.
- 매 단계 통과 뒤 다음 단계로 계속 진행한다. 장기 작업은 context_compress 파일에 Run/Task/Dispatch와 다음 행동을 유지한다.
