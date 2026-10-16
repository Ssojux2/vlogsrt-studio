"""A2 스캔 — 발견·조사·공간 견적·보호 경계(계획 §5.1·§5.2).

조사자는 주입한다(가짜 조사자) — ffmpeg를 부르지 않고도 후보·제외·거부 규칙을
시험한다. 보호 이름(.env 등)은 **stat 이전에** 어휘로 가지치기한다는 것을
os.lstat 포장으로 증명한다.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from vlogsrt.project.raw import ingest
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import SourceNestingError


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주")
    return root


def _fake_inspector(path: Path) -> ingest.SourceInspection:
    """모든 미디어 파일을 영상으로 확인하는 가짜 조사자 — ffprobe를 부르지 않는다."""
    return ingest.SourceInspection(
        duration_seconds=1.0, width=64, height=32, audio_streams=1, capture_time=None
    )


def _rejecting_inspector(path: Path) -> ingest.SourceInspection:
    raise ingest.InspectionError("비디오 스트림이 없습니다")


def _scan(service: raw.RawWorkspaceService, workspace: Path, source: Path, inspector=None):
    return service.scan_sources(
        workspace, source, inspector if inspector is not None else _fake_inspector
    )


def test_scan_confirms_candidates_through_the_inspector(workspace: Path, tmp_path: Path) -> None:
    """후보는 조사자가 영상이라고 확인한 파일만 된다 — 확장자만 믿지 않는다."""
    card = tmp_path / "card"
    (card / "DCIM").mkdir(parents=True)
    (card / "DCIM" / "DJI_0001.MP4").write_bytes(b"video-bytes-1")
    (card / "DCIM" / "DJI_0002.MOV").write_bytes(b"video-bytes-2")
    (card / "DCIM" / "DJI_0003.LRF").write_bytes(b"auxiliary")
    (card / "DCIM" / "note.txt").write_bytes(b"text")

    proposal = _scan(raw.RawWorkspaceService(), workspace, card)
    assert [item.source_name for item in proposal.order] == ["DJI_0001.MP4", "DJI_0002.MOV"]
    assert proposal.scan.total_bytes == len(b"video-bytes-1") + len(b"video-bytes-2")
    assert all(item.inspection.capture_time is None for item in proposal.order)


def test_scan_records_rejected_files_with_visible_reasons(workspace: Path, tmp_path: Path) -> None:
    """조사 실패 파일은 사유와 함께 제외로 남는다 — 조용히 사라지지 않는다."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "broken.mp4").write_bytes(b"not-a-video")
    (card / "fine.mp4").write_bytes(b"ok")

    calls: list[Path] = []

    def selective(path: Path) -> ingest.SourceInspection:
        calls.append(path)
        if path.name == "broken.mp4":
            raise RuntimeError("비디오 스트림이 없습니다")
        return _fake_inspector(path)

    proposal = _scan(raw.RawWorkspaceService(), workspace, card, selective)
    assert [item.source_name for item in proposal.order] == ["fine.mp4"]
    assert len(proposal.scan.rejected) == 1
    assert proposal.scan.rejected[0].source_relative_path == "broken.mp4"
    assert "비디오 스트림" in proposal.scan.rejected[0].reason
    assert calls == [card / "broken.mp4", card / "fine.mp4"]


