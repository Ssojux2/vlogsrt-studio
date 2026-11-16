"""설정 우선순위와 사전 로더."""

from __future__ import annotations

from pathlib import Path

import pytest

from vlogsrt.config import (
    PACKAGE_ROOT,
    Config,
    find_preset,
    find_project_toml,
    flatten_toml,
    load_config,
    read_toml,
)
from vlogsrt.keywords import dedupe, parse_keywords

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
    found = find_project_toml()
    assert found is not None and found.is_file()
    assert found.parent == PACKAGE_ROOT, "패키지 기준으로 찾지 못했다"
    # 찾기만 하고 안 읽으면 소용없다. 그 파일의 키가 실제로 설정에 실렸는지 본다.
    settings = read_toml(found)
    assert settings, "vlogsrt.toml에 키가 하나도 없다 — 이 테스트가 아무것도 못 지킨다"
    cfg = load_config()
    for key, value in settings.items():
        assert getattr(cfg, key) == value, key


def test_the_current_directory_wins_over_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """회차별 설정을 옆에 두고 돌릴 수 있어야 한다. 패키지 기준은 폴백일 뿐이다."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vlogsrt.toml").write_text("[refine]\nmax_chars_per_line = 30\n", encoding="utf-8")
    assert find_project_toml() == tmp_path / "vlogsrt.toml"
    assert load_config().max_chars_per_line == 30


def test_a_missing_toml_is_not_invented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert find_project_toml(NO_PROJECT_TOML) is None


def test_chunk_length_has_two_ceilings() -> None:
    """D27 — 바이트만으로는 조각 **길이**를 못 막는다. 비트레이트가 바뀌면 무너진다."""
    cfg = Config()
    assert cfg.chunk_max_seconds == 180.0
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


def test_keywords_parsing_details() -> None:
    assert parse_keywords("가\n# 주석\n\n  나  \n다 # 꼬리\n") == ["가", "나", "다"]
    assert dedupe(["가", "가", " 나 ", ""]) == ["가", "나"]
