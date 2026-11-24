> 짝 문서: 시작 정리 `docs/plans/2026-11-16-ui-integration-handoff.md` · 끝난 뒤 확인 `docs/plans/2026-11-16-ui-integration-checklist.md`. 승인 2026-11-16(plan mode). 이 문서가 남은 태스크 차례의 정본이고, 진행 상태는 `TASKS.md`가 적는다.

# vlogsrt 통합 개발 계획 — Final Cut식 창 · Subtitle Edit식 자막 · 세 운영체제 · 남은 계획 전부

## Context
사용자가 이 대화 하나로 진행하려는 일은 세 갈래다(2026-11-16).
- **Final Cut식 창 하나**: 흐름 **AI 영상 추천 → 타임라인 편집 → 한/영 자막 번인**을 Final Cut Pro처럼 창 하나에서 한다.
- **Subtitle Edit식 자막 편집**: 자막은 Subtitle Edit처럼 쉽게 고친다.
- **세 운영체제**: 맥·윈도우·리눅스에서 바로 쓴다. 어려우면 맥·윈도우가 먼저다. 브라우저 UI는 그대로 둔다.

또 지시한 것:
- 나뉜 브랜치를 모두 `main`으로 합치고, 거기서부터 개발한다.
- md 파일로 남아 있는 예전 계획을 모두 읽어 이 계획에 넣는다.

**지금 상태**: 흐름이 탭 둘로 흩어져 있다. 편집기 둘이 한 탭에 쌓여 있다(원본 범위 타임라인 D125–D138, 초안 편집 D117). Windows는 실제로 돌려 본 적이 없고, 리눅스 실행기는 없다.

**조사 방법**: Fable이 다섯 가지를 조사했다. 구현은 Opus(이 대화)가 한다.
- 현재 UI
- Final Cut Pro 12.4 정적 REA
- Subtitle Edit 5.2.0 정적 REA
- 설계
- 계획 문서 전수: `docs/plans/*`(origin 최신), TASKS의 열린 행, `~/.claude/plans/`에서 vlogsrt 관련 파일

### 진행 방식 (사용자 지시)
- **이 대화 하나로 모든 태스크를 차례대로 한다.** 사용자 지시가 CLAUDE.md 「한 세션 한 태스크」보다 우선한다.
- 그래도 태스크마다 따로 묶는다:
  - `main`에서 `bp_cc/<이름>` 브랜치와 `.claude/worktrees/<이름>` 워크트리를 판다.
  - 시험을 먼저 쓴다 → `make check` → 커밋 → push → PR(base `main`) → 머지한다.
  - 다음 태스크는 갱신된 `main`에서 판다.
- 다른 세션은 만들지 않는다.
- PR 머지는 CI가 초록이면 이 대화가 한다. 사람 확인(🧑)이 필요한 태스크는 확인 목록을 남기고 다음으로 넘어간다.
- 결정이 필요한 태스크에 닿으면 AskUserQuestion으로 묻고 기다린다.
- 하지 않는 것: 유료 호출, `.env`, `--online`, 온라인 앱 띄우기.

### 사용자 결정 (2026-11-16)
1. 작업대 안에 FCP식 창 하나를 둔다: 위 [브라우저 | 뷰어 | 인스펙터], 아래 타임라인. 탭 둘을 대신한다. Workflow Extension은 되살리지 않는다.
2. **편집기는 D125 원본 타임라인 하나**다(UI-1·UI-2에 대한 답, D130 재검토).
   - 「공유」 한 번이 렌더와 한/영 번인을 함께 한다.
   - D117/D118은 단계적으로 뒤집는다.
   - 영어 번역은 유료라서 공유 안에서 사지 않는다.
