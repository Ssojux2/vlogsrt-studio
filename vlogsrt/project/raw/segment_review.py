"""A5 조각 검토의 순수 도메인 — 불변 기록·리비전 편집·시간순 질의·직렬화.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §6·§10.3·§12(화면 2)·§13·16-A5.
브라우저 검토 화면이 필요로 하는 **값과 계산만** 있다: 이 모듈은 네트워크·ffmpeg·
파일 쓰기를 전혀 하지 않고(`review_projection.py`와 함께 A5의 순수 핵심이다),
UI·DB·작업 연동은 다음 주인이 이 선 위에 얹는다.

지키는 선:

- **모든 편집은 새 리비전이다.** 리비전을 이루는 기록은 전부 얼어 있고(Frozen
  dataclass + tuple), 고친 자리만 바뀐 새 배열로 통째로 나온다. 거부된 편집은
  이전 리비전을 그대로 둔다(§12: 변경 revision).
- **원본 좌표는 정수 PTS다.** 시각은 `time_base`와 함께 읽고(§13.3), 경계는 반드시
  프레임 격자 위다. 원본별 덮개 `[origin_pts, end_pts]`는 어느 편집 뒤에도 구멍·
  겹침 없이 온전하다(§6.1).
- **선택은 상태일 뿐이다.** 사용/제외는 조각을 지우지 않고 표시만 바꾼다(§6.4).
  표시 모드는 `ReviewViewState`가 따로 들고 리비전 지문에 끼지 않는다(§10.3).
- **직렬화는 엄격하다.** 모르는 스키마·틀린 타입·범위 밖 값은 거부한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Protocol, runtime_checkable

REVIEW_SCHEMA_VERSION = "raw-workspace/v1"
"""검토 리비전 직렬화의 스키마 이름 — `raw-workspace/v1` namespace(§13)."""

SELECTION_INCLUDED = "included"
SELECTION_EXCLUDED = "excluded"
SELECTIONS: tuple[str, ...] = (SELECTION_INCLUDED, SELECTION_EXCLUDED)
"""사용/제외 — 제외는 조각을 지우는 게 아니라 선택 상태다(§6.4)."""

RECOMMENDED = "recommended"
REVIEW_REQUIRED = "review_required"
RECOMMENDATIONS: tuple[str, ...] = (RECOMMENDED, "normal", REVIEW_REQUIRED)
"""기본 분할이 붙이는 의미 등급 — 정렬·서식 근거가 아니라 검토 상태다."""

AUTO_EXCLUDED_UNREVIEWED_TAG = "자동 제외 · 미검토"
"""자동 정리가 제외했고 사람이 아직 보지 않은 조각의 표지(D35).

