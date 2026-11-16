"""「최종 편집」 탭(W-66, D117) — 초안 영상 하나를 가운데 두고 컷과 자막을 한 타임라인에서 편집한다.

Final Cut Pro처럼 아래 타임라인에서 블레이드·리플 삭제·트림을 하고, Subtitle Edit처럼 파형 위
자막 블록의 가장자리를 끌어 시각을 맞추며 왼쪽 목록·오른쪽 인스펙터에서 글을 고친다. 편집은
브라우저가 즉시 적용하고(되돌리기 기록도 브라우저), 서버는 자동 저장마다 문서 전체를 다시 검사해
초안 폴더의 `edit/`에만 쓴다.

초안 내보내기가 끝나면 이 탭으로 자동으로 넘어온다 — 그 화면에서 「내보내기」를 누른 작업이
성공했을 때만(새로고침은 탭을 빼앗지 않는다). 결과 카드 아래 「최종 편집 열기」로도 온다.

화면 틀(`RawWorkspaceUi`)과는 `owner`의 작은 면(선택 확인 · 수명 토큰 · 서비스)으로만 닿는다.
`vlogsrt.project`는 import하지 않는다 — 그 예외는 `ui/workspace_service.py` 하나라 서비스의 오류
종류와 내보내기 작업 종류도 만들 때 받는다.
"""

from __future__ import annotations

import html
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import gradio as gr

from ui import platform_keys
from ui.final_edit_app_js import APP_JS
from ui.final_edit_js import CORE_JS

FINAL_EDIT_TAB_ID = "final-edit-tab"
FINAL_EDIT_TAB_KEY = "final-edit"
REVIEW_TAB_KEY = "review"
FINAL_EDIT_BLOCK_ID = "final-edit-block"
FINAL_EDIT_ROOT_ID = "fe-root"
TAB_LABEL = "초안 편집(이전)"
"""렌더한 초안 위의 편집(D117) — 원본 타임라인 하나로 가며 은퇴한다(W-91). 옛 이름 「최종 편집」."""
OPEN_LABEL = "초안 편집 열기"
OPEN_ID = "vl-export-edit"
RELOAD_ID = "vl-final-edit-reload"
"""숨은 「다시 열기」 — 타임라인 재생기가 자기 렌더가 끝나면 누른다(W-71h · 쉬는 타이머, D134)."""
NO_WORKSPACE_TEXT = "작업공간을 열면 여기서 초안을 최종 편집합니다."
NO_EXPORT_TEXT = (
    "아직 내보낸 초안이 없습니다 — 「편집」에서 「추천만 이어붙여 내보내기」를 하면"
    " 끝나는 대로 이 탭이 열립니다."
)
FINAL_EDIT_JS = CORE_JS + APP_JS
JOB_SUCCEEDED = "succeeded"
JOB_ENDED = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class KeyBinding:
    """단축키 하나 — `code`는 `KeyboardEvent.code`(한글 입력 상태에서도 같다).

    `mod`는 macOS ⌘ · 그 밖 Ctrl. `repeat`면 누르고 있는 동안 되풀이하고, `in_text`면 글 칸 안에서도
    받는다(저장·확정·나가기). `keys`는 시트에 보이는 글.
    """

    code: str
    action: str
    label: str
    keys: str
    group: str
    mod: bool = False
    shift: bool = False
    alt: bool = False
    repeat: bool = False
    in_text: bool = False


def _k(code: str, action: str, label: str, keys: str, group: str, **flags: bool) -> KeyBinding:
    return KeyBinding(code, action, label, keys, group, **flags)


