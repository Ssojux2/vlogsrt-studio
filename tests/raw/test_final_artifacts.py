"""A7-2 산출물 게시 — 초안 SRT/VTT 직렬화·재파싱 검사·원자성·리비전 격리.

결과는 손으로 지은 불변 큐로 검사한다 — 게시 계층은 실행 계층과 독립적으로
계약을 지켜야 하므로(§11.7). 실행 전체를 겨냥한 시험은 test_final_caption.py에 있다.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.config import Config
from vlogsrt.models import RETEXT_APPLIED, RetextRecord
from vlogsrt.output import parse_srt
from vlogsrt.project.raw.caption_artifacts import (
    RESPONSES_DIR,
    ArtifactRevisionMismatch,
    ArtifactVerificationError,
    caption_srt_text,
    caption_vtt_text,
    parse_vtt,
    publish_caption_artifacts,
    verify_serialization,
)
from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.final_caption import (
    CaptionCue,
    CaptionRunPlan,
    FinalCaptionResult,
    ResponseEvidence,
)
from vlogsrt.project.raw.final_video import FinalVideoInput
from vlogsrt.project.raw.understanding import PCM_SAMPLE_RATE, SampleSpan, SelectedAudio


def cue(
    start: Fraction, end: Fraction, text: str, *, seg: int = 0, flags: tuple[str, ...] = ()
) -> CaptionCue:
    return CaptionCue(
        start=start,
        end=end,
        text=text,
        whisper_text=text,
        timing_segment_ids=(seg,),
        timing_evidence_id="run-1:timing:0000",
        text_evidence_ids=("run-1:text:0000",),
        text_source="timing_spine",
        timing_source="timing_provider",
        correction_status="whisper",
        flags=flags,
    )


def make_result(
    *,
    revision: str = "a" * 64,
    status: str = "ready",
    cues: tuple[CaptionCue, ...] | None = None,
    evidence: tuple[ResponseEvidence, ...] = (),
) -> FinalCaptionResult:
    return FinalCaptionResult(
        run_id="run-1",
        status=status,
        fingerprint="f" * 64,
        final_video_revision=revision,
        cues=cues
        if cues is not None
        else (cue(Fraction(1, 2), Fraction(5, 2), "망원동 카페에 왔어요"),),
        spine=(),
        repairs=(),
        retext=RetextRecord(status=RETEXT_APPLIED),
        chunks=(),
        evidence=evidence,
        findings=(),
        verify_rows=(),
    )


def make_plan(tmp_path: Path, *, revision: str = "a" * 64) -> CaptionRunPlan:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pcm = tmp_path / "caption.pcm"
    pcm.write_bytes(b"\x00" * 32000)
    source = tmp_path / "final.mp4"
    source.write_bytes(b"synthetic")
    video = FinalVideoInput(
        source_id="final_test",
        final_video_revision=revision,
        original_path=source,
        verified_copy_path=source,
        verified_copy_sha256=revision,
        manifest_path=tmp_path / "final-video.json",
        audio=SelectedAudio(
            source_id="final_test",
            source_path=source,
            source_sha256=revision,
            audio_stream_index=1,
            pcm_path=pcm,
            pcm_sha256="0" * 64,
            sample_rate=PCM_SAMPLE_RATE,
            channels=1,
            sample_count=16000,
            pcm_sample0_file_time=Fraction(0),
        ),
        video_duration=1.0,
        audio_stream_count=1,
    )
    return CaptionRunPlan(
        run_id="run-1",
        video=video,
        spans=(SampleSpan(0, 16000),),
        settings=FinalCaptionSettings(),
        workflow=RawWorkflowConfig(),
        engine=Config(),
        fingerprint="f" * 64,
        estimated_cost_usd=Decimal("0.01"),
        reserved_cost_usd=Decimal("0.01"),
    )


# ─────────────────────────────────────────────────────────────
#  직렬화 — SRT는 엔진 코덱, VTT는 같은 자료의 별도 표기
# ─────────────────────────────────────────────────────────────


def test_srt_uses_engine_codec_and_round_trips(tmp_path: Path) -> None:
    """SRT는 엔진 `write_srt`로 나가고 되읽어도 같다(§11.7 — UTF-8, 1부터 번호)."""
    result = make_result()
    body = caption_srt_text(result)
    assert body.startswith("1\n00:00:00,500 --> 00:00:02,500\n")
    assert parse_srt(body)[0].text == "망원동 카페에 왔어요"


def test_vtt_serializes_and_reparses(tmp_path: Path) -> None:
    """VTT는 마침표 밀리초 표기 — 같은 큐 자료에서 별도 직렬화한다(§11.7)."""
    result = make_result()
    body = caption_vtt_text(result)
    assert body.startswith("WEBVTT")
    assert "00:00:00.500 --> 00:00:02.500" in body
    parsed = parse_vtt(body)
    assert parsed == [(Fraction(1, 2), Fraction(5, 2), "망원동 카페에 왔어요")]


def test_millisecond_rounding_is_half_up(tmp_path: Path) -> None:
    """유리수 시각은 직렬화에서만 밀리초로 반올림한다(§11.6)."""
    result = make_result(cues=(cue(Fraction(1241, 2000), Fraction(3), "반올림"),))
    body = caption_srt_text(result)
    assert "00:00:00,621" in body  # 0.6205 → 621


def test_timeline_looking_text_blocks_publish(tmp_path: Path) -> None:
    """타임라인처럼 읽히는 큐 줄은 재파싱이 어긋난다 — 게시되지 못한다(§11.7)."""
    poisoned_text = "장면 메모\n\n4\n00:00:20,000 --> 00:00:21,000\n꼬리"
    poisoned = (
        cue(Fraction(1, 2), Fraction(5, 2), "망원동 카페에 왔어요"),
        cue(Fraction(3), Fraction(4), poisoned_text, seg=1),
    )
    result = make_result(cues=poisoned)
    with pytest.raises(ArtifactVerificationError):
        verify_serialization(caption_srt_text(result), caption_vtt_text(result))
    outdir = tmp_path / "out"
    with pytest.raises(ArtifactVerificationError):
        publish_caption_artifacts(outdir, result, make_plan(tmp_path))
    assert not outdir.exists()  # 검사를 통과 못하면 아무 파일도 남지 않는다


# ─────────────────────────────────────────────────────────────
#  게시 — 원자성·결정성·증거 기록
# ─────────────────────────────────────────────────────────────


def test_publish_writes_the_full_draft_set(tmp_path: Path) -> None:
    """초안 SRT/VTT·기록·보고서·응답 증거가 한 번의 게시로 전부 나간다(§11.7)."""
    evidence = ResponseEvidence(
        request_id="run-1:timing:0000",
        role="timing",
        chunk_index=0,
        audio_sha256="a" * 64,
        sample_start=0,
        sample_end=16000,
        sample_rate=PCM_SAMPLE_RATE,
        payload={"segments": [{"text": " 망원동", "start": 0.5, "end": 2.5}]},
    )
    result = make_result(evidence=(evidence,))
    artifacts = publish_caption_artifacts(tmp_path / "out", result, make_plan(tmp_path))
    for path in artifacts.paths():
        assert path.is_file() and path.stat().st_size > 0
        assert not path.with_name(path.name + ".partial").exists()  # 반쪽 파일이 없다
    response = tmp_path / "out" / RESPONSES_DIR / "run-1:timing:0000.json"
    body = json.loads(response.read_text(encoding="utf-8"))
    assert body["audio_sha256"] == "a" * 64
    assert body["response"]["segments"][0]["text"] == " 망원동"
    trace = json.loads(artifacts.trace.read_text(encoding="utf-8"))
    assert trace["final_video_revision"] == result.final_video_revision
    assert trace["cues"][0]["text"] == "망원동 카페에 왔어요"
    assert "draft.ko.srt" in str(artifacts.srt)


def test_publish_is_deterministic(tmp_path: Path) -> None:
    """같은 결과를 다시 게시하면 바이트가 같다 — 실행 기록은 흔들리지 않는다."""
    result = make_result()
    first = publish_caption_artifacts(tmp_path / "a", result, make_plan(tmp_path / "a"))
    second = publish_caption_artifacts(tmp_path / "b", result, make_plan(tmp_path / "b"))
    assert first.srt.read_bytes() == second.srt.read_bytes()
    assert first.trace.read_bytes() == second.trace.read_bytes()


def test_publish_refuses_foreign_revision_directory(tmp_path: Path) -> None:
    """다른 리비전의 기록이 있는 자리에는 게시하지 않는다 — 증거가 섞이지 않는다(§11.1)."""
    outdir = tmp_path / "out"
    publish_caption_artifacts(outdir, make_result(), make_plan(tmp_path))
    other = make_result(revision="b" * 64)
    with pytest.raises(ArtifactRevisionMismatch):
        publish_caption_artifacts(outdir, other, make_plan(tmp_path, revision="b" * 64))


def test_draft_language_follows_settings(tmp_path: Path) -> None:
    """언어 코드는 설정을 따라 파일명에 들어간다(§11.7)."""
    result = make_result()
    artifacts = publish_caption_artifacts(
        tmp_path / "out", result, make_plan(tmp_path), language="en"
    )
    assert artifacts.srt.name == "draft.en.srt"
    assert artifacts.vtt.name == "draft.en.vtt"