3. 창·명령 이름만 FCP 한국어를 쓴다. 추천/보통/비추천, 사용/제외, 자막은 그대로 둔다.
4. 테마는 OS를 따른다. 어두움 짝만 FCP 쪽으로 맞춘다.
5. 자막 편집은 Subtitle Edit을 따른다.
6. `main`을 만들어 push하고 GitHub 기본 브랜치로 둔다. **나뉜 브랜치를 모두 합친다.** 브랜치는 지우지 않는다.
7. 진행 중 작업 정리 파일도 담는다. 이 파일은 다른 세션이 이미 origin에 커밋했다: `docs/plans/2026-11-16-ui-integration-handoff.md`·`-checklist.md`.
8. 맥·윈도우·리눅스를 지원한다. 어려운 것은 맥·윈도우를 먼저 한다.
9. 예전 계획 md를 모두 이 계획에 넣는다.

> 번호 결정(먼저 커밋한 타이머가 W-81)은 상황이 바뀌어 지킬 수 없다. 그사이 origin이 W-81·D141(Gemini 429)과 W-82·D142(의미 분석 창 동시 전송)를 이미 push했다. 그래서 합쳐지지 않은 타이머 브랜치가 다음 번호 **W-83·D143**을 받는다.

## 분석 요약
### Final Cut Pro 12.4 (`/Applications/Final Cut Pro.app`, 정적)
- **창**: 위 [사이드바+브라우저 539 | 뷰어 539 | 인스펙터 403], 아래 타임라인, 0.5/0.5로 끌 수 있다. 타임라인 인덱스(318)는 숨김이 기본이다. 작업공간은 기본·정리·색상.
- **키**(`Default.commandset`):
  - 재생: Space, J/K/L, ←/→ 1프레임, ⇧←/→ 10프레임, ↑/↓ 편집점, N 스내핑.
  - 마킹: I/O, F 선호, ⌫(브라우저는 제외, 타임라인은 삭제).
  - 편집: E 추가, ⌘B 블레이드, ⇧⌫ 갭으로 대치, ⌥[ ⌥] 다듬기, ,/. 미세 이동, ⌘Z/⇧⌘Z, ⌘=/−/⇧Z.
  - 캡션·창: ⌥C 캡션 추가, ⌘E 공유, ⌘1–4 창, ⇧⌘2 인덱스.
- **캡션·공유**: 언어별 하위 역할을 둔다. 인스펙터에서 텍스트와 시작/끝/길이를 고친다. 인덱스에 「오류 보기」가 있다. 공유 역할 탭에 「번인 캡션:」이 있고, 내보내기는 백그라운드 작업으로 돈다.
- **색**: 강조 #706BFF(흰 글자 대비 4.01:1이라 지금 기준 4.5 미달). 클립 색은 **추정**이다.

### Subtitle Edit 5.2.0 (`/Applications/Subtitle Edit.app`, 정적)
- **세 칸**: 자막 목록+입력칸 · 비디오 · 파형.
- **목록 열**: # · 시작 · 끝 · 길이 · 텍스트 · 원본 · 간격 · 문자/초. 줄바꿈은 ⏎로 보인다.
- **입력칸**: 시작·끝·길이 조절칸, 텍스트, 문자/초·줄 길이(넘으면 빨강). 번역 모드에서는 원본 칸이 같은 줄과 시각에 붙는다.
- **파형**:
  - 클릭은 이동·멈춤·고르기. 빈 곳을 끌면 새 선택(Enter 넣기, Esc 취소).
  - 가장자리를 끌면 시각이 바뀌고, ⌥를 누르면 이웃 가장자리도 함께 간다. 몸통을 끌면 옮긴다.
  - ⇧클릭 시작, ⌘클릭 끝, ⌥클릭 길이 유지 이동. 휠은 스크롤, ⌥휠은 확대.
  - 최소 간격을 지키고, 장면 전환에 8 px로 스냅한다(⇧는 무시).
  - 블록마다 번호·길이·문자/초를 보이고, 재생 중에는 가운데를 따라가며 줄을 자동으로 고른다.
