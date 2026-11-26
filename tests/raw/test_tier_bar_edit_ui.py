"""등급 막대 직접 고치기(D189) — 원본 구간 레일의 추천·보통·비추천 막대.

막대는 실제로 내보낼 등급이다: 사람 표시(범위 표시, D100 §4)가 자동 등급을 이긴다(렌더 계획
`_judge`와 같은 차례). 막대 끝을 끌면 옮긴 띠를 커진 쪽 등급으로, 막대를 누르면 그 범위를 고른
등급으로 **이미 있는 `range` 편집 하나**에 싣는다 — 새 저장·새 서버 동작은 없다. 실행본은 node에서
실제로 돈다(레일 너비 300 = 3초 → 100 px/초, 25fps).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, needs_node  # noqa: E402
from ui import ui_facade  # noqa: E402

_TIER_SCRIPT = r"""
function tierModel(marks, mode) {
  const m = visualModel();
  m.sources.srcA.suggestions = [
    { start: '0', end: '2', tier: 'yellow', hold: false, rec: 'normal', reason: '보통' },
    { start: '2', end: '3', tier: 'green', hold: false, rec: 'recommended', reason: '추천' },
  ];
  (marks || []).forEach((pair) => { m.sources.srcA.rail.intervals[pair[0]].mark = pair[1]; });
  return m;
}
const tierOut = { case: 'tier-edit' };
const refuse = () => ({ ok: false, error: '시험 — 저장하지 않는다' });
const target = (attrs) => ({ getAttribute: (k) => (k in attrs ? attrs[k] : null) });
function envOf(marks, sourceClock) {
  const model = tierModel(marks);
  if (sourceClock) {
    model.sources.srcA.rail.intervals.forEach((it) => {
      it.sourceStart = it.start; it.sourceEnd = it.end; });
  }
  const env = visualEnv({ vl_apply: refuse }, model);
  env.vid.video.offsetParent = {};
  return env;
}
const bars = (html) => (html.match(/<button type="button" class="vl-recommendation [^"]*"/g) || [])
  .map((tag) => tag.replace('<button type="button" class="vl-recommendation ', '')
    .replace('"', ''));
const edges = (html) => (html.match(/data-vl-tier-edge="\d+"/g) || []).length;
const applied = (env) => env.serverCalls.filter((c) => c.name === 'vl_apply')
  .map((c) => ({ op: c.payload.op, mark: c.payload.mark, sourceId: c.payload.sourceId,
    startSec: c.payload.startSec, endSec: c.payload.endSec }));
function drag(env, edge, xs) {
  const listeners = (t) => (env.rail._listeners[t] || []).slice();
  listeners('pointerdown').forEach((fn) => fn({ target: target({ 'data-vl-tier-edge': edge }),
    preventDefault() {} }));
  xs.forEach((x) => listeners('pointermove').forEach((fn) => fn({ clientX: x })));
  const during = env.rail.innerHTML;
  listeners('pointerup').forEach((fn) => fn({ clientX: xs[xs.length - 1] }));
  listeners('click').forEach((fn) => fn({ target: env.rail, clientX: xs[xs.length - 1] }));
  return during;
}
const railClick = (env, attrs) => (env.rail._listeners.click || []).slice()
  .forEach((fn) => fn({ target: target(attrs) }));

// 1. 자동 등급만 — 막대 둘, 끝 손잡이는 맞닿은 안쪽 경계 하나(원본 처음·끝은 끌 것이 없다).
const plain = envOf([]);
tierOut.plain = { bars: bars(plain.rail.innerHTML), edges: edges(plain.rail.innerHTML) };

// 2. 추천의 왼쪽 끝을 1.6초로 — 커진 추천이 [1.6, 2)를 덮는다. 끄는 동안은 미리 그리기만.
const grow = envOf([]);
const during = drag(grow, '0', [180, 160]);
tierOut.grow = { preview: /class="vl-tier-drag green"/.test(during), calls: applied(grow),
  seekAfter: grow.vid.state.currentTime };

// 3. 같은 끝을 오른쪽(2.25초 → 2.24초 프레임)으로 — 커진 보통이 [2, 2.24)를 덮는다.
const shrink = envOf([]);
drag(shrink, '0', [225]);
tierOut.shrink = applied(shrink);

// 4. 맨 왼쪽까지 끌어도 보통은 한 프레임 남는다(막대가 사라지지 않는다).
const clamp = envOf([]);
drag(clamp, '0', [0]);
tierOut.clamp = applied(clamp);

// 5. 움직이지 않고 놓으면 아무것도 보내지 않는다.
const still = envOf([]);
drag(still, '0', [200]);
tierOut.still = applied(still).length;

// 5b. 이웃 막대가 없는 바깥 끝을 안으로 — 비운 띠에는 표시를 남기지 않는다(「자동으로」).
const loneEnv = visualEnv({ vl_apply: refuse }, (() => { const m = tierModel([[1, 'recommended']]);
  m.sources.srcA.suggestions = []; return m; })());
loneEnv.vid.video.offsetParent = {};
tierOut.lone = { edges: edges(loneEnv.rail.innerHTML) };
const loneDuring = drag(loneEnv, '0', [150]);
tierOut.lone.preview = /class="vl-tier-drag clear"/.test(loneDuring);
tierOut.lone.calls = applied(loneEnv);

// 6. 사람 표시가 자동 등급을 이긴다 — segA2([1, 2])를 추천으로 표시했으면 막대가 그 등급이다.
const marked = envOf([[1, 'recommended']]);
tierOut.marked = { bars: bars(marked.rail.innerHTML), edges: edges(marked.rail.innerHTML),
  glyph: /class="vl-recommendation[^"]* human[^"]*"[^>]*>[^<]*✎/.test(marked.rail.innerHTML) };
const redMarked = envOf([[2, 'not_recommended']]);
tierOut.redMarked = bars(redMarked.rail.innerHTML);

// 7. 막대를 누르면 등급 고르기가 뜨고 — 고르면 그 막대 범위를 그 등급으로 표시한다.
const pick = envOf([]);
railClick(pick, { 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' });
const menuHtml = pick.rail.innerHTML;
tierOut.menu = { open: /class="vl-tier-menu"/.test(menuHtml),
  picks: (menuHtml.match(/data-vl-tier-pick="([a-z_]+)"/g) || [])
    .map((s) => s.split('"')[1]),
  pressed: /data-vl-tier-pick="normal" aria-pressed="true"/.test(menuHtml),
  seek: pick.vid.state.currentTime };
railClick(pick, { 'data-vl-tier-pick': 'not_recommended' });
tierOut.picked = { calls: applied(pick),
  closed: !/class="vl-tier-menu"/.test(pick.rail.innerHTML) };

// 8. 사람 표시 막대만 「자동으로」가 있다 — 누르면 그 범위의 표시를 거둔다(사람 막대는 자동
// 막대 뒤에 그린다 — 보통 [0, 1] · 추천 [2, 3] · 사람 추천 [1, 2]).
const human = envOf([[1, 'recommended']]);
railClick(human, { 'data-vl-tierbar': '2' });
tierOut.humanMenu = (human.rail.innerHTML.match(/data-vl-tier-pick="([a-z_]+)"/g) || [])
  .map((s) => s.split('"')[1]);
railClick(human, { 'data-vl-tier-pick': 'clear' });
tierOut.cleared = applied(human);
railClick(human, { 'data-vl-tierbar': '0' });
railClick(human, { 'data-vl-tier-pick': 'close' });
tierOut.closed = { open: /class="vl-tier-menu"/.test(human.rail.innerHTML),
  calls: applied(human).length };

// 9. 정리 결과 보기는 원본 시계가 아니다 — 손잡이도 고르기도 없다.
const result = envOf([], true);
windowScope.__vlVisualWorkbenchState.timelineMode = 'result';
railClick(result, { 'data-vl-recommendation': '1', 'data-vl-tierbar': '1' });
tierOut.result = { edges: edges(result.rail.innerHTML),
  menu: /class="vl-tier-menu"/.test(result.rail.innerHTML) };
windowScope.__vlVisualWorkbenchState.timelineMode = 'source';
checkpoints.push(tierOut);
"""


def _run_tier_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "tier.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _TIER_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"visual": ui_facade.VISUAL_BOOTSTRAP_JS}), encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2500:]
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "tier-edit")


@pytest.fixture(scope="module")
def tier_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    if shutil.which("node") is None:
        pytest.skip("node가 없는 환경 — JS 실행 시험은 건너뛴다")
    return _run_tier_harness(tmp_path_factory.mktemp("tier"))


def _range(mark: str, start: str, end: str) -> dict[str, str]:
    return {"op": "range", "mark": mark, "sourceId": "srcA", "startSec": start, "endSec": end}


@needs_node
def test_the_bars_are_the_auto_tiers_with_one_handle_between_them(
    tier_report: dict[str, Any],
) -> None:
    assert tier_report["plain"]["bars"][0].startswith("normal yellow")
    assert tier_report["plain"]["bars"][1].startswith("recommended green")
    assert tier_report["plain"]["edges"] == 1  # 원본 처음·끝에는 손잡이가 없다


@needs_node
def test_dragging_an_edge_marks_the_moved_strip_with_the_tier_that_grew(
    tier_report: dict[str, Any],
) -> None:
    grow = tier_report["grow"]
    assert grow["preview"] is True  # 끄는 동안은 그리기만 — 놓을 때 한 번 보낸다
    assert grow["calls"] == [_range("recommended", "8/5", "2")]
    assert grow["seekAfter"] == 0  # 끌기 뒤의 click은 재생 위치를 옮기지 않는다
    assert tier_report["shrink"] == [_range("normal", "2", "56/25")]
    # 이웃 막대는 한 프레임(1/25초) 남는다.
    assert tier_report["clamp"] == [_range("recommended", "1/25", "2")]
    assert tier_report["still"] == 0
    # 이웃이 없는 바깥 끝을 안으로 끌면 비운 띠의 표시를 거둔다(보통을 새로 달지 않는다).
    lone = tier_report["lone"]
    assert lone["edges"] == 2 and lone["preview"] is True
    assert lone["calls"] == [_range("clear", "1", "37/25")]


@needs_node
def test_a_human_mark_wins_over_the_auto_tier_on_the_bar(tier_report: dict[str, Any]) -> None:
    marked = tier_report["marked"]
    # 자동 막대는 예전처럼 하나씩(사람 표시가 덮은 [1, 2]만 잘라 낸다) · 사람 막대는 뒤에 그린다.
    assert [bar.split()[1] for bar in marked["bars"]] == ["yellow", "green", "green"]
    assert ["human" in bar for bar in marked["bars"]] == [False, False, True]
    assert marked["glyph"] is True  # 사람 표시는 글자 표지로도 보인다
    assert marked["edges"] == 2
    assert [bar.split()[1] for bar in tier_report["redMarked"]] == ["yellow", "red"]


@needs_node
def test_clicking_a_bar_offers_the_three_tiers_and_marks_its_range(
    tier_report: dict[str, Any],
) -> None:
    menu = tier_report["menu"]
    assert menu["open"] is True
    assert menu["picks"] == ["recommended", "normal", "not_recommended", "close"]
    assert menu["pressed"] is True  # 지금 등급이 눌려 보인다
    assert menu["seek"] == 0  # 예전처럼 그 막대 시작으로 간다
    assert tier_report["picked"]["calls"] == [_range("not_recommended", "0", "2")]
    assert tier_report["picked"]["closed"] is True


@needs_node
def test_only_a_human_bar_offers_auto_and_close_sends_nothing(tier_report: dict[str, Any]) -> None:
    menu = ["recommended", "normal", "not_recommended", "clear", "close"]
    assert tier_report["humanMenu"] == menu
    assert tier_report["cleared"] == [_range("clear", "1", "2")]
    assert tier_report["closed"] == {"open": False, "calls": 1}


@needs_node
def test_the_result_view_offers_no_tier_editing(tier_report: dict[str, Any]) -> None:
    assert tier_report["result"] == {"edges": 0, "menu": False}
