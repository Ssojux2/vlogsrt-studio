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

from vlogsrt import preprocess
from vlogsrt.project.raw import ingest
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import GlobalOrderError, WorkspaceVersionError
from vlogsrt.project.raw.workspace import (
    CAPTURE_BASIS_KEY,
    CAPTURE_TIME_BASIS,
    DB_NAME,
    DB_SCHEMA_VERSION,
)

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


def test_v2_workspace_upgrades_to_current(workspace: Path, tmp_path: Path) -> None:
    """받아들인 v2(A2) 작업공간도 열린다 — A3 표까지 덧붙고 데이터는 그대로."""
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
    assert status.db_schema_version == DB_SCHEMA_VERSION
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


# ── D53 — 촬영 시각 순서·되채움 ────────────────────────────────────────────────


def _timed_inspector(times: dict[str, str]):
    """파일 이름 → 촬영 시각(가져올 때 본 값) — 실측 모양의 FCP 가져온 시각을 흉내 낸다."""

    def inspect(path: Path) -> ingest.SourceInspection:
        stamp = times.get(path.name)
        return ingest.SourceInspection(
            duration_seconds=1.0,
            width=64,
            height=32,
            audio_streams=1,
            capture_time=stamp,
            capture_time_status="valid" if stamp else "uninspected",
        )

    return inspect


def _import_timed(
    service: raw.RawWorkspaceService, workspace: Path, card: Path, times: dict[str, str]
) -> None:
    proposal = service.scan_sources(workspace, card, _timed_inspector(times))
    handle = service.start_ingest_copy(workspace, service.confirm_ingest(workspace, proposal))
    deadline = time.monotonic() + 5.0
    while service.get_job(workspace, handle.job_id).status not in TERMINAL:
        assert time.monotonic() < deadline, "가져오기 작업이 끝나지 않았다"
        time.sleep(0.01)
    assert service.get_job(workspace, handle.job_id).status == "succeeded"


def _names(service: raw.RawWorkspaceService, workspace: Path, revision) -> list[str]:
    names = {asset.asset_id: asset.source_name for asset in service.list_source_assets(workspace)}
    return [names[entry.asset_id] for entry in revision.entries]


def test_ensure_source_order_reconfirms_on_sequence_mismatch_and_is_idempotent(
    workspace: Path, tmp_path: Path
) -> None:
    """D53 — 같은 원본 묶음이라도 순서가 제안과 다르면 다시 확정한다. 같으면 아무것도 안 한다.

    예전에는 원본 묶음(집합)만 비교해 손으로 뒤집은 순서가 남았다 — '언제나 촬영 시각 순'이
    사용자 지시라 이제 순서까지 맞춘다.
    """
    service = raw.RawWorkspaceService()
    _import_timed(
        service,
        workspace,
        _card(tmp_path, "a", {"late.mp4": b"1"}),
        {"late.mp4": "2026-08-11T09:00:00+00:00"},
    )
    _import_timed(
        service,
        workspace,
        _card(tmp_path, "b", {"early.mp4": b"2"}),
        {"early.mp4": "2026-08-11T08:00:00+00:00"},
    )
    proposal = [asset.asset_id for asset in service.global_order_proposal(workspace)]
    manual = service.confirm_global_source_order(workspace, list(reversed(proposal)))
    assert _names(service, workspace, manual) == ["late.mp4", "early.mp4"]

    assert service.ensure_source_order(workspace) is True
    latest = service.latest_global_source_order(workspace)
    assert latest.revision_id != manual.revision_id
    assert _names(service, workspace, latest) == ["early.mp4", "late.mp4"]
    count = len(service.list_global_source_orders(workspace))
    assert service.ensure_source_order(workspace) is False
    assert len(service.list_global_source_orders(workspace)) == count


