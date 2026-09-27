#!/usr/bin/env python3
"""번들 엔진 진입점 — 개발 래퍼(install-development-engine.sh)과 같은 CLI 계약.

    vlogsrt-engine <operation> <job_id> <job_dir> [status_file] [--stdin-carrier]

PyInstaller onedir로 얼릴 때 이 파일이 진입점이다(scripts/build_engine.py).
개발 래퍼와 달리 파이썬을 다시 띄우지 않고 `vlogsrt.cli`를 직접 부른다.
API 키는 부모가 준 환경변수(VLOGSRT_ENGINE_API_KEY)로만 받는다 — argv에
키가 오는 경로는 없다. 받은 키는 엔진이 읽는 이름(OPENAI_API_KEY)으로
프로세스 안에서만 옮겨 담는다. 개발 실행기(NSUserUnixTask)는 per-task 환경이
없어 마지막 인자 `--stdin-carrier`(비밀 아님)와 함께 키를 stdin 봉투로 받는다
— 봉투가 있으면 그 값이 권위적이다(상속 키 대체·빈 값은 키 없음), 마커 없는
호출은 stdin을 건드리지 않는 낡은 계약 그대로다.

종료 코드는 넷이다: 0 성공, 1 실패한 실행, 2 호출 계약 위반, 3 같은 job이
이미 어딘가에서 도는 중(중복 요청) — 3은 성공도 실패 시도도 아니므로 아무
기록도 쓰지 않는다(S2A-1).

`[status_file]`은 개발 실행기 전용 부가 계약이다(비밀이 아닌 임시 경로).
NSUserUnixTask는 비0 종료를 오류로만 알리기 때문에, 실행기가 이 실행의
정말 종료 코드를 읽는 소유된 채널이다. 경로는 이 호출 전용 모양
`<임시>/vlogsrt-status-<token>/<token>.status`(token은 영숫자·하이픈)여야 하고
job 상태(별칭 포함)와 분리돼 있으며 이미 있어서는 안 된다 — 검증은 읽기만으로,
소유권 확보보다 먼저다. 조건을 갖추면 정확히 한 줄
`VLOGSRT-ENGINE-STATUS <token> <code>`를 배타 생성으로 남기고 프로세스는
**전달 성공으로만 0**으로 끝난다(실패·신호·크래시는 기록이 없거나 못 미더워
기록으로 재분류되지 않는다). 3-인자 호출(번들·직접 계약)과 계약 밖 경로에서는
아무 것도 쓰지 않고 안쪽 종료 코드가 곧 프로세스 종료 코드다.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import shutil
import sys
import time
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
# 개발 실행기의 사적 stdin 봉투 — NSUserUnixTask에는 per-task 환경이 없다(S2A-2).
# 마법줄·상한은 계약이고, 마커(argv, 비밀 아님)는 "이 호출은 봉투를 읽는다"의
# 명시적 신호다. 마커 없는 3/4-인자 호출은 stdin을 건드리지 않는다.
_CARRIER_MARKER = "--stdin-carrier"
_CARRIER_MAGIC = "VLOGSRT-ENGINE-CARRIER"
_CARRIER_VERSION = "1"
_CARRIER_MAX_BYTES = 8192
# 머릿줄(마법줄+버전, 길이 줄)의 상한 — 이 안에 두 번째 줄바꿈이 오지 않으면
# 모양 오류로 닫는다. 마법줄은 25바이트, 길이는 상한(8192)에 4자리면 충분하다.
_CARRIER_HEADER_MAX_BYTES = 40
# 봉투 읽기의 명시적 마감 — 조각·EOF 확인까지 **하나의 예산**을 공유한다.
# 조각마다 마감을 다시 걸면 한 바이트씩 흘리는 쓰기가 마감을 무한히 늘린다.
_CARRIER_READ_DEADLINE_SECONDS = 10.0
_JOB_DIR_ENV = "VLOGSRT_ENGINE_JOB_DIR"
_ENGINE_LOCK_NAME = ".vlogsrt-engine.lock"
# 중복 소유권 전용 종료 코드다 — 성공(0)도 실패 시도(1)도 호출 계약 위반(2)도 아니다.
_DUPLICATE_EXIT = 3
_ALREADY_RUNNING = "vlogsrt-engine: job already running (duplicate request rejected)"
# 개발 실행기의 소유된 상태 기록 — 정확히 한 줄, token은 이 실행에 묶는다.
_STATUS_PREFIX = "VLOGSRT-ENGINE-STATUS"
_STATUS_DIR_PREFIX = "vlogsrt-status-"
_STATUS_SUFFIX = ".status"
_STATUS_TOKEN = re.compile(r"^[A-Za-z0-9-]+$")


def _status_destination_error(path_text: str, canonical_job: Path) -> str | None:
    """상태 기록 경로의 계약을 **소유권 확보 전에**(읽기만으로) 검사한다.

    기록은 이 호출 전용 모양이어야 한다: `<임시>/vlogsrt-status-<token>/<token>.status`
    (token은 영숫자·하이픈 — 해석기가 정확히 한 줄만 읽도록). job 상태(별칭 경로
    포함) 안이면 안 된다 — 중복 판정이 남의 파일을 덮어쓰는 길이 생긴다. 이미
    있는 파일은 절대 덮어쓰지 않는다. 문제가 있으면 계약 위반 문면을, 없으면 None.
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
    if not _STATUS_TOKEN.fullmatch(token):
        return "status file token must be alphanumeric"
    if path.resolve().is_relative_to(canonical_job):
        return "status file must live outside the job directory"
    if path.exists():
        return "status file must not already exist"
    return None


