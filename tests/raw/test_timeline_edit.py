"""원본 범위 타임라인 편집(W-71a, D125) — 렌더 없이 원본 구간을 잇고 앞뒤로 늘이는 순수 모델.

클립은 원본 프레임 구간이고, 출력 위치는 렌더 계획과 **같은 함수**(`render_plan.sequence_edges`)로
놓는다 — 손대지 않은 타임라인의 배치가 `build_plan`의 조각과 같아야 화면이 곧 출력이다.
자막은 원본 시계(밀리초)에 살아 클립을 늘리면 그 자리의 원본 자막이 돌아온다. 원본마다 남긴 클립과
뺀 구간이 `[0, 프레임 수)`를 빈틈없이 덮는다(규칙 9·D35).
"""

from __future__ import annotations

import json
from fractions import Fraction as F
from pathlib import Path

import pytest

from vlogsrt.project.raw import render_plan as rp
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw import timeline_edit as te
from vlogsrt.project.raw.config import ORIENTATION_LANDSCAPE, RenderSettings
from vlogsrt.project.raw.timeline_edit import TimelineEditError

CASES = (
    Path(__file__).resolve().parent.parent / "fixtures" / "synthetic" / "timeline_edit_cases.json"
)
FD30, FD24, FD2997 = F(1, 30), F(1, 24), F(1001, 30000)
SEQUENCE = rp.SequenceFormat(ORIENTATION_LANDSCAPE, 1920, 1080, FD30)
FACTS = te.TimelineFacts(
    sequence=SEQUENCE,
    sources=(
        te.TimelineSource("a", FD30, 300),
        te.TimelineSource("b", FD24, 240),
        te.TimelineSource("c", FD2997, 300),
    ),
)


def _doc(
    clips: list[tuple[str, int, int]], cues: list[tuple[object, ...]] | None = None
) -> te.TimelineDocument:
    return te.TimelineDocument(
        facts=FACTS,
        clips=tuple(te.Clip(*clip) for clip in clips),
        cues=tuple(te.EditCue(*cue) for cue in (cues or [])),
        next_cue=1,
        seeded=(),
    )


def _clips(document: te.TimelineDocument) -> list[tuple[str, int, int]]:
    return [(c.source_id, c.start_frame, c.end_frame) for c in document.clips]


def _row(cue: te.EditCue) -> tuple[object, ...]:
    return (cue.cue_id, cue.source_id, cue.start_ms, cue.end_ms, cue.text, cue.origin)


MIXED = [("a", 30, 120), ("b", 24, 120), ("c", 0, 300)]


# ─── 배치 — 렌더 계획과 같은 함수 ───────────────────────────────────────


def test_ticks_cover_every_grid_and_millisecond() -> None:
    assert te.tick_rate(FACTS) == 30_000_000  # 1000 × lcm(30, 24, 30000)
    assert te.sequence_frame_ticks(FACTS) == 1_000_000
    assert te.source_frame_ticks(FACTS, "b") == 1_250_000
    assert te.source_frame_ticks(FACTS, "c") == 1_001_000
    assert te.ms_ticks(FACTS) == 30_000


def test_layout_rounds_cumulative_edges_and_floors_at_source_end() -> None:
    spans = te.layout(_doc(MIXED))
    # 3초 → 90 · 7초 → 210 · c는 원본 끝에 닿아 210 + ⌊10.01초 × 30⌋ = 510(반올림 510.8과 같다)
    assert [(s.out_start, s.out_frames) for s in spans] == [(0, 90), (90, 120), (210, 300)]
    assert te.edited_frames(_doc(MIXED)) == 510


def test_sequence_edges_pads_a_clip_that_rounds_to_nothing() -> None:
    edges, padded = rp.sequence_edges([(F(1, 60), False), (F(1, 60), False)], FD30)
    assert (edges, padded) == ([0, 1, 2], 1)


