"""Subtitle Edit식 자막 목록·입력칸·키(W-87, D146) — 순수 표 계산은 노드에서, 배선은 글자로."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from ui import caption_grid_js, timeline_player  # noqa: E402
from ui.timeline_edit_js import TIMELINE_CORE_JS  # noqa: E402
from ui.timeline_player_js import PLAYER_APP_JS  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

_GRID = r"""
const G = require(process.argv[2]);
const TL = require(process.argv[3]);
// 초당 1000틱 · 프레임 40틱(25fps). [id, 원본, 시작, 끝, 글, 잘림]
const cues = [
  ["a", "s1", 0, 2000, "안녕하세요\n반가워요", false],
  ["b", "s1", 2040, 2400, "", false],
  ["c", "s1", 2380, 9000, "길게 이어지는 말", true],
];
const warns = { b: ["empty", "short", "overlap"], c: ["long"] };
const rows = G.rows(cues, warns, 40, 1000, TL.textChars);
console.log(JSON.stringify({
  rows: rows.map((r) => [r.id, r.n, r.startFrame, r.endFrame, r.seconds,
    r.chars, r.cps === null ? null : Math.round(r.cps * 100) / 100,
    r.gapSeconds, r.clipped, r.bad, r.warned]),
  next: [G.nextError(rows, null, 1), G.nextError(rows, "b", 1), G.nextError(rows, "c", 1),
    G.nextError(rows, null, -1), G.nextError(rows, "b", -1)],
  lines: G.lineLengths("안녕 하세요\n반가워요", TL.textChars),
  one: G.oneLine("가\n나"),
}));
"""


@needs_node
def test_rows_show_seconds_gap_cps_and_paint_only_the_warned_cell(tmp_path: Path) -> None:
    grid = tmp_path / "grid.js"
    grid.write_text(caption_grid_js.CAPTION_GRID_JS, encoding="utf-8")
    core = tmp_path / "core.js"
    core.write_text(TIMELINE_CORE_JS, encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_GRID, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(grid), str(core)],
        capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    got = json.loads(done.stdout)
    ok = {"text": False, "dur": False, "cps": False, "gap": False}
    assert got["rows"] == [
        ["a", 1, 0, 50, 2.0, 9, 4.5, 0.04, False, ok, False],
        ["b", 2, 51, 60, 0.36, 0, 0, -0.02, False,
         {"text": True, "dur": True, "cps": False, "gap": True}, True],
        ["c", 3, 59, 225, 6.62, 7, 1.06, None, True, {**ok, "dur": True}, True],
    ]  # fmt: skip
    # F8 — 처음부터는 b, b 다음은 c, c 다음은 없음 · ⇧F8 — 끝부터는 c, b 앞은 없음.
    assert got["next"] == ["b", "c", None, "c", None]
    assert got["lines"] == [5, 4]
    assert got["one"] == "가 ⏎ 나"


def test_the_grid_rides_with_the_player_and_never_moves_time() -> None:
    """표 계산은 재생기와 함께 실리고, 문서를 고치는 것은 여전히 `TL.cueEdit` 하나다."""
    assert caption_grid_js.CAPTION_GRID_JS in timeline_player.PLAYER_JS
    js = caption_grid_js.CAPTION_GRID_JS
    assert "TL.cue" not in js and "doc." not in js


def test_subtitle_edit_keys_reach_the_document_operations() -> None:
    """A·F11 시작 · D·F12 끝 · S 나누기 · T·⌘T 입력칸 · F8·⇧F8 경고 줄 · ⌘⇧E 늘리기 · ⌘⇧M 합치기."""
    # 키는 재생기 표 하나(W-89, D154) — 같은 키가 같은 동작 이름에 닿고, 동작이 문서 연산을 부른다.
    from ui.keymap import PLAYER_KEYMAP

    js = PLAYER_APP_JS
    rows = {(k.code, k.mod, k.shift): (k.action, k.pane) for k in PLAYER_KEYMAP if not k.in_text}
    assert rows[("KeyA", False, False)] == ("cue-start", "captions")
    assert rows[("F11", False, False)] == ("cue-start", "captions")
    assert rows[("KeyD", False, False)] == ("cue-end", "captions")
    assert rows[("F12", False, False)] == ("cue-end", "captions")
    assert rows[("KeyS", False, False)] == ("cue-split", "captions")
    assert rows[("KeyT", False, False)] == ("edit-text", "captions")
    assert rows[("KeyT", True, False)] == ("edit-text", "captions")
    assert rows[("F8", False, False)] == ("next-error", "any")
    assert rows[("F8", False, True)] == ("prev-error", "any")
    assert rows[("KeyE", True, True)] == ("cue-extend", "captions")
    assert rows[("KeyM", True, True)] == ("cue-merge", "captions")
    in_text = {(k.code, k.mod): k.action for k in PLAYER_KEYMAP if k.in_text}
    assert in_text[("Escape", False)] == in_text[("Enter", True)] == "leave-text"
    assert in_text[("KeyT", True)] == "leave-text"
    for wiring in (
        '"cue-start": function () { cueOp("start"); }',
        '"cue-end": function () { cueOp("end"); }',
        '"cue-split": function () { cueOp("split"); }',
        '"cue-merge": function () { cueOp("merge"); }',
        '"cue-extend": extendToNext',
        '"next-error": function () { stepError(1); }',
        '"prev-error": function () { stepError(-1); }',
        '"leave-text": leaveText',
    ):
        assert wiring in js, wiring
    # 다음 줄까지 늘리기 · 입력칸 시각은 플레이헤드 대신 그 프레임을 문서 연산에 넘긴다.
    assert 'cueOp("end", id, undefined, Math.floor(next[2] / seqTicks()));' in js
    assert 'cueOp(input.getAttribute("data-side"), id, undefined, frame);' in js
    assert "frame === undefined ? S.playhead : frame" in js


def test_the_editor_carries_time_fields_and_cps_and_line_lengths() -> None:
    js = PLAYER_APP_JS
    for needle in ('id="tl-cue-start"', 'id="tl-cue-end"', 'id="tl-cue-dur"', 'id="tl-cue-stats"'):
        assert needle in js, needle
    assert 'input.addEventListener("change", commitTime)' in js
    # 고치는 중인 시각 칸은 덮지 않는다(글 칸과 같은 규칙).
    assert "document.activeElement !== input" in js
    css = timeline_player.TIMELINE_PLAYER_CSS
    assert ".tl-cues .is-bad { background: var(--vl-danger-soft);" in css
    head = re.search(r"grid-template-columns: ([^;]+);", css[css.index(".tl-cue-grid-head") :])
    assert head is not None and head.group(1).count(" ") >= 6
