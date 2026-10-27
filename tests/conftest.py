"""테스트 공통 설정.

네트워크를 원천 차단한다. 실수로 실제 API를 부르면 테스트가 즉시 실패해야 한다.
dotenv 보호도 여기서 시작한다 — 수집·수입 시각(conftest 수입)부터 붙으므로
어떤 테스트 코드보다 앞서고, monkeypatch가 아니라 직접 얹었으므로 시험
왕복(teardown)에 풀리지 않는다.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

from tests._dotenv_guard import install as install_dotenv_guard

# 실제 `.env`·`.env.example`을 향한 탐색·읽기를 가린다(STAB-TEST-ISOLATION).
# vlogsrt(와 그 밖의 어떤 모듈)보다 **먼저** 붙는다 — 수입 시각 보호가 구조적으로
# 참이어야 한다. 수입 뒤 설치는 '수입에는 보호가 없었다'를 남긴다.
install_dotenv_guard()

import pytest  # noqa: E402

from tests._dotenv_guard import stage_synthetic_dotenv  # noqa: E402
from vlogsrt.config import Config  # noqa: E402
from vlogsrt.models import Cue, Segment  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# 차단 전에 진짜 소켓을 붙잡아 둔다. 루프백 바인딩이 필요한 테스트가 되찾아 쓴다.
_REAL_SOCKET = socket.socket


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """소켓 자체를 막는다. 개발 중 실제 API 호출은 금지다."""

    def guard(*args: object, **kwargs: object) -> None:
        raise NetworkBlocked("테스트에서 네트워크를 쓰려 했다. 픽스처를 사용하라.")

    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


_FINAL_CUT_APP = "Final Cut Pro"


def _refuse_final_cut_argv(args) -> None:
    words = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    for word in words:
        if isinstance(word, (str, bytes, os.PathLike)) and _FINAL_CUT_APP in os.fsdecode(word):
            raise AssertionError(
                f"시험이 {_FINAL_CUT_APP}을 실행하려 했다 — 가짜 opener를 넣어라: {args!r}"
            )


class _GuardedPopen(subprocess.Popen):
    """'Final Cut Pro'를 실은 프로세스는 띄우기 전에 멈춘다 — `subprocess.run`도 이 길을 지난다."""

    def __init__(self, args, *rest, **kwargs) -> None:
        _refuse_final_cut_argv(args)
        super().__init__(args, *rest, **kwargs)


@pytest.fixture(autouse=True)
def never_open_final_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """시험은 실제 Final Cut을 열지 못한다 — 열면 사용자의 진짜 라이브러리로 가져온다(D56).

    「Final Cut으로 보내기」를 부르는 시험은 가짜 opener를 넣어야 한다. 넣지 않으면 실제 열기
    자리(메서드·모듈 함수)에서 멈추고, 그 밖의 길로 "Final Cut Pro"를 실행하려 해도 프로세스를
    띄우기 전에 멈춘다. 웹 UI extra(gradio)가 없는 환경에는 앞의 두 자리가 없다.
    """
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    try:
        from ui import workspace_service
    except ImportError:
        return

    def refuse(path: Path) -> None:
        raise AssertionError(f"시험이 실제 Final Cut을 열려 했다 — 가짜 opener를 넣어라: {path}")

    refuse.__wrapped__ = workspace_service.open_in_final_cut  # 원래 함수 — argv 시험만 쓴다
    monkeypatch.setattr(workspace_service, "open_in_final_cut", refuse)
    monkeypatch.setattr(
        workspace_service.RawWorkspaceUi, "_system_finalcut_open", staticmethod(refuse)
    )


@pytest.fixture(autouse=True)
def fresh_process_caches() -> Iterator[None]:
    """도구 경로·버전·원본 조사 기억(W-45)을 시험마다 비운다 — 가짜 도구가 다음 시험에 새지 않게.

    시험은 `require_ffmpeg`·`_run`을 가짜로 바꿔 서로 다른 버전 문자열을 돌려준다. 기억이 남으면
    앞 시험의 가짜 버전이 뒤 시험의 조사에 실린다.
    """
    from vlogsrt import preprocess

    preprocess.reset_process_caches()
    yield
    preprocess.reset_process_caches()


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    monkeypatch.setenv("VLOGSRT_LOG_LEVEL", "ERROR")


@pytest.fixture
def synthetic_dotenv(tmp_path: Path) -> Path:
    """테스트가 직접 쓴 오프라인 dotenv — 로더의 조상 탐색이 이 뿌리에서 멈춘다.

    PACKAGE_ROOT fallback(진짜 저장소 `.env`)에 닿기 전에 탐색이 끝나야 하는
    시험이 쓴다(S2A-1 격리 원리). 실제 로더가 이 파일을 찾고 파싱한다.
    """
    return stage_synthetic_dotenv(tmp_path)


@pytest.fixture
def local_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """루프백 바인딩만 되살린다.

    빈 포트를 찾는 코드처럼 소켓 객체 자체가 필요한 경우에만 쓴다.
    `create_connection`은 그대로 막혀 있어 밖으로 나가는 호출은 여전히 불가능하다.
    """
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


@pytest.fixture
def cfg() -> Config:
    """코드 기본값. 테스트가 vlogsrt.toml 변경에 흔들리지 않게 한다."""
    return Config()


def make_cue(
    start: float,
    end: float,
    text: str,
    *,
    seg_ids: tuple[int, ...] = (0,),
    origin: str = "spine",
    index: int = 1,
) -> Cue:
    return Cue(
        start=start,
        end=end,
        lines=(text,),
        seg_ids=seg_ids,
        origin=origin,
        index=index,
    )


def make_segment(seg_id: int, start: float, end: float, text: str) -> Segment:
    return Segment(id=seg_id, start=start, end=end, text=text)
