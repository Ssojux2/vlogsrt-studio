"""W-14 / D54 — 의미 분석 모델을 바꿔도 전사 신원은 그대로다.

내부 전사 청크 캐시 열쇠와 최종 자막 실행 지문은 워크플로 설정 전체를 묶는다. 그 안에
의미 분석 모델이 들어 있어서, 모델을 Claude로 바꾸면 whisper를 다시 사고(온도 0.6 —
글자가 달라질 수 있다) 전사 지문이 흔들려 Luna 분석까지 낡는다. 전사 전용 열쇠는
`transcription_identity_view`로 의미 분석 모델을 고정한다 — 기본 설정의 열쇠는 W-14
이전과 한 바이트도 다르지 않다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from tests.raw.test_final_caption_run import make_video
from tests.raw.test_understanding import BEFORE_SHUTDOWN, SETTINGS, FakeTimingProvider, make_audio
from vlogsrt.config import Config
from vlogsrt.project.raw import final_caption
from vlogsrt.project.raw.analysis_approval import ASR_APPROVAL_SCHEMA, asr_settings_fingerprint
from vlogsrt.project.raw.analysis_config import (
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.config import RawWorkflowConfig, raw_workflow_config_to_json
from vlogsrt.project.raw.understanding import SampleSpan, transcribe_selected_audio

CLAUDE = "claude-opus-5-5"
SPANS = (SampleSpan(0, 160000), SampleSpan(160000, 320000))


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
    cache = tmp_path / "asr-cache"
    run(tmp_path, RawWorkflowConfig(), cache)
    expected = {
        pre_w14_chunk_key(tmp_path / f"asr-chunk-{position:04d}.wav", span, RawWorkflowConfig())
        for position, span in enumerate(SPANS)
    }
    assert {path.stem for path in cache.glob("*.json")} == expected


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
    assert default == pre_w14_caption_fingerprint(video, SPANS, settings, workflow, engine)
    switched = final_caption.caption_settings_fingerprint(
        video, SPANS, settings, claude(workflow), engine
    )
    assert switched == default


def pre_w14_asr_settings_fingerprint(
    settings: RawUnderstandingSettings, workflow: RawWorkflowConfig
) -> str:
    """W-14 이전 `asr_settings_fingerprint`의 조립 그대로 — 의미 분석 모델이 끝에 묶였다."""
    parts = [
        ASR_APPROVAL_SCHEMA,
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
    assert default == pre_w14_asr_settings_fingerprint(understanding, workflow)
    assert asr_settings_fingerprint(understanding, claude(workflow)) == default


def test_the_asr_approval_fingerprint_still_binds_the_transcription_settings() -> None:
    """고정은 의미 분석 모델 하나뿐이다 — 전사 설정이 바뀌면 지문도 바뀐다."""
    workflow = claude(RawWorkflowConfig())
    default = asr_settings_fingerprint(RawUnderstandingSettings(), workflow)
    shorter = replace(RawUnderstandingSettings(), asr_chunk_max_seconds=60.0)
    assert asr_settings_fingerprint(shorter, workflow) != default