def test_record_capture_times_changes_only_that_field_in_one_transaction(
    workspace: Path, tmp_path: Path
) -> None:
    """촬영 시각 정정은 잰 근거만 고친다 — 출처 신원·검증 이력·순번은 그대로, 한 번에 다 쓴다."""
    service = raw.RawWorkspaceService()
    _import_timed(
        service,
        workspace,
        _card(tmp_path, "a", {"a.mp4": b"1", "b.mp4": b"2"}),
        {"a.mp4": "2026-08-14T10:38:34+00:00", "b.mp4": "2026-08-14T10:38:40+00:00"},
    )
    before = {asset.source_name: asset for asset in service.list_source_assets(workspace)}
    store = service._workspace(workspace)
    store.record_capture_times(
        {
            before["a.mp4"].asset_id: "2026-08-11T03:50:46+00:00",
            before["b.mp4"].asset_id: None,
        },
        basis="probe/v0",
    )
    after = {asset.source_name: asset for asset in service.list_source_assets(workspace)}
    assert after["a.mp4"].capture_time == "2026-08-11T03:50:46+00:00"
    assert after["b.mp4"].capture_time is None
    assert store.meta_value(CAPTURE_BASIS_KEY) == "probe/v0"
    unchanged = ("capture_time", "updated_at")
    for name in before:
        for field in before[name].__dataclass_fields__:
            if field not in unchanged:
                assert getattr(after[name], field) == getattr(before[name], field), field
    # 없는 자산이 하나라도 섞이면 아무것도 쓰지 않는다 — 반쯤 고친 시각이 순서를 만들지 않게.
    with pytest.raises(raw.RawWorkspaceError, match="없는 자산"):
        store.record_capture_times(
            {before["a.mp4"].asset_id: None, "source_0000000000000000": None}, basis="x"
        )
    kept = {
        asset.source_name: asset.capture_time for asset in service.list_source_assets(workspace)
    }
    assert kept == {"a.mp4": "2026-08-11T03:50:46+00:00", "b.mp4": None}
    assert store.meta_value(CAPTURE_BASIS_KEY) == "probe/v0"


def test_selected_paths_come_back_in_shot_order(workspace: Path, tmp_path: Path) -> None:
    """고른 경로의 원본 ID는 촬영 시각 순이다 — 분석 줄·견적이 화면 순서를 따른다(D53)."""
    service = raw.RawWorkspaceService()
    late = _card(tmp_path, "a", {"late.mp4": b"1"})
    early = _card(tmp_path, "b", {"early.mp4": b"2"})
    _import_timed(service, workspace, late, {"late.mp4": "2026-08-11T09:00:00+00:00"})
    _import_timed(service, workspace, early, {"early.mp4": "2026-08-11T08:00:00+00:00"})
    names = {a.asset_id: a.source_name for a in service.list_source_assets(workspace)}
    paths = [str(late / "late.mp4"), str(early / "early.mp4")]
    picked = service.selected_asset_ids(workspace, paths)
    assert [names[asset_id] for asset_id in picked] == ["early.mp4", "late.mp4"]


def test_capture_backfill_writes_nothing_until_every_probe_returns(
    workspace: Path, tmp_path: Path
) -> None:
    """되채움은 다 읽은 뒤 한 번에 쓴다 — 그 사이 순서 확정이 반쯤 고친 시각을 보지 않는다."""
    service = raw.RawWorkspaceService()
    _import_timed(
        service,
        workspace,
        _card(tmp_path, "c", {"a.mp4": b"1", "b.mp4": b"2", "c.mp4": b"3"}),
        {
            "a.mp4": "2026-08-14T10:39:00+00:00",
            "b.mp4": "2026-08-14T10:38:00+00:00",
            "c.mp4": "2026-08-14T10:37:00+00:00",
        },
    )
    _forget_capture_basis(workspace)
    service.ensure_source_order(workspace)
    seen: list[dict[str, str | None]] = []

    def probe(path: Path) -> preprocess.CreationTime:
        seen.append({a.source_name: a.capture_time for a in service.list_source_assets(workspace)})
        assert service.ensure_source_order(workspace) is False  # 아직 아무것도 바뀌지 않았다
        shot = {"a.mp4": "08:00", "b.mp4": "08:10", "c.mp4": "08:20"}[path.name]
        return preprocess._parse_creation_time(f"2026-08-11T{shot}:00Z")

    assert service.refresh_capture_times(workspace, probe=probe) == ()
    assert len(seen) == 3 and all(snapshot == seen[0] for snapshot in seen)
    assert service.ensure_source_order(workspace) is True
    order = service.latest_global_source_order(workspace)
    names = {a.asset_id: a.source_name for a in service.list_source_assets(workspace)}
    assert [names[e.asset_id] for e in order.entries] == ["a.mp4", "b.mp4", "c.mp4"]


def _forget_capture_basis(workspace: Path) -> None:
    """D53 이전에 만든 작업공간 흉내 — 촬영 시각 근거 판 표지가 없다."""
    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.execute("DELETE FROM meta WHERE key = ?", (CAPTURE_BASIS_KEY,))


