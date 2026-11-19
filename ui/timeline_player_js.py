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
  var LN = W.__vlCaptionLane;
  var KEYS = W.__vlKeys;
  var S = W.__vlTimelinePlayer;
  if (!S) {
    S = W.__vlTimelinePlayer = { epoch: null, doc: null, spans: [], total: 0, playhead: 0, span: 0,
      deck: 0, playing: false, loopGen: 0, media: {}, status: "", statusError: false,
      pollGen: 0, sel: 0, cursor: null, drag: null, dirty: false, saving: false,
      history: { past: [], future: [] } };
  }
  S.server = server;
  var POLL_MS = 2000;
  var TRANSLATION_KEY = "vlogsrt.translationMode";
  var MS_PER_SECOND = 1000;

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
    if (m.empty) {
      S.doc = null; S.timelineId = null;
      if (W.__vlWindow && W.__vlWindow.releaseProgram) { W.__vlWindow.releaseProgram(); }
      return;
    }
    S.signature = m.signature;
    S.token = m.token;
    S.timelineId = m.timelineId;
    S.names = m.names || {};
    S.media = m.media || {};
    S.tiers = m.tiers || {};
    S.settings = m.settings || {};
    S.burn = m.burn || null;
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
      S.cue = null; S.picked = []; S.anchor = null; S.english = null;
    }
    bindDom();
    restoreTranslation();
    // 자막 겹은 번역 모드와 상관없이 영어 줄을 보인다(W-98, D173) — 찾기는 무료 읽기다.
    if (!S.english) { askEnglish(); }
    // 뷰어 하나(W-86b) — 화면(.tl-stage)을 위 덱 뷰어 칸으로 옮긴다(노드째, 청취자 그대로).
    if (W.__vlWindow && W.__vlWindow.adoptProgram) { W.__vlWindow.adoptProgram(S.rootEl); }
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
    v.playbackRate = shuttleRate();
    var started = v.play();
    if (started && started.catch) {
      started.catch(function () { S.playing = false; paintTransport(); });
    }
    frameLoop(S.loopGen);
    paintTransport();
  }
  function stopPlayback() {
    stopReverse();
    S.shuttle = null;
    decks().forEach(function (v) {
      if (v && !v.paused) { v.pause(); }
      if (v) { v.playbackRate = 1; }
    });
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
    if (S.playing || reversing()) { stopPlayback(); }
    seek(S.playhead + frames);
  }
  // 자막 창의 ← · → (Subtitle Edit) — 밀리초 걸음을 시퀀스 프레임으로 옮겨 플레이헤드만 옮긴다.
  // 재생 중이면 그 자리에서 이어 튼다(Subtitle Edit처럼).
  function stepMs(ms) {
    if (!S.doc) { return; }
    var frames = Math.round(ms * TL.msTicks(S.doc.facts) / seqTicks());
    var playing = S.playing;
    if (playing || reversing()) { stopPlayback(); }
    seek(S.playhead + frames);
    if (playing && S.playhead < S.total) { play(); }
  }

  // ─── 셔틀(J · K · L — Final Cut) ───────────────────────────────
  // 앞은 영상의 재생 배속이고, 뒤는 영상이 거꾸로 돌지 못해 `reverseTickMs`마다 그 배속만큼 뒤로
  // 시크한다. 멈춤(K · Space · 프레임 걸음 · 컷 이동)은 배속을 1로 되돌린다.
  function reversing() { return !!(S.shuttle && S.shuttle.dir < 0); }
  function shuttleRate() { return S.shuttle && S.shuttle.dir > 0 ? S.shuttle.rate : 1; }
  function shuttle(dir) {
    if (!S.doc || !S.spans.length) { return; }
    var next = KEYS.shuttle(S.shuttle, dir, S.settings.shuttleRates);
    if (dir < 0 || reversing()) { stopPlayback(); }
    S.shuttle = next;
    if (dir < 0) {
      startReverse();
    } else {
      decks().forEach(function (v) { if (v) { v.playbackRate = next.rate; } });
      if (!S.playing) { play(); }
    }
    say((dir < 0 ? "◀ 거꾸로 " : "▶ ") + next.rate + "배 — K로 멈춥니다.", false);
    paintTransport();
  }
  function startReverse() {
    var gen = S.reverseGen;
    var perSecond = laneRate() / seqTicks();
    var tick = function () {
      S.reverseTimer = null;
      if (gen !== S.reverseGen || !reversing()) { return; }
      if (!S.rootEl || S.rootEl.offsetParent === null || S.playhead <= 0) {
        stopPlayback();
        return;
      }
      var frames = Math.max(1,
        Math.round(S.shuttle.rate * perSecond * S.settings.reverseTickMs / MS_PER_SECOND));
      seek(S.playhead - frames);
      S.reverseTimer = W.setTimeout(tick, S.settings.reverseTickMs);
    };
    tick();
  }
  function stopReverse() {
    S.reverseGen = (S.reverseGen || 0) + 1;
    if (S.reverseTimer) { W.clearTimeout(S.reverseTimer); S.reverseTimer = null; }
  }
  function cutPoints() { return S.spans.map(function (span) { return span.outStart; }); }
  function goCut(direction) {
    var points = cutPoints().concat([S.total]);
    var target = direction > 0
      ? points.filter(function (p) { return p > S.playhead; })[0]
      : points.filter(function (p) { return p < S.playhead; }).pop();
    if (target === undefined) { return; }
    var playing = S.playing;
    if (playing || reversing()) { stopPlayback(); }
    seek(target);
    if (playing && target < S.total) { play(); }
  }
  function toggle() { if (S.playing || reversing()) { stopPlayback(); } else { play(); } }

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
  function paint() {
    paintTransport(); paintMissing(); paintPlayhead(); paintCues(); requestLane(); paintIndex();
  }
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
    // 여러 줄 고름(W-87c) — 보이는 줄만 남긴다(컷에 가려지면 빠진다, 문서에서는 남는다).
    var visible = S.proj.cues.map(function (cue) { return cue[0]; });
    S.picked = (S.picked || []).filter(function (id) { return visible.indexOf(id) >= 0; });
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
    paintOverlay();
    paintStrip();
    paintCueFollow();
    requestLane();
  }
  // 자막 겹(W-98, D173) — 공유의 번인과 같은 모양을 근사한다: 자리(위·아래) · 여백 · 글자 크기 ·
  // 외곽선은 1080 기준 px × (화면 틀의 짧은 변 / 1080)(D171), 영어 줄은 한국어 아래 4/5 크기.
  // 정확한 모양은 「자막 입히기」의 미리보기 한 장(libass)이다 — 여기는 CSS다.
  function overlayCues() {
    var t = S.playhead * TL.sequenceFrameTicks(S.doc.facts);
    return S.proj.cues.filter(function (cue) {
      return cue[2] === cue[3] ? cue[2] === t : cue[2] <= t && t < cue[3];
    });
  }
  function overlayEnglish(cue) {
    var label = englishLabel(cue[0], cue[4]);
    return label.kind === "edited" || label.kind === "saved" ? label.text : "";
  }
  function overlayLine(className, text, size, outline, color) {
    var line = W.document.createElement("div");
    line.className = className;
    line.textContent = text;
    line.style.fontSize = size + "px";
    line.style.color = color;
    line.style.webkitTextStroke = outline > 0 ? (2 * outline) + "px " + S.burn.outlineColor : "";
    return line;
  }
  function paintOverlay() {
    var overlay = $("tl-overlay");
    if (!overlay) { return; }
    var cues = overlayCues();
    if (!S.burn) { overlay.textContent = cueText(); return; }
    var frame = overlay.parentNode.getBoundingClientRect();
    var unit = Math.min(frame.width, frame.height) / S.burn.referenceHeight;
    var style = S.burn.style;
    var side = frame.width * S.burn.sideMargin;
    overlay.className = "tl-overlay is-styled is-" + style.position;
    overlay.style.left = side + "px";
    overlay.style.right = side + "px";
    overlay.style.top = style.position === "top" ? style.margin_px * unit + "px" : "";
    overlay.style.bottom = style.position === "top" ? "" : style.margin_px * unit + "px";
    overlay.textContent = "";
    cues.forEach(function (cue) {
      var english = overlayEnglish(cue);
      if (cue[4]) {
        overlay.appendChild(overlayLine("tl-ov-ko", cue[4], style.font_px * unit,
          style.outline_px * unit, style.text_color));
      }
      if (english) {
        var scale = S.burn.englishScale;
        overlay.appendChild(overlayLine("tl-ov-en", english, style.font_px * scale * unit,
          style.outline_px * scale * unit, style.text_color));
      }
    });
  }
  // 손잡이를 바꾸면(공유 탭 「자막 모양」) 저장과 함께 여기로도 온다 — 다시 그리기만 한다.
  S.setBurnStyle = function (style) {
    if (!S.burn || !S.doc) { return; }
    S.burn.style = style;
    paintOverlay();
  };
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
    var marked = rangeOf();
    if (marked) {
      g.globalAlpha = 0.3;
      g.fillStyle = cssVar("--vl-accent");
      g.fillRect(marked[0] / total * w, 0, (marked[1] - marked[0]) / total * w, h);
      g.globalAlpha = 1;
      g.fillStyle = cssVar("--vl-accent");
      g.fillRect(marked[0] / total * w, 0, 2, h);
      g.fillRect(marked[1] / total * w - 2, 0, 2, h);
    }
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
  var CLIP_EDIT_LABELS = { blade: "블레이드", "delete": "삭제", join: "합치기" };
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

  // ─── 범위(I · O · ⌥X — Final Cut): 타임라인 프레임 [시작, 끝) ──────
  // 화면 상태일 뿐 문서·저장에 없다. 한쪽만 찍으면 다른 쪽은 타임라인 처음 · 끝이다. ⌫는 범위가
  // 있으면 그 범위를 모델의 리플 삭제(`TL.rippleDelete`)로 지운다 — 뒤가 당겨 오고, 지운 원본
  // 구간은 클립 지우기와 같이 문서의 뺀 구간으로 남아 되돌리기로 돌아온다.
  function rangeOf() {
    if (!S.range || !S.total) { return null; }
    var lo = clamp(S.range.start === null ? 0 : S.range.start, 0, S.total);
    var hi = clamp(S.range.end === null ? S.total : S.range.end, 0, S.total);
    return lo < hi ? [lo, hi] : null;
  }
  function markRange(side) {
    if (!S.doc || !S.total) { return; }
    var range = S.range || { start: null, end: null };
    if (side === "in") {
      range.start = S.playhead;
      if (range.end !== null && range.end <= range.start) { range.end = null; }
    } else {
      range.end = Math.min(S.total, S.playhead + 1);
      if (range.start !== null && range.start >= range.end) { range.start = null; }
    }
    S.range = range;
    var got = rangeOf();
    say(got ? "범위 " + timecode(got[0]) + " – " + timecode(got[1]) + " · " + (got[1] - got[0]) +
      "프레임 — ⌫로 지우고 ⌥X로 풉니다." : "범위가 비었습니다.", !got);
    paint();
  }
  function clearRange() {
    if (!S.range) { return false; }
    S.range = null;
    say("범위를 풀었습니다.", false);
    paint();
    return true;
  }
  function deleteRange() {
    var got = rangeOf();
    if (!got) { say("범위가 비었습니다 — I · O로 다시 찍으세요.", true); return; }
    if (got[0] === 0 && got[1] === S.total) {
      say("범위가 타임라인 전체라 지우지 않습니다 — 마지막 클립은 남깁니다.", true);
      return;
    }
    var next;
    try {
      next = TL.rippleDelete(S.doc, got[0], got[1]);
    } catch (error) {
      if (!TL.isError(error)) { throw error; }
      say("범위를 지우지 못했습니다 — " + error.message, true);
      return;
    }
    S.range = null;
    S.cursor = null;
    var left = TL.editedFrames(next);
    S.sel = TL.sourcePoint(next, Math.min(got[0], left - 1))[0];
    change(next, "범위 " + (got[1] - got[0]) + "프레임 삭제");
    seek(Math.min(got[0], left));
  }

  // ─── 보기(⌘= · ⌘− · ⇧Z · N — Final Cut): 파형 자막 레인의 줌 · 맞춤 · 스내핑 ──
  // 클립 띠는 언제나 타임라인 전체라 줌은 레인의 보기다. 플레이헤드가 보이면 그 자리를 두고 넓힌다.
  function zoomLane(factor) {
    var c = laneCanvas();
    if (!c || !S.doc) { return false; }
    var view = laneView();
    var rate = laneRate();
    var width = c.clientWidth || c.width;
    var x = LN.x(view, S.playhead * seqTicks());
    S.lane = LN.zoom(view, factor, x >= 0 && x <= width ? x : width / 2,
      rate / S.settings.zoomMaxPxPerS, rate / S.settings.zoomMinPxPerS);
    requestLane();
    return true;
  }
  function fitLane() {
    var c = laneCanvas();
    if (!c || !S.doc) { return false; }
    var rate = laneRate();
    S.lane = LN.fit(laneTotal(), c.clientWidth || c.width, rate / S.settings.zoomMaxPxPerS,
      rate / S.settings.zoomMinPxPerS);
    requestLane();
    return true;
  }
  function toggleSnap() {
    S.snapOff = !S.snapOff;
    if (S.rootEl) { S.rootEl.setAttribute("data-tl-snap", S.snapOff ? "off" : "on"); }
    say(S.snapOff
      ? "스내핑을 껐습니다 — 자막 가장자리가 컷 경계에 붙지 않습니다(N으로 다시 켭니다)."
      : "스내핑을 켰습니다 — 자막 가장자리가 컷 경계에 붙습니다(⇧를 누르면 그때만 끕니다).", false);
    return true;
  }
  // ⌘E(Final Cut 「공유」) — 공유 칸(번인 캡션)으로 갈 뿐 렌더를 걸지 않는다. 거는 것은 단추다.
  function focusShare() {
    var pick = $("tl-burn");
    if (!pick) { return false; }
    pick.focus();
    say("공유 — 번인 캡션을 고른 뒤 「공유」를 누르세요.", false);
    return true;
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
    commitEnglish();
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
    var pickedKey = S.picked.join("\n") + (S.translation ? "\u0001" + englishKey() : "");
    if (list.__tlProj === S.proj && list.__tlCue === S.cue && list.__tlPicked === pickedKey) {
      paintCueFollow();
      return;
    }
    list.__tlProj = S.proj;
    list.__tlCue = S.cue;
    list.__tlPicked = pickedKey;
    list.__tlCurrent = undefined;
    if (!S.proj.cues.length) {
      list.innerHTML = '<div class="tl-cue-empty">보이는 자막이 없습니다 — 「새 자막」(⌥C)으로' +
        " 넣으세요.</div>";
      return;
    }
    // Subtitle Edit식 표(W-87, D146) — # · 시작 · 끝 · 길이 · 텍스트 · 간격 · 문자/초.
    // 경고는 그 칸만 붉게(지우거나 숨기지 않는다), 까닭은 줄의 title에. F8·⇧F8로 오간다.
    list.classList.toggle("is-translation", !!S.translation);
    list.innerHTML = '<div class="tl-cue-grid-head" aria-hidden="true"><span>#</span>' +
      "<span>시작</span><span>끝</span><span>길이</span><span>텍스트</span>" +
      (S.translation ? "<span>영어</span>" : "") + "<span>간격</span>" +
      "<span>문자/초</span></div>" + captionRows().map(function (row) {
        var warn = warnText(row.id);
        return '<div class="tl-cue-row' + (isPicked(row.id) ? " is-selected" : "") +
          '" role="option" data-cue="' + esc(row.id) + '"' +
          (warn ? ' title="' + esc(warn) + '"' : "") + ">" +
          '<span class="tl-cue-n">' + row.n + "</span>" +
          '<span class="tl-cue-time">' + timecode(row.startFrame) + "</span>" +
          '<span class="tl-cue-time">' + timecode(row.endFrame) + "</span>" +
          cell("tl-cue-num", row.bad.dur, row.seconds.toFixed(2)) +
          cell("tl-cue-words", row.bad.text, esc(CG.oneLine(row.text)) || "(빈 글)") +
          (S.translation ? englishCell(row.id, row.text) : "") +
          cell("tl-cue-num", row.bad.gap,
            row.gapSeconds === null ? "" : row.gapSeconds.toFixed(2)) +
          cell("tl-cue-num", row.bad.cps, row.cps === null ? "" : row.cps.toFixed(1)) + "</div>";
      }).join("");
    paintCueFollow();
  }
  // 고른 줄 — 여러 줄 고름이 있으면 그것, 없으면 고른 자막 하나.
  function clearPicked() { S.cue = null; S.picked = []; S.anchor = null; }
  function isPicked(id) { return id === S.cue || S.picked.indexOf(id) >= 0; }
  function pickedIds() {
    if (S.picked.length > 1) { return S.picked.slice(); }
    return S.cue ? [S.cue] : [];
  }
  function visibleIds() { return S.proj.cues.map(function (cue) { return cue[0]; }); }
  // 목록 누름 — 그냥은 그 줄로 가고, Mod는 넣고 빼기 · ⇧는 기준부터 범위(가지 않는다, SE와 같다).
  function pickFromList(id, ev) {
    var how = (ev.metaKey || ev.ctrlKey) ? "toggle" : (ev.shiftKey ? "range" : "one");
    var base = S.picked.length ? S.picked : (S.cue ? [S.cue] : []);
    var out = LN.pick(visibleIds(), base, S.anchor === null ? S.cue : S.anchor, id, how);
    S.picked = out.picked;
    S.anchor = out.anchor;
    if (how === "one") { pickCue(id); return; }
    commitText();
    S.cue = out.picked.indexOf(id) >= 0 ? id : (out.picked[out.picked.length - 1] || null);
    paintCues();
    requestLane();
  }
  function captionRows() {
    return CG.rows(S.proj.cues, S.warns, seqTicks(), TL.tickRate(S.doc.facts), TL.textChars);
  }
  // ─── 번역 모드(W-87d, D153) — 렌더한 초안의 저장된 영어를 한국어 글로 찾아 보인다(읽기만) ───
  function englishKey() {
    if (!S.english) { return "none"; }
    return S.english === "pending" || S.english === "error" ? S.english : "ready";
  }
  // 사람이 고친 영어(D166)가 먼저다 — 낡지 않은 것, 저장된 번역, 낡은 고친 것(「낡음」) 차례.
  // `kind`: edited · saved · stale · none · pending · error. `value`는 입력칸에 채울 영어다.
  function englishLabel(id, text) {
    var edit = TL.editedEnglish(S.doc, [id, null, 0, 0, text]);
    if (edit && !edit.stale) { return { text: edit.text, value: edit.text, kind: "edited" }; }
    var saved = S.english && S.english.pairs ? CG.englishOf(text, S.english.pairs) : null;
    if (saved !== null && saved !== "") { return { text: saved, value: saved, kind: "saved" }; }
    if (edit) { return { text: "낡음 · " + edit.text, value: edit.text, kind: "stale" }; }
    if (S.english === "pending") { return { text: "찾는 중…", value: "", kind: "pending" }; }
    if (!S.english || S.english === "error") {
      return { text: "번역을 읽지 못함", value: "", kind: "error" };
    }
    return { text: saved === "" ? "" : "번역 없음", value: "", kind: "none" };
  }
  var ENGLISH_NOTE = {
    edited: "고친 영어 — 공유가 저장된 번역보다 먼저 씁니다",
    saved: "저장된 번역 — 고치면 이 줄만 고친 영어로 씁니다",
    stale: "낡음 — 한국어를 고친 뒤의 옛 영어입니다. 고치거나 비우세요",
    none: "번역 없음 — 적으면 이 줄의 영어로 씁니다",
    pending: "번역을 찾는 중…",
    error: "저장된 번역을 읽지 못했습니다 — 적으면 이 줄의 영어로 씁니다" };
  function englishCell(id, text) {
    var label = englishLabel(id, text);
    var muted = label.kind !== "edited" && label.kind !== "saved";
    return '<span class="tl-cue-en' + (muted ? " is-missing" : "") +
      (label.kind === "edited" ? " is-edited" : "") + '" title="' +
      esc(ENGLISH_NOTE[label.kind]) + '">' + esc(label.text) + "</span>";
  }
  // 영어 입력칸 확정 — 보인 값과 다를 때만 문서 연산 하나(빈 값은 고친 영어를 뺀다).
  function commitEnglish() {
    var area = $("tl-cue-en-text");
    if (!area || !S.doc || !S.translation) { return; }
    var id = area.getAttribute("data-cue");
    if (!id || !projectedCue(id)) { return; }
    var value = area.value.replace(/\r/g, "").replace(/\n{2,}/g, "\n").replace(/^\n+|\n+$/g, "");
    if (value === area.getAttribute("data-shown")) { return; }
    if (!value.trim() && !(S.doc.english || {})[id]) { return; }
    var next;
    try {
      next = TL.cueSetEnglish(S.doc, id, value, S.settings);
    } catch (error) {
      if (!TL.isError(error)) { throw error; }
      say("영어 고치기 — 하지 못했습니다: " + error.message, true);
      return;
    }
    area.setAttribute("data-shown", value);
    change(next, (value.trim() ? "영어 고치기 " : "고친 영어 지우기 ") + id);
    paint();
  }
  function onEnglishKey(ev) {
    if (ev.code === "Escape" || (ev.code === "Enter" && (ev.metaKey || ev.ctrlKey))) {
      ev.preventDefault();
      commitEnglish();
      if (S.rootEl) { S.rootEl.focus({ preventScroll: true }); }
    }
  }
  function askEnglish() {
    S.english = "pending";
    var asked = S.timelineId;
    S.server.tl_english({ signature: S.signature, timelineId: asked }).then(function (got) {
      if (asked !== S.timelineId) { return; }
      S.english = got && got.ok ? { pairs: got.pairs || {} } : "error";
      paintCues();
      paintCueEditor(true);
      paintOverlay();
    });
    paintCues();
  }
  // 번역 모드는 이 브라우저에만 기억한다 — 켜져 있으면 열 때 찾는다.
  function restoreTranslation() {
    try { S.translation = W.localStorage.getItem(TRANSLATION_KEY) === "on"; }
    catch (error) { S.translation = false; }
    var button = $("tl-translation");
    if (button) { button.setAttribute("aria-pressed", S.translation ? "true" : "false"); }
    if (S.translation && !S.english) { askEnglish(); }
  }
  function toggleTranslation() {
    S.translation = !S.translation;
    try { W.localStorage.setItem(TRANSLATION_KEY, S.translation ? "on" : ""); }
    catch (error) { /* 기억은 편의일 뿐 */ }
    var button = $("tl-translation");
    if (button) { button.setAttribute("aria-pressed", S.translation ? "true" : "false"); }
    if (S.translation && !S.english) { askEnglish(); return; }
    paintCues();
    paintCueEditor(true);
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
    var typing = !!area && (document.activeElement === area ||
      document.activeElement === $("tl-cue-en-text"));
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
      var en = $("tl-cue-en-text");
      if (en) {
        en.addEventListener("change", commitEnglish);
        en.addEventListener("keydown", onEnglishKey);
      }
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
    paintEnglishEditor(id, cue);
  }
  function paintEnglishEditor(id, cue) {
    var wrap = $("tl-cue-en-edit");
    var area = $("tl-cue-en-text");
    var note = $("tl-cue-en");
    if (!wrap || !area || !note) { return; }
    wrap.hidden = !S.translation;
    var label = englishLabel(id, cue[4]);
    if (document.activeElement !== area) {
      area.value = label.value;
      area.setAttribute("data-shown", label.value);
    }
    note.textContent = ENGLISH_NOTE[label.kind];
    note.classList.toggle("is-missing", label.kind !== "edited" && label.kind !== "saved");
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
      '<div id="tl-cue-en-edit" class="tl-cue-en-edit" hidden>' +
      '<textarea id="tl-cue-en-text" data-cue="' + esc(id) + '" spellcheck="true" lang="en"' +
      ' aria-label="영어 — 고치면 이 줄의 영어로 씁니다. ⌘Enter·Esc로 확정"' +
      ' placeholder="영어"></textarea>' +
      '<span id="tl-cue-en" class="tl-cue-en-note"></span></div>' +
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



  // ─── 타임라인 인덱스(W-86c, D150) ──────────────────────────────────
  // Final Cut 「인덱스」 — 클립 목록 · 자막 목록(「오류 보기」면 경고 줄만). 누르면 그 자리로 간다.
  function indexState() {
    if (!S.index) { S.index = { open: false, tab: "clips", errors: false }; }
    return S.index;
  }
  function toggleIndex() { indexState().open = !indexState().open; paintIndex(); }
  function indexTab(tab) { indexState().tab = tab; paintIndex(); }
  function paintIndex() {
    var box = $("tl-index");
    var list = $("tl-index-list");
    var state = indexState();
    var toggle = $("tl-index-toggle");
    if (toggle) { toggle.setAttribute("aria-pressed", state.open ? "true" : "false"); }
    if (!box || !list || !S.doc) { return; }
    box.hidden = !state.open;
    if (!state.open) { return; }
    Array.prototype.forEach.call(box.querySelectorAll("[data-tl^='index-']"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-tl") === "index-" + state.tab ?
        "true" : "false");
    });
    var errors = $("tl-index-errors");
    if (errors) { errors.disabled = state.tab !== "cues"; errors.checked = state.errors; }
    list.innerHTML = state.tab === "clips" ? indexClipRows() : indexCueRows(state.errors);
  }
  function indexClipRows() {
    return S.spans.map(function (span, i) {
      var seconds = frameSeconds(span.sourceId);
      return '<div class="tl-index-row' + (i === S.sel ? " is-selected" : "") +
        '" role="option" data-index-clip="' + i + '"><span class="tl-cue-n">' + (i + 1) +
        '</span><span class="tl-cue-time">' + timecode(span.outStart) + "</span><span>" +
        esc(nameOf(span.sourceId)) + '</span><span class="tl-cue-num">' +
        (span.startFrame * seconds).toFixed(2) + "–" + (span.endFrame * seconds).toFixed(2) +
        "초</span></div>";
    }).join("") || '<div class="tl-cue-empty">클립이 없습니다.</div>';
  }
  function indexCueRows(errorsOnly) {
    var rows = captionRows().filter(function (row) { return !errorsOnly || row.warned; });
    return rows.map(function (row) {
      var warn = warnText(row.id);
      return '<div class="tl-index-row' + (row.id === S.cue ? " is-selected" : "") +
        '" role="option" data-index-cue="' + esc(row.id) + '"><span class="tl-cue-n">' + row.n +
        '</span><span class="tl-cue-time">' + timecode(row.startFrame) + "</span><span>" +
        esc(CG.oneLine(row.text) || "(빈 글)") + '</span><span class="tl-cue-warn">' +
        esc(warn) + "</span></div>";
    }).join("") || '<div class="tl-cue-empty">' +
      (errorsOnly ? "경고가 있는 자막이 없습니다." : "보이는 자막이 없습니다.") + "</div>";
  }
  function onIndexDown(ev) {
    var row = ev.target.closest ? ev.target.closest(".tl-index-row") : null;
    if (!row) { return; }
    ev.preventDefault();
    var clip = row.getAttribute("data-index-clip");
    if (clip !== null) {
      commitText();
      S.sel = Number(clip);
      S.cursor = null;
      if (S.playing) { stopPlayback(); }
      seek(S.spans[S.sel].outStart);
    } else {
      pickCue(row.getAttribute("data-index-cue"));
    }
    if (S.rootEl) { S.rootEl.focus({ preventScroll: true }); }
    paint();
  }

  // ─── 파형 자막 레인(W-87b, D149) ───────────────────────────────────
  // Subtitle Edit 파형 조작: 누름 = 그 자리로 · 블록 고름 / 가장자리 끌기 = 시작·끝 /
  // 몸통 끌기 = 옮김 / 빈 곳 끌기 = 새 선택(Enter 넣기 · Esc 취소) / ⇧누름 = 시작 ·
  // ⌘(Ctrl)누름 = 끝 · ⌥누름 = 길이째 옮김 / 휠 = 앞뒤로 · ⌥(⌘)휠 = 확대.
  // 틱 고르기는 순수 `LN`, 문서 고침은 `TL` 연산 하나씩(규칙 3).
  function laneCanvas() { return $("tl-lane"); }
  function laneRate() { return TL.tickRate(S.doc.facts); }
  function laneTotal() { return S.total * seqTicks(); }
  function laneView() {
    if (!S.lane) { S.lane = { start: 0, tpp: laneRate() / S.settings.lanePxPerS }; }
    return S.lane;
  }
  function laneMinGap() { return S.settings.minGapMs * TL.msTicks(S.doc.facts); }
  // 틱 → 원본 밀리초 반올림(±½ ms)이 간격을 최소 아래로 내리지 않게 1 ms를 더 둔다(W-101, D170).
  function laneSafeGap() { return laneMinGap() + TL.msTicks(S.doc.facts); }
  // 컷 경계 · 타임라인 끝, 그리고 끄는 가장자리면 이웃 자막에서 최소 간격을 둔 자리(W-101, D170).
  function laneSnaps() {
    var cuts = S.spans.map(function (span) { return span.outStart * seqTicks(); });
    var drag = S.laneDrag;
    var gaps = drag && (drag.part === "start" || drag.part === "end")
      ? LN.gapSnaps(S.proj.cues, drag.index, drag.part, laneSafeGap()) : [];
    return cuts.concat([laneTotal()], gaps);
  }
  function rawCue(id) { return S.doc.cues.filter(function (row) { return row[0] === id; })[0]; }
  function requestLane() {
    if (S.lanePending) { return; }
    S.lanePending = true;
    W.requestAnimationFrame(function () { S.lanePending = false; paintLane(); });
  }
  function askWave(sourceId) {
    S.waves = S.waves || {};
    if (S.waves[sourceId]) { return; }
    S.waves[sourceId] = "pending";
    S.server.tl_wave({ signature: S.signature, sourceId: sourceId }).then(function (got) {
      if (!got || !got.ok) { S.waves[sourceId] = "error"; return; }
      var raw = W.atob(got.wave.peaks_b64);
      var peaks = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i += 1) { peaks[i] = raw.charCodeAt(i); }
      S.waves[sourceId] = { binMs: got.wave.bin_ms, peaks: peaks };
      requestLane();
    });
  }
  function paintLane() {
    var c = laneCanvas();
    if (!c || !S.doc || !S.spans.length) { return; }
    var width = c.clientWidth;
    var height = c.clientHeight;
    var ratio = W.devicePixelRatio || 1;
    if (c.width !== Math.round(width * ratio)) { c.width = Math.round(width * ratio); }
    if (c.height !== Math.round(height * ratio)) { c.height = Math.round(height * ratio); }
    var ctx = c.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    var head = S.playhead * seqTicks();
    if (S.playing) { S.lane = LN.follow(laneView(), head, width); }
    var view = laneView();
    var band = S.settings.laneClipPx;
    ctx.fillStyle = cssVar("--vl-surface-sunken");
    ctx.fillRect(0, 0, width, height);
    paintLaneClips(ctx, view, width, band);
    ctx.save();
    ctx.translate(0, band);
    paintLaneWave(ctx, view, width, height - band);
    paintLaneCues(ctx, view, height - band);
    if (S.laneSel) {
      ctx.fillStyle = cssVar("--vl-accent-soft");
      var a = LN.x(view, S.laneSel[0]);
      ctx.fillRect(a, 0, LN.x(view, S.laneSel[1]) - a, height - band);
    }
    ctx.restore();
    var ph = LN.x(view, head);
    ctx.fillStyle = cssVar("--vl-playhead");
    ctx.fillRect(Math.round(ph), 0, 1, height);
  }
  // 클립 띠(W-100, D169) — Final Cut 스토리라인처럼 클립마다 블록, 끝을 끌어 길이를 고친다.
  function paintLaneClips(ctx, view, width, band) {
    var seq = seqTicks();
    var rate = laneRate();
    S.spans.forEach(function (span, i) {
      var xs = LN.x(view, span.outStart * seq);
      var xe = LN.x(view, (span.outStart + span.outFrames) * seq);
      if (xe < 0 || xs > width) { return; }
      var chosen = i === S.sel;
      ctx.globalAlpha = chosen ? 0.8 : 0.5;
      ctx.fillStyle = cssVar(chosen ? "--vl-accent" : "--vl-caption-chip");
      ctx.fillRect(xs + 1, 1, Math.max(1, xe - xs - 2), band - 2);
      ctx.globalAlpha = 1;
      ctx.fillStyle = cssVar(chosen ? "--vl-ink" : "--vl-ink-muted");
      ctx.fillRect(xs + 1, 1, 2, band - 2);
      ctx.fillRect(xe - 3, 1, 2, band - 2);
      if (xe - xs > 48) {
        ctx.fillStyle = cssVar("--vl-ink");
        ctx.font = "11px " + cssVar("--vl-font-sans");
        var seconds = span.outFrames * seq / rate;
        ctx.fillText("#" + (i + 1) + " " + nameOf(span.sourceId) + " · " +
          seconds.toFixed(1) + "초", xs + 6, band / 2 + 4, xe - xs - 12);
      }
    });
  }
  function paintLaneWave(ctx, view, width, height) {
    var unit = TL.msTicks(S.doc.facts);
    var mid = height / 2;
    ctx.fillStyle = cssVar("--vl-ink-faint");
    TL.tickSpans(S.doc).forEach(function (row) {
      var x0 = Math.max(0, Math.floor(LN.x(view, row[3])));
      var x1 = Math.min(width, Math.ceil(LN.x(view, row[4])));
      if (x1 <= 0 || x0 >= width) { return; }
      ctx.fillRect(Math.round(LN.x(view, row[3])), 0, 1, height);  // 컷
      var wave = (S.waves || {})[row[0]];
      if (!wave) { askWave(row[0]); return; }
      if (!wave.peaks) { return; }
      for (var x = x0; x < x1; x += 1) {
        var ms = (row[1] + LN.ticksAt(view, x) - row[3]) / unit;
        var amp = (wave.peaks[Math.floor(ms / wave.binMs)] || 0) / 255;
        var h = Math.max(1, amp * (height - 4));
        ctx.fillRect(x, mid - h / 2, 1, h);
      }
    });
  }
  function paintLaneCues(ctx, view, height) {
    var rows = captionRows();
    var drag = S.laneDrag;
    S.proj.cues.forEach(function (cue, index) {
      var start = cue[2];
      var end = cue[3];
      if (drag && drag.moved && drag.index === index) {
        if (drag.part === "start") { start = drag.value; }
        else if (drag.part === "end") { end = drag.value; }
        else if (drag.part === "body") { start += drag.value; end += drag.value; }
      }
      if (drag && drag.moved && drag.part === "many" && drag.indices.indexOf(index) >= 0) {
        start += drag.value; end += drag.value;
      }
      if (drag && drag.moved && drag.neighbor === index) {
        if (drag.part === "end") { start = drag.neighborValue; } else { end = drag.neighborValue; }
      }
      var xs = LN.x(view, start);
      var xe = LN.x(view, end);
      var row = rows[index];
      ctx.globalAlpha = 0.55;
      ctx.fillStyle = cssVar(isPicked(cue[0]) ? "--vl-accent" : "--vl-caption-chip");
      ctx.fillRect(xs, 2, Math.max(1, xe - xs), height - 4);
      ctx.globalAlpha = 1;
      ctx.fillStyle = cssVar("--vl-tier-green-ink");
      ctx.fillRect(xs, 2, 2, height - 4);
      ctx.fillStyle = cssVar("--vl-danger");
      ctx.fillRect(xe - 2, 2, 2, height - 4);
      if (xe - xs > 40) {
        ctx.fillStyle = cssVar("--vl-ink");
        ctx.font = "11px " + cssVar("--vl-font-sans");
        ctx.fillText("#" + row.n + " " + row.seconds.toFixed(2) + "초" +
          (row.cps === null ? "" : " · " + row.cps.toFixed(1)), xs + 5, 14, xe - xs - 8);
        ctx.fillText(CG.oneLine(cue[4]), xs + 5, height - 8, xe - xs - 8);
      }
    });
  }
  function laneX(ev) { return ev.clientX - laneCanvas().getBoundingClientRect().left; }
  function frameAtTicks(ticks) {
    return clamp(Math.floor(ticks / seqTicks()), 0, Math.max(0, S.total - 1));
  }
  function onLaneDown(ev) {
    if (!S.doc || !S.spans.length || ev.button === 2) { return; }
    commitText();
    var view = laneView();
    var x = laneX(ev);
    var ticks = clamp(LN.ticksAt(view, x), 0, laneTotal());
    if (S.rootEl) { S.rootEl.focus({ preventScroll: true }); }
    var y = ev.clientY - laneCanvas().getBoundingClientRect().top;
    if (y < S.settings.laneClipPx) { clipBandDown(ev, x, ticks); return; }
    var mod = ev.metaKey || ev.ctrlKey;
    var target = S.cue || targetCue();
    if (ev.shiftKey && !mod && target) {
      cueOp("start", target, undefined, frameAtTicks(ticks));
      return;
    }
    if (mod && !ev.shiftKey && target) {
      cueOp("end", target, undefined, frameAtTicks(ticks));
      return;
    }
    var hit = LN.hit(S.proj.cues, view, x, S.settings.snapPx);
    // ⌥ + 가장자리 = 이웃 가장자리도 함께 끈다(W-87c) · ⌥ + 그 밖 = 길이째 그 자리로 옮기기.
    var pair = ev.altKey && hit && hit.part !== "body";
    if (ev.altKey && !pair && target) { laneShiftTo(target, ticks); return; }
    if (S.playing) { stopPlayback(); }
    S.laneSel = null;
    var part = hit ? hit.part : "new";
    var indices = [];
    if (hit && part === "body" && S.picked.length > 1 && S.picked.indexOf(hit.id) >= 0) {
      part = "many";
      indices = S.proj.cues.map(function (cue, i) { return isPicked(cue[0]) ? i : -1; })
        .filter(function (i) { return i >= 0; });
    } else {
      S.picked = hit ? [hit.id] : [];
      S.anchor = hit ? hit.id : null;
    }
    if (hit) { S.cue = hit.id; }
    seek(frameAtTicks(ticks));
    S.laneDrag = { index: hit ? hit.index : -1, id: hit ? hit.id : null, part: part,
      pair: !!pair, indices: indices, neighbor: -1, neighborValue: null,
      origin: ticks, x: x, moved: false, value: null };
    laneCanvas().setPointerCapture(ev.pointerId);
    paintCues();
    requestLane();
  }
  // 클립 띠를 누르면 키 창은 「타임라인」이다(D154 — ⌫는 클립) · 고른 자막은 풀린다.
  function clipBandDown(ev, x, ticks) {
    var hit = LN.clipHit(S.spans, laneView(), x, S.settings.snapPx, seqTicks());
    if (S.playing) { stopPlayback(); }
    commitText();
    clearPicked();
    S.laneSel = null;
    S.pane = "timeline";
    if (S.rootEl) { S.rootEl.setAttribute("data-tl-pane", "timeline"); }
    if (!hit) { paint(); return; }
    S.sel = hit.index;
    S.cursor = null;
    if (hit.part === "body") { seek(frameAtTicks(ticks)); paint(); return; }
    var clip = S.doc.clips[hit.index];
    var side = hit.part === "start" ? TL.SIDE_START : TL.SIDE_END;
    S.clipDrag = { index: hit.index, side: side, base: S.doc, origin: ticks,
      edge: side === TL.SIDE_START ? clip[1] : clip[2], frame: null };
    laneCanvas().setPointerCapture(ev.pointerId);
    paint();
  }
  // 끄는 동안은 문서를 미리 바꿔 그린다(기록·저장 없음) — 뒤 클립·파형·자막이 함께 밀린다.
  // 자막은 원본 시각에 붙어 있어 제 말과 함께 움직이고, 컷 밖으로 나간 자막은 가려질 뿐
  // 문서에 남는다(규칙 9 — 다시 늘리면 돌아온다).
  function clipBandMove(ev) {
    var drag = S.clipDrag;
    var ticks = LN.ticksAt(laneView(), laneX(ev));
    if (!ev.shiftKey && !S.snapOff) {
      var snaps = [S.playhead * seqTicks()];
      TL.layout(drag.base).forEach(function (span, i) {
        if (i !== drag.index) { snaps.push(span.outStart * seqTicks()); }
      });
      ticks = LN.snap(ticks, snaps, S.settings.snapPx * laneView().tpp);
    }
    var clip = drag.base.clips[drag.index];
    var want = LN.trimFrame(drag.edge, ticks - drag.origin,
      TL.sourceFrameTicks(drag.base.facts, clip[0]));
    var frame = TL.clampEdge(drag.base, drag.index, drag.side, want, S.settings);
    if (frame === drag.frame) { return; }
    drag.frame = frame;
    S.doc = frame === drag.edge ? drag.base
      : (drag.side === TL.SIDE_START ? TL.setClipStart(drag.base, drag.index, frame, S.settings)
        : TL.setClipEnd(drag.base, drag.index, frame, S.settings));
    relayout();
    showSourceFrame(clip[0], drag.side === TL.SIDE_START ? frame : frame - 1);
    paint();
  }
  function clipBandUp() {
    var drag = S.clipDrag;
    S.clipDrag = null;
    var moved = S.doc;
    S.doc = drag.base;
    relayout();
    if (moved === drag.base) { paint(); return; }
    var before = TL.project(drag.base).cues.length;
    var after = TL.project(moved).cues.length;
    var verb = drag.side === TL.SIDE_START ? "앞" : "뒤";
    var label = "클립 " + (drag.index + 1) + " " + verb + " " +
      (drag.frame - drag.edge > 0 ? "+" : "") + (drag.frame - drag.edge) + "프레임";
    if (after < before) { label += " · 자막 " + (before - after) + "개 가려짐(늘리면 돌아옴)"; }
    if (after > before) { label += " · 자막 " + (after - before) + "개 돌아옴"; }
    change(moved, label);
    var span = S.spans[drag.index];
    seek(drag.side === TL.SIDE_START ? span.outStart : span.outStart + span.outFrames - 1);
  }
  function onLaneMove(ev) {
    var c = laneCanvas();
    var view = laneView();
    var x = laneX(ev);
    if (S.clipDrag) { clipBandMove(ev); return; }
    var drag = S.laneDrag;
    if (!drag) {
      var y = ev.clientY - c.getBoundingClientRect().top;
      if (y < S.settings.laneClipPx) {
        var clip = S.spans.length ? LN.clipHit(S.spans, view, x, S.settings.snapPx, seqTicks())
          : null;
        c.style.cursor = !clip ? "default" : (clip.part === "body" ? "pointer" : "ew-resize");
        return;
      }
      var hover = LN.hit(S.proj ? S.proj.cues : [], view, x, S.settings.snapPx);
      c.style.cursor = !hover ? "default" : (hover.part === "body" ? "grab" : "ew-resize");
      return;
    }
    if (Math.abs(x - drag.x) < 2 && !drag.moved) { return; }
    drag.moved = true;
    var ticks = clamp(LN.ticksAt(view, x), 0, laneTotal());
    var cues = S.proj.cues;
    if (drag.part === "start" || drag.part === "end") {
      var snapped = ev.shiftKey || S.snapOff ? ticks
        : LN.snap(ticks, laneSnaps(), S.settings.snapPx * view.tpp);
      var paired = drag.pair ? LN.edgePair(cues, drag.index, drag.part, snapped, seqTicks()) : null;
      drag.neighbor = paired ? paired.neighbor : -1;
      drag.neighborValue = paired ? paired.neighborValue : null;
      drag.value = paired ? paired.value : LN.edge(cues, drag.index, drag.part, snapped,
        laneMinGap(), seqTicks(), laneTotal());
    } else if (drag.part === "many") {
      drag.value = LN.bodyMany(cues, drag.indices, ticks - drag.origin, laneMinGap(), laneTotal());
    } else if (drag.part === "body") {
      drag.value = LN.body(cues, drag.index, ticks - drag.origin, laneMinGap(), laneTotal());
    } else {
      drag.value = LN.range(drag.origin, ticks, laneTotal());
      S.laneSel = drag.value;
    }
    requestLane();
  }
  function onLaneUp() {
    if (S.clipDrag) { clipBandUp(); return; }
    var drag = S.laneDrag;
    S.laneDrag = null;
    if (!drag || !drag.moved || drag.value === null) { requestLane(); return; }
    if (drag.part === "new") {
      say("새 선택 — Enter로 이 구간에 새 자막을 넣고 Esc로 취소합니다.", false);
      requestLane();
      return;
    }
    if (drag.part === "body") { laneShift(drag.id, drag.value, "자막 옮기기"); return; }
    if (drag.part === "many") { laneShiftMany(drag.indices, drag.value); return; }
    if (drag.neighbor >= 0) {
      laneSetPair(drag.id, drag.part, drag.value, S.proj.cues[drag.neighbor][0],
        drag.neighborValue);
      return;
    }
    laneSetEdge(drag.id, drag.part, drag.value);
  }
  function laneEdit(label, build) {
    try {
      var next = build();
      change(next, label);
      paint();
    } catch (error) {
      if (!TL.isError(error)) { throw error; }
      say(label + " — 하지 못했습니다: " + error.message, true);
      requestLane();
    }
  }
  function laneSetEdge(id, part, ticks) {
    laneEdit(part === "start" ? "자막 시작(파형)" : "자막 끝(파형)", function () {
      var cue = rawCue(id);
      var at = TL.sourceMsAt(S.doc, ticks, part === "start" ? TL.SIDE_START : TL.SIDE_END);
      if (at[0] !== cue[1]) {
        throw { tlError: true, message: "그 자리는 이 자막의 원본이 아니다" };
      }
      return TL.cueSetTimes(S.doc, id, part === "start" ? at[1] : cue[2],
        part === "start" ? cue[3] : at[1]);
    });
  }
  // ⌥ 가장자리 — 두 줄을 한 번의 변경으로(실행 취소 한 번). 각 자리는 제 원본이어야 한다.
  function laneSetPair(id, part, ticks, neighborId, neighborTicks) {
    laneEdit("자막 경계 함께 옮기기(⌥)", function () {
      var doc = S.doc;
      [[id, part, ticks], [neighborId, part === "end" ? "start" : "end", neighborTicks]]
        .forEach(function (move) {
          var cue = rawCue(move[0]);
          var at = TL.sourceMsAt(doc, move[2], move[1] === "start" ? TL.SIDE_START : TL.SIDE_END);
          if (at[0] !== cue[1]) {
            throw { tlError: true, message: "그 자리는 그 자막의 원본이 아니다" };
          }
          doc = TL.cueSetTimes(doc, move[0], move[1] === "start" ? at[1] : cue[2],
            move[1] === "start" ? cue[3] : at[1]);
        });
      return doc;
    });
  }
  function laneShiftMany(indices, deltaTicks) {
    var ids = indices.map(function (i) { return S.proj.cues[i][0]; });
    laneEdit("자막 " + ids.length + "개 옮기기", function () {
      var ms = Math.round(deltaTicks / TL.msTicks(S.doc.facts));
      return ids.reduce(function (doc, id) { return TL.cueShift(doc, id, ms); }, S.doc);
    });
  }
  // ⌫ — 여러 줄을 한 번의 변경으로 지운다(실행 취소 한 번, 문서 연산 그대로).
  function deletePicked() {
    var ids = S.picked.slice();
    laneEdit("자막 " + ids.length + "개 지우기", function () {
      return ids.reduce(function (doc, id) { return TL.cueDelete(doc, id); }, S.doc);
    });
    clearPicked();
    paintCues();
  }
  function laneShift(id, deltaTicks, label) {
    laneEdit(label, function () {
      return TL.cueShift(S.doc, id, Math.round(deltaTicks / TL.msTicks(S.doc.facts)));
    });
  }
  // ⌥누름 — 고른 자막을 길이째 옮겨 그 자리에서 시작하게(이웃·간격 안으로).
  function laneShiftTo(id, ticks) {
    var index = S.proj.cues.map(function (cue) { return cue[0]; }).indexOf(id);
    if (index < 0) { say("컷에 가려진 자막은 옮기지 않습니다.", true); return; }
    var delta = LN.body(S.proj.cues, index, ticks - S.proj.cues[index][2], laneMinGap(),
      laneTotal());
    laneShift(id, delta, "자막 옮기기(⌥)");
  }
  // Enter — 새 선택 구간에 빈 자막을 넣고 글 칸으로(같은 원본 안에서만 — 문서 연산이 가른다).
  function insertSelection() {
    // 이웃 자막과 최소 간격을 둔 빈칸 안으로 맞춘다(W-101, D170) — 자리가 없으면 넣지 않는다.
    var span = S.spans.filter(function (row) {
      return S.laneSel[0] < (row.outStart + row.outFrames) * seqTicks();
    })[0] || S.spans[S.spans.length - 1];
    var sel = LN.fitGap(S.proj.cues, S.laneSel[0], S.laneSel[1], laneSafeGap(), seqTicks(),
      span.outStart * seqTicks(), (span.outStart + span.outFrames) * seqTicks());
    S.laneSel = null;
    if (!sel) {
      say("새 자막 — 이웃 자막과 간격(" + S.settings.minGapMs + " ms)을 둘 빈자리가 없습니다." +
        " 자막 밖 빈 곳에서 끌어 주세요.", true);
      requestLane();
      return;
    }
    var id = "n" + S.doc.nextCue;
    laneEdit("새 자막(파형)", function () {
      var a = TL.sourceMsAt(S.doc, sel[0], TL.SIDE_START);
      var b = TL.sourceMsAt(S.doc, sel[1], TL.SIDE_END);
      if (a[0] !== b[0]) { throw { tlError: true, message: "선택이 두 원본에 걸쳐 있다" }; }
      return TL.cueInsert(S.doc, a[0], a[1], b[1], "");
    });
    if (rawCue(id)) { S.cue = id; paintCues(); focusCueText(); }
  }
  function onLaneWheel(ev) {
    if (!S.doc) { return; }
    ev.preventDefault();
    var view = laneView();
    if (ev.altKey || ev.ctrlKey || ev.metaKey) {
      var rate = laneRate();
      var factor = LN.wheelFactor(ev.deltaY, ev.deltaMode, S.settings.zoomStepPercent,
        S.settings.wheelZoomNotchPx);
      S.lane = LN.zoom(view, factor, laneX(ev), rate / S.settings.zoomMaxPxPerS,
        rate / S.settings.zoomMinPxPerS);
    } else {
      var delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      S.lane = { start: view.start + delta * view.tpp, tpp: view.tpp };
    }
    requestLane();
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
  // 「영어 만들기」(W-99, D174) — 저장한 리비전의 자막을 옮긴다(유료 · 누름이 승인, D110).
  // 저장 대기 편집을 먼저 보낸다. 끝나면 영어를 다시 찾아 영어 열·자막 겹에 보인다
  // (같은 글은 다시 사지 않는다).
  function translate() {
    commitText();
    if (S.conflict) {
      say("저장이 충돌한 채로는 옮기지 않습니다 — 먼저 정리하세요.", true);
      return;
    }
    if (S.dirty || S.saving) {
      say("편집을 저장한 뒤 영어로 옮깁니다…", false);
      afterSaved(translate);
      return;
    }
    var payload = { signature: S.signature, token: S.token, timelineId: S.timelineId,
      revision: S.baseRevision };
    if (W.__vlServer) { W.__vlServer.wake(true); }
    S.server.tl_translate(payload).then(function (got) {
      if (!got || !got.ok) {
        say((got && got.error) || "영어 만들기를 걸지 못했습니다.", true);
        return;
      }
      say("리비전 " + payload.revision + "의 자막을 영어로 옮깁니다 — 끝나면 영어 열과" +
        " 화면 자막에 보입니다.", false);
      watchTranslate(got.jobId);
    });
  }
  function watchTranslate(jobId) {
    var gen = S.pollGen;
    W.setTimeout(function () {
      if (gen !== S.pollGen) { return; }
      S.server.tl_job({ signature: S.signature, jobId: jobId }).then(function (got) {
        if (gen !== S.pollGen || !got || !got.ok) { return; }
        if (!got.ended) { watchTranslate(jobId); return; }
        if (got.status !== "succeeded") {
          say("영어를 만들지 못했습니다(" + got.status + ") — 「기록」에서 까닭을 보세요.", true);
          return;
        }
        say("영어를 만들었습니다 — 한국어 아래 영어 줄로 보이고 공유의 「한국어 + 영어」가 씁니다.",
          false);
        askEnglish();
      });
    }, POLL_MS);
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
        say("공유를 마쳤습니다 — 인스펙터 「공유」의 결과 카드 「초안 폴더 열기」에 영상이" +
          " 있습니다(자막을 입혔으면 <이름>.burned.mp4 · 영어 번역이 아직 없으면 한국어만).",
          false);
        if (W.__vlWindow && W.__vlWindow.showInspector) { W.__vlWindow.showInspector("share"); }
        // 새 초안의 저장된 영어가 생겼을 수 있다 — 번역 모드면 다시 찾는다.
        S.english = null;
        if (S.translation) { askEnglish(); }
      });
    }, POLL_MS);
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
    var out = { clips: S.doc.clips, cues: S.doc.cues, nextCue: S.doc.nextCue,
      seeded: S.doc.seeded };
    if (S.doc.english) { out.english = S.doc.english; }  // 고친 영어(D166) — 없으면 예전 모양
    return out;
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
  // 키는 표 하나(`ui/keymap.py`, W-89 · D154) — 키를 받은 칸이 든 창(없으면 마지막으로 누른 창)이
  // 줄을 고른다. 동작이 false를 돌려주면(할 일이 없다) 기본 동작을 막지 않는다.
  function paneOf(target) {
    if (!target || typeof target.closest !== "function") { return null; }
    if (target.closest(".tl-cues, .tl-cues-body")) { return "captions"; }
    if (target.closest("#tl-index-list")) {
      return indexState().tab === "cues" ? "captions" : "timeline";
    }
    if (target.closest(".tl-strip, .tl-range, .tl-transport, .tl-stage")) { return "timeline"; }
    return null;
  }
  function notePane(ev) {
    var pane = paneOf(ev.target);
    if (pane && pane !== S.pane) {
      S.pane = pane;
      if (S.rootEl) { S.rootEl.setAttribute("data-tl-pane", pane); }
    }
  }
  function onKey(ev) {
    if (!S.doc) { return; }
    var inText = editable(ev.target);
    if (inText && ev.target.id !== "tl-cue-text") { return; }
    var mod = ev.metaKey || ev.ctrlKey;
    if (S.menu && ev.code === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      closeMenu();
      return;
    }
    var laneKey = ev.code === "Enter" || ev.code === "Escape";
    if (!inText && S.laneSel && !mod && !ev.altKey && laneKey) {
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.code === "Enter") { insertSelection(); } else { S.laneSel = null; requestLane(); }
      return;
    }
    var pane = paneOf(ev.target) || S.pane || "timeline";
    var action = KEYS.match(KEYS.table, ev, pane, inText);
    var run = action ? keyActions()[action] : null;
    if (!run || run(ev) === false) { return; }
    ev.preventDefault();
    ev.stopPropagation();
  }
  function keyActions() {
    if (S.keyActions) { return S.keyActions; }
    var big = function () { return S.settings.bigStepFrames; };
    S.keyActions = {
      "leave-text": leaveText, toggle: toggle, stop: stopPlayback,
      "frame-back": function () { step(-1); }, "frame-forward": function () { step(1); },
      "big-back": function () { step(-big()); }, "big-forward": function () { step(big()); },
      "prev-cut": function () { goCut(-1); }, "next-cut": function () { goCut(1); },
      "go-start": function () { step(-S.total); }, "go-end": function () { step(S.total); },
      undo: function () { undo(false); }, redo: function () { undo(true); },
      save: function () { commitText(); saveNow(false); }, index: toggleIndex,
      blade: function () { clipOp("blade"); },
      "delete-clip": function () { if (S.range) { deleteRange(); } else { clipOp("delete"); } },
      "trim-start": function () { trimToCursor(TL.SIDE_START); },
      "trim-end": function () { trimToCursor(TL.SIDE_END); },
      "shuttle-back": function () { shuttle(-1); },
      "shuttle-forward": function () { shuttle(1); },
      "cue-step-back": function () { stepMs(-S.settings.captionStepMs); },
      "cue-step-forward": function () { stepMs(S.settings.captionStepMs); },
      "cue-small-back": function () { stepMs(-S.settings.captionSmallStepMs); },
      "cue-small-forward": function () { stepMs(S.settings.captionSmallStepMs); },
      "zoom-in": function () { return zoomLane(S.settings.zoomStepPercent / 100); },
      "zoom-out": function () { return zoomLane(100 / S.settings.zoomStepPercent); },
      "zoom-fit": fitLane, snapping: toggleSnap, share: focusShare,
      "mark-in": function () { markRange("in"); }, "mark-out": function () { markRange("out"); },
      "mark-clear": clearRange, "cue-split-caret": splitAtCaret,
      "cue-insert": function () { commitText(); cueOp("insert"); },
      "cue-start": function () { cueOp("start"); }, "cue-end": function () { cueOp("end"); },
      "cue-split": function () { cueOp("split"); }, "cue-merge": function () { cueOp("merge"); },
      "cue-extend": extendToNext,
      "prev-cue": function () { stepCue(-1); }, "next-cue": function () { stepCue(1); },
      "next-error": function () { stepError(1); }, "prev-error": function () { stepError(-1); },
      "cue-nudge-back": function () { return nudgeCues(-1); },
      "cue-nudge-forward": function () { return nudgeCues(1); },
      "cue-big-back": function () { return nudgeCues(-big()); },
      "cue-big-forward": function () { return nudgeCues(big()); },
      "edit-text": function (ev) {
        if (ev.code === "Enter" && ev.target && ev.target.tagName === "BUTTON") { return false; }
        var id = targetCue();
        if (!id) { say("고칠 자막이 없습니다 — 목록에서 고르세요.", true); return true; }
        S.cue = id;
        paintCues();
        focusCueText();
        return true;
      },
      "delete-cue": function () {
        if (S.picked.length > 1) { deletePicked(); return true; }
        if (!S.cue) { return false; }
        cueOp("delete", S.cue);
        return true;
      },
      "clear-cue": function () {
        if (!S.cue && !S.picked.length) { return false; }
        clearPicked();
        paintCues();
        requestLane();
        return true;
      }
    };
    return S.keyActions;
  }
  // ⌘⌥V(Subtitle Edit 「커서 위치에서 줄 나누기」) — 글은 입력칸의 커서에서, 시각은 플레이헤드에서
  // 나눈다. 고친 글을 먼저 확정하고, 나눈 뒤 입력칸을 다시 그린다 — 그러지 않으면 칸에 남은 온 글이
  // 다음 확정에서 왼쪽 줄로 되돌아간다.
  function splitAtCaret(ev) {
    var area = ev && ev.target && ev.target.id === "tl-cue-text" ? ev.target : $("tl-cue-text");
    var id = area ? area.getAttribute("data-cue") : null;
    if (!area || !id) { return false; }
    var caret = area.selectionStart;
    commitText();
    cueOp("split", id, caret);
    paintCueEditor(true);
    // 입력칸은 고치는 중이라 다시 그리기가 글을 덮지 않는다 — 나눈 왼쪽 글을 여기서 직접 넣는다.
    var kept = S.doc.cues.filter(function (row) { return row[0] === id; })[0];
    var fresh = $("tl-cue-text");
    if (kept && fresh && fresh.getAttribute("data-cue") === id) {
      fresh.value = kept[4];
      fresh.setSelectionRange(fresh.value.length, fresh.value.length);
    }
    return true;
  }

  // ─── 오른쪽 누름 메뉴(Subtitle Edit) — 자막 목록 · 파형 레인 ──────────
  // 누른 줄을 고르되 플레이헤드는 그대로 둔다(「종료점 설정」이 플레이헤드를 쓴다).
  // 메뉴는 이 화면의 DOM 하나이고 동작은 키 · 단추와 같은 연산이다.
  // 다른 곳을 누르거나 Esc면 닫힌다.
  var MENU_ITEMS = [["start", "시작점 설정"], ["end", "종료점 설정"], ["split", "줄 나누기"],
    ["merge", "다음 줄과 합치기"], ["extend", "다음 줄까지 늘리기"], ["delete", "지우기"]];
  function openMenu(ev, id) {
    ev.preventDefault();
    closeMenu();
    commitText();
    // 입력칸에 초점이 있으면 고친 줄이 바뀌어도 칸이 옛 줄에 머문다 — 초점을 재생기로 옮긴다.
    if (document.activeElement && document.activeElement.id === "tl-cue-text" && S.rootEl) {
      S.rootEl.focus({ preventScroll: true });
    }
    var items = id ? MENU_ITEMS.slice() : [];
    items.push(S.laneSel ? ["selection", "선택 영역에 새 자막 삽입"]
      : ["insert", "플레이헤드에 새 자막"]);
    if (id) {
      S.cue = id;
      S.picked = [id];
      S.anchor = id;
      paintCues();
      requestLane();
    }
    var menu = document.createElement("div");
    menu.className = "tl-menu";
    menu.setAttribute("role", "menu");
    menu.innerHTML = items.map(function (item) {
      return '<button type="button" role="menuitem" data-menu="' + item[0] + '">' + esc(item[1]) +
        "</button>";
    }).join("");
    menu.style.left = ev.clientX + "px";
    menu.style.top = ev.clientY + "px";
    menu.addEventListener("click", function (e) {
      var button = e.target.closest ? e.target.closest("[data-menu]") : null;
      if (button) { runMenu(button.getAttribute("data-menu"), id); }
    });
    document.body.appendChild(menu);
    S.menu = menu;
  }
  function runMenu(action, id) {
    closeMenu();
    if (action === "selection") { insertSelection(); return; }
    if (action === "insert") { cueOp("insert"); return; }
    if (action === "extend") { S.cue = id; extendToNext(); return; }
    cueOp(action, id);
  }
  function closeMenu() {
    if (!S.menu) { return; }
    if (S.menu.parentNode) { S.menu.parentNode.removeChild(S.menu); }
    S.menu = null;
  }
  function onLaneMenu(ev) {
    if (!S.doc || !S.spans.length) { return; }
    var hit = LN.hit(S.proj.cues, laneView(), laneX(ev), S.settings.snapPx);
    openMenu(ev, hit ? hit.id : null);
  }
  function onListMenu(ev) {
    var row = ev.target.closest ? ev.target.closest(".tl-cue-row") : null;
    if (!row || !S.doc) { return; }
    openMenu(ev, row.getAttribute("data-cue"));
  }

  // , · . — 고른 줄(여러 줄이면 함께)을 시퀀스 프레임만큼 민다. 이웃 · 최소 간격 · 끝 안에서만
  // (파형 몸통 끌기와 같은 계산 · 같은 문서 연산). 고른 줄이 없으면 할 일이 없다.
  function nudgeCues(frames) {
    var ids = visibleIds();
    var indices = pickedIds().map(function (id) { return ids.indexOf(id); })
      .filter(function (i) { return i >= 0; });
    if (!indices.length) { return false; }
    var delta = LN.bodyMany(S.proj.cues, indices, frames * seqTicks(), laneMinGap(), laneTotal());
    if (!delta) {
      say("더 밀 자리가 없습니다 — 이웃 자막 · 최소 간격 · 타임라인 끝.", true);
      return true;
    }
    var ms = nudgeMs(ids[indices[0]], delta, frames);
    var moved = indices.map(function (i) { return ids[i]; });
    laneEdit(moved.length > 1 ? "자막 " + moved.length + "개 밀기" : "자막 밀기", function () {
      return moved.reduce(function (doc, id) { return TL.cueShift(doc, id, ms); }, S.doc);
    });
    return true;
  }
  // 프레임 단위 밀기의 밀리초 — 시작을 「시작점 설정」과 같은 규칙(`TL.sourceMsAt` 프레임 시작)으로
  // 그 프레임에 놓는다. 틱을 밀리초로 내림하면 한 프레임(33.37ms → 33ms)이 같은 프레임에 남는다.
  // 이웃 · 간격에 막혀 덜 움직이거나 원본이 바뀌는 자리면 막힌 만큼만(틱 → 밀리초).
  function nudgeMs(id, deltaTicks, frames) {
    var plain = Math.round(deltaTicks / TL.msTicks(S.doc.facts));
    if (deltaTicks !== frames * seqTicks()) { return plain; }
    var cue = projectedCue(id);
    var raw = rawCue(id);
    var frame = Math.floor(cue[2] / seqTicks()) + frames;
    var at = TL.sourceMsAt(S.doc, clamp(frame, 0, S.total) * seqTicks(), TL.SIDE_START);
    if (at[0] !== raw[1]) { return plain; }
    // 반내림한 밀리초가 그 프레임 시작 바로 앞으로 돌아오면(앞 프레임으로 보인다)
    // 1ms 뒤가 그 프레임이다.
    var ms = at[1] - raw[2];
    var moved = TL.project(TL.cueShift(S.doc, id, ms)).cues.filter(function (row) {
      return row[0] === id;
    })[0];
    return moved && Math.floor(moved[2] / seqTicks()) < frame ? ms + 1 : ms;
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
      "force-save": function () { saveNow(true); }, render: render, index: toggleIndex,
      "index-clips": function () { indexTab("clips"); },
      "index-cues": function () { indexTab("cues"); }, translation: toggleTranslation,
      translate: translate };
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
    clearPicked();
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
      rootEl.addEventListener("pointerdown", notePane, true);
    }
    // 「자막」 작업공간(W-87c, D152)이 자막 목록 · 입력칸을 재생기 밖(브라우저 열)으로 옮기면
    // 키·누름이 뿌리까지 오지 않는다 — 밖에 있을 때만 같은 처리로 넘긴다(두 번 돌지 않게).
    var body = rootEl.querySelector(".tl-cues-body");
    if (body && !body.__tlBound) {
      body.__tlBound = true;
      body.addEventListener("keydown", function (ev) {
        if (S.rootEl && !S.rootEl.contains(body)) { onKey(ev); }
      });
      body.addEventListener("click", function (ev) {
        if (S.rootEl && !S.rootEl.contains(body)) { onClick(ev); }
      });
      body.addEventListener("pointerdown", function (ev) {
        if (S.rootEl && !S.rootEl.contains(body)) { notePane(ev); }
      }, true);
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
    var indexList = $("tl-index-list");
    if (indexList && !indexList.__tlBound) {
      indexList.__tlBound = true;
      indexList.addEventListener("pointerdown", onIndexDown);
    }
    var indexErrors = $("tl-index-errors");
    if (indexErrors && !indexErrors.__tlBound) {
      indexErrors.__tlBound = true;
      indexErrors.addEventListener("change", function () {
        indexState().errors = indexErrors.checked;
        paintIndex();
      });
    }
    var lane = laneCanvas();
    if (lane && !lane.__tlBound) {
      lane.__tlBound = true;
      lane.addEventListener("pointerdown", onLaneDown);
      lane.addEventListener("pointermove", onLaneMove);
      lane.addEventListener("pointerup", onLaneUp);
      lane.addEventListener("pointercancel", onLaneUp);
      lane.addEventListener("wheel", onLaneWheel, { passive: false });
      lane.addEventListener("contextmenu", onLaneMenu);
      if (W.ResizeObserver) { new W.ResizeObserver(requestLane).observe(lane); }
    }
    // 자막 겹의 크기는 화면 틀 크기에 따른다(W-98) — 틀이 바뀌면 다시 그린다.
    var overlay = $("tl-overlay");
    if (overlay && !overlay.__tlBound && overlay.parentNode) {
      overlay.__tlBound = true;
      if (W.ResizeObserver) {
        new W.ResizeObserver(function () { if (S.doc) { paintOverlay(); } })
          .observe(overlay.parentNode);
      }
    }
    var list = $("tl-cue-list");
    if (list && !list.__tlBound) {
      list.__tlBound = true;
      list.addEventListener("contextmenu", onListMenu);
      // 누르는 순간 고른다 — 글 칸이 먼저 확정되며 목록을 다시 지어도 누름을 잃지 않는다.
      list.addEventListener("pointerdown", function (ev) {
        var row = ev.target.closest ? ev.target.closest(".tl-cue-row") : null;
        if (!row || ev.button === 2) { return; }
        ev.preventDefault();
        pickFromList(row.getAttribute("data-cue"), ev);
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
    // 오른쪽 누름 메뉴는 메뉴 밖을 누르면 닫힌다(메뉴 안의 누름은 메뉴가 먼저 멈춘다).
    document.addEventListener("pointerdown", function (ev) {
      var menu = W.__vlTimelinePlayer && W.__vlTimelinePlayer.menu;
      if (!menu || menu.contains(ev.target)) { return; }
      if (menu.parentNode) { menu.parentNode.removeChild(menu); }
      if (W.__vlTimelinePlayer) { W.__vlTimelinePlayer.menu = null; }
    }, true);
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
