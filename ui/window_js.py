"""Final Cut식 창의 뷰어 하나 · 인스펙터 탭(W-86b, D148).

**뷰어 하나** — Final Cut 뷰어처럼 위 덱의 뷰어 칸 하나가 마지막으로 만진 쪽을 보인다: 브라우저
카드·원본 재생기(레일 포함)를 만지면 원본(source), 아래 덱 타임라인을 만지면 타임라인(program).
타임라인 재생기의 화면(`.tl-stage` — 영상 둘 · 자막 겹 · 「준비 안 됨」)은 덱에서 뷰어 칸 안의
`#vl-program`으로 옮겨 두고(노드를 옮길 뿐 다시 만들지 않는다 — 청취자·재생이 그대로다), 무대의
`data-vl-viewer`가 어느 쪽을 보일지 정한다. 바꿀 때 다른 쪽 영상은 멈춘다. 그라디오가 뷰어 칸을
다시 그려 `#vl-program`이 사라지면 같은 노드를 새 칸에 다시 넣는다(`MutationObserver`).

**인스펙터 탭** — 오른쪽 열(`#vl-quote`)을 「분석」(승인 범위 · 교차 확인)과 「공유」(초안
결과 · 영어 변환 · 자막 입히기)로 나눠 한 번에 하나만 보인다. 고른 탭은 이 브라우저에만
기억한다(실패해도 된다).

`WINDOW_CORE_JS`의 `zoneMode`·`INSPECTOR_TABS`는 순수하다(노드 시험).
"""

from __future__ import annotations

import json

PROGRAM_ID = "vl-program"
STAGE_ID = "visual-stage"
VIEWER_ID = "visual-preview"
DECK_ID = "vl-timeline-deck"
INSPECTOR_ID = "vl-quote"
SOURCE, PROGRAM = "source", "program"
ANALYSIS, SHARE = "analysis", "share"
INSPECTOR_TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (ANALYSIS, "분석", ("vl-approval-mode", "vl-cross-check")),
    (
        SHARE,
        "공유",
        (
            "vl-export-card",
            "vl-export-open",
            "vl-export-srt-again",
            "vl-export-edit",
            "vl-srt-english",
            "vl-burn",
        ),
    ),
)
STORAGE_KEY = "vlogsrt.inspectorTab"
FIT_STORAGE_KEY = "vlogsrt.fitWindow"
FIT_ON = "on"
SPLIT_STORAGE_KEY = "vlogsrt.trackSplit"
CAPTIONS_ID = "vl-captions"
WORKSPACE_STORAGE_KEY = "vlogsrt.workspace"
BASIC, CAPTIONS = "basic", "captions"
WORKSPACES: tuple[tuple[str, str], ...] = ((BASIC, "기본"), (CAPTIONS, "자막"))

WINDOW_CORE_JS = r"""
(function (root) {
  var WIN = root.__vlWindow || {};
  // 만진 자리 → 뷰어가 보일 쪽. 모르는 자리는 그대로 둔다.
  WIN.zoneMode = function (mode, zone) {
    if (zone === "browser" || zone === "source") { return "source"; }
    if (zone === "deck" || zone === "program") { return "program"; }
    return mode;
  };
  // 경계 끌기 — 두 칸(fr 비율 a·b, 지금 폭 pxA·pxB)의 경계를 delta 화소 옮긴
  // 새 비율. 칸마다 최소 폭.
  WIN.splitFr = function (frA, frB, pxA, pxB, delta, minA, minB) {
    var total = pxA + pxB;
    var a = Math.max(minA, Math.min(total - minB, pxA + delta));
    var fr = frA + frB;
    return [fr * a / total, fr * (total - a) / total];
  };
  root.__vlWindow = WIN;
  if (typeof module !== "undefined" && module.exports) { module.exports = WIN; }
})(typeof window !== "undefined" ? window : globalThis);
"""

