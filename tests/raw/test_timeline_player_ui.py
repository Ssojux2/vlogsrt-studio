"""원본 범위 타임라인 재생기(W-71c) — 「최종 편집」 탭 머리에서 원본마다 미리보기로 컷을 건넌다.

재생기는 렌더하지 않는다 — 클립이 있는 원본마다 원본 전체 미리보기(`/media/<등록 ID>`)를 틀고,
다음 컷 뒤 클립 시작에 미리 맞춘 대기 영상과 바꿔 튼다(`TL.playStep`). 미리보기가 없는 원본은
「준비 안 됨」으로 보이고(조용히 건너뛰지 않는다), 사람이 누르면 그 원본들만 무료 로컬 준비를 건다.
D117 초안 편집의 단축키는 재생기 안의 키를 가로채지 않는다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)

from ui import timeline_player  # noqa: E402
from ui.timeline_player import (  # noqa: E402
    PLAYER_BLOCK_ID,
    PLAYER_JS,
    client_model,
    timeline_player_html,
)
from ui.workspace_service import RawWorkspaceUi  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")


def _opened(**overrides: Any) -> dict[str, Any]:
    opened: dict[str, Any] = {
        "timelineId": "20261113T010203Z",
        "revision": 2,
        "facts": {
            "sequence": {
                "orientation": "landscape",
                "width": 1920,
                "height": 1080,
                "frame_duration": {"num": 1, "den": 30},
            },
            "sources": [
                {
                    "source_id": "s1",
                    "name": "<A>.mp4",
                    "frame_duration": {"num": 1, "den": 30},
                    "frame_count": 300,
                },
                {
                    "source_id": "s2",
                    "name": "B.mp4",
                    "frame_duration": {"num": 1001, "den": 30000},
                    "frame_count": 240,
                },
            ],
        },
        "doc": {
            "clips": [["s1", 30, 90], ["s2", 0, 60]],
            "cues": [["c1", "s1", 1100, 2400, "안녕 </div>", "u1"]],
            "nextCue": 2,
            "seeded": {"s1": "r1", "s2": "r1"},
        },
        "settings": {"maxCues": 5000},
    }
    opened.update(overrides)
    return opened


def _media(**rows: Any) -> dict[str, Any]:
    ready = {"state": "ready", "fileId": "f1", "origin": "0", "width": 960, "height": 540}
    sources = {"s1": {**ready, "rotation": 0}, "s2": {**ready, "fileId": "f2", "rotation": 0}}
    sources.update(rows)
    return {"timelineId": "20261113T010203Z", "sources": sources}


def _server(tmp_path: Path) -> tuple[RawWorkspaceUi, Path, str]:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("편집", str(tmp_path / "workspace"))
    root = Path(server.workspace_root())
    signature = server.current_signature()
    assert signature
    return server, root, signature


def _model_of(page: str) -> dict[str, Any]:
    found = re.search(r"data-tl-model hidden>(.*?)</div>", page, re.S)
    assert found, page
    return json.loads(found.group(1))


TIERS = {"s1": [["0", "1", "green"], ["1", "10", "red"]], "s2": [["0", "8", "yellow"]]}


def test_the_client_model_uses_the_core_document_shape() -> None:
    model = client_model("sig", "tok", _opened(), _media(), TIERS)
    doc = model["doc"]
    assert doc["facts"] == {
        "sequence": {"num": 1, "den": 30},
        "sources": [
            {"id": "s1", "num": 1, "den": 30, "frames": 300},
            {"id": "s2", "num": 1001, "den": 30000, "frames": 240},
        ],
    }
    assert doc["clips"] == [["s1", 30, 90], ["s2", 0, 60]]
    assert doc["seeded"] == {"s1": "r1", "s2": "r1"}
    assert model["names"] == {"s1": "<A>.mp4", "s2": "B.mp4"}
    assert model["frame"] == {"width": 1920, "height": 1080}
    assert model["media"]["s2"]["fileId"] == "f2"
    assert (model["signature"], model["token"], model["revision"]) == ("sig", "tok", 2)
    assert model["tiers"] == TIERS
    assert model["settings"] == {"maxCues": 5000}


def test_the_page_escapes_the_model_and_has_two_decks_and_its_own_keys() -> None:
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    assert '</div>"' not in page and "<A>" not in page
    assert _model_of(page)["names"]["s1"] == "<A>.mp4"
    assert page.count("<video") == 2
    assert "data-vl-own-keys" in page and 'tabindex="0"' in page
    assert "aspect-ratio: 1920 / 1080" in page
    empty = timeline_player_html(None)
    assert "<video" not in empty and "data-tl-model" in empty


def test_the_clip_edit_buttons_are_on_the_page_and_every_button_has_a_handler() -> None:
    """W-71f — 자르기(⌘B)·클립 지우기(⌫)·합치기 단추 · 모든 `data-tl` 단추를 실행본이 받는다."""
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    actions = set(re.findall(r'data-tl="([a-z-]+)"', page))
    assert {"blade", "delete-clip", "join"} <= actions
    for action in actions:
        assert re.search(rf'(^|[\s{{,])"?{re.escape(action)}"?: function', PLAYER_JS, re.M) or (
            re.search(rf'(^|[\s{{,])"?{re.escape(action)}"?: [A-Za-z]+[,}} ]', PLAYER_JS, re.M)
        ), action
    # 키는 표 하나(W-89, D154) — ⌘B 블레이드 · ⌫/⌦ 삭제는 타임라인 창의 줄이다.
    from ui.keymap import PLAYER_KEYMAP

    clip_keys = {(k.code, k.mod, k.action) for k in PLAYER_KEYMAP if k.pane == "timeline"}
    assert ("KeyB", True, "blade") in clip_keys
    assert {("Backspace", False, "delete-clip"), ("Delete", False, "delete-clip")} <= clip_keys
    assert 'blade: function () { clipOp("blade"); }' in PLAYER_JS
    assert '"delete-clip": function () { clipOp("delete"); }' in PLAYER_JS


def test_the_cue_panel_is_on_the_page_and_its_buttons_have_handlers() -> None:
    """W-71i — 자막 목록·편집 칸이 재생기 안에 있고, 실행본이 그리는 자막 단추도 처리기가 있다."""
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    assert 'id="tl-cue-list"' in page and 'id="tl-cue-edit"' in page
    assert 'data-tl="cue-insert"' in page
    drawn = set(re.findall(r'cueButton\("([a-z-]+)"', PLAYER_JS))
    assert drawn == {"cue-start", "cue-end", "cue-split", "cue-merge", "cue-delete"}
    for action in drawn | {"cue-insert"}:
        assert re.search(rf'"{re.escape(action)}": function', PLAYER_JS), action
    for op in ("start", "end", "split", "merge", "delete", "insert", "text"):
        assert f'cueOp("{op}"' in PLAYER_JS, op


def test_the_cue_keys_follow_the_draft_editor_and_delete_prefers_a_picked_cue() -> None:
    """D117과 같은 글자([ ] ⇧B ⇧M ⌥C ⌥↑↓ Enter) — 재생기 표(W-89, D154)의 같은 동작이다.
    ⌫는 자막 창에서 고른 자막이 있을 때만 자막을 지우고(없으면 할 일이 없다 — 클립을 지우지 않는다),
    타임라인 창에서는 클립을 지운다."""
    from ui.keymap import PLAYER_KEYMAP

    player = {(k.code, k.shift, k.alt, k.mod): k.action for k in PLAYER_KEYMAP if k.pane == "any"}
    assert player[("BracketLeft", False, False, False)] == "cue-start"
    assert player[("BracketRight", False, False, False)] == "cue-end"
    assert player[("KeyB", True, False, False)] == "cue-split"
    assert player[("KeyM", True, False, False)] == "cue-merge"
    assert player[("KeyC", False, True, False)] == "cue-insert"
    assert player[("ArrowUp", False, True, False)] == "prev-cue"
    assert player[("ArrowDown", False, True, False)] == "next-cue"
    delete = PLAYER_JS[PLAYER_JS.index('"delete-cue": function () {') :][:240]
    assert "if (!S.cue) { return false; }" in delete and 'cueOp("delete", S.cue);' in delete


def test_an_unconfirmed_cue_text_is_committed_before_saving_rendering_adding_or_hiding() -> None:
    """글 칸에 쓴 채로 렌더·넣기·⌘S·창 숨기기를 해도 그 글이 빠지지 않는다."""
    for head in ("function render() {", "function addSource() {"):
        body = PLAYER_JS[PLAYER_JS.index(head) :]
        assert body.index("commitText();") < body.index("\n  }\n"), head
    assert "commitText(); saveNow(false);" in PLAYER_JS
    hidden = PLAYER_JS[PLAYER_JS.index('addEventListener("visibilitychange"') :]
    assert hidden.index("commitText();") < hidden.index("saveNow(false)")


def test_the_cue_buttons_do_not_move_when_a_text_commit_changes_the_status_or_warnings() -> None:
    """글 칸을 떠나며 확정하면 상태·경고 줄이 바뀐다 — 그 사이 단추가 움직이면 누름을 잃는다.

    오프라인 앱에서 실제로 잃었다: 상태 글이 이동 막대를 한 줄 더 감아 아래가 23px 내려갔고, 빈 글
    경고가 사라지며 단추가 올라갔다. 상태 줄은 한 줄 고정 · 경고 줄은 비어도 높이를 둔다.
    """
    css = timeline_player.TIMELINE_PLAYER_CSS
    status = css[css.index(".tl-status {") :].split("}")[0]
    assert "flex: 1 0 100%" in status and "white-space: nowrap" in status
    warn = css[css.index(".tl-cue-edit .tl-cue-warn {") :].split("}")[0]
    assert "min-height" in warn
    assert "warn.hidden" not in PLAYER_JS and "status.title = S.status;" in PLAYER_JS


def test_the_view_is_empty_without_a_timeline_and_carries_media_with_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    assert "<video" not in player.view(signature)
    assert "<video" not in player.view("다른-작업공간")
    monkeypatch.setattr(server.raw, "has_timeline", lambda _root: True)
    monkeypatch.setattr(server.raw, "timeline_open", lambda _root, *_a: _opened())
    monkeypatch.setattr(server.raw, "timeline_media", lambda _root, *_a: _media())

    def no_tiers(*_a: Any) -> Any:
        raise AssertionError("그리기는 등급을 읽지 않는다 — 고른 클립의 원본만 그때 묻는다")

    monkeypatch.setattr(server.raw, "timeline_tiers", no_tiers)
    model = _model_of(player.view(signature))
    assert model["tiers"] == {}
    assert model["timelineId"] == "20261113T010203Z"
    assert model["media"]["s1"]["fileId"] == "f1"
    assert model["token"] == server._visual_lifetime_token


def test_media_polls_and_a_stale_signature_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    monkeypatch.setattr(server.raw, "timeline_media", lambda _root, *_a: _media())
    got = player.action({"signature": signature, "timelineId": "t"}, "media")
    assert got["ok"] is True and got["media"]["s2"]["fileId"] == "f2"
    stale = player.action({"signature": "다른", "timelineId": "t"}, "media")
    assert stale["ok"] is False and stale["stale"] is True


def test_prepare_starts_previews_only_for_missing_timeline_sources_and_needs_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    missing = {"state": "missing", "fileId": None, "origin": None}
    monkeypatch.setattr(server.raw, "timeline_media", lambda _root, *_a: _media(s2=missing))
    asked: list[tuple[str, ...]] = []

    def start(_root: Path, asset_ids: tuple[str, ...] = ()) -> Any:
        asked.append(asset_ids)
        return SimpleNamespace(job_id="p1")

    monkeypatch.setattr(server.raw, "start_source_preview", start)
    payload = {"signature": signature, "timelineId": "t", "token": "낡은-토큰"}
    refused = player.action(payload, "prepare")
    assert refused["ok"] is False and asked == []
    payload["token"] = server._visual_lifetime_token
    got = player.action(payload, "prepare")
    assert got["ok"] is True and got["started"] == ["s2"]
    assert asked == [("s2",)]


def test_blocks_mount_the_player_in_the_window_deck(tmp_path: Path, local_socket: None) -> None:
    """W-85(D145) — 재생기는 「영상 정리」 무대(`visual-stage`)의 아래 덱에 있다."""
    server, _root, _signature = _server(tmp_path)
    app = server.build_blocks()
    config = app.config
    by_elem = {c["props"].get("elem_id"): c for c in config["components"]}
    assert PLAYER_BLOCK_ID in by_elem
    ancestors = _ancestor_elem_ids(config, by_elem[PLAYER_BLOCK_ID]["id"], by_elem)
    assert "vl-timeline-deck" in ancestors and "visual-stage" in ancestors
    deps = config["dependencies"]
    fns = {dep["id"]: app.fns[dep["id"]].fn for dep in deps}
    views = [d for d in deps if fns[d["id"]] == server._timeline_player.view]
    assert len(views) >= 6  # 작업공간 사건마다 + 「타임라인으로 보내기」 성공 뒤


def test_save_needs_the_token_and_passes_the_document_and_base_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    calls: list[tuple[Any, ...]] = []

    def save(_root: Path, timeline_id: str, base: int, doc: Any, *, force: bool = False) -> Any:
        calls.append((timeline_id, base, doc, force))
        return {"revision": base + 1, "summary": {"clips": 2}, "warnings": []}

    monkeypatch.setattr(server.raw, "timeline_save", save)
    doc = {"clips": [["s1", 0, 30]], "cues": [], "nextCue": 1, "seeded": {}}
    payload: dict[str, object] = {
        "signature": signature,
        "token": "낡은-토큰",
        "timelineId": "t1",
        "baseRevision": 3,
        "doc": doc,
    }
    refused = player.action(payload, "save")
    assert refused["ok"] is False and refused["stale"] is True and calls == []
    payload["token"] = server._visual_lifetime_token
    got = player.action(payload, "save")
    assert got == {"ok": True, "revision": 4, "summary": {"clips": 2}, "warnings": []}
    assert calls == [("t1", 3, doc, False)]
    payload["force"] = True
    player.action(payload, "save")
    assert calls[-1][3] is True
    broken = player.action({**payload, "baseRevision": "3"}, "save")
    assert broken["ok"] is False and broken["stale"] is False


def test_adding_a_source_needs_the_token_and_passes_the_document_revision_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-71g — 원본 넣기는 쓰기라 저장처럼 서명·수명 토큰에 묶이고 받은 결과를 그대로 돌려준다."""
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    calls: list[tuple[Any, ...]] = []

    def add(_root: Path, timeline_id: str, base: int, doc: Any, source_id: str) -> Any:
        calls.append((timeline_id, base, doc, source_id))
        return {"revision": base + 1, "doc": doc, "cues": 3, "empty": 1, "clamped": 0}

    monkeypatch.setattr(server.raw, "timeline_add_source", add)
    doc = {"clips": [["s1", 0, 30]], "cues": [], "nextCue": 1, "seeded": {}}
    payload: dict[str, object] = {
        "signature": signature,
        "token": "낡은-토큰",
        "timelineId": "t1",
        "baseRevision": 3,
        "doc": doc,
        "sourceId": "s2",
    }
    refused = player.action(payload, "add")
    assert refused["ok"] is False and refused["stale"] is True and calls == []
    payload["token"] = server._visual_lifetime_token
    got = player.action(payload, "add")
    assert got == {"ok": True, "revision": 4, "doc": doc, "cues": 3, "empty": 1, "clamped": 0}
    assert calls == [("t1", 3, doc, "s2")]
    broken = player.action({**payload, "sourceId": ""}, "add")
    assert broken["ok"] is False and broken["stale"] is False
    assert "tl_add_source" in [f.__name__ for f in player.server_functions()]


