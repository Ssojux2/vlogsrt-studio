"""재생기 시작·끝 표시 · 네 영역 화면 · 가벼운 폴링(W-46b, D100 §7b).

세 층이다 — 실행본(node에서 실제로 돈다): In·Out 상태 · 바꾸기 · 끌기 묶기 · Esc ·
입력 칸에서 막힘 ·
표시 단추의 요청 값(정확 유리수 글자) · 「내보낼 부분」 띠와 목록. 실제 백엔드(합성 ffmpeg 원본):
`range` 동작 = 리비전 하나 · 모델의 `kept`가 바뀜 · 낡음 · 되돌리기 · 거절. 화면 선언: 네 영역 ·
「고급 도구」 · 견적·승인 칸은 그대로 보임(D44). 유료 호출 0회.
"""

from __future__ import annotations

import json
import re
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

from tests.raw.test_visual_workbench_ui import (  # noqa: E402
    _NODE_HARNESS,
    _model_of,
    _new_server,
    _prepare_review,
    needs_node,
)
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.segment_review import RANGE_MARKS  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


# ── 1. 실행본 — node에서 실제 실행 ───────────────────────────────────────────

_RANGE_SCRIPT = r"""
// ── 시작·끝 표시(W-46b) — 전역 키는 문서 청취자가 있는 문서로 한 번 묶는다(D50).
const keyReg = {};
const keyListeners = {};
const keyDoc = { getElementById(id) { return keyReg[id] || null; },
  addEventListener(t, fn) { (keyListeners[t] = keyListeners[t] || []).push(fn); },
  createElement() { return el({}); } };
const keyEl = el({ id: 'visual-workbench-block' });
keyEl._doc = keyDoc;
runEntrypoint(bootstraps.visual, keyEl, { value: '' }, {});
const body = { closest() { return null; } };
const textarea = { closest(sel) { return sel.includes('textarea') ? this : null; } };
function key(code, extra) {
  const ev = Object.assign({ code, key: code, target: body, isComposing: false, keyCode: 0,
    repeat: false, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
    defaultPrevented: false, prevented: 0 }, extra || {});
  ev.preventDefault = function () { this.prevented += 1; this.defaultPrevented = true; };
  (keyListeners.keydown || []).forEach((fn) => fn(ev));
  return ev;
}
const replies = [];
function rangeModel() {
  const m = visualModel();
  m.sources.srcA.kept = [{ start: '0', end: '1', human: false },
    { start: '2', end: '5/2', human: true }];
  m.sources.srcA.rail.intervals[1].mark = 'recommended';
  m.sources.srcA.rail.intervals[2].mark = 'not_recommended';
  return m;
}
const envR = visualEnv({ vl_apply: (p) => replies.shift() }, rangeModel());
Object.assign(keyReg, envR.registry);
envR.vid.video.offsetParent = {};
const R = envR.registry;
const st = () => windowScope.__vlVisualWorkbenchState;
const rangeOf = () => { const r = st().range; return r && { sourceId: r.sourceId,
  inSec: r.inSec && VL.str(r.inSec), outSec: r.outSec && VL.str(r.outSec) }; };
const marksDisabled = () => ['recommended', 'normal', 'not_recommended', 'clear']
  .map((m) => R['vl-mark-' + m].disabled);
const click = (id) => (R[id]._listeners.click || []).slice().forEach((fn) => fn({}));
const out = { case: 'range' };
out.start = { readout: R['vl-range-readout'].textContent, disabled: marksDisabled() };
// In(1.23초 → 31/25) · Out(0.6초 → 3/5) — 시작이 끝 뒤라 둘을 바꾼다.
envR.vid.state.currentTime = 1.23; click('vl-mark-in');
out.afterIn = { range: rangeOf(), readout: R['vl-range-readout'].textContent,
  disabled: marksDisabled() };
envR.vid.state.currentTime = 0.6; click('vl-mark-out');
out.swapped = { range: rangeOf(), disabled: marksDisabled(),
  readout: R['vl-range-readout'].textContent };
const railHtml = envR.rail.innerHTML;
out.rail = { band: railHtml.includes('class="vl-range-band"'),
  handles: (railHtml.match(/data-vl-range-handle="(in|out)"/g) || []).length,
  strip: railHtml.includes('class="vl-kept-strip"'),
  kept: (railHtml.match(/<span class="vl-kept( human)?" style="[^"]*"/g) || []),
  human: (railHtml.match(/vl-human-mark (green|yellow|red)/g) || []) };
// 끌기 — 끝 손잡이를 시작 앞(0.1초)으로 끌면 시작 + 한 프레임에서 멈추고, 레일 밖으로 끌면 원본 끝.
const handle = (side) => ({ getAttribute: (k) => (k === 'data-vl-range-handle' ? side : null) });
const down = (side) => envR.rail._listeners.pointerdown.slice().forEach((fn) =>
  fn({ target: handle(side), preventDefault() {} }));
const move = (x) => (envR.rail._listeners.pointermove || []).slice().forEach((fn) =>
  fn({ clientX: x }));
const up = () => (envR.rail._listeners.pointerup || []).slice().forEach((fn) => fn({}));
const boundaryCalls = () => envR.serverCalls.filter((c) => c.name === 'vl_propose').length;
down('out'); move(10); out.dragClampLow = rangeOf();
move(900); out.dragClampEnd = rangeOf(); up();
down('in'); move(-50); out.dragClampZero = rangeOf(); up();
move(150); out.afterUp = rangeOf();
out.proposeCalls = boundaryCalls();
// 키 — I·O는 입력 칸에서 막히고, 1은 추천을 보낸다(정확 유리수 글자).
envR.vid.state.currentTime = 2.4;
const typed = key('KeyO', { target: textarea });
out.typed = { range: rangeOf(), prevented: typed.prevented };
const modified = key('KeyI', { metaKey: true });
out.modified = modified.prevented;
// 제 키를 가진 칸(아래 덱의 타임라인 재생기) 안의 키는 위 재생기에 닿지 않는다(W-85, D145).
const owned = { closest(sel) { return sel.includes('data-vl-own-keys') ? this : null; } };
const ownedKey = key('KeyO', { target: owned });
const ownedPlay = key('KeyL', { target: owned });
out.owned = { prevented: ownedKey.prevented + ownedPlay.prevented, range: rangeOf(),
  time: envR.vid.state.currentTime };
const oKey = key('KeyO');
out.oKey = { range: rangeOf(), prevented: oKey.prevented };
replies.push({ ok: true, echoEpoch: 'epoch-1', message: '추천으로 표시했습니다',
  rangeStale: false });
const one = key('Digit1');
await new Promise((resolve) => setTimeout(resolve, 0));
const sent = envR.serverCalls.filter((c) => c.name === 'vl_apply');
out.markKey = { prevented: one.prevented, payload: sent[0] && sent[0].payload,
  cleared: st().range === null };
// 낡음 — 화면의 시작·끝은 그대로(다시 누르면 된다), 각주가 까닭을 말한다.
envR.vid.state.currentTime = 1; click('vl-mark-in');
envR.vid.state.currentTime = 2; click('vl-mark-out');
replies.push({ ok: true, echoEpoch: 'epoch-1', rangeStale: true,
  message: '다른 곳에서 바뀌어 새로 불러왔습니다 — 다시 표시해 주세요' });
click('vl-mark-not_recommended');
await new Promise((resolve) => setTimeout(resolve, 0));
const sent2 = envR.serverCalls.filter((c) => c.name === 'vl_apply');
out.stale = { mark: sent2[1].payload.mark, range: rangeOf(),
  footnote: R['vl-footnote'].textContent };
// 거절 — 원문 그대로 각주에, 범위는 그대로.
replies.push({ ok: false, error: '표시하지 못했습니다: 범위가 비었다' });
click('vl-mark-normal');
await new Promise((resolve) => setTimeout(resolve, 0));
out.refused = { footnote: R['vl-footnote'].textContent, range: rangeOf() };
// 되돌리기 — 같은 서버 함수의 `undo` 한 걸음.
replies.push({ ok: true, echoEpoch: 'epoch-1', message: '마지막 편집을 되돌렸습니다.' });
click('vl-range-undo');
await new Promise((resolve) => setTimeout(resolve, 0));
const sent3 = envR.serverCalls.filter((c) => c.name === 'vl_apply');
out.undo = sent3[sent3.length - 1].payload;
// Esc — 찍은 범위를 지운다(지울 것이 없으면 기본 동작을 막지 않는다).
const esc1 = key('Escape');
const esc2 = key('Escape');
out.esc = { first: esc1.prevented, second: esc2.prevented, range: st().range };
// 목록 — 내보낼 부분 두 곳, 누르면 그 시작으로 간다.
out.list = { html: R['vl-range-list'].innerHTML, sum: R['vl-ranges-sum'].textContent };
const row = { getAttribute: (k) => (k === 'data-vl-range-seek' ? '2' : null), parentNode: null };
(R['vl-range-list']._listeners.click || []).forEach((fn) => fn({ target: row }));
out.seek = envR.vid.state.currentTime;
// 「고급 도구」 — 무대에 표지를 달고 단추의 펼침 상태를 맞춘다.
click('vl-advanced-toggle');
out.advanced = { stage: R['visual-stage'].attrs['data-vl-advanced'] || null,
  expanded: R['vl-advanced-toggle'].attrs['aria-expanded'] };
click('vl-advanced-toggle');
out.collapsed = { stage: R['visual-stage'].attrs['data-vl-advanced'] || null,
  expanded: R['vl-advanced-toggle'].attrs['aria-expanded'] };
checkpoints.push(out);
"""


