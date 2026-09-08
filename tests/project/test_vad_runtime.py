"""VAD 런타임 고정의 계약 (P5-01).

Silero v6.2.1 ONNX 아티팩트는 **설치된 꾸러미 자원**으로만 발견되고, 발견과
세션 만들기 양쪽 모두 실패 닫힘이다. 이 파일은 그 선을 지킨다. onnxruntime는
가짜로 갈아끼운다 — 단위 검사는 논리를 보는 것이지 실제 모델 호환성이 아니다
(실측 증거는 isolated 환경에서 따로 낸다). 해시·길이 검사만 진짜 꾸러미
바이트를 상대로 돈다. onnxruntime가 없는 환경에서도 전부 통과해야 한다 —
발견 계층은 선택 extra를 모르고, 세션 계층은 가짜를 쓴다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
import sys
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.config import ProjectConfig

# ─── 가짜 런타임 ────────────────────────────────────────────────────────


@dataclasses.dataclass(slots=True)
class _FakeNodeArg:
    """onnxruntime가 내놓는 NodeArg의 최소 모습 — 이름·모양·타입."""

    name: str
    shape: Sequence[int | None]
    type: str


def _official_inputs() -> list[_FakeNodeArg]:
    """공식 v6.2.1 입력 계약 — 이름·모양·타입 전부 핀이다."""
    return [
        _FakeNodeArg("input", [None, None], "tensor(float)"),
        _FakeNodeArg("state", [2, None, 128], "tensor(float)"),
        _FakeNodeArg("sr", [], "tensor(int64)"),
    ]


def _official_outputs() -> list[_FakeNodeArg]:
    return [
        _FakeNodeArg("output", [None, 1], "tensor(float)"),
        _FakeNodeArg("stateN", [None, None, None], "tensor(float)"),
    ]


class _FakeSessionOptions:
    def __init__(self) -> None:
        self.inter_op_num_threads = 0
        self.intra_op_num_threads = 0


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    inputs: Sequence[_FakeNodeArg] | None = None,
    outputs: Sequence[_FakeNodeArg] | None = None,
    reported_providers: Sequence[str] | None = None,
    init_error: Exception | None = None,
) -> list[dict[str, Any]]:
    """가짜 onnxruntime를 깔고, 세션 생성 호출 인자 기록을 돌려준다.

    `reported_providers`는 세션이 스스로 말하는 공급자 목록이다 — 호출 인자와
    다르게 놓아 "런타임이 조용히 공급자를 더하거나 바꾸는" 상황을 만든다.
    """
    created: list[dict[str, Any]] = []

    class _FakeInferenceSession:
        def __init__(
            self,
            path_or_bytes: object,
            sess_options: object = None,
            providers: Sequence[str] | None = None,
        ) -> None:
            if init_error is not None:
                raise init_error
            created.append(
                {
                    "path": str(path_or_bytes),
                    "sess_options": sess_options,
                    "providers": list(providers) if providers is not None else None,
                }
            )
            self._providers = (
                list(reported_providers)
                if reported_providers is not None
                else (
                    list(providers)
                    if providers is not None
                    else ["CPUExecutionProvider", "CoreMLExecutionProvider"]
                )
            )

        def get_providers(self) -> list[str]:
            return list(self._providers)

        def get_inputs(self) -> list[_FakeNodeArg]:
            return list(inputs) if inputs is not None else _official_inputs()

        def get_outputs(self) -> list[_FakeNodeArg]:
            return list(outputs) if outputs is not None else _official_outputs()

    module = types.ModuleType("onnxruntime")
    module.SessionOptions = _FakeSessionOptions
    module.InferenceSession = _FakeInferenceSession
    monkeypatch.setitem(sys.modules, "onnxruntime", module)
    return created


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _redirect_packaged_files(
    monkeypatch: pytest.MonkeyPatch, model: Path, license_file: Path
) -> None:
    """자원 발견을 임시 경로로 돌린다 — 거부 형태를 만드는 데만 쓴다."""
    from vlogsrt.project.vad import runtime

    def _packaged(basename: str) -> Path:
        return model if basename.endswith(".onnx") else license_file

    monkeypatch.setattr(runtime, "_packaged_file", _packaged)


# ─── 아티팩트 발견 ──────────────────────────────────────────────────────


def test_bundled_artifact_pins_concrete_verified_paths() -> None:
    """레코드의 경로는 실재하는 파일이고 해시는 실제 바이트에서 다시 난다."""
    from vlogsrt.project.vad.runtime import bundled_artifact

    cfg = ProjectConfig()
    artifact = bundled_artifact(cfg)
    assert artifact.model_path.name == "silero_vad_v6.2.1.onnx"
    assert artifact.license_path.name == "SILERO_LICENSE.txt"
    assert artifact.model_path.is_absolute()
    assert artifact.license_path.is_absolute()
    assert artifact.model_path.is_file()
    assert artifact.license_path.is_file()
    assert artifact.model_sha256 == cfg.vad_model_sha256
    assert artifact.license_sha256 == cfg.vad_license_sha256
    # 베낸 값이 아니라 파일에서 다시 계산한 해시와 같아야 한다.
    assert _sha256(artifact.model_path) == artifact.model_sha256
    assert _sha256(artifact.license_path) == artifact.license_sha256
    assert artifact.model_path.stat().st_size == 2_327_524


def test_discovery_ignores_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd를 옮겨도 같은 자원을 찾는다 — 발견은 cwd·루트 추측이 아니다."""
    from vlogsrt.project.vad.runtime import bundled_artifact

    monkeypatch.chdir(tmp_path)
    artifact = bundled_artifact(ProjectConfig())
    assert artifact.model_path.is_file()
    assert not artifact.model_path.is_relative_to(tmp_path)


