"""파형 레인 위 클립 띠(W-100, D169)의 순수 계산 — 노드에서 실제로 돈다."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import caption_lane_js

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

_CLIPS = r"""
const L = require(process.argv[2]);
const view = { start: 0, tpp: 10 };              // 1px = 10틱
const seq = 100;                                 // 시퀀스 프레임 하나 = 100틱
const spans = [
  { index: 0, outStart: 0, outFrames: 10 },      // x 0–100
  { index: 1, outStart: 10, outFrames: 5 },      // x 100–150
  { index: 2, outStart: 15, outFrames: 20 },     // x 150–350
];
const out = {};
out.hit = [
  L.clipHit(spans, view, 2, 4, seq),     // 첫 클립 앞
  L.clipHit(spans, view, 50, 4, seq),    // 첫 클립 몸통
  L.clipHit(spans, view, 99, 4, seq),    // 0의 끝과 1의 앞이 맞닿음 — 끝(늘리기)이 먼저
  L.clipHit(spans, view, 103, 4, seq),   // 1의 앞
  L.clipHit(spans, view, 349, 4, seq),   // 마지막 끝
  L.clipHit(spans, view, 400, 4, seq),   // 밖
];
// 끈 틱 → 원본 프레임: 원본 프레임 하나 = 120틱이면 +250틱은 +2프레임(반올림), -59틱은 0
out.trim = [L.trimFrame(30, 250, 120), L.trimFrame(30, -59, 120), L.trimFrame(30, -61, 120)];
console.log(JSON.stringify(out));
"""


@needs_node
def test_clip_band_hit_and_trim(tmp_path: Path) -> None:
    core = tmp_path / "lane.js"
    core.write_text(caption_lane_js.CAPTION_LANE_JS, encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_CLIPS, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    got = json.loads(done.stdout)
    assert got["hit"] == [
        {"index": 0, "part": "start"},
        {"index": 0, "part": "body"},
        {"index": 0, "part": "end"},
        {"index": 1, "part": "start"},
        {"index": 2, "part": "end"},
        None,
    ]
    assert got["trim"] == [32, 30, 29]


_FILM = r"""
const L = require(process.argv[2]);
const out = {};
// 클립 x 100–400, 칸 폭 64, 보이는 폭 300 — 클립 처음에 맞춰 놓고 보이는 칸만, 가운데는 클립 안.
out.tiles = L.filmTiles(100, 400, 64, 300);
// 클립이 왼쪽 밖에서 시작하면 클립 처음 기준 격자를 지킨다(스크롤해도 칸이 미끄러지지 않는다).
out.left = L.filmTiles(-150, 60, 64, 300);
out.none = [L.filmTiles(310, 400, 64, 300), L.filmTiles(10, 12, 0, 300)];
// 칸 하나가 덮는 초 → 장면 간격(가장 작은 걸음 이상, 넘으면 마지막 걸음).
const steps = [0.5, 1, 2, 4, 8];
out.bucket = [L.filmBucket(0.1, steps), L.filmBucket(1, steps), L.filmBucket(1.2, steps),
  L.filmBucket(30, steps)];
out.key = [L.filmKey("A", 3.74, 0.5), L.filmKey("A", 3.76, 0.5), L.filmKey("B", 9.9, 4)];
// 전체 보기 띠 — 레인 보기(start 300틱 · 1px = 10틱 · 폭 200px)가 덮는 자리, 총 6000틱을 600px에.
out.window = [L.navWindow({ start: 300, tpp: 10 }, 200, 6000, 600),
  L.navWindow({ start: -500, tpp: 10 }, 200, 6000, 600),
  L.navWindow({ start: 0, tpp: 100 }, 200, 6000, 600)];
