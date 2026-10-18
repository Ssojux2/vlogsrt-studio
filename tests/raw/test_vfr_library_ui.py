"""VFR 원본이 섞인 라이브러리의 작업대 — 목록·선택 분석·보내기 결과가 VFR을 이름으로 보인다(D68).

- 영상 목록은 VFR 영상도 촬영 시각 순 자리에 카드로 보인다(재생하지 않고, 누르면 까닭).
- 선택 분석은 검토 머리를 게시한다 — 보지 않는 영상이라도 머리가 아직 없으면 게시하고, 게시가
  실패하면 작업이 실패로 남는다(예전엔 보지 않는 영상의 게시를 말없이 건너뛰고 성공으로 끝냈다).

유료 호출 0회 — 전사·의미 분석은 가짜 전송이다(`make_server`와 같은 짜임, 포트 값만 7993).
"""

from __future__ import annotations

import json
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

import gradio as gr  # noqa: E402

from tests.raw.test_browser_runtime import _CountingSender  # noqa: E402
from tests.raw.test_review_workspace import _ingest_card, _wait_terminal  # noqa: E402
from tests.raw.test_selection_analysis import (  # noqa: E402
    SuitabilityClient,
    keep_luna,
    keep_whisper_only,
    wait_until,
)
from tests.raw.test_vfr_library import make_cfr, mixed_card  # noqa: E402
from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, _model_of  # noqa: E402
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.config import Config  # noqa: E402
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport  # noqa: E402
from vlogsrt.project.raw.models import UNSUPPORTED_GRID_REASON  # noqa: E402
from vlogsrt.project.raw.service import RawWorkspaceError  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

DONE = "추천 구간 준비 완료"
FAILED = "분석 확인 필요"


class _Context:
    def check_cancel(self) -> None:
        return None


def _server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> workspace_service.RawWorkspaceUi:
    """`make_server`와 같은 가짜 전송 서버 — 원본은 아직 없다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    server = workspace_service.RawWorkspaceUi(
        port=7993,
        state_path=tmp_path / "ui-state.json",
        live=LiveAnalysisTransport(
            sender=_CountingSender([{"start": 0.5, "end": 1.5, "text": "합성 영상입니다."}]),
            cfg=Config(),
            client=SuitabilityClient(),
            asr_approvals=lambda *a: {},
            luna_approvals=lambda *a: {},
        ),
    )
    server.create_workspace_view("VFR 섞임", str(tmp_path / "workspace"))
    keep_luna(server)
    keep_whisper_only(server)
    server._selection_auto_approval = None
    return server


def _mixed_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[workspace_service.RawWorkspaceUi, Path, dict[str, str]]:
    server = _server(tmp_path, monkeypatch)
    root = server.workspace_root()
    assert root is not None
    mixed_card(tmp_path / "card")
    _ingest_card(server.raw, root, tmp_path / "card")
    server.raw.ensure_source_order(root)
    ids = {asset.source_name: asset.asset_id for asset in server.raw.list_source_assets(root)}
    return server, root, ids


def _head_ready(server: workspace_service.RawWorkspaceUi, root: Path, source_id: str) -> None:
    handle = server.raw.start_source_preview(root, asset_ids=(source_id,))
    record = _wait_terminal(server.raw, root, handle.job_id)
    assert record.status == "succeeded", record.error


# ── 목록 — VFR 영상도 촬영 시각 순 자리에 까닭과 함께 ─────────────────────────────


def test_workbench_lists_the_vfr_video_in_shooting_order_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, ids = _mixed_server(tmp_path, monkeypatch)
    _head_ready(server, root, ids["01_first.mov"])

    model = _model_of(server.visual_workbench_render(root)[1])

    assert [c["sourceId"] for c in model["mediaCards"]] == [
        ids["01_first.mov"],
        ids["03_last.mov"],
    ]
    (card,) = model["unsupportedSources"]
    assert card["sourceId"] == ids["02_iphone.mov"] and card["sourceName"] == "02_iphone.mov"
    assert card["reason"] == UNSUPPORTED_GRID_REASON
    assert card["before"] == ids["03_last.mov"]  # 첫 영상과 마지막 영상 사이(D53)
    assert "02_iphone.mov" in card["note"] and "viewUnsupported" not in model
    # 그 영상을 고르면 아무 작업도 걸지 않고 까닭을 말한다(「준비 중」으로 멈추지 않는다).
    jobs = len(server.raw.list_jobs(root))
    with pytest.raises(gr.Error) as refused:
        server.quick_source_view(server.current_signature(), ids["02_iphone.mov"])
    assert UNSUPPORTED_GRID_REASON in str(refused.value)
    assert len(server.raw.list_jobs(root)) == jobs


def test_an_all_cfr_workbench_carries_no_vfr_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VFR이 없으면 그리기 값에 칸이 없다 — 그리기 바이트가 D68 이전과 같다."""
    server = _server(tmp_path, monkeypatch)
    root = server.workspace_root()
    assert root is not None
    make_cfr(tmp_path / "card" / "a.mov", "2")
    _ingest_card(server.raw, root, tmp_path / "card")
    server.raw.ensure_source_order(root)
    (asset,) = server.raw.list_source_assets(root)
    _head_ready(server, root, asset.asset_id)

    model = _model_of(server.visual_workbench_render(root)[1])

    assert "unsupportedSources" not in model and "viewUnsupported" not in model


