"""W-14 / D54 — 설정 서랍 「분석 모델」의 영상 분석 모델 선택과 그 둘레(유료 호출 0회).

모델은 작업공간 설정에 산다. 분석이나 승인 확인이 도는 동안에는 바꾸지 않는다 — 견적·승인이 본
모델과 실행 모델이 갈린다. 바꾸면 라이브러리 캐시를 비우고 견적을 다시 낸다. 견적은 모델 이름과
보수적 비용 상한을 말하고, 다른 모델로 끝난 분석은 끝났다고 보이지 않는다. 키는 이름만 말하고 값은
어디에도 싣지 않는다. 모델을 바꾸는 것은 사람의 검토 편집이 아니다 — 검토 머리·영상 순서가
움직이지 않는다(D53).

W-30d(D87)부터 새 작업공간의 기본은 Gemini 3.8 Flash이고 칸 맨 앞에 「(기본)」으로 보인다(그 전에는
Claude Sonnet 5.5, D58). Luna에서 출발하는 시험은 Luna를 저장한 기존 작업공간처럼
연다(`bare_server`· `make_server`) — 사람이 고른 Luna는 열어도 옮기지 않는다.
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import replace
from pathlib import Path

import gradio as gr
import pytest

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_claude_analysis_flow import (
    CLAUDE,
    DONE,
    LUNA,
    SONNET,
    ClaudeOpener,
    choose_model,
    make_server,
    settle,
)
from tests.raw.test_selection_analysis import wait_until
from ui.workspace_service import (
    LIBRARY_APPROVAL_JOB_KIND,
    LIBRARY_APPROVE_LABEL,
    LIBRARY_SCAN_JOB_KIND,
    NOTICE_FAILED,
    RawWorkspaceUi,
)
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.analysis_store import AnalysisStore

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

SECRET = "sk-ant-synthetic-never-shown"
FLASH = "gemini-3.8-flash"


def bare_server(tmp_path: Path, model: str | None = LUNA) -> RawWorkspaceUi:
    """빈 작업대 — `model`이 있으면 그 모델을 저장한 기존 작업공간처럼, None이면 새 기본 그대로."""
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("분석 모델", str(tmp_path / "workspace"))
    if model is not None:
        root = server.workspace_root()
        workflow = server.raw.settings(root)
        server.raw.update_settings(
            root, replace(workflow, models=replace(workflow.models, luna_model=model))
        )
    return server


def model_of(server: RawWorkspaceUi) -> str:
    return server.raw.settings(server.workspace_root()).models.luna_model


def test_the_dropdown_switches_the_workspace_model_and_back(tmp_path: Path) -> None:
    server = bare_server(tmp_path)
    # W-30d(D87): 기본(Gemini 3.8 Flash)이 맨 앞에 「(기본)」으로, 나머지는 그대로 고른다
    assert server.analysis_model_choices() == [
        ("Gemini 3.8 Flash (기본)", FLASH),
        ("Gemini 4 Argon", "gemini-4-argon"),
        ("Claude Sonnet 5.5", SONNET),
        ("Claude Opus 5.5", CLAUDE),
        (LUNA, LUNA),
    ]
    status, dropdown = server.settings_analysis_model_view(server.current_signature(), CLAUDE)
    assert model_of(server) == CLAUDE and dropdown.value == CLAUDE
    assert "Claude Opus 5.5" in status
    status, dropdown = server.settings_analysis_model_view(server.current_signature(), LUNA)
    assert model_of(server) == LUNA and dropdown.value == LUNA


def test_a_new_workspace_shows_flash_as_the_default_without_a_price(tmp_path: Path) -> None:
    """새 작업공간은 Gemini 3.8 Flash로 시작한다(D87) — 「(기본)」은 선택지에만, 달러는 선택지에
    없다."""
    server = bare_server(tmp_path, model=None)
    assert model_of(server) == FLASH
    dropdown, status = server.analysis_model_controls()
    assert dropdown.value == FLASH
    labels = [label for label, _ in server.analysis_model_choices()]
    assert labels[0] == "Gemini 3.8 Flash (기본)"
    assert sum("(기본)" in label for label in labels) == 1
    assert not any("$" in label for label in labels)
    assert "Gemini 3.8 Flash" in status and "(기본)" not in status
    # 상태 줄도 달러·토큰 용어 없이 단가 상태만 말한다 — 금액은 말하지 않는다(D57 결정 7 · D110)
    assert "공식 단가 확인됨" in status and "견적" not in status
    assert not any(word in status for word in PRICE_WORDS), status
    status, dropdown = server.settings_analysis_model_view(server.current_signature(), LUNA)
    assert model_of(server) == LUNA and dropdown.value == LUNA


PRICE_WORDS = ("$", "100만 토큰", "effort", "출력 한도", "입력 $", "출력 $")
"""화면(설정 요약·분석 설정 줄·모델 상태 줄)에 나오면 안 되는 단가·토큰 용어(D57 결정 7)."""


@pytest.mark.parametrize("model", list(raw_service.ANALYSIS_MODEL_CHOICES))
def test_no_screen_text_outside_quotes_shows_a_unit_price(tmp_path: Path, model: str) -> None:
    """모든 모델에서 상태 줄·설정 요약·분석 설정 줄에 달러·토큰 용어가 없다(D110)."""
    server = bare_server(tmp_path, model=model)
    root = server.workspace_root()
    texts = (
        server.analysis_model_status(),
        server.settings_markdown(root),
        server._analysis_settings_markdown(root),
    )
    for text in texts:
        assert not any(word in text for word in PRICE_WORDS), text
    official = raw_service.official_price_model(model)
    expected = "공식 단가 확인됨" if official else "단가 **미확인**"
    assert all(expected in text for text in (texts[0], texts[2]))


def test_switching_workspaces_refreshes_the_model_chooser(tmp_path: Path) -> None:
    """작업공간을 바꾸면 칸도 그 작업공간의 모델을 보인다 — Luna 작업공간이 Flash로 안 보인다.

    사람이 고른 Luna는 열어도 옮기지 않는다 — 옮기는 것은 예전 기본(Sonnet)에 머문
    작업공간뿐이다(D87).
    """
    server = bare_server(tmp_path)  # Luna를 저장한 기존 작업공간
    luna_root = server.workspace_root()
    server.create_workspace_view("새 작업공간", str(tmp_path / "fresh"))
    dropdown, status = server.analysis_model_controls()
    assert dropdown.value == FLASH and "Gemini 3.8 Flash" in status
    server.open_workspace_view(str(luna_root))
    dropdown, status = server.analysis_model_controls()
    assert dropdown.value == LUNA and "gpt-6-luna" in status
    assert server.raw.settings(luna_root).models.luna_model == LUNA  # 맞추기만 하고 저장하지 않는다


def test_every_workspace_change_rewires_the_model_chooser(
    tmp_path: Path, local_socket: None
) -> None:
    """실제 Blocks — 열기·만들기·라이브러리·직접 열기·영상 고르기·앱 열기 뒤에 칸·상태를 맞춘다."""
    server = bare_server(tmp_path)
    app = server.build_blocks()
    fns = app.fns
    by_id = {getattr(block, "elem_id", None): block for block in app.blocks.values()}
    targets = {by_id["analysis-model"]._id, by_id["analysis-model-status"]._id}

    def root_name(fn: object) -> str:
        while getattr(fn, "trigger_after", None) is not None:
            fn = fns[fn.trigger_after]  # type: ignore[attr-defined]
        return fn.fn.__name__  # type: ignore[attr-defined]

    refreshers = [
        f for f in fns.values() if getattr(f.fn, "__name__", "") == "analysis_model_controls"
    ]
    assert all({block._id for block in f.outputs} == targets for f in refreshers)
    roots = [root_name(f) for f in refreshers if f.trigger_after is not None]
    assert sorted(roots) == sorted(
        [
            "direct_project_open_view",
            "capcut_fcp_choose_view",
            "capcut_fcp_choose_view",
            "open_project_chooser_view",
            "create_project_chooser_view",
            "refresh_outputs",
            "capcut_choose_view",
            "close_library_view",  # 「라이브러리 닫기」도 작업공간을 바꾼다(D64 세션 F)
            # 폴더 불러오기(W-46a) — 단추 · 경로 칸 Enter · 찾아보기 뒤
            "folder_import_view",
            "folder_import_view",
            "folder_browse_view",
        ]
    )
    assert not any(
        getattr(f.fn, "__name__", "") == "settings_analysis_model_view"
        and f.trigger_after is not None
        for f in fns.values()
    )  # 저장은 사람의 `input`뿐


def test_an_existing_luna_workspace_is_not_moved_to_the_new_default(tmp_path: Path) -> None:
    """Luna를 저장한 작업공간은 다시 열어도 Luna다 — 칸만 열어서는 저장하지 않는다."""
    server = bare_server(tmp_path)
    dropdown, _ = server.analysis_model_controls()
    assert dropdown.value == LUNA and model_of(server) == LUNA
    reopened = RawWorkspaceUi(state_path=tmp_path / "ui-state-2.json")
    reopened.raw.open_workspace(server.workspace_root())
    assert reopened.raw.settings(server.workspace_root()).models.luna_model == LUNA


@pytest.mark.parametrize("model", ["claude-opus-9", "gpt-5", "", None])
def test_an_unregistered_model_is_refused(tmp_path: Path, model: str | None) -> None:
    server = bare_server(tmp_path)
    with pytest.raises(gr.Error):
        server.settings_analysis_model_view(server.current_signature(), model)
    assert model_of(server) == LUNA


def _blocking_job(server: RawWorkspaceUi, kind: str) -> tuple[str, threading.Event]:
    release = threading.Event()
    handle = server.raw.submit_job(
        server.workspace_root(), kind, f"{kind}-hold", lambda context: release.wait(10)
    )
    return handle.job_id, release


@pytest.mark.parametrize(
    "kind", [raw_service.ANALYSIS_JOB_KIND, raw_service.ANALYSIS_SEMANTIC_JOB_KIND, "approval"]
)
def test_a_switch_is_refused_while_an_analysis_or_approval_runs(tmp_path: Path, kind: str) -> None:
    server = bare_server(tmp_path)
    root = server.workspace_root()
    job_kind = LIBRARY_APPROVAL_JOB_KIND if kind == "approval" else kind
    job_id, release = _blocking_job(server, job_kind)
    if kind == "approval":
        server._approval_jobs[root] = job_id
    try:
        status, dropdown = server.settings_analysis_model_view(server.current_signature(), CLAUDE)
    finally:
        release.set()
    assert model_of(server) == LUNA and dropdown.value == LUNA
    assert "바꾸지 않았습니다" in status


def test_a_switch_is_refused_while_a_selected_video_is_being_analyzed(tmp_path: Path) -> None:
    server = bare_server(tmp_path)
    root = server.workspace_root()
    server._quick_tasks["busy"] = {
        "root": root,
        "token": "busy",
        "asset_ids": ("a",),
        "stage": "Luna 추천 구간 분석",
    }
    status, dropdown = server.settings_analysis_model_view(server.current_signature(), CLAUDE)
    assert model_of(server) == LUNA and dropdown.value == LUNA
    assert "바꾸지 않았습니다" in status


def test_the_key_status_names_the_variable_but_never_the_value(tmp_path: Path) -> None:
    server = bare_server(tmp_path)
    server._startup_claude = raw_service.ClaudeCredentials(api_key=SECRET)
    present = server.analysis_model_status()
    assert "ANTHROPIC_API_KEY" in present and "확인됨" in present
    assert SECRET not in present and "sk-ant" not in present
    assert SECRET not in repr(server._startup_claude)
    server._startup_claude = None
    missing = server.analysis_model_status()
    assert "ANTHROPIC_API_KEY" in missing and "없음" in missing


def test_the_model_panel_carries_the_model_dropdown_and_never_the_key(
    tmp_path: Path, local_socket: None
) -> None:
    """실제 Blocks — 칸은 「분석 모델」 안, 사람이 고를 때(`input`)만 저장, 키 값은 없다."""
    server = bare_server(tmp_path)
    server._startup_claude = raw_service.ClaudeCredentials(api_key=SECRET)
    app = server.build_blocks()
    by_id = {getattr(block, "elem_id", None): block for block in app.blocks.values()}
    dropdown, status = by_id["analysis-model"], by_id["analysis-model-status"]
    assert isinstance(dropdown, gr.Dropdown) and dropdown.label == "영상 분석 모델"
    assert [value for _, value in dropdown.choices] == [
        FLASH,
        "gemini-4-argon",
        SONNET,
        CLAUDE,
        LUNA,
    ]
    assert dropdown.value == LUNA  # 저장된 모델 그대로 — 기본으로 옮기지 않는다
    # gradio가 입력 칸을 Form으로 감싼다
    assert dropdown.parent.parent is by_id["analysis-model-panel"]
    assert "확인됨" in str(status.value)
    triggers = [
        event
        for dep in app.config["dependencies"]
        for target, event in dep["targets"]
        if target == dropdown._id
    ]
    assert triggers == ["input"]
    assert SECRET not in json.dumps(app.config, ensure_ascii=False, default=str)
    # 일괄 승인은 서명만 보낸다 — 견적 문구가 없다(D110).
    approve = next(
        b
        for b in app.blocks.values()
        if isinstance(b, gr.Button) and b.value == LIBRARY_APPROVE_LABEL
    )
    (click,) = [d for d in app.config["dependencies"] if d["targets"] == [(approve._id, "click")]]
    assert click["inputs"] == [server._signature_state._id]


def test_settings_summaries_show_official_claude_prices_only_for_claude(tmp_path: Path) -> None:
    server = bare_server(tmp_path)
    root = server.workspace_root()
    luna_settings = server.settings_markdown(root)
    luna_analysis = server._analysis_settings_markdown(root)
    assert "$4" not in luna_settings and "Luna 단가 **미확인**" in luna_analysis
    choose_model(server, CLAUDE)
    for text in (server.settings_markdown(root), server._analysis_settings_markdown(root)):
        # D58: 단가(달러)는 견적에서만 — 설정 화면은 단가 상태만 말한다(D57 결정 7)
        assert "Claude Opus 5.5" in text and "공식 단가 확인됨" in text
        assert "$" not in text
    choose_model(server, LUNA)
    assert server.settings_markdown(root) == luna_settings
    assert server._analysis_settings_markdown(root) == luna_analysis


def test_the_advanced_semantic_resume_needs_the_claude_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, sender, luna = make_server(tmp_path, monkeypatch, tmp_path / "none.mov", opener=None)
    choose_model(server, CLAUDE)
    with pytest.raises(gr.Error, match="ANTHROPIC_API_KEY"):
        server.analysis_semantic_resume_view(server.current_signature(), "job-x", True)
    assert (len(sender.calls), len(luna.calls)) == (0, 0)


@needs_ffmpeg
def test_the_ready_text_names_the_model_and_no_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """승인 대기 문구는 모델 이름만 말한다 — 견적·금액을 미리 재지 않는다(D110)."""
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    opener = ClaudeOpener()
    server, sender, luna = make_server(
        tmp_path, monkeypatch, source, opener=opener, auto_approve=False
    )
    measured: list[object] = []
    monkeypatch.setattr(server.raw, "selection_quote", lambda *a, **k: measured.append(a))
    choose_model(server, CLAUDE)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    status = server._quick_status()
    assert "Claude Opus 5.5 분석 준비 완료" in status and "$" not in status
    choose_model(server, LUNA)  # 되돌리면 문구가 곧바로 지금 모델을 말한다
    status = server._quick_status()
    assert "Luna 분석 준비 완료" in status and "$" not in status
    assert measured == []  # 고르기·모델 바꾸기는 범위를 재지 않는다
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (0, 0, 0)


@needs_ffmpeg
def test_luna_results_are_not_shown_done_under_claude_and_nothing_is_re_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    opener = ClaudeOpener()
    server, sender, luna = make_server(tmp_path, monkeypatch, source, opener=opener)
    server.capcut_choose_view(server.current_signature())
    assert settle(server)["stage"] == DONE
    root = server.workspace_root()
    ids = tuple(server._quick["asset_ids"])
    wait_until(lambda: server.library_analysis_button().value == "분석 완료")
    facade = server.raw.review_workspace_facade(root)
    head = facade.load_head().revision
    order = server.raw.latest_global_source_order(root)
    revisions = AnalysisStore(server.raw._workspace(root)).list_revisions()
    evidence = facade.tier_evidence_rows()  # Final Cut 보내기(D56)의 판정 입력
    transcripts = [server.raw.source_transcript(root, asset_id) for asset_id in ids]

    server._selection_auto_approval = None
    choose_model(server, CLAUDE)
    wait_until(lambda: not server.raw.active_jobs(root, (LIBRARY_SCAN_JOB_KIND,)))
    button = wait_until(lambda: (got := server.library_analysis_button()).interactive and got)
    assert button.value == LIBRARY_APPROVE_LABEL and button.interactive
    assert DONE not in server._quick_status()
    assert "분석 모델 바뀜 · 다시 승인 필요" in server._quick_status()  # Claude 결과는 아직 없다
    _, html = server.visual_workbench_render(root)
    assert f'"analysisState":"{DONE}"' not in html and "분석 모델 바뀜" in html
    # 모델을 바꾼 것은 사람의 검토 편집이 아니다 — 머리·순서·분석 기록이 그대로다(D53).
    assert facade.load_head().revision == head
    assert server.raw.latest_global_source_order(root) == order
    assert AnalysisStore(server.raw._workspace(root)).list_revisions() == revisions
    assert facade.tier_evidence_rows() == evidence
    assert [server.raw.source_transcript(root, asset_id) for asset_id in ids] == transcripts

    server._start_quick(root, (), asset_ids=ids)
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    assert "Claude Opus 5.5 분석 준비 완료" in server._quick_status()
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 3, 0)


@needs_ffmpeg
def test_a_missing_claude_key_is_named_before_any_analysis_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    server = RawWorkspaceUi(
        state_path=tmp_path / "ui-state.json", path_chooser=lambda kind: str(source)
    )
    server._startup_credentials = raw_service.EnvironmentCredentials("sk-openai-synthetic", None)
    server._startup_claude = None
    server.create_workspace_view("키 없음", str(tmp_path / "workspace"))
    choose_model(server, CLAUDE)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    root = server.workspace_root()
    message = server.quick_approve_view(server.current_signature())
    assert "ANTHROPIC_API_KEY" in message and server._quick["stage"] == "승인 대기"
    server.library_analysis_view(server.current_signature())
    assert "ANTHROPIC_API_KEY" in server._quick_status()
    kinds = {job.kind for job in server.raw.list_jobs(root)}
    assert LIBRARY_APPROVAL_JOB_KIND not in kinds
    assert not [
        job for job in server.raw.list_jobs(root) if job.input_fingerprint.endswith(":approved")
    ]
    assert AnalysisStore(server.raw._workspace(root)).list_revisions() == ()


@needs_ffmpeg
def test_an_approval_uses_the_model_saved_at_the_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """승인 대기 뒤에 모델이 바뀌어도 승인은 누른 순간의 모델로 잰다 — 옛 견적에 묶이지 않는다."""
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    opener = ClaudeOpener()
    server, sender, luna = make_server(
        tmp_path, monkeypatch, source, opener=opener, auto_approve=False
    )
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    root, signature = server.workspace_root(), server.current_signature()
    assert "Luna 분석 준비 완료" in server._quick_status()
    workflow = server.raw.settings(root)
    # 화면을 거치지 않은 경합 — 설정만 Claude가 됐다.
    claude = replace(workflow, models=replace(workflow.models, luna_model=CLAUDE))
    server.raw.update_settings(root, claude)
    server.quick_approve_view(signature)
    finished = settle(server)
    assert finished["stage"] == DONE
    assert finished["quote"].workflow.models.luna_model == CLAUDE
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 0, 1)


@needs_ffmpeg
def test_a_library_approval_uses_the_model_saved_at_the_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """일괄 승인은 누른 순간의 모델로 잰다 — 화면에 묶인 옛 견적이 없다(D110)."""
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    opener = ClaudeOpener()
    server, sender, luna = make_server(
        tmp_path, monkeypatch, source, opener=opener, auto_approve=False
    )
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    root = server.workspace_root()
    choose_model(server, CLAUDE)
    server.library_analysis_view(server.current_signature())
    assert LIBRARY_APPROVAL_JOB_KIND in {job.kind for job in server.raw.list_jobs(root)}
    wait_until(lambda: len(opener.calls) == 1)
    busy = ("selection_analysis", LIBRARY_APPROVAL_JOB_KIND)
    wait_until(lambda: not server.raw.active_jobs(root, busy))
    assert (len(sender.calls), len(luna.calls)) == (1, 0)


def _keyless_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RawWorkspaceUi, object, object, ClaudeOpener]:
    """처음 실행 — OpenAI 키가 없다. 승인 뒤 실행은 가짜 전송으로만 간다(유료 호출 0회)."""
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    opener = ClaudeOpener()
    server, sender, luna = make_server(
        tmp_path, monkeypatch, source, opener=opener, auto_approve=False
    )
    transport = server._live_transport
    server._live_transport, server._startup_credentials = None, None
    server._startup_claude = raw_service.ClaudeCredentials(api_key=SECRET)
    monkeypatch.setattr(server, "_live_transport_for_action", lambda: transport)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    return server, sender, luna, opener


@needs_ffmpeg
def test_a_first_run_without_a_key_still_reaches_the_claude_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """키 없음 → 연결 안내 → Claude 고름 → `.env` 키로 다시 띄움 → 승인: 갇히지 않는다(R2-1 a).

    화면은 키를 받지 않는다(D102) — 시작 환경에서 찍은 키를 세워 다시 띄운 서버를 흉내 낸다.
    """
    server, sender, luna, opener = _keyless_server(tmp_path, monkeypatch)
    signature = server.current_signature()
    server.quick_approve_view(signature)
    assert server._quick.get("connection_required")
    choose_model(server, CLAUDE)
    for _ in range(3):
        server._quick_status()  # 타이머 — 연결 안내가 상태 줄을 붙잡고 있다
    server._startup_credentials = raw_service.EnvironmentCredentials("sk-openai-synthetic", None)
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (0, 0, 0)
    server.quick_approve_view(signature)
    assert not server._quick.get("connection_required")  # 키가 생겼으니 안내를 걷는다
    finished = settle(server)
    assert finished["stage"] == DONE
    assert finished["quote"].workflow.models.luna_model == CLAUDE
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 0, 1)


@needs_ffmpeg
def test_a_held_library_notice_never_blocks_the_video_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """라이브러리 거절 알림이 상태 칸을 붙잡고 있어도 선택 영상 승인은 갇히지 않는다(R2-1 b)."""
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    opener = ClaudeOpener()
    server, sender, luna = make_server(
        tmp_path, monkeypatch, source, opener=opener, auto_approve=False
    )
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    signature, root = server.current_signature(), server.workspace_root()
    choose_model(server, CLAUDE)
    server._set_approval_notice(root, NOTICE_FAILED, "승인한 분석을 시작하지 못했습니다: 합성")
    assert "합성" in server._quick_status()
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (0, 0, 0)
    server.quick_approve_view(signature)
    assert settle(server)["stage"] == DONE
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 0, 1)
