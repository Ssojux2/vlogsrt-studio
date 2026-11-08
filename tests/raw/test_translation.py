"""W-53(D105) — 초안 영어 자막의 순수 계층: 묶음 · 요청 글 · 응답 해석 · 열쇠 · 줄 나눔.

시험이 못 박는 것:

- 줄은 `batch_lines`씩 묶이고, 둘째 묶음부터 직전 `context_lines`줄이 맥락으로만 붙는다.
- 응답은 그 묶음의 아이디와 **정확히** 같아야 한다 — 빠지거나 더하거나 겹치거나 빈 `en`이면 거부한다
  (조용히 빠지는 줄이 없다, 규칙 9).
- 열쇠는 판·모델·용어집·줄(아이디와 글자)을 묶는다 — 글자 하나가 바뀌면 다른 열쇠다.
- 줄 나눔은 42자에서 낱말 사이로만 자르고, 두 줄을 넘어도 글자를 버리지 않고 센다.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from vlogsrt.project.raw import translation as tr
from vlogsrt.project.raw.config import TRANSLATION


def _lines(count: int) -> tuple[tr.TranslationLine, ...]:
    return tuple(tr.TranslationLine(f"u{index:04d}", f"말 {index}") for index in range(count))


def test_batches_cover_every_line_once_and_carry_context() -> None:
    settings = replace(TRANSLATION, batch_lines=4, context_lines=2)
    batches = tr.build_batches(_lines(10), settings)
    assert [len(batch.lines) for batch in batches] == [4, 4, 2]
    flat = [line.utterance_id for batch in batches for line in batch.lines]
    assert flat == [line.utterance_id for line in _lines(10)]
    assert batches[0].context == ()
    assert [line.utterance_id for line in batches[1].context] == ["u0002", "u0003"]
    assert [line.utterance_id for line in batches[2].context] == ["u0006", "u0007"]


def test_no_lines_is_no_batches() -> None:
    assert tr.build_batches((), TRANSLATION) == ()


def test_the_request_text_names_lines_context_and_glossary() -> None:
    settings = replace(TRANSLATION, batch_lines=2, context_lines=1)
    batch = tr.build_batches(_lines(3), settings)[1]
    body = json.loads(tr.batch_text(batch, ("성산일출봉",)))
    assert body == {
        "glossary": ["성산일출봉"],
        "context": [{"id": "u0001", "ko": "말 1"}],
        "lines": [{"id": "u0002", "ko": "말 2"}],
    }


def _reply(batch: tr.TranslationBatch, **overrides: str) -> dict[str, object]:
    items = [{"id": line.utterance_id, "en": f"Line {line.utterance_id}"} for line in batch.lines]
    for key, value in overrides.items():
        items = [dict(item, en=value) if item["id"] == key else item for item in items]
    return {"items": items}


def test_parse_maps_every_id_and_strips_spaces() -> None:
    batch = tr.build_batches(_lines(3), TRANSLATION)[0]
    parsed = tr.parse(_reply(batch, u0001="  Wow,  look!  "), batch)
    assert parsed == {"u0000": "Line u0000", "u0001": "Wow, look!", "u0002": "Line u0002"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda items: items[:-1], "빠졌다"),
        (lambda items: [*items, {"id": "u9999", "en": "extra"}], "모르는"),
        (lambda items: [*items, dict(items[0])], "두 번"),
        (lambda items: [dict(items[0], en="   "), *items[1:]], "비었다"),
        (lambda items: [{"id": 7, "en": "x"}, *items[1:]], "모양"),
    ],
)
def test_parse_refuses_anything_but_the_exact_ids(mutate, message: str) -> None:
    batch = tr.build_batches(_lines(3), TRANSLATION)[0]
    items = _reply(batch)["items"]
    with pytest.raises(tr.TranslationError, match=message):
        tr.parse({"items": mutate(items)}, batch)


def test_parse_refuses_a_body_without_items() -> None:
    batch = tr.build_batches(_lines(1), TRANSLATION)[0]
    with pytest.raises(tr.TranslationError, match="items"):
        tr.parse({"lines": []}, batch)


def test_the_key_binds_revision_model_glossary_and_every_line() -> None:
    lines = _lines(3)
    key = tr.source_key("sha-a", lines, "g1", TRANSLATION)
    assert key == tr.source_key("sha-a", lines, "g1", TRANSLATION)
    changed = (lines[0], replace(lines[1], text="말 1!"), lines[2])
    assert tr.source_key("sha-a", changed, "g1", TRANSLATION) != key
    assert tr.source_key("sha-b", lines, "g1", TRANSLATION) != key
    assert tr.source_key("sha-a", lines, "g2", TRANSLATION) != key
    assert tr.source_key("sha-a", lines, "g1", replace(TRANSLATION, model="other")) != key
    assert tr.source_key("sha-a", lines, "g1", replace(TRANSLATION, batch_lines=2)) != key


def test_wrap_breaks_between_words_at_the_line_limit() -> None:
    text = "We finally made it to the top of the mountain and the view is amazing"
    wrapped, over = tr.wrap_english(text, 42)
    assert wrapped.split("\n") == [
        "We finally made it to the top of the",
        "mountain and the view is amazing",
    ]
    assert not over
    assert wrapped.replace("\n", " ") == text


def test_wrap_keeps_every_word_even_past_two_lines() -> None:
    text = " ".join(["supercalifragilistic"] * 6)
    wrapped, over = tr.wrap_english(text, 42)
    assert over and len(wrapped.split("\n")) == 3
    assert wrapped.replace("\n", " ") == text


def test_wrap_never_splits_a_long_word() -> None:
    word = "a" * 60
    assert tr.wrap_english(word, 42) == (word, False)