def test_render_needs_the_token_and_starts_the_timeline_render_at_that_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    asked: list[tuple[str, int]] = []

    def start(_root: Path, timeline_id: str, revision: int) -> Any:
        asked.append((timeline_id, revision))
        return SimpleNamespace(job_id="r1")

    monkeypatch.setattr(server.raw, "start_timeline_render", start)
    payload: dict[str, object] = {
        "signature": signature,
        "token": "낡은-토큰",
        "timelineId": "t1",
        "revision": 5,
    }
    assert player.action(payload, "render")["ok"] is False and asked == []
    payload["token"] = server._visual_lifetime_token
    assert player.action(payload, "render") == {"ok": True, "jobId": "r1"}
    assert asked == [("t1", 5)]
    assert player.action({**payload, "revision": "5"}, "render")["ok"] is False


def test_share_with_burn_captions_starts_the_share_job_and_none_stays_a_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-88(D147) — 「번인 캡션」을 고르면 같은 렌더 단추가 공유 작업(렌더 + 입히기)을 건다."""
    from vlogsrt.project.raw import timeline_share

    assert timeline_player.BURN_NONE == timeline_share.BURN_NONE
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    shared: list[tuple[str, int, str]] = []
    rendered: list[tuple[str, int]] = []

    def share(_root: Path, timeline_id: str, revision: int, burn: str) -> Any:
        shared.append((timeline_id, revision, burn))
        return SimpleNamespace(job_id="s1")

    def render(_root: Path, timeline_id: str, revision: int) -> Any:
        rendered.append((timeline_id, revision))
        return SimpleNamespace(job_id="r1")

    monkeypatch.setattr(server.raw, "start_timeline_share", share)
    monkeypatch.setattr(server.raw, "start_timeline_render", render)
    payload: dict[str, object] = {
        "signature": signature,
        "token": server._visual_lifetime_token,
        "timelineId": "t1",
        "revision": 5,
    }
    assert player.action({**payload, "burn": "ko-en"}, "render") == {"ok": True, "jobId": "s1"}
    assert player.action({**payload, "burn": "none"}, "render") == {"ok": True, "jobId": "r1"}
    assert shared == [("t1", 5, "ko-en")] and rendered == [("t1", 5)]
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    assert 'id="tl-burn"' in page and ">공유</button>" in page
    for value in timeline_share.BURN_MODES:
        assert f'<option value="{value}"' in page, value
    assert 'burn: burnPick ? burnPick.value : "none"' in PLAYER_JS


def test_job_reports_a_render_job_and_the_script_watches_it_to_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-71h — 재생기가 제 렌더(공유) 작업을 묻고 끝을 안다. 옛 「초안 편집」 칸을 다시 여는 일은
    그 탭과 함께 은퇴했다(W-91, D161)."""
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    jobs = {
        "j1": SimpleNamespace(job_id="j1", kind="render_export", status="running"),
        "j2": SimpleNamespace(job_id="j2", kind="visual_prepare", status="succeeded"),
    }
    monkeypatch.setattr(server.raw, "get_job", lambda _root, job_id: jobs[job_id])
    got = player.action({"signature": signature, "jobId": "j1"}, "job")
    assert got == {"ok": True, "status": "running", "ended": False}
    jobs["j1"].status = "succeeded"
    assert player.action({"signature": signature, "jobId": "j1"}, "job")["ended"] is True
    foreign = player.action({"signature": signature, "jobId": "j2"}, "job")
    assert foreign["ok"] is False
    stale = player.action({"signature": "다른-서명", "jobId": "j1"}, "job")
    assert stale["ok"] is False and stale["stale"] is True
    assert "tl_job" in [f.__name__ for f in player.server_functions()]
    model = client_model("sig", "tok", _opened(), _media(), TIERS)
    assert "reloadId" not in model
    assert "S.server.tl_job(" in PLAYER_JS and "watchRender(got.jobId)" in PLAYER_JS
    assert "reloadId" not in PLAYER_JS and "reopenDraft" not in PLAYER_JS