def _run_range_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "range.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _RANGE_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {"visual": ui_facade.VISUAL_BOOTSTRAP_JS, "caption": ui_facade.CAPTION_BOOTSTRAP_JS}
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2500:]
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "range")


@pytest.fixture(scope="module")
def range_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    if shutil.which("node") is None:
        pytest.skip("node가 없는 환경 — JS 실행 시험은 건너뛴다")
    return _run_range_harness(tmp_path_factory.mktemp("range"))


@needs_node
def test_in_and_out_snap_to_frames_and_swap_when_reversed(range_report: dict[str, Any]) -> None:
    report = range_report
    assert report["start"]["disabled"] == [True] * 4  # 시작·끝이 없으면 표시 단추가 꺼져 있다
    assert "In(I)" in report["start"]["readout"]
    # 1.23초 → 가장 가까운 프레임(25fps) 31/25초. 끝이 없으면 아직 꺼져 있다.
    assert report["afterIn"]["range"] == {"sourceId": "srcA", "inSec": "31/25", "outSec": None}
    assert report["afterIn"]["disabled"] == [True] * 4
    assert "시작 1.24초" in report["afterIn"]["readout"]
    # 끝(0.6초 = 3/5)이 시작보다 앞이다 — 거절하지 않고 둘을 바꾼다.
    assert report["swapped"]["range"] == {"sourceId": "srcA", "inSec": "3/5", "outSec": "31/25"}
    assert report["swapped"]["disabled"] == [False] * 4
    assert report["swapped"]["readout"].startswith("0.60초 ~ 1.24초")


