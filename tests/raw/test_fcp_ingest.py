"""FCP 라이브러리(.fcpbundle) 가져오기 — 합성 라이브러리로 세우는 계약.

합성 라이브러리만 쓴다(실제 Final Cut·실미디어 아님). 배치 모양은 **실제 규격**을
따른다 — 이벤트 폴더는 임의 이름(예: 날짜)이고 그 안에 `CurrentVersion.fcpevent`
표지가 있으며, 미디어는 그 아래 `Original Media/`·`Transcoded Media/Proxy Media/`
에 산다(접미어 규칙 같은 건 없다). 이 묶음이 보는 것:

- 관리 작업공간은 라이브러리 **밖** sibling이고 같은 라이브러리는 같은 자리를 얻는다.
- 원본·프록시를 모두 조사하되, 같은 (이벤트, 이름줄기)는 **한 논리 클립**이다 —
  시간순 제안에는 선택 역할 하나만 들어가고 다른 역할 파일도 기록에 남는다.
- 외부 링크 원본(파일 링크·통째 폴더 링크)은 정당한 원본으로 따라가고, 끊어진
  링크는 사유와 함께 명시적으로 남는다.
- 복사·검증은 일반 SD/폴더와 같은 A2 파이프라인이다 — 원본(라이브러리·외부 대상
  모두)은 한 번도 다시 쓰이지 않고, 해시 재독출로 같은 내용임이 증명된다.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from vlogsrt.project.raw import fcp
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.models import JobRecord

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _fake_inspect(path: Path) -> fcp.SourceInspection:
    return fcp.SourceInspection(
        duration_seconds=2.0, width=320, height=180, audio_streams=1, capture_time=None
    )


class _RecordingInspector:
    """조사자 호출 기록 — 보호 대상을 조사했는지(하면 안 되는 짓) 증거로 남긴다."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, path: Path) -> fcp.SourceInspection:
        self.calls.append(str(path))
        return fcp.SourceInspection(
            duration_seconds=1.0, width=32, height=32, audio_streams=0, capture_time=None
        )


def _make_library(root: Path) -> tuple[Path, Path, Path]:
    """합성 라이브러리(실제 규격) — 원본 2(하나는 프록시 짝)·외부 링크·끊어진 링크."""
    library = root / "제주.fcpbundle"
    media_root = library / "2026-10-07" / "CurrentVersion.fcpevent"
    original = media_root / "Original Media"
    proxy = media_root / "Transcoded Media" / "Proxy Media"
    original.mkdir(parents=True)
    proxy.mkdir(parents=True)
    (original / "A001.MP4").write_bytes(b"original-a001")
    (original / "A002.mov").write_bytes(b"original-a002")
    (proxy / "A001.mov").write_bytes(b"proxy-a001")
    # 최적화(고화질) 미디어 — 프록시가 아니므로 후보가 되면 안 된다.
    (media_root / "Transcoded Media" / "High Quality").mkdir()
    (media_root / "Transcoded Media" / "High Quality" / "A001.mov").write_bytes(b"optimized")
    external = root / "외장" / "ext-clip.mov"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"external-original")
    (original / "ext-clip.mov").symlink_to(external)
    (original / "gone.mov").symlink_to(root / "없는-파일.mov")
    return library, original, external


def _wait_terminal(service: raw_service.RawWorkspaceService, root: Path, job_id: str) -> JobRecord:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.01)
    return service.get_job(root, job_id)


def _snapshot(tree: Path) -> dict[str, tuple[int, int, bytes]]:
    """읽기전용 보존 증거용 스냅숏 — (mtime_ns, size, bytes). 링크 자체는 담지 않는다."""
    captured: dict[str, tuple[int, int, bytes]] = {}
    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            stat = os.lstat(path)
            captured[str(path)] = (stat.st_mtime_ns, stat.st_size, path.read_bytes())
    return captured


# ── 관리 작업공간 — 라이브러리 밖 sibling, 안정 경로 ────────────────────────────


