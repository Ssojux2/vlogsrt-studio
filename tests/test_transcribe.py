"""전사 경계. 네트워크 없이 합성 응답만으로 돈다.

whisper·gpt-transcribe 전송은 D163으로 지웠다 — 남은 문은 Luna 비전·Claude·Gemini다.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.transcribe import create_vision_client, request_luna_vision

# ─────────────────────────────────────────────────────────────
#  비전 호출 경계 (P5-05) — 파생 계층 비전 모델의 유일한 호출 지점
# ─────────────────────────────────────────────────────────────


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


def test_luna_vision_fails_closed_with_an_explicit_false() -> None:
    """기본값도 명시적 False도 같다 — 클라이언트를 만지기 전에 닫힌다."""
    client = FakeResponsesClient()
    with pytest.raises(RuntimeError, match="online=True"):
        request_luna_vision(client, model="gpt-5.6-luna", payload=LUNA_PAYLOAD, online=False)
    assert client.calls == []


def test_luna_vision_rejects_a_payload_that_redefines_the_model() -> None:
    """모델의 정본은 함수 인자 하나뿐이다 — 페이로드가 끼어들면 크게 실패한다."""
    client = FakeResponsesClient()
    with pytest.raises(TypeError):
        request_luna_vision(
            client,
            model="gpt-5.6-luna",
            payload={"model": "other-model", "input": []},
            online=True,
        )
    assert client.calls == []


def test_luna_vision_converts_sdk_objects_to_dicts() -> None:
    client = FakeResponsesClient(reply=DumpableVisionReply())
    raw = request_luna_vision(client, model="gpt-5.6-luna", payload=LUNA_PAYLOAD, online=True)
    assert raw == {"choices": [{"message": {"content": "{}"}}]}


def test_luna_vision_429_is_an_explicit_error_not_a_retry() -> None:
    """재시도가 없다 — 429는 명시적 오류 기록물이 되어야지 조용한 지연이 되면 안 된다."""
    client = FakeResponsesClient(failure=_status_error(429, "rate limit exceeded"))
    with pytest.raises(RuntimeError, match="429"):
        request_luna_vision(client, model="gpt-5.6-luna", payload=LUNA_PAYLOAD, online=True)
    assert len(client.calls) == 1


def test_luna_vision_timeout_is_an_explicit_error_not_a_retry() -> None:
    client = FakeResponsesClient(failure=TimeoutError("request timed out"))
    with pytest.raises(TimeoutError, match="timed out"):
        request_luna_vision(client, model="gpt-5.6-luna", payload=LUNA_PAYLOAD, online=True)
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


def test_vision_client_ignores_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """비전 자격증명은 받은 값뿐이다 — 환경의 다른 자격증명을 물려받지 않는다."""
    for name in ("GOOGLE_API_KEY", "GEMINI_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
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


# ─────────────────────────────────────────────────────────────
#  Claude Messages 경계(D54) — Luna 경계와 같은 문·관찰·취소
# ─────────────────────────────────────────────────────────────


CLAUDE_PAYLOAD = {
    "max_tokens": 16,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "안녕"}]}],
}


class _ClaudeReply:
    status = 200
    headers = {"request-id": "req_synthetic"}

    def read(self) -> bytes:
        return json.dumps({"type": "message", "stop_reason": "end_turn", "content": []}).encode()

    def __enter__(self) -> _ClaudeReply:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _ClaudeOpener:
    """Claude 클라이언트의 전송 자리 — 보낸 본문을 기록하고 정해진 응답을 돌려준다."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float) -> _ClaudeReply:
        self.bodies.append(json.loads(request.data))
        return _ClaudeReply()


def _claude_client(opener: _ClaudeOpener) -> Any:
    from vlogsrt.anthropic_messages import ClaudeMessagesClient

    return ClaudeMessagesClient(
        api_key="sk-ant-synthetic",
        base_url="https://api.anthropic.com",
        api_version="2023-06-01",
        timeout_seconds=30.0,
        opener=opener,
    )