@needs_node
def test_dragging_a_range_handle_moves_only_that_end_and_never_proposes_a_cut(
    range_report: dict[str, Any],
) -> None:
    report = range_report
    assert report["rail"]["band"] is True and report["rail"]["handles"] == 2
    # 끝 손잡이를 시작 앞으로 — 시작 + 한 프레임(3/5 + 1/25 = 16/25)에서 멈춘다.
    assert report["dragClampLow"] == {"sourceId": "srcA", "inSec": "3/5", "outSec": "16/25"}
    assert report["dragClampEnd"] == {"sourceId": "srcA", "inSec": "3/5", "outSec": "3"}  # 원본 끝
    assert report["dragClampZero"] == {"sourceId": "srcA", "inSec": "0", "outSec": "3"}  # 0에 묶임
    assert report["afterUp"] == report["dragClampZero"]  # 놓은 뒤의 움직임은 아무것도 안 바꾼다
    assert report["proposeCalls"] == 0  # 컷 경계를 제안하지 않는다(그 손잡이는 「고급 도구」 몫)


@needs_node
def test_range_keys_respect_inputs_and_modifiers_and_send_exact_fractions(
    range_report: dict[str, Any],
) -> None:
    report = range_report
    assert report["typed"] == {
        "range": {"sourceId": "srcA", "inSec": "0", "outSec": "3"},
        "prevented": 0,
    }
    assert report["modified"] == 0
    assert report["owned"] == {
        "prevented": 0,
        "range": {"sourceId": "srcA", "inSec": "0", "outSec": "3"},
        "time": 2.4,
    }
    assert report["oKey"]["prevented"] == 1
    assert report["oKey"]["range"] == {"sourceId": "srcA", "inSec": "0", "outSec": "12/5"}
    payload = report["markKey"]["payload"]
    assert report["markKey"]["prevented"] == 1 and report["markKey"]["cleared"] is True
    assert {k: payload[k] for k in ("op", "mark", "sourceId", "startSec", "endSec")} == {
        "op": "range",
        "mark": "recommended",
        "sourceId": "srcA",
        "startSec": "0",
        "endSec": "12/5",
    }
    assert payload["revisionId"] == "rev-1" and payload["basePlanId"] == "plan-1"
    assert payload["token"] == "tok-1" and payload["signature"] == "ws-sig"


