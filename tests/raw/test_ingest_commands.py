"""A2 진단 CLI(`vlogsrt media`) — 실제 ffprobe 확인 경로를 지나는 종단 시험.

합성 MP4를 ffmpeg로 만들어(로컬 바이너리, 네트워크 없음) init → import →
inspect → retry의 실제 흐름을 돈다. `--confirm`이 없으면 제안만 인쇄하고 아무
것도 쓰지 않는다는 것도 이 묶음의 계약이다.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import pytest

from vlogsrt.project.raw import commands as media
from vlogsrt.project.raw import ingest

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _make_mp4(path: Path, seconds: str = "0.4") -> None:
    """검은 화면+무음의 아주 작은 실제 MP4 — ffprobe가 영상임을 확인한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=black:s=64x32:r=10:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )


def _import_args(source: Path, workspace: Path, confirm: bool) -> argparse.Namespace:
    return argparse.Namespace(
        media_command="import",
        source=source,
        workspace=workspace,
        confirm=confirm,
    )


@pytest.fixture
def card(tmp_path: Path) -> Path:
    source = tmp_path / "card-a" / "DCIM" / "100MEDIA"
    _make_mp4(source / "DJI_0002.MP4")
    _make_mp4(source / "DJI_0010.MP4")
    return source.parent.parent


@needs_ffmpeg
def test_media_init_creates_workspace(tmp_path: Path) -> None:
    assert (
        media.cmd_init(argparse.Namespace(media_command="init", path=tmp_path / "ws", name="제주"))
        == 0
    )
    assert (tmp_path / "ws" / "project.toml").is_file()


@needs_ffmpeg
def test_media_import_without_confirm_prints_plan_and_copies_nothing(
    tmp_path: Path, card: Path
) -> None:
    media.cmd_init(argparse.Namespace(media_command="init", path=tmp_path / "ws", name="제주"))
    assert media.cmd_import(_import_args(card, tmp_path / "ws", confirm=False)) == 0
    originals = tmp_path / "ws" / "originals"
    assert list(originals.rglob("*.MP4")) == []  # 제안만 인쇄했다 — 파일은 없다


@needs_ffmpeg
def test_media_import_with_confirm_copies_and_verifies(tmp_path: Path, card: Path) -> None:
    """실제 ffprobe 확인 → 확정 → 복사·검증까지 한 명령으로 — 실합성 종단."""
    media.cmd_init(argparse.Namespace(media_command="init", path=tmp_path / "ws", name="제주"))
    assert media.cmd_import(_import_args(card, tmp_path / "ws", confirm=True)) == 0

    from vlogsrt.project.raw import service as raw_service

    service = raw_service.RawWorkspaceService()
    assets = service.list_source_assets(tmp_path / "ws")
    assert [asset.status for asset in assets] == ["ready", "ready"]
    assert [asset.source_name for asset in assets] == ["DJI_0002.MP4", "DJI_0010.MP4"]  # 자연 정렬
    for asset in assets:
        assert asset.duration_seconds is not None and asset.duration_seconds > 0
        assert asset.width == 64 and asset.height == 32
        assert asset.capture_time is None  # 촬영 시각은 여전히 미조사다
        assert service.resolve_media(tmp_path / "ws", asset.file_id).size_bytes == asset.size_bytes


@needs_ffmpeg
def test_media_retry_completes_after_failure(tmp_path: Path, card: Path) -> None:
    media.cmd_init(argparse.Namespace(media_command="init", path=tmp_path / "ws", name="제주"))
    broken = {"active": True}
    real_copy = ingest._copy_with_hash

    def failing(source: Path, target: Path) -> str:
        if broken["active"] and source.name == "DJI_0010.MP4":
            raise OSError("카드 분리")
        return real_copy(source, target)

    ingest._copy_with_hash = failing  # type: ignore[assignment]
    try:
        assert media.cmd_import(_import_args(card, tmp_path / "ws", confirm=True)) == 1
    finally:
        ingest._copy_with_hash = real_copy  # type: ignore[assignment]

    from vlogsrt.project.raw import service as raw_service

    service = raw_service.RawWorkspaceService()
    (job,) = [job for job in service.list_jobs(tmp_path / "ws") if job.kind == "ingest_copy"]
    assert job.status == "failed"

    broken["active"] = False
    assert (
        media.cmd_retry(
            argparse.Namespace(media_command="retry", workspace=tmp_path / "ws", job=job.job_id)
        )
        == 0
    )
    assets = service.list_source_assets(tmp_path / "ws")
    assert [asset.status for asset in assets] == ["ready", "ready"]


@needs_ffmpeg
def test_media_inspect_reports_ready_assets(tmp_path: Path, card: Path) -> None:
    media.cmd_init(argparse.Namespace(media_command="init", path=tmp_path / "ws", name="제주"))
    assert media.cmd_import(_import_args(card, tmp_path / "ws", confirm=True)) == 0
    assert (
        media.cmd_inspect(
            argparse.Namespace(media_command="inspect", workspace=tmp_path / "ws", batch=None)
        )
        == 0
    )
