"""시작·끝으로 범위 표시(W-43, D100 §4) — 추천·보통·비추천을 한 리비전으로.

순수 쪽은 `segment_review.range_edit`·`apply_edit`(종류 `range`)를 합성 리비전(원본 A 40초·4조각,
원본 B 20초·2조각, 30fps)에서 본다. 작업공간 쪽은 합성 미디어(15초)에 재생 분석을 돌린 검토 머리에
서비스 `visual_mark_range`를 건다(유료 호출 0회). 렌더 계획(D100 §5a)이 표시를 그대로 읽는지도 본다.
"""

from __future__ import annotations

import dataclasses
import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_segment_review import base_revision
from tests.raw.test_tier_criteria import LONG_MINIMUM
from tests.raw.test_tier_review_gate import UTTERANCES
from tests.raw.test_tier_timing_service import _project
from vlogsrt.project.raw import render_plan as rp
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.config import RenderSettings
from vlogsrt.project.raw.review_notes import AUTO_NOTE_PREFIX, MANUAL_SELECTION_PREFIX
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade, human_decided
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    RANGE_CLEAR,
    RANGE_MARKS,
    RANGE_NORMAL,
    RANGE_NOT_RECOMMENDED,
    RANGE_RECOMMENDED,
    RANGE_REPLACED_PREFIX,
    SELECTION_EXCLUDED,
    SELECTION_INCLUDED,
    ReviewEditError,
    ReviewRevision,
    ReviewSegment,
    StaleRevisionError,
    apply_edit,
    chronological,
    content_fingerprint,
    is_unreviewed_auto_exclusion,
    parse_revision,
    range_edit,
    serialize_revision,
    set_override,
    split_segment,
    unreviewed_auto_exclusions,
)
from vlogsrt.project.raw.visual_review import VisualReviewError

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

FRAME = 3000
"""합성 리비전의 프레임 틱(1/90000 · 30fps)."""


def _mark(
    revision: ReviewRevision, start: int, end: int, mark: str, source: str = "source_a"
) -> ReviewRevision:
    return apply_edit(revision, range_edit(revision, source, start, end, mark))


def _spans(revision: ReviewRevision, source: str = "source_a") -> list[tuple[str, int, int]]:
    return [
        (s.segment_id, s.start_pts, s.end_pts)
        for s in chronological(revision)
        if s.source_id == source
    ]


def _at(revision: ReviewRevision, start: int, source: str = "source_a") -> ReviewSegment:
    return next(s for s in revision.segments if s.source_id == source and s.start_pts == start)


# ── 순수: 기하 ──────────────────────────────────────────────────────────────


def test_range_inside_one_segment_yields_three_pieces() -> None:
    base = base_revision()
    marked = _mark(base, 1_200_000, 1_500_000, RANGE_RECOMMENDED)
    assert _spans(marked) == [
        ("a1", 0, 900_000),
        ("a2#1", 900_000, 1_200_000),
        ("a2#2#1", 1_200_000, 1_500_000),
        ("a2#2#2", 1_500_000, 1_980_000),
        ("a3", 1_980_000, 2_790_000),
        ("a4", 2_790_000, 3_600_000),
    ]
    middle = _at(marked, 1_200_000)
    assert (middle.override_recommendation, middle.selection) == ("recommended", "included")
    outside = [s for s in marked.segments if s.start_pts != 1_200_000]
    assert all(s.override_recommendation is None for s in outside)
    assert _spans(marked, "source_b") == _spans(base, "source_b")


def test_range_across_three_segments_merges_the_inside_and_concatenates_notes() -> None:
    base = base_revision()
    noted = dataclasses.replace(
        base,
        segments=tuple(
            dataclasses.replace(
                s, notes={"a1": "앞", "a2": "가운데", "a3": "뒤"}.get(s.segment_id, "")
            )
            for s in base.segments
        ),
    )
    marked = _mark(noted, 600_000, 2_100_000, RANGE_NORMAL)
    assert _spans(marked) == [
        ("a1#1", 0, 600_000),
        ("a1#2", 600_000, 2_100_000),
        ("a3#2", 2_100_000, 2_790_000),
        ("a4", 2_790_000, 3_600_000),
    ]
    merged = _at(marked, 600_000)
    assert merged.notes == "앞\n가운데\n뒤"
    assert (merged.override_recommendation, merged.selection) == ("normal", "included")
    assert _at(marked, 0).notes == "앞" and _at(marked, 2_100_000).notes == "뒤"


