"""소스 큐를 입력 타임라인 SOURCE SRT 초안으로 투영한다 (P3-03).

계획("Source analysis, clocks, identity" ＋ "Input and output")의 정본:

- **큐는 이미 완성품이다.** P3-02의 정제된 큐를 정확 `Fraction`으로 옮길 뿐
  다시 손대지 않는다. 이 모듈은 조판 계층(refine·polish·output·verify·layout)을
  모른다 — 시각과 글자는 whisper가 만들고 P3-02가 정제한 그대로다.
- **시계는 두 번 건너간다.** 오디오 국소 시각에 원점
  `SourceTrace.pcm_sample0_file_time`을 **정확히 한 번** 더해 파일 시계가 되고,
  XML 공식 `clip.offset - tcStart + asset.start + file_time - clip.start`이
  입력 타임라인 경과 시각을 만든다. 밀리초 양자화는 P1 timebase가 직렬화
  순간에 딱 한 번 한다.
- **정확 양수 길이가 표현 가능성은 아니다.** half-up 뒤 0이 되는 큐(subms)는
  직렬화가 거부하고, 조립은 증거를 지우지 않는다 — 깃발 `subms_collapsed`·
  미해결 id·SRT에서는 그 장만 뺀다. 늘리거나 합치거나 프레임 틈을 지어내지
  않는다.
- **저변 잘림은 발표만 묶는다.** 원본 소스 기록은 그대로 남고 `boundary_required`
  깃발과 함께 클립 인스턴스가 P4를 위해 기록된다. 조용히 빠지는 큐는 없다.
- **실패는 조용한 성공이 아니다.** 자원 스코프 unavailable은 내용 중복 제거보다
  먼저 붙는다 — 같은 내용이라 `has_audio` 선언이 다르면 해석이 달라야 한다.

순수한 조립이다. 호출·ffmpeg·파일 쓰기가 없다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from vlogsrt.models import Cue
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import AssetResource, ClipInstance, ProjectDocument
from vlogsrt.project.speech import (
    SourceAnalysis,
    SourceTrace,
    UnavailableSourceAudio,
)
from vlogsrt.project.timebase import format_srt_time, milliseconds_half_up

BOUNDARY_REQUIRED = "boundary_required"
"""저변 잘림 깃발 — 발표는 묶었지만 원본 소스 큐는 그대로다(P4가 다시 묻는다)."""

SUBMS_COLLAPSED = "subms_collapsed"
"""반올림 뒤 0이 되는 큐 — SRT에서 그 장만 빼고 증거는 남긴다."""

_CUE_ID_DOMAIN = "vlogsrt-source-cue-v1"
"""소스 큐 신원의 도메인 구분자 — 다른 해시 재료와 우연히 겹치지 않게 한다."""

_STATUS_SUCCESS = "success"
_STATUS_ERROR = "error"


# ─────────────────────────────────────────────────────────────
#  기록 — 소스 큐·배치 큐·조립 결과
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceCue:
    """소스 하나의 정제된 큐 — 0기반 디코드 오디오 국소 시각의 정확 표현.

    원점은 아직 더하지 않았다(`SourceTrace.pcm_sample0_file_time`은 배치에서
    정확히 한 번 더해진다). `segment_ids`는 골격 세그먼트의 결정적 신원이다.
    """

    cue_id: str
    source_id: str
    segment_ids: tuple[str, ...]
    start: Fraction
    end: Fraction
    text: str
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlacedSourceCue:
    """소스 큐의 인스턴스 배치 — 파일 시계 원본과 타임라인 발표를 함께 남긴다.

    `source_start/source_end`은 원점을 더한 파일 시계 **원본 기록**이다(잘림이
    묶어도 그대로다). `timeline_start/timeline_end`은 발표 시각이고 저변 잘림이
    있으면 안쪽으로 묶여 있다.
    """

    cue_id: str
    instance_id: str
    source_id: str
    source_start: Fraction
    source_end: Fraction
    timeline_start: Fraction
    timeline_end: Fraction
    text: str
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceCaptionSet:
    """입력 프로젝트 전체의 SOURCE SRT 초안 — 자동 러프컷 이전의 경과 타임라인."""

    input_sha256: str
    source_cues: tuple[SourceCue, ...]
    placed_cues: tuple[PlacedSourceCue, ...]
    srt_text: str
    boundary_instance_ids: tuple[str, ...]
    unresolved_cue_ids: tuple[str, ...]
    unavailable_audio: tuple[UnavailableSourceAudio, ...]


# ─────────────────────────────────────────────────────────────
#  소스 큐 — 트레이스의 완성품을 정확 Fraction으로 옮긴다
# ─────────────────────────────────────────────────────────────


def source_cues(trace: SourceTrace) -> tuple[SourceCue, ...]:
    """정제된 큐를 소스 큐로 옮긴다. 다듬지 않고, 실패는 큐가 아니라 상태로 남는다."""
    if trace.status == _STATUS_ERROR:
        raise ValueError(
            f"error 트레이스에는 큐가 없다: source_id={trace.source_id!r},"
            f" error={trace.error!r} — 실패를 빈 성공으로 꾸미지 않는다"
        )
    if trace.status != _STATUS_SUCCESS:
        return ()
    cues: list[SourceCue] = []
    seen: set[str] = set()
    for cue in trace.cues:
        start = Fraction(str(cue.start))
        end = Fraction(str(cue.end))
        if end <= start:
            raise ValueError(
                f"정제된 큐의 길이가 0 이하다: start={cue.start!r}, end={cue.end!r}"
                f" (source_id={trace.source_id!r}) — 소스 트레이스가 깨졌다"
            )
        segment_ids = _segment_ids(trace, cue)
        cue_id = _cue_id(trace, cue, start, end, segment_ids)
        if cue_id in seen:
            raise ValueError(f"소스 큐 신원이 중복된다: {cue_id} — 큐 index·시각은 유일해야 한다")
        seen.add(cue_id)
        cues.append(
            SourceCue(
                cue_id=cue_id,
                source_id=trace.source_id,
                segment_ids=segment_ids,
                start=start,
                end=end,
                text="\n".join(cue.lines),
                flags=cue.flags,
            )
        )
    return tuple(cues)


def _segment_ids(trace: SourceTrace, cue: Cue) -> tuple[str, ...]:
    """레거시 `seg_id`를 골격 세그먼트 신원으로. settle이 골격을 0부터 다시 세고
    `_source_segments`가 그 순서를 지키므로 **위치가 곧 레거시 id다**."""
    spine = trace.settled_spine
    ids: list[str] = []
    for legacy_id in cue.seg_ids:
        if not 0 <= legacy_id < len(spine):
            raise ValueError(
                f"큐의 seg_id {legacy_id}가 골격에 없다 — 골격은 {len(spine)}장이다"
                f" (source_id={trace.source_id!r})"
            )
        ids.append(spine[legacy_id].segment_id)
    return tuple(ids)


def _cue_id(
    trace: SourceTrace, cue: Cue, start: Fraction, end: Fraction, segment_ids: tuple[str, ...]
) -> str:
    """소스 큐 신원 — 분석 키·큐 index·정확 시각·세그먼트의 해시. 같은 소스 큐는
    인스턴스마다 같은 신원으로 재생산된다."""
    material = "\n".join(
        [
            _CUE_ID_DOMAIN,
            f"analysis_key={trace.analysis_key}",
            f"cue_index={cue.index}",
            f"start={start}",
            f"end={end}",
            f"segment_ids={','.join(segment_ids)}",
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────
#  직렬화 — half-up은 이곳에서 딱 한 번
# ─────────────────────────────────────────────────────────────


def serialize_srt(cues: Sequence[SourceCue | PlacedSourceCue]) -> str:
    """정확 Fraction 큐를 SRT 문자열로. 배치 큐는 타임라인 시각을 쓴다.

    인접한 `end == next.start`는 같은 경계 문자열이 되는 유효한 형태고,
    `end <= start`(정확)·half-up 뒤 0이 되는 큐·역전 겹침은 큐 신원과 정확
    시각을 댄 진단과 함께 거부한다.
    """
    blocks: list[str] = []
    previous_end: Fraction | None = None
    for number, cue in enumerate(cues, start=1):
        start, end = _endpoints(cue)
        _require_serializable(cue.cue_id, start, end)
        if previous_end is not None and start < previous_end:
            raise ValueError(
                f"큐 {cue.cue_id}의 시작 {start}초가 앞 큐 끝 {previous_end}초보다"
                " 앞이다 — 겹침은 직렬화할 수 없다"
            )
        previous_end = end
        timeline = f"{format_srt_time(start)} --> {format_srt_time(end)}"
        blocks.append(f"{number}\n{timeline}\n{cue.text}\n")
    return "\n".join(blocks)


def _endpoints(cue: SourceCue | PlacedSourceCue) -> tuple[Fraction, Fraction]:
    """직렬화가 보는 시각 — 배치 큐는 타임라인, 소스 큐는 오디오 국소 시각."""
    if isinstance(cue, PlacedSourceCue):
        return cue.timeline_start, cue.timeline_end
    return cue.start, cue.end


def _require_serializable(cue_id: str, start: Fraction, end: Fraction) -> None:
    if end <= start:
        raise ValueError(f"큐 {cue_id}의 길이가 0 이하다: start={start}초, end={end}초")
    if milliseconds_half_up(end) <= milliseconds_half_up(start):
        raise ValueError(
            f"큐 {cue_id}가 밀리초로 나타낼 수 없다: 정확 시각 start={start}초,"
            f" end={end}초 — half-up 양쪽 다 {milliseconds_half_up(start)}ms다"
        )


# ─────────────────────────────────────────────────────────────
#  묶음 — 자원 먼저, 내용은 그 다음
# ─────────────────────────────────────────────────────────────


def _media_by_resource(
    document: ProjectDocument, media: Sequence[ResolvedMedia]
) -> dict[str, ResolvedMedia]:
    known = {resource.scoped_id for resource in document.resources}
    indexed: dict[str, ResolvedMedia] = {}
    for item in media:
        if item.resource_scoped_id not in known:
            raise ValueError(f"문서에 없는 자원을 가진 미디어다: {item.resource_scoped_id!r}")
        if item.resource_scoped_id in indexed:
            raise ValueError(
                f"미디어 자원이 중복이다: {item.resource_scoped_id!r} — 자원마다 정확히 하나다"
            )
        indexed[item.resource_scoped_id] = item
    for resource in document.resources:
        if resource.scoped_id not in indexed:
            raise ValueError(
                f"문서 자원에 미디어가 없다: {resource.xml_id!r}"
                f" ({resource.scoped_id!r}) — 모든 자원이 해석되어야 한다"
            )
    return indexed


def _indexed_analyses(
    traces: Sequence[SourceAnalysis], media_by_resource: dict[str, ResolvedMedia]
) -> tuple[dict[str, UnavailableSourceAudio], dict[str, SourceTrace]]:
    unavailable: dict[str, UnavailableSourceAudio] = {}
    by_source: dict[str, SourceTrace] = {}
    for analysis in traces:
        if isinstance(analysis, UnavailableSourceAudio):
            _index_unavailable(analysis, unavailable, media_by_resource)
        elif isinstance(analysis, SourceTrace):
            if analysis.source_id in by_source:
                raise ValueError(
                    f"소스 하나에 트레이스가 둘 이상이다: {analysis.source_id!r}"
                    " — 어느 쪽이 진짜인지 고르지 않는다(중복 정규화는 호출 쪽 몫이다)"
                )
            by_source[analysis.source_id] = analysis
        else:
            raise ValueError(f"소스 분석 기록이 아니다: {analysis!r}")
    return unavailable, by_source


def _index_unavailable(
    analysis: UnavailableSourceAudio,
    unavailable: dict[str, UnavailableSourceAudio],
    media_by_resource: dict[str, ResolvedMedia],
) -> None:
    if analysis.resource_scoped_id not in media_by_resource:
        raise ValueError(
            f"문서에 없는 자원의 unavailable 기록이다: {analysis.resource_scoped_id!r}"
            " — 자원 스코프 신원이 이 문서와 어긋난다"
        )
    if analysis.resource_scoped_id in unavailable:
        raise ValueError(
            f"자원 하나에 unavailable 기록이 둘 이상이다: {analysis.resource_scoped_id!r}"
        )
    unavailable[analysis.resource_scoped_id] = analysis


def _resolve_media(
    media: ResolvedMedia,
    unavailable: dict[str, UnavailableSourceAudio],
    by_source: dict[str, SourceTrace],
) -> SourceAnalysis:
    """미디어 하나의 분석 — 자원 스코프 unavailable이 먼저, 내용 트레이스가 나중."""
    record = unavailable.get(media.resource_scoped_id)
    if record is not None:
        _require_unavailable_pair(media, record)
        return record
    trace = by_source.get(media.asset_content_id)
    if trace is not None:
        if not media.has_audio:
            raise ValueError(
                f"has_audio=False로 선언된 미디어에 실제 전사 기록이 왔다:"
                f" {media.asset_content_id!r} — 선언된 무음은 unavailable 기록으로만 온다"
            )
        return trace
    raise ValueError(
        f"소스 분석 기록이 없다: asset_content_id={media.asset_content_id!r},"
        f" resource_scoped_id={media.resource_scoped_id!r} — 지어내지 않는다"
    )


def _require_unavailable_pair(media: ResolvedMedia, record: UnavailableSourceAudio) -> None:
    """unavailable 기록은 미디어와 신원·선언이 정확히 같아야 한다(3-02와 같은 선)."""
    if record.source_id != media.asset_content_id:
        raise ValueError(
            f"unavailable 기록의 소스 신원이 미디어와 다르다: {record.source_id!r} 대비"
            f" media {media.asset_content_id!r}"
        )
    if record.original_sha256 != media.original_sha256:
        raise ValueError(
            f"unavailable 기록의 원본 해시가 미디어와 다르다: {record.original_sha256!r}"
            f" 대비 media {media.original_sha256!r}"
        )
    declared = record.status == "no_audio"
    if media.has_audio and declared:
        raise ValueError(
            f"has_audio=True인 미디어에 선언된 무음(no_audio)이 왔다: {media.asset_content_id!r}"
            " — 삼태는 그 미디어의 선언과 같아야 한다"
        )
    if not media.has_audio and not declared:
        raise ValueError(
            f"has_audio=False인 미디어에 pre-PCM 오류 기록이 왔다: {media.asset_content_id!r}"
            " — 선언된 무음(no_audio)만 받는다"
        )


# ─────────────────────────────────────────────────────────────
#  조립 — 스토리라인을 따라 배치하고 증거를 남긴다
# ─────────────────────────────────────────────────────────────


def project_source_captions(
    document: ProjectDocument,
    media: Sequence[ResolvedMedia],
    traces: Sequence[SourceAnalysis],
) -> SourceCaptionSet:
    """불변 소스 큐를 입력 타임라인 SOURCE SRT 초안으로 조립한다.

    미디어·트레이스 순서는 자유다 — 묶음은 신원으로만 한다. 한 소스가 실패해도
    나머지 유효 소스는 명시적 초안에 그대로 산다(실패의 상태는 트레이스가 갖고
    있다). 예외를 밖으로 새지 않되, 빠짐·중복·모호·모순된 묶음은 거부한다.
    """
    media_by_resource = _media_by_resource(document, media)
    unavailable, by_source = _indexed_analyses(traces, media_by_resource)
    resolutions = {
        resource.scoped_id: _resolve_media(
            media_by_resource[resource.scoped_id], unavailable, by_source
        )
        for resource in document.resources
    }
    cues_by_source, ordered_cues, unavailable_audio = _resolved_cues(document, resolutions)
    placed, boundary_instances = _place_storyline(document, resolutions, cues_by_source)
    final, unresolved_ids = _split_by_representability(placed)
    emitted = [cue for cue in final if SUBMS_COLLAPSED not in cue.flags]
    return SourceCaptionSet(
        input_sha256=document.input_sha256,
        source_cues=tuple(ordered_cues),
        placed_cues=tuple(final),
        srt_text=serialize_srt(emitted),
        boundary_instance_ids=tuple(boundary_instances),
        unresolved_cue_ids=tuple(unresolved_ids),
        unavailable_audio=tuple(unavailable_audio),
    )


def _resolved_cues(
    document: ProjectDocument, resolutions: dict[str, SourceAnalysis]
) -> tuple[dict[str, tuple[SourceCue, ...]], list[SourceCue], list[UnavailableSourceAudio]]:
    """문서 자원 순서로 소스 큐와 unavailable 기록을 모은다 — 결과 순서의 정본."""
    cues_by_source: dict[str, tuple[SourceCue, ...]] = {}
    ordered: list[SourceCue] = []
    unavailable_audio: list[UnavailableSourceAudio] = []
    for resource in document.resources:
        analysis = resolutions[resource.scoped_id]
        if isinstance(analysis, UnavailableSourceAudio):
            unavailable_audio.append(analysis)
        elif analysis.status == _STATUS_SUCCESS and analysis.source_id not in cues_by_source:
            cues = source_cues(analysis)
            cues_by_source[analysis.source_id] = cues
            ordered.extend(cues)
    return cues_by_source, ordered, unavailable_audio


def _place_storyline(
    document: ProjectDocument,
    resolutions: dict[str, SourceAnalysis],
    cues_by_source: dict[str, tuple[SourceCue, ...]],
) -> tuple[list[PlacedSourceCue], list[str]]:
    """평평한 스토리라인 순서 = 타임라인 순서. 음소거·비홀성·갭은 말을 내지 않는다."""
    assets = {resource.scoped_id: resource for resource in document.resources}
    placed: list[PlacedSourceCue] = []
    boundary_instances: list[str] = []
    for item in document.storyline:
        if not isinstance(item, ClipInstance):
            continue
        analysis = resolutions[item.resource_scoped_id]
        trace = analysis if isinstance(analysis, SourceTrace) else None
        if trace is None or trace.status != _STATUS_SUCCESS:
            continue
        if not item.enabled or item.src_enable != "all":
            continue
        _place_instance(
            document,
            item,
            assets[item.resource_scoped_id],
            trace,
            cues_by_source[trace.source_id],
            placed,
            boundary_instances,
        )
    return placed, boundary_instances


def _place_instance(
    document: ProjectDocument,
    clip: ClipInstance,
    asset: AssetResource,
    trace: SourceTrace,
    cues: tuple[SourceCue, ...],
    placed: list[PlacedSourceCue],
    boundary_instances: list[str],
) -> None:
    """한 인스턴스에 소스 큐를 놓는다 — 원점은 여기서 정확히 한 번 더해진다."""
    origin = trace.pcm_sample0_file_time
    shift = clip.offset - document.sequence_tc_start - clip.start
    clip_end = clip.start + clip.duration
    for cue in cues:
        asset_start = asset.start + origin + cue.start
        asset_end = asset.start + origin + cue.end
        if asset_end <= clip.start or asset_start >= clip_end:
            continue  # 이 인스턴스가 쓰는 소스 구간 밖이다 — 잘림이 아니다
        shown_start = max(asset_start, clip.start)
        shown_end = min(asset_end, clip_end)
        cut = shown_start != asset_start or shown_end != asset_end
        placed.append(
            PlacedSourceCue(
                cue_id=cue.cue_id,
                instance_id=clip.instance_id,
                source_id=cue.source_id,
                source_start=origin + cue.start,
                source_end=origin + cue.end,
                timeline_start=shift + shown_start,
                timeline_end=shift + shown_end,
                text=cue.text,
                flags=(*cue.flags, BOUNDARY_REQUIRED) if cut else cue.flags,
            )
        )
        if cut and clip.instance_id not in boundary_instances:
            boundary_instances.append(clip.instance_id)


def _split_by_representability(
    placed: list[PlacedSourceCue],
) -> tuple[list[PlacedSourceCue], list[str]]:
    """half-up 뒤 0이 되는 배치 큐를 갈라낸다 — 큐는 남기고 SRT만 뺀다."""
    final: list[PlacedSourceCue] = []
    unresolved: list[str] = []
    for cue in placed:
        if milliseconds_half_up(cue.timeline_end) > milliseconds_half_up(cue.timeline_start):
            final.append(cue)
            continue
        final.append(replace(cue, flags=(*cue.flags, SUBMS_COLLAPSED)))
        if cue.cue_id not in unresolved:
            unresolved.append(cue.cue_id)
    return final, unresolved
