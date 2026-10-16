"""D62 B7 — 재사용·표시 검증의 stat 믿기: stat이 지난 완전 검증과 같으면 해시하지 않는다.

카드를 누르거나 「다음」으로 다른 영상에 갈 때마다 9GB 원본을 다시 해시해 1–2분씩 멈췄다.
stat(크기·mtime_ns·inode)이 하나라도 다르면 예전과 똑같이 완전 해시하고, 내용이 다르면 예전
문장 그대로 거부한다. 분석 본체는 믿지 않는다(언제나 완전 해시).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt.project.raw import analysis_workflow, selection_analysis, source_verify, visual_review
from vlogsrt.project.raw.models import AnalysisError
from vlogsrt.project.raw.visual_review import VisualReviewError
from vlogsrt.project.raw.workspace import RawWorkspace

CONTENT = b"original-bytes" * 1000


@pytest.fixture()
def hashes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """완전 해시를 엿본다 — 부른 경로를 센다."""
    seen: list[Path] = []
    real = source_verify._full_sha256

    def spy(path: Path) -> str:
        seen.append(path)
        return real(path)

    monkeypatch.setattr(source_verify, "_full_sha256", spy)
    return seen


def _copied(tmp_path: Path) -> tuple[RawWorkspace, Any, Path]:
    """복사 가져오기 원본 — 행의 stat은 카드 쪽 것이라 메모만 믿는다."""
    workspace = RawWorkspace.create(tmp_path / "ws", "w")
    path = workspace.root / "originals" / "a.mp4"
    path.write_bytes(CONTENT)
    asset = SimpleNamespace(
        asset_id="asset_1",
        status="ready",
        content_sha256=hashlib.sha256(CONTENT).hexdigest(),
        size_bytes=len(CONTENT),
        source_root="/Volumes/CARD",
        source_relative_path="DCIM/a.mp4",
        source_name="a.mp4",
        source_mtime_ns=1,
        source_inode=2,
        relative_path="originals/a.mp4",
    )
    return workspace, asset, path


def test_a_stat_match_does_no_hashing(tmp_path: Path, hashes: list[Path]) -> None:
    workspace, asset, path = _copied(tmp_path)
    assert source_verify.source_digest(workspace, asset, path, trust_stat=True) == (
        asset.content_sha256
    )
    assert len(hashes) == 1  # 처음은 완전 해시 — 그리고 메모를 남긴다
    assert (workspace.root / source_verify.MEMO_DIR / "asset_1.json").is_file()
    for _ in range(3):
        assert source_verify.source_digest(workspace, asset, path, trust_stat=True) == (
            asset.content_sha256
        )
    assert len(hashes) == 1  # stat이 같다 — 다시 해시하지 않는다


def test_the_paid_path_always_hashes(tmp_path: Path, hashes: list[Path]) -> None:
    workspace, asset, path = _copied(tmp_path)
    source_verify.source_digest(workspace, asset, path, trust_stat=True)
    source_verify.source_digest(workspace, asset, path, trust_stat=False)
    source_verify.source_digest(workspace, asset, path, trust_stat=False)
    assert len(hashes) == 3


@pytest.mark.parametrize("change", ["mtime", "inode", "size"])
def test_any_stat_change_rehashes(tmp_path: Path, hashes: list[Path], change: str) -> None:
    workspace, asset, path = _copied(tmp_path)
    source_verify.source_digest(workspace, asset, path, trust_stat=True)
    if change == "mtime":
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif change == "inode":
        twin = path.with_name("twin.mp4")
        twin.write_bytes(CONTENT)
        keep = path.with_name("keep.mp4")
        os.link(path, keep)  # 옛 inode를 살려 둬 새 파일이 같은 inode를 받지 않게 한다
        os.replace(twin, path)
    else:
        path.write_bytes(CONTENT + b"!")
    digest = source_verify.source_digest(workspace, asset, path, trust_stat=True)
    assert len(hashes) == 2
    assert (digest == asset.content_sha256) is (change != "size")


def test_a_memo_for_another_path_or_hash_is_not_trusted(tmp_path: Path, hashes: list[Path]) -> None:
    workspace, asset, path = _copied(tmp_path)
    source_verify.source_digest(workspace, asset, path, trust_stat=True)
    other = SimpleNamespace(**{**vars(asset), "content_sha256": "f" * 64})
    assert source_verify.source_digest(workspace, other, path, trust_stat=True) != "f" * 64
    assert len(hashes) == 2


def test_a_changed_file_is_never_remembered(tmp_path: Path, hashes: list[Path]) -> None:
    workspace, asset, path = _copied(tmp_path)
    path.write_bytes(b"X" * len(CONTENT))  # 같은 크기 · 다른 내용
    assert source_verify.source_digest(workspace, asset, path, trust_stat=True) != (
        asset.content_sha256
    )
    assert not (workspace.root / source_verify.MEMO_DIR / "asset_1.json").exists()
    source_verify.source_digest(workspace, asset, path, trust_stat=True)
    assert len(hashes) == 2  # 메모가 없으니 매번 완전 해시 — 매번 거부된다


def test_a_reference_original_is_trusted_from_its_import_row(
    tmp_path: Path, hashes: list[Path]
) -> None:
    """참조 원본 — 가져오기가 해시 뒤 stat을 다시 확인했으니 그 행의 stat이 완전 검증 때 값이다."""
    library = tmp_path / "Library.fcpbundle"
    library.mkdir()
    path = library / "a.mov"
    path.write_bytes(CONTENT)
    stat = path.stat()
    asset = SimpleNamespace(
        asset_id="asset_2",
        status="ready",
        content_sha256=hashlib.sha256(CONTENT).hexdigest(),
        size_bytes=stat.st_size,
        source_root=str(library),
        source_relative_path="a.mov",
        source_mtime_ns=stat.st_mtime_ns,
        source_inode=stat.st_ino,
    )
    workspace = SimpleNamespace(root=tmp_path / "ws", source_reference_path=lambda _id: str(path))
    assert source_verify.source_digest(workspace, asset, path, trust_stat=True) == (
        asset.content_sha256
    )
    assert hashes == []
    stale = SimpleNamespace(**{**vars(asset), "source_mtime_ns": stat.st_mtime_ns - 1})
    source_verify.source_digest(workspace, stale, path, trust_stat=True)
    assert len(hashes) == 1  # 행과 stat이 다르면 완전 해시
    copied = SimpleNamespace(root=tmp_path / "ws2", source_reference_path=lambda _id: None)
    source_verify.source_digest(copied, asset, path, trust_stat=True)
    assert len(hashes) == 2  # 참조가 아니면 행의 stat을 믿지 않는다


# ── 호출 자리 — 거부 문장은 예전 그대로 ────────────────────────────────────────


def test_the_cache_check_trusts_stat_and_still_refuses_changed_content(
    tmp_path: Path, hashes: list[Path]
) -> None:
    workspace, asset, path = _copied(tmp_path)
    for _ in range(3):
        analysis_workflow._require_source_unchanged(workspace, path, asset, trust_stat=True)
    assert len(hashes) == 1
    path.write_bytes(b"X" * len(CONTENT))
    with pytest.raises(AnalysisError, match="원본 내용이 가져오기 때와 다르다: DCIM/a.mp4 — 다시"):
        analysis_workflow._require_source_unchanged(workspace, path, asset, trust_stat=True)
    path.write_bytes(CONTENT + b"!")
    with pytest.raises(AnalysisError, match="원본 크기가 가져오기 때와 다르다"):
        analysis_workflow._require_source_unchanged(workspace, path, asset, trust_stat=True)


def test_the_preview_check_trusts_stat_and_still_refuses_changed_content(
    tmp_path: Path, hashes: list[Path]
) -> None:
    workspace, asset, path = _copied(tmp_path)
    for _ in range(3):
        visual_review._verify_source_unchanged(workspace, asset, path)
    assert len(hashes) == 1
    path.write_bytes(b"X" * len(CONTENT))
    with pytest.raises(VisualReviewError, match="원본 내용이 가져오기 때와 다르다"):
        visual_review._verify_source_unchanged(workspace, asset, path)


def test_the_selection_cache_check_asks_for_stat_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 클릭의 캐시 검사(`cached_revision`)가 믿기를 켠 채 부른다 — 분석 본체는 끈 채다."""
    calls: list[bool] = []

    def require(workspace: Any, path: Any, asset: Any, *, trust_stat: bool) -> None:
        calls.append(trust_stat)
        raise AnalysisError("멈춤")

    monkeypatch.setattr(analysis_workflow, "_require_source_unchanged", require)
    asset = SimpleNamespace(asset_id="a1")
    workspace = SimpleNamespace(source_media_path=lambda _asset: Path("/x"))
    fake = SimpleNamespace(
        _workspace=lambda root: workspace,
        latest_global_source_order=lambda root: object(),
        list_source_assets=lambda root: (asset,),
    )
    assert selection_analysis.cached_revision(fake, Path("/ws"), ("a1",)) is None
    assert calls == [True]
    source = Path(analysis_workflow.__file__).read_text(encoding="utf-8")
    body = source[source.index("def _analyze_source(") :]
    assert "_require_source_unchanged(workspace, path, asset, trust_stat=False)" in body[:1500]
