"""A3 결정적 분할 — 순수 계산의 격자·타일·지문 검사(계획 §6.1·§13.3).

ffmpeg도 DB도 없다: 시계 값만 넣어 경계 선택·전체 덮음·이웃 정확히 맞닿음·
사람 구간 검증·지문 안정성을 잰다. 의미 분석 경계는 이 계층에 없다(A4).
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from vlogsrt.project.raw import segmentation
from vlogsrt.project.raw.models import PlannedInterval, SegmentationError

TICKS_PER_FRAME = 3600
"""합성 시계의 프레임당 틱 — 프레임 길이·time_base가 정수 틱으로 떨어지게 한다."""


def _plan(frame_count, intervals=None, order="order_x", fps=Fraction(25), origin_pts=0):
    """측정된 시계 값만 넣어 계획을 세운다 — ffmpeg·DB는 이 묶음에 없다."""
    frame_duration = Fraction(1) / fps
    time_base = frame_duration / TICKS_PER_FRAME
    entries, coverage = segmentation.plan_segments(
        asset_id="source_a",
        batch_dir="import_000001",
        source_order=1,
        time_base=time_base,
        presentation_origin_pts=origin_pts,
        frame_count=frame_count,
        frame_duration=frame_duration,
        exact_end_pts=origin_pts + frame_count * TICKS_PER_FRAME,
        intervals=intervals,
        order_revision_id=order,
    )
    return entries, coverage


# ── 기본 타일링 — 전체 덮음·이웃 정확히 맞닿음 ────────────────────────────────


def test_default_tiling_covers_whole_grid_with_exact_adjacency() -> None:
    """첫 시작=원점, 마지막 끝=정규화 종료, 이웃 경계 틱은 정확히 같다(§6.1)."""
    entries, coverage = _plan(150, intervals=None, order="order_a")
    assert [entry.segment_order for entry in entries] == list(range(1, len(entries) + 1))
    assert entries[0].core_start_pts == 0
    for left, right in zip(entries[:-1], entries[1:], strict=True):
        assert left.core_end_pts == right.core_start_pts
    assert entries[-1].core_end_pts == coverage.exact_end_pts
    assert coverage.segment_count == len(entries)
    assert coverage.source_frame_count == 150


def test_single_tile_when_shorter_than_tile() -> None:
    """타일보다 짧은 원본은 조각 하나 — 억지로 자르지 않는다(§7.6)."""
    entries, _ = _plan(40)
    assert len(entries) == 1
    assert entries[0].core_first_frame == 0
    assert entries[0].core_after_last_frame == 40


def test_numbering_follows_start_time_not_anything_else() -> None:
    """조각 번호 오름차순 = core.start 오름차순 — 다른 근거는 없다(§6.1)."""
    entries, _ = _plan(150)
    starts = [entry.core_start_pts for entry in entries]
    assert starts == sorted(starts)
    orders = [entry.segment_order for entry in entries]
    assert orders == sorted(orders)


def test_media_range_equals_core_for_zero_handles() -> None:
    """기본 핸들 0 — 계획의 media는 core와 같다(§6.5). 핸들은 추출이 벌인다."""
    entries, _ = _plan(150)
    for entry in entries:
        assert entry.media_start_pts == entry.core_start_pts
        assert entry.media_end_pts == entry.core_end_pts


# ── 사람이 준 구간 — 전체 타일만 받는다 ────────────────────────────────────────


def test_user_intervals_tile_the_grid() -> None:
    """명시적 구간도 전체를 덮는 타일이어야 한다 — 경계는 격자로 한 번에(§6.1)."""
    entries, coverage = _plan(
        150,
        intervals=[
            PlannedInterval(start=Fraction(0), end=Fraction(2)),
            PlannedInterval(start=Fraction(2), end=Fraction("4.5")),
            PlannedInterval(start=Fraction("4.5"), end=Fraction(6)),
        ],
    )
    assert len(entries) == 3
    assert entries[0].core_after_last_frame == 50
    assert entries[1].core_first_frame == 50
    assert entries[1].core_after_last_frame == 112  # 4.5초 → 프레임 112(바닥)
    assert entries[2].core_first_frame == 112
    assert entries[2].core_after_last_frame == 150
    assert entries[0].basis == "user_planned"


def test_interval_not_starting_at_zero_is_rejected() -> None:
    with pytest.raises(SegmentationError, match="0초\\(재생 원점\\)에서 시작하지 않는다"):
        _plan(150, intervals=[PlannedInterval(Fraction(1), Fraction(6))])


def test_interval_gap_is_rejected() -> None:
    with pytest.raises(SegmentationError, match="경계를 공유하지 않는다"):
        _plan(
            150,
            intervals=[
                PlannedInterval(Fraction(0), Fraction(2)),
                PlannedInterval(Fraction(3), Fraction(6)),
            ],
        )


def test_interval_overlap_is_rejected() -> None:
    with pytest.raises(SegmentationError, match="경계를 공유하지 않는다"):
        _plan(
            150,
            intervals=[
                PlannedInterval(Fraction(0), Fraction(4)),
                PlannedInterval(Fraction(2), Fraction(6)),
            ],
        )


def test_interval_short_of_end_is_rejected() -> None:
    with pytest.raises(SegmentationError, match="덮지 못한다"):
        _plan(150, intervals=[PlannedInterval(Fraction(0), Fraction(4))])


def test_interval_overrun_is_clamped_to_normalized_end() -> None:
    """마지막 끝이 원본보다 길면 원본 끝에서 잘린다 — 못 미치면 실패였다."""
    entries, coverage = _plan(
        150,
        intervals=[
            PlannedInterval(Fraction(0), Fraction(2)),
            PlannedInterval(Fraction(2), Fraction(99)),
        ],
    )
    assert len(entries) == 2
    assert entries[-1].core_after_last_frame == 150
    assert entries[-1].core_end_pts == coverage.exact_end_pts


def test_interval_boundaries_collapsing_to_one_frame_is_rejected() -> None:
    """경계 둘이 같은 프레임에 떨어지면 빈 조각이다 — 계획이 되지 않는다."""
    with pytest.raises(SegmentationError, match="빈 조각"):
        _plan(
            150,
            intervals=[
                PlannedInterval(Fraction(0), Fraction("0.02")),
                PlannedInterval(Fraction("0.02"), Fraction(6)),
            ],
        )


def test_zero_or_negative_interval_is_rejected() -> None:
    with pytest.raises(SegmentationError, match="비거나 뒤집혔다"):
        _plan(
            150,
            intervals=[
                PlannedInterval(Fraction(0), Fraction(0)),
                PlannedInterval(Fraction(0), Fraction(6)),
            ],
        )


# ── NTSC — µs로 안 떨어지는 프레임 길이 ──────────────────────────────────────


def test_ntsc_fractional_frame_duration_stays_exact() -> None:
    """30000/1001fps — 틱 좌표가 정수로 떨어져 이웃이 정확히 맞닿는다(§13.3)."""
    entries, coverage = _plan(105, fps=Fraction(30000, 1001))
    for left, right in zip(entries[:-1], entries[1:], strict=True):
        assert left.core_end_pts == right.core_start_pts
    assert entries[0].core_start_pts == 0
    assert entries[-1].core_end_pts == coverage.exact_end_pts


def test_deterministic_segment_ids_for_same_inputs() -> None:
    """같은 원본·같은 순서 리비전·같은 경계 → 같은 조각 ID(재개의 안정성)."""
    first, _ = _plan(150, order="order_same")
    second, _ = _plan(150, order="order_same")
    assert [entry.segment_id for entry in first] == [entry.segment_id for entry in second]
    other, _ = _plan(150, order="order_different")
    assert [entry.segment_id for entry in other] != [entry.segment_id for entry in first]


def test_fingerprint_stability() -> None:
    """지문은 순서 리비전·원본 측정·경계에만 달려 있다(§14.2)."""
    clocks = {
        "source_a": {
            "content_sha256": "deadbeef",
            "time_base": "1/90000",
            "origin_pts": 0,
            "frame_count": 150,
            "frame_duration": "1/25",
            "boundaries": [0, 90000, 180000],
        }
    }
    same = {
        "source_a": {
            "content_sha256": "deadbeef",
            "time_base": "1/90000",
            "origin_pts": 0,
            "frame_count": 150,
            "frame_duration": "1/25",
            "boundaries": [0, 90000, 180000],
        }
    }
    changed = {
        "source_a": {
            "content_sha256": "cafebabe",
            "time_base": "1/90000",
            "origin_pts": 0,
            "frame_count": 150,
            "frame_duration": "1/25",
            "boundaries": [0, 90000, 180000],
        }
    }
    assert segmentation.segmentation_fingerprint("order_r", clocks) == (
        segmentation.segmentation_fingerprint("order_r", same)
    )
    assert segmentation.segmentation_fingerprint("order_r", clocks) != (
        segmentation.segmentation_fingerprint("order_r", changed)
    )
    assert segmentation.segmentation_fingerprint("order_r", clocks) != (
        segmentation.segmentation_fingerprint("order_other", clocks)
    )


def test_snapshot_json_round_trip() -> None:
    """계획 기록의 JSON 왕복 — 모양이 어긋나면 죽는다(되살림 아님)."""
    entries, _ = _plan(150)
    payload = segmentation.snapshot_to_json(entries)
    restored = segmentation.snapshot_from_json(payload)
    assert restored == entries
    with pytest.raises(SegmentationError, match="깨졌다"):
        segmentation.snapshot_from_json("{not json")


def test_nonzero_presentation_origin_is_preserved() -> None:
    """0이 아닌 영상 PTS 원점 — 원점 틱이 그대로 첫 조각 시작이다(§8.3)."""
    entries, coverage = _plan(100, origin_pts=180000)
    assert entries[0].core_start_pts == 180000
    assert entries[-1].core_end_pts == coverage.exact_end_pts
