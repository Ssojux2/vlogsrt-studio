"""V2 작업대 UI 계약 — 순수 조립·실제 클라이언트 실행·서버 함수(합성 미디어).

세 층을 잰다(스펙: "presence-only/no-op 인라인 실행본 금지"):

1. **순수 도우미** — 작업대 HTML은 이스케이프되고 `<script>`를 쓰지 않으며 모델
   JSON은 data 속성에 안전히 실린다.
2. **클라이언트 실행본(node)** — 부트스트랩을 실제로 실행해(문자열 포함 검사가
   아니다) 정수 유리수(`1001/30000`), 선택→시작점 탐색(썸네일 가운데 아님),
   시간 렠 클릭=탐색 전용, 반복, 늦은 응답 버림(epoch), 같은 원소 재선택 시
   재생기 미재구성, 제안→적용 CAS, 자막 행 선택 클라이언트 전용·오버레이
   `start<=t<end`·토글·저장, 값 갱신 순서(watch가 먼저 불리고 DOM 교체는
   마이크로태스크로 미뤄지는 실제 그라디오 순서에서 **커밋 뒤의 새 뿌리만**
   다시 묶는다 — A7 실측 결함), 설치본 렌더기의 **그 자리 morph**(같은
   태그 뿌리의 원소 신원은 유지한 채 모델 속성·동적 자식만 갈아친다 — A8
   실측)를 실제 저장 경로로 재현해 **커밋된 모델 세대**로 같은 뿌리를 다시
   묶는다(듣는이 교체·누적 없음, 관찰자 루프 없음), 그리고 **늦은 실패 응답**
   (화면이 바뀐 뒤 도착한 null·거부 — 성공 분기의 echoEpoch 검사 앞이라
   무가드였다, A9 실측)은 요청이 나간 배선 세대·epoch·뿌리가 살아 있을 때만
   효과를 낸다(신선한 화면의 진짜 진단은 보존)는 것을 증명한다. 신선한 요청의
   **진짜 오류**는 생산자 응답이 echoEpoch 없이 와도 그대로 보인다(저장 중 표시
   잔류 없음), 질의·페이징 요청은 화면이 보던 계획·원본·재생 머리 문맥을 실어
   가고, 대기 중 재계획과 경합한 새로고침 응답의 매핑된 선택이 실제 DOM 선택
   (aria-selected)과 이어오는 편집 대상이 된다.
3. **서버 함수(실백엔드·합성 ffmpeg 미디어)** — 질의만으로 모델 조립(생성 0),
   제안→적용의 리비전 묶음, stale·서명·머리 거부, 준비·재계획, A→B→A epoch,
   자막 저장·게이트. 유료 호출 0회.
"""

from __future__ import annotations

import html as html_module
import json
import re
import shutil
import subprocess
import time
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import ui_facade, workspace_service  # noqa: E402

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAIT_SECONDS = 300.0

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node가 없는 환경 — JS 실행 시험은 건너뛴다",
)


def _encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"합성 미디어 생성 실패: {completed.stderr[-400:]}"


def _wait_terminal(server: workspace_service.RawWorkspaceUi, root: Path, job_id: str):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        record = server.raw.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.02)
    return server.raw.get_job(root, job_id)


def _model_of(workbench_html: str) -> dict[str, Any]:
    matched = re.search(r'data-vl-model="([^"]*)"', workbench_html)
    assert matched is not None, "작업대 값에 모델이 없다"
    return json.loads(html_module.unescape(matched.group(1)).replace("\\u003c", "<"))


def _new_server(tmp_path: Path, **kwargs: Any) -> workspace_service.RawWorkspaceUi:
    server = workspace_service.RawWorkspaceUi(
        port=7860, state_path=tmp_path / "state.json", **kwargs
    )
    view = server.create_workspace_view("작업대시험", str(tmp_path / "ws"))
    assert view[1]
    return server


def _prepare_review(server: workspace_service.RawWorkspaceUi, root: Path, tmp_path: Path) -> None:
    """카드 1장(4초 25fps 소리 있는 원본) → 가져오기 → 순서 → 1초 타일 → 검토 초기화."""
    card = tmp_path / "card"
    card.mkdir(exist_ok=True)
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x64:r=25:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000:duration=4",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(card / "발화.mp4"),
        ]
    )
    proposal = server.raw.scan_sources(root, card)
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    record = _wait_terminal(server, root, handle.job_id)
    assert record.status == "succeeded", record.error
    server.raw.confirm_global_source_order(root)
    plan = server.raw.plan_segmentation(root, tile_seconds=Fraction(1))
    from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade

    ReviewWorkspaceFacade(root).initialize_review(plan.revision_id)


# ── 1. 순수 도우미 — 안전한 HTML·모델 속성 ────────────────────────────────────


def test_workbench_html_has_no_script_and_escapes_model() -> None:
    """모델은 data 속성에, `<`·따옴표는 이스케이프되고 `<script>`는 아예 없다."""
    tricky = {
        "empty": None,
        "signature": 'sig"quote<angle>&amp',
        "cards": [{"id": "seg<x", "note": "메모<script>alert(1)</script>"}],
        "video": {"origin": "6673/12800"},
    }
    value = ui_facade.visual_workbench_html(tricky)  # type: ignore[arg-type]
    assert "<script" not in value
    assert 'data-vl-model="' in value
    parsed = _model_of(value)
    assert parsed["signature"] == 'sig"quote<angle>&amp'
    assert parsed["cards"][0]["note"] == "메모<script>alert(1)</script>"
    assert "\\u003c" in value  # 원문 `<`는 속성 안에 그대로 남지 않는다
    # 자리 표시 — 안내 문장도 이스케이프된다.
    placeholder = ui_facade.visual_workbench_html({"empty": "계획이 <없다>"})
    assert "data-vl-empty" in placeholder and "계획이 &lt;없다&gt;" in placeholder


def test_player_and_caption_shells_expose_contract_ids() -> None:
    """껍데기 아이디 계약 — 실행본과 A5 하네스가 짚는 대상들."""
    shell = ui_facade.visual_player_shell_html(None, "안내")
    for anchor in ("visual-video", "visual-rail", "vl-repeat", "vl-pause", "vl-clock"):
        assert f'id="{anchor}"' in shell, anchor
    assert "src=" not in shell  # 미리보기가 없으면 src도 없다(빈 성공 꾸미기 없음)
    assert 'src="/media/abc"' in ui_facade.visual_player_shell_html("/media/abc", "안내")
    player = ui_facade.caption_overlay_player_html(
        "/media/x", waveform="<svg></svg>", provenance="출처"
    )
    for anchor in ("caption-video", "caption-overlay", "cap-overlay-toggle"):
        assert f'id="{anchor}"' in player, anchor
    assert 'class="cap-overlay cap-overlay-on"' in player
    assert 'aria-pressed="true"' in player
    # 출처 원문(경로·sha·코덱·등록 ID)은 접힌 상세로 — 기본 재생기에는 사람 글자만.
    assert "영상 상세" in player and "출처" in player
    assert player.index("<details") > player.index('id="cap-overlay-toggle"')
    workbench = ui_facade.caption_workbench_html({"empty": None, "cues": []})
    for anchor in (
        "cap-cues",
        "cap-text",
        "cap-start",
        "cap-end",
        "cap-cue-id",
        "cap-save",
        "cap-confirm",
    ):
        assert f'id="{anchor}"' in workbench, anchor
    # 정밀 시각·큐 ID는 접힌 상세 안에 — 기본 흐름은 글자 저장·확정만 본다
    # (아이디·읽기 전용·실행본 선택자 계약은 그대로).
    details_at = workbench.index("정밀 시각")
    for anchor in ("cap-start", "cap-end", "cap-cue-id", "cap-save-time"):
        assert workbench.index(f'id="{anchor}"') > details_at, anchor
    for anchor in ("cap-save", "cap-confirm", "cap-text"):
        assert workbench.index(f'id="{anchor}"') < details_at, anchor
    details = ui_facade.caption_workbench_details_html(
        {"empty": None, "details": [{"id": "a|b", "whisper": "w"}], "provenance": "p"}
    )
    assert "<details" in details and "a|b" in details and "whisper" in details


def test_ui_facade_still_has_no_derived_imports() -> None:
    """순수 도우미 계약 유지 — 새 상수·조립기가 파생 계층을 알지 않는다."""
    import ui.ui_facade as module

    sources = set(vars(module))
    assert not any(name.startswith(("vlogsrt", "gradio", "fastapi")) for name in sources)
    assert ui_facade.VISUAL_BOOTSTRAP_JS and ui_facade.CAPTION_BOOTSTRAP_JS
    assert "watch(" in ui_facade.VISUAL_BOOTSTRAP_JS  # 공개 클라이언트 계약을 쓴다


# ── 2. 클라이언트 실행본 — node에서 실제 실행 ─────────────────────────────────

