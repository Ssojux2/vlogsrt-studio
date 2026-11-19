"""「자막 입히기」 칸(W-65, D116) — 자리 · 배선 · 빈 칸 해석 · 결과 줄."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import burn_cell, workspace_service  # noqa: E402
from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt.project.raw import burn_export, burn_in  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402

SRT = "1\n00:00:01,000 --> 00:00:02,000\n안녕\n"


def _ancestors(block: Any) -> list[Any]:
    out = []
    node = getattr(block, "parent", None)
    while node is not None:
        out.append(node)
        node = getattr(node, "parent", None)
    return out


def _built(tmp_path: Path) -> tuple[RawWorkspaceUi, Any, dict[Any, Any]]:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("자막", str(tmp_path / "workspace"))
    app = server.build_blocks()
    by_id = {getattr(b, "elem_id", None): b for b in app.blocks.values()}
    return server, app, by_id


@dataclass(frozen=True)
class _Record:
    status: str
    error: str | None = None


def test_the_cell_sits_under_the_english_cell_and_is_wired(
    tmp_path: Path, local_socket: None
) -> None:
    server, app, by_id = _built(tmp_path)
    cell = by_id[burn_cell.BURN_ID]
    assert by_id["vl-quote"] in _ancestors(cell)
    for suffix in ("video", "korean", "english", "position", "font", "outline", "margin",
                   "color", "preview-run", "run", "preview", "result"):  # fmt: skip
        assert cell in _ancestors(by_id[f"{burn_cell.BURN_ID}-{suffix}"]), suffix
    burn = server._burn_cell
    deps = app.config["dependencies"]
    fns = {d["id"]: app.fns[d["id"]].fn for d in deps if d["id"] in app.fns}
    (export,) = [d for d in deps if fns.get(d["id"]) == burn.export_view]
    expected = [server._signature_state, *burn.path_inputs(), *burn.style_inputs()]
    assert export["inputs"] == [block._id for block in expected]
    assert export["js"] == burn_cell.WAKE_JS
    assert [d for d in deps if fns.get(d["id"]) == burn.preview_view]
    assert [d for d in deps if fns.get(d["id"]) == burn.restore_view]
    kind = raw_service.SUBTITLE_BURN_JOB_KIND
    assert workspace_service.ACTIVITY_JOB_LABELS[kind] == burn_cell.BURN_JOB_LABEL
    assert kind in workspace_service.CLOSE_CANCELS_JOB_KINDS


def test_without_a_workspace_it_answers_folder_first(tmp_path: Path, local_socket: None) -> None:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.build_blocks()
    style = ("bottom", 54, 4, 60, "#ffffff")
    answer = ("", workspace_service.LIBRARY_FIRST_TEXT)
    assert server._burn_cell.export_view(None, "", "", "", *style) == answer
    assert server._burn_cell.preview_view(None, "", "", "", *style) == answer


def test_blank_boxes_need_a_draft_and_srt_boxes_need_srt_files(
    tmp_path: Path, local_socket: None
) -> None:
    server, _app, _by_id = _built(tmp_path)
    signature = server.current_signature()
    style = ("bottom", 54, 4, 60, "#ffffff")
    with pytest.raises(gr.Error, match="아직 낸 초안이 없습니다"):
        server._burn_cell.export_view(signature, "", "", "", *style)
    video = tmp_path / "a.mp4"
    video.write_bytes(b"v")
    with pytest.raises(gr.Error, match="SRT 파일"):
        server._burn_cell.export_view(signature, str(video), str(video), "", *style)
    with pytest.raises(gr.Error, match="글자 크기"):
        srt = tmp_path / "a.srt"
        srt.write_text(SRT, encoding="utf-8")
        server._burn_cell.export_view(
            signature, str(video), str(srt), "", "bottom", 5, 4, 60, "#fff"
        )


def test_the_export_button_waits_and_says_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    server, _app, _by_id = _built(tmp_path)
    folder = tmp_path / "draft"
    folder.mkdir()
    (folder / "draft.mp4").write_bytes(b"v")
    (folder / "draft.srt").write_text(SRT, encoding="utf-8")
    (folder / "draft.en.srt").write_text(SRT.replace("안녕", "Hi"), encoding="utf-8")
    monkeypatch.setattr(
        server.raw,
        "latest_render_export",
        lambda root: {"video": str(folder / "draft.mp4"), "srt": str(folder / "draft.srt")},
    )
    seen: dict[str, Any] = {}

    def prepare(video: Path, korean: Path, english: Path | None) -> str:
        seen["inputs"] = (video, korean, english)
        return "job"

    def start(raw: Any, root: Path, job: Any, style: Any, done: Any) -> raw_service.JobHandle:
        seen["style"] = style
        done({"path": str(folder / "draft.burned.mp4"), "events": 1, "empty": 0,
              "english": True, "fallback": False, "hdr": False})  # fmt: skip
        return raw_service.JobHandle(job_id="job-1", reattached=False)

    monkeypatch.setattr(burn_export, "prepare", prepare)
    monkeypatch.setattr(burn_export, "start", start)
    monkeypatch.setattr(server.raw, "get_job", lambda root, job_id: _Record("succeeded"))
    signature = server.current_signature()
    text, reply = server._burn_cell.export_view(signature, "", "", "", "top", 60, 3, 40, "#abc")
    assert seen["inputs"] == (folder / "draft.mp4", folder / "draft.srt", folder / "draft.en.srt")
    assert seen["style"] == burn_in.BurnStyle("top", 60, 3, 40, "#AABBCC")
    assert "draft.burned.mp4" in text and text == reply
    monkeypatch.setattr(server.raw, "get_job", lambda root, job_id: _Record("failed", "인코더"))
    with pytest.raises(gr.Error, match="자막을 입히지 못했습니다 — 인코더"):
        server._burn_cell.export_view(signature, "", "", "", "top", 60, 3, 40, "#abc")


def test_restore_reads_the_saved_style(tmp_path: Path, local_socket: None) -> None:
    server, _app, _by_id = _built(tmp_path)
    root = server.workspace_root()
    assert root is not None
    burn_export.save_style(server.raw, root, burn_in.BurnStyle("top", 70, 2, 30, "#FFCC00"))
    assert server._burn_cell.restore_view(server.current_signature()) == (
        "top", 70, 2, 30, "#FFCC00",
    )  # fmt: skip


def test_colors_from_the_picker_are_normalized() -> None:
    assert burn_cell.normalize_color("#ffcc00") == "#FFCC00"
    assert burn_cell.normalize_color("#fc0") == "#FFCC00"
    assert burn_cell.normalize_color(None) == ""


def test_a_burn_of_the_final_edit_says_so_and_names_left_out_english() -> None:
    """D118 — 최종 편집이면 리비전·클립·가린 자막을, 뺀 영어 SRT는 이름과 할 일을 말한다."""
    facts = {
        "path": "/x/final.burned.mp4", "events": 4, "empty": 0, "english": False,
        "fallback": False, "hdr": False,
        "edit": {"revision": 2, "clips": 3, "hidden": 1, "english_left_out": ["final.en.srt"]},
    }  # fmt: skip
    text = burn_cell._done_text(facts)
    assert "final.burned.mp4" in text and "(한글만)" in text
    assert "최종 편집(리비전 2 · 클립 3개 · 컷에 가려진 자막 1개)의 컷과 함께" in text
    assert "`final.en.srt`는 지금 편집본 글을 옮긴 것이 아니라 뺐습니다" in text
    plain = burn_cell._done_text({**facts, "edit": None})
    assert "최종 편집" not in plain


def test_moving_a_knob_saves_the_style_and_tells_the_player(
    tmp_path: Path, local_socket: None
) -> None:
    """W-98(D173) — 손잡이를 사람이 바꾸면 바로 저장한다(공유가 화면에 보인 모양을 쓴다).
    재생기 자막 겹에도 먼저 알린다. 되살리기는 `input`이 아니라 저장 고리를 돌지 않는다."""
    server, app, _by_id = _built(tmp_path)
    burn = server._burn_cell
    deps = app.config["dependencies"]
    fns = {d["id"]: app.fns[d["id"]].fn for d in deps if d["id"] in app.fns}
    saves = [d for d in deps if fns.get(d["id"]) == burn.save_view]
    assert len(saves) == len(burn.style_inputs())
    for dep in saves:
        assert dep["js"] == burn_cell.OVERLAY_STYLE_JS
        assert [t[1] for t in dep["targets"]] == ["input"]
    root = server.workspace_root()
    assert root is not None
    burn.save_view(server.current_signature(), "top", 70, 2, 30, "#ffcc00")
    assert burn_export.saved_style(server.raw, root) == burn_in.BurnStyle(
        "top", 70, 2, 30, "#FFCC00"
    )
    burn.save_view("stale-signature", "bottom", 54, 4, 60, "#FFFFFF")  # 다른 작업공간 — 쓰지 않는다
    assert burn_export.saved_style(server.raw, root).position == "top"
