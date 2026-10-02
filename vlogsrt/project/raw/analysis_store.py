"""A4 의미 분석의 저장 계층 — 불변 리비전·원본별 결과·성공 캐시(계획 §13.1·§14.2).

정본은 DB(`analysis_revisions`·`analysis_source_results`)이고 증거 문서는
작업공간의 `analysis/` 아래 상대 경로로 산다(§9.2). 문서는 한 번 쓰이면 다시
쓰지 않는다 — 같은 지문의 재실행은 **캐시 행**으로 이어 붙일 뿐 옛 문서를
고치지 않는다. 오류는 성공 캐시로 저장되지 않는다(§14.2): 실패 원본의 재시도는
그 원본만 다시 분석한다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from vlogsrt.project.raw.models import (
    ANALYSIS_SOURCE_STATUSES,
    AnalysisError,
    AnalysisRevision,
    AnalysisSourceResult,
)
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace

ANALYSIS_ROOT = "analysis"
"""분석 증거의 뿌리 디렉터리 — 브라우저 제공 대상이 아니다(§4.4)."""

ANALYSIS_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS analysis_revisions (
        revision_id TEXT PRIMARY KEY,
        order_revision_id TEXT NOT NULL,
        basis TEXT NOT NULL,
        mode TEXT NOT NULL,
        settings_json TEXT NOT NULL,
        models_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        source_total INTEGER NOT NULL,
        source_done INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_source_results (
        revision_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        source_name TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        speech_status TEXT,
        execution_mode TEXT,
        document_path TEXT,
        record_path TEXT,
        window_count INTEGER NOT NULL DEFAULT 0,
        segment_count INTEGER NOT NULL DEFAULT 0,
        cached_from TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (revision_id, asset_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_source_cache"
    " ON analysis_source_results (fingerprint, status)",
)
"""A4가 더한 표 — 실행 리비전(서버 소유 상태)과 원본별 결과. 캐시 조회는
(지문, 상태) 색인으로: 같은 지문의 `analyzed`/`cached` 행이 성공 캐시다."""


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def new_analysis_revision_id() -> str:
    """분석 리비전의 전역 유일 불투명 ID — 미디어·조각 ID와 같은 규칙."""
    import secrets

    return f"anal_{secrets.token_hex(8)}"


class AnalysisStore:
    """분석 리비전·원본 결과의 SQL 문장 — `RawWorkspace.connect`를 빌려 쓴다."""

    def __init__(self, workspace: RawWorkspace) -> None:
        self._workspace = workspace

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._workspace.root / DB_NAME, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    # ── 리비전 ─────────────────────────────────────────────────

    def insert_revision(self, revision: AnalysisRevision) -> None:
        """리비전 한 행 — 만들기만 한다. 같은 ID의 두 번 삽입은 실패한다."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO analysis_revisions (revision_id, order_revision_id, basis, mode,"
                " settings_json, models_json, fingerprint, status, source_total, source_done,"
                " error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.revision_id,
                    revision.order_revision_id,
                    revision.basis,
                    revision.mode,
                    revision.settings_json,
                    revision.models_json,
                    revision.fingerprint,
                    revision.status,
                    revision.source_total,
                    revision.source_done,
                    revision.error,
                    revision.created_at,
                    revision.updated_at,
                ),
            )
            connection.commit()

    def update_revision(self, revision_id: str, **changes: object) -> AnalysisRevision:
        """리비전의 서버 소유 상태 갱신 — 상태·완료 수·오류만 고친다."""
        allowed = {"status", "source_done", "error"}
        illegal = set(changes) - allowed
        if illegal:
            raise AnalysisError(f"분석 리비전의 이 필드는 못 고친다: {sorted(illegal)}")
        columns = ", ".join(f"{name} = ?" for name in changes)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE analysis_revisions SET {columns}, updated_at = ? WHERE revision_id = ?",
                (*changes.values(), _now(), revision_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise AnalysisError(f"없는 분석 리비전: {revision_id}")
        revision = self.revision(revision_id)
        assert revision is not None
        return revision

    def revision(self, revision_id: str) -> AnalysisRevision | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        return None if row is None else self._revision(row)

    def list_revisions(self) -> tuple[AnalysisRevision, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT revision_id FROM analysis_revisions ORDER BY rowid"
            ).fetchall()
        revisions = [self.revision(str(row[0])) for row in rows]
        return tuple(revision for revision in revisions if revision is not None)

    @staticmethod
    def _revision(row: sqlite3.Row) -> AnalysisRevision:
        return AnalysisRevision(
            revision_id=str(row["revision_id"]),
            order_revision_id=str(row["order_revision_id"]),
            basis=str(row["basis"]),
            mode=str(row["mode"]),
            settings_json=str(row["settings_json"]),
            models_json=str(row["models_json"]),
            fingerprint=str(row["fingerprint"]),
            status=str(row["status"]),
            source_total=int(row["source_total"]),
            source_done=int(row["source_done"]),
            error=None if row["error"] is None else str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    # ── 원본별 결과 ────────────────────────────────────────────

    def upsert_source_result(self, result: AnalysisSourceResult) -> AnalysisSourceResult:
        """원본 하나의 결과 행을 (있으면 갱신, 없으면 만들어) 넣는다.

        신원 필드(순번·이름)는 첫 기록에서 얼어붙는다 — 같은 리비전 안에서 같은
        원본이 다른 자리로 움직이면 조용히 덮어쓰지 않고 실패한다. 지문은 서버가
        분석을 진행하며 채우는 값이라 갱신된다: `pending/failed` 표시 뒤 실측
        지문이 붙는다. 성공 캐시는 지문이 **final**인 `analyzed`/`cached` 행만
        조회하므로 중간값이 캐시로 새는 일은 없다.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT source_order, source_name FROM analysis_source_results"
                " WHERE revision_id = ? AND asset_id = ?",
                (result.revision_id, result.asset_id),
            ).fetchone()
            if row is not None and (
                int(row["source_order"]) != result.source_order
                or str(row["source_name"]) != result.source_name
            ):
                connection.rollback()
                raise AnalysisError(
                    f"분석 결과 {result.asset_id}의 신원이 기록과 다르다 — 같은 리비전에서"
                    " 원본의 자리가 바뀌었으면 새 리비전을 만들어야 한다"
                )
            connection.execute(
                "INSERT INTO analysis_source_results (revision_id, asset_id, source_order,"
                " source_name, fingerprint, status, speech_status, execution_mode,"
                " document_path, record_path, window_count, segment_count, cached_from,"
                " error, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(revision_id, asset_id) DO UPDATE SET"
                " fingerprint = excluded.fingerprint, status = excluded.status,"
                " speech_status = excluded.speech_status,"
                " execution_mode = excluded.execution_mode,"
                " document_path = excluded.document_path, record_path = excluded.record_path,"
                " window_count = excluded.window_count,"
                " segment_count = excluded.segment_count,"
                " cached_from = excluded.cached_from, error = excluded.error,"
                " updated_at = excluded.updated_at",
                self._source_values(result),
            )
            connection.commit()
        restored = self.source_result(result.revision_id, result.asset_id)
        assert restored is not None
        return restored

    def source_result(self, revision_id: str, asset_id: str) -> AnalysisSourceResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_source_results WHERE revision_id = ? AND asset_id = ?",
                (revision_id, asset_id),
            ).fetchone()
        return None if row is None else self._source_result(row)

    def list_source_results(self, revision_id: str) -> tuple[AnalysisSourceResult, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM analysis_source_results WHERE revision_id = ?"
                " ORDER BY source_order, asset_id",
                (revision_id,),
            ).fetchall()
        return tuple(self._source_result(row) for row in rows)

    def cached_success(
        self, fingerprint: str
    ) -> tuple[AnalysisSourceResult, AnalysisRevision] | None:
        """같은 지문의 가장 최근 성공 결과 — 캐시 재사용의 후보(§14.2).

        `analyzed`·`cached` 상태만 성공이다. 호출자는 문서 파일 존재와 안의
        출처 해시를 다시 검사한 뒤에야 재사용한다 — 이 조회는 후보일 뿐이다.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_source_results WHERE fingerprint = ?"
                " AND status IN ('analyzed', 'cached')"
                " ORDER BY updated_at DESC, revision_id DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if row is None:
                return None
            revision = self.revision(str(row["revision_id"]))
        if revision is None:
            return None
        return self._source_result(row), revision

    @staticmethod
    def _source_values(result: AnalysisSourceResult) -> tuple[object, ...]:
        return (
            result.revision_id,
            result.asset_id,
            result.source_order,
            result.source_name,
            result.fingerprint,
            result.status,
            result.speech_status,
            result.execution_mode,
            result.document_path,
            result.record_path,
            result.window_count,
            result.segment_count,
            result.cached_from,
            result.error,
            result.created_at,
            result.updated_at,
        )

    @staticmethod
    def _source_result(row: sqlite3.Row) -> AnalysisSourceResult:
        return AnalysisSourceResult(
            revision_id=str(row["revision_id"]),
            asset_id=str(row["asset_id"]),
            source_order=int(row["source_order"]),
            source_name=str(row["source_name"]),
            fingerprint=str(row["fingerprint"]),
            status=str(row["status"]),
            speech_status=None if row["speech_status"] is None else str(row["speech_status"]),
            execution_mode=None if row["execution_mode"] is None else str(row["execution_mode"]),
            document_path=None if row["document_path"] is None else str(row["document_path"]),
            record_path=None if row["record_path"] is None else str(row["record_path"]),
            window_count=int(row["window_count"]),
            segment_count=int(row["segment_count"]),
            cached_from=None if row["cached_from"] is None else str(row["cached_from"]),
            error=None if row["error"] is None else str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def settings_json_dumps(settings: dict[str, object]) -> str:
    """설정·모델 JSON 직렬화 — 정렬 키로 지문 안정성을 지킨다."""
    return json.dumps(settings, ensure_ascii=False, sort_keys=True)


def valid_source_status(status: str) -> bool:
    return status in ANALYSIS_SOURCE_STATUSES


def ensure_statuses(results: Sequence[AnalysisSourceResult]) -> None:
    """기록된 상태가 값역 안에 있는지 — 깨진 DB를 조용히 통과시키지 않는다."""
    for result in results:
        if not valid_source_status(result.status):
            raise AnalysisError(
                f"분석 결과 상태가 값역 밖이다: {result.asset_id} {result.status!r}"
            )