@needs_node
def test_a_stale_mark_keeps_the_range_and_says_why(range_report: dict[str, Any]) -> None:
    report = range_report
    assert report["stale"]["mark"] == "not_recommended"
    assert report["stale"]["range"] == {"sourceId": "srcA", "inSec": "1", "outSec": "2"}
    assert report["stale"]["footnote"] == workspace_service.RANGE_STALE_TEXT
    assert report["refused"]["footnote"] == "표시하지 못했습니다: 범위가 비었다"
    assert report["refused"]["range"] == report["stale"]["range"]
    assert report["undo"]["op"] == "undo" and report["undo"]["revisionId"] == "rev-1"
    assert report["esc"] == {"first": 1, "second": 0, "range": None}


@needs_node
def test_the_kept_strip_and_list_draw_only_the_model_ranges(range_report: dict[str, Any]) -> None:
    report = range_report
    assert report["rail"]["strip"] is True
    assert report["rail"]["kept"] == [
        '<span class="vl-kept" style="left:0.000%;width:33.333%"',
        '<span class="vl-kept human" style="left:66.667%;width:16.667%"',
    ]
    # 사람 표시는 자동 등급 막대와 따로 그린다 — 추천은 초록 띠, 비추천은 빨강 띠.
    assert report["rail"]["human"] == ["vl-human-mark green", "vl-human-mark red"]
    listed = report["list"]["html"]
    assert listed.count('class="vl-range-row') == 2
    assert 'data-vl-range-seek="0"' in listed and 'data-vl-range-seek="2"' in listed
    assert "자동 추천" in listed and "사람 추천" in listed
    assert report["list"]["sum"] == "2곳 · 1.5초"
    assert report["seek"] == 2
    assert report["advanced"] == {"stage": "open", "expanded": "true"}
    assert report["collapsed"] == {"stage": None, "expanded": "false"}


# ── 2. 화면 선언 — 네 영역 · 「고급 도구」 · 견적·승인 칸 ─────────────────────────


def test_the_player_shell_carries_in_out_marks_undo_and_the_ranges_area() -> None:
    shell = ui_facade.visual_player_shell_html("/media/x", "안내")
    for anchor in (
        'id="vl-mark-in"',
        'id="vl-mark-out"',
        'id="vl-range-readout"',
        'id="vl-range-undo"',
        'id="vl-ranges"',
        'id="vl-range-list"',
        f'id="{ui_facade.ADVANCED_TOGGLE_ID}"',
        'aria-controls="vl-panel"',
    ):
        assert anchor in shell, anchor
    marks = [mark for mark, *_rest in ui_facade.RANGE_MARK_BUTTONS]
    assert tuple(marks) == RANGE_MARKS  # 표시 철자는 서비스 그대로다
    assert tuple(workspace_service.RANGE_MARK_WORDS) == RANGE_MARKS
    for mark in marks:
        assert f'id="vl-mark-{mark}"' in shell and f'data-vl-mark="{mark}" disabled' in shell
    for word in (">In <", ">Out <", ">추천 <", ">보통 <", ">비추천 <", ">표시 지우기<"):
        assert word in shell, word
    # 레일 → 표시 줄 → 추천 구간 칸 차례(재생기 껍데기 안 — 같은 원본을 다시 골라도 남는다).
    assert shell.index('id="visual-rail"') < shell.index('id="vl-markbar"')
    assert shell.index('id="vl-markbar"') < shell.index('id="vl-ranges"')
    sheet = ui_facade.keysheet_html()
    for keys in ('<kbd class="vl-kbd">I</kbd>', '<kbd class="vl-kbd">3</kbd>', "Esc"):
        assert keys in sheet, keys


