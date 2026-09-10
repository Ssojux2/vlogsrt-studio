"""파생 계층 설정 뿌리의 계약 — 원본 `vlogsrt.config`와는 완전히 별개다.

P0의 유일한 프로젝트 설정 키는 `schema_version`이다. P5-00부터 이 뿌리가 VAD·
비전 튜너블과 가격 가정까지 전부 소유한다 — 모델 이름과 임계값은 이 계층의
config 한 곳에만 산다. 이 파일은 그 값들이 계약 그대로인지, 같은 문서를 두 번
읽어도 같은 해시가 나오는지, 잘못된 조합이 전부 명확히 거부되는지를 지킨다.
공유 분석 어휘(`analysis_models`)의 불변 계약도 여기서 지킨다.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import get_args

import pytest

from vlogsrt.project.analysis_models import AnalysisFailure, AnalysisStatus
from vlogsrt.project.config import (
    DEFAULT_PROJECT_CONFIG_PATH,
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
)


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


def test_default_path_that_is_a_directory_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기본 경로 **자리에 디렉터리가 있으면** 없는 것과 다르다 — 조용히 넘기면 안 된다."""
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "vlogsrt.project.toml"
    directory.mkdir()
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(None)
    assert str(directory) in str(excinfo.value)


def test_explicit_path_that_is_a_directory_raises(tmp_path: Path) -> None:
    directory = tmp_path / "settings.d"
    directory.mkdir()
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(directory)
    assert str(directory) in str(excinfo.value)


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
        # 키는 전부 최상위 평면 키다 — [vad] 같은 부모 테이블은 여전히 없다.
        ("[vad]\nthreshold = 0.35\n", "vad"),
        ("schema_version = 1\nvision_temperature = 0.7\n", "vision_temperature"),
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


@pytest.mark.parametrize("body", ["", "# 주석만 있는 파일이다\n", "\n\n"])
def test_present_file_without_schema_version_raises(tmp_path: Path, body: str) -> None:
    """파일이 **있는데** `schema_version`가 없으면 기본값으로 둔갑하면 안 된다.

    빈 파일·주석뿐인 파일이 코드 기본값과 같은 결과를 내면, 실수로 비운 설정이
    아무 일도 없던 것처럼 지나간다. `KeyError`가 새어 나가는 것도 곤란하다 —
    계약의 예외는 `ProjectConfigError`뿐이다.
    """
    path = _write(tmp_path / "vlogsrt.project.toml", body)
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert "schema_version" in message


def test_empty_default_file_also_requires_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기본 경로로 읽을 때도 같다 — 파일이 있으면 키가 있어야 한다."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "vlogsrt.project.toml", "# 주석뿐이다\n")
    with pytest.raises(ProjectConfigError, match="schema_version"):
        load_project_config(None)


# ─── 원본 설정과의 분리 ────────────────────────────────────────────────


def test_project_config_is_not_the_legacy_config_type() -> None:
    from vlogsrt.config import Config

    assert ProjectConfig is not Config
    assert not issubclass(ProjectConfig, Config)
    assert not issubclass(Config, ProjectConfig)
    # 계약 서명 그대로다 — frozen·slots인 불변 설정 객체. slots는 공개 관찰로 증명한다:
    # `__dataclass_params__.slots`는 3.11에 없는 사적 필드다(3.12에 생겼다).
    params = ProjectConfig.__dataclass_params__
    assert params.frozen
    assert "schema_version" in ProjectConfig.__slots__
    assert not hasattr(ProjectConfig(), "__dict__")


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
import vlogsrt.project.analysis_models
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


def test_models_namespace_is_fcpxml_only() -> None:
    """모델 네임스페이스는 FCPXML 전송 모델 전용이다 — 문서로 선언된 선이다.

    P1의 검토된 파서 모델이 이 자리를 채우고, P5 분석 모델은 별도
    `analysis_models.py`로 간다(모듈 문서 참조).
    """
    import vlogsrt.project.models as project_models

    assert "FCPXML" in (project_models.__doc__ or ""), "FCPXML 전용임을 문서로 선언해야 한다"

    # P0에는 데이터클래스가 없다. 이 선언은 P1이 파서 모델을 채운 뒤에도 산다 —
    # 여기 들어오는 데이터클래스는 전부 frozen이어야 한다(계약 §8). 검사기가
    # 정적 선언을 보면 이 테스트는 행동을 본다.
    mutable = [
        name
        for name, cls in vars(project_models).items()
        if isinstance(cls, type)
        and dataclasses.is_dataclass(cls)
        and not cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
    ]
    assert mutable == [], f"가변 데이터클래스가 파생 모델에 있다: {mutable}"


def test_project_config_error_is_a_value_error() -> None:
    """계약 서명. 설정 오류는 ValueError 계열이다."""
    assert issubclass(ProjectConfigError, ValueError)


