"""Final Cut 파생 계층의 설정 뿌리.

원본 `vlogsrt.config.Config`(whisper 파이프라인의 임계값)과는 **완전히 별개다** —
서로를 import하지도 상속하지도 않는다. 이 계층의 설정 파일은
`vlogsrt.project.toml` 하나뿐이다.

P5-00부터 이 뿌리가 파생 계층의 **모든** 튜너블을 소유한다: VAD 아티팩트 좌표와
창 설정, 비전 모델·자격증명 이름·샘플링 한도, 러프컷 블록 길이, 그리고 사용자가
명시한 예산 가정(가격). 모델 이름과 임계값 리터럴은 이 파일에만 산다 — 소비자는
숫자를 베끼지 않고 이 설정을 읽는다(계약 §7). 비전 자격증명은 원본 전사(STT)
자격증명과 **다른** 환경변수의 이름만 저장한다. 값 자체는 저장하지 않는다 —
값을 찾는 일은 명시적 온라인 실행기의 몫이다.

읽기는 결정적이고 부작용이 없다: 파일을 읽는 것 외에 아무것도 하지 않는다.
환경변수 탐색도, 작업 디렉터리 뒤지기도, 레거시 설정 기웃거리기도 없다.
`to_dict`/`sha256`는 정규형(분수는 '분자/분모', 가격은 정규형 십진 문자열)으로만
값을 내보내므로 같은 값의 다른 철자는 같은 해시를 낸다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_PROJECT_CONFIG_PATH = Path("vlogsrt.project.toml")

# VAD 좌표는 모델 아티팩트에 고정이다. 이 세 값은 취향이 아니라 silero-vad v6.2.1
# ONNX가 요구하는 입력 모양이다 — 다른 값이 오면 설정이 틀린 게 아니라 아티팩트가
# 바뀌었다는 뜻이므로 거부한다.
_PINNED_INT_FIELDS: dict[str, int] = {
    "vad_sample_rate": 16000,
    "vad_window_samples": 512,
    "vad_context_samples": 64,
}

# 원본 전사(STT) 자격증명의 환경변수 이름 — 비전이 물려받으면 한쪽 키가 양쪽을
# 다 열게 된다. 비전 자격증명은 별도 이름이어야 한다.
_STT_CREDENTIAL_ENV_NAMES = frozenset({"OPENAI_API_KEY", "OPENAI_BASE_URL"})

_HEX64 = re.compile(r"[0-9a-f]{64}")
_INTEGER = re.compile(r"[+-]?\d+")

_CONSTRUCTOR_SOURCE = "ProjectConfig 생성자"
"""프로그램식 구성의 진단 라벨 — 파일 경로가 없는 자리에서의 출처다."""


class ProjectConfigError(ValueError):
    """프로젝트 설정을 읽지 못했다. 메시지에 경로·키·버전을 담는다."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """`vlogsrt.project.toml`의 전부.

    VAD 좌표는 공식 태그 v6.2.1에서 검증됐다(ONNX 2,327,524바이트, 체크섬은
    필드 그대로). 가격은 사용자가 명시한 예산 가정이지 공급자 요금표의 주장이
    아니다 — 기본은 전부 `None`이다. 구성 요소별 가격이 따로 있는 이유는 한
    모델의 요금이 다른 모델 값을 조용히 대신 치르게 두지 않기 위해서다.
    """

    schema_version: int = SUPPORTED_SCHEMA_VERSION

    vad_model_version: str = "v6.2.1"
    vad_model_url: str = (
        "https://raw.githubusercontent.com/snakers4/silero-vad/"
        "v6.2.1/src/silero_vad/data/silero_vad.onnx"
    )
    vad_model_sha256: str = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
    vad_license_url: str = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/LICENSE"
    vad_license_sha256: str = "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"
    vad_sample_rate: int = 16000
    vad_window_samples: int = 512
    vad_context_samples: int = 64
    vad_positive: float = 0.35
    guard_padding_seconds: Fraction = Fraction(1, 2)

    vision_model: str = "gemini-3.8-flash"
    vision_api_key_env: str = "GOOGLE_API_KEY"
    vision_base_url_env: str = "GEMINI_BASE_URL"
    vision_long_edge: int = 768
    vision_sample_fps: Fraction = Fraction(1)
    vision_max_images: int = 16
    block_min_seconds: Fraction = Fraction(6)
    block_max_seconds: Fraction = Fraction(10)
    keep_seconds: Fraction = Fraction(3)
    vision_apply_confidence: float = 0.8
    silence_min_seconds: Fraction = Fraction(2)
    silence_handle_seconds: Fraction = Fraction(3, 10)

    pricing_currency: str | None = None
    source_audio_cost_per_minute: Decimal | None = None
    repair_verify_audio_cost_per_minute: Decimal | None = None
    boundary_audio_cost_per_minute: Decimal | None = None
    vision_cost_per_image: Decimal | None = None

    def to_dict(self) -> dict[str, object]:
        """정규형 사전 — 분수는 '분자/분모', 가격은 정규형 십진 문자열."""
        return {field.name: _serialize(getattr(self, field.name)) for field in fields(self)}

    def sha256(self) -> str:
        """정규형 JSON의 sha256. 해시는 해석된 값에만 붙는다."""
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __post_init__(self) -> None:
        """생성 우회를 닫는다 — 파일 해석이 거부하는 값은 생성도 거부한다.

        범위·교차 검사는 `load_project_config` 경로에서만 돌았다. 프로그램식
        구성은 그 검사를 건너뛰었고(P5-05b·P5-03b), 소비자마다 같은 검사를
        다시 짜야 했다. 필드별 범위 파서와 `_validate_cross`를 그대로 여기서도
        돌려 한 번에 닫는다 — 새 규칙은 없다. 파서가 돌려준 정규형 값을 다시
        싣어, 생성 경로와 파일 경로가 같은 객체·같은 해시를 내게 한다.
        """
        for field in fields(self):
            parsed = _FIELD_PARSERS[field.name](
                field.name, getattr(self, field.name), _CONSTRUCTOR_SOURCE
            )
            object.__setattr__(self, field.name, parsed)
        _validate_cross(self, _CONSTRUCTOR_SOURCE)


def load_project_config(
    path: Path | None = None, overrides: Mapping[str, object] | None = None
) -> ProjectConfig:
    """`vlogsrt.project.toml`을 읽는다. `path=None`이면 cwd의 기본 경로를 본다.

    기본 경로가 **정말 없을 때만** 코드 기본값을 돌려준다 — 그 자리에 디렉터리가
    있거나 파일이 있는데 `schema_version`가 없으면 에러다. **명시적으로 준 경로**가
    없어도 에러다 — 조용한 성공이 더 위험하기 때문이다. `overrides`는 그 위에
    얹는 **명시적 덧층**이다 — 파일을 대신하는 두 번째 뿌리도, 환경 탐색도 아니다.
    파일은 혼자서 성립해야 한다: 나쁜 파일 값을 overrides가 덮어 구하지 못한다.
    읽기·파싱·검증 전 과정에서 파일 외의 어떤 상태도 건드리지 않는다.
    """
    table, source = _base_table(path)
    parsed = _validate_table(table, source, require_schema_version=True)
    if overrides is not None:
        parsed.update(_validate_table(dict(overrides), "overrides", require_schema_version=False))
    config = ProjectConfig(**parsed)
    _validate_cross(config, source)
    return config


def _base_table(path: Path | None) -> tuple[dict[str, Any], str]:
    """(원본 테이블, 진단 라벨). 라벨은 에러 메시지가 경로 대신 쓴다."""
    if path is None:
        path = Path.cwd() / DEFAULT_PROJECT_CONFIG_PATH
        if not path.exists():
            return {"schema_version": SUPPORTED_SCHEMA_VERSION}, f"코드 기본값({path} 없음)"
        if not path.is_file():
            raise ProjectConfigError(f"프로젝트 설정 기본 경로가 파일이 아닙니다: {path}")
    elif not path.exists():
        raise ProjectConfigError(f"프로젝트 설정 파일이 없습니다: {path}")
    elif not path.is_file():
        raise ProjectConfigError(f"프로젝트 설정 경로가 파일이 아닙니다: {path}")
    return _read_table(path), str(path)


def _read_table(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ProjectConfigError(f"프로젝트 설정을 읽지 못했습니다: {path} ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(f"프로젝트 설정 TOML 문법 오류: {path} ({exc})") from exc


def _validate_table(
    table: Mapping[str, object], source: str, *, require_schema_version: bool
) -> dict[str, Any]:
    """테이블의 키를 검사하고, 있는 키만 해석해 돌려준다."""
    unknown = sorted(repr(key) for key in table if key not in _FIELD_PARSERS)
    if unknown:
        raise ProjectConfigError(
            f"{source}: 알 수 없는 프로젝트 설정 키 {unknown} — "
            f"이 계층의 키는 {sorted(_FIELD_PARSERS)}뿐이다"
        )
    if require_schema_version and "schema_version" not in table:
        # 파일이 있는데 이 키가 없으면 기본값으로 둔갑한다 — 없는 파일과는 다르다.
        raise ProjectConfigError(
            f"{source}: 프로젝트 설정에 schema_version가 없다 — 이 키는 생략할 수 없다"
        )
    return {key: _FIELD_PARSERS[key](key, value, source) for key, value in table.items()}


def _validate_cross(config: ProjectConfig, source: str) -> None:
    """한 키만 봐서는 못 잡는 계약 — 순서와 라벨 짝."""
    if config.block_min_seconds > config.block_max_seconds:
        raise ProjectConfigError(
            f"{source}: block_min_seconds가 block_max_seconds보다 크다 — "
            f"{config.block_min_seconds} > {config.block_max_seconds}"
        )
    if config.keep_seconds > config.block_min_seconds:
        raise ProjectConfigError(
            f"{source}: keep_seconds가 block_min_seconds보다 크다 — "
            f"{config.keep_seconds} > {config.block_min_seconds}"
        )
    prices = (
        config.source_audio_cost_per_minute,
        config.repair_verify_audio_cost_per_minute,
        config.boundary_audio_cost_per_minute,
        config.vision_cost_per_image,
    )
    if any(price is not None for price in prices) and config.pricing_currency is None:
        raise ProjectConfigError(f"{source}: 가격을 넣었으면 pricing_currency 라벨도 명시해야 한다")


def _serialize(value: object) -> object:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, float) and value == 0.0:
        return 0.0  # 부호 붙은 0은 하나다 — -0.0도 0.0으로 나간다
    return value


def _canonical_decimal(value: Decimal) -> str:
    """정규형 십진 문자열 — 문맥 정밀도에 무관하게 값 그대로.

    `normalize()`는 현재 문맥 정밀도(기본 28)로 반올림하므로 쓰지 않는다 —
    유효숫자가 긴 가격이 문맥에 따라 다른 직렬화를 낸다. 정확한 전개
    (`format(value, "f")`는 반올림 없이 값을 편다) 뒤에 **소수점 아래** 끝자리
    0만 줄이고, 부호 붙은 0은 하나("0")로 합친다.
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        return "0"
    return text


