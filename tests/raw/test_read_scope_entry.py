"""화면 진입점은 쓰기 **뒤에** 읽기 범위를 연다(W-23 세션 B, D63).

정리 고리(작업을 건다)·작업대 동작(자막을 쓴다)이 도는 동안에는 범위가 없어야 한다 — 범위가 쓰기를
넘으면 그리기가 방금 쓴 것을 못 본다. 그리기와 고급 패널 머리 맞춤은 한 범위를 함께 쓴다. 타이머
틱은 그리기 열쇠의 재료만 범위 안에서 읽고, 정리 고리가 도는 `_capcut_result`는 그 범위 밖이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt.project.raw import read_scope  # noqa: E402


def _server(tmp_path: Path) -> RawWorkspaceUi:
    return RawWorkspaceUi(state_path=tmp_path / "ui-state.json")


def _spy(seen: dict[str, list[object]], name: str, answer: object) -> Any:
    def call(*_args: object, **_kwargs: object) -> object:
        seen.setdefault(name, []).append(read_scope.current())
        return answer

    return call


def test_capcut_result_runs_upkeep_before_the_scope_and_renders_inside_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    seen: dict[str, list[object]] = {}
    monkeypatch.setattr(server, "_with_upkeep_notes", _spy(seen, "upkeep", "답"))
    monkeypatch.setattr(server, "_refresh_review_panel_state", _spy(seen, "panel", None))
    monkeypatch.setattr(server, "_visual_workbench_render", _spy(seen, "render", ("껍", "본")))
    assert server._capcut_result(tmp_path, "답") == ("답", "껍", "본")
    assert seen["upkeep"] == [None]
    assert seen["panel"][0] is not None
    assert seen["render"] == seen["panel"]  # 같은 범위 하나
    assert read_scope.current() is None


def test_a_caption_edit_writes_before_the_render_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    seen: dict[str, list[object]] = {}
    monkeypatch.setattr(server, "_require_current_selection", lambda _signature: tmp_path)
    monkeypatch.setattr(server, "_require_workbench_token", lambda *_args: None)
    monkeypatch.setattr(server.raw, "edit_source_transcript", _spy(seen, "write", {}))
    monkeypatch.setattr(server, "_prepare_library_quote", _spy(seen, "quote", None))
    monkeypatch.setattr(server, "_visual_workbench_render", _spy(seen, "render", ("껍", "본")))
    reply = server.visual_workbench_action(
        {"signature": "s", "sourceId": "a", "transcriptRevision": "r", "action": "edit"},
        "source-caption",
    )
    assert reply["ok"] is True, reply
    assert seen["write"] == [None] and seen["quote"] == [None]
    assert seen["render"][0] is not None
    assert read_scope.current() is None


def test_a_tick_reads_its_key_in_a_scope_and_runs_upkeep_outside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    seen: dict[str, list[object]] = {}
    monkeypatch.setattr(server, "_snapshot_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_signature_for", lambda _root: "s")
    monkeypatch.setattr(server, "_require_current_selection", lambda _signature: tmp_path)
    monkeypatch.setattr(server.raw, "load_selection_state", lambda _root: {})
    monkeypatch.setattr(server, "_load_quick_tasks", lambda _root: None)
    monkeypatch.setattr(server.raw, "list_jobs", _spy(seen, "jobs", ()))
    monkeypatch.setattr(server, "_job_states", lambda *_args: frozenset())
    monkeypatch.setattr(server, "_semantic_stamps", lambda _root: ())
    monkeypatch.setattr(server, "_capcut_progress", lambda _root: "진행")
    monkeypatch.setattr(server, "_capcut_result", _spy(seen, "result", ("답", "껍", "본")))
    message, _shell, workbench = server.capcut_tick_view("s")
    assert (message, workbench) == ("답", "본")
    assert seen["jobs"][0] is not None  # 열쇠 재료는 범위 안에서 읽는다
    assert seen["result"] == [None]  # 정리 고리가 도는 자리는 범위 밖이다
    assert read_scope.current() is None
