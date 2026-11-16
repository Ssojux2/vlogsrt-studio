"""job 실행 소유권의 회귀 시험 — 실제 진입점 스크립트를 subprocess로 띄운다.

같은 job의 동시 실행은 두 번째부터 이미 실행 중(exit 3)으로 물러난다(S2A-1).
잠금 도우미만 직접 만지는 시험으로는 진입점의 순서 — 소유권 확보가 workspace
생성·스테이징·엔진 호출보다 앞선다는 계약 — 을 못 본다. 그래서 실제
`macos/scripts/vlogsrt-engine.py`를 `sys.executable`로 띄우고 `vlogsrt.cli`를
통제된 가짜로 바꿔 첫 다운스트림 호출 안에서 막는다. 가짜는 제어 파일로
풀어주고, 유료 API도 진짜 설정도 없다. 도달 증명은 호출 개수 델타와 argv
귀속으로 한다 — 앞선 실행의 잔여 호출을 새 실행으로 착각하지 않는다. 시간
추측은 없고, 띄운 프로세스는 반드시 회수한다.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from tests._dotenv_guard import (
    CANARY_VALUE,
    CHILD_BOOTSTRAP_FAILED_EXIT,
    CHILD_GUARD_BREAK,
    CHILD_GUARD_PROOF,
    emit_child_guard,
    preflight_child_guard,
    read_child_proofs,
    stage_canary_standin,
)

# 개발 엔진(`macos/scripts/`)은 POSIX 전용이다 — `fcntl` 잠금 · zsh 설치 스크립트. Windows에는 그
# 엔진이 없다(작업대는 `vlogsrt/filelock.py`의 msvcrt 길을 쓴다, W-84a).
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 전용 개발 엔진(macos/scripts)")

_ENGINE_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py"
_INSTALLER_SCRIPT = (
    Path(__file__).resolve().parents[2] / "macos" / "scripts" / "install-development-engine.sh"
)
_ALREADY_RUNNING_LINE = "vlogsrt-engine: job already running (duplicate request rejected)"
_STATUS_PREFIX = "VLOGSRT-ENGINE-STATUS"
_WAIT_SECONDS = 30.0

# 통제된 vlogsrt.cli — 모든 호출을 기록하고 release 제어 파일이 생길 때까지
# 첫 호출 안에서 기다린다. spawn-child가 있으면 close_fds=False로 손자를 남긴다:
# 잠금 서술자가 상속 가능으로 열려 있으면 손자에게 샌다(상속 결함의 검출기).
_FAKE_CLI_SOURCE = '''\
"""테스트용 vlogsrt.cli — 진입점 시험이 통제하는 가짜 엔진 CLI다."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_CONTROL = Path(os.environ["FAKE_ENGINE_CONTROL"])


def main(argv):
    with (_CONTROL / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv}, ensure_ascii=False) + "\\n")
    if (_CONTROL / "spawn-child").is_file() and not (_CONTROL / "child.pid").exists():
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            close_fds=False,
        )
        (_CONTROL / "child.pid").write_text(str(child.pid), encoding="utf-8")
    deadline = time.monotonic() + 60.0
    while not (_CONTROL / "release").exists():
        if time.monotonic() > deadline:
            return 1
        time.sleep(0.01)
    if (_CONTROL / "fail").exists():
        return 1
    return 0
