"""보수적 말 보호 — 믿을 수 있는 말의 합집합과 인스턴스 투영 (P5-03).

P5-02가 낸 확률 창에 임계값(`vad_positive`)을 처음으로 대고, 지우면 안 되는
구간의 **합집합**을 만든다. 이 계층의 선:

- **합집합은 늘 더 보호 쪽으로만 간다.** Whisper 세그먼트(초안·골격)·소스 큐·
  성공 경계 큐·VAD 양성 창·재전사 범위·글자 없는 덮개를 모두 모아 바깥으로
  `guard_padding_seconds`만큼 부풀리고 저장 프레임에 묶는다. 짧아서 버리는
  최소 길이는 없다 — `GUARD_MIN_RUN_SECONDS`(250ms)는 그 금지의 이름이지
  임계값이 아니다. 닿고 겹치는 범위는 정확히 합쳐진다.
- **실패는 조용한 침묵이 아니다.** VAD·전사 트레이스·덮개가 error거나 없으면
  소스 전체를 보호하고 `keep_all`로 검토를 남긴다. 빈 전사만으로는 침묵이
  아니지만, 글자 없는 덮개는 조각마다 불확실 보호로 들어간다. VAD 양성인데 쓸
  글자가 없는 구간은 보호되고 `vad_without_text`와 검토에 남는다.
- **시계 변환은 딱 한 번.** 보호 끝점은 `Fraction(샘플, 주파수) + 참조 원점`으로
  파일 시계에 옮겨지고(읽을 수 있는 시각은 유도하지 않는다 — P4-04b), 슬라이스
  구간과 교집합한 뒤 `interval_outward(시작, 끝, 프레임 길이, 0)`로 소스 격자에
  맞춘다. 원시 파일 끝점에 에셋 닻을 주면 안 된다 — 에셋 기원 300초가 파일 5초를
  큰 음수 인덱스로 만든다.
- **불가청 인스턴스는 남되 권한이 없다.** 음소거·비활성·영상 전용 슬라이스의
  인스턴스도 기록으로 남지만 제거 권한이 없어 슬라이스 전체를 보호한다. 선언된
  무음·pre-PCM 부재 자원은 애초에 참조가 없으므로 이 모듈의 어느 호출도 없다 —
  같은 내용의 건강한 참조를 빌리지 않는다(P4의 타입 있는 부재가 정본).

순수 함수다 — 네트워크·ffmpeg·모델 실행이 없고 numpy도 모르므로 extra 없이도
import된다(runtime·inference와 같은 선).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from vlogsrt.models import Repair
from vlogsrt.project.boundary import BOUNDARY_UNRESOLVED, BoundaryProjection, LogicalSlice
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.pcm import PcmReference, SampleRange
from vlogsrt.project.speech import SourceTrace
from vlogsrt.project.timebase import FrameInterval, interval_outward
from vlogsrt.project.vad.inference import VadResult

GUARD_MIN_RUN_SECONDS = Fraction(1, 4)
"""결합 사양의 250ms — **짧아서 말을 버리는 최소 길이가 없다**는 금지의 이름이다.

얇고 짧은 창도, 꼬리 창도 보호에서 빠지지 않는다. 이 상수는 임계값이 아니다 —
어떤 함수도 이 값과 비교해 런을 걸러내지 않는다. 규칙이 코드 밖에서 흩어지지
않게 이름만 준 것이다(계획 "VAD guard": no min 250ms speech dropping).
"""

_STATUS_SUCCESS: Literal["success"] = "success"
_STATUS_KEEP_ALL: Literal["keep_all"] = "keep_all"

_SOURCE_GRID_ANCHOR = Fraction(0)
"""소스 격자의 닻 — 파일 시계 끝점을 그대로 주면 격자 인덱스가 곧 나온다.

