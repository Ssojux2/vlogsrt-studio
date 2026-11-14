"""최종 편집 브라우저 핵심(W-66, D117) — `CORE_JS`가 Python 모델과 정확히 같은 값을 내는가.

공용 사례(`final_edit_cases.json`)를 노드에서 돌려 Python 결과(사례의 `expect`)와 비교한다. 되돌리기
기록·키 조회(`KEYMAP` 전 행 · IME · 글 칸 · mac/Windows 수식키)·파형 축소도 여기서 본다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import final_edit_ui
from ui.final_edit_js import CORE_JS

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")
CASES = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic" / "final_edit_cases.json"
SETTINGS = {"minClipFrames": 1, "maxTextChars": 500, "minDurationMs": 850, "maxDurationMs": 7000,
            "minGapMs": 80, "maxCps": 12}  # fmt: skip

RUNNER = r"""
const FE = require(process.argv[2]);
const cases = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const settings = JSON.parse(process.argv[4]);
const SETTINGS_OPS = new Set(["set_clip_start", "set_clip_end", "cue_set_text"]);
const NAMES = {
  blade: "blade", ripple_delete: "rippleDelete", delete_clips: "deleteClips",
  set_clip_start: "setClipStart", set_clip_end: "setClipEnd", join_clips: "joinClips",
  cue_split: "cueSplit", cue_merge_next: "cueMergeNext", cue_insert: "cueInsert",
  cue_delete: "cueDelete", cue_set_times: "cueSetTimes", cue_shift: "cueShift",
  cue_set_text: "cueSetText", cues_delete_inside: "cuesDeleteInside",
  draft_ms_at: "draftMsAt", edited_ticks: "editedTicks",
};
function run(c) {
  const doc = { draft: c.draft, clips: c.doc.clips, cues: c.doc.cues, nextCue: c.doc.nextCue };
  try {
    if (c.op === "project") {
      const p = FE.project(doc);
      return { cues: p.cues.map((q) => [q[0], q[1], q[2], q[4]]), hidden: p.hidden,
        srt: FE.srtText(doc, p) };
    }
    if (c.op === "warnings") { return { warnings: FE.warnings(doc, settings) }; }
    const args = c.args.slice();
    if (SETTINGS_OPS.has(c.op)) { args.push(settings); }
    const out = FE[NAMES[c.op]](doc, ...args);
    if (typeof out === "number") { return { value: out }; }
    return { doc: { clips: out.clips, cues: out.cues, nextCue: out.nextCue } };
  } catch (e) {
    if (FE.isError(e)) { return { error: true }; }
    throw e;
  }
}
console.log(JSON.stringify(cases.map((c) => ({ name: c.name, got: run(c) }))));
"""


def _core(tmp_path: Path) -> Path:
    path = tmp_path / "core.js"
    path.write_text(CORE_JS, encoding="utf-8")
    return path


def _node(tmp_path: Path, script: str, *args: str) -> object:
    runner = tmp_path / "runner.js"
    runner.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(runner), *args], capture_output=True, text=True, check=True, timeout=60
    )
    return json.loads(completed.stdout)


@needs_node
def test_core_matches_python_on_every_shared_case(tmp_path: Path) -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    results = _node(tmp_path, RUNNER, str(_core(tmp_path)), str(CASES), json.dumps(SETTINGS))
    assert isinstance(results, list) and len(results) == len(cases)
    for case, result in zip(cases, results, strict=True):
        assert result["got"] == case["expect"], case["name"]


HISTORY = r"""
const FE = require(process.argv[2]);
const h = new FE.History(3, 500);
const out = [];
h.record("a", "d0", null, 0); h.record("b", "d1", null, 10);
h.record("nudge", "d2", "nudge:c1", 20); h.record("nudge", "d3", "nudge:c1", 400);
out.push(h.past.map((e) => e.doc));
h.record("nudge", "d4", "nudge:c1", 1000);  // 500ms를 넘으면 새 칸 — 상한 3이라 맨 앞이 빠진다
out.push(h.past.map((e) => e.doc));
const u = h.undo("d5"); out.push([u.doc, h.future.map((e) => e.doc)]);
const r = h.redo("d4"); out.push([r.doc, h.past.map((e) => e.doc)]);
h.undo("d5"); h.record("x", "d4", null, 2000); out.push(h.future.length);
console.log(JSON.stringify(out));
"""


@needs_node
def test_history_cap_coalesce_and_redo_clear(tmp_path: Path) -> None:
    result = _node(tmp_path, HISTORY, str(_core(tmp_path)))
    assert result == [
        ["d0", "d1", "d2"],
        ["d1", "d2", "d4"],
        ["d4", ["d5"]],
        ["d5", ["d1", "d2", "d4"]],
        0,
    ]


KEYS = r"""
const FE = require(process.argv[2]);
const keymap = JSON.parse(process.argv[3]);
const out = [];
for (const b of keymap) {
  for (const mac of [true, false]) {
    const ev = { code: b.code, shiftKey: !!b.shift, altKey: !!b.alt,
      metaKey: mac && !!b.mod, ctrlKey: !mac && !!b.mod, repeat: false };
    const hit = FE.keyAction(keymap, ev, { mac, inText: false });
    out.push([b.action, b.code, mac, hit ? hit.action : null]);
  }
}
const space = { code: "Space", shiftKey: false, altKey: false, metaKey: false, ctrlKey: false };
out.push(FE.keyAction(keymap, { ...space, isComposing: true }, { mac: true, inText: false }));
out.push(FE.keyAction(keymap, { ...space, keyCode: 229 }, { mac: true, inText: false }));
out.push(FE.keyAction(keymap, space, { mac: true, inText: true }));
out.push(FE.keyAction(keymap, { ...space, repeat: true }, { mac: true, inText: false }));
const save = { code: "KeyS", metaKey: true, shiftKey: false, altKey: false, ctrlKey: false };
out.push(FE.keyAction(keymap, save, { mac: true, inText: true }).action);
out.push(FE.keyAction(keymap, { ...save, ctrlKey: true }, { mac: true, inText: false }));
const right = { code: "ArrowRight", repeat: true };
out.push(FE.keyAction(keymap, right, { mac: true, inText: false }).action);
console.log(JSON.stringify(out));
"""


@needs_node
def test_every_binding_resolves_on_both_platforms_and_filters(tmp_path: Path) -> None:
    keymap = final_edit_ui.keymap_payload()
    result = _node(tmp_path, KEYS, str(_core(tmp_path)), json.dumps(keymap))
    assert isinstance(result, list)
    *rows, composing, ime, in_text, repeat, save, mac_ctrl, arrow = result
    for action, code, mac, hit in rows:
        assert hit == action, (code, mac)
    assert composing is None and ime is None and in_text is None and repeat is None
    assert save == "save" and mac_ctrl is None and arrow == "frame-forward"


WAVE = r"""
const FE = require(process.argv[2]);
const levels = FE.mipLevels([1, 9, 3, 4, 8, 2, 7]);
console.log(JSON.stringify([levels.map((l) => Array.from(l)), FE.columnPeak(levels, 0, 1),
  FE.columnPeak(levels, 2, 6), FE.columnPeak(levels, 0, 7), FE.columnPeak(levels, 5, 5)]));
