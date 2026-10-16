"""작업공간 저장소 — SQLite 정본, layout, 미디어 등록·해석(계획 §9.1·§9.2).

DB가 자산·작업 상태의 정본이고, 내부 경로는 **작업공간 루트 기준 상대 경로**로
저장한다(§9.2 — 디스크를 옮겨도 다시 연결된다). 미디어 제공은 등록된 ID로만
가능하다: 로그·설정·DB·API 응답 경로는 허용 디렉터리 밖이라 애초에 등록이
거부된다(§4.4). 허용 목록이 곧 보호다 — `jobs/`·`subtitles/`·`analysis/`는
브라우저 제공 대상이 아니다.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import secrets
import sqlite3
import stat as stat_module
import threading
import tomllib
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from vlogsrt import log
from vlogsrt.project.raw import read_scope, segmentation
from vlogsrt.project.raw.config import (
    WORKFLOW_SCHEMA_VERSION,
    RawWorkflowConfig,
    raw_workflow_config_from_json,
    raw_workflow_config_to_json,
)
from vlogsrt.project.raw.models import (
    KIND_PREFIXES,
    MEDIA_KINDS,
    MEDIA_STATUS_READY,
    MEDIA_SUFFIXES,
    SERVABLE_MEDIA_STATUSES,
    ClipArtifact,
    GlobalOrderEntry,
    GlobalOrderRevision,
    MediaNotAllowedError,
    MediaNotFoundError,
    MediaNotReadyError,
    MediaRegistration,
    MediaTargetChangedError,
    PackageError,
    PackageRevision,
    PlanNotFoundError,
    RawWorkspaceError,
    SegmentCoverage,
    SegmentPlanSnapshot,
    SourceAsset,
    SourceAssetDraft,
    SourceOrderEntry,
    SourceOrderRevision,
    WorkspaceAlreadyExistsError,
    WorkspaceNotFoundError,
    WorkspaceVersionError,
)
from vlogsrt.project.raw.package import next_package_dir

PROJECT_TOML_NAME = "project.toml"
DB_NAME = "project.sqlite3"
DB_SCHEMA_VERSION = "raw-workspace-db/6"
DB_SCHEMA_VERSION_V1 = "raw-workspace-db/1"
DB_SCHEMA_VERSION_V2 = "raw-workspace-db/2"
DB_SCHEMA_VERSION_V3 = "raw-workspace-db/3"
DB_SCHEMA_VERSION_V4 = "raw-workspace-db/4"
DB_SCHEMA_VERSION_V5 = "raw-workspace-db/5"
"""A1이 v1을, A2가 `source_assets`·`source_order_revisions`를 더한 v2를, A2F1이
`global_order_revisions`를 더한 v3을, A3이 분할 계획·조각·package 표를 더한 v4를,
A4가 의미 분석 리비전·원본별 결과 표를 더한 v5를 만들었다. v6은 검토·자막
작업공간 저장소(W3 `review_workspace`·W2 `caption_workspace`)의 덧붙이기 표를
중앙에서 한 트랜잭션으로 붙인다 — 두 모듈이 아직 없는 빌드에서는 그 문장들만
건너뛰고(저장소가 처음 붙는 자리에서 스스로 `init_schema`를 돌리므로 반쪽
스키마는 생기지 않는다). 덧붙이기만 한 옛 버전 열기는 한 트랜잭션으로 올리고
meta에 기록한다 — 이미 있는 설정·미디어·작업·작업공간 신원은 그대로다. 이
프로그램이 모르는 버전은 여전히 조용히 마이그레이션하지 않고 실패한다
(코디네이터 ACK)."""
WORKSPACE_ID_KEY = "workspace_id"
"""meta 표의 작업공간 신원 키 — 생성 때 한 번 정해지고 절대 바뀌지 않는다."""

CAPTURE_BASIS_KEY = "capture_time_basis"
"""meta 표의 촬영 시각 근거 판 — 그 판의 규칙으로 ready 원본을 전부 읽었을 때만 남는다(D53)."""

CAPTURE_TIME_BASIS = "creationdate-first/v1"
"""애플 촬영 날짜(`com.apple.quicktime.creationdate`)를 먼저, 없으면 컨테이너 `creation_time`.

이 표지가 없는 작업공간(D53 이전)의 `capture_time`은 FCP가 덮은 가져온 시각일 수 있다 —
앱 열기의 정리 작업이 원본 태그를 다시 읽어 고친 뒤에 남긴다.
"""


def open_db(path: Path) -> sqlite3.Connection:
    """작업공간 DB 연결 하나 — 닫는 것은 호출자 몫이다(`db_session`을 먼저 쓴다)."""
    connection = sqlite3.connect(path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
    except BaseException:
        # 준비 문장이 죽은 연결을 흘리면 fd가 새는 길이 된다 — 닫고 다시 던진다.
        connection.close()
        raise
    return connection


def enable_wal(connection: sqlite3.Connection) -> None:
    """작업공간 DB를 WAL 모드로 — 파일에 남는 설정이라 만들 때·열 때 한 번이면 된다(D63).

    롤백 저널에서는 분석 작업이 쓰는 동안 화면의 읽기가 `busy_timeout`만큼 기다렸다. WAL에서는
    읽기와 쓰기가 서로 막지 않는다. 다른 연결이 쥐고 있어 바꾸지 못하면 경고만 남기고 지금 모드로
    계속한다 — 바꾸지 못해도 정합성은 그대로다(다음 열기가 다시 시도한다).

    옆 파일(`-wal`·`-shm`)도 DB 정본이다 — 아직 체크포인트되지 않은 커밋이 `-wal`에 산다. DB를
    베끼려면 `sqlite3.Connection.backup`을 쓰거나 모든 연결을 닫은 뒤(마지막 연결이 체크포인트하고
    옆 파일을 지운다) 베낀다. 캐시 정리는 옆 파일을 보호한다(`cache_policy`).
    """
    try:
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.OperationalError as error:
        log.warn(f"작업공간 DB를 WAL로 바꾸지 못했다 — 지금 모드로 계속한다: {error}")
        return
    if row is None or str(row[0]).lower() != "wal":
        log.warn(f"작업공간 DB가 WAL을 받지 않았다 — 지금 모드 {row[0] if row else None!r}")


WAL_ANCHOR_LIMIT = 8
"""WAL 색인을 붙잡아 둘 작업공간 DB 수 — 오래 안 연 것부터 닫는다(D63). 앱은 작업공간 하나를 쓴다.

