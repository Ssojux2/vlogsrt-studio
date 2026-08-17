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

# whisper가 시각과 글자를 모두 준다. 우리가 하는 계산은 `+ offset`뿐이다(D25).
RAW = {
    "task": "transcribe",
    "segments": [
        {"id": 0, "start": 0.0, "end": 1.0, "text": " 첫 번째"},
        {"id": 1, "start": 1.5, "end": 2.5, "text": " 두 번째"},
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
    assert responses[1].offset == 12.0


def test_live_transcriber_is_never_built_offline() -> None:
    assert not isinstance(get_transcriber(Config()), LiveTranscriber)


# ─────────────────────────────────────────────────────────────
#  응답 해석 — 시각은 whisper 값 + 오프셋. 그게 전부다 (규칙 1)
# ─────────────────────────────────────────────────────────────


def test_segments_carry_their_own_times_and_text() -> None:
    spine = parse_segments(RAW)
    assert [(s.start, s.end) for s in spine] == [(0.0, 1.0), (1.5, 2.5)]
    assert spine[0].text == "첫 번째"


def test_the_offset_is_the_only_arithmetic_on_time() -> None:
    """조각 좌표 → 원본 좌표. 유도도 배분도 없다(D25)."""
    spine = parse_segments(RAW, offset=100.0)
    assert [(s.start, s.end) for s in spine] == [(100.0, 101.0), (101.5, 102.5)]
    assert RAW["segments"][0]["start"] == 0.0  # 원본은 그대로다


def test_ids_are_ours_not_the_responses() -> None:
    """조각마다 0부터 다시 세면 seg_ids로 역추적이 안 된다(규칙 2)."""
    spine = parse_segments(RAW, offset=0.0, seg_id_base=5)
    assert [segment.id for segment in spine] == [5, 6]


def test_no_spoken_word_is_ever_thrown_away() -> None:
    """길이 0이든 시각이 뒤집혔든, 글자가 있으면 남는다(규칙 9)."""
    raw = {
        "segments": [
            {"start": 1.0, "end": 1.0, "text": "길이 0"},
            {"start": 4.0, "end": 3.0, "text": "뒤집힌 시각"},
            {"start": 5.0, "end": 6.0, "text": "멀쩡한 것"},
        ]
    }
    spine = parse_segments(raw)
    assert [segment.text for segment in spine] == ["길이 0", "뒤집힌 시각", "멀쩡한 것"]
    assert spine[1].end == 4.0  # 뒤집힌 끝은 시작으로 접는다. 글자는 그대로다


def test_only_letterless_segments_are_dropped() -> None:
    raw = {"segments": [{"start": 2.0, "end": 3.0, "text": "   "}, *RAW["segments"]]}
    spine = parse_segments(raw)
    assert [segment.text for segment in spine] == ["첫 번째", "두 번째"]
    assert spine[0].id == 0  # 버린 만큼 번호가 비지 않는다


def test_a_response_without_segments_names_the_setting_to_fix() -> None:
    with pytest.raises(RuntimeError, match="transcribe_model"):
        parse_segments({"text": "시각이 없는 덩어리", "usage": {}})


def test_ids_are_global_across_chunks() -> None:
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
    assert [segment.start for segment in spine] == [0.0, 1.5, 100.0, 101.5]


def test_an_empty_chunk_warns_but_does_not_stop_the_run() -> None:
    """규칙 9 — 그 구간에 자막이 안 서지만 긴 실행을 통째로 죽이지는 않는다."""
    responses = [
        ChunkResponse(index=0, offset=0.0, payload=RAW),
        ChunkResponse(index=1, offset=100.0, payload={"segments": []}),
        ChunkResponse(index=2, offset=200.0, payload=RAW),
    ]
    spine = build_spine(responses)
    assert [segment.start for segment in spine] == [0.0, 1.5, 200.0, 201.5]
    assert [segment.id for segment in spine] == [0, 1, 2, 3]  # 번호에 구멍이 없다


def test_cost_estimate_matches_documented_ballpark() -> None:
    assert estimate_cost(300.0, Config()) == pytest.approx(0.03, abs=0.005)


def test_save_fixture_writes_chunk_shape(tmp_path: Path) -> None:
    save_fixture(tmp_path, "demo", FIXTURE_KIND, [ChunkResponse(0, 0.0, RAW)])
    reloaded = FixtureTranscriber(fixture_dir=tmp_path).transcribe(job(), Config())
    assert reloaded[0].payload["segments"][0]["text"] == " 첫 번째"


# ─────────────────────────────────────────────────────────────
#  실제 API가 거부한 조합 (실측 400)
# ─────────────────────────────────────────────────────────────


def test_the_call_carries_the_language() -> None:
    from vlogsrt.transcribe import _extras

    assert _extras(Config())["language"] == "ko"


def test_the_hint_never_reaches_whisper() -> None:
    """규칙 6 — 한 줄이 '한국 여행 브이로그' 138회·564초를 낳았다(D13, D16)."""
    from vlogsrt.transcribe import _extras

    extras = _extras(data_replace(Config(), prompt="망원동, 아페로", keywords=("블루펭귄",)))
    assert "prompt" not in extras
    assert "keywords" not in extras


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
        return {"segments": []}


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
    # 값이 아니라 배선을 본다. 온도는 되풀이를 막는 손잡이라 바뀔 수 있다(D28).
    assert call["temperature"] == Config().transcribe_temperature


def test_the_hint_is_not_in_the_actual_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """규칙 6이 설정에서만이 아니라 실제 호출 인자에서도 지켜지는지 본다."""
    client = FakeClient()
    cfg = data_replace(Config(), prompt="망원동, 아페로, 코나", keywords=("블루펭귄",))
    _live(monkeypatch, client).transcribe(real_job(tmp_path), cfg)
    (call,) = client.calls
    assert "prompt" not in call
    assert "keywords" not in call


def test_retries_without_extras_on_400(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """부가 인자를 거부해도 전사 자체는 살린다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "language" in kwargs:
                error = RuntimeError("Error code: 400 - unknown parameter 'language'")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"segments": []}

    client = Rejecting()
    live = _live(monkeypatch, client)
    live.transcribe(real_job(tmp_path), Config())

    assert len(client.calls) == 2
    assert "language" in client.calls[0] and "language" not in client.calls[1]


def test_the_model_and_format_that_carry_times_are_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """400을 맞아도 이 둘은 떨어져 나가지 않는다 — 빠지면 재시도도 같은 이유로 실패한다."""
    client = FakeClient()
    _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    (call,) = client.calls
    assert call["model"] == Config().transcribe_model
    assert call["response_format"] == Config().transcribe_format


def test_the_required_arguments_survive_the_400_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "language" in kwargs:
                error = RuntimeError("Error code: 400 - unknown parameter 'language'")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"segments": []}

    client = Rejecting()
    _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    assert all("model" in call and "response_format" in call for call in client.calls)


def test_only_the_named_parameter_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """인자 하나가 거부당했다고 나머지까지 잃을 이유가 없다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "temperature" in kwargs:
                error = RuntimeError("Error code: 400 - Unknown parameter: 'temperature'.")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"segments": []}

    client = Rejecting()
    _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    assert client.calls[1]["language"] == "ko"


def test_an_unnamed_400_drops_every_optional_parameter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                error = RuntimeError("Error code: 400 - 뭐가 문제인지 말해주지 않는다")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"segments": []}

    client = Rejecting()
    _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    assert sorted(client.calls[1]) == ["file", "model", "response_format"]


def test_a_rejected_response_format_names_the_setting_to_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """부가 인자를 아무리 떼어도 낫지 않는 400이다. 고칠 곳을 짚어줘야 한다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            error = RuntimeError(
                "Error code: 400 - {'error': {'message': 'response_format not supported', "
                "'param': 'response_format'}}"
            )
            error.status_code = 400  # type: ignore[attr-defined]
            raise error

    client = Rejecting()
    with pytest.raises(RuntimeError, match="response_format"):
        _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    assert len(client.calls) == 1  # 떼고 다시 부르며 시간을 낭비하지 않는다


def test_the_first_chunk_settles_the_arguments_for_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """조각마다 따로 400을 맞아가며 업로드를 낭비하지 않는다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "temperature" in kwargs:
                error = RuntimeError("Error code: 400 - Unknown parameter: 'temperature'.")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"segments": []}

    audio = tmp_path / "demo.mp3"
    audio.write_bytes(b"\x00")
    job_two = TranscriptionJob(
        stem="demo", chunks=(Chunk(audio, 0.0, 12.0, 0), Chunk(audio, 12.0, 12.0, 1))
    )
    client = Rejecting()
    responses = _live(monkeypatch, client).transcribe(job_two, Config())

    assert len(responses) == 2
    assert [r.offset for r in responses] == [0.0, 12.0]
    assert len(client.calls) == 3  # 첫 조각이 두 번, 둘째 조각은 이미 정리된 인자로 한 번


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