def _parse_schema_version(key: str, value: object, source: str) -> int:
    # bool은 int의 서브클래스다 — `true`가 1로 새어 들어가면 스키마 검사가 무력해진다.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectConfigError(
            f"{source}: {key}는 정수여야 한다 (bool도 정수가 아니다): {value!r}"
        )
    if value != SUPPORTED_SCHEMA_VERSION:
        raise ProjectConfigError(
            f"{source}: 지원하지 않는 {key} {value}다 — "
            f"이 코드는 {SUPPORTED_SCHEMA_VERSION}까지 안다"
        )
    return value


def _parse_pinned_int(key: str, value: object, source: str) -> int:
    expected = _PINNED_INT_FIELDS[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectConfigError(f"{source}: {key}는 정수여야 한다: {value!r}")
    if value != expected:
        raise ProjectConfigError(
            f"{source}: {key}는 silero-vad v6.2.1 아키텍처에 고정된 {expected}여야 한다: {value}"
        )
    return value


def _parse_positive_int(key: str, value: object, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectConfigError(f"{source}: {key}는 정수여야 한다: {value!r}")
    if value <= 0:
        raise ProjectConfigError(f"{source}: {key}는 양수여야 한다: {value}")
    return value


def _parse_unit_float(key: str, value: object, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectConfigError(f"{source}: {key}는 실수여야 한다: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ProjectConfigError(f"{source}: {key}는 유한해야 한다 (nan/inf는 못 쓴다): {value!r}")
    if not 0.0 <= number <= 1.0:
        raise ProjectConfigError(f"{source}: {key}는 0 이상 1 이하여야 한다: {number}")
    return number


def _parse_nonblank_str(key: str, value: object, source: str) -> str:
    if not isinstance(value, str):
        raise ProjectConfigError(f"{source}: {key}는 문자열이어야 한다: {value!r}")
    if not value.strip():
        raise ProjectConfigError(f"{source}: {key}는 비어 있으면 안 된다")
    return value


def _parse_optional_nonblank_str(key: str, value: object, source: str) -> str | None:
    if value is None:
        return None
    return _parse_nonblank_str(key, value, source)


def _parse_sha256_hex(key: str, value: object, source: str) -> str:
    text = _parse_nonblank_str(key, value, source)
    if not _HEX64.fullmatch(text):
        raise ProjectConfigError(f"{source}: {key}는 소문자 16진수 64자여야 한다: {text!r}")
    return text


def _parse_credential_env_name(key: str, value: object, source: str) -> str:
    name = _parse_nonblank_str(key, value, source)
    if name in _STT_CREDENTIAL_ENV_NAMES:
        raise ProjectConfigError(
            f"{source}: {key}는 원본 전사 자격증명 이름 {name!r}을 재사용할 수 없다 — "
            "비전 공급자는 별도 환경변수여야 한다"
        )
    return name


def _parse_positive_fraction(key: str, value: object, source: str) -> Fraction:
    fraction = _coerce_fraction(key, value, source)
    if fraction <= 0:
        raise ProjectConfigError(f"{source}: {key}는 양수여야 한다: {fraction}")
    return fraction


def _coerce_fraction(key: str, value: object, source: str) -> Fraction:
    """분수 필드는 정확해야 한다 — 실수는 0.1조차 1/10이 아니다."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    if isinstance(value, str):
        return _fraction_from_text(key, value, source)
    raise ProjectConfigError(
        f"{source}: {key}는 '분자/분모' 문자열이나 정수여야 한다 (실수는 정확하지 않다): {value!r}"
    )


def _fraction_from_text(key: str, text: str, source: str) -> Fraction:
    numerator, separator, denominator = text.strip().partition("/")
    if not separator:
        raise ProjectConfigError(f"{source}: {key}는 '분자/분모' 꼴이어야 한다: {text!r}")
    if not _INTEGER.fullmatch(numerator) or not _INTEGER.fullmatch(denominator):
        raise ProjectConfigError(f"{source}: {key}의 분자·분모는 정수여야 한다: {text!r}")
    denominator_value = int(denominator)
    if denominator_value <= 0:
        # 부호는 분자가 든다 — 분모가 음수면 같은 값의 두 철자가 생긴다.
        raise ProjectConfigError(
            f"{source}: {key}의 분모는 양수여야 한다 (부호는 분자가 든다): {text!r}"
        )
    return Fraction(int(numerator), denominator_value)


def _parse_price(key: str, value: object, source: str) -> Decimal | None:
    """가격은 사용자가 명시한 예산 가정이다 — 다른 어디서도 오지 않는다."""
    if value is None:
        return None
    price = _coerce_decimal(key, value, source)
    if not price.is_finite():
        raise ProjectConfigError(f"{source}: {key}는 유한해야 한다 (nan/inf는 못 쓴다): {value!r}")
    if price < 0:
        raise ProjectConfigError(f"{source}: {key}는 음수일 수 없다: {price}")
    return price


def _coerce_decimal(key: str, value: object, source: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            raise ProjectConfigError(f"{source}: {key}를 숫자로 읽지 못했다: {value!r}") from None
    # 실수는 돈의 정확한 표기가 아니다 — TOML은 문자열로만 공급한다(계약).
    raise ProjectConfigError(
        f"{source}: {key}는 문자열(또는 Decimal)이어야 한다 (실수는 정확하지 않다): {value!r}"
    )


# 키 하나, 해석기 하나. 여기 없는 키는 전부 거부한다 — 조용히 넘기는 순간
# 오타가 기본값으로 둔갑한다.
_FIELD_PARSERS: dict[str, Callable[[str, object, str], Any]] = {
    "schema_version": _parse_schema_version,
    "vad_model_version": _parse_nonblank_str,
    "vad_model_url": _parse_nonblank_str,
    "vad_model_sha256": _parse_sha256_hex,
    "vad_license_url": _parse_nonblank_str,
    "vad_license_sha256": _parse_sha256_hex,
    "vad_sample_rate": _parse_pinned_int,
    "vad_window_samples": _parse_pinned_int,
    "vad_context_samples": _parse_pinned_int,
    "vad_positive": _parse_unit_float,
    "guard_padding_seconds": _parse_positive_fraction,
    "vision_model": _parse_nonblank_str,
    "vision_api_key_env": _parse_credential_env_name,
    "vision_base_url_env": _parse_credential_env_name,
    "vision_long_edge": _parse_positive_int,
    "vision_sample_fps": _parse_positive_fraction,
    "vision_max_images": _parse_positive_int,
    "block_min_seconds": _parse_positive_fraction,
    "block_max_seconds": _parse_positive_fraction,
    "keep_seconds": _parse_positive_fraction,
    "vision_apply_confidence": _parse_unit_float,
    "silence_min_seconds": _parse_positive_fraction,
    "silence_handle_seconds": _parse_positive_fraction,
    "pricing_currency": _parse_optional_nonblank_str,
    "source_audio_cost_per_minute": _parse_price,
    "repair_verify_audio_cost_per_minute": _parse_price,
    "boundary_audio_cost_per_minute": _parse_price,
    "vision_cost_per_image": _parse_price,
}
