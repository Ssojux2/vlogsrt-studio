"""D59 19b — 「전사만」 실행.

1단계만 돌고 영상 분석 앞에서 멈춘다. 범위는 견적 digest에 접힌다. 자막 작업(재시도·다시
듣기)은 전부 「전사만」으로 끝내고, 영상 분석(Sonnet)은 마지막에 새 전체 실행으로 한 번
돈다(계획 「비용을 한 번만 내기 위한 순서」). 그 전체 실행은 1단계를 다시 사지 않는다.
「전사만」은 Claude 키가 없어도 된다. 범위만 바꾼 승인은 실행 문에서 0송신으로 닫힌다. 유료
호출 0회 — 가짜 전송이 센다. 화면 시험은 포트 7993(시험용)·임시 작업공간만 쓴다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from tests.raw.test_background_library import setup_library  # noqa: E402
from tests.raw.test_relisten import approve, pre_19b_digest, stage  # noqa: E402
from tests.raw.test_selection_analysis import wait_until  # noqa: E402
from tests.raw.test_workbench_layout import _GUARD_HARNESS, needs_node  # noqa: E402
from ui import ui_facade, workspace_service  # noqa: E402
from ui.workspace_service import (  # noqa: E402
    APPROVAL_MODE_CHOICES,
    APPROVAL_MODE_ID,
    LIBRARY_APPROVE_LABEL,
    RawWorkspaceUi,
)
from vlogsrt.env import ANALYSIS_GEMINI_KEY_ENV  # noqa: E402
from vlogsrt.project.raw import selection_analysis  # noqa: E402
from vlogsrt.project.raw import service as raw  # noqa: E402
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport  # noqa: E402
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings  # noqa: E402
from vlogsrt.project.raw.analysis_store import AnalysisStore  # noqa: E402
from vlogsrt.project.raw.config import DEFAULT_ANALYSIS_MODEL  # noqa: E402
from vlogsrt.project.raw.models import AnalysisRevision, RawWorkspaceError  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

TRANSCRIBE = raw.SELECTION_MODE_TRANSCRIBE
FULL = raw.SELECTION_MODE_FULL
DONE = "추천 구간 준비 완료"
POLITE_END = re.compile(r"(니다|세요|까요)[.)]*$")


# ── 견적 — 범위는 digest에 접히고, 전체 범위의 digest는 19b 이전 그대로 ──────────────────


@needs_ffmpeg
def test_the_mode_is_folded_into_the_digest_and_the_full_digest_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, _sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    measured = ui._library_quotes[root]
    every = tuple(measured.asset_ids)
    assert measured.mode == FULL and measured.digest == pre_19b_digest(ui, root, measured)
    only = ui.raw.selection_rebound_quote(root, measured, every, TRANSCRIBE)
    assert only.mode == TRANSCRIBE and only.estimates == measured.estimates
    assert only.digest != measured.digest and only.workflow == measured.workflow
    assert ui.raw.selection_rebound_quote(root, only, every, FULL) == measured
    child = ui.raw.selection_rebound_quote(root, only, (ids["a.mov"],))
    assert child.mode == TRANSCRIBE  # 범위를 주지 않으면 잰 견적의 범위 그대로
    assert ui.raw.selection_quote(root, every, TRANSCRIBE) == only
    with pytest.raises(RawWorkspaceError, match="알 수 없는 승인 범위"):
        ui.raw.selection_quote(root, every, "semantic_only")


@needs_ffmpeg
def test_an_approval_cannot_be_replayed_in_the_other_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「전사만」 승인을 전체 실행으로, 전체 승인을 「전사만」으로 — 둘 다 실행 문에서 0송신."""
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    measured = ui._library_quotes[root]
    a = (ids["a.mov"],)
    only = replace(ui.raw.selection_rebound_quote(root, measured, a, TRANSCRIBE), approved=True)
    full = replace(ui.raw.selection_rebound_quote(root, measured, a, FULL), approved=True)
    context = SimpleNamespace(check_cancel=lambda: None, job_id="test")
    for forged in (replace(only, mode=FULL), replace(full, mode=TRANSCRIBE)):
        with pytest.raises(RawWorkspaceError, match="바뀌었습니다"):
            ui.raw.run_selection_analysis(root, forged, ui._live_transport, context, print)
    assert not sender.calls and not client.calls
    assert ui.raw.settings(root).network_mode != "online"  # 아무것도 열지 않았다


