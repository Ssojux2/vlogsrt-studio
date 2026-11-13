#!/usr/bin/env python3
"""원본 범위 타임라인 공용 사례를 다시 쓴다(W-71a, D123) — 기대값은 Python 모델이 채운다.

    python scripts/make_timeline_edit_cases.py

`tests/fixtures/synthetic/timeline_edit_cases.json`을 덮는다. 노드 시험
(`test_timeline_edit_js`)이 같은 파일로 브라우저 핵심(`TIMELINE_CORE_JS`)을 돌려 Python과 같은
값인지 본다. 모델을 고치면 이것을 다시 돌리고, 바뀐 기대값을 손 계산으로 확인한 뒤 커밋한다
(기대값을 그냥 받아들이지 않는다).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "raw"))

from test_timeline_edit import run_case  # noqa: E402

from vlogsrt import log  # noqa: E402

MIXED = {
    "sequence": {"num": 1, "den": 30},
    "sources": [
        {"id": "a", "num": 1, "den": 30, "frames": 300},
        {"id": "b", "num": 1, "den": 24, "frames": 240},
        {"id": "c", "num": 1001, "den": 30000, "frames": 300},
    ],
}
CUES = [
    ["c1", "a", 500, 1500, "걸침", "u1"],
    ["c2", "a", 4500, 5000, "가려짐", "u2"],
    ["c3", "b", 1500, 2500, "둘", "u1"],
    ["c4", "c", 2000, 2000, "점", "u1"],
    ["c5", "c", 9800, 10010, "끝 넘침", "u2"],
]
MIX = {
    "clips": [["a", 30, 120], ["b", 24, 120], ["c", 0, 300]],
    "cues": CUES,
    "nextCue": 1,
    "seeded": {"a": "ra", "b": "rb", "c": "rc"},
}
TWO = {
    "clips": [["a", 0, 60], ["a", 90, 150], ["b", 0, 240]],
    "cues": CUES,
    "nextCue": 4,
    "seeded": {"a": "ra"},
}
T = 30_000_000
MIXED2997 = {"sequence": {"num": 1001, "den": 30000}, "sources": MIXED["sources"]}
SEQ2997 = {
    "clips": [["b", 24, 120], ["c", 10, 269]],
    "cues": [["c1", "b", 4500, 5500, "끝 걸침", "u1"]],
    "nextCue": 1,
    "seeded": {},
}
TINY_FACTS = {
    "sequence": {"num": 1, "den": 30},
    "sources": [{"id": "b", "num": 1, "den": 24, "frames": 3}],
}
TINY = {"clips": [["b", 0, 3]], "cues": [], "nextCue": 1, "seeded": {}}
TINY2 = {"clips": [["b", 0, 2], ["b", 2, 3]], "cues": [], "nextCue": 1, "seeded": {}}
GAP2997 = {
    "clips": [["b", 24, 72], ["b", 96, 120]],
    "cues": [["c1", "b", 2500, 3500, "틈 끝", "u1"]],
    "nextCue": 1,
    "seeded": {},
}


def case(name, op, args=(), doc=MIX, facts=MIXED):
    return {"name": name, "facts": facts, "doc": doc, "op": op, "args": list(args)}


cases = [
    case("layout mixed rates", "layout"),
    case("layout two same-source clips", "layout", doc=TWO),
    case("project mixed", "project"),
    case("project two clips", "project", doc=TWO),
    case("warnings mixed", "warnings"),
    case("removed ranges", "removed_ranges", doc=TWO),
    case("blade same rate", "blade", [30]),
    case("blade 24fps clip", "blade", [150]),
    case("blade 29.97 clip", "blade", [400]),
    case("blade on edit point", "blade", [90]),
    case("ripple inside a", "ripple_delete", [30, 60]),
    case("ripple inside b", "ripple_delete", [150, 180]),
    case("ripple across b into c", "ripple_delete", [60, 240]),
    case("ripple past timeline end refused", "ripple_delete", [0, 600]),
    case("delete clips", "delete_clips", [[1]]),
    case("delete clips bad index", "delete_clips", [[7]]),
    case("extend start to source start", "set_clip_start", [0, 0]),
    case("extend end to source end", "set_clip_end", [1, 240]),
    case("end past source refused", "set_clip_end", [0, 301]),
    case("end into next same-source refused", "set_clip_end", [0, 91], doc=TWO),
    case("end touching then bounds", "clip_bounds", [1], doc=TWO),
    case("join refuses gap", "join_clips", [0], doc=TWO),
    case("join different sources refused", "join_clips", [1], doc=TWO),
    case("source point 24fps", "source_point", [150]),
    case("source point 29.97 near end", "source_point", [509]),
    case("edited ticks inside", "edited_ticks", ["b", 1500]),
    case("edited ticks before clip", "edited_ticks", ["a", 500]),
    case("edited ticks after clip", "edited_ticks", ["a", 9000]),
    case("edited ticks past c output end", "edited_ticks", ["c", 10010]),
    case("ms at cut start side", "source_ms_at", [3 * T, "start"]),
    case("ms at cut end side", "source_ms_at", [3 * T, "end"]),
    case("ms inside b", "source_ms_at", [7 * T // 2, "start"]),
    case("ms past c source end", "source_ms_at", [17 * T, "end"]),
    case("cue set times", "cue_set_times", ["c3", 1000, 2000]),
    case("cue set times past source", "cue_set_times", ["c3", 1000, 10001]),
    case("cue shift clamps at source end", "cue_shift", ["c3", 100000]),
    case("cue shift clamps at zero", "cue_shift", ["c1", -100000]),
    case("cue set text", "cue_set_text", ["c3", "둘\n줄"]),
    case("cue set text blank line refused", "cue_set_text", ["c3", "둘\n\n줄"]),
    case("cue delete", "cue_delete", ["c2"]),
    case("cues delete inside timeline", "cues_delete_inside", [3 * T, 5 * T]),
    case("cues delete inside none", "cues_delete_inside", [2 * T, 3 * T]),
    case("cue split", "cue_split", ["c3", 2000, "둘", "셋"]),
    case("cue split outside refused", "cue_split", ["c3", 2500, "둘", ""]),
    case(
        "cue merge same source",
        "cue_merge_next",
        ["c1"],
        doc=dict(MIX, clips=[["a", 0, 300], ["b", 0, 240]]),
    ),
    case(
        "cue merge other source refused",
        "cue_merge_next",
        ["c2"],
        doc=dict(MIX, clips=[["a", 0, 300], ["b", 0, 240]]),
    ),
    case("cue insert", "cue_insert", ["b", 3000, 3500, "새"]),
    case("cue insert past source refused", "cue_insert", ["a", 9000, 10001, "밖"]),
    case(
        "bad doc split source refused",
        "layout",
        doc=dict(MIX, clips=[["a", 0, 60], ["b", 0, 24], ["a", 90, 120]]),
    ),
    case("bad doc reversed refused", "layout", doc=dict(MIX, clips=[["a", 90, 120], ["a", 0, 60]])),
    case(
        "edited ticks start side in a 24fps gap",
        "edited_ticks",
        ["b", 3500, "start"],
        doc=GAP2997,
        facts=MIXED2997,
    ),
    case(
        "edited ticks end side in a 24fps gap",
        "edited_ticks",
        ["b", 3500, "end"],
        doc=GAP2997,
        facts=MIXED2997,
    ),
    case("project cue ending in a 24fps gap", "project", doc=GAP2997, facts=MIXED2997),
    case(
        "edited ticks end past a longer output",
        "edited_ticks",
        ["b", 5500, "end"],
        doc=SEQ2997,
        facts=MIXED2997,
    ),
    case("project end past a longer output", "project", doc=SEQ2997, facts=MIXED2997),
    case("layout tiny 24fps clip at source end", "layout", doc=TINY, facts=TINY_FACTS),
    case("layout trimmed pair at source end gains a frame", "layout", doc=TINY2, facts=TINY_FACTS),
]
for item in cases:
    item["expect"] = run_case(item)
out = ROOT / "tests" / "fixtures" / "synthetic" / "timeline_edit_cases.json"
out.write_text(json.dumps(cases, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
log.info(f"공용 사례 {len(cases)}개를 썼다: {out}")
