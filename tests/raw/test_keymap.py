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
    seen: dict[tuple[object, ...], str] = {}
    for key in keymap.PLAYER_KEYMAP:
        slot = (key.code, key.mod, key.shift, key.alt, key.in_text)
        panes = {key.pane} if key.pane != keymap.ANY else {keymap.TIMELINE, keymap.CAPTIONS}
        for pane in panes:
            assert (slot, pane) not in seen, (slot, pane, seen.get((slot, pane)))
            seen[(slot, pane)] = key.action


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
