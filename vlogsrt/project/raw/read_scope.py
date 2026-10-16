"""그리기 한 번의 읽기 범위 — 같은 읽기를 한 번만 한다(W-23 세션 B, D63).

작업대 그리기 한 번이 같은 원본 자막을 540번, 같은 분석 개요를 19번 읽었다. 범위 안에서는 읽기
함수가 같은 인자의 답을 기억했다가 돌려준다. 범위는 화면 진입점(`ui/workspace_service.py`의
그리기·타이머 틱·작업대 동작)이 **쓰기가 끝난 뒤에만** 연다 — 쓰기를 넘어 기억하면 방금 쓴 것을
못 본다(`check_invariants`가 여는 자리를 지킨다).

범위는 그리기 한 번만 산다: 요청을 넘지 않고, 디스크에 남지 않고, 스레드·컨텍스트를 건너지 않는다
(`contextvars` — 작업 스레드는 범위 없이 돈다). 범위가 없으면 모든 읽기는 예전과 똑같이 매번 읽는다.
3단 등급도 여전히 읽을 때마다 다시 계산하고 어디에도 보관하지 않는다(D51·D52) — 기억은 그리기
한 번 안의 같은 값일 뿐이다.

기억한 값은 부른 쪽이 바꾸지 않는 값(얼린 데이터클래스·튜플·읽기만 하는 문서)이어야 한다 — 바꾸는
쪽에 돌려줄 값은 부를 때마다 복사한다.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Hashable, Iterator
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")


class ReadScope:
    """열쇠 → 답. 열쇠에는 읽기 함수의 이름과 인자 전부가 들어간다."""

    def __init__(self) -> None:
        self._answers: dict[Hashable, Any] = {}

    def memo(self, key: Hashable, compute: Callable[[], T]) -> T:
        """기억한 답, 없으면 계산해 기억한다 — 계산이 예외를 올리면 기억하지 않는다."""
        if key in self._answers:
            return self._answers[key]
        answer = compute()
        self._answers[key] = answer
        return answer

    def __len__(self) -> int:
        return len(self._answers)


_CURRENT: ContextVar[ReadScope | None] = ContextVar("vlogsrt_read_scope", default=None)


@contextlib.contextmanager
def open() -> Iterator[ReadScope]:
    """읽기 범위를 연다 — 이미 열려 있으면 바깥 범위를 그대로 쓴다(겹쳐 열어도 한 범위다).

    내장 `open`을 가리는 이름이다 — 언제나 `read_scope.open()`으로만 부른다(검사가 그 자리를 본다).
    """
    outer = _CURRENT.get()
    if outer is not None:
        yield outer
        return
    scope = ReadScope()
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def current() -> ReadScope | None:
    """지금 컨텍스트의 읽기 범위 — 없으면 None(그때는 매번 읽는다)."""
    return _CURRENT.get()


def memo(key: Hashable, compute: Callable[[], T]) -> T:
    """범위가 열려 있으면 기억한 답, 아니면 그냥 계산한다(예전 그대로)."""
    scope = _CURRENT.get()
    return compute() if scope is None else scope.memo(key, compute)
