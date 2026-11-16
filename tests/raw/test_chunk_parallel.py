"""1단계 전사 청크를 함께 보내기(P-5 · 성능 계획 Phase 4-3, D159) — 사용자가 고른 4.

- 1이면 예전 그대로 차례로(같은 스레드), 넘으면 그만큼 함께 — 결과는 언제나 청크 차례다.
- 그 수는 **프로세스 전체** 상한이다 — 두 전사가 함께 돌아도 넘지 않는다.
- 실행 전체의 멈춤(취소)은 올라가고 아직 시작하지 않은 청크는 거둔다.
- 예약 문(`CountingAsrGate`)은 함께 예약해도 한도를 정확히 지킨다.
- 같은 청크 · 같은 응답이면 1과 4의 결과(`AsrEvidence`)가 같다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tests.raw.test_understanding import (
    BEFORE_SHUTDOWN,
    SETTINGS,
    FakeTimingProvider,
    make_audio,
)
from vlogsrt.project.raw import understanding
from vlogsrt.project.raw.analysis_workflow import AsrBudgetExhausted, CountingAsrGate
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.understanding import SampleSpan, transcribe_selected_audio


class _Meter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.threads: set[str] = set()

    def run(self, position: int, span: SampleSpan) -> str:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            self.threads.add(threading.current_thread().name)
        time.sleep(0.02)
        with self.lock:
            self.now -= 1
        return f"chunk-{position}"


def _spans(count: int) -> list[tuple[int, SampleSpan]]:
    return [(k, SampleSpan(k * 10, k * 10 + 10)) for k in range(count)]


def test_one_is_the_old_serial_loop_and_more_runs_together_in_chunk_order() -> None:
    serial = _Meter()
    assert understanding._each_chunk(_spans(5), serial.run, 1) == [f"chunk-{k}" for k in range(5)]
    assert serial.peak == 1 and serial.threads == {threading.current_thread().name}
    together = _Meter()
    assert understanding._each_chunk(_spans(10), together.run, 4) == [
        f"chunk-{k}" for k in range(10)
    ]
    assert together.peak == 4


def test_the_width_is_a_process_wide_ceiling() -> None:
    meter = _Meter()
    results: list[list[str]] = []

    def transcription() -> None:
        results.append(understanding._each_chunk(_spans(8), meter.run, 4))

    workers = [threading.Thread(target=transcription) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert meter.peak <= 4  # 영상 셋이 함께 돌아도 공급자에는 넷까지
    assert all(result == [f"chunk-{k}" for k in range(8)] for result in results)


def test_a_cancel_stops_the_run_and_unstarted_chunks_are_not_sent() -> None:
    started: list[int] = []
    lock = threading.Lock()

    class Cancelled(Exception):
        pass

    def one(position: int, span: SampleSpan) -> str:
        with lock:
            started.append(position)
        if position == 0:
            raise Cancelled
        time.sleep(0.05)
        return "ok"

    with pytest.raises(Cancelled):
        understanding._each_chunk(_spans(40), one, 4)
    assert len(started) < 40  # 거둔 청크는 보내지 않는다


def test_the_chunk_gate_counts_exactly_under_contention() -> None:
    gate = CountingAsrGate(limit=50)
    refused: list[int] = []
    barrier = threading.Barrier(100)

    def reserve(k: int) -> None:
        barrier.wait()
        try:
            gate.reserve(count=1, key=f"chunk-{k}")
        except AsrBudgetExhausted:
            refused.append(k)

    workers = [threading.Thread(target=reserve, args=(k,)) for k in range(100)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert gate.reserved == 50 and gate.remaining == 0 and len(refused) == 50


def test_the_evidence_is_the_same_at_one_and_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = {k: [{"text": f"말 {k}", "start": 0.5, "end": 1.5}] for k in range(6)}
    spans = tuple(SampleSpan(k * 48000, (k + 1) * 48000) for k in range(6))
    evidence = {}
    for width in (1, 4):
        monkeypatch.setattr(
            understanding,
            "PROVIDER_CONCURRENCY",
            replace(understanding.PROVIDER_CONCURRENCY, asr_chunks=width),
        )
        folder = tmp_path / f"w{width}"
        folder.mkdir()
        audio = make_audio(folder, seconds=18.0)
        evidence[width] = transcribe_selected_audio(
            audio,
            spans,
            settings=RawWorkflowConfig(),
            understanding=SETTINGS,
            provider=FakeTimingProvider(replies=replies),
            on=BEFORE_SHUTDOWN,
            request_id="run",
            workdir=folder,
        )
    assert evidence[1] == evidence[4]
    assert [chunk.chunk_index for chunk in evidence[4].chunks] == list(range(6))