def test_record_is_frozen_with_exactly_four_fields() -> None:
    from vlogsrt.project.vad.runtime import VadArtifact, bundled_artifact

    artifact = bundled_artifact(ProjectConfig())
    assert dataclasses.is_dataclass(artifact)
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.model_sha256 = "0" * 64  # type: ignore[misc]
    assert [field.name for field in dataclasses.fields(artifact)] == [
        "model_path",
        "model_sha256",
        "license_path",
        "license_sha256",
    ]
    assert type(artifact) is VadArtifact


def test_unavailable_is_a_runtime_error() -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable

    assert issubclass(VadUnavailable, RuntimeError)


def test_artifact_discovery_needs_no_optional_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """발견은 선택 extra 없이도 돈다 — 없으면 진단이 아니라 죽는 꼴이 된다."""
    from vlogsrt.project.vad.runtime import bundled_artifact

    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    monkeypatch.setitem(sys.modules, "numpy", None)
    artifact = bundled_artifact(ProjectConfig())
    assert artifact.model_path.is_file()


# ─── 발견 거부 형태 ─────────────────────────────────────────────────────


def test_missing_model_resource_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    real_license = bundled_artifact(ProjectConfig()).license_path
    _redirect_packaged_files(monkeypatch, tmp_path / "silero_vad_v6.2.1.onnx", real_license)
    with pytest.raises(VadUnavailable, match="silero_vad_v6\\.2\\.1\\.onnx"):
        bundled_artifact(ProjectConfig())


def test_directory_instead_of_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    real_license = bundled_artifact(ProjectConfig()).license_path
    model_dir = tmp_path / "silero_vad_v6.2.1.onnx"
    model_dir.mkdir()
    _redirect_packaged_files(monkeypatch, model_dir, real_license)
    with pytest.raises(VadUnavailable, match="파일이 아니다"):
        bundled_artifact(ProjectConfig())


def test_git_lfs_pointer_is_rejected_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    real_license = bundled_artifact(ProjectConfig()).license_path
    pointer = tmp_path / "silero_vad_v6.2.1.onnx"
    pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 123\n",
        encoding="utf-8",
    )
    _redirect_packaged_files(monkeypatch, pointer, real_license)
    with pytest.raises(VadUnavailable, match="LFS"):
        bundled_artifact(ProjectConfig())


def test_changed_model_bytes_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """길이는 같아도 한 바이트가 다르면 체크섬이 잡는다."""
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    real = bundled_artifact(ProjectConfig())
    bytes_ = bytearray(real.model_path.read_bytes())
    bytes_[100] ^= 0xFF
    swapped = tmp_path / "silero_vad_v6.2.1.onnx"
    swapped.write_bytes(bytes(bytes_))
    _redirect_packaged_files(monkeypatch, swapped, real.license_path)
    with pytest.raises(VadUnavailable, match="sha256"):
        bundled_artifact(ProjectConfig())


