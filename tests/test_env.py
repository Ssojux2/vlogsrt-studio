"""`.env` 로더. 실제 `.env`는 읽지 않는다 — 전부 임시 파일이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from vlogsrt.env import (
    api_key_source,
    find_env_file,
    has_api_key,
    is_offline,
    load_env,
    parse_env_text,
    require_api_key,
    reset_cache,
    resolve_under_root,
)


def test_parses_simple_pairs() -> None:
    assert parse_env_text("A=1\nB=two\n") == {"A": "1", "B": "two"}


def test_ignores_comments_and_blanks() -> None:
    assert parse_env_text("# 주석\n\nA=1\n") == {"A": "1"}


def test_strips_surrounding_quotes() -> None:
    assert parse_env_text("A='1'\nB=\"two\"\n") == {"A": "1", "B": "two"}


def test_keeps_equals_inside_value() -> None:
    assert parse_env_text("URL=https://x/?a=b\n") == {"URL": "https://x/?a=b"}


def test_missing_file_is_quiet(tmp_path: Path) -> None:
    assert load_env(tmp_path / "absent") == {}


def test_shell_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_text("VLOGSRT_TEST_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("VLOGSRT_TEST_KEY", "from_shell")
    load_env(path)
    import os

    assert os.environ["VLOGSRT_TEST_KEY"] == "from_shell"


def test_offline_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLOGSRT_OFFLINE", raising=False)
    assert is_offline() is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_truthy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", value)
    assert is_offline() is True


def test_explicit_zero_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    assert is_offline() is False


def test_api_key_refused_while_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    with pytest.raises(RuntimeError, match="오프라인"):
        require_api_key()


def test_force_lets_the_human_pay_on_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """`record`와 `--online`은 VLOGSRT_OFFLINE 값과 무관하게 동작해야 한다."""
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert require_api_key(force=True) == "sk-test"


def test_force_still_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        require_api_key(force=True)


def test_api_key_missing_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        require_api_key()


# ─────────────────────────────────────────────────────────────
#  실행 위치에 흔들리지 않기
# ─────────────────────────────────────────────────────────────


def test_env_is_found_from_a_subdirectory(tmp_path: Path) -> None:
    """저장소 안 어디서 실행하든 `.env`를 찾아야 한다."""
    (tmp_path / ".env").write_text("VLOGSRT_TEST_MARK=1\n", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_env_file(deep) == tmp_path / ".env"


def test_env_lookup_gives_up_outside_any_project(tmp_path: Path) -> None:
    found = find_env_file(tmp_path)
    # 저장소 밖이면 None이거나, 설치된 저장소의 .env로 떨어진다.
    assert found is None or found.name == ".env"


def test_relative_config_paths_resolve_under_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VLOGSRT_FIXTURE_DIR=tests/fixtures/api` 가 실행 위치를 따라다니면 안 된다."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    reset_cache()
    try:
        assert resolve_under_root("tests/fixtures/api") == tmp_path / "tests/fixtures/api"
        assert resolve_under_root("/absolute/stays") == Path("/absolute/stays")
    finally:
        reset_cache()


def test_key_state_helpers_never_expose_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-proj-SECRET-VALUE"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    assert has_api_key() is True
    assert secret not in api_key_source()

    monkeypatch.setenv("OPENAI_API_KEY", "  ")
    assert has_api_key() is False
    assert api_key_source() == "없음"
