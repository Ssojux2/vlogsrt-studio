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
from tests.raw.test_review_workspace import _wait_terminal  # noqa: E402
from tests.raw.test_visual_workbench_ui import _model_of  # noqa: E402
from ui import workspace_service  # noqa: E402
from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt.project.raw import read_scope  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.analysis_store import AnalysisStore  # noqa: E402
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace  # noqa: E402

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures/synthetic/workbench_render_18_sources.html"

RENDER_CEILING: dict[str, int] = {
    "open_db": 30,
    "source_transcript.load": 18,
    "sync_report.gather_source": 18,
    "ReviewWorkspaceFacade()": 1,
    "load_head": 1,
    "analysis_overview": 1,
    "visual_workbench_render": 1,
    "start_upkeep": 0,
    "start_sync_upkeep": 0,
}
"""그리기 한 번의 실제 읽기 수 — 상한이다: 줄면 이 값을 낮춘다.

세션 A(1fc3b44)의 실측은 DB 열기 1602 · 원본 자막 540 · 시각 확인 재료 18 · 검토 파사드 3 · 검토
머리 3 · 분석 개요 19였다. 세션 B는 그리기 한 번을 읽기 범위(`read_scope`) 하나로 묶어 같은 읽기를
한 번만 한다 — 파사드는 작업공간마다, 검토 머리는 기본 계획마다, 원본 자막은 지금 리비전 전부를 한
번에, 분석 개요는 리비전마다 한 번이다.
"""


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
    assert read_scope.current() is None  # 범위는 그리기 한 번만 산다