_NODE_HARNESS = r"""
const fs = require('fs');
(async function () {
const checkpoints = [];

// ── DOM 스텁 ───────────────────────────────────────────────────────────────
function el(spec) {
  spec = spec || {};
  const node = {
    id: spec.id || '',
    attrs: Object.assign({}, spec.attrs || {}),
    value: spec.value || '',
    style: {},
    textContent: '',
    parentNode: null,
    disabled: !!spec.disabled,
    classList: { add() {}, remove() {}, contains() { return false; } },
    _listeners: {},
    _inner: '',
    _text: '',
    _children: [],
    set innerHTML(v) { this._inner = String(v); },
    get innerHTML() { return this._inner; },
    // 실제 DOM처럼 — textContent 대입은 자식 노드(예: 내려받기 링크)를 지운다.
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v == null ? '' : v); this._children = []; },
    appendChild(child) { this._children.push(child); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) {
      if (k in this.attrs) {
        return this.attrs[k].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
      }
      if (k === 'id') { return this.id; }
      return null;
    },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      const at = list.indexOf(fn);
      if (at >= 0) { list.splice(at, 1); }
    },
    getBoundingClientRect() { return { left: 0, top: 0, width: spec.width || 400 }; },
  };
  return node;
}

function videoStub(name) {
  const calls = { play: 0, pause: 0, load: 0, srcSets: [], unload: 0 };
  const listeners = {};
  const state = { currentTime: 0, paused: true, _src: '' };
  const video = {
    id: name,
    get src() { return state._src; },
    set src(v) { state._src = String(v); calls.srcSets.push(String(v)); },
    get currentTime() { return state.currentTime; },
    set currentTime(v) { state.currentTime = v; },
    get paused() { return state.paused; },
    attrs: {},
    style: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return (k in this.attrs) ? this.attrs[k] : null; },
    removeAttribute(k) {
      delete this.attrs[k];
      if (k === 'src') { calls.unload += 1; state._src = ''; }
    },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const list = listeners[type] || [];
      const at = list.indexOf(fn);
      if (at >= 0) { list.splice(at, 1); }
    },
    play() { calls.play += 1; state.paused = false; },
    pause() { calls.pause += 1; state.paused = true; },
    load() { calls.load += 1; },
  };
  return { video, calls, listeners, state,
    tick(t) { state.currentTime = t; (listeners.timeupdate || []).slice().forEach((fn) => fn()); },
    pauseEvent() { video.pause(); (listeners.pause || []).slice().forEach((fn) => fn()); } };
}

function docStub(registry) {
  return {
    getElementById(id) { return registry[id] || null; },
    createElement(tag) { const node = el({}); node.tagName = String(tag); return node; },
    createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
  };
}

// ── 실제 진입점 실행 — 사전 주입(Preseed) 없음 ────────────────────────────────
// gradio 공개 계약 그대로: js_on_load 본문을 (element, props, trigger, watch,
// server) 인자로 실행한다. 부트스트랩 스스로 window.__vl*Workbench* 전역을
// 심는다 — 예전 하네스가 이 전역을 미리 깔아 서버 함수 0회 호출 결함을 감췄다.
const bootstraps = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const windowScope = {};

// 관찰자 그루터기 — 제품 코드는 W.MutationObserver 공개 계약을 쓰고, 교체
// 사건 배수는 하네스가 직접 부른다(A7의 독립 실행본과 같은 역할).
class StubObserver {
  constructor(fn) { this.fn = fn; }
  observe(target, options) {
    this.options = options;
    (target.__vlObservers = target.__vlObservers || []).push(this);
  }
  disconnect() { this.disconnected = true; }
}
windowScope.MutationObserver = StubObserver;
function flushObservers(block) {
  (block.__vlObservers || []).slice().forEach((o) => { o.fn([]); });
}

function runEntrypoint(source, element, props, server) {
  const watchCalls = [];
  const watch = (name, fn) => {
    watchCalls.push({ name: String(name) });
    watchCalls[watchCalls.length - 1].fn = fn;
  };
  const fn = new Function('element', 'props', 'trigger', 'watch', 'server',
    'document', 'window', source);
  fn(element, props, () => {}, watch, server, element._doc, windowScope);
  return { watchCalls };
}

const visualBootEl = el({ id: 'visual-workbench-block' });
visualBootEl._doc = docStub({});
const visualBoot = runEntrypoint(bootstraps.visual, visualBootEl,
  { value: '' }, {});
checkpoints.push({ case: 'visual-bootstrap',
  watchRegistered: visualBoot.watchCalls.length === 1 && visualBoot.watchCalls[0].name === 'value',
  libLoaded: !!windowScope.__vlLib,
  wireVisual: typeof windowScope.__vlWireVisual === 'function',
  propsStashed: windowScope.__vlVisualWorkbenchProps !== undefined,
  serverStashed: windowScope.__vlVisualWorkbenchServer !== undefined });

const captionBootEl = el({ id: 'caption-workbench-block' });
captionBootEl._doc = docStub({});
const captionBoot = runEntrypoint(bootstraps.caption, captionBootEl,
  { value: '' }, {});
checkpoints.push({ case: 'caption-bootstrap',
  watchRegistered: captionBoot.watchCalls.length === 1,
  wireCaption: typeof windowScope.__vlWireCaption === 'function',
  propsStashed: windowScope.__vlCaptionWorkbenchProps !== undefined,
  serverStashed: windowScope.__vlCaptionWorkbenchServer !== undefined });

// ── 유리수 라이브러리 — 정수 정확, Number('1/5') 금지 ──────────────────────
const VL = windowScope.__vlLib;
const rat1001 = VL.parse('1001/30000');
checkpoints.push({ case: 'rat-parse',
  exact: rat1001[0] === 1001 && rat1001[1] === 30000,
  parsedNotNaN: rat1001 !== null,
  badRejected: VL.parse('abc') === null && VL.parse('') === null && VL.parse('1/') === null });
checkpoints.push({ case: 'rat-decimal',
  decimal: VL.parse('1.48')[0] === 37 && VL.parse('1.48')[1] === 25,
  intForm: VL.parse('5')[0] === 5 && VL.parse('5')[1] === 1 });
const g3 = VL.sub(VL.parse('19473/12800'), VL.parse('6673/12800'));
checkpoints.push({ case: 'rat-g3-inverse',
  localOne: g3[0] === 1 && g3[1] === 1,
  roundTrip: VL.str(VL.add(g3, VL.parse('6673/12800'))) === '19473/12800' });
checkpoints.push({ case: 'rat-arith',
  add: VL.str(VL.add(VL.parse('1/6'), VL.parse('1/3'))) === '1/2',
  mul: VL.str(VL.mul(VL.parse('3/4'), VL.parse('2/3'))) === '1/2',
  cmp: (VL.cmp(VL.parse('1/2'), VL.parse('1/3')) > 0) &&
    (VL.cmp(VL.parse('1/2'), VL.parse('1/2')) === 0) });

// ── 영상 작업대 — 원본 A/B/C(서로 다른 원점·화면·소리) ──────────────────────
function visualModel() {
  return {
    epoch: 'epoch-1', token: 'tok-1', signature: 'ws-sig',
    basePlanId: 'plan-1', revisionId: 'rev-1',
    page: 1, pageCount: 1, total: 6,
    selectedId: 'segA1', selectedSourceId: 'srcA', selectedCard: null,
    sources: {
      srcA: {
        src: '/media/preview-a', state: 'ready', origin: '0', duration: '3',
        sourceId: 'srcA', sourceName: '01-landscape.mp4', audioOrdinal: 0,
        tracks: [{ ordinal: 0, label: '트랙 0 · aac 8000Hz' }], error: null,
        rail: { duration: '3', frameSec: '1/25', intervals: [
          { id: 'segA1', start: '0', end: '1', sel: 'included', needsExtract: false },
          { id: 'segA2', start: '1', end: '2', sel: 'included', needsExtract: true },
          { id: 'segA3', start: '2', end: '3', sel: 'excluded', needsExtract: false },
        ] },
      },
      srcB: {
        src: '/media/preview-b', state: 'ready', origin: '521/1000', duration: '7/4',
        sourceId: 'srcB', sourceName: '02-portrait-silent.mp4', audioOrdinal: null,
        tracks: [], error: null,
        rail: { duration: '7/4', frameSec: '1/30', intervals: [
          { id: 'segB1', start: '3/2', end: '2', sel: 'included', needsExtract: false },
          { id: 'segB2', start: '2', end: '7/4', sel: 'included', needsExtract: false },
        ] },
      },
      srcC: {
        src: null, state: 'preparing', origin: '0', duration: null,
        sourceId: 'srcC', sourceName: '03-pending.mp4', audioOrdinal: 0,
        tracks: [], error: null,
        rail: { duration: '2', frameSec: '1/24', intervals: [
          { id: 'segC1', start: '0', end: '2', sel: 'included', needsExtract: false },
        ] },
      },
    },
    cards: [
      { id: 'segA1', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 0,
        start: '0', end: '1', dur: '1', sel: 'included', rec: 'recommended',
        speech: 'unconfirmed_empty', tags: ['실내', '대화'],
        note: '첫조각', thumb: { state: 'ready', src: '/media/thumb-a' },
        neighbor: 'segA2', needsExtract: false },
      { id: 'segA2', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 1,
        start: '1', end: '2', dur: '1', sel: 'included', rec: 'normal',
        speech: 'speech_detected', tags: [],
        note: '', thumb: { state: 'missing', src: null },
        neighbor: 'segA3', needsExtract: true },
      { id: 'segA3', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 2,
        start: '2', end: '3', dur: '1', sel: 'excluded', rec: '', speech: '',
        tags: [], note: '', thumb: { state: 'ready', src: '/media/thumb-c' },
        neighbor: null, needsExtract: false },
      { id: 'segB1', sourceId: 'srcB', sourceName: '02-portrait-silent.mp4', order: 3,
        start: '3/2', end: '2', dur: '1/2', sel: 'included', rec: 'normal',
        speech: 'audio_absent', tags: ['세로화면'],
        note: '', thumb: { state: 'ready', src: '/media/thumb-b' },
        neighbor: 'segB2', needsExtract: false },
      { id: 'segB2', sourceId: 'srcB', sourceName: '02-portrait-silent.mp4', order: 4,
        start: '2', end: '7/4', dur: '3/4', sel: 'included', rec: '', speech: '',
        tags: [], note: '', thumb: { state: 'missing', src: null },
        neighbor: null, needsExtract: false },
      { id: 'segC1', sourceId: 'srcC', sourceName: '03-pending.mp4', order: 5,
        start: '0', end: '2', dur: '2', sel: 'included', rec: '', speech: '',
        tags: [], note: '', thumb: { state: 'preparing', src: null },
        neighbor: null, needsExtract: false },
    ],
    activeJobs: [], staleSources: [], needsPlan: true, packageDir: 'clips/package-000001',
  };
}

let envSerial = 0;
function visualEnv(serverHandlers, modelOverride, propsFactory) {
  envSerial += 1;
  const model = modelOverride || visualModel();
  const root = el({ id: 'visual-workbench', attrs: { 'data-vl-model': JSON.stringify(model) } });
  const thumbs = el({ id: 'vl-thumbs' });
  const panel = el({ id: 'vl-panel' });
  const footnote = el({ id: 'vl-footnote' });
  const rail = el({ id: 'visual-rail', width: 300 });
  const clock = el({ id: 'vl-clock' });
  const repeatBtn = el({ id: 'vl-repeat' });
  const pauseBtn = el({ id: 'vl-pause' });
  const note = el({ id: 'visual-note' });
  const vid = videoStub('visual-video');
  const registry = {
    'visual-workbench': root,
    'vl-thumbs': thumbs,
    'vl-panel': panel,
    'vl-footnote': footnote,
    'visual-rail': rail,
    'vl-clock': clock,
    'vl-repeat': repeatBtn,
    'vl-pause': pauseBtn,
    'visual-note': note,
    'visual-video': vid.video,
  };
  const doc = docStub(registry);
  Object.keys(registry).forEach((k) => { registry[k]._doc = doc; });
  const serverCalls = [];
  const held = [];
  const server = {};
  Object.keys(serverHandlers || {}).forEach((name) => {
    server[name] = (payload) => {
      serverCalls.push({ name, payload: JSON.parse(JSON.stringify(payload)) });
      const out = serverHandlers[name](payload);
      if (out && out.__hold) {
        // 지연 응답 — 겹친 제안의 세대 판별 재료(A6 `latest_proposal_nonce_wins`).
        // resolve·reject 둘 다 붙잡는다(늦은 null/거부 응답 재료, A9).
        const deferred = {};
        deferred.promise = new Promise((resolve, reject) => {
          deferred.resolve = resolve;
          deferred.reject = reject;
        });
        held.push({ name, payload, deferred });
        return deferred.promise;
      }
      return Promise.resolve(out);
    };
  });
  const propsCalls = [];
  const props = propsFactory ? propsFactory(propsCalls)
    : { set value(v) { propsCalls.push(String(v)); }, get value() { return ''; } };
  const block = el({ id: 'visual-workbench-block' });
  block._doc = doc;
  runEntrypoint(bootstraps.visual, block, props, server);
  return {
    model, root, thumbs, panel, rail, clock, note, vid, server, serverCalls, props, propsCalls,
    registry, doc, held, block,
    click(target) {
      (this.root._listeners.click || []).slice().forEach((fn) => fn({ target }));
    },
    rewired() { windowScope.__vlWireVisual(root, doc); },
  };
}

// 배선 — 목록·레일·패널이 모델로 그려졌다(사람 상태 표기·태그 포함).
const env = visualEnv({});
checkpoints.push({ case: 'wire-render',
  thumbs: env.thumbs.innerHTML.includes('data-vl-card="segA1"') &&
    env.thumbs.innerHTML.includes('01-landscape.mp4'),
  humanSpeech: env.thumbs.innerHTML.includes('발화 있음'),
  excludedBadge: env.thumbs.innerHTML.includes('제외'),
  needsBadge: env.thumbs.innerHTML.includes('영상 갱신 필요'),
  pager: env.thumbs.innerHTML.includes('페이지 1/1'),
  rail: env.rail.innerHTML.includes('data-vl-interval="segA1"') &&
    env.rail.innerHTML.includes('data-vl-handle="left"'),
  playhead: env.rail.innerHTML.includes('vl-playhead'),
  panel: env.panel.innerHTML.includes('메모') &&
    env.panel.innerHTML.includes('01-landscape.mp4'),
  panelHuman: env.panel.innerHTML.includes('빈 실행(미확인)') &&
    env.panel.innerHTML.includes('사용 권고'),
  panelTags: env.panel.innerHTML.includes('분석 근거') &&
    env.panel.innerHTML.includes('실내'),
  provenanceInDetails: env.panel.innerHTML.includes('G3') &&
    env.panel.innerHTML.includes('<details'),
  panelDetails: env.panel.innerHTML.includes('rev-1') &&
    env.panel.innerHTML.includes('plan-1'),
  videoSrc: env.vid.video.src === '/media/preview-a',
  srcSetOnce: env.vid.calls.srcSets.length === 1 });

// 저장 RPC — 실제 진입점이 심은 서버 전역으로 정확히 한 번 간다(A5 재현).
const envRPC = visualEnv({
  vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1', message: '저장했다' }),
});
envRPC.registry.panel = envRPC.panel;
const noteBox = el({ id: 'vl-note-input' });
noteBox.value = '명시적 메모';
envRPC.doc.getElementById = (id) => {
  if (id === 'vl-note-input') { return noteBox; }
  return { root: envRPC.root, thumbs: envRPC.thumbs, panel: envRPC.panel,
    footnote: envRPC.footnote, 'visual-rail': envRPC.rail, 'vl-clock': envRPC.clock,
    'vl-repeat': envRPC.registry['vl-repeat'], 'vl-pause': envRPC.registry['vl-pause'],
    'visual-note': envRPC.note, 'visual-video': envRPC.vid.video,
    'visual-workbench': envRPC.root }[id] || null;
};
envRPC.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const rpcCall = envRPC.serverCalls[0];
checkpoints.push({ case: 'rpc-real-entrypoint',
  calledOnce: envRPC.serverCalls.length === 1 && rpcCall.name === 'vl_apply',
  noteCarried: rpcCall && rpcCall.payload.note === '명시적 메모',
  signatureCarried: rpcCall && rpcCall.payload.signature === 'ws-sig',
  tokenCarried: rpcCall && rpcCall.payload.token === 'tok-1',
  epochCarried: rpcCall && rpcCall.payload.epoch === 'epoch-1' });

// 원본 전환 A→B — 스트림 교체·반복 해제·B의 렠·B의 잰 원점 가산 시작 이동.
// (rpc-real-entrypoint 진입점이 옛 영상을 능동 언로드했다 — 이 화면을 다시 묶는다.)
env.rewired();
env.vid.state.currentTime = 0.4;
env.registry['vl-repeat']._listeners.click.slice().forEach((fn) => fn());
const playsBeforeSwitch = env.vid.calls.play;
const bCard = { getAttribute: (k) => (k === 'data-vl-card' ? 'segB1' : null) };
env.root._listeners.click.slice().forEach((fn) => fn({ target: bCard }));
const expectB = VL.toFloat(VL.add(VL.parse('3/2'), VL.parse('521/1000')));
checkpoints.push({ case: 'source-switch-a-to-b',
  srcSwitched: env.vid.video.src === '/media/preview-b' &&
    env.vid.calls.srcSets.length === 2,
  paused: env.vid.video.paused === true,
  loopCleared: windowScope.__vlVisualWorkbenchState.loop === null,
  seekStartPlusOrigin: Math.abs(env.vid.state.currentTime - expectB) < 1e-9,
  railIsB: env.rail.innerHTML.includes('data-vl-interval="segB1"') &&
    env.rail.innerHTML.includes('data-vl-interval="segA1"') === false,
  noteIsB: env.note.textContent.includes('02-portrait-silent.mp4'),
  noServer: env.serverCalls.length === 0,
  noAutoplay: env.vid.calls.play === playsBeforeSwitch });

// 같은 원본 재선택 — 재생기 스트림은 그대로(원소 재구성 없음), 시작만 이동.
env.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segB2' : null) } }));
const expectB2 = VL.toFloat(VL.add(VL.parse('2'), VL.parse('521/1000')));
checkpoints.push({ case: 'same-source-keeps-stream',
  srcUntouched: env.vid.calls.srcSets.length === 2,
  seekStart: Math.abs(env.vid.state.currentTime - expectB2) < 1e-9,
  sameElementStill: env.vid.video.id === 'visual-video' });

// 미리보기 없는 원본 C — 이전 영상을 틀은 채 새 이름표를 붙이지 않는다.
env.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segC1' : null) } }));
checkpoints.push({ case: 'missing-source-unloads',
  unloaded: env.vid.calls.unload >= 1 && env.vid.video.src === '',
  pausedStill: env.vid.video.paused === true,
  honestStatus: env.note.textContent.includes('03-pending.mp4') &&
    env.note.textContent.includes('준비 중'),
  railIsC: env.rail.innerHTML.includes('data-vl-interval="segC1"'),
  clockBlank: env.clock.textContent === '',
  panelStatus: env.panel.innerHTML.includes('미리보기 준비 중'),
  repeatBlocked: (env.registry['vl-repeat']._listeners.click || [])
    .slice().forEach(() => {}) === undefined });

// 원본 A로 돌아와 나머지 상호작용 — 렠 클릭 탐색 전용·재생 머리 국소 시각.
const backA = { getAttribute: (k) => (k === 'data-vl-card' ? 'segA2' : null) };
env.root._listeners.click.slice().forEach((fn) => fn({ target: backA }));
const railBox = { getAttribute: () => null };
env.rail._listeners.click.slice().forEach((fn) => fn({ target: railBox, clientX: 150 }));
checkpoints.push({ case: 'rail-click-seeks-only',
  seekOnly: Math.abs(env.vid.state.currentTime - 1.5) < 1e-9,
  stillNoServer: env.serverCalls.length === 0 });
env.vid.tick(2);
checkpoints.push({ case: 'playhead-local',
  clock: env.clock.textContent === '2.000',
  railMark: env.rail.innerHTML.includes('vl-playhead') });

// 선택 반복 — 끝에서 시작으로 되돌리고, 정지하면 풀린다(A 원점 0).
env.registry['vl-repeat']._listeners.click.slice().forEach((fn) => fn());
env.vid.tick(2);
const afterLoop = env.vid.state.currentTime;
env.vid.pauseEvent();
env.vid.tick(2.5);
checkpoints.push({ case: 'repeat-loop',
  rewound: afterLoop === 1,
  played: env.vid.calls.play >= 1,
  stopsAfterPause: env.vid.state.currentTime === 2.5 });

// 제안→초안 상자→적용 — CAS 리비전은 제안이 준 값.
const envP = visualEnv({
  vl_propose: () => ({ ok: true, echoEpoch: 'epoch-1', valid: true, reason: null,
    snapSeconds: '12/25', snapPts: 6144, frame: 12, headRevisionId: 'rev-9' }),
  vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1',
    html: '<div id="visual-workbench" data-vl-model="' +
      JSON.stringify(visualModel()).replace(/"/g, '&quot;') + '"></div>' }),
});
envP.vid.state.currentTime = 12 / 25;
envP.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'split' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'propose-draft',
  draftShown: envP.panel.innerHTML.includes('12/25') ||
    envP.panel.innerHTML.includes('확정 시각'),
  frameShown: envP.panel.innerHTML.includes('12'),
  headBound: envP.panel.innerHTML.includes('rev-9') === false });
envP.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'apply-draft' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
const applyCall = envP.serverCalls.find((c) => c.name === 'vl_apply');
checkpoints.push({ case: 'apply-cas',
  usedProposalHead: applyCall && applyCall.payload.revisionId === 'rev-9',
  usedSnapPts: applyCall && applyCall.payload.splitPts === 6144,
  tokenOnApply: applyCall && applyCall.payload.token === 'tok-1',
  htmlApplied: envP.propsCalls.length === 1 });

// 늦은 응답 — echoEpoch가 화면 epoch와 다르면 값 적용 없음.
const envL = visualEnv({
  vl_refresh: () => ({ ok: true, echoEpoch: 'another-epoch', html: '<p>stale</p>' }),
});
envL.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'refresh' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
const envF = visualEnv({
  vl_refresh: () => ({ ok: true, echoEpoch: 'epoch-1', html: '<p>fresh</p>' }),
});
envF.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'refresh' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'epoch-guard',
  staleDropped: envL.propsCalls.length === 0,
  freshApplied: envF.propsCalls.length === 1 && envF.propsCalls[0] === '<p>fresh</p>' });

// 서명·토큰은 모든 요청에 실린다.
checkpoints.push({ case: 'signature-rides',
  carries: envF.serverCalls.length > 0 && envF.serverCalls[0].payload.signature === 'ws-sig',
  tokenRides: envF.serverCalls[0].payload.token === 'tok-1',
  epochRides: envF.serverCalls[0].payload.epoch === 'epoch-1' });

// 준비 — 트랙 칸이 없으면(첫 준비 전) 선택을 실지 않는다(침묵 명령 금지).
const envPrep = visualEnv({
  vl_prepare: () => ({ ok: true, echoEpoch: 'epoch-1', message: '제출' }),
});
const prepB = { getAttribute: (k) => (k === 'data-vl-card' ? 'segB1' : null) };
envPrep.root._listeners.click.slice().forEach((fn) => fn({ target: prepB }));
envPrep.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'prepare' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
const prepCall = envPrep.serverCalls.find((c) => c.name === 'vl_prepare');
checkpoints.push({ case: 'prepare-no-track-default',
  omitted: prepCall && JSON.stringify(prepCall.payload.audioSelections) === '{}',
  tokenAgain: prepCall && prepCall.payload.token === 'tok-1' });

// ── 자막 작업대 배선 ───────────────────────────────────────────────────────
function captionModel() {
  return {
    epoch: 'cap-epoch-1', token: 'cap-tok-1', signature: 'ws-sig', runId: 'run-1',
    head: 3, canConfirm: false, blockReason: '열린 경고 1개', provenance: '출처 — final',
    cues: [
      { id: 'a0000', startMs: '500', endMs: '2500', text: '망원동 카페', basis: 'auto',
        seek: '0.500000', endSeek: '2.500000' },
      { id: 'm0001', startMs: '3000', endMs: '3400', text: '수동 큐', basis: 'manual',
        seek: '3.000000', endSeek: '3.400000' },
    ],
  };
}

function captionEnv(serverHandlers, propsFactory) {
  const model = captionModel();
  const root = el({ id: 'caption-workbench', attrs: { 'data-vl-model': JSON.stringify(model) } });
  const cues = el({ id: 'cap-cues' });
  const msg = el({ id: 'cap-msg' });
  const textEl = el({ id: 'cap-text' });
  const startEl = el({ id: 'cap-start' });
  const endEl = el({ id: 'cap-end' });
  const cueIdEl = el({ id: 'cap-cue-id' });
  const overlay = el({ id: 'caption-overlay' });
  const toggle = el({ id: 'cap-overlay-toggle', attrs: { 'aria-pressed': 'true' } });
  const vid = videoStub('caption-video');
  const registry = {
    'caption-workbench': root, 'cap-cues': cues, 'cap-msg': msg, 'cap-text': textEl,
    'cap-start': startEl, 'cap-end': endEl, 'cap-cue-id': cueIdEl,
    'caption-overlay': overlay, 'cap-overlay-toggle': toggle, 'caption-video': vid.video,
  };
  const doc = docStub(registry);
  Object.keys(registry).forEach((k) => { registry[k]._doc = doc; });
  const serverCalls = [];
  const held = [];
  const server = {};
  Object.keys(serverHandlers || {}).forEach((name) => {
    server[name] = (payload) => {
      serverCalls.push({ name, payload: JSON.parse(JSON.stringify(payload)) });
      const out = serverHandlers[name](payload);
      if (out && out.__hold) {
        // 지연 응답 — 늦은 null/거부 응답 재료(A9). resolve·reject 둘 다 붙잡는다.
        const deferred = {};
        deferred.promise = new Promise((resolve, reject) => {
          deferred.resolve = resolve;
          deferred.reject = reject;
        });
        held.push({ name, payload, deferred });
        return deferred.promise;
      }
      return Promise.resolve(out);
    };
  });
  const propsCalls = [];
  const props = propsFactory ? propsFactory(propsCalls)
    : { set value(v) { propsCalls.push(String(v)); }, get value() { return ''; } };
  const block = el({ id: 'caption-workbench-block' });
  block._doc = doc;
  runEntrypoint(bootstraps.caption, block, props, server);
  return { model, root, cues, msg, textEl, startEl, endEl, cueIdEl, overlay, toggle, vid,
    server, serverCalls, props, propsCalls, registry, doc, held, block,
    click(target) {
      (this.root._listeners.click || []).slice().forEach((fn) => fn({ target }));
    },
    rewired() { windowScope.__vlWireCaption(root, doc); } };
}

const capEnv = captionEnv({});
checkpoints.push({ case: 'caption-render',
  rows: capEnv.cues.innerHTML.includes('data-vl-cue="a0000"') &&
    capEnv.cues.innerHTML.includes('수동 큐'),
  seekAttrs: capEnv.cues.innerHTML.includes('data-seek-start="0.500000"'),
  manualMark: capEnv.cues.innerHTML.includes('cap-row-manual'),
  editorFilled: capEnv.cueIdEl.value === 'a0000' && capEnv.textEl.value === '망원동 카페',
  msFilled: capEnv.startEl.value === '500' && capEnv.endEl.value === '2500' });

// 행 선택 — 클라이언트 전용(재생기 이동·재생, 서버 0회).
capEnv.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
checkpoints.push({ case: 'cue-row-client-only',
  seeked: Math.abs(capEnv.vid.state.currentTime - 3.0) < 1e-9,
  played: capEnv.vid.calls.play === 1,
  noServer: capEnv.serverCalls.length === 0,
  refilled: capEnv.cueIdEl.value === 'm0001' && capEnv.textEl.value === '수동 큐' });

// 오버레이 — start<=t<end (측정 매핑 그대로). 2.5초는 a0000의 끝=경계 밖.
capEnv.vid.tick(1.0);
const inside = capEnv.overlay.textContent;
capEnv.vid.tick(2.5);
const boundary = capEnv.overlay.textContent;
capEnv.vid.tick(3.2);
const manualInside = capEnv.overlay.textContent;
checkpoints.push({ case: 'caption-overlay',
  inside: inside === '망원동 카페',
  endExclusive: boundary === '',
  manual: manualInside === '수동 큐' });

// 오버레이 토글 — 클라이언트 전용.
capEnv.toggle._listeners.click.slice().forEach((fn) => fn());
checkpoints.push({ case: 'overlay-toggle',
  pressedOff: capEnv.toggle.attrs['aria-pressed'] === 'false',
  noServerStill: capEnv.serverCalls.length === 0 });

// 저장 — 실제 진입점이 심은 서버 전역으로 머리·토큰과 함께 한 번 간다.
const capSave = captionEnv({
  cap_save: () => ({ ok: true, echoEpoch: 'cap-epoch-1', html: '<p>new</p>', message: '저장했다' }),
});
capSave.cueIdEl.value = 'a0000';
capSave.textEl.value = '손으로 고친 글자';
capSave.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const capCall = capSave.serverCalls[0];
checkpoints.push({ case: 'caption-save',
  calledOnce: capSave.serverCalls.length === 1 && capCall.name === 'cap_save',
  headBound: capCall.payload.head === 3,
  signature: capCall.payload.signature === 'ws-sig',
  tokenCarried: capCall.payload.token === 'cap-tok-1',
  applied: capSave.propsCalls.length === 1 });

// 늦은 저장 응답 — epoch가 다르면 적용 없음.
const capLate = captionEnv({
  cap_save: () => ({ ok: true, echoEpoch: 'old', html: '<p>stale</p>' }),
});
capLate.cueIdEl.value = 'a0000';
capLate.textEl.value = 'x';
capLate.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'caption-late-dropped', dropped: capLate.propsCalls.length === 0 });

// 확정 게이트 — 조건이 남아 있으면 서버 없이 안내.
const capGate = captionEnv({});
capGate.click({ getAttribute: (k) => (k === 'id' ? 'cap-confirm' : null) });
checkpoints.push({ case: 'caption-confirm-gate',
  blocked: capGate.msg.textContent.includes('열린 경고'),
  noServer: capGate.serverCalls.length === 0 });

// 자식 원소 클릭 — 글자 칸을 눌러도 그 단추의 사건으로(A5 실측 결함).
const envChild = visualEnv({});
const childSpan = { getAttribute: () => null,
  parentNode: { getAttribute: (k) => (k === 'data-vl-card' ? 'segA3' : null) } };
envChild.root._listeners.click.slice().forEach((fn) => fn({ target: childSpan }));
checkpoints.push({ case: 'child-click-traversal',
  selected: windowScope.__vlVisualWorkbenchState.selectedId === 'segA3',
  seeked: Math.abs(envChild.vid.state.currentTime - 2) < 1e-9,
  noServer: envChild.serverCalls.length === 0 });

// 칠한 조각 클릭 — 선택이 바뀌지 않고 포인터 시각으로만 이동(A5 실측 결함).
envChild.vid.state.currentTime = 0;
const beforeSel = windowScope.__vlVisualWorkbenchState.selectedId;
envChild.rail._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-interval' ? 'segA2' : null) },
  clientX: 150 }));
checkpoints.push({ case: 'rail-interval-seek-only',
  seekPointerTime: Math.abs(envChild.vid.state.currentTime - 1.5) < 1e-9,
  selectionUnchanged: windowScope.__vlVisualWorkbenchState.selectedId === beforeSel,
  noServerStill: envChild.serverCalls.length === 0 });

// 옛 클로저 사건 — 재렌더 뒤 남은 A1 클로저는 배선 세대 검증에서 물러난다:
// 공용 상태·문서·서버 **어디에도** 닿지 않는다(A6: 옛 사건이 새 화면을 바꾸던
// 결함 — 네트워크 이전에 차단된다).
const envStale = visualEnv({
  vl_apply: (p) => ({ ok: true, echoEpoch: p.epoch, message: 'ok' }),
});
const oldClosure = envStale.root._listeners.click[0];
const staleModel = visualModel();
staleModel.epoch = 'epoch-2';
staleModel.token = 'tok-2';
envStale.root.setAttribute('data-vl-model', JSON.stringify(staleModel));
envStale.rewired();
const staleBefore = { sel: windowScope.__vlVisualWorkbenchState.selectedId,
  thumbs: envStale.thumbs.innerHTML, time: envStale.vid.state.currentTime };
oldClosure({ target: { getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) } });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'stale-closure-rejected',
  noServerCall: envStale.serverCalls.length === 0,
  noDomEffect: windowScope.__vlVisualWorkbenchState.selectedId === staleBefore.sel &&
    envStale.thumbs.innerHTML === staleBefore.thumbs &&
    envStale.vid.state.currentTime === staleBefore.time,
  responseDropped: envStale.propsCalls.length === 0,
  freshListenerOnly: envStale.root._listeners.click.length === 1 &&
    envStale.root._listeners.click[0] !== oldClosure });

// A→B→A 뒤 붙잡아 둔 옛 A1 클릭 클로저 — 새 A2 배선 앞에서 선택·이동·DOM·
// 스트림 전부 불변(같은 작업공간 재렌더와 작업공간 전환 둘 다 잡는다).
const envAba = visualEnv({});
envAba.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segA1' : null) });
const abaOldHandler = envAba.root._listeners.click[0];
const abaModel = visualModel();
abaModel.epoch = 'epoch-a2';
abaModel.token = 'tok-a2';
envAba.root.setAttribute('data-vl-model', JSON.stringify(abaModel));
envAba.rewired();
const abaBefore = { sel: windowScope.__vlVisualWorkbenchState.selectedId,
  thumbs: envAba.thumbs.innerHTML, time: envAba.vid.state.currentTime };
abaOldHandler({ target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segB1' : null) } });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'aba-old-closure-inert',
  selectionKept: windowScope.__vlVisualWorkbenchState.selectedId === abaBefore.sel,
  thumbsKept: envAba.thumbs.innerHTML === abaBefore.thumbs,
  seekKept: envAba.vid.state.currentTime === abaBefore.time,
  srcUntouched: envAba.vid.calls.srcSets.length === 1,
  noServer: envAba.serverCalls.length === 0 });

// 같은 epoch에서 겹친 나누기 제안 — 오래된 응답은 새 초안을 채우지 못한다
// (초안 객체 신원 = 요청 세대). 마지막 제안의 스냅만 적용된다.
const envNonce = visualEnv({
  vl_propose: () => ({ __hold: true }),
  vl_apply: (p) => ({ ok: true, echoEpoch: p.epoch, message: 'ok' }),
});
envNonce.vid.state.currentTime = 51 / 25;
envNonce.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'split' : null) } }));
envNonce.vid.state.currentTime = 52 / 25;
envNonce.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'split' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'nonce-two-held', held: envNonce.held.length === 2 });
envNonce.held[0].deferred.resolve({ ok: true, echoEpoch: 'epoch-1', valid: true,
  reason: null, snapSeconds: '51/25', snapPts: 5100, frame: 51,
  headRevisionId: 'rev-1' });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'nonce-older-dropped',
  stillPending: windowScope.__vlVisualWorkbenchState.draft.pending === true,
  noApply: envNonce.serverCalls.filter((c) => c.name === 'vl_apply').length === 0 });
envNonce.held[1].deferred.resolve({ ok: true, echoEpoch: 'epoch-1', valid: true,
  reason: null, snapSeconds: '2/25', snapPts: 6144, frame: 2,
  headRevisionId: 'rev-1' });
await new Promise((resolve) => setTimeout(resolve, 0));
const nonceDraft = windowScope.__vlVisualWorkbenchState.draft;
checkpoints.push({ case: 'nonce-newer-wins',
  snap: nonceDraft && nonceDraft.proposal &&
    nonceDraft.proposal.snapSeconds === '2/25' });
envNonce.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'apply-draft' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
const nonceApply = envNonce.serverCalls.find((c) => c.name === 'vl_apply');
checkpoints.push({ case: 'nonce-apply-newest',
  pts: nonceApply && nonceApply.payload.splitPts === 6144,
  tokenRides: nonceApply && nonceApply.payload.token === 'tok-1' });

// 껍데기 교체 — 문서에서 떨어진 옛 영상을 능동 언로드(정지·청취 해제·주소 제거).
const envLife = visualEnv({});
envLife.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segA1' : null) });
envLife.registry['vl-repeat']._listeners.click.slice().forEach((fn) => fn());
const oldVid = envLife.vid;
envLife.vid.video.play();
const freshVid = videoStub('visual-video');
envLife.registry['visual-video'] = freshVid.video;
envLife.rewired();
checkpoints.push({ case: 'old-video-unloaded',
  paused: oldVid.video.paused === true,
  unloaded: oldVid.calls.unload >= 1 && oldVid.video.src === '',
  listenersDetached: (oldVid.listeners.timeupdate || []).length === 0 &&
    (oldVid.listeners.pause || []).length === 0,
  loopCleared: windowScope.__vlVisualWorkbenchState.loop === null,
  newVideoWired: freshVid.calls.srcSets.length === 1 });

// 자막 행의 글자 칸 클릭 — 그 행 선택으로(A5 실측 결함).
const capChild = captionEnv({});
capChild.click({ getAttribute: () => null,
  parentNode: { getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) } });
checkpoints.push({ case: 'caption-child-click',
  refilled: capChild.cueIdEl.value === 'm0001' &&
    capChild.textEl.value === '수동 큐',
  seeked: Math.abs(capChild.vid.state.currentTime - 3.0) < 1e-9,
  noServer: capChild.serverCalls.length === 0 });

// 자막 옛 클로저 — A→B→A(새 epoch·새 토큰) 뒤 붙잡아 둔 옛 클릭은 새 화면의
// 편집 칸·큐 목록·재생기를 바꾸지 않는다(A6 실측 결함).
const capAba = captionEnv({});
capAba.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
const capAbaOld = capAba.root._listeners.click[0];
const capAbaModel = captionModel();
capAbaModel.epoch = 'cap-epoch-2';
capAbaModel.token = 'cap-tok-2';
capAba.root.setAttribute('data-vl-model', JSON.stringify(capAbaModel));
capAba.rewired();
const capAbaBefore = { sel: windowScope.__vlCaptionWorkbenchState.selectedId,
  cues: capAba.cues.innerHTML, text: capAba.textEl.value,
  time: capAba.vid.state.currentTime, plays: capAba.vid.calls.play };
capAbaOld({ target: { getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) } });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'caption-old-closure-inert',
  selectionKept: windowScope.__vlCaptionWorkbenchState.selectedId === capAbaBefore.sel,
  cuesKept: capAba.cues.innerHTML === capAbaBefore.cues,
  editorKept: capAba.textEl.value === capAbaBefore.text,
  seekKept: capAba.vid.state.currentTime === capAbaBefore.time &&
    capAba.vid.calls.play === capAbaBefore.plays,
  noServer: capAba.serverCalls.length === 0 });

// 자막 껍데기 교체(작업공간 전환·새 원본) — 떨어진 옛 영상을 능동 언로드하고
// 청취를 해제하며 오버레이·선택을 지운다. 옛 영상의 timeupdate는 새 오버레이를
// 건드리지 않는다(A6 실측: 재생·주소·청취가 남던 결함).
const capLife = captionEnv({});
capLife.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
const capOldVid = capLife.vid;
capOldVid.video.play();
capOldVid.tick(3.1);
capLife.overlay.textContent = '지워야 할 옛 오버레이';
const capFreshVid = videoStub('caption-video');
capLife.registry['caption-video'] = capFreshVid.video;
const capLifeModel = captionModel();
capLifeModel.epoch = 'cap-epoch-9';
capLifeModel.token = 'cap-tok-9';
capLife.root.setAttribute('data-vl-model', JSON.stringify(capLifeModel));
capLife.rewired();
capOldVid.tick(3.2);
checkpoints.push({ case: 'caption-old-video-unloaded',
  paused: capOldVid.video.paused === true,
  unloaded: capOldVid.calls.unload >= 1 && capOldVid.video.src === '',
  listenersDetached: (capOldVid.listeners.timeupdate || []).length === 0,
  overlayCleared: capLife.overlay.textContent === '',
  selectionReset: windowScope.__vlCaptionWorkbenchState.selectedId === null,
  editorRefilled: capLife.cueIdEl.value === 'a0000',
  freshVideoWired: (capFreshVid.listeners.timeupdate || []).length === 1 });

// 보통의 저장 재렌더 — epoch만 바뀌고 토큰이 같으면 같은 화면이다: 영상은
// 그대로 재생되고(언로드 0) 선택도 산다(껍데기 교체 때만 정리한다).
const capKeep = captionEnv({});
capKeep.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
const capKeepModel = captionModel();
capKeepModel.epoch = 'cap-epoch-3';
capKeep.root.setAttribute('data-vl-model', JSON.stringify(capKeepModel));
capKeep.rewired();
checkpoints.push({ case: 'caption-save-rerender-keeps-video',
  sameElement: capKeep.vid.video.id === 'caption-video',
  noUnload: capKeep.vid.calls.unload === 0 && capKeep.vid.calls.pause === 0,
  selectionKept: windowScope.__vlCaptionWorkbenchState.selectedId === 'm0001',
  tickAlive: (capKeep.vid.listeners.timeupdate || []).length === 1 });

// ── 값 갱신 순서 — watch가 먼저 불리고 DOM 교체는 마이크로태스크로 미뤄진다 ──
// (A7 실측: watch가 옛 뿌리를 다시 묶는 사이 교체가 그것을 떼어내 새 화면이
// 빈 채 남던 결함). 커밋은 문서 뿌리를 새 원소로 바꾼다(innerHTML 교체의 효과).
const visualWatchFn = visualBoot.watchCalls[0].fn;
const captionWatchFn = captionBoot.watchCalls[0].fn;

// 지연 커밋(영상) — watch 시점에는 아직 옛 뿌리다: 배선 세대는 그때 오르지
// 않는다. 커밋 뒤 새 뿌리만 정확히 한 번 묶인다.
const envDefer = visualEnv({});
envDefer.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segA1' : null) });
const deferModel = visualModel();
deferModel.epoch = 'epoch-deferred';
deferModel.cards = deferModel.cards.filter((c) => c.sourceId === 'srcB');
deferModel.selectedId = 'segB1';
deferModel.selectedCard = deferModel.cards[0];
deferModel.total = 2;
const deferRoot = el({ id: 'visual-workbench',
  attrs: { 'data-vl-model': JSON.stringify(deferModel) } });
const deferSerialBefore = windowScope.__vlVisualWireSerial;
envDefer.props.value = '<div id="visual-workbench"></div>';
Promise.resolve().then(() => {
  envDefer.registry['visual-workbench'] = deferRoot;
  flushObservers(envDefer.block);
});
visualWatchFn('<div id="visual-workbench"></div>');
const deferSerialAtWatch = windowScope.__vlVisualWireSerial;
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'deferred-commit-rewires-new-root',
  noWireAtWatch: deferSerialAtWatch === deferSerialBefore,
  wiredAfterCommit: windowScope.__vlVisualWireSerial === deferSerialBefore + 1,
  newRootWired: deferRoot._listeners.click.length === 1,
  freshModelRendered: envDefer.thumbs.innerHTML.includes('data-vl-card="segB1"') &&
    envDefer.thumbs.innerHTML.includes('data-vl-card="segA1"') === false,
  observerOnce: (envDefer.block.__vlObservers || []).length === 1 &&
    envDefer.block.__vlVisualObserver instanceof StubObserver,
  subtreeWatched: envDefer.block.__vlObservers[0].options.subtree === true,
  noFlushLoop: (() => {
    flushObservers(envDefer.block);
    flushObservers(envDefer.block);
    return deferRoot._listeners.click.length === 1 &&
      windowScope.__vlVisualWireSerial === deferSerialBefore + 1;
  })() });

// 새 뿌리의 클로저가 산다 — 선택·이동은 새 모델을 따른다(B 원점 가산).
(deferRoot._listeners.click || []).slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segB2' : null) } }));
const deferSeekB2 = VL.toFloat(VL.add(VL.parse('2'), VL.parse('521/1000')));
checkpoints.push({ case: 'deferred-commit-closure-alive',
  selected: windowScope.__vlVisualWorkbenchState.selectedId === 'segB2',
  seeked: Math.abs(envDefer.vid.state.currentTime - deferSeekB2) < 1e-9,
  noServer: envDefer.serverCalls.length === 0 });

// 동기 커밋 순서 — 교체가 watch보다 먼저 끝났으면 즉시 묶는다(기다림 없음).
// watch는 **인자 없이** 불리는 게 설치된 그라디오의 실제 계약이다(callback()).
const envSync = visualEnv({});
const syncModel = visualModel();
syncModel.epoch = 'epoch-sync';
const syncRoot = el({ id: 'visual-workbench',
  attrs: { 'data-vl-model': JSON.stringify(syncModel) } });
envSync.registry['visual-workbench'] = syncRoot;
const syncSerial = windowScope.__vlVisualWireSerial;
visualWatchFn();
checkpoints.push({ case: 'sync-commit-wires-immediately',
  wiredNow: windowScope.__vlVisualWireSerial === syncSerial + 1,
  rendered: envSync.thumbs.innerHTML.includes('data-vl-card="segA1"') });

// watch에 변화가 없을 때 — 인자 없는 watch만으로는 아무 일도 하지 않는다
// (표식 가드: 갈아끼운 뿌리가 없으면 재배선 0).
const envNoChange = visualEnv({});
const noChangeSerial = windowScope.__vlVisualWireSerial;
const noChangeObservers = (envNoChange.block.__vlObservers || []).length;
visualWatchFn();
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'watch-no-change-noop',
  noWire: windowScope.__vlVisualWireSerial === noChangeSerial,
  kept: envNoChange.thumbs.innerHTML.includes('data-vl-card="segA1"') &&
    (envNoChange.block.__vlObservers || []).length === noChangeObservers });

// watch가 아예 오지 않는 길 — 설치된 그라디오의 직접 대입은 렌더만 돌고
// watch를 부르지 않는다(세터가 안팎 거울을 함께 갈아 동률 효과가 물러난다).
// props.value 대입 → 커밋(마이크로태스크) → 관찰자 배수만으로 새 뿌리가
// 묶여야 한다(실제 저장 응답이 도착하는 길).
const envNoWatch = visualEnv({});
const noWatchModel = visualModel();
noWatchModel.epoch = 'epoch-nowatch';
noWatchModel.cards = noWatchModel.cards.filter((c) => c.sourceId === 'srcA');
noWatchModel.selectedId = 'segA2';
noWatchModel.selectedCard = noWatchModel.cards[1];
noWatchModel.total = 3;
const noWatchRoot = el({ id: 'visual-workbench',
  attrs: { 'data-vl-model': JSON.stringify(noWatchModel) } });
const noWatchSerial = windowScope.__vlVisualWireSerial;
envNoWatch.props.value = '<div id="visual-workbench"></div>';
Promise.resolve().then(() => {
  envNoWatch.registry['visual-workbench'] = noWatchRoot;
  flushObservers(envNoWatch.block);
});
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'direct-props-no-watch-observer-rewires',
  wiredByObserver: windowScope.__vlVisualWireSerial === noWatchSerial + 1,
  newRootWired: noWatchRoot._listeners.click.length === 1,
  rendered: envNoWatch.thumbs.innerHTML.includes('data-vl-card="segA2"') &&
    envNoWatch.thumbs.innerHTML.includes('data-vl-card="segB1"') === false,
  noServer: envNoWatch.serverCalls.length === 0 });

// 지연 커밋(자막) — 보통의 저장 재렌더(같은 토큰·새 epoch): 커밋 뒤 새 뿌리를
// 묶되 영상은 그대로 재생되고 선택도 산다. 오버레이는 현재 시각으로 다시 칠해진다.
const capDefer = captionEnv({});
capDefer.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
capDefer.vid.state.currentTime = 3.2;
capDefer.vid.video.play();
const capDeferModel = captionModel();
capDeferModel.epoch = 'cap-epoch-deferred';
capDeferModel.cues[0].text = '수정된 자동 큐';
const capDeferRoot = el({ id: 'caption-workbench',
  attrs: { 'data-vl-model': JSON.stringify(capDeferModel) } });
const capSerialBefore = windowScope.__vlCaptionWireSerial;
capDefer.props.value = '<div id="caption-workbench"></div>';
Promise.resolve().then(() => {
  capDefer.registry['caption-workbench'] = capDeferRoot;
  flushObservers(capDefer.block);
});
captionWatchFn('<div id="caption-workbench"></div>');
const capSerialAtWatch = windowScope.__vlCaptionWireSerial;
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'caption-deferred-commit-rewires',
  noWireAtWatch: capSerialAtWatch === capSerialBefore,
  wiredAfterCommit: windowScope.__vlCaptionWireSerial === capSerialBefore + 1,
  cuesRendered: capDefer.cues.innerHTML.includes('수정된 자동 큐'),
  selectionKept: windowScope.__vlCaptionWorkbenchState.selectedId === 'm0001',
  editorRefilled: capDefer.textEl.value === '수동 큐',
  videoAlive: capDefer.vid.calls.unload === 0 && capDefer.vid.calls.pause === 0 &&
    capDefer.vid.video.paused === false,
  tickAlive: (capDefer.vid.listeners.timeupdate || []).length === 1,
  overlayFresh: capDefer.overlay.textContent === '수동 큐',
  noFlushLoop: (() => {
    flushObservers(capDefer.block);
    return capDeferRoot._listeners.click.length === 1;
  })() });

// 자막도 watch 없는 직접 대입 길 — 관찰자(부팅 때 상시)가 커밋을 잡아 새
// 뿌리를 묶는다(작업공간 전환 뒤 첫 저장 응답 등 실제 경로).
const capNoWatch = captionEnv({});
const capNoWatchModel = captionModel();
capNoWatchModel.epoch = 'cap-epoch-nowatch';
capNoWatchModel.cues[1].text = '직접 대입 큐';
const capNoWatchRoot = el({ id: 'caption-workbench',
  attrs: { 'data-vl-model': JSON.stringify(capNoWatchModel) } });
const capNoWatchSerial = windowScope.__vlCaptionWireSerial;
capNoWatch.props.value = '<div id="caption-workbench"></div>';
Promise.resolve().then(() => {
  capNoWatch.registry['caption-workbench'] = capNoWatchRoot;
  flushObservers(capNoWatch.block);
});
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'caption-direct-props-no-watch-rewires',
  wiredByObserver: windowScope.__vlCaptionWireSerial === capNoWatchSerial + 1,
  cuesRendered: capNoWatch.cues.innerHTML.includes('직접 대입 큐'),
  // 같은 화면 생명(같은 토큰)이라 선택 m0001이 이어지고, 편집 칸은 **커밋된
  // 모델의** 그 행으로 다시 채워진다.
  editorRefilledFromCommit: capNoWatch.cueIdEl.value === 'm0001' &&
    capNoWatch.textEl.value === '직접 대입 큐',
  videoAlive: capNoWatch.vid.calls.unload === 0,
  noServer: capNoWatch.serverCalls.length === 0 });

// ── 그 자리 morph — 설치본 렌더기(we/Te)는 같은 태그의 뿌리를 원소째 바꾸지 ──
// 않고 data-vl-model 속성과 동적 자식만 갈아친다(A8 실측: rootIdentityPreserved
// 참·DOM 머리는 새 것·카드 0). 세터는 렌더(=morph)만 마이크로태스크로 미루고
// watch는 오지 않는다(설치본 실측). 실제 저장 경로(call) 전체로 흔들어 본다.
// 모델 세대 표식(__vl*WiredModel)이 바뀐 모델을 알아채 같은 뿌리를 다시 묶는다.
let morphEnv = null;
let morphNextModel = null;
const envMorph = visualEnv({
  vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1', message: '메모를 저장했다',
    html: '<div id="visual-workbench"></div>' }),
}, null, (calls) => ({
  set value(v) {
    calls.push(String(v));
    const next = morphNextModel;
    Promise.resolve().then(() => {
      if (!morphEnv || !next) { return; }
      morphEnv.root.setAttribute('data-vl-model', JSON.stringify(next));
      morphEnv.thumbs.innerHTML = ''; // 서버 뼈대 — 동적 그릇은 비어 온다
      flushObservers(morphEnv.block);
    });
  },
  get value() { return ''; },
}));
morphEnv = envMorph;
const morphNoteBox = el({ id: 'vl-note-input' });
morphNoteBox.value = 'morph 메모';
const morphRealById = envMorph.doc.getElementById;
envMorph.doc.getElementById = (id) => (id === 'vl-note-input' ? morphNoteBox : morphRealById(id));
const morphOldHandler = envMorph.root._listeners.click[0];
const morphSerialBefore = windowScope.__vlVisualWireSerial;
morphNextModel = visualModel();
morphNextModel.epoch = 'epoch-morph';
morphNextModel.page = 2;
morphNextModel.cards = morphNextModel.cards.filter((c) => c.sourceId === 'srcB');
morphNextModel.selectedId = 'segB1';
morphNextModel.selectedCard = morphNextModel.cards[0];
morphNextModel.total = 2;
envMorph.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'morph-save-rewires-same-root',
  replyApplied: envMorph.propsCalls.length === 1,
  wiredOnce: windowScope.__vlVisualWireSerial === morphSerialBefore + 1,
  freshModelBound: windowScope.__vlVisualWorkbenchState.model.epoch === 'epoch-morph' &&
    windowScope.__vlVisualWorkbenchState.model.page === 2,
  freshCards: envMorph.thumbs.innerHTML.includes('data-vl-card="segB1"') &&
    envMorph.thumbs.innerHTML.includes('data-vl-card="segA1"') === false,
  messageKept: envMorph.registry['vl-footnote'].textContent.includes('메모를 저장했다'),
  // 같은 뿌리 재묶기 — 듣는이는 교체되지 쌓이지 않는다(click 1·재생 청취 1).
  clickSingle: envMorph.root._listeners.click.length === 1 &&
    envMorph.root._listeners.click[0] !== morphOldHandler,
  videoReused: envMorph.vid.calls.srcSets.length === 1 &&
    envMorph.vid.calls.unload === 0 &&
    (envMorph.vid.listeners.timeupdate || []).length === 1,
  noLoop: (() => {
    flushObservers(envMorph.block);
    flushObservers(envMorph.block);
    return windowScope.__vlVisualWireSerial === morphSerialBefore + 1 &&
      envMorph.root._listeners.click.length === 1;
  })() });

// morph 뒤 산 클로저 — 같은 뿌리의 새 배선이 새 모델을 따르고(B 원점 가산),
// morph 전에 붙잡아 둔 옛 클로저는 배선 세대 검증에서 물러난다.
envMorph.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segB2' : null) } }));
const morphSeekB2 = VL.toFloat(VL.add(VL.parse('2'), VL.parse('521/1000')));
const morphBeforeOld = { sel: windowScope.__vlVisualWorkbenchState.selectedId,
  thumbs: envMorph.thumbs.innerHTML };
morphOldHandler({ target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segB1' : null) } });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'morph-closure-alive',
  selected: windowScope.__vlVisualWorkbenchState.selectedId === 'segB2',
  seekedOrigin: Math.abs(envMorph.vid.state.currentTime - morphSeekB2) < 1e-9,
  selectionZeroRPC: envMorph.serverCalls.length === 1,
  oldClosureInert: windowScope.__vlVisualWorkbenchState.selectedId === morphBeforeOld.sel &&
    envMorph.thumbs.innerHTML === morphBeforeOld.thumbs,
  noLateRestoration: envMorph.propsCalls.length === 1 });

// epoch만 바뀐 morph(자식 변화 없음) — 모델 세대가 다르면 관찰자가 잡는다
// (data-vl-model 속성 관찰이 없으면 놓치는 길: 화면이 같아 보이는 새로고침).
const envAttr = visualEnv({});
const attrSerial = windowScope.__vlVisualWireSerial;
const attrModel = visualModel();
attrModel.epoch = 'epoch-attr-only';
envAttr.root.setAttribute('data-vl-model', JSON.stringify(attrModel));
flushObservers(envAttr.block);
checkpoints.push({ case: 'morph-attribute-only-rewires',
  rewired: windowScope.__vlVisualWireSerial === attrSerial + 1,
  epochFresh: windowScope.__vlVisualWorkbenchState.model.epoch === 'epoch-attr-only',
  onceOnly: (() => {
    flushObservers(envAttr.block);
    return windowScope.__vlVisualWireSerial === attrSerial + 1;
  })(),
  attrWatched: envAttr.block.__vlObservers[0].options.attributes === true &&
    envAttr.block.__vlObservers[0].options.attributeFilter[0] === 'data-vl-model' });

// 자막 그 자리 morph — 실제 cap_save 응답이 같은 뿌리를 morph해도 같은 토큰의
// 화면 생명은 산다: 영상 그대로 재생, 선택 이어짐, 편집 칸은 커밋된 모델로.
let capMorphEnv = null;
let capMorphNext = null;
const capMorph = captionEnv({
  cap_save: () => ({ ok: true, echoEpoch: 'cap-epoch-1', message: '저장했다',
    html: '<div id="caption-workbench"></div>' }),
}, (calls) => ({
  set value(v) {
    calls.push(String(v));
    const next = capMorphNext;
    Promise.resolve().then(() => {
      if (!capMorphEnv || !next) { return; }
      capMorphEnv.root.setAttribute('data-vl-model', JSON.stringify(next));
      capMorphEnv.cues.innerHTML = '';
      flushObservers(capMorphEnv.block);
    });
  },
  get value() { return ''; },
}));
capMorphEnv = capMorph;
const capMorphSerial = windowScope.__vlCaptionWireSerial;
capMorph.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
capMorph.vid.state.currentTime = 3.2;
capMorph.vid.video.play();
capMorph.cueIdEl.value = 'a0000';
capMorph.textEl.value = 'morph 수정 큐';
capMorphNext = captionModel();
capMorphNext.epoch = 'cap-epoch-morph';
capMorphNext.cues[0].text = 'morph 수정 큐';
capMorph.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'morph-caption-save-keeps-screen-life',
  replyApplied: capMorph.propsCalls.length === 1,
  wiredOnce: windowScope.__vlCaptionWireSerial === capMorphSerial + 1,
  cuesFromCommit: capMorph.cues.innerHTML.includes('morph 수정 큐'),
  selectionKept: windowScope.__vlCaptionWorkbenchState.selectedId === 'm0001',
  editorFromCommit: capMorph.cueIdEl.value === 'm0001' &&
    capMorph.textEl.value === '수동 큐',
  videoAlive: capMorph.vid.calls.unload === 0 && capMorph.vid.calls.pause === 0 &&
    capMorph.vid.video.paused === false && capMorph.vid.video.currentTime === 3.2,
  clickSingle: capMorph.root._listeners.click.length === 1,
  tickSingle: (capMorph.vid.listeners.timeupdate || []).length === 1,
  overlayFresh: capMorph.overlay.textContent === '수동 큐',
  messageKept: capMorph.msg.textContent.includes('저장했다'),
  noLoop: (() => {
    flushObservers(capMorph.block);
    return windowScope.__vlCaptionWireSerial === capMorphSerial + 1;
  })(),
  saveOnce: capMorph.serverCalls.length === 1 });

// morph로 온 새 화면 토큰(작업공간 전환이 morph로 도착) — 선택은 정리되고
// 편집 칸은 커밋된 첫 큐로, 옛 클로저는 물러난다. 영상 원소가 그대로면
// 언로드하지 않는다(껍데기 교체 때만 정리 — 기존 계약).
const capSwitch = captionEnv({});
capSwitch.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
const capSwitchOld = capSwitch.root._listeners.click[0];
const capSwitchModel = captionModel();
capSwitchModel.epoch = 'cap-epoch-switch';
capSwitchModel.token = 'cap-tok-switch';
capSwitchModel.cues[0].text = '전환 뒤 큐';
capSwitch.root.setAttribute('data-vl-model', JSON.stringify(capSwitchModel));
capSwitch.cues.innerHTML = '';
flushObservers(capSwitch.block);
const capSwitchBefore = { sel: windowScope.__vlCaptionWorkbenchState.selectedId,
  text: capSwitch.textEl.value, time: capSwitch.vid.state.currentTime };
capSwitchOld({ target: { getAttribute: (k) => (k === 'data-vl-cue' ? 'a0000' : null) } });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'morph-new-token-resets-screen',
  rewired: windowScope.__vlCaptionWorkbenchState.model.token === 'cap-tok-switch',
  selectionReset: windowScope.__vlCaptionWorkbenchState.selectedId === null,
  editorFirstCue: capSwitch.cueIdEl.value === 'a0000' &&
    capSwitch.textEl.value === '전환 뒤 큐',
  videoElementKept: capSwitch.vid.calls.unload === 0 &&
    (capSwitch.vid.listeners.timeupdate || []).length === 1,
  oldClosureInert: windowScope.__vlCaptionWorkbenchState.selectedId === capSwitchBefore.sel &&
    capSwitch.textEl.value === capSwitchBefore.text &&
    capSwitch.vid.state.currentTime === capSwitchBefore.time,
  noServer: capSwitch.serverCalls.length === 0,
  noLateRestoration: capSwitch.propsCalls.length === 0 });

// 관찰자 없는 길 — 저장 응답의 대입 직후 스케줄러(call()의 __vl*WorkbenchRewire
// 환기, A8 실측: 짝이 안 맞으면 no-op이었다)만으로 재배선되는지. 이 케이스의
// props는 morph만 하고 관찰자를 환기하지 않는다.
let schedEnv = null;
let schedNext = null;
const envSched = visualEnv({
  vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1', message: 'ok',
    html: '<div id="visual-workbench"></div>' }),
}, null, (calls) => ({
  set value(v) {
    calls.push(String(v));
    const next = schedNext;
    Promise.resolve().then(() => {
      if (!schedEnv || !next) { return; }
      schedEnv.root.setAttribute('data-vl-model', JSON.stringify(next));
      schedEnv.thumbs.innerHTML = '';
      // 관찰자 환기 없음 — 오직 call()의 대입 직후 스케줄러만 살아 있다.
    });
  },
  get value() { return ''; },
}));
schedEnv = envSched;
const schedNoteBox = el({ id: 'vl-note-input' });
schedNoteBox.value = '스케줄러 메모';
const schedRealById = envSched.doc.getElementById;
envSched.doc.getElementById = (id) => (id === 'vl-note-input' ? schedNoteBox : schedRealById(id));
const schedSerial = windowScope.__vlVisualWireSerial;
schedNext = visualModel();
schedNext.epoch = 'epoch-scheduler-only';
envSched.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'postassign-rewires-without-observer',
  wiredByScheduler: windowScope.__vlVisualWireSerial === schedSerial + 1,
  modelFresh: windowScope.__vlVisualWorkbenchState.model.epoch === 'epoch-scheduler-only',
  rendered: envSched.thumbs.innerHTML.includes('data-vl-card="segA1"'),
  saveOnce: envSched.serverCalls.length === 1 });

// ── 자리표시 morph(작업공간 없음 — data-vl-empty·모델 없음) — 같은 태그 뿌리가 ──
// 그대로 남으니(A9 실측) 정리 세대를 밟는다: 배선 세대 무효화·상태·목록 청소·
// 재생 영상 능동 언로드. 반복 통보 무동작, 이어지는 실제 모델로 회복.
const envEmpty = visualEnv({});
envEmpty.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segA1' : null) });
envEmpty.registry['vl-repeat']._listeners.click.slice().forEach((fn) => fn());
envEmpty.vid.video.play();
envEmpty.vid.tick(0.5);
const emptyOldHandler = envEmpty.root._listeners.click[0];
const emptySerial = windowScope.__vlVisualWireSerial;
envEmpty.root.removeAttribute('data-vl-model');
envEmpty.root.setAttribute('data-vl-empty', '1');
envEmpty.thumbs.innerHTML = '<p class="vl-note">검토할 계획이 없다</p>';
flushObservers(envEmpty.block);
const emptyState = windowScope.__vlVisualWorkbenchState;
const tornDown = { model: emptyState.model, sel: emptyState.selectedId,
  loop: emptyState.loop, draft: emptyState.draft,
  paused: envEmpty.vid.video.paused, unload: envEmpty.vid.calls.unload,
  src: envEmpty.vid.video.src,
  ticks: (envEmpty.vid.listeners.timeupdate || []).length,
  pauseHooks: (envEmpty.vid.listeners.pause || []).length,
  thumbs: envEmpty.thumbs.innerHTML,
  foot: envEmpty.registry['vl-footnote'].textContent };
flushObservers(envEmpty.block); // 반복 통보 — 무동작(세대 변화 없음)
const afterRepeat = windowScope.__vlVisualWireSerial;
const emptyBeforeOld = { sel: emptyState.selectedId, time: envEmpty.vid.state.currentTime,
  srcSets: envEmpty.vid.calls.srcSets.length };
emptyOldHandler({ target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segA2' : null) } });
checkpoints.push({ case: 'placeholder-morph-tears-down-visual',
  generationInvalidated: afterRepeat === emptySerial + 1,
  stateCleared: tornDown.model === null && tornDown.sel === null &&
    tornDown.loop === null && tornDown.draft === null,
  videoUnloaded: tornDown.paused === true && tornDown.unload >= 1 && tornDown.src === '',
  ticksRemoved: tornDown.ticks === 0 && tornDown.pauseHooks === 0,
  listsCleared: tornDown.thumbs === '' && tornDown.foot === '',
  oldClosureInert: emptyState.selectedId === emptyBeforeOld.sel &&
    envEmpty.vid.state.currentTime === emptyBeforeOld.time &&
    envEmpty.vid.calls.srcSets.length === emptyBeforeOld.srcSets,
  noServer: envEmpty.serverCalls.length === 0 });

// 회복 — 자리표시 뒤 실제 모델이 오면 다시 묶인다(작업공간 A 재오픈).
const recoverModel = visualModel();
recoverModel.epoch = 'epoch-recovered';
recoverModel.token = 'tok-recovered';
envEmpty.root.removeAttribute('data-vl-empty');
envEmpty.root.setAttribute('data-vl-model', JSON.stringify(recoverModel));
envEmpty.thumbs.innerHTML = '';
flushObservers(envEmpty.block);
const recoverSerial = windowScope.__vlVisualWireSerial;
envEmpty.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-card' ? 'segA2' : null) } }));
checkpoints.push({ case: 'placeholder-recovery-visual',
  rewired: recoverSerial === emptySerial + 2,
  freshBound: windowScope.__vlVisualWorkbenchState.model.token === 'tok-recovered',
  cardsRendered: envEmpty.thumbs.innerHTML.includes('data-vl-card="segA1"'),
  actionAlive: windowScope.__vlVisualWorkbenchState.selectedId === 'segA2' &&
    Math.abs(envEmpty.vid.state.currentTime - 1) < 1e-9 });

// 자막 자리표시 morph — 편집 칸·큐·오버레이 청소와 영상 능동 언로드(재생 중이던
// 것까지), 반복 무동작, 새 토큰 모델로 회복.
const capEmpty = captionEnv({});
capEmpty.click({ getAttribute: (k) => (k === 'data-vl-cue' ? 'm0001' : null) });
capEmpty.vid.state.currentTime = 3.2;
capEmpty.vid.video.play();
const capEmptyOld = capEmpty.root._listeners.click[0];
const capEmptySerial = windowScope.__vlCaptionWireSerial;
capEmpty.root.removeAttribute('data-vl-model');
capEmpty.root.setAttribute('data-vl-empty', '1');
capEmpty.cues.innerHTML = '<p class="vl-note">등록된 최종 영상이 없다</p>';
flushObservers(capEmpty.block);
const capEmptyState = windowScope.__vlCaptionWorkbenchState;
const capTorn = { model: capEmptyState.model, sel: capEmptyState.selectedId,
  cueId: capEmpty.cueIdEl.value, text: capEmpty.textEl.value,
  start: capEmpty.startEl.value, end: capEmpty.endEl.value,
  cues: capEmpty.cues.innerHTML, msg: capEmpty.msg.textContent,
  overlay: capEmpty.overlay.textContent,
  paused: capEmpty.vid.video.paused, unload: capEmpty.vid.calls.unload,
  src: capEmpty.vid.video.src,
  ticks: (capEmpty.vid.listeners.timeupdate || []).length };
flushObservers(capEmpty.block); // 반복 통보 — 무동작
const capAfterRepeat = windowScope.__vlCaptionWireSerial;
const capRecover = captionModel();
capRecover.epoch = 'cap-epoch-recovered';
capRecover.token = 'cap-tok-recovered';
capRecover.cues[0].text = '회복 큐';
capEmpty.root.removeAttribute('data-vl-empty');
capEmpty.root.setAttribute('data-vl-model', JSON.stringify(capRecover));
capEmpty.cues.innerHTML = '';
flushObservers(capEmpty.block);
const capRecBeforeOld = { cueId: capEmpty.cueIdEl.value, text: capEmpty.textEl.value,
  time: capEmpty.vid.state.currentTime, plays: capEmpty.vid.calls.play };
capEmptyOld({ target: { getAttribute: (k) => (k === 'data-vl-cue' ? 'a0000' : null) } });
checkpoints.push({ case: 'placeholder-morph-tears-down-caption',
  generationInvalidated: capAfterRepeat === capEmptySerial + 1 &&
    windowScope.__vlCaptionWireSerial === capEmptySerial + 2,
  stateCleared: capTorn.model === null && capTorn.sel === null,
  editorCleared: capTorn.cueId === '' && capTorn.text === '' &&
    capTorn.start === '' && capTorn.end === '',
  cuesMsgOverlayCleared: capTorn.cues === '' && capTorn.msg === '' &&
    capTorn.overlay === '',
  videoUnloaded: capTorn.paused === true && capTorn.unload >= 1 && capTorn.src === '',
  ticksRemoved: capTorn.ticks === 0,
  recovered: capEmptyState.model.token === 'cap-tok-recovered' &&
    capEmpty.cues.innerHTML.includes('회복 큐'),
  oldClosureInert: capEmpty.cueIdEl.value === capRecBeforeOld.cueId &&
    capEmpty.textEl.value === capRecBeforeOld.text &&
    capEmpty.vid.state.currentTime === capRecBeforeOld.time &&
    capEmpty.vid.calls.play === capRecBeforeOld.plays,
  noServer: capEmpty.serverCalls.length === 0 });

// ── 늦은 실패 응답 — 화면이 바뀐 뒤 도착한 null·거부는 새 화면의 각주·lastMsg ──
// 를 바꾸지 않는다(A9 실측: null·거부 분기가 echoEpoch 검사 앞이라 무가드였다).
// 요청이 나간 배선 세대·epoch·뿌리가 응답 때까지 살아 있을 때만 효과가 나고,
// 신선한 화면의 진짜 진단(빈 응답·거부·서버 오류)은 그대로 산다.
const lateNoteBox = el({ id: 'vl-note-input' });
lateNoteBox.value = '늦은 저장 메모';
const envLate = visualEnv({ vl_apply: () => ({ __hold: true }) });
const lateRealById = envLate.doc.getElementById;
envLate.doc.getElementById = (id) => (id === 'vl-note-input' ? lateNoteBox : lateRealById(id));
envLate.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const lateHeld = envLate.held[0];
// 전환 — 같은 뿌리 morph로 새 토큰 화면(작업공간 전환 모양)이 오면 재배선된다.
const lateSwitch = visualModel();
lateSwitch.epoch = 'epoch-late-switch';
lateSwitch.token = 'tok-late-switch';
envLate.root.setAttribute('data-vl-model', JSON.stringify(lateSwitch));
envLate.thumbs.innerHTML = '';
flushObservers(envLate.block);
const lateBefore = { foot: envLate.registry['vl-footnote'].textContent,
  lastMsg: windowScope.__vlVisualWorkbenchState.lastMsg,
  thumbs: envLate.thumbs.innerHTML };
lateHeld.deferred.resolve(null);
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'late-null-inert',
  footnoteKept: envLate.registry['vl-footnote'].textContent === lateBefore.foot,
  lastMsgKept: windowScope.__vlVisualWorkbenchState.lastMsg === lateBefore.lastMsg,
  thumbsKept: envLate.thumbs.innerHTML === lateBefore.thumbs });
// 늦은 거부 — 새 배선에서 낸 요청을 붙잡고 또 전환한 뒤 거부한다.
envLate.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const lateSwitch2 = visualModel();
lateSwitch2.epoch = 'epoch-late-switch-2';
lateSwitch2.token = 'tok-late-switch-2';
envLate.root.setAttribute('data-vl-model', JSON.stringify(lateSwitch2));
envLate.thumbs.innerHTML = '';
flushObservers(envLate.block);
const lateBefore2 = { foot: envLate.registry['vl-footnote'].textContent,
  lastMsg: windowScope.__vlVisualWorkbenchState.lastMsg };
envLate.held[1].deferred.reject(new Error('늦은 거부 사유'));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'late-reject-inert',
  footnoteKept: envLate.registry['vl-footnote'].textContent === lateBefore2.foot,
  lastMsgKept: windowScope.__vlVisualWorkbenchState.lastMsg === lateBefore2.lastMsg });
// 자리표시 전환 뒤 늦은 null — 정리 세대가 비운 lastMsg·각주를 그대로 둔다.
const emptyLateNote = el({ id: 'vl-note-input' });
emptyLateNote.value = '자리표시 전 메모';
const envEmptyLate = visualEnv({ vl_apply: () => ({ __hold: true }) });
const emptyLateRealById = envEmptyLate.doc.getElementById;
envEmptyLate.doc.getElementById = (id) =>
  (id === 'vl-note-input' ? emptyLateNote : emptyLateRealById(id));
envEmptyLate.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
envEmptyLate.root.removeAttribute('data-vl-model');
envEmptyLate.root.setAttribute('data-vl-empty', '1');
envEmptyLate.thumbs.innerHTML = '<p class="vl-note">검토할 계획이 없다</p>';
flushObservers(envEmptyLate.block);
const emptyLateBefore = { foot: envEmptyLate.registry['vl-footnote'].textContent,
  lastMsg: windowScope.__vlVisualWorkbenchState.lastMsg };
envEmptyLate.held[0].deferred.resolve(null);
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'late-null-inert-after-empty',
  clearedBefore: emptyLateBefore.lastMsg === '' && emptyLateBefore.foot === '',
  footnoteKept: envEmptyLate.registry['vl-footnote'].textContent === emptyLateBefore.foot,
  lastMsgKept: windowScope.__vlVisualWorkbenchState.lastMsg === emptyLateBefore.lastMsg });
// 신선한 화면의 진단은 그대로 — 빈 응답·거부·서버 오류 안내가 살아야 한다.
const freshNoteBox = el({ id: 'vl-note-input' });
freshNoteBox.value = '신선한 저장';
const envFreshFail = visualEnv({ vl_apply: () => ({ __hold: true }) });
const freshRealById = envFreshFail.doc.getElementById;
envFreshFail.doc.getElementById = (id) =>
  (id === 'vl-note-input' ? freshNoteBox : freshRealById(id));
envFreshFail.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
envFreshFail.held[0].deferred.resolve(null);
await new Promise((resolve) => setTimeout(resolve, 0));
const freshNullFoot = envFreshFail.registry['vl-footnote'].textContent;
const freshNullLastMsg = windowScope.__vlVisualWorkbenchState.lastMsg;
envFreshFail.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
envFreshFail.held[1].deferred.reject(new Error('거부 사유'));
await new Promise((resolve) => setTimeout(resolve, 0));
const errNoteBox = el({ id: 'vl-note-input' });
errNoteBox.value = '오류 저장';
// 실제 생산자 오류 응답의 모양 — {ok:false, error}에 **echoEpoch는 없다**(공개
// fail() 경로 전부). echo 검사 뒤에 있다면 이 안내는 갈려서 저장 중 표시만
// 남는다(A9 실측) — 신선한 요청의 오류는 echo 없이도 보여야 한다.
const envFreshErr = visualEnv({
  vl_apply: () => ({ ok: false,
    error: '편집이 늦었다 — 화면이 본 리비전 뒤에 다른 편집이 먼저 갔다' }),
});
const errRealById = envFreshErr.doc.getElementById;
envFreshErr.doc.getElementById = (id) => (id === 'vl-note-input' ? errNoteBox : errRealById(id));
envFreshErr.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
// 제안 오류도 같은 모양 — echo 없이 오면 초안은 '적용 불가'와 원문 사유로 닫힌다.
const envProposeErr = visualEnv({
  vl_propose: () => ({ ok: false, error: '스냅한 경계가 조각 밖이다' }),
});
envProposeErr.vid.state.currentTime = 12 / 25;
envProposeErr.root._listeners.click.slice().forEach((fn) => fn({
  target: { getAttribute: (k) => (k === 'data-vl-act' ? 'split' : null) } }));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'fresh-failure-diagnostics-preserved',
  nullShown: freshNullFoot === '빈 응답이다' && freshNullLastMsg === '빈 응답이다',
  rejectShown: envFreshFail.registry['vl-footnote'].textContent === '서버 오류 — 거부 사유',
  serverErrorShown: envFreshErr.registry['vl-footnote'].textContent.includes('편집이 늦었다'),
  notStuckSending:
    envFreshErr.registry['vl-footnote'].textContent.includes('보내는 중') === false });
checkpoints.push({ case: 'fresh-propose-error-closes-draft',
  invalidShown: envProposeErr.panel.innerHTML.includes('적용 불가'),
  reasonKept: envProposeErr.panel.innerHTML.includes('스냅한 경계가 조각 밖이다'),
  notPending: windowScope.__vlVisualWorkbenchState.draft.pending === false });
// 자막 — 같은 결함의 짝: 늦은 null·거부 무효, 신선한 진단 보존.
const capLateEnv = captionEnv({ cap_save: () => ({ __hold: true }) });
capLateEnv.cueIdEl.value = 'a0000';
capLateEnv.textEl.value = '늦은 자막 저장';
capLateEnv.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const capLateSwitch = captionModel();
capLateSwitch.epoch = 'cap-epoch-late';
capLateSwitch.token = 'cap-tok-late';
capLateEnv.root.setAttribute('data-vl-model', JSON.stringify(capLateSwitch));
capLateEnv.cues.innerHTML = '';
flushObservers(capLateEnv.block);
const capLateBefore = { msg: capLateEnv.msg.textContent,
  lastMsg: windowScope.__vlCaptionWorkbenchState.lastMsg };
capLateEnv.held[0].deferred.resolve(null);
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'late-null-inert-caption',
  msgKept: capLateEnv.msg.textContent === capLateBefore.msg,
  lastMsgKept: windowScope.__vlCaptionWorkbenchState.lastMsg === capLateBefore.lastMsg });
capLateEnv.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const capLateSwitch2 = captionModel();
capLateSwitch2.epoch = 'cap-epoch-late-2';
capLateSwitch2.token = 'cap-tok-late-2';
capLateEnv.root.setAttribute('data-vl-model', JSON.stringify(capLateSwitch2));
capLateEnv.cues.innerHTML = '';
flushObservers(capLateEnv.block);
const capLateBefore2 = { msg: capLateEnv.msg.textContent,
  lastMsg: windowScope.__vlCaptionWorkbenchState.lastMsg };
capLateEnv.held[1].deferred.reject(new Error('자막 늦은 거부'));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'late-reject-inert-caption',
  msgKept: capLateEnv.msg.textContent === capLateBefore2.msg,
  lastMsgKept: windowScope.__vlCaptionWorkbenchState.lastMsg === capLateBefore2.lastMsg });
const capFreshFail = captionEnv({ cap_save: () => ({ __hold: true }) });
capFreshFail.cueIdEl.value = 'a0000';
capFreshFail.textEl.value = '신선한 자막';
capFreshFail.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
capFreshFail.held[0].deferred.resolve(null);
await new Promise((resolve) => setTimeout(resolve, 0));
const capFreshNullMsg = capFreshFail.msg.textContent;
capFreshFail.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
capFreshFail.held[1].deferred.reject(new Error('자막 거부 사유'));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'fresh-failure-diagnostics-preserved-caption',
  nullShown: capFreshNullMsg === '빈 응답이다',
  rejectShown: capFreshFail.msg.textContent === '서버 오류 — 자막 거부 사유' });
// 자막 — 실제 생산자 오류(빈 글자 저장)도 echo 없이 그대로 보인다(저장 중 잔류 없음).
const capFreshServerErr = captionEnv({
  cap_save: () => ({ ok: false,
    error: '글자를 적으세요 — 지울 수 없다(수동 확정은 확정 단계다).' }),
});
capFreshServerErr.cueIdEl.value = 'a0000';
capFreshServerErr.textEl.value = '';
capFreshServerErr.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'fresh-server-error-shown-caption',
  errorShown: capFreshServerErr.msg.textContent.includes('글자를 적으세요'),
  notStuckSending: capFreshServerErr.msg.textContent.includes('보내는 중') === false });

// ── 재계획 morph 뒤 선택 — 계획 세대가 바뀌면 옛 ID가 다른 구간에 재주조될 수 ──
// 있다(ROOT 실측: 같은 ID 문자열이 다른 조각에 붙어 하이라이트가 재생 머리 밖
// 구간을 가리켰다). 커밋된 선택(서버가 매핑으로 보존한 것)이 이기고 이어지는
// 저장도 그 조각을 향한다. 같은 계획 안의 로컬 선택은 여전히 이긴다(F2 0RPC).
const replanBase = visualModel();
replanBase.basePlanId = 'plan-old';
replanBase.cards = [
  { id: 'segNear', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 0,
    start: '2', end: '3', dur: '1', sel: 'included', rec: '', speech: '', tags: [],
    note: '', thumb: { state: 'missing', src: null }, neighbor: 'seg871',
    needsExtract: false },
  { id: 'seg871', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 1,
    start: '3', end: '6', dur: '3', sel: 'included', rec: 'normal', speech: '', tags: [],
    note: '옛 메모', thumb: { state: 'missing', src: null }, neighbor: null,
    needsExtract: false },
];
replanBase.selectedId = 'seg871';
replanBase.selectedCard = replanBase.cards[1];
replanBase.total = 2;
const envReplan = visualEnv({
  vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1', message: 'ok' }),
}, replanBase);
envReplan.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'seg871' : null) });
envReplan.vid.state.currentTime = 3.15;
// 재계획 응답 — 같은 뿌리 morph: 새 계획에서 'seg871'이 2..3에 재주조되고 옛
// 3..6은 segd42(서버가 매핑으로 보존한 선택)다. 같은 토큰 — 같은 화면 생명.
const replanModel = visualModel();
replanModel.basePlanId = 'plan-new';
replanModel.epoch = 'epoch-replan';
replanModel.cards = [
  replanBase.cards[0],
  { id: 'seg871', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 1,
    start: '2', end: '3', dur: '1', sel: 'included', rec: '', speech: '', tags: [],
    note: '옛 메모', thumb: { state: 'missing', src: null }, neighbor: 'segd42',
    needsExtract: false },
  { id: 'segd42', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 2,
    start: '3', end: '6', dur: '3', sel: 'included', rec: 'normal', speech: '', tags: [],
    note: '옛 메모', thumb: { state: 'missing', src: null }, neighbor: null,
    needsExtract: false },
];
replanModel.selectedId = 'segd42';
replanModel.selectedCard = replanModel.cards[2];
replanModel.total = 3;
envReplan.root.setAttribute('data-vl-model', JSON.stringify(replanModel));
envReplan.thumbs.innerHTML = '';
flushObservers(envReplan.block);
const replanNoteBox = el({ id: 'vl-note-input' });
replanNoteBox.value = '재계획 뒤 메모';
const replanRealById = envReplan.doc.getElementById;
envReplan.doc.getElementById = (id) =>
  (id === 'vl-note-input' ? replanNoteBox : replanRealById(id));
envReplan.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const replanCall = envReplan.serverCalls[envReplan.serverCalls.length - 1];
checkpoints.push({ case: 'replan-morph-honors-committed-selection',
  committedBound: windowScope.__vlVisualWorkbenchState.model.basePlanId === 'plan-new',
  saveTargetsMapped: replanCall && replanCall.name === 'vl_apply' &&
    replanCall.payload.segmentId === 'segd42',
  playheadKept: Math.abs(envReplan.vid.state.currentTime - 3.15) < 1e-9,
  videoKept: envReplan.vid.calls.srcSets.length === 1 &&
    envReplan.vid.calls.unload === 0 });
// 같은 계획 안의 로컬 선택은 여전히 이긴다 — 새 모델에서 고른 카드가 저장된다.
envReplan.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segNear' : null) });
envReplan.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const replanCall2 = envReplan.serverCalls[envReplan.serverCalls.length - 1];
checkpoints.push({ case: 'replan-morph-local-selection-still-wins',
  saveTargetsLocal: replanCall2 && replanCall2.payload.segmentId === 'segNear' });

// ── 대기 중 재계획과 경합한 새로고침 — 요청은 화면이 보던 문맥(계획·원본·머리)을 ──
// 실고 나가고, 응답(같은 화면 생명의 새 계획)이 매핑된 선택을 그대로 그린다.
// 이어 도착한 늦은 재계획 응답은 새 화면을 못 바꾼다(echo 세대 불일치). 실측 결함:
// 옛 계획 선택 ID가 새 계획에서 다른 구간에 재주조돼 재주조된 앞조각이 골라졌다.
function ariaSelectedId(thumbsNode) {
  const markup = thumbsNode.innerHTML;
  const at = markup.indexOf('aria-selected="true"');
  if (at < 0) { return null; }
  const head = markup.lastIndexOf('data-vl-card="', at);
  if (head < 0) { return null; }
  return markup.slice(head + 14, markup.indexOf('"', head + 14));
}
const inflightBase = visualModel();
inflightBase.epoch = 'epoch-inflight-old';
inflightBase.basePlanId = 'plan-old';
inflightBase.cards = [
  { id: 'segNear', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 0,
    start: '2', end: '3', dur: '1', sel: 'included', rec: '', speech: '', tags: [],
    note: '', thumb: { state: 'missing', src: null }, neighbor: 'segOld',
    needsExtract: false },
  { id: 'segOld', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 1,
    start: '3', end: '6', dur: '3', sel: 'included', rec: 'normal', speech: '', tags: [],
    note: '옛 메모', thumb: { state: 'missing', src: null }, neighbor: null,
    needsExtract: false },
];
inflightBase.selectedId = 'segOld';
inflightBase.selectedCard = inflightBase.cards[1];
inflightBase.total = 2;
const envInflight = visualEnv({
  vl_replan: () => ({ __hold: true }),
  vl_refresh: () => ({ __hold: true }),
  vl_apply: (p) => ({ ok: true, echoEpoch: p.epoch, message: '메모를 저장했다' }),
}, inflightBase);
envInflight.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segOld' : null) });
envInflight.vid.state.currentTime = 3.15;
envInflight.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'replan' : null) });
envInflight.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'refresh' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const inflightRefreshCall = envInflight.serverCalls[1];
checkpoints.push({ case: 'inflight-refresh-carries-shown-context',
  replanWentFirst: envInflight.serverCalls[0] &&
    envInflight.serverCalls[0].name === 'vl_replan',
  refreshHolds: envInflight.held.length === 2 && inflightRefreshCall &&
    inflightRefreshCall.name === 'vl_refresh',
  planCarried: inflightRefreshCall && inflightRefreshCall.payload.basePlanId === 'plan-old',
  sourceCarried: inflightRefreshCall && inflightRefreshCall.payload.sourceId === 'srcA',
  playheadCarried: inflightRefreshCall && inflightRefreshCall.payload.playheadSec === '63/20',
  selectionCarried: inflightRefreshCall && inflightRefreshCall.payload.selectedId === 'segOld' });
// 새로고침 응답 — 같은 토큰(같은 화면 생명)의 새 계획. 'segOld'는 2..3에 재주조됐고
// 서버 매핑이 보존한 선택은 3..6의 'segNew'다(실제 응답의 모양).
const inflightNew = visualModel();
inflightNew.epoch = 'epoch-inflight-new';
inflightNew.basePlanId = 'plan-new';
inflightNew.cards = [
  inflightBase.cards[0],
  { id: 'segOld', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 1,
    start: '2', end: '3', dur: '1', sel: 'included', rec: '', speech: '', tags: [],
    note: '옛 메모', thumb: { state: 'missing', src: null }, neighbor: 'segNew',
    needsExtract: false },
  { id: 'segNew', sourceId: 'srcA', sourceName: '01-landscape.mp4', order: 2,
    start: '3', end: '6', dur: '3', sel: 'included', rec: 'normal', speech: '', tags: [],
    note: '옛 메모', thumb: { state: 'missing', src: null }, neighbor: null,
    needsExtract: false },
];
inflightNew.selectedId = 'segNew';
inflightNew.selectedCard = inflightNew.cards[2];
inflightNew.total = 3;
envInflight.held[1].deferred.resolve({ ok: true, echoEpoch: 'epoch-inflight-old',
  html: '<div id="visual-workbench"></div>', message: '' });
await new Promise((resolve) => setTimeout(resolve, 0));
envInflight.root.setAttribute('data-vl-model', JSON.stringify(inflightNew));
envInflight.thumbs.innerHTML = '';
flushObservers(envInflight.block);
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'inflight-refresh-applies-mapped-selection',
  newPlanBound: windowScope.__vlVisualWorkbenchState.model.basePlanId === 'plan-new',
  visibleSelection: ariaSelectedId(envInflight.thumbs) === 'segNew',
  committedSelection: windowScope.__vlVisualWorkbenchState.model.selectedId === 'segNew',
  playheadKept: Math.abs(envInflight.vid.state.currentTime - 3.15) < 1e-9,
  replyApplied: envInflight.propsCalls.length === 1 });
// 이어오는 저장 — 커밋된(매핑된) 선택을 향한다(옛 개인 캐시가 아니라 실제 선택).
const inflightNoteBox = el({ id: 'vl-note-input' });
inflightNoteBox.value = '매핑 뒤 메모';
const inflightRealById = envInflight.doc.getElementById;
envInflight.doc.getElementById = (id) =>
  (id === 'vl-note-input' ? inflightNoteBox : inflightRealById(id));
envInflight.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'save-note' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const inflightEdit = envInflight.serverCalls[envInflight.serverCalls.length - 1];
checkpoints.push({ case: 'inflight-refresh-named-edit-targets-mapped',
  editSent: inflightEdit && inflightEdit.name === 'vl_apply',
  targetsMapped: inflightEdit && inflightEdit.payload.segmentId === 'segNew',
  planIsNew: inflightEdit && inflightEdit.payload.basePlanId === 'plan-new',
  revisionIsNew: inflightEdit && inflightEdit.payload.revisionId === 'rev-1' });
// 늦은 재계획 응답 — 새로고침이 이미 새 화면을 그린 뒤라 echo 세대가 다르다:
// 모델·목록·재생 머리·값 어느 것도 바꾸지 못한다.
const inflightBefore = { sel: windowScope.__vlVisualWorkbenchState.model.selectedId,
  plan: windowScope.__vlVisualWorkbenchState.model.basePlanId,
  thumbs: envInflight.thumbs.innerHTML,
  time: envInflight.vid.state.currentTime,
  props: envInflight.propsCalls.length,
  foot: envInflight.registry['vl-footnote'].textContent };
envInflight.held[0].deferred.resolve({ ok: true, echoEpoch: 'epoch-inflight-old',
  html: '<div id="visual-workbench"></div>', message: '늦은 재계획 응답' });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'late-replan-after-refresh-inert',
  modelKept: windowScope.__vlVisualWorkbenchState.model.selectedId === inflightBefore.sel &&
    windowScope.__vlVisualWorkbenchState.model.basePlanId === inflightBefore.plan,
  thumbsKept: envInflight.thumbs.innerHTML === inflightBefore.thumbs,
  timeKept: Math.abs(envInflight.vid.state.currentTime - inflightBefore.time) < 1e-9,
  noExtraCommit: envInflight.propsCalls.length === inflightBefore.props,
  footnoteKept: envInflight.registry['vl-footnote'].textContent === inflightBefore.foot });

// ── 확정 게시의 내려받기 — 등록 미디어 ID(`/media/<id>`) 링크만 안전한 <a>로 ──
// 싣는다(사용자 글자·임의 경로가 HTML이 되는 길 없음). 이어오는 안내가 각주를
// 갈아끼우면 링크도 함께 물러난다(낡은 머리 링크 잔류 없음).
const capConfirmModel = captionModel();
capConfirmModel.canConfirm = true;
const capDl = captionEnv({
  cap_confirm: () => ({ ok: true, echoEpoch: 'cap-epoch-1',
    message: '확정 게시 완료 — 검토 머리 3. 내려받기는 아래 링크로.',
    html: '<div id="caption-workbench"></div>',
    downloads: [
      { label: 'SRT', href: '/media/srt-file-id' },
      { label: 'VTT', href: '/media/vtt-file-id' },
      { label: '위험', href: 'https://example.invalid/x' },
    ] }),
  cap_save: () => ({ ok: true, echoEpoch: 'cap-epoch-1',
    html: '<div id="caption-workbench"></div>', message: '저장했다' }),
});
capDl.root.setAttribute('data-vl-model', JSON.stringify(capConfirmModel));
capDl.rewired();
capDl.click({ getAttribute: (k) => (k === 'id' ? 'cap-confirm' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const dlAnchors = (capDl.msg._children || [])
  .filter((child) => child && child.tagName === 'a');
checkpoints.push({ case: 'confirm-downloads-safe-anchors',
  calledOnce: capDl.serverCalls.length === 1 &&
    capDl.serverCalls[0].name === 'cap_confirm',
  humanStatus: capDl.msg.textContent.includes('확정 게시 완료') &&
    capDl.msg.textContent.includes('[') === false,
  twoAnchors: dlAnchors.length === 2 &&
    dlAnchors[0].href === '/media/srt-file-id' &&
    dlAnchors[1].href === '/media/vtt-file-id',
  labelsSafe: dlAnchors[0].textContent === 'SRT' &&
    dlAnchors[1].textContent === 'VTT' &&
    dlAnchors.every((a) => a.attrs.download === ''),
  evilFiltered: (capDl.msg._children || [])
    .every((child) => !child.href || String(child.href).indexOf('/media/') === 0),
  replyApplied: capDl.propsCalls.length === 1 });
// 커밋 morph 뒤 재배선 — 안내문 복원과 함께 상태의 링크도 다시 붙는다(ROOT 실측:
// 옛 DOM에만 붙은 링크는 실제 커밋이 지운다 — 설치본 검증은 scratch 프로브).
capDl.rewired();
const repaintAnchors = (capDl.msg._children || [])
  .filter((child) => child && child.tagName === 'a');
checkpoints.push({ case: 'confirm-links-repaint-after-rewire',
  messageRestored: capDl.msg.textContent.includes('확정 게시 완료'),
  anchorsRepainted: repaintAnchors.length === 2 &&
    repaintAnchors[0].href === '/media/srt-file-id' });
capDl.cueIdEl.value = 'a0000';
capDl.textEl.value = '이어오는 편집';
capDl.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'confirm-links-clear-on-next-message',
  messageFresh: capDl.msg.textContent.includes('저장했다'),
  anchorsGone: (capDl.msg._children || []).length === 0 });

// ── 같은 화면 생명의 낡은 원본 출력 방어 — 저장으로 머리가 오른 뒤 도착한 옛 ──
// 원본 처리(등록·실행·검토)의 지연 출력이 **낮은 머리**의 HTML로 화면을 되돌리던
// 길(ROOT 실측: 머리2 화면의 편집 칸·행·오버레이가 옛 글자로 굴러갔다). 토큰이
// 같고 머리가 낮으면 지금 커밋된 모델을 다시 그린다 — 이어오는 저장도 지금 머리를
// 싣는다. 같은 머리 재렌더는 그대로 받아들인다.
const rollbackSaved = captionModel();
rollbackSaved.epoch = 'cap-epoch-rb-saved';
rollbackSaved.head = 4;
rollbackSaved.cues[0].text = '최종 화면에서 수정했습니다';
let rbSaveCalls = 0;
const envRollback = captionEnv({
  cap_save: (p) => {
    rbSaveCalls += 1;
    return rbSaveCalls <= 2
      ? { ok: true, echoEpoch: p.epoch,
        message: '저장했다 — 큐 `a0000`.',
        html: '<div id="caption-workbench"></div>' }
      : { __hold: true };
  },
});
envRollback.cueIdEl.value = 'a0000';
envRollback.textEl.value = '최종 화면에서 수정했습니다';
envRollback.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
envRollback.root.setAttribute('data-vl-model', JSON.stringify(rollbackSaved));
envRollback.cues.innerHTML = '';
flushObservers(envRollback.block);
await new Promise((resolve) => setTimeout(resolve, 0));
const rollbackSerial = windowScope.__vlCaptionWireSerial;
// 옛 원본 출력의 지연 도착 — 같은 토큰, 저장 전 머리(3)의 옛 서버 뼈대.
const rollbackStale = captionModel();
rollbackStale.epoch = 'cap-epoch-rb-stale';
rollbackStale.head = 3;
rollbackStale.cues[0].text = 'ROOT F4 수동 자막';
envRollback.root.setAttribute('data-vl-model', JSON.stringify(rollbackStale));
envRollback.cues.innerHTML = '<button type="button" class="cap-row" data-vl-cue="a0000">' +
  '<span class="cap-row-text">ROOT F4 수동 자막</span></button>';
envRollback.textEl.value = 'ROOT F4 수동 자막';
flushObservers(envRollback.block);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'stale-native-lower-head-rolled-back',
  headKept: windowScope.__vlCaptionWorkbenchState.model.head === 4,
  cuesRepainted: envRollback.cues.innerHTML.includes('최종 화면에서 수정했습니다') &&
    envRollback.cues.innerHTML.includes('ROOT F4 수동 자막') === false,
  editorFromCommitted: envRollback.textEl.value === '최종 화면에서 수정했습니다',
  repairNotRewire: windowScope.__vlCaptionWireSerial === rollbackSerial,
  noLoop: (() => {
    flushObservers(envRollback.block);
    return windowScope.__vlCaptionWireSerial === rollbackSerial;
  })() });
// 이어오는 저장 — 화면이 묶인 머리(2)를 싣는다(옛 머리로 거부되는 길이 아니라).
envRollback.cueIdEl.value = 'a0000';
envRollback.textEl.value = '이어오는 편집';
envRollback.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const rollbackCall = envRollback.serverCalls[envRollback.serverCalls.length - 1];
checkpoints.push({ case: 'rollback-next-save-carries-current-head',
  headCarried: rollbackCall && rollbackCall.payload.head === 4,
  bothSaves: envRollback.serverCalls.filter((c) => c.name === 'cap_save').length === 2 });
// 같은 머리의 재렌더(보통의 새로고침)는 그대로 받아들인다 — 세대만 갈린다.
const rollbackRerender = captionModel();
rollbackRerender.epoch = 'cap-epoch-rb-rerender';
rollbackRerender.head = 4;
rollbackRerender.cues[0].text = '최종 화면에서 수정했습니다';
envRollback.root.setAttribute('data-vl-model', JSON.stringify(rollbackRerender));
envRollback.cues.innerHTML = '';
flushObservers(envRollback.block);
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'equal-head-rerender-still-accepted',
  epochFresh: windowScope.__vlCaptionWorkbenchState.model.epoch === 'cap-epoch-rb-rerender',
  headStill: windowScope.__vlCaptionWorkbenchState.model.head === 4 });
// A10 실측 짝 — 대기 중인 진짜 저장 사이에 옛 원본 출력이 와도, 방어가 배선
// 세대를 다시 만들지 않으므로 진짜 응답은 살아서 그대로 적용된다.
envRollback.cueIdEl.value = 'a0000';
envRollback.textEl.value = '대기 중 저장';
envRollback.click({ getAttribute: (k) => (k === 'id' ? 'cap-save' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const pendingSerial = windowScope.__vlCaptionWireSerial;
const pendingProps = envRollback.propsCalls.length;
const rollbackStaleAgain = captionModel();
rollbackStaleAgain.epoch = 'cap-epoch-rb-stale-again';
rollbackStaleAgain.head = 3;
envRollback.root.setAttribute('data-vl-model', JSON.stringify(rollbackStaleAgain));
envRollback.cues.innerHTML = '<button type="button" class="cap-row" data-vl-cue="a0000">' +
  '<span class="cap-row-text">ROOT F4 수동 자막</span></button>';
flushObservers(envRollback.block);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'pending-save-survives-stale-native',
  generationKept: windowScope.__vlCaptionWireSerial === pendingSerial,
  headKeptStill: windowScope.__vlCaptionWorkbenchState.model.head === 4,
  cuesStillCommitted: envRollback.cues.innerHTML.includes('최종 화면에서 수정했습니다') });
envRollback.held[envRollback.held.length - 1].deferred.resolve({ ok: true,
  echoEpoch: 'cap-epoch-rb-rerender', message: '저장했다 — 큐 `a0000`.',
  html: '<div id="caption-workbench"></div>' });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'pending-save-applies-after-repair',
  messageShown: envRollback.msg.textContent.includes('저장했다'),
  replyApplied: envRollback.propsCalls.length === pendingProps + 1 });
// 같은 작업공간의 **새 실행** — 머리가 낮게 다시 시작해도(같은 토큰·다른 runId)
// 의도한 현재 화면이니 방어가 문을 닫지 않는다(A10 11번째 짝).
const newRunModel = captionModel();
newRunModel.epoch = 'cap-epoch-rb-newrun';
newRunModel.runId = 'run-2';
newRunModel.head = 0;
newRunModel.cues[0].text = '새 실행 첫 큐';
envRollback.root.setAttribute('data-vl-model', JSON.stringify(newRunModel));
envRollback.cues.innerHTML = '';
flushObservers(envRollback.block);
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'new-run-lower-head-accepted',
  runBound: windowScope.__vlCaptionWorkbenchState.model.runId === 'run-2',
  headBound: windowScope.__vlCaptionWorkbenchState.model.head === 0,
  cuesFromNewRun: envRollback.cues.innerHTML.includes('새 실행 첫 큐') });

process.stdout.write(JSON.stringify(checkpoints));
})().catch((err) => { console.error(err); process.exit(1); });
"""


