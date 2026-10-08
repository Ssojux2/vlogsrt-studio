from __future__ import annotations

import sys
import threading

import pytest

from tests.raw.test_browser_runtime import _make_media
from vlogsrt import preprocess
from vlogsrt.project.raw.jobs import JobCancelledError
from vlogsrt.project.raw.media_refs import _FrameSampler
from vlogsrt.task_runtime import cancellation_scope


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
    sampler = _FrameSampler(source, 0, tmp_path / "cache")
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
            source, 0, list(range(75)), tmp_path / "frames", long_edge=160, on_frame=received
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

    def slow_render(root, message):
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


def test_progress_timer_is_unqueued_and_old_render_cannot_overwrite_it(tmp_path, local_socket):
    from ui.workspace_service import RawWorkspaceUi

    ui = RawWorkspaceUi(state_path=tmp_path / "state.json")
    app = ui.build_blocks()
    status = next(f for f in app.fns.values() if f.fn == ui.capcut_status_view)
    render = next(f for f in app.fns.values() if f.fn == ui.capcut_tick_view)
    assert status.queue is False
    assert status.outputs == [ui._capcut_progress_md]
    assert ui._capcut_progress_md not in render.outputs


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
