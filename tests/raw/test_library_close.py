"""「라이브러리 닫기」(W-24 세션 F, D64) — 불러온 라이브러리를 닫고 빈 화면으로 돌아간다.

닫기는 화면의 선택만 거둔다: 원본·작업공간 DB·분석 결과는 그대로이고, 같은 라이브러리를 다시
불러오면 이어서 연다. 작업이 도는 동안에는 닫지 않는다(아무것도 바꾸지 않고 까닭을 답한다).
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from tests.raw.test_workspace_chooser_ui import _fcp_library, _ScriptedChooser  # noqa: E402
from ui import ui_facade, workspace_service  # noqa: E402
from ui.workspace_service import STATUS_SENT_CELLS  # noqa: E402
from vlogsrt.project.raw import workspace as workspace_module  # noqa: E402
from vlogsrt.project.raw.jobs import JobContext  # noqa: E402
from vlogsrt.project.raw.service import SourceInspection  # noqa: E402

CELLS = len(STATUS_SENT_CELLS)


@pytest.fixture(autouse=True)
def _loopback(local_socket: None) -> None:
    """Blocks 조립이 loopback 소켓을 연다 — 묶음 전체가 같은 허용을 쓴다."""


def _inspect(path: Path) -> SourceInspection:
    return SourceInspection(
        duration_seconds=2.0, width=320, height=180, audio_streams=1, capture_time=None
    )


def _server(state: Path, *responses: object) -> workspace_service.RawWorkspaceUi:
    return workspace_service.RawWorkspaceUi(
        port=7860, state_path=state, inspector=_inspect, path_chooser=_ScriptedChooser(*responses)
    )


def _wait_until(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _opened(tmp_path: Path) -> tuple[workspace_service.RawWorkspaceUi, Path, Path]:
    """합성 FCP 라이브러리를 연 서버 — 여는 길이 건 작업(견적 등)이 끝날 때까지 기다린다."""
    library = _fcp_library(tmp_path)
    server = _server(tmp_path / "state.json")
    server.build_blocks()
    server.open_fcp_library_view(library, originals_only=False)
    root = server.workspace_root()
    assert root is not None
    assert _wait_until(lambda: not server.raw.all_active_jobs(root))
    return server, root, library


def _db_key(root: Path) -> Path:
    return (root / workspace_module.DB_NAME).expanduser().resolve()


def _blocked_job(
    server: workspace_service.RawWorkspaceUi, root: Path, kind: str = "probe"
) -> threading.Event:
    release = threading.Event()

    def blocked(context: JobContext) -> None:
        release.wait(timeout=20)

    handle = server.raw.submit_job(root, kind, "close-refusal", blocked)
    assert _wait_until(lambda: server.raw.get_job(root, handle.job_id).status == "running")
    server._probe_job_id = handle.job_id  # type: ignore[attr-defined]
    return release


def _close_dep(app: gr.Blocks, server: workspace_service.RawWorkspaceUi) -> dict[str, Any]:
    source = server._library_close_button
    for dep in app.config["dependencies"]:
        if dep["targets"] == [(source._id, "click")]:
            return dep
    raise AssertionError("닫기 숨은 단추에 클릭 배선이 없다")


# ── 닫으면 빈 화면 · 상태 파일 비움 · 다음 시작도 빈 화면 ─────────────────────────


def test_close_returns_the_empty_page_and_forgets_the_saved_workspace(tmp_path: Path) -> None:
    server, root, _library = _opened(tmp_path)
    state = tmp_path / "state.json"
    assert json.loads(state.read_text(encoding="utf-8"))["workspace_root"] == str(root)
    signature = server.current_signature()
    server._library_quotes[root] = object()  # 닫으면 그 작업공간 몫의 화면 기억이 걷힌다
    server._project_names[root] = "제주"
    server._approval_notice[root] = object()  # type: ignore[assignment]
    server._quick_tasks["closed-token"] = {"root": root, "stage": "승인 대기"}

    values = server.close_library_view(signature)

    reply, view = values[0], values[1:]
    assert reply == workspace_service.LIBRARY_CLOSED_TEXT
    selection = len(server._selection_outputs())
    workspace_view, fcp, cells, memory = (
        view[:selection],
        view[selection : selection + 3],
        view[selection + 3 : selection + 3 + CELLS],
        view[selection + 3 + CELLS :],
    )
    assert workspace_view[1] is None  # 서명 칸이 비었다 — 이 탭의 틱은 이제 건너뛴다
    shell, workbench = workspace_view[15], workspace_view[16]
    assert workspace_service.FOLDER_FIRST_NOTE in shell  # 폴더 작업대의 빈 화면(W-46a)
    assert workspace_service.FOLDER_FIRST_NOTE in workbench
    assert "data-vl-closed" in workbench  # 머리줄의 닫기 단추를 숨기라는 표지
    assert f'data-vl-project="{ui_facade.PROJECT_EMPTY_LABEL}"' in workbench
    assert workspace_view[-3:] == ("", "", "")  # 경로 표시 셋도 빈다
    assert all(update.visible is False for update in fcp)  # FCP 연결 칸도 걷힌다
    button, summary, _progress, note, usage = cells
    assert button.interactive is False  # 승인 단추는 누를 수 없다
    assert summary == ui_facade.literal_markdown(
        "폴더를 불러오면 분석 범위와 요청 상한을 안내합니다."
    )
    assert note.visible is False and usage == ""
    assert list(memory) == [None] * CELLS  # 1초 틱의 「마지막으로 보낸 값」을 잊는다

    assert server.workspace_root() is None and server.current_signature() is None
    assert root not in server._library_quotes and root not in server._project_names
    assert root not in server._approval_notice
    # 영상별 작업 기록은 남는다 — 지우면 다시 열 때 「승인 대기」가 「중단」으로 되살아난다.
    assert server._quick_tasks["closed-token"]["stage"] == "승인 대기"
    assert "workspace_root" not in json.loads(state.read_text(encoding="utf-8"))
    assert (root / workspace_module.DB_NAME).is_file()  # 작업공간 자체는 그대로다

    restarted = _server(state)
    note_text = restarted.startup()
    assert "이전 작업공간 기록이 없다" in note_text
    assert restarted.workspace_root() is None


def test_startup_after_close_is_empty_with_the_state_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기본 상태 파일 자리(`STATE_ENV`)를 쓰는 서버도 닫은 뒤 다시 띄우면 빈 화면이다."""
    state = tmp_path / "env-state.json"
    monkeypatch.setenv(workspace_service.STATE_ENV, str(state))
    server = workspace_service.RawWorkspaceUi(port=7860, inspector=_inspect)
    assert server.state_path == state
    server.create_workspace_view("닫을 프로젝트", str(tmp_path / "project"))
    root = server.workspace_root()
    assert root is not None
    assert _wait_until(lambda: not server.raw.all_active_jobs(root))
    server.close_library_view(server.current_signature())
    again = workspace_service.RawWorkspaceUi(port=7860, inspector=_inspect)
    assert "이전 작업공간 기록이 없다" in again.startup()
    assert again.workspace_root() is None


