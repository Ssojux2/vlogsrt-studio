"""W-14 / D54 — Claude Opus 5.5를 고른 작업공간의 선택 분석이 처음부터 끝까지 돈다(유료 호출 0회).

가짜 opener가 Messages API 자리를 대신한다 — 밖으로 나가는 소켓은 conftest가 막는다. 전사는 한 번만
산다: 의미 분석 모델을 바꿔도 전사 캐시·원본 자막·전사 지문이 그대로이고, 되돌리면 옛 Luna 분석이
호출 0회로 다시 쓰인다. Claude 영수증은 공식 단가로 값이 매겨진다. 같은 판정이면 Claude 문서도 Luna
문서와 똑같은 3단 등급을 낸다(D52 — 등급은 문서 모양만 읽고 모델 이름을 보지 않는다).

W-18(D58)부터 새 작업공간의 기본은 Claude Sonnet 5.5다. Luna → Claude 전환을 보는 시험은 W-18
이전에 만든 작업공간처럼 Luna로 시작한다(`make_server(model=LUNA)`). 기본 그대로 연 작업공간은
고르지 않아도 Sonnet 5.5로 분석하고 Sonnet 단가로 값이 매겨진다.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw.test_browser_runtime import _CountingSender, _make_media
from tests.raw.test_selection_analysis import keep_whisper_only, wait_until
from tests.raw.test_tiering import document as tier_document
from ui.workspace_service import RawWorkspaceUi
from vlogsrt.anthropic_messages import ClaudeMessagesClient
from vlogsrt.config import Config
from vlogsrt.project.raw import source_transcript, tiering
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.claude_cost import window_ceiling_usd
from vlogsrt.project.raw.config import (
    CLAUDE_ANALYSIS_PROFILES,
    OPENAI_LUNA_MODEL,
    claude_profile,
)
from vlogsrt.project.raw.service import RawWorkspaceError
from vlogsrt.project.raw.understanding_luna import luna_output_schema_bytes

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

CLAUDE = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
LUNA = OPENAI_LUNA_MODEL
DONE = "추천 구간 준비 완료"
SETTLED = (DONE, "분석 확인 필요", "부분 완료 · 확인 필요")
USAGE = {
    "input_tokens": 5000,
    "output_tokens": 1200,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
"""가짜 Claude 응답의 사용량 — Opus 공식 단가로 (5000×$4 + 1200×$20)/100만 = $0.044,
Sonnet 공식 단가로 (5000×$2 + 1200×$10)/100만 = $0.022."""
VISUAL_ISSUES = (["none"], ["shake", "dark"], ["blur", "none"])
"""창마다의 화면 문제(18b, D58) — 셋째는 `none`이 섞여 저장 때 빠진다."""


def judgment(text: str) -> dict[str, Any]:
    """창 하나의 판정 — 두 공급자가 같은 지시문에 같은 판정을 낸다."""
    window = re.search(r"window_id: (\S+)", text)
    frames = re.search(r"프레임 증거\(순서대로\): ([^\n]+)", text)
    assert window is not None and frames is not None, text
    index = int(window.group(1).rsplit("_", 1)[1])
    return {
        "window_id": window.group(1),
        "speech_summary": "합성 설명",
        "visual_summary": "합성 풍경",
        "scene_tags": ["scenery"],
        "evidence_ids": frames.group(1).split(", ")[:1],
        "suggested_boundary_ids": [],
        "editing_notes": [],
        "needs_review": index == 1,
        "uncertainties": [],
        "suitability": ["recommended", "review", "usable"][min(index, 2)],
        "suitability_reason": ["안정된 구도", "움직임 확인", "연결 장면"][min(index, 2)],
        "visual_issues": VISUAL_ISSUES[min(index, 2)],
    }


class LunaClient:
    """Responses 자리의 가짜 — 판정을 돌려주고 호출을 기록한다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **payload: Any) -> dict[str, str]:
        self.calls.append(payload)
        text = "\n".join(
            c["text"] for m in payload["input"] for c in m["content"] if c["type"] == "input_text"
        )
        return {"output_text": json.dumps(judgment(text), ensure_ascii=False)}


