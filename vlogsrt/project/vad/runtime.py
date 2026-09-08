"""Silero VAD ONNX 런타임 고정 — 아티팩트 검증과 CPU 세션 (P5-01).

이 모듈은 추론을 하지 않는다(그건 P5-02의 스트림 상태가 한다). 하는 일은
둘뿐이다. 첫째, 꾸러미에 실은 silero-vad v6.2.1 ONNX와 라이선스를 **설치된
꾸러미 자원**으로만 발견해 길이·해시·Git LFS 포인터 여부로 검증한다(
`bundled_artifact`). 둘째, 그 검증을 다시 믿지 않고 세션을 만들기 직전에 한 번
더 대조한 뒤(`create_cpu_session`) onnxruntime를 CPU 전용 옵션으로만 올린다.

실패는 전부 `VadUnavailable`로 닫힌다 — 런타임 내려받기, 다른 버전·모델·
공급자로의 대체는 존재하지 않는다. numpy·onnxruntime는 선택 extra
(`pip install 'vlogsrt[project]'`)이므로 import는 함수 안에서만 일어난다.
numpy는 세션 만들기에 필요하지 않아 아예 import하지 않는다 — 입력을 만드는
P5-02가 가져올 것이다.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vlogsrt.project.config import ProjectConfig

_PACKAGE = "vlogsrt.project.vad"
_DATA_DIRECTORY = "data"
_LICENSE_BASENAME = "SILERO_LICENSE.txt"
# 공식 태그 v6.2.1 ONNX의 정확한 길이. sha256 핀과 함께 아티팩트 핀을 이룬다 —
# config이 길이 필드를 소유하지 않으므로 여기에 산다(바이트의 정체이지 취향이 아니다).
_MODEL_BYTES = 2_327_524
_CPU_PROVIDER = "CPUExecutionProvider"
_INTER_OP_THREADS = 1
_INTRA_OP_THREADS = 1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
_HASH_BLOCK_BYTES = 1 << 20


class VadUnavailable(RuntimeError):
    """VAD 런타임을 쓸 수 없다 — 메시지가 어느 좌표가 어긋났는지를 말한다."""


@dataclass(frozen=True, slots=True)
class VadArtifact:
    """검증된 아티팩트 한 쌍 — 모델과 라이선스의 구체적 경로와 sha256."""

    model_path: Path
    model_sha256: str
    license_path: Path
    license_sha256: str


@dataclass(frozen=True, slots=True)
class _TensorContract:
    """onnxruntime NodeArg가 지켜야 하는 이름·모양·타입."""

    name: str
    shape: tuple[int | None, ...]
    type: str


# 공식 v6.2.1의 그래프 계약. state 마지막 차원 128은 문맥 64의 두 배(양방향
# 상태)이고, sr은 스칼라다 — ProjectConfig의 핀(vad_context_samples 등)과 같은
# 사실의 다른 표기이므로 숫자를 베끼지 않고 세션 메타데이터와 대조만 한다.
_REQUIRED_INPUTS: tuple[_TensorContract, ...] = (
    _TensorContract("input", (None, None), "tensor(float)"),
    _TensorContract("state", (2, None, 128), "tensor(float)"),
    _TensorContract("sr", (), "tensor(int64)"),
)
_REQUIRED_OUTPUTS: tuple[_TensorContract, ...] = (
    _TensorContract("output", (None, 1), "tensor(float)"),
    _TensorContract("stateN", (None, None, None), "tensor(float)"),
)


def bundled_artifact(cfg: ProjectConfig) -> VadArtifact:
    """꾸러미에 실은 v6.2.1 아티팩트를 발견·검증해 레코드로 돌려준다.

    모델 파일 이름은 설정의 버전 좌표에서 만든다 — 버전이 어긋난 설정은
    존재하지 않는 파일을 찾게 되고 그 자체가 좌표 불일치 거절이다. 발견은
    설치된 꾸러미 자원(importlib.resources)으로만 한다: cwd 기웃거리기, 저장소
    루트 추측, 절대 경로 하드코딩은 없다.
    """
    model_path = _packaged_file(f"silero_vad_{cfg.vad_model_version}.onnx")
    license_path = _packaged_file(_LICENSE_BASENAME)
    _verify_resource(model_path, cfg.vad_model_sha256, "vad_model_sha256", _MODEL_BYTES)
    _verify_resource(license_path, cfg.vad_license_sha256, "vad_license_sha256", None)
    return VadArtifact(
        model_path=model_path,
        model_sha256=cfg.vad_model_sha256,
        license_path=license_path,
        license_sha256=cfg.vad_license_sha256,
    )


def create_cpu_session(artifact: VadArtifact) -> object:
    """레코드를 다시 검증한 뒤 CPU 전용 onnxruntime 세션을 만들어 돌려준다.

    레코드는 호출자가 지어낼 수 있으므로 세션을 만들기 전에 두 경로의 실제
    바이트를 레코드의 해시와 다시 대조한다 — 여기엔 cfg가 없으므로 대조 상대는
    레코드뿐이다(설정 좌표의 두 번째 뿌리를 만들지 않는다, 계약). 공급자는
    CPU 하나뿐이며, 런타임이 스스로 더하거나 바꾼 공급자 목록을 보고하면
    실패 닫힘이다.
    """
    _revalidate_record(artifact)
    onnxruntime = _load_onnxruntime()
    options = onnxruntime.SessionOptions()
    options.inter_op_num_threads = _INTER_OP_THREADS
    options.intra_op_num_threads = _INTRA_OP_THREADS
    try:
        session: Any = onnxruntime.InferenceSession(
            str(artifact.model_path), sess_options=options, providers=[_CPU_PROVIDER]
        )
    except Exception as exc:
        raise VadUnavailable(
            f"onnxruntime가 {artifact.model_path}로 세션을 만들지 못했다: {exc}"
        ) from exc
    _assert_cpu_only(session)
    _assert_metadata(session)
    return session


def _packaged_file(basename: str) -> Path:
    """설치된 꾸러미의 data/ 아래 파일 하나를 구체적 경로로 내놓는다."""
    try:
        resource = importlib.resources.files(_PACKAGE).joinpath(_DATA_DIRECTORY, basename)
    except (ImportError, TypeError, ValueError) as exc:
        raise VadUnavailable(f"꾸러미 {_PACKAGE}의 자원 {basename!r}을 찾지 못했다: {exc}") from exc
    # zip/압축 설치의 Traversable은 구체적 경로가 아니다 — 그대로 닫는다.
    if not isinstance(resource, Path):
        raise VadUnavailable(
            f"꾸러미 {_PACKAGE}의 자원 {basename!r}이 파일시스템 경로가 아니다 "
            "(zip/압축 설치는 지원하지 않는다)"
        )
    if not resource.is_file():
        raise VadUnavailable(
            f"꾸러미 자원 {resource}가 파일이 아니다 — 설치가 깨졌거나 "
            "설정의 아티팩트 좌표(버전·이름)가 꾸러미와 다르다"
        )
    return resource


def _verify_resource(
    path: Path, expected_sha256: str, config_key: str, expected_bytes: int | None
) -> None:
    """파일 하나를 파일 여부·LFS 포인터·길이·해시로 검증한다. 어긋나면 닫는다."""
    if not path.is_file():
        raise VadUnavailable(
            f"{config_key}: {path}가 파일이 아니다 — 설치가 깨졌거나 좌표가 다르다"
        )
    _reject_lfs_pointer(path)
    actual_bytes = _size_of(path)
    if expected_bytes is not None and actual_bytes != expected_bytes:
        raise VadUnavailable(
            f"{config_key}: {path}의 길이가 {actual_bytes}바이트다 — "
            f"공식 v6.2.1은 {expected_bytes}바이트여야 한다"
        )
    actual_sha256 = _sha256_of(path)
    if actual_sha256 != expected_sha256:
        raise VadUnavailable(
            f"{config_key}: {path}의 sha256가 계약과 다르다 — "
            f"파일 {actual_sha256}, 계약 {expected_sha256}"
        )


def _revalidate_record(artifact: VadArtifact) -> None:
    """세션 앞 재검증 — 두 경로의 실제 바이트를 레코드의 해시와 다시 대조한다.

    대조 상대는 레코드 자체뿐이다. cfg가 없는 이 경로에서 ProjectConfig 기본값을
    꺼내 비교하면 설정 좌표의 두 번째 뿌리가 생긴다 — bundled_artifact가 이미
    공급된 cfg와 대조해 레코드를 만들었으므로 여기는 파일만 본다.
    """
    _assert_record_hash_shape("model", artifact.model_sha256)
    _assert_record_hash_shape("license", artifact.license_sha256)
    _verify_resource(artifact.model_path, artifact.model_sha256, "model", _MODEL_BYTES)
    _verify_resource(artifact.license_path, artifact.license_sha256, "license", None)


def _assert_record_hash_shape(label: str, actual: str) -> None:
    """레코드의 해시는 16진수 64자여야 한다 — 아니면 재대조 메시지부터가 무의미하다."""
    if not _SHA256_HEX.fullmatch(actual):
        raise VadUnavailable(f"레코드의 {label} sha256이 16진수 64자가 아니다: {actual!r}")


def _reject_lfs_pointer(path: Path) -> None:
    """Git LFS 포인터는 바이트가 아니라 포인터다 — 해시가 같을 리 없지만 이름부터 잡는다."""
    try:
        with path.open("rb") as handle:
            head = handle.read(len(_LFS_POINTER_PREFIX))
    except OSError as exc:
        raise VadUnavailable(f"{path}를 읽지 못했다: {exc}") from exc
    if head.startswith(_LFS_POINTER_PREFIX):
        raise VadUnavailable(f"{path}는 Git LFS 포인터다 — 실제 모델 바이트가 아니게 반입됐다")


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise VadUnavailable(f"{path}의 상태를 읽지 못했다: {exc}") from exc


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_HASH_BLOCK_BYTES), b""):
                digest.update(block)
    except OSError as exc:
        raise VadUnavailable(f"{path}를 해시하지 못했다: {exc}") from exc
    return digest.hexdigest()


def _load_onnxruntime() -> Any:
    """onnxruntime를 함수 안에서만 끌어온다 — 없으면 extra 설치를 진단한다."""
    try:
        import onnxruntime
    except ImportError as exc:
        raise VadUnavailable(
            f"onnxruntime가 없다 — 선택 extra를 깔아야 한다: pip install 'vlogsrt[project]' ({exc})"
        ) from exc
    for attribute in ("SessionOptions", "InferenceSession"):
        if not hasattr(onnxruntime, attribute):
            raise VadUnavailable(f"onnxruntime 설치가 온전하지 않다 — {attribute}가 없다")
    return onnxruntime


def _assert_cpu_only(session: Any) -> None:
    """만들어진 세션이 스스로 말하는 공급자가 CPU 하나인지 증명한다."""
    try:
        providers = list(session.get_providers())
    except Exception as exc:
        raise VadUnavailable(f"세션의 공급자 목록을 읽지 못했다: {exc}") from exc
    if providers != [_CPU_PROVIDER]:
        raise VadUnavailable(
            f"세션이 CPU 전용이 아니다 — 요청은 [{_CPU_PROVIDER}]인데 런타임이 {providers}를 말한다"
        )


def _assert_metadata(session: Any) -> None:
    """그래프 계약 — 이름·모양·타입이 공식 v6.2.1과 정확히 같아야 한다."""
    try:
        inputs = list(session.get_inputs())
        outputs = list(session.get_outputs())
    except Exception as exc:
        raise VadUnavailable(f"세션 메타데이터를 읽지 못했다: {exc}") from exc
    _assert_tensors(inputs, _REQUIRED_INPUTS, "입력")
    _assert_tensors(outputs, _REQUIRED_OUTPUTS, "출력")


def _assert_tensors(
    actual: Sequence[Any], required: tuple[_TensorContract, ...], label: str
) -> None:
    actual_names = tuple(str(node.name) for node in actual)
    required_names = tuple(contract.name for contract in required)
    if actual_names != required_names:
        raise VadUnavailable(
            f"{label} 이름이 공식 v6.2.1과 다르다 — 계약 {required_names}, 런타임 {actual_names}"
        )
    for node, contract in zip(actual, required, strict=True):
        if str(node.type) != contract.type:
            raise VadUnavailable(
                f"{label} {contract.name}의 타입이 다르다 — "
                f"계약 {contract.type}, 런타임 {node.type}"
            )
        if _dims(node.shape) != contract.shape:
            raise VadUnavailable(
                f"{label} {contract.name}의 shape가 다르다 — "
                f"계약 {contract.shape}, 런타임 {node.shape}"
            )


def _dims(shape: Sequence[Any]) -> tuple[int | str | None, ...]:
    """None은 동적 차원으로 그대로, 정수는 값으로, 그 밖은 이름으로 정규화한다."""
    return tuple(
        None
        if dim is None
        else int(dim)
        if isinstance(dim, int) and not isinstance(dim, bool)
        else str(dim)
        for dim in shape
    )