`visual_tags`에 실리므로 분할·병합·경계 이동·재분석이 그대로 나른다(병합은 합집합이라
미검토 쪽이 이긴다). 지우는 길은 둘뿐이다 — 그 조각의 명시적 선택 편집, 또는 퍼사드의
"자동 제외 확인". 메모 편집·등급 덮기·출력은 못 지운다.
"""

SPEECH_DETECTED = "speech_detected"
SPEECH_UNCONFIRMED_EMPTY = "unconfirmed_empty"
SPEECH_ANALYSIS_FAILED = "analysis_failed"
SPEECH_AUDIO_ABSENT = "audio_absent"
SPEECH_STATUSES: tuple[str, ...] = (
    SPEECH_DETECTED,
    SPEECH_UNCONFIRMED_EMPTY,
    SPEECH_ANALYSIS_FAILED,
    SPEECH_AUDIO_ABSENT,
)
"""§7.3의 네 상태 — 없는 증거를 발화로 꾸미지 않기 위해 서로 다른 값이다."""

DISPLAY_VIDEO_ONLY = "video_only"
DISPLAY_VIDEO_WITH_TRANSCRIPT = "video_with_transcript"
DISPLAY_MODES: tuple[str, ...] = (DISPLAY_VIDEO_ONLY, DISPLAY_VIDEO_WITH_TRANSCRIPT)
"""§10.1의 두 표시 모드 — 보기 상태일 뿐 내용·지문을 바꾸지 않는다."""

EDIT_KINDS: tuple[str, ...] = (
    "split",
    "merge",
    "boundary",
    "selection",
    "override",
    "note",
)
"""리비전을 만드는 편집의 여섯 종류 — 이외의 종류는 적용 거부다."""

# 병합·경계 조정에서 더 보수적인 쪽이 이긴다 — 확신을 붙였다고 믿으면 안 되므로.
_SEVERITY = (SPEECH_DETECTED, SPEECH_UNCONFIRMED_EMPTY, SPEECH_AUDIO_ABSENT, SPEECH_ANALYSIS_FAILED)
_RECOMMENDATION_RANK = {RECOMMENDED: 0, "normal": 1, REVIEW_REQUIRED: 2}


class ReviewError(RuntimeError):
    """검토 도메인의 오류 — 메시지가 대상·좌표·값을 말한다."""


class ReviewValidationError(ReviewError):
    """리비전 구성이 계약을 어겼다 — 만들어지지 않고 이유가 나온다."""


class ReviewEditError(ReviewError):
    """편집이 거부됐다 — 이전 리비전은 그대로다."""


class StaleRevisionError(ReviewEditError):
    """편집이 겨냥한 리비전이 지금 손에 있는 것이 아니다 — 못박힌 대상만 고친다."""


class ReviewParseError(ReviewError):
    """직렬화 문서가 계약 밖이다 — 모르는 스키마·타입·범위는 못 들어온다."""


# ─────────────────────────────────────────────────────────────
#  원본 시계 — PTS·timebase·프레임 격자의 정본
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceClock:
    """원본 하나의 얼어 붙은 시계 — 정수 PTS와 프레임 격자를 함께 못 박는다.

    `origin_pts`는 정규화된 재생 원점(첫 조각의 시작)이고 `end_pts`는 정규화된
    재생 종료다(§6.1). 격자는 원점에 앵커를 묶은 `frame_ticks` 간격이며, 모든
    경계 PTS는 그 정수 좌표 위에만 있을 수 있다.
    """

    source_id: str
    source_sha256: str
    time_base_num: int
    time_base_den: int
    origin_pts: int
    end_pts: int
    frame_ticks: int

    @property
    def time_base(self) -> Fraction:
        """PTS 틱을 초로 읽는 정확 유리수(§13.3)."""
        return Fraction(self.time_base_num, self.time_base_den)

    def is_frame_boundary(self, pts: int) -> bool:
        """`pts`가 이 원본의 프레임 경계인가 — 원점 앵커 격자의 정수 좌표 검사."""
        return (pts - self.origin_pts) % self.frame_ticks == 0

    def covers(self, pts: int) -> bool:
        """`pts`가 `[origin_pts, end_pts]` 안인가."""
        return self.origin_pts <= pts <= self.end_pts


def make_source_clock(
    source_id: str,
    source_sha256: str,
    time_base_num: int,
    time_base_den: int,
    origin_pts: int,
    end_pts: int,
    frame_duration: Fraction,
) -> SourceClock:
    """프레임 길이(초, 정확 유리수)로 시계를 만든다 — 틱 환산이 정수가 아니면 거부한다.

    예: timebase 1/90000 · 29.97fps(1001/30000초) → `frame_ticks = 3003`.
    격자가 timebase에 떨어지지 않으면 정확한 프레임 경계가 존재할 수 없으므로
    시계 자체를 만들지 않는다.
    """
    ticks = Fraction(
        time_base_den * frame_duration.numerator, time_base_num * frame_duration.denominator
    )
    if ticks.denominator != 1:
        raise ReviewValidationError(
            f"원본 {source_id!r}: 프레임 길이 {frame_duration}초가 timebase "
            f"{time_base_num}/{time_base_den} 틱에 떨어지지 않는다 — 정확한 격자가 없다"
        )
    clock = SourceClock(
        source_id=source_id,
        source_sha256=source_sha256,
        time_base_num=time_base_num,
        time_base_den=time_base_den,
        origin_pts=origin_pts,
        end_pts=end_pts,
        frame_ticks=ticks.numerator,
    )
    _check_clock(clock)
    return clock


def _check_clock(clock: SourceClock) -> None:
    """시계 필드의 원초적 계약 — 얼기 전에 한 번, 파싱 뒤에 또 한 번."""
    if not clock.source_id or not isinstance(clock.source_id, str):
        raise ReviewValidationError("원본 ID는 비지 않은 문자열이어야 한다")
    if clock.time_base_num <= 0 or clock.time_base_den <= 0:
        raise ReviewValidationError(f"원본 {clock.source_id!r}: timebase는 양수여야 한다")
    if clock.frame_ticks <= 0:
        raise ReviewValidationError(f"원본 {clock.source_id!r}: 프레임 틱은 양수여야 한다")
    if clock.origin_pts >= clock.end_pts:
        raise ReviewValidationError(
            f"원본 {clock.source_id!r}: origin {clock.origin_pts} >= end {clock.end_pts}"
        )
    if not clock.is_frame_boundary(clock.end_pts):
        raise ReviewValidationError(
            f"원본 {clock.source_id!r}: end {clock.end_pts}가 프레임 격자 밖이다"
        )


# ─────────────────────────────────────────────────────────────
#  조각·리비전 기록
# ─────────────────────────────────────────────────────────────


@runtime_checkable
class SegmentPlanSource(Protocol):
    """분할 계획 조각의 구조 계약 — W1의 `SegmentPlanEntry`가 import 없이 통과한다.

    `asset_id`가 원본 신원이다. `speech_status`·`visual_tags`는 아직 붙지 않은
    계획(A3)을 위해 선택적이다 — 없으면 각각 `unconfirmed_empty`·빈 tuple이 된다.
    """

    segment_id: str
    asset_id: str
    source_order: int
    segment_order: int
    time_base_num: int
    time_base_den: int
    core_start_pts: int
    core_end_pts: int
    recommendation: str


@dataclass(frozen=True, slots=True)
class ReviewSegment:
    """검토 리비전의 조각 하나 — 시각은 원본 좌표 정수 PTS다(§13.3).

    `selection`은 선택 상태일 뿐(§6.4), `override_recommendation`은 사용자가
    기본 등급을 덮은 기록, `notes`는 검토 메모다 — 셋 다 리비전의 얼어 붙은
    데이터이고 고치면 새 리비전이 나온다.
    """

    segment_id: str
    source_id: str
    source_order: int
    segment_order: int
    start_pts: int
    end_pts: int
    speech_status: str
    visual_tags: tuple[str, ...]
    recommendation: str
    override_recommendation: str | None
    selection: str
    notes: str


@dataclass(frozen=True, slots=True)
class ReviewEdit:
    """하나의 편집 명령 — 겨냥한 리비전과 매개변수를 얼어 붙인다.

    `target_revision_id`가 다른 리비전에 적용되면 `StaleRevisionError`다.
    리비전은 자기를 만든 편집 하나만 달고 다닌다(전체 역histories은 호출자 소유).
    """

    kind: str
    target_revision_id: str
    segment_ids: tuple[str, ...]
    split_pts: int | None = None
    boundary_pts: int | None = None
    selection: str | None = None
    override: str | None = None
    notes: str | None = None


def split_edit(target_revision_id: str, segment_id: str, split_pts: int) -> ReviewEdit:
    """조각을 `split_pts`에서 둘로 나누는 명령(§12 화면 2 '분할')."""
    return ReviewEdit("split", target_revision_id, (segment_id,), split_pts=split_pts)


def merge_edit(
    target_revision_id: str, first_segment_id: str, second_segment_id: str
) -> ReviewEdit:
    """같은 원본의 맞닿은 두 조각을 하나로 합치는 명령(§12 '다음 조각과 병합')."""
    return ReviewEdit("merge", target_revision_id, (first_segment_id, second_segment_id))


def boundary_edit(
    target_revision_id: str, left_segment_id: str, right_segment_id: str, boundary_pts: int
) -> ReviewEdit:
    """맞닿은 두 조각의 공유 경계를 `boundary_pts`로 옮기는 명령(§12 '경계 조정')."""
    return ReviewEdit(
        "boundary",
        target_revision_id,
        (left_segment_id, right_segment_id),
        boundary_pts=boundary_pts,
    )


def selection_edit(target_revision_id: str, segment_id: str, selection: str) -> ReviewEdit:
    """사용/제외 표시를 바꾸는 명령 — 조각은 지워지지 않는다(§6.4)."""
    return ReviewEdit("selection", target_revision_id, (segment_id,), selection=selection)


def override_edit(target_revision_id: str, segment_id: str, override: str | None) -> ReviewEdit:
    """사용자가 기본 등급을 덮는(또는 덮기를 거두는) 명령."""
    return ReviewEdit("override", target_revision_id, (segment_id,), override=override)


def note_edit(target_revision_id: str, segment_id: str, notes: str) -> ReviewEdit:
    """검토 메모를 쓰는 명령 — 글자는 그대로 실는다."""
    return ReviewEdit("note", target_revision_id, (segment_id,), notes=notes)


@dataclass(frozen=True, slots=True)
class ReviewRevision:
    """검토 리비전 — 프로젝트·기본 계획·원본 해시·틱 격자에 묶인 불변 상태(§13.1).

    `revision_id`는 `project/base_plan/sequence`에서 정해지므로 같은 신원의 다음
    편집은 늘 다른 ID다. `segments`는 각 원본을 `[origin, end]`까지 빈틈없이
    덮는 타일이고, `edit`는 이 리비전을 만든 편집이다(기본 리비전은 None).
    """

    project_id: str
    base_plan_id: str
    sequence: int
    clocks: tuple[SourceClock, ...]
    segments: tuple[ReviewSegment, ...]
    parent_revision_id: str | None
    edit: ReviewEdit | None

    @property
    def revision_id(self) -> str:
        return f"{self.project_id}/{self.base_plan_id}/{self.sequence:06d}"

    @property
    def source_hashes(self) -> tuple[tuple[str, str], ...]:
        """리비전이 묶인 원본 해시 — (source_id, sha256)을 원본 순서 그대로."""
        return tuple(
            (clock.source_id, clock.source_sha256)
            for clock in sorted(self.clocks, key=lambda clock: clock.source_id)
        )

    def clock_for(self, source_id: str) -> SourceClock | None:
        for clock in self.clocks:
            if clock.source_id == source_id:
                return clock
        return None


@dataclass(frozen=True, slots=True)
class ReviewViewState:
    """보기 상태 — 표시 모드·선택·필터는 리비전 내용이 아니다(§10.3).

    전환만으로 API·재인코딩·파일 쓰기가 일어나지 않는다는 계약을 **타입으로**
    지킨다: 이 기록은 `content_fingerprint`에 끼지 않고 직렬화되지도 않는다.
    """

    display_mode: str = DISPLAY_VIDEO_ONLY
    selected_segment_id: str | None = None
    query: str = ""
    tag_filter: tuple[str, ...] = ()
    show_excluded: bool = True

    def with_display_mode(self, mode: str) -> ReviewViewState:
        """표시 모드만 바꾼 새 보기 상태 — 허용 밖 모드는 거부한다."""
        if mode not in DISPLAY_MODES:
            raise ReviewValidationError(f"알 수 없는 표시 모드: {mode!r}")
        return replace(self, display_mode=mode)


# ─────────────────────────────────────────────────────────────
#  리비전 구성과 검증
# ─────────────────────────────────────────────────────────────


def _validated(
    project_id: str,
    base_plan_id: str,
    sequence: int,
    clocks: tuple[SourceClock, ...],
    segments: tuple[ReviewSegment, ...],
    parent_revision_id: str | None,
    edit: ReviewEdit | None,
) -> ReviewRevision:
    """모든 계약을 통과한 리비전만 나온다 — 어긋나면 리비전은 태어나지 않는다."""
    if not project_id or not base_plan_id:
        raise ReviewValidationError("프로젝트·기본 계획 ID는 비지 않은 문자열이어야 한다")
    if sequence < 0:
        raise ReviewValidationError("sequence는 0 이상이어야 한다")
    _check_clocks(clocks)
    _check_segment_fields(segments, clocks)
    _check_tiling(segments, clocks)
    _check_lineage(sequence, parent_revision_id, edit)
    return ReviewRevision(
        project_id=project_id,
        base_plan_id=base_plan_id,
        sequence=sequence,
        clocks=clocks,
        segments=segments,
        parent_revision_id=parent_revision_id,
        edit=edit,
    )


def _check_clocks(clocks: tuple[SourceClock, ...]) -> None:
    if not clocks:
        raise ReviewValidationError("리비전은 원본 시계를 하나 이상 실어야 한다")
    seen: set[str] = set()
    for clock in clocks:
        _check_clock(clock)
        if clock.source_id in seen:
            raise ReviewValidationError(f"원본 {clock.source_id!r}의 시계가 두 개다")
        seen.add(clock.source_id)


def _check_segment_fields(
    segments: tuple[ReviewSegment, ...], clocks: tuple[SourceClock, ...]
) -> None:
    clock_ids = {clock.source_id for clock in clocks}
    ids: set[str] = set()
    for segment in segments:
        if not segment.segment_id:
            raise ReviewValidationError("조각 ID는 비지 않은 문자열이어야 한다")
        if segment.segment_id in ids:
            raise ReviewValidationError(f"조각 ID {segment.segment_id!r}가 중복이다")
        ids.add(segment.segment_id)
        if segment.source_id not in clock_ids:
            raise ReviewValidationError(
                f"조각 {segment.segment_id!r}: 원본 {segment.source_id!r}의 시계가 리비전에 없다"
            )
        if segment.speech_status not in SPEECH_STATUSES:
            raise ReviewValidationError(
                f"조각 {segment.segment_id!r}: 알 수 없는 발화 상태 {segment.speech_status!r}"
            )
        if segment.recommendation not in RECOMMENDATIONS:
            raise ReviewValidationError(
                f"조각 {segment.segment_id!r}: 알 수 없는 등급 {segment.recommendation!r}"
            )
        if segment.override_recommendation is not None and (
            segment.override_recommendation not in RECOMMENDATIONS
        ):
            raise ReviewValidationError(
                f"조각 {segment.segment_id!r}: 알 수 없는 덮기 등급 "
                f"{segment.override_recommendation!r}"
            )
        if segment.selection not in SELECTIONS:
            raise ReviewValidationError(
                f"조각 {segment.segment_id!r}: 알 수 없는 선택 {segment.selection!r}"
            )
        if segment.source_order < 0 or segment.segment_order < 0:
            raise ReviewValidationError(f"조각 {segment.segment_id!r}: 순번은 0 이상이어야 한다")


def _check_tiling(segments: tuple[ReviewSegment, ...], clocks: tuple[SourceClock, ...]) -> None:
    """원본별 덮개 검증 — 첫 start=원점, 이웃 경계 일치, 끝=종료, 순번=시작 순서(§6.1)."""
    orders_seen: dict[int, str] = {}
    for clock in clocks:
        tiles = sorted(
            (segment for segment in segments if segment.source_id == clock.source_id),
            key=lambda segment: segment.start_pts,
        )
        if not tiles:
            raise ReviewValidationError(f"원본 {clock.source_id!r}를 덮는 조각이 없다")
        if tiles[0].start_pts != clock.origin_pts:
            raise ReviewValidationError(
                f"원본 {clock.source_id!r}: 첫 조각이 원점 {clock.origin_pts}에서 시작하지 않는다"
            )
        if tiles[-1].end_pts != clock.end_pts:
            raise ReviewValidationError(
                f"원본 {clock.source_id!r}: 마지막 조각이 종료 {clock.end_pts}에서 닫지 않는다"
            )
        source_orders = {tile.source_order for tile in tiles}
        if len(source_orders) != 1:
            raise ReviewValidationError(
                f"원본 {clock.source_id!r}: source_order가 조각마다 다르다 {sorted(source_orders)}"
            )
        source_order = tiles[0].source_order
        if source_order in orders_seen:
            raise ReviewValidationError(
                f"source_order {source_order}가 {orders_seen[source_order]!r}와 "
                f"{clock.source_id!r}에 겹친다"
            )
        orders_seen[source_order] = clock.source_id
        _check_tile_neighbors(clock, tiles)


def _check_tile_neighbors(clock: SourceClock, tiles: Sequence[ReviewSegment]) -> None:
    previous: ReviewSegment | None = None
    for position, tile in enumerate(tiles):
        if not clock.covers(tile.start_pts) or not clock.covers(tile.end_pts):
            raise ReviewValidationError(
                f"조각 {tile.segment_id!r}: 범위 [{tile.start_pts}, {tile.end_pts})가 "
                f"원본 [{clock.origin_pts}, {clock.end_pts}] 밖이다"
            )
        if tile.start_pts >= tile.end_pts:
            raise ReviewValidationError(
                f"조각 {tile.segment_id!r}: start {tile.start_pts} >= end {tile.end_pts}"
            )
        if not (clock.is_frame_boundary(tile.start_pts) and clock.is_frame_boundary(tile.end_pts)):
            raise ReviewValidationError(
                f"조각 {tile.segment_id!r}: 경계가 프레임 격자 밖이다 "
                f"[{tile.start_pts}, {tile.end_pts})"
            )
        if tile.segment_order != position:
            raise ReviewValidationError(
                f"조각 {tile.segment_id!r}: segment_order {tile.segment_order}가 "
                f"시작 순서 위치 {position}와 다르다(§6.1)"
            )
        if previous is not None and previous.end_pts != tile.start_pts:
            raise ReviewValidationError(
                f"원본 {clock.source_id!r}: 조각 {previous.segment_id!r}과 {tile.segment_id!r} "
                f"사이가 맞닿지 않는다({previous.end_pts} != {tile.start_pts})"
            )
        previous = tile


def _check_lineage(sequence: int, parent_revision_id: str | None, edit: ReviewEdit | None) -> None:
    if edit is not None and edit.kind not in EDIT_KINDS:
        raise ReviewValidationError(f"알 수 없는 편집 종류 {edit.kind!r}")
    if sequence == 0:
        if parent_revision_id is not None or edit is not None:
            raise ReviewValidationError("기본 리비전(sequence 0)은 부모도 편집도 없다")
        return
    if parent_revision_id is None or edit is None:
        raise ReviewValidationError("편집 리비전은 부모와 편집을 둘 다 실어야 한다")
    if edit.target_revision_id != parent_revision_id:
        raise ReviewValidationError(
            f"편집이 겨냥한 {edit.target_revision_id!r}가 부모 {parent_revision_id!r}가 아니다"
        )


def initial_revision(
    project_id: str,
    base_plan_id: str,
    clocks: tuple[SourceClock, ...],
    entries: Iterable[SegmentPlanSource],
) -> ReviewRevision:
    """확정 분할 계획을 검토 기본 리비전으로 — W1 계획 기록을 구조로만 읽는다.

    `speech_status`·`visual_tags`가 없는 계획(A3)은 각각 `unconfirmed_empty`와
    빈 tuple이 된다 — 측정이 없다고 발화를 지어내지 않는다(§7.3). 계획의
    timebase가 원본 시계와 다르면 좌표가 다른 시계이므로 거부한다(§13.3).
    """
    segments: list[ReviewSegment] = []
    for entry in entries:
        _entry_clock(clocks, entry)
        segments.append(
            ReviewSegment(
                segment_id=entry.segment_id,
                source_id=entry.asset_id,
                source_order=entry.source_order,
                segment_order=0,
                start_pts=entry.core_start_pts,
                end_pts=entry.core_end_pts,
                speech_status=getattr(entry, "speech_status", SPEECH_UNCONFIRMED_EMPTY),
                visual_tags=tuple(getattr(entry, "visual_tags", ())),
                recommendation=entry.recommendation,
                override_recommendation=None,
                selection=SELECTION_INCLUDED,
                notes="",
            )
        )
    return _validated(
        project_id,
        base_plan_id,
        0,
        clocks,
        _renumber(tuple(segments)),
        parent_revision_id=None,
        edit=None,
    )


def _entry_clock(clocks: tuple[SourceClock, ...], entry: SegmentPlanSource) -> SourceClock:
    for clock in clocks:
        if clock.source_id == entry.asset_id:
            if (entry.time_base_num, entry.time_base_den) != (
                clock.time_base_num,
                clock.time_base_den,
            ):
                raise ReviewValidationError(
                    f"조각 {entry.segment_id!r}: 계획 timebase "
                    f"{entry.time_base_num}/{entry.time_base_den}가 원본 시계 "
                    f"{clock.time_base_num}/{clock.time_base_den}와 다르다"
                )
            return clock
    raise ReviewValidationError(
        f"조각 {entry.segment_id!r}: 원본 {entry.asset_id!r}의 시계가 리비전에 없다"
    )


def _renumber(segments: tuple[ReviewSegment, ...]) -> tuple[ReviewSegment, ...]:
    """원본별 시작 순서로 segment_order를 0부터 다시 매긴다 — 번호는 리비전의 값이다(§13.1)."""
    renumbered: list[ReviewSegment] = []
    positions: dict[str, int] = {}
    for segment in sorted(
        segments, key=lambda item: (item.source_order, item.start_pts, item.segment_order)
    ):
        position = positions.get(segment.source_id, 0)
        positions[segment.source_id] = position + 1
        renumbered.append(replace(segment, segment_order=position))
    return tuple(renumbered)


# ─────────────────────────────────────────────────────────────
#  편집 적용 — 늘 새 리비전, 거부면 이전 리비전 그대로
# ─────────────────────────────────────────────────────────────


def apply_edit(revision: ReviewRevision, edit: ReviewEdit) -> ReviewRevision:
    """편집을 리비전에 적용해 **다음** 리비전을 만든다 — 원본은 절대 고쳐지지 않는다.

    `edit.target_revision_id`가 다르면 `StaleRevisionError`다: 늦은 완성이 이미
    앞선 편집을 덮지 못하게 한다. 거부되는 순간 예외가 나고 리비전은 하나도
    바뀌지 않는다(불변 기록이라 시도 자체가 남아 있을 수 없다).
    """
    if edit.target_revision_id != revision.revision_id:
        raise StaleRevisionError(
            f"편집은 {edit.target_revision_id!r}를 겨냥했는데 지금 손에 있는 건 "
            f"{revision.revision_id!r}다"
        )
    appliers = {
        "split": _apply_split,
        "merge": _apply_merge,
        "boundary": _apply_boundary,
        "selection": _apply_metadata,
        "override": _apply_metadata,
        "note": _apply_metadata,
    }
    applier = appliers.get(edit.kind)
    if applier is None:
        raise ReviewEditError(f"알 수 없는 편집 종류 {edit.kind!r}")
    segments = applier(revision, edit)
    return _validated(
        revision.project_id,
        revision.base_plan_id,
        revision.sequence + 1,
        revision.clocks,
        _renumber(segments),
        parent_revision_id=revision.revision_id,
        edit=edit,
    )


def _find_segment(revision: ReviewRevision, segment_id: str) -> ReviewSegment:
    for segment in revision.segments:
        if segment.segment_id == segment_id:
            return segment
    raise ReviewEditError(f"리비전 {revision.revision_id!r}에 조각 {segment_id!r}가 없다")


def _require_frame_point(clock: SourceClock, segment_id: str, pts: int) -> None:
    if not clock.is_frame_boundary(pts):
        raise ReviewEditError(
            f"조각 {segment_id!r}: {pts}는 원본 {clock.source_id!r}의 프레임 격자 밖이다"
        )


def _apply_split(revision: ReviewRevision, edit: ReviewEdit) -> tuple[ReviewSegment, ...]:
    (segment_id,) = edit.segment_ids
    segment = _find_segment(revision, segment_id)
    clock = _required_clock(revision, segment)
    split_pts = edit.split_pts
    if split_pts is None:
        raise ReviewEditError("분할 편집은 split_pts를 실어야 한다")
    _require_frame_point(clock, segment_id, split_pts)
    if not segment.start_pts < split_pts < segment.end_pts:
        raise ReviewEditError(
            f"분할 점 {split_pts}는 조각 {segment_id!r}의 내부 엄격히 안이어야 한다 "
            f"[{segment.start_pts}, {segment.end_pts})"
        )
    first_id, second_id = f"{segment_id}#1", f"{segment_id}#2"
    taken = {existing.segment_id for existing in revision.segments}
    if first_id in taken or second_id in taken:
        raise ReviewEditError(f"분할 결과 ID가 이미 있다: {first_id!r}/{second_id!r}")
    children = (
        replace(segment, segment_id=first_id, end_pts=split_pts),
        replace(segment, segment_id=second_id, start_pts=split_pts),
    )
    rebuilt: list[ReviewSegment] = []
    for existing in revision.segments:
        if existing.segment_id == segment_id:
            rebuilt.extend(children)
        else:
            rebuilt.append(existing)
    return tuple(rebuilt)


def _required_clock(revision: ReviewRevision, segment: ReviewSegment) -> SourceClock:
    clock = revision.clock_for(segment.source_id)
    if clock is None:
        raise ReviewEditError(
            f"조각 {segment.segment_id!r}: 원본 {segment.source_id!r}의 시계가 리비전에 없다"
        )
    return clock


def _chronological_pair(
    revision: ReviewRevision, edit: ReviewEdit
) -> tuple[ReviewSegment, ReviewSegment]:
    if len(edit.segment_ids) != 2:
        raise ReviewEditError("병합·경계 편집은 조각 둘을 겨냥해야 한다")
    first = _find_segment(revision, edit.segment_ids[0])
    second = _find_segment(revision, edit.segment_ids[1])
    if first.source_id != second.source_id:
        raise ReviewEditError(
            f"병합·경계는 같은 원본 안에서만 가능하다: {first.source_id!r} vs {second.source_id!r}"
        )
    if first.start_pts > second.start_pts:
        first, second = second, first
    return first, second


def _apply_merge(revision: ReviewRevision, edit: ReviewEdit) -> tuple[ReviewSegment, ...]:
    left, right = _chronological_pair(revision, edit)
    if left.end_pts != right.start_pts:
        raise ReviewEditError(
            f"병합은 맞닿은 조각에만 가능하다: {left.segment_id!r}의 끝 {left.end_pts}와 "
            f"{right.segment_id!r}의 시작 {right.start_pts}가 다르다"
        )
    merged = ReviewSegment(
        segment_id=left.segment_id,
        source_id=left.source_id,
        source_order=left.source_order,
        segment_order=left.segment_order,
        start_pts=left.start_pts,
        end_pts=right.end_pts,
        speech_status=max(
            (left.speech_status, right.speech_status), key=lambda status: _SEVERITY.index(status)
        ),
        visual_tags=_union_tags(left.visual_tags, right.visual_tags),
        recommendation=max(
            (left.recommendation, right.recommendation),
            key=lambda grade: _RECOMMENDATION_RANK[grade],
        ),
        override_recommendation=(
            left.override_recommendation
            if left.override_recommendation is not None
            else right.override_recommendation
        ),
        selection=(
            SELECTION_EXCLUDED
            if SELECTION_EXCLUDED in (left.selection, right.selection)
            else SELECTION_INCLUDED
        ),
        notes="\n".join(part for part in (left.notes, right.notes) if part),
    )
    rebuilt: list[ReviewSegment] = []
    for existing in revision.segments:
        if existing.segment_id == right.segment_id:
            continue
        rebuilt.append(merged if existing.segment_id == left.segment_id else existing)
    return tuple(rebuilt)


def _union_tags(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    """태그 합집합 — 왼쪽 순서를 지키고 오른쪽의 새 태그만 뒤에 붙인다."""
    merged = list(left)
    for tag in right:
        if tag not in merged:
            merged.append(tag)
    return tuple(merged)


def _apply_boundary(revision: ReviewRevision, edit: ReviewEdit) -> tuple[ReviewSegment, ...]:
    left, right = _chronological_pair(revision, edit)
    if left.end_pts != right.start_pts:
        raise ReviewEditError(
            f"경계 조정은 맞닿은 조각에서만 가능하다: {left.end_pts} != {right.start_pts}"
        )
    boundary_pts = edit.boundary_pts
    if boundary_pts is None:
        raise ReviewEditError("경계 편집은 boundary_pts를 실어야 한다")
    clock = _required_clock(revision, left)
    _require_frame_point(clock, left.segment_id, boundary_pts)
    if not left.start_pts < boundary_pts < right.end_pts:
        raise ReviewEditError(
            f"새 경계 {boundary_pts}는 두 조각의 합친 내부 "
            f"[{left.start_pts}, {right.end_pts}) 엄격히 안이어야 한다"
        )
    if boundary_pts == left.end_pts:
        raise ReviewEditError(f"{boundary_pts}는 이미 이 경계다 — 바뀌는 것이 없다")
    moved = (
        replace(left, end_pts=boundary_pts),
        replace(right, start_pts=boundary_pts),
    )
    return tuple(
        moved[0]
        if existing.segment_id == left.segment_id
        else moved[1]
        if existing.segment_id == right.segment_id
        else existing
        for existing in revision.segments
    )


def _apply_metadata(revision: ReviewRevision, edit: ReviewEdit) -> tuple[ReviewSegment, ...]:
    (segment_id,) = edit.segment_ids
    segment = _find_segment(revision, segment_id)
    if edit.kind == "selection":
        if edit.selection not in SELECTIONS:
            raise ReviewEditError(f"알 수 없는 선택 {edit.selection!r}")
        updated = replace(
            segment,
            selection=edit.selection,
            visual_tags=confirm_tags(segment.visual_tags),
        )
    elif edit.kind == "override":
        if edit.override is not None and edit.override not in RECOMMENDATIONS:
            raise ReviewEditError(f"알 수 없는 덮기 등급 {edit.override!r}")
        updated = replace(segment, override_recommendation=edit.override)
    else:
        updated = replace(segment, notes=edit.notes if edit.notes is not None else "")
    return tuple(
        updated if existing.segment_id == segment_id else existing for existing in revision.segments
    )


def confirm_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    """미검토 표지만 뺀다 — 사람이 그 조각의 사용/제외를 확정했다는 기록이다."""
    return tuple(tag for tag in tags if tag != AUTO_EXCLUDED_UNREVIEWED_TAG)


def is_unreviewed_auto_exclusion(segment: ReviewSegment) -> bool:
    """제외된 채 표지가 남은 조각 — 포함된 조각은 영상에서 아무것도 지우지 않는다."""
    return (
        segment.selection == SELECTION_EXCLUDED
        and AUTO_EXCLUDED_UNREVIEWED_TAG in segment.visual_tags
    )


def unreviewed_auto_exclusions(revision: ReviewRevision) -> tuple[ReviewSegment, ...]:
    """사람이 아직 확인하지 않은 자동 제외 — 있으면 잘라 내는 확정 내보내기가 닫힌다(D35·D56)."""
    return tuple(s for s in chronological(revision) if is_unreviewed_auto_exclusion(s))


def split_segment(revision: ReviewRevision, segment_id: str, split_pts: int) -> ReviewRevision:
    """조각을 둘로 나눈다 — 나눈 점은 프레임 경계여야 하고 덮개는 그대로 온전하다."""
    return apply_edit(revision, split_edit(revision.revision_id, segment_id, split_pts))


def merge_segments(
    revision: ReviewRevision, first_segment_id: str, second_segment_id: str
) -> ReviewRevision:
    """같은 원본의 맞닿은 두 조각을 합친다 — 건너뛰기·다른 원본은 거부다."""
    return apply_edit(
        revision, merge_edit(revision.revision_id, first_segment_id, second_segment_id)
    )


def adjust_boundary(
    revision: ReviewRevision, left_segment_id: str, right_segment_id: str, boundary_pts: int
) -> ReviewRevision:
    """맞닿은 두 조각의 공유 경계를 옮긴다 — 양쪽 끝이 한 리비전에서 함께 움직인다."""
    return apply_edit(
        revision,
        boundary_edit(revision.revision_id, left_segment_id, right_segment_id, boundary_pts),
    )


def set_selection(revision: ReviewRevision, segment_id: str, selection: str) -> ReviewRevision:
    """사용/제외 표시를 바꾼다 — 조각은 하나도 사라지지 않는다(§6.4)."""
    return apply_edit(revision, selection_edit(revision.revision_id, segment_id, selection))


def set_override(revision: ReviewRevision, segment_id: str, override: str | None) -> ReviewRevision:
    """기본 등급의 사용자 덮기를 기록한다."""
    return apply_edit(revision, override_edit(revision.revision_id, segment_id, override))


def set_notes(revision: ReviewRevision, segment_id: str, notes: str) -> ReviewRevision:
    """검토 메모를 기록한다."""
    return apply_edit(revision, note_edit(revision.revision_id, segment_id, notes))


# ─────────────────────────────────────────────────────────────
#  시간순 질의 — 관련도가 순서를 바꾸지 않는다(§12 화면 2)
# ─────────────────────────────────────────────────────────────


def chronological(revision: ReviewRevision) -> tuple[ReviewSegment, ...]:
    """전체 조각을 확정 순서 키로 정렬한다 — (source_order, start_pts, segment_order)(§6.2)."""
    return tuple(
        sorted(
            revision.segments,
            key=lambda segment: (segment.source_order, segment.start_pts, segment.segment_order),
        )
    )


def filter_segments(
    revision: ReviewRevision,
    *,
    query: str | None = None,
    tags: Sequence[str] = (),
    selection: str | None = None,
) -> tuple[ReviewSegment, ...]:
    """검색·태그·선택 필터 — 결과는 늘 시간순다. 관련도 점수는 순서를 못 바꾼다."""
    needle = query.casefold() if query else ""
    matched = []
    for segment in chronological(revision):
        if selection is not None and segment.selection != selection:
            continue
        if any(tag not in segment.visual_tags for tag in tags):
            continue
        if needle:
            haystack = "\n".join((segment.segment_id, segment.notes, *segment.visual_tags))
            if needle not in haystack.casefold():
                continue
        matched.append(segment)
    return tuple(matched)


def previous_segment(revision: ReviewRevision, segment_id: str) -> ReviewSegment | None:
    """시간순 이전 조각 — 첫 조각이면 None."""
    return _neighbor(revision, segment_id, -1)


def next_segment(revision: ReviewRevision, segment_id: str) -> ReviewSegment | None:
    """시간순 다음 조각 — 마지막 조각이면 None."""
    return _neighbor(revision, segment_id, 1)


def _neighbor(revision: ReviewRevision, segment_id: str, step: int) -> ReviewSegment | None:
    ordered = chronological(revision)
    for position, segment in enumerate(ordered):
        if segment.segment_id == segment_id:
            neighbor = position + step
            if 0 <= neighbor < len(ordered):
                return ordered[neighbor]
            return None
    raise ReviewEditError(f"리비전 {revision.revision_id!r}에 조각 {segment_id!r}가 없다")


# ─────────────────────────────────────────────────────────────
#  재추출 영향 — 경계가 움직인 조각만 다시 뽑는다
# ─────────────────────────────────────────────────────────────


def reextract_segment_ids(previous: ReviewRevision, current: ReviewRevision) -> frozenset[str]:
    """경계 변화로 다시 뽑아야 할 조각 ID — 메타데이터·선택만 바뀐 조각은 없다.

    비교는 원본별 **core 범위 집합**으로 한다: 새 리비전의 조각 가운데 그 범위가
    이전 리비전에 없는 것이 정확히 영향받은 조각이다. 분할이면 두 자식, 병합이면
    합쳐진 조각, 경계 조정이면 양쪽 둘 — 선택·메모·등급 덮기는 빈 집합이다.
    신원(프로젝트·계획·원본 해시)이 다른 리비전 사이의 비교는 거부한다.
    """
    _require_same_identity(previous, current)
    old_ranges: dict[str, set[tuple[int, int]]] = {}
    for segment in previous.segments:
        old_ranges.setdefault(segment.source_id, set()).add((segment.start_pts, segment.end_pts))
    return frozenset(
        segment.segment_id
        for segment in current.segments
        if (segment.start_pts, segment.end_pts) not in old_ranges.get(segment.source_id, set())
    )


def _require_same_identity(previous: ReviewRevision, current: ReviewRevision) -> None:
    if (previous.project_id, previous.base_plan_id, previous.source_hashes) != (
        current.project_id,
        current.base_plan_id,
        current.source_hashes,
    ):
        raise ReviewValidationError(
            f"리비전 신원이 다르다: {previous.revision_id!r} vs {current.revision_id!r} — "
            "다른 프로젝트·계획·원본은 비교할 수 없다"
        )


# ─────────────────────────────────────────────────────────────
#  직렬화 — 정준 JSON, 엄격한 재독기
# ─────────────────────────────────────────────────────────────


def serialize_revision(revision: ReviewRevision) -> str:
    """리비전을 정준 JSON으로 — 정렬된 키·UTF-8·결정적 바이트. 보기 상태는 없다."""
    document = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "project_id": revision.project_id,
        "base_plan_id": revision.base_plan_id,
        "sequence": revision.sequence,
        "parent_revision_id": revision.parent_revision_id,
        "edit": None if revision.edit is None else _edit_document(revision.edit),
        "clocks": [_clock_document(clock) for clock in revision.clocks],
        "segments": [_segment_document(segment) for segment in revision.segments],
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _clock_document(clock: SourceClock) -> dict[str, object]:
    return {
        "source_id": clock.source_id,
        "source_sha256": clock.source_sha256,
        "time_base_num": clock.time_base_num,
        "time_base_den": clock.time_base_den,
        "origin_pts": clock.origin_pts,
        "end_pts": clock.end_pts,
        "frame_ticks": clock.frame_ticks,
    }


def _segment_document(segment: ReviewSegment) -> dict[str, object]:
    return {
        "segment_id": segment.segment_id,
        "source_id": segment.source_id,
        "source_order": segment.source_order,
        "segment_order": segment.segment_order,
        "start_pts": segment.start_pts,
        "end_pts": segment.end_pts,
        "speech_status": segment.speech_status,
        "visual_tags": list(segment.visual_tags),
        "recommendation": segment.recommendation,
        "override_recommendation": segment.override_recommendation,
        "selection": segment.selection,
        "notes": segment.notes,
    }


def _edit_document(edit: ReviewEdit) -> dict[str, object]:
    return {
        "kind": edit.kind,
        "target_revision_id": edit.target_revision_id,
        "segment_ids": list(edit.segment_ids),
        "split_pts": edit.split_pts,
        "boundary_pts": edit.boundary_pts,
        "selection": edit.selection,
        "override": edit.override,
        "notes": edit.notes,
    }


def parse_revision(text: str) -> ReviewRevision:
    """정준 문서를 리비전으로 되돌린다 — 모르는 스키마·타입·범위는 못 들어온다.

    되돌린 리비전은 구성 검증을 그대로 통과해야 한다(덮개·격자·순번·어휘),
    그래서 `serialize_revision`과 왕복이 정확히 같다.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReviewParseError(f"JSON이 아니다: {error}") from error
    if not isinstance(document, dict):
        raise ReviewParseError("문서 최상위는 객체여야 한다")
    expected = {
        "schema_version",
        "project_id",
        "base_plan_id",
        "sequence",
        "parent_revision_id",
        "edit",
        "clocks",
        "segments",
    }
    _require_keys(document, expected, "문서")
    if document["schema_version"] != REVIEW_SCHEMA_VERSION:
        raise ReviewParseError(
            f"모르는 스키마 {document['schema_version']!r} — {REVIEW_SCHEMA_VERSION!r}만 읽는다"
        )
    try:
        return _revision_from_document(document)
    except ReviewValidationError as error:
        raise ReviewParseError(f"범위 밖 값: {error}") from error


