"""W-29a — whisper 환각을 짚는 「확인 필요」 셋(D77). 표시만 한다.

실데이터 17개 영상의 환각 가운데 기존 표시가 짚은 것은 6개였다. 시험이 못 박는 것:

- 다른 귀가 조각을 다르게 들은 줄(`retext_reason`에 `chunk_similarity`) — 부른 쪽이 `CueTiming`
  으로 **값으로** 넘길 때만(화면만 넘긴다 — 등급의 「자막 확인됨」 몫에는 들지 않는다).
- 끝 인사말 목록에 실측 구절이 더해졌다(「Thanks for watching」·「유료광고를 포함」 …).
- 몇 가지 말이 번갈아 길게 이어지는 「작은 어휘 런」(Be safe/Be happy/Be cool ×18) — 같은 말 하나의
  런은 기존 규칙의 몫이라 겹쳐 달지 않는다. 짧은 맞장구 몇 줄은 걸리지 않는다.
- 서비스는 줄의 `retext_reason`으로 그 재료를 만든다.
"""

from __future__ import annotations

import dataclasses

import pytest

from vlogsrt.project.raw import subtitle_suspects as ss
from vlogsrt.project.raw.config import SUBTITLE_SUSPECT_SETTINGS, SubtitleSuspectSettings
from vlogsrt.project.raw.service import RawWorkspaceService


def codes(found: dict[str, tuple[ss.Suspect, ...]]) -> dict[str, list[str]]:
    return {cue_id: [s.code for s in reasons] for cue_id, reasons in found.items()}


def test_a_line_the_other_ear_heard_differently_is_flagged_only_with_material() -> None:
    cues = [("a", "시청해주셔서 감사합니다!"), ("b", "좋아")]
    timing = ss.CueTiming(ear_disagreed=frozenset({"b"}))
    found = ss.suspects(cues, timing=timing)
    assert codes(found) == {"a": [ss.BOILERPLATE], "b": [ss.EAR_DISAGREED]}
    assert found["b"][0].text == ss.EAR_DISAGREED_TEXT
    assert "b" not in ss.suspects(cues)  # 재료가 없으면 예전 그대로


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for watching.",
        "이 영상은 유료광고를 포함하고 있습니다.",
        "다음번엔 더 재밌는 영상으로 찾아뵐게요!",
        "그럼 다음에 만나요!",
        "수고하셨습니다.",
        "맛있게 잘 먹었습니다 ^^",
        "오늘도 영상 시청해주셔서 감사합니다.",
    ],
)
def test_measured_outro_and_ad_phrases_are_boilerplate(text: str) -> None:
    assert codes(ss.suspects([("a", text)])) == {"a": [ss.BOILERPLATE]}


def test_alternating_three_phrases_eighteen_times_is_a_small_vocabulary_run() -> None:
    phrases = ["Be safe.", "Be happy.", "Be cool."]
    cues = [(f"c{i}", phrases[i % 3]) for i in range(18)] + [("x", "다른 말")]
    found = ss.suspects(cues)
    assert all(codes(found)[f"c{i}"] == [ss.SMALL_VOCABULARY] for i in range(18))
    assert "x" not in found
    assert found["c0"][0].text == ss.SMALL_VOCABULARY_TEXT.format(count=18)


def test_a_two_phrase_run_followed_by_other_talk_is_still_found() -> None:
    cues = [(f"c{i}", ["Be safe.", "Be happy."][i % 2]) for i in range(8)]
    cues += [("x", "오늘은 시장"), ("y", "내일은 바다")]
    found = ss.suspects(cues)
    assert set(found) == {f"c{i}" for i in range(8)}
    assert found["c0"][0].text == ss.SMALL_VOCABULARY_TEXT.format(count=8)


def test_an_odd_length_alternation_is_found_whole() -> None:
    cues = [(f"c{i}", ["Be safe.", "Be happy."][i % 2]) for i in range(7)]
    found = ss.suspects(cues)
    assert set(found) == {f"c{i}" for i in range(7)}


def test_blocks_of_the_same_phrase_are_not_an_alternation() -> None:
    cues = [(f"c{i}", "아" if i < 4 else "와") for i in range(8)]
    assert {code for reasons in codes(ss.suspects(cues)).values() for code in reasons} == {
        ss.REPEAT
    }


def test_short_back_and_forth_is_not_flagged() -> None:
    cues = [("a", "네"), ("b", "응"), ("c", "네"), ("d", "응"), ("e", "오늘은 시장에 갑니다")]
    assert ss.suspects(cues) == {}


def test_one_phrase_runs_stay_with_the_repeat_rule() -> None:
    cues = [(f"c{i}", "개좋아") for i in range(8)]
    assert {code for reasons in codes(ss.suspects(cues)).values() for code in reasons} == {
        ss.REPEAT
    }


def test_the_run_shape_is_a_setting_and_is_checked() -> None:
    fields = {field.name for field in dataclasses.fields(SubtitleSuspectSettings)}
    assert {"small_vocabulary_run_min", "small_vocabulary_max_kinds"} <= fields
    assert SUBTITLE_SUSPECT_SETTINGS.small_vocabulary_run_min == 6
    assert SUBTITLE_SUSPECT_SETTINGS.small_vocabulary_max_kinds == 3
    for bad in (
        SubtitleSuspectSettings(small_vocabulary_run_min=1),
        SubtitleSuspectSettings(small_vocabulary_max_kinds=1),
    ):
        with pytest.raises(ValueError):
            ss.suspects([("a", "말")], settings=bad)


def test_the_service_reads_ear_disagreement_from_each_line() -> None:
    transcript = {
        "utterances": [
            {"utterance_id": "u0", "text": "a", "retext_reason": "chunk_similarity"},
            {"utterance_id": "u1", "text": "b", "retext_reason": "match+chunk_similarity"},
            {"utterance_id": "u2", "text": "c", "retext_reason": "accepted"},
            {"utterance_id": "u3", "text": "d"},
        ]
    }
    timing = RawWorkspaceService.subtitle_cue_timing(transcript, None)
    assert timing.ear_disagreed == frozenset({"u0", "u1"})
    notes = RawWorkspaceService.subtitle_cue_notes(transcript, None, timing)
    assert set(notes) == {"u0", "u1"}
    assert RawWorkspaceService.subtitle_cue_notes(transcript, None) == {}  # D65 몫


@pytest.mark.parametrize("text", ["¡¡¡¡¡¡¡¡¡¡", "?!?!?!", "!!! ??? ..."])
def test_a_long_line_of_only_symbols_is_flagged(text: str) -> None:
    assert codes(ss.suspects([("a", text)])) == {"a": [ss.SYMBOLS_ONLY]}


@pytest.mark.parametrize("text", ["…", "...", "?!", "  "])
def test_short_pause_marks_are_not_flagged(text: str) -> None:
    assert ss.suspects([("a", text)]) == {}