def _source(
    source_id: str,
    fd: F,
    seconds: int,
    green: list[tuple[F | int, F | int]],
    cues: tuple[rp.SourceCue, ...] = (),
) -> rp.RenderSource:
    tiers = []
    cursor = F(0)
    for start, end in green:
        if start > cursor:
            tiers.append(rp.TierPiece(cursor, F(start), tiering.TIER_YELLOW))
        tiers.append(rp.TierPiece(F(start), F(end), tiering.TIER_GREEN))
        cursor = F(end)
    if cursor < seconds:
        tiers.append(rp.TierPiece(cursor, F(seconds), tiering.TIER_YELLOW))
    return rp.RenderSource(
        source_id=source_id,
        name=f"{source_id}.mov",
        clock=rp.FrameClock(fd, int(seconds / fd)),
        width=1920,
        height=1080,
        grid=rp.GRID_UNIFORM,
        tiers=tuple(tiers),
        cues=cues,
    )


def test_untouched_seed_lays_out_exactly_like_build_plan() -> None:
    # 섞인 레이트(30·24·29.97) · 원본 끝에 닿는 조각 · 한 원본의 두 조각
    sources = [
        _source("a", FD30, 10, [(1, 4), (6, 10)]),
        _source("b", FD24, 10, [(1, 5)]),
        _source("c", FD2997, 10, [(F(1, 3), 9)]),
    ]
    plan = rp.build_plan(sources, RenderSettings(min_part_seconds=F(1), merge_gap_seconds=F(0)))
    facts = te.facts_from_plan(plan, sources)
    clips = [te.Clip(p.source_id, p.start_frame, p.end_frame) for p in plan.parts]
    seeded = te.seed(facts, clips, {s.source_id: ("r0", ()) for s in sources})
    spans = te.layout(seeded.document)
    assert [(s.out_start, s.out_frames) for s in spans] == [
        (p.output_start_frame, p.output_frames) for p in plan.parts
    ]
    assert te.edited_frames(seeded.document) == plan.total_frames
    assert facts.sequence == plan.sequence


def _cue(utterance: str, start: F | int, end: F | int, text: str) -> rp.SourceCue:
    return rp.SourceCue(utterance, F(start), F(end), text)


def test_untouched_seed_writes_the_same_srt_bytes_as_the_draft() -> None:
    # 클립 안 · 클립 앞 가장자리에서 잘림 · 끝 가장자리에서 잘림 · 길이 0 · 뺀 구간 안(가려짐)
    sources = [
        _source(
            "a", FD30, 10, [(1, 4), (6, 10)],
            (_cue("u1", F(1, 2), F(3, 2), "앞 걸침."), _cue("u2", 2, 3, "안"),
             _cue("u3", F(9, 2), 5, "가려짐"), _cue("u4", 7, 7, "점")),
        ),
        _source("b", FD24, 10, [(1, 5)], (_cue("u1", F(9, 2), F(11, 2), "끝 걸침"),)),
        _source("c", FD2997, 10, [(F(1, 3), 9)], (_cue("u1", 1, 2, "셋"),)),
    ]  # fmt: skip
    plan = rp.build_plan(sources, RenderSettings(min_part_seconds=F(1), merge_gap_seconds=F(0)))
    facts = te.facts_from_plan(plan, sources)
    clips = [te.Clip(p.source_id, p.start_frame, p.end_frame) for p in plan.parts]
    transcripts = {s.source_id: ("r0", s.cues) for s in sources}
    seeded = te.seed(facts, clips, transcripts)
    assert te.srt_text(seeded.document) == rp.srt_text(plan)