# ── 도는 작업이 있으면 닫지 않는다 — 아무것도 바꾸지 않는다 ───────────────────────


def _assert_unchanged(
    server: workspace_service.RawWorkspaceUi, root: Path, values: tuple, state: Path
) -> None:
    assert all(isinstance(value, type(gr.skip())) for value in values[1:])
    assert server.workspace_root() == root
    assert json.loads(state.read_text(encoding="utf-8"))["workspace_root"] == str(root)
    assert _db_key(root) in workspace_module._wal_anchors


def test_close_is_refused_while_a_job_runs(tmp_path: Path) -> None:
    server, root, _library = _opened(tmp_path)
    release = _blocked_job(server, root)
    try:
        values = server.close_library_view(server.current_signature())
        assert values[0].startswith("작업이 도는 중입니다")
        assert "작업(probe) 1개" in values[0] and "아무것도 바꾸지 않았습니다" in values[0]
        _assert_unchanged(server, root, values, tmp_path / "state.json")
    finally:
        release.set()


@pytest.mark.parametrize(
    "kind",
    ["selection_analysis", "understanding_analysis", "reference_import", "finalcut_send"],
)
def test_close_is_refused_while_paid_or_import_work_runs(tmp_path: Path, kind: str) -> None:
    """무료 로컬 목록 밖의 종류(분석·가져오기·보내기)는 닫기를 막는다."""
    server, root, _library = _opened(tmp_path)
    release = _blocked_job(server, root, kind)
    try:
        values = server.close_library_view(server.current_signature())
        assert values[0].startswith(("작업이 도는 중입니다", "분석이 도는 중입니다"))
        _assert_unchanged(server, root, values, tmp_path / "state.json")
        job = server.raw.get_job(root, server._probe_job_id)  # type: ignore[attr-defined]
        assert not job.cancellation_requested
    finally:
        release.set()


