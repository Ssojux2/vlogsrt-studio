"""W-114a(D187) — Hydrogen 글자 귀의 줄 글자 계획: 시각·줄은 Transcribe, 글자는 Hydrogen.

시험이 못 박는 것:

- 줄 아이디 계약(`translation.parse`와 같은 꼴) — 보낸 줄 아이디와 같은 아이디가 꼭
  한 번씩 와야 받는다.
- 모양 틀림·모르는 아이디·겹친 아이디·빠진 줄·빈 글자면 **청크 전체**가 Transcribe 초안
  글자 그대로 + 까닭이다. 맞는 줄만 골라 받지 않는다. 예외를 던지지 않는다(규칙 9).
- 어느 길에서도 줄의 아이디·차례·수는 보낸 그대로다 — 계획은 글자만 낸다(시각 칸이
  없다, 규칙 1).
- `speech=false`는 초안 글자를 두고 표시만 한다(지우지도 비우지도 않는다). 그 줄의
  `ko`는 읽지 않는다.
- `extra`(줄 밖 말)는 기록만 한다 — 줄 글자·수를 바꾸지 않고, 거부된 응답의 `extra`는
  남기지 않는다.
- 판 이름은 config에서 온다(규칙 8). 같은 입력이면 같은 결과다.
"""

from __future__ import annotations

import dataclasses

import pytest

from vlogsrt.project.raw import hydrogen_text
from vlogsrt.project.raw.config import HYDROGEN_TEXT_PLAN_VERSION

LINES = (
    hydrogen_text.DraftLine("u0", "안녕 하세여"),
    hydrogen_text.DraftLine("u1", "오늘은 한강에"),
    hydrogen_text.DraftLine("u2", "왔어요"),
)


def _item(line_id: object, ko: object, speech: object = True) -> dict[str, object]:
    return {"id": line_id, "ko": ko, "speech": speech}


def _reply(*items: object, extra: object = None) -> dict[str, object]:
    body: dict[str, object] = {"items": list(items)}
    if extra is not None:
        body["extra"] = extra
    return body


GOOD = _reply(
    _item("u0", "안녕하세요."),
    _item("u1", "오늘은  한강에\n왔는데"),
    _item("u2", "와, 좋다!"),
)


SHAPE = hydrogen_text.REASON_SHAPE


def test_matching_ids_give_hydrogen_text_and_keep_the_skeleton() -> None:
    plan = hydrogen_text.plan(LINES, GOOD)
    assert plan.version == HYDROGEN_TEXT_PLAN_VERSION
    assert plan.accepted and plan.reason is None and plan.detail is None
    assert [(row.line_id, row.text, row.source, row.no_speech) for row in plan.lines] == [
        ("u0", "안녕하세요.", hydrogen_text.SOURCE_HYDROGEN, False),
        ("u1", "오늘은 한강에 왔는데", hydrogen_text.SOURCE_HYDROGEN, False),
        ("u2", "와, 좋다!", hydrogen_text.SOURCE_HYDROGEN, False),
    ]
    assert plan.extra == ()


def test_reply_order_does_not_matter_lines_keep_the_sent_order() -> None:
    shuffled = _reply(_item("u2", "셋"), _item("u0", "하나"), _item("u1", "둘"))
    plan = hydrogen_text.plan(LINES, shuffled)
    assert plan.accepted
    assert [(row.line_id, row.text) for row in plan.lines] == [
        ("u0", "하나"),
        ("u1", "둘"),
        ("u2", "셋"),
    ]