def test_a_cue_over_a_removed_gap_stays_one_cue_unlike_the_draft() -> None:
    # 초안 SRT는 조각마다 한 장(둘)이지만, 타임라인은 D117처럼 한 장으로 줄어든다
    # (블레이드가 SRT를 바꾸지 않게)
    sources = [_source("a", FD30, 10, [(1, 4), (6, 10)], (_cue("u1", F(7, 2), F(13, 2), "넘어"),))]
    plan = rp.build_plan(sources, RenderSettings(min_part_seconds=F(1), merge_gap_seconds=F(0)))
    assert [(c.start, c.end) for c in plan.cues] == [(F(5, 2), F(3)), (F(3), F(7, 2))]
    facts = te.facts_from_plan(plan, sources)
    clips = [te.Clip(p.source_id, p.start_frame, p.end_frame) for p in plan.parts]
    seeded = te.seed(facts, clips, {"a": ("r0", sources[0].cues)})
    projected = te.project(seeded.document).cues
    assert [(c.start, c.end, c.trimmed) for c in projected] == [(F(5, 2), F(7, 2), True)]
    whole = te.join_clips(te.set_clip_end(seeded.document, 0, 180), 0)
    assert [(c.start, c.end) for c in te.project(whole).cues] == [(F(5, 2), F(11, 2))]


def test_splitting_a_mixed_rate_clip_at_source_end_can_add_a_frame() -> None:
    # 24fps 세 프레임(원본 끝) → 30fps 시퀀스 3프레임. 원본 2프레임에서 나눈 두 클립(트림으로
    # 생긴다 — 블레이드는 시퀀스 프레임에서 내림이라 원본 1프레임에 떨어진다)은 4프레임이다.
    # 렌더 계획의 같은 규칙(원본 끝 조각만 내림)이라 화면 = 출력은 지켜진다. W-71d가 보여 줄 일이다.
    facts = te.TimelineFacts(SEQUENCE, (te.TimelineSource("b", FD24, 3),))
    doc = te.TimelineDocument(facts, (te.Clip("b", 0, 3),), (), 1, ())
    assert te.edited_frames(doc) == 3
    bladed = te.TimelineDocument(facts, (te.Clip("b", 0, 2), te.Clip("b", 2, 3)), (), 1, ())
    assert te.edited_frames(bladed) == 4


# ─── 원본 범위로 늘이고 줄이기 ─────────────────────────────────────────


def test_clip_edges_extend_into_the_whole_source_but_not_past_it() -> None:
    doc = _doc([("a", 30, 120), ("b", 24, 120)])
    assert _clips(te.set_clip_start(doc, 0, 0)) == [("a", 0, 120), ("b", 24, 120)]
    assert _clips(te.set_clip_end(doc, 1, 240)) == [("a", 30, 120), ("b", 24, 240)]
    with pytest.raises(TimelineEditError):
        te.set_clip_start(doc, 0, -1)
    with pytest.raises(TimelineEditError):
        te.set_clip_end(doc, 0, 301)
    with pytest.raises(TimelineEditError):
        te.set_clip_end(doc, 0, 30)  # 최소 한 프레임


def test_same_source_clips_stop_at_each_other_and_join_when_they_touch() -> None:
    doc = _doc([("a", 0, 60), ("a", 90, 150)])
    with pytest.raises(TimelineEditError):
        te.set_clip_end(doc, 0, 100)
    with pytest.raises(TimelineEditError):
        te.set_clip_start(doc, 1, 59)
    touching = te.set_clip_end(doc, 0, 90)
    assert _clips(te.join_clips(touching, 0)) == [("a", 0, 150)]
    with pytest.raises(TimelineEditError):
        te.join_clips(doc, 0)


@pytest.mark.parametrize(
    "clips",
    [
        [("a", 0, 60), ("b", 0, 24), ("a", 90, 120)],  # 같은 원본이 떨어져 있다
        [("a", 90, 120), ("a", 0, 60)],  # 같은 원본이 거꾸로 간다
        [("a", 0, 60), ("a", 50, 120)],  # 겹친다
        [("z", 0, 10)],  # 없는 원본
        [("a", 10, 10)],  # 빈 클립
    ],
)
def test_validate_refuses_broken_clip_orders(clips: list[tuple[str, int, int]]) -> None:
    with pytest.raises(TimelineEditError):
        te.validate(_doc(clips))


def test_validate_refuses_ticks_past_exact_javascript_integers() -> None:
    huge = te.TimelineFacts(SEQUENCE, (te.TimelineSource("a", FD30, 10**13),))
    document = te.TimelineDocument(huge, (te.Clip("a", 0, 10**13),), (), 1, ())
    with pytest.raises(TimelineEditError):
        te.validate(document)


