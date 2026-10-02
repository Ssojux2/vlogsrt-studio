"""최종 자막(A7-2) 실행의 동결 설정 — 청크 한도와 실행 지문의 값 원천.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.6·§15.1의 최종 자막 값계를
코드보다 먼저 못 박는다. 모델 이름은 여기에 없다 — 문구·시간 역할은
`raw/config.py`의 `RawModelRoles`가 소유한다. 되풀이 복구·교정 게이트의 임계값도
여기서 다시 정의하지 않는다 — 엔진 `vlogsrt.config.Config`가 정본이고 이 계층은
그 값을 **값으로만** 읽는다(임계값은 하나의 config에서 온다, 규칙 8).

청크 시간 상한 180초는 OpenAI의 시간 제한이 아니라 **앱의 동결 기본값**이다(D26·
D28 — 38분 조각에서 영상의 41.4%가 되풀이됐다). 이 값을 올리는 설정은 만들 수
없다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

FINAL_CAPTION_SCHEMA_VERSION = "raw-caption-run/v2"
"""최종 자막 실행 기록(caption_trace.json 등)의 namespace(계획 §13.1·§15.1)."""

DEFAULT_CAPTION_PROMPT_REVISION = "raw-caption-v2"
"""최종 자막 실행 지문에 함께 실리는 안내문 리비전(§14.2 — 캐시·승인의 열쇠)."""

MAX_CAPTION_CHUNK_SECONDS = 180.0
"""엔진의 동결 상한(D26·D28). 이 값을 넘는 청크 설정은 만들 수 없다."""

MAX_CAPTION_CHUNK_BYTES = 24_000_000
"""오디오 API의 25MB 한도 − 안전 여유. 청크 바이트 상한도 같은 선에 둔다."""

DEFAULT_DRAFT_LANGUAGE = "ko"
"""초안 파일명의 언어 코드 — `draft.<lang>.srt`·`draft.<lang>.vtt`(§11.7)."""

DEFAULT_READING_SPEED_MAX_CPS = 20.0
"""읽기 속도 경고선(초당 글자 수) — Netflix TTSG 성인 기준 17~20의 상단(§11.7).

모델 정확도 보장이 아니라 검토용 초기 제안값이다. 경고만 만들 뿐 글자를 줄이거나
자막을 자르는 근거로 쓰지 않는다.
"""


@dataclass(frozen=True)
class FinalCaptionSettings:
    """최종 자막 실행의 조정값 — 문구·시간 두 경로가 **같은 청크**를 쓰는 한도(§11.3).

    `chunk_max_seconds`·`chunk_max_bytes`는 문구·시간 요청이 함께 쓰는 청크의
    상한이다. 두 경로가 같은 경계에서 대응되어야 교정·검증이 같은 오디오를 들었다고
    증명할 수 있다.
    """

    schema_version: str = FINAL_CAPTION_SCHEMA_VERSION
    chunk_max_seconds: float = MAX_CAPTION_CHUNK_SECONDS
    chunk_max_bytes: int = MAX_CAPTION_CHUNK_BYTES
    prompt_revision: str = DEFAULT_CAPTION_PROMPT_REVISION
    draft_language: str = DEFAULT_DRAFT_LANGUAGE
    reading_speed_max_cps: float = DEFAULT_READING_SPEED_MAX_CPS


def validate_final_caption_settings(settings: FinalCaptionSettings) -> None:
    """설정의 값역 검사 — 어긋나면 ValueError로 전부 보고한다.

    NaN은 비교를 무력화하므로 유한성부터 본다(`analysis_config`와 같은 이유).
    bool은 int의 서브클래스지만 바이트 상한 좌표가 될 수 없다.
    """
    problems = final_caption_settings_problems(settings)
    if problems:
        raise ValueError("final caption 설정이 계약을 어긴다: " + "; ".join(problems))


def _positive_number_problems(label: str, value: object, *, ceiling: float | None) -> list[str]:
    """양의 유한 실수 검사 — 상한이 있으면 그 위도 문제로 붙는다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [f"{label}은 실수여야 한다: {value!r}"]
    if not math.isfinite(float(value)):
        return [f"{label}이 유한하지 않다: {value!r}"]
    if float(value) <= 0.0:
        return [f"{label}은 양수여야 한다: {value!r}"]
    if ceiling is not None and float(value) > ceiling:
        return [f"{label}은 동결 상한 {ceiling}을 넘지 못한다: {value!r}"]
    return []


def final_caption_settings_problems(settings: FinalCaptionSettings) -> list[str]:
    """값역 검사의 본체 — 계획·검사 문이 같은 검사를 지나게 한다."""
    problems: list[str] = []
    if settings.schema_version != FINAL_CAPTION_SCHEMA_VERSION:
        problems.append(f"caption run schema는 {FINAL_CAPTION_SCHEMA_VERSION}여야 한다")
    problems += _positive_number_problems(
        "청크 시간 상한", settings.chunk_max_seconds, ceiling=MAX_CAPTION_CHUNK_SECONDS
    )
    problems += _positive_number_problems(
        "읽기 속도 경고선", settings.reading_speed_max_cps, ceiling=None
    )
    limit = settings.chunk_max_bytes
    if isinstance(limit, bool) or not isinstance(limit, int):
        problems.append(f"청크 바이트 상한은 정수여야 한다: {limit!r}")
    elif limit < 1:
        problems.append(f"청크 바이트 상한은 1 이상이어야 한다: {limit!r}")
    elif limit > MAX_CAPTION_CHUNK_BYTES:
        problems.append(
            f"청크 바이트 상한은 동결 상한 {MAX_CAPTION_CHUNK_BYTES}을 넘지 못한다: {limit!r}"
        )
    if not isinstance(settings.draft_language, str) or not settings.draft_language.strip():
        problems.append(
            f"초안 언어 코드는 비지 않은 문자열이어야 한다: {settings.draft_language!r}"
        )
    if not isinstance(settings.prompt_revision, str) or not settings.prompt_revision.strip():
        problems.append(
            f"프롬프트 리비전은 비지 않은 문자열이어야 한다: {settings.prompt_revision!r}"
        )
    return problems
