"""whisper가 놓친 구간으로 텍스트를 되돌린다. **한 글자도 버리지 않는다.**

실측(D17): whisper가 30~177초를 '오 오 오 …'로만 채우자 그 147초가 통째로 비었고,
그 구간 말이 1.4초짜리 큐 하나에 729자로 얹혔다.
"""

from __future__ import annotations

from vlogsrt.config import Config
from vlogsrt.gapfill import TIME_ESTIMATED, capacity, fill_gaps, report
from vlogsrt.mapping import apply_canonical, spine_to_cues
from vlogsrt.models import ORIGIN_ORPHAN

from .conftest import make_cue, make_segment


def joined(cues: list) -> str:
    return " ".join(cue.text for cue in cues)


# ─────────────────────────────────────────────────────────────
#  T3.6 — 고아 텍스트 (예전 rescue_orphans의 계약)
# ─────────────────────────────────────────────────────────────


def test_orphan() -> None:
    """걸러낸 세그먼트 자리에 정본이 남으면 그 시각으로 되돌아가야 한다."""
    cfg = Config()
    spine = [
        make_segment(0, 0.0, 2.0, "안녕하세요 오늘은"),
        make_segment(1, 2.2, 4.0, "감사합니다 감사합니다", verdict="compression_fail"),
        make_segment(2, 4.2, 6.0, "낮술 하러요"),
    ]
    canonical = "안녕하세요 오늘은 여기 분위기 좋네요 낮술 하러요"
    mapped, _, _ = apply_canonical(spine_to_cues(spine), spine, canonical, [], cfg)
    spread, _ = fill_gaps(mapped, spine, cfg)

    orphans = [cue for cue in spread if cue.origin == ORIGIN_ORPHAN]
    assert len(orphans) == 1
    assert orphans[0].conf <= cfg.orphan_conf_max
    assert TIME_ESTIMATED in orphans[0].flags
    assert orphans[0].start >= 2.2
    assert "분위기" in orphans[0].text
    assert joined(spread).count("분위기") == 1


def test_orphan_none_when_every_segment_is_ok() -> None:
    spine = [make_segment(0, 0.0, 2.0, "안녕하세요"), make_segment(1, 2.2, 4.0, "낮술 하러요")]
    cues = spine_to_cues(spine)
    spread, filled = fill_gaps(cues, spine, Config())
    assert spread == list(cues)
    assert filled == []


def test_silence_is_never_used_as_an_anchor() -> None:
    """`silent`은 "그 시간에 발화가 없었다"는 판정이다. 거기로 글자를 옮기면 안 된다."""
    cfg = Config()
    spine = [
        make_segment(0, 0.0, 2.0, "안녕하세요"),
        make_segment(1, 2.2, 4.0, "어어어어", verdict="silent"),
        make_segment(2, 4.2, 6.0, "낮술 하러요"),
    ]
    canonical = "안녕하세요 낮술 하러요"
    mapped, _, _ = apply_canonical(spine_to_cues(spine), spine, canonical, [], cfg)
    spread, _ = fill_gaps(mapped, spine, cfg)
    assert all(cue.origin != ORIGIN_ORPHAN for cue in spread)


def test_orphan_at_the_edges() -> None:
    """맨 앞과 맨 뒤가 걸러져도 그 텍스트는 제 시간대로 간다."""
    cfg = Config()
    spine = [
        make_segment(0, 0.0, 2.0, "감사합니다 감사합니다", verdict="compression_fail"),
        make_segment(1, 2.2, 4.0, "낮술 하러요"),
        make_segment(2, 4.2, 6.0, "고맙습니다 고맙습니다", verdict="compression_fail"),
    ]
    canonical = "처음 인사말입니다 낮술 하러요 마지막 인사말입니다"
    mapped, _, _ = apply_canonical(spine_to_cues(spine), spine, canonical, [], cfg)
    spread, _ = fill_gaps(mapped, spine, cfg)

    orphans = [cue for cue in spread if cue.origin == ORIGIN_ORPHAN]
    assert len(orphans) == 2
    assert orphans[0].start < 2.2 and orphans[1].start >= 4.2
    assert spread == sorted(spread, key=lambda cue: cue.start)
    assert "처음" in orphans[0].text
    # 시간 비례로 나누므로 마지막 어절 묶음이 뒤 공백으로 간다
    assert canonical.endswith(orphans[1].text)
    assert joined(spread).split() == canonical.split()  # 순서도 내용도 그대로