# ─── 블레이드 · 리플 삭제(섞인 레이트) ──────────────────────────────────


def test_blade_maps_sequence_frames_to_the_clip_source_grid() -> None:
    doc = _doc(MIXED)
    assert _clips(te.blade(doc, 30))[:2] == [("a", 30, 60), ("a", 60, 120)]
    # b 클립(편집 90–210)의 60 시퀀스 프레임 = 2초 = 24fps 48프레임
    assert _clips(te.blade(doc, 150))[1:3] == [("b", 24, 72), ("b", 72, 120)]
    with pytest.raises(TimelineEditError):
        te.blade(doc, 90)  # 편집 점 위


def test_ripple_delete_keeps_head_and_tail_on_each_source_grid() -> None:
    doc = _doc(MIXED)
    assert _clips(te.ripple_delete(doc, 30, 60))[:2] == [("a", 30, 60), ("a", 90, 120)]
    assert _clips(te.ripple_delete(doc, 150, 180))[1:3] == [("b", 24, 72), ("b", 96, 120)]
    # b 전체(편집 90–210)와 c 앞 30 시퀀스 프레임 = 1초 = ⌊29.97⌋ 29프레임
    assert _clips(te.ripple_delete(doc, 60, 240)) == [("a", 30, 90), ("c", 29, 300)]
    with pytest.raises(TimelineEditError):
        te.ripple_delete(doc, 10, 10)


def test_source_point_and_cut_sides() -> None:
    doc = _doc(MIXED)
    assert te.source_point(doc, 150) == (1, 72)
    rate = te.tick_rate(FACTS)
    assert te.source_ms_at(doc, 3 * rate, te.SIDE_START) == ("b", 1000)
    assert te.source_ms_at(doc, 3 * rate, te.SIDE_END) == ("a", 4000)
    assert te.source_ms_at(doc, F(7, 2) * rate, te.SIDE_START) == ("b", 1500)
    assert te.edited_ticks(doc, "b", 1500) == F(7, 2) * rate
    assert te.edited_ticks(doc, "a", 500) == 0  # 클립 앞 — 첫 클립 시작으로
    assert te.edited_ticks(doc, "a", 9000) == 3 * rate  # 클립 뒤 — 그 원본 마지막 클립 끝


# ─── 자막 — 원본 시계에 산다 ────────────────────────────────────────────

CUES: list[tuple[object, ...]] = [
    ("c1", "a", 500, 1500, "걸침", "u1"),
    ("c2", "a", 4500, 5000, "가려짐", "u2"),
    ("c3", "b", 1500, 2500, "둘", "u1"),
]


def test_projection_trims_partial_hides_outside_and_reveals_on_extend() -> None:
    doc = _doc([("a", 30, 120), ("b", 24, 120)], CUES)
    projection = te.project(doc)
    assert [(c.cue_id, c.start, c.end, c.trimmed) for c in projection.cues] == [
        ("c1", F(0), F(1, 2), True),
        ("c3", F(7, 2), F(9, 2), False),
    ]
    assert [c.cue_id for c in projection.hidden] == ["c2"]
    extended = te.project(te.set_clip_end(doc, 0, 150))
    assert [(c.cue_id, c.start, c.end) for c in extended.cues] == [
        ("c1", F(0), F(1, 2)),
        ("c2", F(7, 2), F(4)),
        ("c3", F(9, 2), F(11, 2)),
    ]


def test_cue_ops_stay_on_their_source_clock() -> None:
    doc = _doc([("a", 30, 120), ("b", 24, 120)], CUES)
    moved = te.cue_shift(doc, "c3", 100_000)
    assert _row(next(c for c in moved.cues if c.cue_id == "c3"))[2:4] == (9000, 10000)  # b 끝
    inserted = te.cue_insert(doc, "b", 3000, 3500, "새")
    assert ("n1", "b", 3000, 3500, "새", None) in [_row(c) for c in inserted.cues]
    assert inserted.next_cue == 2
    with pytest.raises(TimelineEditError):
        te.cue_insert(doc, "a", 9000, 10_001, "밖")
    split = te.cue_split(doc, "c3", 2000, "둘", "")
    assert [(c.cue_id, c.start_ms, c.end_ms) for c in split.cues if c.source_id == "b"] == [
        ("c3", 1500, 2000),
        ("n1", 2000, 2500),
    ]