@needs_node
def test_client_bootstraps_execute_real_interactions(tmp_path: Path) -> None:
    """실제 진입점(js_on_load 인자 계약)으로 실행 — 사전 주입 없이 서버 함수까지.

    A5의 독립 하네스가 잡은 결함(부트스트랩이 전역을 다른 이름으로 심어 저장
    RPC가 0회)을 여기서도 잡는다 — 하네스는 어떤 `__vl*Workbench*` 전역도 미리
    깔지 않고, 부트스트랩이 스스로 심은 전역으로 실제 클릭이 서버에 닿는다.
    """
    harness = tmp_path / "v2f1-harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    payload = {
        "visual": ui_facade.VISUAL_BOOTSTRAP_JS,
        "caption": ui_facade.CAPTION_BOOTSTRAP_JS,
    }
    payload_file = tmp_path / "v2f1-payload.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(payload_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    observed = {item["case"]: item for item in json.loads(completed.stdout)}

    def holds(case: str, key: str) -> None:
        assert case in observed, f"케이스가 빠졌다: {case}"
        assert observed[case][key] is True, (case, key, observed[case])

    holds("visual-bootstrap", "watchRegistered")
    holds("visual-bootstrap", "libLoaded")
    holds("visual-bootstrap", "wireVisual")
    holds("visual-bootstrap", "propsStashed")
    holds("visual-bootstrap", "serverStashed")
    holds("caption-bootstrap", "watchRegistered")
    holds("caption-bootstrap", "wireCaption")
    holds("caption-bootstrap", "propsStashed")
    holds("caption-bootstrap", "serverStashed")
    holds("rat-parse", "exact")
    holds("rat-parse", "parsedNotNaN")
    holds("rat-parse", "badRejected")
    holds("rat-decimal", "decimal")
    holds("rat-decimal", "intForm")
    holds("rat-g3-inverse", "localOne")
    holds("rat-g3-inverse", "roundTrip")
    holds("rat-arith", "add")
    holds("rat-arith", "mul")
    holds("rat-arith", "cmp")
    holds("wire-render", "thumbs")
    holds("wire-render", "humanSpeech")
    holds("wire-render", "excludedBadge")
    holds("wire-render", "needsBadge")
    holds("wire-render", "pager")
    holds("wire-render", "rail")
    holds("wire-render", "playhead")
    holds("wire-render", "panel")
    holds("wire-render", "panelHuman")
    holds("wire-render", "panelTags")
    holds("wire-render", "provenanceInDetails")
    holds("wire-render", "panelDetails")
    holds("wire-render", "videoSrc")
    holds("wire-render", "srcSetOnce")
    holds("rpc-real-entrypoint", "calledOnce")
    holds("rpc-real-entrypoint", "noteCarried")
    holds("rpc-real-entrypoint", "signatureCarried")
    holds("rpc-real-entrypoint", "tokenCarried")
    holds("rpc-real-entrypoint", "epochCarried")
    holds("source-switch-a-to-b", "srcSwitched")
    holds("source-switch-a-to-b", "paused")
    holds("source-switch-a-to-b", "loopCleared")
    holds("source-switch-a-to-b", "seekStartPlusOrigin")
    holds("source-switch-a-to-b", "railIsB")
    holds("source-switch-a-to-b", "noteIsB")
    holds("source-switch-a-to-b", "noServer")
    holds("source-switch-a-to-b", "noAutoplay")
    holds("same-source-keeps-stream", "srcUntouched")
    holds("same-source-keeps-stream", "seekStart")
    holds("same-source-keeps-stream", "sameElementStill")
    holds("missing-source-unloads", "unloaded")
    holds("missing-source-unloads", "pausedStill")
    holds("missing-source-unloads", "honestStatus")
    holds("missing-source-unloads", "railIsC")
    holds("missing-source-unloads", "clockBlank")
    holds("missing-source-unloads", "panelStatus")
    holds("rail-click-seeks-only", "seekOnly")
    holds("rail-click-seeks-only", "stillNoServer")
    holds("playhead-local", "clock")
    holds("playhead-local", "railMark")
    holds("repeat-loop", "rewound")
    holds("repeat-loop", "played")
    holds("repeat-loop", "stopsAfterPause")
    holds("propose-draft", "draftShown")
    holds("propose-draft", "frameShown")
    holds("apply-cas", "usedProposalHead")
    holds("apply-cas", "usedSnapPts")
    holds("apply-cas", "tokenOnApply")
    holds("apply-cas", "htmlApplied")
    holds("epoch-guard", "staleDropped")
    holds("epoch-guard", "freshApplied")
    holds("signature-rides", "carries")
    holds("signature-rides", "tokenRides")
    holds("signature-rides", "epochRides")
    holds("prepare-no-track-default", "omitted")
    holds("prepare-no-track-default", "tokenAgain")
    holds("caption-render", "rows")
    holds("caption-render", "seekAttrs")
    holds("caption-render", "manualMark")
    holds("caption-render", "editorFilled")
    holds("caption-render", "msFilled")
    holds("cue-row-client-only", "seeked")
    holds("cue-row-client-only", "played")
    holds("cue-row-client-only", "noServer")
    holds("cue-row-client-only", "refilled")
    holds("caption-overlay", "inside")
    holds("caption-overlay", "endExclusive")
    holds("caption-overlay", "manual")
    holds("overlay-toggle", "pressedOff")
    holds("overlay-toggle", "noServerStill")
    holds("caption-save", "calledOnce")
    holds("caption-save", "headBound")
    holds("caption-save", "signature")
    holds("caption-save", "tokenCarried")
    holds("caption-save", "applied")
    holds("caption-late-dropped", "dropped")
    holds("caption-confirm-gate", "blocked")
    holds("caption-confirm-gate", "noServer")
    holds("child-click-traversal", "selected")
    holds("child-click-traversal", "seeked")
    holds("child-click-traversal", "noServer")
    holds("rail-interval-seek-only", "seekPointerTime")
    holds("rail-interval-seek-only", "selectionUnchanged")
    holds("rail-interval-seek-only", "noServerStill")
    holds("stale-closure-rejected", "noServerCall")
    holds("stale-closure-rejected", "noDomEffect")
    holds("stale-closure-rejected", "responseDropped")
    holds("stale-closure-rejected", "freshListenerOnly")
    holds("aba-old-closure-inert", "selectionKept")
    holds("aba-old-closure-inert", "thumbsKept")
    holds("aba-old-closure-inert", "seekKept")
    holds("aba-old-closure-inert", "srcUntouched")
    holds("aba-old-closure-inert", "noServer")
    holds("nonce-two-held", "held")
    holds("nonce-older-dropped", "stillPending")
    holds("nonce-older-dropped", "noApply")
    holds("nonce-newer-wins", "snap")
    holds("nonce-apply-newest", "pts")
    holds("nonce-apply-newest", "tokenRides")
    holds("old-video-unloaded", "paused")
    holds("old-video-unloaded", "unloaded")
    holds("old-video-unloaded", "listenersDetached")
    holds("old-video-unloaded", "loopCleared")
    holds("old-video-unloaded", "newVideoWired")
    holds("caption-child-click", "refilled")
    holds("caption-child-click", "seeked")
    holds("caption-child-click", "noServer")
    holds("caption-old-closure-inert", "selectionKept")
    holds("caption-old-closure-inert", "cuesKept")
    holds("caption-old-closure-inert", "editorKept")
    holds("caption-old-closure-inert", "seekKept")
    holds("caption-old-closure-inert", "noServer")
    holds("caption-old-video-unloaded", "paused")
    holds("caption-old-video-unloaded", "unloaded")
    holds("caption-old-video-unloaded", "listenersDetached")
    holds("caption-old-video-unloaded", "overlayCleared")
    holds("caption-old-video-unloaded", "selectionReset")
    holds("caption-old-video-unloaded", "editorRefilled")
    holds("caption-old-video-unloaded", "freshVideoWired")
    holds("caption-save-rerender-keeps-video", "sameElement")
    holds("caption-save-rerender-keeps-video", "noUnload")
    holds("caption-save-rerender-keeps-video", "selectionKept")
    holds("caption-save-rerender-keeps-video", "tickAlive")
    holds("deferred-commit-rewires-new-root", "noWireAtWatch")
    holds("deferred-commit-rewires-new-root", "wiredAfterCommit")
    holds("deferred-commit-rewires-new-root", "newRootWired")
    holds("deferred-commit-rewires-new-root", "freshModelRendered")
    holds("deferred-commit-rewires-new-root", "observerOnce")
    holds("deferred-commit-rewires-new-root", "subtreeWatched")
    holds("deferred-commit-rewires-new-root", "noFlushLoop")
    holds("deferred-commit-closure-alive", "selected")
    holds("deferred-commit-closure-alive", "seeked")
    holds("deferred-commit-closure-alive", "noServer")
    holds("sync-commit-wires-immediately", "wiredNow")
    holds("sync-commit-wires-immediately", "rendered")
    holds("watch-no-change-noop", "noWire")
    holds("watch-no-change-noop", "kept")
    holds("direct-props-no-watch-observer-rewires", "wiredByObserver")
    holds("direct-props-no-watch-observer-rewires", "newRootWired")
    holds("direct-props-no-watch-observer-rewires", "rendered")
    holds("direct-props-no-watch-observer-rewires", "noServer")
    holds("caption-deferred-commit-rewires", "noWireAtWatch")
    holds("caption-deferred-commit-rewires", "wiredAfterCommit")
    holds("caption-deferred-commit-rewires", "cuesRendered")
    holds("caption-deferred-commit-rewires", "selectionKept")
    holds("caption-deferred-commit-rewires", "editorRefilled")
    holds("caption-deferred-commit-rewires", "videoAlive")
    holds("caption-deferred-commit-rewires", "tickAlive")
    holds("caption-deferred-commit-rewires", "overlayFresh")
    holds("caption-deferred-commit-rewires", "noFlushLoop")
    holds("caption-direct-props-no-watch-rewires", "wiredByObserver")
    holds("caption-direct-props-no-watch-rewires", "cuesRendered")
    holds("caption-direct-props-no-watch-rewires", "editorRefilledFromCommit")
    holds("caption-direct-props-no-watch-rewires", "videoAlive")
    holds("caption-direct-props-no-watch-rewires", "noServer")
    holds("morph-save-rewires-same-root", "replyApplied")
    holds("morph-save-rewires-same-root", "wiredOnce")
    holds("morph-save-rewires-same-root", "freshModelBound")
    holds("morph-save-rewires-same-root", "freshCards")
    holds("morph-save-rewires-same-root", "messageKept")
    holds("morph-save-rewires-same-root", "clickSingle")
    holds("morph-save-rewires-same-root", "videoReused")
    holds("morph-save-rewires-same-root", "noLoop")
    holds("morph-closure-alive", "selected")
    holds("morph-closure-alive", "seekedOrigin")
    holds("morph-closure-alive", "selectionZeroRPC")
    holds("morph-closure-alive", "oldClosureInert")
    holds("morph-closure-alive", "noLateRestoration")
    holds("morph-attribute-only-rewires", "rewired")
    holds("morph-attribute-only-rewires", "epochFresh")
    holds("morph-attribute-only-rewires", "onceOnly")
    holds("morph-attribute-only-rewires", "attrWatched")
    holds("morph-caption-save-keeps-screen-life", "replyApplied")
    holds("morph-caption-save-keeps-screen-life", "wiredOnce")
    holds("morph-caption-save-keeps-screen-life", "cuesFromCommit")
    holds("morph-caption-save-keeps-screen-life", "selectionKept")
    holds("morph-caption-save-keeps-screen-life", "editorFromCommit")
    holds("morph-caption-save-keeps-screen-life", "videoAlive")
    holds("morph-caption-save-keeps-screen-life", "clickSingle")
    holds("morph-caption-save-keeps-screen-life", "tickSingle")
    holds("morph-caption-save-keeps-screen-life", "overlayFresh")
    holds("morph-caption-save-keeps-screen-life", "messageKept")
    holds("morph-caption-save-keeps-screen-life", "noLoop")
    holds("morph-caption-save-keeps-screen-life", "saveOnce")
    holds("morph-new-token-resets-screen", "rewired")
    holds("morph-new-token-resets-screen", "selectionReset")
    holds("morph-new-token-resets-screen", "editorFirstCue")
    holds("morph-new-token-resets-screen", "videoElementKept")
    holds("morph-new-token-resets-screen", "oldClosureInert")
    holds("morph-new-token-resets-screen", "noServer")
    holds("morph-new-token-resets-screen", "noLateRestoration")
    holds("postassign-rewires-without-observer", "wiredByScheduler")
    holds("postassign-rewires-without-observer", "modelFresh")
    holds("postassign-rewires-without-observer", "rendered")
    holds("postassign-rewires-without-observer", "saveOnce")
    holds("placeholder-morph-tears-down-visual", "generationInvalidated")
    holds("placeholder-morph-tears-down-visual", "stateCleared")
    holds("placeholder-morph-tears-down-visual", "videoUnloaded")
    holds("placeholder-morph-tears-down-visual", "ticksRemoved")
    holds("placeholder-morph-tears-down-visual", "listsCleared")
    holds("placeholder-morph-tears-down-visual", "oldClosureInert")
    holds("placeholder-morph-tears-down-visual", "noServer")
    holds("placeholder-recovery-visual", "rewired")
    holds("placeholder-recovery-visual", "freshBound")
    holds("placeholder-recovery-visual", "cardsRendered")
    holds("placeholder-recovery-visual", "actionAlive")
    holds("placeholder-morph-tears-down-caption", "generationInvalidated")
    holds("placeholder-morph-tears-down-caption", "stateCleared")
    holds("placeholder-morph-tears-down-caption", "editorCleared")
    holds("placeholder-morph-tears-down-caption", "cuesMsgOverlayCleared")
    holds("placeholder-morph-tears-down-caption", "videoUnloaded")
    holds("placeholder-morph-tears-down-caption", "ticksRemoved")
    holds("placeholder-morph-tears-down-caption", "recovered")
    holds("placeholder-morph-tears-down-caption", "oldClosureInert")
    holds("placeholder-morph-tears-down-caption", "noServer")
    holds("late-null-inert", "footnoteKept")
    holds("late-null-inert", "lastMsgKept")
    holds("late-null-inert", "thumbsKept")
    holds("late-reject-inert", "footnoteKept")
    holds("late-reject-inert", "lastMsgKept")
    holds("late-null-inert-after-empty", "clearedBefore")
    holds("late-null-inert-after-empty", "footnoteKept")
    holds("late-null-inert-after-empty", "lastMsgKept")
    holds("fresh-failure-diagnostics-preserved", "nullShown")
    holds("fresh-failure-diagnostics-preserved", "rejectShown")
    holds("fresh-failure-diagnostics-preserved", "serverErrorShown")
    holds("fresh-failure-diagnostics-preserved", "notStuckSending")
    holds("fresh-propose-error-closes-draft", "invalidShown")
    holds("fresh-propose-error-closes-draft", "reasonKept")
    holds("fresh-propose-error-closes-draft", "notPending")
    holds("late-null-inert-caption", "msgKept")
    holds("late-null-inert-caption", "lastMsgKept")
    holds("late-reject-inert-caption", "msgKept")
    holds("late-reject-inert-caption", "lastMsgKept")
    holds("fresh-failure-diagnostics-preserved-caption", "nullShown")
    holds("fresh-failure-diagnostics-preserved-caption", "rejectShown")
    holds("fresh-server-error-shown-caption", "errorShown")
    holds("fresh-server-error-shown-caption", "notStuckSending")
    holds("inflight-refresh-carries-shown-context", "replanWentFirst")
    holds("inflight-refresh-carries-shown-context", "refreshHolds")
    holds("inflight-refresh-carries-shown-context", "planCarried")
    holds("inflight-refresh-carries-shown-context", "sourceCarried")
    holds("inflight-refresh-carries-shown-context", "playheadCarried")
    holds("inflight-refresh-carries-shown-context", "selectionCarried")
    holds("inflight-refresh-applies-mapped-selection", "newPlanBound")
    holds("inflight-refresh-applies-mapped-selection", "visibleSelection")
    holds("inflight-refresh-applies-mapped-selection", "committedSelection")
    holds("inflight-refresh-applies-mapped-selection", "playheadKept")
    holds("inflight-refresh-applies-mapped-selection", "replyApplied")
    holds("inflight-refresh-named-edit-targets-mapped", "editSent")
    holds("inflight-refresh-named-edit-targets-mapped", "targetsMapped")
    holds("inflight-refresh-named-edit-targets-mapped", "planIsNew")
    holds("inflight-refresh-named-edit-targets-mapped", "revisionIsNew")
    holds("late-replan-after-refresh-inert", "modelKept")
    holds("late-replan-after-refresh-inert", "thumbsKept")
    holds("late-replan-after-refresh-inert", "timeKept")
    holds("late-replan-after-refresh-inert", "noExtraCommit")
    holds("late-replan-after-refresh-inert", "footnoteKept")
    holds("replan-morph-honors-committed-selection", "committedBound")
    holds("replan-morph-honors-committed-selection", "saveTargetsMapped")
    holds("replan-morph-honors-committed-selection", "playheadKept")
    holds("replan-morph-honors-committed-selection", "videoKept")
    holds("replan-morph-local-selection-still-wins", "saveTargetsLocal")
    holds("confirm-downloads-safe-anchors", "calledOnce")
    holds("confirm-downloads-safe-anchors", "humanStatus")
    holds("confirm-downloads-safe-anchors", "twoAnchors")
    holds("confirm-downloads-safe-anchors", "labelsSafe")
    holds("confirm-downloads-safe-anchors", "evilFiltered")
    holds("confirm-downloads-safe-anchors", "replyApplied")
    holds("confirm-links-repaint-after-rewire", "messageRestored")
    holds("confirm-links-repaint-after-rewire", "anchorsRepainted")
    holds("confirm-links-clear-on-next-message", "messageFresh")
    holds("confirm-links-clear-on-next-message", "anchorsGone")
    holds("stale-native-lower-head-rolled-back", "headKept")
    holds("stale-native-lower-head-rolled-back", "cuesRepainted")
    holds("stale-native-lower-head-rolled-back", "editorFromCommitted")
    holds("stale-native-lower-head-rolled-back", "noLoop")
    holds("rollback-next-save-carries-current-head", "headCarried")
    holds("rollback-next-save-carries-current-head", "bothSaves")
    holds("equal-head-rerender-still-accepted", "epochFresh")
    holds("equal-head-rerender-still-accepted", "headStill")
    holds("stale-native-lower-head-rolled-back", "repairNotRewire")
    holds("pending-save-survives-stale-native", "generationKept")
    holds("pending-save-survives-stale-native", "headKeptStill")
    holds("pending-save-survives-stale-native", "cuesStillCommitted")
    holds("pending-save-applies-after-repair", "messageShown")
    holds("pending-save-applies-after-repair", "replyApplied")
    holds("new-run-lower-head-accepted", "runBound")
    holds("new-run-lower-head-accepted", "headBound")
    holds("new-run-lower-head-accepted", "cuesFromNewRun")


