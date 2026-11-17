"""원본 범위 타임라인(W-71a, D125)의 브라우저 핵심 — `TIMELINE_CORE_JS`(`window.__vlTL`).

순수 모델(`vlogsrt/project/raw/timeline_edit.py`)의 정수 틱 규칙을 1:1로 옮긴다. DOM을 모르고
노드에서도 돈다(`module.exports`) — 시험이 공용 사례(`timeline_edit_cases.json`)로
Python과 정확히 같은 값을 확인한다. 서버는 저장마다 문서 전체를 다시 검사하므로 이 층이 틀려도 틀린
문서가 저장되지는 않는다. D117의 `CORE_JS`(`window.__vlFE`, 초안 위의 편집)와는 따로 산다.

문서 모양: `{facts: {sequence: {num, den}, sources: [{id, num, den, frames}]}, clips: [[원본, 시작,
끝]], cues: [[아이디, 원본, 시작 ms, 끝 ms, 글, 원본 줄]], nextCue, seeded: {원본: 리비전}}`.
숫자 기준(최소 클립·글 길이·자막 경고)은 서버가 보낸 설정에서 온다(규칙 8).
"""

from __future__ import annotations

TIMELINE_CORE_JS = r"""
(function (root) {
  "use strict";
  var TL = {};
  var SIDE_START = "start";
  var SIDE_END = "end";
  var EXACT_TICK_LIMIT = 9007199254740992;  // 2^53 — Python `EXACT_TICK_LIMIT`과 같다
  TL.SIDE_START = SIDE_START;
  TL.SIDE_END = SIDE_END;

  function fail(message) {
    var error = new Error(message);
    error.tlError = true;
    return error;
  }
  TL.isError = function (error) { return !!(error && error.tlError); };
  function floorDiv(a, b) { return Math.floor(a / b); }
  function gcd(a, b) { while (b) { var t = a % b; a = b; b = t; } return a; }
  function lcm(a, b) { return a / gcd(a, b) * b; }
  function chars(text) { return Array.from(String(text)); }

  // ─── 틱 ──────────────────────────────────────────────────────────
  TL.tickRate = function (facts) {
    var l = facts.sequence.den;
    for (var i = 0; i < facts.sources.length; i++) { l = lcm(l, facts.sources[i].den); }
    return 1000 * l;
  };
  TL.sequenceFrameTicks = function (facts) {
    return facts.sequence.num * TL.tickRate(facts) / facts.sequence.den;
  };
  function source(facts, id) {
    for (var i = 0; i < facts.sources.length; i++) {
      if (facts.sources[i].id === id) { return facts.sources[i]; }
    }
    throw fail("원본이 타임라인 사실에 없다: " + id);
  }
  TL.source = source;
  TL.sourceFrameTicks = function (facts, id) {
    var s = source(facts, id);
    return s.num * TL.tickRate(facts) / s.den;
  };
  TL.msTicks = function (facts) { return TL.tickRate(facts) / 1000; };
  TL.sourceTotalMs = function (s) { return -floorDiv(-(s.frames * s.num * 1000), s.den); };
  function sourceOrder(facts) {
    var order = {};
    for (var i = 0; i < facts.sources.length; i++) { order[facts.sources[i].id] = i; }
    return order;
  }
  function msHalfDown(ticks, unit) { return -floorDiv(unit - 2 * ticks, 2 * unit); }

  // ─── 검사 ────────────────────────────────────────────────────────
  TL.validCueId = function (id) {
    var head = id.charAt(0);
    var number = id.slice(1);
    return (head === "c" || head === "n") && /^[1-9][0-9]*$/.test(number);
  };
  function validateFacts(facts) {
    if (!(facts.sequence.num > 0) || !facts.sources.length) {
      throw fail("시퀀스 프레임 길이가 양수가 아니거나 원본이 없다");
    }
    if (Object.keys(sourceOrder(facts)).length !== facts.sources.length) {
      throw fail("원본 아이디가 겹친다");
    }
    var rate = TL.tickRate(facts);
    facts.sources.forEach(function (s) {
      if (!(s.num > 0) || !(s.frames > 0)) { throw fail("원본 격자가 틀렸다: " + s.id); }
      if (s.frames * s.num * rate / s.den >= EXACT_TICK_LIMIT) {
        throw fail("원본이 너무 길어 틱을 정확히 셀 수 없다: " + s.id);
      }
    });
  }
  function validateClips(doc) {
    var finished = {};
    var previous = null;
    doc.clips.forEach(function (clip) {
      var s = source(doc.facts, clip[0]);
      if (!(0 <= clip[1] && clip[1] < clip[2] && clip[2] <= s.frames)) {
        throw fail("클립이 원본 안의 빈 아닌 구간이 아니다");
      }
      if (previous && previous[0] === clip[0]) {
        if (clip[1] < previous[2]) { throw fail("같은 원본의 클립이 겹치거나 거꾸로 간다"); }
      } else if (finished[clip[0]]) {
        throw fail("같은 원본의 클립이 타임라인에서 떨어져 있다");
      }
      if (previous && previous[0] !== clip[0]) { finished[previous[0]] = true; }
      previous = clip;
    });
  }
  function cueKey(order) {
    return function (a, b) {
      var oa = order[a[1]] === undefined ? -1 : order[a[1]];
      var ob = order[b[1]] === undefined ? -1 : order[b[1]];
      if (oa !== ob) { return oa - ob; }
      if (a[2] !== b[2]) { return a[2] - b[2]; }
      if (a[3] !== b[3]) { return a[3] - b[3]; }
      return a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0);
    };
  }
  function sortedCues(facts, cues) { return cues.slice().sort(cueKey(sourceOrder(facts))); }
  function validateText(text, settings) {
    if (chars(text).length > settings.maxTextChars) { throw fail("자막 글이 너무 길다"); }
    if (text.indexOf("\r") >= 0 || text.indexOf("\n\n") >= 0 || text.charAt(0) === "\n" ||
        text.charAt(text.length - 1) === "\n") {
      throw fail("자막 글에 빈 줄이나 \\r이 있다");
    }
  }
  TL.validateText = validateText;
  function validateCues(doc, settings) {
    if (doc.cues.length > settings.maxCues) { throw fail("자막이 너무 많다"); }
    var seen = {};
    doc.cues.forEach(function (cue) {
      if (!TL.validCueId(cue[0]) || seen[cue[0]]) { throw fail("자막 아이디가 틀렸거나 겹친다"); }
      seen[cue[0]] = true;
      var limit = TL.sourceTotalMs(source(doc.facts, cue[1]));
      if (!(0 <= cue[2] && cue[2] <= cue[3] && cue[3] <= limit)) {
        throw fail("자막 시각이 원본 밖이다");
      }
      validateText(cue[4], settings);
    });
    var sorted = sortedCues(doc.facts, doc.cues);
    for (var i = 0; i < sorted.length; i++) {
      if (sorted[i] !== doc.cues[i]) { throw fail("자막이 원본·시각 차례로 놓이지 않았다"); }
    }
  }
  TL.validate = function (doc, settings) {
    validateFacts(doc.facts);
    validateClips(doc);
    if (TL.editedFrames(doc) * TL.sequenceFrameTicks(doc.facts) >= EXACT_TICK_LIMIT) {
      throw fail("타임라인이 너무 길어 틱을 정확히 셀 수 없다");
    }
    validateCues(doc, settings);
    if (!(doc.nextCue >= 1)) { throw fail("다음 자막 번호가 1보다 작다"); }
    var order = sourceOrder(doc.facts);
    Object.keys(doc.seeded || {}).forEach(function (id) {
      if (order[id] === undefined) { throw fail("심은 리비전의 원본이 사실에 없다"); }
    });
  };

  // ─── 배치 — 렌더 계획 `sequence_edges`와 같은 규칙 ─────────────────
  TL.layout = function (doc) {
    var seq = TL.sequenceFrameTicks(doc.facts);
    var edges = [0];
    var total = 0;
    var spans = [];
    doc.clips.forEach(function (clip, i) {
      var s = source(doc.facts, clip[0]);
      var unit = TL.sourceFrameTicks(doc.facts, clip[0]);
      var length = (clip[2] - clip[1]) * unit;
      total += length;
      var edge = floorDiv(2 * total + seq, 2 * seq);
      var last = edges[edges.length - 1];
      if (clip[2] === s.frames) { edge = Math.min(edge, last + floorDiv(length, seq)); }
      edges.push(Math.max(edge, last + 1));
      spans.push({ index: i, sourceId: clip[0], startFrame: clip[1], endFrame: clip[2],
        outStart: edges[i], outFrames: 0 });
    });
    spans.forEach(function (span, i) { span.outFrames = edges[i + 1] - edges[i]; });
    return spans;
  };
  TL.editedFrames = function (doc) {
    var spans = TL.layout(doc);
    if (!spans.length) { return 0; }
    var last = spans[spans.length - 1];
    return last.outStart + last.outFrames;
  };
  function offsetFrames(doc, span, editedFrame) {
    var ticks = (editedFrame - span.outStart) * TL.sequenceFrameTicks(doc.facts);
    return floorDiv(ticks, TL.sourceFrameTicks(doc.facts, span.sourceId));
  }
  TL.sourcePoint = function (doc, editedFrame) {
    var spans = TL.layout(doc);
    for (var i = 0; i < spans.length; i++) {
      var span = spans[i];
      if (span.outStart <= editedFrame && editedFrame < span.outStart + span.outFrames) {
        var frame = span.startFrame + offsetFrames(doc, span, editedFrame);
        return [i, Math.min(frame, span.endFrame - 1)];
      }
    }
    throw fail("타임라인 프레임이 타임라인 밖이다");
  };
  function tickSpans(doc) {
    var seq = TL.sequenceFrameTicks(doc.facts);
    return TL.layout(doc).map(function (span) {
      var unit = TL.sourceFrameTicks(doc.facts, span.sourceId);
      return [span.sourceId, span.startFrame * unit, span.endFrame * unit, span.outStart * seq,
        (span.outStart + span.outFrames) * seq];
    });
  }
  TL.tickSpans = tickSpans;
  function mappedEnd(row) { return Math.min(row[3] + row[2] - row[1], row[4]); }
  // 렌더 계획 `_piece`와 같은 규칙 — 뺀 구간의 끝은 앞 클립의 원본 끝을 민 자리다.
  TL.editedTicks = function (doc, sourceId, ms, side) {
    return editedIn(tickSpans(doc), TL.msTicks(doc.facts), sourceId, ms, side);
  };
  // `editedTicks`의 몸 — 자막 여럿을 옮기는 쪽(`project`)이 클립 틱을 한 번만 잰다.
  function editedIn(rows, unit, sourceId, ms, side) {
    var point = ms * unit;
    var mine = rows.filter(function (row) { return row[0] === sourceId; });
    if (!mine.length) { throw fail("원본의 클립이 타임라인에 없다"); }
    for (var i = 0; i < mine.length; i++) {
      var row = mine[i];
      if (point < row[1]) {
        return side === SIDE_END && i > 0 ? mappedEnd(mine[i - 1]) : row[3];
      }
      if (point <= row[2]) { return Math.min(row[3] + point - row[1], row[4]); }
    }
    return mappedEnd(mine[mine.length - 1]);
  }
  TL.sourceMsAt = function (doc, ticks, side) {
    var rows = tickSpans(doc);
    if (!rows.length || !(0 <= ticks && ticks <= rows[rows.length - 1][4])) {
      throw fail("타임라인 시각이 타임라인 밖이다");
    }
    var chosen = side === SIDE_START ? rows[rows.length - 1] : rows[0];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      if (side === SIDE_START && row[3] <= ticks && ticks < row[4]) { chosen = row; break; }
      if (side === SIDE_END && row[3] < ticks && ticks <= row[4]) { chosen = row; break; }
    }
    var point = Math.min(chosen[1] + ticks - chosen[3], chosen[2]);
    return [chosen[0], msHalfDown(point, TL.msTicks(doc.facts))];
  };
  TL.removedRanges = function (doc) {
    var gaps = [];
    doc.facts.sources.forEach(function (s) {
      var cursor = 0;
      doc.clips.forEach(function (clip) {
        if (clip[0] !== s.id) { return; }
        if (clip[1] > cursor) { gaps.push([s.id, cursor, clip[1]]); }
        cursor = clip[2];
      });
      if (cursor < s.frames) { gaps.push([s.id, cursor, s.frames]); }
    });
    return gaps;
  };

  // ─── 투영 · 경고 ─────────────────────────────────────────────────
  // → { cues: [[아이디, 원본, 시작 틱, 끝 틱, 글, 잘림]], hidden: [자막], total: 프레임 }
  TL.project = function (doc) {
    var unit = TL.msTicks(doc.facts);
    var rows = tickSpans(doc);
    var visible = [];
    var hidden = [];
    doc.cues.forEach(function (cue, number) {
      var mine = rows.filter(function (row) { return row[0] === cue[1]; });
      var start = cue[2] * unit;
      var end = cue[3] * unit;
      var kept = 0;
      var point = false;
      mine.forEach(function (row) {
        kept += Math.max(0, Math.min(end, row[2]) - Math.max(start, row[1]));
        if (start === end && row[1] <= start && start < row[2]) { point = true; }
      });
      if (kept === 0 && !point) { hidden.push(cue); return; }
      var outStart = editedIn(rows, unit, cue[1], cue[2], SIDE_START);
      var outEnd = editedIn(rows, unit, cue[1], cue[3], SIDE_END);
      visible.push([outStart, outEnd, number,
        [cue[0], cue[1], outStart, outEnd, cue[4], kept < end - start]]);
    });
    visible.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1] || a[2] - b[2]; });
    return { cues: visible.map(function (row) { return row[3]; }), hidden: hidden,
      total: TL.editedFrames(doc) };
  };
  TL.textChars = function (text) {
    return chars(text).filter(function (c) { return c !== " " && c !== "\n"; }).length;
  };
  TL.warnings = function (doc, settings) {
    var rate = TL.tickRate(doc.facts);
    var unit = TL.msTicks(doc.facts);
    var cues = TL.project(doc).cues;
    var found = [];
    cues.forEach(function (cue, i) {
      var following = cues[i + 1];
      var span = cue[3] - cue[2];
      var n = TL.textChars(cue[4]);
      if (n === 0) { found.push([cue[0], "empty"]); }
      if (span === 0) { found.push([cue[0], "zero"]); }
      else if (span < settings.minDurationMs * unit) { found.push([cue[0], "short"]); }
      if (span > settings.maxDurationMs * unit) { found.push([cue[0], "long"]); }
      if (span > 0 && n * rate > settings.maxCps * span) { found.push([cue[0], "fast"]); }
      if (following) {
        var gap = following[2] - cue[3];
        if (gap < 0) { found.push([cue[0], "overlap"]); }
        else if (gap > 0 && gap < settings.minGapMs * unit) { found.push([cue[0], "gap"]); }
      }
    });
    return found;
  };

  // ─── 클립 연산 ───────────────────────────────────────────────────
  function withClips(doc, clips) {
    var changed = Object.assign({}, doc, { clips: clips });
    validateClips(changed);
    return changed;
  }
  function clipAt(doc, index) {
    if (!(0 <= index && index < doc.clips.length)) { throw fail("클립 번호가 없다"); }
    return doc.clips[index];
  }
  TL.blade = function (doc, editedFrame) {
    var hit = TL.sourcePoint(doc, editedFrame);
    var index = hit[0];
    var cut = hit[1];
    var clip = doc.clips[index];
    var span = TL.layout(doc)[index];
    if (editedFrame === span.outStart || !(clip[1] < cut && cut < clip[2])) {
      throw fail("타임라인 프레임이 클립 안이 아니다(편집 점 위다)");
    }
    var pieces = [[clip[0], clip[1], cut], [clip[0], cut, clip[2]]];
    return withClips(doc, doc.clips.slice(0, index).concat(pieces, doc.clips.slice(index + 1)));
  };
  TL.rippleDelete = function (doc, editedStart, editedEnd) {
    if (!(0 <= editedStart && editedStart < editedEnd && editedEnd <= TL.editedFrames(doc))) {
      throw fail("지울 범위가 타임라인 밖이거나 비었다");
    }
    var kept = [];
    TL.layout(doc).forEach(function (span, i) {
      var clip = doc.clips[i];
      var spanEnd = span.outStart + span.outFrames;
      var low = Math.max(editedStart, span.outStart);
      var high = Math.min(editedEnd, spanEnd);
      if (low >= high) { kept.push(clip); return; }
      var head = clip[1] + offsetFrames(doc, span, low);
      var tail = clip[1] + offsetFrames(doc, span, high);
      if (head > clip[1]) { kept.push([clip[0], clip[1], Math.min(head, clip[2])]); }
      if (high < spanEnd && tail < clip[2]) { kept.push([clip[0], tail, clip[2]]); }
    });
    return withClips(doc, kept);
  };
  TL.deleteClips = function (doc, indexes) {
    var chosen = {};
    indexes.forEach(function (i) { chosen[i] = true; });
    var ok = indexes.length > 0 && indexes.every(function (i) {
      return Number.isInteger(i) && 0 <= i && i < doc.clips.length;
    });
    if (!ok) { throw fail("지울 클립 번호가 틀렸다"); }
    return withClips(doc, doc.clips.filter(function (_, i) { return !chosen[i]; }));
  };
  TL.clipBounds = function (doc, index) {
    var clip = clipAt(doc, index);
    var before = index > 0 ? doc.clips[index - 1] : null;
    var after = index + 1 < doc.clips.length ? doc.clips[index + 1] : null;
    var floor = before && before[0] === clip[0] ? before[2] : 0;
    var ceiling = after && after[0] === clip[0] ? after[1] : source(doc.facts, clip[0]).frames;
    return [floor, ceiling];
  };
  function replaceClip(doc, index, clip) {
    var clips = doc.clips.slice();
    clips[index] = clip;
    return withClips(doc, clips);
  }
  TL.setClipStart = function (doc, index, frame, settings) {
    var clip = clipAt(doc, index);
    var floor = TL.clipBounds(doc, index)[0];
    if (!(floor <= frame && frame <= clip[2] - settings.minClipFrames)) {
      throw fail("클립 시작이 갈 수 있는 범위 밖이다");
    }
    return replaceClip(doc, index, [clip[0], frame, clip[2]]);
  };
  TL.setClipEnd = function (doc, index, frame, settings) {
    var clip = clipAt(doc, index);
    var ceiling = TL.clipBounds(doc, index)[1];
    if (!(clip[1] + settings.minClipFrames <= frame && frame <= ceiling)) {
      throw fail("클립 끝이 갈 수 있는 범위 밖이다");
    }
    return replaceClip(doc, index, [clip[0], clip[1], frame]);
  };
  TL.joinClips = function (doc, index) {
    var clip = clipAt(doc, index);
    var following = clipAt(doc, index + 1);
    if (following[0] !== clip[0] || clip[2] !== following[1]) {
      throw fail("같은 원본에서 맞닿지 않아 합칠 수 없다");
    }
    var merged = [clip[0], clip[1], following[2]];
    return withClips(doc, doc.clips.slice(0, index).concat([merged], doc.clips.slice(index + 2)));
  };

  // ─── 자막 연산(원본 밀리초) ──────────────────────────────────────
  function cueIndex(doc, id) {
    for (var i = 0; i < doc.cues.length; i++) { if (doc.cues[i][0] === id) { return i; } }
    throw fail("자막이 없다: " + id);
  }
  function withCues(doc, cues, nextCue) {
    return Object.assign({}, doc, { cues: sortedCues(doc.facts, cues),
      nextCue: nextCue === undefined ? doc.nextCue : nextCue });
  }
  function checkTimes(doc, sourceId, startMs, endMs) {
    var limit = TL.sourceTotalMs(source(doc.facts, sourceId));
    if (!(0 <= startMs && startMs <= endMs && endMs <= limit)) {
      throw fail("자막 시각이 원본 밖이거나 뒤집혔다");
    }
  }
  TL.cueSetTimes = function (doc, id, startMs, endMs) {
    var index = cueIndex(doc, id);
    var cue = doc.cues[index];
    checkTimes(doc, cue[1], startMs, endMs);
    var cues = doc.cues.slice();
    cues[index] = [cue[0], cue[1], startMs, endMs, cue[4], cue[5]];
    return withCues(doc, cues);
  };
  TL.cueShift = function (doc, id, deltaMs) {
    var cue = doc.cues[cueIndex(doc, id)];
    var limit = TL.sourceTotalMs(source(doc.facts, cue[1]));
    var delta = Math.min(Math.max(deltaMs, -cue[2]), limit - cue[3]);
    return TL.cueSetTimes(doc, id, cue[2] + delta, cue[3] + delta);
  };
  TL.cueSetText = function (doc, id, text, settings) {
    var index = cueIndex(doc, id);
    var cue = doc.cues[index];
    validateText(text, settings);
    var cues = doc.cues.slice();
    cues[index] = [cue[0], cue[1], cue[2], cue[3], text, cue[5]];
    return withCues(doc, cues);
  };
  TL.cueDelete = function (doc, id) {
    var index = cueIndex(doc, id);
    return withCues(doc, doc.cues.slice(0, index).concat(doc.cues.slice(index + 1)));
  };
  TL.cuesDeleteInside = function (doc, startTicks, endTicks) {
    var inside = {};
    var any = false;
    TL.project(doc).cues.forEach(function (cue) {
      if (startTicks <= cue[2] && cue[3] <= endTicks) { inside[cue[0]] = true; any = true; }
    });
    if (!any) { throw fail("범위 안에 다 들어간 보이는 자막이 없다"); }
    return withCues(doc, doc.cues.filter(function (cue) { return !inside[cue[0]]; }));
  };
  TL.cueSplit = function (doc, id, atMs, leftText, rightText) {
    var index = cueIndex(doc, id);
    var cue = doc.cues[index];
    if (!(cue[2] < atMs && atMs < cue[3])) { throw fail("나눌 시각이 자막 안이 아니다"); }
    var left = [cue[0], cue[1], cue[2], atMs, leftText, cue[5]];
    var right = ["n" + doc.nextCue, cue[1], atMs, cue[3], rightText, null];
    var cues = doc.cues.slice(0, index).concat([left, right], doc.cues.slice(index + 1));
    return withCues(doc, cues, doc.nextCue + 1);
  };
  TL.cueMergeNext = function (doc, id) {
    var visible = TL.project(doc).cues.map(function (cue) { return cue[0]; });
    var at = visible.indexOf(id);
    if (at < 0 || at + 1 >= visible.length) { throw fail("다음에 합칠 보이는 자막이 없다"); }
    var first = doc.cues[cueIndex(doc, id)];
    var second = doc.cues[cueIndex(doc, visible[at + 1])];
    if (second[1] !== first[1]) { throw fail("다음 자막은 다른 원본이라 합칠 수 없다"); }
    var text = [first[4], second[4]].filter(function (part) { return part; }).join(" ");
    var merged = [first[0], first[1], Math.min(first[2], second[2]), Math.max(first[3], second[3]),
      text, first[5]];
    var cues = doc.cues.filter(function (cue) { return cue !== second; })
      .map(function (cue) { return cue === first ? merged : cue; });
    return withCues(doc, cues);
  };
  TL.cueInsert = function (doc, sourceId, startMs, endMs, text) {
    checkTimes(doc, sourceId, startMs, endMs);
    var cue = ["n" + doc.nextCue, sourceId, startMs, endMs, text, null];
    return withCues(doc, doc.cues.concat([cue]), doc.nextCue + 1);
  };

  // ─── 재생: 컷 건너기(W-71c) ──────────────────────────────────────
  // 브라우저만의 규칙이라 Python 짝이 없다. 활성 영상이 방금 보인 원본 프레임 하나(`presented`)로
  // 다음 걸음을 정한다 — 같은 원본에서 끝 = 다음 시작이면(블레이드) 끊지 않고 그대로 재생하고,
  // 아니면 클립 마지막 프레임에서 다음 클립 시작으로 건넌다. 대기 영상(standby = {sourceId, frame,
  // ready})이 그 자리에 맞춰져 있으면 시크를 기다리지 않고 바꿔 튼다.
  // → { action: "play" | "jump" | "stop", span, edited, target, swap, prime }
  //   target·prime = { span, sourceId, frame } 또는 null.
  function continues(spans, i) {
    var next = spans[i + 1];
    return !!next && next.sourceId === spans[i].sourceId && next.startFrame === spans[i].endFrame;
  }
  function spanTarget(span) {
    return { span: span.index, sourceId: span.sourceId, frame: span.startFrame };
  }
  function editedAt(doc, span, frame) {
    var ticks = (Math.max(frame, span.startFrame) - span.startFrame) *
      TL.sourceFrameTicks(doc.facts, span.sourceId);
    var offset = floorDiv(ticks, TL.sequenceFrameTicks(doc.facts));
    return span.outStart + Math.min(offset, span.outFrames - 1);
  }
  function cutAfter(spans, i) {
    var k = i;
    while (continues(spans, k)) { k += 1; }
    return spans[k + 1] ? spanTarget(spans[k + 1]) : null;
  }
  TL.playStep = function (doc, spanIndex, presented, standby) {
    var spans = TL.layout(doc);
    var i = spanIndex;
    if (!spans[i]) { throw fail("재생 중인 클립이 타임라인에 없다"); }
    while (presented >= spans[i].endFrame && continues(spans, i)) { i += 1; }
    var span = spans[i];
    var step = { action: "play", span: i, edited: editedAt(doc, span, presented), target: null,
      swap: false, prime: null };
    if (presented < span.endFrame - 1 || continues(spans, i)) {
      step.prime = cutAfter(spans, i);
      return step;
    }
    var next = spans[i + 1];
    if (!next) { step.action = "stop"; return step; }
    step.action = "jump";
    step.target = spanTarget(next);
    step.swap = !!(standby && standby.ready && standby.sourceId === next.sourceId &&
      standby.frame === next.startFrame);
    return step;
  };

  // ─── 범위 조정: 가장자리를 갈 수 있는 자리로(W-71d) ─────────────────
  // 끌기·키가 범위 밖을 가리켜도 거기서 멈춘다 — 같은 원본의 앞뒤 클립·원본 처음·끝(`clipBounds`)과
  // 최소 길이 안. 연산(`setClipStart`·`setClipEnd`)은 여전히 밖을 거부한다.
  TL.clampEdge = function (doc, index, side, frame, settings) {
    var clip = clipAt(doc, index);
    var bounds = TL.clipBounds(doc, index);
    var at = Math.round(frame);
    if (side === SIDE_START) {
      return Math.max(bounds[0], Math.min(at, clip[2] - settings.minClipFrames));
    }
    return Math.min(bounds[1], Math.max(at, clip[1] + settings.minClipFrames));
  };

  // ─── 클립 편집 뒤의 선택·플레이헤드(W-71f) ─────────────────────────
  // 연산은 모델 그대로(`blade`·`deleteClips`·`joinClips`) — 여기서는 화면이 어디를 보는지만 정한다.
  // 마지막 클립은 지우지 않는다: 빈 타임라인은 렌더가 거부하고 재생할 것이 없다.
  TL.clampView = function (doc, sel, playhead) {
    var last = Math.max(TL.editedFrames(doc) - 1, 0);
    return { doc: null, sel: Math.max(0, Math.min(sel, doc.clips.length - 1)),
      playhead: Math.max(0, Math.min(playhead, last)) };
  };
  TL.clipEdit = function (doc, op, sel, playhead) {
    if (op === "blade") {
      var cut = TL.blade(doc, playhead);
      return { doc: cut, sel: TL.sourcePoint(doc, playhead)[0] + 1, playhead: playhead };
    }
    if (op === "delete") {
      clipAt(doc, sel);
      if (doc.clips.length === 1) { throw fail("마지막 클립은 지우지 않는다"); }
      var kept = TL.deleteClips(doc, [sel]);
      var at = Math.min(sel, kept.clips.length - 1);
      return { doc: kept, sel: at, playhead: TL.layout(kept)[at].outStart };
    }
    if (op === "join") {
      if (sel === doc.clips.length - 1) { throw fail("마지막 클립은 합칠 다음 조각이 없다"); }
      var joined = TL.joinClips(doc, sel);
      var view = TL.clampView(joined, sel, playhead);
      return { doc: joined, sel: view.sel, playhead: view.playhead };
    }
    throw fail("모르는 클립 편집이다: " + op);
  };

  // ─── 자막 편집: 플레이헤드에서(W-71i) ──────────────────────────────
  // 자막은 원본 밀리초에 산다 — 플레이헤드(출력 프레임)를 그 아래 원본의 밀리초로 바꿔 모델 연산
  // (`cueSetTimes`·`cueSplit`…)에 넘기고, 화면이 다음에 고를 자막(`cue`)만 정한다. 컷에 가려진
  // 자막은 고치지 않는다(클립을 늘리면 그대로 돌아온다, 규칙 9). → { doc | null, cue }
  TL.splitText = function (text) {
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
  // 커서에서 나누기(Subtitle Edit ⌘⌥V) — caret은 입력칸의 `selectionStart`(UTF-16 위치)라 글도
  // 같은 단위로 자른다. 자른 자리의 공백만 떨군다(`splitText`와 같다).
  TL.splitAt = function (text, caret) {
    var at = Math.max(0, Math.min(text.length, Math.round(caret)));
    return [text.slice(0, at).replace(/\s+$/, ""), text.slice(at).replace(/^\s+/, "")];
  };
  function visibleIds(doc) { return TL.project(doc).cues.map(function (cue) { return cue[0]; }); }
  TL.cueAt = function (doc, playhead) {
    var t = playhead * TL.sequenceFrameTicks(doc.facts);
    var hit = TL.project(doc).cues.filter(function (cue) {
      return cue[2] === cue[3] ? cue[2] === t : cue[2] <= t && t < cue[3];
    })[0];
    return hit ? hit[0] : null;
  };
  // 플레이헤드 아래 원본의 밀리초 — 자막의 원본이 아니면 거부한다(다른 원본 시계를 줄 수 없다).
  function playheadMs(doc, cue, playhead, side) {
    var at = TL.sourceMsAt(doc, playhead * TL.sequenceFrameTicks(doc.facts), side);
    if (at[0] !== cue[1]) { throw fail("플레이헤드가 이 자막의 원본에 있지 않다"); }
    return at[1];
  }
  function cueInsertAt(doc, playhead, settings) {
    if (!(0 <= playhead && playhead < TL.editedFrames(doc))) {
      throw fail("플레이헤드 자리에 클립이 없다");
    }
    var seq = TL.sequenceFrameTicks(doc.facts);
    var start = TL.sourceMsAt(doc, playhead * seq, SIDE_START);
    var span = TL.layout(doc)[TL.sourcePoint(doc, playhead)[0]];
    var clipEnd = TL.sourceMsAt(doc, (span.outStart + span.outFrames) * seq, SIDE_END)[1];
    var id = "n" + doc.nextCue;
    var end = Math.max(start[1], Math.min(start[1] + settings.newCueMs, clipEnd));
    return { doc: TL.cueInsert(doc, start[0], start[1], end, ""), cue: id };
  }
  function cueAfterDelete(doc, id) {
    var ids = visibleIds(doc);
    var at = ids.indexOf(id);
    var kept = TL.cueDelete(doc, id);
    var next = ids[at + 1] !== undefined ? ids[at + 1] : ids[at - 1];
    return { doc: kept, cue: next === undefined ? null : next };
  }
  TL.cueEdit = function (doc, op, id, playhead, settings, text) {
    if (op === "insert") { return cueInsertAt(doc, playhead, settings); }
    var cue = doc.cues[cueIndex(doc, id)];
    if (visibleIds(doc).indexOf(id) < 0) {
      throw fail("컷에 가려진 자막은 고치지 않는다 — 클립을 늘리면 돌아온다");
    }
    if (op === "start" || op === "end") {
      var isStart = op === "start";
      var ms = playheadMs(doc, cue, playhead, isStart ? SIDE_START : SIDE_END);
      if (isStart ? ms > cue[3] : ms < cue[2]) { throw fail("자막 시작이 끝보다 늦어진다"); }
      return { doc: TL.cueSetTimes(doc, id, isStart ? ms : cue[2], isStart ? cue[3] : ms),
        cue: id };
    }
    if (op === "split") {
      // text가 수면 그 커서에서(⌘⌥V), 아니면 줄바꿈 · 가운데 공백에서 글을 나눈다. 시각은 언제나
      // 플레이헤드다 — 글 길이로 시각을 나누지 않는다(규칙 1).
      var parts = typeof text === "number" ? TL.splitAt(cue[4], text) : TL.splitText(cue[4]);
      var at = playheadMs(doc, cue, playhead, SIDE_START);
      return { doc: TL.cueSplit(doc, id, at, parts[0], parts[1]), cue: id };
    }
    if (op === "merge") { return { doc: TL.cueMergeNext(doc, id), cue: id }; }
    if (op === "delete") { return cueAfterDelete(doc, id); }
    if (op === "text") {
      if (text === cue[4]) { return { doc: null, cue: id }; }
      return { doc: TL.cueSetText(doc, id, text, settings), cue: id };
    }
    throw fail("모르는 자막 편집이다: " + op);
  };

  root.__vlTL = TL;
  if (typeof module !== "undefined" && module.exports) { module.exports = TL; }
})(typeof window !== "undefined" ? window : globalThis);
"""
