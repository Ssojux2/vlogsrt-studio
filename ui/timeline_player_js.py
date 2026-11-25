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
      history: { past: [], future: [] }, tool: "select", skimOn: true, skimX: null,
      dropAt: null, dropView: null, translating: null,
      lineAsking: null };
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
      // 파형 레인의 보기는 그 문서의 틱 단위다 — 다른 타임라인(레이트가 다르면 틱도 다르다)을 열면
      // 처음 줌으로 돌린다(W-97 — 29.97 문서가 30 문서의 보기를 물려받아 엉뚱하게 확대됐다).
      S.lane = null;
    }
    bindDom();
    restoreBurn();
    restoreTranslation();
    // 영어를 미리 찾아 둔다(무료 읽기) — 자막 겹은 영어 자막이 켜졌을 때만 보인다(W-112, D186).
    if (!S.english) { askEnglish(); }
    // 뷰어 하나(W-86b) — 화면(.tl-stage)을 위 덱 뷰어 칸으로 옮긴다(노드째, 청취자 그대로).
    if (W.__vlWindow && W.__vlWindow.adoptProgram) { W.__vlWindow.adoptProgram(S.rootEl); }
    showDeck();
    fitDeck();
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
    // 스키밍 중이면 스키머 자리에서 튼다(Final Cut) — 뷰어가 이미 그 프레임을 보이고 있다.
    if (S.skimX !== null && S.skimX !== undefined) {
      var skimmed = skimFrame();
      S.skimX = null;
      seek(skimmed);
    }
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
    paintSaveChip();
    paintTools();
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
  // 저장 표지(디자인 점검 U8) — 있는 상태(S.dirty · S.saving)만 읽는다. 폭이 고정이라 글이 바뀌어도
  // 이동 막대가 움직이지 않는다.
  var SAVE_CHIP = {
    saving: ["저장 중…", "is-saving"],
    dirty: ["● 저장 안 됨", "is-dirty"],
    saved: ["✓ 저장됨", "is-saved"],
  };
  function paintSaveChip() {
    var chip = $("tl-save");
    if (!chip) { return; }
    var row = SAVE_CHIP[S.saving ? "saving" : S.dirty ? "dirty" : "saved"];
    chip.hidden = !S.doc;
    if (chip.textContent !== row[0]) { chip.textContent = row[0]; }
    chip.className = "tl-save " + row[1];
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
    if (!englishOn()) { return ""; }
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
    placeOverlay(overlay, style, frame, unit, side);
    overlay.textContent = "";
    // 화면에서 모양을 고칠 때(W-107) 플레이헤드 아래 자막이 없으면 예시 줄로 잡을 것을 보인다.
    if (!cues.length && S.styling) { cues = [[null, null, 0, 0, STYLE_SAMPLE[0]]]; }
    cues.forEach(function (cue) {
      var english = cue[0] === null ? (englishOn() ? STYLE_SAMPLE[1] : "") : overlayEnglish(cue);
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
    paintStyleBox();
  }
  // 자리 — 위·아래 + 여백(D173), 또는 화면에서 끌어 옮긴 점(W-107, D181): 덩어리 가운데가 그 점이고
  // 접는 폭은 그대로 틀 너비 − 좌우 여백 둘이다(번인의 `\pos` + 정렬 5와 같다).
  function placeOverlay(overlay, style, frame, unit, side) {
    var point = style.x_permille !== null && style.x_permille !== undefined;
    overlay.className = "tl-overlay is-styled is-" + (point ? "point" : style.position);
    if (point) {
      var scale = S.burn.positionScale;
      var width = frame.width - 2 * side;
      overlay.style.width = width + "px";
      overlay.style.left = frame.width * style.x_permille / scale - width / 2 + "px";
      overlay.style.right = "";
      overlay.style.top = frame.height * style.y_permille / scale + "px";
      overlay.style.bottom = "";
      return;
    }
    overlay.style.width = "";
    overlay.style.left = side + "px";
    overlay.style.right = side + "px";
    overlay.style.top = style.position === "top" ? style.margin_px * unit + "px" : "";
    overlay.style.bottom = style.position === "top" ? "" : style.margin_px * unit + "px";
  }
  // 손잡이를 바꾸면(공유 탭 「자막 모양」) 저장과 함께 여기로도 온다 — 다시 그리기만 한다.
  // 손잡이에는 끌어 옮긴 자리가 없다 — 「위치」·「여백」이 그대로면 그 자리를 잇는다
  // (서버와 같은 규칙).
  S.setBurnStyle = function (style) {
    if (!S.burn || !S.doc) { return; }
    var old = S.burn.style || {};
    var keep = old.x_permille !== null && old.x_permille !== undefined &&
      style.position === old.position && Number(style.margin_px) === old.margin_px;
    S.burn.style = Object.assign({}, style, { x_permille: keep ? old.x_permille : null,
      y_permille: keep ? old.y_permille : null });
    paintOverlay();
  };

  // ─── 화면 자막 모양(W-107, D181) ───────────────────────────────────────────
  // 인스타그램·캡컷처럼 뷰어 위에서 고친다 — 자막을 끌어 옮기고(⇧ 한 축 · 가운데 선에 붙음 ·
  // ⌘ 붙지 않게), 모서리로 키우고(맞은편 모서리 기준 · ⇧ 가운데 기준 대칭), 막대로 크기·외곽선·색.
  // 모양은 전체 자막 하나다. 놓을 때 · 막대를 누를 때 저장하고(공유 탭 손잡이와 같은 자리 ·
  // 같은 검사) 공유 탭 손잡이를 저장값으로 다시 맞춘다. 실패하면 앞 모양으로 돌린다.
  var STYLE_SAMPLE = ["자막 모양 — 끌어 옮기기", "Drag to move"];
  function styleFrameEl() { var o = $("tl-overlay"); return o ? o.parentNode : null; }
  function setStyling(on) {
    S.styling = !!on && !!S.burn && !!S.doc;
    var frame = styleFrameEl();
    if (frame) { frame.classList.toggle("is-styling", S.styling); }
    var toggle = $("tl-style-toggle");
    if (toggle) { toggle.setAttribute("aria-pressed", S.styling ? "true" : "false"); }
    showGuides(false, false);
    paintOverlay();
  }
  // 덩어리 — 줄들을 감싼 사각형(틀 px). 줄이 없으면 null.
  function textBox() {
    var overlay = $("tl-overlay");
    var frame = styleFrameEl();
    if (!overlay || !frame || !overlay.children.length) { return null; }
    var f = frame.getBoundingClientRect();
    var l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
    Array.prototype.forEach.call(overlay.children, function (line) {
      var q = line.getBoundingClientRect();
      l = Math.min(l, q.left); t = Math.min(t, q.top);
      r = Math.max(r, q.right); b = Math.max(b, q.bottom);
    });
    return { left: l - f.left, top: t - f.top, w: r - l, h: b - t,
      frame: { w: f.width, h: f.height } };
  }
  function paintStyleBox() {
    var box = $("tl-style-box");
    var bar = $("tl-style-bar");
    var toggle = $("tl-style-toggle");
    if (toggle) { toggle.hidden = !S.burn; }
    var rect = S.styling ? textBox() : null;
    if (box) {
      box.hidden = !rect;
      if (rect) {
        box.style.left = rect.left + "px"; box.style.top = rect.top + "px";
        box.style.width = rect.w + "px"; box.style.height = rect.h + "px";
      }
    }
    if (!bar) { return; }
    bar.hidden = !S.styling;
    if (!S.styling) { return; }
    var style = S.burn.style;
    // 막대는 자막과 반대쪽 끝에 — 자막이 위쪽 절반이면 아래에 선다.
    bar.classList.toggle("is-bottom", !!rect && rect.top + rect.h / 2 < rect.frame.h / 2);
    $("tl-style-font").textContent = style.font_px;
    $("tl-style-outline").textContent = style.outline_px;
    var color = $("tl-style-color");
    if (color && W.document.activeElement !== color) {
      color.value = style.text_color.toLowerCase();
    }
  }
  function showGuides(vertical, horizontal) {
    var frame = styleFrameEl();
    if (!frame) { return; }
    var v = frame.querySelector(".tl-guide-v");
    var h = frame.querySelector(".tl-guide-h");
    if (v) { v.hidden = !vertical; }
    if (h) { h.hidden = !horizontal; }
  }
  function withPoint(style, point) { return Object.assign({}, style, point); }
  function onStyleDown(ev) {
    if (!S.burn || !S.doc || ev.button !== 0) { return; }
    if (!S.styling) { setStyling(true); }
    var rect = textBox();
    if (!rect) { return; }
    ev.preventDefault();
    ev.stopPropagation();
    if (S.playing) { stopPlayback(); }
    var grip = ev.target.getAttribute ? ev.target.getAttribute("data-grip") : null;
    var center = { x: rect.left + rect.w / 2, y: rect.top + rect.h / 2 };
    var drag = { grip: grip, x0: ev.clientX, y0: ev.clientY, center: center, rect: rect,
      moved: false, before: Object.assign({}, S.burn.style), font: S.burn.style.font_px,
      outline: S.burn.style.outline_px };
    if (grip) {
      drag.sign = { x: grip.charAt(1) === "e" ? 1 : -1, y: grip.charAt(0) === "s" ? 1 : -1 };
      var half = { x: drag.sign.x * rect.w / 2, y: drag.sign.y * rect.h / 2 };
      drag.corner = { x: center.x + half.x, y: center.y + half.y };
      drag.anchor = { x: center.x - half.x, y: center.y - half.y };
    }
    S.styleDrag = drag;
    if (ev.currentTarget.setPointerCapture) { ev.currentTarget.setPointerCapture(ev.pointerId); }
  }
  function onStyleMove(ev) {
    var drag = S.styleDrag;
    if (!drag) { return; }
    var dx = ev.clientX - drag.x0;
    var dy = ev.clientY - drag.y0;
    if (!drag.moved && Math.hypot(dx, dy) < S.settings.styleDragPx) { return; }
    drag.moved = true;
    if (drag.grip) { scaleStyle(drag, ev, dx, dy); return; }
    var at = LN.styleMove(drag.center, dx, dy, drag.rect, drag.rect.frame, { lock: ev.shiftKey,
      snap: !(ev.metaKey || ev.ctrlKey), snapPx: S.settings.snapPx });
    showGuides(at.guides.v, at.guides.h);
    S.burn.style = withPoint(S.burn.style,
      LN.stylePoint(at, drag.rect.frame, S.burn.positionScale));
    paintOverlay();
  }
  // 모서리 끌기 — 글자(외곽선도 같은 비율로) 크기. 기본은 맞은편 모서리를 제자리에 두고,
  // ⇧면 가운데를 두고 위아래·좌우 대칭으로 커진다. 위·아래 자리였어도 그 가운데가 자리가 된다.
  function scaleStyle(drag, ev, dx, dy) {
    var ranges = S.burn.ranges;
    var scale = S.burn.positionScale;
    var from = ev.shiftKey ? drag.center : drag.anchor;
    var now = { x: drag.corner.x + dx, y: drag.corner.y + dy };
    var font = LN.styleScale(drag.font, from, drag.corner, now, ranges.font_px);
    var outline = LN.clampInt(drag.outline * font / drag.font, ranges.outline_px);
    var sized = Object.assign({}, S.burn.style, { font_px: font, outline_px: outline });
    S.burn.style = withPoint(sized, LN.stylePoint(drag.center, drag.rect.frame, scale));
    paintOverlay();
    if (ev.shiftKey) { return; }
    var rect = textBox();
    if (!rect) { return; }
    var held = LN.styleAnchor(drag.anchor, drag.sign, rect);
    S.burn.style = withPoint(S.burn.style, LN.stylePoint(held, drag.rect.frame, scale));
    paintOverlay();
  }
  function onStyleUp() {
    var drag = S.styleDrag;
    S.styleDrag = null;
    showGuides(false, false);
    if (drag && drag.moved) { saveStyle(drag.before); }
  }
  function onStyleBar(ev) {
    var target = ev.target.closest ? ev.target.closest("[data-style]") : null;
    if (!target || !S.burn) { return; }
    ev.stopPropagation();
    var act = target.getAttribute("data-style");
    if (act === "done") { setStyling(false); return; }
    var before = Object.assign({}, S.burn.style);
    var next = styleAction(act, Object.assign({}, before));
    if (!next) { return; }
    S.burn.style = next;
    paintOverlay();
    saveStyle(before);
  }
  function styleAction(act, style) {
    var ranges = S.burn.ranges;
    var step = S.settings.styleFontStep;
    if (act === "font-up" || act === "font-down") {
      style.font_px = LN.clampInt(style.font_px + (act === "font-up" ? step : -step),
        ranges.font_px);
    } else if (act === "outline-up" || act === "outline-down") {
      style.outline_px = LN.clampInt(style.outline_px + (act === "outline-up" ? 1 : -1),
        ranges.outline_px);
    } else if (act === "center-x") {
      var rect = textBox();
      if (!rect) { return null; }
      style = withPoint(style, LN.stylePoint({ x: rect.frame.w / 2, y: rect.top + rect.h / 2 },
        rect.frame, S.burn.positionScale));
    } else if (act === "anchor") {
      style.x_permille = null; style.y_permille = null;
    } else if (act === "reset") {
      style = Object.assign({}, S.burn.defaults);
    } else { return null; }
    return style;
  }
  function onStyleColorInput(ev) {
    if (!S.burn) { return; }
    if (!S.colorBefore) { S.colorBefore = Object.assign({}, S.burn.style); }
    S.burn.style = Object.assign({}, S.burn.style, { text_color: ev.target.value.toUpperCase() });
    paintOverlay();
  }
  function onStyleColorChange(ev) {
    onStyleColorInput(ev);
    var before = S.colorBefore || Object.assign({}, S.burn.style);
    S.colorBefore = null;
    saveStyle(before);
  }
  function saveStyle(before) {
    S.styleGen = (S.styleGen || 0) + 1;
    var gen = S.styleGen;
    var payload = { signature: S.signature, token: S.token,
      style: Object.assign({}, S.burn.style) };
    S.server.tl_style(payload).catch(function () {
      return { ok: false, error: "자막 모양을 저장하지 못했습니다 — 서버가 답하지 않았습니다." };
    }).then(function (got) {
      if (gen !== S.styleGen || !S.burn) { return; }
      if (!got || !got.ok) {
        S.burn.style = before;
        paintOverlay();
        say((got && got.error) || "자막 모양을 저장하지 못했습니다.", true);
        return;
      }
      S.burn.style = got.style;
      paintOverlay();
      say("자막 모양을 저장했습니다 — 최종출력이 이 모양으로 입힙니다(전체 자막).", false);
      syncShareKnobs();
    });
  }
  // 공유 탭 손잡이를 저장값으로 다시 맞춘다 — 그 숨은 그라디오 단추(되살리기, 저장 고리 밖).
  function syncShareKnobs() {
    var source = W.document.getElementById("vl-burn-sync");
    if (source && source.tagName !== "BUTTON" && source.querySelector) {
      source = source.querySelector("button") || source;
    }
    if (source && typeof source.click === "function") { source.click(); }
  }
  function bindStyleTools() {
    var overlay = $("tl-overlay");
    var box = $("tl-style-box");
    [overlay, box].forEach(function (el) {
      if (!el || el.__tlStyleBound) { return; }
      el.__tlStyleBound = true;
      el.addEventListener("pointerdown", onStyleDown);
      el.addEventListener("pointermove", onStyleMove);
      el.addEventListener("pointerup", onStyleUp);
      el.addEventListener("pointercancel", onStyleUp);
      // 화면 자막을 누르면 고르기만 한다 — 영상 누름(재생 · 멈춤)으로 넘기지 않는다.
      el.addEventListener("click", function (ev) { ev.stopPropagation(); });
    });
    var bar = $("tl-style-bar");
    if (bar && !bar.__tlBound) {
      bar.__tlBound = true;
      bar.addEventListener("click", onStyleBar);
      bar.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); });
    }
    var color = $("tl-style-color");
    if (color && !color.__tlBound) {
      color.__tlBound = true;
      color.addEventListener("input", onStyleColorInput);
      color.addEventListener("change", onStyleColorChange);
    }
    var toggle = $("tl-style-toggle");
    if (toggle && !toggle.__tlBound) {
      toggle.__tlBound = true;
      toggle.addEventListener("click", function (ev) {
        ev.stopPropagation();
        setStyling(!S.styling);
      });
    }
    // ⌘B(Ctrl+B)는 키가 재생기 밖(본문 · 위로 옮긴 뷰어 · 영상 목록)에 있어도 자른다(W-112, D186).
    // 재생기 안의 키는 onKey가 먼저 받아 멈추므로 여기에 오지 않는다. 글 칸 안은 건드리지 않는다.
    if (!W.__tlBladeKey) {
      W.__tlBladeKey = true;
      W.document.addEventListener("keydown", function (ev) {
        var mod = ev.metaKey || ev.ctrlKey;
        if (!mod || ev.code !== "KeyB" || ev.shiftKey || ev.altKey) { return; }
        if (ev.defaultPrevented) { return; }
        if (!S.doc || !S.rootEl || !S.rootEl.isConnected || editable(ev.target)) { return; }
        ev.preventDefault();
        clipOp("blade");
      });
    }
    if (!W.__tlStyleEsc) {
      W.__tlStyleEsc = true;
      W.document.addEventListener("keydown", function (ev) {
        if (S.styling && ev.key === "Escape") { setStyling(false); }
      });
    }
  }
  function cueText() {
    var t = S.playhead * TL.sequenceFrameTicks(S.doc.facts);
    return S.proj.cues.filter(function (cue) {
      return cue[2] === cue[3] ? cue[2] === t : cue[2] <= t && t < cue[3];
    }).map(function (cue) { return cue[4]; }).join("\n");
  }
  function cssVar(name) { return getComputedStyle(S.rootEl).getPropertyValue(name).trim(); }
  // ─── 캔버스 공통(디자인 점검 U4 · U5) — 세 캔버스가 같은 플레이헤드 · 같은 시간 눈금 ─────
  // 눈금 간격은 글자가 적어도 RULER_MIN_GAP_PX 떨어지는 가장 작은 걸음이다(그리기만 — 문서·시각과
  // 무관하다). 빨강은 오류 몫이라 플레이헤드는 `--vl-playhead` + 양옆 1px `--vl-playhead-casing`.
  var RULER_PX = 14;
  var RULER_MIN_GAP_PX = 60;
  var RULER_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];
  var HEAD_PX = 8;
  var CANVAS_FONT_PX = 12;
  var RULER_FONT_PX = 11;
  function canvas2d(c) {
    var w = c.clientWidth;
    var h = c.clientHeight;
    if (!w || !h) { return null; }
    var ratio = W.devicePixelRatio || 1;
    if (c.width !== Math.round(w * ratio)) { c.width = Math.round(w * ratio); }
    if (c.height !== Math.round(h * ratio)) { c.height = Math.round(h * ratio); }
    var g = c.getContext("2d");
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, w, h);
    return g;
  }
  function clockText(seconds) {
    var whole = Math.max(0, Math.floor(seconds + 1e-6));
    var rest = whole % 60;
    return Math.floor(whole / 60) + ":" + (rest < 10 ? "0" : "") + rest;
  }
  function rulerStep(pxPerSecond) {
    for (var i = 0; i < RULER_STEPS.length; i++) {
      if (RULER_STEPS[i] * pxPerSecond >= RULER_MIN_GAP_PX) { return RULER_STEPS[i]; }
    }
    return RULER_STEPS[RULER_STEPS.length - 1];
  }
  // 띠 하나(top부터 RULER_PX) — x = (t − t0) · pxPerSecond. 큰 눈금에 m:ss, 사이에 짧은 눈금.
  function paintRuler(g, width, top, t0, pxPerSecond) {
    if (!(pxPerSecond > 0)) { return; }
    var step = rulerStep(pxPerSecond);
    var line = cssVar("--vl-line-strong");
    g.fillStyle = cssVar("--vl-surface-raised");
    g.fillRect(0, top, width, RULER_PX);
    g.fillStyle = line;
    g.fillRect(0, top + RULER_PX - 1, width, 1);
    g.font = RULER_FONT_PX + "px " + cssVar("--vl-font-mono");
    g.textBaseline = "middle";
    var half = step / 2;
    for (var t = Math.ceil(t0 / half) * half; (t - t0) * pxPerSecond <= width; t += half) {
      var x = Math.round((t - t0) * pxPerSecond);
      var major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      g.fillStyle = line;
      g.fillRect(x, top + RULER_PX - (major ? 6 : 3), 1, major ? 6 : 3);
      if (major) {
        g.fillStyle = cssVar("--vl-ink-muted");
        g.fillText(clockText(t), x + 3, top + RULER_PX / 2 - 1);
      }
    }
  }
  // 플레이헤드 — 2px 선 · 양옆 1px 테두리 · 위에 8px 아래 삼각형 머리(세 캔버스가 같다).
  function paintHead(g, x, h) {
    var px = Math.round(x);
    var tip = HEAD_PX / 2;
    g.fillStyle = cssVar("--vl-playhead-casing");
    g.fillRect(px - 2, 0, 4, h);
    g.beginPath();
    g.moveTo(px - tip - 1, 0); g.lineTo(px + tip + 1, 0); g.lineTo(px, tip + 2);
    g.closePath(); g.fill();
    g.fillStyle = cssVar("--vl-playhead");
    g.fillRect(px - 1, 0, 2, h);
    g.beginPath();
    g.moveTo(px - tip, 0); g.lineTo(px + tip, 0); g.lineTo(px, tip);
    g.closePath(); g.fill();
  }
  // 칸에 맞춰 줄인다 — 다 안 들면 말줄임, 한 글자도 안 들면 빈 글(그리지 않는다).
  function fitText(g, text, room) {
    if (g.measureText(text).width <= room) { return text; }
    var cut = text;
    while (cut.length > 1 && g.measureText(cut + "…").width > room) { cut = cut.slice(0, -1); }
    return cut.length > 1 ? cut + "…" : "";
  }
  function seqSeconds(frames) {
    var seq = S.doc.facts.sequence;
    return frames * seq.num / seq.den;
  }
  // 원본 범위 띠를 그리고 레인을 부른다 — 전체 보기 띠는 레인 보기를 따라 레인 끝에서 그린다.
  function paintStrip() {
    paintSource();
    requestLane();
  }
  // 전체 보기 띠(W-103, D176) — 타임라인 전체를 한 줄에: 클립 채움(원본이 바뀔 때마다 번갈아) ·
  // 컷 눈금 · 고른 클립 · 아래 스토리라인이 보이는 자리(창) · 범위 · 플레이헤드. 글은 없다 —
  // 클립 112개가 화면 폭에 들면 20화소도 안 돼 읽히지 않았다(사용자 화면). 읽고 고치는 곳은
  // 아래 스토리라인이고, 이 띠는 누르거나 끌어 그 자리로 간다.
  function paintNav() {
    var c = $("tl-canvas");
    if (!c || !S.rootEl || !S.doc) { return; }
    var g = canvas2d(c);
    if (!g) { return; }
    var w = c.clientWidth;
    var h = c.clientHeight;
    var total = Math.max(S.total, 1);
    paintNavClips(g, w, h, total);
    var lane = laneCanvas();
    if (lane && lane.clientWidth) {
      var box = LN.navWindow(laneView(), lane.clientWidth, laneTotal(), w);
      g.fillStyle = cssVar("--vl-accent-soft");
      g.globalAlpha = 0.45;
      g.fillRect(box[0], 0, Math.max(2, box[1] - box[0]), h);
      g.globalAlpha = 1;
      g.strokeStyle = cssVar("--vl-accent-ink");
      g.lineWidth = 1.5;
      g.strokeRect(box[0] + 0.75, 0.75, Math.max(2, box[1] - box[0]) - 1.5, h - 1.5);
    }
    var marked = rangeOf();
    if (marked) {
      g.globalAlpha = 0.3;
      g.fillStyle = cssVar("--vl-accent");
      g.fillRect(marked[0] / total * w, 0, (marked[1] - marked[0]) / total * w, h);
      g.globalAlpha = 1;
    }
    paintHead(g, S.playhead / total * w, h);
  }
  function paintNavClips(g, w, h, total) {
    var fills = [cssVar("--vl-clip-fill"), cssVar("--vl-clip-fill-alt")];
    var well = cssVar("--vl-video-well");
    var tone = 0;
    S.spans.forEach(function (span, i) {
      if (i > 0 && span.sourceId !== S.spans[i - 1].sourceId) { tone = 1 - tone; }
      var x = span.outStart / total * w;
      var width = Math.max(1, span.outFrames / total * w);
      g.fillStyle = !mediaOf(span.sourceId) ? cssVar("--vl-warn-soft")
        : i === S.sel ? cssVar("--vl-accent") : fills[tone];
      g.fillRect(x, 0, width, h);
      if (i > 0) { g.fillStyle = well; g.fillRect(Math.round(x), 0, 1, h); }
    });
  }
  // 라벨은 파형 레인의 클립 띠와 같다 — `#3 C_tall · 2.4초`, 좁으면 길이 → 이름 차례로 줄인다.
  function stripLabel(g, span, i, x, width, mid, token) {
    var room = width - 10;
    if (room < 2 * CANVAS_FONT_PX) { return; }
    var head = "#" + (i + 1) + " " + nameOf(span.sourceId).replace(/\.[^.]+$/, "");
    var full = head + " · " + seqSeconds(span.outFrames).toFixed(1) + "초";
    var text = g.measureText(full).width <= room ? full : fitText(g, head, room);
    if (!text) { return; }
    g.fillStyle = cssVar(token);
    g.fillText(text, x + 5, mid);
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
    var g = canvas2d(c);
    if (!g) { return; }
    var w = c.clientWidth;
    var h = c.clientHeight;
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
    paintSourceEnds(g, w, h, source.frames * seconds);
    var at = sourceCursor();
    if (at !== null) { paintHead(g, xOf(at), h); }
  }
  // 원본 띠의 양끝(디자인 점검 U5) — 왼쪽 0:00 · 오른쪽 원본 길이. 등급 띠 위에서도 읽히게 바닥색
  // 받침을 깐다.
  function paintSourceEnds(g, w, h, seconds) {
    g.font = RULER_FONT_PX + "px " + cssVar("--vl-font-mono");
    g.textBaseline = "bottom";
    [[clockText(0), 2], [clockText(seconds), null]].forEach(function (row) {
      var width = g.measureText(row[0]).width;
      var x = row[1] === null ? w - width - 2 : row[1];
      g.fillStyle = cssVar("--vl-surface-sunken");
      g.fillRect(x - 2, h - RULER_FONT_PX - 3, width + 4, RULER_FONT_PX + 3);
      g.fillStyle = cssVar("--vl-ink-muted");
      g.fillText(row[0], x, h - 1);
    });
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
  var CLIP_EDIT_LABELS = { blade: "자르기", "delete": "삭제", join: "합치기" };
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

  // ─── 보기(⌘= · ⌘− · ⇧Z · N — Final Cut): 스토리라인 · 파형 자막 레인의 줌 · 맞춤 · 스내핑 ──
  // 스토리라인과 레인은 한 보기다(W-103, D176 — 위 전체 보기 띠는 그 보기의 자리를 창으로 보인다).
  // 플레이헤드가 보이면 그 자리를 두고 넓힌다.
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
  // 도구(W-103, D176 — Final Cut A · B): 선택은 클립을 고르고 끝을 끈다, 자르기는 스토리라인에서
  // 누른 자리로 클립을 자른다(⌘B와 같은 연산 — 도구는 화면 상태일 뿐 문서·저장에 없다).
  var TOOL_SAY = {
    select: "선택 도구 — 클립을 고르고 끝을 끌어 길이를 고칩니다(B: 자르기 도구).",
    blade: "자르기 도구 — 스토리라인에서 누른 자리로 클립을 자릅니다(A: 선택 도구).",
  };
  function setTool(name) {
    if (!TOOL_SAY[name]) { return false; }
    S.tool = name;
    paintTools();
    say(TOOL_SAY[name], false);
    requestLane();
    return true;
  }
  // 가운데 고정 재생(D178) — 이 브라우저가 기억한다(편의 — 못 읽으면 끔).
  var CENTER_PLAY_KEY = "vlogsrt.centerPlay";
  function centerPlay() {
    if (S.centerPlay === undefined) {
      try { S.centerPlay = W.localStorage.getItem(CENTER_PLAY_KEY) === "on"; }
      catch (error) { S.centerPlay = false; }
    }
    return S.centerPlay;
  }
  function toggleCenterPlay() {
    S.centerPlay = !centerPlay();
    try { W.localStorage.setItem(CENTER_PLAY_KEY, S.centerPlay ? "on" : ""); }
    catch (error) { /* 편의 */ }
    paintTools();
    say(S.centerPlay ? "가운데 고정 — 재생하면 플레이헤드는 가운데에 서고 타임라인이 흐릅니다."
      : "넘겨 보기 — 재생하면 플레이헤드가 화면 끝에 닿을 때 한 화면씩 넘깁니다.", false);
    requestLane();
    return true;
  }
  function paintTools() {
    if (S.rootEl) { S.rootEl.setAttribute("data-tl-tool", S.tool); }
    var center = $("tl-center");
    if (center) { center.setAttribute("aria-pressed", centerPlay() ? "true" : "false"); }
    var skim = $("tl-skim");
    if (skim) { skim.setAttribute("aria-pressed", S.skimOn ? "true" : "false"); }
    var snap = $("tl-snap");
    if (snap) { snap.setAttribute("aria-pressed", S.snapOff ? "false" : "true"); }
    Object.keys(TOOL_SAY).forEach(function (name) {
      var b = $("tl-tool-" + name);
      if (b) { b.setAttribute("aria-pressed", name === S.tool ? "true" : "false"); }
    });
  }
  function toggleSnap() {
    S.snapOff = !S.snapOff;
    if (S.rootEl) { S.rootEl.setAttribute("data-tl-snap", S.snapOff ? "off" : "on"); }
    paintTools();
    say(S.snapOff
      ? "스내핑을 껐습니다 — 자막 가장자리가 컷 경계에 붙지 않습니다(N으로 다시 켭니다)."
      : "스내핑을 켰습니다 — 자막 가장자리가 컷 경계에 붙습니다(⇧를 누르면 그때만 끕니다).", false);
    return true;
  }
  // ⌘E(Final Cut 「공유」) — 최종출력 칸(번인 캡션)으로 갈 뿐 렌더를 걸지 않는다. 거는 것은 단추다.
  function focusShare() {
    var pick = $("tl-burn");
    if (!pick) { return false; }
    pick.focus();
    say("최종출력 — 「영상에 넣을 자막」을 고른 뒤 「최종출력」을 누르세요.", false);
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
  // `at` — 클립 차례(0…클립 수): 영상 목록에서 끌어 놓은 자리(W-111, D185).
  function addSourceAt(id, at) {
    commitText();
    if (!S.doc || !id || S.adding) { return; }
    if (!S.doc.facts.sources.some(function (s) { return s.id === id; })) {
      say("이 타임라인을 만들 때 없던 원본이라 넣지 못합니다(분석 지원 밖이거나 새로 불러온" +
        " 영상) — 「타임라인으로 보내기」로 새 타임라인을 만드세요.", true);
      return;
    }
    if (unclipped().indexOf(id) < 0) {
      say(nameOf(id) + "은(는) 이미 타임라인에 있습니다 — 원본 띠의 손잡이로 늘리세요.", true);
      return;
    }
    if (S.conflict) {
      say("저장이 충돌한 채로는 원본을 넣지 않습니다 — 먼저 정리하세요.", true);
      return;
    }
    if (S.dirty || S.saving) {
      say("편집을 저장한 뒤 넣습니다…", false);
      afterSaved(function () { addSourceAt(id, at); });
      return;
    }
    if (S.playing) { stopPlayback(); }
    S.adding = true;
    var payload = { signature: S.signature, token: S.token, timelineId: S.timelineId,
      baseRevision: S.baseRevision, doc: docPayload(), sourceId: id, at: at };
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
  // ─── 끌어 놓기(W-111, D185): 영상 목록의 카드를 레인에 놓은 자리에 넣는다 ───
  // 놓을 자리는 커서에서 가장 가까운 클립 경계(맨 앞 · 클립 사이 · 끝)다 — 같은 원본의 클립 사이는
  // 가를 수 없어 건너뛴다(D125). 끄는 동안 레인은 타임라인 전체를 보이고(줌 한도 무시 — 어느
  // 자리든 놓을 수 있게) 그 경계에 선을 긋는다. 놓거나 떠나면 보던 줌으로 돌아간다.
  var DROP_TYPE = "application/x-vlogsrt-source";
  function carriesSource(ev) {
    var types = ev.dataTransfer ? ev.dataTransfer.types : null;
    if (!types) { return false; }
    for (var i = 0; i < types.length; i += 1) { if (types[i] === DROP_TYPE) { return true; } }
    return false;
  }
  function slotTicks(slot) {
    var spans = S.spans;
    var frame = slot < spans.length ? spans[slot].outStart
      : spans[slot - 1].outStart + spans[slot - 1].outFrames;
    return frame * seqTicks();
  }
  function dropSlot(ev) {
    var spans = S.spans;
    var ticks = LN.ticksAt(laneView(), laneX(ev));
    var best = spans.length;
    var gap = Infinity;
    for (var i = 0; i <= spans.length; i += 1) {
      if (i > 0 && i < spans.length && spans[i - 1].sourceId === spans[i].sourceId) { continue; }
      var d = Math.abs(slotTicks(i) - ticks);
      if (d < gap) { gap = d; best = i; }
    }
    return best;
  }
  function slotWords(slot) {
    var spans = S.spans;
    if (!spans.length || slot >= spans.length) { return "타임라인 끝에"; }
    if (slot === 0) { return "맨 앞(" + nameOf(spans[0].sourceId) + " 앞)에"; }
    return nameOf(spans[slot - 1].sourceId) + "와(과) " + nameOf(spans[slot].sourceId) + " 사이에";
  }
  function onDropOver(ev) {
    if (!S.doc || !carriesSource(ev)) { return; }
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
    if (S.dropAt === null && !S.dropView) {
      var lane = laneCanvas();
      S.dropView = laneView();
      if (lane) { S.lane = { start: 0, tpp: laneTotal() / Math.max(1, lane.clientWidth) }; }
    }
    var slot = S.spans.length ? dropSlot(ev) : 0;
    if (S.dropAt !== slot) {
      S.dropAt = slot;
      say("놓으면 이 원본 전체를 " + slotWords(slot) + " 넣습니다.", false);
      requestLane();
    }
  }
  function onDropLeave() {
    if (S.dropAt === null && !S.dropView) { return; }
    S.dropAt = null;
    if (S.dropView) { S.lane = S.dropView; S.dropView = null; }
    requestLane();
  }
  function onDrop(ev) {
    if (!S.doc || !carriesSource(ev)) { return; }
    ev.preventDefault();
    var slot = S.spans.length ? dropSlot(ev) : 0;
    onDropLeave();
    addSourceAt(ev.dataTransfer.getData(DROP_TYPE), slot);
  }
  function paintDropMark(ctx, view, width, height) {
    if (S.dropAt === null || !S.spans.length) { return; }
    var x = Math.min(Math.max(LN.x(view, slotTicks(S.dropAt)), 2), width - 2);
    ctx.fillStyle = cssVar("--vl-accent");
    ctx.fillRect(x - 2, 0, 4, height);
    ctx.font = CANVAS_FONT_PX + "px " + cssVar("--vl-font-sans");
    ctx.textBaseline = "top";
    var label = "여기에 넣기";
    var w = ctx.measureText(label).width;
    ctx.fillText(label, x + 6 + w > width ? x - 6 - w : x + 6, RULER_PX + 4);
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
    say(nameOf(id) + " 원본 전체를 넣었습니다 · 리비전 " +
      got.revision + " · 새 자막 " + got.cues +
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
  // 고른 자막의 글 칸으로(D180) — 입력칸을 지금 고름으로 먼저 그린 뒤 초점을 둔다.
  function editPickedText() {
    paintCueEditor();
    focusCueText();
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
  // 한 화면 맞춤(W-102, D175) — 덱이 맞춤이고 넓은 화면이면 재생기를 두 열(`is-fit`)로 두고
  // 덱의 남은 높이를 준다(덱은 스크롤하지 않고 자막 목록 하나만 스크롤한다). 인덱스를 열었거나
  // 자막 목록이 덱 밖(「자막」 작업공간)이면 예전 모양이다.
  function fitDeck() {
    var root = S.rootEl;
    if (!root || !root.closest) { return; }
    var deck = root.closest("#vl-timeline-deck");
    // 자막 목록 · 입력칸이 재생기 밖(인스펙터 · 브라우저 열, D178)이면 한 열 — 타임라인이
    // 덱 폭 전체다.
    var solo = !root.querySelector(".tl-cues-body");
    var index = $("tl-index");
    var wide = W.matchMedia ? W.matchMedia("(min-width: 1051px)").matches : true;
    var on = !!deck && !!deck.closest("[data-vl-fit]") && wide &&
      getComputedStyle(deck).overflowY !== "visible" && !(index && !index.hidden);
    root.classList.toggle("is-solo", solo);
    // 재어 보느라 `is-fit`을 달았다 떼면 덱 내용이 잠깐 줄어 브라우저가 scrollTop을 0으로 당긴다 —
    // 잰 뒤 물러서면 사람이 내려 둔 자리로 돌린다(디자인 점검 U1 — 1초마다 맨 위로 튀었다).
    var kept = deck ? deck.scrollTop : 0;
    if (!on) {
      root.classList.remove("is-fit");
      root.style.height = "";
      if (deck) { deck.scrollTop = kept; }
      fitFloors(deck);
      return;
    }
    root.classList.add("is-fit");
    // 덱 안에서 재생기 위에 놓인 것(요약 · 도구)과 아래 여백을 뺀 높이가 재생기 몫이다.
    var above = root.getBoundingClientRect().top - deck.getBoundingClientRect().top +
      deck.scrollTop;
    var pad = parseFloat(getComputedStyle(deck).paddingBottom) || 0;
    root.style.height = Math.floor(deck.clientHeight - above - pad) + "px";
    // 고정 줄을 빼고 파형 레인이 최소 높이를 못 받으면(위 덱이 원본 타임라인을 펼친 때) 예전처럼
    // 쌓고 덱이 스크롤한다 — 레인을 0으로 잘라 숨기지 않는다. 필름 띠(W-103, D176)는 레인 위쪽에
    // 더해졌으므로 그 높이만큼 더 받아야 파형 · 자막 몫이 예전 최소와 같다.
    var lane = $("tl-lane");
    if (!lane || lane.clientHeight < S.settings.fitLaneMinPx + S.settings.laneFilmPx) {
      root.classList.remove("is-fit");
      root.style.height = "";
      deck.scrollTop = kept;
    }
    // 맞춤을 정한 뒤에 잰다 — 쌓인 모양(머리 · 여백이 다르다)에서 재면 바닥이 부푼다.
    fitFloors(deck);
    if (lane) { requestLane(); }
  }
  S.fitDeck = fitDeck;
  // 맞춤의 바닥(2026-11-24 사용자 지시 「16:10 맥북에서도 UI가 다 보이게」 — 14인치 크롬 창에서
  // 스토리라인 아래가 덱 안 스크롤로 잘리고 번역 모드 편집 칸의 단추가 인스펙터에 숨었다). 위·아래
  // 덱 줄은 이 아래로 줄지 않고 영상 · 자막 목록이 먼저 준다. 늘고 주는 칸(파형 레인 · 자막 목록)만
  // 최소로 바꿔 잴 뿐이라 바닥이 제 결과로 다시 자라지 않는다. 합이 화면보다 크면 무대가 길어진다
  // (숨기지 않고 문서가 스크롤한다). 값이 바뀔 때만 단다.
  function fitFloors(deck) {
    var host = deck && deck.closest ? deck.closest("[id=visual-stage]") : null;
    if (!host) { return; }
    function put(name, px) {
      var value = px > 0 ? Math.ceil(px) + "px" : "";
      if (host.style.getPropertyValue(name) !== value) {
        if (value) { host.style.setProperty(name, value); }
        else { host.style.removeProperty(name); }
      }
    }
    var wide = W.matchMedia ? W.matchMedia("(min-width: 1051px)").matches : true;
    var lane = $("tl-lane");
    var deckFloor = 0;
    var upperFloor = 0;
    if (host.hasAttribute("data-vl-fit") && wide && lane && lane.offsetParent) {
      // 「자막」 작업공간의 레인은 제 최소 높이(토큰)가 더 크다.
      var laneMin = Math.max(S.settings.fitLaneMinPx + S.settings.laneFilmPx,
        parseFloat(getComputedStyle(lane).minHeight) || 0);
      // 쌓인 모양(인덱스를 열었을 때 등)이면 레인이 줄지 않으니 덱 내용 전체가 바닥이다.
      deckFloor = !S.rootEl.classList.contains("is-fit") ? deck.scrollHeight :
        deck.scrollHeight - lane.offsetHeight + laneMin;
      // 자막 칸이 인스펙터에 있을 때(「기본」 · 타임라인 뷰어, D178)만 — 편집 칸과 단추를 지킨다.
      var list = $("tl-cue-list");
      var inspector = list && list.closest ? list.closest("[data-vl-inspector]") : null;
      if (inspector && list.offsetParent) {
        upperFloor = inspector.scrollHeight - list.offsetHeight +
          (parseFloat(getComputedStyle(list).minHeight) || 0);
      }
    }
    var need = 0;
    if (deckFloor > 0) {
      // 무대 줄: 위 덱 · 원본 레일(auto) · 아래 덱 · 상태 줄 — 위 덱 바닥은 덱 최소 높이 토큰 이상.
      var cs = getComputedStyle(host);
      var rows = cs.gridTemplateRows.split(" ").map(parseFloat);
      var gap = parseFloat(cs.rowGap) || 0;
      var fixed = (rows[1] || 0) + deckFloor + (rows[3] || 0) + gap * (rows.length - 1);
      var deckMin = parseFloat(cs.getPropertyValue("--vl-deck-min")) || 0;
      // 화면이 둘 다 담지 못하면(13인치 크롬 창 1440×780) 타임라인을 지키고 위 덱 바닥을 남는
      // 높이로 줄인다 — 인스펙터가 제 안에서 스크롤한다(문서는 스크롤하지 않는다).
      var screen = Math.max(parseFloat(cs.getPropertyValue("--vl-stage-min-h")) || 0,
        W.innerHeight - (parseFloat(host.style.getPropertyValue("--vl-stage-top")) || 0));
      upperFloor = Math.min(upperFloor, Math.max(screen - fixed, deckMin));
      need = Math.max(upperFloor, deckMin) + fixed;
    }
    put("--vl-deck-floor", deckFloor);
    put("--vl-upper-floor", upperFloor);
    put("--vl-fit-need", need);
  }
  function paintCues() {
    if (!S.doc) { return; }
    paintCueList();
    paintCueEditor();
  }
  function pickedKeyOf() {
    return S.picked.join("\n") + (S.translation ? "\u0001" + englishKey() : "");
  }
  function paintCueList() {
    var list = $("tl-cue-list");
    var count = $("tl-cue-count");
    if (!list) { return; }
    if (count) {
      count.textContent = "보이는 " + S.proj.cues.length + "개" + (S.proj.hidden.length
        ? " · 컷에 가려진 " + S.proj.hidden.length + "개(클립을 늘리면 돌아온다)" : "");
    }
    var pickedKey = pickedKeyOf();
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
  // 「이 줄 영어 번역」(W-112, D186) — 고른 줄 하나를 Gemini로 옮겨(유료 · 누름이 승인) 고친
  // 영어(D166)로 넣는다. 앞의 보이는 줄들을 맥락으로 싣는다. 받는 사이 다른 줄로 가도 그 줄에
  // 넣는다.
  function askLineEnglish(id) {
    commitText();
    var cue = id ? projectedCue(id) : null;
    if (!cue || !cue[4] || !cue[4].trim()) { say("옮길 한국어가 없습니다.", true); return; }
    if (S.lineAsking) { say("한 줄을 옮기는 중입니다…", false); return; }
    var at = S.proj.cues.map(function (row) { return row[0]; }).indexOf(id);
    var context = S.proj.cues.slice(0, Math.max(at, 0)).map(function (row) { return row[4]; });
    S.lineAsking = id;
    say("이 줄을 영어로 옮기는 중(Gemini, 유료)…", false);
    var asked = S.timelineId;
    S.server.tl_translate_line({ signature: S.signature, token: S.token, text: cue[4],
      context: context }).catch(function () {
      return { ok: false, error: "서버가 답하지 않았습니다." };
    }).then(function (got) {
      S.lineAsking = null;
      if (asked !== S.timelineId || !S.doc) { return; }
      if (!got || !got.ok) {
        say("이 줄을 옮기지 못했습니다 — " + ((got && got.error) || "까닭을 받지 못했습니다."),
          true);
        return;
      }
      if (!projectedCue(id)) { say("옮기는 사이 그 자막이 사라졌습니다.", true); return; }
      var next;
      try {
        next = TL.cueSetEnglish(S.doc, id, got.english, S.settings);
      } catch (error) {
        if (!TL.isError(error)) { throw error; }
        say("영어 넣기 — 하지 못했습니다: " + error.message, true);
        return;
      }
      change(next, "영어 번역 " + id);
      paintCueEditor(true);
    });
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
  function toggleTranslation() { setTranslation(!S.translation); }
  function setTranslation(on) {
    S.translation = on;
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
      followCurrent(list, current);
      Array.prototype.forEach.call(list.querySelectorAll(".tl-cue-row"), function (row) {
        var id = row.getAttribute("data-cue");
        var on = id === current;
        row.classList.toggle("is-current", on);
        row.classList.toggle("is-selected", isPicked(id));
        if (on && (S.playing || id === S.cue)) { keepInList(list, row); }
      });
    }
    paintCueEditor();
  }
  // 고름이 플레이헤드를 따른다(D179 — Subtitle Edit): 플레이헤드 아래 자막이 바뀌면 그 줄을 고르고
  // 입력칸도 그 줄로 바꾼다. 쓰는 중(글 칸에 초점)이거나 여러 줄을 골랐거나, 막 고른 줄 위에
  // 플레이헤드가 아직 있으면(목록에서 누른 줄은 그 시작으로 간다) 그대로 둔다. 목록은 다시 짓지
  // 않고 표지만 옮긴다(재생 중 스크롤이 튀지 않게).
  function followCurrent(list, current) {
    if (!current || current === S.cue || S.picked.length > 1 || editingText()) { return; }
    if (!S.playing && S.cue && pickedUnderPlayhead()) { return; }
    S.cue = current;
    S.picked = [current];
    S.anchor = current;
    list.__tlCue = S.cue;
    list.__tlPicked = pickedKeyOf();
    requestLane();
  }
  function pickedUnderPlayhead() {
    var cue = projectedCue(S.cue);
    if (!cue) { return false; }
    var a = S.playhead * seqTicks();
    var b = a + seqTicks();
    return cue[2] === cue[3] ? a <= cue[2] && cue[2] <= b : cue[2] < b && cue[3] > a;
  }
  function editingText() {
    var active = document.activeElement;
    return !!active && (active === $("tl-cue-text") || active === $("tl-cue-en-text"));
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
    var typing = !!area && editingText();
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
    // 길이 · 문자/초는 머리 줄 옆(2026-11-24 — 16:10 화면): 시각 줄이 시작 · 끝만이라 좁은
    // 인스펙터(약 400px)에서도 한 줄이다(예전엔 두 줄로 접혔다).
    return '<div class="tl-cue-headline"><strong id="tl-cue-head"></strong>' +
      '<span id="tl-cue-dur" class="tl-cue-num"></span>' +
      '<span id="tl-cue-stats" class="tl-cue-num"></span></div>' +
      '<div class="tl-cue-times"><label>시작 <input id="tl-cue-start" class="tl-tc"' +
      ' data-side="start" data-cue="' + esc(id) + '" spellcheck="false"' +
      ' aria-label="자막 시작 — 시:분:초:프레임"></label>' +
      '<label>끝 <input id="tl-cue-end" class="tl-tc" data-side="end" data-cue="' + esc(id) +
      '" spellcheck="false" aria-label="자막 끝 — 시:분:초:프레임"></label></div>' +
      '<span id="tl-cue-meta" class="tl-cue-muted"></span>' +
      '<span id="tl-cue-warn" class="tl-cue-warn"></span>' +
      '<textarea id="tl-cue-text" data-cue="' + esc(id) + '" spellcheck="false"' +
      ' aria-label="자막 글 — ⌘Enter·Esc로 확정"></textarea>' +
      '<div id="tl-cue-en-edit" class="tl-cue-en-edit" hidden>' +
      '<textarea id="tl-cue-en-text" data-cue="' + esc(id) + '" spellcheck="true" lang="en"' +
      ' aria-label="영어 — 고치면 이 줄의 영어로 씁니다. ⌘Enter·Esc로 확정"' +
      ' placeholder="영어"></textarea>' +
      '<span id="tl-cue-en" class="tl-cue-en-note"></span>' +
      '<div class="tl-cue-buttons">' + cueButton("cue-en-ask", "이 줄 영어 번역",
        "이 줄의 한국어를 Gemini로 옮겨 영어 칸에 넣는다(유료 · 누름이 승인) — 앞 줄들은 맥락") +
      // 그 옆 「전체 자막 영어 번역」(2026-11-24 사용자 지시)은 머리의 「영어 만들기」와 같다.
      cueButton("translate", "전체 자막 영어 번역",
        "저장한 자막 전체를 Gemini로 옮긴다(유료 · 누름이 승인) — 「영어 만들기」와 같고," +
        " 같은 글은 다시 사지 않는다") +
      "</div></div>" +
      // 한 줄(2026-11-24 사용자 지시 — 인스펙터 약 400px에서 두 줄로 접혔다): 이름은 짧게,
      // 뜻은 title.
      '<div class="tl-cue-buttons">' +
      cueButton("cue-start", "시작 [", "시작을 여기로 [ — 자막 시작을 플레이헤드로") +
      cueButton("cue-end", "끝 ]", "끝을 여기로 ] — 자막 끝을 플레이헤드로") +
      cueButton("cue-split", "나누기 ⇧B", "플레이헤드에서 자막 나누기") +
      cueButton("cue-merge", "합치기 ⇧M", "다음과 합치기 ⇧M — 다음 보이는 자막과(같은 원본만)") +
      cueButton("cue-delete", "지우기", "자막 지우기 — 이 자막을 지운다(고른 자막이면 ⌫)") +
      "</div>";
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
  function toggleIndex() { indexState().open = !indexState().open; paintIndex(); fitDeck(); }
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
    // 재생 중 보기 — 「가운데 고정」이면 플레이헤드를 가운데에 두고 타임라인이 흐른다(D178),
    // 아니면 플레이헤드가 화면을 벗어날 때만 한 화면씩 넘긴다.
    if (S.playing) {
      S.lane = centerPlay() ? LN.centerOn(laneView(), head, width)
        : LN.follow(laneView(), head, width);
    }
    var view = laneView();
    var at = laneLayout(height);
    ctx.fillStyle = cssVar("--vl-surface-sunken");
    ctx.fillRect(0, 0, width, height);
    paintRuler(ctx, width, 0, view.start / laneRate(), laneRate() / view.tpp);
    paintLaneCaptions(ctx, view, width, at);
    ctx.save();
    ctx.translate(0, at.bandTop);
    paintLaneClips(ctx, view, width);
    ctx.restore();
    paintSkimmer(ctx, height);
    paintDropMark(ctx, view, width, height);
    paintHead(ctx, LN.x(view, head), height);
    paintZoomSlider(view);
    paintNav();
  }
  // 레인의 띠(D177 — Final Cut): 맨 위 시간 눈금 · 자막 칸(남은 높이, 연결 클립 자리) · 맨 아래
  // 스토리라인(이름 · 필름 · 붙은 소리 파형). 누름 판정도 이 자리로 가른다(`laneZone`).
  function laneLayout(height) {
    var band = bandPx();
    var bandTop = Math.max(RULER_PX, height - band);
    return { capTop: RULER_PX, capH: Math.max(0, bandTop - RULER_PX), bandTop: bandTop };
  }
  function paintLaneCaptions(ctx, view, width, at) {
    if (at.capH <= 0) { return; }
    ctx.save();
    ctx.translate(0, at.capTop);
    ctx.beginPath();
    ctx.rect(0, 0, width, at.capH);
    ctx.clip();
    paintLaneCues(ctx, view, at.capH);
    if (S.laneSel) {
      ctx.fillStyle = cssVar("--vl-accent-soft");
      var a = LN.x(view, S.laneSel[0]);
      ctx.fillRect(a, 0, LN.x(view, S.laneSel[1]) - a, at.capH);
    }
    ctx.restore();
  }
  // 스토리라인(W-100 D169 → W-103 D176) — Final Cut 스토리라인처럼 클립마다 둥근 카드: 위는 이름
  // 줄(`#5 IMG_9001 · 8.7초`, 원본이 바뀔 때마다 두 채움을 번갈아), 아래는 필름 띠(미리보기
  // 장면).
  // 카드 사이 틈이 컷이다. 고른 클립은 노란 2px 테두리, 잡을 끝(올린 · 끄는 끝)은 노란 손잡이다
  // (Final Cut 고름 · 트림). 자르기 도구면 올린 자리에 자를 선을 긋는다.
  var CARD_RADIUS = 4;
  function bandPx() {
    return S.settings.laneClipPx + S.settings.laneFilmPx + S.settings.laneWavePx;
  }
  function paintLaneClips(ctx, view, width) {
    var seq = seqTicks();
    var fills = [cssVar("--vl-clip-fill"), cssVar("--vl-clip-fill-alt")];
    var tone = 0;
    var want = [];
    ctx.font = CANVAS_FONT_PX + "px " + cssVar("--vl-font-sans");
    ctx.textBaseline = "middle";
    S.spans.forEach(function (span, i) {
      if (i > 0 && span.sourceId !== S.spans[i - 1].sourceId) { tone = 1 - tone; }
      var xs = LN.x(view, span.outStart * seq);
      var xe = LN.x(view, (span.outStart + span.outFrames) * seq);
      if (xe < 0 || xs > width) { return; }
      ctx.save();
      cardPath(ctx, xs, xe);
      ctx.clip();
      paintStoryCard(ctx, span, i, xs, xe, fills[tone]);
      paintFilm(ctx, view, span, xs, xe, width, want);
      paintStoryAudio(ctx, view, xs, xe, width, fills[tone]);
      ctx.restore();
      paintStoryFrame(ctx, i, xs, xe);
    });
    filmWant(want);
    paintBladeLine(ctx);
    paintTrimTip(ctx, view);
    // 끝 표는 모든 카드 뒤에 — 옆 카드가 덮지 않게.
    var tip = S.edgeTip;
    S.edgeTip = null;
    if (tip) { paintEdgeTip(ctx, tip.index, tip.side, tip.x); }
  }
  function cardPath(ctx, xs, xe) {
    var x = xs + 1;
    var w = Math.max(1, xe - xs - 2);
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(x, 0, w, bandPx(), Math.min(CARD_RADIUS, w / 2)); }
    else { ctx.rect(x, 0, w, bandPx()); }
  }
  function paintStoryCard(ctx, span, i, xs, xe, fill) {
    var head = S.settings.laneClipPx;
    var x = xs + 1;
    var w = Math.max(1, xe - xs - 2);
    var ready = !!mediaOf(span.sourceId);
    ctx.fillStyle = cssVar("--vl-video-well");
    ctx.fillRect(x, 0, w, bandPx());
    ctx.fillStyle = ready ? fill : cssVar("--vl-warn-soft");
    ctx.fillRect(x, 0, w, head);
    stripLabel(ctx, span, i, x, w, head / 2, "--vl-ink");
  }
  // 클립에 붙은 소리(D177 — Final Cut): 필름 아래 줄에 클립 채움보다 짙은 바탕과 밝은 파형.
  function paintStoryAudio(ctx, view, xs, xe, width, fill) {
    var top = S.settings.laneClipPx + S.settings.laneFilmPx;
    var h = S.settings.laneWavePx;
    var x = Math.max(0, xs + 1);
    var right = Math.min(width, xe - 1);
    if (right <= x) { return; }
    ctx.fillStyle = fill;
    ctx.fillRect(x, top, right - x, h);
    ctx.fillStyle = cssVar("--vl-video-well");
    ctx.globalAlpha = 0.35;
    ctx.fillRect(x, top, right - x, h);
    ctx.globalAlpha = 1;
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, top, right - x, h);
    ctx.clip();
    ctx.translate(0, top);
    paintLaneWave(ctx, view, Math.floor(x), Math.ceil(right), h, "--vl-ink");
    ctx.restore();
  }
  // 카드 테두리와 잡을 끝 — 고른 클립은 노란 2px 테두리, 올린 · 끄는 끝은 노란 4화소 손잡이.
  function paintStoryFrame(ctx, i, xs, xe) {
    var chosen = i === S.sel;
    cardPath(ctx, xs, xe);
    ctx.strokeStyle = cssVar(chosen ? "--vl-tier-yellow-ink" : "--vl-line-strong");
    ctx.lineWidth = chosen ? 2 : 1;
    ctx.stroke();
    var drag = S.clipDrag;
    var hover = S.hoverBand;
    var lit = drag ? (drag.index === i ? drag.side : null)
      : hover && hover.index === i && S.tool !== "blade" && hover.part !== "body"
        ? (hover.part === "start" ? TL.SIDE_START : TL.SIDE_END) : null;
    if (lit === null) { return; }
    // 더 늘릴 원본이 없는 끝은 빨강(Final Cut — 미디어 끝) · 늘릴 수 있으면 노랑.
    var doc = drag ? drag.base : S.doc;
    ctx.fillStyle = cssVar(canExtend(doc, i, lit) ? "--vl-tier-yellow-ink" : "--vl-danger");
    ctx.fillRect(lit === TL.SIDE_START ? xs + 1 : xe - 5, 0, 4, bandPx());
    if (!drag) { S.edgeTip = { index: i, side: lit, x: lit === TL.SIDE_START ? xs : xe }; }
  }
  // 그 끝 바깥으로 한 프레임이라도 갈 수 있는가(원본 처음 · 끝 · 같은 원본의 옆 클립 —
  // `TL.clampEdge`).
  function canExtend(doc, i, side) {
    var clip = doc.clips[i];
    var edge = side === TL.SIDE_START ? clip[1] : clip[2];
    var outward = side === TL.SIDE_START ? edge - 1 : edge + 1;
    return TL.clampEdge(doc, i, side, outward, S.settings) !== edge;
  }
  // 올린 끝 옆 — 원본의 어디서 잘렸는지와 더 늘릴 수 있는지(D177).
  function paintEdgeTip(ctx, i, side, x) {
    var clip = S.doc.clips[i];
    var edge = side === TL.SIDE_START ? clip[1] : clip[2];
    var text = "원본 " + (edge * frameSeconds(clip[0])).toFixed(2) + "초에서 잘림 · " +
      (canExtend(S.doc, i, side) ? "끌어서 늘리기 · 줄이기" : "더 늘릴 원본 없음");
    var w = ctx.measureText(text).width + 10;
    var left = side === TL.SIDE_START ? x + 6 : x - w - 6;
    var top = S.settings.laneClipPx + 4;
    ctx.fillStyle = cssVar("--vl-surface-raised");
    ctx.fillRect(left, top, w, 18);
    ctx.strokeStyle = cssVar("--vl-line-strong");
    ctx.lineWidth = 1;
    ctx.strokeRect(left + 0.5, top + 0.5, w - 1, 17);
    ctx.fillStyle = cssVar("--vl-ink");
    ctx.fillText(text, left + 5, top + 9);
  }
  function paintBladeLine(ctx) {
    var hover = S.hoverBand;
    if (S.tool !== "blade" || !hover || S.clipDrag) { return; }
    var x = Math.round(hover.x) + 0.5;
    ctx.save();
    ctx.strokeStyle = cssVar("--vl-ink");
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, bandPx()); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = cssVar("--vl-ink");
    ctx.fillText("✂", x + 3, S.settings.laneClipPx / 2);
  }
  // 끄는 동안 끝 옆에 얼마나 옮겼는지 · 새 길이(놓으면 기록 한 칸).
  function paintTrimTip(ctx, view) {
    var drag = S.clipDrag;
    if (!drag || drag.frame === null) { return; }
    var span = S.spans[drag.index];
    if (!span) { return; }
    var seq = seqTicks();
    var edge = drag.side === TL.SIDE_START ? span.outStart : span.outStart + span.outFrames;
    var delta = drag.frame - drag.edge;
    var text = (delta > 0 ? "+" : "") + delta + "프레임 · " +
      seqSeconds(span.outFrames).toFixed(2) + "초";
    var x = LN.x(view, edge * seq);
    var w = ctx.measureText(text).width + 10;
    var left = drag.side === TL.SIDE_START ? x + 6 : x - w - 6;
    var top = S.settings.laneClipPx + 4;
    ctx.fillStyle = cssVar("--vl-surface-raised");
    ctx.fillRect(left, top, w, 18);
    ctx.strokeStyle = cssVar("--vl-tier-yellow-ink");
    ctx.lineWidth = 1;
    ctx.strokeRect(left + 0.5, top + 0.5, w - 1, 17);
    ctx.fillStyle = cssVar("--vl-ink");
    ctx.fillText(text, left + 5, top + 9);
  }
  // 스키머(W-103, D176 — Final Cut S): 마우스를 올린 자리의 선(casing 두른 2px). 뷰어도 그 프레임을
  // 보이고, 내리면 플레이헤드 프레임으로 돌아간다. 재생 · 끌기 중에는 쉰다.
  function paintSkimmer(ctx, height) {
    if (!S.skimOn || S.skimX === null || S.skimX === undefined || S.playing) { return; }
    var x = Math.round(S.skimX);
    ctx.fillStyle = cssVar("--vl-playhead-casing");
    ctx.fillRect(x - 2, 0, 4, height);
    ctx.fillStyle = cssVar("--vl-skimmer");
    ctx.fillRect(x - 1, 0, 2, height);
  }
  // 확대 막대(Final Cut 타임라인 오른쪽 위) — 1초의 화소 폭을 로그 눈금 0–1000에.
  var ZOOM_SLIDER_MAX = 1000;
  function zoomSliderValue(view) {
    var lo = Math.log(S.settings.zoomMinPxPerS);
    var hi = Math.log(S.settings.zoomMaxPxPerS);
    var px = laneRate() / view.tpp;
    return Math.round((Math.log(px) - lo) / Math.max(hi - lo, 1e-9) * ZOOM_SLIDER_MAX);
  }
  function paintZoomSlider(view) {
    var slider = $("tl-zoom");
    if (slider && document.activeElement !== slider) { slider.value = zoomSliderValue(view); }
  }
  function onZoomSlider(ev) {
    if (!S.doc) { return; }
    var lo = Math.log(S.settings.zoomMinPxPerS);
    var hi = Math.log(S.settings.zoomMaxPxPerS);
    var px = Math.exp(lo + Number(ev.target.value) / ZOOM_SLIDER_MAX * (hi - lo));
    zoomLane(laneView().tpp / (laneRate() / px));
  }
  // ─── 필름 띠(W-103, D176): 미리보기에서 칸마다 장면 하나 ──────────────
  // 덱 둘과 따로인 영상 하나(문서에 붙이지 않는다 — 덱 · timeupdate · 타이머 임대가 모른다)가
  // 한 번에 한 장면씩 시크해 작은 캔버스로 옮긴다. 열쇠는 원본 + 장면 간격으로 반올림한 미리보기
  // 초라 줌 · 스크롤이 같은 장면을 다시 쓰고, 그릴 때마다 보이는 칸만 줄에 다시 세워 지난 화면
  // 요청은 버린다.
  // 그림일 뿐 문서 · 저장 · 시각에 닿지 않는다(규칙 5). 미리보기가 없거나 못 읽은 원본은 빈 칸이다.
  var FILM_STEPS = [0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128];
  var FILM_STALL_MS = 4000;
  function film() {
    if (!S.film) {
      S.film = { cache: new Map(), queue: [], busy: null, failed: {}, aspect: {}, video: null };
    }
    return S.film;
  }
  function filmVideo() {
    var F = film();
    if (!F.video) {
      var v = document.createElement("video");
      v.muted = true;
      v.preload = "auto";
      v.playsInline = true;
      v.addEventListener("seeked", filmGrabbed);
      v.addEventListener("loadedmetadata", filmSeek);
      v.addEventListener("error", filmFailed);
      F.video = v;
    }
    return F.video;
  }
  function filmWant(list) {
    var F = film();
    F.queue = list.filter(function (job) {
      return !F.cache.has(job.key) && !F.failed[job.sourceId] &&
        !(F.busy && F.busy.key === job.key);
    });
    if (F.busy && Date.now() - F.busy.at > FILM_STALL_MS) { F.busy = null; }
    filmPump();
  }
  function filmPump() {
    var F = film();
    if (F.busy || !F.queue.length) { return; }
    var job = F.queue.shift();
    var media = mediaOf(job.sourceId);
    if (!media) { filmPump(); return; }
    job.at = Date.now();
    F.busy = job;
    var v = filmVideo();
    var src = "/media/" + encodeURIComponent(media.fileId);
    if (v.getAttribute("src") !== src) { v.setAttribute("src", src); return; }
    filmSeek();
  }
  function filmSeek() {
    var F = film();
    var v = F.video;
    if (!F.busy || !v || v.readyState < 1) { return; }
    if (Math.abs(v.currentTime - F.busy.t) < 1e-3 && v.readyState >= 2) { filmGrabbed(); return; }
    v.currentTime = F.busy.t;
  }
  function filmGrabbed() {
    var F = film();
    var job = F.busy;
    var v = F.video;
    if (!job || !v || !v.videoWidth) { return; }
    var ratio = W.devicePixelRatio || 1;
    var h = Math.round(S.settings.laneFilmPx * ratio);
    var c = document.createElement("canvas");
    c.height = h;
    c.width = Math.max(1, Math.round(h * v.videoWidth / v.videoHeight));
    c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
    F.aspect[job.sourceId] = v.videoWidth / v.videoHeight;
    F.cache.set(job.key, c);
    while (F.cache.size > S.settings.filmCacheTiles) {
      F.cache.delete(F.cache.keys().next().value);
    }
    F.busy = null;
    requestLane();
    filmPump();
  }
  function filmFailed() {
    var F = film();
    if (F.busy) { F.failed[F.busy.sourceId] = true; }
    F.busy = null;
    filmPump();
  }
  function filmAspect(sourceId) {
    var seq = S.doc.facts.sequence;
    return film().aspect[sourceId] || (seq.width && seq.height ? seq.width / seq.height : 16 / 9);
  }
  // 클립 하나의 필름 칸 — 칸 가운데 프레임의 미리보기 장면(있으면 그리고, 없으면 줄에 세운다).
  function paintFilm(ctx, view, span, xs, xe, width, want) {
    var id = span.sourceId;
    if (!mediaOf(id) || film().failed[id]) { return; }
    var top = S.settings.laneClipPx;
    var h = S.settings.laneFilmPx;
    var tileW = Math.max(h / 2, Math.round(h * filmAspect(id)));
    var bucket = LN.filmBucket(tileW * view.tpp / laneRate(), FILM_STEPS);
    var seq = seqTicks();
    ctx.save();
    ctx.beginPath();
    ctx.rect(xs + 2, top, Math.max(0, xe - xs - 4), h - 1);
    ctx.clip();
    LN.filmTiles(xs, xe, tileW, width).forEach(function (tile) {
      var frame = clamp(Math.floor(LN.ticksAt(view, tile.mid) / seq), span.outStart,
        span.outStart + span.outFrames - 1);
      var at = TL.sourcePoint(S.doc, frame);
      var t = Math.round(timeOf(id, at[1]) / bucket) * bucket;
      var key = LN.filmKey(id, t, bucket);
      var picture = film().cache.get(key);
      if (picture) { ctx.drawImage(picture, tile.x, top, tileW, h); }
      else { want.push({ key: key, sourceId: id, t: t }); }
    });
    ctx.restore();
  }
  // [from, to) 화소만 그린다 — 스토리라인은 클립 카드마다 제 폭만 부른다(D177).
  function paintLaneWave(ctx, view, from, to, height, token) {
    var unit = TL.msTicks(S.doc.facts);
    var mid = height / 2;
    ctx.fillStyle = cssVar(token);
    TL.tickSpans(S.doc).forEach(function (row) {
      var x0 = Math.max(from, Math.floor(LN.x(view, row[3])));
      var x1 = Math.min(to, Math.ceil(LN.x(view, row[4])));
      if (x1 <= x0) { return; }
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
      // 끝 가장자리는 노랑 ink — 빨강은 오류 몫이다(디자인 점검 U4).
      ctx.fillStyle = cssVar("--vl-tier-yellow-ink");
      ctx.fillRect(xe - 2, 2, 2, height - 4);
      if (xe - xs > 40) {
        ctx.fillStyle = cssVar("--vl-ink");
        ctx.font = CANVAS_FONT_PX + "px " + cssVar("--vl-font-sans");
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
    skimAt(null);
    commitText();
    var view = laneView();
    var x = laneX(ev);
    var ticks = clamp(LN.ticksAt(view, x), 0, laneTotal());
    if (S.rootEl) { S.rootEl.focus({ preventScroll: true }); }
    var zone = laneZone(ev);
    if (zone === "ruler") { rulerDown(ev, ticks); return; }
    if (zone === "band") { clipBandDown(ev, x, ticks); return; }
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
    setPane("timeline");
    if (!hit) { paint(); return; }
    S.sel = hit.index;
    S.cursor = null;
    // 자르기 도구(B — Final Cut 「블레이드」) — 누른 자리에서 그 클립을 자른다(⌘B와 같은 연산).
    if (S.tool === "blade") { seek(frameAtTicks(ticks)); clipOp("blade"); return; }
    if (hit.part === "body") { seek(frameAtTicks(ticks)); paint(); return; }
    var clip = S.doc.clips[hit.index];
    var side = hit.part === "start" ? TL.SIDE_START : TL.SIDE_END;
    var span = S.spans[hit.index];
    var at = (side === TL.SIDE_START ? span.outStart : span.outStart + span.outFrames) * seqTicks();
    S.clipDrag = { index: hit.index, side: side, base: S.doc, origin: ticks, at: at,
      edge: side === TL.SIDE_START ? clip[1] : clip[2], frame: null };
    laneCanvas().setPointerCapture(ev.pointerId);
    paint();
  }
  // 끄는 동안은 문서를 미리 바꿔 그린다(기록·저장 없음) — 뒤 클립·파형·자막이 함께 밀린다.
  // 자막은 원본 시각에 붙어 있어 제 말과 함께 움직이고, 컷 밖으로 나간 자막은 가려질 뿐
  // 문서에 남는다(규칙 9 — 다시 늘리면 돌아온다).
  function clipBandMove(ev) {
    var drag = S.clipDrag;
    // 붙는 것은 가장자리다 — 누른 자리와 가장자리의 차이만큼 옮겨 잰다.
    var ticks = drag.at + LN.ticksAt(laneView(), laneX(ev)) - drag.origin;
    if (!ev.shiftKey && !S.snapOff) {
      var snaps = [S.playhead * seqTicks()];
      TL.layout(drag.base).forEach(function (span, i) {
        if (i !== drag.index) { snaps.push(span.outStart * seqTicks()); }
      });
      ticks = LN.snap(ticks, snaps, S.settings.snapPx * laneView().tpp);
    }
    var clip = drag.base.clips[drag.index];
    var want = LN.trimFrame(drag.edge, ticks - drag.at,
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
  // 스토리라인 위 올린 자리 — 잡을 끝은 노란 손잡이 · 자르기 도구면 자를 선(그리기만).
  function hoverBand(c, view, x) {
    var clip = S.spans.length ? LN.clipHit(S.spans, view, x, S.settings.snapPx, seqTicks())
      : null;
    var blade = S.tool === "blade";
    c.style.cursor = !clip ? "default" : blade ? "crosshair"
      : (clip.part === "body" ? "pointer" : "ew-resize");
    var next = clip ? { index: clip.index, part: clip.part, x: x } : null;
    var was = S.hoverBand;
    S.hoverBand = next;
    var same = was && next && was.index === next.index && was.part === next.part;
    if (blade || !same) { requestLane(); }
  }
  function onLaneLeave() {
    skimAt(null);
    if (!S.hoverBand) { return; }
    S.hoverBand = null;
    requestLane();
  }
  // 레인의 띠 — 맨 위 시간 눈금 · 자막 칸 · 맨 아래 스토리라인(이름 · 필름 · 소리).
  function laneZone(ev) {
    var c = laneCanvas();
    var y = ev.clientY - c.getBoundingClientRect().top;
    if (y < RULER_PX) { return "ruler"; }
    return y >= laneLayout(c.clientHeight).bandTop ? "band" : "lane";
  }
  // 시간 눈금을 누르고 끌면 플레이헤드가 따라온다(Final Cut).
  function rulerDown(ev, ticks) {
    if (S.playing) { stopPlayback(); }
    S.rulerDrag = true;
    skimAt(null);
    laneCanvas().setPointerCapture(ev.pointerId);
    seek(frameAtTicks(ticks));
  }
  // 스키밍(W-103, D176 — Final Cut S) — 올린 자리의 프레임을 뷰어에 보이고(재생 · 끌기 중 쉰다),
  // 내리면 플레이헤드 프레임으로 돌린다. 문서 · 플레이헤드 · 저장에 닿지 않는다.
  function skimAt(x) {
    var on = S.skimOn && x !== null && !S.playing && !S.clipDrag && !S.rulerDrag && !S.navDrag;
    var next = on ? x : null;
    if (next === S.skimX) { return; }
    var was = S.skimX;
    S.skimX = next;
    requestLane();
    if (next === null) {
      if (was !== null && was !== undefined && !S.playing) { showSeqFrame(S.playhead); }
      return;
    }
    showSeqFrame(skimFrame());
  }
  function skimFrame() {
    return frameAtTicks(clamp(LN.ticksAt(laneView(), S.skimX), 0, laneTotal()));
  }
  function showSeqFrame(frame) {
    if (!S.doc || !S.spans.length || S.playing) { return; }
    var at = TL.sourcePoint(S.doc, clamp(frame, 0, Math.max(0, S.total - 1)));
    var id = S.spans[at[0]].sourceId;
    if (mediaOf(id)) { place(active(), id, at[1]); }
  }
  function toggleSkim() {
    S.skimOn = !S.skimOn;
    if (!S.skimOn) { skimAt(null); }
    paintTools();
    say(S.skimOn ? "스키밍을 켰습니다 — 타임라인에 올린 자리가 뷰어에 보입니다(S로 끕니다)."
      : "스키밍을 껐습니다 — 뷰어는 플레이헤드만 따라갑니다(S로 켭니다).", false);
    return true;
  }
  function onLaneMove(ev) {
    var c = laneCanvas();
    var view = laneView();
    var x = laneX(ev);
    if (S.clipDrag) { clipBandMove(ev); return; }
    if (S.rulerDrag) { seek(frameAtTicks(clamp(LN.ticksAt(view, x), 0, laneTotal()))); return; }
    var drag = S.laneDrag;
    skimAt(drag ? null : x);
    if (!drag) {
      var zone = laneZone(ev);
      if (zone === "ruler") { c.style.cursor = "text"; }
      if (zone === "band") { hoverBand(c, view, x); return; }
      if (S.hoverBand) { S.hoverBand = null; requestLane(); }
      if (zone === "ruler") { return; }
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
        laneSafeGap(), seqTicks(), laneTotal());
    } else if (drag.part === "many") {
      drag.value = LN.bodyMany(cues, drag.indices, ticks - drag.origin, laneSafeGap(),
        laneTotal());
    } else if (drag.part === "body") {
      drag.value = LN.body(cues, drag.index, ticks - drag.origin, laneSafeGap(), laneTotal());
    } else {
      drag.value = LN.range(drag.origin, ticks, laneTotal());
      S.laneSel = drag.value;
    }
    requestLane();
  }
  function onLaneUp() {
    if (S.rulerDrag) { S.rulerDrag = false; return; }
    if (S.clipDrag) { clipBandUp(); return; }
    var drag = S.laneDrag;
    S.laneDrag = null;
    // 레인의 자막 블록을 끌지 않고 누르기만 했으면 그 자막의 글 칸으로(D180).
    if (drag && !drag.moved && drag.part === "body" && drag.id) {
      requestLane();
      editPickedText();
      return;
    }
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
    var delta = LN.body(S.proj.cues, index, ticks - S.proj.cues[index][2], laneSafeGap(),
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
    if (S.rendering) { return; }
    if (payload.burn === "ko-en" && !englishReady()) { return; }
    if (W.__vlServer) { W.__vlServer.wake(true); }
    // 누른 순간 답한다 — 번역 연결 확인 · 리비전 SRT 쓰기가 몇 초 걸릴 수 있다. 답이 오지 않거나
    // 서버 함수가 터져도 상태 줄에 까닭을 남긴다(예전에는 거절이 아무 말 없이 사라졌다).
    S.rendering = true;
    paintRenderButton();
    say("최종출력을 거는 중 — 리비전 " + payload.revision + "…", false);
    S.server.tl_render(payload).catch(function () {
      return { ok: false, error: "서버가 답하지 않았습니다(「기록」의 서버 로그를 보세요)." };
    }).then(function (got) {
      S.rendering = false;
      paintRenderButton();
      if (!got || !got.ok) {
        say("최종출력을 걸지 못했습니다 — " + ((got && got.error) || "까닭을 받지 못했습니다."),
          true);
        return;
      }
      say("최종출력 중 — 리비전 " + payload.revision + "(렌더" +
        (payload.burn === "none" ? ", 무료 로컬" : " + 자막 입히기") + ") — 활동 띠에서 진행을" +
        " 보고, 끝나면 결과 카드와 초안 폴더에 나옵니다.", false);
      watchRender(got.jobId);
    });
  }
  function paintRenderButton() {
    var button = $("tl-render");
    if (button) { button.disabled = !!S.rendering; }
  }
  // ─── 영어 자막 켜고 끄기(W-112, D186) ───────────────────────────
  // 「영상에 넣을 자막」이 「한국어 + 영어」일 때만 영어가 켜진다 — 영어 열(번역 모드)과 자막 겹에
  // 영어가 보이고 최종출력이 입힌다. 다른 고름이면 영어를 보이지도 입히지도 않는다. 고름은 이
  // 브라우저에만 기억한다(편의 — 기억이 없거나 막혀도 「없음」으로 그대로 돈다).
  var BURN_KEY = "vlogsrt.burnMode";
  function englishOn() {
    var pick = $("tl-burn");
    return !!pick && pick.value === "ko-en";
  }
  function restoreBurn() {
    var pick = $("tl-burn");
    if (!pick) { return; }
    var saved = null;
    try { saved = W.localStorage.getItem(BURN_KEY); } catch (error) { saved = null; }
    var known = [].some.call(pick.options, function (option) { return option.value === saved; });
    if (saved && known) { pick.value = saved; }
  }
  function onBurnChange() {
    var pick = $("tl-burn");
    try { W.localStorage.setItem(BURN_KEY, pick.value); } catch (error) { /* 기억은 편의일 뿐 */ }
    setTranslation(englishOn());
    paintOverlay();
    say(englishOn()
      ? "영어 자막을 켰습니다 — 영어 열과 화면 자막에서 확인하세요. 영어가 없으면 한국어를 다" +
        " 고친 뒤 자막 칸의 「영어 만들기」를 누르세요(Gemini, 유료)."
      : "영어 자막을 껐습니다 — 영어를 보이지도 영상에 넣지도 않습니다.", false);
  }
  // 「한국어 + 영어」는 영어를 본 뒤에만 출력한다(W-112, D186 — 사용자 지시 「영어자막도 출력전에
  // 확인」). 보이는 자막 중 영어(고친 영어 · 저장된 번역)가 없는 줄이 있으면 렌더하지 않고 「영어
  // 만들기」를 건다(유료 · 누름이 승인, D182). 끝나면 영어 열 · 화면에서 확인하고 다시 누른다.
  function englishReady() {
    if (S.translating) {
      say("영어로 옮기는 중입니다 — 끝나면 확인한 뒤 「최종출력」을 다시 누르세요.", false);
      return false;
    }
    if (!S.english || S.english === "pending") {
      if (!S.english) { askEnglish(); }
      say("영어 자막을 찾는 중입니다 — 잠시 뒤 「최종출력」을 다시 누르세요.", false);
      return false;
    }
    var missing = S.proj.cues.filter(function (cue) {
      if (!cue[4] || !cue[4].trim()) { return false; }
      var kind = englishLabel(cue[0], cue[4]).kind;
      return kind !== "edited" && kind !== "saved";
    }).length;
    if (!missing) { return true; }
    setTranslation(true);
    if (W.__vlWindow && W.__vlWindow.showInspector) { W.__vlWindow.showInspector("analysis"); }
    say("영어가 없는 자막 " + missing + "줄 — 최종출력하지 않았습니다. 한국어를 다 고친 뒤" +
      " 자막 칸의 「영어 만들기」를 눌러 옮기고(Gemini, 유료), 영어 열 · 화면 자막에서 확인한 뒤" +
      " 다시 누르세요.", true);
    return false;
  }
  // 「영어 만들기」(W-99, D174) — 저장한 리비전의 자막을 옮긴다(유료 · 누름이 승인, D110).
  // 저장 대기 편집을 먼저 보낸다. 끝나면 영어를 다시 찾아 영어 열·자막 겹에 보인다
  // (같은 글은 다시 사지 않는다).
  function translate() {
    commitText();
    if (S.translating) {
      say("영어로 옮기는 중입니다 — 끝나면 영어 열과 화면 자막에 보입니다.", false);
      return;
    }
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
    S.translating = "asking";
    S.server.tl_translate(payload).catch(function () {
      return { ok: false, error: "영어 만들기를 걸지 못했습니다 — 서버가 답하지 않았습니다." };
    }).then(function (got) {
      if (!got || !got.ok) {
        S.translating = null;
        say((got && got.error) || "영어 만들기를 걸지 못했습니다.", true);
        return;
      }
      S.translating = got.jobId;
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
        if (gen !== S.pollGen || !got || !got.ok) { S.translating = null; return; }
        if (!got.ended) { watchTranslate(jobId); return; }
        S.translating = null;
        if (got.status !== "succeeded") {
          say("영어를 만들지 못했습니다(" + got.status + ") — 「기록」에서 까닭을 보세요.", true);
          return;
        }
        say("영어를 만들었습니다 — 영어 열과 화면 자막에서 확인하고(고칠 줄은 영어 칸에서" +
          " 고칩니다) 「최종출력」을 누르세요.", false);
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
        say("최종출력을 마쳤습니다 — 인스펙터 「공유」의 결과 카드 「초안 폴더 열기」에 영상이" +
          " 있습니다(자막을 입혔으면 <이름>.burned.mp4 · 영어 번역이 아직 없으면 한국어만).",
          false);
        if (W.__vlWindow && W.__vlWindow.showInspector) { W.__vlWindow.showInspector("share"); }
        // 새 초안의 저장된 영어가 생겼을 수 있다 — 번역 모드면 다시 찾는다.
        S.english = null;
        askEnglish();
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
    // 클립 띠를 끄는 동안의 문서는 미리 그리기다(D169) — 놓은 뒤 기록과 함께 저장한다.
    if (S.clipDrag) { scheduleSave(); return; }
    if (!S.dirty || S.saving || !S.doc) { return; }
    S.saving = true;
    paintSaveChip();
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
    if (target.closest(".tl-cues, .tl-cues-head, .tl-cues-body")) { return "captions"; }
    if (target.closest("#tl-index-list")) {
      return indexState().tab === "cues" ? "captions" : "timeline";
    }
    if (target.closest(".tl-strip, .tl-range, .tl-transport, .tl-stage")) { return "timeline"; }
    return null;
  }
  // 키 창 표지는 재생기와 재생기 밖 자막 칸(D178)에 함께 단다.
  function setPane(pane) {
    S.pane = pane;
    if (S.rootEl) { S.rootEl.setAttribute("data-tl-pane", pane); }
    var holder = $("vl-captions");
    if (holder) { holder.setAttribute("data-tl-pane", pane); }
  }
  function notePane(ev) {
    var pane = paneOf(ev.target);
    if (pane && pane !== S.pane) { setPane(pane); }
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
      "tool-select": function () { return setTool("select"); }, skimming: toggleSkim,
      "tool-blade": function () { return setTool("blade"); },
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
    var delta = LN.bodyMany(S.proj.cues, indices, frames * seqTicks(), laneSafeGap(),
      laneTotal());
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
      "tool-select": function () { setTool("select"); }, skimming: toggleSkim,
      snapping: toggleSnap, "center-play": toggleCenterPlay,
      "tool-blade": function () { setTool("blade"); },
      join: function () { clipOp("join"); },
      "cue-start": function () { cueOp("start", cueTargetOf(target)); },
      "cue-end": function () { cueOp("end", cueTargetOf(target)); },
      "cue-split": function () { cueOp("split", cueTargetOf(target)); },
      "cue-merge": function () { cueOp("merge", cueTargetOf(target)); },
      "cue-delete": function () { cueOp("delete", cueTargetOf(target)); },
      "cue-insert": function () { commitText(); cueOp("insert"); },
      "force-save": function () { saveNow(true); }, render: render, index: toggleIndex,
      "index-clips": function () { indexTab("clips"); },
      "index-cues": function () { indexTab("cues"); }, translation: toggleTranslation,
      translate: translate, "cue-en-ask": function () { askLineEnglish(cueTargetOf(target)); } };
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
  // 전체 보기 띠(W-103, D176) — 누르거나 끌면 그 자리로 가고(그 클립을 고른다) 스토리라인이 그
  // 자리를 보이게 따라간다. 재생 중이었으면 놓은 뒤 다시 튼다.
  function navFrame(c, clientX) {
    var rect = c.getBoundingClientRect();
    var frame = Math.round((clientX - rect.left) / Math.max(rect.width, 1) * S.total);
    return clamp(frame, 0, Math.max(0, S.total - 1));
  }
  function navTo(frame) {
    if (S.spans.length) { S.sel = TL.sourcePoint(S.doc, frame)[0]; }
    S.cursor = null;
    var lane = laneCanvas();
    if (lane && lane.clientWidth) {
      S.lane = LN.follow(laneView(), frame * seqTicks(), lane.clientWidth);
    }
    seek(frame);
  }
  // 영상 목록의 카드를 누르면(W-112, D186) 타임라인에서 그 원본의 첫 클립으로 간다 — 고르고
  // 플레이헤드와 레인만 옮긴다(문서는 그대로). 타임라인에 없는 원본이면 넣는 길을 말한다.
  W.__vlTimelineJump = function (sourceId) {
    if (!S.doc || !S.spans.length) { return false; }
    var span = S.spans.filter(function (row) { return row.sourceId === sourceId; })[0];
    if (!span) {
      say(nameOf(sourceId) + "은(는) 타임라인에 없습니다 — 카드를 레인에 끌어 놓으면" +
        " 넣습니다.", false);
      return false;
    }
    if (S.playing) { stopPlayback(); }
    navTo(span.outStart);
    S.sel = span.index;
    say(nameOf(sourceId) + " — 타임라인 " + (span.index + 1) + "번째 클립으로 갔습니다.", false);
    paint();
    return true;
  };
  function onStripDown(ev) {
    if (!S.doc || !S.total) { return; }
    var c = ev.currentTarget;
    S.navDrag = { resume: S.playing };
    if (S.playing) { stopPlayback(); }
    commitText();
    clearPicked();
    c.setPointerCapture(ev.pointerId);
    navTo(navFrame(c, ev.clientX));
    if (S.rootEl) { S.rootEl.focus({ preventScroll: true }); }
  }
  function onStripMove(ev) {
    if (!S.navDrag) { return; }
    navTo(navFrame(ev.currentTarget, ev.clientX));
  }
  function onStripUp() {
    var drag = S.navDrag;
    S.navDrag = null;
    if (drag && drag.resume) { play(); }
  }
  function onTimeUpdate(ev) {
    var v = ev.target;
    if (S.playing && v === active()) { onFrame(v, v.currentTime, Math.floor); }
  }
  // 화면 자막 모양을 고치는 중이면(W-107) 영상 누름은 고르기를 푼다 — 캡컷처럼 밖을 누르면 닫힌다.
  function onVideoClick() {
    if (S.styling) { setStyling(false); return; }
    toggle();
  }
  function forwardOutside(part) {
    part.__tlBound = true;
    part.addEventListener("keydown", function (ev) {
      if (S.rootEl && !S.rootEl.contains(part)) { onKey(ev); }
    });
    part.addEventListener("click", function (ev) {
      if (S.rootEl && !S.rootEl.contains(part)) { onClick(ev); }
    });
    part.addEventListener("pointerdown", function (ev) {
      if (S.rootEl && !S.rootEl.contains(part)) { notePane(ev); }
    }, true);
  }
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
    // 자막 칸(D178)은 머리 · 목록 · 입력칸을 재생기 밖으로 옮긴다 — 둘 다 같은 처리로 넘긴다.
    [".tl-cues-head", ".tl-cues-body"].forEach(function (selector) {
      var part = rootEl.querySelector(selector) ||
        document.querySelector("#vl-captions " + selector);
      if (part && !part.__tlBound) { forwardOutside(part); }
    });
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
      c.addEventListener("pointermove", onStripMove);
      c.addEventListener("pointerup", onStripUp);
      c.addEventListener("pointercancel", onStripUp);
      if (W.ResizeObserver) { new W.ResizeObserver(paintStrip).observe(c); }
    }
    var indexList = $("tl-index-list");
    if (indexList && !indexList.__tlBound) {
      indexList.__tlBound = true;
      indexList.addEventListener("pointerdown", onIndexDown);
    }
    var burn = $("tl-burn");
    if (burn && !burn.__tlBound) {
      burn.__tlBound = true;
      burn.addEventListener("change", onBurnChange);
    }
    var indexErrors = $("tl-index-errors");
    if (indexErrors && !indexErrors.__tlBound) {
      indexErrors.__tlBound = true;
      indexErrors.addEventListener("change", function () {
        indexState().errors = indexErrors.checked;
        paintIndex();
      });
    }
    var zoom = $("tl-zoom");
    if (zoom && !zoom.__tlBound) {
      zoom.__tlBound = true;
      zoom.addEventListener("input", onZoomSlider);
    }
    var lane = laneCanvas();
    if (lane && !lane.__tlBound) {
      lane.__tlBound = true;
      lane.addEventListener("pointerdown", onLaneDown);
      lane.addEventListener("pointermove", onLaneMove);
      lane.addEventListener("pointerup", onLaneUp);
      lane.addEventListener("pointercancel", onLaneUp);
      lane.addEventListener("pointerleave", onLaneLeave);
      lane.addEventListener("wheel", onLaneWheel, { passive: false });
      lane.addEventListener("contextmenu", onLaneMenu);
      lane.addEventListener("dragover", onDropOver);
      lane.addEventListener("dragleave", onDropLeave);
      lane.addEventListener("drop", onDrop);
      if (W.ResizeObserver) { new W.ResizeObserver(requestLane).observe(lane); }
    }
    var deckEl = S.rootEl && S.rootEl.closest ? S.rootEl.closest("#vl-timeline-deck") : null;
    var refit = function () { if (S.doc) { fitDeck(); } };
    // 편집 칸 높이(자막 고름 · 번역 모드)가 바뀌면 위 덱 바닥을 다시 잰다 — 인스펙터 칸 크기는
    // 그대로라 덱 관찰로는 모른다. 재생기를 다시 그리면 칸도 새것이라 칸마다 묶는다.
    // 이동 막대(상태 줄에 글이 생기면 한 줄 자란다)도 같다.
    [$("tl-cue-edit"), S.rootEl ? S.rootEl.querySelector(".tl-transport") : null]
      .forEach(function (box) {
        if (box && !box.__tlFloorBound && W.ResizeObserver) {
          box.__tlFloorBound = true;
          new W.ResizeObserver(refit).observe(box);
        }
      });
    // 높이만 줄어든 창 — 무대가 바닥 합(`--vl-fit-need`)에 묶여 덱 크기가 그대로면 덱 관찰로는
    // 모른다. 남는 높이(위 덱 바닥의 상한)는 창 높이에서 온다.
    if (!W.__tlFitResizeBound && W.addEventListener) {
      W.__tlFitResizeBound = true;
      W.addEventListener("resize", refit);
    }
    if (deckEl && !deckEl.__tlFitBound && W.ResizeObserver) {
      deckEl.__tlFitBound = true;
      new W.ResizeObserver(refit).observe(deckEl);
      // 작업공간(「자막」 — 목록이 덱 밖으로)·맞춤·뷰어가 바뀌면 덱 크기가 그대로여도 다시 잰다.
      var stageEl = deckEl.closest("[id=visual-stage]");
      if (stageEl && W.MutationObserver) {
        new W.MutationObserver(function () { if (S.doc) { fitDeck(); } }).observe(stageEl, {
          attributes: true,
          attributeFilter: ["data-vl-workspace", "data-vl-fit", "data-vl-viewer"] });
      }
    }
    bindStyleTools();
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
        // 그냥 누르면 그 자막의 글 칸으로 간다(D180 — 사용자 지시). ⇧ · ⌘로 여러 줄을 고를 때는
        // 키(화살표 · ⌫ · [ ])가 재생기로 오게 초점을 재생기에 둔다.
        if (!ev.shiftKey && !ev.metaKey && !ev.ctrlKey && !ev.altKey) { editPickedText(); return; }
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
