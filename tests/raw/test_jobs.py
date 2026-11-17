"""A1 작업 관리 — 서버 소유·지속·중복 결합·취소 게이트·시작 시 복구(계획 §14.1).

작업 상태의 정본은 DB다. 화면(`gr.State`)이 아니라 서비스 인스턴스를 새로 띄워
(=서버 재시작) 상태가 살아있는지를 시험한다. 합성 작업 본문만 돌린다 — 네트워크 없음.
"""

from __future__ import annotations

import gc
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from vlogsrt.project.raw import jobs as jobs_module
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext, JobStore
from vlogsrt.project.raw.models import JobRecord, RawWorkspaceError
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace

TypePredicate = Callable[[], bool]


def wait_until(predicate: TypePredicate, timeout: float = 5.0) -> bool:
    """조건이 성립할 때까지 짧게 잔다 — 스레드 스케줄을 초 단위 잠으로 기다리지 않는다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주")
    return root


def _stepping_job(steps: int, gate: threading.Event) -> Callable[[JobContext], None]:
    """매 걸음 취소 게이트를 확인하는 합성 작업 — 이벤트가 열리기 전에 첫 걸음만 둔다."""

    def run(context: JobContext) -> None:
        context.set_total(steps)
        context.advance()
        gate.wait(timeout=10)
        for _ in range(steps - 1):
            context.check_cancel()
            context.advance()

    return run


def test_lifecycle_persists_progress_to_success(workspace: Path) -> None:
    service = raw.RawWorkspaceService()

    def run(context: JobContext) -> None:
        context.set_total(3)
        for _ in range(3):
            context.advance()

    handle = service.submit_job(workspace, "probe", "fp-1", run, total=3)
    assert not handle.reattached
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "succeeded")
    record = service.list_jobs(workspace)[0]
    assert record.status == "succeeded"
    assert record.progress_current == record.progress_total == 3
    assert record.error is None


def test_progress_is_durable_while_running(workspace: Path) -> None:
    """진행은 매 걸음 DB에 남는다 — 다른 연결(다른 화면 새로고침)이 중간 값을 읽는다."""
    service = raw.RawWorkspaceService()
    gate = threading.Event()
    handle = service.submit_job(workspace, "probe", "fp-1", _stepping_job(3, gate), total=3)
    reader = raw.RawWorkspaceService()
    assert wait_until(
        lambda: (
            reader.get_job(workspace, handle.job_id).status == "running"
            and reader.get_job(workspace, handle.job_id).progress_current == 1
        )
    )
    gate.set()
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "succeeded")


def test_latest_of_kind_finds_a_job_beyond_any_recency_window(workspace: Path) -> None:
    """종류로 찾는다 — 그 뒤로 다른 작업이 수백 개 쌓여도 마지막 그 종류 작업이 보인다(D53)."""
    service = raw.RawWorkspaceService()
    manager = service._jobs(workspace)
    assert manager.latest_of_kind("workspace_upkeep") is None
    first = service.submit_job(workspace, "workspace_upkeep", "fp-a", lambda context: None)
    assert wait_until(lambda: service.get_job(workspace, first.job_id).status == "succeeded")
    for index in range(60):
        service.submit_job(workspace, "probe", f"fp-{index}", lambda context: None)
    assert first.job_id not in {job.job_id for job in service.list_jobs(workspace)}
    found = manager.latest_of_kind("workspace_upkeep")
    assert found is not None and found.job_id == first.job_id


def test_the_same_active_job_reattaches_instead_of_duplicating(workspace: Path) -> None:
    """같은 (종류, 지문)의 활성 작업이 있으면 다시 붙는다 — 두 번 실행하지 않는다."""
    service = raw.RawWorkspaceService()
    gate = threading.Event()
    first = service.submit_job(workspace, "probe", "fp-1", _stepping_job(2, gate), total=2)
    again = service.submit_job(workspace, "probe", "fp-1", _stepping_job(2, gate), total=2)
    assert again.job_id == first.job_id
    assert again.reattached
    other = service.submit_job(workspace, "probe", "fp-2", _stepping_job(2, gate), total=2)
    assert other.job_id != first.job_id
    assert not other.reattached
    gate.set()
    assert wait_until(
        lambda: all(
            service.get_job(workspace, job_id).status == "succeeded"
            for job_id in (first.job_id, other.job_id)
        )
    )
    finished = service.submit_job(workspace, "probe", "fp-1", _stepping_job(2, gate), total=2)
    assert finished.job_id != first.job_id  # 끝난 작업은 다시 붙을 대상이 아니다


def test_cancellation_gates_the_worker_and_keeps_partial_progress(workspace: Path) -> None:
    """취소는 새 걸음을 멈춘다 — 이미 끝난 유효 결과는 보존한다(§14.1)."""
    service = raw.RawWorkspaceService()
    gate = threading.Event()
    handle = service.submit_job(workspace, "probe", "fp-1", _stepping_job(4, gate), total=4)
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).progress_current == 1)
    requested = service.cancel_job(workspace, handle.job_id)
    assert requested.status == "cancel_requested"
    assert requested.cancellation_requested
    gate.set()
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "cancelled")
    record = service.get_job(workspace, handle.job_id)
    assert record.progress_current == 1  # 취소 뒤에도 첫 걸음은 살아 있다


def test_cancelling_a_finished_job_is_a_noop(workspace: Path) -> None:
    service = raw.RawWorkspaceService()
    handle = service.submit_job(
        workspace, "probe", "fp-1", lambda context: context.advance(), total=1
    )
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "succeeded")
    record = service.cancel_job(workspace, handle.job_id)
    assert record.status == "succeeded"
    assert not record.cancellation_requested


def test_a_failing_job_records_its_error(workspace: Path) -> None:
    def run(context: JobContext) -> None:
        context.advance()
        raise RuntimeError("합성 오류 — 본문이 죽는다")

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-1", run, total=1)
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "failed")
    record = service.get_job(workspace, handle.job_id)
    assert "합성 오류" in (record.error or "")


def test_an_unknown_job_id_is_rejected(workspace: Path) -> None:
    with pytest.raises(RawWorkspaceError, match="없는 작업 ID"):
        raw.RawWorkspaceService().get_job(workspace, "job_999999")


def test_recovery_happens_only_at_server_startup(workspace: Path) -> None:
    """진행 중이던 작업은 `recover` 호출에서만 interrupted가 된다(§14.1).

    활성 상태(queued·running·cancel_requested)만 복구되고, 끝난 작업은 그대로
    둔다. 복구는 두 번째 호출에서는 아무 것도 바꾸지 않는다.
    """
    service = raw.RawWorkspaceService()
    gate = threading.Event()
    running = service.submit_job(workspace, "probe", "fp-run", _stepping_job(4, gate), total=4)
    cancelling = service.submit_job(workspace, "probe", "fp-stop", _stepping_job(4, gate), total=4)
    done = service.submit_job(
        workspace, "probe", "fp-done", lambda context: context.advance(), total=1
    )
    assert wait_until(lambda: service.get_job(workspace, done.job_id).status == "succeeded")
    service.cancel_job(workspace, cancelling.job_id)
    gate.set()
    assert wait_until(lambda: service.get_job(workspace, cancelling.job_id).status == "cancelled")
    # 세 스레드가 전부 끝나고 나서 상태를 되돌린다 — 되돌린 뒤 살아 있는 스레드가
    # 다시 succeeded를 적으면 복구 시험 자체가 흔들린다.
    assert wait_until(
        lambda: service.get_job(workspace, running.job_id).status in ("succeeded", "cancelled")
    )
    # 서버 재시작 시뮬레이션: 새 서비스 인스턴스. 크래시 흉내를 위해 DB 상태를
    # 진행 중으로 되돌린 뒤 — 이전 세션의 스레드는 이미 끝났다.
    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running' WHERE job_id IN (?, ?)",
            (running.job_id, cancelling.job_id),
        )
        connection.commit()
    restarted = raw.RawWorkspaceService()
    restarted.open_workspace(workspace)
    assert restarted.get_job(workspace, running.job_id).status == "running"  # 열기만으로 복구 없음
    assert restarted.recover_interrupted_jobs(workspace) == 2
    assert restarted.get_job(workspace, running.job_id).status == "interrupted"
    assert restarted.get_job(workspace, cancelling.job_id).status == "interrupted"
    assert restarted.get_job(workspace, done.job_id).status == "succeeded"
    assert restarted.recover_interrupted_jobs(workspace) == 0


def test_jobs_survive_a_full_service_restart(workspace: Path) -> None:
    """작업은 서비스 인스턴스가 아니라 DB에 산다 — gr.State가 정본일 수 없다."""
    first = raw.RawWorkspaceService()
    handle = first.submit_job(
        workspace, "probe", "fp-1", lambda context: context.advance(), total=1
    )
    assert wait_until(lambda: first.get_job(workspace, handle.job_id).status == "succeeded")
    second = raw.RawWorkspaceService()
    second.open_workspace(workspace)
    assert second.list_jobs(workspace)[0].job_id == handle.job_id
    assert second.list_jobs(workspace)[0].status == "succeeded"


def test_the_cancel_gate_raises_inside_the_job_body(workspace: Path) -> None:
    """게이트 예외는 JobCancelledError로 나타난다 — 상태 전이가 그걸 cancelled로 옮긴다."""
    gate = threading.Event()
    seen: list[str] = []

    def run(context: JobContext) -> None:
        context.set_total(2)
        context.advance()
        gate.wait(timeout=10)
        try:
            context.check_cancel()
        except JobCancelledError as error:
            seen.append(type(error).__name__)
            raise
        context.advance()

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-1", run, total=2)
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).progress_current == 1)
    service.cancel_job(workspace, handle.job_id)
    gate.set()
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "cancelled")
    assert seen == ["JobCancelledError"]


def test_cancel_during_a_blocking_callback_is_not_overwritten(workspace: Path) -> None:
    """루트 재현 — 본문이 게이트 확인 없이 끝나도, 그 사이의 취소는 성공을 덮지 못한다."""
    gate = threading.Event()

    def run(context: JobContext) -> None:
        context.set_total(2)
        context.advance()
        gate.wait(timeout=10)  # 이 blocking 동안 취소가 들어온다
        context.advance()  # 게이트를 확인하지 않고 마저 끝낸 형태의 본문

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-1", run, total=2)
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).progress_current == 1)
    service.cancel_job(workspace, handle.job_id)
    gate.set()
    assert wait_until(
        lambda: service.get_job(workspace, handle.job_id).status in ("cancelled", "succeeded")
    )
    assert service.get_job(workspace, handle.job_id).status == "cancelled"


# ── 명시적 재개 계약(코디네이터 msg_fd377f9e3740) ─────────────────────────────


def _interrupted_job(workspace: Path, service: raw.RawWorkspaceService) -> str:
    """진행 1/3까지 갔다가 서버가 죽은 흉내의 작업을 interrupted로 만든다."""
    gate = threading.Event()
    handle = service.submit_job(workspace, "probe", "fp-resume", _stepping_job(3, gate), total=3)
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).progress_current == 1)
    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'interrupted' WHERE job_id = ?", (handle.job_id,)
        )
        connection.commit()
    return handle.job_id


def test_resume_keeps_the_job_id_and_progress_and_finishes(workspace: Path) -> None:
    """재개는 같은 작업 ID·진행에서 이어 끝난다 — 두 번째 본문만 돈다."""
    service = raw.RawWorkspaceService()
    job_id = _interrupted_job(workspace, service)
    assert service.get_job(workspace, job_id).progress_current == 1

    def remainder(context: JobContext) -> None:
        context.advance()
        context.advance()

    handle = service.resume_job(workspace, job_id, remainder, expected_fingerprint="fp-resume")
    assert handle.job_id == job_id
    assert not handle.reattached
    assert wait_until(lambda: service.get_job(workspace, job_id).status == "succeeded")
    record = service.get_job(workspace, job_id)
    assert record.progress_current == 3  # 남은 두 걸음만 더했다 — 처음부터 다시 돌지 않는다
    assert record.error is None


def test_resume_refuses_changed_fingerprints(workspace: Path) -> None:
    """기록된 입력 지문이 다르면 재개하지 않는다 — 바뀐 입력은 새 제출이다."""
    service = raw.RawWorkspaceService()
    job_id = _interrupted_job(workspace, service)
    with pytest.raises(RawWorkspaceError, match="재개 지문이 다르다"):
        service.resume_job(workspace, job_id, lambda context: None, "다른-지문")


@pytest.mark.parametrize("terminal", ["succeeded", "cancelled", "running"])
def test_resume_refuses_active_or_settled_jobs(workspace: Path, terminal: str) -> None:
    """활성·성공·취소 작업은 재개 대상이 아니다 — 중복 본문이 생기지 않는다."""
    service = raw.RawWorkspaceService()
    gate = threading.Event()
    handle = service.submit_job(workspace, "probe", f"fp-{terminal}", _stepping_job(3, gate))
    if terminal == "succeeded":
        gate.set()
        assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "succeeded")
    elif terminal == "cancelled":
        service.cancel_job(workspace, handle.job_id)
        gate.set()
        assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "cancelled")
    else:  # running — 게이트를 닫아둔 채 재개 시도
        pass
    with pytest.raises(RawWorkspaceError, match="재개할 수 없는 상태"):
        service.resume_job(workspace, handle.job_id, lambda context: None, f"fp-{terminal}")
    gate.set()


def test_refresh_and_open_never_auto_resume(workspace: Path) -> None:
    """재개는 명시적 호출뿐 — 열기·상태 읽기가 interrupted 작업을 실행하지 않는다."""
    service = raw.RawWorkspaceService()
    job_id = _interrupted_job(workspace, service)
    for _ in range(3):
        service.open_workspace(workspace)
        service.list_jobs(workspace)
    assert service.get_job(workspace, job_id).status == "interrupted"
    assert service.get_job(workspace, job_id).progress_current == 1


def test_concurrent_first_opens_share_one_manager(workspace: Path) -> None:
    """루트 재현 — 동시 첫 열기가 관리자를 여럿 만들지 않는다(살아 있는 작업의
    시작 복구를 두 번째 관리자가 훔치는 사고의 뿌리)."""
    service = raw.RawWorkspaceService()
    barrier = threading.Barrier(8)

    def open_concurrently() -> None:
        barrier.wait(timeout=10)
        service.open_workspace(workspace)

    threads = [threading.Thread(target=open_concurrently) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(service._job_managers) == 1
    assert len(service._workspaces) == 1


def test_resume_requires_a_nonempty_fingerprint(workspace: Path) -> None:
    """빈 지문으로는 재결합하지 않는다 — 조용한 우회 금지(코디네이터 재결)."""
    service = raw.RawWorkspaceService()
    job_id = _interrupted_job(workspace, service)
    for empty in ("", "   "):
        with pytest.raises(RawWorkspaceError, match="지문이 필요하다"):
            service.resume_job(workspace, job_id, lambda context: None, empty)
    assert service.get_job(workspace, job_id).status == "interrupted"


def _open_descriptors() -> int:
    """열린 서술자 수 — Windows에는 `/dev/fd`가 없어 이 프로세스의 핸들 수를 센다(W-84a)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # 핸들은 64비트다 — 형을 적지 않으면 ctypes가 int로 잘라 호출이 실패(0)한다.
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        count = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return int(count.value)
    return len(os.listdir("/dev/fd"))


