"""추천만 이어붙이는 렌더 계획 + SRT 투영(W-44a, D100 §5a) — 순수 계층의 시험.

입력은 값뿐이다: 원본 시계·등급 구간·검토 표시·원본 자막. 남길 것(parts)과 뺀 것(dropped)이
원본마다 `[0, extent]`를 빈틈없이 덮고, 뺀 구간은 모두 까닭과 검토 상태를 단다(규칙 9·D35).
"""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction as F

import pytest

from vlogsrt.project.raw import render_plan as rp
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.config import (
    FINALCUT_TIMELINE,
    FRAME_RATE_LOWER,
    ORIENTATION_LANDSCAPE,
    ORIENTATION_PORTRAIT,
    RENDER_SETTINGS,
    FinalCutTimelineSettings,
    RenderSettings,
)
from vlogsrt.project.raw.finalcut_export import caption_text
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    RECOMMENDED,
    SELECTION_EXCLUDED,
    SELECTION_INCLUDED,
    ReviewSegment,
    make_source_clock,
)

FD30 = F(1, 30)
FD24 = F(1, 24)
SETTINGS = RenderSettings(min_part_seconds=F(1), merge_gap_seconds=F(0))


def _tier(
    start: F | int, end: F | int, tier: str = tiering.TIER_GREEN, **kw: object
) -> rp.TierPiece:
    return rp.TierPiece(F(start), F(end), tier, **kw)  # type: ignore[arg-type]


def _mark(start: F | int, end: F | int, **kw: object) -> rp.SegmentMark:
    kw.setdefault("selection", SELECTION_INCLUDED)
    return rp.SegmentMark(F(start), F(end), **kw)  # type: ignore[arg-type]


def _source(
    source_id: str = "a",
    *,
    seconds: int = 10,
    fd: F = FD30,
    size: tuple[int, int] = (1920, 1080),
    grid: str = rp.GRID_UNIFORM,
    tiers: tuple[rp.TierPiece, ...] | None = None,
    segments: tuple[rp.SegmentMark, ...] | None = None,
    cues: tuple[rp.SourceCue, ...] = (),
) -> rp.RenderSource:
    frames = int(seconds / fd)
    return rp.RenderSource(
        source_id=source_id,
        name=f"{source_id}.mov",
        clock=rp.FrameClock(fd, frames),
        width=size[0],
        height=size[1],
        grid=grid,
        tiers=tiers,
        segments=segments,
        cues=cues,
    )


def _ranges(plan: rp.RenderPlan) -> list[tuple[F, F, str]]:
    return [(d.start, d.end, d.reason) for d in plan.dropped]


def _parts(plan: rp.RenderPlan) -> list[tuple[F, F, str]]:
    return [(p.start, p.end, p.reason) for p in plan.parts]


def _require_tiles(plan: rp.RenderPlan, sources: list[rp.RenderSource]) -> None:
    for source in sources:
        rows = sorted(
            [(p.start, p.end) for p in plan.parts if p.source_id == source.source_id]
            + [(d.start, d.end) for d in plan.dropped if d.source_id == source.source_id]
        )
        assert rows[0][0] == 0
        assert rows[-1][1] == source.clock.extent
        assert all(a[1] == b[0] for a, b in zip(rows, rows[1:], strict=False))
        assert all(start < end for start, end in rows)


# ── 남길 범위 ──────────────────────────────────────────────────────────────


def test_green_is_kept_and_yellow_is_dropped_with_its_reason() -> None:
    source = _source(tiers=(_tier(0, 4), _tier(4, 10, tiering.TIER_YELLOW)))
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(0), F(4), rp.PART_AUTO_GREEN)]
    (dropped,) = plan.dropped
    assert (dropped.start, dropped.end, dropped.reason) == (F(4), F(10), rp.DROP_YELLOW)
    assert dropped.review_state == rp.REVIEW_NONE
    assert dropped.tier == tiering.TIER_YELLOW
    assert plan.parts[0].start_frame == 0 and plan.parts[0].end_frame == 120


