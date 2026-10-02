"""A2 복사·검증 — 파일 단위 재개·중복 출처·공간·안전 거부(계획 §5.2·§5.3).

공개 서비스(RawWorkspaceService)를 통해서만 복사한다 — 시험도 실사용과 같은
길을 지난다. 조사자는 가짜로 갈아끼우고(합성 바이트가 곧 영상이다), 실패 주입은
복사 내부 함수만 포장해 넣는다. 완료의 정의는 §5.3 그대로다:

    발견 → 임시 이름 복사 → 목적지 재독출 SHA-256 → 원본 전후 stat 안정 → READY
"""

from __future__ import annotations

import hashlib
import json as json_module
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from vlogsrt.project.raw import ingest
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import (
    JobRecord,
    NoIngestCandidatesError,
    SourceChangedError,
    WorkspaceVersionError,
)
from vlogsrt.project.raw.workspace import DB_NAME

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _fake_inspector(path: Path) -> ingest.SourceInspection:
    return ingest.SourceInspection(
        duration_seconds=1.0, width=64, height=32, audio_streams=1, capture_time=None
    )


def _card(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    card = tmp_path / name
    for relative, payload in files.items():
        target = card / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return card


def _wait_terminal(service: raw.RawWorkspaceService, root: Path, job_id: str) -> JobRecord:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.01)
    return service.get_job(root, job_id)


def _boom(context: object) -> None:
    raise RuntimeError("작업 본문 실패")


def _run_ingest(service: raw.RawWorkspaceService, root: Path, card: Path):
    proposal = service.scan_sources(root, card, _fake_inspector)
    confirmation = service.confirm_ingest(root, proposal)
    handle = service.start_ingest_copy(root, confirmation)
    return confirmation, handle, _wait_terminal(service, root, handle.job_id)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주")
    return root


def test_verified_copy_registers_ready_and_preserves_original(
    workspace: Path, tmp_path: Path
) -> None:
    """완료의 정의 그대로 — 재독출 해시·원본 불변·ready 등록이 한 자리에서 만난다."""
    payload = b"\x00\x00\x00 ftypisom" + bytes(range(256)) * 4
    card = _card(tmp_path, "card", {"DCIM/100MEDIA/DJI_0001.MP4": payload})
    source_stat = (card / "DCIM/100MEDIA/DJI_0001.MP4").stat()
    service = raw.RawWorkspaceService()

    confirmation, handle, record = _run_ingest(service, workspace, card)

    assert record.status == "succeeded", record.error
    assert confirmation.batch_dir == "import_000001"
    assert confirmation.revision.basis == "user_confirmed"
    (asset,) = service.list_source_assets(workspace)
    assert asset.status == "ready"
    assert asset.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert asset.file_id is not None

    destination = workspace / asset.relative_path
    assert destination.read_bytes() == payload
    assert not Path(str(destination) + ".partial").exists()

    registration = service.resolve_media(workspace, asset.file_id)
    assert registration.size_bytes == len(payload)

    after = (card / "DCIM/100MEDIA/DJI_0001.MP4").stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ino,
    )
    assert (card / "DCIM/100MEDIA/DJI_0001.MP4").read_bytes() == payload


def test_same_filename_from_two_cards_never_overwrites(workspace: Path, tmp_path: Path) -> None:
    """다른 카드의 같은 파일명(`영상2.mp4`)은 배치가 갈라 서로를 덮지 않는다(§5.2)."""
    first = _card(tmp_path, "card-a", {"DCIM/영상2.mp4": b"card-a-content"})
    second = _card(tmp_path, "card-b", {"DCIM/영상2.mp4": b"card-b-content"})
    service = raw.RawWorkspaceService()

    confirmation_a, _, record_a = _run_ingest(service, workspace, first)
    confirmation_b, _, record_b = _run_ingest(service, workspace, second)

    assert record_a.status == "succeeded" and record_b.status == "succeeded"
    assert confirmation_a.batch_dir != confirmation_b.batch_dir
    assets = service.list_source_assets(workspace)
    assert len(assets) == 2
    paths = {workspace / asset.relative_path for asset in assets}
    assert all(path.is_file() for path in paths)
    contents = {path.read_bytes() for path in paths}
    assert contents == {b"card-a-content", b"card-b-content"}
    assert len(service.list_media(workspace)) == 2


