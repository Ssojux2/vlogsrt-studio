"""`.env` 로더. 실제 `.env`는 읽지 않는다 — 전부 임시 파일이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from vlogsrt.env import is_offline, load_env, parse_env_text, require_api_key


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


def test_api_key_missing_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        require_api_key()