def test_range_touching_existing_edges_needs_no_split() -> None:
    marked = _mark(base_revision(), 900_000, 2_790_000, RANGE_RECOMMENDED)
    assert _spans(marked) == [
        ("a1", 0, 900_000),
        ("a2", 900_000, 2_790_000),
        ("a4", 2_790_000, 3_600_000),
    ]
    assert "#" not in "".join(s.segment_id for s in marked.segments)


def test_range_over_one_whole_segment_keeps_its_id() -> None:
    marked = _mark(base_revision(), 900_000, 1_980_000, RANGE_NOT_RECOMMENDED)
    assert [span[0] for span in _spans(marked)] == ["a1", "a2", "a3", "a4"]


def test_range_covering_a_whole_source() -> None:
    base = base_revision()
    marked = _mark(base, 0, 3_600_000, RANGE_NOT_RECOMMENDED)
    assert _spans(marked) == [("a1", 0, 3_600_000)]
    assert _spans(marked, "source_b") == _spans(base, "source_b")


@pytest.mark.parametrize(
    ("start", "end", "fragment"),
    [
        (1_200_001, 1_500_000, "프레임 격자"),
        (1_200_000, 1_500_001, "프레임 격자"),
        (1_500_000, 1_200_000, "시작"),
        (1_200_000, 1_200_000, "시작"),
        (0, 3_600_000 + FRAME, "밖"),
    ],
)
def test_misaligned_inverted_and_outside_ranges_are_rejected(
    start: int, end: int, fragment: str
) -> None:
    base = base_revision()
    with pytest.raises(ReviewEditError, match=fragment):
        _mark(base, start, end, RANGE_RECOMMENDED)


def test_unknown_mark_and_unknown_source_are_rejected() -> None:
    base = base_revision()
    with pytest.raises(ReviewEditError, match="표시"):
        _mark(base, 0, 900_000, "great")
    with pytest.raises(ReviewEditError, match="source_z"):
        _mark(base, 0, 900_000, RANGE_RECOMMENDED, source="source_z")


def test_range_edit_must_name_exactly_the_overlapped_segments() -> None:
    base = base_revision()
    edit = range_edit(base, "source_a", 600_000, 2_100_000, RANGE_RECOMMENDED)
    assert edit.segment_ids == ("a1", "a2", "a3")
    forged = dataclasses.replace(edit, segment_ids=("a1", "a2"))
    with pytest.raises(ReviewEditError, match="겹치는 조각"):
        apply_edit(base, forged)


# ── 순수: 한 리비전 · 기록 ──────────────────────────────────────────────────


def test_a_mark_is_exactly_one_revision_and_round_trips() -> None:
    base = base_revision()
    marked = _mark(base, 600_000, 2_100_000, RANGE_NOT_RECOMMENDED)
    assert marked.sequence == base.sequence + 1
    assert marked.parent_revision_id == base.revision_id
    assert marked.edit is not None and marked.edit.kind == "range"
    assert (marked.edit.range_start_pts, marked.edit.range_end_pts) == (600_000, 2_100_000)
    assert marked.edit.mark == RANGE_NOT_RECOMMENDED
    again = parse_revision(serialize_revision(marked))
    assert again == marked
    assert content_fingerprint(again) == content_fingerprint(marked)


def test_other_edit_documents_keep_their_bytes() -> None:
    """옛 편집 문서에 범위 칸이 끼면 저장된 모든 머리의 지문이 깨진다 — 범위 편집에만 싣는다."""
    split = split_segment(base_revision(), "a2", 1_200_000)
    text = serialize_revision(split)
    assert '"mark"' not in text and "range_start_pts" not in text
    assert parse_revision(text) == split


def test_stale_range_edit_is_rejected() -> None:
    base = base_revision()
    edit = range_edit(base, "source_a", 0, 900_000, RANGE_RECOMMENDED)
    moved = set_override(base, "b1", "normal")
    with pytest.raises(StaleRevisionError):
        apply_edit(moved, edit)


# ── 순수: 표시의 뜻 ─────────────────────────────────────────────────────────