def test_same_content_keeps_distinct_provenance(workspace: Path, tmp_path: Path) -> None:
    """같은 내용은 중복으로 알아내되, 출처 이력은 따로 남는다(§5.2)."""
    shared = b"identical-bytes"
    first = _card(tmp_path, "card-a", {"DCIM/DJI_0001.MP4": shared})
    second = _card(tmp_path, "card-b", {"DCIM/OTHER_0001.MOV": shared})
    service = raw.RawWorkspaceService()

    _run_ingest(service, workspace, first)
    _run_ingest(service, workspace, second)

    assets = {asset.source_relative_path: asset for asset in service.list_source_assets(workspace)}
    assert assets["DCIM/DJI_0001.MP4"].duplicate_of is None
    assert assets["DCIM/OTHER_0001.MOV"].duplicate_of == assets["DCIM/DJI_0001.MP4"].asset_id
    # 두 파일 모두 그대로 산다 — 중복이라고 지우지 않는다.
    assert (workspace / assets["DCIM/DJI_0001.MP4"].relative_path).is_file()
    assert (workspace / assets["DCIM/OTHER_0001.MOV"].relative_path).is_file()


def test_invalid_partial_never_becomes_ready(workspace: Path, tmp_path: Path) -> None:
    """검증이 깨진 partial은 ready가 되지 못한다 — 등록도, 제공도 없다(§5.3)."""
    card = _card(tmp_path, "card", {"a.mp4": b"must-not-pass"})
    service = raw.RawWorkspaceService()
    proposal = service.scan_sources(workspace, card, _fake_inspector)
    confirmation = service.confirm_ingest(workspace, proposal)

    real_hash = ingest._sha256_file
    digest = hashlib.sha256()

    def lying_reread(path: Path) -> str:
        digest.update(b"tampered")  # 재독출 값이 복사 때 해시와 어긋난다
        return digest.hexdigest()

    ingest._sha256_file = lying_reread  # type: ignore[assignment]
    try:
        handle = service.start_ingest_copy(workspace, confirmation)
        record = _wait_terminal(service, workspace, handle.job_id)
    finally:
        ingest._sha256_file = real_hash  # type: ignore[assignment]

    assert record.status == "failed"
    assert "검증 실패" in (record.error or "")
    (asset,) = service.list_source_assets(workspace)
    assert asset.status == "failed"
    assert asset.file_id is None
    destination = workspace / asset.relative_path
    assert not destination.exists()
    assert not Path(str(destination) + ".partial").exists()
    assert service.list_media(workspace) == ()


def test_low_disk_blocks_before_any_copy(workspace: Path, tmp_path: Path) -> None:
    """공간이 부족하면 복사 시작 전에 차단된다 — partial조차 남지 않는다(§5.2)."""
    card = _card(tmp_path, "card", {"a.mp4": b"12345678", "b.mp4": b"1234"})
    service = raw.RawWorkspaceService()
    proposal = service.scan_sources(workspace, card, _fake_inspector)
    confirmation = service.confirm_ingest(workspace, proposal)

    usage = shutil.disk_usage(workspace)
    original = shutil.disk_usage

    def starving(path: Path) -> object:
        return usage._replace(free=3)

    shutil.disk_usage = starving  # type: ignore[assignment]
    try:
        handle = service.start_ingest_copy(workspace, confirmation)
        record = _wait_terminal(service, workspace, handle.job_id)
    finally:
        shutil.disk_usage = original  # type: ignore[assignment]

    assert record.status == "failed"
    assert "공간이 부족" in (record.error or "")
    assert not any((workspace / "originals").rglob("*.partial"))
    assert not any((workspace / "originals").rglob("*.mp4"))


def test_source_changed_after_scan_fails_confirm_without_writing(
    workspace: Path, tmp_path: Path
) -> None:
    """스캔과 확정 사이 원본이 바뀌면 확정이 실패한다 — 아무 것도 쓰지 않는다(§5.3)."""
    card = _card(tmp_path, "card", {"a.mp4": b"before"})
    service = raw.RawWorkspaceService()
    proposal = service.scan_sources(workspace, card, _fake_inspector)
    (card / "a.mp4").write_bytes(b"after-the-scan")

    with pytest.raises(SourceChangedError):
        service.confirm_ingest(workspace, proposal)
    assert service.list_source_assets(workspace) == ()
    assert not (workspace / "originals" / "import_000001").exists()