def _write_status_record(path_text: str, code: int) -> None:
    """이 실행의 안쪽 종료 코드를 실행기가 만든 전용 경로에 남긴다(S2A-1).

    배타 생성으로만 쓴다 — 실행 중 누가 그 자리에 파일을 만들었다면 여기서
    실패하고(기록 없음) 남의 바이트는 그대로다. 실행기는 프로세스가 끝난 뒤에만
    읽으므로 도중 임시파일·이름바꾸기는 필요 없다.
    """
    path = Path(path_text)
    record = f"{_STATUS_PREFIX} {path.stem} {code}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, record.encode("utf-8"))
    finally:
        os.close(fd)


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


def _carry_api_key(stdin_carrier: str | None = None) -> None:
    """앱이 보낸 키를 엔진이 읽는 이름(OPENAI_API_KEY)으로 옮겨 담는다.

    우선순위는 정해져 있다: stdin 봉투 > 환경 운반자 > 낡은 직접 호출(건드리지
    않는다). 앱 실행의 운반은 **권위적**이다 — 값이 있으면 이미 상속된 낡은 키를
    대체하고(교체가 묻히면 앱에서 갈아끼운 키가 영영 안 닿는다), 값이 비면
    '키 없음'으로 상속 키를 내려놓는다(앱에 키가 없는데 셸의 낡은 키로 유료
    호출이 열리면 사전 검사가 거짓말이 된다). 빈 값을 지우지 않고 빈 채로 두는
    것은 dotenv의 setdefault가 그 자리를 다시 채우는 길을 끊는 것이다.
    """
    if stdin_carrier is not None:
        carried = stdin_carrier
    elif _CARRIER in os.environ:
        carried = os.environ[_CARRIER]
    else:
        carried = None  # 낡은 직접 호출 — 상속 환경을 그대로 둔다
    if carried is not None:
        os.environ[_ENGINE_KEY_NAME] = carried
    # 운반자 이름은 지운다 — 키가 두 이름으로 흐르지 않게.
    os.environ.pop(_CARRIER, None)


class _CarrierEnvelopeError(Exception):
    """봉투가 계약을 어긴 것 — 문면은 모양·길이만 말하고 페이로드는 모른다."""


