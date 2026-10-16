# DONE — Luna selection → recommendations / CapCut track
갱신: 2026-10-07 | 구현·합성인수·운영적용 완료 | writer: Astra high requested

## 현재 운영
- http://127.0.0.1:7860 · PID10711 한 개 · health/config 정상 · 활성job0개.
- 사용자 명시: 미리보기 중단하고 지금 새 UI 적용. 이전job0003/4/5 cancelled, 원본/완료cache 보존.
- 종료한 encoder245/thumbnail6007은 parent83730·previews출력 검증. 이전server와임시7872 종료.
- 기존260810_wangbang 프로젝트와상태파일바이트보존. 새기동에유료분석자동시작없음.

## 구현
- 영상선택/FCP연결/미디어타일 → 준비 → 유효캐시재사용 또는 최초1회범위승인 → STT → Luna → 추천표시.
- selection_analysis.py: 선택범위·모델·ASR/Luna 요청상한을묶음. 실측초과시차단,선택세대/취소가드.
- understanding_luna.py: suitability/reason+프레임근거필수. scenery/action/무성도평가; 불확실유지.
- 창별부분결과 atomic파일 → 영상트랙 바로위 추천트랙에진행중표시.
- 좌원본미디어격자/중앙플레이어/우측이유/하단전체폭 ruler+추천+실제썸네일영상+오디오유무트랙.
- 마커클릭: 정확한원본시각seek/이유. API키는선택입력·메모리전용,승인뒤폼비움.
- 결과표시는추출을기다리지않음; FinalCut전달때현재검토구간추출.
- 기존캐시평가없는부분을추천으로꾸미지않음. 수동선택/편집 및원본참조 유지.

## 검증 (중복합산금지)
- Luna/분할/검토76pass, 최종영향373pass+옛함수개수기대1수정 뒤집중33pass34.46s.
- 선택6tests19.12s: 실제32초합성영상·fake STT/Responses, 부분결과/grade3종/cache추가0/단일승인/상한차단/새선택cancel/무성풍경.
- 실제브라우저: 영상선택→부분1마커→완료3마커, FCP2미디어→32초타일선택→15초마커seek/15~30이유.
- provider트레이스 gpt-6-luna7calls, 캐시타일선택뒤증가0. 실제유료전송0.
- 1440x900/scroll0 timeline y592~834, project패널접힘39.59px.
- 현장발견64731667/1000000 길이표시 Fraction수정, 회귀1pass1.74s.
- Ruff/format12files,mypy8source 통과(최종표시한줄compile/targeted검증).

## 자료
- 보고서: docs/reports/2026-09-27-high-agency-implementation-feedback.md 1060행부터.
- 화면: docs/reports/assets/2026-10-07-luna-workspace.png (합성패턴영상).
- 주요코드: ui/workspace_service.py, ui/ui_facade.py, raw/selection_analysis.py, raw/suitability.py, raw/understanding_luna.py.
- 공식모델확인: https://developers.openai.com/api/docs/models/gpt-6-luna (text/image, raw audio/video아님).

## 제약
- 실제API계정접근/유료실영상품질 미검증. 최종FCP앱import도별도.
- .env/.env.example/credential파일 미열람·미수정. 원본/기존dirty보존. 커밋/스테이징없음.
- 상태메타파일 /tmp/vlogsrt-native-ui-state.json; 프로젝트/job데이터는기존지속workspace.
