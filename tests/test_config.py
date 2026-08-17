"""설정 우선순위와 사전 로더."""

from __future__ import annotations

from pathlib import Path

import pytest

from vlogsrt.config import Config, find_preset, flatten_toml, load_config
from vlogsrt.keywords import dedupe, load_keywords, parse_keywords, split_inline

NO_PROJECT_TOML = Path("does-not-exist.toml")


def test_defaults_carry_documented_thresholds() -> None:
    cfg = Config()
    assert cfg.compression_ratio_max == 2.4
    assert cfg.avg_logprob_min == -1.0
    assert cfg.min_duration == 0.85
    assert cfg.min_gap == 0.08


def test_preset_shorts_narrows_line() -> None:
    cfg = load_config(preset="shorts", project_toml=NO_PROJECT_TOML)
    assert cfg.max_chars_per_line == 12


def test_cli_beats_preset() -> None:
    cfg = load_config(
        preset="shorts", overrides={"max_chars_per_line": 14}, project_toml=NO_PROJECT_TOML
    )
    assert cfg.max_chars_per_line == 14


def test_preset_beats_project_toml(tmp_path: Path) -> None:
    project = tmp_path / "vlogsrt.toml"
    project.write_text("[refine]\nmax_chars_per_line = 30\n", encoding="utf-8")
    assert load_config(project_toml=project).max_chars_per_line == 30
    assert load_config(preset="shorts", project_toml=project).max_chars_per_line == 12


def test_none_overrides_are_ignored() -> None:
    cfg = load_config(overrides={"max_chars_per_line": None}, project_toml=NO_PROJECT_TOML)
    assert cfg.max_chars_per_line == Config().max_chars_per_line


def test_unknown_key_is_loud() -> None:
    with pytest.raises(RuntimeError, match="알 수 없는 설정 키"):
        flatten_toml({"refine": {"max_charz": 3}}, "test")


def test_every_preset_loads() -> None:
    for name in ("vlog", "shorts", "lecture"):
        assert find_preset(name).is_file()
        assert load_config(preset=name, project_toml=NO_PROJECT_TOML).max_chars_per_line > 0


def test_list_values_become_tuples() -> None:
    cfg = load_config(overrides={"extra_languages": "ko, en"}, project_toml=NO_PROJECT_TOML)
    assert cfg.extra_languages == ("ko", "en")


def test_keywords(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    base.write_text("# 주석\n블루펭귄\n\n망원동\n", encoding="utf-8")
    episode = tmp_path / "ep01.txt"
    episode.write_text("망원동\n연남동  # 회차\n", encoding="utf-8")

    entries = load_keywords(episode_file=episode, inline=["압구정"], base_file=base)
    assert entries == ["블루펭귄", "망원동", "연남동", "압구정"]


def test_keywords_missing_episode_file_is_loud(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="회차 사전"):
        load_keywords(episode_file=tmp_path / "nope.txt", base_file=tmp_path / "none.txt")


def test_keywords_parsing_details() -> None:
    assert parse_keywords("가\n# 주석\n\n  나  \n다 # 꼬리\n") == ["가", "나", "다"]
    assert dedupe(["가", "가", " 나 ", ""]) == ["가", "나"]
    assert split_inline("가, 나 ,") == ["가", "나"]
    assert split_inline(None) == []
