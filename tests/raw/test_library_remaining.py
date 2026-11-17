"""남은 영상만 분석(W-24 세션 E, D64) — 일괄 승인은 끝나지 않은 영상만 승인·실행한다.

남은 영상 = 준비된 원본 − 다시 쓸 수 있는 저장 결과(단계 글자와 무관) − 지금 돌거나 기다리는 영상.
승인은 남은 영상만 잰다(D110 — 견적 없이) — 끝난 영상의 바뀜(자막 고침)은 남은 영상의 승인을
무르지 않는다. 유료 호출 0회 — 가짜 전송이 센다.
"""

from __future__ import annotations

import shutil

import pytest

from tests.raw.test_background_library import setup_library
from tests.raw.test_parallel_analysis import Gate
from tests.raw.test_selection_analysis import wait_until
from ui import workspace_service
from vlogsrt.project.raw import service as raw

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

DONE = "추천 구간 준비 완료"
FAILED = "분석 확인 필요"
INTERRUPTED = "중단 · 재시도 승인 필요"
REMAINING_ONE = "남은 1개 분석 승인"
AGAIN = "다시 승인"
TRANSCRIBE = raw.SELECTION_MODE_TRANSCRIBE
# 32초 a는 전사 1조각·영상 분석 창 3개, 3초 b는 1조각·1창이다.
A_CALLS = (1, 3)
BOTH_CALLS = (2, 4)


def _stage(ui, root, source_id: str) -> str | None:
    return (ui._task_for(root, (source_id,)) or {}).get("stage")


def _calls(sender, client) -> tuple[int, int]:
    return len(sender.calls), len(client.calls)


def _idle(ui, root) -> bool:
    kinds = (
        "selection_analysis",
        workspace_service.LIBRARY_SCAN_JOB_KIND,
        workspace_service.LIBRARY_APPROVAL_JOB_KIND,
    )
    return (
        not ui._analysis_active
        and not ui._analysis_queue
        and not ui._approval_active(root)
        and not ui.raw.active_jobs(root, kinds)
    )


def _approve_shown(ui) -> None:
    """사람이 일괄 승인 단추를 누른다."""
    ui.library_analysis_view(ui.current_signature())


def _b_failed(tmp_path, monkeypatch):
    """한 번의 일괄 분석에서 a는 끝나고 b만 실패한 작업대."""
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    a, b = ids["a.mov"], ids["b.mov"]
    gate = Gate(ui, fail=(b,), closed=False)
    _approve_shown(ui)
    wait_until(lambda: _stage(ui, root, a) == DONE and _stage(ui, root, b) == FAILED)
    wait_until(lambda: _idle(ui, root))
    assert _calls(sender, client) == A_CALLS
    gate._fail.clear()
    return ui, sender, client, root, a, b, gate


def _edit_caption(ui, root, asset_id: str) -> None:
    document = ui.raw.source_transcript(root, asset_id)
    cue = document["utterances"][0]
    ui.raw.edit_source_transcript(
        root,
        asset_id,
        document["revision"],
        action="edit",
        utterance_id=cue["utterance_id"],
        text="고침",
    )


@needs_ffmpeg
def test_after_one_failure_only_the_failed_video_is_approved_and_run(tmp_path, monkeypatch):
    ui, sender, client, root, a, b, gate = _b_failed(tmp_path, monkeypatch)
    button = wait_until(
        lambda: (got := ui.library_analysis_button()).value == REMAINING_ONE and got
    )
    assert button.interactive
    assert ui._library_pending(root) == (b,)
    entered = len(gate.entered)
    _approve_shown(ui)
    wait_until(lambda: _stage(ui, root, b) == DONE)
    wait_until(lambda: _idle(ui, root))
    assert gate.entered[entered:] == [b]
    assert [len(q.asset_ids) for q in gate.quotes[entered:]] == [1]
    assert _calls(sender, client) == BOTH_CALLS  # a에는 요청이 하나도 가지 않았다
    done = wait_until(lambda: (got := ui.library_analysis_button()).value == "분석 완료" and got)
    assert not done.interactive


@needs_ffmpeg
def test_editing_a_finished_videos_caption_keeps_the_remaining_approval(tmp_path, monkeypatch):
    ui, sender, client, root, a, b, gate = _b_failed(tmp_path, monkeypatch)
    signature = ui.current_signature()
    pending = ui._library_pending(root)
    _edit_caption(ui, root, a)
    entered = len(gate.entered)
    ui.library_analysis_view(signature)
    assert pending == (b,)
    wait_until(lambda: _stage(ui, root, b) == DONE)
    wait_until(lambda: _idle(ui, root))
    assert AGAIN not in str(ui.capcut_status_view(signature))
    assert gate.entered[entered:] == [b]
    assert _calls(sender, client) == BOTH_CALLS
    # 고친 a는 이제 저장 결과로 세지 않는다 — 다음 승인의 남은 영상이다(이번 승인에는 없다).
    wait_until(lambda: ui.library_analysis_button().value == REMAINING_ONE)
    assert ui._library_pending(root) == (a,)


@needs_ffmpeg
def test_a_reusable_result_counts_as_done_whatever_its_stage_says(tmp_path, monkeypatch):
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    a, b = ids["a.mov"], ids["b.mov"]
    _approve_shown(ui)
    wait_until(lambda: _stage(ui, root, a) == DONE and _stage(ui, root, b) == DONE)
    wait_until(lambda: _idle(ui, root))
    assert ui.raw.cached_selection_analysis(root, (b,))
    # 끝난 결과가 남았는데 단계 글자만 「중단」인 영상(서버가 완료 기록 전에 멈췄다)을 흉내 낸다.
    ui._quick_update(ui._task_for(root, (b,))["token"], stage=INTERRUPTED)
    ui._library_cached_ids.pop(root, None)
    ui._scan_library(root)
    wait_until(lambda: root in ui._library_cached_ids and _idle(ui, root))
    assert ui._library_pending(root) == ()
    button = ui.library_analysis_button()
    assert button.value == "분석 완료" and not button.interactive
    calls = _calls(sender, client)
    _approve_shown(ui)  # 다른 탭의 낡은 단추가 눌러도 아무것도 시작하지 않는다
    assert not ui._approval_active(root)
    assert _calls(sender, client) == calls


@needs_ffmpeg
def test_transcribe_only_binds_only_the_remaining_videos(tmp_path, monkeypatch):
    ui, sender, client, root, a, b, gate = _b_failed(tmp_path, monkeypatch)
    signature = ui.current_signature()
    ui.approval_mode_view(signature, TRANSCRIBE)
    assert ui._library_pending(root) == (b,)
    _edit_caption(ui, root, a)  # 끝난 영상의 바뀜은 남은 영상의 승인과 무관하다
    entered = len(gate.entered)
    ui.library_analysis_view(signature)
    wait_until(lambda: _stage(ui, root, b) == raw.TRANSCRIBED_STAGE)
    wait_until(lambda: _idle(ui, root))
    assert gate.entered[entered:] == [b] and gate.quotes[-1].mode == TRANSCRIBE
    assert _calls(sender, client) == (2, 3)  # b의 전사 한 조각뿐 — 영상 분석은 없다
