from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal, _write_replay_fixtures
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.service import RawWorkspaceService


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_reference_originals_preview_without_analysis_no_copy_and_restart(tmp_path: Path):
    source = tmp_path / "external.mov"
    made = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-x264-params",
            "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            str(source),
        ],
        capture_output=True,
    )
    assert made.returncode == 0, made.stderr
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    library = tmp_path / "Synthetic.fcpbundle"
    event = library / "Event"
    originals = event / "Original Media"
    originals.mkdir(parents=True)
    (event / "CurrentVersion.fcpevent").write_bytes(b"synthetic")
    (originals / "linked.mov").symlink_to(source)
    service = RawWorkspaceService()
    state, _ = service.managed_fcp_workspace(library)
    root = Path(state.workspace_root)
    proposal = service.scan_fcp_library(root, library, originals_only=True).proposal
    handle = service.start_reference_import(root, proposal)
    result = _wait_terminal(service, root, handle.job_id)
    assert result.status == "succeeded", result.error
    assets = service.list_source_assets(root)
    assert len(assets) == 1 and assets[0].status == "ready"
    workspace = service._workspace(root)
    assert workspace.source_media_path(assets[0]) == source
    assert not (root / assets[0].relative_path).exists()
    assert list((root / "originals").rglob("*.mov")) == []
    assert service.analysis_overview(root) is None
    overview = service.visual_review_query(root)
    assert overview.sources[0].state == "ready"
    preview_id = overview.sources[0].registered_file_id
    assert preview_id and service.resolve_media(root, preview_id).path.is_file()
    retry = service.start_reference_import(root, proposal)
    assert _wait_terminal(service, root, retry.job_id).status == "succeeded"
    assert len(service.list_source_assets(root)) == 1
    reopened = RawWorkspaceService()
    reopened.open_workspace(root)
    assert reopened._workspace(root).source_media_path(assets[0]) == source
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    _write_replay_fixtures(
        root,
        {assets[0].content_sha256: [{"start": 0.2, "end": 1.2, "text": "참조 영상 분석입니다."}]},
    )
    analysis = reopened.start_analysis(root, mode="replay")
    analyzed = _wait_terminal(reopened, root, analysis.job_id)
    assert analyzed.status == "succeeded", analyzed.error
    cleanup = reopened.start_cleanup(root)
    cleaned = _wait_terminal(reopened, root, cleanup.job_id)
    assert cleaned.status == "succeeded", cleaned.error
    assert reopened.package_overview(root).package.complete
    assert not (root / assets[0].relative_path).exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    source.rename(tmp_path / "offline.mov")
    with pytest.raises(RawWorkspaceError, match="원본 연결"):
        reopened._workspace(root).source_media_path(assets[0])
    assert reopened.review_workspace_facade(root).load_head().stale_sources
