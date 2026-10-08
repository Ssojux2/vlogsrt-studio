from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from tests.raw.test_browser_runtime import _CountingSender, _make_media
from ui.workspace_service import RawWorkspaceUi
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport
from vlogsrt.project.raw.config import RawModelRoles


class SuitabilityClient:
    def __init__(self, pause_second: bool = False):
        self.calls = []
        self.second_entered = threading.Event()
        self.resume = threading.Event()
        self.pause_second = pause_second
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **payload):
        self.calls.append(payload)
        text = "\n".join(
            c["text"] for m in payload["input"] for c in m["content"] if c["type"] == "input_text"
        )
        window = re.search(r"window_id: (\S+)", text).group(1)
        frames = re.search(r"프레임 증거\(순서대로\): ([^\n]+)", text).group(1).split(", ")
        index = int(window.rsplit("_", 1)[1])
        if index == 1 and self.pause_second:
            self.second_entered.set()
            assert self.resume.wait(15), "test did not release second request"
        body = {
            "window_id": window,
            "speech_summary": "합성 설명",
            "visual_summary": "합성 풍경",
            "scene_tags": ["scenery"],
            "evidence_ids": frames[:1],
            "suggested_boundary_ids": [],
            "editing_notes": [],
            "needs_review": index == 1,
            "uncertainties": [],
            "suitability": ["recommended", "review", "usable"][min(index, 2)],
            "suitability_reason": [
                "안정된 구도와 선명한 풍경",
                "움직임을 확인해 주세요",
                "연결 장면으로 사용 가능",
            ][min(index, 2)],
        }
        return {"output_text": json.dumps(body, ensure_ascii=False)}


def wait_until(check, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("condition did not settle")


def make_server(
    tmp_path: Path, monkeypatch, source: Path, client=None, parallel_analysis_limit=None
):
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    sender = _CountingSender([{"start": 0.5, "end": 1.5, "text": "합성 영상입니다."}])
    client = client or SuitabilityClient()
    server = RawWorkspaceUi(
        state_path=tmp_path / "ui-state.json",
        path_chooser=lambda kind: str(source),
        live=LiveAnalysisTransport(
            sender=sender,
            cfg=SimpleNamespace(),
            client=client,
            asr_approvals=lambda *a: {},
            luna_approvals=lambda *a: {},
        ),
        parallel_analysis_limit=parallel_analysis_limit,
    )
    server.create_workspace_view("선택 자동 분석", str(tmp_path / "workspace"))
    server._selection_auto_approval = lambda quote: True
    return server, sender, client


def test_selection_runs_both_providers_and_publishes_progress_and_recommendations(
    tmp_path, monkeypatch
):
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    client = SuitabilityClient(pause_second=True)
    server, sender, client = make_server(tmp_path, monkeypatch, source, client)
    server.capcut_choose_view(server.current_signature())
    try:
        assert client.second_entered.wait(35), server._quick
        state = server._quick
        root = server.workspace_root()
        progress = server.raw.selection_suitability(root, state["revision"], state["asset_ids"])
        ranges = progress[state["asset_ids"][0]]
        assert len(ranges) == 3
        assert ranges[0]["end"] == {"num": 1, "den": 2}
        assert ranges[1]["end"] == {"num": 3, "den": 2}
        assert ranges[2]["recommendation"] == "review_required"
        assert ranges[2]["evaluated"] is False
        assert progress[state["asset_ids"][0]][0]["recommendation"] == "recommended"
    finally:
        client.resume.set()
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert len(sender.calls) == 1
    assert len(client.calls) == 3
    assert all(call["model"] == RawModelRoles().luna_model for call in client.calls)
    assert all(
        any(c["type"] == "input_image" for m in call["input"] for c in m["content"])
        for call in client.calls
    )
    head = server.raw.review_workspace_facade(root).load_head().revision
    assert {s.recommendation for s in head.segments} == {"review_required"}
    assert all(s.selection == "included" for s in head.segments)  # no scenic deletion
    assert not list(
        (root / "clips").glob("package_*")
    )  # showing results doesn't wait for extraction
    _, html = server.visual_workbench_render(root)
    assert "suggestions" in html and "안정된 구도" in html
    # Same selected source reuses a valid cache without another approval/provider call.
    ids = state["asset_ids"]
    server._start_quick(root, (), asset_ids=ids)
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert len(sender.calls) == 1 and len(client.calls) == 3


def test_unapproved_selection_prepares_preview_but_never_calls_providers(tmp_path, monkeypatch):
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server._selection_auto_approval = None
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("승인 대기", "분석 확인 필요"))
    assert server._quick["stage"] == "승인 대기", server._quick
    assert not sender.calls and not client.calls
    assert "한 번 승인" in server._quick_status()


