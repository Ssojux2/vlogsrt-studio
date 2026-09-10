"""경계 전사 결과를 슬라이스·인스턴스·소스 구간으로 갈아끼우는 투영 (P4-04 계약).

계획("Boundary transcripts" ＋ P4 인수)의 정본:

- **갈아끼우기는 정확히 (슬라이스, 인스턴스, 소스 구간) 스코프다.** 성공한 경계
  기록의 이미 정제된 큐가 그 스코프 안의 소스 큐를 **대체**한다 — 둘을 함께
  내지 않고, 어느 쪽도 다시 정제·조판하지 않는다. 같은 소스가 다른 인스턴스에
  반복되어도 형제 인스턴스는 손대지 않는다.
- **연결은 신원으로만 한다.** 요청·경계 기록·소스 트레이스를 요청/소스/PCM 키로
  묶고 빠짐·중복·모순은 닫는다. 순서는 재료가 아니다.
- **타임라인 사상은 P4 공식 그대로다.** 경계 큐의 디코드 국소 시각에 원점을
  정확히 한 번 더해 파일 시계를 만들고, 슬라이스의 타임라인 이동량은 묶인 큐의
  두 시계에서 정확히 유도한다. 출력 큐는 순서 있고 겹치지 않으며 길이가 양수다.
- **빈·실패 경계는 조용한 성공이 아니다.** 묶인 원본 글자를 그대로 두고
  `boundary_unresolved` 깃발과 요청 신원을 검토에 남긴다 — 사람이 풀 때까지 초안이다.
- **밀리초 표현 가능성은 교체 뒤에 다시 잰다.** half-up 뒤 0이 되는 큐는 정확
  큐와 깃발·미해결 신원을 남기고 SRT에서 그 장만 뺀다. 성공한 경계가 옛 묶인
  큐를 대체하면 그 미해결 큐 신원만 내려가고, 상류 SOURCE 기록은 그대로다.
- **잠금·부재는 계획의 것을 그대로 옮긴다.** unavailable 기록은 인스턴스가 잠겼는지만
  검사하고 바이트 그대로 순서대로 내놓는다 — 선언된 무음은 완결이고 되풀이
  가능한 실패만 최종 수락을 막는다.

합성 XML·손 트레이스·손 경계 기록만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.models import Cue
from vlogsrt.project import boundary
from vlogsrt.project.boundary import (
    BOUNDARY_UNRESOLVED,
    RETRYABLE_UNAVAILABLE_REASONS,
    BoundaryPlan,
    BoundaryProjection,
    BoundaryTranscript,
    LogicalSlice,
    ProjectCue,
    UnavailableInstance,
    logical_slices,
    plan_boundaries,
    project_boundaries,
)
from vlogsrt.project.captions import (
    BOUNDARY_REQUIRED,
    SUBMS_COLLAPSED,
    SourceCaptionSet,
    SourceCue,
    project_source_captions,
)
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

BOUNDARY_TEXT = "경계 복원... 잘린 마디입니다"
"""경계 큐의 글자 — 조판이 다시 손대면 `...`가 `…`로 바뀌므로 증거가 된다."""


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 XML·손 미디어·손 트레이스·손 경계 기록
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
    start: float | Fraction
    end: float | Fraction
    seg_ids: tuple[int, ...] = (0,)


@dataclass
class _SegSpec:
    start: float | Fraction
    end: float | Fraction


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
                start=cue.start,  # type: ignore[arg-type]
                end=cue.end,  # type: ignore[arg-type]
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


def _reference(trace: SourceTrace) -> PcmReference:
    """손 참조 — 트레이스의 PCM 신원·원점과 짝을 이룬다."""
    return PcmReference(
        source_id=trace.source_id,
        original_stem="demo",
        original_sha256="demo-sha",
        path=Path("/cache/demo.pcm"),
        pcm_sha256="e" * 64,
        analysis_key=trace.pcm_analysis_key,
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


@dataclass
class _World:
    """계획까지 미리 지어둔 세계 — 투영은 여기에 경계 기록만 얹는다."""

    document: ProjectDocument
    slices: tuple[LogicalSlice, ...]
    source: SourceCaptionSet
    traces: list[SourceTrace]
    references: list[PcmReference]
    plan: BoundaryPlan

    def project(
        self,
        transcripts: list[BoundaryTranscript],
        *,
        traces: list[SourceTrace] | None = None,
        plan: BoundaryPlan | None = None,
    ) -> BoundaryProjection:
        sources = self.traces if traces is None else traces
        return project_boundaries(plan or self.plan, transcripts, self.source, sources)


def _world(
    tmp_path: Path,
    assets: list[str],
    items: list[str],
    analyses: list[SourceAnalysis],
    *,
    contents: dict[str, str] | None = None,
) -> _World:
    """문서 → 미디어 → P3 초안 → 논리 슬라이스 → 경계 계획까지 실제로 잇는다."""
    document = _document(tmp_path, assets, items)
    media = [
        _media(document, resource.xml_id, content_id=(contents or {}).get(resource.xml_id))
        for resource in document.resources
    ]
    source = project_source_captions(document, media, analyses)
    slices = logical_slices(document, media)
    traces = [item for item in analyses if isinstance(item, SourceTrace)]
    references = [_reference(trace) for trace in traces]
    plan = plan_boundaries(slices, source, traces, references)
    return _World(document, slices, source, traces, references, plan)


def _leading(
    tmp_path: Path, *, origin: Fraction = Fraction(0)
) -> tuple[_World, BoundaryTranscript]:
    """들머리 잘림 세계 — 요청 [4, 4.45] 한 개, 묶인 큐 한 장, 온전한 큐 한 장.

    슬라이스는 파일 [4, 6) · 타임라인 [0, 2)다. 성공 경계 큐의 파일 시각은
    [81/20, 22/5]로 고정하고 원점만 파라미터로 바꾼다.
    """
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
        [
            _trace(
                "acid-r2",
                [_CueSpec(3.6, 4.4), _CueSpec(5.0, 5.5, seg_ids=(1,))],
                [_SegSpec(3.5, 4.45), _SegSpec(4.95, 5.6)],
                origin=origin,
            )
        ],
    )
    (request,) = world.plan.requests
    assert (request.source_start, request.source_end) == (Fraction(4), Fraction(89, 20) + origin)
    transcript = _transcript(
        request,
        [
            _bcue(
                0,
                Fraction(81, 20) - origin,
                Fraction(22, 5) - origin,
                BOUNDARY_TEXT,
                source_id="acid-r2",
            )
        ],
        origin=origin,
    )
    return world, transcript


def _trailing(tmp_path: Path) -> tuple[_World, BoundaryTranscript]:
    """끝머리 잘림 세계 — 슬라이스에 온전한 큐가 없어 닻이 닿은 자루 끝에서 난다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
        [_trace("acid-r2", [_CueSpec(5.7, 6.3)], [_SegSpec(5.6, 6.35)])],
    )
    (request,) = world.plan.requests
    assert (request.source_start, request.source_end) == (Fraction(28, 5), Fraction(6))
    transcript = _transcript(
        request,
        [_bcue(0, Fraction(113, 20), Fraction(119, 20), BOUNDARY_TEXT, source_id="acid-r2")],
    )
    return world, transcript


