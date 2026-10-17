"""작업공간 DB의 `-wal`은 줄어든다 — 크기 상한·작업 끝 PASSIVE·닻 닫기 TRUNCATE(W-24 세션 H, D63).

SQLite 기본값(`journal_size_limit = -1`)에서는 `-wal`이 가장 컸던 때 크기로 남았다(실제 3.4 MB).
바쁜 체크포인트는 예상된 결과다 — 닫기·종료를 막지 않고 경고로 남는다. 잠자기 없이 사건으로 맞춘다.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from vlogsrt import log
from vlogsrt.project.raw import jobs as jobs_module
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import workspace as workspace_module
from vlogsrt.project.raw.analysis_config import (
    WORKSPACE_DB,
    WorkspaceDbSettings,
    validate_workspace_db_settings,
)
from vlogsrt.project.raw.jobs import JobManager
from vlogsrt.project.raw.workspace import (
    DB_NAME,
    RawWorkspace,
    checkpoint_wal,
    open_db,
    release_wal_anchor,
    release_wal_anchors,
)

BURST_ROWS = 400
BURST_BYTES = 8000
"""400줄 × 8 KB ≈ 3.2 MB — 실제로 본 3.4 MB `-wal`과 같은 크기를 한 번에 쓴다."""


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(path, "제주 브이로그")
    yield path
    release_wal_anchor(path / DB_NAME)


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(log, "warn", seen.append)
    return seen


def _wal_size(root: Path) -> int:
    wal = root / f"{DB_NAME}-wal"
    return wal.stat().st_size if wal.exists() else 0


def _burst(root: Path) -> None:
    """한 트랜잭션에 몰아 쓴다 — 자동 체크포인트(1000쪽)보다 작아 `-wal`이 그대로 자란다."""
    connection = open_db(root / DB_NAME)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS burst (payload BLOB)")
            connection.executemany(
                "INSERT INTO burst VALUES (randomblob(?))", [(BURST_BYTES,)] * BURST_ROWS
            )
    finally:
        connection.close()


def _integrity(root: Path) -> str:
    connection = sqlite3.connect(root / DB_NAME)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def test_every_workspace_connection_reads_back_the_configured_limit(root: Path) -> None:
    connection = open_db(root / DB_NAME)
    try:
        limit = connection.execute("PRAGMA journal_size_limit").fetchone()[0]
    finally:
        connection.close()
    assert limit == WORKSPACE_DB.journal_size_limit_bytes == 1_048_576
    anchor = workspace_module._wal_anchors[(root / DB_NAME).resolve()]
    assert anchor.execute("PRAGMA journal_size_limit").fetchone()[0] == limit


def _small_write(root: Path) -> None:
    connection = open_db(root / DB_NAME)
    try:
        with connection:
            connection.execute("INSERT INTO burst VALUES (x'00')")
    finally:
        connection.close()


def _run_burst_job(root: Path) -> JobManager:
    manager = JobManager(RawWorkspace.open(root))
    handle = manager.submit("test_burst", "fp-burst", lambda context: _burst(root))
    _join_job_thread(handle.job_id)  # 끝 상태 기록과 체크포인트까지 기다린다
    assert manager.get(handle.job_id).status == "succeeded"
    return manager


def test_a_burst_shrinks_back_under_the_limit_after_a_job_and_to_zero_on_close(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """버스트로 상한을 넘긴 `-wal`이 작업 끝 PASSIVE 뒤 상한 아래로, 닻 닫기 TRUNCATE로 0이 된다.

    PASSIVE는 내용을 옮길 뿐 파일을 자르지 않는다 — SQLite는 다 옮긴 WAL을 다음 커밋이 처음부터
    다시 쓸 때 상한으로 자른다. 버스트는 자동 체크포인트(1000쪽)보다 작아 그것만으로는 줄지 않는다.
    """
    modes: list[str] = []
    real = jobs_module.checkpoint_wal

    def recording(connection: sqlite3.Connection, mode: str, path: Path) -> bool:
        # 앞선 시험이 남긴 데몬 작업 스레드도 이 자리를 지난다 — 이 작업공간 것만 센다.
        if path == root / DB_NAME:
            modes.append(mode)
        return real(connection, mode, path)

    monkeypatch.setattr(jobs_module, "checkpoint_wal", recording)
    _run_burst_job(root)
    assert modes == ["PASSIVE"]  # 한 번, 다시 묻지 않는다
    assert _wal_size(root) > WORKSPACE_DB.journal_size_limit_bytes
    _small_write(root)
    assert 0 < _wal_size(root) <= WORKSPACE_DB.journal_size_limit_bytes

    assert release_wal_anchor(root / DB_NAME)
    assert _wal_size(root) == 0
    assert _integrity(root) == "ok"


def test_without_the_job_checkpoint_the_next_write_keeps_the_burst_size(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """대조 — 작업 끝 체크포인트가 없으면 다음 커밋도 `-wal`을 줄이지 못한다(D63 H 이전 모양)."""
    monkeypatch.setattr(JobManager, "_checkpoint_after_finish", lambda self, job_id: None)
    _run_burst_job(root)
    _small_write(root)
    assert _wal_size(root) > WORKSPACE_DB.journal_size_limit_bytes


def _join_job_thread(job_id: str) -> None:
    for thread in threading.enumerate():
        if thread.name == job_id:
            thread.join(timeout=30)
            assert not thread.is_alive()


def test_truncate_on_close_with_a_reader_left_open_still_empties_the_wal(root: Path) -> None:
    """닻이 마지막 연결이 아니어도 TRUNCATE가 `-wal`을 0으로 자른다(SQLite가 지우지 않는 경우)."""
    _burst(root)
    other = sqlite3.connect(root / DB_NAME)
    try:
        other.execute("SELECT count(*) FROM burst").fetchone()  # 옆 파일을 실제로 연다
        assert release_wal_anchor(root / DB_NAME)
        assert (root / f"{DB_NAME}-wal").exists()
        assert _wal_size(root) == 0
    finally:
        other.close()


def test_a_checkpoint_behind_a_held_write_transaction_warns_and_does_not_corrupt(
    root: Path, warnings: list[str]
) -> None:
    _burst(root)
    writer = sqlite3.connect(root / DB_NAME, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO burst VALUES (x'00')")
        assert release_wal_anchor(root / DB_NAME)  # 던지지 않는다
        assert any("TRUNCATE" in line and "바빴다" in line for line in warnings)
        writer.execute("COMMIT")
    finally:
        writer.close()
    assert _integrity(root) == "ok"
    connection = sqlite3.connect(root / DB_NAME)
    try:
        assert connection.execute("SELECT count(*) FROM burst").fetchone()[0] == BURST_ROWS + 1
    finally:
        connection.close()


def test_atexit_release_survives_a_busy_writer(root: Path, warnings: list[str]) -> None:
    writer = sqlite3.connect(root / DB_NAME, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("CREATE TABLE held (x)")
        release_wal_anchors()
        writer.execute("COMMIT")
    finally:
        writer.close()
    assert workspace_module._wal_anchors == {}
    assert warnings
    assert _integrity(root) == "ok"


def test_passive_does_not_wait_for_a_held_write_transaction(root: Path) -> None:
    _burst(root)
    writer = sqlite3.connect(root / DB_NAME, isolation_level=None)
    connection = open_db(root / DB_NAME)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert checkpoint_wal(connection, "PASSIVE", root / DB_NAME)
        writer.execute("ROLLBACK")
    finally:
        connection.close()
        writer.close()


class _LockedConnection:
    def __init__(self, error: sqlite3.Error) -> None:
        self.error = error

    def execute(self, statement: str) -> object:
        raise self.error


def test_a_locked_checkpoint_is_a_warned_result_and_other_errors_still_raise(
    warnings: list[str], tmp_path: Path
) -> None:
    locked = _LockedConnection(sqlite3.OperationalError("database is locked"))
    assert not checkpoint_wal(locked, "TRUNCATE", tmp_path)  # type: ignore[arg-type]
    assert any("잠금" in line for line in warnings)
    broken = _LockedConnection(sqlite3.OperationalError("disk I/O error"))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        checkpoint_wal(broken, "PASSIVE", tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="체크포인트 방식"):
        checkpoint_wal(locked, "FULL", tmp_path)  # type: ignore[arg-type]


def test_the_job_checkpoint_never_creates_a_missing_db(tmp_path: Path) -> None:
    """작업이 끝난 사이 작업공간이 치워졌으면 빈 DB를 만들지 않는다(`must_exist`)."""
    missing = tmp_path / "gone" / DB_NAME
    missing.parent.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        open_db(missing, must_exist=True)
    assert not missing.exists()


@pytest.mark.parametrize(
    "settings",
    [
        WorkspaceDbSettings(journal_size_limit_bytes=-1),
        WorkspaceDbSettings(journal_size_limit_bytes=True),
        WorkspaceDbSettings(checkpoint_busy_timeout_ms=0),
        WorkspaceDbSettings(checkpoint_busy_timeout_ms=0.5),  # type: ignore[arg-type]
    ],
)
def test_settings_outside_the_contract_are_refused(settings: WorkspaceDbSettings) -> None:
    with pytest.raises(ValueError, match="작업공간 DB 설정"):
        validate_workspace_db_settings(settings)