def test_the_main_view_shows_four_areas_and_folds_the_rest_behind_advanced() -> None:
    css = ui_facade.WORKBENCH_CSS
    assert "#vl-ranges { grid-area: ranges;" in css
    # 패널 · 제외 제안 대기열 · 컷 경계 손잡이는 「고급 도구」를 펼칠 때만 — 숨길 뿐 지우지 않는다.
    hidden = (
        '#visual-stage:not([data-vl-advanced="open"]) #vl-panel,\n'
        '#visual-stage:not([data-vl-advanced="open"]) .vl-thumbs > .vl-queue,\n'
        '#visual-stage:not([data-vl-advanced="open"]) .vl-handle { display: none; }'
    )
    assert hidden in css
    workbench = ui_facade.visual_workbench_html({"empty": None, "sources": {}, "cards": []})
    assert 'id="vl-panel"' in workbench and 'id="vl-thumbs"' in workbench
    # 1050px 이하(1024 노트북)·860px 이하에도 추천 구간 칸이 자리를 갖는다.
    narrow = css[css.index("@media (max-width: 1050px)") :]
    assert '"thumbs ranges"' in narrow
    assert '"timeline" "ranges"' in css[css.index("@media (max-width: 860px)") :]
    # 견적·승인 칸(D44)은 숨기는 규칙 밖이다 — 늘 무대 오른쪽 열 맨 위다.
    assert "#vl-quote { grid-area: quote;" in css
    assert "data-vl-advanced" not in _rules_for(css, "#vl-quote")
    assert "data-vl-advanced" not in _rules_for(css, "#library-approve")


def _rules_for(css: str, selector: str) -> str:
    flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return "".join(rule for rule in flat.split("}") if selector in rule.split("{")[0])


def test_blocks_keep_the_approve_and_export_controls_on_the_main_view(
    tmp_path: Path, local_socket: None
) -> None:
    server = _new_server(tmp_path)
    app = server.build_blocks()
    by_elem = {getattr(b, "elem_id", None): b for b in app.blocks.values()}
    bar = by_elem["vl-command-bar"]
    stage = by_elem["visual-stage"]

    def inside(block: Any, parent: Any) -> bool:
        node = block
        while node is not None:
            if node is parent:
                return True
            node = getattr(node, "parent", None)
        return False

    for elem in ("library-approve", workspace_service.FOLDER_OPEN_ID, "vl-render-export"):
        assert inside(by_elem[elem], bar), elem  # 명령 줄 — 서랍 밖
    for elem in ("vl-quote", workspace_service.RENDER_EXPORT_CARD_ID):
        assert inside(by_elem[elem], stage), elem  # 승인 칸·결과 카드는 무대에 그대로
    drawer = by_elem["settings-drawer"]
    assert not inside(by_elem["library-approve"], drawer)
    assert not inside(by_elem["vl-quote"], drawer)


# ── 3. 실제 백엔드 — 합성 원본 하나 ──────────────────────────────────────────


def _range_call(server: Any, model: dict[str, Any], **extra: object) -> dict[str, Any]:
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    payload: dict[str, object] = {
        "epoch": model["epoch"],
        "token": model["token"],
        "signature": model["signature"],
        "op": "range",
        "revisionId": model["revisionId"],
        "basePlanId": model["basePlanId"],
        "sourceId": model["selectedSourceId"],
        "page": 1,
    }
    payload.update(extra)
    return fns["vl_apply"](payload)


@needs_ffmpeg
def test_a_range_mark_is_one_revision_and_changes_the_kept_ranges(tmp_path: Path) -> None:
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    model = _model_of(server.visual_workbench_render(root)[1])
    source_id = model["selectedSourceId"]
    assert model["sources"][source_id]["kept"] == []  # 분석 전 — 등급이 없어 남길 것이 없다
    facade = server.raw.review_workspace_facade(root)
    before = facade.load_head().revision

    # 1.23초~3.01초 → 프레임(25fps)에 맞춰 31/25~3초(가장 가까운 프레임) 「추천」.
    reply = _range_call(server, model, mark="recommended", startSec="1.23", endSec="3.01")
    assert reply["ok"] is True and reply["rangeStale"] is False, reply
    assert reply["message"].startswith("1.24초~3.00초 표시: 추천.")
    head = facade.load_head().revision
    assert head.parent_revision_id == before.revision_id  # 표시 하나 = 리비전 하나
    assert head.edit is not None and head.edit.kind == "range"
    marked = _model_of(reply["html"])
    assert marked["revisionId"] == head.revision_id
    assert marked["sources"][source_id]["kept"] == [{"start": "31/25", "end": "3", "human": True}]
    marks = [it.get("mark") for it in marked["sources"][source_id]["rail"]["intervals"]]
    assert "recommended" in marks

    # 그 머리 0.28초를 「비추천」 — 사람이 확정한 제외라 추천 덮기를 이기고 그만큼 빠진다
    # (렌더와 같다).
    second = _range_call(server, marked, mark="not_recommended", startSec="1.24", endSec="1.52")
    assert second["ok"] is True, second
    after = _model_of(second["html"])
    assert after["sources"][source_id]["kept"] == [{"start": "38/25", "end": "3", "human": True}]
    assert "not_recommended" in [
        it.get("mark") for it in after["sources"][source_id]["rail"]["intervals"]
    ]

    # 되돌리기 — 같은 서버 함수의 `undo` 한 걸음, 앞 표시의 남길 범위로 돌아간다.
    undone = _range_call(server, after, op="undo")
    assert undone["ok"] is True and undone["message"] == workspace_service.RANGE_UNDONE_TEXT
    back = _model_of(undone["html"])
    assert back["sources"][source_id]["kept"] == [{"start": "31/25", "end": "3", "human": True}]


