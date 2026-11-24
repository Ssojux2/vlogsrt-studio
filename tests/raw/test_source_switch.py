"""영상을 바꾸면 마지막에 누른 영상이 남는다 — 재생 중에 옛 영상으로 넘어가지 않는다.

영상을 누르면 화면은 곧바로 그 영상으로 바뀌고 서버에 「이 영상을 본다」를 보낸다. 실측 결함:

- 앞 영상(미리보기 준비 중)의 답이 늦게 오면 그 답이 화면을 앞 영상으로 되돌렸고, 정작 마지막에
  누른 영상의 답은 「화면이 그 사이 다시 그려졌다」며 버려졌다 — 다음 틱이 다시 고칠 때까지
  재생 중인 영상이 다른 영상으로 넘어갔다.
- 일괄 승인은 시작할 때 보던 영상을 기억했다가 영상들을 다 띄운 뒤 그 영상을 다시 골랐다 —
  그 사이 사람이 고른 영상이 옛 영상으로 넘어갔다.
- 앞 영상의 미리보기 만들기(무료 로컬)가 계속 돌아 새로 고른 영상의 미리보기를 늦췄다. 이제 다른
  영상을 고르면 그 일을 멈춘다 — 분석(유료)은 멈추지 않는다. 멈추는 중인 작업에 다시 붙으면
  아무것도 만들지 않고 끝나므로, 돌아온 영상은 새 작업으로 다시 만든다.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_background_library import setup_library  # noqa: E402
from tests.raw.test_review_workspace import _wait_terminal  # noqa: E402
from tests.raw.test_selection_analysis import wait_until  # noqa: E402
from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, needs_ffmpeg, needs_node  # noqa: E402
from ui import ui_facade  # noqa: E402
from vlogsrt.project.raw.models import ACTIVE_JOB_STATUSES  # noqa: E402

GATE_SECONDS = 30
PREVIEW_KINDS = frozenset({"source_preview", "visual_prepare"})

_SWITCH_SCRIPT = r"""
const sw = () => windowScope.__vlVisualWorkbenchState;
function resetSwitch() {
  sw().selectedId = null; sw().selectedPlan = null; sw().requestedSource = null;
  sw().lastMsg = ''; sw().filter = 'all';
}
function switchModel() { const m = visualModel(); m.autoAnalyzeSelection = true; return m; }
const card = (id) => ({ getAttribute: (k) => (k === 'data-vl-card' ? id : null) });
const reply = (tag) => ({ ok: true, echoEpoch: 'epoch-1', html: '<div data-reply="' + tag + '">',
  message: tag });
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const holdSelect = { vl_select_source: () => ({ __hold: true }) };

// 미리보기 준비 중인 영상(C)을 누르고 곧바로 B를 누른다 — C의 답이 먼저 온다.
resetSwitch();
const late = visualEnv(holdSelect, switchModel());
late.click(card('segC1'));
late.click(card('segB1'));
const sent = late.serverCalls.map((c) => c.payload.sourceId);
late.held[0].deferred.resolve(reply('C'));
await settle();
const afterOld = late.propsCalls.slice();
const footAfterOld = late.registry['vl-footnote'].textContent;
late.held[1].deferred.resolve(reply('B'));
await settle();
checkpoints.push({ case: 'switch-old-first', sent, afterOld, footAfterOld,
  final: late.propsCalls.slice(), src: late.vid.video.src });

// 같은 두 번 누름 — 마지막 영상(B)의 답이 먼저 오고 C의 답이 뒤에 온다.
resetSwitch();
const early = visualEnv(holdSelect, switchModel());
early.click(card('segC1'));
early.click(card('segB1'));
early.held[1].deferred.resolve(reply('B'));
await settle();
early.held[0].deferred.resolve(reply('C'));
await settle();
checkpoints.push({ case: 'switch-old-last', final: early.propsCalls.slice(),
  src: early.vid.video.src });

// C → B → C — 다시 C로 돌아오면 C의 답이 산다(B의 답만 버린다).
resetSwitch();
const back = visualEnv(holdSelect, switchModel());
back.click(card('segC1'));
back.click(card('segB1'));
back.click(card('segC1'));
back.held.forEach((h, i) => h.deferred.resolve(reply(['C1', 'B', 'C2'][i])));
await settle();
checkpoints.push({ case: 'switch-back', sent: back.serverCalls.map((c) => c.payload.sourceId),
  final: back.propsCalls.slice() });

