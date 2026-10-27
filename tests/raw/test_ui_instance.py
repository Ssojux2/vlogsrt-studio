import os
import tempfile
from pathlib import Path

import pytest

from ui import instance
from ui.instance import UiInstance, sweep_stale_locks
from vlogsrt import filelock


@pytest.fixture(autouse=True)
def private_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """잠금 폴더는 `$TMPDIR` 아래다 — 시험은 진짜 것(살아 있는 서버의 잠금)을 만지지 않는다."""
    directory = tmp_path / "tmpdir"
    directory.mkdir()
    monkeypatch.setenv("TMPDIR", str(directory))
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


def _lock_dir(private_tmpdir: Path) -> Path:
    return private_tmpdir / f"vlogsrt-ui-{os.getuid()}"


def _sibling(private_tmpdir: Path, token: str) -> Path:
    directory = _lock_dir(private_tmpdir)
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{token * 20}.json"
    path.write_text('{"pid": 1, "port": 7860}', encoding="utf-8")
    return path


def test_same_checkout_reuses_owned_port_and_releases_on_exit(tmp_path: Path):
    with UiInstance(tmp_path, 7860) as owner:
        assert owner.owned
        with UiInstance(tmp_path, 7899) as duplicate:
            assert not duplicate.owned
            assert duplicate.port == 7860
    with UiInstance(tmp_path, 7860) as next_run:
        assert next_run.owned


def test_another_checkout_has_an_independent_lock(tmp_path: Path):
    with UiInstance(tmp_path / "a", 7860) as first, UiInstance(tmp_path / "b", 7861) as second:
        assert first.owned and second.owned


def test_held_lock_requires_a_healthy_app_response(tmp_path: Path, monkeypatch):
    import io

    with UiInstance(tmp_path, 7860) as owner:
        assert owner.owned
        with UiInstance(tmp_path, 7860) as duplicate:
            monkeypatch.setattr(
                instance.urllib.request,
                "urlopen",
                lambda *args, **kwargs: io.StringIO('{"title":"vlogsrt 원본 브라우저"}'),
            )
            assert duplicate.wait_until_listening(timeout=0.01)

            def unavailable(*args, **kwargs):
                raise OSError("server is shutting down")

            monkeypatch.setattr(instance.urllib.request, "urlopen", unavailable)
            assert not duplicate.wait_until_listening(timeout=0.01)


def test_the_lock_file_lives_under_the_private_tmpdir(tmp_path: Path, private_tmpdir: Path):
    with UiInstance(tmp_path, 7860) as owner:
        assert owner.path.parent == _lock_dir(private_tmpdir)
        assert owner.path.exists()


def test_the_owner_removes_its_own_file_on_exit(tmp_path: Path, private_tmpdir: Path):
    with UiInstance(tmp_path, 7860) as owner:
        path = owner.path
        with UiInstance(tmp_path, 7899) as duplicate:
            assert not duplicate.owned
        assert path.exists()  # 소유하지 않은 쪽의 끝은 지우지 않는다
    assert not path.exists()
    assert list(_lock_dir(private_tmpdir).iterdir()) == []


def test_a_replaced_file_is_not_removed_on_exit(tmp_path: Path):
    with UiInstance(tmp_path, 7860) as owner:
        replacement = owner.path.with_name("replacement.tmp")
        replacement.write_text('{"pid": 2, "port": 7999}', encoding="utf-8")
        os.replace(replacement, owner.path)
    assert owner.path.read_text(encoding="utf-8") == '{"pid": 2, "port": 7999}'


def test_the_owner_sweeps_dead_siblings_and_keeps_live_ones(tmp_path: Path, private_tmpdir: Path):
    dead = _sibling(private_tmpdir, "a")
    live = _sibling(private_tmpdir, "b")
    stranger = _lock_dir(private_tmpdir) / "notes.txt"
    stranger.write_text("not a lock", encoding="utf-8")
    holder = os.open(live, os.O_RDWR)
    try:
        filelock.lock_exclusive(holder, blocking=False)  # 살아 있는 서버 대역
        with UiInstance(tmp_path, 7860) as owner:
            assert owner.owned
            assert not dead.exists()
            assert live.exists()
            assert stranger.exists()
            assert owner.path.exists()
    finally:
        os.close(holder)
    assert live.exists()


def test_a_non_owner_launch_does_not_sweep(tmp_path: Path, private_tmpdir: Path):
    with UiInstance(tmp_path, 7860):
        dead = _sibling(private_tmpdir, "c")
        with UiInstance(tmp_path, 7899) as duplicate:
            assert not duplicate.owned
        assert dead.exists()


