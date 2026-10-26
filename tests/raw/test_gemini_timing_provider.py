"""Gemini 3.5 Transcribe 시간 제공자(W-31c, D92).

사전 검사 → 확인된 조합 → 신원 → 문 → 해석 → 묶기.

가짜 opener가 실제 전송 자리에 앉는다(유료 호출 0회). 응답은 문서 모양의 합성 픽스처다. 결과는
whisper 제공자와 같은 `TimedTranscript` 모양이라 1단계의 나머지가 바뀌지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt.gemini_messages import GeminiClient
from vlogsrt.models import Chunk
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw.caption_provider import (
    AudioChunkBinding,
    GeminiTranscribeTimingProvider,
    MalformedCaptionResponse,
    TransportTimingProvider,
    timing_provider_for,
)
from vlogsrt.project.raw.config import (
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINES,
    RawWorkflowConfig,
    timing_engine_view,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "gemini"
ON = date(2026, 11, 1)
GEMINI_VIEW = timing_engine_view(RawWorkflowConfig(), TIMING_ENGINES[TIMING_ENGINE_GEMINI])


class _Reply:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Opener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        self.sent.append(json.loads(request.data))
        return _Reply(self.body)


def _client(opener: _Opener) -> GeminiClient:
    return GeminiClient(
        api_key="AIza-synthetic",
        base_url="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        timeout_seconds=30.0,
        opener=opener,
    )


def _chunk(tmp_path: Path) -> tuple[Chunk, AudioChunkBinding]:
    path = tmp_path / "asr-chunk-0002.wav"
    path.write_bytes(b"RIFF-synthetic")
    chunk = Chunk(path=path, offset=40.0, duration=30.0, index=2)
    binding = AudioChunkBinding(
        chunk_index=2,
        audio_sha256=hashlib.sha256(b"RIFF-synthetic").hexdigest(),
        sample_start=640_000,
        sample_end=1_120_000,
        sample_rate=16_000,
        request_id="run-g:0002",
    )
    return chunk, binding


def _provider(body: bytes) -> tuple[GeminiTranscribeTimingProvider, _Opener]:
    opener = _Opener(body)
    return GeminiTranscribeTimingProvider(client=_client(opener)), opener


def test_a_gemini_chunk_becomes_the_same_timed_transcript_shape(tmp_path: Path) -> None:
    provider, opener = _provider((FIXTURES / "transcribe_korean.json").read_bytes())
    chunk, binding = _chunk(tmp_path)
    timed = provider.timed_transcript(binding=binding, chunk=chunk, settings=GEMINI_VIEW, on=ON)
    assert (timed.model, timed.provider, timed.chunk_offset) == (
        "gemini-3.5-transcribe",
        "gemini_transcribe_word_info",
        40.0,
    )
    assert [(s.index, s.start, s.end, s.text, s.evidence_id) for s in timed.segments] == [
        (0, 0.1, 0.9, "안녕하세요.", "run-g:0002:0"),
        (1, 1.2, 2.8, "오늘은 한강에 왔어요!", "run-g:0002:1"),
        (2, 3.6, 4.8, "날씨 진짜 좋다", "run-g:0002:2"),
    ]
    assert [word.word for word in timed.words][:2] == ["안녕하세요", "오늘은"]
    assert timed.words[0].start == 0.1 and timed.response is not None
    ((body,),) = [opener.sent]
    assert body["generation_config"]["transcription_config"]["language_codes"] == ["ko-KR"]
    assert body["store"] is False


def test_a_silent_chunk_is_an_unconfirmed_empty(tmp_path: Path) -> None:
    provider, _ = _provider((FIXTURES / "transcribe_silent.json").read_bytes())
    chunk, binding = _chunk(tmp_path)
    timed = provider.timed_transcript(binding=binding, chunk=chunk, settings=GEMINI_VIEW, on=ON)
    assert timed.unconfirmed_empty


def test_an_unusable_response_is_a_malformed_chunk(tmp_path: Path) -> None:
    provider, _ = _provider(b'{"status": "failed", "output_text": ""}')
    chunk, binding = _chunk(tmp_path)
    with pytest.raises(MalformedCaptionResponse, match="끝나지"):
        provider.timed_transcript(binding=binding, chunk=chunk, settings=GEMINI_VIEW, on=ON)


def test_whisper_settings_never_reach_the_gemini_door(tmp_path: Path) -> None:
    provider, opener = _provider(b"{}")
    chunk, binding = _chunk(tmp_path)
    with pytest.raises(capabilities.UnverifiedModelCapability):
        provider.timed_transcript(binding=binding, chunk=chunk, settings=RawWorkflowConfig(), on=ON)
    assert opener.sent == []


def test_a_stale_binding_is_refused_before_sending(tmp_path: Path) -> None:
    from vlogsrt.project.raw.caption_provider import ChunkBindingMismatch

    provider, opener = _provider(b"{}")
    chunk, binding = _chunk(tmp_path)
    chunk.path.write_bytes(b"RIFF-changed")
    with pytest.raises(ChunkBindingMismatch):
        provider.timed_transcript(binding=binding, chunk=chunk, settings=GEMINI_VIEW, on=ON)
    assert opener.sent == []


def test_the_gemini_provider_takes_no_temperature() -> None:
    from inspect import signature

    assert (
        "temperature" not in signature(GeminiTranscribeTimingProvider.timed_transcript).parameters
    )


def test_the_factory_follows_the_configured_timing_model() -> None:
    sender = SimpleNamespace(observer=None, cancellation=None)
    cfg = object()
    whisper = timing_provider_for(RawWorkflowConfig(), sender=sender, cfg=cfg, gemini_client=None)
    assert isinstance(whisper, TransportTimingProvider)
    with pytest.raises(capabilities.TimingProviderUnavailable, match="GEMINI_API_KEY"):
        timing_provider_for(GEMINI_VIEW, sender=sender, cfg=cfg, gemini_client=None)
    client = _client(_Opener(b"{}"))
    gemini = timing_provider_for(GEMINI_VIEW, sender=sender, cfg=cfg, gemini_client=client)
    assert isinstance(gemini, GeminiTranscribeTimingProvider) and gemini.client is client


# ── 영수증 ───────────────────────────────────────────────────────────────


def _meta(model: str = "gemini-3.5-transcribe") -> Any:
    from vlogsrt.transcribe import GEMINI_TRANSCRIBE_OPERATION

    return SimpleNamespace(operation=GEMINI_TRANSCRIBE_OPERATION, model=model, chunk_duration=30.0)


def test_a_transcribe_receipt_prices_the_reported_tokens() -> None:
    from vlogsrt.project.raw.gemini_cost import measure_gemini_transcribe_usage

    reply = json.loads((FIXTURES / "transcribe_korean.json").read_text(encoding="utf-8"))
    usage = measure_gemini_transcribe_usage(_meta(), reply, on=ON)
    expected = Decimal(125) * Decimal(2) / Decimal(10**6) + Decimal(18) * Decimal(12) / Decimal(
        10**6
    )
    assert Decimal(str(usage.cost_usd)) == expected
    assert (usage.input_tokens, usage.output_tokens) == (125, 18)
    assert usage.provider_request_id == "int_synthetic_transcribe_ko"


@pytest.mark.parametrize(
    ("meta", "reply", "on", "reason"),
    [
        ("other", {"usage": {"total_input_tokens": 1, "total_output_tokens": 1}}, ON, "model"),
        ("ok", {"usage": {"total_input_tokens": -1}}, ON, "usage"),
        (
            "ok",
            {"usage": {"total_input_tokens": 1, "total_output_tokens": 1}},
            date(2027, 6, 1),
            "price",
        ),
    ],
)
def test_an_unpriceable_transcribe_receipt_keeps_tokens_but_no_cost(
    meta: str, reply: dict[str, Any], on: date, reason: str
) -> None:
    from vlogsrt.project.raw.gemini_cost import measure_gemini_transcribe_usage

    model = "gemini-9-unknown" if meta == "other" else "gemini-3.5-transcribe"
    usage = measure_gemini_transcribe_usage(_meta(model), reply, on=on)
    assert usage.cost_usd is None and reason in str(usage.unpriced_reason)


def test_the_usage_observer_routes_transcribe_calls_to_the_gemini_receipt() -> None:
    from vlogsrt.project.raw.request_usage import UsageObserver

    reply = json.loads((FIXTURES / "transcribe_korean.json").read_text(encoding="utf-8"))
    usage = UsageObserver()._measure(_meta(), reply)
    assert usage.input_tokens == 125 and usage.model == "gemini-3.5-transcribe"
