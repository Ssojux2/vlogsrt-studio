"""경계 매핑. 프로젝트의 심장이라 테스트를 아끼지 않는다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vlogsrt.config import Config
from vlogsrt.mapping import (
    align,
    apply_canonical,
    build_alignment,
    build_w,
    cue_confidence,
    enforce_monotonic,
    map_position,
    rescue_orphans,
    similarity,
    snap_to_word_boundary,
    spine_to_cues,
)
from vlogsrt.models import Cue

from .conftest import make_cue, make_segment, make_word

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "synthetic" / "map_cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _spine() -> list:
    return [
        make_segment(0, 0.0, 2.0, "안녕하세요 오늘은"),
        make_segment(1, 2.2, 4.0, "망언동에 왔습니다"),
        make_segment(2, 4.2, 6.0, "낮술 하러요"),
    ]


# ─────────────────────────────────────────────────────────────
#  T3.2 — W 구성
# ─────────────────────────────────────────────────────────────


def test_build_w() -> None:
    w, cuts, owner = build_w(_spine())
    assert cuts[-1] == len(w)
    assert cuts == sorted(cuts)
    assert len(owner) == len(w)
    assert w.startswith("안녕하세요 오늘은 ")
    assert owner[0] == 0
    assert owner[-1] == 2


def test_build_w_excludes_bad_verdicts() -> None:
    spine = [*_spine(), make_segment(3, 6.2, 8.0, "감사합니다" * 4, verdict="compression_fail")]
    w, cuts, _ = build_w(spine)
    assert "감사합니다감사합니다" not in w
    assert len(cuts) == 3
    full_w, full_cuts, _ = build_w(spine, include_all=True)
    assert len(full_cuts) == 4
    assert "감사합니다" in full_w


def test_build_w_handles_empty_text() -> None:
    spine = [make_segment(0, 0.0, 1.0, ""), make_segment(1, 1.0, 2.0, "가나")]
    w, cuts, owner = build_w(spine)
    assert cuts[-1] == len(w)
    assert cuts == sorted(cuts)
    assert len(owner) == len(w)


def test_build_w_on_empty_spine() -> None:
    assert build_w([]) == ("", [], [])


# ─────────────────────────────────────────────────────────────
#  T3.3 — map_position
# ─────────────────────────────────────────────────────────────


def test_map_position() -> None:
    """T3.3 DoD가 이름으로 부르는 테스트. 합성 12케이스 + 경계값."""
    for case in CASES:
        alignment = build_alignment(case["w"], case["a"])
        got = map_position(case["pos_w"], alignment.opcodes, alignment.imap_w, alignment.imap_a)
        assert got == case["expected"], case["name"]
        assert 0 <= got <= len(case["a"]), case["name"]


@pytest.mark.parametrize("opcode", ["equal", "replace", "insert", "delete"])
def test_every_opcode_is_covered(opcode: str) -> None:
    assert any(case["opcode"] == opcode for case in CASES)


def test_align_is_not_fooled_by_frequent_jamo() -> None:
    """autojunk=True였다면 `ㅇ`, `ㅏ`가 잡음으로 버려져 정렬이 무너진다."""
    long_w = "아아아아아아아아아아아아아아아아아아아아 블루팽귄에서 낮술"
    long_a = "아아아아아아아아아아아아아아아아아아아아 블루펭귄에서 낮술"
    opcodes = align(long_w, long_a)
    equal_span = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag == "equal")
    assert equal_span > 50


def test_map_position_never_returns_time() -> None:
    alignment = build_alignment("가나다", "가나다")
    result = map_position(1, alignment.opcodes, alignment.imap_w, alignment.imap_a)
    assert isinstance(result, int)


# ─────────────────────────────────────────────────────────────
#  T3.4 — enforce_monotonic
# ─────────────────────────────────────────────────────────────


def test_monotonic() -> None:
    assert enforce_monotonic([0, 3, 7, 10], 10) == [0, 3, 7, 10]
    assert enforce_monotonic([0, 7, 3, 10], 10) == [0, 7, 7, 10]
    assert enforce_monotonic([5, 4, 3, 2], 10) == [5, 5, 5, 5]
    assert enforce_monotonic([-3, 99], 10) == [0, 10]


def test_monotonic_stays_in_range() -> None:
    for cuts in ([0, 5, 2], [12, 1], [-1, -2]):
        result = enforce_monotonic(cuts, 10)
        assert all(0 <= value <= 10 for value in result)
        assert result == sorted(result)


# ─────────────────────────────────────────────────────────────
#  T3.5 — 어절 스냅
# ─────────────────────────────────────────────────────────────


def test_snap() -> None:
    cfg = Config()
    text = "오늘은 블루펭귄에서 낮술을"
    words = [make_word("블루펭귄에서", 1.0, 2.0)]

    assert snap_to_word_boundary(3, text, words, 1.5, cfg) == (3, [])
    assert snap_to_word_boundary(5, text, words, 1.5, cfg)[0] == 4
    # 오른쪽 공백(10)이 왼쪽 어절 머리(4)보다 가깝다.
    assert snap_to_word_boundary(9, text, words, 1.5, cfg)[0] == 10


def test_snap_gives_up_when_both_sides_are_far() -> None:
    cfg = Config()
    text = "가" * 40
    position, flags = snap_to_word_boundary(20, text, [make_word("가", 0.0, 1.0)], 0.5, cfg)
    assert position == 20
    assert flags == ["snap_failed"]


def test_snap_without_words_prefers_left() -> None:
    cfg = Config()
    text = "가나 다라마바사"
    # 오른쪽 경계가 더 가깝지만 --fast 모드에서는 왼쪽을 택한다.
    assert snap_to_word_boundary(6, text, [], 0.0, cfg)[0] == 3


def test_snap_at_string_edges() -> None:
    cfg = Config()
    text = "가나다"
    assert snap_to_word_boundary(0, text, [], 0.0, cfg) == (0, [])
    assert snap_to_word_boundary(3, text, [], 0.0, cfg) == (3, [])
    assert snap_to_word_boundary(99, text, [], 0.0, cfg)[0] == 3


def test_snap_tie_uses_word_ownership() -> None:
    cfg = Config()
    text = "가나 다다 라마"
    early = make_word("다다", 1.0, 2.0)
    # 경계 시각이 어절의 뒤쪽이면 그 어절은 앞 세그먼트 것이다 → 오른쪽으로 붙인다.
    assert snap_to_word_boundary(4, text, [early], 1.9, cfg)[0] == 5
    assert snap_to_word_boundary(4, text, [early], 1.1, cfg)[0] == 3


# ─────────────────────────────────────────────────────────────
#  T3.6 — 고아 텍스트
# ─────────────────────────────────────────────────────────────


def test_orphan() -> None:
    cfg = Config()
    spine = [
        make_segment(0, 0.0, 2.0, "안녕하세요 오늘은"),
        make_segment(1, 2.2, 4.0, "감사합니다 감사합니다", verdict="compression_fail"),
        make_segment(2, 4.2, 6.0, "낮술 하러요"),
    ]
    canonical = "안녕하세요 오늘은 여기 분위기 좋네요 낮술 하러요"
    cues = spine_to_cues(spine)
    mapped, cuts_a, _ = apply_canonical(cues, spine, canonical, [], cfg)
    rescued = rescue_orphans(cuts_a, spine, mapped, [], canonical, cfg)

    orphans = [cue for cue in rescued if cue.origin == "orphan"]
    assert len(orphans) == 1
    assert orphans[0].conf <= 0.30
    assert "time_estimated" in orphans[0].flags
    assert orphans[0].start >= 2.2
    assert "분위기" in orphans[0].text
    joined = " ".join(cue.text for cue in rescued)
    assert joined.count("분위기") == 1


def test_orphan_none_when_every_segment_is_ok() -> None:
    cfg = Config()
    spine = _spine()
    cues = spine_to_cues(spine)
    assert rescue_orphans([], spine, cues, [], "안녕하세요", cfg) == list(cues)


def test_orphan_skipped_when_text_too_short() -> None:
    cfg = Config()
    spine = [
        make_segment(0, 0.0, 2.0, "안녕하세요"),
        make_segment(1, 2.2, 4.0, "어어어어", verdict="silent"),
        make_segment(2, 4.2, 6.0, "낮술 하러요"),
    ]
    canonical = "안녕하세요 낮술 하러요"
    cues = spine_to_cues(spine)
    mapped, cuts_a, _ = apply_canonical(cues, spine, canonical, [], cfg)
    rescued = rescue_orphans(cuts_a, spine, mapped, [], canonical, cfg)
    assert all(cue.origin != "orphan" for cue in rescued)


def test_orphan_at_the_edges() -> None:
    cfg = Config()
    spine = [
        make_segment(0, 0.0, 2.0, "감사합니다 감사합니다", verdict="compression_fail"),
        make_segment(1, 2.2, 4.0, "낮술 하러요"),
        make_segment(2, 4.2, 6.0, "고맙습니다 고맙습니다", verdict="compression_fail"),
    ]
    canonical = "처음 인사말입니다 낮술 하러요 마지막 인사말입니다"
    cues = spine_to_cues(spine)
    mapped, cuts_a, _ = apply_canonical(cues, spine, canonical, [], cfg)
    rescued = rescue_orphans(cuts_a, spine, mapped, [], canonical, cfg)
    orphans = [cue for cue in rescued if cue.origin == "orphan"]
    assert len(orphans) == 2
    assert rescued == sorted(rescued, key=lambda cue: cue.start)


# ─────────────────────────────────────────────────────────────
#  T3.7 — 신뢰도
# ─────────────────────────────────────────────────────────────


def test_confidence() -> None:
    cfg = Config()
    segment = make_segment(0, 0.0, 2.0, "블루펭귄에서 낮술")

    same = make_cue(0.0, 2.0, "블루펭귄에서 낮술")
    assert cue_confidence(same, segment, cfg) == pytest.approx(1.0)

    one_jamo = make_cue(0.0, 2.0, "블루펭귄에서 낮술", whisper_text="블루팽귄에서 낮술")
    assert cue_confidence(one_jamo, segment, cfg) >= 0.9

    different = make_cue(0.0, 2.0, "전혀 다른 문장이 들어왔다", whisper_text="가나다라마바사")
    assert cue_confidence(different, segment, cfg) <= 0.3

    orphan = make_cue(0.0, 2.0, "고아", origin="orphan", conf=0.3)
    assert cue_confidence(orphan, None, cfg) <= 0.30

    reversed_cue = Cue(start=2.0, end=1.0, lines=("가",))
    assert cue_confidence(reversed_cue, segment, cfg) == 0.0

    low = make_segment(0, 0.0, 2.0, "블루펭귄에서 낮술", verdict="low_logprob")
    assert cue_confidence(same, low, cfg) == pytest.approx(cfg.low_logprob_penalty)


def test_similarity_uses_jamo_distance() -> None:
    assert similarity("블루펭귄", "블루팽귄") > similarity("블루펭귄", "블루펭귁이다")
    assert similarity("", "") == 1.0
    assert similarity("가", "") == 0.0


# ─────────────────────────────────────────────────────────────
#  텍스트만 바뀐다 — 타이밍 불변
# ─────────────────────────────────────────────────────────────


def test_apply_canonical_never_touches_timing() -> None:
    cfg = Config()
    spine = _spine()
    cues = spine_to_cues(spine)
    canonical = "안녕하세요 오늘은 망원동에 왔습니다 낮술 하러요"
    mapped, _, _ = apply_canonical(cues, spine, canonical, [], cfg)
    assert [(c.start, c.end) for c in mapped] == [(c.start, c.end) for c in cues]
    assert "망원동" in " ".join(cue.text for cue in mapped)


def test_apply_canonical_keeps_every_character() -> None:
    cfg = Config()
    spine = _spine()
    canonical = "안녕하세요 오늘은 망원동에 왔습니다 낮술 하러요"
    mapped, _, _ = apply_canonical(spine_to_cues(spine), spine, canonical, [], cfg)
    joined = "".join(cue.text.replace(" ", "") for cue in mapped)
    assert joined == canonical.replace(" ", "")
