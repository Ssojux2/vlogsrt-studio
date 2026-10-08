# context_compress: frame-background-overlay
갱신: 2026-10-08 | 진행: 1/3 인수 완료, 후속 사용자 요구 구현 중

## 완료
- [x] 1. 프레임 단일 디코딩/원자적 JPEG 게시/성공 캐시/PIPE 배출/취소 개선.
- 실제0009:40프레임89.63초, 단일ffmpeg, 기존JPEG18/18바이트일치. 원본/Movies캐시 수정 없음.
- 표적173passed, 취소→재개 포함7passed(4.97s). STT1 유지/Luna0→1, 완료프레임 재사용.
- 진행표시1초queue=False로 무거운 작업대 렌더와 분리.
- files: preprocess.py, raw/media_refs.py, raw/understanding.py, ui/workspace_service.py, tests/raw/test_frame_batch.py.

## 다음 행동
1. 선택과 작업수명 분리: 원본A승인분석 중 B선택해도 A계속; per-source 지속상태/캐시.
2. 명시취소는 해당 선택작업만, 예전콜백이 새선택화면을 덮지 않도록 view/token분리.
3. 영상위 원본자막 overlay(재생/정지/seek/source변경/편집 즉시반영)와 테스트.

## 핵심 사실
- 현재 _start_quick가 _cancel_quick 호출, _quick_assert_current가 view변경을 취소로 처리하는 것이 새요구의 결함.
- _quick 단일상태와 meta selection_analysis_state를 원본별 task상태+현재view로 분리해야 함.
- 기존 publish_selection_review는 전체plan 재초기화하므로 background 완료가 다른source판정을 지우지 않게 조정필요.
- UI video element는별도 shell, 원본time=video.currentTime-preview_origin. ui_facade.py VISUAL_BOOTSTRAP_JS.
- 단일Astra writer. HighAgency활성화 요청, trustedguard UNVERIFIED. 추가agents 없음.
- 사용자 추가요구로 scope drift 코드/테스트≤16파일·≤4모듈(최초≤10파일/≤4모듈).

## 실제 사용자 작업/제한
- 일반앱7860, Movies/test_dir/260810_wangbang-vlogsrt,0009, anal_beee036b8b007b37.
- 최근 old앱21/40/STT1성공/Luna0; 실제live진행은그뒤변할수있음. 새패치미적용.
- sandbox workspace-write/network-restricted/approval-never. 프로세스/브라우저제어불가, 우회안함.
- Movies프로젝트는읽기전용. 사용자가 단순다른영상선택하려고 취소하도록 안내하면안됨.
- 이미 승인한분석 계속, 새원본자동유료호출없음. 실제추가API호출을 하지않음.