def test_new_workspace_records_the_current_capture_basis(workspace: Path) -> None:
    """새 작업공간의 가져오기는 처음부터 지금 규칙으로 읽는다 — 되채울 것이 없다."""
    store = raw.RawWorkspaceService()._workspace(workspace)
    assert store.meta_value(CAPTURE_BASIS_KEY) == CAPTURE_TIME_BASIS


def test_capture_backfill_probes_once_sets_flag_and_reorders(
    workspace: Path, tmp_path: Path
) -> None:
    """D53 되채움 — 옛 작업공간의 가져온 시각을 촬영 시각으로 고치고, 표지를 남기고, 순서가 따른다.

    실측 모양: 가져온 시각으로는 b가 앞이지만(먼저 가져왔다) 촬영은 a가 먼저다.
    """
    service = raw.RawWorkspaceService()
    _import_timed(
        service,
        workspace,
        _card(tmp_path, "c", {"a.mp4": b"1", "b.mp4": b"2"}),
        {
            "a.mp4": "2026-08-14T10:39:00+00:00",
            "b.mp4": "2026-08-14T10:38:00+00:00",
        },
    )
    _forget_capture_basis(workspace)
    service.ensure_source_order(workspace)
    first = service.latest_global_source_order(workspace)
    assert _names(service, workspace, first) == ["b.mp4", "a.mp4"]
    shot = {"a.mp4": "2026-08-11T03:50:46Z", "b.mp4": "2026-08-11T03:51:58Z"}
    probed: list[str] = []

    def probe(path: Path) -> preprocess.CreationTime:
        probed.append(path.name)
        return preprocess._parse_creation_time(shot[path.name])

    assert service.refresh_capture_times(workspace, probe=probe) == ()
    assert sorted(probed) == ["a.mp4", "b.mp4"]
    times = {a.source_name: a.capture_time for a in service.list_source_assets(workspace)}
    assert times == {"a.mp4": "2026-08-11T03:50:46+00:00", "b.mp4": "2026-08-11T03:51:58+00:00"}
    assert service._workspace(workspace).meta_value(CAPTURE_BASIS_KEY) == CAPTURE_TIME_BASIS
    assert service.ensure_source_order(workspace) is True
    latest = service.latest_global_source_order(workspace)
    assert _names(service, workspace, latest) == ["a.mp4", "b.mp4"]
    probed.clear()
    assert (
        service.refresh_capture_times(workspace, probe=probe) == ()
    )  # 표지가 있으면 다시 안 읽는다
    assert probed == []


def test_capture_backfill_failure_warns_keeps_values_and_retries_next_time(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원본 하나를 못 읽으면(볼륨이 빠졌다) 경고하고 기록값을 그대로 둔다 — 표지는 남기지 않는다."""
    from vlogsrt import log

    service = raw.RawWorkspaceService()
    _import_timed(
        service,
        workspace,
        _card(tmp_path, "c", {"a.mp4": b"1", "b.mp4": b"2"}),
        {
            "a.mp4": "2026-08-14T10:39:00+00:00",
            "b.mp4": "2026-08-14T10:38:00+00:00",
        },
    )
    _forget_capture_basis(workspace)
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)

    def flaky(path: Path) -> preprocess.CreationTime:
        if path.name == "a.mp4":
            raise RuntimeError("ffprobe 실패 (코드 1)\nNo such file or directory")
        return preprocess._parse_creation_time("2026-08-11T03:51:58Z")

    assert service.refresh_capture_times(workspace, probe=flaky) == ("a.mp4",)
    times = {a.source_name: a.capture_time for a in service.list_source_assets(workspace)}
    assert times == {"a.mp4": "2026-08-14T10:39:00+00:00", "b.mp4": "2026-08-11T03:51:58+00:00"}
    assert service._workspace(workspace).meta_value(CAPTURE_BASIS_KEY) is None
    assert any("a.mp4" in w and "No such file" in w for w in warnings)
    calls: list[str] = []

    def healed(path: Path) -> preprocess.CreationTime:
        calls.append(path.name)
        return preprocess._parse_creation_time("2026-08-11T03:50:46Z")

    assert service.refresh_capture_times(workspace, probe=healed) == ()
    assert sorted(calls) == ["a.mp4", "b.mp4"]
    assert service._workspace(workspace).meta_value(CAPTURE_BASIS_KEY) == CAPTURE_TIME_BASIS
