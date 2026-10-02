"""작업공간 저장소 — SQLite 정본, layout, 미디어 등록·해석(계획 §9.1·§9.2).

DB가 자산·작업 상태의 정본이고, 내부 경로는 **작업공간 루트 기준 상대 경로**로
저장한다(§9.2 — 디스크를 옮겨도 다시 연결된다). 미디어 제공은 등록된 ID로만
가능하다: 로그·설정·DB·API 응답 경로는 허용 디렉터리 밖이라 애초에 등록이
거부된다(§4.4). 허용 목록이 곧 보호다 — `jobs/`·`subtitles/`·`analysis/`는
브라우저 제공 대상이 아니다.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat as stat_module
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

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
    MediaNotAllowedError,
    MediaNotFoundError,
    MediaNotReadyError,
    MediaRegistration,
    MediaTargetChangedError,
    WorkspaceAlreadyExistsError,
    WorkspaceNotFoundError,
    WorkspaceVersionError,
)

PROJECT_TOML_NAME = "project.toml"
DB_NAME = "project.sqlite3"
DB_SCHEMA_VERSION = "raw-workspace-db/1"
WORKSPACE_ID_KEY = "workspace_id"
"""meta 표의 작업공간 신원 키 — 생성 때 한 번 정해지고 절대 바뀌지 않는다."""


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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        (root / PROJECT_TOML_NAME).write_bytes(
            tomli_w.dumps(
                {
                    "schema_version": WORKFLOW_SCHEMA_VERSION,
                    "name": name.strip(),
                    "created_at": created_at,
                }
            ).encode("utf-8")
        )
        workspace = cls(root, name.strip(), created_at)
        with workspace.connect() as connection:
            connection.executescript(_SCHEMA_SQL)  # executescript는 먼저 commit한다
            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('db_schema_version', ?)",
                (DB_SCHEMA_VERSION,),
            )
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (WORKSPACE_ID_KEY, new_workspace_id()),
            )
            connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('workflow', ?, ?)",
                (raw_workflow_config_to_json(RawWorkflowConfig()), created_at),
            )
            connection.commit()
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
        version = workspace.db_schema_version()
        if version != DB_SCHEMA_VERSION:
            raise WorkspaceVersionError(
                f"DB schema가 다르다: {version} (이 프로그램은 {DB_SCHEMA_VERSION})"
            )
        return workspace

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
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'db_schema_version'"
            ).fetchone()
        if row is None:
            raise WorkspaceVersionError("DB에 schema 버전이 없다")
        return str(row[0])

    def workspace_id(self) -> str:
        """이 작업공간의 불변 신원 — 화면의 선택 검증이 이 토큰을 믿는다.

        이 fix 이전에 만들어진 작업공간에는 없다 — 처음 물은 한 번만 정해져 이후
        불변이다(경합에서도 INSERT OR IGNORE가 한 쪽만 살린다).
        """
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

    # ── 연결 ───────────────────────────────────────────────────

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.root / DB_NAME, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    # ── 설정 ───────────────────────────────────────────────────

    def load_workflow(self) -> RawWorkflowConfig:
        """저장된 설정을 검증까지 마쳐서 돌려준다 — 왕복이 곧 계약이다(A0 §9.1)."""
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
        """등록 순서대로 — ID가 무작위라 정렬 근거는 삽입 행(rowid)이다."""
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
        """
        row = self._media_row(file_id)
        if str(row["status"]) not in SERVABLE_MEDIA_STATUSES:
            raise MediaNotReadyError(f"아직 준비되지 않은 미디어다: {file_id}")
        relative = str(row["relative_path"])
        target = self.root / relative
        if not target.exists():
            raise MediaNotFoundError(f"등록된 파일이 없다: {file_id}")
        probed = target.lstat()
        fingerprint_changed = (
            probed.st_size != int(row["size_bytes"])
            or probed.st_mtime_ns != int(row["mtime_ns"])
            or probed.st_ino != int(row["inode"])
        )
        if fingerprint_changed or not self._free_of_symlinks(target):
            raise MediaTargetChangedError(
                f"등록된 자리의 파일이 바뀌었다: {file_id} — 다시 등록해야 한다"
            )
        return target, probed.st_size, str(row["kind"])
