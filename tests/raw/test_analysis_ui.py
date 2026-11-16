"""화면 2의 A4 묶음 — 분석 견적·오프라인 재생 분석·결과 읽기가 서명 아래에서만.

가벼운 경로(서명·빈 상태·견적 거절)는 ffmpeg 없이, 실제 흐름 한 번은 진짜
합성 미디어로: 조사 → 확정 → 전체 순서 → 견적 → **오프라인 재생 분석** →
새로고침 → 요약·태그 읽기 → 분석 결과로 분할 계획까지 화면 처리기로 지나간다
(§12 화면 2, §14.1·§14.3 — 브라우저는 실제 호출을 만들 수 없다).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import types
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from tests.raw.gemini_echo import GeminiEcho
from tests.raw.test_selection_analysis import keep_luna  # noqa: E402
from ui import workspace_service  # noqa: E402
from vlogsrt.config import Config  # noqa: E402

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@pytest.fixture
def server(tmp_path: Path) -> workspace_service.RawWorkspaceUi:
    return workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")


def _opened(server: workspace_service.RawWorkspaceUi, tmp_path: Path) -> tuple[str, Path]:
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    return view[1], Path(view[0].split("`")[1])


# ── 경로 검사 — 서명 없이·옛 서명으로는 아무 것도 움직이지 않는다 ─────────────


def test_analysis_views_require_current_selection(server, tmp_path: Path) -> None:
    signature, _ = _opened(server, tmp_path)
    for call in (
        lambda: server.analysis_start_view(None, None),
        lambda: server.analysis_start_view("stale-signature", None),
        lambda: server.analysis_refresh_view(None),
        lambda: server.analysis_retry_view(None, "job_000001"),
        lambda: server.analysis_plan_view("stale-signature"),
        lambda: server.analysis_live_asr_view("stale-signature", None, True),
        lambda: server.analysis_semantic_resume_view("stale-signature", "job_000001", True),
        lambda: server.settings_network_view("stale-signature", "online"),
    ):
        with pytest.raises(gr.Error):
            call()


def test_live_production_requires_startup_environment_key(server, tmp_path, monkeypatch) -> None:
    """시작 환경에 키가 없는 서버 — 실제 호출은 행동 가능한 오류로 닫힌다(§14.3).

    생산 transport는 게이트가 `.env`를 적재하기 **전에** 찍어 둔 시작 환경 키만
    쓴다 — 이 시험 프로세스 환경에서 키를 지운 채 서버를 만들면 .env 파일이
    있어도 문이 열리지 않는다.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    boot = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    assert boot._startup_credentials is None
    signature, _root = _opened(boot, tmp_path)
    # 확인란 없이는 승인 자체가 성립하지 않는다.
    with pytest.raises(gr.Error, match="승인 확인이 필요하다"):
        boot.analysis_live_asr_view(signature, None, False)
    with pytest.raises(gr.Error, match="승인 확인이 필요하다"):
        boot.analysis_semantic_resume_view(signature, "job_000001", False)
    for call in (
        lambda: boot.analysis_live_asr_view(signature, None, True),
        lambda: boot.analysis_semantic_resume_view(signature, "job_000001", True),
    ):
        with pytest.raises(gr.Error, match="OPENAI_API_KEY"):
            call()
    assert not hasattr(boot, "analysis_semantic_estimate_view")  # 견적 표가 없다(D110)


