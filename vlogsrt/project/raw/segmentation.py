"""A3 결정적 분할 계획 — 순수 계산, I/O 없음(계획 §6.1·§7.6·§13.1).

경계는 **의미 분석이 아니라 격자가 정한다**: 원본 하나의 측정된 시계
(`RawVideoClock`) 위에서 프레임 경계 격자를 **한 번** 고르고, 그 격자로 전체
재생 구간을 빈틈없이 타일링한다. 조각은 이웃과 경계 프레임을 공유하므로
틈·겹침는 구조적으로 생길 수 없고(§6.1), 조각마다 따로 바깥으로 스냅하지
않는다 — 바깥 스냅은 핸들이 붙는 추출 단계의 몫이다(§6.5).

여기엔 ffmpeg·DB·파일이 없다. 시계·해시는 호출자가 측정해 넘기고, 이 모듈은
계획 값과 증명만 만든다. 의미 경계(Luna)는 A4가 같은 격자 위의 **후보**로
제안할 뿐 이 계층을 다시 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from fractions import Fraction

from vlogsrt.project.raw.models import (
    SEGMENT_RECOMMENDATION_NORMAL,
    SEGMENTATION_BASIS_DETERMINISTIC,
    SEGMENTATION_BASIS_PLANNED,
    PlannedInterval,
    SegmentationError,
    SegmentCoverage,
    SegmentPlanEntry,
)

__all__ = [
    "DEFAULT_TILE_SECONDS",
    "MIN_SEGMENT_FRAMES",
    "SEGMENTATION_SCHEMA_DIGEST",
    "boundary_frames",
    "new_segment_id",
    "plan_segments",
    "segmentation_fingerprint",
    "snapshot_to_json",
    "snapshot_from_json",
]

DEFAULT_TILE_SECONDS = Fraction(30)
"""기본 타일 길이 — 계획 §1.2의 대화 밴드(5~45초) 한가운데 값. 의미 완결성이
길이보다 우선이라는 원칙은 A4의 몫이고, A3는 어디까지나 결정적 기본값이다."""

MIN_SEGMENT_FRAMES = 1
"""조각의 최소 프레임 수 — 경계 둘이 같은 프레임에 떨어지면 빈 조각이다."""

SEGMENTATION_SCHEMA_DIGEST = "raw-segmentation/1:frame-lattice+full-tile"
"""계획 지문에 박는 분할 규칙 요약 — 격자·타일 규칙이 바뀌면 지문이 달라진다."""


def boundary_frames(
    frame_count: int,
    frame_duration: Fraction,
    playable_seconds: Fraction,
    intervals: Sequence[PlannedInterval] | None,
    tile_seconds: Fraction = DEFAULT_TILE_SECONDS,
) -> tuple[int, ...]:
    """경계 프레임 번호 열 — 이 원본의 **유일한** 격자 선택(§6.1).

    시각은 전부 **정규화된 재생 시각**이다: 0 = 제시 원점, `playable_seconds` =
    정규화된 재생 종료(원점에서 잰 길이). 사람이 준 구간도 같은 시계로 읽는다 —
    절대 타임라인 좌표가 아니라 화면에서 흐르는 시간(§6.1의 `00:00.000` 표기).
    첫 경계는 0, 마지막 경계는 `frame_count`를 **이 함수가 붙인다** — 사람이 준
    마지막 끝이 원본보다 길면 원본 끝에서 자른다. 가운데 경계는 초를 격자로
    내리는동일한 규칙(바닥)으로 정수 프레임에 떨어지고, 경계를 각자 따로 스냅하지
    않는다 — 이 열 하나가 모든 조각의 경계가 된다.
    """
    validated_tile = _positive_fraction(tile_seconds, "타일 길이")
    if frame_count < 1:
        raise SegmentationError(f"원본 프레임이 없다: {frame_count}프레임")
    ends = _interval_ends(intervals, playable_seconds)
    if ends is None:
        ends = _tiled_ends(playable_seconds, validated_tile)
    frames = [0]
    for position in ends:
        frame = math.floor(position / frame_duration)
        if frame <= frames[-1] or frame >= frame_count:
            raise SegmentationError(
                f"경계가 격자 위에서 빈 조각을 만든다: {position}초 → 프레임 {frame}"
                f" (이전 경계 {frames[-1]}, 원본 {frame_count}프레임)"
            )
        frames.append(frame)
    frames.append(frame_count)
    return tuple(frames)


def _interval_ends(
    intervals: Sequence[PlannedInterval] | None, playable_seconds: Fraction
) -> list[Fraction] | None:
    """사람이 준 구간의 안쪽 경계 — 전체 타일 검사를 여기서 끝낸다(§6.1).

    첫 구간은 0(재생 원점)에서 시작, 이웃 구간은 경계를 공유, 마지막 끝은 재생
    길이를 넘거나 닿아야 한다(넘는 몫은 원본 끝으로 잘린다). 검사를 통과한
    **안쪽** 경계만 돌려준다 — 0과 원본 끝은 `boundary_frames`가 붙인다.
    """
    if not intervals:
        return None
    ordered = sorted(intervals, key=lambda span: (span.start, span.end))
    if ordered[0].start != 0:
        raise SegmentationError(
            f"첫 구간이 0초(재생 원점)에서 시작하지 않는다: {ordered[0].start}초"
            " — 전체 덮음이 계약이다"
        )
    ends: list[Fraction] = []
    previous_end = Fraction(0)
    for index, span in enumerate(ordered):
        if span.end <= span.start:
            raise SegmentationError(
                f"{index + 1}번째 구간이 비거나 뒤집혔다: [{span.start}, {span.end})"
            )
        if span.start != previous_end:
            raise SegmentationError(
                f"{index + 1}번째 구간이 이웃과 경계를 공유하지 않는다: "
                f"{previous_end}초 → {span.start}초 — 틈·겹침 없는 타일만 받는다"
            )
        previous_end = span.end
        ends.append(span.end)
    if ordered[-1].end < playable_seconds:
        raise SegmentationError(
            f"구간이 원본 끝({playable_seconds}초)을 덮지 못한다: 마지막 끝 "
            f"{ordered[-1].end}초 — 전체 덮음이 계약이다"
        )
    return ends[:-1]


def _tiled_ends(playable_seconds: Fraction, tile_seconds: Fraction) -> list[Fraction]:
    """균일 타일의 안쪽 경계 — 원본 끝은 마지막 타일에 흡수된다(남은 조각 내주기)."""
    tiles = max(1, math.ceil(playable_seconds / tile_seconds))
    if tiles == 1:
        return []
    return [playable_seconds * Fraction(index, tiles) for index in range(1, tiles)]


def _positive_fraction(value: Fraction, label: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int)):
        raise SegmentationError(f"{label}은 Fraction 또는 정수여야 한다: {value!r}")
    parsed = Fraction(value)
    if parsed <= 0:
        raise SegmentationError(f"{label}이 양수가 아니다: {parsed}")
    return parsed


def new_segment_id(asset_id: str, order_revision_id: str, core_start_pts: int) -> str:
    """조각의 안정적 신원 — 파일명이 아니라 내용·입력의 지문(§13.1).

    같은 원본·같은 순서 리비전·같은 경계에서 같은 ID가 나오므로, 같은 계획을
    다시 세워도 조각 신원이 흔들리지 않는다.
    """
    digest = hashlib.sha256(
        f"{asset_id}|{order_revision_id}|{core_start_pts}".encode()
    ).hexdigest()[:16]
    return f"seg_{digest}"


def plan_segments(
    *,
    asset_id: str,
    batch_dir: str,
    source_order: int,
    time_base: Fraction,
    presentation_origin_pts: int,
    frame_count: int,
    frame_duration: Fraction,
    exact_end_pts: int,
    intervals: Sequence[PlannedInterval] | None,
    order_revision_id: str,
    tile_seconds: Fraction = DEFAULT_TILE_SECONDS,
) -> tuple[tuple[SegmentPlanEntry, ...], SegmentCoverage]:
    """경계 열을 조각 기록으로 — 이웃 조각은 경계 틱을 정확히 공유한다.

    `core[i].end_pts == core[i+1].start_pts`는 `clock_frame_range`의 연속 호출이
    같은 프레임 번호에서 같은 틱을 내기 때문에 구조적으로 보장된다. 여기서
    다시 증명해 계획과 함께 남긴다(§6.1의 다섯 조건).
    """
    origin_seconds = presentation_origin_pts * time_base
    exact_end_seconds = exact_end_pts * time_base
    playable_seconds = exact_end_seconds - origin_seconds
    frames = boundary_frames(frame_count, frame_duration, playable_seconds, intervals, tile_seconds)
    basis = SEGMENTATION_BASIS_PLANNED if intervals else SEGMENTATION_BASIS_DETERMINISTIC
    entries: list[SegmentPlanEntry] = []
    for index, (first, after) in enumerate(zip(frames[:-1], frames[1:], strict=True)):
        start_pts = _snap(origin_seconds + first * frame_duration, time_base)
        end_pts = _snap(origin_seconds + after * frame_duration, time_base)
        if index == len(frames) - 2 and end_pts != exact_end_pts:
            raise SegmentationError(
                f"마지막 경계 틱이 재생 종료와 다르다: {end_pts} ≠ {exact_end_pts}"
            )
        if end_pts <= start_pts:
            raise SegmentationError(
                f"조각 {index + 1}의 틱이 비거나 뒤집혔다: [{start_pts}, {end_pts})"
            )
        entries.append(
            SegmentPlanEntry(
                segment_id=new_segment_id(asset_id, order_revision_id, start_pts),
                asset_id=asset_id,
                batch_dir=batch_dir,
                source_order=source_order,
                segment_order=index + 1,
                time_base_num=time_base.numerator,
                time_base_den=time_base.denominator,
                core_start_pts=start_pts,
                core_end_pts=end_pts,
                media_start_pts=start_pts,
                media_end_pts=end_pts,
                core_first_frame=first,
                core_after_last_frame=after,
                source_frame_count=frame_count,
                recommendation=SEGMENT_RECOMMENDATION_NORMAL,
                basis=basis,
            )
        )
    coverage = _coverage(
        asset_id=asset_id,
        source_order=source_order,
        entries=entries,
        time_base=time_base,
        presentation_origin_pts=presentation_origin_pts,
        exact_end_pts=exact_end_pts,
        frame_count=frame_count,
    )
    return tuple(entries), coverage


def _snap(seconds: Fraction, time_base: Fraction) -> int:
    """격자 시각을 틱으로 — 천장 하나로 통일했다.

    같은 경계가 조각 i의 끝과 조각 i+1의 시작에 **같은 틱**으로 들어가야
    이웃이 정확히 맞닿는다(§6.1). 시작은 바닥·끝은 천장으로 따로 스냅하면
    경계마다 한 틱의 겹침·틈이 생긴다. 천장 통일은 모든 경계을 원점 +
    n×프레임길이에서 **한 번** 계산하므로 누수도 없다(바닥과의 차는 항상
    1틱 미만이다).
    """
    return math.ceil(seconds / time_base)


def _coverage(
    *,
    asset_id: str,
    source_order: int,
    entries: Sequence[SegmentPlanEntry],
    time_base: Fraction,
    presentation_origin_pts: int,
    exact_end_pts: int,
    frame_count: int,
) -> SegmentCoverage:
    """타일링 증명 — 계획이 생성되는 자리에서 §6.1의 조건을 다시 통과시킨다."""
    if not entries:
        raise SegmentationError(f"조각이 하나도 없다: {asset_id}")
    for index, entry in enumerate(entries):
        if entry.segment_order != index + 1:
            raise SegmentationError(f"조각 번호가 오름차순이 아니다: {entry.segment_order}")
    starts = [entry.core_start_pts for entry in entries]
    if starts != sorted(starts):
        raise SegmentationError("조각 시작이 번호 순과 다르다 — 순서는 시각을 따른다")
    for left, right in zip(entries[:-1], entries[1:], strict=True):
        if left.core_end_pts != right.core_start_pts:
            raise SegmentationError(
                f"이웃 조각의 경계가 어긋난다: {left.core_end_pts} ≠ {right.core_start_pts}"
            )
    if entries[0].core_start_pts != presentation_origin_pts:
        raise SegmentationError(
            f"첫 조각이 원점에서 시작하지 않는다: {entries[0].core_start_pts}"
            f" ≠ {presentation_origin_pts}"
        )
    if entries[-1].core_end_pts != exact_end_pts:
        raise SegmentationError(
            f"마지막 조각이 재생 종료에서 끝나지 않는다: {entries[-1].core_end_pts}"
            f" ≠ {exact_end_pts}"
        )
    return SegmentCoverage(
        asset_id=asset_id,
        source_order=source_order,
        segment_count=len(entries),
        source_frame_count=frame_count,
        first_core_start_pts=entries[0].core_start_pts,
        last_core_end_pts=entries[-1].core_end_pts,
        presentation_origin_pts=presentation_origin_pts,
        exact_end_pts=exact_end_pts,
        time_base_num=time_base.numerator,
        time_base_den=time_base.denominator,
    )


def segmentation_fingerprint(
    order_revision_id: str, clocks: Mapping[str, dict[str, object]]
) -> str:
    """계획 지문 — 순서 리비전과 원본별 해시·시계·경계를 묶는 sha256.

    같은 지문이면 같은 조각 배열이라는 뜻이다. 추출 작업 지문의 밑천이 되므로
    원본 내용 해시가 여기 들어간다(§14.2의 추출 캐시 키와 같은 재료).
    """
    payload = json.dumps(
        {
            "schema": SEGMENTATION_SCHEMA_DIGEST,
            "order_revision": order_revision_id,
            "sources": {key: dict(value) for key, value in sorted(clocks.items())},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_to_json(snapshot_plan: tuple[SegmentPlanEntry, ...]) -> str:
    """조각 기록의 정렬된 JSON — DB가 리비전 행에 그대로 묻는다(§9.2)."""
    records = [
        {
            "segment_id": entry.segment_id,
            "asset_id": entry.asset_id,
            "batch_dir": entry.batch_dir,
            "source_order": entry.source_order,
            "segment_order": entry.segment_order,
            "time_base_num": entry.time_base_num,
            "time_base_den": entry.time_base_den,
            "core_start_pts": entry.core_start_pts,
            "core_end_pts": entry.core_end_pts,
            "media_start_pts": entry.media_start_pts,
            "media_end_pts": entry.media_end_pts,
            "core_first_frame": entry.core_first_frame,
            "core_after_last_frame": entry.core_after_last_frame,
            "source_frame_count": entry.source_frame_count,
            "recommendation": entry.recommendation,
            "basis": entry.basis,
        }
        for entry in snapshot_plan
    ]
    return json.dumps(records, ensure_ascii=False, sort_keys=True)


def snapshot_from_json(payload: str) -> tuple[SegmentPlanEntry, ...]:
    """묻어 둔 JSON을 조각 기록으로 되살리기 — 모양이 어긋나면 죽는다(되살림 아님)."""
    try:
        records = json.loads(payload)
        return tuple(SegmentPlanEntry(**record) for record in records)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise SegmentationError(f"분할 계획 기록이 깨졌다: {error}") from error
