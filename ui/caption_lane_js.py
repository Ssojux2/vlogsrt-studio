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
  // 클립 띠(W-100, D169) — 레인 위쪽에 클립마다 블록. 맞힌 클립과 자리(start · end · body).
  // 가장자리는 edgePx 안에서 가까운 쪽이 이기고, 같으면 x를 몸통에 담은 클립이 이긴다(맞닿은
  // 두 클립 사이에서는 누른 쪽 클립의 가장자리). spans는 배치 줄({outStart, outFrames})이다.
  L.clipHit = function (spans, view, x, edgePx, seqTicks) {
    var best = null;
    spans.forEach(function (span, index) {
      var xs = L.x(view, span.outStart * seqTicks);
      var xe = L.x(view, (span.outStart + span.outFrames) * seqTicks);
      var inside = xs <= x && x < xe;
      [["start", Math.abs(x - xs)], ["end", Math.abs(x - xe)]].forEach(function (pair) {
        if (pair[1] > edgePx) { return; }
        var better = !best || pair[1] < best.distance ||
          (pair[1] === best.distance && inside && !best.inside);
        if (better) { best = { index: index, part: pair[0], distance: pair[1], inside: inside }; }
      });
      if (!best && inside) { best = { index: index, part: "body", distance: edgePx + 1 }; }
    });
    return best ? { index: best.index, part: best.part } : null;
  };
  // 클립 가장자리 끌기 — 끈 틱만큼 원본 프레임을 옮긴다(가장 가까운 프레임). 갈 수 있는 자리는
  // 재생기가 문서 규칙으로 다시 막는다.
  L.trimFrame = function (edgeFrame, deltaTicks, sourceFrameTicks) {
    return edgeFrame + Math.round(deltaTicks / sourceFrameTicks);
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
  // ⌥ 가장자리 끌기(W-87c, D152) — 맞닿은 이웃의 마주 보는 가장자리도 둘 사이 간격 그대로
  // 함께 간다(Subtitle Edit). 두 줄 모두 최소 길이 안. 이웃이 없으면 null — 보통 끌기로.
  L.edgePair = function (cues, index, part, ticks, minLength) {
    var cue = cues[index];
    if (part === "end") {
      var next = cues[index + 1];
      if (!next) { return null; }
      var gap = next[2] - cue[3];
      var lo = cue[2] + minLength;
      var hi = next[3] - minLength - gap;
      var end = Math.round(Math.min(Math.max(ticks, lo), Math.max(hi, lo)));
      return { value: end, neighbor: index + 1, neighborValue: end + gap };
    }
    var prev = cues[index - 1];
    if (!prev) { return null; }
    var gapBefore = cue[2] - prev[3];
    var low = prev[2] + minLength + gapBefore;
    var high = cue[3] - minLength;
    var start = Math.round(Math.max(Math.min(ticks, high), Math.min(low, high)));
    return { value: start, neighbor: index - 1, neighborValue: start - gapBefore };
  };
  // 여러 줄 몸통 끌기 — 고른 줄을 같은 틱만큼, 고르지 않은 이웃(최소 간격)·타임라인 끝 안으로.
  L.bodyMany = function (cues, indices, delta, minGap, total) {
    var picked = {};
    indices.forEach(function (i) { picked[i] = true; });
    var low = -Infinity;
    var high = Infinity;
    indices.forEach(function (i) {
      var cue = cues[i];
      var prev = cues[i - 1];
      var next = cues[i + 1];
      if (!picked[i - 1]) { low = Math.max(low, (prev ? prev[3] + minGap : 0) - cue[2]); }
      if (!picked[i + 1]) { high = Math.min(high, (next ? next[2] - minGap : total) - cue[3]); }
    });
    if (!indices.length || high < low) { return 0; }
    return Math.round(Math.max(low, Math.min(high, delta)));
  };
  // 목록 고르기 — "one"(그 줄만) · "toggle"(Mod 누름, 넣고 빼기) · "range"(⇧ 누름, 기준부터 그
  // 줄까지). ids는 보이는 차례이고 고른 줄도 그 차례로 돌려준다.
  L.pick = function (ids, picked, anchor, id, how) {
    if (how === "toggle") {
      var had = picked.indexOf(id) >= 0;
      var kept = had ? picked.filter(function (p) { return p !== id; }) : picked.concat([id]);
      return { picked: ids.filter(function (p) { return kept.indexOf(p) >= 0; }), anchor: id };
    }
    if (how === "range" && anchor !== null && ids.indexOf(anchor) >= 0) {
      var a = ids.indexOf(anchor);
      var b = ids.indexOf(id);
      return { picked: ids.slice(Math.min(a, b), Math.max(a, b) + 1), anchor: anchor };
    }
    return { picked: [id], anchor: id };
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
  // 창에 맞추기(⇧Z) — 타임라인 전체가 width 화소에 들게, 화소당 틱을 [minTpp, maxTpp] 안으로.
  L.fit = function (total, width, minTpp, maxTpp) {
    var tpp = Math.max(minTpp, Math.min(maxTpp, total / Math.max(1, width)));
    return { start: 0, tpp: tpp };
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
