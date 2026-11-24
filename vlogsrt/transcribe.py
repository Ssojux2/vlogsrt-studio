"""유일한 네트워크 경계(불변 규칙 5).

다른 모듈은 전부 순수 함수다. `openai`를 import하는 파일은 이 파일뿐이며,
그것도 실제 호출 직전에 지연 import한다. Claude Messages(D54)는 HTTP 전송만
`anthropic_messages.py`에 살고, 그 파일을 import하는 곳도 이 파일뿐이다 — 승인 문·
관찰·취소는 여기 있다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from types import MappingProxyType
from typing import Any, Protocol
from uuid import uuid4

from . import log
from .config import GEMINI_RATE_RETRY, ProviderRateRetrySettings
from .models import Chunk
from .task_runtime import QUOTA_PER_DAY, check_cancel

CAPTION_TIMING_KIND = "caption-timing"  # 파생 계층 시간 경로 — verbose_json 세그먼트(A7-1)


# 힌트가 실릴 수 있는 호출. **whisper 쪽은 절대 여기 들어오지 않는다**(규칙 6, D16).
# ─────────────────────────────────────────────────────────────
#  픽스처
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
#  실제 호출
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
#  요청 관찰 (S2B-2a) — 실제 호출을 겉에서 본다. 출력은 못 바꾼다
# ─────────────────────────────────────────────────────────────


RESPONSES_OPERATION = "responses.create"
"""Responses SDK 연산 — `client.responses.create` 한 번."""

MESSAGES_OPERATION = "messages.create"
"""Claude Messages API 연산 — `POST /v1/messages` 한 번(D54)."""

GENERATE_CONTENT_OPERATION = "models.generateContent"
"""Gemini generateContent 연산 — `POST /v1beta/models/{model}:generateContent` 한 번(W-30a, D83)."""
GEMINI_TRANSCRIBE_OPERATION = "interactions.transcribe"
"""시간 엔진 Gemini 3.5 Transcribe 연산 — `POST /v1beta/interactions` 한 번(W-31a, D92)."""

_REDACTED_OPTION_KEYS = frozenset({"api_key", "extra_headers"})
"""메타의 인자 사본에 아예 담지 않는 자격 칸 — 나가는 요청에서 걷어내는 게 아니라
비추지 않는 것뿐이다(요건 2)."""


class ObserverFailure(RuntimeError):
    """관찰자(회계 기반구조) 자신의 실패 — SDK 오류와는 종류가 다른 치명 사고다.

    SDK 오류로 위장할 수 없다. 이 클래스는 `status_code`를 물려받지 않으므로
    `_is_retryable`·`_is_bad_request` 어느 재시도 판정에도 걸리지 않고, 관찰자의
    실패를 오류 훅으로 다시 보고하지도 않는다. 원인 예외는 `__cause__` 사슬로
    보존된다. 관찰자가 없는 호출자는 이 예외를 아예 볼 수 없다.
    """


@dataclass(frozen=True, slots=True)
class RequestAttemptMeta:
    """실제 SDK 호출 **시도 하나**의 불변 신원(S2B-2a).

    `attempt_id`는 SDK 요청 한 번의 신원이지 프로젝트 실행의 AnalysisAttempt ID와는
    다른 층이다 — 재시도·거부 인자 재요청마다 새로 생긴다. 넣는 것: 시도 신원·실제
    모델·연산·(오디오) 작업 종류와 출처·조각 좌표·실제로 실리는 인자의 방어 사본.
    안 넣는 것: API 키·인증 헤더·파일 핸들. 엔드포인트·설정·가격 신원은 관찰자를
    만드는 호출자 소유다 — 이 파일은 환경 탐색을 하지 않는다.

    `options`는 나가는 요청의 깊은 사본을 읽기 전용으로 감싼 것이다. 관찰자가 이
    사본을 어떻게 손대도 나가는 요청은 변하지 않고, 나르개의 자격 칸(`api_key`·
    `extra_headers`)은 사본에 아예 담지 않는다.
    """

    attempt_id: str
    operation: str
    model: str
    kind: str = ""  # 오디오 호출의 작업 종류. Responses 호출은 비워 둔다
    stem: str = ""
    source: str = ""
    chunk_index: int | None = None
    chunk_offset: float | None = None
    chunk_duration: float | None = None
    # 빈 사전을 그대로 기본값으로 두면 3.11의 dataclass가 "해시 불가능한 기본값"으로
    # 거부한다(mappingproxy의 hash 지원은 3.12부터) — 공장으로 만들어 피한다.
    options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


class RequestObserver(Protocol):
    """실제 호출 하나하나를 겉에서 보는 주입 경계(S2B-2a).

    구현체가 실제로 여럿(예산 원장 어댑터·기록기)이므로 Protocol이 필요하다 —
    `Transcriber`·`VisionResponder`와 같은 판단이다. 티켓은 불투명하다: `authorize`가
    돌려준 값을 되돌려줄 뿐 해석하지 않는다. 훅이 던지면 그 관찰자는 고장난 것이다
    — `ObserverFailure`로 갈라 올라가고 SDK 재시도로 취급되지 않는다.
    """

    def authorize(self, meta: RequestAttemptMeta) -> Any: ...

    def on_response(self, ticket: Any, meta: RequestAttemptMeta, response: Any) -> None: ...

    def on_error(self, ticket: Any, meta: RequestAttemptMeta, error: Exception) -> None: ...


@dataclass(slots=True)
class _AttemptWatch:
    """`transcribe` 호출 하나의 관찰 맥락. 치명 표시는 잠금 안에서 한 번만 정해진다.

    `admit`의 잠금이 이 호출의 **입장 선형화 지점**이다 — 치명 확인·취소 확인·관찰자
    승인이 같은 잠금 안에서 일어나므로 그 사이를 비집고 들어올 틈이 없다. 이미
    승인받은 병렬 조각은 그대로 끝까지 돌고 결과를 보고한다 — 그 위의 실행 전체를
    끊는 건 이 계층의 권한이 아니다.
    """

    observer: RequestObserver
    stem: str = ""
    source: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _fatal: ObserverFailure | None = None

    def admit(
        self,
        make_meta: Callable[[], RequestAttemptMeta],
        gate: Callable[[], None] | None = None,
    ) -> tuple[Any, RequestAttemptMeta]:
        """치명 확인 · 메타 준비 · 취소 확인 · 승인을 한 잠금 안에서 끝낸다(S2B-2a).

        취소 확인이 잠금 **안**에 있는 것이 핵심이다 — 입장 줄에 서서 다른 승인을
        기다리는 동안 취소가 켜져도 잠금을 얻는 순간 새로 확인하므로 그 시도의
        승인은 일어나지 않는다.
        """
        with self._lock:
            if self._fatal is not None:
                raise ObserverFailure(
                    "관찰자 치명 실패 뒤에는 새 승인이 없다 — 이 시도는 SDK를 부르지 않는다"
                ) from self._fatal
            try:
                meta = make_meta()
            except Exception as exc:  # noqa: BLE001 - 관찰 준비의 실패도 관찰자 실패다
                failure = ObserverFailure(f"요청 관찰의 메타 준비가 실패했다: {exc}")
                self._fatal = failure
                raise failure from exc
            if gate is not None:
                gate()  # 메타 뒤·승인 직전 — 잠금 안의 마지막 확인
            try:
                ticket = self.observer.authorize(meta)
            except Exception as exc:  # noqa: BLE001 - 관찰자 실패로 바꿔 올린다
                failure = ObserverFailure(f"요청 관찰자의 authorize 훅이 실패했다: {exc}")
                self._fatal = failure  # 잠금 안이다 — 첫 실패가 그대로 first-wins다
                raise failure from exc
            return ticket, meta

    def record_fatal(self, failure: ObserverFailure) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = failure


def _hook_failure(watch: _AttemptWatch, stage: str, exc: Exception) -> ObserverFailure:
    """관찰자 실패를 치명 사고로 만든다. 첫 실패가 이 호출의 새 승인을 막는다."""
    failure = ObserverFailure(f"요청 관찰자의 {stage} 훅이 실패했다: {exc}")
    watch.record_fatal(failure)
    return failure


def _observed_call(
    watch: _AttemptWatch,
    make_meta: Callable[[], RequestAttemptMeta],
    call: Callable[[Any], Any],
    prepare: Callable[[], Any] | None = None,
    gate: Callable[[], None] | None = None,
) -> Callable[[], Any]:
    """SDK 호출 하나를 관찰 훅으로 감싼다 — S2B-2a 요건 7의 작은 래퍼.

    `_retry`는 결과를 일반 호출처럼 부른다. 안에서는 (지역 전제) → 승인 → (취소
    확인) → SDK 호출 → 응답/오류 훅 순서로만 움직이므로 재시도·거부 인자 재요청마다
    새 티켓이 자연히 생긴다. 승인 **직전**의 취소 확인은 `watch.admit`의 잠금 안에
    하나만 있다 — 줄 밖의 확인은 기다림 사이가 뚫리므로 따로 두지 않는다. 관찰자
    자신의 실패는 `ObserverFailure`로 갈라 올라가 SDK 예외와 섞이지 않는다. 취소
    (BaseException)는 오류 훅 없이 그대로 올라간다 — 승인이 이미 났다면 예약은
    불확실성으로 보수적으로 남는다.
    """

    def attempt() -> Any:
        prepared = None if prepare is None else prepare()  # 지역 전제가 승인보다 먼저다
        try:
            ticket, meta = watch.admit(make_meta, gate)
            if gate is not None:
                gate()  # 승인이 늦어지는 동안 취소가 왔으면 SDK를 부르지 않는다
            try:
                response = call(prepared)
            except Exception as exc:  # noqa: BLE001 - 오류 훅이 먼저, 재분류는 _retry가 한다
                try:
                    watch.observer.on_error(ticket, meta, exc)
                except Exception as hook_exc:  # noqa: BLE001
                    raise _hook_failure(watch, "on_error", hook_exc) from hook_exc
                raise
            try:
                watch.observer.on_response(ticket, meta, response)
            except Exception as exc:  # noqa: BLE001
                raise _hook_failure(watch, "on_response", exc) from exc
            return response
        finally:
            if prepared is not None:
                prepared.close()

    return attempt


def _snapshot_options(options: Mapping[str, Any]) -> Mapping[str, Any]:
    """나가는 인자의 깊은 사본을 읽기 전용으로 돌려준다. 자격 칸은 담지 않는다.

    사본일 뿐 나가는 요청은 한 글자도 안 바뀐다 — 관찰자가 이 사본을 어떻게 손대도
    실제 요청은 그대로다(요건 2).
    """
    return MappingProxyType(
        {
            name: deepcopy(value)
            for name, value in options.items()
            if name not in _REDACTED_OPTION_KEYS
        }
    )


def _responses_attempt_meta(
    model: str, payload: Mapping[str, object], operation: str = RESPONSES_OPERATION
) -> RequestAttemptMeta:
    """Responses·Messages 호출의 관찰 메타. 페이로드는 방어적 깊은 사본이다.

    창·블록 같은 사업 신원은 페이로드 안에 있고 그 해석은 호출자 소유다 — 여기는
    실려 나가는 값 자체만 비춘다.
    """
    return RequestAttemptMeta(
        attempt_id=uuid4().hex,
        operation=operation,
        model=model,
        options=_snapshot_options({"model": model, **dict(payload)}),
    )


# A stalled HTTP read must surface as a failed attempt instead of pinning a job
# forever; retries stay in `_retry`, so this bounds one attempt only.
SDK_TIMEOUT_SECONDS = 60.0


def _to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    for attribute in ("model_dump", "to_dict"):
        converter = getattr(response, attribute, None)
        if callable(converter):
            return dict(converter())
    return json.loads(str(response))


# ─────────────────────────────────────────────────────────────
#  공개 전송 경계 (A7-1) — 파생 계층이 실제 호출에 쓰는 좁은 타입 요청
# ─────────────────────────────────────────────────────────────
#
# 파생 계층(vlogsrt/project/raw)은 SDK를 모른다. 이 아래의 불변 요청형과
# 전송 메서드가 **이미 존재하는** 유일한 호출 지점(_send_audio)을 그대로 쓴다 —
# 호출 지점이 둘이면 출처가 둘이다(불변 규칙 1). 역할별 조합의 금지(문구 모델에
# 세그먼트 시간 요청 등)는 파생 계층의 기능 등록표가 정하고, 여기는 모든 역할에
# 참인 형태만 검사한다.

# ─────────────────────────────────────────────────────────────
#  비전 호출 경계 (P5-05) — 파생 계층 비전 모델의 유일한 호출 지점
# ─────────────────────────────────────────────────────────────


_PUBLIC_OPENAI_BASE_URL = "https://api.openai.com/v1"
"""SDK 3.8.0이 환경 변수 없이 쓰는 공식 공개 엔드포인트(`_client.py`의 기본값).

