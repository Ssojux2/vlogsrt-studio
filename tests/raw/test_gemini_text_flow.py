"""W-35b(D95) 흐름 — 처음 듣는 whisper 영상은 Gemini 글자 귀로 듣는다(재생 픽스처, 유료 호출 없음).

- 1단계가 시작될 때 글자 귀(Gemini)를 고정하고, 받은 단어를 whisper 줄에 배정해 원본 자막에 심는다.
- 같은 입력으로 다시 돌리면 끝난 분석이 지문으로 이어진다(결속 꼬리표가 실행 전후로 같다).
- 견적은 그 영상의 글자 분량을 Gemini 모델·공식 단가로 세고 다시 묻기(D78) 칸이 없다. 승인은 그
  모델과 힌트 없는 Gemini 신원을 묶는다.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_source_text_flow import (
    GPT_TEXT,
    WHISPER_TEXT,
    _phase_documents,
    _replay_workspace,
    _write_replay,
)
from vlogsrt.project.raw import (
    asr_epoch,
    browser_runtime,
    cross_check,
    gemini_text,
    source_text,
    source_transcript,
    text_ear,
)
from vlogsrt.project.raw.config import GEMINI_TEXT_SETTINGS

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

GEMINI_MODEL = GEMINI_TEXT_SETTINGS.model
GEMINI_TEXT = "합성 영상이네요."

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")


def _gemini_payload() -> dict[str, object]:
    words = [("합성", "0.6s", "0.9s"), ("영상이네요", "1.0s", "1.4s"), ("음", "2.0s", "2.3s")]
    text = "합성 영상이네요. 음"
    return {
        "status": "completed",
        "model": GEMINI_MODEL,
        "output_text": text,
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {"type": "word_info", "text": w, "start_offset": s, "end_offset": e}
                            for w, s, e in words
                        ],
                    }
                ],
            }
        ],
    }


def _write_gemini(root: Path, source_sha: str) -> None:
    body = {"sources": {source_sha: {"0": _gemini_payload()}}}
    (root / "analysis" / "replay" / "word-text-fixtures.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8"
    )


@needs_ffmpeg
def test_a_new_video_takes_gemini_words_on_whisper_times_and_a_rerun_reuses_it(
    tmp_path: Path,
) -> None:
    service, root, asset_id, source_sha = _replay_workspace(tmp_path)
    _write_replay(root, source_sha)  # gpt 글자 선언도 있다 — Gemini 귀는 그것을 묻지 않는다
    _write_gemini(root, source_sha)
    first = service.start_analysis(root)
    assert _wait_terminal(service, root, first.job_id).status == "succeeded"
    workspace = service._workspace(root)
    assert text_ear.pinned(workspace, asset_id, asr_epoch.current(workspace, asset_id)) == "gemini"
    document = source_transcript.load(workspace, asset_id)
    assert document is not None
    (row,) = document["utterances"]
    assert (row["text"], row["whisper_text"]) == (GEMINI_TEXT, WHISPER_TEXT)
    assert row["text"] != GPT_TEXT
    assert (row["text_source"], row["retext_reason"]) == (GEMINI_MODEL, gemini_text.REASON_PLACED)
    (outcome,) = _phase_documents(root, asset_id, "asr_outcome.json")
    record = outcome["asr_text"]
    assert (record["status"], record["model"]) == (source_text.PASS_APPLIED, GEMINI_MODEL)
    assert [word["text"] for word in record["unplaced"]] == ["음"]  # 줄 밖 단어는 기록만

    again = service.start_analysis(root)
    assert _wait_terminal(service, root, again.job_id).status == "succeeded"
    latest = service.analysis_overview(root)
    assert latest is not None and [r.status for r in latest.results] == ["cached"]
    assert cross_check.source_tag(workspace, asset_id) == (
        cross_check.GEMINI_TAG_PREFIX + gemini_text.text_identity(GEMINI_TEXT_SETTINGS)
    )


@needs_ffmpeg
def test_the_quote_counts_gemini_minutes_and_the_approval_binds_that_model(tmp_path: Path) -> None:
    service, root, asset_id, _ = _replay_workspace(tmp_path)
    workspace = service._workspace(root)
    estimates = cross_check.with_text_passes(workspace, service.analysis_estimate(root, [asset_id]))
    (estimate,) = estimates
    assert estimate.text_model == GEMINI_MODEL
    assert estimate.text_chunk_count == estimate.asr_chunk_count
    assert estimate.text_usd is not None and float(estimate.text_usd) > 0
    assert estimate.rescue_chunk_count is None  # D78은 Gemini 글자 귀에 없다
    digest = browser_runtime.asr_estimate_digest(estimates)
    # Gemini 글자 모델은 견적 digest에 접힌다(gpt 줄은 칸이 없어 바이트 그대로).
    assert digest != browser_runtime.asr_estimate_digest([replace(estimate, text_model=None)])
    (approval,) = browser_runtime.mint_asr_approvals(
        service,
        root,
        [asset_id],
        quoted=estimates,
        text_identity=cross_check.request_identity(workspace),
    ).values()
    assert approval.text_model == GEMINI_MODEL
    assert approval.text_identity == gemini_text.text_identity(GEMINI_TEXT_SETTINGS)
    assert approval.text_chunk_budget == estimate.text_chunk_count
    assert approval.rescue_chunk_budget == 0
