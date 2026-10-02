"""A5 조각 검토 도메인의 계약 — 덮개·리비전 불변·거부·직렬화·보기 상태 분리.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §6·§10.3·§12·§13·16-A5. 전부
합성 값만 쓴다 — 네트워크·ffmpeg·파일이 없고, W1 계획 기록은 **형태가 같은
dataclass**로 재현해 구조 계약(Protocol)만 증명한다.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from fractions import Fraction

import pytest

from vlogsrt.project.raw.segment_review import (
    DISPLAY_VIDEO_ONLY,
    DISPLAY_VIDEO_WITH_TRANSCRIPT,
    RECOMMENDATIONS,
    REVIEW_SCHEMA_VERSION,
    SELECTION_EXCLUDED,
    SELECTION_INCLUDED,
    SPEECH_UNCONFIRMED_EMPTY,
    ReviewEdit,
    ReviewEditError,
    ReviewParseError,
    ReviewValidationError,
    ReviewViewState,
    SegmentPlanSource,
    SourceClock,
    StaleRevisionError,
    adjust_boundary,
    apply_edit,
    chronological,
    content_fingerprint,
    filter_segments,
    initial_revision,
    make_source_clock,
    merge_edit,
    merge_segments,
    next_segment,
    parse_revision,
    previous_segment,
    reextract_segment_ids,
    serialize_revision,
    set_notes,
    set_override,
    set_selection,
    split_edit,
    split_segment,
)

TB_NUM, TB_DEN = 1, 90000
FRAME_30 = Fraction(1, 30)
TICKS_PER_FRAME = 3000


@dataclass(frozen=True, slots=True)
class W1ShapedEntry:
    """W1이 제안한 `SegmentPlanEntry`와 같은 필드 — 발화 상태·태그가 없다(A3 계획)."""

    segment_id: str
    asset_id: str
    batch_dir: str
    source_order: int
    segment_order: int
    time_base_num: int
    time_base_den: int
    core_start_pts: int
    core_end_pts: int
    media_start_pts: int
    media_end_pts: int
    core_first_frame: int
    core_after_last_frame: int
    source_frame_count: int
    recommendation: str
    basis: str


@dataclass(frozen=True, slots=True)
class EnrichedEntry(W1ShapedEntry):
    """A4가 등급을 붙인 계획 조각 — 발화 상태·태그가 자라난 같은 형태."""

    speech_status: str = SPEECH_UNCONFIRMED_EMPTY
    visual_tags: tuple[str, ...] = ()


def clock_a() -> SourceClock:
    return make_source_clock("source_a", "sha-a", TB_NUM, TB_DEN, 0, 3_600_000, FRAME_30)


def clock_b() -> SourceClock:
    return make_source_clock("source_b", "sha-b", TB_NUM, TB_DEN, 0, 1_800_000, FRAME_30)


def plan_entries() -> tuple[EnrichedEntry, ...]:
    """원본 A(40초·4조각) + 원본 B(20초·2조각) — 경계는 전부 30fps 격자다."""
    return (
        EnrichedEntry(
            "a1",
            "source_a",
            "batch",
            0,
            1,
            TB_NUM,
            TB_DEN,
            0,
            900_000,
            0,
            900_000,
            0,
            300,
            1200,
            "normal",
            "length",
            speech_status="speech_detected",
            visual_tags=("walking",),
        ),
        EnrichedEntry(
            "a2",
            "source_a",
            "batch",
            0,
            2,
            TB_NUM,
            TB_DEN,
            900_000,
            1_980_000,
            900_000,
            1_980_000,
            300,
            660,
            1200,
            "recommended",
            "speech_end",
            speech_status="speech_detected",
            visual_tags=("talking", "arrival"),
        ),
        EnrichedEntry(
            "a3",
            "source_a",
            "batch",
            0,
            3,
            TB_NUM,
            TB_DEN,
            1_980_000,
            2_790_000,
            1_980_000,
            2_790_000,
            660,
            930,
            1200,
            "review_required",
            "uncertain",
            visual_tags=("scenery",),
        ),
        EnrichedEntry(
            "a4",
            "source_a",
            "batch",
            0,
            4,
            TB_NUM,
            TB_DEN,
            2_790_000,
            3_600_000,
            2_790_000,
            3_600_000,
            930,
            1200,
            1200,
            "normal",
            "length",
            speech_status="audio_absent",
        ),
        EnrichedEntry(
            "b1",
            "source_b",
            "batch",
            1,
            1,
            TB_NUM,
            TB_DEN,
            0,
            1_080_000,
            0,
            1_080_000,
            0,
            360,
            600,
            "normal",
            "length",
        ),
        EnrichedEntry(
            "b2",
            "source_b",
            "batch",
            1,
            2,
            TB_NUM,
            TB_DEN,
            1_080_000,
            1_800_000,
            1_080_000,
            1_800_000,
            360,
            600,
            600,
            "recommended",
            "speech_end",
        ),
    )


def base_revision():
    return initial_revision("proj", "plan-1", (clock_a(), clock_b()), plan_entries())


def assert_covered(revision, clocks, *, where="") -> None:
    """§6.1 덮개 증명 — 원점 시작·이웃 접촉·종료 닫기·순번=시작 순서."""
    for clock in clocks:
        tiles = sorted(
            (s for s in revision.segments if s.source_id == clock.source_id),
            key=lambda s: s.start_pts,
        )
        assert tiles, where
        assert tiles[0].start_pts == clock.origin_pts, where
        assert tiles[-1].end_pts == clock.end_pts, where
        for position, tile in enumerate(tiles):
            assert tile.start_pts < tile.end_pts, where
            assert clock.is_frame_boundary(tile.start_pts), where
            assert clock.is_frame_boundary(tile.end_pts), where
            assert tile.segment_order == position, where
            if position:
                assert tiles[position - 1].end_pts == tile.start_pts, where


def revision_map(revision):
    return {s.segment_id: (s.start_pts, s.end_pts, s.selection, s.notes) for s in revision.segments}


# ─────────────────────────────────────────────────────────────
#  구성과 시간순
# ─────────────────────────────────────────────────────────────


def test_initial_revision_covers_whole_source_in_time_order():
    revision = base_revision()
    assert_covered(revision, (clock_a(), clock_b()))
    ordered = chronological(revision)
    assert [s.segment_id for s in ordered] == ["a1", "a2", "a3", "a4", "b1", "b2"]
    assert [s.segment_order for s in ordered if s.source_id == "source_a"] == [0, 1, 2, 3]
    assert all(s.selection == SELECTION_INCLUDED for s in ordered)
    a3 = next(s for s in ordered if s.segment_id == "a3")
    assert a3.speech_status == SPEECH_UNCONFIRMED_EMPTY
    assert a3.visual_tags == ("scenery",)


def test_protocol_accepts_w1_entry_shape_without_import():
    bare = W1ShapedEntry(
        segment_id="a1",
        asset_id="source_a",
        batch_dir="batch",
        source_order=0,
        segment_order=1,
        time_base_num=TB_NUM,
        time_base_den=TB_DEN,
        core_start_pts=0,
        core_end_pts=3_600_000,
        media_start_pts=0,
        media_end_pts=3_600_000,
        core_first_frame=0,
        core_after_last_frame=1200,
        source_frame_count=1200,
        recommendation="normal",
        basis="length",
    )
    assert isinstance(bare, SegmentPlanSource)
    revision = initial_revision("proj", "plan-1", (clock_a(),), [bare])
    assert_covered(revision, (clock_a(),))
    segment = revision.segments[0]
    assert segment.speech_status == SPEECH_UNCONFIRMED_EMPTY
    assert segment.visual_tags == ()


def test_clock_construction_refuses_off_grid_frame_duration():
    with pytest.raises(ReviewValidationError):
        make_source_clock("bad", "sha", TB_NUM, TB_DEN, 0, 3_600_000, Fraction(1, 7))
    with pytest.raises(ReviewValidationError):
        make_source_clock("bad", "sha", TB_NUM, TB_DEN, 0, 3_600_001, FRAME_30)
    with pytest.raises(ReviewValidationError):
        make_source_clock("bad", "sha", TB_NUM, TB_DEN, 3_600_000, 3_600_000, FRAME_30)


def test_foreign_source_and_timebase_are_refused_at_construction():
    entry = dataclasses.replace(plan_entries()[0], asset_id="source_missing")
    with pytest.raises(ReviewValidationError, match="시계가 리비전에 없다"):
        initial_revision("proj", "plan-1", (clock_a(),), [entry])
    entry = dataclasses.replace(plan_entries()[0], time_base_den=60000)
    with pytest.raises(ReviewValidationError, match="timebase"):
        initial_revision("proj", "plan-1", (clock_a(),), [entry])


# ─────────────────────────────────────────────────────────────
#  편집 — 새 리비전, 거부는 이전 리비전 그대로
# ─────────────────────────────────────────────────────────────


def test_split_creates_new_revision_and_keeps_coverage():
    revision = base_revision()
    before = revision_map(revision)
    split_at = 1_350_000
    edited = split_segment(revision, "a2", split_at)
    assert edited.sequence == revision.sequence + 1
    assert edited.parent_revision_id == revision.revision_id
    assert edited.edit is not None and edited.edit.kind == "split"
    assert revision_map(revision) == before
    assert_covered(edited, (clock_a(), clock_b()))
    children = [s for s in edited.segments if s.segment_id in ("a2#1", "a2#2")]
    assert [(c.start_pts, c.end_pts) for c in children] == [
        (900_000, split_at),
        (split_at, 1_980_000),
    ]
    others = {s.segment_id: s for s in edited.segments if s.segment_id not in ("a2#1", "a2#2")}
    assert "a2" not in others
    for unchanged in others.values():
        assert (unchanged.start_pts, unchanged.end_pts, unchanged.selection) == (
            before[unchanged.segment_id][0],
            before[unchanged.segment_id][1],
            before[unchanged.segment_id][2],
        )


def test_split_refusals_leave_old_revision_intact():
    revision = base_revision()
    before = revision_map(revision)
    for bad in (0, 900_000, 1_980_000, 900_001, 3_600_000):
        with pytest.raises(ReviewEditError):
            split_segment(revision, "a2", bad)
    with pytest.raises(ReviewEditError):
        split_segment(revision, "nope", 1_350_000)
    assert revision_map(revision) == before
    assert_covered(revision, (clock_a(), clock_b()))


def test_merge_adjacent_same_source_and_conservative_fields():
    revision = base_revision()
    noted = set_notes(revision, "a2", "질문과 답을 함께")
    excluded = set_selection(noted, "a3", SELECTION_EXCLUDED)
    merged = merge_segments(excluded, "a2", "a3")
    assert_covered(merged, (clock_a(), clock_b()))
    union = next(s for s in merged.segments if s.segment_id == "a2")
    assert (union.start_pts, union.end_pts) == (900_000, 2_790_000)
    assert union.speech_status == SPEECH_UNCONFIRMED_EMPTY
    assert union.recommendation == "review_required"
    assert union.selection == SELECTION_EXCLUDED
    assert union.visual_tags == ("talking", "arrival", "scenery")
    assert union.notes == "질문과 답을 함께"
    assert [s.segment_id for s in chronological(merged) if s.source_id == "source_a"] == [
        "a1",
        "a2",
        "a4",
    ]


def test_merge_argument_order_does_not_matter():
    revision = base_revision()
    forward = merge_segments(revision, "a1", "a2")
    backward = merge_segments(revision, "a2", "a1")
    assert revision_map(forward) == revision_map(backward)


def test_merge_refuses_cross_source_gap_and_nonadjacent():
    revision = base_revision()
    before = revision_map(revision)
    with pytest.raises(ReviewEditError, match="같은 원본"):
        merge_segments(revision, "a1", "b1")
    with pytest.raises(ReviewEditError, match="맞닿은"):
        merge_segments(revision, "a1", "a3")
    with pytest.raises(ReviewEditError, match="맞닿은"):
        merge_segments(revision, "a4", "a1")
    with pytest.raises(ReviewEditError):
        merge_segments(revision, "a1", "nope")
    assert revision_map(revision) == before


def test_boundary_adjust_moves_both_ends_atomically():
    revision = base_revision()
    boundary = 1_500_000
    edited = adjust_boundary(revision, "a2", "a3", boundary)
    assert_covered(edited, (clock_a(), clock_b()))
    ranges = {s.segment_id: (s.start_pts, s.end_pts) for s in edited.segments}
    assert ranges["a2"] == (900_000, boundary)
    assert ranges["a3"] == (boundary, 2_790_000)
    assert edited.edit is not None and edited.edit.kind == "boundary"
    assert reextract_segment_ids(revision, edited) == frozenset({"a2", "a3"})


def test_boundary_adjust_refusals():
    revision = base_revision()
    before = revision_map(revision)
    for bad in (900_000, 2_790_000, 1_980_000, 1_980_001, 3_600_000):
        with pytest.raises(ReviewEditError):
            adjust_boundary(revision, "a2", "a3", bad)
    with pytest.raises(ReviewEditError, match="같은 원본"):
        adjust_boundary(revision, "a1", "b2", 300_000)
    with pytest.raises(ReviewEditError, match="맞닿은"):
        adjust_boundary(revision, "a1", "a3", 1_500_000)
    assert revision_map(revision) == before


def test_selection_retains_every_segment():
    revision = base_revision()
    excluded = set_selection(revision, "a3", SELECTION_EXCLUDED)
    assert len(excluded.segments) == len(revision.segments)
    assert {s.segment_id for s in revision.segments} == {s.segment_id for s in excluded.segments}
    assert (
        next(s for s in excluded.segments if s.segment_id == "a3").selection == SELECTION_EXCLUDED
    )
    assert reextract_segment_ids(revision, excluded) == frozenset()
    overridden = set_override(excluded, "a3", "recommended")
    noted = set_notes(overridden, "a3", "다음에 다시 보기")
    assert reextract_segment_ids(excluded, overridden) == frozenset()
    assert reextract_segment_ids(overridden, noted) == frozenset()
    assert next(s for s in noted.segments if s.segment_id == "a3").notes == "다음에 다시 보기"


def test_reextract_targets_exactly_range_changed_segments():
    revision = base_revision()
    split = split_segment(revision, "a2", 1_350_000)
    assert reextract_segment_ids(revision, split) == frozenset({"a2#1", "a2#2"})
    merged = merge_segments(revision, "a2", "a3")
    assert reextract_segment_ids(revision, merged) == frozenset({"a2"})
    with pytest.raises(ReviewValidationError, match="신원"):
        other_clock = make_source_clock(
            "source_a", "sha-other", TB_NUM, TB_DEN, 0, 3_600_000, FRAME_30
        )
        foreign = initial_revision("proj", "plan-1", (other_clock, clock_b()), plan_entries())
        reextract_segment_ids(revision, foreign)


def test_old_revisions_stay_untouched_through_edit_chain():
    revision = base_revision()
    snapshot = dataclasses.replace(revision)
    chain = set_notes(revision, "a1", "첫 메모")
    chain = split_segment(chain, "a2", 1_350_000)
    chain = merge_segments(chain, "a4", "a3")
    chain = adjust_boundary(chain, "a1", "a2#1", 600_000)
    chain = set_selection(chain, "b1", SELECTION_EXCLUDED)
    assert revision == snapshot
    assert revision.sequence == 0
    assert revision.edit is None
    with pytest.raises(ReviewEditError):
        split_segment(chain, "a2", 1_350_000)
    assert chain.edit is not None and chain.edit.kind == "selection"


def test_stale_edit_cannot_patch_newer_revision():
    revision = base_revision()
    newer = set_selection(revision, "a1", SELECTION_EXCLUDED)
    stale = split_edit(revision.revision_id, "a3", 2_100_000)
    with pytest.raises(StaleRevisionError):
        apply_edit(newer, stale)
    assert revision_map(newer) == revision_map(newer)
    unknown = ReviewEdit("teleport", newer.revision_id, ("a3",))
    with pytest.raises(ReviewEditError, match="알 수 없는 편집"):
        apply_edit(newer, unknown)
    late = merge_edit(revision.revision_id, "a1", "a2")
    with pytest.raises(StaleRevisionError):
        apply_edit(newer, late)


# ─────────────────────────────────────────────────────────────
#  질의 — 필터 뒤에도 시간순
# ─────────────────────────────────────────────────────────────


def test_filter_and_search_keep_chronological_order():
    revision = base_revision()
    revision = set_notes(revision, "a1", "야경 도착")
    revision = set_notes(revision, "a4", "NIGHT 야경")
    revision = set_notes(revision, "b1", "창밖 풍경")
    for result in (
        filter_segments(revision),
        filter_segments(revision, query="야경"),
        filter_segments(revision, tags=("talking",)),
        filter_segments(revision, selection=SELECTION_INCLUDED),
    ):
        keys = [(s.source_order, s.start_pts, s.segment_order) for s in result]
        assert keys == sorted(keys)
    assert [s.segment_id for s in filter_segments(revision, query="야경")] == ["a1", "a4"]
    assert [s.segment_id for s in filter_segments(revision, tags=("scenery",))] == ["a3"]
    excluded = set_selection(revision, "a4", SELECTION_EXCLUDED)
    assert [s.segment_id for s in filter_segments(excluded, selection=SELECTION_EXCLUDED)] == ["a4"]
    assert len(filter_segments(excluded, query="야경")) == 2


def test_previous_and_next_follow_chronology():
    revision = base_revision()
    assert previous_segment(revision, "a1") is None
    assert next_segment(revision, "a1").segment_id == "a2"
    assert previous_segment(revision, "b1").segment_id == "a4"
    assert next_segment(revision, "b2") is None
    with pytest.raises(ReviewEditError):
        previous_segment(revision, "nope")


# ─────────────────────────────────────────────────────────────
#  직렬화 — 왕복과 거부
# ─────────────────────────────────────────────────────────────


def edited_revision():
    revision = base_revision()
    revision = set_notes(revision, "a2", "질문과 답을 함께")
    revision = split_segment(revision, "a2", 1_350_000)
    revision = merge_segments(revision, "a4", "a3")
    revision = adjust_boundary(revision, "a1", "a2#1", 600_000)
    revision = set_selection(revision, "b1", SELECTION_EXCLUDED)
    revision = set_override(revision, "b2", "recommended")
    return revision


def test_serialization_roundtrip_is_exact():
    revision = edited_revision()
    document = serialize_revision(revision)
    assert document.endswith("\n")
    assert serialize_revision(revision) == document
    assert parse_revision(document) == revision
    assert content_fingerprint(revision) == content_fingerprint(parse_revision(document))
    assert "display_mode" not in document
    assert "ReviewViewState" not in document


def test_fingerprint_tracks_content_not_sequence_noise():
    revision = base_revision()
    assert content_fingerprint(revision) != content_fingerprint(edited_revision())
    assert content_fingerprint(set_notes(revision, "a1", "메모")) != content_fingerprint(revision)


def mutate(document: str, **changes) -> str:
    parsed = json.loads(document)
    for key, value in changes.items():
        if value is _DROP:
            parsed.pop(key, None)
        else:
            parsed[key] = value
    return json.dumps(parsed, ensure_ascii=False)


_DROP = object()


def test_parse_refuses_unknown_schema_and_structures():
    document = serialize_revision(base_revision())
    with pytest.raises(ReviewParseError):
        parse_revision("이건 json이 아니다")
    with pytest.raises(ReviewParseError):
        parse_revision("[]")
    with pytest.raises(ReviewParseError, match="스키마"):
        parse_revision(mutate(document, schema_version="raw-workspace/v999"))
    with pytest.raises(ReviewParseError, match="모르는 키"):
        parse_revision(mutate(document, surprise=1))
    with pytest.raises(ReviewParseError, match="없는 키"):
        parse_revision(mutate(document, project_id=_DROP))


def test_parse_refuses_wrong_types_and_ranges():
    document = serialize_revision(edited_revision())
    segments = json.loads(document)["segments"]

    def with_segments(replacement) -> str:
        return mutate(document, segments=replacement)

    broken = [dict(segment) for segment in segments]
    broken[0]["start_pts"] = str(broken[0]["start_pts"])
    with pytest.raises(ReviewParseError, match="정수"):
        parse_revision(with_segments(broken))
    with pytest.raises(ReviewParseError, match="정수"):
        parse_revision(mutate(document, sequence=True))
    with pytest.raises(ReviewParseError, match="문자열"):
        parse_revision(mutate(document, project_id=7))
    inverted = [dict(segment) for segment in segments]
    for segment in inverted:
        if segment["segment_id"] == "a1":
            segment["start_pts"], segment["end_pts"] = segment["end_pts"], segment["start_pts"]
    with pytest.raises(ReviewParseError):
        parse_revision(with_segments(inverted))
    off_grid = [dict(segment) for segment in segments]
    for segment in off_grid:
        if segment["segment_id"] == "a1":
            segment["end_pts"] += 1
        if segment["segment_id"] == "a2#1":
            segment["start_pts"] += 1
    with pytest.raises(ReviewParseError, match="격자"):
        parse_revision(with_segments(off_grid))
    holed = [dict(segment) for segment in segments if segment["source_id"] != "source_b"]
    with pytest.raises(ReviewParseError):
        parse_revision(with_segments(holed))
    foreign = [dict(segment) for segment in segments]
    foreign[0]["source_id"] = "source_missing"
    with pytest.raises(ReviewParseError, match="시계가 리비전에 없다"):
        parse_revision(with_segments(foreign))
    bad_enum = [dict(segment) for segment in segments]
    bad_enum[0]["selection"] = "deleted"
    with pytest.raises(ReviewParseError):
        parse_revision(with_segments(bad_enum))
    duplicated = [dict(segment) for segment in segments]
    duplicated[1]["segment_id"] = duplicated[0]["segment_id"]
    with pytest.raises(ReviewParseError, match="중복"):
        parse_revision(with_segments(duplicated))


# ─────────────────────────────────────────────────────────────
#  보기 상태 — 표시 모드는 내용이 아니다(§10.3)
# ─────────────────────────────────────────────────────────────


def test_display_mode_toggle_is_view_state_only():
    revision = edited_revision()
    fingerprint = content_fingerprint(revision)
    view = ReviewViewState(selected_segment_id="a2#1")
    toggled = view.with_display_mode(DISPLAY_VIDEO_WITH_TRANSCRIPT)
    assert toggled.display_mode == DISPLAY_VIDEO_WITH_TRANSCRIPT
    assert view.display_mode == DISPLAY_VIDEO_ONLY
    assert toggled.selected_segment_id == view.selected_segment_id
    back = toggled.with_display_mode(DISPLAY_VIDEO_ONLY)
    assert back == view
    with pytest.raises(ReviewValidationError):
        view.with_display_mode("video_and_magic")
    # 토글 뒤에도 같은 리비전에서 나오는 지문·바이트는 한 글자도 안 바뀐다.
    assert content_fingerprint(revision) == fingerprint
    assert content_fingerprint(parse_revision(serialize_revision(revision))) == fingerprint


def test_recommendation_vocabulary_is_the_plan_grades():
    assert RECOMMENDATIONS == ("recommended", "normal", "review_required")
    assert REVIEW_SCHEMA_VERSION == "raw-workspace/v1"
