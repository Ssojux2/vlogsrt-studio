"""A5/A6 검토 작업공간의 저장 계층 — 불변 리비전·헤드·매핑·내보내기 기록.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §13.1·§14.4·16-A5/A6. 정본은
작업공간 DB의 **덧붙인 표**다 — `segment_review`의 순수 리비전을 정준 JSON 그대로
묻고, 헤드 이동은 조건부 한 문장(경합에서 두 편집이 동시에 이길 수 없다)으로만
한다. 표 문장은 `REVIEW_WORKSPACE_SCHEMA_STATEMENTS`로 제공되어 W1이 DB v6
마이그레이션에 그대로 덧붙일 수 있다(지금은 저장소가 처음 붙는 자리에서 스스로
만든다 — 이미 있는 v5 DB의 행은 하나도 고치지 않는다).

지키는 선:

- **출판은 한 트랜잭션이다.** 리비전 행과 헤드 이동(초기 출판이면 첫 헤드,
  입양이면 매핑까지)은 `BEGIN IMMEDIATE` 한 트랜잭션에서만 생긴다 — 출판이
  반쪽으로 끝나면(크래시·잠금) 전부 없던 일이 되므로, 리비전만 있고 헤드가
  없는 고아가 다음 편집을 막는 일은 물리적으로 없다.
- **리비전은 만들기만 한다.** 행의 본문(document_json)을 고치는 길은 없다 —
  어긋난 내용 지문은 되돌리기에서 바로 실패한다. 실패를 감추려고 받아들인
  리비전을 지우는 길도 없다.
- **헤드는 조건부로만 움직인다.** CAS가 지금 헤드가 호출자가 본 리비전임을
  확인한다 — 늦은 완성이 앞선 편집을 덮지 못하고(stale-head), 진 쪽은
  `StaleRevisionError`로 깨끗하게 물러난다(리비전도 안 남긴다).
- **내보내기 신원은 서버가 민다.** 사용자가 고르지 않는 불투명 `export_id`가
  `clips/confirmed_reviewexport_*` 이름공간과 한 쌍이다.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.segment_review import StaleRevisionError
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace, open_db

REVIEW_STORE_SCHEMA_VERSION = "raw-review-workspace/v1"
"""검토 작업공간 표의 스키마 이름 — v6 마이그레이션이 meta에 기록할 값."""

REVIEW_WORKSPACE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS review_workspace_revisions (
        project_id TEXT NOT NULL,
        base_plan_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        document_json TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (project_id, base_plan_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_workspace_heads (
        project_id TEXT NOT NULL,
        base_plan_id TEXT NOT NULL,
        head_revision_id TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, base_plan_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_workspace_remappings (
        old_revision_id TEXT PRIMARY KEY,
        new_base_plan_id TEXT NOT NULL,
        new_revision_id TEXT NOT NULL,
        mapping_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confirmed_export_revisions (
        export_id TEXT PRIMARY KEY,
        destination_dir TEXT NOT NULL,
        project_id TEXT NOT NULL,
        base_plan_id TEXT NOT NULL,
        review_revision_id TEXT NOT NULL,
        review_fingerprint TEXT NOT NULL,
        export_fingerprint TEXT NOT NULL,
        mode TEXT NOT NULL,
        clip_count INTEGER NOT NULL,
        total_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_review_workspace_revisions_head"
    " ON review_workspace_revisions (project_id, base_plan_id, created_at)",
)
"""검토 작업공간이 더한 표 — 리비전 본문·헤드·계획 재매핑·내보내기 리비전.
`ANALYSIS_SCHEMA_STATEMENTS`와 같은 덧붙이기 계약이다(CREATE IF NOT EXISTS)."""


class ReviewWorkspaceStoreError(RawWorkspaceError):
    """검토 저장 계층의 오류 — 이유를 사람에게 그대로 보여준다."""


def init_review_schema(connection: sqlite3.Connection) -> None:
    """검토 표를 (있으면 그대로 두고) 만든다 — 생성·v6 마이그레이션이 같은 문장을 쓴다.

    W1의 `_apply_ingest_schema` 자리에 이 함수를 호출로 붙이면 DB v6이 된다.
    """
    for statement in REVIEW_WORKSPACE_SCHEMA_STATEMENTS:
        connection.execute(statement)


def new_review_export_id() -> str:
    """내보내기 신원 — 서버가 민는 불투명 토큰(사용자 입력이 아니다)."""
    return f"reviewexport_{secrets.token_hex(8)}"


def confirmed_destination_dir(export_id: str) -> str:
    """내보내기 목적지 — `clips/confirmed_*` 이름공간의 서버 소유 자리."""
    return f"clips/confirmed_{export_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class RemappingInsert:
    """입양 매핑 한 행 — 첫 출판과 같은 트랜잭션에 들어갈 값."""

    old_revision_id: str
    new_base_plan_id: str
    new_revision_id: str
    mapping_json: str


class ReviewWorkspaceStore:
    """검토 리비전·헤드·매핑·내보내기의 SQL 문장 — `RawWorkspace`의 DB를 빌려 쓴다."""

    def __init__(self, workspace: RawWorkspace) -> None:
        self._workspace = workspace
        self._schema_ready = False

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """`db_session`과 같은 커밋·롤백·닫기 — 스키마 준비가 연결 안에 끼어 있어 따로 둔다."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = open_db(self._workspace.root / DB_NAME)
        try:
            if not self._schema_ready:
                # 덧붙이기만 한다 — 이미 있는 v5 DB의 어떤 행도 고치지 않는다.
                init_review_schema(connection)
                connection.commit()
                self._schema_ready = True
        except BaseException:
            # 스키마 준비가 죽은 연결을 흘리면 fd가 새는 길이 된다 — 닫고 다시 던진다.
            connection.close()
            raise
        return connection

    # ── 출판 — 리비전과 헤드는 한 몸이다 ─────────────────────────

    def publish_initial_revision(
        self,
        *,
        project_id: str,
        base_plan_id: str,
        document_json: str,
        content_fingerprint: str,
        head_revision_id: str,
        created_at: str,
        remapping: RemappingInsert | None = None,
    ) -> None:
        """첫 리비전 + 첫 헤드(+(입양이면) 매핑) — 한 트랜잭션, 실패면 전부 없던 일.

        초기화·입양의 멱등 경계는 여기 있다: 어느 삽입이든 이미 있으면 트랜잭션
        전체가 되돌려지므로, 반쪽 출판(리비전만 있고 헤드 없는)은 생길 수 없다.
        """
        sequence = self._sequence_of(document_json)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_revision_row(
                    connection,
                    project_id=project_id,
                    base_plan_id=base_plan_id,
                    sequence=sequence,
                    document_json=document_json,
                    content_fingerprint=content_fingerprint,
                    created_at=created_at,
                )
                connection.execute(
                    "INSERT INTO review_workspace_heads (project_id, base_plan_id,"
                    " head_revision_id, updated_at) VALUES (?, ?, ?, ?)",
                    (project_id, base_plan_id, head_revision_id, created_at),
                )
                if remapping is not None:
                    connection.execute(
                        "INSERT INTO review_workspace_remappings (old_revision_id,"
                        " new_base_plan_id, new_revision_id, mapping_json, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            remapping.old_revision_id,
                            remapping.new_base_plan_id,
                            remapping.new_revision_id,
                            remapping.mapping_json,
                            created_at,
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except sqlite3.IntegrityError as error:
            raise ReviewWorkspaceStoreError(
                f"이 계획의 검토(또는 입양 매핑)가 이미 있다: {base_plan_id} —"
                " 다시 초기화하지 말고 load_head으로 여세요"
            ) from error
        except sqlite3.DatabaseError as error:
            raise ReviewWorkspaceStoreError(
                f"검토 초기 출판이 실패해 되돌렸다(잠금·디스크 의심): {error}"
            ) from error
        finally:
            connection.close()

    def publish_edit_revision(
        self,
        *,
        project_id: str,
        base_plan_id: str,
        document_json: str,
        content_fingerprint: str,
        expected_head_revision_id: str,
        new_head_revision_id: str,
        updated_at: str,
    ) -> None:
        """편집 리비전 + 헤드 CAS — 한 트랜잭션. 헤드가 못 움직이면 리비전도 못 남는다.

        동시 편집이 그 자리를 먼저 차지했거나(Uniqueness) 헤드가 이미 옮겨갔으면
        (CAS) 늦은 쪽은 `StaleRevisionError`로 깨끗하게 진다 — 지는 쪽의 리비전 행은
        되돌려지므로 다음 편집이 막히는 일이 없다.
        """
        sequence = self._sequence_of(document_json)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_revision_row(
                    connection,
                    project_id=project_id,
                    base_plan_id=base_plan_id,
                    sequence=sequence,
                    document_json=document_json,
                    content_fingerprint=content_fingerprint,
                    created_at=updated_at,
                )
                cursor = connection.execute(
                    "UPDATE review_workspace_heads SET head_revision_id = ?, updated_at = ?"
                    " WHERE project_id = ? AND base_plan_id = ? AND head_revision_id = ?",
                    (
                        new_head_revision_id,
                        updated_at,
                        project_id,
                        base_plan_id,
                        expected_head_revision_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleRevisionError(
                        f"stale_head: 헤드가 이미 {expected_head_revision_id!r}이(가) 아니다 —"
                        " 화면을 다시 읽고 새 헤드로 편집하세요"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except sqlite3.IntegrityError as error:
            raise StaleRevisionError(
                f"동시 편집: 그 자리({base_plan_id}/{sequence})에 이미 리비전이 있다 —"
                " 헤드를 다시 읽어 새 헤드로 편집하세요"
            ) from error
        except sqlite3.DatabaseError as error:
            raise ReviewWorkspaceStoreError(
                f"검토 편집 출판이 실패해 되돌렸다(잠금·디스크 의심): {error}"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _insert_revision_row(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        base_plan_id: str,
        sequence: int,
        document_json: str,
        content_fingerprint: str,
        created_at: str,
    ) -> None:
        """리비전 한 행 — 만들기만 한다. 같은 자리의 두 번 삽입은 트랜잭션째 실패한다."""
        connection.execute(
            "INSERT INTO review_workspace_revisions (project_id, base_plan_id,"
            " sequence, document_json, content_fingerprint, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                project_id,
                base_plan_id,
                sequence,
                document_json,
                content_fingerprint,
                created_at,
            ),
        )

    # ── 불변 리비전 ─────────────────────────────────────────────

    def revision_document(
        self, project_id: str, base_plan_id: str, sequence: int
    ) -> tuple[str, str]:
        """리비전 본문과 기록된 지문 — 되돌리기에서 다시 잰 지문과 비교할 쌍."""
        with self._session() as connection:
            row = connection.execute(
                "SELECT document_json, content_fingerprint FROM review_workspace_revisions"
                " WHERE project_id = ? AND base_plan_id = ? AND sequence = ?",
                (project_id, base_plan_id, sequence),
            ).fetchone()
        if row is None:
            raise ReviewWorkspaceStoreError(
                f"검토 리비전이 없다: {base_plan_id}/{sequence} — 다시 열거나 초기화하세요"
            )
        return str(row["document_json"]), str(row["content_fingerprint"])

    def list_base_plan_ids(self, project_id: str) -> tuple[str, ...]:
        """리비전이 있는 기본 계획 — 만들어진 순서 그대로."""
        with self._session() as connection:
            rows = connection.execute(
                "SELECT DISTINCT base_plan_id FROM review_workspace_revisions"
                " WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
        return tuple(str(row["base_plan_id"]) for row in rows)

    # ── 헤드 — 조건부 갱신이 stale-head 가드다 ──────────────────

    def head_revision_id(self, project_id: str, base_plan_id: str) -> str | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT head_revision_id FROM review_workspace_heads"
                " WHERE project_id = ? AND base_plan_id = ?",
                (project_id, base_plan_id),
            ).fetchone()
        return None if row is None else str(row["head_revision_id"])

    # ── 계획 재매핑 기록 ─────────────────────────────────────────

    def remapping(self, old_revision_id: str) -> sqlite3.Row | None:
        with self._session() as connection:
            return connection.execute(
                "SELECT * FROM review_workspace_remappings WHERE old_revision_id = ?",
                (old_revision_id,),
            ).fetchone()

    # ── 내보내기 리비전 — 서버 소유 이름공간의 기록 ──────────────

    def insert_export_revision(
        self,
        *,
        export_id: str,
        destination_dir: str,
        project_id: str,
        base_plan_id: str,
        review_revision_id: str,
        review_fingerprint: str,
        export_fingerprint: str,
        mode: str,
        clip_count: int,
        total_bytes: int,
        created_at: str,
    ) -> None:
        """내보내기 한 벌의 기록 — 같은 신원의 두 번 삽입은 실패한다."""
        try:
            with self._session() as connection:
                connection.execute(
                    "INSERT INTO confirmed_export_revisions (export_id, destination_dir,"
                    " project_id, base_plan_id, review_revision_id, review_fingerprint,"
                    " export_fingerprint, mode, clip_count, total_bytes, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        export_id,
                        destination_dir,
                        project_id,
                        base_plan_id,
                        review_revision_id,
                        review_fingerprint,
                        export_fingerprint,
                        mode,
                        clip_count,
                        total_bytes,
                        created_at,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise ReviewWorkspaceStoreError(
                f"이미 기록된 내보내기 신원이다: {export_id}"
            ) from error

    def list_export_revisions(self, project_id: str) -> tuple[sqlite3.Row, ...]:
        with self._session() as connection:
            return tuple(
                connection.execute(
                    "SELECT * FROM confirmed_export_revisions WHERE project_id = ? ORDER BY rowid",
                    (project_id,),
                ).fetchall()
            )

    @staticmethod
    def _sequence_of(document_json: str) -> int:
        """정본 문서에서 sequence를 다시 읽는다 — 행 키는 본문과 어긋날 수 없다."""
        try:
            sequence = int(json.loads(document_json)["sequence"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ReviewWorkspaceStoreError(
                f"검토 리비전 문서에서 sequence를 읽지 못했다: {error}"
            ) from error
        if sequence < 0:
            raise ReviewWorkspaceStoreError(f"sequence는 0 이상이어야 한다: {sequence}")
        return sequence