def test_merge_only_with_the_next_visible_cue_of_the_same_source() -> None:
    doc = _doc([("a", 0, 300), ("b", 0, 240)], CUES)
    merged = te.cue_merge_next(doc, "c1")
    assert [(c.cue_id, c.start_ms, c.end_ms, c.text) for c in merged.cues][0] == (
        "c1",
        500,
        5000,
        "걸침 가려짐",
    )
    with pytest.raises(TimelineEditError):
        te.cue_merge_next(doc, "c2")  # 다음 보이는 자막은 다른 원본이다


def test_cues_delete_inside_uses_the_timeline_range() -> None:
    doc = _doc([("a", 30, 120), ("b", 24, 120)], CUES)
    rate = te.tick_rate(FACTS)
    kept = te.cues_delete_inside(doc, 3 * rate, 5 * rate)
    assert [c.cue_id for c in kept.cues] == ["c1", "c2"]  # 가린 c2는 타임라인에 없어 그대로
    with pytest.raises(TimelineEditError):
        te.cues_delete_inside(doc, 2 * rate, 3 * rate)


# ─── 심기 · SRT · 장부 ───────────────────────────────────────────────────


def test_seed_takes_every_cue_of_timeline_sources_polishes_once_and_counts() -> None:
    transcripts = {
        "a": (
            "ra",
            (
                rp.SourceCue("u1", F(1, 2), F(3, 2), "안녕."),
                rp.SourceCue("u2", F(2), F(2), "점"),
                rp.SourceCue("u3", F(3), F(4), "  "),
                rp.SourceCue("u4", F(19, 2), F(21, 2), "끝"),
            ),
        ),
        "b": ("rb", (rp.SourceCue("u1", F(3, 2), F(5, 2), "둘"),)),
        "c": ("rc", ()),
    }
    seeded = te.seed(FACTS, [te.Clip("a", 30, 120)], transcripts)
    assert (seeded.empty, seeded.clamped) == (1, 1)
    assert [_row(c) for c in seeded.document.cues] == [
        ("c1", "a", 500, 1500, "안녕", "u1"),
        ("c2", "a", 2000, 2000, "점", "u2"),
        ("c3", "a", 9500, 10000, "끝", "u4"),
        ("c4", "b", 1500, 2500, "둘", "u1"),
    ]
    assert seeded.document.seeded == (("a", "ra"), ("b", "rb"), ("c", "rc"))


def test_seed_survives_many_cues_at_the_same_time() -> None:
    # 아이디 c10이 c2보다 앞에 오는 문자열 차례여도 심은 문서가 제 검사를 지난다
    same = tuple(rp.SourceCue(f"u{i}", F(1), F(2), f"말{i}") for i in range(12))
    seeded = te.seed(FACTS, [te.Clip("a", 0, 300)], {"a": ("ra", same)})
    assert len(seeded.document.cues) == 12
    te.validate(seeded.document)


def test_srt_lengthens_zero_length_cues_like_the_draft_srt() -> None:
    cues = [("c1", "a", 1000, 1000, "점", None), ("c2", "a", 1500, 2500, "다음", None)]
    doc = _doc([("a", 0, 300)], cues)
    text = te.srt_text(doc)
    # 길이 0 → 다음 늦은 큐 앞 min_gap까지(1.5 − 0.08 = 1.42초), D115 그대로
    assert text.startswith(
        "1\n00:00:01,000 --> 00:00:01,420\n점\n\n2\n00:00:01,500 --> 00:00:02,500\n"
    )


