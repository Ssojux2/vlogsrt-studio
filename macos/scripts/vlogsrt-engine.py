#!/usr/bin/env python3
"""번들 엔진 진입점 — 개발 래퍼(install-development-engine.sh)과 같은 CLI 계약.

    vlogsrt-engine <operation> <job_id> <job_dir> [status_file]

PyInstaller onedir로 얼릴 때 이 파일이 진입점이다(scripts/build_engine.py).
개발 래퍼와 달리 파이썬을 다시 띄우지 않고 `vlogsrt.cli`를 직접 부른다.
API 키는 부모가 준 환경변수(VLOGSRT_ENGINE_API_KEY)로만 받는다 — argv에
키가 오는 경로는 없다. 받은 키는 엔진이 읽는 이름(OPENAI_API_KEY)으로
프로세스 안에서만 옮겨 담는다.

종료 코드는 넷이다: 0 성공, 1 실패한 실행, 2 호출 계약 위반, 3 같은 job이
이미 어딘가에서 도는 중(중복 요청) — 3은 성공도 실패 시도도 아니므로 아무
기록도 쓰지 않는다(S2A-1).

`[status_file]`은 개발 실행기 전용 부가 계약이다(비밀이 아닌 임시 경로).
NSUserUnixTask는 비0 종료를 오류로만 알리기 때문에, 실행기가 이 실행의
정말 종료 코드를 읽는 소유된 채널이다. 경로는 이 호출 전용 모양
`<임시>/vlogsrt-status-<token>/<token>.status`여야 하고 job 상태(별칭 포함)와
분리돼 있으며 이미 있어서는 안 된다 — 검증은 읽기만으로, 소유권 확보보다
먼저다. 조건을 갖추면 정확히 한 줄 `VLOGSRT-ENGINE-STATUS <token> <code>`를
배타 생성으로 남긴다. 3-인자 호출(번들·직접 계약)과 계약 밖 경로에서는 아무
것도 쓰지 않고 종료 코드 계약은 그대로다.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import sys
from pathlib import Path

_CANDIDATE_ID = re.compile(r"^[0-9a-f]{64}$")
_RESULT_FILES = (
    "analysis-estimate.jsonl",
    "analysis-attempts.jsonl",
    "analysis-usage.jsonl",
    "candidates.jsonl",
    "vision-decisions.jsonl",
    "analysis-result.jsonl",
)
# 실패한 실행이 job 루트로 내놓는 진단 증거 — 성공 산물(후보·러프컷)은 없다.
# 앱이 실패를 보여주려고 성공 실행을 한 번 더 요구하는 일이 없게 한다(S1B).
_DIAGNOSTIC_FILES = (
    "analysis-attempts.jsonl",
    "analysis-usage.jsonl",
    "vision-decisions.jsonl",
    "analysis-result.jsonl",
)
_CARRIER = "VLOGSRT_ENGINE_API_KEY"
_ENGINE_KEY_NAME = "OPENAI_API_KEY"
_JOB_DIR_ENV = "VLOGSRT_ENGINE_JOB_DIR"
_ENGINE_LOCK_NAME = ".vlogsrt-engine.lock"
# 중복 소유권 전용 종료 코드다 — 성공(0)도 실패 시도(1)도 호출 계약 위반(2)도 아니다.
_DUPLICATE_EXIT = 3
_ALREADY_RUNNING = "vlogsrt-engine: job already running (duplicate request rejected)"
# 개발 실행기의 소유된 상태 기록 — 정확히 한 줄, token은 이 실행에 묶는다.
_STATUS_PREFIX = "VLOGSRT-ENGINE-STATUS"
_STATUS_DIR_PREFIX = "vlogsrt-status-"
_STATUS_SUFFIX = ".status"


def _status_destination_error(path_text: str, canonical_job: Path) -> str | None:
    """상태 기록 경로의 계약을 **소유권 확보 전에**(읽기만으로) 검사한다.

    기록은 이 호출 전용 모양이어야 한다: `<임시>/vlogsrt-status-<token>/<token>.status`.
    job 상태(별칭 경로 포함) 안이면 안 된다 — 중복 판정이 남의 파일을 덮어쓰는
    길이 생긴다. 이미 있는 파일(최종이든 부분이든)은 절대 덮어쓰지 않는다.
    문제가 있으면 계약 위반 문면을, 없으면 None.
    """
    path = Path(path_text)
    if not path.is_absolute():
        return "status file must be an absolute path"
    token = path.stem
    if path.suffix != _STATUS_SUFFIX or path.parent.name != f"{_STATUS_DIR_PREFIX}{token}":
        return (
            "status file must be an invocation-owned "
            f"{_STATUS_DIR_PREFIX}<token>/{token}{_STATUS_SUFFIX} path"
        )
    if path.resolve().is_relative_to(canonical_job):
        return "status file must live outside the job directory"
    if path.exists() or path.with_name(path.name + ".partial").exists():
        return "status file must not already exist"
    return None


def _write_status_record(path_text: str, code: int) -> None:
    """이 실행의 종료 코드를 실행기가 만든 전용 경로에 남긴다(S2A-1).

    경로 검증은 main이 끝냈다 — 여기는 배타 생성으로만 쓴다: 이미 있는 파일을
    덮어쓰는 경로가 아예 없다. 쓰기는 os.replace로 한 번에 끝난다 — 부분 기록이
    실행기에게 잘못된 상태로 보이지 않게.
    """
    path = Path(path_text)
    record = f"{_STATUS_PREFIX} {path.stem} {code}\n"
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, record.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(partial, path)


class _ExecutionLock:
    """job 디렉터리 하나의 실행 소유권 — 같은 job이 두 프로세스에서 함께 도는 것을 막는다.

    키는 정준화한 job 디렉터리다 — symlink·상대 경로 별칭은 같은 잠금 파일에
    부딪히고, 글자가 같은 job_id라도 디렉터리가 다르면 독립이다(S2A-1).
    flock은 열린 서술자가 닫히는 순간 풀린다 — 예외·취소·비정상 종료가 별도의
    해제 경로나 낡은 PID/표식 검사를 필요로 하지 않는다. 잠금 파일은 지우지
    않는다: inode가 갈라지면 경쟁이 두 갈래로 흩어진다. 서술자는 상속 불가로
    연다 — 엔진이 남긴 자식이 부모 종료 뒤에도 소유권을 붙잡지 않는다.

    비차단 flock의 충돌은 EAGAIN(EWOULDBLOCK)으로 온다 — 그것만이 "누가 쥐고
    있다"다. 그 밖의 OS 오류(EBADF·EIO·지원하지 않는 파일시스템 등)를 충돌로
    입히면 없는 동시 실행자를 지어내고 중복(3)으로 물러나니, 실행 오류로 나간다.
    """

    _CONTENTION_ERRNOS = (errno.EAGAIN, errno.EWOULDBLOCK)

    def __init__(self, job_dir: Path) -> None:
        self._path = job_dir / _ENGINE_LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> bool:
        """비차단으로 잠근다 — 충돌이면 False, 다른 OS 오류는 그대로 나간다(서술자는 닫는다)."""
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            os.set_inheritable(fd, False)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno not in self._CONTENTION_ERRNOS:
                raise
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            # unlock이 실패해도 닫기는 반드시 — 닫히면 커널이 잠금을 푼다.
            os.close(fd)


def _carry_api_key() -> None:
    carrier = os.environ.get(_CARRIER, "")
    if carrier:
        os.environ.setdefault(_ENGINE_KEY_NAME, carrier)
    # 운반자 이름은 지운다 — 키가 두 이름으로 흐르지 않게.
    os.environ.pop(_CARRIER, None)


_STAGED_FILES = ("cancellation.jsonl", "media-grant.json")


def _propagate_staged(job_dir: Path, workspace: Path) -> None:
    """앱이 작업 폴더에 남긴 취소·미디어 권한 기록을 엔진 작업공간으로 옮긴다.

    엔진(Python)은 workspace 안만 본다 — 앱이 job 루트에 스테이징한 파일을
    여기서 옮겨 담는다. 없는 파일은 만들지 않는다. 이미 workspace에 있는
    파일은 바이트를 바꾸지 않는다 — 앱이 실행 중 새로 남긴 기록을 되돌리는
    길이 여기 없어야 한다.
    """
    for name in _STAGED_FILES:
        source = job_dir / name
        if not source.is_file():
            continue
        destination = workspace / name
        if not destination.exists():
            shutil.copyfile(source, destination)


def _expose_job_dir(job_dir: Path) -> None:
    """실행 중 취소 관측 경로를 CLI에 알린다 — 앱은 job 루트에 기록한다.

    `vlogsrt.project.commands._check_cancellation`이 이 값을 읽어 각 외부 호출
    직전에 job 루트의 `cancellation.jsonl`을 다시 본다. 시작 때 한 번 복사만
    으로는 workspace가 만들어진 뒤 남긴 취소가 유실된다. 개발 래퍼는 이 파일에
    위임하므로 번들·개발이 같은 방식으로 알린다.
    """
    os.environ[_JOB_DIR_ENV] = str(job_dir.resolve())


def _run(argv: list[str]) -> int:
    from vlogsrt.cli import main as cli_main

    return int(cli_main(argv))


def _step(argv: list[str], *, when: bool = True) -> int:
    if when and _run(argv) != 0:
        return 1
    return 0


def _sync_results(job_dir: Path, workspace: Path, *, diagnostics_only: bool) -> None:
    """workspace의 결과를 job 루트로 옮긴다 — 실패면 진단 파일만.

    취소된 실행도 실패 종료로 여기에 들어오지만 앱 쪽 게이트(CancellationStore)가
    취소 뒤 sync·import를 막는다(S1A). 엔진은 진단만 전달한다.
    """
    for name in _DIAGNOSTIC_FILES if diagnostics_only else _RESULT_FILES:
        source = workspace / name
        if source.is_file():
            shutil.copyfile(source, job_dir / name)


def main(argv: list[str]) -> int:
    exit_code, status_file = _dispatch(argv)
    if status_file is not None:
        # 정상 종료에만 기록한다 — 예외로 무너진 실행은 기록이 없고 실행기는
        # 그것을 상태 추측이 아니라 오류로 다룬다.
        _write_status_record(status_file, exit_code)
    return exit_code


def _dispatch(argv: list[str]) -> tuple[int, str | None]:
    """검증(읽기 전용) → 소유권 → 실행. 상태 경로는 검증을 통과해야 돌아온다."""
    if len(argv) not in (3, 4):
        print(
            "usage: vlogsrt-engine <operation> <job_id> <job_dir> [status_file]",
            file=sys.stderr,
        )
        return 2, None
    operation, job_id, job_dir_text = argv[0], argv[1], argv[2]
    if "/" in job_id or ".." in job_id:
        return 2, None
    if operation not in {"analyze", "roughcut"}:
        print(f"unknown operation: {operation}", file=sys.stderr)
        return 2, None
    job_dir = Path(job_dir_text)
    if not job_dir.is_dir():
        # 없는 job 루트는 소유권보다 먼저 정중히 거절한다 — workspace를 만들지 않는다.
        print(f"vlogsrt-engine: job directory not found: {job_dir_text}", file=sys.stderr)
        return 2, None
    canonical = job_dir.resolve()
    status_file: str | None = None
    if len(argv) == 4:
        problem = _status_destination_error(argv[3], canonical)
        if problem:
            print(f"vlogsrt-engine: {problem}", file=sys.stderr)
            return 2, None
        status_file = argv[3]
    lock = _ExecutionLock(canonical)
    try:
        owned = lock.acquire()
    except OSError as error:
        print(f"vlogsrt-engine: cannot acquire execution lock: {error}", file=sys.stderr)
        return 1, status_file
    if not owned:
        # 이미 실행 중인 job이다 — 성공도 실패 시도도 아니다. 아무것도 쓰지 않는다.
        # 상태 기록은 예외다: job 상태 밖의 이 호출 전용 채널이니 중복 사실을 전한다.
        print(_ALREADY_RUNNING, file=sys.stderr)
        return _DUPLICATE_EXIT, status_file
    try:
        return _run_owned(operation, canonical), status_file
    finally:
        lock.release()


def _run_owned(operation: str, job_dir: Path) -> int:
    """잠긴 상태에서 실행하는 본문 — 스테이징·엔진 호출·결과 동기화 전부(S2A-1)."""
    _carry_api_key()
    workspace = job_dir / "workspace"
    # 스테이징 복사가 workspace를 만들기 전에 일어나면 FileNotFoundError다 —
    # 새 job에서도 안전하게 먼저 만든다(있으면 그대로 둔다).
    workspace.mkdir(parents=True, exist_ok=True)
    _expose_job_dir(job_dir)
    _propagate_staged(job_dir, workspace)
    exit_code = _run_operation(operation, job_dir, workspace)
    if exit_code == 2:
        # 호출 계약 위반이지 실행 실패가 아니다 — 진단도 없다.
        return 2
    if exit_code:
        # 실패해도 시도·사용량·판정·집계는 앱으로 나간다 — 성공 산물은 나가지 않는다.
        _sync_results(job_dir, workspace, diagnostics_only=True)
        return 1
    _sync_results(job_dir, workspace, diagnostics_only=False)
    return 0


def _run_operation(operation: str, job_dir: Path, workspace: Path) -> int:
    """operation의 단계를 실행한다 — 0 성공, 1 실행 실패, 2 호출 계약 위반."""
    if _step(
        [
            "project",
            "receive",
            str(job_dir / "input.fcpxml"),
            "--workspace",
            str(workspace),
        ],
        when=not (workspace / "workflow.jsonl").is_file(),
    ):
        return 1
    if operation == "analyze":
        if _step(
            ["project", "inspect", str(workspace)],
            when=not (workspace / "analysis-estimate.jsonl").is_file(),
        ):
            return 1
        authorization = job_dir / "analysis-authorization.jsonl"
        if _step(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
            ],
            when=authorization.is_file(),
        ):
            return 1
    else:
        review_args = ["project", "review", str(workspace)]
        approved = job_dir / "approved-candidates.txt"
        if approved.is_file():
            for candidate in approved.read_text(encoding="utf-8").splitlines():
                if not _CANDIDATE_ID.fullmatch(candidate):
                    return 2
                review_args += ["--approve", candidate]
        if _step(review_args):
            return 1
        if _step(["project", "roughcut", str(workspace), "-o", str(job_dir / "roughcut.fcpxml")]):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
