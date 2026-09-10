"""경계 전사 요청 계획 — 고정점 폐쇄와 자원 먼저 묶음 (P4-02 계약).

계획("Boundary transcripts")의 정본:

- **세그먼트가 다 들어 있으면 발표만 묶는다.** 큐가 가리키는 골격 세그먼트가
  슬라이스에 온전히 들어 있으면 조판만 묶이고 재요청은 없다. 세그먼트가 입력
  자름점에서 잘리면 그때만 경계 요청이 생긴다 — 빠진 말에 대한 추측 여유는 없다.
- **폐쇄는 고정점이다.** 겹치는 큐·세그먼트 의존을 범위가 더 자라지 않을 때까지
  반복해 흡수하고, 남긴 논리 슬라이스로 묶는다. 요청은 제거된 오디오를 절대
  담지 않는다.
- **시계는 원점을 정확히 한 번 뺀다.** 요청 경계는 파일 시계, 저장 PCM 환산은
  오디오 국소 시계다. 환산은 ceil-start/floor-end로 안쪽만 가고, 실측 프레임은
  다시 파일 시계로 정확히 돌아온다.
- **자원 먼저, 내용은 그 다음.** 자원 스코프 unavailable 기록이 있으면 그 자원의
  인스턴스마다 타입 있는 기록과 잠금만 남는다 — 같은 내용의 건강한 트레이스가
  있어도 넘어가지 않는다.

합성 XML·손 트레이스·손 참조만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, dataclass, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.models import Cue
from vlogsrt.project import boundary
from vlogsrt.project.boundary import (
    BoundaryPlan,
    BoundaryRequest,
    LogicalSlice,
    ProjectCue,
    UnavailableInstance,
    logical_slices,
    plan_boundaries,
)
from vlogsrt.project.captions import BOUNDARY_REQUIRED, SourceCaptionSet, project_source_captions
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.pcm import PcmReference, SampleRange
from vlogsrt.project.speech import (
    SourceAnalysis,
    SourceSegment,
    SourceTrace,
    UnavailableSourceAudio,
)

RATE = 16000
"""계약 주파수 — 손 참조가 주장하는 저장 PCM의 사실이다."""

SAMPLE_COUNT = 320000
"""손 참조의 프레임 수 — 환산 묶음이 우연히 잘리지 않을 만큼 크다."""


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 XML·손 미디어·손 트레이스·손 참조
# ─────────────────────────────────────────────────────────────


def _document(tmp_path: Path, assets: list[str], items: list[str]) -> ProjectDocument:
    fmt = (
        '<format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s"'
        ' width="1920" height="1080"/>'
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        + fmt
        + "\n    "
        + "\n    ".join(assets)
        + '\n  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="프로젝트">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n            "
        + "\n            ".join(items)
        + "\n          </spine>\n        </sequence>\n"
        "      </project>\n    </event>\n  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "project.fcpxml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return parse_project(path)


def _asset_xml(asset_id: str, *, has_audio: str = "1") -> str:
    return (
        f'<asset id="{asset_id}" name="{asset_id}.mp4" uid="AAAA" start="2500/25s"'
        f' duration="500/25s" hasAudio="{has_audio}" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file:///media/{asset_id}.mp4"/></asset>'
    )


def _clip_xml(ref: str, *, offset: str, start: str, duration: str = "50/25s") -> str:
    return (
        f'<asset-clip ref="{ref}" name="{ref}" offset="{offset}" start="{start}"'
        f' duration="{duration}"/>'
    )


def _media(
    document: ProjectDocument, xml_id: str, *, content_id: str | None = None
) -> ResolvedMedia:
    resource = next(item for item in document.resources if item.xml_id == xml_id)
    original = Path(f"/media/{xml_id}.mp4")
    return ResolvedMedia(
        resource_scoped_id=resource.scoped_id,
        asset_content_id=content_id or f"acid-{xml_id}",
        original=original,
        image_source=original,
        original_sha256=f"{xml_id}-sha",
        has_audio=resource.has_audio,
        used_proxy=False,
    )


@dataclass
class _CueSpec:
    start: float
    end: float
    seg_ids: tuple[int, ...] = (0,)


@dataclass
class _SegSpec:
    start: float
    end: float


def _trace(
    source_id: str,
    cues: list[_CueSpec],
    segs: list[_SegSpec],
    *,
    origin: Fraction = Fraction(0),
    status: str = "success",
) -> SourceTrace:
    """손 트레이스 — 큐·골격 시각을 오디오 국소 초로 직접 준다."""
    published = status in ("success", "empty")
    return SourceTrace(
        variant="source_trace",
        source_id=source_id,
        original_stem="demo",
        analysis_key=f"key-{source_id}",
        pcm_analysis_key=f"pcm-{source_id}",
        pcm_sample0_file_time=origin,
        status=status,  # type: ignore[arg-type]
        draft=(),
        settled_spine=tuple(
            SourceSegment(
                segment_id=f"{source_id}-seg-{position}",
                audio_start=Fraction(str(seg.start)),
                audio_end=Fraction(str(seg.end)),
                file_start=Fraction(str(seg.start)) + origin,
                file_end=Fraction(str(seg.end)) + origin,
                text=f"골격 {position}",
                compression_ratio=1.0,
                avg_logprob=-0.1,
                no_speech_prob=0.0,
            )
            for position, seg in enumerate(segs)
        ),
        repairs=(),
        cues=tuple(
            Cue(
                start=cue.start,
                end=cue.end,
                lines=("자막",),
                seg_ids=cue.seg_ids,
                flags=(),
                index=position,
            )
            for position, cue in enumerate(cues, start=1)
        ),
        coverage=(),
        model="fixture-model",
        config_sha256="c" * 64,
        source_srt_relative_path=f"work/{source_id}/demo.SOURCE.srt" if published else None,
        source_srt_sha256="d" * 64 if published else None,
        error="준비된 실패" if status == "error" else None,
    )


def _reference(trace: SourceTrace, *, analysis_key: str | None = None) -> PcmReference:
    """손 참조 — 트레이스의 PCM 신원·원점과 짝을 이룬다."""
    return PcmReference(
        source_id=trace.source_id,
        original_stem="demo",
        original_sha256="demo-sha",
        path=Path("/cache/demo.pcm"),
        pcm_sha256="e" * 64,
        analysis_key=analysis_key or trace.pcm_analysis_key,
        pcm_sample0_file_time=trace.pcm_sample0_file_time,
        sample_rate=RATE,
        channels=1,
        sample_count=SAMPLE_COUNT,
    )


def _unavailable(
    document: ProjectDocument,
    xml_id: str,
    *,
    source_id: str | None = None,
    status: str = "no_audio",
    reason_code: str | None = None,
) -> UnavailableSourceAudio:
    resource = next(item for item in document.resources if item.xml_id == xml_id)
    media = _media(document, xml_id)
    return UnavailableSourceAudio(
        variant="unavailable_source_audio",
        resource_scoped_id=resource.scoped_id,
        source_id=source_id or media.asset_content_id,
        original_sha256=media.original_sha256,
        status=status,  # type: ignore[arg-type]
        reason_code=reason_code  # type: ignore[arg-type]
        or ("declared_no_audio" if status == "no_audio" else "stream_unavailable"),
        reason="테스트용 기록",
    )


def _assemble(
    document: ProjectDocument,
    analyses: list[SourceAnalysis],
    *,
    contents: dict[str, str] | None = None,
) -> tuple[tuple[LogicalSlice, ...], SourceCaptionSet, list[SourceTrace]]:
    """문서 → 미디어 → P3 초안 → 논리 슬라이스까지 실제 조립으로 잇는다."""
    media = [
        _media(document, resource.xml_id, content_id=(contents or {}).get(resource.xml_id))
        for resource in document.resources
    ]
    source = project_source_captions(document, media, analyses)
    slices = logical_slices(document, media)
    traces = [item for item in analyses if isinstance(item, SourceTrace)]
    return slices, source, traces


def _single(
    tmp_path: Path, cues: list[_CueSpec], segs: list[_SegSpec], **trace_kwargs: object
) -> tuple[
    ProjectDocument,
    tuple[LogicalSlice, ...],
    SourceCaptionSet,
    list[SourceTrace],
    list[PcmReference],
]:
    """기본 세계 — 자원 r2 하나, 클립 하나(파일 [4,6)), 트레이스 하나."""
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
    )
    slices, source, traces = _assemble(
        document,
        [_trace("acid-r2", cues, segs, **trace_kwargs)],  # type: ignore[arg-type]
    )
    return document, slices, source, traces, [_reference(traces[0])]


def _clips(document: ProjectDocument) -> tuple[ClipInstance, ...]:
    return tuple(item for item in document.storyline if isinstance(item, ClipInstance))


# ─────────────────────────────────────────────────────────────
#  변환 — 배치 큐를 논리 슬라이스 큐로
# ─────────────────────────────────────────────────────────────


def test_padding_only_clamp_converts_cues_without_request(tmp_path: Path) -> None:
    """세그먼트가 다 들어 있으면 발표만 묶인다 — 재요청도 조판 재실행도 없다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5), _CueSpec(5.8, 6.2, seg_ids=(1,))],
        segs=[_SegSpec(4.4, 5.6), _SegSpec(5.75, 5.95)],
    )
    (clip,) = _clips(document)
    resource = document.resources[0]
    plan = plan_boundaries(slices, source, traces, references)
    assert len(plan.clamped_cues) == 2
    cue_inside, cue_overhanging = plan.clamped_cues
    assert isinstance(cue_inside, ProjectCue) and isinstance(cue_overhanging, ProjectCue)
    # 안쪽 큐 — 묶임도 없다
    assert cue_inside.timeline_start == Fraction(1, 2)
    assert cue_inside.timeline_end == Fraction(3, 2)
    assert cue_inside.flags == ()
    # 저변으로 삐져나온 큐 — 발표만 묶이고 원본 소스 기록은 그대로다
    assert cue_overhanging.source_start == Fraction(29, 5)
    assert cue_overhanging.source_end == Fraction(31, 5)
    assert cue_overhanging.timeline_start == Fraction(9, 5)
    assert cue_overhanging.timeline_end == Fraction(2)
    assert cue_overhanging.flags == (BOUNDARY_REQUIRED,)
    # XML 공식이 묶인 발표 끝점에서도 정확히 왕복한다
    elapsed = clip.offset - document.sequence_tc_start + resource.start - clip.start
    assert elapsed + cue_overhanging.source_start == Fraction(9, 5)
    # 세그먼트가 온전하므로 요청도 unavailable도 없다
    assert plan.requests == ()
    assert plan.unavailable_instances == ()
    assert plan.locked_instance_ids == frozenset()


