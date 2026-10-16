"""D62 A2 — 승인 견적의 원본 재검증은 stat 믿기, 유료 작업 직전은 언제나 완전 해시.

앱 시작·자막·용어집 편집마다 라이브러리 견적(`selection_quote`)이 원본 전체를 다시 해시했다 —
원본 18개면 20–35분의 디스크 읽기다. 견적은 stat이 지난 완전 검증과 같으면 기록을 믿는다.
1단계 승인 만들기·실행 문·분석 본체는 예전대로 완전 해시하므로, stat을 그대로 둔 채 내용만
바뀐 원본도 유료 요청 앞에서 예전 문장 그대로 닫힌다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt.project.raw import analysis_workflow, selection_analysis, source_verify
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.models import SegmentationError

CONTENT = b"original-bytes" * 1000

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


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


@pytest.fixture()
def digests(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """원본 재검증 호출의 `trust_stat`을 센다 — 견적이 믿기를 켰는지, 유료 문이 껐는지."""
    seen: list[bool] = []
    real = source_verify.source_digest

    def spy(workspace: Any, asset: Any, path: Path, *, trust_stat: bool) -> str:
        seen.append(trust_stat)
        return real(workspace, asset, path, trust_stat=trust_stat)

    monkeypatch.setattr(source_verify, "source_digest", spy)
    return seen


def _copied(tmp_path: Path) -> tuple[Any, Any, Path]:
    """복사본 원본 하나를 가진 가짜 작업공간 — 메모만 믿는다(행의 stat은 카드 쪽 것)."""
    path = tmp_path / "originals" / "a.mp4"
    path.parent.mkdir(parents=True)
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
    )
    workspace = SimpleNamespace(
        root=tmp_path / "ws",
        source_asset=lambda _id: asset,
        source_media_path=lambda _asset: path,
        source_reference_path=lambda _id: None,
    )
    return workspace, asset, path


def _forge(path: Path) -> None:
    """크기·mtime·inode를 그대로 둔 채 가운데 몇 바이트만 바꾼다(mtime을 되돌리는 쓰기)."""
    before = path.stat()
    with path.open("r+b") as handle:
        handle.seek(before.st_size // 2)
        chunk = handle.read(8)
        handle.seek(before.st_size // 2)
        handle.write(bytes(b ^ 0xFF for b in chunk))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ino,
    )


# ── `_verified_asset` — 기본은 완전 해시, 견적만 믿기 ─────────────────────────────────


def test_the_strict_check_always_hashes_and_seeds_the_quote(
    tmp_path: Path, hashes: list[Path]
) -> None:
    workspace, asset, path = _copied(tmp_path)
    for _ in range(3):
        assert raw_service._verified_asset(workspace, "asset_1") is asset
    assert len(hashes) == 3  # 계획·추출·분석 실행의 밑천은 언제나 완전 해시
    for _ in range(3):
        assert raw_service._verified_asset(workspace, "asset_1", trust_stat=True) is asset
    assert len(hashes) == 3  # 완전 검증이 남긴 메모 — stat이 같으면 견적은 해시하지 않는다


@pytest.mark.parametrize("change", ["mtime", "inode", "size", "content"])
def test_any_stat_change_makes_the_quote_hash_and_refuse_as_before(
    tmp_path: Path, hashes: list[Path], change: str
) -> None:
    workspace, asset, path = _copied(tmp_path)
    raw_service._verified_asset(workspace, "asset_1", trust_stat=True)
    assert len(hashes) == 1  # 메모가 없던 첫 번째 — 완전 해시하고 남긴다
    stat = path.stat()
    if change == "mtime":
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif change == "inode":
        twin = path.with_name("twin.mp4")
        twin.write_bytes(CONTENT)
        os.link(path, path.with_name("keep.mp4"))  # 옛 inode를 살려 새 파일이 받지 않게 한다
        os.replace(twin, path)
    elif change == "size":
        path.write_bytes(CONTENT + b"!")
    else:
        path.write_bytes(b"X" * len(CONTENT))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    if change == "size":
        with pytest.raises(SegmentationError, match="원본 크기가 가져오기 때와 다르다"):
            raw_service._verified_asset(workspace, "asset_1", trust_stat=True)
        assert len(hashes) == 1  # 크기는 해시 전에 닫는다(예전 그대로)
        return
    if change == "content":
        with pytest.raises(SegmentationError, match="원본 내용이 가져오기 때와 다르다: DCIM/a.mp4"):
            raw_service._verified_asset(workspace, "asset_1", trust_stat=True)
    else:
        assert raw_service._verified_asset(workspace, "asset_1", trust_stat=True) is asset
    assert len(hashes) == 2


def test_a_forged_stat_passes_the_quote_but_not_the_strict_check(
    tmp_path: Path, hashes: list[Path]
) -> None:
    """stat을 위조한 내용 변경 — 견적은 못 본다(D62의 대가), 유료 문의 완전 해시가 닫는다."""
    workspace, asset, path = _copied(tmp_path)
    raw_service._verified_asset(workspace, "asset_1")
    _forge(path)
    assert raw_service._verified_asset(workspace, "asset_1", trust_stat=True) is asset
    with pytest.raises(SegmentationError, match="원본 내용이 가져오기 때와 다르다"):
        raw_service._verified_asset(workspace, "asset_1")
    with pytest.raises(Exception, match="원본 내용이 가져오기 때와 다르다"):
        analysis_workflow._require_source_unchanged(workspace, path, asset, trust_stat=False)


# ── 배선 — 견적만 믿기를 켜고, 선택의 기본은 완전 해시다 ───────────────────────────────


def test_only_the_quote_asks_the_estimate_for_stat_trust() -> None:
    calls: list[dict[str, Any]] = []

    def estimate(root: Path, asset_ids: Any, settings: Any = None, **kwargs: Any) -> tuple:
        calls.append(kwargs)
        raise SegmentationError("멈춤")

    fake = SimpleNamespace(analysis_estimate=estimate)
    with pytest.raises(SegmentationError, match="멈춤"):
        selection_analysis.quote(fake, Path("/ws"), ("a1",))
    assert calls == [{"trust_stat": True}]


def test_the_selection_defaults_to_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digests: list[bool], hashes: list[Path]
) -> None:
    workspace, asset, _path = _copied(tmp_path)
    service = raw_service.RawWorkspaceService()
    monkeypatch.setattr(service, "_workspace", lambda _root: workspace)
    order = SimpleNamespace(entries=(SimpleNamespace(asset_id="asset_1"),))
    assert service._analysis_selection(tmp_path, order, None) == {"asset_1": asset}
    assert service._analysis_selection(tmp_path, order, ["asset_1"], trust_stat=True) == {
        "asset_1": asset
    }
    assert digests == [False, True]
    assert len(hashes) == 1  # 완전 검증 하나가 메모를 남겼고, 믿기는 그것을 썼다


# ── 작업대 — 라이브러리 견적은 0회, 승인 뒤 유료 문은 완전 해시 ─────────────────────────


def _library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    pytest.importorskip(
        "gradio",
        reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
        exc_type=ImportError,
    )
    from tests.raw.test_background_library import setup_library

    return setup_library(tmp_path, monkeypatch)


@needs_ffmpeg
def test_the_library_quote_and_a_glossary_requote_hash_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hashes: list[Path]
) -> None:
    ui, sender, client, root, ids = _library(tmp_path, monkeypatch)
    client.resume.set()
    measured = ui._library_quotes[root]
    verified: list[bool] = []
    real = raw_service._verified_asset

    def spy(workspace: Any, asset_id: str, *, trust_stat: bool = False) -> Any:
        verified.append(trust_stat)
        return real(workspace, asset_id, trust_stat=trust_stat)

    monkeypatch.setattr(raw_service, "_verified_asset", spy)
    hashes.clear()
    every = tuple(measured.asset_ids)
    assert ui.raw.selection_quote(root, every).digest == measured.digest  # 같은 결속·같은 숫자
    assert hashes == [] and verified == [True] * len(every)
    ui.raw.save_glossary(root, "협재\n애월")  # 용어집 편집 — 다시 묶는 견적도 해시하지 않는다
    ui.raw.selection_quote(root, every)
    assert hashes == [] and verified == [True] * (2 * len(every))
    assert not sender.calls and not client.calls


@needs_ffmpeg
def test_after_approval_the_paid_path_still_hashes_every_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hashes: list[Path]
) -> None:
    from tests.raw.test_relisten import approve, stage
    from tests.raw.test_selection_analysis import wait_until

    ui, sender, client, root, ids = _library(tmp_path, monkeypatch)
    client.resume.set()
    strict: list[str] = []
    real = analysis_workflow._require_source_unchanged

    def require(workspace: Any, path: Path, asset: Any, *, trust_stat: bool) -> None:
        if not trust_stat:
            strict.append(asset.asset_id)
        real(workspace, path, asset, trust_stat=trust_stat)

    monkeypatch.setattr(analysis_workflow, "_require_source_unchanged", require)
    hashes.clear()
    approve(ui, raw_service.SELECTION_MODE_TRANSCRIBE)
    wait_until(
        lambda: all(stage(ui, root, i) == raw_service.TRANSCRIBED_STAGE for i in ids.values())
    )
    assert sorted(strict) == sorted(ids.values())  # 분석 본체가 원본마다 완전 해시했다
    workspace = ui.raw._workspace(root)
    paths = {workspace.source_media_path(a) for a in ui.raw.list_source_assets(root)}
    assert paths <= set(hashes) and len(hashes) >= 3 * len(paths)  # 승인 만들기·실행 문·본체
    assert len(sender.calls) == 2


@needs_ffmpeg
def test_a_forged_stat_is_refused_at_the_paid_step_with_zero_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hashes: list[Path]
) -> None:
    from tests.raw.test_relisten import approve, stage
    from tests.raw.test_selection_analysis import wait_until

    ui, sender, client, root, ids = _library(tmp_path, monkeypatch)
    client.resume.set()
    measured = ui._library_quotes[root]
    a, b = ids["a.mov"], ids["b.mov"]
    workspace = ui.raw._workspace(root)
    _forge(workspace.source_media_path(workspace.source_asset(a)))
    hashes.clear()
    assert ui.raw.selection_quote(root, tuple(measured.asset_ids)).digest == measured.digest
    assert hashes == []  # 견적은 위조를 못 본다 — 기록된 해시를 묶으니 숫자도 그대로다
    approve(ui, raw_service.SELECTION_MODE_TRANSCRIBE)
    wait_until(lambda: stage(ui, root, b) == raw_service.TRANSCRIBED_STAGE)
    wait_until(lambda: stage(ui, root, a) == "분석 확인 필요")
    error = str(ui._task_for(root, (a,)).get("error"))
    assert "원본 내용이 가져오기 때와 다르다" in error and "a.mov" in error
    assert len(sender.calls) == 1  # b만 보냈다 — a는 유료 요청 앞에서 0송신