def test_a_hold_yellow_keeps_its_hold_flag_in_the_ledger() -> None:
    source = _source(tiers=(_tier(0, 10, tiering.TIER_YELLOW, hold=True),))
    plan = rp.build_plan([source], SETTINGS)
    assert plan.parts == ()
    assert plan.dropped[0].hold is True
    assert plan.sequence is None
    assert plan.total_frames == 0


def test_human_recommended_adds_a_yellow_range_and_merges_with_adjacent_green() -> None:
    source = _source(
        tiers=(_tier(0, 4), _tier(4, 10, tiering.TIER_YELLOW)),
        segments=(
            _mark(0, 4),
            _mark(4, 7, override="recommended"),
            _mark(7, 10),
        ),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(0), F(7), rp.PART_HUMAN_RECOMMENDED)]
    assert _ranges(plan) == [(F(7), F(10), rp.DROP_YELLOW)]
    assert plan.dropped[0].review_state == rp.REVIEW_UNREVIEWED


def test_human_normal_removes_green_inside_its_range() -> None:
    source = _source(
        tiers=(_tier(0, 10),),
        segments=(_mark(0, 3), _mark(3, 5, override="normal"), _mark(5, 10)),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(0), F(3), rp.PART_AUTO_GREEN), (F(5), F(10), rp.PART_AUTO_GREEN)]
    assert _ranges(plan) == [(F(3), F(5), rp.DROP_HUMAN_NORMAL)]
    assert plan.dropped[0].review_state == rp.REVIEW_HUMAN


def test_human_exclusion_removes_everything_and_beats_a_recommended_override() -> None:
    source = _source(
        tiers=(_tier(0, 4), _tier(4, 6, tiering.TIER_RED), _tier(6, 10)),
        segments=(
            _mark(0, 2),
            _mark(2, 4, selection=SELECTION_EXCLUDED, override="recommended"),
            _mark(4, 6, selection=SELECTION_EXCLUDED),
            _mark(6, 10),
        ),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(0), F(2), rp.PART_AUTO_GREEN), (F(6), F(10), rp.PART_AUTO_GREEN)]
    assert _ranges(plan) == [
        (F(2), F(4), rp.DROP_HUMAN_EXCLUDED),
        (F(4), F(6), rp.DROP_RED_CONFIRMED),
    ]
    assert {d.review_state for d in plan.dropped} == {rp.REVIEW_HUMAN}
    assert plan.unreviewed_red_dropped == 0


def test_unreviewed_red_is_dropped_listed_and_counted_but_does_not_block() -> None:
    source = _source(
        tiers=(_tier(0, 3), _tier(3, 5, tiering.TIER_RED), _tier(5, 7), _tier(7, 10, "red")),
        segments=(
            _mark(0, 3),
            _mark(3, 5, selection=SELECTION_EXCLUDED, unreviewed_auto_excluded=True),
            _mark(5, 10),
        ),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(0), F(3), rp.PART_AUTO_GREEN), (F(5), F(7), rp.PART_AUTO_GREEN)]
    assert _ranges(plan) == [
        (F(3), F(5), rp.DROP_RED_UNREVIEWED),
        (F(7), F(10), rp.DROP_RED_UNREVIEWED),
    ]
    assert [d.review_state for d in plan.dropped] == [rp.REVIEW_AUTO_EXCLUDED, rp.REVIEW_UNREVIEWED]
    assert plan.unreviewed_red_dropped == 2


def test_red_a_human_has_seen_is_confirmed_not_unreviewed() -> None:
    source = _source(
        tiers=(_tier(0, 5), _tier(5, 10, tiering.TIER_RED)),
        segments=(_mark(0, 5), _mark(5, 10, decided=True)),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert _ranges(plan) == [(F(5), F(10), rp.DROP_RED_CONFIRMED)]
    assert plan.unreviewed_red_dropped == 0


def test_a_stale_unreviewed_auto_exclusion_does_not_remove_green() -> None:
    """미검토 자동 제외는 사람 표시가 아니다 — Final Cut 보내기(D80·D93)처럼 녹색은 남는다."""
    source = _source(
        tiers=(_tier(0, 10),),
        segments=(_mark(0, 10, selection=SELECTION_EXCLUDED, unreviewed_auto_excluded=True),),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(0), F(10), rp.PART_AUTO_GREEN)]