# ─── P5-00: 기본값 계약 ────────────────────────────────────────────────
#
# VAD 좌표는 공식 태그 v6.2.1에서, 비전 튜너블은 검토된 계획에서 왔다.
# URL 리터럴이 소스에서 줄바꿈으로 쪼개져 있어도 바이트가 정확함을 증명하기 위해
# 다이제스트로 고정한다.

_ONNX_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    "v6.2.1/src/silero_vad/data/silero_vad.onnx"
)
_LICENSE_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/LICENSE"
_ONNX_URL_SHA256 = "d6f852c813b4556a34d7ab2cf164bc0705691e17a8021f4304e7b17fc25d56ce"
_LICENSE_URL_SHA256 = "dd359bb5530bcd7950751f9e77d142e11a6e8c2edd4144bbf89d4c321b04e6fe"

_PRICE_KEYS = (
    "source_audio_cost_per_minute",
    "repair_verify_audio_cost_per_minute",
    "boundary_audio_cost_per_minute",
    "vision_cost_per_image",
)
_FRACTION_KEYS = (
    "guard_padding_seconds",
    "vision_sample_fps",
    "block_min_seconds",
    "block_max_seconds",
    "keep_seconds",
)


def test_defaults_are_the_binding_values() -> None:
    """P5-00 계약의 전 필드가 글자 그대로 기본값으로 산다 — 가격 가정은 없다."""
    config = ProjectConfig()
    assert config.schema_version == 1
    assert config.vad_model_version == "v6.2.1"
    assert config.vad_model_sha256 == (
        "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
    )
    assert config.vad_license_sha256 == (
        "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"
    )
    assert config.vad_sample_rate == 16000
    assert config.vad_window_samples == 512
    assert config.vad_context_samples == 64
    assert config.vad_positive == 0.35
    assert config.guard_padding_seconds == Fraction(1, 2)
    assert config.vision_model == "glm-5.3-flash"
    assert config.vision_api_key_env == "ZAI_API_KEY"
    assert config.vision_base_url_env == "ZAI_BASE_URL"
    assert config.vision_long_edge == 768
    assert config.vision_sample_fps == Fraction(1)
    assert config.vision_max_images == 16
    assert config.block_min_seconds == Fraction(6)
    assert config.block_max_seconds == Fraction(10)
    assert config.keep_seconds == Fraction(3)
    assert config.vision_apply_confidence == 0.8
    assert config.pricing_currency is None
    for key in _PRICE_KEYS:
        assert getattr(config, key) is None, key


def test_artifact_urls_are_pinned_by_byte_digest() -> None:
    """URL 기본값의 바이트를 다이제스트로 고정한다 — 쪼개 놓아도 값은 하나다."""
    config = ProjectConfig()
    assert hashlib.sha256(config.vad_model_url.encode("utf-8")).hexdigest() == _ONNX_URL_SHA256
    assert hashlib.sha256(config.vad_license_url.encode("utf-8")).hexdigest() == _LICENSE_URL_SHA256


def test_project_config_field_set_is_the_p5_00_contract() -> None:
    """필드 집합 자체가 계약이다 — 슬롯으로 관찰한다(전부 slots 여야 한다)."""
    assert set(ProjectConfig.__slots__) == {
        "schema_version",
        "vad_model_version",
        "vad_model_url",
        "vad_model_sha256",
        "vad_license_url",
        "vad_license_sha256",
        "vad_sample_rate",
        "vad_window_samples",
        "vad_context_samples",
        "vad_positive",
        "guard_padding_seconds",
        "vision_model",
        "vision_api_key_env",
        "vision_base_url_env",
        "vision_long_edge",
        "vision_sample_fps",
        "vision_max_images",
        "block_min_seconds",
        "block_max_seconds",
        "keep_seconds",
        "vision_apply_confidence",
        "pricing_currency",
        "source_audio_cost_per_minute",
        "repair_verify_audio_cost_per_minute",
        "boundary_audio_cost_per_minute",
        "vision_cost_per_image",
    }


# ─── P5-00: 정규형 직렬화와 결정적 해시 ───────────────────────────────


def _fully_spelled_defaults_toml() -> str:
    """생략 없이 기본값을 전부 푼 문서 — 생략형(`schema_version`만)과 동치여야 한다."""
    lines = [
        "schema_version = 1",
        'vad_model_version = "v6.2.1"',
        f'vad_model_url = "{_ONNX_URL}"',
        'vad_model_sha256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"',
        f'vad_license_url = "{_LICENSE_URL}"',
        'vad_license_sha256 = "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"',
        "vad_sample_rate = 16000",
        "vad_window_samples = 512",
        "vad_context_samples = 64",
        "vad_positive = 0.35",
        'guard_padding_seconds = "1/2"',
        'vision_model = "glm-5.3-flash"',
        'vision_api_key_env = "ZAI_API_KEY"',
        'vision_base_url_env = "ZAI_BASE_URL"',
        "vision_long_edge = 768",
        'vision_sample_fps = "1/1"',
        "vision_max_images = 16",
        'block_min_seconds = "6/1"',
        'block_max_seconds = "10/1"',
        'keep_seconds = "3/1"',
        "vision_apply_confidence = 0.8",
    ]
    return "\n".join(lines) + "\n"


