"""Gemini generateContent API의 HTTP 전송(W-30a, D83) — 네트워크 모듈을 import하는 둘째 파일.

이 파일을 import할 수 있는 것은 `transcribe.py` 하나다. 승인 문·관찰·취소는
`transcribe.request_gemini_generate`가 지고, 여기는 요청 한 번을 보내고 응답 한 번을 받을
뿐이다. `google-genai` SDK를 쓰지 않는다(규칙 10) — 표준 라이브러리 urllib로 부른다.
`anthropic_messages.py`의 거울이다 — 그 파일은 이 파일을 모르고 이 파일도 그 파일을 모른다
(전송 파일끼리 서로 import하지 않는다).

의미 분석은 `generateContent`를 쓴다 — 상태 없는 한 번의 요청·응답이다. 새 Interactions API는
기본으로 요청을 서버에 저장한다(`store=true`) — 판정 창의 프레임을 공급자 쪽에 남길 까닭이 없다.
시간 엔진 Gemini 3.5 Transcribe(W-31, D92)는 Interactions에만 있다 — 그 요청은 문(`transcribe`)이
`store: false`를 실어 보내고, 여기는 `POST /{version}/interactions` 한 번을 보낼 뿐이다.

재시도·스트리밍이 없다. 429·503·시간 초과는 명시적 실패 기록이어야 조용한 지연이 되지
않는다. 리다이렉트도 따르지 않는다 — 키를 실은 요청이 다른 호스트로 넘어가는 길을 남기지
않는다. https만 쓴다 — base_url은 클라이언트를 만들 때와 키를 얹기 직전에 두 번 검사하고,
opener에는 https 처리기(와 환경 프록시를 따르는 처리기)만 있다. 키는 쿼리(`?key=`)가 아니라
`x-goog-api-key` 헤더로만 간다 — URL은 프록시·오류 문면에 남을 수 있다.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import env

GENERATE_PATH = "/{version}/models/{model}:generateContent"
INTERACTIONS_PATH = "/{version}/interactions"
API_KEY_HEADER = "x-goog-api-key"
ERROR_MESSAGE_LIMIT = 200
"""오류 문면에 싣는 공급자 메시지의 최대 글자 수 — 진단에 넉넉하고 화면을 덮지 않는다."""
UNPARSED_ERROR = "unparsed_error_body"

MODEL_SEGMENT = re.compile(r"[a-z0-9][a-z0-9.-]*")
"""경로에 들어가는 모델 이름 — 소문자·숫자·점·하이픈만(`gemini-3.8-flash`). `/`·`:`·`?`가 들면
다른 경로·연산·쿼리가 된다."""
VERSION_SEGMENT = re.compile(r"v[0-9][a-z0-9]*")
"""경로에 들어가는 API 판(`v1beta`·`v1`)."""
STATUS_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
"""오류 문면에 싣는 공급자 상태·까닭의 모양 — 서버 글자를 그대로 되뇌지 않는다(대문자 토큰만)."""
ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"

Opener = Callable[[urllib.request.Request, float], Any]
"""(요청, 시간 한도) → 응답. 시험은 가짜를 주입하고, 실제는 리다이렉트를 끈 opener다."""


class GeminiAPIStatusError(RuntimeError):
    """2xx가 아닌 응답 — 상태 코드·오류 상태·까닭만 싣는다(본문·키는 싣지 않는다).

    `error_type`은 Google 오류 본문의 `error.status`(`RESOURCE_EXHAUSTED` 따위), `reason`은
    `error.details[]`의 `ErrorInfo.reason`(`API_KEY_INVALID` 따위)이다 — 키가 틀리면 Google은
    401이 아니라 400을 돌려주므로 분류(`provider_error`)가 이 칸을 본다.
    """

    def __init__(self, status_code: int, error_type: str, reason: str, message: str) -> None:
        detail = f": {message}" if message else ""
        super().__init__(f"Gemini API {status_code} {error_type}{detail}")
        self.status_code = status_code
        self.error_type = error_type
        self.reason = reason


class GeminiAPITimeoutError(RuntimeError):
    """시간 초과 — 응답 수신이 불명확하다. 클래스 이름이 분류(`provider_error`)의 열쇠다."""


class GeminiAPIConnectionError(RuntimeError):
    """연결 실패 — 응답 수신이 불명확하다. 클래스 이름이 분류의 열쇠다."""


class GeminiResponseError(RuntimeError):
    """2xx인데 본문이 JSON 객체가 아니다 — 판정으로 받아 줄 것이 없다."""


@dataclass(frozen=True, slots=True)
class GeminiClient:
    """generateContent 클라이언트 — 값은 받은 것만 쓰고 환경을 스스로 읽지 않는다.

    키는 repr에 나오지 않는다. `opener`는 시험의 주입 자리라 비교·출력에서 뺀다.
    """

    api_key: str = field(repr=False)
    base_url: str
    api_version: str
    timeout_seconds: float
    opener: Opener | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_endpoint(self)


def _require_endpoint(client: GeminiClient) -> None:
    """키는 https 호스트의 정해진 경로로만 나간다 — 클라이언트를 만들 때와 키를 얹기 직전에 본다.

    base_url은 스킴·호스트(·포트)뿐이다: userinfo(`u:p@`)·쿼리(`?`)·프래그먼트(`#`)·경로가 있으면
    연산이 쿼리로 밀리거나(`https://h?x=`) 경로가 사라진다(`https://h#`). 빈 구분자도 닫는다.
    """
    parts = urlsplit(client.base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Gemini API base_url은 호스트가 있는 https URL이어야 한다")
    if parts.username is not None or "?" in client.base_url or "#" in client.base_url:
        raise ValueError(
            "Gemini API base_url은 https 호스트뿐이어야 한다(사용자 정보·쿼리·조각 금지)"
        )
    if parts.path not in ("", "/"):
        raise ValueError("Gemini API base_url은 https 호스트뿐이어야 한다(경로 금지)")
    if not VERSION_SEGMENT.fullmatch(client.api_version):
        raise ValueError(f"Gemini API 버전이 경로 조각이 아니다: {client.api_version!r}")


def require_model_segment(model: str) -> None:
    """모델 이름은 경로 한 조각이어야 한다 — 문(`transcribe`)이 승인 전에, 전송이 키를 얹기
    전에 본다."""
    if not MODEL_SEGMENT.fullmatch(model):
        raise ValueError(f"Gemini 모델 이름이 경로 조각이 아니다: {model!r}")


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

    까닭은 `anthropic_messages.build_opener`와 같다 — `urllib.request.build_opener`가 함께 다는
    HTTP·FTP·File·Data 처리기로 키가 새지 않게 손으로 짓고, 프록시만 열린 망에서도 되도록 환경
    프록시는 따른다(https는 CONNECT 터널이라 키는 원 호스트와의 TLS 안에만 있다).
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


def post_generate_content(
    client: GeminiClient, model: str, body: Mapping[str, object]
) -> dict[str, Any]:
    """`POST {base_url}/{version}/models/{model}:generateContent` 한 번 — 파싱한 응답을 돌려준다.

    모델은 본문이 아니라 경로에 산다 — 경로 조각이 아닌 이름은 키를 얹기 전에 닫는다.
    """
    _require_endpoint(client)  # 생성 뒤의 바꿔치기도 키를 얹기 전에 다시 본다
    require_model_segment(model)
    path = GENERATE_PATH.format(version=client.api_version, model=model)
    return _post_json(client, path, body)


def post_interaction(client: GeminiClient, body: Mapping[str, object]) -> dict[str, Any]:
    """`POST {base_url}/{version}/interactions` 한 번(W-31a, D92) — 파싱한 응답을 돌려준다.

    Interactions에서는 모델이 경로가 아니라 본문에 산다 — 본문의 모델 이름도 경로 조각과 같은
    모양만 받는다(키를 얹기 전에 닫는다). 오류·시간 초과·리다이렉트 거절은 generateContent와
    한 길이다.
    """
    _require_endpoint(client)
    model = body.get("model")
    if not isinstance(model, str):
        raise ValueError(f"Gemini Interactions 본문의 모델 이름이 없다: {model!r}")
    require_model_segment(model)
    path = INTERACTIONS_PATH.format(version=client.api_version)
    return _post_json(client, path, body)


def _post_json(client: GeminiClient, path: str, body: Mapping[str, object]) -> dict[str, Any]:
    request = urllib.request.Request(
        client.base_url.rstrip("/") + path,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        ),
        method="POST",
        headers={API_KEY_HEADER: client.api_key, "content-type": "application/json"},
    )
    status, raw = _exchange(client, request)
    if not 200 <= status < 300:
        raise _status_error(status, raw, client)
    return _parsed(raw)


def _exchange(client: GeminiClient, request: urllib.request.Request) -> tuple[int, bytes]:
    """요청을 한 번 보내고 (상태, 본문)을 받는다 — 전송 실패는 형식이 정해진 예외다."""
    open_request = client.opener or _default_opener
    try:
        with open_request(request, client.timeout_seconds) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        try:
            raw = error.read() if error.fp is not None else b""
        finally:
            error.close()
        return error.code, raw
    except TimeoutError as error:
        raise GeminiAPITimeoutError("Gemini API 시간 초과 — 응답 수신 불명확") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise GeminiAPITimeoutError("Gemini API 연결 시간 초과") from error
        raise GeminiAPIConnectionError("Gemini API 연결 실패 — 응답 수신 불명확") from error
    except (OSError, http.client.HTTPException) as error:
        raise GeminiAPIConnectionError("Gemini API 연결이 끊겼다 — 응답 수신 불명확") from error


def _parsed(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise GeminiResponseError("Gemini 응답 본문이 JSON이 아니다") from error
    if not isinstance(parsed, dict):
        raise GeminiResponseError(f"Gemini 응답 본문이 JSON 객체가 아니다: {type(parsed).__name__}")
    return parsed


def _status_error(status: int, raw: bytes, client: GeminiClient) -> GeminiAPIStatusError:
    """오류 본문에서 상태·까닭·메시지만 꺼낸다 — 모양이 다르면 본문을 되뇌지 않는다."""
    error_type, reason, message = UNPARSED_ERROR, "", ""
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("error"), dict):
        error = document["error"]
        error_type = _token(error.get("status")) or error_type
        message = str(error.get("message") or "")
        reason = _error_reason(error.get("details"))
    message = env.redact_secrets(message, secrets=(client.api_key,))[:ERROR_MESSAGE_LIMIT]
    return GeminiAPIStatusError(status, error_type, reason, message)


def _token(value: object) -> str:
    """상태·까닭이 대문자 토큰이면 그대로, 아니면 빈 글자 — 서버 글자를 길이 없이 되뇌지 않는다."""
    return value if isinstance(value, str) and STATUS_TOKEN.fullmatch(value) else ""


def _error_reason(details: object) -> str:
    """`error.details[]`에서 첫 `ErrorInfo`의 `reason` — 없거나 모양이 다르면 빈 글자다."""
    if not isinstance(details, list):
        return ""
    for detail in details:
        if isinstance(detail, dict) and detail.get("@type") == ERROR_INFO_TYPE:
            return _token(detail.get("reason"))
    return ""
