"""설정 우선순위와 사전 로더."""

from __future__ import annotations

from pathlib import Path

import pytest

from vlogsrt.config import Config, find_preset, find_project_toml, flatten_toml, load_config
from vlogsrt.keywords import dedupe, load_keywords, parse_keywords, split_inline

NO_PROJECT_TOML = Path("does-not-exist.toml")


def test_defaults_carry_documented_thresholds() -> None:
    """Netflix TTSG 근거값. 바꾸려면 근거부터 가져와라."""
    cfg = Config()
    assert cfg.min_duration == 0.85  # 최소 노출 5/6초
    assert cfg.min_gap == 0.08  # 24fps 기준 2프레임


def test_the_transcription_model_must_return_segment_times() -> None:
    """시각 없는 응답으로는 SRT를 만들 수 없다(D25). 자를 기준은 모델 입력 한도다."""
    cfg = Config()
    assert cfg.transcribe_format in {"verbose_json"}
    assert cfg.chunk_max_bytes <= 25_000_000  # OpenAI 오디오 API 한도


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


def test_the_project_toml_is_found_from_any_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**D27 사고의 회귀 테스트.** 다른 폴더에서 실행해도 설정이 적용돼야 한다.

    현재 디렉터리만 보던 시절, UI에서 돌리면 `vlogsrt.toml`을 못 찾고 코드 기본값으로
    돌아갔다. D26이 조각을 10분으로 낮췄는데도 38분 조각으로 실행된 경로가 이것이다.
    """
    monkeypatch.chdir(tmp_path)
    assert find_project_toml() is not None
    # 프로젝트 toml은 코드 기본값보다 조각을 잘게 쪼갠다. 값이 다르면 파일을 읽은 것이다.
    assert load_config().chunk_max_bytes != Config().chunk_max_bytes


def test_a_missing_toml_is_not_invented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert find_project_toml(NO_PROJECT_TOML) is None


def test_chunk_length_has_two_ceilings() -> None:
    """D27 — 바이트만으로는 조각 **길이**를 못 막는다. 비트레이트가 바뀌면 무너진다."""
    cfg = Config()
    assert cfg.chunk_max_seconds == 600.0
    assert cfg.chunk_max_bytes <= 25_000_000


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
    cfg = load_config(overrides={"keywords": "ko, en"}, project_toml=NO_PROJECT_TOML)
    assert cfg.keywords == ("ko", "en")


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