def _bcue(
    index: int,
    start: Fraction,
    end: Fraction,
    text: str,
    *,
    source_id: str = "acid-r2",
    flags: tuple[str, ...] = (),
) -> SourceCue:
    """손 경계 큐 — 디코드 국소 시각의 정확 표현."""
    return SourceCue(
        cue_id=f"bcue-{index}",
        source_id=source_id,
        segment_ids=(f"b-seg-{index}",),
        start=start,
        end=end,
        text=text,
        flags=flags,
    )


def _transcript(
    request: boundary.BoundaryRequest,
    cues: list[SourceCue] | tuple[SourceCue, ...],
    *,
    status: str = "success",
    error: str | None = None,
    origin: Fraction = Fraction(0),
) -> BoundaryTranscript:
    """손 경계 기록 — 요청과의 연결 재료를 그대로 실는다(디코드 국소 시계)."""
    records = tuple(cues)
    segments = tuple(
        SourceSegment(
            segment_id=f"b-seg-{position}",
            audio_start=cue.start,
            audio_end=cue.end,
            file_start=cue.start + origin,
            file_end=cue.end + origin,
            text="경계 골격",
            compression_ratio=1.0,
            avg_logprob=-0.1,
            no_speech_prob=0.0,
        )
        for position, cue in enumerate(records)
    )
    return BoundaryTranscript(
        boundary_id=f"bnd-{request.request_id[:12]}",
        cache_key=f"cache-{request.request_id[:12]}",
        request_id=request.request_id,
        source_id=request.source_id,
        pcm_analysis_key=request.pcm_analysis_key,
        original_stem="demo",
        source_start=request.source_start,
        source_end=request.source_end,
        sample_range=request.sample_range,
        status=status,  # type: ignore[arg-type]
        draft=(),
        settled_spine=segments if status == "success" else (),
        repairs=(),
        cues=records if status == "success" else (),
        error=error,
    )