def test_tiers_are_asked_per_source_when_a_clip_is_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    asked: list[list[str]] = []

    def tiers(_root: Path, ids: list[str]) -> Any:
        asked.append(list(ids))
        return {i: TIERS[i] for i in ids}

    monkeypatch.setattr(server.raw, "timeline_tiers", tiers)
    got = server._timeline_player.action({"signature": signature, "sourceId": "s1"}, "tiers")
    assert got == {"ok": True, "sourceId": "s1", "tiers": TIERS["s1"]}
    assert asked == [["s1"]]
    stale = server._timeline_player.action({"signature": "다른", "sourceId": "s1"}, "tiers")
    assert stale["ok"] is False and asked == [["s1"]]


def test_a_timeline_that_cannot_be_read_says_why_instead_of_an_empty_player(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.raw.service import RawWorkspaceError

    server, _root, signature = _server(tmp_path)

    def broken(_root: Path, *_a: Any) -> Any:
        raise RawWorkspaceError("타임라인 `x`의 판이 다르다")

    monkeypatch.setattr(server.raw, "has_timeline", lambda _root: True)
    monkeypatch.setattr(server.raw, "timeline_open", broken)
    page = server._timeline_player.view(signature)
    assert "<video" not in page and "판이 다르다" in page


def test_the_player_script_takes_its_numbers_from_the_settings() -> None:
    """규칙 8 — 큰 걸음·손잡이 폭·되돌리기 칸·자동 저장은 서버 설정에서 온다(대체 숫자 없음)."""
    for name in ("bigStepFrames", "snapPx", "undoLimit", "autosaveMs"):  # newCueMs는 핵심이 쓴다
        assert f"S.settings.{name}" in PLAYER_JS
    assert "BIG_STEP" not in PLAYER_JS and "HANDLE_PX" not in PLAYER_JS
    assert "|| 200" not in PLAYER_JS and "|| 1500" not in PLAYER_JS


@needs_node
def test_the_player_script_parses(tmp_path: Path) -> None:
    script = tmp_path / "player.js"
    script.write_text(f"(function (element, server) {{ {PLAYER_JS} }})", encoding="utf-8")
    subprocess.run(["node", "--check", str(script)], check=True, timeout=60)


def test_the_module_does_not_import_the_project_layer() -> None:
    source = Path(timeline_player.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import) vlogsrt", source, re.M)


def test_the_cue_editor_is_not_rebuilt_by_picking_and_playback_scrolls_only_the_list() -> None:
    """확정이 고름을 바꿔도 단추는 그대로다 · 재생 중 목록 따라가기는 페이지를 움직이지 않는다."""
    assert "box.__tlSel" not in PLAYER_JS and 'id="tl-cue-head"' in PLAYER_JS
    assert ".scrollIntoView(" not in PLAYER_JS
    assert "keepInList(list, row)" in PLAYER_JS
    css = timeline_player.TIMELINE_PLAYER_CSS
    assert "position: relative" in css[css.index(".tl-cue-list {") :].split("}")[0]


def _ancestor_elem_ids(config: dict[str, Any], target: int, by_elem: dict[Any, Any]) -> list[str]:
    """그라디오 배치 나무에서 `target` 위의 elem_id들 — 가까운 것부터."""
    names = {c["id"]: c["props"].get("elem_id") for c in config["components"]}

    def walk(node: dict[str, Any], path: list[int]) -> list[int] | None:
        if node.get("id") == target:
            return path
        for child in node.get("children", []):
            found = walk(child, [*path, node["id"]] if "id" in node else path)
            if found is not None:
                return found
        return None

    path = walk(config["layout"], []) or []
    return [names[i] for i in reversed(path) if names.get(i)]


def test_the_timeline_index_lists_clips_and_cues_and_filters_errors() -> None:
    """W-86c(D150) — Final Cut 「인덱스」: 클립 · 자막(「오류 보기」) 목록, 누르면 그 자리로."""
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    for needle in ('id="tl-index-toggle"', 'id="tl-index"', 'id="tl-index-errors"',
                   'data-tl="index-clips"', 'data-tl="index-cues"', ">플레이헤드 자르기</button>",
                   ">실행 취소</button>", ">다시 실행</button>", ">목록</button>"):  # fmt: skip
        assert needle in page, needle
    for action in ("index", "index-clips", "index-cues"):
        assert re.search(rf'"{re.escape(action)}": ', PLAYER_JS) or f"{action}: " in PLAYER_JS
    assert "!errorsOnly || row.warned" in PLAYER_JS
    assert 'indexList.addEventListener("pointerdown", onIndexDown);' in PLAYER_JS


def test_the_model_carries_the_burn_style_for_the_overlay() -> None:
    """W-98(D173) — 자막 겹은 저장한 번인 모양을 근사한다(모양 · 기준 높이 · 영어 크기 · 여백)."""
    from ui.timeline_player import burn_model
    from vlogsrt.project.raw import burn_in
    from vlogsrt.project.raw.burn_config import BURN_SETTINGS

    style = burn_in.BurnStyle("top", 70, 2, 30, "#FFCC00")
    model = client_model("sig", "tok", _opened(), _media(), TIERS, burn_model(style, BURN_SETTINGS))
    assert model["burn"] == {
        "style": {"position": "top", "font_px": 70, "outline_px": 2, "margin_px": 30,
                  "text_color": "#FFCC00"},
        "referenceHeight": 1080, "englishScale": 0.8, "sideMargin": 0.05,
        "outlineColor": "#000000",
    }  # fmt: skip
    assert client_model("sig", "tok", _opened(), _media(), TIERS)["burn"] is None
    js = PLAYER_JS
    assert "function paintOverlay()" in js and "S.setBurnStyle = function (style)" in js
    # 영어 줄은 고친 영어(낡지 않은 것) 또는 저장된 번역만 — 「번역 없음」 같은 표시는 싣지 않는다.
    assert 'label.kind === "edited" || label.kind === "saved" ? label.text : ""' in js
    painter = js.split("function overlayCues")[1].split("S.setBurnStyle")[0]
    assert "line.textContent = text;" in painter and "innerHTML" not in painter


def test_make_english_needs_the_token_and_translates_that_revisions_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-99(D174) — 「영어 만들기」는 수명 토큰에 묶인 유료 누름이다. 저장한 리비전의 SRT를
    「영어 SRT로 변환」과 같은 작업에 넘기고, 이미 옮긴 글이면 연결을 묻지 않는다."""
    from vlogsrt.project.raw import service as raw_service

    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    srt = tmp_path / "korean-r5.srt"
    started: list[tuple[Path, object]] = []
    monkeypatch.setattr(server.raw, "timeline_korean_srt", lambda _r, _t, revision: srt)
    monkeypatch.setattr(server.raw, "srt_translation_cached", lambda _r, _s: True)
    monkeypatch.setattr(
        server,
        "_live_transport_for_action",
        lambda: pytest.fail("이미 옮긴 글 — 연결을 묻지 않는다"),
    )

    def start(_root: Path, path: Path, client: object) -> Any:
        started.append((path, client))
        return SimpleNamespace(job_id="e1")

    monkeypatch.setattr(server.raw, "start_srt_translation", start)
    payload: dict[str, object] = {
        "signature": signature, "token": "낡은-토큰", "timelineId": "t1", "revision": 5,
    }  # fmt: skip
    assert player.action(payload, "translate")["ok"] is False and started == []
    payload["token"] = server._visual_lifetime_token
    assert player.action(payload, "translate") == {"ok": True, "jobId": "e1"}
    assert started == [(srt, None)]
    assert player.action({**payload, "revision": True}, "translate")["ok"] is False
    jobs = {"e1": SimpleNamespace(job_id="e1", kind=raw_service.SRT_TRANSLATE_JOB_KIND,
                                  status="succeeded")}  # fmt: skip
    monkeypatch.setattr(server.raw, "get_job", lambda _root, job_id: jobs[job_id])
    assert player.action({"signature": signature, "jobId": "e1"}, "job")["ended"] is True
    assert "tl_translate" in [f.__name__ for f in player.server_functions()]
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    assert 'data-tl="translate"' in page and ">영어 만들기</button>" in page
    assert "translate: translate" in PLAYER_JS and "watchTranslate(got.jobId)" in PLAYER_JS


def test_one_screen_is_the_default_and_the_keys_live_in_the_top_bar_sheet() -> None:
    """W-102(D175, 사용자 지시 「한화면에서 보이도록 · 단축키는 오른쪽 위 단추에 몰아서」) — 맞춤은
    기억이 없으면 켜지고, 맞춤이면 재생기가 두 열로 덱 남은 높이를 받는다 — 파형 레인이 최소
    높이를 못 받으면 예전처럼 쌓는다(0으로 잘라 숨기지 않는다). 재생기 안에는 단축키 시트가 없다."""
    from ui import keymap, ui_facade, window_js

    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    assert 'class="tl-keys"' not in page and "<details" not in page
    assert keymap.keysheet_sections_html() in ui_facade.keysheet_html()
    assert "return stored === null || stored === CFG.fitOn;" in window_js.window_bind_js()
    assert "function fitDeck()" in PLAYER_JS and "S.settings.fitLaneMinPx" in PLAYER_JS
    fallback = (
        "lane.clientHeight < S.settings.fitLaneMinPx + S.settings.laneFilmPx) {\n"
        '      root.classList.remove("is-fit");'
    )
    assert fallback in PLAYER_JS
    # 끄는 동안 미리 그린 문서는 저장하지 않는다(D169) · 공유 뒤 영어는 모드와 상관없이 찾는다.
    assert "if (S.clipDrag) { scheduleSave(); return; }" in PLAYER_JS
    assert "S.english = null;\n        askEnglish();" in PLAYER_JS


def test_make_english_without_a_connection_answers_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-99(D174) — 키가 없어 연결을 못 만들면(화면 글 `gr.Error`) 값으로 까닭을 돌려준다(조용히
    실패하지 않는다). 파형 레인 끌기도 반올림 여유를 둔 간격을 쓴다(W-101, D170)."""
    import gradio as gr

    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    monkeypatch.setattr(server.raw, "timeline_korean_srt", lambda *_a: tmp_path / "k.srt")
    monkeypatch.setattr(server.raw, "srt_translation_cached", lambda _r, _s: False)

    def no_key() -> Any:
        raise gr.Error("키가 없다")

    monkeypatch.setattr(server, "_live_transport_for_action", no_key)
    payload = {"signature": signature, "token": server._visual_lifetime_token,
               "timelineId": "t1", "revision": 1}  # fmt: skip
    got = player.action(payload, "translate")
    assert got["ok"] is False and "키가 없다" in str(got["error"])
    assert "S.server.tl_translate(payload).catch(" in PLAYER_JS
    for needle in ("laneSafeGap(), seqTicks(), laneTotal());",
                   "ticks - drag.origin, laneSafeGap(), laneTotal());"):  # fmt: skip
        assert needle in PLAYER_JS, needle


def test_the_deck_reads_at_a_glance_after_the_design_audit() -> None:
    """디자인 점검(U1·U3·U4·U5·U6·U8·U9) — 그리기·표시만 바꾼다(문서·키 뜻은 그대로).

    맞춤을 재다 물러서도 덱 스크롤 자리를 지킨다 · 세 캔버스의 플레이헤드는 하나의 모양(빨강은 오류
    몫이라 자막 끝 가장자리는 노랑 ink) · 시간 눈금 · 클립 채움 토큰 · 키 입력 창 표지 · 저장 표지 ·
    「공유」가 주 동작이고 쉬운 이름을 쓴다."""
    css = timeline_player.TIMELINE_PLAYER_CSS
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    fit = PLAYER_JS[PLAYER_JS.index("function fitDeck()") :].split("S.fitDeck = fitDeck;")[0]
    assert "var kept = deck ? deck.scrollTop : 0;" in fit and fit.count("scrollTop = kept") == 2
    assert '"transport head" "strip body" "range body" "lane body"' in css
    for painter in ("function paintNav()", "function paintSource()", "function paintLane()"):
        body = PLAYER_JS[PLAYER_JS.index(painter) :].split("\n  }\n")[0]
        assert "paintHead(" in body and '"--vl-danger"' not in body, painter
    head = PLAYER_JS[PLAYER_JS.index("function paintHead(") :].split("\n  }\n")[0]
    assert '"--vl-playhead-casing"' in head and '"--vl-playhead"' in head
    cues = PLAYER_JS[PLAYER_JS.index("function paintLaneCues(") :].split("\n  }\n")[0]
    assert '"--vl-tier-yellow-ink"' in cues and '"--vl-danger"' not in cues
    # 레인 눈금은 맨 위(W-103, D176 — Final Cut), 전체 보기 띠에는 눈금이 없다.
    assert "paintRuler(ctx, width, 0, view.start" in PLAYER_JS
    assert "paintSourceEnds(g, w, h, source.frames * seconds);" in PLAYER_JS
    assert '"--vl-clip-fill"' in PLAYER_JS and '"--vl-clip-fill-alt"' in PLAYER_JS
    assert 'content: "키 입력"' in css and "0 0 0 2px var(--vl-accent-ink)" in css
    assert ".tl-cue-row.is-current { background: var(--vl-surface-hover); }" in css
    assert 'id="tl-save"' in page and "S.saving = true;\n    paintSaveChip();" in PLAYER_JS
    for needle in ('class="tl-divider"', 'id="tl-render" class="tl-primary"',
                   '영상에 넣을 자막 <select id="tl-burn"'):  # fmt: skip
        assert needle in page, needle
    for old in (">블레이드<", ">실행 복귀<", ">인덱스<", "번인 캡션 <select"):
        assert old not in page, old


def test_the_storyline_reads_like_final_cut() -> None:
    """W-103(D176) — 사용자 화면에서 클립 112개가 화면 폭 클립 띠에 20화소도 안 돼 읽히지 않았다.

    위 띠는 전체 보기(창 · 플레이헤드, 글 없음)이고, 읽고 고치는 곳은 레인 위쪽 스토리라인이다 —
    맨 위 시간 눈금(끌면 플레이헤드) · 이름 줄 + 미리보기 장면 필름 띠 · 노란 고름 테두리 ·
    노란 트림 손잡이와 끄는 동안의 프레임 표 · 자르기 도구(B)의 자를 선 · 스키머(S) · 스내핑(N) ·
    확대 막대.
    그리기와 화면 상태뿐이다 — 연산은 예전 그대로(`clipOp("blade")` · `TL.clampEdge`)."""
    css = timeline_player.TIMELINE_PLAYER_CSS
    page = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    for needle in ('id="tl-tool-select" aria-pressed="true"', 'id="tl-tool-blade"',
                   'id="tl-skim"', 'id="tl-snap"', 'id="tl-zoom"', ">↖ 선택</button>",
                   ">✂ 자르기</button>", ">플레이헤드 자르기</button>"):  # fmt: skip
        assert needle in page, needle
    assert ".tl-strip { position: relative; height: 22px;" in css
    assert ".tl-lane { display: block; width: 100%; height: 164px;" in css
    nav = PLAYER_JS[PLAYER_JS.index("function paintNav()") :].split("\n  }\n")[0]
    assert "LN.navWindow(laneView()" in nav and "fillText" not in nav
    band = "return S.settings.laneClipPx + S.settings.laneFilmPx + S.settings.laneWavePx;"
    assert band in PLAYER_JS
    # D177 — 파형은 클립 카드 안(필름 아래 줄), 자막은 스토리라인 위 칸, 스토리라인은 레인 맨 아래.
    assert "paintStoryAudio(ctx, view, xs, xe, width, fills[tone]);" in PLAYER_JS
    assert "bandTop = Math.max(RULER_PX, height - band);" in PLAYER_JS
    assert 'return y >= laneLayout(c.clientHeight).bandTop ? "band" : "lane";' in PLAYER_JS
    # 끝에 올리면 원본의 어디서 잘렸는지, 더 늘릴 원본이 없으면 빨강(Final Cut 미디어 끝).
    assert "TL.clampEdge(doc, i, side, outward, S.settings) !== edge" in PLAYER_JS
    assert '"더 늘릴 원본 없음"' in PLAYER_JS
    frame = PLAYER_JS[PLAYER_JS.index("function paintStoryFrame(") :].split("\n  }\n")[0]
    assert '"--vl-tier-yellow-ink"' in frame
    blade = 'if (S.tool === "blade") { seek(frameAtTicks(ticks)); clipOp("blade"); return; }'
    assert blade in PLAYER_JS
    assert 'if (zone === "ruler") { rulerDown(ev, ticks); return; }' in PLAYER_JS
    assert "paintSkimmer(ctx, height);" in PLAYER_JS and '"--vl-skimmer"' in PLAYER_JS
    # 필름 띠는 덱 둘과 따로인 영상 하나 — 문서에 붙이지 않는다(덱 · timeupdate가 모른다).
    grab = PLAYER_JS[PLAYER_JS.index("function filmVideo()") :].split("\n  }\n")[0]
    assert 'document.createElement("video")' in grab and "appendChild" not in grab
    assert 'function decks() { return [$("tl-deck-a"), $("tl-deck-b")]; }' in PLAYER_JS
    # 스키밍 중 재생은 스키머 자리에서(Final Cut).
    assert "var skimmed = skimFrame();" in PLAYER_JS