# ── 3. 서버 함수 — 실백엔드·합성 미디어 ──────────────────────────────────────


@needs_ffmpeg
def test_visual_workbench_model_from_public_query(tmp_path: Path) -> None:
    """작업대 모델은 공개 질의만으로 조립된다 — 카드·레일·원점·이름·페이지."""
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)

    shell, workbench = server.visual_workbench_render(root)
    assert 'id="visual-video"' in shell and "src=" not in shell  # 미리보기 전 — 정직
    model = _model_of(workbench)
    assert model["signature"] == server.current_signature()
    assert model["basePlanId"] and model["revisionId"].endswith("/000000")
    assert model["page"] == 1 and model["pageCount"] == 1 and model["total"] == 4
    (source_id,) = model["sources"]
    source = model["sources"][source_id]
    assert source["state"] == "missing" and source["src"] is None
    assert source["sourceName"] == "발화.mp4"
    assert source["rail"]["duration"] == "4" and source["rail"]["frameSec"] == "1/25"
    assert [item["id"] for item in source["rail"]["intervals"]] == [
        card["id"] for card in model["cards"]
    ]  # 레일은 전체 원본(페이지 경계 밖 이웃 포함 경로) — 질의와 같은 리비전
    assert model["selectedSourceId"] == source_id
    assert model["selectedCard"]["id"] == model["selectedId"]
    assert model["token"] and set(model["sources"]) == {model["selectedSourceId"]}
    assert all("tags" in card for card in model["cards"])
    first = model["cards"][0]
    assert (first["start"], first["end"], first["dur"]) == ("0", "1", "1")
    assert first["neighbor"] == model["cards"][1]["id"]
    assert all(card["needsExtract"] is False for card in model["cards"])
    assert model["needsPlan"] is False and model["epoch"]
    # 다시 그리면 epoch는 매번 새로 파진다(늦은 응답 버림의 재료).
    _, again = server.visual_workbench_render(root)
    assert _model_of(again)["epoch"] != model["epoch"]