시험 한 벌은 작업공간 수천 개를 만들므로 상한이 fd(닻 하나에 DB·`-wal`·`-shm` 셋)를 묶는다."""

_wal_anchors: OrderedDict[Path, sqlite3.Connection] = OrderedDict()
_wal_anchor_lock = threading.Lock()


def anchor_wal(path: Path) -> None:
    """이 DB에 아무것도 하지 않는 연결 하나를 열어 둔다 — WAL 색인이 프로세스에 남게(D63).

    WAL에서는 마지막 연결이 닫힐 때 체크포인트하고 `-wal`·`-shm`을 지운다. 연결마다 짧게 여닫는
    이 저장소에서는 그래서 열기마다 색인을 새로 만들었다(실측 열기 하나 0.07 → 0.4ms, 그리기 한
    번에 열기 1600번 — 그리기가 3배 느려졌다). 닻은 트랜잭션을 쥐지 않는다 — 문장을 끝까지 읽고
    놓으므로 체크포인트를 막지 않는다.
    """
    key = path.expanduser().resolve()
    with _wal_anchor_lock:
        if key in _wal_anchors:
            _wal_anchors.move_to_end(key)
            return
    anchor = sqlite3.connect(key, timeout=10.0, check_same_thread=False)
    try:
        anchor.execute("SELECT count(*) FROM sqlite_master").fetchall()
    except BaseException:
        anchor.close()
        raise
    with _wal_anchor_lock:
        stale = [anchor] if key in _wal_anchors else []
        if not stale:
            _wal_anchors[key] = anchor
        while len(_wal_anchors) > WAL_ANCHOR_LIMIT:
            stale.append(_wal_anchors.popitem(last=False)[1])
    for connection in stale:
        connection.close()


def release_wal_anchors() -> None:
    """닻을 모두 닫는다 — 마지막 연결이면 SQLite가 체크포인트하고 옆 파일을 지운다."""
    with _wal_anchor_lock:
        anchors = list(_wal_anchors.values())
        _wal_anchors.clear()
    for connection in anchors:
        connection.close()


atexit.register(release_wal_anchors)


def _schema_version(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT value FROM meta WHERE key = 'db_schema_version'").fetchone()
    if row is None:
        raise WorkspaceVersionError("DB에 schema 버전이 없다")
    return str(row[0])


@contextmanager
def db_session(path: Path) -> Iterator[sqlite3.Connection]:
    """`with sqlite3.connect()`와 같은 커밋·롤백에 **닫기**를 더한다.

    sqlite3의 `with`는 닫지 않고, 연결은 문장 캐시와 순환 참조라 GC가 돌 때까지
    fd를 쥔다. 폴링하는 작업 여럿이 그 사이 fd 256을 다 써서 작업 스레드가
    `unable to open database file`로 줄줄이 죽었다.
    """
    connection = open_db(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def new_source_asset_id() -> str:
    """가져오기 자산의 전역 유일 불투명 ID — 미디어 ID와 같은 규칙(128비트)."""
    return f"source_{secrets.token_hex(8)}"


def new_order_revision_id() -> str:
    """원본 순서 리비전의 전역 유일 불투명 ID — 한 번 정해지면 불변이다."""
    return f"order_{secrets.token_hex(8)}"


def new_global_order_revision_id() -> str:
    """전체 순서 리비전의 전역 유일 불투명 ID — 배치 순서와 표기를 갈라 놓는다."""
    return f"gorder_{secrets.token_hex(8)}"


def new_segment_plan_revision_id() -> str:
    """분할 계획 리비전의 전역 유일 불투명 ID — 한 번 만들어지면 불변이다."""
    return f"splan_{secrets.token_hex(8)}"


def new_clip_id(entry_digest: str, package_dir: str, job_fingerprint: str) -> str:
    """조각의 전역 유일 ID — 조각 지문 **+ package 신원**에서 나온다.

    같은 계획을 다른 package(다른 프로파일·핸들)로 추출하면 조각 지문만으로는
    같은 ID가 나와 PRIMARY KEY가 부딪힌다(루트 재현: 같은 계획 MP4→MOV). ID는
    package_dir·작업 지문(계획+프로파일+핸들+도구)까지 묶는다 — 같은 package
    재개에서는 지문이 그대로라 ID도 그대로다(§6.3).
    """
    digest = hashlib.sha256(f"{package_dir}|{job_fingerprint}|{entry_digest}".encode()).hexdigest()
    return f"clip_{digest[:20]}"


def new_media_file_id() -> str:
    """전역 유일한 불투명 미디어 ID — 작업공간이 달라도 같은 ID가 나오지 않는다.

    예전의 `media_000001` 같은 작업공간 국소 번호는 두 프로젝트에서 같은 표기를
    가져, 서버가 다른 작업공간을 가리키는 순간 남의 파일로 조용히 이어졌다(루트
    재현). 128비트 무작위로 그 충돌을 원천적으로 없앤다 — 표기는 그대로 불투명하다.
    """
    return f"media_{secrets.token_hex(8)}"


def new_workspace_id() -> str:
    """작업공간 신원 — 화면의 '마지막으로 본 프로젝트' 검증에 쓰는 안정적인 토큰."""
    return f"ws_{secrets.token_hex(8)}"


WORKSPACE_LAYOUT: tuple[str, ...] = (
    "originals",
    "analysis",
    "clips",
    "previews",
    "review",
    "final",
    "subtitles",
    "cache",
    "jobs",
)

SERVABLE_MEDIA_PREFIXES: tuple[str, ...] = ("originals/", "previews/", "clips/", "final/")
"""미디어로 등록·제공할 수 있는 디렉터리 — 이 목록이 곧 보호 경계다(§4.4).

`jobs/`(실행 기록·설정 스냅샷), `subtitles/`(API 원응답 포함), `analysis/`
(전사 증거), `cache/`, `project.toml`, `project.sqlite3`은 브라우저 제공 대상이
아니므로 등록 자체가 거부된다.
"""

_SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE media_files (
    file_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_jobs_active ON jobs (kind, input_fingerprint, status);
"""

_INGEST_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS source_assets (
        asset_id TEXT PRIMARY KEY,
        batch_dir TEXT NOT NULL,
        relative_path TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        content_sha256 TEXT,
        file_id TEXT,
        duplicate_of TEXT,
        source_root TEXT NOT NULL,
        source_relative_path TEXT NOT NULL,
        source_name TEXT NOT NULL,
        capture_time TEXT,
        duration_seconds REAL,
        width INTEGER,
        height INTEGER,
        error TEXT,
        source_mtime_ns INTEGER NOT NULL,
        source_inode INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_assets_batch ON source_assets (batch_dir, ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_source_assets_hash ON source_assets (content_sha256, status)",
    """
    CREATE TABLE IF NOT EXISTS source_order_revisions (
        revision_id TEXT PRIMARY KEY,
        batch_dir TEXT NOT NULL UNIQUE,
        basis TEXT NOT NULL,
        entries_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS global_order_revisions (
        revision_id TEXT PRIMARY KEY,
        basis TEXT NOT NULL,
        entries_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

_A3_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS segment_plan_revisions (
        revision_id TEXT PRIMARY KEY,
        order_revision_id TEXT NOT NULL,
        basis TEXT NOT NULL,
        entries_json TEXT NOT NULL,
        coverage_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS package_revisions (
        package_dir TEXT PRIMARY KEY,
        plan_revision_id TEXT NOT NULL,
        order_revision_id TEXT NOT NULL,
        profile_name TEXT NOT NULL,
        handles_before TEXT NOT NULL,
        handles_after TEXT NOT NULL,
        job_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        clip_total INTEGER NOT NULL,
        clip_published INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clip_artifacts (
        clip_id TEXT PRIMARY KEY,
        package_dir TEXT NOT NULL,
        segment_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        segment_order INTEGER NOT NULL,
        profile_name TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        status TEXT NOT NULL,
        content_sha256 TEXT,
        size_bytes INTEGER,
        file_id TEXT,
        time_base_num INTEGER NOT NULL,
        time_base_den INTEGER NOT NULL,
        core_start_pts INTEGER NOT NULL,
        core_end_pts INTEGER NOT NULL,
        media_start_pts INTEGER NOT NULL,
        media_end_pts INTEGER NOT NULL,
        core_local_start TEXT NOT NULL,
        core_local_end TEXT NOT NULL,
        media_local_duration TEXT NOT NULL,
        frame_count INTEGER,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clip_artifacts_package"
    " ON clip_artifacts (package_dir, source_order, segment_order)",
)
"""A2·A2F1이 더한 표 — 가져오기 자산의 출처·검증 상태, 배치 불변 순서 리비전,
배치를 넘는 전체 불변 순서 리비전(§13.1). A3가 분할 계획 리비전(불변)·package
기록(예약이 곧 경합 경계)·조각 행(서버 소유 추출 상태)을 더했다. `entries_json`은
[{asset_id, ordinal, basis_note}]·[{asset_id, ordinal, batch_dir}] 배열의 정렬된
JSON이다. 문장 목록로 두는 이유는 하나의 트랜잭션 안에서 실행하기 위해서다 —
`executescript`는 먼저 commit하므로 마이그레이션의 원자성을 깬다."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _insert_source_asset(
    connection: sqlite3.Connection,
    batch_dir: str,
    draft: SourceAssetDraft,
    now: str,
) -> tuple[SourceAsset, SourceOrderEntry]:
    """자산 한 행과 순서 항목을 만든다 — 초안의 순번이 곧 확정 순번이다."""
    asset_id = new_source_asset_id()
    relative = f"originals/{batch_dir}/{draft.source_relative_path}"
    connection.execute(
        "INSERT INTO source_assets (asset_id, batch_dir, relative_path, status,"
        " size_bytes, source_root, source_relative_path, source_name, capture_time,"
        " duration_seconds, width, height, source_mtime_ns, source_inode, ordinal,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _source_asset_values(asset_id, batch_dir, relative, draft, now),
    )
    asset = _asset_from_draft(asset_id, batch_dir, relative, draft, now)
    entry = SourceOrderEntry(asset_id=asset_id, ordinal=draft.ordinal, basis_note=draft.basis_note)
    return asset, entry


def _source_asset_values(
    asset_id: str,
    batch_dir: str,
    relative: str,
    draft: SourceAssetDraft,
    now: str,
) -> tuple[object, ...]:
    """자산 한 행의 SQL 매개변수 — 초안의 출처 신원을 그대로 옮긴다."""
    return (
        asset_id,
        batch_dir,
        relative,
        draft.size_bytes,
        draft.source_root,
        draft.source_relative_path,
        draft.source_name,
        draft.capture_time,
        draft.duration_seconds,
        draft.width,
        draft.height,
        draft.source_mtime_ns,
        draft.source_inode,
        draft.ordinal,
        now,
        now,
    )


def _asset_from_draft(
    asset_id: str,
    batch_dir: str,
    relative: str,
    draft: SourceAssetDraft,
    now: str,
) -> SourceAsset:
    """삽입 직후의 자산 기록 — 아직 검증 전이므로 해시·등록은 비어 있다."""
    return SourceAsset(
        asset_id=asset_id,
        batch_dir=batch_dir,
        relative_path=relative,
        status="pending",
        size_bytes=draft.size_bytes,
        source_root=draft.source_root,
        source_relative_path=draft.source_relative_path,
        source_name=draft.source_name,
        content_sha256=None,
        file_id=None,
        duplicate_of=None,
        capture_time=draft.capture_time,
        duration_seconds=draft.duration_seconds,
        width=draft.width,
        height=draft.height,
        error=None,
        source_mtime_ns=draft.source_mtime_ns,
        source_inode=draft.source_inode,
        ordinal=draft.ordinal,
        created_at=now,
        updated_at=now,
    )


def _insert_order_revision(
    connection: sqlite3.Connection,
    batch_dir: str,
    basis: str,
    entries: list[SourceOrderEntry],
    now: str,
) -> str:
    """순서 리비전 한 행 — 만들기만 하고 고치는 길은 없다(불변 기록)."""
    revision_id = new_order_revision_id()
    connection.execute(
        "INSERT INTO source_order_revisions (revision_id, batch_dir, basis,"
        " entries_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            revision_id,
            batch_dir,
            basis,
            json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, sort_keys=True),
            now,
        ),
    )
    return revision_id