def test_clamped_cue_carries_slice_identity_and_source_provenance(tmp_path: Path) -> None:
    """큐는 슬라이스 신원과 인스턴스, 출처 'source'를 그대로 싣는다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    (slice_,) = slices
    (clip,) = _clips(document)
    plan = plan_boundaries(slices, source, traces, references)
    (cue,) = plan.clamped_cues
    assert isinstance(cue, ProjectCue)
    assert cue.slice_id == slice_.slice_id
    assert cue.instance_ids == (clip.instance_id,)
    assert cue.source_id == slice_.source_id == "acid-r2"
    assert cue.provenance == "source"
    assert cue.text == "자막"


# ─────────────────────────────────────────────────────────────
#  절단 — 세그먼트가 잘렸을 때만 요청이 생긴다
# ─────────────────────────────────────────────────────────────


def test_leading_truncation_starts_request_with_clamped_span(tmp_path: Path) -> None:
    """들머리 잘림 — 폐쇄 범위는 슬라이스 시작으로 묶이고 프레임은 안쪽으로 환산된다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(3.6, 4.4), _CueSpec(5.0, 5.5, seg_ids=(1,))],
        segs=[_SegSpec(3.5, 4.45), _SegSpec(4.95, 5.6)],
    )
    (slice_,) = slices
    (clip,) = _clips(document)
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    assert isinstance(request, BoundaryRequest)
    assert request.slice_id == slice_.slice_id
    assert request.instance_ids == (clip.instance_id,)
    assert request.source_id == "acid-r2"
    assert request.pcm_analysis_key == "pcm-acid-r2"
    assert request.source_start == Fraction(4)
    assert request.source_end == Fraction("4.45") == Fraction(89, 20)
    assert request.sample_range == SampleRange(64000, 71200)
    assert request.source_segment_ids == ("acid-r2-seg-0",)
    assert plan.locked_instance_ids == frozenset({clip.instance_id})
    (clamped,) = (cue for cue in plan.clamped_cues if BOUNDARY_REQUIRED in cue.flags)
    assert clamped.timeline_start == Fraction(0)
    assert clamped.timeline_end == Fraction(2, 5)


