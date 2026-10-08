from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt.project.raw import media_refs
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.runner import SourceEvidence
from vlogsrt.project.raw.service import RawWorkspaceService


def transcode(source, target, *extra):
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            "scale=160:90",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "copy",
            *extra,
            str(target),
        ],
        check=True,
        capture_output=True,
    )


def test_measured_pair_direct_playback_original_export_and_unchanged_library(tmp_path):
    original = tmp_path / "Library.fcpbundle/Event/Original Media/clip.mov"
    proxy = tmp_path / "Library.fcpbundle/Event/Transcoded Media/Proxy Media/clip.mp4"
    _make_media(original, audio=True, seconds="3")
    transcode(original, proxy)
    (original.parent.parent / "CurrentVersion.fcpevent").touch()
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, proxy)}
    service = RawWorkspaceService()
    state, _ = service.managed_fcp_workspace(tmp_path / "Library.fcpbundle")
    root = Path(state.workspace_root)
    scan = service.scan_fcp_library(root, tmp_path / "Library.fcpbundle")
    job = service.start_reference_import(root, scan.proposal, fcp_scan=scan)
    result = _wait_terminal(service, root, job.job_id)
    assert result.status == "succeeded", result.error
    (asset,) = service.list_source_assets(root)
    workspace = service._workspace(root)
    refs = media_refs.load(workspace, asset.asset_id)
    assert refs["validation"]["status"] == "verified", refs
    assert refs["validation"]["audio_verified"]
    assert refs["validation"]["offset_frames"] == 0
    view = service.visual_review_query(root)
    assert view.sources[0].state == "ready"
    assert service.resolve_media(root, view.sources[0].registered_file_id).path == proxy
    assert not list((root / "previews/visual").glob("*.mp4"))
    assert media_refs.frame_sampler(workspace, asset).source == proxy
    assert SourceEvidence(workspace, asset).path == original
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_same_name_wrong_picture_and_duration_never_pair(tmp_path):
    a, b, c = (tmp_path / name for name in ("a.mov", "b.mp4", "c.mp4"))
    _make_media(a, audio=False, seconds="3")
    transcode(a, b, "-t", "2")
    assert media_refs.validate_pair(a, b).status == "rejected"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=160x90:rate=25:duration=3",
            "-c:v",
            "libx264",
            str(c),
        ],
        check=True,
        capture_output=True,
    )
    assert media_refs.validate_pair(a, c).status == "rejected"


def test_proxy_only_review_and_verified_relink(tmp_path):
    source = tmp_path / "original.mov"
    proxy = tmp_path / "P.fcpbundle/Event/Transcoded Media/Proxy Media/clip.mp4"
    _make_media(source, audio=False, seconds="3")
    transcode(source, proxy)
    (proxy.parents[2] / "CurrentVersion.fcpevent").touch()
    service = RawWorkspaceService()
    state, _ = service.managed_fcp_workspace(tmp_path / "P.fcpbundle")
    root = Path(state.workspace_root)
    scan = service.scan_fcp_library(root, tmp_path / "P.fcpbundle")
    job = service.start_reference_import(root, scan.proposal, fcp_scan=scan)
    assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    (asset,) = service.list_source_assets(root)
    workspace = service._workspace(root)
    assert media_refs.resolve(workspace, asset, "frames") == proxy
    with pytest.raises(RawWorkspaceError, match="원본 미연결"):
        SourceEvidence(workspace, asset)
    media_refs.relink_original(workspace, asset.asset_id, source)
    assert SourceEvidence(workspace, asset).path == source
    assert len(service.list_source_assets(root)) == 1


def test_shifted_proxy_is_not_a_verified_identity_mapping(tmp_path):
    original, proxy = tmp_path / "original.mov", tmp_path / "shifted.mp4"
    _make_media(original, audio=False, seconds="3")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(original),
            "-vf",
            "tpad=start_mode=clone:start_duration=0.12,trim=duration=3,scale=160:90",
            "-an",
            "-c:v",
            "libx264",
            str(proxy),
        ],
        check=True,
        capture_output=True,
    )
    assert media_refs.validate_pair(original, proxy).status == "rejected"
