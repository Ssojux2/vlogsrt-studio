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
    parse_text,
    save_fixture,
)

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"

# 이 모델이 주는 것은 글자뿐이다. 시각은 spans에서 온다(D24).
RAW = {"text": "첫 번째 두 번째", "usage": {}}
SPANS = ((0.0, 1.0), (1.5, 2.5))


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
    assert responses[0].payload["text"]


def test_the_fixture_carries_the_spans_that_make_the_times() -> None:
    """오프라인 실행의 자막 시각이 전부 여기서 나온다. 응답만으로는 재현할 수 없다."""
    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job(), Config())
    assert all(response.spans for response in responses)
    assert responses[1].spans[-1] == (19.6, 24.0)


def test_live_transcriber_is_never_built_offline() -> None:
    assert not isinstance(get_transcriber(Config()), LiveTranscriber)


# ─────────────────────────────────────────────────────────────
#  응답 해석 — 글자는 모델에서, 시각은 spans에서 (규칙 1)
# ─────────────────────────────────────────────────────────────


def test_the_response_gives_letters_and_nothing_else() -> None:
    assert parse_text(RAW) == "첫 번째 두 번째"


def test_a_response_without_text_names_the_setting_to_fix() -> None:
    with pytest.raises(RuntimeError, match="transcribe_model"):
        parse_text({"segments": [], "usage": {}})


def test_the_times_come_from_the_spans_not_the_model() -> None:
    """규칙 1 — 모델은 시각을 주지 않는다. 시각은 오디오가 정한다."""
    spine = build_spine([ChunkResponse(index=0, offset=0.0, payload=RAW, spans=SPANS)])
    assert [(s.start, s.end) for s in spine] == [(0.0, 1.0), (1.5, 2.5)]


def test_spans_are_already_in_source_coordinates() -> None:
    """묶음 좌표가 아니라 원본 좌표라 재기준화할 것이 없다."""
    far = ((100.0, 101.0), (101.5, 102.5))
    spine = build_spine([ChunkResponse(index=3, offset=100.0, payload=RAW, spans=far)])
    assert [segment.start for segment in spine] == [100.0, 101.5]


def test_ids_are_global_across_chunks() -> None:
    responses = [
        ChunkResponse(index=0, offset=0.0, payload=RAW, spans=SPANS),
        ChunkResponse(index=1, offset=100.0, payload=RAW, spans=((100.0, 101.0), (101.5, 102.5))),
    ]
    spine = build_spine(responses)
    assert [segment.id for segment in spine] == [0, 1, 2, 3]
    assert spine[2].start == 100.0


def test_chunks_are_stitched_in_time_order_not_call_order() -> None:
    responses = [
        ChunkResponse(index=1, offset=100.0, payload=RAW, spans=((100.0, 101.0), (101.5, 102.5))),
        ChunkResponse(index=0, offset=0.0, payload=RAW, spans=SPANS),
    ]
    spine = build_spine(responses)
    assert [segment.start for segment in spine] == [0.0, 1.5, 100.0, 101.5]


def test_an_empty_chunk_warns_but_does_not_stop_the_run() -> None:
    """규칙 9 — 그 구간에 자막이 안 서지만 76분 실행을 통째로 죽이지는 않는다."""
    responses = [
        ChunkResponse(index=0, offset=0.0, payload=RAW, spans=SPANS),
        ChunkResponse(index=1, offset=100.0, payload={"text": "  "}, spans=((100.0, 105.0),)),
        ChunkResponse(index=2, offset=200.0, payload=RAW, spans=((200.0, 201.0), (201.5, 202.5))),
    ]
    spine = build_spine(responses)
    assert [segment.start for segment in spine] == [0.0, 1.5, 200.0, 201.5]
    assert [segment.id for segment in spine] == [0, 1, 2, 3]  # 번호에 구멍이 없다


def test_a_chunk_without_spans_contributes_nothing() -> None:
    assert build_spine([ChunkResponse(index=0, offset=0.0, payload=RAW, spans=())]) == []


def test_cost_estimate_matches_documented_ballpark() -> None:
    assert estimate_cost(300.0, Config()) == pytest.approx(0.03, abs=0.005)


def test_save_fixture_keeps_the_spans(tmp_path: Path) -> None:
    """spans 없이 저장하면 그 픽스처로는 시각을 되살릴 수 없다."""
    save_fixture(tmp_path, "demo", FIXTURE_KIND, [ChunkResponse(0, 0.0, RAW, SPANS)])
    reloaded = FixtureTranscriber(fixture_dir=tmp_path).transcribe(job(), Config())
    assert reloaded[0].payload["text"] == "첫 번째 두 번째"
    assert reloaded[0].spans == SPANS


