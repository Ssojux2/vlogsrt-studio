"""D59 19f — 말소리 빈칸 고르기(순수): 「말소리 − 큐 ±τ」를 다시 물을 구간으로 넓힌다.

시험이 못 박는 것:

- 재료는 자막 시각 확인(D55)의 Rτ와 **같은 계산**이다 — 고른 말소리의 합은 언제나
  `(1 − Rτ) × 말소리`다.
- 넓힌 구간은 통째로 다시 묻는다 — 조각은 빈틈·겹침 없이 구간을 덮고 모델 한도를 넘지
  않는다(규칙 4).
- 잇기·넓히기·`[0, PCM 길이]` 자르기, 큐가 다 덮으면 아무것도 고르지 않는다, 결정적이다.
- 기존 큐는 고르는 재료일 뿐 하나도 바뀌지 않는다 — 함수는 값을 받고 값을 돌려준다.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from vlogsrt.project import sync_check
from vlogsrt.project.config import (
    GAP_REFILL_SETTINGS,
    SYNC_CHECK_SETTINGS,
    GapRefillSettings,
    gap_refill_problems,
)
from vlogsrt.project.raw import speech_gaps

RATE = 16000
HOP = Fraction(512, RATE)  # VAD 창 하나 = 32ms(ProjectConfig 기본)
TAU = sync_check._frames(SYNC_CHECK_SETTINGS.speech_reach_seconds, HOP)  # 16칸 = 0.512초


def track(runs: list[tuple[int, int]], frames: int) -> sync_check.SpeechTrack:
    return sync_check.SpeechTrack(HOP, frames, frames * HOP, tuple(runs))


def cue(start: str, end: str, cue_id: str = "c") -> sync_check.Cue:
    return sync_check.Cue(cue_id, Fraction(start), Fraction(end), "말")


def select(
    cues: list[sync_check.Cue],
    speech: sync_check.SpeechTrack,
    *,
    settings: GapRefillSettings = GAP_REFILL_SETTINGS,
    failed: tuple[tuple[int, int], ...] = (),
    sample_count: int | None = None,
) -> tuple[speech_gaps.GapSpan, ...]:
    count = speech.frame_count * 512 if sample_count is None else sample_count
    return speech_gaps.select_gaps(
        cues,
        speech,
        sample_count=count,
        sample_rate=RATE,
        failed=failed,
        sync_settings=SYNC_CHECK_SETTINGS,
        settings=settings,
    )


# ── 재료 — Rτ의 나머지 그대로 ─────────────────────────────────────────────────


def test_subtract_runs_is_set_difference_on_frames() -> None:
    left = [(0, 10), (20, 30), (40, 50)]
    right = [(5, 22), (25, 26), (45, 60)]
    assert sync_check.subtract_runs(left, right) == ((0, 5), (22, 25), (26, 30), (40, 45))
    assert sync_check.subtract_runs(left, []) == tuple(left)
    assert sync_check.subtract_runs([], right) == ()
    assert sync_check.subtract_runs(left, [(0, 100)]) == ()


def test_uncovered_speech_is_exactly_what_recall_does_not_count() -> None:
    speech = track([(10, 60), (100, 160), (300, 320), (400, 520)], 600)
    cues = [cue("0.5", "1.6", "a"), cue("13", "15", "b")]
    marks = sync_check.cue_runs(cues, HOP, speech.frame_count)
    _precision, recall = sync_check.coverage(marks, speech, SYNC_CHECK_SETTINGS)
    uncovered = sync_check.uncovered_speech(marks, speech, SYNC_CHECK_SETTINGS)
    assert recall is not None
    assert Fraction(sync_check.run_total(uncovered)) == (1 - recall) * sync_check.run_total(
        speech.runs
    )
    near = sync_check.dilate_runs(marks, TAU, speech.frame_count)
    assert sync_check.intersection_frames(uncovered, near) == 0  # 큐 τ 안은 하나도 고르지 않는다


def test_coverage_is_unchanged_by_the_shared_reach_helper() -> None:
    """빈칸 고르기와 같은 τ를 쓰도록 나눈 뒤에도 Pτ·Rτ는 예전 산술 그대로다(D55 보고서 바이트)."""
    speech = track([(10, 60), (100, 160), (300, 320)], 400)
    marks = sync_check.cue_runs([cue("0.5", "1.6"), cue("9", "10")], HOP, speech.frame_count)
    near_speech = sync_check.dilate_runs(speech.runs, TAU, speech.frame_count)
    near_cues = sync_check.dilate_runs(marks, TAU, speech.frame_count)
    expected = (
        Fraction(sync_check.intersection_frames(marks, near_speech), sync_check.run_total(marks)),
        Fraction(
            sync_check.intersection_frames(speech.runs, near_cues),
            sync_check.run_total(speech.runs),
        ),
    )
    assert sync_check.coverage(marks, speech, SYNC_CHECK_SETTINGS) == expected


# ── 고르기 — 잇기·넓히기·자르기 ─────────────────────────────────────────────────


def test_nothing_is_selected_when_every_speech_is_covered() -> None:
    speech = track([(100, 150), (300, 340)], 500)
    cues = [cue("3.2", "4.8", "a"), cue("9.6", "10.9", "b")]
    assert select(cues, speech) == ()
    assert select([], track([], 500)) == ()


def test_far_apart_gaps_stay_separate_and_each_is_padded_by_half_a_second() -> None:
    speech = track([(100, 125), (400, 425)], 600)  # 3.2–4.0초 · 12.8–13.6초, 자막 없음
    spans = select([], speech)
    pad = 8000  # 0.5초
    assert [(s.start, s.end) for s in spans] == [
        (100 * 512 - pad, 125 * 512 + pad),
        (400 * 512 - pad, 425 * 512 + pad),
    ]
    assert [s.speech for s in spans] == [25 * 512, 25 * 512]
    assert all(s.failed_speech == 0 for s in spans)


def test_gaps_closer_than_the_merge_distance_become_one_span() -> None:
    near = track([(100, 125), (150, 175)], 600)  # 틈 25칸 = 0.8초 < 2초
    assert len(select([], near)) == 1
    far = track([(100, 125), (200, 225)], 600)  # 틈 75칸 = 2.4초 > 2초 — 넓힘(1초)을 더해도 따로다
    assert len(select([], far)) == 2
    merged = select([], near)[0]
    assert (merged.start, merged.end) == (100 * 512 - 8000, 175 * 512 + 8000)
    assert merged.speech == 50 * 512  # 사이의 조용한 틈도 묻지만 말소리 합은 말소리만 센다
    assert merged.speech_runs == ((100 * 512, 125 * 512), (150 * 512, 175 * 512))


def test_merging_never_bridges_an_existing_cue() -> None:
    """말소리 · 짧은 큐 · 말소리 — 틈이 2초보다 짧아도 큐를 품은 틈은 잇지 않는다(검증 지적 2).

    이으면 whisper가 큐의 말과 새 말을 한 세그먼트로 듣고, 그 줄은 기존 큐와 겹쳐 넣지 못한 채
    조각이 「물었음」으로 남는다 — 다시 묻지도 못한다.
    """
    speech = track([(100, 185)], 600)
    cue_frames = (135, 150)  # 4.32–4.8초의 0.48초 큐
    cues = [cue(str(Fraction(135 * 512, RATE)), str(Fraction(150 * 512, RATE)), "short")]
    spans = select(cues, speech)
    assert len(spans) == 2  # 틈(119–166칸 = 1.5초)은 잇는 거리 안이지만 큐가 있다
    wall = (cue_frames[0] * 512, cue_frames[1] * 512)
    assert all(s.end <= wall[0] or s.start >= wall[1] for s in spans)
    without_cue = select([], speech)
    assert len(without_cue) == 1  # 큐가 없으면 그대로 하나다


def test_spans_are_clipped_to_the_pcm_and_padding_never_shrinks() -> None:
    speech = track([(0, 10), (590, 600)], 600)  # 머리와 꼬리
    count = 600 * 512 - 100  # 마지막 칸은 PCM 끝을 넘는다(칸 수는 올림)
    spans = select([], speech, sample_count=count)
    assert spans[0].start == 0 and spans[-1].end == count
    assert all(0 <= s.start < s.end <= count for s in spans)
    wide = select([], speech, settings=GapRefillSettings(Fraction(2), Fraction(3)))
    narrow = select([], speech, settings=GapRefillSettings(Fraction(2), Fraction(0)))
    for w, n in zip(wide, narrow, strict=True):
        assert w.start <= n.start and n.end <= w.end


def test_padding_at_tau_stays_out_of_the_existing_cue_frames() -> None:
    """넓힘(0.5초)이 τ(16칸 = 0.512초) 이하라 넓힌 구간이 기존 큐의 칸으로 들어가지 않는다."""
    speech = track([(90, 200)], 300)
    cues = [cue("2.88", "4.0", "kept")]  # 90–125칸을 덮는다
    spans = select(cues, speech, settings=GapRefillSettings(Fraction(0), Fraction(1, 2)))
    marks = sync_check.cue_runs(cues, HOP, speech.frame_count)
    cue_samples = [(a * 512, b * 512) for a, b in marks]
    assert spans and all(s.start >= cue_samples[0][1] for s in spans)


def test_failed_chunk_speech_is_split_out_for_the_screen_only() -> None:
    speech = track([(100, 125), (400, 425)], 600)
    failed = ((0, 300 * 512),)  # 첫 덩어리만 실패 청크 안
    spans = select([], speech, failed=failed)
    assert [s.failed_speech for s in spans] == [25 * 512, 0]
    assert [(s.start, s.end) for s in spans] == [(s.start, s.end) for s in select([], speech)]


def test_selection_is_deterministic_and_ignores_cue_order() -> None:
    speech = track([(10, 60), (100, 160), (300, 320), (400, 520)], 600)
    cues = [cue("0.5", "1.6", "a"), cue("13", "15", "b")]
    first = select(cues, speech)
    assert first == select(list(reversed(cues)), speech) == select(cues, speech)


def test_existing_cues_are_never_touched() -> None:
    cues = [cue("0.5", "1.6", "a"), cue("13", "15", "b")]
    before = list(cues)
    select(cues, track([(10, 60), (400, 520)], 600))
    assert cues == before


# ── 다시 묻기 — 넓힌 구간을 통째로, 빈틈 없이 ────────────────────────────────────


def test_request_pieces_cover_every_span_with_no_holes_or_overlaps() -> None:
    spans = (
        speech_gaps.GapSpan(1000, 1000 + 7 * 100 + 30, 10, 0),
        speech_gaps.GapSpan(5000, 5050, 10, 0),
    )
    pieces = speech_gaps.request_pieces(spans, 100)
    for span in spans:
        own = [p for p in pieces if span.start <= p[0] < span.end]
        assert own[0][0] == span.start and own[-1][1] == span.end
        assert all(a[1] == b[0] for a, b in zip(own, own[1:], strict=False))  # 빈틈 없음
        assert all(0 < end - start <= 100 for start, end in own)
    assert sum(b - a for a, b in pieces) == sum(s.end - s.start for s in spans)
    assert len(pieces) == 9  # 730 표본 = 100×7 + 30, 그리고 50 표본 하나
    with pytest.raises(ValueError):
        speech_gaps.request_pieces(spans, 0)


def test_a_long_failed_chunk_is_asked_whole_in_bounded_pieces() -> None:
    frames = 6000  # 192초
    speech = track([(0, frames)], frames)
    spans = select([], speech)
    assert [(s.start, s.end) for s in spans] == [(0, frames * 512)]
    bound = 180 * RATE
    pieces = speech_gaps.request_pieces(spans, bound)
    assert pieces == ((0, bound), (bound, frames * 512))


# ── 설정 — 지문 밖, 값역 ──────────────────────────────────────────────────────


def test_settings_are_frozen_and_validated() -> None:
    assert gap_refill_problems(GAP_REFILL_SETTINGS) == []
    assert gap_refill_problems(GapRefillSettings(Fraction(-1), Fraction(1, 2)))
    assert gap_refill_problems(GapRefillSettings(Fraction(1), 0.5))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="pad_seconds"):
        select([], track([(1, 2)], 10), settings=GapRefillSettings(Fraction(1), Fraction(-1)))
    with pytest.raises(AttributeError):
        GAP_REFILL_SETTINGS.pad_seconds = Fraction(9)  # type: ignore[misc]


def test_settings_live_outside_every_fingerprinted_config() -> None:
    from dataclasses import fields

    from vlogsrt.project.config import ProjectConfig
    from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
    from vlogsrt.project.raw.config import RawWorkflowConfig, SourceTextSettings

    names = {f.name for f in fields(GapRefillSettings)}
    for owner in (ProjectConfig, RawWorkflowConfig, RawUnderstandingSettings, SourceTextSettings):
        assert not names & {f.name for f in fields(owner)}, owner
