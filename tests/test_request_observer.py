"""요청 관찰 경계(S2B-2a) — 실제 SDK 호출 하나하나를 겉에서 본다.

가짜 SDK는 `LiveTranscriber._client`와 `request_luna_vision`의 클라이언트 자리에
직접 들어가고, 관찰자는 승인·응답·오류 훅을 기록하는 가짜 원장이다. 네트워크는
없다. 훅·SDK 호출·변환이 어떤 순서로 일어나는지는 공유 사건 기록(trace)으로
본다 — 호출 뒤의 개수 세기만으로는 순서가 보이지 않기 때문이다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as data_replace
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import env, transcribe
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.transcribe import (
    ARBITER_KIND,
    FIXTURE_KIND,
    RESCUE_KIND,
    RESPONSES_OPERATION,
    TRANSCRIPTION_OPERATION,
    VERIFY_KIND,
    FixtureTranscriber,
    LiveTranscriber,
    ObserverFailure,
    RequestAttemptMeta,
    RequestObserver,
    TranscriptionJob,
    get_transcriber,
    request_luna_vision,
)

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"


# ─────────────────────────────────────────────────────────────
#  가짜 — SDK는 실제 호출 자리에, 관찰자는 훅을 기록한다
# ─────────────────────────────────────────────────────────────


class SdkReply:
    """SDK 응답 객체의 최소 모습 — `model_dump`로 사전이 된다(변환 시점을 남긴다)."""

    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def model_dump(self) -> dict[str, Any]:
        self.trace.append("convert")
        return {"segments": [{"start": 0.0, "end": 1.0, "text": " 망원동"}]}


class BrokenReply:
    """변환 단계에서 깨지는 응답 — 응답 훅이 먼저 지나갔는지를 본다."""

    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def model_dump(self) -> dict[str, Any]:
        self.trace.append("convert")
        raise ValueError("깨진 응답")


class Cancelled(BaseException):
    """프로젝트 계층의 취소와 같은 부류 — `except Exception`에 잡히지 않는다."""


class FakeAudioSDK:
    """`audio.transcriptions.create` 자리의 가짜 SDK. 인자·순서·결과를 기록한다."""

    def __init__(
        self,
        trace: list[str],
        failures: list[Exception] | None = None,
        reject_parameter: str | None = None,
        reply_factory: Callable[[list[str]], object] | None = None,
        barrier_after_first: int = 0,
    ) -> None:
        self.trace = trace
        self.calls: list[dict[str, object]] = []
        self.failures = list(failures or ())
        self.reject_parameter = reject_parameter
        self.reply_factory = reply_factory
        self.audio = self
        self.transcriptions = self
        # 머리 호출(1번째) 뒤의 호출들이 파티 수만큼 동시에 걸리는 장치 — "이미
        # 입장한 병렬 작업"을 결정적으로 만든다. 어긋나면 파열로 죽지 교착하지 않는다.
        self._barrier = threading.Barrier(barrier_after_first) if barrier_after_first else None

    def create(self, **kwargs: object) -> object:
        self.trace.append("sdk-call")
        self.calls.append(kwargs)
        if self._barrier is not None and len(self.calls) > 1:
            self._barrier.wait(timeout=20)
        if self.reject_parameter is not None and self.reject_parameter in kwargs:
            raise status_error(400, f"unknown parameter '{self.reject_parameter}'")
        if self.failures:
            raise self.failures.pop(0)
        if self.reply_factory is not None:
            return self.reply_factory(self.trace)
        return SdkReply(self.trace)


class CancellingSDK(FakeAudioSDK):
    """SDK 호출 안에서 취소(BaseException)가 터지는 모양."""

    def create(self, **kwargs: object) -> object:
        self.trace.append("sdk-call")
        self.calls.append(kwargs)
        raise Cancelled


class FakeResponsesSDK:
    """`responses.create` 자리의 가짜 SDK — Luna 경계용. 같은 방식으로 기록한다."""

    def __init__(self, trace: list[str], failures: list[Exception] | None = None) -> None:
        self.trace = trace
        self.calls: list[dict[str, object]] = []
        self.failures = list(failures or ())
        self.responses = self

    def create(self, **kwargs: object) -> object:
        self.trace.append("sdk-call")
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return SdkReply(self.trace)


@dataclass
class RecordingObserver:
    """가짜 원장. 훅 세 개를 받은 순서 그대로 사건을 남긴다 — 티켓은 승인 순번이다.

    `on_authorize`는 승인 시점의 메타를 만질 수 있는 구멍이다 — 사본이 정말
    방어적인지(나가는 요청이 변하지 않는지)를 보는 데만 쓴다. `fail_chunks`·
    `fail_response_chunks`는 특정 조각에서 원장이 고장나는 모양을 만든다.
    """

    trace: list[str] = field(default_factory=list)
    authorize_failures: list[Exception] = field(default_factory=list)
    response_failure: Exception | None = None
    error_failure: Exception | None = None
    on_authorize: Callable[[RequestAttemptMeta], None] | None = None
    fail_chunks: set[int] = field(default_factory=set)
    fail_response_chunks: set[int] = field(default_factory=set)
    authorized: list[RequestAttemptMeta] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    outcomes: list[tuple[str, str, str]] = field(default_factory=list)
    raw_responses: list[object] = field(default_factory=list)

    def authorize(self, meta: RequestAttemptMeta) -> str:
        self.trace.append("authorize")
        self.authorized.append(meta)
        if self.on_authorize is not None:
            self.on_authorize(meta)
        if meta.chunk_index is not None and meta.chunk_index in self.fail_chunks:
            raise RuntimeError(f"{meta.chunk_index}번 조각의 원장 승인이 실패했다")
        if self.authorize_failures:
            raise self.authorize_failures.pop(0)
        ticket = f"ticket-{len(self.tickets)}"
        self.tickets.append(ticket)
        return ticket

    def on_response(self, ticket: Any, meta: RequestAttemptMeta, response: object) -> None:
        self.trace.append("response")
        self.raw_responses.append(response)
        if meta.chunk_index is not None and meta.chunk_index in self.fail_response_chunks:
            raise RuntimeError(f"{meta.chunk_index}번 조각의 응답 기록이 실패했다")
        if self.response_failure is not None:
            raise self.response_failure
        self.outcomes.append(("response", ticket, meta.attempt_id))

    def on_error(self, ticket: Any, meta: RequestAttemptMeta, error: Exception) -> None:
        self.trace.append("error")
        if self.error_failure is not None:
            raise self.error_failure
        self.outcomes.append(("error", ticket, meta.attempt_id))


def status_error(status: int, message: str) -> RuntimeError:
    """SDK 오류의 최소 모습 — `status_code`를 달고 있다."""
    error = RuntimeError(f"Error code: {status} - {message}")
    error.status_code = status  # type: ignore[attr-defined]
    return error


def one_chunk_job(tmp_path: Path, kind: str = FIXTURE_KIND) -> TranscriptionJob:
    """조각 하나짜리 실제 호출 작업. 파일은 SDK 호출 직전에 열린다."""
    audio = tmp_path / "demo.mp3"
    audio.write_bytes(b"\x00")
    chunk = Chunk(audio, 0.0, 24.0, 0)
    return TranscriptionJob(stem="demo", source="golden/demo.mp4", chunks=(chunk,), kind=kind)


def multi_chunk_job(tmp_path: Path, count: int) -> TranscriptionJob:
    """조각 여럿짜리 작업 — 머리 하나에 꼬리 count-1개가 병렬로 붙는다."""
    audio = tmp_path / "demo.mp3"
    audio.write_bytes(b"\x00")
    return TranscriptionJob(
        stem="demo",
        source="golden/demo.mp4",
        chunks=tuple(Chunk(audio, index * 24.0, 24.0, index) for index in range(count)),
    )


def live_with(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeAudioSDK,
    observer: RequestObserver | None,
    cancellation: Callable[[], None] | None = None,
) -> LiveTranscriber:
    """`LiveTranscriber`는 frozen+slots라 클라이언트를 클래스에 꽂는다(기존 관례)."""
    monkeypatch.setattr(LiveTranscriber, "_client", lambda self: client)
    return LiveTranscriber(api_key="sk-test", cancellation=cancellation, observer=observer)


def unique_ids(ledger: RecordingObserver) -> list[str]:
    return [meta.attempt_id for meta in ledger.authorized]


# ─────────────────────────────────────────────────────────────
#  세로 조각 — 순서가 곧 계약이다
# ─────────────────────────────────────────────────────────────


def test_each_actual_audio_call_is_authorized_once_and_reported_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """승인 → SDK 호출 → 응답 훅(원본) → 변환. 사건 기록이 그 순서를 못 박는다."""
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace)
    live = live_with(monkeypatch, client, ledger)

    responses = live.transcribe(one_chunk_job(tmp_path), Config())

    assert trace == ["authorize", "sdk-call", "response", "convert"]
    assert len(client.calls) == 1
    assert len(responses) == 1
    assert responses[0].payload["segments"][0]["text"] == " 망원동"
    (meta,) = ledger.authorized
    assert meta.operation == TRANSCRIPTION_OPERATION
    assert meta.model == Config().transcribe_model
    assert meta.kind == FIXTURE_KIND
    assert meta.source == "golden/demo.mp4" and meta.stem == "demo"
    assert meta.chunk_index == 0 and meta.chunk_offset == 0.0 and meta.chunk_duration == 24.0
    assert meta.attempt_id  # 시도마다 새 신원이 붙는다
    assert "file" not in meta.options  # 파일 핸들은 인자 사본에 없다
    assert meta.options["response_format"] == Config().transcribe_format
    assert isinstance(ledger.raw_responses[0], SdkReply)  # 사전이 아니라 SDK 객체다
    assert [stage for stage, _, _ in ledger.outcomes] == ["response"]
    assert ledger.outcomes[0][1] == ledger.tickets[0]  # 같은 티켓이 짝으로 돌아온다


def test_luna_responses_call_is_authorized_and_reported_in_order() -> None:
    """Responses 경계도 같은 순서다 — 승인 게이트를 지난 뒤에만 훅이 움직인다."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace)

    converted = request_luna_vision(
        client, model="vision-model-x", payload={"input": []}, online=True, observer=ledger
    )

    assert trace == ["authorize", "sdk-call", "response", "convert"]
    assert client.calls == [{"model": "vision-model-x", "input": []}]
    assert converted["segments"][0]["text"] == " 망원동"
    (meta,) = ledger.authorized
    assert meta.operation == RESPONSES_OPERATION
    assert meta.model == "vision-model-x"
    assert meta.kind == "" and meta.chunk_index is None
    assert dict(meta.options) == {"model": "vision-model-x", "input": []}
    assert isinstance(ledger.raw_responses[0], SdkReply)


