"""그리기 한 번의 읽기 범위(W-23 세션 B, D63) — 범위 밖에서는 예전 그대로, 안에서는 한 번만 읽는다.

못 박는 것:

1. 범위는 열린 동안만 기억한다 — 닫으면 새 범위는 다시 읽는다. 겹쳐 열면 바깥 범위 하나다.
   다른 스레드·새 컨텍스트는 범위를 보지 않는다. 예외는 기억하지 않는다.
2. 원본 자막 읽기는 범위 안에서 원본마다 한 번이다. 쓰기는 기억한 값을 쓰지 않는다(늘 새로 읽는다) —
   범위가 열린 채 쓰여도 기억한 문서를 바꾸지 않는다.
3. 다른 원본과 같은 자막 후보(`cross_source_candidates`)는 범위가 있든 없든 바이트까지 같다 —
   원본 사이 중복·한 원본 안 중복·빈 자막·자막 없는 원본·짧은 글이 섞여도.
4. 화면 진입점은 쓰기(정리 고리·동작) **뒤에** 범위를 연다 — 쓰는 동안에는 범위가 없다.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import sqlite3
import threading
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt.project.raw import read_scope
from vlogsrt.project.raw import source_transcript as captions
from vlogsrt.project.raw.models import RawWorkspaceError

# ── 1. 범위의 수명 ─────────────────────────────────────────────────────────────


def _counter() -> tuple[list[int], Any]:
    calls: list[int] = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    return calls, compute


def test_without_a_scope_every_read_is_fresh() -> None:
    calls, compute = _counter()
    assert read_scope.current() is None
    assert [read_scope.memo(("k",), compute) for _ in range(3)] == [1, 2, 3]
    assert len(calls) == 3


def test_inside_a_scope_the_same_key_is_read_once_and_forgotten_on_exit() -> None:
    calls, compute = _counter()
    with read_scope.open() as scope:
        assert read_scope.current() is scope
        assert [read_scope.memo(("k", 1), compute) for _ in range(3)] == [1, 1, 1]
        assert read_scope.memo(("k", 2), compute) == 2  # 인자가 다르면 다른 열쇠
    assert read_scope.current() is None
    assert read_scope.memo(("k", 1), compute) == 3  # 닫힌 범위의 답은 남지 않는다
    with read_scope.open():
        assert read_scope.memo(("k", 1), compute) == 4  # 새 범위는 다시 읽는다
    assert len(calls) == 4


def test_a_nested_open_reuses_the_outer_scope() -> None:
    calls, compute = _counter()
    with read_scope.open() as outer:
        read_scope.memo(("k",), compute)
        with read_scope.open() as inner:
            assert inner is outer
            assert read_scope.memo(("k",), compute) == 1
        assert read_scope.current() is outer  # 안쪽을 닫아도 바깥은 그대로다
    assert read_scope.current() is None
    assert len(calls) == 1


def test_an_exception_is_not_remembered() -> None:
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("처음 한 번은 못 읽는다")
        return "읽었다"

    with read_scope.open():
        with pytest.raises(OSError):
            read_scope.memo(("flaky",), flaky)
        assert read_scope.memo(("flaky",), flaky) == "읽었다"
    assert len(attempts) == 2


def test_other_threads_and_fresh_contexts_do_not_see_the_scope() -> None:
    seen: list[object] = []
    with read_scope.open() as scope:
        worker = threading.Thread(target=lambda: seen.append(read_scope.current()))
        worker.start()
        worker.join()
        seen.append(contextvars.Context().run(read_scope.current))
        assert read_scope.current() is scope
    assert seen == [None, None]


def test_two_threads_with_their_own_scopes_do_not_share_answers() -> None:
    barrier = threading.Barrier(2)
    answers: dict[str, object] = {}

    def render(name: str) -> None:
        with read_scope.open():
            read_scope.memo(("who",), lambda: name)
            barrier.wait()  # 둘 다 범위를 연 채 — 서로의 답을 보면 안 된다
            answers[name] = read_scope.memo(("who",), lambda: "다른 값")

    threads = [threading.Thread(target=render, args=(name,)) for name in ("가", "나")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert answers == {"가": "가", "나": "나"}


# ── 2·3. 원본 자막 — 가짜 작업공간(DB 파일 하나와 원본 목록) ────────────────────


class _Workspace:
    """`source_transcript`가 쓰는 만큼만 — 루트·연결·원본 목록. 연 횟수를 센다."""

    def __init__(self, root: Path, assets: list[tuple[str, str]]) -> None:
        self.root = root
        self.opened = 0
        self._assets = tuple(SimpleNamespace(asset_id=a, source_name=n) for a, n in assets)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.opened += 1
        connection = sqlite3.connect(self.root / "db.sqlite3")
        try:
            yield connection
        finally:
            connection.close()

    def list_source_assets(self) -> tuple[SimpleNamespace, ...]:
        return self._assets


def _cue(cue_id: str, text: str, start: int) -> dict[str, Any]:
    return {
        "utterance_id": cue_id,
        "text": text,
        "start": {"num": start, "den": 1},
        "end": {"num": start + 1, "den": 1},
    }


LONG = "오늘은 계곡에서 수영을 했다"  # 12자 이상 — 후보가 될 수 있는 글
OTHER = "저녁에는 바비큐를 먹었어요 진짜"


def _varied(tmp_path: Path) -> _Workspace:
    """원본 다섯 — 원본 사이 중복·한 원본 안 중복·공백만 다른 중복·짧은 글·빈 자막·자막 없음."""
    workspace = _Workspace(
        tmp_path,
        [("a", "A.mov"), ("b", "B.mov"), ("c", "C.mov"), ("d", "D.mov"), ("e", "E.mov")],
    )
    rows = {
        "a": [_cue("a1", LONG, 0), _cue("a2", "짧은 말", 2), _cue("a3", OTHER, 4)],
        "b": [_cue("b1", OTHER, 0), _cue("b2", "  오늘은  계곡에서 수영을   했다 ", 2)],
        "c": [_cue("c1", LONG, 0), _cue("c2", LONG, 2), _cue("c3", "짧은 말", 4)],
        "d": [],
    }
    for asset_id, utterances in rows.items():
        captions.seed(workspace, asset_id, utterances, Fraction(30), f"sha-{asset_id}")
    return workspace


def test_cross_source_candidates_are_byte_equal_with_and_without_a_scope(tmp_path: Path) -> None:
    workspace = _varied(tmp_path)
    ids = ("a", "b", "c", "d", "e")
    plain = {asset_id: captions.cross_source_candidates(workspace, asset_id) for asset_id in ids}
    with read_scope.open():
        scoped = {
            asset_id: captions.cross_source_candidates(workspace, asset_id) for asset_id in ids
        }
    assert json.dumps(scoped, ensure_ascii=False) == json.dumps(plain, ensure_ascii=False)
    assert [row["other_utterance"] for row in plain["a"]] == ["b1", "b2", "c1", "c2"]
    assert plain["d"] == plain["e"] == []  # 빈 자막·자막 없는 원본은 후보가 없다
    assert [row["utterance_id"] for row in plain["c"]] == ["c2", "c2"]  # 같은 글은 마지막 줄
    assert [row["other_utterance"] for row in plain["c"]] == ["a1", "b2"]


def test_inside_a_scope_each_transcript_is_read_once(tmp_path: Path) -> None:
    workspace = _varied(tmp_path)
    before = workspace.opened
    with read_scope.open():
        for asset_id in ("a", "b", "c", "d", "e"):
            captions.cross_source_candidates(workspace, asset_id)
            assert captions.load(workspace, asset_id) == captions.load(workspace, asset_id)
    assert workspace.opened - before <= 2  # 표 준비 한 번 + 지금 리비전 전부 한 번
    with read_scope.open():
        assert captions.load(workspace, "a") is captions.load(workspace, "a")
        assert captions.load(workspace, "e") is None


def test_a_write_inside_a_scope_reads_fresh_and_leaves_the_remembered_document(
    tmp_path: Path,
) -> None:
    workspace = _varied(tmp_path)
    with read_scope.open():
        remembered = captions.load(workspace, "a")
        assert remembered is not None
        frozen = json.dumps(remembered, ensure_ascii=False, sort_keys=True)
        edited = captions.edit(
            workspace, "a", remembered["revision"], action="edit", utterance_id="a1", text="새 글"
        )
        # 두 번째 쓰기는 기억한 옛 리비전이 아니라 방금 쓴 리비전을 본다 — 낡은 쪽은 거절된다.
        with pytest.raises(RawWorkspaceError):
            captions.edit(
                workspace, "a", remembered["revision"], action="edit", utterance_id="a1", text="x"
            )
        captions.edit(
            workspace, "a", edited["revision"], action="edit", utterance_id="a2", text="y"
        )
        assert json.dumps(remembered, ensure_ascii=False, sort_keys=True) == frozen
    fresh = captions.load(workspace, "a")
    assert fresh is not None
    assert [row["text"] for row in fresh["utterances"]][:2] == ["새 글", "y"]
