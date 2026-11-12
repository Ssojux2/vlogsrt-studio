"""D59 19f — 작업대의 「말소리 빈칸 재전사」 칸: 모델 값 · 승인 대조(보이는 견적) · 패널 그리기.

유료 호출은 없다 — 승인이 접수되면 가짜 전송이 받는다(환경 문은 시험 안에서만 가짜로 연다).
포트는 7993(시험용)만 쓴다. 시험이 못 박는 것:

- 모델의 `refill`은 상태·빈칸·순서 경고·견적 문구·digest를 싣고, 금액·모델 이름은 견적 문구 안에만
  있다(D57). 화면 말에 「VAD」가 없다.
- 승인은 수명 토큰 문을 지나고, 보이는 견적 문구와 digest가 지금 견적과 같을 때만 작업을 건다 —
  다른 탭·바뀐 용어집이면 「새 견적을 확인하세요」와 0송신이다. 키가 없으면 시작하지 않는다.
- 접수되면 작업이 빈칸을 넣고 원본 자막은 고친 자막이 된다(다시 듣기 칸이 그것을 말한다).
- 클라이언트는 그려진 견적 문구를 그대로 보내고, 빈칸 단추는 재생 머리를 옮길 뿐이다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
)

from tests._sync_fakes import energy_factory, numpy_or_fake  # noqa: E402
from tests.raw.test_speech_refill import RefillSender, transport  # noqa: E402
from tests.raw.test_speech_refill_edges import Blocking  # noqa: E402
from tests.raw.test_visual_workbench_ui import (  # noqa: E402
    _NODE_HARNESS,
    _model_of,
    _prepare_review,
)
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.project.raw import glossary, source_transcript, sync_report  # noqa: E402
from vlogsrt.project.raw import service as raw  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
POLITE_END = re.compile(r"(니다|세요|까요)[.)]*$")


def wait_kind(server: Any, root: Path, kind: str) -> Any:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        latest = server.raw.latest_job(root, kind)
        if latest is not None and latest.status in TERMINAL:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"작업이 끝나지 않았다: {kind}")


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """검토가 선 작업대 + 4초 사인 원본에 원본 자막 세 줄(0–2.8초) — 3.3–4초는 자막 없는 말소리."""
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    numpy_or_fake(monkeypatch)
    server = workspace_service.RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    assert server.create_workspace_view("빈칸 화면", str(tmp_path / "ws"))[1]
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    asset = server.raw.list_source_assets(root)[0]
    rows = [
        {
            "utterance_id": f"u{index}",
            "text": f"말 {index}",
            "start": source_transcript.encoded(Fraction(index)),
            "end": source_transcript.encoded(Fraction(index) + Fraction(4, 5)),
        }
        for index in range(3)
    ]
    source_transcript.seed(
        server.raw._workspace(root), asset.asset_id, rows, Fraction(4), asset.content_sha256
    )
    assert server.raw.start_sync_checks(root, (asset.asset_id,)) is not None
    assert wait_kind(server, root, raw.SYNC_CHECK_JOB_KIND).status == "succeeded"
    return server, root, asset.asset_id


def _refill_of(server: Any, root: Path, source_id: str) -> dict[str, Any]:
    _, html = server.visual_workbench_render(root)
    return _model_of(html)["sources"][source_id]["refill"]


def _approve(server: Any, root: Path, payload: dict[str, Any], *, token: bool = True) -> Any:
    _, html = server.visual_workbench_render(root)
    model = _model_of(html)
    body = {"epoch": model["epoch"], "signature": model["signature"], **payload}
    if token:
        body["token"] = model["token"]
    function = {fn.__name__: fn for fn in server._visual_server_functions()}["vl_refill"]
    return function(body)


def _texts(view: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key, value in view.items():
        if key in ("digest", "state"):  # 기계 값은 따로 본다
            continue
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found += [item for item in value if isinstance(item, str)]
    return found


# ── 모델 값 ───────────────────────────────────────────────────────────────────


def test_the_model_carries_the_refill_view_without_any_quote_or_price(
    bench: tuple[Any, Path, str],
) -> None:
    server, root, source_id = bench
    view = _refill_of(server, root, source_id)
    assert view["state"] == "ready", view
    scope = server.raw.refill_scope(root, source_id)
    assert view["digest"] == scope.digest and "quote" not in view  # 견적 문구가 없다(D110)
    assert view["explanation"] == workspace_service.REFILL_EXPLANATION
    assert view["order"] == workspace_service.REFILL_ORDER_WARNING
    assert len(view["spans"]) == 1 and view["more"] == 0
    span = view["spans"][0]
    assert Fraction(span["start"]) > Fraction(14, 5) and Fraction(span["end"]) <= 4
    texts = _texts(view)
    assert texts and all("$" not in text for text in texts)  # 금액이 없다(D110)
    for text in texts:
        assert "VAD" not in text and "whisper" not in text and "GPT-Transcribe" not in text
        assert POLITE_END.search(text), text
    # 이 원본에는 1단계 기록이 없다(손으로 심은 자막) — 실패 구간을 모르니
    # 「실패 구간 안 0.0초」라고 말하지 않고 그렇다고 밝힌다(검증 지적 8a).
    assert "실패한 구간 안" not in view["summary"]
    assert view["summary"].endswith("입니다. " + workspace_service.REFILL_SUMMARY_UNRECORDED)
    assert workspace_service.REFILL_NOTE_UNRECORDED in view["notes"]
    assert view["skipped"] == [] and "skippedNote" not in view


def test_the_relisten_pending_and_the_running_states_have_no_approve_digest(
    bench: tuple[Any, Path, str],
) -> None:
    server, root, source_id = bench
    workspace = server.raw._workspace(root)
    from vlogsrt.project.raw import asr_epoch

    asr_epoch.bump(workspace, source_id)
    view = _refill_of(server, root, source_id)
    assert view["state"] == "blocked" and view["reason"] == raw.REFILL_RELISTEN_PENDING
    assert "digest" not in view


# ── 승인 대조 — 토큰 · 범위 digest · 키 · 0송신 ────────────────────────────────


def test_approval_needs_the_token_and_the_current_scope(
    bench: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, source_id = bench
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    sender = RefillSender()
    server._live_transport = transport(sender)
    view = _refill_of(server, root, source_id)
    payload = {"sourceId": source_id, "digest": view["digest"]}
    assert _approve(server, root, payload, token=False)["ok"] is False  # 토큰 없음
    stale = _approve(server, root, {**payload, "digest": "0" * 64})
    assert stale["ok"] is True and stale["message"] == workspace_service.REFILL_CHANGED_TEXT
    glossary.save(server.raw._workspace(root), "왕방")  # 다른 탭이 용어집을 바꿨다
    moved = _approve(server, root, payload)
    assert moved["message"] == workspace_service.REFILL_CHANGED_TEXT
    assert _model_of(moved["html"])["sources"][source_id]["refill"]["digest"] != view["digest"]
    assert server.raw.latest_job(root, raw.REFILL_JOB_KIND) is None
    assert sender.whisper == [] and sender.text == []  # 0송신


def test_without_a_key_nothing_starts(bench: tuple[Any, Path, str]) -> None:
    server, root, source_id = bench
    # 시작 환경의 키는 서버가 띄워질 때 찍힌다 — 앞선 시험이 프로세스 환경에 남긴 시험용 키에 기대지
    # 않게 「키 없음」을 그대로 세운다(주입 transport도 없다).
    server._startup_credentials = None
    server._live_transport = None
    view = _refill_of(server, root, source_id)
    payload = {"sourceId": source_id, "digest": view["digest"]}
    refused = _approve(server, root, payload)
    assert refused == {"ok": False, "error": workspace_service.REFILL_KEY_TEXT}
    assert server.raw.latest_job(root, raw.REFILL_JOB_KIND) is None


def test_an_approved_refill_inserts_rows_and_makes_the_transcript_edited(
    bench: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, source_id = bench
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    sender = RefillSender()
    server._live_transport = transport(sender)
    before = server.raw.source_transcript(root, source_id)
    view = _refill_of(server, root, source_id)
    reply = _approve(server, root, {"sourceId": source_id, "digest": view["digest"]})
    assert reply["ok"] is True and reply["message"] == workspace_service.REFILL_STARTED_TEXT
    record = wait_kind(server, root, raw.REFILL_JOB_KIND)
    assert record.status == "succeeded", record.error
    assert len(sender.whisper) == 1 and len(sender.text) == 1
    after = server.raw.source_transcript(root, source_id)
    assert after["edited"] is True and len(after["utterances"]) == len(before["utterances"]) + 1
    assert after["utterances"][:3] == before["utterances"]  # 기존 줄 그대로
    model = _model_of(server.visual_workbench_render(root)[1])["sources"][source_id]
    assert model["relisten"]["edited"] is True  # 다시 듣기 칸이 고친 자막이라 말한다
    assert "넣은 줄 1개" in model["refill"]["last"]
    assert "실행 기록에" not in model["refill"]["last"]  # 넣지 않은 줄이 없으면 그 말도 없다
    assert model["refill"]["skipped"] == []


def test_relisten_waits_while_a_refill_runs_and_both_panels_say_so(
    bench: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """빈칸 재전사가 도는 동안 「다시 듣기」는 받지 않는다 — 두 칸이 어긋나지 않게(검증 지적 1)."""
    server, root, source_id = bench
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    sender = Blocking()
    server._live_transport = transport(sender)
    view = _refill_of(server, root, source_id)
    payload = {"sourceId": source_id, "digest": view["digest"]}
    assert _approve(server, root, payload)["message"] == workspace_service.REFILL_STARTED_TEXT
    assert sender.entered.wait(30)
    model = _model_of(server.visual_workbench_render(root)[1])["sources"][source_id]
    assert model["refill"]["state"] == "running"
    assert model["relisten"]["refillRunning"] == workspace_service.RELISTEN_REFILL_RUNNING_TEXT
    _, html = server.visual_workbench_render(root)
    relisten = {fn.__name__: fn for fn in server._visual_server_functions()}["vl_relisten"]
    page = _model_of(html)
    refused = relisten(
        {"epoch": page["epoch"], "signature": page["signature"], "token": page["token"],
         "sourceId": source_id}
    )  # fmt: skip
    assert refused == {"ok": False, "error": raw.RELISTEN_REFILL_RUNNING_REFUSAL}
    sender.release.set()
    assert wait_kind(server, root, raw.REFILL_JOB_KIND).status == "succeeded"
    after = _model_of(server.visual_workbench_render(root)[1])["sources"][source_id]
    assert "refillRunning" not in after["relisten"]  # 돌지 않으면 다시 듣기 칸 값은 19b 그대로
    from vlogsrt.project.raw import asr_epoch

    assert asr_epoch.state(server.raw._workspace(root), source_id).epoch == 0


def test_skipped_rows_are_shown_with_their_text_and_the_note_only_when_any(
    bench: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """겹쳐 넣지 않은 줄은 칸에 글자·시각째 보인다 — 그 조각은 다시 묻지 않는다(검증 지적 6)."""
    server, root, source_id = bench
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    server.raw.set_cross_check(root, False)
    sender = RefillSender(
        [
            {"start": 0.5, "end": 1.0, "text": "앞 말"},
            {"start": 0.8, "end": 1.1, "text": "<겹친> 말"},
        ]
    )
    server._live_transport = transport(sender)
    view = _refill_of(server, root, source_id)
    payload = {"sourceId": source_id, "digest": view["digest"]}
    reply = _approve(server, root, payload)
    assert reply["ok"] is True, reply
    assert wait_kind(server, root, raw.REFILL_JOB_KIND).status == "succeeded"
    after = _refill_of(server, root, source_id)
    (row,) = after["skipped"]
    assert row["text"] == "<겹친> 말" and row["why"] == workspace_service.REFILL_SKIPPED_OVERLAP
    assert Fraction(row["start"]) < Fraction(row["end"])
    assert after["skippedNote"] == workspace_service.REFILL_SKIPPED_NOTE.format(count=1)
    assert "넣지 않은 줄 1개" in after["last"] and "실행 기록에" in after["last"]


# ── 클라이언트 — node에서 실제 실행본으로 ─────────────────────────────────────

_REFILL_SCRIPT = r"""
const refillModel = visualModel();
refillModel.epoch = 'refill-1'; refillModel.token = 'refill-token';
refillModel.sources.srcA.refill = {
  state: 'ready', explanation: '설명 <b>', order: '순서 경고', summary: '요약',
  spans: [{ start: '33/10', end: '4', failed: false }, { start: '1', end: '2', failed: true }],
  more: 3, notes: ['실패 구간 안내'], digest: 'd'.repeat(64), last: '지난 결과',
  skipped: [{ start: '7/2', end: '19/5', text: '<겹친> 말', why: '기존 자막과 겹침' }],
  skippedNote: '넣지 않은 줄 1개 — 그 구간은 다시 묻지 않습니다.', skippedMore: 2,
};
refillModel.sources.srcA.relisten = { explanation: '다시 듣기 설명', pending: false,
  edited: false, heard: false, refillRunning: '빈칸 재전사가 도는 중이라 지금은 받지 않습니다.' };