def test_a_before_hook_failure_prevents_the_paid_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """승인 훅이 실패하면 SDK를 부르지 않는다 — 오류 훅도 오지 않는다.

    실패한 관찰자 예외가 429을 달고 있어도 재시도로 취급되지 않는다(요건 5).
    """
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    poisoned = status_error(429, "원장이 이미 치명 상태다")
    ledger = RecordingObserver(trace, authorize_failures=[poisoned])
    live = live_with(monkeypatch, client, ledger)

    with pytest.raises(ObserverFailure) as excinfo:
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert trace == ["authorize"]  # 승인에서 끝났다 — SDK 호출도 변환도 없다
    assert client.calls == []
    assert ledger.outcomes == []  # 관찰자 자신의 실패에 오류 훅을 재사용하지 않는다
    assert excinfo.value.__cause__ is poisoned  # 원인 예외는 그대로 보존된다
    assert ledger.authorized[0].attempt_id


def test_the_default_meta_options_are_independent_and_read_only() -> None:
    """빈 기본값도 시도마다 따로이고 읽기 전용이다(3.11 호환: default_factory)."""
    first = RequestAttemptMeta(attempt_id="a", operation=RESPONSES_OPERATION, model="m")
    second = RequestAttemptMeta(attempt_id="b", operation=RESPONSES_OPERATION, model="m")

    assert dict(first.options) == {} and dict(second.options) == {}
    assert first.options is not second.options
    with pytest.raises(TypeError):
        first.options["model"] = "x"


