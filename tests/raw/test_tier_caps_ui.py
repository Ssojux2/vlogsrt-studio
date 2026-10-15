"""작업대가 「추천 제한」(D60)을 보인다 — 렠 막대의 제목·읽기 글과 구간 패널, 글은 이스케이프한다.

표가 추천이라 했지만 추천하지 않는 까닭(3초보다 짧음 · 자막 시각 빨강)은 서버가 말로 싣고
(`caps`), 화면은 그 말을 그대로 보인다 — 화면에 없는 단추를 가리키지 않고 「VAD」라고 쓰지 않는다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, needs_node
from ui import ui_facade

_CAP_SCRIPT = r"""
const capModel = visualModel();
capModel.sources.srcA.suggestions = [
  { start: '0', end: '1', tier: 'yellow', hold: false, rec: 'normal', rule: 'both-good',
    reason: 'r0', redReason: null,
    caps: ['3초보다 짧아 추천하지 않습니다', '<b>굵게</b> & 따옴표"'],
    audio: { status: 'speech_dense', label: '발화 많음',
      reason: '발화가 구간의 80%를 덮는다 · ' +
        '말소리 측정상 없는 줄 1개를 밀도에서 뺐습니다(측정 한계일 수 있음)',
      density: '4/5' },
    video: { status: 'good', label: '좋음', reason: '계곡이 선명하다', evidence: [] } },
];
const envCap = visualEnv({}, capModel);
const capBars = envCap.rail.innerHTML.split('<button')
  .filter((b) => b.includes('vl-recommendation'));
envCap.rail._listeners.click.slice().forEach((fn) => fn({ target: {
  getAttribute: (k) => (k === 'data-vl-recommendation' ? '0' : null) } }));
checkpoints.push({ case: 'caps', bars: capBars, panel: envCap.panel.innerHTML });

// ── 다시 잰 뒤의 틱 — 같은 구간(원본·시작·끝)의 등급이 바뀌면 열린 패널도 따라온다(검증 지적 1) ──
const SPEECH = { status: 'speech_dense', label: '발화 많음', reason: '발화가 구간의 80%를 덮는다',
  density: '4/5' };
const VIDEO = { status: 'good', label: '좋음', reason: '계곡이 선명하다', evidence: [] };
const RED_CAP = '자막 시각 확인이 빨강이라 보통까지만 추천합니다 — ' +
  '시각을 맞춘 뒤 다시 재면 풀립니다';
function tick(env, row) {
  const next = visualModel();
  next.sources.srcA.suggestions = row ? [row] : [];
  env.root.setAttribute('data-vl-model', JSON.stringify(next));
  env.rewired();
  const reason = env.panel.innerHTML.match(/<div class="vl-marker-reason[^]*?<\/div>/);
  return reason ? reason[0] : '';
}
const capped = { start: '0', end: '1', tier: 'yellow', hold: false, rec: 'normal',
  rule: 'both-good', reason: 'r', redReason: null, caps: [RED_CAP], audio: SPEECH, video: VIDEO };
const lifted = Object.assign({}, capped, { tier: 'green', rec: 'recommended', caps: [] });
const envTick = visualEnv({}, (() => { const m = visualModel();
  m.sources.srcA.suggestions = [capped]; return m; })());
envTick.rail._listeners.click.slice().forEach((fn) => fn({ target: {
  getAttribute: (k) => (k === 'data-vl-recommendation' ? '0' : null) } }));
const openedRed = (envTick.panel.innerHTML.match(/<div class="vl-marker-reason[^]*?<\/div>/) ||
  [''])[0];
const afterGreen = tick(envTick, lifted);
const afterRedAgain = tick(envTick, capped);
const afterGone = tick(envTick, Object.assign({}, lifted, { start: '1', end: '2' }));
checkpoints.push({ case: 'marker-refresh', openedRed, afterGreen, afterRedAgain, afterGone,
  barAfterGone: envTick.rail.innerHTML.includes('vl-rec-sel') });
"""


def _run(tmp_path: Path, case: str = "caps") -> dict[str, Any]:
    harness = tmp_path / "caps.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _CAP_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {"visual": ui_facade.VISUAL_BOOTSTRAP_JS, "caption": ui_facade.CAPTION_BOOTSTRAP_JS}
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2500:]
    return next(row for row in json.loads(completed.stdout) if row.get("case") == case)


@needs_node
def test_rail_bar_and_panel_name_every_cap_escaped(tmp_path: Path) -> None:
    report = _run(tmp_path)
    (bar,) = report["bars"]
    assert 'class="vl-recommendation normal yellow"' in bar
    assert "추천 제한: 3초보다 짧아 추천하지 않습니다" in bar  # 제목·읽기 글
    assert "추천 제한: &lt;b&gt;굵게&lt;/b&gt; &amp; 따옴표&quot;" in bar
    panel = report["panel"]
    assert '<p class="vl-kv">추천 제한 · 3초보다 짧아 추천하지 않습니다</p>' in panel
    assert '<p class="vl-kv">추천 제한 · &lt;b&gt;굵게&lt;/b&gt; &amp; 따옴표&quot;</p>' in panel
    assert "<b>굵게</b>" not in panel and "<b>굵게</b>" not in bar
    assert "(측정 한계일 수 있음)" in panel
    assert "VAD" not in panel and "VAD" not in bar


@needs_node
def test_open_marker_panel_follows_a_remeasure_in_both_directions(tmp_path: Path) -> None:
    """막대를 눌러 연 패널은 누른 순간의 사본이 아니다 — 다음 그리기가 같은 구간을 다시 읽는다."""
    report = _run(tmp_path, "marker-refresh")
    cap = "추천 제한 · 자막 시각 확인이 빨강이라 보통까지만 추천합니다"
    assert "<strong>보통</strong>" in report["openedRed"] and cap in report["openedRed"]
    assert "<strong>추천</strong>" in report["afterGreen"] and cap not in report["afterGreen"]
    assert "<strong>보통</strong>" in report["afterRedAgain"] and cap in report["afterRedAgain"]
    # 그 구간이 사라졌으면 표지를 거둔다 — 옛 구간의 등급을 계속 말하지 않는다.
    assert report["afterGone"] == "" and report["barAfterGone"] is False


def test_suggestion_row_carries_the_cap_words() -> None:
    from ui.workspace_service import RawWorkspaceUi

    row = {
        "start": {"num": 0, "den": 1},
        "end": {"num": 3, "den": 1},
        "tier": "yellow",
        "hold": False,
        "recommendation": "normal",
        "rule": "both-good",
        "reason": "자막: a · 영상: b",
        "caps": [{"code": "timing_red", "text": "자막 시각 확인이 빨강이라 보통까지만 추천합니다"}],
        "red_reason": None,
        "audio": {"status": "speech_dense", "label": "발화 많음", "reason": "a"},
        "video": {"status": "good", "label": "좋음", "reason": "b"},
    }
    shown = RawWorkspaceUi._suggestion_row(row)
    assert shown["caps"] == ["자막 시각 확인이 빨강이라 보통까지만 추천합니다"]
    assert RawWorkspaceUi._suggestion_row({**row, "caps": []})["caps"] == []
    old = {key: value for key, value in row.items() if key != "caps"}  # v2 문서 모양
    assert RawWorkspaceUi._suggestion_row(old)["caps"] == []
