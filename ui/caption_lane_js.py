"""파형 자막 레인(Subtitle Edit식, W-87b, D149) — 순수 계산. 노드에서도 돈다(`module.exports`).

Subtitle Edit 5.2.0 파형 조작(정적 분석 — `docs/plans/2026-11-16-fcp-window.md`)을 원본 범위
타임라인의 시퀀스 틱 위에 옮긴다: 블록 맞히기(가장자리 · 몸통), 가장자리 끌기(이웃 자막 ·
최소 간격 · 최소 길이 안), 몸통 끌기(이웃 사이), 빈 곳 끌기(새 선택), 컷 경계 스냅(⇧이면 안
붙는다), 휠 확대. 여기는 **틱을 고를 뿐** 문서를 고치지 않는다 — 고른 틱을 원본 밀리초로 바꿔
문서 연산(`TL.cueSetTimes`·`cueShift`·`cueInsert`)에 넘기는 것은 재생기다(규칙 3).

보기(view)는 `{start, tpp}` — 레인 왼쪽 끝의 틱과 화소 하나의 틱.
자막(cues)은 `TL.project(doc).cues`의 줄 — `[아이디, 원본, 시작 틱, 끝 틱, 글, 잘림]`.
"""

from __future__ import annotations

CAPTION_LANE_JS = r"""
(function (root) {
  var L = {};
  L.x = function (view, ticks) { return (ticks - view.start) / view.tpp; };
  L.ticksAt = function (view, x) { return view.start + x * view.tpp; };
  // 맞힌 블록과 자리 — 가장자리(edgePx 안)가 몸통보다 먼저, 가까운 쪽이 이긴다. 없으면 null.
  L.hit = function (cues, view, x, edgePx) {
    var best = null;
    cues.forEach(function (cue, index) {
      var xs = L.x(view, cue[2]);
      var xe = L.x(view, cue[3]);
      var ds = Math.abs(x - xs);
      var de = Math.abs(x - xe);
      var part = null;
      var distance = 0;
      if (ds <= edgePx && ds <= de) { part = "start"; distance = ds; }
      else if (de <= edgePx) { part = "end"; distance = de; }
      else if (xs <= x && x <= xe) { part = "body"; distance = edgePx + 1; }
      if (part && (!best || distance < best.distance)) {
        best = { index: index, id: cue[0], part: part, distance: distance };
      }
    });
    return best ? { index: best.index, id: best.id, part: best.part } : null;
  };
  // 가까운 스냅 자리(컷 경계 등)로 — snapTicks 안에 없으면 그대로.
  L.snap = function (ticks, snaps, snapTicks) {
    var chosen = ticks;
    var gap = snapTicks;
    snaps.forEach(function (point) {
      var d = Math.abs(point - ticks);
      if (d <= gap) { gap = d; chosen = point; }
    });
    return chosen;
  };
  // 가장자리 끌기 — 앞(뒤) 자막과 최소 간격, 자기 길이는 최소 길이, 타임라인 [0, total] 안.
  L.edge = function (cues, index, part, ticks, minGap, minLength, total) {
    var cue = cues[index];
    var prev = cues[index - 1];
    var next = cues[index + 1];
    if (part === "start") {
      var low = prev ? prev[3] + minGap : 0;
      var high = cue[3] - minLength;
      return Math.round(Math.max(Math.min(ticks, high), Math.min(low, high)));
    }
    var floor = cue[2] + minLength;
    var ceiling = next ? next[2] - minGap : total;
    return Math.round(Math.min(Math.max(ticks, floor), Math.max(ceiling, floor)));
  };
  // 몸통 끌기 — 길이 그대로 옮기는 틱 수를 앞뒤 자막(최소 간격)·타임라인 끝 안으로.
  L.body = function (cues, index, delta, minGap, total) {
    var cue = cues[index];
    var prev = cues[index - 1];
    var next = cues[index + 1];
    var low = (prev ? prev[3] + minGap : 0) - cue[2];
    var high = (next ? next[2] - minGap : total) - cue[3];
    if (high < low) { return 0; }
    return Math.round(Math.max(low, Math.min(high, delta)));
  };
  // 새 선택 — 두 끝을 차례대로, 타임라인 안으로.
  L.range = function (a, b, total) {
    var lo = Math.max(0, Math.min(a, b));
    var hi = Math.min(total, Math.max(a, b));
    return [Math.round(lo), Math.round(hi)];
  };
  // 확대 — anchorX 화소 아래 틱을 제자리에 두고, 화소당 틱을 [minTpp, maxTpp] 안으로.
  L.zoom = function (view, factor, anchorX, minTpp, maxTpp) {
    var tpp = Math.max(minTpp, Math.min(maxTpp, view.tpp / factor));
    var anchor = L.ticksAt(view, anchorX);
    return { start: anchor - anchorX * tpp, tpp: tpp };
  };
  // 보이게 — ticks가 보기 밖이면 그 자리를 가운데로 옮긴 보기, 안이면 그대로.
  L.follow = function (view, ticks, width) {
    var x = L.x(view, ticks);
    if (x >= 0 && x <= width) { return view; }
    return { start: ticks - (width / 2) * view.tpp, tpp: view.tpp };
  };
  // 휠 한 번의 확대 배율 — 마우스 한 칸(notchPx)이 한 걸음(stepPercent),
  // 트랙패드의 작은 델타는 작게.
  L.wheelFactor = function (deltaY, deltaMode, stepPercent, notchPx) {
    var step = stepPercent / 100;
    if (!deltaY) { return 1; }
    if (deltaMode !== 0) { return deltaY < 0 ? step : 1 / step; }
    return Math.pow(step, Math.max(-1, Math.min(1, -deltaY / notchPx)));
  };
  root.__vlCaptionLane = L;
  if (typeof module !== "undefined" && module.exports) { module.exports = L; }
})(typeof window !== "undefined" ? window : globalThis);
"""