"""


@needs_node
def test_waveform_mip_levels_and_column_peaks(tmp_path: Path) -> None:
    result = _node(tmp_path, WAVE, str(_core(tmp_path)))
    assert result == [
        [[1, 9, 3, 4, 8, 2, 7], [9, 4, 8, 7], [9, 8], [9]],
        1,
        8,
        9,
        0,
    ]


@needs_node
def test_core_parses_in_node(tmp_path: Path) -> None:
    subprocess.run(["node", "--check", str(_core(tmp_path))], check=True, timeout=60)


APP = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[2], "utf8");
const model = JSON.parse(process.argv[3]);
const saves = [];
let reply = { ok: true, revision: 1, summary: { hidden_by_cut: [] }, warnings: [] };
const listeners = {};
const stubEl = () => ({ addEventListener() {}, classList: { toggle() {}, add() {}, remove() {} } });
let holderText = JSON.stringify(model);
const holder = { get textContent() { return holderText; } };
const root = Object.assign(stubEl(), { querySelector: () => holder });
global.window = global;
global.navigator = { platform: "MacIntel" };
global.document = {
  getElementById: () => null, querySelector: () => null, activeElement: null,
  addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
};
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.addEventListener = () => {};
const element = { querySelector: () => root };
const server = {
  fe_save: (p) => { saves.push(p); return Promise.resolve(reply); },
  fe_waveform: () => Promise.resolve({ ok: false, error: "없음" }),
  fe_load: () => Promise.resolve({ ok: false }),
};
let watcher = null;
new Function("element", "props", "server", "watch", source)(element, {}, server,
  (name, fn) => { watcher = fn; });
const S = global.__vlFinalEdit;
const run = global.__vlFinalEditRun;
const out = {};
(async () => {
  S.playhead = 30; run("blade");
  out.afterBlade = [S.doc.clips, S.dirty, S.history.past.length];
  run("save");
  await new Promise((r) => setTimeout(r, 10));
  out.firstSave = [saves[0].force, saves[0].baseRevision, saves[0].doc.clips, S.dirty];
  // 저장된 깨끗한 문서의 재그리기(같은 리비전) — 문서와 되돌리기 기록을 지킨다.
  const saved = Object.assign({}, model.edit, { revision: 1 });
  holderText = JSON.stringify(Object.assign({}, model, { epoch: "e1b", edit: saved }));
  watcher();
  out.cleanRerender = [S.doc.clips, S.history.past.length];
  // 같은 초안의 새 모델(그라디오 재그리기) — 편집 중이면 로컬 문서를 지킨다.
  run("undo");
  holderText = JSON.stringify(Object.assign({}, model, { epoch: "e2" }));
  watcher();
  out.keptLocal = [S.doc.clips, S.dirty, S.history.past.length >= 0];
  reply = { ok: true, conflict: true, revision: 7 };
  run("save");
  await new Promise((r) => setTimeout(r, 10));
  out.conflict = [S.banner, saves[1].force];
  reply = { ok: true, revision: 8, summary: { hidden_by_cut: ["c1"] }, warnings: [] };
  run("force-save");
  await new Promise((r) => setTimeout(r, 10));
  out.forced = [saves[2].force, S.baseRevision, S.banner, S.dirty];
  reply = { ok: false, error: "수명 토큰", stale: true };
  S.playhead = 60; run("blade"); run("save");
  await new Promise((r) => setTimeout(r, 10));
  out.stale = [S.banner, S.statusError];
  out.keysBound = (listeners.keydown || []).length;
  console.log(JSON.stringify(out));
})();
"""


