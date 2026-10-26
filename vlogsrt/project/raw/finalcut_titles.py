"""SRT 줄마다 Final Cut 타이틀 — 영상 타임라인 위에 Basic Title을 다는 순수 계층(W-30f, D91).

캡션(`<caption>`)은 타이틀 템플릿·글꼴·Inspector로 꾸밀 수 없다. 그래서 SRT 줄마다 **타이틀 클립**
(기본 Basic Title)을 만든다 — 작업대가 보내는 그 전체 타임라인(`finalcut_timeline.plan`의 문서)의
스파인 클립에 레인 1로 붙인다. 사람은 Final Cut에서 영상과 타이틀이 함께 있는 프로젝트를 받고, 붙은
타이틀을 모두 골라 템플릿·글꼴을 한꺼번에 바꾼다.

입력은 값뿐이다(계획된 문서 · 타임라인 시각의 큐 · 모양 설정). 파일·프로세스·서비스를 모른다.
지키는 선:

- **글은 한 자도 고치지 않는다.** 두 줄은 `\\n` 그대로 한 타이틀에 들어간다.
- **시각은 SRT에서만 온다.** 시퀀스 프레임 격자에 맞출 뿐이다(`caption_pieces`와 같은 `_snapped`).
  줄은 그 줄이 시작하는 스파인 클립에 붙고(꺼진 클립도 시간을 차지한다), 시작은 그 클립 안 원본
  시각, 길이는 시퀀스 시간이다 — 컷을 넘는 줄도 나누지 않는다.
- **한 레인에 두 클립을 둘 수 없다.** Final Cut은 겹친 클립을 위 레인으로 밀어 계단을 만든다(Vrew의
  실제 버그). 그래서 겹친 줄은 끝을 다음 줄 시작으로 줄이고 센다 — 「고치지 않고 센다」의 의도된
  예외다. 같은 프레임에서 시작한 줄은 줄일 길이가 없어 둘 다 두고 센다(위 레인으로 간다). 타임라인
  끝을 넘는 꼬리는 자르고, 통째로 밖이면 붙일 클립이 없어 싣지 않고 센다.
- **지우는 줄은 없다.** 빈 글·밖의 줄만 싣지 않고 센다. 실을 줄이 하나도 없으면 거부한다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from vlogsrt.project.raw.config import FINALCUT_TITLES, FinalCutTitleSettings
from vlogsrt.project.raw.finalcut_timeline import (
    _XML_FORBIDDEN,
    Cue,
    FinalCutTimelineError,
    SourcePlan,
    TimelineDocument,
    TitlePiece,
    TitleStyle,
    _cue_time,
    _line_issue,
    _sequence_frames,
    _snapped,
    _time,
    sequence_frame_total,
)
from vlogsrt.project.timebase import frame_nearest

EFFECT_ID = "e1"
"""타이틀 템플릿 effect의 자원 id — 포맷(`r…`)·원본(`a…`) id와 겹치지 않는다."""
_STYLE_ID = "ts{index}"
_POSITION = ("Position", "9999/999166631/999166633/1/100/101")
_PARAMS = (
    ("Flatten", "9999/999166631/999166633/2/351", "1"),
    ("Alignment", "9999/999166631/999166633/2/354/999169573/401", "1 (Center)"),
)
"""Basic Title(FCP 12.4 `.moti`의 장면 id)의 매개변수 열쇠 — Final Cut 자신의 내보내기 철자다."""
_ALIGNMENT = "center"


@dataclass(frozen=True, slots=True)
class TitleCounts:
    """만들며 센 것. `cues = empty + outside + placed`. `widened`·`inverted`는 1프레임으로 둔 줄,
    `trimmed`는 다음 줄 시작에서 끝을 줄인 줄, `stacked`는 앞 줄과 같은 프레임에서 시작한 줄(Final
    Cut이 위 레인으로 올린다), `tails`는 타임라인 끝에서 자른 줄, `disabled`는 꺼진 클립에 붙어
    함께 꺼진 타이틀이다."""

    cues: int = 0
    placed: int = 0
    empty: int = 0
    outside: int = 0
    widened: int = 0
    inverted: int = 0
    trimmed: int = 0
    stacked: int = 0
    tails: int = 0
    disabled: int = 0
    line_issues: int = 0


@dataclass(frozen=True, slots=True)
class _Line:
    start_frame: int
    end_frame: int
    text: str


@dataclass(frozen=True, slots=True)
class _Slot:
    source: int
    clip: int
    offset: int
    frames: int


def _half_up(value: Fraction) -> int:
    return int((value + Fraction(1, 2)) // 1)


def font_size(height: int, settings: FinalCutTitleSettings) -> int:
    return _half_up(height * settings.font_size_fraction)


def position_value(height: int, settings: FinalCutTitleSettings) -> str:
    """Basic Title의 Position — 캔버스 픽셀, 가운데 원점, 아래가 음수다."""
    return f"0 {-_half_up(height * settings.position_y_fraction)}"


def title_style(height: int, settings: FinalCutTitleSettings) -> TitleStyle:
    """시퀀스 높이에 맞춘 템플릿·모양 — 크기·위치는 높이 몫이라 세로 프로젝트도 같은 비율이다."""
    text_style = [
        ("font", settings.font),
        ("fontSize", str(font_size(height, settings))),
        ("fontFace", settings.font_face),
        ("fontColor", settings.font_color),
    ]
    if settings.stroke_color is not None:
        stroke = str(_half_up(height * settings.stroke_width_fraction))
        text_style += [("strokeColor", settings.stroke_color), ("strokeWidth", stroke)]
    text_style.append(("alignment", _ALIGNMENT))
    position = (*_POSITION, position_value(height, settings))
    return TitleStyle(
        effect_id=EFFECT_ID,
        effect_name=settings.effect_name,
        effect_uid=settings.effect_uid,
        params=(position, *_PARAMS),
        text_style=tuple(text_style),
    )


# ── 줄 ───────────────────────────────────────────────────────────────────


def _snap_all(cues: Sequence[Cue], fd: Fraction, counts: Counter[str]) -> list[_Line]:
    """빈 글은 세고 빼고, 나머지는 시퀀스 프레임에 맞춘다(시작·끝 순).

    XML이 받지 않는 글은 그 줄 시각을 대고 거부한다.
    """
    lines: list[_Line] = []
    for cue in sorted(cues, key=lambda row: (row.start, row.end)):
        counts["cues"] += 1
        if not cue.text.strip():
            counts["empty"] += 1
            continue
        if _XML_FORBIDDEN.search(cue.text):
            raise FinalCutTimelineError(
                f"{_cue_time(cue.start)} 자막 글에 XML이 받지 않는 문자가 있다: {cue.text!r}"
            )
        start, end, repair = _snapped(cue, fd)
        if repair is not None:
            counts[repair] += 1
        counts["line_issues"] += _line_issue(cue.text)
        lines.append(_Line(start, end, cue.text))
    return sorted(lines, key=lambda line: (line.start_frame, line.end_frame))


def _one_lane(lines: list[_Line], counts: Counter[str]) -> list[_Line]:
    """겹친 줄은 끝을 다음 줄(시작이 더 늦은 첫 줄) 시작으로 줄인다 — 늘 1프레임 이상 남는다. 같은
    프레임에서 시작한 줄은 줄일 수 없어 그대로 두고 센다."""
    kept: list[_Line] = []
    for index, line in enumerate(lines):
        starts = (row.start_frame for row in lines[index + 1 :])
        later = next((start for start in starts if start > line.start_frame), None)
        if later is not None and line.end_frame > later:
            line = replace(line, end_frame=later)
            counts["trimmed"] += 1
        counts["stacked"] += index > 0 and lines[index - 1].start_frame == line.start_frame
        kept.append(line)
    return kept


# ── 클립에 붙이기 ────────────────────────────────────────────────────────


def _slots(document: TimelineDocument) -> list[_Slot]:
    """렌더링이 스파인 클립을 놓는 순서·자리 그대로 — 꺼진 클립도 시간을 차지한다."""
    slots: list[_Slot] = []
    offset = 0
    for source, plan in enumerate(document.sources):
        for clip, frames in enumerate(_sequence_frames(plan)):
            slots.append(_Slot(source, clip, offset, frames))
            offset += frames
    return slots


def _local_start(plan: SourcePlan, slot: _Slot, frame: int, sequence_fd: Fraction) -> int:
    """시퀀스 프레임 → 붙을 클립 안 원본 프레임. 레이트가 다른 원본은 클립 안 **시각**이 그대로
    시퀀스 시각으로 옮겨지므로(`scaleEnabled="0"`, D71) 시각으로 옮겨 원본 격자에 맞춘다."""
    clip, fd = plan.clips[slot.clip], plan.media.frame_duration
    delta = frame - slot.offset
    if fd == sequence_fd:
        return clip.start_frame + delta
    return min(clip.start_frame + frame_nearest(delta * sequence_fd, fd), clip.end_frame - 1)


def _place(
    document: TimelineDocument, lines: list[_Line], counts: Counter[str]
) -> list[list[TitlePiece]]:
    """줄마다 시작이 든 스파인 클립에 붙인다 — 끝은 타임라인 끝까지, 통째로 밖이면 세고 뺀다."""
    sequence_fd = document.sequence_frame_duration
    total = sequence_frame_total(document)
    slots = _slots(document)
    pieces: list[list[TitlePiece]] = [[] for _ in document.sources]
    for line in lines:
        if line.start_frame >= total:
            counts["outside"] += 1
            continue
        end = min(line.end_frame, total)
        counts["tails"] += end != line.end_frame
        slot = next(s for s in slots if s.offset <= line.start_frame < s.offset + s.frames)
        plan = document.sources[slot.source]
        enabled = plan.clips[slot.clip].enabled
        counts["disabled"] += not enabled
        counts["placed"] += 1
        start = _local_start(plan, slot, line.start_frame, sequence_fd)
        duration = _time(end - line.start_frame, sequence_fd)
        style_id = _STYLE_ID.format(index=counts["placed"])
        piece = TitlePiece(slot.clip, start, duration, line.text, enabled, style_id)
        pieces[slot.source].append(piece)
    return pieces


def _sequence_height(document: TimelineDocument) -> int:
    return next(f.height for f in document.formats if f.format_id == document.sequence_format)


def with_srt_titles(
    document: TimelineDocument,
    cues: Sequence[Cue],
    settings: FinalCutTitleSettings = FINALCUT_TITLES,
) -> tuple[TimelineDocument, TitleCounts]:
    """같은 문서(원본·클립·키워드·켜짐·포맷·레이트)에 원본 자막 캡션 대신 SRT 줄의 타이틀만 싣는다.

    SRT 시각은 타임라인 시각(0초 = 시퀀스 처음)이다. 실을 줄이 하나도 없으면 거부한다.
    """
    counts: Counter[str] = Counter()
    lines = _one_lane(_snap_all(cues, document.sequence_frame_duration, counts), counts)
    pieces = _place(document, lines, counts)
    if not counts["placed"]:
        raise FinalCutTimelineError(
            f"실을 줄이 없다 — {counts['cues']}줄 가운데 빈 글 {counts['empty']}줄 ·"
            f" 타임라인 밖 {counts['outside']}줄"
        )
    sources = tuple(
        replace(plan, captions=(), caption_counts=None, titles=tuple(row))
        for plan, row in zip(document.sources, pieces, strict=True)
    )
    style = title_style(_sequence_height(document), settings)
    return replace(document, sources=sources, title_style=style), TitleCounts(**counts)


def style_facts(document: TimelineDocument) -> dict[str, object]:
    """리포트에 남길 모양 — 실제로 문서에 적은 값 그대로."""
    style = document.title_style
    if style is None:
        return {}
    position = next(value for name, _, value in style.params if name == _POSITION[0])
    return {**dict(style.text_style), "position": position}