@pytest.mark.parametrize("kind", sorted(workspace_service.CLOSE_CANCELS_JOB_KINDS))
def test_close_cancels_free_local_work_and_closes(tmp_path: Path, kind: str) -> None:
    server, root, _library = _opened(tmp_path)
    release = _blocked_job(server, root, kind)
    try:
        values = server.close_library_view(server.current_signature())
        assert values[0] == workspace_service.LIBRARY_CLOSED_TEXT
        assert server.workspace_root() is None
        job = server.raw.get_job(root, server._probe_job_id)  # type: ignore[attr-defined]
        assert job.cancellation_requested  # 닫기가 취소를 요청했다
    finally:
        release.set()


@pytest.mark.parametrize("kind", sorted(workspace_service.CLOSE_OUTLIVES_JOB_KINDS))
def test_close_leaves_db_only_upkeep_running(tmp_path: Path, kind: str) -> None:
    server, root, _library = _opened(tmp_path)
    release = _blocked_job(server, root, kind)
    try:
        assert server.close_library_view(server.current_signature())[0] == (
            workspace_service.LIBRARY_CLOSED_TEXT
        )
        job = server.raw.get_job(root, server._probe_job_id)  # type: ignore[attr-defined]
        assert not job.cancellation_requested  # 실행마다 한 번만 걸리는 일이라 끝나게 둔다
    finally:
        release.set()


def test_open_then_close_with_a_running_library_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """여는 길이 띄운 라이브러리 견적이 도는 중에도 닫힌다 — 늦게 끝나도 견적을 되살리지 않는다."""
    server, root, library = _opened(tmp_path)
    entered, release = threading.Event(), threading.Event()
    sentinel = object()

    def rebound(*args: Any, **kwargs: Any) -> object:
        entered.set()  # 두 번째 취소 확인을 지난 자리 — 닫기 세대만이 쓰기를 막는다
        assert release.wait(20)
        return sentinel

    monkeypatch.setattr(server, "_library_ids", lambda selected: ("source-a",))
    monkeypatch.setattr(server, "_quote_current", lambda *args: True)
    monkeypatch.setattr(server.raw, "selection_quote", lambda *args, **kwargs: object())
    monkeypatch.setattr(server.raw, "cached_selection_analysis", lambda *args: None)
    monkeypatch.setattr(server.raw, "selection_rebound_quote", rebound)
    server._prepare_library_quote(root)
    assert entered.wait(20)
    (quote_job,) = [j for j in server.raw.all_active_jobs(root) if j.kind == "library_quote"]

    values = server.close_library_view(server.current_signature())
    assert values[0] == workspace_service.LIBRARY_CLOSED_TEXT
    assert server.raw.get_job(root, quote_job.job_id).cancellation_requested
    release.set()
    assert _wait_until(lambda: not server.raw.all_active_jobs(root))
    assert root not in server._library_quotes  # 늦은 끝이 닫힌 작업공간의 견적을 쓰지 않았다
    assert root not in server._library_cached_ids

    server.open_fcp_library_view(library, originals_only=False)
    assert _wait_until(lambda: server._library_quotes.get(root) is sentinel)  # 새로 잰다
    quotes = [j for j in server.raw.list_jobs(root) if j.kind == "library_quote"]
    assert len(quotes) == 2 and quotes[0].job_id != quote_job.job_id