def _assert_draft(plan: hydrogen_text.LineTextPlan, reason: str) -> None:
    assert not plan.accepted
    assert plan.reason == reason
    assert plan.detail
    assert [(row.line_id, row.text, row.source, row.no_speech) for row in plan.lines] == [
        (line.line_id, line.text, hydrogen_text.SOURCE_DRAFT, False) for line in LINES
    ]
    assert plan.extra == ()


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (None, hydrogen_text.REASON_SHAPE),
        ("items", hydrogen_text.REASON_SHAPE),
        ({}, hydrogen_text.REASON_SHAPE),
        ({"items": {"u0": "x"}}, hydrogen_text.REASON_SHAPE),
        (_reply("u0"), hydrogen_text.REASON_SHAPE),
        (_reply(_item(0, "하나"), _item("u1", "둘"), _item("u2", "셋")), SHAPE),
        (_reply(_item("u0", None), _item("u1", "둘"), _item("u2", "셋")), SHAPE),
        (
            _reply(_item("u0", "하나", "false"), _item("u1", "둘"), _item("u2", "셋")),
            hydrogen_text.REASON_SHAPE,
        ),
        (
            _reply({"id": "u0", "ko": "하나"}, _item("u1", "둘"), _item("u2", "셋")),
            hydrogen_text.REASON_SHAPE,
        ),
        (
            _reply(_item("u0", "하나"), _item("u1", "둘"), _item("u9", "셋")),
            hydrogen_text.REASON_UNKNOWN_ID,
        ),
        (
            _reply(_item("u0", "하나"), _item("u0", "또"), _item("u1", "둘"), _item("u2", "셋")),
            hydrogen_text.REASON_DUPLICATE_ID,
        ),
        (_reply(_item("u0", "하나"), _item("u2", "셋")), hydrogen_text.REASON_MISSING_ID),
        (_reply(), hydrogen_text.REASON_MISSING_ID),
        (
            _reply(_item("u0", "하나"), _item("u1", "  \n "), _item("u2", "셋")),
            hydrogen_text.REASON_EMPTY_TEXT,
        ),
    ],
)
def test_any_mismatch_keeps_the_whole_chunk_as_draft_with_a_reason(
    reply: object, reason: str
) -> None:
    _assert_draft(hydrogen_text.plan(LINES, reply), reason)


def test_bad_extra_rejects_the_chunk_too() -> None:
    for extra, reason in (
        ("말", hydrogen_text.REASON_SHAPE),
        (["말"], hydrogen_text.REASON_SHAPE),
        ([{"after_id": "u0"}], hydrogen_text.REASON_SHAPE),
        ([{"after_id": 3, "ko": "말"}], hydrogen_text.REASON_SHAPE),
        ([{"after_id": "u7", "ko": "말"}], hydrogen_text.REASON_UNKNOWN_ID),
    ):
        reply = {**GOOD, "extra": extra}
        _assert_draft(hydrogen_text.plan(LINES, reply), reason)


def test_no_speech_keeps_the_draft_text_and_only_marks_it() -> None:
    reply = _reply(
        _item("u0", "안녕하세요."),
        _item("u1", "", speech=False),
        _item("u2", "지어낸 말", speech=False),
    )
    plan = hydrogen_text.plan(LINES, reply)
    assert plan.accepted
    assert [(row.line_id, row.text, row.source, row.no_speech) for row in plan.lines] == [
        ("u0", "안녕하세요.", hydrogen_text.SOURCE_HYDROGEN, False),
        ("u1", "오늘은 한강에", hydrogen_text.SOURCE_DRAFT, True),
        ("u2", "왔어요", hydrogen_text.SOURCE_DRAFT, True),
    ]


def test_extra_speech_is_recorded_without_touching_lines() -> None:
    extra = [
        {"after_id": None, "ko": " 저기요 "},
        {"after_id": "u1", "ko": "어 잠깐만"},
        {"after_id": "u2", "ko": "   "},
    ]
    plan = hydrogen_text.plan(LINES, {**GOOD, "extra": extra})
    accepted = hydrogen_text.plan(LINES, GOOD)
    assert plan.accepted
    assert plan.lines == accepted.lines
    assert plan.extra == (
        hydrogen_text.ExtraSpeech(None, "저기요"),
        hydrogen_text.ExtraSpeech("u1", "어 잠깐만"),
    )


def test_missing_or_empty_extra_is_nothing_heard() -> None:
    assert hydrogen_text.plan(LINES, {**GOOD, "extra": []}).extra == ()
    assert hydrogen_text.plan(LINES, GOOD).extra == ()


def test_no_lines_take_an_empty_reply_and_reject_any_item() -> None:
    plan = hydrogen_text.plan((), _reply())
    assert plan.accepted and plan.lines == ()
    rejected = hydrogen_text.plan((), _reply(_item("u0", "하나")))
    assert rejected.reason == hydrogen_text.REASON_UNKNOWN_ID and rejected.lines == ()


def test_duplicate_sent_ids_are_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="u0"):
        hydrogen_text.plan((LINES[0], LINES[0]), GOOD)


def test_same_input_same_plan_and_no_class_carries_time() -> None:
    assert hydrogen_text.plan(LINES, GOOD) == hydrogen_text.plan(LINES, GOOD)
    time_fields = {"start", "end", "start_sample", "end_sample"}
    for cls in (
        hydrogen_text.DraftLine,
        hydrogen_text.PlannedLine,
        hydrogen_text.ExtraSpeech,
        hydrogen_text.LineTextPlan,
    ):
        assert not time_fields & {field.name for field in dataclasses.fields(cls)}