def _without_cyclic_gc(calls: Callable[[], None], rounds: int = 200) -> int:
    """sqlite3 연결은 순환 참조라 GC가 돌기 전엔 안 닫힌다 — GC를 멈추고 fd 증가를 잰다."""
    calls()  # 스키마 준비 같은 첫 호출의 일회성 fd를 기준선에서 뺀다
    gc.collect()
    gc.disable()
    try:
        before = _open_descriptors()
        for _ in range(rounds):
            calls()
        return _open_descriptors() - before
    finally:
        gc.enable()
        gc.collect()


def test_job_store_closes_every_connection(workspace: Path) -> None:
    """실측: 연결이 GC에 기대 닫히면 폴링 부모 여럿이 fd 256을 다 써서
    `unable to open database file`로 작업 스레드가 줄줄이 죽었다."""
    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-fd", lambda context: None)
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "succeeded")
    store = JobStore(RawWorkspace.open(workspace))

    def calls() -> None:
        store.get(handle.job_id)
        store.cancellation_requested(handle.job_id)
        store.update(handle.job_id, progress_total=1)
        store.advance(handle.job_id)
        store.list_jobs()

    # 새는 회귀는 라운드마다 호출 수만큼 fd를 늘린다(200라운드면 수백 개). 남은
    # 데몬 스레드가 연결을 몇 개 쥐고 있을 수 있어 상한은 그만큼 여유를 둔다.
    rounds = 200
    assert _without_cyclic_gc(calls, rounds=rounds) < rounds // 4


