"""원본 범위 타임라인 브라우저 핵심(W-71a, D125) — `TIMELINE_CORE_JS`가 Python과 같은 값을 내는가.

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


# ── 범위 조정: 가장자리를 갈 수 있는 자리로(W-71d) — 브라우저만의 규칙 ─────────────────────

EDGE_RUNNER = r"""
const TL = require(process.argv[2]);
const doc = JSON.parse(process.argv[3]);
const asks = JSON.parse(process.argv[4]);
const settings = { minClipFrames: 3 };
console.log(JSON.stringify(asks.map((a) => TL.clampEdge(doc, a[0], a[1], a[2], settings))));
"""


@needs_node
def test_clamp_edge_keeps_an_edge_inside_the_source_room_and_the_minimum_length(
    tmp_path: Path,
) -> None:
    """같은 원본의 앞뒤 클립·원본 처음·끝·최소 길이 안으로 — 범위 밖을 끌어도 거기서 멈춘다."""
    core = tmp_path / "timeline_core.js"
    core.write_text(TIMELINE_CORE_JS, encoding="utf-8")
    clips = [["a", 30, 90], ["a", 150, 200], ["b", 10, 40]]
    doc = {"facts": PLAY_FACTS, "clips": clips, "cues": [], "nextCue": 1, "seeded": {}}
    asks = [
        [0, "start", -50],
        [0, "start", 60],
        [0, "start", 95],
        [0, "end", 120],
        [0, "end", 200],
        [1, "start", 80],
        [1, "end", 999],
        [2, "start", 0],
        [2, "end", 41],
        [2, "end", 11],
    ]
    got = _node(tmp_path, EDGE_RUNNER, str(core), json.dumps(doc), json.dumps(asks))
    assert got == [0, 60, 87, 120, 150, 90, 300, 0, 41, 13]


# ── 클립 편집: 블레이드·지우기·합치기 뒤의 선택과 플레이헤드(W-71f) — 브라우저만의 규칙 ─────────

CLIP_EDIT_RUNNER = r"""
const TL = require(process.argv[2]);
const doc = JSON.parse(process.argv[3]);
const asks = JSON.parse(process.argv[4]);
function one(a) {
  try {
    const out = a[0] === "view" ? TL.clampView(doc, a[1], a[2])
      : TL.clipEdit(doc, a[0], a[1], a[2]);
    return { clips: out.doc ? out.doc.clips : null, sel: out.sel, playhead: out.playhead };
  } catch (e) {
    if (TL.isError(e)) { return { error: true }; }
    throw e;
  }
}
console.log(JSON.stringify(asks.map(one)));
"""

# a 둘은 맞닿은 블레이드 조각 · b 30프레임 · c는 24fps 48프레임 = 시퀀스 60프레임. 출력 0–150.
EDIT_CLIPS = [["a", 0, 30], ["a", 30, 60], ["b", 10, 40], ["c", 24, 72]]


def _clip_edit(tmp_path: Path, clips: list[list[object]], asks: list[list[object]]) -> list[dict]:
    core = tmp_path / "timeline_core.js"
    core.write_text(TIMELINE_CORE_JS, encoding="utf-8")
    doc = {"facts": PLAY_FACTS, "clips": clips, "cues": [], "nextCue": 1, "seeded": {}}
    out = _node(tmp_path, CLIP_EDIT_RUNNER, str(core), json.dumps(doc), json.dumps(asks))
    assert isinstance(out, list)
    return out


@needs_node
def test_blade_at_the_playhead_selects_the_right_piece_and_keeps_the_playhead(
    tmp_path: Path,
) -> None:
    got = _clip_edit(tmp_path, EDIT_CLIPS, [["blade", 0, 70], ["blade", 2, 60], ["blade", 0, 0]])
    assert got[0] == {
        "clips": [["a", 0, 30], ["a", 30, 60], ["b", 10, 20], ["b", 20, 40], ["c", 24, 72]],
        "sel": 3,
        "playhead": 70,
    }
    assert got[1] == {"error": True}  # 편집 점 위
    assert got[2] == {"error": True}  # 타임라인 처음도 편집 점이다


@needs_node
def test_delete_ripples_and_moves_to_the_clip_that_took_its_place(tmp_path: Path) -> None:
    """지운 자리를 뒤가 당겨 채운다 — 선택은 그 자리 클립(마지막이면 앞 클립), 헤드는 그 시작."""
    got = _clip_edit(tmp_path, EDIT_CLIPS, [["delete", 2, 75], ["delete", 3, 140]])
    assert got[0] == {
        "clips": [["a", 0, 30], ["a", 30, 60], ["c", 24, 72]],
        "sel": 2,
        "playhead": 60,
    }
    assert got[1] == {
        "clips": [["a", 0, 30], ["a", 30, 60], ["b", 10, 40]],
        "sel": 2,
        "playhead": 60,
    }


@needs_node
def test_the_last_clip_is_not_deleted(tmp_path: Path) -> None:
    """빈 타임라인은 렌더가 거부하고 재생할 것이 없다 — 마지막 하나는 지우지 않는다."""
    assert _clip_edit(tmp_path, [["b", 10, 40]], [["delete", 0, 5]]) == [{"error": True}]


@needs_node
def test_join_merges_only_a_touching_piece_of_the_same_source(tmp_path: Path) -> None:
    got = _clip_edit(tmp_path, EDIT_CLIPS, [["join", 0, 45], ["join", 1, 45], ["join", 3, 100]])
    assert got[0] == {
        "clips": [["a", 0, 60], ["b", 10, 40], ["c", 24, 72]],
        "sel": 0,
        "playhead": 45,
    }
    assert got[1] == {"error": True}  # a와 b는 다른 원본
    assert got[2] == {"error": True}  # 마지막 클립은 합칠 다음이 없다


@needs_node
def test_an_unknown_edit_is_refused(tmp_path: Path) -> None:
    assert _clip_edit(tmp_path, EDIT_CLIPS, [["swap", 0, 0], ["delete", 9, 0]]) == [
        {"error": True},
        {"error": True},
    ]


@needs_node
def test_clamp_view_keeps_selection_and_playhead_inside_a_shorter_timeline(tmp_path: Path) -> None:
    """되돌리기·다시 하기로 클립이 줄면 선택과 플레이헤드가 문서 안으로 돌아온다."""
    got = _clip_edit(tmp_path, [["a", 0, 30], ["b", 10, 40]], [["view", 3, 140], ["view", 1, 12]])
    assert got == [
        {"clips": None, "sel": 1, "playhead": 59},
        {"clips": None, "sel": 1, "playhead": 12},
    ]


# ── 자막 편집: 플레이헤드에서 자막 고치기(W-71i) — 브라우저만의 규칙 ─────────────────────────

CUE_EDIT_RUNNER = r"""
const TL = require(process.argv[2]);
const doc = JSON.parse(process.argv[3]);
const asks = JSON.parse(process.argv[4]);
const settings = JSON.parse(process.argv[5]);
function one(a) {
  try {
    if (a[0] === "at") { return { cue: TL.cueAt(doc, a[1]) }; }
    if (a[0] === "split-text") { return { parts: TL.splitText(a[1]) }; }
    const out = TL.cueEdit(doc, a[0], a[1], a[2], settings, a[3]);
    return { cues: out.doc ? out.doc.cues : null, nextCue: out.doc ? out.doc.nextCue : null,
      cue: out.cue };
  } catch (e) {
    if (TL.isError(e)) { return { error: true }; }
    throw e;
  }
}
console.log(JSON.stringify(asks.map(one)));
"""

# a 0–30프레임(0–1000 ms) → 출력 0–30 · b 10–40프레임(333–1333 ms) → 출력 30–60.
# c4는 b의 클립 앞이라 컷에 가려진 자막이다.
CUE_CLIPS = [["a", 0, 30], ["b", 10, 40]]
CUES = [
    ["c1", "a", 100, 600, "하나 둘", None],
    ["c2", "a", 700, 900, "셋", None],
    ["c3", "b", 400, 1000, "넷 다섯", None],
    ["c4", "b", 0, 200, "숨음", None],
]


def _cue_edit(tmp_path: Path, asks: list[list[object]]) -> list[dict]:
    core = tmp_path / "timeline_core.js"
    core.write_text(TIMELINE_CORE_JS, encoding="utf-8")
    doc = {"facts": PLAY_FACTS, "clips": CUE_CLIPS, "cues": CUES, "nextCue": 5, "seeded": {}}
    settings = json.dumps(client_settings())
    out = _node(tmp_path, CUE_EDIT_RUNNER, str(core), json.dumps(doc), json.dumps(asks), settings)
    assert isinstance(out, list)
    return out


def _times(got: dict, cue_id: str) -> list[object]:
    return next(row[2:5] for row in got["cues"] if row[0] == cue_id)


@needs_node
def test_cue_edges_move_to_the_playhead_only_on_the_cues_own_source(tmp_path: Path) -> None:
    got = _cue_edit(
        tmp_path,
        [
            ["start", "c1", 6],
            ["start", "c1", 20],
            ["start", "c1", 40],
            ["end", "c3", 45],
            ["end", "c3", 31],
        ],
    )
    assert _times(got[0], "c1") == [200, 600, "하나 둘"] and got[0]["cue"] == "c1"
    assert got[1] == {"error": True}  # 667 ms — 끝보다 늦은 시작
    assert got[2] == {"error": True}  # 플레이헤드가 다른 원본(b)에 있다
    assert _times(got[3], "c3") == [400, 833, "넷 다섯"]
    assert got[4] == {"error": True}  # 367 ms — 시작보다 이른 끝


@needs_node
def test_split_divides_the_text_at_the_space_nearest_the_middle(tmp_path: Path) -> None:
    got = _cue_edit(tmp_path, [["split", "c1", 9], ["split", "c1", 2], ["split-text", "하나 둘"]])
    assert _times(got[0], "c1") == [100, 300, "하나"]
    assert _times(got[0], "n5") == [300, 600, "둘"]
    assert got[0]["nextCue"] == 6 and got[0]["cue"] == "c1"
    assert got[1] == {"error": True}  # 67 ms는 자막 앞이다
    assert got[2] == {"parts": ["하나", "둘"]}


@needs_node
def test_merge_takes_the_next_visible_cue_of_the_same_source(tmp_path: Path) -> None:
    got = _cue_edit(tmp_path, [["merge", "c1", 0], ["merge", "c2", 0]])
    assert _times(got[0], "c1") == [100, 900, "하나 둘 셋"]
    assert "c2" not in [row[0] for row in got[0]["cues"]]
    assert got[1] == {"error": True}  # 다음 보이는 자막 c3은 다른 원본


@needs_node
def test_delete_moves_the_selection_to_the_next_visible_cue_then_the_previous(
    tmp_path: Path,
) -> None:
    got = _cue_edit(tmp_path, [["delete", "c2", 0], ["delete", "c3", 0]])
    assert "c2" not in [row[0] for row in got[0]["cues"]] and got[0]["cue"] == "c3"
    assert "c3" not in [row[0] for row in got[1]["cues"]] and got[1]["cue"] == "c2"


@needs_node
def test_insert_puts_an_empty_cue_at_the_playhead_inside_its_clip(tmp_path: Path) -> None:
    got = _cue_edit(tmp_path, [["insert", None, 15], ["insert", None, 60]])
    new = next(row for row in got[0]["cues"] if row[0] == "n5")
    assert new == ["n5", "a", 500, 1000, "", None]  # 500 + 2000 ms를 클립 끝(1000 ms)에서 멈춘다
    assert got[0]["cue"] == "n5" and got[0]["nextCue"] == 6
    assert got[1] == {"error": True}  # 타임라인 끝에는 넣을 클립이 없다


@needs_node
def test_text_changes_only_when_it_differs_and_hidden_cues_are_not_edited(tmp_path: Path) -> None:
    got = _cue_edit(
        tmp_path,
        [
            ["text", "c1", 0, "새 글"],
            ["text", "c1", 0, "하나 둘"],
            ["start", "c4", 35],
            ["delete", "c4", 0],
        ],
    )
    assert _times(got[0], "c1") == [100, 600, "새 글"]
    assert got[1] == {"cues": None, "nextCue": None, "cue": "c1"}
    assert got[2] == {"error": True}  # 컷에 가려진 자막은 고치지 않는다 — 클립을 늘리면 돌아온다
    assert got[3] == {"error": True}


@needs_node
def test_cue_at_names_the_visible_cue_under_the_playhead(tmp_path: Path) -> None:
    got = _cue_edit(tmp_path, [["at", 6], ["at", 22], ["at", 31], ["at", 60]])
    assert got == [{"cue": "c1"}, {"cue": "c2"}, {"cue": None}, {"cue": None}]
