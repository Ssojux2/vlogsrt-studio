"""「최종 편집」 탭(W-66, D117) — 빈 화면 · 심은 초안 · 자동 전환 · 서버 함수 · Blocks 배선.

자동 전환은 그 화면에서 「내보내기」를 누른 작업(무장한 작업 아이디)이 성공했을 때만이다 — 틱이
진행 상태를 한 번도 못 보고 바로 성공을 봐도 넘기고, 새로고침(무장 없음)이나 다른 작업의 성공은
넘기지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
pytest.importorskip("fastapi", reason="웹 UI extra(fastapi)가 없다", exc_type=ImportError)

import gradio as gr  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ui import final_edit_ui  # noqa: E402
from ui.final_edit_ui import (  # noqa: E402
    FINAL_EDIT_TAB_ID,
    FINAL_EDIT_TAB_KEY,
    KEYMAP,
    NO_EXPORT_TEXT,
    NO_WORKSPACE_TEXT,
)
from ui.workspace_service import RawWorkspaceUi  # noqa: E402

SRT = "1\n00:00:01,000 --> 00:00:02,000\n안녕\n\n2\n00:00:03,000 --> 00:00:04,000\n반가워\n"


def _server(tmp_path: Path) -> tuple[RawWorkspaceUi, Path, str]:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("편집", str(tmp_path / "workspace"))
    root = Path(server.workspace_root())
    signature = server.current_signature()
    assert signature
    return server, root, signature


def _plant(server: RawWorkspaceUi, root: Path, stamp: str = "20261112T010203Z") -> Path:
    folder = root / "export" / stamp
    folder.mkdir(parents=True)
    (folder / "draft.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))
    (folder / "draft.srt").write_text(SRT, encoding="utf-8")
    report = {
        "sequence": {"frame_duration": {"num": 1, "den": 30}},
        "total_frames": 150,
        "total_seconds": {"num": 5, "den": 1},
        "parts": [],
    }
    (folder / "render_report.json").write_text(json.dumps(report), encoding="utf-8")
    server.raw.register_media(root, folder / "draft.mp4", "artifact")
    return folder


def _model(page: str) -> dict[str, Any]:
    found = re.search(r"data-fe-model hidden>(.*?)</div>", page)
    assert found
    return json.loads(found.group(1).replace("\\u003c", "<").replace("\\u0026", "&"))


def _job(job_id: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, status=status)


def _jobs(monkeypatch: pytest.MonkeyPatch, server: RawWorkspaceUi, jobs: list[Any]) -> None:
    """`latest_job`이 차례로 돌려줄 값 — 마지막 값은 계속 돌려준다."""
    queue = list(jobs)

    def latest(root: Path, kind: str) -> Any:
        assert kind == "render_export"
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(server.raw, "latest_job", latest)


def test_view_is_empty_without_workspace_or_export(tmp_path: Path) -> None:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    assert NO_WORKSPACE_TEXT in server._final_edit.view(None)
    server, root, signature = _server(tmp_path)
    assert NO_EXPORT_TEXT.split(" — ")[0] in server._final_edit.view(signature)
    assert _model(server._final_edit.view(signature))["empty"]


def test_view_carries_the_seeded_draft_and_keymap(tmp_path: Path, local_socket: None) -> None:
    server, root, signature = _server(tmp_path)
    folder = _plant(server, root)
    page = server._final_edit.view(signature)
    model = _model(page)
    edit = model["edit"]
    assert edit["exportId"] == folder.name and edit["revision"] == 0
    assert edit["doc"]["clips"] == [[0, 150]]
    assert [cue[3] for cue in edit["doc"]["cues"]] == ["안녕", "반가워"]
    assert edit["settings"]["autosaveMs"] > 0
    assert model["signature"] == signature and model["token"]
    assert len(model["keymap"]) == len(KEYMAP)
    assert 'id="fe-canvas"' in page and 'id="fe-video"' in page and "단축키" in page
    client = TestClient(server.build_fastapi(mount_blocks=False), base_url="http://127.0.0.1")
    response = client.get(f"/media/{edit['mediaFileId']}", headers={"Range": "bytes=0-7"})
    assert response.status_code == 206 and response.content == b"\x00\x00\x00\x18ftyp"


def test_switch_only_when_the_armed_job_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _server(tmp_path)
    panel = server._final_edit
    _plant(server, root)
    # 페이지 로드 — 지난 내보내기가 성공해 있어도 넘기지 않는다.
    _jobs(monkeypatch, server, [_job("old", "succeeded")])
    tabs, block, button, seen = panel.switch_tick(signature, None)
    assert tabs == gr.skip() and block == gr.skip() and button.visible is True
    assert panel.switch_tick(signature, seen) == (gr.skip(),) * 4
    # 「내보내기」를 눌렀다 — 무장. 틱이 진행 상태를 못 보고 바로 성공을 본다.
    _jobs(monkeypatch, server, [_job("new", "running")])
    seen = panel.arm(signature, seen)
    assert seen["armed"] == "new"
    _jobs(monkeypatch, server, [_job("new", "succeeded")])
    tabs, block, button, seen = panel.switch_tick(signature, seen)
    assert isinstance(tabs, gr.Tabs) and tabs.selected == FINAL_EDIT_TAB_KEY
    assert "fe-canvas" in block and button.visible is True and seen["armed"] is None
    assert panel.switch_tick(signature, seen) == (gr.skip(),) * 4


def test_failed_or_foreign_jobs_do_not_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _server(tmp_path)
    panel = server._final_edit
    _plant(server, root)
    _jobs(monkeypatch, server, [_job("mine", "queued")])
    seen = panel.arm(signature, {})
    _jobs(monkeypatch, server, [_job("mine", "failed")])
    tabs, block, button, seen = panel.switch_tick(signature, seen)
    assert tabs == gr.skip() and block == gr.skip() and seen["armed"] is None
    # 다른 탭이 낸 새 초안 — 탭은 넘기지 않고 화면만 새로 그린다.
    _jobs(monkeypatch, server, [_job("theirs", "succeeded")])
    tabs, block, button, seen = panel.switch_tick(signature, seen)
    assert tabs == gr.skip() and "fe-canvas" in block


def test_tick_skips_other_workspaces_and_hides_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _server(tmp_path)
    panel = server._final_edit
    _plant(server, root)
    _jobs(monkeypatch, server, [_job("a", "succeeded")])
    assert panel.switch_tick("other", None) == (gr.skip(),) * 4
    _tabs, _block, _button, seen = panel.switch_tick(signature, None)
    server._set_root(None)
    _tabs, _block, button, cleared = panel.switch_tick(signature, seen)
    assert button.visible is False and cleared == {}
    assert panel.switch_tick(signature, cleared) == (gr.skip(),) * 4


def test_open_view_switches_and_refuses_without_export(tmp_path: Path) -> None:
    server, root, signature = _server(tmp_path)
    with pytest.raises(gr.Error, match="아직 내보낸 초안이 없습니다"):
        server._final_edit.open_view(signature)
    _plant(server, root)
    tabs, page = server._final_edit.open_view(signature)
    assert tabs.selected == FINAL_EDIT_TAB_KEY and "fe-canvas" in page


def test_server_functions_load_save_and_guard(tmp_path: Path) -> None:
    server, root, signature = _server(tmp_path)
    folder = _plant(server, root)
    model = _model(server._final_edit.view(signature))
    load, save, _wave = server._final_edit.server_functions()
    loaded = load({"signature": signature})
    assert loaded["ok"] is True and loaded["edit"]["exportId"] == folder.name
    doc = {**model["edit"]["doc"], "clips": [[0, 30], [60, 150]], "seedSrt": "draft.srt"}
    payload = {"signature": signature, "token": model["token"], "exportId": folder.name,
               "baseRevision": 0, "doc": doc}  # fmt: skip
    saved = save(payload)
    assert saved["ok"] is True and saved["revision"] == 1
    assert (folder / "edit" / "final.srt").read_text(encoding="utf-8").count("-->") == 1
    assert save(payload) == {"ok": True, "conflict": True, "revision": 1}
    stale = save({**payload, "token": "old"})
    assert stale["ok"] is False and stale["stale"] is True
    wrong = save({**payload, "signature": "other"})
    assert wrong["ok"] is False and wrong["stale"] is True
    broken = save({**payload, "baseRevision": 1, "doc": {**doc, "clips": [[0, 999]]}})
    assert broken["ok"] is False and broken["stale"] is False
    shapeless = save({**payload, "baseRevision": "1"})
    assert shapeless["ok"] is False


def test_blocks_wire_the_tabs_and_the_switch(tmp_path: Path, local_socket: None) -> None:
    server, _root, _signature = _server(tmp_path)
    app = server.build_blocks()
    config = app.config
    by_id = {component["id"]: component for component in config["components"]}
    tab_ids = {c["props"].get("elem_id") for c in config["components"] if c["type"] == "tabitem"}
    assert {FINAL_EDIT_TAB_ID, "review-tab"} <= tab_ids
    deps = config["dependencies"]
    fns = {dep["id"]: app.fns[dep["id"]].fn for dep in deps}
    panel = server._final_edit
    (tick,) = [d for d in deps if fns[d["id"]] == panel.switch_tick]
    tabs_id = tick["outputs"][0]
    assert by_id[tabs_id]["type"] == "tabs"
    assert tick["outputs"][1] == panel.block._id and tick["outputs"][2] == panel.open_button._id
    (armed,) = [d for d in deps if fns[d["id"]] == panel.arm]
    started = [d for d in deps if fns[d["id"]] == server.render_export_view]
    assert armed["trigger_after"] == started[0]["id"]
    (opened,) = [d for d in deps if fns[d["id"]] == panel.open_view]
    assert opened["outputs"] == [tabs_id, panel.block._id]
    views = [d for d in deps if fns[d["id"]] == panel.view]
    assert len(views) >= 5  # 작업공간을 열고 바꾸는 사건마다 다시 그린다


def test_keymap_has_no_duplicate_chords_and_every_action_is_known() -> None:
    chords = [(b.code, b.mod, b.shift, b.alt) for b in KEYMAP]
    assert len(chords) == len(set(chords))
    app_js = final_edit_ui.FINAL_EDIT_JS
    for binding in KEYMAP:
        assert f'"{binding.action}"' in app_js, binding.action