PLAY, EDIT, CUE, VIEW = "재생", "컷 편집", "자막", "보기"
KEYMAP: tuple[KeyBinding, ...] = (
    _k("Space", "play-toggle", "재생 · 멈춤", "Space", PLAY),
    _k("KeyK", "stop", "멈춤", "K", PLAY),
    _k("KeyL", "shuttle-forward", "앞으로 재생(거듭 누르면 빨라짐)", "L", PLAY),
    _k("KeyJ", "shuttle-reverse", "뒤로 재생(거듭 누르면 빨라짐)", "J", PLAY),
    _k("ArrowLeft", "frame-back", "1프레임 뒤로", "←", PLAY, repeat=True),
    _k("ArrowRight", "frame-forward", "1프레임 앞으로", "→", PLAY, repeat=True),
    _k("ArrowLeft", "big-back", "10프레임 뒤로", "⇧←", PLAY, shift=True, repeat=True),
    _k("ArrowRight", "big-forward", "10프레임 앞으로", "⇧→", PLAY, shift=True, repeat=True),
    _k("ArrowUp", "prev-edit", "이전 편집 점", "↑", PLAY, repeat=True),
    _k("ArrowDown", "next-edit", "다음 편집 점", "↓", PLAY, repeat=True),
    _k("Home", "go-start", "처음으로", "Home", PLAY),
    _k("End", "go-end", "끝으로", "End", PLAY),
    _k("Slash", "play-selection", "선택 자막 · 범위 재생", "/", PLAY),
    _k("KeyI", "mark-in", "범위 시작 표시", "I", EDIT),
    _k("KeyO", "mark-out", "범위 끝 표시", "O", EDIT),
    _k("KeyI", "go-in", "범위 시작으로", "⇧I", EDIT, shift=True),
    _k("KeyO", "go-out", "범위 끝으로", "⇧O", EDIT, shift=True),
    _k("KeyX", "mark-clip", "클립을 범위로", "X", EDIT),
    _k("KeyX", "clear-range", "범위 지우기", "⌥X", EDIT, alt=True),
    _k("KeyC", "select-clip", "플레이헤드 클립 선택", "C", EDIT),
    _k("KeyA", "tool-select", "선택 도구", "A", EDIT),
    _k("KeyB", "tool-blade", "블레이드 도구", "B", EDIT),
    _k("KeyB", "blade", "플레이헤드에서 자르기", "⌘B", EDIT, mod=True),
    _k("Delete", "delete", "리플 삭제(범위 · 클립 · 자막)", "Delete", EDIT),
    _k("Backspace", "delete", "리플 삭제(범위 · 클립 · 자막)", "⌫", EDIT),
    _k("Delete", "delete-cues", "범위 안 자막만 지우기", "⇧Delete", EDIT, shift=True),
    _k("Backspace", "delete-cues", "범위 안 자막만 지우기", "⇧⌫", EDIT, shift=True),
    _k("BracketLeft", "trim-start", "클립 앞을 플레이헤드까지 잘라내기", "⌥[", EDIT, alt=True),
    _k("BracketRight", "trim-end", "클립 뒤를 플레이헤드부터 잘라내기", "⌥]", EDIT, alt=True),
    _k("BracketLeft", "cue-start", "자막 시작을 플레이헤드로", "[", CUE),
    _k("BracketRight", "cue-end", "자막 끝을 플레이헤드로", "]", CUE),
    _k("Comma", "cue-nudge-back", "자막 1프레임 앞당기기", ",", CUE, repeat=True),
    _k("Period", "cue-nudge-forward", "자막 1프레임 미루기", ".", CUE, repeat=True),
    _k("Comma", "cue-big-back", "자막 10프레임 앞당기기", "⇧,", CUE, shift=True, repeat=True),
    _k("Period", "cue-big-forward", "자막 10프레임 미루기", "⇧.", CUE, shift=True, repeat=True),
    _k("KeyC", "cue-insert", "플레이헤드에 자막 넣기", "⌥C", CUE, alt=True),
    _k("KeyB", "cue-split", "플레이헤드에서 자막 나누기", "⇧B", CUE, shift=True),
    _k("KeyM", "cue-merge", "다음 자막과 합치기", "⇧M", CUE, shift=True),
    _k("ArrowUp", "prev-cue", "이전 자막", "⌥↑", CUE, alt=True, repeat=True),
    _k("ArrowDown", "next-cue", "다음 자막", "⌥↓", CUE, alt=True, repeat=True),
    _k("Enter", "edit-text", "자막 글 고치기", "Enter", CUE),
    _k("Enter", "commit-text", "글 확정하고 타임라인으로", "⌘Enter", CUE, mod=True, in_text=True),
    _k("Escape", "leave-text", "글 칸에서 나가기 · 선택 풀기", "Esc", CUE, in_text=True),
    _k("KeyN", "snap", "스냅 켜기 · 끄기", "N", VIEW),
    _k("Equal", "zoom-in", "확대", "⌘=", VIEW, mod=True, repeat=True),
    _k("Minus", "zoom-out", "축소", "⌘-", VIEW, mod=True, repeat=True),
    _k("KeyZ", "zoom-fit", "타임라인 맞춤", "⇧Z", VIEW, shift=True),
    _k("KeyZ", "undo", "되돌리기", "⌘Z", VIEW, mod=True, repeat=True),
    _k("KeyZ", "redo", "다시 하기", "⌘⇧Z", VIEW, mod=True, shift=True, repeat=True),
    _k("KeyY", "redo", "다시 하기(Windows)", "Ctrl+Y", VIEW, mod=True, repeat=True),
    _k("KeyS", "save", "지금 저장", "⌘S", VIEW, mod=True, in_text=True),
)