def test_removed_ranges_tile_every_source_including_unused_ones() -> None:
    doc = _doc([("a", 30, 60), ("a", 90, 120), ("b", 24, 120)])
    assert te.removed_ranges(doc) == (
        ("a", 0, 30),
        ("a", 60, 90),
        ("a", 120, 300),
        ("b", 0, 24),
        ("b", 120, 240),
        ("c", 0, 300),
    )


def test_payload_round_trip_and_refusals() -> None:
    doc = _doc([("a", 30, 120)], [("c1", "a", 500, 1500, "걸침", "u1")])
    doc = te.TimelineDocument(doc.facts, doc.clips, doc.cues, 3, (("a", "ra"),))
    payload = te.to_payload(doc)
    assert payload == {
        "clips": [["a", 30, 120]],
        "cues": [["c1", "a", 500, 1500, "걸침", "u1"]],
        "nextCue": 3,
        "seeded": {"a": "ra"},
    }
    assert te.from_payload(json.loads(json.dumps(payload)), FACTS) == doc
    for broken in (
        {**payload, "clips": [["a", 30]]},
        {**payload, "cues": [["c1", "a", 500, 400, "뒤집힘", None]]},
        {**payload, "cues": [["x1", "a", 500, 600, "아이디", None]]},
        {**payload, "nextCue": 0},
        {**payload, "seeded": {"z": "r"}},
    ):
        with pytest.raises(TimelineEditError):
            te.from_payload(broken, FACTS)


def test_shared_cases_match_python() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 20
    for case in cases:
        assert run_case(case) == case["expect"], case["name"]


# ─── 공용 사례 — 브라우저 핵심(`TL`)이 같은 값을 내는지 노드 시험이 같은 파일로 본다 ─────

DOC_OPS = {
    "blade": te.blade,
    "ripple_delete": te.ripple_delete,
    "delete_clips": te.delete_clips,
    "set_clip_start": te.set_clip_start,
    "set_clip_end": te.set_clip_end,
    "join_clips": te.join_clips,
    "cue_set_times": te.cue_set_times,
    "cue_shift": te.cue_shift,
    "cue_set_text": te.cue_set_text,
    "cue_delete": te.cue_delete,
    "cues_delete_inside": te.cues_delete_inside,
    "cue_split": te.cue_split,
    "cue_merge_next": te.cue_merge_next,
    "cue_insert": te.cue_insert,
}


def case_facts(raw: dict) -> te.TimelineFacts:
    sequence = raw["sequence"]
    return te.TimelineFacts(
        sequence=rp.SequenceFormat(
            ORIENTATION_LANDSCAPE, 1920, 1080, F(sequence["num"], sequence["den"])
        ),
        sources=tuple(
            te.TimelineSource(s["id"], F(s["num"], s["den"]), s["frames"]) for s in raw["sources"]
        ),
    )


def _value(document: te.TimelineDocument, op: str, args: list) -> object:
    rate = te.tick_rate(document.facts)
    if op == "layout":
        return [[s.source_id, s.out_start, s.out_frames] for s in te.layout(document)]
    if op == "project":
        projection = te.project(document)
        return {
            "cues": [[c.cue_id, int(c.start * rate), int(c.end * rate), c.trimmed]
                     for c in projection.cues],
            "hidden": [c.cue_id for c in projection.hidden],
            "total": projection.total_frames,
        }  # fmt: skip
    if op == "warnings":
        return [[w.cue_id, w.kind] for w in te.warnings(document)]
    if op == "removed_ranges":
        return [list(row) for row in te.removed_ranges(document)]
    if op in ("source_point", "source_ms_at", "clip_bounds"):
        return list(getattr(te, op)(document, *args))
    if op == "edited_ticks":
        return te.edited_ticks(document, *args)
    return te.to_payload(DOC_OPS[op](document, *args))


def run_case(case: dict) -> dict:
    facts = case_facts(case["facts"])
    try:
        document = te.from_payload(case["doc"], facts)
        return {"value": _value(document, case["op"], case["args"])}
    except TimelineEditError:
        return {"error": True}