class _Reply:
    def __init__(self, body: dict[str, Any], request_id: str) -> None:
        self.status = 200
        self.headers = {"request-id": request_id}
        self._raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class ClaudeOpener:
    """Messages API 전송 자리의 가짜 — 실제 요청 객체를 받아 본문을 기록한다(소켓 없음)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        body = json.loads(request.data)
        self.calls.append((request, body))
        text = "\n".join(
            block["text"]
            for message in body["messages"]
            for block in message["content"]
            if block["type"] == "text"
        )
        reply = {
            "id": f"msg_flow_{len(self.calls)}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],  # 응답은 요청한 모델을 되울린다
            "content": [
                {"type": "thinking", "thinking": "", "signature": "synthetic"},
                {"type": "text", "text": json.dumps(judgment(text), ensure_ascii=False)},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": dict(USAGE),
        }
        return _Reply(reply, f"req_flow_{len(self.calls)}")


def claude_client(opener: ClaudeOpener, model: str = CLAUDE) -> ClaudeMessagesClient:
    profile = claude_profile(model)
    return ClaudeMessagesClient(
        api_key="sk-ant-synthetic-flow",
        base_url=profile.api_base_url,
        api_version=profile.api_version,
        timeout_seconds=profile.timeout_seconds,
        opener=opener,
    )


def claude_clients(opener: ClaudeOpener | None) -> dict[str, ClaudeMessagesClient]:
    """등록된 Claude 모델마다 그 프로파일로 만든 클라이언트 — 생산 transport와 같은 모양."""
    if opener is None:
        return {}
    return {model: claude_client(opener, model) for model in CLAUDE_ANALYSIS_PROFILES}


def make_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    *,
    opener: ClaudeOpener | None,
    auto_approve: bool = True,
    model: str | None = LUNA,
    gemini_clients: dict[str, Any] | None = None,
) -> tuple[RawWorkspaceUi, _CountingSender, LunaClient]:
    """가짜 전송의 작업대 — `model`이 있으면 그 모델로 저장된 기존 작업공간처럼 시작한다.

    기본은 W-18 이전에 만든 Luna 작업공간이다. `model=None`은 새 작업공간의 기본(Gemini 3.8 Flash,
    D87) 그대로다 — 그때는 `gemini_clients`(가짜 전송을 단 Gemini 클라이언트)를 함께 준다.
    """
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    sender = _CountingSender([{"start": 0.5, "end": 1.5, "text": "합성 영상입니다."}])
    luna = LunaClient()
    server = RawWorkspaceUi(
        state_path=tmp_path / "ui-state.json",
        path_chooser=lambda kind: str(source),
        live=LiveAnalysisTransport(
            sender=sender,
            cfg=Config(),  # 전사 경계는 설정 칸(온도 등)을 읽는다 — 빈 이름공간이 아니다
            client=luna,
            asr_approvals=lambda *a: {},
            luna_approvals=lambda *a: {},
            claude_clients=claude_clients(opener),
            gemini_clients=gemini_clients or {},
        ),
    )
    server.create_workspace_view("Claude 분석 흐름", str(tmp_path / "workspace"))
    keep_whisper_only(server)  # 19d 이전 흐름 — 교차 확인을 끈 채 Claude 호출·상한을 잰다
    if model is not None:
        root = server.workspace_root()
        workflow = server.raw.settings(root)
        models = replace(workflow.models, luna_model=model)
        server.raw.update_settings(root, replace(workflow, models=models))
    server._selection_auto_approval = (lambda quote: True) if auto_approve else None
    return server, sender, luna


def choose_model(server: RawWorkspaceUi, model: str) -> None:
    server.settings_analysis_model_view(server.current_signature(), model)
    assert server.raw.settings(server.workspace_root()).models.luna_model == model


def settle(server: RawWorkspaceUi) -> dict[str, Any]:
    wait_until(lambda: server._quick.get("stage") in SETTLED)
    return server._quick


def claude_rows(root: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(root / "project.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return list(db.execute("SELECT * FROM analysis_requests WHERE model=?", (CLAUDE,)))


def tier_layout(server: RawWorkspaceUi, revision: str, asset_id: str) -> list[tuple]:
    report = server.raw.tier_report(server.workspace_root(), revision, asset_id)
    assert report is not None
    return [(s.start, s.end, s.tier, s.rule, s.audio.status, s.video.status) for s in report.spans]


@needs_ffmpeg
def test_a_claude_selection_buys_asr_once_and_prices_every_claude_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    opener = ClaudeOpener()
    server, sender, luna = make_server(tmp_path, monkeypatch, source, opener=opener)
    choose_model(server, CLAUDE)
    server.capcut_choose_view(server.current_signature())
    state = settle(server)
    assert state["stage"] == DONE, state
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 0, 3)
    for request, body in opener.calls:
        assert request.full_url == "https://api.anthropic.com/v1/messages"
        assert body["model"] == CLAUDE and body["inference_geo"] == "global"
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert not {"thinking", "tools", "temperature"} & set(body)
    root = server.workspace_root()
    revision = AnalysisStore(server.raw._workspace(root)).revision(state["revision"])
    assert revision is not None and json.loads(revision.models_json)["luna"] == CLAUDE
    rows = claude_rows(root)
    assert len(rows) == 3
    assert all(row["stage"] == "luna" and row["unpriced_reason"] is None for row in rows)
    assert all(Decimal(row["cost_usd"]) == Decimal("0.044") for row in rows)
    assert {row["provider_request_id"] for row in rows} == {f"req_flow_{n}" for n in (1, 2, 3)}
    # 승인한 견적은 공식 단가의 보수적 상한을 실었고, 실제 금액은 그 안이다.
    (quoted,) = state["quote"].estimates
    per_window = window_ceiling_usd(
        claude_profile(CLAUDE),
        RawUnderstandingSettings(),
        schema_bytes=luna_output_schema_bytes(),
    )
    assert Decimal(quoted.ceiling_usd) == per_window * quoted.window_count
    assert sum(Decimal(row["cost_usd"]) for row in rows) <= Decimal(quoted.ceiling_usd)


@needs_ffmpeg
def test_a_sonnet_workspace_analyzes_with_sonnet_and_prices_at_sonnet_rates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-18(D58) — Sonnet 5.5 작업공간은 Sonnet으로 판정하고 Sonnet 단가로 값을 매긴다.

    D87부터 새 작업공간의 기본은 Gemini 3.8 Flash다 — 이 시험은 Sonnet을 고른 작업공간으로 연다.
    """
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    opener = ClaudeOpener()
    server, sender, luna = make_server(tmp_path, monkeypatch, source, opener=opener, model=SONNET)
    root = server.workspace_root()
    assert server.raw.settings(root).models.luna_model == SONNET
    server.capcut_choose_view(server.current_signature())
    state = settle(server)
    assert state["stage"] == DONE, state
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 0, 3)
    for request, body in opener.calls:
        assert request.full_url == "https://api.anthropic.com/v1/messages"
        assert body["model"] == SONNET and body["max_tokens"] == 12000
        assert body["output_config"]["effort"] == "medium"
        assert not {"thinking", "tools", "tool_choice", "temperature", "top_p", "top_k"} & set(body)
    revision = AnalysisStore(server.raw._workspace(root)).revision(state["revision"])
    assert revision is not None and json.loads(revision.models_json)["luna"] == SONNET
    with sqlite3.connect(root / "project.sqlite3") as db:
        db.row_factory = sqlite3.Row
        rows = list(db.execute("SELECT * FROM analysis_requests WHERE model=?", (SONNET,)))
    assert len(rows) == 3 and all(row["unpriced_reason"] is None for row in rows)
    assert all(Decimal(row["cost_usd"]) == Decimal("0.022") for row in rows)
    (quoted,) = state["quote"].estimates
    per_window = window_ceiling_usd(
        claude_profile(SONNET), RawUnderstandingSettings(), schema_bytes=luna_output_schema_bytes()
    )
    assert Decimal(quoted.ceiling_usd) == per_window * quoted.window_count
    assert sum(Decimal(row["cost_usd"]) for row in rows) <= Decimal(quoted.ceiling_usd)


