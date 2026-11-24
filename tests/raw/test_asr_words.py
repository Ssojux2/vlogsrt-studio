"""W-25c — whisper 단어 시각·세그먼트 신뢰도를 버리지 않고 남긴다(D73).

유료 호출은 없다. 시험이 못 박는 것:

- 1단계 내부 전사만 `["segment","word"]`로 묻는다 — 작업공간에 저장된 설정(세그먼트만)은 그대로이고
  청크 캐시 열쇠도 그대로라 이미 산 전사를 다시 사지 않는다. 최종 자막 요청은 세그먼트만이다.
- 파서: 기존 녹화 응답(`fr_day_1_2_fhd.whisper.json`)의 단어·신뢰도를 읽고, 세그먼트 시각은 단어·
  신뢰도를 뺀 응답과 바이트째 같다(규칙 1 — 단어는 시각의 원천이 아니다). 꼴이 어긋난 단어는 청크를
  버리지 않고 단어만 비우며 경고한다.
- 기록: 단어·신뢰도가 있으면 청크 기록·캐시에 실리고, 없으면 칸이 없다(옛 바이트 그대로). 1단계
  기록에서 다시 세운 증거는 같은 JSON을 낸다. 발화(원본 자막을 심는 줄)에는 실리지 않는다.
- 보고서: 1단계 기록의 단어 경계를 공통 시계로 읽고, 줄 경계를 찾는 폭 안의 가장 가까운 단어
  경계로 옮긴 「단어 경계」 값을 정답과 견준다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from vlogsrt import log
from vlogsrt.models import Chunk
from vlogsrt.project.raw import capabilities, sync_report, timing_truth
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw.caption_provider import (
    audio_chunk_binding,
    parse_timing_response,
)
from vlogsrt.project.raw.config import (
    RawCaptionContract,
    RawWorkflowConfig,
    caption_problems,
    stage1_timing_view,
)
from vlogsrt.project.raw.understanding import SelectedAudio

F = Fraction
BEFORE_SHUTDOWN = date(2026, 10, 2)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "api" / "fr_day_1_2_fhd.whisper.json"
REPORT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "timing_truth_report.py"


WORDED = {
    "segments": [
        {
            "start": 0.5,
            "end": 2.0,
            "text": " 안녕 하세요",
            "no_speech_prob": 0.1,
            "avg_logprob": -0.3,
        },
        {"start": 3.0, "end": 4.5, "text": " 좋아", "no_speech_prob": 0.7, "avg_logprob": -1.2},
    ],
    "words": [
        {"word": "안녕", "start": 0.62, "end": 1.1},
        {"word": "하세요", "start": 1.1, "end": 1.94},
        {"word": "좋아", "start": 3.2, "end": 4.4},
    ],
}


def audio_of(tmp_path: Path, seconds: int = 20) -> SelectedAudio:
    samples = seconds * stage1.PCM_SAMPLE_RATE
    pcm = tmp_path / "analysis.pcm"
    pcm.write_bytes(bytes((index * 7 + 3) % 256 for index in range(samples * 2)))
    return SelectedAudio(
        source_id="source_test",
        source_path=tmp_path / "original.mp4",
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=pcm,
        pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
        sample_rate=stage1.PCM_SAMPLE_RATE,
        channels=1,
        sample_count=samples,
        pcm_sample0_file_time=F(0),
    )


def chunk_of(tmp_path: Path, duration: float) -> tuple[Chunk, Any]:
    wav = tmp_path / "chunk.wav"
    wav.write_bytes(b"synthetic-wav")
    chunk = Chunk(path=wav, offset=0.0, duration=duration, index=0)
    binding = audio_chunk_binding(
        chunk, sample_start=0, sample_end=round(duration * 16000), sample_rate=16000, request_id="r"
    )
    return chunk, binding


def parse(tmp_path: Path, payload: dict[str, Any], duration: float) -> Any:
    chunk, binding = chunk_of(tmp_path, duration)
    return parse_timing_response(
        payload,
        binding=binding,
        model="whisper-1",
        provider="whisper_verbose_json",
        chunk_duration=chunk.duration,
    )


# ── 요청 — 1단계만 단어를 묻고, 저장된 설정·열쇠는 그대로 ──────────────────────


def test_the_stage_one_view_only_changes_the_granularity() -> None:
    stored = RawWorkflowConfig()
    view = stage1_timing_view(stored)
    assert view.caption.timestamp_granularities == ("segment", "word")
    assert replace(view, caption=stored.caption) == stored


def test_the_contract_accepts_segment_or_segment_and_word_only() -> None:
    contract = RawCaptionContract()
    assert caption_problems(replace(contract, timestamp_granularities=("segment", "word"))) == []
    for bad in (("word",), ("word", "segment"), ()):
        problems = caption_problems(replace(contract, timestamp_granularities=bad))
        assert any("시간 입자" in problem for problem in problems), bad


def test_the_timing_role_confirms_word_times() -> None:
    timing = capabilities.confirmed_capability_snapshots()[1]
    assert timing.supports(capabilities.FEATURE_WORD_TIMES)
    confirmed = capabilities.confirm_request_combo(
        role=capabilities.ROLE_TIMING,
        model=timing.model_id,
        response_format="verbose_json",
        features=(capabilities.FEATURE_SEGMENT_TIMES, capabilities.FEATURE_WORD_TIMES),
    )
    assert confirmed.role == capabilities.ROLE_TIMING


# ── 파서 — 녹화 응답의 단어·신뢰도, 세그먼트 시각은 그대로 ─────────────────────


def _recorded() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["chunks"][1]["response"]


def test_the_recorded_response_keeps_words_and_scores(tmp_path: Path) -> None:
    response = _recorded()
    timed = parse(tmp_path, response, float(response["duration"]))
    assert len(timed.words) == len(response["words"]) > 0
    first = response["words"][0]
    assert (timed.words[0].start, timed.words[0].end, timed.words[0].word) == (
        first["start"],
        first["end"],
        first["word"],
    )
    segment = response["segments"][0]
    assert timed.segments[0].no_speech_prob == segment["no_speech_prob"]
    assert timed.segments[0].avg_logprob == segment["avg_logprob"]


def test_words_and_scores_never_change_the_segment_times(tmp_path: Path) -> None:
    response = _recorded()
    bare = {
        "segments": [
            {"start": s["start"], "end": s["end"], "text": s["text"]} for s in response["segments"]
        ]
    }
    full = parse(tmp_path, response, float(response["duration"]))
    plain = parse(tmp_path, bare, float(response["duration"]))
    times = [(s.index, s.start, s.end, s.text, s.evidence_id) for s in full.segments]
    assert times == [(s.index, s.start, s.end, s.text, s.evidence_id) for s in plain.segments]
    assert plain.words == () and all(s.no_speech_prob is None for s in plain.segments)


@pytest.mark.parametrize(
    "words",
    [
        "not a list",
        [{"word": "a", "start": 1.0}],
        [{"word": 3, "start": 0.0, "end": 1.0}],
        [{"word": "a", "start": 2.0, "end": 1.0}],
        [{"word": "a", "start": -0.5, "end": 1.0}],
        [{"word": "a", "start": 4.0, "end": 9.0}],  # 청크 끝을 관용 밖으로
    ],
)
def test_malformed_words_drop_only_the_words_and_warn(
    tmp_path: Path, words: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    payload = {"segments": WORDED["segments"], "words": words}
    timed = parse(tmp_path, payload, 5.0)
    assert timed.words == ()
    assert [s.text for s in timed.segments] == [" 안녕 하세요", " 좋아"]
    assert any("단어 시각" in warning for warning in warnings)


def test_a_word_just_past_the_chunk_end_closes_at_the_end_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    payload = {"segments": [], "words": [{"word": "끝", "start": 4.8, "end": 5.6}]}
    timed = parse(tmp_path, payload, 5.0)
    assert [(w.start, w.end) for w in timed.words] == [(4.8, 5.0)]
    assert any("단어" in warning for warning in warnings)


def test_bad_scores_are_left_out_not_invented(tmp_path: Path) -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "a", "no_speech_prob": "high", "avg_logprob": None},
            {"start": 1.0, "end": 2.0, "text": "b", "no_speech_prob": True},
        ]
    }
    timed = parse(tmp_path, payload, 5.0)
    assert [(s.no_speech_prob, s.avg_logprob) for s in timed.segments] == [(None, None)] * 2


# ── 기록 — 있으면 싣고, 없으면 옛 바이트 그대로 ────────────────────────────────


# ── 보고서 — 단어 경계 ─────────────────────────────────────────────────────────


def _stage1_folder(root: Path, words: list[dict[str, Any]]) -> None:
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
                {"chunk_index": 0, "sample_start": 0, "sample_end": 2880000, "failure": ""},
                {
                    "chunk_index": 1,
                    "sample_start": 2880000,
                    "sample_end": 6400000,
                    "failure": "",
                    "words": words,
                },
            ],
        },
    }
    (folder / "asr_outcome.json").write_text(json.dumps(body), encoding="utf-8")


def test_stage_one_records_carry_word_edges_on_the_pcm_clock(tmp_path: Path) -> None:
    _stage1_folder(tmp_path, [{"word": "a", "start": 1.25, "end": 2.0}])
    stage1_audio = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert stage1_audio is not None
    assert stage1_audio.word_edges == ((F(181, 1) + F(1, 4), F(182)),)


def test_word_lines_move_each_edge_to_the_nearest_word_edge_within_reach() -> None:
    lines = (
        timing_truth.TruthLine("a", "말", F(10), F(12)),
        timing_truth.TruthLine("b", "말", F(20), F(22)),
    )
    edges = ((F(41, 4), F(23, 2)), (F(9, 2), F(5)), (F(30), F(31)))
    moved = timing_truth.word_lines(lines, edges, reach=F(1))
    assert moved == (
        timing_truth.TruthLine("a", "말", F(41, 4), F(23, 2)),
        timing_truth.TruthLine("b", "말", F(20), F(22)),  # 폭 안에 단어가 없다 — 그대로
    )


def test_word_lines_break_ties_toward_the_earlier_edge() -> None:
    lines = (timing_truth.TruthLine("a", "말", F(10), F(12)),)
    edges = ((F(19, 2), F(10)), (F(21, 2), F(11)))
    (moved,) = timing_truth.word_lines(lines, edges, reach=F(1))
    assert moved.start == F(19, 2)


def _report_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("timing_truth_report", REPORT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_report_reads_word_edges_on_the_common_clock(tmp_path: Path) -> None:
    _stage1_folder(tmp_path, [{"word": "a", "start": 1.25, "end": 2.0}])
    transcript = {
        "source_sha256": "f" * 64,
        "utterances": [
            {"utterance_id": "u1", "text": "a", "start": "181", "end": "182"},
        ],
    }
    report = _report_module()
    moved = report.word_lines_of(tmp_path, "src", transcript)
    # PCM 원점 1 − 제시 원점 1/2 = 공통 시계 +1/2 → 단어 [181.75, 182.5]
    assert moved == (timing_truth.TruthLine("u1", "a", F(727, 4), F(365, 2)),)


def test_the_report_names_a_video_without_word_times(tmp_path: Path) -> None:
    _stage1_folder(tmp_path, [])
    report = _report_module()
    transcript = {"source_sha256": "f" * 64, "utterances": []}
    assert report.word_lines_of(tmp_path, "src", transcript) == "단어 시각 없음"


def test_malformed_record_words_drop_only_the_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """단어 칸이 깨져도 1단계 기록(자막 시각 확인의 재료)은 그대로 읽힌다."""
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    _stage1_folder(tmp_path, [{"word": "a", "start": "x", "end": 2.0}])
    stage1_audio = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert stage1_audio is not None and stage1_audio.chunk_starts == (F(0), F(180))
    assert stage1_audio.word_edges == ()
    assert any("단어" in warning for warning in warnings)
