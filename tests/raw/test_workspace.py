"""A1 작업공간 — 생성·열기·버전·설정 저장 계약(계획 §9.1·§15.1·§16 A1).

합성 임시 폴더만 쓴다 — 네트워크·ffmpeg·원본 미디어·실API 픽스처 전부 무관.
"""

from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.config import WORKFLOW_SCHEMA_VERSION, RawWorkflowConfig
from vlogsrt.project.raw.models import (
    WorkspaceAlreadyExistsError,
    WorkspaceNotFoundError,
    WorkspaceVersionError,
)
from vlogsrt.project.raw.workspace import (
    DB_NAME,
    DB_SCHEMA_VERSION,
    PROJECT_TOML_NAME,
    WORKSPACE_LAYOUT,
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """만들어 둔 합성 작업공간 루트. 서비스 인스턴스는 테스트마다 새로 띄운다."""
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주 브이로그")
    return root


def test_create_makes_the_full_layout_and_identity(tmp_path: Path) -> None:
    """§9.1의 디렉터리들과 project.toml·DB가 생긴다 — 없는 것은 다음 단계가 채운다."""
    root = tmp_path / "VlogProject"
    status = raw.RawWorkspaceService().create_workspace(root, "제주 브이로그")
    for directory in WORKSPACE_LAYOUT:
        assert (root / directory).is_dir(), directory
    with (root / PROJECT_TOML_NAME).open("rb") as handle:
        identity = tomllib.load(handle)
    assert identity["schema_version"] == WORKFLOW_SCHEMA_VERSION
    assert identity["name"] == "제주 브이로그"
    assert (root / DB_NAME).is_file()
    assert status.db_schema_version == DB_SCHEMA_VERSION
    assert status.network_mode == "offline"
    assert status.media_count == 0


def test_create_rejects_nonempty_or_existing_folders(tmp_path: Path) -> None:
    """빈 폴더에만 만든다 — 있던 파일을 덮어쓰는 길은 없다."""
    service = raw.RawWorkspaceService()
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "기존.txt").write_text("지켜야 한다", encoding="utf-8")
    with pytest.raises(WorkspaceAlreadyExistsError):
        service.create_workspace(dirty, "이름")
    clean = tmp_path / "clean"
    service.create_workspace(clean, "이름")
    with pytest.raises(WorkspaceAlreadyExistsError):
        service.create_workspace(clean, "또 다른 이름")
    assert (dirty / "기존.txt").read_text(encoding="utf-8") == "지켜야 한다"


