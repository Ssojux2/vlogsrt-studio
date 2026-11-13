"""원본 범위 타임라인 재생기(W-71c)의 화면 실행본 — `TIMELINE_CORE_JS`(`window.__vlTL`) 위에서 돈다.

상태는 `window.__vlTimelinePlayer` 하나에 산다. DOM은 사건 순간에 다시 찾는다(그라디오가 칸을 다시
그리면 원소가 바뀐다). 컷을 건널지·어디로 건널지는 순수 핵심 `TL.playStep`이 정하고, 여기서는 영상
둘(활성·대기)을 놓고 바꿔 틀 뿐이다. 원본 국소 프레임 f는 미리보기 `원점 + (f + ½)·T`초다(D117의
`seek`과 같은 가운데 규칙). 실측은 균일 원본뿐이다 — snap 원본은 규칙상 같은 프레임이지만 재지
않았고, map 원본은 렌더 계획이 빼므로 타임라인에 오지 않는다.
"""

from __future__ import annotations

PLAYER_APP_JS = r"""
(function () {
  var W = window;
  var TL = W.__vlTL;
  var S = W.__vlTimelinePlayer;
  if (!S) {
    S = W.__vlTimelinePlayer = { epoch: null, doc: null, spans: [], total: 0, playhead: 0, span: 0,
      deck: 0, playing: false, loopGen: 0, media: {}, status: "", statusError: false,
      pollGen: 0, sel: 0, cursor: null, drag: null, dirty: false, saving: false,
      history: { past: [], future: [] } };
  }
  S.server = server;
  var POLL_MS = 2000;

  function $(id) { return document.getElementById(id); }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  function rational(text) {
    var parts = String(text).split("/");
    return parts.length === 2 ? Number(parts[0]) / Number(parts[1]) : Number(text);
  }

  // ─── 모델 받기 ───────────────────────────────────────────────────
  function readModel() {
    var rootEl = element.querySelector ? element.querySelector("#tl-root") : null;
    if (!rootEl) { return null; }
    var holder = rootEl.querySelector("[data-tl-model]");
    if (!holder) { return null; }
    var raw = holder.textContent;
    if (rootEl === S.rootEl && raw === S.rawModel) { return null; }
    try { return { rootEl: rootEl, raw: raw, model: JSON.parse(raw) }; }
    catch (err) { return null; }
  }
  function boot() {
    var got = readModel();
    if (!got) { return; }
    S.rawModel = got.raw;
    if (got.model.epoch === S.epoch && S.rootEl === got.rootEl) { return; }
    var m = got.model;
    var same = S.timelineId === m.timelineId;
    // 같은 타임라인을 다시 그린 것이면 저장 전 편집과 되돌리기 기록을 지킨다.
    var keepLocal = same && S.dirty && S.doc;
    stopPlayback();
    S.rootEl = got.rootEl;
    S.epoch = m.epoch;
    S.pollGen += 1;
    if (m.empty) { S.doc = null; S.timelineId = null; return; }
    S.signature = m.signature;
    S.token = m.token;
    S.timelineId = m.timelineId;
    S.names = m.names || {};
    S.media = m.media || {};
    S.tiers = m.tiers || {};
    S.settings = m.settings || {};
    if (!keepLocal) {
      S.doc = m.doc;
      S.baseRevision = m.revision;
      S.dirty = false;
      S.history = { past: [], future: [] };
    }
    relayout();
    S.deck = 0;
    if (!same) {
      S.playhead = 0; S.sel = 0; S.cursor = null; S.status = ""; S.statusError = false;
    }
    bindDom();
    showDeck();
    seek(S.playhead);
    if (anyPreparing()) { poll(S.pollGen); }
  }

  // ─── 원본 · 미디어 ───────────────────────────────────────────────
  function sourceOf(id) {
    var list = S.doc.facts.sources;
    for (var i = 0; i < list.length; i++) { if (list[i].id === id) { return list[i]; } }
    return null;
  }
  function frameSeconds(id) { var s = sourceOf(id); return s.num / s.den; }
  function mediaOf(id) {
    var row = S.media[id];
    return row && row.state === "ready" && row.fileId && row.origin !== null ? row : null;
  }
  function timeOf(id, frame) {
    return rational(mediaOf(id).origin) + (frame + 0.5) * frameSeconds(id);
  }
  function frameOf(id, seconds, rounding) {
    return rounding((seconds - rational(mediaOf(id).origin)) / frameSeconds(id));
  }
  function nameOf(id) { return S.names[id] || id; }
  function notReady() {
    return Object.keys(S.media).filter(function (id) { return !mediaOf(id); });
  }
  function anyPreparing() {
    return Object.keys(S.media).some(function (id) { return S.media[id].state === "preparing"; });
  }

  // ─── 영상 둘 ─────────────────────────────────────────────────────
  // 활성 영상은 보이고 소리를 낸다. 대기 영상은 다음 컷 뒤 클립 시작에 맞춰 멈춰 둔다.
  function decks() { return [$("tl-deck-a"), $("tl-deck-b")]; }
  function active() { return decks()[S.deck]; }
  function standby() { return decks()[1 - S.deck]; }
  function showDeck() {
    decks().forEach(function (v, i) { if (v) { v.classList.toggle("is-standby", i !== S.deck); } });
  }
  // 영상 v를 원본 id의 프레임 frame에 놓는다 — 원본이 바뀌면 메타데이터를 받은 뒤 시크한다.
  function place(v, id, frame) {
    var media = mediaOf(id);
    if (!v || !media) { return false; }
    var src = "/media/" + encodeURIComponent(media.fileId);
    var t = timeOf(id, frame);
    v.__tlSource = id;
    v.__tlFrame = frame;
    if (v.getAttribute("src") !== src) {
      v.__tlPending = t;
      v.addEventListener("loadedmetadata", onDeckMeta);
      v.setAttribute("src", src);
      return true;
    }
    if (v.__tlPending !== undefined && v.__tlPending !== null) { v.__tlPending = t; return true; }
    if (v.seeking && v.__tlTarget === t) { return true; }
    v.__tlTarget = t;
    v.currentTime = t;
    return true;
  }
  function onDeckMeta(ev) {
    var v = ev.target;
    v.removeEventListener("loadedmetadata", onDeckMeta);
    var t = v.__tlPending;
    v.__tlPending = null;
    if (t !== undefined && t !== null) { v.__tlTarget = t; v.currentTime = t; }
  }
  function standbyState() {
    var b = standby();
    if (!b || b.__tlFrame === undefined || b.__tlFrame === null) { return null; }
    var settled = !b.seeking && b.readyState >= 2 && (b.__tlPending === undefined ||
      b.__tlPending === null);
    return { sourceId: b.__tlSource, frame: b.__tlFrame, ready: settled };
  }
  function prime(target) {
    var b = standby();
    if (!b || !target || !mediaOf(target.sourceId)) { return; }
    if (b.__tlSource === target.sourceId && b.__tlFrame === target.frame) { return; }
    if (!b.paused) { b.pause(); }
    place(b, target.sourceId, target.frame);
  }
  function nextCut() {
    var span = S.spans[S.span];
    var step = TL.playStep(S.doc, S.span, span.startFrame, null);
    return step.prime || step.target;
  }

  // ─── 재생 ────────────────────────────────────────────────────────
  function seek(frame) {
    if (!S.doc) { return; }
    S.playhead = clamp(Math.round(frame), 0, S.total);
    if (!S.spans.length) { paint(); return; }
    var at = TL.sourcePoint(S.doc, Math.min(S.playhead, S.total - 1));
    S.span = at[0];
    var id = S.spans[S.span].sourceId;
    if (!mediaOf(id)) {
      if (S.playing) { stopPlayback(); }
      paint();
      return;
    }
    place(active(), id, at[1]);
    prime(nextCut());
    paint();
  }
  function play() {
    var v = active();
    if (!v || !S.doc || !S.spans.length) { return; }
    if (S.playhead >= S.total) { seek(0); }
    var id = S.spans[S.span].sourceId;
    if (!mediaOf(id)) { say("「" + nameOf(id) + "」의 미리보기가 아직 없습니다.", true); return; }
    S.playing = true;
    S.loopGen += 1;
    var started = v.play();
    if (started && started.catch) {
      started.catch(function () { S.playing = false; paintTransport(); });
    }
    frameLoop(S.loopGen);
    paintTransport();
  }
  function stopPlayback() {
    decks().forEach(function (v) { if (v && !v.paused) { v.pause(); } });
    S.playing = false;
    S.loopGen += 1;
    paintTransport();
  }
  // 루프는 재생마다 하나 — 세대가 바뀌면(다시 재생·멈춤·교대) 옛 루프는 조용히 끝난다.
  function frameLoop(gen) {
    var v = active();
    if (!v || !S.playing || gen !== S.loopGen) { return; }
    if (!S.rootEl || S.rootEl.offsetParent === null) { stopPlayback(); return; }
    if (v.requestVideoFrameCallback) {
      v.requestVideoFrameCallback(function (now, meta) {
        if (gen !== S.loopGen) { return; }
        onFrame(v, meta.mediaTime, Math.round);
        frameLoop(gen);
      });
    } else {
      W.requestAnimationFrame(function () {
        if (gen !== S.loopGen) { return; }
        onFrame(v, v.currentTime, Math.floor);
        frameLoop(gen);
      });
    }
  }
  function onFrame(v, seconds, rounding) {
    if (!S.playing || v !== active() || v.seeking || (v.__tlPending !== undefined &&
      v.__tlPending !== null)) { return; }
    var span = S.spans[S.span];
    if (!span || v.__tlSource !== span.sourceId) { return; }
    var step = TL.playStep(S.doc, S.span, frameOf(span.sourceId, seconds, rounding),
      standbyState());
    S.span = step.span;
    S.playhead = step.edited;
    if (step.action === "stop") { stopPlayback(); S.playhead = S.total; paint(); return; }
    if (step.action === "jump") { jump(step); return; }
    if (step.prime) { prime(step.prime); }
    paintPlayhead();
  }
  // 컷: 대기 영상이 그 자리에 서 있으면 바꿔 틀고, 아니면 활성 영상을 옮긴다.
  function jump(step) {
    var target = step.target;
    S.span = target.span;
    S.playhead = S.spans[target.span].outStart;
    if (!mediaOf(target.sourceId)) {
      stopPlayback();
      say("「" + nameOf(target.sourceId) + "」의 미리보기가 아직 없어 멈췄습니다.", true);
      paint();
      return;
    }
    if (step.swap) { swapDecks(); paint(); return; }
    var v = active();
    place(v, target.sourceId, target.frame);
    var started = v.play();
    if (started && started.catch) { started.catch(function () { stopPlayback(); }); }
    paint();
  }
  function swapDecks() {
    var old = active();
    var next = standby();
    next.playbackRate = old.playbackRate;
    next.muted = old.muted;
    next.volume = old.volume;
    S.deck = 1 - S.deck;
    showDeck();
    S.loopGen += 1;
    var gen = S.loopGen;
    var started = next.play();
    old.pause();
    if (started && started.catch) {
      started.catch(function () {
        if (gen !== S.loopGen) { return; }
        stopPlayback();
        say("다음 클립을 틀지 못했습니다 — 다시 재생하세요.", true);
      });
    }
    frameLoop(gen);
  }
  function step(frames) {
    if (S.playing) { stopPlayback(); }
    seek(S.playhead + frames);
  }
  function cutPoints() { return S.spans.map(function (span) { return span.outStart; }); }
  function goCut(direction) {
    var points = cutPoints().concat([S.total]);
    var target = direction > 0
      ? points.filter(function (p) { return p > S.playhead; })[0]
      : points.filter(function (p) { return p < S.playhead; }).pop();
    if (target === undefined) { return; }
    var playing = S.playing;
    if (playing) { stopPlayback(); }
    seek(target);
    if (playing && target < S.total) { play(); }
  }
  function toggle() { if (S.playing) { stopPlayback(); } else { play(); } }

  // ─── 준비 · 다시 묻기 ────────────────────────────────────────────
  function prepare() {
    var payload = { signature: S.signature, token: S.token, timelineId: S.timelineId };
    say("미리보기 준비를 거는 중…", false);
    S.server.tl_prepare(payload).then(function (got) {
      if (!got || !got.ok) {
        say((got && got.error) || "미리보기 준비를 걸지 못했습니다.", true);
        return;
      }
      say(got.started.length ? "미리보기 " + got.started.length + "개를 준비합니다(무료 로컬)."
        : "준비할 미리보기가 없습니다.", false);
      poll(S.pollGen);
    });
  }
  function poll(gen) {
    W.setTimeout(function () {
      if (gen !== S.pollGen || !S.doc) { return; }
      var payload = { signature: S.signature, timelineId: S.timelineId };
      S.server.tl_media(payload).then(function (got) {
        if (gen !== S.pollGen || !got || !got.ok) { return; }
        var before = notReady().length;
        S.media = got.media;
        if (notReady().length !== before && !S.playing) { seek(S.playhead); } else { paint(); }
        if (anyPreparing()) { poll(gen); }
      });
    }, POLL_MS);
  }

  // ─── 그리기 ──────────────────────────────────────────────────────
  function say(text, isError) { S.status = text; S.statusError = !!isError; paintTransport(); }
  function timecode(frame) {
    var seq = S.doc.facts.sequence;
    var fps = Math.max(1, Math.round(seq.den / seq.num));
    var seconds = Math.floor(frame / fps);
    function two(value) { return (value < 10 ? "0" : "") + value; }
    return two(Math.floor(seconds / 3600)) + ":" + two(Math.floor(seconds / 60) % 60) + ":" +
      two(seconds % 60) + ":" + two(frame % fps);
  }
  function paint() { paintTransport(); paintMissing(); paintPlayhead(); }
  function relayout() {
    S.spans = TL.layout(S.doc);
    S.total = TL.editedFrames(S.doc);
    S.proj = TL.project(S.doc);
    S.playhead = clamp(S.playhead, 0, S.total);
    S.sel = clamp(S.sel, 0, Math.max(0, S.doc.clips.length - 1));
  }
  function paintTransport() {
    var button = $("tl-toggle");
    if (button) { button.textContent = S.playing ? "⏸" : "▶"; }
    var status = $("tl-status");
    if (status) {
      status.textContent = S.status;
      status.classList.toggle("is-error", S.statusError);
    }
    var undoButton = $("tl-undo");
    if (undoButton) { undoButton.disabled = !S.history.past.length; }
    var redoButton = $("tl-redo");
    if (redoButton) { redoButton.disabled = !S.history.future.length; }
    var force = $("tl-force");
    if (force) { force.hidden = !S.conflict; }
    var prep = $("tl-prepare");
    if (prep && S.doc) {
      var waiting = notReady();
      prep.hidden = !waiting.length;
      prep.textContent = "미리보기 준비(" + waiting.length + "개 · 무료)";
    }
  }
  function paintMissing() {
    var box = $("tl-missing");
    if (!box || !S.doc || !S.spans.length) { return; }
    var id = S.spans[S.span].sourceId;
    var row = S.media[id] || { state: "missing" };
    var ready = !!mediaOf(id);
    box.hidden = ready;
    if (!ready) {
      box.textContent = "「" + nameOf(id) + "」 미리보기가 " + (row.state === "preparing"
        ? "준비 중입니다." : "아직 없습니다 — 「미리보기 준비」를 누르세요.");
    }
  }
  function paintPlayhead() {
    if (!S.doc) { return; }
    var tc = $("tl-timecode");
    if (tc) { tc.textContent = timecode(S.playhead) + " / " + timecode(S.total); }
    var where = $("tl-source");
    if (where && S.spans.length) {
      where.textContent = "클립 " + (S.span + 1) + "/" + S.spans.length + " · " +
        nameOf(S.spans[S.span].sourceId);
    }
    var overlay = $("tl-overlay");
    if (overlay) { overlay.textContent = cueText(); }
    paintStrip();
  }
  function cueText() {
    var t = S.playhead * TL.sequenceFrameTicks(S.doc.facts);
    return S.proj.cues.filter(function (cue) {
      return cue[2] === cue[3] ? cue[2] === t : cue[2] <= t && t < cue[3];
    }).map(function (cue) { return cue[4]; }).join("\n");
  }
  function cssVar(name) { return getComputedStyle(S.rootEl).getPropertyValue(name).trim(); }
  function paintStrip() {
    var c = $("tl-canvas");
    if (!c || !S.rootEl) { return; }
    var w = c.clientWidth;
    var h = c.clientHeight;
    if (!w || !h) { return; }
    var ratio = W.devicePixelRatio || 1;
    if (c.width !== Math.round(w * ratio)) { c.width = Math.round(w * ratio); }
    if (c.height !== Math.round(h * ratio)) { c.height = Math.round(h * ratio); }
    var g = c.getContext("2d");
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, w, h);
    var total = Math.max(S.total, 1);
    var tones = [cssVar("--vl-accent-soft"), cssVar("--vl-surface-raised")];
    var tone = 0;
    g.font = "11px " + cssVar("--vl-font-sans");
    g.textBaseline = "middle";
    S.spans.forEach(function (span, i) {
      if (i > 0 && span.sourceId !== S.spans[i - 1].sourceId) { tone = 1 - tone; }
      var x = span.outStart / total * w;
      var width = Math.max(1, span.outFrames / total * w);
      g.fillStyle = mediaOf(span.sourceId) ? tones[tone] : cssVar("--vl-warn-soft");
      g.fillRect(x, 4, width - 1, h - 8);
      g.fillStyle = cssVar("--vl-line-strong");
      g.fillRect(x, 4, 1, h - 8);
      if (i === S.sel) {
        g.strokeStyle = cssVar("--vl-accent");
        g.lineWidth = 2;
        g.strokeRect(x + 1, 5, width - 3, h - 10);
      }
      if (width > 60) {
        g.fillStyle = cssVar("--vl-ink");
        g.save();
        g.beginPath();
        g.rect(x + 3, 4, width - 6, h - 8);
        g.clip();
        g.fillText(nameOf(span.sourceId), x + 4, h / 2);
        g.restore();
      }
    });
    var px = S.playhead / total * w;
    g.fillStyle = cssVar("--vl-danger");
    g.fillRect(Math.round(px) - 1, 0, 2, h);
    paintSource();
  }

  // ─── 범위 조정(W-71d): 고른 클립의 원본 전체 띠 ─────────────────
  // 원본 처음부터 끝까지를 한 줄에 — 등급(추천·보통·비추천)은 흐리게, 같은 원본의 다른 클립은
  // 회색, 고른 클립은 밝게 손잡이와 함께. 손잡이를 끌면 그 가장자리 프레임을 화면에 보이고, 놓으면
  // 클립 앞뒤가 원본 안에서 바뀐다(같은 원본의 앞뒤 클립·원본 처음·끝까지, `TL.clampEdge`).
  var TIER_FILL = { green: "--vl-tier-green", yellow: "--vl-tier-yellow", red: "--vl-tier-red" };
  function selClip() { return S.doc && S.doc.clips.length ? S.doc.clips[S.sel] : null; }
  function sourceFrameAtX(c, clientX) {
    var clip = selClip();
    var rect = c.getBoundingClientRect();
    var frames = sourceOf(clip[0]).frames;
    return clamp(Math.round((clientX - rect.left) / Math.max(rect.width, 1) * frames), 0, frames);
  }
  function edgeOf(side) {
    var clip = selClip();
    if (S.drag && S.drag.side === side) { return S.drag.frame; }
    return side === TL.SIDE_START ? clip[1] : clip[2];
  }
  function paintSource() {
    var c = $("tl-source-canvas");
    var head = $("tl-source-head");
    var clip = selClip();
    if (!c || !clip) { return; }
    var source = sourceOf(clip[0]);
    var seconds = source.num / source.den;
    var start = edgeOf(TL.SIDE_START);
    var end = edgeOf(TL.SIDE_END);
    if (head) {
      head.textContent = "클립 " + (S.sel + 1) + " · " + nameOf(clip[0]) + " · 원본 " +
        (start * seconds).toFixed(2) + "–" + (end * seconds).toFixed(2) + "초 (" +
        ((end - start) * seconds).toFixed(2) + "초) / 원본 " +
        (source.frames * seconds).toFixed(1) + "초";
    }
    var w = c.clientWidth;
    var h = c.clientHeight;
    if (!w || !h) { return; }
    var ratio = W.devicePixelRatio || 1;
    if (c.width !== Math.round(w * ratio)) { c.width = Math.round(w * ratio); }
    if (c.height !== Math.round(h * ratio)) { c.height = Math.round(h * ratio); }
    var g = c.getContext("2d");
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, w, h);
    function xOf(frame) { return frame / Math.max(source.frames, 1) * w; }
    if (S.tiers[clip[0]] === undefined) { askTiers(clip[0]); }
    g.globalAlpha = 0.35;
    (S.tiers[clip[0]] || []).forEach(function (row) {
      var token = TIER_FILL[row[2]];
      if (!token) { return; }
      g.fillStyle = cssVar(token);
      var x0 = xOf(rational(row[0]) / seconds);
      g.fillRect(x0, 0, Math.max(1, xOf(rational(row[1]) / seconds) - x0), h);
    });
    g.globalAlpha = 1;
    S.doc.clips.forEach(function (other, i) {
      if (other[0] !== clip[0] || i === S.sel) { return; }
      g.fillStyle = cssVar("--vl-line-strong");
      g.fillRect(xOf(other[1]), h / 3, Math.max(1, xOf(other[2]) - xOf(other[1])), h / 3);
    });
    g.fillStyle = cssVar("--vl-accent");
    g.fillRect(xOf(start), h / 4, Math.max(2, xOf(end) - xOf(start)), h / 2);
    g.fillStyle = cssVar("--vl-ink");
    [start, end].forEach(function (frame) { g.fillRect(xOf(frame) - 2, 0, 4, h); });
    var at = sourceCursor();
    if (at !== null) {
      g.fillStyle = cssVar("--vl-danger");
      g.fillRect(Math.round(xOf(at)) - 1, 0, 2, h);
    }
  }
  // 등급 띠는 그 원본을 처음 고를 때 한 번 묻는다(표시만 — 못 받으면 띠 없이 그린다).
  function askTiers(sourceId) {
    S.tiers[sourceId] = null;
    S.server.tl_tiers({ signature: S.signature, sourceId: sourceId }).then(function (got) {
      if (!got || !got.ok) { return; }
      S.tiers[got.sourceId] = got.tiers;
      paintSource();
    });
  }
  // 원본 띠의 커서 — 띠를 눌러 고른 원본 프레임, 없으면 플레이헤드가 그 원본에 있을 때의 프레임.
  function sourceCursor() {
    var clip = selClip();
    if (!clip) { return null; }
    if (S.cursor && S.cursor.sourceId === clip[0]) { return S.cursor.frame; }
    if (!S.spans.length) { return null; }
    var at = TL.sourcePoint(S.doc, Math.min(S.playhead, S.total - 1));
    return S.spans[at[0]].sourceId === clip[0] ? at[1] : null;
  }
  function showSourceFrame(sourceId, frame) {
    if (!mediaOf(sourceId)) { return; }
    if (S.playing) { stopPlayback(); }
    var source = sourceOf(sourceId);
    place(active(), sourceId, clamp(frame, 0, source.frames - 1));
  }
  function onSourceDown(ev) {
    var clip = selClip();
    if (!clip) { return; }
    var c = ev.currentTarget;
    var frame = sourceFrameAtX(c, ev.clientX);
    var rect = c.getBoundingClientRect();
    var px = function (f) { return f / Math.max(sourceOf(clip[0]).frames, 1) * rect.width; };
    var x = ev.clientX - rect.left;
    var side = null;
    if (Math.abs(x - px(clip[1])) <= S.settings.snapPx) { side = TL.SIDE_START; }
    else if (Math.abs(x - px(clip[2])) <= S.settings.snapPx) { side = TL.SIDE_END; }
    if (side) {
      S.drag = { side: side, frame: side === TL.SIDE_START ? clip[1] : clip[2] };
      c.setPointerCapture(ev.pointerId);
      dragTo(c, ev.clientX);
    } else {
      S.cursor = { sourceId: clip[0], frame: Math.min(frame, sourceOf(clip[0]).frames - 1) };
      showSourceFrame(clip[0], S.cursor.frame);
      paintSource();
    }
    if (S.rootEl) { S.rootEl.focus(); }
  }
  function dragTo(c, clientX) {
    var frame = TL.clampEdge(S.doc, S.sel, S.drag.side, sourceFrameAtX(c, clientX), S.settings);
    S.drag.frame = frame;
    showSourceFrame(selClip()[0], S.drag.side === TL.SIDE_START ? frame : frame - 1);
    paintSource();
  }
  function onSourceMove(ev) {
    if (S.drag) { dragTo(ev.currentTarget, ev.clientX); return; }
    var clip = selClip();
    if (!clip) { return; }
    var c = ev.currentTarget;
    var rect = c.getBoundingClientRect();
    var frames = Math.max(sourceOf(clip[0]).frames, 1);
    var x = ev.clientX - rect.left;
    var near = [clip[1], clip[2]].some(function (f) {
      return Math.abs(x - f / frames * rect.width) <= S.settings.snapPx;
    });
    c.style.cursor = near ? "ew-resize" : "pointer";
  }
  function onSourceUp() {
    if (!S.drag) { return; }
    var drag = S.drag;
    S.drag = null;
    setEdge(drag.side, drag.frame);
  }
  // 고른 클립의 한 가장자리를 원본 프레임 frame으로 — 바뀌면 기록하고 그 가장자리로 간다.
  function setEdge(side, frame) {
    var clip = selClip();
    if (!clip) { return; }
    var at = TL.clampEdge(S.doc, S.sel, side, frame, S.settings);
    if (at === (side === TL.SIDE_START ? clip[1] : clip[2])) { paint(); return; }
    var next = side === TL.SIDE_START ? TL.setClipStart(S.doc, S.sel, at, S.settings)
      : TL.setClipEnd(S.doc, S.sel, at, S.settings);
    var verb = side === TL.SIDE_START ? "앞" : "뒤";
    var moved = Math.abs(at - (side === TL.SIDE_START ? clip[1] : clip[2]));
    change(next, "클립 " + (S.sel + 1) + " " + verb + " " + moved + "프레임");
    var span = S.spans[S.sel];
    S.cursor = null;
    seek(side === TL.SIDE_START ? span.outStart : span.outStart + span.outFrames - 1);
  }
  function trimToCursor(side) {
    var at = sourceCursor();
    if (at === null) {
      say("원본 띠를 눌러 자리를 고르거나 플레이헤드를 그 클립에 두세요.", true);
      return;
    }
    setEdge(side, at);
  }

  // ─── 렌더(W-71e): 저장된 리비전 그대로 원본에서 한 번 ──────────
  // 저장 대기 편집을 먼저 보내고, 저장이 끝난 리비전을 이름으로 건다 — 서버는 다른 리비전이면
  // 렌더하지 않는다. 결과는 초안과 같은 폴더라 결과 카드·영어 변환·자막 입히기가 그대로 쓴다.
  function render() {
    if (S.conflict) {
      say("저장이 충돌한 채로는 렌더하지 않습니다 — 먼저 정리하세요.", true);
      return;
    }
    if (S.dirty || S.saving) {
      saveNow(false);
      say("편집을 저장한 뒤 렌더합니다…", false);
      var gen = S.pollGen;
      var wait = function () {
        if (gen !== S.pollGen) { return; }
        if (S.dirty || S.saving) { W.setTimeout(wait, S.settings.autosaveMs); return; }
        if (!S.conflict) { render(); }
      };
      W.setTimeout(wait, S.settings.autosaveMs);
      return;
    }
    if (S.playing) { stopPlayback(); }
    var payload = { signature: S.signature, token: S.token, timelineId: S.timelineId,
      revision: S.baseRevision };
    if (W.__vlServer) { W.__vlServer.wake(true); }
    S.server.tl_render(payload).then(function (got) {
      if (!got || !got.ok) { say((got && got.error) || "렌더를 걸지 못했습니다.", true); return; }
      say("리비전 " + payload.revision + "을 렌더합니다(무료 로컬) — 활동 띠에서 진행을 보고," +
        " 끝나면 아래 초안 편집과 「영상 정리」 결과 카드에 나옵니다.", false);
    });
  }

  // ─── 문서 바꾸기 · 되돌리기 · 저장 ──────────────────────────────
  function change(next, label) {
    if (S.playing) { stopPlayback(); }
    S.history.past.push(S.doc);
    if (S.history.past.length > S.settings.undoLimit) { S.history.past.shift(); }
    S.history.future = [];
    S.doc = next;
    relayout();
    S.dirty = true;
    say(label + " — 저장 대기", false);
    scheduleSave();
  }
  function undo(redo) {
    var from = redo ? S.history.future : S.history.past;
    var to = redo ? S.history.past : S.history.future;
    if (!from.length) { return; }
    if (S.playing) { stopPlayback(); }
    to.push(S.doc);
    S.doc = from.pop();
    relayout();
    S.dirty = true;
    say(redo ? "다시 했습니다 — 저장 대기" : "되돌렸습니다 — 저장 대기", false);
    seek(S.playhead);
    scheduleSave();
  }
  function scheduleSave() {
    if (S.saveTimer) { W.clearTimeout(S.saveTimer); }
    S.saveTimer = W.setTimeout(function () { saveNow(false); }, S.settings.autosaveMs);
  }
  function docPayload() {
    return { clips: S.doc.clips, cues: S.doc.cues, nextCue: S.doc.nextCue, seeded: S.doc.seeded };
  }
  function saveNow(force) {
    if (S.saveTimer) { W.clearTimeout(S.saveTimer); S.saveTimer = null; }
    if (!S.dirty || S.saving || !S.doc) { return; }
    S.saving = true;
    var sent = S.doc;
    var payload = { signature: S.signature, token: S.token, timelineId: S.timelineId,
      baseRevision: S.baseRevision, doc: docPayload(), force: !!force };
    S.server.tl_save(payload).then(function (got) {
      S.saving = false;
      if (!got || !got.ok) { say((got && got.error) || "저장하지 못했습니다.", true); return; }
      if (got.conflict) {
        S.conflict = true;
        say("다른 곳에서 이 타임라인이 리비전 " + got.revision + "으로 바뀌었습니다 — " +
          "새로고침하거나 「내 편집으로 덮기」를 누르세요.", true);
        return;
      }
      S.conflict = false;
      S.baseRevision = got.revision;
      if (S.doc === sent) { S.dirty = false; } else { scheduleSave(); }
      say("저장했습니다 · 리비전 " + got.revision, false);
    }, function () { S.saving = false; say("저장하지 못했습니다.", true); });
  }

  // ─── 사건 ────────────────────────────────────────────────────────
  function editable(target) {
    return !!target && (target.isContentEditable ||
      /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || ""));
  }
  function onKey(ev) {
    if (!S.doc || editable(ev.target)) { return; }
    var mod = ev.metaKey || ev.ctrlKey;
    var special = null;
    if (ev.altKey && !mod && ev.code === "BracketLeft") {
      special = function () { trimToCursor(TL.SIDE_START); };
    } else if (ev.altKey && !mod && ev.code === "BracketRight") {
      special = function () { trimToCursor(TL.SIDE_END); };
    } else if (mod && !ev.altKey && ev.code === "KeyZ") {
      special = function () { undo(ev.shiftKey); };
    } else if (mod && !ev.altKey && ev.code === "KeyS") {
      special = function () { saveNow(false); };
    }
    if (special) { ev.preventDefault(); ev.stopPropagation(); special(); return; }
    if (mod || ev.altKey) { return; }
    var big = ev.shiftKey ? S.settings.bigStepFrames : 1;
    var acts = {
      Space: toggle, KeyK: stopPlayback,
      ArrowLeft: function () { step(-big); }, ArrowRight: function () { step(big); },
      ArrowUp: function () { goCut(-1); }, ArrowDown: function () { goCut(1); },
      Home: function () { step(-S.total); }, End: function () { step(S.total); },
    };
    var act = acts[ev.code];
    if (!act) { return; }
    ev.preventDefault();
    ev.stopPropagation();
    act();
  }
  function onClick(ev) {
    var target = ev.target.closest ? ev.target.closest("[data-tl]") : null;
    if (!target) { return; }
    var acts = { toggle: toggle, "prev-cut": function () { goCut(-1); },
      "next-cut": function () { goCut(1); }, prepare: prepare,
      undo: function () { undo(false); }, redo: function () { undo(true); },
      "trim-start": function () { trimToCursor(TL.SIDE_START); },
      "trim-end": function () { trimToCursor(TL.SIDE_END); },
      "force-save": function () { saveNow(true); }, render: render };
    var act = acts[target.getAttribute("data-tl")];
    if (act) { act(); }
  }
  function onStripDown(ev) {
    var c = ev.currentTarget;
    var rect = c.getBoundingClientRect();
    var frame = Math.round((ev.clientX - rect.left) / Math.max(rect.width, 1) * S.total);
    var playing = S.playing;
    if (playing) { stopPlayback(); }
    if (S.spans.length) { S.sel = TL.sourcePoint(S.doc, Math.min(frame, S.total - 1))[0]; }
    S.cursor = null;
    seek(frame);
    if (playing) { play(); }
    if (S.rootEl) { S.rootEl.focus(); }
  }
  function onTimeUpdate(ev) {
    var v = ev.target;
    if (S.playing && v === active()) { onFrame(v, v.currentTime, Math.floor); }
  }
  function onVideoClick() { toggle(); }
  function bindDom() {
    var rootEl = S.rootEl;
    if (!rootEl.__tlBound) {
      rootEl.__tlBound = true;
      rootEl.addEventListener("keydown", onKey);
      rootEl.addEventListener("click", onClick);
    }
    decks().forEach(function (v) {
      if (v && !v.__tlBound) {
        v.__tlBound = true;
        v.addEventListener("timeupdate", onTimeUpdate);
        v.addEventListener("click", onVideoClick);
      }
    });
    var c = $("tl-canvas");
    if (c && !c.__tlBound) {
      c.__tlBound = true;
      c.addEventListener("pointerdown", onStripDown);
      if (W.ResizeObserver) { new W.ResizeObserver(paintStrip).observe(c); }
    }
    var bar = $("tl-source-canvas");
    if (bar && !bar.__tlBound) {
      bar.__tlBound = true;
      bar.addEventListener("pointerdown", onSourceDown);
      bar.addEventListener("pointermove", onSourceMove);
      bar.addEventListener("pointerup", onSourceUp);
      bar.addEventListener("pointercancel", onSourceUp);
      if (W.ResizeObserver) { new W.ResizeObserver(paintSource).observe(bar); }
    }
  }

  // 창을 닫거나 숨기기 전에 저장 대기 편집을 보낸다(자동 저장 간격을 기다리지 않는다).
  if (!W.__vlTimelinePlayerSaveBound) {
    W.__vlTimelinePlayerSaveBound = true;
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden" && S.dirty) { saveNow(false); }
    });
    W.addEventListener("beforeunload", function (ev) {
      if (S.dirty) { ev.preventDefault(); ev.returnValue = ""; }
    });
  }

  boot();
  if (!element.__vlTimelinePlayerObserver && W.MutationObserver) {
    element.__vlTimelinePlayerObserver = new W.MutationObserver(function () { boot(); });
    element.__vlTimelinePlayerObserver.observe(element, { childList: true, subtree: true,
      characterData: true });
  }
  if (typeof watch === "function") { watch("value", function () { boot(); }); }
})();
"""