- **키**:
  - 기본: Space, ←/→ 1초, ⌥←/→ 500 ms, F11 시작점, F12 종료점, ⌥↑/⌥↓ 줄 이동, ⌘⇧E 늘리기, ⌘⇧M 합치기, ⌘⌥V 커서에서 나누기, F8 다음 오류.
  - **사용자 설정**: A 시작점 · D 종료점 · S 나누기 · T 입력칸 · ⌘T 입력칸↔파형.
  - Mod는 macOS에서 ⌘, 다른 운영체제에서 Ctrl이다(`GetCommandOrWin`).
- **검증 기본값**: 43자 · 2줄 · 문자/초 25 · 1–8초 · 간격 24 ms · 오류는 #32FF0000.
- **한국어 용어**(`Korean.json`): 자막 목록 · 입력칸 · 파형 · 시작점 설정 · 종료점 설정 · 줄 나누기 · 이전/다음 줄과 합치기 · 다음 줄로 늘리거나 줄이기 · 선택 영역에 새 자막 삽입 · 번역 모드 전환 · 싱크 조정 · 재생 시 현재 줄 자동 선택.

### 인수 문서가 정한 선 (`docs/plans/2026-11-16-ui-integration-handoff.md`·`-checklist.md`)
- 두 문서의 시간 기준(원본 ms 대 초안 ms)을 섞지 않는다. 타임라인 → 초안은 렌더 한 방향뿐이다.
- 깨지기 쉬운 장치:
  - 타이머 쉼(D134)
  - 숨은 「다시 열기」(W-71h)
  - 패널은 고른 영상만(D122, payload 상한 시험)
  - 급한 자리(D136·D140)
  - ETag·lazy(D133)
  - 구성요소 수(약 370개에서 틱 260 ms)
- 화면 작업에서 배운 것:
  - 단추가 움직이면 click을 잃는다(상태 줄은 한 줄로 고정).
  - 목록은 pointerdown에서 고른다.
  - 누르면 루트에 focus를 준다.
  - 따라가기는 `scrollTop`만 쓴다(`scrollIntoView` 금지).
  - 글 확정 자리를 지킨다.
- 합성 원본 라이브러리 시드(부록 B)를 미리보기 실행기에 쓴다.

## 설계 원칙
- **역할 나누기**: FCP는 틀(창·클립 편집·공유)이고, SE는 자막 칸(목록·입력칸·파형 블록)이다.
- **작업공간**: 「기본」·「자막」 두 가지.
  - 「자막」: 위 [자막 목록+입력칸 | 뷰어 | 인스펙터(자막)], 아래는 자막 레인을 키운 타임라인이다.
  - 「기본」: 인스펙터 「자막」 탭이 SE식 입력칸이고, 타임라인 인덱스가 작은 자막 목록이다.
- **키**: 포커스한 창이 정한다. 자막 창은 SE 키(사용자 A/D/S/T 포함), 클립 레인·브라우저는 FCP 키다. 표기는 `Mod`(⌘/Ctrl)·`Alt`(⌥/Alt)이고, 운영체제마다 표시를 다르게 한다.
- **한/영 짝 = SE 번역 모드**: 시각 하나에 텍스트 칸 둘을 둔다. 영어는 저장된 번역을 보여 주고, 한국어를 고치면 「낡음」으로 표시한다.
- **검증 숫자**: `FinalEditSettings`(refine 값 그대로)를 쓴다(규칙 8). 보이는 방식만 SE를 따른다.
- **재료는 이미 있다**: `timeline_edit.py:871-958`·`timeline_edit_js.py:407-465`의 자막 연산(맞춤 시험 포함), D138 자막 칸, `__vlFE`의 파형·휠 줌(순수 부분을 떼어 공용으로 쓴다).
- **운영체제 범위**:
  - 분석·렌더·번인·UI는 세 운영체제에서 돈다.
  - Final Cut 길과 videotoolbox는 macOS 전용이다. 다른 운영체제에서는 단추를 감추거나 까닭을 보여 준다.
- **흉내 범위**: Apple·SE의 비트맵·아이콘·글꼴은 싣지 않는다. 배치·키·용어만 흉내 낸다. SE는 GPL이라 코드는 옮기지 않는다.

---

