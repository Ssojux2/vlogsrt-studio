"""자막 목록 표(Subtitle Edit식, W-87, D146) — 순수 계산. 노드에서도 돈다(`module.exports`).

Subtitle Edit 5.2.0의 목록 열(#·시작·끝·길이·텍스트·간격·문자/초, 정적 분석 —
`docs/plans/2026-11-16-fcp-window.md`)을 원본 범위 타임라인의 투영(`TL.project`)과
경고(`TL.warnings`)에서 만든다. 시각은 계산하지 않는다 — 시퀀스 틱을 프레임·초로
**보여 줄** 뿐이고, 문서를 고치는 것은 여전히 `TL.cueEdit` 하나다(규칙 3).

경고 → 칸: 빈 글 → 텍스트 · 길이 0·짧음·김 → 길이 · 빠름 → 문자/초 · 겹침·좁은 틈 → 간격.
Subtitle Edit처럼 칸만 붉게 칠하고(F8·⇧F8로 오간다) 줄을 지우거나 숨기지 않는다.
"""

from __future__ import annotations

CAPTION_GRID_JS = r"""
(function (root) {
  var G = {};
  var CELL_OF = { empty: "text", zero: "dur", short: "dur", long: "dur", fast: "cps",
    overlap: "gap", gap: "gap" };
  G.CELL_OF = CELL_OF;
  // cues: TL.project(doc).cues — [id, source, startTicks, endTicks, text, clipped].
  // warns: { id: ["short", ...] }. seqTicks: 시퀀스 프레임 하나의 틱. rate: 초당 틱.
  G.rows = function (cues, warns, seqTicks, rate, textChars) {
    return cues.map(function (cue, i) {
      var following = cues[i + 1];
      var span = cue[3] - cue[2];
      var bad = { text: false, dur: false, cps: false, gap: false };
      (warns[cue[0]] || []).forEach(function (kind) {
        if (CELL_OF[kind]) { bad[CELL_OF[kind]] = true; }
      });
      var chars = textChars(cue[4]);
      return {
        id: cue[0],
        n: i + 1,
        startFrame: Math.floor(cue[2] / seqTicks),
        endFrame: Math.floor(cue[3] / seqTicks),
        seconds: span / rate,
        text: cue[4],
        chars: chars,
        cps: span > 0 ? chars * rate / span : null,
        gapSeconds: following ? (following[2] - cue[3]) / rate : null,
        clipped: !!cue[5],
        bad: bad,
        warned: (warns[cue[0]] || []).length > 0
      };
    });
  };
  // F8·⇧F8 — 지금 줄 다음(앞) 경고 줄의 아이디, 없으면 null. 지금 줄이 없으면 처음(끝)부터.
  G.nextError = function (rows, currentId, direction) {
    var at = -1;
    for (var i = 0; i < rows.length; i += 1) { if (rows[i].id === currentId) { at = i; } }
    var step = direction < 0 ? -1 : 1;
    var start = at < 0 ? (step > 0 ? 0 : rows.length - 1) : at + step;
    for (var j = start; j >= 0 && j < rows.length; j += step) {
      if (rows[j].warned) { return rows[j].id; }
    }
    return null;
  };
  // 줄마다 글자 수(공백 제외) — 입력칸의 줄 길이 표시. 줄바꿈은 ⏎로 보인다(목록).
  G.lineLengths = function (text, textChars) {
    return String(text).split("\n").map(function (line) { return textChars(line); });
  };
  G.oneLine = function (text) { return String(text).split("\n").join(" ⏎ "); };
  root.__vlCaptionGrid = G;
  if (typeof module !== "undefined" && module.exports) { module.exports = G; }
})(typeof window !== "undefined" ? window : globalThis);
"""
