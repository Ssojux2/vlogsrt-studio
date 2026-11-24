"""D59 19h — 작업대의 「시각 맞춤 제안」 칸: 모델 값 · 적용·되돌리기 서버 함수 · 패널 그리기.

유료 호출은 없다(적용·되돌리기는 무료다). 포트는 7993(시험용)만 쓴다. 시험이 못 박는 것:

- 모델의 `snap`은 상태·sha·센 어긋남·판정 수(막힘을 「그대로」로 세지 않는다)·예측(「같은
  측정으로 잰 예측 — 귀로 확인하세요」)·끝이 크게 움직인 줄부터 다섯을 싣는다. 말은 존댓말이고
  「VAD」·금액·모델 이름이 없다.
- 적용은 수명 토큰 문을 지나고, 화면이 본 sha가 지금 제안과 같을 때만 쓴다 — 다르면 아무것도
  쓰지 않고 새 제안을 그려 보인다. 적용한 뒤에는 「되돌리기」가 그 리비전에 묶여 붙고, 다시 듣기
  칸은 되돌리는 길을 말한다.
- 분석이 도는 영상·다시 듣기 대기에는 적용 단추 대신 그 까닭이 보인다.
- 클라이언트는 누른 순간의 sha·리비전을 보내고(새로 그려진 지 1.5초 안이면 빈 sha), 들어 보기 단추는
  그 구간만 재생하고 끝에서 멈춘다. 글자는 escape된다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
)

from tests._sync_fakes import energy_factory, make_media, numpy_or_fake  # noqa: E402
from tests.raw.test_sync_snap_apply import snap_rows, wait_sync  # noqa: E402
from tests.raw.test_visual_workbench_ui import (  # noqa: E402
    _NODE_HARNESS,
    _model_of,
    _wait_terminal,
)
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.project.raw import asr_epoch, source_transcript, sync_report  # noqa: E402
from vlogsrt.project.raw import service as raw  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")
POLITE_END = re.compile(r"(니다|세요|까요)[.)]*$")


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """검토가 선 작업대 + 톤 덩어리 원본(1.5초 이른 원본 자막·길이 0 줄·조용한 곳의 줄) + 시각
    확인."""
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    numpy_or_fake(monkeypatch)
    server = workspace_service.RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    assert server.create_workspace_view("시각 맞춤 화면", str(tmp_path / "ws"))[1]
    root = server.workspace_root()
    assert root is not None
    make_media(tmp_path / "card" / "tone.mov")
    proposal = server.raw.scan_sources(root, tmp_path / "card")
    handle = server.raw.start_ingest_copy(root, server.raw.confirm_ingest(root, proposal))
    assert _wait_terminal(server, root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    plan = server.raw.plan_segmentation(root, tile_seconds=Fraction(4))
    from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade

    ReviewWorkspaceFacade(root).initialize_review(plan.revision_id)
    asset = server.raw.list_source_assets(root)[0]
    source_transcript.seed(
        server.raw._workspace(root), asset.asset_id, snap_rows(), Fraction(40), asset.content_sha256
    )
    assert server.raw.start_sync_checks(root, (asset.asset_id,)) is not None
    wait_sync(server.raw, root)
    return server, root, asset.asset_id


def _source_model(server: Any, root: Path, source_id: str) -> dict[str, Any]:
    _, html = server.visual_workbench_render(root)
    return _model_of(html)["sources"][source_id]


def _call(
    server: Any, root: Path, name: str, payload: dict[str, Any], *, token: bool = True
) -> Any:
    _, html = server.visual_workbench_render(root)
    model = _model_of(html)
    body = {"epoch": model["epoch"], "signature": model["signature"], **payload}
    if token:
        body["token"] = model["token"]
    return {fn.__name__: fn for fn in server._visual_server_functions()}[name](body)


def _texts(view: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key, value in view.items():
        if key in ("sha", "state"):
            continue
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            found += [
                item for k, item in value.items() if k != "revision" and isinstance(item, str)
            ]
        elif isinstance(value, list):  # 들어 볼 줄 — 문장은 이유뿐이다(판정 말은 표지 낱말)
            found += [row["reason"] for row in value if isinstance(row, dict)]
    return found


# ── 모델 값 ───────────────────────────────────────────────────────────────────


def test_the_model_carries_the_snap_view(bench: tuple[Any, Path, str]) -> None:
    server, root, source_id = bench
    view = _source_model(server, root, source_id)["snap"]
    preview = server.raw.apply_snap_preview(root, source_id)
    assert view["state"] == "ready" and view["sha"] == preview.sha256
    assert view["explanation"] == workspace_service.SNAP_EXPLANATION
    assert view["order"] == workspace_service.SNAP_ORDER_WARNING
    assert view["undoRule"] == workspace_service.SNAP_UNDO_RULE and "undo" not in view
    seconds = f"{float(preview.shift):.3f}"
    assert view["shift"] == (
        workspace_service.SNAP_SHIFT_LATE.format(seconds=seconds, count=14)
        + " "
        + workspace_service.SNAP_SHIFT_HELD.format(count=1)
    )
    assert view["counts"] == workspace_service.SNAP_COUNTS.format(
        total=15, moved=13, unchanged=0, blocked=2, no_speech=0
    )
    assert view["changes"] == (
        workspace_service.SNAP_CHANGES.format(changes=14)
        + " "
        + workspace_service.SNAP_SHIFT_ONLY.format(count=1)
    )
    assert view["prediction"] == "말소리 측정으로 다시 재면 지금 빨강에서 적용 뒤 초록입니다."
    assert view["predictionLabel"] == "같은 측정으로 잰 예측 — 귀로 확인하세요"
    rows = view["listen"]
    assert len(rows) == raw.SNAP_LISTEN_FIRST
    assert view["listenNote"] == workspace_service.SNAP_LISTEN_NOTE.format(count=5)
    ends = [abs(Fraction(r["newEnd"]) - Fraction(r["oldEnd"])) for r in rows]
    assert ends == sorted(ends, reverse=True)  # 끝이 크게 움직인 줄부터
    first = rows[0]
    assert first["label"] == "맞춤" and "moves" not in first  # 움직임은 이유 문장이 한 번만 말한다
    assert first["reason"].startswith("말소리 시작과 끝에 맞췄습니다 — 시작 +")
    assert Fraction(first["oldStart"]) < Fraction(first["newStart"])
    for text in _texts(view):
        assert POLITE_END.search(text), text
        assert "$" not in text and "VAD" not in text and "whisper" not in text.lower()
    for row in rows:
        assert row["label"] in ("맞춤", "그대로") or "밀기만" in row["label"]


def test_a_moving_blocked_row_reads_as_shifted_never_as_unchanged() -> None:
    cue = raw.SnapCue(
        cue_id="q",
        text="조용",
        old_start=Fraction(1),
        old_end=Fraction(2),
        new_start=Fraction(5, 2),
        new_end=Fraction(7, 2),
        status="blocked",
        shift_only=True,
        reason="말소리 경계에 맞추면 시작이 끝보다 늦어져 맞추지 않았습니다.",
    )
    row = workspace_service._snap_listen_row(cue, Fraction(3, 2))
    assert row["label"] == "막힘 · 밀기만(+1.500초)" and "그대로" not in row["label"]
    assert "moves" not in row and row["reason"] == cue.reason
    moved = workspace_service._snap_label(
        raw.SnapCue("m", "", Fraction(0), Fraction(1), Fraction(0), Fraction(1), "moved", True, ""),
        Fraction(-1, 3000),
    )
    assert moved == "밀기만(0.001초 미만 앞당김)"


def test_waiting_states_show_the_reason_and_no_ready_state(
    bench: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, source_id = bench
    asr_epoch.bump(server.raw._workspace(root), source_id)
    view = _source_model(server, root, source_id)["snap"]
    assert view["state"] == "waiting" and view["reason"] == raw.SNAP_RELISTEN_PENDING
    asr_epoch.withdraw(server.raw._workspace(root), source_id)
    monkeypatch.setattr(server, "_source_busy", lambda *_: True)
    view = _source_model(server, root, source_id)["snap"]
    assert view["state"] == "waiting" and view["reason"] == workspace_service.SNAP_BUSY_TEXT
    refused = _call(server, root, "vl_snap_apply", {"sourceId": source_id, "sha": view["sha"]})
    assert refused == {"ok": False, "error": workspace_service.SNAP_BUSY_TEXT}
    assert source_transcript.load(server.raw._workspace(root), source_id)["sequence"] == 0


# ── 적용·되돌리기 — 토큰 · 본 sha · 리비전 ──────────────────────────────────────


def test_apply_and_undo_through_the_server_functions(bench: tuple[Any, Path, str]) -> None:
    server, root, source_id = bench
    workspace = server.raw._workspace(root)
    view = _source_model(server, root, source_id)["snap"]
    payload = {"sourceId": source_id, "sha": view["sha"]}
    assert _call(server, root, "vl_snap_apply", payload, token=False)["ok"] is False
    stale = _call(server, root, "vl_snap_apply", {**payload, "sha": "0" * 64})
    assert stale["ok"] is True and stale["message"] == raw.SNAP_CHANGED
    assert _model_of(stale["html"])["sources"][source_id]["snap"]["sha"] == view["sha"]
    assert source_transcript.load(workspace, source_id)["sequence"] == 0  # 아무것도 쓰지 않았다
    applied = _call(server, root, "vl_snap_apply", payload)
    assert applied["ok"] is True
    assert applied["message"] == workspace_service.SNAP_APPLIED_TEXT.format(changed=14)
    current = source_transcript.load(workspace, source_id)
    model = _model_of(applied["html"])["sources"][source_id]
    assert model["snap"]["undo"] == {
        "revision": current["revision"],
        "note": workspace_service.SNAP_UNDO_NOTE.format(changed=14),
    }
    assert model["snap"]["state"] == "applied"
    assert model["relisten"]["snapped"] == workspace_service.RELISTEN_SNAPPED_TEXT
    relisten = _call(server, root, "vl_relisten", {"sourceId": source_id})
    assert relisten == {"ok": False, "error": raw.RELISTEN_SNAPPED_REFUSAL}
    wrong = _call(server, root, "vl_snap_revert", {"sourceId": source_id, "revision": "x"})
    assert wrong["ok"] is True and wrong["message"] == source_transcript.UNDO_CHANGED
    undone = _call(
        server, root, "vl_snap_revert", {"sourceId": source_id, "revision": current["revision"]}
    )
    assert undone["ok"] is True and undone["message"] == workspace_service.SNAP_REVERTED_TEXT
    restored = source_transcript.load(workspace, source_id)
    assert restored["edited"] is False and restored["sequence"] == 2
    model = _model_of(undone["html"])["sources"][source_id]
    assert "undo" not in model["snap"] and "snapped" not in model["relisten"]
    wait_sync(server.raw, root)


def test_an_applied_revision_shows_only_the_undo_and_refuses_a_second_apply(
    bench: tuple[Any, Path, str],
) -> None:
    """한 걸음 규칙 — 적용한 리비전의 칸은 적용한 상태·되돌리기·안내 한 줄뿐이다(새 제안·적용 단추·
    판정 수·들어 볼 줄·순서 경고 없음). 낡은 탭의 두 번째 적용은 쓰지 않는다(19h 검증 뒤 고침 1)."""
    server, root, source_id = bench
    workspace = server.raw._workspace(root)
    payload = {"sourceId": source_id, "sha": _source_model(server, root, source_id)["snap"]["sha"]}
    assert _call(server, root, "vl_snap_apply", payload)["ok"] is True
    for measured in (False, True):
        if measured:
            wait_sync(server.raw, root)  # 다시 잰 뒤에도 적용한 리비전에는 새 제안이 없다
        view = _source_model(server, root, source_id)["snap"]
        assert view["state"] == "applied" and view["reason"] == raw.SNAP_APPLIED_ONCE
        assert not {"sha", "counts", "changes", "listen", "order", "undoRule"} & set(view)
        again = _call(server, root, "vl_snap_apply", payload)  # 두 번 누름·다른 탭
        assert again["ok"] is True and again["message"] == source_transcript.SNAP_ALREADY_APPLIED
        assert source_transcript.load(workspace, source_id)["sequence"] == 1


# ── 클라이언트 — node에서 실제 실행본으로 ─────────────────────────────────────

_SNAP_SCRIPT = r"""
let fakeNow = 5000000;
Date.now = () => fakeNow;
const snapModel = visualModel();
snapModel.epoch = 'snap-1'; snapModel.token = 'snap-token';
const listenRow = { id: 'c05', text: '말 <b>5</b>', oldStart: '131/10', oldEnd: '139/10',
  newStart: '3623/250', newEnd: '3931/250', label: '맞춤', moves: '시작 +1.392초 · 끝 +1.824초',
  reason: '말소리 시작과 끝에 맞췄습니다 & 옮겼습니다.' };
