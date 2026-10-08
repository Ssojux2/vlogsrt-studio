"""A7 작업공간의 SQLite 선로 — 덧붙이기 전용 자막 표(계획 §9.2·§16 A7).

`project.sqlite3`에 자막 등록·승인·실행·응답 캐시 표를 **덧붙이기만** 한다.
기존 표·`db_schema_version`은 건드리지 않는다 — 중앙 마이그레이션은 W1이
`init_schema`(또는 `caption_schema_sql`) 후크로 DBv6에서 함께 내린다. 이 모듈은
선로일 뿐이다: 값의 의미는 `caption_workspace_types.py`가, 오케스트레이션은
`caption_workspace.py`가 정한다.

지키는 선:

- **덧붙이기만 한다.** 모든 문장은 `CREATE … IF NOT EXISTS`다 — 옛 작업공간을
  열면 없는 표만 생기고 이미 있는 행은 그대로다.
- **첫 값이 산다.** 승인·응답 캐시는 같은 신원에 처음 저장된 값을 유지한다
  (바꿔치기 경로가 없다, §11.1). 실행 행은 갱신 칸이 상태뿐이다.
- **값은 타이핑된다.** 행은 곧바로 값형으로 나가고, 유리수는 분자·분모 정수
  쌍으로 왕복한다(§13.3 — float을 거치지 않는다).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from vlogsrt.project.raw.caption_store import canonical_json
from vlogsrt.project.raw.caption_workspace_types import (
    ApprovalRecord,
    CachedResponse,
    CaptionRunRecord,
    CaptionWorkspaceError,
    FinalVideoRegistration,
    RunIdentity,
)
from vlogsrt.project.raw.workspace import DB_NAME, db_session

CAPTION_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS caption_registrations (
        registration_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        final_video_revision TEXT NOT NULL,
        status TEXT NOT NULL,
        audio_ordinal INTEGER,
        audio_stream_count INTEGER NOT NULL,
        external_source TEXT NOT NULL,
        audio_stream_index INTEGER,
        pcm_sha256 TEXT,
        sample_rate INTEGER,
        channels INTEGER,
        sample_count INTEGER,
        origin_num INTEGER,
        origin_den INTEGER,
        video_duration REAL,
        verified_copy_rel TEXT,
        pcm_rel TEXT,
        manifest_rel TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (final_video_revision, audio_ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS caption_approvals (
        final_video_revision TEXT NOT NULL,
        run_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        audio_stream_index INTEGER NOT NULL,
        sample_start INTEGER NOT NULL,
        sample_end INTEGER NOT NULL,
        text_model TEXT NOT NULL,
        timing_model TEXT NOT NULL,
        settings_fingerprint TEXT NOT NULL,
        budget_usd TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (final_video_revision, run_id, fingerprint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS caption_runs (
        final_video_revision TEXT NOT NULL,
        run_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        cancellation_reason TEXT NOT NULL DEFAULT '',
        error TEXT,
        draft_rel TEXT,
        auto_content_sha256 TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (final_video_revision, run_id, fingerprint)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_caption_runs_revision ON caption_runs (final_video_revision)",
    """
    CREATE TABLE IF NOT EXISTS caption_responses (
        run_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        request_id TEXT NOT NULL,
        role TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        audio_sha256 TEXT NOT NULL,
        sample_start INTEGER NOT NULL,
        sample_end INTEGER NOT NULL,
        sample_rate INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'injected_provider',
        PRIMARY KEY (run_id, request_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_caption_responses_run"
    " ON caption_responses (run_id, fingerprint)",
)


def init_schema(connection: sqlite3.Connection) -> None:
    """자막 표를 한 트랜잭션으로 (있으면 그대로 두고) 만든다 — W1 DBv6 후크."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in CAPTION_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def caption_schema_sql() -> str:
    """같은 문장의 SQL 뭉치 — 마이그레이션 도구가 그대로 실을 수 있는 모양."""
    return ";\n".join(statement.strip() for statement in CAPTION_SCHEMA_STATEMENTS) + ";\n"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_payload(payload_json: str) -> dict[str, object]:
    document = json.loads(payload_json)
    if not isinstance(document, dict):
        raise CaptionWorkspaceError("캐시 응답 payload가 객체가 아니다 — 손상된 기록이다")
    return document


class CaptionWorkspaceStore:
    """자막 작업공간 표의 문장 단위 갱신 — 호출마다 짧은 연결을 쓴다(스레드 안전)."""

    def __init__(self, root: Path) -> None:
        self._db_path = root / DB_NAME
        with self._connect() as connection:
            init_schema(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with db_session(self._db_path) as connection:
            yield connection

    # ── 등록 ────────────────────────────────────────────────────

    def save_registration(self, registration: FinalVideoRegistration) -> None:
        """등록 행을 넣는다 — 같은 (렌더, 오디오 선택) 자리는 호출자가 먼저 본다."""
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO caption_registrations (registration_id, source_id,"
                " final_video_revision, status, audio_ordinal, audio_stream_count,"
                " external_source, audio_stream_index, pcm_sha256, sample_rate, channels,"
                " sample_count, origin_num, origin_den, video_duration, verified_copy_rel,"
                " pcm_rel, manifest_rel, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _registration_values(registration, now),
            )
            connection.commit()

    def registration(
        self, final_video_revision: str, audio_ordinal: int | None
    ) -> FinalVideoRegistration | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM caption_registrations"
                " WHERE final_video_revision = ? AND audio_ordinal IS ?",
                (final_video_revision, audio_ordinal),
            ).fetchone()
        return None if row is None else _registration(row)

    def registrations(self) -> tuple[FinalVideoRegistration, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM caption_registrations ORDER BY rowid"
            ).fetchall()
        return tuple(_registration(row) for row in rows)

    # ── 승인 — 첫 값이 산다 ─────────────────────────────────────

    def save_approval(self, record: ApprovalRecord) -> None:
        """승인을 no-replace로 못박는다 — 같은 신원 같은 값은 멱등, 다른 값은 거부."""
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO caption_approvals (final_video_revision, run_id, fingerprint,"
                    " audio_stream_index, sample_start, sample_end, text_model, timing_model,"
                    " settings_fingerprint, budget_usd, note, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _approval_values(record),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                _require_same_approval(self.approval(record.identity), record)

    def approval(self, identity: RunIdentity) -> ApprovalRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM caption_approvals WHERE final_video_revision = ?"
                " AND run_id = ? AND fingerprint = ?",
                identity.triple(),
            ).fetchone()
        return None if row is None else _approval(row)

    # ── 실행 행 ─────────────────────────────────────────────────

    def ensure_run(self, identity: RunIdentity) -> CaptionRunRecord:
        """실행 행을 (있으면 그대로 두고) 만든다 — 재제출·재개가 같은 행에 붙는다."""
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO caption_runs (final_video_revision, run_id,"
                " fingerprint, status, cancellation_reason, created_at, updated_at)"
                " VALUES (?, ?, ?, 'running', '', ?, ?)",
                (*identity.triple(), now, now),
            )
            connection.commit()
        found = self.run(identity)
        assert found is not None
        return found

    def update_run(self, identity: RunIdentity, **changes: object) -> CaptionRunRecord:
        """실행 행의 상태 갱신 — 상태·취소 사유·오류·초안 자리·동결 해시만 고친다."""
        allowed = {"status", "cancellation_reason", "error", "draft_rel", "auto_content_sha256"}
        illegal = set(changes) - allowed
        if illegal:
            raise CaptionWorkspaceError(f"실행 행의 이 필드는 못 고친다: {sorted(illegal)}")
        columns = ", ".join(f"{name} = ?" for name in changes)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE caption_runs SET {columns}, updated_at = ?"
                " WHERE final_video_revision = ? AND run_id = ? AND fingerprint = ?",
                (*changes.values(), _now(), *identity.triple()),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise CaptionWorkspaceError(f"없는 실행 기록이다: {identity}")
        found = self.run(identity)
        assert found is not None
        return found

    def run(self, identity: RunIdentity) -> CaptionRunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM caption_runs WHERE final_video_revision = ?"
                " AND run_id = ? AND fingerprint = ?",
                identity.triple(),
            ).fetchone()
        return None if row is None else _run(row)

    def runs(self, final_video_revision: str) -> tuple[CaptionRunRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM caption_runs WHERE final_video_revision = ? ORDER BY rowid",
                (final_video_revision,),
            ).fetchall()
        return tuple(_run(row) for row in rows)

    # ── 응답 캐시 — 첫 값이 산다 ────────────────────────────────

    def record_responses(self, items: Iterable[CachedResponse]) -> None:
        """응답을 캐시에 넣는다 — 같은 (run, 요청) 자리는 첫 값을 유지한다(§14.2)."""
        rows = [_response_values(item) for item in items]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO caption_responses (run_id, fingerprint, request_id,"
                " role, chunk_index, audio_sha256, sample_start, sample_end, sample_rate,"
                " payload_json, created_at, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()

    def responses(self, run_id: str, fingerprint: str) -> tuple[CachedResponse, ...]:
        """그 run·그 지문의 캐시 — 다른 지문의 응답은 못 본 척하지 않는다."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM caption_responses WHERE run_id = ? AND fingerprint = ?"
                " ORDER BY chunk_index, request_id",
                (run_id, fingerprint),
            ).fetchall()
        return tuple(_response(row) for row in rows)


