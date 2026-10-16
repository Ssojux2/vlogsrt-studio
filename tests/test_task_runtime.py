"""공급자 오류 분류(`provider_error`) — Claude가 더한 상태 코드(D54).

529(과부하)·402(결제)·413(요청 크기)는 Anthropic이 쓰는 코드다. 분류는 값만 본다 —
본문·키를 문면에 되뇌지 않는다.
"""

from __future__ import annotations

import pytest

from vlogsrt.task_runtime import provider_error


class OverloadedError(Exception):
    """SDK식 클래스 이름 — 상태 코드 없이도 이름으로 과부하를 안다."""


def _status_error(status: int) -> RuntimeError:
    error = RuntimeError("sk-ant-synthetic-never-display")
    error.status_code = status  # type: ignore[attr-defined]
    return error


def test_529_is_overloaded_and_not_uncertain() -> None:
    code, message, uncertain = provider_error(_status_error(529))
    assert code == "overloaded"
    assert "과부하" in message and "다시 시도" in message
    assert uncertain is False  # 과부하는 처리 전 거절이다 — 청구 불명확이 아니다
    assert "sk-ant" not in message


def test_an_overloaded_class_name_is_overloaded() -> None:
    assert provider_error(OverloadedError("busy"))[0] == "overloaded"


def test_402_is_billing() -> None:
    code, message, uncertain = provider_error(_status_error(402))
    assert code == "billing"
    assert "결제" in message and uncertain is False
    assert "sk-ant" not in message


def test_413_is_a_request_format_rejection() -> None:
    code, _message, uncertain = provider_error(_status_error(413))
    assert (code, uncertain) == ("request_format", False)


@pytest.mark.parametrize(
    ("status", "category", "uncertain"),
    [(401, "authentication", False), (429, "rate_limit", False), (503, "connection", True)],
)
def test_existing_categories_are_unchanged(status: int, category: str, uncertain: bool) -> None:
    code, _message, flag = provider_error(_status_error(status))
    assert (code, flag) == (category, uncertain)