def test_managed_workspace_root_is_stable_and_outside_the_library(tmp_path: Path) -> None:
    """같은 라이브러리는 언제나 같은 sibling 자리 — 라이브러리 안이 아니다."""
    library, _original, _external = _make_library(tmp_path)
    first = fcp.managed_workspace_root(library)
    second = fcp.managed_workspace_root(library)
    assert first == second == library.parent / "제주-vlogsrt"
    assert not first.is_relative_to(library)
    assert not library.is_relative_to(first)


def test_managed_fcp_workspace_creates_then_reopens(tmp_path: Path) -> None:
    """첫 선택은 만들고, 다시 선택하면 같은 작업공간을 연다(두 번째가 생기지 않는다)."""
    service = raw_service.RawWorkspaceService()
    library, _original, _external = _make_library(tmp_path)
    status, created = service.managed_fcp_workspace(library)
    root = Path(status.workspace_root)
    assert created is True
    assert root == tmp_path / "제주-vlogsrt"
    assert status.name == "제주"

    again, created_again = service.managed_fcp_workspace(library)
    assert created_again is False
    assert Path(again.workspace_root) == root
    assert again.workspace_id == status.workspace_id  # 같은 작업공간이다


def test_managed_fcp_workspace_refuses_junk_at_the_stable_spot(tmp_path: Path) -> None:
    """안정 자리에 작업공간 아닌 것이 있으면 명시적으로 실패한다 — 덮지 않는다."""
    service = raw_service.RawWorkspaceService()
    library, _original, _external = _make_library(tmp_path)
    junk = tmp_path / "제주-vlogsrt"
    junk.mkdir()
    (junk / "메모.txt").write_bytes(b"not a workspace")
    with pytest.raises(raw_service.FcpLibraryError, match="다른 내용이 있다"):
        service.managed_fcp_workspace(library)
    assert (junk / "메모.txt").is_file()  # 덮지 않았다


# ── 조사 — 실제 이벤트 규격·논리 클립·외부 링크·끊어진 링크 ───────────────────


def test_scan_finds_events_by_marker_with_arbitrary_names(tmp_path: Path) -> None:
    """이벤트는 임의 이름 + CurrentVersion.fcpevent 표지로 찾는다 — 접미어 규칙은 없다."""
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "MyLib.fcpbundle"
    marker_dir = library / "2026-10-07 fcp" / "CurrentVersion.fcpevent"
    media = marker_dir / "Original Media"
    media.mkdir(parents=True)
    (media / "clip.mov").write_bytes(b"clip")
    # 표지 없는 폴더는 이벤트가 아니다(렌더 폴더 등).
    (library / "Render Files").mkdir(parents=True)
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    assert scan.events == ("2026-10-07 fcp",)
    assert len(scan.proposal.order) == 1


def test_scan_groups_original_and_proxy_into_logical_clips(tmp_path: Path) -> None:
    """원본·프록시가 같은 클립이면 제안에는 선택 역할만 들어간다 — 중복 클립이 없다."""
    service = raw_service.RawWorkspaceService()
    library, _original, _external = _make_library(tmp_path)
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    assert scan.events == ("2026-10-07",)
    stems = {clip.stem: clip for clip in scan.clips}
    assert stems["A001"].original is not None and stems["A001"].proxy is not None
    assert stems["A002"].proxy is None
    ordered = scan.proposal.order
    assert len(ordered) == 3  # A001 원본·A002 원본·ext-clip 원본 — 프록시는 빠진다
    for candidate in ordered:
        assert "Proxy Media" not in candidate.source_relative_path
        assert "High Quality" not in candidate.source_relative_path  # 최적화도 빠진다


def test_scan_reports_dangling_links_explicitly(tmp_path: Path) -> None:
    """끊어진 링크는 사유와 자리가 남는다 — 조용히 사라지지 않는다."""
    service = raw_service.RawWorkspaceService()
    library, _original, _external = _make_library(tmp_path)
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    assert len(scan.broken) == 1
    broken = scan.broken[0]
    assert broken.linked_from == "2026-10-07/CurrentVersion.fcpevent/Original Media/gone.mov"
    assert "대상이 없다" in broken.reason
    assert all(
        "gone.mov" not in candidate.source_relative_path for candidate in scan.proposal.order
    )