def _clips(document: ProjectDocument) -> tuple[ClipInstance, ...]:
    return tuple(item for item in document.storyline if isinstance(item, ClipInstance))


# ─────────────────────────────────────────────────────────────
#  교체 — 정확히 그 (슬라이스, 인스턴스, 소스 구간)만 갈아끼운다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("origin", [Fraction(0), Fraction(8, 125)])
def test_successful_boundary_replaces_exactly(tmp_path: Path, origin: Fraction) -> None:
    """성공 경계 큐가 스코프 안의 묶인 소스 큐를 대체한다 — 덧붙이지 않고 다시
    다듬지도 않는다. 파일 시각은 원점을 정확히 한 번 더해 그대로다."""
    world, transcript = _leading(tmp_path, origin=origin)
    projection = world.project([transcript])
    (request,) = world.plan.requests
    (clip,) = _clips(world.document)
    (slice_,) = world.slices

    assert isinstance(projection, BoundaryProjection)
    assert len(projection.cues) == 2, "갈아끼우기다 — 옛 큐와 새 큐가 함께 나오지 않는다"
    replaced, kept = projection.cues
    assert isinstance(replaced, ProjectCue) and isinstance(kept, ProjectCue)
    # 경계 큐 — 이미 정제된 글자·시각 그대로, 조판은 다시 돌지 않는다
    assert replaced.provenance == "boundary"
    assert replaced.cue_id == "bcue-0"
    assert replaced.slice_id == request.slice_id == slice_.slice_id
    assert replaced.instance_ids == request.instance_ids == (clip.instance_id,)
    assert replaced.source_id == "acid-r2"
    assert replaced.source_start == Fraction(81, 20)
    assert replaced.source_end == Fraction(22, 5)
    assert replaced.timeline_start == Fraction(1, 20)
    assert replaced.timeline_end == Fraction(2, 5)
    assert replaced.text == BOUNDARY_TEXT, "'...'가 '…'로 바뀌면 두 번 조판한 것이다"
    assert replaced.flags == ()
    # 스코프 밖의 소스 큐는 하나도 안 바뀐다
    assert kept.provenance == "source"
    assert kept.source_start == Fraction(5) + origin
    assert kept.source_end == Fraction(11, 2) + origin
    assert kept.timeline_start == Fraction(1) + origin
    assert kept.timeline_end == Fraction(3, 2) + origin
    assert kept.flags == ()
    # 겹침마다 출처는 정확히 하나 — 요청의 타임라인 상 안에 소스 큐가 남지 않는다
    span = (request.source_start - Fraction(4), request.source_end - Fraction(4))
    for cue in projection.cues:
        overlaps = cue.timeline_start < span[1] and span[0] < cue.timeline_end
        assert not (overlaps and cue.provenance == "source")
    assert projection.unresolved_cue_ids == ()
    assert projection.unresolved_request_ids == ()
    assert "경계 복원" in projection.source_srt and "자막" in projection.source_srt