def test_one_approval_continues_without_a_second_phase_button(tmp_path, monkeypatch):
    source = tmp_path / "one.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server._selection_auto_approval = None
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    server.quick_approve_view(server.current_signature(), None)
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert len(sender.calls) == 1 and len(client.calls) == 1


def test_initial_luna_request_ceiling_stops_semantic_calls(tmp_path, monkeypatch):
    from dataclasses import replace

    source = tmp_path / "bounded.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    original = server.raw.analysis_semantic_estimate
    monkeypatch.setattr(
        server.raw,
        "analysis_semantic_estimate",
        lambda *args, **kwargs: tuple(
            replace(e, luna_request_count=e.luna_request_count + 1)
            for e in original(*args, **kwargs)
        ),
    )
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "분석 확인 필요")
    assert "상한" in server._quick["error"]
    assert len(sender.calls) == 1 and not client.calls


def test_new_source_keeps_approved_analysis_and_return_reuses_state(tmp_path, monkeypatch):
    from tests.raw.test_review_workspace import _wait_terminal

    source, other = tmp_path / "old.mov", tmp_path / "new.mov"
    _make_media(source, audio=True, seconds="32")
    _make_media(other, audio=True, seconds="3")
    client = SuitabilityClient(pause_second=True)
    server, sender, client = make_server(tmp_path, monkeypatch, source, client)
    root = server.workspace_root()
    for path in (source, other):
        handle = server.raw.import_selected_video(root, path)
        assert _wait_terminal(server.raw, root, handle.job_id).status == "succeeded"
    server.raw.ensure_source_order(root)
    assets = {a.source_name: a.asset_id for a in server.raw.list_source_assets(root)}
    server._start_quick(root, (), asset_ids=(assets[source.name],))
    assert client.second_entered.wait(35), server._quick
    old = server._quick
    server._selection_auto_approval = None
    server.quick_source_view(server.current_signature(), assets[other.name])
    try:
        wait_until(lambda: server._quick.get("stage") == "승인 대기")
        assert not server.raw.get_job(root, old["job_id"]).cancellation_requested
        assert server._quick["asset_ids"] == (assets[other.name],)
        assert "백그라운드 분석 1개" in server.capcut_status_view(server.current_signature())
    finally:
        client.resume.set()
    assert _wait_terminal(server.raw, root, old["job_id"]).status == "succeeded"
    assert old["stage"] == "추천 구간 준비 완료"
    assert server._quick["asset_ids"] == (assets[other.name],)
    assert server._quick["stage"] == "승인 대기" and server._quick["revision"] is None
    calls = (len(sender.calls), len(client.calls))
    server.quick_source_view(server.current_signature(), assets[source.name])
    assert server._quick is old and server._quick["stage"] == "추천 구간 준비 완료"
    assert (len(sender.calls), len(client.calls)) == calls == (1, 3)
    states = server.raw.load_selection_states(root)
    assert {a for task in states for a in task["asset_ids"]} == set(assets.values())


def test_scenery_without_audio_still_gets_luna_recommendation(tmp_path, monkeypatch):
    source = tmp_path / "scenery.mov"
    _make_media(source, audio=False, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert not sender.calls and len(client.calls) == 1
    head = server.raw.review_workspace_facade(server.workspace_root()).load_head().revision
    assert head.segments[0].recommendation == "recommended"
    assert head.segments[0].selection == "included"


def test_quote_status_handles_measured_rational_audio_duration(tmp_path):
    server = RawWorkspaceUi(state_path=tmp_path / "state.json")
    estimate = SimpleNamespace(
        asr_seconds="64731667/1000000", asr_chunk_count=1, luna_request_count=5
    )
    server._quick = {
        "stage": "승인 대기",
        "quote": SimpleNamespace(asset_ids=("synthetic",), estimates=(estimate,)),
    }
    assert "65초" in server._quick_status()
    assert estimate.asr_seconds == "64731667/1000000"
