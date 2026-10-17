"""W-24 세션 J — 오래 도는 작업이 최근 50개 창 밖으로 밀려도 「돌고 있음」을 놓치지 않는다.

실측: 0024의 원본 전체 미리보기를 만드는 준비 작업(job_000761)이 도는 동안 짧은 빈 준비 작업이
몇 분 만에 백 개 넘게 쌓였다. 활성 준비 작업을 최근 50개 창에서 거르던 질의는 그 작업을 못 봐
「준비 중」을 「미리보기 낡음」으로 그렸다. 「도는가」·「그 종류의 마지막」은 창이 아니라
상태·종류로 묻는다. 짧은 빈 작업 자체도 줄인다 — 원본 미리보기는 고른 원본의 조각이 있는
쪽만 준비한다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw.test_visual_review import (
    ONE_SECOND_TILE,
    _review_ready,
    _voiced_media,
    needs_ffmpeg,
)
from vlogsrt.project.raw import commands as raw_commands
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import visual_review
from vlogsrt.project.raw.jobs import JobStore
from vlogsrt.project.raw.models import JobRecord
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.workspace import RawWorkspace

NEWER = 55  # 최근 50개 창 밖으로 밀어낸다


@pytest.fixture()
def service() -> raw_service.RawWorkspaceService:
    return raw_service.RawWorkspaceService()


@pytest.fixture()
def root(service: raw_service.RawWorkspaceService, tmp_path: Path) -> Path:
    workspace_root = tmp_path / "ws"
    service.create_workspace(workspace_root, "창")
    return workspace_root


def _job(root: Path, kind: str, fingerprint: str, status: str) -> JobRecord:
    store = JobStore(RawWorkspace.open(root))
    record, _ = store.insert_deduplicated(kind, fingerprint, 0)
    store.update(record.job_id, status=status)
    return store.get(record.job_id)


def _bury(root: Path, kind: str = "visual_prepare", count: int = NEWER) -> None:
    """짧게 끝난 새 작업을 쌓는다 — 실측의 빈 준비 작업 꼴."""
    for index in range(count):
        _job(root, kind, f"newer-{index}", "succeeded")


# ── 저장소 — 종류별 최근 창 ───────────────────────────────────────────────────


def test_recent_jobs_of_a_kind_are_not_pushed_out_by_other_kinds(
    service: raw_service.RawWorkspaceService, root: Path
) -> None:
    old = _job(root, "ingest_copy", "old", "failed")
    _bury(root)
    assert old.job_id not in {job.job_id for job in service.list_jobs(root)}
    assert [job.job_id for job in service.recent_jobs(root, ("ingest_copy",))] == [old.job_id]
    newer = _job(root, "ingest_copy", "newer", "succeeded")
    assert [job.job_id for job in service.recent_jobs(root, ("ingest_copy",))] == [
        newer.job_id,
        old.job_id,
    ]
    assert [job.job_id for job in service.recent_jobs(root, ("ingest_copy",), limit=1)] == [
        newer.job_id
    ]
    every = service.recent_jobs(root, ("visual_prepare",), limit=None)
    assert len(every) == NEWER


def test_workspace_status_counts_an_old_running_job(
    service: raw_service.RawWorkspaceService, root: Path
) -> None:
    _job(root, "visual_prepare", "long-encode", "running")
    _bury(root)
    assert service.workspace_status(root).active_jobs == 1


# ── 실측 증상 — 「준비 중」이 「미리보기 낡음」으로 ─────────────────────────────


@needs_ffmpeg
def test_an_old_running_preparation_still_reads_as_preparing_and_claimed(
    service: raw_service.RawWorkspaceService, root: Path, tmp_path: Path
) -> None:
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, root, card, ONE_SECOND_TILE)
    clock = ReviewWorkspaceFacade(root).load_head(base).revision.clocks[0]
    owner = _job(root, "visual_prepare", "full-source-encode", "running")
    workspace = RawWorkspace.open(root)
    visual_review._write_record(
        root / visual_review._preview_record_relative("prep0"),
        {
            "schema": visual_review.VISUAL_PREVIEW_RECIPE,
            "key": "prep0",
            "source_sha256": clock.source_sha256,
            "audio_ordinal": 0,
            "state": "preparing",
            "built_at": "2026-10-17T00:00:00",
            "job_id": owner.job_id,
        },
    )
    claims = visual_review._write_claims(
        workspace, owner.job_id, {(clock.source_id, clock.source_sha256)}
    )
    _bury(root)
    assert owner.job_id not in {job.job_id for job in service.list_jobs(root)}

    overview = service.visual_review_query(root, base_plan_id=base)

    assert overview.sources[0].state == "preparing"  # 「미리보기 낡음」이 아니다
    assert owner.job_id in overview.active_preparation_jobs
    # 맡음 표시도 그 작업의 것으로 읽힌다 — 썸네일 없는 컷도 「준비 중」이다
    assert {segment.thumb_state for segment in overview.segments} == {"preparing"}
    visual_review._drop_claims(claims)


# ── 분석 — 도는 분석을 놓치지 않는다 ──────────────────────────────────────────


def test_the_model_switch_guard_sees_an_old_running_analysis(
    service: raw_service.RawWorkspaceService, root: Path
) -> None:
    _job(root, raw_service.ANALYSIS_JOB_KIND, "old-analysis", "running")
    _bury(root)
    assert raw_commands._analysis_active(service, root)


def test_cleanup_finds_its_old_success_beyond_the_recent_window(
    service: raw_service.RawWorkspaceService, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """자동 정리의 「이미 세운 머리」 판정 — 창 밖의 성공을 놓치면 사람 결정이 실린 머리를
    다시 세운다."""
    _job(root, "automatic_cleanup", "anal_1", "succeeded")
    _bury(root)
    summary = SimpleNamespace(revision=SimpleNamespace(status="complete", revision_id="anal_1"))
    monkeypatch.setattr(service, "analysis_overview", lambda _root: summary)
    seen: list[bool] = []

    def head(_root: Path, _facade: Any, _analysis: Any, *, initialized: bool) -> Any:
        seen.append(initialized)
        raise RuntimeError("멈춤")

    def run_now(_root: Path, _kind: str, _fingerprint: str, body: Any, **_: Any) -> Any:
        with pytest.raises(RuntimeError, match="멈춤"):
            body(SimpleNamespace(check_cancel=lambda: None))
        return None

    monkeypatch.setattr(service, "_cleanup_head", head)
    monkeypatch.setattr(service, "review_workspace_facade", lambda _root: object())
    monkeypatch.setattr(service, "submit_job", run_now)
    service.start_cleanup(root)
    assert seen == [True]


# ── 빈 준비 작업 — 고른 원본의 조각이 없는 쪽은 걸지 않는다 ────────────────────


def _segment(source_id: str, order: int) -> SimpleNamespace:
    return SimpleNamespace(
        source_id=source_id, source_order=order, start_pts=0, segment_order=order
    )


def test_source_preview_prepares_only_the_pages_holding_the_chosen_source(
    service: raw_service.RawWorkspaceService, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = visual_review.VISUAL_PAGE_SIZE
    segments = [_segment(f"s{index}", index) for index in range(size * 3)]
    head = SimpleNamespace(segments=segments, base_plan_id="plan_1")
    chosen = segments[size + 1].source_id  # 둘째 쪽에만 있다
    assert visual_review.pages_with_sources(head, (chosen,)) == (2,)  # type: ignore[arg-type]
    assert visual_review.pages_with_sources(head, ()) == (1, 2, 3)  # type: ignore[arg-type]
    submitted: list[str] = []
    monkeypatch.setattr(
        service,
        "submit_job",
        lambda _root, _kind, fingerprint, _body, **_: submitted.append(fingerprint),
    )
    monkeypatch.setattr(service, "wait_child_job", lambda *_args: None)
    context = SimpleNamespace(check_cancel=lambda: None)
    service._prepare_preview_pages(root, object(), head, (chosen,), context)  # type: ignore[arg-type]
    assert submitted == [f"plan_1:2:{(chosen,)}"]
    submitted.clear()
    service._prepare_preview_pages(root, object(), head, (), context)  # type: ignore[arg-type]
    assert len(submitted) == 3  # 원본을 고르지 않은 준비는 예전처럼 모든 쪽이다


# ── 작업대 화면 — 그라디오가 있을 때만 ────────────────────────────────────────


@pytest.fixture()
def ui_server(tmp_path: Path, local_socket: None) -> Any:
    pytest.importorskip(
        "gradio",
        reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
        exc_type=ImportError,
    )
    from tests.raw.test_library_close import _opened

    server, opened_root, _library = _opened(tmp_path)
    return server, opened_root


def test_the_status_line_sees_an_old_running_finalcut_send(ui_server: Any) -> None:
    server, root = ui_server
    _job(root, "finalcut_send", "send", "running")
    _bury(root, count=12)  # 예전 창은 최근 10개였다
    message = server.capcut_status_view(server.current_signature())
    assert "Final Cut으로 보내는 중" in message


def test_the_finished_send_is_announced_beyond_the_recent_window(ui_server: Any) -> None:
    server, root = ui_server
    signature = server.current_signature()
    _job(root, "finalcut_send", "send", "succeeded")
    sent = {"path": "/tmp/x.fcpxml", "counts": {"sources": 1, "spine_clips": 1}}
    server._finalcut_sent[signature] = sent
    _bury(root, count=12)
    assert server._finished_send(signature) is sent


def test_resumable_and_active_choices_survive_a_flood_of_short_jobs(ui_server: Any) -> None:
    server, root = ui_server
    failed_analysis = _job(root, raw_service.ANALYSIS_JOB_KIND, "a", "failed")
    failed_ingest = _job(root, raw_service.INGEST_JOB_KIND, "i", "interrupted")
    failed_extract = _job(root, raw_service.SEGMENT_JOB_KIND, "e", "paused")
    running = _job(root, "source_preview", "p", "running")
    _bury(root)
    assert failed_analysis.job_id in {v for _, v in server.resumable_analysis_choices(root)}
    assert failed_ingest.job_id in {v for _, v in server.resumable_ingest_choices(root)}
    assert failed_extract.job_id in {v for _, v in server.resumable_extract_choices(root)}
    assert running.job_id in {v for _, v in server.active_job_choices(root)}


def test_retry_finds_an_old_failure_beyond_the_flood(
    ui_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root = ui_server
    failed = _job(root, "source_preview", "p", "failed")
    _bury(root)
    retried: list[str] = []

    def retry(_signature: Any, _root: Path, job: JobRecord) -> str:
        retried.append(job.job_id)
        return "다시"

    monkeypatch.setattr(server, "_retry_job", retry)
    server._quick = {}
    assert server.capcut_retry_view(server.current_signature()) == "다시"
    assert retried == [failed.job_id]


# ── 닫힌 라이브러리 — 처음 값에 옛 미디어가 없고, 명령 줄 단추가 답한다 ──────────


def test_the_built_workbench_carries_no_media_of_the_open_workspace(ui_server: Any) -> None:
    server, _root = ui_server
    shell, workbench = server._initial_workbench_values()
    assert "/media/" not in shell and "/media/" not in workbench
    from ui import workspace_service

    assert workspace_service.WORKBENCH_LOADING_TEXT in workbench


@pytest.mark.parametrize(
    "handler", ["capcut_cancel_view", "capcut_cancel_all_view", "finalcut_send_view"]
)
def test_command_bar_buttons_answer_while_no_library_is_open(
    tmp_path: Path, local_socket: None, handler: str
) -> None:
    pytest.importorskip("gradio", exc_type=ImportError)
    from tests.raw.test_library_close import _server
    from ui import workspace_service

    server = _server(tmp_path / "state.json")
    server.build_blocks()
    assert server.workspace_root() is None
    assert getattr(server, handler)(None) == workspace_service.LIBRARY_FIRST_TEXT