snapModel.sources.srcA.snap = {
  state: 'ready', sha: 'a'.repeat(64), explanation: '설명 <i>', order: '순서 경고',
  undoRule: '되돌림 안내', shift: '자막이 말보다 1.504초 이릅니다.', counts: '제안 15줄입니다.',
  changes: '바뀌는 줄 14개입니다.', prediction: '빨강에서 초록입니다.',
  predictionLabel: '같은 측정으로 잰 예측 — 귀로 확인하세요',
  listenNote: '먼저 들어 볼 줄 1개입니다.',
  listen: [listenRow], undo: { revision: 'rev-<1>', note: '적용한 자막입니다.' },
};
snapModel.sources.srcA.relisten = { explanation: '다시 듣기 설명', pending: false, edited: true,
  heard: false, snapped: '시각 맞춤을 적용한 자막이라 다시 듣지 않습니다 — 되돌리면 됩니다.' };
const sent = [];
const handlers = {
  vl_snap_apply: (payload) => { sent.push({ fn: 'apply', sha: payload.sha,
    sourceId: payload.sourceId, token: payload.token }); return { ok: true, echoEpoch: 'snap-1',
    message: '접수' }; },
  vl_snap_revert: (payload) => { sent.push({ fn: 'revert', revision: payload.revision,
    sourceId: payload.sourceId }); return { ok: true, echoEpoch: 'snap-1', message: '접수' }; },
};
const envSnap = visualEnv(handlers, snapModel);
const panelHtml = envSnap.panel.innerHTML;
const press = (target) => (envSnap.root._listeners.pointerdown || []).slice()
  .forEach((fn) => fn({ target }));
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const applyBtn = (sha) => el({ attrs: { 'data-vl-act': 'snap-apply', 'data-vl-sha': sha } });
// 1) 그린 바로 뒤 — 들을 틈 전이라 빈 sha(서버가 거절하고 새 제안을 그린다).
let button = applyBtn('a'.repeat(64));
press(button); envSnap.click(button); await flush();
// 2) 틈이 지난 뒤 — 누른 순간의 sha.
fakeNow += 2000;
button = applyBtn('a'.repeat(64));
press(button); envSnap.click(button); await flush();
// 3) 누른 뒤 1초 틱이 새 제안(b…)을 그려도 누른 순간의 sha(a…)를 보낸다.
fakeNow += 2000;
press(applyBtn('a'.repeat(64)));
snapModel.sources.srcA.snap = Object.assign({}, snapModel.sources.srcA.snap,
  { sha: 'b'.repeat(64) });
