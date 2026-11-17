#!/usr/bin/env python3
"""작업대 그리기·타이머 틱의 벽시계 시간과 부른 수를 잰다(W-23, D63) — 유료 호출 0.

    python scripts/bench_workbench.py                      # 합성 작업공간(원본 18개)을 만들어 잰다
    python scripts/bench_workbench.py --workspace <사본>   # 실제 작업공간의 **사본**을 잰다
    python scripts/bench_workbench.py --profile            # cProfile 상위 30줄을 함께
    python scripts/bench_workbench.py --golden <파일>      # 합성 작업공간의 정규화 그리기를 쓴다

잰 것(각 ×5): 작업대 그리기, 단추 길(`_capcut_result` — 정리 고리 + 그리기), 영상 카드 누르기
(`quick_source_view`), 1초 상태 틱, 아무것도 바뀌지 않은 `capcut_tick_view`, 작업 진행 수만 바뀐
틱(작업대에 진행이 안 보이는 종류 · 썸네일을 만드는 종류), 다른 스레드가 쉬지 않고 쓰는 동안의
그리기(분석 작업이 도는 때), 원본 18개 조사(ffprobe), 「자동 제외 전체 복원」(리비전 150개
사슬). 그린 값의 바이트(껍데기 + 본문)도 적는다. 합성 작업공간은 실데이터 크기로 부풀린다 —
끝난 옛 작업 700줄 · 선택 분석 상태 100개(W-24 세션 G). 화면 상태 파일은 임시 폴더에 둔다(앱의
`.vlogsrt-ui-state.json`을 건드리지 않는다). `--workspace`는 사본만 넘긴다 — 열기가
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
from ui.workspace_service import STATUS_SENT_CELLS, RawWorkspaceUi  # noqa: E402
from vlogsrt import log, preprocess  # noqa: E402
from vlogsrt.project.raw import ingest, sync_report  # noqa: E402
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade  # noqa: E402
from vlogsrt.project.raw.service import VISUAL_PREPARE_JOB_KIND, RawWorkspaceService  # noqa: E402

REPEATS = 5
WRITE_BYTES = 256 * 1024
"""쓰기 단계의 한 트랜잭션 크기 — 검토 리비전 문서(JSON) 한 벌쯤이다."""
WRITE_KEY = "bench_workbench_write"
CHAIN_REVISIONS = 150
"""「자동 제외 전체 복원」이 걷는 리비전 사슬 — 실데이터 152개쯤이다."""


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
        log.out("  한 번에 " + " · ".join(f"{k} {v:g}" for k, v in per_call.items() if v))
    _report_processes()


def _report_processes() -> None:
    """측정 동안 끝난 외부 프로세스의 단계별 합계(D120).

    열기·카드 누르기가 건 미리보기·썸네일 작업의 몫이다.
    """
    totals = preprocess.process_step_totals()
    if not totals:
        return
    log.out("외부 프로세스(단계 · 도구): 수 · 합 · 평균 · 최대 초")
    for row in totals:
        log.out(
            f"  {row.step} · {row.tool}: {row.count} · {row.total_seconds:.1f}"
            f" · {row.total_seconds / row.count:.2f} · {row.max_seconds:.1f}"
        )


def _settled(server: RawWorkspaceUi, root: Path) -> Callable[[int], None]:
    """잴 동작 앞 — 앞 동작이 건 작업(미리보기 준비 등)이 끝날 때까지 기다린다(재지 않는다)."""

    def wait(_index: int) -> None:
        for job in server.raw.all_active_jobs(root):
            _wait_terminal(server.raw, root, job.job_id)

    return wait


def _actions(server: RawWorkspaceUi, root: Path, signature: str, counts: Counter[str]) -> list:
    """단추 길 · 영상 카드 누르기 · 1초 상태 틱 — 화면의 동작 셋."""
    settled = _settled(server, root)
    rows = [
        _measure(
            "단추 길(정리 고리 + 그리기)",
            counts,
            lambda _i: server._capcut_result(root, "", upkeep=True),
            settled,
        )
    ]
    sources = [asset.asset_id for asset in server.raw.list_source_assets(root)][:2]
    rows.append(
        _measure(
            "영상 카드 누르기",
            counts,
            lambda index: server.quick_source_view(signature, sources[index % 2]),
            settled,
        )
    )
    summary = server.raw.analysis_overview(root)
    if summary is not None and server._quick:
        server._quick["revision"] = summary.revision.revision_id  # 실사용 줄이 읽을 리비전
    sent = (None,) * len(STATUS_SENT_CELLS)  # 이 세션이 지난 틱에 보낸 값 — 칸마다 하나
    rows.append(
        _measure(
            "1초 상태 틱",
            counts,
            lambda _i: server.status_tick(signature, None, None, None, None, *sent),
            settled,
        )
    )
    return rows


def _progress_ticks(server: RawWorkspaceUi, root: Path, signature: str, counts: Counter[str]):
    """진행 수만 바뀐 틱 — 작업대에 진행이 안 보이는 종류와 썸네일을 만드는 종류."""
    rows = []
    for name, kind in (
        ("진행만 바뀐 틱(안 보이는 종류)", fixture.SteppedJob.KIND),
        ("진행만 바뀐 틱(썸네일 종류)", VISUAL_PREPARE_JOB_KIND),
    ):
        tab = fixture.TickTab(server, signature)
        tab.tick()
        stepped = fixture.SteppedJob(server.raw, root, kind)
        try:
            stepped.step(1)
            tab.tick()  # 작업이 막 돌기 시작한 틱(상태가 바뀌었다)은 빼고 잰다
            rows.append(
                _measure(
                    name, counts, lambda _i, t=tab: t.tick(), lambda i, j=stepped: j.step(i + 2)
                )
            )
        finally:
            stepped.finish()
    return rows


def _probes(card: Path, counts: Counter[str]) -> dict[str, Any]:
    """원본 18개를 조사한다 — 작업대 첫 Final Cut 판이 원본·프록시마다 하는 일이다."""
    files = sorted(card.glob("*.mov"))

    def probe_all(_index: int) -> None:
        for path in files:
            ingest.ffprobe_media_inspector(path)

    return _measure(f"원본 {len(files)}개 조사(ffprobe)", counts, probe_all, lambda _i: None)


def _restore(root: Path, counts: Counter[str]) -> dict[str, Any]:
    """「자동 제외 전체 복원」 — 사람 선택 리비전 150개 사슬을 걷는다(머리를 바꾸므로 맨 끝)."""
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    target = next(s for s in head.segments if not s.notes.startswith("자동 제외"))
    for index in range(CHAIN_REVISIONS):
        selection = "excluded" if index % 2 == 0 else "included"
        facade.set_selection(head.base_plan_id, target.segment_id, selection)
    return _measure(
        f"자동 제외 전체 복원(리비전 {CHAIN_REVISIONS}개)",
        counts,
        lambda _i: facade.restore_automatic(),
        lambda _i: None,
    )


def _phases(server: RawWorkspaceUi, root: Path, signature: str, counts: Counter[str]) -> list:
    nothing: Callable[[int], None] = lambda _index: None  # noqa: E731
    shell, body = server.visual_workbench_render(root)
    log.out(
        f"그린 값: 껍데기 {len(shell.encode())} + 본문 {len(body.encode())} 바이트"
        f" (합 {len((shell + body).encode())})"
    )
    rows = [
        _measure("작업대 그리기", counts, lambda _i: server.visual_workbench_render(root), nothing)
    ]
    rows.extend(_actions(server, root, signature, counts))
    tab = fixture.TickTab(server, signature)  # 탭 하나 — 마지막으로 그린 열쇠를 쥔다(D64)
    tab.tick()  # 열쇠를 채운다 — 아래 틱은 바뀐 것이 없다
    rows.append(_measure("바뀐 것 없는 틱", counts, lambda _i: tab.tick(), nothing))
    rows.extend(_progress_ticks(server, root, signature, counts))
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


def _offline_phases(root: Path, card: Path | None, counts: Counter[str]) -> list:
    """작업공간을 바꾸는 단계 — 조사(합성 카드가 있을 때)와 복원(머리를 바꾼다)은 맨 끝이다."""
    rows = [] if card is None else [_probes(card, counts)]
    rows.append(_restore(root, counts))
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
            synthetic = args.workspace is None
            if synthetic and args.golden is None:
                fixture.inflate(root)
            server, signature = _open(root, base, synthetic=synthetic)
            if args.golden is not None:
                _write_golden(server, root, base, args.golden)
                return 0
            counts = fixture.install_counters(patch)
            with _profiled(args.profile):
                rows = _phases(server, root, signature, counts)
                rows.extend(_offline_phases(root, base / "card" if synthetic else None, counts))
            _report(rows)
        finally:
            patch.restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
