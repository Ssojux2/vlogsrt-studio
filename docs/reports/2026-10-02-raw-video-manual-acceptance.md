# 원본 영상 브라우저 v2 — A8 수동 인수 초안 (2026-10-02)

상태: **초안**(2026-10-03 개정 — main 3cbf235 기준). 계획 §16 A8·§17·§17.1의
수동 인수 표를 채우는 문서다. 완료 표시는 루트의 독립 인수만 한다 — 이 초안의
어떤 항목도 사람 실기 확인을 완료로 표기하지 않는다. 실행 가능한 절차는
`docs/raw-video-workflow.md`에 있다.

증거 종류 구분:

- `PASS(자동)` — 승인 커밋의 시험이나 루트 탐침(합성 미디어)이 증명.
- `PASS(자동 브라우저)` — 루트가 합성·가드 루프백 서버에서 **실제 브라우저**로
  잰 자동 실측. 사람 실기와 다르고, 사람 항목을 대신하지 않는다.
- `PENDING(통합)` — 코드는 있으나 서비스·UI·CLI가 아직 열지 않음.
- `PENDING_HUMAN` — 사람 실기 필요. `NOT_RUN` — 실행하지 않았다.

## 기준선 (2026-10-03 개정, main 3cbf235)

- main **f939478** 루트 전체 점검: **3746 passed**, all21 invariants·Ruff·
  format·mypy PASS(루트 기록, 역사 재인용).
- **1b7283d**: A4(f431d4a+49bd624)·A7-3 검토(13ad043+0180354) 승인·병합 —
  루트 실측 스테레오 오프셋·NTSC 무성, 2단계 승인 게이트 탐침 통과.
- **cd16d80**: 캡션 작업공간 백엔드 c6a0f6b+eff0f9e 승인·병합 — 루트 30 시험 +
  실측 탐침(두 오디오 트랙·수동 확정·재시작·불변성·오프라인 0송신) 통과.
- **3cbf235**: 검토 작업공간 백엔드 313f24d+648b5d2 승인·병합 — 루트 16 시험 +
  실측 탐침(SQLite 크래시 롤백·재시도, 실제 분할→재계획→재추출→VTT→선택·전체
  확정→정리→재오픈) 통과.
- 이 문서 개정 중 재확인: `pytest tests/raw` → main 3cbf235에서 **841 passed**
  (합성 미디어·오프라인, exit 0). 역사 수치 702(f939478)·805(1b7283d+검토
  백엔드)는 각 시점 기록.
- 네 화면 통합 후보(W1, `Ssojux2/raw-a2-ingest`)은 b910fcc → F1 72d98bf →
  F2 **e47ae95**까지 갱신됐고 **승인 전**이다. 루트가 F1·F2에서 실측 브라우저를
  돌렸다 — 검토·내보내기 흐름, 자막 백엔드 흐름, 자막 화면 표현까지 부분
  통과. 이벤트 결함 2건으로 전체 판정은 보류(아래 1-9·1-10). 증거:
  `/tmp/vlogsrt-raw-A8-browser-{review,caption-final}.png`,
  `/tmp/vlogsrt-raw-A8-F2-caption-player.png`,
  `/tmp/vlogsrt-raw-A8-browser-server.log`.

## 기계 현재 상태 — AI Rough Cut 플러그인 비활성

사용자 요청에 따라 **루트가** 조치한 상태다. **재활성화·재설치하지 않는다.**

- `pluginkit ignore com.ssojux2.vlogsrt.workflow` 적용됨.
- 등록 복사본 다섯 벌 unregistered — 이후 `pluginkit` 조회 일치 0건(match0).
- 호스트 앱 `/Applications/FinalCutAgent.app`은 **루트가 사용자 지시에 따라**
  휴지통으로 옮겼다(`FinalCutAgent-disabled-20261002-220737.app`, 2026-10-02
  22:07) — 영구 삭제가 아니라 휴지통에 보존된 비활성 상태다.
