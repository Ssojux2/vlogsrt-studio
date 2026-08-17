"""전사 경계. 네트워크 없이 픽스처만으로 돈다."""

from __future__ import annotations

from pathlib import Path

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.transcribe import (
    FixtureTranscriber,
    LiveTranscriber,
    TranscriptionJob,
    apply_verdicts,
    build_spine,
    canonical_text,
    estimate_cost,
    get_transcriber,
    judge_verdict,
    overlap_ratio,
    parse_spine,
    parse_words,
    rebase_timestamps,
    save_fixture,
)

from .conftest import make_segment

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"

RAW = {
    "segments": [
        {"id": 0, "start": 0.0, "end": 1.0, "text": " 첫 번째"},
        {"id": 1, "start": 1.0, "end": 2.0, "text": " 두 번째"},
    ],
    "words": [
        {"word": "첫", "start": 0.0, "end": 0.4},
        {"word": "번째", "start": 0.5, "end": 1.0},
    ],
}


def job() -> TranscriptionJob:
    return TranscriptionJob(stem="demo", chunks=(Chunk(Path("demo.mp3"), 0.0, 24.0, 0),))


def test_offline_default_is_the_fixture_transcriber() -> None:
    assert type(get_transcriber(None)).__name__ == "FixtureTranscriber"


def test_online_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_transcriber(Config(), online=True)


def test_force_offline_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    assert isinstance(get_transcriber(Config(), force_offline=True), FixtureTranscriber)


def test_online_flag_overrides_offline_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """--online은 사람이 직접 친 경우뿐이라 VLOGSRT_OFFLINE=1도 넘어선다."""
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(get_transcriber(Config(), online=True), LiveTranscriber)


def test_default_path_stays_offline_even_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(get_transcriber(Config()), FixtureTranscriber)


def test_missing_fixture_says_what_to_run(tmp_path: Path) -> None:
    transcriber = FixtureTranscriber(fixture_dir=tmp_path)
    with pytest.raises(RuntimeError, match="vlogsrt record"):
        transcriber.whisper(job(), Config())


def test_fixture_whisper_round_trip() -> None:
    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).whisper(job(), Config())
    assert len(responses) == 1
    assert responses[0].payload["segments"]


def test_gpt() -> None:
    """T3.1 DoD가 이름으로 부르는 테스트다."""
    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).gpt(job(), Config())
    text = canonical_text(responses)
    assert "블루펭귄" in text
    assert "망원동" in text


def test_rebase_moves_segments_words_and_ids() -> None:
    rebased = rebase_timestamps(RAW, offset=10.0, seg_id_base=5)
    assert [s["id"] for s in rebased["segments"]] == [5, 6]
    assert [s["start"] for s in rebased["segments"]] == [10.0, 11.0]
    assert [w["start"] for w in rebased["words"]] == [10.0, 10.5]
    assert RAW["segments"][0]["start"] == 0.0  # 원본은 그대로다


def test_spine() -> None:
    """T1.4 DoD가 이름으로 부르는 테스트다."""
    spine = parse_spine(RAW)
    assert [segment.id for segment in spine] == [0, 1]
    assert all(segment.start < segment.end for segment in spine)
    assert all(segment.verdict == "ok" for segment in spine)
    assert spine[0].text == "첫 번째"


def test_spine_drops_zero_length_segments() -> None:
    raw = {"segments": [{"id": 0, "start": 1.0, "end": 1.0, "text": "x"}], "words": []}
    assert parse_spine(raw) == []


def test_multi_chunk_ids_are_global() -> None:
    from vlogsrt.transcribe import ChunkResponse

    responses = [
        ChunkResponse(index=0, offset=0.0, payload=RAW),
        ChunkResponse(index=1, offset=100.0, payload=RAW),
    ]
    spine, words = build_spine(responses)
    assert [segment.id for segment in spine] == [0, 1, 2, 3]
    assert spine[2].start == 100.0
    assert len(words) == 4


def test_parse_words_applies_offset() -> None:
    words = parse_words(RAW, offset=3.0)
    assert words[0].start == 3.0


def test_verdict() -> None:
    """T4.2 DoD가 이름으로 부르는 테스트다. 임계값은 OpenAI 문서 근거값이다."""
    cfg = Config()
    healthy = make_segment(0, 0.0, 2.0, "정상")
    assert judge_verdict(healthy, [], cfg) == "ok"

    compressed = make_segment(1, 0.0, 2.0, "반복", compression_ratio=2.81)
    assert judge_verdict(compressed, [], cfg) == "compression_fail"

    silent = make_segment(2, 0.0, 2.0, "무음", avg_logprob=-1.5, no_speech_prob=0.8)
    assert judge_verdict(silent, [], cfg) == "silent"

    noisy = make_segment(3, 0.0, 2.0, "잡음", avg_logprob=-1.5, no_speech_prob=0.1)
    assert judge_verdict(noisy, [], cfg) == "low_logprob"

    overlapped = make_segment(4, 0.0, 2.0, "무음 위")
    assert judge_verdict(overlapped, [(0.0, 1.9)], cfg) == "silent"


def test_compression_is_checked_before_logprob() -> None:
    cfg = Config()
    both = make_segment(0, 0.0, 2.0, "x", avg_logprob=-1.5, compression_ratio=3.0)
    assert judge_verdict(both, [], cfg) == "compression_fail"


def test_overlap_ratio_is_bounded() -> None:
    segment = make_segment(0, 0.0, 2.0, "x")
    assert overlap_ratio(segment, [(0.0, 10.0)]) == 1.0
    assert overlap_ratio(segment, []) == 0.0


def test_apply_verdicts_returns_new_objects() -> None:
    spine = [make_segment(0, 0.0, 2.0, "x", compression_ratio=3.0)]
    judged = apply_verdicts(spine, [], Config())
    assert judged[0].verdict == "compression_fail"
    assert spine[0].verdict == "ok"  # 골격은 그대로다


def test_cost_estimate_matches_documented_ballpark() -> None:
    assert estimate_cost(300.0, Config()) == pytest.approx(0.06, abs=0.005)


def test_save_fixture_writes_chunk_shape(tmp_path: Path) -> None:
    from vlogsrt.transcribe import ChunkResponse

    path = save_fixture(tmp_path, "demo", "whisper", [ChunkResponse(0, 0.0, RAW)])
    reloaded = FixtureTranscriber(fixture_dir=tmp_path).whisper(job(), Config())
    assert path.is_file()
    assert reloaded[0].payload["segments"][0]["text"] == " 첫 번째"


def test_live_transcriber_is_never_built_offline() -> None:
    assert not isinstance(get_transcriber(Config()), LiveTranscriber)


# ─────────────────────────────────────────────────────────────
#  실제 API가 거부한 조합 (실측 400)
# ─────────────────────────────────────────────────────────────


def test_gpt_never_sends_language_and_languages_together() -> None:
    """`language`와 `languages`를 함께 보내면 API가 400을 돌려준다.

    TASKS.md §T3.1은 둘 다 넘기라고 적어두었지만 실측은 이렇게 답했다:
      "The 'language' and 'languages' parameters cannot be used together."
    """
    from vlogsrt.transcribe import _gpt_extras

    extras = _gpt_extras(Config())
    assert "languages" in extras
    assert "language" not in extras


def test_gpt_falls_back_to_single_language() -> None:
    from dataclasses import replace as data_replace

    from vlogsrt.transcribe import _gpt_extras

    extras = _gpt_extras(data_replace(Config(), extra_languages=()))
    assert extras["language"] == "ko"
    assert "languages" not in extras