def test_startup_environment_key_opens_production_transport(tmp_path, monkeypatch) -> None:
    """시작 환경 키가 있으면 생산 transport가 정본 경계로 만들어진다(호출 0회)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    boot = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    assert boot._startup_credentials is not None
    transport = boot._live_transport_for_action()
    assert type(transport.sender).__name__ == "LiveRequestCarrier"  # 오디오 전송 없음(D163)
    assert transport.asr_approvals == boot._production_asr_approvals
    assert transport.luna_approvals == boot._production_luna_approvals


def test_analysis_views_before_order_confirmed_explain_honestly(server, tmp_path: Path) -> None:
    """확정 순서가 없으면 안내로 알린다 — 빈 성공으로 위장하지 않는다."""
    signature, root = _opened(server, tmp_path)
    with pytest.raises(gr.Error, match="전체 원본 순서가 없다"):
        server.analysis_start_view(signature, None)
    markdown = server._analysis_state_markdown(root)
    assert "오프라인 재생" in markdown  # 모드 표기가 처음부터 정직하다
    empty = server._analysis_state_markdown(None)
    assert "오프라인 재생" in empty and "승인" in empty


def test_analysis_settings_summary_shows_frozen_defaults(server, tmp_path: Path) -> None:
    """동결 기본값(15초·2초·8장·768)과 모델이 설정 요약에 보인다(§7.4)."""
    signature, _ = _opened(server, tmp_path)
    markdown = server._analysis_settings_markdown(None)
    assert "15초" in markdown and "2초" in markdown and "8장" in markdown and "768" in markdown


# ── 실제 흐름 — 견적 → 오프라인 재생 분석 → 읽기 → 분할 계획 ─────────────────


def _make_media(path: Path, seconds: str = "6") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
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
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@needs_ffmpeg
def test_browser_replay_analysis_flow_to_segment_plan(server, tmp_path: Path) -> None:
    """견적 → 재생 분석 → 상태·요약 읽기 → 분석 결과로 분할 계획까지(§12 화면 2)."""
    signature, root = _opened(server, tmp_path)
    keep_luna(server)  # Luna 재생 픽스처 — W-18 이전 작업공간처럼 gpt-6-luna를 저장한다(D58)
    card = tmp_path / "card" / "DCIM"
    _make_media(card / "제주.mov")
    proposal = server.raw.scan_sources(root, tmp_path / "card")
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.raw.get_job(root, handle.job_id).status in TERMINAL:
            break
        time.sleep(0.02)
    assert server.raw.get_job(root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    asset = server.raw.list_source_assets(root)[0]

    # 명시적 합성 픽스처 — 없는 창·청크는 실패로 기록된다(지어내지 않는다).
    fixtures = root / "analysis" / "replay"
    fixtures.mkdir(parents=True)
    (fixtures / "asr-fixtures.json").write_text(
        json.dumps({"sources": {asset.content_sha256 or "": []}}), encoding="utf-8"
    )
    (fixtures / "luna-fixtures.json").write_text(
        json.dumps(
            {
                "windows": {
                    "window_000000": {
                        "window_id": "window_000000",
                        "speech_summary": "",
                        "visual_summary": "실내 풍경이 이어진다",
                        "scene_tags": ["indoor"],
                        "evidence_ids": ["window_000000"],
                        "suggested_boundary_ids": [],
                        "editing_notes": [],
                        "needs_review": False,
                        "uncertainties": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    message, _ = server.analysis_start_view(signature, None)
    assert "오프라인 재생" in message and "지어내지 않는다" in message
    job_id = message.split("`")[1]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if server.raw.get_job(root, job_id).status in TERMINAL:
            break
        time.sleep(0.05)
    assert server.raw.get_job(root, job_id).status == "succeeded"

    markdown, _jobs, sources = server.analysis_refresh_view(signature)
    assert "의미 분석" in markdown
    assert "오프라인 재생" in markdown  # 실행 모드 표기
    assert "발화" in markdown  # 원본별 상태 표
    assert sources.choices  # 원본 선택 목록이 찬다
    assert "실내 풍경이 이어진다" in markdown  # 요약 읽기(accepted 판정)

    segment_md = server.analysis_plan_view(signature)
    assert "semantic_analysis" in segment_md  # 분석 근거의 분할 계획이 패널에 떴다

    # 재시도 목록 — 성공한 작업은 재시도 대상이 아니다.
    assert server.resumable_analysis_choices(root) == []


# ── 실제 호출 두 단계 — 주입된 전송 경계로 화면이 끝까지 흐른다(A4-F1) ──────────


class _CountingSender(GeminiEcho):
    """시간 엔진 가짜(Gemini, D163) — 화면 흐름 시험용(유료 호출 없음)."""


class _CountingClient:
    """Responses 클라이언트 가짜 — 창 판정을 돌려주고 호출 수를 센다."""

    def __init__(self, window_id: str) -> None:
        self.calls = 0
        body = {
            "window_id": window_id,
            "speech_summary": "도착해서 일정을 설명한다",
            "visual_summary": "실내에서 걷는다",
            "scene_tags": ["arrival"],
            "evidence_ids": [window_id],
            "suggested_boundary_ids": [],
            "editing_notes": [],
            "needs_review": False,
            "uncertainties": [],
        }
        self._text = json.dumps(body, ensure_ascii=False)
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **payload):
        self.calls += 1
        return {"output_text": self._text}


@needs_ffmpeg
def test_browser_two_phase_live_flow_with_injected_transport(tmp_path, monkeypatch) -> None:
    """1단계 → 의미 승인 대기 → 2단계 재개까지 화면 처리기로(견적 없이, D110)."""
    from dataclasses import replace as _replace
    from datetime import date

    from vlogsrt import preprocess as _preprocess
    from vlogsrt.project.raw.analysis_approval import (
        LiveAnalysisTransport,
        asr_settings_fingerprint,
    )
    from vlogsrt.project.raw.understanding_luna import (
        AnalysisBudget,
        OnlineAnalysisApproval,
    )

    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    server = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    signature, root = view[1], Path(view[0].split("`")[1])
    keep_luna(server)  # OpenAI 가짜 Luna 클라이언트 — 기존 Luna 작업공간에서 출발한다(D58)
    card = tmp_path / "card" / "DCIM"
    _make_media(card / "제주.mov")
    proposal = server.raw.scan_sources(root, tmp_path / "card")
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.raw.get_job(root, handle.job_id).status in TERMINAL:
            break
        time.sleep(0.02)
    assert server.raw.get_job(root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    config = server.raw.settings(root)
    server.raw.update_settings(root, _replace(config, network_mode="online"))
    asset = server.raw.list_source_assets(root)[0]
    server.raw.set_new_hearing_engine(root, "gemini")  # 시간 엔진은 Gemini 하나다(D163)
    workflow = server.raw.source_settings(root, asset.asset_id)
    probe = _preprocess.probe_raw_media(root / asset.relative_path)

    sender = _CountingSender([{"start": 0.5, "end": 2.0, "text": "도착해서 일정 설명"}])
    client = _CountingClient("window_000000")
    cfg = Config()  # 실전송 어댑터가 엔진 1차 온도를 읽는다(D55)

    def asr_approvals(ws_root: Path, selected):
        from vlogsrt.project.raw.analysis_approval import AsrPhaseApproval
        from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings

        return {
            asset.asset_id: AsrPhaseApproval(
                approved=True,
                source_sha256=asset.content_sha256 or "",
                audio_stream_index=probe.audio[0].stream_index,
                chunk_budget=1,
                settings_fingerprint=asr_settings_fingerprint(RawUnderstandingSettings(), workflow),
                timing_model=workflow.models.timing_model,
                approved_on=date.today().isoformat(),
            )
        }

    def luna_approvals(ws_root: Path, revision_id: str, selected):
        estimates = server.raw.analysis_semantic_estimate(ws_root, revision_id)
        summary = server.raw.analysis_overview(ws_root, revision_id)
        assert summary is not None
        row = next(item for item in summary.results if item.asset_id == asset.asset_id)
        artifact = json.loads((ws_root / row.document_path).read_text(encoding="utf-8"))
        by_asset = {item.asset_id: item for item in estimates}
        return {
            asset.asset_id: OnlineAnalysisApproval(
                approved=True,
                source_sha256=asset.content_sha256 or "",
                audio_stream_index=artifact["audio"]["audio_stream_index"],
                sample_count=artifact["audio"]["sample_count"],
                window_count=by_asset[asset.asset_id].window_count,
                fingerprint=by_asset[asset.asset_id].semantic_fingerprint,
                asr_model=workflow.models.timing_model,
                luna_model=workflow.models.luna_model,
                budget=AnalysisBudget(max_luna_requests=1, max_asr_chunks=1),
            )
        }

    live_server = workspace_service.RawWorkspaceUi(
        port=7860,
        state_path=tmp_path / "state.json",
        live=LiveAnalysisTransport(
            sender=sender,
            cfg=cfg,
            client=client,
            asr_approvals=asr_approvals,
            luna_approvals=luna_approvals,
            timing_client=sender.client(),
        ),
    )
    live_server.open_workspace_view(str(root))  # 같은 작업공간을 공개 경로로 연다

    # 1단계(전사) — Luna 0호출로 의미 승인 대기에 멈춘다.
    message, _ = live_server.analysis_live_asr_view(signature, None, True)
    assert "1단계" in message and "Luna 0호출" in message
    job_id = message.split("`")[1]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if live_server.raw.get_job(root, job_id).status in TERMINAL:
            break
        time.sleep(0.05)
    assert live_server.raw.get_job(root, job_id).status == "succeeded"
    assert len(sender.calls) == 1 and client.calls == 0

    waiting = live_server._analysis_state_markdown(root)
    assert "의미 승인 대기" in waiting
    assert "전사 완료(2단계 대기)" in waiting

    # 2단계 재개 — 대기 리비전의 1단계 작업이 재개 대상 목록에 있다.
    choices = live_server.resumable_analysis_choices(root)
    assert any(job_id == value for _label, value in choices)
    resume_message, _ = live_server.analysis_semantic_resume_view(signature, job_id, True)
    assert "2단계" in resume_message and "ASR 0호출" in resume_message
    semantic_job = resume_message.split("`")[1]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if live_server.raw.get_job(root, semantic_job).status in TERMINAL:
            break
        time.sleep(0.05)
    assert live_server.raw.get_job(root, semantic_job).status == "succeeded"
    assert len(sender.calls) == 1  # 전사 재호출 없음
    assert client.calls == 1  # 실제(가짜) Luna 호출 1회

    done_md, _jobs, _sources = live_server.analysis_refresh_view(signature)
    assert "실제 호출" in done_md  # 실행 증거 표기
    assert "분석 완료" in done_md
