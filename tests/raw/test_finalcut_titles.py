"""SRT를 Final Cut 타이틀로 — 타이틀만 있는 프로젝트(W-30f, D90 — D89 대체).

사용자 지시(2026-10-26): 「SRT 파일을 주면 FCPXML로 만들어서 파이널컷 자체 자막을 적용할 수 있도록 —
Vrew처럼 fcpxml로 뽑으면 파이널컷의 title 등을 적용할 수 있도록」. 고른 것: 타이틀만 있는 프로젝트 ·
Basic Title · 캡션으로 싣던 D89 모드를 교체. 시험이 못 박는 것:

- 스파인은 빈 gap 하나이고, 줄마다 레인 1에 Basic Title 타이틀이 붙는다(offset = 3600s + 줄 시작,
  프레임 격자 · 프레임 길이의 분모 그대로). 글은 한 자도 고치지 않는다(두 줄은 `\\n` 그대로).
- 한 레인에 두 클립을 둘 수 없으므로 겹친 줄은 끝을 다음 줄 시작으로 줄이고 센다. 같은 프레임에서
  시작한 줄은 둘 다 두고 센다(Final Cut이 위 레인으로 올린다). 지우는 줄은 없다.
- 빈 글·0프레임·뒤집힌 줄·두 줄 넘는 줄은 고치지 않고 센다. 실을 줄이 없으면 거부한다.
- 글꼴 크기·아래 위치는 프레임 높이에 비례한다(설정 `FINALCUT_TITLES`).
- 문서는 설치된 FCPXML 1.14 DTD를 통과한다(`text-style-def` id는 문서에서 유일).
- 보내기는 원본을 조사하지 않는다 — 포맷은 마지막 작업대 보내기의 시퀀스 포맷, 없으면 기본 포맷.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_finalcut_timeline import (
    INSTALLED_DTD,
    INSTALLED_DTD_SHA256,
    needs_installed_dtd,
    needs_xmllint,
)
from vlogsrt.project.fcp_tables import FRAME_RATE_TAGS
from vlogsrt.project.raw import finalcut_titles as titles
from vlogsrt.project.raw.config import FINALCUT_TITLES, FinalCutTitleFormat
from vlogsrt.project.raw.finalcut_timeline import Cue, FinalCutTimelineError
from vlogsrt.project.xml_validation import validate_xml

F = Fraction
PAL = F(1, 25)
NTSC = F(1001, 30000)
HD = FinalCutTitleFormat(width=1920, height=1080, frame_duration=PAL)
PORTRAIT = FinalCutTitleFormat(width=1080, height=1920, frame_duration=F(1, 30))
BASIC_TITLE = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _plan(cues: list[Cue], fmt: FinalCutTitleFormat = HD, **settings: Any) -> Any:
    chosen = replace(FINALCUT_TITLES, **settings) if settings else FINALCUT_TITLES
    return titles.plan_titles(cues, fmt, title="자막 타이틀", settings=chosen)


def _root(cues: list[Cue], fmt: FinalCutTitleFormat = HD, **settings: Any) -> ET.Element:
    document, _ = _plan(cues, fmt, **settings)
    return ET.fromstring(titles.render_titles(document))


def _titles(root: ET.Element) -> list[ET.Element]:
    return root.findall("library/event/project/sequence/spine/gap/title")


# ── 시각 ─────────────────────────────────────────────────────────────────


def test_titles_hang_on_one_gap_at_lane_one_on_the_frame_grid() -> None:
    root = _root([Cue(F(2, 5), F(6, 5), "첫 줄"), Cue(F(2), F(3), "둘째 줄")])
    sequence = root.find("library/event/project/sequence")
    assert sequence is not None
    assert sequence.get("tcStart") == "0s" and sequence.get("tcFormat") == "NDF"
    (gap,) = sequence.findall("spine/gap")
    assert [gap.get(k) for k in ("name", "offset", "start")] == ["Gap", "0s", "3600s"]
    assert gap.get("duration") == sequence.get("duration") == "75/25s"
    found = [
        (t.get("lane"), t.get("offset"), t.get("start"), t.get("duration"), t.get("ref"))
        for t in _titles(root)
    ]
    assert found == [
        ("1", "90010/25s", "3600s", "20/25s", "r2"),
        ("1", "90050/25s", "3600s", "25/25s", "r2"),
    ]


def test_a_line_at_zero_and_an_ntsc_line_keep_exact_rational_times() -> None:
    ntsc = FinalCutTitleFormat(width=1920, height=1080, frame_duration=NTSC, drop_frame=True)
    root = _root([Cue(F(0), F(1, 2), "처음"), Cue(F(1), F(2), "일 초")], ntsc)
    offsets = [t.get("offset") for t in _titles(root)]
    assert offsets == ["3600s", "108030030/30000s"]
    assert root.find("library/event/project/sequence").get("tcFormat") == "DF"  # type: ignore[union-attr]


def test_anchored_time_is_exact_for_every_final_cut_rate() -> None:
    rng = random.Random(90)
    for fd in FRAME_RATE_TAGS:
        for frames in (0, 1, rng.randrange(2, 10**6)):
            assert F(titles.anchored_time(frames, fd).removesuffix("s")) == 3600 + frames * fd


def test_overlapping_lines_are_trimmed_and_same_frame_lines_both_stay() -> None:
    cues = [
        Cue(F(0), F(2), "앞"),
        Cue(F(1), F(3), "겹침"),
        Cue(F(4), F(6), "같은 시작 긴 줄"),
        Cue(F(4), F(5), "같은 시작 짧은 줄"),
    ]
    document, counts = _plan(cues)
    spans = [(t.start_frame, t.end_frame, t.text) for t in document.titles]
    assert spans == [
        (0, 25, "앞"),
        (25, 75, "겹침"),
        (100, 125, "같은 시작 짧은 줄"),
        (100, 150, "같은 시작 긴 줄"),
    ]
    assert counts.trimmed == 1 and counts.stacked == 1
    assert counts.placed == counts.cues == 4


def test_zero_length_inverted_empty_and_long_lines_are_counted_not_fixed() -> None:
    cues = [
        Cue(F(1), F(1), "0초"),
        Cue(F(3), F(5, 2), "뒤집힘"),
        Cue(F(4), F(5), "   "),
        Cue(F(6), F(7), "한 줄\n두 줄\n세 줄"),
    ]
    document, counts = _plan(cues)
    assert [t.end_frame - t.start_frame for t in document.titles] == [1, 1, 25]
    assert (counts.widened, counts.inverted, counts.empty, counts.line_issues) == (1, 1, 1, 1)
    assert counts.placed == 3 and counts.cues == 4


def test_an_srt_with_nothing_to_place_is_refused() -> None:
    with pytest.raises(FinalCutTimelineError, match="실을 줄이 없다"):
        _plan([Cue(F(0), F(1), " "), Cue(F(1), F(2), "")])


def test_a_forbidden_character_names_the_line_time() -> None:
    with pytest.raises(FinalCutTimelineError, match="00:00:01,000"):
        _plan([Cue(F(1), F(2), "깨진\x01글")])


# ── 글·모양 ──────────────────────────────────────────────────────────────


def test_two_line_text_is_kept_and_the_clip_name_joins_the_lines() -> None:
    (title,) = _titles(_root([Cue(F(0), F(2), "첫 줄 & <둘>\n둘째 줄")]))
    assert title.findtext("text/text-style") == "첫 줄 & <둘>\n둘째 줄"
    assert title.get("name") == "첫 줄 & <둘> 둘째 줄"


def test_each_title_has_its_own_style_id_params_then_text_then_style() -> None:
    root = _root([Cue(F(0), F(1), "가"), Cue(F(2), F(3), "나")])
    for index, title in enumerate(_titles(root), start=1):
        assert [child.tag for child in title] == [
            "param",
            "param",
            "param",
            "text",
            "text-style-def",
        ]
        params = {p.get("name"): (p.get("key"), p.get("value")) for p in title.findall("param")}
        assert params["Flatten"] == ("9999/999166631/999166633/2/351", "1")
        assert params["Alignment"] == ("9999/999166631/999166633/2/354/999169573/401", "1 (Center)")
        assert params["Position"][0] == "9999/999166631/999166633/1/100/101"
        assert title.find("text/text-style").get("ref") == f"ts{index}"  # type: ignore[union-attr]
        assert title.find("text-style-def").get("id") == f"ts{index}"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("fmt", "size", "position"), [(HD, "63", "0 -450"), (PORTRAIT, "112", "0 -800")]
)
def test_font_size_and_lower_position_follow_the_frame_height(
    fmt: FinalCutTitleFormat, size: str, position: str
) -> None:
    (title,) = _titles(_root([Cue(F(0), F(1), "자막")], fmt))
    style = title.find("text-style-def/text-style")
    assert style is not None
    assert style.attrib == {
        "font": "Apple SD Gothic Neo",
        "fontSize": size,
        "fontFace": "Bold",
        "fontColor": "1 1 1 1",
        "alignment": "center",
    }
    position_param = next(p for p in title.findall("param") if p.get("name") == "Position")
    assert position_param.get("value") == position


def test_a_stroke_is_written_only_when_the_settings_ask_for_it() -> None:
    root = _root(
        [Cue(F(0), F(1), "테두리")], stroke_color="0 0 0 1", stroke_width_fraction=F(1, 540)
    )
    style = _titles(root)[0].find("text-style-def/text-style")
    assert style is not None
    assert (style.get("strokeColor"), style.get("strokeWidth")) == ("0 0 0 1", "2")


def test_resources_name_the_basic_title_effect_and_the_format() -> None:
    root = _root([Cue(F(0), F(1), "가")], PORTRAIT)
    fmt, effect = root.find("resources/format"), root.find("resources/effect")
    assert fmt is not None and effect is not None
    assert fmt.attrib == {
        "id": "r1",
        "name": "FFVideoFormat1080x1920p30",
        "frameDuration": "1/30s",
        "width": "1080",
        "height": "1920",
        "colorSpace": "1-1-1 (Rec. 709)",
    }
    assert effect.attrib == {"id": "r2", "name": "Basic Title", "uid": BASIC_TITLE}
    assert root.find("library/event").get("name") == "자막 타이틀"  # type: ignore[union-attr]
    assert root.findall(".//asset") == [] and root.findall(".//caption") == []


@needs_xmllint
@needs_installed_dtd
@pytest.mark.parametrize("fmt", [HD, PORTRAIT, replace(HD, frame_duration=NTSC, drop_frame=True)])
def test_a_korean_title_document_passes_the_installed_dtd_and_is_deterministic(
    tmp_path: Path, fmt: FinalCutTitleFormat
) -> None:
    digest = hashlib.sha256(INSTALLED_DTD.read_bytes()).hexdigest()
    assert digest == INSTALLED_DTD_SHA256, f"설치 DTD가 바뀌었다: {digest}"
    cues = [Cue(F(1), F(3), "안녕하세요 오늘은\n한강에 왔어요"), Cue(F(3), F(9, 2), "날씨 & 바람")]
    document, _ = titles.plan_titles(cues, fmt, title="vlogsrt · 시험 · 자막 타이틀")
    payload = titles.render_titles(document)
    assert payload == titles.render_titles(titles.plan_titles(cues, fmt, title=document.title)[0])
    target = tmp_path / "titles.fcpxml"
    target.write_bytes(payload)
    result = validate_xml(target, dtd_path=INSTALLED_DTD)
    assert result.dtd_checked is True
    assert result.valid is True, result.diagnostics


# ── 설정 ─────────────────────────────────────────────────────────────────


def test_the_default_settings_are_basic_title_on_a_final_cut_rate() -> None:
    assert FINALCUT_TITLES.effect_name == "Basic Title"
    assert FINALCUT_TITLES.effect_uid == BASIC_TITLE
    assert FINALCUT_TITLES.default_format.frame_duration in FRAME_RATE_TAGS


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


@pytest.mark.parametrize("bad", [{"width": 0}, {"height": -1}, {"frame_duration": F(0)}])
def test_a_title_format_without_a_size_or_rate_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        FinalCutTitleFormat(**bad)


# ── 보내기 ───────────────────────────────────────────────────────────────

SRT_TEXT = (
    "1\n00:00:00,400 --> 00:00:01,200\n편집한 자막\n\n"
    "2\n00:00:01,000 --> 00:00:03,000\n겹치는 줄\n둘째 줄\n\n"
    "3\n00:00:04,000 --> 00:00:05,000\n\n"
)
MOMENT = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)


def _write_srt(tmp_path: Path, text: str = SRT_TEXT) -> Path:
    path = tmp_path / "edited" / "고친 자막.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("﻿" + text).encode("utf-8"))
    return path


def _empty_workspace(tmp_path: Path) -> tuple[Any, Path]:
    from vlogsrt.project.raw.service import RawWorkspaceService

    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "타이틀")
    return service, root


def test_without_a_previous_export_the_titles_use_the_default_format_and_probe_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt import preprocess
    from vlogsrt.project.raw import finalcut_export, timeline_srt

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("타이틀 보내기는 원본을 조사하지 않는다")

    monkeypatch.setattr(preprocess, "probe_extraction_clock", refuse)
    service, root = _empty_workspace(tmp_path)
    srt = _write_srt(tmp_path)
    outcome = finalcut_export.export_titles(
        service, root, timeline_srt.read(srt), now=lambda: MOMENT
    )
    assert outcome.fcpxml.name == "vlogsrt_20261026T050000Z_titles.fcpxml"
    tree = ET.parse(outcome.fcpxml).getroot()
    assert [t.findtext("text/text-style") for t in _titles(tree)] == [
        "편집한 자막",
        "겹치는 줄\n둘째 줄",
    ]
    assert tree.find("library/event/project").get("name").endswith(" · 자막 타이틀")  # type: ignore[union-attr]
    report = json.loads(outcome.report.read_text(encoding="utf-8"))
    assert report["schema"] == finalcut_export.TITLES_REPORT_SCHEMA
    assert report["srt"] == {
        "name": "고친 자막.srt",
        "path": str(srt),
        "sha256": hashlib.sha256(srt.read_bytes()).hexdigest(),
    }
    assert report["format"]["origin"] == "default" and report["format"]["file"] is None
    assert report["format"]["width"] == FINALCUT_TITLES.default_format.width
    assert report["effect"] == {"name": "Basic Title", "uid": BASIC_TITLE}
    counts = report["counts"]
    assert (counts["cues"], counts["placed"], counts["empty"], counts["trimmed"]) == (3, 2, 1, 1)
    assert counts["duration_seconds"] == "3" and counts["last_cue_end_seconds"] == "5"
    if INSTALLED_DTD.is_file():
        assert report["validation"]["dtd_checked"] is True
    assert report["validation"]["valid"] is True


@needs_ffmpeg
def test_titles_follow_the_last_workbench_export_format_never_a_titles_file(
    tmp_path: Path,
) -> None:
    from tests.raw.test_finalcut_export import _plain_project
    from vlogsrt.project.raw import finalcut_export, timeline_srt

    service, root = _plain_project(tmp_path)
    timeline = service.export_finalcut_timeline(root)
    srt = timeline_srt.read(_write_srt(tmp_path))
    first = finalcut_export.export_titles(service, root, srt)
    second = finalcut_export.export_titles(service, root, srt)
    for outcome in (first, second):
        report = json.loads(outcome.report.read_text(encoding="utf-8"))
        assert report["format"]["origin"] == "last_export"
        assert report["format"]["file"] == timeline.name
        fmt = ET.parse(outcome.fcpxml).getroot().find("resources/format")
        assert fmt is not None
        assert (fmt.get("width"), fmt.get("height"), fmt.get("frameDuration")) == (
            "160",
            "90",
            "1/25s",
        )


def test_a_broken_last_export_is_refused_by_name_and_nothing_is_written(tmp_path: Path) -> None:
    from vlogsrt.project.raw import finalcut_export, timeline_srt

    service, root = _empty_workspace(tmp_path)
    folder = root / finalcut_export.EXPORT_FOLDER
    folder.mkdir(parents=True)
    (folder / "vlogsrt_20261020T000000Z.fcpxml").write_text("<fcpxml/>", encoding="utf-8")
    with pytest.raises(finalcut_export.FinalCutExportError, match="vlogsrt_20261020T000000Z"):
        finalcut_export.export_titles(service, root, timeline_srt.read(_write_srt(tmp_path)))
    assert sorted(p.name for p in folder.iterdir()) == ["vlogsrt_20261020T000000Z.fcpxml"]


def test_an_unreadable_srt_is_refused_by_name_and_nothing_is_written(tmp_path: Path) -> None:
    from vlogsrt.project.raw import finalcut_export, timeline_srt
    from vlogsrt.project.raw.service import RawWorkspaceError

    service, root = _empty_workspace(tmp_path)
    broken = _write_srt(tmp_path, "1\n시각 없는 줄\n")
    with pytest.raises(timeline_srt.TimelineSrtError, match="고친 자막.srt"):
        service.export_finalcut_titles(root, broken)
    with pytest.raises(timeline_srt.TimelineSrtError, match="없는.srt"):
        service.export_finalcut_titles(root, tmp_path / "없는.srt")
    assert not (root / finalcut_export.EXPORT_FOLDER).exists()
    assert issubclass(timeline_srt.TimelineSrtError, RawWorkspaceError)


def test_the_cli_writes_titles_for_an_srt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.raw.test_finalcut_export import _parser
    from vlogsrt.project.raw import commands as media_commands

    _service, root = _empty_workspace(tmp_path)
    srt = _write_srt(tmp_path)
    args = _parser().parse_args(["media", "finalcut", "--workspace", str(root), "--srt", str(srt)])
    assert media_commands.media_command(args) == 0
    printed = capsys.readouterr().out.replace("\n", "")
    assert "_titles.fcpxml" in printed and "고친 자막.srt" in printed
    assert "타이틀 2개" in printed and "⌘V" in printed


# ── 작업대 단추 ───────────────────────────────────────────────────────────


def test_the_workbench_button_picks_an_srt_sends_titles_and_retries_with_the_same_file(
    tmp_path: Path,
) -> None:
    """선택기 취소는 아무것도 보내지 않는다 · 고른 SRT로 타이틀 파일을 쓰고 가짜 Final Cut에
    건넨다 · 결과 문구가 줄 수·포맷·붙이는 법을 말한다 · 재시도는 같은 SRT로(없어졌으면 다시
    고르라고)."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_review_workspace import _wait_terminal
    from tests.raw.test_workspace_chooser_ui import _ScriptedChooser
    from ui import chooser as ui_chooser
    from ui import workspace_service as ws

    _service, root = _empty_workspace(tmp_path)
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
    assert "Final Cut으로 보냈습니다" in status
    assert "`고친 자막.srt`의 줄 2개를 「Basic Title」 타이틀로" in status
    assert "1920×1080 30fps" in status and "기본 포맷" in status
    assert "겹쳐서 끝을 줄인 줄 1개" in status and "빈 줄 1개" in status
    assert "⌘A" in status and "⌘V" in status

    assert "고친 자막.srt" in server._retry_finalcut(signature, finished)
    again = next(j for j in sends() if j.job_id != job.job_id)
    assert _wait_terminal(server.raw, root, again.job_id).status == "succeeded"
    assert len(opened) == 2 and "_titles" in opened[1].stem  # 같은 초면 `_titles_2`
    srt.unlink()
    assert "다시 고르세요" in server._retry_finalcut(signature, again)
    assert len(sends()) == 2
