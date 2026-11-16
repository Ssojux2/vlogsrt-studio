"""Gemini generateContent의 HTTP 전송(W-30a, D83) — 표준 라이브러리 urllib 한 번, 재시도 없음.

가짜 opener가 실제 전송 자리에 앉는다. 진짜 opener는 `tests/conftest.py`가 막은 소켓에
닿아 `NetworkBlocked`로 끝난다 — 유료 호출 0회. Claude 전송 시험(`test_anthropic_messages.py`)의
거울이다 — 다른 것은 경로(`/v1beta/models/{model}:generateContent`)·키 헤더(`x-goog-api-key`)·
오류 본문 모양(`error.status`)·모델 이름이 경로에 들어가는 것 하나다.
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

from tests.test_anthropic_messages import FakeOpener, RedirectingHTTPS, TunnelSpy, _proxy_env
from vlogsrt import gemini_messages
from vlogsrt.gemini_messages import (
    GeminiAPIConnectionError,
    GeminiAPIStatusError,
    GeminiAPITimeoutError,
    GeminiClient,
    GeminiResponseError,
    post_generate_content,
)
from vlogsrt.task_runtime import provider_error

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic" / "gemini"
SECRET = "AIzaSynthetic-never-display"
MODEL = "gemini-3.8-flash"
URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
BODY = {"contents": [{"role": "user", "parts": [{"text": "안녕"}]}]}


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def client_with(
    opener: Any, base_url: str = "https://generativelanguage.googleapis.com"
) -> GeminiClient:
    return GeminiClient(
        api_key=SECRET,
        base_url=base_url,
        api_version="v1beta",
        timeout_seconds=300.0,
        opener=opener,
    )


def opener_with(**kwargs: Any) -> FakeOpener:
    kwargs.setdefault("headers", {})
    return FakeOpener(**kwargs)


# ─────────────────────────────────────────────────────────────
#  요청 모양 — URL·메서드·헤더 둘·압축 JSON
# ─────────────────────────────────────────────────────────────


def test_post_sends_exact_url_method_headers_and_compact_json() -> None:
    opener = opener_with(body=_fixture_bytes("response_accepted.json"))
    reply = post_generate_content(client_with(opener), MODEL, BODY)

    ((request, timeout),) = opener.calls
    assert request.full_url == URL
    assert request.get_method() == "POST"
    assert dict(request.header_items()) == {
        "X-goog-api-key": SECRET,
        "Content-type": "application/json",
    }
    assert request.data == json.dumps(BODY, ensure_ascii=False, separators=(",", ":")).encode()
    assert timeout == 300.0
    assert reply["candidates"][0]["finishReason"] == "STOP"
    assert reply["responseId"] == "resp_synthetic_ok"
    assert SECRET not in request.full_url  # 키는 쿼리(`?key=`)가 아니라 헤더로만 간다


def test_a_trailing_slash_base_url_does_not_double_the_path() -> None:
    opener = opener_with()
    post_generate_content(
        client_with(opener, "https://generativelanguage.googleapis.com/"), MODEL, BODY
    )
    assert opener.calls[0][0].full_url == URL


@pytest.mark.parametrize(
    "model",
    [
        "",
        "Gemini-3.8-flash",
        "gemini/../../v1/files",
        "gemini-3.8-flash:streamGenerateContent",
        "gemini-3.8-flash?key=x",
        "gemini 3.8",
        "-gemini",
        "models/gemini-3.8-flash",
    ],
)
def test_a_model_name_that_is_not_a_plain_path_segment_is_refused(model: str) -> None:
    """모델 이름은 URL 경로에 들어간다 — 경로·연산·쿼리를 바꿀 수 있는 글자는 키를 얹기 전에
    닫는다."""
    opener = opener_with()
    with pytest.raises(ValueError, match="모델"):
        post_generate_content(client_with(opener), model, BODY)
    assert opener.calls == []


def test_the_client_repr_never_shows_the_key() -> None:
    client = client_with(opener_with())
    assert SECRET not in repr(client)
    assert SECRET not in str(client)


# ─────────────────────────────────────────────────────────────
#  오류 — 형식이 정해진 예외, 재시도 없음, 키는 문면에 없다
# ─────────────────────────────────────────────────────────────


def test_429_is_a_typed_rate_limit_error_sent_once() -> None:
    opener = opener_with(status=429, body=_fixture_bytes("error_429.json"))
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    error = info.value
    assert (error.status_code, error.error_type) == (429, "RESOURCE_EXHAUSTED")
    assert len(opener.calls) == 1  # 재시도 없음 — 실패는 한 번의 명시적 시도다
    assert provider_error(error)[0] == "rate_limit"
    assert (error.quota_id, error.quota_value, error.retry_delay_seconds) == ("", "", None)
    assert "밝히지 않았습니다" in provider_error(error)[1]  # 실데이터 2026-11-12 — 이름 없는 429


def _rate_limited(fixture: str) -> GeminiAPIStatusError:
    opener = opener_with(status=429, body=_fixture_bytes(fixture))
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    assert len(opener.calls) == 1  # 한도 칸을 읽어도 재시도는 없다
    return info.value


def test_a_daily_quota_429_keeps_which_quota_and_its_limit() -> None:
    error = _rate_limited("error_429_quota_day.json")
    assert error.quota_id == "GenerateRequestsPerDayPerProjectPerModel"
    assert (error.quota_value, error.retry_delay_seconds) == ("100", 43.0)
    code, message, uncertain = provider_error(error)
    assert (code, uncertain) == ("rate_limit", False)
    assert "하루 요청 한도" in message and "100" in message
    assert "GenerateRequestsPerDayPerProjectPerModel" in message


def test_a_per_minute_quota_429_says_to_wait_the_suggested_delay() -> None:
    error = _rate_limited("error_429_quota_minute.json")
    assert error.quota_id == "GenerateRequestsPerMinutePerProjectPerModel"
    assert (error.quota_value, error.retry_delay_seconds) == ("10", 12.4)
    code, message, _uncertain = provider_error(error)
    assert code == "rate_limit"
    assert "분당 요청 한도" in message and "10" in message and "13초" in message


def _quota_body(violation: object, retry_delay: object) -> bytes:
    details = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [violation]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
    ]
    error = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": details}
    return json.dumps({"error": error}).encode()


@pytest.mark.parametrize(
    ("violation", "retry_delay"),
    [
        ({"quotaId": "하루 한도", "quotaValue": "백"}, "43 seconds"),
        ({"quotaId": "x" * 200, "quotaValue": "1" * 40}, "-3s"),
        ({"quotaId": SECRET, "quotaValue": 100}, {"seconds": 43}),
        ("not a violation", None),
    ],
)
def test_odd_quota_details_are_dropped_not_echoed(violation: object, retry_delay: object) -> None:
    opener = opener_with(status=429, body=_quota_body(violation, retry_delay))
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    error = info.value
    assert (error.quota_id, error.quota_value, error.retry_delay_seconds) == ("", "", None)
    message = provider_error(error)[1]
    assert message.startswith(
        "API 요청 한도에 걸렸습니다 · 공급자가 어느 한도인지 밝히지 않았습니다"
    )
    assert SECRET not in message


def test_the_retry_after_header_is_read_when_the_body_has_no_delay() -> None:
    """본문에 `RetryInfo`가 없으면 `Retry-After`(정수 초)를 대기 초로 싣는다(D141).

    전송은 여전히 한 번이다 — 다시 보내는 것은 문(`transcribe`)이다."""
    opener = opener_with(
        status=429, body=_fixture_bytes("error_429.json"), headers={"Retry-After": "7"}
    )
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    assert info.value.retry_delay_seconds == 7.0
    assert len(opener.calls) == 1


def test_the_body_delay_wins_over_the_retry_after_header() -> None:
    opener = opener_with(
        status=429,
        body=_fixture_bytes("error_429_quota_minute.json"),
        headers={"Retry-After": "40"},
    )
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    assert info.value.retry_delay_seconds == 12.4


@pytest.mark.parametrize(
    "value", ["soon", "-1", "1e3", "7.5", "1234567", "Wed, 21 Oct 2026 07:28:00 GMT", ""]
)
def test_odd_retry_after_headers_are_dropped(value: str) -> None:
    opener = opener_with(
        status=429, body=_fixture_bytes("error_429.json"), headers={"Retry-After": value}
    )
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    assert info.value.retry_delay_seconds is None


def test_503_is_an_uncertain_connection_error_sent_once() -> None:
    opener = opener_with(status=503, body=_fixture_bytes("error_503.json"))
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    assert (info.value.status_code, info.value.error_type) == (503, "UNAVAILABLE")
    assert len(opener.calls) == 1
    assert provider_error(info.value)[0::2] == ("connection", True)


@pytest.mark.parametrize(
    ("status", "kind", "category"),
    [
        (400, "INVALID_ARGUMENT", "request_format"),
        (401, "UNAUTHENTICATED", "authentication"),
        (403, "PERMISSION_DENIED", "model_access"),
        (404, "NOT_FOUND", "model_access"),
    ],
)
def test_google_status_codes_map_to_the_shared_categories(
    status: int, kind: str, category: str
) -> None:
    body = json.dumps({"error": {"code": status, "message": "합성", "status": kind}}).encode()
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener_with(status=status, body=body)), MODEL, BODY)
    assert info.value.error_type == kind
    assert provider_error(info.value)[0] == category


def test_an_invalid_key_is_authentication_although_google_answers_400() -> None:
    """키가 틀리면 Google은 400 INVALID_ARGUMENT + `ErrorInfo.reason = API_KEY_INVALID`다."""
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                    }
                ],
            }
        }
    ).encode()
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener_with(status=400, body=body)), MODEL, BODY)
    assert (info.value.error_type, info.value.reason) == ("INVALID_ARGUMENT", "API_KEY_INVALID")
    assert provider_error(info.value)[0] == "authentication"


def test_an_error_message_never_contains_the_key() -> None:
    body = json.dumps(
        {"error": {"code": 400, "message": f"API key not valid: {SECRET}", "status": "X"}}
    ).encode()
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener_with(status=400, body=body)), MODEL, BODY)
    assert SECRET not in str(info.value)
    assert "API key not valid" in str(info.value)


def test_a_long_error_message_is_truncated() -> None:
    body = json.dumps({"error": {"code": 400, "message": "가" * 5000, "status": "X"}}).encode()
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener_with(status=400, body=body)), MODEL, BODY)
    assert len(str(info.value)) < 400


def test_a_non_json_error_body_is_not_echoed() -> None:
    opener = opener_with(status=502, body=b"<html>proxy</html>")
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    assert "<html>" not in str(info.value)
    assert info.value.error_type == "unparsed_error_body"
    assert provider_error(info.value)[2] is True  # 502 — 수신 불명확


@pytest.mark.parametrize(
    "error",
    [TimeoutError("read timed out"), urllib.error.URLError(TimeoutError("connect timed out"))],
)
def test_timeouts_are_the_timeout_category(error: BaseException) -> None:
    opener = opener_with(error=error)
    with pytest.raises(GeminiAPITimeoutError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    assert len(opener.calls) == 1
    category, _message, uncertain = provider_error(info.value)
    assert (category, uncertain) == ("timeout", True)


def test_url_errors_are_the_connection_category() -> None:
    opener = opener_with(error=urllib.error.URLError("name resolution failed"))
    with pytest.raises(GeminiAPIConnectionError) as info:
        post_generate_content(client_with(opener), MODEL, BODY)
    category, _message, uncertain = provider_error(info.value)
    assert (category, uncertain) == ("connection", True)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_a_200_that_is_not_a_json_object_is_a_response_error(body: bytes) -> None:
    with pytest.raises(GeminiResponseError):
        post_generate_content(client_with(opener_with(body=body)), MODEL, BODY)


# ─────────────────────────────────────────────────────────────
#  진짜 opener — 리다이렉트를 따르지 않고, 시험에서는 소켓에서 막힌다
# ─────────────────────────────────────────────────────────────


def test_the_default_opener_never_follows_a_redirect() -> None:
    director = gemini_messages.build_opener()
    redirecting = RedirectingHTTPS()
    director.add_handler(redirecting)
    client = client_with(lambda request, timeout: director.open(request, timeout=timeout))
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client, MODEL, BODY)
    assert info.value.status_code == 302
    assert redirecting.urls == [URL]


def test_the_default_opener_is_blocked_by_the_test_socket_guard() -> None:
    client = GeminiClient(
        api_key=SECRET,
        base_url="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        timeout_seconds=5.0,
    )
    with pytest.raises(RuntimeError) as info:  # conftest의 NetworkBlocked — 변환되지 않는다
        post_generate_content(client, MODEL, BODY)
    assert type(info.value).__name__ == "NetworkBlocked"


# ─────────────────────────────────────────────────────────────
#  https만 — 키는 평문·다른 스킴으로 나가지 않는다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base_url",
    [
        "http://generativelanguage.googleapis.com",
        "ftp://generativelanguage.googleapis.com",
        "file:///etc/passwd",
        "https://",
        "generativelanguage.googleapis.com",
        "",
    ],
)
def test_a_non_https_base_url_cannot_make_a_client(base_url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        client_with(opener_with(), base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://generativelanguage.googleapis.com?x=",
        "https://generativelanguage.googleapis.com?",
        "https://generativelanguage.googleapis.com#",
        "https://generativelanguage.googleapis.com#frag",
        "https://user:pass@generativelanguage.googleapis.com",
        "https://user@generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com/v1beta",
    ],
)
def test_a_base_url_with_userinfo_query_fragment_or_path_cannot_make_a_client(
    base_url: str,
) -> None:
    """`https://h?x=`는 연산을 쿼리로 밀고 `https://h#`는 경로를 지운다 — 호스트뿐이어야 한다."""
    with pytest.raises(ValueError, match="호스트뿐"):
        client_with(opener_with(), base_url)


def test_a_port_is_allowed() -> None:
    opener = opener_with()
    post_generate_content(
        client_with(opener, "https://generativelanguage.googleapis.com:443"), MODEL, BODY
    )
    assert opener.calls[0][0].full_url.startswith("https://generativelanguage.googleapis.com:443/")


def test_a_tampered_api_version_is_refused_before_the_key_is_attached() -> None:
    opener = opener_with()
    client = client_with(opener)
    object.__setattr__(client, "api_version", "v1beta/../../x")
    with pytest.raises(ValueError, match="버전"):
        post_generate_content(client, MODEL, BODY)
    assert opener.calls == []


def test_server_status_and_reason_are_echoed_only_as_upper_case_tokens() -> None:
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "합성",
                "status": "x" * 500 + " <script>",
                "details": [
                    {"reason": "API_KEY_INVALID"},  # ErrorInfo가 아니다 — 읽지 않는다
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "a b"},
                ],
            }
        }
    ).encode()
    with pytest.raises(GeminiAPIStatusError) as info:
        post_generate_content(client_with(opener_with(status=400, body=body)), MODEL, BODY)
    assert (info.value.error_type, info.value.reason) == ("unparsed_error_body", "")
    assert "<script>" not in str(info.value)
    assert provider_error(info.value)[0] == "request_format"


@pytest.mark.parametrize("version", ["", "v1beta/../v1", "v1beta?x", "V1"])
def test_an_unsafe_api_version_cannot_make_a_client(version: str) -> None:
    with pytest.raises(ValueError, match="버전"):
        GeminiClient(
            api_key=SECRET,
            base_url="https://generativelanguage.googleapis.com",
            api_version=version,
            timeout_seconds=1.0,
        )


def test_a_tampered_base_url_is_refused_before_the_key_is_attached() -> None:
    opener = opener_with()
    client = client_with(opener)
    object.__setattr__(client, "base_url", "http://generativelanguage.googleapis.com")
    with pytest.raises(ValueError, match="https"):
        post_generate_content(client, MODEL, BODY)
    assert opener.calls == []


def test_the_default_opener_speaks_https_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _proxy_env(
        monkeypatch, http_proxy="http://proxy.test:3128", https_proxy="http://proxy.test:3128"
    )
    director = gemini_messages.build_opener()
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
    for url in ("http://generativelanguage.googleapis.com/", "file:///etc/passwd", "data:,x"):
        with pytest.raises(urllib.error.URLError, match="unknown url type"):
            director.open(urllib.request.Request(url), timeout=1)


class _JsonSpy(TunnelSpy):
    """Claude 시험의 간첩과 같다 — 본문만 JSON 객체(`{}`)로 돌려준다."""

    def https_open(self, request: urllib.request.Request) -> Any:
        tunnel = getattr(request, "_tunnel_host", None)
        self.seen.append((request.type, request.host, tunnel, request.full_url))
        response = urllib.response.addinfourl(io.BytesIO(b"{}"), Message(), request.full_url, 200)
        response.msg = "OK"  # type: ignore[attr-defined]
        return response


def test_https_through_a_configured_proxy_is_a_connect_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch, https_proxy="http://proxy.test:3128")
    director = gemini_messages.build_opener()
    spy = _JsonSpy()
    director.add_handler(spy)
    post_generate_content(
        client_with(lambda request, timeout: director.open(request, timeout=timeout)), MODEL, BODY
    )
    assert spy.seen == [("https", "proxy.test:3128", "generativelanguage.googleapis.com", URL)]


# ─────────────────────────────────────────────────────────────
#  Interactions — Gemini 3.5 Transcribe의 전송(W-31a, D92)
# ─────────────────────────────────────────────────────────────

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
TRANSCRIBE_BODY = {
    "model": "gemini-3.5-transcribe",
    "store": False,
    "input": [{"type": "audio", "data": "UklGRg==", "mime_type": "audio/wav"}],
}


def test_an_interaction_posts_to_the_interactions_path_with_the_key_header_only() -> None:
    opener = opener_with(body=b'{"status": "completed", "output_text": ""}')
    reply = gemini_messages.post_interaction(client_with(opener), TRANSCRIBE_BODY)
    ((request, timeout),) = opener.calls
    assert request.full_url == INTERACTIONS_URL  # 키는 URL에 없다
    assert request.get_method() == "POST"
    assert dict(request.header_items()) == {
        "X-goog-api-key": SECRET,
        "Content-type": "application/json",
    }
    assert json.loads(request.data) == TRANSCRIBE_BODY  # 모델은 경로가 아니라 본문에 산다
    assert timeout == 300.0 and reply["status"] == "completed"


@pytest.mark.parametrize("model", [None, "", "gemini-3.5-transcribe/../x", "G", 3])
def test_an_interaction_model_that_is_not_a_segment_is_refused_before_sending(
    model: object,
) -> None:
    opener = opener_with()
    body = {key: value for key, value in TRANSCRIBE_BODY.items() if key != "model"}
    if model is not None:
        body["model"] = model
    with pytest.raises(ValueError, match="모델"):
        gemini_messages.post_interaction(client_with(opener), body)
    assert opener.calls == []


def test_an_interaction_error_uses_the_same_bounded_status_parsing() -> None:
    body = json.dumps(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": f"monthly spending cap {SECRET}",
            }
        }
    ).encode()
    opener = opener_with(status=429, body=body)
    with pytest.raises(GeminiAPIStatusError) as caught:
        gemini_messages.post_interaction(client_with(opener), TRANSCRIBE_BODY)
    assert caught.value.status_code == 429 and caught.value.error_type == "RESOURCE_EXHAUSTED"
    assert SECRET not in str(caught.value)


def test_a_tampered_client_is_refused_before_an_interaction_carries_the_key() -> None:
    opener = opener_with()
    client = client_with(opener)
    object.__setattr__(client, "base_url", "http://generativelanguage.googleapis.com")
    with pytest.raises(ValueError, match="https"):
        gemini_messages.post_interaction(client, TRANSCRIBE_BODY)
    assert opener.calls == []
