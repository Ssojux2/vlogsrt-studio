"""잠금은 닫기 전에 푼다 — Windows의 닫기는 잠금 풀림을 늦출 수 있다(D100 §1b).

`request_accounting`·`workflow_protocol`과 같은 꼴을 원장·분석 프록시·Final Cut 보내기에도 편다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import filelock
from vlogsrt.project import budget_ledger
from vlogsrt.project.raw import finalcut_export, media_refs


def _record_unlocks(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """풀 때마다 그 서술자가 아직 열려 있었는지 적는다."""
    still_open: list[bool] = []
    real_unlock = filelock.unlock

    def unlock(target: Any) -> None:
        descriptor = target if isinstance(target, int) else target.fileno()
        os.fstat(descriptor)  # 닫혔으면 여기서 OSError
        still_open.append(True)
        real_unlock(target)

    monkeypatch.setattr(filelock, "unlock", unlock)
    return still_open


def test_the_ledger_lock_is_released_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlocks = _record_unlocks(monkeypatch)
    with budget_ledger._ledger_lock(tmp_path / "ledger.json"):
        assert unlocks == []
    assert unlocks == [True]


def test_the_proxy_build_lock_is_released_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlocks = _record_unlocks(monkeypatch)
    with pytest.raises(RuntimeError, match="boom"), media_refs._exclusive(tmp_path / "proxy"):
        raise RuntimeError("boom")
    assert unlocks == [True]
    with media_refs._exclusive(tmp_path / "proxy"):  # 풀렸으니 다시 잡힌다
        pass
    assert unlocks == [True, True]


def test_the_finalcut_folder_lock_is_released_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlocks = _record_unlocks(monkeypatch)
    with finalcut_export._exclusive(tmp_path):
        assert unlocks == []
    assert unlocks == [True]
