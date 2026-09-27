"""테스트 공통 설정.

네트워크를 원천 차단한다. 실수로 실제 API를 부르면 테스트가 즉시 실패해야 한다.
dotenv 보호도 여기서 시작한다 — 수집·수입 시각(conftest 수입)부터 붙으므로
어떤 테스트 코드보다 앞서고, monkeypatch가 아니라 직접 얹었으므로 시험
왕복(teardown)에 풀리지 않는다.
"""

from __future__ import annotations

import socket
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