def _auto_excluded(revision: ReviewRevision, segment_id: str) -> ReviewRevision:
    return dataclasses.replace(
        revision,
        segments=tuple(
            dataclasses.replace(
                s,
                selection=SELECTION_EXCLUDED,
                visual_tags=(*s.visual_tags, AUTO_EXCLUDED_UNREVIEWED_TAG),
            )
            if s.segment_id == segment_id
            else s
            for s in revision.segments
        ),
    )


def test_not_recommended_is_a_confirmed_human_exclusion() -> None:
    base = _auto_excluded(base_revision(), "a3")
    marked = _mark(base, 1_980_000, 2_790_000, RANGE_NOT_RECOMMENDED)
    segment = _at(marked, 1_980_000)
    assert segment.selection == SELECTION_EXCLUDED
    assert segment.override_recommendation == "review_required"
    assert AUTO_EXCLUDED_UNREVIEWED_TAG not in segment.visual_tags
    assert not is_unreviewed_auto_exclusion(segment)
    assert unreviewed_auto_exclusions(marked) == ()
    assert human_decided(segment)


def test_recommended_on_an_unreviewed_auto_exclusion_clears_the_tag() -> None:
    base = _auto_excluded(base_revision(), "a3")
    marked = _mark(base, 2_100_000, 2_400_000, RANGE_RECOMMENDED)
    segment = _at(marked, 2_100_000)
    assert segment.selection == SELECTION_INCLUDED
    assert segment.override_recommendation == "recommended"
    assert AUTO_EXCLUDED_UNREVIEWED_TAG not in segment.visual_tags
    # 표시 밖의 남은 조각은 사람이 보지 않았다 — 미검토 제안 그대로다.
    assert {s.segment_id for s in unreviewed_auto_exclusions(marked)} == {"a3#1", "a3#2#2"}
    assert human_decided(segment)


def test_every_mark_is_a_human_decision() -> None:
    for mark in (RANGE_RECOMMENDED, RANGE_NORMAL, RANGE_NOT_RECOMMENDED):
        assert human_decided(_at(_mark(base_revision(), 0, 900_000, mark), 0))
    assert set(RANGE_MARKS) == {
        RANGE_RECOMMENDED,
        RANGE_NORMAL,
        RANGE_NOT_RECOMMENDED,
        RANGE_CLEAR,
    }


def test_replaced_human_decisions_are_recorded_in_the_note() -> None:
    base = set_override(base_revision(), "a2", "normal")
    marked = _mark(base, 600_000, 2_100_000, RANGE_RECOMMENDED)
    merged = _at(marked, 600_000)
    lines = merged.notes.split("\n")
    assert lines[-1].startswith(RANGE_REPLACED_PREFIX)
    assert "a2" in lines[-1] and "normal" in lines[-1]
    # 같은 표시로 다시 덮는 것은 바뀐 결정이 아니다 — 기록하지 않는다.
    again = _mark(marked, 600_000, 2_100_000, RANGE_RECOMMENDED)
    assert _at(again, 600_000).notes == merged.notes


def test_clear_returns_the_range_to_automatic() -> None:
    base = set_override(base_revision(), "a2", "normal")
    marked = _mark(base, 600_000, 2_100_000, RANGE_NOT_RECOMMENDED)
    cleared = _mark(marked, 600_000, 2_100_000, RANGE_CLEAR)
    segment = _at(cleared, 600_000)
    assert (segment.override_recommendation, segment.selection) == (None, SELECTION_INCLUDED)
    assert RANGE_REPLACED_PREFIX not in segment.notes
    assert not human_decided(segment)
    assert cleared.sequence == marked.sequence + 1


# ── 렌더 계획(D100 §5a)이 표시를 읽는다 ────────────────────────────────────


