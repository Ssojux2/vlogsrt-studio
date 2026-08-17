"""발화 구간과 묶음 — 시각의 유일한 출처(규칙 1). 순수 함수라 ffmpeg가 필요 없다."""

from __future__ import annotations

from dataclasses import replace as data_replace

from vlogsrt.config import Config
from vlogsrt.spans import group_spans, parse_silence_output, speech_spans

SILENCE_LOG = """
[silencedetect @ 0x1] silence_start: 3.5
[silencedetect @ 0x1] silence_end: 5.25 | silence_duration: 1.75
[silencedetect @ 0x1] silence_start: 40.0
[silencedetect @ 0x1] silence_end: 41.2 | silence_duration: 1.2
"""


# ─────────────────────────────────────────────────────────────
#  silencedetect 출력 해석
# ─────────────────────────────────────────────────────────────


def test_silence_comes_from_stderr_format() -> None:
    assert parse_silence_output(SILENCE_LOG, 60.0) == [(3.5, 5.25), (40.0, 41.2)]


def test_unclosed_silence_runs_to_the_end() -> None:
    log = "[silencedetect @ 0x1] silence_start: 55.0\n"
    assert parse_silence_output(log, 60.0) == [(55.0, 60.0)]


def test_no_silence_lines() -> None:
    assert parse_silence_output("nothing here", 60.0) == []


# ─────────────────────────────────────────────────────────────
#  발화 구간 = 무음의 여집합
# ─────────────────────────────────────────────────────────────


def test_speech_is_what_is_left_between_silences() -> None:
    spans = speech_spans([(3.5, 5.25), (40.0, 41.2)], 60.0, Config())
    assert spans == [(0.0, 3.5), (5.25, 40.0), (41.2, 60.0)]


def test_silence_at_both_ends_is_trimmed() -> None:
    spans = speech_spans([(0.0, 2.0), (8.0, 10.0)], 10.0, Config())
    assert spans == [(2.0, 8.0)]


def test_no_silence_means_one_span_over_everything() -> None:
    """BGM이 깔려 무음이 안 잡히는 회차. 타이밍이 통짜로 퇴화한다(D24 위험표)."""
    assert speech_spans([], 30.0, Config()) == [(0.0, 30.0)]


def test_all_silence_means_no_span() -> None:
    assert speech_spans([(0.0, 30.0)], 30.0, Config()) == []


def test_blips_shorter_than_the_floor_are_not_speech() -> None:
    cfg = Config()
    silences = [(1.0, 2.0), (2.0 + cfg.min_speech_duration / 2, 5.0)]
    assert speech_spans(silences, 10.0, cfg) == [(0.0, 1.0), (5.0, 10.0)]


def test_spans_never_overlap_and_stay_inside_the_audio() -> None:
    spans = speech_spans([(3.5, 5.25), (40.0, 41.2)], 60.0, Config())
    assert spans[0][0] >= 0.0
    assert spans[-1][1] <= 60.0
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier[1] <= later[0]


def test_overlapping_silences_are_absorbed() -> None:
    """ffmpeg가 겹친 구간을 줄 이유는 없지만, 주면 시각이 뒤집힌다."""
    assert speech_spans([(1.0, 5.0), (3.0, 7.0)], 10.0, Config()) == [(0.0, 1.0), (7.0, 10.0)]


# ─────────────────────────────────────────────────────────────
#  묶음 — 자르는 자리는 항상 무음
# ─────────────────────────────────────────────────────────────


def _spans(count: int, length: float, gap: float) -> list[tuple[float, float]]:
    return [(i * (length + gap), i * (length + gap) + length) for i in range(count)]


def test_a_short_talk_is_one_batch() -> None:
    spans = _spans(3, 2.0, 0.5)
    batches = group_spans(spans, 100_000, 60.0, Config())
    assert len(batches) == 1
    assert batches[0].spans == tuple(spans)


def test_no_batch_runs_longer_than_the_limit() -> None:
    cfg = Config()
    spans = _spans(60, 2.0, 0.5)
    batches = group_spans(spans, 100_000, 200.0, cfg)
    assert batches
    for batch in batches:
        assert batch.end - batch.start <= cfg.batch_max_seconds + 1e-9


def test_every_span_lands_in_exactly_one_batch() -> None:
    """구간 유실 0. 하나라도 새면 그 구간의 말이 자막에서 사라진다(규칙 9)."""
    spans = _spans(60, 2.0, 0.5)
    batches = group_spans(spans, 100_000, 200.0, Config())
    collected = [span for batch in batches for span in batch.spans]
    assert collected == spans


def test_a_batch_never_starts_or_ends_inside_speech() -> None:
    """말 중간에서 자르면 경계의 어절이 사라진다. 잘린 자리는 항상 무음이어야 한다."""
    spans = _spans(60, 2.0, 0.5)
    batches = group_spans(spans, 100_000, 200.0, Config())
    for batch in batches:
        assert batch.start <= batch.spans[0][0]
        assert batch.end >= batch.spans[-1][1]
    for earlier, later in zip(batches, batches[1:], strict=False):
        # 앞 묶음이 뒤 묶음의 첫 발화를 침범하지 않는다
        assert earlier.end <= later.spans[0][0]
        assert later.start >= earlier.spans[-1][1]


def test_batches_stay_inside_the_audio() -> None:
    spans = _spans(30, 2.0, 0.5)
    batches = group_spans(spans, 100_000, 120.0, Config())
    assert batches[0].start >= 0.0
    assert batches[-1].end <= 120.0
    assert batches[0].start <= spans[0][0]
    assert batches[-1].end >= spans[-1][1]


def test_long_silence_is_not_uploaded() -> None:
    """말이 74초에 끝나고 46초가 무음이면, 그 46초를 모델에 보낼 이유가 없다."""
    spans = _spans(30, 2.0, 0.5)
    batches = group_spans(spans, 100_000, 120.0, Config())
    assert batches[-1].end < spans[-1][1] + 1.0


def test_the_size_limit_can_force_more_batches_than_time_would() -> None:
    """업로드 한도(24MB)는 길이 한도와 독립이다."""
    cfg = Config()
    spans = _spans(4, 2.0, 0.5)
    huge = 4 * cfg.chunk_max_bytes
    batches = group_spans(spans, huge, 10.0, cfg)
    assert len(batches) >= 4


def test_a_span_longer_than_the_limit_still_gets_its_own_batch() -> None:
    """자를 무음이 없으면 한도를 넘겨서라도 통째로 보낸다. 버리지 않는다(규칙 9)."""
    cfg = Config()
    long_span = (0.0, cfg.batch_max_seconds * 3)
    batches = group_spans([long_span], 100_000, cfg.batch_max_seconds * 3, cfg)
    assert len(batches) == 1
    assert batches[0].spans == (long_span,)


def test_no_spans_means_no_batches() -> None:
    assert group_spans([], 100_000, 60.0, Config()) == []


def test_a_bigger_limit_makes_fewer_batches() -> None:
    spans = _spans(60, 2.0, 0.5)
    tight = group_spans(spans, 100_000, 200.0, data_replace(Config(), batch_max_seconds=20.0))
    loose = group_spans(spans, 100_000, 200.0, data_replace(Config(), batch_max_seconds=60.0))
    assert len(loose) < len(tight)