def _require_same_approval(existing: ApprovalRecord | None, given: ApprovalRecord) -> None:
    if existing is None or existing != given:
        raise CaptionWorkspaceError(
            "같은 신원에 이미 다른 승인이 있다 — 승인은 바꿔치기가 아니라 새 실행이다"
        )


def _registration_values(registration: FinalVideoRegistration, now: str) -> tuple[object, ...]:
    origin = registration.origin
    return (
        registration.registration_id,
        registration.source_id,
        registration.final_video_revision,
        registration.status,
        registration.audio_ordinal,
        registration.audio_stream_count,
        registration.external_source,
        registration.audio_stream_index,
        registration.pcm_sha256,
        registration.sample_rate,
        registration.channels,
        registration.sample_count,
        None if origin is None else origin.numerator,
        None if origin is None else origin.denominator,
        registration.video_duration,
        registration.verified_copy_rel,
        registration.pcm_rel,
        registration.manifest_rel,
        registration.created_at,
        now,
    )


def _registration(row: sqlite3.Row) -> FinalVideoRegistration:
    num, den = row["origin_num"], row["origin_den"]
    return FinalVideoRegistration(
        registration_id=str(row["registration_id"]),
        source_id=str(row["source_id"]),
        final_video_revision=str(row["final_video_revision"]),
        status=str(row["status"]),
        audio_stream_count=int(row["audio_stream_count"]),
        external_source=str(row["external_source"]),
        audio_ordinal=None if row["audio_ordinal"] is None else int(row["audio_ordinal"]),
        audio_stream_index=None
        if row["audio_stream_index"] is None
        else int(row["audio_stream_index"]),
        pcm_sha256=None if row["pcm_sha256"] is None else str(row["pcm_sha256"]),
        sample_rate=None if row["sample_rate"] is None else int(row["sample_rate"]),
        channels=None if row["channels"] is None else int(row["channels"]),
        sample_count=None if row["sample_count"] is None else int(row["sample_count"]),
        origin=None if num is None or den is None else Fraction(int(num), int(den)),
        video_duration=None if row["video_duration"] is None else float(row["video_duration"]),
        verified_copy_rel=None
        if row["verified_copy_rel"] is None
        else str(row["verified_copy_rel"]),
        pcm_rel=None if row["pcm_rel"] is None else str(row["pcm_rel"]),
        manifest_rel=None if row["manifest_rel"] is None else str(row["manifest_rel"]),
        created_at=str(row["created_at"]),
    )


