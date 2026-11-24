"""pid를 실은 임시 폴더와 시작 청소(D63 H) — 만든 프로세스가 죽은 폴더만 지운다.

모든 시험은 가짜 `$TMPDIR`에서 돈다 — 진짜 것에는 살아 있는 서버의 폴더가 있다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from vlogsrt import scratch_dirs
from vlogsrt.scratch_dirs import (
    PROGRESS_SCRATCH,
    SYNC_SCRATCH,
    ScratchSweepSettings,
    pid_prefix,
    process_alive,
    sweep_dead_scratch,
)


@pytest.fixture(autouse=True)
def private_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "tmpdir"
    directory.mkdir()
    monkeypatch.setenv("TMPDIR", str(directory))
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_the_prefix_carries_this_pid_and_only_known_kinds() -> None:
    assert pid_prefix(PROGRESS_SCRATCH) == f"vlogsrt-progress-{os.getpid()}-"
    assert pid_prefix(SYNC_SCRATCH) == f"vlogsrt-sync-{os.getpid()}-"
    with pytest.raises(ValueError, match="종류"):
        pid_prefix("vlogsrt-status-")


def test_liveness_follows_signal_zero() -> None:
    assert process_alive(os.getpid())
    assert not process_alive(_dead_pid())
    if os.name == "posix" and os.getuid() != 0:
        assert process_alive(1)  # launchd/init — 권한 없음은 살아 있음이다


def test_a_temporary_directory_made_with_the_prefix_is_swept_only_after_its_owner_dies(
    private_tmpdir: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix=pid_prefix(SYNC_SCRATCH)) as scratch:
        assert Path(scratch).parent == private_tmpdir
        assert sweep_dead_scratch() == ()
        assert Path(scratch).is_dir()


def test_dead_pid_dirs_go_and_live_old_format_and_other_entries_stay(
    private_tmpdir: Path,
) -> None:
    dead_pid = _dead_pid()
    dead = [
        private_tmpdir / f"vlogsrt-progress-{dead_pid}-a1b2c3d4",
        private_tmpdir / f"vlogsrt-sync-{dead_pid}-e5f6g7h8",
    ]
    for path in dead:
        path.mkdir()
    (dead[1] / "audio.pcm").write_bytes(b"\0" * 1024)
    kept_dirs = [
        private_tmpdir / f"vlogsrt-progress-{os.getpid()}-livelive",
        private_tmpdir / "vlogsrt-progress-12345678",  # 옛 이름(무작위가 숫자뿐이어도 pid가 아니다)
        private_tmpdir / "vlogsrt-sync-oldfmt_1",
        private_tmpdir / "vlogsrt-status-0F0E2C1B-0000-4000-8000-000000000000",
        private_tmpdir / f"vlogsrt-render-browser-{dead_pid}-x",
    ]
    for path in kept_dirs:
        path.mkdir()
    plain_file = private_tmpdir / f"vlogsrt-sync-{dead_pid}-file"
    plain_file.write_text("not a directory", encoding="utf-8")
    target = private_tmpdir / "target"
    target.mkdir()
    link = private_tmpdir / f"vlogsrt-progress-{dead_pid}-link"
    link.symlink_to(target, target_is_directory=True)

    removed = sweep_dead_scratch()

    assert sorted(removed) == sorted(dead)
    assert not any(path.exists() for path in dead)
    assert all(path.is_dir() for path in kept_dirs)
    assert plain_file.is_file()
    assert link.is_symlink() and target.is_dir()


def test_the_sweep_is_capped_and_the_rest_waits_for_the_next_start(private_tmpdir: Path) -> None:
    dead_pid = _dead_pid()
    for index in range(5):
        (private_tmpdir / f"vlogsrt-progress-{dead_pid}-{index:08d}").mkdir()
    assert len(sweep_dead_scratch(settings=ScratchSweepSettings(entry_limit=3))) == 3
    assert len(sweep_dead_scratch(settings=ScratchSweepSettings(entry_limit=3))) == 2
    assert list(private_tmpdir.iterdir()) == []


def test_a_root_that_cannot_be_listed_warns_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(scratch_dirs.log, "warn", seen.append)
    assert sweep_dead_scratch(tmp_path / "missing") == ()
    assert seen and "훑지 못했다" in seen[0]


@pytest.mark.parametrize("limit", [0, -1, True])
def test_sweep_limit_is_validated(limit: object):
    from vlogsrt.scratch_dirs import ScratchSweepSettings

    with pytest.raises(ValueError):
        ScratchSweepSettings(entry_limit=limit)  # type: ignore[arg-type]