def test_normal_cues_are_untouched() -> None:
    cues = [make_cue(0.0, 2.0, "오늘은 망원동에 왔어"), make_cue(2.0, 4.0, "빵집부터 가자")]
    spread, filled = fill_gaps(cues, [], Config())
    assert spread == cues
    assert filled == []


def test_a_dump_is_spread_over_the_gap_it_belongs_to() -> None:
    """앞 147초를 whisper가 놓쳤다. 그 말은 그 시간대로 돌아가야 한다."""
    cfg = Config()
    spine = [
        make_segment(i, 30.0 + i * 20, 48.0 + i * 20, "오 오 오", verdict="compression_fail")
        for i in range(7)
    ]
    dumped = " ".join(f"문장{i}입니다" for i in range(60))
    cues = [make_cue(10.0, 12.0, "시작 인사"), make_cue(178.0, 179.4, dumped + " 마지막 말")]

    spread, filled = fill_gaps(cues, spine, cfg)

    assert len(spread) > 2
    placed = [cue for cue in spread if cue.origin == ORIGIN_ORPHAN]
    assert placed, "공백 구간에 큐가 생겨야 한다"
    assert all(30.0 <= cue.start < 178.0 for cue in placed)
    assert all(TIME_ESTIMATED in cue.flags for cue in placed)
    assert filled and filled[0].pieces > 1


def test_not_one_character_is_lost() -> None:
    cfg = Config()
    spine = [
        make_segment(i, 30.0 + i * 20, 48.0 + i * 20, "오", verdict="compression_fail")
        for i in range(7)
    ]
    body = " ".join(f"낱말{i}" for i in range(200))
    cues = [make_cue(10.0, 12.0, "시작"), make_cue(178.0, 179.4, body)]

    spread, _ = fill_gaps(cues, spine, cfg)
    before = {"시작", *body.split()}
    after = set(joined(spread).split())
    assert before == after


def test_the_cue_keeps_its_own_tail_not_the_gap_text() -> None:
    """앞부분은 앞 공백에서 나온 말이다. 큐에 남는 것은 **뒤쪽**이어야 한다."""
    cfg = Config()
    spine = [make_segment(0, 20.0, 90.0, "오", verdict="compression_fail")]
    text = "앞 공백에서 나온 말 " * 30 + "이 큐가 실제로 덮는 말"
    cues = [make_cue(10.0, 12.0, "시작"), make_cue(95.0, 96.4, text)]

    spread, _ = fill_gaps(cues, spine, cfg)
    survivor = next(cue for cue in spread if cue.start == 95.0)
    # 70초 공백 옆의 1.4초 큐다. 비율대로면 제 몫은 몇 글자뿐이고,
    # 그 몇 글자는 반드시 **끝부분**이어야 한다.
    assert text.endswith(survivor.text)
    assert "앞 공백에서" not in survivor.text


def test_excluded_segments_are_the_timing_anchors() -> None:
    """환청 구간이라도 세그먼트 경계는 오디오를 따라간다. 균등 분할보다 낫다."""
    cfg = Config()
    spine = [
        make_segment(0, 30.0, 40.0, "오", verdict="compression_fail"),
        make_segment(1, 100.0, 110.0, "오", verdict="compression_fail"),
    ]
    cues = [make_cue(10.0, 12.0, "시작"), make_cue(178.0, 179.4, "가나다 " * 80)]
    spread, _ = fill_gaps(cues, spine, cfg)
    placed = [cue for cue in spread if cue.origin == ORIGIN_ORPHAN]
    assert {(cue.start, cue.end) for cue in placed} <= {(30.0, 40.0), (100.0, 110.0)}