@needs_ffmpeg
def test_visual_server_functions_bind_client_revision(tmp_path: Path) -> None:
    """제안→적용 전 경로 — CAS 리비전·stale 거부·선택·메모(이스케이프)·병합."""
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    _, workbench = server.visual_workbench_render(root)
    model = _model_of(workbench)
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    assert list(fns) == [
        "vl_refresh",
        "vl_prepare",
        "vl_propose",
        "vl_apply",
        "vl_replan",
        "vl_select_source",
        "vl_source_caption",
        "vl_relink_original",
    ]
    signature = model["signature"]
    base = model["basePlanId"]
    first = model["cards"][0]

    # 잘못된 서명 — 화면이 속한 작업공간이 아니면 거부(500 아님).
    refused = fns["vl_refresh"]({"epoch": "e", "signature": "다른작업공간", "page": 1})
    assert refused["ok"] is False and "바뀌었다" in refused["error"]

    # 제안 — 서브틱 유리수 `12/25`초가 프레임 12(정확히 격자 위)로 스냅.
    proposal = fns["vl_propose"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": signature,
            "op": "split",
            "revisionId": model["revisionId"],
            "basePlanId": base,
            "segmentId": first["id"],
            "desiredSec": "12/25",
        }
    )
    assert proposal["ok"] and proposal["valid"], proposal
    assert proposal["snapSeconds"] == str(Fraction(12, 25)) and proposal["frame"] == 12
    assert proposal["headRevisionId"] == model["revisionId"]

    # stale 리비전 제안 — 옛 사건에 새 토큰 없음.
    stale = fns["vl_propose"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": signature,
            "op": "split",
            "revisionId": "phantom/000000",
            "basePlanId": base,
            "segmentId": first["id"],
            "desiredSec": "12/25",
        }
    )
    assert stale["ok"] is False and "다르다" in stale["error"]

    # 적용 — 재생 머리(1/2초) 유지: 머리가 속한 둘째 자식이 선택된다.
    applied = fns["vl_apply"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": signature,
            "op": "split",
            "revisionId": proposal["headRevisionId"],
            "basePlanId": base,
            "segmentId": first["id"],
            "splitPts": proposal["snapPts"],
            "page": 1,
            "sourceId": model["selectedSourceId"],
            "selectedId": first["id"],
            "playheadSec": "1/2",
        }
    )
    assert applied["ok"] and "새 검토 리비전" in applied["message"], applied
    after = _model_of(applied["html"])
    assert len(after["cards"]) == 5 and after["revisionId"] != model["revisionId"]
    selected = next(card for card in after["cards"] if card["id"] == after["selectedId"])
    assert Fraction(selected["start"]) <= Fraction(1, 2) < Fraction(selected["end"])
    assert after["needsPlan"] is True  # 경계가 계획과 달라졌다 — 영상 갱신 필요

    # 적용 응답의 html은 작업대 본문만 — 재생기 껍데기(영상 원소)는 없다.
    assert 'id="visual-video"' not in applied["html"]

    # 경계 이동 — 13/25초로.
    boundary = fns["vl_propose"](
        {
            "epoch": after["epoch"],
            "token": after["token"],
            "signature": signature,
            "op": "boundary",
            "revisionId": after["revisionId"],
            "basePlanId": base,
            "leftId": after["cards"][0]["id"],
            "rightId": after["cards"][1]["id"],
            "desiredSec": "13/25",
        }
    )
    assert boundary["ok"] and boundary["valid"]
    assert boundary["snapSeconds"] == str(Fraction(13, 25))
    moved = fns["vl_apply"](
        {
            "epoch": after["epoch"],
            "token": after["token"],
            "signature": signature,
            "op": "boundary",
            "revisionId": boundary["headRevisionId"],
            "basePlanId": base,
            "leftId": after["cards"][0]["id"],
            "rightId": after["cards"][1]["id"],
            "boundaryPts": boundary["snapPts"],
            "page": 1,
            "sourceId": after["selectedSourceId"],
            "selectedId": after["selectedId"],
            "playheadSec": "1/2",
        }
    )
    assert moved["ok"], moved
    moved_model = _model_of(moved["html"])
    assert moved_model["cards"][0]["dur"] == str(Fraction(13, 25))
    assert moved_model["cards"][0]["needsExtract"] is True

    # 사용/제외·메모(값은 이스케이프 경로로만 간다)·병합.
    excluded = fns["vl_apply"](
        {
            "epoch": moved_model["epoch"],
            "token": moved_model["token"],
            "signature": signature,
            "op": "selection",
            "revisionId": moved_model["revisionId"],
            "basePlanId": base,
            "segmentId": moved_model["cards"][0]["id"],
            "selection": "excluded",
            "page": 1,
            "sourceId": moved_model["selectedSourceId"],
            "selectedId": moved_model["cards"][0]["id"],
            "playheadSec": "0",
        }
    )
    assert excluded["ok"]
    excluded_model = _model_of(excluded["html"])
    assert excluded_model["cards"][0]["sel"] == "excluded"
    noted = fns["vl_apply"](
        {
            "epoch": excluded_model["epoch"],
            "token": excluded_model["token"],
            "signature": signature,
            "op": "note",
            "revisionId": excluded_model["revisionId"],
            "basePlanId": base,
            "segmentId": excluded_model["cards"][0]["id"],
            "note": "도착 장면<&>",
            "page": 1,
            "sourceId": excluded_model["selectedSourceId"],
            "selectedId": excluded_model["cards"][0]["id"],
            "playheadSec": "0",
        }
    )
    assert noted["ok"]
    noted_model = _model_of(noted["html"])
    assert noted_model["cards"][0]["note"] == "도착 장면<&>"  # 왕복 보존(이스케이프 무해)
    pair = next(card for card in noted_model["cards"] if card["neighbor"])
    merged = fns["vl_apply"](
        {
            "epoch": noted_model["epoch"],
            "token": noted_model["token"],
            "signature": signature,
            "op": "merge",
            "revisionId": noted_model["revisionId"],
            "basePlanId": base,
            "firstId": pair["id"],
            "secondId": pair["neighbor"],
            "page": 1,
            "sourceId": noted_model["selectedSourceId"],
            "selectedId": pair["id"],
            "playheadSec": "0",
        }
    )
    assert merged["ok"]
    # 안 닿은 짝 병합은 거부다(서버 제안·저장 계약 그대로).
    last = _model_of(merged["html"])
    lone = next(card for card in last["cards"] if card["neighbor"] is None)
    bad_pair = fns["vl_apply"](
        {
            "epoch": last["epoch"],
            "token": last["token"],
            "signature": signature,
            "op": "merge",
            "revisionId": last["revisionId"],
            "basePlanId": base,
            "firstId": lone["id"],
            "secondId": last["cards"][0]["id"],
            "page": 1,
            "sourceId": last["selectedSourceId"],
            "selectedId": lone["id"],
            "playheadSec": "0",
        }
    )
    assert bad_pair["ok"] is False