def test_store_connection_keeps_commit_and_rollback_semantics(workspace: Path) -> None:
    store = JobStore(RawWorkspace.open(workspace))
    with store._connect() as connection:
        connection.execute("INSERT INTO meta(key, value) VALUES('probe-commit', 'kept')")
    with pytest.raises(RuntimeError), store._connect() as connection:
        connection.execute("INSERT INTO meta(key, value) VALUES('probe-rollback', 'lost')")
        raise RuntimeError("본문 실패")
    with sqlite3.connect(workspace / DB_NAME) as check:
        keys = {row[0] for row in check.execute("SELECT key FROM meta WHERE key LIKE 'probe-%'")}
    assert keys == {"probe-commit"}


def test_an_unrecordable_failure_is_logged_not_raised_from_the_thread(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실패 상태조차 못 남기면 스레드가 중첩 트레이스백으로 죽었다 — 기록하고 끝낸다.

    작업은 running으로 남고, 서버 시작 복구가 interrupted로 돌린다.
    """
    thread_errors: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))
    logged: list[str] = []
    monkeypatch.setattr(jobs_module.log, "error", logged.append)
    monkeypatch.setattr(jobs_module, "FINAL_STATE_RETRY_SECONDS", 0.0)
    original = JobStore.update

    def failing_update(self: JobStore, job_id: str, **changes: object) -> None:
        if changes.get("status") == "failed":
            raise sqlite3.OperationalError("unable to open database file")
        original(self, job_id, **changes)

    monkeypatch.setattr(JobStore, "update", failing_update)

    def run(context: JobContext) -> None:
        raise sqlite3.OperationalError("unable to open database file")

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-unrecordable", run)
    assert wait_until(lambda: bool(logged))
    assert handle.job_id in logged[0]
    assert thread_errors == []
    assert service.get_job(workspace, handle.job_id).status == "running"


def test_a_transient_store_error_still_records_the_final_state(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs_module, "FINAL_STATE_RETRY_SECONDS", 0.0)
    original = JobStore.update
    failures = iter([True])

    def flaky_update(self: JobStore, job_id: str, **changes: object) -> None:
        if changes.get("status") == "failed" and next(failures, False):
            raise sqlite3.OperationalError("database is locked")
        original(self, job_id, **changes)

    monkeypatch.setattr(JobStore, "update", flaky_update)

    def run(context: JobContext) -> None:
        raise RuntimeError("합성 오류")

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-flaky", run)
    assert wait_until(lambda: service.get_job(workspace, handle.job_id).status == "failed")
    assert "합성 오류" in (service.get_job(workspace, handle.job_id).error or "")


def _job_status(workspace: Path, job_id: str) -> tuple[str, str | None]:
    """저장 계층을 우회한 직접 읽기 — 스토어 메서드가 흉내로 갈린 시험의 판정 보기."""
    with sqlite3.connect(workspace / DB_NAME) as connection:
        row = connection.execute(
            "SELECT status, error FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


@pytest.mark.parametrize("stage", ["status_read", "running_write"])
def test_a_pre_body_store_failure_is_settled_and_the_execution_entry_is_popped(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """본문 앞의 저장 호출(status 읽기·running 기록)이 죽어도 스레드는 죽지 않는다.

    그 전에는 try 밖이라 스레드가 죽어 작업이 queued에 묶인 채 남고 실행 항목도
    못 치웠다 — 이제 실패로 기록하고 항목을 치운다.
    """
    thread_errors: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))
    original_get, original_update = JobStore.get, JobStore.update

    def failing_get(self: JobStore, job_id: str) -> JobRecord:
        if stage == "status_read":
            raise sqlite3.OperationalError("unable to open database file")
        return original_get(self, job_id)

    def failing_update(self: JobStore, job_id: str, **changes: object) -> None:
        if stage == "running_write" and changes.get("status") == "running":
            raise sqlite3.OperationalError("unable to open database file")
        original_update(self, job_id, **changes)

    monkeypatch.setattr(JobStore, "get", failing_get)
    monkeypatch.setattr(JobStore, "update", failing_update)

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", f"fp-pre-body-{stage}", lambda context: None)
    assert wait_until(lambda: _job_status(workspace, handle.job_id)[0] == "failed")
    _, error = _job_status(workspace, handle.job_id)
    assert "unable to open database file" in (error or "")
    assert thread_errors == []
    assert wait_until(lambda: not service._jobs(workspace)._executions)


def test_a_non_operational_settle_error_is_logged_not_retried(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OperationalError가 아닌 sqlite 오류는 다시 물어도 같은 답이다 — 기록하고 끝낸다."""
    thread_errors: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))
    logged: list[str] = []
    monkeypatch.setattr(jobs_module.log, "error", logged.append)
    original = JobStore.update
    update_statuses: list[object] = []

    def malformed_update(self: JobStore, job_id: str, **changes: object) -> None:
        update_statuses.append(changes.get("status"))
        if changes.get("status") == "failed":
            raise sqlite3.DatabaseError("database disk image is malformed")
        original(self, job_id, **changes)

    monkeypatch.setattr(JobStore, "update", malformed_update)

    def run(context: JobContext) -> None:
        raise RuntimeError("합성 오류")

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-malformed", run)
    assert wait_until(lambda: bool(logged))
    assert handle.job_id in logged[0]
    assert thread_errors == []
    assert update_statuses == ["running", "failed"]  # 재시도하지 않고 바로 기록했다
    assert _job_status(workspace, handle.job_id)[0] == "running"


