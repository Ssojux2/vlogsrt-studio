"""화면 틱 임대(W-85, D145) — 타임라인 덱을 만지는 동안 화면 틱(D143)을 쉬게 한다.

D134는 「최종 편집」 탭이 앞이면 틱을 쉬게 했다 — 틱 디스패치 하나가 그라디오 안에서
주 스레드를 막아 확대·끌기가 늦었다. 이제 타임라인이 「영상 정리」 창 아래 덱에 함께
있어 탭으로는 가를 수 없다. 그래서 **사람이 덱을 만지는 동안**(누름 · 휠 · 키) 또는 덱의
영상이 도는 동안 쉬고, 그 뒤 `idleMs` 동안 아무 일이 없거나 덱에서 초점이 나가면 다시
켠다. 쉬고 켜는 일은 숨은 단추 둘이 한다(처리기는 `RawWorkspaceUi.timers_rest`·
`timers_resume` — 갱신 하나, 대기열 밖).

`LEASE_CORE_JS`는 순수 상태 기계다(노드에서 시험한다 — `module.exports`). `lease_bind_js`가
문서에 청취자를 한 번 묶는다. 쉬는 동안에도 활동 띠(`/vl-activity`)와 재생기의 렌더 묻기
(`tl_job`)는 돈다.
"""

from __future__ import annotations

import json

DECK_ID = "vl-timeline-deck"
REST_BUTTON_ID = "vl-timers-rest"
RESUME_BUTTON_ID = "vl-timers-resume"
TICK_MS = 1000
"""유휴 확인 간격 — 브라우저 타이머 하나(그라디오 디스패치가 아니다)라 값이 거의 없다."""
IDLE_MS = 4000
"""덱을 놓은 뒤 화면 틱을 다시 켜기까지(외부 근거 없는 조정 대상, D145) — 끌기·휠 줌
사이의 짧은 멈춤에 틱이 끼어들지 않을 만큼 길고, 작업 상태가 늦게 보이는 것을 사람이
느끼지 못할 만큼 짧다(틱 주기 2초의 두 배)."""

LEASE_CORE_JS = r"""
(function (root) {
  // 상태: { resting, last } — last는 마지막 상호작용(또는 재생 중 확인) 시각(ms).
  // 사건: "interact" · "tick"(playing 함께) · "blur". 돌려주는 click은 누를 숨은 단추(없으면 null).
  var LEASE = {
    initial: function () { return { resting: false, last: 0 }; },
    step: function (state, event, now, idleMs, playing) {
      var next = { resting: state.resting, last: state.last };
      if (event === "interact") {
        next.last = now;
        if (!state.resting) { next.resting = true; return { state: next, click: "rest" }; }
        return { state: next, click: null };
      }
      if (!state.resting) { return { state: next, click: null }; }
      if (playing) { next.last = now; return { state: next, click: null }; }
      if (event === "blur" || (event === "tick" && now - state.last >= idleMs)) {
        next.resting = false;
        return { state: next, click: "resume" };
      }
      return { state: next, click: null };
    }
  };
  root.__vlTimerLease = LEASE;
  if (typeof module !== "undefined" && module.exports) { module.exports = LEASE; }
})(typeof window !== "undefined" ? window : globalThis);
"""

_BIND_JS = r"""
(function () {
  var W = window;
  if (W.__vlTimerLeaseBound || typeof document.addEventListener !== "function") { return; }
  W.__vlTimerLeaseBound = true;
  var CFG = __CONFIG__;
  var lease = W.__vlTimerLease.initial();
  function deck() { return document.getElementById(CFG.deck); }
  function inDeck(node) {
    var d = deck();
    return !!(d && node && typeof d.contains === "function" && d.contains(node));
  }
  function playing() {
    var d = deck();
    if (!d) { return false; }
    var videos = d.querySelectorAll("video");
    for (var i = 0; i < videos.length; i += 1) {
      if (!videos[i].paused && !videos[i].ended) { return true; }
    }
    return false;
  }
  function apply(event) {
    var out = W.__vlTimerLease.step(lease, event, Date.now(), CFG.idleMs, playing());
    lease = out.state;
    if (!out.click) { return; }
    var button = document.getElementById(out.click === "rest" ? CFG.rest : CFG.resume);
    if (button) { button.click(); }
  }
  function onInteract(ev) { if (inDeck(ev.target)) { apply("interact"); } }
  ["pointerdown", "wheel", "keydown"].forEach(function (name) {
    document.addEventListener(name, onInteract, { capture: true, passive: true });
  });
  document.addEventListener("focusout", function (ev) {
    if (inDeck(ev.target) && !inDeck(ev.relatedTarget)) { apply("blur"); }
  }, true);
  setInterval(function () { apply("tick"); }, CFG.tickMs);
})();
"""


def lease_bind_js(idle_ms: int = IDLE_MS) -> str:
    """순수 상태 기계 + 문서 청취자 — 화면 숫자는 설정에서 데이터로 싣는다."""
    config = {
        "deck": DECK_ID,
        "rest": REST_BUTTON_ID,
        "resume": RESUME_BUTTON_ID,
        "idleMs": idle_ms,
        "tickMs": TICK_MS,
    }
    return LEASE_CORE_JS + _BIND_JS.replace("__CONFIG__", json.dumps(config))