def keymap_payload() -> list[dict[str, object]]:
    """브라우저가 받는 단축키 표 — 이름은 JS 쪽 그대로(camelCase)."""
    rows = []
    for binding in KEYMAP:
        row = asdict(binding)
        row["inText"] = row.pop("in_text")
        row["keys"] = platform_keys.display(binding.keys)
        rows.append(row)
    return rows


def _model_holder(model: dict[str, Any]) -> str:
    """모델은 숨은 칸의 글자로 싣는다(작업대와 같은 꼴 — `<`·`&`는 JSON 안에서 이스케이프)."""
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    text = payload.replace("<", "\\u003c").replace("&", "\\u0026")
    return f'<div class="fe-model" data-fe-model hidden>{text}</div>'


def _sheet_html() -> str:
    groups: dict[str, list[KeyBinding]] = {}
    for binding in KEYMAP:
        groups.setdefault(binding.group, []).append(binding)
    parts = []
    for group, bindings in groups.items():
        rows = "".join(
            f"<dt><kbd>{html.escape(platform_keys.display(b.keys))}</kbd></dt><dd>{html.escape(b.label)}</dd>"
            for b in bindings
        )
        parts.append(f"<h4>{html.escape(group)}</h4><dl>{rows}</dl>")
    return f'<details class="fe-sheet" open><summary>단축키</summary>{"".join(parts)}</details>'


def final_edit_html(model: dict[str, Any]) -> str:
    """탭의 정적 뼈대 — 목록 · 재생기 · 인스펙터 · 도구 막대 · 타임라인. 내용은 실행본이 채운다."""
    empty = model.get("empty")
    if empty:
        return (
            f'<div id="{FINAL_EDIT_ROOT_ID}" class="fe-root fe-empty">{_model_holder(model)}'
            f'<p class="fe-empty-text">{html.escape(str(empty))}</p></div>'
        )
    return (
        f'<div id="{FINAL_EDIT_ROOT_ID}" class="fe-root">{_model_holder(model)}'
        '<div id="fe-banner" class="fe-banner" hidden></div>'
        '<div class="fe-top">'
        '<section class="fe-list"><div class="fe-list-head"><span>#</span>'
        "<span>시작 → 끝</span><span>길이 · 글자/초</span></div>"
        '<div id="fe-list-body" class="fe-list-body"></div></section>'
        '<section class="fe-viewer"><div class="fe-video-wrap">'
        '<video id="fe-video" preload="auto" playsinline></video>'
        '<video id="fe-video-b" class="fe-standby" preload="auto" playsinline></video>'
        '<div id="fe-overlay" class="fe-overlay"></div></div>'
        '<div class="fe-transport"><span id="fe-timecode" class="fe-timecode">00:00:00:00</span>'
        '<span id="fe-duration" class="fe-timecode fe-muted"></span></div></section>'
        f'<section class="fe-side"><div id="fe-inspector" class="fe-inspector"></div>'
        f"{_sheet_html()}</section>"
        "</div>"
        '<div id="fe-toolbar" class="fe-toolbar"></div>'
        '<div id="fe-timeline" class="fe-timeline" tabindex="0">'
        '<canvas id="fe-canvas"></canvas></div>'
        '<div id="fe-status" class="fe-status" aria-live="polite"></div>'
        "</div>"
    )