def test_truncated_model_is_rejected_on_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    real = bundled_artifact(ProjectConfig())
    truncated = tmp_path / "silero_vad_v6.2.1.onnx"
    truncated.write_bytes(real.model_path.read_bytes()[:-1])
    _redirect_packaged_files(monkeypatch, truncated, real.license_path)
    with pytest.raises(VadUnavailable, match="2327524"):
        bundled_artifact(ProjectConfig())


def test_padded_model_is_rejected_on_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    real = bundled_artifact(ProjectConfig())
    padded = tmp_path / "silero_vad_v6.2.1.onnx"
    padded.write_bytes(real.model_path.read_bytes() + b"\x00")
    _redirect_packaged_files(monkeypatch, padded, real.license_path)
    with pytest.raises(VadUnavailable, match="2327524"):
        bundled_artifact(ProjectConfig())


def test_config_version_drift_finds_no_artifact() -> None:
    """버전 좌표가 다른 설정은 다른 파일을 찾는다 — 없으니 거절이다."""
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    drifted = dataclasses.replace(ProjectConfig(), vad_model_version="v6.2.0")
    with pytest.raises(VadUnavailable, match="v6\\.2\\.0"):
        bundled_artifact(drifted)


def test_config_with_foreign_model_sha_is_rejected() -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    drifted = dataclasses.replace(ProjectConfig(), vad_model_sha256="0" * 64)
    with pytest.raises(VadUnavailable, match="sha256"):
        bundled_artifact(drifted)


def test_config_with_foreign_license_sha_is_rejected() -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact

    drifted = dataclasses.replace(ProjectConfig(), vad_license_sha256="1" * 64)
    with pytest.raises(VadUnavailable, match="sha256"):
        bundled_artifact(drifted)


# ─── 세션 만들기 ────────────────────────────────────────────────────────


def test_session_pins_single_threads_and_exactly_cpu_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vlogsrt.project.vad.runtime import bundled_artifact, create_cpu_session

    created = _install_fake_runtime(monkeypatch)
    artifact = bundled_artifact(ProjectConfig())
    session = create_cpu_session(artifact)
    assert session is not None
    assert len(created) == 1
    assert created[0]["path"] == str(artifact.model_path)
    options = created[0]["sess_options"]
    assert isinstance(options, _FakeSessionOptions)
    assert options.inter_op_num_threads == 1
    assert options.intra_op_num_threads == 1
    assert created[0]["providers"] == ["CPUExecutionProvider"]


def test_runtime_that_adds_a_provider_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(
        monkeypatch, reported_providers=["CPUExecutionProvider", "CoreMLExecutionProvider"]
    )
    artifact = bundled_artifact(ProjectConfig())
    with pytest.raises(VadUnavailable, match="CoreMLExecutionProvider"):
        create_cpu_session(artifact)


def test_runtime_with_empty_provider_list_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch, reported_providers=[])
    artifact = bundled_artifact(ProjectConfig())
    with pytest.raises(VadUnavailable, match="CPUExecutionProvider"):
        create_cpu_session(artifact)


@pytest.mark.parametrize(
    ("inputs", "outputs", "fragment"),
    [
        # 입력이 하나 더 늘었다
        (_official_inputs() + [_FakeNodeArg("extra", [None], "tensor(float)")], None, "입력"),
        # 입력 순서가 바뀌었다
        ([_official_inputs()[i] for i in (0, 2, 1)], None, "입력"),
        # 입력 이름이 다르다
        (
            [_FakeNodeArg("audio", [None, None], "tensor(float)")] + _official_inputs()[1:],
            None,
            "입력",
        ),
        # sr의 타입이 다르다
        (
            _official_inputs()[:2] + [_FakeNodeArg("sr", [], "tensor(float)")],
            None,
            "sr",
        ),
        # state의 모양이 다르다(문맥 64 → 32)
        (
            _official_inputs()[:1]
            + [_FakeNodeArg("state", [2, None, 64], "tensor(float)")]
            + _official_inputs()[2:],
            None,
            "shape",
        ),
        # 출력 이름이 다르다
        (
            None,
            [_FakeNodeArg("prob", [None, 1], "tensor(float)"), _official_outputs()[1]],
            "출력",
        ),
        # 출력 모양이 다르다
        (
            None,
            [_FakeNodeArg("output", [None, 2], "tensor(float)"), _official_outputs()[1]],
            "shape",
        ),
        # 출력 타입이 다르다
        (
            None,
            [_FakeNodeArg("output", [None, 1], "tensor(int64)"), _official_outputs()[1]],
            "출력",
        ),
    ],
)
def test_metadata_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    inputs: Sequence[_FakeNodeArg] | None,
    outputs: Sequence[_FakeNodeArg] | None,
    fragment: str,
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch, inputs=inputs, outputs=outputs)
    artifact = bundled_artifact(ProjectConfig())
    with pytest.raises(VadUnavailable, match=fragment):
        create_cpu_session(artifact)


