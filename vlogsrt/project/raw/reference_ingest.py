"""Explicit read-only FCP original references; only metadata lives in the workspace."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vlogsrt.project.raw.ingest import IngestProposal, confirm_ingest
from vlogsrt.project.raw.jobs import JobContext
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.workspace import RawWorkspace


def register_references(
    workspace: RawWorkspace, proposal: IngestProposal, context: JobContext
) -> None:
    existing = {
        str(Path(asset.source_root) / asset.source_relative_path): asset
        for asset in workspace.list_source_assets()
    }
    pending = []
    selected = []
    for item in proposal.order:
        source = str(Path(item.source_root) / item.source_relative_path)
        asset = existing.get(source)
        if asset and (asset.size_bytes, asset.source_mtime_ns, asset.source_inode) == (
            item.size_bytes,
            item.mtime_ns,
            item.inode,
        ):
            selected.append(asset)
        else:
            pending.append(item)
    if pending:
        from dataclasses import replace

        confirmation = confirm_ingest(
            workspace,
            replace(
                proposal,
                order=tuple(pending),
                scan=replace(proposal.scan, candidates=tuple(pending)),
            ),
        )
        selected.extend(confirmation.assets)
    context.set_total(len(selected))
    errors = []
    for index, asset in enumerate(selected, 1):
        context.check_cancel()
        try:
            workspace.mark_source_reference(asset.asset_id)
            path = workspace.source_media_path(asset)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    context.check_cancel()
                    digest.update(chunk)
            workspace.source_media_path(asset)  # stat/inode/mtime must still match after hashing
            actual = digest.hexdigest()
            if asset.content_sha256 and asset.content_sha256 != actual:
                raise RawWorkspaceError("원본 내용이 기존 검증 기록과 다릅니다")
            workspace.update_source_asset(
                asset.asset_id, status="ready", content_sha256=actual, error=None
            )
        except (OSError, RawWorkspaceError) as error:
            workspace.update_source_asset(asset.asset_id, status="failed", error=str(error))
            errors.append(f"{asset.source_name}: {error}")
        context.set_progress(index)
    if errors:
        raise RawWorkspaceError("참조 원본 확인 실패 · " + "; ".join(errors))