## 0단계 — 모든 브랜치를 `main`으로 (가장 먼저)
**대상**: origin 최신 `7910601`이 기준이다. 로컬 `feat/folder-workbench`는 3커밋 뒤이고, 이 checkout은 다른 세션이 쓰므로 건드리지 않는다.

| ref | 상태 | 처리 |
|---|---|---|
| `bp_cc/lucid-elion-b4943c` · `objective-tu-6058f3` · `timeline-clip-ops` · `timeline-render-open` · `timeline-subtitle-edit` · `video-timeline-subtitle-review-7f6894` · `youthful-merkle-7af3dd` · `tmp/base-sync` · 로컬 `feat/folder-workbench` | 이미 조상 | `git merge`로 「Already up to date」를 확인하고 표로 남긴다 |
| `bp_cc/elastic-tu-b5ae65`(`f8895ee`·`5dfcd18`) — 타이머 셋을 `gr.Timer(2)` 하나로(성능 2-6, 사용자 확인) | 합쳐지지 않음 | 실제로 합친다. **W-81→W-83, D141→D143**으로 번호를 바꾼다. 충돌은 문서(TASKS·DECISIONS·CLAUDE·성능 계획)뿐이니 두 쪽을 다 살린다 |
| `bp_cc/w71-playback-wip`(`378721c`, 검증 안 된 실험) | 합쳐지지 않음 | 실제로 합친다. 충돌은 W-71c·D134·W-83 쪽을 살린다. 겹치지 않는 시드 스크립트·시험은 들인다. 남긴 것과 버린 것을 커밋에 적는다. `make check`가 깨지면 코드를 고친다 |
| `upstream/worktree-agent-aecbdcde38f97922e`(`6acda38`, W-25c 앞 판 — 다른 저장소) | 내용은 `b0c093c`가 대신한다 | `git merge`로 이력을 잇는다. 충돌은 모두 `main` 쪽이다. 커밋에 그 사실을 적는다 |
| `upstream/main`·`upstream/claude/*`(`d317611`) | 이미 조상 | 확인만 한다 |

**절차**
1. `git fetch origin` → `git worktree add .claude/worktrees/main-merge -b main origin/feat/folder-workbench`.
2. 위 표 차례로 합친다. 매번 `make check`를 돌린다.
3. **보드를 하나로** 묶는다(purring 계획 §5와 인수 문서). TASKS에 다음을 넣는다.
   - 빠진 행 W-32: whisper 종료(2027-02-26) 앞의 옛 CLI·숨은 「자막」 탭 거부.
   - 오래된 머리를 닫는다: W-35c·S17·Q1·S12/Q3-2 머리.
   - 🧑 사람 확인 표 하나.
   - 이 계획 문서 `docs/plans/2026-11-16-fcp-window.md`. 인수 문서와 서로를 한 줄로 링크한다.
4. `git branch -a --no-merged main`이 비어 있는지 본다 → `git push -u origin main` → `gh repo edit Ssojux2/vlogsrt-studio --default-branch main`. `feat/folder-workbench`는 남긴다.
5. 사용자에게 알린다: 이 checkout을 `main`으로 옮기는 일은 다른 세션이 끝난 뒤에 하면 된다.

다음 번호는 **W-84 · D144**다.

---

## 태스크 차례 (이 대화에서 순서대로)

### 1군 — 바탕
**W-84 세 운영체제 기반 (D144)**
- **CI** `.github/workflows/check.yml`
  - macos·windows·ubuntu × Python 3.11. 모두 `VLOGSRT_OFFLINE=1`이고 비밀 값은 없다.
  - ffmpeg는 운영체제마다 깐다: brew / choco·gyan full / apt.
  - 단계는 셋이다:
    1. `scripts/smoke_ui.py`: import하고, 오프라인 앱을 127.0.0.1에 띄워 `/`가 200인지 본다.
    2. `scripts/check_all.py`: Makefile `check`를 Python으로 옮긴 것이다(Windows에는 make가 없다). Windows에서는 `mypy --platform win32`도 돈다.
    3. libass `subtitles` 필터가 있는지 확인한다.
  - mac은 막는 게이트다. win·linux는 처음엔 보고만 하고(`continue-on-error`, skip·xfail은 하지 않는다) 초록이 되면 막는 쪽으로 올린다.
