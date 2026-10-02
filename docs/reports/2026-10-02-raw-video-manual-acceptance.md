# 원본 영상 브라우저 v2 — A8 수동 인수 초안 (2026-10-02)

상태: **초안.** 계획 §16 A8·§17·§17.1의 수동 인수 표를 마는 문서다. 완료 표시는
루트의 독립 인수만 한다 — 이 초안의 어떤 항목도 사람 실기 확인을 완료로 표기하지
않는다. 실행 가능한 절차는 `docs/raw-video-workflow.md`에 있다.

## 기준선 (2026-10-02, 개정: main 1b7283d)

- main **f939478**에서 루트 전체 점검: **3746 passed**, all21 invariants·Ruff·
  format·mypy PASS (루트 기록, 이 문서는 재인용 — 역사 기록).
- 같은 날 루트 승인·병합: **A4(f431d4a)·A7-3(13ad043) → main 1b7283d**
  (루트 재현 탐침 통과 — 실측 스테레오 오프셋·NTSC 무성, 2단계 승인 게이트,
  검토 리비전). 화면 4는 여전히 읽기 전용이므로 아래 A7 항목의 **UI 통합은
  PENDING** 그대로다.
- 이 문서 작성 중 재확인: `pytest tests/raw` → f939478에서 **702 passed**, 개정
  시점(1b7283d + 검토 백엔드)에는 **805 passed** (합성 미디어·오프라인, exit 0).
- 화면 3(편집용 폴더)·화면 4(최종 자막)는 읽기 전용 상태이며 버튼이 비활성이다.

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

상태 표기: `PASS(자동)` = 이 커밋의 시험이 증명. `PENDING_HUMAN` = 사람 실기
필요. `PENDING(통합)` = 코드는 있으나 서비스·UI·CLI가 아직 열지 않음. `NOT_RUN` =
실행하지 않았다.

### 1. 네 화면 (all4screens)

| # | 항목 | 방법 | 기대 | 상태 |
|---|---|---|---|---|
| 1-1 | 화면 1 — 작업공간 만들기·열기 | `make ui` → 화면 1 | 9디렉터리 작업공간 생성, 새로고침·재시작 후 상태 유지 | PASS(자동: test_workspace_view_flow) · 실브라우저 PENDING_HUMAN |
| 1-2 | 화면 1 — 조사→순서 확인→복사·검증 | 합성 카드 폴더 조사 | `--confirm` 없으면 쓰기 없음, 복사 후 SHA 일치 | PASS(자동: test_ingest_ui) · 실브라우저 PENDING_HUMAN |
| 1-3 | 화면 1 — 전체 원본 순서 확정 | 버튼(제안 그대로 또는 ID 나열) | 확정 리비전 생성, 재확인으로 덮어쓰지 않음 | PASS(자동: test_ingest_global_order) |
| 1-4 | 화면 2 — 결정적 계획·추출·재시도 | 확정 순서 위 계획 → 추출 | 전체 덮개, 중간 실패 재개 시 기존 번호·파일 보존 | PASS(자동: test_segmentation·test_segment_runner) |
| 1-5 | 화면 2 — 조각 재생 | 조각 선택 → ID 기반 스트리밍 | 원본 루트 밖 경로·비밀 파일 거부, Range 206 | PASS(자동: test_media_access·test_access_policy) · 실브라우저 PENDING_HUMAN |
| 1-6 | 화면 3 — 편집용 폴더 | package 상태 새로고침 | 경로 표시·복사만. 확정·선택본 내보내기 버튼 없음(정직한 비활성) | PENDING(통합: A6 라이브러리는 시험 통과) |
| 1-7 | 화면 4 — 최종 자막 | 탭 열기 | 설정 요약만, `정밀 자막 생성`·`SRT 내보내기` 비활성 | PENDING(통합: A7-3 13ad043은 1b7283d에 승인·병합 — W1 UI 연결 대기) |

