"""원본 범위 타임라인에서 렌더(W-71e, D130) — 클립 그대로 원본에서 한 번 렌더해 초안 폴더에 낸다.

손대지 않은 타임라인은 초안 내보내기와 같은 조각·같은 프레임 수·같은 SRT다. 고친 타임라인은
`edited_frames` 프레임이고 SRT는 `timeline_edit.srt_text` 바이트다. 보고서는 타임라인 아이디·
리비전을 싣고 뺀 구간(`timeline_cut`)·가린 자막 수를 남긴다. 「SRT만 다시 내보내기」는 그 폴더를
거부한다(원본 자막을 다시 얹으면 타임라인 자막을 조용히 바꾼다). 화면이 본 리비전이 아니거나
원본 격자가 파일의 사실과 다르면 렌더하지 않는다.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_render_export import Folder, _streams
from tests.raw.test_timeline_edit_io import _create, _strict, trip  # noqa: F401 — 모듈 범위 픽스처
from vlogsrt.project.raw import (
    render_export,
    render_plan,
    timeline_edit,
    timeline_edit_io,
    timeline_render,
)
from vlogsrt.project.raw.final_edit_config import FINAL_EDIT_SETTINGS
from vlogsrt.project.raw.timeline_edit import TimelineEditError

pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

MOMENT = datetime(2026, 11, 13, 5, 0, 0, tzinfo=UTC)


def _render(owner: Folder, folder: Path, revision: int, minute: int) -> Any:
    return timeline_render.export_timeline(
        owner.service,
        owner.root,
        folder.name,
        revision,
        now=lambda: MOMENT.replace(minute=minute),
    )


def _report(result: Any) -> dict[str, Any]:
    return json.loads(result.report.read_text(encoding="utf-8"))


def test_the_untouched_timeline_renders_like_the_draft(trip: Folder) -> None:  # noqa: F811
    draft = render_export.export_draft(
        trip.service, trip.root, now=lambda: MOMENT.replace(minute=1)
    )
    created = _create(trip, MOMENT.replace(minute=2))
    result = _render(trip, created.folder, 1, 3)
    assert result.folder.parent == draft.folder.parent
    keep = ("source_id", "start_frame", "end_frame", "output_start_frame", "output_frames")
    pick = [{k: getattr(p, k) for k in keep} for p in result.plan.parts]
    assert pick == [{k: getattr(p, k) for k in keep} for p in draft.plan.parts]
    assert result.plan.total_frames == draft.plan.total_frames
    assert result.srt.read_bytes() == draft.srt.read_bytes()
    assert _streams(result.video)["video"]["nb_read_frames"] == str(draft.plan.total_frames)
    report = _report(result)
    assert report["timeline"] == {"id": created.folder.name, "revision": 1}
    assert {row["reason"] for row in report["dropped"]} <= {render_plan.DROP_TIMELINE_CUT}


def test_an_edited_timeline_renders_its_frames_and_its_srt(trip: Folder) -> None:  # noqa: F811
    created = _create(trip, MOMENT.replace(minute=4))
    document = created.document
    index = next(
        i
        for i, clip in enumerate(document.clips)
        if timeline_edit.clip_bounds(document, i)[1] > clip.end_frame
    )
    first = document.clips[index]
    room = timeline_edit.clip_bounds(document, index)
    edited = timeline_edit.set_clip_end(document, index, room[1], FINAL_EDIT_SETTINGS)
    saved = timeline_edit_io.save_timeline(
        trip.root, created.folder.name, 1, timeline_edit.to_payload(edited)
    )
    assert saved["revision"] == 2 and room[1] > first.end_frame
    result = _render(trip, created.folder, 2, 5)
    assert result.plan.total_frames == timeline_edit.edited_frames(edited)
    assert result.srt.read_text(encoding="utf-8") == timeline_edit.srt_text(edited)
    assert _streams(result.video)["video"]["nb_read_frames"] == str(result.plan.total_frames)
    report = _report(result)
    assert report["timeline"]["revision"] == 2
    counts = report["cue_counts"]
    assert counts["source"] == len(edited.cues)
    assert counts["source"] == counts["kept"] + counts["dropped"] + counts["empty"]
    removed = timeline_edit.removed_ranges(edited)
    assert len(report["dropped"]) == len(removed)


def test_a_deleted_clip_is_reported_as_a_timeline_cut_not_lost(trip: Folder) -> None:  # noqa: F811
    """W-71f · 규칙 9 — 화면에서 지운 클립의 원본 구간은 렌더 보고서의 `timeline_cut`으로 남는다."""
    created = _create(trip, MOMENT.replace(minute=11))
    document = created.document
    assert len(document.clips) > 1
    gone = document.clips[0]
    edited = timeline_edit.delete_clips(document, [0])
    timeline_edit_io.save_timeline(
        trip.root, created.folder.name, 1, timeline_edit.to_payload(edited)
    )
    result = _render(trip, created.folder, 2, 12)
    assert result.plan.total_frames == timeline_edit.edited_frames(edited)
    step = next(s.frame_duration for s in document.facts.sources if s.source_id == gone.source_id)
    covering = [
        row
        for row in result.plan.dropped
        if row.source_id == gone.source_id
        and row.reason == render_plan.DROP_TIMELINE_CUT
        and row.start <= gone.start_frame * step
        and gone.end_frame * step <= row.end
    ]
    assert len(covering) == 1
    assert any(row["reason"] == render_plan.DROP_TIMELINE_CUT for row in _report(result)["dropped"])


def test_an_added_source_renders_whole_with_its_seeded_subtitles(trip: Folder) -> None:  # noqa: F811
    """W-71g — 넣은 원본은 원본 전체 클립으로 렌더되고 SRT는 심은 자막까지 문서 그대로다."""
    created = _strict(trip, 13)
    clipped = {clip.source_id for clip in created.document.clips}
    source = next(s for s in created.document.facts.sources if s.source_id not in clipped)
    got = trip.service.timeline_add_source(
        trip.root,
        created.folder.name,
        1,
        timeline_edit.to_payload(created.document),
        source.source_id,
    )
    _, _, document = timeline_edit_io.read_document(trip.root, created.folder.name)
    result = _render(trip, created.folder, got["revision"], 14)
    assert result.plan.total_frames == timeline_edit.edited_frames(document)
    assert result.srt.read_text(encoding="utf-8") == timeline_edit.srt_text(document)
    assert any(
        (p.source_id, p.start_frame, p.end_frame) == (source.source_id, 0, source.frame_count)
        for p in result.plan.parts
    )


def test_srt_again_refuses_a_timeline_render(trip: Folder) -> None:  # noqa: F811
    created = _create(trip, MOMENT.replace(minute=6))
    _render(trip, created.folder, 1, 7)
    with pytest.raises(render_export.RenderExportError, match="타임라인"):
        render_export.export_srt_again(trip.service, trip.root)


def test_a_stale_revision_or_changed_source_grid_is_refused_before_rendering(
    trip: Folder,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create(trip, MOMENT.replace(minute=8))
    before = sorted(p.name for p in (trip.root / render_export.EXPORT_FOLDER).iterdir())
    with pytest.raises(TimelineEditError, match="리비전"):
        _render(trip, created.folder, 7, 9)
    real = timeline_edit_io.read_document

    def shifted(root: Path, timeline_id: str | None) -> Any:
        name, revision, document = real(root, timeline_id)
        source = document.facts.sources[0]
        moved = dataclasses.replace(source, frame_count=source.frame_count + 1)
        facts = dataclasses.replace(document.facts, sources=(moved, *document.facts.sources[1:]))
        return name, revision, dataclasses.replace(document, facts=facts)

    monkeypatch.setattr(timeline_edit_io, "read_document", shifted)
    with pytest.raises(render_export.RenderExportError, match="격자"):
        _render(trip, created.folder, 1, 10)
    after = sorted(p.name for p in (trip.root / render_export.EXPORT_FOLDER).iterdir())
    assert after == before


def test_the_service_job_is_a_render_export_with_the_timeline_in_its_fingerprint(
    trip: Folder,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vlogsrt.project.raw import service as raw_service

    asked: list[tuple[str, str]] = []

    def submit(_root: Path, kind: str, fingerprint: str, _body: Any) -> Any:
        asked.append((kind, fingerprint))
        return None

    monkeypatch.setattr(trip.service, "submit_job", submit)
    trip.service.start_timeline_render(trip.root, "20261113T000000Z", 4)
    assert asked == [
        (raw_service.RENDER_EXPORT_JOB_KIND, "render_export/timeline/20261113T000000Z@4")
    ]
