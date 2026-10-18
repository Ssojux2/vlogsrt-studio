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


# ─────────────────────────────────────────────────────────────
#  자막 시각 확인(D55) — 표시 전용 판정의 기준
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SyncCheckSettings:
    """자막 시각 확인의 판정 기준 — **외부 근거 없음**, 실측 13개 영상으로 고른 조정 대상(D55).

    `ProjectConfig`·`RawWorkflowConfig`·`RawUnderstandingSettings`의 칸이 **아니다** —
    어느 분석·전사·자막 지문이나 캐시 열쇠에도 들어가지 않는다. 확인은 표시만 하므로
    기준을 바꿔도 다시 살 것이 없다(D47의 교훈: 사전째 해시하는 열쇠에 칸을 얹으면 이미
    받은 결과가 모두 낡는다). 기준의 지문은 보고서 파일 이름에만 실린다.

    실측(원본 18개 · Silero v6.2.1 · 문턱 0.35)의 여유는 얇다 — 초록 최소 Pτ .75 대
    노랑 최대 .70, 초록 최대 '이른 시작' .11 대 나쁜 쪽 최소 .27(D55 표).
    """

    version: str = "sync-check/v1"
    # 측정 창 — 발화·자막 칸을 이만큼 늘려 '가까이 있다'를 잰다(Pτ·Rτ의 τ).
    speech_reach_seconds: Fraction = Fraction(1, 2)
    # 자막 하나 둘레 이 안에서 말소리를 하나도 못 찾으면 '말소리를 못 찾음'.
    no_speech_reach_seconds: Fraction = Fraction(1)
    # 시각 어긋남을 찾아보는 폭(±). 끝에 걸린 최고점은 '잴 수 없음'이다.
    shift_search_seconds: Fraction = Fraction(10)
    # 잴 수 있는 최소 재료 — 이보다 적으면 '측정 불가'(초록이 아니다).
    min_cues: int = 3
    min_speech_seconds: Fraction = Fraction(3)
    # 어긋남 추정을 믿는 문 — 최고점의 z와 0초 대비 이득.
    offset_min_peak_z: float = 2.0
    offset_min_gain: Fraction = Fraction(5, 100)
    # 머리·가운데·꼬리 — 구간마다 따로 잰 어긋남의 폭이 '점점 밀림'이다.
    segments: int = 3
    # 말보다 이만큼 넘게 먼저 뜨고, 그 말이 자막 끝 + 이만큼 안에서 시작하면 '이른 시작'.
    early_lead_seconds: Fraction = Fraction(1)
    early_onset_reach_seconds: Fraction = Fraction(3)
    # 판정 — 빨강(어긋남).
    red_offset_seconds: Fraction = Fraction(7, 10)
    red_precision_below: Fraction = Fraction(45, 100)
    red_recall_below: Fraction = Fraction(60, 100)
    red_early_at_least: Fraction = Fraction(30, 100)
    red_no_speech_at_least: Fraction = Fraction(30, 100)
    # 판정 — 노랑(확인).
    amber_precision_below: Fraction = Fraction(72, 100)
    amber_recall_below: Fraction = Fraction(75, 100)
    amber_early_at_least: Fraction = Fraction(20, 100)
    amber_no_speech_at_least: Fraction = Fraction(20, 100)
    amber_drift_seconds: Fraction = Fraction(1)
    # 표시 전용 지문 — 정수 초 길이(이 오차 안)·붙은 시각. 판정에 쓰지 않는다.
    whole_second_tolerance_seconds: Fraction = Fraction(1, 1000)
    # 저장 PCM ↔ 원본 표본 대조(D32 값): 창 수·길이·포락선 칸·찾는 폭·허용 지연·상관 하한.
    spot_windows: int = 3
    spot_window_seconds: Fraction = Fraction(20)
    spot_envelope_seconds: Fraction = Fraction(1, 100)
    spot_search_seconds: Fraction = Fraction(1)
    spot_max_lag_seconds: Fraction = Fraction(1, 10)
    spot_min_correlation: float = 0.95
    # 원점 — 이보다 작으면 0으로 본다 · SRT의 어긋남이 원점과 이만큼 안이면 '오디오 원점 기준 SRT'.
    origin_negligible_seconds: Fraction = Fraction(1, 100)
    origin_match_seconds: Fraction = Fraction(1, 10)


SYNC_CHECK_SETTINGS = SyncCheckSettings()


def validate_sync_check_settings(settings: SyncCheckSettings) -> None:
    """값역·순서 검사 — 어긋나면 ProjectConfigError로 전부 보고한다."""
    problems = [*_sync_range_problems(settings), *_sync_order_problems(settings)]
    if problems:
        raise ProjectConfigError("자막 시각 확인 설정이 계약을 어긴다: " + "; ".join(problems))