def test_ranges_without_a_tier_verdict_are_unanalyzed() -> None:
    plan = rp.build_plan([_source(tiers=(_tier(0, 4),)), _source("b", tiers=None)], SETTINGS)
    assert _ranges(plan) == [(F(4), F(10), rp.DROP_UNANALYZED), (F(0), F(10), rp.DROP_UNANALYZED)]


# ── 합치기 · 최소 길이 · 격자 ─────────────────────────────────────────────


def test_adjacent_green_pieces_merge_into_one_part() -> None:
    source = _source(tiers=(_tier(0, 2), _tier(2, 5), _tier(5, 10, tiering.TIER_YELLOW)))
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(0), F(5), rp.PART_AUTO_GREEN)]


def test_merge_gap_bridges_only_short_automatic_yellow_gaps() -> None:
    tiers = (
        _tier(0, 3),
        _tier(3, F(16, 5), tiering.TIER_YELLOW),
        _tier(F(16, 5), 6),
        _tier(6, F(31, 5), tiering.TIER_YELLOW),
        _tier(F(31, 5), 10),
    )
    segments = (_mark(0, 6), _mark(6, F(31, 5), selection=SELECTION_EXCLUDED), _mark(F(31, 5), 10))
    exact = rp.build_plan([_source(tiers=tiers, segments=segments)], SETTINGS)
    assert len(exact.parts) == 3
    bridged = RenderSettings(min_part_seconds=F(1), merge_gap_seconds=F(1, 2))
    plan = rp.build_plan([_source(tiers=tiers, segments=segments)], bridged)
    assert _parts(plan) == [(F(0), F(6), rp.PART_AUTO_GREEN), (F(31, 5), F(10), rp.PART_AUTO_GREEN)]
    assert plan.parts[0].bridged_seconds == F(1, 5)
    assert _ranges(plan) == [(F(6), F(31, 5), rp.DROP_HUMAN_EXCLUDED)]


