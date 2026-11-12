"""최종 편집 탭(W-66, D117)의 화면 실행본 — `CORE_JS`(`window.__vlFE`) 위에서 돈다.

상태는 `window.__vlFinalEdit` 하나에 산다 — 그라디오가 탭을 다시 그려도(같은 초안이면) 편집 중인
문서와 되돌리기 기록이 남는다. DOM은 사건 순간에 다시 찾는다. 숫자는 서버가 보낸
설정(`edit.settings`)과 단축키 표(`keymap`)에서 온다. 아래 `LAYOUT`은 그리기 자리(화소)일 뿐
판정 기준이 아니다.
"""

from __future__ import annotations

APP_JS = r"""
(function () {
  var W = window;
  var FE = W.__vlFE;
  var S = W.__vlFinalEdit;
  if (!S) {
    S = W.__vlFinalEdit = { epoch: null, exportId: null, doc: null, playhead: 0, range: null,
      sel: null, tool: "select", snap: true, pxPerS: 0, scrollX: 0, wave: null, dirty: false,
      saving: false, playing: false, rateIndex: 0, reverseTimer: null, stopAt: null,
      banner: null, status: "", statusError: false };
  }
  W.__vlFinalEditServer = server;
  var LAYOUT = { ruler: 22, clip: 42, gap: 6, edge: 6, pad: 4, minLabelPx: 80 };
  var RULER_STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200];
  var IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

  function $(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  function fd() { return S.doc.draft.num / S.doc.draft.den; }
  function ft() { return FE.frameTicks(S.doc); }
  function total() { return FE.editedFrames(S.doc); }
  function phTicks() { return S.playhead * ft(); }
  function nominalFps() { return Math.round(S.doc.draft.den / S.doc.draft.num); }
  function timecode(frame) {
    var fps = nominalFps();
    var secs = Math.floor(frame / fps);
    var ff = frame - secs * fps;
    function two(v) { return (v < 10 ? "0" : "") + v; }
    return two(Math.floor(secs / 3600)) + ":" + two(Math.floor(secs / 60) % 60) + ":" +
      two(secs % 60) + ":" + two(ff);
  }
  function tcMs(ticks) { return FE.formatMs(FE.ticksToMsHalfUp(S.doc, ticks)); }
  function parseMs(text) {
    var m = /^\s*(?:(\d+):)?(\d+):(\d+)(?:[,.](\d{1,3}))?\s*$/.exec(text);
    if (!m) { return null; }
    var ms = m[4] ? parseInt((m[4] + "00").slice(0, 3), 10) : 0;
    return ((parseInt(m[1] || "0", 10) * 60 + parseInt(m[2], 10)) * 60 +
      parseInt(m[3], 10)) * 1000 + ms;
  }
  function say(text, isError) {
    S.status = text || "";
    S.statusError = !!isError;
    var el = $("fe-status");
    if (el) { el.textContent = S.status; el.classList.toggle("is-error", S.statusError); }
  }

  // ─── 모델 받기 ───────────────────────────────────────────────────
  function readModel() {
    var rootEl = element.querySelector ? element.querySelector("#fe-root") : null;
    if (!rootEl) { return null; }
    var holder = rootEl.querySelector("[data-fe-model]");
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
    S.rootEl = got.rootEl;
    S.epoch = got.model.epoch;
    if (got.model.empty) { stopPlayback(); S.empty = true; return; }
    S.empty = false;
    S.signature = got.model.signature;
    S.token = got.model.token;
    S.keymap = got.model.keymap;
    adopt(got.model.edit, true);
    bindDom();
    fetchWave();
  }
  function docFrom(edit) {
    return { draft: { num: edit.frameDuration.num, den: edit.frameDuration.den,
      totalFrames: edit.totalFrames }, clips: edit.doc.clips, cues: edit.doc.cues,
      nextCue: edit.doc.nextCue };
  }
  function adopt(edit, keepLocal) {
    var same = S.exportId === edit.exportId;
    if (!same && S.dirty && S.exportId) { saveNow(false); }
    S.settings = edit.settings;
    var unchanged = same && S.doc && edit.revision === S.baseRevision;
    if (!(same && keepLocal && S.doc && (S.dirty || unchanged))) {
      S.doc = docFrom(edit);
      S.baseRevision = edit.revision;
      S.dirty = false;
      S.history = new FE.History(S.settings.undoLimit, S.settings.coalesceMs);
      if (!same) {
        S.playhead = 0; S.range = null; S.sel = null; S.wave = null; S.pxPerS = 0;
        S.scrollX = 0; S.banner = null;
      }
    }
    S.edit = edit;
    S.exportId = edit.exportId;
    S.seedSrt = S.seedSrt && same ? S.seedSrt : edit.seedSrt;
    if (edit.newerSeed && !S.banner) { S.banner = "seed"; }
    else if (edit.olderEdits && edit.olderEdits.length && !S.banner) { S.banner = "older"; }
    refresh();
  }

  // ─── 문서 바꾸기 · 되돌리기 ─────────────────────────────────────
  // light: 드래그 중 — 캔버스·오버레이만(목록·인스펙터·도구 막대는 놓을 때 한 번).
  function refresh(light) {
    S.proj = FE.project(S.doc);
    var warns = {};
    FE.warnings(S.doc, S.settings, S.proj).forEach(function (w) {
      (warns[w[0]] = warns[w[0]] || []).push(w[1]);
    });
    S.warns = warns;
    if (S.playhead > total()) { S.playhead = total(); }
    if (light) { paintPlayhead(); return; }
    paintAll();
  }
  function apply(label, fn, key) {
    var before = S.doc;
    var after;
    try { after = fn(before); }
    catch (err) {
      if (FE.isError(err)) { say(err.message, true); return false; }
      throw err;
    }
    if (!after || after === before) { return false; }
    S.history.record(label, before, key || null, Date.now());
    S.doc = after;
    changed(label);
    return true;
  }
  function changed(label) {
    S.dirty = true;
    refresh();
    say(label ? label + " — 저장 대기" : "");
    scheduleSave();
  }
  function undo(redo) {
    var entry = redo ? S.history.redo(S.doc) : S.history.undo(S.doc);
    if (!entry) { say(redo ? "다시 할 것이 없습니다." : "되돌릴 것이 없습니다."); return; }
    S.doc = entry.doc;
    S.sel = null;
    changed((redo ? "다시 하기: " : "되돌리기: ") + entry.label);
  }

  // ─── 저장 ────────────────────────────────────────────────────────
  function scheduleSave() {
    clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(function () { saveNow(false); }, S.settings.autosaveMs);
    paintToolbar();
  }
  // always: 바뀐 것이 없어도 저장(⌘S). overwrite: 다른 화면의 저장을 덮는다(충돌 배너의 단추만).
  function saveNow(always, overwrite) {
    clearTimeout(S.saveTimer);
    if (!S.doc || (!S.dirty && !always)) { return; }
    if (S.saving) { S.saveAgain = true; return; }
    var sent = S.doc;
    var exportId = S.exportId;
    S.saving = true;
    paintToolbar();
    var payload = { signature: S.signature, token: S.token, epoch: S.epoch, exportId: exportId,
      baseRevision: S.baseRevision, force: !!overwrite,
      doc: { clips: sent.clips, cues: sent.cues, nextCue: sent.nextCue, seedSrt: S.seedSrt } };
    var srv = W.__vlFinalEditServer;
    srv.fe_save(payload).then(function (res) {
      S.saving = false;
      if (exportId !== S.exportId) { return; }
      if (!res || !res.ok) {
        if (res && res.stale) { S.banner = "stale"; }
        say("저장하지 못했습니다 — " + ((res && res.error) || "응답 없음"), true);
      } else if (res.conflict) {
        S.banner = "conflict";
        S.conflictRevision = res.revision;
        say("다른 화면이 먼저 저장했습니다(리비전 " + res.revision + ").", true);
      } else {
        S.baseRevision = res.revision;
        S.summary = res.summary;
        if (S.doc === sent) { S.dirty = false; }
        if (S.banner === "conflict") { S.banner = null; }
        if (!S.sel) { paintInspector(); }
        var hidden = res.summary.hidden_by_cut.length;
        say("저장했습니다 · 리비전 " + res.revision + " · edit/final.srt" +
          (hidden ? " · 컷에 가려진 자막 " + hidden + "개" : ""));
      }
      paintBanner();
      paintToolbar();
      if (S.saveAgain) { S.saveAgain = false; if (S.dirty) { saveNow(false); } }
    }, function (err) {
      S.saving = false;
      say("저장하지 못했습니다 — " + err, true);
      paintToolbar();
    });
  }
  function load(extra, then) {
    var payload = { signature: S.signature, exportId: S.exportId };
    Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; });
    W.__vlFinalEditServer.fe_load(payload).then(function (res) {
      if (!res || !res.ok) {
        say("불러오지 못했습니다 — " + ((res && res.error) || ""), true);
        return;
      }
      then(res.edit);
    });
  }

  // ─── 파형 ────────────────────────────────────────────────────────
  function fetchWave() {
    if (!S.exportId || (S.wave && S.wave.exportId === S.exportId) || S.waveLoading === S.exportId) {
      return;
    }
    S.waveLoading = S.exportId;
    var asked = S.exportId;
    W.__vlFinalEditServer.fe_waveform({ signature: S.signature, exportId: asked })
      .then(function (res) {
        S.waveLoading = null;
        if (asked !== S.exportId) { return; }
        if (!res || !res.ok) {
          say("파형을 읽지 못했습니다 — " + ((res && res.error) || ""), true);
          return;
        }
        var raw = atob(res.waveform.peaks_b64);
        var peaks = new Array(raw.length);
        for (var i = 0; i < raw.length; i++) { peaks[i] = raw.charCodeAt(i); }
        S.wave = { exportId: asked, binMs: res.waveform.bin_ms, levels: FE.mipLevels(peaks) };
        invalidate();
      });
  }

  // ─── 재생 ────────────────────────────────────────────────────────
  function video() { return $("fe-video"); }
  function seek(frame) {
    if (!S.doc) { return; }
    S.playhead = clamp(Math.round(frame), 0, total());
    var v = video();
    if (v && S.doc.clips.length) {
      var draftFrame = S.playhead >= total() ? S.doc.clips[S.doc.clips.length - 1][1] - 1
        : FE.draftFrameAt(S.doc, S.playhead);
      v.currentTime = (draftFrame + 0.5) * fd();
    }
    paintPlayhead();
  }
  function play(rate) {
    var v = video();
    if (!v || !S.doc.clips.length) { return; }
    stopReverse();
    if (S.playhead >= total()) { seek(0); }
    v.playbackRate = rate;
    S.playing = true;
    S.loopGen = (S.loopGen || 0) + 1;
    var started = v.play();
    if (started && started.catch) { started.catch(function () { S.playing = false; }); }
    frameLoop(S.loopGen);
  }
  function stopReverse() {
    if (S.reverseTimer) { clearInterval(S.reverseTimer); S.reverseTimer = null; }
  }
  function stopPlayback() {
    var v = video();
    if (v && !v.paused) { v.pause(); }
    stopReverse();
    S.playing = false;
    S.stopAt = null;
    S.loopGen = (S.loopGen || 0) + 1;
  }
  // 루프는 재생마다 하나 — 세대가 바뀌면(다시 재생·멈춤) 옛 루프는 조용히 끝난다.
  function frameLoop(gen) {
    var v = video();
    if (!v || !S.playing || gen !== S.loopGen) { return; }
    if (v.requestVideoFrameCallback) {
      v.requestVideoFrameCallback(function (now, meta) {
        if (gen !== S.loopGen) { return; }
        onFrame(Math.round(meta.mediaTime / fd()));
        frameLoop(gen);
      });
    } else {
      requestAnimationFrame(function () {
        if (gen !== S.loopGen) { return; }
        onFrame(Math.floor(v.currentTime / fd()));
        frameLoop(gen);
      });
    }
  }
  function nextClipAfter(draftFrame) {
    for (var i = 0; i < S.doc.clips.length; i++) {
      if (S.doc.clips[i][0] > draftFrame) { return S.doc.clips[i]; }
    }
    return null;
  }
  function onFrame(draftFrame) {
    if (!S.playing) { return; }
    var v = video();
    var edited = FE.editedFrameOf(S.doc, draftFrame);
    if (edited === null) {
      var next = nextClipAfter(draftFrame);
      if (!next) { stopPlayback(); S.playhead = total(); paintPlayhead(); return; }
      v.currentTime = (next[0] + 0.5) * fd();
      return;
    }
    var index = FE.clipIndexAt(S.doc, edited);
    var clip = S.doc.clips[index];
    var following = S.doc.clips[index + 1];
    if (draftFrame >= clip[1] - 1 && (!following || following[0] !== clip[1])) {
      if (!following) { stopPlayback(); S.playhead = total(); paintPlayhead(); return; }
      v.currentTime = (following[0] + 0.5) * fd();
    }
    S.playhead = edited;
    if (S.stopAt !== null && edited >= S.stopAt) { stopPlayback(); }
    follow();
    paintPlayhead();
  }
  function shuttle(direction) {
    var rates = S.settings.shuttleRates;
    var same = direction > 0 ? (S.playing && !S.reverseTimer) : !!S.reverseTimer;
    S.rateIndex = same ? Math.min(S.rateIndex + 1, rates.length - 1) : 0;
    if (direction > 0) { play(rates[S.rateIndex]); return; }
    var v = video();
    if (v && !v.paused) { v.pause(); }
    S.playing = false;
    stopReverse();
    var tick = S.settings.reverseTickMs;
    S.reverseTimer = setInterval(function () {
      var frames = Math.max(1, Math.round(rates[S.rateIndex] * tick / 1000 / fd()));
      if (S.playhead <= 0) { stopReverse(); return; }
      seek(S.playhead - frames);
      follow();
    }, tick);
  }

  // ─── 좌표 ────────────────────────────────────────────────────────
  function canvas() { return $("fe-canvas"); }
  function width() { var c = canvas(); return c ? c.clientWidth : 0; }
  function fitZoom() {
    var seconds = Math.max(total() * fd(), 1);
    return Math.max((width() - 2 * LAYOUT.pad) / seconds, 0.01);
  }
  function zoomFloor() { return Math.min(S.settings.zoomMinPxPerS, fitZoom()); }
  function xOfTicks(ticks) {
    return LAYOUT.pad + ticks / FE.tickRate(S.doc) * S.pxPerS - S.scrollX;
  }
  function xOfFrame(frame) { return xOfTicks(frame * ft()); }
  function ticksAtX(x) {
    var ticks = Math.round((x - LAYOUT.pad + S.scrollX) / S.pxPerS * FE.tickRate(S.doc));
    return clamp(ticks, 0, total() * ft());
  }
  function frameAtX(x) { return clamp(Math.round(ticksAtX(x) / ft()), 0, total()); }
  function clampScroll() {
    var span = total() * fd() * S.pxPerS;
    S.scrollX = clamp(S.scrollX, 0, Math.max(0, span - width() + 2 * LAYOUT.pad));
  }
  function zoomBy(factor, anchorX) {
    var anchor = anchorX === undefined ? xOfFrame(S.playhead) : anchorX;
    var ticks = ticksAtX(anchor);
    S.pxPerS = clamp(S.pxPerS * factor, zoomFloor(), S.settings.zoomMaxPxPerS);
    S.scrollX = LAYOUT.pad + ticks / FE.tickRate(S.doc) * S.pxPerS - anchor;
    clampScroll();
    invalidate();
  }
  function follow() {
    var x = xOfFrame(S.playhead);
    var w = width();
    if (x > w * 0.92 || x < 0) { S.scrollX += x - w * 0.3; clampScroll(); }
  }
  function lanes() {
    var c = canvas();
    var height = c ? c.clientHeight : 0;
    var waveTop = LAYOUT.ruler + LAYOUT.clip + LAYOUT.gap;
    return { clipTop: LAYOUT.ruler, waveTop: waveTop, waveH: Math.max(20, height - waveTop - 2),
      height: height };
  }
  function snapTicks(ticks, skipCue) {
    if (!S.snap) { return ticks; }
    var limit = S.settings.snapPx / S.pxPerS * FE.tickRate(S.doc);
    var best = ticks;
    var bestGap = limit;
    function consider(candidate) {
      var gap = Math.abs(candidate - ticks);
      if (gap <= bestGap) { best = candidate; bestGap = gap; }
    }
    consider(phTicks());
    FE.clipOffsets(S.doc).forEach(function (offset) { consider(offset * ft()); });
    consider(total() * ft());
    if (S.range) { consider(S.range[0] * ft()); consider(S.range[1] * ft()); }
    S.proj.cues.forEach(function (cue) {
      if (cue[0] !== skipCue) { consider(cue[1]); consider(cue[2]); }
    });
    return best;
  }

  // ─── 그리기 ──────────────────────────────────────────────────────
  function paintAll() {
    paintBanner();
    paintToolbar();
    paintList();
    paintInspector();
    paintPlayhead();
  }
  function invalidate() {
    if (S.frameQueued) { return; }
    S.frameQueued = true;
    requestAnimationFrame(function () { S.frameQueued = false; paintTimeline(); });
  }
  // 색은 토큰에서만 온다(테마 시험이 UI 파일의 색 글자를 막는다).
  function cssVar(style, name) { return style.getPropertyValue(name).trim(); }
  function paintTimeline() {
    var c = canvas();
    if (!c || !S.doc || !c.clientWidth) { return; }
    if (!S.pxPerS) { S.pxPerS = fitZoom(); }
    var ratio = W.devicePixelRatio || 1;
    var w = c.clientWidth;
    var h = c.clientHeight;
    if (c.width !== Math.round(w * ratio) || c.height !== Math.round(h * ratio)) {
      c.width = Math.round(w * ratio);
      c.height = Math.round(h * ratio);
    }
    var g = c.getContext("2d");
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, w, h);
    var style = getComputedStyle(S.rootEl || c);
    var colors = {
      ink: cssVar(style, "--vl-ink"), muted: cssVar(style, "--vl-ink-muted"),
      line: cssVar(style, "--vl-line"), accent: cssVar(style, "--vl-accent"),
      soft: cssVar(style, "--vl-accent-soft"), danger: cssVar(style, "--vl-danger"),
      warn: cssVar(style, "--vl-warn"), clip: cssVar(style, "--vl-surface-raised"),
      wave: cssVar(style, "--vl-ink-faint"), red: cssVar(style, "--vl-tier-red"),
    };
    var L = lanes();
    paintRuler(g, w, colors);
    paintRange(g, L, colors);
    paintClips(g, w, L, colors);
    paintWave(g, w, L, colors);
    paintCues(g, w, L, colors);
    var x = xOfFrame(S.playhead);
    g.fillStyle = colors.danger;
    g.fillRect(Math.round(x) - 1, 0, 2, h);
    g.beginPath(); g.moveTo(x - 6, 0); g.lineTo(x + 6, 0); g.lineTo(x, 8); g.fill();
  }
  function paintRuler(g, w, colors) {
    var step = RULER_STEPS[RULER_STEPS.length - 1];
    for (var i = 0; i < RULER_STEPS.length; i++) {
      if (RULER_STEPS[i] * S.pxPerS >= LAYOUT.minLabelPx) { step = RULER_STEPS[i]; break; }
    }
    g.fillStyle = colors.muted;
    g.strokeStyle = colors.line;
    g.font = "11px " + "ui-monospace, monospace";
    var first = Math.floor(S.scrollX / S.pxPerS / step) * step;
    for (var t = first; t <= total() * fd() + step; t += step) {
      var x = LAYOUT.pad + t * S.pxPerS - S.scrollX;
      if (x > w) { break; }
      g.fillRect(Math.round(x), LAYOUT.ruler - 7, 1, 7);
      var m = Math.floor(t / 60);
      var s = t - m * 60;
      var label = m + ":" + (s < 10 ? "0" : "") + (step < 1 ? s.toFixed(1) : Math.round(s));
      g.fillText(label, x + 3, 12);
    }
    g.fillRect(0, LAYOUT.ruler - 1, w, 1);
  }
  function paintRange(g, L, colors) {
    if (!S.range) { return; }
    var x0 = xOfFrame(S.range[0]);
    var x1 = xOfFrame(S.range[1]);
    g.globalAlpha = 0.18;
    g.fillStyle = colors.warn;
    g.fillRect(x0, 0, x1 - x0, L.height);
    g.globalAlpha = 1;
    g.fillStyle = colors.warn;
    g.fillRect(x0, 0, 2, L.height);
    g.fillRect(x1 - 2, 0, 2, L.height);
  }
  function selectedClips() {
    return S.sel && S.sel.kind === "clip" ? S.sel.ids : [];
  }
  function paintClips(g, w, L, colors) {
    var offsets = FE.clipOffsets(S.doc);
    var chosen = selectedClips();
    g.font = "12px " + "system-ui, sans-serif";
    for (var i = 0; i < S.doc.clips.length; i++) {
      var clip = S.doc.clips[i];
      var x0 = xOfFrame(offsets[i]);
      var x1 = xOfFrame(offsets[i] + clip[1] - clip[0]);
      if (x1 < 0 || x0 > w) { continue; }
      var on = chosen.indexOf(i) >= 0;
      g.fillStyle = on ? colors.soft : colors.clip;
      g.fillRect(x0 + 1, L.clipTop + 2, Math.max(1, x1 - x0 - 2), LAYOUT.clip - 4);
      g.strokeStyle = on ? colors.accent : colors.line;
      g.lineWidth = on ? 2 : 1;
      g.strokeRect(x0 + 1, L.clipTop + 2, Math.max(1, x1 - x0 - 2), LAYOUT.clip - 4);
      if (x1 - x0 > 40) {
        g.fillStyle = colors.ink;
        var secs = ((clip[1] - clip[0]) * fd()).toFixed(1);
        g.fillText("#" + (i + 1) + " · " + secs + "s", Math.max(x0, 0) + 6, L.clipTop + 18);
      }
    }
    g.lineWidth = 1;
  }
  function paintWave(g, w, L, colors) {
    var mid = L.waveTop + L.waveH / 2;
    g.fillStyle = colors.line;
    g.fillRect(0, Math.round(mid), w, 1);
    if (!S.wave) { return; }
    var offsets = FE.clipOffsets(S.doc);
    var secPerPx = 1 / S.pxPerS;
    var binsPerSec = 1000 / S.wave.binMs;
    g.fillStyle = colors.wave;
    for (var i = 0; i < S.doc.clips.length; i++) {
      var clip = S.doc.clips[i];
      var x0 = Math.max(0, Math.floor(xOfFrame(offsets[i])));
      var x1 = Math.min(w, Math.ceil(xOfFrame(offsets[i] + clip[1] - clip[0])));
      var clipStart = clip[0] * fd();
      var editStart = offsets[i] * fd();
      for (var x = x0; x < x1; x++) {
        var edited = (x - LAYOUT.pad + S.scrollX) * secPerPx;
        var draftSec = clipStart + edited - editStart;
        var a = draftSec * binsPerSec;
        var peak = FE.columnPeak(S.wave.levels, a, Math.max(a + 1, a + secPerPx * binsPerSec));
        var half = peak / 255 * (L.waveH / 2 - 2);
        if (half >= 0.5) { g.fillRect(x, mid - half, 1, half * 2); }
      }
    }
  }
  function selectedCue() { return S.sel && S.sel.kind === "cue" ? S.sel.ids[0] : null; }
  function paintCues(g, w, L, colors) {
    var chosen = selectedCue();
    g.font = "12px " + "system-ui, sans-serif";
    for (var i = 0; i < S.proj.cues.length; i++) {
      var cue = S.proj.cues[i];
      var x0 = xOfTicks(cue[1]);
      var x1 = Math.max(x0 + 2, xOfTicks(cue[2]));
      if (x1 < 0 || x0 > w) { continue; }
      var on = cue[0] === chosen;
      var warned = !!S.warns[cue[0]];
      g.globalAlpha = on ? 0.42 : 0.24;
      g.fillStyle = warned ? colors.red : colors.accent;
      g.fillRect(x0, L.waveTop + 2, x1 - x0, L.waveH - 4);
      g.globalAlpha = 1;
      g.strokeStyle = on ? colors.ink : (warned ? colors.red : colors.accent);
      g.lineWidth = on ? 2 : 1;
      g.strokeRect(x0, L.waveTop + 2, x1 - x0, L.waveH - 4);
      if (x1 - x0 > 24) {
        g.save();
        g.beginPath();
        g.rect(x0 + 2, L.waveTop + 2, x1 - x0 - 4, L.waveH - 4);
        g.clip();
        g.fillStyle = colors.ink;
        g.fillText(cue[3].replace(/\n/g, " / "), x0 + 4, L.waveTop + 16);
        g.restore();
      }
    }
    g.lineWidth = 1;
  }
  function currentCueIndex() {
    var t = phTicks();
    var cues = S.proj.cues;
    var low = 0;
    var high = cues.length - 1;
    var found = -1;
    while (low <= high) {
      var mid = (low + high) >> 1;
      if (cues[mid][1] <= t) { found = mid; low = mid + 1; } else { high = mid - 1; }
    }
    for (var i = found; i >= 0 && i >= found - 3; i--) {
      if (cues[i][1] <= t && t < cues[i][2]) { return i; }
    }
    return -1;
  }
  function paintPlayhead() {
    if (!S.doc || S.empty) { return; }
    var tc = $("fe-timecode");
    if (tc) { tc.textContent = timecode(S.playhead); }
    var dur = $("fe-duration");
    if (dur) { dur.textContent = timecode(total()); }
    var index = currentCueIndex();
    var overlay = $("fe-overlay");
    if (overlay) { overlay.textContent = index >= 0 ? S.proj.cues[index][3] : ""; }
    var id = index >= 0 ? S.proj.cues[index][0] : null;
    if (id !== S.currentCue) {
      var body = $("fe-list-body");
      if (body) {
        var old = body.querySelector(".fe-row.is-current");
        if (old) { old.classList.remove("is-current"); }
        var row = id ? body.querySelector('[data-cue="' + id + '"]') : null;
        if (row) {
          row.classList.add("is-current");
          if (S.playing && row.scrollIntoView) { row.scrollIntoView({ block: "nearest" }); }
        }
      }
      S.currentCue = id;
    }
    invalidate();
  }
  var WARN_LABEL = { empty: "빈 글", zero: "길이 0", short: "짧음", long: "김", fast: "빠름",
    overlap: "겹침", gap: "좁은 틈" };
  function paintList() {
    var body = $("fe-list-body");
    if (!body) { return; }
    var chosen = selectedCue();
    var rate = FE.tickRate(S.doc);
    var rows = S.proj.cues.map(function (cue, i) {
      var span = cue[2] - cue[1];
      var cps = span > 0 ? (FE.textChars(cue[3]) * rate / span).toFixed(1) : "–";
      var kinds = S.warns[cue[0]] || [];
      var flags = kinds.map(function (k) {
        return '<span class="fe-flag">' + esc(WARN_LABEL[k] || k) + "</span>";
      }).join("");
      var cls = "fe-row" + (cue[0] === chosen ? " is-selected" : "") +
        (kinds.length ? " has-warn" : "") + (cue[0] === S.currentCue ? " is-current" : "");
      return '<div class="' + cls + '" data-cue="' + esc(cue[0]) + '">' +
        '<span class="fe-num">' + (i + 1) + "</span>" +
        '<span class="fe-time">' + tcMs(cue[1]) + " → " + tcMs(cue[2]) + "</span>" +
        '<span class="fe-cps">' + (span / rate).toFixed(2) + "s · " + cps + "</span>" +
        '<span class="fe-text">' + esc(cue[3]) + flags + "</span></div>";
    });
    body.innerHTML = rows.join("");
  }
  function projected(id) {
    for (var i = 0; i < S.proj.cues.length; i++) {
      if (S.proj.cues[i][0] === id) { return S.proj.cues[i]; }
    }
    return null;
  }
  function paintInspector() {
    var box = $("fe-inspector");
    if (!box || !S.doc) { return; }
    if (box.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA") {
      return;
    }
    var id = selectedCue();
    var cue = id ? projected(id) : null;
    if (cue) { box.innerHTML = cueInspector(cue); return; }
    var clips = selectedClips();
    if (clips.length === 1 && S.doc.clips[clips[0]]) {
      box.innerHTML = clipInspector(clips[0]);
      return;
    }
    box.innerHTML = summaryInspector();
  }
  function cueInspector(cue) {
    var kinds = (S.warns[cue[0]] || []).map(function (k) { return WARN_LABEL[k] || k; });
    var rate = FE.tickRate(S.doc);
    return "<strong>자막 " + esc(cue[0]) + "</strong>" +
      (kinds.length ? '<span class="fe-status is-error">' + esc(kinds.join(" · ")) + "</span>"
        : "") +
      field("시작", "start", tcMs(cue[1])) + field("끝", "end", tcMs(cue[2])) +
      "<span>길이 " + ((cue[2] - cue[1]) / rate).toFixed(3) + "초" +
      (cue[4] ? " · 컷에 일부 잘림" : "") + "</span>" +
      '<textarea id="fe-text" spellcheck="false">' + esc(cue[3]) + "</textarea>" +
      '<div class="fe-buttons">' + button("cue-split", "나누기 ⇧B") +
      button("cue-merge", "다음과 합치기 ⇧M") + button("delete", "지우기 ⌫") + "</div>";
  }
  function field(label, name, value) {
    return '<label class="fe-field"><span>' + label + "</span>" +
      '<input data-time="' + name + '" value="' + esc(value) + '">' +
      button("nudge-" + name + "--1", "−1f") + button("nudge-" + name + "-1", "+1f") + "</label>";
  }
  function button(action, label) {
    return '<button type="button" data-act="' + action + '">' + esc(label) + "</button>";
  }
  function clipInspector(index) {
    var clip = S.doc.clips[index];
    return "<strong>클립 #" + (index + 1) + "</strong>" +
      "<span>초안 " + FE.formatMs(Math.round(clip[0] * fd() * 1000)) + " → " +
      FE.formatMs(Math.round(clip[1] * fd() * 1000)) + "</span>" +
      "<span>길이 " + ((clip[1] - clip[0]) * fd()).toFixed(2) + "초 · " +
      (clip[1] - clip[0]) + "프레임</span>" +
      '<div class="fe-buttons">' + button("delete", "리플 삭제 ⌫") + "</div>";
  }
  function summaryInspector() {
    var removed = FE.removedRanges(S.doc).length;
    return "<strong>최종 편집 · " + esc(S.exportId) + "</strong>" +
      "<span>편집본 " + timecode(total()) + " · 클립 " + S.doc.clips.length + "개</span>" +
      "<span>자막 " + S.proj.cues.length + "개 · 컷에 가려진 자막 " + S.proj.hidden.length +
      "개 · 지운 구간 " + removed + "곳</span>" +
      "<span>심은 SRT " + esc(S.seedSrt || "") + " · 저장 리비전 " + S.baseRevision + "</span>" +
      '<span class="fe-muted">클립이나 자막을 고르면 여기서 고칩니다. 컷을 건너는 재생은' +
      " 잠깐 멈출 수 있습니다(초안 키프레임).</span>";
  }
  function paintToolbar() {
    var bar = $("fe-toolbar");
    if (!bar || !S.doc) { return; }
    var state = S.saving ? "저장 중…" : (S.dirty ? "저장 대기" : "저장됨");
    var exports = (S.edit.exports || []).map(function (row) {
      return '<option value="' + esc(row.id) + '"' + (row.id === S.exportId ? " selected" : "") +
        ">" + esc(row.id) + (row.hasEdit ? " ✎" : "") + "</option>";
    }).join("");
    bar.innerHTML =
      tool("tool-select", "선택 A", S.tool === "select") +
      tool("tool-blade", "블레이드 B", S.tool === "blade") +
      tool("snap", "스냅 N", S.snap) +
      button("zoom-out", "축소 ⌘−") + button("zoom-in", "확대 ⌘=") + button("zoom-fit", "맞춤 ⇧Z") +
      button("undo", "되돌리기 ⌘Z") + button("redo", "다시 ⌘⇧Z") +
      '<span class="fe-gap"></span>' +
      '<select data-act="export" title="초안">' + exports + "</select>" +
      '<span class="fe-save">' + state + "</span>" + button("save", "저장 ⌘S");
  }
  function tool(action, label, on) {
    return '<button type="button" data-act="' + action + '" class="' + (on ? "is-on" : "") + '">' +
      esc(label) + "</button>";
  }
  function paintBanner() {
    var el = $("fe-banner");
    if (!el) { return; }
    var text = "";
    var actions = "";
    if (S.banner === "conflict") {
      text = "다른 화면이 이 초안의 편집을 먼저 저장했습니다.";
      actions = button("load-server", "서버 것 불러오기") +
        button("force-save", "내 것으로 덮어쓰기");
    } else if (S.banner === "stale") {
      text = "작업공간이 바뀌었거나 다시 열렸습니다 — 저장되지 않은 편집은 이 화면에만 있습니다." +
        " 새로고침하세요.";
    } else if (S.banner === "seed") {
      text = "초안 옆에 새 SRT(" + (S.edit.newerSeed ? "SRT만 다시 내보내기" : "") +
        ")가 있습니다. 컷은 그대로 두고 자막만 새로 시작할 수 있습니다.";
      actions = button("reseed", "새 자막으로 다시 시작") + button("dismiss", "닫기");
    } else if (S.banner === "older") {
      text = "이전 초안에도 저장한 편집이 있습니다: " + S.edit.olderEdits.join(", ") +
        " — 위 목록에서 고를 수 있습니다.";
      actions = button("dismiss", "닫기");
    }
    el.hidden = !text;
    el.innerHTML = text ? "<span>" + esc(text) + "</span>" + actions : "";
  }

  // ─── 동작 ────────────────────────────────────────────────────────
  function clipAtPlayhead() { return S.doc.clips.length ? FE.clipIndexAt(S.doc, S.playhead) : -1; }
  function cueAtPlayhead() {
    var index = currentCueIndex();
    return index >= 0 ? S.proj.cues[index][0] : null;
  }
  function targetCue() { return selectedCue() || cueAtPlayhead(); }
  function selectCue(id, seekTo) {
    S.sel = id ? { kind: "cue", ids: [id] } : null;
    var cue = id ? projected(id) : null;
    if (cue && seekTo) { seek(Math.ceil(cue[1] / ft())); }
    paintList(); paintInspector(); invalidate();
  }
  function editPoints() {
    var points = FE.clipOffsets(S.doc);
    points.push(total());
    return points;
  }
  function focusText() {
    var area = $("fe-text");
    if (area) { area.focus(); area.setSelectionRange(area.value.length, area.value.length); }
  }
  function commitText() {
    var area = $("fe-text");
    var id = selectedCue();
    if (!area || !id) { return; }
    var cue = S.doc.cues[FE.cueIndex(S.doc, id)];
    if (cue && area.value !== cue[3]) {
      var value = area.value.replace(/\r/g, "").replace(/\n{2,}/g, "\n").replace(/^\n+|\n+$/g, "");
      apply("자막 글", function (d) { return FE.cueSetText(d, id, value, S.settings); });
    }
  }
  function msPerFrame() { return Math.max(1, Math.round(ft() / S.doc.draft.den)); }
  var ACTIONS = {
    "play-toggle": function () { if (S.playing) { stopPlayback(); } else { play(1); } },
    "stop": function () { stopPlayback(); },
    "shuttle-forward": function () { shuttle(1); },
    "shuttle-reverse": function () { shuttle(-1); },
    "frame-back": function () { stopPlayback(); seek(S.playhead - 1); follow(); },
    "frame-forward": function () { stopPlayback(); seek(S.playhead + 1); follow(); },
    "big-back": function () {
      stopPlayback(); seek(S.playhead - S.settings.bigStepFrames); follow();
    },
    "big-forward": function () {
      stopPlayback(); seek(S.playhead + S.settings.bigStepFrames); follow();
    },
    "prev-edit": function () {
      var points = editPoints().filter(function (p) { return p < S.playhead; });
      if (points.length) { seek(points[points.length - 1]); follow(); }
    },
    "next-edit": function () {
      var points = editPoints().filter(function (p) { return p > S.playhead; });
      if (points.length) { seek(points[0]); follow(); }
    },
    "go-start": function () { seek(0); S.scrollX = 0; invalidate(); },
    "go-end": function () { seek(total()); follow(); },
    "play-selection": function () {
      var cue = selectedCue() ? projected(selectedCue()) : null;
      var from = cue ? Math.floor(cue[1] / ft()) : (S.range ? S.range[0] : null);
      var to = cue ? Math.ceil(cue[2] / ft()) : (S.range ? S.range[1] : null);
      if (from === null) { say("고른 자막이나 범위가 없습니다."); return; }
      seek(from); play(1); S.stopAt = to;
    },
    "mark-in": function () {
      var out = S.range && S.range[1] > S.playhead ? S.range[1] : total();
      S.range = S.playhead < out ? [S.playhead, out] : null; invalidate();
    },
    "mark-out": function () {
      var start = S.range && S.range[0] < S.playhead ? S.range[0] : 0;
      S.range = start < S.playhead ? [start, S.playhead] : null; invalidate();
    },
    "go-in": function () { if (S.range) { seek(S.range[0]); follow(); } },
    "go-out": function () { if (S.range) { seek(S.range[1]); follow(); } },
    "mark-clip": function () {
      var index = clipAtPlayhead();
      if (index < 0) { return; }
      var start = FE.clipOffsets(S.doc)[index];
      S.range = [start, start + S.doc.clips[index][1] - S.doc.clips[index][0]];
      invalidate();
    },
    "clear-range": function () { S.range = null; invalidate(); },
    "select-clip": function () {
      var index = clipAtPlayhead();
      S.sel = index >= 0 ? { kind: "clip", ids: [index] } : null;
      paintInspector(); invalidate();
    },
    "tool-select": function () { S.tool = "select"; paintToolbar(); },
    "tool-blade": function () { S.tool = "blade"; paintToolbar(); },
    "blade": function () {
      apply("자르기", function (d) { return FE.blade(d, S.playhead); });
    },
    "delete": function () { deleteSelection(false); },
    "delete-cues": function () { deleteSelection(true); },
    "trim-start": function () { trimToPlayhead(true); },
    "trim-end": function () { trimToPlayhead(false); },
    "cue-start": function () { setCueEdge(true); },
    "cue-end": function () { setCueEdge(false); },
    "cue-nudge-back": function () { nudgeCue(-1, "nudge"); },
    "cue-nudge-forward": function () { nudgeCue(1, "nudge"); },
    "cue-big-back": function () { nudgeCue(-S.settings.bigStepFrames, "nudge"); },
    "cue-big-forward": function () { nudgeCue(S.settings.bigStepFrames, "nudge"); },
    "cue-insert": function () {
      if (!S.doc.clips.length) { return; }
      var start = FE.draftMsAt(S.doc, phTicks(), FE.SIDE_START);
      var end = Math.min(start + S.settings.newCueMs, FE.totalMs(S.doc));
      var id = "n" + S.doc.nextCue;
      if (apply("자막 넣기", function (d) { return FE.cueInsert(d, start, end, ""); })) {
        selectCue(id, false);
        focusText();
      }
    },
    "cue-split": function () {
      var id = targetCue();
      if (!id || !S.doc.clips.length) { say("나눌 자막이 없습니다."); return; }
      var at = FE.draftMsAt(S.doc, phTicks(), FE.SIDE_START);
      var cue = S.doc.cues[FE.cueIndex(S.doc, id)];
      var parts = FE.splitText(cue[3]);
      apply("자막 나누기", function (d) { return FE.cueSplit(d, id, at, parts[0], parts[1]); });
    },
    "cue-merge": function () {
      var id = targetCue();
      if (id) { apply("자막 합치기", function (d) { return FE.cueMergeNext(d, id); }); }
    },
    "prev-cue": function () { stepCue(-1); },
    "next-cue": function () { stepCue(1); },
    "edit-text": function () {
      if (!selectedCue()) { selectCue(cueAtPlayhead(), false); }
      focusText();
    },
    "commit-text": function () { commitText(); focusTimeline(); },
    "leave-text": function () {
      var active = document.activeElement;
      if (active && active.id === "fe-text") { commitText(); focusTimeline(); return; }
      if (active && active.tagName === "INPUT") { focusTimeline(); return; }
      S.sel = null; S.range = null; paintAll();
    },
    "snap": function () { S.snap = !S.snap; paintToolbar(); say(S.snap ? "스냅 켬" : "스냅 끔"); },
    "zoom-in": function () { zoomBy(S.settings.zoomStepPercent / 100); },
    "zoom-out": function () { zoomBy(100 / S.settings.zoomStepPercent); },
    "zoom-fit": function () { S.pxPerS = fitZoom(); S.scrollX = 0; invalidate(); },
    "undo": function () { undo(false); },
    "redo": function () { undo(true); },
    "save": function () { saveNow(true, false); },
  };
  function deleteSelection(cuesOnly) {
    if (S.range) {
      var range = S.range;
      if (cuesOnly) {
        var a = FE.draftMsAt(S.doc, range[0] * ft(), FE.SIDE_START);
        var b = FE.draftMsAt(S.doc, range[1] * ft(), FE.SIDE_END);
        apply("범위 안 자막 지우기", function (d) { return FE.cuesDeleteInside(d, a, b); });
        return;
      }
      var cut = function (d) { return FE.rippleDelete(d, range[0], range[1]); };
      if (apply("범위 리플 삭제", cut)) {
        S.range = null; S.sel = null; seek(range[0]);
      }
      return;
    }
    var clips = selectedClips();
    if (clips.length && !cuesOnly) {
      if (apply("클립 리플 삭제", function (d) { return FE.deleteClips(d, clips); })) {
        S.sel = null; seek(Math.min(S.playhead, total())); paintInspector();
      }
      return;
    }
    var id = selectedCue();
    if (id && apply("자막 지우기", function (d) { return FE.cueDelete(d, id); })) {
      S.sel = null; paintInspector();
    }
  }
  function trimToPlayhead(front) {
    var index = clipAtPlayhead();
    if (index < 0) { return; }
    var start = FE.clipOffsets(S.doc)[index];
    var end = start + S.doc.clips[index][1] - S.doc.clips[index][0];
    var a = front ? start : S.playhead;
    var b = front ? S.playhead : end;
    if (b <= a) { say("플레이헤드가 클립 끝자리입니다 — 잘라낼 것이 없습니다."); return; }
    if (apply(front ? "클립 앞 잘라내기" : "클립 뒤 잘라내기",
        function (d) { return FE.rippleDelete(d, a, b); })) {
      seek(a);
    }
  }
  function setCueEdge(isStart) {
    var id = targetCue();
    if (!id || !S.doc.clips.length) { say("고른 자막이 없습니다."); return; }
    var cue = S.doc.cues[FE.cueIndex(S.doc, id)];
    var ms = FE.draftMsAt(S.doc, phTicks(), isStart ? FE.SIDE_START : FE.SIDE_END);
    if (isStart ? ms > cue[2] : ms < cue[1]) {
      say("자막 시작이 끝보다 늦어집니다.", true);
      return;
    }
    apply(isStart ? "자막 시작" : "자막 끝", function (d) {
      return FE.cueSetTimes(d, id, isStart ? ms : cue[1], isStart ? cue[2] : ms);
    });
    selectCue(id, false);
  }
  function nudgeCue(frames, key) {
    var id = selectedCue();
    if (!id) { say("고른 자막이 없습니다."); return; }
    apply("자막 밀기", function (d) { return FE.cueShift(d, id, frames * msPerFrame()); },
      key + ":" + id);
  }
  function nudgeEdge(name, frames) {
    var id = selectedCue();
    if (!id) { return; }
    var cue = S.doc.cues[FE.cueIndex(S.doc, id)];
    var delta = frames * msPerFrame();
    var start = name === "start" ? clamp(cue[1] + delta, 0, cue[2]) : cue[1];
    var end = name === "end" ? clamp(cue[2] + delta, cue[1], FE.totalMs(S.doc)) : cue[2];
    apply("자막 " + (name === "start" ? "시작" : "끝"),
      function (d) { return FE.cueSetTimes(d, id, start, end); }, "edge:" + name + ":" + id);
  }
  function stepCue(direction) {
    var t = phTicks();
    var cues = S.proj.cues;
    var pick = null;
    for (var i = 0; i < cues.length; i++) {
      var start = Math.ceil(cues[i][1] / ft());
      if (direction > 0 && start > S.playhead) { pick = cues[i]; break; }
      if (direction < 0 && cues[i][1] < t && start < S.playhead) { pick = cues[i]; }
    }
    if (pick) { selectCue(pick[0], true); follow(); }
  }
  function focusTimeline() { var el = $("fe-timeline"); if (el) { el.focus(); } }
  function run(action) {
    var fn = ACTIONS[action];
    if (fn) { fn(); return; }
    var nudge = /^nudge-(start|end)-(-?1)$/.exec(action);
    if (nudge) { nudgeEdge(nudge[1], parseInt(nudge[2], 10)); return; }
    if (action === "load-server") {
      load({}, function (edit) { S.dirty = false; S.banner = null; adopt(edit, false); });
    } else if (action === "force-save") {
      S.banner = null; saveNow(true, true);
    } else if (action === "reseed") {
      load({ reseed: true }, function (edit) {
        var fresh = function (d) { return FE.replaceCues(d, edit.reseedCues); };
        if (apply("새 자막으로 다시 시작", fresh)) {
          S.seedSrt = edit.reseedSrt;
        }
        S.banner = null; paintBanner();
      });
    } else if (action === "dismiss") {
      S.banner = null; paintBanner();
    }
  }
  W.__vlFinalEditRun = run;

  // ─── 포인터 ──────────────────────────────────────────────────────
  function hitClip(x) {
    var offsets = FE.clipOffsets(S.doc);
    for (var i = 0; i < S.doc.clips.length; i++) {
      var x0 = xOfFrame(offsets[i]);
      var x1 = xOfFrame(offsets[i] + S.doc.clips[i][1] - S.doc.clips[i][0]);
      if (x >= x0 - LAYOUT.edge && x <= x1 + LAYOUT.edge) {
        var edge = Math.abs(x - x0) <= LAYOUT.edge ? "start"
          : (Math.abs(x - x1) <= LAYOUT.edge ? "end" : null);
        if (edge || (x >= x0 && x <= x1)) { return { index: i, edge: edge }; }
      }
    }
    return null;
  }
  function hitCue(x) {
    var chosen = selectedCue();
    var found = null;
    for (var i = 0; i < S.proj.cues.length; i++) {
      var cue = S.proj.cues[i];
      var x0 = xOfTicks(cue[1]);
      var x1 = Math.max(x0 + 2, xOfTicks(cue[2]));
      if (x < x0 - LAYOUT.edge || x > x1 + LAYOUT.edge) { continue; }
      var edge = Math.abs(x - x0) <= LAYOUT.edge ? "start"
        : (Math.abs(x - x1) <= LAYOUT.edge ? "end" : null);
      if (!edge && (x < x0 || x > x1)) { continue; }
      var hit = { id: cue[0], edge: edge, cue: cue };
      if (cue[0] === chosen) { return hit; }
      if (!found) { found = hit; }
    }
    return found;
  }
  function onPointerDown(ev) {
    if (!S.doc || ev.button !== 0) { return; }
    var c = canvas();
    var rect = c.getBoundingClientRect();
    var x = ev.clientX - rect.left;
    var y = ev.clientY - rect.top;
    var L = lanes();
    focusTimeline();
    var drag = { x: x, base: S.doc, kind: "scrub" };
    if (y >= L.clipTop && y < L.clipTop + LAYOUT.clip) {
      drag = clipDown(x, ev);
    } else if (y >= L.waveTop) {
      drag = cueDown(x, ev);
    }
    if (!drag) { return; }
    drag.x = x;
    drag.base = S.doc;
    S.drag = drag;
    if (drag.kind === "scrub") { stopPlayback(); seek(frameAtX(x)); }
    c.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  }
  function clipDown(x, ev) {
    var hit = hitClip(x);
    if (S.tool === "blade") {
      apply("자르기", function (d) { return FE.blade(d, frameAtX(x)); });
      return null;
    }
    if (!hit) { S.sel = null; paintInspector(); invalidate(); return null; }
    if (hit.edge) { return { kind: "clip-" + hit.edge, index: hit.index }; }
    var ids = ev.shiftKey && S.sel && S.sel.kind === "clip" ? S.sel.ids.slice() : [];
    var at = ids.indexOf(hit.index);
    if (at >= 0) { ids.splice(at, 1); } else { ids.push(hit.index); }
    S.sel = ids.length ? { kind: "clip", ids: ids } : null;
    paintInspector(); invalidate();
    return null;
  }
  function cueDown(x, ev) {
    var hit = hitCue(x);
    if (!hit) {
      S.sel = null; paintList(); paintInspector();
      return { kind: "scrub" };
    }
    selectCue(hit.id, false);
    var cue = S.doc.cues[FE.cueIndex(S.doc, hit.id)];
    return { kind: hit.edge ? "cue-" + hit.edge : "cue-move", id: hit.id, cue: cue,
      startTicks: hit.cue[1] };
  }
  function onPointerMove(ev) {
    var drag = S.drag;
    if (!drag) { hover(ev); return; }
    var rect = canvas().getBoundingClientRect();
    var x = ev.clientX - rect.left;
    if (drag.kind === "scrub") { seek(frameAtX(x)); return; }
    var next = null;
    try { next = dragDoc(drag, x); }
    catch (err) { if (!FE.isError(err)) { throw err; } }
    if (next && next !== S.doc) { S.doc = next; refresh(true); }
  }
  function dragDoc(drag, x) {
    var base = drag.base;
    if (drag.kind === "clip-start" || drag.kind === "clip-end") {
      var clip = base.clips[drag.index];
      var delta = Math.round((x - drag.x) / S.pxPerS / fd());
      var lo = drag.kind === "clip-start" ? (drag.index ? base.clips[drag.index - 1][1] : 0)
        : clip[0] + S.settings.minClipFrames;
      var hi = drag.kind === "clip-start" ? clip[1] - S.settings.minClipFrames
        : (base.clips[drag.index + 1] ? base.clips[drag.index + 1][0] : base.draft.totalFrames);
      var edge = drag.kind === "clip-start" ? clip[0] : clip[1];
      var frame = clamp(edge + (drag.kind === "clip-start" ? delta : delta), lo, hi);
      return drag.kind === "clip-start" ? FE.setClipStart(base, drag.index, frame, S.settings)
        : FE.setClipEnd(base, drag.index, frame, S.settings);
    }
    var cue = drag.cue;
    var t = snapTicks(ticksAtX(x), drag.id);
    if (drag.kind === "cue-start") {
      var start = Math.min(FE.draftMsAt(base, t, FE.SIDE_START), cue[2]);
      return FE.cueSetTimes(base, drag.id, start, cue[2]);
    }
    if (drag.kind === "cue-end") {
      var end = Math.max(FE.draftMsAt(base, t, FE.SIDE_END), cue[1]);
      return FE.cueSetTimes(base, drag.id, cue[1], end);
    }
    var moved = snapTicks(clamp(drag.startTicks + ticksAtX(x) - ticksAtX(drag.x), 0,
      total() * ft()), drag.id);
    var length = cue[2] - cue[1];
    var from = clamp(FE.draftMsAt(base, moved, FE.SIDE_START), 0, FE.totalMs(base) - length);
    return FE.cueSetTimes(base, drag.id, from, from + length);
  }
  var DRAG_LABEL = { "clip-start": "클립 앞 트림", "clip-end": "클립 뒤 트림",
    "cue-start": "자막 시작", "cue-end": "자막 끝", "cue-move": "자막 옮기기" };
  function onPointerUp() {
    var drag = S.drag;
    S.drag = null;
    if (!drag || drag.kind === "scrub" || S.doc === drag.base) { return; }
    S.history.record(DRAG_LABEL[drag.kind], drag.base, null, Date.now());
    changed(DRAG_LABEL[drag.kind]);
  }
  function hover(ev) {
    var c = canvas();
    if (!c || !S.doc) { return; }
    var rect = c.getBoundingClientRect();
    var x = ev.clientX - rect.left;
    var y = ev.clientY - rect.top;
    var L = lanes();
    var cursor = "default";
    if (y < L.clipTop) { cursor = "text"; }
    else if (y < L.clipTop + LAYOUT.clip) {
      var clip = hitClip(x);
      cursor = S.tool === "blade" ? "crosshair" : (clip && clip.edge ? "ew-resize" : "pointer");
    } else {
      var cue = hitCue(x);
      cursor = cue ? (cue.edge ? "ew-resize" : "grab") : "text";
    }
    c.style.cursor = cursor;
  }
  function onDoubleClick(ev) {
    if (!S.doc) { return; }
    var rect = canvas().getBoundingClientRect();
    if (ev.clientY - rect.top < lanes().waveTop) { return; }
    var hit = hitCue(ev.clientX - rect.left);
    if (hit) { selectCue(hit.id, false); focusText(); }
  }
  function onWheel(ev) {
    if (!S.doc) { return; }
    ev.preventDefault();
    var rect = canvas().getBoundingClientRect();
    if (ev.ctrlKey || ev.metaKey) {
      zoomBy(ev.deltaY < 0 ? S.settings.zoomStepPercent / 100 : 100 / S.settings.zoomStepPercent,
        ev.clientX - rect.left);
      return;
    }
    S.scrollX += Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
    clampScroll();
    invalidate();
  }

  // ─── DOM 배선 ────────────────────────────────────────────────────
  function bindDom() {
    var v = video();
    var src = S.edit.mediaFileId ? "/media/" + S.edit.mediaFileId : "";
    if (v && v.getAttribute("src") !== src) {
      v.setAttribute("src", src);
      v.addEventListener("loadedmetadata", function () { seek(S.playhead); }, { once: true });
    }
    if (v) {
      v.addEventListener("pause", onVideoPause);
      v.addEventListener("click", onVideoClick);
      v.addEventListener("timeupdate", onTimeUpdate);
    }
    var c = canvas();
    if (c) {
      c.addEventListener("pointerdown", onPointerDown);
      c.addEventListener("pointermove", onPointerMove);
      c.addEventListener("pointerup", onPointerUp);
      c.addEventListener("pointercancel", onPointerUp);
      c.addEventListener("wheel", onWheel, { passive: false });
      c.addEventListener("dblclick", onDoubleClick);
    }
    S.rootEl.addEventListener("click", onClick);
    S.rootEl.addEventListener("change", onChange);
    S.rootEl.addEventListener("focusout", onFocusOut);
    var timeline = $("fe-timeline");
    if (W.ResizeObserver && timeline && !timeline.__feResize) {
      timeline.__feResize = new W.ResizeObserver(onResize);
      timeline.__feResize.observe(timeline);
    }
    paintAll();
  }
  // 이름 붙인 처리기 — 그라디오가 같은 원소를 남긴 채 다시 그려도 두 번 붙지 않는다.
  function onVideoPause() { if (!S.reverseTimer) { S.playing = false; } }
  function onVideoClick() { run("play-toggle"); }
  // 프레임 콜백이 줄어드는 때(숨은 창)에도 컷을 건너뛰도록 한 번 더 본다.
  function onTimeUpdate() {
    var v = video();
    if (S.playing && v && S.doc) { onFrame(Math.floor(v.currentTime / fd())); }
  }
  function onResize() {
    if (!S.doc) { return; }
    if (!S.pxPerS || S.pxPerS < zoomFloor()) { S.pxPerS = fitZoom(); }
    clampScroll();
    invalidate();
  }
  function onClick(ev) {
    var target = ev.target.closest ? ev.target.closest("[data-act],[data-cue]") : null;
    if (!target || target.tagName === "SELECT") { return; }
    var cueId = target.getAttribute("data-cue");
    if (cueId) { selectCue(cueId, true); return; }
    run(target.getAttribute("data-act"));
  }
  function onChange(ev) {
    var target = ev.target;
    if (target.getAttribute("data-act") === "export") {
      var chosen = target.value;
      saveNow(false);
      load({ exportId: chosen }, function (edit) { adopt(edit, false); fetchWave(); });
      return;
    }
    var name = target.getAttribute("data-time");
    var id = selectedCue();
    if (!name || !id) { return; }
    var ms = parseMs(target.value);
    if (ms === null) { say("시각은 00:00:01,250처럼 적습니다.", true); paintInspector(); return; }
    var ticks = clamp(ms * S.doc.draft.den, 0, total() * ft());
    var cue = S.doc.cues[FE.cueIndex(S.doc, id)];
    var draftMs = FE.draftMsAt(S.doc, ticks, name === "start" ? FE.SIDE_START : FE.SIDE_END);
    var start = name === "start" ? draftMs : cue[1];
    var end = name === "end" ? draftMs : cue[2];
    if (end < start) { say("자막 시작이 끝보다 늦어집니다.", true); paintInspector(); return; }
    apply("자막 " + (name === "start" ? "시작" : "끝"),
      function (d) { return FE.cueSetTimes(d, id, start, end); });
  }
  function onFocusOut(ev) {
    if (ev.target && ev.target.id === "fe-text") { commitText(); }
  }

  // ─── 키(문서에 한 번만) ─────────────────────────────────────────
  function editable(target) {
    return !!target && (target.isContentEditable ||
      /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || ""));
  }
  function visible() {
    var rootEl = $("fe-root");
    return !!(rootEl && rootEl.offsetParent !== null && S.doc && !S.empty &&
      !rootEl.classList.contains("fe-empty"));
  }
  if (!W.__vlFinalEditKeysBound) {
    W.__vlFinalEditKeysBound = true;
    document.addEventListener("keydown", function (ev) {
      if (!visible() || document.querySelector(".sidebar.open")) { return; }
      var target = ev.target;
      var inText = editable(target);
      if (inText && !$("fe-root").contains(target)) { return; }
      var binding = FE.keyAction(W.__vlFinalEdit.keymap || [], ev,
        { mac: IS_MAC, inText: inText });
      if (!binding) { return; }
      ev.preventDefault();
      ev.stopPropagation();
      W.__vlFinalEditRun(binding.action);
    }, true);
    document.addEventListener("keyup", function (ev) {
      if (ev.code === "Space" && visible() && !editable(ev.target)) { ev.preventDefault(); }
    }, true);
    W.addEventListener("beforeunload", function (ev) {
      if (W.__vlFinalEdit && W.__vlFinalEdit.dirty) { ev.preventDefault(); ev.returnValue = ""; }
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden" && W.__vlFinalEdit.dirty) { saveNow(false); }
    });
  }

  boot();
  if (!element.__vlFinalEditObserver && W.MutationObserver) {
    element.__vlFinalEditObserver = new W.MutationObserver(function () { boot(); });
    element.__vlFinalEditObserver.observe(element, { childList: true, subtree: true,
      characterData: true });
  }
  if (typeof watch === "function") { watch("value", function () { boot(); }); }
})();
"""
