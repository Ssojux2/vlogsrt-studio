"""W-39b(D99) — 라이브러리 맥락 평가: 견적 → 승인 → 실행 → 저장 → 등급 반영 (가짜 Gemini).

시험이 못 박는 것:

- 견적은 무료다 — 원본 수 · 구간 수 · 입력 토큰(보수적) · 출력 한도 · 공식 단가 상한 · digest.
- 실행은 사람이 본 digest와 지금 견적이 같을 때만, 온라인(환경 + 작업공간)일 때만 보낸다.
- 성공하면 `<열쇠>.json`을 쓰고 그 판정이 등급 값(`_tier_context`)으로 등급 보고서의 구간에 닿는다.
  같은 열쇠는 다시 부르지 않는다. (판정이 등급을 어떻게 바꾸는지는 `test_context_review`가 본다.)
- 입력이 바뀌면(모델) 평가가 낡아 등급이 원래대로 돌아오고 상태가 「낡음」이다.
- 실패(잘린 응답)는 까닭과 함께 남고 등급은 그대로다. 가격 미정이면 승인할 수 없다.

유료 호출은 없다 — 전송 자리에 가짜 opener가 앉는다.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_analysis_workflow import _live_online
from tests.raw.test_gemini_timing_provider import _client, _Opener
from vlogsrt.project.raw import context_review as cr
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.analysis_config import CONTEXT_REVIEW

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def _reply(body: dict[str, Any], finish: str = "STOP") -> bytes:
    return json.dumps(
        {
            "candidates": [
                {"finishReason": finish, "content": {"parts": [{"text": json.dumps(body)}]}}
            ],
            "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 300},
            "modelVersion": "gemini-3.8-flash-001",
        }
    ).encode("utf-8")


def _all_redundant(service: raw_service.RawWorkspaceService, root: Path) -> dict[str, Any]:
    given = cr.build_input(service._context_sources(root), CONTEXT_REVIEW)
    return {
        "storyline_summary": "카페에서 여행을 시작한다.",
        "segments": [
            {
                "span_id": span_id,
                "verdict": "redundant",
                "confidence": "high",
                "reason": "같은 장면",
                "duplicate_of": None,
            }
            for span_id in given.spans
        ],
    }


def _signals(service: raw_service.RawWorkspaceService, root: Path) -> dict[str, Any]:
    """원본마다 등급 보고서의 구간 (시작, 끝) → 그 구간에 닿은 맥락 판정."""
    workspace = service._workspace(root)
    rows = service.review_workspace_facade(root).tier_evidence_rows()
    found: dict[str, Any] = {}
    for asset_id, row in rows.items():
        report = service.tier_report(root, row.revision_id, asset_id)
        verdicts = service._tier_context(workspace, asset_id).verdicts
        found[asset_id] = {(s.start, s.end): verdicts.get((s.start, s.end)) for s in report.spans}
    return found


def _tiers(service: raw_service.RawWorkspaceService, root: Path) -> list[str]:
    rows = service.review_workspace_facade(root).tier_evidence_rows()
    tiers = []
    for asset_id, row in rows.items():
        report = service.tier_report(root, row.revision_id, asset_id)
        tiers += [span.tier for span in report.spans]
    return tiers


@pytest.fixture
def project(tmp_path: Path) -> tuple[raw_service.RawWorkspaceService, Path]:
    from tests.raw.test_auto_cleanup_review_gate import _synthetic_project

    service, root = _synthetic_project(tmp_path)
    workflow = service.settings(root)
    models = replace(workflow.models, luna_model="gemini-3.8-flash")
    service.update_settings(root, replace(workflow, models=models))
    return service, root


@needs_ffmpeg
def test_one_run_lowers_greens_and_never_rebuys(
    project: tuple[raw_service.RawWorkspaceService, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = project
    _live_online(service, root, monkeypatch)
    before = _tiers(service, root)
    assert all(v is None for row in _signals(service, root).values() for v in row.values())
    assert service.context_review_model(root) == "gemini-3.8-flash"
    opener = _Opener(_reply(_all_redundant(service, root)))
    document = service.run_context_review(root, _client(opener))
    assert len(opener.sent) == 1 and document["storyline"] == "카페에서 여행을 시작한다."
    assert opener.sent[0]["generationConfig"]["responseJsonSchema"] == cr.RESPONSE_SCHEMA
    reached = [v for row in _signals(service, root).values() for v in row.values()]
    assert reached and all(v is not None and v.verdict == "redundant" for v in reached)
    assert len(_tiers(service, root)) == len(before)  # 경계는 그대로
    again = service.run_context_review(root, _client(opener))
    assert len(opener.sent) == 1 and again["key"] == document["key"]  # 다시 사지 않는다
    state = service.context_review_state(root)
    assert state["current"] is not None and state["stale"] is False


@needs_ffmpeg
def test_a_changed_input_makes_the_review_stale_and_restores_the_tiers(
    project: tuple[raw_service.RawWorkspaceService, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = project
    _live_online(service, root, monkeypatch)
    before = _tiers(service, root)
    opener = _Opener(_reply(_all_redundant(service, root)))
    service.run_context_review(root, _client(opener))
    workflow = service.settings(root)
    models = replace(workflow.models, luna_model="gemini-4-argon")
    service.update_settings(root, replace(workflow, models=models))  # 열쇠의 모델이 바뀐다
    assert _tiers(service, root) == before
    assert all(v is None for row in _signals(service, root).values() for v in row.values())
    state = service.context_review_state(root)
    assert state["current"] is None and state["stale"] is True


@needs_ffmpeg
def test_a_cut_reply_is_recorded_and_changes_nothing(
    project: tuple[raw_service.RawWorkspaceService, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = project
    _live_online(service, root, monkeypatch)
    before = _tiers(service, root)
    opener = _Opener(_reply(_all_redundant(service, root), finish="MAX_TOKENS"))
    with pytest.raises(raw_service.AnalysisError, match="맥락 평가가 실패했습니다"):
        service.run_context_review(root, _client(opener))
    assert _tiers(service, root) == before
    assert "출력 한도" in service.context_review_state(root)["failure"]


@needs_ffmpeg
def test_offline_runs_send_nothing(
    project: tuple[raw_service.RawWorkspaceService, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = project
    opener = _Opener(b"{}")
    with pytest.raises(raw_service.AnalysisError):
        service.run_context_review(root, _client(opener))
    assert opener.sent == []


# ── 화면(W-39c) ─────────────────────────────────────────────────────────


def test_the_drawer_panel_says_what_it_does_and_runs_without_a_quote(tmp_path: Path) -> None:
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    import gradio as gr

    from ui import workspace_service as ws

    server = ws.RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("맥락", str(tmp_path / "workspace"))
    assert server.context_review_status() == ws.CONTEXT_NONE_TEXT
    with pytest.raises(gr.Error, match="맥락을 평가할 분석이 없습니다"):
        server.context_run_view(server.current_signature())
    assert not hasattr(server, "context_quote_view") and ws.CONTEXT_RUN_LABEL == "맥락 평가 실행"


def test_an_expired_gemini_price_refuses_context_and_english_before_any_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D110 — 견적이 없어도 단가 재확인 기한이 지난 Gemini는 보내기 전 문(`semantic_client`)이
    닫는다.

    맥락 평가와 영어 자막 모두 클라이언트를 받기 전에 거절하고, 서비스의 유료 길은 한 번도 불리지
    않는다."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from datetime import date

    import gradio as gr

    from ui import workspace_service as ws
    from vlogsrt.config import Config
    from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport

    flash = "gemini-3.8-flash"
    server = ws.RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("단가", str(tmp_path / "workspace"))
    signature = server.current_signature()
    server._live_transport = LiveAnalysisTransport(
        sender=object(),
        cfg=Config(),
        client=object(),
        asr_approvals=server._production_asr_approvals,
        luna_approvals=server._production_luna_approvals,
        gemini_clients={flash: object()},
        on=date(2030, 1, 1),  # 모든 Gemini 단가의 재확인 기한 뒤
    )
    paid: list[str] = []
    monkeypatch.setattr(server.raw, "context_review_model", lambda root: flash)
    monkeypatch.setattr(server.raw, "run_context_review", lambda *a: paid.append("context"))
    monkeypatch.setattr(server.raw, "srt_translation_cached", lambda root, srt: False)
    monkeypatch.setattr(server.raw, "start_srt_translation", lambda *a: paid.append("english"))
    srt = tmp_path / "draft.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n안녕\n", encoding="utf-8")
    with pytest.raises(gr.Error, match="가격 미정"):
        server.context_run_view(signature)
    with pytest.raises(gr.Error, match="가격 미정"):
        server.srt_english_view(signature, str(srt))
    assert paid == []
