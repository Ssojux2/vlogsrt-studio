"""W-25f — 원본 자막 「확인 필요」에 시각 표시 셋을 더한다: 조각 끝 잘림 · 정수 초 · 낮은 확신(D76).

유료 호출은 없다. 시험이 못 박는 것:

- 순수 규칙(`subtitle_suspects`): 부른 쪽이 **값으로** 넘긴 재료(청크 끝에 맞춘 줄 · 전사
  조각 시계의 시각 · whisper 세그먼트 신뢰도)로만 판정한다. 정수 초는 시작·끝이 **둘 다** 조각
  시계의 정수 초일 때다. 낮은 확신은 whisper 레퍼런스 기본값(`avg_logprob < -1.0` ·
  `no_speech_prob ≥ 0.6`)이고 기준은 설정에 있다. 「말소리를 못 찾음」과 겹치면(무음 확률이 높다)
  그 표시 하나가 더 센 말로 바뀐다.
- 재료가 없으면(기본값) 예전 표시 그대로다 — 등급의 「자막 확인됨」 몫(D65)은 이 재료 없이 센다.
- 서비스: 1단계 기록의 청크(요청 아이디·시작·끝에 맞춘 위치·신뢰도)를 발화의 `evidence_id`로
  줄에 잇는다.
- 기록 읽기(`sync_report.stage1_audio`)가 청크마다 그 재료를 싣는다(옛 기록은 빈 값).
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.raw import subtitle_suspects as ss
from vlogsrt.project.raw import sync_report
from vlogsrt.project.raw.config import SUBTITLE_SUSPECT_SETTINGS, SubtitleSuspectSettings
from vlogsrt.project.raw.service import RawWorkspaceService

F = Fraction
CUES = [("a", "안녕하세요"), ("b", "좋아"), ("c", "여기"), ("d", "끝")]


def codes(found: dict[str, tuple[ss.Suspect, ...]]) -> dict[str, list[str]]:
    return {cue_id: [s.code for s in reasons] for cue_id, reasons in found.items()}


# ── 순수 규칙 ─────────────────────────────────────────────────────────────────


def test_without_timing_material_nothing_new_is_flagged() -> None:
    assert ss.suspects(CUES) == {}
    assert ss.suspects(CUES, timing=ss.CueTiming()) == {}


def test_a_line_closed_at_the_chunk_end_is_flagged() -> None:
    found = ss.suspects(CUES, timing=ss.CueTiming(clamped=frozenset({"d"})))
    assert codes(found) == {"d": [ss.CHUNK_END_CLAMPED]}
    assert found["d"][0].text == ss.CHUNK_END_CLAMPED_TEXT


def test_whole_seconds_need_both_edges_on_the_chunk_clock() -> None:
    local = {
        "a": (F(12), F(14)),  # 둘 다 정수 초
        "b": (F(12), F(29, 2)),  # 끝만 아니다
        "c": (F(0), F(3)),  # 조각 시작 0.0도 정수 초다
        "d": (F(1, 3), F(2)),
    }
    found = ss.suspects(CUES, timing=ss.CueTiming(local_times=local))
    assert codes(found) == {"a": [ss.WHOLE_SECOND], "c": [ss.WHOLE_SECOND]}
    assert found["a"][0].text == ss.WHOLE_SECOND_TEXT


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ((0.1, -0.3), None),
        ((0.1, -1.0), None),  # 경계값은 낮지 않다(< -1.0)
        ((0.1, -1.2), ss.LOW_LOGPROB_TEXT),
        ((0.6, -0.3), ss.HIGH_NO_SPEECH_TEXT),  # 경계값은 높다(≥ 0.6)
        ((0.7, -1.5), f"{ss.LOW_LOGPROB_TEXT} · {ss.HIGH_NO_SPEECH_TEXT}"),
        ((None, None), None),
        ((None, -2.0), ss.LOW_LOGPROB_TEXT),
    ],
)
def test_low_confidence_uses_the_whisper_reference_thresholds(
    score: tuple[float | None, float | None], expected: str | None
) -> None:
    found = ss.suspects(CUES, timing=ss.CueTiming(scores={"b": score}))
    if expected is None:
        assert found == {}
    else:
        assert codes(found) == {"b": [ss.LOW_CONFIDENCE]}
        assert found["b"][0].text == expected


def test_overlap_with_no_speech_becomes_one_stronger_mark() -> None:
    timing = ss.CueTiming(scores={"a": (0.8, -0.2), "b": (0.8, -1.4)})
    found = ss.suspects(CUES, no_speech={"a", "b", "c"}, timing=timing)
    assert codes(found) == {
        "a": [ss.NO_SPEECH],
        "b": [ss.NO_SPEECH, ss.LOW_CONFIDENCE],
        "c": [ss.NO_SPEECH],
    }
    assert found["a"][0].text == found["b"][0].text == ss.NO_SPEECH_STRONG_TEXT
    assert found["b"][1].text == ss.LOW_LOGPROB_TEXT  # 무음 확률은 센 표시로 옮겼다
    assert found["c"][0].text == ss.NO_SPEECH_TEXT


def test_rules_keep_their_order_and_never_drop_a_line() -> None:
    timing = ss.CueTiming(
        clamped=frozenset({"a"}),
        local_times={"a": (F(1), F(2))},
        scores={"a": (0.9, -2.0)},
    )
    found = ss.suspects([("a", "같은 말"), ("x", "같은 말"), ("y", "같은 말")], timing=timing)
    assert codes(found)["a"] == [
        ss.REPEAT,
        ss.CHUNK_END_CLAMPED,
        ss.WHOLE_SECOND,
        ss.LOW_CONFIDENCE,
    ]


def test_the_thresholds_are_settings_and_are_checked() -> None:
    assert SUBTITLE_SUSPECT_SETTINGS.low_confidence_avg_logprob == -1.0
    assert SUBTITLE_SUSPECT_SETTINGS.low_confidence_no_speech_prob == 0.6
    strict = SubtitleSuspectSettings(low_confidence_avg_logprob=-0.5)
    found = ss.suspects(CUES, settings=strict, timing=ss.CueTiming(scores={"a": (0.0, -0.7)}))
    assert codes(found) == {"a": [ss.LOW_CONFIDENCE]}
    for bad in (
        SubtitleSuspectSettings(low_confidence_no_speech_prob=1.5),
        SubtitleSuspectSettings(low_confidence_avg_logprob=float("nan")),
    ):
        with pytest.raises(ValueError):
            ss.suspects(CUES, settings=bad)


def test_new_wording_never_says_it_is_not_speech() -> None:
    for text in (
        ss.CHUNK_END_CLAMPED_TEXT,
        ss.WHOLE_SECOND_TEXT,
        ss.LOW_LOGPROB_TEXT,
        ss.HIGH_NO_SPEECH_TEXT,
        ss.NO_SPEECH_STRONG_TEXT,
    ):
        assert "말이 아니" not in text and "VAD" not in text and "whisper" not in text.lower()


# ── 기록 읽기 · 서비스 ───────────────────────────────────────────────────────


def _record(root: Path) -> None:
    folder = root / "analysis" / "anal_1" / "000001_src"
    folder.mkdir(parents=True)
    body = {
        "source_sha256": "f" * 64,
        "created_at": "2026-10-22T00:00:00+00:00",
        "video": {"presentation_origin": {"num": 1, "den": 2}},
        "audio": {
            "pcm_sha256": "0" * 64,
            "audio_stream_index": 1,
            "sample_rate": 16000,
            "channels": 1,
            "sample_count": 16000 * 400,
            "pcm_sample0_file_time": {"num": 1, "den": 1},
        },
        "asr": {
            "sample_rate": 16000,
            "chunks": [
                {
                    "chunk_index": 0,
                    "request_id": "run:chunk-0000",
                    "sample_start": 0,
                    "sample_end": 2880000,
                    "failure": "",
                    "segments": [
                        {"start": 0.0, "end": 2.0, "text": "안녕하세요"},
                        {"start": 3.5, "end": 180.0, "text": "좋아"},
                    ],
                    "clamped_segments": [1],
                },
                {
                    "chunk_index": 1,
                    "request_id": "run:chunk-0001",
                    "sample_start": 2880000,
                    "sample_end": 6400000,
                    "failure": "",
                    "segments": [
                        {
                            "start": 1.25,
                            "end": 2.0,
                            "text": "여기",
                            "no_speech_prob": 0.7,
                            "avg_logprob": -1.3,
                        }
                    ],
                },
            ],
        },
    }
    (folder / "asr_outcome.json").write_text(json.dumps(body), encoding="utf-8")


def test_stage_one_records_carry_each_chunks_material(tmp_path: Path) -> None:
    _record(tmp_path)
    audio = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert audio is not None
    first, second = audio.chunks
    assert (first.request_id, first.start, first.clamped) == ("run:chunk-0000", F(0), (1,))
    assert first.scores == ()
    assert (second.request_id, second.start) == ("run:chunk-0001", F(180))
    assert second.scores == ((0.7, -1.3),)


def _row(cue_id: str, start: Fraction, end: Fraction, evidence: str, text: str) -> dict[str, Any]:
    enc = {"num": 0, "den": 1}
    return {
        "utterance_id": cue_id,
        "text": text,
        "start": {**enc, "num": start.numerator, "den": start.denominator},
        "end": {**enc, "num": end.numerator, "den": end.denominator},
        "evidence_id": evidence,
    }


def test_the_service_joins_record_material_to_lines_by_evidence_id(tmp_path: Path) -> None:
    _record(tmp_path)
    audio = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    shift = F(1) - F(1, 2)  # PCM 원점 − 제시 원점 = 공통 시계의 PCM 0
    transcript = {
        "utterances": [
            _row("u0", shift + F(0), shift + F(2), "run:chunk-0000:0", "안녕하세요"),
            _row("u1", shift + F(7, 2), shift + F(180), "run:chunk-0000:1", "좋아"),
            _row("u2", shift + F(180) + F(5, 4), shift + F(182), "run:chunk-0001:0", "여기"),
            {**_row("u3", F(300), F(301), "", "고친 줄"), "evidence_id": ""},
        ]
    }
    timing = RawWorkspaceService.subtitle_cue_timing(transcript, audio)
    assert timing.clamped == frozenset({"u1"})
    assert timing.local_times["u0"] == (F(0), F(2)) and timing.local_times["u2"] == (F(5, 4), F(2))
    assert timing.scores == {"u2": (0.7, -1.3)}
    assert "u3" not in timing.local_times
    notes = RawWorkspaceService.subtitle_cue_notes(transcript, None, timing)
    assert [s["code"] for s in notes["u0"]["suspects"]] == [ss.WHOLE_SECOND]
    assert [s["code"] for s in notes["u1"]["suspects"]] == [ss.CHUNK_END_CLAMPED]
    assert [s["code"] for s in notes["u2"]["suspects"]] == [ss.LOW_CONFIDENCE]
    assert RawWorkspaceService.subtitle_cue_notes(transcript, None) == {}  # 재료 없이 — D65 몫


def test_without_a_record_there_is_no_timing_material() -> None:
    timing = RawWorkspaceService.subtitle_cue_timing({"utterances": []}, None)
    assert timing == ss.CueTiming()


# ── W-52b1 — Gemini 시간 엔진이 줄에서 센 것 ────────────────────────────────────


def test_engine_word_counts_become_display_only_lines_in_rule_order() -> None:
    timing = ss.CueTiming(
        clamped=frozenset({"d"}),
        engine_words={"b": ("untimed_words", "out_of_order"), "d": ("unplaced_chars",)},
    )
    found = ss.suspects(CUES, timing=timing)
    assert codes(found) == {
        "b": [ss.ENGINE_WORDS, ss.ENGINE_WORDS],
        "d": [ss.CHUNK_END_CLAMPED, ss.ENGINE_WORDS],
    }
    assert [s.text for s in found["b"]] == [
        ss.ENGINE_WORD_TEXTS["untimed_words"],
        ss.ENGINE_WORD_TEXTS["out_of_order"],
    ]
    for text in ss.ENGINE_WORD_TEXTS.values():
        assert text.endswith("니다") and "말이 아니" not in text


def test_engine_word_names_match_the_counter_and_unknown_names_are_ignored() -> None:
    from vlogsrt.project.raw import word_cues

    assert tuple(ss.ENGINE_WORD_TEXTS) == word_cues.WORD_NOTES
    timing = ss.CueTiming(engine_words={"a": ("다른 이름",)})
    assert ss.suspects(CUES, timing=timing) == {}


def test_the_record_carries_engine_word_counts_to_the_line(tmp_path: Path) -> None:
    _record(tmp_path)
    path = next(tmp_path.rglob("asr_outcome.json"))
    body = json.loads(path.read_text(encoding="utf-8"))
    body["asr"]["chunks"][0]["word_notes"] = [[0, "untimed_words"], [0, "fallback_alignment"]]
    path.write_text(json.dumps(body), encoding="utf-8")
    audio = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert audio is not None
    first, second = audio.chunks
    assert first.word_notes == ((0, "untimed_words"), (0, "fallback_alignment"))
    assert second.word_notes == ()  # 옛 기록·whisper 청크는 칸이 없다
    shift = F(1) - F(1, 2)
    transcript = {"utterances": [_row("u0", shift, shift + F(2), "run:chunk-0000:0", "안녕")]}
    timing = RawWorkspaceService.subtitle_cue_timing(transcript, audio)
    assert timing.engine_words == {"u0": ("untimed_words", "fallback_alignment")}
    notes = RawWorkspaceService.subtitle_cue_notes(transcript, None, timing)
    assert [s["code"] for s in notes["u0"]["suspects"]] == [
        ss.ENGINE_WORDS,
        ss.ENGINE_WORDS,
        ss.WHOLE_SECOND,
    ]
