"""Claude Messages API의 HTTP 전송(D54) — 표준 라이브러리 urllib 한 번, 재시도 없음.

가짜 opener가 실제 전송 자리에 앉는다. 진짜 opener는 `tests/conftest.py`가 막은 소켓에
닿아 `NetworkBlocked`로 끝난다 — 유료 호출 0회.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import anthropic_messages
from vlogsrt.anthropic_messages import (
    ClaudeAPIConnectionError,
    ClaudeAPIStatusError,
    ClaudeAPITimeoutError,
    ClaudeMessagesClient,
    ClaudeResponseError,
    post_messages,
)
from vlogsrt.task_runtime import provider_error

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic" / "claude"
SECRET = "sk-ant-synthetic-never-display"
BODY = {
    "model": "claude-opus-5-5",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "안녕"}]}],
}


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    """`http.client.HTTPResponse`의 최소 모습 — 상태·헤더·본문, 문맥 관리자."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOpener:
    """실제 전송 자리의 가짜 — 받은 요청을 기록하고 정해진 응답·오류를 돌려준다."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"{}",
        headers: dict[str, str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers if headers is not None else {"request-id": "req_synthetic_ok"}
        self.error = error
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        if self.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "error", self.headers, io.BytesIO(self.body)
            )
        return FakeResponse(self.status, self.body, self.headers)


def client_with(opener: Any, base_url: str = "https://api.anthropic.com") -> ClaudeMessagesClient:
    return ClaudeMessagesClient(
        api_key=SECRET,
        base_url=base_url,
        api_version="2023-06-01",
        timeout_seconds=300.0,
        opener=opener,
    )


# ─────────────────────────────────────────────────────────────
#  요청 모양 — URL·메서드·헤더 셋·압축 JSON
# ─────────────────────────────────────────────────────────────


def test_post_sends_exact_url_method_headers_and_compact_json() -> None:
    opener = FakeOpener(body=_fixture_bytes("message_accepted.json"))
    reply = post_messages(client_with(opener), BODY)

    ((request, timeout),) = opener.calls
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_method() == "POST"
    assert dict(request.header_items()) == {
        "X-api-key": SECRET,
        "Anthropic-version": "2023-06-01",
        "Content-type": "application/json",
    }
    assert request.data == json.dumps(BODY, ensure_ascii=False, separators=(",", ":")).encode()
    assert timeout == 300.0
    assert reply["stop_reason"] == "end_turn"
    assert reply["request_id"] == "req_synthetic_ok"  # 응답 헤더 `request-id`에서 온다


def test_a_trailing_slash_base_url_does_not_double_the_path() -> None:
    opener = FakeOpener()
    post_messages(client_with(opener, "https://api.anthropic.com/"), BODY)
    assert opener.calls[0][0].full_url == "https://api.anthropic.com/v1/messages"


def test_the_client_repr_never_shows_the_key() -> None:
    client = client_with(FakeOpener())
    assert SECRET not in repr(client)
    assert SECRET not in str(client)


# ─────────────────────────────────────────────────────────────
#  오류 — 형식이 정해진 예외, 재시도 없음, 키는 문면에 없다
# ─────────────────────────────────────────────────────────────


def test_429_is_a_typed_rate_limit_error_sent_once() -> None:
    opener = FakeOpener(
        status=429, body=_fixture_bytes("error_429.json"), headers={"request-id": "req_h"}
    )
    with pytest.raises(ClaudeAPIStatusError) as info:
        post_messages(client_with(opener), BODY)
    error = info.value
    assert (error.status_code, error.error_type) == (429, "rate_limit_error")
    assert error.request_id == "req_h"
    assert len(opener.calls) == 1  # 재시도 없음 — 실패는 한 번의 명시적 시도다
    assert provider_error(error)[0] == "rate_limit"


def test_529_is_an_overloaded_error_sent_once() -> None:
    opener = FakeOpener(status=529, body=_fixture_bytes("error_529.json"), headers={})
    with pytest.raises(ClaudeAPIStatusError) as info:
        post_messages(client_with(opener), BODY)
    assert (info.value.status_code, info.value.error_type) == (529, "overloaded_error")
    assert info.value.request_id == "req_synthetic_529"  # 헤더가 없으면 본문의 신원
    assert len(opener.calls) == 1
    assert provider_error(info.value)[:1] == ("overloaded",)


def test_401_message_never_contains_the_key() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {"type": "authentication_error", "message": f"invalid x-api-key {SECRET}"},
        }
    ).encode()
    opener = FakeOpener(status=401, body=body, headers={})
    with pytest.raises(ClaudeAPIStatusError) as info:
        post_messages(client_with(opener), BODY)
    assert SECRET not in str(info.value)
    assert "authentication_error" in str(info.value)
    assert provider_error(info.value)[0] == "authentication"


def test_a_long_error_message_is_truncated() -> None:
    body = json.dumps(
        {"type": "error", "error": {"type": "invalid_request_error", "message": "가" * 5000}}
    ).encode()
    with pytest.raises(ClaudeAPIStatusError) as info:
        post_messages(client_with(FakeOpener(status=400, body=body, headers={})), BODY)
    assert len(str(info.value)) < 400


def test_a_non_json_error_body_is_not_echoed() -> None:
    opener = FakeOpener(status=502, body=b"<html>proxy</html>", headers={})
    with pytest.raises(ClaudeAPIStatusError) as info:
        post_messages(client_with(opener), BODY)
    assert "<html>" not in str(info.value)
    assert provider_error(info.value)[2] is True  # 502 — 수신 불명확


@pytest.mark.parametrize(
    "error",
    [TimeoutError("read timed out"), urllib.error.URLError(TimeoutError("connect timed out"))],
)
def test_timeouts_are_the_timeout_category(error: BaseException) -> None:
    opener = FakeOpener(error=error)
    with pytest.raises(ClaudeAPITimeoutError) as info:
        post_messages(client_with(opener), BODY)
    assert len(opener.calls) == 1
    category, _message, uncertain = provider_error(info.value)
    assert (category, uncertain) == ("timeout", True)


def test_url_errors_are_the_connection_category() -> None:
    opener = FakeOpener(error=urllib.error.URLError("name resolution failed"))
    with pytest.raises(ClaudeAPIConnectionError) as info:
        post_messages(client_with(opener), BODY)
    category, _message, uncertain = provider_error(info.value)
    assert (category, uncertain) == ("connection", True)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_a_200_that_is_not_a_json_object_is_a_response_error(body: bytes) -> None:
    with pytest.raises(ClaudeResponseError):
        post_messages(client_with(FakeOpener(body=body)), BODY)


# ─────────────────────────────────────────────────────────────
#  진짜 opener — 리다이렉트를 따르지 않고, 시험에서는 소켓에서 막힌다
# ─────────────────────────────────────────────────────────────


class RedirectingHTTPS(urllib.request.BaseHandler):
    """기본 HTTPS 처리기보다 먼저 서서 302를 돌려주는 가짜 — 몇 번 불렸는지 센다."""

    handler_order = 100

    def __init__(self) -> None:
        self.urls: list[str] = []

    def https_open(self, request: urllib.request.Request) -> Any:
        self.urls.append(request.full_url)
        headers = Message()
        headers["Location"] = "https://elsewhere.example/v1/messages"
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
        response.msg = "Found"  # type: ignore[attr-defined]
        return response


def test_the_default_opener_never_follows_a_redirect() -> None:
    """키를 실은 요청이 다른 호스트로 따라가지 않는다 — 3xx는 그대로 오류다."""
    director = anthropic_messages.build_opener()
    redirecting = RedirectingHTTPS()
    director.add_handler(redirecting)
    client = client_with(lambda request, timeout: director.open(request, timeout=timeout))
    with pytest.raises(ClaudeAPIStatusError) as info:
        post_messages(client, BODY)
    assert info.value.status_code == 302
    assert redirecting.urls == ["https://api.anthropic.com/v1/messages"]


def test_the_default_opener_is_blocked_by_the_test_socket_guard() -> None:
    client = ClaudeMessagesClient(
        api_key=SECRET,
        base_url="https://api.anthropic.com",
        api_version="2023-06-01",
        timeout_seconds=5.0,
    )
    with pytest.raises(RuntimeError) as info:  # conftest의 NetworkBlocked — 변환되지 않는다
        post_messages(client, BODY)
    assert type(info.value).__name__ == "NetworkBlocked"


# ─────────────────────────────────────────────────────────────
#  https만 — 키는 평문·다른 스킴으로 나가지 않는다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.anthropic.com",
        "ftp://api.anthropic.com",
        "file:///etc/passwd",
        "https://",
        "api.anthropic.com",
        "",
    ],
)
def test_a_non_https_base_url_cannot_make_a_client(base_url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        client_with(FakeOpener(), base_url)


def test_a_tampered_base_url_is_refused_before_the_key_is_attached() -> None:
    """생성 뒤 바꿔치기(`object.__setattr__`)도 전송 직전에 다시 본다 — opener는 불리지 않는다."""
    opener = FakeOpener()
    client = client_with(opener)
    object.__setattr__(client, "base_url", "http://api.anthropic.com")
    with pytest.raises(ValueError, match="https"):
        post_messages(client, BODY)
    assert opener.calls == []


_PROXY_ENV = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _proxy_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """프록시 환경을 시험 값으로만 — 대소문자 둘 다 지운 뒤 준 값만 얹는다(소문자가 이긴다)."""
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
        monkeypatch.setenv(name.upper(), value)


def test_the_default_opener_speaks_https_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP·FTP·File·Data 처리기가 없다 — 프록시가 설정돼 있어도 다른 스킴은 소켓 전에 닫힌다.

    프록시 처리기는 있다 — OpenAI 경로(httpx)처럼 환경 프록시를 따른다. 그래야 프록시만
    열린 망에서 전사는 사 놓고 Claude만 매번 실패하는 일이 없다.
    """
    _proxy_env(
        monkeypatch, http_proxy="http://proxy.test:3128", https_proxy="http://proxy.test:3128"
    )
    director = anthropic_messages.build_opener()
    kinds = {type(handler) for handler in director.handlers}
    for banned in (
        urllib.request.HTTPHandler,
        urllib.request.FTPHandler,
        urllib.request.FileHandler,
        urllib.request.DataHandler,
        urllib.request.HTTPRedirectHandler,
    ):
        assert banned not in kinds, banned
    assert urllib.request.HTTPSHandler in kinds
    assert urllib.request.ProxyHandler in kinds
    for url in ("http://api.anthropic.com/v1/messages", "file:///etc/passwd", "data:,x"):
        with pytest.raises(urllib.error.URLError, match="unknown url type"):
            director.open(urllib.request.Request(url), timeout=1)


