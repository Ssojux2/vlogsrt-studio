"""D104(W-52a) — 새로 듣는 영상은 지금부터 Gemini 시간 엔진이고, 그 원본에는 다른 귀가 없다.

유료 호출은 없다(재생 픽스처). 시험이 못 박는 것:

- 새 작업공간에서 처음 듣는 영상은 Gemini로 듣는다 — 견적이 그 엔진·금액을 싣고 gpt 글자 칸·다시
  묻기 칸이 없으며, 결속 꼬리표는 시간 엔진 것 하나다(gpt 교차 확인 꼬리표 없음).
- 1단계는 gpt 글자 선언이 있어도 다른 귀를 묻지 않는다 — 글자 경로 기록(`asr_text`)이 없다.
- 이미 들은 whisper 영상은 그대로 whisper·gpt 귀다(꼬리표 바이트 그대로).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_source_text_flow import _phase_documents, _replay_workspace, _write_replay
from tests.raw.test_timing_engine_resolve import _seed_document
from vlogsrt.project.raw import browser_runtime, cross_check, text_ear, timing_engine
from vlogsrt.project.raw.config import (
    GEMINI_TRANSCRIBE_PROFILE,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@needs_ffmpeg
def test_a_new_video_is_quoted_on_gemini_alone(tmp_path: Path) -> None:
    service, root, asset_id, _ = _replay_workspace(tmp_path)
    workspace = service._workspace(root)
    assert timing_engine.resolve(workspace, asset_id) == TIMING_ENGINE_GEMINI
    assert text_ear.resolve(workspace, asset_id) == text_ear.EAR_NONE
    assert cross_check.enabled(workspace)  # 설정은 켜져 있어도 —
    assert cross_check.source_tag(workspace, asset_id) is None  # Gemini 원본에는 꼬리표가 없다
    estimates = cross_check.with_text_passes(workspace, service.analysis_estimate(root, [asset_id]))
    (estimate,) = estimates
    assert estimate.timing_model == GEMINI_TRANSCRIBE_PROFILE.model
    assert estimate.timing_usd is not None and float(estimate.timing_usd) > 0
    assert estimate.text_chunk_count is None and estimate.text_usd is None
    assert estimate.rescue_chunk_count is None
    assert (
        service.source_bindings(root, {asset_id: object()})[asset_id][-1] == "timing-engine/gemini"
    )
    (approval,) = browser_runtime.mint_asr_approvals(
        service,
        root,
        [asset_id],
        quoted=estimates,
        text_identity=cross_check.request_identity(workspace),
    ).values()
    assert approval.timing_model == GEMINI_TRANSCRIBE_PROFILE.model
    assert approval.text_model is None and approval.text_chunk_budget == 0


@needs_ffmpeg
def test_stage1_asks_no_other_ear_for_a_gemini_video(tmp_path: Path) -> None:
    service, root, asset_id, source_sha = _replay_workspace(tmp_path)
    _write_replay(root, source_sha)  # gpt 글자 선언이 있어도 묻지 않는다
    handle = service.start_analysis(root)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    workspace = service._workspace(root)
    epoch = 0
    assert timing_engine.pinned(workspace, asset_id, epoch) == TIMING_ENGINE_GEMINI
    assert text_ear.pinned(workspace, asset_id, epoch) == text_ear.EAR_NONE
    (outcome,) = _phase_documents(root, asset_id, "asr_outcome.json")
    assert "asr_text" not in outcome


def test_an_already_heard_video_stays_on_whisper_and_its_gpt_tag(tmp_path: Path) -> None:
    from tests.raw.test_timing_engine_resolve import _workspace

    workspace = _workspace(tmp_path)
    _seed_document(workspace, "old")
    assert timing_engine.resolve(workspace, "old") == TIMING_ENGINE_WHISPER
    assert text_ear.resolve(workspace, "old") == text_ear.EAR_GPT
    assert cross_check.source_tag(workspace, "old") == cross_check.binding_tag(workspace)