out.center = L.centerOn({ start: 0, tpp: 10 }, 3000, 200);
console.log(JSON.stringify(out));
"""


@needs_node
def test_storyline_film_tiles_and_navigator(tmp_path: Path) -> None:
    core = tmp_path / "lane.js"
    core.write_text(caption_lane_js.CAPTION_LANE_JS, encoding="utf-8")
    runner = tmp_path / "film.js"
    runner.write_text(_FILM, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    got = json.loads(done.stdout)
    assert got["tiles"] == [
        {"x": 100, "mid": 132},
        {"x": 164, "mid": 196},
        {"x": 228, "mid": 260},
        {"x": 292, "mid": 324},
    ]
    assert got["left"] == [{"x": -22, "mid": 10}, {"x": 42, "mid": 51}]
    assert got["none"] == [[], []]
    assert got["bucket"] == [0.5, 1, 2, 8]
    assert got["key"] == ["A:3.5", "A:4", "B:8"]
    assert got["window"] == [[30, 230], [0, 150], [0, 600]]
    assert got["center"] == {"start": 2000, "tpp": 10}


_STYLE = r"""
const L = require(process.argv[2]);
const frame = { w: 400, h: 800 };          // 세로 화면 틀(px) — 짧은 변이 너비다(D171)
const box = { w: 200, h: 60 };             // 자막 덩어리
const at = { x: 200, y: 600 };             // 덩어리 가운데
const free = { lock: false, snap: false, snapPx: 8 };
const out = {};
out.move = L.styleMove(at, 30, -20, box, frame, free);
// ⇧ — 큰 쪽 축 하나만(좌우 또는 상하)
out.lockX = L.styleMove(at, 30, -20, box, frame, { lock: true, snap: false, snapPx: 8 });
out.lockY = L.styleMove(at, 5, -90, box, frame, { lock: true, snap: false, snapPx: 8 });
// 가운데 선 스냅 — 세로선(가로 가운데) · 가로선(세로 가운데), 8px 안이면 붙고 안내선을 켠다
out.snap = L.styleMove(at, 6, -196, box, frame, { lock: false, snap: true, snapPx: 8 });
out.far = L.styleMove(at, 20, 0, box, frame, { lock: false, snap: true, snapPx: 8 });
// 틀 밖으로 나가지 않는다 — 가운데는 [반폭, 틀 − 반폭] 안, 틀보다 크면 틀 가운데
out.edge = L.styleMove(at, -500, 900, box, frame, free);
out.huge = L.styleMove(at, 10, 10, { w: 500, h: 60 }, frame, free);
// 모서리 끌기 — 붙잡은 점(맞은편 모서리 또는 가운데)에서 잰 거리의 비로 글자 크기
out.scale = [
  L.styleScale(54, { x: 100, y: 570 }, { x: 300, y: 630 }, { x: 400, y: 660 }, [20, 160]),
  L.styleScale(54, { x: 200, y: 600 }, { x: 300, y: 630 }, { x: 250, y: 615 }, [20, 160]),
  L.styleScale(54, { x: 0, y: 0 }, { x: 10, y: 0 }, { x: 1000, y: 0 }, [20, 160]),
  L.styleScale(54, { x: 0, y: 0 }, { x: 0, y: 0 }, { x: 5, y: 5 }, [20, 160]),
];
out.anchor = L.styleAnchor({ x: 100, y: 570 }, { x: 1, y: 1 }, { w: 240, h: 72 });
out.point = [L.stylePoint({ x: 200, y: 600 }, frame, 1000),
  L.stylePoint({ x: -3, y: 801 }, frame, 1000)];
out.clamp = [L.clampInt(4.6, [0, 16]), L.clampInt(-2, [0, 16]), L.clampInt(99, [0, 16])];
console.log(JSON.stringify(out));
"""


@needs_node
def test_on_viewer_caption_move_scale_and_snap(tmp_path: Path) -> None:
    """W-107(D181) — 화면 자막 끌기: ⇧는 한 축, 가운데 선 스냅, 틀 안,
    모서리 끌기는 붙잡은 점에서."""
    core = tmp_path / "lane.js"
    core.write_text(caption_lane_js.CAPTION_LANE_JS, encoding="utf-8")
    runner = tmp_path / "style.js"
    runner.write_text(_STYLE, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    got = json.loads(done.stdout)
    off = {"v": False, "h": False}
    assert got["move"] == {"x": 230, "y": 580, "guides": off}
    assert got["lockX"] == {"x": 230, "y": 600, "guides": off}
    assert got["lockY"] == {"x": 200, "y": 510, "guides": off}
    assert got["snap"] == {"x": 200, "y": 400, "guides": {"v": True, "h": True}}
    assert got["far"] == {"x": 220, "y": 600, "guides": off}
    assert got["edge"] == {"x": 100, "y": 770, "guides": off}
    assert got["huge"]["x"] == 200
    assert got["scale"] == [81, 27, 160, 54]
    assert got["anchor"] == {"x": 220, "y": 606}
    assert got["point"] == [
        {"x_permille": 500, "y_permille": 750},
        {"x_permille": 0, "y_permille": 1000},
    ]
    assert got["clamp"] == [5, 0, 16]