# ── 선택 분석 — VFR이 있어도 검토를 게시하고, 실패는 숨지 않는다 ───────────────────


def test_selection_analysis_publishes_a_review_with_a_vfr_source_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, ids = _mixed_server(tmp_path, monkeypatch)
    server._selection_auto_approval = lambda quote: True

    server._start_quick(root, (), asset_ids=(ids["01_first.mov"],))
    wait_until(lambda: server._quick.get("stage") in (DONE, FAILED))

    assert server._quick["stage"] == DONE, server._quick
    job = server.raw.get_job(root, server._quick["job_id"])
    assert job.kind == "selection_analysis" and job.status == "succeeded"
    head = server.raw.review_workspace_facade(root).load_head().revision
    assert {c.source_id for c in head.clocks} == {ids["01_first.mov"], ids["03_last.mov"]}
    assert [u.asset_id for u in server.raw.review_unsupported_sources(root)] == [
        ids["02_iphone.mov"]
    ]


def test_a_publish_failure_fails_the_selection_job_instead_of_succeeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, ids = _mixed_server(tmp_path, monkeypatch)
    server._selection_auto_approval = lambda quote: True

    def broken(*_: Any, **__: Any) -> str:
        raise RawWorkspaceError("합성 게시 실패")

    monkeypatch.setattr(server.raw, "publish_selection_review", broken)
    server._start_quick(root, (), asset_ids=(ids["01_first.mov"],))
    wait_until(lambda: server._quick.get("stage") in (DONE, FAILED))

    assert server._quick["stage"] == FAILED and "합성 게시 실패" in server._quick["error"]
    job = _wait_terminal(server.raw, root, server._quick["job_id"])
    assert job.status == "failed" and "합성 게시 실패" in (job.error or "")


def test_an_unviewed_selection_publishes_when_no_review_exists_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """라이브러리 승인의 영상은 보지 않는 채 돈다 — 머리가 없으면 게시를 건너뛰지 않는다.

    예전 실데이터: 34개가 모두 「추천 구간 준비 완료」·작업 성공이었는데 검토 머리가 0개였다."""
    server, root, _ids = _mixed_server(tmp_path, monkeypatch)
    published: list[str] = []
    monkeypatch.setattr(
        server.raw,
        "publish_selection_review",
        lambda _root, revision, _context: published.append(revision) or "review",
    )
    assert not server.raw.review_workspace_facade(root).has_review()

    server._publish_selection(root, "not-the-viewed-token", "analysis-1", _Context())
    assert published == ["analysis-1"]


def test_an_unviewed_selection_defers_publishing_once_a_review_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, ids = _mixed_server(tmp_path, monkeypatch)
    _head_ready(server, root, ids["01_first.mov"])
    published: list[str] = []
    monkeypatch.setattr(
        server.raw,
        "publish_selection_review",
        lambda _root, revision, _context: published.append(revision) or "review",
    )

    server._publish_selection(root, "not-the-viewed-token", "analysis-1", _Context())
    assert published == []  # 그 영상을 볼 때 게시한다(원본 전부 재해시를 영상마다 하지 않는다)


# ── 실행본 — 덮지 못한 영상 카드는 고르지 않고 까닭을 보인다 ──────────────────────

_VFR_SCRIPT = r"""
const vState = () => windowScope.__vlVisualWorkbenchState;
vState().selectedId = null; vState().selectedPlan = null; vState().marker = null;
vState().lastMsg = ''; vState().filter = 'all';
const vModel = visualModel();
const vVideos = vModel.mediaCards;
vModel.mediaCards = vVideos;
vModel.unsupportedSources = [{ sourceId: 'srcV', sourceName: 'IMG_8844.mov',
  reason: '분석 지원 밖 — 프레임 격자가 고르지 않음(VFR)', note: 'IMG_8844.mov: 까닭',
  before: 'srcB' }];
const envV = visualEnv({}, vModel);
const vHtml = envV.thumbs.innerHTML;
envV.click({ getAttribute: (k) => ({ 'data-vl-act': 'unsupported-pick',
  'data-vl-note': 'IMG_8844.mov: 까닭' })[k] || null });
const viewModel = visualModel();
viewModel.mediaCards = viewModel.mediaCards;
viewModel.unsupportedSources = vModel.unsupportedSources;
viewModel.viewUnsupported = 'IMG_8844.mov: 보던 영상';
const envView = visualEnv({}, viewModel);
checkpoints.push({ case: 'vfr-cards', html: vHtml, note: envV.note.textContent,
  calls: envV.serverCalls.length, viewNote: envView.note.textContent });
"""


@needs_node
def test_vfr_card_sits_in_order_and_explains_itself_without_a_server_call(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "vfr.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _VFR_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    report = next(row for row in json.loads(completed.stdout) if row.get("case") == "vfr-cards")
    html = report["html"]
    assert "<b>영상 4개</b> · 촬영 시각 순 · 분석 지원 밖 1개" in html
    at = html.index('data-vl-unsupported="srcV"')
    assert html.index('data-vl-card="segA1"') < at < html.index('data-vl-card="segB1"')
    assert "분석 지원 밖 — 프레임 격자가 고르지 않음(VFR)" in html
    assert report["note"] == "IMG_8844.mov: 까닭" and report["calls"] == 0
    assert report["viewNote"] == "IMG_8844.mov: 보던 영상"
