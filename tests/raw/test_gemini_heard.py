"""W-30e — 자막 검증에 Gemini Flash가 들은 말을 반영한다: 표시 + 참고 글자(D88).

사용자 지시(2026-10-25): 「자막 검증에 gemini flash 음성인식 결과도 반영해줘」 — 고른 것: 표시 +
편집기에
참고 글자(자막은 저절로 바뀌지 않는다). 유료 호출 0회. 시험이 못 박는 것:

- 줄은 가운데가 코어에 드는 창 하나에 속하고, 줄 글자 가운데 Gemini가 들은 말에서 찾은 몫이 기준보다
  작으면 「Gemini가 다르게 들음」이다. 들은 말이 비면 「듣지 못함」(말이 아니라고 하지 않는다). 아주
  짧은 줄은 짚지 않고, Gemini만 들은 말(자막에 없는 말)도 짚지 않는다.
- 표시는 「확인 필요」 하나로만 나가고 「자막 확인됨」 몫(D65)에 들지 않는다.
- 서비스는 가장 최근 분석이 Gemini 판정일 때만 그 창의 들은 말을 쓴다.
- 편집기는 그 줄의 창에서 들은 말을 참고로 보여 주고 「가져오기」는 문구 칸만 채운다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_visual_workbench_ui import _NODE_HARNESS
from ui import ui_facade
from vlogsrt.project.raw import subtitle_suspects as ss
from vlogsrt.project.raw.service import RawWorkspaceService

F = Fraction
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node required")


def windows(*rows: tuple[int, int, str]) -> tuple[ss.HeardWindow, ...]:
    return tuple(ss.HeardWindow(F(a), F(b), heard) for a, b, heard in rows)


# ── 비교(순수) ────────────────────────────────────────────────────────────


def test_a_line_mostly_missing_from_what_gemini_heard_is_flagged() -> None:
    cues = [
        ("u0", "여기 바다 진짜 예쁘다", F(1), F(3)),
        ("u1", "시청해주셔서 감사합니다", F(5), F(8)),
        ("u2", "와", F(9), F(10)),  # 한 글자 — 짚지 않는다
    ]
    heard, disagreed = ss.heard_comparison(
        cues, windows((0, 15, "아 여기 바다 진짜 예쁘다 그치 와"))
    )
    assert heard == {cue_id: "아 여기 바다 진짜 예쁘다 그치 와" for cue_id, *_ in cues}
    assert disagreed == frozenset({"u1"})


def test_a_line_belongs_to_the_window_whose_core_holds_its_middle() -> None:
    cues = [("u0", "첫 창의 말", F(13), F(14)), ("u1", "둘째 창의 말", F(14), F(17))]
    heard, disagreed = ss.heard_comparison(
        cues, windows((0, 15, "첫 창의 말"), (15, 30, "둘째 창의 말"))
    )
    assert heard == {"u0": "첫 창의 말", "u1": "둘째 창의 말"}
    assert disagreed == frozenset()


def test_an_empty_hearing_flags_lines_but_lines_outside_every_window_are_left_alone() -> None:
    cues = [("u0", "지어낸 끝인사", F(2), F(4)), ("u1", "창 밖의 말", F(40), F(42))]
    heard, disagreed = ss.heard_comparison(cues, windows((0, 15, "")))
    assert heard == {"u0": ""} and disagreed == frozenset({"u0"})


def test_words_only_gemini_heard_are_not_flagged() -> None:
    cues = [("u0", "안녕", F(1), F(2))]
    _heard, disagreed = ss.heard_comparison(
        cues, windows((0, 15, "안녕 오늘 날씨 좋다 바다 가자 여기 너무 예쁘다"))
    )
    assert disagreed == frozenset()


def test_the_floor_and_minimum_come_from_settings() -> None:
    from dataclasses import replace

    from vlogsrt.project.raw.config import SUBTITLE_SUSPECT_SETTINGS, subtitle_suspect_problems

    strict = replace(SUBTITLE_SUSPECT_SETTINGS, gemini_heard_coverage_floor=1.0)
    cues = [("u0", "여기 바다", F(1), F(2))]
    _, loose = ss.heard_comparison(cues, windows((0, 15, "여기 바닥")))
    _, tight = ss.heard_comparison(cues, windows((0, 15, "여기 바닥")), strict)
    assert loose == frozenset() and tight == frozenset({"u0"})
    for bad in ({"gemini_heard_coverage_floor": 0}, {"gemini_heard_min_chars": 0}):
        assert subtitle_suspect_problems(replace(SUBTITLE_SUSPECT_SETTINGS, **bad))


# ── 표시 ─────────────────────────────────────────────────────────────────


def test_the_mark_says_heard_differently_or_heard_nothing_never_not_speech() -> None:
    timing = ss.CueTiming(
        gemini_heard={"u0": "다른 말", "u1": ""}, gemini_disagreed=frozenset({"u0", "u1"})
    )
    found = ss.suspects([("u0", "원래 말"), ("u1", "끝인사"), ("u2", "그대로")], timing=timing)
    assert found["u0"] == (ss.Suspect(ss.GEMINI_DISAGREED, ss.GEMINI_DISAGREED_TEXT),)
    assert found["u1"] == (ss.Suspect(ss.GEMINI_DISAGREED, ss.GEMINI_HEARD_NOTHING_TEXT),)
    assert "u2" not in found
    assert "말이 아니" not in ss.GEMINI_HEARD_NOTHING_TEXT


def transcript() -> dict[str, Any]:
    def row(cue_id: str, text: str, start: int, end: int) -> dict[str, Any]:
        return {
            "utterance_id": cue_id,
            "text": text,
            "start": {"num": start, "den": 1},
            "end": {"num": end, "den": 1},
        }

    return {"utterances": [row("u0", "여기 바다 예쁘다", 1, 3), row("u1", "시청 감사합니다", 5, 8)]}


def test_service_notes_carry_gemini_text_and_mark_but_not_into_the_verified_share() -> None:
    timing = RawWorkspaceService.with_gemini_heard(
        ss.CueTiming(), transcript(), windows((0, 15, "여기 바다 예쁘다"))
    )
    notes = RawWorkspaceService.subtitle_cue_notes(transcript(), None, timing)
    assert notes["u0"] == {"gemini": {"heard": "여기 바다 예쁘다"}}
    assert notes["u1"]["gemini"] == {"heard": "여기 바다 예쁘다"}
    assert [s["code"] for s in notes["u1"]["suspects"]] == [ss.GEMINI_DISAGREED]
    # 「자막 확인됨」 몫(D65)은 시각 재료 없이 센다 — Gemini 표시는 들지 않는다
    assert RawWorkspaceService.subtitle_cue_notes(transcript(), None) == {}


def test_no_windows_leave_the_timing_untouched() -> None:
    timing = ss.CueTiming(ear_disagreed=frozenset({"u0"}))
    assert RawWorkspaceService.with_gemini_heard(timing, transcript(), ()) is timing


def document(model: str) -> dict[str, Any]:
    def window(window_id: str, start: int, end: int) -> dict[str, Any]:
        return {
            "window_id": window_id,
            "core_start": {"num": start, "den": 1},
            "core_end": {"num": end, "den": 1},
        }

    return {
        "windows": {"windows": [window("window_000000", 0, 15), window("window_000001", 15, 30)]},
        "annotations": [
            {"window_id": "window_000000", "status": "accepted", "luna_model": model,
             "heard_speech": "여기 바다"},
            {"window_id": "window_000001", "status": "failed", "luna_model": model},
        ],
    }  # fmt: skip


def test_only_a_latest_gemini_analysis_gives_heard_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = RawWorkspaceService()
    monkeypatch.setattr(service, "_workspace", lambda root: None)
    monkeypatch.setattr(
        service, "_latest_analysis_document", lambda workspace, sid: document("gemini-3.8-flash")
    )
    (only,) = service._gemini_heard_windows(tmp_path, "src")  # 실패한 창은 들은 말이 없다
    assert only == ss.HeardWindow(F(0), F(15), "여기 바다")
    monkeypatch.setattr(
        service, "_latest_analysis_document", lambda workspace, sid: document("claude-sonnet-5-5")
    )
    assert service._gemini_heard_windows(tmp_path, "src") == ()
    monkeypatch.setattr(service, "_latest_analysis_document", lambda workspace, sid: None)
    assert service._gemini_heard_windows(tmp_path, "src") == ()


# ── 편집기 ────────────────────────────────────────────────────────────────

_GEMINI_SCRIPT = r"""
const gModel = visualModel();
gModel.epoch = 'gem-1'; gModel.token = 'gem-token';
gModel.sources.srcA.transcriptRevision = 'rev-g';
gModel.sources.srcA.subtitles = [
  { id: 'g0', start: '0', end: '2', text: '시청 감사합니다',
    gemini: { heard: '아 여기 <바다> 예쁘다' },
    suspects: [{ code: 'gemini_disagreed', text: 'Gemini가 이 구간에서 다른 말을 들었습니다' }] },
  { id: 'g1', start: '3', end: '4', text: '그냥 줄' },
];
const envG = visualEnv({ vl_source_caption: () => ({ ok: true, echoEpoch: 'gem-1' }) }, gModel);
const pickG = (id) => envG.click(el({ attrs: { 'data-vl-act': 'source-pick',
  'data-vl-cue-id': id } }));
