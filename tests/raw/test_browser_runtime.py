"""A4 생산 transport 갭 — 브라우저 승인 주조가 게이트를 통과한다(가짜 전송만).

브라우저 서버가 **자격증명 없이**는 실제 호출을 시작하지 못하고, 시작 환경
키로 만든 생산 transport가 화면 견적에서 주조한 승인으로 두 단계를 끝까지
흐르는지를 증명한다. 전송 경계는 전부 가짜(세기만 한다) — 유료 호출은 0회.

- 환경 키는 **시작할 때** 찍은 것뿐이다 — 이후 `.env` 적재로 문이 열리지 않는다.
- 승인 기록(견적 지문)이 없거나·날았거나·환경·네트워크·종료일이 닫혀 있으면
  전부 **0송신**으로 거부된다(§14.3).
- 주조된 승인은 게이트가 검증하는 값(출처·스트림·청크 예산·설정 지문·실측
  지문)을 그대로 묶는다 — 오디오 원본과 무성 원본이 각자의 모양으로.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import types
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import workspace_service  # noqa: E402
from vlogsrt.config import Config  # noqa: E402
from vlogsrt.project.raw import browser_runtime  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


# ── 환경 키·전송 경계 — 유료 호출 없이 ────────────────────────────────────────


def test_capture_environment_credentials_reads_process_environ_only(monkeypatch) -> None:
    """시작 환경에서만 찍는다 — 값·base_url 손질, 없으면 None(조용한 실패 아님)."""
    monkeypatch.setenv("OPENAI_API_KEY", "  sk-test-only  ")
    monkeypatch.setenv("OPENAI_BASE_URL", " https://example.test/v1 ")
    captured = browser_runtime.capture_environment_credentials()
    assert captured is not None
    assert captured.api_key == "sk-test-only"
    assert captured.base_url == "https://example.test/v1"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert browser_runtime.capture_environment_credentials() is None


def test_production_transport_refuses_missing_key_with_actionable_error() -> None:
    """키가 없으면 다음 행동을 알려주는 오류 — 모델 대체도 우회도 없다."""
    with pytest.raises(browser_runtime.BrowserRuntimeError, match="OPENAI_API_KEY") as info:
        browser_runtime.production_live_transport(
            asr_approvals=lambda *a: {}, luna_approvals=lambda *a: {}, environ={}
        )
    assert ".env" in str(info.value)  # 파일을 읽지 않는다는 사실도 안내한다


def test_production_transport_builds_canonical_boundaries() -> None:
    """키가 있으면 정본 경계(LiveTranscriber·Responses 클라이언트)로 만든다."""
    transport = browser_runtime.production_live_transport(
        asr_approvals=lambda *a: {},
        luna_approvals=lambda *a: {},
        environ={"OPENAI_API_KEY": "sk-test-only"},
    )
    assert type(transport.sender).__name__ == "LiveTranscriber"
    assert transport.cfg.api_max_retries >= 1  # 엔진 재시도 정책 그대로
    assert hasattr(transport.client, "responses")  # Luna가 쓰는 Responses 클라이언트


def test_estimate_digests_bind_measurements_not_notes() -> None:
    """견적 지문 — 측정값이 같으면 같고, 문구(note)는 묶지 않는다."""
    from vlogsrt.project.raw.models import AnalysisEstimate, AnalysisSemanticEstimate

    def estimate(**overrides):
        values = {
            "asset_id": "a1",
            "source_order": 1,
            "source_name": "제주.mov",
            "has_audio": True,
            "audio_stream_index": 1,
            "sample_count": 96000,
            "video_frame_count": 150,
            "extent_seconds": "6",
            "window_count": 1,
            "frame_sample_count": 8,
            "asr_chunk_count": 1,
            "asr_seconds": "6",
            "luna_request_count": 1,
            "price_status": "unknown",
            "user_ceiling_total": None,
            "note": "문구",
        }
        values.update(overrides)
        return AnalysisEstimate(**values)

    first = (estimate(),)
    same_note_changed = (estimate(note="다른 문구"),)
    drifted = (estimate(asr_chunk_count=2),)
    assert browser_runtime.asr_estimate_digest(first) == browser_runtime.asr_estimate_digest(
        same_note_changed
    )
    assert browser_runtime.asr_estimate_digest(first) != browser_runtime.asr_estimate_digest(
        drifted
    )

    def semantic(**overrides):
        values = {
            "asset_id": "a1",
            "source_order": 1,
            "source_name": "제주.mov",
            "phase": "semantic",
            "semantic_fingerprint": "f" * 64,
            "window_count": 1,
            "image_count": 8,
            "luna_model": "luna",
            "luna_request_count": 1,
            "price_status": "unknown",
            "user_ceiling_total": None,
            "note": "문구",
        }
        values.update(overrides)
        return AnalysisSemanticEstimate(**values)

    left = (semantic(),)
    assert browser_runtime.semantic_estimate_digest(
        left
    ) == browser_runtime.semantic_estimate_digest((semantic(note="다른 문구"),))
    assert browser_runtime.semantic_estimate_digest(
        left
    ) != browser_runtime.semantic_estimate_digest((semantic(window_count=2),))


# ── 브라우저 생산 흐름 — 가짜 전송 경계로 끝까지(승인은 화면이 주조한다) ────────


class _CountingSender:
    """전사 전송 경계 가짜 — 보낸 수만 센다(유료 호출 없음)."""

    def __init__(self, segments: list[dict[str, object]]) -> None:
        self.calls: list[object] = []
        self._segments = segments

    def send_request(self, request, cfg, *, kind, stem, source):
        self.calls.append(request)
        return {"segments": list(self._segments)}


class _CountingClient:
    """Responses 클라이언트 가짜 — 창 판정을 돌려주고 호출 수를 센다."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        body = {
            "window_id": "window_000000",
            "speech_summary": "도착해서 일정을 설명한다",
            "visual_summary": "실내에서 걷는다",
            "scene_tags": ["arrival"],
            "evidence_ids": ["window_000000"],
            "suggested_boundary_ids": [],
            "editing_notes": [],
            "needs_review": False,
            "uncertainties": [],
        }
        self._text = json.dumps(body, ensure_ascii=False)
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **payload):
        self.calls.append(str(payload.get("model")))
        return {"output_text": self._text}


