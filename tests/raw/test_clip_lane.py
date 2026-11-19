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
