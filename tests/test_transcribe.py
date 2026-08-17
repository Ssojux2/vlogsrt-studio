"""전사 경계. 네트워크 없이 픽스처만으로 돈다."""

from __future__ import annotations

from dataclasses import replace as data_replace
from pathlib import Path

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.transcribe import (
    FIXTURE_KIND,
    ChunkResponse,
    FixtureTranscriber,
    LiveTranscriber,
    TranscriptionJob,
    build_spine,
    estimate_cost,
    get_transcriber,
    parse_segments,
    save_fixture,
)

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"

RAW = {
    "task": "transcribe",
    "segments": [
        {"id": "seg_0", "start": 0.0, "end": 1.0, "text": " 첫 번째", "speaker": "A"},
        {"id": "seg_1", "start": 1.0, "end": 2.0, "text": " 두 번째", "speaker": "B"},
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
        transcriber.transcribe(job(), Config())


def test_fixture_round_trip() -> None:
    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job(), Config())
    assert len(responses) == 2
    assert responses[0].payload["segments"]


def test_live_transcriber_is_never_built_offline() -> None:
    assert not isinstance(get_transcriber(Config()), LiveTranscriber)


# ─────────────────────────────────────────────────────────────
#  응답 해석 — 시각과 글자가 여기서만 나온다 (규칙 1)
# ─────────────────────────────────────────────────────────────


def test_segments_carry_their_own_times_and_text() -> None:
    spine = parse_segments(RAW)
    assert [(s.start, s.end) for s in spine] == [(0.0, 1.0), (1.0, 2.0)]
    assert spine[0].text == "첫 번째"


def test_ids_are_ours_not_the_responses() -> None:
    """diarize는 `"seg_0"` 같은 문자열 id를 청크마다 0부터 다시 준다."""
    spine = parse_segments(RAW, offset=0.0, seg_id_base=5)
    assert [segment.id for segment in spine] == [5, 6]


def test_offset_moves_segments_into_source_coordinates() -> None:
    spine = parse_segments(RAW, offset=10.0)
    assert [segment.start for segment in spine] == [10.0, 11.0]
    assert RAW["segments"][0]["start"] == 0.0  # 원본은 그대로다


def test_zero_length_and_empty_segments_are_dropped() -> None:
    raw = {
        "segments": [
            {"start": 1.0, "end": 1.0, "text": "길이 0"},
            {"start": 2.0, "end": 3.0, "text": "   "},
            {"start": 4.0, "end": 5.0, "text": "남는다"},
        ]
    }
    spine = parse_segments(raw)
    assert [segment.text for segment in spine] == ["남는다"]
    assert spine[0].id == 0  # 버린 만큼 번호가 비지 않는다


def test_a_response_without_segments_names_the_setting_to_fix() -> None:
    """gpt-transcribe(비 diarize)는 text만 준다. 그 응답으로는 SRT를 못 만든다(D21)."""
    with pytest.raises(RuntimeError, match="transcribe_model"):
        parse_segments({"text": "시각이 없는 덩어리", "usage": {}})


def test_multi_chunk_ids_are_global_and_times_are_rebased() -> None:
    responses = [
        ChunkResponse(index=0, offset=0.0, payload=RAW),
        ChunkResponse(index=1, offset=100.0, payload=RAW),
    ]
    spine = build_spine(responses)
    assert [segment.id for segment in spine] == [0, 1, 2, 3]
    assert spine[2].start == 100.0


def test_chunks_are_stitched_in_time_order_not_call_order() -> None:
    responses = [
        ChunkResponse(index=1, offset=100.0, payload=RAW),
        ChunkResponse(index=0, offset=0.0, payload=RAW),
    ]
    spine = build_spine(responses)
    assert [segment.start for segment in spine] == [0.0, 1.0, 100.0, 101.0]


def test_cost_estimate_matches_documented_ballpark() -> None:
    assert estimate_cost(300.0, Config()) == pytest.approx(0.03, abs=0.005)


def test_save_fixture_writes_chunk_shape(tmp_path: Path) -> None:
    path = save_fixture(tmp_path, "demo", FIXTURE_KIND, [ChunkResponse(0, 0.0, RAW)])
    reloaded = FixtureTranscriber(fixture_dir=tmp_path).transcribe(job(), Config())
    assert path.is_file()
    assert reloaded[0].payload["segments"][0]["text"] == " 첫 번째"


# ─────────────────────────────────────────────────────────────
#  실제 API가 거부한 조합 (실측 400)
# ─────────────────────────────────────────────────────────────


def test_never_sends_language_and_languages_together() -> None:
    """실측 400: "The 'language' and 'languages' parameters cannot be used together." """
    from vlogsrt.transcribe import _extras

    extras = _extras(Config())
    assert "languages" in extras
    assert "language" not in extras


def test_falls_back_to_single_language() -> None:
    from vlogsrt.transcribe import _extras

    extras = _extras(data_replace(Config(), extra_languages=()))
    assert extras["language"] == "ko"
    assert "languages" not in extras


def test_empty_hint_sends_no_prompt_at_all() -> None:
    from vlogsrt.transcribe import _extras

    assert "prompt" not in _extras(Config())


# ─────────────────────────────────────────────────────────────
#  실제 호출 인자 (네트워크 없이 클라이언트만 가짜로)
# ─────────────────────────────────────────────────────────────


class FakeClient:
    """`audio.transcriptions.create` 호출 인자만 기록한다. 네트워크 없음."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.audio = self
        self.transcriptions = self

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"text": "가", "segments": []}


def real_job(tmp_path: Path) -> TranscriptionJob:
    """`chunk.path.open("rb")` 때문에 실제 파일이 필요하다. 내용은 상관없다."""
    audio = tmp_path / "demo.mp3"
    audio.write_bytes(b"\x00")
    return TranscriptionJob(stem="demo", chunks=(Chunk(audio, 0.0, 24.0, 0),))


def _live(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> LiveTranscriber:
    """`LiveTranscriber`는 frozen+slots라 인스턴스에 못 꽂는다. 클래스에 꽂는다."""
    monkeypatch.setattr(LiveTranscriber, "_client", lambda self: client)
    return LiveTranscriber(api_key="sk-test")


def test_the_call_asks_for_a_format_that_carries_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient()
    _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    (call,) = client.calls
    assert call["response_format"] == Config().transcribe_format
    assert call["model"] == Config().transcribe_model
    assert call["temperature"] == 0


def test_the_hint_goes_to_the_model_that_makes_the_subtitles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient()
    cfg = data_replace(Config(), prompt="망원동, 아페로, 코나")
    _live(monkeypatch, client).transcribe(real_job(tmp_path), cfg)
    (call,) = client.calls
    assert call["prompt"] == "망원동, 아페로, 코나"


def test_retries_without_extras_on_400(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """미검증 모델이 부가 인자를 거부해도 전사 자체는 살린다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "prompt" in kwargs:
                error = RuntimeError("Error code: 400 - unknown parameter 'prompt'")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"text": "가", "segments": []}

    client = Rejecting()
    live = _live(monkeypatch, client)
    responses = live.transcribe(real_job(tmp_path), data_replace(Config(), prompt="망원동"))

    assert len(client.calls) == 2
    assert "prompt" in client.calls[0] and "prompt" not in client.calls[1]
    assert responses[0].payload["text"] == "가"


def test_other_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Broken(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            error = RuntimeError("Error code: 401 - bad key")
            error.status_code = 401  # type: ignore[attr-defined]
            raise error

    client = Broken()
    with pytest.raises(RuntimeError, match="401"):
        _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
