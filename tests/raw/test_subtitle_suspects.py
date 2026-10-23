"""D59 19e — 원본 자막의 무료 「확인 필요」 표시: 값만 받아 큐마다 이유를 돌려준다. 표시만 한다.

시험이 못 박는 것:

- 네 규칙(같은 말 런 · 영상 끝 인사말 · 다른 문자 · 말소리 측정상 없음)이 실데이터 모양의 예에서
  맞는 큐를 짚고, 문턱 아래(맞장구 두 번)는 짚지 않는다.
- 표시는 큐를 지우거나 숨기거나 고치지 않는다 — 입력 큐는 그대로이고, 결과는 이유뿐이다(규칙 4·9).
  「치즈」×9처럼 진짜 말일 수 있는 런도 표시만 될 뿐이다.
- 기준은 설정(`SubtitleSuspectSettings`)에서 오고, 어느 지문·설정 사전에도 실리지 않는다.
- 서비스의 줄 표시(`subtitle_cue_notes`)는 「다른 귀」 증거와 이유를 싣고, 말소리 측정상 없음은 지금
  자막 리비전의 확인 보고서(낡지 않은 것)에서만 가져온다.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import asdict, replace
from typing import Any

import pytest

from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import source_transcript
from vlogsrt.project.raw.analysis_config import RawTierSettings, RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    SUBTITLE_SUSPECT_SETTINGS,
    RawWorkflowConfig,
    SourceTextSettings,
    SubtitleSuspectSettings,
    subtitle_suspect_problems,
)
from vlogsrt.project.raw.subtitle_suspects import (
    BOILERPLATE,
    NO_SPEECH,
    NO_SPEECH_TEXT,
    OTHER_SCRIPT,
    REPEAT,
    suspects,
)

POLITE_END = re.compile(r"(니다|세요)$")


def _cues(*texts: str) -> list[tuple[str, str]]:
    return [(f"u{index}", text) for index, text in enumerate(texts)]


def _codes(found: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    return {cue_id: tuple(item.code for item in items) for cue_id, items in found.items()}


# ── (a) 같은 말이 이어진다 ─────────────────────────────────────────────────────────


def test_an_echo_run_flags_every_cue_in_it_with_the_run_length() -> None:
    """「개좋아」×35(실측 0021 꼴) — 접은 글자가 같으면 앞 공백·부호가 달라도 한 런이다."""
    texts = ["오늘 왕방 간다", *([" 개좋아"] * 34), "개좋아!", "다음에 또 오자"]
    cues = _cues(*texts)
    found = suspects(cues)
    run_ids = [cue_id for cue_id, _text in cues[1:36]]
    assert list(found) == run_ids  # 런 밖의 줄은 이유가 없다
    for cue_id in run_ids:
        (item,) = found[cue_id]
        assert (item.code, item.text) == (REPEAT, "같은 말이 35번 이어집니다")


def test_a_long_sentence_repeated_eighteen_times_is_one_run() -> None:
    sentence = " 이곳은 정말 아름다운 곳입니다."
    cues = _cues(*([sentence] * 18))
    found = suspects(cues)
    assert len(found) == 18
    assert {items[0].text for items in found.values()} == {"같은 말이 18번 이어집니다"}


def test_two_in_a_row_stays_below_the_threshold_and_breaks_are_runs_apart() -> None:
    """맞장구 두 번(「네」「네」)은 표시하지 않는다 — 문턱 3(설정). 사이에 다른 말이 끼면 다른
    런이다."""
    cues = _cues("네", "네.", "그래", "음", "음", "음", "좋아", "음", "음")
    assert _codes(suspects(cues)) == {"u3": (REPEAT,), "u4": (REPEAT,), "u5": (REPEAT,)}
    looser = replace(SUBTITLE_SUSPECT_SETTINGS, repeat_run_min=2)
    assert set(suspects(cues, settings=looser)) == {"u0", "u1", "u3", "u4", "u5", "u7", "u8"}


def test_cheese_nine_times_is_flagged_only_as_display_and_never_removed() -> None:
    """「치즈」×9 — 진짜 말일 수 있다(규칙 9). 표시는 이유만 돌려주고 큐는 하나도 건드리지
    않는다."""
    cues = _cues(*(["치즈"] * 9))
    before = list(cues)
    found = suspects(cues)
    assert cues == before  # 입력 그대로 — 지우거나 고치지 않는다
    assert list(found) == [cue_id for cue_id, _ in cues]
    assert all(items[0].text == "같은 말이 9번 이어집니다" for items in found.values())
    assert all(not hasattr(items[0], "remove") for items in found.values())


def test_texts_that_fold_to_nothing_never_make_a_run() -> None:
    """부호뿐인 줄(「…」)은 비교할 글자가 없다 — 런을 만들지 않는다."""
    assert suspects(_cues("…", "...", "…", "?")) == {}


# ── (b) 영상 끝 인사말 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        " 영상 봐주셔서 감사합니다",  # 실측 0005 꼴
        " 다음 영상에서 만나요!",  # 실측 0002 꼴
        "시청해주셔서 감사합니다.",  # 띄어쓰기가 달라도 접으면 같다
        "오늘도 구독과 좋아요 부탁드립니다~",
    ],
)
def test_end_of_video_boilerplate_is_flagged_by_folded_containment(text: str) -> None:
    (items,) = suspects(_cues("밥 먹자", text)).values()
    assert [(item.code, item.text) for item in items] == [
        (BOILERPLATE, "영상 끝 인사말이 끼었을 수 있습니다")
    ]


def test_ordinary_thanks_are_not_boilerplate() -> None:
    assert suspects(_cues("감사합니다", "봐줘서 고마워", "영상 찍자")) == {}


# ── (c) 한글·라틴 밖의 글자 ────────────────────────────────────────────────────────


def test_cyrillic_and_japanese_letters_are_flagged_with_the_letters_named() -> None:
    cues = _cues(
        " 물놀이 얼마 하지도 않긴 한거 усhod",  # 실측 0003 — 키릴 у·с가 라틴 사이에 섞였다
        "水が少し",  # 실측 0018
        " 피 потому 그럼 안 가져가고 좋을까?",
    )
    found = suspects(cues)
    assert _codes(found) == {"u0": (OTHER_SCRIPT,), "u1": (OTHER_SCRIPT,), "u2": (OTHER_SCRIPT,)}
    assert found["u0"][0].text == "다른 문자가 섞였습니다(у·с)"
    assert found["u1"][0].text == "다른 문자가 섞였습니다(水·が·少)"  # 처음 셋만


def test_hangul_latin_digits_punctuation_and_symbols_pass() -> None:
    cues = _cues(
        "ㅋㅋㅋ 진짜 OK? 3시 30분!",
        "Ｃｕｔ ½ ① ♪ ~ 100% ₩5,000 — 「왕방」",
        "café",  # 합성형으로 맞춘 뒤 라틴이다
        "café",  # 분해형도 같다
        "Iʼm ˈhello",  # 수식 글자(Lm, U+02BC·U+02C8)는 다른 문자가 아니다
        "1º 2ª 5µm 3ℓ",  # 호환 글자 — NFKC로 라틴(º·ª·ℓ), 마이크로는 허용 글자(설정)
    )
    assert suspects(cues) == {}


def test_lookalikes_that_fold_to_other_scripts_still_count() -> None:
    """NFKC가 라틴·한글로 접지 않는 글자는 여전히 다른 문자다 — 그리스 뮤(U+03BC)·키릴 у."""
    found = suspects(_cues("5μm", "усhod"))
    assert [items[0].code for items in found.values()] == [OTHER_SCRIPT, OTHER_SCRIPT]
    assert found["u0"][0].text == "다른 문자가 섞였습니다(μ)"


# ── (d) 말소리 측정상 없음 — 값으로 받는다 ───────────────────────────────────────────


def test_no_speech_cues_come_in_as_values_with_the_d55_wording() -> None:
    found = suspects(_cues("안녕", "여기 봐"), no_speech={"u1", "다른 원본의 줄"})
    assert list(found) == ["u1"]
    (item,) = found["u1"]
    assert (item.code, item.text) == (NO_SPEECH, "말소리를 못 찾음 (측정 한계일 수 있음)")
    assert item.text == NO_SPEECH_TEXT


def test_reasons_keep_rule_order_and_cue_order() -> None:
    cues = _cues("시청해 주셔서 감사합니다", "시청해 주셔서 감사합니다", "시청해 주셔서 감사합니다")
    found = suspects(cues, no_speech={"u2", "u0"})
    assert list(found) == ["u0", "u1", "u2"]
    assert [item.code for item in found["u0"]] == [REPEAT, BOILERPLATE, NO_SPEECH]
    assert [item.code for item in found["u1"]] == [REPEAT, BOILERPLATE]


def test_every_reason_is_polite_and_never_says_vad() -> None:
    cues = _cues(*(["усhod 영상 봐주셔서 감사합니다"] * 3))
    found = suspects(cues, no_speech={"u0"})
    texts = {item.text for items in found.values() for item in items}
    assert len(texts) == 4
    for text in texts:
        assert "VAD" not in text and "$" not in text
        if text != NO_SPEECH_TEXT:  # D55의 고정 문구 그대로다
            assert POLITE_END.search(re.sub(r"\([^)]*\)$", "", text)), text


# ── 설정 — 표시 기준은 지문 밖에 산다 ─────────────────────────────────────────────


def test_settings_live_outside_every_fingerprinted_settings() -> None:
    names = {field.name for field in dataclasses.fields(SubtitleSuspectSettings)}
    assert names == {
        "repeat_run_min",
        "boilerplate_phrases",
        "allowed_letter_scripts",
        "allowed_letters",
        "low_confidence_avg_logprob",  # W-25f(D76) — 표시 기준, whisper 레퍼런스 기본값
        "low_confidence_no_speech_prob",
        "small_vocabulary_run_min",  # W-29a(D77) — 번갈아 이어지는 적은 어휘 런
        "small_vocabulary_max_kinds",
        "symbols_only_min_chars",  # W-29a(D77) — 글자 없는 긴 부호 줄
    }
    for fingerprinted in (
        RawWorkflowConfig(),
        RawUnderstandingSettings(),
        RawTierSettings(),
        SourceTextSettings(),
    ):
        flat = repr(asdict(fingerprinted))
        assert "boilerplate" not in flat and "repeat_run" not in flat, type(fingerprinted)
        assert not any(
            isinstance(value, SubtitleSuspectSettings) for value in vars(fingerprinted).values()
        )
    assert subtitle_suspect_problems(SUBTITLE_SUSPECT_SETTINGS) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"repeat_run_min": 1}, "런 길이"),
        ({"repeat_run_min": True}, "런 길이"),
        ({"boilerplate_phrases": ("…",)}, "글자가 없다"),
        ({"boilerplate_phrases": ("줄\n바꿈",)}, "제어문자"),
        ({"allowed_letter_scripts": ()}, "비었다"),
        ({"allowed_letter_scripts": ("hangul",)}, "대문자"),
        ({"allowed_letters": ("µm",)}, "한 글자"),
    ],
)
def test_bad_settings_are_refused_before_any_flag(change: dict[str, Any], fragment: str) -> None:
    bad = replace(SUBTITLE_SUSPECT_SETTINGS, **change)
    assert any(fragment in problem for problem in subtitle_suspect_problems(bad))
    with pytest.raises(ValueError, match="확인 필요"):
        suspects(_cues("가", "가", "가"), settings=bad)


# ── 「다른 귀」 증거 — 원본 자막 줄에서 읽기만 ─────────────────────────────────────


def _row(text: str, **evidence: str) -> dict[str, Any]:
    return {"utterance_id": "u0", "text": text, **evidence}


def test_ear_evidence_names_the_rejected_candidate_and_the_replaced_line() -> None:
    model = SourceTextSettings().model
    assert source_transcript.ear_evidence(_row("옛 줄")) is None  # 19c 이전 — 증거 칸이 없다
    whisper_only = _row("망원동", whisper_text="망원동", text_source="whisper", alt_text="")
    assert source_transcript.ear_evidence(whisper_only) is None  # 두 귀가 같다(agreed)
    rejected = _row(
        "개좋아 여기 진짜", whisper_text="개좋아 여기 진짜", text_source="whisper",
        alt_text="개 좋아 여기 진짜 좋다 정말로 너무", retext_reason="too_long",
    )  # fmt: skip
    assert source_transcript.ear_evidence(rejected) == {
        "mark": "alt",
        "other": "개 좋아 여기 진짜 좋다 정말로 너무",
        "first": "",
    }
    replaced = _row("왕방 왔어요", whisper_text="왕반 왔어요", text_source=model, alt_text="")
    assert source_transcript.ear_evidence(replaced) == {
        "mark": "replaced",
        "other": "왕방 왔어요",
        "first": "왕반 왔어요",
    }
    typed = _row("사람이 고침", whisper_text="왕반 왔어요", text_source="manual", alt_text="")
    assert source_transcript.ear_evidence(typed) is None  # 사람이 고친 줄은 다른 귀 표시가 아니다
    typed_alt = dict(typed, alt_text="왕방 왔어요")
    assert source_transcript.ear_evidence(typed_alt) == {
        "mark": "alt",
        "other": "왕방 왔어요",
        "first": "왕반 왔어요",
    }


def test_ear_evidence_ignores_differences_only_in_spaces_and_punctuation() -> None:
    """공백·부호·대소문자만 다른 후보는 다른 귀 후보가 아니다 — 19d (c)의 `agreed`와 같은 접기
    (`hangul.strip_for_compare`)로 견준다(검증 지적 9). 부호뿐인 후보도 후보가 아니다."""
    model = SourceTextSettings().model
    for alt in ("개좋아 여기 진짜.", " 개좋아, 여기 진짜!", "…"):
        row = _row("개좋아 여기 진짜", whisper_text="개좋아 여기 진짜", text_source="whisper",
                   alt_text=alt)  # fmt: skip
        assert source_transcript.ear_evidence(row) is None, alt
    replaced = _row("왕방 왔어요", whisper_text="왕방 왔어요.", text_source=model, alt_text="")
    assert source_transcript.ear_evidence(replaced) == {
        "mark": "replaced",
        "other": "왕방 왔어요",
        "first": "",  # 처음 들은 글이 부호만 다르면 가져올 것이 없다
    }


def test_service_notes_carry_ears_and_reasons_and_read_no_speech_only_from_a_current_report() -> (
    None
):
    model = SourceTextSettings().model
    transcript = {
        "utterances": [
            _row("치즈", whisper_text="치즈", text_source="whisper", alt_text="치즈요"),
            {"utterance_id": "u1", "text": "치즈"},
            {"utterance_id": "u2", "text": "치즈"},
            {"utterance_id": "u3", "text": "왕방", "whisper_text": "왕반", "text_source": model},
            {"utterance_id": "u4", "text": "여기"},
        ]
    }
    before = repr(transcript)
    current = {"stale": False, "cues": [{"id": "u4", "flags": ["no_speech", "early"]}]}
    notes = raw.RawWorkspaceService.subtitle_cue_notes(transcript, current)
    assert repr(transcript) == before  # 읽기만 한다
    assert notes["u0"]["ear"]["mark"] == "alt"
    assert notes["u0"]["suspects"] == [{"code": REPEAT, "text": "같은 말이 3번 이어집니다"}]
    assert "ear" not in notes["u1"] and notes["u1"]["suspects"][0]["code"] == REPEAT
    assert notes["u3"] == {"ear": {"mark": "replaced", "other": "왕방", "first": "왕반"}}
    assert notes["u4"] == {"suspects": [{"code": NO_SPEECH, "text": NO_SPEECH_TEXT}]}
    stale = dict(current, stale=True)  # 다른 자막 리비전을 잰 보고서 — 그 표지는 싣지 않는다
    assert "u4" not in raw.RawWorkspaceService.subtitle_cue_notes(transcript, stale)
    assert "u4" not in raw.RawWorkspaceService.subtitle_cue_notes(transcript, None)