FINAL_EDIT_CSS = """
#final-edit-block { padding: 0; }
.fe-root { display: flex; flex-direction: column; gap: 6px; font-family: var(--vl-font-sans);
  color: var(--vl-ink); height: calc(100vh - var(--vl-topbar-h, 44px) - 64px); min-height: 560px;
  outline: none; }
.fe-empty { height: auto; min-height: 0; padding: 32px; color: var(--vl-ink-muted); }
.fe-banner { padding: 6px 10px; border: 1px solid var(--vl-warn); border-radius: 6px;
  background: var(--vl-warn-soft); display: flex; gap: 8px; align-items: center; }
.fe-banner[hidden] { display: none; }
.fe-top { flex: 1 1 auto; min-height: 0; display: grid; gap: 6px;
  grid-template-columns: minmax(280px, 1fr) minmax(360px, 2fr) minmax(260px, 1fr); }
.fe-list, .fe-side, .fe-viewer { min-height: 0; border: 1px solid var(--vl-line);
  border-radius: 6px; background: var(--vl-surface); overflow: hidden; display: flex;
  flex-direction: column; }
.fe-list-head, .fe-row { display: grid; grid-template-columns: 30px 1fr auto;
  gap: 2px 6px; padding: 4px 8px; font-size: 12px; align-items: baseline; }
.fe-row .fe-text { grid-column: 2 / -1; font-size: 13px; }
.fe-list-head { color: var(--vl-ink-muted); border-bottom: 1px solid var(--vl-line); }
.fe-list-body { overflow-y: auto; flex: 1 1 auto; }
.fe-row { cursor: pointer; border-bottom: 1px solid var(--vl-line); }
.fe-row:hover { background: var(--vl-surface-hover); }
.fe-row.is-current { box-shadow: inset 3px 0 0 var(--vl-danger); }
.fe-row.is-selected { background: var(--vl-accent-soft); }
.fe-row .fe-num, .fe-row .fe-time { font-family: var(--vl-font-mono); }
.fe-row .fe-text { white-space: pre-wrap; word-break: break-all; }
.fe-row.has-warn .fe-cps { color: var(--vl-tier-red-ink); font-weight: 600; }
.fe-row .fe-flag { display: inline-block; margin-left: 4px; font-size: 10px; padding: 0 3px;
  border-radius: 3px; background: var(--vl-warn-soft); color: var(--vl-tier-yellow-ink); }
.fe-viewer { background: var(--vl-video-well); }
.fe-video-wrap { position: relative; flex: 1 1 auto; min-height: 0; display: flex;
  align-items: center; justify-content: center; }
.fe-video-wrap video { max-width: 100%; max-height: 100%; background: var(--vl-video-well); }
.fe-video-wrap video.fe-standby { position: absolute; visibility: hidden; pointer-events: none; }
.fe-overlay { position: absolute; left: 6%; right: 6%; bottom: 7%; text-align: center;
  pointer-events: none; white-space: pre-line; font-size: clamp(14px, 2.4vw, 30px);
  font-weight: 600; color: var(--vl-on-video-overlay);
  text-shadow: 0 0 3px var(--vl-video-well), 0 0 3px var(--vl-video-well),
    0 0 6px var(--vl-video-well); }
.fe-transport { display: flex; justify-content: space-between; padding: 4px 8px;
  color: var(--vl-on-video-overlay); }
.fe-timecode { font-family: var(--vl-font-mono); font-size: 14px; }
.fe-muted { opacity: 0.7; }
.fe-side { overflow-y: auto; }
.fe-inspector { padding: 8px; display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.fe-inspector textarea { width: 100%; min-height: 72px; resize: vertical; font: inherit;
  color: var(--vl-ink); background: var(--vl-surface-sunken); border: 1px solid var(--vl-line);
  border-radius: 4px; padding: 4px; }
.fe-inspector .fe-field { display: grid; grid-template-columns: 44px 1fr auto auto; gap: 4px;
  align-items: center; }
.fe-inspector input { font-family: var(--vl-font-mono); font-size: 12px; width: 100%;
  color: var(--vl-ink); background: var(--vl-surface-sunken); border: 1px solid var(--vl-line);
  border-radius: 4px; padding: 2px 4px; }
.fe-inspector button, .fe-toolbar button, .fe-banner button, .fe-toolbar select {
  font: inherit; font-size: 12px; padding: 2px 8px; border-radius: 4px; cursor: pointer;
  border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised);
  color: var(--vl-ink); }
.fe-toolbar button.is-on { background: var(--vl-accent); color: var(--vl-on-accent);
  border-color: var(--vl-accent); }
.fe-sheet { padding: 4px 8px 8px; font-size: 12px; border-top: 1px solid var(--vl-line); }
.fe-sheet h4 { margin: 6px 0 2px; font-size: 12px; color: var(--vl-ink-muted); }
.fe-sheet dl { display: grid; grid-template-columns: auto 1fr; gap: 1px 8px; margin: 0; }
.fe-sheet dd { margin: 0; }
.fe-sheet kbd { font-family: var(--vl-font-mono); font-size: 11px; }
.fe-toolbar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; }
.fe-toolbar .fe-gap { flex: 1 1 auto; }
.fe-timeline { flex: 0 0 210px; position: relative; border: 1px solid var(--vl-line);
  border-radius: 6px; background: var(--vl-surface-sunken); overflow: hidden; outline: none; }
.fe-timeline:focus-visible { box-shadow: 0 0 0 2px var(--vl-accent); }
.fe-timeline canvas { position: absolute; inset: 0; width: 100%; height: 100%;
  touch-action: none; }
.fe-status { font-size: 12px; color: var(--vl-ink-muted); min-height: 16px; }
.fe-status.is-error { color: var(--vl-danger); }
@media (max-width: 1050px) {
  .fe-top { grid-template-columns: 1fr; grid-auto-rows: minmax(200px, auto); }
  .fe-root { height: auto; }
}
"""