def test_too_short_parts_are_dropped_and_counted_never_silently() -> None:
    source = _source(
        tiers=(_tier(0, F(1, 2)), _tier(F(1, 2), 5, tiering.TIER_YELLOW), _tier(5, 10)),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert _parts(plan) == [(F(5), F(10), rp.PART_AUTO_GREEN)]
    assert _ranges(plan) == [(F(0), F(1, 2), rp.DROP_TOO_SHORT), (F(1, 2), F(5), rp.DROP_YELLOW)]
    assert plan.too_short == 1


def test_part_edges_snap_inward_and_slivers_join_their_dropped_neighbour() -> None:
    edge_in = F(1, 100) + 2  # 2.01초 — 60.3프레임
    edge_out = F(799, 100)  # 7.99초 — 239.7프레임
    source = _source(
        tiers=(
            _tier(0, edge_in, tiering.TIER_YELLOW),
            _tier(edge_in, edge_out),
            _tier(edge_out, 10, tiering.TIER_YELLOW),
        )
    )
    plan = rp.build_plan([source], SETTINGS)
    (part,) = plan.parts
    assert (part.start_frame, part.end_frame) == (61, 239)
    assert (part.start, part.end) == (F(61, 30), F(239, 30))
    assert _ranges(plan) == [(F(0), F(61, 30), rp.DROP_YELLOW), (F(239, 30), F(10), rp.DROP_YELLOW)]
    _require_tiles(plan, [source])


def test_vfr_map_sources_are_excluded_whole_with_their_reason() -> None:
    vfr = _source("v", grid=rp.GRID_MAP, tiers=(_tier(0, 10),))
    plan = rp.build_plan([_source(tiers=(_tier(0, 10),)), vfr], SETTINGS)
    assert [p.source_id for p in plan.parts] == ["a"]
    assert [(u.source_id, u.reason) for u in plan.unsupported] == [("v", rp.DROP_UNSUPPORTED_GRID)]
    assert [(d.source_id, d.reason) for d in plan.dropped] == [("v", rp.DROP_UNSUPPORTED_GRID)]


def test_a_source_the_renderer_refuses_is_excluded_whole_with_its_reason() -> None:
    """렌더러가 거부한 원본(D113)은 map처럼 통째로 빼고 까닭 글을 싣는다 — 초안을 막지 않는다."""
    refused = replace(
        _source("r", tiers=(_tier(0, 10),), cues=(_cue("u1", 1, 2),)),
        refusal="렌더할 수 없다 — 패킷 증거가 선언 창을 넘는다",
    )
    plan = rp.build_plan([_source(tiers=(_tier(0, 10),)), refused], SETTINGS)
    assert [p.source_id for p in plan.parts] == ["a"]
    assert [(u.source_id, u.reason, u.detail) for u in plan.unsupported] == [
        ("r", rp.DROP_UNRENDERABLE, "렌더할 수 없다 — 패킷 증거가 선언 창을 넘는다")
    ]
    assert [(d.source_id, d.reason) for d in plan.dropped] == [("r", rp.DROP_UNRENDERABLE)]
    assert plan.cue_counts.source == 0  # 뺀 원본의 자막은 투영하지 않는다
    _require_tiles(plan, [_source(tiers=(_tier(0, 10),)), refused])
    document = rp.report_document(plan)
    assert document["unsupported"] == [
        {
            "source_id": "r",
            "name": "r.mov",
            "reason": rp.DROP_UNRENDERABLE,
            "detail": "렌더할 수 없다 — 패킷 증거가 선언 창을 넘는다",
        }
    ]


def test_kept_and_dropped_tile_every_source_exactly() -> None:
    sources = [
        _source(
            "a",
            tiers=(_tier(0, F(7, 3)), _tier(F(7, 3), F(41, 7), "yellow"), _tier(F(41, 7), 10)),
            segments=(_mark(0, 1), _mark(1, F(4), override="normal"), _mark(4, 10)),
        ),
        _source("b", fd=FD24, tiers=(_tier(0, 4, "red"),)),
        _source("c", grid=rp.GRID_MAP),
        _source("d", grid=rp.GRID_SNAP, tiers=(_tier(0, 10),)),
    ]
    plan = rp.build_plan(sources, SETTINGS)
    _require_tiles(plan, sources)


def test_kept_ranges_of_one_source_are_exactly_the_plan_parts_of_that_source() -> None:
    """작업대 레일의 「내보낼 부분」(W-46b) — 원본 하나로 잰 범위가 전체 계획의 그 원본 조각과
    같다."""
    sources = [
        _source(
            "a",
            tiers=(_tier(0, F(7, 3)), _tier(F(7, 3), F(41, 7), "yellow"), _tier(F(41, 7), 10)),
            segments=(
                _mark(0, 1),
                _mark(1, F(4), override="normal"),
                _mark(4, 5, override=RECOMMENDED),
                _mark(5, F(13, 2), selection=SELECTION_EXCLUDED),
                _mark(F(13, 2), 10),
            ),
        ),
        _source("b", fd=FD24, tiers=(_tier(0, 4, "red"), _tier(4, 10))),
        _source("c", grid=rp.GRID_MAP, tiers=(_tier(0, 10),)),
    ]
    plan = rp.build_plan(sources, SETTINGS)
    for source in sources:
        kept = rp.kept_ranges(source, SETTINGS)
        parts = [(p.start, p.end, p.reason) for p in plan.parts if p.source_id == source.source_id]
        assert [(k.start, k.end, k.reason) for k in kept] == parts
    assert rp.kept_ranges(sources[2], SETTINGS) == ()  # map 원본은 초안이 통째로 뺀다(D68)
    human = rp.kept_ranges(sources[0], SETTINGS)
    assert (F(4), F(5), rp.PART_HUMAN_RECOMMENDED) in [(k.start, k.end, k.reason) for k in human]


def test_kept_ranges_ignore_the_frame_size_the_rail_does_not_know() -> None:
    """레일은 화면 크기를 모른다 — 남길 범위는 크기와 무관하고, 렌더 계획만 크기를 검사한다."""
    sized = _source(tiers=(_tier(0, 10),))
    unsized = replace(sized, width=0, height=0)
    assert rp.kept_ranges(unsized, SETTINGS) == rp.kept_ranges(sized, SETTINGS)
    with pytest.raises(rp.RenderPlanError):
        rp.build_plan([unsized], SETTINGS)
    with pytest.raises(rp.RenderPlanError):
        rp.kept_ranges(_source(tiers=(_tier(0, 6), _tier(5, 10))), SETTINGS)


def test_bad_inputs_are_refused() -> None:
    with pytest.raises(rp.RenderPlanError):
        rp.build_plan([_source(tiers=(_tier(0, 6), _tier(5, 10)))], SETTINGS)
    with pytest.raises(rp.RenderPlanError):
        rp.build_plan([_source(segments=(_mark(0, 4, unreviewed_auto_excluded=True),))], SETTINGS)
    with pytest.raises(rp.RenderPlanError):
        rp.build_plan([_source(), _source()], SETTINGS)
    with pytest.raises(rp.RenderPlanError):
        rp.build_plan([_source(grid="vfr")], SETTINGS)


# ── 시퀀스 포맷 ───────────────────────────────────────────────────────────


def _kept(source_id: str, seconds: int, **kw: object) -> rp.RenderSource:
    return _source(source_id, tiers=(_tier(0, seconds),), seconds=seconds, **kw)  # type: ignore[arg-type]


def test_orientation_follows_kept_seconds_and_ties_use_the_setting() -> None:
    plan = rp.build_plan([_kept("a", 4), _kept("b", 6, size=(1080, 1920))], SETTINGS)
    assert plan.sequence is not None
    assert plan.sequence.orientation == ORIENTATION_PORTRAIT
    assert (plan.sequence.width, plan.sequence.height) == (1080, 1920)
    assert [(p.scale, p.pad) for p in plan.parts] == [(True, True), (False, False)]
    tied = [_kept("a", 5), _kept("b", 5, size=(1080, 1920))]
    assert rp.build_plan(tied, SETTINGS).sequence.orientation == ORIENTATION_LANDSCAPE  # type: ignore[union-attr]
    portrait_tie = RenderSettings(
        min_part_seconds=F(1), timeline=FinalCutTimelineSettings(orientation_tie="portrait")
    )
    assert rp.build_plan(tied, portrait_tie).sequence.orientation == ORIENTATION_PORTRAIT  # type: ignore[union-attr]


def test_frame_rate_follows_kept_seconds_within_the_orientation_and_ties_use_the_setting() -> None:
    plan = rp.build_plan(
        [_kept("a", 3), _kept("b", 5, fd=FD24), _kept("c", 9, fd=F(1, 60), size=(1080, 1920))],
        SETTINGS,
    )
    assert plan.sequence is not None
    assert plan.sequence.orientation == ORIENTATION_PORTRAIT
    assert plan.sequence.frame_duration == F(1, 60)
    landscape = rp.build_plan([_kept("a", 3), _kept("b", 5, fd=FD24)], SETTINGS)
    assert landscape.sequence.frame_duration == FD24  # type: ignore[union-attr]
    assert [p.fps_convert for p in landscape.parts] == [True, False]
    tied = [_kept("a", 5), _kept("b", 5, fd=FD24)]
    assert rp.build_plan(tied, SETTINGS).sequence.frame_duration == FD30  # type: ignore[union-attr]
    lower = RenderSettings(
        min_part_seconds=F(1), timeline=FinalCutTimelineSettings(frame_rate_tie=FRAME_RATE_LOWER)
    )
    assert rp.build_plan(tied, lower).sequence.frame_duration == FD24  # type: ignore[union-attr]


def test_sequence_size_is_the_most_kept_size_of_the_orientation() -> None:
    plan = rp.build_plan(
        [_kept("a", 3, size=(3840, 2160)), _kept("b", 5), _kept("c", 4, size=(3840, 2160))],
        SETTINGS,
    )
    assert plan.sequence is not None
    assert (plan.sequence.width, plan.sequence.height) == (3840, 2160)
    assert [(p.scale, p.pad) for p in plan.parts] == [(False, False), (True, False), (False, False)]


def test_render_settings_reuse_the_final_cut_tie_rules() -> None:
    assert RENDER_SETTINGS.timeline is FINALCUT_TIMELINE


# ── 출력 타임라인 ─────────────────────────────────────────────────────────


def test_same_rate_parts_keep_exact_frame_counts_and_cumulative_offsets() -> None:
    source = _source(tiers=(_tier(0, 2), _tier(2, 4, "yellow"), _tier(4, 9), _tier(9, 10, "red")))
    plan = rp.build_plan([source], SETTINGS)
    assert [(p.output_start_frame, p.output_frames) for p in plan.parts] == [(0, 60), (60, 150)]
    assert [(p.output_start, p.output_end) for p in plan.parts] == [(F(0), F(2)), (F(2), F(7))]
    assert plan.total_frames == 210
    assert plan.total_seconds == F(7)


def test_cumulative_rounding_over_many_mixed_rate_parts_stays_bounded() -> None:
    """25프레임 @24 = 31.25프레임 @30 — 따로 반올림하면 100개에 25프레임(0.83초)이 샌다."""
    tiers = []
    for index in range(100):
        start = F(index * 50, 24)
        tiers += [
            _tier(start, start + F(25, 24)),
            _tier(start + F(25, 24), start + F(50, 24), "yellow"),
        ]
    slow = _source(
        "slow", seconds=209, fd=FD24, tiers=tuple(tiers[:-1] + [_tier(F(4975, 24), 209, "yellow")])
    )
    main = _kept("main", 200)
    plan = rp.build_plan([main, slow], SETTINGS)
    assert plan.sequence is not None and plan.sequence.frame_duration == FD30
    slow_parts = [p for p in plan.parts if p.source_id == "slow"]
    assert len(slow_parts) == 100
    assert {p.output_frames for p in slow_parts} == {31, 32}
    total = F(0)
    for part in plan.parts:
        total += part.end - part.start
        edge = part.output_start_frame + part.output_frames
        assert abs(edge - total / FD30) <= F(1, 2)
    assert plan.total_frames == 6000 + 3125
    assert plan.padded == 0


def test_a_part_at_the_end_of_its_source_never_runs_past_the_media() -> None:
    """누적 반올림이 올림이어도 원본 끝에 닿은 조각은 남은 미디어로 내린다(D71과 같은 선)."""
    middle = _source(
        "p", seconds=2, fd=FD24, tiers=(_tier(0, F(25, 24)), _tier(F(25, 24), 2, "yellow"))
    )
    edge = replace(_source("e", seconds=2, fd=FD24), clock=rp.FrameClock(FD24, 25))
    edge = replace(edge, tiers=(_tier(0, F(25, 24)),))
    plan = rp.build_plan([_kept("main", 3), middle, edge], SETTINGS)
    # 90 + 31.25 = 121.25 → 121, 그 뒤 152.5 → 가장 가까운 153이지만 남은 미디어는 31.25프레임이다.
    assert [p.output_frames for p in plan.parts] == [90, 31, 31]
    last = plan.parts[-1]
    assert last.end == edge.clock.extent
    assert last.output_frames * FD30 <= last.end - last.start
    assert plan.total_frames == 152


# ── SRT 투영 ─────────────────────────────────────────────────────────────


def _cue(uid: str, start: F | int, end: F | int, text: str = "말") -> rp.SourceCue:
    return rp.SourceCue(uid, F(start), F(end), text)


def test_cues_are_clipped_split_shifted_and_dropped() -> None:
    source = _source(
        tiers=(
            _tier(0, 2, "yellow"),
            _tier(2, 5),
            _tier(5, 6, "yellow"),
            _tier(6, 9),
            _tier(9, 10, "yellow"),
        ),
        cues=(
            _cue("u1", 1, 3, "앞이 잘린다"),
            _cue("u2", 3, 4, "안쪽"),
            _cue("u3", F(9, 2), F(13, 2), "틈을 넘는다"),
            _cue("u4", F(51, 10), F(29, 5), "틈 안"),
            _cue("u5", F(17, 2), F(19, 2), "뒤가 잘린다"),
        ),
    )
    plan = rp.build_plan([source], SETTINGS)
    rows = [(c.utterance_id, c.start, c.end, c.clipped) for c in plan.cues]
    assert rows == [
        ("u1", F(0), F(1), True),
        ("u2", F(1), F(2), False),
        ("u3", F(5, 2), F(3), True),
        ("u3", F(3), F(7, 2), True),
        ("u5", F(11, 2), F(6), True),
    ]
    counts = plan.cue_counts
    assert (counts.source, counts.kept, counts.split, counts.dropped) == (5, 4, 1, 1)
    assert counts.clipped == 4
    assert len(plan.cues) == counts.kept + counts.split


def test_srt_text_uses_half_up_milliseconds_and_polished_text() -> None:
    source = _source(
        tiers=(_tier(0, 10),),
        cues=(
            _cue("u1", F(10005, 10000), F(2), "안녕하세요."),
            _cue("u2", F(3), F(3), "점"),
            _cue("u3", F(4), F(5), "첫 줄...\n둘째 줄."),
            _cue("u4", F(6), F(7), "   "),
        ),
    )
    plan = rp.build_plan([source], SETTINGS)
    assert rp.srt_text(plan) == (
        "1\n00:00:01,001 --> 00:00:02,000\n안녕하세요\n"
        "\n2\n00:00:03,000 --> 00:00:03,000\n점\n"
        "\n3\n00:00:04,000 --> 00:00:05,000\n첫 줄…\n둘째 줄\n"
    )
    counts = plan.cue_counts
    assert (counts.zero_length, counts.empty, counts.kept) == (1, 1, 3)


def test_caption_text_matches_the_final_cut_export_polish() -> None:
    for text in ("안녕하세요.", "1.5배 빠르게.", "음...\n그래.", "...", "  여러   칸  "):
        assert rp.caption_text(text) == caption_text(text)


def test_projected_cues_never_leave_the_output_timeline() -> None:
    slow = _source(
        "slow", seconds=3, fd=FD24, tiers=(_tier(0, F(25, 24)), _tier(F(25, 24), 3, "yellow"))
    )
    slow = replace(slow, cues=(_cue("u", 0, F(25, 24)),))
    plan = rp.build_plan([_kept("main", 3), slow], SETTINGS)
    assert plan.total_frames == 90 + 31
    for cue in plan.cues:
        assert 0 <= cue.start <= cue.end <= plan.total_seconds
    (cue,) = plan.cues
    assert cue.end == plan.total_seconds and cue.clipped


def test_inverted_cues_are_kept_as_points_and_counted() -> None:
    source = _source(tiers=(_tier(0, 10),), cues=(_cue("u", 3, 2),))
    plan = rp.build_plan([source], SETTINGS)
    (cue,) = plan.cues
    assert (cue.start, cue.end) == (F(3), F(3))
    assert plan.cue_counts.inverted == 1


# ── 보고서 ───────────────────────────────────────────────────────────────


def test_report_document_is_json_and_counts_everything() -> None:
    sources = [
        _source(
            "a",
            tiers=(_tier(0, 4), _tier(4, 6, "red"), _tier(6, 10, "yellow")),
            segments=(
                _mark(0, 4),
                _mark(4, 6, selection=SELECTION_EXCLUDED, unreviewed_auto_excluded=True),
                _mark(6, 10, override="recommended"),
            ),
            cues=(_cue("u1", 1, 2), _cue("u2", 5, F(11, 2))),
        ),
        _source("v", grid=rp.GRID_MAP),
    ]
    plan = rp.build_plan(sources, SETTINGS)
    document = rp.report_document(plan)
    json.dumps(document, ensure_ascii=False)
    assert document["unreviewed_red_dropped"] == 1
    assert document["cue_counts"] == {
        "source": 2, "kept": 1, "clipped": 0, "split": 0, "dropped": 1,
        "empty": 0, "inverted": 0, "zero_length": 0,
    }  # fmt: skip
    assert [p["reason"] for p in document["parts"]] == [
        rp.PART_AUTO_GREEN,
        rp.PART_HUMAN_RECOMMENDED,
    ]
    assert [d["reason"] for d in document["dropped"]] == [
        rp.DROP_RED_UNREVIEWED,
        rp.DROP_UNSUPPORTED_GRID,
    ]
    assert document["unsupported"] == [
        {"source_id": "v", "name": "v.mov", "reason": rp.DROP_UNSUPPORTED_GRID}
    ]
    assert document["sequence"]["width"] == 1920
    assert document["total_frames"] == 240
    assert document["too_short"] == 0
    assert document["rules"]["min_part_seconds"] == {"num": 1, "den": 1}
    assert document["rules"]["orientation_tie"] == FINALCUT_TIMELINE.orientation_tie
    first = document["parts"][0]
    assert first["source_start"] == {"num": 0, "den": 1}
    assert first["output_end"] == {"num": 4, "den": 1}


# ── 설정 · 변환 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_part_seconds": F(-1)},
        {"merge_gap_seconds": F(-1, 2)},
        {"container": "mkv"},
        {"video_codecs": ()},
        {"video_codecs": ("libx264", "libx264")},
        {"crf": 99},
        {"preset": "warp"},
        {"audio_rate": 0},
        {"audio_channels": 0},
        {"audio_bitrate_kbps": 0},
        {"pad_color": " "},
    ],
)
def test_render_settings_refuse_bad_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RenderSettings(**overrides)  # type: ignore[arg-type]