def test_replacement_never_refines_or_polishes_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """투영은 조판 계층을 부르지 않는다 — 경계 큐도 소스 큐도 이미 완성품이다."""
    world, transcript = _leading(tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("투영은 정제·조판을 다시 부르지 않는다")

    monkeypatch.setattr("vlogsrt.refine.refine", boom)
    monkeypatch.setattr("vlogsrt.refine.polish_pass", boom)
    projection = world.project([transcript])
    (replaced,) = (cue for cue in projection.cues if cue.provenance == "boundary")
    assert replaced.text == BOUNDARY_TEXT


def test_replacement_of_one_repeated_instance_leaves_the_sibling_untouched(
    tmp_path: Path,
) -> None:
    """반복 인스턴스 둘 — 성공한 쪽만 갈아끼우고, 빈 쪽은 원문 그대로 검토로 남는다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r2", offset="100/25s", start="2600/25s"),
        ],
        [_trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)])],
    )
    first_request, second_request = world.plan.requests
    first_clip, second_clip = _clips(world.document)
    assert first_request.instance_ids == (first_clip.instance_id,)
    assert second_request.instance_ids == (second_clip.instance_id,)
    transcripts = [
        _transcript(
            first_request,
            [_bcue(0, Fraction(41, 10), Fraction(22, 5), BOUNDARY_TEXT, source_id="acid-r2")],
        ),
        _transcript(second_request, [], status="empty"),
    ]

    projection = world.project(transcripts)

    assert len(projection.cues) == 2
    replaced, sibling = projection.cues
    assert replaced.provenance == "boundary"
    assert replaced.instance_ids == (first_clip.instance_id,)
    assert replaced.timeline_start == Fraction(1, 10)
    assert replaced.timeline_end == Fraction(2, 5)
    # 형제 인스턴스는 지워지지도 바뀌지도 않는다 — 빈 경계의 초안이다
    assert sibling.provenance == "source"
    assert sibling.instance_ids == (second_clip.instance_id,)
    assert sibling.text == "자막"
    assert sibling.timeline_start == Fraction(4)
    assert sibling.timeline_end == Fraction(22, 5)
    assert BOUNDARY_REQUIRED in sibling.flags
    assert BOUNDARY_UNRESOLVED in sibling.flags
    assert SUBMS_COLLAPSED not in sibling.flags
    assert projection.unresolved_request_ids == (second_request.request_id,)
    assert projection.unresolved_cue_ids == ()
    assert projection.locked_instance_ids == world.plan.locked_instance_ids
    # SRT는 둘 다 산다 — 빈 경계도 글자를 지우지 않는다
    assert BOUNDARY_TEXT in projection.source_srt
    assert "자막" in projection.source_srt


def test_error_transcript_falls_back_with_typed_unresolved(tmp_path: Path) -> None:
    """실패 경계 — 원문이 그대로 남고 요청 신원이 검토에 이름 붙는다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
        [_trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)])],
    )
    (request,) = world.plan.requests
    transcript = _transcript(request, [], status="error", error="호출이 죽었다")

    projection = world.project([transcript])

    (cue,) = projection.cues
    assert cue.provenance == "source"
    assert cue.text == "자막"
    assert cue.flags == (BOUNDARY_REQUIRED, BOUNDARY_UNRESOLVED)
    assert projection.unresolved_request_ids == (request.request_id,)
    assert projection.source_srt.startswith("1\n")


def test_all_empty_fallback_regenerates_the_source_srt_bytes(tmp_path: Path) -> None:
    """되물을 것이 하나도 성공하지 않으면 SOURCE SRT는 P3-03의 바이트 그대로다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r3", offset="100/25s", start="2600/25s"),
        ],
        [
            _trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)]),
            _trace("acid-r3", [_CueSpec(5.7, 6.3)], [_SegSpec(5.6, 6.35)]),
        ],
    )
    assert len(world.plan.requests) == 2
    transcripts = [_transcript(request, [], status="empty") for request in world.plan.requests]

    projection = world.project(transcripts)

    assert projection.source_srt == world.source.srt_text
    assert projection.unresolved_request_ids == tuple(
        request.request_id for request in world.plan.requests
    )
    for cue in projection.cues:
        assert cue.provenance == "source"
        assert BOUNDARY_UNRESOLVED in cue.flags


def test_trailing_cut_replacement_maps_through_the_slice_shift(tmp_path: Path) -> None:
    """끝머리 잘림 — 슬라이스에 온전한 큐가 없어도 닻은 정확하다. 요청이 슬라이스
    닻을 직접 싣고 있으므로 유도도 가설도 없다(P4-04b)."""
    world, transcript = _trailing(tmp_path)
    projection = world.project([transcript])
    (replaced,) = (cue for cue in projection.cues if cue.provenance == "boundary")
    assert replaced.source_start == Fraction(113, 20)
    assert replaced.source_end == Fraction(119, 20)
    assert replaced.timeline_start == Fraction(33, 20)
    assert replaced.timeline_end == Fraction(39, 20)


def test_single_cue_spanning_the_whole_slice_places_from_the_anchor(tmp_path: Path) -> None:
    """자루를 통째로 덮은 큐 하나 — 옛 구간 교집합 유도가 가설 없이는 좁힐 수
    없던 모양이다(P4-04b). 요청은 자루 전체 [4, 6]이고 닻을 직접 싣는다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
        [
            _trace(
                "acid-r2",
                [_CueSpec(Fraction(39, 10), Fraction(61, 10))],
                [_SegSpec(3.85, 6.15)],
            )
        ],
    )
    (request,) = world.plan.requests
    assert (request.source_start, request.source_end) == (Fraction(4), Fraction(6))
    assert request.timeline_start == Fraction(0)
    (clamped,) = world.plan.clamped_cues
    assert (clamped.timeline_start, clamped.timeline_end) == (Fraction(0), Fraction(2))
    transcript = _transcript(
        request,
        [_bcue(0, Fraction(21, 5), Fraction(22, 5), BOUNDARY_TEXT, source_id="acid-r2")],
    )

    projection = world.project([transcript])

    (restored,) = projection.cues
    assert restored.provenance == "boundary"
    assert restored.source_start == Fraction(21, 5)
    assert restored.source_end == Fraction(22, 5)
    assert restored.timeline_start == Fraction(1, 5)
    assert restored.timeline_end == Fraction(2, 5)


