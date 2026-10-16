"""A4 공통 파일 시간축의 순수 계산 — 비디오 전체와 선택 오디오를 함께 덮는다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7.4(원본 전체를 시간창으로)·
§13.3(정확 유리수). ffmpeg도 DB도 없다: 측정된 시계 값만 넣어 공통 시간축의
범위·환산·창 계획·경계 조정 규칙을 잰다. 오디오가 없으면 비디오만의 시간축이
되고 빈 PCM을 지어내지 않는다(§7.3).
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.understanding import (
    AnalysisPlanningError,
    FramePlan,
    SelectedAudio,
    UtteranceEvidence,
    VideoGrid,
    common_timeline,
    plan_common_windows,
    utterances_in_common_clock,
)

TICKS_PER_FRAME = 3600


def _grid(
    frame_count: int, fps: Fraction = Fraction(25), origin: Fraction = Fraction(0)
) -> VideoGrid:
    return VideoGrid(
        stream_index=0,
        presentation_origin=origin,
        frame_duration=Fraction(1) / fps,
        frame_count=frame_count,
    )


def _audio(
    *,
    seconds: Fraction,
    origin: Fraction = Fraction(0),
    channels: int = 2,
    path=None,
) -> SelectedAudio:
    from pathlib import Path

    pcm = path if path is not None else Path("/tmp/unused-analysis.pcm")
    return SelectedAudio(
        source_id="source_a",
        source_path=Path("/tmp/unused-source.mp4"),
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=pcm,
        pcm_sha256="b" * 64,
        sample_rate=16000,
        channels=channels,
        sample_count=int(seconds * 16000),
        pcm_sample0_file_time=origin,
    )


def _utterance(
    ordinal: int, start: Fraction, end: Fraction, *, chunk: int = 0
) -> UtteranceEvidence:
    rate = 16000
    return UtteranceEvidence(
        utterance_id=f"utterance_{ordinal:06d}",
        chunk_index=chunk,
        position=ordinal,
        text=f"발화 {ordinal}",
        start=start,
        end=end,
        start_sample=int(start * rate),
        end_sample=int(end * rate),
        evidence_id=f"run:0:{ordinal}",
    )


# ── 공통 시간축 — 범위와 환산 ────────────────────────────────────────────────


def test_timeline_covers_silent_video_head_and_tail_beyond_audio() -> None:
    """오디오가 짧아도 비디오 머리·꼬리는 창 밖에 남지 않는다(§7.4)."""
    video = _grid(150)  # 6초 25fps
    audio = _audio(seconds=Fraction("3"), origin=Fraction("1.5"))  # 1.5~4.5초
    timeline = common_timeline(source_id="source_a", video=video, audio=audio)
    assert timeline.extent == Fraction(6)  # 비디오 전체
    assert timeline.audio_span() == (Fraction("1.5"), Fraction("4.5"))
    assert timeline.audio_time(Fraction(0)) is None  # 조용한 머리 — 오디오 시각 없음
    assert timeline.audio_time(Fraction("2")) == Fraction("0.5")
    assert timeline.audio_time(Fraction(6)) is None  # 끝은 반개구간 밖


def test_timeline_extends_to_audio_tail_beyond_last_frame() -> None:
    """오디오 꼬리가 마지막 프레임보다 길면 증거는 그 끝까지 덮는다(§7.3)."""
    video = _grid(150)  # 6초
    audio = _audio(seconds=Fraction("7.5"))
    timeline = common_timeline(source_id="source_a", video=video, audio=audio)
    assert timeline.extent == Fraction("7.5")
    assert timeline.frame_index(Fraction("5.9")) == 147
    assert timeline.frame_index(Fraction("6.0")) is None  # 마지막 프레임 뒤는 격자 밖


def test_timeline_without_audio_is_video_only_no_fake_pcm() -> None:
    """오디오 트랙 부재 — 비디오만의 시간축, 빈 샘플을 지어내지 않는다(§7.3)."""
    video = _grid(100, fps=Fraction(30000, 1001))
    timeline = common_timeline(source_id="source_a", video=video, audio=None)
    assert timeline.audio is None
    assert timeline.audio_span() is None
    assert timeline.audio_time(Fraction(1)) is None
    assert timeline.extent == video.extent


def test_timeline_requires_a_proven_grid() -> None:
    broken = VideoGrid(
        stream_index=0, presentation_origin=Fraction(0), frame_duration=Fraction(0), frame_count=0
    )
    with pytest.raises(AnalysisPlanningError, match="증명된 상태가 아니다"):
        common_timeline(source_id="source_a", video=broken, audio=None)


def test_utterances_shift_once_into_the_common_clock() -> None:
    """발화 증거의 공통 시계 이동 — 원점 차이는 정확히 한 번만 적용된다(§13.3)."""
    video = _grid(150)
    audio = _audio(seconds=Fraction(4), origin=Fraction("0.5"))
    timeline = common_timeline(source_id="source_a", video=video, audio=audio)
    spoken = utterances_in_common_clock(timeline, [_utterance(0, Fraction("0.5"), Fraction("1"))])
    assert spoken[0].start == Fraction(1)  # 0.5 + 0.5 — 이동이 전부다
    assert spoken[0].start_sample == int(Fraction("0.5") * 16000)  # 틱은 오디오 격자 그대로


def test_utterances_without_audio_are_a_defect_not_dropped() -> None:
    video = _grid(50)
    timeline = common_timeline(source_id="source_a", video=video, audio=None)
    with pytest.raises(AnalysisPlanningError, match="오디오 트랙이 없는데"):
        utterances_in_common_clock(timeline, [_utterance(0, Fraction(0), Fraction(1))])


# ── 공통 시간창 — 전체 덮음·프레임·경계 후보 ─────────────────────────────────


def test_common_windows_cover_the_whole_common_extent() -> None:
    """창 코어는 공통 범위 전체를 빈틈없이 덮는다 — 오디오보다 긴 꼬리도(§7.4)."""
    video = _grid(375)  # 15초
    audio = _audio(seconds=Fraction("20"))  # 영상보다 긴 오디오
    timeline = common_timeline(source_id="source_a", video=video, audio=audio)
    plan = plan_common_windows(
        timeline, [_utterance(0, Fraction(0), Fraction(1))], RawUnderstandingSettings()
    )
    assert plan.extent == Fraction(20)
    cursor = Fraction(0)
    for window in plan.windows:
        assert window.core_start == cursor
        assert window.core_end > window.core_start
        cursor = window.core_end
    assert cursor == Fraction(20)
    assert plan.windows[0].context_start == Fraction(0)  # 문맥은 범위 안으로 클램프


def test_common_windows_without_audio_have_frames_and_length_boundaries_only() -> None:
    """오디오 없음 — 프레임 표본은 실제 격자 번호, 경계는 길이 후보뿐(ASR 없음)."""
    video = _grid(750)  # 30초 → 15초 창 2개
    timeline = common_timeline(source_id="source_a", video=video, audio=None)
    plan = plan_common_windows(timeline, (), RawUnderstandingSettings())
    assert len(plan.windows) == 2
    for window in plan.windows:
        assert window.utterance_ids == ()
        assert window.frame_plans  # 비디오 격자의 실제 프레임
        for item in window.frame_plans:
            assert isinstance(item, FramePlan)
            assert 0 <= item.frame_index < video.frame_count
    kinds = {candidate.kind for candidate in plan.candidates}
    assert kinds == {"window_length"}
    # 마지막 창의 끝(전체 끝)은 경계가 못 된다 — 자르는 경계가 아니다.
    for candidate in plan.candidates:
        assert 0 < candidate.tick < video.frame_count


def test_common_window_boundaries_follow_utterance_ends_in_common_clock() -> None:
    """발화 종료 후보 — 오디오 시계→공통 시계 이동 뒤 창 코어 안에 배정된다."""
    video = _grid(750)
    audio = _audio(seconds=Fraction(30), origin=Fraction(2))
    timeline = common_timeline(source_id="source_a", video=video, audio=audio)
    utterances = [
        _utterance(0, Fraction(1), Fraction("2.5")),  # 공통 시계 3~4.5초
        _utterance(1, Fraction(10), Fraction("14")),  # 공통 시계 12~16초(창 0 코어 안 종료)
    ]
    plan = plan_common_windows(timeline, utterances, RawUnderstandingSettings())
    by_kind = {}
    for candidate in plan.candidates:
        by_kind.setdefault(candidate.kind, []).append(candidate)
    ends = by_kind.get("utterance_end", [])
    assert sorted(candidate.tick for candidate in ends) == [
        113,  # 공통 4.5초 → 올림 프레임 ceil(4.5×25) — 발화를 앞 조각에 온전히
        400,  # 공통 16초 → 창 1(코어 15~20초)이 소유한다
    ]
    assert all(candidate.position > Fraction(2) for candidate in ends)  # 공통 시계 좌표


def test_common_window_ids_are_deterministic_and_unique() -> None:
    video = _grid(400)
    audio = _audio(seconds=Fraction(16))
    timeline = common_timeline(source_id="source_a", video=video, audio=audio)
    first = plan_common_windows(timeline, (), RawUnderstandingSettings())
    second = plan_common_windows(timeline, (), RawUnderstandingSettings())
    assert [w.window_id for w in first.windows] == [w.window_id for w in second.windows]
    ids = [c.boundary_id for c in first.candidates]
    assert len(set(ids)) == len(ids)
    # 같은 프레임에 후보가 겹쳐도 아이디는 갈라 진다 — 접는 것은 조정 계층이다.
    overlapping = plan_common_windows(
        timeline,
        [
            _utterance(0, Fraction(0), Fraction("14.93")),
            _utterance(1, Fraction("14.93"), Fraction("14.96")),
        ],
        RawUnderstandingSettings(),
    )
    end_ids = [c.boundary_id for c in overlapping.candidates if c.kind == "utterance_end"]
    end_frames = [c.tick for c in overlapping.candidates if c.kind == "utterance_end"]
    assert end_frames == [374, 374]  # 두 종료의 올림이 같은 프레임에 떨어진다
    assert len(set(end_ids)) == len(end_ids)  # 아이디는 여전히 유일하다
