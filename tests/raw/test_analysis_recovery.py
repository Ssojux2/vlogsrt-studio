from __future__ import annotations

import sys
import threading

import pytest

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_selection_analysis import SuitabilityClient, make_server, wait_until
from vlogsrt.preprocess import run_process
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.jobs import JobCancelledError
from vlogsrt.project.raw.selection_analysis import load_state
from vlogsrt.task_runtime import cancellation_scope


class FailsOnce(SuitabilityClient):
    def __init__(self):
        super().__init__()
        self.failed = False

    def _create(self, **payload):
        if len(self.calls) == 1 and not self.failed:
            self.failed = True
            self.calls.append(payload)
            raise TimeoutError("synthetic timeout")
        return super()._create(**payload)


def test_partial_retry_only_failed_window_and_persisted_status(tmp_path, monkeypatch):
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    server, sender, client = make_server(tmp_path, monkeypatch, source, FailsOnce())
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("부분 완료 · 확인 필요", "분석 확인 필요"))
    assert server._quick["stage"] == "부분 완료 · 확인 필요", server._quick
    root = server.workspace_root()
    revision = server._quick["revision"]
    assert AnalysisStore(server.raw._workspace(root)).revision(revision).status == "partial"
    assert load_state(server.raw, root)["stage"] == "부분 완료 · 확인 필요"
    assert len(sender.calls) == 1 and len(client.calls) == 3
    source_result = AnalysisStore(server.raw._workspace(root)).list_source_results(revision)[0]
    frame_dir = (root / source_result.document_path).parent / "frames"
    next(
        frame_dir.glob("*.jpg")
    ).unlink()  # missing local stage artifact must recover from frame cache
    from vlogsrt import preprocess

    monkeypatch.setattr(
        preprocess,
        "extract_frame_index",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("already completed frames must reuse their verified cache")
        ),
    )
    server.capcut_retry_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert server._quick["revision"] == revision
    assert len(sender.calls) == 1 and len(client.calls) == 4


def test_interrupted_selection_restores_without_paid_resume(tmp_path, monkeypatch):
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="2")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server._selection_auto_approval = None
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    root = server.workspace_root()
    wait_until(lambda: server.raw.get_job(root, server._quick["job_id"]).status == "succeeded")
    state = load_state(server.raw, root)
    assert state["stage"] == "중단 · 재시도 승인 필요"
    assert state["asset_ids"] == server._quick["asset_ids"]
    assert not sender.calls and not client.calls


def test_cancellation_terminates_active_process(tmp_path):
    cancelled = threading.Event()

    def check():
        if cancelled.is_set():
            raise JobCancelledError("synthetic")

    timer = threading.Timer(0.4, cancelled.set)
    timer.start()
    try:
        with cancellation_scope(check), pytest.raises(JobCancelledError):
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True,
                text=True,
                check=False,
            )
    finally:
        timer.cancel()


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "authentication"),
        (403, "model_access"),
        (404, "model_access"),
        (400, "request_format"),
        (429, "rate_limit"),
        (503, "connection"),
    ],
)
def test_provider_errors_are_distinct_and_sanitized(status, category):
    from vlogsrt.task_runtime import provider_error

    error = RuntimeError("sk-secret-never-display")
    error.status_code = status
    code, message, uncertain = provider_error(error)
    assert code == category
    assert "sk-secret" not in message
    assert uncertain == (status == 503)


def test_timeout_uncertainty_is_not_success_cache():
    from vlogsrt.task_runtime import provider_error

    code, message, uncertain = provider_error(TimeoutError("sk-secret-never-display"))
    assert code == "timeout" and uncertain
    assert "추가 호출" in message and "sk-secret" not in message


def test_no_audio_verified_scene_boundaries_preserve_both_scenes(tmp_path, monkeypatch):
    import json
    import re
    import subprocess

    class SceneClient(SuitabilityClient):
        def _create(self, **payload):
            result = super()._create(**payload)
            body = json.loads(result["output_text"])
            text = "\n".join(
                c["text"]
                for m in payload["input"]
                for c in m["content"]
                if c["type"] == "input_text"
            )
            body["suggested_boundary_ids"] = list(dict.fromkeys(re.findall(r"scene_\d+", text)))
            return {"output_text": json.dumps(body)}

    source = tmp_path / "scenes.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=160x90:r=25:d=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    server, sender, client = make_server(tmp_path, monkeypatch, source, SceneClient())
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert not sender.calls and len(client.calls) == 1
    head = server.raw.review_workspace_facade(server.workspace_root()).load_head().revision
    assert len(head.segments) == 2
    assert all(s.selection == "included" for s in head.segments)
    assert all(s.speech_status == "audio_absent" for s in head.segments)