def test_slice_anchor_contradicting_a_clamped_cue_rejects(tmp_path: Path) -> None:
    """요청의 닻이 묶인 큐의 배치와 어긋나면 닫는다 — 유도로 덮는 길은 없다."""
    world, boundary = _leading(tmp_path)
    (request,) = world.plan.requests
    clamped, untouched = world.plan.clamped_cues
    assert clamped.slice_id == request.slice_id
    drifted = replace(
        clamped,
        timeline_start=clamped.timeline_start + Fraction(1),
        timeline_end=clamped.timeline_end + Fraction(1),
    )
    forged = replace(world.plan, clamped_cues=(drifted, untouched))
    with pytest.raises(ValueError, match="모순"):
        project_boundaries(forged, [boundary], world.source, world.traces)


# ─────────────────────────────────────────────────────────────
#  연결 — 신원으로만 묶고 순서를 믿지 않는다
# ─────────────────────────────────────────────────────────────


def test_shuffled_transcript_and_trace_orders_give_identical_projection(tmp_path: Path) -> None:
    """경계 기록·트레이스 순서를 섞어도 투영은 같다 — 연결은 신원으로 한다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r3", offset="100/25s", start="2600/25s"),
        ],
        [
            _trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)]),
            _trace("acid-r3", [_CueSpec(5.7, 6.3)], [_SegSpec(5.6, 6.35)]),
        ],
    )
    assert len(world.plan.requests) == 2
    first, second = world.plan.requests
    transcripts = [
        _transcript(
            first,
            [_bcue(0, Fraction(81, 20), Fraction(22, 5), BOUNDARY_TEXT, source_id="acid-r2")],
        ),
        _transcript(
            second,
            [_bcue(1, Fraction(113, 20), Fraction(119, 20), "둘째 경계", source_id="acid-r3")],
        ),
    ]

    forward = world.project(transcripts)
    backward = world.project(list(reversed(transcripts)), traces=list(reversed(world.traces)))

    assert forward == backward
    assert [cue.text for cue in forward.cues] == [BOUNDARY_TEXT, "둘째 경계"]
    assert [cue.provenance for cue in forward.cues] == ["boundary", "boundary"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda w, ts: [], id="missing-transcript"),
        pytest.param(
            lambda w, ts: [replace(ts[0], request_id="req-없는-신원")],
            id="orphan-transcript",
        ),
        pytest.param(lambda w, ts: [*ts, *ts], id="duplicate-transcripts"),
        pytest.param(
            lambda w, ts: [replace(ts[0], source_id="acid-다른-소스")],
            id="transcript-source-mismatch",
        ),
        pytest.param(
            lambda w, ts: [replace(ts[0], pcm_analysis_key="pcm-다른-분석")],
            id="transcript-pcm-key-mismatch",
        ),
        pytest.param(
            lambda w, ts: [replace(ts[0], source_end=Fraction(5))],
            id="transcript-interval-mismatch",
        ),
        pytest.param(
            lambda w, ts: [
                replace(ts[0], sample_range=SampleRange(64000, 72000)),
            ],
            id="transcript-sample-range-mismatch",
        ),
        pytest.param(lambda w, ts: (ts, []), id="missing-trace"),
        pytest.param(lambda w, ts: (ts, [*w.traces, *w.traces]), id="duplicate-traces"),
        pytest.param(
            lambda w, ts: (
                ts,
                [replace(w.traces[0], pcm_sample0_file_time=Fraction(1, 20))],
            ),
            id="origin-disagreement",
        ),
    ],
)
def test_broken_linkage_rejects_the_projection(
    tmp_path: Path,
    mutate: Callable[[object, list], object],
) -> None:
    """빠짐·중복·모순된 연결은 그 자리에서 닫는다 — 조용한 투영이 아니다."""
    world, transcript = _leading(tmp_path)
    mutated = mutate(world, [transcript])  # type: ignore[call-arg]
    transcripts, traces = mutated if isinstance(mutated, tuple) else (mutated, world.traces)
    with pytest.raises(ValueError, match="없다|둘 이상|다르다|신원"):
        project_boundaries(world.plan, transcripts, world.source, traces)


@pytest.mark.parametrize(
    "cues",
    [
        pytest.param([_bcue(0, Fraction(39, 10), Fraction(79, 20), "앞")], id="outside-before"),
        pytest.param([_bcue(0, Fraction(89, 20), Fraction(9, 2), "뒤")], id="outside-after"),
        pytest.param([_bcue(0, Fraction(81, 20), Fraction(81, 20), "길이 0")], id="zero-length"),
        pytest.param(
            [
                _bcue(0, Fraction(81, 20), Fraction(21, 5), "첫"),
                _bcue(1, Fraction(83, 20), Fraction(22, 5), "겹"),
            ],
            id="overlapping",
        ),
        pytest.param(
            [
                _bcue(0, Fraction(43, 20), Fraction(22, 5), "뒤"),
                _bcue(1, Fraction(81, 20), Fraction(21, 5), "앞"),
            ],
            id="descending",
        ),
    ],
)
def test_forged_transcript_intervals_reject(tmp_path: Path, cues: list[SourceCue]) -> None:
    """경계 큐는 요청 구간 안에 순서 있게 양수 길이로 산다 — 위조는 거부한다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
        [_trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)])],
    )
    (request,) = world.plan.requests
    transcript = _transcript(request, cues)
    with pytest.raises(ValueError, match="구간|길이|겹치|순서"):
        world.project([transcript])


