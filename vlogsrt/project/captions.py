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

P6-03는 그 SOURCE 위에 **편집 계획을 통과하는 EDIT 투영**만 얹는다:

- **큐 부착은 run 단위다.** `retained`는 제거가 없는 블록 경계마다 쪼개지므로
  조각 하나 안에 들어가라고 요구하면 자르지 않은 경계를 건너는 큐가 잘못
  실패한다. 소스와 출력이 둘 다 연속인 `EditPlan.runs`가 정본이고 이동량은
  `run.output.start − run.input.start` 프레임이다. run 밖의 큐는 제거와 겹친
  것이다 — 부분 큐를 합성하지 않고 하드 실패한다.
- **EDIT 신원은 (cue_id, instance_ids)다.** 내부 칼날을 걸친 소스 큐는 인스턴스마다
  한 장이고 여러 칼날을 덮는 경계 큐는 하나다 — cue_id로 합치면 전자가 뭉개진다.
- **격자는 `plan.frame_duration`에서 온다.** 느슨한 매개변수를 받으면 플랜과 다른
  격자로 투영할 수 있으므로 계약이 그 매개변수를 없앴다.
- **접힘은 EDIT 시계에서 다시 잰다.** 상류 깃발은 증거로 남고 SRT에서 그 장만
  뺀다(P3-03·P4-04와 같은 선). 늘리거나 합치거나 프레임 틈을 지어내지 않는다.

순수한 조립이다. 호출·ffmpeg·파일 쓰기가 없다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import pairwise
from typing import TYPE_CHECKING

from vlogsrt.models import Cue
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import AssetResource, ClipInstance, ProjectDocument
from vlogsrt.project.speech import (
    SourceAnalysis,
    SourceTrace,
    UnavailableSourceAudio,
)
from vlogsrt.project.timebase import FrameInterval, format_srt_time, milliseconds_half_up

