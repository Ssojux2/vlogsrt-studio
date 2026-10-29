"""D59 19c — 1단계 글자 경로를 작업대·분석 흐름 끝까지 본다(합성 미디어, 유료 호출 0회).

흐름 재료(`setup_library`)는 「다른 귀로 교차 확인」을 끈 작업공간이다(19d — 켠 흐름은
`test_cross_check.py`).

- **교차 확인이 꺼져 있으면 온라인 1단계는 글자 요청을 한 번도 보내지 않는다.** 1단계 승인이 글자
  모델을 묶지 않으므로(19d) 실제 제공자가 만들어지지 않는다. 용어집이 있어도 whisper 요청에는 힌트가
  없다.
- 캐시에 이미 있는 답(교차 확인이 산 것)은 꺼져 있어도 그대로 얹힌다 — 보내는 것은 없다. 글자가
  바뀌어 원본 자막 지문이 바뀌어도 같은 리비전의 2단계가 1단계 기록을 받아들인다(입력 지문을 심은
  뒤 다시 잰다).
- 재생(픽스처) 전체 실행도 같은 길이다. 같은 whisper·글자 답으로 다시 돌면 심기는 아무것도 바꾸지
  않고, 끝난 분석은 지문이 맞아 캐시로 이어진다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_background_library import setup_library
from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_relisten import approve, relisten, stage
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_selection_analysis import wait_until
from vlogsrt import log
from vlogsrt.project.raw import glossary, source_text, source_transcript, text_ear
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    OPENAI_LUNA_MODEL,
    SOURCE_TEXT_SETTINGS,
    RawModelRoles,
)
from vlogsrt.project.raw.understanding import SampleSpan
from vlogsrt.project.raw.workspace import DB_NAME


@pytest.fixture(autouse=True)
def _gpt_ear_for_new_hearings(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일은 gpt 다른 귀(D59 19c·19d)의 흐름을 시험한다. W-35b부터 처음 듣는 whisper 영상의
    글자 귀는 Gemini다(D95) — gpt 귀는 그 전에 들은 영상의 귀로 남는다. 여기서는 처음 듣는 영상도
    gpt 귀로 듣게 해 그 흐름을 그대로 잰다(Gemini 글자 경로는 `test_gemini_text*.py`)."""
    monkeypatch.setattr(text_ear, "DEFAULT_NEW_HEARING_EAR", text_ear.EAR_GPT)


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

WHISPER_TEXT = "합성 영상입니다."
GPT_TEXT = "합성 영상이에요."
DONE = "추천 구간 준비 완료"
TERMS = "왕방\n백숙\n"


def _phase_documents(root: Path, asset_id: str, name: str = "asr_phase.json") -> list[dict]:
    found = []
    for path in sorted((root / "analysis").rglob(name)):
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("asset_id") == asset_id:
            found.append({**body, "_folder": str(path.parent)})
    return found


def _text_request_rows(root: Path) -> int:
    with sqlite3.connect(root / DB_NAME) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_requests'"
        ).fetchone()
        if not exists:
            return 0
        return int(
            db.execute(
                "SELECT COUNT(*) FROM analysis_requests WHERE stage=?",
                (source_text.TEXT_REQUEST_STAGE,),
            ).fetchone()[0]
        )


def _assert_whisper_only(sender: Any) -> None:
    timing = RawModelRoles().timing_model
    for request in sender.calls:
        assert request.model == timing and request.response_format == "verbose_json"
        assert (request.keywords, request.languages, request.prompt) == ((), (), "")


@needs_ffmpeg
def test_online_stage1_sends_no_text_request_and_keeps_whisper_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    glossary.save(ui.raw._workspace(root), TERMS)  # 용어집이 있어도 whisper에는 가지 않는다
    approve(ui, raw.SELECTION_MODE_TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in ids.values()))
    assert len(sender.calls) == 2  # whisper 둘 — 글자 요청은 0
    _assert_whisper_only(sender)
    workspace = ui.raw._workspace(root)
    for asset_id in ids.values():
        (phase,) = _phase_documents(root, asset_id)
        assert phase["asr_text"]["status"] == source_text.STATUS_NOT_REQUESTED
        assert phase["asr_text"]["glossary_terms"] == 2
        assert all(chunk["failure"] == "" for chunk in phase["asr"]["chunks"])
        document = source_transcript.load(workspace, asset_id)
        assert document is not None
        assert all("text_source" not in row for row in document["utterances"])  # 19c 이전 모양
        assert source_transcript.fingerprint(workspace, asset_id) == "asr-original/v1"
    assert not list((root / "cache").glob(f"{source_text.TEXT_CACHE_DIRNAME}/*/*.json"))
    assert _text_request_rows(root) == 0