def test_fully_spelled_defaults_hash_identically_to_omitted_form(tmp_path: Path) -> None:
    """동치 문서는 같은 해시를 낸다 — 해시는 해석된 값에만 붙는다."""
    bare = _write(tmp_path / "bare.project.toml", "schema_version = 1\n")
    full = _write(tmp_path / "full.project.toml", _fully_spelled_defaults_toml())
    bare_config = load_project_config(bare)
    full_config = load_project_config(full)
    assert full_config == ProjectConfig()
    assert bare_config.sha256() == full_config.sha256()
    assert full_config.sha256() == ProjectConfig().sha256()


def test_repo_root_config_file_is_pure_defaults() -> None:
    """저장소 뿌리의 `vlogsrt.project.toml`은 선언뿐이다 — 코드 기본값과 동치."""
    repo_root = Path(__file__).resolve().parents[2]
    loaded = load_project_config(repo_root / "vlogsrt.project.toml")
    assert loaded == ProjectConfig()
    assert loaded.sha256() == ProjectConfig().sha256()


@pytest.mark.parametrize("key", _FRACTION_KEYS)
def test_equivalent_fraction_spellings_hash_identically(tmp_path: Path, key: str) -> None:
    """값이 같으면 철자가 달라도 해시는 같다 — "6/1"·"12/2"·정수 6은 하나다."""
    hashes = set()
    for index, value in enumerate(['"6/1"', '"12/2"', "6"]):
        body = f"schema_version = 1\n{key} = {value}\n"
        config = load_project_config(_write(tmp_path / f"eq{index}.project.toml", body))
        assert getattr(config, key) == Fraction(6)
        hashes.add(config.sha256())
    assert len(hashes) == 1


def test_to_dict_serializes_canonically() -> None:
    """분수는 '분자/분모', 가격은 정규형 십진 문자열, 없음은 None 그대로."""
    as_dict = ProjectConfig().to_dict()
    assert set(as_dict) == set(ProjectConfig.__slots__)
    assert as_dict["schema_version"] == 1
    assert as_dict["vad_sample_rate"] == 16000
    assert as_dict["vad_positive"] == 0.35
    assert as_dict["vision_apply_confidence"] == 0.8
    assert as_dict["guard_padding_seconds"] == "1/2"
    assert as_dict["vision_sample_fps"] == "1/1"
    assert as_dict["block_min_seconds"] == "6/1"
    assert as_dict["block_max_seconds"] == "10/1"
    assert as_dict["keep_seconds"] == "3/1"
    assert as_dict["pricing_currency"] is None
    for key in _PRICE_KEYS:
        assert as_dict[key] is None, key
    # 같은 값을 두 번 직렬화하면 같은 바이트다.
    assert json.dumps(as_dict, sort_keys=True) == json.dumps(
        ProjectConfig().to_dict(), sort_keys=True
    )


def test_sha256_is_a_stable_lowercase_hex_digest() -> None:
    digest = ProjectConfig().sha256()
    assert digest == ProjectConfig().sha256()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_hash_distinguishes_different_values(tmp_path: Path) -> None:
    base = load_project_config(_write(tmp_path / "a.project.toml", "schema_version = 1\n"))
    bumped = load_project_config(
        _write(tmp_path / "b.project.toml", "schema_version = 1\nvision_max_images = 15\n")
    )
    assert base.sha256() != bumped.sha256()


