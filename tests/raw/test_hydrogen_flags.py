"""W-114e(D187) — Hydrogen 글자 귀의 「말 없음」·「줄 밖 말」은 「확인 필요」 표시일 뿐이다.

- `speech=false` 줄은 지우지도 고치지도 않고 「Hydrogen이 말을 듣지 못했다」 표시가 붙는다.
- 줄 밖 말(`extra`)은 시각이 없어 줄로 넣지 않고, 앞 줄(첫 줄 앞이면 첫 줄)에 들은 말째 표시된다.
- 기록 모양이 다르면 표시를 지어내지 않는다.
"""

from __future__ import annotations

from vlogsrt.project.raw import hydrogen_text_pass, subtitle_suspects

CUES = (("a", "안녕하세요"), ("b", "오늘은 한강에"), ("c", "왔어요"))


def test_silent_lines_and_extra_speech_are_flagged_only() -> None:
    timing = subtitle_suspects.CueTiming(
        hydrogen_silent=frozenset({"b"}),
        hydrogen_extra={"": ("저기요",), "c": ("어 잠깐만", "가자")},
    )
    found = subtitle_suspects.suspects(CUES, timing=timing)
    codes = {cue: [s.code for s in reasons] for cue, reasons in found.items()}
    assert codes == {
        "a": [subtitle_suspects.HYDROGEN_EXTRA],
        "b": [subtitle_suspects.HYDROGEN_SILENT],
        "c": [subtitle_suspects.HYDROGEN_EXTRA, subtitle_suspects.HYDROGEN_EXTRA],
    }
    assert "앞에서" in found["a"][0].text and "저기요" in found["a"][0].text
    assert "뒤에서" in found["c"][0].text and "어 잠깐만" in found["c"][0].text
    assert subtitle_suspects.suspects(CUES) == {}  # 재료가 없으면 표시도 없다


def test_record_flags_read_both_records_and_ignore_odd_shapes() -> None:
    record = {
        "chunks": [
            {"no_speech": ["b"], "extra": [{"after_id": "c", "text": "가자"}]},
            {"no_speech": [], "extra": [{"after_id": None, "text": "저기요"}, {"text": " "}]},
            "odd",
        ]
    }
    silent, extra = hydrogen_text_pass.record_flags(record)
    assert silent == frozenset({"b"})
    assert extra == {"c": ("가자",), "": ("저기요",)}
    assert hydrogen_text_pass.record_flags(None) == (frozenset(), {})
    assert hydrogen_text_pass.record_flags({"chunks": "x"}) == (frozenset(), {})