def test_even_slices_when_there_is_no_anchor() -> None:
    """whisper가 세그먼트조차 안 준 구간. 그래도 자막은 나와야 한다."""
    cfg = Config()
    cues = [make_cue(10.0, 12.0, "시작"), make_cue(178.0, 179.4, "가나다 " * 80)]
    spread, filled = fill_gaps(cues, [], cfg)
    placed = [cue for cue in spread if cue.origin == ORIGIN_ORPHAN]
    assert len(placed) > 1
    assert all(12.0 <= cue.start < 178.0 for cue in placed)
    assert filled


def test_a_long_collapse_is_spread_not_crammed() -> None:
    """147초를 통째로 놓친 구간. 한 큐에 몰지 말고 그 시간대에 펼쳐야 한다."""
    cfg = Config()
    spine = [
        make_segment(i, 30.0 + i * 20, 48.0 + i * 20, "오", verdict="compression_fail")
        for i in range(7)
    ]
    cues = [make_cue(10.0, 12.0, "시작"), make_cue(178.0, 179.0, "가나다 " * 200)]

    spread, filled = fill_gaps(cues, spine, cfg)
    placed = [cue for cue in spread if cue.origin == ORIGIN_ORPHAN]
    assert len(placed) > 1
    assert max(cue.end for cue in placed) > 100.0  # 앞 몇 초에 갇혀 있지 않다
    assert filled and filled[0].pieces > 1


def test_nowhere_to_put_it_keeps_the_text() -> None:
    """되돌릴 빈 시간이 없으면 그대로 둔다. 버리는 것보다 빽빽한 편이 낫다."""
    cfg = Config()
    crowded = "가나다 " * 200
    # 0초부터 큐가 붙어 있어 앞에 빈 시간이 없다
    cues = [make_cue(0.0, 1.0, "앞"), make_cue(1.0, 2.0, crowded), make_cue(2.0, 3.0, "뒤")]
    spread, filled = fill_gaps(cues, [], cfg)
    assert filled == []
    assert crowded.strip() in joined(spread)


def test_free_time_before_the_run_is_used() -> None:
    """앞에 빈 10초가 있으면 거기로 편다. 붙어 있는 큐 묶음도 함께 나눈다."""
    cfg = Config()
    crowded = "가나다 " * 200
    cues = [make_cue(10.0, 11.0, "앞"), make_cue(11.0, 12.0, crowded), make_cue(12.0, 13.0, "뒤")]
    spread, filled = fill_gaps(cues, [], cfg)
    assert filled
    assert any(cue.start < 10.0 for cue in spread)
    assert {"앞", "뒤", *crowded.split()} == set(joined(spread).split())


def test_capacity_grows_with_duration() -> None:
    cfg = Config()
    assert capacity(0.0, 7.0, cfg) > capacity(0.0, 1.0, cfg)
    assert capacity(0.0, 0.1, cfg) >= cfg.max_chars_per_cue


def test_report_is_silent_when_nothing_moved() -> None:
    assert report([]) == []


def test_report_names_the_biggest_moves() -> None:
    spine = [
        make_segment(i, 30.0 + i * 20, 48.0 + i * 20, "오", verdict="compression_fail")
        for i in range(7)
    ]
    cues = [make_cue(10.0, 12.0, "시작"), make_cue(178.0, 179.4, "가나다 " * 200)]
    _, filled = fill_gaps(cues, spine, Config())
    lines = report(filled)
    assert "되돌렸다" in lines[0]
    assert "추정값" in lines[0]