# ─── P5-00: 타입·범위 거부 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        # bool은 int의 서브클래스다 — 어느 숫자 필드로도 새면 안 된다.
        "vision_long_edge = true",
        "vad_sample_rate = true",
        "vision_max_images = false",
        "guard_padding_seconds = true",
        "vad_positive = true",
        # 정수 필드에 실수·문자열
        "vision_long_edge = 768.0",
        'vision_long_edge = "768"',
        # 치수는 양수
        "vision_long_edge = 0",
        "vision_long_edge = -1",
        "vision_max_images = 0",
        # VAD 좌표는 아티팩트에 고정이다
        "vad_sample_rate = 8000",
        "vad_sample_rate = 44100",
        "vad_window_samples = 256",
        "vad_context_samples = 32",
        # nan/inf·범위 밖 신뢰도
        "vad_positive = nan",
        "vad_positive = inf",
        "vad_positive = -inf",
        "vad_positive = -0.01",
        "vad_positive = 1.5",
        "vision_apply_confidence = -0.2",
        "vision_apply_confidence = 1.0001",
        # 분수 필드: 실수·bool·까다로운 문자열·양수 아닌 값
        "guard_padding_seconds = 0.5",
        'guard_padding_seconds = "0/1"',
        'guard_padding_seconds = "-1/2"',
        'guard_padding_seconds = "1/0"',
        'guard_padding_seconds = "1/-2"',
        'guard_padding_seconds = "half"',
        'guard_padding_seconds = "1"',
        "vision_sample_fps = 0",
        'block_min_seconds = "0/3"',
        # 문자열 필드: 빈 값·문자열 아님
        'vision_model = ""',
        "vision_model = 5",
        'vad_model_version = ""',
        # 체크섬은 소문자 16진수 64자
        'vad_model_sha256 = "not-hex"',
        ('vad_model_sha256 = "1A153A22F4509E292A94E67D6F9B85E8DEB25B4988682B7E174C65279D8788E3"'),
        'vad_license_sha256 = "abc"',
        # 가격: 실수·bool·음수·비유한·해독 불가
        "vision_cost_per_image = 0.01",
        "vision_cost_per_image = true",
        'vision_cost_per_image = "-0.01"',
        'vision_cost_per_image = "nan"',
        'vision_cost_per_image = "inf"',
        'vision_cost_per_image = "abc"',
        'vision_cost_per_image = ""',
    ],
)
def test_invalid_values_are_rejected_naming_key_and_path(tmp_path: Path, line: str) -> None:
    path = _write(tmp_path / "bad.project.toml", f"schema_version = 1\n{line}\n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    message = str(excinfo.value)
    key = line.split(" =")[0]
    assert key in message, line
    assert str(path) in message, line


@pytest.mark.parametrize(
    ("line", "key", "value"),
    [
        ("vad_positive = 0", "vad_positive", 0.0),
        ("vad_positive = 1", "vad_positive", 1.0),
        ("vad_positive = 0.5", "vad_positive", 0.5),
        ("vision_apply_confidence = 0.0", "vision_apply_confidence", 0.0),
        ("vision_apply_confidence = 1.0", "vision_apply_confidence", 1.0),
    ],
)
def test_unit_bounds_are_inclusive(tmp_path: Path, line: str, key: str, value: float) -> None:
    config = load_project_config(
        _write(tmp_path / "bound.project.toml", f"schema_version = 1\n{line}\n")
    )
    assert getattr(config, key) == value
    assert isinstance(getattr(config, key), float)


@pytest.mark.parametrize(
    ("body", "keys"),
    [
        (
            'schema_version = 1\nblock_min_seconds = "11/1"\n',
            ("block_min_seconds", "block_max_seconds"),
        ),
        (
            'schema_version = 1\nblock_max_seconds = "5/1"\n',
            ("block_min_seconds", "block_max_seconds"),
        ),
        (
            'schema_version = 1\nblock_min_seconds = "12/1"\nblock_max_seconds = "11/1"\n',
            ("block_min_seconds", "block_max_seconds"),
        ),
        (
            'schema_version = 1\nkeep_seconds = "7/1"\n',
            ("keep_seconds", "block_min_seconds"),
        ),
        (
            'schema_version = 1\nblock_min_seconds = "4/1"\nkeep_seconds = "5/1"\n',
            ("keep_seconds", "block_min_seconds"),
        ),
    ],
)
def test_block_ordering_violations_are_rejected(
    tmp_path: Path, body: str, keys: tuple[str, ...]
) -> None:
    path = _write(tmp_path / "order.project.toml", body)
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    message = str(excinfo.value)
    for key in keys:
        assert key in message


def test_block_ordering_allows_equality(tmp_path: Path) -> None:
    """계약이 거부하는 건 min > max와 keep > min뿐이다 — 등호는 허용한다."""
    body = (
        'schema_version = 1\nblock_min_seconds = "10/1"\n'
        'block_max_seconds = "10/1"\nkeep_seconds = "10/1"\n'
    )
    config = load_project_config(_write(tmp_path / "equal.project.toml", body))
    assert config.block_min_seconds == config.block_max_seconds == config.keep_seconds


# ─── P5-06: 생성자 우회 닫기 — 검증 없이 만든 구성도 생성에서 닫는다 ──────
#
# P5-05b·P5-03b가 만났던 구멍이다: `ProjectConfig`를 프로그램식으로 만들면
# `load_project_config`의 검증을 건너뛴다. 세 소비자가 같은 검사를 각자 다시
# 짜는 대신 생성자에서 한 번에 닫는다 — 파일 해석이 거부하는 값은 생성도 거부한다.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # 실수 필드: nan·inf·범위 밖·bool·문자열
        pytest.param("vad_positive", float("nan"), id="vad-positive-nan"),
        pytest.param("vad_positive", float("inf"), id="vad-positive-inf"),
        pytest.param("vad_positive", float("-inf"), id="vad-positive-neg-inf"),
        pytest.param("vad_positive", -0.01, id="vad-positive-below-range"),
        pytest.param("vad_positive", 1.5, id="vad-positive-above-range"),
        pytest.param("vad_positive", True, id="vad-positive-bool"),
        pytest.param("vad_positive", "0.5", id="vad-positive-string"),
        pytest.param("vision_apply_confidence", float("nan"), id="confidence-nan"),
        pytest.param("vision_apply_confidence", float("inf"), id="confidence-inf"),
        pytest.param("vision_apply_confidence", -0.2, id="confidence-below-range"),
        pytest.param("vision_apply_confidence", 1.0001, id="confidence-above-range"),
        pytest.param("vision_apply_confidence", True, id="confidence-bool"),
        # 정수 필드: bool·실수·문자열·양수 아닌 값
        pytest.param("vision_max_images", True, id="max-images-bool"),
        pytest.param("vision_max_images", 16.0, id="max-images-float"),
        pytest.param("vision_max_images", "16", id="max-images-string"),
        pytest.param("vision_max_images", 0, id="max-images-zero"),
        pytest.param("vision_max_images", -1, id="max-images-negative"),
        pytest.param("vision_long_edge", 0, id="long-edge-zero"),
        pytest.param("vision_long_edge", 768.0, id="long-edge-float"),
        # VAD 좌표는 아티팩트에 고정이다 — 파일 밖에서도 마찬가지다
        pytest.param("vad_sample_rate", 8000, id="sample-rate-unpinned"),
        pytest.param("vad_window_samples", True, id="window-samples-bool"),
        # 분수 필드: 실수·bool·양수 아닌 값·'분자/분모' 아닌 문자열
        pytest.param("guard_padding_seconds", 0.5, id="padding-float"),
        pytest.param("guard_padding_seconds", True, id="padding-bool"),
        pytest.param("guard_padding_seconds", Fraction(0), id="padding-zero"),
        pytest.param("guard_padding_seconds", Fraction(-1, 2), id="padding-negative"),
        pytest.param("guard_padding_seconds", "1", id="padding-int-text"),
        pytest.param("vision_sample_fps", Fraction(0), id="sample-fps-zero"),
        pytest.param("block_min_seconds", Fraction(-6), id="block-min-negative"),
        # 스키마·문자열·체크섬·자격증명 이름
        pytest.param("schema_version", 2, id="schema-unsupported"),
        pytest.param("schema_version", True, id="schema-bool"),
        pytest.param("vision_model", "", id="model-blank"),
        pytest.param("vision_model", 5, id="model-not-a-string"),
        pytest.param("vad_model_sha256", "not-hex", id="model-sha-not-hex"),
        pytest.param("vision_api_key_env", "OPENAI_API_KEY", id="stt-credential-reuse"),
        # 가격: 음수·비유한·실수·bool
        pytest.param("vision_cost_per_image", Decimal("-0.01"), id="price-negative"),
        pytest.param("vision_cost_per_image", Decimal("nan"), id="price-nan"),
        pytest.param("vision_cost_per_image", 0.01, id="price-float"),
        pytest.param("vision_cost_per_image", True, id="price-bool"),
    ],
)
def test_a_programmatic_defect_is_rejected_at_construction(field: str, value: object) -> None:
    """파일 해석의 값역 검사는 프로그래밍 구성에는 닿지 않았다 — 이제 생성이 닫는다."""
    with pytest.raises(ProjectConfigError, match=field):
        ProjectConfig(**{field: value})


