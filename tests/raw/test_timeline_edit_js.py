"""원본 범위 타임라인 브라우저 핵심(W-71a, D122) — `TIMELINE_CORE_JS`가 Python과 같은 값을 내는가.

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
