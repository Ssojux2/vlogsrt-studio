"""원본마다 시간 엔진(W-31d, D92) — 정하는 순서와 고정.

1. 다시 듣기가 기다리면 → 이번 차례에 이미 고정된 엔진, 없으면 「새로 듣는 엔진」
2. 이번 차례에 고정된 엔진이 있으면 → 그 엔진
3. 원본 자막 문서가 이미 있으면 → whisper(이미 산 34개는 그대로)
4. 처음 듣는 영상 → 「새로 듣는 엔진」

고정은 1단계가 시작될 때(첫 유료 호출 전) 쓰이고 바뀌지 않는다. whisper 원본의 설정 보기는 같은
객체라 열쇠·지문·결속이 바이트 그대로다(결속 꼬리표가 없다).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.raw import asr_epoch, read_scope, source_transcript, timing_engine
from vlogsrt.project.raw.config import (
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
    TIMING_ENGINES,
    RawWorkflowConfig,
)
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.service import RawWorkspaceService


def _workspace(tmp_path: Path) -> Any:
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "엔진")
    return service._workspace(root)


def _seed_document(workspace: Any, asset_id: str) -> None:
    source_transcript._init(workspace)
    with workspace.connect() as db:
        db.execute(
            "INSERT INTO source_transcripts(asset_id, sequence, revision, payload) VALUES(?,?,?,?)",
            (asset_id, 1, f"rev-{asset_id}", json.dumps({"utterances": []})),
        )
        db.commit()


def test_the_default_new_hearing_engine_is_whisper_until_the_comparison() -> None:
    assert timing_engine.DEFAULT_NEW_HEARING_ENGINE == TIMING_ENGINE_WHISPER


def test_a_never_heard_video_follows_the_new_hearing_engine(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert timing_engine.resolve(workspace, "a") == TIMING_ENGINE_WHISPER
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_GEMINI)
    assert timing_engine.new_hearing_engine(workspace) == TIMING_ENGINE_GEMINI
    assert timing_engine.resolve(workspace, "a") == TIMING_ENGINE_GEMINI
    assert timing_engine.binding_tag(workspace, "a") == "timing-engine/gemini"


def test_an_already_heard_video_stays_whisper_whatever_the_setting(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _seed_document(workspace, "old")
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_GEMINI)
    assert timing_engine.resolve(workspace, "old") == TIMING_ENGINE_WHISPER
    assert timing_engine.binding_tag(workspace, "old") is None
    base = RawWorkflowConfig()
    assert timing_engine.source_workflow(workspace, "old", base) is base


def test_a_relisten_hears_with_the_new_engine_and_the_pin_holds_afterwards(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _seed_document(workspace, "v")
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_GEMINI)
    epoch = asr_epoch.bump(workspace, "v").epoch
    assert timing_engine.resolve(workspace, "v") == TIMING_ENGINE_GEMINI
    timing_engine.pin(workspace, "v", epoch, TIMING_ENGINE_GEMINI)  # 1단계가 시작될 때
    asr_epoch.mark_heard(workspace, "v", epoch)
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_WHISPER)  # 나중에 바꿔도
    assert timing_engine.resolve(workspace, "v") == TIMING_ENGINE_GEMINI
    view = timing_engine.source_workflow(workspace, "v", RawWorkflowConfig())
    assert view.models.timing_model == TIMING_ENGINES[TIMING_ENGINE_GEMINI].model


def test_a_pending_relisten_keeps_the_engine_its_run_already_pinned(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_GEMINI)
    epoch = asr_epoch.bump(workspace, "p").epoch
    timing_engine.pin(workspace, "p", epoch, TIMING_ENGINE_GEMINI)
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_WHISPER)
    assert timing_engine.resolve(workspace, "p") == TIMING_ENGINE_GEMINI


def test_a_pin_never_changes_and_unknown_engines_are_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    timing_engine.pin(workspace, "x", 0, TIMING_ENGINE_WHISPER)
    timing_engine.pin(workspace, "x", 0, TIMING_ENGINE_WHISPER)  # 같은 값은 다시 써도 된다
    with pytest.raises(RawWorkspaceError, match="고정"):
        timing_engine.pin(workspace, "x", 0, TIMING_ENGINE_GEMINI)
    with pytest.raises(RawWorkspaceError, match="엔진"):
        timing_engine.set_new_hearing_engine(workspace, "deepgram")
    with pytest.raises(RawWorkspaceError, match="엔진"):
        timing_engine.pin(workspace, "y", 0, "deepgram")


def test_reading_creates_no_table(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert timing_engine.resolve(workspace, "z") == TIMING_ENGINE_WHISPER
    with workspace.connect() as db:
        found = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (timing_engine.TABLE,)
        ).fetchone()
    assert found is None


def test_a_render_scope_reads_the_engine_state_once(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_GEMINI)
    timing_engine.pin(workspace, "a", 0, TIMING_ENGINE_GEMINI)
    with read_scope.open():
        first = [timing_engine.resolve(workspace, asset) for asset in ("a", "b", "c")]
        timing_engine.set_new_hearing_engine(workspace, TIMING_ENGINE_WHISPER)
        again = [timing_engine.resolve(workspace, asset) for asset in ("a", "b", "c")]
    assert first == again == [TIMING_ENGINE_GEMINI] * 3  # 범위 안에서는 한 번 읽은 값