def _read_carrier_envelope(stream: object | None = None) -> str:
    """개발 실행기의 사적 1회 봉투를 stdin에서 읽는다 — NSUserUnixTask에는
    환경 운반이 없어서 이 통로가 유일한 per-task 채널이다.

    읽기는 **fd 위에서** 마감이 정해진 select+os.read로만 한다 — EOF를 기다리는
    무제한 버퍼 읽기는 멈춘 쓰기 끝 앞에서 영원히 블록된다. 마감은 조각 읽기와
    EOF 확인이 **함께 쓰는 하나의 예산**이다(조각마다 다시 걸지 않는다 — 한
    바이트씩 흘리면 마감이 무한히 늘어난다). 모양은 정확히 네 덩어리다: 마법줄·
    버전, 십진 바이트 길이, 본문, 끝표식 줄바꿈. 끝은 엄격하다: 쓰는 쪽이 execute
    앞에서 쓰기 끝을 닫으므로 EOF는 반드시 오고, 끝표식 뒤 바이트가 하나라도
    더 보이면 잔여 입력으로 거절하며, 마감 안에 EOF가 오지 않으면 실패로 닫는다.
    본문의 NUL도 닫는다. 예외 문면은 모양·길이만 말하고 페이로드는 모른다.
    """
    source = sys.stdin if stream is None else stream
    raw = getattr(source, "buffer", source)
    try:
        fd = raw.fileno()
    except (AttributeError, OSError, ValueError) as error:
        raise _CarrierEnvelopeError("carrier input has no bounded file descriptor") from error
    deadline = time.monotonic() + _CARRIER_READ_DEADLINE_SECONDS
    buffer = bytearray()
    while True:
        payload = _parse_carrier_envelope(bytes(buffer))
        if payload is not None:
            _require_clean_eof(fd, deadline)
            return payload
        if len(buffer) > _CARRIER_HEADER_MAX_BYTES + _CARRIER_MAX_BYTES + 1:
            raise _CarrierEnvelopeError("carrier envelope exceeds the size limit")
        chunk = _read_chunk(fd, deadline)
        if not chunk:
            raise _CarrierEnvelopeError("carrier envelope ended before its own terminator")
        buffer += chunk


def _read_chunk(fd: int, deadline: float) -> bytes:
    """공유 마감 안에서 한 덩어리를 읽는다 — 마감이 끝나면 그 자리에서 닫는다."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _CarrierEnvelopeError("carrier input deadline expired")
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except OSError as error:
            raise _CarrierEnvelopeError("carrier input cannot be observed") from error
        if not ready:
            continue  # 남은 시간을 다시 걸고 곧 마감을 다시 잰다
        try:
            return os.read(fd, 4096)  # b""이면 EOF다
        except OSError as error:
            raise _CarrierEnvelopeError("carrier input read failed") from error


def _require_clean_eof(fd: int, deadline: float) -> None:
    """끝표식 뒤에는 EOF만 허용한다 — 잔여 바이트는 거절, 마감 초과는 실패.

    쓰는 쪽은 execute 앞에서 프레임 전부를 쓰고 쓰기 끝을 닫는다 — EOF는 이미
    정해져 있다. 조용한 열린 파이프를 받아들이지 않는다(엄격한 EOF).
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _CarrierEnvelopeError("carrier input deadline expired before EOF")
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except OSError as error:
            raise _CarrierEnvelopeError("carrier input cannot be observed") from error
        if not ready:
            continue
        try:
            trailing = os.read(fd, 1)
        except OSError as error:
            raise _CarrierEnvelopeError("carrier input read failed") from error
        if trailing:
            raise _CarrierEnvelopeError("carrier envelope has trailing input after its terminator")
        return  # EOF — 끝표식 뒤가 깨끗하다


