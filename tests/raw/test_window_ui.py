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
from ui import timeline_player, window_js, workspace_service  # noqa: E402
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
    # 토큰 비율로 나누되 바닥은 실측(2026-11-24 — 16:10 화면에서 잘리지 않게, 아래 시험).
    assert "var(--vl-upper-deck-fr))" in wide and "var(--vl-lower-deck-fr))" in wide
    assert '"thumbs player quote" "thumbs timeline ranges" "deck deck deck"' in wide
    assert '[data-vl-viewer="program"] #visual-timeline' in wide
    from ui import design_tokens

    assert design_tokens.SCALAR_TOKENS["upper-deck-fr"] == "1fr"
    assert design_tokens.SCALAR_TOKENS["lower-deck-fr"] == "1fr"
    js = window_js.window_bind_js()
    assert 'id="vl-fit-toggle"' in js and "WIN.showInspector = function (key)" in js


def test_the_player_opens_the_share_tab_when_sharing_ends() -> None:
    body = PLAYER_APP_JS[PLAYER_APP_JS.index("최종출력을 마쳤습니다") :][:400]
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
    """W-87c(D152) → D178 — 자막 머리 · 목록 · 입력칸은 노드째(지우거나 새로 짓지 않는다) 재생기
    밖으로 옮긴다: 「기본」이면 인스펙터(「분석」 탭, 타임라인 뷰어일 때), 「자막」이면 브라우저 열.
    그래서 아래 덱 타임라인은 한 열(덱 폭 전체)이다. 고른 작업공간은 이 브라우저에만."""
    js = window_js.window_bind_js()
    config = json.loads(js[js.index("var CFG = ") + 10 : js.index(";\n  if (WIN.bound")])
    assert config["workspaces"] == [["basic", "기본"], ["captions", "자막"]]
    assert config["captions"] == window_js.CAPTIONS_ID
    assert "holder.appendChild(deckBody)" in js and "holder.appendChild(deckHead)" in js
    assert "key === CFG.captionsMode ? stage : ($(CFG.inspector) || stage)" in js
    assert ".remove()" not in js and "cloneNode" not in js
    assert 'holder.setAttribute("data-vl-own-keys", "")' in js
    css = window_js.window_css()
    assert '#visual-stage[data-vl-workspace="captions"] .tl-lane' in css
    inspector = (
        '#visual-stage[data-vl-workspace="basic"][data-vl-viewer="program"]\n'
        '  #vl-quote[data-vl-inspector="analysis"] #vl-captions:has(.tl-cues-body)'
    )
    assert inspector in css
    assert 'grid-template-areas: "transport" "strip" "range" "lane"' in (
        timeline_player.TIMELINE_PLAYER_CSS
    )
    assert "var(--vl-caption-lane-h)" in css
    from ui import design_tokens

    assert design_tokens.SCALAR_TOKENS["caption-lane-h"] == "192px"


def test_moved_caption_panes_still_reach_the_player_keys_once() -> None:
    """옮긴 칸의 키·누름은 뿌리까지 오지 않으니 밖에 있을 때만 같은 처리로 넘긴다."""
    bind = PLAYER_APP_JS[PLAYER_APP_JS.index("function forwardOutside(part) {") :]
    assert '[".tl-cues-head", ".tl-cues-body"].forEach' in bind
    assert "if (S.rootEl && !S.rootEl.contains(part)) { onKey(ev); }" in bind
    assert "if (S.rootEl && !S.rootEl.contains(part)) { onClick(ev); }" in bind


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


_PANES = r"""
const WIN = require(process.argv[2]);
const table = JSON.parse(process.argv[3]);
const ev = (code, o) => Object.assign({ code: code, metaKey: false, ctrlKey: false,
  shiftKey: false, altKey: false }, o || {});
console.log(JSON.stringify([
  WIN.paneFor(table, ev("Digit1", { altKey: true }), false),
  WIN.paneFor(table, ev("Digit2", { altKey: true }), false),
  WIN.paneFor(table, ev("Digit3", { altKey: true }), false),
  WIN.paneFor(table, ev("Digit4", { altKey: true }), false),
  WIN.paneFor(table, ev("Digit5", { altKey: true }), false),
  WIN.paneFor(table, ev("Digit1"), false),
  WIN.paneFor(table, ev("Digit1", { altKey: true, metaKey: true }), false),
  WIN.paneFor(table, ev("Digit1", { altKey: true }), true),
]));
"""