_BIND_JS = r"""
(function () {
  var W = window;
  var WIN = W.__vlWindow;
  var CFG = __CONFIG__;
  if (WIN.bound || typeof document.addEventListener !== "function") { return; }
  WIN.bound = true;
  WIN.mode = CFG.source;
  WIN.stageNode = null;
  WIN.rootEl = null;
  function $(id) { return document.getElementById(id); }
  function within(node, id) { var el = $(id); return !!(el && node && el.contains(node)); }
  function zoneOf(node) {
    if (within(node, CFG.program)) { return "program"; }
    if (within(node, CFG.deck)) { return "deck"; }
    if (within(node, CFG.viewer)) { return "source"; }
    var thumbs = document.getElementById("vl-thumbs");
    if (thumbs && thumbs.contains(node)) { return "browser"; }
    if (within(node, "visual-timeline")) { return "source"; }
    return "other";
  }
  function pauseAll(container) {
    if (!container) { return; }
    Array.prototype.forEach.call(container.querySelectorAll("video"), function (v) {
      if (!v.paused) { v.pause(); }
    });
  }
  // 타임라인 화면을 뷰어 칸으로 — 칸이 없으면 만들고, 다시 그려져 빠졌으면 같은 노드를 다시 넣는다.
  function ensureProgram() {
    var viewer = $(CFG.viewer);
    if (!viewer || !WIN.stageNode) { return; }
    var holder = $(CFG.program);
    if (!holder) {
      holder = document.createElement("div");
      holder.id = CFG.program;
      holder.className = "vl-program";
      holder.addEventListener("pointerdown", function () {
        if (WIN.rootEl) { WIN.rootEl.focus({ preventScroll: true }); }
      });
      viewer.appendChild(holder);
    }
    if (WIN.stageNode.parentNode !== holder) {
      while (holder.firstChild) { holder.removeChild(holder.firstChild); }
      holder.appendChild(WIN.stageNode);
    }
    paint();
  }
  WIN.adoptProgram = function (rootEl) {
    if (!rootEl) { return; }
    var stage = rootEl.querySelector(".tl-stage");
    WIN.rootEl = rootEl;
    if (stage) { WIN.stageNode = stage; }
    ensureProgram();
    placeCaptions();
  };
  // 타임라인이 없어지면(라이브러리 닫기 · 빈 모델) 뷰어 칸을 비우고 원본으로 돌아간다.
  WIN.releaseProgram = function () {
    var holder = $(CFG.program);
    if (holder) { while (holder.firstChild) { holder.removeChild(holder.firstChild); } }
    WIN.stageNode = null;
    WIN.rootEl = null;
    WIN.mode = CFG.source;
    paint();
  };
  function paint() {
    var stage = $(CFG.stage);
    if (!stage) { return; }
    var has = !!(WIN.stageNode && WIN.stageNode.isConnected);
    stage.setAttribute("data-vl-viewer", has ? WIN.mode : CFG.source);
  }
  WIN.setMode = function (mode) {
    if (mode === WIN.mode) { return; }
    WIN.mode = mode;
    if (mode === CFG.program) {
      var surface = $(CFG.viewer);
      Array.prototype.forEach.call(surface ? surface.querySelectorAll("video") : [], function (v) {
        if (!(WIN.stageNode && WIN.stageNode.contains(v)) && !v.paused) { v.pause(); }
      });
    } else {
      pauseAll(WIN.stageNode);
    }
    paint();
  };
  function onTouch(ev) {
    var next = WIN.zoneMode(WIN.mode, zoneOf(ev.target));
    if (next !== WIN.mode) { WIN.setMode(next); }
  }
  ["pointerdown", "keydown"].forEach(function (name) {
    document.addEventListener(name, onTouch, true);
  });
  document.addEventListener("play", function (ev) {
    var zone = zoneOf(ev.target);
    if (zone === "program") { WIN.setMode(CFG.programMode); }
    else if (zone === "source") { WIN.setMode(CFG.source); }
  }, true);
  // ─── 인스펙터 탭 ───────────────────────────────────────────────
  function readTab() {
    try { return W.localStorage.getItem(CFG.storageKey) || CFG.tabs[0][0]; }
    catch (error) { return CFG.tabs[0][0]; }
  }
  function writeTab(key) {
    try { W.localStorage.setItem(CFG.storageKey, key); } catch (error) { /* 기억은 편의일 뿐 */ }
  }
  function ensureTabs() {
    var inspector = $(CFG.inspector);
    if (!inspector) { return; }
    var bar = inspector.querySelector(".vl-inspector-tabs");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "vl-inspector-tabs";
      bar.setAttribute("role", "tablist");
      bar.innerHTML = CFG.tabs.map(function (tab) {
        return '<button type="button" role="tab" data-vl-tab="' + tab[0] + '">' + tab[1] +
          "</button>";
      }).join("");
      bar.addEventListener("click", function (ev) {
        var button = ev.target.closest ? ev.target.closest("[data-vl-tab]") : null;
        if (!button) { return; }
        writeTab(button.getAttribute("data-vl-tab"));
        paintTabs();
      });
      inspector.insertBefore(bar, inspector.firstChild);
    }
    paintTabs();
  }
  function paintTabs() {
    var inspector = $(CFG.inspector);
    if (!inspector) { return; }
    var key = readTab();
    inspector.setAttribute("data-vl-inspector", key);
    Array.prototype.forEach.call(inspector.querySelectorAll("[data-vl-tab]"), function (b) {
      b.setAttribute("aria-selected", b.getAttribute("data-vl-tab") === key ? "true" : "false");
    });
  }
  WIN.showInspector = function (key) { writeTab(key); paintTabs(); };
  // ─── 트랙 손잡이(W-86d) — 열 경계 둘 · (화면 맞춤이면) 위·아래 덱 경계. 비율은 무대 변수 ───
  function readSplit() {
    try { return JSON.parse(W.localStorage.getItem(CFG.splitKey) || "{}") || {}; }
    catch (error) { return {}; }
  }
  function writeSplit(value) {
    try { W.localStorage.setItem(CFG.splitKey, JSON.stringify(value)); }
    catch (error) { /* 편의 */ }
  }
  function frOf(name, saved) {
    if (saved[name]) { return saved[name]; }
    var value = getComputedStyle($(CFG.stage)).getPropertyValue("--vl-" + name + "-fr");
    return parseFloat(value) || 1;
  }
  function pxOf(name) {
    var value = getComputedStyle($(CFG.stage)).getPropertyValue("--vl-" + name);
    return parseFloat(value) || 0;
  }
  function applySplit() {
    var stage = $(CFG.stage);
    if (!stage) { return; }
    var saved = readSplit();
    Object.keys(saved).forEach(function (name) {
      stage.style.setProperty("--vl-" + name + "-fr", saved[name] + "fr");
    });
  }
  var SPLITS = [
    { key: "left", a: "browser", b: "viewer", elA: ["vl-thumbs", CFG.captions], elB: CFG.viewer,
      axis: "x",
      minA: "browser-min", minB: "viewer-min" },
    { key: "right", a: "viewer", b: "inspector", elA: CFG.viewer, elB: CFG.inspector, axis: "x",
      minA: "viewer-min", minB: "inspector-min" },
    // 위 덱의 높이는 브라우저 열이 잰다 — 그 열만 위 덱의 두 줄(뷰어 · 원본 레일)을 다 덮는다.
    { key: "deck", a: "upper-deck", b: "lower-deck", elA: ["vl-thumbs", CFG.captions],
      elB: CFG.deck, axis: "y",
      minA: "deck-min", minB: "deck-min" }
  ];
  // 칸 이름 하나 또는 여럿(보이는 첫 칸 — 「자막」 작업공간이면 브라우저 열이 자막 칸이다).
  function paneOf(ids) {
    var list = typeof ids === "string" ? [ids] : ids;
    for (var i = 0; i < list.length; i++) {
      var el = $(list[i]);
      if (el && el.offsetParent) { return el; }
    }
    return $(list[0]);
  }
  function placeHandles() {
    var stage = $(CFG.stage);
    if (!stage) { return; }
    var box = stage.getBoundingClientRect();
    SPLITS.forEach(function (split) {
      var handle = stage.querySelector('.vl-split[data-split="' + split.key + '"]');
      if (!handle) {
        handle = document.createElement("div");
        handle.className = "vl-split";
        handle.setAttribute("data-split", split.key);
        handle.setAttribute("role", "separator");
        handle.setAttribute("aria-orientation", split.axis === "x" ? "vertical" : "horizontal");
        handle.addEventListener("pointerdown", function (ev) { startSplit(ev, split, handle); });
        stage.appendChild(handle);
      }
      var a = paneOf(split.elA);
      var b = paneOf(split.elB);
      var fit = stage.getAttribute("data-vl-fit") === CFG.fitOn;
      var show = a && b && a.offsetParent && b.offsetParent && (split.axis === "x" || fit);
      var ra = show ? a.getBoundingClientRect() : null;
      var rb = show ? b.getBoundingClientRect() : null;
      // 좁은 화면은 칸을 쌓는다 — 나란히(가로) · 위아래(세로)로 붙어 있을 때만 손잡이를 보인다.
      if (show) { show = split.axis === "x" ? ra.right <= rb.left + 1 : ra.bottom <= rb.top + 1; }
      handle.hidden = !show;
      if (!show) { return; }
      if (split.axis === "x") {
        handle.style.left = ((ra.right + rb.left) / 2 - box.left) + "px";
        handle.style.top = (ra.top - box.top) + "px";
        handle.style.height = Math.max(ra.height, 24) + "px";
      } else {
        handle.style.top = ((ra.bottom + rb.top) / 2 - box.top) + "px";
        handle.style.left = "0px";
        handle.style.width = box.width + "px";
      }
    });
  }
  function startSplit(ev, split, handle) {
    var stage = $(CFG.stage);
    var a = paneOf(split.elA);
    var b = paneOf(split.elB);
    if (!stage || !a || !b) { return; }
    ev.preventDefault();
    var saved = readSplit();
    var size = split.axis === "x" ? "width" : "height";
    var start = { at: split.axis === "x" ? ev.clientX : ev.clientY,
      pxA: a.getBoundingClientRect()[size], pxB: b.getBoundingClientRect()[size],
      frA: frOf(split.a, saved), frB: frOf(split.b, saved) };
    try { handle.setPointerCapture(ev.pointerId); }
    catch (error) { /* 잡지 못해도 손잡이 위 끌기는 된다 */ }
    function move(e) {
      var delta = (split.axis === "x" ? e.clientX : e.clientY) - start.at;
      var next = WIN.splitFr(start.frA, start.frB, start.pxA, start.pxB, delta,
        pxOf(split.minA), pxOf(split.minB));
      saved[split.a] = Math.round(next[0] * 100) / 100;
      saved[split.b] = Math.round(next[1] * 100) / 100;
      stage.style.setProperty("--vl-" + split.a + "-fr", saved[split.a] + "fr");
      stage.style.setProperty("--vl-" + split.b + "-fr", saved[split.b] + "fr");
      placeHandles();
    }
    function up() {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      writeSplit(saved);
    }
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  }
  // 두 번 누르면 그 경계를 기본 비율로 돌린다.
  document.addEventListener("dblclick", function (ev) {
    var handle = ev.target.closest ? ev.target.closest(".vl-split") : null;
    if (!handle) { return; }
    var key = handle.getAttribute("data-split");
    var split = SPLITS.filter(function (s) { return s.key === key; })[0];
    var saved = readSplit();
    delete saved[split.a];
    delete saved[split.b];
    writeSplit(saved);
    $(CFG.stage).style.removeProperty("--vl-" + split.a + "-fr");
    $(CFG.stage).style.removeProperty("--vl-" + split.b + "-fr");
    placeHandles();
  });
  // ─── 화면 맞춤(W-86c) — 켜면 무대를 화면 높이에 묶고 위·아래 덱을 토큰 비율로 나눈다 ───
  function readFit() {
    try { return W.localStorage.getItem(CFG.fitKey) === CFG.fitOn; } catch (error) { return false; }
  }
  function writeFit(on) {
    try { W.localStorage.setItem(CFG.fitKey, on ? CFG.fitOn : ""); } catch (error) { /* 편의 */ }
  }
  function paintFit() {
    var stage = $(CFG.stage);
    var on = readFit();
    if (stage) {
      if (on) { stage.setAttribute("data-vl-fit", CFG.fitOn); }
      else { stage.removeAttribute("data-vl-fit"); }
    }
    var button = document.getElementById("vl-fit-toggle");
    if (button) { button.setAttribute("aria-pressed", on ? "true" : "false"); }
    placeHandles();
  }
  function ensureFit() {
    var deck = $(CFG.deck);
    if (!deck) { return; }
    if (!deck.querySelector(".vl-deck-tools")) {
      var tools = document.createElement("div");
      tools.className = "vl-deck-tools";
      tools.innerHTML = '<span class="vl-workspaces" role="group" aria-label="작업공간">' +
        CFG.workspaces.map(function (w) {
          return '<button type="button" data-vl-workspace-pick="' + w[0] + '"' +
            ' aria-pressed="false" title="작업공간 — ' + w[1] + '">' + w[1] + "</button>";
        }).join("") + "</span>" +
        '<button type="button" id="vl-fit-toggle" aria-pressed="false"' +
        ' title="위 덱 · 아래 덱을 화면 높이에 맞춰 나눈다(Final Cut 창처럼)">화면 맞춤</button>';
      tools.addEventListener("click", function (ev) {
        var pick = ev.target.closest ? ev.target.closest("[data-vl-workspace-pick]") : null;
        if (pick) { WIN.setWorkspace(pick.getAttribute("data-vl-workspace-pick")); return; }
        if (!(ev.target.closest && ev.target.closest("#vl-fit-toggle"))) { return; }
        writeFit(!readFit());
        paintFit();
      });
      deck.insertBefore(tools, deck.firstChild);
    }
    paintFit();
  }
  // ─── 작업공간(W-87c, D152) — 「기본」 · 「자막」(Subtitle Edit식). 「자막」이면 재생기의 자막
  // 목록 · 입력칸(`.tl-cues-body`)을 노드째 브라우저 열의 자막 칸으로 옮기고(청취자 그대로) 뷰어는
  // 타임라인, 아래 덱의 파형 레인은 키운다. 「기본」으로 오면 제자리(재생기의 자막 칸)로 돌린다.
  function readWorkspace() {
    try { return W.localStorage.getItem(CFG.workspaceKey) === CFG.captionsMode ? CFG.captionsMode
      : CFG.basicMode; }
    catch (error) { return CFG.basicMode; }
  }
  function writeWorkspace(key) {
    try { W.localStorage.setItem(CFG.workspaceKey, key); } catch (error) { /* 편의 */ }
  }
  function captionsHolder() {
    var holder = $(CFG.captions);
    var stage = $(CFG.stage);
    if (!holder && stage) {
      holder = document.createElement("div");
      holder.id = CFG.captions;
      holder.setAttribute("data-vl-own-keys", "");
      holder.setAttribute("aria-label", "자막 목록 · 입력칸");
      stage.appendChild(holder);
    }
    return holder;
  }
  function placeCaptions() {
    var key = readWorkspace();
    var stage = $(CFG.stage);
    if (stage) { stage.setAttribute("data-vl-workspace", key); }
    var live = "#" + CFG.deck + " .tl-root:not(.tl-empty) ";
    var deckBody = document.querySelector(live + ".tl-cues-body");
    var holder = $(CFG.captions);
    if (key === CFG.captionsMode) {
      holder = captionsHolder();
      if (holder && deckBody) {
        while (holder.firstChild) { holder.removeChild(holder.firstChild); }
        holder.appendChild(deckBody);
      }
    } else if (holder && holder.firstChild) {
      var home = document.querySelector(live + ".tl-cues");
      var moved = holder.querySelector(".tl-cues-body");
      if (home && moved && !home.querySelector(".tl-cues-body")) { home.appendChild(moved); }
      while (holder.firstChild) { holder.removeChild(holder.firstChild); }
    }
    Array.prototype.forEach.call(document.querySelectorAll("[data-vl-workspace-pick]"),
      function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-vl-workspace-pick") === key
          ? "true" : "false");
      });
  }
  WIN.setWorkspace = function (key) {
    writeWorkspace(key);
    placeCaptions();
    if (key === CFG.captionsMode && WIN.stageNode) { WIN.setMode(CFG.programMode); }
    placeHandles();
  };
  function settle() {
    if (!WIN.stageNode) {
      var found = document.querySelector("#" + CFG.deck + " .tl-root:not(.tl-empty)");
      if (found) { WIN.adoptProgram(found); }
    }
    if (WIN.stageNode && !WIN.stageNode.isConnected) { ensureProgram(); }
    ensureTabs();
    ensureFit();
    placeCaptions();
    applySplit();
    placeHandles();
    watchSizes();
    paint();
  }
  // 칸 크기는 글자·그림이 늦게 들어와도 바뀐다(문서 노드는 그대로) — 크기를 지켜 손잡이를 옮긴다.
  var sizeWatch = null;
  var sizePending = false;
  function watchSizes() {
    if (typeof ResizeObserver !== "function") { return; }
    if (!sizeWatch) {
      sizeWatch = new ResizeObserver(function () {
        if (sizePending) { return; }
        sizePending = true;
        W.requestAnimationFrame(function () { sizePending = false; placeHandles(); });
      });
    }
    [CFG.stage, "vl-thumbs", CFG.viewer, CFG.inspector, CFG.deck].forEach(function (id) {
      var el = $(id);
      if (el) { sizeWatch.observe(el); }
    });
  }
  W.addEventListener("resize", function () { placeHandles(); });
  if (typeof MutationObserver === "function") {
    var pending = false;
    new MutationObserver(function () {
      if (pending) { return; }
      pending = true;
      W.requestAnimationFrame(function () { pending = false; settle(); });
    }).observe(document.body, { childList: true, subtree: true });
  }
  settle();
})();
"""


