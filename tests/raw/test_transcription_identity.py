"""W-14 / D54 — 의미 분석 모델을 바꿔도 전사 신원은 그대로다.

내부 전사 청크 캐시 열쇠와 최종 자막 실행 지문은 워크플로 설정 전체를 묶는다. 그 안에
의미 분석 모델이 들어 있어서, 모델을 Claude로 바꾸면 whisper를 다시 사고(온도 0.6 —
글자가 달라질 수 있다) 전사 지문이 흔들려 Luna 분석까지 낡는다. 전사 전용 열쇠는
`transcription_identity_view`로 의미 분석 모델을 고정한다 — 기본 설정의 열쇠는 W-14
이전과 한 바이트도 다르지 않다.

W-18(D58)에서 새 작업공간의 기본 모델이 Claude Sonnet 5.5가 됐다. 그래서 "W-14 이전 조립"에
넣는 설정은 지금의 기본값이 아니라 **바뀌기 전의 기본 설정** — A0/A1에서 수락된 저장 설정
글자 그대로(`ACCEPTED_SETTINGS_JSON`, gpt-6-luna) — 이다. 기본이 바뀐 뒤의 열쇠가 그 값과
바이트까지 같다는 것이 이 파일의 증명이다. 18b(D58)에서 안내문 리비전이 올라갔다 — 1단계 승인
지문의 조립에 넣는 분석 설정도 같은 이유로 18b 이전의 기본(`PRE_18B_UNDERSTANDING`)이다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from tests.raw.test_final_caption_run import make_video
from tests.raw.test_raw_config import ACCEPTED_SETTINGS_JSON
from tests.raw.test_understanding import BEFORE_SHUTDOWN, SETTINGS, FakeTimingProvider, make_audio
from vlogsrt.config import Config
from vlogsrt.project.raw import final_caption
from vlogsrt.project.raw.analysis_approval import asr_settings_fingerprint
from vlogsrt.project.raw.analysis_config import (
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.config import (
    RawWorkflowConfig,
    raw_workflow_config_from_json,
    raw_workflow_config_to_json,
)
from vlogsrt.project.raw.understanding import SampleSpan, transcribe_selected_audio

CLAUDE = "claude-opus-5-5"
SPANS = (SampleSpan(0, 160000), SampleSpan(160000, 320000))
PRE_W18_DEFAULT = raw_workflow_config_from_json(ACCEPTED_SETTINGS_JSON)
"""W-18 이전의 기본 설정 — 의미 분석 모델이 gpt-6-luna다. 이미 있는 작업공간이 저장한 값이다."""
PRE_18B_UNDERSTANDING = replace(RawUnderstandingSettings(), prompt_revision="raw-suitability-v2")
"""18b(D58) 이전의 기본 분석 설정 — 안내문 리비전만 다르다. 1단계 승인 지문은 이 값으로 조립된다."""


def claude(config: RawWorkflowConfig) -> RawWorkflowConfig:
    return replace(config, models=replace(config.models, luna_model=CLAUDE))


def pre_w14_chunk_key(wav: Path, span: SampleSpan, settings: RawWorkflowConfig) -> str:
    """W-14 이전 `_transcribe_span`의 열쇠 조립 그대로 — 설정 전체의 asdict를 묶는다."""
    return hashlib.sha256(
        json.dumps(
            {
                "wav": hashlib.sha256(wav.read_bytes()).hexdigest(),
                "provider_origin": "FakeTimingProvider",
                "model": settings.models.timing_model,
                "settings": asdict(settings),
                "start": span.start,
                "end": span.end,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def run(tmp_path: Path, settings: RawWorkflowConfig, cache: Path) -> FakeTimingProvider:
    provider = FakeTimingProvider(
        replies={0: [{"text": "첫 문장", "start": 0.0, "end": 4.0}], 1: []}
    )
    transcribe_selected_audio(
        make_audio(tmp_path, seconds=20.0),
        SPANS,
        settings=settings,
        understanding=SETTINGS,
        provider=provider,
        on=BEFORE_SHUTDOWN,
        request_id="run-pin",
        workdir=tmp_path,
        cache_dir=cache,
    )
    return provider


def test_the_default_asr_chunk_key_is_the_pre_w14_key(tmp_path: Path) -> None:
    """기본이 Sonnet 5.5가 돼도(D58) 기본 설정의 청크 열쇠는 W-14 이전의 기본 열쇠 그대로다."""
    assert PRE_W18_DEFAULT.models.luna_model == "gpt-6-luna"
    assert RawWorkflowConfig().models.luna_model != PRE_W18_DEFAULT.models.luna_model
    cache = tmp_path / "asr-cache"
    run(tmp_path, RawWorkflowConfig(), cache)
    expected = {
        pre_w14_chunk_key(tmp_path / f"asr-chunk-{position:04d}.wav", span, PRE_W18_DEFAULT)
        for position, span in enumerate(SPANS)
    }
    assert {path.stem for path in cache.glob("*.json")} == expected


def test_an_existing_luna_workspace_and_a_new_sonnet_one_share_the_transcription(
    tmp_path: Path,
) -> None:
    """옛 작업공간(gpt-6-luna)이 사 둔 전사를 새 기본(Sonnet 5.5)이 그대로 쓴다 — 재구매 0회."""
    cache = tmp_path / "asr-cache"
    first = run(tmp_path, PRE_W18_DEFAULT, cache)
    assert len(first.seen) == len(SPANS)
    keys = sorted(path.name for path in cache.glob("*.json"))
    second = run(tmp_path, RawWorkflowConfig(), cache)
    assert second.seen == []
    assert sorted(path.name for path in cache.glob("*.json")) == keys


def test_a_claude_run_after_a_luna_run_buys_no_transcription(tmp_path: Path) -> None:
    """같은 청크·같은 오디오면 의미 분석 모델이 바뀌어도 전사 캐시가 그대로 맞는다."""
    cache = tmp_path / "asr-cache"
    first = run(tmp_path, RawWorkflowConfig(), cache)
    assert len(first.seen) == len(SPANS)
    keys = sorted(path.name for path in cache.glob("*.json"))
    second = run(tmp_path, claude(RawWorkflowConfig()), cache)
    assert second.seen == []  # 전송 0회 — whisper를 다시 사지 않는다
    assert sorted(path.name for path in cache.glob("*.json")) == keys


def pre_w14_caption_fingerprint(video, spans, settings, workflow, engine) -> str:
    """W-14 이전 `caption_settings_fingerprint`의 조립 그대로."""
    audio = video.audio
    payload = {
        "schema": settings.schema_version,
        "prompt_revision": settings.prompt_revision,
        "final_video_revision": video.final_video_revision,
        "pcm_sha256": audio.pcm_sha256,
        "audio_stream_index": audio.audio_stream_index,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "sample_count": audio.sample_count,
        "pcm_sample0_file_time": str(audio.pcm_sample0_file_time),
        "chunk_max_seconds": settings.chunk_max_seconds,
        "chunk_max_bytes": settings.chunk_max_bytes,
        "spans": [[span.start, span.end] for span in spans],
        "text_model": workflow.models.text_model,
        "timing_model": workflow.models.timing_model,
        "languages": list(workflow.caption.languages),
        "keywords": list(workflow.caption.keywords),
        "registry_revision": workflow.registry.revision,
        "workflow": json.loads(raw_workflow_config_to_json(workflow)),
        "engine_thresholds": final_caption._engine_threshold_snapshot(engine),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_the_final_caption_fingerprint_ignores_the_analysis_model(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    settings, engine, workflow = FinalCaptionSettings(), Config(), RawWorkflowConfig()
    default = final_caption.caption_settings_fingerprint(video, SPANS, settings, workflow, engine)
    assert default == pre_w14_caption_fingerprint(video, SPANS, settings, PRE_W18_DEFAULT, engine)
    switched = final_caption.caption_settings_fingerprint(
        video, SPANS, settings, claude(workflow), engine
    )
    assert switched == default


def pre_w14_asr_settings_fingerprint(
    settings: RawUnderstandingSettings, workflow: RawWorkflowConfig
) -> str:
    """W-14 이전 `asr_settings_fingerprint`의 조립 그대로 — 의미 분석 모델이 끝에 묶였다."""
    parts = [
        "raw-analysis-asr-approval/v1",  # 19d가 승인 기록을 v2로 올려도 지문은 v1 바이트(D59 19d)
        UNDERSTANDING_SCHEMA_VERSION,
        str(settings.window_seconds),
        str(settings.context_seconds),
        str(settings.max_images_per_window),
        str(settings.image_long_edge),
        str(settings.prompt_revision),
        str(settings.asr_chunk_max_seconds),
        str(settings.asr_chunk_max_bytes),
        workflow.models.timing_model,
        workflow.models.luna_model,
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def test_the_asr_approval_fingerprint_ignores_the_analysis_model() -> None:
    """전사 승인은 전사만 묶는다 — 모델을 바꿔도 1단계 승인·전사 지문이 그대로다."""
    understanding, workflow = RawUnderstandingSettings(), RawWorkflowConfig()
    default = asr_settings_fingerprint(understanding, workflow)
    assert default == pre_w14_asr_settings_fingerprint(PRE_18B_UNDERSTANDING, PRE_W18_DEFAULT)
    assert asr_settings_fingerprint(understanding, PRE_W18_DEFAULT) == default
    assert asr_settings_fingerprint(understanding, claude(workflow)) == default


def test_the_asr_approval_fingerprint_still_binds_the_transcription_settings() -> None:
    """고정은 의미 분석 모델 하나뿐이다 — 전사 설정이 바뀌면 지문도 바뀐다."""
    workflow = claude(RawWorkflowConfig())
    default = asr_settings_fingerprint(RawUnderstandingSettings(), workflow)
    shorter = replace(RawUnderstandingSettings(), asr_chunk_max_seconds=60.0)
    assert asr_settings_fingerprint(shorter, workflow) != default