def test_close_is_refused_while_the_library_approval_or_analysis_runs(tmp_path: Path) -> None:
    server, root, _library = _opened(tmp_path)
    release = _blocked_job(server, root)
    try:
        server._approval_jobs[root] = server._probe_job_id  # type: ignore[attr-defined]
        values = server.close_library_view(server.current_signature())
        assert values[0] == workspace_service.LIBRARY_CLOSE_BUSY_ANALYSIS
        assert "「전체 분석 취소」" in values[0]
        _assert_unchanged(server, root, values, tmp_path / "state.json")
    finally:
        release.set()
    assert _wait_until(lambda: not server.raw.all_active_jobs(root))
    server._approval_jobs.pop(root, None)
    # 승인 작업 없이 화면 기억에만 도는 분석(대기열에 든 영상)도 막는다.
    server._quick_tasks["running-token"] = {"root": root, "stage": "분석 대기"}
    values = server.close_library_view(server.current_signature())
    assert values[0] == workspace_service.LIBRARY_CLOSE_BUSY_ANALYSIS
    _assert_unchanged(server, root, values, tmp_path / "state.json")


def test_a_stale_tab_cannot_close_another_workspace(tmp_path: Path) -> None:
    server, root, _library = _opened(tmp_path)
    with pytest.raises(gr.Error, match="선택한 작업공간이 바뀌었습니다"):
        server.close_library_view("ws_stale")
    assert server.workspace_root() == root


def test_closing_when_nothing_is_open_just_redraws_the_empty_page(tmp_path: Path) -> None:
    server = _server(tmp_path / "state.json")
    server.build_blocks()
    values = server.close_library_view(None)
    assert values[0] == workspace_service.LIBRARY_CLOSE_NOTHING
    assert values[2] is None  # 서명 칸
    assert not (tmp_path / "state.json").exists()  # 지울 기록이 없으면 파일을 만들지 않는다


# ── WAL 닻을 놓고, 같은 라이브러리를 다시 불러오면 그대로 열린다 ──────────────────


def test_close_releases_the_wal_anchor_and_reopen_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, library = _opened(tmp_path)
    key = _db_key(root)
    signature = server.current_signature()
    assert key in workspace_module._wal_anchors
    server.close_library_view(signature)
    assert key not in workspace_module._wal_anchors  # 그 DB를 붙잡던 연결이 닫혔다

    prepared: list[Path] = []
    original = server._prepare_library_quote

    def record(selected: Path) -> None:
        prepared.append(selected)
        original(selected)

    monkeypatch.setattr(server, "_prepare_library_quote", record)
    view = server.open_fcp_library_view(library, originals_only=False)
    assert view[1] == signature  # 같은 작업공간이 같은 신원으로 다시 열린다
    assert server.workspace_root() == root
    assert key in workspace_module._wal_anchors  # 닻이 다시 섰다
    assert prepared == [root]  # 견적도 다시 준비한다
    assert server._fcp_scan is not None  # 라이브러리 조사도 다시 했다
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))[
        "workspace_root"
    ] == str(root)


def test_release_wal_anchor_closes_only_that_database(tmp_path: Path) -> None:
    from vlogsrt.project.raw.workspace import RawWorkspace

    first = RawWorkspace.create(tmp_path / "one", "one")
    second = RawWorkspace.create(tmp_path / "two", "two")
    one, two = _db_key(first.root), _db_key(second.root)
    held = workspace_module._wal_anchors[one]
    assert workspace_module.release_wal_anchor(first.root / workspace_module.DB_NAME) is True
    assert one not in workspace_module._wal_anchors and two in workspace_module._wal_anchors
    with pytest.raises(Exception, match="closed"):
        held.execute("SELECT 1")  # 닫힌 연결이다
    assert workspace_module.release_wal_anchor(first.root / workspace_module.DB_NAME) is False