def window_bind_js() -> str:
    """순수 핵심 + 문서 청취자 — 자리 이름·탭은 데이터로 싣는다."""
    config = {
        "program": PROGRAM_ID,
        "stage": STAGE_ID,
        "viewer": VIEWER_ID,
        "deck": DECK_ID,
        "inspector": INSPECTOR_ID,
        "source": SOURCE,
        "programMode": PROGRAM,
        "storageKey": STORAGE_KEY,
        "fitKey": FIT_STORAGE_KEY,
        "fitOn": FIT_ON,
        "splitKey": SPLIT_STORAGE_KEY,
        "captions": CAPTIONS_ID,
        "workspaceKey": WORKSPACE_STORAGE_KEY,
        "basicMode": BASIC,
        "captionsMode": CAPTIONS,
        "workspaces": [list(pair) for pair in WORKSPACES],
        "tabs": [[key, label, list(ids)] for key, label, ids in INSPECTOR_TABS],
    }
    return WINDOW_CORE_JS + _BIND_JS.replace("__CONFIG__", json.dumps(config, ensure_ascii=False))


def window_css() -> str:
    """뷰어 쪽과 인스펙터 탭 — 고른 쪽만 보인다(숨길 뿐 지우지 않는다)."""
    hide_share = ", ".join(
        f'#{INSPECTOR_ID}[data-vl-inspector="{ANALYSIS}"] #{elem}' for elem in INSPECTOR_TABS[1][2]
    )
    hide_analysis = ", ".join(
        f'#{INSPECTOR_ID}[data-vl-inspector="{SHARE}"] #{elem}' for elem in INSPECTOR_TABS[0][2]
    )
    return f"""
/* 뷰어 하나(W-86b, D148) — 원본(source)이면 원본 재생기, 타임라인(program)이면 타임라인 화면. */
#{PROGRAM_ID} {{ display: none; flex: 1 1 auto; min-height: 0; }}
#{STAGE_ID}[data-vl-viewer="{PROGRAM}"] #{PROGRAM_ID} {{ display: flex; flex-direction: column; }}
#{STAGE_ID}[data-vl-viewer="{PROGRAM}"] #{VIEWER_ID} .vl-video-surface {{ display: none; }}
#{PROGRAM_ID} .tl-stage {{ flex: 1 1 auto; }}
#{PROGRAM_ID} .tl-frame {{ height: var(--vl-deck-viewer-h); }}
/* 인스펙터 탭(W-86b) — 「분석」 · 「공유」. 고른 탭의 칸만 보인다. */
.vl-inspector-tabs {{ display: flex; gap: 0; border: 1px solid var(--vl-line-strong);
  border-radius: 4px; overflow: hidden; flex: none; background: var(--vl-surface-raised); }}
.vl-inspector-tabs button {{ flex: 1 1 0; height: 28px; border: 0; background: transparent;
  color: var(--vl-ink-muted); font: inherit; font-size: 12px; cursor: pointer; }}
.vl-inspector-tabs button + button {{ border-left: 1px solid var(--vl-line-strong); }}
.vl-inspector-tabs button[aria-selected="true"] {{ background: var(--vl-accent-soft);
  color: var(--vl-ink); box-shadow: inset 0 -2px 0 var(--vl-accent-ink); }}
{hide_share} {{ display: none !important; }}
{hide_analysis} {{ display: none !important; }}
/* 트랙 손잡이(W-86d, D151) — 열 경계 둘 · 화면 맞춤이면 위·아래 덱 경계.
   끌면 비율, 두 번 누르면 기본. */
#{STAGE_ID} {{ position: relative; }}
.vl-split {{ position: absolute; z-index: 5; touch-action: none; }}
.vl-split[hidden] {{ display: none; }}
.vl-split[aria-orientation="vertical"] {{ width: 8px; margin-left: -4px; cursor: col-resize; }}
.vl-split[aria-orientation="horizontal"] {{ height: 8px; margin-top: -4px; cursor: row-resize; }}
.vl-split:hover {{ background: var(--vl-accent-soft); }}
/* 화면 맞춤(W-86c, D150) — 덱 머리의 단추. 켜면(넓은 화면, 타임라인이 있을 때) 무대를
   화면 높이에 묶고 위 덱(재생기 줄)과 아래 덱을 토큰 비율로 나눈다 — 덱과 뷰어는 제 칸
   안에서 스크롤·맞춤한다. */
/* 「자막」 작업공간(W-87c, D152) — 브라우저 열에 자막 목록 · 입력칸, 파형 레인을 키운다. 재생기가
   없으면(자막 칸이 비면) 브라우저 그대로다. */
#{CAPTIONS_ID} {{ display: none; }}
#{STAGE_ID}[data-vl-workspace="{CAPTIONS}"]:has(#{CAPTIONS_ID} .tl-cues-body) #vl-thumbs {{
  display: none; }}
#{STAGE_ID}[data-vl-workspace="{CAPTIONS}"] #{CAPTIONS_ID}:has(.tl-cues-body) {{ display: flex;
  grid-area: thumbs; flex-direction: column; min-width: 0; min-height: 0; overflow: auto; }}
#{CAPTIONS_ID} .tl-cues-body {{ flex-direction: column; flex: 1 1 auto; min-height: 0; }}
#{CAPTIONS_ID} .tl-cue-list {{ flex: 1 1 auto; max-height: none; min-height: 160px; }}
#{STAGE_ID}[data-vl-workspace="{CAPTIONS}"] .tl-lane {{ height: var(--vl-caption-lane-h); }}
.vl-workspaces {{ display: inline-flex; margin-right: auto; }}
.vl-workspaces button + button {{ border-left: 0; }}
.vl-workspaces button[aria-pressed="true"] {{ background: var(--vl-accent-soft); }}
.vl-deck-tools {{ display: flex; justify-content: flex-end; gap: 6px; }}
.vl-deck-tools button {{ font: inherit; font-size: 12px; padding: 1px 8px; border-radius: 4px;
  border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised);
  color: var(--vl-ink); cursor: pointer; }}
.vl-deck-tools button[aria-pressed="true"] {{ background: var(--vl-accent-soft); }}
@media (min-width: 1051px) {{
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"]:has(#{DECK_ID} .tl-root:not(.tl-empty)) {{
    height: max(var(--vl-stage-min-h), calc(100vh - var(--vl-stage-top)));
    grid-template-areas: "thumbs player quote" "thumbs timeline ranges" "deck deck deck"
      "foot foot foot";
    grid-template-rows: minmax(0, var(--vl-upper-deck-fr)) auto
      minmax(0, var(--vl-lower-deck-fr)) var(--vl-statusline-h);
    --vl-viewer-video-h: auto; --vl-viewer-video-flex: 1 1 auto; }}
  /* 인스펙터는 위 덱 칸 안에서 스크롤 · 패널은 「고급 도구」 길이라 맞춤에서는 접는다. */
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"] #{INSPECTOR_ID} {{ max-height: none; min-height: 0; }}
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"] #vl-panel {{ display: none; }}
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"] #vl-ranges {{ max-height: var(--vl-rail-h); }}
  /* 원본 레일 · 추천 구간은 원본 쪽 칸이다 — 뷰어가 타임라인이면 접어 위 덱을 넓힌다. */
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"][data-vl-viewer="{PROGRAM}"] #visual-timeline,
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"][data-vl-viewer="{PROGRAM}"] #vl-ranges {{ display: none; }}
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"] #{DECK_ID} {{ min-height: 0; overflow: hidden auto; }}
  #{STAGE_ID}[data-vl-fit="{FIT_ON}"] #{PROGRAM_ID} .tl-frame {{ height: 100%; }}
}}
"""