if TYPE_CHECKING:
    # 편집 계층은 이 모듈을 import한다(boundary → captions) — 실행 시점 import는
    # 순환되므로 형에만 잡고, 실행 시 필요한 ReviewIssue는 함수 안에서 받는다.
    from vlogsrt.project.boundary import BoundaryProjection, ProjectCue
    from vlogsrt.project.edit import EditPlan, RemovedInterval, RetainedRun, ReviewIssue

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
    resources = {resource.scoped_id: resource for resource in document.resources}
    indexed: dict[str, ResolvedMedia] = {}
    for item in media:
        resource = resources.get(item.resource_scoped_id)
        if resource is None:
            raise ValueError(f"문서에 없는 자원을 가진 미디어다: {item.resource_scoped_id!r}")
        if item.resource_scoped_id in indexed:
            raise ValueError(
                f"미디어 자원이 중복이다: {item.resource_scoped_id!r} — 자원마다 정확히 하나다"
            )
        if item.has_audio != resource.has_audio:
            # 가청 선언의 정본은 문서의 asset이다 — 미디어 행이 문서와 어긋나면
            # 해석이 미디어를 따라가는 순간 조용한 무음·조용한 대사가 된다.
            raise ValueError(
                f"미디어의 가청 선언이 문서와 다르다: resource {resource.xml_id!r}는"
                f" hasAudio={int(resource.has_audio)}인데 media.has_audio={item.has_audio}"
                " — 자원의 has_audio는 문서의 asset 선언 그대로여야 한다"
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


# ═══════════════════════════════════════════════════════════════
#  P6-03 — 편집 계획을 통과하는 EDIT 투영
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class EditCaptionSet:
    """편집 계획을 통과한 자막 한 벌 — 리플 출력 타임라인의 EDIT SRT.

    `cues`의 `timeline_*`은 **출력** 경과 시각(0기반)이고 나머지 필드는 출처
    큐의 그대로다. `review`는 계획의 구조화 리뷰를 글자 그대로·순서 그대로
    실고 EDIT 증거를 뒤에 붙인다.
    """

    plan_revision: str
    cues: tuple[ProjectCue, ...]
    srt_text: str
    review: tuple[ReviewIssue, ...]


def project_edit_captions(plan: EditPlan, source: BoundaryProjection) -> EditCaptionSet:
    """SOURCE 큐를 편집 계획의 리플 출력으로 투영해 EDIT SRT를 낸다.

    순수한 투영이다 — 모델 호출·재전사·글자 합성이 없고 SOURCE 기록은 하나도
    고치지 않는다. 계획과 출처가 같은 문서·잠금·부재를 말하는지 먼저 보고,
    큐는 run에 붙여 리플 이동량만 더한다. 제거와 겹치는 큐는 부분 큐를
    지어내지 않고 하드 실패한다.
    """
    _require_edit_revision(plan, source)
    fd = plan.frame_duration
    runs_by_instance = _runs_by_instance(plan)
    removed_by_instance = _removed_by_instance(plan)
    rippled: list[ProjectCue] = []
    for cue in source.cues:
        shift = _edit_shift(runs_by_instance, removed_by_instance, cue, fd)
        rippled.append(
            replace(
                cue,
                timeline_start=cue.timeline_start + shift,
                timeline_end=cue.timeline_end + shift,
            )
        )
    ordered = _ordered_edit_cues(rippled)
    cues, collapsed = _edit_representability(ordered)
    return EditCaptionSet(
        plan_revision=plan.revision,
        cues=cues,
        srt_text=_edit_srt(cues),
        review=(*plan.review, *collapsed),
    )


def _require_edit_revision(plan: EditPlan, source: BoundaryProjection) -> None:
    """신원 대조 셋 — 문서 해시·잠금·부재 기록이 어긋나면 섞인 리비전이다.

    부재 기록은 튜플 그대로 비교한다: stale·누락·재정렬 전부 섞인 입력이므로
    조용히 자막을 내지 않고 닫는다.
    """
    if plan.input_sha256 != source.input_sha256:
        raise ValueError(
            f"계획의 문서 해시가 출처와 다르다: {plan.input_sha256} ≠"
            f" {source.input_sha256} — 섞인 리비전이다"
        )
    if plan.locked_instance_ids != source.locked_instance_ids:
        mismatched = sorted(plan.locked_instance_ids ^ source.locked_instance_ids)
        raise ValueError(
            f"계획의 잠긴 인스턴스가 출처와 다르다: 어긋난 인스턴스 {mismatched} — 섞인 리비전이다"
        )
    if plan.unavailable_instances != source.unavailable_instances:
        raise ValueError(
            "계획의 부재 기록이 출처와 정확히 같지 않다: 계획"
            f" {len(plan.unavailable_instances)}건, 출처"
            f" {len(source.unavailable_instances)}건 — stale·누락·재정렬은"
            " 섞인 리비전이다"
        )


def _runs_by_instance(plan: EditPlan) -> dict[str, tuple[RetainedRun, ...]]:
    """런을 인스턴스로 묶는다 — 계획 순서(곧 소스 순서)를 그대로 지킨다."""
    grouped: dict[str, list[RetainedRun]] = {}
    for run in plan.runs:
        grouped.setdefault(run.instance_id, []).append(run)
    return {instance: tuple(runs) for instance, runs in grouped.items()}


def _removed_by_instance(plan: EditPlan) -> dict[str, tuple[RemovedInterval, ...]]:
    """제거 구간을 인스턴스로 묶는다 — 진단이 이름을 댈 수 있게 한다."""
    grouped: dict[str, list[RemovedInterval]] = {}
    for row in plan.removed:
        grouped.setdefault(row.instance_id, []).append(row)
    return {instance: tuple(rows) for instance, rows in grouped.items()}


def _edit_shift(
    runs_by_instance: dict[str, tuple[RetainedRun, ...]],
    removed_by_instance: dict[str, tuple[RemovedInterval, ...]],
    cue: ProjectCue,
    fd: Fraction,
) -> Fraction:
    """큐 한 장의 리플 이동량 — 인스턴스 수로 갈라 단일·경계 규칙을 적용한다."""
    if len(cue.instance_ids) == 1:
        return _single_instance_shift(runs_by_instance, removed_by_instance, cue, fd)
    return _whole_clip_shift(runs_by_instance, cue, fd)


def _single_instance_shift(
    runs_by_instance: dict[str, tuple[RetainedRun, ...]],
    removed_by_instance: dict[str, tuple[RemovedInterval, ...]],
    cue: ProjectCue,
    fd: Fraction,
) -> Fraction:
    """단일 인스턴스 큐 — 제거를 먼저 보고, 그다음 run 포함을 본다.

    P6-03은 시그니처에 문서도 guard도 없어 `validate_edit_plan`을 부를 수
    없다. 그래서 `runs`와 `removed`가 서로소라고 가정하지 않는다 — 제거는
    모든 큐에 대해 run 포함보다 먼저 보므로 위조 플랜의 run이 제거 위를
    덮어도 큐를 내보내지 않는다. `retained`는 제거가 없는 블록 경계마다
    쪼개지므로 조각을 요구하지 않고, run 밖 진단은 두 번째 실패로 남는다.
    """
    instance = cue.instance_ids[0]
    runs = runs_by_instance.get(instance)
    if runs is None:
        raise ValueError(
            f"큐의 인스턴스가 계획에 없다: {instance!r} (cue {cue.cue_id!r}) — 지어낼 run이 없다"
        )
    _require_not_removed(removed_by_instance, cue, instance, fd)
    for run in runs:
        lo, hi = _frames_seconds(run.input_timeline_frames, fd)
        if lo <= cue.timeline_start and cue.timeline_end <= hi:
            return _run_shift(run, fd)
    raise ValueError(
        f"계획의 어느 run도 이 큐를 덮지 않는다: cue {cue.cue_id!r}"
        f" (인스턴스 {instance!r}, [{cue.timeline_start}, {cue.timeline_end})초)"
        " — 제거도 없이 run 밖에 있는 큐는 섞인 입력이다"
    )


def _require_not_removed(
    removed_by_instance: dict[str, tuple[RemovedInterval, ...]],
    cue: ProjectCue,
    instance: str,
    fd: Fraction,
) -> None:
    """제거 구간과 겹치는 큐는 하드 실패다 — 합성·재전사로 부분 큐를 만들지 않는다."""
    for row in removed_by_instance.get(instance, ()):
        lo, hi = _frames_seconds(row.input_timeline_frames, fd)
        if cue.timeline_start < hi and lo < cue.timeline_end:
            raise ValueError(
                f"큐가 제거 구간과 겹친다: cue {cue.cue_id!r} (인스턴스 {instance!r})의"
                f" [{cue.timeline_start}, {cue.timeline_end})초가 제거 [{lo}, {hi})초와"
                " 겹친다 — 부분 큐를 지어내지 않고 실패한다"
            )


def _whole_clip_shift(
    runs_by_instance: dict[str, tuple[RetainedRun, ...]],
    cue: ProjectCue,
    fd: Fraction,
) -> Fraction:
    """다중 인스턴스(경계) 큐 — 인스턴스마다 통째 유지 run 하나, 출력 연속·같은 이동량.

    경계 요청에 참여한 인스턴스는 잠겨 있으므로 run이 하나씩이다. 그것들이
    출력에서 연속이고 같은 이동량을 가질 때만 큐가 그 합집합에 붙는다 —
    사이에 제거가 끼어들면 큐가 잘리므로 하드 실패다.
    """
    if not cue.instance_ids:
        raise ValueError(f"큐에 인스턴스가 없다: cue {cue.cue_id!r} — 붙일 run이 없다")
    whole: list[RetainedRun] = []
    for instance in cue.instance_ids:
        runs = runs_by_instance.get(instance)
        if runs is None:
            raise ValueError(f"경계 큐의 인스턴스가 계획에 없다: {instance!r} (cue {cue.cue_id!r})")
        if len(runs) != 1:
            raise ValueError(
                f"경계 큐의 인스턴스가 통째 유지 run 하나가 아니다: {instance!r}이(가)"
                f" run {len(runs)}개다 (cue {cue.cue_id!r}) — 제거가 있으면 경계 큐가"
                " 잘리므로 실패다"
            )
        whole.append(runs[0])
    ordered = sorted(whole, key=lambda run: run.output_timeline_frames.start_frame)
    for previous, following in pairwise(ordered):
        butted = (
            previous.output_timeline_frames.end_frame
            != following.output_timeline_frames.start_frame
        )
        if butted:
            raise ValueError(
                f"경계 큐의 인스턴스들이 출력에서 연속이 아니다: cue {cue.cue_id!r}"
                f" (인스턴스 {[run.instance_id for run in ordered]})"
                " — 사이에 제거가 끼어들었다"
            )
    shifts = {_run_shift(run, fd) for run in ordered}
    if len(shifts) != 1:
        raise ValueError(
            f"경계 큐의 인스턴스들이 같은 이동량이 아니다: cue {cue.cue_id!r},"
            f" 이동량 {sorted(shifts)} — 합집합이 잘못된 세계다"
        )
    lo, _ = _frames_seconds(ordered[0].input_timeline_frames, fd)
    _, last_hi = _frames_seconds(ordered[-1].input_timeline_frames, fd)
    if cue.timeline_start < lo or cue.timeline_end > last_hi:
        raise ValueError(
            f"경계 큐가 통째 유지 합집합 안에 있지 않다: cue {cue.cue_id!r}의"
            f" [{cue.timeline_start}, {cue.timeline_end})초가 [{lo}, {last_hi})초를"
            " 벗어난다 — 제거를 건너는 경계 큐다"
        )
    return shifts.pop()


def _run_shift(run: RetainedRun, fd: Fraction) -> Fraction:
    """run의 리플 이동량 — 입력 창에서 출력 창으로 밀린 프레임 수 × 프레임 길이."""
    return (
        Fraction(run.output_timeline_frames.start_frame - run.input_timeline_frames.start_frame)
        * fd
    )


def _frames_seconds(window: FrameInterval, fd: Fraction) -> tuple[Fraction, Fraction]:
    """프레임 창의 초 끝점 — 격자 인덱스 n은 곧 n × 프레임 길이다."""
    return Fraction(window.start_frame) * fd, Fraction(window.end_frame) * fd


def _ordered_edit_cues(cues: Sequence[ProjectCue]) -> tuple[ProjectCue, ...]:
    """리플 뒤 순서 확인 — 정렬은 순서를 고치는 것이 아니라 다시 확인하는 것이다.

    이동량은 이야기 순서를 따라 단조롭지 않게 내려가므로(제거가 누적된다)
    정상 입력은 이미 순서대로다. 닿는 경계는 허용하고 겹침은 닫는다.
    """
    ordered = tuple(sorted(cues, key=lambda cue: (cue.timeline_start, cue.timeline_end)))
    previous_end: Fraction | None = None
    for cue in ordered:
        if cue.timeline_end <= cue.timeline_start:
            raise ValueError(
                f"리플 뒤 큐의 길이가 0 이하다: cue {cue.cue_id!r},"
                f" [{cue.timeline_start}, {cue.timeline_end})초"
            )
        if previous_end is not None and cue.timeline_start < previous_end:
            raise ValueError(
                f"리플 뒤 큐가 서로 겹친다: cue {cue.cue_id!r}의 시작"
                f" {cue.timeline_start}초가 앞 큐 끝 {previous_end}초보다 앞이다"
            )
        previous_end = cue.timeline_end
    return ordered


def _edit_representability(
    cues: Sequence[ProjectCue],
) -> tuple[tuple[ProjectCue, ...], tuple[ReviewIssue, ...]]:
    """EDIT 시계에서 접힘을 다시 잰다 — 큐는 남기고 SRT에서 그 장만 뺀다.

    상류 `subms_collapsed` 깃발은 그대로 둔다(P4-04와 같은 선). 리플 이동량이
    밀리초 정수가 아닌 격자(1001계)에서는 접힘이 생기기도 하고 풀리기도 한다 —
    그래서 여기서 다시 잰다. 리뷰는 미위치로 남는다: 마커 근원을 지어내지
    않고 최종 수용을 막는다.
    """
    from vlogsrt.project.edit import ReviewIssue

    final: list[ProjectCue] = []
    issues: list[ReviewIssue] = []
    for cue in cues:
        if milliseconds_half_up(cue.timeline_end) > milliseconds_half_up(cue.timeline_start):
            final.append(cue)
            continue
        final.append(_with_subms_flag(cue))
        issues.append(
            ReviewIssue(
                code=SUBMS_COLLAPSED,
                message=(
                    "EDIT 리플 뒤 밀리초로 나타낼 수 없다 — cue"
                    f" {cue.cue_id} (인스턴스 {','.join(cue.instance_ids)}):"
                    f" start={_fraction_text(cue.timeline_start)}초와"
                    f" end={_fraction_text(cue.timeline_end)}초가 모두"
                    f" {milliseconds_half_up(cue.timeline_start)}ms로 반올림된다"
                ),
                instance_id=None,
                source_frame=None,
            )
        )
    return tuple(final), tuple(issues)


def _with_subms_flag(cue: ProjectCue) -> ProjectCue:
    """접힘 깃발을 얹는다 — 이미 있으면 그대로(상류 증거를 중복하지 않는다)."""
    if SUBMS_COLLAPSED in cue.flags:
        return cue
    return replace(cue, flags=(*cue.flags, SUBMS_COLLAPSED))


def _edit_srt(cues: Sequence[ProjectCue]) -> str:
    """EDIT SRT — 경과 0기반, half-up은 직렬화 순간에 딱 한 번, 접힌 장만 뺀다."""
    blocks: list[str] = []
    for cue in cues:
        if SUBMS_COLLAPSED in cue.flags:
            continue
        if milliseconds_half_up(cue.timeline_end) <= milliseconds_half_up(cue.timeline_start):
            raise ValueError(
                f"직렬화할 수 없는 큐가 깃발 없이 남았다: {cue.cue_id!r} — 접힘 검사를 우회했다"
            )
        number = len(blocks) + 1
        timeline = f"{format_srt_time(cue.timeline_start)} --> {format_srt_time(cue.timeline_end)}"
        blocks.append(f"{number}\n{timeline}\n{cue.text}\n")
    return "\n".join(blocks)


def _fraction_text(value: Fraction) -> str:
    """정확 끝점의 직렬 — 분모까지 담아야 같은 값이 늘 같은 글자가 된다."""
    return f"{value.numerator}/{value.denominator}"