def test_trailing_truncation_starts_request(tmp_path: Path) -> None:
    """끝머리 잘림 — 폐쇄 범위는 슬라이스 끝으로 묶인다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(5.7, 6.3)],
        segs=[_SegSpec(5.6, 6.35)],
    )
    (clip,) = _clips(document)
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    assert request.source_start == Fraction("5.6") == Fraction(28, 5)
    assert request.source_end == Fraction(6)
    assert request.sample_range == SampleRange(89600, 96000)
    assert plan.locked_instance_ids == frozenset({clip.instance_id})


def test_transitive_overlap_closure_grows_to_fixed_point(tmp_path: Path) -> None:
    """폐쇄는 겹치는 의존을 되풀이해 흡수한다 — 닿지 않는 의존은 그대로 둔다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[
            _CueSpec(3.6, 4.2),
            _CueSpec(4.3, 4.8, seg_ids=(1,)),
            _CueSpec(5.0, 5.5, seg_ids=(2,)),
        ],
        segs=[_SegSpec(3.5, 4.25), _SegSpec(4.2, 4.9), _SegSpec(4.95, 5.6)],
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    # 씨앗 [3.5, 4.25]가 seg1 [4.2, 4.9]를 흡수해 [3.5, 4.9]로 자랐고
    # seg2 [4.95, 5.6]는 닿지 않아 그대로 남는다 — 고정점에서 멈춘다
    assert request.source_start == Fraction(4)
    assert request.source_end == Fraction("4.9") == Fraction(49, 10)
    assert request.sample_range == SampleRange(64000, 78400)
    assert request.source_segment_ids == ("acid-r2-seg-0", "acid-r2-seg-1")


