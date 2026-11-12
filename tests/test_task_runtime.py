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


def _quota_error(quota_id: str, quota_value: str, delay: float | None) -> RuntimeError:
    error = _status_error(429)
    error.quota_id = quota_id  # type: ignore[attr-defined]
    error.quota_value = quota_value  # type: ignore[attr-defined]
    error.retry_delay_seconds = delay  # type: ignore[attr-defined]
    return error


def test_a_429_without_quota_detail_keeps_the_old_words() -> None:
    assert provider_error(_status_error(429))[1] == "API 사용량 또는 요청 한도를 확인하세요"


def test_a_gemini_429_that_names_no_quota_says_so() -> None:
    message = provider_error(_quota_error("", "", None))[1]
    assert message == (
        "API 요청 한도에 걸렸습니다 · 공급자가 어느 한도인지 밝히지 않았습니다(일시적인 처리 용량"
        " 부족일 수 있습니다) · 잠시 뒤 실패 구간을 다시 시도하고, 계속되면 태평양 시간 자정"
        "(한국 오후 4–5시) 뒤에 다시 시도하세요"
    )


def test_an_unnamed_429_with_a_suggested_wait_uses_it() -> None:
    message = provider_error(_quota_error("", "", 7.5))[1]
    assert "8초쯤 뒤 실패 구간을 다시 시도하고" in message


def test_a_daily_quota_points_at_the_pacific_midnight_reset() -> None:
    error = _quota_error("GenerateRequestsPerDayPerProjectPerModel", "100", 43.0)
    message = provider_error(error)[1]
    assert message.startswith("API 하루 요청 한도에 걸렸습니다(한도 100)")
    assert "태평양 시간 자정" in message
    assert "43초" not in message  # 하루 한도에 몇십 초 대기는 오해를 부른다


def test_a_per_minute_quota_rounds_the_suggested_wait_up() -> None:
    error = _quota_error("GenerateRequestsPerMinutePerProjectPerModel", "", 0.2)
    message = provider_error(error)[1]
    assert message.startswith("API 분당 요청 한도에 걸렸습니다 ·")
    assert "1초" in message and "(한도" not in message


def test_an_unknown_quota_keeps_the_old_words_and_names_the_quota() -> None:
    message = provider_error(_quota_error("ProjectSpendCap", "", None))[1]
    assert message == "API 사용량 또는 요청 한도를 확인하세요 (ProjectSpendCap)"
