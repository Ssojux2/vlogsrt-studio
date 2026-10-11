"""A4 실추출 스모크 — 실제 ffmpeg 경계로 증거 수집 전체가 돈다.

합성 CFR 영상(영상+오디오)을 직접 만들어 `preprocess`의 공개 경계
(`extract_pcm_s16le`·`probe_video_stream`·`extract_frame_index`)를 실제로
부른다 — 네트워크는 없고, ASR·Luna는 주입된 가짜다. 유료 실기는 이 문서의
범위 밖이다(NOT_RUN).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.models import Chunk
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_provider import parse_timing_response
from vlogsrt.project.raw.config import OPENAI_LUNA_MODEL, RawModelRoles, RawWorkflowConfig
from vlogsrt.project.raw.understanding import (
    PreprocessFrameSampler,
    plan_audio_chunks,
    plan_windows,
    propose_segmentation,
    select_audio,
    transcribe_selected_audio,
    utterance_evidence,
    write_stage1_document,
)
from vlogsrt.project.raw.understanding_luna import (
    analysis_fingerprint,
    annotate_windows,
    stage1_document,
    verdicts_from_annotations,
    window_evidence,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

BEFORE_SHUTDOWN = date(2026, 10, 2)


@pytest.fixture
def synthetic_clip(tmp_path: Path) -> Path:
    """6초·25fps CFR 합성 영상 — 정현파 오디오 두 톤(뒤쪽 3초는 조용하다)."""
    path = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:duration=3",
            "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return path


class ReplayingTimingProvider:
    """시간 제공자 가짜 — 청크 길이에 맞춘 세그먼트를 파서 통과 모양으로 돌려준다."""

    def timed_transcript(
        self,
        *,
        binding: Any,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> Any:
        duration = chunk.duration
        half = duration / 4
        return parse_timing_response(
            {
                "segments": [
                    {"text": " 합성 클립 안녕", "start": 0.25, "end": half},
                    {"text": " 망원동 카페", "start": duration - half, "end": duration - 0.1},
                ]
            },
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=duration,
            chunk_offset=chunk.offset,
        )


@needs_ffmpeg
def test_stage1_runs_on_real_synthetic_media(synthetic_clip: Path, tmp_path: Path) -> None:
    """실제 추출→전사→시간창→프레임→판정→분할→문서까지 한 번에 돈다."""
    settings = RawUnderstandingSettings()
    # Luna(OpenAI) 응답 모양의 재생 — W-18(D58) 전에는 기본값이 곧 이 모델이었다
    workflow = RawWorkflowConfig(models=RawModelRoles(luna_model=OPENAI_LUNA_MODEL))
    workdir = tmp_path / "work"
    workdir.mkdir()

    audio = select_audio(synthetic_clip, workdir, source_id="source_synth")
    assert audio.sample_rate == stage1.PCM_SAMPLE_RATE
    assert audio.sample_count > 5 * stage1.PCM_SAMPLE_RATE  # 거의 전체 길이
    assert audio.channels == 1

    from vlogsrt.preprocess import probe_video_stream

    grid = stage1.video_grid_from_stream(probe_video_stream(synthetic_clip))
    assert grid.frame_duration == Fraction(1, 25)
    assert grid.frame_count >= 149  # 6초 × 25fps

    spans = plan_audio_chunks(audio, settings)
    asr = transcribe_selected_audio(
        audio,
        spans,
        settings=workflow,
        understanding=settings,
        provider=ReplayingTimingProvider(),
        on=BEFORE_SHUTDOWN,
        request_id="smoke-1",
        workdir=workdir,
    )
    assert all(chunk.succeeded for chunk in asr.chunks)
    utterances = utterance_evidence(audio, asr)
    assert utterances and all(item.text for item in utterances)

    plan = plan_windows(audio, utterances, settings, grid)
    assert plan.video_status == stage1.VIDEO_PROVEN
    assert all(window.frame_plans for window in plan.windows)

    frames = stage1.sample_frames(
        plan,
        directory=tmp_path / "frames",
        settings=settings,
        sampler=PreprocessFrameSampler(synthetic_clip),
    )
    assert len(frames.frames) == len({item.frame_index for item in plan.windows[0].frame_plans})
    assert frames.failures == ()
    assert all(frame.image_path.stat().st_size > 0 for frame in frames.frames)

    evidence = window_evidence(plan, frames, utterances)
    responder = luna.ReplaySemanticResponder(
        {
            window.window_id: {
                "output_text": json.dumps(
                    {
                        "window_id": window.window_id,
                        "speech_summary": "합성 인사",
                        "visual_summary": "테스트 패턴",
                        "scene_tags": ["talking"],
                        "evidence_ids": sorted(window.utterance_ids)[:1],
                        "suggested_boundary_ids": sorted(window.boundary_ids)[:1],
                        "editing_notes": [],
                        "needs_review": False,
                        "uncertainties": [],
                    },
                    ensure_ascii=False,
                )
            }
            for window in plan.windows
        }
    )
    annotations = annotate_windows(
        evidence, responder, settings=settings, model=workflow.models.luna_model, origin="replay"
    )
    assert all(item.status == luna.ANNOTATION_ACCEPTED for item in annotations)

    proposal = propose_segmentation(
        plan,
        verdicts_from_annotations(annotations),
        utterances,
        asr_failures=[chunk for chunk in asr.chunks if not chunk.succeeded],
    )
    assert proposal.segments  # 전체 보존 — 분할이 없어도 조각은 있다

    fingerprint = analysis_fingerprint(
        audio=audio,
        plan=plan,
        settings=settings,
        asr_model=workflow.models.timing_model,
        luna_model=workflow.models.luna_model,
    )
    document = stage1_document(
        audio=audio,
        asr=asr,
        utterances=utterances,
        plan=plan,
        frames=frames,
        segmentation=proposal,
        annotations=annotations,
        settings=settings,
        workflow=workflow,
        fingerprint=fingerprint,
    )
    path = write_stage1_document(tmp_path / "analysis_transcript.json", document)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["execution_mode"] == "offline_replay"
    assert body["frames"]["frames"]
    assert sorted(tmp_path.rglob("*.srt")) == []  # 첫 단계는 SRT를 만들지 않는다


@needs_ffmpeg
def test_stage1_blocks_vfr_input_for_frames(synthetic_clip: Path, tmp_path: Path) -> None:
    """CFR 증명이 깨지면(지연 만든 VFR) 프레임 증거는 없고 상태로 남는다."""
    vfr = tmp_path / "vfr.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=4",
            "-vf", "select='not(between(n,10,14))'", "-fps_mode", "vfr",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(vfr),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    from vlogsrt.preprocess import probe_video_stream

    with pytest.raises(RuntimeError):
        probe_video_stream(vfr)  # 격자 증명 실패는 preprocess가 크게 닫는다


@needs_ffmpeg
def test_select_audio_rejects_a_stream_without_audio(tmp_path: Path) -> None:
    """오디오 트랙이 없으면 선택 자체가 실패한다 — 무발화 상태로 바꾸지 않는다."""
    silent = tmp_path / "video_only.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(silent),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    with pytest.raises(RuntimeError):
        select_audio(silent, tmp_path / "work", source_id="source_video_only")