- **실행기**
  - `실행.sh`(bash, mode 755)를 새로 둔다. `실행.command`와 같은 판단을 한다.
  - `launch_check`에 tkinter·libass 안내를 운영체제별로 넣는다. 둘 다 막지는 않는다.
  - `burn_config.ffmpeg_candidates`를 운영체제별 표로 바꾼다. PATH ffmpeg에 libass가 있으면 그것을 쓴다.
  - README에 리눅스 줄을 더한다.
- **미리보기** `scripts/preview_workbench.py`
  - pathlib만 쓰고 세 운영체제에서 돈다.
  - 합성 라이브러리는 인수 문서 부록 B 방식, 원본 셋이다. 위치는 `.vlogsrt-cache/preview/`, 상태 파일은 따로 둔다.
  - `UiInstance`를 쓰지 않고, 오프라인을 강제한다. 플래그: `--build-only`·`--rebuild`·`--port`.
  - 개인 `.claude/launch.json`이 이 스크립트를 부른다.
- **키 정책**
  - `ui/platform_keys.py`(순수): Mod/Alt를 표시 문자열과 브라우저 판정으로 바꾼다.
  - `KEYMAP`과 키시트가 박아 둔 ⌘ 자리를 이 함수로 바꾼다. mac 바이트는 그대로다.
- **키 측정** `scripts/key_intercept_probe.html`: 🧑 사람이 mac Chrome·Safari, win Chrome·Edge, linux Chrome·Firefox에서 잰다.
- **시험**: `test_launchers` · `test_launch_check` · `test_burn_ffmpeg_lookup` · `test_platform_keys` · `test_preview_launch` · `test_check_all`(Makefile과 견준다) · `test_ci_workflow`.
- **문서**: D144 · `docs/plans/2026-11-16-cross-platform.md`(첫 CI 결과·실패 목록·사람 첫 실행 목록) · CLAUDE 실행 문단.

**W-84a… 크로스플랫폼 고치기**: 첫 CI가 드러낸 실패를 고친다. Windows를 먼저, 다음이 Linux다. 다 초록이면 막는 게이트로 바꾼다. 실제 Windows 첫 실행은 🧑(README WP2)이다.

### 2군 — FCP식 창과 SE식 자막 (화면)
**W-85 FCP식 창 골격 (D145)** — 모델은 바꾸지 않는다.
- 재생기·요약을 `_review_stage` 안 `#vl-timeline-deck`으로 옮긴다.
- `#visual-stage` grid는 토큰을 쓴다(539/539/403 · 0.5/0.5). 좁은 화면은 쌓는다. `data-vl-workspace` 자리를 둔다.
- 탭은 「편집」·「초안 편집(이전)」. `send_view`는 같은 창에 머문다.
- `__vlShortcutBlocked`에 `[data-vl-own-keys]` 검사를 넣는다(타임라인 키가 리뷰 영상을 움직이는 버그).
- **타이머 임대**(D134를 대신하고, W-83의 단일 타이머 위에서 돈다):
  - 숨은 쉼·켬 단추를 두고 순수 `__vlTimerLease`가 정한다. `timer_rest_idle_ms`는 설정값이다.
  - 쉬는 동안에도 `tl_job`·`/vl-activity`는 돈다. 1-튜플 함정을 지킨다.
- 기대값 변경(승인 대상):
  - `test_workbench_layout::test_grid_areas_at_three_breakpoints`
  - `test_timeline_cell_ui:90` → `REVIEW_TAB_KEY`·문구
  - `test_timeline_player_ui:205` → 조상 `visual-stage`를 더한다
  - 탭 이름 시험
  - W-83 타이머 배선 → 쉼 경로를 더한다(지우지 않는다)
  - 표 밖의 변경은 멈추고 보고한다. 골든은 그대로다.