- **재설치·재활성화하지 않는다.** 실행 중인 Final Cut Pro는 재시작되지 않았다 —
  재시작 후 비활성 확인: PENDING_HUMAN.

## A8 체크리스트

### 1. 네 화면 (all4screens)

루트 A3·A4 실측 브라우저 증거는 **자동 브라우저 실측**이다(합성 미디어·가드
루프백 서버·불투명 미디어 ID). 사람 실기 항목을 대신하지 않는다.

| # | 항목 | 방법 | 기대 | 상태 |
|---|---|---|---|---|
| 1-1 | 화면 1 — 작업공간 만들기·열기 | `make ui` → 화면 1 | 9디렉터리 작업공간 생성, 새로고침·재시작 후 상태 유지 | PASS(자동: test_workspace_view_flow) · PASS(자동 브라우저: 루트 A3 — 생성·열기, 네 탭 렌더, 오류 0) |
| 1-2 | 화면 1 — 조사→순서 확인→복사·검증 | 합성 카드 폴더 조사 | `--confirm` 없으면 쓰기 없음, 복사 후 SHA 일치 | PASS(자동: test_ingest_ui·test_ingest_scan) · PASS(자동 브라우저: 루트 A3 — 조사·유효 creation_time·확정+복사+전체 순서) |
| 1-3 | 화면 1 — 전체 원본 순서 확정 | 버튼(제안 그대로 또는 ID 나열) | 확정 리비전 생성, 재확인으로 덮어쓰지 않음 | PASS(자동: test_ingest_global_order) · PASS(자동 브라우저: 루트 A3 — 전체 순서 확정 후 계획으로 진행) |
| 1-4 | 화면 2 — 결정적 계획·추출·재시도 | 확정 순서 위 계획 → 추출 | 전체 덮개, 중간 실패 재개 시 기존 번호·파일 보존 | PASS(자동: test_segmentation·test_segment_runner) · PASS(자동 브라우저: 루트 A3 — 전체 덮개 계획, 조각 2개 시간순 게시+전달물) |
| 1-5 | 화면 2 — 조각 재생 | 조각 선택 → ID 기반 스트리밍 | 원본 루트 밖 경로·비밀 파일 거부, Range 206 | PASS(자동: test_media_access·test_access_policy) · PASS(자동 브라우저: 루트 A3 — 등록 ID 재생 duration 3.4·readyState 4, 1초 탐색 완료) |
| 1-6 | 화면 2 — 의미 분석(재생) | 분석 패널 견적 → 재생 분석 | 견적 인쇄, 재생 2/2 상태·태그, 의미 계획 생성 | PASS(자동 브라우저: 루트 A4 — 견적·재생 2/2(speech·noaudio)·새 의미 계획, 오류 0) |
| 1-7 | 화면 2 — 같은 형상 새 리비전 추출 | 의미 계획 → 추출 | 새 리비전은 새 package에서 뽑고 옛 package 불변 | PASS(자동 브라우저: 루트 A8 실측 72d98bf — 분할 1.6초·메모·제외→새 계획→package_000002 조각 3개 실추출, 오류 없음. A4 때의 "이미 완료된 package" 오류는 재현되지 않았다) |
| 1-8 | 화면 3 — 편집용 폴더 확정·내보내기 | package 상태 → 확정 내보내기·Finder·캐시 정리 | 선택·전체 내보내기, 읽기전용 동결, 보호 거부 | PASS(자동 브라우저·후보 72d98bf: 전체 3·선택 2 확정 내보내기, 정리 뒤 재검증 통과) · 통합 승인은 1-10과 묶여 보류 |
| 1-9 | 화면 4 — 최종 자막 전 과정 | 등록→추정→승인 실행→검토→편집→확정 | 승인 없이 유료 호출 없음, 확정 SRT/VTT 등록 제공 | **NOT PASS(통합)** — 백엔드 흐름은 루트 실측 통과(무성 상태 구분, 멀티트랙 명시 선택, ordinal1 교정 문구=공유 WAV 해시·샘플 경계 정확(가짜 호출 timing 1·text 1), 수동 문구 저장, 최종 SRT/VTT GET 200·Range 206, 임의 `file=` 403, 원본 무변경, dotenv 이벤트 0). 표현 3건(재생 화면·파형·수동 문구)도 F2 e47ae95에서 루트 실측 통과(readyState 4·aria-label 파형·큐 선택 실제 탐색 1.57675초·선택 오디오 미리듣기가 고른 트랙과 일치: 시작 1.076780초·44100Hz). 남은 이벤트 결함 2건: ▶·반복·처음 단추 클릭 무반응(즉시호출 괄호 누락), 검토 새로고침 뒤 큐 드롭다운 변경 콜백 오류 — W1 F3 수정 후 재실측 |
| 1-10 | 네 화면 최종 실측(전체 흐름) | 루트 실측 브라우저 | 전 흐름 오류 0, 증거 기록 | PENDING — 이벤트 결함 2건(W1 F3)이 닫힌 뒤 루트가 다시 잰다. 그 기록이 오면 이 칸을 채운다 |

