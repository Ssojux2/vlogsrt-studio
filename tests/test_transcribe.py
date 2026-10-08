"""전사 경계. 네트워크 없이 픽스처만으로 돈다."""

from __future__ import annotations

import json
import os
import sys
import types
from collections.abc import Callable, Sequence
from dataclasses import replace as data_replace
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import env
from vlogsrt import transcribe as transcribe_module
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.transcribe import (
    CAPTION_TEXT_KIND,
    CAPTION_TIMING_KIND,
    FIXTURE_KIND,
    TRANSCRIPTION_OPERATION,
    AudioTranscriptionRequest,
    ChunkCoverage,
    ChunkResponse,
    ClosedVisionResponder,
    EmptyTranscript,
    FixtureTranscriber,
    FixtureVisionResponder,
    LiveTranscriber,
    LiveVisionResponder,
    MalformedTranscript,
    MissingVisionFixture,
    TranscriptionJob,
    UnsupportedTranscriptionRequest,
    build_spine,
    create_vision_client,
    estimate_cost,
    get_transcriber,
    parse_segments,
    request_luna_vision,
    request_vision,
    require_nonempty,
    save_fixture,
    transcribe_outcome,
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


def _live(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeClient,
    cancellation: Callable[[], None] | None = None,
) -> LiveTranscriber:
    """`LiveTranscriber`는 frozen+slots라 인스턴스에 못 꽂는다. 클래스에 꽂는다."""
    monkeypatch.setattr(LiveTranscriber, "_client", lambda self: client)
    return LiveTranscriber(api_key="sk-test", cancellation=cancellation)


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


# ─────────────────────────────────────────────────────────────
#  호출 직전 취소 게이트 (S1A) — 조각·재시도·거부 재요청마다 지난다
# ─────────────────────────────────────────────────────────────


class Cancelled(BaseException):
    """프로젝트 계층의 WorkflowCancelled와 같은 부류 — `except Exception`에 잡히지 않는다."""


class Throttled(FakeClient):
    """429로 답한다 — 재시도 판단을 유도한다."""

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        error = RuntimeError("Error code: 429 - rate limited")
        error.status_code = 429  # type: ignore[attr-defined]
        raise error


def _gate_after(limit: int) -> tuple[list[int], Callable[[], None]]:
    """`limit`번 확인 뒤 취소되는 게이트 — 확인 횟수를 세서 돌려준다."""
    checks: list[int] = []

    def gate() -> None:
        checks.append(len(checks) + 1)
        if len(checks) > limit:
            raise Cancelled

    return checks, gate


def multi_job(tmp_path: Path) -> TranscriptionJob:
    audio = tmp_path / "demo.mp3"
    audio.write_bytes(b"\x00")
    return TranscriptionJob(
        stem="demo",
        chunks=(
            Chunk(audio, 0.0, 24.0, 0),
            Chunk(audio, 24.0, 24.0, 1),
            Chunk(audio, 48.0, 24.0, 2),
        ),
    )


def test_cancellation_gate_blocks_the_transient_retry_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """429 뒤의 재시도 직전에 취소가 보이면 재요청이 없다."""
    client = Throttled()
    _, gate = _gate_after(1)
    live = _live(monkeypatch, client, gate)
    cfg = data_replace(Config(), api_backoff_base=0.0)  # 대기 없이 즉시 재시도한다

    with pytest.raises(Cancelled):
        live.transcribe(real_job(tmp_path), cfg)
    assert len(client.calls) == 1  # 첫 시도만 — 재시도 요청은 없다


def test_cancellation_gate_blocks_later_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """둘째 조각 직전에 취소가 보이면 그 조각 요청이 없다."""
    monkeypatch.setattr(env, "max_workers", lambda: 1)  # 조각을 한 줄로 세워 순서를 잡는다
    client = FakeClient()
    _, gate = _gate_after(1)
    live = _live(monkeypatch, client, gate)

    with pytest.raises(Cancelled):
        live.transcribe(multi_job(tmp_path), Config())
    assert len(client.calls) == 1  # 첫 조각만 — 둘째·셋째 요청은 없다


def test_cancellation_gate_blocks_the_rejected_parameter_reak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """400으로 부가 인자를 떼고 다시 부르는 재요청도 게이트를 지난다."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if "language" in kwargs:
                error = RuntimeError("Error code: 400 - unknown parameter 'language'")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            return {"segments": []}

    client = Rejecting()
    _, gate = _gate_after(1)
    live = _live(monkeypatch, client, gate)

    with pytest.raises(Cancelled):
        live.transcribe(real_job(tmp_path), Config())
    assert len(client.calls) == 1  # 거부당한 원요청만 — 떼고 다시 부르는 요청은 없다


def test_cancellation_is_not_swallowed_as_a_transcription_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """게이트의 취소는 실패 상태 기록이 아니다 — 결과형 안에서 사라지지 않는다."""
    client = Throttled()
    _, gate = _gate_after(1)
    live = _live(monkeypatch, client, gate)
    cfg = data_replace(Config(), api_backoff_base=0.0)

    # transcribe_outcome의 `except Exception`은 상태 기록으로 바꿔 삼키는 층이다 —
    # 취소(BaseException)는 여기를 뚫고 올라가야 실행 전체가 cancelled로 닫힌다.
    with pytest.raises(Cancelled):
        transcribe_outcome(live, real_job(tmp_path), cfg)


def test_get_transcriber_routes_the_gate_to_the_live_path_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def gate() -> None:
        raise Cancelled

    live = get_transcriber(Config(), online=True, cancellation=gate)
    assert isinstance(live, LiveTranscriber)
    assert live.cancellation is gate
    # 픽스처 경로는 외부 호출이 없다 — 게이트를 물려주지 않는다.
    assert isinstance(get_transcriber(None, cancellation=gate), FixtureTranscriber)


def test_stt_client_pins_the_sdk_hidden_retries_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 기본 재시도(2)는 게이트 밖에서 조용히 재호출한다 — 재시도는 _retry가 한다.

    실제 SDK로 생성만 증명한다(호출은 없다). 비전 클라이언트와 같은 규율이다.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert LiveTranscriber(api_key="sk-test")._client().max_retries == 0
    proxy = LiveTranscriber(api_key="sk-test", base_url="https://stt.example/v1")
    assert proxy._client().max_retries == 0


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


# ─────────────────────────────────────────────────────────────
#  전사 결과형 — 상태는 셋, 조립은 기존 build_spine 그대로 (P3-01)
# ─────────────────────────────────────────────────────────────

EMPTY = {"segments": []}
BLANK = {"segments": [{"start": 0.0, "end": 1.0, "text": "   "}]}
LEGACY_RANGE = {
    "segments": [
        {"start": 1.0, "end": 1.0, "text": "길이 0"},
        {"start": 4.0, "end": 3.0, "text": "뒤집힌 시각"},
    ]
}


class Scripted:
    """정해둔 응답을 그대로 돌려주는 가짜 전사자. 예외도 낼 수 있다."""

    def __init__(
        self, responses: Sequence[ChunkResponse] = (), failure: Exception | None = None
    ) -> None:
        self.responses = list(responses)
        self.failure = failure
        self.calls = 0

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.responses


def chunk_job(offsets: Sequence[float] = (0.0, 12.0)) -> TranscriptionJob:
    audio = Path("demo.mp3")
    return TranscriptionJob(
        stem="demo",
        chunks=tuple(Chunk(audio, offset, 12.0, index) for index, offset in enumerate(offsets)),
    )


def responses_for(job: TranscriptionJob, payloads: Sequence[dict[str, Any]]) -> list[ChunkResponse]:
    """작업의 조각마다 순서대로 응답 하나씩."""
    return [
        ChunkResponse(chunk.index, chunk.offset, payload)
        for chunk, payload in zip(job.chunks, payloads, strict=True)
    ]


def test_all_lists_literally_empty_is_the_explicit_empty_outcome() -> None:
    job = chunk_job()
    outcome = transcribe_outcome(Scripted(responses_for(job, [EMPTY, EMPTY])), job, Config())
    assert outcome.status == "empty"
    assert outcome.segments == ()
    assert outcome.error is None
    assert outcome.coverage == (
        ChunkCoverage(index=0, offset=0.0, duration=12.0, segment_count=0),
        ChunkCoverage(index=1, offset=12.0, duration=12.0, segment_count=0),
    )


def test_a_missing_segments_key_is_an_error_never_empty() -> None:
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(
        Scripted([ChunkResponse(0, 0.0, {"text": "시각이 없는 덩어리"})]), job, Config()
    )
    assert outcome.status == "error"
    assert outcome.segments == ()
    assert outcome.coverage == ()
    assert "segments" in (outcome.error or "")


def test_segments_that_is_not_a_list_is_an_error() -> None:
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(
        Scripted([ChunkResponse(0, 0.0, {"segments": {"0": []}})]), job, Config()
    )
    assert outcome.status == "error"


def test_a_payload_that_is_not_an_object_is_an_error() -> None:
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(Scripted([ChunkResponse(0, 0.0, ["segments"])]), job, Config())
    assert outcome.status == "error"


def test_a_segment_that_is_not_an_object_is_an_error() -> None:
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(
        Scripted([ChunkResponse(0, 0.0, {"segments": ["첫 번째"]})]), job, Config()
    )
    assert outcome.status == "error"


@pytest.mark.parametrize(
    "segment",
    [
        pytest.param({"end": 1.0, "text": "가"}, id="start-missing"),
        pytest.param({"start": 0.0, "text": "가"}, id="end-missing"),
        pytest.param({"start": "0.0", "end": 1.0, "text": "가"}, id="start-string"),
        pytest.param({"start": 0.0, "end": None, "text": "가"}, id="end-null"),
        pytest.param({"start": float("inf"), "end": 1.0, "text": "가"}, id="start-inf"),
        pytest.param({"start": 0.0, "end": float("nan"), "text": "가"}, id="end-nan"),
        pytest.param({"start": True, "end": 1.0, "text": "가"}, id="start-bool"),
        pytest.param(
            {"start": 0.0, "end": 1.0, "text": "가", "compression_ratio": True}, id="meta-bool"
        ),
    ],
)
def test_a_broken_segment_number_is_an_error_never_empty(segment: dict[str, Any]) -> None:
    """build_spine가 기본값으로 덮던 값들도 결과형에서는 있어야 한다."""
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(
        Scripted([ChunkResponse(0, 0.0, {"segments": [segment]})]), job, Config()
    )
    assert outcome.status == "error"


def test_integer_segment_numbers_are_valid() -> None:
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(
        Scripted([ChunkResponse(0, 0.0, {"segments": [{"start": 0, "end": 2, "text": " 가"}]})]),
        job,
        Config(),
    )
    assert outcome.status == "success"
    assert [segment.text for segment in outcome.segments] == ["가"]


def test_raw_nonempty_but_blank_text_is_a_success_with_no_segments() -> None:
    """조립 결과가 비는 것과 응답이 비는 것은 다르다 — 전자는 성공이다."""
    job = chunk_job()
    outcome = transcribe_outcome(Scripted(responses_for(job, [BLANK, BLANK])), job, Config())
    assert outcome.status == "success"
    assert outcome.segments == ()
    assert [cover.segment_count for cover in outcome.coverage] == [1, 1]


def test_mixed_empty_and_nonempty_lists_are_a_success() -> None:
    job = chunk_job()
    outcome = transcribe_outcome(Scripted(responses_for(job, [EMPTY, RAW])), job, Config())
    assert outcome.status == "success"
    assert [segment.start for segment in outcome.segments] == [12.0, 13.5]  # RAW + 조각 오프셋
    assert outcome.coverage[0].segment_count == 0


def test_zero_and_reversed_ranges_survive_through_unchanged_build_spine() -> None:
    """길이 0·뒤집힌 시각은 계약 위반이 아니다 — 기존 접힘 그대로 흘러간다(규칙 9)."""
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(Scripted([ChunkResponse(0, 0.0, LEGACY_RANGE)]), job, Config())
    assert outcome.status == "success"
    assert [segment.text for segment in outcome.segments] == ["길이 0", "뒤집힌 시각"]
    assert outcome.segments[1].end == 4.0  # 뒤집힌 끝은 시작으로 접는다 — 기존과 같다


@pytest.mark.parametrize(
    "responses",
    [
        pytest.param([ChunkResponse(0, 0.0, RAW)], id="count-short"),
        pytest.param([ChunkResponse(2, 24.0, RAW), ChunkResponse(0, 0.0, RAW)], id="unrequested"),
        pytest.param([ChunkResponse(True, 0.0, RAW), ChunkResponse(1, 12.0, RAW)], id="bool-index"),
        pytest.param([ChunkResponse(1.0, 12.0, RAW), ChunkResponse(0, 0.0, RAW)], id="float-index"),
        pytest.param(
            [ChunkResponse(0, 5.0, RAW), ChunkResponse(1, 12.0, RAW)], id="offset-mismatch"
        ),
        pytest.param([ChunkResponse(0, True, RAW), ChunkResponse(1, 12.0, RAW)], id="bool-offset"),
        pytest.param(
            [ChunkResponse(0, float("nan"), RAW), ChunkResponse(1, 12.0, RAW)], id="nan-offset"
        ),
        pytest.param([ChunkResponse(0, 0.0, RAW), ChunkResponse(0, 0.0, RAW)], id="duplicate"),
        pytest.param(
            [ChunkResponse(0, 0.0, RAW), ChunkResponse(1, 12.0, RAW), ChunkResponse(2, 24.0, RAW)],
            id="count-long",
        ),
    ],
)
def test_a_broken_response_identity_is_an_error(responses: list[ChunkResponse]) -> None:
    """덮개 검사가 느슨하면 조각이 몰래 빠져도 조립은 그대로 돌아간다."""
    job = chunk_job()
    outcome = transcribe_outcome(Scripted(responses), job, Config())
    assert outcome.status == "error"
    assert outcome.segments == ()


def test_a_call_failure_is_an_error_outcome_not_a_raise() -> None:
    job = chunk_job((0.0,))
    failure = RuntimeError("Error code: 401 - bad key")
    outcome = transcribe_outcome(Scripted(failure=failure), job, Config())
    assert outcome.status == "error"
    assert "401" in (outcome.error or "")
    assert outcome.coverage == ()


# ── 수정(Sol 1): 요청 가드 — 깨진 요청은 위임조차 하지 않는다 ────────────────


@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param((), id="no-chunks"),
        pytest.param(
            (
                Chunk(Path("demo.mp3"), 0.0, 12.0, 0),
                Chunk(Path("demo.mp3"), 0.0, 12.0, 0),
            ),
            id="duplicate-index",
        ),
        pytest.param((Chunk(Path("demo.mp3"), 0.0, 12.0, True),), id="bool-index"),
        pytest.param((Chunk(Path("demo.mp3"), 0.0, 12.0, 1.0),), id="float-index"),
        pytest.param((Chunk(Path("demo.mp3"), float("nan"), 12.0, 0),), id="nan-offset"),
        pytest.param((Chunk(Path("demo.mp3"), True, 12.0, 0),), id="bool-offset"),
        pytest.param((Chunk(Path("demo.mp3"), 0.0, 0.0, 0),), id="zero-duration"),
        pytest.param((Chunk(Path("demo.mp3"), 0.0, -5.0, 0),), id="negative-duration"),
        pytest.param((Chunk(Path("demo.mp3"), 0.0, float("nan"), 0),), id="nan-duration"),
    ],
)
def test_a_broken_request_is_rejected_without_a_single_call(
    chunks: tuple[Chunk, ...],
) -> None:
    """유료 호출은 깨진 요청에 값을 쓰지 않는다 — 검사는 위임 앞에서 끝난다."""
    transcriber = Scripted()
    outcome = transcribe_outcome(
        transcriber, TranscriptionJob(stem="demo", chunks=chunks), Config()
    )
    assert outcome.status == "error"
    assert outcome.segments == ()
    assert outcome.coverage == ()
    assert transcriber.calls == 0


def test_a_duplicated_chunk_index_never_duplicates_captions() -> None:
    """같은 조각 번호 둘에 응답 하나가 오면 같은 자막이 두 번 서던 길이다."""
    audio = Path("demo.mp3")
    job = TranscriptionJob(
        stem="demo",
        chunks=(Chunk(audio, 0.0, 12.0, 0), Chunk(audio, 0.0, 12.0, 0)),
    )
    transcriber = Scripted([ChunkResponse(0, 0.0, RAW)])
    outcome = transcribe_outcome(transcriber, job, Config())
    assert outcome.status == "error"
    assert transcriber.calls == 0


# ── 수정(Sol 2): 세그먼트 글자는 명시적인 문자열이어야 한다 ──────────────────


@pytest.mark.parametrize(
    "segment",
    [
        pytest.param({"start": 0.0, "end": 1.0}, id="text-missing"),
        pytest.param({"start": 0.0, "end": 1.0, "text": {"unexpected": 1}}, id="text-object"),
        pytest.param({"start": 0.0, "end": 1.0, "text": 3}, id="text-number"),
        pytest.param({"start": 0.0, "end": 1.0, "text": True}, id="text-bool"),
    ],
)
def test_missing_or_non_string_text_is_malformed(segment: dict[str, Any]) -> None:
    """str() 강제로 글자모양을 만들어 내는 길을 막는다 — 글자는 문자열로 와야 한다."""
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(
        Scripted([ChunkResponse(0, 0.0, {"segments": [segment]})]), job, Config()
    )
    assert outcome.status == "error"


@pytest.mark.parametrize("text", ["", "   "])
def test_explicit_blank_text_stays_a_filter_empty_success(text: str) -> None:
    """빈 문자열·공백 문자열은 말이 없는 것이다 — 걸러져 빈 성공이지 위반이 아니다."""
    job = chunk_job()
    blank = {"segments": [{"start": 0.0, "end": 1.0, "text": text}]}
    outcome = transcribe_outcome(Scripted(responses_for(job, [blank, blank])), job, Config())
    assert outcome.status == "success"
    assert outcome.segments == ()
    assert [cover.segment_count for cover in outcome.coverage] == [1, 1]


def test_require_nonempty_returns_segments_only_for_nonempty_success() -> None:
    job = chunk_job()
    outcome = transcribe_outcome(Scripted(responses_for(job, [RAW, RAW])), job, Config())
    segments = require_nonempty(outcome)
    assert len(segments) == 4
    assert segments is outcome.segments


def test_require_nonempty_raises_malformed_with_the_recorded_context() -> None:
    job = chunk_job((0.0,))
    outcome = transcribe_outcome(
        Scripted(failure=RuntimeError("Error code: 401 - bad key")), job, Config()
    )
    with pytest.raises(MalformedTranscript, match="401"):
        require_nonempty(outcome)


def test_require_nonempty_raises_empty_for_every_nonerror_without_segments() -> None:
    """명시적 빈이든 걸러져 빈이든 — 초안이 없으면 같은 예외로 갈린다."""
    job = chunk_job()
    literal = transcribe_outcome(Scripted(responses_for(job, [EMPTY, EMPTY])), job, Config())
    filtered = transcribe_outcome(Scripted(responses_for(job, [BLANK, BLANK])), job, Config())
    for outcome in (literal, filtered):
        with pytest.raises(EmptyTranscript):
            require_nonempty(outcome)


def test_the_new_errors_remain_runtime_errors() -> None:
    """기존 호출자는 RuntimeError로 잡는다 — 호환을 끊지 않는다."""
    assert issubclass(EmptyTranscript, RuntimeError)
    assert issubclass(MalformedTranscript, RuntimeError)


# ─────────────────────────────────────────────────────────────
#  픽스처 네임스페이스 — 같은 stem, 다른 디렉터리, 엄격한 원본 검사 (P3-01)
# ─────────────────────────────────────────────────────────────


def _voiced(text: str) -> dict[str, Any]:
    return {"segments": [{"start": 0.0, "end": 1.0, "text": f" {text}"}]}


def write_whole_fixture(root: Path, records: list[dict[str, Any]], stem: str = "demo") -> Path:
    body = {"chunks": records}
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stem}.whisper.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def namespaced(namespace: Path, offsets: Sequence[float]) -> TranscriptionJob:
    return TranscriptionJob(stem="demo", chunks=chunk_job(offsets).chunks, namespace=namespace)


def test_two_namespaces_with_one_stem_resolve_independently(tmp_path: Path) -> None:
    """같은 stem이라도 namespace가 다르면 다른 픽스처를 읽는다. 환경은 그대로다."""
    write_whole_fixture(
        tmp_path / "ns-a", [{"index": 0, "offset": 0.0, "response": _voiced("망원동")}]
    )
    write_whole_fixture(
        tmp_path / "ns-b", [{"index": 0, "offset": 0.0, "response": _voiced("연남동")}]
    )
    transcriber = FixtureTranscriber(fixture_dir=SYNTHETIC)  # namespace가 디렉터리를 이긴다
    before = dict(os.environ)

    first = transcriber.transcribe(namespaced(tmp_path / "ns-a", (0.0,)), Config())
    second = transcriber.transcribe(namespaced(tmp_path / "ns-b", (0.0,)), Config())

    assert first[0].payload["segments"][0]["text"] == " 망원동"
    assert second[0].payload["segments"][0]["text"] == " 연남동"
    assert dict(os.environ) == before  # 전역 환경은 건드리지 않는다


def test_a_missing_namespace_fixture_names_the_exact_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(
            namespaced(tmp_path / "ns", (0.0,)), Config()
        )
    message = str(excinfo.value)
    assert str(tmp_path / "ns" / "demo.whisper.json") in message
    assert "vlogsrt record" in message


def test_namespace_selects_only_the_requested_subset_in_chunk_order(tmp_path: Path) -> None:
    write_whole_fixture(
        tmp_path,
        [
            {"index": 0, "offset": 0.0, "response": _voiced("첫")},
            {"index": 1, "offset": 12.0, "response": _voiced("둘")},
            {"index": 2, "offset": 24, "response": _voiced("셋")},  # 정수 오프셋도 받는다
        ],
    )
    audio = Path("demo.mp3")
    job = TranscriptionJob(
        stem="demo",
        chunks=(Chunk(audio, 24.0, 12.0, 2), Chunk(audio, 0.0, 12.0, 0)),  # 순서는 섞여 있다
        namespace=tmp_path,
    )
    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())
    assert [r.index for r in responses] == [0, 2]  # 조각 번호 순서로 결정적이다
    assert [r.offset for r in responses] == [0.0, 24.0]
    assert [r.payload["segments"][0]["text"] for r in responses] == [" 첫", " 셋"]


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({"offset": 0.0, "response": RAW}, id="index-missing"),
        pytest.param({"index": True, "offset": 0.0, "response": RAW}, id="index-bool"),
        pytest.param({"index": "0", "offset": 0.0, "response": RAW}, id="index-string"),
        pytest.param({"index": 1.5, "offset": 0.0, "response": RAW}, id="index-float"),
        pytest.param({"index": 0, "response": RAW}, id="offset-missing"),
        pytest.param({"index": 0, "offset": True, "response": RAW}, id="offset-bool"),
        pytest.param({"index": 0, "offset": float("nan"), "response": RAW}, id="offset-nan"),
        pytest.param({"index": 0, "offset": "0.0", "response": RAW}, id="offset-string"),
        pytest.param({"index": 0, "offset": 0.0}, id="response-missing"),
    ],
)
def test_namespace_rejects_a_broken_raw_record_before_coercion(
    tmp_path: Path, record: dict[str, Any]
) -> None:
    """int()/float() 강제가 잘못된 원본을 숨기기 전에 걸러야 한다."""
    write_whole_fixture(tmp_path, [record])
    with pytest.raises(RuntimeError):
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(namespaced(tmp_path, (0.0,)), Config())


def test_namespace_rejects_a_duplicate_requested_identity(tmp_path: Path) -> None:
    write_whole_fixture(
        tmp_path,
        [
            {"index": 0, "offset": 0.0, "response": RAW},
            {"index": 0, "offset": 0.0, "response": RAW},
        ],
    )
    with pytest.raises(RuntimeError, match="둘 이상"):
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(namespaced(tmp_path, (0.0,)), Config())


def test_namespace_rejects_a_missing_requested_identity(tmp_path: Path) -> None:
    write_whole_fixture(tmp_path, [{"index": 0, "offset": 0.0, "response": RAW}])
    job = namespaced(tmp_path, (0.0, 12.0))
    with pytest.raises(RuntimeError, match="없는 조각"):
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())


def test_namespace_rejects_an_offset_mismatch_before_coercion(tmp_path: Path) -> None:
    write_whole_fixture(tmp_path, [{"index": 1, "offset": 12.5, "response": RAW}])
    job = namespaced(tmp_path, (0.0, 12.0))
    with pytest.raises(RuntimeError, match="오프셋"):
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())


def test_namespace_none_keeps_the_legacy_return_all() -> None:
    """namespace가 없으면 지금처럼 설정된 디렉터리에서 전부를 읽는다."""
    single = TranscriptionJob(stem="demo", chunks=chunk_job((0.0,)).chunks)
    every = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(single, Config())
    assert [r.index for r in every] == [0, 1]  # 요청과 무관하게 전부
    whole = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(
        TranscriptionJob(stem="demo", chunks=()), Config()
    )
    assert [r.index for r in whole] == [0, 1]


# ── 수정(Sol 3): namespace 안에 머무는 이름 조각만 받는다 ────────────────────


@pytest.mark.parametrize(
    "stem",
    ["../outside", "..", ".", "/etc/passwd", "a/b", "", "a\\b"],
)
def test_namespace_rejects_unsafe_stem_components(tmp_path: Path, stem: str) -> None:
    """경로를 만들기 전에 거부한다. 디렉터리 밖에 함정 파일을 두고 못 나가는지 본다."""
    (tmp_path / "outside.whisper.json").write_text(
        json.dumps({"chunks": [{"index": 0, "offset": 0.0, "response": RAW}]}), encoding="utf-8"
    )
    job = TranscriptionJob(
        stem=stem, chunks=chunk_job((0.0,)).chunks, namespace=tmp_path / "namespace"
    )
    with pytest.raises(MalformedTranscript):
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())


@pytest.mark.parametrize("kind", ["../outside", "", "a/b"])
def test_namespace_rejects_unsafe_kind_components(tmp_path: Path, kind: str) -> None:
    job = TranscriptionJob(
        stem="demo", chunks=chunk_job((0.0,)).chunks, namespace=tmp_path / "namespace", kind=kind
    )
    with pytest.raises(MalformedTranscript):
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())


def test_namespace_keeps_normal_unicode_space_dot_stems(tmp_path: Path) -> None:
    """정상 이름은 그대로 통과한다 — 한글·공백·점이 흔하게 쓰인다."""
    write_whole_fixture(
        tmp_path, [{"index": 0, "offset": 0.0, "response": _voiced("망원동")}], stem="망원 동.vlog"
    )
    job = TranscriptionJob(stem="망원 동.vlog", chunks=chunk_job((0.0,)).chunks, namespace=tmp_path)
    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())
    assert responses[0].payload["segments"][0]["text"] == " 망원동"


def test_the_name_guard_is_namespace_present_only() -> None:
    """namespace=None 레거시는 같은 stem을 전혀 다르게 다룬다 — 그대로 둔다."""
    job = TranscriptionJob(stem="../demo", chunks=())
    with pytest.raises(RuntimeError, match="vlogsrt record"):
        FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())


# ─────────────────────────────────────────────────────────────
#  비전 호출 경계 (P5-05) — 파생 계층 비전 모델의 유일한 호출 지점
# ─────────────────────────────────────────────────────────────


class FakeVisionClient:
    """`chat.completions.create` 호출 인자만 기록한다. 네트워크 없음."""

    def __init__(self, reply: object | None = None, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.reply: object = {} if reply is None else reply
        self.failure = failure
        self.chat = self
        self.completions = self

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.reply


class DumpableVisionReply:
    """SDK 응답 객체의 최소 모습 — `model_dump`로 사전이 된다."""

    def model_dump(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "{}"}}]}


class FakeResponsesClient:
    """`responses.create`의 모델/페이로드와 재시도 없는 1회 호출을 기록한다."""

    def __init__(self, reply: object | None = None, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.reply = reply if reply is not None else {"output_text": "{}"}
        self.failure = failure
        self.responses = self

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.reply


def _status_error(status: int, message: str) -> RuntimeError:
    error = RuntimeError(f"Error code: {status} - {message}")
    error.status_code = status  # type: ignore[attr-defined]
    return error


def _fake_openai(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, object]], object]:
    """가짜 openai 꾸러미 — `OpenAI()` 생성 인자를 기록한다. 네트워크 없음."""
    built: list[dict[str, object]] = []
    sentinel = object()

    def _openai(**kwargs: object) -> object:
        built.append(kwargs)
        return sentinel

    module = types.ModuleType("openai")
    module.OpenAI = _openai
    monkeypatch.setitem(sys.modules, "openai", module)
    return built, sentinel


VISION_PAYLOAD = {"messages": [{"role": "user", "content": []}]}
LUNA_PAYLOAD = {"input": [{"role": "user", "content": []}]}


def test_luna_vision_uses_responses_api_only_after_online_gate() -> None:
    """rough-cut Luna가 chat.completions로 회귀하거나 승인 없이 호출되는 버그를 잡는다."""
    client = FakeResponsesClient(reply={"output_text": '{"choice":"keep"}'})
    with pytest.raises(RuntimeError, match="online=True"):
        request_luna_vision(client, model="gpt-5.6-luna", payload=LUNA_PAYLOAD)
    assert client.calls == []
    raw = request_luna_vision(client, model="gpt-5.6-luna", payload=LUNA_PAYLOAD, online=True)
    assert client.calls == [{"model": "gpt-5.6-luna", **LUNA_PAYLOAD}]
    assert raw == {"output_text": '{"choice":"keep"}'}


def test_vision_requests_fail_closed_without_explicit_online() -> None:
    """기본값도 명시적 False도 같다 — 클라이언트를 만지기 전에 닫힌다."""
    client = FakeVisionClient()
    for online in (None, False):
        kwargs = {} if online is None else {"online": online}
        with pytest.raises(RuntimeError, match="online=True"):
            request_vision(client, model="vision-model-x", payload=VISION_PAYLOAD, **kwargs)
    assert client.calls == []


def test_vision_online_sends_the_exact_model_and_payload() -> None:
    client = FakeVisionClient(reply={"choices": []})
    raw = request_vision(client, model="vision-model-x", payload=VISION_PAYLOAD, online=True)
    (call,) = client.calls
    assert call == {"model": "vision-model-x", **VISION_PAYLOAD}
    assert raw == {"choices": []}


def test_vision_rejects_a_payload_that_redefines_the_model() -> None:
    """모델의 정본은 함수 인자 하나뿐이다 — 페이로드가 끼어들면 크게 실패한다."""
    client = FakeVisionClient()
    with pytest.raises(TypeError):
        request_vision(
            client,
            model="vision-model-x",
            payload={"model": "other-model", "messages": []},
            online=True,
        )


def test_vision_converts_sdk_objects_to_dicts() -> None:
    client = FakeVisionClient(reply=DumpableVisionReply())
    raw = request_vision(client, model="vision-model-x", payload=VISION_PAYLOAD, online=True)
    assert raw == {"choices": [{"message": {"content": "{}"}}]}


def test_vision_429_is_an_explicit_error_not_a_retry() -> None:
    """재시도가 없다 — 429는 명시적 오류 기록물이 되어야지 조용한 지연이 되면 안 된다."""
    client = FakeVisionClient(failure=_status_error(429, "rate limit exceeded"))
    with pytest.raises(RuntimeError, match="429"):
        request_vision(client, model="vision-model-x", payload=VISION_PAYLOAD, online=True)
    assert len(client.calls) == 1


def test_vision_timeout_is_an_explicit_error_not_a_retry() -> None:
    client = FakeVisionClient(failure=TimeoutError("request timed out"))
    with pytest.raises(TimeoutError, match="timed out"):
        request_vision(client, model="vision-model-x", payload=VISION_PAYLOAD, online=True)
    assert len(client.calls) == 1


def test_vision_client_takes_explicit_key_and_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """값은 받은 것만 간다 — base_url이 없으면 공개 엔드포인트를 명시적으로 못 박는다.

    `max_retries=0`도 못 박는다 — SDK 기본값 2는 `request_vision`의 "재시도 없음"
    계약 뒤에 숨은 재시도다(429·타임아웃이 조용한 지연이 되면 안 된다).
    """
    built, sentinel = _fake_openai(monkeypatch)
    with_base_url = create_vision_client(
        api_key="vision-key", base_url="https://vision.example/api"
    )
    without_base_url = create_vision_client(api_key="vision-key", base_url=None)
    assert with_base_url is sentinel and without_base_url is sentinel
    assert built == [
        {
            "api_key": "vision-key",
            "base_url": "https://vision.example/api",
            "max_retries": 0,
            "timeout": 60.0,
        },
        {
            "api_key": "vision-key",
            "base_url": "https://api.openai.com/v1",
            "max_retries": 0,
            "timeout": 60.0,
        },
    ]


def test_vision_client_ignores_the_environment_and_legacy_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """비전 자격증명은 별도 이름이다 — 레거시 STT 자격증명 경로를 물려받지 않는다."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("비전 클라이언트는 legacy STT 자격증명 경로를 쓰면 안 된다")

    for name in ("GOOGLE_API_KEY", "GEMINI_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("vlogsrt.env.require_api_key", _boom)
    monkeypatch.setattr("vlogsrt.env.base_url", _boom)
    built, _ = _fake_openai(monkeypatch)

    create_vision_client(api_key="vision-key", base_url=None)
    assert built == [
        {
            "api_key": "vision-key",
            "base_url": "https://api.openai.com/v1",
            "max_retries": 0,
            "timeout": 60.0,
        }
    ]


def test_vision_client_without_base_url_does_not_inherit_the_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK의 None 폴백은 OPENAI_BASE_URL을 물려받는다(설치된 3.8.0 실측) — 비전은 못 쓴다.

    실제 SDK로 생성만 증명한다(호출은 없다). 없으면 레거시 프록시를 물려받는다.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "https://legacy-stt-proxy.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-stt-key")
    client = create_vision_client(api_key="vision-key", base_url=None)
    assert str(client.base_url).rstrip("/") == "https://api.openai.com/v1"
    assert client.api_key == "vision-key"
    assert client.max_retries == 0  # SDK 기본값 2는 숨은 재시도다 — 여기선 한 번뿐이어야 한다


def test_vision_client_keeps_the_caller_base_url_verbatim_under_the_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://legacy-stt-proxy.example/v1")
    client = create_vision_client(api_key="vision-key", base_url="https://vision.example/api")
    assert str(client.base_url).rstrip("/") == "https://vision.example/api"
    assert client.max_retries == 0  # 사용자 base_url에서도 SDK 숨은 재시도는 못 박아 뺀다


def test_offline_defaults_and_lazy_clients_make_zero_constructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """오프라인 기본은 픽스처고, 실제 호출 경로도 호출 직전까지 클라이언트를 안 만든다."""
    built, _ = _fake_openai(monkeypatch)
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")

    assert isinstance(get_transcriber(None), FixtureTranscriber)
    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job(), Config())
    assert responses  # 픽스체 경로는 그대로 산다

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(get_transcriber(Config(), online=True), LiveTranscriber)
    assert built == []  # 클라이언트 생성은 실제 전송 직전(지연 import)에야 일어난다


