"""D61 — 작업대가 기다리게 하는 ffmpeg 단계가 이름을 달고, 입력 경로가 원본 이름으로 읽힌다.

「지금 도는 작업」 칸은 등록표(`preprocess.active_processes`)의 단계 이름과 입력 경로를 보인다.
썸네일·미리보기는 이름이 없어 「ffmpeg」으로만 보였고, 분석 프록시에서 뽑는 프레임은
`proxy.mp4`로만 보였다 — 어느 영상인지 알 수 없었다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_visual_review import (
    ONE_SECOND_TILE,
    _prepared_page,
    _review_ready,
    _voiced_media,
    needs_ffmpeg,
)
from vlogsrt import preprocess
from vlogsrt.project.raw import media_refs
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.models import SourceAssetDraft


@pytest.fixture()
def service() -> raw_service.RawWorkspaceService:
    return raw_service.RawWorkspaceService()


@needs_ffmpeg
def test_visual_preparation_names_its_preview_and_thumbnail_steps(
    service: raw_service.RawWorkspaceService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ws"
    service.create_workspace(root, "단계")
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "짧은.mp4", "3")
    base = _review_ready(service, root, card, ONE_SECOND_TILE)
    steps: list[str | None] = []
    real_run = preprocess._run

    def spy(command: list[str], **kwargs: Any) -> Any:
        steps.append(kwargs.get("step") or preprocess.current_process_step())
        return real_run(command, **kwargs)

    monkeypatch.setattr(preprocess, "_run", spy)
    _, record = _prepared_page(service, root, base, page=1)
    assert record.status == "succeeded", record.error
    assert "미리보기 만들기" in steps
    assert "썸네일 추출" in steps  # 썸네일 자리가 프레임 추출의 일반 이름을 덮는다
    assert None not in steps  # 이 흐름의 ffmpeg·ffprobe는 모두 이름이 있다


def _draft(root: Path, name: str, duration: float | None) -> SourceAssetDraft:
    return SourceAssetDraft(
        source_root=str(root),
        source_relative_path=f"Original Media/{name}",
        source_name=name,
        size_bytes=10,
        source_mtime_ns=1,
        source_inode=1,
        capture_time=None,
        duration_seconds=duration,
        width=None,
        height=None,
        ordinal=1,
        basis_note="시험",
    )


def test_source_media_names_cover_copies_references_and_analysis_proxies(
    service: raw_service.RawWorkspaceService, tmp_path: Path
) -> None:
    root = tmp_path / "ws"
    service.create_workspace(root, "이름")
    library = tmp_path / "Library.fcpbundle"
    workspace = service._workspace(root)
    (asset,), _ = workspace.create_ingest_batch(
        (_draft(library, "DJI_20260901_0016_D.mov", 545.0),), "시험"
    )
    sha = "ab" * 32
    workspace.update_source_asset(asset.asset_id, content_sha256=sha)
    db = root / "project.sqlite3"
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    names = service.source_media_names(root)

    expected = ("DJI_20260901_0016_D.mov", 545.0)
    assert names[str(root / asset.relative_path)] == expected
    assert names[str(library / "Original Media" / "DJI_20260901_0016_D.mov")] == expected
    proxy = media_refs._proxy_directory(workspace, sha, media_refs.ANALYSIS_PROXY)
    assert names[str(proxy / "proxy.mp4")] == expected  # 프록시에서 뽑는 프레임도 원본 이름으로
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before  # 읽기만 한다