@needs_ffmpeg
def test_a_cached_text_answer_lands_online_without_a_send_and_stage2_accepts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui, sender, client, root, ids = setup_library(tmp_path, monkeypatch)
    client.resume.set()
    a, b = ids["a.mov"], ids["b.mov"]
    approve(ui, raw.SELECTION_MODE_TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in (a, b)))
    (phase,) = _phase_documents(root, a)
    chunk = phase["asr"]["chunks"][0]
    wav = Path(phase["_folder"]) / "asr-chunk-0000.wav"
    key = source_text.text_chunk_key(
        hashlib.sha256(wav.read_bytes()).hexdigest(),
        SOURCE_TEXT_SETTINGS,
        glossary.glossary_sha(()),
        SampleSpan(chunk["sample_start"], chunk["sample_end"]),
        "live",  # 실제 실행이 산 답(19d) — 재생 답은 다른 열쇠라 온라인이 읽지 않는다
    )
    cache = root / "cache" / source_text.TEXT_CACHE_DIRNAME / a
    cache.mkdir(parents=True)
    body = {"key": key, "text": GPT_TEXT, "languages": ["ko"]}
    (cache / f"{key}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    assert relisten(ui, a)["ok"] is True  # 새 1단계 — whisper 하나를 다시 산다
    approve(ui, raw.SELECTION_MODE_FULL)  # 전사와 영상 분석을 한 승인으로 — 같은 리비전의 2단계
    wait_until(lambda: stage(ui, root, a) == DONE)
    assert len(sender.calls) == 3  # whisper 하나만 더 — 글자는 캐시에서 왔다
    _assert_whisper_only(sender)
    assert _text_request_rows(root) == 0
    workspace = ui.raw._workspace(root)
    document = source_transcript.load(workspace, a)
    assert document is not None and document["edited"] is False
    assert (
        document["sequence"] == 1
    )  # 1단계가 한 번 심었다 — 2단계의 다시 심기는 아무것도 안 바꿨다
    (row,) = document["utterances"]
    assert (row["text"], row["whisper_text"], row["retext_reason"]) == (
        GPT_TEXT,
        WHISPER_TEXT,
        source_text.REASON_ACCEPTED,
    )
    assert row["text_source"] == RawModelRoles().text_model
    assert source_transcript.fingerprint(workspace, a) != "asr-original/v1"
    assert source_transcript.fingerprint(workspace, b) == "asr-original/v1"
    revision = ui._task_for(root, (a,))["revision"]
    (latest,) = [d for d in _phase_documents(root, a) if d["analysis_revision_id"] == revision]
    assert latest["asr_text"]["status"] == source_text.PASS_APPLIED
    assert latest["asr_text"]["chunks"][0]["cached"] is True
    analysis = ui.raw.analysis_document(root, revision, a)
    assert analysis is not None and analysis["execution_mode"] == "online_live"
    assert analysis["transcript_fingerprint"] == source_transcript.fingerprint(workspace, a)
    assert any(GPT_TEXT in json.dumps(call, ensure_ascii=False) for call in client.calls)


def _replay_workspace(tmp_path: Path) -> tuple[raw.RawWorkspaceService, Path, str, str]:
    service = raw.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "글자 재생")
    workflow = service.settings(root)
    models = replace(workflow.models, luna_model=OPENAI_LUNA_MODEL)
    service.update_settings(root, replace(workflow, models=models))
    media = tmp_path / "talk.mov"
    _make_media(media, audio=True, seconds="3")
    handle = service.import_selected_video(root, media)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.ensure_source_order(root)
    (asset,) = service.list_source_assets(root)
    return service, root, asset.asset_id, asset.content_sha256 or ""