- 시험: `test_fcp_window_ui`, 노드(own-keys·임대), 배선.
- 확인: 미리보기로 줌 100번 동안 긴 프레임 0, 유휴 뒤 다시 켜짐.

**W-86 창 DOM·FCP 이름 · 골든 한 번**
- 뷰어 하나(포커스한 창에 따라 `.tl-frame`을 옮긴다).
- 인스펙터 탭(정보·자막·공유)은 클라이언트 쪽에서 만들고 새 `gr.Tabs`는 쓰지 않는다.
- 타임라인 인덱스 칸, 트랙 손잡이(순수 clamp · localStorage).
- 이름: 클립 범위 · 블레이드 · 삭제 · 실행 취소/실행 복귀 · 스토리라인에 추가 · 백그라운드 작업.
- 골든 `workbench_render_18_sources.html`은 한 번만 바꾼다(셸만, 모델 JSON은 바이트 그대로). ETag·lazy·D122 상한은 그대로다.

**W-87 「자막」 작업공간 — Subtitle Edit식 (D146)** — D138 칸을 옮기고 넓힌다.
- **자막 목록**:
  - 열: # · 시작 · 끝 · 길이 · 텍스트 · (영어) · 간격 · 문자/초.
  - 줄바꿈은 ⏎. 경고 칸은 반투명 빨강, F8/⇧F8로 오류를 오간다.
  - pointerdown에서 고르고, 더블클릭하면 그 자리로 가서 멈춘다. 재생 중에는 자동으로 고른다(`scrollTop`만).
- **입력칸**: 시작·끝·길이(프레임 ±), 한국어 텍스트, 문자/초·줄 길이. 영어 칸에는 낡음을 표시한다.
- **자막 레인(파형)**:
  - `__vlFE` 파형 mip·휠 줌을 공용 순수 모듈로 옮긴다.
  - SE 조작을 그대로 둔다: 클릭, 빈 곳 끌기로 새 선택, 가장자리(⌥는 이웃과 함께), 몸통 끌기, ⇧·Mod·Alt 클릭, 휠·Alt휠.
  - 최소 간격을 지키고, 클립 컷 경계에 스냅한다(⇧는 무시).
  - 블록에 번호·길이·문자/초를 보인다. 우클릭 메뉴는 SE 한국어 용어로 쓴다.
- **SE 루프 키**: A/F11 · D/F12 · S · T · Mod+T · Alt+↑/↓ · Mod+⇧E · Mod+⇧M · Mod+Alt+V · Delete · Space.
- **겹침 정리**(인수 문서 UI-dup): `FE.splitText`/`TL.splitText` 둘을 `TL` 하나로 줄인다. 한 탭 두 키 처리기 초점 버그도 여기서 없앤다.
- **프레임 단위 밀기 `,`/`.`**(D138이 남긴 것): 원본 프레임 → ms 변환은 반올림 규칙 하나로 하고, 순수 함수와 맞춤 사례를 둔다.
- 연산·저장은 `__vlTL`·`tl_save` 그대로다. 가린 자막은 남는다(규칙 9). 맞춤 사례를 `timeline_edit_cases.json`에 더한다.

**W-88 「공유」 (D147)** — UI-3·UI-4.
- `timeline_share.py` + `ui/share_panel.py`(주입).
- `export_timeline` → `export_plan`, 그리고 「번인 캡션:」이 있으면 컷 없는 번인으로 `draft.burned.mp4`를 만든다.
  - D116 스타일과 덮지 않는 이름을 지키고, `burn_in.py`는 손대지 않는다. 급한 자리를 쓴다(D140).
- 검사: id·리비전과 `srt_text` sha.
- 영어는 `matching_english`일 때만 쓰고, 사지 않는다. 무료 「자막 내보내기(SRT)」를 둔다.
- 결과 카드·「영어 SRT로 변환」·번인 손잡이는 인스펙터 「공유」 탭으로 옮긴다(UI-4).
- **UI-3(입구 두 단추)는 이때 묻는다.** 「추천만 이어붙여 내보내기」를 은퇴시킬지, 「스토리라인에 추가」 + 바로 공유로 바꿀지.
- `test_burn_ui.py:55`의 조상을 공유 패널로 바꾼다. CI 세 운영체제에서 libass 번인 시험을 돌린다.