class _ClaudeLedger:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.metas: list[Any] = []
        self.responses: list[Any] = []

    def authorize(self, meta: Any) -> str:
        self.trace.append("authorize")
        self.metas.append(meta)
        return "ticket"

    def on_response(self, ticket: object, meta: Any, response: Any) -> None:
        self.trace.append("response")
        self.responses.append(response)

    def on_error(self, ticket: object, meta: Any, error: Exception) -> None:
        self.trace.append("error")


def test_claude_messages_are_closed_without_explicit_online() -> None:
    from vlogsrt.transcribe import request_claude_messages

    opener = _ClaudeOpener()
    with pytest.raises(RuntimeError, match="online=True"):
        request_claude_messages(
            _claude_client(opener), model="claude-opus-5-5", payload=CLAUDE_PAYLOAD
        )
    with pytest.raises(RuntimeError, match="online=True"):
        request_claude_messages(
            _claude_client(opener), model="claude-opus-5-5", payload=CLAUDE_PAYLOAD, online=False
        )
    assert opener.bodies == []


def test_claude_payload_redefining_model_fails_before_authorization() -> None:
    from vlogsrt.transcribe import request_claude_messages

    opener = _ClaudeOpener()
    ledger = _ClaudeLedger([])
    with pytest.raises(TypeError, match="model"):
        request_claude_messages(
            _claude_client(opener),
            model="claude-opus-5-5",
            payload={**CLAUDE_PAYLOAD, "model": "other"},
            online=True,
            observer=ledger,
        )
    assert ledger.metas == [] and opener.bodies == []


def test_a_non_claude_client_fails_before_authorization() -> None:
    from vlogsrt.transcribe import request_claude_messages

    ledger = _ClaudeLedger([])
    with pytest.raises(TypeError, match="Claude"):
        request_claude_messages(
            object(), model="claude-opus-5-5", payload=CLAUDE_PAYLOAD, online=True, observer=ledger
        )
    assert ledger.metas == []


def test_claude_observer_hooks_fire_with_the_messages_operation() -> None:
    from vlogsrt.transcribe import MESSAGES_OPERATION, request_claude_messages

    trace: list[str] = []
    opener = _ClaudeOpener()
    ledger = _ClaudeLedger(trace)
    reply = request_claude_messages(
        _claude_client(opener),
        model="claude-opus-5-5",
        payload=CLAUDE_PAYLOAD,
        online=True,
        observer=ledger,
    )
    assert trace == ["authorize", "response"]
    (meta,) = ledger.metas
    assert meta.operation == MESSAGES_OPERATION == "messages.create"
    assert meta.model == "claude-opus-5-5"
    assert meta.options["max_tokens"] == 16
    assert opener.bodies == [{"model": "claude-opus-5-5", **CLAUDE_PAYLOAD}]
    assert ledger.responses == [reply]
    assert reply["request_id"] == "req_synthetic"


def test_claude_cancellation_gates_the_send() -> None:
    from vlogsrt.transcribe import request_claude_messages

    class Stop(BaseException):
        pass

    def gate() -> None:
        raise Stop

    opener = _ClaudeOpener()
    ledger = _ClaudeLedger([])
    with pytest.raises(Stop):
        request_claude_messages(
            _claude_client(opener),
            model="claude-opus-5-5",
            payload=CLAUDE_PAYLOAD,
            online=True,
            observer=ledger,
            cancellation=gate,
        )
    with pytest.raises(Stop):  # 관찰자 없는 길도 전송 전에 멈춘다
        request_claude_messages(
            _claude_client(opener),
            model="claude-opus-5-5",
            payload=CLAUDE_PAYLOAD,
            online=True,
            cancellation=gate,
        )
    assert opener.bodies == [] and ledger.metas == []


# ─── Gemini generateContent 경계(W-30a, D83) — Claude 문의 거울 ───────────────

