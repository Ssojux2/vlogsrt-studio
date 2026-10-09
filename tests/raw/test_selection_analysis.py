from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import pytest

from tests.raw.test_browser_runtime import _CountingSender, _make_media
from ui.workspace_service import RawWorkspaceUi
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport
from vlogsrt.project.raw.config import RawModelRoles
from vlogsrt.project.raw.segment_review import unreviewed_auto_exclusions
from vlogsrt.project.raw.service import RawWorkspaceError


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
        # 3단 등급(D51·D52) — Luna가 도는 동안은 자막 칸이 아직 없어 빨강이 없고(자막 전),
        # 판정 전 창은 보류다. 발화 끝(1.5초)+여유 0.3초까지가 발화 구역이다.
        assert len(ranges) == 3
        assert ranges[0]["end"] == {"num": 9, "den": 5}
        assert (ranges[0]["tier"], ranges[0]["recommendation"]) == ("green", "recommended")
        assert ranges[1]["end"] == {"num": 15, "den": 1}
        assert (ranges[1]["tier"], ranges[1]["audio"]["status"]) == ("yellow", "subtitle_pending")
        assert ranges[2]["recommendation"] == "review_required"
        assert (ranges[2]["tier"], ranges[2]["hold"]) == ("yellow", True)
        assert ranges[2]["video"]["status"] == "unanalyzed"
        assert all(r["tier"] != "red" for r in ranges)
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
    # 3단 등급(D51·D52): Luna의 확인 메모가 더는 전부를 '검토 필요'로 끌어내리지 않는다.
    # 합성 소리 트랙은 4초뿐이라(`_make_media`) 4–32초는 자막 공백이 아니라 소리가 없는 자리다 —
    # 무음 후보가 아니므로 제외 제안이 없고, 원본 하나가 한 조각('보통')으로 남는다.
    assert [
        (s.recommendation, [t for t in s.visual_tags if t.startswith("등급: ")])
        for s in head.segments
    ] == [("normal", ["등급: 보통"])]
    assert all("등급판 raw-tier/v2" in s.visual_tags for s in head.segments)
    assert all(s.selection == "included" for s in head.segments)  # no scenic deletion
    assert unreviewed_auto_exclusions(head) == ()
    final = server.raw.selection_suitability(root, state["revision"], state["asset_ids"])
    statuses = {r["audio"]["status"] for r in final[state["asset_ids"][0]]}
    assert statuses == {"speech_sparse", "unconfirmed_empty"}
    assert all(r["tier"] == "yellow" for r in final[state["asset_ids"][0]])
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


def _make_media_with_full_audio(path: Path, seconds: int) -> None:
    """영상 길이만큼 소리가 있는 합성 원본 — 자막 없는 긴 공백이 소리 범위 안에 놓인다.

    색 좌표를 명시해야 Final Cut 출력이 받는다(모르는 색 좌표는 거부한다).
    """
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:a", "aac", "-b:a", "128k",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


def test_analysis_completion_proposes_red_as_unreviewed_exclusion_and_export_waits(
    tmp_path, monkeypatch
):
    """분석 완료 경로(publish_review → initialize_review) 끝까지 — 빨강은 미검토 제외 제안이다.

    소리는 32초 내내 있고 발화는 0.5–1.5초뿐이라 1.8–32초가 자막 없는 긴 공백이다. Luna가 창 0은
    '추천'(풍경 → 노랑), 창 1은 '검토', 창 2는 '사용 가능'으로 본다 — 15–32초가 제외 제안이 되고,
    사람이 확인하기 전에는 Final Cut 출력이 열리지 않는다(D35·D52). 유료 호출 0회(가짜 제공자).
    """
    from tests.raw.test_review_workspace import _wait_terminal

    source = tmp_path / "long.mov"
    _make_media_with_full_audio(source, 32)
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert (len(sender.calls), len(client.calls)) == (1, 3)
    root = server.workspace_root()
    facade = server.raw.review_workspace_facade(root)
    head = facade.load_head().revision
    clock = head.clocks[0]

    def seconds(segment) -> tuple[Fraction, Fraction]:
        return tuple(
            (pts - clock.origin_pts) * clock.time_base
            for pts in (segment.start_pts, segment.end_pts)
        )

    pending = unreviewed_auto_exclusions(head)
    assert [seconds(s) for s in pending] == [(15, 30), (30, 32)]
    for segment in pending:
        assert segment.selection == "excluded" and "등급: 제외 제안" in segment.visual_tags
        provenance = json.loads(segment.notes.removeprefix("자동 제외: "))
        assert (provenance["reason"], provenance["version"]) == ("tier_silence", "raw-tier/v2")
        assert provenance["evidence"][:2] == ["tier:silent-not-scenery", "audio:subtitle_silence"]
        assert provenance["source_id"] == segment.source_id
        assert provenance["analysis_revision_id"] == server._quick["revision"]
    kept = [s for s in head.segments if s not in pending]
    assert [(seconds(s), s.selection) for s in kept] == [((0, 15), "included")]
    # 출력 관문 — 사람이 보기 전에는 영상에서 아무것도 지우지 않는다.
    with pytest.raises(gr.Error, match="자동 제외 2개를 먼저 확인"):
        server.capcut_handoff_view(server.current_signature())
    with pytest.raises(RawWorkspaceError, match="자동 제외 2개를 먼저 확인"):
        server.raw.export_final_cut(root)
    assert not [j for j in server.raw.list_jobs(root) if j.kind == "capcut_export"]
    facade.confirm_automatic_exclusions()
    server.capcut_handoff_view(server.current_signature())
    job = next(j for j in server.raw.list_jobs(root) if j.kind == "capcut_export")
    finished = _wait_terminal(server.raw, root, job.job_id)
    assert finished.status == "succeeded", finished.error
    manifest = json.loads((root / server._last_export_dir / "confirmed_manifest.json").read_text())
    assert [c["segment_id"] for c in manifest["clips"]] == [kept[0].segment_id]


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
