"""A2F1 — 배치를 넘는 전체 순서·결정적 목록·스키마 v3(루트 지적 반영).

`list_source_assets(batch=None)`가 `ordinal`만으로 정렬되면 모든 배치의 1번이
무작위 자산 ID로 뒤섞인다. 전체 순서 확정은 **불변 리비전**이며 배치 안
리비전을 다시 쓰지 않는다 — 옛 패키지를 조용히 다시 섞지 않는다(§6.5).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from vlogsrt.project.raw import ingest
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import GlobalOrderError, WorkspaceVersionError
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


def _import(service: raw.RawWorkspaceService, workspace: Path, card: Path) -> str:
    proposal = service.scan_sources(workspace, card, _fake_inspector)
    confirmation = service.confirm_ingest(workspace, proposal)
    handle = service.start_ingest_copy(workspace, confirmation)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = service.get_job(workspace, handle.job_id)
        if record.status in TERMINAL:
            assert record.status == "succeeded", record.error
            return confirmation.batch_dir
        time.sleep(0.01)
    raise AssertionError("가져오기 작업이 끝나지 않았다")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주")
    return root


def test_list_source_assets_orders_batches_then_ordinals(workspace: Path, tmp_path: Path) -> None:
    """전체 목록은 배치 연대순 → 배치 안 확정 순번 — 무작위 ID 뒤섞임이 없다."""
    service = raw.RawWorkspaceService()
    _import(
        service, workspace, _card(tmp_path, "card-a", {"DJI_0010.MP4": b"10", "DJI_0002.MP4": b"2"})
    )
    _import(
        service, workspace, _card(tmp_path, "card-b", {"B_0001.MOV": b"b1", "B_0002.MOV": b"b2"})
    )
    _import(service, workspace, _card(tmp_path, "card-c", {"C.MP4": b"c"}))

    assets = service.list_source_assets(workspace)
    listing = [(asset.batch_dir, asset.ordinal) for asset in assets]
    assert listing == [
        ("import_000001", 1),
        ("import_000001", 2),
        ("import_000002", 1),
        ("import_000002", 2),
        ("import_000003", 1),
    ]
    # 배치 안 사용자(자연 정렬 제안) 순서는 그대로다 — DJI_0002가 0010보다 앞.
    first_batch = service.list_source_assets(workspace, "import_000001")
    assert [asset.source_name for asset in first_batch] == ["DJI_0002.MP4", "DJI_0010.MP4"]


def test_global_order_confirm_covers_ready_assets_immutably(
    workspace: Path, tmp_path: Path
) -> None:
    """전체 순서 확정 — 불변 리비전, 재확정은 새 리비전, 옛 스냅숏은 그대로."""
    service = raw.RawWorkspaceService()
    _import(service, workspace, _card(tmp_path, "card-a", {"a1.mp4": b"1"}))
    _import(service, workspace, _card(tmp_path, "card-b", {"b1.mp4": b"2", "b2.mp4": b"3"}))

    proposal = service.global_order_proposal(workspace)
    assert [(a.batch_dir, a.ordinal) for a in proposal] == [
        ("import_000001", 1),
        ("import_000002", 1),
        ("import_000002", 2),
    ]

    first = service.confirm_global_source_order(workspace)
    assert [entry.ordinal for entry in first.entries] == [1, 2, 3]
    assert first.basis == "user_confirmed"
    assert service.latest_global_source_order(workspace).revision_id == first.revision_id

    # 사용자가 배치를 건너뛰어 순서를 바꿔 다시 확정 — 새 리비전, 옛 것은 불변.
    reversed_ids = [asset.asset_id for asset in reversed(proposal)]
    second = service.confirm_global_source_order(workspace, reversed_ids)
    assert second.revision_id != first.revision_id
    assert [entry.asset_id for entry in second.entries] == reversed_ids
    stored_first = [
        revision
        for revision in service.list_global_source_orders(workspace)
        if revision.revision_id == first.revision_id
    ][0]
    assert [entry.asset_id for entry in stored_first.entries] == [
        asset.asset_id for asset in proposal
    ]  # 옛 스냅숏은 다시 쓰이지 않았다


def test_global_order_confirmation_leaves_batch_revisions_untouched(
    workspace: Path, tmp_path: Path
) -> None:
    """전체 확정이 배치 안 리비전을 조용히 다시 섞지 않는다(옛 패키지 보존)."""
    service = raw.RawWorkspaceService()
    _import(service, workspace, _card(tmp_path, "card-a", {"a1.mp4": b"1"}))
    _import(service, workspace, _card(tmp_path, "card-b", {"b1.mp4": b"2"}))
    before = service.list_order_revisions(workspace)

    reversed_ids = [asset.asset_id for asset in reversed(service.global_order_proposal(workspace))]
    service.confirm_global_source_order(workspace, reversed_ids)

    after = service.list_order_revisions(workspace)
    assert [r.revision_id for r in after] == [r.revision_id for r in before]
    assert [r.entries for r in after] == [r.entries for r in before]


def test_global_order_rejects_incomplete_or_unknown_inputs(workspace: Path, tmp_path: Path) -> None:
    """모르는 ID·ready 아님·빠진 원본은 확정을 만들지 않고 이유를 말한다."""
    service = raw.RawWorkspaceService()
    _import(service, workspace, _card(tmp_path, "card-a", {"a1.mp4": b"1"}))
    ready_ids = [asset.asset_id for asset in service.global_order_proposal(workspace)]

    with pytest.raises(GlobalOrderError, match="모르는 자산 ID"):
        service.confirm_global_source_order(workspace, [*ready_ids, "source_unknown"])
    with pytest.raises(GlobalOrderError, match="빠진 ready 원본"):
        service.confirm_global_source_order(workspace, [])  # 전부 빠졌다
    with pytest.raises(GlobalOrderError, match="중복"):
        service.confirm_global_source_order(workspace, [*ready_ids, *ready_ids])
    assert service.list_global_source_orders(workspace) == ()  # 실패는 기록을 안 남겼다


def test_global_order_without_ready_assets_is_an_error(workspace: Path) -> None:
    """가져온 원본이 없으면 전체 확정이 성공으로 위장하지 않는다."""
    service = raw.RawWorkspaceService()
    with pytest.raises(GlobalOrderError, match="ready 원본이 없다"):
        service.confirm_global_source_order(workspace)


def test_v2_workspace_upgrades_to_v3(workspace: Path, tmp_path: Path) -> None:
    """받아들인 v2(A2) 작업공간도 열린다 — 전체 순서 표만 덧붙고 데이터는 그대로."""
    service = raw.RawWorkspaceService()
    _import(service, workspace, _card(tmp_path, "card-a", {"a1.mp4": b"1"}))
    (asset,) = service.list_source_assets(workspace)

    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.executescript(
            "DROP TABLE global_order_revisions;"
            "UPDATE meta SET value = 'raw-workspace-db/2' WHERE key = 'db_schema_version';"
        )

    restarted = raw.RawWorkspaceService()  # 재시작 — 디스크의 v2를 연다
    status = restarted.open_workspace(workspace)
    assert status.db_schema_version == "raw-workspace-db/3"
    assert restarted.list_source_assets(workspace)[0].status == "ready"
    revision = restarted.confirm_global_source_order(workspace)
    assert [entry.asset_id for entry in revision.entries] == [asset.asset_id]
    with pytest.raises(WorkspaceVersionError):
        # 모르는 버전은 여전히 거부 — v3 뒤에 온 미래의 표기를 흉내 낸다.
        with sqlite3.connect(workspace / DB_NAME) as connection:
            connection.execute(
                "UPDATE meta SET value = 'raw-workspace-db/99' WHERE key = 'db_schema_version'"
            )
        raw.RawWorkspaceService().open_workspace(workspace)
