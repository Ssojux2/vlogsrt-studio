"""Gemini 3.5 Transcribe 응답 해석(W-31b, D92) — 엄격하고 순수하다.

문서 모양(https://ai.google.dev/gemini-api/docs/transcribe)의 합성 픽스처를 쓴다 — 사람의 녹화(H1)가
오면 그 응답으로 계약 시험을 더한다. 시험이 못 박는 것:

- `status == "completed"`이고 전체 글이 있어야 한다 — SDK 꼴의 문자열 `output_text`, 또는 REST
  그대로의 `steps[].content[]` 글 칸(`type: "text"`의 `text`, W-52b2 — 실제 첫 영상에서 REST 응답에
  `output_text`가 없어 모든 조각이 실패했다). 둘 다 없으면 실패다. 단어는
  `steps[].content[].annotations[]`의 `type: "word_info"`뿐이고, 시각은
  `^\\d+(\\.\\d{1,9})?s$` 꼴만 정확 유리수로 읽는다.
- 띄어쓰기·문장부호는 `output_text`에 맞춰 단어에 붙인다(앞 단어 뒤에). 단어가 덮지 못한 한글·영문·
  숫자는 자리 없는 글자로 세기만 한다(D37과 같은 생각) — 시각 없는 글자를 내보내지 않는다.
- 시각이 없거나 뒤집힌 단어는 버리지 않고 시각 없는 단어로 센다(규칙 9). 청크 끝을 2초 이하로 넘은
  끝은 청크 끝으로 닫고(D55), 그 이상은 청크 실패다. 글자는 있는데 시각 단어가 0개면 실패, 둘 다
  비면 빈 결과다(「말이 아니다」라고 하지 않는다 — 규칙 4).
"""

from __future__ import annotations

import json
import unicodedata
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.raw import gemini_transcribe_parse as gtp

F = Fraction
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "gemini"


def _fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return payload


def _payload(text: str, *words: dict[str, Any], status: str = "completed") -> dict[str, Any]:
    content = [{"type": "text", "text": text, "annotations": list(words)}]
    return {"status": status, "output_text": text, "steps": [{"content": content}]}


def _word(text: str, start: str | None, end: str | None) -> dict[str, Any]:
    word: dict[str, Any] = {"type": "word_info", "text": text}
    if start is not None:
        word["start_offset"] = start
    if end is not None:
        word["end_offset"] = end
    return word


THIRTY = F(30)


def _parse(payload: dict[str, Any], duration: Fraction = THIRTY) -> gtp.ParsedTranscript:
    return gtp.parse_transcription(payload, chunk_duration=duration)


def test_the_documented_shape_becomes_exact_words_with_their_punctuation() -> None:
    parsed = _parse(_fixture("transcribe_korean.json"))
    assert [(w.text, w.display, w.start, w.end) for w in parsed.words] == [
        ("안녕하세요", "안녕하세요. ", F(1, 10), F(9, 10)),
        ("오늘은", "오늘은 ", F(6, 5), F(8, 5)),
        ("한강에", "한강에 ", F(17, 10), F(21, 10)),
        ("왔어요", "왔어요! ", F(11, 5), F(14, 5)),
        ("날씨", "날씨 ", F(18, 5), F(39, 10)),
        ("진짜", "진짜 ", F(4), F(43, 10)),
        ("좋다", "좋다", F(22, 5), F(24, 5)),
    ]
    assert (parsed.unplaced_chars, parsed.untimed_words, parsed.clamped_words) == (0, 0, 0)
    assert parsed.alignment == gtp.ALIGNED


def test_the_raw_rest_shape_without_output_text_reads_the_content_text() -> None:
    """W-52b2 — urllib가 받는 REST 응답에는 SDK 편의 칸 `output_text`가 없다(문서 REST 예시 꼴 ·
    단어마다 `speaker`). 글은 단어가 실린 content 글 칸에서 읽고, 결과는 SDK 꼴과 같다."""
    rest = _fixture("transcribe_rest_korean.json")
    assert "output_text" not in rest
    assert _parse(rest) == _parse(_fixture("transcribe_korean.json"))


def test_several_text_items_are_read_in_order_and_other_items_are_ignored() -> None:
    first = {"type": "text", "text": "안녕", "annotations": [_word("안녕", "0s", "0.4s")]}
    second = {"type": "text", "text": "가자", "annotations": [_word("가자", "1s", "1.3s")]}
    audio = {"type": "audio", "mime_type": "audio/wav"}
    payload = {"status": "completed", "steps": [{"content": [audio, first]}, {"content": [second]}]}
    parsed = _parse(payload)
    assert parsed.output_text == "안녕 가자"
    assert [w.display for w in parsed.words] == ["안녕 ", "가자"]
    assert parsed.unplaced_chars == 0 and parsed.alignment == gtp.ALIGNED


def test_a_response_without_any_transcript_names_the_keys_it_did_get() -> None:
    with pytest.raises(gtp.GeminiTranscribeResponseError, match=r"받은 칸: id, outputs, status"):
        _parse({"status": "completed", "id": "x", "outputs": []})