def test_the_snapshot_cannot_change_the_outbound_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """메타의 인자 사본은 방어적이다 — 관찰자가 만져도 나가는 요청은 그대로다."""

    def mutate(meta: RequestAttemptMeta) -> None:
        meta.options["keywords"].append("몰래 끼워넣기")  # 겉의 사전을 뚫고 안의 리스트까지

    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace, on_authorize=mutate)
    live = live_with(monkeypatch, client, ledger)
    cfg = data_replace(Config(), keywords=("블루펭귄",))

    live.transcribe(one_chunk_job(tmp_path, kind=VERIFY_KIND), cfg)

    (call,) = client.calls
    assert call["keywords"] == ["블루펭귄"]  # 나간 요청은 한 글자도 안 바뀌었다
    assert ledger.authorized[0].options["keywords"] == ["블루펭귄", "몰래 끼워넣기"]
    with pytest.raises(TypeError):  # 사본 자체도 읽기 전용이다
        ledger.authorized[0].options["model"] = "x"


# ─────────────────────────────────────────────────────────────
#  재시도 · 400 인자 축소 — 시도마다 새 신원
# ─────────────────────────────────────────────────────────────


def test_each_retry_attempt_gets_a_fresh_identity_and_reports_before_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """오류 훅 → 백오프 → 다음 승인 순서다. 대기는 기록으로 갈음한다(정책은 그대로)."""
    trace: list[str] = []
    client = FakeAudioSDK(trace, failures=[status_error(429, "한도"), status_error(429, "한도")])
    ledger = RecordingObserver(trace)
    live = live_with(monkeypatch, client, ledger)
    monkeypatch.setattr("vlogsrt.transcribe.time.sleep", lambda seconds: trace.append("backoff"))

    live.transcribe(one_chunk_job(tmp_path), Config())

    assert len(client.calls) == 3
    ids = unique_ids(ledger)
    assert len(set(ids)) == 3  # 시도마다 새 신원이다
    assert [stage for stage, _, _ in ledger.outcomes] == ["error", "error", "response"]
    assert [ticket for _, ticket, _ in ledger.outcomes] == ledger.tickets  # 짝이 맞는 티켓
    assert trace == [
        "authorize",
        "sdk-call",
        "error",
        "backoff",
        "authorize",
        "sdk-call",
        "error",
        "backoff",
        "authorize",
        "sdk-call",
        "response",
        "convert",
    ]