def test_the_constructor_names_the_key_and_the_value() -> None:
    """위반은 키와 값을 대고 말한다 — 어느 필드가 왜 못 났는지 메시지로 남는다."""
    with pytest.raises(ProjectConfigError) as excinfo:
        ProjectConfig(vad_positive=float("nan"))
    message = str(excinfo.value)
    assert "vad_positive" in message
    assert "nan" in message


@pytest.mark.parametrize(
    ("kwargs", "keys"),
    [
        (
            {"block_min_seconds": Fraction(11)},
            ("block_min_seconds", "block_max_seconds"),
        ),
        (
            {"block_max_seconds": Fraction(5)},
            ("block_min_seconds", "block_max_seconds"),
        ),
        (
            {"keep_seconds": Fraction(7)},
            ("keep_seconds", "block_min_seconds"),
        ),
        ({"vision_cost_per_image": Decimal("0.01")}, ("pricing_currency",)),
    ],
)
def test_cross_violations_are_rejected_at_construction(
    kwargs: dict[str, object], keys: tuple[str, ...]
) -> None:
    """한 키만 봐서 못 잡는 계약도 생성에서 닫는다 — 순서와 라벨 짝."""
    with pytest.raises(ProjectConfigError) as excinfo:
        ProjectConfig(**kwargs)
    message = str(excinfo.value)
    for key in keys:
        assert key in message


def test_replace_cannot_sneak_a_defect_past_the_constructor() -> None:
    """P5-05b·P5-03b가 만났던 우회 그 자체다 — `replace`도 생성자를 지난다."""
    with pytest.raises(ProjectConfigError, match="vision_apply_confidence"):
        dataclasses.replace(ProjectConfig(), vision_apply_confidence=float("nan"))
    with pytest.raises(ProjectConfigError, match="guard_padding_seconds"):
        dataclasses.replace(ProjectConfig(), guard_padding_seconds=Fraction(-1, 2))