@needs_ffmpeg
def test_a_sonnet_judgment_stores_its_visual_issues_in_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """18b(D58) — 가짜 Messages 응답의 `visual_issues`가 완성 문서의 판정에 그대로 남는다."""
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    opener = ClaudeOpener()
    server, _sender, _luna = make_server(tmp_path, monkeypatch, source, opener=opener, model=SONNET)
    server.capcut_choose_view(server.current_signature())
    state = settle(server)
    assert state["stage"] == DONE, state
    for _request, body in opener.calls:
        schema = body["output_config"]["format"]["schema"]
        assert "visual_issues" in schema["required"]
        assert "visual_issues" in body["messages"][0]["content"][-1]["text"]
    root, (asset_id,) = server.workspace_root(), tuple(state["asset_ids"])
    document = server.raw.analysis_document(root, state["revision"], asset_id)
    rows = sorted(document["annotations"], key=lambda row: row["window_id"])
    assert [row["status"] for row in rows] == ["accepted"] * 3
    assert [row["visual_issues"] for row in rows] == [["none"], ["shake", "dark"], ["blur"]]
    assert [bool(row["visual_issues_note"]) for row in rows] == [False, False, True]
    assert all(row["prompt_revision"] == RawUnderstandingSettings().prompt_revision for row in rows)


