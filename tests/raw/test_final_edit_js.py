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


PLAY = r"""
const FE = require(process.argv[2]);
const draft = { num: 1, den: 30, totalFrames: 300 };
const cut = { draft, clips: [[0, 30], [30, 60], [90, 120], [150, 180]], cues: [], nextCue: 0 };
const whole = { draft, clips: [[0, 300]], cues: [], nextCue: 0 };
const none = { target: null, ready: false };
const at = (target, ready) => ({ target, ready });
const step = (doc, frame, standby) => {
  const s = FE.playStep(doc, frame, standby);
  return [s.action, s.edited, s.target, s.swap, s.prime];
};
console.log(JSON.stringify([
  step(cut, 10, none),
  step(cut, 29, at(90, true)),
  step(cut, 59, at(90, true)),
  step(cut, 59, at(90, false)),
  step(cut, 59, at(150, true)),
  step(cut, 59, null),
  step(cut, 70, at(90, true)),
  step(cut, 100, none),
  step(cut, 119, at(150, true)),
  step(cut, 179, at(150, true)),
  step(cut, 200, none),
  step(whole, 10, none),
  step(whole, 299, none),
]));
"""


@needs_node
def test_play_step_primes_the_standby_and_swaps_only_when_it_waits_at_the_target(
    tmp_path: Path,
) -> None:
    # 맞닿은 클립은 한 줄기로 이어 틀고, 컷 앞 마지막 프레임 · 지운 구간 안에서만 건넌다(W-71).
    result = _node(tmp_path, PLAY, str(_core(tmp_path)))
    assert result == [
        ["play", 10, None, False, 90],
        ["play", 29, None, False, 90],
        ["jump", 59, 90, True, None],
        ["jump", 59, 90, False, None],  # 대기 영상이 아직 시크 중 — 지금처럼 시크로 건넌다
        ["jump", 59, 90, False, None],  # 다른 자리에 맞춰 둠(편집·시크 뒤) — 바꾸지 않는다
        ["jump", 59, 90, False, None],
        ["jump", None, 90, True, None],  # 지운 구간이 이미 보였다 — 바로 건넌다
        ["play", 70, None, False, 150],
        ["jump", 89, 150, True, None],
        ["stop", 119, None, False, None],
        ["stop", None, None, False, None],
        ["play", 10, None, False, None],
        ["stop", 299, None, False, None],
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


DECKS = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[2], "utf8");
const model = JSON.parse(process.argv[3]);
const log = [];
function fakeVideo(id) {
  const el = {
    id, attrs: {}, listeners: {}, classes: new Set(), seeking: false, readyState: 4,
    paused: true, playbackRate: 1, muted: false, volume: 1, frameFn: null, sets: [], _t: 0,
    getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
    classList: null,
    get currentTime() { return this._t; },
    set currentTime(value) {
      this._t = value; this.seeking = true; this.sets.push(Math.round(value * 30 - 0.5));
    },
    play() { this.paused = false; log.push("play " + id); return Promise.resolve(); },
    pause() {
      this.paused = true; log.push("pause " + id);
      for (const fn of this.listeners.pause || []) { fn({ target: this }); }
    },
    requestVideoFrameCallback(fn) { this.frameFn = fn; },
    present(frame) {
      const fn = this.frameFn; this.frameFn = null; fn(0, { mediaTime: frame / 30 });
    },
  };
  el.classList = {
    toggle(name, on) { if (on) { el.classes.add(name); } else { el.classes.delete(name); } },
    add() {}, remove() {},
  };
  return el;
}
const a = fakeVideo("a");
const b = fakeVideo("b");
const stubEl = () => ({ addEventListener() {}, classList: { toggle() {}, add() {}, remove() {} } });
const holder = { get textContent() { return JSON.stringify(model); } };
const root = Object.assign(stubEl(), { querySelector: () => holder });
global.window = global;
global.navigator = { platform: "MacIntel" };
global.document = {
  getElementById: (id) => (id === "fe-video" ? a : id === "fe-video-b" ? b : null),
  querySelector: () => null, activeElement: null, addEventListener() {},
};
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.addEventListener = () => {};
const server = {
  fe_save: () => Promise.resolve({ ok: true }),
  fe_waveform: () => Promise.resolve({ ok: false, error: "없음" }),
  fe_load: () => Promise.resolve({ ok: false }),
};
const element = { querySelector: () => root };
new Function("element", "props", "server", "watch", source)(element, {}, server, () => {});
const S = global.__vlFinalEdit;
const run = global.__vlFinalEditRun;
const out = {};
(async () => {
  out.boot = [a.classes.has("fe-standby"), b.classes.has("fe-standby"), b.getAttribute("src")];
  run("play-toggle");
  a.seeking = false;
  const fromA = a.frameFn;
  a.present(10);                       // 컷 앞 — 대기 영상을 다음 클립 시작에 맞춘다
  out.primed = [b.sets.slice(), b.paused];
  b.seeking = false;
  a.present(29);                       // 클립 끝 — 대기 영상으로 바꿔 튼다
  await new Promise((r) => setTimeout(r, 0));
  out.swapped = [S.deck, a.classes.has("fe-standby"), b.classes.has("fe-standby"), S.playing,
    log.slice(), a.sets.length];
  fromA(0, { mediaTime: 31 / 30 });    // 옛 영상의 늦은 프레임 콜백은 무시된다
  b.present(61);
  out.afterSwap = [S.playhead, a.sets.slice(), a.paused];
  b.present(89);                       // 대기 영상(a)이 아직 시크 중 — 활성 영상을 시크한다
  b.present(89);                       // 같은 자리로 가는 시크가 도는 동안 다시 걸지 않는다
  out.fallback = [S.deck, b.sets.slice()];
  b.seeking = false;
  b.present(120);
  out.cutDone = [S.playhead];
  console.log(JSON.stringify(out));
})();
"""


@needs_node
def test_app_swaps_to_the_primed_standby_at_a_cut_and_seeks_only_when_it_is_not_ready(
    tmp_path: Path,
) -> None:
    edit = {
        "exportId": "20261113T000000Z",
        "mediaFileId": "m1",
        "frameDuration": {"num": 1, "den": 30},
        "totalFrames": 300,
        "revision": 1,
        "seedSrt": "draft.srt",
        "doc": {"clips": [[0, 30], [60, 90], [120, 150]], "cues": [], "nextCue": 0},
        "exports": [{"id": "20261113T000000Z", "hasEdit": True, "mediaFileId": "m1"}],
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
    result = _node(tmp_path, DECKS, str(script), json.dumps(model))
    assert result == {
        "boot": [False, True, "/media/m1"],
        "primed": [[60], True],
        "swapped": [1, True, False, True, ["play a", "play b", "pause a"], 0],
        "afterSwap": [31, [120], True],  # 멈춘 옛 영상이 다음 컷 뒤를 맞춘다
        "fallback": [1, [60, 120]],
        "cutDone": [60],
    }
