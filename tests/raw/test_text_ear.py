"""원본마다 글자 귀(W-35b, D95) — 정하는 순서·고정·결속 꼬리표.

1. 시간 엔진이 whisper가 아니면 → gpt(Gemini 원본의 다른 귀는 후보로만, D92)
2. 다시 듣기가 기다리면 → 이번 차례에 고정된 귀, 없으면 「새로 듣는 글자 귀」
3. 이번 차례에 고정된 귀 → 그 귀
4. 원본 자막 문서가 이미 있으면 → gpt(W-35b 이전에 들은 영상 — 꼬리표가 바이트 그대로)
5. 처음 듣는 영상 → 「새로 듣는 글자 귀」(기본 Gemini)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_timing_engine_resolve import _seed_document
from tests.raw.test_timing_engine_resolve import _workspace as _any_workspace
from vlogsrt.project.raw import asr_epoch, cross_check, gemini_text, text_ear, timing_engine
from vlogsrt.project.raw.config import (
    GEMINI_TEXT_SETTINGS,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
)
from vlogsrt.project.raw.models import RawWorkspaceError


def _workspace(tmp_path: Path) -> Any:
    """whisper로 새로 듣는 작업공간 — 글자 귀는 whisper 시간 엔진 원본의 것이다. D104부터 새로 듣는
    엔진의 기본은 Gemini라(그 원본에는 귀가 없다) 여기서는 이전 엔진을 고른다."""
    workspace = _any_workspace(tmp_path)
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_WHISPER)
    return workspace


def test_the_default_ear_for_new_hearings_is_gemini() -> None:
    assert text_ear.DEFAULT_NEW_HEARING_EAR == text_ear.EAR_GEMINI


def test_a_never_heard_whisper_video_hears_text_with_gemini(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert text_ear.resolve(workspace, "new") == text_ear.EAR_GEMINI
    gemini_tag = cross_check.GEMINI_TAG_PREFIX + gemini_text.text_identity(GEMINI_TEXT_SETTINGS)
    assert cross_check.source_tag(workspace, "new") == gemini_tag


def test_an_already_heard_video_keeps_the_gpt_ear_and_its_exact_tag(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _seed_document(workspace, "old")
    assert text_ear.resolve(workspace, "old") == text_ear.EAR_GPT
    # W-35b 이전과 같은 꼬리표 — 이미 끝난 분석의 결속·입력 지문이 바뀌지 않는다.
    assert cross_check.source_tag(workspace, "old") == cross_check.binding_tag(workspace)


def test_turning_cross_check_off_removes_every_source_tag(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _seed_document(workspace, "old")
    cross_check.set_enabled(workspace, False)
    assert cross_check.source_tag(workspace, "old") is None
    assert cross_check.source_tag(workspace, "new") is None


def test_a_gemini_timing_video_has_no_other_ear_and_no_text_tag(tmp_path: Path) -> None:
    # D104 — Gemini 시간 엔진 원본에는 다른 귀가 없다(gpt 후보에 돈을 쓰지 않는다).
    workspace = _workspace(tmp_path)
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_GEMINI)
    assert text_ear.resolve(workspace, "g") == text_ear.EAR_NONE
    assert cross_check.source_tag(workspace, "g") is None
    text_ear.pin(workspace, "g", 0, text_ear.EAR_NONE)


def test_the_pin_holds_once_heard_and_a_relisten_takes_the_new_ear(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    text_ear.pin(workspace, "v", 0, text_ear.EAR_GEMINI)  # 1단계가 시작될 때
    _seed_document(workspace, "v")
    assert text_ear.resolve(workspace, "v") == text_ear.EAR_GEMINI  # 문서가 생겨도 고정이 이긴다
    _seed_document(workspace, "w")  # W-35b 이전에 들은 영상
    epoch = asr_epoch.bump(workspace, "w").epoch
    assert text_ear.resolve(workspace, "w") == text_ear.EAR_GEMINI  # 다시 듣기는 새 귀로
    text_ear.pin(workspace, "w", epoch, text_ear.EAR_GEMINI)
    asr_epoch.mark_heard(workspace, "w", epoch)
    text_ear.set_new_hearing_ear(workspace, text_ear.EAR_GPT)  # 나중에 바꿔도
    assert text_ear.resolve(workspace, "w") == text_ear.EAR_GEMINI


def test_a_pin_never_changes_and_unknown_ears_are_refused(tmp_path: Path) -> None:
    workspace: Any = _workspace(tmp_path)
    text_ear.pin(workspace, "v", 0, text_ear.EAR_GEMINI)
    text_ear.pin(workspace, "v", 0, text_ear.EAR_GEMINI)  # 같은 값은 다시 써도 된다
    with pytest.raises(RawWorkspaceError, match="다시 듣기"):
        text_ear.pin(workspace, "v", 0, text_ear.EAR_GPT)
    with pytest.raises(RawWorkspaceError, match="모르는 글자 귀"):
        text_ear.set_new_hearing_ear(workspace, "luna")
