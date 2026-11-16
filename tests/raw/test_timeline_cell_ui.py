"""「타임라인으로 보내기」 단추(W-71b, D126) — 성공하면 「최종 편집」 탭으로 넘기고 요약을 보인다.

실패는 까닭을 오류로, 취소는 활동 칸 글로 말하고 탭을 넘기지 않는다. 요약은 바뀐 원본 자막·지원 밖
원본·심지 않은 원본을 이름으로 대며 모든 글을 이스케이프한다. 「추천만 이어붙여 내보내기」는 그대로
옆에 남는다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)

import gradio as gr  # noqa: E402

from ui import timeline_cell, ui_facade  # noqa: E402
from ui.final_edit_ui import REVIEW_TAB_KEY  # noqa: E402
from ui.timeline_cell import SEND_ID, timeline_summary_html  # noqa: E402
from ui.workspace_service import RENDER_EXPORT_ID, RawWorkspaceUi  # noqa: E402


def _model(**overrides: Any) -> dict[str, Any]:
    model: dict[str, Any] = {
        "timelineId": "20261113T010203Z",
        "revision": 1,
        "facts": {"sequence": {"frame_duration": {"num": 1, "den": 30}}},
        "summary": {"clips": 3, "cues": 5, "visible_cues": 4, "edited_frames": 90},
        "seed": {"unsupported": [], "unseeded": [], "empty": 0, "clamped": 0},
        "changedSources": [],
    }
    model.update(overrides)
    return model


def _server(tmp_path: Path) -> tuple[RawWorkspaceUi, Path, str]:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("편집", str(tmp_path / "workspace"))
    root = Path(server.workspace_root())
    signature = server.current_signature()
    assert signature
    return server, root, signature


def _fake_jobs(
    monkeypatch: pytest.MonkeyPatch, server: RawWorkspaceUi, final: str, error: str = ""
) -> None:
    statuses = iter(["running", final])

    def get_job(_root: Path, job_id: str) -> Any:
        return SimpleNamespace(job_id=job_id, status=next(statuses, final), error=error)

    monkeypatch.setattr(timeline_cell, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        server.raw, "start_timeline_export", lambda _root: SimpleNamespace(job_id="j1")
    )
    monkeypatch.setattr(server.raw, "get_job", get_job)
    monkeypatch.setattr(server.raw, "timeline_open", lambda _root, *_a: _model())


def test_the_summary_names_what_was_left_out_and_escapes_it() -> None:
    page = timeline_summary_html(
        _model(
            changedSources=[{"source_id": "s1", "name": "<B>.mp4"}],
            seed={
                "unsupported": [{"name": "C.mp4", "reason": "unsupported_grid", "detail": ""}],
                "unseeded": [{"source_id": "s4", "name": "D.mp4"}],
                "empty": 2,
                "clamped": 1,
            },
        )
    )
    assert "클립 3개" in page and "자막 4/5줄 보임" in page and "3.0초" in page
    assert "&lt;B&gt;.mp4" in page and "<B>" not in page
    assert "C.mp4 — unsupported_grid" in page
    assert "추천 구간이 없어 넣지 않은 원본: D.mp4" in page
    assert "빈 자막 2줄" in page and "1줄을 원본 안으로" in page
    assert timeline_summary_html(None) == ""


def test_a_successful_send_stays_in_the_window_with_the_timeline_deck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-85(D145) — 타임라인은 같은 창 아래 덱이다. 보내기가 끝나도 「영상 정리」에 머문다."""
    server, _root, signature = _server(tmp_path)
    _fake_jobs(monkeypatch, server, "succeeded")
    tabs, summary, text = server._timeline_cell.send_view(signature)
    assert isinstance(tabs, gr.Tabs) and tabs.selected == REVIEW_TAB_KEY
    assert "원본 범위 타임라인" in summary
    assert text == timeline_cell.STARTED_TEXT.format(clips=3, cues=4)


def test_a_failed_send_raises_the_reason_and_a_cancelled_one_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    _fake_jobs(monkeypatch, server, "failed", error="자막이 9000줄로 상한 5000줄을 넘어")
    with pytest.raises(gr.Error, match="상한 5000줄"):
        server._timeline_cell.send_view(signature)
    _fake_jobs(monkeypatch, server, "cancelled")
    tabs, summary, text = server._timeline_cell.send_view(signature)
    assert text == timeline_cell.CANCELLED_TEXT
    assert not isinstance(tabs, gr.Tabs) and not isinstance(summary, str)


def test_the_view_is_empty_without_a_timeline_and_shows_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    cell = server._timeline_cell
    assert cell.view(signature) == ""
    assert cell.view("다른-작업공간") == ""
    monkeypatch.setattr(server.raw, "has_timeline", lambda _root: True)
    monkeypatch.setattr(server.raw, "timeline_open", lambda _root, *_a: _model())
    assert "원본 범위 타임라인 · 20261113T010203Z" in cell.view(signature)


def test_blocks_put_the_button_beside_the_render_export_and_wire_the_tab(
    tmp_path: Path, local_socket: None
) -> None:
    server, _root, _signature = _server(tmp_path)
    app = server.build_blocks()
    config = app.config
    by_id = {component["id"]: component for component in config["components"]}
    elem_ids = {c["props"].get("elem_id") for c in config["components"]}
    assert {SEND_ID, RENDER_EXPORT_ID} <= elem_ids
    deps = config["dependencies"]
    fns = {dep["id"]: app.fns[dep["id"]].fn for dep in deps}
    cell = server._timeline_cell
    (sent,) = [d for d in deps if fns[d["id"]] == cell.send_view]
    assert [tuple(t) for t in sent["targets"]] == [(cell.button._id, "click")]
    assert by_id[sent["outputs"][0]]["type"] == "tabs"
    assert sent["outputs"][1] == cell.summary._id
    assert sent["js"] == ui_facade.EXPORT_WAKE_JS  # 기다리는 동안 활동 띠가 깨어 있다
    views = [d for d in deps if fns[d["id"]] == cell.view]
    assert len(views) >= 5  # 작업공간을 열고 바꾸는 사건마다 다시 그린다
    assert [d for d in deps if fns[d["id"]] == server.render_export_view]