def test_converters_read_the_review_clock_and_the_tier_report() -> None:
    clock = make_source_clock("a", "0" * 64, 1, 90000, 9000, 9000 + 300 * 3000, FD30)
    frame_clock = rp.frame_clock(clock)
    assert (frame_clock.frame_duration, frame_clock.frame_count, frame_clock.origin) == (
        FD30, 300, F(1, 10)
    )  # fmt: skip
    segment = ReviewSegment(
        "s", "a", 0, 0, 9000 + 30 * 3000, 9000 + 60 * 3000, "speech_detected",
        (AUTO_EXCLUDED_UNREVIEWED_TAG,), "normal", "recommended", SELECTION_EXCLUDED, "",
    )  # fmt: skip
    mark = rp.segment_mark(segment, clock, decided=True)
    assert (mark.start, mark.end, mark.override) == (F(1), F(2), "recommended")
    assert mark.unreviewed_auto_excluded and mark.decided
    audio = tiering.AudioJudgment(tiering.AUDIO_SPEECH_DENSE, F(1), "말", ())
    video = tiering.VideoJudgment(tiering.VIDEO_FAILED, "실패", (), ())
    span = tiering.TierSpan(F(0), F(10), tiering.TIER_YELLOW, "rule", audio, video, None)
    report = tiering.TierReport("v", tiering.TIER_SETTINGS, "x", (span,))
    (piece,) = rp.tier_pieces(report)
    assert (piece.start, piece.end, piece.tier, piece.hold) == (F(0), F(10), "yellow", True)
