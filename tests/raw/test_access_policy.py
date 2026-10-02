"""순수 접근 정책 — Range·Host·Origin/Referer 판정(계획 §4.3·§4.4).

HTTP 계층이나 gradio 없이 서비스의 순수 함수만으로 도는 검사다 — 웹 UI extra가
없는 기본 환경에서도 핵심 raw 계약 검사는 그대로 돈다(코디네이터 재결).
행동(요청·응답) 검증은 `test_media_access.py`가 맡는다.
"""

from __future__ import annotations

import pytest

from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import ByteRange, RangeNotSatisfiableError

PORT = 7860


# ── 정책 함수 — 순수하게, HTTP 없이 ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 100, None),
        ("bytes=0-9", 100, ByteRange(0, 9)),
        ("bytes=90-", 100, ByteRange(90, 99)),
        ("bytes=-5", 100, ByteRange(95, 99)),
        ("bytes=-200", 100, ByteRange(0, 99)),  # 접미가 파일보다 길면 전체
        ("bytes=0-1000", 100, ByteRange(0, 99)),  # 끝이 넘으면 자른다(RFC 허용)
        ("chunks=0-9", 100, None),  # 알 수 없는 단위는 무시
        ("bytes=0-1,5-9", 100, None),  # 여러 구간은 우리 계약에 없다 — 전체로
        ("bytes=a-b", 100, None),  # 모양이 어긋나면 무시 — bytes=-0은 아래 416 묶음이다
    ],
)
def test_parse_range_policy(header: str | None, size: int, expected: ByteRange | None) -> None:
    assert raw.parse_range(header, size) == expected


@pytest.mark.parametrize(
    ("header", "size"),
    [
        ("bytes=100-200", 100),  # 시작이 크기를 벗어났다
        ("bytes=50-40", 100),  # 끝이 시작보다 앞이다
        ("bytes=-0", 100),  # 0길이 접미는 만족할 수 없다
    ],
)
def test_parse_range_rejects_the_unsatisfiable(header: str, size: int) -> None:
    with pytest.raises(RangeNotSatisfiableError):
        raw.parse_range(header, size)


def test_host_policy_is_loopback_fixed() -> None:
    """Host는 loopback과 포트까지 일치해야 한다 — DNS 재바인딩은 여기서 끊긴다."""
    assert raw.host_header_allowed("127.0.0.1", PORT)
    assert raw.host_header_allowed("127.0.0.1:7860", PORT)
    assert raw.host_header_allowed("localhost:7860", PORT)
    assert raw.host_header_allowed("LOCALHOST:7860", PORT)
    assert not raw.host_header_allowed("evil.example.com", PORT)
    assert not raw.host_header_allowed("127.0.0.1:9999", PORT)
    assert not raw.host_header_allowed("", PORT)
    assert not raw.host_header_allowed(None, PORT)


def test_mutation_policy_trusts_only_our_origin() -> None:
    """Origin이 오면 엄격히, 안 오면 Referer로, 둘 다 없으면 로컬 도구로 본다."""
    assert raw.mutation_request_allowed(f"http://127.0.0.1:{PORT}", None, PORT)
    assert raw.mutation_request_allowed(f"http://localhost:{PORT}", None, PORT)
    assert not raw.mutation_request_allowed("http://evil.example.com", None, PORT)
    assert not raw.mutation_request_allowed("null", None, PORT)  # 파일·샌드박스 출처
    assert raw.mutation_request_allowed(None, f"http://127.0.0.1:{PORT}/탭/1", PORT)
    assert not raw.mutation_request_allowed(None, "http://evil.example.com/x", PORT)
    assert not raw.mutation_request_allowed(None, None, PORT)  # 머리글 없음은 거부