# ─────────────────────────────────────────────────────────────
#  비전 재생기 (V1 §3-a) — Transcriber와 대칭인 주입 경계
# ─────────────────────────────────────────────────────────────


def test_live_responder_opens_the_paid_gate_and_passes_payload_verbatim() -> None:
    """LiveVisionResponder는 request_vision(online=True) 하나로 간다 — 우회 호출이 없다."""
    client = FakeVisionClient(reply={"choices": []})
    raw = LiveVisionResponder(client=client).decide(
        model="vision-model-x", payload=VISION_PAYLOAD, block_id="b" * 64
    )
    (call,) = client.calls
    assert call == {"model": "vision-model-x", **VISION_PAYLOAD}
    assert raw == {"choices": []}


def test_closed_responder_raises_the_gate_itself() -> None:
    """닫힘의 문구를 다시 쓰지 않는다 — request_vision의 게이트 예외가 곧 답이다."""
    with pytest.raises(RuntimeError, match="online=True"):
        ClosedVisionResponder().decide(
            model="vision-model-x", payload=VISION_PAYLOAD, block_id="b" * 64
        )


def test_fixture_responder_reads_the_recorded_reply_per_block(tmp_path: Path) -> None:
    """블록마다 파일 하나 — reason에 출처가 달린 채로 돌아간다(정규화는 러너가 한다)."""
    body = {"choice": "c1", "reason": "되풀이 B-roll이다", "confidence": 0.9}
    reply = {"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]}
    (tmp_path / "aaa.vision.json").write_text(
        json.dumps(reply, ensure_ascii=False), encoding="utf-8"
    )
    responder = FixtureVisionResponder(namespace=tmp_path)
    marked = responder.decide(model="vision-model-x", payload=VISION_PAYLOAD, block_id="aaa")
    replayed = json.loads(marked["choices"][0]["message"]["content"])
    assert replayed["reason"] == "재생 판정(픽스처) — 되풀이 B-roll이다"
    assert replayed["choice"] == "c1" and replayed["confidence"] == 0.9  # 다른 칸은 그대로
    recorded = json.loads(reply["choices"][0]["message"]["content"])
    assert recorded["reason"] == "되풀이 B-roll이다"  # 기록 파일의 원문은 그대로다


@pytest.mark.parametrize(
    "reply",
    [
        {"choices": []},
        {"choices": "no"},
        {"choices": [{"message": {"content": "{not json"}}]},
        {"choices": [{"message": {"content": "{}"}}]},  # reason이 없는 몸통
    ],
)
def test_fixture_responder_leaves_broken_replies_untouched(tmp_path: Path, reply: dict) -> None:
    """깨진 답은 표시를 다다가 오히려 깨뜨리지 않는다 — 어차피 오류 판정으로 간다."""
    (tmp_path / "aaa.vision.json").write_text(json.dumps(reply), encoding="utf-8")
    responder = FixtureVisionResponder(namespace=tmp_path)
    assert responder.decide(model="m", payload=VISION_PAYLOAD, block_id="aaa") == reply, (
        "멀쩡한 답만 표시한다 — 승인될 수 없는 답은 위장될 수도 없다"
    )


def test_fixture_responder_missing_file_is_a_loud_configuration_failure(
    tmp_path: Path,
) -> None:
    """픽스처가 없으면 조용히 통과하지 않는다 — 파일을 놓으라고 안내한다."""
    with pytest.raises(MissingVisionFixture, match="aaa.vision.json"):
        FixtureVisionResponder(namespace=tmp_path).decide(
            model="vision-model-x", payload=VISION_PAYLOAD, block_id="aaa"
        )


def test_fixture_responder_rejects_broken_records(tmp_path: Path) -> None:
    (tmp_path / "bad.vision.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "list.vision.json").write_text("[1, 2]", encoding="utf-8")
    responder = FixtureVisionResponder(namespace=tmp_path)
    with pytest.raises(RuntimeError, match="올바른 JSON이 아닙니다"):
        responder.decide(model="m", payload=VISION_PAYLOAD, block_id="bad")
    with pytest.raises(RuntimeError, match="JSON 객체가 아닙니다"):
        responder.decide(model="m", payload=VISION_PAYLOAD, block_id="list")


@pytest.mark.parametrize("block_id", ["", ".", "..", "a/b", "a\\b"])
def test_fixture_responder_rejects_unsafe_block_ids(tmp_path: Path, block_id: str) -> None:
    """`_namespace_path`와 같은 선 — namespace 밖으로 나가는 경로는 아예 만들지 않는다."""
    with pytest.raises(RuntimeError, match="안전한 파일명 조각이 아닙니다"):
        FixtureVisionResponder(namespace=tmp_path).decide(
            model="m", payload=VISION_PAYLOAD, block_id=block_id
        )


# ─────────────────────────────────────────────────────────────
#  공개 전송 경계 (A7-1) — 파생 계층의 좁은 요청이 같은 호출 지점을 지난다
# ─────────────────────────────────────────────────────────────


def text_request(tmp_path: Path) -> AudioTranscriptionRequest:
    """문구 경로 요청 — 확인된 힌트 세 칸만 쓴다(§11.4)."""
    audio = tmp_path / "final.mp3"
    audio.write_bytes(b"\x00")
    chunk = Chunk(audio, 0.0, 24.0, 0)
    return AudioTranscriptionRequest(
        chunk=chunk,
        model=Config().verify_model,
        response_format="json",
        languages=("ko",),
        keywords=("블루펭귄",),
        prompt="망원동, 아페로",
    )


def timing_request(tmp_path: Path) -> AudioTranscriptionRequest:
    """시간 경로 요청 — 문구 힌트 칸이 비어 있는 것이 배선 자체다(§11.4)."""
    audio = tmp_path / "final.mp3"
    audio.write_bytes(b"\x00")
    chunk = Chunk(audio, 0.0, 24.0, 0)
    return AudioTranscriptionRequest(
        chunk=chunk,
        model=Config().transcribe_model,
        response_format="verbose_json",
        timestamp_granularities=("segment",),
    )


@pytest.mark.parametrize("response_format", ["srt", "vtt", "text", ""])
def test_transport_rejects_subtitle_formats_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response_format: str
) -> None:
    """srt·vtt는 전송 계약에 없다 — 클라이언트를 만들기도 전에 끝난다."""
    client = FakeClient()
    live = _live(monkeypatch, client)
    request = AudioTranscriptionRequest(
        chunk=real_job(tmp_path).chunks[0], model="m", response_format=response_format
    )
    with pytest.raises(UnsupportedTranscriptionRequest, match="전송 계약에 없다"):
        live.send_request(request, Config(), kind=CAPTION_TEXT_KIND)
    assert client.calls == []


def test_transport_rejects_broken_shapes_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient()
    live = _live(monkeypatch, client)
    chunk = real_job(tmp_path).chunks[0]
    broken = [
        AudioTranscriptionRequest(chunk=chunk, model="  ", response_format="json"),
        AudioTranscriptionRequest(
            chunk=chunk, model="m", response_format="json", prompt="줄\n바꿈"
        ),
        AudioTranscriptionRequest(chunk=chunk, model="m", response_format="json", keywords=("",)),
        AudioTranscriptionRequest(
            chunk=chunk, model="m", response_format="json", timestamp_granularities=("frame",)
        ),
        AudioTranscriptionRequest(
            chunk=chunk,
            model="m",
            response_format="json",
            timestamp_granularities=("segment",),  # 입자는 verbose_json과만 짝한다
        ),
        AudioTranscriptionRequest(
            chunk=chunk, model="m", response_format="json", temperature=float("nan")
        ),
    ]
    for request in broken:
        with pytest.raises(UnsupportedTranscriptionRequest):
            live.send_request(request, Config(), kind=CAPTION_TEXT_KIND)
    assert client.calls == []


def test_text_request_sends_native_hint_arrays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """설치된 SDK(3.8)는 languages·keywords를 정식 인자로 받는다 — extra_body 없이."""
    client = FakeClient()
    _live(monkeypatch, client).send_request(
        text_request(tmp_path), Config(), kind=CAPTION_TEXT_KIND
    )
    (call,) = client.calls
    assert call["languages"] == ["ko"]
    assert call["keywords"] == ["블루펭귄"]
    assert call["prompt"] == "망원동, 아페로"
    assert "extra_body" not in call
    assert "timestamp_granularities" not in call
    assert call["response_format"] == "json"


def test_empty_hints_are_omitted_not_sent_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """빈 힌트는 빈 배열로 보내지 않고 필드째 뺀다(§11.4)."""
    client = FakeClient()
    request = AudioTranscriptionRequest(
        chunk=real_job(tmp_path).chunks[0], model="m", response_format="json"
    )
    _live(monkeypatch, client).send_request(request, Config(), kind=CAPTION_TEXT_KIND)
    (call,) = client.calls
    for absent in ("languages", "keywords", "prompt", "extra_body"):
        assert absent not in call


def test_old_sdk_gets_hints_through_documented_extra_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """정식 인자가 없는 옛 SDK는 extra_body 길로 — 같은 필드를 두 경로로 보내지 않는다."""
    monkeypatch.setattr(transcribe_module, "sdk_native_hint_params", lambda: ())
    client = FakeClient()
    _live(monkeypatch, client).send_request(
        text_request(tmp_path), Config(), kind=CAPTION_TEXT_KIND
    )
    (call,) = client.calls
    assert call["extra_body"] == {"languages": ["ko"], "keywords": ["블루펭귄"]}
    assert "languages" not in call and "keywords" not in call  # 중복 전달 금지
    assert call["prompt"] == "망원동, 아페로"  # prompt는 옛 SDK에서도 정식 인자다


def test_timing_request_carries_no_text_hints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """문구 힌트가 시간 요청에 실리는 일은 요청 칸이 아니라 배선에서 막힌다."""
    client = FakeClient()
    _live(monkeypatch, client).send_request(
        timing_request(tmp_path), Config(), kind=CAPTION_TIMING_KIND
    )
    (call,) = client.calls
    for absent in ("prompt", "keywords", "languages", "extra_body"):
        assert absent not in call
    assert call["timestamp_granularities"] == ["segment"]
    assert call["response_format"] == "verbose_json"


def test_transport_retries_transient_failures_and_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """429는 `_retry`의 바운드 안에서만 다시 묻는다 — 못 하면 그대로 올라간다."""
    client = Throttled()
    live = _live(monkeypatch, client)
    cfg = data_replace(Config(), api_max_retries=3, api_backoff_base=0.0)
    with pytest.raises(RuntimeError, match="429"):
        live.send_request(timing_request(tmp_path), cfg, kind=CAPTION_TIMING_KIND)
    assert len(client.calls) == 3  # 바운드를 넘은 몰래 재시도는 없다


def test_transport_does_not_silently_drop_fields_on_400(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """확인된 등록표가 틀렸다는 신호다 — 인자를 떼고 다시 부르지 않는다(§11.8)."""

    class Rejecting(FakeClient):
        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            error = RuntimeError("Error code: 400 - unknown parameter 'languages'")
            error.status_code = 400  # type: ignore[attr-defined]
            raise error

    client = Rejecting()
    live = _live(monkeypatch, client)
    with pytest.raises(RuntimeError, match="400"):
        live.send_request(text_request(tmp_path), Config(), kind=CAPTION_TEXT_KIND)
    assert len(client.calls) == 1  # 레거시와 달리 축소 재요청이 없다


def test_transport_cancellation_gate_blocks_before_the_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """승인 직전의 취소 확인 — 게이트가 닫히면 SDK를 부르지 않는다(S1A)."""
    client = FakeClient()

    def gate() -> None:
        raise Cancelled

    live = _live(monkeypatch, client, gate)
    with pytest.raises(Cancelled):
        live.send_request(timing_request(tmp_path), Config(), kind=CAPTION_TIMING_KIND)
    assert client.calls == []


def test_transport_observer_sees_kind_and_sanitized_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """관찰 훅은 파생 계층 호출에도 그대로 붙는다 — 인자 사본에 자격 칸은 없다."""
    metas: list[object] = []
    responses: list[object] = []

    class Ledger:
        def authorize(self, meta: object) -> None:
            metas.append(meta)

        def on_response(self, ticket: object, meta: object, response: object) -> None:
            responses.append(response)

        def on_error(self, ticket: object, meta: object, error: object) -> None:
            raise AssertionError("성공 경로에서 오류 훅은 없다")

    client = FakeClient()
    monkeypatch.setattr(LiveTranscriber, "_client", lambda self: client)  # type: ignore[arg-type]
    live = LiveTranscriber(api_key="sk-test", observer=Ledger())  # type: ignore[arg-type]
    payload = live.send_request(
        timing_request(tmp_path), Config(), kind=CAPTION_TIMING_KIND, stem="final-01"
    )
    (meta,) = metas
    assert meta.kind == CAPTION_TIMING_KIND
    assert meta.operation == TRANSCRIPTION_OPERATION
    assert meta.stem == "final-01"
    assert meta.options["response_format"] == "verbose_json"
    assert "api_key" not in meta.options
    assert payload == {"segments": []}
    assert len(responses) == 1
