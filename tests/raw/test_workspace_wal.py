"""작업공간 DB는 WAL이다 — 분석 작업이 쓰는 동안에도 화면의 읽기가 기다리지 않는다(W-23, D63).

롤백 저널에서는 쓰기가 커밋하는 동안(배타 잠금) 읽기가 `busy_timeout`만큼 기다렸다. 실측 합성
작업공간(원본 18개)에서 다른 스레드가 쉬지 않고 쓰는 동안 작업대 한 번 그리기가 56초였다(WAL 0.7초,
`scripts/bench_workbench.py`). 잠자기 없이 사건(`threading.Event`)으로만 순서를 맞춘다.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from vlogsrt.project.raw import cache_policy
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import workspace as workspace_module
from vlogsrt.project.raw.models import WorkspaceVersionError
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace, db_session, open_db

READER_WAIT_SECONDS = 5.0
"""롤백 저널이면 읽기가 `busy_timeout`(10초)까지 막힌다 — 그보다 짧게 기다려 실패로 드러낸다."""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(path, "제주 브이로그")
    return path


def _journal_mode(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        connection.close()


def test_a_new_workspace_db_is_wal(root: Path) -> None:
    assert _journal_mode(root / DB_NAME) == "wal"


def test_opening_an_older_workspace_turns_it_to_wal(root: Path) -> None:
    """D63 이전 작업공간(롤백 저널)도 열기 한 번으로 바뀐다 — 파일에 남는 설정이다."""
    workspace_module.release_wal_anchors()  # 닻이 쥐고 있으면 저널 모드를 되돌릴 수 없다
    connection = sqlite3.connect(root / DB_NAME)
    try:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    finally:
        connection.close()
    RawWorkspace.open(root)
    assert _journal_mode(root / DB_NAME) == "wal"


def test_an_unknown_db_schema_is_refused_without_touching_its_journal(root: Path) -> None:
    """모르는 버전은 조용히 바꾸지 않는다 — 저널 모드도 그대로 둔 채 실패한다."""
    workspace_module.release_wal_anchors()
    connection = sqlite3.connect(root / DB_NAME)
    try:
        connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        connection.execute(
            "UPDATE meta SET value = 'raw-workspace-db/9' WHERE key = 'db_schema_version'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(WorkspaceVersionError, match="DB schema"):
        RawWorkspace.open(root)
    assert _journal_mode(root / DB_NAME) == "delete"


@pytest.mark.parametrize("begin", ["BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"])
def test_a_reader_finishes_while_another_thread_holds_the_write_lock(
    root: Path, begin: str
) -> None:
    """쓰기 잠금을 쥔 채 멈춘 스레드 옆에서 다른 스레드의 읽기가 끝난다.

    `BEGIN EXCLUSIVE`는 롤백 저널에서 읽기까지 막는 잠금이다(커밋 순간의 배타 잠금과 같다) —
    WAL에서는 읽기를 막지 않는다.
    """
    holding, release, read_done = threading.Event(), threading.Event(), threading.Event()
    values: list[str] = []
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            with db_session(root / DB_NAME) as connection:
                connection.execute(begin)
                connection.execute("INSERT INTO meta (key, value) VALUES ('wal-probe', 'new')")
                holding.set()
                assert release.wait(READER_WAIT_SECONDS * 2)
        except BaseException as error:  # noqa: BLE001 - 시험 스레드의 실패를 본 스레드로 옮긴다
            failures.append(error)
            holding.set()

    def reader() -> None:
        try:
            connection = open_db(root / DB_NAME)
            try:
                row = connection.execute("SELECT value FROM meta WHERE key = 'workspace_id'")
                values.append(str(row.fetchone()[0]))
            finally:
                connection.close()
        except BaseException as error:  # noqa: BLE001 - 시험 스레드의 실패를 본 스레드로 옮긴다
            failures.append(error)
        finally:
            read_done.set()

    writing = threading.Thread(target=writer)
    writing.start()
    try:
        assert holding.wait(READER_WAIT_SECONDS)
        reading = threading.Thread(target=reader)
        reading.start()
        assert read_done.wait(READER_WAIT_SECONDS), "쓰기 잠금 옆에서 읽기가 막혔다"
        reading.join()
    finally:
        release.set()
        writing.join()
    assert failures == []
    assert values == [RawWorkspace.open(root).workspace_id()]
    with db_session(root / DB_NAME) as connection:
        stored = connection.execute("SELECT value FROM meta WHERE key = 'wal-probe'").fetchone()
    assert stored[0] == "new"  # 쓴 쪽은 그대로 커밋됐다


def test_a_read_only_uri_still_reads_a_wal_workspace(root: Path) -> None:
    """`scripts/verify_sync_workspace.py`의 `mode=ro` 열기 — 닻이 있어도, 없어도 읽는다."""
    expected = RawWorkspace.open(root).workspace_id()
    for release in (False, True):
        if release:
            workspace_module.release_wal_anchors()
        connection = sqlite3.connect(f"file:{root / DB_NAME}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = 'workspace_id'")
            assert row.fetchone()[0] == expected
        finally:
            connection.close()


def test_commits_are_kept_across_closing_every_connection(root: Path) -> None:
    """마지막 연결이 닫히면 SQLite가 체크포인트한다 — DB 파일 하나만으로도 커밋이 남는다."""
    with db_session(root / DB_NAME) as connection:
        connection.execute("INSERT INTO meta (key, value) VALUES ('wal-kept', 'yes')")
    workspace_module.release_wal_anchors()
    assert not (root / f"{DB_NAME}-wal").exists()
    copy = root.parent / "copy.sqlite3"
    copy.write_bytes((root / DB_NAME).read_bytes())
    connection = sqlite3.connect(copy)
    try:
        assert connection.execute("SELECT value FROM meta WHERE key = 'wal-kept'").fetchone() == (
            "yes",
        )
    finally:
        connection.close()


def test_anchors_are_bounded(tmp_path: Path) -> None:
    """닻은 최근 작업공간 몇 개만 — 시험 한 벌이 수천 개를 만들어도 fd가 늘지 않는다."""
    service = raw.RawWorkspaceService()
    for index in range(workspace_module.WAL_ANCHOR_LIMIT + 3):
        service.create_workspace(tmp_path / f"w{index}", f"작업공간 {index}")
    assert len(workspace_module._wal_anchors) == workspace_module.WAL_ANCHOR_LIMIT
    newest = (tmp_path / f"w{workspace_module.WAL_ANCHOR_LIMIT + 2}" / DB_NAME).resolve()
    assert newest in workspace_module._wal_anchors


def test_cache_purge_never_removes_wal_side_files(tmp_path: Path) -> None:
    """옆 파일도 DB 정본이다 — cache 안에 잘못 붙은 이름이어도 지우지 않는다."""
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주 브이로그")
    guarded = ("project.sqlite3-wal", "project.sqlite3-shm", "project.sqlite3-journal")
    for name in guarded:
        (root / "cache" / name).write_bytes(b"side")
    plan = cache_policy.plan_cache_purge(root)
    refused = {item.relative_path for item in plan.refused}
    assert {f"cache/{name}" for name in guarded} <= refused
    assert not {f"cache/{name}" for name in guarded} & set(plan.purge_paths)
