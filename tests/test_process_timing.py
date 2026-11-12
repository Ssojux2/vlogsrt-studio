"""끝난 외부 프로세스의 경과 시간 기록(성능 계획 Phase 0, W-69 · D120).

`run_process` 한 자리(D61 등록표)가 끝난 프로세스마다 단계·도구·입력·초를 남긴다 — 최근 줄은
한도가 있는 고리, 단계별 합계는 (단계, 도구)마다 하나다. **표시만 한다**: argv·출력 바이트·지문은
그대로이고, 서버 로그에는 한도 이상 걸린 것만 한 줄씩 적는다(ffprobe 수백 번이 로그를 덮지 않게).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from vlogsrt import preprocess
from vlogsrt.task_runtime import cancellation_scope

FFMPEG_ARGV = [
    "/opt/homebrew/bin/ffmpeg", "-hide_banner", "-nostdin", "-y",
    "-i", "/media/in.mov", "-c:v", "libx264", "/out/proxy.mp4",
]  # fmt: skip
FFPROBE_ARGV = ["/opt/homebrew/bin/ffprobe", "-v", "error", "-of", "json", "/media/in.mov"]


class FakeClock:
    """부를 때마다 정해 둔 걸음만큼 간다 — 프로세스 하나 = 시작·끝 두 번 읽기."""

    def __init__(self, step: float) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture(autouse=True)
def fresh_timing() -> Iterator[None]:
    preprocess.disable_process_progress()
    preprocess.reset_process_timing()
    yield
    preprocess.reset_process_timing()


def _fake_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(preprocess.subprocess, "run", run)
    return seen


def _logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(preprocess.log, "info", lines.append)
    return lines


def test_a_finished_process_is_recorded_with_its_step_input_and_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _fake_run(monkeypatch)
    monkeypatch.setattr(preprocess, "_TIMING_CLOCK", FakeClock(1.5))
    preprocess.run_process(FFMPEG_ARGV, capture_output=True, step="분석 프록시 만들기")
    assert seen == [FFMPEG_ARGV]  # 재기만 한다 — argv는 받은 그대로
    (finished,) = preprocess.recent_processes()
    assert finished.step == "분석 프록시 만들기"
    assert finished.tool == "ffmpeg"
    assert finished.input_path == "/media/in.mov"
    assert finished.seconds == pytest.approx(1.5)
    assert finished.outcome == "finished"
    assert finished.finished_at > 0


def test_step_totals_add_up_per_step_and_tool_and_sort_by_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_run(monkeypatch)
    clock = FakeClock(0.5)
    monkeypatch.setattr(preprocess, "_TIMING_CLOCK", clock)
    for _ in range(3):
        preprocess.run_process(FFPROBE_ARGV, step="원본 조사")
    clock.step = 4.0
    preprocess.run_process(FFMPEG_ARGV, step="미리보기 만들기")
    preview, probe = sorted(preprocess.process_step_totals(), key=lambda row: row.step)
    assert (preview.step, preview.tool, preview.count) == ("미리보기 만들기", "ffmpeg", 1)
    assert preview.total_seconds == pytest.approx(4.0)
    assert (probe.step, probe.tool, probe.count) == ("원본 조사", "ffprobe", 3)
    assert probe.total_seconds == pytest.approx(1.5)
    assert probe.max_seconds == pytest.approx(0.5)
    steps = [row.step for row in preprocess.process_step_totals()]
    assert steps == ["미리보기 만들기", "원본 조사"]


def test_a_process_that_raises_is_recorded_as_raised() -> None:
    with pytest.raises(OSError):
        preprocess.run_process(["/nonexistent/ffmpeg-binary", "-i", "x", "y"], step="썸네일 추출")
    (finished,) = preprocess.recent_processes()
    assert (finished.step, finished.outcome) == ("썸네일 추출", "raised")


@pytest.mark.parametrize("cancellable", [False, True])
def test_a_timeout_is_recorded_on_both_paths(cancellable: bool) -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    scope = cancellation_scope(lambda: None) if cancellable else _nothing()
    with scope, pytest.raises(preprocess.ProcessTimeout):
        preprocess.run_process(sleeper, capture_output=True, timeout=0.3, step="장면 컷 찾기")
    (finished,) = preprocess.recent_processes()
    assert finished.outcome == "raised"
    assert finished.seconds >= 0.3


def test_the_recent_ring_keeps_only_the_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch)
    monkeypatch.setattr(preprocess, "PROCESS_TIMING", preprocess.ProcessTimingSettings(recent=2))
    preprocess.reset_process_timing()
    for step in ("하나", "둘", "셋"):
        preprocess.run_process(FFPROBE_ARGV, step=step)
    assert [row.step for row in preprocess.recent_processes()] == ["둘", "셋"]
    # 합계는 고리와 따로다 — 밀려난 줄도 단계 합계에 남는다.
    assert {row.step for row in preprocess.process_step_totals()} == {"하나", "둘", "셋"}


def test_only_slow_processes_reach_the_server_log(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch)
    lines = _logged(monkeypatch)
    clock = FakeClock(0.2)
    monkeypatch.setattr(preprocess, "_TIMING_CLOCK", clock)
    monkeypatch.setattr(
        preprocess, "PROCESS_TIMING", preprocess.ProcessTimingSettings(log_seconds=5.0)
    )
    preprocess.run_process(FFPROBE_ARGV, step="원본 조사")
    assert lines == []
    clock.step = 6.0
    preprocess.run_process(FFMPEG_ARGV, step="분석 프록시 만들기")
    assert len(lines) == 1
    assert "분석 프록시 만들기" in lines[0] and "in.mov" in lines[0] and "6.0초" in lines[0]


@pytest.mark.parametrize(
    "fields",
    [{"recent": 0}, {"recent": True}, {"recent": 1.5}, {"log_seconds": -1.0}],
)
def test_settings_refuse_nonsense(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        preprocess.ProcessTimingSettings(**fields)


def _nothing() -> Any:
    from contextlib import nullcontext

    return nullcontext()