def test_render_plan_keeps_a_recommended_yellow_range_and_drops_a_rejected_green_one() -> None:
    base = base_revision()
    marked = _mark(base, 1_080_000, 1_350_000, RANGE_RECOMMENDED)  # 12–15초, 노랑 위
    marked = _mark(marked, 2_250_000, 2_520_000, RANGE_NOT_RECOMMENDED)  # 25–28초, 녹색 위
    clock = marked.clock_for("source_a")
    assert clock is not None
    second = Fraction(1)
    source = rp.RenderSource(
        source_id="source_a",
        name="a.mov",
        clock=rp.frame_clock(clock),
        width=1920,
        height=1080,
        grid=rp.GRID_UNIFORM,
        tiers=(
            rp.TierPiece(0 * second, 10 * second, tiering.TIER_GREEN),
            rp.TierPiece(10 * second, 20 * second, tiering.TIER_YELLOW),
            rp.TierPiece(20 * second, 40 * second, tiering.TIER_GREEN),
        ),
        segments=tuple(
            rp.segment_mark(s, clock, decided=human_decided(s))
            for s in chronological(marked)
            if s.source_id == "source_a"
        ),
    )
    plan = rp.build_plan([source], RenderSettings(min_part_seconds=second))
    parts = [(p.start, p.end, p.reason) for p in plan.parts]
    assert (12 * second, 15 * second, rp.PART_HUMAN_RECOMMENDED) in parts
    dropped = [(d.start, d.end, d.reason) for d in plan.dropped]
    assert (25 * second, 28 * second, rp.DROP_HUMAN_EXCLUDED) in dropped
    assert plan.unreviewed_red_dropped == 0


# ── 작업공간 · 서비스 ───────────────────────────────────────────────────────


def _mark_seconds(service, root: Path, start: int, end: int, mark: str):  # type: ignore[no-untyped-def]
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    return service.visual_mark_range(
        root,
        expected_revision_id=head.revision_id,
        source_id=head.clocks[0].source_id,
        start_seconds=Fraction(start),
        end_seconds=Fraction(end),
        mark=mark,
    )


@needs_ffmpeg
def test_service_mark_is_one_revision_and_undo_restores_it(tmp_path: Path) -> None:
    service, root, asset, _workspace, _source = _project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    before = facade.load_head().revision
    result = _mark_seconds(service, root, 3, 6, RANGE_NOT_RECOMMENDED)
    head = facade.load_head().revision
    assert head.revision_id == result.head_revision_id == result.revision.revision_id
    assert head.sequence == before.sequence + 1
    assert head.edit is not None and head.edit.kind == "range"
    assert result.source_id == asset.asset_id
    assert [s.segment_id for s in result.segments] == [
        s.segment_id for s in chronological(head) if s.source_id == asset.asset_id
    ]
    assert len(result.segments) == 3
    marked = next(s for s in result.segments if s.segment_id == result.marked_segment_id)
    clock = head.clocks[0]
    assert (marked.start_pts - clock.origin_pts) * clock.time_base == 3
    assert (marked.end_pts - clock.origin_pts) * clock.time_base == 6
    assert marked.selection == SELECTION_EXCLUDED and human_decided(marked)

    undone = facade.undo()
    assert undone.segments == before.segments
    assert undone.sequence == head.sequence + 1


@needs_ffmpeg
def test_service_rejects_stale_misaligned_and_outside_ranges(tmp_path: Path) -> None:
    service, root, asset, _workspace, _source = _project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    clock = head.clocks[0]
    _mark_seconds(service, root, 3, 6, RANGE_RECOMMENDED)
    with pytest.raises(StaleRevisionError):
        service.visual_mark_range(
            root,
            expected_revision_id=head.revision_id,
            source_id=asset.asset_id,
            start_seconds=Fraction(1),
            end_seconds=Fraction(2),
            mark=RANGE_NORMAL,
        )
    fresh = facade.load_head().revision.revision_id
    with pytest.raises(VisualReviewError, match="프레임 격자"):
        service.visual_mark_range(
            root,
            expected_revision_id=fresh,
            source_id=asset.asset_id,
            start_pts=clock.origin_pts + 1,
            end_pts=clock.origin_pts + 10 * clock.frame_ticks,
            mark=RANGE_NORMAL,
        )
    with pytest.raises(VisualReviewError, match="밖"):
        service.visual_mark_range(
            root,
            expected_revision_id=fresh,
            source_id=asset.asset_id,
            start_pts=clock.origin_pts,
            end_pts=clock.end_pts + clock.frame_ticks,
            mark=RANGE_NORMAL,
        )
    with pytest.raises(VisualReviewError, match="하나"):
        service.visual_mark_range(
            root,
            expected_revision_id=fresh,
            source_id=asset.asset_id,
            start_pts=clock.origin_pts,
            start_seconds=Fraction(0),
            end_seconds=Fraction(2),
            mark=RANGE_NORMAL,
        )
    assert facade.load_head().revision.revision_id == fresh  # 거부는 쓰지 않는다