def _parse_carrier_envelope(head: bytes) -> str | None:
    """완결된 봉투면 페이로드를, 아니면 None을 — 잘못된 모양은 예외로 닫는다.

    반환 None은 "아직 덜 왔다"뿐이다(조각 읽기). 모양이 틀렸다는 것은 데이터로
    증명되는 순간 곧바로 예외다 — 마법줄이 온전히 온 뒤에만 비교하고(온전한
    마법줄을 '틀렸다'로 오판하지 않는다), 끝표식 뒤에 **이미 읽힌** 바이트가
    하나라도 있으면 잔여 입력으로 거절한다(나중에 오는 바이트는 호출자의
    EOF 확인이 잡는다).
    """
    magic = f"{_CARRIER_MAGIC} {_CARRIER_VERSION}".encode()
    first_newline = head.find(b"\n")
    if first_newline == -1:
        if len(head) > len(magic):
            raise _CarrierEnvelopeError("carrier envelope magic line does not match")
        return None  # 마법줄이 아직 못 끝났다 — 온전해질 때까지 미완이다
    if head[:first_newline] != magic:
        raise _CarrierEnvelopeError("carrier envelope magic line does not match")
    rest = head[first_newline + 1 :]
    second_newline = rest.find(b"\n")
    if second_newline == -1:
        if len(head) > _CARRIER_HEADER_MAX_BYTES:
            raise _CarrierEnvelopeError("carrier envelope header exceeds its bound")
        return None  # 길이 줄이 아직 못 끝났다
    if first_newline + 1 + second_newline + 1 > _CARRIER_HEADER_MAX_BYTES:
        # 완결된 머릿줄에도 같은 상한 — 재지 않으면 모두 0인 긴 길이 줄이 int
        # 변환을 지나 빈 페이로드로 상한을 빠져나간다. 머릿줄 **영역만** 잰다:
        # 한 번의 읽기가 본문까지 실어도 본문 바이트는 상한과 무관하다.
        raise _CarrierEnvelopeError("carrier envelope header exceeds its bound")
    length_text = rest[:second_newline].decode("ascii", errors="replace")
    if not length_text.isdigit():
        raise _CarrierEnvelopeError("carrier envelope length must be a decimal byte count")
    length = int(length_text)
    if length > _CARRIER_MAX_BYTES:
        raise _CarrierEnvelopeError("carrier envelope declares an oversized payload")
    body = rest[second_newline + 1 :]
    if len(body) < length + 1:
        if len(body) > _CARRIER_MAX_BYTES + 1:
            raise _CarrierEnvelopeError("carrier envelope exceeds the size limit")
        return None  # 본문이나 끝표식이 아직 덜 왔다
    payload = body[:length]
    if body[length] != ord(b"\n"):
        raise _CarrierEnvelopeError("carrier envelope payload does not match its length")
    if body[length + 1 :]:
        raise _CarrierEnvelopeError("carrier envelope has trailing input after its terminator")
    if b"\x00" in payload:
        raise _CarrierEnvelopeError("carrier envelope payload contains a NUL byte")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _CarrierEnvelopeError("carrier envelope payload is not valid UTF-8") from error


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
    if status_file is None:
        return exit_code  # 3-인자(번들·직접) 계약 — 안쪽 코드가 곧 종료 코드다.
    # 4-인자(개발 실행기) 계약 — 안쪽 코드는 기록으로 전달되고, 프로세스는
    # **전달이 끝난 뒤에만** 0으로 끝난다. 전달이 실패하면 1이고 기록은 없다:
    # 실행기는 오류(exit 비0·신호)를 기록으로 재분류하는 길이 아예 없다.
    try:
        _write_status_record(status_file, exit_code)
    except OSError as error:
        print(f"vlogsrt-engine: cannot write status record: {error}", file=sys.stderr)
        return 1
    return 0


def _dispatch(argv: list[str]) -> tuple[int, str | None]:
    """검증(읽기 전용) → 소유권 → 실행. 상태 경로는 검증을 통과해야 돌아온다."""
    if len(argv) not in (3, 4, 5):
        print(
            "usage: vlogsrt-engine <operation> <job_id> <job_dir> [status_file] "
            f"[{_CARRIER_MARKER}]",
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
    tail = argv[3:]
    status_file: str | None = None
    reads_envelope = False
    for argument in tail:
        if argument == _CARRIER_MARKER:
            if reads_envelope:
                print(f"vlogsrt-engine: {argument} given twice", file=sys.stderr)
                return 2, None
            reads_envelope = True
        elif status_file is None:
            problem = _status_destination_error(argument, canonical)
            if problem:
                print(f"vlogsrt-engine: {problem}", file=sys.stderr)
                return 2, None
            status_file = argument
        else:
            print(
                f"vlogsrt-engine: unrecognized argument after status file: {argument!r}",
                file=sys.stderr,
            )
            return 2, None
    stdin_carrier: str | None = None
    if reads_envelope:
        try:
            stdin_carrier = _read_carrier_envelope()
        except _CarrierEnvelopeError as error:
            print(f"vlogsrt-engine: {error}", file=sys.stderr)
            return 2, status_file
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
        return _run_owned(operation, canonical, stdin_carrier), status_file
    finally:
        lock.release()


def _run_owned(operation: str, job_dir: Path, stdin_carrier: str | None = None) -> int:
    """잠긴 상태에서 실행하는 본문 — 스테이징·엔진 호출·결과 동기화 전부(S2A-1)."""
    _carry_api_key(stdin_carrier)
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