def test_scan_follows_external_linked_originals(tmp_path: Path) -> None:
    """외부 링크 원본은 정당하다 — 후보는 링크가 아니라 푼 대상을 가리킨다."""
    service = raw_service.RawWorkspaceService()
    library, _original, external = _make_library(tmp_path)
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    ext_file = next(file for file, _note in scan.selected if file.external)
    assert ext_file.media_path == str(external)
    candidate = next(c for c in scan.proposal.order if c.source_relative_path == "ext-clip.mov")
    assert candidate.source_root == str(external.parent)  # 복사는 링크 없는 실제 파일에서


def test_scan_follows_relocated_media_folder(tmp_path: Path) -> None:
    """통째 폴더 링크(재배치 미디어)도 외부 원본으로 따라간다."""
    relocated = tmp_path / "relocated" / "Original Media"
    relocated.mkdir(parents=True)
    (relocated / "R001.mov").write_bytes(b"relocated-original")
    library = tmp_path / "이사.fcpbundle"
    event = library / "2026-01-01" / "CurrentVersion.fcpevent"
    event.mkdir(parents=True)
    (event / "Original Media").symlink_to(relocated)
    service = raw_service.RawWorkspaceService()
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    assert len(scan.proposal.order) == 1
    file, _note = scan.selected[0]
    assert file.external is True
    assert Path(file.media_path) == relocated / "R001.mov"


def _forbid_paths(monkeypatch: pytest.MonkeyPatch, forbidden: tuple[str, ...]) -> None:
    """보호 경로를 stat·lstat·scandir로 건드리면 즉시 실패하는 감시 — 표기만 읽어야 한다."""
    real_stat = os.stat
    real_lstat = os.lstat
    real_scandir = os.scandir

    def _clean(path: object) -> None:
        text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
        assert not any(mark in str(text) for mark in forbidden), f"보호 경로를 건드렸다: {text}"

    def guarded_stat(path: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        _clean(path)
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_lstat(path: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        _clean(path)
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_scandir(path: object = "."):  # type: ignore[no-untyped-def]
        _clean(path)
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", guarded_stat)
    monkeypatch.setattr(os, "lstat", guarded_lstat)
    monkeypatch.setattr(os, "scandir", guarded_scandir)


def test_hidden_link_target_is_rejected_before_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """숨김 경로의 링크 대상은 어휘 판정으로 거절된다 — stat도 조사도 하지 않는다.

    보호 경로(`.hidden-media/`, 보호 철자 `credentials`)는 `os.readlink` 표기만
    읽어 가장 먼저 거절한다 — 대상 stat·조사가 전혀 일어나지 않는 것을 감시로
    증명한다. 사유는 명시적으로 남는다(조용한 제외가 아니다).
    """
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "숨김.fcpbundle"
    original = library / "2026-03-03" / "CurrentVersion.fcpevent" / "Original Media"
    original.mkdir(parents=True)
    hidden = tmp_path / ".hidden-media" / "clip.mov"
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"hidden")
    credentials = tmp_path / "credentials" / "key.mov"
    credentials.parent.mkdir(parents=True)
    credentials.write_bytes(b"protected-spelling")
    (original / "hidden.mov").symlink_to(hidden)
    (original / "cred.mov").symlink_to(credentials)
    (original / "fine.mov").write_bytes(b"fine")
    status = service.create_workspace(tmp_path / "ws", "ws")
    recorder = _RecordingInspector()
    _forbid_paths(monkeypatch, (str(hidden), str(credentials)))
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=recorder)
    reasons = {item.linked_from.rsplit("/", 1)[-1]: item.reason for item in scan.broken}
    assert "숨김·보호" in reasons["hidden.mov"]  # 숨김 칸 — 명시적 사유
    assert "숨김·보호" in reasons["cred.mov"]  # 보호 철자(credentials) — 같은 판정
    names = {candidate.source_name for candidate in scan.proposal.order}
    assert names == {"fine.mov"}  # 정당한 내부 파일만 후보다
    assert str(hidden) not in recorder.calls and str(credentials) not in recorder.calls
    assert recorder.calls  # 정당한 파일은 조사했다 — 막힌 것만 안 보았다


