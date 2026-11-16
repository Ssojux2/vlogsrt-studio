"""파형 자막 레인의 순수 계산(W-87b, D149) — 노드에서 실제로 돈다."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import caption_lane_js

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

_LANE = r"""
const L = require(process.argv[2]);
const view = { start: 1000, tpp: 10 };       // 레인 0px = 1000틱, 1px = 10틱
const cues = [
  ["a", "s", 1000, 2000, "", false],          // x 0–100
  ["b", "s", 2400, 3000, "", false],          // x 140–200
  ["c", "s", 3080, 4000, "", false],          // x 208–300
];
const out = {};
out.hit = [L.hit(cues, view, 2, 4), L.hit(cues, view, 98, 4), L.hit(cues, view, 50, 4),
  L.hit(cues, view, 120, 4), L.hit(cues, view, 204, 4)];
out.snap = [L.snap(2015, [2000, 2400], 30), L.snap(2100, [2000, 2400], 30)];
// 가장자리: b의 시작을 앞으로 1000틱까지 → a 끝 + 간격 80 · b의 끝을 뒤로 → c 시작 - 80
out.edge = [L.edge(cues, 1, "start", 1000, 80, 200, 9999),
  L.edge(cues, 1, "end", 5000, 80, 200, 9999),
  L.edge(cues, 1, "end", 2450, 80, 200, 9999), L.edge(cues, 0, "start", -50, 80, 200, 9999),
  L.edge(cues, 2, "end", 99999, 80, 200, 5000)];
// 몸통: b를 -900 → a 끝 + 80까지(-320) · +500 → c 시작 - 80까지(0)
out.body = [L.body(cues, 1, -900, 80, 9999), L.body(cues, 1, 500, 80, 9999),
  L.body(cues, 0, -5000, 80, 9999)];
out.range = [L.range(3000, 1200, 2500), L.range(-10, 50, 2500)];
const z = L.zoom(view, 2, 100, 1, 100);
out.zoom = [z.start, z.tpp, L.ticksAt(z, 100), L.zoom(view, 1000, 0, 1, 100).tpp];
out.follow = [L.follow(view, 1500, 300).start, L.follow(view, 9000, 300).start];
console.log(JSON.stringify(out));
"""


@needs_node
def test_lane_geometry_clamps_and_snaps(tmp_path: Path) -> None:
    core = tmp_path / "lane.js"
    core.write_text(caption_lane_js.CAPTION_LANE_JS, encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_LANE, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    got = json.loads(done.stdout)
    assert got["hit"] == [
        {"index": 0, "id": "a", "part": "start"},
        {"index": 0, "id": "a", "part": "end"},
        {"index": 0, "id": "a", "part": "body"},
        None,
        {"index": 1, "id": "b", "part": "end"},  # b 끝(200)과 c 시작(208)의 가운데 앞 — 가까운 쪽
    ]
    assert got["snap"] == [2000, 2100]
    assert got["edge"] == [2080, 3000, 2600, 0, 5000]
    assert got["body"] == [-320, 0, -1000]
    assert got["range"] == [[1200, 2500], [0, 50]]
    assert got["zoom"] == [1500, 5, 2000, 1]
    assert got["follow"] == [1000, 7500]


def test_the_lane_core_never_touches_the_document() -> None:
    js = caption_lane_js.CAPTION_LANE_JS
    assert "TL." not in js and "doc" not in js.replace("문서", "")


_WHEEL = r"""
const L = require(process.argv[2]);
console.log(JSON.stringify([L.wheelFactor(-100, 0, 125, 100), L.wheelFactor(100, 0, 125, 100),
  L.wheelFactor(-3, 1, 125, 100), L.wheelFactor(0, 0, 125, 100)]));
"""


@needs_node
def test_wheel_zoom_is_one_step_per_notch(tmp_path: Path) -> None:
    core = tmp_path / "lane.js"
    core.write_text(caption_lane_js.CAPTION_LANE_JS, encoding="utf-8")
    runner = tmp_path / "wheel.js"
    runner.write_text(_WHEEL, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(done.stdout) == [1.25, 0.8, 1.25, 1]