def _apply_ingest_schema(connection: sqlite3.Connection) -> None:
    """A2~A4·검토·자막 저장소 표를 (있으면 그대로 두고) 만든다.

    생성·마이그레이션이 같은 문장을 쓴다. A4 문장은 `analysis_store`에서, v6의
    검토·자막 문장은 각 저장소 모듈의 스키마 후크에서 가져온다(표와 문장이 한
    자리에 산다). 그 모듈들이 이 모듈을 다시 import하므로 위쪽 import가 아니라
    호출 시점 import로 순환을 끊는다. 저장소 모듈이 없는 빌드에서는 그 문장만
    건너뛴다 — 저장소가 처음 붙는 자리에서 스스로 `init_schema`를 돌리므로 v6
    작업공간이 반쪽 스키마로 남는 일은 없다.
    """
    from vlogsrt.project.raw.analysis_store import ANALYSIS_SCHEMA_STATEMENTS

    statements: tuple[str, ...] = (
        _INGEST_SCHEMA_STATEMENTS + _A3_SCHEMA_STATEMENTS + ANALYSIS_SCHEMA_STATEMENTS
    )
    for extra in _store_schema_statements():
        statements = statements + extra
    for statement in statements:
        connection.execute(statement)


def _store_schema_statements() -> tuple[tuple[str, ...], ...]:
    """검토·자막 저장소의 덧붙이기 문장 — **있는 모듈만** 가져온다.

    두 백엔드(W2·W3)는 아직 병합 전일 수 있다. 없는 모듈을 강제하면 이 파일이
    백엔드 병합을 기다리게 되므로, import 가능한 모듈의 후크만 모은다 — 전부
    `CREATE TABLE IF NOT EXISTS`라 없는 쪽을 건너뛰어도 기존 표는 절대 바뀌지
    않는다.
    """
    collected: list[tuple[str, ...]] = []
    for module_name in ("review_workspace_store", "caption_workspace_store"):
        try:
            module = __import__(f"vlogsrt.project.raw.{module_name}", fromlist=[module_name])
        except ImportError:
            continue
        if module_name == "review_workspace_store":
            collected.append(tuple(module.REVIEW_WORKSPACE_SCHEMA_STATEMENTS))
        else:
            # `caption_schema_sql()`은 여러 문장을 ';'로 이은 **한 덩어리 문자열**이다 —
            # 문장 tuple은 `CAPTION_SCHEMA_STATEMENTS`가 정본이다(문자열을 tuple()로
            # 감싸면 글자별로 쪼개진다).
            collected.append(tuple(module.CAPTION_SCHEMA_STATEMENTS))
    return tuple(collected)


def _clip_values(clip: ClipArtifact) -> tuple[object, ...]:
    """조각 한 행의 SQL 매개변수 — 신원은 계획에서, 상태는 서버에서 온 그대로."""
    return (
        clip.clip_id,
        clip.package_dir,
        clip.segment_id,
        clip.asset_id,
        clip.source_order,
        clip.segment_order,
        clip.profile_name,
        clip.relative_path,
        clip.status,
        clip.content_sha256,
        clip.size_bytes,
        clip.file_id,
        clip.time_base_num,
        clip.time_base_den,
        clip.core_start_pts,
        clip.core_end_pts,
        clip.media_start_pts,
        clip.media_end_pts,
        clip.core_local_start,
        clip.core_local_end,
        clip.media_local_duration,
        clip.frame_count,
        clip.error,
        clip.created_at,
        clip.updated_at,
    )


def _write_project_toml(root: Path, name: str, created_at: str) -> None:
    """작업공간 신원 문서 — schema·이름·생성 시각 세 가지뿐이다."""
    (root / PROJECT_TOML_NAME).write_bytes(
        tomli_w.dumps(
            {
                "schema_version": WORKFLOW_SCHEMA_VERSION,
                "name": name,
                "created_at": created_at,
            }
        ).encode("utf-8")
    )