def test_a_rest_silent_chunk_is_an_unconfirmed_empty_not_a_failure() -> None:
    parsed = _parse({"status": "completed", "steps": []})
    assert parsed.words == () and parsed.output_text == ""


SILENT_REST = {
    "created": "2026-11-12T05:04:02Z",
    "model": "gemini-3.5-transcribe",
    "object": "interaction",
    "service_tier": "standard",
    "status": "completed",
    "updated": "2026-11-12T05:04:02Z",
    "usage": {"total_input_tokens": 80, "total_output_tokens": 0},
}
"""실제 무음 조각(2026-11-12, 말소리 0초 클립 넷)이 받은 최상위 칸 그대로 — 글 칸도 steps도 없다."""


def test_a_completed_rest_response_without_steps_is_an_unconfirmed_empty() -> None:
    parsed = _parse(dict(SILENT_REST))
    assert parsed.words == () and parsed.output_text == ""


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in SILENT_REST.items() if key != "usage"},
        {**SILENT_REST, "outputs": []},
        {**SILENT_REST, "status": "failed"},
    ],
)
def test_a_stepless_response_is_empty_only_when_completed_billed_and_metadata_only(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(gtp.GeminiTranscribeResponseError):
        _parse(payload)


def test_a_silent_chunk_is_an_unconfirmed_empty_not_a_failure() -> None:
    parsed = _parse(_fixture("transcribe_silent.json"))
    assert parsed.words == () and parsed.output_text == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "in_progress", "output_text": ""},
        {"status": "completed"},
        {"status": "completed", "output_text": 3},
        "not a mapping",
        _payload("말이 있는데 시각 단어가 없다"),
        {"status": "completed", "output_text": "", "steps": "x"},
        {"status": "completed", "steps": [{"content": [{"type": "text", "text": 3}]}]},
    ],
)
def test_a_response_that_is_not_a_completed_timed_transcript_fails(payload: object) -> None:
    with pytest.raises(gtp.GeminiTranscribeResponseError):
        gtp.parse_transcription(payload, chunk_duration=F(30))


def test_missing_bad_or_inverted_offsets_keep_the_text_as_untimed_words() -> None:
    parsed = _parse(
        _payload(
            "하나 둘 셋 넷",
            _word("하나", "0.5s", "0.9s"),
            _word("둘", None, "1.4s"),
            _word("셋", "1.5", "1.8s"),  # 단위 없음
            _word("넷", "2.4s", "2.1s"),  # 뒤집힘
        )
    )
    assert [(w.text, w.start, w.end) for w in parsed.words] == [
        ("하나", F(1, 2), F(9, 10)),
        ("둘", None, None),
        ("셋", None, None),
        ("넷", None, None),
    ]
    assert parsed.untimed_words == 3


def test_an_end_just_past_the_chunk_is_closed_and_far_past_fails() -> None:
    parsed = _parse(_payload("끝", _word("끝", "29.5s", "31.000s")))
    assert (parsed.words[0].start, parsed.words[0].end, parsed.clamped_words) == (
        F(59, 2),
        F(30),
        1,
    )
    with pytest.raises(gtp.GeminiTranscribeResponseError, match="청크"):
        _parse(_payload("끝", _word("끝", "29.5s", "32.5s")))


def test_letters_no_word_covers_are_counted_as_unplaced_not_output() -> None:
    parsed = _parse(
        _payload("안녕 숨은말 가자", _word("안녕", "0s", "0.4s"), _word("가자", "1s", "1.3s"))
    )
    assert [w.display for w in parsed.words] == ["안녕 ", "가자"]
    assert parsed.unplaced_chars == 3 and parsed.alignment == gtp.ALIGNED
    assert [w.dropped_after for w in parsed.words] == [3, 0]  # 그 글자 곁 단어에만 센다(W-52b1)


def test_leading_punctuation_attaches_to_the_first_word() -> None:
    parsed = _parse(
        _payload("「진짜?」 응", _word("진짜", "0s", "0.5s"), _word("응", "1s", "1.2s"))
    )
    assert [w.display for w in parsed.words] == ["「진짜?」 ", "응"]


def test_a_word_missing_from_the_text_falls_back_to_space_joined_words() -> None:
    parsed = _parse(
        _payload("완전히 다른 글", _word("안녕", "0s", "0.4s"), _word("가자", "1s", "1.3s"))
    )
    assert parsed.alignment == gtp.FALLBACK
    assert [w.display for w in parsed.words] == ["안녕 ", "가자 "]


def test_decomposed_hangul_is_normalised_before_matching() -> None:
    decomposed = unicodedata.normalize("NFD", "한강")
    parsed = _parse(
        _payload("한강 가자", _word(decomposed, "0s", "0.4s"), _word("가자", "1s", "1.3s"))
    )
    assert parsed.words[0].text == "한강" and parsed.alignment == gtp.ALIGNED


def test_non_word_annotations_and_speakers_are_ignored() -> None:
    payload = _payload("안녕", {**_word("안녕", "0s", "0.4s"), "speaker": "A"})
    payload["steps"][0]["content"][0]["annotations"].append({"type": "citation", "text": "x"})
    parsed = _parse(payload)
    assert [w.text for w in parsed.words] == ["안녕"]
