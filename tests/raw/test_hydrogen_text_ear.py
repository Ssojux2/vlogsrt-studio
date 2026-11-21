"""W-114c(D187) — Gemini 원본의 글자 귀: 처음·다시 듣는 영상은 Hydrogen, 이미 들은 영상은 그대로.

시험이 못 박는 것:

- 처음 듣는 Gemini 원본은 `hydrogen`이고 결속 꼬리표가 `hydrogen-text/<신원>`이다.
- 이미 들은 Gemini 원본(이 차례가 `none`으로 고정됐거나, 고정 없이 원본 자막만 있는 영상)은
  `none`이고 꼬리표가 없다 — 결속·입력 지문이 바이트 그대로라 이미 산 분석이 낡지 않는다.
- 「다시 듣기」가 기다리면 그 원본은 `hydrogen`이다.
- whisper 원본의 정하는 순서는 그대로다(옛 「새로 듣는 글자 귀」 표지는 Gemini 원본에 닿지
  않는다).
- 「다른 귀로 교차 확인」의 옛 끔 표지는 Hydrogen을 막지 않는다(그 칸은 화면에서 사라졌다).
- 신원은 모델·판·안내문·응답 schema가 바뀌면 바뀐다.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from tests.raw.test_timing_engine_resolve import _seed_document, _workspace
from vlogsrt.project.raw import asr_epoch, cross_check, hydrogen_text_pass, text_ear, timing_engine
from vlogsrt.project.raw.config import (
    HYDROGEN_TEXT,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
)

HYDROGEN_TAG = cross_check.HYDROGEN_TAG_PREFIX + hydrogen_text_pass.text_identity(HYDROGEN_TEXT)


def _gemini(workspace: Any, asset_id: str) -> None:
    timing_engine.pin(workspace, asset_id, 0, TIMING_ENGINE_GEMINI)


def test_a_fresh_gemini_video_hears_text_with_hydrogen(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert timing_engine.resolve(workspace, "new") == TIMING_ENGINE_GEMINI
    assert text_ear.resolve(workspace, "new") == text_ear.EAR_HYDROGEN
    assert cross_check.source_tag(workspace, "new") == HYDROGEN_TAG
    workspace.set_meta_value(text_ear.META_KEY, text_ear.EAR_GPT)  # whisper 시절 표지가 남아도
    assert text_ear.resolve(workspace, "new") == text_ear.EAR_HYDROGEN


def test_an_already_heard_gemini_video_keeps_no_ear_and_no_tag(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _gemini(workspace, "pinned")
    text_ear.pin(workspace, "pinned", 0, text_ear.EAR_NONE)
    _seed_document(workspace, "pinned")
    _gemini(workspace, "unpinned")  # 글자 귀 고정 없이 원본 자막만 있는 Gemini 영상
    _seed_document(workspace, "unpinned")
    for asset_id in ("pinned", "unpinned"):
        assert text_ear.resolve(workspace, asset_id) == text_ear.EAR_NONE
        assert cross_check.source_tag(workspace, asset_id) is None


def test_hearing_again_switches_a_gemini_video_to_hydrogen(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _gemini(workspace, "old")
    text_ear.pin(workspace, "old", 0, text_ear.EAR_NONE)
    _seed_document(workspace, "old")
    asr_epoch.bump(workspace, "old")
    assert text_ear.resolve(workspace, "old") == text_ear.EAR_HYDROGEN
    assert cross_check.source_tag(workspace, "old") == HYDROGEN_TAG


def test_whisper_videos_keep_their_order(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    timing_engine.pin(workspace, "w", 0, TIMING_ENGINE_WHISPER)
    assert text_ear.resolve(workspace, "w") == text_ear.DEFAULT_NEW_HEARING_EAR
    _seed_document(workspace, "w")
    assert text_ear.resolve(workspace, "w") == text_ear.EAR_GPT


def test_an_old_cross_check_off_marker_does_not_stop_hydrogen(tmp_path: Path) -> None:
    """켬·끔 칸은 화면에서 사라졌다 — 예전에 끈 작업공간도 Hydrogen을 쓴다(D187)."""
    workspace = _workspace(tmp_path)
    workspace.set_meta_value(cross_check.META_KEY, cross_check.SETTING_OFF)
    assert text_ear.resolve(workspace, "new") == text_ear.EAR_HYDROGEN
    assert cross_check.source_tag(workspace, "new") == HYDROGEN_TAG
    timing_engine.pin(workspace, "w", 0, TIMING_ENGINE_WHISPER)
    _seed_document(workspace, "w")
    assert cross_check.source_tag(workspace, "w") is None  # gpt 귀는 예전처럼 꺼진다


def test_the_identity_moves_with_what_is_asked() -> None:
    base = hydrogen_text_pass.text_identity(HYDROGEN_TEXT)
    assert base == hydrogen_text_pass.text_identity(HYDROGEN_TEXT)
    for changed in (
        replace(HYDROGEN_TEXT, prompt_revision="hydrogen-text/v2"),
        replace(HYDROGEN_TEXT, thinking_level="MEDIUM"),
        replace(HYDROGEN_TEXT, max_output_tokens=8000),
        replace(HYDROGEN_TEXT, time_decimals=2),
    ):
        assert hydrogen_text_pass.text_identity(changed) != base