# ── 단추는 열려 있을 때만 · 배선 수는 처리기 값 수와 같다 ─────────────────────────


def _close_tag(page: str) -> str:
    """머리줄 닫기 단추의 여는 태그."""
    found = re.search(rf'<button[^>]*id="{ui_facade.LIBRARY_CLOSE_ID}"[^>]*>', page)
    assert found is not None, "머리줄에 닫기 단추가 없다"
    return found.group(0)


def test_the_top_bar_button_shows_only_while_a_library_is_open(tmp_path: Path) -> None:
    closed = ui_facade.topbar_html(online=False, mode_note="오프라인", project="", flow="")
    opened = ui_facade.topbar_html(
        online=False, mode_note="오프라인", project="제주", flow="", can_close=True
    )
    assert " hidden" in _close_tag(closed) and " hidden" not in _close_tag(opened)
    assert f"{workspace_service.LIBRARY_CLOSE_LABEL}</button>" in opened

    server = _server(tmp_path / "state.json")
    assert " hidden" in _close_tag(server._topbar_value())
    server.create_workspace_view("열린 프로젝트", str(tmp_path / "p"))
    assert " hidden" not in _close_tag(server._topbar_value())
    # 열린 작업공간의 작업대 값에는 닫힘 표지가 없다 — 실행본이 단추를 보인다.
    _shell, workbench = server.visual_workbench_render(server.workspace_root())
    assert "data-vl-closed" not in workbench
    _shell, empty = server.visual_workbench_render(None)
    assert "data-vl-closed" in empty
    shell_js, visual_js = ui_facade.SHELL_JS, ui_facade.VISUAL_BOOTSTRAP_JS
    assert f'"{ui_facade.LIBRARY_CLOSE_SOURCE_ID}"' in shell_js
    assert f'"#{ui_facade.LIBRARY_CLOSE_ID}"' in shell_js
    assert f'd.getElementById("{ui_facade.LIBRARY_CLOSE_ID}")' in visual_js
    assert 'hasAttribute("data-vl-closed")' in visual_js


def test_close_wiring_matches_the_handler_values(tmp_path: Path, local_socket: None) -> None:
    server = _server(tmp_path / "state.json")
    app = server.build_blocks()
    source = server._library_close_button
    assert source.elem_id == ui_facade.LIBRARY_CLOSE_SOURCE_ID and source.visible == "hidden"
    dep = _close_dep(app, server)
    assert app.fns[dep["id"]].fn == server.close_library_view
    assert dep["inputs"] == [server._signature_state._id]
    selection = [block._id for block in server._selection_outputs()]
    fcp = [server._fcp_originals_group._id, server._fcp_primary_info._id]
    cells = [
        server._library_approve_button._id,
        server._library_summary_md._id,
        server._capcut_progress_md._id,
        server._quote_note_md._id,
        server._usage_md._id,
    ]
    sent = [state._id for state in server._status_sent]
    assert dep["outputs"][0] == server._capcut_status._id
    assert dep["outputs"][1 : 1 + len(selection)] == selection
    assert dep["outputs"][1 + len(selection) : 3 + len(selection)] == fcp
    assert dep["outputs"][-2 * CELLS :] == [*cells, *sent]
    server.create_workspace_view("A", str(tmp_path / "a"))
    root = server.workspace_root()
    assert root is not None
    assert _wait_until(lambda: not server.raw.all_active_jobs(root))
    assert len(server.close_library_view(server.current_signature())) == len(dep["outputs"])
    # 닫는 사건 뒤에도 「영상 분석 모델」·용어집 칸을 맞춘다(작업공간을 바꾸는 사건 모두와 같다).
    by_id = {d["id"]: d for d in app.config["dependencies"]}
    followers = {
        getattr(app.fns[d["id"]].fn, "__name__", "")
        for d in by_id.values()
        if d.get("trigger_after") == dep["id"]
    }
    assert {"analysis_model_controls", "glossary_controls"} <= followers
