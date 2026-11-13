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
from ui.final_edit_app_js import APP_JS  # noqa: E402
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


def test_d117_keys_leave_the_player_alone() -> None:
    """초안 편집의 문서 키 처리기(캡처 단계)가 재생기 안의 Space·화살표를 가로채지 않는다."""
    assert 'closest("[data-vl-own-keys]")' in APP_JS


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
    monkeypatch.setattr(server.raw, "timeline_tiers", lambda _root, ids: {i: [] for i in ids})
    model = _model_of(player.view(signature))
    assert model["tiers"] == {"s1": [], "s2": []}
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


def test_blocks_mount_the_player_in_the_final_edit_tab(tmp_path: Path, local_socket: None) -> None:
    server, _root, _signature = _server(tmp_path)
    app = server.build_blocks()
    config = app.config
    by_elem = {c["props"].get("elem_id"): c for c in config["components"]}
    assert PLAYER_BLOCK_ID in by_elem
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


@needs_node
def test_the_player_script_parses(tmp_path: Path) -> None:
    script = tmp_path / "player.js"
    script.write_text(f"(function (element, server) {{ {PLAYER_JS} }})", encoding="utf-8")
    subprocess.run(["node", "--check", str(script)], check=True, timeout=60)


def test_the_module_does_not_import_the_project_layer() -> None:
    source = Path(timeline_player.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import) vlogsrt", source, re.M)