def _revision_from_document(document: dict[str, object]) -> ReviewRevision:
    clocks = tuple(
        _clock_from_document(item) for item in _require_list(document["clocks"], "clocks")
    )
    segments = tuple(
        _segment_from_document(item) for item in _require_list(document["segments"], "segments")
    )
    edit_value = document["edit"]
    edit = None if edit_value is None else _edit_from_document(_require_dict(edit_value, "edit"))
    parent = document["parent_revision_id"]
    if parent is not None and not isinstance(parent, str):
        raise ReviewParseError("parent_revision_id는 문자열 또는 null이어야 한다")
    return _validated(
        _require_str(document["project_id"], "project_id"),
        _require_str(document["base_plan_id"], "base_plan_id"),
        _require_int(document["sequence"], "sequence"),
        clocks,
        segments,
        parent_revision_id=parent,
        edit=edit,
    )


def _clock_from_document(item: object) -> SourceClock:
    values = _require_dict(item, "clock")
    _require_keys(values, set(_clock_document(SourceClock("", "", 1, 1, 0, 1, 1))), "clock")
    return SourceClock(
        source_id=_require_str(values["source_id"], "clock.source_id"),
        source_sha256=_require_str(values["source_sha256"], "clock.source_sha256"),
        time_base_num=_require_int(values["time_base_num"], "clock.time_base_num"),
        time_base_den=_require_int(values["time_base_den"], "clock.time_base_den"),
        origin_pts=_require_int(values["origin_pts"], "clock.origin_pts"),
        end_pts=_require_int(values["end_pts"], "clock.end_pts"),
        frame_ticks=_require_int(values["frame_ticks"], "clock.frame_ticks"),
    )