# ── 실행 — 1단계만 · 키 없음 · 검토 상태 · 보내기 · 나중의 전체 실행 ──────────────────────


def _as_sonnet_without_key(ui: Any, root: Path) -> None:
    """새 작업공간의 기본(Gemini 3.8 Flash, D87) — 가짜 전송에는 Gemini 클라이언트가 없다(키
    없음)."""
    workflow = ui.raw.settings(root)
    models = replace(workflow.models, luna_model=DEFAULT_ANALYSIS_MODEL)
    ui.raw.update_settings(root, replace(workflow, models=models))
    ui._prepare_library_quote(root)
    wait_until(
        lambda: ui._library_quotes[root].workflow.models.luna_model == DEFAULT_ANALYSIS_MODEL
    )


@needs_ffmpeg
def test_transcribe_only_sends_no_semantic_request_and_needs_no_claude_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    a, b = ids["a.mov"], ids["b.mov"]
    _as_sonnet_without_key(ui, root)
    signature = ui.current_signature()
    status = ui.library_analysis_view(signature, None, ui.library_analysis_summary())[0]
    assert ANALYSIS_GEMINI_KEY_ENV in status  # 전체 분석은 키가 없어 닫힌다
    assert not sender.calls and ui._approval_jobs.get(root) is None

    def no_semantic(self: Any, model: str) -> object:
        raise AssertionError(f"「전사만」이 의미 분석 클라이언트를 받았다: {model}")

    monkeypatch.setattr(LiveAnalysisTransport, "semantic_client", no_semantic)
    shown = ui.approval_mode_view(signature, TRANSCRIBE)
    summary = ui.library_analysis_summary()
    assert shown == ui_facade.literal_markdown(summary)  # 고르면 견적 칸을 바로 다시 그린다
    assert summary.startswith("전사만 · 대상 영상 2개") and "$" not in summary
    ui.library_analysis_view(signature, None, summary)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in (a, b)))
    assert len(sender.calls) == 2 and client.calls == []
    store = AnalysisStore(ui.raw._workspace(root))
    for asset_id in (a, b):
        revision_id = ui._task_for(root, (asset_id,))["revision"]
        assert store.revision(revision_id).status == "awaiting_semantic"
        rows = store.list_source_results(revision_id)
        assert [r.status for r in rows if r.asset_id == asset_id] == ["transcribed"]
        assert ui.raw.selection_suitability(root, revision_id, (asset_id,)) == {}  # 판정 없음
        assert ui.raw.tier_report(root, revision_id, asset_id) is None
        assert ui.raw.source_transcript(root, asset_id)["utterances"]  # 원본 자막은 심겼다
    assert ui.raw.analysis_overview(root) is not None
    # 화면 — 「전사 완료 · 분석 대기」(조용한 배지)와 다음에 누를 것을 존댓말로.
    ui.quick_source_view(signature, a)
    assert "**전사 완료 · 분석 대기**" in ui._quick_status_text()
    assert "「전사 + 영상 분석」" in ui._quick_status_text()
    badge = ui._analysis_badge(ui._task_for(root, (a,)), raw.TRANSCRIBED_STAGE)
    assert badge == {"label": raw.TRANSCRIBED_STAGE, "tone": ""}
    _shell, html = ui.visual_workbench_render(root)
    assert "data-vl-model" in html
    # Final Cut 보내기는 그대로 돈다 — 판정이 없으니 「미분석」, 자막은 실린다.
    fcpxml = ui.raw.export_finalcut_timeline(root)
    text = fcpxml.read_text(encoding="utf-8")
    assert "미분석" in text and "추천" not in text
    report = json.loads(fcpxml.with_suffix(".json").read_text(encoding="utf-8"))
    assert all(s["analysis_revision"] is None and s["captions"] for s in report["sources"])