def test_closure_is_bounded_by_retained_slice_on_both_ends(tmp_path: Path) -> None:
    """폐쇄가 양 끝을 넘어 자라도 요청은 남긴 슬라이스 안에만 있다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(5.0, 5.5)],
        segs=[_SegSpec(3.5, 6.5)],
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    (slice_,) = slices
    assert request.source_start == slice_.source_start == Fraction(4)
    assert request.source_end == slice_.source_end == Fraction(6)
    assert request.sample_range == SampleRange(64000, 96000)


# ─────────────────────────────────────────────────────────────
#  병합 — 같은 슬라이스의 겹친·이어진 범위만 하나가 된다
# ─────────────────────────────────────────────────────────────


def test_touching_spans_of_same_slice_merge_into_one_request(tmp_path: Path) -> None:
    """닿은 두 범위는 호환 가능하다 — 하나의 요청으로 합쳐진다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[
            _CueSpec(3.6, 4.4),
            _CueSpec(4.5, 5.0, seg_ids=(1,)),
            _CueSpec(5.5, 6.2, seg_ids=(2,)),
        ],
        segs=[_SegSpec(3.5, 4.5), _SegSpec(4.5, 5.4), _SegSpec(4.5, 6.3)],
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    assert request.source_start == Fraction(4)
    assert request.source_end == Fraction(6)
    assert request.sample_range == SampleRange(64000, 96000)
    assert request.source_segment_ids == (
        "acid-r2-seg-0",
        "acid-r2-seg-1",
        "acid-r2-seg-2",
    )


def test_same_instance_touching_spans_still_merge_and_lock(tmp_path: Path) -> None:
    """참여 칼날이 같은 닿은 범위는 여전히 호환된다 — 하나의 요청에 잠금이 그 칼날을 덮는다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[
            _CueSpec(3.6, 4.4),
            _CueSpec(4.5, 5.0, seg_ids=(1,)),
            _CueSpec(5.5, 6.2, seg_ids=(2,)),
        ],
        segs=[_SegSpec(3.5, 4.5), _SegSpec(4.5, 5.4), _SegSpec(4.5, 6.3)],
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    (clip,) = _clips(document)
    assert request.instance_ids == (clip.instance_id,)
    assert request.source_start == Fraction(4)
    assert request.source_end == Fraction(6)
    assert plan.locked_instance_ids == frozenset({clip.instance_id})


def test_touching_spans_on_different_blades_stay_separate_requests(tmp_path: Path) -> None:
    """다른 칼날에 앉은 닿은 범위는 합치지 않는다 — 각자의 단일 인스턴스를 이름하는
    두 요청으로 남고, 병합 호환(자원·슬라이스·인스턴스·가청)은 칼날부터 본다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r2", offset="50/25s", start="2650/25s"),
        ],
    )
    slices, source, traces = _assemble(
        document,
        [
            _trace(
                "acid-r2",
                [
                    _CueSpec(3.6, 4.4),
                    _CueSpec(5.5, 5.95, seg_ids=(1,)),
                    _CueSpec(6.05, 7.55, seg_ids=(2,)),
                    _CueSpec(7.6, 8.2, seg_ids=(3,)),
                ],
                [
                    _SegSpec(3.5, 4.45),
                    _SegSpec(4.4, 6.0),
                    _SegSpec(6.0, 7.6),
                    _SegSpec(7.5, 8.3),
                ],
            )
        ],
    )
    references = [_reference(traces[0])]
    (slice_,) = slices
    first_clip, second_clip = _clips(document)
    # 이어진 두 칼날은 하나의 슬라이스 [4, 8)다 — 폐쇄는 6.0에서 닿아 멈춘다
    assert slice_.instance_ids == (first_clip.instance_id, second_clip.instance_id)
    plan = plan_boundaries(slices, source, traces, references)
    assert len(plan.requests) == 2
    first, second = plan.requests
    assert first.instance_ids == (first_clip.instance_id,)
    assert second.instance_ids == (second_clip.instance_id,)
    assert (first.source_start, first.source_end) == (Fraction(4), Fraction(6))
    assert (second.source_start, second.source_end) == (Fraction(6), Fraction(8))
    assert first.sample_range == SampleRange(64000, 96000)
    assert second.sample_range == SampleRange(96000, 128000)
    assert first.source_segment_ids == ("acid-r2-seg-0", "acid-r2-seg-1")
    assert second.source_segment_ids == ("acid-r2-seg-2", "acid-r2-seg-3")
    assert first.request_id != second.request_id
    # 두 요청 모두 잠긴다 — 합치지 않은 만큼 잠금은 두 칼날을 다 덮는다
    assert plan.locked_instance_ids == frozenset({first_clip.instance_id, second_clip.instance_id})
    assert len(plan.clamped_cues) == 4