class FinalEditPanel:
    """탭 하나 — `build_tab`은 탭 묶음 안에서, `build_open_button`은 결과 카드 아래에서 부른다."""

    def __init__(self, owner: Any, service_error: type[Exception], export_job_kind: str) -> None:
        self.owner = owner
        self.service_error = service_error
        self.export_job_kind = export_job_kind
        self.tab: Any = None
        self.block: Any = None
        self.open_button: Any = None

    # ── 짓기 ──────────────────────────────────────────────────────────

    def build_tab(self, header: Callable[[], None] | None = None) -> None:
        """탭 하나 — `header`는 편집 칸 위에 먼저 짓는다(원본 범위 타임라인 요약, W-71b)."""
        with gr.Tab(TAB_LABEL, id=FINAL_EDIT_TAB_KEY, elem_id=FINAL_EDIT_TAB_ID) as self.tab:
            if header is not None:
                header()
            self.block = gr.HTML(
                final_edit_html(self._empty_model(NO_WORKSPACE_TEXT)),
                elem_id=FINAL_EDIT_BLOCK_ID,
                js_on_load=FINAL_EDIT_JS,
                server_functions=self.server_functions(),
                apply_default_css=False,
            )
            self.reload_button = gr.Button(visible="hidden", elem_id=RELOAD_ID)

    def build_open_button(self) -> None:
        self.open_button = gr.Button(OPEN_LABEL, visible=False, size="sm", elem_id=OPEN_ID)

    # ── 잇기 ──────────────────────────────────────────────────────────

    def wire(self, tabs: Any, tick: Any, started: Any, refresh_events: tuple[Any, ...]) -> None:
        """자동 전환(누른 내보내기 성공) · 「최종 편집 열기」 · 작업공간이 바뀌면 다시 그리기.

        `tick`은 화면 틱에 처리기를 싣는다(W-83, D143) — 자동 전환은 화면 틱 한 번에 함께 돈다."""
        signature = self.owner._signature_state
        seen = gr.State(None)
        outputs = [tabs, self.block, self.open_button, seen]
        # 성공한 누름에서만 — 오류로 끝난 누름은 옛 작업을 무장하지 않는다.
        started.success(self.arm, [signature, seen], seen, show_progress="hidden")
        tick(self.switch_tick, [signature, seen], outputs)
        self.open_button.click(self.open_view, signature, [tabs, self.block])
        self.reload_button.click(
            self.reload, [signature, seen], outputs[1:], queue=False, show_progress="hidden"
        )
        for event in refresh_events:
            event.then(self.view, signature, self.block, show_progress="hidden")

    # ── 처리기 ────────────────────────────────────────────────────────

    def arm(self, signature: str | None, seen: dict[str, Any] | None) -> dict[str, Any]:
        """「내보내기」를 누른 이 화면만 — 그 작업이 성공하면 탭을 넘긴다(틱이 진행을 못 봐도)."""
        state = dict(seen or {})
        root = self.owner._snapshot_root()
        if root is None or not signature or signature != self.owner._signature_for(root):
            return state
        latest = self.owner.raw.latest_job(root, self.export_job_kind)
        if latest is None or latest.status in JOB_ENDED - {JOB_SUCCEEDED}:
            return state
        # 새 작업을 걸지 않고 끝난 누름(닫는 중 등)이 지난 초안으로 탭을 넘기지 않게 — 틱이 이미
        # 끝난 것으로 본 작업이나, 틱이 아직 아무것도 못 본 때의 끝난 작업은 무장하지 않는다.
        known = state.get("key")
        if latest.status in JOB_ENDED and (not known or known[0] == latest.job_id):
            return state
        state["armed"] = latest.job_id
        return state

    def switch_tick(self, signature: str | None, seen: dict[str, Any] | None) -> tuple[Any, ...]:
        """(탭, 화면, 「최종 편집 열기」, 기억) — 내보내기 작업 (아이디, 상태)가 바뀐 틱에서만."""
        skip = (gr.skip(),) * 4
        state = dict(seen or {})
        root = self.owner._snapshot_root()
        if root is None:
            if not state.get("key"):
                return skip
            return gr.skip(), gr.skip(), gr.Button(visible=False), {}
        if not signature or signature != self.owner._signature_for(root):
            return skip
        latest = self.owner.raw.latest_job(root, self.export_job_kind)
        key = [latest.job_id, latest.status] if latest else None
        armed = state.get("armed")
        if armed and latest and latest.job_id == armed and latest.status in JOB_ENDED:
            state.update(armed=None, key=key)
            if latest.status == JOB_SUCCEEDED and self.owner.raw.has_render_export(root):
                tabs = gr.Tabs(selected=FINAL_EDIT_TAB_KEY)
                return tabs, self.view(signature), gr.Button(visible=True), state
            return gr.skip(), gr.skip(), gr.skip(), state
        first = "key" not in state
        if not first and key == state["key"]:
            return skip
        state["key"] = key
        # 첫 틱은 페이지 로드가 이미 그렸다 — 다른 탭이 낸 새 초안이면 다시 그린다(탭은 그대로).
        succeeded = key is not None and key[1] == JOB_SUCCEEDED
        refreshed = self.view(signature) if succeeded and not first else gr.skip()
        return (
            gr.skip(),
            refreshed,
            gr.Button(visible=self.owner.raw.has_render_export(root)),
            state,
        )

    def reload(self, signature: str | None, seen: dict[str, Any] | None) -> tuple[Any, ...]:
        """(화면, 「최종 편집 열기」, 기억) — 지금 가장 새 초안을 다시 연다(W-71h).

        「최종 편집」 탭이 앞이면 타이머가 쉬어(D134) 틱이 새 초안을 못 본다 — 그 탭의 재생기가 자기
        렌더가 끝난 것을 보고 이것을 누른다. 기억을 그 작업으로 맞춰 타이머가 깨도 또 그리지 않는다.
        """
        state = dict(seen or {})
        root = self.owner._snapshot_root()
        if root is None or not signature or signature != self.owner._signature_for(root):
            return gr.skip(), gr.skip(), state
        latest = self.owner.raw.latest_job(root, self.export_job_kind)
        state.update(armed=None, key=[latest.job_id, latest.status] if latest else None)
        shown = gr.Button(visible=self.owner.raw.has_render_export(root))
        return self.view(signature), shown, state

    def open_view(self, signature: str | None) -> tuple[Any, str]:
        root = self.owner._require_current_selection(signature)
        if not self.owner.raw.has_render_export(root):
            raise gr.Error(
                "아직 내보낸 초안이 없습니다 — 「추천만 이어붙여 내보내기」를 먼저 하세요."
            )
        return gr.Tabs(selected=FINAL_EDIT_TAB_KEY), self.view(signature)

    def view(self, signature: str | None) -> str:
        """지금 작업공간의 가장 새 초안 — 없거나 못 읽으면 까닭을 말하는 빈 화면."""
        root = self.owner._snapshot_root()
        if root is None or not signature or signature != self.owner._signature_for(root):
            return final_edit_html(self._empty_model(NO_WORKSPACE_TEXT))
        if not self.owner.raw.has_render_export(root):
            return final_edit_html(self._empty_model(NO_EXPORT_TEXT))
        try:
            edit = self.owner.raw.final_edit_open(root)
        except self.service_error as error:
            return final_edit_html(self._empty_model(f"초안을 열지 못했습니다 — {error}"))
        return final_edit_html(self._model(signature, edit))

    def _model(self, signature: str, edit: dict[str, Any]) -> dict[str, Any]:
        return {
            "epoch": uuid.uuid4().hex,
            "signature": signature,
            "token": self.owner._visual_lifetime_token,
            "edit": edit,
            "keymap": keymap_payload(),
            "empty": None,
        }

    def _empty_model(self, text: str) -> dict[str, Any]:
        return {"epoch": uuid.uuid4().hex, "empty": text}

    # ── 서버 함수(클라이언트 `server.fe_*`) ──────────────────────────

    def server_functions(self) -> list[Callable[[dict[str, object]], dict[str, object]]]:
        def fe_load(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "load")

        def fe_save(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "save")

        def fe_waveform(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "waveform")

        return [fe_load, fe_save, fe_waveform]

    def action(self, payload: dict[str, object], kind: str) -> dict[str, object]:
        """어떤 예외도 500이 아니라 값으로 돌아간다. 저장은 서명·수명 토큰에 묶인다."""
        owner = self.owner

        def fail(error: object, *, stale: bool = False) -> dict[str, object]:
            return {"ok": False, "error": owner._serve_error(error), "stale": stale}

        try:
            root = owner._require_current_selection(owner._serve_str(payload, "signature") or None)
            if kind == "save":
                owner._require_workbench_token(payload, owner._visual_lifetime_token, TAB_LABEL)
        except gr.Error as error:
            return fail(error, stale=True)
        export_id = owner._serve_str(payload, "exportId") or None
        try:
            if kind == "load":
                reseed = payload.get("reseed") is True
                return {
                    "ok": True,
                    "edit": owner.raw.final_edit_open(root, export_id, reseed=reseed),
                }
            if kind == "waveform":
                return {"ok": True, "waveform": owner.raw.final_edit_waveform(root, export_id)}
            return self._save(root, export_id, payload)
        except (
            self.service_error,
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
            OSError,
        ) as error:
            return fail(error)

    def _save(
        self, root: Any, export_id: str | None, payload: dict[str, object]
    ) -> dict[str, object]:
        base = payload.get("baseRevision")
        document = payload.get("doc")
        if export_id is None or not isinstance(base, int) or not isinstance(document, dict):
            raise ValueError("저장 요청의 모양이 틀렸습니다(초안 · 리비전 · 문서).")
        result = self.owner.raw.final_edit_save(
            root, export_id, base, document, force=payload.get("force") is True
        )
        return {"ok": True, **result}