class RawWorkspace:
    """열린 작업공간 하나. 생성·열기는 클래스 메서드로만 — 검사를 건너뛰는 길이 없다."""

    def __init__(self, root: Path, name: str, created_at: str) -> None:
        # 루트를 정규화해 둔다 — macOS의 /tmp ↔ /private/tmp처럼 같은 자리가 두
        # 표기로 오면 상대 경로 계산이 흔들린다.
        self.root = root.expanduser().resolve()
        self.name = name
        self.created_at = created_at

    # ── 생성·열기 ──────────────────────────────────────────────

    @classmethod
    def create(cls, root: Path, name: str) -> RawWorkspace:
        """빈 작업공간을 만든다. 이미 정본이 있으면 덮어쓰지 않고 실패한다."""
        if not name.strip():
            raise WorkspaceNotFoundError("프로젝트 이름이 비어 있다")
        root = root.expanduser()
        if (root / DB_NAME).exists() or (root / PROJECT_TOML_NAME).exists():
            raise WorkspaceAlreadyExistsError(f"이미 작업공간이 있다: {root}")
        existing = sorted(entry.name for entry in root.iterdir()) if root.is_dir() else []
        if existing:
            raise WorkspaceAlreadyExistsError(f"폴더가 비어 있지 않다: {root} (예: {existing[0]})")
        root.mkdir(parents=True, exist_ok=True)
        for directory in WORKSPACE_LAYOUT:
            (root / directory).mkdir()
        created_at = _now()
        _write_project_toml(root, name.strip(), created_at)
        workspace = cls(root, name.strip(), created_at)
        with workspace.connect() as connection:
            enable_wal(connection)  # 저널 모드는 트랜잭션 밖에서만 바뀐다 — 스키마보다 먼저
            connection.executescript(_SCHEMA_SQL)  # executescript는 먼저 commit한다
            _apply_ingest_schema(connection)
            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('db_schema_version', ?)",
                (DB_SCHEMA_VERSION,),
            )
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (WORKSPACE_ID_KEY, new_workspace_id()),
            )
            # 새 작업공간의 가져오기는 처음부터 지금 규칙으로 읽는다 — 되채울 것이 없다(D53).
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (CAPTURE_BASIS_KEY, CAPTURE_TIME_BASIS),
            )
            connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('workflow', ?, ?)",
                (raw_workflow_config_to_json(RawWorkflowConfig()), created_at),
            )
            connection.commit()
        anchor_wal(root / DB_NAME)
        return workspace

    @classmethod
    def open(cls, root: Path) -> RawWorkspace:
        """기존 작업공간을 검사하고 연다. schema/version이 다르면 실패한다.

        작업 복구는 여기서 하지 않는다 — 서버 시작 때 `jobs` 모듈의 명시적
        복구 호출 한 번으로만 일어난다(§14.1).
        """
        root = root.expanduser()
        if not (root / PROJECT_TOML_NAME).is_file() or not (root / DB_NAME).is_file():
            raise WorkspaceNotFoundError(f"v2 작업공간이 아니다: {root}")
        identity = cls._read_identity(root)
        workspace = cls(root, identity["name"], identity["created_at"])
        older = (
            DB_SCHEMA_VERSION_V1,
            DB_SCHEMA_VERSION_V2,
            DB_SCHEMA_VERSION_V3,
            DB_SCHEMA_VERSION_V4,
            DB_SCHEMA_VERSION_V5,
        )
        with workspace.connect() as connection:
            version = _schema_version(connection)
            if version != DB_SCHEMA_VERSION and version not in older:
                raise WorkspaceVersionError(
                    f"DB schema가 다르다: {version} (이 프로그램은 {DB_SCHEMA_VERSION})"
                )
            # 받아들인 DB만 — D63 이전 작업공간도 여기서 한 번 바뀐다(파일에 남는다). 버전 읽기와
            # 같은 연결이라 열기마다 연결을 하나 더 쓰지 않는다. 모르는 버전은 건드리지 않는다.
            enable_wal(connection)
        anchor_wal(root / DB_NAME)
        if version in older:
            workspace._upgrade_ingest_tables()
        return workspace

    def _upgrade_ingest_tables(self) -> None:
        """v1~v5 → 현재 열기 — 덧붙이기만 한 한 트랜잭션, 이미 있는 행은 그대로 둔다.

        이미 받아들인 옛 작업공간을 버리지 않기 위한 유일한 마이그레이션이다.
        없는 표를 만들고 meta의 버전을 올리는 것 외에 아무 행도 고치지 않는다 —
        설정·미디어·작업·작업공간 신원이 그대로 보존된다. 그 밖의 버전 차이는
        `WorkspaceVersionError`로 계속 실패한다(조용한 마이그레이션 금지).
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _apply_ingest_schema(connection)
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'db_schema_version'",
                (DB_SCHEMA_VERSION,),
            )
            connection.commit()

    @staticmethod
    def _read_identity(root: Path) -> dict[str, str]:
        try:
            with (root / PROJECT_TOML_NAME).open("rb") as handle:
                identity = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise WorkspaceNotFoundError(f"project.toml을 읽을 수 없다: {error}") from error
        if identity.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
            raise WorkspaceVersionError(
                f"workflow schema가 다르다: {identity.get('schema_version')!r} "
                f"(이 프로그램은 {WORKFLOW_SCHEMA_VERSION})"
            )
        if not isinstance(identity.get("name"), str) or not isinstance(
            identity.get("created_at"), str
        ):
            raise WorkspaceNotFoundError("project.toml에 이름·생성 시각이 없다")
        return {
            "name": str(identity["name"]),
            "created_at": str(identity["created_at"]),
        }

    def db_schema_version(self) -> str:
        with self.connect() as connection:
            return _schema_version(connection)

    def workspace_id(self) -> str:
        """이 작업공간의 불변 신원 — 화면의 선택 검증이 이 토큰을 믿는다.

        이 fix 이전에 만들어진 작업공간에는 없다 — 처음 물은 한 번만 정해져 이후
        불변이다(경합에서도 INSERT OR IGNORE가 한 쪽만 살린다). 불변이라 읽기 범위(그리기 한 번)
        안에서는 한 번만 묻는다(D63).
        """
        return read_scope.memo(("workspace_id", str(self.root)), self._read_workspace_id)

    def _read_workspace_id(self) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (WORKSPACE_ID_KEY,)
            ).fetchone()
            if row is None:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (WORKSPACE_ID_KEY, new_workspace_id()),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = ?", (WORKSPACE_ID_KEY,)
                ).fetchone()
        assert row is not None
        return str(row[0])

    def meta_value(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    # ── 연결 ───────────────────────────────────────────────────

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = open_db(self.root / DB_NAME)
        try:
            yield connection
        finally:
            connection.close()

    # ── 설정 ───────────────────────────────────────────────────

    def load_workflow(self) -> RawWorkflowConfig:
        """저장된 설정을 검증까지 마쳐서 돌려준다 — 왕복이 곧 계약이다(A0 §9.1).

        읽기 범위(그리기 한 번) 안에서는 한 번만 읽는다(D63) — 설정은 얼린 값이다."""
        return read_scope.memo(("load_workflow", str(self.root)), self._read_workflow)

    def _read_workflow(self) -> RawWorkflowConfig:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = 'workflow'").fetchone()
        if row is None:
            raise WorkspaceNotFoundError("DB에 workflow 설정이 없다")
        return raw_workflow_config_from_json(str(row[0]))

    def save_workflow(self, config: RawWorkflowConfig) -> None:
        """설정을 문장 하나로 갈아끼운다 — 읽는 쪽은 언제나 전체 지문을 본다."""
        payload = raw_workflow_config_to_json(config)
        raw_workflow_config_from_json(payload)  # 저장 직전 왕복 검증
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('workflow', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (payload, _now()),
            )
            connection.commit()

    # ── 미디어 등록·해석 ───────────────────────────────────────

    def register_media_file(self, path: Path, kind: str) -> MediaRegistration:
        """작업공간 안의 실제 파일을 미디어로 등록하고 브라우저용 ID를 만든다.

        허용 디렉터리 밖(로그·설정·DB·API 응닷 자리)과 심볼릭 링크를 거친 경로는
        등록 단계에서 거부한다. 제공 계층은 ID만 다루므로, 등록을 통과한 파일만
        브라우저에 보일 수 있다.
        """
        if kind not in MEDIA_KINDS:
            raise MediaNotAllowedError(f"알 수 없는 미디어 종류: {kind}")
        relative = self._registrable_relative(path, kind)
        target = self.root / relative
        probed = target.lstat()
        registered_at = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            clash = connection.execute(
                "SELECT file_id FROM media_files WHERE relative_path = ?", (relative,)
            ).fetchone()
            if clash is not None:
                connection.rollback()
                raise MediaNotAllowedError(f"이미 등록된 경로다: {relative} ({clash[0]})")
            file_id = self._insert_media_row(connection, kind, relative, probed, registered_at)
            connection.commit()
        return MediaRegistration(
            file_id=file_id,
            kind=kind,
            relative_path=relative,
            status=MEDIA_STATUS_READY,
            size_bytes=probed.st_size,
            registered_at=registered_at,
        )

    @staticmethod
    def _insert_media_row(
        connection: sqlite3.Connection,
        kind: str,
        relative: str,
        probed: os.stat_result,
        registered_at: str,
    ) -> str:
        """전역 유일 ID 뽑아 한 행 넣기 — 128비트 충돌은 다시 뽑는다(사실상 없다)."""
        for _ in range(3):
            file_id = new_media_file_id()
            try:
                connection.execute(
                    "INSERT INTO media_files "
                    "(file_id, kind, relative_path, status, size_bytes, mtime_ns, inode, "
                    "registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        file_id,
                        kind,
                        relative,
                        MEDIA_STATUS_READY,
                        probed.st_size,
                        probed.st_mtime_ns,
                        probed.st_ino,
                        registered_at,
                    ),
                )
                return file_id
            except sqlite3.IntegrityError:
                connection.rollback()
                connection.execute("BEGIN IMMEDIATE")
        raise MediaNotAllowedError("미디어 ID를 만들지 못했다 — 잠시 뒤 다시 등록하라")

    def _registrable_relative(self, path: Path, kind: str) -> str:
        """등록 가능한 상대 경로인가 — 어휘 검사 → 링크 검사 → 파일 검사 순서로 가른다.

        어휘 단계(파일시스템 조회 없음)가 제일 앞에 선다: 허용 디렉터리, 숨김·
        자격 증명 철자, 미디어 확장자, 종류↔자리 짝. 그 다음에 루트부터 각 칸을
        lstat해 심볼릭 링크를 끊고, 마지막에 실제 파일인지 본다. 보호 철자는
        stat조차 일으키지 않는다(코디네이터 재결).
        """
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        relative = self._relative_within_root(Path(os.path.normpath(candidate)))
        if relative is None:
            raise MediaNotAllowedError(f"작업공간 밖 경로는 등록할 수 없다: {path}")
        if not relative.startswith(SERVABLE_MEDIA_PREFIXES):
            raise MediaNotAllowedError(
                f"미디어는 {', '.join(SERVABLE_MEDIA_PREFIXES)} 아래만 등록할 수 있다: {relative}"
            )
        if not relative.startswith(KIND_PREFIXES[kind]):
            raise MediaNotAllowedError(
                f"{kind} 미디어는 {', '.join(KIND_PREFIXES[kind])} 아래에 있어야 한다: {relative}"
            )
        parts = relative.split("/")
        if any(part.startswith(".") for part in parts):
            raise MediaNotAllowedError(f"숨김 항목은 등록할 수 없다: {relative}")
        if Path(relative).suffix.lower() not in MEDIA_SUFFIXES:
            raise MediaNotAllowedError(
                f"미디어 확장자가 아니다({Path(relative).suffix or '확장자 없음'}): {relative}"
            )
        target = self.root / relative
        if not self._free_of_symlinks(target):
            raise MediaNotAllowedError(f"심볼릭 링크를 거쳐 등록할 수 없다: {relative}")
        if not target.is_file():
            raise MediaNotAllowedError(f"등록 대상이 실제 파일이 아니다: {relative}")
        return relative

    def _relative_within_root(self, candidate: Path) -> str | None:
        """정규화된 경로가 루트 안인가 — `..`가 남거나 밖으로 나가면 None.

        여기선 링크를 따라가지 않는다(표기만 본다). 조상 표기가 macOS의
        /tmp ↔ /private/tmp처럼 어긋날 때만 루트 위쪽을 realpath로 맞춘 뒤 다시
        본다 — 그 재시도에서 안쪽 링크가 풀리면 저장 경로가 실제 이름으로
        정규화될 뿐, 보호·확장자·지문 검사는 그대로 지난다.
        """
        relative = self._spelled_relative(candidate)
        if relative is None:
            relative = self._spelled_relative(Path(os.path.realpath(candidate)))
        return relative

    def _spelled_relative(self, candidate: Path) -> str | None:
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            return None
        parts = relative.parts
        if not parts or any(part == ".." for part in parts):
            return None
        return relative.as_posix()

    def _free_of_symlinks(self, target: Path) -> bool:
        """루트 아래 각 칸이 전부 실제 이름인가 — 한 칸이라도 링크면 거짓.

        없는 칸은 여기서 말하지 않는다(존재 검사는 호출자의 몫) — FileNotFoundError는
        '링크 아님'으로 넘겨 바깥의 안내 문장이 나오게 한다.
        """
        try:
            relative = target.relative_to(self.root)
        except ValueError:
            return False
        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                return True
            if stat_module.S_ISLNK(mode):
                return False
        return True

    def media_registration(self, file_id: str) -> MediaRegistration:
        row = self._media_row(file_id)
        return self._registration(row)

    def _media_row(self, file_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_files WHERE file_id = ?", (file_id,)
            ).fetchone()
        if row is None:
            raise MediaNotFoundError(f"없는 미디어 ID: {file_id}")
        return row

    @staticmethod
    def _registration(row: sqlite3.Row) -> MediaRegistration:
        return MediaRegistration(
            file_id=str(row["file_id"]),
            kind=str(row["kind"]),
            relative_path=str(row["relative_path"]),
            status=str(row["status"]),
            size_bytes=int(row["size_bytes"]),
            registered_at=str(row["registered_at"]),
        )

    def list_media(self, kind: str | None = None) -> tuple[MediaRegistration, ...]:
        """등록 순서대로 — ID가 무작위라 정렬 근거는 삽입 행(rowid)이다. 읽기 범위 안에서는 종류마다
        한 번만 읽는다(D63)."""
        return read_scope.memo(("list_media", str(self.root), kind), lambda: self._read_media(kind))

    def _read_media(self, kind: str | None) -> tuple[MediaRegistration, ...]:
        query = "SELECT * FROM media_files"
        params: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE kind = ?"
            params = (kind,)
        query += " ORDER BY rowid"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._registration(row) for row in rows)

    def media_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0])

    def resolve_media_file(self, file_id: str) -> tuple[Path, int, str]:
        """ID → (실제 경로, 크기, 상태). 등록 때의 지문과 어긋나면 거부한다.

        바뀐 대상을 조용히 다시 제공하지 않는다 — 다시 등록하라고 알린다(§4.4).
        지문은 size·mtime이다: inode는 같은 파일이 디스크를 옮기면 변하므로(§9.2
        — 작업공간은 통째로 옮겨도 다시 열려야 한다) 여기에 넣지 않는다. 내용
        검증은 계층마다 따로 산다 — 원본은 가져오기·계획의 SHA-256, 조각은 게시
        때 찍은 해시의 재검증이다.
        """
        if file_id.startswith("proxy_"):
            from vlogsrt.project.raw.media_refs import resolve_direct

            return resolve_direct(self, file_id)
        row = self._media_row(file_id)
        if str(row["status"]) not in SERVABLE_MEDIA_STATUSES:
            raise MediaNotReadyError(f"아직 준비되지 않은 미디어다: {file_id}")
        relative = str(row["relative_path"])
        target = self.root / relative
        if not target.exists():
            raise MediaNotFoundError(f"등록된 파일이 없다: {file_id}")
        probed = target.lstat()
        fingerprint_changed = probed.st_size != int(row["size_bytes"]) or probed.st_mtime_ns != int(
            row["mtime_ns"]
        )
        if fingerprint_changed or not self._free_of_symlinks(target):
            raise MediaTargetChangedError(
                f"등록된 자리의 파일이 바뀌었다: {file_id} — 다시 등록해야 한다"
            )
        return target, probed.st_size, str(row["kind"])

    # ── A2 — 가져오기 자산·원본 순서 리비전 ─────────────────────

    def create_ingest_batch(
        self, drafts: tuple[SourceAssetDraft, ...], basis: str
    ) -> tuple[tuple[SourceAsset, ...], SourceOrderRevision]:
        """확정된 초안들로 배치 하나를 연다 — 한 트랜잭션에 자산·순서 리비전까지.

        `batch_dir`은 이 트랜잭션 안에서 다음 번호로 정한다(경합에서 두 배치가
        같은 디렉터리를 받지 않는다). 자산은 확정 순번(ordinal) 오름차순으로
        들어가고, 순서 리비전은 같은 트랜잭션에서 **불변 기록**으로 남는다.
        """
        if not drafts:
            raise RawWorkspaceError("확정할 자산이 없다")
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch_dir = self._next_batch_dir(connection)
            assets: list[SourceAsset] = []
            entries: list[SourceOrderEntry] = []
            for draft in sorted(drafts, key=lambda item: item.ordinal):
                asset, entry = _insert_source_asset(connection, batch_dir, draft, now)
                assets.append(asset)
                entries.append(entry)
            revision_id = _insert_order_revision(connection, batch_dir, basis, entries, now)
            connection.commit()
        return tuple(assets), SourceOrderRevision(
            revision_id=revision_id,
            batch_dir=batch_dir,
            basis=basis,
            entries=tuple(entries),
            created_at=now,
        )

    @staticmethod
    def _next_batch_dir(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT batch_dir FROM source_order_revisions ORDER BY batch_dir DESC LIMIT 1"
        ).fetchone()
        number = 1
        if row is not None:
            _, _, tail = str(row[0]).rpartition("_")
            number = int(tail) + 1 if tail.isdigit() else 1
        return f"import_{number:06d}"

    def mark_source_reference(self, asset_id: str) -> None:
        """Mark an explicit external original; never copy, move or write its bytes."""
        asset = self.source_asset(asset_id)
        path = self._reference_target(asset)
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS source_references ("
                "asset_id TEXT PRIMARY KEY, absolute_path TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO source_references VALUES (?, ?)", (asset_id, str(path))
            )
            connection.commit()

    def source_reference_path(self, asset_id: str) -> str | None:
        """원본 참조 경로 — 읽기 범위 안에서는 참조 표를 한 번에 읽는다(D63)."""
        scope = read_scope.current()
        if scope is not None:
            key = ("source_references", str(self.root))
            return scope.memo(key, self._read_reference_paths).get(asset_id)
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_references'"
            ).fetchone()
            if exists is None:
                return None
            row = connection.execute(
                "SELECT absolute_path FROM source_references WHERE asset_id=?", (asset_id,)
            ).fetchone()
        return str(row[0]) if row is not None else None

    def source_media_path(self, asset: SourceAsset) -> Path:
        """Resolve copied and explicitly referenced originals through one checked boundary."""
        referenced = self.source_reference_path(asset.asset_id)
        if referenced is None:
            return self.root / asset.relative_path
        expected = Path(asset.source_root) / asset.source_relative_path
        if str(expected) != referenced:
            raise RawWorkspaceError(f"원본 참조 기록이 맞지 않습니다: {asset.source_name}")
        try:
            path = self._reference_target(asset)
        except (OSError, RawWorkspaceError) as error:
            raise RawWorkspaceError(
                f"원본 연결을 확인하세요: {asset.source_name} · {error}"
            ) from error
        return path

    @staticmethod
    def _reference_target(asset: SourceAsset) -> Path:
        from vlogsrt.project.raw.fcp import _resolve_path_safely
        from vlogsrt.project.raw.ingest import _source_stat

        expected = Path(asset.source_root) / asset.source_relative_path
        target, reason, facts = _resolve_path_safely(expected)
        if reason or facts is None or target != expected:
            raise RawWorkspaceError(reason or "원본 경로의 연결 대상이 바뀌었습니다")
        _source_stat(target, asset)
        return target

    def _read_reference_paths(self) -> dict[str, str]:
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_references'"
            ).fetchone()
            if exists is None:
                return {}
            rows = connection.execute(
                "SELECT asset_id, absolute_path FROM source_references"
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def source_asset(self, asset_id: str) -> SourceAsset:
        """원본 하나 — 읽기 범위 안에서는 전체 목록 한 번에서 찾는다(같은 행·같은 값, D63)."""
        if read_scope.current() is not None:
            by_id = read_scope.memo(
                ("source_assets_by_id", str(self.root)),
                lambda: {asset.asset_id: asset for asset in self.list_source_assets()},
            )
            if asset_id not in by_id:
                raise RawWorkspaceError(f"없는 자산 ID: {asset_id}")
            return by_id[asset_id]
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        if row is None:
            raise RawWorkspaceError(f"없는 자산 ID: {asset_id}")
        return self._source_asset(row)

    def list_source_assets(self, batch_dir: str | None = None) -> tuple[SourceAsset, ...]:
        """배치(없으면 전체)의 자산 — 전체일 땐 배치 연대순, 배치 안은 확정 순번순.

        전체 목록이 `ordinal`만으로 정렬되면 모든 배치의 1번이 무작위 자산 ID로
        뒤섞인다(루트 지적). 배치 디렉터리(가져온 순서)를 앞에 두고, 그 안에서만
        사람이 확정한 순번을 지킨다 — 배치 안 사용자 순서는 그대로다. 읽기 범위 안에서는 배치마다
        한 번만 읽는다(D63).
        """
        key = ("list_source_assets", str(self.root), batch_dir)
        return read_scope.memo(key, lambda: self._read_source_assets(batch_dir))

    def _read_source_assets(self, batch_dir: str | None) -> tuple[SourceAsset, ...]:
        query = "SELECT * FROM source_assets"
        params: tuple[str, ...] = ()
        if batch_dir is not None:
            query += " WHERE batch_dir = ?"
            params = (batch_dir,)
        query += " ORDER BY batch_dir, ordinal, asset_id"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._source_asset(row) for row in rows)

    def update_source_asset(self, asset_id: str, **changes: object) -> SourceAsset:
        """가져오기 상태 갱신 — 상태·검증 필드만 고친다. 출처 신원은 불변이다.

        `content_sha256`은 **검증 이력 표식**이다: 한번 기록되면 못 지우고 못
        바꾼다 — 상태가 어떻게 오가든(실패·재검증 실패) 검증된 적 있다는 사실이
        남아야, 어떤 재시도도 검증된 원본을 다시 쓰는 길로 빠지지 않는다(루트 재현).
        """
        allowed = {
            "status",
            "content_sha256",
            "file_id",
            "duplicate_of",
            "error",
        }
        illegal = set(changes) - allowed
        if illegal:
            raise RawWorkspaceError(f"자산의 이 필드는 못 고친다: {sorted(illegal)}")
        self._refuse_hash_removal(asset_id, changes)
        columns = ", ".join(f"{name} = ?" for name in changes)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE source_assets SET {columns}, updated_at = ? WHERE asset_id = ?",
                (*changes.values(), _now(), asset_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise RawWorkspaceError(f"없는 자산 ID: {asset_id}")
        return self.source_asset(asset_id)

    def record_capture_times(
        self, changes: Mapping[str, str | None], *, basis: str | None = None
    ) -> None:
        """촬영 시각 근거만 고친다 — 잰 근거의 정정이지 출처 신원이 아니다(D53).

        같은 원본 태그를 지금 규칙(애플 촬영 날짜 우선)으로 다시 읽은 값이다. 전부 한
        트랜잭션이다 — 반쯤 고친 시각으로 순서가 확정되지 않게, 없는 자산이 섞이면 아무것도
        쓰지 않는다. `basis`를 주면 같은 트랜잭션에서 근거 판 표지를 남긴다. 배치 안 순서
        리비전의 근거 문장은 불변 기록이라 그대로 둔다 — 전체 순서는 새 리비전이 맡는다.
        """
        updated_at = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for asset_id, capture_time in changes.items():
                cursor = connection.execute(
                    "UPDATE source_assets SET capture_time = ?, updated_at = ? WHERE asset_id = ?",
                    (capture_time, updated_at, asset_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise RawWorkspaceError(f"없는 자산 ID: {asset_id}")
            if basis is not None:
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (CAPTURE_BASIS_KEY, basis),
                )
            connection.commit()

    def _refuse_hash_removal(self, asset_id: str, changes: dict[str, object]) -> None:
        """검증 이력 표식 보호 — 기록된 해시를 지우거나 바꾸는 갱신은 거절한다."""
        if "content_sha256" not in changes:
            return
        current = self.source_asset(asset_id)
        if (
            current.content_sha256 is not None
            and changes["content_sha256"] != current.content_sha256
        ):
            raise RawWorkspaceError(
                f"한번 검증된 자산의 내용 해시는 못 바꾼다: {asset_id} "
                f"({current.content_sha256[:12]}… → {changes['content_sha256']!r})"
                " — 다시 가져오려면 새 가져오기(새 배치)를 만들어라"
            )

    def first_ready_asset_with_hash(self, content_sha256: str, exclude_asset_id: str) -> str | None:
        """같은 내용을 가진 먼저 검증된 자산 — 중복 출처 표시용(§5.2)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT asset_id FROM source_assets WHERE content_sha256 = ?"
                " AND status = 'ready' AND asset_id != ?"
                " ORDER BY created_at, asset_id LIMIT 1",
                (content_sha256, exclude_asset_id),
            ).fetchone()
        return None if row is None else str(row[0])

    def order_revision(self, batch_dir: str) -> SourceOrderRevision | None:
        """그 배치의 순서 리비전 — 배치마다 정확히 하나(불변)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_order_revisions WHERE batch_dir = ?", (batch_dir,)
            ).fetchone()
        if row is None:
            return None
        entries = [
            SourceOrderEntry(
                asset_id=str(entry["asset_id"]),
                ordinal=int(entry["ordinal"]),
                basis_note=str(entry["basis_note"]),
            )
            for entry in json.loads(str(row["entries_json"]))
        ]
        return SourceOrderRevision(
            revision_id=str(row["revision_id"]),
            batch_dir=str(row["batch_dir"]),
            basis=str(row["basis"]),
            entries=tuple(sorted(entries, key=lambda entry: entry.ordinal)),
            created_at=str(row["created_at"]),
        )

    def list_order_revisions(self) -> tuple[SourceOrderRevision, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT batch_dir FROM source_order_revisions ORDER BY batch_dir"
            ).fetchall()
        revisions = [self.order_revision(str(row["batch_dir"])) for row in rows]
        return tuple(revision for revision in revisions if revision is not None)

    # ── A2F1 — 배치를 넘는 전체 순서 리비전(불변) ───────────────

    def create_global_order_revision(
        self, entries: tuple[GlobalOrderEntry, ...], basis: str
    ) -> GlobalOrderRevision:
        """전체 순서 리비전 한 행 — 만들기만 한다. 고치는 길은 없다(불변 기록)."""
        if not entries:
            raise RawWorkspaceError("확정할 전체 순서 항목이 없다")
        ordered = tuple(sorted(entries, key=lambda entry: entry.ordinal))
        revision_id = new_global_order_revision_id()
        created_at = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO global_order_revisions (revision_id, basis, entries_json,"
                " created_at) VALUES (?, ?, ?, ?)",
                (
                    revision_id,
                    basis,
                    json.dumps(
                        [asdict(entry) for entry in ordered], ensure_ascii=False, sort_keys=True
                    ),
                    created_at,
                ),
            )
            connection.commit()
        return GlobalOrderRevision(
            revision_id=revision_id,
            basis=basis,
            entries=ordered,
            created_at=created_at,
        )

    def latest_global_order_revision(self) -> GlobalOrderRevision | None:
        """가장 최근의 전체 순서 리비전 — 삽입 역순(rowid), 옛 것은 그대로 산다."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM global_order_revisions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._global_order_revision(row)

    def list_global_order_revisions(self) -> tuple[GlobalOrderRevision, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM global_order_revisions ORDER BY rowid"
            ).fetchall()
        return tuple(self._global_order_revision(row) for row in rows)

    @staticmethod
    def _global_order_revision(row: sqlite3.Row) -> GlobalOrderRevision:
        entries = [
            GlobalOrderEntry(
                asset_id=str(entry["asset_id"]),
                ordinal=int(entry["ordinal"]),
                batch_dir=str(entry["batch_dir"]),
            )
            for entry in json.loads(str(row["entries_json"]))
        ]
        return GlobalOrderRevision(
            revision_id=str(row["revision_id"]),
            basis=str(row["basis"]),
            entries=tuple(sorted(entries, key=lambda entry: entry.ordinal)),
            created_at=str(row["created_at"]),
        )

    def refresh_media_fingerprint(self, file_id: str) -> None:
        """등록 행의 지문만 다시 찍는다 — ID·경로는 그대로(A2F1 회복 전용).

        검증됐던 원본이 변조 뒤 원래 내용으로 돌아오면(heal) 등록 때 찍어둔
        size·mtime·inode는 이미 어긋나 있다. 행을 새로 만들면 file_id가 바뀌므로,
        **같은 ID로** 현재 파일의 지문을 다시 박는다 — 이후 다른 변조는 여전히
        어긋나는 지문으로 잡힌다(제공 안전망은 그대로).
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT relative_path FROM media_files WHERE file_id = ?", (file_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise MediaNotFoundError(f"없는 미디어 ID: {file_id}")
            target = self.root / str(row["relative_path"])
            probed = target.lstat()
            connection.execute(
                "UPDATE media_files SET size_bytes = ?, mtime_ns = ?, inode = ? WHERE file_id = ?",
                (probed.st_size, probed.st_mtime_ns, probed.st_ino, file_id),
            )
            connection.commit()

    def discard_media_registration(self, relative_path: str) -> bool:
        """그 상대 경로의 미디어 등록 행을 지운다 — 검증 전 표기의 회복 전용.

        복사는 성공해 등록까지 됐는데 자산 표시가 `ready`로 못 남은 창을 닫는
        길이다. 자산 장부(source_assets)가 정본이므로 장부와 어긋난 등록 행은
        다음 시도에서 다시 만들어진다. 파일은 지우지 않는다 — 호출자의 몫이다.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM media_files WHERE relative_path = ?", (relative_path,)
            )
            connection.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _source_asset(row: sqlite3.Row) -> SourceAsset:
        return SourceAsset(
            asset_id=str(row["asset_id"]),
            batch_dir=str(row["batch_dir"]),
            relative_path=str(row["relative_path"]),
            status=str(row["status"]),
            size_bytes=int(row["size_bytes"]),
            source_root=str(row["source_root"]),
            source_relative_path=str(row["source_relative_path"]),
            source_name=str(row["source_name"]),
            content_sha256=None if row["content_sha256"] is None else str(row["content_sha256"]),
            file_id=None if row["file_id"] is None else str(row["file_id"]),
            duplicate_of=None if row["duplicate_of"] is None else str(row["duplicate_of"]),
            capture_time=None if row["capture_time"] is None else str(row["capture_time"]),
            duration_seconds=None
            if row["duration_seconds"] is None
            else float(row["duration_seconds"]),
            width=None if row["width"] is None else int(row["width"]),
            height=None if row["height"] is None else int(row["height"]),
            error=None if row["error"] is None else str(row["error"]),
            source_mtime_ns=int(row["source_mtime_ns"]),
            source_inode=int(row["source_inode"]),
            ordinal=int(row["ordinal"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    # ── A3 — 분할 계획 리비전(불변) ─────────────────────────────

    def insert_segment_plan_revision(self, snapshot: SegmentPlanSnapshot) -> None:
        """분할 계획 리비전 한 행 — 만들기만 한다. 고치는 길은 없다(§6.5).

        조각·덮음 증명은 값 그대로 JSON으로 묻는다(§9.2 — DB는 위치와 지문).
        같은 리비전 ID의 두 번 삽입은 어김없이 실패한다.
        """
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO segment_plan_revisions (revision_id, order_revision_id, basis,"
                " entries_json, coverage_json, fingerprint, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.revision_id,
                    snapshot.order_revision_id,
                    snapshot.basis,
                    segmentation.snapshot_to_json(snapshot.entries),
                    json.dumps(
                        [asdict(item) for item in snapshot.coverage],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    snapshot.fingerprint,
                    snapshot.created_at,
                ),
            )
            connection.commit()

    def segment_plan_revision(self, revision_id: str) -> SegmentPlanSnapshot:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM segment_plan_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        if row is None:
            raise PlanNotFoundError(f"없는 분할 계획 리비전: {revision_id}")
        return SegmentPlanSnapshot(
            revision_id=str(row["revision_id"]),
            order_revision_id=str(row["order_revision_id"]),
            basis=str(row["basis"]),
            entries=segmentation.snapshot_from_json(str(row["entries_json"])),
            coverage=tuple(
                SegmentCoverage(**entry) for entry in json.loads(str(row["coverage_json"]))
            ),
            fingerprint=str(row["fingerprint"]),
            created_at=str(row["created_at"]),
        )

    def list_segment_plan_revisions(self) -> tuple[SegmentPlanSnapshot, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT revision_id FROM segment_plan_revisions ORDER BY rowid"
            ).fetchall()
        snapshots = [self.segment_plan_revision(str(row[0])) for row in rows]
        return tuple(snapshots)

    # ── A3 — package 예약·갱신(경합 경계) ────────────────────────

    def reserve_package(
        self,
        *,
        plan_revision_id: str,
        order_revision_id: str,
        profile_name: str,
        handles_before: str,
        handles_after: str,
        job_fingerprint: str,
        clip_total: int,
    ) -> PackageRevision:
        """package 디렉터리를 한 트랜잭션으로 예약한다 — 같은 자리는 두 벌이 안 생긴다.

        다음 번호를 읽고 삽입하는 사이를 `BEGIN IMMEDIATE`로 막는다: 동시에 온 두
        추출이 같은 `package_000001`을 받지 않는다. 이미 있는 package_dir(재시도)은
        같은 계획·같은 지문일 때 그 행을 그대로 돌려준다 — 어긋나면 예약 실패다.
        """
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_dirs = [
                str(row[0])
                for row in connection.execute(
                    "SELECT package_dir FROM package_revisions ORDER BY package_dir"
                ).fetchall()
            ]
            package_dir = next_package_dir(existing_dirs)
            try:
                connection.execute(
                    "INSERT INTO package_revisions (package_dir, plan_revision_id,"
                    " order_revision_id, profile_name, handles_before, handles_after,"
                    " job_fingerprint, status, clip_total, clip_published, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, 0, ?, ?)",
                    (
                        package_dir,
                        plan_revision_id,
                        order_revision_id,
                        profile_name,
                        handles_before,
                        handles_after,
                        job_fingerprint,
                        clip_total,
                        now,
                        now,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise PackageError(
                    "package 예약이 경합에 걸렸다 — 잠시 뒤 다시 시도하거나 진행 중"
                    " 작업 상태를 확인하세요"
                ) from None
        return self.package_revision(package_dir)  # type: ignore[return-value]

    def update_package(self, package_dir: str, **changes: object) -> PackageRevision:
        """package 행의 서버 소유 상태 갱신 — 상태·게시 수·오류만 고친다."""
        allowed = {"status", "clip_published", "error"}
        illegal = set(changes) - allowed
        if illegal:
            raise RawWorkspaceError(f"package의 이 필드는 못 고친다: {sorted(illegal)}")
        columns = ", ".join(f"{name} = ?" for name in changes)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE package_revisions SET {columns}, updated_at = ? WHERE package_dir = ?",
                (*changes.values(), _now(), package_dir),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise PackageError(f"없는 package: {package_dir}")
        revision = self.package_revision(package_dir)
        assert revision is not None
        return revision

    def package_revision(self, package_dir: str) -> PackageRevision | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM package_revisions WHERE package_dir = ?", (package_dir,)
            ).fetchone()
        return None if row is None else self._package_revision(row)

    def list_package_revisions(self) -> tuple[PackageRevision, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT package_dir FROM package_revisions ORDER BY package_dir"
            ).fetchall()
        revisions = [self.package_revision(str(row[0])) for row in rows]
        return tuple(revision for revision in revisions if revision is not None)

    @staticmethod
    def _package_revision(row: sqlite3.Row) -> PackageRevision:
        return PackageRevision(
            package_dir=str(row["package_dir"]),
            plan_revision_id=str(row["plan_revision_id"]),
            order_revision_id=str(row["order_revision_id"]),
            profile_name=str(row["profile_name"]),
            handles_before=str(row["handles_before"]),
            handles_after=str(row["handles_after"]),
            job_fingerprint=str(row["job_fingerprint"]),
            status=str(row["status"]),
            clip_total=int(row["clip_total"]),
            clip_published=int(row["clip_published"]),
            error=None if row["error"] is None else str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    # ── A3 — 조각 행(서버 소유 추출 상태) ────────────────────────

    def ensure_clip_row(self, clip: ClipArtifact) -> ClipArtifact:
        """조각 행을 (있으면 그대로 두고) 만든다 — 재개가 같은 ID·번호를 유지하는 길.

        clip_id는 계획 지문에서 유도되므로, 같은 계획을 다시 실행해도 같은 행에
        붙는다. 이미 있는 행의 신원 필드가 어긋나면 조용히 덮어쓰지 않고 실패한다.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT clip_id FROM clip_artifacts WHERE clip_id = ?", (clip.clip_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO clip_artifacts (clip_id, package_dir, segment_id, asset_id,"
                    " source_order, segment_order, profile_name, relative_path, status,"
                    " content_sha256, size_bytes, file_id, time_base_num, time_base_den,"
                    " core_start_pts, core_end_pts, media_start_pts, media_end_pts,"
                    " core_local_start, core_local_end, media_local_duration, frame_count,"
                    " error, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?, ?, ?)",
                    _clip_values(clip),
                )
                connection.commit()
                return clip
            connection.rollback()
        existing = self.clip_artifact(clip.clip_id)
        identity = (
            existing.package_dir,
            existing.segment_id,
            existing.asset_id,
            existing.source_order,
            existing.segment_order,
            existing.profile_name,
            existing.relative_path,
            existing.time_base_num,
            existing.time_base_den,
            existing.core_start_pts,
            existing.core_end_pts,
        )
        given = (
            clip.package_dir,
            clip.segment_id,
            clip.asset_id,
            clip.source_order,
            clip.segment_order,
            clip.profile_name,
            clip.relative_path,
            clip.time_base_num,
            clip.time_base_den,
            clip.core_start_pts,
            clip.core_end_pts,
        )
        if identity != given:
            raise PackageError(
                f"조각 {clip.clip_id}의 신원이 기록과 다르다 — 같은 ID로 다른 조각을 붙일 수 없다"
            )
        return existing

    def update_clip_artifact(self, clip_id: str, **changes: object) -> ClipArtifact:
        """조각 행의 추출 상태 갱신 — 해시·등록·상태·오류만 고친다. 계획 신원 불변."""
        allowed = {
            "status",
            "content_sha256",
            "size_bytes",
            "file_id",
            "media_start_pts",
            "media_end_pts",
            "core_local_start",
            "core_local_end",
            "media_local_duration",
            "frame_count",
            "error",
        }
        illegal = set(changes) - allowed
        if illegal:
            raise RawWorkspaceError(f"조각의 이 필드는 못 고친다: {sorted(illegal)}")
        columns = ", ".join(f"{name} = ?" for name in changes)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE clip_artifacts SET {columns}, updated_at = ? WHERE clip_id = ?",
                (*changes.values(), _now(), clip_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise PackageError(f"없는 조각: {clip_id}")
        return self.clip_artifact(clip_id)

    def clip_artifact(self, clip_id: str) -> ClipArtifact:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM clip_artifacts WHERE clip_id = ?", (clip_id,)
            ).fetchone()
        if row is None:
            raise PackageError(f"없는 조각: {clip_id}")
        return self._clip_artifact(row)

    def list_clip_artifacts(self, package_dir: str) -> tuple[ClipArtifact, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clip_artifacts WHERE package_dir = ?"
                " ORDER BY source_order, segment_order, clip_id",
                (package_dir,),
            ).fetchall()
        return tuple(self._clip_artifact(row) for row in rows)

    @staticmethod
    def _clip_artifact(row: sqlite3.Row) -> ClipArtifact:
        return ClipArtifact(
            clip_id=str(row["clip_id"]),
            package_dir=str(row["package_dir"]),
            segment_id=str(row["segment_id"]),
            asset_id=str(row["asset_id"]),
            source_order=int(row["source_order"]),
            segment_order=int(row["segment_order"]),
            profile_name=str(row["profile_name"]),
            relative_path=str(row["relative_path"]),
            status=str(row["status"]),
            content_sha256=None if row["content_sha256"] is None else str(row["content_sha256"]),
            size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
            file_id=None if row["file_id"] is None else str(row["file_id"]),
            time_base_num=int(row["time_base_num"]),
            time_base_den=int(row["time_base_den"]),
            core_start_pts=int(row["core_start_pts"]),
            core_end_pts=int(row["core_end_pts"]),
            media_start_pts=int(row["media_start_pts"]),
            media_end_pts=int(row["media_end_pts"]),
            core_local_start=str(row["core_local_start"]),
            core_local_end=str(row["core_local_end"]),
            media_local_duration=str(row["media_local_duration"]),
            frame_count=None if row["frame_count"] is None else int(row["frame_count"]),
            error=None if row["error"] is None else str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def media_registration_by_path(self, relative_path: str) -> MediaRegistration | None:
        """그 상대 경로의 등록 행 — 재개가 같은 file_id를 유지하는지 보는 길."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_files WHERE relative_path = ?", (relative_path,)
            ).fetchone()
        return None if row is None else self._registration(row)
