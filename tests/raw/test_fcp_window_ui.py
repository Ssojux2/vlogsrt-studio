"""Final Cut식 창 골격(W-85, D145) — 아래 덱 · 토큰 비율 · 화면 틱 임대.

FCP 비율과 배치는 Final Cut Pro 12.4의 정적 분석이다(`docs/plans/2026-11-16-fcp-window.md`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_timeline_player_ui import _ancestor_elem_ids, _server  # noqa: E402
from ui import design_tokens, timer_lease_js, ui_facade  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")


def test_stage_tracks_come_from_tokens_not_literals() -> None:
    """열·행 비율은 토큰에만 산다 — 무대 CSS에 539·403 같은 숫자가 없다(규칙 8)."""
    tokens = design_tokens.SCALAR_TOKENS
    assert (tokens["browser-fr"], tokens["viewer-fr"], tokens["inspector-fr"]) == (
        "539fr",
        "539fr",
        "403fr",
    )
    assert tokens["deck-viewer-h"].startswith("clamp(")
    css = ui_facade.WORKBENCH_CSS
    assert not re.search(r"\b(539|403)(fr|px)", css)
    assert "#vl-timeline-deck { grid-area: deck;" in css


def test_a_filled_deck_lets_the_stage_grow_and_an_empty_one_changes_nothing() -> None:
    """타임라인이 있으면 무대는 내용 높이이고 위 덱 뷰어 영상은 토큰 높이다. 없으면 예전 화면."""
    css = ui_facade.WORKBENCH_CSS
    filled = css[css.index("#visual-stage:has(#vl-timeline-deck .tl-root:not(.tl-empty)) {") :]
    filled = filled[: filled.index("}")]
    assert "height: auto;" in filled
    assert "--vl-viewer-video-h: var(--vl-deck-viewer-h);" in filled
    assert "height: var(--vl-viewer-video-h, auto)" in css
    wide = css[css.index("@media (min-width: 1051px)") :]
    wide = wide[: wide.index("\n}\n")]
    assert '#visual-stage[data-vl-advanced="open"]:has(#vl-timeline-deck' in wide


def test_deck_and_lease_buttons_live_in_the_stage(tmp_path: Path, local_socket: None) -> None:
    server, _root, _signature = _server(tmp_path)
    app = server.build_blocks()
    config = app.config
    by_elem = {c["props"].get("elem_id"): c for c in config["components"]}
    for elem in (timer_lease_js.REST_BUTTON_ID, timer_lease_js.RESUME_BUTTON_ID):
        assert by_elem[elem]["props"]["visible"] == "hidden"
        assert "vl-timeline-deck" in _ancestor_elem_ids(config, by_elem[elem]["id"], by_elem)
    summary = by_elem["vl-timeline-summary"]
    assert "__vlTimerLease" in (summary["props"].get("js_on_load") or "")


def test_lease_buttons_rest_and_resume_only_the_review_tick(
    tmp_path: Path, local_socket: None
) -> None:
    """숨은 단추 둘은 화면 틱 하나만 끄고 켠다 — 대기열 밖, 갱신 하나(D143의 4 ms 함정)."""
    server, _root, _signature = _server(tmp_path)
    app = server.build_blocks()
    config = app.config
    ids = {c["props"].get("elem_id"): c["id"] for c in config["components"]}
    timer = server._review_timer
    assert timer is not None
    found: dict[str, Any] = {}
    for dep in config["dependencies"]:
        targets = {target[0] for target in dep["targets"]}
        for elem in (timer_lease_js.REST_BUTTON_ID, timer_lease_js.RESUME_BUTTON_ID):
            if ids[elem] in targets:
                found[elem] = dep
    assert set(found) == {timer_lease_js.REST_BUTTON_ID, timer_lease_js.RESUME_BUTTON_ID}
    for elem, dep in found.items():
        assert dep["outputs"] == [timer._id], elem
        assert dep["queue"] is False, elem
    rest = app.fns[found[timer_lease_js.REST_BUTTON_ID]["id"]].fn
    resume = app.fns[found[timer_lease_js.RESUME_BUTTON_ID]["id"]].fn
    assert rest().active is False and resume().active is True  # type: ignore[union-attr]


_LEASE_SCRIPT = r"""
const L = require(process.argv[2]);
const idle = 4000;
let s = L.initial();
const out = [];
function go(event, now, playing) {
  const r = L.step(s, event, now, idle, playing);
  s = r.state;
  out.push([event, now, r.click, s.resting]);
}
go("tick", 0, false);        // 쉬지 않을 때 틱은 아무것도 하지 않는다
go("interact", 100, false);  // 만지면 쉼 — 단추 한 번
go("interact", 200, false);  // 이미 쉼 — 다시 누르지 않는다
go("tick", 3000, false);     // 아직 유휴 아님
go("tick", 4300, true);      // 재생 중이면 켜지 않고 시각을 민다
go("tick", 8000, false);     // 재생이 4300에 있었으니 아직
go("tick", 8300, false);     // 유휴 — 켬
go("tick", 9000, false);     // 이미 켬 — 다시 누르지 않는다
go("interact", 9100, false); // 다시 쉼
go("blur", 9200, true);      // 재생 중 초점이 나가도 켜지 않는다
go("blur", 9300, false);     // 초점이 나가면 켬
console.log(JSON.stringify(out));
"""


@needs_node
def test_lease_rests_while_touched_or_playing_and_resumes_after_idle(tmp_path: Path) -> None:
    core = tmp_path / "lease.js"
    core.write_text(timer_lease_js.LEASE_CORE_JS, encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_LEASE_SCRIPT, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(done.stdout) == [
        ["tick", 0, None, False],
        ["interact", 100, "rest", True],
        ["interact", 200, None, True],
        ["tick", 3000, None, True],
        ["tick", 4300, None, True],
        ["tick", 8000, None, True],
        ["tick", 8300, "resume", False],
        ["tick", 9000, None, False],
        ["interact", 9100, "rest", True],
        ["blur", 9200, None, True],
        ["blur", 9300, "resume", False],
    ]


def test_lease_numbers_ride_as_data() -> None:
    js = timer_lease_js.lease_bind_js()
    config = json.loads(js[js.index("var CFG = ") + len("var CFG = ") : js.index(";\n  var lease")])
    assert config == {
        "deck": "vl-timeline-deck",
        "rest": timer_lease_js.REST_BUTTON_ID,
        "resume": timer_lease_js.RESUME_BUTTON_ID,
        "idleMs": timer_lease_js.IDLE_MS,
        "tickMs": timer_lease_js.TICK_MS,
    }
    assert "W.__vlTimerLeaseBound = true" in js
