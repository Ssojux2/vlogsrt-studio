"""요청 관찰 경계(S2B-2a) — 실제 SDK 호출 하나하나를 겉에서 본다.

가짜 SDK는 `request_luna_vision`의 클라이언트 자리에 직접 들어가고, 관찰자는
승인·응답·오류 훅을 기록하는 가짜 원장이다. 네트워크는 없다. 훅·SDK 호출·변환이
어떤 순서로 일어나는지는 공유 사건 기록(trace)으로 본다 — 호출 뒤의 개수 세기만으로는
순서가 보이지 않기 때문이다. whisper 전송은 D163으로 지웠다 — 관찰 장치는 Luna·
Claude·Gemini 문이 함께 쓴다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from vlogsrt import transcribe
from vlogsrt.transcribe import (
    RESPONSES_OPERATION,
    ObserverFailure,
    RequestAttemptMeta,
    request_luna_vision,
)

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


class FakeResponsesSDK:
    """`responses.create` 자리의 가짜 SDK — Luna 경계용. 인자·순서·결과를 기록한다."""

    def __init__(
        self,
        trace: list[str],
        failures: list[BaseException] | None = None,
        reply_factory: Callable[[list[str]], object] | None = None,
    ) -> None:
        self.trace = trace
        self.calls: list[dict[str, object]] = []
        self.failures = list(failures or ())
        self.reply_factory = reply_factory
        self.responses = self

    def create(self, **kwargs: object) -> object:
        self.trace.append("sdk-call")
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        if self.reply_factory is not None:
            return self.reply_factory(self.trace)
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


def luna(
    client: FakeResponsesSDK,
    observer: RecordingObserver,
    payload: dict[str, object] | None = None,
    cancellation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    return request_luna_vision(
        client,
        model="vision-model-x",
        payload={"input": []} if payload is None else payload,
        online=True,
        observer=observer,
        cancellation=cancellation,
    )


# ─────────────────────────────────────────────────────────────
#  세로 조각 — 순서가 곧 계약이다
# ─────────────────────────────────────────────────────────────


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


def test_the_default_meta_options_are_independent_and_read_only() -> None:
    """빈 기본값도 시도마다 따로이고 읽기 전용이다(3.11 호환: default_factory)."""
    first = RequestAttemptMeta(attempt_id="a", operation=RESPONSES_OPERATION, model="m")
    second = RequestAttemptMeta(attempt_id="b", operation=RESPONSES_OPERATION, model="m")

    assert dict(first.options) == {} and dict(second.options) == {}
    assert first.options is not second.options
    with pytest.raises(TypeError):
        first.options["model"] = "x"


def test_a_before_hook_failure_prevents_the_paid_call() -> None:
    """승인 훅이 실패하면 SDK를 부르지 않는다 — 오류 훅도 오지 않는다.

    실패한 관찰자 예외가 429을 달고 있어도 SDK 오류로 취급되지 않는다(요건 5).
    """
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    poisoned = status_error(429, "원장이 이미 치명 상태다")
    ledger = RecordingObserver(trace, authorize_failures=[poisoned])

    with pytest.raises(ObserverFailure) as excinfo:
        luna(client, ledger)

    assert trace == ["authorize"]  # 승인에서 끝났다 — SDK 호출도 변환도 없다
    assert client.calls == []
    assert ledger.outcomes == []  # 관찰자 자신의 실패에 오류 훅을 재사용하지 않는다
    assert excinfo.value.__cause__ is poisoned  # 원인 예외는 그대로 보존된다
    assert ledger.authorized[0].attempt_id


def test_an_sdk_class_name_disguise_is_still_an_observer_failure() -> None:
    """관찰자 예외가 SDK 오류와 같은 이름이어도 관찰자 실패로 올라간다."""

    class APIConnectionError(Exception):
        """SDK 오류와 같은 이름 — 그러나 관찰자가 던진 것이다(요건 5)."""

    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace, authorize_failures=[APIConnectionError("원장 연결 끊김")])

    with pytest.raises(ObserverFailure):
        luna(client, ledger)

    assert client.calls == []
    assert len(ledger.authorized) == 1  # 재승인도 없다


def test_the_snapshot_cannot_change_the_outbound_request() -> None:
    """메타의 인자 사본은 방어적이다 — 관찰자가 만져도 나가는 요청은 그대로다."""

    def mutate(meta: RequestAttemptMeta) -> None:
        meta.options["input"].append("몰래 끼워넣기")  # 겉의 사전을 뚫고 안의 리스트까지

    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace, on_authorize=mutate)

    luna(client, ledger, payload={"input": ["블루펭귄"]})

    (call,) = client.calls
    assert call["input"] == ["블루펭귄"]  # 나간 요청은 한 글자도 안 바뀌었다
    assert ledger.authorized[0].options["input"] == ["블루펭귄", "몰래 끼워넣기"]
    with pytest.raises(TypeError):  # 사본 자체도 읽기 전용이다
        ledger.authorized[0].options["model"] = "x"


# ─────────────────────────────────────────────────────────────
#  관찰자 실패 격리 — 훅의 실패는 SDK 오류와 섞이지 않는다
# ─────────────────────────────────────────────────────────────


def test_a_response_hook_failure_is_an_observer_failure_after_one_call() -> None:
    """응답 훅이 400을 달고 실패해도 다시 부르는 길이 열리지 않는다."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace, response_failure=status_error(400, "원장 기록 실패"))

    with pytest.raises(ObserverFailure):
        luna(client, ledger)

    assert len(client.calls) == 1
    assert ledger.authorized and ledger.outcomes == []