def test_missing_onnxruntime_is_contextual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    artifact = bundled_artifact(ProjectConfig())
    with pytest.raises(VadUnavailable, match="onnxruntime"):
        create_cpu_session(artifact)


def test_broken_onnxruntime_module_is_contextual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모듈은 있지만 SessionOptions가 없는 반쯤 깨진 설치도 진단이 된다."""
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    artifact = bundled_artifact(ProjectConfig())
    with pytest.raises(VadUnavailable, match="onnxruntime"):
        create_cpu_session(artifact)


def test_session_constructor_failure_is_contextual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch, init_error=RuntimeError("onnx load failed"))
    artifact = bundled_artifact(ProjectConfig())
    with pytest.raises(VadUnavailable, match="onnx load failed"):
        create_cpu_session(artifact)


# ─── 세션 앞 재검증 ─────────────────────────────────────────────────────


def test_stale_record_with_wrong_hash_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """세션은 레코드를 믿지 않는다 — 지어낸 해시는 고정 계약에서 걸린다."""
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch)
    real = bundled_artifact(ProjectConfig())
    stale = dataclasses.replace(real, model_sha256="0" * 64)
    with pytest.raises(VadUnavailable, match="ProjectConfig"):
        create_cpu_session(stale)


def test_record_with_malformed_hash_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch)
    real = bundled_artifact(ProjectConfig())
    stale = dataclasses.replace(real, license_sha256="not-hex")
    with pytest.raises(VadUnavailable, match="sha256"):
        create_cpu_session(stale)


def test_tampered_file_behind_valid_record_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """레코드가 그럴듯해도 파일이 변했으면 세션 앞에서 걸린다."""
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch)
    real = bundled_artifact(ProjectConfig())
    tampered = tmp_path / "silero_vad_v6.2.1.onnx"
    tampered.write_bytes(real.model_path.read_bytes()[:-1])
    stale = dataclasses.replace(real, model_path=tampered)
    with pytest.raises(VadUnavailable, match="2327524"):
        create_cpu_session(stale)


def test_record_pointing_at_missing_license_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch)
    real = bundled_artifact(ProjectConfig())
    stale = dataclasses.replace(real, license_path=tmp_path / "gone.txt")
    with pytest.raises(VadUnavailable, match="gone\\.txt"):
        create_cpu_session(stale)


def test_git_lfs_pointer_behind_record_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

    _install_fake_runtime(monkeypatch)
    real = bundled_artifact(ProjectConfig())
    pointer = tmp_path / "silero_vad_v6.2.1.onnx"
    pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\n", encoding="utf-8"
    )
    stale = dataclasses.replace(real, model_path=pointer)
    with pytest.raises(VadUnavailable, match="LFS"):
        create_cpu_session(stale)


# ─── import 위생 ────────────────────────────────────────────────────────


def test_import_does_not_pull_optional_runtimes() -> None:
    """import만으로 numpy·onnxruntime를 끌어오면 발견 계층이 더러워진다."""
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; import vlogsrt.project.vad.runtime; "
        "assert 'onnxruntime' not in sys.modules, 'eager onnxruntime'; "
        "assert 'numpy' not in sys.modules, 'eager numpy'"
    )
    env = {**os.environ, "PYTHONPATH": str(root)}
    subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True)