# ─────────────────────────────────────────────────────────────
#  밀리초 — 교체 뒤에 다시 잰다, 증거는 남기고 SRT만 뺀다
# ─────────────────────────────────────────────────────────────


def test_exact_seventeen_millisecond_boundary_collapse(tmp_path: Path) -> None:
    """정확 `133/8000`초 → `+1/24000`초 — 양 끝이 모두 17ms로 반올림된다.

    정확 길이는 양수지만 밀리초로 나타낼 수 없다: 큐는 남고 깃발·미해결 신원이
    붙고 SRT에서 그 장만 빠진다.
    """
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
        [_trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)])],
    )
    (request,) = world.plan.requests
    start = Fraction(4) + Fraction(133, 8000)  # 타임라인 133/8000초로 정확히 간다
    transcript = _transcript(
        request,
        [
            _bcue(0, start, start + Fraction(1, 24000), "한 프레임 마디", source_id="acid-r2"),
            _bcue(1, Fraction(21, 5), Fraction(22, 5), "온전한 마디", source_id="acid-r2"),
        ],
    )

    projection = world.project([transcript])

    collapsed, healthy = projection.cues
    assert collapsed.provenance == "boundary"
    assert collapsed.timeline_start == Fraction(133, 8000)
    assert collapsed.timeline_end == Fraction(133, 8000) + Fraction(1, 24000)
    assert collapsed.timeline_end > collapsed.timeline_start, "정확 길이는 양수다"
    assert SUBMS_COLLAPSED in collapsed.flags
    assert projection.unresolved_cue_ids == ("bcue-0",)
    assert "한 프레임 마디" not in projection.source_srt
    assert "온전한 마디" in projection.source_srt


def test_boundary_clamp_edge_collapse(tmp_path: Path) -> None:
    """요청 끝에 딱 붙어 자른 경계 큐 — 끝이 자루 끝에 닿아도 밀리초로 접힌다."""
    world = _world(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
        [_trace("acid-r2", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)])],
    )
    (request,) = world.plan.requests
    end = request.source_end  # 89/20초 — 요청 구간의 끝에 정확히 닿는다
    transcript = _transcript(
        request,
        [
            _bcue(1, Fraction(81, 20), Fraction(21, 5), "온전한 마디", source_id="acid-r2"),
            _bcue(0, end - Fraction(1, 24000), end, "끝에 붙은 마디", source_id="acid-r2"),
        ],
    )

    projection = world.project([transcript])

    healthy, collapsed = projection.cues
    assert collapsed.source_end == request.source_end
    assert SUBMS_COLLAPSED in collapsed.flags
    assert projection.unresolved_cue_ids == ("bcue-0",)
    assert "끝에 붙은 마디" not in projection.source_srt
    assert "온전한 마디" in projection.source_srt