@needs_node
def test_app_boot_save_conflict_and_stale_states(tmp_path: Path) -> None:
    edit = {
        "exportId": "20261112T010203Z",
        "mediaFileId": "m1",
        "frameDuration": {"num": 1, "den": 30},
        "totalFrames": 300,
        "revision": 0,
        "seedSrt": "draft.srt",
        "doc": {"clips": [[0, 300]], "cues": [["c1", 1000, 2000, "안녕"]], "nextCue": 1},
        "exports": [{"id": "20261112T010203Z", "hasEdit": False, "mediaFileId": "m1"}],
        "olderEdits": [],
        "newerSeed": False,
        "settings": {
            **SETTINGS, "undoLimit": 10, "coalesceMs": 500, "autosaveMs": 60000,
            "newCueMs": 2000, "reverseTickMs": 100, "zoomMinPxPerS": 2, "zoomMaxPxPerS": 400,
            "zoomStepPercent": 125, "snapPx": 8, "bigStepFrames": 10, "shuttleRates": [1, 2],
        },
    }  # fmt: skip
    model = {"epoch": "e1", "signature": "sig", "token": "tok", "edit": edit,
             "keymap": final_edit_ui.keymap_payload(), "empty": None}  # fmt: skip
    script = tmp_path / "app.js"
    script.write_text(final_edit_ui.FINAL_EDIT_JS, encoding="utf-8")
    result = _node(tmp_path, APP, str(script), json.dumps(model))
    assert result == {
        "afterBlade": [[[0, 30], [30, 300]], True, 1],
        "firstSave": [False, 0, [[0, 30], [30, 300]], False],  # ⌘S는 덮어쓰지 않는다
        "cleanRerender": [[[0, 30], [30, 300]], 1],
        "keptLocal": [[[0, 300]], True, True],
        "conflict": ["conflict", False],
        "forced": [True, 8, None, False],
        "stale": ["stale", True],
        "keysBound": 1,
    }


WHEEL = """
const FE = require(process.argv[2]);
const s = { zoomStepPercent: 125, wheelZoomNotchPx: 100 };
const cases = [[-100, 0], [100, 0], [-4, 0], [3, 0], [-900, 0], [0, 0], [-3, 1], [2, 1]];
console.log(JSON.stringify(cases.map(([dy, mode]) => FE.wheelZoomFactor(dy, mode, s))));
"""


@needs_node
def test_wheel_zoom_follows_the_wheel_amount(tmp_path: Path) -> None:
    """W-75, D134 — 마우스 한 칸(100 px)이 한 걸음(125%)이고, 트랙패드 핀치의 작은 델타는
    그만큼 작게 — 한 사건은 한 걸음까지. 줄 단위(deltaMode 1)는 부호만 보고 한 걸음이다."""
    got = _node(tmp_path, WHEEL, str(_core(tmp_path)))
    assert isinstance(got, list)
    expected = [1.25, 0.8, 1.25**0.04, 1.25**-0.03, 1.25, 1.0, 1.25, 0.8]
    assert [round(value, 12) for value in got] == [round(value, 12) for value in expected]
