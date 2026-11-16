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

WINDOW_CORE_JS = r"""
(function (root) {
  var WIN = root.__vlWindow || {};
  // 만진 자리 → 뷰어가 보일 쪽. 모르는 자리는 그대로 둔다.
  WIN.zoneMode = function (mode, zone) {
    if (zone === "browser" || zone === "source") { return "source"; }
    if (zone === "deck" || zone === "program") { return "program"; }
    return mode;
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
  function settle() {
    if (!WIN.stageNode) {
      var found = document.querySelector("#" + CFG.deck + " .tl-root:not(.tl-empty)");
      if (found) { WIN.adoptProgram(found); }
    }
    if (WIN.stageNode && !WIN.stageNode.isConnected) { ensureProgram(); }
    ensureTabs();
    paint();
  }
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
"""
