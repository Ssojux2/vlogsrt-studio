"""파생 계층 설정 뿌리의 계약 — 원본 `vlogsrt.config`와는 완전히 별개다.

P0의 유일한 프로젝트 설정 키는 `schema_version`이다. 이 파일은 그 최소 뿌리가
원본 설정과 섞이지 않는지, 같은 파일 안에서 조용히 잘못 읽히지 않는지를 지킨다.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from vlogsrt.project.config import (
    DEFAULT_PROJECT_CONFIG_PATH,
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
)

PROJECT_PACKAGE_DIR = Path(__file__).resolve().parents[2] / "vlogsrt" / "project"


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ─── 기본 경로 해석 ─────────────────────────────────────────────────────


def test_default_path_is_the_single_relative_root() -> None:
    """설정 뿌리는 하나다 — 상대 경로 `vlogsrt.project.toml`. cwd 기준으로 푼다."""
    assert Path("vlogsrt.project.toml") == DEFAULT_PROJECT_CONFIG_PATH
    assert not DEFAULT_PROJECT_CONFIG_PATH.is_absolute()


def test_none_reads_the_default_in_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`path=None`은 cwd의 기본 파일을 실제로 읽는다 — 지원 밖 버전으로 증명한다.

    값이 기본값(1)과 같으면 읽었는지 증명이 안 된다. 2를 넣고 에러가 나는지 본다.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "vlogsrt.project.toml", "schema_version = 2\n")
    with pytest.raises(ProjectConfigError, match="2"):
        load_project_config(None)
    # 같은 자리의 지원 버전은 그냥 실린다.
    _write(tmp_path / "vlogsrt.project.toml", "schema_version = 1\n")
    assert load_project_config(None) == ProjectConfig()


def test_absent_default_returns_code_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기본 경로가 없다고 물러서지 않는다 — 코드 기본값을 돌려줄 뿐이다."""
    monkeypatch.chdir(tmp_path)
    assert load_project_config(None) == ProjectConfig()
    assert load_project_config().schema_version == 1