화면 3·화면 4는 승인 기준선(main 3cbf235)에서 읽기 전용 상태이며 버튼이
비활성이다(정직한 비활성). 후보(F2 e47ae95)가 이 버튼들을 연다 — 승인 전이다.

### 2. 불변 복사·순서·추출·재개 (immutable copy / order / extract / resume)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 원본은 복사 후에도 무변경(바이트·해시) | test_ingest_copy | PASS(자동) |
| 같은 내용 재가져오기 — 덮어쓰기 없음, 중복·출처 구별 | test_ingest_scan | PASS(자동) |
| 복사 중 실패(카드 분리 등) → READY 오판정 없음, 재개 후 해시 일치 | test_ingest_copy·test_jobs | PASS(자동) |
| 원본 순서 — 동일 촬영시각·미조사·번호 역전의 결정적 정렬 | test_source_order·test_ingest_creation_time | PASS(자동) |
| 한국어·공백·Unicode 경로, 심볼릭 링크·루프 이탈 차단 | test_ingest_scan | PASS(자동) |
| 추출 재개 — 누락 성공 없음, 기존 번호·파일 보존 | test_segment_runner | PASS(자동) |
| 검토 출판 원자성 — 크래시·동시 편집·CAS·프로세스 즉사에 고아 리비전 없음, 재시도 안 막힘 | test_review_workspace(출판 원자성 6건) | PASS(자동 — 313f24d+648b5d2, 루트 16 시험·실측 탐침 동일) |
| 확정 내보내기 — 실제 바이트 복사(하드링크 아님), 게시 후 옛 package 무변경 | test_confirmed_package·test_review_workspace 종단 | PASS(자동) · Finder 실사용 PENDING(통합) |
| 실물 SD 카드에서 같은 흐름 | — | PENDING_HUMAN |

### 3. A/V 오프셋·무오디오·꼬리 (A/V offset / noaudio / tail)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 프레임/초 — 24/25/30, 30000/1001, nonzero PTS 무단 변환 없음 | test_native_media_probe·test_native_media_extract | PASS(자동) |
| 마지막 조각의 원본 오디오 꼬리 보존(구간별 길이, 마지막 조각만 소스 끝까지) | test_native_media_extract | PASS(자동) |
| 무음 합성 미디어(anullsrc) 추출·검증 | test_ingest_commands·test_native_media_extract | PASS(자동) |
| 최종 영상 오디오 — 청크·샘플 범위 증명, 오프라인 설정의 실전송 거부 | test_final_video·test_final_caption_run·test_caption_provider | PASS(자동) |
| 두 오디오 트랙 실측 — 캡션 작업공간 등록·실행·확정 | 루트 실측 탐침(캡션 백엔드 cd16d80 승인 기록) | PASS(자동 — 실측 합성 2트랙) |
| 늦게 시작하는 오디오·압축 지연 실측(개인 원본) | — | PENDING_HUMAN(개인 원본 필요) |
| 최종 영상 기준 A/V 싱크(시작·중간·끝) | — | PENDING(통합 후) PENDING_HUMAN |

