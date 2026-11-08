"""W-46a(D100 §7a) — 폴더 불러오기 · 추천만 이어붙여 내보내기 · 추천 기준 편집 · 「비추천」 범례.

화면 처리기는 `RawWorkspaceUi`를 직접 만들어 부르고, 배선은 실제 Blocks 의존성(`app.config`)에서
읽는다. 원본은 가짜 조사자(영상 바이트 아님)로, 초안 결과는 보고서 폴더를 심어 실제 서비스
`latest_render_export`가 읽게 한다. 유료 호출 0회 — 승인 단추는 누르지 않는다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
from fastapi.testclient import TestClient  # noqa: E402

from tests.raw.test_browser_runtime import _make_media  # noqa: E402
from ui import chooser as ui_chooser  # noqa: E402
from ui import tk_chooser, ui_facade, workspace_service  # noqa: E402
from ui.workspace_service import (  # noqa: E402
    FINALCUT_ADVANCED_LABEL,
    FINALCUT_LIBRARY_LABEL,
    FINALCUT_SEND_LABEL,
    FINALCUT_SRT_SEND_LABEL,
    FOLDER_BROWSE_LABEL,
    FOLDER_IMPORT_LABEL,
    FOLDER_SUBMIT_LABEL,
    LIBRARY_APPROVE_LABEL,
    RENDER_EXPORT_LABEL,
    RENDER_EXPORT_OPEN_LABEL,
    TIER_CRITERIA_FIELDS,
    TIER_CRITERIA_LABEL,
    RawWorkspaceUi,
)
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.models import IngestError  # noqa: E402

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")


class _Chooser:
    def __init__(self, *responses: str | None) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, kind: str) -> str | None:
        self.calls.append(kind)
        return self.responses.pop(0)


def _fake_inspect(path: Path) -> raw_service.SourceInspection:
    return raw_service.SourceInspection(
        duration_seconds=2.0, width=320, height=180, audio_streams=1, capture_time=None
    )


def _server(tmp_path: Path, *responses: str | None, **kwargs: Any) -> RawWorkspaceUi:
    return RawWorkspaceUi(
        state_path=tmp_path / "ui-state.json",
        inspector=_fake_inspect,
        path_chooser=_Chooser(*responses),
        **kwargs,
    )


def _no_preview(server: RawWorkspaceUi, monkeypatch: pytest.MonkeyPatch) -> None:
    """가짜 바이트라 미리보기를 만들 수 없다 — 미리보기 작업만 빈 작업으로 바꾼다."""
    monkeypatch.setattr(
        server.raw,
        "start_source_preview",
        lambda root, **kwargs: server.raw.submit_job(
            root, "source_preview", "synthetic-no-media", lambda ctx: None
        ),
    )


def _wait(server: RawWorkspaceUi, root: Path, kind: str) -> Any:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        jobs = [j for j in server.raw.list_jobs(root) if j.kind == kind]
        if jobs and jobs[0].status in TERMINAL:
            return jobs[0]
        time.sleep(0.02)
    raise AssertionError(f"{kind} 작업이 끝나지 않았다")


def _trip(tmp_path: Path) -> Path:
    folder = tmp_path / "제주 여행"
    (folder / "day1").mkdir(parents=True)
    (folder / "A001.mp4").write_bytes(b"clip-a")
    (folder / "day1" / "B002.mov").write_bytes(b"clip-b")
    return folder


# ── 폴더 불러오기 ─────────────────────────────────────────────────────────────


def test_submitting_a_folder_creates_its_workspace_and_starts_the_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _trip(tmp_path)
    server = _server(tmp_path)
    _no_preview(server, monkeypatch)
    values = server.folder_import_view(f'  "{folder}"  ')  # 붙여 넣은 따옴표·공백을 걷는다
    root = folder / "_vlogsrt"
    assert server.workspace_root() == root.resolve() and root.is_dir()
    assert "「제주 여행」 폴더를 불러옵니다" in values[0]
    assert values[2] == server.current_signature()  # 화면의 서명 칸이 새 작업공간이다
    assert _wait(server, root, "reference_import").status == "succeeded"
    names = sorted(a.source_name for a in server.raw.list_source_assets(root))
    assert names == ["A001.mp4", "B002.mov"]
    assert not (root / "originals" / "A001.mp4").exists()  # 복사하지 않는다(참조)
    # 마지막 폴더를 기억한다 — 서버를 다시 띄워도 칸이 그 경로로 열린다.
    state = json.loads((tmp_path / "ui-state.json").read_text(encoding="utf-8"))
    assert state["last_folder"] == str(folder) and state["workspace_root"] == str(root.resolve())
    again = _server(tmp_path)
    assert again.last_folder_value() == str(folder)
    # 작업공간 기록만 바꾸는 쓰기도 마지막 폴더를 지우지 않는다.
    again._write_state(root)
    assert again.last_folder_value() == str(folder)


def test_the_library_quote_is_measured_once_the_folder_import_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불러오기는 원본 연결을 백그라운드에 걸고 바로 작업공간을 연다 — 그 순간엔 준비된 영상이
    # 없어 견적을 잴 수 없다. 연결이 끝난 뒤 다시 재지 않으면 승인 단추가 영영 꺼져 있다.
    folder = tmp_path / "촬영"
    _make_media(folder / "A001.mov", audio=True, seconds="2")
    _make_media(folder / "B002.mov", audio=True, seconds="3")
    server = _server(tmp_path)
    _no_preview(server, monkeypatch)
    server.folder_import_view(str(folder))
    root = server.workspace_root()
    assert root is not None
    assert _wait(server, root, "reference_import").status == "succeeded"
    deadline = time.monotonic() + 20
    while root not in server._library_quotes and time.monotonic() < deadline:
        time.sleep(0.02)
    quote = server._library_quotes.get(root)
    assert quote is not None, "연결이 끝났는데 라이브러리 견적을 다시 재지 않았다"
    ready = {a.asset_id for a in server.raw.list_source_assets(root) if a.status == "ready"}
    assert set(quote.asset_ids) == ready and len(ready) == 2
    button = server.library_analysis_button()
    assert button.interactive is True and button.value == LIBRARY_APPROVE_LABEL


def test_an_empty_folder_is_refused_and_creates_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "빈 카드"
    folder.mkdir()
    (folder / "메모.txt").write_text("영상 아님", encoding="utf-8")
    server = _server(tmp_path)
    with pytest.raises(gr.Error, match="불러올 영상이 없다"):
        server.folder_import_view(str(folder))
    assert sorted(p.name for p in folder.iterdir()) == ["메모.txt"]  # 빈 `_vlogsrt`가 없다
    assert not (tmp_path / "빈 카드-vlogsrt").exists()
    assert server.workspace_root() is None
    assert not (tmp_path / "ui-state.json").exists()


def test_the_service_scans_before_it_creates_the_workspace(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    with pytest.raises(IngestError, match="불러올 영상이 없다"):
        raw_service.RawWorkspaceService().start_folder_import(folder)
    assert list(folder.iterdir()) == []


def test_a_workspace_folder_itself_is_not_a_video_folder(tmp_path: Path) -> None:
    service = raw_service.RawWorkspaceService()
    status = service.create_workspace(tmp_path / "ws", "작업")
    with pytest.raises(IngestError, match="작업공간 폴더"):
        service.start_folder_import(Path(status.workspace_root))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "경로를 적거나"),
        ("상대/경로", "전체 경로"),
        ("/없는/폴더/여기", "폴더가 없습니다"),
    ],
)
def test_bad_folder_text_is_refused_before_any_scan(
    tmp_path: Path, text: str, message: str
) -> None:
    server = _server(tmp_path)
    with pytest.raises(gr.Error, match=message):
        server.folder_import_view(text)
    assert server.workspace_root() is None


def test_a_final_cut_library_path_points_to_the_advanced_drawer(tmp_path: Path) -> None:
    library = tmp_path / "제주.fcpbundle"
    (library / "2026-10-07" / "CurrentVersion.fcpevent").mkdir(parents=True)
    with pytest.raises(gr.Error, match="고급 · Final Cut"):
        _server(tmp_path).folder_import_view(str(library))


def test_browse_fills_the_path_then_imports_and_cancel_changes_nothing(
    tmp_path: Path, local_socket: None
) -> None:
    folder = _trip(tmp_path)
    server = _server(tmp_path, str(folder), None)
    assert server.folder_browse_view() == str(folder)
    with pytest.raises(gr.Error, match="취소"):
        server.folder_browse_view()
    assert server._path_chooser.calls == [ui_chooser.FOLDER_KIND, ui_chooser.FOLDER_KIND]
    assert server.workspace_root() is None
    app = server.build_blocks()
    deps = app.config["dependencies"]
    by_fn = {dep["id"]: app.fns[dep["id"]].fn for dep in deps}
    (browse,) = [d for d in deps if by_fn[d["id"]] == server.folder_browse_view]
    assert browse["outputs"] == [server._folder_path_box._id]
    follow = [d for d in deps if d.get("trigger_after") == browse["id"]]
    (chained,) = [d for d in follow if by_fn[d["id"]] == server.folder_import_view]
    assert chained["trigger_only_on_success"] is True  # 취소(오류)면 불러오지 않는다
    imports = [d for d in deps if by_fn[d["id"]] == server.folder_import_view]
    assert len(imports) == 3  # 단추 · 경로 칸 Enter · 찾아보기 뒤
    for dep in imports:
        assert dep["inputs"] == [server._folder_path_box._id]
        assert dep["outputs"][0] == server._capcut_status._id
        assert server._signature_state._id in dep["outputs"]


def test_the_browse_button_hides_without_a_native_chooser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ui_chooser.importlib.util, "find_spec", lambda name: None)
    assert ui_chooser.native_chooser_available() is False
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.build_blocks()
    assert server._folder_browse_button.visible is False
    assert server._folder_path_box.interactive is not False  # 경로 입력은 남는다
    monkeypatch.setattr(ui_chooser.importlib.util, "find_spec", lambda name: object())
    assert ui_chooser.native_chooser_available() is True
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ui_chooser.importlib.util, "find_spec", lambda name: None)
    assert ui_chooser.native_chooser_available() is True  # macOS는 NSOpenPanel


# ── 선택 창(macOS 밖) ─────────────────────────────────────────────────────────


def test_the_tk_child_shares_the_kind_names_and_refuses_unknown_kinds() -> None:
    assert tk_chooser.KINDS == (
        ui_chooser.WORKSPACE_KIND,
        ui_chooser.FOLDER_KIND,
        ui_chooser.VIDEO_KIND,
        ui_chooser.SRT_KIND,
    )
    assert Path(tk_chooser.__file__) == ui_chooser.TK_CHOOSER_SCRIPT
    # 모르는 종류는 창을 열기 전에 거절한다 — 격리 실행(-I)이라 ui 패키지 없이 돈다.
    completed = subprocess.run(
        [sys.executable, "-I", str(ui_chooser.TK_CHOOSER_SCRIPT), "bogus"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == tk_chooser.USAGE_EXIT
    assert "모르는 선택기 종류" in json.loads(completed.stdout)["error"]


def test_the_tk_chooser_reads_the_child_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        out = answers.pop(0)
        return subprocess.CompletedProcess(argv, out[0], stdout=out[1], stderr="")

    answers = [
        (0, json.dumps({"path": "C:\\촬영\\제주"}, ensure_ascii=True) + "\n"),
        (0, json.dumps({"path": None}) + "\n"),
        (0, json.dumps({"error": "TclError: no display"}) + "\n"),
        (1, ""),
    ]
    monkeypatch.setattr(ui_chooser.subprocess, "run", run)
    assert ui_chooser.tkinter_path_chooser(ui_chooser.FOLDER_KIND) == "C:\\촬영\\제주"
    assert seen[0] == [
        sys.executable,
        "-I",
        str(ui_chooser.TK_CHOOSER_SCRIPT),
        ui_chooser.FOLDER_KIND,
    ]
    assert ui_chooser.tkinter_path_chooser(ui_chooser.FOLDER_KIND) is None  # 취소
    with pytest.raises(ui_chooser.ChooserError, match="no display"):
        ui_chooser.tkinter_path_chooser(ui_chooser.FOLDER_KIND)
    with pytest.raises(ui_chooser.ChooserError, match="종료코드 1"):
        ui_chooser.tkinter_path_chooser(ui_chooser.FOLDER_KIND)
    with pytest.raises(ui_chooser.ChooserError, match="모르는"):
        ui_chooser.tkinter_path_chooser("bogus")
    monkeypatch.setattr(sys, "platform", "win32")
    answers.append((0, json.dumps({"path": "D:\\a"}) + "\n"))
    assert ui_chooser.native_path_chooser(ui_chooser.FOLDER_KIND) == "D:\\a"


# ── 명령 줄 · 서랍 배치 ───────────────────────────────────────────────────────


def _ancestors(block: Any) -> list[Any]:
    out = []
    node = getattr(block, "parent", None)
    while node is not None:
        out.append(node)
        node = getattr(node, "parent", None)
    return out


def _built(tmp_path: Path) -> tuple[RawWorkspaceUi, Any, dict[Any, Any], dict[Any, Any]]:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("배치", str(tmp_path / "workspace"))
    app = server.build_blocks()
    by_id = {getattr(b, "elem_id", None): b for b in app.blocks.values()}
    buttons = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}
    return server, app, by_id, buttons


def test_final_cut_import_and_export_live_only_in_the_drawers(
    tmp_path: Path, local_socket: None
) -> None:
    server, app, by_id, buttons = _built(tmp_path)
    bar = by_id["vl-command-bar"]
    assert buttons[FOLDER_IMPORT_LABEL].parent is bar
    assert buttons[RENDER_EXPORT_LABEL].parent is bar
    assert buttons[RENDER_EXPORT_LABEL].variant == "primary"
    for label in (
        FINALCUT_LIBRARY_LABEL,
        FINALCUT_SEND_LABEL,
        FINALCUT_SRT_SEND_LABEL,
        "Final Cut 라이브러리에서 가져오기",
        "선택한 오리지널 연결",
    ):
        parents = _ancestors(buttons[label])
        assert bar not in parents, label
        accordion = next(p for p in parents if isinstance(p, gr.Accordion))
        assert accordion.label == FINALCUT_ADVANCED_LABEL and accordion.open is False, label
        assert any(isinstance(p, gr.Sidebar) for p in parents), label
    # 옛 명령 줄 단추 이름은 남지 않는다.
    assert "라이브러리 불러오기" not in buttons
    right = by_id["settings-drawer"]
    assert right in _ancestors(buttons[FINALCUT_SEND_LABEL])
    assert right in _ancestors(by_id[workspace_service.TIER_CRITERIA_ID])
    left = by_id["project-tools"]
    for label in (FOLDER_BROWSE_LABEL, FOLDER_SUBMIT_LABEL):
        assert left in _ancestors(buttons[label]), label
    # 보내기 처리기·입출력은 그대로다(옮긴 것은 자리뿐).
    deps = app.config["dependencies"]
    (send,) = [d for d in deps if d["targets"] == [(buttons[FINALCUT_SEND_LABEL]._id, "click")]]
    assert app.fns[send["id"]].fn == server.finalcut_send_view
    assert send["outputs"] == [server._capcut_status._id]
    # 「폴더 불러오기」는 서버를 부르지 않고 서랍만 연다.
    (opener,) = [d for d in deps if d["targets"] == [(buttons[FOLDER_IMPORT_LABEL]._id, "click")]]
    assert opener["backend_fn"] is False and "project-tools" in opener["js"]
    # 견적·승인 문은 그대로 보인다(D44).
    approve = buttons[workspace_service.LIBRARY_APPROVE_LABEL]
    assert approve.parent is bar and approve.visible is True
    assert by_id["library-analysis-scope"].visible is not False


def test_flow_steps_and_empty_states_talk_about_folders(tmp_path: Path) -> None:
    assert ui_facade.FLOW_STEP_LABELS == ("폴더 불러오기", "분석 승인", "검토", "내보내기")
    exported = ui_facade.flow_steps(
        ui_facade.FlowCounts(imported=2, analysed=2, proposals=1, exported=True)
    )
    assert [s.state for s in exported] == ["done"] * 4 and exported[3].sub == "내보냄"
    assert exported[2].sub == "미확인 비추천 1"
    assert "Final Cut" not in workspace_service.NO_WORKSPACE_TEXT
    assert "폴더" in workspace_service.NO_WORKSPACE_TEXT
    shell, workbench = RawWorkspaceUi(
        state_path=tmp_path / "ui-state.json"
    ).visual_workbench_render(None)
    assert workspace_service.FOLDER_FIRST_NOTE in shell
    assert workspace_service.FOLDER_FIRST_NOTE in workbench
    assert "Final Cut 라이브러리" not in shell + workbench


def test_the_red_tier_reads_not_recommended_on_screen() -> None:
    legend = ui_facade.TIER_LEGEND_HTML
    assert "▨ 비추천" in legend and "제외 제안</span>" not in legend
    js = ui_facade.VISUAL_BOOTSTRAP_JS
    assert 'red: "비추천"' in js and 'red: "✕ 비추천"' in js
    assert 'red: "제외 제안"' not in js
    # 내부 이름과 Final Cut 키워드는 그대로다(표시만 바뀐다).
    assert workspace_service.UNREVIEWED_KEYWORD == "제외 제안(미검토)"


# ── 추천만 이어붙여 내보내기 ──────────────────────────────────────────────────


def test_the_export_button_starts_the_render_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    server, app, _by_id, buttons = _built(tmp_path)
    root = server.workspace_root()
    calls: list[Path] = []

    def start(at: Path, digest: str | None = None, client: object = None) -> raw_service.JobHandle:
        assert (digest, client) == (None, None)  # 영어 자막이 꺼져 있으면 살 것이 없다(D105)
        calls.append(at)
        return raw_service.JobHandle(job_id="job-1", reattached=len(calls) > 1)

    monkeypatch.setattr(server.raw, "start_render_export", start)
    signature = server.current_signature()
    assert server.render_export_view(signature) == workspace_service.RENDER_EXPORT_STARTED_TEXT
    assert server.render_export_view(signature) == workspace_service.RENDER_EXPORT_ATTACHED_TEXT
    assert calls == [root, root]
    deps = app.config["dependencies"]
    clicks = [d for d in deps if d["targets"] == [(buttons[RENDER_EXPORT_LABEL]._id, "click")]]
    (click,) = [d for d in clicks if app.fns[d["id"]].fn == server.render_export_view]
    assert click["inputs"] == [server._signature_state._id, server._english_quote_md._id]
    assert click["outputs"] == [server._capcut_status._id]
    assert any(app.fns[d["id"]].fn == server.english_quote_markdown for d in clicks)
    tails = [d for d in deps if d.get("trigger_after") == click["id"]]
    assert any(d.get("js") == ui_facade.ACTIVITY_REPLY_JS for d in tails)  # 답은 활동 칸으로
    assert any(app.fns[d["id"]].fn == server.render_export_card_tick for d in tails)
    assert workspace_service.ACTIVITY_JOB_LABELS["render_export"] == "추천만 이어붙이기"


def test_import_export_and_approve_wake_the_activity_poller(
    tmp_path: Path, local_socket: None
) -> None:
    """돈 일을 거는 단추는 누르는 순간 서버 쪽 고리를 깨운다(D101) — 동기 스캔 동안에도 줄이 흐른다.

    앞 단계는 입력을 그대로 넘긴다(처리기·입력·출력 무변경).
    """
    server, app, _by_id, buttons = _built(tmp_path)
    deps = app.config["dependencies"]
    imports = [
        d for d in deps if d["id"] in app.fns and app.fns[d["id"]].fn == (server.folder_import_view)
    ]
    assert len(imports) == 3  # 경로 칸 Enter · 「이 폴더 불러오기」 · 「찾아보기」 뒤
    for dep in imports:
        assert dep["js"] == ui_facade.ACTIVITY_WAKE_JS
        assert dep["inputs"] == [server._folder_path_box._id]
    clicks = [d for d in deps if d["targets"] == [(buttons[RENDER_EXPORT_LABEL]._id, "click")]]
    (export,) = [d for d in clicks if app.fns[d["id"]].fn == server.render_export_view]
    assert export["js"] == ui_facade.EXPORT_WAKE_JS  # 입력 둘을 그대로 넘긴다(D105)
    assert "__vlServer.wake(true)" in ui_facade.EXPORT_WAKE_JS
    assert ui_facade.EXPORT_WAKE_JS.rstrip().endswith("return [signature, englishQuote]; }")
    assert "__vlServer.wake(true)" in ui_facade.ACTIVITY_WAKE_JS
    assert ui_facade.ACTIVITY_WAKE_JS.rstrip().endswith("return value; }")
    assert "window.__vlServer.wake(true)" in ui_facade.APPROVE_GUARD_JS


def test_the_english_box_saves_the_flag_and_shows_a_quote_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    """D105 — 칸을 켜면 표지를 저장하고 견적 한 줄을 그린다. 끄면 빈 줄이고 살 것이 없다."""
    server, app, by_id, _buttons = _built(tmp_path)
    root = server.workspace_root()
    signature = server.current_signature()
    assert by_id[workspace_service.ENGLISH_SUBTITLES_ID].label == (
        workspace_service.ENGLISH_SUBTITLES_LABEL
    )
    assert server.english_quote_markdown(root) == ""
    line = server.english_subtitles_view(signature, True)
    assert server.raw.english_subtitles(root) is True
    text = server.raw.english_quote(root).text
    assert line == ui_facade.literal_markdown(text) and text.startswith("영어 자막")
    assert ui_facade.unliteral_markdown(line) == text
    assert server.english_subtitles_view(signature, False) == ""
    assert server.raw.english_subtitles(root) is False


def test_the_export_click_is_bound_to_the_english_quote_it_showed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    """D105 — 살 것이 있으면 보인 문구가 지금 견적과 같아야 하고, 그때만 digest·클라이언트를
    넘긴다."""
    server, _app, _by_id, _buttons = _built(tmp_path)
    quote = raw_service.EnglishQuote(
        model="gemini-3.8-flash", sources=1, missing_sources=1, missing_lines=4,
        input_tokens=900, output_tokens=16000, usd="0.07", priced=True,
        text="영어 자막: 영상 1개 자막 4줄을 옮깁니다(공식 단가로 약 $0.07).", digest="d" * 64,
    )  # fmt: skip
    seen: list[tuple[str | None, object]] = []
    monkeypatch.setattr(server.raw, "english_subtitles", lambda at: True)
    monkeypatch.setattr(server.raw, "english_quote", lambda at: quote)
    monkeypatch.setattr(server, "_live_transport_for_action", lambda: _Transport())

    def start(at: Path, digest: str | None = None, client: object = None) -> raw_service.JobHandle:
        seen.append((digest, client))
        return raw_service.JobHandle(job_id="job-1", reattached=False)

    monkeypatch.setattr(server.raw, "start_render_export", start)
    signature = server.current_signature()
    with pytest.raises(gr.Error, match="견적이 바뀌었습니다"):
        server.render_export_view(signature, "영어 자막: 다른 문구")
    assert seen == []
    server.render_export_view(signature, ui_facade.literal_markdown(quote.text))
    assert seen == [("d" * 64, "client:gemini-3.8-flash")]


class _Transport:
    def semantic_client(self, model: str) -> str:
        return f"client:{model}"


def test_the_export_button_without_a_workspace_answers_folder_first(tmp_path: Path) -> None:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    assert server.render_export_view(None) == workspace_service.LIBRARY_FIRST_TEXT


SUMMARY = {
    "folder": "/ws/export/20261028T010203Z",
    "video": "/ws/export/20261028T010203Z/draft.mp4",
    "srt": "/ws/export/20261028T010203Z/draft.srt",
    "report": "/ws/export/20261028T010203Z/render_report.json",
    "media_file_id": "media_abc",
    "created_at": "2026-10-28T01:02:03Z",
    "encoder": "h264_videotoolbox",
    "total_seconds": 192.44,
    "parts": 12,
    "dropped": 5,
    "unreviewed_red_dropped": 3,
    "unsupported": ["IMG_0042.MOV"],
    "cues": {},
}


def test_the_result_card_names_paths_warning_player_and_unsupported() -> None:
    card = ui_facade.render_export_card_html(SUMMARY)
    assert "3분 12.4초" in card and "조각 12개" in card and "h264_videotoolbox" in card
    assert 'role="alert">확인하지 않은 비추천 구간 3곳이 빠졌습니다 — 보고서에 목록이' in card
    assert 'href="/media/media_abc"' in card and 'src="/media/media_abc"' in card
    for key in ("video", "srt", "report"):
        assert SUMMARY[key] in card
    assert "분석 지원 밖이라 빠진 원본: IMG_0042.MOV" in card
    calm = ui_facade.render_export_card_html({**SUMMARY, "unreviewed_red_dropped": 0})
    assert "vl-export-warn" not in calm and "IMG_0042.MOV" in calm
    hostile = ui_facade.render_export_card_html({**SUMMARY, "srt": "/a/<b>&.srt"})
    assert "/a/&lt;b&gt;&amp;.srt" in hostile and "<b>" not in hostile
    assert ui_facade.render_export_card_html(None) == ""
    running = ui_facade.render_export_card_html(None, running=True)
    assert ui_facade.RENDER_EXPORT_RUNNING_TEXT in running
    failed = ui_facade.render_export_card_html(SUMMARY, failure="조각 3 실패")
    assert "지난 내보내기 실패 — 조각 3 실패" in failed and "draft.mp4" in failed
    assert ui_facade.format_duration(8.24) == "8.2초"
    assert "영어 자막" not in card  # 끈 초안에는 영어 줄이 없다(D105)
    english = {
        **SUMMARY,
        "english_srt": "/ws/export/x/draft.en.srt",
        "english": {"status": "written"},
    }
    assert "<dt>영어 자막</dt><dd>/ws/export/x/draft.en.srt</dd>" in (
        ui_facade.render_export_card_html(english)
    )
    broken = {
        **SUMMARY,
        "english": {"status": "failed", "untranslated": [{"name": "B.mp4", "reason": "빠졌다"}]},
    }
    warned = ui_facade.render_export_card_html(broken)
    assert "영어 자막(draft.en.srt)은 만들지 못했습니다" in warned and "B.mp4: 빠졌다" in warned


def _plant_export(server: RawWorkspaceUi, root: Path, *, red: int) -> Path:
    """보고서가 있는 초안 폴더 하나 — 실제 서비스가 읽는 모양(영상은 재생기 시험용 바이트)."""
    folder = root / "export" / "20261028T010203Z"
    folder.mkdir(parents=True)
    (folder / "draft.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))
    (folder / "draft.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n안녕\n", encoding="utf-8")
    report = {
        "created_at": "2026-10-28T01:02:03Z",
        "encoder": "libx264",
        "total_seconds": {"num": 41, "den": 5},
        "parts": [{}, {}, {}, {}],
        "dropped": [{}] * 3,
        "unreviewed_red_dropped": red,
        "unsupported": [{"name": "VFR.MOV"}],
    }
    (folder / "render_report.json").write_text(json.dumps(report), encoding="utf-8")
    server.raw.register_media(root, folder / "draft.mp4", "artifact")
    return folder


def test_the_card_tick_reads_the_latest_export_and_serves_its_media(
    tmp_path: Path, local_socket: None
) -> None:
    opened: list[Path] = []
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json", folder_opener=opened.append)
    server.create_workspace_view("초안", str(tmp_path / "workspace"))
    root = Path(server.workspace_root())
    signature = server.current_signature()
    empty, button, key = server.render_export_card_tick(signature, None)
    assert empty == "" and button.visible is False and key == (signature, None, None)
    assert server.render_export_card_tick(signature, key) == (gr.skip(),) * 3
    with pytest.raises(gr.Error, match="아직 내보낸 초안이 없습니다"):
        server.render_export_open_view(signature)
    folder = _plant_export(server, root, red=2)
    card, button, key = server.render_export_card_tick(signature, None)  # 다시 연 화면
    assert "8.2초" in card and "조각 4개" in card and "libx264" in card
    assert "확인하지 않은 비추천 구간 2곳이 빠졌습니다" in card and "VFR.MOV" in card
    assert str(folder / "draft.srt") in card and str(folder / "render_report.json") in card
    assert button.visible is True
    media_id = server.raw.latest_render_export(root)["media_file_id"]
    assert media_id and f'src="/media/{media_id}"' in card
    client = TestClient(server.build_fastapi(mount_blocks=False), base_url="http://127.0.0.1")
    response = client.get(f"/media/{media_id}")
    assert response.status_code == 200 and response.content.startswith(b"\x00\x00\x00\x18ftyp")
    assert "열었습니다" in server.render_export_open_view(signature)
    assert opened == [folder.resolve()]
    # 같은 열쇠면 다시 보내지 않고, 닫으면 카드를 비운다.
    assert server.render_export_card_tick(signature, key) == (gr.skip(),) * 3
    server._set_root(None)
    cleared = server.render_export_card_tick(signature, key)
    assert cleared[0] == "" and cleared[2] is None


def test_the_export_step_is_done_once_a_draft_report_exists(tmp_path: Path) -> None:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("초안", str(tmp_path / "workspace"))
    root = Path(server.workspace_root())
    assert server._flow_counts(root).exported is False
    (root / "export" / "20261027T000000Z").mkdir(parents=True)  # 보고서 없는 자리(취소·실패)
    assert server._flow_counts(root).exported is False
    _plant_export(server, root, red=0)
    assert server._flow_counts(root).exported is True


# ── 추천 기준 ─────────────────────────────────────────────────────────────────


def _criteria_server(tmp_path: Path) -> tuple[RawWorkspaceUi, str, Path]:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("기준", str(tmp_path / "workspace"))
    return server, str(server.current_signature()), Path(server.workspace_root())


def _shown(server: RawWorkspaceUi) -> list[Any]:
    return list(server.tier_criteria_controls()[1:])


def test_the_criteria_fields_are_the_editable_keys_in_order() -> None:
    assert [f.key for f in TIER_CRITERIA_FIELDS] == list(raw_service.EDITABLE_TIER_KEYS)
    assert {f.kind for f in TIER_CRITERIA_FIELDS} == {"number", "toggle", "choices"}
    choices = raw_service.tier_criteria_choices()["visual_issue_caps"]
    assert ("어두움", "dark") in choices and ("과노출", "overexposed") in choices


def test_criteria_apply_regrades_and_marks_the_change(tmp_path: Path) -> None:
    server, signature, root = _criteria_server(tmp_path)
    status, *values = server.tier_criteria_controls()
    assert status == workspace_service.TIER_DEFAULT_TEXT
    assert values == [
        server.raw.tier_settings_view(root)["defaults"][f.key] for f in TIER_CRITERIA_FIELDS
    ]
    assert server._criteria_changed(root) is False
    generation = server._capcut_render_generation
    edited = list(values)
    edited[0] = 2  # 추천 최소 길이 3 → 2(정수도 실수 칸으로 받는다)
    edited[7] = ["overexposed", "blur"]  # 차례는 종류 차례로 맞춘다
    reply = server.tier_apply_view(signature, *edited)
    assert reply[0].startswith(workspace_service.TIER_APPLIED_TEXT)
    assert "추천 최소 길이(초) 2(기본 3)" in reply[0] and "흐림·과노출" in reply[0]
    view = server.raw.tier_settings_view(root)
    assert view["overrides"] == {
        "recommended_min_seconds": 2.0,
        "visual_issue_caps": ["blur", "overexposed"],
    }
    assert list(reply[1:]) == [view["values"][f.key] for f in TIER_CRITERIA_FIELDS]
    assert server._capcut_render_generation == generation + 1  # 레일을 다시 그린다
    assert server._criteria_changed(root) is True
    _shell, workbench = server.visual_workbench_render(root)
    assert 'data-vl-criteria="changed"' in workbench
    # 보이지 않는 칸의 덮기는 「적용」이 지우지 않는다.
    server.raw.set_tier_settings(root, {**view["overrides"], "same_text_run_min": 4})
    server.tier_apply_view(signature, *_shown(server))
    assert server.raw.tier_settings_view(root)["overrides"]["same_text_run_min"] == 4


def test_an_invalid_criterion_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    server, signature, root = _criteria_server(tmp_path)
    values = _shown(server)
    bad = list(values)
    bad[1] = 7.0  # 밀도는 0–1
    reply = server.tier_apply_view(signature, *bad)
    assert reply[0].startswith("적용하지 못했습니다 —")
    assert all(v == gr.skip() for v in reply[1:])  # 칸은 고칠 수 있게 그대로
    assert server.raw.tier_settings_view(root)["overrides"] == {}
    empty = list(values)
    empty[0] = None
    reply = server.tier_apply_view(signature, *empty)
    assert "「추천 최소 길이(초)」에 수를 넣으세요" in reply[0]
    assert server.raw.tier_settings_view(root)["overrides"] == {}


def test_criteria_reset_one_field_and_all_fields(tmp_path: Path) -> None:
    server, signature, root = _criteria_server(tmp_path)
    values = _shown(server)
    values[3] = False
    values[6] = 0.75
    server.tier_apply_view(signature, *values)
    assert server._criteria_changed(root) is True
    assert server.tier_default_value("segment_green_share") == 0.5
    defaults = server.tier_defaults_view()
    assert defaults[0] == workspace_service.TIER_PENDING_TEXT
    assert server.raw.tier_settings_view(root)["overrides"] != {}  # 「적용」 전에는 쓰지 않는다
    reply = server.tier_apply_view(signature, *defaults[1:])
    assert reply[0].endswith(workspace_service.TIER_DEFAULT_TEXT)
    assert server.raw.tier_settings_view(root)["overrides"] == {}
    assert server._criteria_changed(root) is False
    _shell, workbench = server.visual_workbench_render(root)
    assert "data-vl-criteria" not in workbench  # 기본 기준이면 예전 바이트 그대로


def test_criteria_panel_wiring_and_badge(tmp_path: Path, local_socket: None) -> None:
    server, app, by_id, buttons = _built(tmp_path)
    panel = by_id[workspace_service.TIER_CRITERIA_ID]
    assert isinstance(panel, gr.Accordion) and panel.label == TIER_CRITERIA_LABEL
    assert len(server._tier_fields) == len(TIER_CRITERIA_FIELDS)
    for field in server._tier_fields:
        assert panel in _ancestors(field)
    deps = app.config["dependencies"]
    (apply,) = [d for d in deps if app.fns[d["id"]].fn == server.tier_apply_view]
    assert apply["inputs"] == [server._signature_state._id, *(f._id for f in server._tier_fields)]
    assert apply["outputs"] == [server._tier_status_md._id, *(f._id for f in server._tier_fields)]
    legend = ui_facade.TIER_LEGEND_HTML
    assert f'id="{ui_facade.CRITERIA_BADGE_ID}"' in legend and ">기준 바꿈</span>" in legend
    assert " hidden>기준 바꿈" in legend  # 기본은 숨는다


_PAINT_FLOW = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('var paintFlow = function (fresh) {');
const end = src.indexOf('var wireCommitted = function', start);
const body = src.slice(start, end);
const nodes = {
  'vl-flow': { innerHTML: '' }, 'vl-project-name': { textContent: '' },
  'vl-library-close': { hidden: false }, 'vl-criteria-badge': { hidden: true },
};
const W = { __vlVisualDoc: { getElementById: (id) => nodes[id] || null } };
const paintFlow = new Function('W', body + '; return paintFlow;')(W);
const fresh = (attrs) => ({ getAttribute: (n) => (n in attrs ? attrs[n] : null),
  hasAttribute: (n) => n in attrs });
const out = [];
paintFlow(fresh({ 'data-vl-flow': 'x', 'data-vl-project': 'p', 'data-vl-criteria': 'changed' }));
out.push(nodes['vl-criteria-badge'].hidden);
paintFlow(fresh({ 'data-vl-flow': 'x', 'data-vl-project': 'p' }));
out.push(nodes['vl-criteria-badge'].hidden);
paintFlow(fresh({}));  // 머리줄 값이 없는 그리기는 표지를 건드리지 않는다
out.push(nodes['vl-criteria-badge'].hidden);
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_the_badge_follows_the_workbench_value(tmp_path: Path) -> None:
    harness = tmp_path / "paint.js"
    harness.write_text(_PAINT_FLOW, encoding="utf-8")
    script = tmp_path / "visual.js"
    script.write_text(ui_facade.VISUAL_BOOTSTRAP_JS, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(script)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert json.loads(completed.stdout) == [False, True, True]


def test_the_export_open_button_is_named_apart_from_the_old_folder_button() -> None:
    assert (
        RENDER_EXPORT_OPEN_LABEL != "폴더 열기"
    )  # 옛 「폴더 열기」(확정 내보내기)와 겹치지 않는다