def _make_media(path: Path, *, audio: bool, seconds: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate=25:duration={seconds}",
    ]
    if audio:
        command += [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)


def _prepare_workspace(server: workspace_service.RawWorkspaceUi, tmp_path: Path, card: Path):
    proposal = server.raw.scan_sources(root := _root_of(server, tmp_path), card)
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.raw.get_job(root, handle.job_id).status in TERMINAL:
            break
        time.sleep(0.02)
    assert server.raw.get_job(root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    return root


def _root_of(server: workspace_service.RawWorkspaceUi, tmp_path: Path) -> Path:
    return tmp_path / "ws"


def _opened(server: workspace_service.RawWorkspaceUi, tmp_path: Path) -> str:
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    return view[1]


def _wait_terminal(server: workspace_service.RawWorkspaceUi, root: Path, job_id: str):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if server.raw.get_job(root, job_id).status in TERMINAL:
            break
        time.sleep(0.05)
    return server.raw.get_job(root, job_id)


@needs_ffmpeg
def test_browser_production_flow_fake_sends(tmp_path: Path, monkeypatch) -> None:
    """화면 견적→확인란→1단계→2단계 견적→재개 — 주조된 승인이 게이트를 지난다."""
    from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport

    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    server = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    signature = _opened(server, tmp_path)
    card = tmp_path / "card" / "DCIM"
    _make_media(card / "스테레오.mov", audio=True, seconds="6")
    _make_media(card / "무성.mov", audio=False, seconds="3.5")
    root = _prepare_workspace(server, tmp_path, tmp_path / "card")
    config = server.raw.settings(root)
    server.raw.update_settings(root, replace(config, network_mode="online"))
    assets = {a.source_name: a for a in server.raw.list_source_assets(root)}

    # 1단계 견적(승인용) — 측정값 표가 서버에 기록으로 남는다.
    estimate_md = server.analysis_live_estimate_view(signature, None)
    assert "스테레오.mov" in estimate_md and "무성.mov" in estimate_md
    assert "청크 예산" in estimate_md and "종료 예정" in estimate_md
    assert server._asr_approval_record is not None

    # 주조 단위 확인 — 오디오 원본만 승인이 생기고, 측정값을 그대로 묶는다.
    minted = raw_service.mint_asr_approvals(
        server.raw, root, [asset.asset_id for asset in assets.values()]
    )
    assert set(minted) == {assets["스테레오.mov"].asset_id}  # 무성은 승인할 전사가 없다
    stereo = minted[assets["스테레오.mov"].asset_id]
    assert stereo.approved and stereo.chunk_budget >= 1
    assert stereo.source_sha256 == assets["스테레오.mov"].content_sha256

    # 생산 transport의 승인 공급자(화면 주조)를 가짜 전송 경계와 잇는다.
    sender = _CountingSender([{"start": 0.5, "end": 2.0, "text": "도착해서 일정 설명"}])
    client = _CountingClient()
    server._live_transport = LiveAnalysisTransport(
        sender=sender,
        cfg=Config(),
        client=client,
        asr_approvals=server._production_asr_approvals,
        luna_approvals=server._production_luna_approvals,
    )

    # 1단계(전사) — Luna 0호출로 의미 승인 대기에 멈춘다.
    message, _ = server.analysis_live_asr_view(signature, None, True)
    job_id = message.split("`")[1]
    record = _wait_terminal(server, root, job_id)
    assert record.status == "succeeded", record.error
    assert len(sender.calls) == 1  # 오디오 원본 청크 1 — 무성은 전송 자체가 없다
    assert client.calls == []
    summary = server.raw.analysis_overview(root)
    assert summary is not None and summary.revision.status == "awaiting_semantic"

    # 2단계 견적 — 실측 지문 표가 기록으로 남는다(오디오=의미, 무성=시각).
    semantic_md = server.analysis_semantic_estimate_view(signature)
    assert "의미(전사+이미지)" in semantic_md and "시각(이미지만)" in semantic_md
    assert server._semantic_approval_record is not None
    choices = server.resumable_analysis_choices(root)
    assert any(job_id == value for _label, value in choices)

    # 2단계 재개 — 주조된 승인(오디오·시각 각각)이 게이트를 지나 fake 전송만.
    resume_message, _ = server.analysis_semantic_resume_view(signature, job_id, True)
    semantic_job = resume_message.split("`")[1]
    resumed = _wait_terminal(server, root, semantic_job)
    assert resumed.status == "succeeded", resumed.error
    assert len(sender.calls) == 1  # 전사 재호출 없음
    assert len(client.calls) == 2  # 창 1 + 창 1(시각 Luna 포함)
    final = server.raw.analysis_overview(root)
    assert final is not None and final.revision.status == "complete"

    silent = assets["무성.mov"]
    silent_doc = server.raw.analysis_document(root, final.revision.revision_id, silent.asset_id)
    assert silent_doc is not None
    assert silent_doc["audio"]["status"] == "audio_absent"  # 없는 오디오를 지어내지 않는다
    assert silent_doc["models"]["asr"] is None
    assert silent_doc["execution_mode"] == "online_live"


@needs_ffmpeg
def test_browser_production_refusals_are_zero_send(tmp_path: Path, monkeypatch) -> None:
    """확인란·기록·네트워크·환경·종료일·견적 드리프트 — 전부 0송신으로 닫힌다."""
    from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport

    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    server = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    signature = _opened(server, tmp_path)
    card = tmp_path / "card" / "DCIM"
    _make_media(card / "제주.mov", audio=True, seconds="6")
    root = _prepare_workspace(server, tmp_path, tmp_path / "card")

    sender = _CountingSender([])
    client = _CountingClient()
    server._live_transport = LiveAnalysisTransport(
        sender=sender,
        cfg=Config(),
        client=client,
        asr_approvals=server._production_asr_approvals,
        luna_approvals=server._production_luna_approvals,
        on=date(2026, 10, 2),
    )

    # 확인란 없이는 시작하지 않는다.
    with pytest.raises(gr.Error, match="승인 확인이 필요하다"):
        server.analysis_live_asr_view(signature, None, False)
    # 견적 기록 없이는 시작하지 않는다.
    with pytest.raises(gr.Error, match="승인 기록이 없다"):
        server.analysis_live_asr_view(signature, None, True)
    assert sender.calls == []

    # 견적을 남기고 — 네트워크가 닫혀 있으면 0송신.
    server.analysis_live_estimate_view(signature, None)
    config = server.raw.settings(root)
    server.raw.update_settings(root, replace(config, network_mode="offline"))
    with pytest.raises(gr.Error, match="network_mode"):
        server.analysis_live_asr_view(signature, None, True)
    assert sender.calls == []

    # 네트워크를 열어도 환경이 닫혀 있으면 0송신(설정이 환경을 우회하지 않는다).
    config = server.raw.settings(root)
    server.raw.update_settings(root, replace(config, network_mode="online"))
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: True)
    with pytest.raises(gr.Error, match="오프라인"):
        server.analysis_live_asr_view(signature, None, True)
    assert sender.calls == []
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)

    # 종료 예정일 이후 — 수명주기 문이 먼저 닫힌다(§11.10).
    stale_transport = server._live_transport
    server._live_transport = LiveAnalysisTransport(
        sender=sender,
        cfg=Config(),
        client=client,
        asr_approvals=server._production_asr_approvals,
        luna_approvals=server._production_luna_approvals,
        on=date(2027, 3, 1),
    )
    with pytest.raises(gr.Error, match="종료 예정일"):
        server.analysis_live_asr_view(signature, None, True)
    assert sender.calls == []
    server._live_transport = stale_transport

    # 견적 드리프트 — 승인 때 본 값과 지금 값이 다르면 0송신으로 거부된다.
    record = server._asr_approval_record
    assert record is not None
    drifted = tuple(
        replace(item, asr_chunk_count=item.asr_chunk_count + 1) for item in record.estimates
    )
    server._asr_approval_record = replace(record, estimates=drifted)
    with pytest.raises(gr.Error, match="다르다"):
        server.analysis_live_asr_view(signature, None, True)
    assert sender.calls == []

    # 네트워크 모드 화면 — 변경은 되지만 환경 우회 안내를 남긴다.
    note = server.settings_network_view(signature, "offline")
    assert "offline" in note
    online_note = server.settings_network_view(signature, "online")
    assert "VLOGSRT_OFFLINE" in online_note  # 환경 스위치는 사람의 손으로만
    assert sender.calls == [] and client.calls == []