`base_url=None`을 SDK에 그대로 넘기면 SDK가 먼저 `OPENAI_BASE_URL`을 물려받는다
(설치된 3.8.0 실측) — 레거시 STT 프록시가 비전 쪽 공급자를 바꿔치는 길이다.
None은 "환경을 따라간다"가 아니라 **공개 엔드포인트**라 여기서 명시적으로 못 박는다.
"""


def create_vision_client(*, api_key: str, base_url: str | None) -> Any:
    """비전 전용 클라이언트 — 값은 받은 것만 쓴다.

    환경·파일을 스스로 읽지 않는다. 실제 값은 사람이 승인한 온라인 실행기가
    `vlogsrt.project.toml`의 자격증명 이름(`vision_api_key_env`·`vision_base_url_env`,
    P5-00)으로 명시적으로 풀어 넘긴다. 레거시 STT helpers(`env.require_api_key`·
    `env.base_url`)를 물려받지 않는다 — 한쪽 키가 양쪽 공급자를 열면 안 된다.

    `base_url=None`은 환경 상속이 아니라 공개 엔드포인트다(`_PUBLIC_OPENAI_BASE_URL`
    문서 참조). 호출자가 준 base_url은 그대로 간다.

    `max_retries=0`도 명시적으로 못 박는다 — SDK 기본값(2, 설치된 3.8.0 실측)은
    `request_vision`의 "재시도 없음" 계약 뒤에 숨어서 429·타임아웃을 조용한 지연으로
    바꾼다. 실패는 한 번의 명시적 시도여야 한다.
    """
    import openai  # 지연 import — openai를 얻는 유일한 파일이다(불변 규칙 5)

    if base_url:
        return openai.OpenAI(
            api_key=api_key, base_url=base_url, max_retries=0, timeout=SDK_TIMEOUT_SECONDS
        )
    return openai.OpenAI(
        api_key=api_key,
        base_url=_PUBLIC_OPENAI_BASE_URL,
        max_retries=0,
        timeout=SDK_TIMEOUT_SECONDS,
    )


def request_luna_vision(
    client: Any,
    *,
    model: str,
    payload: Mapping[str, object],
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """승인형 rough-cut 비전의 Responses API 경계.

    기존 B-roll 프로토타입의 Chat Completions 경로를 바꾸지 않고, 새 1.1 분석만
    이 함수를 쓴다. 명시적 승인에서 온 ``online=True`` 없이는 클라이언트도
    만지지 않는다. `observer`는 실제 요청 하나를 겉에서 보는 선택 경계다(S2B-2a)
    — 승인 게이트를 지난 뒤에만 움직이고, 단발 요청이라 기다리는 대기열이 없다.
    `cancellation`은 승인·전송 주위의 선택 게이트다. 이미 날아간 SDK 호출은
    되돌릴 수 없으니 결과 보고는 그대로 둔다.
    """
    if not online:
        raise RuntimeError(
            "Luna 비전 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "AnalysisAuthorization 승인이 필요하다"
        )
    if "model" in payload:
        # 지역 요청 결함이다 — 승인(유료 예약)을 소비한 뒤 SDK가 TypeError로 닫는
        # 길을 만들지 않는다. 관찰자가 없어도 같은 TypeError로 닫힌다.
        raise TypeError(
            f"payload가 model을 다시 정의한다({payload['model']!r}) — "
            "모델의 정본은 함수 인자 하나뿐이다"
        )

    def send(_prepared: Any) -> Any:
        return _responses_create(client, model, payload)

    if observer is None:
        if cancellation is not None:
            cancellation()
        response = send(None)
    else:
        response = _observed_call(
            _AttemptWatch(observer=observer),
            partial(_responses_attempt_meta, model, payload),
            send,
            gate=cancellation,
        )()
    converted = _to_dict(response)
    output_text = getattr(response, "output_text", None)
    if "output_text" not in converted and isinstance(output_text, str):
        converted["output_text"] = output_text
    return converted


def _responses_create(client: Any, model: str, payload: Mapping[str, object]) -> Any:
    return client.responses.create(model=model, **dict(payload))


# ─────────────────────────────────────────────────────────────
#  Claude Messages 경계(D54) — HTTP 전송은 `anthropic_messages`, 문은 여기
# ─────────────────────────────────────────────────────────────


def create_claude_client(
    *, api_key: str, base_url: str, api_version: str, timeout_seconds: float
) -> Any:
    """Claude 의미 분석 클라이언트 — 값은 받은 것만 쓴다(환경·파일을 스스로 읽지 않는다).

    만드는 것만으로는 아무 호출도 없다. 전송 모듈은 지연 import한다 — 네트워크 모듈을
    얻는 파일은 그 하나이고, 그 파일을 얻는 파일은 이 파일 하나다.
    """
    from . import anthropic_messages

    return anthropic_messages.ClaudeMessagesClient(
        api_key=api_key,
        base_url=base_url,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
    )


def is_claude_client(candidate: object) -> bool:
    """Claude 클라이언트인가 — 응답자가 공급자와 클라이언트 종류를 맞춰 보는 자리다."""
    from . import anthropic_messages

    return isinstance(candidate, anthropic_messages.ClaudeMessagesClient)


def request_claude_messages(
    client: Any,
    *,
    model: str,
    payload: Mapping[str, object],
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """승인형 의미 분석의 Claude Messages 경계 — `request_luna_vision`과 같은 문이다.

    명시적 승인에서 온 ``online=True`` 없이는 클라이언트를 만지지 않는다. 지역 요청
    결함(모델 재정의·Claude가 아닌 클라이언트)은 승인(유료 예약)을 쓰기 **전에** 닫는다.
    재시도는 없다 — 429·529·시간 초과는 명시적 실패 기록이다.
    """
    _require_claude_request(client, payload, online=online)

    def send(_prepared: Any) -> Any:
        return _messages_create(client, model, payload)

    if observer is None:
        if cancellation is not None:
            cancellation()
        return _messages_create(client, model, payload)
    reply: dict[str, Any] = _observed_call(
        _AttemptWatch(observer=observer),
        partial(_responses_attempt_meta, model, payload, MESSAGES_OPERATION),
        send,
        gate=cancellation,
    )()
    return reply


def _require_claude_request(client: Any, payload: Mapping[str, object], *, online: bool) -> None:
    """승인 문과 지역 요청 결함 — 관찰자 승인(유료 예약)보다 먼저 닫는다."""
    if not online:
        raise RuntimeError(
            "Claude 의미 분석 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "AnalysisAuthorization 승인이 필요하다"
        )
    if "model" in payload:
        raise TypeError(
            f"payload가 model을 다시 정의한다({payload['model']!r}) — "
            "모델의 정본은 함수 인자 하나뿐이다"
        )
    if not is_claude_client(client):
        raise TypeError(f"Claude 클라이언트가 아니다: {type(client).__name__}")


def _messages_create(client: Any, model: str, payload: Mapping[str, object]) -> dict[str, Any]:
    from . import anthropic_messages

    return anthropic_messages.post_messages(client, {"model": model, **dict(payload)})


# ─────────────────────────────────────────────────────────────
#  Gemini generateContent 경계(W-30a, D83) — HTTP 전송은 `gemini_messages`, 문은 여기
# ─────────────────────────────────────────────────────────────


def create_gemini_client(
    *, api_key: str, base_url: str, api_version: str, timeout_seconds: float
) -> Any:
    """Gemini 의미 분석 클라이언트 — 값은 받은 것만 쓴다(환경·파일을 스스로 읽지 않는다).

    만드는 것만으로는 아무 호출도 없다. 전송 모듈은 지연 import한다 — 그 파일을 얻는 파일은
    이 파일 하나다(Claude 전송과 같은 선).
    """
    from . import gemini_messages

    return gemini_messages.GeminiClient(
        api_key=api_key,
        base_url=base_url,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
    )


def is_gemini_client(candidate: object) -> bool:
    """Gemini 클라이언트인가 — 응답자가 공급자와 클라이언트 종류를 맞춰 보는 자리다."""
    from . import gemini_messages

    return isinstance(candidate, gemini_messages.GeminiClient)


def request_gemini_generate(
    client: Any,
    *,
    model: str,
    payload: Mapping[str, object],
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """승인형 의미 분석의 Gemini generateContent 경계 — `request_claude_messages`와 같은 문이다.

    명시적 승인에서 온 ``online=True`` 없이는 클라이언트를 만지지 않는다. 지역 요청 결함(본문의
    모델 재정의·Gemini가 아닌 클라이언트)은 승인(유료 예약)을 쓰기 **전에** 닫는다. 모델은 본문이
    아니라 경로에 실린다 — 관찰 메타에는 다른 경계처럼 `model`이 함께 비친다. 공급자가 대기
    시간을 말한 분당 한도 429만 기다렸다 다시 보낸다(D141) — 시도마다 승인·취소 문을 다시 지난다.
    """
    _require_gemini_request(client, model, payload, online=online)

    def send(_prepared: Any) -> Any:
        return _generate_content(client, model, payload)

    def bare() -> dict[str, Any]:
        if cancellation is not None:
            cancellation()
        return _generate_content(client, model, payload)

    attempt = bare
    if observer is not None:
        attempt = _observed_call(
            _AttemptWatch(observer=observer),
            partial(_responses_attempt_meta, model, payload, GENERATE_CONTENT_OPERATION),
            send,
            gate=cancellation,
        )
    reply: dict[str, Any] = _gemini_rate_retry(attempt, "Gemini 의미 분석", cancellation)
    return reply


def _require_gemini_request(
    client: Any, model: str, payload: Mapping[str, object], *, online: bool
) -> None:
    """승인 문과 지역 요청 결함 — 관찰자 승인(유료 예약)보다 먼저 닫는다. 모델 이름은 경로에
    들어가므로 경로 조각 검사도 여기서 한다(전송이 키를 얹기 전에 한 번 더 본다)."""
    if not online:
        raise RuntimeError(
            "Gemini 의미 분석 호출은 명시적 online=True 없이는 닫혀 있다 — "
            "AnalysisAuthorization 승인이 필요하다"
        )
    if "model" in payload:
        raise TypeError(
            f"payload가 model을 다시 정의한다({payload['model']!r}) — "
            "모델의 정본은 함수 인자 하나뿐이다"
        )
    if not is_gemini_client(client):
        raise TypeError(f"Gemini 클라이언트가 아니다: {type(client).__name__}")
    from . import gemini_messages

    gemini_messages.require_model_segment(model)


def _generate_content(client: Any, model: str, payload: Mapping[str, object]) -> dict[str, Any]:
    from . import gemini_messages

    return gemini_messages.post_generate_content(client, model, payload)


_rate_sleep: Callable[[float], None] = time.sleep
"""429 대기의 잠 — 시험이 가짜로 갈아 끼운다(실제로 자지 않는다)."""


def _gemini_rate_retry(
    attempt: Callable[[], Any],
    what: str,
    cancellation: Callable[[], None] | None,
    settings: ProviderRateRetrySettings = GEMINI_RATE_RETRY,
) -> Any:
    """공급자가 대기 시간을 말한 분당 한도 429만 그만큼 기다렸다 다시 보낸다(D141).

    `attempt` 한 번이 승인 → 취소 확인 → 요청 한 번 → 훅이라, 다시 보낸 시도도 새 승인·새 시도
    신원이다. 그 밖의 실패(대기를 말하지 않은 429·하루 한도·상한보다 긴 대기·503·시간 초과·관찰자
    실패)는 예전처럼 한 번의 명시적 실패로 그대로 올라간다.
    """
    for tried in range(settings.max_retries + 1):
        try:
            return attempt()
        except Exception as exc:  # noqa: BLE001 - 재시도 판단 뒤 그대로 다시 던진다
            wait = _told_rate_wait(exc, settings)
            if wait is None or tried == settings.max_retries:
                raise
            log.warn(
                f"{what} 429 — 공급자가 {wait:g}초 뒤를 말했다. 기다렸다 다시 보낸다"
                f"({tried + 1}/{settings.max_retries})."
            )
            _wait_cancellable(wait, cancellation, settings.poll_seconds)
    raise AssertionError("재시도 고리가 끝까지 돌았다")  # 마지막 시도는 돌려주거나 던진다


def _told_rate_wait(error: Exception, settings: ProviderRateRetrySettings) -> float | None:
    """기다렸다 다시 보낼 429면 그 대기 초, 아니면 None.

    하루 한도는 공급자가 몇십 초를 말해도 그 뒤에 풀리지 않는다(D111)."""
    from . import gemini_messages

    if not isinstance(error, gemini_messages.GeminiAPIStatusError) or error.status_code != 429:
        return None
    delay = error.retry_delay_seconds
    if delay is None or not 0 <= delay <= settings.max_wait_seconds:
        return None
    if QUOTA_PER_DAY in error.quota_id:
        return None
    return delay


def _wait_cancellable(
    seconds: float, cancellation: Callable[[], None] | None, poll_seconds: float
) -> None:
    """조각마다 취소를 보며 잔다 — 문이 받은 확인이 없으면 작업 범위의 취소를 본다."""
    left = seconds
    while left > 0:
        step = min(poll_seconds, left)
        _rate_sleep(step)
        left -= step
        if cancellation is not None:
            cancellation()
        check_cancel()


# ─────────────────────────────────────────────────────────────
#  Gemini 3.5 Transcribe 경계(W-31a, D92) — 시간 엔진의 문
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GeminiTranscriptionRequest:
    """시간 엔진 요청 하나 — 오디오 조각 하나를 단어 시각과 함께 받아 적는다(D92).

    보내는 것은 오디오·언어 잠금·verbatim·단어 시각뿐이다. `prompt`·`custom_vocabulary`·
    `system_instruction` 칸은 없다(규칙 6 — 힌트는 시간 엔진에 가지 않는다, 그리고 공급자는
    custom_vocabulary와 단어 시각을 함께 받지 않는다). 한도는 호출자가 등록표에서 넘긴다.
    """

    model: str
    chunk: Chunk
    language_codes: tuple[str, ...]
    max_audio_seconds: float
    max_request_bytes: int
    mime_type: str = "audio/wav"
    mode: str = "verbatim"
    granularities: tuple[str, ...] = ("word",)


def request_gemini_transcription(
    client: Any,
    *,
    request: GeminiTranscriptionRequest,
    online: bool = False,
    observer: RequestObserver | None = None,
    cancellation: Callable[[], None] | None = None,
    stem: str = "",
    source: str = "",
) -> dict[str, Any]:
    """승인형 1단계 전사의 Gemini 3.5 Transcribe 경계 — `request_gemini_generate`의 거울이다.

    명시적 ``online=True`` 없이는 클라이언트를 만지지 않는다. 지역 결함(Gemini가 아닌 클라이언트·
    경로 조각이 아닌 모델·빈 언어·한도를 넘는 길이와 크기)은 승인(유료 예약)보다 **먼저** 닫는다.
    오디오는 인라인 base64로 싣는다(조각이 180초 이하라 20 MB 안 — 업로드·서버 보관이 없다).
    `store: false` — 공급자 쪽에 오디오를 남기지 않는다. 관찰 기록에는 오디오 대신 지문과
    크기만 남는다. 공급자가 대기 시간을 말한 분당 한도 429만 기다렸다 다시 보낸다(D141).
    """
    _require_gemini_transcription(client, request, online=online)
    audio = request.chunk.path.read_bytes()
    body = _transcription_body(request, audio)
    _require_transcription_size(request, body)

    def send(_prepared: Any) -> Any:
        return _interaction(client, body)

    def bare() -> dict[str, Any]:
        if cancellation is not None:
            cancellation()
        return _interaction(client, body)

    attempt = bare
    if observer is not None:
        watch = _AttemptWatch(observer=observer, stem=stem, source=source)
        attempt = _observed_call(
            watch,
            partial(_transcription_attempt_meta, watch, request, body, audio),
            send,
            gate=cancellation,
        )
    reply: dict[str, Any] = _gemini_rate_retry(attempt, "Gemini 전사", cancellation)
    return reply


def _require_gemini_transcription(
    client: Any, request: GeminiTranscriptionRequest, *, online: bool
) -> None:
    if not online:
        raise RuntimeError(
            "Gemini 전사 호출은 명시적 online=True 없이는 닫혀 있다 — 1단계 승인이 필요하다"
        )
    if not is_gemini_client(client):
        raise TypeError(f"Gemini 클라이언트가 아니다: {type(client).__name__}")
    from . import gemini_messages

    gemini_messages.require_model_segment(request.model)
    if not request.language_codes or not all(request.language_codes):
        raise ValueError("Gemini 전사의 언어 잠금(language_codes)이 비었다")
    if request.chunk.duration > request.max_audio_seconds:
        raise ValueError(
            f"조각 {request.chunk.index}의 길이 {request.chunk.duration}초가 단어 시각 전사의"
            f" 한도 {request.max_audio_seconds}초를 넘는다"
        )


def _transcription_body(request: GeminiTranscriptionRequest, audio: bytes) -> dict[str, Any]:
    audio_part = {
        "type": "audio",
        "data": base64.b64encode(audio).decode("ascii"),
        "mime_type": request.mime_type,
    }
    return {
        "model": request.model,
        "store": False,
        "input": [audio_part],
        "generation_config": {"transcription_config": _transcription_config(request)},
    }


def _transcription_config(request: GeminiTranscriptionRequest) -> dict[str, Any]:
    mode = {"type": request.mode, "timestamp_granularities": list(request.granularities)}
    return {"language_codes": list(request.language_codes), "mode": mode}


def _require_transcription_size(request: GeminiTranscriptionRequest, body: dict[str, Any]) -> None:
    """인라인 요청 크기 — 넘으면 보내지 않는다(Files API로 몰래 바꾸지 않는다)."""
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > request.max_request_bytes:
        raise ValueError(
            f"조각 {request.chunk.index}의 요청 크기 {size}바이트가 인라인 한도"
            f" {request.max_request_bytes}바이트를 넘는다"
        )


def _transcription_attempt_meta(
    watch: _AttemptWatch, request: GeminiTranscriptionRequest, body: dict[str, Any], audio: bytes
) -> RequestAttemptMeta:
    """관찰 메타 — 오디오(수 MB의 base64)는 다시 싣지 않고 sha256과 크기만 남긴다."""
    audio_facts = {
        "type": "audio",
        "mime_type": request.mime_type,
        "sha256": hashlib.sha256(audio).hexdigest(),
        "bytes": len(audio),
    }
    chunk = request.chunk
    return RequestAttemptMeta(
        attempt_id=uuid4().hex,
        operation=GEMINI_TRANSCRIBE_OPERATION,
        model=request.model,
        kind=CAPTION_TIMING_KIND,
        stem=watch.stem,
        source=watch.source,
        chunk_index=chunk.index,
        chunk_offset=chunk.offset,
        chunk_duration=chunk.duration,
        options=_snapshot_options({**body, "input": [audio_facts]}),
    )


def _interaction(client: Any, body: Mapping[str, object]) -> dict[str, Any]:
    from . import gemini_messages

    return gemini_messages.post_interaction(client, body)


# ─────────────────────────────────────────────────────────────
#  응답 해석
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
#  전사 결과형 (P3-01)
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
#  픽스처 저장 (record 전용)
# ─────────────────────────────────────────────────────────────