@needs_ffmpeg
def test_a_later_full_run_reuses_stage_one_and_buys_no_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    approve(ui, TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in ids.values()))
    assert (len(sender.calls), len(client.calls)) == (2, 0)
    transcribed = {i: ui._task_for(root, (i,))["revision"] for i in ids.values()}
    approve(ui, FULL)  # 새 견적·새 승인 — 같은 지문이면 같은 리비전의 1단계 기록을 다시 쓴다
    wait_until(lambda: all(stage(ui, root, i) == DONE for i in ids.values()))
    assert (len(sender.calls), len(client.calls)) == (2, 4)  # whisper 0회 추가
    assert {i: ui._task_for(root, (i,))["revision"] for i in ids.values()} == transcribed
    assert ui.raw.load_selection_state(root)  # 단계는 기록에 남는다


@needs_ffmpeg
def test_the_click_submits_exactly_the_quote_it_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증 지적 2 — 대조와 접수 사이에 범위 칸이 견적을 다시 묶어도 대조한 견적을 접수한다.

    예전에는 대조 뒤 놓인 견적을 다시 읽어, 그 틈에 「전사만」으로 묶인 견적이 전체 문구를 보고
    누른 클릭으로 접수될 수 있었다(범위 칸의 `input`은 대기열 밖에서 돈다).
    """
    ui, sender, client, root, _ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    signature = ui.current_signature()
    compared = ui._library_quotes[root]
    real = ui.library_analysis_summary
    shown = real()
    raced: list[bool] = []

    def racing() -> str:
        text = real()
        if not raced:  # 대조가 문구를 읽는 바로 그때 사람이 「전사만」을 누른다
            raced.append(True)
            ui.approval_mode_view(signature, TRANSCRIBE)
        return text

    submitted: list[Any] = []
    monkeypatch.setattr(ui, "library_analysis_summary", racing)
    monkeypatch.setattr(ui, "_submit_library_approval", lambda r, i, q: submitted.append(q))
    ui.library_analysis_view(signature, None, shown)
    assert raced == [True] and ui._library_quotes[root].mode == TRANSCRIBE
    assert submitted == [compared] and submitted[0].mode == FULL  # 사람이 본 전체 견적 그대로
    assert not sender.calls and not client.calls


# ── 이전 기준 리비전의 「실패 구간 재시도」는 「전사만」 새 분석으로 ─────────────────────────


def test_the_old_prompt_predicate_reads_the_revision(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "기준")
    store = AnalysisStore(service._workspace(root))
    for revision_id, prompt in (("old", "raw-suitability-v2"), ("now", None)):
        settings = RawUnderstandingSettings()
        if prompt is not None:
            settings = replace(settings, prompt_revision=prompt)
        store.insert_revision(
            AnalysisRevision(
                revision_id=revision_id,
                order_revision_id="o",
                basis="user_selected",
                mode="live",
                settings_json=json.dumps(asdict(settings), sort_keys=True),
                models_json="{}",
                fingerprint=revision_id,
                status="failed",
                source_total=1,
                source_done=0,
                error=None,
                created_at="2026-10-11T00:00:00Z",
                updated_at="2026-10-11T00:00:00Z",
            )
        )
    assert service.analysis_revision_on_old_prompt(root, "old") is True
    assert service.analysis_revision_on_old_prompt(root, "now") is False
    assert service.analysis_revision_on_old_prompt(root, "missing") is False


def test_retry_on_an_old_prompt_offers_a_transcribe_only_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("재시도", str(tmp_path / "ws"))
    root = ui.workspace_root()
    signature = ui.current_signature()
    old_job = SimpleNamespace(job_id="job_old", status="failed", kind=raw.ANALYSIS_JOB_KIND)
    # 재시도는 다시 걸 수 있는 종류의 최근 창만 본다(W-24 J).
    monkeypatch.setattr(ui.raw, "recent_jobs", lambda _root, kinds, limit=50: (old_job,))
    monkeypatch.setattr(ui.raw, "analysis_jobs_on_old_prompt", lambda _root: frozenset({"job_old"}))
    retried: list[str] = []
    monkeypatch.setattr(ui, "_retry_job", lambda *args: retried.append("retry") or "재시도")
    assert ui.raw.approval_mode(root) == FULL
    assert ui.capcut_retry_view(signature) == workspace_service.RETRY_TRANSCRIBE_TEXT
    assert ui.raw.approval_mode(root) == TRANSCRIBE and retried == []  # 이어서 재시도하지 않는다
    # 고른 영상이 이전 기준의 리비전이면 「전사만」 견적을 새로 낸다(승인 전에는 사지 않는다).
    ui.raw.set_approval_mode(root, FULL)
    starts: list[Any] = []
    monkeypatch.setattr(ui, "_start_quick", lambda *args, **kwargs: starts.append(kwargs))
    monkeypatch.setattr(ui.raw, "analysis_revision_on_old_prompt", lambda _root, rev: rev == "old")
    ui._quick = {"root": root, "token": "t", "asset_ids": ("source",), "revision": "old"}
    assert ui.capcut_retry_view(signature) == workspace_service.RETRY_TRANSCRIBE_TEXT
    assert ui.raw.approval_mode(root) == TRANSCRIBE
    assert starts == [{"asset_ids": ("source",), "retry": True}]
    ui._quick = {"root": root, "token": "t", "asset_ids": ("source",), "revision": "now"}
    assert "표시된 범위를 승인하면" in ui.capcut_retry_view(signature)  # 지금 기준은 예전 그대로


# ── 화면 배선(Blocks) · 문구 · 승인 앞 단계 ─────────────────────────────────────────────


def test_blocks_put_the_approval_mode_above_the_quote_and_wire_it(
    tmp_path: Path, local_socket: None
) -> None:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("범위", str(tmp_path / "ws"))
    app = ui.build_blocks()
    by_id = {getattr(b, "elem_id", None): b for b in app.blocks.values()}
    radio = by_id[APPROVAL_MODE_ID]
    quote = by_id["vl-quote"]
    assert isinstance(radio, gr.Radio) and radio is ui._approval_mode_radio
    # 그라디오는 입력 칸을 폼 하나로 감싼다 — 그 폼이 견적 칸 안, 견적 문구 바로 앞이다.
    form = radio.parent
    assert form.parent is quote
    assert quote.children.index(form) + 1 == quote.children.index(by_id["library-analysis-scope"])
    assert (
        [tuple(c) for c in radio.choices]
        == [
            ("전사 + 영상 분석", FULL),
            ("전사만", TRANSCRIBE),
        ]
        == [tuple(c) for c in APPROVAL_MODE_CHOICES]
    )
    assert radio.value == FULL  # 새 작업공간 — 지금까지의 흐름 그대로
    deps = app.config["dependencies"]
    (chosen,) = [d for d in deps if d["targets"] == [(radio._id, "input")]]
    assert chosen["inputs"] == [ui._signature_state._id, radio._id]
    assert chosen["outputs"] == [ui._library_summary_md._id]
    follow = [d for d in deps if d.get("trigger_after") == chosen["id"]]
    assert [d["outputs"] for d in follow] == [[ui._quote_note_md._id]]
    # 칸의 값을 맞추는 것은 둘 — 화면을 열 때(그때의 작업공간 값)와 1초 틱. 틱은 그 탭의 칸
    # 값을 입력으로 받아 저장값과 다를 때만 보낸다(탭마다 따로 — 검증 지적 1). 틱은 1초 상태 틱
    # 하나다(D63 세션 C) — 범위 칸은 그 틱의 첫 칸이다.
    refresh = {d["targets"][0][1]: d for d in deps if radio._id in d["outputs"]}
    assert sorted(refresh) == ["load", "tick"]
    assert refresh["tick"]["inputs"][:2] == [ui._signature_state._id, radio._id]
    # 승인 클릭의 결합은 그대로 — 보이는 견적 문구만 함께 간다(D54·D57).
    buttons = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}
    (approve_dep,) = [
        d for d in deps if d["targets"] == [(buttons[LIBRARY_APPROVE_LABEL]._id, "click")]
    ]
    assert approve_dep["js"] == ui_facade.APPROVE_GUARD_JS
    assert ui._library_summary_md._id in approve_dep["inputs"]
    names = [fn.__name__ for fn in ui._visual_workbench_block.server_fns]
    # 19f가 빈칸 재전사 승인을, 19h가 시각 맞춤 적용·되돌리기를, W-25b가 시각 정답을, W-25e가
    # 모든 영상 시각 맞춤 확인·적용을 끝에 더했다.
    assert names[-7:] == [
        "vl_relisten",
        "vl_refill",
        "vl_snap_apply",
        "vl_snap_revert",
        "vl_timing_truth",
        "vl_snap_all_check",
        "vl_snap_all_apply",
    ]
    css = ui_facade.WORKBENCH_CSS
    assert "#review-tab:has(#library-approve:disabled) #vl-approval-mode { display: none; }" in css
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    assert 'data-vl-act="relisten"' in script and "call('vl_relisten'" in script
    assert "function relistenPanel" in script and "out.push(relistenPanel(info));" in script


def test_the_mode_view_saves_rescopes_and_the_tick_follows_other_changes(
    tmp_path: Path, local_socket: None
) -> None:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("범위", str(tmp_path / "ws"))
    root = ui.workspace_root()
    signature = ui.current_signature()
    assert ui.approval_mode_value() == FULL
    assert _skipped(ui.approval_mode_tick(signature, FULL))  # 칸이 저장값과 같으면 보내지 않는다
    ui.approval_mode_view(signature, TRANSCRIBE)
    assert ui.raw.approval_mode(root) == TRANSCRIBE
    assert _skipped(ui.approval_mode_tick(signature, TRANSCRIBE))
    reopened = RawWorkspaceUi(port=7993, state_path=tmp_path / "state2.json")
    reopened.open_workspace_view(str(root))
    assert reopened.approval_mode_value() == TRANSCRIBE  # 앱을 다시 열어도 남는다
    ui._set_approval_mode(root, FULL)  # 다른 곳(다시 듣기·재시도)이 바꾸면 틱이 칸을 맞춘다
    assert ui.approval_mode_tick(signature, TRANSCRIBE).value == FULL
    assert _skipped(ui.approval_mode_tick("다른 화면", TRANSCRIBE))
    with pytest.raises(gr.Error):
        ui.approval_mode_view(signature, "semantic_only")


def test_two_tabs_never_keep_a_radio_that_contradicts_the_quote(
    tmp_path: Path, local_socket: None
) -> None:
    """검증 지적 1 — 탭 A가 「전사만」, 탭 B가 전체로 되돌리면 탭 A의 칸도 다음 틱에 전체가 된다.

    예전 틱은 서버 전역의 「마지막으로 보낸 값」과 견줘, 탭 B에 보낸 뒤로는 탭 A에 아무것도 보내지
    않았다 — 탭 A의 칸은 「전사만」인데 견적 문구와 승인은 전체였다. 이제 틱은 그 탭의 칸
    값을 받는다.
    """
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("두 탭", str(tmp_path / "ws"))
    signature = ui.current_signature()
    ui.approval_mode_view(signature, TRANSCRIBE)  # 탭 A가 누른다(그 탭의 칸은 전사만)
    assert _skipped(ui.approval_mode_tick(signature, TRANSCRIBE))  # 탭 A의 틱 — 같다
    ui.approval_mode_view(signature, FULL)  # 탭 B가 되돌린다(탭 B의 칸은 전체)
    assert _skipped(ui.approval_mode_tick(signature, FULL))  # 탭 B의 틱 — 같다
    for _ in range(3):  # 탭 A의 틱은 칸이 고쳐질 때까지 매번 저장값을 보낸다
        assert ui.approval_mode_tick(signature, TRANSCRIBE).value == FULL
    assert _skipped(ui.approval_mode_tick(signature, FULL))  # 고쳐진 뒤에는 보내지 않는다


def _skipped(update: Any) -> bool:
    return bool(update == gr.skip())


@needs_node
def test_the_approve_pre_step_passes_the_visible_transcribe_only_quote(tmp_path: Path) -> None:
    """「전사만」 견적 문구도 보이면 그대로 간다 — 「」·가운뎃점·괄호가 escape 왕복을 지난다."""
    raw_text = workspace_service._transcribe_only_summary(6, 12, 23, 3)
    harness = tmp_path / "guard.js"
    harness.write_text(_GUARD_HARNESS, encoding="utf-8")
    script = tmp_path / "guard_src.js"
    script.write_text(ui_facade.APPROVE_GUARD_JS, encoding="utf-8")
    quotes = tmp_path / "quotes.json"
    shown = ui_facade.literal_markdown(raw_text)
    quotes.write_text(
        json.dumps(
            {"raw": raw_text, "shown": shown, "specialRaw": raw_text, "specialShown": shown}
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(script), str(quotes)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    out = json.loads(completed.stdout)
    assert out["visible"] == {"quote": raw_text, "scrolled": 0, "closed": 0}
    assert out["drawer"]["quote"] == "" and out["covered"]["quote"] == ""


def test_the_new_copy_is_polite_and_carries_no_prices() -> None:
    quote = selection_analysis.SelectionQuote(
        asset_ids=("a",),
        estimates=(),
        digest="d",
        workflow=raw.RawWorkflowConfig(),
        mode=TRANSCRIBE,
    )
    texts = [
        workspace_service._transcribe_only_summary(2, 1, 4, 3),
        workspace_service.RawWorkspaceUi._approval_ready_text(quote),
        workspace_service.RELISTEN_EXPLANATION,
        workspace_service.RELISTEN_DONE_TEXT,
        workspace_service.RELISTEN_PENDING_TEXT,
        workspace_service.RELISTEN_BUSY_TEXT,
        workspace_service.RETRY_TRANSCRIBE_TEXT,
        *workspace_service.STAGE_NOTES.values(),
        raw.RELISTEN_EDITED_REFUSAL,
        raw.RELISTEN_UNTRANSCRIBED_REFUSAL,
        raw.RELISTEN_SILENT_REFUSAL,
    ]
    for text in texts:
        assert "$" not in text and "Claude" not in text and "whisper" not in text, text
        for line in filter(None, (part.strip() for part in text.split("\n"))):
            assert POLITE_END.search(line) or line.startswith("**"), line
        # 반말 끝맺음(…한다. …없다.)이 없다 — 「다.」는 언제나 「니다.」다.
        assert not re.search(r"(?<!니)다\.", text), text
    # 새로 사는 것을 모두 이름 댄다 — 실패한 조각 · 다시 듣기 · 처음 듣는 영상(검증 지적 4).
    summary = workspace_service._transcribe_only_summary(2, 1, 4, 3)
    for named in ("실패한 조각", "다시 듣기를 고른 영상", "처음 듣는 영상"):
        assert named in summary, named
    assert raw.RELISTEN_SILENT_REFUSAL.endswith("둡니다.")
    assert "온도 설정 오류" in workspace_service.RELISTEN_EXPLANATION
    assert workspace_service.RELISTEN_EXPLANATION.startswith("이 영상을 처음부터 다시 듣습니다")
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    panel = script[script.index("function relistenPanel") : script.index("function tierWord")]
    assert "덮어쓰지 않습니다" in panel and "승인해야" in panel and "$" not in panel