class TunnelSpy(urllib.request.BaseHandler):
    """프록시 처리기(100) 뒤·HTTPS 처리기(500) 앞에 서서 요청이 어디로 가는지만 본다."""

    handler_order = 200

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str | None, str]] = []

    def https_open(self, request: urllib.request.Request) -> Any:
        tunnel = getattr(request, "_tunnel_host", None)
        self.seen.append((request.type, request.host, tunnel, request.full_url))
        body = io.BytesIO(b'{"type": "message"}')
        response = urllib.response.addinfourl(body, Message(), request.full_url, 200)
        response.msg = "OK"  # type: ignore[attr-defined]
        return response


def _send_through_spy() -> TunnelSpy:
    director = anthropic_messages.build_opener()
    spy = TunnelSpy()
    director.add_handler(spy)
    post_messages(
        client_with(lambda request, timeout: director.open(request, timeout=timeout)), BODY
    )
    return spy


def test_https_through_a_configured_proxy_is_a_connect_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프록시는 TCP 길만 잇는다 — 요청은 원 호스트로 가는 https 그대로(CONNECT 터널)다.

    키 헤더는 터널 안의 TLS로 원 호스트에만 간다 — 프록시가 평문 요청을 받는 길이 아니다.
    """
    _proxy_env(monkeypatch, https_proxy="http://proxy.test:3128")
    spy = _send_through_spy()
    assert spy.seen == [
        ("https", "proxy.test:3128", "api.anthropic.com", "https://api.anthropic.com/v1/messages")
    ]


def test_no_proxy_bypasses_the_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    _proxy_env(monkeypatch, https_proxy="http://proxy.test:3128", no_proxy="api.anthropic.com")
    spy = _send_through_spy()
    assert spy.seen == [
        ("https", "api.anthropic.com", None, "https://api.anthropic.com/v1/messages")
    ]
