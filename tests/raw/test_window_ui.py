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
    body = PLAYER_APP_JS[PLAYER_APP_JS.index("공유를 마쳤습니다") :][:400]
    assert 'W.__vlWindow.showInspector("share")' in body


_SPLIT = r"""
const WIN = require(process.argv[2]);
const r = (pair) => pair.map((v) => Math.round(v * 1000) / 1000);
console.log(JSON.stringify([
  r(WIN.splitFr(539, 539, 500, 500, 100, 240, 320)),   // 오른쪽으로 100px
  r(WIN.splitFr(539, 539, 500, 500, -400, 240, 320)),  // 왼쪽 최소 240px에서 멈춘다
  r(WIN.splitFr(539, 539, 500, 500, 400, 240, 320)),   // 오른쪽 최소 320px에서 멈춘다
  r(WIN.splitFr(1, 1, 300, 300, 0, 160, 160)),         // 그대로
]));
"""


@needs_node
def test_a_track_handle_moves_the_boundary_inside_both_minimums(tmp_path: Path) -> None:
    """W-86d(D151) — 경계를 끈 화소만큼 비율을 옮기되 두 칸 모두 최소 폭(토큰) 아래로 가지 않는다.
    두 칸 비율의 합은 그대로라 나머지 열은 움직이지 않는다."""
    core = tmp_path / "window.js"
    core.write_text(window_js.WINDOW_CORE_JS, encoding="utf-8")
    runner = tmp_path / "split.js"
    runner.write_text(_SPLIT, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(done.stdout) == [
        [646.8, 431.2],
        [258.72, 819.28],
        [733.04, 344.96],
        [1, 1],
    ]


def test_track_handles_override_stage_tokens_and_remember_per_browser() -> None:
    """손잡이는 무대의 열·덱 비율 변수만 덮는다(격자를 다시 쓰지 않는다) — 열 비율 변수는
    무대 자신에 선언되어 있어 인라인 덮기가 그 칸에 먹는다.
    기억은 이 브라우저에만(실패해도 된다)."""
    js = window_js.window_bind_js()
    config = json.loads(js[js.index("var CFG = ") + 10 : js.index(";\n  if (WIN.bound")])
    assert config["splitKey"] == window_js.SPLIT_STORAGE_KEY
    for name in ("browser", "viewer", "inspector", "upper-deck", "lower-deck"):
        assert f'"{name}"' in js
    assert 'stage.style.setProperty("--vl-" + split.a + "-fr"' in js
    assert 'setAttribute("role", "separator")' in js
    from ui import design_tokens

    for token in ("browser-min", "viewer-min", "inspector-min", "deck-min", "index-w"):
        assert token in design_tokens.SCALAR_TOKENS
    css = workspace_service.CSS
    assert "--vl-list-col: minmax(var(--vl-browser-min), var(--vl-browser-fr));" in css
    assert '.vl-split[aria-orientation="vertical"]' in css


def test_the_index_stands_left_of_the_timeline_on_wide_screens() -> None:
    """Final Cut 인덱스처럼 덱의 왼쪽 열 — 넓은 화면에서만, 덱을 제 내용으로 늘리지 않는다."""
    from ui.timeline_player import TIMELINE_PLAYER_CSS

    wide = TIMELINE_PLAYER_CSS[TIMELINE_PLAYER_CSS.index("@media (min-width: 1051px)") :]
    assert '"index strip" "index range"' in wide
    assert "minmax(0, var(--vl-index-w))" in wide
    assert "contain: size" in wide


def test_the_captions_workspace_moves_the_caption_panes_not_copies_them() -> None:
    """W-87c(D152) — 「자막」 작업공간은 재생기의 자막 목록 · 입력칸을 노드째 브라우저 열로 옮기고
    「기본」이면 제자리로 돌린다(지우거나 새로 짓지 않는다). 고른 작업공간은 이 브라우저에만."""
    js = window_js.window_bind_js()
    config = json.loads(js[js.index("var CFG = ") + 10 : js.index(";\n  if (WIN.bound")])
    assert config["workspaces"] == [["basic", "기본"], ["captions", "자막"]]
    assert config["captions"] == window_js.CAPTIONS_ID
    assert "holder.appendChild(deckBody)" in js and "home.appendChild(moved)" in js
    assert ".remove()" not in js and "cloneNode" not in js
    assert 'holder.setAttribute("data-vl-own-keys", "")' in js
    css = window_js.window_css()
    assert '#visual-stage[data-vl-workspace="captions"] .tl-lane' in css
    assert "var(--vl-caption-lane-h)" in css
    from ui import design_tokens

    assert design_tokens.SCALAR_TOKENS["caption-lane-h"] == "192px"


def test_moved_caption_panes_still_reach_the_player_keys_once() -> None:
    """옮긴 칸의 키·누름은 뿌리까지 오지 않으니 밖에 있을 때만 같은 처리로 넘긴다."""
    bind = PLAYER_APP_JS[PLAYER_APP_JS.index("function bindDom() {") :]
    assert 'var body = rootEl.querySelector(".tl-cues-body");' in bind
    assert "if (S.rootEl && !S.rootEl.contains(body)) { onKey(ev); }" in bind
    assert "if (S.rootEl && !S.rootEl.contains(body)) { onClick(ev); }" in bind


def test_many_picked_cues_move_and_delete_as_one_change() -> None:
    """여러 줄 고름(W-87c) — 몸통 끌기는 `LN.bodyMany`, ⌫는 한 번의 변경(실행 취소 한 번)."""
    assert "LN.bodyMany(cues, drag.indices" in PLAYER_APP_JS
    assert "LN.edgePair(cues, drag.index" in PLAYER_APP_JS
    assert 'pickFromList(row.getAttribute("data-cue"), ev)' in PLAYER_APP_JS
    for label in (
        '"자막 경계 함께 옮기기(⌥)"',
        '"자막 " + ids.length + "개 옮기기"',
        '"자막 " + ids.length + "개 지우기"',
    ):
        assert label in PLAYER_APP_JS
