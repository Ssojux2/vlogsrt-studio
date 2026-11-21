"""W-114d(D187) — 이미 들은 Gemini 영상에 「글자 다시 받기(Hydrogen)」: 같은 줄 골격의 새 리비전.

유료 호출은 없다 — 전송 자리에 가짜 opener가 앉는다. 시험이 못 박는 것:

- 이미 들은 영상(글자 귀 `none`)의 범위는 1단계 청크 WAV와 지금 리비전의 줄로 짓는다(무료).
- 누른 digest가 지금 범위와 같을 때만 Hydrogen에 묻고, 받은 글자로 새 리비전 하나를 쓴다 —
  `edited`, 출처 기록 `hydrogen_text`, 시각·아이디·줄 수·순서는 그대로, 처음 들은 글은 남는다.
- 같은 줄을 다시 받으면 캐시에서 읽는다(새로 사지 않는다).
- digest가 다르면·다시 듣기를 기다리면·whisper 영상이면 아무것도 보내지 않는다.
- 사람이 고친 줄은 덮지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw import test_analysis_workflow as _workflow
from tests.raw.test_analysis_workflow import _live_online, needs_ffmpeg
from tests.raw.test_gemini_timing_provider import _client
from tests.raw.test_hydrogen_text_flow import TRANSCRIBED, _run, _TwoEndpoints
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_timing_engine_flow import _gemini_approval, _project
from tests.raw.test_timing_engine_resolve import _seed_document, _workspace
from vlogsrt.project.raw import asr_epoch, source_transcript, text_ear, timing_engine
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.config import HYDROGEN_TEXT, TIMING_ENGINE_WHISPER

media = _workflow.media
stereo_offset = _workflow.stereo_offset


def _heard(tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """W-114c 이전처럼 들은 Gemini 영상 — 글자 귀 `none`, Transcribe 글자."""
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    text_ear.pin(service._workspace(root), asset.asset_id, 0, text_ear.EAR_NONE)
    _run(service, root, _gemini_approval(service, root, asset), _TwoEndpoints())
    return service, root, asset


def _retext(service: Any, root: Path, asset_id: str, digest: str, opener: _TwoEndpoints) -> Any:
    transport = SimpleNamespace(timing_client=_client(opener), on=None)
    handle = service.hydrogen_retext_start(root, asset_id, digest, transport)
    return _wait_terminal(service, root, handle.job_id)


def _rows(service: Any, root: Path, asset_id: str) -> list[dict[str, Any]]:
    document = service.source_transcript(root, asset_id)
    assert document is not None
    rows: list[dict[str, Any]] = document["utterances"]
    return rows


@needs_ffmpeg
def test_an_already_heard_video_takes_hydrogen_text_on_the_same_lines(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _heard(tmp_path, stereo_offset, monkeypatch)
    before = _rows(service, root, asset.asset_id)
    scope = service.hydrogen_retext_scope(root, asset.asset_id)
    assert not isinstance(scope, str) and scope.pieces == 1
    opener = _TwoEndpoints()
    record = _retext(service, root, asset.asset_id, scope.digest, opener)
    assert record.status == "succeeded", record.error
    assert len(opener.hydrogen) == 1 and opener.transcribe == []
    after = _rows(service, root, asset.asset_id)
    assert [row["text"] for row in after] == [f"H:{text}" for text in TRANSCRIBED]
    skeleton = ("utterance_id", "start", "end", "start_sample", "end_sample", "chunk_index")
    assert [[row[k] for k in skeleton] for row in after] == [
        [row[k] for k in skeleton] for row in before
    ]
    assert [row["whisper_text"] for row in after] == TRANSCRIBED
    assert {row["text_source"] for row in after} == {HYDROGEN_TEXT.model}
    document = service.source_transcript(root, asset.asset_id)
    assert document["edited"] is True
    assert document[source_transcript.HYDROGEN_FIELD]["changed"] == 3
    last = service.hydrogen_retext_last(root, asset.asset_id)
    assert last is not None and last["changed"] == 3 and last["asked"] == 1


@needs_ffmpeg
def test_a_changed_digest_sends_nothing(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _heard(tmp_path, stereo_offset, monkeypatch)
    opener = _TwoEndpoints()
    record = _retext(service, root, asset.asset_id, "0" * 64, opener)
    assert record.status == "failed" and raw.HYDROGEN_RETEXT_CHANGED in str(record.error)
    assert opener.hydrogen == []
    assert [row["text"] for row in _rows(service, root, asset.asset_id)] == TRANSCRIBED


@needs_ffmpeg
def test_a_pending_relisten_refuses_before_any_send(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _heard(tmp_path, stereo_offset, monkeypatch)
    scope = service.hydrogen_retext_scope(root, asset.asset_id)
    asr_epoch.bump(service._workspace(root), asset.asset_id)
    assert service.hydrogen_retext_scope(root, asset.asset_id) == raw.HYDROGEN_RETEXT_PENDING
    opener = _TwoEndpoints()
    record = _retext(service, root, asset.asset_id, scope.digest, opener)
    assert record.status == "failed" and opener.hydrogen == []


@needs_ffmpeg
def test_hydrogen_never_overwrites_a_human_edit_and_reuses_its_cache(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _heard(tmp_path, stereo_offset, monkeypatch)
    workspace = service._workspace(root)
    document = service.source_transcript(root, asset.asset_id)
    first = document["utterances"][0]["utterance_id"]
    source_transcript.edit(
        workspace,
        asset.asset_id,
        document["revision"],
        action="edit",
        utterance_id=first,
        text="사람이 고친 글",
    )
    scope = service.hydrogen_retext_scope(root, asset.asset_id)
    record = _retext(service, root, asset.asset_id, scope.digest, _TwoEndpoints())
    assert record.status == "succeeded", record.error
    texts = [row["text"] for row in _rows(service, root, asset.asset_id)]
    assert texts == ["사람이 고친 글", *(f"H:{text}" for text in TRANSCRIBED[1:])]
    # 같은 줄·같은 초안은 이제 없다(글자가 바뀌었다) — 새 범위의 청크는 새 열쇠다.
    again = service.hydrogen_retext_scope(root, asset.asset_id)
    assert not isinstance(again, str) and again.digest != scope.digest


@needs_ffmpeg
def test_an_answer_bought_once_is_never_bought_twice(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """받은 답은 쓰기가 거절돼도 캐시에 남는다 — 같은 범위를 다시 누르면 새로 사지 않는다."""
    service, root, asset = _heard(tmp_path, stereo_offset, monkeypatch)
    scope = service.hydrogen_retext_scope(root, asset.asset_id)
    real = source_transcript.replace_texts

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise raw.RawWorkspaceError(source_transcript.HYDROGEN_REVISION_CHANGED)

    monkeypatch.setattr(source_transcript, "replace_texts", refuse)
    first = _TwoEndpoints()
    assert _retext(service, root, asset.asset_id, scope.digest, first).status == "failed"
    assert len(first.hydrogen) == 1
    assert [row["text"] for row in _rows(service, root, asset.asset_id)] == TRANSCRIBED
    monkeypatch.setattr(source_transcript, "replace_texts", real)
    again = service.hydrogen_retext_scope(root, asset.asset_id)
    assert again.digest == scope.digest and again.pieces == 0
    second = _TwoEndpoints()
    record = _retext(service, root, asset.asset_id, again.digest, second)
    assert record.status == "succeeded", record.error
    assert second.hydrogen == []  # 캐시에서 읽었다
    assert [row["text"] for row in _rows(service, root, asset.asset_id)] == [
        f"H:{text}" for text in TRANSCRIBED
    ]


def test_a_whisper_video_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    timing_engine.pin(workspace, "w", 0, TIMING_ENGINE_WHISPER)
    _seed_document(workspace, "w")
    scope = raw.RawWorkspaceService().hydrogen_retext_scope(tmp_path / "project", "w")
    assert scope == raw.HYDROGEN_RETEXT_WHISPER