def test_hidden_relocated_folder_link_is_rejected_before_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """숨김 경로로 통째 링크된 미디어 폴더도 걷기 전에 거절된다 — 안의 무엇도 보지 않는다.

    폴더 링크 대상의 숨김 판정도 어휘(`os.readlink`)가 먼저다 — 대상 stat·lstat·
    scandir(존재 확인·걷기)이 전혀 일어나지 않는 것을 감시로 증명한다.
    """
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "숨폴더.fcpbundle"
    event = library / "2026-04-04" / "CurrentVersion.fcpevent"
    event.mkdir(parents=True)
    hidden_dir = tmp_path / ".vault" / "Original Media"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "V001.mov").write_bytes(b"vault")
    (event / "Original Media").symlink_to(hidden_dir)
    status = service.create_workspace(tmp_path / "ws", "ws")
    recorder = _RecordingInspector()
    _forbid_paths(monkeypatch, (str(hidden_dir),))
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=recorder)
    assert len(scan.broken) == 1
    assert "숨김·보호" in scan.broken[0].reason
    assert scan.proposal.order == ()  # 후보가 없다 — 조용한 빈 성공이 아니라 사유가 남는다
    assert recorder.calls == []  # 숨김 폴더 안의 무엇도 조사하지 않았다


def test_link_chain_into_hidden_target_never_touches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """공개 링크 사슬(outer→alias→.hidden/blocked)의 숨김 대상도 lstat되지 않는다.

    솔 실측 재현: `Path.resolve`는 사슬을 한 번에 따라가며 숨김 대상을 lstat하고
    본 뒤에야 거절했다. 칸마다 푸는 풀기는 매 칸을 어휘로 검사하므로 보호 대상은
    한 번도 조회되지 않는다 — 감시(stat·lstat·scandir)와 조사자 기록으로 증명한다.
    """
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "사슬.fcpbundle"
    original = library / "2026-05-05" / "CurrentVersion.fcpevent" / "Original Media"
    original.mkdir(parents=True)
    blocked = tmp_path / ".hidden" / "blocked.mov"
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b"blocked")
    alias = tmp_path / "alias.mov"
    alias.symlink_to(blocked)
    (original / "outer.mov").symlink_to(alias)
    (original / "fine.mov").write_bytes(b"fine")
    status = service.create_workspace(tmp_path / "ws", "ws")
    recorder = _RecordingInspector()
    _forbid_paths(monkeypatch, (str(blocked),))
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=recorder)
    broken = {item.linked_from.rsplit("/", 1)[-1]: item.reason for item in scan.broken}
    assert "숨김·보호" in broken["outer.mov"]  # 사슬 끝 숨김 — 명시적 사유
    assert {candidate.source_name for candidate in scan.proposal.order} == {"fine.mov"}
    assert str(blocked) not in recorder.calls
    assert recorder.calls  # 정당한 파일은 조사했다


def test_link_loop_is_cut_explicitly(tmp_path: Path) -> None:
    """링크 고리(A→B→A)는 풀기 상한에서 명시적으로 끊긴다 — 매달리지 않는다."""
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "고리.fcpbundle"
    original = library / "2026-06-06" / "CurrentVersion.fcpevent" / "Original Media"
    original.mkdir(parents=True)
    loop_a = tmp_path / "loop-a.mov"
    loop_b = tmp_path / "loop-b.mov"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    (original / "circular.mov").symlink_to(loop_a)
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    assert len(scan.broken) == 1
    assert "고리" in scan.broken[0].reason  # 명시적 사유 — 조용한 제외가 아니다
    assert scan.proposal.order == ()