@needs_ffmpeg
def test_an_unchanged_tick_neither_renders_nor_runs_upkeep(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    tab = fixture.TickTab(server, signature)
    tab.tick()  # 열쇠를 채운다
    calls = _during(counts, tab.tick)
    assert calls["visual_workbench_render"] == 0
    assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 0


@needs_ffmpeg
def test_a_progress_only_tick_renders_without_upkeep(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    stepped = fixture.SteppedJob(server.raw, root)
    tab = fixture.TickTab(server, signature)
    try:
        stepped.step(1)
        tab.tick()  # 작업이 돌기 시작한 틱 — 상태가 바뀌었다
        for value in (2, 3):
            stepped.step(value)
            calls = _during(counts, tab.tick)
            assert calls["visual_workbench_render"] == 1  # 진행 수는 그리기 열쇠에 남는다
            assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 0
            # 고급 패널 머리 맞춤과 그리기가 한 범위를 함께 쓴다 — 파사드·검토 머리는 한 번이다.
            assert calls["ReviewWorkspaceFacade()"] == calls["load_head"] == 1, dict(calls)
            assert calls["source_transcript.load"] <= RENDER_CEILING["source_transcript.load"]
    finally:
        stepped.finish()


@needs_ffmpeg
def test_a_job_status_change_runs_upkeep_once(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    stepped = fixture.SteppedJob(server.raw, root)
    tab = fixture.TickTab(server, signature)
    stepped.step(1)
    tab.tick()
    stepped.finish()  # 돌던 작업이 끝났다 — 정리 고리의 재료가 바뀌었을 수 있는 순간이다
    calls = _during(counts, tab.tick)
    assert calls["visual_workbench_render"] == 1
    assert calls["start_upkeep"] == calls["start_sync_upkeep"] == 1
    again = _during(counts, tab.tick)
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
    tab = fixture.TickTab(server, signature)
    tab.tick()
    still = _during(counts, tab.tick)
    assert still["visual_workbench_render"] == 0
    row = AnalysisStore(RawWorkspace.open(root)).source_result(revision, asset_id)
    assert row is not None
    directory, _document = raw_service._tier_paths(root.resolve(), row)
    before = server.raw.analysis_tier_stamp(root, revision, (asset_id,))
    progress = directory / "suitability_progress.json"
    progress.write_text(json.dumps({"analysis_revision_id": "다른 리비전"}), encoding="utf-8")
    assert server.raw.analysis_tier_stamp(root, revision, (asset_id,)) != before
    redrawn = _during(counts, tab.tick)
    assert redrawn["visual_workbench_render"] == 1
    assert redrawn["start_upkeep"] == redrawn["start_sync_upkeep"] == 0


# ── 화면과 동작 일치(W-24 세션 D, D64) — 같은 18개 작업공간 ──────────────────────────


@needs_ffmpeg
def test_every_video_gets_a_card_with_its_real_thumbnail(built: Path, tmp_path: Path) -> None:
    """영상 18개 — 컷은 한 쪽(24)을 넘지만 카드는 모두이고, 쪽 밖 영상도 진짜 썸네일 상태다."""
    server, root, _signature = _opened(built, tmp_path)
    model = _model_of(server.visual_workbench_render(root)[1])
    assert model["pageCount"] > 1 and len(model["cards"]) == raw_service.VISUAL_PAGE_SIZE
    cards = model["mediaCards"]
    assert [card["sourceId"] for card in cards] == list(model["sources"])
    assert len(cards) == fixture.SOURCE_COUNT
    paged = {card["sourceId"] for card in model["cards"]}
    assert any(card["sourceId"] not in paged for card in cards)  # 이 쪽에 컷이 없는 영상
    assert all(card["thumb"]["state"] == "ready" and card["thumb"]["src"] for card in cards)
    for card in cards:  # 대표 컷은 그 영상의 컷이다(누르면 그 컷이 골라진다)
        cuts = model["sources"][card["sourceId"]]["rail"]["intervals"]
        assert card["id"] in [cut["id"] for cut in cuts]


@needs_ffmpeg
def test_the_selected_video_beyond_the_first_page_ships_its_cuts(
    built: Path, tmp_path: Path
) -> None:
    """첫 쪽(24컷) 밖 영상을 고르면 그 영상의 컷(썸네일 포함)만 따로 싣는다 — 다른 영상은 없다."""
    server, root, _signature = _opened(built, tmp_path)
    overview = server.raw.visual_review_query(root, include_all_sources=True)
    last = overview.representatives[-1]
    model = _model_of(
        server.visual_workbench_render(root, preferred_selected_id=last.segment_id)[1]
    )
    assert model["selectedSourceId"] == last.source_id
    rail = [cut["id"] for cut in model["sources"][last.source_id]["rail"]["intervals"]]
    paged = {card["id"] for card in model["cards"]}
    assert not paged & set(rail)  # 쪽 목록에는 이 영상의 컷이 없다
    view = model["viewCards"]
    assert [card["id"] for card in view] == rail
    assert {card["sourceId"] for card in view} == {last.source_id}
    assert all(card["thumb"]["state"] == "ready" and card["thumb"]["src"] for card in view)


@needs_ffmpeg
def test_two_tabs_keep_their_own_render_keys(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """한 탭이 그렸다고 다른 탭이 건너뛰지 않는다 — 열쇠는 탭마다다(서버 하나의 값이 아니다)."""
    server, root, signature = _opened(built, tmp_path)
    counts = _counted(monkeypatch)
    first, second = fixture.TickTab(server, signature), fixture.TickTab(server, signature)
    for tab in (first, second):  # 처음 본 탭은 각자 그린다
        assert _during(counts, tab.tick)["visual_workbench_render"] == 1
    for tab in (first, second):
        assert _during(counts, tab.tick)["visual_workbench_render"] == 0
    stepped = fixture.SteppedJob(server.raw, root)
    try:
        stepped.step(1)
        assert _during(counts, first.tick)["visual_workbench_render"] == 1
        assert _during(counts, second.tick)["visual_workbench_render"] == 1  # 예전엔 건너뛰었다
        assert _during(counts, second.tick)["visual_workbench_render"] == 0
    finally:
        stepped.finish()


@needs_ffmpeg
def test_redraws_without_a_filter_leave_the_tab_filter_alone(built: Path, tmp_path: Path) -> None:
    """틱·명령 줄 단추의 다시 그리기는 필터 칸이 비었다 — 실행본이 탭의 필터를 그대로 둔다."""
    server, root, signature = _opened(built, tmp_path)
    ticked = fixture.TickTab(server, signature).tick()
    assert _model_of(ticked[2])["filter"] is None
    assert _model_of(server.capcut_refresh_view(signature)[2])["filter"] is None
    shown = _model_of(server.visual_workbench_render(root)[1])
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    reply = fns["vl_refresh"](
        {"epoch": shown["epoch"], "signature": signature, "page": 1, "filter": "excluded"}
    )
    assert reply["ok"] and _model_of(reply["html"])["filter"] == "excluded"


@needs_ffmpeg
def test_reprepare_fills_the_selected_video_beyond_the_first_page(
    built: Path, tmp_path: Path
) -> None:
    """「미리보기 다시 준비」는 고른 영상의 썸네일을 채운다 — 첫 쪽 밖이어도, 다른 영상은 그대로."""
    server, root, signature = _opened(built, tmp_path)
    overview = server.raw.visual_review_query(root, include_all_sources=True)
    first, last = overview.representatives[0], overview.representatives[-1]
    assert last.segment_id not in {card.segment_id for card in overview.segments}  # 쪽 밖
    for card in (first, last):
        assert card.thumb_relative_path is not None
        (root / card.thumb_relative_path).unlink()
    shown = _model_of(server.visual_workbench_render(root)[1])
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    reply = fns["vl_prepare"](
        {
            "epoch": shown["epoch"],
            "token": shown["token"],
            "signature": signature,
            "page": 1,
            "selectedId": last.segment_id,
            "sourceId": last.source_id,
        }
    )
    assert reply["ok"] and "이 영상의 미리보기 준비 작업" in reply["message"]
    for job in server.raw.all_active_jobs(root):
        assert _wait_terminal(server.raw, root, job.job_id).status == "succeeded"
    assert (root / str(last.thumb_relative_path)).is_file()  # 고른 영상 — 채웠다
    assert not (root / str(first.thumb_relative_path)).is_file()  # 첫 쪽 영상 — 그대로