def test_an_sdk_error_report_failure_preserves_both_exceptions() -> None:
    """오류 훅이 실패해도 원인 사슬에 SDK 오류가 그대로 남는다(요건 5)."""
    trace: list[str] = []
    sdk_error = status_error(500, "서버 오류")
    client = FakeResponsesSDK(trace, failures=[sdk_error])
    ledger = RecordingObserver(trace, error_failure=RuntimeError("원장 오류 기록 실패"))

    with pytest.raises(ObserverFailure) as excinfo:
        luna(client, ledger)

    assert len(client.calls) == 1  # 재시도가 아니다
    hook_failure = excinfo.value.__cause__
    assert isinstance(hook_failure, RuntimeError)  # 훅의 실패가 직접 원인이다
    assert hook_failure.__context__ is sdk_error  # 그 아래에 SDK 오류가 있다


def test_an_sdk_error_is_reported_then_raised_unchanged() -> None:
    trace: list[str] = []
    sdk_error = status_error(429, "한도")
    client = FakeResponsesSDK(trace, failures=[sdk_error])
    ledger = RecordingObserver(trace)

    with pytest.raises(RuntimeError) as excinfo:
        luna(client, ledger)

    assert excinfo.value is sdk_error
    assert trace == ["authorize", "sdk-call", "error"]
    assert [stage for stage, _, _ in ledger.outcomes] == ["error"]


# ─────────────────────────────────────────────────────────────
#  취소 — 승인보다 게이트가 먼저, BaseException은 그대로 올라간다
# ─────────────────────────────────────────────────────────────


def test_the_cancellation_gate_precedes_authorization() -> None:
    """게이트가 던지면 승인도 SDK 호출도 없다(요건 3)."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace)

    def gate() -> None:
        raise Cancelled

    with pytest.raises(Cancelled):
        luna(client, ledger, cancellation=gate)

    assert trace == []
    assert ledger.authorized == [] and client.calls == []


def test_a_base_exception_from_the_sdk_call_is_not_reported_as_an_error() -> None:
    """SDK 호출 안의 취소는 오류 훅 없이 올라간다 — 예약은 보수적으로 남는다."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace, failures=[Cancelled()])
    ledger = RecordingObserver(trace)

    with pytest.raises(Cancelled):
        luna(client, ledger)

    assert trace == ["authorize", "sdk-call"]
    assert len(ledger.authorized) == 1 and ledger.outcomes == []


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