def test_empty_scan_cannot_confirm(workspace: Path, tmp_path: Path) -> None:
    """가져올 영상이 없으면 확정·복사가 성공으로 위장하지 않는다."""
    card = _card(tmp_path, "card", {"readme.txt": b"no media"})
    service = raw.RawWorkspaceService()
    proposal = service.scan_sources(workspace, card, _fake_inspector)
    with pytest.raises(NoIngestCandidatesError):
        service.confirm_ingest(workspace, proposal)


def test_resume_skips_ready_files_and_revalidates_destination(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """재시도는 ready를 다시 읽어 확인하고 건너뛴다 — 눈먼 신뢰도 다시 복사도 아니다."""
    card = _card(tmp_path, "card", {"a.mp4": b"first", "b.mp4": b"second"})
    service = raw.RawWorkspaceService()
    proposal = service.scan_sources(workspace, card, _fake_inspector)
    confirmation = service.confirm_ingest(workspace, proposal)

    broken = {"active": True}
    real_copy = ingest._copy_with_hash

    def failing_copy(source: Path, target: Path) -> str:
        if broken["active"] and source.name == "b.mp4":
            raise OSError("카드가 분리되었다")
        return real_copy(source, target)

    monkeypatch.setattr(ingest, "_copy_with_hash", failing_copy)
    handle = service.start_ingest_copy(workspace, confirmation)
    failed_record = _wait_terminal(service, workspace, handle.job_id)
    assert failed_record.status == "failed"

    assets = {asset.source_name: asset for asset in service.list_source_assets(workspace)}
    assert assets["a.mp4"].status == "ready"
    assert assets["b.mp4"].status == "failed"
    assert "카드가 분리되었다" in (assets["b.mp4"].error or "")
    first_ready_at = (workspace / assets["a.mp4"].relative_path).stat().st_mtime_ns

    broken["active"] = False
    reread_paths: list[Path] = []
    real_sha = ingest._sha256_file

    def watching_reread(path: Path) -> str:
        reread_paths.append(path)
        return real_sha(path)

    monkeypatch.setattr(ingest, "_sha256_file", watching_reread)
    retry = service.retry_ingest_copy(workspace, handle.job_id)
    assert retry.job_id == handle.job_id  # 같은 작업 ID로 다시 붙는다
    record = _wait_terminal(service, workspace, retry.job_id)

    assert record.status == "succeeded", record.error
    assets_after = {asset.source_name: asset for asset in service.list_source_assets(workspace)}
    assert all(asset.status == "ready" for asset in assets_after.values())
    # ready였던 a.mp4의 목적지는 다시 읽어 확인했고(맹신 금지), 다시 쓰지 않았다.
    assert (workspace / assets_after["a.mp4"].relative_path).stat().st_mtime_ns == first_ready_at
    assert any(path.name == "a.mp4" for path in reread_paths)


def test_retry_with_changed_source_stays_incomplete_then_recovers(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원본이 확정 때와 다른 채로는 재시도가 끝나지 않는다 — 복구돼도 같은 작업으로 끝낸다."""
    card = _card(tmp_path, "card", {"a.mp4": b"stable"})
    service = raw.RawWorkspaceService()
    confirmation = service.confirm_ingest(
        workspace, service.scan_sources(workspace, card, _fake_inspector)
    )

    broken = {"active": True}
    real_copy = ingest._copy_with_hash

    def failing_copy(source: Path, target: Path) -> str:
        if broken["active"]:
            raise OSError("일시적 끊김")
        return real_copy(source, target)

    monkeypatch.setattr(ingest, "_copy_with_hash", failing_copy)
    handle = service.start_ingest_copy(workspace, confirmation)
    assert _wait_terminal(service, workspace, handle.job_id).status == "failed"

    # 원본이 바뀐 채 재시도 — 지문(스냅숏)과 어긋나 복사가 시작되지 못한다.
    (card / "a.mp4").write_bytes(b"changed-on-the-card")
    retry = service.retry_ingest_copy(workspace, handle.job_id)
    record = _wait_terminal(service, workspace, retry.job_id)
    assert record.status == "failed"
    (asset,) = service.list_source_assets(workspace)
    assert asset.status == "failed"
    assert "확정 때와 다르다" in (asset.error or "")

    # 원본을 스냅숏 그대로 되돌리면 같은 작업이 끝난다.
    broken["active"] = False
    asset_row = service.list_source_assets(workspace)[0]
    (card / "a.mp4").write_bytes(b"stable")
    os.utime(card / "a.mp4", ns=(asset_row.source_mtime_ns, asset_row.source_mtime_ns))
    final_retry = service.retry_ingest_copy(workspace, handle.job_id)
    final = _wait_terminal(service, workspace, final_retry.job_id)
    assert final.status == "succeeded", final.error
    assert service.list_source_assets(workspace)[0].status == "ready"


def test_copy_follows_confirmed_order(workspace: Path, tmp_path: Path) -> None:
    """복사 순서는 확정된 순서를 따른다 — 자연 정렬 제안 그대로(§6.3)."""
    card = _card(
        tmp_path,
        "card",
        {"DJI_0010.MP4": b"ten", "DJI_0002.MP4": b"two", "DJI_0001.MP4": b"one"},
    )
    service = raw.RawWorkspaceService()
    proposal = service.scan_sources(workspace, card, _fake_inspector)
    assert [item.source_name for item in proposal.order] == [
        "DJI_0001.MP4",
        "DJI_0002.MP4",
        "DJI_0010.MP4",
    ]

    copied_order: list[str] = []
    real_copy = ingest._copy_with_hash

    def watching_copy(source: Path, target: Path) -> str:
        copied_order.append(source.name)
        return real_copy(source, target)

    ingest._copy_with_hash = watching_copy  # type: ignore[assignment]
    try:
        confirmation, handle, record = _run_ingest(service, workspace, card)
    finally:
        ingest._copy_with_hash = real_copy  # type: ignore[assignment]

    assert record.status == "succeeded"
    assert copied_order == ["DJI_0001.MP4", "DJI_0002.MP4", "DJI_0010.MP4"]
    assert [entry.ordinal for entry in confirmation.revision.entries] == [1, 2, 3]


def test_order_revisions_are_immutable_across_confirmations(
    workspace: Path, tmp_path: Path
) -> None:
    """두 번째 확정은 새 리비전을 만들 뿐 — 첫 리비전 기록은 그대로다(§6.5)."""
    first = _card(tmp_path, "card-a", {"a.mp4": b"a"})
    second = _card(tmp_path, "card-b", {"b.mp4": b"b"})
    service = raw.RawWorkspaceService()

    confirmation_a, _, _ = _run_ingest(service, workspace, first)
    confirmation_b, _, _ = _run_ingest(service, workspace, second)

    revisions = service.list_order_revisions(workspace)
    assert [revision.revision_id for revision in revisions] == [
        confirmation_a.revision.revision_id,
        confirmation_b.revision.revision_id,
    ]
    with sqlite3.connect(workspace / DB_NAME) as connection:
        row = connection.execute(
            "SELECT entries_json FROM source_order_revisions WHERE revision_id = ?",
            (confirmation_a.revision.revision_id,),
        ).fetchone()
    entries = json_module.loads(row[0])
    assert entries == [
        {
            "asset_id": confirmation_a.assets[0].asset_id,
            "ordinal": 1,
            "basis_note": "capture_time:uninspected",
        }
    ]


def test_v1_workspace_upgrades_transactionally_and_keeps_data(
    workspace: Path, tmp_path: Path
) -> None:
    """받아들인 v1 작업공간은 열리며 설정·미디어·작업·신원이 그대로다(A2 덧붙이기)."""
    service = raw.RawWorkspaceService()
    clip = workspace / "originals" / "hand.MP4"
    clip.write_bytes(b"handmade")
    registration = service.register_media(workspace, clip, "original")
    blocked = service.submit_job(workspace, "probe", "fp-1", _boom)
    _wait_terminal(service, workspace, blocked.job_id)

    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.executescript(
            "DROP TABLE source_assets; DROP TABLE source_order_revisions;"
            "UPDATE meta SET value = 'raw-workspace-db/1' WHERE key = 'db_schema_version';"
        )

    # 다시 여는 주체는 새 서버다(재시작) — 캐시가 아니라 디스크의 v1을 연다.
    restarted = raw.RawWorkspaceService()
    reopened = restarted.open_workspace(workspace)
    assert reopened.db_schema_version == "raw-workspace-db/2"
    assert restarted.resolve_media(workspace, registration.file_id).size_bytes == len(b"handmade")
    assert restarted.get_job(workspace, blocked.job_id).status == "failed"
    # 갱신 뒤에도 가져오기가 그 작업공간에서 그대로 돈다.
    card = _card(tmp_path, "card", {"new.mp4": b"new"})
    confirmation, handle, record = _run_ingest(restarted, workspace, card)
    assert record.status == "succeeded"
    assert confirmation.batch_dir == "import_000001"


def test_unknown_db_version_still_refuses(workspace: Path) -> None:
    """모르는 DB 버전은 여전히 조용히 마이그레이션하지 않는다."""
    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.execute(
            "UPDATE meta SET value = 'raw-workspace-db/99' WHERE key = 'db_schema_version'"
        )
    with pytest.raises(WorkspaceVersionError):
        raw.RawWorkspaceService().open_workspace(workspace)


def test_discarded_registration_window_closes_on_retry(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """개명·등록까지 끝나고 ready 표시 직전에 죽은 창도 재시도가 닫는다."""
    card = _card(tmp_path, "card", {"a.mp4": b"window"})
    service = raw.RawWorkspaceService()
    confirmation = service.confirm_ingest(
        workspace, service.scan_sources(workspace, card, _fake_inspector)
    )

    # 첫 시도: 등록 뒤 ready 표시 직전에 죽는다(창을 인위적으로 연다).
    real_update = type(service._workspace(workspace)).update_source_asset
    crashed = {"active": True}

    def dying_before_ready(self_ws, asset_id, **changes):
        if crashed["active"] and changes.get("status") == "ready":
            crashed["active"] = False
            raise RuntimeError("ready 표시 직전 크래시")
        return real_update(self_ws, asset_id, **changes)

    monkeypatch.setattr(
        type(service._workspace(workspace)), "update_source_asset", dying_before_ready
    )
    handle = service.start_ingest_copy(workspace, confirmation)
    assert _wait_terminal(service, workspace, handle.job_id).status == "failed"
    (asset,) = service.list_source_assets(workspace)
    assert asset.status == "failed"  # ready 직전에 죽었다 — 최종 파일·등록만 남은 창
    assert (workspace / asset.relative_path).is_file()  # 최종 파일은 이미 자리에 있다

    retry = service.retry_ingest_copy(workspace, handle.job_id)
    record = _wait_terminal(service, workspace, retry.job_id)
    assert record.status == "succeeded", record.error
    (settled,) = service.list_source_assets(workspace)
    assert settled.status == "ready"
    assert settled.file_id is not None
    assert len(service.list_media(workspace)) == 1  # 등록 행이 두 벌로 늘지 않았다


def test_retry_progress_recounts_this_run(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 작업 ID 재시도의 진행값은 이번 실행 기준이다 — 지난 진행 위로 쌓지 않는다."""
    card = _card(tmp_path, "card", {"a.mp4": b"first", "b.mp4": b"second"})
    service = raw.RawWorkspaceService()
    confirmation = service.confirm_ingest(
        workspace, service.scan_sources(workspace, card, _fake_inspector)
    )

    broken = {"active": True}
    real_copy = ingest._copy_with_hash

    def failing_copy(source: Path, target: Path) -> str:
        if broken["active"] and source.name == "b.mp4":
            raise OSError("일시적 끊김")
        return real_copy(source, target)

    monkeypatch.setattr(ingest, "_copy_with_hash", failing_copy)
    handle = service.start_ingest_copy(workspace, confirmation)
    first = _wait_terminal(service, workspace, handle.job_id)
    assert first.status == "failed"
    assert (first.progress_current, first.progress_total) == (2, 2)

    broken["active"] = False
    retry = service.retry_ingest_copy(workspace, handle.job_id)
    final = _wait_terminal(service, workspace, retry.job_id)
    assert final.status == "succeeded", final.error
    assert (final.progress_current, final.progress_total) == (2, 2)  # 4가 아니다


def test_tampered_ready_original_is_never_deleted_on_retry(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """한번 검증된 원본이 나중에 변조돼도 재시도가 지우지 않는다 — 사람에게 넘긴다."""
    card = _card(tmp_path, "card", {"a.mp4": b"original-bytes", "b.mp4": b"second"})
    service = raw.RawWorkspaceService()
    confirmation = service.confirm_ingest(
        workspace, service.scan_sources(workspace, card, _fake_inspector)
    )

    broken = {"active": True}
    real_copy = ingest._copy_with_hash

    def failing_copy(source: Path, target: Path) -> str:
        if broken["active"] and source.name == "b.mp4":
            raise OSError("일시적 끊김")
        return real_copy(source, target)

    monkeypatch.setattr(ingest, "_copy_with_hash", failing_copy)
    handle = service.start_ingest_copy(workspace, confirmation)
    assert _wait_terminal(service, workspace, handle.job_id).status == "failed"

    # a.mp4는 검증까지 끝난 뒤 누군가 변조했다 — 재시도가 이걸 지우면 안 된다.
    ready = {asset.source_name: asset for asset in service.list_source_assets(workspace)}["a.mp4"]
    destination = workspace / ready.relative_path
    destination.write_bytes(b"tampered-by-someone")

    broken["active"] = False
    retry = service.retry_ingest_copy(workspace, handle.job_id)
    record = _wait_terminal(service, workspace, retry.job_id)
    assert record.status == "failed"  # 변조된 ready 때문에 — b.mp4는 끝났다
    states = {asset.source_name: asset for asset in service.list_source_assets(workspace)}
    assert states["b.mp4"].status == "ready"
    assert states["a.mp4"].status == "failed"
    assert "재검증에 실패" in (states["a.mp4"].error or "")
    assert destination.read_bytes() == b"tampered-by-someone"  # 그대로 두었다


def test_symlinked_destination_parent_refuses_without_writing(
    workspace: Path, tmp_path: Path
) -> None:
    """목적지 조상이 링크면 그 자리에서 실패한다 — 링크 너머로는 아무 것도 안 쓴다."""
    card = _card(tmp_path, "card", {"DCIM/a.mp4": b"payload"})
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (workspace / "originals").mkdir(exist_ok=True)
    (workspace / "originals" / "import_000001").symlink_to(outside)

    service = raw.RawWorkspaceService()
    confirmation = service.confirm_ingest(
        workspace, service.scan_sources(workspace, card, _fake_inspector)
    )
    handle = service.start_ingest_copy(workspace, confirmation)
    record = _wait_terminal(service, workspace, handle.job_id)

    assert record.status == "failed"
    assert "심볼릭 링크" in (record.error or "")
    (asset,) = service.list_source_assets(workspace)
    assert asset.status == "failed"
    assert list(outside.iterdir()) == []  # 링크 너머에 아무 것도 생기지 않았다


def test_symlinked_source_parent_refuses_copy(workspace: Path, tmp_path: Path) -> None:
    """원본 조상이 확정 뒤 링크로 바뀌어도 복사하지 않는다 — 마지막 파일 lstat만으론 부족하다."""
    real = tmp_path / "real-card" / "DCIM"
    real.mkdir(parents=True)
    (real / "a.mp4").write_bytes(b"payload")
    card = tmp_path / "card" / "DCIM"
    card.mkdir(parents=True)
    (card / "a.mp4").write_bytes(b"payload")

    service = raw.RawWorkspaceService()
    proposal = service.scan_sources(workspace, tmp_path / "card", _fake_inspector)
    confirmation = service.confirm_ingest(workspace, proposal)

    # 확정 뒤 원본 폴더 칸이 링크로 바뀌었다 — 링크 대상은 같은 내용이다.
    (card / "a.mp4").unlink()
    (tmp_path / "card" / "DCIM").rmdir()
    (tmp_path / "card" / "DCIM").symlink_to(real)

    handle = service.start_ingest_copy(workspace, confirmation)
    record = _wait_terminal(service, workspace, handle.job_id)
    assert record.status == "failed"
    assert "심볼릭 링크" in (record.error or "")
    (asset,) = service.list_source_assets(workspace)
    assert asset.status == "failed"
    assert not (workspace / asset.relative_path).exists()