def _sync_range_problems(settings: SyncCheckSettings) -> list[str]:
    positives: dict[str, Fraction | float] = {
        "speech_reach_seconds": settings.speech_reach_seconds,
        "no_speech_reach_seconds": settings.no_speech_reach_seconds,
        "shift_search_seconds": settings.shift_search_seconds,
        "min_speech_seconds": settings.min_speech_seconds,
        "early_lead_seconds": settings.early_lead_seconds,
        "red_offset_seconds": settings.red_offset_seconds,
        "amber_drift_seconds": settings.amber_drift_seconds,
        "spot_window_seconds": settings.spot_window_seconds,
        "spot_envelope_seconds": settings.spot_envelope_seconds,
        "spot_search_seconds": settings.spot_search_seconds,
        "offset_min_peak_z": settings.offset_min_peak_z,
    }
    problems = [
        f"{name}는 양수여야 한다: {value}" for name, value in positives.items() if value <= 0
    ]
    counts = {"min_cues": settings.min_cues, "segments": settings.segments}
    counts["spot_windows"] = settings.spot_windows
    problems += [f"{name}는 1 이상 정수여야 한다" for name, value in counts.items() if value < 1]
    shares = {
        name: getattr(settings, name)
        for name in (
            "offset_min_gain",
            "red_precision_below",
            "red_recall_below",
            "red_early_at_least",
            "red_no_speech_at_least",
            "amber_precision_below",
            "amber_recall_below",
            "amber_early_at_least",
            "amber_no_speech_at_least",
            "spot_min_correlation",
        )
    }
    problems += [f"{name}는 0~1이어야 한다: {v}" for name, v in shares.items() if not 0 <= v <= 1]
    return problems


def _sync_order_problems(settings: SyncCheckSettings) -> list[str]:
    """빨강은 노랑보다 엄해야 한다 — 거꾸로면 노랑 없이 빨강이 먼저 뜬다."""
    pairs = (
        ("Pτ", settings.red_precision_below, settings.amber_precision_below),
        ("Rτ", settings.red_recall_below, settings.amber_recall_below),
    )
    problems = [f"빨강 {name} 문턱이 노랑보다 높다" for name, red, amber in pairs if red > amber]
    shares = (
        ("이른 시작", settings.red_early_at_least, settings.amber_early_at_least),
        ("말소리 못 찾음", settings.red_no_speech_at_least, settings.amber_no_speech_at_least),
    )
    problems += [f"빨강 {name} 문턱이 노랑보다 낮다" for name, red, amber in shares if red < amber]
    if settings.spot_max_lag_seconds >= settings.spot_search_seconds:
        problems.append("표본 대조의 허용 지연이 찾는 폭보다 크다 — 어긋남을 못 본다")
    if settings.speech_reach_seconds >= settings.shift_search_seconds:
        problems.append("측정 창이 어긋남 찾는 폭보다 넓다")
    return problems


def sync_check_settings_sha256(settings: SyncCheckSettings) -> str:
    """기준의 지문 — 보고서 파일 이름에만 쓴다(어느 분석 지문에도 들어가지 않는다)."""
    payload = {field.name: _serialize(getattr(settings, field.name)) for field in fields(settings)}
    body = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SyncSnapSettings:
    """시각 맞춤 제안(D59 19g)의 기준 — 큐 경계를 말소리 경계에 맞춘 **제안**만 계산한다.

    `reach_seconds`만 새 조정 대상이다(외부 근거 없음 — 실측 18개로 고른 값, D59 19g). 나머지
    넷은 정제(`vlogsrt.config.Config` — refine)의 값을 **그대로** 옮겼다: 이 파일은 원본 설정을
    import하지 않으므로 숫자를 적고, 시험이 정확 유리수로 같음을 못 박는다. refine 값을 바꾸면
    여기도 같이 바꾼다.

    `ProjectConfig`·`RawWorkflowConfig`·`RawCaptionContract`·`RawUnderstandingSettings`·
    `SourceTextSettings`의 칸이 **아니고** 어느 분석·전사·자막 지문이나 캐시 열쇠·승인에도 실리지
    않는다 — 바꿔도 이미 산 것이 낡지 않는다. 기준은 제안의 sha에만 실린다(19h의 적용이 그 sha를
    대조한다).
    """

    version: str = "sync-snap/v1"
    # 경계에서 이만큼 안의 말소리 경계에만 맞춘다(±). 그 안에 말소리가 없으면 「못 찾음」 표시만.
    reach_seconds: Fraction = Fraction(1)
    # refine 값 그대로(Config.lead_in · lead_out · min_gap · min_duration).
    lead_in: Fraction = Fraction(1, 10)
    lead_out: Fraction = Fraction(3, 10)
    min_gap: Fraction = Fraction(8, 100)
    min_duration: Fraction = Fraction(85, 100)


SYNC_SNAP_SETTINGS = SyncSnapSettings()
"""지금 쓰는 시각 맞춤 기준 — 주입이 없으면 이 하나를 읽는다."""