def _segment_from_document(item: object) -> ReviewSegment:
    values = _require_dict(item, "segment")
    _require_keys(values, set(_segment_document(_blank_segment())), "segment")
    override = values["override_recommendation"]
    if override is not None and not isinstance(override, str):
        raise ReviewParseError("segment.override_recommendation은 문자열 또는 null이어야 한다")
    tags = _require_list(values["visual_tags"], "segment.visual_tags")
    return ReviewSegment(
        segment_id=_require_str(values["segment_id"], "segment.segment_id"),
        source_id=_require_str(values["source_id"], "segment.source_id"),
        source_order=_require_int(values["source_order"], "segment.source_order"),
        segment_order=_require_int(values["segment_order"], "segment.segment_order"),
        start_pts=_require_int(values["start_pts"], "segment.start_pts"),
        end_pts=_require_int(values["end_pts"], "segment.end_pts"),
        speech_status=_require_str(values["speech_status"], "segment.speech_status"),
        visual_tags=tuple(_require_str(tag, "segment.visual_tags[]") for tag in tags),
        recommendation=_require_str(values["recommendation"], "segment.recommendation"),
        override_recommendation=override,
        selection=_require_str(values["selection"], "segment.selection"),
        notes=_require_str(values["notes"], "segment.notes"),
    )