**W-89 창 기준 키 맵 하나 (D148, W-84 측정이 필요하다)**
- `ui/keymap.py`(KeyBinding + pane, Mod/Alt 표기). 디스패처 하나와 포커스 링이 키 처리 셋을 대신한다.
- 창마다 다른 키:
  - ⌫: 브라우저에서는 제외, 타임라인에서는 삭제.
  - ,/.: 미세 이동.
  - J/L: 셔틀(D에 적는다).
  - A/D/S/T: 자막 창만.
- 측정을 통과한 키만 쓴다. 대안은 Alt+1–4, Mod+⇧E, 툴바다.
- **결정**: 영어 칸을 직접 고칠지, 고친다면 어디에 저장할지.

**W-90 어두움 짝을 FCP 쪽으로**: 채움은 #645FF0(4.73:1), 표시·포커스는 #706BFF. 기준은 낮추지 않는다.

**W-91 D117·숨은 「자막」 탭 은퇴 + W-32 (D149)** — UI-5. W-88·W-89를 마친 뒤 하고, 🧑 실제 라이브러리 인수는 목록으로 남긴다.
- 지운다: 레거시 탭, `final_edit_ui`·`final_edit_js`·`final_edit_app_js`, `prepare_edit` 길, 숨은 「자막」 탭과 그 처리기·시험.
- W-32를 붙인다: 옛 CLI `vlogsrt run/record`는 whisper 종료 뒤 유료 호출 앞에서 바로 거부한다. **기한은 2027-02-26.**
- `load_for_burn`을 정한다. `CLAUDE.md`를 다시 쓴다.

### 3군 — 성능 (성능 계획 `2026-11-12-performance-optimization.md` 남은 것)
- **P-1 (4-2)** 창 payload 한 번 짓기. 결정 없음.
- **P-2 (4-6)** 일괄 승인 `unanalyzed` upsert O(N²) → 그 행 하나. 영어 묶음 병렬은 `srt_english.py` 기준으로 다시 확인한다(D114가 옛 경로를 지웠다).
- **P-3 (3-5)** 미리보기·렌더 디코드에 `-hwaccel`(macOS videotoolbox, 다른 운영체제는 끔)과 실패 기억. 운영체제 가드를 둔다.
- **P-4 (4-4)** 1단계 동안 Gemini 클립을 미리 인코드한다(「전사만」은 하지 않는다).
- **P-5 (4-3)** 1단계 청크 병렬 + `CountingAsrGate` 잠금. **동시 수를 물어 정한다.**
- **P-6 (4-5)** 캐시 열쇠 안정화. **결정 D-3(옛 열쇠 이행)** 이 필요하다.
- 측정했을 때만 할 것: 3-1 거듭 해시(D62 결정 필요) · 3-2 · 3-3 · 2-7 · 3-7 · 3-8. 기준과 견줄 때는 `bench_workbench.py`와 `docs/perf/2026-11-baseline.md`를 쓴다.
- 남겨 둔 결정: D-2 요청 사이 캐시, D-4 바이트 신원(기본은 손대지 않음).

### 4군 — 자막·번역·기타
- **W-15F 정리**: whisper 은퇴로 닫히는 것과 남는 것(④·⑧)을 가른다. ⑤는 W-91과 함께, ③은 유료라 🧑다.
- **W-52c**(H2 뒤) · **W-38 a/b/c**(H-B1·H-B2 픽스처 뒤): 사람이 픽스처를 주면 그때 한다.
- **원본 재생 컷 공백 다시 재기**(D127): 보이는 창에서 재야 하므로 🧑 확인 목록에 둔다.