def test_the_sweep_never_unlinks_a_file_it_could_not_lock(private_tmpdir: Path):
    live = _sibling(private_tmpdir, "d")
    holder = os.open(live, os.O_RDWR)
    try:
        filelock.lock_exclusive(holder, blocking=False)
        assert sweep_stale_locks(live.parent, keep=live.parent / "x.json", limit=10) == 0
        assert live.exists()
    finally:
        os.close(holder)
    assert sweep_stale_locks(live.parent, keep=live.parent / "x.json", limit=10) == 1
    assert not live.exists()


def test_the_sweep_work_is_capped(private_tmpdir: Path):
    paths = [_sibling(private_tmpdir, token) for token in "0123"]
    assert sweep_stale_locks(paths[0].parent, keep=paths[0].parent / "x.json", limit=3) == 3
    assert sum(path.exists() for path in paths) == 1


def test_a_launcher_that_locked_an_unlinked_inode_retries_on_a_fresh_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """늦게 잠근 쪽이 이미 지워진 inode를 쥐었으면 새 파일로 다시 연다 — 서버가 둘 생기지 않는다."""
    launcher = UiInstance(tmp_path, 7860)
    real_open = UiInstance._open
    calls: list[int] = []

    def stale_then_real(self: UiInstance):
        calls.append(1)
        handle = real_open(self)
        if len(calls) == 1:
            self.path.unlink()  # 끝나던 주인(또는 청소)이 그 사이 지웠다
        return handle

    monkeypatch.setattr(UiInstance, "_open", stale_then_real)
    with launcher as owner:
        assert owner.owned
        assert len(calls) == 2
        assert owner.path.exists()
        assert instance.same_file(owner.handle.fileno(), owner.path)
        with UiInstance(tmp_path, 7899) as duplicate:
            assert not duplicate.owned
            assert duplicate.port == 7860


def test_a_sweep_that_raises_releases_the_owner_lock(tmp_path: Path, monkeypatch):
    """`with`는 `__enter__`가 올리면 `__exit__`를 부르지 않는다 — 잠금과 파일을 스스로 놓는다."""

    def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("sweep broke")

    monkeypatch.setattr(instance, "sweep_stale_locks", broken)
    attempt = UiInstance(tmp_path, 7860)
    with pytest.raises(RuntimeError, match="sweep broke"):
        attempt.__enter__()
    assert attempt.handle is None
    assert not attempt.path.exists()
    monkeypatch.undo()
    with UiInstance(tmp_path, 7860) as next_run:
        assert next_run.owned


@pytest.mark.parametrize(
    "settings",
    [
        {"lock_open_attempts": 0},
        {"port_read_attempts": True},
        {"failure_examples": -1},
        {"port_read_retry_seconds": 0.0},
    ],
)
def test_launcher_settings_are_validated(settings: dict[str, object]):
    with pytest.raises(ValueError):
        instance.UiInstanceSettings(**settings)  # type: ignore[arg-type]


# ── 사용자 꼬리표(D100 §1b) ──────────────────────────────────────────────


def test_posix_user_tag_is_the_uid(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(instance.getpass, "getuser", lambda: pytest.fail("POSIX는 uid다"))
    assert instance._user_tag() == str(os.getuid())


@pytest.mark.parametrize("name", ["alice", "a.b-c_9"])
def test_windows_plain_login_names_are_kept(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setattr(instance.sys, "platform", "win32")
    monkeypatch.setattr(instance.getpass, "getuser", lambda: name)
    assert instance._user_tag() == name


@pytest.mark.parametrize("name", ["홍길동", "John Smith", "a/b", "c:\\d"])
def test_windows_unusual_login_names_are_hashed(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setattr(instance.sys, "platform", "win32")
    monkeypatch.setattr(instance.getpass, "getuser", lambda: name)
    tag = instance._user_tag()
    assert tag.startswith("user-") and len(tag) == len("user-") + 12
    assert tag[len("user-") :].isalnum()
    assert instance._user_tag() == tag  # 같은 이름은 같은 폴더다


@pytest.mark.parametrize("error", [ImportError("no pwd"), OSError("no login name")])
def test_windows_without_a_login_name_falls_back_to_the_home_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
):
    def missing() -> str:
        raise error

    monkeypatch.setattr(instance.sys, "platform", "win32")
    monkeypatch.setattr(instance.getpass, "getuser", missing)
    monkeypatch.setattr(instance.Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    tag = instance._user_tag()
    assert tag == "home-" + instance._short_digest(str(tmp_path / "home"))

    def homeless(_cls: type) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(instance.Path, "home", classmethod(homeless))
    assert instance._user_tag() == "home-" + instance._short_digest(tempfile.gettempdir())