def test_disjoint_spans_of_same_slice_stay_separate_requests(tmp_path: Path) -> None:
    """갈라진 범위는 호환되지 않는다 — 두 요청으로 남는다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[
            _CueSpec(3.6, 4.4),
            _CueSpec(4.5, 4.9, seg_ids=(1,)),
            _CueSpec(5.5, 6.2, seg_ids=(2,)),
        ],
        segs=[_SegSpec(3.5, 4.4), _SegSpec(4.45, 4.95), _SegSpec(5.0, 6.3)],
    )
    plan = plan_boundaries(slices, source, traces, references)
    first, second = plan.requests
    assert first.source_start == Fraction(4)
    assert first.source_end == Fraction("4.4")
    assert first.sample_range == SampleRange(64000, 70400)
    assert second.source_start == Fraction(5)
    assert second.source_end == Fraction(6)
    assert second.sample_range == SampleRange(80000, 96000)
    assert first.request_id != second.request_id


def test_repeated_instance_request_names_only_that_slice(tmp_path: Path) -> None:
    """반복 인스턴스 — 요청은 저 슬라이스의 인스턴스만 이름한다, 합치지도 않는다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r2", offset="100/25s", start="2600/25s"),
        ],
    )
    slices, source, traces = _assemble(
        document, [_trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)])]
    )
    references = [_reference(traces[0])]
    first_clip, second_clip = _clips(document)
    first_slice, second_slice = slices
    plan = plan_boundaries(slices, source, traces, references)
    assert len(plan.requests) == 2
    first_request, second_request = plan.requests
    assert first_request.instance_ids == (first_clip.instance_id,)
    assert second_request.instance_ids == (second_clip.instance_id,)
    assert first_request.slice_id == first_slice.slice_id
    assert second_request.slice_id == second_slice.slice_id
    assert first_request.request_id != second_request.request_id
    # 두 요청 모두 같은 자원·PCM 신원과 경계를 가진다 — 슬라이스만 다르다
    for request in plan.requests:
        assert request.source_id == "acid-r2"
        assert request.source_start == Fraction(4)
        assert request.source_end == Fraction(89, 20)
        assert request.sample_range == SampleRange(64000, 71200)
    # 배치 큐도 인스턴스마다 하나씩 산다 — 지워지지 않는다
    assert len(plan.clamped_cues) == 2
    assert {cue.instance_ids[0] for cue in plan.clamped_cues} == {
        first_clip.instance_id,
        second_clip.instance_id,
    }
    assert len({cue.cue_id for cue in plan.clamped_cues}) == 1
    assert plan.locked_instance_ids == frozenset({first_clip.instance_id, second_clip.instance_id})


def test_continuous_blades_cause_zero_requests_across_blade_edges(tmp_path: Path) -> None:
    """이어진 칼날은 슬라이스 하나다 — 칼날 경계를 넘는 큐가 잘림이 아니다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r2", offset="50/25s", start="2650/25s"),
            _clip_xml("r2", offset="100/25s", start="2700/25s"),
        ],
    )
    slices, source, traces = _assemble(
        document, [_trace("acid-r2", [_CueSpec(5.8, 6.3)], [_SegSpec(5.75, 6.35)])]
    )
    references = [_reference(traces[0])]
    (slice_,) = slices
    assert len(slice_.instance_ids) == 3
    plan = plan_boundaries(slices, source, traces, references)
    # 세그먼트 [5.75, 6.35]가 칼날 경계 6.0을 넘지만 슬라이스 [4, 10) 안에 온전하다
    assert plan.requests == ()
    assert plan.locked_instance_ids == frozenset()
    # 배치 큐는 인접 인스턴스마다 저변으로 묶여 그대로 산다
    assert len(plan.clamped_cues) == 2
    assert all(BOUNDARY_REQUIRED in cue.flags for cue in plan.clamped_cues)


# ─────────────────────────────────────────────────────────────
#  시계 — PCM 원점 환산과 실측 경계
# ─────────────────────────────────────────────────────────────


def test_positive_pcm_origin_snaps_exactly_and_roundtrips(tmp_path: Path) -> None:
    """양의 원점(64ms) — 파일 시각에서 원점을 정확히 한 번 빼고 환산한다."""
    origin = Fraction(8, 125)
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(3.9, 4.3)],
        segs=[_SegSpec(3.8, 4.35)],
        origin=origin,
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    assert request.source_start == Fraction(4)
    assert request.source_end == Fraction("4.414") == Fraction(2207, 500)
    assert request.sample_range == SampleRange(62976, 69600)
    # 실측 프레임을 다시 파일 시계로 옮기면 요청 경계와 정확히 같다
    measured_start = Fraction(request.sample_range.start, RATE) + origin
    measured_end = Fraction(request.sample_range.end, RATE) + origin
    assert measured_start == request.source_start
    assert measured_end == request.source_end


def test_negative_pcm_origin_snaps_exactly_and_roundtrips(tmp_path: Path) -> None:
    """음의 원점(−50ms) — 국소 시각은 파일 시각보다 뒤로 간다."""
    origin = Fraction(-1, 20)
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5), _CueSpec(5.95, 6.15, seg_ids=(1,))],
        segs=[_SegSpec(4.45, 5.55), _SegSpec(5.9, 6.2)],
        origin=origin,
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    assert request.source_start == Fraction("5.85") == Fraction(117, 20)
    assert request.source_end == Fraction(6)
    assert request.sample_range == SampleRange(94400, 96800)
    measured_start = Fraction(request.sample_range.start, RATE) + origin
    measured_end = Fraction(request.sample_range.end, RATE) + origin
    assert measured_start == request.source_start
    assert measured_end == request.source_end
    # 국소 환산: 파일 5.85 − 원점(−0.05) = 5.9초 → 정확 프레임 — 원점을 두 번 더하지 않는다
    assert Fraction(request.sample_range.start, RATE) == Fraction("5.9")


def test_requested_bounds_stay_distinct_from_measured_sample_bounds(tmp_path: Path) -> None:
    """요청 경계와 실측 프레임 경계는 다른 기록이다 — 환산은 안쪽으로만 간다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(3.6, 4.4)],
        segs=[_SegSpec(3.59995, 4.40005)],
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    assert request.source_end == Fraction("4.40005") == Fraction(88001, 20000)
    assert request.sample_range == SampleRange(64000, 70400)
    assert Fraction(request.sample_range.start, RATE) == request.source_start
    assert Fraction(request.sample_range.end, RATE) == Fraction("4.4") < request.source_end


