"""불러온 원본 전부를 태그 달린 타임라인 하나로 — Final Cut에 보낼 FCPXML의 순수 빌더(D56).

입력은 값뿐이다: 원본 사실(`SourceMedia`), 검토 조각의 사용/제외 상태(`SegmentState`), 저장된
등급 보고서(`TierReport`), 자막 큐(`Cue`). 파일·프로세스·서비스·검토 저장소를 모른다 — 모으는
일과 쓰는 일은 내보내기 계층(Phase B)의 몫이다. 같은 입력이면 같은 바이트가 나온다.

지키는 선:

- **아무것도 지우지 않는다.** 원본마다 클립이 `[0, frame_count)`를 빈틈없이 덮는다. 끄는 것은
  사람이 확정한 제외(`enabled="0"` + `제외 확정`)와 미검토 빨강(`enabled="0"` + `제외 제안` +
  `제외 제안(미검토)`, W-29d D80)이고, 꺼진 클립은 Final Cut에서 V로 되살아난다 — 클립·캡션·
  키워드가 그대로 남아 사람이 찾을 수 있다(D35, 규칙 9). **추천만 켠다**(D93, 설정
  `recommended_only`, 보내기 설정 `FINALCUT_TIMELINE`이 켠다): 보통·판정 보류·미분석 클립도 끈 채
  보낸다 — 사람이 「사용」으로 정한 조각은 등급과 무관하게 켠다. 빌더의 기본 설정
  (`FinalCutTimelineSettings()`)은 D80 그대로 보통·판정 보류를 켠다.
- **시각은 실측에서만 온다.** 클립 시작은 원본 타임코드 프레임 × 프레임 길이다(Final Cut이 같은
  값을 쓴다: 0008 `12:56:29;10` → `1397678282/30000s`). 캡션·키워드 범위를 프레임에 맞추는
  것(½프레임 이내, 최소 1프레임)은 FCPXML이 프레임 격자만 받기 때문이지 시각 유도가 아니다.
- **고치지 않고 센다.** 빈 글·겹침·줄 수·영상 밖 꼬리는 그대로 두거나 건너뛰고 수를 보고한다.
- **묻힌 추천도 보인다(D60 20b-final).** 켠 클립의 등급이 추천이 아니어도 그 안에 추천 최소 길이
  이상 겹친 브라우저 「추천」 범위가 있으면 그 겹침에 `추천` 키워드 범위를 하나 더 단다 — 컷·켜짐·
  등급 키워드·캡션은 그대로다. 묻힌 추천이 없으면 바이트가 W-16 빌더 그대로다.
- **프로젝트 방향은 많은 쪽이다(W-26, D66).** 켠 스파인 클립 길이를 가로(너비 ≥ 높이)·세로로
  나눠 많은 쪽이 시퀀스 방향이 되고(켠 클립이 없으면 모든 클립, 같으면 설정의 편), 그 방향의 포맷
  가운데서만 W-16 규칙(원본 길이가 가장 긴 포맷)으로 고른다. 한쪽 방향뿐이면 바이트가 W-16
  그대로다. 소수 쪽 클립은 제 포맷 그대로 두고 맞춤 지시(`adjust-conform`)를 적지 않는다 —
  Final Cut 기본(fit)이다.
- **프레임레이트가 섞이면 많은 쪽 레이트 하나다(W-27d, D71).** 고른 방향 안에서 켠 클립 길이(초)가
  많은 레이트가 시퀀스 레이트다(같으면 설정의 편). 소수 쪽 클립은 제 포맷 그대로이고 첫 자식에
  `conform-rate scaleEnabled="0"`을 적는다 — 프레임을 1:1로 얹으면(scaleEnabled="1") 시간이
  늘거나 줄어 D42의 1.36초 사고가 된다. 그 클립의 위치·길이는 시퀀스 격자이고, 원본마다 누적
  경계를 가장 가까운 시퀀스 프레임에 맞춰(½프레임 이내) 오차가 쌓이지 않으며, 마지막 클립만 남은
  미디어로 내려 원본 끝을 넘지 않는다. 시작·캡션·키워드 범위는 클립 안 시각(원본 격자) 그대로다.
  레이트가 하나면 바이트가 W-27d 앞 그대로다.
"""

from __future__ import annotations