def test_the_response_hook_sees_the_raw_reply_even_when_conversion_fails() -> None:
    """변환이 깨져도 응답 훅은 이미 원본을 받았다 — 기록 물릴 기회가 지나가지 않는다."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace, reply_factory=BrokenReply)
    ledger = RecordingObserver(trace)

    with pytest.raises(ValueError, match="깨진 응답"):
        luna(client, ledger)

    assert trace == ["authorize", "sdk-call", "response", "convert"]
    assert isinstance(ledger.raw_responses[0], BrokenReply)


# ─────────────────────────────────────────────────────────────
#  입장 · 치명 표시 — 한 관찰 맥락(_AttemptWatch)을 나눠 쓰는 시도들
#
#  whisper 조각 병렬 전사는 D163으로 지웠지만, 같은 맥락을 다시 부르는 길은 남았다
#  (Gemini 분당 한도 재시도가 같은 시도를 되부른다). 그래서 관찰 래퍼를 직접 몬다.
# ─────────────────────────────────────────────────────────────


def _meta() -> RequestAttemptMeta:
    return RequestAttemptMeta(attempt_id="a", operation=RESPONSES_OPERATION, model="m")


def _attempt(
    watch: transcribe._AttemptWatch,
    client: FakeResponsesSDK,
    *,
    prepare: Callable[[], Any] | None = None,
    gate: Callable[[], None] | None = None,
) -> Callable[[], Any]:
    return transcribe._observed_call(
        watch, _meta, lambda _prepared: client.create(input=[]), prepare=prepare, gate=gate
    )


class _Prepared:
    """지역 준비물 — 닫혔는지 기록한다(승인 전에 무너져도 닫힌다)."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def close(self) -> None:
        self.log.append("closed")


def test_cancellation_during_local_preparation_never_authorizes() -> None:
    """지역 준비가 한창인 동안 취소가 켜지면 승인도 SDK도 없다(S1 순서)."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace)
    prepared: list[str] = []

    def prepare() -> _Prepared:
        prepared.append("opened")  # 진짜 준비가 일어났다 — 그 한가운데에 취소가 켜진다
        return _Prepared(prepared)

    def gate() -> None:
        if prepared:
            raise Cancelled

    watch = transcribe._AttemptWatch(observer=ledger)
    with pytest.raises(Cancelled):
        _attempt(watch, client, prepare=prepare, gate=gate)()

    assert prepared == ["opened", "closed"]  # 준비 **뒤의** 확인에서 멈췄고 준비물은 닫혔다
    assert ledger.authorized == [] and client.calls == []


def test_a_failed_local_preparation_never_consumes_an_authorization() -> None:
    """지역 준비(파일 열기 등)는 승인보다 먼저다 — 무너지면 예약도 SDK도 없다(요건 3)."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace)

    def prepare() -> _Prepared:
        raise FileNotFoundError("없는-파일.wav")

    watch = transcribe._AttemptWatch(observer=ledger)
    with pytest.raises(FileNotFoundError):
        _attempt(watch, client, prepare=prepare)()

    assert trace == []  # 승인도 SDK도 없다
    assert client.calls == []


