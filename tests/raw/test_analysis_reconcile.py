"""A4 경계 조정의 순수 계산 — 받아들인 경계를 하나의 비디오 격자로 확정한다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7.6(분할 알고리즘)·§8.2(오디오
꼬리는 마지막 조각). 발화 중간 경계는 옮기지 않고 버리고, 격자 스냅이 발화 안에
경계를 놓으면 그것도 버린다 — 버리면 조각이 길어질 뿐 전체 비디오 보존은
언제나 이긴다. 판정이 없는 창은 자르지 않는다(강제 절단 없음).
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_workflow import _cut_frame, reconcile_to_video_lattice
from vlogsrt.project.raw.understanding import (
    SPEECH_ANALYSIS_FAILED,
    SPEECH_AUDIO_ABSENT,
    SPEECH_DETECTED,
    SPEECH_UNCONFIRMED_EMPTY,
    AsrChunkOutcome,
    BoundaryCandidate,
    SelectedAudio,
    UtteranceEvidence,
    VideoGrid,
    WindowVerdict,
    common_timeline,
    plan_common_windows,
)

FPS = Fraction(25)
SETTINGS = RawUnderstandingSettings()


def _grid(frame_count: int) -> VideoGrid:
    return VideoGrid(
        stream_index=0,
        presentation_origin=Fraction(0),
        frame_duration=Fraction(1) / FPS,
        frame_count=frame_count,
    )


def _utterance(
    ordinal: int, start: Fraction, end: Fraction, *, text: str = "말"
) -> UtteranceEvidence:
    rate = 16000
    return UtteranceEvidence(
        utterance_id=f"utterance_{ordinal:06d}",
        chunk_index=0,
        position=ordinal,
        text=text,
        start=start,
        end=end,
        start_sample=int(start * rate),
        end_sample=int(end * rate),
        evidence_id=f"run:0:{ordinal}",
    )


def _timeline(frame_count: int = 400):
    """공통 시계 테스트용 시간축 — 오디오 원점이 비디오 원점과 같아 이동이 0이다.

    발화 좌표가 오디오 시계 == 공통 시계가 되므로 같은 목록을 창 계획과 조정에
    그대로 넘길 수 있다(원점 차이는 없다, §13.3).
    """
    grid = _grid(frame_count)
    audio = _audio_aligned(grid)
    return common_timeline(source_id="source_a", video=grid, audio=audio), grid


def _audio_aligned(grid: VideoGrid) -> SelectedAudio:
    seconds = grid.frame_count * grid.frame_duration
    return SelectedAudio(
        source_id="source_a",
        source_path=Path("/tmp/unused-source.mp4"),
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=Path("/tmp/unused-analysis.pcm"),
        pcm_sha256="b" * 64,
        sample_rate=16000,
        channels=1,
        sample_count=int(seconds * 16000),
        pcm_sample0_file_time=grid.presentation_origin,  # 공통 시계 이동 = 0
    )


def _verdicts(plan, *values: tuple[bool, tuple[str, ...], bool]) -> dict[str, WindowVerdict]:
    """창 순서 → (accepted, 제안 경계 ID, needs_review) 판정 지도."""
    return {
        window.window_id: WindowVerdict(
            window_id=window.window_id,
            accepted=accepted,
            suggested_boundary_ids=ids,
            needs_review=review,
        )
        for window, (accepted, ids, review) in zip(plan.windows, values, strict=True)
    }


def _failure(chunk_index: int, start: Fraction, end: Fraction) -> AsrChunkOutcome:
    rate = 16000
    return AsrChunkOutcome(
        chunk_index=chunk_index,
        sample_start=int(start * rate),
        sample_end=int(end * rate),
        wav_sha256="c" * 64,
        request_id=f"run:chunk-{chunk_index:04d}",
        transcript=(),
        failure="주입된 실패",
    )


def _segments_of(result) -> list[tuple[Fraction, Fraction, str]]:
    return [(s.start, s.end, s.speech_status) for s in result.proposal.segments]


def test_accepted_utterance_end_cuts_on_the_video_lattice() -> None:
    """받아들인 발화 종료 — 프레임 경계에서 자르고 전체를 정확히 덮는다(§7.6)."""
    timeline, _ = _timeline(400)  # 16초
    spoken = [_utterance(0, Fraction("3"), Fraction("8"))]  # 3~8초 발화
    plan = plan_common_windows(timeline, spoken, SETTINGS)
    boundary = next(c for c in plan.candidates if c.kind == "utterance_end" and c.tick == 200)
    result = reconcile_to_video_lattice(
        timeline,
        plan,
        _verdicts(plan, (True, (boundary.boundary_id,), False), (False, (), False)),
        spoken,
    )
    assert _segments_of(result) == [
        (Fraction(0), Fraction(8), SPEECH_DETECTED),
        (Fraction(8), Fraction(16), SPEECH_UNCONFIRMED_EMPTY),
    ]
    assert result.proposal.tick_grid == "video_frame"
    assert result.proposal.segments[0].end_tick == 200
    assert result.intervals[0].end == Fraction(8)


def test_boundary_inside_another_utterance_is_dropped_not_moved() -> None:
    """짧은 발화의 종료가 긴 발화 안에 있으면 그 후보는 버린다(§7.6)."""
    timeline, _ = _timeline(400)
    spoken = [
        _utterance(0, Fraction("3"), Fraction("12")),  # 긴 발화
        _utterance(1, Fraction("4"), Fraction("4.5")),  # 종료 4.5초는 긴 발화 안이다
    ]
    plan = plan_common_windows(timeline, spoken, SETTINGS)
    nested = next(
        c for c in plan.candidates if c.kind == "utterance_end" and c.position == Fraction("4.5")
    )
    long_end = next(
        c for c in plan.candidates if c.kind == "utterance_end" and c.position == Fraction(12)
    )
    result = reconcile_to_video_lattice(
        timeline,
        plan,
        _verdicts(
            plan,
            (True, (nested.boundary_id, long_end.boundary_id), False),
            (False, (), False),
        ),
        spoken,
    )
    assert nested.boundary_id in result.dropped_boundary_ids
    assert any("발화 중간" in r for r in result.review_reasons)
    # 긴 발화의 종료만 살아 자른다 — 버린 경계는 조각을 늘렸을 뿐이다.
    assert [s.end for s in result.proposal.segments] == [Fraction(12), Fraction(16)]


def test_snap_that_lands_inside_an_utterance_is_dropped() -> None:
    """격자 스냅이 발화 안에 경계를 놓으면 버린다 — 스냅이 컷을 만들지 못한다."""
    timeline, _ = _timeline(400)
    # 다음 발화 [5.05, 5.3)초가 프레임 127(5.08초)을 품고 있다. 앞 발화가
    # 5.045초에 끝나면 후보 위치는 다음 발화 밖이지만 올림 스냅은 5.08초가
    # 되어 다음 발화의 열린 구간 안이다 — 버려야 한다.
    spoken = [_utterance(0, Fraction("5.05"), Fraction("5.3"))]
    candidate = BoundaryCandidate(
        boundary_id="boundary_probe",
        window_id="window_000000",
        kind="utterance_end",
        position=Fraction("5.045"),
        tick=127,
    )
    frame, note = _cut_frame(timeline, candidate, spoken)
    assert frame is None
    assert note == "격자 스냅이 발화 중간에 놓는다 — 버리고 긴 조각 유지"


def test_missing_verdict_keeps_one_long_segment_with_review() -> None:
    """판정 없는 창은 자르지 않는다 — 보존이 이기고 검토 표시만 남는다(§7.6)."""
    timeline, _ = _timeline(200)  # 8초 → 창 1개
    plan = plan_common_windows(timeline, (), SETTINGS)
    result = reconcile_to_video_lattice(timeline, plan, {}, ())
    assert _segments_of(result) == [(Fraction(0), Fraction(8), SPEECH_UNCONFIRMED_EMPTY)]
    assert result.proposal.segments[0].needs_review
    assert any("판정 없음" in reason for reason in result.review_reasons)


def test_failed_asr_chunk_marks_segment_analysis_failed_not_empty() -> None:
    """청크 실패는 그 범위를 analysis_failed로 남긴다 — 무발화가 아니다(§7.3)."""
    timeline, _ = _timeline(200)
    spoken = [_utterance(0, Fraction(0), Fraction("3"))]
    plan = plan_common_windows(timeline, (), SETTINGS)
    result = reconcile_to_video_lattice(
        timeline,
        plan,
        _verdicts(plan, (False, (), False)),
        spoken,
        [_failure(0, Fraction("4"), Fraction("8"))],
    )
    assert _segments_of(result) == [(Fraction(0), Fraction(8), SPEECH_ANALYSIS_FAILED)]
    assert SPEECH_AUDIO_ABSENT not in {s.speech_status for s in result.proposal.segments}


def test_foreign_boundary_suggestion_is_rejected() -> None:
    """다른 창의 경계 아이디는 거부된다 — 창은 자기 후보만 제안받는다(§7.5)."""
    timeline, _ = _timeline(800)  # 32초 → 창 3개
    plan = plan_common_windows(timeline, (), SETTINGS)
    foreign = next(c for c in plan.candidates if c.window_id == "window_000001")
    result = reconcile_to_video_lattice(
        timeline,
        plan,
        {
            "window_000000": WindowVerdict(
                window_id="window_000000",
                accepted=True,
                suggested_boundary_ids=(foreign.boundary_id,),
                needs_review=False,
            )
        },
        (),
    )
    assert result.dropped_boundary_ids == (foreign.boundary_id,)
    assert any("제공되지 않은 경계" in reason for reason in result.review_reasons)
    assert _segments_of(result) == [(Fraction(0), Fraction(32), SPEECH_UNCONFIRMED_EMPTY)]


def test_window_length_cut_is_a_valid_basis_not_forced() -> None:
    """받아들인 길이 경계는 자른다(단순 길이 분할) — 판정 없음과는 다르다(§7.1)."""
    timeline, _ = _timeline(400)
    plan = plan_common_windows(timeline, (), SETTINGS)
    length = next(
        c for c in plan.candidates if c.kind == "window_length" and c.window_id == "window_000000"
    )
    result = reconcile_to_video_lattice(
        timeline,
        plan,
        _verdicts(plan, (True, (length.boundary_id,), False), (False, (), False)),
        (),
    )
    assert _segments_of(result) == [
        (Fraction(0), Fraction(15), SPEECH_UNCONFIRMED_EMPTY),
        (Fraction(15), Fraction(16), SPEECH_UNCONFIRMED_EMPTY),
    ]


def test_two_cuts_collapsing_to_one_frame_dedupe_without_gap() -> None:
    """같은 프레임으로 접히는 두 후보 — 하나로 접고 덮음은 그대로다."""
    timeline, _ = _timeline(400)
    spoken = [
        _utterance(0, Fraction("2"), Fraction("4.925")),
        _utterance(1, Fraction("4.925"), Fraction("4.96")),  # 이어지는 발화 — 겹침 없음
    ]
    plan = plan_common_windows(timeline, spoken, SETTINGS)
    ends = [c for c in plan.candidates if c.kind == "utterance_end"]
    assert [c.tick for c in ends] == [124, 124]  # 두 후보가 같은 프레임에 떨어진다
    result = reconcile_to_video_lattice(
        timeline,
        plan,
        _verdicts(plan, (True, tuple(c.boundary_id for c in ends), False), (False, (), False)),
        spoken,
    )
    assert len(result.proposal.segments) == 2  # 접힌 경계 하나 — 빈 조각이 없다
    assert result.proposal.segments[0].end == Fraction("4.96")  # 접힌 자리 = 4.96초
    assert any("접힌다" in reason for reason in result.review_reasons)
