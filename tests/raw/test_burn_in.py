"""자막 입히기 순수 조판(W-65, D116) — SRT 큐 → ASS."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from vlogsrt.project.raw import burn_in
from vlogsrt.project.raw.burn_config import BURN_SETTINGS, BurnSettings
from vlogsrt.project.raw.finalcut_timeline import Cue


def _cue(start: str, end: str, text: str) -> Cue:
    return Cue(Fraction(start), Fraction(end), text)


KO = (
    _cue("1", "2.5", "오늘은 제주도"),
    _cue("3", "4.005", ""),
    _cue("5", "6", "{괄호} 백\\n슬래시"),
)
EN = (
    _cue("1", "2.5", "Today, Jeju"),
    _cue("3", "4.005", ""),
    _cue("5", "6", ""),
)


def _style(**changes: object) -> burn_in.BurnStyle:
    return replace(burn_in.default_style(), **changes)


def _events(document: str) -> list[str]:
    return [line for line in document.splitlines() if line.startswith("Dialogue:")]


def _styles(document: str) -> dict[str, list[str]]:
    rows = [line.removeprefix("Style: ").split(",") for line in document.splitlines()]
    return {row[0]: row for row in rows if len(row) == 23}


def test_default_style_is_the_settings() -> None:
    style = burn_in.default_style()
    assert style == burn_in.BurnStyle("bottom", 54, 4, 60, "#FFFFFF")


def test_style_from_mapping_fills_defaults_and_validates() -> None:
    style = burn_in.style_from_mapping(
        {"position": "top", "font_px": "72", "text_color": "#ffcc00"}
    )
    assert style == burn_in.BurnStyle("top", 72, 4, 60, "#FFCC00")
    assert burn_in.style_from_mapping(burn_in.style_to_mapping(style)) == style
    for bad in (
        {"position": "middle"},
        {"font_px": 7},
        {"font_px": 54.5},
        {"outline_px": 99},
        {"margin_px": -1},
        {"text_color": "white"},
        {"font_px": True},
    ):
        with pytest.raises(burn_in.BurnInError):
            burn_in.style_from_mapping(bad)


def test_pairing_keeps_timing_and_counts_empty() -> None:
    pairing = burn_in.pair_cues(KO, EN)
    assert pairing.cues == 3 and pairing.empty == 1 and pairing.english
    assert [(e.start, e.korean, e.english) for e in pairing.events] == [
        (Fraction(1), "오늘은 제주도", "Today, Jeju"),
        (Fraction(5), "{괄호} 백\\n슬래시", ""),
    ]


def test_pairing_refuses_count_or_time_mismatch() -> None:
    with pytest.raises(burn_in.BurnInError, match="큐 3개"):
        burn_in.pair_cues(KO, EN[:2])
    shifted = (EN[0], EN[1], _cue("5.001", "6", "x"))
    with pytest.raises(burn_in.BurnInError, match="3번 큐"):
        burn_in.pair_cues(KO, shifted)


def test_korean_only_without_english() -> None:
    pairing = burn_in.pair_cues(KO, None)
    assert not pairing.english and pairing.empty == 1
    document = burn_in.ass_document(pairing, burn_in.default_style(), 1920, 1080)
    assert "\\r" not in "".join(_events(document))


def test_ass_document_shapes_events() -> None:
    pairing = burn_in.pair_cues(KO, EN)
    document = burn_in.ass_document(pairing, burn_in.default_style(), 1920, 1080)
    assert "PlayResX: 1920\nPlayResY: 1080" in document
    first, second = _events(document)
    assert (
        first == "Dialogue: 0,0:00:01.00,0:00:02.50,KO,,0,0,0,,오늘은 제주도\\N{\\rEN}Today, Jeju"
    )
    assert second.endswith(",KO,,0,0,0,,\\{괄호\\} 백\\⁠n슬래시")


def test_english_only_event_uses_english_style() -> None:
    pairing = burn_in.pair_cues((_cue("0", "1", ""),), (_cue("0", "1", "Hi"),))
    (event,) = _events(burn_in.ass_document(pairing, burn_in.default_style(), 1280, 720))
    assert ",EN,,0,0,0,,Hi" in event


def test_style_scales_with_video_height() -> None:
    pairing = burn_in.pair_cues(KO, EN)
    styles = _styles(burn_in.ass_document(pairing, burn_in.default_style(), 3840, 2160))
    ko, en = styles["KO"], styles["EN"]
    assert ko[1] == BURN_SETTINGS.font_name
    assert (ko[2], ko[16], ko[18], ko[21]) == ("108", "8", "2", "120")
    assert (en[2], en[16]) == (str(round(Fraction(108 * 4, 5))), str(round(Fraction(8 * 4, 5))))
    assert ko[19] == ko[20] == "192"  # 3840 / 20


def test_portrait_style_scales_with_the_short_side() -> None:
    """W-96(D171) — 세로 영상은 짧은 변(너비)으로 잰다: 2160×3840은 가로 4K와 같은 글자 크기다."""
    pairing = burn_in.pair_cues(KO, EN)
    tall = _styles(burn_in.ass_document(pairing, burn_in.default_style(), 2160, 3840))["KO"]
    assert (tall[2], tall[16], tall[21]) == ("108", "8", "120")
    assert tall[19] == tall[20] == "108"  # 2160 / 20 — 좌우 여백은 여전히 너비 몫
    phone = _styles(burn_in.ass_document(pairing, burn_in.default_style(), 1080, 1920))["KO"]
    assert (phone[2], phone[16], phone[21]) == ("54", "4", "60")


def test_top_position_and_colors() -> None:
    style = _style(position="top", text_color="#FF8000")
    ko = _styles(burn_in.ass_document(burn_in.pair_cues(KO, None), style, 1920, 1080))["KO"]
    assert ko[18] == "8"
    assert ko[3] == "&H000080FF"  # #RRGGBB → &H00BBGGRR
    assert ko[5] == "&H00000000"


def test_times_round_to_centiseconds() -> None:
    assert burn_in._ass_time(Fraction("4.005")) == "0:00:04.01"
    assert burn_in._ass_time(Fraction("4.004")) == "0:00:04.00"
    assert burn_in._ass_time(Fraction(3723) + Fraction(1, 2)) == "1:02:03.50"


def test_multiline_and_blank_rows() -> None:
    assert burn_in._escape("첫 줄\n\n 둘째 줄 ") == "첫 줄\\N둘째 줄"


def test_preview_document_is_always_on() -> None:
    pairing = burn_in.pair_cues(KO, EN)
    event = burn_in.preview_event(pairing)
    assert event is not None and event.korean == "오늘은 제주도"
    (line,) = _events(burn_in.preview_document(event, burn_in.default_style(), 1920, 1080))
    assert line.startswith("Dialogue: 0,0:00:00.00,9:59:59.99,KO")
    assert burn_in.preview_event(burn_in.pair_cues((), None)) is None


def test_unknown_video_size_is_refused() -> None:
    with pytest.raises(burn_in.BurnInError):
        burn_in.ass_document(burn_in.pair_cues(KO, None), burn_in.default_style(), 0, 1080)


def test_settings_reject_bad_values() -> None:
    with pytest.raises(ValueError):
        BurnSettings(default_font_px=500)
    with pytest.raises(ValueError):
        BurnSettings(default_text_color="red")
    with pytest.raises(ValueError):
        BurnSettings(font_file="../x.otf")
