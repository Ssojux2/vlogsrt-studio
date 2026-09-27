"""테스트 전용 dotenv 보호와 합성 엔진 sandbox — 실제 `.env`를 철자로만 가려낸다.

보호 판정은 경로 철자만으로 하고 그 순간 어떤 파일시스템 접근도 하지 않는다
(stat·resolve 금지 — 진짜 `.env`가 있는지 끝까지 모른다). 판정은 `vlogsrt/env.py`
로더가 실제로 쓰는 탐색·읽기 API(`Path.is_file`·`Path.read_text`)와 그 우회로
(`open`·`os.stat`)에 얹는다. 등록된 합성 fixture 뿌리 안의 `.env`만 읽을 수 있고
그 밖의 보호 철자는 탐색이 빠진 것으로 숨기고 읽기는 예외로 막는다.

자식(subprocess) 보호는 같은 어휘 규칙을 sitecustomize로 싣는다 — 자식이 실제
저장소를 참조하게 두지 않기 위해 규칙을 자식용으로 다시 쓴다(파일 수입이 아니다).
"""

from __future__ import annotations

import builtins
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 로더(`vlogsrt/env.py`)가 실제로 찾는 이름은 `.env`뿐이다. `.env.example`은
# 사람이 소유한 문서라 함께 지킨다. `foo.env`·`.envrc`처럼 로더가 읽지 않는
# 철자는 보호하지 않는다 — 보호 범위가 곧 로더의 공격면이다.
PROTECTED_NAMES = frozenset({".env", ".env.example"})
CANARY_KEY = "VLOGSRT_TEST_GUARD_CANARY"
# 카나리 값은 실행마다 새로 만든다 — 소스·출력에 고정 문면이 남으면 그 자체가
# 노출이다. 누구도 이 값을 출력해서는 안 된다.
CANARY_VALUE = f"synthetic-canary-{uuid.uuid4().hex}"
SYNTHETIC_DOTENV_TEXT = "VLOGSRT_OFFLINE=1\n"

# 부모가 자식 보호를 구성하는 환경변수 — 값은 전부 경로다(비밀이 아니다).
CHILD_GUARD_ROOTS = "VLOGSRT_TEST_GUARD_ROOTS"
CHILD_GUARD_STANDIN = "VLOGSRT_TEST_GUARD_STANDIN"
CHILD_GUARD_PROOF = "VLOGSRT_TEST_GUARD_PROOF"
# 시험이 일부러 부수는 bootstrap — 실패 닫힘(fail-closed) 증명용 주입구다.
CHILD_GUARD_BREAK = "VLOGSRT_TEST_GUARD_BREAK"
# bootstrap 실패 전용 종료 코드 — 보호 없이 payload가 도는 일을 원천 차단한다.
CHILD_BOOTSTRAP_FAILED_EXIT = 125

# 보호 설치 전의 원본 open — stand-in을 직접 쓰는 일에만 쓴다(합성 파일 생성).
_REAL_OPEN = builtins.open

_allowed_roots: set[Path] = set()
_violations: list[tuple[str, str]] = []
_installed = False


class GuardedEnvAccess(RuntimeError):
    """등록되지 않은 보호 철자에 대한 읽기·stat 시도를 막았다 — 테스트 격리 경계다."""