@needs_node
def test_alt_digits_move_to_the_final_cut_panes(tmp_path: Path) -> None:
    """⌥1–4(Final Cut ⌘1–4 — 브라우저 · 타임라인 · 뷰어 · 인스펙터).

    다른 수식키 · 글 칸 안은 아니다."""
    core = tmp_path / "window.js"
    core.write_text(window_js.WINDOW_CORE_JS, encoding="utf-8")
    runner = tmp_path / "panes.js"
    runner.write_text(_PANES, encoding="utf-8")
    table = json.dumps([[code, pane] for code, pane, _l, _k in window_js.PANE_KEYS])
    done = subprocess.run(
        ["node", str(runner), str(core), table],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(done.stdout) == [
        "vl-thumbs",
        window_js.DECK_ID,
        window_js.VIEWER_ID,
        window_js.INSPECTOR_ID,
        None,
        None,
        None,
        None,
    ]
    bind = window_js.window_bind_js()
    assert '"paneKeys": [["Digit1", "vl-thumbs"]' in bind
    assert "WIN.paneFor(CFG.paneKeys, ev, inText)" in bind


def test_the_fit_window_never_hides_the_deck_or_the_caption_editor_on_a_16_10_screen() -> None:
    """2026-11-24 사용자 지시 「16:10 맥북에서도 UI가 다 보이게」 — 14인치 크롬 창(1512×860)에서
    아래 덱의 스토리라인이 37px 잘리고(덱 안 스크롤) 번역 모드 자막 편집 칸의 단추 두 줄이 인스펙터
    `overflow: hidden`에 숨었다. 맞춤의 위·아래 덱 줄은 실측 바닥(재생기 실행본이 단다) 아래로 줄지
    않고 영상 · 자막 목록이 먼저 줄며, 바닥의 합이 화면보다 크면 숨기지 않고 무대가 길어진다(문서가
    스크롤한다). 덱 머리의 도구는 요약과 한 줄이다."""
    css = window_js.window_css()
    wide = css[css.index("@media (min-width: 1051px)") :]
    assert (
        "minmax(max(var(--vl-upper-floor, 0px), var(--vl-deck-min)), var(--vl-upper-deck-fr))"
        in wide
    )
    assert "minmax(var(--vl-deck-floor, 0px), var(--vl-lower-deck-fr))" in wide
    assert "min-height: var(--vl-fit-need, 0px);" in wide
    # 바닥이 모자라도 편집 칸은 숨지 않는다 — 인스펙터는 스크롤한다.
    assert "overflow: hidden auto; max-height: none; display: flex; flex-direction: column;" in css
    assert "overflow: hidden; max-height: none; display: flex" not in css
    # 덱 머리 한 줄 — 도구 묶음을 풀어(display: contents) 요약 양옆에 세운다(누름 배선 그대로).
    assert f'#visual-stage[data-vl-fit="on"] #{window_js.DECK_ID} > .vl-deck-tools' in wide
    assert "display: contents;" in wide and "#vl-timeline-summary" in wide
    # 자막 칸이 인스펙터에 있으면 숨긴 승인 범위의 빈 틀(테두리 · 간격 8px)까지 접는다.
    assert "\n  .form:has(> #vl-approval-mode) { display: none; }" in css
    # 「자막」 작업공간의 레인 — 맞춤이면 남은 높이를 받되 토큰 높이 아래로 줄지 않는다.
    assert '[data-vl-workspace="captions"] .tl-root.is-fit .tl-lane { height: 100%;' in css
    assert "min-height: var(--vl-caption-lane-h); }" in css


def test_a_picked_clip_gives_the_viewer_the_whole_column_and_edits_tiers_in_the_inspector() -> None:
    """D191(사용자 지시 2026-11-28 「클립 선택시 가운데 창에 너무 많은 내용들이 떠서 영상이 안보여 —
    오른쪽 인스펙터 창에 추천을 수정할 수 있는 UI가 뜰 수 있도록」) — 맞춤 · 원본 뷰어(클립을 고른
    때)면 영상이 가운데 열 위 덱 전체를 받고, 원본 구간(등급 막대 · 표시 줄)과 내보낼 부분은
    오른쪽 인스펙터 「분석」 탭 아래에 선다. 「공유」 탭이면 인스펙터가 오른쪽 열 전체다."""
    css = window_js.window_css()
    wide = css[css.index("@media (min-width: 1051px)") :]
    source = '#visual-stage[data-vl-fit="on"]:not([data-vl-viewer="program"])'
    assert source + ":has(#vl-timeline-deck .tl-root:not(.tl-empty))" in wide
    assert '"thumbs player quote" "thumbs player timeline" "thumbs player ranges"\n' in wide
    # 원본 구간은 제 높이를 다 받는다(숨기지 않는다, D188) — 늘고 주는 줄은 내보낼 부분이고
    # 맞춤 바닥 계산이 그 줄과 바닥을 안다.
    assert "--vl-upper-flex-row: 2; --vl-upper-flex-min: var(--vl-ranges-min);" in wide
    assert "grid-template-rows: max-content max-content\n" in wide
    from ui import design_tokens

    assert design_tokens.SCALAR_TOKENS["ranges-min"] == "64px"
    assert source + " #vl-ranges { max-height: none; }" in wide
    share = ':has(#vl-quote[data-vl-inspector="share"])'
    assert '"thumbs player quote" "thumbs player quote" "thumbs player quote"' in wide
    assert source + share + " #visual-timeline" in wide
    assert source + share + " #vl-ranges" in wide
    # 타임라인 뷰어의 모양은 그대로다(원본 레일 · 추천 구간을 접는다).
    assert '"thumbs player quote" "thumbs timeline ranges" "deck deck deck"' in wide
