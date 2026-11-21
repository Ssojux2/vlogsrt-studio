"""추천만 이어붙인 초안의 순수 계획 + SRT 투영(W-44a, D100 §5a).

입력은 값뿐이다: 원본마다 프레임 시계·등급 구간(`TierPiece`)·검토 머리의 사람 표시(`SegmentMark`)·
원본 자막(`SourceCue`). 같은 입력이면 같은 계획이 나온다 — 작업대 레일과 ffmpeg 렌더러가 **이 한
계획**을 함께 읽으므로 화면에 보인 것이 곧 출력이다. 파일·프로세스·서비스·작업공간을 모른다
(`scripts/check_invariants.py`의 `check_render_plan_stays_pure`).

남길 범위(사용자 규칙):

- 초안 = 녹색 등급 구간. 사람의 `추천` 덮기는 그 범위 전체를 더하고, 사람의 `보통` 덮기는 그 안의
  녹색을 빼고, 사람이 정한 제외(`selection = excluded`, 미검토 표지 없음)는 그 안의 전부를 뺀다 —
  사람 표시가 자동 등급을 이긴다. **미검토 자동 제외는 사람 표시가 아니다**: 빨강 위의 제안일
  뿐이라 녹색을 빼지 않는다(Final Cut 보내기 D80·D93과 같은 판단).
- 맞닿은 남길 조각은 하나로 합치고, 합칠 틈(`merge_gap_seconds`) 안의 자동 노랑 틈은 잇는다.
  조각 끝은 원본 프레임 격자 **안쪽**으로 붙이고(1프레임 미만 자투리는 이웃한 빠진 구간에 붙는다),
  최소 길이보다 짧은 조각은 빼서 `too_short`로 센다.
- VFR(map 격자) 원본은 통째로 빼고 `unsupported_grid`로 남긴다(D68). 렌더러가 거부한
  원본(`refusal`)도 통째로 빼고 `unrenderable`과 까닭 글로 남긴다 — 원본 하나가 초안 전체를
  막지 않는다(D113).

**조용히 사라지는 구간은 없다(규칙 9·D35).** 원본마다 남긴 조각과 뺀 구간(`dropped` — 까닭과 검토
상태)이 `[0, extent]`를 겹침·틈 없이 덮는다 — 어기면 계획을 만들지 않는다. 확인하지 않은 빨강도
내보내기를 막지 않는다(사용자 결정 2026-10-27 — D94의 거부와 다른 선택이다). 뺀 목록과
`unreviewed_red_dropped` 수로 알린다.

**시각은 원본 시계에서만 온다(규칙 1·3).** 조각의 출력 위치는 앞 조각 길이의 누적이고, 시퀀스 격자에
맞출 때는 누적 경계를 가장 가까운 시퀀스 프레임에 맞춰 오차가 쌓이지 않는다(D71 `_sequence_layout`).
원본 끝에 닿은 조각만 남은 미디어로 내린다. 레이트가 다른 조각은 출력 길이가 원본 범위와 1프레임
미만 다를 수 있다 — 렌더러는 `[start, end)` 밖을 읽지 않고 마지막 프레임을 붙잡아 채운다.
자막은 조각에 겹치는 큐만 조각으로 자르고 그만큼 옮긴다. 밀리초 반올림 뒤 길이 0인 큐만 시작은 두고
끝을 `refine`의 `min_duration`까지 늘린다 — 다음(더 늦게 시작하는) 큐 앞 `min_gap`과 출력 끝에서
멈추고, 늘린 장은 센다(D115 — Final Cut은 길이 0인 큐에서 SRT 읽기를 멈춘다). 글자는 Final Cut
보내기와 같은 조판 정리(D96 — 줄마다 `polish_text`)를 지난다.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from typing import Any, TypeVar

from vlogsrt import polish
from vlogsrt.config import Config
from vlogsrt.project.raw.config import (
    FRAME_RATE_HIGHER,
    ORIENTATION_LANDSCAPE,
    ORIENTATION_PORTRAIT,
    RENDER_SETTINGS,
    RenderSettings,
)
from vlogsrt.project.raw.segment_review import (
    RECOMMENDATIONS,
    RECOMMENDED,
    SELECTION_EXCLUDED,
    SELECTIONS,
    ReviewSegment,
    SourceClock,
    is_unreviewed_auto_exclusion,
)
from vlogsrt.project.raw.tiering import TIER_GREEN, TIER_RED, TIER_YELLOW, TIERS, TierReport
from vlogsrt.project.timebase import format_srt_time, milliseconds_half_up

GRID_UNIFORM = "uniform"
GRID_SNAP = "snap"
GRID_MAP = "map"
GRID_KINDS = (GRID_UNIFORM, GRID_SNAP, GRID_MAP)
"""원본 격자 종류 — `preprocess`의 `conform_kind` 철자 그대로(D69·D70). map은 렌더하지 않는다."""

PART_AUTO_GREEN = "auto_green"
PART_HUMAN_RECOMMENDED = "human_recommended"
PART_NORMAL = "normal"
"""남긴 보통 자리(자동 노랑 · 사람 「보통」, D186) — 묶음의 까닭은 사람 추천이 없으면
`auto_green`이다."""
PART_TIMELINE = "timeline"
"""원본 범위 타임라인의 클립(W-71e, D130) — 등급이 아니라 사람이 고른 범위다."""

DROP_YELLOW = "yellow"
DROP_RED_UNREVIEWED = "red_unreviewed"
DROP_RED_CONFIRMED = "red_confirmed"
DROP_HUMAN_NORMAL = "human_normal"
DROP_HUMAN_EXCLUDED = "human_excluded"
DROP_TOO_SHORT = "too_short"
DROP_UNSUPPORTED_GRID = "unsupported_grid"
DROP_UNANALYZED = "unanalyzed"
DROP_UNRENDERABLE = "unrenderable"
DROP_TIMELINE_CUT = "timeline_cut"
DROP_REASONS = (
    DROP_YELLOW,
    DROP_RED_UNREVIEWED,
    DROP_RED_CONFIRMED,
    DROP_HUMAN_NORMAL,
    DROP_HUMAN_EXCLUDED,
    DROP_TOO_SHORT,
    DROP_UNSUPPORTED_GRID,
    DROP_UNANALYZED,
    DROP_UNRENDERABLE,
    DROP_TIMELINE_CUT,
)
"""뺀 구간의 까닭 — 빨강은 사람이 그 조각을 본 기록(확정 제외·덮기·결정)이 있으면 `red_confirmed`,
없으면 `red_unreviewed`다. `unanalyzed`는 등급 판정이 없는 자리(분석 전 원본 · 판정 길이 밖)다.
`timeline_cut`은 원본 범위 타임라인에서 클립 밖에 남은 원본 구간이다(D130)."""
_BRIDGEABLE = frozenset({DROP_YELLOW})
"""합칠 틈으로 이을 수 있는 까닭 — 사람 표시·빨강·미분석은 잇지 않는다."""

REVIEW_HUMAN = "human"
REVIEW_AUTO_EXCLUDED = "auto_excluded_unreviewed"
REVIEW_UNREVIEWED = "unreviewed"
REVIEW_NONE = "no_review"
REVIEW_STATES = (REVIEW_HUMAN, REVIEW_AUTO_EXCLUDED, REVIEW_UNREVIEWED, REVIEW_NONE)
"""그 자리 검토 조각의 상태 — 사람 결정 · 미검토 자동 제외 · 결정 없음 · 검토 조각 없음."""

_OVERRIDE_NORMAL = "normal"
_Key = TypeVar("_Key")
_HALF = Fraction(1, 2)


class RenderPlanError(ValueError):
    """계획을 만들 수 없는 입력 — 어느 원본의 무엇이 문제인지 말한다."""


# ── 입력 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FrameClock:
    """원본 하나의 프레임 시계 — 공통 원본 시계(원점 0)의 격자 `k × frame_duration`.

    `origin`은 컨테이너의 재생 원점(초)이다 — 렌더러가 찾아갈 때만 더한다. 계획의 모든 시각은
    원점 0의 공통 시계(등급 보고서·검토 조각·원본 자막과 같은 원점)다.
    """

    frame_duration: Fraction
    frame_count: int
    origin: Fraction = Fraction(0)

    @property
    def extent(self) -> Fraction:
        return self.frame_count * self.frame_duration


@dataclass(frozen=True, slots=True)
class TierPiece:
    """등급 구간 하나 — `tiering.TierSpan`의 값만(`tier_pieces`가 옮긴다)."""

    start: Fraction
    end: Fraction
    tier: str
    red_reason: str | None = None
    hold: bool = False
    caps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SegmentMark:
    """검토 머리 조각 하나의 사람 표시 — 원본 시계 초(`segment_mark`가 옮긴다).

    `decided`는 조각에 사람 결정이 실렸는가(`review_workspace.human_decided`, D82)다 — 모으는
    쪽이 값으로 넘긴다.
    """

    start: Fraction
    end: Fraction
    selection: str
    override: str | None = None
    unreviewed_auto_excluded: bool = False
    decided: bool = False

    @property
    def confirmed_exclusion(self) -> bool:
        return self.selection == SELECTION_EXCLUDED and not self.unreviewed_auto_excluded

    @property
    def human(self) -> bool:
        return self.confirmed_exclusion or self.override is not None or self.decided


@dataclass(frozen=True, slots=True)
class SourceCue:
    """원본 자막 줄 하나 — 원본 시계 초, 글자는 저장된 그대로(정리는 투영이 한다)."""

    utterance_id: str
    start: Fraction
    end: Fraction
    text: str


@dataclass(frozen=True, slots=True)
class RenderSource:
    """원본 하나 — 촬영 시각 순서로 넘긴다. `tiers`가 None이면 분석 전, `segments`가 None이면
    검토 머리에 없다. `width`·`height`는 화면에 보이는 기하(회전 반영)다."""

    source_id: str
    name: str
    clock: FrameClock
    width: int
    height: int
    grid: str
    tiers: tuple[TierPiece, ...] | None = None
    segments: tuple[SegmentMark, ...] | None = None
    cues: tuple[SourceCue, ...] = ()
    refusal: str = ""
    """렌더러가 이 원본을 거부한 까닭(D113) — 비어 있지 않으면 map처럼 통째로 뺀다."""


def excluded_reason(source: RenderSource) -> str | None:
    """원본을 통째로 뺄 까닭 — map 격자(D68)·렌더 거부(D113), 아니면 None."""
    if source.grid == GRID_MAP:
        return DROP_UNSUPPORTED_GRID
    if source.refusal:
        return DROP_UNRENDERABLE
    return None


# ── 계획 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RenderPart:
    """남긴 조각 하나 — 원본 `[start_frame, end_frame)`과 출력
    `[output_start_frame, output_start_frame + output_frames)`.

    `start`·`end`는 원본 시계 초(프레임 격자 위), 출력 시각은 시퀀스 프레임 × 시퀀스 프레임 길이다.
    `scale`은 크기가 시퀀스와 다르다, `pad`는 화면비가 달라 빈 칸이 생긴다, `fps_convert`는 레이트가
    시퀀스와 다르다는 뜻이다. `bridged_seconds`는 합칠 틈으로 이은 자동 노랑의 길이다.
    """

    index: int
    source_id: str
    start_frame: int
    end_frame: int
    start: Fraction
    end: Fraction
    output_start_frame: int
    output_frames: int
    output_start: Fraction
    output_end: Fraction
    reason: str
    scale: bool
    pad: bool
    fps_convert: bool
    bridged_seconds: Fraction = Fraction(0)


@dataclass(frozen=True, slots=True)
class KeptRange:
    """원본 하나의 남길 조각(원점 기준 초, 원본 프레임 격자 위) — 출력 위치는 없다(W-46b).

    `reason`은 `auto_green`·`human_recommended`다. 작업대 레일의 「내보낼 부분」 띠가 이것을
    그린다 — `build_plan`과 같은 판정·합치기·격자·최소 길이라 화면에 보인 것이 곧 출력이다."""

    start: Fraction
    end: Fraction
    reason: str


@dataclass(frozen=True, slots=True)
class DroppedRange:
    """뺀 구간 하나 — 까닭·검토 상태·그 자리의 등급(없으면 None)·판정 보류."""

    source_id: str
    start: Fraction
    end: Fraction
    reason: str
    review_state: str
    tier: str | None
    hold: bool = False


@dataclass(frozen=True, slots=True)
class SequenceFormat:
    orientation: str
    width: int
    height: int
    frame_duration: Fraction


@dataclass(frozen=True, slots=True)
class ProjectedCue:
    """출력 시계로 옮긴 자막 한 장 — 조각 하나 안에 있다(`clipped`는 조각·출력 끝에서 잘렸다)."""

    utterance_id: str
    source_id: str
    part_index: int
    start: Fraction
    end: Fraction
    text: str
    clipped: bool


@dataclass(frozen=True, slots=True)
class CueCounts:
    """자막 투영에서 센 것 — `source = kept + dropped + empty`, 쓴 장 수 = `kept + split`.

    `split`은 빠진 틈을 넘어 더 생긴 장 수, `clipped`는 잘린 장 수, `inverted`는 끝이 시작보다
    앞이라 시작 한 점으로 본 줄 수다. `lengthened`는 밀리초 반올림 뒤 길이 0이라 끝을 최소 길이
    쪽으로 늘린 장 수(D115), `zero_length`는 늘릴 자리가 없어(출력 끝·바로 뒤 큐) 길이 0으로 남은
    장 수다.
    """

    source: int = 0
    kept: int = 0
    clipped: int = 0
    split: int = 0
    dropped: int = 0
    empty: int = 0
    inverted: int = 0
    zero_length: int = 0
    lengthened: int = 0


@dataclass(frozen=True, slots=True)
class UnsupportedSource:
    source_id: str
    name: str
    reason: str
    detail: str = ""
    """사람이 읽는 까닭 글 — 렌더 거부(D113)만 싣는다. 비면 보고서에 칸을 쓰지 않는다."""


@dataclass(frozen=True, slots=True)
class RenderPlan:
    parts: tuple[RenderPart, ...]
    dropped: tuple[DroppedRange, ...]
    sequence: SequenceFormat | None
    cues: tuple[ProjectedCue, ...]
    cue_counts: CueCounts
    unsupported: tuple[UnsupportedSource, ...]
    total_frames: int
    padded: int
    """시퀀스 1프레임이 안 돼 1프레임으로 늘린 조각 수 — 고치지 않고 센다."""
    too_short: int
    """최소 길이보다 짧아 뺀 조각 수."""
    settings: RenderSettings = RENDER_SETTINGS

    @property
    def total_seconds(self) -> Fraction:
        if self.sequence is None:
            return Fraction(0)
        return self.total_frames * self.sequence.frame_duration

    @property
    def unreviewed_red_dropped(self) -> int:
        """사람이 보지 않은 빨강을 뺀 구간 수 — 막지 않고 화면 경고로만 알린다."""
        return sum(1 for row in self.dropped if row.reason == DROP_RED_UNREVIEWED)


# ── 변환 ─────────────────────────────────────────────────────────────────


def frame_clock(clock: SourceClock) -> FrameClock:
    """검토 시계 → 프레임 시계. 원점은 정규화된 재생 원점(초)이다."""
    frames, remainder = divmod(clock.end_pts - clock.origin_pts, clock.frame_ticks)
    if remainder:
        raise RenderPlanError(f"{clock.source_id}: 검토 시계 끝이 프레임 격자 밖이다")
    return FrameClock(
        clock.frame_ticks * clock.time_base, frames, clock.origin_pts * clock.time_base
    )


def segment_mark(segment: ReviewSegment, clock: SourceClock, *, decided: bool) -> SegmentMark:
    """검토 조각 → 원본 시계 초의 사람 표시. `decided`는 모으는 쪽이 `human_decided`로 정한다."""
    return SegmentMark(
        start=(segment.start_pts - clock.origin_pts) * clock.time_base,
        end=(segment.end_pts - clock.origin_pts) * clock.time_base,
        selection=segment.selection,
        override=segment.override_recommendation,
        unreviewed_auto_excluded=is_unreviewed_auto_exclusion(segment),
        decided=decided,
    )


def tier_pieces(report: TierReport) -> tuple[TierPiece, ...]:
    """등급 보고서 → 등급 구간 값. 판정 보류·추천 제한 코드를 그대로 옮긴다."""
    return tuple(
        TierPiece(
            span.start,
            span.end,
            span.tier,
            span.red_reason,
            span.hold,
            tuple(code for code, _ in span.caps),
        )
        for span in report.spans
    )


def caption_text(text: str, config: Config | None = None) -> str:
    """내보내는 자막 글자 — `finalcut_export.caption_text`와 같은 규칙(D30·D96): 줄마다 조판
    정리, 빈 줄은 빼고, 다 지워져 비면 원문 그대로다(규칙 9). 그 모듈은 파일 잠금을 import하므로
    여기서 같은 두 줄을 쓴다 — 시험이 둘이 같음을 못 박는다."""
    settings = config or Config()
    lines = [polish.polish_text(line, settings) for line in text.splitlines()]
    sent = "\n".join(line for line in lines if line)
    return sent or text


# ── 입력 검사 ─────────────────────────────────────────────────────────────


def _refuse(source: RenderSource, reason: str) -> RenderPlanError:
    return RenderPlanError(f"{source.name}: {reason}")


def _require_source(source: RenderSource) -> None:
    if source.width <= 0 or source.height <= 0:
        raise _refuse(source, f"화면 크기가 양수가 아니다: {source.width}x{source.height}")
    _require_layout(source)


def _require_layout(source: RenderSource) -> None:
    """남길 범위를 정하는 재료의 검사 — 화면 크기는 보지 않는다(포맷 투표만 쓴다)."""
    clock = source.clock
    if clock.frame_duration <= 0 or clock.frame_count <= 0:
        raise _refuse(source, "프레임 길이와 프레임 수는 양수여야 한다")
    if source.grid not in GRID_KINDS:
        raise _refuse(source, f"모르는 격자 종류: {source.grid!r}")
    for piece in source.tiers or ():
        if piece.tier not in TIERS:
            raise _refuse(source, f"모르는 등급: {piece.tier!r}")
    _require_ordered(source, source.tiers or (), "등급 구간")
    _require_ordered(source, source.segments or (), "검토 조각")
    for mark in source.segments or ():
        if mark.selection not in SELECTIONS:
            raise _refuse(source, f"모르는 선택: {mark.selection!r}")
        if mark.override is not None and mark.override not in RECOMMENDATIONS:
            raise _refuse(source, f"모르는 덮기: {mark.override!r}")
        if mark.unreviewed_auto_excluded and mark.selection != SELECTION_EXCLUDED:
            raise _refuse(source, "미검토 표지가 제외되지 않은 조각에 붙었다")


def _require_ordered(
    source: RenderSource, rows: Sequence[TierPiece | SegmentMark], label: str
) -> None:
    previous = Fraction(0)
    for row in rows:
        if row.start < previous or row.end <= row.start:
            raise _refuse(source, f"{label}이 겹치거나 뒤집혔다({row.start}–{row.end}초)")
        previous = row.end


def _require_unique(sources: Sequence[RenderSource]) -> None:
    seen: set[str] = set()
    for source in sources:
        if source.source_id in seen:
            raise _refuse(source, f"같은 원본이 두 번 들어왔다: {source.source_id}")
        seen.add(source.source_id)


# ── 원본 하나: 자리마다 판정 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Slot:
    """원본의 가장 작은 조각(등급 경계 × 검토 경계) 하나와 그 판정."""

    start: Fraction
    end: Fraction
    kept: bool
    reason: str
    review_state: str
    tier: str | None
    hold: bool
    bridged: bool = False


def _covering(rows: Sequence[TierPiece] | Sequence[SegmentMark], point: Fraction) -> int | None:
    starts = [row.start for row in rows]
    index = bisect.bisect_right(starts, point) - 1
    if index >= 0 and rows[index].start <= point < rows[index].end:
        return index
    return None


def _slots(source: RenderSource, keep_normal: bool) -> list[_Slot]:
    extent = source.clock.extent
    tiers, marks = source.tiers or (), source.segments or ()
    edges = {Fraction(0), extent}
    edges.update(edge for tier in tiers for edge in (tier.start, tier.end))
    edges.update(edge for mark in marks for edge in (mark.start, mark.end))
    ordered = sorted(edge for edge in edges if 0 <= edge <= extent)
    slots = []
    for start, end in zip(ordered, ordered[1:], strict=False):
        middle = (start + end) / 2
        tier_index, mark_index = _covering(tiers, middle), _covering(marks, middle)
        tier = tiers[tier_index] if tier_index is not None else None
        mark = marks[mark_index] if mark_index is not None else None
        slots.append(_judge(start, end, tier, mark, keep_normal))
    return slots


def _review_state(mark: SegmentMark | None) -> str:
    if mark is None:
        return REVIEW_NONE
    if mark.unreviewed_auto_excluded:
        return REVIEW_AUTO_EXCLUDED
    return REVIEW_HUMAN if mark.human else REVIEW_UNREVIEWED


def _judge(
    start: Fraction,
    end: Fraction,
    tier: TierPiece | None,
    mark: SegmentMark | None,
    keep_normal: bool,
) -> _Slot:
    """자리 하나의 판정 — 사람이 정한 제외 → 사람 추천 → 사람 보통 → 자동 등급 순서다.

    `keep_normal`(D186)이면 보통(사람 「보통」 · 판정 보류가 아닌 자동 노랑)도 남긴다."""
    level = tier.tier if tier is not None else None
    state = _review_state(mark)
    slot = _Slot(start, end, False, DROP_UNANALYZED, state, level, tier.hold if tier else False)
    if mark is not None and mark.confirmed_exclusion:
        return replace(
            slot, reason=DROP_RED_CONFIRMED if level == TIER_RED else DROP_HUMAN_EXCLUDED
        )
    if mark is not None and mark.override == RECOMMENDED:
        return replace(slot, kept=True, reason=PART_HUMAN_RECOMMENDED)
    if mark is not None and mark.override == _OVERRIDE_NORMAL:
        if keep_normal:
            return replace(slot, kept=True, reason=PART_NORMAL)
        return replace(slot, reason=DROP_HUMAN_NORMAL)
    if level == TIER_GREEN:
        return replace(slot, kept=True, reason=PART_AUTO_GREEN)
    if level == TIER_RED:
        seen = mark is not None and mark.human
        return replace(slot, reason=DROP_RED_CONFIRMED if seen else DROP_RED_UNREVIEWED)
    if level == TIER_YELLOW and keep_normal and not slot.hold:
        return replace(slot, kept=True, reason=PART_NORMAL)
    return replace(slot, reason=DROP_YELLOW) if level == TIER_YELLOW else slot


# ── 원본 하나: 합치기 · 격자 · 최소 길이 ────────────────────────────────────


def _runs(slots: list[_Slot]) -> list[tuple[int, int]]:
    """남길 자리가 이어진 묶음 — 자리 번호의 반개 구간."""
    runs: list[tuple[int, int]] = []
    for index, slot in enumerate(slots):
        if not slot.kept:
            continue
        if runs and runs[-1][1] == index:
            runs[-1] = (runs[-1][0], index + 1)
        else:
            runs.append((index, index + 1))
    return runs


def _bridge(
    slots: list[_Slot], runs: list[tuple[int, int]], gap: Fraction
) -> list[tuple[int, int]]:
    """틈이 `gap`보다 짧고 그 자리가 모두 자동 노랑이면 두 묶음을 잇는다(틈 자리는 `bridged`)."""
    merged: list[tuple[int, int]] = []
    for run in runs:
        if merged:
            between = slots[merged[-1][1] : run[0]]
            width = sum((slot.end - slot.start for slot in between), Fraction(0))
            if width < gap and all(slot.reason in _BRIDGEABLE for slot in between):
                for index in range(merged[-1][1], run[0]):
                    slots[index] = replace(slots[index], bridged=True)
                merged[-1] = (merged[-1][0], run[1])
                continue
        merged.append(run)
    return merged


@dataclass(frozen=True, slots=True)
class _Kept:
    """격자에 붙인 남길 조각 — 아직 출력 위치가 없다."""

    source: RenderSource
    start_frame: int
    end_frame: int
    reason: str
    bridged_seconds: Fraction

    @property
    def start(self) -> Fraction:
        return self.start_frame * self.source.clock.frame_duration

    @property
    def end(self) -> Fraction:
        return self.end_frame * self.source.clock.frame_duration


def _inward(start: Fraction, end: Fraction, fd: Fraction) -> tuple[int, int]:
    return math.ceil(start / fd), math.floor(end / fd)


def _ledger_slot(slot: _Slot, start: Fraction, end: Fraction) -> _Slot:
    return replace(slot, start=start, end=end, kept=False, bridged=False)


def _layout_source(
    source: RenderSource, settings: RenderSettings
) -> tuple[list[_Kept], list[_Slot], int]:
    """원본 하나 → 남길 조각, 뺀 자리(합치기 전), 짧아서 뺀 조각 수."""
    slots = _slots(source, settings.keep_normal)
    whole = excluded_reason(source)
    if whole is not None:
        return [], [replace(s, kept=False, reason=whole) for s in slots], 0
    runs = _bridge(slots, _runs(slots), settings.merge_gap_seconds)
    kept: list[_Kept] = []
    dropped: list[_Slot] = []
    too_short = 0
    cursor = 0
    for first, last in runs:
        dropped += slots[cursor:first]
        cursor = last
        piece = _keep_run(source, slots[first:last], settings)
        if piece is None:
            too_short += 1
            dropped += [_too_short(slot) for slot in slots[first:last]]
            continue
        kept.append(piece)
        dropped += _slivers(piece, slots, (first, last))
    dropped += slots[cursor:]
    return kept, dropped, too_short


def _too_short(slot: _Slot) -> _Slot:
    """짧아 뺀 조각의 자리 — 남길 자리는 `too_short`, 이은 노랑은 제 까닭 그대로다."""
    if slot.bridged:
        return replace(slot, bridged=False)
    return replace(slot, kept=False, reason=DROP_TOO_SHORT)


def _keep_run(source: RenderSource, run: list[_Slot], settings: RenderSettings) -> _Kept | None:
    fd = source.clock.frame_duration
    start_frame, end_frame = _inward(run[0].start, run[-1].end, fd)
    if end_frame <= start_frame or (end_frame - start_frame) * fd < settings.min_part_seconds:
        return None
    human = any(slot.reason == PART_HUMAN_RECOMMENDED for slot in run if not slot.bridged)
    bridged = sum((slot.end - slot.start for slot in run if slot.bridged), Fraction(0))
    reason = PART_HUMAN_RECOMMENDED if human else PART_AUTO_GREEN
    return _Kept(source, start_frame, end_frame, reason, bridged)


def _slivers(piece: _Kept, slots: list[_Slot], run: tuple[int, int]) -> list[_Slot]:
    """격자 안쪽으로 붙이며 남은 1프레임 미만 자투리 — 이웃한 빠진 자리의 판정을 그대로 받는다.

    묶음은 언제나 빠진 자리 사이에 있다(맞닿은 남길 자리는 한 묶음이다). 0과 `extent`는 격자
    위라 그쪽 자투리는 생기지 않는다.
    """
    first, last = run
    rows: list[_Slot] = []
    if piece.start > slots[first].start:
        rows.append(_ledger_slot(slots[first - 1], slots[first].start, piece.start))
    if piece.end < slots[last - 1].end:
        rows.append(_ledger_slot(slots[last], piece.end, slots[last - 1].end))
    return rows


def _ledger(source: RenderSource, slots: list[_Slot]) -> list[DroppedRange]:
    """뺀 자리 → 뺀 구간. 맞닿은 같은 판정은 하나로 합친다."""
    rows: list[DroppedRange] = []
    for slot in sorted(slots, key=lambda row: row.start):
        last = rows[-1] if rows else None
        key = (slot.reason, slot.review_state, slot.tier, slot.hold)
        if (
            last is not None
            and last.end == slot.start
            and key
            == (
                last.reason,
                last.review_state,
                last.tier,
                last.hold,
            )
        ):
            rows[-1] = replace(last, end=slot.end)
            continue
        rows.append(DroppedRange(source.source_id, slot.start, slot.end, *key))
    return rows


def _require_tiling(source: RenderSource, kept: list[_Kept], dropped: list[DroppedRange]) -> None:
    rows = sorted([(k.start, k.end) for k in kept] + [(d.start, d.end) for d in dropped])
    contiguous = all(a[1] == b[0] for a, b in zip(rows, rows[1:], strict=False))
    positive = all(start < end for start, end in rows)
    if not rows or rows[0][0] != 0 or rows[-1][1] != source.clock.extent:
        raise _refuse(source, "남긴 조각과 뺀 구간이 원본 처음과 끝에 닿지 않는다")
    if not (contiguous and positive):
        raise _refuse(source, "남긴 조각과 뺀 구간이 원본을 빈틈없이 덮지 못했다")


# ── 시퀀스 포맷 ───────────────────────────────────────────────────────────


def _orientation(source: RenderSource) -> str:
    return ORIENTATION_LANDSCAPE if source.width >= source.height else ORIENTATION_PORTRAIT


def _sequence(kept: list[_Kept], settings: RenderSettings) -> SequenceFormat | None:
    """방향(D66) → 그 안의 레이트(D71) → 그 방향의 가장 많이 남긴 크기. 표는 남긴 길이(초)다."""
    if not kept:
        return None
    sides = _tally((_orientation(k.source), k.end - k.start) for k in kept)
    side = _pick_side(sides, settings.timeline.orientation_tie)
    chosen = [k for k in kept if _orientation(k.source) == side]
    rates = _tally((k.source.clock.frame_duration, k.end - k.start) for k in chosen)
    most = max(rates.values())
    tied = [fd for fd, seconds in rates.items() if seconds == most]
    higher = settings.timeline.frame_rate_tie == FRAME_RATE_HIGHER
    rate = min(tied) if higher else max(tied)
    sizes = _tally(((k.source.width, k.source.height), k.end - k.start) for k in chosen)
    # 같은 길이면 큰 쪽 — 줄이는 쪽이 키우는 쪽보다 잃는 것이 없다.
    width, height = max(sizes, key=lambda size: (sizes[size], size[0] * size[1], size))
    return SequenceFormat(side, width, height, rate)


def _tally(rows: Iterable[tuple[_Key, Fraction]]) -> dict[_Key, Fraction]:
    totals: dict[_Key, Fraction] = {}
    for key, seconds in rows:
        totals[key] = totals.get(key, Fraction(0)) + seconds
    return totals


# ── 출력 타임라인 ─────────────────────────────────────────────────────────


def sequence_edges(
    pieces: Sequence[tuple[Fraction, bool]], sequence_fd: Fraction
) -> tuple[list[int], int]:
    """조각마다 출력 경계(시퀀스 프레임)와 1프레임으로 늘린 조각 수(D71 `_sequence_layout`).

    `pieces`는 차례대로 (원본 길이 초, 원본 끝에 닿았는가)다. 누적 길이를 가장 가까운 시퀀스
    프레임(½은 위로)에 맞추므로 오차가 쌓이지 않는다. 원본 끝에 닿은 조각만 남은 미디어를 넘지 않게
    내림이다. 두 경계가 한 프레임에 떨어지면 0프레임 조각은 쓸 수 없어 1프레임으로 늘리고 센다.
    렌더 계획과 원본 범위 타임라인(`timeline_edit`, D125)이 함께 쓰는 한 규칙이다.
    """
    edges = [0]
    padded = 0
    total = Fraction(0)
    for length, at_extent in pieces:
        total += length
        edge = math.floor(total / sequence_fd + _HALF)
        if at_extent:
            edge = min(edge, edges[-1] + math.floor(length / sequence_fd))
        padded += edge <= edges[-1]
        edges.append(max(edge, edges[-1] + 1))
    return edges, padded


def _output_edges(kept: list[_Kept], sequence_fd: Fraction) -> tuple[list[int], int]:
    pieces = [(k.end - k.start, k.end == k.source.clock.extent) for k in kept]
    return sequence_edges(pieces, sequence_fd)


def _parts(kept: list[_Kept], sequence: SequenceFormat) -> tuple[tuple[RenderPart, ...], int]:
    sfd = sequence.frame_duration
    edges, padded = _output_edges(kept, sfd)
    parts = []
    for index, piece in enumerate(kept):
        source = piece.source
        same_size = (source.width, source.height) == (sequence.width, sequence.height)
        same_shape = source.width * sequence.height == source.height * sequence.width
        parts.append(
            RenderPart(
                index=index,
                source_id=source.source_id,
                start_frame=piece.start_frame,
                end_frame=piece.end_frame,
                start=piece.start,
                end=piece.end,
                output_start_frame=edges[index],
                output_frames=edges[index + 1] - edges[index],
                output_start=edges[index] * sfd,
                output_end=edges[index + 1] * sfd,
                reason=piece.reason,
                scale=not same_size,
                pad=not same_shape,
                fps_convert=source.clock.frame_duration != sfd,
                bridged_seconds=piece.bridged_seconds,
            )
        )
    return tuple(parts), padded


# ── 자막 투영 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Projection:
    cues: tuple[ProjectedCue, ...]
    counts: CueCounts


def _overlaps(start: Fraction, end: Fraction, part: RenderPart) -> bool:
    if start == end:
        return part.start <= start < part.end
    return start < part.end and end > part.start


def _piece(cue: SourceCue, end: Fraction, part: RenderPart, text: str) -> ProjectedCue:
    """큐를 조각으로 자르고 조각의 출력 위치만큼 옮긴다 — 출력 조각 끝을 넘지 않는다."""
    start = max(cue.start, part.start)
    stop = min(end, part.end)
    shift = part.output_start - part.start
    out_start = min(start + shift, part.output_end)
    out_end = min(stop + shift, part.output_end)
    moved = (out_start, out_end) != (start + shift, stop + shift)
    clipped = (start, stop) != (cue.start, end) or moved
    return ProjectedCue(
        cue.utterance_id, part.source_id, part.index, out_start, out_end, text, clipped
    )


def _project_source(
    cues: Sequence[SourceCue], mine: Sequence[RenderPart], config: Config
) -> tuple[list[ProjectedCue], dict[str, int]]:
    pieces: list[ProjectedCue] = []
    tally = dict.fromkeys(_TALLY_KEYS, 0)
    for cue in cues:
        tally["source"] += 1
        text = caption_text(cue.text, config)
        if not text.strip():
            tally["empty"] += 1
            continue
        end = max(cue.end, cue.start)
        tally["inverted"] += cue.end < cue.start
        hits = [_piece(cue, end, part, text) for part in mine if _overlaps(cue.start, end, part)]
        tally["kept" if hits else "dropped"] += 1
        tally["split"] += max(len(hits) - 1, 0)
        pieces += hits
    return pieces, tally


_TALLY_KEYS = ("source", "kept", "split", "dropped", "empty", "inverted")


def project_cues(
    transcripts: Mapping[str, Sequence[SourceCue]],
    parts: Sequence[RenderPart],
    total_seconds: Fraction,
) -> tuple[tuple[ProjectedCue, ...], CueCounts]:
    """원본별 자막을 남긴 조각에 얹는다 — 계획과 「SRT만 다시 내보내기」(D115)가 함께 쓰는 한 길.

    `transcripts`의 차례가 같은 시각 큐의 차례다. 조각의 원본이 `transcripts`에 없으면 거부한다
    (자막 없는 원본은 빈 묶음으로 넘긴다 — 조용히 빠뜨리지 않는다).
    """
    missing = sorted({part.source_id for part in parts} - set(transcripts))
    if missing:
        raise RenderPlanError(f"자막을 얹을 원본이 없다: {', '.join(missing)}")
    config = Config()
    pieces: list[ProjectedCue] = []
    totals = dict.fromkeys(_TALLY_KEYS, 0)
    for source_id, cues in transcripts.items():
        mine = [part for part in parts if part.source_id == source_id]
        found, tally = _project_source(cues, mine, config)
        pieces += found
        for key, value in tally.items():
            totals[key] += value
    ordered = sorted(pieces, key=lambda cue: (cue.start, cue.end, cue.part_index))
    lengthened, count = _lengthen(ordered, total_seconds, config)
    counts = CueCounts(
        clipped=sum(cue.clipped for cue in lengthened),
        zero_length=sum(_zero_length(cue) for cue in lengthened),
        lengthened=count,
        **totals,
    )
    return lengthened, counts


def _project(
    sources: Sequence[RenderSource], parts: Sequence[RenderPart], total_seconds: Fraction
) -> _Projection:
    transcripts = {source.source_id: source.cues for source in sources}
    cues, counts = project_cues(transcripts, parts, total_seconds)
    return _Projection(cues, counts)


def _lengthen(
    ordered: Sequence[ProjectedCue], total_seconds: Fraction, config: Config
) -> tuple[tuple[ProjectedCue, ...], int]:
    """길이 0인 장(밀리초)의 끝만 `min_duration`까지 — 시작·차례·글자는 그대로다(D115).

    다음으로 **더 늦게** 시작하는 장 앞 `min_gap`에서 멈추고(그럴 자리가 없으면 그 장의 시작까지),
    출력 끝을 넘지 않는다. 같은 시각에 시작하는 장끼리는 막지 않는다 — 둘 다 길이 0으로 두면
    Final Cut이 거기서 읽기를 멈춘다.
    """
    floor = Fraction(str(config.min_duration))
    gap = Fraction(str(config.min_gap))
    starts = [milliseconds_half_up(cue.start) for cue in ordered]
    result: list[ProjectedCue] = []
    count = 0
    for index, cue in enumerate(ordered):
        if not _zero_length(cue):
            result.append(cue)
            continue
        end = min(cue.start + floor, total_seconds)
        later = bisect.bisect_right(starts, starts[index])
        if later < len(ordered):
            following = ordered[later].start
            room = following - gap
            end = min(end, room if milliseconds_half_up(room) > starts[index] else following)
        lengthened = replace(cue, end=max(end, cue.end))
        count += not _zero_length(lengthened)
        result.append(lengthened)
    return tuple(result), count


def lengthen_cues(
    ordered: Sequence[ProjectedCue], total_seconds: Fraction
) -> tuple[tuple[ProjectedCue, ...], int]:
    """시각 차례로 놓인 장들의 길이 0을 늘린다(D115) — 원본 범위 타임라인의 SRT도 같은 규칙이다."""
    return _lengthen(ordered, total_seconds, Config())


def _zero_length(cue: ProjectedCue) -> bool:
    return milliseconds_half_up(cue.start) == milliseconds_half_up(cue.end)


# ── 계획 짓기 ─────────────────────────────────────────────────────────────


def kept_ranges(
    source: RenderSource, settings: RenderSettings = RENDER_SETTINGS
) -> tuple[KeptRange, ...]:
    """원본 하나에서 렌더 계획이 남길 조각 — `build_plan`의 원본별 걸음 그대로(W-46b).

    `build_plan`은 원본마다 따로 `_layout_source`를 돌므로 같은 원본의 남길 조각은 다른 원본과
    무관하다. 그래서 레일은 원본 하나만으로 출력과 같은 범위를 그린다. map·렌더 거부 원본은
    빈 묶음이다.
    화면 크기는 보지 않는다 — 남길 범위와 무관하고 포맷 투표(`_sequence`)만 쓴다.
    """
    _require_layout(source)
    pieces, slots, _short = _layout_source(source, settings)
    _require_tiling(source, pieces, _ledger(source, slots))
    return tuple(KeptRange(piece.start, piece.end, piece.reason) for piece in pieces)


def build_plan(
    sources: Sequence[RenderSource], settings: RenderSettings = RENDER_SETTINGS
) -> RenderPlan:
    """원본들(촬영 시각 순) → 렌더 계획. 같은 입력이면 같은 계획이다."""
    _require_unique(sources)
    kept: list[_Kept] = []
    dropped: list[DroppedRange] = []
    too_short = 0
    for source in sources:
        _require_source(source)
        pieces, slots, short = _layout_source(source, settings)
        rows = _ledger(source, slots)
        _require_tiling(source, pieces, rows)
        kept += pieces
        dropped += rows
        too_short += short
    sequence = _sequence(kept, settings)
    parts, padded = _parts(kept, sequence) if sequence is not None else ((), 0)
    total = (parts[-1].output_start_frame + parts[-1].output_frames) if parts else 0
    seconds = total * sequence.frame_duration if sequence is not None else Fraction(0)
    projection = _project([s for s in sources if excluded_reason(s) is None], parts, seconds)
    plan = RenderPlan(
        parts=parts,
        dropped=tuple(dropped),
        sequence=sequence,
        cues=projection.cues,
        cue_counts=projection.counts,
        unsupported=_unsupported(sources),
        total_frames=total,
        padded=padded,
        too_short=too_short,
        settings=settings,
    )
    _require_cues_inside(plan)
    return plan


def plan_from_clips(
    sources: Sequence[RenderSource],
    clips: Sequence[tuple[str, int, int]],
    sequence: SequenceFormat,
    cues: Sequence[ProjectedCue],
    counts: CueCounts,
    dropped: Sequence[DroppedRange],
    settings: RenderSettings = RENDER_SETTINGS,
) -> RenderPlan:
    """원본 범위 타임라인(D130)의 클립 → 렌더 계획 — 등급으로 고르지 않고 클립 그대로다.

    시퀀스 포맷은 타임라인 문서에 고정된 것이고, 출력 배치는 `build_plan`과 같은 `_parts`다
    (`sequence_edges` — 타임라인 화면의 배치와 같다). 자막은 타임라인이 투영한 장을 그대로 싣는다.
    """
    by_id = {source.source_id: source for source in sources}
    kept: list[_Kept] = []
    for source_id, start, end in clips:
        source = by_id.get(source_id)
        if source is None:
            raise RenderPlanError(f"클립의 원본 {source_id}이 계획 재료에 없다")
        if not 0 <= start < end <= source.clock.frame_count:
            raise RenderPlanError(f"{source.name}: 클립 [{start}, {end})이 원본 프레임 밖이다")
        kept.append(_Kept(source, start, end, PART_TIMELINE, Fraction(0)))
    parts, padded = _parts(kept, sequence)
    total = parts[-1].output_start_frame + parts[-1].output_frames if parts else 0
    plan = RenderPlan(
        parts=parts,
        dropped=tuple(dropped),
        sequence=sequence,
        cues=tuple(cues),
        cue_counts=counts,
        unsupported=(),
        total_frames=total,
        padded=padded,
        too_short=0,
        settings=settings,
    )
    _require_cues_inside(plan)
    return plan


def _unsupported(sources: Sequence[RenderSource]) -> tuple[UnsupportedSource, ...]:
    rows: list[UnsupportedSource] = []
    for source in sources:
        reason = excluded_reason(source)
        if reason is not None:
            detail = source.refusal if reason == DROP_UNRENDERABLE else ""
            rows.append(UnsupportedSource(source.source_id, source.name, reason, detail))
    return tuple(rows)


def _require_cues_inside(plan: RenderPlan) -> None:
    for cue in plan.cues:
        if not 0 <= cue.start <= cue.end <= plan.total_seconds:
            raise RenderPlanError(f"자막 {cue.utterance_id}이 출력 [0, 끝] 밖에 놓였다")


# ── 직렬화 ───────────────────────────────────────────────────────────────


def srt_text(plan: RenderPlan) -> str:
    """계획의 자막 → SRT 글. 밀리초 반올림은 여기서 딱 한 번(half-up)이다."""
    return cues_srt_text(plan.cues)


def cues_srt_text(cues: Sequence[ProjectedCue]) -> str:
    """투영한 자막 → SRT 글(번호는 1부터) — 계획 없이 다시 얹은 자막도 같은 바이트로 쓴다."""
    blocks = []
    for number, cue in enumerate(cues, start=1):
        timeline = f"{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}"
        blocks.append(f"{number}\n{timeline}\n{cue.text}\n")
    return "\n".join(blocks)


def rational_from_document(value: Mapping[str, Any]) -> Fraction:
    """보고서의 유리수 칸(`{"num", "den"}`) → 값."""
    return Fraction(int(value["num"]), int(value["den"]))


def part_from_document(row: Mapping[str, Any]) -> RenderPart:
    """보고서의 조각 한 줄 → 조각 — `_part_document`의 거꾸로(D115 「SRT만 다시 내보내기」)."""
    return RenderPart(
        index=int(row["index"]),
        source_id=str(row["source_id"]),
        start_frame=int(row["source_start_frame"]),
        end_frame=int(row["source_end_frame"]),
        start=rational_from_document(row["source_start"]),
        end=rational_from_document(row["source_end"]),
        output_start_frame=int(row["output_start_frame"]),
        output_frames=int(row["output_frames"]),
        output_start=rational_from_document(row["output_start"]),
        output_end=rational_from_document(row["output_end"]),
        reason=str(row["reason"]),
        scale=bool(row["scale"]),
        pad=bool(row["pad"]),
        fps_convert=bool(row["fps_convert"]),
        bridged_seconds=rational_from_document(row["bridged_seconds"]),
    )


def _rational(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _part_document(part: RenderPart) -> dict[str, Any]:
    return {
        "index": part.index,
        "source_id": part.source_id,
        "source_start": _rational(part.start),
        "source_end": _rational(part.end),
        "source_start_frame": part.start_frame,
        "source_end_frame": part.end_frame,
        "output_start": _rational(part.output_start),
        "output_end": _rational(part.output_end),
        "output_start_frame": part.output_start_frame,
        "output_frames": part.output_frames,
        "reason": part.reason,
        "scale": part.scale,
        "pad": part.pad,
        "fps_convert": part.fps_convert,
        "bridged_seconds": _rational(part.bridged_seconds),
    }


def _dropped_document(row: DroppedRange) -> dict[str, Any]:
    return {
        "source_id": row.source_id,
        "start": _rational(row.start),
        "end": _rational(row.end),
        "reason": row.reason,
        "review_state": row.review_state,
        "tier": row.tier,
        "hold": row.hold,
    }


def _sequence_document(sequence: SequenceFormat | None) -> dict[str, Any] | None:
    if sequence is None:
        return None
    return {
        "orientation": sequence.orientation,
        "width": sequence.width,
        "height": sequence.height,
        "frame_duration": _rational(sequence.frame_duration),
    }


def _unsupported_document(row: UnsupportedSource) -> dict[str, Any]:
    """빠진 원본 한 줄 — 까닭 글은 있을 때만 싣는다(map 줄은 예전 바이트 그대로)."""
    document = asdict(row)
    if not row.detail:
        del document["detail"]
    return document


def report_document(plan: RenderPlan) -> dict[str, Any]:
    """`render_report.json`의 몸 — 남긴 조각·뺀 구간·자막 수·포맷·지원 밖 원본(JSON 값만)."""
    return {
        "parts": [_part_document(part) for part in plan.parts],
        "dropped": [_dropped_document(row) for row in plan.dropped],
        "cue_counts": asdict(plan.cue_counts),
        "sequence": _sequence_document(plan.sequence),
        "unsupported": [_unsupported_document(row) for row in plan.unsupported],
        "unreviewed_red_dropped": plan.unreviewed_red_dropped,
        "too_short": plan.too_short,
        "padded": plan.padded,
        "total_frames": plan.total_frames,
        "total_seconds": _rational(plan.total_seconds),
        "rules": {
            "min_part_seconds": _rational(plan.settings.min_part_seconds),
            "merge_gap_seconds": _rational(plan.settings.merge_gap_seconds),
            "orientation_tie": plan.settings.timeline.orientation_tie,
            "frame_rate_tie": plan.settings.timeline.frame_rate_tie,
        },
    }


def _pick_side(sides: dict[str, Fraction], tie: str) -> str:
    landscape = sides.get(ORIENTATION_LANDSCAPE, Fraction(0))
    portrait = sides.get(ORIENTATION_PORTRAIT, Fraction(0))
    if landscape == portrait:
        return tie
    return ORIENTATION_LANDSCAPE if landscape > portrait else ORIENTATION_PORTRAIT