def test_a_fatal_authorize_failure_blocks_every_later_admission_on_the_same_watch() -> None:
    """첫 관찰자 실패가 치명 표시가 된다 — 같은 맥락의 다음 시도는 승인 훅조차 안 부른다."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace, authorize_failures=[RuntimeError("원장 고장")])
    watch = transcribe._AttemptWatch(observer=ledger)

    with pytest.raises(ObserverFailure):
        _attempt(watch, client)()
    with pytest.raises(ObserverFailure, match="치명") as second:
        _attempt(watch, client)()

    assert isinstance(second.value.__cause__, ObserverFailure)  # 첫 실패가 원인으로 남는다
    assert len(ledger.authorized) == 1  # 두 번째 시도는 승인 훅에 닿지 않았다
    assert client.calls == []


def test_a_fatal_response_failure_blocks_new_admissions_but_the_call_already_happened() -> None:
    """응답 훅의 실패도 치명이다 — 이미 간 호출은 그대로지만 새 승인은 없다."""
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace, response_failure=RuntimeError("응답 기록 실패"))
    watch = transcribe._AttemptWatch(observer=ledger)

    with pytest.raises(ObserverFailure):
        _attempt(watch, client)()
    with pytest.raises(ObserverFailure, match="치명"):
        _attempt(watch, client)()

    assert len(client.calls) == 1  # 첫 시도는 SDK까지 갔다
    assert len(ledger.authorized) == 1  # 두 번째는 승인조차 없다


def test_a_failed_meta_preparation_is_a_fatal_observer_failure() -> None:
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    ledger = RecordingObserver(trace)
    watch = transcribe._AttemptWatch(observer=ledger)

    def broken_meta() -> RequestAttemptMeta:
        raise ValueError("메타 재료가 깨졌다")

    with pytest.raises(ObserverFailure, match="메타 준비"):
        transcribe._observed_call(watch, broken_meta, lambda _p: client.create())()
    with pytest.raises(ObserverFailure, match="치명"):
        _attempt(watch, client)()
    assert ledger.authorized == [] and client.calls == []


def test_cancellation_while_waiting_for_admission_stops_the_queued_authorization() -> None:
    """입장 줄에 선 시도는 잠금을 얻는 순간 취소를 새로 본다 — 승인은 일어나지 않는다.

    먼저 온 시도의 승인이 잠금을 잡은 채 기다리고, 뒤 시도가 그 잠금 앞에 선 동안 취소가
    켜진다. 줄을 세운 증거는 입장(admit) 호출 수를 세는 좁은 계측이다.
    """
    trace: list[str] = []
    client = FakeResponsesSDK(trace)
    cancelled = threading.Event()
    first_in_authorize = threading.Event()
    queued_at_admission = threading.Event()
    release = threading.Event()
    admit_calls: list[int] = []
    real_admit = transcribe._AttemptWatch.admit

    class QueueLedger(RecordingObserver):
        def authorize(self, meta: RequestAttemptMeta) -> str:
            if not release.is_set():
                first_in_authorize.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("입장 잠금이 10초 안에 풀리지 않았다")
            return super().authorize(meta)

    class CountingWatch(transcribe._AttemptWatch):
        def admit(self, make_meta: Any, gate: Any = None) -> Any:
            admit_calls.append(len(admit_calls) + 1)
            if len(admit_calls) == 2:
                queued_at_admission.set()  # 잠금이 잡혀 있는 동안 들어오는 입장은 줄이다
            return real_admit(self, make_meta, gate)

    def gate() -> None:
        if cancelled.is_set():
            raise Cancelled

    ledger = QueueLedger(trace)
    watch = CountingWatch(observer=ledger)
    outcomes: dict[str, BaseException | None] = {}

    def run(name: str) -> None:
        try:
            _attempt(watch, client, gate=gate)()
            outcomes[name] = None
        except BaseException as exc:  # noqa: BLE001 - 스레드 밖으로 온전히 옮긴다
            outcomes[name] = exc

    first = threading.Thread(target=run, args=("first",))
    second = threading.Thread(target=run, args=("second",))
    first.start()
    try:
        assert first_in_authorize.wait(timeout=10)  # 첫 시도가 잠금 안 승인에 들어갔다
        second.start()
        assert queued_at_admission.wait(timeout=10)  # 둘째가 잠금 앞에 줄을 섰다
        cancelled.set()  # 줄에 서 있는 동안 취소가 켜진다
    finally:
        release.set()
        first.join(timeout=15)
        if second.ident is not None:  # 시작된 경우에만 회수한다
            second.join(timeout=15)
    assert not first.is_alive() and not second.is_alive()  # 교착 없이 닫혔다

    assert len(admit_calls) == 2
    assert len(ledger.authorized) == 1  # 줄을 잡은 첫 시도뿐 — 줄서던 둘째의 승인은 없다
    assert isinstance(outcomes["second"], Cancelled)
    # 첫 시도는 승인 뒤 게이트에서 취소를 봤다 — SDK는 아무도 부르지 않았다
    assert isinstance(outcomes["first"], Cancelled)
    assert client.calls == []
