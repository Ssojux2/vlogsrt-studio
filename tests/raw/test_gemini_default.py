"""D104(W-52a) — 새로 듣는 영상은 지금부터 Gemini 시간 엔진이고, 그 원본에는 다른 귀가 없다.

유료 호출은 없다(재생 픽스처). 시험이 못 박는 것:

- 새 작업공간에서 처음 듣는 영상은 Gemini로 듣는다 — 견적이 그 엔진·금액을 싣고 gpt 글자 칸·다시
  묻기 칸이 없으며, 결속 꼬리표는 시간 엔진 것 하나다(gpt 교차 확인 꼬리표 없음).
- 1단계는 gpt 글자 선언이 있어도 다른 귀를 묻지 않는다 — 글자 경로 기록(`asr_text`)이 없다.
- 이미 들은 whisper 영상은 그대로 whisper·gpt 귀다(꼬리표 바이트 그대로).
- (W-52b1) 재생은 Gemini 보기에서 whisper 모양 선언을 Gemini 이름으로 내지 않는다 — Gemini 응답
  선언(`word-text-fixtures.json`, 같은 요청의 같은 응답)만 재생하고, 없으면 보이는 실패다.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.raw.test_gemini_text_flow import _gemini_payload, _write_gemini
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_source_text_flow import _phase_documents, _replay_workspace, _write_replay
from tests.raw.test_timing_engine_resolve import _seed_document
from vlogsrt.models import Chunk
from vlogsrt.project.raw import (
    browser_runtime,
    cross_check,
    source_transcript,
    text_ear,
    timing_engine,
)
from vlogsrt.project.raw.analysis_workflow import ReplayAsrProvider, ReplayFixtureError
from vlogsrt.project.raw.config import (
    GEMINI_TRANSCRIBE_PROFILE,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
    TIMING_ENGINES,
    RawWorkflowConfig,
    timing_engine_view,
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
    _write_gemini(root, source_sha)
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


def _chunk_call() -> dict[str, object]:
    binding = SimpleNamespace(
        chunk_index=0,
        audio_sha256="wav-sha",
        sample_start=0,
        sample_end=48000,
        sample_rate=16000,
        request_id="req-0",
    )
    return {
        "binding": binding,
        "chunk": Chunk(path=Path("c.wav"), offset=0.0, duration=3.0, index=0),
        "on": date(2026, 10, 30),
    }


def test_replay_never_plays_a_whisper_declaration_under_the_gemini_name() -> None:
    gemini = timing_engine_view(RawWorkflowConfig(), TIMING_ENGINES[TIMING_ENGINE_GEMINI])
    provider = ReplayAsrProvider(declared=((0.5, 1.5, "whisper가 답하면 안 된다"),))
    with pytest.raises(ReplayFixtureError, match="Gemini 응답 선언"):
        provider.timed_transcript(settings=gemini, **_chunk_call())


def test_replay_plays_a_gemini_declaration_through_the_gemini_parser() -> None:
    gemini = timing_engine_view(RawWorkflowConfig(), TIMING_ENGINES[TIMING_ENGINE_GEMINI])
    provider = ReplayAsrProvider(
        declared=((0.5, 1.5, "whisper가 답하면 안 된다"),),
        gemini_declared={0: _gemini_payload()},
    )
    timed = provider.timed_transcript(settings=gemini, **_chunk_call())
    assert timed.model == GEMINI_TRANSCRIBE_PROFILE.model
    assert [segment.text for segment in timed.segments] == ["합성 영상이네요.", "음"]
    assert (timed.segments[0].start, timed.segments[-1].end) == (0.6, 2.3)


def test_replay_keeps_the_whisper_shape_for_a_whisper_view() -> None:
    provider = ReplayAsrProvider(
        declared=((0.5, 1.5, "whisper 글"),), gemini_declared={0: _gemini_payload()}
    )
    timed = provider.timed_transcript(settings=RawWorkflowConfig(), **_chunk_call())
    assert [segment.text for segment in timed.segments] == ["whisper 글"]


@needs_ffmpeg
def test_a_gemini_video_without_a_gemini_declaration_fails_visibly(tmp_path: Path) -> None:
    service, root, asset_id, source_sha = _replay_workspace(tmp_path)
    _write_replay(root, source_sha)  # whisper 모양 선언만 있다
    handle = service.start_analysis(root)
    _wait_terminal(service, root, handle.job_id)
    (outcome,) = _phase_documents(root, asset_id, "asr_outcome.json")
    failures = [chunk["failure"] for chunk in outcome["asr"]["chunks"]]
    assert failures and all("Gemini 응답 선언" in failure for failure in failures)
    document = source_transcript.load(service._workspace(root), asset_id)
    assert (
        document is None or not document["utterances"]
    )  # whisper 글이 Gemini 이름으로 새지 않는다


@needs_ffmpeg
def test_a_gemini_untimed_word_reaches_the_record_and_the_line_display(tmp_path: Path) -> None:
    import json

    from vlogsrt.project.raw import subtitle_suspects

    service, root, asset_id, source_sha = _replay_workspace(tmp_path)
    _write_replay(root, source_sha)
    payload = _gemini_payload()
    (note,) = [
        item
        for step in payload["steps"]  # type: ignore[attr-defined]
        for content in step["content"]
        for item in content["annotations"]
        if item["text"] == "음"
    ]
    del note["start_offset"], note["end_offset"]
    body = {"sources": {source_sha: {"0": payload}}}
    (root / "analysis" / "replay" / "word-text-fixtures.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8"
    )
    handle = service.start_analysis(root)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    (outcome,) = _phase_documents(root, asset_id, "asr_outcome.json")
    (chunk,) = outcome["asr"]["chunks"]
    assert chunk["word_notes"] == [[0, "untimed_words"]]
    document = source_transcript.load(service._workspace(root), asset_id)
    assert document is not None
    (row,) = document["utterances"]
    assert row["text"] == "합성 영상이네요. 음"  # 글자는 남고 시각을 받지 않는다
    timing = service.subtitle_timing(root, asset_id, document)
    found = subtitle_suspects.suspects([(row["utterance_id"], row["text"])], timing=timing)
    assert [s.code for s in found[row["utterance_id"]]] == [subtitle_suspects.ENGINE_WORDS]
