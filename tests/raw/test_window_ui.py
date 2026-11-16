"""뷰어 하나 · 인스펙터 탭(W-86b, D148) — 순수 규칙은 노드에서, 배선은 그라디오 설정과 글자로."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_timeline_player_ui import _ancestor_elem_ids, _server  # noqa: E402
from ui import window_js, workspace_service  # noqa: E402
from ui.timeline_player_js import PLAYER_APP_JS  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

_ZONES = r"""
const WIN = require(process.argv[2]);
const out = [];
let mode = "source";
for (const zone of ["deck", "other", "browser", "program", "source", "other"]) {
  mode = WIN.zoneMode(mode, zone);
  out.push([zone, mode]);
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_the_viewer_follows_what_was_touched_last(tmp_path: Path) -> None:
    """덱·타임라인 화면을 만지면 타임라인, 카드·원본 재생기를 만지면 원본, 그 밖은 그대로."""
    core = tmp_path / "window.js"
    core.write_text(window_js.WINDOW_CORE_JS, encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_ZONES, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(done.stdout) == [
        ["deck", "program"],
        ["other", "program"],
        ["browser", "source"],
        ["program", "program"],
        ["source", "source"],
        ["other", "source"],
    ]


def test_the_player_hands_its_screen_to_the_viewer_and_takes_it_back_when_empty() -> None:
    boot = PLAYER_APP_JS[PLAYER_APP_JS.index("function boot() {") :]
    boot = boot[: boot.index("\n  }\n")]
    assert boot.index("bindDom();") < boot.index("W.__vlWindow.adoptProgram(S.rootEl)")
    assert "W.__vlWindow.releaseProgram()" in boot


def test_inspector_tabs_cover_real_cells_inside_the_inspector(
    tmp_path: Path, local_socket: None
) -> None:
    """탭이 가리는 칸은 모두 인스펙터 열(`vl-quote`) 안의 실제 구성요소다(이름이 바뀌면 깨진다)."""
    server, _root, _signature = _server(tmp_path)
    app = server.build_blocks()
    config = app.config
    by_elem = {c["props"].get("elem_id"): c for c in config["components"]}
    for _key, _label, ids in window_js.INSPECTOR_TABS:
        for elem in ids:
            assert elem in by_elem, elem
            assert window_js.INSPECTOR_ID in _ancestor_elem_ids(
                config, by_elem[elem]["id"], by_elem
            ), elem
    summary = by_elem["vl-timeline-summary"]["props"].get("js_on_load") or ""
    assert "__vlWindow" in summary and "__vlTimerLease" in summary


def test_css_shows_one_side_and_one_tab() -> None:
    css = workspace_service.CSS
    assert window_js.window_css() in css
    assert '#visual-stage[data-vl-viewer="program"] #visual-preview .vl-video-surface' in css
    assert '#vl-quote[data-vl-inspector="analysis"] #vl-burn' in css
    assert '#vl-quote[data-vl-inspector="share"] #vl-approval-mode' in css
    assert "#vl-program .tl-frame { height: var(--vl-deck-viewer-h); }" in css


def test_bind_carries_names_as_data_and_never_deletes_cells() -> None:
    js = window_js.window_bind_js()
    config = json.loads(js[js.index("var CFG = ") + 10 : js.index(";\n  if (WIN.bound")])
    assert config["program"] == "vl-program" and config["inspector"] == "vl-quote"
    assert [tab[0] for tab in config["tabs"]] == ["analysis", "share"]
    # 칸은 숨길 뿐 지우지 않는다 — 옮기는 것은 타임라인 화면 노드 하나다.
    assert ".remove()" not in js
    assert "localStorage" in js and "catch (error)" in js


def test_fit_window_splits_the_decks_by_tokens_and_folds_source_rows_for_the_program() -> None:
    """W-86c(D150) — 「화면 맞춤」: 무대를 화면 높이에 묶고 위·아래 덱을 토큰 비율로 나눈다.
    타임라인 화면이면 원본 레일·추천 구간(원본 쪽 칸)을 접는다. 넓은 화면만."""
    css = window_js.window_css()
    wide = css[css.index("@media (min-width: 1051px)") :]
    assert '#visual-stage[data-vl-fit="on"]:has(#vl-timeline-deck' in wide
    assert "minmax(0, var(--vl-upper-deck-fr))" in wide
    assert "minmax(0, var(--vl-lower-deck-fr))" in wide
    assert '"thumbs player quote" "thumbs timeline ranges" "deck deck deck"' in wide
    assert '[data-vl-viewer="program"] #visual-timeline' in wide
    from ui import design_tokens

    assert design_tokens.SCALAR_TOKENS["upper-deck-fr"] == "1fr"
    assert design_tokens.SCALAR_TOKENS["lower-deck-fr"] == "1fr"
    js = window_js.window_bind_js()
    assert 'id="vl-fit-toggle"' in js and "WIN.showInspector = function (key)" in js


def test_the_player_opens_the_share_tab_when_sharing_ends() -> None:
    body = PLAYER_APP_JS[PLAYER_APP_JS.index("reopenDraft();") :][:200]
    assert 'W.__vlWindow.showInspector("share")' in body
