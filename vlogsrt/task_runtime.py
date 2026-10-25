"""Thread-local cancellation shared by provider and subprocess boundaries."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_cancel: ContextVar[Callable[[], None] | None] = ContextVar("task_cancel", default=None)


def check_cancel() -> None:
    check = _cancel.get()
    if check is not None:
        check()


@contextmanager
def cancellation_scope(check: Callable[[], None]) -> Iterator[None]:
    token = _cancel.set(check)
    try:
        check_cancel()
        yield
    finally:
        _cancel.reset(token)


def cancellation_active() -> bool:
    return _cancel.get() is not None


AUTH_REASONS = frozenset({"API_KEY_INVALID"})
"""인증 실패로 읽는 공급자 오류 까닭(`reason`) — 상태 코드가 401이 아닌 공급자의 것이다."""


def provider_error(error: Exception) -> tuple[str, str, bool]:
    """Sanitized category and uncertainty, without echoing provider bodies or keys."""
    status = getattr(error, "status_code", None)
    kind = type(error).__name__.lower()
    # 529·402는 Anthropic이 쓰는 코드다(D54). 과부하는 처리 전 거절이라 청구 불명확이 아니다.
    if status == 529 or "overloaded" in kind:
        return "overloaded", "API 과부하 · 잠시 뒤 실패 구간을 다시 시도하세요", False
    if status == 402 or "billing" in kind:
        return "billing", "API 결제·크레딧 문제 · 계정 결제 상태를 확인하세요", False
    # Google은 틀린 키에 401이 아니라 400 + `API_KEY_INVALID`를 돌려준다(W-30a, D83).
    reason = getattr(error, "reason", "")
    if (
        status == 401
        or "authentication" in kind
        or (isinstance(reason, str) and reason in AUTH_REASONS)
    ):
        return "authentication", "API 인증 실패 · 키 설정을 확인하세요", False
    if status in (403, 404) or "permission" in kind or "notfound" in kind:
        return (
            "model_access",
            "설정한 모델에 접근할 수 없습니다 · 모델은 변경하지 않았습니다",
            False,
        )
    if status in (400, 413, 422) or "badrequest" in kind:
        return "request_format", "API 요청 형식이 거부됐습니다", False
    if status == 429 or "ratelimit" in kind:
        return "rate_limit", "API 사용량 또는 요청 한도를 확인하세요", False
    if "timeout" in kind:
        return "timeout", "API 시간 초과 · 응답 수신 불명확, 재시도 시 추가 호출 가능", True
    if "connection" in kind or status in (408, 500, 502, 503, 504):
        return "connection", "API 연결 실패 · 응답 수신 불명확, 재시도 시 추가 호출 가능", True
    return (
        "execution",
        re.sub(r"sk-[A-Za-z0-9_-]+", "[숨김]", f"{type(error).__name__}: {error}"),
        False,
    )