def test_a_sound_programmatic_config_constructs_and_matches_the_loaded_path(
    tmp_path: Path,
) -> None:
    """이게 없으면 "전부 막혔다"가 "생성자가 망가졌다"와 구별되지 않는다 —
    정상 구성은 그대로 만들어지고, 파일 해석과 같은 객체·같은 해시를 낸다.
    """
    kwargs: dict[str, object] = {
        "vision_max_images": 8,
        "keep_seconds": Fraction(2),
        "vision_long_edge": 1024,
        "pricing_currency": "KRW",
        "vision_cost_per_image": Decimal("0.02"),
    }
    built = ProjectConfig(**kwargs)
    loaded = load_project_config(
        _write(tmp_path / "c.project.toml", "schema_version = 1\n"), kwargs
    )
    assert built == loaded
    assert built.sha256() == loaded.sha256()
    assert built.keep_seconds == Fraction(2)
    assert built.vision_cost_per_image == Decimal("0.02")


def test_the_constructor_normalizes_like_the_file_path() -> None:
    """정수 분수·정수 실수도 파일 해석과 같은 정규형으로 들어간다 — 해시는 하나다."""
    as_int = ProjectConfig(keep_seconds=2, vad_positive=1)
    as_exact = ProjectConfig(keep_seconds=Fraction(2), vad_positive=1.0)
    assert as_int == as_exact
    assert as_int.sha256() == as_exact.sha256()
    assert as_int.to_dict()["keep_seconds"] == "2/1"
    assert isinstance(as_int.vad_positive, float)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("vad_positive", 0, id="vad-positive-zero"),
        pytest.param("vad_positive", 1, id="vad-positive-one"),
        pytest.param("vision_apply_confidence", 0.0, id="confidence-zero"),
        pytest.param("vision_apply_confidence", 1.0, id="confidence-one"),
    ],
)
def test_constructor_bounds_are_inclusive(field: str, value: float) -> None:
    """레일 양 끝은 프로그래머의 정당한 선택이다 — 문은 닫히되 조여 붙이지 않는다."""
    assert getattr(ProjectConfig(**{field: value}), field) == float(value)


def test_constructor_allows_block_ordering_equality() -> None:
    """생성에서도 등호는 허용한다 — 거부하는 건 min > max와 keep > min뿐이다."""
    config = ProjectConfig(
        block_min_seconds=Fraction(10),
        block_max_seconds=Fraction(10),
        keep_seconds=Fraction(10),
    )
    assert config.block_min_seconds == config.block_max_seconds == config.keep_seconds


# ─── P5-00: 자격증명 분리 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ('vision_api_key_env = "OPENAI_API_KEY"', "OPENAI_API_KEY"),
        ('vision_base_url_env = "OPENAI_BASE_URL"', "OPENAI_BASE_URL"),
    ],
)
def test_reusing_stt_credential_names_is_rejected(tmp_path: Path, line: str, name: str) -> None:
    """비전 자격증명은 원본 전사(STT) 자격증명과 다른 이름이어야 한다."""
    path = _write(tmp_path / "cred.project.toml", f"schema_version = 1\n{line}\n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    assert name in str(excinfo.value)
    assert "vision" in str(excinfo.value)


def test_loading_reads_nothing_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """설정 적재는 환경을 보지 않는다 — 자격증명·가격 값은 오직 명시적 계층에서."""
    monkeypatch.chdir(tmp_path)  # 기본 파일도 없다 → 코드 기본값
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ZAI_API_KEY", "ZAI_BASE_URL"):
        monkeypatch.setenv(name, "set-by-test")
    monkeypatch.setenv("VLOGSRT_PRICING_CURRENCY", "USD")
    config = load_project_config(None)
    assert config == ProjectConfig()
    assert config.sha256() == ProjectConfig().sha256()


# ─── P5-00: 선택적 가격 ───────────────────────────────────────────────


def test_price_without_currency_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "p.project.toml", 'schema_version = 1\nvision_cost_per_image = "0.01"\n'
    )
    with pytest.raises(ProjectConfigError, match="pricing_currency"):
        load_project_config(path)


def test_blank_currency_with_price_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "p.project.toml",
        'schema_version = 1\npricing_currency = ""\nvision_cost_per_image = "0.01"\n',
    )
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path)
    assert "pricing_currency" in str(excinfo.value)


def test_currency_alone_without_prices_is_allowed(tmp_path: Path) -> None:
    path = _write(tmp_path / "p.project.toml", 'schema_version = 1\npricing_currency = "USD"\n')
    config = load_project_config(path)
    assert config.pricing_currency == "USD"
    for key in _PRICE_KEYS:
        assert getattr(config, key) is None, key