const sent = [];
const envRefill = visualEnv({ vl_refill: (payload) => { sent.push(payload);
  return { ok: true, echoEpoch: 'refill-1', message: '접수' }; } }, refillModel);
const panelHtml = envRefill.panel.innerHTML;
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
// 누르면 단추가 실은 범위 digest를 보낸다 — 견적 문구는 없다(D110).
envRefill.click(el({ attrs: { 'data-vl-act': 'refill-approve',
  'data-vl-digest': 'd'.repeat(64) } }));
await flush();
envRefill.click(el({ attrs: { 'data-vl-act': 'refill-pick', 'data-vl-at': '33/10' } }));
checkpoints.push({ case: 'refill', panelHtml, seek: envRefill.vid.state.currentTime,
  sent: sent.map((p) => ({ sourceId: p.sourceId, digest: p.digest, quote: p.quote,
    token: p.token })) });
"""


def _run_refill_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "refill.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _REFILL_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "refill")


@needs_node
def test_the_panel_sends_the_scope_digest_and_the_span_buttons_only_seek(tmp_path: Path) -> None:
    report = _run_refill_harness(tmp_path)
    panel = report["panelHtml"]
    assert (
        '<details class="vl-details" data-vl-refill="1" open><summary>말소리 빈칸 재전사' in panel
    )
    assert "설명 &lt;b&gt;" in panel  # 글은 escape된다
    assert 'data-vl-act="refill-pick" data-vl-at="33/10">3.3–4.0초</button>' in panel
    assert "1.0–2.0초 · 전사 실패 구간</button>" in panel and "외 3곳" in panel
    assert "vl-refill-quote" not in panel  # 견적 문구 칸이 없다(D110)
    assert 'data-vl-act="refill-approve"' in panel and "빈칸 재전사 승인</button>" in panel
    # 넣지 않은 줄 — 글자는 escape되어 시각과 함께 보인다(검증 지적 6).
    assert "넣지 않은 줄 1개 — 그 구간은 다시 묻지 않습니다." in panel
    assert "3.5–3.8초 · &lt;겹친&gt; 말 · 기존 자막과 겹침" in panel and "외 2줄" in panel
    # 다시 듣기 칸 — 빈칸 재전사가 도는 동안은 단추 대신 그 사정을 말한다(검증 지적 1).
    assert "빈칸 재전사가 도는 중이라 지금은 받지 않습니다." in panel
    assert 'data-vl-act="relisten"' not in panel
    (sent,) = report["sent"]
    assert sent["digest"] == "d" * 64 and sent["token"] == "refill-token"
    assert sent.get("quote") is None
    assert report["seek"] == pytest.approx(3.3)


def test_the_blocks_wire_the_refill_function_and_the_panel() -> None:
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    assert "function refillPanel" in script and "out.push(refillPanel(info));" in script
    assert "call('vl_refill'" in script and "refillQuoteSeen" not in script
    assert 'root.addEventListener("pointerdown", root._vlDown)' in script
    assert "refill" in workspace_service.WORKBENCH_TOKEN_ACTIONS


def test_new_wording_is_polite_and_names_no_model_or_price() -> None:
    texts = [
        workspace_service.REFILL_EXPLANATION,
        workspace_service.REFILL_ORDER_WARNING,
        workspace_service.REFILL_STARTED_TEXT,
        workspace_service.REFILL_CHANGED_TEXT,
        workspace_service.REFILL_KEY_TEXT,
        workspace_service.REFILL_BUSY_TEXT,
        workspace_service.REFILL_RUNNING_TEXT,
        workspace_service.REFILL_NONE_TEXT,
        workspace_service.REFILL_DONE_TEXT,
        workspace_service.REFILL_NOTE_FAILED,
        workspace_service.REFILL_NOTE_RED,
        workspace_service.REFILL_NOTE_UNCHECKED,
        workspace_service.REFILL_NOTE_UNRECORDED,
        workspace_service.REFILL_SKIPPED_NOTE.format(count=2),
        workspace_service.RELISTEN_REFILL_RUNNING_TEXT,
        raw.REFILL_RELISTEN_SETTLE_REFUSAL,
        raw.RELISTEN_REFILL_RUNNING_REFUSAL,
        raw.REFILL_UNTRANSCRIBED,
        raw.REFILL_WAITING,
        raw.REFILL_UNMEASURED,
        raw.REFILL_RELISTEN_PENDING,
        raw.REFILL_CHANGED,
        raw.REFILL_GLOSSARY_CHANGED,
        raw.REFILL_PCM_MISSING,
    ]
    for text in texts:
        assert POLITE_END.search(text), text
        assert "$" not in text and "VAD" not in text and "whisper" not in text.lower()
        assert "GPT" not in text
