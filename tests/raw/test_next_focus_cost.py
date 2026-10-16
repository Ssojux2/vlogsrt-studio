"""D62 B7 — 「다음」(대기열 초점)은 분석 선택·미리보기 작업을 띄우지 않고 원본을 해시하지 않는다.

「다음 제외 제안으로 옮기는 중입니다…」가 멈춰 보인 것은 다른 영상으로 갈 때마다 원본 전체
재해시(카드 클릭의 선택 분석·미리보기 시작)가 끼었기 때문이다. 「다음」 자체는 그리기만 한다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_proposal_queue import _opened_tier, needs_ffmpeg  # noqa: E402
from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt.project.raw import source_verify, visual_review  # noqa: E402
from vlogsrt.project.raw.segment_review import unreviewed_auto_exclusions  # noqa: E402


@needs_ffmpeg
def test_the_next_focus_starts_no_job_and_hashes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature, facade = _opened_tier(tmp_path)
    head = facade.load_head().revision
    proposal = unreviewed_auto_exclusions(head)[0]
    before = {job.job_id for job in server.raw.list_jobs(root, limit=500)}
    calls: list[str] = []

    def forbid(name: str) -> Any:
        def call(*_args: object, **_kwargs: object) -> Any:
            calls.append(name)
            raise AssertionError(f"「다음」이 {name}을(를) 불렀다")

        return call

    monkeypatch.setattr(source_verify, "_full_sha256", forbid("원본 해시"))
    monkeypatch.setattr(server, "_start_quick", forbid("선택 분석"))
    monkeypatch.setattr(server.raw, "start_source_preview", forbid("미리보기 작업"))
    monkeypatch.setattr(server.raw, "cached_selection_analysis", forbid("캐시 검사"))
    payload = {
        "signature": signature,
        "token": server._visual_lifetime_token,
        "epoch": "e",
        "page": 1,
        "basePlanId": head.base_plan_id,
        "sourceId": proposal.source_id,
        "focusId": proposal.segment_id,
    }
    found = server.visual_workbench_action(payload, "refresh")
    assert found["ok"] is True and found["focusStale"] is False, found
    assert calls == []
    started = {job.job_id for job in server.raw.list_jobs(root, limit=500)} - before
    kinds = {server.raw.get_job(root, job_id).kind for job_id in started}
    assert not kinds & {"source_preview", "visual_prepare", "selection_prepare", "selection_review"}


def test_server_start_sweeps_dead_preview_leftovers(tmp_path: Path) -> None:
    """서버 시작(작업 복구 뒤)이 끊긴 만들기의 `.part`를 지우고 끊긴 준비 기록을 닫는다(B5)."""
    first = RawWorkspaceUi(state_path=tmp_path / "state.json")
    status = first.raw.create_workspace(tmp_path / "VlogProject", "제주")
    first._remember(Path(status.workspace_root))
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    visual = Path(status.workspace_root) / visual_review.VISUAL_PREVIEW_ROOT
    visual.mkdir(parents=True)
    junk = visual / f".k.mp4.{child.pid}.0a1b2c3d.part"
    junk.write_bytes(b"x" * 128)
    record = visual / "k.json"
    record.write_text(json.dumps({"state": "preparing", "key": "k"}), encoding="utf-8")
    RawWorkspaceUi(state_path=tmp_path / "state.json").startup()
    assert not junk.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["state"] == "interrupted"
