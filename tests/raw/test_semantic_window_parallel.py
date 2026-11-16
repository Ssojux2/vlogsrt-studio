"""의미 분석 창 병렬 전송(W-82, D142) — 묶인 풀 · 창 차례 결과 · 앞에서부터 이어진 진행.

공급자의 동시 전송 수가 1이면 예전 직렬과 같다(같은 판정 바이트 · 같은 진행 알림 · 한 번에 하나).
1을 넘으면 영상 안에서 그만큼 함께 보내고, 그 수가 프로세스 전체 상한이다. 실제 호출은 없다 —
가짜 응답자가 들어온 창을 붙잡고 센다.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_understanding_luna import SETTINGS, accepted_body, make_evidence, respond
from vlogsrt.project.raw import request_log
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_config import (
    PROVIDER_CONCURRENCY,
    RawProviderConcurrency,
    validate_provider_concurrency,
)
from vlogsrt.project.raw.config import analysis_provider
from vlogsrt.project.raw.understanding_luna import (
    AnalysisBudget,
    BudgetExhausted,
    CountingReservationGate,
    SemanticAnnotation,
    WindowEvidence,
    annotate_windows,
)
from vlogsrt.task_runtime import cancellation_scope, check_cancel

MODEL = "luna-role"  # openai 공급자 — 동시 수는 주입한 설정으로 고른다
WAIT = 10.0  # 시험이 멈춰 버리지 않게 하는 상한일 뿐이다


def _windows(tmp_path: Path, count: int, name: str = "window") -> tuple[WindowEvidence, ...]:
    first = make_evidence(tmp_path)
    return tuple(
        replace(first, window=replace(first.window, ordinal=k, window_id=f"{name}_{k:06d}"))
        for k in range(count)
    )


class Holding:
    """창마다 문 하나 — 시험이 열어야 응답한다. 함께 들어와 있는 수를 센다."""

    def __init__(self, windows: Sequence[WindowEvidence], *, open_all: bool = False) -> None:
        self.bodies = {w.window.window_id: respond(accepted_body(w)) for w in windows}
        self.gates = {key: threading.Event() for key in self.bodies}
        self.entered: list[str] = []
        self.inside = 0
        self.most_inside = 0
        self.lock = threading.Lock()
        self.arrived = threading.Condition(self.lock)
        if open_all:
            for gate in self.gates.values():
                gate.set()

    def respond(self, *, window_id: str, payload: Any) -> Any:
        with self.arrived:
            self.entered.append(window_id)
            self.inside += 1
            self.most_inside = max(self.most_inside, self.inside)
            self.arrived.notify_all()
        try:
            assert self.gates[window_id].wait(WAIT), f"{window_id} 문이 열리지 않았다"
            return self.bodies[window_id]
        finally:
            with self.lock:
                self.inside -= 1

    def wait_inside(self, count: int) -> None:
        with self.arrived:
            assert self.arrived.wait_for(lambda: len(self.entered) >= count, WAIT)


def _run(
    windows: Sequence[WindowEvidence],
    responder: Any,
    width: int,
    progress: list[tuple[SemanticAnnotation, ...]] | None = None,
    settled: list[SemanticAnnotation] | None = None,
    *,
    checked: bool = False,
) -> tuple[SemanticAnnotation, ...]:
    """`checked`면 진행 알림이 작업대 `publish_progress`처럼 먼저 취소를 본다."""

    def notify(rows: Sequence[SemanticAnnotation]) -> None:
        if checked:
            check_cancel()
        assert progress is not None
        progress.append(tuple(rows))

    return annotate_windows(
        windows,
        responder,
        settings=SETTINGS,
        model=MODEL,
        origin="online_live",
        on_annotations=None if progress is None else notify,
        on_settled=None if settled is None else settled.append,
        concurrency=RawProviderConcurrency(openai=width),
    )


# ─────────────────────────────────────────────────────────────
#  설정 — 사용자가 고른 기본값과 값역
# ─────────────────────────────────────────────────────────────


def test_defaults_send_four_gemini_windows_and_one_for_the_others() -> None:
    assert PROVIDER_CONCURRENCY.limit(analysis_provider("gemini-3.8-flash")) == 4
    assert PROVIDER_CONCURRENCY.limit(analysis_provider("claude-sonnet-5-5")) == 1
    assert PROVIDER_CONCURRENCY.limit(analysis_provider("gpt-6-luna")) == 1


@pytest.mark.parametrize(
    "settings",
    [
        RawProviderConcurrency(google=0),
        RawProviderConcurrency(anthropic=True),
        RawProviderConcurrency(openai=1.5),  # type: ignore[arg-type]
        RawProviderConcurrency(slot_poll_seconds=0),
    ],
)
def test_bad_concurrency_values_are_refused(settings: RawProviderConcurrency) -> None:
    with pytest.raises(ValueError):
        validate_provider_concurrency(settings)


def test_an_unknown_provider_has_no_limit() -> None:
    with pytest.raises(ValueError):
        PROVIDER_CONCURRENCY.limit("mistral")


# ─────────────────────────────────────────────────────────────
#  1이면 예전 직렬 그대로
# ─────────────────────────────────────────────────────────────


def test_one_at_a_time_matches_the_serial_bytes_and_progress(tmp_path: Path) -> None:
    windows = _windows(tmp_path, 5)
    serial = Holding(windows, open_all=True)
    progress: list[tuple[SemanticAnnotation, ...]] = []
    annotations = _run(windows, serial, 1, progress)
    assert serial.most_inside == 1
    assert serial.entered == [w.window.window_id for w in windows]
    # 창마다 한 번, 그때까지의 접두 — 예전 직렬 루프의 알림과 같다
    assert progress == [annotations[: k + 1] for k in range(len(windows))]
    wide = _run(windows, Holding(windows, open_all=True), 4)
    assert wide == annotations  # 동시 수와 무관하게 같은 판정 바이트


# ─────────────────────────────────────────────────────────────
#  1을 넘으면 — 함께 보내되 차례는 창 순서
# ─────────────────────────────────────────────────────────────


def test_windows_go_out_together_up_to_the_limit(tmp_path: Path) -> None:
    windows = _windows(tmp_path, 7)
    holding = Holding(windows)
    done: list[tuple[SemanticAnnotation, ...]] = []
    runner = threading.Thread(target=lambda: done.append(_run(windows, holding, 3)))
    runner.start()
    holding.wait_inside(3)
    assert holding.entered == ["window_000000", "window_000001", "window_000002"]
    for gate in holding.gates.values():
        gate.set()
    runner.join(WAIT)
    assert holding.most_inside == 3
    assert [a.window_id for a in done[0]] == [w.window.window_id for w in windows]


def test_progress_reports_only_the_contiguous_prefix(tmp_path: Path) -> None:
    windows = _windows(tmp_path, 4)
    holding = Holding(windows)
    progress: list[tuple[SemanticAnnotation, ...]] = []
    done: list[tuple[SemanticAnnotation, ...]] = []
    runner = threading.Thread(target=lambda: done.append(_run(windows, holding, 4, progress)))
    runner.start()
    holding.wait_inside(4)
    for key in ("window_000003", "window_000001", "window_000002"):  # 뒤 창부터 끝난다
        holding.gates[key].set()
    assert progress == []  # 첫 창이 아직이면 아무것도 알리지 않는다
    holding.gates["window_000000"].set()
    runner.join(WAIT)
    final = done[0]
    assert progress and progress[-1] == final
    for rows in progress:
        assert rows == final[: len(rows)]
    assert [len(rows) for rows in progress] == sorted({len(rows) for rows in progress})


def test_videos_share_the_process_wide_limit(tmp_path: Path) -> None:
    """영상 둘이 함께 돌아도 그 공급자에는 상한만큼만 나간다."""
    left = _windows(tmp_path, 4, "left")
    right = _windows(tmp_path, 4, "right")
    holding = Holding((*left, *right))
    shared = threading.Lock()
    count = {"in": 0, "most": 0}
    real = holding.respond

    def counted(*, window_id: str, payload: Any) -> Any:
        with shared:
            count["in"] += 1
            count["most"] = max(count["most"], count["in"])
        try:
            return real(window_id=window_id, payload=payload)
        finally:
            with shared:
                count["in"] -= 1

    holding.respond = counted  # type: ignore[method-assign]
    threads = [threading.Thread(target=_run, args=(side, holding, 3)) for side in (left, right)]
    for thread in threads:
        thread.start()
    holding.wait_inside(3)
    with holding.arrived:  # 문이 닫힌 동안 넷째는 오지 않는다 — 영상마다 셋이면 여섯이 왔을 것이다
        assert not holding.arrived.wait_for(lambda: len(holding.entered) > 3, 0.5)
    for gate in holding.gates.values():
        gate.set()
    for thread in threads:
        thread.join(WAIT)
    assert count["most"] == 3


def test_cancelling_stops_new_windows_and_raises(tmp_path: Path) -> None:
    windows = _windows(tmp_path, 8)
    holding = Holding(windows)
    stop = threading.Event()

    class Stop(BaseException):
        pass

    def check() -> None:
        if stop.is_set():
            raise Stop

    errors: list[BaseException] = []
    settled: list[SemanticAnnotation] = []

    def work() -> None:
        try:
            with cancellation_scope(check):
                _run(windows, holding, 2, settled=settled)
        except BaseException as exc:  # 시험이 실행 스레드의 끝을 본다
            errors.append(exc)

    runner = threading.Thread(target=work)
    runner.start()
    holding.wait_inside(2)
    stop.set()
    holding.gates["window_000000"].set()  # 첫 창만 돌아와 취소 확인에 닿는다
    time.sleep(0.3)
    for gate in holding.gates.values():  # 둘째 창은 그 뒤에 돌아온다 — 끝까지 기다려 남긴다
        gate.set()
    runner.join(WAIT)
    assert errors and isinstance(errors[0], Stop)
    assert len(holding.entered) <= 2 + 1  # 이미 나간 창과 막 자리를 얻은 창까지만
    # 나간 창은 값을 치렀다 — 멈춰도 모두 판정으로 남는다
    assert sorted(a.window_id for a in settled) == sorted(holding.entered)


def test_windows_behind_a_slow_first_window_are_kept_when_cancelled(tmp_path: Path) -> None:
    """첫 창이 늦으면 뒤 창들은 접두에 들지 못한다 — 그래도 끝나는 대로 남아 다시 사지 않는다."""
    windows = _windows(tmp_path, 6)
    holding = Holding(windows)
    for key in list(holding.gates)[1:]:
        holding.gates[key].set()
    stop = threading.Event()

    class Stop(BaseException):
        pass

    def check() -> None:
        if stop.is_set():
            raise Stop

    progress: list[tuple[SemanticAnnotation, ...]] = []
    settled: list[SemanticAnnotation] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            with cancellation_scope(check):
                _run(windows, holding, 3, progress, settled, checked=True)
        except BaseException as exc:
            errors.append(exc)

    runner = threading.Thread(target=work)
    runner.start()
    holding.wait_inside(6)
    give_up = time.monotonic() + WAIT
    while len(settled) < 5 and time.monotonic() < give_up:
        time.sleep(0.01)
    assert sorted(a.window_id for a in settled) == [w.window.window_id for w in windows[1:]]
    assert progress == []  # 첫 창이 아직이라 진행은 하나도 알리지 않았다
    stop.set()
    holding.gates["window_000000"].set()
    runner.join(WAIT)
    assert errors and isinstance(errors[0], Stop)
    assert progress == []  # 알림은 취소에 막혔다 — 판정은 이미 남았다
    assert len(settled) == 6


def test_each_worker_keeps_its_own_receipt(tmp_path: Path) -> None:
    """일꾼은 문맥을 복사해 돈다 — 창마다 제 영수증 줄이 열리고 제 줄에서 닫힌다."""

    class Workspace:
        def connect(self) -> sqlite3.Connection:
            return sqlite3.connect(tmp_path / "workspace.db")

    windows = _windows(tmp_path, 6)
    seen: dict[str, str | None] = {}
    lock = threading.Lock()
    bodies = {w.window.window_id: respond(accepted_body(w)) for w in windows}

    class Watching:
        def respond(self, *, window_id: str, payload: Any) -> Any:
            with lock:
                seen[window_id] = request_log.open_attempt()
            return bodies[window_id]

    with request_log.scope(Workspace(), "job_parallel", live=True):
        annotations = _run(windows, Watching(), 4)
    with Workspace().connect() as db:
        rows = dict(db.execute("SELECT id, fingerprint FROM analysis_requests").fetchall())
        statuses = {row[0] for row in db.execute("SELECT status FROM analysis_requests")}
    assert len(rows) == len(windows) and statuses == {"response_received"}
    for annotation in annotations:
        attempt = seen[annotation.window_id]
        assert attempt is not None and rows[attempt] == annotation.request_sha256


def test_a_failed_window_does_not_stop_the_others(tmp_path: Path) -> None:
    windows = _windows(tmp_path, 4)

    class Flaky:
        def respond(self, *, window_id: str, payload: Any) -> Any:
            if window_id == "window_000001":
                raise TimeoutError("30초 초과")
            return respond(accepted_body(windows[int(window_id[-1])]))

    annotations = _run(windows, Flaky(), 4)
    assert [a.status for a in annotations] == [
        luna.ANNOTATION_ACCEPTED,
        luna.ANNOTATION_FAILED,
        luna.ANNOTATION_ACCEPTED,
        luna.ANNOTATION_ACCEPTED,
    ]


# ─────────────────────────────────────────────────────────────
#  예약 문 — 여러 일꾼이 함께 예약해도 한도를 넘지 않는다
# ─────────────────────────────────────────────────────────────


def test_the_reservation_gate_never_overspends_under_threads() -> None:
    gate = CountingReservationGate(AnalysisBudget(max_luna_requests=40, max_asr_chunks=1))
    granted: list[str] = []
    refused: list[str] = []
    start = threading.Barrier(16)

    def reserve(worker: int) -> None:
        start.wait(WAIT)
        for k in range(10):
            key = f"window_{worker:02d}_{k}"
            try:
                gate.reserve(count=1, key=key)
                granted.append(key)
            except BudgetExhausted:
                refused.append(key)

    threads = [threading.Thread(target=reserve, args=(w,)) for w in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert len(granted) == 40 and gate.remaining == 0
    assert len(refused) == 16 * 10 - 40