def test_open_rejects_plain_folders(tmp_path: Path) -> None:
    """project.toml·DB가 없으면 v2 작업공간이 아니다 — 조용히 만들어 열지 않는다."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(WorkspaceNotFoundError):
        raw.RawWorkspaceService().open_workspace(empty)
    with pytest.raises(WorkspaceNotFoundError):
        raw.RawWorkspaceService().open_workspace(tmp_path / "없는-폴더")


def test_open_rejects_an_unknown_workflow_schema(workspace_root: Path) -> None:
    """schema가 다른 작업공간은 못 연다 — 마이그레이션은 암묵적으로 일어나지 않는다."""
    identity = (workspace_root / PROJECT_TOML_NAME).read_text(encoding="utf-8")
    (workspace_root / PROJECT_TOML_NAME).write_text(
        identity.replace(WORKFLOW_SCHEMA_VERSION, "raw-workspace/v9"), encoding="utf-8"
    )
    with pytest.raises(WorkspaceVersionError, match="workflow schema"):
        raw.RawWorkspaceService().open_workspace(workspace_root)


def test_open_rejects_an_unknown_db_schema(workspace_root: Path) -> None:
    with sqlite3.connect(workspace_root / DB_NAME) as connection:
        connection.execute(
            "UPDATE meta SET value = 'raw-workspace-db/9' WHERE key = 'db_schema_version'"
        )
        connection.commit()
    with pytest.raises(WorkspaceVersionError, match="DB schema"):
        raw.RawWorkspaceService().open_workspace(workspace_root)


def test_settings_roundtrip_and_offline_default(workspace_root: Path) -> None:
    """설정은 A0의 검증 왕복을 그대로 지난다 — 기본은 오프라인이다(§14.3)."""
    service = raw.RawWorkspaceService()
    assert service.settings(workspace_root) == RawWorkflowConfig()
    hinted = replace(
        RawWorkflowConfig().caption, languages=("ko", "en"), keywords=("북촌한옥마을",)
    )
    service.update_settings(workspace_root, RawWorkflowConfig(caption=hinted))
    assert service.settings(workspace_root).caption.keywords == ("북촌한옥마을",)
    assert service.settings(workspace_root).network_mode == "offline"


def test_tampered_settings_fail_closed(workspace_root: Path) -> None:
    """DB의 설정 문자열이 계약 밖으로 바뀌면 읽는 쪽이 거부한다 — 왕복이 우회로가 아니다."""
    weakened = raw.RawWorkspaceService().settings(workspace_root)
    assert weakened.network_mode == "offline"
    with sqlite3.connect(workspace_root / DB_NAME) as connection:
        payload = connection.execute(
            "SELECT value FROM settings WHERE key = 'workflow'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = 'workflow'",
            (payload.replace('"offline"', '"always_on"'),),
        )
        connection.commit()
    with pytest.raises(ValueError, match="네트워크 모드"):
        raw.RawWorkspaceService().settings(workspace_root)


def test_reopening_does_not_touch_running_jobs(workspace_root: Path) -> None:
    """작업 복구는 서버 시작 때만 — 열기·새로고침이 작업 상태를 바꾸지 않는다(§14.1)."""
    with sqlite3.connect(workspace_root / DB_NAME) as connection:
        connection.execute(
            "INSERT INTO jobs (job_id, kind, input_fingerprint, status, progress_current,"
            " progress_total, error, cancellation_requested, created_at, updated_at)"
            " VALUES ('job_000001', 'probe', 'fp', 'running', 1, 3, NULL, 0,"
            " '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:01+00:00')"
        )
        connection.commit()
    service = raw.RawWorkspaceService()
    service.open_workspace(workspace_root)  # UI 새로고침·재오픈에 해당
    service.open_workspace(workspace_root)
    assert service.list_jobs(workspace_root)[0].status == "running"


def test_status_counts_media_and_active_jobs(workspace_root: Path) -> None:
    service = raw.RawWorkspaceService()
    media = workspace_root / "originals" / "clip.mp4"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b"0123456789")
    service.register_media(workspace_root, media, "original")
    with sqlite3.connect(workspace_root / DB_NAME) as connection:
        connection.execute(
            "INSERT INTO jobs (job_id, kind, input_fingerprint, status, progress_current,"
            " progress_total, error, cancellation_requested, created_at, updated_at)"
            " VALUES ('job_000001', 'probe', 'fp', 'running', 0, 0, NULL, 0,"
            " '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:00+00:00'),"
            " ('job_000002', 'probe', 'fp2', 'succeeded', 0, 0, NULL, 0,"
            " '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:00+00:00')"
        )
        connection.commit()
    status = service.workspace_status(workspace_root)
    assert status.media_count == 1
    assert status.active_jobs == 1
    assert status.workflow_schema_version == WORKFLOW_SCHEMA_VERSION


def test_each_workspace_gets_a_stable_distinct_identity(tmp_path: Path) -> None:
    """작업공간 신원은 불변이고 서로 다르다 — 선택 검증·바인딩의 기준 토큰."""
    import re

    service = raw.RawWorkspaceService()
    first = service.create_workspace(tmp_path / "a", "A")
    second = service.create_workspace(tmp_path / "b", "B")
    pattern = re.compile(r"^ws_[0-9a-f]{16}$")
    assert pattern.match(first.workspace_id)
    assert pattern.match(second.workspace_id)
    assert first.workspace_id != second.workspace_id
    reopened = service.open_workspace(tmp_path / "a")
    assert reopened.workspace_id == first.workspace_id