def test_failed_asr_retry_fills_captions_and_keeps_uncertain_receipt(tmp_path, monkeypatch):
    from vlogsrt.project.raw import source_transcript

    source = tmp_path / "asr_retry.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    real = sender.send_request
    attempts = []

    def send(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("synthetic")
        return real(*args, **kwargs)

    monkeypatch.setattr(sender, "send_request", send)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("부분 완료 · 확인 필요", "분석 확인 필요"))
    root = server.workspace_root()
    (asset_id,) = server._quick["asset_ids"]
    assert source_transcript.load(server.raw._workspace(root), asset_id)["utterances"] == []
    server.capcut_retry_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert source_transcript.load(server.raw._workspace(root), asset_id)["utterances"]
    with server.raw._workspace(root).connect() as db:
        receipts = db.execute(
            "SELECT stage,status,note FROM analysis_requests ORDER BY rowid"
        ).fetchall()
    assert any(
        r["stage"] == "asr" and r["status"] == "response_uncertain" and r["note"] == "timeout"
        for r in receipts
    )
    assert sum(r["stage"] == "asr" for r in receipts) == 2


def test_missing_completed_document_rebuilds_from_success_cache(tmp_path, monkeypatch):
    source = tmp_path / "missing_document.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    root = server.workspace_root()
    row = AnalysisStore(server.raw._workspace(root)).list_source_results(server._quick["revision"])[
        0
    ]
    (root / row.document_path).unlink()
    server.capcut_retry_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert (root / row.document_path).is_file()
    assert len(sender.calls) == 1 and len(client.calls) == 1


def test_failed_source_does_not_block_healthy_source_or_cached_retry(tmp_path, monkeypatch):
    from tests.raw.test_review_workspace import _wait_terminal
    from vlogsrt.project.raw import analysis_workflow

    a, b = tmp_path / "healthy.mov", tmp_path / "broken.mov"
    _make_media(a, audio=True, seconds="3")
    _make_media(b, audio=True, seconds="4")
    server, sender, client = make_server(tmp_path, monkeypatch, a)
    root = server.workspace_root()
    for source in (a, b):
        child = server.raw.import_selected_video(root, source)
        assert _wait_terminal(server.raw, root, child.job_id).status == "succeeded"
    server.raw.ensure_source_order(root)
    ids = tuple(a.asset_id for a in server.raw.list_source_assets(root))
    original = analysis_workflow.stage1.select_audio
    fail = [True]

    def select(source, *args, **kwargs):
        if source.name == "broken.mov" and fail[0]:
            raise RuntimeError("synthetic preparation failure")
        return original(source, *args, **kwargs)

    monkeypatch.setattr(analysis_workflow.stage1, "select_audio", select)
    server._start_quick(root, (), asset_ids=ids)
    wait_until(lambda: server._quick.get("stage") in ("부분 완료 · 확인 필요", "분석 확인 필요"))
    assert server._quick["stage"] == "부분 완료 · 확인 필요", server._quick
    assert len(sender.calls) == 1 and len(client.calls) == 1
    fail[0] = False
    server.capcut_retry_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert len(sender.calls) == 2 and len(client.calls) == 2


def test_cancelled_selection_stays_cancelled_after_inflight_response(tmp_path, monkeypatch):
    source = tmp_path / "cancel.mov"
    _make_media(source, audio=True, seconds="32")
    client = SuitabilityClient(pause_second=True)
    server, _, _ = make_server(tmp_path, monkeypatch, source, client)
    server.capcut_choose_view(server.current_signature())
    assert client.second_entered.wait(30)
    job_id = server._quick["job_id"]
    server.capcut_cancel_view(server.current_signature())
    client.resume.set()
    wait_until(lambda: server.raw.get_job(server.workspace_root(), job_id).status == "cancelled")
    assert server._quick["stage"] == "취소됨"
    assert load_state(server.raw, server.workspace_root())["stage"] == "취소됨"
