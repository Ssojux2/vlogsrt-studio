"""작업대 그리기의 비용과 바이트를 잠근다 — 원본 18개 합성 작업공간(W-23, D63).

세 가지를 못 박는다.

1. **바이트.** 그리기 값(정규화)이 기준 파일과 한 글자도 다르지 않다 — 다음 세션(B: 읽기 범위
   기억, C: 타이머 합치기)이 읽기를 줄여도 화면은 그대로여야 한다. 기준은 W-23 이전 코드(1fc3b44)로
   만든 값이다(`scripts/bench_workbench.py --golden`).
2. **부른 수의 천장.** 그리기 한 번이 DB 열기·원본 자막 읽기·검토 머리 읽기를 몇 번 하는지 —
   지금 값이 상한이다. 줄이는 것은 좋고 늘면 실패다.
3. **타이머 틱.** 바뀐 것이 없는 틱은 그리지 않는다. 작업 진행 수만 바뀐 틱은 그리되 정리 고리
   (`start_upkeep`·`start_sync_upkeep`)를 돌지 않는다. 작업 상태가 바뀐 틱은 한 번씩 돈다. 영상
   분석 중에는 등급 진행 문서가 바뀐 틱만 그린다(예전에는 그 단계 내내 틱마다 정리 고리째 그렸다).

유료 호출 0회. 작업공간은 시험 한 벌에 한 번 만들고(약 80초) 시험마다 베낀다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw import render_cost_fixture as fixture  # noqa: E402
from tests.raw.test_proposal_queue import _with_tiers, needs_ffmpeg  # noqa: E402
from ui import workspace_service  # noqa: E402
from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.analysis_store import AnalysisStore  # noqa: E402
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace  # noqa: E402

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures/synthetic/workbench_render_18_sources.html"

RENDER_CEILING: dict[str, int] = {
    "open_db": 1602,
    "source_transcript.load": 540,
    "sync_report.gather_source": 18,
    "ReviewWorkspaceFacade()": 3,
    "load_head": 3,
    "analysis_overview": 19,
    "visual_workbench_render": 1,
    "start_upkeep": 0,
    "start_sync_upkeep": 0,
}
"""그리기 한 번의 부른 수 — W-23 세션 A의 실측(1fc3b44와 같다). 상한이다: 줄면 이 값을 낮춘다."""


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    root = fixture.build_workspace(tmp_path_factory.mktemp("render_cost"))
    service = raw_service.RawWorkspaceService()
    fixture.settle(service, root)
    _checkpoint(root)
    return root


def _checkpoint(root: Path) -> None:
    """베끼기 전에 WAL을 DB 파일로 접는다 — 옆 파일 없이 DB 하나로 온전하다(D63)."""
    connection = sqlite3.connect(root / DB_NAME)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        connection.close()


def _opened(built: Path, tmp_path: Path) -> tuple[RawWorkspaceUi, Path, str]:
    root = tmp_path / "project"
    shutil.copytree(built, root, ignore=shutil.ignore_patterns(f"{DB_NAME}-*"))
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    signature = server.open_workspace_view(str(root))[1]
    _with_tiers(server)
    fixture.settle(server.raw, root)
    return server, root, signature


def _counted(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    return fixture.install_counters(monkeypatch.setattr)


def _during(counts: Counter[str], call: object) -> Counter[str]:
    before = Counter(counts)
    call()  # type: ignore[operator]
    return counts - before


@needs_ffmpeg
def test_the_rendered_bytes_match_the_locked_render(built: Path, tmp_path: Path) -> None:
    server, root, _signature = _opened(built, tmp_path)
    first = fixture.normalize("\n".join(server.visual_workbench_render(root)), tmp_path)
    second = fixture.normalize("\n".join(server.visual_workbench_render(root)), tmp_path)
    assert first == second  # 두 번 그려도 같다 — 잠글 수 있는 값이다
    assert first == GOLDEN.read_text(encoding="utf-8")


@needs_ffmpeg
def test_one_render_stays_under_the_measured_call_ceiling(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, _signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    calls = _during(counts, lambda: server.visual_workbench_render(root))
    over = {name: calls[name] for name, ceiling in RENDER_CEILING.items() if calls[name] > ceiling}
    assert over == {}, f"그리기 한 번의 부른 수가 늘었다: {dict(calls)}"
    assert calls["visual_workbench_render"] == 1


@needs_ffmpeg
def test_an_unchanged_tick_neither_renders_nor_runs_upkeep(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    server.capcut_tick_view(signature)  # 열쇠를 채운다
    calls = _during(counts, lambda: server.capcut_tick_view(signature))
    assert calls["visual_workbench_render"] == 0
    assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 0


@needs_ffmpeg
def test_a_progress_only_tick_renders_without_upkeep(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    stepped = fixture.SteppedJob(server.raw, root)
    try:
        stepped.step(1)
        server.capcut_tick_view(signature)  # 작업이 돌기 시작한 틱 — 상태가 바뀌었다
        for value in (2, 3):
            stepped.step(value)
            calls = _during(counts, lambda: server.capcut_tick_view(signature))
            assert calls["visual_workbench_render"] == 1  # 진행 수는 그리기 열쇠에 남는다
            assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 0
    finally:
        stepped.finish()


@needs_ffmpeg
def test_a_job_status_change_runs_upkeep_once(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    stepped = fixture.SteppedJob(server.raw, root)
    stepped.step(1)
    server.capcut_tick_view(signature)
    stepped.finish()  # 돌던 작업이 끝났다 — 정리 고리의 재료가 바뀌었을 수 있는 순간이다
    calls = _during(counts, lambda: server.capcut_tick_view(signature))
    assert calls["visual_workbench_render"] == 1
    assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 1
    again = _during(counts, lambda: server.capcut_tick_view(signature))
    assert again["start_upkeep"] == again["visual_workbench_render"] == 0


@needs_ffmpeg
def test_during_semantic_analysis_only_a_changed_progress_document_redraws(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """영상 분석 중 — 창 판정이 진행 문서에 쌓일 때만 다시 그린다(그 단계 내내 그리지 않는다)."""
    server, root, signature = _opened(built, tmp_path)
    summary = server.raw.analysis_overview(root)
    assert summary is not None
    revision = summary.revision.revision_id
    asset_id = server.raw.list_source_assets(root)[0].asset_id
    server._quick = {
        "root": root,
        "token": "semantic-probe",
        "stage": workspace_service.SEMANTIC_STAGE,
        "revision": revision,
        "asset_ids": (asset_id,),
    }
    counts = _counted(monkeypatch)
    server.capcut_tick_view(signature)
    still = _during(counts, lambda: server.capcut_tick_view(signature))
    assert still["visual_workbench_render"] == 0
    row = AnalysisStore(RawWorkspace.open(root)).source_result(revision, asset_id)
    assert row is not None
    directory, _document = raw_service._tier_paths(root.resolve(), row)
    before = server.raw.analysis_tier_stamp(root, revision, (asset_id,))
    progress = directory / "suitability_progress.json"
    progress.write_text(json.dumps({"analysis_revision_id": "다른 리비전"}), encoding="utf-8")
    assert server.raw.analysis_tier_stamp(root, revision, (asset_id,)) != before
    redrawn = _during(counts, lambda: server.capcut_tick_view(signature))
    assert redrawn["visual_workbench_render"] == 1
    assert redrawn["start_upkeep"] == redrawn["start_sync_upkeep"] == 0
