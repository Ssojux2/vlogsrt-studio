"""원본 범위 타임라인 재생기의 키 표 하나(W-89, D154) — 포커스한 창(pane)이 정한다.

Final Cut은 키가 창마다 뜻이 다르다(⌫ — 브라우저는 제외, 타임라인은 삭제). Subtitle Edit은
자막 창에서 A·D·S·T를 쓴다. 그래서 재생기의 키를 한 표에 적고 줄마다 창을 단다:

- `timeline` — 클립 띠 · 원본 띠 · 이동 막대 · 화면(클립 편집 — Final Cut 키)
- `captions` — 자막 목록 · 입력칸 · 파형 레인(Subtitle Edit 키)
- `any` — 둘 다

재생기는 마지막으로 누른 창(또는 키를 받은 칸이 든 창)을 들고 `__vlKeys.match`로 동작 이름을
얻는다 — 판정은 순수하다(노드 시험). 표기는 macOS 글자로 적고 화면은 `platform_keys.display`가
운영체제마다 바꾼다(Mod = ⌘ · Ctrl). 브라우저가 먼저 가져갈 수 있는 키(⌘T 새 탭 · ⌘⇧M 프로필)는
같은 일을 하는 수식키 없는 짝(T · ⇧M)을 함께 둔다 — 🧑 측정(`scripts/key_intercept_probe.html`)
뒤에 표만 고친다.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from ui import platform_keys, window_js

ANY, TIMELINE, CAPTIONS = "any", "timeline", "captions"
PANE_LABELS = {ANY: "어디서나", TIMELINE: "타임라인", CAPTIONS: "자막"}


@dataclass(frozen=True, slots=True)
class PlayerKey:
    """키 한 줄 — 수식키는 정확히 같아야 맞는다. `in_text`면 글 칸 안에서도 받는다."""

    code: str
    action: str
    label: str
    keys: str
    pane: str = ANY
    mod: bool = False
    shift: bool = False
    alt: bool = False
    in_text: bool = False


def _k(code: str, action: str, label: str, keys: str, pane: str = ANY, **flags: bool) -> PlayerKey:
    return PlayerKey(code, action, label, keys, pane, **flags)


# 자막 창 걸음의 이름 — 재생기는 설정(`captionStepMs` · `captionSmallStepMs`)의 값으로 걷고, 이름은
# 그 기본값이다(이 모듈은 project 계층을 모른다 — 시험이 설정과 같음을 못 박는다).
_STEP = "1초"
_SMALL = "0.5초"


PLAYER_KEYMAP: tuple[PlayerKey, ...] = (
    # 글 칸 안(확정 · 나가기)
    _k("Escape", "leave-text", "글 확정하고 나가기", "Esc", CAPTIONS, in_text=True),
    _k("Enter", "leave-text", "글 확정하고 나가기", "⌘Enter", CAPTIONS, mod=True, in_text=True),
    _k("KeyT", "leave-text", "입력칸 ↔ 목록", "⌘T", CAPTIONS, mod=True, in_text=True),
    # 재생(어디서나 — Final Cut)
    _k("Space", "toggle", "재생 · 멈춤", "Space"),
    _k("KeyK", "stop", "멈춤", "K"),
    _k("KeyJ", "shuttle-back", "거꾸로 재생(거듭 누르면 빨라진다)", "J"),
    _k("KeyL", "shuttle-forward", "재생(거듭 누르면 빨라진다)", "L"),
    # 자막 창의 ← · → 는 Subtitle Edit 걸음이다 — 창이 정한 줄이 「어디서나」 줄보다 먼저다.
    _k("ArrowLeft", "cue-step-back", _STEP + " 뒤로", "←", CAPTIONS),
    _k("ArrowRight", "cue-step-forward", _STEP + " 앞으로", "→", CAPTIONS),
    _k("ArrowLeft", "cue-small-back", _SMALL + " 뒤로", "⌥←", CAPTIONS, alt=True),
    _k("ArrowRight", "cue-small-forward", _SMALL + " 앞으로", "⌥→", CAPTIONS, alt=True),
    _k("ArrowLeft", "frame-back", "1프레임 뒤로", "←"),
    _k("ArrowRight", "frame-forward", "1프레임 앞으로", "→"),
    _k("ArrowLeft", "big-back", "10프레임 뒤로", "⇧←", shift=True),
    _k("ArrowRight", "big-forward", "10프레임 앞으로", "⇧→", shift=True),
    _k("ArrowUp", "prev-cut", "이전 컷", "↑"),
    _k("ArrowDown", "next-cut", "다음 컷", "↓"),
    _k("Home", "go-start", "처음으로", "Home"),
    _k("End", "go-end", "끝으로", "End"),
    # 편집(어디서나)
    _k("KeyZ", "undo", "실행 취소", "⌘Z", mod=True),
    _k("KeyZ", "redo", "실행 복귀", "⌘⇧Z", mod=True, shift=True),
    _k("KeyS", "save", "지금 저장", "⌘S", mod=True, in_text=True),
    _k("Digit2", "index", "타임라인 인덱스", "⇧⌘2", mod=True, shift=True),
    # 보기(어디서나 — Final Cut). ⌘= · ⌘− 는 브라우저 확대와 겹쳐 수식키 없는 짝을 함께 둔다.
    _k("Equal", "zoom-in", "확대", "⌘=", mod=True),
    _k("Equal", "zoom-in", "확대", "="),
    _k("Minus", "zoom-out", "축소", "⌘−", mod=True),
    _k("Minus", "zoom-out", "축소", "−"),
    _k("KeyZ", "zoom-fit", "창에 맞추기", "⇧Z", shift=True),
    _k("KeyN", "snapping", "스내핑 켜기 · 끄기", "N"),
    _k("KeyE", "share", "공유(번인 캡션 칸으로)", "⌘E", mod=True),
    # 타임라인 창(클립 — Final Cut)
    _k("KeyB", "blade", "블레이드(플레이헤드에서 클립 자르기)", "⌘B", TIMELINE, mod=True),
    _k("Backspace", "delete-clip", "삭제(범위 또는 고른 클립 — 뒤가 당겨 온다)", "⌫", TIMELINE),
    _k("Delete", "delete-clip", "삭제(범위 또는 고른 클립 — 뒤가 당겨 온다)", "⌦", TIMELINE),
    _k("KeyI", "mark-in", "범위 시작 설정(플레이헤드)", "I", TIMELINE),
    _k("KeyO", "mark-out", "범위 끝 설정(플레이헤드)", "O", TIMELINE),
    _k("KeyX", "mark-clear", "범위 지우기", "⌥X", TIMELINE, alt=True),
    _k("BracketLeft", "trim-start", "클립 앞을 플레이헤드로", "⌥[", TIMELINE, alt=True),
    _k("BracketRight", "trim-end", "클립 뒤를 플레이헤드로", "⌥]", TIMELINE, alt=True),
    _k("KeyC", "cue-insert", "캡션 추가(플레이헤드에 빈 자막)", "⌥C", alt=True),
    # 자막(Subtitle Edit — 사용자 A·D·S·T와 기본 F11·F12·F8). 클립 키와 겹치지 않는 것은 어디서나
    # 그대로(D138 · D146), 겹치거나 Final Cut 도구 글자인 것(⌫ · A · D · S · T · Enter)은 자막 창만.
    _k("BracketLeft", "cue-start", "시작점 설정(플레이헤드)", "["),
    _k("KeyA", "cue-start", "시작점 설정(플레이헤드)", "A", CAPTIONS),
    _k("F11", "cue-start", "시작점 설정(플레이헤드)", "F11", CAPTIONS),
    _k("BracketRight", "cue-end", "종료점 설정(플레이헤드)", "]"),
    _k("KeyD", "cue-end", "종료점 설정(플레이헤드)", "D", CAPTIONS),
    _k("F12", "cue-end", "종료점 설정(플레이헤드)", "F12", CAPTIONS),
    _k("KeyS", "cue-split", "줄 나누기(플레이헤드)", "S", CAPTIONS),
    _k("KeyB", "cue-split", "줄 나누기(플레이헤드)", "⇧B", shift=True),
    _k("KeyM", "cue-merge", "다음 줄과 합치기", "⇧M", shift=True),
    _k("KeyM", "cue-merge", "다음 줄과 합치기", "⌘⇧M", CAPTIONS, mod=True, shift=True),
    _k("KeyE", "cue-extend", "다음 줄까지 늘리기", "⌘⇧E", CAPTIONS, mod=True, shift=True),
    _k("ArrowUp", "prev-cue", "이전 줄", "⌥↑", alt=True),
    _k("ArrowDown", "next-cue", "다음 줄", "⌥↓", alt=True),
    _k("F8", "next-error", "다음 오류", "F8"),
    _k("F8", "prev-error", "이전 오류", "⇧F8", shift=True),
    _k("Comma", "cue-nudge-back", "고른 줄 1프레임 앞당기기", ","),
    _k("Period", "cue-nudge-forward", "고른 줄 1프레임 미루기", "."),
    _k("Comma", "cue-big-back", "고른 줄 10프레임 앞당기기", "⇧,", shift=True),
    _k("Period", "cue-big-forward", "고른 줄 10프레임 미루기", "⇧.", shift=True),
    _k("Enter", "edit-text", "입력칸으로", "Enter", CAPTIONS),
    _k("KeyT", "edit-text", "입력칸으로", "T", CAPTIONS),
    _k("KeyT", "edit-text", "입력칸으로", "⌘T", CAPTIONS, mod=True),
    _k(
        "KeyV",
        "cue-split-caret",
        "커서에서 줄 나누기",
        "⌘⌥V",
        CAPTIONS,
        mod=True,
        alt=True,
        in_text=True,
    ),
    _k("Backspace", "delete-cue", "고른 줄 지우기", "⌫", CAPTIONS),
    _k("Delete", "delete-cue", "고른 줄 지우기", "⌦", CAPTIONS),
    _k("Escape", "clear-cue", "고름 풀기", "Esc"),
)

KEYS_CORE_JS = r"""
(function (root) {
  var K = root.__vlKeys || {};
  // 키 하나 → 동작 이름(없으면 null). 수식키는 정확히 같아야 하고(Mod = ⌘ 또는 Ctrl), 창은 그
  // 줄의 창이거나 「any」, 글 칸 안이면 `inText` 줄만 맞는다. 그 창의 줄이 「any」 줄보다 먼저이고
  // (자막 창의 ← · → 는 Subtitle Edit 걸음이다), 같은 창 안에서는 표의 앞 줄이 먼저다.
  K.match = function (table, ev, pane, inText) {
    var mod = !!(ev.metaKey || ev.ctrlKey);
    var code = ev.code || (ev.key === " " ? "Space" : "");
    var fallback = null;
    for (var i = 0; i < table.length; i += 1) {
      var row = table[i];
      if (row.code !== code || row.mod !== mod || row.shift !== !!ev.shiftKey ||
        row.alt !== !!ev.altKey) { continue; }
      if (inText && !row.inText) { continue; }
      if (row.pane === pane) { return row.action; }
      if (row.pane === "any" && fallback === null) { fallback = row.action; }
    }
    return fallback;
  };
  // J · L 셔틀(Final Cut) — 같은 쪽을 거듭 누르면 다음 배속(끝에서 멈춘다), 다른 쪽이면 그 쪽 첫
  // 배속. dir은 1(앞) · -1(뒤) · 0(멈춤)이다.
  K.shuttle = function (state, dir, rates) {
    var stepAt = state && state.dir === dir ? Math.min(state.step + 1, rates.length - 1) : 0;
    return { dir: dir, step: stepAt, rate: rates[stepAt] };
  };
  root.__vlKeys = K;
  if (typeof module !== "undefined" && module.exports) { module.exports = K; }
})(typeof window !== "undefined" ? window : globalThis);
"""


def keymap_rows() -> list[dict[str, object]]:
    """브라우저가 받는 표 — 이름은 JS 그대로(camelCase)."""
    return [
        {
            "code": key.code,
            "action": key.action,
            "pane": key.pane,
            "mod": key.mod,
            "shift": key.shift,
            "alt": key.alt,
            "inText": key.in_text,
        }
        for key in PLAYER_KEYMAP
    ]


def keymap_js() -> str:
    """순수 판정 + 표(데이터) — 재생기 실행본보다 앞에 싣는다."""
    table = json.dumps(keymap_rows(), ensure_ascii=False, separators=(",", ":"))
    holder = "(typeof window !== 'undefined' ? window : globalThis)"
    return f"{KEYS_CORE_JS}\n{holder}.__vlKeys.table = {table};\n"


def keysheet_html(platform: str | None = None) -> str:
    """재생기의 단축키 시트 — 창마다 묶고 같은 동작의 키를 한 줄에 모은다(표에서 짓는다)."""
    return (
        '<details class="tl-keys"><summary>단축키 — 마지막으로 누른 창이 정한다</summary>'
        f"{keysheet_sections_html(platform)}</details>"
    )


def keysheet_sections_html(platform: str | None = None) -> str:
    """시트의 몸(창마다 제목 + 표) — 머리줄 「단축키」 판이 싣는다(W-102, D175)."""
    parts = []
    for pane in (ANY, TIMELINE, CAPTIONS):
        merged: dict[str, list[str]] = {}
        for key in PLAYER_KEYMAP:
            if key.pane == pane:
                merged.setdefault(key.label, []).append(platform_keys.display(key.keys, platform))
        rows = "".join(
            f"<dt>{' · '.join(f'<kbd>{html.escape(k)}</kbd>' for k in dict.fromkeys(keys))}</dt>"
            f"<dd>{html.escape(label)}</dd>"
            for label, keys in merged.items()
        )
        parts.append(f"<h4>{html.escape(PANE_LABELS[pane])}</h4><dl>{rows}</dl>")
    windows = "".join(
        f"<dt><kbd>{html.escape(platform_keys.display(keys, platform))}</kbd></dt>"
        f"<dd>{html.escape(label)}</dd>"
        for _code, _pane, label, keys in window_js.PANE_KEYS
    )
    parts.append(f"<h4>창 옮기기</h4><dl>{windows}</dl>")
    return "".join(parts)