@needs_ffmpeg
def test_switching_models_keeps_the_transcript_and_switching_back_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    opener = ClaudeOpener()
    server, sender, luna = make_server(tmp_path, monkeypatch, source, opener=opener)
    server.capcut_choose_view(server.current_signature())
    first = settle(server)
    assert first["stage"] == DONE, first
    root, ids = server.workspace_root(), tuple(first["asset_ids"])
    (asset_id,) = ids
    workspace, luna_revision = server.raw._workspace(root), first["revision"]
    transcript = server.raw.source_transcript(root, asset_id)
    fingerprint = source_transcript.fingerprint(workspace, asset_id)
    asr_cache = sorted((root / "cache" / "source-asr").rglob("*.json"))
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 3, 0)

    choose_model(server, CLAUDE)
    server._start_quick(root, (), asset_ids=ids)
    second = settle(server)
    assert second["stage"] == DONE and second["revision"] != luna_revision, second
    # 전사는 다시 사지 않는다 — 캐시·원본 자막·전사 지문이 한 바이트도 움직이지 않는다.
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 3, 3)
    assert server.raw.source_transcript(root, asset_id) == transcript
    assert source_transcript.fingerprint(workspace, asset_id) == fingerprint
    assert sorted((root / "cache" / "source-asr").rglob("*.json")) == asr_cache
    # 같은 판정이면 Claude 문서도 Luna 문서와 같은 등급을 낸다.
    assert tier_layout(server, second["revision"], asset_id) == tier_layout(
        server, luna_revision, asset_id
    )

    choose_model(server, LUNA)
    shown = server._quick_status()  # 끝난 Claude 작업 위 — 가벼운 확인은 0회를 약속하지 못한다
    assert "저장된 결과 있음" in shown and "무료" not in shown
    assert server.raw.cached_selection_analysis(root, ids) == luna_revision
    server._start_quick(root, (), asset_ids=ids)
    third = settle(server)
    assert (third["stage"], third["revision"]) == (DONE, luna_revision)
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 3, 3)


