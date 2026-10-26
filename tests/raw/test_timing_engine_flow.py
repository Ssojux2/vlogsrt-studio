"""원본마다 시간 엔진 — 견적·승인·실행·다른 귀·다시 묻기(W-31d, D92).

- whisper 원본은 견적·결속·승인이 바이트 그대로다(칸이 붙지 않는다).
- Gemini로 듣는 원본은 견적에 엔진·금액이 실리고, 결속에 꼬리표가 붙으며, 승인이 그 엔진을 묶는다.
- Gemini 원본의 다른 귀 글자는 자막을 바꾸지 않고 후보로만 남고, 다시 묻기(D78) 칸이 없다.
- 실제 1단계: Gemini 키가 없으면 유료 호출 전에 닫고, 있으면 whisper 전송 없이 Gemini 문으로 듣고
  단어 경계에서 묶은 줄이 원본 자막이 되며, 그 차례가 Gemini로 고정된다.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw import test_analysis_workflow as _workflow
from tests.raw.test_analysis_workflow import (
    _analyze,
    _asr_approval,
    _card,
    _FakeSender,
    _ingest_card,
    _live_online,
    needs_ffmpeg,
)
from tests.raw.test_gemini_timing_provider import _client, _Opener
from tests.raw.test_source_text import GPT, RecordingSender, _request, _whisper
from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import source_text, timing_engine
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_provider import TransportTextProvider
from vlogsrt.project.raw.config import (
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
    TIMING_ENGINES,
    timing_engine_view,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "gemini"
GEMINI = TIMING_ENGINES[TIMING_ENGINE_GEMINI]
# 그 파일의 합성 영상 fixture를 이 파일에서도 찾게 한다.
media = _workflow.media
stereo_offset = _workflow.stereo_offset
TAG = "timing-engine/gemini"


# ── 다른 귀는 Gemini 원본에서 후보만 ───────────────────────────────────────


def _text_pass(tmp_path: Path, gemini: bool) -> tuple[Any, tuple[Any, ...]]:
    tmp_path.mkdir()
    audio, evidence, utterances = _whisper(tmp_path)
    request = _request(tmp_path)
    if gemini:
        request = replace(request, workflow=timing_engine_view(request.workflow, GEMINI))
    provider = TransportTextProvider(sender=RecordingSender(GPT), cfg=Config())
    outcomes = source_text.run_text_pass(audio, evidence, request, provider)
    return source_text.apply_text_pass(utterances, outcomes, request, audio.sample_rate), utterances


def test_a_gemini_source_keeps_its_own_text_and_shows_the_other_ear_as_a_candidate(
    tmp_path: Path,
) -> None:
    whisper, _ = _text_pass(tmp_path / "w", gemini=False)
    gemini, heard = _text_pass(tmp_path / "g", gemini=True)
    replaced = [i for i in whisper.utterances if i.retext_reason == source_text.REASON_ACCEPTED]
    assert replaced  # whisper 원본에서는 게이트를 지난 줄이 바뀐다
    assert [item.text for item in gemini.utterances] == [item.text for item in heard]
    candidates = {
        item.utterance_id: item.alt_text
        for item in gemini.utterances
        if item.retext_reason == source_text.REASON_CANDIDATE
    }
    assert candidates == {item.utterance_id: item.text for item in replaced}
    assert gemini.record["replaced"] == 0


# ── 견적·결속·승인 ────────────────────────────────────────────────────────


def _project(tmp_path: Path, clip: Path) -> tuple[raw.RawWorkspaceService, Path, Any]:
    service = raw.RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "엔진")
    _ingest_card(service, root, _card(tmp_path, "cardG", {"스테레오.mov": clip}))
    service.confirm_global_source_order(root)
    (asset,) = service.list_source_assets(root)
    return service, root, asset


@needs_ffmpeg
def test_only_a_gemini_quote_names_the_engine_its_price_and_its_binding_tag(
    tmp_path: Path,
    stereo_offset: Path,
) -> None:
    from vlogsrt.project.raw.browser_runtime import asr_estimate_digest
    from vlogsrt.project.raw.cross_check import with_text_passes

    service, root, asset = _project(tmp_path, stereo_offset)
    workspace = service._workspace(root)
    (whisper,) = service.analysis_estimate(root, [asset.asset_id])
    assert (whisper.timing_model, whisper.timing_usd) == (None, None)
    (whisper_passes,) = with_text_passes(workspace, [whisper])
    assert whisper_passes.rescue_chunk_count is not None  # whisper에는 다시 묻기 상한이 있다
    assert TAG not in service.source_bindings(root, {asset.asset_id: asset})[asset.asset_id]

    service.set_new_hearing_engine(root, TIMING_ENGINE_GEMINI)
    (gemini,) = service.analysis_estimate(root, [asset.asset_id])
    assert gemini.timing_model == GEMINI.model and gemini.timing_usd is not None
    minimum = Fraction(gemini.asr_seconds) / 60 * Fraction("0.005")
    assert Fraction(gemini.timing_usd) >= minimum
    (gemini_passes,) = with_text_passes(workspace, [gemini])
    assert (gemini_passes.rescue_chunk_count, gemini_passes.rescue_usd) == (None, None)
    assert asr_estimate_digest([gemini]) != asr_estimate_digest([whisper])
    assert TAG in service.source_bindings(root, {asset.asset_id: asset})[asset.asset_id]


@needs_ffmpeg
def test_approvals_bind_the_engine_each_source_hears_with(
    tmp_path: Path,
    stereo_offset: Path,
) -> None:
    from vlogsrt.project.raw.browser_runtime import mint_asr_approvals

    service, root, asset = _project(tmp_path, stereo_offset)
    whisper = mint_asr_approvals(service, root, [asset.asset_id])[asset.asset_id]
    assert whisper.timing_model == TIMING_ENGINES[TIMING_ENGINE_WHISPER].model
    service.set_new_hearing_engine(root, TIMING_ENGINE_GEMINI)
    gemini = mint_asr_approvals(service, root, [asset.asset_id])[asset.asset_id]
    assert gemini.timing_model == GEMINI.model
    assert gemini.settings_fingerprint != whisper.settings_fingerprint


# ── 실제 1단계 ───────────────────────────────────────────────────────────


def _gemini_approval(service: raw.RawWorkspaceService, root: Path, asset: Any) -> dict[str, Any]:
    probe = preprocess.probe_raw_media(root / asset.relative_path)
    workflow = service.source_settings(root, asset.asset_id)
    approval = _asr_approval(
        asset, workflow, RawUnderstandingSettings(), probe.audio[0].stream_index
    )
    return {asset.asset_id: approval}


@needs_ffmpeg
def test_a_live_gemini_hearing_needs_its_key_then_hears_without_whisper(
    tmp_path: Path,
    stereo_offset: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    service.set_new_hearing_engine(root, TIMING_ENGINE_GEMINI)
    approvals = _gemini_approval(service, root, asset)
    sender = _FakeSender([{"start": 0.5, "end": 2.0, "text": "whisper가 답하면 안 된다"}])
    with pytest.raises(raw.AnalysisError, match="GEMINI_API_KEY"):
        service.start_analysis(
            root, mode="live", asr_approvals=approvals, sender=sender, cfg=Config()
        )
    opener = _Opener((FIXTURES / "transcribe_korean.json").read_bytes())
    _handle, record = _analyze(
        service,
        root,
        mode="live",
        asr_approvals=approvals,
        sender=sender,
        cfg=Config(),
        timing_client=_client(opener),
    )
    assert record.status == "succeeded", record.error
    assert sender.calls == [] and len(opener.sent) == 1  # whisper 전송 0회
    assert opener.sent[0]["model"] == GEMINI.model
    document = service.source_transcript(root, asset.asset_id)
    assert document is not None
    texts = [row["text"] for row in document["utterances"]]
    assert texts == ["안녕하세요.", "오늘은 한강에 왔어요!", "날씨 진짜 좋다"]
    workspace = service._workspace(root)
    assert timing_engine.pinned(workspace, asset.asset_id, 0) == TIMING_ENGINE_GEMINI
    service.set_new_hearing_engine(root, TIMING_ENGINE_WHISPER)  # 들은 뒤에 바꿔도
    assert timing_engine.resolve(workspace, asset.asset_id) == TIMING_ENGINE_GEMINI
    assert service.source_settings(root, asset.asset_id).models.timing_model == GEMINI.model


@needs_ffmpeg
def test_an_engine_flip_after_the_quote_is_refused_before_any_send(
    tmp_path: Path,
    stereo_offset: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    approvals = _gemini_approval(service, root, asset)  # whisper로 승인했다
    service.set_new_hearing_engine(root, TIMING_ENGINE_GEMINI)  # 그 뒤에 바꿨다
    sender = _FakeSender()
    opener = _Opener(b"{}")
    with pytest.raises(raw.AnalysisError, match="ASR 모델"):
        service.start_analysis(
            root,
            mode="live",
            asr_approvals=approvals,
            sender=sender,
            cfg=Config(),
            timing_client=_client(opener),
        )
    assert sender.calls == [] and opener.sent == []
    assert timing_engine.pinned(service._workspace(root), asset.asset_id, 0) is None


# ── 빈칸 재전사는 그 원본의 엔진 단가로 ────────────────────────────────────


def test_a_refill_quote_prices_the_timing_role_with_the_engine_the_source_heard_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from vlogsrt.project.raw import capabilities, speech_refill
    from vlogsrt.project.raw.config import RawWorkflowConfig
    from vlogsrt.project.raw.models import RawWorkspaceError

    minute = [(0, 60 * speech_refill.stage1.PCM_SAMPLE_RATE)]
    whisper = SimpleNamespace(workflow=RawWorkflowConfig())
    gemini = SimpleNamespace(workflow=timing_engine_view(RawWorkflowConfig(), GEMINI))
    assert speech_refill._cost(minute, "timing", whisper) == Fraction("0.006")
    assert speech_refill._cost(minute, "timing", gemini) == Fraction("0.005")
    monkeypatch.setattr(capabilities, "timing_price_per_minute", lambda model, on: None)
    with pytest.raises(RawWorkspaceError, match="가격 미정"):
        speech_refill._cost(minute, "timing", gemini)  # 재확인 기한이 지났다 — 0원이 아니다