def test_scan_prunes_protected_names_before_any_stat(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env·자격 증명 철자·숨김 항목은 조회(stat)조차 일으키지 않는다.

    `.env`·`.env.example` stand-in은 이 저장소의 dotenv 보호가 제공하는
    합성 파일 제작 경로로 만든다 — 실제 보호 철자를 직접 쓰지 않는다.
    """
    from tests._dotenv_guard import stage_canary_standin

    card = tmp_path / "card"
    (card / ".hidden").mkdir(parents=True)
    (card / ".hidden" / "leak.mp4").write_bytes(b"x")
    stage_canary_standin(card)  # .env stand-in — 합성 카나리다
    stage_canary_standin(card, name=".env.example")
    (card / "credentials.json").write_bytes(b"{}")
    (card / "id_rsa").write_bytes(b"key")
    (card / "normal.mp4").write_bytes(b"ok")

    stat_paths: list[str] = []
    real_lstat = os.lstat

    def watching_lstat(path):
        stat_paths.append(str(path))
        return real_lstat(path)

    monkeypatch.setattr(os, "lstat", watching_lstat)

    proposal = _scan(raw.RawWorkspaceService(), workspace, card)

    assert [item.source_name for item in proposal.order] == ["normal.mp4"]
    pruned_marks = (".env", "credentials.json", "id_rsa", ".hidden")
    for path in stat_paths:
        assert not any(mark in path for mark in pruned_marks), f"보호 이름을 stat했다: {path}"


def test_scan_does_not_descend_into_symlink_directories(workspace: Path, tmp_path: Path) -> None:
    """심볼릭 링크 디렉터리는 내려가지 않고, 링크 파일은 따라가지 않는다(§5.2)."""
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "escape.mp4").write_bytes(b"escape")
    card = tmp_path / "card"
    (card / "DCIM").mkdir(parents=True)
    (card / "DCIM" / "linked-dir").symlink_to(outside / "deep")
    (card / "DCIM" / "linked-file.mp4").symlink_to(outside / "deep" / "escape.mp4")
    (card / "DCIM" / "real.mp4").write_bytes(b"real")

    proposal = _scan(raw.RawWorkspaceService(), workspace, card)
    assert [item.source_name for item in proposal.order] == ["real.mp4"]
    reasons = {item.source_relative_path: item.reason for item in proposal.scan.rejected}
    assert "DCIM/linked-file.mp4" in reasons
    assert "심볼릭 링크" in reasons["DCIM/linked-file.mp4"]
    assert all(item.source_relative_path != "escape.mp4" for item in proposal.scan.rejected)


def test_scan_rejects_nesting_in_both_directions(workspace: Path, tmp_path: Path) -> None:
    """작업공간=원본, 원본⊂작업공간, 작업공간⊂원본 — 셋 다 재귀 복사가 된다."""
    inside = workspace / "originals" / "card"
    inside.mkdir(parents=True)
    (inside / "a.mp4").write_bytes(b"a")
    service = raw.RawWorkspaceService()

    with pytest.raises(SourceNestingError):
        service.scan_sources(workspace, workspace, _fake_inspector)  # 같은 곳
    with pytest.raises(SourceNestingError):
        service.scan_sources(workspace, inside, _fake_inspector)  # 원본이 작업공간 안
    with pytest.raises(SourceNestingError):
        service.scan_sources(workspace, tmp_path, _fake_inspector)  # 작업공간이 원본 안


def test_scan_hides_nesting_behind_symlinks(workspace: Path, tmp_path: Path) -> None:
    """링크로 위장한 포함 관계도 resolve 뒤 판정에 걸린다."""
    alias = tmp_path / "card-alias"
    alias.symlink_to(workspace)
    with pytest.raises(SourceNestingError):
        raw.RawWorkspaceService().scan_sources(workspace, alias, _fake_inspector)


def test_scan_never_uses_mtime_as_capture_time(workspace: Path, tmp_path: Path) -> None:
    """mtime을 촬영 시각으로 대체하지 않는다 — 조사값은 조사자만 채운다."""
    card = tmp_path / "card"
    card.mkdir()
    media = card / "clip.mp4"
    media.write_bytes(b"v")
    os.utime(media, ns=(113_000_000_000_000_000, 113_000_000_000_000_000))

    proposal = _scan(raw.RawWorkspaceService(), workspace, card)
    assert proposal.order[0].inspection.capture_time is None
    assert proposal.order[0].mtime_ns == 113_000_000_000_000_000  # 스냅숏일 뿐이다
    from vlogsrt.project.raw import source_order

    assert source_order.capture_basis_note(None) == source_order.CAPTURE_BASIS_UNKNOWN


def test_space_estimate_flags_shortage_before_copy(workspace: Path) -> None:
    """공간이 부족하면 견적이 그렇게 말한다 — 조각 공간은 별도 안내로 남긴다."""
    usage = shutil.disk_usage(workspace)
    original = shutil.disk_usage

    def fake_disk_usage(path: Path) -> object:
        return usage._replace(free=3)  # 8바이트 복사에도 못 미치는 남은 공간

    shutil.disk_usage = fake_disk_usage  # type: ignore[assignment]
    try:
        estimate = ingest.space_estimate(workspace, 8)
    finally:
        shutil.disk_usage = original  # type: ignore[assignment]

    assert estimate.sufficient is False
    assert estimate.required_bytes > 8  # 여유 배수가 필요량에 붙는다
    assert "A3" in estimate.note


def test_fingerprint_binds_identity_snapshot_order_and_settings() -> None:
    """지문은 출처 신원(크기·mtime·inode)·순서·설정까지 묶는다 — 크기만이 아니다."""
    stats = [("a.mp4", 10, 111, 1), ("b.mp4", 20, 222, 2)]
    base = ingest.ingest_fingerprint("/card", stats, "order_x")
    assert base == ingest.ingest_fingerprint("/card", stats, "order_x")
    assert base != ingest.ingest_fingerprint("/card", stats, "order_y")  # 순서 리비전
    assert base != ingest.ingest_fingerprint("/card2", stats, "order_x")  # 원본 루트
    assert base != ingest.ingest_fingerprint(
        "/card", [("a.mp4", 10, 999, 1), ("b.mp4", 20, 222, 2)], "order_x"
    )
    assert base != ingest.ingest_fingerprint("/card", list(reversed(stats)), "order_x")  # 순서
    assert base != ingest.ingest_fingerprint(
        "/card", [("a.mp4", 99, 111, 1), ("b.mp4", 20, 222, 2)], "order_x"
    )