envSnap.root.attrs['data-vl-model'] = JSON.stringify(snapModel);
envSnap.rewired();
envSnap.click(applyBtn('b'.repeat(64))); await flush();
// 4) 되돌리기 — 그 리비전에 묶인다.
const revert = el({ attrs: { 'data-vl-act': 'snap-revert', 'data-vl-revision': 'rev-<1>' } });
press(revert); envSnap.click(revert); await flush();
// 5) 들어 보기 — 옛 시각 구간만 재생하고 끝에서 멈춘다. 끝은 화면 프레임마다 본다.
const frames = [];
windowScope.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
const nextFrame = () => { const fn = frames.shift(); if (fn) { fn(); } };
envSnap.click(el({ attrs: { 'data-vl-act': 'snap-listen', 'data-vl-from': '131/10',
  'data-vl-to': '139/10' } }));
const seekAt = envSnap.vid.state.currentTime;
const playing = !envSnap.vid.state.paused;
envSnap.vid.state.currentTime = 13.5; nextFrame();
const midPaused = envSnap.vid.state.paused;
const midFrames = frames.length;
envSnap.vid.state.currentTime = 13.92; nextFrame();
const frameStop = { paused: envSnap.vid.state.paused, at: envSnap.vid.state.currentTime,
  frames: frames.length };
