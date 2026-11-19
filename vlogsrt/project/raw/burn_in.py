"""자막 입히기(번인, W-65 D116)의 순수 조판 — 한국어·영어 SRT 큐 → ASS 문서 하나.

시각은 SRT가 준 값을 ASS의 1/100초로 적을 뿐 만들거나 옮기지 않는다(규칙 1·3) — 영어 SRT는
한국어 SRT와 큐 수·시작·끝이 모두 같을 때만 짝짓고(D114가 그렇게 쓴다), 하나라도 다르면 첫 다른
번호를 대고 거부한다. 맞추거나 짐작하지 않는다. 짝지은 큐 하나가 Dialogue 하나다 — 한글 줄 아래에
영어 줄을 `\\N{\\rEN}`으로 이어 한 덩어리로 놓으므로 긴 줄이 접혀도 두 언어가 겹치지 않는다.
둘 다 빈 큐만 건너뛰고 센다(규칙 9 — 조용히 사라지지 않는다).

파일·프로세스·서비스를 모른다 — `burn_export`가 값을 넘기고 ASS 글을 받는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.raw.burn_config import BURN_POSITIONS, BURN_SETTINGS, HEX_COLOR, BurnSettings
from vlogsrt.project.raw.finalcut_timeline import Cue

KOREAN_STYLE = "KO"
ENGLISH_STYLE = "EN"
_ALIGNMENT = {"bottom": 2, "top": 8}
"""ASS 숫자판 정렬 — 2 = 아래 가운데, 8 = 위 가운데."""
_CENTIS = 100
_PREVIEW_END = "9:59:59.99"
"""미리보기 한 장의 사건 끝 — 찾기(`-ss`)가 시각을 0으로 되돌려도 늘 보이게 한다."""
_WORD_JOINER = "⁠"
"""백슬래시 뒤에 끼워 `\\n`·`\\N`·`\\h`·`\\{` 같은 ASS 탈출이 되지 않게 한다(보이지 않는다)."""
_STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
    " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
    " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
_EVENT_FORMAT = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"


class BurnInError(ValueError):
    """조판할 수 없다 — 모양 값이 범위 밖이거나 두 SRT가 짝이 맞지 않는다."""


@dataclass(frozen=True, slots=True)
class BurnStyle:
    """사람이 고르는 네 가지 — 자리·글자 크기·외곽선·글자색(+ 자리에서 가장자리까지 여백).

    크기·외곽선·여백은 1080p 기준 픽셀이고 영상의 짧은 변에 비례한다
    (`BurnSettings.reference_height`, D171)."""

    position: str
    font_px: int
    outline_px: int
    margin_px: int
    text_color: str


@dataclass(frozen=True, slots=True)
class BurnEvent:
    """화면에 올릴 큐 하나 — 시각(초), 한글 줄, 영어 줄(없으면 빈 글)."""

    start: Fraction
    end: Fraction
    korean: str
    english: str


@dataclass(frozen=True, slots=True)
class BurnPairing:
    """짝지은 결과 — 올릴 사건, 읽은 큐 수, 둘 다 비어 건너뛴 큐 수, 영어가 있었는가."""

    events: tuple[BurnEvent, ...]
    cues: int
    empty: int
    english: bool


def default_style(settings: BurnSettings = BURN_SETTINGS) -> BurnStyle:
    return BurnStyle(
        settings.default_position,
        settings.default_font_px,
        settings.default_outline_px,
        settings.default_margin_px,
        settings.default_text_color.upper(),
    )


def style_from_mapping(
    raw: Mapping[str, object], settings: BurnSettings = BURN_SETTINGS
) -> BurnStyle:
    """저장값·화면 값 → 모양. 없는 칸은 기본값, 범위 밖·꼴이 다른 값은 이름을 대고 거부한다."""
    base = default_style(settings)
    position = str(raw.get("position", base.position))
    if position not in BURN_POSITIONS:
        raise BurnInError(
            f"자막 자리는 {', '.join(BURN_POSITIONS)} 가운데 하나입니다: {position!r}"
        )
    color = str(raw.get("text_color", base.text_color))
    if not HEX_COLOR.fullmatch(color):
        raise BurnInError(f"글자색은 #RRGGBB 꼴이어야 합니다: {color!r}")
    return BurnStyle(
        position,
        _whole(raw, "font_px", base.font_px, settings.font_px_range, "글자 크기"),
        _whole(raw, "outline_px", base.outline_px, settings.outline_px_range, "외곽선"),
        _whole(raw, "margin_px", base.margin_px, settings.margin_px_range, "여백"),
        color.upper(),
    )


def _whole(
    raw: Mapping[str, object], key: str, default: int, bounds: tuple[int, int], label: str
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise BurnInError(f"{label}는 정수여야 합니다: {value!r}")
    try:
        number = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as error:
        raise BurnInError(f"{label}는 정수여야 합니다: {value!r}") from error
    if number.denominator != 1:
        raise BurnInError(f"{label}는 정수여야 합니다: {value!r}")
    low, high = bounds
    if not low <= number <= high:
        raise BurnInError(f"{label}는 {low}–{high}px 사이여야 합니다: {value}")
    return int(number)


def style_to_mapping(style: BurnStyle) -> dict[str, object]:
    return {
        "position": style.position,
        "font_px": style.font_px,
        "outline_px": style.outline_px,
        "margin_px": style.margin_px,
        "text_color": style.text_color,
    }


# ── 짝짓기 ──────────────────────────────────────────────────────────────


def pair_cues(korean: Sequence[Cue], english: Sequence[Cue] | None) -> BurnPairing:
    """한국어 큐(+ 같은 시각의 영어 큐) → 올릴 사건. 영어가 없으면 한글만이다."""
    if english is not None:
        _require_same_timing(korean, english)
    events: list[BurnEvent] = []
    for index, cue in enumerate(korean):
        other = english[index].text.strip() if english is not None else ""
        if not cue.text.strip() and not other:
            continue
        events.append(BurnEvent(cue.start, cue.end, cue.text.strip(), other))
    return BurnPairing(tuple(events), len(korean), len(korean) - len(events), english is not None)


def _require_same_timing(korean: Sequence[Cue], english: Sequence[Cue]) -> None:
    if len(korean) != len(english):
        raise BurnInError(
            f"한국어 SRT는 큐 {len(korean)}개, 영어 SRT는 {len(english)}개입니다 — 같은 SRT에서"
            " 옮긴 영어 SRT를 고르세요"
        )
    for number, (ko, en) in enumerate(zip(korean, english, strict=True), start=1):
        if (ko.start, ko.end) != (en.start, en.end):
            raise BurnInError(
                f"{number}번 큐의 시각이 다릅니다 — 한국어 {_span(ko)}, 영어 {_span(en)}."
                " 같은 SRT에서 옮긴 영어 SRT를 고르세요"
            )


def _span(cue: Cue) -> str:
    return f"{float(cue.start):.3f}–{float(cue.end):.3f}초"


def preview_event(pairing: BurnPairing) -> BurnEvent | None:
    """미리보기에 쓸 사건 — 처음으로 한글이 있는 것, 없으면 첫 사건."""
    for event in pairing.events:
        if event.korean:
            return event
    return pairing.events[0] if pairing.events else None


# ── ASS ─────────────────────────────────────────────────────────────────


def ass_document(
    pairing: BurnPairing,
    style: BurnStyle,
    width: int,
    height: int,
    settings: BurnSettings = BURN_SETTINGS,
) -> str:
    """번인할 ASS 글 — 화면 좌표는 영상 크기 그대로(`PlayResX/Y`)라 비율이 실제 픽셀이 된다."""
    lines = [_dialogue(_ass_time(e.start), _ass_time(e.end), e) for e in pairing.events]
    return _document(style, width, height, settings, lines)


def preview_document(
    event: BurnEvent,
    style: BurnStyle,
    width: int,
    height: int,
    settings: BurnSettings = BURN_SETTINGS,
) -> str:
    """미리보기 한 장의 ASS — 고른 사건 하나가 처음부터 끝까지 보인다."""
    return _document(style, width, height, settings, [_dialogue("0:00:00.00", _PREVIEW_END, event)])


def _document(
    style: BurnStyle, width: int, height: int, settings: BurnSettings, events: list[str]
) -> str:
    if width < 1 or height < 1:
        raise BurnInError(f"영상 크기를 알 수 없습니다: {width}×{height}")
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        _STYLE_FORMAT,
        _style_line(KOREAN_STYLE, style, Fraction(1), width, height, settings),
        _style_line(ENGLISH_STYLE, style, settings.english_scale, width, height, settings),
        "",
        "[Events]",
        _EVENT_FORMAT,
    ]
    return "\n".join([*head, *events]) + "\n"


def _style_line(
    name: str, style: BurnStyle, scale: Fraction, width: int, height: int, s: BurnSettings
) -> str:
    # 짧은 변으로 잰다(D171) — 높이로 재면 세로 2160×3840에서 글자가 너비의 9%가 된다.
    unit = Fraction(min(width, height), s.reference_height)
    size = max(1, round(style.font_px * scale * unit))
    outline = round(style.outline_px * scale * unit)
    side = round(width * s.side_margin_ratio)
    margin = round(style.margin_px * unit)
    fill, edge = _ass_color(style.text_color), _ass_color(s.outline_color)
    return (
        f"Style: {name},{s.font_name},{size},{fill},{fill},{edge},{edge},"
        f"0,0,0,0,100,100,0,0,1,{outline},0,{_ALIGNMENT[style.position]},{side},{side},{margin},1"
    )


def _dialogue(start: str, end: str, event: BurnEvent) -> str:
    if event.korean and event.english:
        text = f"{_escape(event.korean)}\\N{{\\r{ENGLISH_STYLE}}}{_escape(event.english)}"
        first = KOREAN_STYLE
    elif event.korean:
        text, first = _escape(event.korean), KOREAN_STYLE
    else:
        text, first = _escape(event.english), ENGLISH_STYLE
    return f"Dialogue: 0,{start},{end},{first},,0,0,0,,{text}"


def _escape(text: str) -> str:
    """SRT 글 → ASS 글. 줄바꿈은 `\\N`, 백슬래시·중괄호는 글자 그대로 보이게 한다."""
    escaped = text.replace("\\", "\\" + _WORD_JOINER)
    escaped = escaped.replace("{", "\\{").replace("}", "\\}")
    rows = [row.strip() for row in escaped.replace("\r\n", "\n").split("\n")]
    return "\\N".join(row for row in rows if row)


def _ass_time(seconds: Fraction) -> str:
    """초 → `H:MM:SS.cc`. 1/100초로 가장 가까이 반올림한다(표시 양자화 — SRT는 그대로다)."""
    centis = max(0, int(Fraction(seconds * _CENTIS) + Fraction(1, 2)))
    hours, rest = divmod(centis, 3600 * _CENTIS)
    minutes, rest = divmod(rest, 60 * _CENTIS)
    whole, part = divmod(rest, _CENTIS)
    return f"{hours}:{minutes:02d}:{whole:02d}.{part:02d}"


def _ass_color(hex_color: str) -> str:
    """`#RRGGBB` → ASS `&H00BBGGRR`(알파 00 = 불투명)."""
    red, green, blue = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f"&H00{blue}{green}{red}".upper()