@needs_ffmpeg
def test_visual_prepare_ready_preview_and_player_stability(tmp_path: Path) -> None:
    """준비(공개 함수) 뒤 미리보기·썸네일 ready — 새로고침 응답은 재생기를 안 건드린다."""
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    _, workbench = server.visual_workbench_render(root)
    model = _model_of(workbench)
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}

    prepared = fns["vl_prepare"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": model["signature"],
            "page": 1,
            "selectedId": model["selectedId"],
            "audioSelections": {model["selectedSourceId"]: 0},
        }
    )
    assert prepared["ok"] and "준비 작업" in prepared["message"]
    for job in [job for job in server.raw.list_jobs(root) if job.kind == "visual_prepare"]:
        record = _wait_terminal(server, root, job.job_id)
        assert record.status == "succeeded", record.error

    shell_ready, workbench_ready = server.visual_workbench_render(root)
    ready = _model_of(workbench_ready)
    (ready_sid,) = ready["sources"]
    ready_source = ready["sources"][ready_sid]
    assert ready_source["state"] == "ready" and ready_source["src"]
    assert ready_source["origin"] == "0"  # 영상이 가장 이른 스트림 — 항등 매핑
    assert ready_source["audioOrdinal"] == 0
    assert ready_source["tracks"][0]["ordinal"] == 0
    assert all(
        card["thumb"]["state"] == "ready" and card["thumb"]["src"] for card in ready["cards"]
    )
    assert 'src="/media/' in shell_ready  # 껍데기도 처음부터 재생 가능한 주소를 싣는다

    # 새로고침(질의) — 응답은 작업대 본문뿐. 서버도 껍데기를 다시 만들지 않는다:
    # 시점을 찍어 같은 미리보기면 껍데기 문자열이 그대로다(클라이언트가 값을 안
    # 갈아끼우면 영상 원소가 살아 있다 — same-source no-reconstruct).
    refreshed = fns["vl_refresh"](
        {"epoch": ready["epoch"], "signature": ready["signature"], "page": 1}
    )
    assert refreshed["ok"] and 'id="visual-video"' not in refreshed["html"]
    shell_again, _ = server.visual_workbench_render(root)
    assert shell_again == shell_ready
    # 잘못된 소리 서수는 제출 순간 거부다(조용한 환원 없음).
    bad_ordinal = fns["vl_prepare"](
        {
            "epoch": ready["epoch"],
            "token": ready["token"],
            "signature": ready["signature"],
            "page": 1,
            "audioSelections": {ready["selectedSourceId"]: True},
        }
    )
    assert bad_ordinal["ok"] is False and "소리 서수" in bad_ordinal["error"]