// 6) 프레임 콜백이 없으면 틱(timeupdate)이 멈춘다 — 제안 시각 구간.
delete windowScope.requestAnimationFrame;
envSnap.click(el({ attrs: { 'data-vl-act': 'snap-listen', 'data-vl-from': '3623/250',
  'data-vl-to': '3931/250' } }));
const proposedAt = envSnap.vid.state.currentTime;
envSnap.vid.tick(15.2);
const tickMid = envSnap.vid.state.paused;
envSnap.vid.tick(15.8);
checkpoints.push({ case: 'snap', panelHtml, sent, seekAt, playing, midPaused, midFrames,
  frameStop, proposedAt, tickMid, tickEnd: envSnap.vid.state.paused,
  plays: envSnap.vid.calls.play });
// 7) 적용한 리비전의 칸 — 적용한 상태·되돌리기·안내 한 줄만.
const appliedModel = visualModel();
appliedModel.epoch = 'snap-2';
appliedModel.sources.srcA.snap = { state: 'applied', explanation: '설명',
  undo: { revision: 'rev-2', note: '적용한 자막입니다.' },
  reason: '이미 시각 맞춤을 적용한 자막입니다 — 다시 맞추려면 「되돌리기」로 돌리세요.' };
const envApplied = visualEnv({}, appliedModel);
checkpoints.push({ case: 'snap-applied', panelHtml: envApplied.panel.innerHTML });
"""


def _run_snap_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "snap.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _SNAP_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    rows = json.loads(completed.stdout)
    report = next(row for row in rows if row.get("case") == "snap")
    applied = next(row for row in rows if row.get("case") == "snap-applied")
    return {**report, "appliedHtml": applied["panelHtml"]}


@needs_node
def test_the_panel_binds_the_pressed_sha_and_the_listen_buttons_play_one_span(
    tmp_path: Path,
) -> None:
    report = _run_snap_harness(tmp_path)
    panel = report["panelHtml"]
    assert '<details class="vl-details" data-vl-snap="1" open><summary>시각 맞춤 제안' in panel
    assert '<span class="vl-badge">적용함</span>' in panel
    assert "설명 &lt;i&gt;" in panel and "말 &lt;b&gt;5&lt;/b&gt;" in panel  # 글은 escape된다
    assert "맞췄습니다 &amp; 옮겼습니다." in panel
    assert (
        'data-vl-act="snap-listen" data-vl-from="131/10" data-vl-to="139/10">'
        "옛 시각 13.100–13.900초</button>"
    ) in panel
    assert 'data-vl-from="3623/250" data-vl-to="3931/250">제안 14.492–15.724초</button>' in panel
    assert (
        'data-vl-act="snap-apply" data-vl-sha="' + "a" * 64 + '">시각 맞춤 적용(무료)</button>'
        in panel
    )
    assert 'data-vl-act="snap-revert" data-vl-revision="rev-&lt;1&gt;">되돌리기</button>' in panel
    assert "같은 측정으로 잰 예측 — 귀로 확인하세요" in panel and "순서 경고" in panel
    # 다시 듣기 칸 — 시각 맞춤만 얹은 자막이면 단추 대신 되돌리는 길을 말한다.
    assert "시각 맞춤을 적용한 자막이라 다시 듣지 않습니다" in panel
    assert 'data-vl-act="relisten"' not in panel
    fresh, read, swapped, revert = report["sent"]
    assert fresh == {"fn": "apply", "sha": "", "sourceId": "srcA", "token": "snap-token"}
    assert read["sha"] == "a" * 64
    assert swapped["sha"] == "a" * 64  # 누른 뒤 다시 그려진 제안이 아니라 누른 순간의 제안
    assert revert == {"fn": "revert", "revision": "rev-<1>", "sourceId": "srcA"}
    assert report["seekAt"] == pytest.approx(13.1) and report["playing"] is True
    assert report["midPaused"] is False and report["midFrames"] == 1  # 다음 프레임을 또 본다
    assert report["frameStop"] == {"paused": True, "at": 13.92, "frames": 0}  # 끝 프레임에서 멈춘다
    assert report["proposedAt"] == pytest.approx(14.492) and report["plays"] == 2
    assert report["tickMid"] is False and report["tickEnd"] is True  # 틱 대체 길
    applied = report["appliedHtml"]
    assert '<span class="vl-badge">적용함</span>' in applied and ">되돌리기</button>" in applied
    assert "이미 시각 맞춤을 적용한 자막입니다" in applied
    assert 'data-vl-act="snap-apply"' not in applied and 'data-vl-act="snap-listen"' not in applied
    assert '<p class="vl-kv"><small></small></p>' not in applied  # 빈 순서 경고 줄이 없다


def test_the_blocks_wire_the_snap_functions_and_the_panel() -> None:
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    assert "function snapPanel" in script and "out.push(snapPanel(info));" in script
    assert script.index("out.push(syncPanel(info));") < script.index("out.push(snapPanel(info));")
    assert "call('vl_snap_apply'" in script and "call('vl_snap_revert'" in script
    assert workspace_service.SNAP_APPLY_LABEL + "</button>" in script
    assert ">" + workspace_service.SNAP_REVERT_LABEL + "</button>" in script
    assert {"snap-apply", "snap-revert"} <= set(workspace_service.WORKBENCH_TOKEN_ACTIONS)
    assert "sync_snap" not in script and "source_transcript" not in script


def _new_texts() -> list[str]:
    """19h가 더한 화면 말 전부(값을 채운 꼴)."""
    return [
        workspace_service.SNAP_EXPLANATION,
        workspace_service.SNAP_PREDICTION_LABEL,
        workspace_service.SNAP_PREDICTION.format(before="빨강", after="초록"),
        workspace_service.SNAP_PREDICTION_SAME.format(before="노랑"),
        workspace_service.SNAP_SHIFT_LATE.format(seconds="3.872", count=53),
        workspace_service.SNAP_SHIFT_EARLY.format(seconds="1.500", count=2),
        workspace_service.SNAP_SHIFT_HELD.format(count=8),
        workspace_service.SNAP_COUNTS.format(
            total=53, moved=43, unchanged=0, blocked=6, no_speech=4
        ),
        workspace_service.SNAP_CHANGES.format(changes=53),
        workspace_service.SNAP_SHIFT_ONLY.format(count=10),
        workspace_service.SNAP_LISTEN_NOTE.format(count=5),
        workspace_service.SNAP_ORDER_WARNING,
        workspace_service.SNAP_UNDO_RULE,
        workspace_service.SNAP_UNDO_NOTE.format(changed=53),
        workspace_service.SNAP_NONE_TEXT,
        workspace_service.SNAP_BUSY_TEXT,
        workspace_service.SNAP_APPLIED_TEXT.format(changed=53),
        workspace_service.SNAP_REVERTED_TEXT,
        workspace_service.RELISTEN_SNAPPED_TEXT,
    ]


def _service_texts() -> list[str]:
    """서비스·쓰기 계층이 더한 말 — 거절·까닭."""
    return [
        raw.SNAP_UNTRANSCRIBED,
        raw.SNAP_WAITING,
        raw.SNAP_UNMEASURED,
        raw.SNAP_SPEECH_MISSING,
        raw.SNAP_RELISTEN_PENDING,
        raw.SNAP_REFILL_RUNNING,
        raw.SNAP_CHANGED,
        raw.SNAP_NOTHING_CHANGED,
        raw.SNAP_ANALYSIS_RUNNING,
        raw.SNAP_APPLIED_ONCE,
        source_transcript.SNAP_ALREADY_APPLIED,
        raw.RELISTEN_SNAPPED_REFUSAL,
        source_transcript.SNAP_REVISION_CHANGED,
        source_transcript.SNAP_ROWS_MISMATCH,
        source_transcript.SNAP_NO_CHANGE,
        source_transcript.UNDO_CHANGED,
        source_transcript.UNDO_NOTHING,
        source_transcript.UNDO_MISSING,
    ]


def test_waiting_copy_points_at_no_missing_button() -> None:
    """기다림 상태의 말은 화면에 없는 단추를 가리키지 않는다(D57) — 그때는 적용 단추가 없다."""
    for text in (
        raw.SNAP_REFILL_RUNNING,
        raw.SNAP_ANALYSIS_RUNNING,
        workspace_service.SNAP_BUSY_TEXT,
    ):
        assert "눌러" not in text and "누르" not in text
    assert "화면과 다릅니다 — 그사이" not in raw.SNAP_CHANGED  # 까닭을 지어내지 않는다


def test_new_wording_is_polite_and_names_no_model_or_price() -> None:
    for text in [*_new_texts(), *_service_texts()]:
        assert POLITE_END.search(text), text
        assert "$" not in text and "VAD" not in text and "whisper" not in text.lower()
        assert "GPT" not in text and "Claude" not in text


# ── 고르지 않은 영상의 패널 칸(W-71, D122) — 볼 때 읽어 와 합친다 ─────────────────

_DETAIL_SCRIPT = r"""
const detailModel = visualModel();
detailModel.epoch = 'detail-1';
Object.assign(detailModel.sources.srcA, { transcriptRevision: 'r1', subtitles: [],
  detail: false, syncCheck: { tone: 'amber', level: 'amber', label: '확인 권함', stale: false } });
