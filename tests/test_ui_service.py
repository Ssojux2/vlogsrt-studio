"""ui/service.py — 포트 고르기와 사람에게 보일 오류만 남았다(D163 — 옛 단일 영상 화면은 지웠다)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from ui import service
from ui.service import UiError


def test_pick_port_returns_the_preferred_one_when_free(local_socket: None) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert service.pick_port(free) == free


def test_pick_port_steps_aside_when_busy(local_socket: None) -> None:
    """앞서 띄운 UI가 남아 있어도 죽으라고 하지 않는다."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        assert service.pick_port(busy) > busy


def test_pick_port_gives_up_loudly(local_socket: None) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        with pytest.raises(UiError, match="빈 포트가 없습니다"):
            service.pick_port(busy, attempts=1)


def test_service_never_imports_gradio() -> None:
    """UI 로직은 gradio 없이도 돌아야 한다. 화면단만 gradio를 안다."""
    source = (Path(__file__).parent.parent / "ui" / "service.py").read_text(encoding="utf-8")
    assert "import gradio" not in source