pickG('g0');
const gemPanel = envG.panel.innerHTML;
const gBox = el({ id: 'vl-source-text', value: '시청 감사합니다' });
envG.registry['vl-source-text'] = gBox;
envG.click(el({ attrs: { 'data-vl-act': 'ear-take', 'data-vl-take': 'gemini' } }));
const gTaken = { value: gBox.value, calls: envG.serverCalls.length };
pickG('g1');
const plainPanel = envG.panel.innerHTML;
checkpoints.push({ case: 'gemini', gemPanel, gTaken, plainPanel });
"""


@needs_node
def test_the_editor_shows_what_gemini_heard_and_take_only_fills_the_text_box(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "gemini.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _GEMINI_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    report = next(row for row in json.loads(completed.stdout) if row.get("case") == "gemini")
    panel = report["gemPanel"]
    editor = panel[panel.index('<fieldset class="vl-source-caption-editor">') :]
    assert "Gemini가 들은 말(이 구간 전체): 아 여기 &lt;바다&gt; 예쁘다" in editor
    assert 'data-vl-act="ear-take" data-vl-take="gemini"' in editor
    assert "Gemini가 이 구간에서 다른 말을 들었습니다" in editor
    assert report["gTaken"] == {"value": "아 여기 <바다> 예쁘다", "calls": 0}
    assert "Gemini가 들은 말" not in report["plainPanel"]