const fetched = { snap: { state: 'waiting', explanation: '맞춤 설명', reason: '기다리는 까닭' },
  relisten: { explanation: '다시 듣기 설명', pending: false, edited: false, heard: false } };
const envDetail = visualEnv({ vl_source_detail: (payload) => ({ ok: true, echoEpoch: payload.epoch,
  sourceId: payload.sourceId, detail: fetched }) }, detailModel);
const loadingHtml = envDetail.panel.innerHTML;
await new Promise((resolve) => setTimeout(resolve, 0));
const asked = envDetail.serverCalls.filter((c) => c.name === 'vl_source_detail');
envDetail.rewired();
checkpoints.push({ case: 'detail', loadingHtml, filledHtml: envDetail.panel.innerHTML,
  asked: asked.map((c) => c.payload),
  askedAfterRewire: envDetail.serverCalls.filter((c) => c.name === 'vl_source_detail').length });
const failModel = visualModel();
failModel.epoch = 'detail-2';
Object.assign(failModel.sources.srcA, { transcriptRevision: 'r1', subtitles: [], detail: false });
const envFail = visualEnv({ vl_source_detail: (payload) => ({ ok: false,
  error: '작업공간이 바뀌었습니다', echoEpoch: payload.epoch }) }, failModel);
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'detail-fail', failHtml: envFail.panel.innerHTML });
const fullModel = visualModel();
fullModel.epoch = 'detail-3';
const envFull = visualEnv({ vl_source_detail: () => ({ ok: true }) }, fullModel);
checkpoints.push({ case: 'detail-full', calls: envFull.serverCalls.length,
  html: envFull.panel.innerHTML });