@needs_ffmpeg
def test_a_missing_claude_key_stops_before_any_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    server, sender, luna = make_server(tmp_path, monkeypatch, source, opener=None)
    choose_model(server, CLAUDE)
    server.capcut_choose_view(server.current_signature())
    state = settle(server)
    assert state["stage"] == "분석 확인 필요", state
    assert "ANTHROPIC_API_KEY" in state["error"]
    assert (len(sender.calls), len(luna.calls)) == (0, 0)
    root = server.workspace_root()
    assert AnalysisStore(server.raw._workspace(root)).list_revisions() == ()
    assert server.raw.settings(root).network_mode == "offline"  # 문을 열기 전에 닫혔다


@needs_ffmpeg
@pytest.mark.parametrize(("quoted", "switched"), [(LUNA, CLAUDE), (CLAUDE, LUNA)])
def test_a_model_change_between_quote_and_execution_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quoted: str, switched: str
) -> None:
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    opener = ClaudeOpener()
    server, sender, luna = make_server(
        tmp_path, monkeypatch, source, opener=opener, auto_approve=False
    )
    choose_model(server, quoted)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    root, quote = server.workspace_root(), server._quick["quote"]
    workflow = server.raw.settings(root)
    # 화면의 거절을 건너뛴 경합 — 견적과 실행 사이에 설정이 바뀌었다.
    server.raw.update_settings(
        root, replace(workflow, models=replace(workflow.models, luna_model=switched))
    )
    context = SimpleNamespace(check_cancel=lambda: None, job_id="race")
    with pytest.raises(RawWorkspaceError, match="설정이 바뀌었습니다"):
        server.raw.run_selection_analysis(
            root, replace(quote, approved=True), server._live_transport, context, lambda *a: None
        )
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (0, 0, 0)


def test_tiering_reads_claude_annotations_exactly_like_luna_annotations() -> None:
    """등급은 판정 칸만 읽는다 — 모델 이름·요청 지문이 달라도 같은 판정은 같은 등급이다."""
    luna_document = tier_document()
    claude_document = json.loads(json.dumps(luna_document))
    for annotation in claude_document["annotations"]:
        annotation["luna_model"] = CLAUDE
        annotation["request_sha256"] = "c" * 64
    luna_report = tiering.classify(luna_document)
    claude_report = tiering.classify(claude_document)
    assert claude_report.spans == luna_report.spans
    assert claude_report.document_sha256 != luna_report.document_sha256


def _quoted(ceiling: str | None, requests: int = 3) -> SimpleNamespace:
    return SimpleNamespace(asset_id="a", luna_request_count=requests, ceiling_usd=ceiling)


def test_the_measured_ceiling_may_never_exceed_the_approved_ceiling() -> None:
    """전사 뒤 실측 상한이 사람이 본 금액보다 크면 2단계 승인을 만들지 않는다(D54)."""
    from vlogsrt.project.raw.selection_analysis import _require_within_approval

    _require_within_approval([_quoted("0.75")], [_quoted("0.75")])
    _require_within_approval([_quoted("0.75")], [_quoted("0.50", requests=2)])
    _require_within_approval([_quoted(None)], [_quoted(None)])  # Luna — 요청 수만 본다
    with pytest.raises(RawWorkspaceError, match="비용 상한"):
        _require_within_approval([_quoted("0.75")], [_quoted("0.76")])
    with pytest.raises(RawWorkspaceError, match="비용 상한"):
        _require_within_approval([_quoted("0.75")], [_quoted(None)])
    with pytest.raises(RawWorkspaceError, match="요청 상한"):
        _require_within_approval([_quoted("0.75")], [_quoted("0.75", requests=4)])
