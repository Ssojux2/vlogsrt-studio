"""D94 — 「꺼진 클립 빼고 보내기」: 켠(추천·「사용」) 클립만 프로젝트에 넣는다(2026-10-27).

시험이 못 박는 것:

- 끈 채 보내기(기본)는 바이트가 그대로다 — 이 옵션은 설정 하나로만 켜진다.
- 빼면 스파인에 켠 클립만 남고, 캡션은 남은 클립의 차례로 옮겨지며 뺀 클립의 캡션은 함께 빠진다.
- 원본 전체는 이벤트 브라우저 클립으로 남는다 — 분석 전 원본도 통째로 하나(키워드 「미분석」).
- 빠질 클립에 사람이 확인하지 않은 제외 제안이 있으면 거부한다(규칙 9·D35). 켠 클립이 없어도 거부.
- 레이트가 다른 원본의 시퀀스 길이는 남은 클립 길이로 누적하고, 뒤가 끊긴 클립은 제 미디어를
  넘지 않는다.
- SRT 타이틀은 짧아진 타임라인 시각으로 붙는다(옵션은 두 보내기 모두에 걸린다).
- 실제 보내기: 미검토 제외가 남으면 아무것도 쓰지 않고 거부하고, 확인한 뒤에는 켠 클립만 담은
  `_green` 파일과 뺀 수를 실은 리포트를 쓴다. 작업공간 표지·화면 칸이 그 옵션을 켠다.

유료 호출은 없다.
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_finalcut_timeline import NO_MINIMUM, at, media, report, seg, span
from vlogsrt.project.raw import finalcut_timeline as ft
from vlogsrt.project.raw import finalcut_titles as titles
from vlogsrt.project.raw.config import FINALCUT_TIMELINE

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

F = Fraction
DROP = replace(FINALCUT_TIMELINE, drop_disabled=True)
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def _three(**middle: bool) -> ft.SourceInput:
    """추천 · 보통 · 추천 120프레임씩, 조각마다 큐 하나."""
    return ft.SourceInput(
        media=media("three", frames=360, ident=None),
        segments=(seg(0, 120), seg(120, 240, **middle), seg(240, 360)),
        report=report(
            span(at(0), at(120), "green"),
            span(at(120), at(240), "yellow"),
            span(at(240), at(360), "green"),
        ),
        cues=(
            ft.Cue(at(10), at(60), "첫 줄"),
            ft.Cue(at(130), at(180), "보통 줄"),
            ft.Cue(at(250), at(300), "끝 줄"),
        ),
    )


def _plan(
    *sources: ft.SourceInput, settings: ft.FinalCutTimelineSettings = DROP
) -> ft.TimelineDocument:
    return ft.plan(list(sources), title="빼기", library_bundle=None, settings=settings)


def test_the_option_is_off_by_default_and_off_keeps_every_clip() -> None:
    assert FINALCUT_TIMELINE.drop_disabled is False
    whole = _plan(_three(), settings=FINALCUT_TIMELINE)
    assert len(whole.sources[0].clips) == 3 and whole.drop_disabled is False
    assert "dropped_clips" not in ft.document_counts(whole)


def test_dropping_keeps_only_enabled_clips_and_renumbers_their_captions() -> None:
    document = _plan(_three())
    (source,) = document.sources
    assert [(c.start_frame, c.end_frame) for c in source.clips] == [(0, 120), (240, 360)]
    assert all(clip.enabled for clip in source.clips)
    assert [(p.clip_index, p.text) for p in source.captions] == [(0, "첫 줄"), (1, "끝 줄")]
    assert (source.dropped_clips, source.dropped_seconds) == (1, 120 * media().frame_duration)
    counts = ft.document_counts(document)
    assert counts["dropped_clips"] == 1 and counts["spine_clips"] == 2
    assert ft.sequence_frame_total(document) == 240
    root = ET.fromstring(ft.render(document))
    spine = root.findall(".//spine/asset-clip")
    assert len(spine) == 2 and all(clip.get("enabled") != "0" for clip in spine)
    event = root.find(".//event")
    assert event is not None
    browser = event.findall("asset-clip")
    assert len(browser) == 1 and browser[0].get("duration") is not None  # 원본 전체가 남는다


def test_a_human_use_decision_is_kept_and_a_confirmed_exclusion_is_dropped() -> None:
    kept = _plan(_three(decided=True)).sources[0]
    assert len(kept.clips) == 3
    dropped = _plan(_three(excluded=True, decided=True)).sources[0]
    assert len(dropped.clips) == 2 and dropped.dropped_clips == 1


def test_unreviewed_exclusions_and_an_empty_project_are_refused() -> None:
    with pytest.raises(ft.UnreviewedDropError) as refused:
        _plan(_three(excluded=True, unreviewed=True))
    assert refused.value.count == 1 and refused.value.names == ("three",)
    never_proposed = ft.SourceInput(
        media=media("loose", frames=120, ident=None),
        segments=(seg(0, 120),),
        report=report(span(at(0), at(120), "red")),
    )
    with pytest.raises(ft.UnreviewedDropError):
        _plan(_three(), never_proposed)
    yellow = ft.SourceInput(
        media=media("yellow", frames=120, ident=None),
        segments=(seg(0, 120),),
        report=report(span(at(0), at(120), "yellow")),
    )
    with pytest.raises(ft.FinalCutTimelineError, match="켠 클립이 하나도 없다"):
        _plan(yellow)


def test_an_unanalysed_source_leaves_the_spine_but_stays_whole_in_the_browser() -> None:
    loose = ft.SourceInput(media=media("loose", frames=90, ident=None))
    document = _plan(_three(), loose)
    unanalysed = document.sources[1]
    assert unanalysed.clips == () and unanalysed.dropped_clips == 1
    assert unanalysed.browser == (ft.BrowserRange(0, 90, ft.KEYWORD_UNANALYZED, False),)
    event = ET.fromstring(ft.render(document)).find(".//event")
    assert event is not None and len(event.findall("asset-clip")) == 2


def test_a_minority_rate_source_lays_out_kept_clips_without_reaching_into_dropped_media() -> None:
    """24fps 원본(0–25 추천 · 25–50 보통 · 50–75 추천)이 30fps 시퀀스에 — 25프레임 = 31.25프레임.

    누적 경계로 맞추되 뒤가 끊긴 클립은 제 미디어를 넘지 않는다(31 · 31). 1초짜리 추천이라 추천 최소
    길이(D60)를 끈 등급 설정으로 본다."""
    film = F(1, 24)
    minority = ft.SourceInput(
        media=media("film", frames=75, ident=None, fd=film, drop=False, tc=0),
        segments=(seg(0, 25), seg(25, 50), seg(50, 75)),
        report=report(
            span(F(0), 25 * film, "green"),
            span(25 * film, 50 * film, "yellow"),
            span(50 * film, 75 * film, "green"),
            settings=NO_MINIMUM,
        ),
    )
    majority = ft.SourceInput(
        media=media("video", frames=900, ident=None, fd=F(1, 30), drop=False, tc=0),
        segments=(seg(0, 900),),
        report=report(span(F(0), F(30), "green")),
    )
    document = _plan(majority, minority, settings=replace(DROP, frame_rate_tie="higher"))
    assert document.sequence_frame_duration == F(1, 30)
    film_plan = document.sources[1]
    assert film_plan.sequence_frames == (31, 31)
    for clip, frames in zip(film_plan.clips, film_plan.sequence_frames, strict=True):
        assert frames * F(1, 30) <= (clip.end_frame - clip.start_frame) * film


def test_srt_titles_land_on_the_shortened_timeline() -> None:
    document = _plan(_three())
    second_start = 120 * media().frame_duration  # 짧아진 타임라인에서 둘째 남은 클립의 시작
    with_titles, counts = titles.with_srt_titles(
        document, (ft.Cue(second_start + F(1), second_start + F(2), "둘째 클립 위"),)
    )
    (piece,) = with_titles.sources[0].titles
    assert piece.clip_index == 1 and piece.enabled and counts.placed == 1


# ── 실제 보내기 ──────────────────────────────────────────────────────────


@needs_ffmpeg
def test_a_real_export_refuses_unreviewed_drops_then_writes_a_green_project(
    tmp_path: Path,
) -> None:
    from tests.raw.test_auto_cleanup_review_gate import _synthetic_project
    from vlogsrt.project.raw.finalcut_export import FinalCutExportError
    from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade

    service, root = _synthetic_project(tmp_path)
    assert service.finalcut_drop_disabled(root) is False
    assert service.set_finalcut_drop_disabled(root, True) is True
    folder = root / "review" / "finalcut"
    before = sorted(folder.glob("*")) if folder.exists() else []
    with pytest.raises(FinalCutExportError, match="확인하지 않은 제외 제안"):
        service.export_finalcut_timeline(root)
    after = sorted(path for path in folder.glob("*") if not path.name.startswith("."))
    assert after == [path for path in before if not path.name.startswith(".")]  # 쓰지 않았다
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    used = next(s for s in head.segments if s.selection == "excluded")
    facade.set_selection(head.base_plan_id, used.segment_id, "included")  # 사람이 「사용」으로
    facade.confirm_automatic_exclusions()
    written = service.export_finalcut_timeline(root)
    assert written.name.endswith("_green.fcpxml")
    spine = ET.parse(written).findall(".//spine/asset-clip")
    assert spine and all(clip.get("enabled") != "0" for clip in spine)
    document = json.loads(written.with_suffix(".json").read_text(encoding="utf-8"))
    assert document["counts"]["dropped_clips"] >= 1 and "추천만" in document["title"]
    assert service.set_finalcut_drop_disabled(root, False) is False
    assert not service.export_finalcut_timeline(root).name.endswith("_green.fcpxml")


def test_the_settings_drawer_toggle_saves_the_workspace_marker(tmp_path: Path) -> None:
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from ui import workspace_service as ws

    server = ws.RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("빼기", str(tmp_path / "workspace"))
    assert server.finalcut_drop_value() is False
    signature = server.current_signature()
    assert server.finalcut_drop_view(signature, True) == ws.FINALCUT_DROP_ON_TEXT
    assert server.finalcut_drop_value() is True
    assert server.finalcut_drop_view(signature, False) == ws.FINALCUT_DROP_OFF_TEXT
    assert server.raw.finalcut_drop_disabled(server.workspace_root()) is False