'''


class _FakeCli:
    """가짜 vlogsrt 패키지와 제어 파일의 주인 — 자식 환경만 꾸민다(전역은 안 고친다).

    root는 자식의 cwd이자 자식 보호(sitecustomize)의 등록 뿌리다. stand-in은
    등록 밖 `sealed/`에 둔다 — 자식이 보호 없이 그 철자에 닿으면 증명이 거짓으로
    드러난다(카나리 내용은 증명에 절대 없다).
    """

    def __init__(self, root: Path, *, spawn_child: bool = False) -> None:
        self.root = root
        self.standin = stage_canary_standin(root.parent / "sealed")
        self.proof = root / "guard-proof.jsonl"
        package = root / "vlogsrt"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "cli.py").write_text(_FAKE_CLI_SOURCE, encoding="utf-8")
        if spawn_child:
            (root / "spawn-child").write_text("", encoding="utf-8")
        self._guard_env = emit_child_guard(
            root, allow_roots=(root,), standin=self.standin, proof=self.proof
        )

    def broken_guard_env(self, broken_proof: Path) -> dict[str, str]:
        """bootstrap을 일부러 부순 자식 환경 — 실패 닫힘 증명용 주입구다."""
        return {**self.env, CHILD_GUARD_BREAK: "1", CHILD_GUARD_PROOF: str(broken_proof)}

    @property
    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.root) + (os.pathsep + existing if existing else "")
        env["FAKE_ENGINE_CONTROL"] = str(self.root)
        env.update(self._guard_env)
        return env

    def calls(self) -> list[dict]:
        log = self.root / "calls.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def wait_for_calls(self, count: int) -> list[dict]:
        """다운스트림 호출 count번째가 기록될 때까지 기다린다 — 실행 도달의 동기화 신호."""
        deadline = time.monotonic() + _WAIT_SECONDS
        while True:
            calls = self.calls()
            if len(calls) >= count:
                return calls
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"엔진 호출 {count}번째가 오지 않았다 — 지금 {len(calls)}번: {calls}"
                )
            time.sleep(0.01)

    def release(self, *, fail: bool = False) -> None:
        # fail 표식을 먼저 남긴다 — release만 먼저 보이면 성공 경로가 열린다.
        if fail:
            (self.root / "fail").write_text("", encoding="utf-8")
        (self.root / "release").write_text("", encoding="utf-8")

    def clear_release(self) -> None:
        for name in ("release", "fail"):
            (self.root / name).unlink(missing_ok=True)


@pytest.fixture()
def control(tmp_path: Path) -> _FakeCli:
    return _FakeCli(tmp_path / "control")


def _spawn_engine(
    control: _FakeCli,
    job_dir_text: str,
    *,
    job_id: str = "job-1",
    cwd: Path | None = None,
    status_file: Path | None = None,
) -> subprocess.Popen[bytes]:
    argv = [sys.executable, str(_ENGINE_SCRIPT), "analyze", job_id, job_dir_text]
    if status_file is not None:
        argv.append(str(status_file))
    # 자식 cwd의 기본은 통제 뿌리다 — 실제 저장소에서 띄우지 않는다.
    child_cwd = cwd if cwd is not None else control.root
    # 보호 사전확인 게이트 — sitecustomize가 없거나 깨졌으면 payload를 띄우지 않는다.
    preflight_child_guard(control.env, cwd=child_cwd, expected_package_root=control.root)
    return subprocess.Popen(
        argv,
        cwd=child_cwd,
        env=control.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _run_engine(
    control: _FakeCli,
    job_dir_text: str,
    *,
    job_id: str = "job-1",
    cwd: Path | None = None,
    status_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(_ENGINE_SCRIPT), "analyze", job_id, job_dir_text]
    if status_file is not None:
        argv.append(str(status_file))
    child_cwd = cwd if cwd is not None else control.root
    preflight_child_guard(control.env, cwd=child_cwd, expected_package_root=control.root)
    return subprocess.run(
        argv,
        cwd=child_cwd,
        env=control.env,
        capture_output=True,
        text=True,
        timeout=_WAIT_SECONDS,
    )


def _await_execution(control: _FakeCli, calls_before: int, job_dir: Path) -> dict:
    """새 프로세스의 첫 다운스트림 호출을 기다린다 — 델타와 argv 귀속으로 증명한다.

    개수뿐인 대기는 앞선 실행의 잔여 호출(release 뒤의 inspect 등)을 새 실행의
    도달로 착각할 수 있다. 스폰 전 개수를 찍어두고 +1을 기다린 뒤, 그 호출의
    argv가 이 job 디렉터리를 가리키는지까지 본다.
    """
    calls = control.wait_for_calls(calls_before + 1)
    latest = calls[-1]
    assert str(job_dir.resolve()) in " ".join(latest["argv"]), latest
    return latest


def _settle(control: _FakeCli, process: subprocess.Popen[bytes]) -> None:
    """어떤 경로로 끝나도 프로세스를 회수한다 — 놓아두면 잠금이 다음 시험을 오염시킨다."""
    control.release()
    try:
        process.wait(timeout=_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_WAIT_SECONDS)


def _snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    """job 트리의 관측 가능한 상태 — 파일 바이트와 디렉터리 자리까지 남긴다."""
    state: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_dir():
            state[relative] = ("dir", b"")
        elif path.is_file():
            state[relative] = ("file", path.read_bytes())
    return state


def test_second_request_for_a_held_job_exits_three_without_touching_it(
    tmp_path: Path, control: _FakeCli
) -> None:
    """같은 job의 중복 요청은 exit 3으로 물러난다 — 실행도 기록 변화도 없이."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    held_job = jobs / "job-1"
    held_job.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(held_job, target_is_directory=True)
    # 같은 글자 job_id라도 다른 디렉터리는 다른 job이다 — 키는 경로다.
    distinct_job = tmp_path / "elsewhere" / "job-1"
    distinct_job.mkdir(parents=True)

    calls_before = len(control.calls())
    holder = _spawn_engine(control, str(held_job))
    distinct: subprocess.Popen[bytes] | None = None
    try:
        # 첫 실행이 소유권을 쥐고 첫 다운스트림 호출 안에 있다
        _await_execution(control, calls_before, held_job)
        before = _snapshot(held_job)

        duplicate = _run_engine(control, str(alias))
        assert duplicate.returncode == 3, duplicate.stderr
        assert _ALREADY_RUNNING_LINE in duplicate.stderr
        # symlink 별칭이 아니어도 같은 job이다 — 상대 경로 표기도 충돌한다.
        relative = _run_engine(control, "jobs/job-1", cwd=tmp_path)
        assert relative.returncode == 3, relative.stderr
        assert _ALREADY_RUNNING_LINE in relative.stderr

        assert _snapshot(held_job) == before  # 중복은 아무 것도 만들지 않고 고치지 않는다
        assert len(control.calls()) == 1  # 두 번째 실행은 없다
        assert holder.poll() is None  # 정상 첫 실행을 기다리게 하거나 죽이지 않는다

        distinct_calls_before = len(control.calls())
        distinct = _spawn_engine(control, str(distinct_job))
        _await_execution(control, distinct_calls_before, distinct_job)  # 다른 job은 함께 돈다

        control.release()
        assert holder.wait(timeout=_WAIT_SECONDS) == 0
        assert distinct.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, holder)
        if distinct is not None:
            _settle(control, distinct)


def test_lock_releases_after_successful_and_failed_runs(tmp_path: Path, control: _FakeCli) -> None:
    """소유권은 성공·실패한 실행 뒤에도 돌아온다 — 같은 job이 다시 돈다.

    취소된 실행의 잠금 해제는 진짜 오프라인 경로에서 본다
    (test_engine_entrypoint.py — 취소된 실행도 결국 실패 종료라 여기와 같은 길이다).
    """
    job = tmp_path / "job"
    job.mkdir()

    calls_before = len(control.calls())
    first = _spawn_engine(control, str(job))
    try:
        _await_execution(control, calls_before, job)
        control.release()
        assert first.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, first)

    # 실패한 실행 — 가짜 CLI가 비0을 돌려주면 진입점은 1로 끝난다.
    control.clear_release()
    calls_before = len(control.calls())
    failed = _spawn_engine(control, str(job))
    try:
        _await_execution(control, calls_before, job)
        control.release(fail=True)
        assert failed.wait(timeout=_WAIT_SECONDS) == 1
    finally:
        _settle(control, failed)

    # 두 경우 뒤에도 재획득은 즉시 된다.
    control.clear_release()
    calls_before = len(control.calls())
    reacquired = _spawn_engine(control, str(job))
    try:
        _await_execution(control, calls_before, job)
        control.release()
        assert reacquired.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, reacquired)


def test_killed_process_releases_the_lock(tmp_path: Path, control: _FakeCli) -> None:
    """비정상 종료로 사라진 실행이 job을 영구 잠그지 않는다."""
    job = tmp_path / "job"
    job.mkdir()

    calls_before = len(control.calls())
    crashed = _spawn_engine(control, str(job))
    reacquired: subprocess.Popen[bytes] | None = None
    try:
        _await_execution(control, calls_before, job)
        crashed.kill()
        assert crashed.wait(timeout=_WAIT_SECONDS) == -9

        reacquired_calls_before = len(control.calls())
        reacquired = _spawn_engine(control, str(job))
        _await_execution(control, reacquired_calls_before, job)  # 커널이 잠금을 이미 돌려줬다
        control.release()
        assert reacquired.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, crashed)
        if reacquired is not None:
            _settle(control, reacquired)


def test_engine_crash_frees_the_lock_even_while_a_child_outlives_it(tmp_path: Path) -> None:
    """잠금 서술자가 자식에게 상속되지 않는다 — 손자가 살아 있어도 재획득된다.

    손자는 close_fds=False로 열어 상속 가능한 서술자가 있다면 반드시 물려받게
    한다. 잠금 서술자가 샜다면 부모를 죽여도 손자가 소유권을 쥐고 있어 다음
    실행은 exit 3으로 물러나야 한다 — 도달 증명이 그 반대를 못 박는다.
    """
    control = _FakeCli(tmp_path / "control", spawn_child=True)
    job = tmp_path / "job"
    job.mkdir()

    calls_before = len(control.calls())
    crashed = _spawn_engine(control, str(job))
    followup: subprocess.Popen[bytes] | None = None
    child_pid = 0
    try:
        child_pid_file = control.root / "child.pid"
        deadline = time.monotonic() + _WAIT_SECONDS
        while not child_pid_file.is_file():
            assert time.monotonic() <= deadline, "손자가 생기지 않았다"
            time.sleep(0.01)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        _await_execution(control, calls_before, job)
        crashed.kill()  # 부모를 죽인다 — 손자는 살아 있다
        assert crashed.wait(timeout=_WAIT_SECONDS) == -9
        os.kill(child_pid, 0)  # 손자는 여전히 산다

        followup_calls_before = len(control.calls())
        followup = _spawn_engine(control, str(job))
        _await_execution(control, followup_calls_before, job)  # 서술자가 샜다면 손자가 쥔다
        control.release()
        assert followup.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, crashed)
        if followup is not None:
            _settle(control, followup)
        if child_pid:
            os.kill(child_pid, 9)
            time.sleep(0.05)


def test_missing_job_root_is_rejected_without_creating_anything(
    tmp_path: Path, control: _FakeCli
) -> None:
    """없는 job 루트는 소유권보다 먼저 정중히 거절한다 — workspace를 만들지 않는다."""
    missing = tmp_path / "no-such-job"
    result = _run_engine(control, str(missing))
    assert result.returncode == 2, result.stderr
    assert not missing.exists()
    assert control.calls() == []  # 엔진 호출도 없다


def test_children_run_guarded_from_a_synthetic_cwd(tmp_path: Path, control: _FakeCli) -> None:
    """자식은 보호가 켜진 채 임시 cwd에서 돈다 — vlogsrt도 통제 뿌리에서 온다.

    sitecustomize 증명이 없으면 보호가 자식에 실리지 않은 것이다(RED) —
    인터프리터가 sitecustomize 예외를 삼키므로 증명 부재를 실패로 다룬다.
    증명은 참/거짓과 경로뿐이고 카나리 값은 어디에도 없다.
    """
    job = tmp_path / "job"
    job.mkdir()
    calls_before = len(control.calls())
    holder = _spawn_engine(control, str(job))
    try:
        _await_execution(control, calls_before, job)
        control.release()
        assert holder.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, holder)
    proofs = read_child_proofs(control.proof)
    assert proofs, "자식 보호 증명이 없다 — sitecustomize가 자식에 실리지 않았다"
    for record in proofs:
        assert record["blocked_read"] is True
        assert record["blocked_read_bytes"] is True
        assert record["blocked_path_open"] is True
        assert record["blocked_io_open"] is True
        assert record["blocked_os_open"] is True
        assert record["probe_miss"] is True
        assert record["socket_refused"] is True
        assert record["connect_refused"] is True
        assert record["cwd"] == str(control.root.resolve())
        assert record["vlogsrt_file"].startswith(str(control.root.resolve()))
    assert CANARY_VALUE not in control.proof.read_text(encoding="utf-8")


def test_broken_child_guard_bootstrap_prevents_the_payload(
    tmp_path: Path, control: _FakeCli
) -> None:
    """보호 bootstrap이 부서지면 payload는 도직전에 끝난다(실패 닫힘).

    해석기는 sitecustomize 안의 예외를 조용히 삼키므로, bootstrap은 스스로
    프로세스를 끝내야 한다 — 엔진 호출 기록(calls.jsonl)이 늘지 않는 것이
    payload가 도 직전에 막혔다는 증거다. 부서진 bootstrap의 사실은 별도
    증명 파일에 stage로 남는다.
    """
    job = tmp_path / "job"
    job.mkdir()
    broken_proof = tmp_path / "broken-guard-proof.jsonl"
    result = subprocess.run(
        [sys.executable, str(_ENGINE_SCRIPT), "analyze", "job-1", str(job)],
        cwd=control.root,
        env=control.broken_guard_env(broken_proof),
        capture_output=True,
        text=True,
        timeout=_WAIT_SECONDS,
    )
    assert result.returncode == CHILD_BOOTSTRAP_FAILED_EXIT, result.stderr
    assert control.calls() == []  # payload marker — 엔진 호출은 한 번도 없다
    assert broken_proof.is_file()
    broken = broken_proof.read_text(encoding="utf-8")
    assert '"bootstrap": "failed"' in broken
    assert '"stage": "install"' in broken
    assert CANARY_VALUE not in broken


def test_broken_bootstrap_still_terminates_when_the_proof_write_fails(
    tmp_path: Path, control: _FakeCli
) -> None:
    """진단 기록을 못 남겨도 종료는 무조건이다 — 기록 실패가 payload를 열지 않는다."""
    job = tmp_path / "job"
    job.mkdir()
    unreachable_proof = tmp_path / "absent-dir" / "proof.jsonl"
    result = subprocess.run(
        [sys.executable, str(_ENGINE_SCRIPT), "analyze", "job-1", str(job)],
        cwd=control.root,
        env={
            **control.env,
            CHILD_GUARD_BREAK: "1",
            CHILD_GUARD_PROOF: str(unreachable_proof),
        },
        capture_output=True,
        text=True,
        timeout=_WAIT_SECONDS,
    )
    assert result.returncode == CHILD_BOOTSTRAP_FAILED_EXIT, result.stderr
    assert control.calls() == []  # payload는 여전히 도 직전에 끝났다
    assert not unreachable_proof.parent.exists()  # 기록은 못 남겼다 — 그래도 끝났다


def test_launch_gate_refuses_when_the_child_guard_bootstrap_is_missing(
    tmp_path: Path, control: _FakeCli
) -> None:
    """sitecustomize가 없으면 해석기가 조용히 무시한다 — 게이트가 payload를 막는다."""
    (control.root / "sitecustomize.py").unlink()
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(RuntimeError, match="사전확인 실패"):
        _run_engine(control, str(job))
    assert control.calls() == []  # payload는 띄우지 않았다


def test_launch_gate_refuses_when_the_child_guard_bootstrap_is_syntactically_broken(
    tmp_path: Path, control: _FakeCli
) -> None:
    """문법이 깨진 sitecustomize도 조용히 무시된다 — 게이트가 payload를 막는다."""
    (control.root / "sitecustomize.py").write_text("def broken(:\n", encoding="utf-8")
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(RuntimeError, match="사전확인 실패"):
        _run_engine(control, str(job))
    assert control.calls() == []  # payload는 띄우지 않았다


# ─────────────────────────────────────────────────────────────
#  개발 실행기의 소유된 상태 기록 — 이 호출에 묶인 정확히 한 줄
# ─────────────────────────────────────────────────────────────


def _status_path(tmp_path: Path, token: str) -> Path:
    """실행기가 만드는 모양의 상태 경로 — 호출마다 고유한 디렉터리 안에 있다."""
    directory = tmp_path / "status" / f"vlogsrt-status-{token}"
    directory.mkdir(parents=True)
    return directory / f"{token}.status"


def test_entrypoint_writes_this_invocations_status_record(tmp_path: Path) -> None:
    """넷째 인자를 받은 실행은 정확히 한 줄 `VLOGSRT-ENGINE-STATUS <token> <code>`를 남긴다."""
    control = _FakeCli(tmp_path / "control")
    job = tmp_path / "job"
    job.mkdir()
    token = "token-alpha"
    status_file = _status_path(tmp_path, token)

    calls_before = len(control.calls())
    first = _spawn_engine(control, str(job), status_file=status_file)
    try:
        _await_execution(control, calls_before, job)
        control.release()
        assert first.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, first)
    assert status_file.read_text(encoding="utf-8") == f"{_STATUS_PREFIX} {token} 0\n"
    assert list(status_file.parent.iterdir()) == [status_file]  # 부분 기록은 남지 않는다

    # 중복(3)도 같은 채널로 나간다 — 기록은 job 상태 밖(임시)이니 중복 기록 아니다.
    control.clear_release()
    holder_token = "token-holder"
    holder_status = _status_path(tmp_path, holder_token)
    calls_before = len(control.calls())
    holder = _spawn_engine(control, str(job), status_file=holder_status)
    try:
        _await_execution(control, calls_before, job)
        job_before_duplicate = _snapshot(job)
        duplicate_status = _status_path(tmp_path, "token-duplicate")
        duplicate = _run_engine(control, str(job), status_file=duplicate_status)
        # 4-인자 계약 — 프로세스는 전달 성공으로 0, 안쪽 중복(3)은 기록으로 나간다.
        assert duplicate.returncode == 0, duplicate.stderr
        assert (
            duplicate_status.read_text(encoding="utf-8") == f"{_STATUS_PREFIX} token-duplicate 3\n"
        )
        # 중복의 기록은 job 상태 밖(임시)에만 생긴다 — job 트리는 그대로다.
        assert _snapshot(job) == job_before_duplicate
        assert not holder_status.exists()  # 홀더는 아직 실행 중 — 기록은 종료 때 생긴다
        control.release()
        assert holder.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, holder)
    assert holder_status.read_text(encoding="utf-8") == f"{_STATUS_PREFIX} {holder_token} 0\n"


def test_status_record_never_overwrites_a_file_created_during_the_run(
    tmp_path: Path, control: _FakeCli
) -> None:
    """검증 뒤 실행 중 그 자리에 파일이 생기면 기록은 실패(1)다 — 남의 바이트를 지키며."""
    job = tmp_path / "job"
    job.mkdir()
    status_file = _status_path(tmp_path, "hijacked")

    calls_before = len(control.calls())
    holder = _spawn_engine(control, str(job), status_file=status_file)
    try:
        _await_execution(control, calls_before, job)
        status_file.write_text("occupied-during-run", encoding="utf-8")
        control.release()
        # 안쪽 실행은 0이었지만 전달이 실패했다 — 프로세스는 1로 끝난다.
        assert holder.wait(timeout=_WAIT_SECONDS) == 1
        assert status_file.read_text(encoding="utf-8") == "occupied-during-run"
    finally:
        _settle(control, holder)


def test_status_token_must_be_alphanumeric(tmp_path: Path, control: _FakeCli) -> None:
    """token에 공백·개행이 섞이면 계약 위반이다 — 해석기는 정확히 한 줄만 읽는다."""
    job = tmp_path / "job"
    job.mkdir()
    spaced = tmp_path / "status" / "vlogsrt-status-bad token" / "bad token.status"
    spaced.parent.mkdir(parents=True)

    result = _run_engine(control, str(job), status_file=spaced)
    assert result.returncode == 2, result.stderr
    assert not spaced.exists()
    assert not (job / "workspace").exists()


def test_entrypoint_without_status_argument_writes_nothing(tmp_path: Path) -> None:
    """3-인자 호출(번들·직접 계약)은 기록을 남기지 않는다 — 종료 코드 계약만 있다."""
    control = _FakeCli(tmp_path / "control")
    job = tmp_path / "job"
    job.mkdir()
    status_area = tmp_path / "status"
    status_area.mkdir()

    calls_before = len(control.calls())
    plain = _spawn_engine(control, str(job))
    try:
        _await_execution(control, calls_before, job)
        control.release()
        assert plain.wait(timeout=_WAIT_SECONDS) == 0
    finally:
        _settle(control, plain)
    assert list(status_area.iterdir()) == []


def test_relative_status_argument_is_a_contract_violation(tmp_path: Path) -> None:
    """상태 기록은 실행기가 만든 절대 임시 경로로만 온다 — 상대 경로는 2다."""
    control = _FakeCli(tmp_path / "control")
    job = tmp_path / "job"
    job.mkdir()
    result = _run_engine(control, str(job), status_file=Path("relative.status"))
    assert result.returncode == 2, result.stderr
    # 자식 cwd(통제 뿌리)에도, 옛 자리(부모 cwd)에도 그 파일은 없다.
    assert not (control.root / "relative.status").exists()
    assert not Path("relative.status").exists()
    assert not (job / "workspace").exists()


def test_status_destination_inside_the_job_tree_is_rejected(
    tmp_path: Path, control: _FakeCli
) -> None:
    """상태 기록이 job 안을 가리키면 계약 위반(2) — 중복 판정이 남의 파일을 덮어쓰지 않는다."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    job = jobs / "job-1"
    job.mkdir()
    # 모양은 맞지만 job 상태 안이다 — 검증은 소유권 확보보다 먼저다.
    inside = job / "vlogsrt-status-inside" / "inside.status"
    inside.parent.mkdir()
    before = _snapshot(job)

    result = _run_engine(control, str(job), status_file=inside)
    assert result.returncode == 2, result.stderr
    assert _snapshot(job) == before  # 아무 것도 만들지 않고 쓰지도 않는다
    assert control.calls() == []


def test_status_destination_that_already_exists_is_never_replaced(
    tmp_path: Path, control: _FakeCli
) -> None:
    """이미 있는 상태 경로는 절대 덮어쓰지 않는다 — 남의 파일이 그대로다."""
    job = tmp_path / "job"
    job.mkdir()
    existing = _status_path(tmp_path, "occupied")
    existing.write_text("do-not-touch", encoding="utf-8")

    result = _run_engine(control, str(job), status_file=existing)
    assert result.returncode == 2, result.stderr
    assert existing.read_text(encoding="utf-8") == "do-not-touch"
    assert not (job / "workspace").exists()


def test_status_destination_with_wrong_shape_is_rejected(tmp_path: Path, control: _FakeCli) -> None:
    """이 호출 전용 모양(vlogsrt-status-<token>/<token>.status)이 아니면 2다."""
    job = tmp_path / "job"
    job.mkdir()
    plain = tmp_path / "plain.status"

    result = _run_engine(control, str(job), status_file=plain)
    assert result.returncode == 2, result.stderr
    assert not plain.exists()
    assert not (job / "workspace").exists()


def test_installed_wrapper_forwards_the_status_argument(tmp_path: Path) -> None:
    """설치된 개발 래퍼는 넷째 인자를 있을 때만 진입점으로 넘긴다(S2A-1 상태 채널)."""
    import stat

    repo = tmp_path / "repo"
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='fake'\n", encoding="utf-8")
    python.write_text(
        '#!/bin/zsh\nprint -r -- "$@" >> "${0:A:h:h:h}/calls.log"\n',
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    destination = tmp_path / "scripts"
    subprocess.run([str(_INSTALLER_SCRIPT), str(repo), str(destination)], check=True)
    wrapper = destination / "vlogsrt-engine"
    job = tmp_path / "job"
    job.mkdir()
    status_file = _status_path(tmp_path, "wrapped-token")
    env = {**os.environ, "VLOGSRT_OFFLINE": "1"}

    subprocess.run([str(wrapper), "analyze", "job-1", str(job)], check=False, env=env)
    subprocess.run(
        [str(wrapper), "analyze", "job-1", str(job), str(status_file)],
        check=False,
        env=env,
    )
    engine_script = str(repo / "macos/scripts/vlogsrt-engine.py")
    assert (repo / "calls.log").read_text(encoding="utf-8").splitlines() == [
        f"{engine_script} analyze job-1 {job}",
        f"{engine_script} analyze job-1 {job} {status_file}",
    ]


# ─────────────────────────────────────────────────────────────
#  잠금의 OS 오류 판정 — 충돌(EAGAIN)만 중복, 그 밖은 실행 오류
# ─────────────────────────────────────────────────────────────


def _load_engine_module() -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("vlogsrt_engine_lock_probe", _ENGINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lock_os_errors_are_execution_failures_not_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EAGAIN만 '누가 쥐고 있다'다 — EBADF·EIO를 중복으로 입히면 없는 실행자를 지어낸다."""
    engine = _load_engine_module()
    import fcntl

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    real_flock = fcntl.flock
    real_open = os.open
    opened_fds: list[int] = []

    def recording_open(path: object, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        opened_fds.append(fd)
        return fd

    monkeypatch.setattr(os, "open", recording_open)

    def assert_closed(fd: int) -> None:
        with pytest.raises(OSError) as failure:
            os.fstat(fd)
        assert failure.value.errno == errno.EBADF  # 닫혔다 — 실패한 획득이 서술자를 흘리지 않았다

    def refuse(_fd: int, operation: int) -> None:
        raise OSError(errno.EIO, "injected lock failure")

    monkeypatch.setattr(fcntl, "flock", refuse)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 1
    assert not (job_dir / "workspace").exists()  # 소유권 실패 — 아무것도 만들지 않았다
    assert_closed(opened_fds[-1])

    # 같은 실패가 set_inheritable에서 나도 서술자는 닫힌다.
    def refuse_inheritable(_fd: int, _inheritable: bool) -> None:
        raise OSError(errno.EIO, "injected inheritable failure")

    real_set_inheritable = os.set_inheritable
    monkeypatch.setattr(fcntl, "flock", lambda fd, operation: None)
    monkeypatch.setattr(os, "set_inheritable", refuse_inheritable)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 1
    assert_closed(opened_fds[-1])

    # 두 실패 뒤 **진짜** flock으로 획득은 즉시 된다. 입력 파일이 없어 실행은
    # 실패(1)하지만, workspace가 생겼다는 것이 소유권 획득의 증거다.
    monkeypatch.setattr(os, "set_inheritable", real_set_inheritable)
    monkeypatch.setattr(fcntl, "flock", real_flock)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 1
    assert (job_dir / "workspace").is_dir()


def test_lock_contention_errno_alone_maps_to_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flock이 EAGAIN(BlockingIOError)을 내면 중복(3)이다 — 진짜 충돌과 같은 길이다."""
    engine = _load_engine_module()
    import fcntl

    job_dir = tmp_path / "job"
    job_dir.mkdir()

    def busy(_fd: int, operation: int) -> None:
        raise BlockingIOError(errno.EAGAIN, "injected contention")

    monkeypatch.setattr(fcntl, "flock", busy)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 3
    assert not (job_dir / "workspace").exists()  # 중복은 아무것도 쓰지 않는다


def test_release_closes_the_descriptor_even_when_unlock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unlock이 실패해도 닫기는 일어난다 — 다음 실행이 즉시 잠금을 얻는다."""
    engine = _load_engine_module()
    import fcntl

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    real_flock = fcntl.flock

    def failing_unlock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "injected unlock failure")
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", failing_unlock)
    with pytest.raises(OSError, match="injected unlock failure"):
        engine.main(["analyze", "job-1", str(job_dir)])

    # 닫기는 됐다 — 다음 실행이 중복(3) 없이 소유권을 얻고 실행까지 간다.
    monkeypatch.setattr(fcntl, "flock", real_flock)
    assert engine.main(["analyze", "job-1", str(job_dir)]) != 3
    assert (job_dir / "workspace").is_dir()
