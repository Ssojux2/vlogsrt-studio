"""서버 소유의 지속 작업 관리(계획 §14.1).

브라우저 세션이 작업을 소유하지 않는다 — 상태는 전부 DB에 있고 화면 새로고침은
기존 작업에 다시 붙는다. 같은 (종류, 입력 지문)의 활성 작업이 있으면 새로 만들지
않고 그 작업을 돌려준다(재클릭·새로고침 중복 방지). 실행 중이던 작업의 복구는
**서버 시작 시 명시적 호출 한 번**으로만 — 작업공간을 열 때마다 복구하지 않는다.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from vlogsrt import log
from vlogsrt.project.raw.models import (
    ACTIVE_JOB_STATUSES,
    JobHandle,
    JobRecord,
    RawWorkspaceError,
)
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace, db_session, open_db
from vlogsrt.task_runtime import cancellation_scope

FINAL_STATE_ATTEMPTS = 3
FINAL_STATE_RETRY_SECONDS = 0.5
"""끝 상태 기록의 재시도 — 잠금·fd 고갈 같은 일시 오류가 스레드를 죽이지 않게."""


class JobCancelledError(Exception):
    """작업이 취소 게이트에 걸렸다 — 이미 끝난 유효 결과는 그대로 둔다."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobContext:
    """작업 본문이 쓰는 창구 — 진행은 매 걸음 DB에 남는다(durable progress)."""

    def __init__(self, job_id: str, workspace: RawWorkspace) -> None:
        self.job_id = job_id
        self._workspace = workspace

    def check_cancel(self) -> None:
        """취소 게이트 — 새 요청·새 추출을 시작하기 전에 반드시 지난다."""
        if JobStore(self._workspace).cancellation_requested(self.job_id):
            raise JobCancelledError(self.job_id)

    def set_total(self, total: int) -> None:
        JobStore(self._workspace).update(self.job_id, progress_total=total)

    def set_progress(self, current: int) -> None:
        """이번 실행의 진행값을 다시 맞춘다 — 재시도가 지난 진행 위에 더 쌓지 않는다."""
        JobStore(self._workspace).update(self.job_id, progress_current=current)

    def advance(self) -> None:
        JobStore(self._workspace).advance(self.job_id)


def active_fingerprints_on(connection: sqlite3.Connection, kinds: Collection[str]) -> set[str]:
    """연결 하나 위에서 읽은 그 종류의 활성 작업 지문 — 다른 쓰기 트랜잭션 안의 검사가 같은
    연결로 읽는다(작업 등록 `insert_deduplicated`도 같은 DB의 쓰기 트랜잭션이라 그 사이에 끼지
    못한다).
    """
    kind_marks = ", ".join("?" for _ in kinds)
    status_marks = ", ".join("?" for _ in ACTIVE_JOB_STATUSES)
    rows = connection.execute(
        f"SELECT input_fingerprint FROM jobs WHERE kind IN ({kind_marks})"
        f" AND status IN ({status_marks})",
        (*kinds, *ACTIVE_JOB_STATUSES),
    ).fetchall()
    return {str(row[0]) for row in rows}