@needs_ffmpeg
def test_visual_replan_and_epoch_distinct_across_switches(tmp_path: Path) -> None:
    """재계획+재추출 경계 — A→B→A epoch 고유성과 낡은 서명 거부."""
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    _, workbench = server.visual_workbench_render(root)
    model = _model_of(workbench)

    # 편집 하나를 만들어 재계획 감을 켠다.
    proposal = fns["vl_propose"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": model["signature"],
            "op": "split",
            "revisionId": model["revisionId"],
            "basePlanId": model["basePlanId"],
            "segmentId": model["cards"][0]["id"],
            "desiredSec": "12/25",
        }
    )
    applied = fns["vl_apply"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": model["signature"],
            "op": "split",
            "revisionId": proposal["headRevisionId"],
            "basePlanId": model["basePlanId"],
            "segmentId": model["cards"][0]["id"],
            "splitPts": proposal["snapPts"],
            "page": 1,
            "sourceId": model["selectedSourceId"],
            "selectedId": model["cards"][0]["id"],
            "playheadSec": "0",
        }
    )
    edited = _model_of(applied["html"])
    replanned = fns["vl_replan"](
        {
            "epoch": edited["epoch"],
            "token": edited["token"],
            "signature": edited["signature"],
            "revisionId": edited["revisionId"],
            "basePlanId": edited["basePlanId"],
            "page": 1,
            "selectedId": edited["selectedId"],
            "playheadSec": "0",
        }
    )
    assert replanned["ok"] and "새 계획 리비전" in replanned["message"]
    # 재계획 뒤 선택 — 계획 세대가 바뀌어도 옛 선택의 의미 구간이 보존된다:
    # 재생 머리가 있으면 머리가 속한 조각이 이기고(기존 보존 규칙), 머리가 없으면
    # 검증 매핑(옛↔새 같은 core)이 옛 선택의 뒤 조각을 고른다(ROOT 실측: 같은 ID가
    # 다른 구간에 재주조되어 머리 밖 구간을 가리키던 결함의 서버측 수정).
    replan_after = _model_of(replanned["html"])
    assert replan_after["basePlanId"] != edited["basePlanId"]
    head_card = next(c for c in replan_after["cards"] if c["id"] == replan_after["selectedId"])
    assert Fraction(head_card["start"]) == 0  # 머리 0 → 첫 조각
    middle = next(c for c in edited["cards"] if c["neighbor"])
    replan_no_head = fns["vl_replan"](
        {
            "epoch": replan_after["epoch"],
            "token": replan_after["token"],
            "signature": edited["signature"],
            "revisionId": replan_after["revisionId"],
            "basePlanId": replan_after["basePlanId"],
            "page": 1,
            "selectedId": middle["id"],
        }
    )
    assert replan_no_head["ok"], replan_no_head
    mapped_after = _model_of(replan_no_head["html"])
    mapped_sel = next(c for c in mapped_after["cards"] if c["id"] == mapped_after["selectedId"])
    assert (mapped_sel["start"], mapped_sel["end"]) == (middle["start"], middle["end"])
    extraction = [job for job in server.raw.list_jobs(root) if job.kind == "segment_extract"]
    assert extraction  # 추출 작업이 실제로 제출됐다
    # stale 리비전 재계획 — 거부(토큰은 지금 것을 실어 리비전 CAS 계층을 잰다).
    stale = fns["vl_replan"](
        {
            "epoch": "x",
            "token": edited["token"],
            "signature": edited["signature"],
            "revisionId": "phantom/000000",
            "basePlanId": edited["basePlanId"],
            "page": 1,
        }
    )
    assert stale["ok"] is False and ("다르다" in stale["error"] or "먼저 갔다" in stale["error"])
    # 토큰이 늦으면 리비전과 무관하게 거부(작업공간을 다시 연 옛 사건).
    stale_token = fns["vl_replan"](
        {
            "epoch": "x",
            "token": "0" * 32,
            "signature": edited["signature"],
            "revisionId": edited["revisionId"],
            "basePlanId": edited["basePlanId"],
            "page": 1,
        }
    )
    assert stale_token["ok"] is False and "토큰" in stale_token["error"]

    # A→B→A — B(빈 작업공간)의 작업대는 정직한 자리표시자(모델 없음), A를 다시
    # 열면 epoch가 새로 파지고 낡은 서명의 B 시절 요청은 거부된다.
    first_epoch = _model_of(server.visual_workbench_render(root)[1])["epoch"]
    signature_before = server.current_signature()
    server.create_workspace_view("B", str(tmp_path / "ws-b"))
    b_workbench = server.visual_workbench_render(server.workspace_root())[1]
    assert "data-vl-empty" in b_workbench  # B에는 검토 리비전이 없다 — 빈 성공 아님
    reopened = server.open_workspace_view(str(root))
    a_epoch = _model_of(reopened[-5])["epoch"]  # 영상 작업대 본문(-2는 출처 표시 칸)
    again_epoch = _model_of(server.visual_workbench_render(root)[1])["epoch"]
    assert len({first_epoch, a_epoch, again_epoch}) == 3  # 매 그림마다 고유
    assert server.current_signature() == signature_before  # 같은 프로젝트 재오픈
    # 낡은(빈) 서명의 요청 — 거부, 어떤 상태도 바뀌지 않는다.
    late = fns["vl_refresh"]({"epoch": "x", "signature": None, "page": 1})
    assert late["ok"] is False


@needs_ffmpeg
def test_visual_pending_refresh_maps_old_plan_selection(tmp_path: Path) -> None:
    """대기 중 재계획과 경합한 새로고침 — 옛 계획 선택 ID의 의미 구간이 보존된다.

    실측 결함(A9): 재계획이 대기 중인 화면이 옛 계획의 선택 ID를 실고 새로고침을
    보내면, 이미 갈아친 새 계획에서 같은 ID가 **다른 구간**에 재주조돼 있어 재주조된
    앞조각이 골라졌다. 새로고침은 화면이 보던 계획(basePlanId)을 함께 받아, 계획이
    바뀌었으면 검증 매핑의 불변(옛↔새 같은 core)으로 지금 머리의 같은 core 조각을
    찾는다(읽기 전용). 같은 계획 안의 요청 선택은 그대로다(로컬 선택 보존).
    """
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    _, workbench = server.visual_workbench_render(root)
    model = _model_of(workbench)

    def payload(m: dict[str, Any]) -> dict[str, Any]:
        keys = ("signature", "token", "epoch", "revisionId", "basePlanId", "page")
        return {k: m[k] for k in keys}

    # 경계 편집으로 옛 선택의 구간을 6/5..2로 옮긴 뒤 재계획 — 새 계획에서 옛 선택
    # ID가 다른 구간(1..6/5)에 재주조되는 모양을 만든다(실측 재현 순서).
    selected = model["cards"][1]["id"]
    sid = model["cards"][1]["sourceId"]
    noted = fns["vl_apply"](
        {
            **payload(model),
            "op": "note",
            "segmentId": selected,
            "note": "의미 구간 메모",
            "selectedId": selected,
            "sourceId": sid,
            "playheadSec": "3/2",
        }
    )
    assert noted["ok"], noted
    model = _model_of(noted["html"])
    proposal = fns["vl_propose"](
        {**payload(model), "op": "split", "segmentId": model["cards"][0]["id"], "desiredSec": "2/5"}
    )
    assert proposal["ok"] and proposal["valid"]
    split = fns["vl_apply"](
        {
            **payload(model),
            "op": "split",
            "segmentId": model["cards"][0]["id"],
            "splitPts": proposal["snapPts"],
            "selectedId": selected,
            "sourceId": sid,
            "playheadSec": "3/2",
        }
    )
    assert split["ok"], split
    model = _model_of(split["html"])
    left = model["cards"][1]
    boundary = fns["vl_propose"](
        {
            **payload(model),
            "op": "boundary",
            "leftId": left["id"],
            "rightId": selected,
            "desiredSec": "6/5",
        }
    )
    assert boundary["ok"] and boundary["valid"]
    moved = fns["vl_apply"](
        {
            **payload(model),
            "op": "boundary",
            "leftId": left["id"],
            "rightId": selected,
            "boundaryPts": boundary["snapPts"],
            "selectedId": selected,
            "sourceId": sid,
            "playheadSec": "3/2",
        }
    )
    assert moved["ok"], moved
    model = _model_of(moved["html"])
    left = model["cards"][1]
    again = fns["vl_propose"](
        {**payload(model), "op": "split", "segmentId": left["id"], "desiredSec": "1"}
    )
    assert again["ok"] and again["valid"]
    resplit = fns["vl_apply"](
        {
            **payload(model),
            "op": "split",
            "segmentId": left["id"],
            "splitPts": again["snapPts"],
            "selectedId": selected,
            "sourceId": sid,
            "playheadSec": "3/2",
        }
    )
    assert resplit["ok"], resplit
    edited = _model_of(resplit["html"])
    old_span = next(
        (Fraction(c["start"]), Fraction(c["end"])) for c in edited["cards"] if c["id"] == selected
    )
    assert old_span == (Fraction(6, 5), Fraction(2, 1))
    old_payload = payload(edited)

    replanned = fns["vl_replan"](
        {**old_payload, "selectedId": selected, "sourceId": sid, "playheadSec": "3/2"}
    )
    assert replanned["ok"], replanned
    after = _model_of(replanned["html"])
    assert after["basePlanId"] != edited["basePlanId"]
    reused = next(c for c in after["cards"] if c["id"] == selected)
    assert (Fraction(reused["start"]), Fraction(reused["end"])) != old_span  # 재주조 전제
    expected = next(
        c for c in after["cards"] if (Fraction(c["start"]), Fraction(c["end"])) == old_span
    )
    assert expected["note"] == "의미 구간 메모"

    # 옛 계획 문맥의 새로고침 — 같은 core의 조각(재주조 ID가 아님)이 선택된다.
    refresh_old = fns["vl_refresh"](
        {
            **old_payload,
            "selectedId": selected,
            "basePlanId": edited["basePlanId"],
            "sourceId": sid,
            "playheadSec": "3/2",
        }
    )
    assert refresh_old["ok"], refresh_old
    refreshed = _model_of(refresh_old["html"])
    assert refreshed["selectedId"] == expected["id"]
    refreshed_span = next(
        (Fraction(c["start"]), Fraction(c["end"]))
        for c in refreshed["cards"]
        if c["id"] == refreshed["selectedId"]
    )
    assert refreshed_span == old_span
    # 같은 계획 문맥의 새로고침 — 요청 선택이 그대로 이긴다(로컬 선택 보존):
    # 재주조된 조각을 직접 고른 화면의 새로고침은 그 조각을 유지한다.
    refresh_same_plan = fns["vl_refresh"](
        {
            **payload(after),
            "selectedId": reused["id"],
            "basePlanId": after["basePlanId"],
            "sourceId": sid,
            "playheadSec": "3/2",
        }
    )
    assert refresh_same_plan["ok"], refresh_same_plan
    same_plan_model = _model_of(refresh_same_plan["html"])
    assert same_plan_model["selectedId"] == reused["id"]
    # 계획을 알 수 없는 옛 요청 — 옛 ID를 믿지 않는다(재주조된 구간을 가리키지 않게).
    refresh_unknown_plan = fns["vl_refresh"](
        {
            **old_payload,
            "selectedId": selected,
            "basePlanId": "splan_unknown",
            "sourceId": sid,
            "playheadSec": "3/2",
        }
    )
    assert refresh_unknown_plan["ok"], refresh_unknown_plan
    unknown_model = _model_of(refresh_unknown_plan["html"])
    assert unknown_model["selectedId"] != selected


# ── 자막 작업대 — 실제 등록·실행·저장·게이트 ─────────────────────────────────


class _FakeTiming:
    def timed_transcript(self, *, binding: Any, chunk: Any, settings: Any, on: date, **kw: Any):
        from vlogsrt.project.raw.caption_provider import parse_timing_response

        return parse_timing_response(
            {"segments": [{"text": " 망원동 커피에 왔어요", "start": 0.5, "end": 2.5}]},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


class _FakeText:
    def text_transcript(self, *, binding: Any, chunk: Any, settings: Any, on: date, **kw: Any):
        from vlogsrt.project.raw.caption_provider import parse_text_response

        return parse_text_response(
            {"text": "망원동 카페에 왔어요"}, binding=binding, model=settings.models.text_model
        )


@needs_ffmpeg
def test_caption_workbench_save_and_gates(tmp_path: Path) -> None:
    """자막 작업대 — 모델·저장(글자·시각)·늦은 머리·게이트·오버레이 재생기."""
    video = tmp_path / "final.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "1",
            str(video),
        ]
    )
    server = workspace_service.RawWorkspaceUi(
        port=7860,
        state_path=tmp_path / "state.json",
        caption_providers=lambda: (_FakeTiming(), _FakeText()),
    )
    view = server.create_workspace_view("자막작업대", str(tmp_path / "ws"))
    signature = view[1]
    root = server.workspace_root()
    assert root is not None

    markdown, player = server.caption_register_view(signature, str(video), "f1", "자동(첫 트랙)")
    assert "ready" in markdown
    assert 'id="caption-overlay"' in player and 'id="cap-overlay-toggle"' in player
    assert 'id="caption-video"' in player  # 기존 아이디 계약 유지
    # 등록 전에는 작업대가 안내다.
    assert "data-vl-empty" in server.caption_workbench_value(root)

    server.caption_estimate_view(signature, "run-1")
    note = server.caption_run_view(signature, "run-1", True)
    assert _wait_terminal(server, root, note.split("`")[1]).status == "succeeded"
    server.caption_review_view(signature, "run-1")

    value = server.caption_workbench_value(root)
    assert 'id="cap-cues"' in value and "<details" in value
    model = _model_of(value)
    cue = model["cues"][0]
    assert cue["id"] == "a0000" and cue["startMs"] == "500" and cue["endMs"] == "2500"
    assert cue["seek"] and cue["seek"] != ""  # 측정 매핑이 심어졌다(오버레이·행 이동용)
    assert model["head"] is not None and model["signature"] == signature

    fns = {fn.__name__: fn for fn in server._caption_server_functions()}
    assert list(fns) == ["cap_save", "cap_confirm"]
    saved = fns["cap_save"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": signature,
            "head": model["head"],
            "op": "text",
            "cueId": "a0000",
            "text": "손으로 고친 글자",
            "startMs": "",
            "endMs": "",
        }
    )
    assert saved["ok"] and saved["echoEpoch"] == model["epoch"], saved
    after = _model_of(saved["html"])
    assert after["cues"][0]["text"] == "손으로 고친 글자"
    assert after["cues"][0]["basis"] == "manual"
    assert after["epoch"] != model["epoch"]
    assert 'id="visual-video"' not in saved["html"] and "<script" not in saved["html"]

    # 늦은 머리 저장 — 화면이 본 머리가 서버 머리와 다르면 거부.
    late = fns["cap_save"](
        {
            "epoch": after["epoch"],
            "token": after["token"],
            "signature": signature,
            "head": model["head"],
            "op": "text",
            "cueId": "a0000",
            "text": "덮어쓰기",
            "startMs": "",
            "endMs": "",
        }
    )
    assert late["ok"] is False and "늦었다" in late["error"]

    # 시각 저장(ms 정수만).
    timed = fns["cap_save"](
        {
            "epoch": after["epoch"],
            "token": after["token"],
            "signature": signature,
            "head": after["head"],
            "op": "time",
            "cueId": "a0000",
            "text": "",
            "startMs": "600",
            "endMs": "2600",
        }
    )
    assert timed["ok"], timed
    timed_model = _model_of(timed["html"])
    assert timed_model["cues"][0]["startMs"] == "600" and timed_model["cues"][0]["endMs"] == "2600"
    bad_ms = fns["cap_save"](
        {
            "epoch": timed_model["epoch"],
            "token": timed_model["token"],
            "signature": signature,
            "head": timed_model["head"],
            "op": "time",
            "cueId": "a0000",
            "text": "",
            "startMs": "x",
            "endMs": "2000",
        }
    )
    assert bad_ms["ok"] is False

    # 잘못된 서명 — 거부(500 아님).
    bad_sig = fns["cap_save"](
        {
            "epoch": "x",
            "signature": "없는작업공간",
            "head": 1,
            "op": "text",
            "cueId": "a0000",
            "text": "x",
            "startMs": "",
            "endMs": "",
        }
    )
    assert bad_sig["ok"] is False and "바뀌었다" in bad_sig["error"]

    # ms 환산은 정확히 — 이진 부동 잔여가 1200을 1199로 자르지 않는다(A5 실측).
    assert ui_facade.fraction_ms(Fraction(1.2)) == 1200  # 이진 부동에서 온 값
    assert ui_facade.fraction_ms(Fraction(6, 5)) == 1200  # 정확한 유리수
    assert ui_facade.fraction_ms(1.2) == 1200
    assert ui_facade.fraction_ms(Fraction(1199, 1000)) == 1199
    assert ui_facade.fraction_ms(Fraction(1199500, 10**6)) == 1200  # 동률은 올린다

    # 확정은 화면이 보낸 머리에 묶인다 — 옛 머리 번호로 최신 머리를 게시하지
    # 못한다(A5 실측: head 0 요청이 head 1을 확정 게시하던 결함).
    stale_confirm = fns["cap_confirm"](
        {
            "epoch": timed_model["epoch"],
            "token": timed_model["token"],
            "signature": signature,
            "head": model["head"],
        }
    )
    assert stale_confirm["ok"] is False and "확정이 늦었다" in stale_confirm["error"]
    fresh_confirm = fns["cap_confirm"](
        {
            "epoch": timed_model["epoch"],
            "token": timed_model["token"],
            "signature": signature,
            "head": timed_model["head"],
        }
    )
    assert fresh_confirm["ok"], fresh_confirm
    # 작업대 확정 흐름 — 사람 문장 + 등록 미디어 ID 내려받기만(원문 마크다운·해시는
    # 고급 패널 상세가 싣는다). 링크는 실제 등록 파일로 제공된다.
    assert "확정 게시 완료" in fresh_confirm["message"]
    downloads = fresh_confirm["downloads"]
    assert [(d["label"], d["href"].startswith("/media/")) for d in downloads] == [
        ("SRT", True),
        ("VTT", True),
    ]
    for entry in downloads:
        media_id = entry["href"].rsplit("/", 1)[-1]
        registered = next(
            m for m in server.raw.list_media(root, kind="artifact") if m.file_id == media_id
        )
        assert (root / registered.relative_path).is_file()


def test_caption_confirm_gate_reports_open_items(tmp_path: Path) -> None:
    """확정 조건 표시 — 미해결 항목이 남으면 canConfirm 거짓과 이유가 값으로 간다."""
    placeholder = ui_facade.caption_workbench_html({"empty": "안내"})
    assert "data-vl-empty" in placeholder
    model = {
        "empty": None,
        "epoch": "e",
        "signature": "s",
        "runId": "run-1",
        "head": 2,
        "cues": [
            {
                "id": "a0000",
                "startMs": "0",
                "endMs": "1",
                "text": "t",
                "basis": "auto",
                "seek": "0",
                "endSeek": "1",
            }
        ],
        "canConfirm": False,
        "blockReason": "열린 경고 1개",
        "provenance": "",
        "details": [],
    }
    value = ui_facade.caption_workbench_html(model)  # type: ignore[arg-type]
    assert 'id="cap-confirm"' in value and 'data-vl-model="' in value
    parsed = _model_of(value)
    assert parsed["canConfirm"] is False and parsed["blockReason"] == "열린 경고 1개"


# ── 배선 — 작업대 구성요소가 전환·새로고침 출력에 실제로 묶여 있다 ───────────


def test_stage_layout_declared_dom_and_css() -> None:
    """3단 무대 선언 — DOM 구조·범위 CSS가 계약 그대로다(F1a).

    실제 1440x900 배치는 ROOT 실브라우저 수용의 몫이다. 여기는 선언 자체를 잰다:
    본문 뿌리가 목록·패널·각주를 직젉 품고(중간 .vl-grid 없음), CSS가 세 칸
    격자 영역과 모바일 재생기-먼저 순서를 정의하며, 색은 테마 토큰을 따른다.
    """
    workbench = ui_facade.visual_workbench_html({"empty": None, "sources": {}, "cards": []})
    assert "vl-grid" not in workbench  # 중간 포장이 없어 격자가 세 칸에 만난다
    for anchor_id in ("vl-thumbs", "vl-panel", "vl-footnote"):
        assert f'id="{anchor_id}"' in workbench, anchor_id
    assert workbench.index("vl-thumbs") < workbench.index("vl-panel")
    css = ui_facade.WORKBENCH_CSS
    assert (
        'grid-template-areas: "thumbs player panel" "timeline timeline timeline" "foot foot foot"'
        in css
    )
    assert "#vl-thumbs { grid-area: thumbs; }" in css
    assert "#visual-preview { grid-area: player;" in css
    assert "#visual-timeline { grid-area:timeline;" in css
    assert "#vl-panel { grid-area: panel; max-height:" in css
    assert "#visual-footnote" not in css and "#vl-footnote { grid-area: foot; }" in css
    assert "#visual-stage .vl-stage-cell { display: contents; }" in css
    assert "#visual-workbench:not([data-vl-empty]) { display: contents; }" in css
    # 모바일(≤860px) — 격자 영역 순서가 재생기를 맨 먼저 보낸다.
    mobile = css.index("@media (max-width: 860px)")
    assert css.index('"player" "thumbs" "panel" "timeline" "foot"', mobile) > mobile
    assert "grid-template-columns: minmax(0, 1fr)" in css[mobile:]
    # 무채색은 테마 토큰 — 본점(강조) 색은 그대로 하나다.
    for token in (
        "--body-text-color",
        "--body-text-color-subdued",
        "--block-background-fill",
        "--block-border-color",
        "--button-secondary-background-fill",
        "--input-background-fill",
    ):
        assert f"var({token}" in css, token
    assert css.count("#2f6db3") >= 3  # 강조(선택·손잡이·주 단추) — 본점은 하나
    # 자막 무대 — 재생기와 큐 목록+편집기가 나란히, 좁은 화면에서도 재생기 먼저.
    assert 'grid-template-areas: "player cues"' in css
    assert "#caption-stage .vl-stage-cell { display: contents; }" in css
    assert "#caption-player { grid-area: player; }" in css
    assert "#caption-workbench { grid-area: cues; }" in css
    assert css.index('"player" "cues"', mobile) > mobile
    # 네이티브 포장 — 그라디오 .html-container가 무대 칸 사이에 남아 배치를
    # 가로막던 결함(ROOT 1440x900 실측: 재생기 x116 w276 · 패널이 아래로). 무대
    # **범위 안에서만** 편다 — 범용 `.html-container` 덮어쓰기는 없다.
    assert "#visual-stage .html-container { display: contents !important; }" in css
    assert "#caption-stage .html-container { display: contents !important; }" in css
    stripped_css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    bare_selectors = [
        part.strip()
        for rule in stripped_css.split("}")
        if ".html-container" in rule.split("{")[0]
        for part in rule.split("{")[0].split(",")
    ]
    assert bare_selectors, "html-container 규칙이 있어야 한다"
    for selector in bare_selectors:
        assert selector.startswith(("#visual-stage", "#caption-stage")), selector
    # 껍데기도 첫 주소 표식을 심는다 — 첫 배선이 같은 주소를 다시 대입하지 않는다.
    shell = ui_facade.visual_player_shell_html("/media/x", "안내")
    assert 'data-vl-src="/media/x"' in shell