import math
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
    CONFORM_RATES,
    FRAME_RATE_TAGS,
)
from vlogsrt.project.raw.config import (
    FRAME_RATE_HIGHER,
    ORIENTATION_LANDSCAPE,
    ORIENTATION_PORTRAIT,
    FinalCutTimelineSettings,
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
from vlogsrt.project.timebase import format_srt_time, frame_floor, frame_nearest

_Key = TypeVar("_Key")

FCPXML_VERSION = "1.14"
"""설치된 Final Cut Pro 12.4의 DTD 버전 — 이 문서는 그 DTD로 검증된다."""

BUILDER_DEFAULTS = FinalCutTimelineSettings()
"""빌더 함수의 기본 설정 — D80 규칙 그대로(보통·보류도 켠다). 보내기는 `FINALCUT_TIMELINE`(추천만
켠다, D93)을 넘긴다 — 빌더 시험은 규칙마다 설정을 골라 본다."""

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

# Final Cut 형식의 사실(조정 임계값이 아니다). 캡션은 한국어 SRT 역할에 lane 1로 단다 — 역할이
# 캡션 형식을 정하고 Final Cut의 캡션 내보내기가 그 형식으로 파일을 쓴다(사용자 지시 2026-10-23
# 「SRT 파일로」, W-29f D81). 철자는 Final Cut 12.4 자신의 규칙
# `<짧은 이름>?captionFormat=<형식>.<언어>`(예전 `iTT?captionFormat=ITT.ko`와 같은 틀,
# 짧은 이름·형식 둘 다 `SRT` — FFLocalizable.strings).
# 캡션은 `start="3600s"`를 쓴다(Final Cut 자신의 내보내기). 두 줄을 넘는 캡션은 세기만 한다(Apple
# 캡션 검증 규칙의 iTT 한도를 보수적으로 그대로 쓴다). 평가는 즐겨찾기만 쓴다 — reject는 기본
# 필터가 숨겨 지운 것처럼 보인다.
CAPTION_ROLE = "SRT?captionFormat=SRT.ko"
CAPTION_LANE = "1"
CAPTION_START = "3600s"
CAPTION_MAX_LINES = 2
TITLE_LANE = "1"
TITLE_START = "3600s"
"""타이틀의 안쪽 시계 시작 — Final Cut 자신의 철자(D91)."""
AUDIO_ROLE = "dialogue"
SEQUENCE_TC_START = "0s"
CONFORM_KEEP_TIME = "0"
"""`conform-rate@scaleEnabled` — 0이면 클립의 시간을 지킨다(Final Cut 자신의 내보내기 철자)."""
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
    decided: bool = False
    """조각에 사람 결정이 실렸는가(되살림·덮기·사람 메모·수동 선택, W-29g D82) — 결정이 없는 빨강은
    자동 제외로 제안된 적이 없어도 끈다."""


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
    recommended: tuple[tuple[int, int], ...] = ()
    """클립 안에 묻힌 추천 범위(원본 프레임 반개 구간) — 켠 비추천 클립에만 있다(D60)."""


@dataclass(frozen=True, slots=True)
class CaptionPiece:
    """클립 하나에 단 캡션 조각 — 컷을 넘는 큐는 같은 글로 여러 조각이 된다."""

    clip_index: int
    start_frame: int
    end_frame: int
    text: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class TitlePiece:
    """스파인 클립 하나에 붙은 타이틀(SRT 한 줄, D91) — 시작은 그 클립 안 원본 프레임(캡션과 같은
    원본 격자), 길이는 시퀀스 시간이다. 컷을 넘는 줄도 나누지 않는다(붙은 클립 너머로 이어진다)."""

    clip_index: int
    start_frame: int
    duration: str
    text: str
    enabled: bool
    style_id: str
    """`text-style-def@id` — XML ID라 문서 전체에서 유일하다."""


@dataclass(frozen=True, slots=True)
class TitleStyle:
    """문서의 모든 타이틀이 같이 쓰는 템플릿과 모양 — `finalcut_titles`가 설정으로 만든다(D91)."""

    effect_id: str
    effect_name: str
    effect_uid: str
    params: tuple[tuple[str, str, str], ...]
    """(이름, 열쇠, 값) — 템플릿 매개변수(Position·Flatten·Alignment)."""
    text_style: tuple[tuple[str, str], ...]
    """`text-style` 속성 — 넣은 순서 그대로 적는다."""


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
    sequence_frames: tuple[int, ...] = ()
    """클립마다 시퀀스 프레임 수 — 시퀀스와 레이트가 다른 원본에만 있다(비면 원본 프레임 그대로,
    W-27d)."""
    sequence_padded: int = 0
    """시퀀스 1프레임이 안 돼 1프레임으로 늘린 클립 수 — 고치지 않고 센다."""
    titles: tuple[TitlePiece, ...] = ()
    """SRT 줄의 타이틀(D91) — 원본 자막 캡션 대신 실을 때만 있다."""
    dropped_clips: int = 0
    """프로젝트에서 뺀 꺼진 클립 수(D94) — 빼지 않으면 0이다."""
    dropped_seconds: Fraction = Fraction(0)
    """뺀 클립 길이(초) 합(D94)."""


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
    sequence_frame_duration: Fraction
    """시퀀스 포맷의 프레임 길이 — 스파인의 위치·길이가 이 격자에 선다(W-27d)."""
    title_style: TitleStyle | None = None
    """타이틀(D91)의 템플릿·모양 — 없으면 문서가 W-27d 그대로다."""
    drop_disabled: bool = False
    """꺼진 클립을 뺀 문서(D94) — 리포트가 뺀 수를 싣는다."""


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


def _clip(source: SourceInput, segment: SegmentState, recommended_only: bool) -> ClipPlan:
    """조각 하나 → 클립. 사람이 확정한 제외와 사람이 보지 않은 빨강(`제외 제안`)을 끈다(D80·D82).

    사람이 보지 않은 빨강은 미검토 자동 제외이거나, 제안된 적 없이 사용으로 남은 결정 없는 조각이다
    (분석 전에 세운 머리 · 컷을 다 덮지 못한 빨강, W-29g) — 둘 다 `제외 제안(미검토)` 키워드를 단다.
    미검토 제외라도 등급이 빨강이 아니면(보통·보류·미분석) 키워드로 알리고, 사람이 되살린
    빨강(결정이 실린 사용 조각)은 켠다. `recommended_only`(D93)면 추천이 아니고 사람이 「사용」으로
    정하지도 않은 조각(보통·보류·미분석)도 끈다. 끄기는 지우기가 아니다 — V로 되살아난다.
    """
    tier_keyword = _base_keyword(source, segment.start_frame, segment.end_frame)
    keywords = [tier_keyword]
    confirmed = segment.excluded and not segment.unreviewed
    never_proposed = not segment.excluded and not segment.decided
    suggested = tier_keyword == KEYWORD_SUGGESTED_EXCLUSION and (
        segment.unreviewed or never_proposed
    )
    if segment.unreviewed or suggested:
        keywords.append(KEYWORD_UNREVIEWED)
    if confirmed:
        keywords.append(KEYWORD_CONFIRMED_EXCLUSION)
    kept = tier_keyword == KEYWORD_RECOMMENDED or (segment.decided and not segment.excluded)
    enabled = not (confirmed or suggested) and (kept or not recommended_only)
    return ClipPlan(segment.start_frame, segment.end_frame, enabled, tuple(keywords))


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


def timeline_clips(
    source: SourceInput, settings: FinalCutTimelineSettings = BUILDER_DEFAULTS
) -> tuple[ClipPlan, ...]:
    """원본 하나의 스파인 클립 — 검토 조각마다 하나, 검토 머리 밖이면 파일 통째로 하나."""
    media = source.media
    only = settings.recommended_only
    if source.segments is None:
        whole = SegmentState(0, media.frame_count, excluded=False, unreviewed=False)
        return (_clip(source, whole, only),)
    segments = sorted(source.segments, key=lambda segment: segment.start_frame)
    _require_tiling(media, segments)
    clips = tuple(_clip(source, segment, only) for segment in segments)
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


def _minimum_frames(seconds: float, frame_duration: Fraction) -> int:
    """추천 최소 길이(보고서 설정의 `recommended_min_seconds`)를 프레임으로 — **올림**.

    그 길이 이상인 가장 짧은 프레임 수라 「최소 3초」의 뜻 그대로이고(29.97p 90프레임 = 3.003초,
    89프레임 = 2.97초는 모자란다), 검토 조각의 3초 규칙(`segment_keyword`가 프레임 경계의 정확
    유리수로 견준다)과 같은 경계다. 0이면 1프레임 이상이다.
    """
    return max(math.ceil(Fraction(str(seconds)) / frame_duration), 1)


def _buried_recommended(
    clip: ClipPlan, browser: Sequence[BrowserRange], minimum: int
) -> tuple[tuple[int, int], ...]:
    """클립 하나에 묻힌 추천 — 브라우저 즐겨찾기(=「추천」) 범위와의 겹침 가운데 `minimum`프레임
    이상인 것. 제외로 꺼진 클립(사람이 확정한 제외·미검토 빨강)과 이미 추천인 클립은 없다. 추천만
    켜서(D93) 꺼진 보통·보류 클립에는 단다 — 그 안의 추천을 찾아 V로 켤 수 있게."""
    excluded = KEYWORD_CONFIRMED_EXCLUSION in clip.keywords or (
        not clip.enabled and KEYWORD_UNREVIEWED in clip.keywords
    )
    if excluded or clip.keywords[0] == KEYWORD_RECOMMENDED:
        return ()
    overlaps = (
        (max(clip.start_frame, row.start_frame), min(clip.end_frame, row.end_frame))
        for row in browser
        if row.favorite
    )
    return tuple((start, end) for start, end in overlaps if end - start >= minimum)


def _with_buried_recommended(
    clips: tuple[ClipPlan, ...], browser: Sequence[BrowserRange], report: TierReport, fd: Fraction
) -> tuple[ClipPlan, ...]:
    """보통·보류·제외 제안 클립 안의 추천 범위를 클립에 싣는다 — 컷·켜짐·등급 키워드는 그대로다."""
    minimum = _minimum_frames(report.settings.recommended_min_seconds, fd)
    return tuple(
        replace(clip, recommended=_buried_recommended(clip, browser, minimum)) for clip in clips
    )


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


def plan_source(
    source: SourceInput,
    *,
    caption_timing: bool = False,
    settings: FinalCutTimelineSettings = BUILDER_DEFAULTS,
) -> SourcePlan:
    """원본 하나의 계획 — 클립·캡션·브라우저 범위. `caption_timing`이면 자막 시각 키워드를 단다."""
    media = source.media
    _require_media(media)
    clips = timeline_clips(source, settings)
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
    browser = None if report is None else browser_ranges(report, media)
    if report is not None and browser is not None:
        clips = _with_buried_recommended(clips, browser, report, media.frame_duration)
    return SourcePlan(
        media=media,
        color_space=COLOR_SPACES.get(media.color),
        analysed=report is not None,
        clips=clips,
        captions=captions,
        caption_counts=counts,
        browser=browser,
        caption_timing=caption_timing,
    )


# ── 문서 ─────────────────────────────────────────────────────────────────


def _dominant(uses: Sequence[tuple[_Key, int]]) -> _Key:
    """가장 길게 쓰이는 값 — 길이가 같으면 촬영 순서에서 먼저 나온 값(`max`는 첫 최대를 준다)."""
    totals: dict[_Key, int] = {}
    for key, frames in uses:
        totals[key] = totals.get(key, 0) + frames
    return max(totals, key=lambda key: totals[key])


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


def orientation_of(width: int, height: int) -> str:
    """화면 방향 — 너비가 높이 이상이면 가로다(정사각형도 가로, D66)."""
    return ORIENTATION_LANDSCAPE if width >= height else ORIENTATION_PORTRAIT


def _media_orientation(media: SourceMedia) -> str:
    return orientation_of(media.width, media.height)


def _voting_seconds(plans: Sequence[SourcePlan]) -> list[tuple[SourcePlan, Fraction]]:
    """원본마다 표가 되는 길이(초) — 켠 클립만 센다. 켠 클립이 하나도 없으면 모든 클립이다
    (사람이 전부 끈 타임라인도 방향·레이트는 있어야 한다). 레이트가 섞이면 프레임 수는 서로 다른
    시간이라 초로 잰다 — 레이트가 하나면 같은 배수라 프레임으로 잰 것과 순서가 같다."""
    any_enabled = any(clip.enabled for plan in plans for clip in plan.clips)
    return [
        (
            plan,
            plan.media.frame_duration
            * sum(
                clip.end_frame - clip.start_frame
                for clip in plan.clips
                if clip.enabled or not any_enabled
            ),
        )
        for plan in plans
    ]


def _orientation_seconds(plans: Sequence[SourcePlan]) -> dict[str, Fraction]:
    """방향마다 켠 스파인 클립 길이(초) 합(D66)."""
    totals = {ORIENTATION_LANDSCAPE: Fraction(), ORIENTATION_PORTRAIT: Fraction()}
    for plan, seconds in _voting_seconds(plans):
        totals[_media_orientation(plan.media)] += seconds
    return totals


def _sequence_orientation(plans: Sequence[SourcePlan], settings: FinalCutTimelineSettings) -> str:
    totals = _orientation_seconds(plans)
    landscape, portrait = totals[ORIENTATION_LANDSCAPE], totals[ORIENTATION_PORTRAIT]
    if landscape == portrait:
        return settings.orientation_tie
    return ORIENTATION_LANDSCAPE if landscape > portrait else ORIENTATION_PORTRAIT


def _sequence_rate(
    plans: Sequence[SourcePlan], side: str, settings: FinalCutTimelineSettings
) -> Fraction:
    """시퀀스 프레임 길이 — 고른 방향 안에서 켠 길이(초)가 가장 많은 레이트(W-27d, D71).

    예전에는 섞이면 거부했다(D42·D66). 이제 소수 쪽은 시간을 지키며 맞춰 재생된다. 길이가 같은
    레이트가 여럿이면 설정의 편(`frame_rate_tie`) — 높은 레이트는 프레임 길이가 짧은 쪽이다.
    고른 방향의 표는 0보다 크므로(`_sequence_format`) 늘 레이트가 하나 이상 있다.
    """
    totals: dict[Fraction, Fraction] = {}
    for plan, seconds in _voting_seconds(plans):
        if _media_orientation(plan.media) == side:
            fd = plan.media.frame_duration
            totals[fd] = totals.get(fd, Fraction()) + seconds
    most = max(totals.values())
    tied = [fd for fd, seconds in totals.items() if seconds == most]
    return min(tied) if settings.frame_rate_tie == FRAME_RATE_HIGHER else max(tied)


def _sequence_format(
    plans: Sequence[SourcePlan],
    ids: dict[tuple[int, int, Fraction, str | None], str],
    settings: FinalCutTimelineSettings,
) -> tuple[str, Fraction]:
    """시퀀스 포맷과 프레임 길이 — 방향(D66) → 그 안의 레이트(D71) → 그 (방향·레이트)의 포맷
    가운데 W-16 규칙(원본 길이가 가장 긴 것) 그대로.

    방향·레이트의 표는 켠 길이로 세지만 그 안의 고르기는 바꾸지 않는다 — 한쪽 방향·한 레이트뿐이면
    거르는 것이 없으므로 W-16과 같은 포맷이 나온다. 고른 방향에는 늘 원본이 있다: 이긴 쪽은 0보다
    크고, 같으면 두 쪽 다 0보다 크다(원본마다 클립이 1프레임 이상을 덮고, 켠 클립이 없으면 모든
    클립이 표다). 이긴 레이트도 같은 까닭으로 0보다 크다.
    """
    side = _sequence_orientation(plans, settings)
    rate = _sequence_rate(plans, side, settings)
    chosen = [
        p for p in plans if _media_orientation(p.media) == side and p.media.frame_duration == rate
    ]
    return _dominant([(ids[_format_key(p)], p.media.frame_count) for p in chosen]), rate


def _sequence_layout(plan: SourcePlan, sequence_fd: Fraction) -> tuple[tuple[int, ...], int]:
    """레이트가 다른 원본의 클립마다 시퀀스 프레임 수와, 1프레임으로 늘린 클립 수(W-27d, D71).

    클립마다 따로 반올림하면 오차가 클립 수만큼 쌓인다(25프레임 @24 = 31.25프레임 @30 — 200개면
    1.7초). 그래서 원본 안의 **누적 경계**를 가장 가까운 시퀀스 프레임에 맞추고 그 차를 쓴다 —
    안쪽 컷마다 ½프레임 이내이고 쌓이지 않는다. **마지막 클립만 남은 미디어로 내림**이다: 그 클립의
    시작은 원본 시각 그대로라 끝 = 시작 + 시퀀스 길이이고, 앞 경계가 내려 맞춰졌으면 가장 가까운
    끝(또는 원본 끝의 내림)도 원본에 없는 미디어를 가리킨다(Final Cut 자신의 내보내기에는 그런
    클립이 없다). 그래서 마지막 길이는 남은 미디어를 넘지 않는 가장 긴 프레임 수다 — 1프레임 안의
    꼬리만 보이지 않는다. 두 경계가 같은 프레임에 떨어지면(시퀀스 1프레임보다 짧은 조각) 0프레임
    클립은 쓸 수 없어 1프레임으로 늘리고 세며, 다음 경계부터 다시 제 규칙으로 돌아간다. 시간은
    원본 시계에서만 온다 — 격자에 맞출 뿐 유도하지 않는다.
    """
    fd = plan.media.frame_duration
    clips = plan.clips
    edges = [0]
    padded = 0
    total = Fraction(0)
    for index, clip in enumerate(clips):
        length = (clip.end_frame - clip.start_frame) * fd
        total += length
        edge = frame_nearest(total, sequence_fd)
        following = clips[index + 1] if index + 1 < len(clips) else None
        if following is None or following.start_frame != clip.end_frame:
            # 뒤에 이어지는 미디어가 없다(원본 끝 · 뺀 클립 앞, D94) — 제 미디어를 넘지 않는다.
            edge = min(edge, edges[-1] + frame_floor(length, sequence_fd))
        padded += edge <= edges[-1]
        edges.append(max(edge, edges[-1] + 1))
    return tuple(b - a for a, b in zip(edges, edges[1:], strict=False)), padded


def _with_sequence_layout(plan: SourcePlan, sequence_fd: Fraction) -> SourcePlan:
    """시퀀스와 레이트가 같은 원본은 그대로 둔다 — 레이트가 하나면 계획이 W-27d 앞과 같다."""
    if plan.media.frame_duration == sequence_fd:
        return plan
    frames, padded = _sequence_layout(plan, sequence_fd)
    return replace(plan, sequence_frames=frames, sequence_padded=padded)


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


def _sequence_drop_frame(plans: Sequence[SourcePlan], sequence_fd: Fraction) -> bool:
    """시퀀스 타임코드 형식 — 시퀀스 레이트의 원본만 정한다(D71). 30p 프로젝트에 29.97 DF 소수가
    DF를 끌어오면 성립하지 않는 선언이 된다(드롭프레임은 29.97·59.94에서만). 레이트가 하나면
    모든 원본이라 W-16 그대로다."""
    majority = [p for p in plans if p.media.frame_duration == sequence_fd]
    return _dominant([(p.media.drop_frame, p.media.frame_count) for p in majority])


class UnreviewedDropError(FinalCutTimelineError):
    """빠질 꺼진 클립에 사람이 확인하지 않은 제외 제안이 있다(D94·규칙 9·D35) — 먼저 확인한다."""

    def __init__(self, names: Sequence[str], count: int) -> None:
        self.names = tuple(names)
        self.count = count
        super().__init__(
            f"확인하지 않은 제외 제안 {count}개가 빠질 클립에 있다({', '.join(self.names)})"
        )


def _without_disabled(plans: Sequence[SourcePlan]) -> tuple[SourcePlan, ...]:
    """꺼진 클립을 뺀 계획(D94) — 사람이 보지 않은 제외 제안이 빠지거나 켠 클립이 하나도 없으면
    거부한다. 원본 전체는 브라우저 클립으로 남는다(분석 전 원본도 통째로 하나 단다)."""
    unreviewed = [
        (plan.media.name, clip)
        for plan in plans
        for clip in plan.clips
        if not clip.enabled and KEYWORD_UNREVIEWED in clip.keywords
    ]
    if unreviewed:
        names = list(dict.fromkeys(name for name, _clip in unreviewed))
        raise UnreviewedDropError(names, len(unreviewed))
    if not any(clip.enabled for plan in plans for clip in plan.clips):
        raise FinalCutTimelineError("켠 클립이 하나도 없다 — 꺼진 클립을 빼면 빈 프로젝트가 된다")
    return tuple(_kept_only(plan) for plan in plans)


def _kept_only(plan: SourcePlan) -> SourcePlan:
    """원본 하나에서 켠 클립만 — 캡션은 남은 클립의 차례로 옮기고 뺀 클립의 캡션은 함께 뺀다."""
    kept = [index for index, clip in enumerate(plan.clips) if clip.enabled]
    order = {old: new for new, old in enumerate(kept)}
    dropped = [clip for clip in plan.clips if not clip.enabled]
    media = plan.media
    browser = plan.browser
    if browser is None:
        browser = (BrowserRange(0, media.frame_count, KEYWORD_UNANALYZED, False),)
    return replace(
        plan,
        clips=tuple(plan.clips[index] for index in kept),
        captions=tuple(
            replace(piece, clip_index=order[piece.clip_index])
            for piece in plan.captions
            if piece.clip_index in order
        ),
        browser=browser,
        dropped_clips=len(dropped),
        dropped_seconds=sum(
            ((clip.end_frame - clip.start_frame) * media.frame_duration for clip in dropped),
            Fraction(0),
        ),
    )


def plan(
    sources: Sequence[SourceInput],
    *,
    title: str,
    library_bundle: Path | None,
    caption_timing: frozenset[str] = frozenset(),
    settings: FinalCutTimelineSettings = BUILDER_DEFAULTS,
) -> TimelineDocument:
    """원본 전부(주어진 촬영 순서 그대로) → 타임라인 문서. 원본 순서를 바꾸지 않는다.

    `caption_timing`은 자막 시각 확인(D55)이 **지금 빨강**이라고 한 원본 ID다 — 고르는 일은
    내보내기 계층(서비스)이 저장된 보고서를 읽어 하고, 여기는 값으로 받아 키워드만 단다.
    비어 있으면 문서는 W-16과 바이트까지 같다. `settings`는 방향·레이트 규칙이다(D66·D71).
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
        plan_source(
            source, caption_timing=source.media.source_id in caption_timing, settings=settings
        )
        for source in sources
    )
    if settings.drop_disabled:
        plans = _without_disabled(plans)
    formats = _formats(plans)
    sequence_format, sequence_fd = _sequence_format(plans, _format_ids(formats), settings)
    return TimelineDocument(
        title=title,
        library_location=None if library_bundle is None else _library_uri(library_bundle),
        formats=formats,
        sequence_format=sequence_format,
        sequence_drop_frame=_sequence_drop_frame(plans, sequence_fd),
        sequence_audio=_sequence_audio(plans),
        sources=tuple(_with_sequence_layout(p, sequence_fd) for p in plans),
        sequence_frame_duration=sequence_fd,
        drop_disabled=settings.drop_disabled,
    )


def _library_uri(bundle: Path) -> str:
    """번들은 디렉터리라 URI 끝에 `/`를 단다 — Final Cut 자신의 내보내기가 그렇게 적는다
    (이 기기 실물: `<library location="file:///Volumes/SubSSD/EU.fcpbundle/">`)."""
    return bundle.as_uri() + "/"


def export_title(workspace_name: str, moment: datetime, tag: str | None = None) -> str:
    """새 이벤트·프로젝트 이름 — `vlogsrt · <작업공간> · <YYYY-MM-DD HH:MM>[ · <꼬리>]`."""
    stamp = moment.strftime("%Y-%m-%d %H:%M")
    parts = (_TITLE_PREFIX, workspace_name, stamp, *(() if tag is None else (tag,)))
    return _TITLE_SEPARATOR.join(parts)


def _dropped_counts(document: TimelineDocument) -> dict[str, object]:
    """꺼진 클립을 뺀 문서(D94)만 — 뺀 클립 수·길이(초). 빼지 않은 문서는 칸이 없다."""
    if not document.drop_disabled:
        return {}
    seconds = sum((source.dropped_seconds for source in document.sources), Fraction(0))
    return {
        "dropped_clips": sum(source.dropped_clips for source in document.sources),
        "dropped_seconds": str(seconds),
    }


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
        "orientation": _document_orientation(document),
        **_rate_counts(document),
        **_dropped_counts(document),
    }


def _rate_counts(document: TimelineDocument) -> dict[str, object]:
    """프로젝트 레이트와 그에 맞춰 재생되는 소수 쪽(W-27d, D71) — 리포트 JSON과 화면 문구에만 싣고
    문서 바이트와 무관하다. `frames`(원본 프레임 합)는 그대로 두고 시퀀스 길이는 따로 센다."""
    sequence_fd = document.sequence_frame_duration
    conformed = [s for s in document.sources if s.media.frame_duration != sequence_fd]
    fates = Counter(fate for s in conformed for fate in _caption_fates(s, sequence_fd))
    return {
        "frame_rate": CONFORM_RATES[sequence_fd],
        "sequence_frames": sum(sum(_sequence_frames(s)) for s in document.sources),
        "conformed_sources": [s.media.name for s in conformed],
        "conformed_clips": sum(len(s.clips) for s in conformed),
        "conformed_padded_clips": sum(s.sequence_padded for s in conformed),
        "conformed_clamped_captions": fates[_CAPTION_CLAMPED],
        "conformed_hidden_captions": fates[_CAPTION_HIDDEN],
    }


def _document_orientation(document: TimelineDocument) -> str:
    """보낸 프로젝트(시퀀스 포맷)의 방향 — 리포트 JSON에만 싣는다(D66)."""
    (sequence,) = (f for f in document.formats if f.format_id == document.sequence_format)
    return orientation_of(sequence.width, sequence.height)


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


def _seconds(value: Fraction) -> str:
    """격자 밖 시각(잘린 캡션 꼬리) → FCPXML 시간. 약분한 유리수 그대로 적는다."""
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


def _tc_format(drop_frame: bool) -> str:
    return TC_DROP if drop_frame else TC_NON_DROP


def _import_options(root: ET.Element, library_location: str | None) -> None:
    """원본을 복사하지 않고 링크하며, 경고를 숨기지 않고, 원래 라이브러리로 들어간다."""
    options = ET.SubElement(root, "import-options")
    if library_location is not None:
        ET.SubElement(options, "option", {"key": "library location", "value": library_location})
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
    asset_id: str,
    format_id: str,
    media: SourceMedia,
    start_frame: int,
    frames: int,
    duration: str | None = None,
) -> dict[str, str]:
    """클립 속성 — `start`는 원본 격자, `duration`은 부모 시간이다. 스파인 클립은 시퀀스 격자의
    길이를 넘기고(`duration`), 브라우저 클립은 원본 프레임 그대로다."""
    fd = media.frame_duration
    attrib = {
        "ref": asset_id,
        "name": media.name,
        "start": _time(media.timecode_frames + start_frame, fd),
        "duration": _time(frames, fd) if duration is None else duration,
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


_CAPTION_KEPT, _CAPTION_CLAMPED, _CAPTION_HIDDEN = "kept", "clamped", "hidden"


def _sequence_frames(plan: SourcePlan) -> tuple[int, ...]:
    """클립마다 시퀀스 프레임 수 — 시퀀스와 레이트가 같은 원본은 원본 프레임 그대로다."""
    return plan.sequence_frames or tuple(clip.end_frame - clip.start_frame for clip in plan.clips)


def sequence_frame_total(document: TimelineDocument) -> int:
    """시퀀스 길이(시퀀스 프레임) — 스파인 클립 길이의 합이다(꺼진 클립도 든다)."""
    return sum(sum(_sequence_frames(plan)) for plan in document.sources)


def _local_ends(plan: SourcePlan, sequence_fd: Fraction) -> list[Fraction | None]:
    """클립마다 보이는 끝의 클립 안 시각 — 시작(원본 격자) + 시퀀스 길이. 같은 레이트는 None이다.

    `scaleEnabled="0"`이면 클립 안 시각이 그대로 시퀀스 시각으로 옮겨지므로(이동만), 시퀀스 길이가
    원본 조각보다 짧으면(½프레임 이내) 이 시각 뒤는 보이지 않는다.
    """
    if not plan.sequence_frames:
        return [None] * len(plan.clips)
    media = plan.media
    return [
        (media.timecode_frames + clip.start_frame) * media.frame_duration + frames * sequence_fd
        for clip, frames in zip(plan.clips, plan.sequence_frames, strict=True)
    ]


def _caption_fate(media: SourceMedia, piece: CaptionPiece, local_end: Fraction | None) -> str:
    """캡션 조각이 클립 끝과 어떤가 — 그대로 · 꼬리를 그 끝에서 자름 · 보이는 끝 뒤에서 시작함.

    뒤에서 시작하는 조각은 소수 쪽 원본 프레임이 시퀀스 프레임보다 짧을 때만 생긴다(경계 둘이
    ½프레임씩 반대로 맞춰지면 클립이 시퀀스 1프레임 가까이 짧아진다 — 그 클립에서 보이지 않는
    조각) — 길이 0 이하 캡션은 쓸 수 없어 넣지 않고 센다.
    """
    if local_end is None:
        return _CAPTION_KEPT
    fd = media.frame_duration
    if (media.timecode_frames + piece.start_frame) * fd >= local_end:
        return _CAPTION_HIDDEN
    if (media.timecode_frames + piece.end_frame) * fd > local_end:
        return _CAPTION_CLAMPED
    return _CAPTION_KEPT


def _caption_fates(plan: SourcePlan, sequence_fd: Fraction) -> list[str]:
    ends = _local_ends(plan, sequence_fd)
    return [_caption_fate(plan.media, piece, ends[piece.clip_index]) for piece in plan.captions]


def _caption_element(
    parent: ET.Element, media: SourceMedia, piece: CaptionPiece, local_end: Fraction | None
) -> None:
    """캡션 하나 — 클립 안 시각(원본 격자). 소수 쪽 클립에서 꼬리가 보이는 끝을 넘으면 거기서
    자른다(다음 클립의 캡션과 겹치지 않게). 글·시작은 그대로다."""
    fate = _caption_fate(media, piece, local_end)
    if fate == _CAPTION_HIDDEN:
        return
    fd = media.frame_duration
    begin = media.timecode_frames + piece.start_frame
    if fate == _CAPTION_CLAMPED and local_end is not None:
        duration = _seconds(local_end - begin * fd)
    else:
        duration = _time(piece.end_frame - piece.start_frame, fd)
    attrib = {
        "lane": CAPTION_LANE,
        "offset": _time(begin, fd),
        "name": piece.text,  # Final Cut 자신의 내보내기처럼 캡션 이름이 곧 글이다
        "start": CAPTION_START,
        "duration": duration,
        "role": CAPTION_ROLE,
    }
    if not piece.enabled:
        attrib["enabled"] = "0"
    caption = ET.SubElement(parent, "caption", attrib)
    ET.SubElement(caption, "text").text = piece.text


def _title_element(
    parent: ET.Element, media: SourceMedia, piece: TitlePiece, style: TitleStyle
) -> None:
    """타이틀 하나(D91) — 클립 안 시각(원본 격자)에서 시작하고, 자식은 param* → text →
    text-style-def 순서여야 DTD를 통과한다. 글은 그대로(두 줄은 `\n`), 이름은 줄을 이은 글이다."""
    attrib = {
        "ref": style.effect_id,
        "lane": TITLE_LANE,
        "offset": _time(media.timecode_frames + piece.start_frame, media.frame_duration),
        "name": " ".join(line.strip() for line in piece.text.splitlines() if line.strip()),
        "start": TITLE_START,
        "duration": piece.duration,
    }
    if not piece.enabled:
        attrib["enabled"] = "0"
    title = ET.SubElement(parent, "title", attrib)
    for name, key, value in style.params:
        ET.SubElement(title, "param", {"name": name, "key": key, "value": value})
    text = ET.SubElement(title, "text")
    ET.SubElement(text, "text-style", {"ref": piece.style_id}).text = piece.text
    definition = ET.SubElement(title, "text-style-def", {"id": piece.style_id})
    ET.SubElement(definition, "text-style", dict(style.text_style))


def _spine_clip(
    spine: ET.Element,
    ids: tuple[str, str],
    plan: SourcePlan,
    index: int,
    placement: tuple[int, int],
    sequence_fd: Fraction,
    style: TitleStyle | None = None,
) -> None:
    """스파인 클립 하나 — 자식은 conform-rate(timing) · 캡션(anchor) · 키워드(marker) 순서여야
    DTD를 통과한다. `placement`는 시퀀스 프레임의 (위치, 길이)다.

    시퀀스와 레이트가 다른 원본이면 첫 자식에 시간을 지키는 `conform-rate`를 적는다(D71) — 빼면
    Final Cut 기본(scaleEnabled="1")이 프레임을 1:1로 얹어 시간이 바뀐다. 클립 전체의 키워드 뒤에
    묻힌 추천 범위(`recommended`)마다 `추천` 키워드 범위를 단다(D60).
    """
    media, clip = plan.media, plan.clips[index]
    offset, frames = placement
    span = clip.end_frame - clip.start_frame
    attrib = _clip_attrib(*ids, media, clip.start_frame, span, _time(frames, sequence_fd))
    attrib = {"ref": attrib.pop("ref"), "offset": _time(offset, sequence_fd), **attrib}
    if not clip.enabled:
        attrib["enabled"] = "0"
    element = ET.SubElement(spine, "asset-clip", attrib)
    if media.frame_duration != sequence_fd:
        rate = CONFORM_RATES[media.frame_duration]
        ET.SubElement(
            element, "conform-rate", {"scaleEnabled": CONFORM_KEEP_TIME, "srcFrameRate": rate}
        )
    local_end = _local_ends(plan, sequence_fd)[index]
    for piece in plan.captions:
        if piece.clip_index == index:
            _caption_element(element, media, piece, local_end)
    for title in plan.titles:
        if title.clip_index == index and style is not None:
            _title_element(element, media, title, style)
    for keyword in clip.keywords:
        _range_element(element, "keyword", media, clip.start_frame, clip.end_frame, keyword)
    for start, end in clip.recommended:
        _range_element(element, "keyword", media, start, end, KEYWORD_RECOMMENDED)


def _sequence(project: ET.Element, document: TimelineDocument, total_frames: int) -> ET.Element:
    fd = document.sequence_frame_duration
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
    spine = _sequence(project, document, sequence_frame_total(document))
    sequence_fd = document.sequence_frame_duration
    offset = 0
    for plan, pair in zip(document.sources, ids, strict=True):
        for index, frames in enumerate(_sequence_frames(plan)):
            placement = (offset, frames)
            _spine_clip(spine, pair, plan, index, placement, sequence_fd, document.title_style)
            offset += frames


def render(document: TimelineDocument) -> bytes:
    """문서 → FCPXML 1.14 바이트. 같은 문서면 같은 바이트다(속성은 넣은 순서 그대로 나온다)."""
    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    _import_options(root, document.library_location)
    resources = ET.SubElement(root, "resources")
    for fmt in document.formats:
        _format_element(resources, fmt)
    ids = _source_ids(document)
    for plan, (asset_id, format_id) in zip(document.sources, ids, strict=True):
        _asset_element(resources, asset_id, format_id, plan.media)
    style = document.title_style
    if style is not None:
        effect = {"id": style.effect_id, "name": style.effect_name, "uid": style.effect_uid}
        ET.SubElement(resources, "effect", effect)
    location = {} if document.library_location is None else {"location": document.library_location}
    _event(ET.SubElement(root, "library", location), document, ids)
    ET.indent(root, space=_INDENT)
    return (_XML_HEADER + ET.tostring(root, encoding="unicode") + "\n").encode("utf-8")
