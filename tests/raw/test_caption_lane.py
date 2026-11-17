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


_PAIRS = r"""
const L = require(process.argv[2]);
const cues = [
  ["a", "s", 1000, 2000, "", false],
  ["b", "s", 2400, 3000, "", false],
  ["c", "s", 3080, 4000, "", false],
];
const out = {};
// b 끝을 3500으로 ⌥끌기 → c 시작도 간격 80 그대로 3580 · 너무 멀리 → c가 최소 길이 200을 남긴다
out.pair = [L.edgePair(cues, 1, "end", 3500, 200), L.edgePair(cues, 1, "end", 9999, 200),
  // b 시작을 앞으로 1500 → a 끝도 간격 400 그대로 1100 · a 시작+200+400 = 1600이 바닥
  L.edgePair(cues, 1, "start", 1500, 200), L.edgePair(cues, 1, "start", 1700, 200),
  L.edgePair(cues, 2, "end", 5000, 200), L.edgePair(cues, 0, "start", 0, 200)];
// 여러 줄: a·b를 함께 → 앞 0까지(-1000) · 뒤는 c 시작 - 80 - b 끝(0)
// b·c → a 끝 + 80 - b 시작(-320)
out.many = [L.bodyMany(cues, [0, 1], -5000, 80, 9999), L.bodyMany(cues, [0, 1], 500, 80, 9999),
  L.bodyMany(cues, [1, 2], -900, 80, 9999), L.bodyMany(cues, [1, 2], 9000, 80, 5000),
  L.bodyMany(cues, [], 50, 80, 9999)];
const ids = ["a", "b", "c", "d"];
out.pick = [L.pick(ids, ["a"], "a", "c", "range"), L.pick(ids, ["a", "b"], "a", "d", "toggle"),
  L.pick(ids, ["a", "d"], "a", "a", "toggle"), L.pick(ids, ["b"], "b", "c", "one"),
  L.pick(ids, ["b"], null, "c", "range")];
console.log(JSON.stringify(out));
"""


@needs_node
def test_neighbor_edges_and_many_cues_move_inside_their_limits(tmp_path: Path) -> None:
    """W-87c(D152) — ⌥ 가장자리는 이웃과 간격째 함께 · 여러 줄은 고르지 않은 이웃 사이에서
    같은 틱만큼 · 목록 고르기(하나 · Mod 넣고 빼기 · ⇧ 범위)."""
    core = tmp_path / "lane.js"
    core.write_text(caption_lane_js.CAPTION_LANE_JS, encoding="utf-8")
    runner = tmp_path / "pairs.js"
    runner.write_text(_PAIRS, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    got = json.loads(done.stdout)
    assert got["pair"] == [
        {"value": 3500, "neighbor": 2, "neighborValue": 3580},
        {"value": 3720, "neighbor": 2, "neighborValue": 3800},
        {"value": 1600, "neighbor": 0, "neighborValue": 1200},
        {"value": 1700, "neighbor": 0, "neighborValue": 1300},
        None,
        None,
    ]
    assert got["many"] == [-1000, 0, -320, 1000, 0]
    assert got["pick"] == [
        {"picked": ["a", "b", "c"], "anchor": "a"},
        {"picked": ["a", "b", "d"], "anchor": "d"},
        {"picked": ["d"], "anchor": "a"},
        {"picked": ["c"], "anchor": "c"},
        {"picked": ["c"], "anchor": "c"},
    ]


_FIT = r"""
const L = require(process.argv[2]);
console.log(JSON.stringify([L.fit(30000, 300, 1, 1000), L.fit(30000, 0, 1, 1000),
  L.fit(10, 300, 1, 1000)]));
"""


@needs_node
def test_fit_shows_the_whole_timeline_inside_the_zoom_limits(tmp_path: Path) -> None:
    """⇧Z(Final Cut 「창에 맞추기」) — 타임라인 전체가 레인 폭에 든다.

    줌 한도 밖이면 한도에서 멈춘다."""
    core = tmp_path / "lane.js"
    core.write_text(caption_lane_js.CAPTION_LANE_JS, encoding="utf-8")
    runner = tmp_path / "fit.js"
    runner.write_text(_FIT, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(done.stdout) == [
        {"start": 0, "tpp": 100},
        {"start": 0, "tpp": 1000},
        {"start": 0, "tpp": 1},
    ]