def protected_spelling(path: Path | str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> bool:
    """철자만으로 보호 여부를 정한다. 존재 여부도 모른다 — 파일시스템 접근이 없다.

    str·bytes·Path·PathLike 전부 같은 판정을 받는다(byte 경로 우회를 닫는다).
    """
    try:
        text = os.fsdecode(path)
    except TypeError:  # 정수 fd처럼 경로가 아닌 것은 보호 대상이 아니다
        return False
    return os.path.basename(text.rstrip("/")) in PROTECTED_NAMES


def _absolute(path: Path | str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> Path:
    # abspath는 cwd와 문자열로만 접는다 — stat이 없다.
    return Path(os.path.abspath(os.fsdecode(path)))


def _allowed(path: Path | str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> bool:
    spelling = _absolute(path)
    return any(spelling == root or root in spelling.parents for root in _allowed_roots)


def _blocked(kind: str, path: object) -> bool:
    """차단이면 True. 차단 사실을 종류와 경로 철자로만 기록한다 — 내용은 없다."""
    if not isinstance(path, (str, bytes, Path)) and not hasattr(path, "__fspath__"):
        return False
    if not protected_spelling(path) or _allowed(path):
        return False
    spelling = os.fsdecode(path)
    _violations.append((kind, spelling))
    return True


def _refusal(action: str, path: object) -> GuardedEnvAccess:
    return GuardedEnvAccess(f"보호된 dotenv 철자는 {action}할 수 없다: {os.fsdecode(path)}")


def allow_dotenv_under(root: Path) -> Path:
    """합성 dotenv가 살아도 되는 뿌리를 등록한다. 되돌림은 없다 — 좁힘만 있다."""
    spelling = Path(os.path.abspath(root))
    _allowed_roots.update({spelling, spelling.resolve()})
    return spelling


def guard_installed() -> bool:
    return _installed


def violations() -> tuple[tuple[str, str], ...]:
    return tuple(_violations)


def clear_violations() -> None:
    _violations.clear()


def _wrap_path_probe(original: Callable[..., bool]) -> Callable[..., bool]:
    def probe(self: Path, *args: object, **kwargs: object) -> bool:
        if _blocked("probe", self):
            return False  # 없는 파일로 숨긴다 — 로더의 조상 탐색이 여기서 멈춘다
        return original(self, *args, **kwargs)

    return probe


def _wrap_path_read(original: Callable[..., Any]) -> Callable[..., Any]:
    def read(self: Path, *args: object, **kwargs: object) -> Any:
        if _blocked("read", self):
            raise _refusal("읽", self)
        return original(self, *args, **kwargs)

    return read


def _wrap_path_hide(original: Callable[..., Any]) -> Callable[..., Any]:
    """stat을 '파일이 없다'로 숨긴다 — pathlib 내부는 OSError를 잡아 False로 돌린다.

    pytest 수집이 rootdir 후보 경로(저장소 루트 `.env` 포함)를 `is_dir`로
    stat하므로, stat에서 예외를 던지면 수집 자체가 무너진다. FileNotFoundError는
    '없다'와 같은 신호라 존재 신탁도 함께 막는다.
    """

    def hidden(self: Path, *args: object, **kwargs: object) -> Any:
        if _blocked("probe", self):
            raise FileNotFoundError(errno.ENOENT, "hidden protected dotenv spelling", str(self))
        return original(self, *args, **kwargs)

    return hidden


def _install_path_guard() -> None:
    Path.is_file = _wrap_path_probe(Path.is_file)  # type: ignore[method-assign]
    Path.exists = _wrap_path_probe(Path.exists)  # type: ignore[method-assign]
    Path.stat = _wrap_path_hide(Path.stat)  # type: ignore[method-assign]
    Path.read_text = _wrap_path_read(Path.read_text)  # type: ignore[method-assign]
    Path.read_bytes = _wrap_path_read(Path.read_bytes)  # type: ignore[method-assign]
    Path.open = _wrap_path_read(Path.open)  # type: ignore[method-assign]
    # resolve도 보호 철자에서는 신탁이 될 수 있다(readlink 성패로 존재가 드러난다).
    Path.resolve = _wrap_path_read(Path.resolve)  # type: ignore[method-assign]


def _install_open_guard() -> None:
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file: object, *args: object, **kwargs: object) -> Any:
        if _blocked("read", file):
            raise _refusal("열", file)
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_os_open(path: object, flags: int, mode: int = 0o777, **kw: object) -> int:
        if _blocked("read", path):
            raise _refusal("열", path)
        return real_os_open(path, flags, mode, **kw)  # type: ignore[arg-type, call-arg]

    # io.open은 내장 open과 같은 원본을 가리키므로 따로 덮는다 — 직접 참조 우회를 닫는다.
    builtins.open = guarded_open
    io.open = guarded_open
    os.open = guarded_os_open


def _install_stat_guard() -> None:
    real_stat, real_lstat = os.stat, os.lstat
    real_exists, real_isfile, real_lexists = os.path.exists, os.path.isfile, os.path.lexists

    def hidden_stat(path: object, *args: object, **kwargs: object) -> Any:
        if _blocked("probe", path):
            # '없다'로 숨긴다 — 예외를 던지면 pytest 수집(is_dir→stat)이 무너진다.
            raise FileNotFoundError(errno.ENOENT, "hidden protected dotenv spelling", path)
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    def hidden_lstat(path: object, *args: object, **kwargs: object) -> Any:
        if _blocked("probe", path):
            raise FileNotFoundError(errno.ENOENT, "hidden protected dotenv spelling", path)
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    def hiding(path: object) -> bool:
        if _blocked("probe", path):
            return False  # 차단은 '없다'다 — 참이 아니라
        return real_exists(path)

    def hiding_isfile(path: object) -> bool:
        if _blocked("probe", path):
            return False
        return real_isfile(path)

    def hiding_lexists(path: object) -> bool:
        if _blocked("probe", path):
            return False
        return real_lexists(path)

    os.stat = hidden_stat
    os.lstat = hidden_lstat
    os.path.exists = hiding
    os.path.isfile = hiding_isfile
    os.path.lexists = hiding_lexists


def install() -> None:
    """탐색·읽기 API에 보호를 얹는다. conftest 수입 시각(수집 전)에 한 번만 붙는다.

    monkeypatch로 얹지 않았으므로 시험 왕복(teardown)에 풀리지 않는다.
    """
    global _installed
    if _installed:
        return
    _installed = True
    _install_path_guard()
    _install_open_guard()
    _install_stat_guard()


def stage_synthetic_dotenv(root: Path) -> Path:
    """테스트가 직접 쓴 오프라인 dotenv — 조상 탐색이 이 뿌리에서 멈춘다(S2A-1 원리).

    뿌리를 먼저 등록하므로 로더의 탐색·파싱은 실제 코드 그대로 돈다.
    """
    allow_dotenv_under(root)
    dotenv = root / ".env"
    dotenv.write_text(SYNTHETIC_DOTENV_TEXT, encoding="utf-8")
    return dotenv


def stage_canary_standin(directory: Path, *, name: str = ".env") -> Path:
    """등록되지 않은 보호 철자의 stand-in — '보호 전에는 닿는다'의 증명 재료.

    카나리 값은 절대 출력하지 않는다. 작성은 보호 설치 전에 잡아둔 open으로
    직접 한다(stand-in은 어차피 합성 파일이다).
    """
    directory.mkdir(parents=True, exist_ok=True)
    standin = directory / name
    with _REAL_OPEN(standin, "w", encoding="utf-8") as handle:
        handle.write(f"{CANARY_KEY}={CANARY_VALUE}\n")
    return standin


# 자식이 실제 저장소를 참조하지 않으므로 규칙을 자식용으로 다시 쓴다 — 이 파일을
# 자식이 수입하게 두면 그 순간 자식이 실제 저장소에 닿는다. 인터프리터는
# sitecustomize 안의 예외를 조용히 삼키므로, bootstrap은 **실패하면 스스로 프로세스를
# 끝낸다**(os._exit 125) — 보호 없이 payload(engine CLI)가 도는 일이 없게 한다.
# stand-in 증명은 참/거짓만 남긴다(카나리 내용은 절대 기록하지 않는다).
_CHILD_GUARD_SOURCE = '''\
"""자식 프로세스용 dotenv 보호 — 부모 테스트가 생성했다 (실제 저장소 무참조)."""
import builtins
import errno
import io
import json
import os
import socket
from pathlib import Path

PROTECTED_NAMES = {".env", ".env.example"}
BOOTSTRAP_FAILED_EXIT = 125


class GuardedEnvAccess(RuntimeError):
    pass


def _protected(path):
    try:
        text = os.fsdecode(path)
    except TypeError:
        return False
    return os.path.basename(text.rstrip("/")) in PROTECTED_NAMES


def _roots():
    raw = os.environ.get("VLOGSRT_TEST_GUARD_ROOTS", "")
    return [Path(part) for part in raw.split(os.pathsep) if part]


def _blocked(path):
    if not _protected(path):
        return False
    spelling = Path(os.path.abspath(os.fsdecode(path)))
    return not any(spelling == root or root in spelling.parents for root in _roots())


def _hidden(path):
    return FileNotFoundError(errno.ENOENT, "hidden protected dotenv spelling", path)


def _refusal(path):
    return GuardedEnvAccess("blocked dotenv spelling: " + os.fsdecode(path))


def _install_probe_guard(real_os_stat, real_os_lstat, real_exists, real_isfile, real_lexists):
    def hidden_stat(path, *args, **kwargs):
        if _blocked(path):
            raise _hidden(path)
        return real_os_stat(path, *args, **kwargs)

    def hidden_lstat(path, *args, **kwargs):
        if _blocked(path):
            raise _hidden(path)
        return real_os_lstat(path, *args, **kwargs)

    def hiding_path_isfile(self, *args, **kwargs):
        if _blocked(self):
            return False
        return real_isfile(self, *args, **kwargs)

    def hiding_path_exists(self, *args, **kwargs):
        if _blocked(self):
            return False
        return real_exists(self, *args, **kwargs)

    def guarded_path_stat(self, *args, **kwargs):
        if _blocked(self):
            raise _hidden(self)
        return real_os_stat(self, *args, **kwargs)

    os.stat = hidden_stat
    os.lstat = hidden_lstat
    os.path.exists = lambda p: False if _blocked(p) else real_exists(p)
    os.path.isfile = lambda p: False if _blocked(p) else real_isfile(p)
    os.path.lexists = lambda p: False if _blocked(p) else real_lexists(p)
    Path.is_file = hiding_path_isfile
    Path.exists = hiding_path_exists
    Path.stat = guarded_path_stat


def _install_read_guard(real_open, real_os_open, real_read_text, real_read_bytes):
    def guarded_open(file, *args, **kwargs):
        if _blocked(file):
            raise _refusal(file)
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, flags, mode=0o777, **kwargs):
        if _blocked(path):
            raise _refusal(path)
        return real_os_open(path, flags, mode, **kwargs)

    def guarded_read_text(self, *args, **kwargs):
        if _blocked(self):
            raise _refusal(self)
        return real_read_text(self, *args, **kwargs)

    def guarded_read_bytes(self, *args, **kwargs):
        if _blocked(self):
            raise _refusal(self)
        return real_read_bytes(self, *args, **kwargs)

    def guarded_path_open(self, *args, **kwargs):
        if _blocked(self):
            raise _refusal(self)
        return real_open(self, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_open
    os.open = guarded_os_open
    Path.read_text = guarded_read_text
    Path.read_bytes = guarded_read_bytes
    Path.open = guarded_path_open


def _block_network(real_socket):
    def no_network(*args, **kwargs):
        raise RuntimeError("blocked network in test child")

    socket.socket = no_network
    socket.create_connection = no_network


def _install():
    if os.environ.get("VLOGSRT_TEST_GUARD_BREAK") == "1":
        # 시험이 일부러 부수는 bootstrap — 실패 닫힘 증명의 주입구다.
        raise RuntimeError("injected bootstrap break")
    _install_probe_guard(os.stat, os.lstat, os.path.exists, os.path.isfile, os.path.lexists)
    _install_read_guard(builtins.open, os.open, Path.read_text, Path.read_bytes)
    _block_network(socket.socket)


def _verify():
    """보호가 실제로 붙었는지 스스로 잰다 — 없으면 payload를 못 박고 끝낸다."""
    refused = False
    try:
        open("/nonexistent-guard-check/.env", "r")
    except GuardedEnvAccess:
        refused = True
    except OSError:
        refused = False  # 보호가 없으면 진짜 open이 답한다 — 실패다.
    socket_refused = False
    connect_refused = False
    try:
        socket.socket()
    except RuntimeError:
        socket_refused = True
    if socket_refused:
        # socket이 이미 막혔을 때만 잰다 — 막힌 걸 모르고 나가는 일은 없다.
        try:
            socket.create_connection(("127.0.0.1", 1), timeout=0.05)
        except RuntimeError:
            connect_refused = True
    return refused, socket_refused, connect_refused


def _proof_path():
    return os.environ.get("VLOGSRT_TEST_GUARD_PROOF", "")


def _append_record(record):
    proof = _proof_path()
    if not proof:
        return
    with open(proof, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\\n")


def _abort(stage):
    try:
        _append_record({"bootstrap": "failed", "stage": stage})
    except BaseException:
        # 진단 기록 실패가 종료를 되돌려선 안 된다 — 종료가 본체, 기록은 부속이다.
        pass
    os._exit(BOOTSTRAP_FAILED_EXIT)


def _record_proof():
    standin = os.environ.get("VLOGSRT_TEST_GUARD_STANDIN")
    refused, socket_refused, connect_refused = _verify()
    if not (refused and socket_refused and connect_refused):
        _abort("verify")

    def _refused(call):
        try:
            call()
            return False
        except GuardedEnvAccess:
            return True

    # 읽기 경로마다 실제 시도해 본다 — pathlib이 io.open으로 빠지든 직접 부르든
    # 같은 경계인지는 시도한 사실로 말한다(내용은 어디에도 남기지 않는다).
    blocked_read = _refused(lambda: Path(standin).read_text(encoding="utf-8"))
    blocked_read_bytes = _refused(lambda: Path(standin).read_bytes())
    blocked_path_open = _refused(lambda: Path(standin).open("rb"))
    blocked_io_open = _refused(lambda: io.open(standin, "rb"))
    blocked_os_open = _refused(lambda: os.open(standin, os.O_RDONLY))
    probe_miss = not Path(standin).is_file()
    if not (
        standin
        and blocked_read
        and blocked_read_bytes
        and blocked_path_open
        and blocked_io_open
        and blocked_os_open
        and probe_miss
    ):
        _abort("standin")
    import vlogsrt  # 수입 출처 증명 — 실패하면 그대로 터뜨린다(삼키지 않는다).

    _append_record(
        {
            "blocked_read": blocked_read,
            "blocked_read_bytes": blocked_read_bytes,
            "blocked_path_open": blocked_path_open,
            "blocked_io_open": blocked_io_open,
            "blocked_os_open": blocked_os_open,
            "probe_miss": probe_miss,
            "socket_refused": socket_refused,
            "connect_refused": connect_refused,
            "cwd": os.getcwd(),
            "vlogsrt_file": str(Path(vlogsrt.__file__).resolve()),
        }
    )


def _bootstrap():
    try:
        _install()
    except BaseException:
        _abort("install")
    try:
        _record_proof()
    except BaseException:
        _abort("record")


_bootstrap()
'''


def emit_child_guard(
    root: Path,
    *,
    allow_roots: tuple[Path, ...],
    standin: Path | None,
    proof: Path,
    break_bootstrap: bool = False,
) -> dict[str, str]:
    """root에 sitecustomize를 두고 자식 보호 환경변수를 돌려준다.

    root는 자식의 PYTHONPATH 첫머리에 있어야 한다 — sitecustomize는 해석기
    시작 때 그 경로에서 수입된다. 경로는 전부 어휘적으로 접는다(abspath) —
    보호 대상 철자를 resolve하면 우리 보호에 우리가 막힌다. break_bootstrap은
    실패 닫힘 증명용 주입구다 — 실제 시험 외에는 켜지 않는다.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "sitecustomize.py").write_text(_CHILD_GUARD_SOURCE, encoding="utf-8")
    env = {
        CHILD_GUARD_ROOTS: os.pathsep.join(os.path.abspath(item) for item in allow_roots),
        CHILD_GUARD_PROOF: os.path.abspath(proof),
    }
    if standin is not None:
        env[CHILD_GUARD_STANDIN] = os.path.abspath(standin)
    if break_bootstrap:
        env[CHILD_GUARD_BREAK] = "1"
    return env


def read_child_proofs(path: Path) -> list[dict[str, Any]]:
    """자식이 남긴 보호 증명을 읽는다 — 참/거짓과 경로뿐, 카나리는 없다."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# 사전확인 canary가 증명해야 하는 참 필드 — 하나라도 거짓이면 payload를 띄우지 않는다.
_PROOF_REQUIRED_TRUE = (
    "blocked_read",
    "blocked_read_bytes",
    "blocked_path_open",
    "blocked_io_open",
    "blocked_os_open",
    "probe_miss",
    "socket_refused",
    "connect_refused",
)


def preflight_child_guard(
    guard_env: dict[str, str],
    *,
    cwd: Path,
    expected_package_root: Path,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """payload보다 먼저 canary 자식(`-c pass`)로 보호를 확인한다.

    sitecustomize가 없거나 문법이 깨져 있으면 해석기가 조용히 무시하고 payload가
    보호 없이 돈다 — 자식 안의 bootstrap은 그 경우 아예 실행되지 않는다. 그래서
    띄우는 쪽(이 함수)이 canary로 먼저 증명을 확인하고, 없으면 payload를
    띄우지 않는다(실패 닫힘 게이트). 성공하면 canary의 증명 기록을 돌려준다.
    """
    proof = Path(guard_env[CHILD_GUARD_PROOF])
    before = len(read_child_proofs(proof))
    canary = subprocess.run(
        [sys.executable, "-c", "pass"],
        env=guard_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    records = read_child_proofs(proof)
    if canary.returncode != 0 or len(records) <= before:
        raise RuntimeError(
            f"자식 보호 사전확인 실패(exit {canary.returncode}) — payload를 띄우지 않는다"
        )
    latest = records[-1]
    for field in _PROOF_REQUIRED_TRUE:
        if latest.get(field) is not True:
            raise RuntimeError(f"자식 보호 사전확인 실패({field}) — payload를 띄우지 않는다")
    if latest["cwd"] != str(cwd.resolve()):
        raise RuntimeError("자식 보호 사전확인 실패(cwd) — payload를 띄우지 않는다")
    if not latest["vlogsrt_file"].startswith(str(expected_package_root.resolve())):
        raise RuntimeError("자식 보호 사전확인 실패(수입 출처) — payload를 띄우지 않는다")
    return latest


class EngineSandbox:
    """합성 저장소 — 공개 파이썬 소스와 바이트 동일 엔진 스크립트만 실어 나른다.

    경로에 일부러 공백을 넣는다. cwd·PYTHONPATH·PACKAGE_ROOT가 전부 임시 안에
    갇혀 실제 저장소의 보호된 `.env`로 흐를 길이 없다(S2A-1 브리지 시험과 같은
    원리). 해석기만 exec 짐으로 실제 venv를 부른다 — 심볼링크는 venv 접두어
    해석이 합성 쪽으로 샜다(rich를 못 찾음).
    """

    def __init__(self, base: Path, *, source: Path) -> None:
        self.base = base
        self.source = source
        self.repo = base / "synthetic repo"
        self.package = self.repo / "vlogsrt"
        self.engine_script = self.repo / "macos" / "scripts" / "vlogsrt-engine.py"
        self.dotenv = self.repo / ".env"
        self.proof = base / "engine-sandbox-guard-proof.jsonl"
        self.build()

    def build(self) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        copied = self._copy_public_python_sources()
        self._copy_engine_script()
        (self.repo / "pyproject.toml").write_text(
            "[project]\nname='synthetic-engine-sandbox'\n", encoding="utf-8"
        )
        stage_synthetic_dotenv(self.repo)
        self._install_interpreter_shim()
        assert copied >= 20, "패키지 통째가 와야 cli가 돈다 — 빠뜨림은 설정 실패다"

    def _copy_public_python_sources(self) -> int:
        """`vlogsrt/**/*.py`만 복사한다 — 형식·경로 허용목록은 이 하나다."""
        origin = self.source / "vlogsrt"
        copied = 0
        for source_file in sorted(origin.rglob("*.py")):
            if source_file.is_symlink():
                raise RuntimeError(f"공개 원본이 심볼링크다 — 복사를 거절한다: {source_file}")
            destination = self.package / source_file.relative_to(origin)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination)
            copied += 1
        assert (self.package / "__init__.py").is_file()
        return copied

    def _copy_engine_script(self) -> None:
        original = self.source / "macos" / "scripts" / "vlogsrt-engine.py"
        if original.is_symlink():
            raise RuntimeError(f"엔진 스크립트가 심볼링크다 — 복사를 거절한다: {original}")
        self.engine_script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, self.engine_script)
        # 바이트 동일 — 복사본이 흘러가는 일이 없게 못 박는다.
        assert self.engine_script.read_bytes() == original.read_bytes()

    def _install_interpreter_shim(self) -> None:
        binary = self.repo / ".venv" / "bin" / "python"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        binary.chmod(0o755)

    def guard_env(self, *, standin: Path) -> dict[str, str]:
        return emit_child_guard(
            self.repo, allow_roots=(self.repo,), standin=standin, proof=self.proof
        )

    def dotenv_files(self) -> list[Path]:
        """sandbox 안의 보호 철자 전부 — 시험이 지은 오프라인 dotenv 하나뿐이어야 한다."""
        return [path for path in sorted(self.repo.rglob("*")) if path.name in PROTECTED_NAMES]

    def symlinks(self) -> list[Path]:
        """저장소 원본을 향한 심볼링크가 없다는 증명 재료."""
        return [path for path in sorted(self.repo.rglob("*")) if path.is_symlink()]


def build_engine_sandbox(base: Path, *, source: Path) -> EngineSandbox:
    """base 아래 합성 저장소를 만든다 — 실제 저장소를 참조하는 것은 공개 원본뿐이다."""
    return EngineSandbox(base, source=source)