### 4. 캐시 보호 (cache protection)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 정리 대상은 `cache/`·`previews/` 내용물뿐 — originals·clips·final·subtitles·review·analysis·jobs·config·project DB·`confirmed_*` 거부(잘못 붙은 이름도) | test_cache_policy | PASS(자동) |
| 심볼릭 링크는 링크만 지우고 대상 무사 | test_cache_policy | PASS(자동) |
| 확정 미디어는 캐시 정리·작업공간 이동 뒤에도 매니페스트 재검증 통과 | test_confirmed_package | PASS(자동) |
| 화면/CLI의 캐시 정리 단축·경고 문구 | 루트 실측(72d98bf — 정리 후 확정 재검증) | PASS(자동 브라우저·후보 72d98bf) · 통합 승인은 1-10과 묶여 보류 |

### 5. 명시적 최종 자막·수동 확인 (explicit final caption / manual confirmation)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 최종 자막은 문구/시간 모델·추정 비용·승인 게이트를 명시적으로 거친다 — 승인 없이 유료 호출 없음 | test_caption_provider·test_final_caption_run | PASS(자동) |
| 캡션 작업공간 — 등록→추정→승인 실행→검토·수동 편집→수동 확정, 재시작 뒤 불변, 오프라인 0송신 | 루트 30 시험 + 실측 탐침(cd16d80) | PASS(자동 — 백엔드) |
| 미완료·0바이트·오류 결과는 성공으로 게시되지 않음 | test_final_artifacts | PASS(자동) |
| 초안/확정 SRT·수동 patch·리비전 분리 | test_final_artifacts | PASS(자동) |
| 실 최종 영상 등록→승인→생성→검토→내보내기 실기 흐름(화면) | 루트 실측(72d98bf — 백엔드 흐름, 가짜 제공자) | 부분 통과 · 표현 결함 3건(1-9)으로 NOT PASS(통합: W1 F2 대기) · 유료 경로 NOT_RUN |

### 6. 사람 전용 항목 — 개인 미디어·실기

| 항목 | 상태 |
|---|---|
| 개인 SD 카드·개인 원본 영상 실사용 | NOT_RUN |
| HDR·Log·4K 10-bit HEVC 색 보존 실측 | NOT_RUN |
| Final Cut 실제 가져오기·파일명 순서·재생 확인 | PENDING_HUMAN |
| 제품 API 유료 호출·계정 접근·모델 품질 비교(옛것·새것) | NOT_RUN |
| AI Rough Cut 재활성화 | **하지 않는다**(사용자 요청으로 비활성 유지) |

## 판정 규칙

- 각 `PENDING_HUMAN`·`NOT_RUN`은 사람이 실행해 결과를 이 표에 되돌린 뒤에만
  완료로 센다. `PENDING(통합)`은 W1 통합 커밋의 루트 인수 뒤 이 표를 다시 연다.
- `PASS(자동 브라우저)`는 루트가 잰 자동 실측이다 — 사람 실기 항목을 대신하지
  않으며, 최종 전 흐름 실측(1-10)은 별도로 기록된다.
- 후보(지금 F2 e47ae95)의 상태는 루트의 최종 전 흐름 실측이 도착하기 전까지
  '후보(승인 전)'로 유지한다 — 작업자·후보 문서의 서술로 완료 표시를 만들지
  않는다.
- 유료 호출이 필요한 항목은 별도 비용 승인 전에 NOT_RUN을 유지한다.
- 루트 독립 인수가 최종 판정이다 — 작업자 보고는 보조 자료다.
