"""프레임 묶음 추출 — 정확·중복 제거·취소·캐시, 그리고 select 식의 파서 깊이 한도.

평평한 `eq(n,a)+eq(n,b)+…` 사슬은 101항에서 libavutil 식 파서의 깊이(100)를 넘겨
"Cannot allocate memory"로 필터 초기화에서 죽었다. 실측 실패는 112~504 프레임이었다.
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_browser_runtime import _make_media
from vlogsrt import preprocess
from vlogsrt.preprocess import extract_frame_index, extract_frame_indices
from vlogsrt.project.raw.analysis_config import ANALYSIS_PROXY_LIMITS
from vlogsrt.project.raw.jobs import JobCancelledError
from vlogsrt.project.raw.media_refs import _FrameSampler
from vlogsrt.task_runtime import cancellation_scope

LONG_EDGE = 160
TIMEOUT = ANALYSIS_PROXY_LIMITS.frame_batch_timeout(None)  # 길이를 모르면 바닥(D59)


def test_batch_is_exact_and_deduplicates_with_single_decode(tmp_path, monkeypatch):
    source = tmp_path / "frames.mov"
    _make_media(source, audio=False, seconds="3")
    expected = {
        i: preprocess.extract_frame_index(
            source, 0, i, tmp_path / f"old-{i}.jpg", long_edge=160
        ).read_bytes()
        for i in (0, 7, 31, 74)
    }
    actual_run = preprocess.run_process
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return actual_run(command, **kwargs)

    monkeypatch.setattr(preprocess, "run_process", run)
    ready = []
    output = preprocess.extract_frame_indices(
        source,
        0,
        [74, 7, 31, 7, 0],
        tmp_path / "batch",
        long_edge=160,
        timeout_seconds=TIMEOUT,
        on_frame=lambda i, p: ready.append((i, p.read_bytes())),
    )
    assert len(calls) == 1 and calls[0].count("-i") == 1
    assert list(output) == [0, 7, 31, 74]
    assert ready == list(expected.items())
    assert all(output[i].read_bytes() == expected[i] for i in expected)
    assert not list((tmp_path / "batch").glob(".frame-batch-*"))


def test_batch_cache_reuses_success_and_only_requests_missing(tmp_path, monkeypatch):
    source = tmp_path / "frames.mov"
    _make_media(source, audio=False, seconds="3")
    sampler = _FrameSampler(source, 0, tmp_path / "cache", TIMEOUT)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    sampler.prepare(frame_indices=(2, 10), directory=a, long_edge=160)
    original = preprocess.extract_frame_indices
    calls = []

    def extract(source, stream, indices, *args, **kwargs):
        calls.append(indices)
        return original(source, stream, indices, *args, **kwargs)

    monkeypatch.setattr(preprocess, "extract_frame_indices", extract)
    sampler.prepare(frame_indices=(2, 10, 25, 25), directory=b, long_edge=160)
    assert calls == [[25]]
    assert (a / "frame_00000002.jpg").read_bytes() == (b / "frame_00000002.jpg").read_bytes()
    sampler.prepare(frame_indices=(2, 10, 25), directory=b, long_edge=160)
    assert calls == [[25]]


def test_batch_cancel_keeps_completed_frames_without_partial_publication(tmp_path):
    source = tmp_path / "frames.mov"
    _make_media(source, audio=False, seconds="3")
    stop = threading.Event()
    ready = []

    def check():
        if stop.is_set():
            raise JobCancelledError("synthetic cancel")

    def received(index, path):
        ready.append(path)
        stop.set()

    with cancellation_scope(check), pytest.raises(JobCancelledError):
        preprocess.extract_frame_indices(
            source,
            0,
            list(range(75)),
            tmp_path / "frames",
            long_edge=160,
            timeout_seconds=TIMEOUT,
            on_frame=received,
        )
    assert len(ready) == 1 and ready[0].read_bytes().startswith(b"\xff\xd8")
    assert list((tmp_path / "frames").iterdir()) == ready


def test_process_drains_large_stdout_and_stderr_while_polling():
    polls = []
    result = preprocess.run_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x'*2000000); sys.stdout.flush(); "
            "sys.stderr.write('y'*2000000)",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        on_poll=lambda: polls.append(1),
    )
    assert result.returncode == 0
    assert len(result.stdout) == len(result.stderr) == 2000000 and polls


def test_lightweight_status_does_not_wait_for_workbench_render(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from ui.workspace_service import RawWorkspaceUi

    ui = RawWorkspaceUi(state_path=tmp_path / "state.json")
    ui.create_workspace_view("progress", str(tmp_path / "workspace"))
    entered, release = threading.Event(), threading.Event()
    count = [1]
    monkeypatch.setattr(ui, "_quick_status", lambda: f"프레임 {count[0]}/40")

    def slow_render(root, message, **_upkeep):  # 틱은 `upkeep=`를 넘긴다(D63)
        entered.set()
        assert release.wait(5)
        return message, "shell", "workbench"

    monkeypatch.setattr(ui, "_capcut_result", slow_render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ui.capcut_tick_view, ui.current_signature())
        try:
            assert entered.wait(2)
            count[0] = 6
            assert ui.capcut_status_view(ui.current_signature()) == "프레임 6/40"
            assert not future.done()
        finally:
            release.set()
        future.result(timeout=2)


def test_only_the_status_tick_writes_the_progress_line_not_the_redraw(tmp_path, local_socket):
    from tests.raw.review_tick_parts import tick_part
    from ui.workspace_service import RawWorkspaceUi

    ui = RawWorkspaceUi(state_path=tmp_path / "state.json")
    app = ui.build_blocks()
    # 상태 줄은 상태 틱 하나가 쓴다(D63 세션 C) — 다시 그리기와 따로. 둘은 화면 틱 하나에
    # 실린 처리기다(W-83, D143 — 의존은 대기열이다, 알림이 대기열에서만 뜬다). 다시 그리기의
    # 몫에는 상태 줄 칸이 없다 — 옛 그리기가 상태 줄을 덮어쓰지 못한다.
    status = tick_part(app, ui, ui.status_tick)
    render = tick_part(app, ui, ui.capcut_tick_view)
    assert ui._capcut_progress_md._id in status.outputs
    assert ui._capcut_progress_md._id not in render.outputs


def test_cancel_during_batch_resumes_without_second_stt_call(tmp_path, monkeypatch):
    from tests.raw.test_selection_analysis import make_server, wait_until

    source = tmp_path / "resume.mov"
    _make_media(source, audio=True, seconds="3")
    ui, sender, client = make_server(tmp_path, monkeypatch, source)
    entered, release = threading.Event(), threading.Event()
    original = preprocess.extract_frame_indices
    requests = []
    first_frame = []

    def extract(source, stream, indices, *args, **kwargs):
        requests.append(tuple(indices))
        callback = kwargs["on_frame"]

        def ready(index, path):
            callback(index, path)  # completed frame and its hash are durable before cancel
            if not entered.is_set():
                first_frame.append(index)
                entered.set()
                assert release.wait(20)

        kwargs["on_frame"] = ready
        return original(source, stream, indices, *args, **kwargs)

    monkeypatch.setattr(preprocess, "extract_frame_indices", extract)
    ui.capcut_choose_view(ui.current_signature())
    try:
        assert entered.wait(20), ui._quick
        root = ui.workspace_root()
        job_id = ui._quick["job_id"]
        ui.capcut_cancel_view(ui.current_signature())
    finally:
        release.set()
    wait_until(lambda: ui.raw.get_job(root, job_id).status == "cancelled")
    wait_until(lambda: not any(j.status in ("queued", "running") for j in ui.raw.list_jobs(root)))
    assert len(sender.calls) == 1 and not client.calls
    assert ui._quick["stage"] == "취소됨"
    ui.capcut_retry_view(ui.current_signature())
    wait_until(lambda: ui._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert ui._quick["stage"] == "추천 구간 준비 완료", ui._quick
    assert len(sender.calls) == 1 and len(client.calls) == 1
    assert len(requests) == 2 and first_frame[0] not in requests[1]
    assert len(requests[1]) == len(requests[0]) - 1


def test_batch_over_one_hundred_frames_is_one_decode_and_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """150 프레임도 디코드 한 번 — 각 장은 단일 추출과 바이트가 같다."""
    source = tmp_path / "clip.mp4"
    _make_media(source, audio=False, seconds="6")  # 25fps × 6초 = 150 프레임
    commands: list[list[str]] = []
    real_run_process = preprocess.run_process

    def spy(command: list[str], *args: Any, **kwargs: Any) -> Any:
        commands.append(list(command))
        return real_run_process(command, *args, **kwargs)

    monkeypatch.setattr(preprocess, "run_process", spy)
    published = extract_frame_indices(
        source,
        0,
        list(range(150)),
        tmp_path / "batch",
        long_edge=LONG_EDGE,
        timeout_seconds=TIMEOUT,
    )
    monkeypatch.setattr(preprocess, "run_process", real_run_process)

    assert len(commands) == 1
    assert commands[0].count("-i") == 1
    assert sorted(published) == list(range(150))
    assert all(path.is_file() for path in published.values())
    for index in (0, 101, 149):
        single = extract_frame_index(
            source, 0, index, tmp_path / "single" / f"{index}.jpg", long_edge=LONG_EDGE
        )
        assert published[index].read_bytes() == single.read_bytes(), index


def _chain_lengths_by_depth(expression: str) -> list[int]:
    """괄호 깊이가 같은 연속 구간마다 `+`로 이어진 항의 수를 센다.

    `eq(n\\,k)`의 괄호는 함수 호출이라 사슬 깊이에 넣지 않는다 — 묶음 괄호만 센다.
    """
    flattened = re.sub(r"eq\(n\\,\d+\)", "T", expression)
    assert set(flattened) <= {"T", "+", "(", ")"}, flattened
    runs: list[int] = []
    stack: list[int] = [0]
    for char in flattened:
        if char == "(":
            stack.append(0)
        elif char == ")":
            runs.append(stack.pop())
            stack[-1] += 1  # 닫힌 묶음은 바깥 사슬의 한 항이다
        elif char == "T":
            stack[-1] += 1
    runs.append(stack.pop())
    assert not stack
    return runs


def test_select_expression_bounds_chain_depth() -> None:
    """짧은 목록은 평평한 그대로, 긴 목록은 사슬마다 상한 이하의 항으로 묶인다."""
    # 모듈 속성으로 찾는다 — 없으면 이 테스트만 실패하고 묶음 테스트는 따로 돈다
    expression_of = preprocess._select_frames_expression
    limit = preprocess._SELECT_CHAIN_MAX_TERMS
    assert expression_of(range(3)) == "eq(n\\,0)+eq(n\\,1)+eq(n\\,2)"
    for count in (51, 101, 2000):
        expression = expression_of(range(count))
        runs = _chain_lengths_by_depth(expression)
        assert max(runs) <= limit, (count, max(runs))
        found = [int(item) for item in re.findall(r"eq\(n\\,(\d+)\)", expression)]
        assert sorted(found) == list(range(count))
        assert len(found) == count


def test_batch_hands_its_limit_to_the_decoder_and_a_timeout_is_a_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """묶음 추출의 한도는 호출자가 원본 길이로 정한 값이다(D59) — 예전 리터럴 1800이 아니다.

    한도를 넘기면 `ProcessTimeout`(RuntimeError)이고 반쪽 장은 게시되지 않는다.
    """
    source = tmp_path / "clip.mp4"
    _make_media(source, audio=False, seconds="3")
    seen: list[dict[str, Any]] = []
    real_run_process = preprocess.run_process

    def spy(command: list[str], *args: Any, **kwargs: Any) -> Any:
        seen.append(dict(kwargs))
        return real_run_process(command, *args, **kwargs)

    monkeypatch.setattr(preprocess, "run_process", spy)
    extract_frame_indices(
        source, 0, [3], tmp_path / "a", long_edge=LONG_EDGE, timeout_seconds=4321.0
    )
    assert (seen[0]["timeout"], seen[0]["step"]) == (4321.0, "프레임 묶음 추출")
    with (
        pytest.raises(RuntimeError, match="프레임 묶음 추출 — 시간 한도"),
        cancellation_scope(lambda: None),
    ):
        extract_frame_indices(
            source, 0, list(range(75)), tmp_path / "b", long_edge=LONG_EDGE, timeout_seconds=0.0
        )
    assert not list((tmp_path / "b").glob(".frame-batch-*"))
