"""W-114c(D187) — 실제 1단계에 Hydrogen 글자 귀를 잇는다: 견적·승인·실행·캐시.

유료 호출은 없다 — 전송 자리에 가짜 opener가 앉아 Transcribe(Interactions)와 Hydrogen
(generateContent) 두 끝점에 답한다. 시험이 못 박는 것:

- 처음 듣는 Gemini 원본의 견적 줄은 Hydrogen 분량(청크 전부 · 상한 · 모델)을 싣고, 승인이 그 모델·
  신원·상한을 묶는다(열쇠는 없다 — 줄 골격은 듣기 전에 모른다).
- 승인이 글자를 묶으면 Transcribe가 들은 같은 WAV를 Hydrogen에 묻고, 원본 자막의 글자가 Hydrogen
  것이 된다 — 시각·아이디·줄 수는 Transcribe 그대로다. 이 차례의 글자 귀가 `hydrogen`으로 고정되고
  받은 답은 캐시된다(다시 들으면 묻지 않는다).
- 승인이 글자를 묶지 않으면 Hydrogen 요청은 0회다(Transcribe 글자 그대로, 기록은 `not_requested`).
- Hydrogen 응답이 줄과 어긋나면 그 청크는 Transcribe 글자 그대로 + 까닭이고 캐시하지 않는다.
- 이미 들은 Gemini 원본(`none`)은 승인이 글자를 묶어도 Hydrogen을 묻지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.raw import test_analysis_workflow as _workflow
from tests.raw.replay_helpers import _phase_documents
from tests.raw.test_analysis_workflow import _analyze, _FakeSender, _live_online, needs_ffmpeg
from tests.raw.test_gemini_timing_provider import _client, _Reply
from tests.raw.test_timing_engine_flow import _gemini_approval, _project
from vlogsrt.config import Config
from vlogsrt.project.raw import (
    asr_epoch,
    browser_runtime,
    cross_check,
    hydrogen_text_pass,
    selection_analysis,
    text_ear,
)
from vlogsrt.project.raw.config import HYDROGEN_TEXT

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "gemini"
TRANSCRIBED = ["안녕하세요.", "오늘은 한강에 왔어요!", "날씨 진짜 좋다"]
media = _workflow.media
stereo_offset = _workflow.stereo_offset


class _TwoEndpoints:
    """Transcribe에는 녹화 모양 응답을, Hydrogen에는 받은 줄마다 「H:<초안>」을 돌려준다."""

    def __init__(self, *, broken: bool = False, fail: bool = False, silent: bool = False) -> None:
        self.broken = broken
        self.fail = fail
        self.silent = silent
        self.transcribe: list[dict[str, Any]] = []
        self.hydrogen: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        body = json.loads(request.data)
        if ":generateContent" not in request.full_url:
            self.transcribe.append(body)
            return _Reply((FIXTURES / "transcribe_korean.json").read_bytes())
        self.hydrogen.append(body)
        if self.fail:
            raise OSError("connection reset")
        rows = json.loads(body["contents"][0]["parts"][2]["text"])["lines"]
        items = [{"id": row["id"], "ko": f"H:{row['draft']}", "speech": True} for row in rows]
        if self.silent:  # 첫 줄은 말이 없다고 한다
            items[0] = {"id": rows[0]["id"], "ko": "", "speech": False}
        answer = {"items": items[:-1] if self.broken else items, "extra": []}
        reply = {
            "candidates": [
                {
                    "content": {"parts": [{"text": json.dumps(answer, ensure_ascii=False)}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 60},
            "modelVersion": f"{HYDROGEN_TEXT.model}-001",
        }
        return _Reply(json.dumps(reply, ensure_ascii=False).encode("utf-8"))


def _bound(approvals: dict[str, Any], budget: int = 1) -> dict[str, Any]:
    """1단계 승인에 Hydrogen 글자를 묶는다 — 견적 줄에서 만든 것과 같은 칸."""
    identity = hydrogen_text_pass.text_identity(HYDROGEN_TEXT)
    return {
        asset_id: replace(
            approval,
            text_model=HYDROGEN_TEXT.model,
            text_identity=identity,
            text_chunk_budget=budget,
        )
        for asset_id, approval in approvals.items()
    }


def _run(service: Any, root: Path, approvals: dict[str, Any], opener: _TwoEndpoints) -> Any:
    _handle, record = _analyze(
        service,
        root,
        mode="live",
        asr_approvals=approvals,
        sender=_FakeSender(),
        cfg=Config(),
        timing_client=_client(opener),
    )
    assert record.status == "succeeded", record.error
    return record


def _texts(service: Any, root: Path, asset_id: str) -> list[str]:
    document = service.source_transcript(root, asset_id)
    assert document is not None
    return [row["text"] for row in document["utterances"]]


def _times(service: Any, root: Path, asset_id: str) -> list[tuple[Any, Any, Any]]:
    document = service.source_transcript(root, asset_id)
    assert document is not None
    return [(row["utterance_id"], row["start"], row["end"]) for row in document["utterances"]]


def _text_record(root: Path, asset_id: str) -> dict[str, Any]:
    (outcome,) = _phase_documents(root, asset_id, "asr_outcome.json")
    record: dict[str, Any] = outcome["asr_text"]
    return record


@needs_ffmpeg
def test_the_quote_and_the_approval_bind_hydrogen_for_a_fresh_video(
    tmp_path: Path, stereo_offset: Path
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    quoted = selection_analysis.quote(service, root, (asset.asset_id,))
    (row,) = quoted.estimates
    assert row.text_model == HYDROGEN_TEXT.model
    assert row.text_chunk_count == row.asr_chunk_count and row.text_chunk_keys is None
    assert row.text_usd is not None and float(row.text_usd) > 0
    identity = cross_check.request_identity(service._workspace(root))
    (approval,) = browser_runtime.mint_asr_approvals(
        service, root, [asset.asset_id], quoted=quoted.estimates, text_identity=identity
    ).values()
    assert approval.text_model == HYDROGEN_TEXT.model
    assert approval.text_identity == hydrogen_text_pass.text_identity(HYDROGEN_TEXT)
    assert approval.text_chunk_budget == row.asr_chunk_count
    assert approval.text_chunk_keys is None


@needs_ffmpeg
def test_an_already_heard_gemini_video_quotes_byte_for_byte_as_before(
    tmp_path: Path, stereo_offset: Path
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    workspace = service._workspace(root)
    text_ear.pin(workspace, asset.asset_id, 0, text_ear.EAR_NONE)
    (estimate,) = service.analysis_estimate(root, [asset.asset_id])
    (row,) = cross_check.with_text_passes(workspace, [estimate])
    assert row == cross_check._without_text(estimate)
    assert cross_check.source_tag(workspace, asset.asset_id) is None


@needs_ffmpeg
def test_a_bound_hearing_takes_hydrogen_text_on_transcribe_lines(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    opener = _TwoEndpoints()
    _run(service, root, _bound(_gemini_approval(service, root, asset)), opener)
    assert len(opener.transcribe) == 1 and len(opener.hydrogen) == 1
    sent = opener.hydrogen[0]["contents"][0]["parts"]
    assert sent[1]["inlineData"]["mimeType"] == "audio/wav"  # 같은 청크 WAV를 싣는다
    assert _texts(service, root, asset.asset_id) == [f"H:{text}" for text in TRANSCRIBED]
    record = _text_record(root, asset.asset_id)
    assert (record["status"], record["replaced"], record["model"]) == (
        "applied",
        3,
        HYDROGEN_TEXT.model,
    )
    workspace = service._workspace(root)
    assert text_ear.pinned(workspace, asset.asset_id, 0) == text_ear.EAR_HYDROGEN
    cache = root / "cache" / hydrogen_text_pass.TEXT_CACHE_DIRNAME / asset.asset_id
    assert len(list(cache.glob("*.json"))) == 1


@needs_ffmpeg
def test_hydrogen_never_moves_a_time_or_a_line(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain_service, plain_root, plain = _project(tmp_path / "plain", stereo_offset)
    _live_online(plain_service, plain_root, monkeypatch)
    _run(
        plain_service,
        plain_root,
        _gemini_approval(plain_service, plain_root, plain),
        _TwoEndpoints(),
    )
    service, root, asset = _project(tmp_path / "hydrogen", stereo_offset)
    _live_online(service, root, monkeypatch)
    _run(service, root, _bound(_gemini_approval(service, root, asset)), _TwoEndpoints())
    assert _times(service, root, asset.asset_id) == _times(
        plain_service, plain_root, plain.asset_id
    )


@needs_ffmpeg
def test_without_a_text_binding_hydrogen_is_never_asked(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    opener = _TwoEndpoints()
    _run(service, root, _gemini_approval(service, root, asset), opener)
    assert len(opener.transcribe) == 1 and opener.hydrogen == []
    assert _texts(service, root, asset.asset_id) == TRANSCRIBED
    assert _text_record(root, asset.asset_id)["status"] == "not_requested"


@needs_ffmpeg
def test_a_mismatched_reply_keeps_transcribe_text_and_is_not_cached(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    opener = _TwoEndpoints(broken=True)
    _run(service, root, _bound(_gemini_approval(service, root, asset)), opener)
    assert len(opener.hydrogen) == 1
    assert _texts(service, root, asset.asset_id) == TRANSCRIBED
    record = _text_record(root, asset.asset_id)
    (chunk,) = record["chunks"]
    assert record["status"] == "unavailable" and "missing-id" in chunk["error"]
    cache = root / "cache" / hydrogen_text_pass.TEXT_CACHE_DIRNAME / asset.asset_id
    assert not list(cache.glob("*.json"))


@needs_ffmpeg
def test_an_already_heard_gemini_video_never_asks_hydrogen(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    text_ear.pin(service._workspace(root), asset.asset_id, 0, text_ear.EAR_NONE)
    opener = _TwoEndpoints()
    _run(service, root, _bound(_gemini_approval(service, root, asset)), opener)
    assert len(opener.transcribe) == 1 and opener.hydrogen == []
    assert _texts(service, root, asset.asset_id) == TRANSCRIBED
    (outcome,) = _phase_documents(root, asset.asset_id, "asr_outcome.json")
    assert "asr_text" not in outcome


@needs_ffmpeg
def test_a_transport_failure_keeps_transcribe_text_and_the_job_succeeds(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    opener = _TwoEndpoints(fail=True)
    _run(service, root, _bound(_gemini_approval(service, root, asset)), opener)
    assert len(opener.hydrogen) == 1
    assert _texts(service, root, asset.asset_id) == TRANSCRIBED
    record = _text_record(root, asset.asset_id)
    (chunk,) = record["chunks"]
    assert record["status"] == "unavailable" and chunk["error"]
    cache = root / "cache" / hydrogen_text_pass.TEXT_CACHE_DIRNAME / asset.asset_id
    assert not cache.exists() or not list(cache.glob("*.json"))


@needs_ffmpeg
def test_no_speech_keeps_the_line_and_its_text_and_is_only_recorded(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    _run(service, root, _bound(_gemini_approval(service, root, asset)), _TwoEndpoints(silent=True))
    assert _texts(service, root, asset.asset_id) == [
        TRANSCRIBED[0],
        *(f"H:{text}" for text in TRANSCRIBED[1:]),
    ]
    record = _text_record(root, asset.asset_id)
    (chunk,) = record["chunks"]
    first = service.source_transcript(root, asset.asset_id)["utterances"][0]["utterance_id"]
    assert (record["no_speech"], record["replaced"], chunk["no_speech"]) == (1, 2, [first])
    # 「확인 필요」 표시로만 보인다(W-114e) — 줄·글자는 그대로다.
    transcript = service.source_transcript(root, asset.asset_id)
    timing = service.subtitle_timing(root, asset.asset_id, transcript)
    assert timing.hydrogen_silent == frozenset({first})


@needs_ffmpeg
def test_hearing_again_asks_hydrogen_for_an_already_heard_video(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """이미 들은 영상(`none`)이 Hydrogen 글자를 얻는 길은 「다시 듣기」다 — 사용자가 고르는 길."""
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    workspace = service._workspace(root)
    text_ear.pin(workspace, asset.asset_id, 0, text_ear.EAR_NONE)
    first = _TwoEndpoints()
    _run(service, root, _bound(_gemini_approval(service, root, asset)), first)
    assert first.hydrogen == [] and _texts(service, root, asset.asset_id) == TRANSCRIBED
    asr_epoch.bump(workspace, asset.asset_id)
    again = _TwoEndpoints()
    _run(service, root, _bound(_gemini_approval(service, root, asset)), again)
    assert len(again.transcribe) == 1 and len(again.hydrogen) == 1
    assert _texts(service, root, asset.asset_id) == [f"H:{text}" for text in TRANSCRIBED]
    assert text_ear.pinned(workspace, asset.asset_id, 1) == text_ear.EAR_HYDROGEN


@needs_ffmpeg
def test_a_cached_hydrogen_answer_is_never_bought_twice(
    tmp_path: Path, stereo_offset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    _run(service, root, _bound(_gemini_approval(service, root, asset)), _TwoEndpoints())
    asr_epoch.bump(service._workspace(root), asset.asset_id)  # 같은 WAV·같은 줄을 다시 듣는다
    again = _TwoEndpoints()
    _run(service, root, _bound(_gemini_approval(service, root, asset)), again)
    assert len(again.transcribe) == 1 and again.hydrogen == []  # 캐시에서 읽었다
    assert _texts(service, root, asset.asset_id) == [f"H:{text}" for text in TRANSCRIBED]
    (chunk,) = _text_record_of(root, asset.asset_id, epoch_folders=2)["chunks"]
    assert chunk["cached"] is True


def _text_record_of(root: Path, asset_id: str, *, epoch_folders: int) -> dict[str, Any]:
    """가장 최근 1단계 기록의 글자 귀 기록 — 다시 들으면 기록이 여럿이다."""
    found = _phase_documents(root, asset_id, "asr_outcome.json")
    assert len(found) == epoch_folders
    latest = max(found, key=lambda body: str(body.get("created_at") or ""))
    record: dict[str, Any] = latest["asr_text"]
    return record


def test_the_live_provider_never_sends_past_its_budget() -> None:
    from tests.raw.test_hydrogen_text_io import _Opener as _HydrogenOpener
    from vlogsrt.project.raw.analysis_approval import AnalysisApprovalError

    opener = _HydrogenOpener()
    provider = hydrogen_text_pass.LiveHydrogenProvider(
        client=_client(opener), model=HYDROGEN_TEXT.model, budget=1
    )
    payload = {"contents": [{"role": "user", "parts": [{"text": "x"}]}]}
    provider.generate(payload)
    with pytest.raises(AnalysisApprovalError, match="상한"):
        provider.generate(payload)
    assert len(opener.sent) == 1