def test_a_400_argument_reduction_is_a_new_attempt_with_reduced_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """떼고 다시 부르는 재요청은 다른 시도다 — 사본으로 구분된다(요건 3)."""
    trace: list[str] = []
    client = FakeAudioSDK(trace, reject_parameter="language")
    ledger = RecordingObserver(trace)
    live = live_with(monkeypatch, client, ledger)

    live.transcribe(one_chunk_job(tmp_path), Config())

    assert len(client.calls) == 2
    first, second = ledger.authorized
    assert "language" in first.options and "language" not in second.options
    assert first.attempt_id != second.attempt_id  # 축소 재요청도 새 시도다
    assert [stage for stage, _, _ in ledger.outcomes] == ["error", "response"]


@pytest.mark.parametrize(
    "kind, model_field",
    [
        (FIXTURE_KIND, "transcribe_model"),
        (RESCUE_KIND, "transcribe_model"),
        (VERIFY_KIND, "verify_model"),
        (ARBITER_KIND, "verify_model"),
    ],
)
def test_meta_reports_the_actual_kind_and_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, model_field: str
) -> None:
    """작업 종류마다 실제로 나가는 모델이 메타에 그대로 비친다(요건 2)."""
    client = FakeAudioSDK([])
    ledger = RecordingObserver([])
    live = live_with(monkeypatch, client, ledger)

    live.transcribe(one_chunk_job(tmp_path, kind=kind), Config())

    (meta,) = ledger.authorized
    assert meta.kind == kind
    assert meta.model == getattr(Config(), model_field)


# ─────────────────────────────────────────────────────────────
#  관찰자 실패 격리 — SDK 오류로 위장해도 재시도가 아니다
# ─────────────────────────────────────────────────────────────


def test_a_response_hook_failure_carrying_400_is_not_an_argument_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """응답 훅이 400을 달고 실패해도 인자를 떼고 다시 부르는 길이 열리지 않는다."""
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace, response_failure=status_error(400, "원장 기록 실패"))
    live = live_with(monkeypatch, client, ledger)

    with pytest.raises(ObserverFailure):
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert len(client.calls) == 1  # 400 인자 정리 재요청이 아니다
    assert ledger.authorized and ledger.outcomes == []


def test_an_error_hook_failure_carrying_429_is_not_a_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """오류 훅이 429를 달고 실패해도 그 아래 SDK 오류의 재시도로 이어지지 않는다."""
    trace: list[str] = []
    client = FakeAudioSDK(trace, failures=[status_error(429, "한도")])
    ledger = RecordingObserver(trace, error_failure=status_error(429, "원장 오류 기록 실패"))
    live = live_with(monkeypatch, client, ledger)
    cfg = data_replace(Config(), api_backoff_base=0.0)

    with pytest.raises(ObserverFailure):
        live.transcribe(one_chunk_job(tmp_path), cfg)

    assert len(client.calls) == 1  # 재시도가 아니다
    assert len(ledger.authorized) == 1