def test_span_thinner_than_one_sample_is_rejected(tmp_path: Path) -> None:
    """환산 뒤 프레임이 없는 범위는 요청이 될 수 없다 — 여유를 더해 살리지 않는다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(5.99997, 5.99999)],
        segs=[_SegSpec(5.99996, 6.00004)],
    )
    with pytest.raises(ValueError, match="환산 뒤 구간이 비었다"):
        plan_boundaries(slices, source, traces, references)


# ─────────────────────────────────────────────────────────────
#  묶음 — 트레이스·참조 연결은 신원으로만
# ─────────────────────────────────────────────────────────────


def test_shuffled_trace_and_reference_orders_give_identical_plan(tmp_path: Path) -> None:
    """순서를 섞어도 계획은 같다 — 연결은 위치(zip)가 아니라 신원으로 한다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r3", offset="100/25s", start="2600/25s"),
        ],
    )
    slices, source, traces = _assemble(
        document,
        [
            _trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)]),
            _trace("acid-r3", [_CueSpec(4.5, 5.5)], [_SegSpec(4.4, 5.6)]),
        ],
    )
    references = [_reference(trace) for trace in traces]
    forward = plan_boundaries(slices, source, traces, references)
    backward = plan_boundaries(slices, source, list(reversed(traces)), list(reversed(references)))
    assert forward == backward
    assert len(forward.requests) == 1
    assert forward.requests[0].source_id == "acid-r2"


