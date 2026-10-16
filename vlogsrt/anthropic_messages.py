"""Claude Messages API의 HTTP 전송(D54) — 네트워크 모듈을 import하는 유일한 파일.

이 파일을 import할 수 있는 것은 `transcribe.py` 하나다. 승인 문·관찰·취소는
`transcribe.request_claude_messages`가 지고, 여기는 요청 한 번을 보내고 응답 한 번을
받을 뿐이다. `anthropic` SDK를 쓰지 않는다(규칙 10) — 표준 라이브러리 urllib로 부른다.

재시도·스트리밍이 없다. 429·529·시간 초과는 명시적 실패 기록이어야 조용한 지연이
되지 않는다(Luna 경계의 `max_retries=0`과 같은 판단이다). 리다이렉트도 따르지 않는다 —
키를 실은 요청이 다른 호스트로 넘어가는 길을 남기지 않는다. https만 쓴다 — base_url은
클라이언트를 만들 때와 키를 얹기 직전에 두 번 검사하고, opener에는 https 처리기(와 환경
프록시를 따르는 처리기)만 있다.
"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import env

MESSAGES_PATH = "/v1/messages"
REQUEST_ID_HEADER = "request-id"
ERROR_MESSAGE_LIMIT = 200
"""오류 문면에 싣는 공급자 메시지의 최대 글자 수 — 진단에 넉넉하고 화면을 덮지 않는다."""

Opener = Callable[[urllib.request.Request, float], Any]
"""(요청, 시간 한도) → 응답. 시험은 가짜를 주입하고, 실제는 리다이렉트를 끈 opener다."""


class ClaudeAPIStatusError(RuntimeError):
    """2xx가 아닌 응답 — 상태 코드·오류 종류·요청 신원만 싣는다(본문·키는 싣지 않는다)."""

    def __init__(self, status_code: int, error_type: str, request_id: str, message: str) -> None:
        detail = f": {message}" if message else ""
        super().__init__(f"Claude API {status_code} {error_type}{detail}")
        self.status_code = status_code
        self.error_type = error_type
        self.request_id = request_id


class ClaudeAPITimeoutError(RuntimeError):
    """시간 초과 — 응답 수신이 불명확하다. 클래스 이름이 분류(`provider_error`)의 열쇠다."""


class ClaudeAPIConnectionError(RuntimeError):
    """연결 실패 — 응답 수신이 불명확하다. 클래스 이름이 분류의 열쇠다."""


class ClaudeResponseError(RuntimeError):
    """2xx인데 본문이 JSON 객체가 아니다 — 판정으로 받아 줄 것이 없다."""


@dataclass(frozen=True, slots=True)
class ClaudeMessagesClient:
    """Messages API 클라이언트 — 값은 받은 것만 쓰고 환경을 스스로 읽지 않는다.

    키는 repr에 나오지 않는다. `opener`는 시험의 주입 자리라 비교·출력에서 뺀다.
    """

    api_key: str = field(repr=False)
    base_url: str
    api_version: str
    timeout_seconds: float
    opener: Opener | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_https(self.base_url)


def _require_https(base_url: str) -> None:
    """키는 https로만 나간다 — 평문·파일·다른 스킴의 엔드포인트는 만들 수도 쓸 수도 없다."""
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Claude API base_url은 호스트가 있는 https URL이어야 한다")


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """3xx를 따라가지 않는다 — None을 돌려주면 urllib이 그 응답을 HTTPError로 올린다."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """https·프록시·리다이렉트 거절 처리기만 있는 opener — 실제 전송의 유일한 길이다.

    `urllib.request.build_opener`는 HTTP·FTP·File·Data 처리기도 함께 단다 — 그 길로 키가
    평문이나 다른 스킴으로 새지 않게 손으로 짓는다. 프록시 처리기는 둔다: OpenAI 경로
    (httpx)처럼 HTTP(S)_PROXY·ALL_PROXY·NO_PROXY·macOS 시스템 프록시를 따라야 프록시만
    열린 망에서 전사는 사 놓고 Claude만 매번 실패하는 일이 없다. https 요청은 프록시를
    지나도 CONNECT 터널이라 키는 원 호스트와의 TLS 안에만 있다. 나머지는 상태 코드를
    오류로 올리는 처리기 둘과 모르는 스킴을 URLError로 닫는 처리기다.
    """
    director = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _RefuseRedirect(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.UnknownHandler(),
    ):
        director.add_handler(handler)
    return director


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return build_opener().open(request, timeout=timeout)


def post_messages(client: ClaudeMessagesClient, body: Mapping[str, object]) -> dict[str, Any]:
    """`POST {base_url}/v1/messages` 한 번 — 파싱한 응답에 `request_id`를 얹어 돌려준다."""
    _require_https(client.base_url)  # 생성 뒤의 바꿔치기도 키를 얹기 전에 다시 본다
    request = urllib.request.Request(
        client.base_url.rstrip("/") + MESSAGES_PATH,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        ),
        method="POST",
        headers={
            "x-api-key": client.api_key,
            "anthropic-version": client.api_version,
            "content-type": "application/json",
        },
    )
    status, headers, raw = _exchange(client, request)
    if not 200 <= status < 300:
        raise _status_error(status, headers, raw, client)
    return _parsed(raw, _header(headers, REQUEST_ID_HEADER))


def _exchange(
    client: ClaudeMessagesClient, request: urllib.request.Request
) -> tuple[int, Any, bytes]:
    """요청을 한 번 보내고 (상태, 헤더, 본문)을 받는다 — 전송 실패는 형식이 정해진 예외다."""
    open_request = client.opener or _default_opener
    try:
        with open_request(request, client.timeout_seconds) as response:
            return int(response.status), response.headers, response.read()
    except urllib.error.HTTPError as error:
        try:
            raw = error.read() if error.fp is not None else b""
        finally:
            error.close()
        return error.code, error.headers, raw
    except TimeoutError as error:
        raise ClaudeAPITimeoutError("Claude API 시간 초과 — 응답 수신 불명확") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ClaudeAPITimeoutError("Claude API 연결 시간 초과") from error
        raise ClaudeAPIConnectionError("Claude API 연결 실패 — 응답 수신 불명확") from error
    except (OSError, http.client.HTTPException) as error:
        raise ClaudeAPIConnectionError("Claude API 연결이 끊겼다 — 응답 수신 불명확") from error


def _header(headers: Any, name: str) -> str:
    value = headers.get(name) if headers is not None else None
    return str(value) if value else ""


def _parsed(raw: bytes, request_id: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise ClaudeResponseError("Claude 응답 본문이 JSON이 아니다") from error
    if not isinstance(parsed, dict):
        raise ClaudeResponseError(f"Claude 응답 본문이 JSON 객체가 아니다: {type(parsed).__name__}")
    if request_id:
        parsed["request_id"] = request_id
    return parsed


def _status_error(
    status: int, headers: Any, raw: bytes, client: ClaudeMessagesClient
) -> ClaudeAPIStatusError:
    """오류 본문에서 종류·메시지만 꺼낸다 — 모양이 다르면 본문을 되뇌지 않는다."""
    error_type, message, body_request_id = "unparsed_error_body", "", ""
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("error"), dict):
        error_type = str(document["error"].get("type") or error_type)
        message = str(document["error"].get("message") or "")
        body_request_id = str(document.get("request_id") or "")
    message = env.redact_secrets(message, secrets=(client.api_key,))[:ERROR_MESSAGE_LIMIT]
    request_id = _header(headers, REQUEST_ID_HEADER) or body_request_id
    return ClaudeAPIStatusError(status, error_type, request_id, message)
