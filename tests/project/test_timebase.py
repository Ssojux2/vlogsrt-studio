"""정확 유리수 시각·프레임 격자의 계약 — P1 시간 인터페이스를 여기서 얼어 붙인다.

계획("Source analysis, clocks, identity")의 정본: XML 시간은 `Fraction` 그대로
다루고, 밀리초 양자화는 최종 직렬화에서 **딱 한 번** half-up 한다. 이 파일은
그 위반(조기 양자화·누적 반올림·float 산술)이 전부 잡히는지를 지킨다.
레거시 float 직렬화기(`vlogsrt.output`)는 바이트 그대로 남는다 — 같은 값이라도
이 계층의 정확 경로는 다른 문자열을 낼 수 있다(0.5005초 → 501ms vs float 500ms).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from fractions import Fraction

import pytest

from vlogsrt.output import format_timecode
from vlogsrt.project.timebase import (
    SUPPORTED_FRAME_DURATIONS,
    FrameInterval,
    format_fcpx_time,
    format_srt_time,
    frame_ceil,
    frame_floor,
    frame_nearest,
    interval_outward,
    milliseconds_half_up,
    parse_fcpx_time,
    require_supported_frame_duration,
)

# 계획 "Input and output"이 허락하는 프레임 길이 전부 — 24·25·30·50·60와
# 23.976·29.97·59.94(1001계). 이 집합 외의 rate는 정상 속도 매칭이 아니다.
EXPECTED_FRAME_DURATIONS = frozenset(
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

# FCPXML 왕복 증명용 분수 — 지원 rate 전부·경계 분수·음수 에포크를 다 담는다.
ROUNDTRIP_FRACTIONS = [
    Fraction(0),
    Fraction(1),
    Fraction(5),
    Fraction(3600),
    Fraction(107892),
    Fraction(1, 2),
    Fraction(2, 3),
    Fraction(1, 24),
    Fraction(1, 25),
    Fraction(1, 30),
    Fraction(1, 50),
    Fraction(1, 60),
    Fraction(1, 30000),
    Fraction(1001, 24000),
    Fraction(1001, 30000),
    Fraction(1001, 60000),
    Fraction(-5),
    Fraction(-3600),
    Fraction(-1, 30),
    Fraction(-1001, 30000),
]


def _parse_srt_seconds(text: str) -> Fraction:
    """직렬화된 `HH:MM:SS,mmm`을 다시 정확 분수로 — 오차 검증용."""
    hours, minutes, seconds = text.split(":")
    secs, millis = seconds.split(",")
    total_ms = int(hours) * 3_600_000 + int(minutes) * 60_000 + int(secs) * 1000 + int(millis)
    return Fraction(total_ms, 1000)


# ─── FCPXML 문자열 ↔ Fraction ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0s", Fraction(0)),
        ("5s", Fraction(5)),
        ("3600s", Fraction(3600)),
        ("1/2s", Fraction(1, 2)),
        ("10/4s", Fraction(5, 2)),
        ("1001/30000s", Fraction(1001, 30000)),
        ("5/1s", Fraction(5)),
    ],
)
def test_parse_accepts_integer_and_rational_seconds(text: str, expected: Fraction) -> None:
    """정수·분수 형태만 받는다 — 분수는 기약분수로 정규화된다."""
    assert parse_fcpx_time(text) == expected


def test_parse_accepts_signed_epochs_directly() -> None:
    """파서는 부호 붙은 정준 에포크를 그대로 읽는다 — 단일 인수, 별도 깃발 없음.

    음수 길이의 금지는 파서가 아니라 길이를 아는 호출자·검증기의 몫이다.
    """
    assert parse_fcpx_time("-5s") == Fraction(-5)
    assert parse_fcpx_time("-1/30s") == Fraction(-1, 30)
    assert parse_fcpx_time("-1001/30000s") == Fraction(-1001, 30000)


@pytest.mark.parametrize(
    "text",
    [
        "5",
        "s",
        "",
        " 5s",
        "5s ",
        "5 s",
        "5S",
        "+5s",
        "5.5s",
        "1/2",
        "1/2ss",
        "1 / 2s",
        "1/-30s",
        "1/2/3s",
    ],
)
def test_parse_rejects_malformed_time_strings(text: str) -> None:
    """접미사 누락·공백·부호·소수·이중 슬래시는 전부 형식 위반이다."""
    with pytest.raises(ValueError, match="FCPXML 시간 형식"):
        parse_fcpx_time(text)


def test_parse_rejects_zero_denominator() -> None:
    """0 분모는 분수가 아니다 — 조용히 무한대로 두지 않는다."""
    with pytest.raises(ValueError, match="분모"):
        parse_fcpx_time("1/0s")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(0), "0s"),
        (Fraction(5), "5s"),
        (Fraction(3600), "3600s"),
        (Fraction(1, 2), "1/2s"),
        (Fraction(10, 4), "5/2s"),
        (Fraction(1001, 30000), "1001/30000s"),
        (Fraction(-5), "-5s"),
        (Fraction(-1, 30), "-1/30s"),
    ],
)
def test_format_uses_fcpxml_canonical_forms(value: Fraction, expected: str) -> None:
    """정수 초는 `5s`, 그 외는 기약분수 `1/30s` — `Fraction`이 이미 기약이다."""
    assert format_fcpx_time(value) == expected


@pytest.mark.parametrize("value", ROUNDTRIP_FRACTIONS)
def test_roundtrip_preserves_exact_fraction(value: Fraction) -> None:
    """포맷→파싱 왕복은 한 톱니도 잃지 않는다 — 음수 에포크도 단일 인수로."""
    assert parse_fcpx_time(format_fcpx_time(value)) == value


# ─── 지원 프레임 길이 ───────────────────────────────────────────────────


def test_supported_frame_durations_are_exactly_the_plan_set() -> None:
    """여덟 rate 외에는 없다 — 23.976·29.97·59.94는 1001계 분수로만 온다."""
    assert SUPPORTED_FRAME_DURATIONS == EXPECTED_FRAME_DURATIONS


@pytest.mark.parametrize("duration", sorted(EXPECTED_FRAME_DURATIONS))
def test_require_returns_supported_duration(duration: Fraction) -> None:
    assert require_supported_frame_duration(duration) == duration


def test_require_accepts_normalized_equivalent() -> None:
    """`Fraction`은 이미 기약분수다 — 2/60은 1/30으로 정규화되어 통과한다."""
    assert require_supported_frame_duration(Fraction(2, 60)) == Fraction(1, 30)


@pytest.mark.parametrize(
    "duration",
    [
        Fraction(0),
        Fraction(1),
        Fraction(1, 15),
        Fraction(1, 29),
        Fraction(1, 48),
        Fraction(100, 2997),
        Fraction(1001, 12000),
    ],
)
def test_require_rejects_unsupported_durations(duration: Fraction) -> None:
    with pytest.raises(ValueError, match="프레임 길이"):
        require_supported_frame_duration(duration)


# ─── 프레임 격자 — 바닥·천장·반올림 ────────────────────────────────────


@pytest.mark.parametrize("duration", sorted(EXPECTED_FRAME_DURATIONS))
@pytest.mark.parametrize("anchor", [Fraction(0), Fraction(-3600), Fraction(1001, 30000) * 17])
@pytest.mark.parametrize("frame_index", [0, 1, 5, 1000, 107893])
def test_on_grid_value_lands_on_its_own_frame(
    duration: Fraction, anchor: Fraction, frame_index: int
) -> None:
    """격자 위 값은 셋 다 그 프레임 자체다 — 0프레임 항등 불일치가 없다."""
    on_grid = anchor + frame_index * duration
    assert frame_floor(on_grid, duration, anchor) == frame_index
    assert frame_ceil(on_grid, duration, anchor) == frame_index
    assert frame_nearest(on_grid, duration, anchor) == frame_index


@pytest.mark.parametrize("duration", sorted(EXPECTED_FRAME_DURATIONS))
@pytest.mark.parametrize("frame_index", [0, 2, 5, 107891])
def test_half_frame_tie_rounds_up(duration: Fraction, frame_index: int) -> None:
    """반 프레임 걸침(정확 k+1/2)은 위로 — 음수가 아닌 자리의 half-up 규칙이다."""
    half = Fraction(0) + (frame_index + Fraction(1, 2)) * duration
    assert frame_floor(half, duration) == frame_index
    assert frame_ceil(half, duration) == frame_index + 1
    assert frame_nearest(half, duration) == frame_index + 1


@pytest.mark.parametrize(
    ("offset_frames", "floor", "ceil", "nearest"),
    [
        (Fraction(1, 4), 0, 1, 0),
        (Fraction(3, 4), 0, 1, 1),
        (Fraction(49, 20), 2, 3, 2),
        (Fraction(51, 20), 2, 3, 3),
    ],
)
def test_sub_grid_values_split_floor_and_ceil(
    offset_frames: Fraction, floor: int, ceil: int, nearest: int
) -> None:
    """격자 사이 값은 바닥·천장이 갈라지고, 반올림은 이웃 프레임을 고른다."""
    duration = Fraction(1, 30)
    value = offset_frames * duration
    assert frame_floor(value, duration) == floor
    assert frame_ceil(value, duration) == ceil
    assert frame_nearest(value, duration) == nearest


def test_anchor_defaults_to_zero_epoch() -> None:
    """앵커를 주지 않으면 0초 에포크다 — 절대 앵커가 기본값이다."""
    assert frame_floor(Fraction(1, 60), Fraction(1, 30)) == 0
    assert frame_ceil(Fraction(1, 60), Fraction(1, 30)) == 1
    assert frame_nearest(Fraction(1, 60), Fraction(1, 30)) == 1


def test_negative_epoch_anchor_keeps_grid_relative() -> None:
    """음수 에포크(0보다 앞선 타임라인)에서도 격자는 앵커 기준으로 센다."""
    anchor = Fraction(-3600)
    duration = Fraction(1, 25)
    value = anchor + 7 * duration
    assert frame_floor(value, duration, anchor) == 7
    assert frame_ceil(value, duration, anchor) == 7
    assert frame_nearest(value, duration, anchor) == 7


def test_value_before_anchor_gives_negative_frame_indices() -> None:
    """앵커보다 앞선 값은 음수 인덱스다 — half-up은 양수 방향으로 동일하다."""
    anchor = Fraction(10)
    duration = Fraction(1, 30)
    one_and_half_before = anchor - Fraction(3, 2) * duration
    assert frame_floor(one_and_half_before, duration, anchor) == -2
    assert frame_ceil(one_and_half_before, duration, anchor) == -1
    # -1.5프레임의 반올림은 위로(-1) — half-up이지 0에서 멀어지는 것이 아니다.
    assert frame_nearest(one_and_half_before, duration, anchor) == -1
    half_before = anchor - Fraction(1, 2) * duration
    assert frame_nearest(half_before, duration, anchor) == 0


def test_30fps_and_2997_diverge_over_long_durations() -> None:
    """30과 30000/1001는 한 시간에 108프레임만큼 갈라진다 — 드롭프레임 보정량."""
    one_hour = Fraction(3600)
    whole = Fraction(1, 30)
    ntsc = Fraction(1001, 30000)
    assert frame_floor(one_hour, whole) == 108000
    # 3600 * 30000/1001 = 107892.107… — float 근사가 아니라 정확 몫의 바닥이다.
    assert frame_floor(one_hour, ntsc) == 107892
    assert frame_ceil(one_hour, ntsc) == 107893
    assert frame_floor(one_hour, ntsc) != frame_floor(one_hour, whole)
    # 23.976(24000/1001)도 제 갈래: 86313.68… → 86313
    assert frame_floor(one_hour, Fraction(1001, 24000)) == 86313
    # 59.94(60000/1001): 215784.21… → 215784
    assert frame_floor(one_hour, Fraction(1001, 60000)) == 215784
    # 1800초에서도 갈라진다: 54000 vs 53946.05…
    assert frame_floor(Fraction(1800), ntsc) == 53946


@pytest.mark.parametrize("duration", [Fraction(1, 29), Fraction(100, 2997)])
def test_frame_math_rejects_unsupported_duration(duration: Fraction) -> None:
    """격자 함수는 지원 rate만 받는다 — 검사 게이트를 우회하지 않는다."""
    value = Fraction(1)
    anchor = Fraction(0)
    with pytest.raises(ValueError, match="프레임 길이"):
        frame_floor(value, duration, anchor)
    with pytest.raises(ValueError, match="프레임 길이"):
        frame_ceil(value, duration, anchor)
    with pytest.raises(ValueError, match="프레임 길이"):
        frame_nearest(value, duration, anchor)
    with pytest.raises(ValueError, match="프레임 길이"):
        interval_outward(Fraction(0), value, duration, anchor)


# ─── 반개 구간 — 바깥쪽 스냅 ────────────────────────────────────────────


def test_frame_interval_is_frozen_with_public_slots() -> None:
    """불변 값 객체다 — 쓰기는 거부하고 slots는 공개 관찰로 증명한다."""
    interval = FrameInterval(start_frame=1, end_frame=2)
    with pytest.raises(FrozenInstanceError):
        interval.start_frame = 5  # type: ignore[misc]
    assert FrameInterval.__dataclass_params__.frozen
    assert "start_frame" in FrameInterval.__slots__
    assert interval == FrameInterval(start_frame=1, end_frame=2)


@pytest.mark.parametrize("duration", sorted(EXPECTED_FRAME_DURATIONS))
def test_outward_interval_covers_and_never_shrinks(duration: Fraction) -> None:
    """바닥+천장이라 원본을 항상 덮고, 반개라 끝 프레임 자체는 밖이다."""
    anchor = Fraction(1001, 30000) * 3
    start = anchor + Fraction(101, 4) * duration  # 25.25프레임
    end = anchor + Fraction(207, 4) * duration  # 51.75프레임
    interval = interval_outward(start, end, duration, anchor)
    assert interval == FrameInterval(start_frame=25, end_frame=52)
    assert anchor + interval.start_frame * duration <= start
    assert anchor + interval.end_frame * duration >= end


def test_on_frame_interval_is_identity() -> None:
    duration = Fraction(1, 24)
    interval = interval_outward(Fraction(0), 10 * duration, duration, Fraction(0))
    assert interval == FrameInterval(start_frame=0, end_frame=10)


def test_adjacent_intervals_share_boundary_frame_exactly() -> None:
    """앞 구간의 끝 == 뒤 구간의 시작 — 반개 구간끼리 겹치지도 벌어지지도 않는다."""
    duration = Fraction(1, 30)
    cut = Fraction(1, 2)  # 정확히 15프레임 자리
    first = interval_outward(Fraction(0), cut, duration, Fraction(0))
    second = interval_outward(cut, Fraction(1), duration, Fraction(0))
    assert first == FrameInterval(start_frame=0, end_frame=15)
    assert second == FrameInterval(start_frame=15, end_frame=30)
    assert first.end_frame == second.start_frame


def test_reversed_interval_raises() -> None:
    with pytest.raises(ValueError, match="뒤집"):
        interval_outward(Fraction(1, 30), Fraction(0), Fraction(1, 30), Fraction(0))


def test_point_interval_edge_follows_the_grid() -> None:
    """점 구간의 결과는 격자 위 여부를 따른다 — 빈 구간이 보장되지는 않는다.

    입력은 반개 `[start, end)`다. 바깥쪽 스냅(바닥+천장) 때문에 격자 밖의 점은
    한 프레임을 보수적으로 덮고, 격자 위의 점만 빈 구간이 된다.
    """
    duration = Fraction(1, 30)
    on_grid = Fraction(1, 2)  # 정확히 15프레임 자리
    assert interval_outward(on_grid, on_grid, duration, Fraction(0)) == FrameInterval(
        start_frame=15, end_frame=15
    )
    off_grid = Fraction(1, 60)  # 0.5프레임 자리 — 보수적으로 한 프레임
    assert interval_outward(off_grid, off_grid, duration, Fraction(0)) == FrameInterval(
        start_frame=0, end_frame=1
    )


# ─── SRT 직렬화 — 정확 half-up, 딱 한 번 ────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected_ms"),
    [
        (Fraction(0), 0),
        (Fraction(1, 1000), 1),
        (Fraction(1, 2), 500),
        (Fraction(499, 1000), 499),
        (Fraction(1001, 2000), 501),
        (Fraction(1, 3), 333),
        (Fraction(2, 3), 667),
        (Fraction(1), 1000),
        (Fraction(24821, 2000), 12411),
        (Fraction(3600), 3_600_000),
    ],
)
def test_milliseconds_half_up_definition(value: Fraction, expected_ms: int) -> None:
    """floor(초*1000 + 1/2) — 정확 0.5ms 걸침(1001/2000)은 위로 간다."""
    assert milliseconds_half_up(value) == expected_ms


def test_exact_counterexample_rounds_half_up_not_like_float() -> None:
    """정확 0.5005초는 500.5ms다 — half-up이면 501. float 경로의 500이 아니다."""
    assert format_srt_time(Fraction(1001, 2000)) == "00:00:00,501"


def test_legacy_float_serializer_stays_unchanged() -> None:
    """레거시 바이트는 그대로다 — float 0.5005는 500.4999…ms라 500을 낸다."""
    assert format_timecode(0.5005) == "00:00:00,500"


def test_legacy_float_conversion_rule_is_fraction_of_str() -> None:
    """float를 들일 수 있는 길은 `Fraction(str(value))` 하나뿐이다."""
    assert Fraction(str(0.5005)) == Fraction(1001, 2000)
    assert format_srt_time(Fraction(str(0.5005))) == "00:00:00,501"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(0), "00:00:00,000"),
        (Fraction(1, 1000), "00:00:00,001"),
        (Fraction(999, 1000), "00:00:00,999"),
        (Fraction(1, 3), "00:00:00,333"),
        (Fraction(1), "00:00:01,000"),
        (Fraction(24821, 2000), "00:00:12,411"),
        (Fraction(2 * 3600 + 59 * 60 + 59, 1) + Fraction(999, 1000), "02:59:59,999"),
        (Fraction(3600), "01:00:00,000"),
    ],
)
def test_format_srt_time_shapes(value: Fraction, expected: str) -> None:
    assert format_srt_time(value) == expected


def test_format_srt_time_rejects_negative() -> None:
    """SRT 경과 시각은 0 기반이다 — 음수는 숨겨 바깥으로 가지 않는다."""
    with pytest.raises(ValueError, match="음수"):
        format_srt_time(Fraction(-1, 1000))


def test_serialization_error_stays_within_half_millisecond() -> None:
    """순수 직렬화 오차는 0.5ms 이내 — 1ms 허용선보다 타이트한 실제 보장."""
    step = Fraction(1, 3)  # float는 표현하지 못하는 스텝
    for k in range(2701):
        boundary = step * k
        text = format_srt_time(boundary)
        assert abs(_parse_srt_seconds(text) - boundary) <= Fraction(1, 2000)
    # 마지막 경계도 정확히 900초 — 양자화는 직렬화 순간 하나뿐이다.
    assert step * 2700 == Fraction(900)


def test_adjacent_cue_boundaries_serialize_identically() -> None:
    """같은 경계는 같은 문자열 — 앞 큐의 끝과 뒤 큐의 시작이 다르면 뒤틀린다."""
    step = Fraction(1, 7)
    for k in range(1, 501):
        boundary = step * k
        end_role = format_srt_time(boundary)
        start_role = format_srt_time(boundary)
        assert end_role == start_role


def test_accumulated_boundaries_match_direct_products_exactly() -> None:
    """누적 반올림이 없다 — 1/7초를 500번 쌓은 값도 곱셈 값과 같은 문자열."""
    step = Fraction(1, 7)
    accumulated = Fraction(0)
    for k in range(1, 501):
        accumulated += step
        assert format_srt_time(accumulated) == format_srt_time(step * k)


def test_hours_widen_past_two_digits() -> None:
    """두 자리를 넘는 시간은 자리를 넓힌다 — 자르지 않는다."""
    assert format_srt_time(Fraction(360000)) == "100:00:00,000"