"""


def _run_detail_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "detail.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _DETAIL_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    rows = json.loads(completed.stdout)
    return {row["case"]: row for row in rows if str(row.get("case", "")).startswith("detail")}


@needs_node
def test_an_unselected_video_fetches_its_panels_once_and_says_so_while_waiting(
    tmp_path: Path,
) -> None:
    report = _run_detail_harness(tmp_path)
    detail = report["detail"]
    assert "불러오는 중입니다" in detail["loadingHtml"]
    assert "맞춤 설명" not in detail["loadingHtml"]
    assert detail["asked"] == [{"sourceId": "srcA", "epoch": "detail-1", "signature": "ws-sig"}]
    assert "맞춤 설명" in detail["filledHtml"] and "다시 듣기 설명" in detail["filledHtml"]
    assert "불러오는 중" not in detail["filledHtml"]
    assert detail["askedAfterRewire"] == 1  # 같은 화면(epoch)에서는 다시 묻지 않는다
    failed = report["detail-fail"]["failHtml"]
    assert "불러오지 못했습니다 — 작업공간이 바뀌었습니다" in failed
    assert "불러오는 중" not in failed
    full = report["detail-full"]  # 칸을 다 실은 영상(고른 영상)은 묻지 않는다
    assert full["calls"] == 0 and "불러오" not in full["html"]
