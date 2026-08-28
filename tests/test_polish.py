"""조판 정리 — 영화 자막 관행으로 문장부호를 다듬는다(D30).

이 계층의 안전선은 하나다: **글자는 한 자도 지우지 않는다.**
`strip_for_compare`가 전후로 같아야 한다.
"""

from __future__ import annotations

from dataclasses import replace

from vlogsrt.config import Config
from vlogsrt.hangul import strip_for_compare
from vlogsrt.models import Cue
from vlogsrt.polish import POLISHED, polish_pass, polish_text

from .conftest import make_cue

# ─────────────────────────────────────────────────────────────
#  온점
# ─────────────────────────────────────────────────────────────


def test_a_sentence_period_is_deleted() -> None:
    assert polish_text("망원동에 왔습니다.", Config()) == "망원동에 왔습니다"


def test_a_decimal_point_survives() -> None:
    """숫자 사이의 온점은 부호가 아니라 값이다. 지우면 1.5가 15가 된다."""
    cfg = Config()
    assert polish_text("여기서 1.5초 정도 걸려요.", cfg) == "여기서 1.5초 정도 걸려요"
    assert polish_text("12.30에 만나기로 했다.", cfg) == "12.30에 만나기로 했다"


def test_a_period_after_a_number_still_goes() -> None:
    """숫자 **사이**만 예외다. 문장 끝의 온점은 앞이 숫자여도 부호다."""
    assert polish_text("도착한 시각이 12.30.", Config()) == "도착한 시각이 12.30"


# ─────────────────────────────────────────────────────────────
#  말줄임표 · 중복 부호
# ─────────────────────────────────────────────────────────────


def test_dots_become_one_ellipsis_before_periods_are_dropped() -> None:
    """순서 회귀 — 온점 삭제를 먼저 돌리면 말줄임표가 통째로 증발한다."""
    cfg = Config()
    assert polish_text("그래서...", cfg) == "그래서…"
    assert polish_text("음.. 글쎄요..", cfg) == "음… 글쎄요…"


def test_repeated_marks_collapse_to_one() -> None:
    cfg = Config()
    assert polish_text("진짜!!!", cfg) == "진짜!"
    assert polish_text("뭐야??", cfg) == "뭐야?"
    assert polish_text("어…… 그러니까", cfg) == "어… 그러니까"


def test_different_marks_side_by_side_stay() -> None:
    """`?!`는 겹쳐 쓴 것이 아니라 뜻이 다른 두 부호다."""
    assert polish_text("이게 뭐야?!", Config()) == "이게 뭐야?!"


# ─────────────────────────────────────────────────────────────
#  공백 · 쉼표
# ─────────────────────────────────────────────────────────────


def test_space_moves_from_before_the_mark_to_after() -> None:
    assert polish_text("진짜 ?이제 갈까", Config()) == "진짜? 이제 갈까"


def test_a_trailing_comma_goes_but_the_others_stay() -> None:
    cfg = Config()
    assert polish_text("그래서 뭐냐면,", cfg) == "그래서 뭐냐면"
    assert polish_text("어, 그래서 뭐냐면 말이지", cfg) == "어, 그래서 뭐냐면 말이지"


def test_whitespace_is_normalized() -> None:
    assert polish_text("  어   그래서  ", Config()) == "어 그래서"


# ─────────────────────────────────────────────────────────────
#  규칙 9 — 큐를 비우지 않는다
# ─────────────────────────────────────────────────────────────


def test_a_cue_that_is_only_a_period_survives_untouched() -> None:
    """부호밖에 없던 큐를 비우면 자막 한 장이 조용히 사라진다(규칙 9)."""
    cfg = Config()
    polished = polish_pass([make_cue(0.0, 1.0, ".")], cfg)
    assert polished[0].text == "."
    assert POLISHED not in polished[0].flags


def test_an_ellipsis_only_cue_stays_visible() -> None:
    polished = polish_pass([make_cue(0.0, 1.0, "...")], Config())
    assert polished[0].text == "…"


# ─────────────────────────────────────────────────────────────
#  큐 — 시각과 글자
# ─────────────────────────────────────────────────────────────


def test_polish_touches_text_and_nothing_else() -> None:
    cfg = Config()
    cue = make_cue(3.0, 5.5, "망원동에 왔습니다.", seg_ids=(7, 8), index=4)
    polished = polish_pass([cue], cfg)[0]
    assert polished.text == "망원동에 왔습니다"
    assert (polished.start, polished.end) == (cue.start, cue.end)
    assert polished.seg_ids == cue.seg_ids
    assert polished.origin == cue.origin
    assert polished.index == cue.index
    assert POLISHED in polished.flags


def test_an_untouched_cue_keeps_its_flags_clean() -> None:
    cfg = Config()
    cue = make_cue(0.0, 2.0, "부호가 없는 큐")
    assert polish_pass([cue], cfg) == [cue]


def test_polish_never_deletes_a_letter() -> None:
    """이 계층의 유일한 안전선 — 지울 수 있는 것은 문장부호뿐이다."""
    cfg = Config()
    samples = [
        "망원동에 왔습니다. 낮술 하러 왔어요!!",
        "어... 그러니까, 1.5초 정도?",
        "Oh my god. 진짜 대박!!!",
    ]
    for text in samples:
        assert strip_for_compare(polish_text(text, cfg)) == strip_for_compare(text)


# ─────────────────────────────────────────────────────────────
#  토글
# ─────────────────────────────────────────────────────────────


def test_the_toggle_leaves_every_cue_object_alone() -> None:
    cfg = replace(Config(), polish_enabled=False)
    cues: list[Cue] = [make_cue(0.0, 1.0, "왔습니다."), make_cue(2.0, 3.0, "진짜!!!", index=2)]
    polished = polish_pass(cues, cfg)
    assert [id(cue) for cue in polished] == [id(cue) for cue in cues]


def test_marks_to_drop_come_from_config() -> None:
    """규칙 8 — 부호 집합도 코드가 아니라 config에서 온다."""
    cfg = replace(Config(), polish_drop_marks=())
    assert polish_text("망원동에 왔습니다.", cfg) == "망원동에 왔습니다."