def test_representable_replacement_clears_only_the_replaced_collapse(tmp_path: Path) -> None:
    """접힌 옛 큐를 표현 가능한 경계 큐가 대체하면 그 미해결 신원만 내려간다.

    형제의 접힘과 빈 경계 요청은 그대로 남고, 상류 SOURCE 기록은 접힌 채로
    불변이다 — 그것이 그 접힘의 증거다.
    """
    tiny_end = Fraction(4) + Fraction(1, 24000)
    world = _world(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r2", offset="0s", start="2600/25s"),
            _clip_xml("r3", offset="100/25s", start="2600/25s"),
        ],
        [
            _trace(
                "acid-r2",
                [_CueSpec(Fraction(39, 10), tiny_end), _CueSpec(5.0, 5.5, seg_ids=(1,))],
                [_SegSpec(3.85, 4.05), _SegSpec(4.95, 5.6)],
            ),
            _trace(
                "acid-r3",
                [_CueSpec(Fraction(39, 10), tiny_end)],
                [_SegSpec(3.85, 4.05)],
            ),
        ],
    )
    assert world.source.srt_text == ("1\n00:00:01,000 --> 00:00:01,500\n자막\n"), (
        "접힌 두 큐는 빠지고 온전한 큐만 SOURCE SRT에 남아 있다"
    )
    collapsed_ids = world.source.unresolved_cue_ids
    assert len(collapsed_ids) == 2
    replaced_request, kept_request = world.plan.requests
    transcripts = [
        # r2의 접힌 큐를 표현 가능한 경계 큐(타임라인 [0, 1/25])가 대체한다
        _transcript(
            replaced_request,
            [_bcue(0, Fraction(4), Fraction(101, 25), BOUNDARY_TEXT, source_id="acid-r2")],
        ),
        _transcript(kept_request, [], status="empty"),
    ]
    source_before = world.source

    projection = world.project(transcripts)

    texts = {cue.text: cue for cue in projection.cues}
    restored = texts[BOUNDARY_TEXT]
    assert restored.provenance == "boundary"
    assert restored.timeline_start == Fraction(0)
    assert restored.timeline_end == Fraction(1, 25)
    assert SUBMS_COLLAPSED not in restored.flags
    # 형제의 접힘은 그대로 — 지워지지 않고 여전히 미해결이다
    assert projection.unresolved_cue_ids == (collapsed_ids[1],)
    assert projection.unresolved_request_ids == (kept_request.request_id,)
    sibling = next(
        cue
        for cue in projection.cues
        if cue.provenance == "source" and SUBMS_COLLAPSED in cue.flags
    )
    assert sibling.cue_id == collapsed_ids[1]
    assert BOUNDARY_UNRESOLVED in sibling.flags
    # 상류 SOURCE 기록은 불변 — 옛 접힘의 증거로 그대로 산다
    assert world.source is source_before
    assert world.source.unresolved_cue_ids == collapsed_ids
    assert world.source.srt_text == "1\n00:00:01,000 --> 00:00:01,500\n자막\n"
    assert BOUNDARY_TEXT in projection.source_srt


# ─────────────────────────────────────────────────────────────
#  잠금·부재 — 계획의 것을 그대로, 되풀이 가능한 실패만 막는다
# ─────────────────────────────────────────────────────────────


def test_typed_unavailability_propagates_unchanged_and_only_retryable_blocks(
    tmp_path: Path,
) -> None:
    """세 사유 코드 모두 바이트 그대로 순서대로 — 선언된 무음은 완결이고 되풀이
    가능한 둘만 최종 수락을 막는다."""
    for reason_code, declared in (
        ("declared_no_audio", True),
        ("stream_unavailable", False),
        ("pcm_unavailable", False),
    ):
        document = _document(
            tmp_path / reason_code,
            [_asset_xml("r2", has_audio="0" if declared else "1")],
            [
                _clip_xml("r2", offset="0s", start="2600/25s"),
                _clip_xml("r2", offset="100/25s", start="2600/25s"),
            ],
        )
        record = _unavailable(
            document,
            "r2",
            status="no_audio" if declared else "error",
            reason_code=reason_code,
        )
        media = [_media(document, resource.xml_id) for resource in document.resources]
        source = project_source_captions(document, media, [record])
        slices = logical_slices(document, media)
        plan = plan_boundaries(slices, source, [], [])

        projection = project_boundaries(plan, [], source, [])

        assert projection.unavailable_instances == plan.unavailable_instances
        assert projection.locked_instance_ids == plan.locked_instance_ids
        assert projection.cues == () and projection.source_srt == ""
        blocking = [
            item
            for item in projection.unavailable_instances
            if item.reason_code in RETRYABLE_UNAVAILABLE_REASONS
        ]
        if declared:
            assert blocking == [], "선언된 무음은 완결된 비과금 부재다 — 수락을 막지 않는다"
            assert projection.unresolved_request_ids == ()
        else:
            assert [item.reason_code for item in blocking] == [reason_code] * 2
        assert all(isinstance(item, UnavailableInstance) for item in blocking)


