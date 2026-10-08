"""Synthetic media crosses ingest, analysis, review, extraction and FCPXML boundaries."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal, _write_replay_fixtures
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.service import RawWorkspaceService
from vlogsrt.project.timebase import parse_fcpx_time


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_synthetic_cleanup_restore_restart_and_final_cut(tmp_path: Path):
    source = tmp_path / "synthetic.mov"
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=12",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-t",
            "12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-x264-params",
            "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "합성 인수")
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.confirm_global_source_order(root)
    asset = service.list_source_assets(root)[0]
    _write_replay_fixtures(
        root,
        {
            asset.content_sha256: [
                {"start": 0.5, "end": 1.5, "text": "오늘은 여행을 갑니다."},
                {"start": 5, "end": 6, "text": "오늘은 여행을 갑니다."},
                {"start": 8, "end": 9, "text": "다음 장소로 갑니다."},
            ]
        },
    )
    luna_path = root / "analysis/replay/luna-fixtures.json"
    fixtures = json.loads(luna_path.read_text())
    fixtures["windows"]["window_000000"]["scene_tags"] = ["dialogue"]
    luna_path.write_text(json.dumps(fixtures))
    analysis = service.start_analysis(root, mode="replay")
    assert _wait_terminal(service, root, analysis.job_id).status == "succeeded"
    # Old successful caches did not contain decisions. Reuse their saved evidence
    # without provider calls or rewriting immutable analysis records.
    summary = service.analysis_overview(root)
    record_path = root / summary.results[0].record_path
    legacy = json.loads(record_path.read_text())
    legacy.pop("auto_decisions")
    record_path.write_text(json.dumps(legacy))
    legacy_bytes = record_path.read_bytes()
    cleanup = service.start_cleanup(root)
    finished = _wait_terminal(service, root, cleanup.job_id)
    assert finished.status == "succeeded", finished.error
    assert record_path.read_bytes() == legacy_bytes
    first_package = service.package_overview(root).package.package_dir
    repeat_cleanup = service.start_cleanup(root)
    repeated = _wait_terminal(service, root, repeat_cleanup.job_id)
    assert repeated.status == "succeeded", repeated.error
    assert service.package_overview(root).package.package_dir == first_package
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    excluded = [s for s in head.segments if s.selection == "excluded"]
    assert len(excluded) >= 2  # repeated take plus measured internal silence
    filtered = service.visual_review_query(root, selection_filter="excluded")
    assert filtered.total_segments == len(excluded)
    assert all(s.selection == "excluded" for s in filtered.segments)
    assert all("analysis_revision_id" in s.notes and "source_sha256" in s.notes for s in excluded)
    # Replacement is kept, even when an earlier exact take was removed.
    clock = head.clocks[0]
    assert any(
        s.selection == "included"
        and s.start_pts * clock.time_base <= 5
        and s.end_pts * clock.time_base >= 6
        for s in head.segments
    )
    handles = service.start_segment_extraction(
        root, head.base_plan_id, handles_before=Fraction(3, 10), handles_after=Fraction(3, 10)
    )
    handle_result = _wait_terminal(service, root, handles.job_id)
    assert handle_result.status == "succeeded", handle_result.error
    destination, xml_path = service.export_final_cut(root)
    xml = ET.parse(xml_path)
    manifest = json.loads((root / destination / "confirmed_manifest.json").read_text())
    assert len(manifest["clips"]) == sum(s.selection == "included" for s in head.segments)
    assert not {s.segment_id for s in excluded}.intersection(
        c["segment_id"] for c in manifest["clips"]
    )
    expected = sum(
        (s.end_pts - s.start_pts) * clock.time_base
        for s in head.segments
        if s.selection == "included"
    )
    assert parse_fcpx_time(xml.find(".//sequence").attrib["duration"]) == expected
    assert any(
        parse_fcpx_time(item.attrib["start"]) > 0 for item in xml.findall(".//spine/asset-clip")
    )
    restored = facade.restore_automatic()
    assert all(s.selection == "included" for s in restored.segments)
    reopened = ReviewWorkspaceFacade(root)
    assert reopened.load_head().revision == restored
    undone = reopened.undo()
    assert [s.selection for s in undone.segments] == [s.selection for s in head.segments]
    changed = reopened.set_selection(head.base_plan_id, excluded[0].segment_id, "included")
    new_plan = service.plan_segmentation_from_analysis(root)
    reanalyzed = reopened.initialize_review(new_plan.revision_id)
    assert [(s.start_pts, s.end_pts, s.selection) for s in reanalyzed.segments] == [
        (s.start_pts, s.end_pts, s.selection) for s in changed.segments
    ]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_final_video_replacement_clears_binding_and_persists_selection(tmp_path: Path):
    from tests.raw.test_full_ui_screens import _final_video, _two_track_final_video
    from ui.workspace_service import RawWorkspaceUi

    first, second = tmp_path / "first.mp4", tmp_path / "second.mov"
    _final_video(first)
    _two_track_final_video(second)
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    signature = server.create_workspace_view("자막 교체", str(tmp_path / "workspace"))[1]
    server.caption_register_view(signature, str(first), "final_1", "자동(첫 트랙)")
    server.caption_estimate_view(signature, "run-1")
    old_identity = dict(server._caption_identity)
    token = server._caption_lifetime_token
    server.caption_register_view(signature, str(second), "final_1", "1")
    assert server._caption_identity is None
    assert server._caption_cue_rows == ()
    assert server._caption_lifetime_token != token
    server.caption_estimate_view(signature, "run-1")
    assert server._caption_identity["revision"] != old_identity["revision"]
    assert server._caption_identity["audio_ordinal"] == 1
    root = server.workspace_root()
    restarted = RawWorkspaceUi(state_path=tmp_path / "ui-state2.json")
    signature = restarted.open_workspace_view(str(root))[1]
    restarted.caption_estimate_view(signature, "run-1")
    assert restarted._caption_identity == server._caption_identity
    # Selecting the older render again is explicit and survives another reopen.
    restarted.caption_register_view(signature, str(first), "final_1", "자동(첫 트랙)")
    restarted.caption_estimate_view(signature, "run-1")
    assert restarted._caption_identity["revision"] == old_identity["revision"]
