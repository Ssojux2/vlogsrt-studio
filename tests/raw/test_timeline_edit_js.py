"""원본 범위 타임라인 브라우저 핵심(W-71a, D123) — `TIMELINE_CORE_JS`가 Python과 같은 값을 내는가.

공용 사례(`timeline_edit_cases.json`)를 노드에서 돌려 Python 결과(사례의 `expect`)와 비교한다. 같은
파일을 `test_timeline_edit.test_shared_cases_match_python`이 Python으로 돌린다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui.timeline_edit_js import TIMELINE_CORE_JS
from vlogsrt.project.raw.final_edit_config import client_settings

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"
CASES = FIXTURES / "timeline_edit_cases.json"

RUNNER = r"""
const TL = require(process.argv[2]);
const cases = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const settings = JSON.parse(process.argv[4]);
const SETTINGS_OPS = new Set(["set_clip_start", "set_clip_end", "cue_set_text"]);
const NAMES = {
  blade: "blade", ripple_delete: "rippleDelete", delete_clips: "deleteClips",
  set_clip_start: "setClipStart", set_clip_end: "setClipEnd", join_clips: "joinClips",
  cue_set_times: "cueSetTimes", cue_shift: "cueShift", cue_set_text: "cueSetText",
  cue_delete: "cueDelete", cues_delete_inside: "cuesDeleteInside", cue_split: "cueSplit",
  cue_merge_next: "cueMergeNext", cue_insert: "cueInsert",
  source_point: "sourcePoint", source_ms_at: "sourceMsAt", clip_bounds: "clipBounds",
  edited_ticks: "editedTicks",
};
function payload(doc) {
  return { clips: doc.clips, cues: doc.cues, nextCue: doc.nextCue, seeded: doc.seeded };
}
function value(doc, c) {
  if (c.op === "layout") {
    return TL.layout(doc).map((s) => [s.sourceId, s.outStart, s.outFrames]);
  }
  if (c.op === "project") {
    const p = TL.project(doc);
    return { cues: p.cues.map((q) => [q[0], q[2], q[3], q[5]]),
      hidden: p.hidden.map((q) => q[0]), total: p.total };
  }
  if (c.op === "warnings") { return TL.warnings(doc, settings); }
  if (c.op === "removed_ranges") { return TL.removedRanges(doc); }
  const args = c.args.slice();
  if (SETTINGS_OPS.has(c.op)) { args.push(settings); }
  const out = TL[NAMES[c.op]](doc, ...args);
  if (Array.isArray(out) || typeof out === "number") { return out; }
  return payload(out);
}
function run(c) {
  const doc = Object.assign({ facts: c.facts }, c.doc);
  try {
    TL.validate(doc, settings);
    return { value: value(doc, c) };
  } catch (e) {
    if (TL.isError(e)) { return { error: true }; }
    throw e;
  }
}
console.log(JSON.stringify(cases.map((c) => ({ name: c.name, got: run(c) }))));
"""


def _node(tmp_path: Path, script: str, *args: str) -> object:
    runner = tmp_path / "runner.js"
    runner.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(runner), *args], capture_output=True, text=True, check=True, timeout=60
    )
    return json.loads(completed.stdout)


@needs_node
def test_core_matches_python_on_every_shared_case(tmp_path: Path) -> None:
    core = tmp_path / "timeline_core.js"
    core.write_text(TIMELINE_CORE_JS, encoding="utf-8")
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    results = _node(tmp_path, RUNNER, str(core), str(CASES), json.dumps(client_settings()))
    assert isinstance(results, list) and len(results) == len(cases)
    for case, result in zip(cases, results, strict=True):
        assert result["got"] == case["expect"], case["name"]


@needs_node
def test_core_parses_in_node(tmp_path: Path) -> None:
    core = tmp_path / "timeline_core.js"
    core.write_text(TIMELINE_CORE_JS, encoding="utf-8")
    subprocess.run(["node", "--check", str(core)], check=True, timeout=60)


# ── 재생: 컷 건너기(W-71c) — 브라우저만의 규칙이라 Python 짝이 없다 ──────────────────────

PLAY_RUNNER = r"""
const TL = require(process.argv[2]);
const doc = JSON.parse(process.argv[3]);
const steps = JSON.parse(process.argv[4]);
console.log(JSON.stringify(steps.map((s) => TL.playStep(doc, s[0], s[1], s[2]))));
"""

PLAY_FACTS = {
    "sequence": {"num": 1, "den": 30},
    "sources": [
        {"id": "a", "num": 1, "den": 30, "frames": 300},
        {"id": "b", "num": 1, "den": 30, "frames": 300},
        {"id": "c", "num": 1, "den": 24, "frames": 240},
    ],
}


def _play(tmp_path: Path, clips: list[list[object]], steps: list[list[object]]) -> list[dict]:
    core = tmp_path / "timeline_core.js"
    core.write_text(TIMELINE_CORE_JS, encoding="utf-8")
    doc = {"facts": PLAY_FACTS, "clips": clips, "cues": [], "nextCue": 1, "seeded": {}}
    out = _node(tmp_path, PLAY_RUNNER, str(core), json.dumps(doc), json.dumps(steps))
    assert isinstance(out, list)
    return out


def _target(span: int, source: str, frame: int) -> dict[str, object]:
    return {"span": span, "sourceId": source, "frame": frame}


@needs_node
def test_play_step_plays_inside_a_clip_and_primes_the_next_cut(tmp_path: Path) -> None:
    clips = [["a", 30, 90], ["b", 0, 60]]
    got = _play(tmp_path, clips, [[0, 40, None]])
    assert got == [
        {
            "action": "play",
            "span": 0,
            "edited": 10,
            "target": None,
            "swap": False,
            "prime": _target(1, "b", 0),
        }
    ]


@needs_node
def test_play_step_jumps_on_the_last_frame_and_swaps_only_a_ready_matching_standby(
    tmp_path: Path,
) -> None:
    clips = [["a", 30, 90], ["b", 120, 180]]
    ready = {"sourceId": "b", "frame": 120, "ready": True}
    wrong = {"sourceId": "b", "frame": 121, "ready": True}
    loading = {"sourceId": "b", "frame": 120, "ready": False}
    got = _play(tmp_path, clips, [[0, 89, ready], [0, 89, wrong], [0, 95, loading]])
    jump = _target(1, "b", 120)
    assert [(g["action"], g["swap"], g["target"]) for g in got] == [
        ("jump", True, jump),
        ("jump", False, jump),
        ("jump", False, jump),
    ]
    assert [g["edited"] for g in got] == [59, 59, 59]


@needs_node
def test_play_step_runs_through_a_blade_without_a_cut(tmp_path: Path) -> None:
    """같은 원본에서 끝 = 다음 시작이면 끊지 않는다 — 다음 클립으로 넘어가 그대로 재생한다."""
    clips = [["a", 0, 60], ["a", 60, 120], ["b", 0, 30]]
    got = _play(tmp_path, clips, [[0, 59, None], [0, 61, None], [1, 119, None]])
    assert [(g["action"], g["span"], g["edited"]) for g in got] == [
        ("play", 0, 59),
        ("play", 1, 61),
        ("jump", 1, 119),
    ]
    assert got[0]["prime"] == _target(2, "b", 0)


@needs_node
def test_play_step_stops_after_the_last_clip(tmp_path: Path) -> None:
    got = _play(tmp_path, [["a", 0, 30]], [[0, 29, None], [0, 40, None]])
    assert [(g["action"], g["edited"], g["target"]) for g in got] == [
        ("stop", 29, None),
        ("stop", 29, None),
    ]


@needs_node
def test_play_step_holds_a_frame_before_the_clip_at_its_first_edited_frame(
    tmp_path: Path,
) -> None:
    """시크가 클립 앞 프레임에 떨어져도 뺀 구간을 지나간 것으로 보지 않는다 — 클립 첫 프레임이다."""
    got = _play(tmp_path, [["a", 30, 90]], [[0, 28, None]])
    assert (got[0]["action"], got[0]["edited"]) == ("play", 0)


@needs_node
def test_play_step_maps_a_mixed_rate_clip_onto_sequence_frames(tmp_path: Path) -> None:
    """24fps 원본 48프레임(2초)은 30fps 시퀀스 60프레임 — 원본 프레임 k는 ⌊k·30/24⌋."""
    clips = [["a", 0, 30], ["c", 24, 72]]
    got = _play(tmp_path, clips, [[1, 24, None], [1, 25, None], [1, 47, None], [1, 71, None]])
    assert [g["edited"] for g in got] == [30, 31, 58, 88]
    assert got[-1]["action"] == "stop"
