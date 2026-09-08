"""FCPXML 시간의 정본 — 정확 유리수와 프레임 격자.

XML 시간은 `Fraction` 그대로 다루고 밀리초 양자화는 최종 직렬화에서 **딱 한 번**
half-up 한다(계획 "Source analysis, clocks, identity"). 이 모듈은 float 산술을
전혀 하지 않는다 — 레거시 float 값은 호출자가 `Fraction(str(value))`로 바꿔서만
들어온다. 레거시 직렬화기(`vlogsrt.output`)와는 독립이다: 같은 경계라도 이쪽은
정확 half-up이라 바이트가 다를 수 있다(정확 0.5005초 → 501ms, float → 500ms).

프레임 인덱스는 앵커에서 프레임 길이만큼 놓인 격자의 정수 좌표다. 소스 격자는
`asset.start`에 앵커를 묶는 식으로 쓴다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction

SUPPORTED_FRAME_DURATIONS: frozenset[Fraction] = frozenset(
    {
        Fraction(1, 24),
        Fraction(1, 25),
        Fraction(1, 30),
        Fraction(1, 50),
        Fraction(1, 60),
        Fraction(1001, 24000),
        Fraction(1001, 30000),
        Fraction(1001, 60000),
    }
)
"""정상 속도 매칭이 허락하는 프레임 길이 전부(계획 "Input and output").

24·25·30·50·60과 23.976·29.97·59.94(1001계)다. `Fraction`은 생성 시점에
기약분수로 정규화하므로 집합에는 정규화된 값만 들어간다.
"""

# FCPXML 시간 형식: 정수 `3600s` 또는 분수 `1001/30000s`. 소수·공백·`+`는 실제
# 형식이 아니다 — 느슨하게 받으면 정준 출력과 어긋나는 입력이 조용히 통과한다.
_FCPX_TIME_PATTERN = re.compile(r"(-?\d+)(?:/(\d+))?s")

_HALF = Fraction(1, 2)
# SRT 포맷의 단위 사실(조정 임계값이 아니다) — 레거시 output.py와 같은 값.
_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60_000
_MS_PER_HOUR = 3_600_000


@dataclass(frozen=True, slots=True)
class FrameInterval:
    """반개 프레임 구간 `[start_frame, end_frame)` — 인덱스는 앵커 기준 정수다."""

    start_frame: int
    end_frame: int


def parse_fcpx_time(value: str) -> Fraction:
    """FCPXML 시간 문자열을 초 단위 `Fraction`으로.

    형식은 부호 붙은 정수 `3600s` 또는 분수 `1001/30000s`다. 접미사 누락·0 분모는
    `ValueError`다. 부호는 에포크의 일부라 그대로 읽는다 — 음수 길이의 금지는
    파서가 아니라 길이를 아는 호출자·검증기가 한다.
    """
    match = _FCPX_TIME_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(
            f"FCPXML 시간 형식이 아니다(정수 `3600s` 또는 분수 `1001/30000s`): {value!r}"
        )
    numerator_text, denominator_text = match.groups()
    numerator = int(numerator_text)
    denominator = int(denominator_text) if denominator_text is not None else 1
    if denominator == 0:
        raise ValueError(f"FCPXML 시간의 분모가 0이다: {value!r}")
    return Fraction(numerator, denominator)


def format_fcpx_time(value: Fraction) -> str:
    """`Fraction`을 FCPXML 정준형으로 — 정수 초는 `5s`, 나머지는 `1/30s`."""
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


def require_supported_frame_duration(value: Fraction) -> Fraction:
    """지원 프레임 길이면 그대로 돌려주고, 아니면 `ValueError`."""
    if value not in SUPPORTED_FRAME_DURATIONS:
        supported = ", ".join(str(duration) for duration in sorted(SUPPORTED_FRAME_DURATIONS))
        raise ValueError(f"지원하지 않는 프레임 길이 {value}초 — 지원되는 값: {supported}초")
    return value


def _frames_from_anchor(value: Fraction, frame_duration: Fraction, anchor: Fraction) -> Fraction:
    """`value`의 앵커 기준 프레임 좌표 — 격자 검사를 함께 한다."""
    require_supported_frame_duration(frame_duration)
    return (value - anchor) / frame_duration


def frame_floor(value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()) -> int:
    """`value`를 넘지 않는 가장 가까운 앞 프레임 인덱스."""
    return math.floor(_frames_from_anchor(value, frame_duration, anchor))


def frame_ceil(value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()) -> int:
    """`value`부터 뒤에 있는 가장 가까운 프레임 인덱스."""
    return math.ceil(_frames_from_anchor(value, frame_duration, anchor))


def frame_nearest(value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()) -> int:
    """가장 가까운 프레임 인덱스 — 반 프레임 걸침은 위로(half-up)."""
    return math.floor(_frames_from_anchor(value, frame_duration, anchor) + _HALF)


def interval_outward(
    start: Fraction, end: Fraction, frame_duration: Fraction, anchor: Fraction
) -> FrameInterval:
    """반개 입력 `[start, end)`를 덮는 반개 구간 — 시작은 바닥, 끝은 천장.

    뒤집힘(`end < start`)은 거부한다. 점 구간(시작==끝)의 결과는 격자가 정한다:
    격자 위의 점은 빈 구간, 격자 밖의 점은 한 프레임을 보수적으로 덮는다.
    """
    if end < start:
        raise ValueError(f"구간이 뒤집혔다: start={start}초 > end={end}초")
    return FrameInterval(
        start_frame=frame_floor(start, frame_duration, anchor),
        end_frame=frame_ceil(end, frame_duration, anchor),
    )


def milliseconds_half_up(value: Fraction) -> int:
    """밀리초로 half-up — `floor(초*1000 + 1/2)`. 양자화는 이곳에서만 일어난다."""
    return math.floor(value * _MS_PER_SECOND + _HALF)


def format_srt_time(value: Fraction) -> str:
    """`HH:MM:SS,mmm` — 정확 값에서 직렬화 순간에 딱 한 번 half-up 한다.

    같은 `Fraction`은 언제나 같은 문자열이다(누적 반올림 없음). SRT 경과 시각은
    0 기반이라 음수는 거부한다.
    """
    if value < 0:
        raise ValueError(f"SRT 시각은 음수일 수 없다: {value}초")
    total_ms = milliseconds_half_up(value)
    hours, remainder = divmod(total_ms, _MS_PER_HOUR)
    minutes, remainder = divmod(remainder, _MS_PER_MINUTE)
    secs, millis = divmod(remainder, _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