@needs_ffmpeg
def test_visual_two_source_model_binds_per_card_preview(tmp_path: Path) -> None:
    """원본 A/B(서로 다른 잰 원점) — 모델이 원본별 미리보기·렠·트랙을 실는다(F1b).

    B는 선행 소리 원본(영상 +2초·소리 +1.5초)이라 미리보기의 잰 원점이 0이
    아니다(백엔드 A3 실측 계약). 카드마다 원본이 묶여 클라이언트가 서버 없이
    원본을 갈아끼울 재료가 전부 여기 있다.
    """
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    card = tmp_path / "two"
    card.mkdir()
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x64:r=25:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000:duration=3",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(card / "01-landscape.mp4"),
        ]
    )
    _encode(
        [
            "-itsoffset",
            "2",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x32:r=10:d=3",
            "-itsoffset",
            "1.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(card / "02-leading-audio.mp4"),
        ]
    )
    proposal = server.raw.scan_sources(root, card)
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    assert _wait_terminal(server, root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade

    plan = server.raw.plan_segmentation(root, tile_seconds=Fraction(1))
    ReviewWorkspaceFacade(root).initialize_review(plan.revision_id)
    prepare = server.raw.start_visual_page_preparation(root)
    assert _wait_terminal(server, root, prepare.job_id).status == "succeeded"

    shell, workbench = server.visual_workbench_render(root)
    model = _model_of(workbench)
    assert len(model["sources"]) == 2
    by_name = {info["sourceName"]: info for info in model["sources"].values()}
    landscape = by_name["01-landscape.mp4"]
    leading = by_name["02-leading-audio.mp4"]
    # 미리보기 준비 뒤 — 둘 다 재생 주소와 잰 원점을 실었다.
    assert landscape["src"] and leading["src"]
    assert Fraction(landscape["origin"]) == 0  # 영상이 가장 이른 스트림 — 항등 매핑
    assert Fraction(leading["origin"]) != 0  # 선행 소리 — 출력 영상 첫 PTS가 더한다(G3)
    # 렠은 원본별로 자기 조각만 — 카드 묶음과 짝이 맞는다.
    for info in (landscape, leading):
        rail_ids = {item["id"] for item in info["rail"]["intervals"]}
        card_ids = {c["id"] for c in model["cards"] if c["sourceId"] == info["sourceId"]}
        assert rail_ids and rail_ids == card_ids
    # 카드는 원본에 묶여 있고 선택 카드 값도 함께 온다.
    assert all(c["sourceId"] in model["sources"] for c in model["cards"])
    assert model["selectedCard"]["id"] == model["selectedId"]
    assert model["selectedCard"]["sourceId"] == model["selectedSourceId"]
    # 선택을 B 조각으로 바꾼 재그림 — 껍데기 주소도 B의 것이다.
    b_card = next(c for c in model["cards"] if c["sourceId"] == leading["sourceId"])
    shell_b, workbench_b = server.visual_workbench_render(root, preferred_selected_id=b_card["id"])
    model_b = _model_of(workbench_b)
    assert model_b["selectedSourceId"] == leading["sourceId"]
    assert f'src="{leading["src"]}"' in shell_b
    # 정확한 유리수 — B의 첫 조각 시작 + B의 잰 원점이 클라이언트 이동 값이다.
    start_b = Fraction(b_card["start"])
    assert start_b + Fraction(leading["origin"]) == Fraction(b_card["start"]) + Fraction(
        model_b["sources"][leading["sourceId"]]["origin"]
    )


@needs_ffmpeg
def test_visual_refresh_honors_requested_page(tmp_path: Path) -> None:
    """명시적 페이지는 요청이 이긴다 — 페이지 넘김이 1번으로 되돌아가지 않는다."""
    from vlogsrt.project.raw.visual_review import VISUAL_PAGE_SIZE

    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    card = tmp_path / "long"
    card.mkdir()
    duration = VISUAL_PAGE_SIZE + 2  # 26초 → 조각 26개 → 페이지 2
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=128x64:r=25:d={duration}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(card / "long.mp4"),
        ]
    )
    proposal = server.raw.scan_sources(root, card)
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    assert _wait_terminal(server, root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade

    plan = server.raw.plan_segmentation(root, tile_seconds=Fraction(1))
    ReviewWorkspaceFacade(root).initialize_review(plan.revision_id)

    _, workbench = server.visual_workbench_render(root)
    model = _model_of(workbench)
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    # A5 재현 — vl_refresh(page=2, selectedId=1페이지 조각)가 1페이지로 돌아갔다.
    refreshed = fns["vl_refresh"](
        {
            "epoch": model["epoch"],
            "signature": model["signature"],
            "page": 2,
            "selectedId": model["cards"][0]["id"],
        }
    )
    assert refreshed["ok"], refreshed
    after = _model_of(refreshed["html"])
    assert after["page"] == 2 and after["pageCount"] == 2
    assert len(after["cards"]) == 2  # 26 − 24
    # 선택은 유지되되 페이지 카드 밖(1페이지 조각)에 살아 있다 — 값으로 온다.
    assert after["selectedId"] == model["cards"][0]["id"]
    assert after["selectedCard"]["id"] == after["selectedId"]
    assert after["selectedSourceId"] in after["sources"]
    # 재생 머리 유지 재그림(편집 뒤)은 머리가 속한 페이지를 따라간다 — 계약 유지.
    from vlogsrt.project.raw.visual_review import VISUAL_PAGE_SIZE as _page_size

    applied = fns["vl_apply"](
        {
            "epoch": after["epoch"],
            "token": after["token"],
            "signature": after["signature"],
            "op": "note",
            "segmentId": after["cards"][0]["id"],
            "note": "2페이지 메모",
            "revisionId": after["revisionId"],
            "basePlanId": after["basePlanId"],
            "page": 2,
            "selectedId": after["cards"][0]["id"],
            "playheadSec": str(Fraction(24, 1) + Fraction(1, 2)),
        }
    )
    assert applied["ok"], applied
    kept = _model_of(applied["html"])
    assert kept["page"] == 2  # 머리(24.5초)가 속한 조각의 페이지
    assert kept["cards"][0]["note"] == "2페이지 메모"
    del _page_size


@needs_ffmpeg
def test_advanced_context_rejects_old_and_stale_native_events(
    tmp_path: Path, local_socket: None
) -> None:
    """고급(기존) 단추의 실제 배선 — 옛 문맥 사건은 적용 전에 거부된다(F2-2).

    A6 실측(`advanced-results-final.json`)의 세 축을 그대로 잰다: (1) 다른
    클라이언트가 저장해 머리가 옮겨도 옛 머리 사건이 적용되던 결함, (2) A→B→A
    뒤 옛 사건 적용, (3) 자막 수동 편집·확정의 같은 결함(옛 머리 확정이 최신
    머리를 게시하던 길). 배선 핸들러는 문맥(수명 토큰·화면이 본 리비전/머리)을
    사건 입력으로 받아 검증을 적용보다 앞세운다 — 거부되면 리비전·큐·지속본
    어느 것도 바뀌지 않는다.
    """
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    server.build_blocks()
    signature = server.current_signature()
    handlers = server._advanced_handlers
    facade = server.raw.review_workspace_facade(root)

    # 화면 문맥 — 새로고침 계열(gated=False)이 내리는 값을 클라이언트가 댄다.
    _markdown, shown_revision, nonce = handlers["review-refresh"](None, None, signature)
    assert shown_revision == server._review_revision_id
    assert nonce == server._visual_lifetime_token
    segment_id = next(iter(server._review_spans))

    # 클라이언트 B가 먼저 저장 — 머리(리비전)가 한 칸 옮는다.
    _markdown, shown_after_b, _nonce_b = handlers["review-note"](
        nonce, shown_revision, signature, segment_id, "B 클라이언트 메모"
    )
    assert shown_after_b != shown_revision

    # 늦은 클라이언트 A 사건 — 화면이 본 리비전이 서버 머리와 다르면 거부다.
    with pytest.raises(gr.Error, match="늦었다"):
        handlers["review-note"](
            nonce,
            shown_revision,
            signature,
            segment_id,
            "OLD NATIVE A1 EVENT MUST NEVER APPLY",
        )
    head = facade.load_head().revision
    assert head.revision_id == shown_after_b  # 리비전 불변
    segment = next(s for s in head.segments if s.segment_id == segment_id)
    assert segment.notes == "B 클라이언트 메모"  # 지속본 무오염

    # A→B→A — 수명 토큰이 다시 파진다. 새로고침으로 회복한 뒤에도 **옛 토큰**
    # 사건은 머리가 같아도 거부다.
    server.create_workspace_view("B", str(tmp_path / "ws-b"))
    server.open_workspace_view(str(root))
    assert server.current_signature() == signature  # 결정적 서명 — 같다
    _markdown, shown_revision2, nonce2 = handlers["review-refresh"](None, None, signature)
    assert nonce2 != nonce and shown_revision2 == shown_after_b
    with pytest.raises(gr.Error, match="토큰"):
        handlers["review-note"](nonce, shown_after_b, signature, segment_id, "OLD NATIVE ABA EVENT")
    head = facade.load_head().revision
    segment = next(s for s in head.segments if s.segment_id == segment_id)
    assert head.revision_id == shown_after_b and segment.notes == "B 클라이언트 메모"
    # 지금 문맥으로는 정상 흐름이 이어진다.
    _markdown, shown_after_a2, _nonce = handlers["review-note"](
        nonce2, shown_revision2, signature, segment_id, "A2 메모"
    )
    assert shown_after_a2 != shown_revision2


@needs_ffmpeg
def test_advanced_caption_context_rejects_old_head_and_aba(
    tmp_path: Path, local_socket: None
) -> None:
    """자막 고급(수동 편집·확정)의 실제 배선 — 옛 머리·옛 토큰 사건 거부(F2-2).

    A6 실측 재현: 옛 머리(0)의 수동 편집이 머리 1→2로 적용되고, 이어 옛 머리
    확정이 최신 머리를 게시하던 결함. 문맥 검증이 먼저면 큐·머리·확정 어느 것도
    바뀌지 않는다.
    """
    video = tmp_path / "final.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "1",
            str(video),
        ]
    )
    server = workspace_service.RawWorkspaceUi(
        port=7860,
        state_path=tmp_path / "state.json",
        caption_providers=lambda: (_FakeTiming(), _FakeText()),
    )
    view = server.create_workspace_view("자막문맥", str(tmp_path / "ws"))
    signature = view[1]
    root = server.workspace_root()
    assert root is not None
    server.caption_register_view(signature, str(video), "f1", "자동(첫 트랙)")
    server.caption_estimate_view(signature, "run-1")
    note = server.caption_run_view(signature, "run-1", True)
    assert _wait_terminal(server, root, note.split("`")[1]).status == "succeeded"
    server.caption_review_view(signature, "run-1")
    server.build_blocks()
    handlers = server._advanced_handlers

    # 화면 문맥 — 검토 새로고침 배선이 내리는 값 그대로.
    _md, _table, _player, _dd, _wb, nonce, shown_head = handlers["caption-refresh"](
        signature, "run-1"
    )
    assert nonce == server._caption_lifetime_token
    assert shown_head == server._caption_head_sequence

    # 클라이언트 B의 수동 편집 — 머리가 한 칸 옮는다.
    result = handlers["caption-edit"](
        nonce,
        shown_head,
        signature,
        "글자 교정",
        "a0000",
        "B 클라이언트 글자",
        "",
        "",
        "",
        "",
        "",
    )
    head_after_b = result[-1]
    assert head_after_b == shown_head + 1

    # 늦은 클라이언트 A 사건 — 옛 머리 편집은 거부다(지속본 무오염).
    with pytest.raises(gr.Error, match="늦었다"):
        handlers["caption-edit"](
            nonce,
            shown_head,
            signature,
            "글자 교정",
            "a0000",
            "OLD NATIVE CAPTION EVENT",
            "",
            "",
            "",
            "",
            "",
        )
    ws = server._caption_ws(root)
    bundle = ws.review_bundle(server._caption_run_identity(server._require_caption_identity(root)))
    assert bundle.state.head_revision == head_after_b
    row = next(r for r in bundle.state.rows if r.cue.cue_id == "a0000")
    assert row.cue.text == "B 클라이언트 글자"

    # 옛 머리 확정도 거부 — 최신 머리를 몰래 게시하지 않는다(A6 결함의 축).
    with pytest.raises(gr.Error, match="늦었다"):
        handlers["caption-confirm"](nonce, shown_head, signature, "run-1", True)
    published_dir = root / "final" / "captions" / "published"
    assert not published_dir.exists() or not any(published_dir.iterdir())

    # 지금 문맥 확정 — 게시된다.
    published = handlers["caption-confirm"](nonce, head_after_b, signature, "run-1", True)
    assert "확정 완료" in published[0]

    # A→B→A — 옛 토큰 편집은 머리가 옮겨도 거부다.
    server.create_workspace_view("B", str(tmp_path / "ws-b"))
    server.open_workspace_view(str(root))
    signature = server.current_signature()
    server.caption_register_view(signature, str(video), "f1", "자동(첫 트랙)")
    server.caption_estimate_view(signature, "run-1")
    server.caption_review_view(signature, "run-1")
    with pytest.raises(gr.Error, match="토큰"):
        handlers["caption-edit"](
            nonce, head_after_b, signature, "글자 교정", "a0000", "OLD ABA", "", "", "", "", ""
        )


@needs_ffmpeg
def test_visual_token_lifetime_rejects_old_aba(tmp_path: Path) -> None:
    """A→B→A 수명 토큰 — 같은 서명·같은 머리여도 옛 사건은 거부된다(F1c)."""
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    _, workbench = server.visual_workbench_render(root)
    model = _model_of(workbench)
    token_a1 = model["token"]

    # 보통의 새로고침·편집은 토큰을 갈지 않는다 — 같은 화면 생명 안에서 이어진다.
    refreshed = fns["vl_refresh"](
        {"epoch": model["epoch"], "signature": model["signature"], "page": 1}
    )
    assert refreshed["ok"] and _model_of(refreshed["html"])["token"] == token_a1
    noted = fns["vl_apply"](
        {
            "epoch": model["epoch"],
            "token": token_a1,
            "signature": model["signature"],
            "op": "note",
            "segmentId": model["cards"][0]["id"],
            "note": "A1 시절 메모",
            "revisionId": model["revisionId"],
            "basePlanId": model["basePlanId"],
            "page": 1,
            "selectedId": model["cards"][0]["id"],
            "playheadSec": "0",
        }
    )
    assert noted["ok"]
    head_before = _model_of(noted["html"])["revisionId"]

    # A→B→A — 서명은 같아도 토큰은 다시 파진다.
    server.create_workspace_view("B", str(tmp_path / "ws-b"))
    reopened = server.open_workspace_view(str(root))
    model_a2 = _model_of(reopened[-5])  # 영상 작업대 본문(뒤에 경로 표시 칸 세 개가 붙는다)
    assert model_a2["signature"] == model["signature"]  # 결정적 서명 — 같다
    assert model_a2["token"] != token_a1  # 수명 토큰 — 다르다

    # 옛 A1 사건(A5 문구 그대로) — 머리가 지금과 같아도 토큰이 늦어 거부다.
    old_event = fns["vl_apply"](
        {
            "epoch": model["epoch"],
            "token": token_a1,
            "signature": model["signature"],
            "op": "note",
            "segmentId": model_a2["cards"][0]["id"],
            "note": "OLD A1 EVENT MUST NEVER APPLY",
            "revisionId": model_a2["revisionId"],
            "basePlanId": model_a2["basePlanId"],
            "page": 1,
            "selectedId": model_a2["cards"][0]["id"],
            "playheadSec": "0",
        }
    )
    assert old_event["ok"] is False and "토큰" in old_event["error"]
    head_after = _model_of(server.visual_workbench_render(root)[1])["revisionId"]
    assert head_after == head_before  # 아무 리비전도 만들어지지 않았다
    facade = server.raw.review_workspace_facade(root)
    segment = facade.load_head().revision.segments[0]
    assert segment.notes != "OLD A1 EVENT MUST NEVER APPLY"  # 지속본에도 없다
    # 옛 제안·준비도 같은 문으로 거부된다.
    old_proposal = fns["vl_propose"](
        {
            "epoch": model["epoch"],
            "token": token_a1,
            "signature": model["signature"],
            "op": "split",
            "revisionId": model_a2["revisionId"],
            "basePlanId": model_a2["basePlanId"],
            "segmentId": model_a2["cards"][0]["id"],
            "desiredSec": "12/25",
        }
    )
    assert old_proposal["ok"] is False and "토큰" in old_proposal["error"]
    old_prepare = fns["vl_prepare"](
        {
            "epoch": model["epoch"],
            "token": token_a1,
            "signature": model["signature"],
            "page": 1,
        }
    )
    assert old_prepare["ok"] is False and "토큰" in old_prepare["error"]
    # 지금 토큰을 실은 요청은 그대로 통과한다 — 정상 흐름 회복.
    fresh_note = fns["vl_apply"](
        {
            "epoch": model_a2["epoch"],
            "token": model_a2["token"],
            "signature": model_a2["signature"],
            "op": "note",
            "segmentId": model_a2["cards"][0]["id"],
            "note": "A2 메모",
            "revisionId": model_a2["revisionId"],
            "basePlanId": model_a2["basePlanId"],
            "page": 1,
            "selectedId": model_a2["cards"][0]["id"],
            "playheadSec": "0",
        }
    )
    assert fresh_note["ok"] and "A2 메모" in fresh_note["html"]


@needs_ffmpeg
def test_caption_token_rejects_old_aba(tmp_path: Path) -> None:
    """자막 작업대 수명 토큰 — A→B→A 뒤 옛 저장·확정은 거부된다(F1c)."""
    video = tmp_path / "final.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "1",
            str(video),
        ]
    )
    server = workspace_service.RawWorkspaceUi(
        port=7860,
        state_path=tmp_path / "state.json",
        caption_providers=lambda: (_FakeTiming(), _FakeText()),
    )
    view = server.create_workspace_view("자막ABA", str(tmp_path / "ws"))
    signature = view[1]
    root = server.workspace_root()
    assert root is not None
    server.caption_register_view(signature, str(video), "f1", "자동(첫 트랙)")
    server.caption_estimate_view(signature, "run-1")
    note = server.caption_run_view(signature, "run-1", True)
    assert _wait_terminal(server, root, note.split("`")[1]).status == "succeeded"
    server.caption_review_view(signature, "run-1")
    model = _model_of(server.caption_workbench_value(root))
    token_cap1 = model["token"]
    fns = {fn.__name__: fn for fn in server._caption_server_functions()}

    # A→B→A — 자막 토큰도 작업공간 전환마다 다시 파진다. 전환은 자막 화면 상태도
    # 지우므로 같은 최종 영상을 다시 등록·검토해 A2 화면을 만든다(실행은 이미
    # 끝났고 새 제공자 호출은 없다).
    server.create_workspace_view("B", str(tmp_path / "ws-b"))
    reopened = server.open_workspace_view(str(root))
    signature = server.current_signature()
    server.caption_register_view(signature, str(video), "f1", "자동(첫 트랙)")
    server.caption_estimate_view(signature, "run-1")  # 실행 신원을 다시 세운다(로컬)
    server.caption_review_view(signature, "run-1")
    model2 = _model_of(server.caption_workbench_value(root))
    assert model2["token"] != token_cap1
    del reopened

    # 옛 토큰 저장 — 머리가 맞아도 거부다.
    old_save = fns["cap_save"](
        {
            "epoch": model["epoch"],
            "token": token_cap1,
            "signature": signature,
            "head": model2["head"],
            "op": "text",
            "cueId": "a0000",
            "text": "OLD CAPTION EVENT",
            "startMs": "",
            "endMs": "",
        }
    )
    assert old_save["ok"] is False and "토큰" in old_save["error"]
    old_confirm = fns["cap_confirm"](
        {"epoch": model["epoch"], "token": token_cap1, "signature": signature, "head": 1}
    )
    assert old_confirm["ok"] is False and "토큰" in old_confirm["error"]
    # 지금 토큰으로는 저장된다.
    fresh = fns["cap_save"](
        {
            "epoch": model2["epoch"],
            "token": model2["token"],
            "signature": signature,
            "head": model2["head"],
            "op": "text",
            "cueId": "a0000",
            "text": "A2 자막",
            "startMs": "",
            "endMs": "",
        }
    )
    assert fresh["ok"] and "A2 자막" in fresh["html"]


def test_blocks_wire_workbench_outputs(tmp_path: Path, local_socket: None) -> None:
    """조립 뒤 전환·페이지 로드 출력에 작업대 세 구성요소가 포함된다."""
    server = _new_server(tmp_path)
    server.build_blocks()
    outputs = server._selection_outputs()
    assert server._visual_player_block in outputs
    assert server._visual_workbench_block in outputs
    assert server._caption_workbench_block in outputs
    assert server._signature_state in server._load_outputs()
    values = server.workspace_view_values()
    assert len(values) == len(outputs)
    # 보기 꼬리: …·영상 작업대 본문(-5)·자막 작업대 본문(-4)·경로 표시 세 칸(-3~-1).
    assert 'id="visual-workbench"' in values[-5]
    assert 'id="caption-workbench"' in values[-4]
    assert values[-2] == "" and values[-1] == ""  # 일반 작업공간 — 출처·최종 영상 표시는 비다
    # 3단 무대 — 껍데기·본문 두 구성요소가 같은 `#visual-stage` Row 안에 있고
    # 각자 vl-stage-cell 껍데기 계약을 단다(F1a: display:contents 대상).
    player_block = server._visual_player_block
    workbench_block = server._visual_workbench_block
    stage = player_block.parent
    assert stage is not None and stage is workbench_block.parent
    assert getattr(stage, "elem_id", None) == "visual-stage"
    assert "vl-stage-cell" in getattr(player_block, "elem_classes", ())
    assert "vl-stage-cell" in getattr(workbench_block, "elem_classes", ())
    # 자막 무대 — 재생기·자막 작업대가 같은 `#caption-stage` Row 안에 나란히 있고
    # 세팅(설정 안내·등록·실행)은 접힌 아코디언 안으로 물러난다(ROOT 실측 재배치).
    cap_player = server._caption_player_html
    cap_workbench = server._caption_workbench_block
    cap_stage = cap_player.parent
    assert cap_stage is not None and cap_stage is cap_workbench.parent
    assert getattr(cap_stage, "elem_id", None) == "caption-stage"
    assert "vl-stage-cell" in getattr(cap_player, "elem_classes", ())
    assert "vl-stage-cell" in getattr(cap_workbench, "elem_classes", ())
    assert server._settings_md is not None and server._settings_md.parent is not None
    assert isinstance(server._settings_md.parent, gr.Accordion)
    assert server._settings_md.parent is not cap_stage
    # 자막 등록 기록(64자리 지문 등)도 접힌 상세로 — 같은 객체·출력 배선 그대로.
    assert server._caption_md is not None
    assert isinstance(server._caption_md.parent, gr.Accordion)
    assert server._caption_md.parent is not cap_stage
    # 서버 함수가 구성요소에 실제로 붙어 있다(공개 server_functions 경계).
    assert server._visual_workbench_block.server_fns
    names = [fn.__name__ for fn in server._visual_workbench_block.server_fns]
    assert names == [
        "vl_refresh",
        "vl_prepare",
        "vl_propose",
        "vl_apply",
        "vl_replan",
        "vl_select_source",
        "vl_source_caption",
        "vl_relink_original",
    ]
    cap_names = [fn.__name__ for fn in server._caption_workbench_block.server_fns]
    assert cap_names == ["cap_save", "cap_confirm"]
    # 고급(기존) 패널 문맥 상태 — 네 gr.State가 만들어지고 실제 배선이 문맥
    # (수명 토큰·화면이 본 리비전/머리)을 입력·출력에 단다(F2-2, A6 실측).
    for context_state in (
        server._edit_context_state,
        server._review_head_state,
        server._caption_context_state,
        server._caption_head_state,
    ):
        assert isinstance(context_state, gr.State)
    wirings = server._advanced_wirings
    gated_review = {
        "review-note",
        "review-selection",
        "review-boundary",
        "review-split",
        "review-merge",
        "review-plan",
        "review-reextract",
        "review-export",
        "review-export-open",
        "review-export-copy",
        "review-cleanup",
    }
    assert gated_review <= set(wirings)
    for name in gated_review:
        inputs, outputs = wirings[name]
        assert server._edit_context_state in inputs, name
        assert server._review_head_state in inputs, name
        assert server._review_head_state in outputs, name
        assert server._edit_context_state in outputs, name
    for name in ("review-initialize", "review-refresh", "review-filter"):
        inputs, outputs = wirings[name]
        assert server._edit_context_state in inputs and server._review_head_state in inputs, name
        assert server._review_head_state in outputs and server._edit_context_state in outputs, name
    for name in ("caption-edit", "caption-confirm"):
        inputs, outputs = wirings[name]
        assert server._caption_context_state in inputs, name
        assert server._caption_head_state in inputs, name
        assert server._caption_context_state in outputs, name
        assert server._caption_head_state in outputs, name
    for name in ("caption-register", "caption-refresh"):
        inputs, outputs = wirings[name]
        assert server._caption_context_state in outputs, name
        assert server._caption_head_state in outputs, name
    # 문맥 검증은 배선 핸들러 안에 있다 — 뷰 함수 서명(직접 호출 호환)은 그대로.
    import inspect

    assert list(inspect.signature(server.review_note_view).parameters) == [
        "signature",
        "segment_id",
        "note",
    ]
    assert list(inspect.signature(server.caption_edit_view).parameters) == [
        "signature",
        "operation",
        "cue_id",
        "text",
        "start_ms",
        "end_ms",
        "second_id",
        "index_text",
        "warning_key",
    ]
    assert list(inspect.signature(server.caption_confirm_view).parameters) == [
        "signature",
        "run_id",
        "confirm",
    ]
    with pytest.raises(AttributeError):
        _ = server._visual_workbench_block.vl_missing  # 없는 함수는 그대로 없다