def _blank_segment() -> ReviewSegment:
    return ReviewSegment(
        segment_id="",
        source_id="",
        source_order=0,
        segment_order=0,
        start_pts=0,
        end_pts=0,
        speech_status=SPEECH_UNCONFIRMED_EMPTY,
        visual_tags=(),
        recommendation="normal",
        override_recommendation=None,
        selection=SELECTION_INCLUDED,
        notes="",
    )


def _edit_from_document(item: object) -> ReviewEdit:
    values = _require_dict(item, "edit")
    _require_keys(values, set(_edit_document(ReviewEdit("", "", ()))), "edit")
    return ReviewEdit(
        kind=_require_str(values["kind"], "edit.kind"),
        target_revision_id=_require_str(values["target_revision_id"], "edit.target_revision_id"),
        segment_ids=tuple(
            _require_str(segment_id, "edit.segment_ids[]")
            for segment_id in _require_list(values["segment_ids"], "edit.segment_ids")
        ),
        split_pts=_optional_int(values["split_pts"], "edit.split_pts"),
        boundary_pts=_optional_int(values["boundary_pts"], "edit.boundary_pts"),
        selection=_optional_str(values["selection"], "edit.selection"),
        override=_optional_str(values["override"], "edit.override"),
        notes=_optional_str(values["notes"], "edit.notes"),
    )


