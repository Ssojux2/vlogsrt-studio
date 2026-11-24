"""번역 모드(W-87d, D153) — 영어 열의 서버 함수 · 순수 찾기 · 재생기 배선. 사지 않는다."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_timeline_player_ui import _server  # noqa: E402
from ui import caption_grid_js  # noqa: E402
from ui.timeline_player import timeline_player_html  # noqa: E402
from ui.timeline_player_js import PLAYER_APP_JS  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")


def test_english_server_function_returns_pairs_for_the_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    asked: list[str] = []

    def english(_root: Path, timeline_id: str) -> dict[str, str]:
        asked.append(timeline_id)
        return {"안녕": "Hi"}

    monkeypatch.setattr(server.raw, "timeline_english", english)
    got = player.action({"signature": signature, "timelineId": "t1"}, "english")
    assert got == {"ok": True, "timelineId": "t1", "pairs": {"안녕": "Hi"}}
    assert asked == ["t1"]
    assert "tl_english" in [f.__name__ for f in player.server_functions()]


_LOOKUP = r"""
const G = require(process.argv[2]);
const pairs = { "안녕하세요 여러분": "Hello everyone", "산": "Mountain" };
console.log(JSON.stringify([G.englishOf("안녕하세요\n여러분", pairs),
  G.englishOf("  안녕하세요   여러분 ", pairs), G.englishOf("산!", pairs),
  G.englishOf("", pairs), G.englishOf("산", null), G.englishOf("toString", {})]));
"""


@needs_node
def test_english_is_found_by_the_same_joined_text_the_translation_keyed(tmp_path: Path) -> None:
    """번역의 줄 글은 공백을 한 칸으로 이은 한국어(`translation.cue_lines`) — 같은 규칙으로 찾는다.
    고친 글은 null(「번역 없음」), 빈 글은 ""(빈 칸), 객체의 물려받은 이름은 짝이 아니다."""
    core = tmp_path / "grid.js"
    core.write_text(caption_grid_js.CAPTION_GRID_JS, encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(_LOOKUP, encoding="utf-8")
    done = subprocess.run(
        ["node", str(runner), str(core)], capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(done.stdout) == ["Hello everyone", "Hello everyone", None, "", None, None]


def test_translation_mode_adds_an_english_column_edited_only_by_one_document_op() -> None:
    """「번역 모드」가 영어 열과 입력칸의 영어 칸을 붙인다. 저장된 번역은 읽기만 하고, 고치기는
    문서 연산 `TL.cueSetEnglish` 하나다(D166).

    보이는 차례는 고친 것 · 저장된 번역 · 낡은 고친 것이다.
    """
    from tests.raw.test_timeline_player_ui import TIERS, _media, _opened
    from ui.timeline_player import client_model

    html = timeline_player_html(client_model("sig", "tok", _opened(), _media(), TIERS))
    assert 'data-tl="translation" id="tl-translation" aria-pressed="false"' in html
    assert "번역 모드" in html
    body = PLAYER_APP_JS[PLAYER_APP_JS.index("function englishKey()") :]
    body = body[: body.index("function cell(")]
    assert "S.server.tl_english({ signature: S.signature, timelineId: asked })" in body
    assert set(re.findall(r"TL\.(\w+)\(", body)) == {"editedEnglish", "cueSetEnglish", "isError"}
    label = body[body.index("function englishLabel(") : body.index("var ENGLISH_NOTE")]
    order = [label.index(k) for k in ('kind: "edited"', 'kind: "saved"', 'kind: "stale"')]
    assert order == sorted(order)
    commit = body[body.index("function commitEnglish()") : body.index("function onEnglishKey(")]
    assert 'getAttribute("data-shown")' in commit and "change(next," in commit
    assert '(S.translation ? englishCell(row.id, row.text) : "")' in PLAYER_APP_JS
    assert 'id="tl-cue-en-text"' in PLAYER_APP_JS
    # 확정 자리(저장 · 렌더 · 고르기 · 메뉴 앞)는 모두 `commitText`를 지나고,
    # 그것이 영어도 확정한다.
    assert "function commitText() {\n    commitEnglish();" in PLAYER_APP_JS
    # 공유가 끝나면 새 초안의 영어를 다시 찾는다.
    done = PLAYER_APP_JS[PLAYER_APP_JS.index('showInspector("share"); }') :][:400]
    assert "S.english = null;" in done and "askEnglish();" in done
