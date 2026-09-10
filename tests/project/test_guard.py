"""보수적 말 보호 — 소스 가드 합집합과 인스턴스 투영의 계약 (P5-03).

계획("VAD guard")의 정본:

- **합집합만이 말을 지킨다.** Whisper 세그먼트(초안·골격)·소스 큐·성공 경계 큐·
  VAD 창(`vad_positive` 이상)·재전사 범위·글자 없는 덮개(불확실)를 전부 모아
  바깥으로 `guard_padding_seconds`만큼 부풀리고 저장 프레임에 묶는다. 짧아서
  버리는 최소 길이는 **없다** — 250ms 미만도, 그 밖의 어떤 최소값도.
- **빈 전사는 침묵이 아니다.** VAD·의존·런타임·덮개가 error거나 없으면 소스
  전체를 보호하고 `keep_all`로 검토를 남긴다. VAD 말이 있는데 쓸 글자가 없는
  구간은 보호되고 `vad_without_text`·검토에 남는다.
- **시계 변환은 정확히 한 번.** 보호 프레임 끝점은 `Fraction(샘플, 주파수) +
  참조 원점`으로 파일 시계에 딱 한 번 옮겨 슬라이스 구간과 교집합하고,
  `interval_outward(파일 시작, 파일 끝, 프레임 길이, 0)`로 소스 격자에 딱 맞춘다.
  원점을 빼먹거나 두 번 더하면 프레임이 통째로 밀린다.
- **인스턴스마다 따로 닻을 내린다.** 같은 소스가 반복되어도 각 슬라이스의 파일
  구간·에셋 기원에서 각자 격자를 받는다. 불가청(음소거·비활성·영상 전용) 인스턴스는
  남되 제거 권한이 없어 슬라이스 전체를 보호한다. 선언된 무음·pre-PCM 부재 자원은
  참조·VAD 호출·가드·투영 자체가 없다 — 같은 내용의 건강한 자원 참조를 빌리지
  않는다(P4의 타입 있는 부재 기록이 정본이다).

손 참조·손 트레이스·손 슬라이스만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

from dataclasses import fields, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.models import Cue, Repair, Repeat
from vlogsrt.project.boundary import (
    BOUNDARY_UNRESOLVED,
    BoundaryProjection,
    LogicalSlice,
    ProjectCue,
    UnavailableInstance,
)
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.pcm import PcmReference, SampleRange
from vlogsrt.project.speech import SourceSegment, SourceTrace, UnavailableSourceAudio
from vlogsrt.project.timebase import FrameInterval, interval_outward
from vlogsrt.project.vad.guard import (
    GUARD_MIN_RUN_SECONDS,
    InstanceGuard,
    SourceGuard,
    build_source_guard,
    project_guard,
)
from vlogsrt.project.vad.inference import VadResult, VadWindow
from vlogsrt.transcribe import ChunkCoverage

CFG = ProjectConfig()
RATE = CFG.vad_sample_rate
"""계약 주파수 — 손 참조가 주장하는 저장 PCM의 사실이다."""

PAD = CFG.guard_padding_seconds
"""바깥 여유 — 결합 사양의 0.5초가 설정에 산다(함수 몸통의 리터럴이 아니다)."""

TOTAL = 160_000
"""기본 손 참조의 프레임 수 — 10초."""


def _samples(seconds: str) -> int:
    """기대 프레임 수 — 초를 정확한 Fraction으로 곱해 float 함정을 피한다."""
    return int(Fraction(seconds) * RATE)


# ─────────────────────────────────────────────────────────────
#  재료 — 손 참조·손 트레이스·손 VAD·손 투영·손 슬라이스
# ─────────────────────────────────────────────────────────────


def _reference(
    *,
    source_id: str = "asset-1",
    origin: Fraction = Fraction(0),
    sample_count: int = TOTAL,
    sample_rate: int = RATE,
    key: str | None = None,
) -> PcmReference:
    return PcmReference(
        source_id=source_id,
        original_stem="demo",
        original_sha256="a" * 64,
        path=Path("/cache/demo.pcm"),
        pcm_sha256="b" * 64,
        analysis_key=key or f"pcm-{source_id}",
        pcm_sample0_file_time=origin,
        sample_rate=sample_rate,
        channels=1,
        sample_count=sample_count,
    )


def _segment(source_id: str, start: float, end: float, position: int = 0) -> SourceSegment:
    audio_start = Fraction(str(start))
    audio_end = Fraction(str(end))
    return SourceSegment(
        segment_id=f"{source_id}-seg-{position}",
        audio_start=audio_start,
        audio_end=audio_end,
        file_start=audio_start,
        file_end=audio_end,
        text=f"세그먼트 {position}",
        compression_ratio=1.0,
        avg_logprob=-0.1,
        no_speech_prob=0.0,
    )


def _trace(
    source_id: str = "asset-1",
    *,
    cues: tuple[tuple[float, float], ...] = ((1.0, 2.0),),
    draft: tuple[SourceSegment, ...] = (),
    spine: tuple[SourceSegment, ...] | None = None,
    repairs: tuple[Repair, ...] = (),
    coverage: tuple[ChunkCoverage, ...] | None = None,
    status: str = "success",
    key: str | None = None,
    origin: Fraction = Fraction(0),
) -> SourceTrace:
    """손 트레이스 — 큐·세그먼트 시각을 오디오 국소 초로 직접 준다(원점 0 기본)."""
    if spine is None:
        spine = (_segment(source_id, 1.0, 2.0, 0),)
    published = status in ("success", "empty")
    return SourceTrace(
        variant="source_trace",
        source_id=source_id,
        original_stem="demo",
        analysis_key=f"analysis-{source_id}",
        pcm_analysis_key=key or f"pcm-{source_id}",
        pcm_sample0_file_time=origin,
        status=status,  # type: ignore[arg-type]
        draft=draft,
        settled_spine=spine,
        repairs=repairs,
        cues=tuple(
            Cue(start=start, end=end, lines=("자막",), seg_ids=(position,), index=position)
            for position, (start, end) in enumerate(cues, start=1)
        ),
        coverage=(
            (ChunkCoverage(index=0, offset=0.0, duration=10.0, segment_count=1),)
            if coverage is None
            else coverage
        ),
        model="fixture-model",
        config_sha256="c" * 64,
        source_srt_relative_path=f"work/{source_id}/demo.SOURCE.srt" if published else None,
        source_srt_sha256="d" * 64 if published else None,
        error="준비된 실패" if status == "error" else None,
    )


def _window(start: int, end: int, probability: float, channel: int = 0) -> VadWindow:
    return VadWindow(channel=channel, start_sample=start, end_sample=end, probability=probability)


def _vad(
    *,
    windows: tuple[VadWindow, ...] | None = None,
    status: str = "success",
    error: str | None = None,
    source_id: str = "asset-1",
    analysis_key: str = "pcm-asset-1",
) -> VadResult:
    if windows is None:
        windows = (_window(0, TOTAL, 0.1),)
    return VadResult(
        source_id=source_id,
        analysis_key=analysis_key,
        status=status,  # type: ignore[arg-type]
        windows=windows,
        error=error,
    )


def _pcue(
    source_id: str,
    file_start: Fraction,
    file_end: Fraction,
    *,
    provenance: str = "boundary",
    flags: tuple[str, ...] = (),
) -> ProjectCue:
    return ProjectCue(
        cue_id=f"cue-{source_id}-{file_start}/{file_end}",
        slice_id="slice-x",
        instance_ids=("clip-x",),
        source_id=source_id,
        source_start=file_start,
        source_end=file_end,
        timeline_start=file_start,
        timeline_end=file_end,
        text="경계 큐",
        provenance=provenance,  # type: ignore[arg-type]
        flags=flags,
    )


def _projection(
    *,
    cues: tuple[ProjectCue, ...] = (),
    unavailable: tuple[UnavailableInstance, ...] = (),
    locked: frozenset[str] = frozenset(),
) -> BoundaryProjection:
    return BoundaryProjection(
        input_sha256="e" * 64,
        cues=cues,
        source_srt="",
        locked_instance_ids=locked,
        unavailable_instances=unavailable,
        unresolved_request_ids=(),
        unresolved_cue_ids=(),
    )


def _slice(
    source_id: str,
    file_start: Fraction,
    file_end: Fraction,
    *instance_ids: str,
    audible: bool = True,
    asset_start: Fraction = Fraction(0),
) -> LogicalSlice:
    return LogicalSlice(
        slice_id=f"slice-{file_start}/{file_end}-{instance_ids[0]}",
        instance_ids=instance_ids,
        resource_scoped_id="res-1",
        source_id=source_id,
        asset_start=asset_start,
        source_start=file_start,
        source_end=file_end,
        timeline_start=file_start,
        timeline_end=file_end,
        audible=audible,
        blades=(),
    )


def _repair(start: float, end: float) -> Repair:
    return Repair(
        repeat=Repeat(start=start, end=end, count=3, text="되풀이", reason="echo"),
        outcome="kept",
        arbiter_clean=False,
        attempts=1,
        before=3,
        after=3,
        span=(start, end),
    )


def _build(
    reference: PcmReference | None = None,
    *,
    trace: SourceTrace | None = None,
    vad: VadResult | None = None,
    boundary: BoundaryProjection | None = None,
    repairs: tuple[Repair, ...] = (),
    cfg: ProjectConfig = CFG,
) -> SourceGuard:
    ref = reference or _reference()
    # 손 창열만으로는 [0, 프레임 수)를 못 덮는다 — 임계값 아래 깔개 창을 넣어
    # 실제 P5-02 결과의 모양(전체 타일링)을 맞춘다. 깔개는 보호 합집합에 못
    # 들어가므로 어떤 기대값도 움직이지 않는다(P5-03b 항목 2).
    if vad is None:
        vad = _vad(
            windows=(_window(0, ref.sample_count, 0.1),),
            source_id=ref.source_id,
            analysis_key=ref.analysis_key,
        )
    elif vad.status == "success" and vad.windows:
        vad = replace(vad, windows=(*vad.windows, _window(0, ref.sample_count, 0.1)))
    return build_source_guard(
        trace or _trace(ref.source_id, key=ref.analysis_key, origin=ref.pcm_sample0_file_time),
        ref,
        vad,
        boundary or _projection(),
        repairs,
        cfg,
    )


def _project(
    guard: SourceGuard,
    reference: PcmReference,
    *slices: LogicalSlice,
    frame_duration: Fraction = Fraction(1, 25),
) -> tuple[InstanceGuard, ...]:
    return project_guard(guard, reference, slices, frame_duration)


def _frames(*spans: tuple[int, int]) -> tuple[FrameInterval, ...]:
    return tuple(FrameInterval(start, end) for start, end in spans)


# ─────────────────────────────────────────────────────────────
#  합집합 — 어디서 보호가 오는가
# ─────────────────────────────────────────────────────────────


def test_union_protects_draft_and_settled_segments() -> None:
    reference = _reference()
    trace = _trace(
        reference.source_id,
        key=reference.analysis_key,
        draft=(_segment(reference.source_id, 5.0, 5.5, 9),),
    )
    guard = _build(reference, trace=trace)
    # 초안 [5, 5.5)와 골격 [1, 2)가 각각 여유를 받아 서로 합쳐지지 않는다.
    assert guard.protected == (
        SampleRange(_samples("1/2"), _samples("5/2")),
        SampleRange(_samples("9/2"), _samples("6")),
    )
    assert guard.status == "success"


def test_union_protects_source_cues() -> None:
    reference = _reference()
    trace = _trace(reference.source_id, key=reference.analysis_key, cues=((6.0, 7.0),))
    guard = _build(reference, trace=trace)
    assert SampleRange(_samples("11/2"), _samples("15/2")) in guard.protected


def test_union_protects_successful_boundary_cues_in_file_clock() -> None:
    reference = _reference()
    boundary = _projection(cues=(_pcue(reference.source_id, Fraction(7), Fraction(8)),))
    guard = _build(reference, boundary=boundary)
    assert SampleRange(_samples("13/2"), _samples("17/2")) in guard.protected


def test_union_takes_vad_windows_at_or_above_threshold_only() -> None:
    reference = _reference()
    vad = _vad(
        windows=(
            _window(RATE, 2 * RATE, CFG.vad_positive),  # 경계에 정확히 걸린 창
            _window(4 * RATE, 5 * RATE, 0.34),  # 임계값 아래 — 못 들어간다
        )
    )
    guard = _build(reference, vad=vad)
    assert guard.protected == (SampleRange(_samples("1/2"), _samples("5/2")),)
    assert not any(span.start >= _samples("7/2") for span in guard.protected)


def test_union_protects_repair_argument_and_trace_repairs() -> None:
    reference = _reference()
    argued = _build(reference, repairs=(_repair(5.5, 6.5),))
    assert SampleRange(_samples("5"), _samples("7")) in argued.protected
    traced = _build(
        reference,
        trace=_trace(
            reference.source_id,
            key=reference.analysis_key,
            repairs=(_repair(8.0, 9.0),),
        ),
    )
    assert SampleRange(_samples("15/2"), _samples("19/2")) in traced.protected


def test_union_protects_coverage_without_segments() -> None:
    reference = _reference()
    trace = _trace(
        reference.source_id,
        key=reference.analysis_key,
        coverage=(
            ChunkCoverage(index=0, offset=0.0, duration=5.0, segment_count=2),
            ChunkCoverage(index=1, offset=5.0, duration=5.0, segment_count=0),
        ),
    )
    guard = _build(reference, trace=trace)
    # 글자 없는 조각은 침묵의 증거가 아니므로 조각 전체가 불확실 보호로 들어간다.
    assert guard.protected == (
        SampleRange(_samples("1/2"), _samples("5/2")),
        SampleRange(_samples("9/2"), TOTAL),
    )


def test_padding_is_half_second_outward_and_clamped() -> None:
    reference = _reference()
    trace = _trace(
        reference.source_id,
        key=reference.analysis_key,
        cues=((0.0, 0.1), (9.9, 10.0)),
        spine=(),
    )
    guard = _build(reference, trace=trace)
    assert guard.protected == (
        SampleRange(0, _samples("3/5")),  # 앞 여유는 0에서 묶인다
        SampleRange(_samples("47/5"), TOTAL),  # 뒤 여유는 저장 끝에서 묶인다
    )
    assert Fraction(1, 2) == PAD


def test_adjacent_and_overlapping_ranges_merge_exactly() -> None:
    reference = _reference()
    vad = _vad(
        windows=(
            _window(RATE, 2 * RATE, 0.9),
            _window(2 * RATE, 3 * RATE, 0.9),
            _window(_samples("6/5"), _samples("7/5"), 0.8, channel=1),
        )
    )
    guard = _build(reference, vad=vad)
    # 닿은 창 둘과 겹친 채널 1 창, 그리고 골격 [1, 2)가 한 덩어리가 된다.
    assert guard.protected == (SampleRange(_samples("1/2"), _samples("7/2")),)


def test_no_short_run_drop_even_below_250ms() -> None:
    reference = _reference()
    short = _window(5 * RATE, 5 * RATE + CFG.vad_window_samples, 0.99)
    guard = _build(reference, vad=_vad(windows=(short,)))
    run_seconds = Fraction(CFG.vad_window_samples, RATE)
    assert run_seconds < GUARD_MIN_RUN_SECONDS  # 32ms — 결합 사양의 250ms보다 짧다
    assert (
        SampleRange(_samples("9/2"), 5 * RATE + CFG.vad_window_samples + _samples("1/2"))
        in guard.protected
    )


def test_tail_window_at_sample_count_survives() -> None:
    reference = _reference()
    tail = _window(TOTAL - CFG.vad_window_samples, TOTAL, 0.9)
    guard = _build(reference, vad=_vad(windows=(tail,)))
    assert SampleRange(TOTAL - CFG.vad_window_samples - _samples("1/2"), TOTAL) in guard.protected


def test_protected_proportion_metrics_count_samples() -> None:
    reference = _reference()
    trace = _trace(
        reference.source_id,
        key=reference.analysis_key,
        draft=(_segment(reference.source_id, 5.0, 5.5, 9),),
    )
    guard = _build(reference, trace=trace)
    assert guard.total_samples == TOTAL
    assert guard.protected_samples == _samples("2") + _samples("3/2")


# ─────────────────────────────────────────────────────────────
#  실패 닫힘 — error·부재는 전체 보호와 검토
# ─────────────────────────────────────────────────────────────


def test_vad_error_keeps_all_with_review() -> None:
    reference = _reference()
    guard = _build(reference, vad=_vad(status="error", error="세션 run이 실패했다"))
    assert guard.status == "keep_all"
    assert guard.protected == (SampleRange(0, TOTAL),)
    assert guard.vad_without_text == ()
    assert guard.review


def test_trace_error_keeps_all_with_review() -> None:
    reference = _reference()
    guard = _build(reference, trace=_trace(reference.source_id, status="error"))
    assert guard.status == "keep_all"
    assert guard.protected == (SampleRange(0, TOTAL),)
    assert guard.review


def test_missing_coverage_keeps_all() -> None:
    reference = _reference()
    trace = _trace(reference.source_id, key=reference.analysis_key, coverage=())
    guard = _build(reference, trace=trace)
    assert guard.status == "keep_all"
    assert guard.protected == (SampleRange(0, TOTAL),)


def test_empty_vad_windows_on_nonempty_audio_keeps_all() -> None:
    reference = _reference()
    guard = _build(reference, vad=_vad(windows=()))
    assert guard.status == "keep_all"
    assert guard.protected == (SampleRange(0, TOTAL),)
    assert guard.review


def test_empty_stt_alone_is_not_silence() -> None:
    reference = _reference()
    trace = _trace(
        reference.source_id,
        key=reference.analysis_key,
        cues=(),
        spine=(),
        status="empty",
        coverage=(ChunkCoverage(index=0, offset=0.0, duration=10.0, segment_count=0),),
    )
    guard = _build(reference, trace=trace)
    # 빈 성공은 error가 아니다 — 덮개가 전부 불확실이라 전체가 보호일 뿐이다.
    assert guard.status == "success"
    assert guard.protected == (SampleRange(0, TOTAL),)


def test_vad_speech_without_usable_stt_is_protected_and_listed() -> None:
    reference = _reference()
    vad = _vad(
        windows=(
            _window(4 * RATE, 5 * RATE, 0.9),  # 글자 없는 말
            _window(_samples("6/5"), _samples("3/2"), 0.9),  # 골격 [1, 2) 안의 말
        )
    )
    guard = _build(reference, vad=vad)
    assert SampleRange(_samples("7/2"), _samples("11/2")) in guard.protected
    # 나열은 여유 없는 VAD 런 그 자체다 — 세그먼트가 덮는 쪽은 빠진다.
    assert guard.vad_without_text == (SampleRange(4 * RATE, 5 * RATE),)
    assert guard.review


def test_unresolved_boundary_cues_stay_in_review() -> None:
    reference = _reference()
    boundary = _projection(
        cues=(
            _pcue(
                reference.source_id,
                Fraction(1),
                Fraction(2),
                provenance="source",
                flags=(BOUNDARY_UNRESOLVED,),
            ),
        )
    )
    guard = _build(reference, boundary=boundary)
    assert guard.status == "success"
    assert any(BOUNDARY_UNRESOLVED in note for note in guard.review)


def test_build_rejects_forged_linkage() -> None:
    reference = _reference()
    with pytest.raises(ValueError, match="source_id"):
        _build(reference, trace=_trace("other"))
    with pytest.raises(ValueError, match="분석 키"):
        _build(reference, trace=_trace(reference.source_id, key="pcm-forged"))
    with pytest.raises(ValueError, match="원점"):
        _build(
            reference,
            trace=_trace(reference.source_id, key=reference.analysis_key, origin=Fraction(1)),
        )
    with pytest.raises(ValueError, match="source_id"):
        _build(reference, vad=_vad(source_id="other"))


# ─────────────────────────────────────────────────────────────
#  투영 — 원점 한 번·소스 격자·인스턴스마다 닻
# ─────────────────────────────────────────────────────────────


def test_projection_adds_origin_exactly_once() -> None:
    reference = _reference(origin=Fraction(1, 2), sample_count=4 * RATE)
    vad = _vad(windows=(_window(0, 2 * RATE, 0.9),))
    guard = _build(reference, vad=vad)
    # 보호 샘플 [0, 40000) = 오디오 [0, 2.5초) → 파일 [1/2, 3). 원점을 두 번 더하면
    # [1, 7/2)가 되어 첫 슬라이스가 (25, 38)로 밀린다.
    first = _slice(reference.source_id, Fraction(1, 2), Fraction(3, 2), "clip-1")
    second = _slice(reference.source_id, Fraction(2), Fraction(3), "clip-2")
    guards = _project(guard, reference, first, second)
    assert [(row.instance_id, row.protected_frames) for row in guards] == [
        ("clip-1", _frames((12, 38))),  # floor(25/2)=12, ceil(75/2)=38
        ("clip-2", _frames((50, 75))),  # 교집합이 슬라이스 끝 3초에서 묶인다
    ]


def test_projection_handles_negative_origin() -> None:
    origin = Fraction(-1, 32)
    reference = _reference(origin=origin, sample_count=RATE)
    trace = _trace(
        reference.source_id,
        key=reference.analysis_key,
        origin=origin,
        cues=((0.0, 0.05),),
        spine=(),
    )
    vad = _vad(windows=(_window(0, 800, 0.9),))
    guard = _build(reference, trace=trace, vad=vad)
    # 샘플 0의 파일 시각은 -1/32다 — 음수 원점도 정확히 한 번 더한다.
    edge = _slice(reference.source_id, origin, origin + Fraction(8800, RATE), "clip-1")
    (instance,) = _project(guard, reference, edge, frame_duration=Fraction(1, 50))
    assert instance.protected_frames == _frames((-2, 26))


def test_projection_snaps_on_source_grid_not_raw_asset_anchor() -> None:
    reference = _reference()
    vad = _vad(windows=(_window(5 * RATE, 6 * RATE, 0.9),))
    guard = _build(reference, vad=vad)
    epoch = Fraction(300)
    clip = _slice(reference.source_id, Fraction(5), Fraction(6), "clip-1", asset_start=epoch)
    duration = Fraction(1, 30)
    (instance,) = _project(guard, reference, clip, frame_duration=duration)
    # 두 정식 공식은 같은 소스 격자를 준다 — 에셋 시계에 300초를 더해도 인덱스는 그대로.
    via_asset = interval_outward(epoch + 5, epoch + 6, duration, epoch)
    via_file = interval_outward(Fraction(5), Fraction(6), duration, Fraction(0))
    assert via_asset == via_file == FrameInterval(150, 180)
    assert instance.protected_frames == _frames((150, 180))
    # 원시 파일 끝점을 에셋 닻에 그대로 넣는 그릇된 공식 — 큰 음수가 나온다.
    wrong = interval_outward(Fraction(5), Fraction(6), duration, epoch)
    assert wrong.start_frame == -8850
    assert instance.protected_frames != _frames((wrong.start_frame, wrong.end_frame))


def test_repeated_and_merged_instances_get_separate_guards() -> None:
    reference = _reference()
    vad = _vad(
        windows=(
            _window(RATE, 2 * RATE, 0.9),
            _window(8 * RATE, 9 * RATE, 0.9),
        )
    )
    guard = _build(reference, vad=vad)
    first = _slice(reference.source_id, Fraction(1), Fraction(2), "clip-1")
    second = _slice(reference.source_id, Fraction(8), Fraction(9), "clip-2")
    merged = _slice(reference.source_id, Fraction(1), Fraction(2), "clip-3", "clip-4")
    guards = _project(guard, reference, first, second, merged)
    assert [(row.instance_id, row.protected_frames) for row in guards] == [
        ("clip-1", _frames((25, 50))),
        ("clip-2", _frames((200, 225))),
        ("clip-3", _frames((25, 50))),
        ("clip-4", _frames((25, 50))),
    ]


def test_inaudible_slice_is_retained_without_removal_authority() -> None:
    reference = _reference()
    guard = _build(reference)
    quiet = _slice(reference.source_id, Fraction(5), Fraction(6), "clip-mute", audible=False)
    loud = _slice(reference.source_id, Fraction(5), Fraction(6), "clip-live")
    guards = _project(guard, reference, quiet, loud)
    by_id = {row.instance_id: row for row in guards}
    # 보호가 없는 가청 슬라이스는 지울 수 있지만, 불가청 슬라이스는 전체가 보호다.
    assert by_id["clip-live"].protected_frames == ()
    assert by_id["clip-mute"].protected_frames == _frames((125, 150))
    assert by_id["clip-mute"].review


def test_keep_all_guard_projects_to_full_slice_protection() -> None:
    reference = _reference()
    guard = _build(reference, vad=_vad(status="error", error="세션 run이 실패했다"))
    first = _slice(reference.source_id, Fraction(1), Fraction(2), "clip-1")
    second = _slice(reference.source_id, Fraction(8), Fraction(9), "clip-2")
    guards = _project(guard, reference, first, second)
    assert [(row.instance_id, row.protected_frames) for row in guards] == [
        ("clip-1", _frames((25, 50))),
        ("clip-2", _frames((200, 225))),
    ]
    assert all(row.review for row in guards)


def test_projection_rejects_broken_linkage() -> None:
    reference = _reference()
    guard = _build(reference)
    clip = _slice(reference.source_id, Fraction(1), Fraction(2), "clip-1")
    with pytest.raises(ValueError, match="source_id"):
        _project(replace(guard, source_id="other"), reference, clip)
    with pytest.raises(ValueError, match="total_samples"):
        _project(replace(guard, total_samples=TOTAL - 1), reference, clip)
    with pytest.raises(ValueError, match="sample_rate"):
        _project(guard, _reference(sample_rate=0), clip)
    with pytest.raises(ValueError, match="source_id"):
        _project(guard, reference, _slice("other", Fraction(1), Fraction(2), "clip-x"))


# ─────────────────────────────────────────────────────────────
#  부재 — 참조가 없으면 호출도 없다
# ─────────────────────────────────────────────────────────────


def test_unavailable_resources_never_borrow_the_healthy_reference() -> None:
    shared = "acid-shared"
    reference = _reference(source_id=shared)
    healthy = _slice(shared, Fraction(1), Fraction(2), "clip-healthy")
    # 같은 내용이라 자원이 둘: 건강한 쪽만 참조가 있고 나머지는 타입 있는 부재다.
    stream_failed = UnavailableInstance(
        resource_scoped_id="res-b", instance_id="clip-b", reason_code="stream_unavailable"
    )
    declared_mute = UnavailableInstance(
        resource_scoped_id="res-c", instance_id="clip-c", reason_code="declared_no_audio"
    )
    projection = _projection(
        unavailable=(stream_failed, declared_mute), locked=frozenset({"clip-b", "clip-c"})
    )
    unavailable_records = (
        UnavailableSourceAudio(
            variant="unavailable_source_audio",
            resource_scoped_id="res-b",
            source_id=shared,
            original_sha256="b" * 64,
            status="error",
            reason_code="stream_unavailable",
            reason="스트림이 없다",
        ),
        UnavailableSourceAudio(
            variant="unavailable_source_audio",
            resource_scoped_id="res-c",
            source_id=shared,
            original_sha256="c" * 64,
            status="no_audio",
            reason_code="declared_no_audio",
            reason="선언된 무음",
        ),
    )
    # 참조·VAD·가드는 건강한 소스 하나에서만 산다 — 부재에는 PcmReference 자체가 없다.
    assert all(record.variant == "unavailable_source_audio" for record in unavailable_records)
    guard = _build(reference, boundary=projection)
    guards = _project(guard, reference, healthy)
    assert [row.instance_id for row in guards] == ["clip-healthy"]
    assert {row.instance_id for row in guards}.isdisjoint({"clip-b", "clip-c"})
    # 부재는 문장이 아니라 타입 기록으로 남는다 — 제거 권한이 없는 keep-all 증거다.
    assert [row.reason_code for row in projection.unavailable_instances] == [
        "stream_unavailable",
        "declared_no_audio",
    ]


# ─────────────────────────────────────────────────────────────
#  레일과 신원 — 설정은 raise, 입력은 keep_all, 세대는 증명 (P5-03b)
# ─────────────────────────────────────────────────────────────


def _result(
    windows: tuple[VadWindow, ...],
    *,
    source_id: str = "asset-1",
    analysis_key: str = "pcm-asset-1",
    status: str = "success",
    error: str | None = None,
) -> VadResult:
    """손 VAD 결과 — 정규화 없이 그대로 build_source_guard에 들어간다."""
    return VadResult(
        source_id=source_id,
        analysis_key=analysis_key,
        status=status,  # type: ignore[arg-type]
        windows=windows,
        error=error,
    )


def _forged(**overrides: object) -> ProjectConfig:
    """검증을 한 번도 통과하지 않은 설정 — `__init__`을 건너뛰고 슬롯에 넣는다.

    생성자가 우회를 닫은 뒤(P5-06)로 `replace(CFG, ...)`는 생성 단계에서 먼저
    터져 버려서 이 파일의 관문(`build_source_guard`의 `_require_config`)을
    겨본 적이 없는 채 녹색이 된다. 슬롯에 직접 넣는 이 손쓰기는 그 관문이
    여전히 산다는 것을 증명한다 — 테스트 안에서만 산다. 프로덕션이 이 우회를
    쓰는 순간 P5-03b가 재현된다.
    """
    config = object.__new__(ProjectConfig)
    for field in fields(ProjectConfig):
        object.__setattr__(config, field.name, overrides.get(field.name, getattr(CFG, field.name)))
    return config


def test_config_defects_raise_before_any_protection() -> None:
    reference = _reference()
    trace = _trace(reference.source_id, key=reference.analysis_key)
    for bad_positive in (float("nan"), 1.5, True, -0.1):
        with pytest.raises(ValueError, match="vad_positive"):
            build_source_guard(
                trace,
                reference,
                _vad(),
                _projection(),
                (),
                _forged(vad_positive=bad_positive),
            )
    for bad_padding in (Fraction(0), Fraction(-1, 2), Fraction(-2), 0.5):
        with pytest.raises(ValueError, match="guard_padding_seconds"):
            build_source_guard(
                trace,
                reference,
                _vad(),
                _projection(),
                (),
                _forged(guard_padding_seconds=bad_padding),
            )
    # 음수 여유는 protected=[]를 내지 않고 그 자리에서 닫힌다 — 설정 결함은
    # 호출자 잘못이므로 keep_all로 세탁하지도 않는다(P5-05b가 그은 선).


def test_vad_window_defects_fail_closed_to_keep_all() -> None:
    reference = _reference()
    trace = _trace(reference.source_id, key=reference.analysis_key)
    defects = {
        "확률 nan": _result((_window(0, TOTAL, float("nan")),)),
        "확률 범위 밖": _result((_window(0, TOTAL, 1.5),)),
        "부분 덮음": _result((_window(0, 2 * RATE, 0.9),)),
        "뒤집힌 창": _result((_window(2 * RATE, RATE, 0.9),)),
        "범위 밖 창": _result((_window(0, TOTAL + 1, 0.9),)),
        "실수 끝점": _result(
            (VadWindow(channel=0, start_sample=0.5, end_sample=512, probability=0.9),)  # type: ignore[arg-type]
        ),
        "창 없음": _result(()),
    }
    for label, vad in defects.items():
        guard = build_source_guard(trace, reference, vad, _projection(), (), CFG)
        assert guard.status == "keep_all", label
        assert guard.protected == (SampleRange(0, TOTAL),), label
        assert guard.vad_without_text == (), label
        assert guard.review, label


def test_vad_result_from_another_pcm_generation_is_rejected() -> None:
    reference = _reference()
    trace = _trace(reference.source_id, key=reference.analysis_key)
    forged = _result((_window(0, TOTAL, 0.9),), analysis_key="pcm-forged")
    with pytest.raises(ValueError, match="analysis_key"):
        build_source_guard(trace, reference, forged, _projection(), (), CFG)


def test_projection_rejects_stale_reference_generation() -> None:
    fresh = _reference(origin=Fraction(1, 2), sample_count=4 * RATE)
    guard = _build(fresh, vad=_vad(windows=(_window(0, 2 * RATE, 0.9),)))
    clip = _slice(fresh.source_id, Fraction(1, 2), Fraction(3, 2), "clip-1")
    stale_origin = _reference(origin=Fraction(3, 2), sample_count=4 * RATE)
    with pytest.raises(ValueError, match="원점"):
        _project(guard, stale_origin, clip)
    stale_key = _reference(origin=Fraction(1, 2), sample_count=4 * RATE, key="pcm-other")
    with pytest.raises(ValueError, match="analysis_key"):
        _project(guard, stale_key, clip)


def test_keep_all_protects_the_whole_slice_even_past_pcm_end() -> None:
    reference = _reference()
    guard = _build(reference, vad=_vad(status="error", error="세션 run이 실패했다"))
    tail = _slice(reference.source_id, Fraction(9), Fraction(12), "clip-tail")
    (instance,) = _project(guard, reference, tail)
    # PCM은 10초(250프레임)뿐이지만 keep_all은 슬라이스 전체(300프레임)를 지킨다.
    assert instance.protected_frames == _frames((225, 300))
    assert instance.status == "keep_all"


def test_instance_guards_carry_typed_status_and_source() -> None:
    reference = _reference()
    healthy = _build(reference)
    broken = _build(reference, vad=_vad(status="error", error="세션 run이 실패했다"))
    live = _slice(reference.source_id, Fraction(1), Fraction(2), "clip-live")
    mute = _slice(reference.source_id, Fraction(1), Fraction(2), "clip-mute", audible=False)
    rows = _project(healthy, reference, live, mute)
    assert [(row.instance_id, row.status, row.source_id) for row in rows] == [
        ("clip-live", "success", reference.source_id),
        ("clip-mute", "inaudible", reference.source_id),
    ]
    assert _project(broken, reference, live)[0].status == "keep_all"
    # 불가청이 keep_all보다 강한 진술이다 — 둘이 겹치면 inaudible이 이긴다.
    assert _project(broken, reference, mute)[0].status == "inaudible"


def test_healthy_inputs_still_produce_the_exact_union() -> None:
    reference = _reference()
    trace = _trace(reference.source_id, key=reference.analysis_key)
    vad = _result(
        (
            _window(0, 4 * RATE, 0.1),
            _window(4 * RATE, 5 * RATE, 0.9),
            _window(5 * RATE, TOTAL, 0.1),
        )
    )
    guard = build_source_guard(trace, reference, vad, _projection(), (), CFG)
    # 검출 probe의 기준선 그대로 — 골격 [1,2)와 VAD [4,5)만 보호다.
    assert guard.status == "success"
    assert guard.protected == (
        SampleRange(_samples("1/2"), _samples("5/2")),
        SampleRange(_samples("7/2"), _samples("11/2")),
    )
    assert guard.vad_without_text == (SampleRange(4 * RATE, 5 * RATE),)
    assert len(guard.review) == 1


def test_overlapping_windows_across_channels_are_valid_coverage() -> None:
    reference = _reference()
    trace = _trace(reference.source_id, key=reference.analysis_key)
    vad = _result(
        (
            _window(0, TOTAL, 0.1),
            _window(0, TOTAL, 0.05, channel=1),
        )
    )
    guard = build_source_guard(trace, reference, vad, _projection(), (), CFG)
    assert guard.status == "success"
    # 채널끼리 겹쳐도 합집합이 [0, TOTAL)을 덮으면 합격 — 양쪽 다 임계값 아래다.
    assert guard.protected == (SampleRange(_samples("1/2"), _samples("5/2")),)