@pytest.mark.parametrize("key", _PRICE_KEYS)
def test_each_price_component_is_separately_priced(tmp_path: Path, key: str) -> None:
    """구성 요소는 따로 값이 매겨진다 — 한 모델 요금이 다른 요금을 덮지 못한다."""
    body = f'schema_version = 1\npricing_currency = "USD"\n{key} = "0.02"\n'
    config = load_project_config(_write(tmp_path / "p.project.toml", body))
    assert getattr(config, key) == Decimal("0.02")
    for other in _PRICE_KEYS:
        if other != key:
            assert getattr(config, other) is None, other


def test_price_spellings_canonicalize_to_one_hash(tmp_path: Path) -> None:
    """같은 값의 다른 철자("0.010"·"0.01")는 하나의 정규형 십진 문자열로 해시에 든다."""
    first = load_project_config(
        _write(
            tmp_path / "a.project.toml",
            'schema_version = 1\npricing_currency = "USD"\nvision_cost_per_image = "0.010"\n',
        )
    )
    second = load_project_config(
        _write(
            tmp_path / "b.project.toml",
            'schema_version = 1\npricing_currency = "USD"\nvision_cost_per_image = "0.01"\n',
        )
    )
    assert first == second
    assert first.sha256() == second.sha256()
    assert first.to_dict()["vision_cost_per_image"] == "0.01"
    assert first.vision_cost_per_image == Decimal("0.01")


def test_high_precision_price_hash_is_context_independent(tmp_path: Path) -> None:
    """정규형은 문맥 정밀도에 무관해야 한다 — 반올림은 정규화가 아니라 훼손이다.

    `Decimal.normalize()`는 현재 문맥 정밀도(기본 28)로 반올림한다. 유효숫자가
    그보다 긴 가격이 문맥에 따라 다르게 직렬화되면 같은 파일이 세 개의 해시를
    낸다. 정밀도 6·28·50 어디에서도 값 그대로 하나의 해시여야 한다.
    """
    long_price = "0.123456789012345678901234567890123456789"  # 유효숫자 39자리
    body = f'schema_version = 1\npricing_currency = "USD"\nvision_cost_per_image = "{long_price}"\n'
    path = _write(tmp_path / "prec.project.toml", body)
    hashes = set()
    with localcontext() as context:
        for precision in (6, 28, 50):
            context.prec = precision
            config = load_project_config(path)
            assert config.boundary_audio_cost_per_minute is None
            assert config.vision_cost_per_image == Decimal(long_price)
            canonical = config.to_dict()["vision_cost_per_image"]
            assert canonical == long_price, f"prec={precision}: 반올림이 들어왔다"
            hashes.add(config.sha256())
    assert len(hashes) == 1, "문맥 정밀도가 해시를 갈랐다"


def test_signed_decimal_zero_and_trailing_zeros_keep_one_hash(tmp_path: Path) -> None:
    """부호 붙은 0과 끝자리 0 철자는 하나의 값이다 — 해시도 하나여야 한다."""
    hashes = set()
    for index, spelling in enumerate(["0", "-0", "0.000", "+0"]):
        body = (
            f'schema_version = 1\npricing_currency = "USD"\nvision_cost_per_image = "{spelling}"\n'
        )
        config = load_project_config(_write(tmp_path / f"z{index}.project.toml", body))
        assert config.vision_cost_per_image == Decimal(0)
        assert config.to_dict()["vision_cost_per_image"] == "0"
        hashes.add(config.sha256())
    assert len(hashes) == 1


@pytest.mark.parametrize("key", ["vad_positive", "vision_apply_confidence"])
def test_signed_float_zero_hashes_identically(tmp_path: Path, key: str) -> None:
    """같은 값의 부호 철자("0.0"·"-0.0")는 해시도 같아야 한다."""
    path = _write(tmp_path / "z.project.toml", "schema_version = 1\n")
    positive = load_project_config(path, {key: 0.0})
    negative = load_project_config(path, {key: -0.0})
    assert positive == negative  # 값은 원래 같다 — 문제는 직렬화다
    assert positive.to_dict()[key] == 0.0
    assert negative.to_dict()[key] == 0.0
    # 바이트로 증명한다: dict 비교는 -0.0을 못 본다.
    assert json.dumps(positive.to_dict(), sort_keys=True) == json.dumps(
        negative.to_dict(), sort_keys=True
    )
    assert positive.sha256() == negative.sha256()


# ─── P5-00: overrides 명시적 덧층 ─────────────────────────────────────


def test_overrides_layer_explicitly_over_the_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "base.project.toml", "schema_version = 1\nvision_long_edge = 1024\n")
    config = load_project_config(path, {"vision_long_edge": 512, "vision_max_images": 8})
    assert config.vision_long_edge == 512
    assert config.vision_max_images == 8
    assert config.guard_padding_seconds == Fraction(1, 2)  # 건드리지 않은 값은 그대로