def _approval_values(record: ApprovalRecord) -> tuple[object, ...]:
    return (
        *record.identity.triple(),
        record.audio_stream_index,
        record.sample_start,
        record.sample_end,
        record.text_model,
        record.timing_model,
        record.settings_fingerprint,
        record.budget_usd,
        record.note,
        record.created_at,
    )


def _approval(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        identity=RunIdentity(
            final_video_revision=str(row["final_video_revision"]),
            run_id=str(row["run_id"]),
            fingerprint=str(row["fingerprint"]),
        ),
        audio_stream_index=int(row["audio_stream_index"]),
        sample_start=int(row["sample_start"]),
        sample_end=int(row["sample_end"]),
        text_model=str(row["text_model"]),
        timing_model=str(row["timing_model"]),
        settings_fingerprint=str(row["settings_fingerprint"]),
        budget_usd=str(row["budget_usd"]),
        note=str(row["note"]),
        created_at=str(row["created_at"]),
    )


def _run(row: sqlite3.Row) -> CaptionRunRecord:
    return CaptionRunRecord(
        identity=RunIdentity(
            final_video_revision=str(row["final_video_revision"]),
            run_id=str(row["run_id"]),
            fingerprint=str(row["fingerprint"]),
        ),
        status=str(row["status"]),
        cancellation_reason=str(row["cancellation_reason"]),
        error=None if row["error"] is None else str(row["error"]),
        draft_rel=None if row["draft_rel"] is None else str(row["draft_rel"]),
        auto_content_sha256=None
        if row["auto_content_sha256"] is None
        else str(row["auto_content_sha256"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _response_values(item: CachedResponse) -> tuple[object, ...]:
    return (
        item.run_id,
        item.fingerprint,
        item.request_id,
        item.role,
        item.chunk_index,
        item.audio_sha256,
        item.sample_start,
        item.sample_end,
        item.sample_rate,
        canonical_json(dict(item.payload)),
        item.created_at,
        item.origin,
    )


def _response(row: sqlite3.Row) -> CachedResponse:
    return CachedResponse(
        run_id=str(row["run_id"]),
        fingerprint=str(row["fingerprint"]),
        request_id=str(row["request_id"]),
        role=str(row["role"]),
        chunk_index=int(row["chunk_index"]),
        audio_sha256=str(row["audio_sha256"]),
        sample_start=int(row["sample_start"]),
        sample_end=int(row["sample_end"]),
        sample_rate=int(row["sample_rate"]),
        payload=_json_payload(str(row["payload_json"])),
        created_at=str(row["created_at"]),
        origin=str(row["origin"]),
    )
