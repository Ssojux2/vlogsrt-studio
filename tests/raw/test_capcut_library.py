"""Primary original-media selection uses synthetic libraries and verified copies."""

from pathlib import Path

import gradio as gr
import pytest

from tests.raw.test_workspace_chooser_ui import _fcp_library, _server, _wait_terminal
from ui import chooser


def test_primary_library_selects_logical_clips_deduplicates_and_references(
    tmp_path: Path, monkeypatch
):
    library = _fcp_library(tmp_path)
    event = library / "2026-10-07" / "CurrentVersion.fcpevent"
    originals = event / "Original Media"
    proxies = event / "Transcoded Media" / "Proxy Media"
    (proxies / "proxy-only.mov").write_bytes(b"never-import-proxy")
    external = tmp_path / "외장" / "ext-clip.mov"
    (originals / "same-target.mov").symlink_to(external)
    other = tmp_path / "다른 외장" / "ext-clip.mov"
    other.parent.mkdir()
    other.write_bytes(b"different-original-same-name")
    (originals / "other-target.mov").symlink_to(other)
    before = {p: p.read_bytes() for p in (originals / "A001.MP4", external, other)}
    server = _server(tmp_path, library)
    # These library fixtures are byte strings, not playable video. The real-media
    # reference test separately exercises actual preview generation.
    monkeypatch.setattr(
        server.raw,
        "start_source_preview",
        lambda root, **kwargs: server.raw.submit_job(
            root, "source_preview", "synthetic-no-media", lambda ctx: None
        ),
    )
    view = server.capcut_fcp_choose_view()
    root = server.workspace_root()
    assert root and not root.is_relative_to(library)
    assert view[1] == server.current_signature()
    assert server._fcp_scan.role == "original"
    assert len(server._fcp_scan.selected) == 4  # originals + explicit proxy-only clip
    assert sum(file.role == "original" for file, _ in server._fcp_scan.selected) == 3
    assert server._fcp_scan.broken
    # The primary library action starts reference import without a second button.
    pipeline = next(j for j in server.raw.list_jobs(root) if j.kind == "reference_import")
    finished = _wait_terminal(server, root, pipeline.job_id)
    assert finished.status == "succeeded", finished.error
    assets = server.raw.list_source_assets(root)
    assert len(assets) == 4 and all(a.status == "ready" for a in assets)
    workspace = server.raw._workspace(root)
    referenced = {workspace.source_media_path(a).read_bytes() for a in assets}
    assert referenced == set(before.values()) | {b"never-import-proxy"}
    assert not any((root / a.relative_path).exists() for a in assets)
    assert all(p.read_bytes() == content for p, content in before.items())
    assert server.raw.latest_global_source_order(root) is not None
    assert server._path_chooser.calls == [chooser.WORKSPACE_KIND]


def test_advanced_library_subset_and_stale_project_guard(tmp_path: Path, monkeypatch):
    library = _fcp_library(tmp_path)
    server = _server(tmp_path, library)
    # These library fixtures are byte strings, not playable video. The real-media
    # reference test separately exercises actual preview generation.
    monkeypatch.setattr(
        server.raw,
        "start_source_preview",
        lambda root, **kwargs: server.raw.submit_job(
            root, "source_preview", "synthetic-no-media", lambda ctx: None
        ),
    )
    signature = server.open_fcp_library_view(library, originals_only=False)[1]
    with pytest.raises(gr.Error, match="목록이 바뀌"):
        server.capcut_fcp_import_view(signature, ["999"])
    root = server.workspace_root()
    server.capcut_fcp_import_view(signature, ["0"])
    pipeline = next(j for j in server.raw.list_jobs(root) if j.kind == "reference_import")
    assert _wait_terminal(server, root, pipeline.job_id).status == "succeeded"
    assert len(server.raw.list_source_assets(root)) == 1
    server.create_workspace_view("another", str(tmp_path / "another"))
    with pytest.raises(gr.Error):
        server.capcut_fcp_import_view(signature, ["0"])


def test_primary_library_button_has_actual_blocks_wiring(tmp_path: Path, local_socket: None):
    server = _server(tmp_path)
    app = server.build_blocks()
    chooser_fns = [fn for fn in app.fns.values() if fn.fn == server.capcut_fcp_choose_view]
    assert len(chooser_fns) == 2  # main toolbar and project menu share the same chooser
    assert all(server._fcp_originals_group in fn.outputs for fn in chooser_fns)
    assert all(server._signature_state in fn.outputs for fn in chooser_fns)
    importer_fns = [fn for fn in app.fns.values() if fn.fn == server.capcut_fcp_import_view]
    assert len(importer_fns) == 1
    assert server._fcp_originals_group in importer_fns[0].inputs
