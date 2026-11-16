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
  var CG = W.__vlCaptionGrid;
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
    S.reloadId = m.reloadId || "";
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
      S.cue = null;
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
  function paint() { paintTransport(); paintMissing(); paintPlayhead(); paintCues(); }
  function relayout() {
    S.spans = TL.layout(S.doc);
    S.total = TL.editedFrames(S.doc);
    S.proj = TL.project(S.doc);
    S.playhead = clamp(S.playhead, 0, S.total);
    S.sel = clamp(S.sel, 0, Math.max(0, S.doc.clips.length - 1));
    S.warns = {};
    TL.warnings(S.doc, S.settings).forEach(function (row) {
      (S.warns[row[0]] = S.warns[row[0]] || []).push(row[1]);
    });
    if (S.cue && !S.proj.cues.some(function (cue) { return cue[0] === S.cue; })) { S.cue = null; }
  }
  function paintTransport() {
    var button = $("tl-toggle");
    if (button) { button.textContent = S.playing ? "⏸" : "▶"; }
    var status = $("tl-status");
    if (status) {
      // 상태 줄은 한 줄 고정이다 — 글 길이로 줄 수가 바뀌어 아래 단추가 움직이면 누름을 잃는다.
      status.textContent = S.status;
      status.title = S.status;
      status.classList.toggle("is-error", S.statusError);
    }
    var undoButton = $("tl-undo");
    if (undoButton) { undoButton.disabled = !S.history.past.length; }
    var redoButton = $("tl-redo");
    if (redoButton) { redoButton.disabled = !S.history.future.length; }
    var force = $("tl-force");
    if (force) { force.hidden = !S.conflict; }
    paintAddPicker();
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
    paintCueFollow();
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

  // ─── 클립 편집(W-71f): 플레이헤드에서 자르기 · 고른 클립 지우기(리플) · 다음 조각과 합치기 ─
  // 연산은 모델(`TL.clipEdit`)이 하고 거부도 모델의 글 그대로 보인다. 지운 클립의 원본 구간은
  // 문서의 뺀 구간으로 남는다(렌더 보고서 `timeline_cut`) — 되돌리기로 돌아온다.
  var CLIP_EDIT_LABELS = { blade: "자르기", "delete": "지우기", join: "합치기" };
  function clipOp(op) {
    if (!S.doc || !S.spans.length) { return; }
    var number = op === "blade" ? TL.sourcePoint(S.doc, Math.min(S.playhead, S.total - 1))[0]
      : S.sel;
    var out;
    try {
      out = TL.clipEdit(S.doc, op, S.sel, S.playhead);
    } catch (error) {
      if (!TL.isError(error)) { throw error; }
      say(CLIP_EDIT_LABELS[op] + "를 하지 못했습니다 — " + error.message, true);
      return;
    }
    S.sel = out.sel;
    S.cursor = null;
    change(out.doc, "클립 " + (number + 1) + " " + CLIP_EDIT_LABELS[op]);
    seek(out.playhead);
  }

  // ─── 원본 넣기(W-71g): 클립이 없는 원본을 원본 전체 클립으로 ─────
  // 처음 넣는 원본은 서버가 지금 원본 자막을 심어 새 리비전으로 쓴다(심은 리비전은 저장 밖이다)
  // — 그래서 저장 대기 편집을 먼저 보내고, 받은 문서로 바꾸며 되돌리기 기록은 여기서 새로 시작한다.
  // 빼는 길은 「클립 지우기」다(그 원본의 자막·심은 리비전은 문서에 남아 다시 넣어도 그대로다).
  function unclipped() {
    if (!S.doc) { return []; }
    var has = {};
    S.doc.clips.forEach(function (clip) { has[clip[0]] = true; });
    return S.doc.facts.sources.filter(function (s) { return !has[s.id]; })
      .map(function (s) { return s.id; });
  }
  function afterSaved(next) {
    var gen = S.pollGen;
    var wait = function () {
      if (gen !== S.pollGen) { return; }
      if (S.dirty || S.saving) { W.setTimeout(wait, S.settings.autosaveMs); return; }
      if (!S.conflict) { next(); }
    };
    saveNow(false);
    W.setTimeout(wait, S.settings.autosaveMs);
  }
  function addSource() {
    commitText();
    var pick = $("tl-add-pick");
    var id = pick ? pick.value : "";
    if (!S.doc || !id || S.adding) { return; }
    if (S.conflict) {
      say("저장이 충돌한 채로는 원본을 넣지 않습니다 — 먼저 정리하세요.", true);
      return;
    }
    if (S.dirty || S.saving) {
      say("편집을 저장한 뒤 넣습니다…", false);
      afterSaved(addSource);
      return;
    }
    if (S.playing) { stopPlayback(); }
    S.adding = true;
    var payload = { signature: S.signature, token: S.token, timelineId: S.timelineId,
      baseRevision: S.baseRevision, doc: docPayload(), sourceId: id };
    S.server.tl_add_source(payload).then(function (got) {
      S.adding = false;
      if (!got || !got.ok) { say((got && got.error) || "원본을 넣지 못했습니다.", true); return; }
      if (got.conflict) {
        S.conflict = true;
        say("다른 곳에서 이 타임라인이 리비전 " + got.revision + "으로 바뀌었습니다 — " +
          "새로고침한 뒤 다시 넣으세요.", true);
        return;
      }
      placeAdded(id, got);
    }, function () { S.adding = false; say("원본을 넣지 못했습니다.", true); });
  }
  function placeAdded(id, got) {
    S.doc = Object.assign({ facts: S.doc.facts }, got.doc);
    S.baseRevision = got.revision;
    S.dirty = false;
    S.history = { past: [], future: [] };
    relayout();
    S.sel = S.doc.clips.map(function (clip) { return clip[0]; }).indexOf(id);
    S.cursor = null;
    var notes = [];
    if (got.empty) { notes.push("빈 줄 " + got.empty + "개는 넣지 않음"); }
    if (got.clamped) { notes.push("원본 밖 시각 " + got.clamped + "줄은 원본 안으로"); }
    say(nameOf(id) + " 원본 전체를 넣었습니다 · 리비전 " + got.revision + " · 새 자막 " + got.cues +
      "줄" + (notes.length ? " (" + notes.join(" · ") + ")" : "") +
      " — 되돌리기는 여기서 새로 시작합니다(빼려면 「클립 지우기」).", false);
    seek(S.spans[S.sel].outStart);
    refreshMedia();
  }
  function refreshMedia() {
    var gen = S.pollGen;
    S.server.tl_media({ signature: S.signature, timelineId: S.timelineId }).then(function (got) {
      if (gen !== S.pollGen || !got || !got.ok) { return; }
      S.media = got.media;
      if (!S.playing) { seek(S.playhead); } else { paint(); }
      if (anyPreparing()) { poll(gen); }
    });
  }
  function paintAddPicker() {
    var pick = $("tl-add-pick");
    var button = $("tl-add");
    if (!pick || !button) { return; }
    var ids = unclipped();
    var key = ids.join("\n");
    if (pick.__tlKey !== key) {
      pick.__tlKey = key;
      pick.innerHTML = "";
      ids.forEach(function (id) {
        var option = document.createElement("option");
        option.value = id;
        option.textContent = nameOf(id);
        pick.appendChild(option);
      });
    }
    pick.hidden = button.hidden = !ids.length;
    button.disabled = !!S.adding;
  }


  // ─── 자막 편집(W-71i): 보이는 자막 목록 · 고른 자막의 편집 칸 ──────
  // 자막은 원본 밀리초에 산다 — 연산은 모델(`TL.cueEdit`)이 플레이헤드를 그 아래 원본의 밀리초로
  // 바꿔 한다. 고른 자막(`S.cue`)이 있으면 그것을, 없으면 플레이헤드 아래 자막을 고친다. ⌫는 고른
  // 자막이 있을 때만 자막을 지운다(클립 띠를 누르면 고름이 풀려 ⌫가 다시 클립을 지운다).
  // 컷에 가려진 자막은 문서에 남아 수로 보이고, 클립을 늘리면 돌아온다(규칙 9).
  var CUE_LABELS = { start: "자막 시작", end: "자막 끝", split: "자막 나누기", merge: "자막 합치기",
    "delete": "자막 지우기", insert: "새 자막", text: "자막 글" };
  var WARN_LABEL = { empty: "빈 글", zero: "길이 0", short: "짧음", long: "김", fast: "빠름",
    overlap: "겹침", gap: "좁은 틈" };
  function esc(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function seqTicks() { return TL.sequenceFrameTicks(S.doc.facts); }
  function cueUnderPlayhead() {
    var t = S.playhead * seqTicks();
    var hit = S.proj.cues.filter(function (cue) {
      return cue[2] === cue[3] ? cue[2] === t : cue[2] <= t && t < cue[3];
    })[0];
    return hit ? hit[0] : null;
  }
  function targetCue() { return S.cue || cueUnderPlayhead(); }
  function projectedCue(id) {
    return S.proj.cues.filter(function (cue) { return cue[0] === id; })[0] || null;
  }
  // `frame`를 주면 플레이헤드 대신 그 프레임(입력칸의 시각 · 다음 줄 시작)에서 고친다(W-87).
  function cueOp(op, id, text, frame) {
    if (!S.doc || !S.spans.length) { return; }
    var target = op === "insert" ? null : (id || targetCue());
    if (op !== "insert" && !target) {
      say("고칠 자막이 없습니다 — 목록에서 고르거나 플레이헤드를 자막 위에 두세요.", true);
      return;
    }
    var out;
    try {
      out = TL.cueEdit(S.doc, op, target, frame === undefined ? S.playhead : frame, S.settings,
        text);
    } catch (error) {
      if (!TL.isError(error)) { throw error; }
      say(CUE_LABELS[op] + " — 하지 못했습니다: " + error.message, true);
      return;
    }
    S.cue = out.cue;
    if (!out.doc) { paintCues(); return; }
    change(out.doc, CUE_LABELS[op] + (target ? " " + target : " " + out.cue));
    paint();
    if (op === "insert") { focusCueText(); }
  }
  function pickCue(id) {
    var cue = projectedCue(id);
    if (!cue) { return; }
    commitText();
    S.cue = id;
    var playing = S.playing;
    if (playing) { stopPlayback(); }
    seek(Math.floor(cue[2] / seqTicks()));
  }
  // ⌥↑·⌥↓ — 고른(또는 플레이헤드 아래) 자막의 앞·뒤 자막, 없으면 플레이헤드 앞·뒤의 가장 가까운 것.
  function stepCue(direction) {
    var ids = S.proj.cues.map(function (cue) { return cue[0]; });
    if (!ids.length) { return; }
    var at = ids.indexOf(targetCue());
    if (at >= 0) { pickCue(ids[clamp(at + direction, 0, ids.length - 1)]); return; }
    var starts = S.proj.cues.map(function (cue) { return Math.floor(cue[2] / seqTicks()); });
    var before = starts.filter(function (f) { return f < S.playhead; }).length;
    var pick = direction > 0 ? before : before - 1;
    if (pick < 0 || pick >= ids.length) { pick = direction > 0 ? ids.length - 1 : 0; }
    pickCue(ids[pick]);
  }
  function focusCueText() {
    var area = $("tl-cue-text");
    if (area) { area.focus(); area.setSelectionRange(area.value.length, area.value.length); }
  }
  // 글 칸의 고친 글을 문서로 — 칸을 떠날 때(change)·⌘Enter·Esc, 그리고 저장·렌더·넣기 앞에서.
  function commitText() {
    var area = $("tl-cue-text");
    if (!area || !S.doc) { return; }
    var id = area.getAttribute("data-cue");
    var cue = id ? projectedCue(id) : null;
    if (!cue) { return; }
    var value = area.value.replace(/\r/g, "").replace(/\n{2,}/g, "\n").replace(/^\n+|\n+$/g, "");
    if (value !== cue[4]) { cueOp("text", id, value); }
  }
  function leaveText() {
    commitText();
    if (S.rootEl) { S.rootEl.focus(); }
  }
  function cueTime(cue) { return timecode(Math.floor(cue[2] / seqTicks())); }
  function warnText(id) {
    return (S.warns[id] || []).map(function (k) { return WARN_LABEL[k] || k; }).join(" · ");
  }
  function paintCues() {
    if (!S.doc) { return; }
    paintCueList();
    paintCueEditor();
  }
  function paintCueList() {
    var list = $("tl-cue-list");
    var count = $("tl-cue-count");
    if (!list) { return; }
    if (count) {
      count.textContent = "보이는 " + S.proj.cues.length + "개" + (S.proj.hidden.length
        ? " · 컷에 가려진 " + S.proj.hidden.length + "개(클립을 늘리면 돌아온다)" : "");
    }
    if (list.__tlProj === S.proj && list.__tlCue === S.cue) { paintCueFollow(); return; }
    list.__tlProj = S.proj;
    list.__tlCue = S.cue;
    list.__tlCurrent = undefined;
    if (!S.proj.cues.length) {
      list.innerHTML = '<div class="tl-cue-empty">보이는 자막이 없습니다 — 「새 자막」(⌥C)으로' +
        " 넣으세요.</div>";
      return;
    }
    // Subtitle Edit식 표(W-87, D146) — # · 시작 · 끝 · 길이 · 텍스트 · 간격 · 문자/초.
    // 경고는 그 칸만 붉게(지우거나 숨기지 않는다), 까닭은 줄의 title에. F8·⇧F8로 오간다.
    list.innerHTML = '<div class="tl-cue-grid-head" aria-hidden="true"><span>#</span>' +
      "<span>시작</span><span>끝</span><span>길이</span><span>텍스트</span><span>간격</span>" +
      "<span>문자/초</span></div>" + captionRows().map(function (row) {
        var warn = warnText(row.id);
        return '<div class="tl-cue-row' + (row.id === S.cue ? " is-selected" : "") +
          '" role="option" data-cue="' + esc(row.id) + '"' +
          (warn ? ' title="' + esc(warn) + '"' : "") + ">" +
          '<span class="tl-cue-n">' + row.n + "</span>" +
          '<span class="tl-cue-time">' + timecode(row.startFrame) + "</span>" +
          '<span class="tl-cue-time">' + timecode(row.endFrame) + "</span>" +
          cell("tl-cue-num", row.bad.dur, row.seconds.toFixed(2)) +
          cell("tl-cue-words", row.bad.text, esc(CG.oneLine(row.text)) || "(빈 글)") +
          cell("tl-cue-num", row.bad.gap,
            row.gapSeconds === null ? "" : row.gapSeconds.toFixed(2)) +
          cell("tl-cue-num", row.bad.cps, row.cps === null ? "" : row.cps.toFixed(1)) + "</div>";
      }).join("");
    paintCueFollow();
  }
  function captionRows() {
    return CG.rows(S.proj.cues, S.warns, seqTicks(), TL.tickRate(S.doc.facts), TL.textChars);
  }
  function cell(cls, bad, html) {
    return '<span class="' + cls + (bad ? " is-bad" : "") + '">' + html + "</span>";
  }
  // F8·⇧F8 — 다음(앞) 경고 줄을 고르고 그 시작으로 간다.
  function stepError(direction) {
    var id = CG.nextError(captionRows(), targetCue(), direction);
    if (id === null) { say("더 갈 경고 줄이 없습니다."); return; }
    pickCue(id);
  }
  // ⌘⇧E — 고른 자막의 끝을 다음 보이는 자막의 시작까지(같은 원본일 때만 — 문서 연산이 가른다).
  function extendToNext() {
    var id = targetCue();
    var ids = S.proj.cues.map(function (cue) { return cue[0]; });
    var next = S.proj.cues[ids.indexOf(id) + 1];
    if (!id || !next) { say("늘릴 다음 자막이 없습니다.", true); return; }
    cueOp("end", id, undefined, Math.floor(next[2] / seqTicks()));
  }
  // 입력칸의 시작·끝 — 시간 코드(시:분:초:프레임)를 그 프레임으로 받아 문서 연산에 넘긴다.
  function parseTimecode(text) {
    var parts = String(text).trim().split(":").map(function (part) { return Number(part); });
    if (parts.length !== 4 || parts.some(function (n) { return !isFinite(n) || n < 0; })) {
      return null;
    }
    var seq = S.doc.facts.sequence;
    var fps = Math.max(1, Math.round(seq.den / seq.num));
    return ((parts[0] * 60 + parts[1]) * 60 + parts[2]) * fps + parts[3];
  }
  function commitTime(ev) {
    var input = ev.target;
    var id = input.getAttribute("data-cue");
    var frame = parseTimecode(input.value);
    if (frame === null) {
      say("시각은 시:분:초:프레임 꼴로 적어 주세요(예 00:00:12:05).", true);
      paintCueEditor(true);
      return;
    }
    cueOp(input.getAttribute("data-side"), id, undefined, frame);
  }
  // 재생 중에는 목록을 다시 짓지 않고 플레이헤드 아래 줄의 표시만 옮긴다.
  function paintCueFollow() {
    var list = $("tl-cue-list");
    if (!list || !S.doc) { return; }
    var current = cueUnderPlayhead();
    if (list.__tlCurrent !== current) {
      list.__tlCurrent = current;
      Array.prototype.forEach.call(list.querySelectorAll(".tl-cue-row"), function (row) {
        var on = row.getAttribute("data-cue") === current;
        row.classList.toggle("is-current", on);
        if (on && S.playing) { keepInList(list, row); }
      });
    }
    paintCueEditor();
  }
  // 목록 안에서만 그 줄이 보이게 — scrollIntoView는 페이지까지 움직여 재생 중 영상을 밀어낸다.
  function keepInList(list, row) {
    if (row.offsetTop < list.scrollTop) { list.scrollTop = row.offsetTop; return; }
    var bottom = row.offsetTop + row.offsetHeight;
    var view = list.scrollTop + list.clientHeight;
    if (bottom > view) { list.scrollTop = bottom - list.clientHeight; }
  }
  // 같은 자막을 보는 동안은 칸을 다시 짓지 않고 머리·글·정보만 바꾼다 — 글 칸을 떠나며 확정할
  // 때 누른 단추가 사라지거나 움직여 누름을 잃지 않게(누름과 뗌 사이에 단추가 바뀌면 click이 오지
  // 않는다). 고름(「고름」·「플레이헤드」)도 다시 짓는 열쇠가 아니다 — 확정이 고름을 바꾼다.
  // 경고 줄도 비어 있을 때 같은 높이를 둔다.
  function paintCueEditor(force) {
    var box = $("tl-cue-edit");
    if (!box || !S.doc) { return; }
    var area = $("tl-cue-text");
    var typing = !!area && document.activeElement === area;
    var id = targetCue();
    var cue = id ? projectedCue(id) : null;
    var same = box.__tlId === id && (!cue || !!area);
    if (!same && typing) { return; }
    if (!same) {
      box.__tlId = id;
      box.__tlProj = null;
      box.innerHTML = cue ? cueEditorHtml(id) : '<span class="tl-cue-muted">플레이헤드에 자막이' +
        " 없습니다 — 목록에서 고르거나 「새 자막」(⌥C)으로 넣으세요.</span>";
      var fresh = $("tl-cue-text");
      if (fresh) { fresh.addEventListener("change", commitText); }
      ["tl-cue-start", "tl-cue-end"].forEach(function (name) {
        var input = $(name);
        if (input) { input.addEventListener("change", commitTime); }
      });
    }
    var head = $("tl-cue-head");
    if (head) { head.textContent = "자막 " + id + (S.cue === id ? " · 고름" : " · 플레이헤드"); }
    if (!cue || (box.__tlProj === S.proj && !force)) { return; }
    box.__tlProj = S.proj;
    paintCueTimes(id, cue);
    var raw = S.doc.cues.filter(function (row) { return row[0] === id; })[0];
    var meta = $("tl-cue-meta");
    if (meta) {
      meta.textContent = nameOf(raw[1]) + " 원본 " + (raw[2] / 1000).toFixed(3) + "–" +
        (raw[3] / 1000).toFixed(3) + "초 (" + ((raw[3] - raw[2]) / 1000).toFixed(3) +
        "초) · 출력 " + cueTime(cue) + (cue[5] ? " · 컷에 일부 잘림" : "");
    }
    var warn = $("tl-cue-warn");
    if (warn) { warn.textContent = warnText(id); }
    var text = $("tl-cue-text");
    if (text && !typing && text.value !== cue[4]) { text.value = cue[4]; }
  }
  // 입력칸의 시각·길이·문자/초·줄 길이(Subtitle Edit 입력칸) — 고치는 중인 칸은 덮지 않는다.
  function paintCueTimes(id, cue) {
    var row = captionRows().filter(function (r) { return r.id === id; })[0];
    if (!row) { return; }
    [["tl-cue-start", row.startFrame], ["tl-cue-end", row.endFrame]].forEach(function (pair) {
      var input = $(pair[0]);
      if (input && document.activeElement !== input) { input.value = timecode(pair[1]); }
    });
    var dur = $("tl-cue-dur");
    if (dur) {
      dur.textContent = "길이 " + row.seconds.toFixed(2) + "초";
      dur.classList.toggle("is-bad", row.bad.dur);
    }
    var stats = $("tl-cue-stats");
    if (stats) {
      stats.textContent = "문자/초 " + (row.cps === null ? "—" : row.cps.toFixed(1)) +
        " · 줄 " + CG.lineLengths(cue[4], TL.textChars).join("/") + "자";
      stats.classList.toggle("is-bad", row.bad.cps);
    }
  }
  function cueEditorHtml(id) {
    return '<strong id="tl-cue-head"></strong>' +
      '<div class="tl-cue-times"><label>시작 <input id="tl-cue-start" class="tl-tc"' +
      ' data-side="start" data-cue="' + esc(id) + '" spellcheck="false"' +
      ' aria-label="자막 시작 — 시:분:초:프레임"></label>' +
      '<label>끝 <input id="tl-cue-end" class="tl-tc" data-side="end" data-cue="' + esc(id) +
      '" spellcheck="false" aria-label="자막 끝 — 시:분:초:프레임"></label>' +
      '<span id="tl-cue-dur" class="tl-cue-num"></span>' +
      '<span id="tl-cue-stats" class="tl-cue-num"></span></div>' +
      '<span id="tl-cue-meta" class="tl-cue-muted"></span>' +
      '<span id="tl-cue-warn" class="tl-cue-warn"></span>' +
      '<textarea id="tl-cue-text" data-cue="' + esc(id) + '" spellcheck="false"' +
      ' aria-label="자막 글 — ⌘Enter·Esc로 확정"></textarea>' +
      '<div class="tl-cue-buttons">' +
      cueButton("cue-start", "시작을 여기로 [", "자막 시작을 플레이헤드로") +
      cueButton("cue-end", "끝을 여기로 ]", "자막 끝을 플레이헤드로") +
      cueButton("cue-split", "나누기 ⇧B", "플레이헤드에서 자막 나누기") +
      cueButton("cue-merge", "다음과 합치기 ⇧M", "다음 보이는 자막과 합치기(같은 원본만)") +
      cueButton("cue-delete", "자막 지우기", "이 자막 지우기(고른 자막이면 ⌫)") + "</div>";
  }
  function cueButton(action, label, title) {
    return '<button type="button" data-tl="' + action + '" title="' + esc(title) + '">' +
      esc(label) + "</button>";
  }

  // ─── 렌더(W-71e): 저장된 리비전 그대로 원본에서 한 번 ──────────
  // 저장 대기 편집을 먼저 보내고, 저장이 끝난 리비전을 이름으로 건다 — 서버는 다른 리비전이면
  // 렌더하지 않는다. 결과는 초안과 같은 폴더라 결과 카드·영어 변환·자막 입히기가 그대로 쓴다.
  function render() {
    commitText();
    if (S.conflict) {
      say("저장이 충돌한 채로는 렌더하지 않습니다 — 먼저 정리하세요.", true);
      return;
    }
    if (S.dirty || S.saving) {
      say("편집을 저장한 뒤 렌더합니다…", false);
      afterSaved(render);
      return;
    }
    if (S.playing) { stopPlayback(); }
    var burnPick = $("tl-burn");
    var payload = { signature: S.signature, token: S.token, timelineId: S.timelineId,
      revision: S.baseRevision, burn: burnPick ? burnPick.value : "none" };
    if (W.__vlServer) { W.__vlServer.wake(true); }
    S.server.tl_render(payload).then(function (got) {
      if (!got || !got.ok) { say((got && got.error) || "공유를 걸지 못했습니다.", true); return; }
      say("리비전 " + payload.revision + "을 공유합니다(렌더" +
        (payload.burn === "none" ? "" : " + 자막 입히기") + ", 무료 로컬) — 활동 띠에서 진행을" +
        " 보고, 끝나면 결과 카드와 초안 폴더에 나옵니다.", false);
      watchRender(got.jobId);
    });
  }
  // 이 탭이 앞이면 화면 타이머가 쉰다(D134) — 초안 편집 칸의 틱이 새 초안을 못 보므로 재생기가 자기
  // 렌더를 물어 끝나면 그 칸의 숨은 「다시 열기」를 누른다(W-71h). 다른 화면의 렌더는 틱이 맡는다.
  function watchRender(jobId) {
    var gen = S.pollGen;
    S.renderJob = jobId;
    W.setTimeout(function () {
      if (gen !== S.pollGen || S.renderJob !== jobId) { return; }
      S.server.tl_job({ signature: S.signature, jobId: jobId }).then(function (got) {
        if (gen !== S.pollGen || S.renderJob !== jobId) { return; }
        if (!got || !got.ok) { S.renderJob = null; return; }
        if (!got.ended) { watchRender(jobId); return; }
        S.renderJob = null;
        if (got.status !== "succeeded") {
          say("렌더가 끝나지 못했습니다(" + got.status + ") — 「기록」에서 까닭을 보세요.", true);
          return;
        }
        say("공유를 마쳤습니다 — 결과 카드의 「초안 폴더 열기」에 영상이 있습니다(자막을" +
          " 입혔으면 <이름>.burned.mp4 · 영어 번역이 아직 없으면 한국어만). 「최종 편집」 탭도" +
          " 새 초안으로 열었습니다.", false);
        reopenDraft();
      });
    }, POLL_MS);
  }
  function reopenDraft() {
    var source = S.reloadId ? document.getElementById(S.reloadId) : null;
    if (source && source.tagName !== "BUTTON" && source.querySelector) {
      source = source.querySelector("button") || source;
    }
    if (source && typeof source.click === "function") { source.click(); }
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
    if (!S.doc) { return; }
    var mod = ev.metaKey || ev.ctrlKey;
    if (ev.target && ev.target.id === "tl-cue-text" &&
      (ev.code === "Escape" || (mod && (ev.code === "Enter" || ev.code === "KeyT")))) {
      ev.preventDefault();
      ev.stopPropagation();
      leaveText();
      return;
    }
    if (editable(ev.target)) { return; }
    var special = cueKey(ev, mod);
    if (special) {
      // 자막 키가 먼저다(아래 클립 키와 겹치는 것은 ⌫ 하나 — 고른 자막이 있을 때만 자막).
    } else if (ev.altKey && !mod && ev.code === "BracketLeft") {
      special = function () { trimToCursor(TL.SIDE_START); };
    } else if (ev.altKey && !mod && ev.code === "BracketRight") {
      special = function () { trimToCursor(TL.SIDE_END); };
    } else if (mod && !ev.altKey && ev.code === "KeyZ") {
      special = function () { undo(ev.shiftKey); };
    } else if (mod && !ev.altKey && ev.code === "KeyS") {
      special = function () { commitText(); saveNow(false); };
    } else if (mod && !ev.altKey && !ev.shiftKey && ev.code === "KeyB") {
      special = function () { clipOp("blade"); };
    } else if (!mod && !ev.altKey && !ev.shiftKey &&
      (ev.code === "Delete" || ev.code === "Backspace")) {
      special = function () { clipOp("delete"); };
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
  // 자막 키(D117과 같은 글자) — 고른 자막이 있으면 ⌫가 자막을 지운다.
  function cueKey(ev, mod) {
    var plain = !mod && !ev.altKey && !ev.shiftKey;
    var shiftOnly = !mod && !ev.altKey && ev.shiftKey;
    var altOnly = !mod && ev.altKey && !ev.shiftKey;
    if (plain && ev.code === "BracketLeft") { return function () { cueOp("start"); }; }
    if (plain && ev.code === "BracketRight") { return function () { cueOp("end"); }; }
    if (shiftOnly && ev.code === "KeyB") { return function () { cueOp("split"); }; }
    if (shiftOnly && ev.code === "KeyM") { return function () { cueOp("merge"); }; }
    if (altOnly && ev.code === "KeyC") { return function () { cueOp("insert"); }; }
    if (altOnly && ev.code === "ArrowUp") { return function () { stepCue(-1); }; }
    if (altOnly && ev.code === "ArrowDown") { return function () { stepCue(1); }; }
    // Subtitle Edit 키(W-87, D146) — 사용자가 Subtitle Edit에서 쓰던 A·D·S·T와 그 기본
    // F11·F12·F8, ⌘⇧E(다음 줄까지 늘리기)·⌘⇧M(합치기)·⌘T(입력칸). 재생기 안에서만 산다.
    var modShift = mod && !ev.altKey && ev.shiftKey;
    if (plain && (ev.code === "KeyA" || ev.code === "F11")) {
      return function () { cueOp("start"); };
    }
    if (plain && (ev.code === "KeyD" || ev.code === "F12")) {
      return function () { cueOp("end"); };
    }
    if (plain && ev.code === "KeyS") { return function () { cueOp("split"); }; }
    if (!mod && !ev.altKey && ev.code === "F8") {
      return function () { stepError(ev.shiftKey ? -1 : 1); };
    }
    if (modShift && ev.code === "KeyE") { return extendToNext; }
    if (modShift && ev.code === "KeyM") { return function () { cueOp("merge"); }; }
    var textKey = plain && ev.code === "KeyT";
    var modT = mod && !ev.altKey && !ev.shiftKey && ev.code === "KeyT";
    if ((plain && ev.code === "Enter" && ev.target.tagName !== "BUTTON") || textKey || modT) {
      return function () {
        var id = targetCue();
        if (!id) { say("고칠 자막이 없습니다 — 목록에서 고르세요.", true); return; }
        S.cue = id;
        paintCues();
        focusCueText();
      };
    }
    if (plain && ev.code === "Escape" && S.cue) {
      return function () { S.cue = null; paintCues(); };
    }
    if (plain && S.cue && (ev.code === "Delete" || ev.code === "Backspace")) {
      return function () { cueOp("delete", S.cue); };
    }
    return null;
  }
  function onClick(ev) {
    var target = ev.target.closest ? ev.target.closest("[data-tl]") : null;
    if (!target) { return; }
    var acts = { toggle: toggle, "prev-cut": function () { goCut(-1); },
      "next-cut": function () { goCut(1); }, prepare: prepare,
      undo: function () { undo(false); }, redo: function () { undo(true); },
      "trim-start": function () { trimToCursor(TL.SIDE_START); },
      "trim-end": function () { trimToCursor(TL.SIDE_END); },
      blade: function () { clipOp("blade"); }, "delete-clip": function () { clipOp("delete"); },
      join: function () { clipOp("join"); }, "add-source": addSource,
      "cue-start": function () { cueOp("start", cueTargetOf(target)); },
      "cue-end": function () { cueOp("end", cueTargetOf(target)); },
      "cue-split": function () { cueOp("split", cueTargetOf(target)); },
      "cue-merge": function () { cueOp("merge", cueTargetOf(target)); },
      "cue-delete": function () { cueOp("delete", cueTargetOf(target)); },
      "cue-insert": function () { commitText(); cueOp("insert"); },
      "force-save": function () { saveNow(true); }, render: render };
    var act = acts[target.getAttribute("data-tl")];
    if (act) { act(); }
  }
  // 편집 칸 단추는 그 칸이 보이는 자막을 고친다(글 칸의 고친 글을 먼저 확정한다).
  function cueTargetOf() {
    var area = $("tl-cue-text");
    var id = area ? area.getAttribute("data-cue") : null;
    commitText();
    return id;
  }
  function onStripDown(ev) {
    var c = ev.currentTarget;
    var rect = c.getBoundingClientRect();
    var frame = Math.round((ev.clientX - rect.left) / Math.max(rect.width, 1) * S.total);
    var playing = S.playing;
    if (playing) { stopPlayback(); }
    if (S.spans.length) { S.sel = TL.sourcePoint(S.doc, Math.min(frame, S.total - 1))[0]; }
    S.cursor = null;
    commitText();
    S.cue = null;
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
    var list = $("tl-cue-list");
    if (list && !list.__tlBound) {
      list.__tlBound = true;
      // 누르는 순간 고른다 — 글 칸이 먼저 확정되며 목록을 다시 지어도 누름을 잃지 않는다.
      list.addEventListener("pointerdown", function (ev) {
        var row = ev.target.closest ? ev.target.closest(".tl-cue-row") : null;
        if (!row) { return; }
        ev.preventDefault();
        pickCue(row.getAttribute("data-cue"));
        // 누름 기본 동작을 막았으니 키(화살표·Enter·[ ])가 재생기로 오게 초점을 직접 둔다.
        if (S.rootEl) { S.rootEl.focus({ preventScroll: true }); }
      });
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
      if (document.visibilityState !== "hidden") { return; }
      commitText();
      if (S.dirty) { saveNow(false); }
    });
    W.addEventListener("beforeunload", function (ev) {
      commitText();
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