@needs_ffmpeg
def test_a_stale_mark_redraws_without_writing_and_refusals_say_why(tmp_path: Path) -> None:
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    model = _model_of(server.visual_workbench_render(root)[1])
    first = _range_call(server, model, mark="normal", startSec="1", endSec="2")
    assert first["ok"] is True
    facade = server.raw.review_workspace_facade(root)
    head = facade.load_head().revision.revision_id
    # 화면이 본 옛 리비전으로 다시 — 쓰지 않고 새 머리로 다시 그려 까닭을 말한다.
    stale = _range_call(server, model, mark="recommended", startSec="0", endSec="1")
    assert stale["ok"] is True and stale["rangeStale"] is True
    assert stale["message"] == workspace_service.RANGE_STALE_TEXT
    assert _model_of(stale["html"])["revisionId"] == head
    assert facade.load_head().revision.revision_id == head
    # 옛 화면의 되돌리기도 낡음이다 — 다른 곳의 편집을 몰래 되돌리지 않는다.
    stale_undo = _range_call(server, model, op="undo")
    assert stale_undo["rangeStale"] is True
    assert facade.load_head().revision.revision_id == head
    fresh = _model_of(stale["html"])
    for start, end in (("2", "2"), ("3", "1"), ("0", "9")):
        refused = _range_call(server, fresh, mark="recommended", startSec=start, endSec=end)
        assert refused["ok"] is False, (start, end)
        assert refused["error"].startswith(workspace_service.RANGE_REFUSED_PREFIX)
    unknown = _range_call(server, fresh, mark="best", startSec="0", endSec="1")
    assert unknown["ok"] is False and "알 수 없다" in unknown["error"]
    float_end = _range_call(server, fresh, mark="recommended", startSec="0", endSec=1.5)
    assert float_end["ok"] is True  # 십진 표기로 읽는다(정확 유리수) — float 이진값을 쓰지 않는다
    assert facade.load_head().revision.parent_revision_id == head


def test_range_service_constants_match_the_review_marks() -> None:
    assert raw_service.KEPT_HUMAN_REASON == "human_recommended"
    assert set(workspace_service.RANGE_MARK_WORDS) == set(RANGE_MARKS)


def test_command_bar_review_edits_redraw_on_a_stale_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「제외 제안 모두 확인」·실행 취소·「자동 제외 전체 복원」이 낡은 머리를 만나면(여는 때의 정리
    고리가 D53 순서 맞춤으로 머리를 옮기는 중) 오류로 끝나지 않고 다시 그려 다시 누르게 한다."""
    from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade

    server = _new_server(tmp_path)
    signature = server.current_signature()

    def stale(*_args: object, **_kwargs: object) -> None:
        raise raw_service.StaleRevisionError("검토가 새 계획으로 옮겨졌다")

    monkeypatch.setattr(ReviewWorkspaceFacade, "unreviewed_auto_exclusion_count", lambda _s: 1)
    for name in ("confirm_automatic_exclusions", "undo", "restore_automatic"):
        monkeypatch.setattr(ReviewWorkspaceFacade, name, stale)
    toasts: list[tuple[str, str]] = []
    monkeypatch.setattr(workspace_service, "_toast", lambda kind, text: toasts.append((kind, text)))
    replies = [
        server.capcut_confirm_auto_view(signature),
        server.capcut_edit_view(signature, "undo"),
        server.capcut_edit_view(signature, "restore"),
    ]
    for message, _shell, workbench in replies:
        assert message == workspace_service.EDIT_STALE_TEXT
        assert 'id="visual-workbench"' in workbench  # 새로 그린 화면이 간다
    assert toasts == [("warning", workspace_service.EDIT_STALE_TEXT)] * 3
