"""테스트 공통 설정.

네트워크를 원천 차단한다. 실수로 실제 API를 부르면 테스트가 즉시 실패해야 한다.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Cue, Segment, Word

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


def make_segment(
    seg_id: int,
    start: float,
    end: float,
    text: str,
    *,
    verdict: str = "ok",
    avg_logprob: float = -0.2,
    compression_ratio: float = 1.4,
    no_speech_prob: float = 0.05,
) -> Segment:
    return Segment(
        id=seg_id,
        start=start,
        end=end,
        text=text,
        avg_logprob=avg_logprob,
        compression_ratio=compression_ratio,
        no_speech_prob=no_speech_prob,
        verdict=verdict,
    )


def make_word(text: str, start: float, end: float) -> Word:
    return Word(text=text, start=start, end=end)