# ─────────────────────────────────────────────────────────────
#  실제 API가 거부한 조합 (실측 400)
# ─────────────────────────────────────────────────────────────


def test_never_sends_language_and_languages_together() -> None:
    """실측 400: "The 'language' and 'languages' parameters cannot be used together." """
    from vlogsrt.transcribe import _extras

    extras = _extras(data_replace(Config(), extra_languages=("ko", "en")))
    assert "languages" in extras
    assert "language" not in extras


def test_the_default_sends_the_singular_language() -> None:
    """실측 400: "The 'languages' parameter is not supported for this model." (D23)"""
    from vlogsrt.transcribe import _extras

    extras = _extras(Config())
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


def test_the_server_is_not_asked_to_chunk_for_us(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """우리가 무음에서 직접 자른다(D24). 서버 VAD가 끼어들면 구간과 어긋난다."""
    client = FakeClient()
    _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    assert "chunking_strategy" not in client.calls[0]


def test_a_configured_chunking_strategy_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """diarize 계열로 바꾸면 이 인자가 필수가 된다 — 실측 400(D22)."""
    client = FakeClient()
    cfg = data_replace(Config(), chunking_strategy="auto")
    _live(monkeypatch, client).transcribe(real_job(tmp_path), cfg)
    assert client.calls[0]["chunking_strategy"] == "auto"


def test_chunking_strategy_survives_the_400_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """부가 인자와 함께 떨어져 나가면 재시도도 같은 이유로 400이 된다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "prompt" in kwargs:
                error = RuntimeError("Error code: 400 - unknown parameter 'prompt'")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"text": "가", "segments": []}

    client = Rejecting()
    cfg = data_replace(Config(), prompt="망원동", chunking_strategy="auto")
    _live(monkeypatch, client).transcribe(real_job(tmp_path), cfg)
    assert all("chunking_strategy" in call for call in client.calls)


def test_only_the_named_parameter_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """힌트 하나가 거부당했다고 언어 지정까지 잃을 이유가 없다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "prompt" in kwargs:
                error = RuntimeError("Error code: 400 - Unknown parameter: 'prompt'.")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"text": "가", "segments": []}

    client = Rejecting()
    cfg = data_replace(Config(), prompt="망원동")
    _live(monkeypatch, client).transcribe(real_job(tmp_path), cfg)
    assert client.calls[1]["language"] == "ko"


def test_a_rejected_languages_falls_back_to_the_singular_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """실측 400. 복수형이 막혔다고 언어 지정 자체를 잃지는 않는다(D23)."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "languages" in kwargs:
                error = RuntimeError(
                    "Error code: 400 - {'error': {'message': \"The 'languages' parameter "
                    "is not supported for this model.\", 'param': 'languages'}}"
                )
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"text": "가", "segments": []}

    client = Rejecting()
    cfg = data_replace(Config(), extra_languages=("ko", "en"))
    _live(monkeypatch, client).transcribe(real_job(tmp_path), cfg)

    assert len(client.calls) == 2
    assert client.calls[1]["language"] == "ko"
    assert "languages" not in client.calls[1]


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
            return {"text": "가", "segments": []}

    client = Rejecting()
    _live(monkeypatch, client).transcribe(real_job(tmp_path), Config())
    assert sorted(client.calls[1]) == ["file", "model", "response_format"]


def test_a_rejected_chunking_strategy_names_the_setting_to_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """부가 인자를 아무리 떼어도 낫지 않는 400이다. 고칠 곳을 짚어줘야 한다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            error = RuntimeError(
                "Error code: 400 - {'error': {'message': 'chunking_strategy is required "
                "for diarization models', 'param': 'chunking_strategy'}}"
            )
            error.status_code = 400  # type: ignore[attr-defined]
            raise error

    client = Rejecting()
    cfg = data_replace(Config(), chunking_strategy="auto")
    with pytest.raises(RuntimeError, match="chunking_strategy"):
        _live(monkeypatch, client).transcribe(real_job(tmp_path), cfg)
    assert len(client.calls) == 1  # 떼고 다시 부르며 시간을 낭비하지 않는다


def test_the_first_chunk_settles_the_arguments_for_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """조각마다 따로 400을 맞아가며 업로드를 낭비하지 않는다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "prompt" in kwargs:
                error = RuntimeError("Error code: 400 - Unknown parameter: 'prompt'.")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"text": "가", "segments": []}

    audio = tmp_path / "demo.mp3"
    audio.write_bytes(b"\x00")
    job_two = TranscriptionJob(
        stem="demo", chunks=(Chunk(audio, 0.0, 12.0, 0), Chunk(audio, 12.0, 12.0, 1))
    )
    client = Rejecting()
    cfg = data_replace(Config(), prompt="망원동")
    responses = _live(monkeypatch, client).transcribe(job_two, cfg)

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
