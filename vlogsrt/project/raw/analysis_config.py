"""첫 단계(A4) 의미 분석의 동결 설정 — 창·프레임·청크 한도의 값 원천.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7.4·§15.1의 `[understanding]`
값을 코드보다 먼저 못 박는다. 모델 이름은 여기에 없다 — 문구·시간·Luna 역할은
`raw/config.py`의 `RawModelRoles`가 이미 소유하므로 이 설정은 **값으로만** 읽는다
(모델 리터럴은 허용된 설정 경로에만 산다, 불변 규칙).

기본값은 계획의 초기 기준 그대로다 — 창 15초·문맥 2초·창당 8장·긴변 768.
ASR 청크 한도는 엔진의 동결 상한(180초·24MB)을 **천장으로** 다시 못 박는다:
이 설정을 낮추는 것은 회차별 조정이지만 올리는 것은 D26·D28이 닫은 길을
다시 여는 것이므로 검사가 거부한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

UNDERSTANDING_SCHEMA_VERSION = "raw-understanding/v1"
"""첫 단계 기록(analysis_transcript.json)의 namespace(계획 §13.1·§15.1)."""

DEFAULT_PROMPT_REVISION = "raw-suitability-v2"
"""Luna 안내문의 리비전 — 캐시 지문과 승인에 함께 실린다(§14.2)."""

MAX_ASR_CHUNK_SECONDS = 180.0
"""엔진의 동결 상한(D26·D28). 이 값을 넘는 청크 설정은 만들 수 없다."""

MAX_ASR_CHUNK_BYTES = 24_000_000
"""오디오 API의 25MB 한도 − 안전 여유. 청크 바이트 상한도 같은 선에 둔다."""


@dataclass(frozen=True)
class RawUnderstandingSettings:
    """첫 단계 분석의 조정값 — 크기·시간·개수는 코드의 매직 넘버가 아니라 여기서.

    `window_seconds`·`context_seconds`는 **분석 시간창**의 크기다(§7.4). 실제
    편집 조각의 길이가 아니다 — 조각은 Luna 판정 뒤 결정적 분할이 정한다.
    """

    schema_version: str = UNDERSTANDING_SCHEMA_VERSION
    window_seconds: float = 15.0
    context_seconds: float = 2.0
    max_images_per_window: int = 8
    image_long_edge: int = 768
    prompt_revision: str = DEFAULT_PROMPT_REVISION
    asr_chunk_max_seconds: float = MAX_ASR_CHUNK_SECONDS
    asr_chunk_max_bytes: int = MAX_ASR_CHUNK_BYTES


def validate_understanding_settings(settings: RawUnderstandingSettings) -> None:
    """설정의 값역 검사 — 어긋나면 ValueError로 전부 보고한다.

    NaN은 비교를 무력화하므로 유한성부터 본다(`vision/policy.py`의 재검사와 같은
    이유). bool은 int 서브클래스지만 개수·길이 좌표가 될 수 없다.
    """
    problems = understanding_settings_problems(settings)
    if problems:
        raise ValueError("understanding 설정이 계약을 어긴다: " + "; ".join(problems))


def understanding_settings_problems(settings: RawUnderstandingSettings) -> list[str]:
    """값역 검사의 본체 — 조립 문과 검사 문이 같은 검사를 지나게 한다."""
    problems: list[str] = []
    if settings.schema_version != UNDERSTANDING_SCHEMA_VERSION:
        problems.append(f"understanding schema는 {UNDERSTANDING_SCHEMA_VERSION}여야 한다")
    for name, value, ceiling in (
        ("창 길이", settings.window_seconds, None),
        ("문맥 길이", settings.context_seconds, None),
        ("ASR 청크 시간 상한", settings.asr_chunk_max_seconds, MAX_ASR_CHUNK_SECONDS),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{name}은 실수여야 한다: {value!r}")
            continue
        if not math.isfinite(float(value)):
            problems.append(f"{name}이 유한하지 않다: {value!r}")
        elif float(value) <= 0.0:
            problems.append(f"{name}은 양수여야 한다: {value!r}")
        elif ceiling is not None and float(value) > ceiling:
            problems.append(f"{name}은 동결 상한 {ceiling}초를 넘지 못한다: {value!r}")
    if (
        isinstance(settings.context_seconds, (int, float))
        and isinstance(settings.window_seconds, (int, float))
        and settings.context_seconds > settings.window_seconds
    ):
        problems.append("문맥이 창보다 길 수는 없다")
    for name, value, ceiling in (
        ("창당 이미지 한도", settings.max_images_per_window, None),
        ("이미지 긴변", settings.image_long_edge, None),
        ("ASR 청크 바이트 상한", settings.asr_chunk_max_bytes, MAX_ASR_CHUNK_BYTES),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{name}은 정수여야 한다: {value!r}")
        elif value < 1:
            problems.append(f"{name}은 1 이상이어야 한다: {value!r}")
        elif ceiling is not None and value > ceiling:
            problems.append(f"{name}은 동결 상한 {ceiling}을 넘지 못한다: {value!r}")
    if not isinstance(settings.prompt_revision, str) or not settings.prompt_revision.strip():
        problems.append(
            f"프롬프트 리비전은 비지 않은 문자열이어야 한다: {settings.prompt_revision!r}"
        )
    return problems