## 사람만 할 수 있는 것 (🧑 — 하나의 표로 TASKS에 둔다)
- **실제 원본 라이브러리**: W-71b–i 전부, 공유·번인(W-65·W-67·이번 W-88).
- **보이는 브라우저**: W-74 lazy · W-75 핀치 · W-69 payload · W-10/11/17 배치·테마 · 키 측정(W-84).
- **유료**:
  - D139 겹침 · D141 429 문구 · D142 동시 4개
  - W-52 H2 · **H3 가격 재확인(2027-01-26 앞 — 지나면 새 전사가 막힌다)**
  - W-39 H-C1 · W-14 · W-55 · W-57 · 옛 인수(W-4·W-6·V3·V4·S11·S16·Q3-H·Q2)
- **실제 Windows 첫 실행**(W-84).
- **결정**: W-8 Luna 가격 · W-14b · D-2 · D-3 · D-4 · 청크 동시 수 · UI-3 · 영어 직접 편집.

## 낡았거나 대체된 계획 (따로 하지 않는다)
- 성능 Phase 1 `system_profile`(→ D139)
- 2-5(→ D133)
- D-1(→ D141)
- 2-6(→ W-83 타이머)
- W-35c/d/e·W-31f(→ D95·D104)
- D105 영어 꾸러미(→ D114)
- 견적 문구(→ D110)
- D117 「초안 안에서만」(→ D125)
- render-declared-window(→ D113)
- 9월 Final Cut·플러그인 계획들·`working.md`·P7·X·V5(→ D42–D56·D100)
- S17(진단상 구현하지 않음)
- Q1(UNSOUND)
- `~/.claude/plans/`의 splendid-hopping-quill·cryptic-hopcroft·enumerated-hugging-lagoon·agile-panda·graceful-hopper·b977(모두 끝남)
- whisper-harmonic-starlight 단어 시계 검사(승인된 적 없고, Gemini 엔진에서는 쓸모가 없다)

## 위험과 대응
- **merge 충돌**: 0단계는 문서 충돌이 대부분이다. WIP 브랜치는 코드 충돌이 날 수 있다. 머지마다 `make check`를 돌리고, 기대값은 바꾸지 않는다.
- **첫 Windows CI의 많은 실패**: 보고만 하는 상태로 시작하고, 태스크로 나눠 고친다. skip은 하지 않는다.
- **타이머 임대 깜빡임 · 골든 변경 · 브라우저가 가로채는 키 · SE와 FCP 키 충돌 · Gradio 분할 창 없음 · 공유의 두 번 인코드 · 구성요소 수 증가**: 앞에서 정한 대응을 따른다. 유휴 값은 설정, 골든은 한 번, 키는 측정 뒤, 포커스한 창이 이긴다, CSS grid, 단일 인코드는 미룬다, 새 구성요소는 최소로 한다.
- **긴 대화**: 태스크마다 커밋·PR로 상태를 남긴다. 이어 가는 지점은 TASKS 보드와 이 계획 문서다.

## 검증
- **태스크마다**: 워크트리에서 `make check`(W-84부터는 `scripts/check_all.py`도) → PR CI(ccd_pr `get_status`) → 화면 태스크는 `preview_start vlogsrt-ui-offline`으로 띄운 뒤 `read_page` · 콘솔 · 상호작용 · 스크린샷을 본다. 성능은 Long Animation Frame을 잰다.
- **0단계**: `git branch -a --no-merged main`이 비어 있다. `gh repo view --json defaultBranchRef`가 `main`이다.
- **인수 문서 확인 목록**(`2026-11-16-ui-integration-checklist.md`): W-86과 W-88 뒤에 오프라인 앱으로 상자를 채운다.

---

## 키 가로채기 측정표 (W-84 — 사람이 `scripts/key_intercept_probe.html`로 잰 결과를 붙인다)

| 운영체제 · 브라우저 | 결과 |
|---|---|
| macOS Chrome | (미측정) |
| macOS Safari | (미측정) |
| Windows Chrome | (미측정) |
| Windows Edge | (미측정) |
| Linux Chrome | (미측정) |
| Linux Firefox | (미측정) |
