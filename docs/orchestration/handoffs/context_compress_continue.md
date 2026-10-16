# context_compress: continue
갱신: 2026-10-08 | 계획: Final Cut 라이브러리 → 분석 → 클립 선택 → Final Cut 출력 | 진행: 구현 중, 재개 지점 저장

## 완료
- [x] 4K 프레임 단일 패스 추출 — 실제 DJI_20260811125157_0009_D.mov 40장 89.63초; 기존 JPEG 18/18 바이트 일치.
- [x] 원본별 백그라운드 작업 — A→B 선택 중 A 계속 실행, FIFO 일괄 승인, 선택 작업만 취소, 완료 결과·수동 제외 보존.
- [x] 영상 위 원본 자막 — 재생·정지·탐색·속도 변경·원본 전환·문구 수정·native cue/DOM fallback 테스트 연결.
- [x] 주 흐름 UI — Final Cut 라이브러리 불러오기 → 한 번 승인해 분석 → 사용/제외 검토 → Final Cut 출력 중심으로 축소; 영상·자막 영역을 크게 조정.
- [x] `wait_child_job` — `check_cancel`만 있는 경량 컨텍스트에서도 하위 작업 취소가 즉시 전달되도록 보완.
- [x] 마지막 관련 테스트 16개 통과: frame batch, background library, source overlay, FCP library/export.

## 다음 행동 (재개 시 이 순서)
1. `.venv/bin/pytest -q tests/raw/test_frame_batch.py tests/raw/test_background_library.py tests/raw/test_source_overlay.py tests/raw/test_capcut_library.py tests/raw/test_capcut_export.py` 재실행.
2. 취소 전파·FIFO 대기·A→B→A 복귀·자막 overlay·사용/제외 Final Cut 출력 영향 범위 테스트를 실행하고 실패가 있으면 원인만 수정한다.
3. `ruff`와 source-command invariant를 다시 실행한다.
4. 가능하면 실제 앱을 재시작해 새 UI를 확인한다. 현재 앱/Movies 작업공간은 권한상 자동 재시작하지 않았고, 기존 `0009` 작업은 중단 상태(STT 성공 1, Luna 0)로 기록되어 있다.

## 핵심 파일
- `vlogsrt/preprocess.py` — 단일 패스 ffmpeg 프레임 추출, 원자적 게시, 취소.
- `vlogsrt/project/raw/service.py` — 원본별 작업 큐, 하위 작업 취소 전파, 라이브러리·출력 서비스.
- `vlogsrt/project/raw/selection_analysis.py` — 선택과 분석 작업 수명 분리.
- `ui/workspace_service.py` — 4단계 주 화면, 백그라운드 상태·분석 승인·출력 배선.
- `ui/ui_facade.py` — 큰 영상 플레이어, 재생 동기화 자막, 타임라인·결과 표시 CSS/JS.
- `tests/raw/test_background_library.py`, `tests/raw/test_source_overlay.py`, `tests/raw/test_frame_batch.py` — 후속 요구 회귀.

## 미해결 / 위험
- 마지막 전체 영향 범위 실행 전 에이전트 사용량 제한으로 중단됨; 표적 16개 통과만 확인됨.
- 실제 앱의 기존 프로세스는 새 코드로 교체되지 않았을 수 있다. 기존 터미널에서 `Ctrl+C` 후 `/Users/ssojux2/Working/vlogsrt/실행.command` 재실행이 필요하다.
- 실제 라이브러리 분석·Final Cut 왕복은 사용자 승인과 실행 환경에 의존한다. 원본·`.env`·키 파일은 수정하지 않는다.
- 이 파일과 현재 작업 트리의 변경은 커밋·스테이징하지 않았다.