에셋 시계 공식 `interval_outward(asset_start + 끝점, …, anchor=asset_start)`와
정확히 같은 인덱스를 준다(계획 "VAD guard"의 동치 공식).
"""

_Span = tuple[Fraction, Fraction]
"""오디오 국소 초의 반개 구간 — 보호 씨앗과 STT 증거의 좌표계다."""

__all__ = [
    "GUARD_MIN_RUN_SECONDS",
    "InstanceGuard",
    "SourceGuard",
    "build_source_guard",
    "project_guard",
]


@dataclass(frozen=True, slots=True)
class SourceGuard:
    """소스 하나의 보호 합집합 — 좌표는 저장 PCM의 `[start, end)` 프레임이다.

    `keep_all`은 실패 닫힘이다 — 가청 소스 전체가 보호이고 이유는 `review`에
    남는다. `vad_without_text`는 말은 있는데 쓸 글자이 없던 VAD 런이다(여유 없는
    런 그 자체). 두 합계는 `protected_samples`/`total_samples`로 보호 비율을
    바로 잰다.
    """

    source_id: str
    protected: tuple[SampleRange, ...]
    vad_without_text: tuple[SampleRange, ...]
    protected_samples: int
    total_samples: int
    status: Literal["success", "keep_all"]
    review: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError(f"source_id가 비지 않은 문자열이 아니다: {self.source_id!r}")
        if self.status not in {_STATUS_SUCCESS, _STATUS_KEEP_ALL}:
            raise ValueError(f"알 수 없는 상태다: {self.status!r}")
        if _bad_count(self.total_samples) or self.total_samples < 1:
            raise ValueError(f"total_samples가 1 이상 정수가 아니다: {self.total_samples!r}")
        _require_ordered("protected", self.protected, self.total_samples)
        _require_ordered("vad_without_text", self.vad_without_text, self.total_samples)
        if sum(span.end - span.start for span in self.protected) != self.protected_samples:
            raise ValueError(
                f"protected_samples가 보호 구간의 합과 다르다: {self.protected_samples!r}"
            )


@dataclass(frozen=True, slots=True)
class InstanceGuard:
    """인스턴스 하나의 보호 — 좌표는 소스 격자의 반개 프레임 구간이다."""

    instance_id: str
    protected_frames: tuple[FrameInterval, ...]
    review: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, str) or not self.instance_id:
            raise ValueError(f"instance_id가 비지 않은 문자열이 아니다: {self.instance_id!r}")
        previous_end: int | None = None
        for span in self.protected_frames:
            if span.end_frame < span.start_frame:
                raise ValueError(f"protected_frames가 뒤집혔다: {span!r}")
            if previous_end is not None and span.start_frame < previous_end:
                raise ValueError(
                    f"protected_frames가 겹치거나 순서가 어긋난다: {span!r}"
                    f" (앞 구간 끝 {previous_end})"
                )
            previous_end = span.end_frame


def build_source_guard(
    trace: SourceTrace,
    reference: PcmReference,
    vad: VadResult,
    boundary: BoundaryProjection,
    repairs: Sequence[Repair],
    cfg: ProjectConfig,
) -> SourceGuard:
    """소스 하나의 보호 합집합을 만든다. 판단이 필요할 때는 늘 더 보호한다."""
    _require_source_linkage(trace, reference)
    _require_vad_linkage(vad, reference)
    blocked = _blocked_reviews(trace, vad)
    if blocked:
        return _keep_all(reference, blocked)
    seeds, evidence, reviews = _source_spans(trace, boundary, repairs, reference)
    rate = reference.sample_rate
    vad_runs = _vad_runs(vad, cfg, reference.sample_count)
    # 여유는 합집합 전체에 붙는다 — VAD 창도 세그먼트도 같은 0.5초를 받는다.
    vad_spans = [(Fraction(run.start, rate), Fraction(run.end, rate)) for run in vad_runs]
    protected = _merge(
        _clamped_samples(
            [*seeds, *vad_spans], cfg.guard_padding_seconds, rate, reference.sample_count
        )
    )
    covered = _merge(_clamped_samples(evidence, Fraction(0), rate, reference.sample_count))
    without_text = _difference(vad_runs, covered)
    if without_text:
        frames = sum(span.end - span.start for span in without_text)
        reviews.append(
            f"VAD 양성 {len(without_text)}구간 {frames}프레임에 쓸 글자가 없다 — "
            "보호하고 검토로 남긴다"
        )
    return SourceGuard(
        source_id=reference.source_id,
        protected=protected,
        vad_without_text=without_text,
        protected_samples=sum(span.end - span.start for span in protected),
        total_samples=reference.sample_count,
        status=_STATUS_SUCCESS,
        review=tuple(reviews),
    )


def project_guard(
    source_guard: SourceGuard,
    reference: PcmReference,
    slices: Sequence[LogicalSlice],
    frame_duration: Fraction,
) -> tuple[InstanceGuard, ...]:
    """소스 가드를 인스턴스마다 소스 격자로 내린다 — 원점은 정확히 한 번 더한다.

    순수한 투영이다: 슬라이스 순서·인스턴스 순서 그대로 하나씩 내놓는다. 불가청
    슬라이스의 인스턴스도 남되 제거 권한이 없어 슬라이스 전체를 보호한다.
    """
    _require_projection_linkage(source_guard, reference, slices)
    guards: list[InstanceGuard] = []
    for slice_ in slices:
        frames = _slice_frames(source_guard, reference, slice_, frame_duration)
        review = _instance_review(source_guard, slice_)
        guards.extend(
            InstanceGuard(instance_id=instance_id, protected_frames=frames, review=review)
            for instance_id in slice_.instance_ids
        )
    return tuple(guards)


# ─────────────────────────────────────────────────────────────
#  합집합 재료 — 어디서 보호가 오는가
# ─────────────────────────────────────────────────────────────


def _source_spans(
    trace: SourceTrace,
    boundary: BoundaryProjection,
    repairs: Sequence[Repair],
    reference: PcmReference,
) -> tuple[list[_Span], list[_Span], list[str]]:
    """보호 씨앗과 STT 증거 — 전부 오디오 국소 초(원점 없음)의 정확한 값.

    씨앗은 여유를 받아 보호가 되고, 증거는 `vad_without_text`를 가르는 잣대가
    된다. 글자 없는 덮개는 씨앗에만 있다 — 증거가 아니라 불확실이므로.
    """
    segments = _segment_spans(trace)
    cues = _cue_spans(trace)
    reasked = _repair_spans(repairs, trace)
    from_boundary, unresolved = _boundary_spans(boundary, trace, reference)
    uncertain = _uncertain_spans(trace)
    seeds = [*segments, *cues, *reasked, *from_boundary, *uncertain]
    evidence = [*segments, *cues, *reasked, *from_boundary]
    reviews: list[str] = []
    if trace.status == "empty":
        reviews.append("빈 전사(status=empty)는 침묵의 증거가 아니다 — 덮개 단위로 보호한다")
    if uncertain:
        reviews.append(f"글자 없는 덮개 {len(uncertain)}조각 — 침묵의 증거가 아니므로 보호한다")
    if unresolved:
        reviews.append(
            f"미해결 경계 큐 {unresolved}장({BOUNDARY_UNRESOLVED}) — 원문 큐 그대로 보호한다"
        )
    return seeds, evidence, reviews


def _segment_spans(trace: SourceTrace) -> list[_Span]:
    """초안·골격 세그먼트 전부 — 길이 0·뒤집힌 기록도 위치는 지킨다(규칙 9)."""
    return [
        (min(segment.audio_start, segment.audio_end), max(segment.audio_start, segment.audio_end))
        for segment in (*trace.draft, *trace.settled_spine)
    ]


def _cue_spans(trace: SourceTrace) -> list[_Span]:
    """정제된 소스 큐 — 레거시 float를 정확한 Fraction으로 옮긴 그대로."""
    spans: list[_Span] = []
    for cue in trace.cues:
        start, end = Fraction(str(cue.start)), Fraction(str(cue.end))
        spans.append((min(start, end), max(start, end)))
    return spans


def _repair_spans(repairs: Sequence[Repair], trace: SourceTrace) -> list[_Span]:
    """다시 물어본 구간 — 인자로 받은 것과 트레이스가 남긴 것 둘 다.

    `span=(0, 0)`은 "다시 묻지 않았다"의 기본값이지 위치가 아니다 — 여유를 주고
    보호할 자리가 못 된다.
    """
    spans: list[_Span] = []
    for repair in (*repairs, *trace.repairs):
        start, end = Fraction(str(repair.span[0])), Fraction(str(repair.span[1]))
        if end > start:
            spans.append((start, end))
    return spans


def _boundary_spans(
    boundary: BoundaryProjection, trace: SourceTrace, reference: PcmReference
) -> tuple[list[_Span], int]:
    """경계 투영 큐 — 파일 시계에서 원점을 정확히 한 번 빼 국소 시계로.

    성공 경계 큐뿐 아니라 남은 원문 큐도 합친다 — 원문은 어차피 소스 큐가 덮고,
    미해결 경계는 검토로만 센다. 다른 소스의 큐는 여기에 들어오지 않는다.
    """
    origin = reference.pcm_sample0_file_time
    spans: list[_Span] = []
    unresolved = 0
    for cue in boundary.cues:
        if cue.source_id != trace.source_id:
            continue
        if BOUNDARY_UNRESOLVED in cue.flags:
            unresolved += 1
        spans.append((cue.source_start - origin, cue.source_end - origin))
    return spans, unresolved


def _uncertain_spans(trace: SourceTrace) -> list[_Span]:
    """글자 없는 덮개 — 조각이 돌아왔지만 세그먼트가 하나도 없다(불확실)."""
    return [
        (Fraction(str(record.offset)), Fraction(str(record.offset + record.duration)))
        for record in trace.coverage
        if record.segment_count == 0
    ]


def _vad_runs(vad: VadResult, cfg: ProjectConfig, total: int) -> tuple[SampleRange, ...]:
    """양성 창의 합집합 — 채널을 합치고 저장 범위에 묶는다.

    채널별 추론은 P5-02가 이미 했다(반대 위상 사칭 방지). 여기는 임계값
    `vad_positive` 이상인 창을 전부 모을 뿐 — 창 길이에 최소는 없다
    (`GUARD_MIN_RUN_SECONDS` 참조).
    """
    positive = [
        SampleRange(
            max(0, min(total, window.start_sample)),
            max(0, min(total, window.end_sample)),
        )
        for window in vad.windows
        if window.probability >= cfg.vad_positive
    ]
    return _merge(positive)


# ─────────────────────────────────────────────────────────────
#  구간 산술 — 여유·환산·병합·차집합
# ─────────────────────────────────────────────────────────────


def _clamped_samples(
    spans: Sequence[_Span], padding: Fraction, sample_rate: int, total: int
) -> tuple[SampleRange, ...]:
    """씨앗마다 바깥으로 여유를 주고 저장 프레임에 묶는다 — 시작 floor·끝 ceil.

    환산은 바깥쪽으로만 간다(남는 오디오 이상을 보호하지만 덜 보호하지는 않는다).
    양 끝은 실제 프레임 수에 묶인다 — 여유가 저장 밖으로 나가면 잘린다.
    """
    samples: list[SampleRange] = []
    for start, end in spans:
        first = min(total, max(0, math.floor((start - padding) * sample_rate)))
        last = min(total, max(0, math.ceil((end + padding) * sample_rate)))
        if last > first:
            samples.append(SampleRange(first, last))
    return tuple(samples)


def _merge(ranges: Sequence[SampleRange]) -> tuple[SampleRange, ...]:
    """닿거나 겹치는 반개 구간을 정확히 합친다 — 좌표는 하나도 움직이지 않는다."""
    merged: list[SampleRange] = []
    for span in sorted(ranges, key=lambda item: (item.start, item.end)):
        if span.end <= span.start:
            continue
        if merged and span.start <= merged[-1].end:
            previous = merged[-1]
            merged[-1] = SampleRange(previous.start, max(previous.end, span.end))
        else:
            merged.append(span)
    return tuple(merged)


def _difference(
    runs: Sequence[SampleRange], evidence: Sequence[SampleRange]
) -> tuple[SampleRange, ...]:
    """병합된 런에서 병합된 증거를 뺀다 — 남은 조각이 `vad_without_text`다."""
    leftover: list[SampleRange] = []
    for run in runs:
        cursor = run.start
        for covered in evidence:
            if covered.end <= cursor:
                continue
            if covered.start >= run.end:
                break
            if covered.start > cursor:
                leftover.append(SampleRange(cursor, covered.start))
            cursor = max(cursor, covered.end)
        if cursor < run.end:
            leftover.append(SampleRange(cursor, run.end))
    return tuple(leftover)


# ─────────────────────────────────────────────────────────────
#  투영 — 파일 시계 한 번·소스 격자·불가청 전체 보호
# ─────────────────────────────────────────────────────────────


def _slice_frames(
    source_guard: SourceGuard,
    reference: PcmReference,
    slice_: LogicalSlice,
    frame_duration: Fraction,
) -> tuple[FrameInterval, ...]:
    """한 슬라이스 안의 보호 프레임 — 불가청 슬라이스는 전체가 보호다."""
    if not slice_.audible:
        whole = interval_outward(
            slice_.source_start, slice_.source_end, frame_duration, _SOURCE_GRID_ANCHOR
        )
        return _merge_frames((whole,))
    origin = reference.pcm_sample0_file_time
    frames: list[FrameInterval] = []
    for span in source_guard.protected:
        file_start = Fraction(span.start, reference.sample_rate) + origin
        file_end = Fraction(span.end, reference.sample_rate) + origin
        low = max(file_start, slice_.source_start)
        high = min(file_end, slice_.source_end)
        if high > low:
            frames.append(interval_outward(low, high, frame_duration, _SOURCE_GRID_ANCHOR))
    return _merge_frames(frames)


def _merge_frames(frames: Sequence[FrameInterval]) -> tuple[FrameInterval, ...]:
    """닿거나 겹치는 프레임 구간을 합치고 빈 것은 버린다."""
    merged: list[FrameInterval] = []
    for span in sorted(frames, key=lambda item: (item.start_frame, item.end_frame)):
        if span.end_frame <= span.start_frame:
            continue
        if merged and span.start_frame <= merged[-1].end_frame:
            previous = merged[-1]
            merged[-1] = FrameInterval(
                previous.start_frame, max(previous.end_frame, span.end_frame)
            )
        else:
            merged.append(span)
    return tuple(merged)


def _instance_review(source_guard: SourceGuard, slice_: LogicalSlice) -> tuple[str, ...]:
    """인스턴스 검토 — 소스의 keep_all·글자 없는 말·불가청 이유만 싣는다."""
    notes: list[str] = []
    if not slice_.audible:
        notes.append("불가청 슬라이스 — 제거 권한이 없어 슬라이스 전체를 보호한다")
    if source_guard.status == _STATUS_KEEP_ALL:
        notes.append("소스 가드가 keep_all이다 — 이 슬라이스 전체가 보호다")
    if source_guard.vad_without_text:
        notes.append("말이 있는데 쓸 글자이 없는 소스 구간이 있다 — 검토로 남긴다")
    return tuple(notes)


# ─────────────────────────────────────────────────────────────
#  연결 검증 — 위조 신원·모순된 짝은 호출 앞에서 닫는다
# ─────────────────────────────────────────────────────────────


def _require_source_linkage(trace: SourceTrace, reference: PcmReference) -> None:
    """트레이스와 참조는 같은 저장 PCM을 말해야 한다 — 신원·키·원점으로 증명한다."""
    if trace.source_id != reference.source_id:
        raise ValueError(
            f"트레이스와 참조의 source_id가 다르다: trace {trace.source_id!r},"
            f" reference {reference.source_id!r} — 위조 연결로는 가드를 만들지 않는다"
        )
    if trace.pcm_analysis_key != reference.analysis_key:
        raise ValueError(
            f"트레이스의 PCM 분석 키가 참조와 다르다: trace {trace.pcm_analysis_key!r},"
            f" reference {reference.analysis_key!r}"
        )
    if trace.pcm_sample0_file_time != reference.pcm_sample0_file_time:
        raise ValueError(
            f"트레이스의 원점이 참조와 다르다: trace {trace.pcm_sample0_file_time},"
            f" reference {reference.pcm_sample0_file_time}"
        )
    if _bad_count(reference.sample_rate) or reference.sample_rate < 1:
        raise ValueError(f"참조의 sample_rate가 1 이상 정수가 아니다: {reference.sample_rate!r}")
    if _bad_count(reference.sample_count) or reference.sample_count < 1:
        raise ValueError(f"참조의 sample_count가 1 이상 정수가 아니다: {reference.sample_count!r}")


def _require_vad_linkage(vad: VadResult, reference: PcmReference) -> None:
    """VAD 결과의 신원은 참조와 정확히 같아야 한다 — 남의 창은 위조다."""
    if vad.source_id != reference.source_id:
        raise ValueError(
            f"VAD 결과의 source_id가 참조와 다르다: vad {vad.source_id!r},"
            f" reference {reference.source_id!r} — 위조 연결로는 가드를 만들지 않는다"
        )


def _require_projection_linkage(
    source_guard: SourceGuard, reference: PcmReference, slices: Sequence[LogicalSlice]
) -> None:
    """투영은 가드를 만든 참조 그 자체만 받는다 — 신원·프레임 수가 증거다."""
    if source_guard.source_id != reference.source_id:
        raise ValueError(
            f"가드와 참조의 source_id가 다르다: guard {source_guard.source_id!r},"
            f" reference {reference.source_id!r} — 다른 참조의 가드로 투영하지 않는다"
        )
    if source_guard.total_samples != reference.sample_count:
        raise ValueError(
            f"가드의 total_samples가 참조의 sample_count와 다르다:"
            f" guard {source_guard.total_samples!r}, reference.sample_count"
            f"={reference.sample_count!r}"
        )
    if _bad_count(reference.sample_rate) or reference.sample_rate < 1:
        raise ValueError(f"참조의 sample_rate가 1 이상 정수가 아니다: {reference.sample_rate!r}")
    for slice_ in slices:
        if slice_.source_id != reference.source_id:
            raise ValueError(
                f"슬라이스의 source_id가 참조와 다르다: slice {slice_.slice_id!r}는"
                f" {slice_.source_id!r}인데 reference는 {reference.source_id!r}다"
            )


def _blocked_reviews(trace: SourceTrace, vad: VadResult) -> list[str]:
    """전체 보호로 닫는 실패 — 조용한 침묵이 아니라 이유 있는 검토로 남는다."""
    blocked: list[str] = []
    if trace.status == "error":
        blocked.append(f"전사 트레이스가 error다 — 전체를 보호한다: {trace.error}")
    if vad.status == "error":
        blocked.append(f"VAD 추론이 error다 — 전체를 보호한다: {vad.error}")
    elif not vad.windows:
        blocked.append("VAD 성공 결과에 창이 하나도 없다 — 전체를 보호한다")
    if trace.status != "error" and not trace.coverage:
        blocked.append("전사 덮개(coverage)가 비었다 — 전체를 보호한다")
    return blocked


def _keep_all(reference: PcmReference, reviews: list[str]) -> SourceGuard:
    """실패 닫힘 — 가청 소스 전체를 보호하고 이유를 검토에 남긴다."""
    total = reference.sample_count
    return SourceGuard(
        source_id=reference.source_id,
        protected=(SampleRange(0, total),),
        vad_without_text=(),
        protected_samples=total,
        total_samples=total,
        status=_STATUS_KEEP_ALL,
        review=tuple(reviews),
    )


def _require_ordered(name: str, ranges: tuple[SampleRange, ...], total: int) -> None:
    """기록의 구간들은 순서 있고 겹치지 않고 저장 범위 안에 있어야 한다."""
    previous_end = 0
    for span in ranges:
        if _bad_count(span.start) or _bad_count(span.end):
            raise ValueError(f"{name}의 좌표가 정수가 아니다: {span!r}")
        if span.start < previous_end or span.end <= span.start or span.end > total:
            raise ValueError(
                f"{name}가 순서·범위 계약을 어긴다: {span!r} (저장 {total}프레임,"
                f" 앞 구간 끝 {previous_end})"
            )
        previous_end = span.end


def _bad_count(value: object) -> bool:
    """bool은 int인 척한다 — 세는 수에서는 걸러낸다."""
    return isinstance(value, bool) or not isinstance(value, int)