def _equal_content_world(tmp_path: Path) -> _World:
    """같은 내용 두 자원 — r2는 pre-PCM 실패, r3는 건강한 트레이스다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r3", offset="0s", start="2600/25s"),
            _clip_xml("r2", offset="100/25s", start="2600/25s"),
        ],
    )
    analyses: list[SourceAnalysis] = [
        _unavailable(document, "r2", source_id="same-bytes", status="error"),
        _trace("same-bytes", [_CueSpec(3.6, 4.4)], [_SegSpec(3.5, 4.45)]),
    ]
    media = [
        _media(document, resource.xml_id, content_id="same-bytes")
        for resource in document.resources
    ]
    source = project_source_captions(document, media, analyses)
    slices = logical_slices(document, media)
    traces = [item for item in analyses if isinstance(item, SourceTrace)]
    references = [_reference(trace) for trace in traces]
    plan = plan_boundaries(slices, source, traces, references)
    return _World(document, slices, source, traces, references, plan)


def test_equal_content_unavailable_leaves_healthy_projection(tmp_path: Path) -> None:
    world = _equal_content_world(tmp_path)
    (request,) = world.plan.requests
    assert request.source_id == "same-bytes"
    transcript = _transcript(
        request,
        [_bcue(0, Fraction(81, 20), Fraction(22, 5), BOUNDARY_TEXT, source_id="same-bytes")],
    )

    projection = world.project([transcript])

    (restored,) = projection.cues
    assert restored.provenance == "boundary"
    assert restored.text == BOUNDARY_TEXT
    (record,) = projection.unavailable_instances
    assert record.reason_code == "stream_unavailable"
    assert record.reason_code in RETRYABLE_UNAVAILABLE_REASONS
    assert projection.locked_instance_ids == world.plan.locked_instance_ids


def test_unavailable_instance_must_be_locked(tmp_path: Path) -> None:
    """잠기지 않은 인스턴스의 unavailable 기록은 모순이다 — 검사하고 닫는다."""
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [_clip_xml("r2", offset="0s", start="2600/25s")],
    )
    record = _unavailable(document, "r2", status="error")
    media = [_media(document, resource.xml_id) for resource in document.resources]
    source = project_source_captions(document, media, [record])
    slices = logical_slices(document, media)
    plan = plan_boundaries(slices, source, [], [])
    forged = replace(plan, locked_instance_ids=frozenset())
    with pytest.raises(ValueError, match="잠겨 있지 않다"):
        project_boundaries(forged, [], source, [])


# ─────────────────────────────────────────────────────────────
#  규율 — 재귀 없음·불변·순수성·결정성
# ─────────────────────────────────────────────────────────────


def test_projection_does_not_plan_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """투영은 경계를 되묻지 않는다 — 계획은 다시 만들지 않는다."""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("투영은 경계 요청을 다시 계획하지 않는다")

    monkeypatch.setattr(boundary, "plan_boundaries", boom)
    world, transcript = _leading(tmp_path)
    projection = world.project([transcript])
    assert len(projection.cues) == 2


def test_projection_is_idempotent_and_frozen(tmp_path: Path) -> None:
    world, transcript = _leading(tmp_path)
    first = world.project([transcript])
    second = world.project([transcript])
    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.cues = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.cues[0].text = "고침"  # type: ignore[misc]


def test_projection_keeps_inputs_immutable(tmp_path: Path) -> None:
    """입력은 하나도 고치지 않는다 — 소스·경계 트레이스·기록이 전부 증거로 남는다."""
    world, transcript = _leading(tmp_path)
    plan_before = world.plan
    source_before = world.source
    traces_before = tuple(world.traces)
    transcripts_before = (transcript,)

    world.project([transcript])

    assert world.plan == plan_before
    assert world.source == source_before
    assert tuple(world.traces) == traces_before
    assert (transcript,) == transcripts_before
    assert traces_before[0].settled_spine == world.traces[0].settled_spine
    assert transcript.settled_spine == transcripts_before[0].settled_spine


def test_projection_signature_is_the_binding_contract() -> None:
    """투영은 네 입력만 받는다 — 계획·기록·소스·트레이스, 그 밖은 없다."""
    assert tuple(inspect.signature(project_boundaries).parameters) == (
        "plan",
        "transcripts",
        "source",
        "source_traces",
    )
