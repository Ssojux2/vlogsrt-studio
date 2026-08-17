"""rich 기반 로깅. `print`는 쓰지 않는다."""

from __future__ import annotations

import os

from rich.console import Console

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

console = Console(stderr=True)
stdout_console = Console()


def _threshold() -> int:
    return _LEVELS.get(os.environ.get("VLOGSRT_LOG_LEVEL", "INFO").upper(), 20)


def debug(message: str) -> None:
    if _threshold() <= 10:
        console.print(f"[dim]· {message}[/dim]")


def info(message: str) -> None:
    if _threshold() <= 20:
        console.print(f"[cyan]›[/cyan] {message}")


def warn(message: str) -> None:
    if _threshold() <= 30:
        console.print(f"[yellow]WARN[/yellow] {message}")


def error(message: str) -> None:
    if _threshold() <= 40:
        console.print(f"[red]ERR[/red] {message}")


def out(message: str) -> None:
    """사람이 파이프로 받는 결과. 로그와 달리 stdout으로 나간다."""
    stdout_console.print(message, highlight=False)