def test_an_sdk_class_name_disguise_is_not_a_connection_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """관찰자 예외가 SDK 오류와 같은 이름이어도 연결 재시도로 취급되지 않는다."""

    class APIConnectionError(Exception):
        """SDK 오류와 같은 이름 — 그러나 관찰자가 던진 것이다(요건 5)."""

    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace, authorize_failures=[APIConnectionError("원장 연결 끊김")])
    live = live_with(monkeypatch, client, ledger)

    with pytest.raises(ObserverFailure):
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert client.calls == []
    assert len(ledger.authorized) == 1  # 재시도(재승인)도 없다


def test_an_sdk_error_report_failure_preserves_both_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """오류 훅이 실패해도 원인 사슬에 SDK 오류가 그대로 남는다(요건 5)."""
    trace: list[str] = []
    sdk_error = status_error(500, "서버 오류")
    client = FakeAudioSDK(trace, failures=[sdk_error])
    ledger = RecordingObserver(trace, error_failure=RuntimeError("원장 오류 기록 실패"))
    live = live_with(monkeypatch, client, ledger)
    cfg = data_replace(Config(), api_backoff_base=0.0)

    with pytest.raises(ObserverFailure) as excinfo:
        live.transcribe(one_chunk_job(tmp_path), cfg)

    hook_failure = excinfo.value.__cause__
    assert isinstance(hook_failure, RuntimeError)  # 훅의 실패가 직접 원인이다
    assert hook_failure.__context__ is sdk_error  # 그 아래에 SDK 오류가 있다


# ─────────────────────────────────────────────────────────────
#  취소 — 승인보다 게이트가 먼저, BaseException은 그대로 올라간다
# ─────────────────────────────────────────────────────────────


def test_the_cancellation_gate_precedes_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """게이트가 던지면 승인도 SDK 호출도 없다(요건 3)."""
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace)

    def gate() -> None:
        raise Cancelled

    live = live_with(monkeypatch, client, ledger, cancellation=gate)

    with pytest.raises(Cancelled):
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert trace == []
    assert ledger.authorized == [] and client.calls == []


