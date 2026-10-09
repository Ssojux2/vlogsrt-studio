"""불러온 원본 전부를 태그 달린 타임라인 하나로 — Final Cut에 보낼 FCPXML의 순수 빌더(D56).

입력은 값뿐이다: 원본 사실(`SourceMedia`), 검토 조각의 사용/제외 상태(`SegmentState`), 저장된
등급 보고서(`TierReport`), 자막 큐(`Cue`). 파일·프로세스·서비스·검토 저장소를 모른다 — 모으는
일과 쓰는 일은 내보내기 계층(Phase B)의 몫이다. 같은 입력이면 같은 바이트가 나온다.

지키는 선:

- **아무것도 지우지 않는다.** 원본마다 클립이 `[0, frame_count)`를 빈틈없이 덮는다. 끄는 것은
  사람이 확정한 제외뿐이고(`enabled="0"` + `제외 확정`), 꺼진 클립은 Final Cut에서 V로 되살아난다.
  미검토 자동 제외는 켠 채 `제외 제안(미검토)` 키워드만 단다 — 사람이 보기 전에 화면에서 사라지면
  손실을 발견할 기회가 없어진다(D35, 규칙 9).
- **시각은 실측에서만 온다.** 클립 시작은 원본 타임코드 프레임 × 프레임 길이다(Final Cut이 같은
  값을 쓴다: 0008 `12:56:29;10` → `1397678282/30000s`). 캡션·키워드 범위를 프레임에 맞추는
  것(½프레임 이내, 최소 1프레임)은 FCPXML이 프레임 격자만 받기 때문이지 시각 유도가 아니다.
- **고치지 않고 센다.** 빈 글·겹침·줄 수·영상 밖 꼬리는 그대로 두거나 건너뛰고 수를 보고한다.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import TypeVar

from vlogsrt.project.fcp_tables import (
    AUDIO_LAYOUTS,
    AUDIO_RATE_TAGS,
    COLOR_SPACES,
    FRAME_RATE_TAGS,
)
from vlogsrt.project.raw.tiering import (
    HOLD_LABEL,
    TIER_GREEN,
    TIER_LABELS,
    TIER_RED,
    TIER_YELLOW,
    TierReport,
    segment_keyword,
)
from vlogsrt.project.timebase import format_srt_time, frame_nearest

_Key = TypeVar("_Key")

FCPXML_VERSION = "1.14"
"""설치된 Final Cut Pro 12.4의 DTD 버전 — 이 문서는 그 DTD로 검증된다."""

KEYWORD_RECOMMENDED = TIER_LABELS[TIER_GREEN]
KEYWORD_NORMAL = TIER_LABELS[TIER_YELLOW]
KEYWORD_SUGGESTED_EXCLUSION = TIER_LABELS[TIER_RED]
KEYWORD_HOLD = HOLD_LABEL
KEYWORD_UNANALYZED = "미분석"
KEYWORD_UNREVIEWED = "제외 제안(미검토)"
KEYWORD_CONFIRMED_EXCLUSION = "제외 확정"
KEYWORD_CAPTION_TIMING = "자막 시각 확인 필요"
"""W-15가 자막 시각이 어긋났다고 짚은 영상의 키워드 — W-15와 W-16 중 나중 쪽이 잇는다.
아직 어떤 입력에도 연결하지 않는다(늘 비는 매개변수를 내보내지 않는다)."""
KEYWORDS = (
    KEYWORD_RECOMMENDED,
    KEYWORD_NORMAL,
    KEYWORD_SUGGESTED_EXCLUSION,
    KEYWORD_HOLD,
    KEYWORD_UNANALYZED,
    KEYWORD_UNREVIEWED,
    KEYWORD_CONFIRMED_EXCLUSION,
    KEYWORD_CAPTION_TIMING,
)
"""키워드 철자 전부 — `keyword@value`는 쉼표로 나뉘는 목록이라 쉼표가 들어가면 둘로 쪼개진다."""
_TIER_KEYWORDS = (KEYWORD_RECOMMENDED, KEYWORD_NORMAL, KEYWORD_SUGGESTED_EXCLUSION, KEYWORD_HOLD)

# Final Cut 형식의 사실(조정 임계값이 아니다). 캡션은 한국어 iTT 역할에 lane 1로 단다 —
# Final Cut이 내보내는 캡션이 `start="3600s"`를 쓰고, iTT 캡션은 두 줄까지만 받는다(Apple
# 캡션 검증 규칙). 평가는 즐겨찾기만 쓴다 — reject는 기본 필터가 숨겨 지운 것처럼 보인다.
CAPTION_ROLE = "iTT?captionFormat=ITT.ko"
CAPTION_LANE = "1"
CAPTION_START = "3600s"
CAPTION_MAX_LINES = 2
AUDIO_ROLE = "dialogue"
SEQUENCE_TC_START = "0s"
RATING_FAVORITE = "favorite"
TC_DROP, TC_NON_DROP = "DF", "NDF"
_DROP_FRAME_DURATIONS = frozenset({Fraction(1001, 30000), Fraction(1001, 60000)})
_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n'
_INDENT = "    "
_TITLE_PREFIX = "vlogsrt"
_TITLE_SEPARATOR = " · "

# 라이브러리 밖 원본의 uid. Apple은 대문자 hex만으로 된 문자열을 Final Cut 내부 신원으로
# 예약했다 — 제3자가 쓰면 기존 미디어의 uid를 덮어쓸 수 있다(=라이브러리 변경).
UID_PREFIX = "com.vlogsrt.source."
_MEDIA_IDENTIFIER = re.compile(r"[0-9A-F]{32}")
_CONTENT_SHA256 = re.compile(r"[0-9a-f]{64}")
# XML 1.0이 받는 문자 밖(제어 문자 등)이 섞이면 문서가 깨진다 — 고치지 않고 거부한다.
_XML_FORBIDDEN = re.compile(r"[^\t\n\r\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")


class FinalCutTimelineError(ValueError):
    """타임라인을 만들 수 없는 입력 — 어느 원본의 무엇이 문제인지 이름으로 말한다."""


# ── 입력 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceMedia:
    """원본 하나의 사실 — 내보내기 계층이 저장값·ffprobe·라이브러리에서 모아 대조한 값이다.

    `width`·`height`는 화면에 보이는 기하(회전 반영)다. `color`는 ffprobe의
    (primaries, transfer, matrix) 그대로고, 모르는 값이면 `colorSpace`를 생략하고 보고한다.
    `media_identifier`는 Final Cut 라이브러리가 가진 원본일 때만 있다.
    """

    source_id: str
    name: str
    path: Path
    content_sha256: str
    media_identifier: str | None
    frame_duration: Fraction
    frame_count: int
    timecode_frames: int
    drop_frame: bool
    width: int
    height: int
    color: tuple[str, str, str]
    audio_channels: int | None
    audio_rate: int | None


@dataclass(frozen=True, slots=True)
class SegmentState:
    """검토 조각 하나 — 원본 프레임 좌표(원점 0)의 반개 구간과 사용/제외 상태."""

    start_frame: int
    end_frame: int
    excluded: bool
    unreviewed: bool
    """미검토 자동 제외 표지 — 제외된 조각에만 붙는다."""


@dataclass(frozen=True, slots=True)
class Cue:
    """자막 큐 하나 — 원본 시계의 초(검토 조각·등급 보고서와 같은 원점)."""

    start: Fraction
    end: Fraction
    text: str


@dataclass(frozen=True, slots=True)
class SourceInput:
    """원본 하나에 대해 모은 것. `segments`가 None이면 검토 머리에 없고, `report`가 None이면
    분석 전이며, `cues`가 None이면 자막이 없다."""

    media: SourceMedia
    segments: tuple[SegmentState, ...] | None = None
    report: TierReport | None = None
    cues: tuple[Cue, ...] | None = None


# ── 계획 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClipPlan:
    """스파인 클립 하나 — 원본 프레임 `[start_frame, end_frame)`와 켜짐·키워드."""

    start_frame: int
    end_frame: int
    enabled: bool
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaptionPiece:
    """클립 하나에 단 캡션 조각 — 컷을 넘는 큐는 같은 글로 여러 조각이 된다."""

    clip_index: int
    start_frame: int
    end_frame: int
    text: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class CaptionCounts:
    """캡션을 만들며 센 것 — 무엇도 고치지 않았다는 증거다.

    칸은 서로 겹치지 않는다: `cues = empty + outside + placed`. 넣은 큐 가운데 `widened`(프레임에
    맞추니 0프레임)와 `inverted`(끝이 시작보다 앞)는 서로 배타이고 둘 다 시작 프레임에 1프레임으로
    둔다. `tails`는 영상 밖이 잘린 **넣은** 큐뿐이다 — 통째로 밖이면 `outside`로만 센다.
    `overlaps`·`line_issues`·`split`은 넣은 큐의 성질이라 위 칸들과 함께 셀 수 있다.
    """

    cues: int = 0
    placed: int = 0
    pieces: int = 0
    empty: int = 0
    widened: int = 0
    inverted: int = 0
    overlaps: int = 0
    line_issues: int = 0
    tails: int = 0
    outside: int = 0
    split: int = 0


@dataclass(frozen=True, slots=True)
class BrowserRange:
    """브라우저 클립(원본 전체)의 키워드 범위 하나 — 초록이면 즐겨찾기도 단다."""

    start_frame: int
    end_frame: int
    keyword: str
    favorite: bool


@dataclass(frozen=True, slots=True)
class SourcePlan:
    media: SourceMedia
    color_space: str | None
    analysed: bool
    clips: tuple[ClipPlan, ...]
    captions: tuple[CaptionPiece, ...]
    caption_counts: CaptionCounts | None
    browser: tuple[BrowserRange, ...] | None
    caption_timing: bool = False
    """자막 시각 확인(D55)이 지금 빨강이라고 한 원본 — 모든 클립과 브라우저 클립에 키워드만 단다."""


@dataclass(frozen=True, slots=True)
class FormatPlan:
    format_id: str
    width: int
    height: int
    frame_duration: Fraction
    color_space: str | None

    @property
    def name(self) -> str:
        tag = FRAME_RATE_TAGS[self.frame_duration]
        return f"FFVideoFormat{self.width}x{self.height}p{tag}"


@dataclass(frozen=True, slots=True)
class TimelineDocument:
    title: str
    library_location: str | None
    formats: tuple[FormatPlan, ...]
    sequence_format: str
    sequence_drop_frame: bool
    sequence_audio: tuple[int, int] | None
    sources: tuple[SourcePlan, ...]


# ── 원본 하나 ────────────────────────────────────────────────────────────


def _refuse(media: SourceMedia, reason: str) -> FinalCutTimelineError:
    return FinalCutTimelineError(f"{media.name}: {reason}")


def _require_media(media: SourceMedia) -> None:
    """원본 사실의 계약 — 어긋난 값을 맞추지 않고 거부한다."""
    if not media.name or _XML_FORBIDDEN.search(media.name):
        raise FinalCutTimelineError(f"원본 이름을 XML에 쓸 수 없다: {media.name!r}")
    if not media.path.is_absolute():
        raise _refuse(media, f"원본 경로가 절대 경로가 아니다: {media.path}")
    if media.frame_duration not in FRAME_RATE_TAGS:
        raise _refuse(media, f"지원하지 않는 프레임 길이 {media.frame_duration}초")
    if media.drop_frame and media.frame_duration not in _DROP_FRAME_DURATIONS:
        raise _refuse(media, f"드롭프레임은 29.97·59.94에서만 성립한다({media.frame_duration}초)")
    if media.frame_count <= 0 or media.timecode_frames < 0:
        raise _refuse(media, "프레임 수는 양수, 시작 타임코드는 0 이상이어야 한다")
    if media.width <= 0 or media.height <= 0:
        raise _refuse(media, f"화면 크기가 양수가 아니다: {media.width}x{media.height}")
    if (media.audio_channels is None) != (media.audio_rate is None):
        raise _refuse(media, "오디오 채널 수와 샘플레이트는 함께 있거나 함께 없어야 한다")
    _require_identity(media)


def _require_identity(media: SourceMedia) -> None:
    identifier = media.media_identifier
    if identifier is not None and not _MEDIA_IDENTIFIER.fullmatch(identifier):
        raise _refuse(media, f"라이브러리 mediaIdentifier 형식이 아니다: {identifier!r}")
    if not _CONTENT_SHA256.fullmatch(media.content_sha256):
        raise _refuse(media, f"내용 해시 형식이 아니다: {media.content_sha256!r}")


def asset_identity(media: SourceMedia) -> tuple[str, str | None]:
    """(uid, sig) — 라이브러리 원본이면 그 신원 그대로, 아니면 접두사 붙은 소문자 해시.

    라이브러리의 `mediaIdentifier`를 uid·sig로 쓰면 Final Cut이 이미 가진 미디어와 맞춘다.
    sig는 Final Cut이 매기는 값이라 라이브러리 밖 원본에는 적지 않는다.
    """
    if media.media_identifier is not None:
        return media.media_identifier, media.media_identifier
    return UID_PREFIX + media.content_sha256, None


def _require_tiling(media: SourceMedia, segments: Sequence[SegmentState]) -> None:
    """조각이 `[0, frame_count)`를 겹침·틈 없이 덮어야 한다 — 빠진 구간은 조용히 사라진다."""
    cursor = 0
    for segment in segments:
        if segment.start_frame != cursor or segment.end_frame <= segment.start_frame:
            raise _refuse(media, f"검토 조각이 원본을 빈틈없이 덮지 못한다(프레임 {cursor})")
        if segment.unreviewed and not segment.excluded:
            raise _refuse(media, "미검토 표지가 제외되지 않은 조각에 붙었다")
        cursor = segment.end_frame
    if cursor != media.frame_count:
        raise _refuse(media, f"검토 조각이 {cursor}프레임에서 끝난다 — 원본은 {media.frame_count}")


def _base_keyword(source: SourceInput, start_frame: int, end_frame: int) -> str:
    if source.report is None:
        return KEYWORD_UNANALYZED
    fd = source.media.frame_duration
    return segment_keyword(source.report, start_frame * fd, end_frame * fd)


def _clip(source: SourceInput, segment: SegmentState) -> ClipPlan:
    """조각 하나 → 클립. 사람이 확정한 제외만 끄고, 미검토 제외는 켠 채 키워드로만 알린다."""
    keywords = [_base_keyword(source, segment.start_frame, segment.end_frame)]
    confirmed = segment.excluded and not segment.unreviewed
    if segment.unreviewed:
        keywords.append(KEYWORD_UNREVIEWED)
    if confirmed:
        keywords.append(KEYWORD_CONFIRMED_EXCLUSION)
    return ClipPlan(segment.start_frame, segment.end_frame, not confirmed, tuple(keywords))


def _merge_identical(clips: Sequence[ClipPlan]) -> tuple[ClipPlan, ...]:
    """분석 전 원본의 맞닿은 같은 상태 조각을 하나로 — 나눠 보낼 정보가 없다."""
    merged: list[ClipPlan] = []
    for clip in clips:
        last = merged[-1] if merged else None
        if last is not None and (last.enabled, last.keywords) == (clip.enabled, clip.keywords):
            merged[-1] = replace(last, end_frame=clip.end_frame)
        else:
            merged.append(clip)
    return tuple(merged)


def timeline_clips(source: SourceInput) -> tuple[ClipPlan, ...]:
    """원본 하나의 스파인 클립 — 검토 조각마다 하나, 검토 머리 밖이면 파일 통째로 하나."""
    media = source.media
    if source.segments is None:
        whole = SegmentState(0, media.frame_count, excluded=False, unreviewed=False)
        return (_clip(source, whole),)
    segments = sorted(source.segments, key=lambda segment: segment.start_frame)
    _require_tiling(media, segments)
    clips = tuple(_clip(source, segment) for segment in segments)
    return clips if source.report is not None else _merge_identical(clips)


def browser_ranges(report: TierReport, media: SourceMedia) -> tuple[BrowserRange, ...]:
    """등급 구간 → 브라우저 클립의 키워드 범위. 양 끝을 가장 가까운 프레임에 맞추고
    `[0, frame_count]`로 자른다. 0프레임 자투리는 버리고, 맞닿은 같은 키워드는 합친다
    (범위가 실은 정보는 키워드 하나뿐이다). 초록만 즐겨찾기이고 reject는 없다."""
    fd = media.frame_duration
    ranges: list[BrowserRange] = []
    for span in report.spans:
        start = min(max(frame_nearest(span.start, fd), 0), media.frame_count)
        end = min(max(frame_nearest(span.end, fd), 0), media.frame_count)
        if end <= start:
            continue
        keyword = segment_keyword(report, span.start, span.end)
        last = ranges[-1] if ranges else None
        if last is not None and last.keyword == keyword and last.end_frame == start:
            ranges[-1] = replace(last, end_frame=end)
            continue
        ranges.append(BrowserRange(start, end, keyword, keyword == KEYWORD_RECOMMENDED))
    return tuple(ranges)


def _line_issue(text: str) -> bool:
    lines = text.replace("\r\n", "\n").split("\n")
    return len(lines) > CAPTION_MAX_LINES or any(not line.strip() for line in lines)


def _snapped(cue: Cue, fd: Fraction) -> tuple[int, int, str | None]:
    """큐 → 자르기 전 프레임 구간과 손댄 까닭. 뒤집힌 큐와 0프레임 큐는 시작 프레임에 1프레임."""
    start, end = frame_nearest(cue.start, fd), frame_nearest(cue.end, fd)
    if cue.end < cue.start:
        return start, start + 1, "inverted"
    if end <= start:
        return start, start + 1, "widened"
    return start, end, None


def caption_pieces(
    cues: Sequence[Cue], clips: Sequence[ClipPlan], frame_duration: Fraction
) -> tuple[tuple[CaptionPiece, ...], CaptionCounts]:
    """큐를 프레임에 맞춰 클립마다 나눈다 — 글은 한 자도 고치지 않고, 문제는 센다.

    빈 글은 건너뛰고, 겹침은 그대로 두고, 두 줄 넘는 글·빈 줄은 그대로 두고, 영상 밖 꼬리는
    잘라 낸다(통째로 밖이면 넣지 않는다). 컷을 넘는 큐는 같은 글로 조각이 여럿이 되고,
    꺼진 클립 밑의 조각은 함께 꺼진다. 세는 칸의 뜻은 `CaptionCounts`에 있다.
    """
    counts: Counter[str] = Counter()
    pieces: list[CaptionPiece] = []
    frame_count = clips[-1].end_frame if clips else 0
    reach = 0
    for cue in sorted(cues, key=lambda row: (row.start, row.end)):
        counts["cues"] += 1
        if not cue.text.strip():
            counts["empty"] += 1
            continue
        start, end, repair = _snapped(cue, frame_duration)
        begin, finish = max(start, 0), min(end, frame_count)
        if finish <= begin:
            counts["outside"] += 1
            continue
        if repair is not None:
            counts[repair] += 1
        counts["tails"] += (begin, finish) != (start, end)
        counts["overlaps"] += begin < reach
        counts["line_issues"] += _line_issue(cue.text)
        reach = max(reach, finish)
        made = _cut_pieces(cue, begin, finish, clips)
        counts.update(placed=1, pieces=len(made), split=len(made) > 1)
        pieces.extend(made)
    return tuple(pieces), CaptionCounts(**counts)


def _cue_time(value: Fraction) -> str:
    """사람이 찾아갈 큐 시각 — 자막 편집 화면과 같은 `HH:MM:SS,mmm`."""
    return format_srt_time(value) if value >= 0 else f"{value}초"


def _cut_pieces(cue: Cue, start: int, end: int, clips: Sequence[ClipPlan]) -> list[CaptionPiece]:
    if _XML_FORBIDDEN.search(cue.text):
        raise FinalCutTimelineError(
            f"{_cue_time(cue.start)} 자막 글에 XML이 받지 않는 문자가 있다: {cue.text!r}"
        )
    spans = [
        (index, max(start, c.start_frame), min(end, c.end_frame)) for index, c in enumerate(clips)
    ]
    return [
        CaptionPiece(index, begin, finish, cue.text, clips[index].enabled)
        for index, begin, finish in spans
        if begin < finish
    ]


def _with_caption_timing(clips: tuple[ClipPlan, ...]) -> tuple[ClipPlan, ...]:
    """등급 키워드 바로 옆에 `자막 시각 확인 필요` — 자막·켜짐은 그대로다(D55·D56)."""
    return tuple(
        replace(clip, keywords=(clip.keywords[0], KEYWORD_CAPTION_TIMING, *clip.keywords[1:]))
        for clip in clips
    )


def plan_source(source: SourceInput, *, caption_timing: bool = False) -> SourcePlan:
    """원본 하나의 계획 — 클립·캡션·브라우저 범위. `caption_timing`이면 자막 시각 키워드를 단다."""
    media = source.media
    _require_media(media)
    clips = timeline_clips(source)
    if caption_timing:
        clips = _with_caption_timing(clips)
    captions: tuple[CaptionPiece, ...] = ()
    counts: CaptionCounts | None = None
    if source.cues is not None:
        try:
            captions, counts = caption_pieces(source.cues, clips, media.frame_duration)
        except FinalCutTimelineError as error:
            raise _refuse(media, str(error)) from error
    report = source.report
    return SourcePlan(
        media=media,
        color_space=COLOR_SPACES.get(media.color),
        analysed=report is not None,
        clips=clips,
        captions=captions,
        caption_counts=counts,
        browser=None if report is None else browser_ranges(report, media),
        caption_timing=caption_timing,
    )


# ── 문서 ─────────────────────────────────────────────────────────────────


def _dominant(uses: Sequence[tuple[_Key, int]]) -> _Key:
    """가장 길게 쓰이는 값 — 길이가 같으면 촬영 순서에서 먼저 나온 값(`max`는 첫 최대를 준다)."""
    totals: dict[_Key, int] = {}
    for key, frames in uses:
        totals[key] = totals.get(key, 0) + frames
    return max(totals, key=lambda key: totals[key])


def _require_one_rate(plans: Sequence[SourcePlan]) -> None:
    """프레임레이트가 섞이면 거부한다 — 맞추면 그 리샘플링이 1.36초 사고를 다시 낸다."""
    groups: dict[Fraction, list[str]] = {}
    for plan in plans:
        groups.setdefault(plan.media.frame_duration, []).append(plan.media.name)
    if len(groups) > 1:
        listed = " / ".join(f"{fd}초: {', '.join(names)}" for fd, names in groups.items())
        raise FinalCutTimelineError(f"프레임레이트가 섞였다 — 맞추지 않고 거부한다: {listed}")


def _format_key(plan: SourcePlan) -> tuple[int, int, Fraction, str | None]:
    media = plan.media
    return media.width, media.height, media.frame_duration, plan.color_space


def _formats(plans: Sequence[SourcePlan]) -> tuple[FormatPlan, ...]:
    """(크기, 프레임 길이, 색공간)마다 포맷 하나 — 촬영 순서에서 처음 나온 순서로 번호를 매긴다."""
    keys = list(dict.fromkeys(_format_key(plan) for plan in plans))
    return tuple(
        FormatPlan(f"r{index + 1}", width, height, fd, color)
        for index, (width, height, fd, color) in enumerate(keys)
    )


def _format_ids(formats: Sequence[FormatPlan]) -> dict[tuple[int, int, Fraction, str | None], str]:
    return {(f.width, f.height, f.frame_duration, f.color_space): f.format_id for f in formats}


def _sequence_audio(plans: Sequence[SourcePlan]) -> tuple[int, int] | None:
    """시퀀스 오디오 — Final Cut 철자가 있는 값 가운데 가장 길게 쓰이는 것, 없으면 생략한다."""
    uses: list[tuple[tuple[int, int], int]] = []
    for plan in plans:
        channels, rate = plan.media.audio_channels, plan.media.audio_rate
        if channels is None or rate is None:
            continue
        if channels in AUDIO_LAYOUTS and rate in AUDIO_RATE_TAGS:
            uses.append(((channels, rate), plan.media.frame_count))
    return _dominant(uses) if uses else None


def plan(
    sources: Sequence[SourceInput],
    *,
    title: str,
    library_bundle: Path | None,
    caption_timing: frozenset[str] = frozenset(),
) -> TimelineDocument:
    """원본 전부(주어진 촬영 순서 그대로) → 타임라인 문서. 원본 순서를 바꾸지 않는다.

    `caption_timing`은 자막 시각 확인(D55)이 **지금 빨강**이라고 한 원본 ID다 — 고르는 일은
    내보내기 계층(서비스)이 저장된 보고서를 읽어 하고, 여기는 값으로 받아 키워드만 단다.
    비어 있으면 문서는 W-16과 바이트까지 같다.
    """
    if not sources:
        raise FinalCutTimelineError("보낼 원본이 하나도 없다")
    unknown = sorted(caption_timing - {source.media.source_id for source in sources})
    if unknown:
        raise FinalCutTimelineError(f"자막 시각 확인 표지가 보내지 않는 원본을 가리킨다: {unknown}")
    if not title.strip() or _XML_FORBIDDEN.search(title):
        raise FinalCutTimelineError(f"이벤트·프로젝트 이름을 쓸 수 없다: {title!r}")
    if library_bundle is not None and not library_bundle.is_absolute():
        raise FinalCutTimelineError(f"라이브러리 경로가 절대 경로가 아니다: {library_bundle}")
    plans = tuple(
        plan_source(source, caption_timing=source.media.source_id in caption_timing)
        for source in sources
    )
    _require_one_rate(plans)
    formats = _formats(plans)
    ids = _format_ids(formats)
    uses = [(ids[_format_key(p)], p.media.frame_count) for p in plans]
    drop = _dominant([(p.media.drop_frame, p.media.frame_count) for p in plans])
    return TimelineDocument(
        title=title,
        library_location=None if library_bundle is None else _library_uri(library_bundle),
        formats=formats,
        sequence_format=_dominant(uses),
        sequence_drop_frame=drop,
        sequence_audio=_sequence_audio(plans),
        sources=plans,
    )


def _library_uri(bundle: Path) -> str:
    """번들은 디렉터리라 URI 끝에 `/`를 단다 — Final Cut 자신의 내보내기가 그렇게 적는다
    (이 기기 실물: `<library location="file:///Volumes/SubSSD/EU.fcpbundle/">`)."""
    return bundle.as_uri() + "/"


def export_title(workspace_name: str, moment: datetime) -> str:
    """새 이벤트·프로젝트 이름 — `vlogsrt · <작업공간> · <YYYY-MM-DD HH:MM>`."""
    stamp = moment.strftime("%Y-%m-%d %H:%M")
    return _TITLE_SEPARATOR.join((_TITLE_PREFIX, workspace_name, stamp))


def document_counts(document: TimelineDocument) -> dict[str, object]:
    """보낸 것의 요약 — 리포트 JSON과 화면 문구의 재료다."""
    clips = [clip for source in document.sources for clip in source.clips]
    browser = [row for s in document.sources for row in (s.browser or ())]
    captions: Counter[str] = Counter()
    for source in document.sources:
        captions.update(asdict(source.caption_counts or CaptionCounts()))
    return {
        "sources": len(document.sources),
        "analysed": sum(source.analysed for source in document.sources),
        "spine_clips": len(clips),
        "disabled_clips": sum(not clip.enabled for clip in clips),
        "unreviewed_clips": sum(KEYWORD_UNREVIEWED in clip.keywords for clip in clips),
        "frames": sum(source.media.frame_count for source in document.sources),
        "keywords": dict(Counter(word for clip in clips for word in clip.keywords)),
        "browser_ranges": len(browser),
        "favorites": sum(row.favorite for row in browser),
        "captions": {name.name: captions[name.name] for name in fields(CaptionCounts)},
        "sources_without_captions": [
            s.media.name for s in document.sources if s.caption_counts is None
        ],
        "unknown_color": [s.media.name for s in document.sources if s.color_space is None],
        "formats": len(document.formats),
    }


# ── 직렬화 ───────────────────────────────────────────────────────────────


def _time(frames: int, frame_duration: Fraction) -> str:
    """프레임 수 → FCPXML 시간. 프레임 길이의 분모를 그대로 둔다(Final Cut 자신의 철자:
    1396282프레임 @ 1001/30000 → `1397678282/30000s`)."""
    if frames == 0:
        return "0s"
    numerator = frames * frame_duration.numerator
    if frame_duration.denominator == 1:
        return f"{numerator}s"
    return f"{numerator}/{frame_duration.denominator}s"


def _tc_format(drop_frame: bool) -> str:
    return TC_DROP if drop_frame else TC_NON_DROP


def _import_options(root: ET.Element, document: TimelineDocument) -> None:
    """원본을 복사하지 않고 링크하며, 경고를 숨기지 않고, 원래 라이브러리로 들어간다."""
    options = ET.SubElement(root, "import-options")
    if document.library_location is not None:
        ET.SubElement(
            options, "option", {"key": "library location", "value": document.library_location}
        )
    ET.SubElement(options, "option", {"key": "copy assets", "value": "0"})
    ET.SubElement(options, "option", {"key": "suppress warnings", "value": "0"})


def _format_element(parent: ET.Element, fmt: FormatPlan) -> None:
    attrib = {
        "id": fmt.format_id,
        "name": fmt.name,
        "frameDuration": _time(1, fmt.frame_duration),
        "width": str(fmt.width),
        "height": str(fmt.height),
    }
    if fmt.color_space is not None:
        attrib["colorSpace"] = fmt.color_space
    ET.SubElement(parent, "format", attrib)


def _asset_element(parent: ET.Element, asset_id: str, format_id: str, media: SourceMedia) -> None:
    uid, sig = asset_identity(media)
    fd = media.frame_duration
    attrib = {
        "id": asset_id,
        "name": media.name,
        "uid": uid,
        "start": _time(media.timecode_frames, fd),
        "duration": _time(media.frame_count, fd),
        "hasVideo": "1",
        "format": format_id,
        "videoSources": "1",
        "hasAudio": "0" if media.audio_channels is None else "1",
    }
    if media.audio_channels is not None:
        attrib.update(
            audioSources="1",
            audioChannels=str(media.audio_channels),
            audioRate=str(media.audio_rate),
        )
    asset = ET.SubElement(parent, "asset", attrib)
    rep = {"kind": "original-media", **({} if sig is None else {"sig": sig})}
    ET.SubElement(asset, "media-rep", {**rep, "src": media.path.as_uri()})


def _clip_attrib(
    asset_id: str, format_id: str, media: SourceMedia, start_frame: int, frames: int
) -> dict[str, str]:
    fd = media.frame_duration
    attrib = {
        "ref": asset_id,
        "name": media.name,
        "start": _time(media.timecode_frames + start_frame, fd),
        "duration": _time(frames, fd),
        "format": format_id,
        "tcFormat": _tc_format(media.drop_frame),
    }
    if media.audio_channels is not None:
        attrib["audioRole"] = AUDIO_ROLE
    return attrib


def _range_element(
    parent: ET.Element, tag: str, media: SourceMedia, a: int, b: int, value: str
) -> None:
    fd = media.frame_duration
    start, duration = _time(media.timecode_frames + a, fd), _time(b - a, fd)
    ET.SubElement(parent, tag, {"start": start, "duration": duration, "value": value})


def _browser_clip(parent: ET.Element, ids: tuple[str, str], plan: SourcePlan) -> None:
    """원본 전체를 덮는 브라우저 클립 — 키워드 컬렉션과 평가는 브라우저에서만 보인다."""
    media = plan.media
    clip = ET.SubElement(parent, "asset-clip", _clip_attrib(*ids, media, 0, media.frame_count))
    rows = plan.browser or ()
    for row in rows:
        _range_element(clip, "keyword", media, row.start_frame, row.end_frame, row.keyword)
    if plan.caption_timing:
        _range_element(clip, "keyword", media, 0, media.frame_count, KEYWORD_CAPTION_TIMING)
    for row in rows:
        if row.favorite:
            _range_element(clip, "rating", media, row.start_frame, row.end_frame, RATING_FAVORITE)


def _caption_element(parent: ET.Element, media: SourceMedia, piece: CaptionPiece) -> None:
    fd = media.frame_duration
    attrib = {
        "lane": CAPTION_LANE,
        "offset": _time(media.timecode_frames + piece.start_frame, fd),
        "name": piece.text,  # Final Cut 자신의 내보내기처럼 캡션 이름이 곧 글이다
        "start": CAPTION_START,
        "duration": _time(piece.end_frame - piece.start_frame, fd),
        "role": CAPTION_ROLE,
    }
    if not piece.enabled:
        attrib["enabled"] = "0"
    caption = ET.SubElement(parent, "caption", attrib)
    ET.SubElement(caption, "text").text = piece.text


def _spine_clip(
    spine: ET.Element, ids: tuple[str, str], plan: SourcePlan, index: int, offset: int
) -> int:
    """스파인 클립 하나 — 자식은 캡션(anchor) 다음 키워드(marker) 순서여야 DTD를 통과한다."""
    media, clip = plan.media, plan.clips[index]
    frames = clip.end_frame - clip.start_frame
    attrib = _clip_attrib(*ids, media, clip.start_frame, frames)
    attrib = {"ref": attrib.pop("ref"), "offset": _time(offset, media.frame_duration), **attrib}
    if not clip.enabled:
        attrib["enabled"] = "0"
    element = ET.SubElement(spine, "asset-clip", attrib)
    for piece in plan.captions:
        if piece.clip_index == index:
            _caption_element(element, media, piece)
    for keyword in clip.keywords:
        _range_element(element, "keyword", media, clip.start_frame, clip.end_frame, keyword)
    return offset + frames


def _sequence(project: ET.Element, document: TimelineDocument, total_frames: int) -> ET.Element:
    fd = document.sources[0].media.frame_duration
    attrib = {
        "format": document.sequence_format,
        "duration": _time(total_frames, fd),
        "tcStart": SEQUENCE_TC_START,
        "tcFormat": _tc_format(document.sequence_drop_frame),
    }
    if document.sequence_audio is not None:
        channels, rate = document.sequence_audio
        attrib.update(audioLayout=AUDIO_LAYOUTS[channels], audioRate=AUDIO_RATE_TAGS[rate])
    return ET.SubElement(ET.SubElement(project, "sequence", attrib), "spine")


def _source_ids(document: TimelineDocument) -> list[tuple[str, str]]:
    """원본마다 (asset id, format id) — 순번으로만 정한다. 입력이 같으면 id도 같다."""
    ids = _format_ids(document.formats)
    return [
        (f"a{index + 1}", ids[_format_key(plan)]) for index, plan in enumerate(document.sources)
    ]


def _event(library: ET.Element, document: TimelineDocument, ids: list[tuple[str, str]]) -> None:
    event = ET.SubElement(library, "event", {"name": document.title})
    used: set[str] = set()
    for plan, pair in zip(document.sources, ids, strict=True):
        if plan.browser is not None:
            _browser_clip(event, pair, plan)
            used.update(row.keyword for row in plan.browser)
    if any(plan.caption_timing and plan.browser is not None for plan in document.sources):
        used.add(KEYWORD_CAPTION_TIMING)
    for keyword in (*_TIER_KEYWORDS, KEYWORD_CAPTION_TIMING):
        if keyword in used:
            ET.SubElement(event, "keyword-collection", {"name": keyword})
    project = ET.SubElement(event, "project", {"name": document.title})
    total = sum(plan.media.frame_count for plan in document.sources)
    spine = _sequence(project, document, total)
    offset = 0
    for plan, pair in zip(document.sources, ids, strict=True):
        for index in range(len(plan.clips)):
            offset = _spine_clip(spine, pair, plan, index, offset)


def render(document: TimelineDocument) -> bytes:
    """문서 → FCPXML 1.14 바이트. 같은 문서면 같은 바이트다(속성은 넣은 순서 그대로 나온다)."""
    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    _import_options(root, document)
    resources = ET.SubElement(root, "resources")
    for fmt in document.formats:
        _format_element(resources, fmt)
    ids = _source_ids(document)
    for plan, (asset_id, format_id) in zip(document.sources, ids, strict=True):
        _asset_element(resources, asset_id, format_id, plan.media)
    location = {} if document.library_location is None else {"location": document.library_location}
    _event(ET.SubElement(root, "library", location), document, ids)
    ET.indent(root, space=_INDENT)
    return (_XML_HEADER + ET.tostring(root, encoding="unicode") + "\n").encode("utf-8")
