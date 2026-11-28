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

// 4b. 끄는 동안 작업대가 다시 묶여도(분석 진행 · 다른 곳의 편집) 끌기는 이어지고 한 번 보낸다.
const rewire = envOf([]);
const rl = (t) => (rewire.rail._listeners[t] || []).slice();
rl('pointerdown').forEach((fn) => fn({ target: target({ 'data-vl-tier-edge': '0' }),
  preventDefault() {} }));
rl('pointermove').forEach((fn) => fn({ clientX: 190 }));
const wireBefore = windowScope.__vlVisualWorkbenchState.wire;
rewire.rewired();
tierOut.rewire = { rewired: windowScope.__vlVisualWorkbenchState.wire !== wireBefore };
rl('pointermove').forEach((fn) => fn({ clientX: 160 }));
rl('pointerup').forEach((fn) => fn({ clientX: 160 }));
tierOut.rewire.calls = applied(rewire);

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
// 맞닿은 자동 추천 [2, 3]과는 한 줄기 [1, 3]이다(D190 — 추천 끝을 끌어 늘린 결과가 이것이다).
const marked = envOf([[1, 'recommended']]);
const labels = (html) => (html.match(/aria-label="(추천|보통|비추천)[^"]*"/g) || [])
  .map((s) => s.split('"')[1].split(' · ')[0]);
tierOut.marked = { bars: bars(marked.rail.innerHTML), edges: edges(marked.rail.innerHTML),
  labels: labels(marked.rail.innerHTML),
  glyph: /class="vl-recommendation[^"]* human[^"]*"[^>]*>[^<]*✎/.test(marked.rail.innerHTML) };
// 6b. 같은 등급 자동끼리 맞닿은 이음매에는 손잡이가 없다(옮겨도 바뀌는 것이 없다).
const twins = visualEnv({ vl_apply: refuse }, (() => { const m = tierModel([]);
  m.sources.srcA.suggestions[0].tier = 'green'; m.sources.srcA.suggestions[0].rec = 'recommended';
  return m; })());
twins.vid.video.offsetParent = {};
tierOut.twins = { bars: bars(twins.rail.innerHTML).length, edges: edges(twins.rail.innerHTML) };
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
// 막대 뒤에 그린다 — 보통 [0, 1] · 사람 추천과 맞닿은 자동 추천의 한 줄기 [1, 3]).
const human = envOf([[1, 'recommended']]);
railClick(human, { 'data-vl-tierbar': '1' });
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

// ── D190 — 1·2·3 · 막대 안 끌기 · ⌘Z ───────────────────────────────────────
// 전역 키는 문서 청취자가 있는 문서로 한 번 묶는다(D50 — 시작·끝 표시 시험과 같은 차림).
const keyReg = {};
const keyListeners = {};
const keyDoc = { getElementById(id) { return keyReg[id] || null; },
  addEventListener(t, fn) { (keyListeners[t] = keyListeners[t] || []).push(fn); },
  createElement() { return el({}); } };
const keyEl = el({ id: 'visual-workbench-block' });
keyEl._doc = keyDoc;
runEntrypoint(bootstraps.visual, keyEl, { value: '' }, {});
const st = () => windowScope.__vlVisualWorkbenchState;
const nowhere = { closest() { return null; } };
const inPlayer = { closest(sel) { return sel.includes('#visual-player') ? this : null; } };
const typing = { closest(sel) { return sel.includes('textarea') ? this : null; } };
const owned = { closest(sel) { return sel.includes('data-vl-own-keys') ? this : null; } };
function key(code, extra) {
  const ev = Object.assign({ code, key: code, target: nowhere, isComposing: false, keyCode: 0,
    repeat: false, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
    defaultPrevented: false, prevented: 0 }, extra || {});
  ev.preventDefault = function () { this.prevented += 1; this.defaultPrevented = true; };
  (keyListeners.keydown || []).forEach((fn) => fn(ev));
  return ev;
}
const pressAt = (target) => (keyListeners.pointerdown || []).forEach((fn) => fn({ target }));
const menuOpen = (env) => /class="vl-tier-menu"/.test(env.rail.innerHTML);
const clickId = (env, id) => (env.registry[id]._listeners.click || []).slice()
  .forEach((fn) => fn({}));

// 10. 막대를 고른 채 1·2·3 — 그 막대 범위를 바꾸고 고름은 남는다(다시 눌러 바꾼다).
const keyed = envOf([]);
Object.assign(keyReg, keyed.registry);
railClick(keyed, { 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' });
tierOut.keys = { buttons: ['recommended', 'normal', 'not_recommended']
  .map((m) => keyed.registry['vl-mark-' + m].disabled),
  readout: keyed.registry['vl-range-readout'].textContent };
const k1 = key('Digit1');
tierOut.keys.first = { prevented: k1.prevented, open: menuOpen(keyed) };
key('Numpad3');
clickId(keyed, 'vl-mark-normal');
tierOut.keys.calls = applied(keyed);
const esc1 = key('Escape');
const esc2 = key('Escape');
tierOut.keys.esc = { first: esc1.prevented, second: esc2.prevented, open: menuOpen(keyed) };
// 닫은 뒤의 숫자는 예전처럼 찍은 범위 몫이다 — 범위가 없으면 보내지 않는다.
key('Digit2');
tierOut.keys.afterClose = applied(keyed).length;
// I·O로 시작·끝을 찍으면 막대 고름은 닫힌다(숫자는 찍은 범위로 간다).
railClick(keyed, { 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' });
key('KeyI');
tierOut.keys.inCloses = !menuOpen(keyed);

// 11. 막대 안을 끌면 그 구간을 고른다 — 놓으면 고르기 칸이 뜨고 아직 보내지 않는다.
const sweep = envOf([]);
// 레일이 포인터를 붙잡으면 뒤의 click이 레일로 간다 — 누름(막대 고르기)이 될 수 있는 동안은
// 붙잡지 않는다.
const captured = [];
sweep.rail.setPointerCapture = (id) => captured.push(id);
const sweepDown = (x) => (sweep.rail._listeners.pointerdown || []).slice().forEach((fn) =>
  fn({ target: target({ 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' }),
    clientX: x, pointerId: 7, preventDefault() {} }));
const sweepMove = (x) => (sweep.rail._listeners.pointermove || []).slice().forEach((fn) =>
  fn({ clientX: x }));
const sweepUp = (x) => (sweep.rail._listeners.pointerup || []).slice().forEach((fn) =>
  fn({ clientX: x }));
sweepDown(60); sweepMove(62);
tierOut.sweep = { jitter: /class="vl-tier-span/.test(sweep.rail.innerHTML),
  heldOnPress: captured.length };
sweepMove(120);
tierOut.sweep.heldOnDrag = captured.slice();
tierOut.sweep.band = /class="vl-tier-span/.test(sweep.rail.innerHTML);
sweepUp(120);
railClick(sweep, { 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' });
tierOut.sweep.open = menuOpen(sweep);
tierOut.sweep.before = applied(sweep).length;
tierOut.sweep.seek = sweep.vid.state.currentTime;
st().rangeKey('recommended');
// 두 막대에 걸쳐(1.6초 → 2.6초) — 눌린 등급이 없다.
sweepDown(160); sweepMove(260); sweepUp(260);
railClick(sweep, { 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' });
tierOut.sweep.mixedPressed = /aria-pressed="true"/.test(sweep.rail.innerHTML);
st().rangeKey('normal');
// 거꾸로(1.2초 → 0.4초)도 같은 구간이다.
sweepDown(120); sweepMove(40); sweepUp(40);
railClick(sweep, { 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' });
st().rangeKey('not_recommended');
// 거의 움직이지 않은 누름은 예전처럼 막대 전체를 고른다.
sweepDown(50); sweepMove(52); sweepUp(52);
railClick(sweep, { 'data-vl-recommendation': '0', 'data-vl-tierbar': '0' });
st().rangeKey('normal');
tierOut.sweep.calls = applied(sweep);

// 12. ⌘Z · Ctrl+Z — 원본 구간(재생기)을 마지막으로 누른 뒤에만 「되돌리기」 한 걸음이다.
const undo = envOf([]);
Object.assign(keyReg, undo.registry);
const undos = () => applied(undo).filter((c) => c.op === 'undo').length;
const before = key('KeyZ', { metaKey: true });
tierOut.undo = { untouched: { prevented: before.prevented, calls: undos() } };
pressAt(inPlayer);
const meta = key('KeyZ', { metaKey: true });
const ctrl = key('KeyZ', { ctrlKey: true });
tierOut.undo.sent = { prevented: meta.prevented + ctrl.prevented, calls: undos(),
  payload: applied(undo).filter((c) => c.op === 'undo')[0] };
const redo = key('KeyZ', { metaKey: true, shiftKey: true });
const inText = key('KeyZ', { metaKey: true, target: typing });
const inOwned = key('KeyZ', { metaKey: true, target: owned });
const plainZ = key('KeyZ');
tierOut.undo.skipped = { prevented: redo.prevented + inText.prevented + inOwned.prevented +
  plainZ.prevented, calls: undos() };
pressAt(nowhere);
const away = key('KeyZ', { metaKey: true });
tierOut.undo.away = { prevented: away.prevented, calls: undos() };
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
    # 끄는 사이 다시 묶여도 끌기는 이어진다 — 놓을 때 지금 배선이 한 번 보낸다.
    assert tier_report["rewire"] == {
        "rewired": True,
        "calls": [_range("recommended", "8/5", "2")],
    }
    # 이웃이 없는 바깥 끝을 안으로 끌면 비운 띠의 표시를 거둔다(보통을 새로 달지 않는다).
    lone = tier_report["lone"]
    assert lone["edges"] == 2 and lone["preview"] is True
    assert lone["calls"] == [_range("clear", "1", "37/25")]


@needs_node
def test_a_human_mark_wins_over_the_auto_tier_on_the_bar(tier_report: dict[str, Any]) -> None:
    marked = tier_report["marked"]
    # 자동 막대는 예전처럼 하나씩(사람 표시가 덮은 [1, 2]만 잘라 낸다) · 사람 막대는 맞닿은 같은
    # 등급 자동 막대와 한 줄기로 뒤에 그린다(D190 — 추천 끝을 끌어 늘린 결과가 한 막대로 길어진다).
    assert [bar.split()[1] for bar in marked["bars"]] == ["yellow", "green"]
    assert ["human" in bar for bar in marked["bars"]] == [False, True]
    assert marked["labels"] == ["보통 0.0초~1.0초", "추천 1.0초~3.0초"]
    assert marked["glyph"] is True  # 사람 표시는 글자 표지로도 보인다
    assert marked["edges"] == 1  # 보통 | 추천 사이 하나 — 추천 안의 이음매에는 없다
    assert tier_report["twins"] == {"bars": 2, "edges": 0}
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
    assert tier_report["cleared"] == [_range("clear", "1", "3")]  # 고른 한 줄기 전체
    assert tier_report["closed"] == {"open": False, "calls": 1}


@needs_node
def test_the_result_view_offers_no_tier_editing(tier_report: dict[str, Any]) -> None:
    assert tier_report["result"] == {"edges": 0, "menu": False}


@needs_node
def test_number_keys_change_the_picked_bar_and_keep_it_picked(
    tier_report: dict[str, Any],
) -> None:
    keys = tier_report["keys"]
    # 막대를 고르면 아래 표시 단추가 켜지고 안내가 그 구간을 말한다.
    assert keys["buttons"] == [False, False, False]
    assert "고른 구간" in keys["readout"] and "0.00초 ~ 2.00초" in keys["readout"]
    assert keys["first"] == {"prevented": 1, "open": True}  # 고름은 남는다 — 다시 눌러 바꾼다
    # 1 · 숫자판 3 · 「보통」 단추 — 모두 그 막대 범위 하나에 간다.
    assert keys["calls"] == [
        _range("recommended", "0", "2"),
        _range("not_recommended", "0", "2"),
        _range("normal", "0", "2"),
    ]
    # Esc는 고름을 닫고(막음), 닫을 것이 없으면 막지 않는다.
    assert keys["esc"] == {"first": 1, "second": 0, "open": False}
    assert keys["afterClose"] == 3  # 닫은 뒤의 숫자는 찍은 범위 몫 — 범위가 없어 보내지 않는다
    assert keys["inCloses"] is True


@needs_node
def test_dragging_inside_a_bar_picks_that_span_for_the_number_keys(
    tier_report: dict[str, Any],
) -> None:
    sweep = tier_report["sweep"]
    assert sweep["jitter"] is False  # 몇 px 흔들림은 누름이다
    # 끌기가 된 뒤에만 레일이 포인터를 붙잡는다(누름의 click은 막대에 간다).
    assert sweep["heldOnPress"] == 0 and sweep["heldOnDrag"] == [7]
    assert sweep["band"] is True and sweep["open"] is True
    assert sweep["before"] == 0  # 끌기만으로는 보내지 않는다 — 등급을 골라야 한다
    assert sweep["seek"] == 0  # 끌기 뒤의 click은 재생 위치를 옮기지 않는다
    assert sweep["mixedPressed"] is False
    assert sweep["calls"] == [
        _range("recommended", "3/5", "6/5"),
        _range("normal", "8/5", "13/5"),
        _range("not_recommended", "2/5", "6/5"),
        _range("normal", "0", "2"),
    ]


@needs_node
def test_cmd_z_undoes_one_step_only_after_touching_the_player(
    tier_report: dict[str, Any],
) -> None:
    undo = tier_report["undo"]
    assert undo["untouched"] == {"prevented": 0, "calls": 0}
    assert undo["sent"]["prevented"] == 2 and undo["sent"]["calls"] == 2  # ⌘Z · Ctrl+Z
    assert undo["sent"]["payload"]["op"] == "undo"
    assert undo["sent"]["payload"]["sourceId"] == "srcA"
    # ⌘⇧Z · 글 칸 · 제 키를 가진 칸(타임라인 재생기) · 수식키 없는 Z는 건드리지 않는다.
    assert undo["skipped"] == {"prevented": 0, "calls": 2}
    # 다른 곳을 누른 뒤에는 원본 구간을 되돌리지 않는다.
    assert undo["away"] == {"prevented": 0, "calls": 2}


def test_the_keysheet_and_undo_button_name_cmd_z() -> None:
    sheet = ui_facade.keysheet_html()
    assert "⌘Z" in sheet or "Ctrl+Z" in sheet
    shell = ui_facade.visual_player_shell_html("/media/x", "안내")
    undo = shell[shell.index('id="vl-range-undo"') :]
    undo = undo[: undo.index("</button>")]
    # 작업대 그림은 운영체제마다 같은 바이트다 — 단추는 둘을 함께 적는다.
    assert 'aria-keyshortcuts="Meta+Z Control+Z"' in undo
    assert "⌘Z · Ctrl+Z" in undo


def test_the_rail_drag_threshold_matches_the_client() -> None:
    """막대 안 끌기의 문턱은 파이썬 설정 값 그대로 실행본에 있다(숫자는 한 곳에서 온다)."""
    px = ui_facade.RAIL_DRAG_PX
    assert f"var VL_RAIL_DRAG_PX = {px};" in ui_facade.VISUAL_BOOTSTRAP_JS
