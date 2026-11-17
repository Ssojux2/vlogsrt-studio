"""재생기 키 표 하나(W-89, D154) — 판정은 노드에서 실제로 돈다."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import keymap

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

_MATCH = r"""
const K = require(process.argv[2]);
const T = K.table;
const ev = (code, o) => Object.assign({ code: code, metaKey: false, ctrlKey: false,
  shiftKey: false, altKey: false }, o || {});
console.log(JSON.stringify([
  K.match(T, ev("Backspace"), "captions", false),
  K.match(T, ev("Backspace"), "timeline", false),
  K.match(T, ev("KeyA"), "captions", false),
  K.match(T, ev("KeyA"), "timeline", false),
  K.match(T, ev("BracketLeft"), "timeline", false),
  K.match(T, ev("BracketLeft", { altKey: true }), "timeline", false),
  K.match(T, ev("KeyB", { metaKey: true }), "timeline", false),
  K.match(T, ev("KeyB", { ctrlKey: true }), "timeline", false),
  K.match(T, ev("KeyB", { shiftKey: true }), "timeline", false),
  K.match(T, ev("KeyZ", { metaKey: true, shiftKey: true }), "captions", false),
  K.match(T, ev("KeyS"), "captions", true),
  K.match(T, ev("KeyS", { metaKey: true }), "captions", true),
  K.match(T, ev("Escape"), "captions", true),
  K.match(T, ev("Comma", { shiftKey: true }), "timeline", false),
  K.match(T, ev("", { key: " " }), "timeline", false),
  K.match(T, ev("KeyQ"), "captions", false),
]));
"""


@needs_node
def test_the_pane_and_exact_modifiers_pick_one_action(tmp_path: Path) -> None:
    """⌫는 창마다 뜻이 다르다(자막 창 — 줄, 타임라인 창 — 클립) · A는 자막 창만 · [ 는 어디서나 ·
    수식키는 정확히(⌥[ 는 다듬기, ⇧B는 나누기) · Mod는 ⌘이든 Ctrl이든 · 글 칸 안은 `inText` 줄만."""
    core = tmp_path / "keys.js"
    core.write_text(keymap.keymap_js(), encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_MATCH, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(done.stdout) == [
        "delete-cue",
        "delete-clip",
        "cue-start",
        None,
        "cue-start",
        "trim-start",
        "blade",
        "blade",
        "cue-split",
        "redo",
        None,
        "save",
        "leave-text",
        "cue-big-back",
        "toggle",
        None,
    ]


def test_every_action_in_the_table_has_a_handler_and_no_key_is_bound_twice() -> None:
    from ui.timeline_player_js import PLAYER_APP_JS

    body = PLAYER_APP_JS[PLAYER_APP_JS.index("S.keyActions = {") :]
    body = body[: body.index("return S.keyActions;")]
    for action in {key.action for key in keymap.PLAYER_KEYMAP}:
        assert f'"{action}":' in body or f" {action}:" in body, action
    # 한 창(또는 「어디서나」) 안에서 같은 키는 한 줄뿐이다. 창의 줄은 그 창에서 「어디서나」 줄을
    # 앞선다(자막 창의 ← · → 는 Subtitle Edit 걸음) — 덮는 것은 표에 적은 그 짝뿐이다.
    seen: dict[tuple[object, ...], str] = {}
    shadowed = set()
    for key in keymap.PLAYER_KEYMAP:
        slot = (key.code, key.mod, key.shift, key.alt, key.in_text)
        assert (slot, key.pane) not in seen, (slot, key.pane, seen.get((slot, key.pane)))
        seen[(slot, key.pane)] = key.action
    for (slot, pane), action in seen.items():
        if pane != keymap.ANY and (slot, keymap.ANY) in seen:
            shadowed.add((seen[(slot, keymap.ANY)], pane, action))
    assert shadowed == {
        ("frame-back", keymap.CAPTIONS, "cue-step-back"),
        ("frame-forward", keymap.CAPTIONS, "cue-step-forward"),
    }


def test_the_sheet_is_built_from_the_table_and_follows_the_platform() -> None:
    mac = keymap.keysheet_html("darwin")
    other = keymap.keysheet_html("win32")
    for label in {key.label for key in keymap.PLAYER_KEYMAP}:
        assert label in mac
    assert "<kbd>⌘B</kbd>" in mac and "<kbd>Ctrl+B</kbd>" in other
    assert "⌘" not in other
    assert ">타임라인<" in mac and ">자막<" in mac and ">어디서나<" in mac


def test_a_frame_nudge_lands_on_the_target_frame_through_document_operations() -> None:
    """, · . (D138이 남긴 프레임 밀기) — 「시작점 설정」과 같은 규칙
    (`TL.sourceMsAt` 프레임 시작)으로 그 프레임에 놓고, 반내림이 앞 프레임으로 돌아오면 1ms
    뒤로. 이웃 · 간격은 `LN.bodyMany`가 막고, 옮기기는 `TL.cueShift` 하나다(규칙 3 —
    고른 틱을 문서 연산에 넘길 뿐)."""
    from ui.timeline_player_js import PLAYER_APP_JS

    body = PLAYER_APP_JS[PLAYER_APP_JS.index("function nudgeCues(frames) {") :]
    body = body[: body.index("  function ", 40)]
    assert "LN.bodyMany(S.proj.cues, indices, frames * seqTicks()" in body
    assert "TL.cueShift(doc, id, ms)" in body
    rule = PLAYER_APP_JS[PLAYER_APP_JS.index("function nudgeMs(") :][:900]
    assert "TL.sourceMsAt(S.doc, clamp(frame, 0, S.total) * seqTicks(), TL.SIDE_START)" in rule
    assert "< frame ? ms + 1 : ms" in rule


_MORE = r"""
const K = require(process.argv[2]);
const T = K.table;
const ev = (code, o) => Object.assign({ code: code, metaKey: false, ctrlKey: false,
  shiftKey: false, altKey: false }, o || {});
const rates = [1, 2, 4, 8];
let state = null;
const shuttle = [];
for (const dir of [1, 1, 1, 1, 1, -1, -1, 1]) {
  state = K.shuttle(state, dir, rates);
  shuttle.push([state.dir, state.rate]);
}
console.log(JSON.stringify({
  match: [
    K.match(T, ev("ArrowLeft"), "captions", false),
    K.match(T, ev("ArrowLeft"), "timeline", false),
    K.match(T, ev("ArrowRight", { altKey: true }), "captions", false),
    K.match(T, ev("ArrowLeft", { shiftKey: true }), "captions", false),
    K.match(T, ev("KeyJ"), "captions", false),
    K.match(T, ev("KeyL"), "timeline", false),
    K.match(T, ev("KeyI"), "timeline", false),
    K.match(T, ev("KeyI"), "captions", false),
    K.match(T, ev("KeyX", { altKey: true }), "timeline", false),
    K.match(T, ev("Equal", { metaKey: true }), "timeline", false),
    K.match(T, ev("Minus"), "captions", false),
    K.match(T, ev("KeyZ", { shiftKey: true }), "timeline", false),
    K.match(T, ev("KeyN"), "captions", false),
    K.match(T, ev("KeyE", { ctrlKey: true }), "timeline", false),
    K.match(T, ev("KeyE", { metaKey: true, shiftKey: true }), "captions", false),
    K.match(T, ev("KeyV", { metaKey: true, altKey: true }), "captions", true),
    K.match(T, ev("KeyV", { metaKey: true, altKey: true }), "timeline", true),
    K.match(T, ev("KeyJ"), "captions", true),
  ],
  shuttle: shuttle,
}));
"""


@needs_node
def test_final_cut_and_subtitle_edit_keys_reach_their_actions(tmp_path: Path) -> None:
    """REA로 본 두 앱의 키 — 자막 창의 ← · → 는 걸음(창의 줄이 먼저), 타임라인 창은 프레임 ·
    J · L 셔틀 · I · O · ⌥X 범위(타임라인 창만) · ⌘= · = · ⇧Z 보기 · N 스내핑 · ⌘E 공유 ·
    ⌘⌥V 커서 나누기(글 칸 안 · 자막 창만). L을 거듭 누르면 배속이 오르다 끝에서 멈추고, 쪽을
    바꾸면 첫 배속이다."""
    core = tmp_path / "keys.js"
    core.write_text(keymap.keymap_js(), encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_MORE, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    got = json.loads(done.stdout)
    assert got["match"] == [
        "cue-step-back",
        "frame-back",
        "cue-small-forward",
        "big-back",
        "shuttle-back",
        "shuttle-forward",
        "mark-in",
        None,
        "mark-clear",
        "zoom-in",
        "zoom-out",
        "zoom-fit",
        "snapping",
        "share",
        "cue-extend",
        "cue-split-caret",
        None,
        None,
    ]
    assert got["shuttle"] == [[1, 1], [1, 2], [1, 4], [1, 8], [1, 8], [-1, 1], [-1, 2], [1, 1]]


def test_caption_step_labels_name_the_settings_defaults() -> None:
    """키 이름의 걸음은 설정 기본값과 같다 — 재생기는 설정 값으로 걷는다(규칙 8)."""
    from fractions import Fraction

    from vlogsrt.project.raw.final_edit_config import FINAL_EDIT_SETTINGS

    def seconds(label: str) -> Fraction:
        return Fraction(label.removesuffix("초"))

    assert seconds(keymap._STEP) == Fraction(FINAL_EDIT_SETTINGS.caption_step_ms, 1000)
    assert seconds(keymap._SMALL) == Fraction(FINAL_EDIT_SETTINGS.caption_small_step_ms, 1000)


def test_the_sheet_lists_the_window_keys_with_alt_not_command() -> None:
    """⌘1–4는 브라우저 탭 바꾸기라 페이지가 막지 못한다 — 창 옮기기는 ⌥1–4다."""
    mac = keymap.keysheet_html("darwin")
    other = keymap.keysheet_html("win32")
    assert ">창 옮기기<" in mac
    assert "<kbd>⌥1</kbd>" in mac and "<kbd>Alt+4</kbd>" in other
    assert "⌘1" not in mac
