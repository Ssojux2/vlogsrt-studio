"""자를 자리 고르기 — **자리만 고르고 구간을 고르지 않는다**(불변 규칙 4).

순수 함수라 ffmpeg 없이 돈다. 진폭 배열과 가짜 탐색기만 주면 전부 테스트된다.
"""

from __future__ import annotations

from dataclasses import replace as data_replace

import pytest

from vlogsrt.config import Config
from vlogsrt.cutpoints import chunk_count, chunks_from_cuts, plan_cuts, quietest, target_boundaries

MB = 1_000_000


# ─────────────────────────────────────────────────────────────
#  조각 수 — 유일한 기준은 모델 최대 입력
# ─────────────────────────────────────────────────────────────


def test_small_audio_is_never_cut() -> None:
    assert chunk_count(5 * MB, Config()) == 1


def test_the_model_input_limit_is_the_only_criterion() -> None:
    cfg = Config()
    assert chunk_count(cfg.chunk_max_bytes, cfg) == 1
    assert chunk_count(cfg.chunk_max_bytes + 1, cfg) == 2
    assert chunk_count(cfg.chunk_max_bytes * 3, cfg) == 3


def test_a_77_minute_vlog_becomes_two_chunks() -> None:
    """64kbps 모노 ≈ 8000 B/s. 77분이면 37MB."""
    assert chunk_count(int(4620 * 8000), Config()) == 2


def test_lowering_the_limit_makes_more_chunks() -> None:
    """D13 반복 환청이 재발할 때의 탈출구. 코드가 아니라 설정 한 줄이다."""
    tight = data_replace(Config(), chunk_max_bytes=4_800_000)
    assert chunk_count(int(4620 * 8000), tight) == 8


def test_chunk_count_never_returns_zero() -> None:
    assert chunk_count(0, Config()) == 1


# ─────────────────────────────────────────────────────────────
#  목표 경계
# ─────────────────────────────────────────────────────────────


def test_one_chunk_has_no_interior_boundary() -> None:
    assert target_boundaries(600.0, 1) == []


def test_boundaries_split_the_audio_evenly() -> None:
    assert target_boundaries(600.0, 2) == [300.0]
    assert target_boundaries(600.0, 3) == pytest.approx([200.0, 400.0])


# ─────────────────────────────────────────────────────────────
#  가장 조용한 지점 — 임계값이 없다
# ─────────────────────────────────────────────────────────────

HOP = 0.02


def test_the_quietest_sample_wins() -> None:
    amplitudes = [0.9, 0.8, 0.0, 0.7, 0.9]
    assert quietest(amplitudes, HOP, 100.0, 100.05) == pytest.approx(100.0 + 2 * HOP)


def test_a_tie_goes_to_the_point_nearest_the_target() -> None:
    """0이 여럿이면 목표에서 가장 가까운 곳. 경계가 멀리 밀리면 조각이 기운다."""
    amplitudes = [0.0, 0.9, 0.9, 0.9, 0.0]
    near_start = quietest(amplitudes, HOP, 100.0, 100.0)
    near_end = quietest(amplitudes, HOP, 100.0, 100.08)
    assert near_start == pytest.approx(100.0)
    assert near_end == pytest.approx(100.0 + 4 * HOP)


def test_uniformly_loud_audio_cuts_at_the_target() -> None:
    """조용한 곳이 없어도 자른다. 여기서 멈추거나 구간을 버리지 않는다(규칙 4)."""
    amplitudes = [0.7] * 11
    target = 100.0 + 5 * HOP
    assert quietest(amplitudes, HOP, 100.0, target) == pytest.approx(target)


def test_no_amplitudes_falls_back_to_the_target() -> None:
    assert quietest([], HOP, 100.0, 100.05) == 100.05


# ─────────────────────────────────────────────────────────────
#  자를 시각 — 단조 증가하고 오디오 안에 있다
# ─────────────────────────────────────────────────────────────


def _fake_finder(mapping: dict[float, float]):
    """목표 → 실제로 고른 지점. ffmpeg 없이 탐색 결과를 흉내낸다."""
    return lambda target: mapping.get(target, target)


def test_cuts_are_the_quiet_points_that_were_found() -> None:
    cfg = Config()
    size = cfg.chunk_max_bytes * 2
    cuts = plan_cuts(600.0, size, cfg, _fake_finder({300.0: 297.4}))
    assert cuts == pytest.approx([297.4])


def test_cuts_stay_strictly_increasing() -> None:
    """탐색기가 뒤엉킨 값을 줘도 시각이 뒤집히지 않는다."""
    cfg = data_replace(Config(), chunk_max_bytes=1000)
    cuts = plan_cuts(600.0, 4000, cfg, _fake_finder({150.0: 400.0, 300.0: 200.0}))
    assert cuts == sorted(cuts)
    assert len(set(cuts)) == len(cuts)


def test_cuts_stay_inside_the_audio() -> None:
    cfg = data_replace(Config(), chunk_max_bytes=1000)
    cuts = plan_cuts(600.0, 3000, cfg, _fake_finder({200.0: -50.0, 400.0: 9999.0}))
    assert all(0.0 < cut < 600.0 for cut in cuts)


def test_a_single_chunk_needs_no_cut() -> None:
    assert plan_cuts(600.0, 5 * MB, Config(), _fake_finder({})) == []


# ─────────────────────────────────────────────────────────────
#  커버리지 — 이번 사고의 회귀 테스트
# ─────────────────────────────────────────────────────────────


def test_chunks_cover_the_whole_audio_with_no_gap() -> None:
    """규칙 4 — 조각은 [0, duration]을 빈틈없이 덮는다. 사라지는 구간이 없다."""
    spans = chunks_from_cuts([297.4, 601.1], 900.0)
    assert spans[0][0] == 0.0
    assert spans[-1][1] == 900.0
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier[1] == later[0]


def test_a_silent_opening_still_starts_at_zero() -> None:
    """**이번 사고의 직접 회귀.** 초반이 전부 무음이어도 첫 조각은 0초에서 시작한다.

    D24는 여기서 초반 3~6분을 버렸다 — 무음이면 발화 구간에 넣지 않았기 때문이다.
    """
    cfg = data_replace(Config(), chunk_max_bytes=1000)
    quiet_opening = _fake_finder({200.0: 12.0})  # 탐색기가 초반 무음 한복판을 골랐다
    cuts = plan_cuts(600.0, 3000, cfg, quiet_opening)
    spans = chunks_from_cuts(cuts, 600.0)
    assert spans[0][0] == 0.0
    assert spans[-1][1] == 600.0


def test_a_silent_ending_still_reaches_the_last_second() -> None:
    cfg = data_replace(Config(), chunk_max_bytes=1000)
    cuts = plan_cuts(600.0, 2000, cfg, _fake_finder({300.0: 588.0}))
    spans = chunks_from_cuts(cuts, 600.0)
    assert spans[-1][1] == 600.0


def test_no_cut_means_one_chunk_over_everything() -> None:
    assert chunks_from_cuts([], 600.0) == [(0.0, 600.0)]


def test_coverage_holds_for_any_number_of_chunks() -> None:
    cfg = Config()
    for pieces in (1, 2, 3, 5, 8):
        size = cfg.chunk_max_bytes * pieces
        cuts = plan_cuts(4620.0, size, cfg, _fake_finder({}))
        spans = chunks_from_cuts(cuts, 4620.0)
        assert len(spans) == pieces
        assert spans[0][0] == 0.0
        assert spans[-1][1] == pytest.approx(4620.0)
        covered = sum(end - start for start, end in spans)
        assert covered == pytest.approx(4620.0)
