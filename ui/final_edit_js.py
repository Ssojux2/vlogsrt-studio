"""최종 편집 탭(W-66, D117)의 브라우저 실행본 — 두 층.

`CORE_JS`는 DOM을 모른다: 순수 모델(`vlogsrt/project/raw/final_edit.py`)의 정수 틱 규칙을 1:1로
옮긴 것과 되돌리기 기록·키 조회·파형 축소다. 노드에서도 돌아(`module.exports`) 시험이 공용 사례
(`tests/fixtures/synthetic/final_edit_cases.json`)로 Python과 정확히 같은 값을 확인한다. 서버는
저장마다 문서 전체를 다시 검사하므로 이 층이 틀려도 틀린 문서가 저장되지는 않는다.

`APP_JS`는 화면이다(`ui/final_edit_app_js.py`): 목록·재생기·인스펙터·타임라인 캔버스·단축키·
자동 저장.
숫자는 모두 서버가 모델로 보낸 설정에서 온다(규칙 8).
"""

from __future__ import annotations

CORE_JS = r"""
(function (root) {
  "use strict";
  var FE = {};
  var SIDE_START = "start";
  var SIDE_END = "end";
  FE.SIDE_START = SIDE_START;
  FE.SIDE_END = SIDE_END;

  function fail(message) {
    var error = new Error(message);
    error.feError = true;
    return error;
  }
  FE.isError = function (error) { return !!(error && error.feError); };

  function floorDiv(a, b) { return Math.floor(a / b); }
  function chars(text) { return Array.from(String(text)); }

  // ─── 틱 ──────────────────────────────────────────────────────────
  FE.tickRate = function (doc) { return 1000 * doc.draft.den; };
  FE.frameTicks = function (doc) { return 1000 * doc.draft.num; };
  FE.msTicks = function (doc) { return doc.draft.den; };
  FE.totalMs = function (doc) {
    var whole = doc.draft.totalFrames * FE.frameTicks(doc);
    return -floorDiv(-whole, FE.msTicks(doc));
  };
  function msHalfDown(ticks, unit) { return -floorDiv(unit - 2 * ticks, 2 * unit); }
  FE.msHalfDown = msHalfDown;
  FE.ticksToMsHalfUp = function (doc, ticks) {
    var den = doc.draft.den;
    return floorDiv(2 * ticks + den, 2 * den);
  };

  // ─── 투영 ────────────────────────────────────────────────────────
  function spans(doc) {
    var unit = FE.frameTicks(doc);
    var out = [];
    var offset = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      var start = doc.clips[i][0] * unit;
      var end = doc.clips[i][1] * unit;
      out.push([start, end, offset]);
      offset += end - start;
    }
    return out;
  }
  FE.spans = spans;
  FE.editedFrames = function (doc) {
    var total = 0;
    for (var i = 0; i < doc.clips.length; i++) { total += doc.clips[i][1] - doc.clips[i][0]; }
    return total;
  };
  FE.clipOffsets = function (doc) {
    var out = [];
    var offset = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      out.push(offset);
      offset += doc.clips[i][1] - doc.clips[i][0];
    }
    return out;
  };
  FE.draftFrameAt = function (doc, edited) {
    if (!doc.clips.length || edited < 0 || edited > FE.editedFrames(doc)) {
      throw fail("편집 프레임이 편집본 밖이다");
    }
    var offset = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      var length = doc.clips[i][1] - doc.clips[i][0];
      if (edited < offset + length) { return doc.clips[i][0] + edited - offset; }
      offset += length;
    }
    return doc.clips[doc.clips.length - 1][1];
  };
  FE.editedFrameOf = function (doc, draftFrame) {
    var offset = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      var clip = doc.clips[i];
      if (clip[0] <= draftFrame && draftFrame < clip[1]) { return offset + draftFrame - clip[0]; }
      offset += clip[1] - clip[0];
    }
    return null;
  };
  FE.clipIndexAt = function (doc, edited) {
    var offset = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      var length = doc.clips[i][1] - doc.clips[i][0];
      if (edited < offset + length) { return i; }
      offset += length;
    }
    return doc.clips.length ? doc.clips.length - 1 : -1;
  };
  FE.editedTicks = function (doc, draftMs) {
    var point = draftMs * FE.msTicks(doc);
    var list = spans(doc);
    for (var i = 0; i < list.length; i++) {
      if (point < list[i][0]) { return list[i][2]; }
      if (point <= list[i][1]) { return list[i][2] + point - list[i][0]; }
    }
    return FE.editedFrames(doc) * FE.frameTicks(doc);
  };
  function spanAt(list, ticks, side) {
    var last = list[list.length - 1];
    var total = last[2] + last[1] - last[0];
    if (ticks < 0 || ticks > total) { throw fail("편집 시각이 편집본 밖이다"); }
    for (var i = 0; i < list.length; i++) {
      var length = list[i][1] - list[i][0];
      var offset = list[i][2];
      if (side === SIDE_START && offset <= ticks && ticks < offset + length) { return list[i]; }
      if (side === SIDE_END && offset < ticks && ticks <= offset + length) { return list[i]; }
    }
    return side === SIDE_START ? last : list[0];
  }
  FE.draftMsAt = function (doc, ticks, side) {
    var list = spans(doc);
    if (!list.length) { throw fail("클립이 없다"); }
    var span = spanAt(list, ticks, side);
    return msHalfDown(span[0] + ticks - span[2], FE.msTicks(doc));
  };
  function keptMeasure(list, start, end) {
    var total = 0;
    for (var i = 0; i < list.length; i++) {
      total += Math.max(0, Math.min(end, list[i][1]) - Math.max(start, list[i][0]));
    }
    return total;
  }
  function pointKept(list, point) {
    for (var i = 0; i < list.length; i++) {
      if (list[i][0] <= point && point <= list[i][1]) { return true; }
    }
    return false;
  }
  // 보이는 자막: [id, 편집 시작 틱, 편집 끝 틱, 글, 잘림]
  FE.project = function (doc) {
    var unit = FE.msTicks(doc);
    var list = spans(doc);
    var visible = [];
    var hidden = [];
    for (var i = 0; i < doc.cues.length; i++) {
      var cue = doc.cues[i];
      var start = cue[1] * unit;
      var end = cue[2] * unit;
      var kept = keptMeasure(list, start, end);
      if (kept === 0 && !(start === end && pointKept(list, start))) {
        hidden.push(cue[0]);
        continue;
      }
      visible.push([cue[0], FE.editedTicks(doc, cue[1]), FE.editedTicks(doc, cue[2]), cue[3],
        kept < end - start]);
    }
    return { cues: visible, hidden: hidden, totalFrames: FE.editedFrames(doc) };
  };
  function pad(value, width) {
    var text = String(value);
    while (text.length < width) { text = "0" + text; }
    return text;
  }
  FE.formatMs = function (ms) {
    var hours = floorDiv(ms, 3600000);
    var rest = ms - hours * 3600000;
    var minutes = floorDiv(rest, 60000);
    rest -= minutes * 60000;
    var seconds = floorDiv(rest, 1000);
    return pad(hours, 2) + ":" + pad(minutes, 2) + ":" + pad(seconds, 2) + "," +
      pad(rest - seconds * 1000, 3);
  };
  FE.srtText = function (doc, projection) {
    var blocks = [];
    for (var i = 0; i < projection.cues.length; i++) {
      var cue = projection.cues[i];
      var line = FE.formatMs(FE.ticksToMsHalfUp(doc, cue[1])) + " --> " +
        FE.formatMs(FE.ticksToMsHalfUp(doc, cue[2]));
      blocks.push((i + 1) + "\n" + line + "\n" + cue[3] + "\n");
    }
    return blocks.join("\n");
  };
  FE.removedRanges = function (doc) {
    var gaps = [];
    var cursor = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      if (doc.clips[i][0] > cursor) { gaps.push([cursor, doc.clips[i][0]]); }
      cursor = doc.clips[i][1];
    }
    if (cursor < doc.draft.totalFrames) { gaps.push([cursor, doc.draft.totalFrames]); }
    return gaps;
  };
  FE.textChars = function (text) {
    var count = 0;
    var list = chars(text);
    for (var i = 0; i < list.length; i++) {
      if (list[i] !== " " && list[i] !== "\n") { count += 1; }
    }
    return count;
  };
  function cueWarnings(cue, next, doc, settings) {
    var rate = FE.tickRate(doc);
    var unit = doc.draft.den;
    var span = cue[2] - cue[1];
    var count = FE.textChars(cue[3]);
    var kinds = [];
    if (count === 0) { kinds.push("empty"); }
    if (span === 0) { kinds.push("zero"); }
    else if (span < settings.minDurationMs * unit) { kinds.push("short"); }
    if (span > settings.maxDurationMs * unit) { kinds.push("long"); }
    if (span > 0 && count * rate > settings.maxCps * span) { kinds.push("fast"); }
    if (next) {
      var gap = next[1] - cue[2];
      if (gap < 0) { kinds.push("overlap"); }
      else if (gap > 0 && gap < settings.minGapMs * unit) { kinds.push("gap"); }
    }
    return kinds;
  }
  FE.warnings = function (doc, settings, projection) {
    var cues = (projection || FE.project(doc)).cues;
    var found = [];
    for (var i = 0; i < cues.length; i++) {
      var kinds = cueWarnings(cues[i], cues[i + 1] || null, doc, settings);
      for (var k = 0; k < kinds.length; k++) { found.push([cues[i][0], kinds[k]]); }
    }
    return found;
  };

  // ─── 클립 연산 ───────────────────────────────────────────────────
  function withClips(doc, clips) {
    return { draft: doc.draft, clips: clips, cues: doc.cues, nextCue: doc.nextCue };
  }
  FE.blade = function (doc, edited) {
    var offset = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      var clip = doc.clips[i];
      var length = clip[1] - clip[0];
      if (offset < edited && edited < offset + length) {
        var cut = clip[0] + edited - offset;
        var clips = doc.clips.slice(0, i).concat([[clip[0], cut], [cut, clip[1]]],
          doc.clips.slice(i + 1));
        return withClips(doc, clips);
      }
      offset += length;
    }
    throw fail("편집 프레임이 클립 안이 아니다(편집 점 위거나 밖이다)");
  };
  FE.rippleDelete = function (doc, start, end) {
    if (!(0 <= start && start < end && end <= FE.editedFrames(doc))) {
      throw fail("지울 범위가 편집본 밖이거나 비었다");
    }
    var kept = [];
    var offset = 0;
    for (var i = 0; i < doc.clips.length; i++) {
      var clip = doc.clips[i];
      var length = clip[1] - clip[0];
      var head = Math.min(Math.max(start - offset, 0), length);
      var tail = Math.min(Math.max(end - offset, 0), length);
      if (head > 0) { kept.push([clip[0], clip[0] + head]); }
      if (tail < length) { kept.push([clip[0] + tail, clip[1]]); }
      offset += length;
    }
    return withClips(doc, kept);
  };
  FE.deleteClips = function (doc, indexes) {
    var chosen = {};
    var count = 0;
    for (var i = 0; i < indexes.length; i++) {
      if (indexes[i] < 0 || indexes[i] >= doc.clips.length) {
        throw fail("지울 클립 번호가 틀렸다");
      }
      if (!chosen[indexes[i]]) { chosen[indexes[i]] = true; count += 1; }
    }
    if (!count) { throw fail("지울 클립 번호가 틀렸다"); }
    return withClips(doc, doc.clips.filter(function (clip, index) { return !chosen[index]; }));
  };
  function clipAt(doc, index) {
    if (index < 0 || index >= doc.clips.length) { throw fail("클립 번호가 없다"); }
    return doc.clips[index];
  }
  function replaceClip(doc, index, clip) {
    var clips = doc.clips.slice();
    clips[index] = clip;
    return withClips(doc, clips);
  }
  FE.setClipStart = function (doc, index, frame, settings) {
    var clip = clipAt(doc, index);
    var floor = index > 0 ? doc.clips[index - 1][1] : 0;
    if (!(floor <= frame && frame <= clip[1] - settings.minClipFrames)) {
      throw fail("클립 시작이 허용 범위 밖이다");
    }
    return replaceClip(doc, index, [frame, clip[1]]);
  };
  FE.setClipEnd = function (doc, index, frame, settings) {
    var clip = clipAt(doc, index);
    var ceiling = index + 1 < doc.clips.length ? doc.clips[index + 1][0] : doc.draft.totalFrames;
    if (!(clip[0] + settings.minClipFrames <= frame && frame <= ceiling)) {
      throw fail("클립 끝이 허용 범위 밖이다");
    }
    return replaceClip(doc, index, [clip[0], frame]);
  };
  FE.joinClips = function (doc, index) {
    var clip = clipAt(doc, index);
    var next = clipAt(doc, index + 1);
    if (clip[1] !== next[0]) { throw fail("사이에 지운 구간이 있어 합칠 수 없다"); }
    return withClips(doc, doc.clips.slice(0, index).concat([[clip[0], next[1]]],
      doc.clips.slice(index + 2)));
  };

  // ─── 자막 연산(초안 밀리초) ──────────────────────────────────────
  function compareCues(a, b) {
    if (a[1] !== b[1]) { return a[1] - b[1]; }
    if (a[2] !== b[2]) { return a[2] - b[2]; }
    return a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0);
  }
  function withCues(doc, cues, nextCue) {
    var sorted = cues.slice().sort(compareCues);
    return { draft: doc.draft, clips: doc.clips, cues: sorted,
      nextCue: nextCue === undefined ? doc.nextCue : nextCue };
  }
  function cueIndex(doc, id) {
    for (var i = 0; i < doc.cues.length; i++) { if (doc.cues[i][0] === id) { return i; } }
    throw fail("자막이 없다: " + id);
  }
  FE.cueIndex = function (doc, id) {
    for (var i = 0; i < doc.cues.length; i++) { if (doc.cues[i][0] === id) { return i; } }
    return -1;
  };
  function checkTimes(doc, start, end) {
    if (!(0 <= start && start <= end && end <= FE.totalMs(doc))) {
      throw fail("자막 시각이 초안 밖이거나 뒤집혔다");
    }
  }
  function checkText(text, settings) {
    if (chars(text).length > settings.maxTextChars) { throw fail("자막 글이 너무 길다"); }
    if (text.indexOf("\r") >= 0 || text.indexOf("\n\n") >= 0 || text.charAt(0) === "\n" ||
        text.charAt(text.length - 1) === "\n") {
      throw fail("자막 글에 빈 줄이나 \\r이 있다");
    }
  }
  FE.checkText = checkText;
  function replaceCue(doc, index, cue) {
    var cues = doc.cues.slice();
    cues[index] = cue;
    return withCues(doc, cues);
  }
  FE.cueSetTimes = function (doc, id, start, end) {
    checkTimes(doc, start, end);
    var index = cueIndex(doc, id);
    var cue = doc.cues[index];
    return replaceCue(doc, index, [cue[0], start, end, cue[3]]);
  };
  FE.cueShift = function (doc, id, delta) {
    var cue = doc.cues[cueIndex(doc, id)];
    var moved = Math.max(delta, -cue[1]);
    moved = Math.min(moved, FE.totalMs(doc) - cue[2]);
    return FE.cueSetTimes(doc, id, cue[1] + moved, cue[2] + moved);
  };
  FE.cueSetText = function (doc, id, text, settings) {
    var index = cueIndex(doc, id);
    checkText(text, settings);
    var cue = doc.cues[index];
    return replaceCue(doc, index, [cue[0], cue[1], cue[2], text]);
  };
  FE.cueDelete = function (doc, id) {
    var index = cueIndex(doc, id);
    return withCues(doc, doc.cues.slice(0, index).concat(doc.cues.slice(index + 1)));
  };
  FE.cuesDeleteInside = function (doc, start, end) {
    checkTimes(doc, start, end);
    var kept = doc.cues.filter(function (cue) { return !(start <= cue[1] && cue[2] <= end); });
    if (kept.length === doc.cues.length) { throw fail("범위 안에 다 들어간 자막이 없다"); }
    return withCues(doc, kept);
  };
  FE.splitText = function (text) {
    var newline = text.indexOf("\n");
    if (newline >= 0) { return [text.slice(0, newline), text.slice(newline + 1)]; }
    var list = chars(text);
    var middle = list.length / 2;
    var best = -1;
    for (var i = 0; i < list.length; i++) {
      if (list[i] !== " ") { continue; }
      if (best < 0 || Math.abs(i - middle) < Math.abs(best - middle)) { best = i; }
    }
    if (best < 0) { return [text, ""]; }
    return [list.slice(0, best).join(""), list.slice(best + 1).join("")];
  };
  FE.cueSplit = function (doc, id, at, left, right) {
    var index = cueIndex(doc, id);
    var cue = doc.cues[index];
    if (!(cue[1] < at && at < cue[2])) { throw fail("나눌 시각이 자막 안이 아니다"); }
    var cues = doc.cues.slice(0, index).concat([[cue[0], cue[1], at, left],
      ["n" + doc.nextCue, at, cue[2], right]], doc.cues.slice(index + 1));
    return withCues(doc, cues, doc.nextCue + 1);
  };
  FE.cueMergeNext = function (doc, id) {
    var visible = FE.project(doc).cues.map(function (cue) { return cue[0]; });
    var at = visible.indexOf(id);
    if (at < 0 || at + 1 >= visible.length) { throw fail("다음에 합칠 보이는 자막이 없다"); }
    var first = doc.cues[cueIndex(doc, id)];
    var second = doc.cues[cueIndex(doc, visible[at + 1])];
    var text = [first[3], second[3]].filter(function (part) { return part; }).join(" ");
    var merged = [first[0], Math.min(first[1], second[1]), Math.max(first[2], second[2]), text];
    var cues = [];
    for (var i = 0; i < doc.cues.length; i++) {
      if (doc.cues[i] === second) { continue; }
      cues.push(doc.cues[i] === first ? merged : doc.cues[i]);
    }
    return withCues(doc, cues);
  };
  FE.cueInsert = function (doc, start, end, text) {
    checkTimes(doc, start, end);
    var cue = ["n" + doc.nextCue, start, end, text];
    return withCues(doc, doc.cues.concat([cue]), doc.nextCue + 1);
  };
  FE.replaceCues = function (doc, cues) { return withCues(doc, cues); };

  // ─── 되돌리기 기록 ───────────────────────────────────────────────
  // 기록은 연산 **앞**의 문서를 쌓는다. 같은 열쇠의 미세 조정이 `coalesceMs` 안에 이어지면 한 칸.
  FE.History = function (limit, coalesceMs) {
    this.limit = limit;
    this.coalesceMs = coalesceMs;
    this.past = [];
    this.future = [];
    this.key = null;
    this.at = 0;
  };
  FE.History.prototype.record = function (label, before, key, now) {
    this.future = [];
    if (key && key === this.key && now - this.at <= this.coalesceMs) {
      this.at = now;
      return false;
    }
    this.past.push({ label: label, doc: before });
    if (this.past.length > this.limit) { this.past.shift(); }
    this.key = key || null;
    this.at = now;
    return true;
  };
  FE.History.prototype.undo = function (current) {
    if (!this.past.length) { return null; }
    var entry = this.past.pop();
    this.future.push({ label: entry.label, doc: current });
    this.key = null;
    return entry;
  };
  FE.History.prototype.redo = function (current) {
    if (!this.future.length) { return null; }
    var entry = this.future.pop();
    this.past.push({ label: entry.label, doc: current });
    this.key = null;
    return entry;
  };

  // ─── 키 ──────────────────────────────────────────────────────────
  // ctx: {mac, inText}. IME 조합 중(keyCode 229) · 허락 없는 반복 키 · 글 칸 안(inText만)을 거른다.
  FE.keyAction = function (keymap, ev, ctx) {
    if (ev.isComposing || ev.keyCode === 229) { return null; }
    var mod = ctx.mac ? !!ev.metaKey : !!ev.ctrlKey;
    var other = ctx.mac ? !!ev.ctrlKey : !!ev.metaKey;
    if (other) { return null; }
    for (var i = 0; i < keymap.length; i++) {
      var b = keymap[i];
      if (b.code !== ev.code || !!b.mod !== mod || !!b.shift !== !!ev.shiftKey ||
          !!b.alt !== !!ev.altKey) { continue; }
      if (ev.repeat && !b.repeat) { return null; }
      if (ctx.inText && !b.inText) { return null; }
      return b;
    }
    return null;
  };

  // ─── 파형 ────────────────────────────────────────────────────────
  FE.mipLevels = function (peaks) {
    var levels = [peaks];
    var current = peaks;
    while (current.length > 1) {
      var next = [];
      for (var i = 0; i < current.length; i += 2) {
        next.push(Math.max(current[i], i + 1 < current.length ? current[i + 1] : 0));
      }
      levels.push(next);
      current = next;
    }
    return levels;
  };
  FE.columnPeak = function (levels, binStart, binEnd) {
    var start = Math.max(0, Math.floor(binStart));
    var end = Math.min(levels[0].length, Math.ceil(binEnd));
    var peak = 0;
    var i = start;
    // 정확한 최댓값 — 맞물린 가장 큰 칸부터 집는다(칸 수는 로그).
    while (i < end) {
      var level = 0;
      while (level + 1 < levels.length && i % (1 << (level + 1)) === 0 &&
             i + (1 << (level + 1)) <= end) { level += 1; }
      var value = levels[level][i >> level];
      if (value > peak) { peak = value; }
      i += 1 << level;
    }
    return peak;
  };

  // 휠 줌 배율 — 픽셀 단위면 델타에 비례(트랙패드 핀치는 작은 델타가 잦다), 한 사건은 한 걸음까지.
  FE.wheelZoomFactor = function (deltaY, deltaMode, settings) {
    var step = settings.zoomStepPercent / 100;
    if (!deltaY) { return 1; }
    if (deltaMode !== 0) { return deltaY < 0 ? step : 1 / step; }
    var steps = Math.max(-1, Math.min(1, -deltaY / settings.wheelZoomNotchPx));
    return Math.pow(step, steps);
  };

  root.__vlFE = FE;
  if (typeof module !== "undefined" && module.exports) { module.exports = FE; }
})(typeof window !== "undefined" ? window : globalThis);
"""