// 앞 영상 요청의 실패도 마지막 영상의 각주를 덮지 않는다.
resetSwitch();
const failed = visualEnv(holdSelect, switchModel());
failed.click(card('segC1'));
failed.click(card('segB1'));
const footSending = failed.registry['vl-footnote'].textContent;
failed.held[0].deferred.resolve({ ok: false, error: '앞 영상 실패' });
await settle();
checkpoints.push({ case: 'switch-old-failure', footSending,
  foot: failed.registry['vl-footnote'].textContent });
"""


def _run(tmp_path: Path, case: str) -> dict[str, Any]:
    harness = tmp_path / "switch.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _SWITCH_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"visual": ui_facade.VISUAL_BOOTSTRAP_JS}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2500:]
    return next(row for row in json.loads(completed.stdout) if row.get("case") == case)


@needs_node
def test_late_reply_for_the_earlier_video_does_not_take_the_screen_back(tmp_path: Path) -> None:
    report = _run(tmp_path, "switch-old-first")
    assert report["sent"] == ["srcC", "srcB"]
    assert report["afterOld"] == []  # 앞 영상의 답은 화면을 다시 그리지 않는다
    assert report["footAfterOld"] != "C"
    assert report["final"] == ['<div data-reply="B">']  # 마지막 영상의 답이 산다
    assert report["src"] == "/media/preview-b"


@needs_node
def test_reply_for_the_earlier_video_after_the_last_one_is_ignored(tmp_path: Path) -> None:
    report = _run(tmp_path, "switch-old-last")
    assert report["final"] == ['<div data-reply="B">']
    assert report["src"] == "/media/preview-b"


@needs_node
def test_coming_back_to_a_video_keeps_its_replies(tmp_path: Path) -> None:
    report = _run(tmp_path, "switch-back")
    assert report["sent"] == ["srcC", "srcB", "srcC"]
    assert '<div data-reply="B">' not in report["final"]
    assert report["final"] and all("C" in html for html in report["final"])


@needs_node
def test_failure_of_the_earlier_video_does_not_overwrite_the_footnote(tmp_path: Path) -> None:
    report = _run(tmp_path, "switch-old-failure")
    assert report["foot"] == report["footSending"]
    assert "앞 영상 실패" not in report["foot"]


# ── 서버 — 보는 영상은 마지막에 누른 영상이다 ─────────────────────────────────


@needs_ffmpeg
def test_library_approval_keeps_the_video_clicked_while_it_fans_out(tmp_path, monkeypatch):
    ui, _sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    signature, a_id, b_id = ui.current_signature(), ids["a.mov"], ids["b.mov"]
    assert ui._quick["view_source_id"] == a_id
    real = ui._start_quick
    clicked: list[str] = []

    def click_during_fan_out(*args: Any, **kwargs: Any) -> None:
        # 승인 작업이 영상들을 띄우는 도중에 사람이 B를 고른다.
        if kwargs.get("activate") is False and not clicked:
            clicked.append(b_id)
            ui.quick_source_view(signature, b_id)
        real(*args, **kwargs)

    monkeypatch.setattr(ui, "_start_quick", click_during_fan_out)
    ui.library_analysis_view(signature)
    _wait_terminal(ui.raw, root, ui._approval_jobs[root])
    assert clicked == [b_id]
    assert ui._quick["view_source_id"] == b_id
    wait_until(
        lambda: all(
            (ui._task_for(root, (i,)) or {}).get("stage") == "추천 구간 준비 완료"
            for i in ids.values()
        )
    )
    assert ui._quick["view_source_id"] == b_id


def _gate_previews(ui) -> tuple[threading.Event, list[tuple[str, ...]]]:
    """미리보기 본문을 막는다 — 취소를 보지 않고 문이 열릴 때까지 산다(멈추는 중인 작업)."""
    gate = threading.Event()
    entered: list[tuple[str, ...]] = []
    real = ui.raw._prepare_preview_pages

    def gated(root, facade, head, wanted, context):
        entered.append(tuple(wanted))
        assert gate.wait(GATE_SECONDS)
        return real(root, facade, head, wanted, context)

    ui.raw._prepare_preview_pages = gated
    return gate, entered


def _active_preview(ui, root, source_id: str):
    fingerprint_tail = repr((source_id,))
    return [
        job
        for job in ui.raw.active_jobs(root, ("source_preview",))
        if fingerprint_tail in job.input_fingerprint
    ]


@needs_ffmpeg
def test_switching_video_stops_the_previous_preview_and_restarts_on_return(tmp_path, monkeypatch):
    ui, _sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    signature, a_id, b_id = ui.current_signature(), ids["a.mov"], ids["b.mov"]
    gate, entered = _gate_previews(ui)
    try:
        ui.quick_source_view(signature, b_id)
        b_first = wait_until(lambda: _active_preview(ui, root, b_id))[0]
        wait_until(lambda: (b_id,) in entered)
        # 처음 보던 A의 미리보기가 아직 멈추는 중일 수 있다 — 머리 짓기가 빨라 문에 먼저
        # 닿으면(D135) 취소를 보지 않는 문에서 산다. 이 전환이 새로 건 A 작업을 본다(아래 B와 같다).
        older = {job.job_id for job in _active_preview(ui, root, a_id)}
        ui.quick_source_view(signature, a_id)
        assert ui.raw.get_job(root, b_first.job_id).status == "cancel_requested"
        a_job = wait_until(
            lambda: [j for j in _active_preview(ui, root, a_id) if j.job_id not in older]
        )[0]
        assert not a_job.cancellation_requested
        # 멈추는 중인 B 작업이 아직 살아 있을 때 B로 돌아온다 — 새 작업이 만든다.
        ui.quick_source_view(signature, b_id)
        assert ui.raw.get_job(root, a_job.job_id).status == "cancel_requested"
        fresh = [j for j in _active_preview(ui, root, b_id) if j.job_id != b_first.job_id]
        assert len(fresh) == 1 and not fresh[0].cancellation_requested
    finally:
        gate.set()
    assert _wait_terminal(ui.raw, root, fresh[0].job_id).status == "succeeded"
    assert ui._quick["view_source_id"] == b_id
    stopped = [j for j in ui.raw.list_jobs(root, limit=200) if j.cancellation_requested]
    assert stopped and all(j.kind in PREVIEW_KINDS for j in stopped)  # 분석은 멈추지 않는다
    wait_until(
        lambda: all(
            ui.raw.get_job(root, j.job_id).status not in ACTIVE_JOB_STATUSES for j in stopped
        )
    )


@needs_ffmpeg
def test_concurrent_clicks_leave_the_last_clicked_video(tmp_path, monkeypatch):
    ui, _sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    signature, a_id, b_id = ui.current_signature(), ids["a.mov"], ids["b.mov"]
    assert ui._quick["view_source_id"] == a_id
    start = ui._view_click_serial
    replies: dict[str, Any] = {}
    activated: list[str | None] = []
    real_activate = ui._activate_quick

    def recording(state: dict, source_id: str | None = None) -> None:
        activated.append(source_id)
        real_activate(state, source_id)

    monkeypatch.setattr(ui, "_activate_quick", recording)

    def click(source_id: str) -> None:
        replies[source_id] = ui.quick_source_view(signature, source_id)

    # B를 누르고 곧바로 A로 돌아온다 — 두 요청이 함께 들어 어느 쪽이 먼저 바꾸기 문을 지나든
    # 앞 클릭(B)은 영상을 바꾸지 않는다(B로 갔다가 A로 되돌아오는 깜빡임이 없다).
    with ui._view_switch_lock:
        stale = threading.Thread(target=click, args=(b_id,))
        stale.start()
        wait_until(lambda: ui._view_click_serial == start + 1)
        latest = threading.Thread(target=click, args=(a_id,))
        latest.start()
        wait_until(lambda: ui._view_click_serial == start + 2)
    stale.join(GATE_SECONDS)
    latest.join(GATE_SECONDS)
    assert set(replies) == {a_id, b_id}
    assert b_id not in activated
    assert ui._quick["view_source_id"] == a_id
