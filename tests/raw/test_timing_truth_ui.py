"""W-25b — 작업대의 「참 시작」·「참 끝」: 모델 값 · 서버 함수 · 원본 자막 편집기 그리기(D67).

유료 호출은 없다(정답 찍기는 무료다). 포트는 7993(시험용)만 쓴다. 시험이 못 박는 것:

- 모델의 원본 자막 줄은 권하는 층(`truthStratum` — 이름째)과 찍은 정답(`truth`)을 싣는다 — 그 밖의
  줄은 칸이 없다. 사라진 줄의 정답은 원본 칸의 한 줄(`truthMissing`)이다.
- 서버 함수는 수명 토큰 문을 지나고, 화면이 본 리비전의 줄에만 찍는다 — 낡은 화면은 아무것도 쓰지
  않고 새 화면과 까닭을 보인다. 찍고 지워도 원본 자막 리비전은 그대로다.
- 클라이언트는 지금 재생 위치(원본 시각, 정확 유리수)를 보내고, 편집기에 단추·찍은 값·층 표지·낡음
  안내를 그린다. 글자는 escape된다.
"""

from __future__ import annotations

import json
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
from tests.raw.test_sync_snap_ui import _call, _source_model  # noqa: E402
from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, _wait_terminal  # noqa: E402
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.project.raw import source_transcript, sync_report, timing_truth  # noqa: E402
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """검토가 선 작업대 + 톤 덩어리 원본(1.5초 이른 자막) + 시각 확인 — 19h 화면 시험 그대로."""
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    numpy_or_fake(monkeypatch)
    server = workspace_service.RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    assert server.create_workspace_view("시각 정답 화면", str(tmp_path / "ws"))[1]
    root = server.workspace_root()
    assert root is not None
    make_media(tmp_path / "card" / "tone.mov")
    proposal = server.raw.scan_sources(root, tmp_path / "card")
    handle = server.raw.start_ingest_copy(root, server.raw.confirm_ingest(root, proposal))
    assert _wait_terminal(server, root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    plan = server.raw.plan_segmentation(root, tile_seconds=Fraction(4))
    ReviewWorkspaceFacade(root).initialize_review(plan.revision_id)
    asset = server.raw.list_source_assets(root)[0]
    source_transcript.seed(
        server.raw._workspace(root), asset.asset_id, snap_rows(), Fraction(40), asset.content_sha256
    )
    assert server.raw.start_sync_checks(root, (asset.asset_id,)) is not None
    wait_sync(server.raw, root)
    return server, root, asset.asset_id


def _cue(server: Any, root: Path, source_id: str, cue_id: str) -> dict[str, Any]:
    subtitles = _source_model(server, root, source_id)["subtitles"]
    return next(cue for cue in subtitles if cue["id"] == cue_id)


def _mark(server: Any, root: Path, source_id: str, **payload: Any) -> Any:
    revision = _source_model(server, root, source_id)["transcriptRevision"]
    body = {"sourceId": source_id, "transcriptRevision": revision, "action": "mark", **payload}
    return _call(server, root, "vl_timing_truth", body)


def test_the_model_carries_strata_and_marks_and_the_server_function_writes_only_truth(
    bench: tuple[Any, Path, str],
) -> None:
    server, root, source_id = bench
    workspace = server.raw._workspace(root)
    model = _source_model(server, root, source_id)
    suggested = [cue for cue in model["subtitles"] if "truthStratum" in cue]
    assert 1 <= len(suggested) <= 5 and "truthMissing" not in model
    for cue in suggested:
        code = cue["truthStratum"]["code"]
        assert cue["truthStratum"]["label"] == workspace_service.TRUTH_STRATUM_LABELS[code]
    assert not any("truth" in cue for cue in model["subtitles"])
    before = source_transcript.load(workspace, source_id)
    reply = _mark(server, root, source_id, utteranceId="c05", edge="start", at="73/5")
    assert reply["ok"] is True
    assert reply["message"] == "시각 정답을 찍었습니다(참 시작 14.600초) — 자막은 그대로입니다."
    assert _cue(server, root, source_id, "c05")["truth"] == {"start": {"at": "73/5"}}
    assert source_transcript.load(workspace, source_id) == before  # 자막은 바이트 그대로
    cleared = _call(
        server,
        root,
        "vl_timing_truth",
        {"sourceId": source_id, "utteranceId": "c05", "edge": "start", "action": "clear"},
    )
    assert cleared["message"] == workspace_service.TRUTH_CLEARED_TEXT
    assert "truth" not in _cue(server, root, source_id, "c05")
    assert timing_truth.marks(workspace, source_id) == ()


def test_the_server_function_needs_the_token_and_refuses_a_stale_screen(
    bench: tuple[Any, Path, str],
) -> None:
    server, root, source_id = bench
    workspace = server.raw._workspace(root)
    body = {"sourceId": source_id, "utteranceId": "c05", "edge": "end", "action": "mark"}
    tokenless = _call(server, root, "vl_timing_truth", {**body, "at": "1"}, token=False)
    assert tokenless["ok"] is False
    stale = _call(server, root, "vl_timing_truth", {**body, "at": "1", "transcriptRevision": "x"})
    assert (
        stale["ok"] is True
        and stale["message"] == workspace_service.raw_service.TRUTH_REVISION_CHANGED
    )
    assert timing_truth.marks(workspace, source_id) == ()
    assert "timing-truth" in workspace_service.WORKBENCH_TOKEN_ACTIONS


def test_a_vanished_line_shows_one_missing_note(bench: tuple[Any, Path, str]) -> None:
    server, root, source_id = bench
    assert _mark(server, root, source_id, utteranceId="q", edge="end", at="93/10")["ok"] is True
    revision = _source_model(server, root, source_id)["transcriptRevision"]
    server.raw.edit_source_transcript(
        root, source_id, revision, action="merge", utterance_id="c03"
    )  # 다음 줄(q)을 c03에 합친다 — q가 사라진다
    model = _source_model(server, root, source_id)
    assert model["truthMissing"] == workspace_service.TRUTH_MISSING_TEXT.format(count=1)


# ── 클라이언트 — node에서 실제 실행본으로 ─────────────────────────────────────

_TRUTH_SCRIPT = r"""
const truthModel = visualModel();
truthModel.epoch = 'truth-1'; truthModel.token = 'truth-token';
truthModel.sources.srcA.transcriptRevision = 'rev-a';
truthModel.sources.srcA.truthMissing = '사라진 줄의 시각 정답 <1>개';
truthModel.sources.srcA.subtitles = [
  { id: 'u0', start: '0', end: '2/5', text: '첫 <줄>',
    truthStratum: { code: 'chunk_first', label: '조각 첫 줄' },
    truth: { start: { at: '1/20' }, end: { at: '9/20', stale: true } } },
  { id: 'u1', start: '1/2', end: '9/10', text: '둘째' },
];
const sent = [];
const envTruth = visualEnv({ vl_timing_truth: (payload) => { sent.push(payload);
  return { ok: true, echoEpoch: 'truth-1', message: 'x' }; } }, truthModel);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const listed = envTruth.panel.innerHTML;
envTruth.click(el({ attrs: { 'data-vl-act': 'source-pick', 'data-vl-cue-id': 'u0' } }));
const marked = envTruth.panel.innerHTML;
envTruth.vid.state.currentTime = 1.25;
envTruth.click(el({ attrs: { 'data-vl-act': 'truth-mark', 'data-vl-edge': 'end' } }));
await flush();
envTruth.click(el({ attrs: { 'data-vl-act': 'truth-clear', 'data-vl-edge': 'start' } }));
await flush();
envTruth.click(el({ attrs: { 'data-vl-act': 'source-pick', 'data-vl-cue-id': 'u1' } }));
const plain = envTruth.panel.innerHTML;
checkpoints.push({ case: 'truth', listed, marked, plain,
  sent: sent.map((p) => ({ sourceId: p.sourceId, transcriptRevision: p.transcriptRevision,
    utteranceId: p.utteranceId, edge: p.edge, action: p.action, at: p.at, token: p.token })) });
"""


def _run_truth_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "truth.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _TRUTH_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "truth")


@needs_node
def test_the_editor_draws_the_truth_buttons_and_sends_the_playhead(tmp_path: Path) -> None:
    report = _run_truth_harness(tmp_path)
    listed = report["listed"]
    assert '<span class="vl-badge" data-vl-truth-stratum="chunk_first"' in listed
    assert '<span class="vl-badge" data-vl-truth="2">정답 2/2</span>' in listed
    assert "사라진 줄의 시각 정답 &lt;1&gt;개" in listed
    panel = report["marked"]
    editor = panel[panel.index('<fieldset class="vl-source-caption-editor">') :]
    assert (
        '<button type="button" class="vl-btn" data-vl-act="truth-mark" data-vl-edge="start">'
        + workspace_service.TRUTH_START_LABEL
        + "</button> 참 시작 0.050초"
    ) in editor
    assert 'data-vl-act="truth-mark" data-vl-edge="end">참 끝</button> 참 끝 0.450초' in editor
    assert 'data-vl-act="truth-clear" data-vl-edge="start" aria-label="참 시작 지우기"' in editor
    assert workspace_service.TRUTH_STALE_TEXT in editor  # 끝 정답은 낡았다
    assert workspace_service.TRUTH_EXPLANATION in editor
    assert "조각 첫 줄</span> 시각 정답을 찍어 볼 줄입니다." in editor
    mark, clear = report["sent"]
    assert mark == {
        "sourceId": "srcA",
        "transcriptRevision": "rev-a",
        "utteranceId": "u0",
        "edge": "end",
        "action": "mark",
        "at": "5/4",
        "token": "truth-token",
    }
    assert clear["action"] == "clear" and clear["edge"] == "start" and clear["at"] == "5/4"
    plain = report["plain"]
    editor = plain[plain.index('<fieldset class="vl-source-caption-editor">') :]
    assert "참 시작 아직 없음" in editor and 'data-vl-act="truth-clear"' not in editor
    assert "시각 정답을 찍어 볼 줄입니다." not in editor


def test_the_blocks_wire_the_truth_function() -> None:
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    assert "call('vl_timing_truth'" in script and "truthLines(cue)" in script
    for label in (workspace_service.TRUTH_START_LABEL, workspace_service.TRUTH_END_LABEL):
        assert f'"{label}"' in script
    assert "source_transcript" not in script