def test_explicitly_missing_path_raises_naming_it(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.project.toml"
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(missing)
    assert str(missing) in str(excinfo.value)


def test_explicit_file_load(tmp_path: Path) -> None:
    path = _write(tmp_path / "custom.project.toml", "schema_version = 1\n")
    loaded = load_project_config(path)
    assert loaded == ProjectConfig()
    assert dataclasses.is_dataclass(loaded)
    # 결정적이어야 한다 — 같은 파일을 두 번 읽으면 같은 결과다.
    assert loaded == load_project_config(path)


# ─── 거부 형태 ──────────────────────────────────────────────────────────


def test_malformed_toml_names_the_path(tmp_path: Path) -> None:
    path = _write(tmp_path / "vlogsrt.project.toml", "schema_version = \n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    assert str(path) in str(excinfo.value)


@pytest.mark.parametrize(
    ("body", "rejected_key"),
    [
        ("schema_version = 1\nmax_chars_per_line = 16\n", "max_chars_per_line"),
        ("[project]\nschema_version = 1\n", "project"),  # 부모 테이블은 없다
        ("[vad]\nthreshold = 0.35\n", "vad"),  # P5 키를 미리 넣지 않는다
    ],
)
def test_unknown_key_names_key_and_path(tmp_path: Path, body: str, rejected_key: str) -> None:
    path = _write(tmp_path / "vlogsrt.project.toml", body)
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert rejected_key in message


@pytest.mark.parametrize("version", [0, 2, 99])
def test_unsupported_version_names_version(tmp_path: Path, version: int) -> None:
    path = _write(tmp_path / "vlogsrt.project.toml", f"schema_version = {version}\n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert "schema_version" in message
    assert str(version) in message


@pytest.mark.parametrize("body", ['schema_version = "1"\n', "schema_version = 1.5\n"])
def test_non_integer_version_names_key(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path / "vlogsrt.project.toml", body)
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert "schema_version" in message


def test_bool_version_is_not_an_integer(tmp_path: Path) -> None:
    """`bool`은 `int`의 서브클래스다 — `true`가 1로 새어 들어가면 안 된다."""
    path = _write(tmp_path / "vlogsrt.project.toml", "schema_version = true\n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert "schema_version" in message


# ─── 원본 설정과의 분리 ────────────────────────────────────────────────


def test_project_config_is_not_the_legacy_config_type() -> None:
    from vlogsrt.config import Config

    assert ProjectConfig is not Config
    assert not issubclass(ProjectConfig, Config)
    assert not issubclass(Config, ProjectConfig)
    # P0의 필드는 schema_version 하나다 — 투기적 키가 섞여 들어오면 바로 드러난다.
    assert {f.name for f in dataclasses.fields(ProjectConfig)} == {"schema_version"}


def test_project_config_source_does_not_import_legacy() -> None:
    """레거시 import 금지는 방향이 둘 다다 — project → legacy도 없어야 한다.

    검사기(check_invariants)는 legacy → project만 본다. 반대쪽 선은 이 테스트가 지킨다.
    """
    source = Path(sys.modules["vlogsrt.project.config"].__file__ or "").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            offenders = [alias.name for alias in node.names if alias.name.startswith("vlogsrt")]
        elif isinstance(node, ast.ImportFrom):
            offenders = (
                [node.module or ""]
                if (node.module or "").startswith("vlogsrt") or node.level > 0
                else []
            )
        else:
            continue
        assert not offenders, f"파생 설정이 레거시를 import한다: {node.lineno}번 줄 {offenders}"


def test_importing_is_side_effect_free(tmp_path: Path) -> None:
    """import만으로 네트워크·서브프로세스·파일 쓰기·레거시 설정 적재가 일어나면 안 된다.

    같은 인터프리터에서 이미 import됐을 수 있으니 깨끗한 자식 프로세스에서 본다.
    """
    script = """
import os, socket, subprocess, sys


def blocked(*args, **kwargs):
    raise AssertionError("import가 네트워크/서브프로세스를 몰래 썼다")


socket.socket = blocked
socket.create_connection = blocked
for name in ("run", "Popen", "call", "check_call", "check_output"):
    setattr(subprocess, name, blocked)

before = sorted(os.listdir("."))
import vlogsrt.project
import vlogsrt.project.config
import vlogsrt.project.models
after = sorted(os.listdir("."))
assert before == after, f"import가 파일을 만들었다: {sorted(set(after) - set(before))}"
assert "vlogsrt.config" not in sys.modules, "파생 계층이 레거시 설정을 끌어들였다"
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"부작용 검사 실패:\n{result.stdout}\n{result.stderr}"


# ─── FCPXML 전용 모델 네임스페이스 ─────────────────────────────────────


def test_models_namespace_is_fcpxml_only_and_p0_empty() -> None:
    """P0의 모델 네임스페이스는 **자리 선언**이다 — 투기적 필드를 만들지 않는다.

    P1의 검토된 파서 모델이 이 자리를 채우고, P5 분석 모델은 별도
    `analysis_models.py`로 간다. 지금 여기에 장식된 클래스가 있으면 그 전부가 투기다.
    """
    import vlogsrt.project.models as project_models

    assert "FCPXML" in (project_models.__doc__ or ""), "FCPXML 전용임을 문서로 선언해야 한다"

    source = Path(project_models.__file__ or "").read_text(encoding="utf-8")
    decorated = [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and node.decorator_list
    ]
    assert decorated == [], f"P0에 투기적 모델이 들어왔다: {decorated}"
    # P5의 분석 모델은 별도 파일이다 — 아직 없어야 한다.
    assert not (PROJECT_PACKAGE_DIR / "analysis_models.py").exists()


def test_project_config_error_is_a_value_error() -> None:
    """계약 서명. 설정 오류는 ValueError 계열이다."""
    assert issubclass(ProjectConfigError, ValueError)