### 2. 불변 복사·순서·추출·재개 (immutable copy / order / extract / resume)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 원본은 복사 후에도 무변경(바이트·해시) | test_ingest_copy | PASS(자동) |
| 같은 내용 재가져오기 — 덮어쓰기 없음, 중복·출처 구별 | test_ingest_scan | PASS(자동) |
| 복사 중 실패(카드 분리 등) → READY 오판정 없음, 재개 후 해시 일치 | test_ingest_copy·test_jobs | PASS(자동) |
| 원본 순서 — 동일 촬영시각·미조사·번호 역전의 결정적 정렬 | test_source_order·test_ingest_creation_time | PASS(자동) |
| 한국어·공백·Unicode 경로, 심볼릭 링크·루프 이탈 차단 | test_ingest_scan | PASS(자동) |
| 추출 재개 — 누락 성공 없음, 기존 번호·파일 보존 | test_segment_runner | PASS(자동) |
| 확정 내보내기 — 실제 바이트 복사(하드링크 아님), 게시 후 옛 package 무변경 | test_confirmed_package | PASS(자동) · Finder 실사용 PENDING(통합) |
| 실물 SD 카드에서 같은 흐름 | — | PENDING_HUMAN |

### 3. A/V 오프셋·무오디오·꼬리 (A/V offset / noaudio / tail)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 프레임/초 — 24/25/30, 30000/1001, nonzero PTS 무단 변환 없음 | test_native_media_probe·test_native_media_extract | PASS(자동) |
| 마지막 조각의 원본 오디오 꼬리 보존(구간별 길이, 마지막 조각만 소스 끝까지) | test_native_media_extract | PASS(자동) |
| 무음 합성 미디어(anullsrc) 추출·검증 | test_ingest_commands·test_native_media_extract | PASS(자동) |
| 최종 영상 오디오 — 청크·샘플 범위 증명, 오프라인 설정의 실전송 거부 | test_final_video·test_final_caption_run·test_caption_provider | PASS(자동) |
| 늦게 시작하는 오디오·압축 지연·여러 오디오 트랙 실측 | — | PENDING_HUMAN(개인 원본 필요) |
| 최종 영상 기준 A/V 싱크(시작·중간·끝) | — | PENDING(A7 통합 후) PENDING_HUMAN |

### 4. 캐시 보호 (cache protection)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 정리 대상은 `cache/`·`previews/` 내용물뿐 — originals·clips·final·subtitles·review·analysis·jobs·config·project DB·`confirmed_*` 거부(잘못 붙은 이름도) | test_cache_policy | PASS(자동) |
| 심볼릭 링크는 링크만 지우고 대상 무사 | test_cache_policy | PASS(자동) |
| 확정 미디어는 캐시 정리·작업공간 이동 뒤에도 매니페스트 재검증 통과 | test_confirmed_package | PASS(자동) |
| 화면/CLI의 캐시 정리 단축·경고 문구 | — | PENDING(통합: W1) |

### 5. 명시적 최종 자막·수동 확인 (explicit final caption / manual confirmation)

| 항목 | 자동 근거 | 상태 |
|---|---|---|
| 최종 자막은 문구/시간 모델·추정 비용·승인 게이트를 명시적으로 거친다 — 승인 없이 유료 호출 없음 | test_caption_provider·test_final_caption_run | PASS(자동) |
| 미완료·0바이트·오류 결과는 성공으로 게시되지 않음 | test_final_artifacts | PASS(자동) |
| 초안/확정 SRT·수동 patch·리비전 분리 | test_final_artifacts | PASS(자동) |
| 실 최종 영상 등록→승인→생성→검토→내보내기 실기 흐름 | — | PENDING(A7 통합 후) · 유료 경로 NOT_RUN |

### 6. 사람 전용 항목 — 개인 미디어·실기

| 항목 | 상태 |
|---|---|
| 개인 SD 카드·개인 원본 영상 실사용 | NOT_RUN |
| HDR·Log·4K 10-bit HEVC 색 보존 실측 | NOT_RUN |
| Final Cut 실제 가져오기·파일명 순서·재생 확인 | PENDING_HUMAN |
| 제품 API 유료 호출·계정 접근 | NOT_RUN |
| AI Rough Cut 재활성화 | **하지 않는다**(사용자 요청으로 비활성 유지) |

## 판정 규칙

- 각 `PENDING_HUMAN`·`NOT_RUN`은 사람이 실행해 결과를 이 표에 되메긴 뒤에만
  완료로 센다. `PENDING(통합)`은 W1/W2 통합 커밋 후 이 표를 다시 연다.
- 유료 호출이 필요한 항목은 별도 비용 승인 전에 NOT_RUN을 유지한다.
- 루트 독립 인수가 최종 판정이다 — 작업자 보고는 보조 자료다.
