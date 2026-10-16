#!/usr/bin/env python3
"""작업대 그리기·타이머 틱의 벽시계 시간과 부른 수를 잰다(W-23, D63) — 유료 호출 0.

    python scripts/bench_workbench.py                      # 합성 작업공간(원본 18개)을 만들어 잰다
    python scripts/bench_workbench.py --workspace <사본>   # 실제 작업공간의 **사본**을 잰다
    python scripts/bench_workbench.py --profile            # cProfile 상위 30줄을 함께
    python scripts/bench_workbench.py --golden <파일>      # 합성 작업공간의 정규화 그리기를 쓴다

잰 것은 넷이다: 작업대 그리기 ×5, 아무것도 바뀌지 않은 `capcut_tick_view` ×5, 작업 진행 수만 바뀐
틱 ×5, 다른 스레드가 쉬지 않고 쓰는 동안의 그리기 ×5(분석 작업이 도는 때). 화면 상태 파일은 임시
폴더에 둔다(앱의 `.vlogsrt-ui-state.json`을 건드리지 않는다). `--workspace`는 사본만 넘긴다 — 열기가
DB를 WAL로 바꾸고, 진행 틱이 작업 기록 한 줄을, 쓰기 단계가 meta 열쇠 하나를 쓰고 지운다. 정리
고리는 할 일을 **읽기만** 하고 작업을 걸지 않는다.
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VLOGSRT_OFFLINE", "1")

from tests.raw import render_cost_fixture as fixture  # noqa: E402
from tests.raw.test_proposal_queue import _with_tiers  # noqa: E402
from tests.raw.test_review_workspace import _wait_terminal  # noqa: E402
from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt import log  # noqa: E402
from vlogsrt.project.raw import sync_report  # noqa: E402
from vlogsrt.project.raw.service import RawWorkspaceService  # noqa: E402

REPEATS = 5
WRITE_BYTES = 256 * 1024
"""쓰기 단계의 한 트랜잭션 크기 — 검토 리비전 문서(JSON) 한 벌쯤이다."""
WRITE_KEY = "bench_workbench_write"


class _Writer(threading.Thread):
    """쉬지 않고 쓰는 연결 하나 — 세는 `open_db`를 거치지 않는다(부른 수에 섞이지 않게)."""

    def __init__(self, database: Path) -> None:
        super().__init__(daemon=True)
        self._database = database
        self.stop = threading.Event()
        self.commits = 0

    def run(self) -> None:
        connection = sqlite3.connect(self._database, timeout=10.0, isolation_level=None)
        try:
            while not self.stop.is_set():
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                    " SET value = excluded.value",
                    (WRITE_KEY, f"{self.commits:08d}" + "x" * WRITE_BYTES),
                )
                connection.execute("COMMIT")
                self.commits += 1
            connection.execute("DELETE FROM meta WHERE key = ?", (WRITE_KEY,))
        finally:
            connection.close()


class _Patcher:
    """monkeypatch.setattr처럼 갈아 끼우고, 끝나면 거꾸로 되돌린다."""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def __call__(self, owner: object, attribute: str, value: object) -> None:
        self._undo.append((owner, attribute, getattr(owner, attribute)))
        setattr(owner, attribute, value)

    def restore(self) -> None:
        for owner, attribute, value in reversed(self._undo):
            setattr(owner, attribute, value)
        self._undo.clear()


def _read_only_upkeep(patch: _Patcher) -> None:
    """정리 고리 둘이 할 일을 재기만 하고 작업을 걸지 않는다 — 사본도 바꾸지 않는다."""

    def start_upkeep(self: RawWorkspaceService, root: Path) -> str | None:
        self._upkeep_pending(root)
        return None

    def start_sync_upkeep(self: RawWorkspaceService, root: Path) -> str | None:
        sync_report.pending_sources(self._workspace(root))
        return None

    patch(RawWorkspaceService, "start_upkeep", start_upkeep)
    patch(RawWorkspaceService, "start_sync_upkeep", start_sync_upkeep)


def _open(root: Path, state_dir: Path, *, synthetic: bool) -> tuple[RawWorkspaceUi, str]:
    server = RawWorkspaceUi(state_path=state_dir / "ui-state.json")
    service = server.raw
    before = {job.job_id for job in service.list_jobs(root, limit=1000)} if not synthetic else set()
    server.open_workspace_view(str(root))
    if synthetic:
        _with_tiers(server)  # 선택 분석 기록 대신 지금 분석 리비전을 영상마다(시험과 같다)
        fixture.settle(service, root)
    else:
        # 사본의 「돌던」 기록은 이 프로세스가 돌리지 않는다 — 열기가 새로 건 작업만 기다린다.
        for job in service.list_jobs(root, limit=1000):
            if job.job_id not in before:
                _wait_terminal(service, root, job.job_id)
    return server, server.current_signature() or ""


def _measure(
    name: str, counts: Counter[str], call: Callable[[int], Any], before: Callable[[int], Any]
) -> dict[str, Any]:
    """`before(i)` 뒤에 `call(i)`만 잰다 — 부른 수도 `call` 동안만 센다."""
    walls: list[float] = []
    calls: Counter[str] = Counter()
    for index in range(REPEATS):
        before(index)
        snapshot = Counter(counts)
        start = time.perf_counter()
        call(index)
        walls.append((time.perf_counter() - start) * 1000)
        calls.update(counts - snapshot)
    return {"name": name, "walls": walls, "calls": calls}


def _report(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        walls = row["walls"]
        log.out(
            f"{row['name']}: 중앙 {statistics.median(walls):.1f} ms · 최소 {min(walls):.1f}"
            f" · 최대 {max(walls):.1f} (×{len(walls)})"
        )
        per_call = {name: row["calls"][name] / len(walls) for name in fixture.COUNTED_NAMES}
        log.out("  한 번에 " + " · ".join(f"{k} {v:g}" for k, v in per_call.items()))


def _phases(server: RawWorkspaceUi, root: Path, signature: str, counts: Counter[str]) -> list:
    nothing: Callable[[int], None] = lambda _index: None  # noqa: E731
    rows = [
        _measure("작업대 그리기", counts, lambda _i: server.visual_workbench_render(root), nothing)
    ]
    tab = fixture.TickTab(server, signature)  # 탭 하나 — 마지막으로 그린 열쇠를 쥔다(D64)
    tab.tick()  # 열쇠를 채운다 — 아래 틱은 바뀐 것이 없다
    rows.append(_measure("바뀐 것 없는 틱", counts, lambda _i: tab.tick(), nothing))
    stepped = fixture.SteppedJob(server.raw, root)
    stepped.step(1)
    tab.tick()  # 작업이 막 돌기 시작한 틱(상태가 바뀌었다)은 빼고 잰다
    rows.append(
        _measure(
            "진행만 바뀐 틱",
            counts,
            lambda _i: tab.tick(),
            lambda index: stepped.step(index + 2),
        )
    )
    stepped.finish()
    writer = _Writer(root / "project.sqlite3")
    writer.start()
    try:
        rows.append(
            _measure(
                "쓰기와 겹친 그리기",
                counts,
                lambda _i: server.visual_workbench_render(root),
                nothing,
            )
        )
    finally:
        writer.stop.set()
        writer.join()
    log.out(f"쓰기 단계 동안 커밋 {writer.commits}번")
    return rows


@contextmanager
def _profiled(enabled: bool) -> Iterator[None]:
    profile = cProfile.Profile() if enabled else None
    if profile is not None:
        profile.enable()
    try:
        yield
    finally:
        if profile is not None:
            profile.disable()
            pstats.Stats(profile, stream=sys.stderr).sort_stats("cumulative").print_stats(30)


def _write_golden(server: RawWorkspaceUi, root: Path, base: Path, target: Path) -> None:
    shell, body = server.visual_workbench_render(root)
    text = fixture.normalize(shell + "\n" + body, base)
    again = fixture.normalize("\n".join(server.visual_workbench_render(root)), base)
    if again != text:
        raise SystemExit("두 번 그린 값이 다르다 — 잠글 수 없다")
    target.write_text(text, encoding="utf-8")
    log.out(f"정규화 그리기를 썼습니다: {target} ({len(text)}자)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, help="실제 작업공간의 **사본** 경로")
    parser.add_argument("--profile", action="store_true", help="cProfile 상위 30줄")
    parser.add_argument("--golden", type=Path, help="합성 작업공간의 정규화 그리기를 쓸 파일")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bench-workbench-") as scratch:
        base = Path(scratch)
        patch = _Patcher()
        try:
            if args.workspace is not None:
                _read_only_upkeep(patch)
                root = args.workspace.expanduser().resolve()
            else:
                started = time.perf_counter()
                root = fixture.build_workspace(base)
                log.out(f"합성 작업공간 {time.perf_counter() - started:.1f}초: {root}")
            server, signature = _open(root, base, synthetic=args.workspace is None)
            if args.golden is not None:
                _write_golden(server, root, base, args.golden)
                return 0
            counts = fixture.install_counters(patch)
            with _profiled(args.profile):
                rows = _phases(server, root, signature, counts)
            _report(rows)
        finally:
            patch.restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