def test_a_base_exception_from_the_sdk_call_is_not_reported_as_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SDK 호출 안의 취소는 오류 훅 없이 올라간다 — 예약은 보수적으로 남는다."""
    trace: list[str] = []
    client = CancellingSDK(trace)
    ledger = RecordingObserver(trace)
    live = live_with(monkeypatch, client, ledger)

    with pytest.raises(Cancelled):
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert trace == ["authorize", "sdk-call"]
    assert len(ledger.authorized) == 1 and ledger.outcomes == []


def test_cancellation_during_local_preparation_never_authorizes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """지역 준비가 한창인 동안 취소가 켜지면 승인도 SDK도 없다(S1 순서)."""
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace)
    prepared: list[bool] = []

    def slow_open(chunk: Chunk) -> object:
        handle = chunk.path.open("rb")  # 진짜 준비가 일어났다
        prepared.append(True)  # 그 한가운데에 취소가 켜진다
        return handle

    def gate() -> None:
        if prepared:
            raise Cancelled

    monkeypatch.setattr("vlogsrt.transcribe._open_audio", slow_open)
    live = live_with(monkeypatch, client, ledger, cancellation=gate)

    with pytest.raises(Cancelled):
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert prepared  # 준비 **뒤의** 확인에서 멈췄다 — 시간 배치의 증명
    assert ledger.authorized == [] and client.calls == []


def test_cancellation_during_authorize_blocks_the_sdk_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """승인이 늦어지는 동안 취소가 오면 SDK를 부르지 않는다 — 티켓은 불확실성으로 남는다."""
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    armed: list[bool] = []

    def gate() -> None:
        if armed:
            raise Cancelled

    def arm(meta: RequestAttemptMeta) -> None:
        armed.append(True)  # 관찰자 승인이 한창인 동안 취소가 켜진다

    ledger = RecordingObserver(trace, on_authorize=arm)
    live = live_with(monkeypatch, client, ledger, cancellation=gate)

    with pytest.raises(Cancelled):
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert len(ledger.authorized) == 1  # 승인은 일어났고
    assert len(ledger.tickets) == 1  # 티켓도 나왔다
    assert client.calls == []  # 그러나 SDK는 안 부른다
    assert ledger.outcomes == []  # 오류 훅 없음 — 예약은 불확실성으로 남는다


def test_cancellation_while_waiting_for_admission_stops_the_queued_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """입장 줄에 선 시도는 잠금을 얻는 순간 취소를 새로 본다 — 승인은 일어나지 않는다.

    줄 서기를 결정적으로 만드는 신호 셋: 첫 꼬리 승인이 잠금을 잡았다(
    first_in_authorize), 둘째 꼬리가 문 앞에 왔다(third_open_reached), 잠금 앞에
    실제로 섰다(queued_at_admission). 입장(admit) 호출 수를 세는 좁은 계측만 단다.
    """
    monkeypatch.setattr(env, "max_workers", lambda: 2)
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    cancelled = threading.Event()
    first_in_authorize = threading.Event()
    third_open_reached = threading.Event()
    let_third_through = threading.Event()
    queued_at_admission = threading.Event()
    release = threading.Event()
    admissions: list[int] = []
    opens: list[int] = []
    opens_lock = threading.Lock()
    admit_calls: list[int] = []

    def sequenced_open(chunk: Chunk) -> object:
        with opens_lock:
            opens.append(len(opens) + 1)
            count = len(opens)
        if count == 3:  # 둘째 꼬리 — 첫 승인이 잠금을 잡은 뒤에만 입장을 시도한다
            third_open_reached.set()
            if not let_third_through.wait(timeout=10):
                raise RuntimeError("둘째 꼬리 줄세우기가 10초 안에 풀리지 않았다")
        return chunk.path.open("rb")

    real_admit = transcribe._AttemptWatch.admit

    def instrumented_admit(watch: object, make_meta: object, gate: object = None) -> object:
        admit_calls.append(len(admit_calls) + 1)
        if len(admit_calls) == 3:
            queued_at_admission.set()  # 잠금이 잡혀 있는 동안 들어오는 입장은 줄이다
        return real_admit(watch, make_meta, gate)

    class QueueLedger(RecordingObserver):
        """먼저 온 꼬리 승인은 잠금을 잡은 채 기다린다 — 나머지는 그 뒤에 줄을 선다."""

        def authorize(self, meta: RequestAttemptMeta) -> str:
            admissions.append(meta.chunk_index)
            if meta.chunk_index != 0 and not release.is_set():
                first_in_authorize.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("입장 잠금이 10초 안에 풀리지 않았다")
            return super().authorize(meta)

    def gate() -> None:
        if cancelled.is_set():
            raise Cancelled

    monkeypatch.setattr("vlogsrt.transcribe._open_audio", sequenced_open)
    monkeypatch.setattr("vlogsrt.transcribe._AttemptWatch.admit", instrumented_admit)
    ledger = QueueLedger(trace)
    live = live_with(monkeypatch, client, ledger, cancellation=gate)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            live.transcribe(multi_chunk_job(tmp_path, 3), Config())
        except BaseException as exc:  # noqa: BLE001 - 스레드 밖으로 온전히 옮긴다
            outcome.append(exc)

    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert first_in_authorize.wait(timeout=10)  # 첫 꼬리가 잠금 안 승인에 들어갔다
        assert third_open_reached.wait(timeout=10)  # 둘째 꼬리가 문 앞에 와 있다
        let_third_through.set()
        assert queued_at_admission.wait(timeout=10)  # 잠금 앞에 줄을 섰다
        cancelled.set()  # 줄에 서 있는 동안 취소가 켜진다
    finally:
        release.set()
        runner.join(timeout=15)
    assert not runner.is_alive()  # 교착 없이 닫혔다

    assert len(admit_calls) == 3
    # 머리와 줄을 잡은 첫 꼬리뿐 — 줄서던 둘째의 승인은 일어나지 않았다
    assert len(admissions) == 2 and admissions[0] == 0
    assert len(client.calls) == 1  # 머리만 SDK까지 갔다
    assert isinstance(outcome[0], Cancelled)


def test_luna_honors_a_caller_cancellation_around_authorization_and_send() -> None:
    """Responses 경계도 승인·전송 주위에 호출자 취소 게이트를 단다."""
    # 1) 승인 뒤 취소 — 티켓은 나왔지만 SDK는 안 부른다
    trace: list[str] = []
    armed: list[bool] = []

    def gate() -> None:
        if armed:
            raise Cancelled

    def arm(meta: RequestAttemptMeta) -> None:
        armed.append(True)

    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace, on_authorize=arm)
    with pytest.raises(Cancelled):
        request_luna_vision(
            client,
            model="vision-model-x",
            payload={"input": []},
            online=True,
            observer=ledger,
            cancellation=gate,
        )
    assert len(ledger.tickets) == 1 and client.calls == [] and ledger.outcomes == []

    # 2) 관찰자 없는 경로 — 전송 직전 취소
    plain = FakeResponsesSDK(trace)

    def gate_now() -> None:
        raise Cancelled

    with pytest.raises(Cancelled):
        request_luna_vision(
            plain,
            model="vision-model-x",
            payload={"input": []},
            online=True,
            cancellation=gate_now,
        )
    assert plain.calls == []

    # 3) 기본값(None)은 지금처럼 게이트 없이 돈다
    raw = request_luna_vision(
        FakeResponsesSDK(trace), model="vision-model-x", payload={"input": []}, online=True
    )
    assert raw["segments"]


# ─────────────────────────────────────────────────────────────
#  지역 전제 · 자격 칸 — 승인은 유료 호출이 실제로 갈 수 있을 때만
# ─────────────────────────────────────────────────────────────


def test_a_missing_audio_file_never_consumes_an_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """파일 열기는 승인보다 먼저다 — 지역 전제가 무너지면 예약도 없다(요건 3)."""
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace)
    live = live_with(monkeypatch, client, ledger)
    chunk = Chunk(tmp_path / "없는-파일.mp3", 0.0, 24.0, 0)

    with pytest.raises(FileNotFoundError):
        live.transcribe(TranscriptionJob(stem="demo", chunks=(chunk,)), Config())

    assert trace == []  # 승인도 SDK도 없다
    assert client.calls == []


def test_a_luna_payload_redefining_model_is_rejected_before_authorization() -> None:
    """지역 요청 결함에 예약을 쓰지 않는다 — 관찰자가 없어도 같은 TypeError다."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace)
    payload = {"model": "other-model", "input": []}

    with pytest.raises(TypeError, match="model"):
        request_luna_vision(
            client, model="vision-model-x", payload=payload, online=True, observer=ledger
        )
    assert client.calls == [] and ledger.authorized == []

    with pytest.raises(TypeError, match="model"):  # 관찰자 없는 호환 경로도 같다
        request_luna_vision(client, model="vision-model-x", payload=payload, online=True)


