"""SRT를 Final Cut 타이틀로 — 영상 타임라인 위에 Basic Title을 단다(W-30f, D91 — D90·D89 대체).

사용자 지시(2026-10-26): 「복사·붙여넣기 작업이 없도록 영상 타임라인이 다 있는 상태에서 Basic
Title이 붙도록 — SRT로 Final Cut 보내기를 누르면 영상 + Basic Title이 출력되도록」. 시험이 못
박는 것:

- 작업대가 보내는 그 전체 타임라인(원본·컷·켜짐·키워드·포맷·레이트)을 그대로 쓰고, 원본 자막 캡션
  대신 SRT 줄마다 Basic Title 타이틀을 그 줄이 시작하는 스파인 클립에 레인 1로 붙인다.
- SRT 시각 = 타임라인 시각(0초 = 시퀀스 처음). 타이틀 offset은 붙은 클립 안 원본 시각(원본 격자),
  길이는 시퀀스 프레임이다. 컷을 넘는 줄도 한 타이틀이다(나누지 않는다). 글은 한 자도 고치지 않는다.
- 겹친 줄은 끝을 다음 줄 시작으로 줄이고 센다(한 레인에 두 클립을 둘 수 없다). 같은 프레임 시작은
  둘 다 두고 센다. 타임라인 끝을 넘는 꼬리는 자르고, 통째로 밖이면 싣지 않고 센다. 꺼진 클립에 붙은
  타이틀은 함께 꺼진다. 실을 줄이 없으면 거부한다.
- 글꼴 크기·아래 위치는 시퀀스 높이에 비례한다. 문서는 설치된 FCPXML 1.14 DTD를 통과한다.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_finalcut_timeline import (
    INSTALLED_DTD,
    INSTALLED_DTD_SHA256,
    media,
    needs_installed_dtd,
    needs_xmllint,
    seg,
)
from vlogsrt.project.raw import finalcut_timeline as ft
from vlogsrt.project.raw import finalcut_titles as titles
from vlogsrt.project.raw.config import FINALCUT_TITLES
from vlogsrt.project.xml_validation import validate_xml

F = Fraction
PAL = F(1, 25)
BASIC_TITLE = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _media(
    name: str, *, frames: int, fd: Fraction = PAL, size: tuple[int, int] = (1920, 1080)
) -> ft.SourceMedia:
    return media(name, frames=frames, tc=0, drop=False, ident=None, fd=fd, size=size)


def _two_sources() -> ft.TimelineDocument:
    """A(2초: 켠 0.8초 + 사람이 제외한 1.2초, 원본 자막 하나) · B(2초, 한 클립) — 25fps 4초."""
    a = ft.SourceInput(
        media=_media("A", frames=50),
        segments=(seg(0, 20), seg(20, 50, excluded=True)),
        cues=(ft.Cue(F(0), F(1), "원래 자막"),),
    )
    b = ft.SourceInput(media=_media("B", frames=50))
    return ft.plan([a, b], title="t", library_bundle=None)


def _titled(cues: list[ft.Cue], document: ft.TimelineDocument | None = None, **settings: Any):
    chosen = replace(FINALCUT_TITLES, **settings) if settings else FINALCUT_TITLES
    return titles.with_srt_titles(document or _two_sources(), cues, chosen)


def _pieces(document: ft.TimelineDocument) -> list[tuple[str, int, int, str, str, bool]]:
    return [
        (plan.media.name, p.clip_index, p.start_frame, p.duration, p.text, p.enabled)
        for plan in document.sources
        for p in plan.titles
    ]


def _spine_titles(document: ft.TimelineDocument) -> list[tuple[int, ET.Element]]:
    root = ET.fromstring(ft.render(document))
    clips = root.findall("library/event/project/sequence/spine/asset-clip")
    return [(index, title) for index, clip in enumerate(clips) for title in clip.findall("title")]


# ── 자리 ─────────────────────────────────────────────────────────────────


def test_each_line_hangs_on_the_clip_where_it_starts_without_being_split() -> None:
    document, counts = _titled(
        [
            ft.Cue(F(2, 5), F(6, 5), "컷을 넘는 줄"),  # 시퀀스 10→30: A 켠 클립에서 시작
            ft.Cue(F(9, 5), F(12, 5), "꺼진 클립에서 시작"),  # 45→60: A 꺼진 클립
            ft.Cue(F(5, 2), F(3), "B의 줄"),  # 62.5→75 → 63: B 안 13
        ]
    )
    assert _pieces(document) == [
        ("A", 0, 10, "20/25s", "컷을 넘는 줄", True),
        ("A", 1, 45, "15/25s", "꺼진 클립에서 시작", False),
        ("B", 0, 13, "12/25s", "B의 줄", True),
    ]
    assert (counts.placed, counts.disabled, counts.outside, counts.tails) == (3, 1, 0, 0)


def test_original_subtitle_captions_are_replaced_and_clips_stay_the_same() -> None:
    before = _two_sources()
    assert {p.text for p in before.sources[0].captions} == {"원래 자막"}
    after, _ = _titled([ft.Cue(F(0), F(1), "새 자막")])
    assert all(plan.captions == () for plan in after.sources)
    assert [plan.clips for plan in after.sources] == [plan.clips for plan in before.sources]
    assert after.sequence_format == before.sequence_format
    assert "원래 자막" not in ft.render(after).decode("utf-8")


def test_tails_past_the_timeline_are_cut_and_lines_wholly_outside_are_counted() -> None:
    document, counts = _titled(
        [ft.Cue(F(39, 10), F(9, 2), "끝을 넘는 줄"), ft.Cue(F(5), F(6), "타임라인 밖")]
    )
    assert _pieces(document) == [("B", 0, 48, "2/25s", "끝을 넘는 줄", True)]
    assert (counts.cues, counts.placed, counts.tails, counts.outside) == (2, 1, 1, 1)


def test_overlaps_are_trimmed_and_same_frame_lines_both_stay() -> None:
    document, counts = _titled(
        [
            ft.Cue(F(0), F(2), "앞"),
            ft.Cue(F(1), F(3), "겹침"),
            ft.Cue(F(3), F(7, 2), "같은 시작 긴 줄"),
            ft.Cue(F(3), F(13, 4), "같은 시작 짧은 줄"),
        ]
    )
    durations = [(p[4], p[3]) for p in _pieces(document)]
    assert durations == [
        ("앞", "25/25s"),
        ("겹침", "50/25s"),
        ("같은 시작 짧은 줄", "6/25s"),
        ("같은 시작 긴 줄", "13/25s"),
    ]
    assert counts.trimmed == 1 and counts.stacked == 1 and counts.placed == 4


def test_zero_inverted_empty_and_long_lines_are_counted_not_fixed() -> None:
    document, counts = _titled(
        [
            ft.Cue(F(1), F(1), "0초"),
            ft.Cue(F(3), F(5, 2), "뒤집힘"),
            ft.Cue(F(2), F(3), "  "),
            ft.Cue(F(7, 2), F(18, 5), "한 줄\n두 줄\n세 줄"),
        ]
    )
    assert [p[3] for p in _pieces(document)] == ["1/25s", "1/25s", "2/25s"]
    assert (counts.widened, counts.inverted, counts.empty, counts.line_issues) == (1, 1, 1, 1)


def test_nothing_to_place_is_refused_and_forbidden_text_names_the_time() -> None:
    with pytest.raises(ft.FinalCutTimelineError, match="실을 줄이 없다"):
        _titled([ft.Cue(F(0), F(1), " "), ft.Cue(F(9), F(10), "밖")])
    with pytest.raises(ft.FinalCutTimelineError, match="00:00:01,000"):
        _titled([ft.Cue(F(1), F(2), "깨진\x01글")])


def test_a_line_on_a_conformed_rate_source_starts_at_its_clip_time() -> None:
    """30fps 시퀀스 안 24fps 원본(D71) — 시작은 클립 안 시각(원본 격자), 길이는 시퀀스 프레임."""
    a = ft.SourceInput(media=_media("A30", frames=60, fd=F(1, 30)))
    b = ft.SourceInput(media=_media("B24", frames=24, fd=F(1, 24)))
    document = ft.plan([a, b], title="t", library_bundle=None)
    placed, _ = _titled([ft.Cue(F(5, 2), F(29, 10), "24fps 줄")], document)
    assert _pieces(placed) == [("B24", 0, 12, "12/30s", "24fps 줄", True)]


# ── 문서 ─────────────────────────────────────────────────────────────────


def test_titles_render_inside_their_clips_with_basic_title_and_unique_styles() -> None:
    document, _ = _titled(
        [ft.Cue(F(2, 5), F(6, 5), "첫 줄 & <둘>\n둘째 줄"), ft.Cue(F(9, 5), F(12, 5), "꺼짐")]
    )
    found = _spine_titles(document)
    assert [index for index, _ in found] == [0, 1]
    first, second = (title for _, title in found)
    assert [first.get(k) for k in ("ref", "lane", "offset", "start", "duration")] == [
        titles.EFFECT_ID,
        "1",
        "10/25s",
        "3600s",
        "20/25s",
    ]
    assert first.get("name") == "첫 줄 & <둘> 둘째 줄" and first.get("enabled") is None
    assert second.get("enabled") == "0"
    assert first.findtext("text/text-style") == "첫 줄 & <둘>\n둘째 줄"
    assert [child.tag for child in first] == ["param", "param", "param", "text", "text-style-def"]
    params = {p.get("name"): (p.get("key"), p.get("value")) for p in first.findall("param")}
    assert params == {
        "Position": ("9999/999166631/999166633/1/100/101", "0 -450"),
        "Flatten": ("9999/999166631/999166633/2/351", "1"),
        "Alignment": ("9999/999166631/999166633/2/354/999169573/401", "1 (Center)"),
    }
    ids = [t.find("text-style-def").get("id") for _, t in found]  # type: ignore[union-attr]
    assert ids == ["ts1", "ts2"]
    root = ET.fromstring(ft.render(document))
    effect = root.find("resources/effect")
    assert effect is not None
    assert effect.attrib == {"id": titles.EFFECT_ID, "name": "Basic Title", "uid": BASIC_TITLE}
    assert root.findall(".//caption") == []


@pytest.mark.parametrize(
    ("size", "font", "position"), [((1920, 1080), "63", "0 -450"), ((1080, 1920), "112", "0 -800")]
)
def test_font_size_and_lower_position_follow_the_sequence_height(
    size: tuple[int, int], font: str, position: str
) -> None:
    source = ft.SourceInput(media=_media("S", frames=50, size=size))
    document = ft.plan([source], title="t", library_bundle=None)
    placed, _ = _titled([ft.Cue(F(0), F(1), "자막")], document)
    ((_, title),) = _spine_titles(placed)
    style = title.find("text-style-def/text-style")
    assert style is not None and style.attrib == {
        "font": "Apple SD Gothic Neo",
        "fontSize": font,
        "fontFace": "Bold",
        "fontColor": "1 1 1 1",
        "alignment": "center",
    }
    assert title.find("param").get("value") == position  # type: ignore[union-attr]


def test_a_stroke_is_written_only_when_the_settings_ask_for_it() -> None:
    placed, _ = _titled(
        [ft.Cue(F(0), F(1), "테두리")], stroke_color="0 0 0 1", stroke_width_fraction=F(1, 540)
    )
    ((_, title),) = _spine_titles(placed)
    style = title.find("text-style-def/text-style")
    assert style is not None
    assert (style.get("strokeColor"), style.get("strokeWidth")) == ("0 0 0 1", "2")


def test_without_titles_the_timeline_bytes_are_unchanged() -> None:
    document = _two_sources()
    assert document.title_style is None
    assert b"<title" not in ft.render(document) and b"<effect" not in ft.render(document)


@needs_xmllint
@needs_installed_dtd
@pytest.mark.parametrize("mixed", [False, True])
def test_a_titled_timeline_passes_the_installed_dtd_and_is_deterministic(
    tmp_path: Path, mixed: bool
) -> None:
    digest = hashlib.sha256(INSTALLED_DTD.read_bytes()).hexdigest()
    assert digest == INSTALLED_DTD_SHA256, f"설치 DTD가 바뀌었다: {digest}"
    if mixed:
        sources = [
            ft.SourceInput(media=_media("A30", frames=60, fd=F(1, 30))),
            ft.SourceInput(media=_media("B24", frames=24, fd=F(1, 24))),
        ]
        document = ft.plan(sources, title="t", library_bundle=None)
    else:
        document = _two_sources()
    cues = [
        ft.Cue(F(1, 2), F(2), "안녕하세요 오늘은\n한강에 왔어요"),
        ft.Cue(F(5, 2), F(3), "날씨 & 바람"),
    ]
    placed, _ = _titled(cues, document)
    payload = ft.render(placed)
    assert payload == ft.render(_titled(cues, document)[0])
    target = tmp_path / "titled.fcpxml"
    target.write_bytes(payload)
    result = validate_xml(target, dtd_path=INSTALLED_DTD)
    assert result.dtd_checked is True
    assert result.valid is True, result.diagnostics


# ── 설정 ─────────────────────────────────────────────────────────────────


def test_the_default_settings_are_basic_title() -> None:
    assert FINALCUT_TITLES.effect_name == "Basic Title"
    assert FINALCUT_TITLES.effect_uid == BASIC_TITLE


@pytest.mark.parametrize(
    "bad",
    [
        {"effect_uid": "Basic Title"},
        {"font": " "},
        {"font_size_fraction": F(0)},
        {"font_color": "1 1 1"},
        {"font_color": "2 0 0 1"},
        {"stroke_width_fraction": F(1, 100)},  # 테두리 색 없이 굵기만
        {"position_y_fraction": F(2, 3)},
    ],
)
def test_bad_title_settings_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(FINALCUT_TITLES, **bad)


def test_title_rounding_is_half_up_for_every_height() -> None:
    rng = random.Random(91)
    for height in (rng.randrange(90, 4320) for _ in range(50)):
        exact = height * FINALCUT_TITLES.font_size_fraction
        assert abs(titles.font_size(height, FINALCUT_TITLES) - exact) <= F(1, 2)


# ── 보내기 · CLI · 작업대 ───────────────────────────────────────────────

SRT_TEXT = (
    "1\n00:00:00,400 --> 00:00:01,200\n편집한 자막\n\n"
    "2\n00:00:01,000 --> 00:00:01,800\n겹치는 줄\n둘째 줄\n\n"
    "3\n00:00:01,900 --> 00:00:02,000\n\n"
    "4\n00:00:05,000 --> 00:00:06,000\n타임라인 밖\n"
)


def _write_srt(tmp_path: Path, text: str = SRT_TEXT) -> Path:
    path = tmp_path / "edited" / "고친 자막.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("﻿" + text).encode("utf-8"))
    return path


@needs_ffmpeg
def test_the_export_writes_the_video_timeline_with_srt_titles_and_reports_them(
    tmp_path: Path,
) -> None:
    from tests.raw.test_finalcut_export import _plain_project

    service, root = _plain_project(tmp_path)
    srt = _write_srt(tmp_path)
    path = service.export_finalcut_titles(root, srt)
    assert path.name.endswith("_titles.fcpxml")
    tree = ET.parse(path).getroot()
    (clip,) = tree.findall("library/event/project/sequence/spine/asset-clip")
    assert [t.findtext("text/text-style") for t in clip.findall("title")] == [
        "편집한 자막",
        "겹치는 줄\n둘째 줄",
    ]
    assert tree.find("library/event/project").get("name").endswith(" · 자막 타이틀")  # type: ignore[union-attr]
    assert tree.findall(".//caption") == [] and tree.findall("resources/asset")
    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    section = report["titles"]
    assert section["srt"] == {
        "name": "고친 자막.srt",
        "path": str(srt),
        "sha256": hashlib.sha256(srt.read_bytes()).hexdigest(),
    }
    assert section["effect"] == {"name": "Basic Title", "uid": BASIC_TITLE}
    counts = section["counts"]
    assert (counts["cues"], counts["placed"], counts["empty"], counts["outside"]) == (4, 2, 1, 1)
    assert counts["trimmed"] == 1 and counts["last_cue_end_seconds"] == "6"
    assert report["counts"]["sources"] == 1 and "sync_check" not in report
    assert report["validation"]["valid"] is True


def test_an_unreadable_srt_is_refused_by_name_and_nothing_is_written(tmp_path: Path) -> None:
    from vlogsrt.project.raw import finalcut_export, timeline_srt
    from vlogsrt.project.raw.service import RawWorkspaceError, RawWorkspaceService

    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "타이틀")
    broken = _write_srt(tmp_path, "1\n시각 없는 줄\n")
    with pytest.raises(timeline_srt.TimelineSrtError, match="고친 자막.srt"):
        service.export_finalcut_titles(root, broken)
    with pytest.raises(timeline_srt.TimelineSrtError, match="없는.srt"):
        service.export_finalcut_titles(root, tmp_path / "없는.srt")
    assert not (root / finalcut_export.EXPORT_FOLDER).exists()
    assert issubclass(timeline_srt.TimelineSrtError, RawWorkspaceError)


@needs_ffmpeg
def test_the_cli_writes_the_titled_timeline_for_an_srt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.raw.test_finalcut_export import _parser, _plain_project
    from vlogsrt.project.raw import commands as media_commands

    _service, root = _plain_project(tmp_path)
    srt = _write_srt(tmp_path)
    args = _parser().parse_args(["media", "finalcut", "--workspace", str(root), "--srt", str(srt)])
    assert media_commands.media_command(args) == 0
    printed = capsys.readouterr().out.replace("\n", "")
    assert "_titles.fcpxml" in printed and "원본 1개" in printed
    assert "고친 자막.srt" in printed and "Basic Title 타이틀 2개" in printed


@needs_ffmpeg
def test_the_workbench_button_sends_video_plus_titles_and_retries_with_the_same_file(
    tmp_path: Path,
) -> None:
    """선택기 취소는 아무것도 보내지 않는다 · 고른 SRT로 영상 타임라인 + 타이틀을 쓰고 가짜 Final
    Cut에 건넨다 · 결과 문구가 원본·클립·타이틀 수를 말한다 · 재시도는 같은 SRT로."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_finalcut_export import _plain_project
    from tests.raw.test_review_workspace import _wait_terminal
    from tests.raw.test_workspace_chooser_ui import _ScriptedChooser
    from ui import chooser as ui_chooser
    from ui import workspace_service as ws

    _service, root = _plain_project(tmp_path)
    srt = _write_srt(tmp_path)
    opened: list[Path] = []
    picker = _ScriptedChooser(None, srt)
    server = ws.RawWorkspaceUi(
        state_path=tmp_path / "ui-state.json", finalcut_opener=opened.append, path_chooser=picker
    )
    signature = server.open_workspace_view(str(root))[1]

    def sends() -> list[Any]:
        return [j for j in server.raw.list_jobs(root) if j.kind == ws.FINALCUT_SEND_JOB_KIND]

    assert server.finalcut_srt_send_view(signature) == ws.FINALCUT_SRT_CANCELLED_TEXT
    assert sends() == [] and opened == []
    assert "고친 자막.srt" in server.finalcut_srt_send_view(signature)
    assert picker.calls == [ui_chooser.SRT_KIND, ui_chooser.SRT_KIND]
    (job,) = sends()
    assert job.input_fingerprint == f"{ws.FINALCUT_SRT_FINGERPRINT_PREFIX}{srt}"
    finished = _wait_terminal(server.raw, root, job.job_id)
    assert finished.status == "succeeded", finished.error
    (path,) = opened
    assert path.name.endswith("_titles.fcpxml")
    status = str(server.capcut_status_view(signature))
    assert "Final Cut으로 보냈습니다" in status and "원본 1개" in status
    assert "`고친 자막.srt`의 줄 2개를 「Basic Title」 타이틀로" in status
    assert "타임라인 밖이라 싣지 않은 줄 1개" in status and "겹쳐서 끝을 줄인 줄 1개" in status
    assert "⌘V" not in status and "자막이 없는 영상" not in status

    assert "고친 자막.srt" in server._retry_finalcut(signature, finished)
    again = next(j for j in sends() if j.job_id != job.job_id)
    assert _wait_terminal(server.raw, root, again.job_id).status == "succeeded"
    assert len(opened) == 2 and "_titles" in opened[1].stem  # 같은 초면 `_titles_2`
    srt.unlink()
    assert "다시 고르세요" in server._retry_finalcut(signature, again)
    assert len(sends()) == 2
