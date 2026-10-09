"""작업대의 자막 시각 확인 배선(D55) — 모델의 `syncCheck`, 「다시 재기」 토큰 문, 앱 열기 고리.

표시만 한다: 다시 재기는 무료 백그라운드 작업 하나를 걸 뿐 자막·컷을 바꾸지 않는다. 고리가
실패해도 화면 그리기는 막히지 않는다. 포트는 7993(시험용)만 쓴다 — 실제 앱 상태는 건드리지 않는다.
"""

from __future__ import annotations

import shutil
import sqlite3
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
from tests.raw.test_visual_workbench_ui import _model_of, _prepare_review  # noqa: E402
from ui import workspace_service  # noqa: E402
from vlogsrt.project.raw import service as raw  # noqa: E402
from vlogsrt.project.raw import source_transcript, sync_report  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def settle(server: Any, root: Path) -> None:
    """걸린 확인 작업이 끝날 때까지 — 시험이 끝난 뒤 작업이 남지 않게."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        latest = server.raw.latest_job(root, raw.SYNC_CHECK_JOB_KIND)
        if latest is None or latest.status in TERMINAL:
            return
        time.sleep(0.02)
    raise AssertionError("자막 시각 확인 작업이 끝나지 않았다")


@pytest.fixture
def workbench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """검토가 선 작업대 + 원본 자막 하나(세 줄)."""
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    numpy_or_fake(monkeypatch)
    server = workspace_service.RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    assert server.create_workspace_view("시각 화면", str(tmp_path / "ws"))[1]
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
    return server, root, asset.asset_id


def test_the_model_carries_the_sync_badge(workbench: tuple[Any, Path, str]) -> None:
    server, root, source_id = workbench
    assert server.raw.start_sync_checks(root, (source_id,)) is not None
    settle(server, root)
    _, html = server.visual_workbench_render(root)
    sync = _model_of(html)["sources"][source_id]["syncCheck"]
    assert sync["label"] in {label for label, _tone in sync_report.BADGES.values()}
    assert sync["level"] in sync_report.BADGES and sync["stale"] is False
    assert (sync["label"], sync["tone"]) == sync_report.badge(sync["level"])


def test_recheck_needs_the_workbench_token(workbench: tuple[Any, Path, str]) -> None:
    """「다시 재기」는 다른 작업대 변경과 같은 토큰 문을 지난다 — 토큰이 없으면 거절."""
    server, root, source_id = workbench
    _, html = server.visual_workbench_render(root)
    model = _model_of(html)
    recheck = {fn.__name__: fn for fn in server._visual_server_functions()}["vl_sync_recheck"]
    payload = {"epoch": model["epoch"], "signature": model["signature"], "sourceId": source_id}
    refused = recheck(dict(payload))
    assert refused["ok"] is False
    assert server.raw.latest_job(root, raw.SYNC_CHECK_JOB_KIND) is None
    accepted = recheck({**payload, "token": model["token"]})
    assert accepted["ok"] is True and "다시 잽니다" in accepted["message"], accepted
    assert server.raw.latest_job(root, raw.SYNC_CHECK_JOB_KIND) is not None
    settle(server, root)


def test_the_app_open_hook_adds_its_note_and_never_blocks_the_screen(
    workbench: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, _source_id = workbench
    monkeypatch.setattr(server, "_maybe_upkeep", lambda _root: None)
    monkeypatch.setattr(server.raw, "start_sync_upkeep", lambda _root: "자막 시각 확인 1개")
    message, _shell, _html = server._capcut_result(root, "처음")
    assert message == "처음\n\n자막 시각 확인 1개"

    def locked(_root: Path) -> str:
        raise sqlite3.OperationalError("database is locked")

    warnings: list[str] = []
    monkeypatch.setattr(server.raw, "start_sync_upkeep", locked)
    monkeypatch.setattr(workspace_service.log, "warn", warnings.append)
    message, _shell, html = server._capcut_result(root, "처음")
    assert message == "처음" and "data-vl-model" in html
    assert any("OperationalError" in line for line in warnings), warnings
    sync = _model_of(html)["sources"][_source_id]["syncCheck"]
    assert (sync["label"], sync["tone"]) == ("자막 시각 확인을 걸지 못함 (로그 참고)", "grey")