def test_overrides_work_over_absent_default_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """overrides는 파일 없이도 쓸 수 있다 — 두 번째 뿌리가 아니라 덧층이다."""
    monkeypatch.chdir(tmp_path)
    config = load_project_config(None, {"vision_model": "glm-5.3"})
    assert config.vision_model == "glm-5.3"
    assert config == ProjectConfig(vision_model="glm-5.3")


def test_empty_overrides_behave_like_none(tmp_path: Path) -> None:
    path = _write(tmp_path / "b.project.toml", "schema_version = 1\nvision_long_edge = 1024\n")
    assert load_project_config(path, {}) == load_project_config(path)


def test_overrides_reject_unknown_keys(tmp_path: Path) -> None:
    path = _write(tmp_path / "b.project.toml", "schema_version = 1\n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path, {"vision_temperature": 0.7})
    assert "vision_temperature" in str(excinfo.value)


def test_overrides_cannot_smuggle_unsupported_schema(tmp_path: Path) -> None:
    path = _write(tmp_path / "b.project.toml", "schema_version = 1\n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path, {"schema_version": 2})
    assert "schema_version" in str(excinfo.value)


def test_overrides_accept_native_fraction_and_decimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_project_config(
        None,
        {
            "keep_seconds": Fraction(2),
            "vision_cost_per_image": Decimal("0.0125"),
            "pricing_currency": "KRW",
        },
    )
    assert config.keep_seconds == Fraction(2)
    assert config.vision_cost_per_image == Decimal("0.0125")
    assert config.to_dict()["vision_cost_per_image"] == "0.0125"


def test_bad_override_values_name_the_overrides_layer(tmp_path: Path) -> None:
    path = _write(tmp_path / "b.project.toml", "schema_version = 1\n")
    with pytest.raises(ProjectConfigError) as excinfo:
        load_project_config(path, {"vad_positive": 5})
    message = str(excinfo.value)
    assert "vad_positive" in message
    assert "overrides" in message


def test_invalid_file_value_is_not_rescued_by_overrides(tmp_path: Path) -> None:
    """파일은 혼자서 성립해야 한다 — overrides가 나쁜 값을 덮어 구할 수 없다."""
    path = _write(tmp_path / "bad.project.toml", "schema_version = 1\nvad_sample_rate = 8000\n")
    with pytest.raises(ProjectConfigError, match="vad_sample_rate"):
        load_project_config(path, {"vad_sample_rate": 16000})


def test_overrides_cannot_repair_a_file_missing_schema_version(tmp_path: Path) -> None:
    path = _write(tmp_path / "broken.project.toml", "vision_long_edge = 512\n")
    with pytest.raises(ProjectConfigError, match="schema_version"):
        load_project_config(path, {"schema_version": 1})


# ─── P5-00: 공유 분석 어휘(analysis_models) ────────────────────────────


def test_analysis_status_is_the_frozen_vocabulary() -> None:
    """상태는 셋뿐이다 — 성공·빈 결과·오류. 갈래가 늘어도 이 집합은 못 늘어난다."""
    assert get_args(AnalysisStatus) == ("success", "empty", "error")


def test_analysis_failure_is_frozen_and_slotted() -> None:
    failure = AnalysisFailure(
        kind="vad_runtime", message="ONNX 세션을 만들지 못했다", retryable=True
    )
    assert failure.kind == "vad_runtime"
    assert failure.message == "ONNX 세션을 만들지 못했다"
    assert failure.retryable is True
    # 값 객체다 — 같은 내용은 같고, 내용은 못 바꾼다.
    assert failure == AnalysisFailure("vad_runtime", "ONNX 세션을 만들지 못했다", True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.retryable = False  # type: ignore[misc]
    assert AnalysisFailure.__dataclass_params__.frozen
    assert AnalysisFailure.__slots__ == ("kind", "message", "retryable")
    assert not hasattr(failure, "__dict__")


def test_analysis_models_stays_the_shared_vocabulary_only() -> None:
    """VAD·비전 고유 레코드는 이 파일에 없다 — 갈래별 하위 패키지의 소유다."""
    import vlogsrt.project.analysis_models as analysis_models

    dataclass_names = [
        name
        for name, cls in vars(analysis_models).items()
        if isinstance(cls, type) and dataclasses.is_dataclass(cls)
    ]
    assert dataclass_names == ["AnalysisFailure"]


def test_analysis_models_declares_the_branch_boundary() -> None:
    """문서로 선언된 선이다 — 두 갈래 어느 쪽도 이 공유 파일을 고치지 않는다."""
    import vlogsrt.project.analysis_models as analysis_models

    doc = analysis_models.__doc__ or ""
    assert "VAD" in doc and "비전" in doc


def test_analysis_models_imports_no_project_module() -> None:
    """공유 어휘는 project 안 그 무엇도 끌어들이지 않는다 — 의존 방향이 없다."""
    source = Path(sys.modules["vlogsrt.project.analysis_models"].__file__ or "").read_text(
        encoding="utf-8"
    )
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
        assert not offenders, f"공유 어휘가 project를 import한다: {node.lineno}번 줄 {offenders}"
