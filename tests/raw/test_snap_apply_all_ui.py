"""W-25e — 작업대의 「모든 영상 시각 맞춤 확인」·「확인한 영상 모두 적용」(D75).

유료 호출은 없다. 포트는 7993(시험용)만 쓴다. 시험이 못 박는 것:

- 확인하기 전에는 모델에 `all` 칸이 없다(그리기 바이트 그대로). 확인하면 요약·영상마다 한 줄·토큰이
  실리고 아무것도 쓰지 않는다.
- 적용은 수명 토큰 문을 지나고, 확인 때의 묶음 토큰이 지금과 같을 때만 쓴다 — 다르면 아무것도 쓰지
  않고 다시 확인하라고 말한다. 적용한 뒤 목록은 걷히고 그 영상에 「되돌리기」가 붙는다.
- 패널은 확인 단추를 언제나, 「모두 적용」은 토큰이 있을 때만 그린다. 말은 존댓말이고 금액·모델
  이름이 없다.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
)

from tests.raw import test_sync_snap_ui as snap_ui  # noqa: E402
from tests.raw.test_sync_snap_apply import wait_sync  # noqa: E402
from tests.raw.test_sync_snap_ui import _call, _source_model  # noqa: E402
from tests.raw.test_visual_workbench_ui import _model_of  # noqa: E402
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.project.raw import service as raw  # noqa: E402
from vlogsrt.project.raw import source_transcript  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
POLITE_END = re.compile(r"(니다|세요|까요)[.)]*$")
bench = snap_ui.bench  # 「시각 맞춤 제안」 칸 시험과 같은 작업대(원본 하나)


def test_check_then_apply_all_through_the_server_functions(bench: tuple[Any, Path, str]) -> None:
    server, root, source_id = bench
    workspace = server.raw._workspace(root)
    assert "all" not in _source_model(server, root, source_id)["snap"]
    single = server.raw.apply_snap_preview(root, source_id)
    checked = _call(server, root, "vl_snap_all_check", {"sourceId": source_id})
    assert checked["ok"] is True and checked["message"] == workspace_service.SNAP_ALL_CHECKED_TEXT
    view = _model_of(checked["html"])["sources"][source_id]["snap"]["all"]
    assert view["summary"] == workspace_service.SNAP_ALL_SUMMARY.format(
        ready=1, lines=single.changes, held=0
    )
    name = server.raw.list_source_assets(root)[0].source_name
    assert view["rows"] == [
        workspace_service.SNAP_ALL_READY_ROW.format(name=name, changed=single.changes)
    ]
    assert view["token"] == server.raw.apply_snap_all_preview(root).token
    assert source_transcript.load(workspace, source_id)["sequence"] == 0  # 확인은 쓰지 않는다
    payload = {"sourceId": source_id, "snapToken": view["token"]}
    assert _call(server, root, "vl_snap_all_apply", payload, token=False)["ok"] is False
    stale = _call(server, root, "vl_snap_all_apply", {**payload, "snapToken": "0" * 64})
    assert stale["message"] == f"{raw.SNAP_ALL_CHANGED} {raw.SNAP_NOTHING_CHANGED}"
    assert source_transcript.load(workspace, source_id)["sequence"] == 0
    _call(server, root, "vl_snap_all_check", {"sourceId": source_id})
    applied = _call(server, root, "vl_snap_all_apply", payload)
    assert applied["message"] == workspace_service.SNAP_ALL_DONE_TEXT.format(applied=1)
    snap = _model_of(applied["html"])["sources"][source_id]["snap"]
    assert "all" not in snap and snap["state"] == "applied"
    assert source_transcript.snap_applied(source_transcript.load(workspace, source_id))
    wait_sync(server.raw, root)


def test_nothing_ready_shows_no_apply_button_value(bench: tuple[Any, Path, str]) -> None:
    server, root, source_id = bench
    single = server.raw.apply_snap_preview(root, source_id)
    server.raw.apply_snap(root, source_id, single.sha256)
    checked = _call(server, root, "vl_snap_all_check", {"sourceId": source_id})
    assert checked["message"] == workspace_service.SNAP_ALL_NONE_TEXT
    view = _model_of(checked["html"])["sources"][source_id]["snap"]["all"]
    assert "token" not in view
    name = server.raw.list_source_assets(root)[0].source_name
    assert view["rows"] == [
        workspace_service.SNAP_ALL_HELD_ROW.format(name=name, reason=raw.SNAP_APPLIED_ONCE)
    ]
    wait_sync(server.raw, root)


def test_the_panel_draws_the_check_button_and_the_apply_button_only_with_a_token() -> None:
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    assert "out.push(snapAllPart(sn.all));" in script
    assert workspace_service.SNAP_ALL_CHECK_LABEL + "</button>" in script
    assert workspace_service.SNAP_ALL_APPLY_LABEL + "</button>" in script
    assert "if (all.token)" in script
    assert "call('vl_snap_all_check'" in script and "call('vl_snap_all_apply'" in script
    assert {"snap-all-check", "snap-all-apply"} <= set(workspace_service.WORKBENCH_TOKEN_ACTIONS)


def test_new_wording_is_polite_and_names_no_model_or_price() -> None:
    texts = [
        workspace_service.SNAP_ALL_EXPLANATION,
        workspace_service.SNAP_ALL_SUMMARY.format(ready=2, lines=40, held=1),
        workspace_service.SNAP_ALL_CHECKED_TEXT,
        workspace_service.SNAP_ALL_NONE_TEXT,
        workspace_service.SNAP_ALL_DONE_TEXT.format(applied=2),
        workspace_service.SNAP_ALL_SKIPPED_TEXT.format(count=1, names="a.mov"),
        raw.SNAP_ALL_CHANGED,
        raw.SNAP_ALL_NOTHING,
    ]
    for text in texts:
        assert POLITE_END.search(text), text
        assert "$" not in text and "VAD" not in text and "whisper" not in text.lower()
        assert "GPT" not in text and "Claude" not in text