def _optional_int(item: object, where: str) -> int | None:
    if item is None:
        return None
    return _require_int(item, where)


def _optional_str(item: object, where: str) -> str | None:
    if item is None:
        return None
    return _require_str(item, where)


def _require_keys(values: dict[str, object], expected: set[str], where: str) -> None:
    unknown = sorted(set(values) - expected)
    if unknown:
        raise ReviewParseError(f"{where}에 모르는 키가 있다: {unknown}")
    missing = sorted(expected - set(values))
    if missing:
        raise ReviewParseError(f"{where}에 없는 키가 있다: {missing}")


def _require_dict(item: object, where: str) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ReviewParseError(f"{where}는 객체여야 한다")
    return item


def _require_list(item: object, where: str) -> list[object]:
    if not isinstance(item, list):
        raise ReviewParseError(f"{where}는 배열이어야 한다")
    return item


def _require_str(item: object, where: str) -> str:
    if not isinstance(item, str):
        raise ReviewParseError(f"{where}는 문자열이어야 한다")
    return item


def _require_int(item: object, where: str) -> int:
    if isinstance(item, bool) or not isinstance(item, int):
        raise ReviewParseError(f"{where}는 정수여야 한다")
    return item


def content_fingerprint(revision: ReviewRevision) -> str:
    """리비전 내용의 지문 — 정준 직렬화의 SHA-256. 보기 상태는 애초에 없다(§10.3)."""
    return hashlib.sha256(serialize_revision(revision).encode("utf-8")).hexdigest()
