"""원본마다 시간 엔진 — 견적·승인·실행·다른 귀·다시 묻기(W-31d, D92).

- whisper 원본은 견적·결속·승인이 바이트 그대로다(칸이 붙지 않는다).
- Gemini로 듣는 원본은 견적에 엔진·금액이 실리고, 결속에 꼬리표가 붙으며, 승인이 그 엔진을 묶는다.
- 다른 귀·다시 묻기(D78)는 whisper와 함께 지웠다(D163) — 견적에 그 칸이 없다.
- whisper로 듣는 원본의 실제 1단계는 유료 호출 전에 닫힌다(D163 — 「다시 듣기」로 Gemini).
- 실제 1단계: Gemini 키가 없으면 유료 호출 전에 닫고, 있으면 whisper 전송 없이 Gemini 문으로 듣고
  단어 경계에서 묶은 줄이 원본 자막이 되며, 그 차례가 Gemini로 고정된다.
"""

from __future__ import annotations

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
from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import timing_engine
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
    TIMING_ENGINES,
)

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "gemini"
GEMINI = TIMING_ENGINES[TIMING_ENGINE_GEMINI]
# 그 파일의 합성 영상 fixture를 이 파일에서도 찾게 한다.
media = _workflow.media
stereo_offset = _workflow.stereo_offset
TAG = "timing-engine/gemini"


# ── 다른 귀는 Gemini 원본에서 후보만 ───────────────────────────────────────


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
    # 다른 귀·다시 묻기(D78)는 whisper와 함께 지웠다(D163) — 견적에 그 칸이 없다.
    assert (whisper_passes.rescue_chunk_count, whisper_passes.text_chunk_count) == (None, None)
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
    workspace.set_meta_value(timing_engine.META_KEY, TIMING_ENGINE_WHISPER)  # 옛 표지가 남아도
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


@needs_ffmpeg
def test_a_live_whisper_hearing_is_refused_before_any_send(
    tmp_path: Path,
    stereo_offset: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """whisper는 은퇴했다(D163) — whisper로 듣게 된 원본은 키가 있어도 아무것도 보내지 않는다."""
    service, root, asset = _project(tmp_path, stereo_offset)
    _live_online(service, root, monkeypatch)
    approvals = _gemini_approval(service, root, asset)  # 이 원본은 whisper로 듣게 돼 있다
    assert approvals[asset.asset_id].timing_model == TIMING_ENGINES[TIMING_ENGINE_WHISPER].model
    opener = _Opener(b"{}")
    _handle, record = _analyze(
        service,
        root,
        mode="live",
        asr_approvals=approvals,
        sender=_FakeSender(),
        cfg=Config(),
        timing_client=_client(opener),
    )
    assert opener.sent == []  # 유료 호출 0회
    summary = service.analysis_overview(root)
    assert summary is not None
    (row,) = summary.results
    assert row.status == "failed"  # 조용한 빈 성공이 아니다 — 까닭을 대고 실패로 남는다
    assert "TranscriptionRetired" in str(row.error) and "다시 듣기" in str(row.error)
