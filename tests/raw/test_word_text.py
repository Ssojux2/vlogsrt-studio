"""W-35a(D95) — Gemini 단어를 whisper 줄에 배정한다: 시각은 whisper, 글자는 Gemini(순수).

시험이 못 박는 것:

- 단어 자리 = (시작 + 끝) / 2. 그 자리를 `[줄 시작, 줄 끝)`에 담은 줄의 글자가 된다.
- 줄의 아이디·시작·끝·차례·수는 그대로다 — 계획은 글자만 낸다(시각을 만들지 않는다, 규칙 1·3).
- 겹친 whisper 줄은 가운데가 가장 가까운 줄, 같으면 앞 줄. 길이 0인 줄은 아무것도 담지 않는다.
- 줄 밖 단어·시각 없는 단어는 어느 줄에도 붙지 않고 기록된다(앞뒤 줄 아이디와 함께).
- 단어가 없는 줄은 whisper 글자 그대로다(지우지 않는다, 규칙 9).
- 글자는 해석기가 붙인 띄어쓰기·부호 그대로 이어 앞뒤 공백만 정리한다. 같은 입력이면 같은 결과.
"""

from __future__ import annotations

from fractions import Fraction

from vlogsrt.project.raw import word_text
from vlogsrt.project.raw.config import WORD_TEXT_VERSION
from vlogsrt.project.raw.gemini_transcribe_parse import ParsedWord

F = Fraction


def _word(text: str, start: Fraction | None, end: Fraction | None, display: str | None = None):
    return ParsedWord(
        text=text, display=display if display is not None else text + " ", start=start, end=end
    )


def _line(line_id: str, start: Fraction, end: Fraction, text: str) -> word_text.TimedLine:
    return word_text.TimedLine(line_id, start, end, text)


LINES = (
    _line("u0", F(0), F(2), " 안녕 하세요"),
    _line("u1", F(2), F(5), " 오늘은 한강에"),
    _line("u2", F(6), F(8), " 왔어요"),
)
WORDS = (
    _word("안녕하세요.", F(1, 10), F(9, 10)),
    _word("오늘은", F(21, 10), F(26, 10)),
    _word("한강에", F(27, 10), F(32, 10)),
    _word("왔어요!", F(61, 10), F(7), display="왔어요!"),
)


def test_words_take_the_line_their_middle_falls_in_and_lines_keep_their_times() -> None:
    plan = word_text.place(LINES, WORDS)
    assert plan.version == WORD_TEXT_VERSION
    assert [(row.line_id, row.text, row.source) for row in plan.lines] == [
        ("u0", "안녕하세요.", word_text.SOURCE_GEMINI),
        ("u1", "오늘은 한강에", word_text.SOURCE_GEMINI),
        ("u2", "왔어요!", word_text.SOURCE_GEMINI),
    ]
    assert [row.words for row in plan.lines] == [(0,), (1, 2), (3,)]
    assert plan.unplaced == () and plan.untimed == 0


def test_a_line_without_words_keeps_its_whisper_text() -> None:
    plan = word_text.place(LINES, WORDS[:3])
    last = plan.lines[2]
    assert (last.text, last.source, last.words) == (" 왔어요", word_text.SOURCE_WHISPER, ())


def test_the_middle_decides_and_the_end_is_open() -> None:
    lines = (_line("a", F(0), F(2), "a"), _line("b", F(2), F(4), "b"))
    # 1.5–2.5초 단어의 가운데는 2초 — 앞 줄 끝은 열려 있어 뒤 줄이 담는다
    straddling = _word("걸침", F(3, 2), F(5, 2))
    plan = word_text.place(lines, (straddling,))
    assert [row.words for row in plan.lines] == [(), (0,)]
    # 줄 바깥 끝(4초)에 가운데가 떨어지면 아무 줄도 담지 않는다
    tail = word_text.place(lines, (_word("끝", F(7, 2), F(9, 2)),))
    assert [row.words for row in tail.lines] == [(), ()]
    (unplaced,) = tail.unplaced
    assert (unplaced.index, unplaced.before, unplaced.after) == (0, "b", None)


def test_overlapping_lines_go_to_the_nearest_centre_and_ties_to_the_earlier() -> None:
    # 긴 줄 가운데 5초 · 짧은 줄 가운데 6초 — 자리 5.9초는 짧은 줄에 더 가깝다
    lines = (_line("long", F(0), F(10), "x"), _line("short", F(5), F(7), "y"))
    plan = word_text.place(lines, (_word("가운데", F(58, 10), F(60, 10)),))
    assert [row.words for row in plan.lines] == [(), (0,)]
    twins = (_line("first", F(0), F(4), "x"), _line("second", F(0), F(4), "y"))
    tie = word_text.place(twins, (_word("같음", F(1), F(3)),))
    assert [row.words for row in tie.lines] == [(0,), ()]


def test_zero_length_lines_hold_nothing() -> None:
    lines = (_line("zero", F(3), F(3), "치즈"), _line("next", F(3), F(5), "다음"))
    plan = word_text.place(lines, (_word("말", F(29, 10), F(31, 10)),))
    assert [row.words for row in plan.lines] == [(), (0,)]
    assert plan.lines[0].text == "치즈"


def test_words_between_lines_and_untimed_words_are_recorded_not_attached() -> None:
    words = (*WORDS, _word("놓친말", F(51, 10), F(59, 10)), _word("시각없음", None, None))
    plan = word_text.place(LINES, words)
    assert [row.words for row in plan.lines] == [(0,), (1, 2), (3,)]
    gap, untimed = plan.unplaced
    assert (gap.text, gap.start, gap.end, gap.before, gap.after) == (
        "놓친말",
        F(51, 10),
        F(59, 10),
        "u1",
        "u2",
    )
    assert (untimed.text, untimed.start, untimed.before, untimed.after) == (
        "시각없음",
        None,
        None,
        None,
    )
    assert plan.untimed == 1


def test_word_order_within_a_line_follows_the_response_and_spacing_is_kept() -> None:
    words = (
        _word("커피", F(21, 10), F(24, 10), display="커피"),
        _word("한", F(25, 10), F(26, 10), display=" 한"),
        _word("잔,", F(27, 10), F(3), display=" 잔, "),
    )
    plan = word_text.place(LINES, words)
    assert plan.lines[1].text == "커피 한 잔,"


def test_the_plan_is_deterministic_and_never_changes_line_count() -> None:
    first = word_text.place(LINES, WORDS)
    assert first == word_text.place(LINES, WORDS)
    assert len(first.lines) == len(LINES)
    assert word_text.place((), WORDS).lines == ()
    assert len(word_text.place((), WORDS).unplaced) == len(WORDS)