def test_a_failed_cancel_read_after_success_still_succeeds(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """성공한 본문 뒤의 취소 확인 읽기가 끝내 죽으면 — 성공을 실패로 뒤집지 않는다."""
    thread_errors: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))
    logged: list[str] = []
    monkeypatch.setattr(jobs_module.log, "error", logged.append)
    monkeypatch.setattr(jobs_module, "FINAL_STATE_RETRY_SECONDS", 0.0)
    body_done = threading.Event()
    original = JobStore.get

    def failing_get(self: JobStore, job_id: str) -> JobRecord:
        if body_done.is_set():
            raise sqlite3.OperationalError("database is locked")
        return original(self, job_id)

    monkeypatch.setattr(JobStore, "get", failing_get)

    def run(context: JobContext) -> None:
        body_done.set()

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-cancel-read", run)
    assert wait_until(lambda: _job_status(workspace, handle.job_id)[0] == "succeeded")
    assert thread_errors == []
    assert logged and handle.job_id in logged[0]


def test_a_cancel_request_survives_a_transient_cancel_read(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """취소 확인 읽기가 한 번 일시 실패하면 다시 읽는다 — 취소는 그대로 지켜진다."""
    monkeypatch.setattr(jobs_module, "FINAL_STATE_RETRY_SECONDS", 0.0)
    gate = threading.Event()
    body_done = threading.Event()
    original = JobStore.get
    failures = iter([True])

    def flaky_get(self: JobStore, job_id: str) -> JobRecord:
        if body_done.is_set() and next(failures, False):
            raise sqlite3.OperationalError("database is locked")
        return original(self, job_id)

    monkeypatch.setattr(JobStore, "get", flaky_get)

    def run(context: JobContext) -> None:
        context.set_total(1)
        context.advance()
        gate.wait(timeout=10)
        body_done.set()

    service = raw.RawWorkspaceService()
    handle = service.submit_job(workspace, "probe", "fp-cancel-flaky", run, total=1)
    assert wait_until(lambda: _job_status(workspace, handle.job_id)[0] == "running")
    requested = service.cancel_job(workspace, handle.job_id)
    assert requested.cancellation_requested
    gate.set()
    assert wait_until(lambda: _job_status(workspace, handle.job_id)[0] == "cancelled")


class _CountingContext:
    def __init__(self) -> None:
        self.job_id = "parent"
        self.writes: list[tuple[str, int]] = []
        self.polls = 0

    def check_cancel(self) -> None:
        self.polls += 1

    def set_total(self, total: int) -> None:
        self.writes.append(("total", total))

    def set_progress(self, current: int) -> None:
        self.writes.append(("progress", current))


def test_waiting_on_a_child_writes_progress_only_when_it_changes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """부모의 폴링은 매 0.2초 DB를 연다 — 바뀌지 않은 진행을 다시 쓰지 않는다."""
    monkeypatch.setattr(raw, "CHILD_JOB_POLL_SECONDS", 0.0)
    gate = threading.Event()
    service = raw.RawWorkspaceService()
    child = service.submit_job(workspace, "probe", "fp-child", _stepping_job(2, gate))
    context = _CountingContext()
    waiter = threading.Thread(
        target=service.wait_child_job, args=(workspace, child, context), daemon=True
    )
    waiter.start()
    assert wait_until(lambda: context.polls > 20)
    gate.set()
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    for kind in ("total", "progress"):
        values = [value for name, value in context.writes if name == kind]
        assert all(a != b for a, b in zip(values, values[1:], strict=False))
    assert len(context.writes) <= 6 < context.polls


def test_cancellation_of_a_parent_propagates_to_the_waited_child(monkeypatch):
    """부모 작업이 취소되면 기다리던 자식도 취소된다(옛 test_capcut_export에서 옮김)."""
    from types import SimpleNamespace

    service = raw.RawWorkspaceService()
    cancelled = []
    monkeypatch.setattr(service, "cancel_job", lambda root, job_id: cancelled.append(job_id))

    class CancelledContext:
        def check_cancel(self):
            raise JobCancelledError("parent")

    with pytest.raises(JobCancelledError):
        service.wait_child_job(
            Path("/synthetic"), SimpleNamespace(job_id="child"), CancelledContext()
        )
    assert cancelled == ["child"]
