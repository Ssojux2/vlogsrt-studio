"""Gemini 429 재시도(W-81, D141) — 공급자가 대기 시간을 말한 분당 한도만 기다렸다 다시 보낸다.

재시도는 문(`transcribe.request_gemini_generate`·`request_gemini_transcription`)에 있고 전송
(`gemini_messages`)은 여전히 요청 한 번이다. 시도마다 관찰 승인·취소 문을 다시 지난다. 대기는
가짜 잠으로 갈아 끼운다 — 시험은 실제로 자지 않는다.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from tests.test_anthropic_messages import FakeResponse
from vlogsrt import transcribe
from vlogsrt.config import GEMINI_RATE_RETRY
from vlogsrt.gemini_messages import GeminiAPIStatusError, GeminiClient
from vlogsrt.transcribe import request_gemini_generate

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic" / "gemini"
MODEL = "gemini-3.8-flash"
TRANSCRIBE_MODEL = "gemini-3.5-transcribe"
PAYLOAD = {"contents": [{"role": "user", "parts": [{"text": "합성"}]}]}
OK = b'{"candidates": [], "responseId": "resp_synthetic"}'


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _quota_429(quota_id: str, delay: str | None) -> bytes:
    details: list[dict[str, Any]] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": quota_id, "quotaValue": "10"}],
        }
    ]
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    error = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": details}
    return json.dumps({"error": error}).encode()


MINUTE = "GenerateRequestsPerMinutePerProjectPerModel"
DAY = "GenerateRequestsPerDayPerProjectPerModel"


class ScriptedOpener:
    """정해진 차례로 답한다 — (상태, 본문, 헤더). 마지막 답은 되풀이한다."""

    def __init__(self, *replies: tuple[int, bytes, dict[str, str]]) -> None:
        self.replies = list(replies)
        self.sent = 0

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        status, body, headers = self.replies[min(self.sent, len(self.replies) - 1)]
        self.sent += 1
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "error",
                headers,  # type: ignore[arg-type]
                io.BytesIO(body),
            )
        return FakeResponse(status, body, headers)


def _client(opener: ScriptedOpener) -> GeminiClient:
    return GeminiClient(
        api_key="AIza-synthetic",
        base_url="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        timeout_seconds=30.0,
        opener=opener,
    )


class Ledger:
    def __init__(self) -> None:
        self.trace: list[str] = []

    def authorize(self, meta: Any) -> str:
        self.trace.append("authorize")
        return f"ticket-{len(self.trace)}"

    def on_response(self, ticket: object, meta: Any, response: Any) -> None:
        self.trace.append("response")

    def on_error(self, ticket: object, meta: Any, error: Exception) -> None:
        self.trace.append(f"error {getattr(error, 'status_code', '')}")


@pytest.fixture
def naps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """가짜 잠 — 잔 길이를 적기만 한다."""
    slept: list[float] = []
    monkeypatch.setattr(transcribe, "_rate_sleep", slept.append)
    return slept


def _generate(opener: ScriptedOpener, **gates: Any) -> dict[str, Any]:
    return request_gemini_generate(
        _client(opener), model=MODEL, payload=PAYLOAD, online=True, **gates
    )


# ─────────────────────────────────────────────────────────────
#  기다렸다 다시 보내는 것 — 분당 한도 · 공급자가 말한 대기
# ─────────────────────────────────────────────────────────────


def test_a_per_minute_429_waits_the_told_delay_then_sends_again(naps: list[float]) -> None:
    opener = ScriptedOpener((429, _fixture("error_429_quota_minute.json"), {}), (200, OK, {}))
    ledger = Ledger()
    reply = _generate(opener, observer=ledger)
    assert reply["responseId"] == "resp_synthetic"
    assert opener.sent == 2
    # 시도마다 승인을 새로 받는다 — 429 시도는 오류로, 다시 보낸 시도는 응답으로 남는다
    assert ledger.trace == ["authorize", "error 429", "authorize", "response"]
    assert sum(naps) == pytest.approx(12.4)
    assert max(naps) <= GEMINI_RATE_RETRY.poll_seconds


def test_the_retry_after_header_is_a_told_delay_too(naps: list[float]) -> None:
    opener = ScriptedOpener((429, _fixture("error_429.json"), {"Retry-After": "7"}), (200, OK, {}))
    assert _generate(opener)["responseId"] == "resp_synthetic"  # 관찰자 없는 길도 같다
    assert opener.sent == 2
    assert sum(naps) == pytest.approx(7.0)


def test_retries_stop_at_the_configured_count(naps: list[float]) -> None:
    opener = ScriptedOpener((429, _quota_429(MINUTE, "2s"), {}))
    with pytest.raises(GeminiAPIStatusError) as info:
        _generate(opener, observer=Ledger())
    assert info.value.status_code == 429
    assert opener.sent == GEMINI_RATE_RETRY.max_retries + 1
    assert sum(naps) == pytest.approx(2.0 * GEMINI_RATE_RETRY.max_retries)


def test_the_transcription_gate_waits_the_same_way(naps: list[float], tmp_path: Path) -> None:
    from vlogsrt.models import Chunk
    from vlogsrt.transcribe import GeminiTranscriptionRequest, request_gemini_transcription

    path = tmp_path / "asr-chunk-0000.wav"
    path.write_bytes(b"RIFF-synthetic-wav")
    request = GeminiTranscriptionRequest(
        model=TRANSCRIBE_MODEL,
        chunk=Chunk(path=path, offset=12.5, duration=30.0, index=3),
        language_codes=("ko-KR",),
        max_audio_seconds=900.0,
        max_request_bytes=20_000_000,
    )
    done = b'{"id": "int_synthetic", "status": "completed", "output_text": ""}'
    opener = ScriptedOpener((429, _quota_429(MINUTE, "3s"), {}), (200, done, {}))
    reply = request_gemini_transcription(
        _client(opener), request=request, online=True, observer=Ledger()
    )
    assert reply["id"] == "int_synthetic"
    assert opener.sent == 2 and sum(naps) == pytest.approx(3.0)


# ─────────────────────────────────────────────────────────────
#  다시 보내지 않는 것 — 예전처럼 한 번의 명시적 실패
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (429, _fixture("error_429.json"), {}),  # 대기를 말하지 않았다
        (429, _fixture("error_429_quota_day.json"), {}),  # 하루 한도 — 43초 뒤에도 풀리지 않는다
        (429, _quota_429(MINUTE, "120s"), {}),  # 상한보다 긴 대기
        (429, _fixture("error_429.json"), {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        (503, _fixture("error_503.json"), {"Retry-After": "5"}),  # 과부하·불명확은 그대로
        (400, _quota_429(MINUTE, "2s"), {}),
    ],
)
def test_other_failures_are_still_sent_once(
    naps: list[float], status: int, body: bytes, headers: dict[str, str]
) -> None:
    opener = ScriptedOpener((status, body, headers), (200, OK, {}))
    with pytest.raises(GeminiAPIStatusError):
        _generate(opener, observer=Ledger())
    assert opener.sent == 1 and naps == []


def test_a_day_quota_is_never_waited_out_even_with_a_short_delay(naps: list[float]) -> None:
    opener = ScriptedOpener((429, _quota_429(DAY, "2s"), {}), (200, OK, {}))
    with pytest.raises(GeminiAPIStatusError):
        _generate(opener)
    assert opener.sent == 1 and naps == []


def test_cancelling_during_the_wait_sends_nothing_more(naps: list[float]) -> None:
    class Stop(BaseException):
        pass

    calls = 0

    def gate() -> None:
        nonlocal calls
        calls += 1
        if naps:  # 기다리기 시작한 뒤의 첫 확인에서 멈춘다
            raise Stop

    opener = ScriptedOpener((429, _quota_429(MINUTE, "30s"), {}), (200, OK, {}))
    with pytest.raises(Stop):
        _generate(opener, observer=Ledger(), cancellation=gate)
    assert opener.sent == 1
    assert sum(naps) <= GEMINI_RATE_RETRY.poll_seconds  # 30초를 다 자지 않는다


def test_cancelling_in_the_task_scope_also_stops_the_wait(naps: list[float]) -> None:
    """문이 취소 확인을 받지 않은 호출(맥락 평가)도 작업 범위의 취소로 멈춘다."""
    from vlogsrt.task_runtime import cancellation_scope

    class Stop(BaseException):
        pass

    def check() -> None:
        if naps:
            raise Stop

    opener = ScriptedOpener((429, _quota_429(MINUTE, "30s"), {}), (200, OK, {}))
    with cancellation_scope(check), pytest.raises(Stop):
        _generate(opener)
    assert opener.sent == 1
