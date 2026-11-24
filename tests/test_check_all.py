"""`scripts/check_all.py`가 Makefile `check`와 같은 단계를 돈다(W-84, D144)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_all", ROOT / "scripts" / "check_all.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_all = _load()


def _makefile_steps() -> list[tuple[str, list[tuple[str, ...]]]]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    order = re.search(r"^check:(.*)$", text, re.M)
    assert order is not None
    steps = []
    for name in order.group(1).split():
        block = re.search(rf"^{name}:\n((?:\t.*\n)+)", text, re.M)
        assert block is not None, name
        commands = [
            tuple(line.strip().removeprefix("$(PY)").split())
            for line in block.group(1).splitlines()
        ]
        steps.append((name, commands))
    return steps


def test_steps_match_the_makefile() -> None:
    ours = [(name, list(commands)) for name, commands in check_all.STEPS]
    assert ours == _makefile_steps()


def test_selection_keeps_makefile_order() -> None:
    assert [name for name, _ in check_all.selected(["test", "lint"])] == ["lint", "test"]
    assert [name for name, _ in check_all.selected([])] == [name for name, _ in check_all.STEPS]


def test_unknown_step_is_refused() -> None:
    with pytest.raises(SystemExit):
        check_all.selected(["nope"])


def test_stops_at_the_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[str, ...]] = []

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode = code

    def fake_run(command: list[str], cwd: Path, check: bool) -> Done:
        ran.append(tuple(command[1:]))
        return Done(3 if "mypy" in command else 0)

    monkeypatch.setattr(check_all.subprocess, "run", fake_run)
    assert check_all.main([]) == 3
    assert ran[-1] == ("-m", "mypy")
    assert not any("pytest" in args for args in ran)
