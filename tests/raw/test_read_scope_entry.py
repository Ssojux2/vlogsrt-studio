"""화면 진입점은 쓰기 **뒤에** 읽기 범위를 연다(W-23 세션 B, D63).

작업대 동작(자막을 쓴다)이 도는 동안에는 범위가 없어야 한다 — 범위가 쓰기를 넘으면 그리기가 방금
쓴 것을 못 본다. 그리기와 고급 패널 머리 맞춤은 한 범위를 함께 쓴다. 타이머 틱은 그리기 열쇠의
재료만 범위 안에서 읽고, 정리 고리가 도는 `_capcut_result`는 그 범위 밖이다.

정리 고리는 W-24 세션 G부터 `_capcut_result`의 범위 **안**에서 할 일을 잰다(그리기와 같은 읽기다).
고리가 작업을 걸었을 수 있으면(안내가 돌아오면) 그리기 앞에서 범위의 기억을 모두 버린다 — 쓴 뒤의
읽기는 여전히 처음부터다.
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
from vlogsrt.project.raw import service as raw_service  # noqa: E402


def _server(tmp_path: Path) -> RawWorkspaceUi:
    return RawWorkspaceUi(state_path=tmp_path / "ui-state.json")


def _spy(seen: dict[str, list[object]], name: str, answer: object) -> Any:
    def call(*_args: object, **_kwargs: object) -> object:
        seen.setdefault(name, []).append(read_scope.current())
        return answer

    return call


def test_capcut_result_reads_upkeep_panel_and_render_in_one_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    seen: dict[str, list[object]] = {}
    monkeypatch.setattr(server, "_maybe_upkeep", _spy(seen, "upkeep", None))
    monkeypatch.setattr(server, "_maybe_sync_upkeep", _spy(seen, "sync", None))
    monkeypatch.setattr(server, "_refresh_review_panel_state", _spy(seen, "panel", None))
    monkeypatch.setattr(server, "_visual_workbench_render", _spy(seen, "render", ("껍", "본")))
    assert server._capcut_result(tmp_path, "답") == ("답", "껍", "본")
    assert seen["upkeep"][0] is not None  # 고리의 할 일 재기도 범위 안이다(W-24 세션 G)
    assert seen["upkeep"] == seen["sync"] == seen["panel"] == seen["render"]  # 같은 범위 하나
    assert read_scope.current() is None


def test_an_upkeep_note_forgets_the_scope_before_the_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """고리가 작업을 걸었을 수 있다(안내) — 고리가 기억한 답은 그리기 앞에서 버려진다."""
    server = _server(tmp_path)
    seen: dict[str, list[object]] = {}

    def started(_root: Path) -> str:
        read_scope.memo(("고리가 읽은 것",), lambda: "쓰기 전")
        return "정리 작업을 걸었다"

    def render(*_args: object, **_kwargs: object) -> tuple[str, str]:
        seen["render"] = [read_scope.memo(("고리가 읽은 것",), lambda: "쓴 뒤")]
        return "껍", "본"

    monkeypatch.setattr(server, "_maybe_upkeep", started)
    monkeypatch.setattr(server, "_maybe_sync_upkeep", lambda _root: None)
    monkeypatch.setattr(server, "_refresh_review_panel_state", lambda _root: None)
    monkeypatch.setattr(server, "_visual_workbench_render", render)
    assert server._capcut_result(tmp_path, "답") == ("답\n\n정리 작업을 걸었다", "껍", "본")
    assert seen["render"] == ["쓴 뒤"]
    assert read_scope.current() is None


def test_without_upkeep_nothing_runs_the_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    seen: dict[str, list[object]] = {}
    monkeypatch.setattr(server, "_maybe_upkeep", _spy(seen, "upkeep", None))
    monkeypatch.setattr(server, "_maybe_sync_upkeep", _spy(seen, "sync", None))
    monkeypatch.setattr(server, "_refresh_review_panel_state", _spy(seen, "panel", None))
    monkeypatch.setattr(server, "_visual_workbench_render", _spy(seen, "render", ("껍", "본")))
    assert server._capcut_result(tmp_path, "답", upkeep=False) == ("답", "껍", "본")
    assert "upkeep" not in seen and "sync" not in seen
    assert seen["panel"] == seen["render"] and seen["render"][0] is not None


def test_a_caption_edit_writes_before_the_render_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    seen: dict[str, list[object]] = {}
    monkeypatch.setattr(server, "_require_current_selection", lambda _signature: tmp_path)
    monkeypatch.setattr(server, "_require_workbench_token", lambda *_args: None)
    monkeypatch.setattr(server.raw, "edit_source_transcript", _spy(seen, "write", {}))
    monkeypatch.setattr(server, "_scan_library", _spy(seen, "quote", None))
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
    # 열쇠의 DB 재료는 도장 하나다(W-46b) — 원본 자막·작업·선택 상태를 한 연결로 읽는다.
    stamp = raw_service.PollStamp((), (), (), 0)
    monkeypatch.setattr(server.raw, "poll_stamp", _spy(seen, "jobs", stamp))
    monkeypatch.setattr(server, "_semantic_stamps", lambda _root: ())
    monkeypatch.setattr(server, "_capcut_progress", lambda _root: "진행")
    monkeypatch.setattr(server, "_capcut_result", _spy(seen, "result", ("답", "껍", "본")))
    message, _shell, workbench, _drawn = server.capcut_tick_view("s")
    assert (message, workbench) == ("답", "본")
    assert seen["jobs"][0] is not None  # 열쇠 재료는 범위 안에서 읽는다
    assert seen["result"] == [None]  # 정리 고리가 도는 자리는 범위 밖이다
    assert read_scope.current() is None
