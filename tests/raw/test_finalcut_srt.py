"""SRT를 FCPXML로 — 고친 SRT(타임라인 시각)를 같은 전체 타임라인의 캡션으로 싣는다(W-30f, D89).

사용자 지시(2026-10-26): 「SRT를 FCPXML로 추출하는 모드도 필요해」 — 고른 것: 작업대가 보내는 그
타임라인(같은 원본·컷·키워드·켜짐)에 원본 자막 대신 그 SRT의 줄을 캡션으로 싣는다. 시험이 못
박는 것:

- SRT 시각은 시퀀스 시각(0초 = 타임라인 처음)이다. 줄은 시퀀스 프레임에 맞추고, 덮는 스파인
  클립마다(꺼진 클립도 시간을 차지한다) 클립 안 원본 프레임으로 옮겨 조각이 된다. 글은 한 자도
  고치지 않는다.
- 원본 자막은 싣지 않는다 — SRT 줄만 캡션이다. 꺼진 클립 밑 조각은 함께 꺼진다.
- 빈 글·타임라인 밖·잘린 꼬리·겹침·컷을 넘은 줄은 고치지 않고 센다(`CaptionCounts`).
- 레이트가 다른 원본(시간을 지키는 conform-rate)은 클립 안 **시각**으로 옮긴다.
- 보내기는 SRT 파일 이름·sha256·센 수를 보고서에 싣고, 이름에 `_srt`를 달며, 원본 자막 표지
  (`자막 시각 확인 필요`)를 달지 않는다. 읽지 못하는 SRT는 이름을 대고 아무것도 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_finalcut_timeline import media, seg
from vlogsrt.project.raw import finalcut_timeline as ft

F = Fraction
PAL = F(1, 25)


def _media(name: str, *, frames: int, fd: Fraction = PAL) -> ft.SourceMedia:
    return media(name, frames=frames, tc=0, drop=False, ident=None, fd=fd)


def _two_sources() -> ft.TimelineDocument:
    """A(2초: 켠 0.8초 + 사람이 제외한 1.2초, 원본 자막 하나) · B(2초, 한 클립) — 25fps 4초."""
    a = ft.SourceInput(
        media=_media("A", frames=50),
        segments=(seg(0, 20), seg(20, 50, excluded=True)),
        cues=(ft.Cue(F(0), F(1), "원래 자막"),),
    )
    b = ft.SourceInput(media=_media("B", frames=50))
    return ft.plan([a, b], title="t", library_bundle=None)


SRT_CUES = (
    ft.Cue(F(2, 5), F(6, 5), "컷을 넘는 줄"),  # 시퀀스 10→30: A 켠 클립 → A 꺼진 클립
    ft.Cue(F(9, 5), F(12, 5), "원본을 넘는 줄"),  # 45→60: A 꺼진 클립 → B
    ft.Cue(F(3), F(3), "  "),  # 빈 글
    ft.Cue(F(39, 10), F(9, 2), "끝을 넘는 줄"),  # 97.5→112.5: 꼬리가 잘린다
    ft.Cue(F(5), F(6), "타임라인 밖"),
)


def _pieces(document: ft.TimelineDocument) -> list[tuple[str, int, int, int, str, bool]]:
    return [
        (plan.media.name, p.clip_index, p.start_frame, p.end_frame, p.text, p.enabled)
        for plan in document.sources
        for p in plan.captions
    ]


def test_srt_lines_are_cut_at_every_spine_clip_and_moved_into_clip_frames() -> None:
    document, counts = ft.with_timeline_captions(_two_sources(), SRT_CUES)
    assert _pieces(document) == [
        ("A", 0, 10, 20, "컷을 넘는 줄", True),
        ("A", 1, 20, 30, "컷을 넘는 줄", False),
        ("A", 1, 45, 50, "원본을 넘는 줄", False),
        ("B", 0, 0, 10, "원본을 넘는 줄", True),
        ("B", 0, 48, 50, "끝을 넘는 줄", True),
    ]
    assert counts == ft.CaptionCounts(
        cues=5, placed=3, pieces=5, empty=1, outside=1, tails=1, split=2
    )


def test_the_original_subtitles_are_replaced_and_clips_stay_the_same() -> None:
    before = _two_sources()
    assert {p.text for p in before.sources[0].captions} == {"원래 자막"}
    after, _ = ft.with_timeline_captions(before, ())
    assert _pieces(after) == []
    assert [plan.clips for plan in after.sources] == [plan.clips for plan in before.sources]
    assert after.sequence_frame_duration == before.sequence_frame_duration


def test_overlaps_widening_and_inverted_lines_are_counted_not_fixed() -> None:
    cues = (
        ft.Cue(F(0), F(1), "앞"),
        ft.Cue(F(1, 2), F(3, 2), "겹침"),
        ft.Cue(F(2), F(2), "0초"),
        ft.Cue(F(3), F(5, 2), "뒤집힘"),
        ft.Cue(F(7, 2), F(18, 5), "한 줄\n두 줄\n세 줄"),
    )
    document, counts = ft.with_timeline_captions(_two_sources(), cues)
    assert counts.overlaps == 1 and counts.widened == 1 and counts.inverted == 1
    assert counts.line_issues == 1
    texts = [piece[4] for piece in _pieces(document)]
    assert "0초" in texts and "뒤집힘" in texts and "한 줄\n두 줄\n세 줄" in texts


def test_a_line_on_a_conformed_rate_source_is_placed_by_clip_time() -> None:
    """30fps 시퀀스 안 24fps 원본 — 시퀀스 프레임이 아니라 클립 안 시각으로 옮긴다(D71)."""
    a = ft.SourceInput(media=_media("A30", frames=60, fd=F(1, 30)))
    b = ft.SourceInput(media=_media("B24", frames=24, fd=F(1, 24)))
    document = ft.plan([a, b], title="t", library_bundle=None)
    assert document.sequence_frame_duration == F(1, 30)
    placed, _ = ft.with_timeline_captions(document, (ft.Cue(F(5, 2), F(29, 10), "24fps 줄"),))
    # 시퀀스 75→87프레임 → B 클립 안 0.5→0.9초 → 24fps 12→22(21.6에 가장 가까운) 프레임
    assert _pieces(placed) == [("B24", 0, 12, 22, "24fps 줄", True)]


def test_a_forbidden_character_names_the_line_time() -> None:
    with pytest.raises(ft.FinalCutTimelineError, match="00:00:01,000"):
        ft.with_timeline_captions(_two_sources(), (ft.Cue(F(1), F(2), "깨진\x01글"),))


def test_the_rendered_captions_sit_in_their_clips_and_disabled_pieces_stay_off() -> None:
    document, _ = ft.with_timeline_captions(_two_sources(), SRT_CUES[:2])
    root = ET.fromstring(ft.render(document))
    clips = root.findall(".//spine/asset-clip")
    found = [
        (index, caption.get("offset"), caption.get("enabled"), caption.findtext("text"))
        for index, clip in enumerate(clips)
        for caption in clip.findall("caption")
    ]
    assert found == [
        (0, ft._time(10, PAL), None, "컷을 넘는 줄"),
        (1, ft._time(20, PAL), "0", "컷을 넘는 줄"),
        (1, ft._time(45, PAL), "0", "원본을 넘는 줄"),
        (2, ft._time(0, PAL), None, "원본을 넘는 줄"),
    ]
    assert "원래 자막" not in ft.render(document).decode("utf-8")


# ── 보내기 ───────────────────────────────────────────────────────────────

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

SRT_TEXT = (
    "1\n00:00:00,400 --> 00:00:01,200\n편집한 자막\n\n"
    "2\n00:00:01,500 --> 00:00:03,000\n끝을 넘는 줄\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\n타임라인 밖\n"
)


def _write_srt(tmp_path: Path, text: str = SRT_TEXT) -> Path:
    path = tmp_path / "edited" / "고친 자막.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("﻿" + text).encode("utf-8"))
    return path


@needs_ffmpeg
def test_export_with_an_srt_writes_its_lines_as_the_captions_and_reports_the_file(
    tmp_path: Path,
) -> None:
    from tests.raw.test_finalcut_export import _plain_project
    from vlogsrt.project.raw import finalcut_export, timeline_srt

    service, root = _plain_project(tmp_path)
    srt = _write_srt(tmp_path)
    outcome = finalcut_export.export(service, root, timeline_srt=timeline_srt.read(srt))
    assert outcome.fcpxml.name.endswith("_srt.fcpxml")
    tree = ET.parse(outcome.fcpxml).getroot()
    texts = [caption.findtext("text") for caption in tree.iter("caption")]
    assert texts == ["편집한 자막", "끝을 넘는 줄"]
    assert tree.find(".//project").get("name").endswith(" · SRT")
    report = json.loads(outcome.report.read_text(encoding="utf-8"))
    section = report["timeline_srt"]
    assert section["name"] == "고친 자막.srt" and section["path"] == str(srt)
    assert section["sha256"] == hashlib.sha256(srt.read_bytes()).hexdigest()
    assert section["timeline_seconds"] == "2" and section["last_cue_end_seconds"] == "6"
    assert section["captions"]["placed"] == 2 and section["captions"]["outside"] == 1
    assert section["captions"]["tails"] == 1
    assert report["counts"]["captions"] == section["captions"]
    assert "sync_check" not in report
    assert ft.KEYWORD_CAPTION_TIMING not in outcome.fcpxml.read_text(encoding="utf-8")


@needs_ffmpeg
def test_an_unreadable_srt_is_refused_by_name_and_nothing_is_written(tmp_path: Path) -> None:
    from tests.raw.test_finalcut_export import _plain_project
    from vlogsrt.project.raw import finalcut_export, timeline_srt
    from vlogsrt.project.raw import service as raw_service

    service, root = _plain_project(tmp_path)
    broken = _write_srt(tmp_path, "1\n시각 없는 줄\n")
    with pytest.raises(timeline_srt.TimelineSrtError, match="고친 자막.srt"):
        service.export_finalcut_timeline(root, timeline_srt=broken)
    folder = root / finalcut_export.EXPORT_FOLDER
    assert not folder.exists()  # 읽기 전에 거부한다 — 폴더도 만들지 않았다
    with pytest.raises(timeline_srt.TimelineSrtError, match="없는.srt"):
        service.export_finalcut_timeline(root, timeline_srt=tmp_path / "없는.srt")
    assert issubclass(timeline_srt.TimelineSrtError, raw_service.RawWorkspaceError)


# ── 작업대 단추 ───────────────────────────────────────────────────────────


@needs_ffmpeg
def test_the_workbench_button_picks_an_srt_sends_it_and_retries_with_the_same_file(
    tmp_path: Path,
) -> None:
    """선택기 취소는 아무것도 보내지 않는다 · 고른 SRT로 보내고 결과에 그 파일과 센 수를 말한다 ·
    재시도는 같은 SRT로 다시 건다(없어졌으면 다시 고르라고 한다). Final Cut은 가짜로만 연다."""
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
    assert path.name.endswith("_srt.fcpxml")
    status = str(server.capcut_status_view(signature))
    assert "Final Cut으로 보냈습니다" in status
    assert "`고친 자막.srt`의 줄 2개(캡션 조각 2개)" in status
    assert "밖이라 싣지 않은 줄 1개 · 끝이 잘린 줄 1개" in status and "6초에 끝납니다" in status
    assert "자막이 없는 영상" not in status  # 원본 자막의 빈 곳을 말하지 않는다

    assert "고친 자막.srt" in server._retry_finalcut(signature, finished)
    again = next(j for j in sends() if j.job_id != job.job_id)
    assert _wait_terminal(server.raw, root, again.job_id).status == "succeeded"
    assert len(opened) == 2 and "_srt" in opened[1].stem  # 같은 초면 `_srt_2`
    srt.unlink()
    assert "찾지 못해" in server._retry_finalcut(signature, again)
    assert len(sends()) == 2
