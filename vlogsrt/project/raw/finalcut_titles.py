"""SRT 줄마다 Final Cut 타이틀 — 타이틀만 있는 프로젝트의 순수 빌더(W-30f, D90).

캡션(`<caption>`)은 타이틀 템플릿·글꼴·Inspector로 꾸밀 수 없다. 그래서 줄마다 **타이틀 클립**
(기본 Basic Title)을 만든다 — Vrew가 FCPXML로 내보내는 모양이다. 문서는 스파인에 빈 gap 하나를 두고
줄마다 레인 1에 타이틀을 붙인다. 사람은 Final Cut에서 그 타이틀을 모두 복사해 편집 프로젝트에 붙이고
템플릿·글꼴을 한꺼번에 바꾼다.

입력은 값뿐이다(타임라인 시각의 큐 · 포맷 · 모양 설정). 파일·프로세스·서비스를 모른다. 지키는 선:

- **글은 한 자도 고치지 않는다.** 두 줄은 `\\n` 그대로 한 타이틀에 들어간다.
- **시각은 SRT에서만 온다.** 프레임 격자에 맞출 뿐이다(`caption_pieces`와 같은 `_snapped` — 0프레임·
  뒤집힌 줄은 시작에 1프레임).
- **한 레인에 두 클립을 둘 수 없다.** Final Cut은 겹친 클립을 위 레인으로 밀어 계단을 만든다(Vrew의
  실제 버그). 그래서 겹친 줄은 끝을 다음 줄 시작으로 줄이고 센다 — 「고치지 않고 센다」의 하나뿐인
  의도된 예외다. 같은 프레임에서 시작한 줄은 줄일 길이가 없어 둘 다 두고 센다(위 레인으로 간다).
- **지우는 줄은 없다.** 빈 글만 싣지 않고 센다. 실을 줄이 하나도 없으면 거부한다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.raw.config import (
    FINALCUT_TITLES,
    FinalCutTitleFormat,
    FinalCutTitleSettings,
)
from vlogsrt.project.raw.finalcut_timeline import (
    _INDENT,
    _XML_FORBIDDEN,
    _XML_HEADER,
    FCPXML_VERSION,
    SEQUENCE_TC_START,
    Cue,
    FinalCutTimelineError,
    FormatPlan,
    _cue_time,
    _format_element,
    _import_options,
    _line_issue,
    _snapped,
    _tc_format,
    _time,
)

ANCHOR_SECONDS = 3600
"""gap·타이틀의 안쪽 시계 시작 — Final Cut 자신의 철자(`start="3600s"`). 타이틀 offset은 gap의 안쪽
시계라 `3600s + 줄 시작`이다."""
ANCHOR_START = f"{ANCHOR_SECONDS}s"
TITLE_LANE = "1"
GAP_NAME = "Gap"
FORMAT_ID = "r1"
EFFECT_ID = "r2"
_STYLE_ID = "ts{index}"
"""`text-style-def@id`는 XML ID라 문서 전체에서 유일해야 한다 — 타이틀마다 하나."""
_POSITION_KEY = "9999/999166631/999166633/1/100/101"
_PARAMS = (
    ("Flatten", "9999/999166631/999166633/2/351", "1"),
    ("Alignment", "9999/999166631/999166633/2/354/999169573/401", "1 (Center)"),
)
"""Basic Title(FCP 12.4 `.moti`의 장면 id)의 매개변수 열쇠 — Final Cut 자신의 내보내기 철자다."""
_ALIGNMENT = "center"


@dataclass(frozen=True, slots=True)
class TitlePlan:
    """타이틀 하나 — 시퀀스 프레임 `[start_frame, end_frame)`와 글(고치지 않은 그대로)."""

    start_frame: int
    end_frame: int
    text: str


@dataclass(frozen=True, slots=True)
class TitleCounts:
    """만들며 센 것. `cues = empty + placed`. `widened`·`inverted`는 1프레임으로 둔 줄,
    `trimmed`는 다음 줄 시작에서 끝을 줄인 줄, `stacked`는 앞 줄과 같은 프레임에서 시작한 줄(Final
    Cut이 위 레인으로 올린다), `frames`는 시퀀스 길이(마지막 끝)다."""

    cues: int = 0
    placed: int = 0
    empty: int = 0
    widened: int = 0
    inverted: int = 0
    trimmed: int = 0
    stacked: int = 0
    line_issues: int = 0
    frames: int = 0


@dataclass(frozen=True, slots=True)
class TitleDocument:
    title: str
    format: FinalCutTitleFormat
    settings: FinalCutTitleSettings
    titles: tuple[TitlePlan, ...]
    frames: int


def _half_up(value: Fraction) -> int:
    return int((value + Fraction(1, 2)) // 1)


def font_size(fmt: FinalCutTitleFormat, settings: FinalCutTitleSettings) -> int:
    return _half_up(fmt.height * settings.font_size_fraction)


def position_value(fmt: FinalCutTitleFormat, settings: FinalCutTitleSettings) -> str:
    """Basic Title의 Position — 캔버스 픽셀, 가운데 원점, 아래가 음수다."""
    return f"0 {-_half_up(fmt.height * settings.position_y_fraction)}"


def anchored_time(frames: int, frame_duration: Fraction) -> str:
    """gap 안쪽 시계의 시각 `3600s + frames × 프레임 길이` — 프레임 길이의 분모를 그대로 둔다."""
    if frames == 0:
        return ANCHOR_START
    numerator = ANCHOR_SECONDS * frame_duration.denominator + frames * frame_duration.numerator
    if frame_duration.denominator == 1:
        return f"{numerator}s"
    return f"{numerator}/{frame_duration.denominator}s"


# ── 계획 ─────────────────────────────────────────────────────────────────


def _snap_all(
    cues: Sequence[Cue], fd: Fraction, counts: Counter[str]
) -> list[tuple[int, int, str]]:
    """빈 글은 세고 빼고, 나머지는 프레임에 맞춘다(시작·끝 순).

    XML이 받지 않는 글은 그 줄 시각을 대고 거부한다.
    """
    snapped: list[tuple[int, int, str]] = []
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
        snapped.append((start, end, cue.text))
    return sorted(snapped, key=lambda row: (row[0], row[1]))


def _one_lane(snapped: list[tuple[int, int, str]], counts: Counter[str]) -> list[TitlePlan]:
    """겹친 줄은 끝을 다음 줄(시작이 더 늦은 첫 줄) 시작으로 줄인다 — 늘 1프레임 이상 남는다. 같은
    프레임에서 시작한 줄은 줄일 수 없어 그대로 두고 센다."""
    plans: list[TitlePlan] = []
    for index, (start, end, text) in enumerate(snapped):
        later = next((row[0] for row in snapped[index + 1 :] if row[0] > start), None)
        if later is not None and end > later:
            end = later
            counts["trimmed"] += 1
        counts["stacked"] += index > 0 and snapped[index - 1][0] == start
        plans.append(TitlePlan(start, end, text))
    return plans


def plan_titles(
    cues: Sequence[Cue],
    fmt: FinalCutTitleFormat,
    *,
    title: str,
    settings: FinalCutTitleSettings = FINALCUT_TITLES,
) -> tuple[TitleDocument, TitleCounts]:
    """타임라인 시각(0초 = 시퀀스 처음)의 큐 → 타이틀 문서와 센 수. 실을 줄이 없으면 거부한다."""
    if not title.strip() or _XML_FORBIDDEN.search(title):
        raise FinalCutTimelineError(f"이벤트·프로젝트 이름을 쓸 수 없다: {title!r}")
    counts: Counter[str] = Counter()
    plans = _one_lane(_snap_all(cues, fmt.frame_duration, counts), counts)
    if not plans:
        raise FinalCutTimelineError(f"실을 줄이 없다 — {counts['cues']}줄이 모두 빈 글이다")
    frames = max(plan.end_frame for plan in plans)
    counts.update(placed=len(plans), frames=frames)
    document = TitleDocument(title, fmt, settings, tuple(plans), frames)
    return document, TitleCounts(**counts)


def document_style(document: TitleDocument) -> dict[str, object]:
    """리포트에 남길 모양 — 실제로 문서에 적은 값 그대로."""
    fmt, settings = document.format, document.settings
    return {
        "font": settings.font,
        "font_face": settings.font_face,
        "font_size": font_size(fmt, settings),
        "font_color": settings.font_color,
        "stroke_color": settings.stroke_color,
        "position": position_value(fmt, settings),
    }


# ── 문서 ─────────────────────────────────────────────────────────────────


def _resources(root: ET.Element, document: TitleDocument) -> None:
    fmt, settings = document.format, document.settings
    resources = ET.SubElement(root, "resources")
    plan = FormatPlan(FORMAT_ID, fmt.width, fmt.height, fmt.frame_duration, fmt.color_space)
    _format_element(resources, plan)
    ET.SubElement(
        resources,
        "effect",
        {"id": EFFECT_ID, "name": settings.effect_name, "uid": settings.effect_uid},
    )


def _style_attrib(document: TitleDocument) -> dict[str, str]:
    fmt, settings = document.format, document.settings
    attrib = {
        "font": settings.font,
        "fontSize": str(font_size(fmt, settings)),
        "fontFace": settings.font_face,
        "fontColor": settings.font_color,
    }
    if settings.stroke_color is not None:
        attrib["strokeColor"] = settings.stroke_color
        attrib["strokeWidth"] = str(_half_up(fmt.height * settings.stroke_width_fraction))
    attrib["alignment"] = _ALIGNMENT
    return attrib


def _title_element(gap: ET.Element, document: TitleDocument, plan: TitlePlan, index: int) -> None:
    """타이틀 하나 — 자식은 param* → text → text-style-def 순서여야 DTD를 통과한다."""
    fd = document.format.frame_duration
    attrib = {
        "ref": EFFECT_ID,
        "lane": TITLE_LANE,
        "offset": anchored_time(plan.start_frame, fd),
        "name": " ".join(line.strip() for line in plan.text.splitlines() if line.strip()),
        "start": ANCHOR_START,
        "duration": _time(plan.end_frame - plan.start_frame, fd),
    }
    title = ET.SubElement(gap, "title", attrib)
    position = position_value(document.format, document.settings)
    ET.SubElement(title, "param", {"name": "Position", "key": _POSITION_KEY, "value": position})
    for name, key, value in _PARAMS:
        ET.SubElement(title, "param", {"name": name, "key": key, "value": value})
    style_id = _STYLE_ID.format(index=index)
    text = ET.SubElement(title, "text")
    ET.SubElement(text, "text-style", {"ref": style_id}).text = plan.text
    definition = ET.SubElement(title, "text-style-def", {"id": style_id})
    ET.SubElement(definition, "text-style", _style_attrib(document))


def _gap(project: ET.Element, document: TitleDocument) -> ET.Element:
    fd = document.format.frame_duration
    duration = _time(document.frames, fd)
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": FORMAT_ID,
            "duration": duration,
            "tcStart": SEQUENCE_TC_START,
            "tcFormat": _tc_format(document.format.drop_frame),
        },
    )
    spine = ET.SubElement(sequence, "spine")
    attrib = {"name": GAP_NAME, "offset": "0s", "start": ANCHOR_START, "duration": duration}
    return ET.SubElement(spine, "gap", attrib)


def render_titles(document: TitleDocument) -> bytes:
    """문서 → FCPXML 1.14 바이트. 같은 문서면 같은 바이트다."""
    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    _import_options(root, None)
    _resources(root, document)
    event = ET.SubElement(ET.SubElement(root, "library"), "event", {"name": document.title})
    project = ET.SubElement(event, "project", {"name": document.title})
    gap = _gap(project, document)
    for index, plan in enumerate(document.titles, start=1):
        _title_element(gap, document, plan, index)
    ET.indent(root, space=_INDENT)
    return (_XML_HEADER + ET.tostring(root, encoding="unicode") + "\n").encode("utf-8")