GEMINI_PAYLOAD = {
    "contents": [{"role": "user", "parts": [{"text": "합성"}]}],
    "generationConfig": {"maxOutputTokens": 16},
}
GEMINI_MODEL = "gemini-3.8-flash"


class _GeminiReply(_ClaudeReply):
    def read(self) -> bytes:
        return b'{"candidates": [], "responseId": "resp_synthetic"}'


class _GeminiOpener:
    """Gemini 클라이언트의 전송 자리 — 보낸 (URL, 본문)을 기록하고 정해진 응답을 돌려준다."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: Any, timeout: float) -> _GeminiReply:
        self.sent.append((request.full_url, json.loads(request.data)))
        return _GeminiReply()


def _gemini_client(opener: _GeminiOpener) -> Any:
    from vlogsrt.gemini_messages import GeminiClient

    return GeminiClient(
        api_key="AIza-synthetic",
        base_url="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        timeout_seconds=30.0,
        opener=opener,
    )


def test_gemini_generate_is_closed_without_explicit_online() -> None:
    from vlogsrt.transcribe import request_gemini_generate

    opener = _GeminiOpener()
    for online in (None, False):
        kwargs = {} if online is None else {"online": online}
        with pytest.raises(RuntimeError, match="online=True"):
            request_gemini_generate(
                _gemini_client(opener), model=GEMINI_MODEL, payload=GEMINI_PAYLOAD, **kwargs
            )
    assert opener.sent == []


def test_gemini_payload_redefining_model_fails_before_authorization() -> None:
    from vlogsrt.transcribe import request_gemini_generate

    opener = _GeminiOpener()
    ledger = _ClaudeLedger([])
    with pytest.raises(TypeError, match="model"):
        request_gemini_generate(
            _gemini_client(opener),
            model=GEMINI_MODEL,
            payload={**GEMINI_PAYLOAD, "model": "other"},
            online=True,
            observer=ledger,
        )
    assert ledger.metas == [] and opener.sent == []


@pytest.mark.parametrize("client", [object(), "claude"])
def test_a_non_gemini_client_fails_before_authorization(client: object) -> None:
    from vlogsrt.transcribe import request_gemini_generate

    candidate = _claude_client(_ClaudeOpener()) if client == "claude" else client
    ledger = _ClaudeLedger([])
    with pytest.raises(TypeError, match="Gemini"):
        request_gemini_generate(
            candidate, model=GEMINI_MODEL, payload=GEMINI_PAYLOAD, online=True, observer=ledger
        )
    assert ledger.metas == []


@pytest.mark.parametrize("model", ["", "gemini-3.8-flash:streamGenerateContent", "a/b", "G"])
def test_a_gemini_model_that_is_not_a_path_segment_fails_before_authorization(model: str) -> None:
    """모델 이름은 경로에 들어간다 — 그 결함은 유료 예약(승인)보다 먼저 닫힌다."""
    from vlogsrt.transcribe import request_gemini_generate

    opener = _GeminiOpener()
    ledger = _ClaudeLedger([])
    with pytest.raises(ValueError, match="모델"):
        request_gemini_generate(
            _gemini_client(opener),
            model=model,
            payload=GEMINI_PAYLOAD,
            online=True,
            observer=ledger,
        )
    assert ledger.metas == [] and opener.sent == []


def test_gemini_observer_hooks_fire_with_the_generate_operation() -> None:
    from vlogsrt.transcribe import GENERATE_CONTENT_OPERATION, request_gemini_generate

    trace: list[str] = []
    opener = _GeminiOpener()
    ledger = _ClaudeLedger(trace)
    reply = request_gemini_generate(
        _gemini_client(opener),
        model=GEMINI_MODEL,
        payload=GEMINI_PAYLOAD,
        online=True,
        observer=ledger,
    )
    assert trace == ["authorize", "response"]
    (meta,) = ledger.metas
    assert meta.operation == GENERATE_CONTENT_OPERATION == "models.generateContent"
    assert meta.model == GEMINI_MODEL
    assert meta.options["generationConfig"] == {"maxOutputTokens": 16}
    ((url, body),) = opener.sent
    assert url.endswith("/v1beta/models/gemini-3.8-flash:generateContent")
    assert body == GEMINI_PAYLOAD  # 모델은 본문이 아니라 경로에 실린다
    assert ledger.responses == [reply]
    assert reply["responseId"] == "resp_synthetic"


def test_gemini_cancellation_gates_the_send() -> None:
    from vlogsrt.transcribe import request_gemini_generate

    class Stop(BaseException):
        pass

    def gate() -> None:
        raise Stop

    opener = _GeminiOpener()
    ledger = _ClaudeLedger([])
    for observer in (ledger, None):  # 관찰자 없는 길도 전송 전에 멈춘다
        with pytest.raises(Stop):
            request_gemini_generate(
                _gemini_client(opener),
                model=GEMINI_MODEL,
                payload=GEMINI_PAYLOAD,
                online=True,
                observer=observer,
                cancellation=gate,
            )
    assert opener.sent == [] and ledger.metas == []


def test_create_gemini_client_takes_only_given_values() -> None:
    from vlogsrt.transcribe import create_gemini_client, is_claude_client, is_gemini_client

    client = create_gemini_client(
        api_key="AIza-synthetic",
        base_url="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        timeout_seconds=300.0,
    )
    assert is_gemini_client(client) and not is_claude_client(client)
    assert not is_gemini_client(object())
    assert not is_gemini_client(_claude_client(_ClaudeOpener()))


def test_create_claude_client_takes_only_given_values() -> None:
    from vlogsrt.transcribe import create_claude_client, is_claude_client

    client = create_claude_client(
        api_key="sk-ant-synthetic",
        base_url="https://api.anthropic.com",
        api_version="2023-06-01",
        timeout_seconds=300.0,
    )
    assert is_claude_client(client)
    assert not is_claude_client(object())
    assert (client.base_url, client.api_version, client.timeout_seconds) == (
        "https://api.anthropic.com",
        "2023-06-01",
        300.0,
    )
    assert "sk-ant-synthetic" not in repr(client)


# ─── Gemini 3.5 Transcribe 경계(W-31a, D92) — 시간 엔진의 문 ─────────────────

TRANSCRIBE_MODEL = "gemini-3.5-transcribe"


class _InteractionReply(_ClaudeReply):
    def read(self) -> bytes:
        return b'{"id": "int_synthetic", "status": "completed", "output_text": ""}'


class _InteractionOpener:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: Any, timeout: float) -> _InteractionReply:
        self.sent.append((request.full_url, json.loads(request.data)))
        return _InteractionReply()


def _wav_chunk(tmp_path: Path, payload: bytes = b"RIFF-synthetic-wav", **values: Any) -> Any:
    from vlogsrt.models import Chunk

    path = tmp_path / "asr-chunk-0000.wav"
    path.write_bytes(payload)
    fields = {"path": path, "offset": 12.5, "duration": 30.0, "index": 3}
    fields.update(values)
    return Chunk(**fields)


def _transcription(chunk: Any, **values: Any) -> Any:
    from vlogsrt.transcribe import GeminiTranscriptionRequest

    fields: dict[str, Any] = {
        "model": TRANSCRIBE_MODEL,
        "chunk": chunk,
        "language_codes": ("ko-KR",),
        "max_audio_seconds": 900.0,
        "max_request_bytes": 20_000_000,
    }
    fields.update(values)
    return GeminiTranscriptionRequest(**fields)


def _send_transcription(opener: Any, request: Any, **values: Any) -> Any:
    from vlogsrt.transcribe import request_gemini_transcription

    return request_gemini_transcription(_gemini_client(opener), request=request, **values)


def test_gemini_transcription_is_closed_without_explicit_online(tmp_path: Path) -> None:
    opener = _InteractionOpener()
    for values in ({}, {"online": False}):
        with pytest.raises(RuntimeError, match="online=True"):
            _send_transcription(opener, _transcription(_wav_chunk(tmp_path)), **values)
    assert opener.sent == []


def test_gemini_transcription_sends_inline_wav_with_word_times_and_no_storage(
    tmp_path: Path,
) -> None:
    import base64
    import hashlib

    from vlogsrt.transcribe import CAPTION_TIMING_KIND, GEMINI_TRANSCRIBE_OPERATION

    trace: list[str] = []
    opener = _InteractionOpener()
    ledger = _ClaudeLedger(trace)
    audio = b"RIFF-synthetic-wav"
    reply = _send_transcription(
        opener,
        _transcription(_wav_chunk(tmp_path, audio)),
        online=True,
        observer=ledger,
        stem="IMG_8875",
        source="src-1",
    )
    ((url, body),) = opener.sent
    assert url.endswith("/v1beta/interactions")
    assert body == {
        "model": TRANSCRIBE_MODEL,
        "store": False,
        "input": [
            {
                "type": "audio",
                "data": base64.b64encode(audio).decode("ascii"),
                "mime_type": "audio/wav",
            }
        ],
        "generation_config": {
            "transcription_config": {
                "language_codes": ["ko-KR"],
                "mode": {"type": "verbatim", "timestamp_granularities": ["word"]},
            }
        },
    }
    assert trace == ["authorize", "response"] and reply["status"] == "completed"
    (meta,) = ledger.metas
    assert meta.operation == GEMINI_TRANSCRIBE_OPERATION == "interactions.transcribe"
    assert (meta.model, meta.kind, meta.stem, meta.source) == (
        TRANSCRIBE_MODEL,
        CAPTION_TIMING_KIND,
        "IMG_8875",
        "src-1",
    )
    assert (meta.chunk_index, meta.chunk_offset, meta.chunk_duration) == (3, 12.5, 30.0)
    # 관찰 기록은 오디오를 다시 싣지 않는다 — 지문과 크기뿐이다
    assert meta.options["input"] == [
        {
            "type": "audio",
            "mime_type": "audio/wav",
            "sha256": hashlib.sha256(audio).hexdigest(),
            "bytes": len(audio),
        }
    ]
    assert "data" not in json.dumps(dict(meta.options), default=str).replace('"bytes"', "")


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"model": "gemini-3.5-transcribe:x"}, "모델"),
        ({"language_codes": ()}, "언어"),
        ({"max_audio_seconds": 10.0}, "길이"),
        ({"max_request_bytes": 10}, "크기"),
    ],
)
def test_a_local_transcription_defect_fails_before_authorization(
    tmp_path: Path, values: dict[str, Any], match: str
) -> None:
    opener = _InteractionOpener()
    ledger = _ClaudeLedger([])
    with pytest.raises(ValueError, match=match):
        _send_transcription(
            opener, _transcription(_wav_chunk(tmp_path), **values), online=True, observer=ledger
        )
    assert ledger.metas == [] and opener.sent == []


def test_a_non_gemini_client_cannot_transcribe(tmp_path: Path) -> None:
    from vlogsrt.transcribe import request_gemini_transcription

    ledger = _ClaudeLedger([])
    with pytest.raises(TypeError, match="Gemini"):
        request_gemini_transcription(
            _claude_client(_ClaudeOpener()),
            request=_transcription(_wav_chunk(tmp_path)),
            online=True,
            observer=ledger,
        )
    assert ledger.metas == []


def test_gemini_transcription_cancellation_gates_the_send(tmp_path: Path) -> None:
    class Stop(BaseException):
        pass

    def gate() -> None:
        raise Stop

    opener = _InteractionOpener()
    ledger = _ClaudeLedger([])
    for observer in (ledger, None):
        with pytest.raises(Stop):
            _send_transcription(
                opener,
                _transcription(_wav_chunk(tmp_path)),
                online=True,
                observer=observer,
                cancellation=gate,
            )
    assert opener.sent == [] and ledger.metas == []