def sync_snap_problems(settings: SyncSnapSettings) -> list[str]:
    """값역 — 찾는 폭·최소 길이는 양수, 여유·간격은 0 이상의 정확 유리수(초)다."""
    problems: list[str] = []
    if not isinstance(settings.version, str) or not settings.version.strip():
        problems.append(f"version은 비어 있지 않은 문자열이어야 한다: {settings.version!r}")
    for name in ("reach_seconds", "lead_in", "lead_out", "min_gap", "min_duration"):
        value = getattr(settings, name)
        positive = name in ("reach_seconds", "min_duration")
        if not isinstance(value, Fraction) or value < 0 or (positive and value == 0):
            kind = "양" if positive else "0 이상"
            problems.append(f"{name}은(는) {kind}의 정확 유리수(초)여야 한다: {value!r}")
    return problems


@dataclass(frozen=True, slots=True)
class TimingTruthSettings:
    """시각 정답 표본(W-25b, D67) — 영상마다 들어 볼 줄을 층마다 몇 개 권할지와 적중 폭.

    층별 줄 수는 외부 근거 없는 조정 대상이다(영상당 약 다섯 줄 — 사람이 한 영상에 들일 만한 품).
    적중 폭 ±200ms는 원본 평가(`vlogsrt.config.Config.start_tol`)의 값 **그대로**다 — 이 파일은 원본
    설정을 import하지 않으므로 숫자를 적고, 시험이 정확 유리수로 같음을 못 박는다.

    `ProjectConfig`·`RawWorkflowConfig`·`RawUnderstandingSettings`·`SourceTextSettings`의 칸이
    **아니고** 어느 지문·캐시 열쇠·승인·견적·등급에도 실리지 않는다 — 정답 표시와 보고서만 읽는다.
    """

    chunk_first_lines: int = 2
    no_speech_lines: int = 1
    whole_second_lines: int = 1
    ordinary_lines: int = 1
    hit_seconds: Fraction = Fraction(1, 5)


TIMING_TRUTH_SETTINGS = TimingTruthSettings()
"""지금 쓰는 시각 정답 기준 — 주입이 없으면 이 하나를 읽는다."""


def timing_truth_problems(settings: TimingTruthSettings) -> list[str]:
    """값역 — 층별 줄 수는 0 이상의 정수, 적중 폭은 양의 정확 유리수(초)다."""
    problems: list[str] = []
    for name in ("chunk_first_lines", "no_speech_lines", "whole_second_lines", "ordinary_lines"):
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{name}은(는) 0 이상의 정수여야 한다: {value!r}")
    if not isinstance(settings.hit_seconds, Fraction) or settings.hit_seconds <= 0:
        problems.append(f"hit_seconds는 양의 정확 유리수(초)여야 한다: {settings.hit_seconds!r}")
    return problems


@dataclass(frozen=True)
class GapRefillSettings:
    """말소리 빈칸 재전사(D59 19f)가 **다시 물을** 구간을 넓히는 기준 — 외부 근거 없는 조정 대상.

    재료는 자막 시각 확인(D55)의 「말소리 − 큐 ±τ」다(τ는 `SyncCheckSettings.speech_reach_seconds`).
    그 칸을 표본으로 옮긴 뒤 `merge_seconds` 이하로 떨어진 빈칸을 하나로 잇고(whisper가 앞뒤 말과
    함께 듣게 — 단 사이에 기존 큐 칸이 한 프레임이라도 있으면 잇지 않는다), 양쪽을 `pad_seconds`만큼
    넓혀 `[0, PCM 길이]`로 자른다. 넓힌 구간은 **통째로** 다시 묻는다(규칙 4 — 다시 묻기 위해 고르는
    것은 되고, 빼려고 고르지 않는다). 잇기가 큐 칸을 건너지 않고 넓힘이 τ 이하이므로 넓힌 구간은
    기존 큐 칸으로 들어가지 않는다(시험이 둘 다 못 박는다).

    `ProjectConfig`·`RawWorkflowConfig`·`RawUnderstandingSettings`·`SourceTextSettings`의 칸이
    **아니고** 어느 분석·전사·자막 지문이나 캐시 열쇠·1단계 승인에도 실리지 않는다 — 바꿔도 이미
    산 것이 낡지 않는다. 그 결과(고른 구간)만 빈칸 재전사 견적 digest와 그 승인이 묶는다.
    """

    merge_seconds: Fraction = Fraction(2)
    pad_seconds: Fraction = Fraction(1, 2)


GAP_REFILL_SETTINGS = GapRefillSettings()
"""지금 쓰는 빈칸 고르기 기준 — 주입이 없으면 이 하나를 읽는다."""


def gap_refill_problems(settings: GapRefillSettings) -> list[str]:
    """값역 — 잇는 거리·넓힘은 0 이상의 정확 유리수(초)다."""
    problems: list[str] = []
    for name in ("merge_seconds", "pad_seconds"):
        value = getattr(settings, name)
        if not isinstance(value, Fraction) or value < 0:
            problems.append(f"{name}은(는) 0 이상의 정확 유리수(초)여야 한다: {value!r}")
    return problems


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
