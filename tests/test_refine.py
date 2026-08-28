"""정제 엔진 — 분할·병합·시간·줄바꿈."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vlogsrt.config import Config
from vlogsrt.layout import break_text
from vlogsrt.refine import (
    adjust_timing,
    drop_empty,
    merge_pass,
    refine,
    resolve_overlaps,
    split_cue,
    split_pass,
)

from .conftest import make_cue

LONG_TEXT = "안녕하세요 오늘은 망원동에 왔습니다 낮술 하러 왔어요"


# ─────────────────────────────────────────────────────────────
#  분할
# ─────────────────────────────────────────────────────────────


def test_split() -> None:
    cfg = Config()

    short = make_cue(0.0, 2.0, "짧은 자막")
    assert split_cue(short, cfg) == [short]

    long_cue = make_cue(0.0, 8.5, LONG_TEXT)
    pieces = split_cue(long_cue, cfg)
    assert len(pieces) == 2
    assert pieces[0].start == long_cue.start
    assert pieces[-1].end == long_cue.end
    assert all(piece.origin == "split" for piece in pieces)
    assert all(piece.seg_ids == long_cue.seg_ids for piece in pieces)


def test_split_cuts_at_a_word_boundary_in_proportion_to_the_text() -> None:
    """D21 — 단어 타임스탬프가 없다. 어절 경계를 고르고 시각은 글자 수 비례다."""
    cfg = Config()
    cue = make_cue(0.0, 8.5, LONG_TEXT)
    pieces = split_cue(cue, cfg)
    assert pieces[1].start - pieces[0].end == pytest.approx(cfg.min_gap)
    assert " ".join(piece.text for piece in pieces) == LONG_TEXT
    ratio = len(pieces[0].text) / len(LONG_TEXT)
    assert pieces[0].end == pytest.approx(cue.duration * ratio - cfg.min_gap / 2, abs=0.01)


def test_split_gives_up_on_a_single_long_word() -> None:
    cfg = Config()
    cue = make_cue(0.0, 8.0, "가" * 40)
    pieces = split_cue(cue, cfg)
    assert len(pieces) == 1
    assert "no_split_point" in pieces[0].flags


def test_split_respects_recursion_limit() -> None:
    cfg = Config()
    cue = make_cue(0.0, 60.0, " ".join(["단어"] * 60))
    pieces = split_cue(cue, cfg)
    assert len(pieces) <= 2**cfg.max_split_depth


def test_split_keeps_pieces_inside_the_original_span() -> None:
    cfg = Config()
    cue = make_cue(10.0, 20.0, LONG_TEXT * 2)
    pieces = split_pass([cue], cfg)
    assert pieces[0].start >= cue.start
    assert pieces[-1].end <= cue.end
    for previous, following in zip(pieces, pieces[1:], strict=False):
        assert previous.end <= following.start


# ─────────────────────────────────────────────────────────────
#  T2.3 — 병합
# ─────────────────────────────────────────────────────────────


def test_merge() -> None:
    cfg = Config()
    first = make_cue(0.0, 0.6, "어", seg_ids=(0,))
    second = make_cue(0.9, 2.5, "일단 생맥주", seg_ids=(1,), index=2)
    merged = merge_pass([first, second], cfg)
    assert len(merged) == 1
    assert merged[0].text == "어 일단 생맥주"
    assert merged[0].origin == "merge"
    assert merged[0].seg_ids == (0, 1)
    assert merged[0].start == 0.0
    assert merged[0].end == 2.5


def test_merge_skipped_when_gap_is_wide() -> None:
    cfg = Config()
    first = make_cue(0.0, 0.6, "어")
    distant = make_cue(1.5, 3.0, "일단 생맥주", index=2)
    assert len(merge_pass([first, distant], cfg)) == 2


def test_merge_skipped_when_cps_would_break() -> None:
    cfg = Config()
    first = make_cue(0.0, 0.6, "어")
    dense = make_cue(0.7, 1.2, "가" * 20, index=2)
    assert len(merge_pass([first, dense], cfg)) == 2


def test_merge_chain_stops_on_recomputed_cps() -> None:
    cfg = Config()
    cues = [
        make_cue(0.0, 0.30, "어"),
        make_cue(0.35, 0.70, "음", index=2),
        make_cue(0.75, 1.00, "가" * 20, index=3),
    ]
    merged = merge_pass(cues, cfg)
    assert len(merged) == 2
    assert merged[0].text == "어 음"
    assert merged[1].text == "가" * 20


def test_merge_inserts_exactly_one_space() -> None:
    cfg = Config()
    merged = merge_pass([make_cue(0.0, 0.5, "어"), make_cue(0.6, 2.0, "그래", index=2)], cfg)
    assert "  " not in merged[0].text


# ─────────────────────────────────────────────────────────────
#  T2.4 — 시간 보정 / 충돌 해소
# ─────────────────────────────────────────────────────────────


def test_timing() -> None:
    cfg = Config()
    cues = [make_cue(0.05, 2.0, "첫 큐"), make_cue(7.0, 9.9, "마지막 큐", index=2)]
    adjusted = adjust_timing(cues, cfg, duration=10.0)
    assert adjusted[0].start == 0.0
    assert adjusted[-1].end == 10.0
    assert all(cue.duration >= cfg.min_duration for cue in adjusted)


def test_timing_clamps_long_cues() -> None:
    cfg = Config()
    adjusted = adjust_timing([make_cue(0.0, 30.0, "긴 큐")], cfg, duration=60.0)
    assert adjusted[0].duration == pytest.approx(cfg.max_duration)


def test_overlaps_are_split_at_the_midpoint() -> None:
    cfg = Config()
    cues = [make_cue(0.0, 3.0, "앞"), make_cue(2.0, 5.0, "뒤", index=2)]
    resolved = resolve_overlaps(cues, cfg)
    assert resolved[0].end < resolved[1].start
    assert resolved[1].start - resolved[0].end == pytest.approx(cfg.min_gap)
    assert resolved[0].end == pytest.approx(2.5 - cfg.min_gap / 2)


def test_tight_gap_pulls_the_earlier_cue_back() -> None:
    cfg = Config()
    cues = [make_cue(0.0, 3.0, "앞"), make_cue(3.02, 5.0, "뒤", index=2)]
    resolved = resolve_overlaps(cues, cfg)
    assert resolved[1].start - resolved[0].end >= cfg.min_gap - 1e-9


def test_timing_is_monotonic_after_repair() -> None:
    cfg = Config()
    cues = [
        make_cue(0.0, 3.0, "가"),
        make_cue(2.0, 4.0, "나", index=2),
        make_cue(3.5, 6.0, "다", index=3),
    ]
    resolved = resolve_overlaps(adjust_timing(cues, cfg, duration=10.0), cfg)
    for previous, following in zip(resolved, resolved[1:], strict=False):
        assert previous.end <= following.start
        assert previous.start < previous.end


# ─────────────────────────────────────────────────────────────
#  T2.5 — 줄바꿈
# ─────────────────────────────────────────────────────────────


def test_linebreak() -> None:
    cfg = Config()
    assert break_text("짧은 자막", cfg)[0] == ["짧은 자막"]

    lines, flags = break_text("오늘은 블루펭귄에서 낮술을 마셨다", cfg)
    assert len(lines) == 2
    assert all(len(line) <= cfg.max_chars_per_line for line in lines)
    assert len(lines[0]) >= len(lines[1])
    assert flags == []


def test_linebreak_forces_a_split_without_spaces() -> None:
    cfg = Config()
    lines, flags = break_text("가" * 30, cfg)
    assert len(lines) == 2
    assert "forced_break" in flags


def test_linebreak_never_starts_a_line_with_a_particle() -> None:
    cfg = Config()
    lines, _ = break_text("오늘은 블루펭귄 에서 낮술 마셨다", cfg)
    assert not lines[1].startswith("에서")


def test_linebreak_keeps_normal_words_that_look_like_particles() -> None:
    cfg = Config()
    lines, _ = break_text("어제 만난 사람은 고양이를 아주 좋아한다", cfg)
    assert len(lines) == 2
    assert "".join(lines).replace(" ", "") == "어제만난사람은고양이를아주좋아한다"


# ─────────────────────────────────────────────────────────────
#  전 과정 — 검사 단계가 없으니 정제가 스스로 보장해야 한다
# ─────────────────────────────────────────────────────────────


def test_full_refine_output_is_well_formed() -> None:
    cfg = Config()
    cues = refine([make_cue(0.0, 8.5, LONG_TEXT)], cfg, duration=10.0)
    assert [cue.index for cue in cues] == list(range(1, len(cues) + 1))
    for cue in cues:
        assert cue.end > cue.start
        assert cue.text.strip()
        assert len(cue.lines) <= cfg.max_lines
    for previous, following in zip(cues, cues[1:], strict=False):
        assert following.start - previous.end >= cfg.min_gap - 1e-9


def test_refine_repairs_overlapping_input() -> None:
    cfg = Config()
    cues = refine(
        [make_cue(0.0, 3.0, "앞 큐"), make_cue(2.0, 3.5, "뒤 큐", index=2)], cfg, duration=10.0
    )
    for previous, following in zip(cues, cues[1:], strict=False):
        assert following.start >= previous.end


def test_refine_leaves_no_period_behind() -> None:
    """D30 — 조판 정리는 `refine` 안에 있다. `run`도 `redo`도 이 함수 하나만 부른다."""
    cfg = Config()
    cues = refine([make_cue(0.0, 4.0, "망원동에 왔습니다.")], cfg, duration=10.0)
    assert [cue.text for cue in cues] == ["망원동에 왔습니다"]


def test_polish_runs_before_the_split_decides() -> None:
    """순서 회귀 — 부호를 지우면 글자 수가 줄어 분할 판정이 바뀐다.

    부호까지 세면 34자라 두 장으로 갈리지만, 온점 둘을 지우면 32자로 한 장에 들어간다.
    자른 뒤에 지우면 굳이 두 장일 필요가 없던 큐가 두 장으로 남는다.
    """
    cfg = Config()
    text = "오늘은 망원동에 왔습니다. 낮술 한잔 하러 온 김에 들렀어요."
    assert len(text) > cfg.max_chars_per_cue
    assert len(text) - text.count(".") == cfg.max_chars_per_cue
    assert len(refine([make_cue(0.0, 6.0, text)], cfg, duration=10.0)) == 1
    unpolished = replace(cfg, polish_enabled=False)
    # 정리를 끄면 부호 두 개 때문에 두 장으로 갈린다 — 그게 정리가 앞에 있어야 하는 이유다.
    assert len(refine([make_cue(0.0, 6.0, text)], unpolished, duration=10.0)) == 2


def test_an_empty_cue_is_dropped_but_never_in_silence() -> None:
    """규칙 9 — 멈추지는 않되, 어느 세그먼트에서 잃었는지는 로그에 남는다."""
    cues = [make_cue(0.0, 1.0, "말"), make_cue(2.0, 3.0, "  ", index=2)]
    assert [cue.text for cue in drop_empty(cues)] == ["말"]


def test_drop_empty_passes_normal_cues_through() -> None:
    cues = [make_cue(0.0, 1.0, "말"), make_cue(2.0, 3.0, "더", index=2)]
    assert drop_empty(cues) == cues


def test_a_zero_length_segment_still_becomes_a_visible_cue() -> None:
    """구간이 길이 0이어도 자막은 min_duration만큼 보인다."""
    cfg = Config()
    cues = refine([make_cue(4.0, 4.0, "길이 0")], cfg, duration=30.0)
    assert len(cues) == 1
    assert cues[0].text == "길이 0"
    assert cues[0].duration >= cfg.min_duration - 1e-9