def test_missing_trace_for_audible_slice_is_rejected(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    with pytest.raises(ValueError, match="전사 트레이스가 없다"):
        plan_boundaries(slices, source, [], references)


def test_duplicate_traces_are_rejected(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    with pytest.raises(ValueError, match="트레이스가 둘 이상이다"):
        plan_boundaries(slices, source, [*traces, *traces], references)


def test_missing_reference_is_rejected(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    with pytest.raises(ValueError, match="PCM 참조가 없다"):
        plan_boundaries(slices, source, traces, [])


def test_duplicate_references_are_rejected(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    with pytest.raises(ValueError, match="PCM 참조가 둘 이상이다"):
        plan_boundaries(slices, source, traces, [*references, *references])


def test_reference_analysis_key_mismatch_is_rejected(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    forged = replace(references[0], analysis_key="pcm-다른-분석")
    with pytest.raises(ValueError, match="분석 키가 트레이스와 다르다"):
        plan_boundaries(slices, source, traces, [forged])


def test_reference_origin_mismatch_is_rejected(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    forged = replace(references[0], pcm_sample0_file_time=Fraction(-1, 20))
    with pytest.raises(ValueError, match="원점이 트레이스와 다르다"):
        plan_boundaries(slices, source, traces, [forged])


def test_forged_placed_cue_source_mismatch_is_rejected(tmp_path: Path) -> None:
    """위조 배치 큐 — 소스 신원이 슬라이스·소스 큐와 다르면 ProjectCue가 하나의
    소스를 말하고 BoundaryRequest가 다른 소스를 말하는 계획이 나간다 — 그
    자리에서 이름 붙은 거부로 닫는다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(3.6, 4.4)],
        segs=[_SegSpec(3.5, 4.45)],
    )
    (placed,) = source.placed_cues
    forged = replace(source, placed_cues=(replace(placed, source_id="acid-위조"),))
    with pytest.raises(ValueError, match="배치 큐의 소스 신원이 슬라이스와 다르다"):
        plan_boundaries(slices, forged, traces, references)


def test_forged_source_cue_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    """위조 소스 큐 — 배치 큐가 가리키는 소스 큐의 신원이 어긋나도 조용히
    지나가지 않는다 — 내구 연결은 배치·소스 큐·슬라이스·트레이스가 정확히
    같을 때만 성립한다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(3.6, 4.4)],
        segs=[_SegSpec(3.5, 4.45)],
    )
    (source_cue,) = source.source_cues
    forged = replace(
        source,
        source_cues=(replace(source_cue, source_id="acid-다른-소스"),),
    )
    with pytest.raises(ValueError, match="소스 큐의 신원이 배치 큐와 다르다"):
        plan_boundaries(slices, forged, traces, references)


# ─────────────────────────────────────────────────────────────
#  unavailable — 자원 먼저, 내용은 그 다음
# ─────────────────────────────────────────────────────────────


def test_declared_no_audio_resource_yields_typed_instances_and_locks(tmp_path: Path) -> None:
    """선언된 무음 — 인스턴스마다 타입 있는 기록이 순서대로 남고 잠긴다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2", has_audio="0")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r2", offset="100/25s", start="2600/25s"),
        ],
    )
    record = _unavailable(document, "r2")
    slices, source, traces = _assemble(document, [record])
    first_clip, second_clip = _clips(document)
    plan = plan_boundaries(slices, source, traces, [])
    assert plan.unavailable_instances == (
        UnavailableInstance(
            resource_scoped_id=record.resource_scoped_id,
            instance_id=first_clip.instance_id,
            reason_code="declared_no_audio",
        ),
        UnavailableInstance(
            resource_scoped_id=record.resource_scoped_id,
            instance_id=second_clip.instance_id,
            reason_code="declared_no_audio",
        ),
    )
    assert plan.clamped_cues == ()
    assert plan.requests == ()
    assert plan.locked_instance_ids == frozenset({first_clip.instance_id, second_clip.instance_id})


@pytest.mark.parametrize("reason_code", ["stream_unavailable", "pcm_unavailable"])
def test_retryable_pre_pcm_unavailability_is_typed_and_locks(
    tmp_path: Path, reason_code: str
) -> None:
    """되풀이 가능한 pre-PCM 실패 — 다른 초안을 지우지 않고 타입 기록과 잠금만 남긴다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
    )
    record = _unavailable(document, "r2", status="error", reason_code=reason_code)
    slices, source, traces = _assemble(document, [record])
    (clip,) = _clips(document)
    plan = plan_boundaries(slices, source, traces, [])
    assert plan.unavailable_instances == (
        UnavailableInstance(
            resource_scoped_id=record.resource_scoped_id,
            instance_id=clip.instance_id,
            reason_code=reason_code,
        ),
    )
    assert plan.clamped_cues == ()
    assert plan.requests == ()
    assert plan.locked_instance_ids == frozenset({clip.instance_id})


def test_equal_content_unavailable_wins_over_healthy_trace_in_both_orders(tmp_path: Path) -> None:
    """같은 내용이라 자원 신원이 다르면 서로를 지우지 않는다 — 양쪽 순서 모두."""

    def build(clip_order: list[str]) -> BoundaryPlan:
        document = _document(
            tmp_path / f"world-{clip_order[0]}",
            [_asset_xml("r2"), _asset_xml("r3")],
            [
                _clip_xml(clip_order[0], offset="0s", start="2600/25s"),
                _clip_xml(clip_order[1], offset="100/25s", start="2600/25s"),
            ],
        )
        analyses: list[SourceAnalysis] = [
            _unavailable(document, "r2", source_id="same-bytes", status="error"),
            _trace("same-bytes", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)]),
        ]
        slices, source, traces = _assemble(
            document, analyses, contents={"r2": "same-bytes", "r3": "same-bytes"}
        )
        return plan_boundaries(slices, source, traces, [_reference(traces[0])])

    healthy_first = build(["r3", "r2"])
    healthy_second = build(["r2", "r3"])
    for plan in (healthy_first, healthy_second):
        # unavailable 자원의 인스턴스는 타입 기록과 잠금만 남는다
        (record,) = plan.unavailable_instances
        assert record.reason_code == "stream_unavailable"
        # 같은 내용의 건강한 자원은 요청과 큐를 그대로 받는다 — 억누르지 않는다
        (request,) = plan.requests
        assert request.source_id == "same-bytes"
        assert request.source_start == Fraction(4)
        assert request.source_end == Fraction(89, 20)
        assert len(plan.clamped_cues) == 1
        assert plan.locked_instance_ids == frozenset({record.instance_id, request.instance_ids[0]})


def test_duplicate_unavailable_records_are_rejected(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
    )
    record = _unavailable(document, "r2", status="error")
    slices, source, traces = _assemble(document, [record])
    duplicated = replace(source, unavailable_audio=(record, record))
    with pytest.raises(ValueError, match="unavailable 기록이 둘 이상이다"):
        plan_boundaries(slices, duplicated, traces, [])


def test_unavailable_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
    )
    record = replace(_unavailable(document, "r2", status="error"), source_id="acid-다른-소스")
    slices, source, traces = _assemble(document, [_unavailable(document, "r2", status="error")])
    forged = replace(source, unavailable_audio=(record,))
    with pytest.raises(ValueError, match="소스 신원이 슬라이스와 다르다"):
        plan_boundaries(slices, forged, traces, [])