def test_credential_transport_fields_stay_out_of_the_snapshot() -> None:
    """나르개의 자격 칸은 사본에 담지 않는다 — 나가는 요청은 그대로다(요건 2)."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace)
    payload = {
        "input": [],
        "extra_headers": {"Authorization": "Bearer sk-synthetic"},
        "api_key": "sk-synthetic",
    }

    request_luna_vision(
        client, model="vision-model-x", payload=payload, online=True, observer=ledger
    )

    (meta,) = ledger.authorized
    assert "extra_headers" not in meta.options
    assert "api_key" not in meta.options
    assert meta.options["input"] == []
    (call,) = client.calls  # 걷어내는 건 사본뿐 — 실제 요청은 한 글자도 안 바뀐다
    assert call["extra_headers"] == {"Authorization": "Bearer sk-synthetic"}
    assert call["api_key"] == "sk-synthetic"


def test_the_response_hook_sees_the_raw_reply_even_when_conversion_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """변환이 깨져도 응답 훅은 이미 원본을 받았다 — 기록 물릴 기회가 지나가지 않는다."""
    trace: list[str] = []
    client = FakeAudioSDK(trace, reply_factory=BrokenReply)
    ledger = RecordingObserver(trace)
    live = live_with(monkeypatch, client, ledger)

    with pytest.raises(ValueError, match="깨진 응답"):
        live.transcribe(one_chunk_job(tmp_path), Config())

    assert trace == ["authorize", "sdk-call", "response", "convert"]
    assert isinstance(ledger.raw_responses[0], BrokenReply)


# ─────────────────────────────────────────────────────────────
#  팩터리 · 픽스처 · 빈 작업 · 관찰자 없는 호환
# ─────────────────────────────────────────────────────────────


def test_the_factory_forwards_the_observer_to_the_live_path_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ledger = RecordingObserver([])

    live = get_transcriber(Config(), online=True, observer=ledger)
    assert isinstance(live, LiveTranscriber)
    assert live.observer is ledger
    # 픽스처 경로는 외부 호출이 없다 — 관찰자를 물려주지 않는다.
    assert isinstance(get_transcriber(None, observer=ledger), FixtureTranscriber)


def test_offline_fixture_replay_makes_no_observer_callbacks() -> None:
    """픽스처 재생은 관찰 훅을 일으키지 않는다(요건 3) — 합성 픽스처로 본다."""
    ledger = RecordingObserver([])
    job = TranscriptionJob(stem="demo", chunks=(Chunk(Path("demo.mp3"), 0.0, 24.0, 0),))

    responses = FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(job, Config())

    assert responses  # 재생은 산다
    assert ledger.authorized == []  # 훅은 없다


def test_an_empty_job_touches_neither_client_nor_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace)
    live = live_with(monkeypatch, client, ledger)

    assert live.transcribe(TranscriptionJob(stem="demo", chunks=()), Config()) == []
    assert trace == []


def test_without_an_observer_the_calls_keep_their_existing_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """관찰자가 없으면 인자·응답이 지금과 같다(요건 3·6)."""
    client = FakeAudioSDK([])
    live = live_with(monkeypatch, client, None)

    responses = live.transcribe(one_chunk_job(tmp_path), Config())

    (call,) = client.calls
    assert call["model"] == Config().transcribe_model
    assert call["response_format"] == Config().transcribe_format
    assert call["temperature"] == Config().transcribe_temperature
    assert call["language"] == Config().language
    assert [r.offset for r in responses] == [0.0]
    assert responses[0].payload["segments"][0]["text"] == " 망원동"
    assert LiveTranscriber(api_key="sk-test").observer is None


# ─────────────────────────────────────────────────────────────
#  동시성 — 새 승인은 막고, 이미 입장한 작업은 끝까지 보고한다
# ─────────────────────────────────────────────────────────────


def test_a_fatal_authorize_failure_stops_queued_but_not_admitted_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """치명 승인 실패 뒤 대기 중 조각은 입장하지 못한다(요건 5)."""
    monkeypatch.setattr(env, "max_workers", lambda: 1)  # 조각을 한 줄로 세워 순서를 잡는다
    trace: list[str] = []
    client = FakeAudioSDK(trace)
    ledger = RecordingObserver(trace, fail_chunks={2})
    live = live_with(monkeypatch, client, ledger)

    with pytest.raises(ObserverFailure):
        live.transcribe(multi_chunk_job(tmp_path, 4), Config())

    assert sorted(meta.chunk_index for meta in ledger.authorized) == [0, 1, 2]  # 3번은 못 했다
    assert len(client.calls) == 2  # 0·1번만 SDK까지 갔다
    assert [stage for stage, _, _ in ledger.outcomes] == ["response", "response"]
    ids = unique_ids(ledger)
    assert len(set(ids)) == 3  # 겹치는 신원이 없다


def test_admitted_parallel_chunks_finish_and_report_after_a_fatal_response_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """치명 실패 뒤에도 **이미 입장한** 조각은 끝까지 돌고 보고한다(요건 5)."""
    monkeypatch.setattr(env, "max_workers", lambda: 4)
    trace: list[str] = []
    # 머리(1호 호출) 뒤의 호출 셋이 전부 동시에 걸린다 — 입장이 전부 끝난 뒤에야
    # 치명 표시가 생기는 순서를 결정적으로 만든다. 어긋나면 파열로 죽지 교착하지 않는다.
    client = FakeAudioSDK(trace, barrier_after_first=3)
    ledger = RecordingObserver(trace, fail_response_chunks={2})
    live = live_with(monkeypatch, client, ledger)

    with pytest.raises(ObserverFailure):
        live.transcribe(multi_chunk_job(tmp_path, 4), Config())

    assert sorted(meta.chunk_index for meta in ledger.authorized) == [0, 1, 2, 3]  # 전부 입장
    assert len(client.calls) == 4  # 전부 SDK까지 갔다
    reported = {attempt_id for _, _, attempt_id in ledger.outcomes}
    expected = {m.attempt_id for m in ledger.authorized if m.chunk_index != 2}
    assert reported == expected  # 2번만 기록에 실패했다 — 나머지는 보고했다
    ids = unique_ids(ledger)
    assert len(set(ids)) == 4  # 겹치는 신원이 없다
