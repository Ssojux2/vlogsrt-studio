"""A4 이해 라이브러리의 결정적 층 — 전사·시간창·프레임·경계·분할·직렬화.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7.3·§7.4·§7.6·§13.3. 네트워크는
전혀 나가지 않는다 — 시간 제공자·프레임 추출기는 가짜로 주입한다. ffmpeg가
필요한 실추출 경로는 test_understanding_media_smoke.py가 실제 미디어로 본다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.models import Chunk
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_provider import parse_timing_response
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.understanding import (
    AnalysisPlanningError,
    AsrChunkOutcome,
    AsrEvidence,
    FramePlan,
    FrameSamplingResult,
    SampleSpan,
    SegmentProposal,
    SelectedAudio,
    UtteranceEvidence,
    VideoGrid,
    WindowVerdict,
    audio_absent_proposal,
    plan_audio_chunks,
    plan_windows,
    propose_segmentation,
    sample_frames,
    stage1_evidence_document,
    transcribe_selected_audio,
    utterance_evidence,
    write_pcm_slice,
    write_stage1_document,
)

BEFORE_SHUTDOWN = date(2026, 10, 2)
SETTINGS = RawUnderstandingSettings()


def make_audio(tmp_path: Path, *, seconds: float = 40.0, channels: int = 1) -> SelectedAudio:
    """합성 PCM 위의 선택 오디오 — 잰 원점은 음수도 되는 실측값의 하나다."""
    samples = int(seconds * stage1.PCM_SAMPLE_RATE)
    pcm = tmp_path / "analysis.pcm"
    pcm.write_bytes(bytes((index * 7 + 3) % 256 for index in range(samples * channels * 2)))
    return SelectedAudio(
        source_id="source_test",
        source_path=tmp_path / "original.mp4",
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=pcm,
        pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
        sample_rate=stage1.PCM_SAMPLE_RATE,
        channels=channels,
        sample_count=samples,
        pcm_sample0_file_time=Fraction(-1, 50),
    )


def utterance(
    ordinal: int,
    start: float,
    end: float,
    text: str = "망원동 카페",
    *,
    chunk_index: int = 0,
) -> UtteranceEvidence:
    """발화 증거 하나 — 좌표는 초에서 틱으로 내린 모양이다."""
    start_sample = round(start * stage1.PCM_SAMPLE_RATE)
    end_sample = round(end * stage1.PCM_SAMPLE_RATE)
    return UtteranceEvidence(
        utterance_id=f"utterance_{ordinal:06d}",
        chunk_index=chunk_index,
        position=ordinal,
        text=text,
        start=Fraction(start_sample, stage1.PCM_SAMPLE_RATE),
        end=Fraction(end_sample, stage1.PCM_SAMPLE_RATE),
        start_sample=start_sample,
        end_sample=end_sample,
        evidence_id=f"run:{chunk_index}:{ordinal}",
    )


def outcome(
    index: int, span: SampleSpan, segments: list[dict[str, Any]], *, failure: str = ""
) -> AsrChunkOutcome:
    return AsrChunkOutcome(
        chunk_index=index,
        sample_start=span.start,
        sample_end=span.end,
        wav_sha256="b" * 64,
        request_id=f"run-1:chunk-{index:04d}",
        transcript=tuple((seg["start"], seg["end"], seg["text"]) for seg in segments),
        failure=failure,
    )


# ─────────────────────────────────────────────────────────────
#  청크 커버리지 — 선택 오디오 전체를 한 번에 덮는다
# ─────────────────────────────────────────────────────────────


def test_chunk_plan_covers_whole_audio_without_gaps(tmp_path: Path) -> None:
    """단일 패스 커버리지 — 순서·빈틈·겹침 없음, 마지막 청크는 남은 만큼."""
    audio = make_audio(tmp_path, seconds=40.0)
    short = replace(SETTINGS, asr_chunk_max_seconds=15.0)
    spans = plan_audio_chunks(audio, short)
    assert [(span.start, span.end) for span in spans] == [
        (0, 240000),
        (240000, 480000),
        (480000, 640000),
    ]
    assert spans[-1].end - spans[-1].start == 160000  # 남은 만큼 짧다 — 늘리지 않는다


def test_chunk_plan_honors_the_byte_ceiling(tmp_path: Path) -> None:
    """바이트 상한이 시간 상한보다 먼저 끊는다 — 작은 쪽이 이긴다."""
    audio = make_audio(tmp_path, seconds=40.0)
    tight = replace(SETTINGS, asr_chunk_max_bytes=16000 * 4)
    spans = plan_audio_chunks(audio, tight)
    # 64,000바이트 / 2바이트 mono 프레임 = 32,000 샘플(2초)이 시간 상한(180초)보다 작다
    assert all(span.end - span.start <= 32000 for span in spans)
    assert spans[0].end == 32000


def test_pcm_slice_round_trips_exact_bytes(tmp_path: Path) -> None:
    """WAV 슬라이스는 헤더만 붙인다 — 데이터 바이트는 저장 PCM 그대로다."""
    import wave

    audio = make_audio(tmp_path, seconds=2.0)
    span = SampleSpan(16000, 32000)
    path = write_pcm_slice(audio, span, tmp_path / "chunk.wav")
    with path.open("rb") as handle:
        raw = handle.read()
    with wave.open(str(path), "rb") as parsed:
        assert parsed.getframerate() == stage1.PCM_SAMPLE_RATE
        assert parsed.getnchannels() == 1
        frames = parsed.readframes(parsed.getnframes())
    with audio.pcm_path.open("rb") as source:
        source.seek(16000 * 2)
        expected = source.read(16000 * 2)
    assert frames == expected
    assert raw[44:] == expected  # 표준 헤더 뒤가 전부 데이터다


def test_pcm_slice_rejects_out_of_range(tmp_path: Path) -> None:
    """범위 밖 슬라이스는 못 만든다 — 조용히 자르는 길이 없다."""
    audio = make_audio(tmp_path, seconds=1.0)
    with pytest.raises(AnalysisPlanningError):
        write_pcm_slice(audio, SampleSpan(-5, 100), tmp_path / "a.wav")
    with pytest.raises(AnalysisPlanningError):
        write_pcm_slice(audio, SampleSpan(100, 100), tmp_path / "b.wav")
    with pytest.raises(AnalysisPlanningError):
        write_pcm_slice(audio, SampleSpan(0, 10**9), tmp_path / "c.wav")


# ─────────────────────────────────────────────────────────────
#  내부 전사 — 주입된 TimingProvider, 실패는 불확실성으로
# ─────────────────────────────────────────────────────────────


class FakeTimingProvider:
    """시간 제공자 가짜 — 확인된 전송 결과를 그대로 파서에 넣은 모양으로 돌려준다."""

    def __init__(
        self, *, replies: dict[int, list[dict[str, Any]]], fail: set[int] | None = None
    ) -> None:
        self.replies = replies
        self.fail = fail or set()
        self.seen: list[tuple[str, int, int]] = []

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
        self.seen.append((binding.request_id, binding.sample_start, binding.sample_end))
        if chunk.index in self.fail:
            raise RuntimeError("시간 제공자가 닫혀 있다")
        return parse_timing_response(
            {"segments": self.replies[chunk.index]},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


def test_transcribe_covers_every_chunk_with_bound_identity(tmp_path: Path) -> None:
    """청크마다 묶은 신원·샘플 범위로 호출한다 — 커버리지와 신원이 어긋나지 않는다."""
    audio = make_audio(tmp_path, seconds=20.0)
    spans = (SampleSpan(0, 160000), SampleSpan(160000, 320000))
    provider = FakeTimingProvider(
        replies={
            0: [{"text": "첫 문장", "start": 0.0, "end": 4.0}],
            1: [{"text": "둘째", "start": 1.5, "end": 3.5}],
        }
    )
    evidence = transcribe_selected_audio(
        audio,
        spans,
        settings=RawWorkflowConfig(),
        understanding=SETTINGS,
        provider=provider,
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
    )
    assert provider.seen == [
        ("run-1:chunk-0000", 0, 160000),
        ("run-1:chunk-0001", 160000, 320000),
    ]
    assert all(chunk.succeeded for chunk in evidence.chunks)
    assert [chunk.sample_start for chunk in evidence.chunks] == [0, 160000]
    assert all((tmp_path / f"asr-chunk-{i:04d}.wav").is_file() for i in range(2))


def test_failed_chunk_is_recorded_not_converted_to_silence(tmp_path: Path) -> None:
    """실패 청크는 실패로 남는다 — 무발화·빈 성공으로 바뀌지 않는다(§7.3)."""
    audio = make_audio(tmp_path, seconds=20.0)
    provider = FakeTimingProvider(replies={1: []}, fail={0})
    evidence = transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000), SampleSpan(160000, 320000)),
        settings=RawWorkflowConfig(),
        understanding=SETTINGS,
        provider=provider,
        on=BEFORE_SHUTDOWN,
        request_id="run-2",
        workdir=tmp_path,
    )
    first, second = evidence.chunks
    assert not first.succeeded and "닫혀 있다" in first.failure
    assert first.transcript == ()
    assert second.succeeded


def test_utterances_project_onto_the_pcm_tick_grid(tmp_path: Path) -> None:
    """세그먼트 시각은 청크 국소 초에서 전역 틱으로 — 원점 추정은 없다."""
    audio = make_audio(tmp_path, seconds=20.0)
    evidence = AsrEvidence(
        request_id="run-3",
        sample_rate=stage1.PCM_SAMPLE_RATE,
        chunks=(
            outcome(
                0,
                SampleSpan(0, 320000),
                [
                    {"start": 0.5, "end": 2.0, "text": "안녕"},
                    {"start": 3.0, "end": 4.5, "text": "망원동"},
                ],
            ),
        ),
    )
    utterances = utterance_evidence(audio, evidence)
    assert [item.utterance_id for item in utterances] == ["utterance_000000", "utterance_000001"]
    assert utterances[0].start_sample == 8000 and utterances[0].end_sample == 32000
    assert utterances[0].start == Fraction(1, 2)
    assert utterances[1].start_sample == 48000
    assert utterances[1].evidence_id == f"{evidence.chunks[0].request_id}:1"


def test_utterance_rounding_clamps_at_chunk_end(tmp_path: Path) -> None:
    """반올림이 청크 끝을 넘서면 청크 끝에서 닫는다 — 시각을 늘리지 않는다."""
    audio = make_audio(tmp_path, seconds=10.0)
    evidence = AsrEvidence(
        request_id="run-4",
        sample_rate=stage1.PCM_SAMPLE_RATE,
        chunks=(outcome(0, SampleSpan(0, 160000), []),),
    )
    chunk = evidence.chunks[0]
    projected = stage1._project_segment(audio, chunk, 0, 9.9997, 10.0, "끝자락", 0)
    assert projected.end_sample == 160000  # 청크 끝에서 닫혔다
    assert projected.start_sample == 159995


# ─────────────────────────────────────────────────────────────
#  시간창 — 코어·문맥·프레임 표본·경계 후보
# ─────────────────────────────────────────────────────────────


def test_windows_cover_the_extent_with_clamped_context(tmp_path: Path) -> None:
    """코어는 전체를 덮고 문맥은 범위 안에서만 넓혀진다(§7.4)."""
    audio = make_audio(tmp_path, seconds=40.0)
    plan = plan_windows(audio, [], SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    assert plan.extent == Fraction(40)
    assert [window.window_id for window in plan.windows] == [
        "window_000000",
        "window_000001",
        "window_000002",
    ]
    first, last = plan.windows[0], plan.windows[-1]
    assert (first.core_start, first.core_end) == (Fraction(0), Fraction(15))
    assert (first.context_start, first.context_end) == (Fraction(0), Fraction(17))
    assert (last.core_start, last.core_end) == (Fraction(30), Fraction(40))
    assert (last.context_start, last.context_end) == (Fraction(28), Fraction(40))
    assert plan.windows[1].context_start == Fraction(13)  # 문맥 겹침은 추적용이다


def test_frame_plans_distribute_across_the_window(tmp_path: Path) -> None:
    """표본은 문맥 전체에 분포한다 — 순서·중복 없음이 계약이다(§7.4)."""
    audio = make_audio(tmp_path, seconds=40.0)
    video = VideoGrid(0, Fraction(0), Fraction(1, 25), 1000)
    plan = plan_windows(audio, [], SETTINGS, video)
    first = plan.windows[0]
    indices = [item.frame_index for item in first.frame_plans]
    assert len(indices) == 8 and indices == sorted(set(indices))  # 분포·중복 없음
    assert indices[0] > 0 and indices[-1] < 425  # 문맥 [0,17)초 안에 고르게
    gaps = [b - a for a, b in zip(indices, indices[1:], strict=False)]
    assert max(gaps) - min(gaps) <= 1  # 균등 간격(틱 반올림 ±1)


def test_frame_plans_respect_origin_arithmetic(tmp_path: Path) -> None:
    """프레임 번호는 `잰 원점 차이` 한 번의 산술로 온다(§13.3)."""
    audio = make_audio(tmp_path, seconds=40.0)
    # 오디오 원점 -0.02초, 비디오 원점 +0.5초 — 격자 환산이 이 차이를 그대로 쓴다
    shifted = replace(audio, pcm_sample0_file_time=Fraction(-1, 50))
    video = VideoGrid(0, Fraction(1, 2), Fraction(1, 25), 1000)
    plan = plan_windows(shifted, [], SETTINGS, video)
    moments = [item.window_time for item in plan.windows[0].frame_plans]
    for item, moment in zip(plan.windows[0].frame_plans, moments, strict=True):
        expected = (shifted.file_time(moment) - video.presentation_origin) / video.frame_duration
        assert item.frame_index == int(expected)  # 안쪽(이른 프레임)으로 내린다


def test_unproven_video_means_no_frames_not_a_failure(tmp_path: Path) -> None:
    """비디오 증명 실패는 프레임 없음 + 상태 기록 — 오디오 증거는 살아 있다."""
    audio = make_audio(tmp_path, seconds=20.0)
    plan = plan_windows(
        audio, [], SETTINGS, None, video_status=stage1.VIDEO_UNPROVEN, video_note="VFR"
    )
    assert plan.video_status == stage1.VIDEO_UNPROVEN and plan.video_note == "VFR"
    assert all(not window.frame_plans for window in plan.windows)


def test_boundary_candidates_follow_utterance_ends_and_window_edges(tmp_path: Path) -> None:
    """후보는 발화 종료(코어 안 소유)와 창 끝 길이 경계다 — 아이디는 틱에서 온다."""
    audio = make_audio(tmp_path, seconds=40.0)
    utterances = [utterance(0, 1.0, 5.0), utterance(1, 16.0, 22.0)]
    plan = plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    by_id = {item.boundary_id: item for item in plan.candidates}
    assert "boundary_000080000" in by_id  # 발화 0의 종료(5초 틱)
    assert by_id["boundary_000080000"].kind == "utterance_end"
    assert by_id["boundary_000080000"].window_id == "window_000000"
    assert by_id["boundary_000352000"].window_id == "window_000001"  # 22초 발화 종료
    assert "boundary_000240000" in by_id  # 15초 창 길이 경계
    assert by_id["boundary_000240000"].kind == "window_length"
    window0 = plan.windows[0]
    assert set(window0.boundary_ids) == {"boundary_000080000", "boundary_000240000"}
    # 발화가 창 경계를 넘어 이어지면 그 종료 후보는 다음 창이 소유한다
    spanning = [utterance(0, 1.0, 18.0)]
    plan2 = plan_windows(audio, spanning, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    assert set(plan2.windows[0].boundary_ids) == {"boundary_000240000"}
    assert "boundary_000288000" in set(plan2.windows[1].boundary_ids)  # 18초 발화 종료


def test_window_utterances_include_zero_length_evidence(tmp_path: Path) -> None:
    """길이 0 발화도 시작점이 문맥 안이면 증거로 싣는다 — 글자는 버리지 않는다."""
    audio = make_audio(tmp_path, seconds=20.0)
    zero = replace(utterance(0, 4.0, 4.0), utterance_id="utterance_000000")
    plan = plan_windows(audio, [zero], SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    assert "utterance_000000" in plan.windows[0].utterance_ids


# ─────────────────────────────────────────────────────────────
#  프레임 표본 — 주입 추출기, 아티팩트 공유
# ─────────────────────────────────────────────────────────────


class FakeSampler:
    """추출 경계 가짜 — 실측 대신 결정적 바이트를 남긴다."""

    def __init__(self, *, fail: set[int] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[int] = []

    def sample(
        self, *, stream_index: int, frame_index: int, destination: Path, long_edge: int
    ) -> Path:
        self.calls.append(frame_index)
        if frame_index in self.fail:
            raise RuntimeError("프레임을 못 뽑았다")
        destination.write_bytes(f"jpeg:{frame_index}".encode())
        return destination


def test_shared_frame_is_extracted_once(tmp_path: Path) -> None:
    """겹치는 문맥이 같은 프레임을 가리키면 추출·아티팩트는 하나다(§7.4)."""
    audio = make_audio(tmp_path, seconds=2.0)
    video = VideoGrid(0, Fraction(0), Fraction(1, 25), 50)
    plan = plan_windows(audio, [], SETTINGS, video)
    shared_plan = FramePlan("frame_00000018", 18, Fraction(18, 25))
    windows = tuple(replace(window, frame_plans=(shared_plan,)) for window in plan.windows)
    sampler = FakeSampler()
    result = sample_frames(
        replace(plan, windows=windows),
        directory=tmp_path / "frames",
        settings=SETTINGS,
        sampler=sampler,
    )
    assert sampler.calls == [18]  # 두 창이 가리켜도 추출은 한 번
    assert [frame.frame_id for frame in result.frames] == ["frame_00000018"]


def test_sample_frames_extracts_once_per_frame_and_isolates_failures(tmp_path: Path) -> None:
    """같은 프레임은 한 번 뽑고, 한 장 실패가 나머지를 멈추지 않는다."""
    audio = make_audio(tmp_path, seconds=20.0)
    video = VideoGrid(0, Fraction(0), Fraction(1, 25), 1000)
    plan = plan_windows(audio, [], SETTINGS, video)
    sampler = FakeSampler(fail={plan.windows[0].frame_plans[3].frame_index})
    result = sample_frames(plan, directory=tmp_path / "frames", settings=SETTINGS, sampler=sampler)
    assert len(sampler.calls) == len(set(sampler.calls))  # 프레임마다 한 번
    assert result.frames and result.failures
    failed_id = result.failures[0].frame_id
    assert failed_id not in {frame.frame_id for frame in result.frames}
    assert all(frame.image_sha256 for frame in result.frames)
    assert result.frames[0].file_time == Fraction(result.frames[0].frame_index, 25)


def test_sample_frames_skips_entirely_without_a_proven_grid(tmp_path: Path) -> None:
    """증명된 격자가 없으면 추출기는 한 번도 움직이지 않는다."""
    audio = make_audio(tmp_path, seconds=20.0)
    plan = plan_windows(audio, [], SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    sampler = FakeSampler()
    result = sample_frames(plan, directory=tmp_path / "frames", settings=SETTINGS, sampler=sampler)
    assert sampler.calls == []
    assert result == FrameSamplingResult((), ())


# ─────────────────────────────────────────────────────────────
#  분할 제안 — 전체 보존, 안전 경계, 강제 절단 없음
# ─────────────────────────────────────────────────────────────


def verdict(
    window_id: str, *boundary_ids: str, accepted: bool = True, note: str = ""
) -> WindowVerdict:
    return WindowVerdict(
        window_id=window_id,
        accepted=accepted,
        suggested_boundary_ids=tuple(boundary_ids),
        needs_review=False,
        note=note,
    )


def test_accepted_utterance_boundaries_cut_and_extent_is_preserved(tmp_path: Path) -> None:
    """받아들인 발화 종료 경계로 자르고 전체 범위는 그대로다(§7.6)."""
    audio = make_audio(tmp_path, seconds=40.0)
    utterances = [utterance(0, 1.0, 5.0), utterance(1, 16.0, 22.0)]
    plan = plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    verdicts = {
        "window_000000": verdict("window_000000", "boundary_000080000"),
        "window_000001": verdict("window_000001", "boundary_000352000"),
    }
    proposal = propose_segmentation(plan, verdicts, utterances)
    assert [(seg.start, seg.end) for seg in proposal.segments] == [
        (Fraction(0), Fraction(5)),
        (Fraction(5), Fraction(22)),
        (Fraction(22), Fraction(40)),
    ]
    assert [seg.speech_status for seg in proposal.segments] == [
        stage1.SPEECH_DETECTED,
        stage1.SPEECH_DETECTED,
        stage1.SPEECH_UNCONFIRMED_EMPTY,  # 창 2 판정 없음 — 미확인 빈 골격 + 검토
    ]
    assert [seg.needs_review for seg in proposal.segments] == [False, False, True]
    assert proposal.dropped_boundary_ids == ()


def test_boundary_inside_an_utterance_is_dropped_not_moved(tmp_path: Path) -> None:
    """발화 중간 경계는 옮기지 않고 버린다 — 조각이 길어질 뿐이다."""
    audio = make_audio(tmp_path, seconds=40.0)
    utterances = [utterance(0, 1.0, 18.0)]  # 15초 창 경계가 발화 중간이다
    plan = plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    inside = next(item for item in plan.candidates if item.position == Fraction(15))
    assert inside.kind == "window_length"
    proposal = propose_segmentation(
        plan, {"window_000000": verdict("window_000000", inside.boundary_id)}, utterances
    )
    assert proposal.dropped_boundary_ids == (inside.boundary_id,)
    assert len(proposal.segments) == 1  # 긴 조각 하나로 보존
    assert proposal.segments[0].needs_review
    assert any("발화 중간" in reason for reason in proposal.review_reasons)


def test_failed_window_keeps_one_long_segment_with_review(tmp_path: Path):
    """판정 없음·거부·실패 창은 경계를 만들지 않는다 — 보수적 유지 + 검토."""
    audio = make_audio(tmp_path, seconds=45.0)
    utterances = [utterance(0, 1.0, 5.0)]
    plan = plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    verdicts = {
        "window_000000": WindowVerdict("window_000000", False, (), True, "timeout"),
        # window_000001·window_000002: 판정 없음
    }
    proposal = propose_segmentation(plan, verdicts, utterances)
    assert len(proposal.segments) == 1
    assert proposal.segments[0].needs_review
    assert any("판정 없음" in reason for reason in proposal.review_reasons)
    assert any("timeout" in reason for reason in proposal.review_reasons)


def test_no_forced_cut_without_accepted_verdicts(tmp_path: Path) -> None:
    """어떤 판정도 없으면 창 수와 무관하게 조각 하나 — 목표 길이 강제 절단은 없다."""
    audio = make_audio(tmp_path, seconds=90.0)
    plan = plan_windows(audio, [], SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    assert len(plan.windows) == 6
    proposal = propose_segmentation(plan, {}, [])
    assert len(proposal.segments) == 1
    assert proposal.segments[0].speech_status == stage1.SPEECH_UNCONFIRMED_EMPTY


def test_foreign_boundary_suggestion_is_rejected(tmp_path: Path) -> None:
    """다른 창의 경계·모르는 아이디는 거부된다 — 제공된 세계 밖은 없다."""
    audio = make_audio(tmp_path, seconds=40.0)
    utterances = [utterance(0, 1.0, 5.0), utterance(1, 16.0, 22.0)]
    plan = plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    verdicts = {
        "window_000000": verdict("window_000000", "boundary_000352000"),  # 창 1의 후보
        "window_000001": verdict("window_000001", "boundary_999999999"),  # 없는 아이디
    }
    proposal = propose_segmentation(plan, verdicts, utterances)
    assert set(proposal.dropped_boundary_ids) == {
        "boundary_000352000",
        "boundary_999999999",
    }
    assert len(proposal.segments) == 1


def test_speech_statuses_are_distinct(tmp_path: Path) -> None:
    """발화 있음·미확인 빈 골격·분석 실패는 각각 다른 상태다(§7.3)."""
    audio = make_audio(tmp_path, seconds=45.0)
    utterances = [utterance(0, 1.0, 5.0), utterance(1, 16.0, 22.0)]
    plan = plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    verdicts = {
        "window_000000": verdict("window_000000", "boundary_000080000"),
        "window_000001": verdict("window_000001", "boundary_000352000"),
    }
    failed = AsrChunkOutcome(2, 640000, 720000, "c" * 64, "run:chunk-0002", (), "timeout")
    proposal = propose_segmentation(plan, verdicts, utterances, asr_failures=[failed])
    statuses = [seg.speech_status for seg in proposal.segments]
    assert statuses == [
        stage1.SPEECH_DETECTED,
        stage1.SPEECH_DETECTED,
        stage1.SPEECH_ANALYSIS_FAILED,
    ]
    assert proposal.segments[2].needs_review


def test_audio_absent_keeps_whole_video_with_distinct_state() -> None:
    """오디오 부재는 무발화와 다른 상태로 전체를 보존한다."""
    video = VideoGrid(0, Fraction(0), Fraction(1, 25), 500)
    proposal = audio_absent_proposal(video, reason="오디오 트랙 없음")
    assert proposal.tick_grid == stage1.TICK_GRID_VIDEO
    assert len(proposal.segments) == 1
    segment = proposal.segments[0]
    assert segment.speech_status == stage1.SPEECH_AUDIO_ABSENT
    assert (segment.start, segment.end) == (Fraction(0), Fraction(20))
    assert segment.needs_review
    with pytest.raises(AnalysisPlanningError):
        audio_absent_proposal(VideoGrid(0, Fraction(0), Fraction(1, 25), 0), reason="깨짐")


# ─────────────────────────────────────────────────────────────
#  직렬화 — analysis_transcript.json (SRT가 아니다)
# ─────────────────────────────────────────────────────────────


def test_stage1_document_is_json_only_with_exact_fractions(tmp_path: Path) -> None:
    """문서는 JSON이다 — 유리수는 분자·분모, SRT는 어디에도 없다(§7.1·§13.3)."""
    audio = make_audio(tmp_path, seconds=20.0)
    evidence = AsrEvidence(
        request_id="run-5",
        sample_rate=stage1.PCM_SAMPLE_RATE,
        chunks=(outcome(0, SampleSpan(0, 320000), [{"start": 0.5, "end": 2.0, "text": "안녕"}]),),
    )
    utterances = utterance_evidence(audio, evidence)
    plan = plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    verdicts = {"window_000000": verdict("window_000000")}
    proposal = propose_segmentation(plan, verdicts, utterances)
    frames = FrameSamplingResult((), ())
    document = stage1_evidence_document(
        audio=audio,
        asr=evidence,
        utterances=utterances,
        plan=plan,
        frames=frames,
        segmentation=proposal,
        annotations=[{"window_id": "window_000000", "status": "accepted"}],
        models={"asr": "m", "luna": "l"},
        prompt_revision="raw-understanding-v1",
        fingerprint="f" * 64,
        execution_mode="offline_replay",
    )
    path = write_stage1_document(tmp_path / "analysis_transcript.json", document)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["audio"]["pcm_sample0_file_time"] == {"num": -1, "den": 50}
    assert body["execution_mode"] == "offline_replay"
    assert body["utterances"][0]["start"] == {"num": 1, "den": 2}
    dumped = json.dumps(body)
    assert ".srt" not in dumped and " --> " not in dumped  # SRT 흔적은 없다


def test_stage1_document_rejects_unknown_execution_modes(tmp_path: Path) -> None:
    """실행 모드는 replay·live 둘뿐 — 다른 값은 기록 자체가 계약 위반이다."""
    audio = make_audio(tmp_path, seconds=1.0)
    evidence = AsrEvidence(request_id="r", sample_rate=stage1.PCM_SAMPLE_RATE, chunks=())
    plan = plan_windows(audio, [], SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    proposal = propose_segmentation(plan, {}, [])
    with pytest.raises(AnalysisPlanningError, match="실행 모드"):
        stage1_evidence_document(
            audio=audio,
            asr=evidence,
            utterances=[],
            plan=plan,
            frames=FrameSamplingResult((), ()),
            segmentation=proposal,
            annotations=[],
            models={},
            prompt_revision="raw-understanding-v1",
            fingerprint="f" * 64,
            execution_mode="live_paid",
        )


def test_segmentation_never_leaves_gaps_or_overlaps(tmp_path: Path) -> None:
    """불변 검사 — 빈틈·겹침·뒤집힘 있는 제안은 만들어질 수 없다."""
    broken = [
        SegmentProposal(
            segment_id="segment_000000000",
            ordinal=0,
            start=Fraction(0),
            end=Fraction(10),
            start_tick=0,
            end_tick=160000,
            speech_status=stage1.SPEECH_UNCONFIRMED_EMPTY,
            needs_review=False,
            basis=(),
            review_reasons=(),
        )
    ]
    with pytest.raises(AnalysisPlanningError, match="전체를 덮지 않는다"):
        stage1._require_segment_coverage(broken, Fraction(20))