def _write_replay(root: Path, source_sha: str, *, declared: bool = True) -> None:
    folder = root / "analysis" / "replay"
    folder.mkdir(parents=True, exist_ok=True)
    asr = {"sources": {source_sha: [{"start": 0.5, "end": 1.5, "text": WHISPER_TEXT}]}}
    luna = {
        "windows": {
            "window_000000": {
                "window_id": "window_000000",
                "speech_summary": "합성 설명",
                "visual_summary": "합성 풍경",
                "scene_tags": ["scenery"],
                "evidence_ids": ["window_000000"],
                "suggested_boundary_ids": [],
                "editing_notes": [],
                "needs_review": False,
                "uncertainties": [],
            }
        }
    }
    text = {"sources": {source_sha: {"0": GPT_TEXT}} if declared else {}}
    for name, body in (("asr", asr), ("luna", luna), ("text", text)):
        (folder / f"{name}-fixtures.json").write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8"
        )


@needs_ffmpeg
def test_a_replay_run_cross_checks_and_a_rerun_neither_reseeds_nor_rebuys(
    tmp_path: Path,
) -> None:
    service, root, asset_id, source_sha = _replay_workspace(tmp_path)
    _write_replay(root, source_sha)
    first = service.start_analysis(root)
    assert _wait_terminal(service, root, first.job_id).status == "succeeded"
    workspace = service._workspace(root)
    document = source_transcript.load(workspace, asset_id)
    assert document is not None and document["sequence"] == 0
    (row,) = document["utterances"]
    assert (row["text"], row["whisper_text"]) == (GPT_TEXT, WHISPER_TEXT)
    overview = service.analysis_overview(root)
    assert overview is not None and overview.revision.status == "complete"
    (result,) = overview.results
    assert result.status == "analyzed"
    analysis = json.loads((root / str(result.document_path)).read_text(encoding="utf-8"))
    assert analysis["transcript_fingerprint"] == source_transcript.fingerprint(workspace, asset_id)
    (outcome,) = _phase_documents(root, asset_id, "asr_outcome.json")
    assert outcome["asr_text"]["status"] == source_text.PASS_APPLIED

    # 같은 입력 — 끝난 분석이 지문으로 이어진다(글자가 바꾼 지문으로 저장됐다).
    again = service.start_analysis(root)
    assert _wait_terminal(service, root, again.job_id).status == "succeeded"
    latest = service.analysis_overview(root)
    assert latest is not None and [r.status for r in latest.results] == ["cached"]
    # 다른 창 설정 — 1단계가 다시 돌아도 같은 whisper·글자 답이라 심기는 아무것도 바꾸지 않는다.
    wider = service.start_analysis(root, settings=RawUnderstandingSettings(window_seconds=15.0))
    assert _wait_terminal(service, root, wider.job_id).status == "succeeded"
    reseeded = source_transcript.load(workspace, asset_id)
    assert reseeded is not None
    assert (reseeded["revision"], reseeded["sequence"]) == (document["revision"], 0)


@needs_ffmpeg
def test_a_text_failure_keeps_whisper_text_and_never_marks_the_transcription_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    service, root, asset_id, source_sha = _replay_workspace(tmp_path)
    _write_replay(root, source_sha, declared=False)  # 글자 픽스처 파일은 있는데 선언이 없다
    handle = service.start_analysis(root)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    overview = service.analysis_overview(root)
    assert overview is not None and [r.status for r in overview.results] == ["analyzed"]
    (outcome,) = _phase_documents(root, asset_id, "asr_outcome.json")
    assert outcome["asr_text"]["status"] == source_text.STATUS_UNAVAILABLE
    assert all(chunk["failure"] == "" for chunk in outcome["asr"]["chunks"])  # 전사는 성공이다
    workspace = service._workspace(root)
    document = source_transcript.load(workspace, asset_id)
    assert document is not None
    assert [row["text"] for row in document["utterances"]] == [WHISPER_TEXT]
    assert all("text_source" not in row for row in document["utterances"])
    assert source_transcript.fingerprint(workspace, asset_id) == "asr-original/v1"
    assert any("1단계 글자 경로 청크 0 실패" in line for line in warnings)