@needs_ffmpeg
def test_service_seconds_snap_through_the_source_clock(tmp_path: Path) -> None:
    service, root, *_ = _project(tmp_path)
    head = ReviewWorkspaceFacade(root).load_head().revision
    clock = head.clocks[0]
    frame = clock.frame_ticks * clock.time_base
    result = service.visual_mark_range(
        root,
        expected_revision_id=head.revision_id,
        source_id=clock.source_id,
        start_seconds=3 + frame * Fraction(2, 5),  # 앞 프레임에 가깝다
        end_seconds=6 + frame * Fraction(3, 5),  # 뒤 프레임에 가깝다
        mark=RANGE_RECOMMENDED,
    )
    assert (result.start_pts - clock.origin_pts) * clock.time_base == 3
    assert (result.end_pts - clock.origin_pts) * clock.time_base == 6 + frame


@needs_ffmpeg
def test_regrade_keeps_marks(tmp_path: Path) -> None:
    service, root, *_ = _project(tmp_path)
    _mark_seconds(service, root, 2, 5, RANGE_RECOMMENDED)
    _mark_seconds(service, root, 8, 11, RANGE_NOT_RECOMMENDED)
    facade = ReviewWorkspaceFacade(root)
    before = facade.load_head().revision
    service.set_tier_settings(root, LONG_MINIMUM)  # 녹색이 보통까지 내려간다 — 다시 매김
    after = facade.load_head().revision
    assert after.revision_id != before.revision_id
    assert after.edit is not None and (after.edit.notes or "").startswith("등급 재계산 ")
    marks = [
        (s.start_pts, s.end_pts, s.override_recommendation, s.selection)
        for s in chronological(after)
    ]
    assert marks == [
        (s.start_pts, s.end_pts, s.override_recommendation, s.selection)
        for s in chronological(before)
    ]
    recommended = next(s for s in after.segments if s.override_recommendation == "recommended")
    assert human_decided(recommended)
    automatic = [s for s in after.segments if s.override_recommendation is None]
    assert {s.recommendation for s in automatic} == {"normal"}


@needs_ffmpeg
def test_recommended_on_a_red_proposal_confirms_it_in_the_workspace(tmp_path: Path) -> None:
    service, root, *_ = _project(tmp_path, suitability="usable", utterances=UTTERANCES)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    (pending,) = unreviewed_auto_exclusions(head)
    assert pending.notes.startswith(AUTO_NOTE_PREFIX)
    result = service.visual_mark_range(
        root,
        expected_revision_id=head.revision_id,
        source_id=pending.source_id,
        start_pts=pending.start_pts,
        end_pts=pending.end_pts,
        mark=RANGE_RECOMMENDED,
    )
    marked = next(s for s in result.segments if s.segment_id == result.marked_segment_id)
    assert marked.segment_id == pending.segment_id
    assert marked.selection == SELECTION_INCLUDED
    assert AUTO_EXCLUDED_UNREVIEWED_TAG not in marked.visual_tags
    assert marked.notes.startswith(MANUAL_SELECTION_PREFIX + AUTO_NOTE_PREFIX)
    assert unreviewed_auto_exclusions(facade.load_head().revision) == ()
    # 「자동 제외 전체 복원」은 사람이 표시한 조각을 건드리지 않는다.
    restored = facade.restore_automatic()
    kept = next(s for s in restored.segments if s.segment_id == pending.segment_id)
    assert kept.override_recommendation == "recommended"

    cleared = service.visual_mark_range(
        root,
        expected_revision_id=restored.revision_id,
        source_id=pending.source_id,
        start_pts=pending.start_pts,
        end_pts=pending.end_pts,
        mark=RANGE_CLEAR,
    )
    back = next(s for s in cleared.segments if s.segment_id == cleared.marked_segment_id)
    assert (back.override_recommendation, back.selection) == (None, SELECTION_INCLUDED)
    assert AUTO_NOTE_PREFIX not in back.notes
    assert not human_decided(back)