def test_role_selection_swaps_the_chosen_role(tmp_path: Path) -> None:
    """역할을 프록시로 바꾸면 프록시가 들어가고, 프록시 없는 클립은 원본으로 대체 표시된다."""
    service = raw_service.RawWorkspaceService()
    library, original, _external = _make_library(tmp_path)
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(
        Path(status.workspace_root), library, inspect=_fake_inspect, role=fcp.FCP_ROLE_PROXY
    )
    a001 = next(file for file, _note in scan.selected if file.stem == "A001")
    assert a001.role == fcp.FCP_ROLE_PROXY  # A001 은 프록시 파일을 가져간다
    a002 = next(file for file, _note in scan.selected if file.stem == "A002")
    assert a002.role == fcp.FCP_ROLE_ORIGINAL  # 프록시 없는 클립은 원본으로
    notes = [note for file, note in scan.selected if note]
    assert notes and all("original만 있다" in note for note in notes)
    assert (original / "A001.MP4").is_file()  # 다른 역할 파일은 그대로 있다


def test_scan_rejects_non_library_and_nested_workspace(tmp_path: Path) -> None:
    """.fcpbundle이 아니면 실패하고, 작업공간이 라이브러리 안이면 재귀 복사를 미리 닫는다."""
    service = raw_service.RawWorkspaceService()
    plain = tmp_path / "그냥-폴더"
    plain.mkdir()
    status = service.create_workspace(tmp_path / "ws", "ws")
    with pytest.raises(raw_service.FcpLibraryError, match="아니다"):
        service.scan_fcp_library(Path(status.workspace_root), plain, inspect=_fake_inspect)

    library, _original, _external = _make_library(tmp_path)
    inside = library / "속-작업공간"
    service.create_workspace(inside, "inside")
    with pytest.raises(raw_service.SourceNestingError, match="안이다"):
        service.scan_fcp_library(inside, library, inspect=_fake_inspect)


def test_same_stem_originals_are_all_preserved(tmp_path: Path) -> None:
    """같은 이벤트·같은 줄기의 원본 여럿(A.mov·A.mp4)은 모두 후보로 남는다.

    루트 직접 재현: 같은 줄기 원본 둘이 조사에서 하나(A.mp4)로 덮어져 사라졌다.
    짝짓기는 원본·프록시가 한 개씩일 때만 성립한다 — 애매하면 파일마다 제각각
    클립으로 남겨 하나도 잃지 않는다. 확정·복사까지 모두 정상 원본으로 흐른다.
    """
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "실제.fcpbundle"
    original = library / "2026-10-07" / "CurrentVersion.fcpevent" / "Original Media"
    original.mkdir(parents=True)
    (original / "A.mov").write_bytes(b"mov")
    (original / "A.mp4").write_bytes(b"mp4")
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    assert len(scan.proposal.order) == 2  # 둘 다 산다 — 하나로 덮어지지 않는다
    assert {candidate.source_name for candidate in scan.proposal.order} == {"A.mov", "A.mp4"}

    confirmation = service.confirm_ingest(Path(status.workspace_root), scan.proposal)
    handle = service.start_ingest_copy(Path(status.workspace_root), confirmation)
    record = _wait_terminal(service, Path(status.workspace_root), handle.job_id)
    assert record.status == "succeeded", record.error
    assets = service.list_source_assets(Path(status.workspace_root))
    assert len(assets) == 2
    by_name = {asset.source_name: asset for asset in assets}
    assert by_name["A.mov"].content_sha256 == hashlib.sha256(b"mov").hexdigest()
    assert by_name["A.mp4"].content_sha256 == hashlib.sha256(b"mp4").hexdigest()
    assert len({asset.relative_path for asset in assets}) == 2  # 목적지도 갈라졌다


