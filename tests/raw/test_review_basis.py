"""검토 기본의 원본별 근거 기록(W-73, D124) — 이미 게시한 분석을 볼 때 다시 게시하지 않는다.

카드를 누를 때마다 배경 `selection_review`가 라이브러리 전체 계획을 새로 세웠다(원본마다 전체
해시 셋 · 새 계획·검토 기본 하나 · 실측 원본 169개에 20–75초). 이제 검토 기본마다 원본별로 「어느
분석 행으로 지었나」를 같은 트랜잭션에 적고, 그 영상의 지금 분석 행과 같으면 게시를 건너뛴다.
기록이 없거나 다르면 예전처럼 게시한다 — 보지 않은 채 분석된 영상은 처음 볼 때 게시된다(D68).
"""

from __future__ import annotations

import sqlite3

from tests.raw.test_background_library import setup_library
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_selection_analysis import wait_until
from vlogsrt.project.raw.review_workspace import carried_basis
from vlogsrt.project.raw.workspace import DB_NAME


def _review_jobs(ui, root) -> list:
    return [job for job in ui.raw.list_jobs(root, limit=1000) if job.kind == "selection_review"]


def _settle(ui, root) -> None:
    for job in ui.raw.all_active_jobs(root):
        _wait_terminal(ui.raw, root, job.job_id)


def _analyzed_library(tmp_path, monkeypatch):
    """a(보던 영상)와 b(보지 않은 채) 둘 다 분석이 끝난 라이브러리."""
    ui, _sender, client, root, ids = setup_library(tmp_path, monkeypatch, parallel_analysis_limit=1)
    client.resume.set()
    ui.library_analysis_view(ui.current_signature())
    tasks = {name: lambda name=name: ui._task_for(root, (ids[name],)) for name in ids}
    wait_until(
        lambda: all((tasks[name]() or {}).get("stage") == "추천 구간 준비 완료" for name in ids)
    )
    _settle(ui, root)
    return ui, root, ids


def _click(ui, root, source_id: str) -> None:
    ui.quick_source_view(ui.current_signature(), source_id)
    _settle(ui, root)


def test_switching_between_published_videos_publishes_nothing_more(tmp_path, monkeypatch):
    ui, root, ids = _analyzed_library(tmp_path, monkeypatch)
    # 보지 않은 채 끝난 b는 처음 볼 때 게시된다(D68) — 그다음부터 둘 다 근거가 맞는다.
    _click(ui, root, ids["b.mov"])
    _click(ui, root, ids["a.mov"])
    facade = ui.raw.review_workspace_facade(root)
    b_rows = [s for s in facade.load_head().revision.segments if s.source_id == ids["b.mov"]]
    assert b_rows and all(s.speech_status != "unconfirmed_empty" for s in b_rows)
    reviews = len(_review_jobs(ui, root))
    plans = len(ui.raw.list_segment_plan_revisions(root))
    head = facade.load_head().revision.revision_id
    for name in ("b.mov", "a.mov", "b.mov", "a.mov"):
        _click(ui, root, ids[name])
        assert ui._quick["view_source_id"] == ids[name]
    assert len(_review_jobs(ui, root)) == reviews
    assert len(ui.raw.list_segment_plan_revisions(root)) == plans
    assert facade.load_head().revision.revision_id == head


def test_a_video_analysed_unseen_is_published_on_first_view(tmp_path, monkeypatch):
    ui, root, ids = _analyzed_library(tmp_path, monkeypatch)
    # b는 a를 보는 동안 끝났고 그때 머리가 이미 있었다 — 게시는 b를 볼 때로 미뤄졌다(D68).
    b_task = ui._task_for(root, (ids["b.mov"],))
    assert not ui.raw.review_reflects_analysis(root, b_task["revision"], (ids["b.mov"],))
    before = len(_review_jobs(ui, root))
    _click(ui, root, ids["b.mov"])
    assert len(_review_jobs(ui, root)) == before + 1
    assert ui.raw.review_reflects_analysis(root, b_task["revision"], (ids["b.mov"],))


def test_changed_recommendation_criteria_publish_again_on_view(tmp_path, monkeypatch):
    """추천 기준을 덮으면 근거가 낡는다 — 볼 때 그 기준으로 다시 짓는다(빨강 제안이 새로 선다)."""
    ui, root, ids = _analyzed_library(tmp_path, monkeypatch)
    _click(ui, root, ids["b.mov"])
    _click(ui, root, ids["a.mov"])
    a_task = ui._task_for(root, (ids["a.mov"],))
    assert ui.raw.review_reflects_analysis(root, a_task["revision"], (ids["a.mov"],))
    ui.raw.set_tier_settings(root, {"recommended_min_seconds": 1.0})
    assert not ui.raw.review_reflects_analysis(root, a_task["revision"], (ids["a.mov"],))
    before = len(_review_jobs(ui, root))
    _click(ui, root, ids["b.mov"])
    _click(ui, root, ids["a.mov"])
    assert len(_review_jobs(ui, root)) == before + 2
    assert ui.raw.review_reflects_analysis(root, a_task["revision"], (ids["a.mov"],))


def test_a_head_without_a_basis_record_is_published_again(tmp_path, monkeypatch):
    """기록이 없는 머리(이 판 전의 작업공간)는 믿지 않는다 — 예전처럼 게시한다."""
    ui, root, ids = _analyzed_library(tmp_path, monkeypatch)
    _click(ui, root, ids["b.mov"])
    _click(ui, root, ids["a.mov"])
    with sqlite3.connect(root / DB_NAME) as connection:
        connection.execute("DELETE FROM review_workspace_basis")
    a_task = ui._task_for(root, (ids["a.mov"],))
    assert not ui.raw.review_reflects_analysis(root, a_task["revision"], (ids["a.mov"],))
    before = len(_review_jobs(ui, root))
    _click(ui, root, ids["b.mov"])
    assert len(_review_jobs(ui, root)) == before + 1
    _click(ui, root, ids["a.mov"])
    assert len(_review_jobs(ui, root)) == before + 2
    assert ui.raw.review_reflects_analysis(root, a_task["revision"], (ids["a.mov"],))


def test_a_newer_analysis_of_the_video_is_not_mistaken_for_the_published_one(tmp_path, monkeypatch):
    ui, root, ids = _analyzed_library(tmp_path, monkeypatch)
    _click(ui, root, ids["a.mov"])
    a_task = ui._task_for(root, (ids["a.mov"],))
    assert ui.raw.review_reflects_analysis(root, a_task["revision"], (ids["a.mov"],))
    b_task = ui._task_for(root, (ids["b.mov"],))
    # a의 근거는 a의 분석 행이다 — b의 분석 리비전으로 묻으면 a의 행이 없어 맞지 않는다.
    assert not ui.raw.review_reflects_analysis(root, b_task["revision"], (ids["a.mov"],))
    assert not ui.raw.review_reflects_analysis(root, a_task["revision"], ())


def test_carried_basis_keeps_only_sources_whose_boundaries_did_not_move() -> None:
    old = {"a": [(0, 10), (10, 30)], "b": [(0, 5)], "c": [(0, 9)]}
    new = {"a": [(0, 10), (10, 30)], "b": [(0, 4), (4, 5)], "d": [(0, 3)]}
    basis = {"a": {"revision": "r1"}, "b": {"revision": "r1"}, "c": {"revision": "r2"}}
    assert carried_basis(old, new, basis) == {"a": {"revision": "r1"}}