class JobStore:
    """jobs 표의 문장 단위 갱신 — 호출마다 짧은 연결을 쓴다(스레드 안전)."""

    def __init__(self, workspace: RawWorkspace) -> None:
        self._workspace = workspace

    def insert_deduplicated(
        self, kind: str, input_fingerprint: str, total: int
    ) -> tuple[JobRecord, bool]:
        """한 트랜잭션에서 (활성 작업 찾기 → 없으면 삽입) — 동시 제출이 두 벌을 못 만든다."""
        placeholders = ", ".join("?" for _ in ACTIVE_JOB_STATUSES)
        connection = open_db(self._workspace.root / DB_NAME)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT * FROM jobs WHERE kind = ? AND input_fingerprint = ? "
                f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
                (kind, input_fingerprint, *ACTIVE_JOB_STATUSES),
            ).fetchone()
            if row is not None:
                connection.commit()
                return self._record(row), True
            number = connection.execute("SELECT COUNT(*) + 1 FROM jobs").fetchone()[0]
            record = JobRecord(
                job_id=f"job_{int(number):06d}",
                kind=kind,
                input_fingerprint=input_fingerprint,
                status="queued",
                progress_current=0,
                progress_total=total,
                error=None,
                cancellation_requested=False,
                created_at=_now(),
                updated_at=_now(),
            )
            connection.execute(
                "INSERT INTO jobs (job_id, kind, input_fingerprint, status, progress_current,"
                " progress_total, error, cancellation_requested, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                self._values(record),
            )
            connection.commit()
            return record, False
        finally:
            connection.close()

    def get(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise RawWorkspaceError(f"없는 작업 ID: {job_id}")
        return self._record(row)

    def list_jobs(self, limit: int = 50) -> tuple[JobRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def active_of_kinds(self, kinds: Collection[str]) -> tuple[JobRecord, ...]:
        """그 종류의 활성 작업 전부 — 최근 몇 개의 창이 아니라 상태로 찾는다(오래된 것이 먼저)."""
        kind_marks = ", ".join("?" for _ in kinds)
        status_marks = ", ".join("?" for _ in ACTIVE_JOB_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE kind IN ({kind_marks}) AND status IN ({status_marks})"
                " ORDER BY created_at, job_id",
                (*kinds, *ACTIVE_JOB_STATUSES),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def active(self) -> tuple[JobRecord, ...]:
        """종류를 가리지 않는 활성 작업 전부 — 상태로 찾는다(오래된 것이 먼저). 읽기만 한다."""
        status_marks = ", ".join("?" for _ in ACTIVE_JOB_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE status IN ({status_marks}) ORDER BY created_at, job_id",
                ACTIVE_JOB_STATUSES,
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def latest_of_kind(self, kind: str) -> JobRecord | None:
        """그 종류의 가장 최근 작업 — 최근 몇 개의 창이 아니라 종류로 찾는다."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE kind = ? ORDER BY created_at DESC, job_id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return None if row is None else self._record(row)

    def cancellation_requested(self, job_id: str) -> bool:
        return self.get(job_id).cancellation_requested

    def update(self, job_id: str, **changes: object) -> None:
        self._update(job_id, changes)

    def advance(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET progress_current = progress_current + 1, updated_at = ? "
                "WHERE job_id = ?",
                (_now(), job_id),
            )
            connection.commit()

    def request_cancel(self, job_id: str) -> JobRecord:
        """취소 요청을 남긴다 — 게이트는 작업 본문이 확인하고 멈춘다."""
        record = self.get(job_id)
        if record.status in ACTIVE_JOB_STATUSES and not record.cancellation_requested:
            self._update(job_id, {"status": "cancel_requested", "cancellation_requested": 1})
            return self.get(job_id)
        return record

    def requeue_for_resume(self, job_id: str, allowed: tuple[str, ...]) -> JobRecord | None:
        """재개 준비 — 허용 상태일 때만 queued로 되돌린다(조건부 한 문장, 경합 안전)."""
        placeholders = ", ".join("?" for _ in allowed)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE jobs SET status = 'queued', updated_at = ? "
                f"WHERE job_id = ? AND status IN ({placeholders})",
                (_now(), job_id, *allowed),
            )
            connection.commit()
        return self.get(job_id) if cursor.rowcount == 1 else None

    def recover_interrupted(self) -> int:
        """시작 시 복구 — 진행 중이던 작업을 재개 가능 상태로 돌린다.

        서버 시작 경로에서만 부른다. `open`이 이걸 호출하지 않는 것이 계약이다.
        """
        placeholders = ", ".join("?" for _ in ACTIVE_JOB_STATUSES)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE jobs SET status = 'interrupted', updated_at = ?, "
                f"error = COALESCE(error, '서버가 종료되어 중단되었다 — 재개 가능') "
                f"WHERE status IN ({placeholders})",
                (_now(), *ACTIVE_JOB_STATUSES),
            )
            connection.commit()
        return int(cursor.rowcount)

    def _update(self, job_id: str, changes: dict[str, object]) -> None:
        columns = ", ".join(f"{name} = ?" for name in changes)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE jobs SET {columns}, updated_at = ? WHERE job_id = ?",
                (*changes.values(), _now(), job_id),
            )
            connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with db_session(self._workspace.root / DB_NAME) as connection:
            yield connection

    @staticmethod
    def _values(record: JobRecord) -> tuple[object, ...]:
        return (
            record.job_id,
            record.kind,
            record.input_fingerprint,
            record.status,
            record.progress_current,
            record.progress_total,
            record.error,
            record.created_at,
            record.updated_at,
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=str(row["job_id"]),
            kind=str(row["kind"]),
            input_fingerprint=str(row["input_fingerprint"]),
            status=str(row["status"]),
            progress_current=int(row["progress_current"]),
            progress_total=int(row["progress_total"]),
            error=None if row["error"] is None else str(row["error"]),
            cancellation_requested=bool(row["cancellation_requested"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True)
class _Execution:
    run: Callable[[JobContext], None]


class JobManager:
    """작업공간 하나의 작업 관리자 — 제출·중복 결합·취소·재개·조회를 소유한다."""

    RESUMABLE_STATUSES: tuple[str, ...] = ("interrupted", "paused", "failed")

    def __init__(self, workspace: RawWorkspace) -> None:
        self._workspace = workspace
        self._store = JobStore(workspace)
        self._executions: dict[str, _Execution] = {}
        self._lock = threading.Lock()

    def submit(
        self, kind: str, input_fingerprint: str, run: Callable[[JobContext], None], total: int = 0
    ) -> JobHandle:
        """같은 활성 작업이 있으면 그 작업에 다시 붙는다 — 두 번 시작하지 않는다."""
        record, reattached = self._store.insert_deduplicated(kind, input_fingerprint, total)
        if reattached:
            return JobHandle(job_id=record.job_id, reattached=True)
        return self._launch(record.job_id, run)

    def resume(
        self, job_id: str, run: Callable[[JobContext], None], expected_fingerprint: str
    ) -> JobHandle:
        """명시적 재개 — 재개 가능 상태의 작업에 본문을 다시 묶는다(코디네이터 재결).

        작업 ID·진행은 유지하고, 지문은 **비지 않아야 하고** 기록된 것과 같아야
        하며, 활성·성공 상태면 거부한다. 상태 되돌리기는 한 문장의 조건부 갱신이라
        경합에서 두 본문이 동시에 묶일 수 없다 — 새로고침·열기가 이 길을 건드리지
        않는다.
        """
        if not expected_fingerprint.strip():
            raise RawWorkspaceError(
                "재개에는 현재 입력/설정 지문이 필요하다 — 빈 지문은 재결합하지 않는다"
            )
        record = self._store.get(job_id)
        if expected_fingerprint is not None and record.input_fingerprint != expected_fingerprint:
            raise RawWorkspaceError(
                f"재개 지문이 다르다: 작업 {record.input_fingerprint!r} / 요청 "
                f"{expected_fingerprint!r} — 입력이 바뀐 작업은 새로 제출해야 한다"
            )
        bound = self._store.requeue_for_resume(job_id, self.RESUMABLE_STATUSES)
        if bound is None:
            raise RawWorkspaceError(
                f"재개할 수 없는 상태다: {job_id}는 {record.status}다 "
                "(활성·성공·취소 작업은 재개 대상이 아니다)"
            )
        return self._launch(job_id, run)

    def _launch(self, job_id: str, run: Callable[[JobContext], None]) -> JobHandle:
        with self._lock:
            self._executions[job_id] = _Execution(run=run)
        thread = threading.Thread(target=self._execute, args=(job_id,), daemon=True, name=job_id)
        thread.start()
        return JobHandle(job_id=job_id, reattached=False)

    def _execute(self, job_id: str) -> None:
        with self._lock:
            execution = self._executions.get(job_id)
        try:
            if execution is None:
                self._settle(job_id, status="failed", error="작업 본문을 찾지 못했다")
                return
            if self._store.get(job_id).status not in ("queued", "running"):
                self._settle(job_id, status="cancelled")
                return
            self._store.update(job_id, status="running")
            context = JobContext(job_id, self._workspace)
            with cancellation_scope(context.check_cancel):
                execution.run(context)
            if self._cancellation_after_success(job_id):
                self._settle(job_id, status="cancelled")
            else:
                self._settle(job_id, status="succeeded", error=None)
        except JobCancelledError:
            self._settle(job_id, status="cancelled")
        except Exception as error:  # noqa: BLE001 - 오류 본문을 상태로 남긴다
            self._settle(job_id, status="failed", error=str(error)[:500])
        finally:
            with self._lock:
                self._executions.pop(job_id, None)

    def _settle(self, job_id: str, **changes: object) -> None:
        """끝 상태를 남긴다 — 끝내 못 남기면 스레드를 죽이지 않고 기록만 한다.

        일시 오류(OperationalError)만 같은 한도로 다시 시도한다. 그 밖의 sqlite
        오류는 다시 물어도 같은 답이 나올 것이므로 바로 기록한다. 어느 쪽이든
        상태는 running으로 남고, 서버 시작 복구(`recover_interrupted`)가 재개
        가능한 interrupted로 돌린다.
        """
        for attempt in range(1, FINAL_STATE_ATTEMPTS + 1):
            try:
                self._store.update(job_id, **changes)
                return
            except sqlite3.Error as error:
                transient = isinstance(error, sqlite3.OperationalError)
                if not transient or attempt == FINAL_STATE_ATTEMPTS:
                    log.error(
                        f"작업 {job_id}의 끝 상태({changes.get('status')})를 DB에 남기지 못했다: "
                        f"{error} — 다음 서버 시작 때 interrupted로 복구된다"
                    )
                    return
                time.sleep(FINAL_STATE_RETRY_SECONDS)

    def _cancellation_after_success(self, job_id: str) -> bool:
        """성공한 본문 뒤의 취소 확인 — 이 읽기가 죽어도 성공을 실패로 뒤집지 않는다.

        `_settle`과 같은 한도로 다시 읽고, 끝내 못 읽으면 취소 없음으로 친다 —
        읽기 실패를 본문 실패로 섞으면 성공이 실패로 뒤집힌다.
        """
        for attempt in range(1, FINAL_STATE_ATTEMPTS + 1):
            try:
                return self._store.get(job_id).cancellation_requested
            except sqlite3.Error as error:
                transient = isinstance(error, sqlite3.OperationalError)
                if not transient or attempt == FINAL_STATE_ATTEMPTS:
                    log.error(f"작업 {job_id}의 취소 요청을 읽지 못해 성공으로 남긴다: {error}")
                    return False
                time.sleep(FINAL_STATE_RETRY_SECONDS)
        return False

    def cancel(self, job_id: str) -> JobRecord:
        return self._store.request_cancel(job_id)

    def get(self, job_id: str) -> JobRecord:
        return self._store.get(job_id)

    def list_jobs(self, limit: int = 50) -> tuple[JobRecord, ...]:
        return self._store.list_jobs(limit)

    def latest_of_kind(self, kind: str) -> JobRecord | None:
        return self._store.latest_of_kind(kind)

    def active_of_kinds(self, kinds: Collection[str]) -> tuple[JobRecord, ...]:
        return self._store.active_of_kinds(kinds)

    def active(self) -> tuple[JobRecord, ...]:
        return self._store.active()

    def is_executing(self, job_id: str) -> bool:
        """이 관리자가 그 작업의 본문을 아직 쥐고 있는가 — 끝 상태를 정산할 때까지 참이다."""
        with self._lock:
            return job_id in self._executions

    def recover_interrupted(self) -> int:
        return self._store.recover_interrupted()