def test_ambiguous_pair_keeps_every_file_as_its_own_clip(tmp_path: Path) -> None:
    """짝이 애매하면(원본 둘·프록시 하나) 묶지 않는다 — 파일마다 클립, 아무도 사라지지 않는다."""
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "애매.fcpbundle"
    media = library / "2026-10-07" / "CurrentVersion.fcpevent"
    original = media / "Original Media"
    proxy = media / "Transcoded Media" / "Proxy Media"
    original.mkdir(parents=True)
    proxy.mkdir(parents=True)
    (original / "A.mov").write_bytes(b"original-mov")
    (original / "A.mp4").write_bytes(b"original-mp4")
    (proxy / "A.mov").write_bytes(b"proxy-mov")
    status = service.create_workspace(tmp_path / "ws", "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    stems = [clip for clip in scan.clips if clip.stem == "A"]
    assert len(stems) == 3  # 원본 둘 + 프록시 하나 — 제각각 클립
    assert all(clip.proxy is None or clip.original is None for clip in stems)  # 애매한 짝 없음
    files = sorted(str(Path(file.media_path).name) for file, _note in scan.selected)
    assert files == ["A.mov", "A.mov", "A.mp4"]  # 세 파일 전부 선택에 산다
    for file, note in scan.selected:
        if file.role == fcp.FCP_ROLE_PROXY:
            assert "proxy만 있다" in note  # 프록시뿐인 클립은 대체 표기가 붙는다


def test_external_same_name_links_keep_real_identity_and_both_copy(tmp_path: Path) -> None:
    """외부 폴더 둘의 same.mov — 공통 조상 뿌리 + 실제 상대경로로 둘 다 확정·복사된다.

    루트 직접 재현: 둘째 후보의 상대경로를 꾸며 확정이 `external-b/external-b/
    same.mov`를 찾다 SourceChangedError로 죽었다. 이제 외부 후보는 푼 대상 부모들의
    **공통 조상**(external-a·external-b의 부모)을 출처 뿌리로 삼고 실제 상대경로
    (`external-a/same.mov`·`external-b/same.mov`)를 적는다 — 신원과 목적지 이름이
    자연히 어울리고, 조상을 훑지 않는다. 원본 둘은 정상 검증 원본이고
    라이브러리·외부 폴더는 불가침이다.
    """
    service = raw_service.RawWorkspaceService()
    library = tmp_path / "충돌.fcpbundle"
    original = library / "2026-02-02" / "CurrentVersion.fcpevent" / "Original Media"
    original.mkdir(parents=True)
    one = tmp_path / "external-a" / "same.mov"
    two = tmp_path / "external-b" / "same.mov"
    one.parent.mkdir(parents=True)
    two.parent.mkdir(parents=True)
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    (original / "one.mov").symlink_to(one)
    (original / "two.mov").symlink_to(two)
    root = tmp_path / "ws"
    status = service.create_workspace(root, "ws")
    scan = service.scan_fcp_library(Path(status.workspace_root), library, inspect=_fake_inspect)
    candidates = scan.proposal.order
    assert sorted(candidate.source_relative_path for candidate in candidates) == [
        "external-a/same.mov",
        "external-b/same.mov",
    ]  # 조상 아래 실제 상대경로 — 겹치지도 꾸미지도 않는다
    assert {candidate.source_root for candidate in candidates} == {str(tmp_path)}
    for candidate in candidates:  # 뿌리+상대경로는 실제 파일을 가리킨다(확정 재검사 재료)
        assert (Path(candidate.source_root) / candidate.source_relative_path).is_file()

    library_before = _snapshot(library)
    sources_before = {p: (os.lstat(p).st_mtime_ns, p.read_bytes()) for p in (one, two)}
    confirmation = service.confirm_ingest(Path(status.workspace_root), scan.proposal)
    handle = service.start_ingest_copy(Path(status.workspace_root), confirmation)
    record = _wait_terminal(service, Path(status.workspace_root), handle.job_id)
    assert record.status == "succeeded", record.error
    assets = service.list_source_assets(Path(status.workspace_root))
    assert len(assets) == 2
    by_hash = {asset.content_sha256: asset for asset in assets}
    assert hashlib.sha256(b"one").hexdigest() in by_hash
    assert hashlib.sha256(b"two").hexdigest() in by_hash  # 둘 다 내용이 검증되었다
    assert {asset.relative_path for asset in assets} == {
        f"originals/{assets[0].batch_dir}/external-a/same.mov",
        f"originals/{assets[0].batch_dir}/external-b/same.mov",
    }  # 목적지도 실제 상대경로 그대로
    assert (root / by_hash[hashlib.sha256(b"one").hexdigest()].relative_path).read_bytes() == b"one"
    assert (root / by_hash[hashlib.sha256(b"two").hexdigest()].relative_path).read_bytes() == b"two"
    assert _snapshot(library) == library_before  # 라이브러리 불가침
    assert {p: (os.lstat(p).st_mtime_ns, p.read_bytes()) for p in (one, two)} == sources_before


# ── 복사·검증 — 같은 A2 파이프라인, 원본 불가침 ───────────────────────────────


def test_copy_from_library_preserves_sources_and_verifies_hash(tmp_path: Path) -> None:
    """확정→복사가 라이브러리·외부 대상 모두에서 일어난다 — 해시 재독출·원본 불가침 증명."""
    service = raw_service.RawWorkspaceService()
    library, _original, external = _make_library(tmp_path)
    status, _created = service.managed_fcp_workspace(library)
    root = Path(status.workspace_root)
    scan = service.scan_fcp_library(root, library, inspect=_fake_inspect)
    library_before = _snapshot(library)
    external_before = (
        os.lstat(external).st_mtime_ns,
        os.lstat(external).st_size,
        external.read_bytes(),
    )
    confirmation = service.confirm_ingest(root, scan.proposal)
    handle = service.start_ingest_copy(root, confirmation)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error

    assets = service.list_source_assets(root)
    assert len(assets) == 3
    by_name = {asset.source_name: asset for asset in assets}
    for name, expected_bytes in (
        ("A001.MP4", b"original-a001"),
        ("A002.mov", b"original-a002"),
        ("ext-clip.mov", b"external-original"),
    ):
        asset = by_name[name]
        assert asset.status == "ready", asset.error
        copied = root / asset.relative_path
        assert copied.read_bytes() == expected_bytes  # 목적지 바이트
        assert asset.content_sha256 == hashlib.sha256(expected_bytes).hexdigest()  # 재독출 해시
    # 원본(라이브러리 안·외부 링크 대상)은 한 번도 다시 쓰이지 않았다.
    assert _snapshot(library) == library_before
    external_after = (
        os.lstat(external).st_mtime_ns,
        os.lstat(external).st_size,
        external.read_bytes(),
    )
    assert external_after == external_before
    assert not (library / "originals").exists()  # 라이브러리 안에 아무것도 쓰지 않았다


def test_retry_after_failure_reuses_same_batch(tmp_path: Path) -> None:
    """FCP 원본도 일반 재시도 계약을 탄다 — 같은 지문의 실패 작업에 다시 붙는다."""
    from vlogsrt.project.raw import ingest as ingest_module

    service = raw_service.RawWorkspaceService()
    library, _original, _external = _make_library(tmp_path)
    status, _created = service.managed_fcp_workspace(library)
    root = Path(status.workspace_root)
    scan = service.scan_fcp_library(root, library, inspect=_fake_inspect)
    confirmation = service.confirm_ingest(root, scan.proposal)

    broken = {"active": True}
    original_copy = ingest_module._copy_with_hash

    def failing(source: Path, target: Path) -> str:
        if broken["active"] and source.name == "A002.mov":
            raise OSError("라이브러리 볼륨 분리")
        return original_copy(source, target)

    ingest_module._copy_with_hash = failing  # type: ignore[assignment]
    try:
        handle = service.start_ingest_copy(root, confirmation)
        failed = _wait_terminal(service, root, handle.job_id)
    finally:
        ingest_module._copy_with_hash = original_copy  # type: ignore[assignment]
    assert failed.status == "failed"

    broken["active"] = False
    retried = service.retry_ingest_copy(root, handle.job_id)
    record = _wait_terminal(service, root, retried.job_id)
    assert record.status == "succeeded", record.error
    assert all(asset.status == "ready" for asset in service.list_source_assets(root))
