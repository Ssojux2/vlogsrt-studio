# UI 통합 작업 인수 정리 (2026-11-16)

기준 커밋: `fef5ef2` (`feat/folder-workbench`, PR #4 머지 직후). 이 문서는 통합 작업을 시작하는 세션이
지금 화면이 어떻게 생겼는지, 무엇이 겹치는지, 무엇이 남았는지 한 번에 보도록 정리한 것이다.
설계 결정은 하지 않는다 — 결정은 통합 세션이 사용자와 정하고 새 D 번호로 남긴다.

짝 문서: `docs/plans/2026-11-16-ui-integration-checklist.md` — 통합이 **끝난 뒤** 확인할 목록(깨지기 쉬운 장치 ·
오프라인 앱 확인 · 사람 확인 · 성능 Phase 4 남은 일). 이 문서는 통합을 **시작할 때** 읽는다. 그 사이 base에 W-81(D141 —
Gemini 429 대기 후 재시도)·W-82(D142 — 의미 분석 창 동시 전송)가 더해졌고, 화면 구성은 그대로다. 다음 번호는 W-83 · D143.

용어 고정(이 문서 전체)

| 용어 | 뜻 |
|---|---|
| 원본 범위 타임라인 | D125–D138(W-71a–i). 원본 프레임 구간 클립 + 원본 밀리초 자막. 렌더 전 편집 |
| 초안 편집 | D117(W-66). 렌더한 `draft.mp4` 위의 컷·자막 편집. 「최종 편집」 탭 아래쪽 칸 |
| 「최종 편집」 탭 | 위 둘이 함께 들어 있는 탭 하나(`ui/final_edit_ui.py`의 `build_tab`) |

---

## 1. 지금 화면 구성

□ 탭은 셋이고 보이는 것은 둘임
  ○ 「영상 정리」 — `workspace_service._review_tab`
  ○ 「최종 편집」 — `final_edit_ui.FinalEditCell.build_tab(header=_final_edit_header)`
  ○ 「자막」 — `workspace_service._caption_tab`, `visible=False`. 옛 자막 흐름 그대로 숨겨 둔 탭

□ 「영상 정리」 탭 (위 → 아래)
  ○ 명령 줄 `#vl-command-bar`: 「폴더 불러오기」 · 승인 · 취소 둘 · 「추천만 이어붙여 내보내기」 · 「타임라인으로 보내기」 · 활동 칸
  ○ 무대 `#visual-stage`: 목록·재생기(`#visual-player-block`) · 작업대 본문(`#visual-workbench-block`)
  ○ 오른쪽 칸 `#vl-quote`: 승인 범위 · 교차 확인 · 초안 결과 카드 · 「초안 폴더 열기」 · 「SRT만 다시 내보내기」 · 「최종 편집 열기」
  ○ 같은 오른쪽 칸 아래: 「영어 SRT로 변환」(D114) · 자막 입히기(`ui/burn_cell.py`, D116·D118)
  ○ 서랍: 분석 설정 · 추천 기준 · 고급 Final Cut · 맥락 평가 · 용어집 · 복원·고급 조작

□ 「최종 편집」 탭 (위 → 아래)
  ○ 타임라인 요약 — `ui/timeline_cell.py` `build_summary` (W-71b)
  ○ 재생기 + 이동 막대 + 「이 타임라인으로 렌더」 — `ui/timeline_player.py` (W-71c·e·f)
  ○ 원본 띠 + 「원본 넣기」 — 같은 파일 (W-71d·g)
  ○ 자막 칸(목록 + 편집 칸) — 같은 파일 `_cue_panel_html` (W-71i)
  ○ 초안 편집 블록 — `final_edit_ui.final_edit_html` + `FINAL_EDIT_JS` (D117)

□ 탭 사이 이동
  ○ 초안 내보내기·타임라인 렌더 성공 → 「최종 편집」 자동 전환 (`FinalEditCell.switch_tick`)
  ○ 「타임라인으로 보내기」 성공 → 「최종 편집」 전환 (`TimelineCell`)
  ○ 「최종 편집」이 앞이면 화면 타이머 셋(1초·2초·2초)을 쉰다 (D134, `_wire_timer_rest`)
  ○ 타임라인 렌더가 끝나면 재생기가 `tl_job`으로 보고 초안 편집 칸의 숨은 「다시 열기」를 누름 (W-71h)

---

## 2. 편집기가 둘임 — 통합의 중심

| | 원본 범위 타임라인 | 초안 편집 |
|---|---|---|
| 시간 기준 | 원본 프레임(클립) · 원본 밀리초(자막) | 초안 프레임(클립) · 초안 밀리초(자막) |
| 문서 | `timeline/<시각>/timeline.json` | `export/<시각>/edit/final-edit.json` + `final.srt` |
| 순수 모델 | `raw/timeline_edit.py` | `raw/final_edit.py` |
| 입출력 | `raw/timeline_edit_io.py` | `raw/final_edit_io.py` |
| 브라우저 핵심 | `ui/timeline_edit_js.py` (`__vlTL`, 노드 시험) | `ui/final_edit_js.py` (`FE`) |
| 화면 JS | `ui/timeline_player_js.py` (1224줄) | `ui/final_edit_app_js.py` (1275줄) |
| 서버 함수 | `tl_media` `tl_prepare` `tl_save` `tl_render` `tl_tiers` `tl_add_source` `tl_job` | `fe_load` `fe_save` `fe_waveform` |
| 출력 | 원본에서 한 번 렌더 → `export/<시각>/` (D130) | 영상 안 만듦. 번인이 입력으로 씀 (D118) |

□ 흐름이 두 갈래임

```
영상 정리 ─┬─ 추천만 이어붙여 내보내기 ──────────────→ export/<시각>/draft.mp4 ─→ 초안 편집 ─→ 자막 입히기
           └─ 타임라인으로 보내기 → 원본 범위 타임라인 ─(이 타임라인으로 렌더)─┘
```

□ 겹치는 것
  ○ 자막 연산이 두 벌임: 나누기·합치기·지우기·새 자막·시작/끝을 플레이헤드로
    - 나누기 글 규칙이 두 곳: `FE.splitText`(`final_edit_js.py:383`) · `TL.splitText`(`timeline_edit_js.py:563`)
  ○ 키 글자가 같음: `[` `]` ⇧B ⇧M ⌥C ⌥↑↓ Enter — 정본은 `final_edit_ui.KEYMAP`
    - 두 키 처리기가 한 탭에 있어 초점이 어느 쪽에 있느냐로 갈림 (W-71i에서 실제로 꼬였다)
  ○ 재생기가 둘임: 원본 미리보기 바꿔 틀기(D128) · `draft.mp4` 하나
  ○ 클립 편집이 두 벌임: 자르기·리플 지우기·트림 (W-71f 대 D117)

□ D130의 결정이 아직 살아 있음
  ○ 「D117 초안 편집은 남긴다 — 타임라인은 렌더 전 편집, D117은 렌더한 초안 위의 다듬기」
  ○ 초안 편집을 없애거나 합치려면 사용자 승인과 새 D 번호 필요

---

## 3. 통합할 때 지킬 선

□ 두 문서의 시간 기준을 섞지 않음
  ○ 원본 범위 타임라인 자막은 원본 밀리초에 살고, 컷에 가려져도 문서에 남음 (규칙 9)
  ○ 초안 편집 자막은 초안 밀리초. 같은 자막이라도 두 문서 사이에 이어지는 아이디 없음
  ○ 한쪽 편집을 다른 쪽에 옮기려면 렌더(타임라인 → 초안) 한 방향뿐임

□ 불변식 검사 (`scripts/check_invariants.py`)
  ○ `check_final_edit_stays_pure` · `check_timeline_edit_stays_pure` · `check_burn_in_stays_pure`
  ○ `check_final_edit_io_writes_only_edit_folder` · `check_timeline_io_writes_only_timeline_folder`
  ○ `check_timeline_render_writes_only_through_export_plan`
  ○ 네트워크 import 허용은 `ui/instance.py`·`ui/service.py` 둘뿐 (`UI_NETWORK_ALLOWLIST`, 규칙 7)

□ 저장 규칙
  ○ 두 편집기 모두 서버가 저장마다 문서 전체를 순수 모델로 다시 검사함
  ○ 리비전이 다르면 충돌로 거부. 저장은 작업공간 서명·수명 토큰에 묶임
  ○ 원본 범위 타임라인의 `seeded`(심은 리비전)는 저장으로 못 바꿈 — `add_source_to_timeline`만 바꿈 (D132)

□ 성능 선
  ○ 「최종 편집」 탭에서는 타이머 셋이 쉼 (D134). 탭을 옮기거나 나누면 `_wire_timer_rest`를 같이 고칠 것
  ○ Gradio 실행 기록은 마운트에서 끔 (D127). 구성요소 수가 늘면 틱 한 번 비용이 커짐 (약 370개에서 260 ms)

---

## 4. 화면 작업에서 실제로 겪은 것 (W-71 세션)

□ 누름을 잃는 경우
  ○ mousedown과 mouseup 사이에 단추가 움직이면 click이 오지 않음
    - 원인 예: 글 칸 확정이 상태 줄·경고 줄 높이를 바꿈
    - 처방: 상태 줄 한 줄 고정(말줄임 + title) · 경고 줄은 비어도 높이 유지
  ○ 목록은 click 대신 pointerdown에서 고름

□ 초점
  ○ 칸을 누른 뒤 초점이 재생기 밖이면 키가 초안 편집으로 감
    - 처방: 누름에서 `S.rootEl.focus({preventScroll:true})`
  ○ 글 칸 안의 Space·⌫·⌘Z가 재생·클립·문서에 닿지 않는지 매번 확인

□ 다시 짓기
  ○ 같은 대상을 보는 동안은 편집 칸을 다시 짓지 않고 글·정보만 제자리에서 바꿈
  ○ 재생 중 목록 따라가기는 `list.scrollTop`만. `scrollIntoView`는 페이지를 움직여 금지 (시험이 잠금)

□ 고친 글 확정 자리
  ○ 떠날 때(change) · ⌘Enter·Esc · 저장 · 렌더 · 원본 넣기 · 창 숨기기 · 닫기 앞

□ 화면 실측 방법
  ○ `.claude/launch.json`의 `vlogsrt-ui-offline` (포트 7871, `--offline`) — 부록 A
  ○ 서버를 다시 띄울 때 7871이 풀린 뒤 `preview_start` (안 그러면 「기존 서버가 시작 또는 종료 중」)
  ○ 스크린샷 좌표는 800px 틀, 뷰포트 1024px — JS 좌표에 800/innerWidth를 곱함
  ○ 합성 원본 라이브러리는 부록 B로 만듦 (무료, 오프라인)

---

## 5. 남은 일

□ 기능으로 남은 것 (D 기록의 「남은 것」)
  ○ 프레임 단위 밀기 `,`·`.` — 원본 프레임 길이가 정수 밀리초가 아님 (D138)
  ○ 분석 중 만드는 프록시를 급한 자리로 — 호출 문맥 필요 (D140)
  ○ 카드마다 원본 전체 해시 (D121 — D62 결정으로 남김)
  ○ 썸네일 진행 틱마다 그리기 한 번 (D123·D124 — D64 G 결정으로 남김)
  ○ 원본 재생 컷 공백 다시 재기 (D127)

□ 사람 확인 대기 — 화면에 닿는 것
  ○ 실제 원본 라이브러리에서 W-71b–i 전부: 보내기·재생·범위 조정·클립 편집·원본 넣기·자막 편집·렌더
  ○ W-75 실제 브라우저 핀치 느낌 · W-74 보이는 창의 lazy 썸네일
  ○ W-65·W-67 실제 초안 번인 · W-64 Final Cut 재불러오기 · W-63 실제 Gemini 번역
  ○ W-10·W-11 실브라우저 배치·빗금 확인

□ 사람 확인 대기 — 화면 밖
  ○ D139 분석 중 겹침 측정 · W-4·W-6 실물 인수 · W-38 사람 픽스처 · W-39 H-C1
  ○ 결정 대기: W-8 Luna 가격 · W-14b 판정 증거 모델

---

## 6. 통합 세션이 먼저 사용자와 정할 것

□ 편집기
  ○ 원본 범위 타임라인과 초안 편집을 하나로 합칠지, 둘 다 두고 화면만 정리할지 (D130 재검토)
  ○ 합친다면 어느 쪽 모델이 남는지. 원본 범위 타임라인이 자막 편집까지 갖춘 지금, 초안 편집만 하는 일은 렌더 뒤 다듬기뿐임

□ 입구
  ○ 「추천만 이어붙여 내보내기」와 「타임라인으로 보내기」 두 단추를 둘 다 둘지

□ 자리
  ○ 오른쪽 칸의 결과 카드·영어 변환·자막 입히기를 「최종 편집」 쪽으로 옮길지
  ○ 숨은 「자막」 탭을 지울지 (지우면 그 처리기·시험 정리 필요)

---

## 7. 파일 지도

| 파일 | 줄 | 맡은 것 |
|---|---|---|
| `ui/workspace_service.py` | 11964 | 앱 조립 · 탭 · 명령 줄 · 오른쪽 칸 · 서랍 · 배선 전부 |
| `ui/ui_facade.py` | 5854 | 작업대 HTML·JS 조각 · 결과 카드 · 활동 칸 |
| `ui/final_edit_ui.py` | 479 | 「최종 편집」 탭 짓기 · 자동 전환 · `KEYMAP` · `fe_*` |
| `ui/final_edit_js.py` · `final_edit_app_js.py` | 524 · 1275 | 초안 편집 브라우저 핵심 · 화면 |
| `ui/timeline_cell.py` | 164 | 「타임라인으로 보내기」 · 탭 머리 요약 |
| `ui/timeline_player.py` | 398 | 재생기·원본 띠·자막 칸 HTML/CSS · `tl_*` |
| `ui/timeline_edit_js.py` · `timeline_player_js.py` | 639 · 1224 | 원본 범위 타임라인 브라우저 핵심 · 화면 |
| `ui/burn_cell.py` | 284 | 자막 입히기 칸 |
| `ui/design_tokens.py` | 309 | 색·글꼴 토큰 |

시험: `tests/raw/test_final_edit_*.py` · `test_timeline_*.py` · `test_burn_ui.py` · `test_full_ui_screens.py` ·
`test_visual_workbench_ui.py` · `test_folder_workbench_ui.py`. 화면 구조를 문자열로 잠그는 시험이 많아 칸을 옮기면 함께 깨진다 —
기대값을 고치기 전에 그 시험이 지키던 것이 새 구조에서도 지켜지는지 먼저 본다.

---

## 부록 A. 오프라인 앱

```json
{"name": "vlogsrt-ui-offline", "runtimeExecutable": ".venv/bin/python",
 "runtimeArgs": ["-u", "-m", "ui.app", "--offline", "--port", "7871", "--no-browser"], "port": 7871}
```

`.claude/launch.json`은 커밋하지 않는다(로컬 파일). 에이전트는 온라인 앱을 띄우지 않는다(CLAUDE.md).

## 부록 B. 합성 원본 라이브러리 (무료, 오프라인)

원본 셋(4K 가로 · HD 가로 · 세로 24fps, 각 30초, 자막 4줄) + 초안 하나(`export/`) + 타임라인 하나를 만든다.
시험 도우미를 쓰므로 저장소 밖 스크래치 폴더에서 돌린다:
`VLOGSRT_OFFLINE=1 <저장소>/.venv/bin/python -I seed.py <저장소> <출력 폴더>` (2026-11-16 `fef5ef2`에서 돌려 확인).
앱에서 「폴더 불러오기」로 `<출력 폴더>/shoot`을 연다.

```python
import json, sys
from pathlib import Path

repo = Path(sys.argv[1]); out = Path(sys.argv[2])
sys.path.insert(0, str(repo))
import tests.raw.test_render_export as tre
import tests.raw.test_review_workspace as trw
from vlogsrt.project.raw import timing_engine, timeline_edit_io
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.config import TIMING_ENGINE_WHISPER

SECONDS = 30
def speech(offset):
    return [{"start": 7 * k + offset, "end": 7 * k + offset + 1.8, "text": f"{k + 1}번째 말입니다"} for k in range(4)]
tre.UTTERANCES.clear()
tre.UTTERANCES.update({"A_uhd.mp4": speech(0.2), "B_hd.mp4": speech(0.4), "C_tall.mp4": speech(0.3)})
folder = out / "shoot"
tre._encode(folder / "A_uhd.mp4", "3840x2160", 30, SECONDS, created="2026-09-01T10:05:00Z")
tre._encode(folder / "B_hd.mp4", "1920x1080", 30, SECONDS, created="2026-09-01T10:00:00Z")
tre._encode(folder / "C_tall.mp4", "1080x1920", 24, SECONDS, created="2026-09-01T09:55:00Z")
originals = {name: tre._identity(folder / name) for name in tre.UTTERANCES}
timing_engine.DEFAULT_NEW_HEARING_ENGINE = TIMING_ENGINE_WHISPER
raw_service.DEFAULT_NEW_HEARING_ENGINE = TIMING_ENGINE_WHISPER
real = tre._write_replay_fixtures
def more_windows(root, sources):
    real(root, sources)
    luna = root / "analysis/replay/luna-fixtures.json"
    data = json.loads(luna.read_text(encoding="utf-8"))
    for n in range(1, 40):
        key = f"window_{n:06d}"
        body = trw._luna_body(key)
        body.update(suitability="usable", suitability_reason="선명하다", scene_tags=["dialogue"])
        data["windows"][key] = body
    luna.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
tre._write_replay_fixtures = more_windows
trip = tre._trip(folder, originals)
timeline_edit_io.create_timeline(trip.service, trip.root)
print("root", trip.root)
```

시험 모듈의 내부 도우미(`_encode`·`_trip`·`_write_replay_fixtures`)에 기대므로 그 시험이 바뀌면 함께 손봐야 한다.