def test_declared_no_audio_against_audible_slice_is_rejected(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(4.5, 5.5)],
        segs=[_SegSpec(4.4, 5.6)],
    )
    record = _unavailable(document, "r2")  # 가청 자원에 선언된 무음 — 모순이다
    forged = replace(source, unavailable_audio=(record,))
    with pytest.raises(ValueError, match="가청 슬라이스에 선언된 무음"):
        plan_boundaries(slices, forged, traces, references)


def test_unknown_reason_code_record_cannot_be_forged(tmp_path: Path) -> None:
    """모르는 사유 코드는 기록 자체가 거부한다 — 종이 문장이 아니라 타입이다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
    )
    with pytest.raises(ValueError, match="알 수 없는 사유 코드다"):
        _unavailable(document, "r2", status="error", reason_code="gpu_on_fire")


# ─────────────────────────────────────────────────────────────
#  error 트레이스 — post-PCM 실패는 unavailable이 아니다
# ─────────────────────────────────────────────────────────────


def test_post_pcm_error_trace_locks_without_becoming_unavailable(tmp_path: Path) -> None:
    """error 트레이스는 참조 신원을 가진 진짜 트레이스다 — 타입 강등이 없다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[],
        segs=[],
        status="error",
    )
    (clip,) = _clips(document)
    plan = plan_boundaries(slices, source, traces, references)
    assert plan.requests == ()
    assert plan.unavailable_instances == ()
    assert plan.clamped_cues == ()
    assert plan.locked_instance_ids == frozenset({clip.instance_id})


def test_error_trace_still_requires_reference_linkage(tmp_path: Path) -> None:
    """error 트레이스도 참조 증명은 살아 있다 — 묶음이 느슨해지지 않는다."""
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[],
        segs=[],
        status="error",
    )
    with pytest.raises(ValueError, match="PCM 참조가 없다"):
        plan_boundaries(slices, source, traces, [])


# ─────────────────────────────────────────────────────────────
#  규율 — 결정성·불변·순수성
# ─────────────────────────────────────────────────────────────


def test_plan_is_idempotent(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(3.6, 4.4), _CueSpec(5.5, 6.2, seg_ids=(1,))],
        segs=[_SegSpec(3.5, 4.45), _SegSpec(5.4, 6.25)],
    )
    first = plan_boundaries(slices, source, traces, references)
    second = plan_boundaries(slices, source, traces, references)
    assert first == second
    assert [item.request_id for item in first.requests] == [
        item.request_id for item in second.requests
    ]


def test_records_are_frozen(tmp_path: Path) -> None:
    document, slices, source, traces, references = _single(
        tmp_path,
        cues=[_CueSpec(3.6, 4.4)],
        segs=[_SegSpec(3.5, 4.45)],
    )
    plan = plan_boundaries(slices, source, traces, references)
    (request,) = plan.requests
    (cue,) = plan.clamped_cues
    assert isinstance(plan, BoundaryPlan)
    with pytest.raises(FrozenInstanceError):
        plan.requests = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        cue.text = "고침"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        request.source_start = Fraction(0)  # type: ignore[misc]


def test_plan_is_pure_request_planning() -> None:
    """계획은 네 입력만 받고 엔진·조판 계층을 모른다."""
    assert tuple(inspect.signature(plan_boundaries).parameters) == (
        "slices",
        "source",
        "traces",
        "references",
    )
    module_source = inspect.getsource(boundary)
    for forbidden in ("transcribe", "repair", "refine", "output", "layout", "verify"):
        assert f"import {forbidden}" not in module_source
        assert f"from vlogsrt import {forbidden}" not in module_source
        assert f"vlogsrt.{forbidden}" not in module_source
