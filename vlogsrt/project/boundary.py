"""경계 전사가 소비할 논리 슬라이스 — 원본 칼날을 잃지 않는 이웃 합침 (P4-01).

계획("Boundary transcripts" ＋ "Source analysis, clocks, identity")의 정본:

- **시각은 XML 그대로다.** 파일 시각은 `clip.start - asset.start`, 입력 경과는
  `clip.offset - sequence.tcStart`. 전체 사상
  `elapsed = clip.offset - tcStart + asset.start + file_time - clip.start`이
  클립 양 끝점에서 정확히 왕복한다 — 그 밖의 계산은 없다.
- **가청은 저작 상태만 본다.** 정확히 `media.has_audio and clip.enabled and
  clip.src_enable == "all"`이다. 전사·PCM·VAD의 이후 상태는 이 함수의 입력에
  아예 없다. 선언된 무음·비활성·영상 전용 슬라이스는 신원과 칼날을 그대로 가진
  채 `audible=False`로 남는다 — 선언된 오디오는 나중에 PCM이 없어도 가청이다.
- **합침은 이웃 판정이다.** 같은 자원·같은 내용·같은 원점에서 파일과 타임라인
  양쪽이 이어지고 가청 정책이 같을 때만 이웃 클립을 합친다. 자원 신원이 다르면
  내용이 같아도 합치지 않는다. 갭은 합침을 끊고 슬라이스가 되지 않는다 — 갭은
  문서가 표현한다. XML 칼날은 합친 뒤에도 전부 `blades`에 남는다.

순수 함수다 — 네트워크·ffmpeg·파일 쓰기·전사가 없다.

P4-02는 그 슬라이스 위에서 경계 전사 **요청**을 계획한다:

- **세그먼트가 온전하면 발표만 묶는다.** 큐가 가리키는 골격 세그먼트가 슬라이스에
  온전히 들어 있으면 조판만 묶이고 재요청은 없다. 세그먼트가 입력 자름점에서
  잘리는 순간 요청이 시작되고, 겹치는 큐·세그먼트 의존은 범위가 더 자라지 않을
  때까지 흡수된다(고정점). 요청 경계는 남긴 슬라이스로 묶이고, 저장 PCM 환산은
  원점을 정확히 한 번 뺀 오디오 국소 시계에서 ceil-start/floor-end로 안쪽만 간다.
- **묶음은 자원 먼저다.** 자원 스코프 unavailable 기록이 있으면 그 자원의
  인스턴스마다 타입 기록과 잠금만 남는다 — 같은 내용의 건강한 트레이스가 있어도
  그 기록을 넘지 않는다. post-PCM 실패 트레이스는 unavailable로 강등되지 않고
  참조 증명과 잠금만 남는다.

이 계획은 요청을 만들 뿐이다 — 모델 호출·오디오 추출·큐 교체가 없다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Literal, TypeVar

from vlogsrt.project.captions import PlacedSourceCue, SourceCaptionSet, SourceCue
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import AssetResource, ClipInstance, ProjectDocument
from vlogsrt.project.pcm import PcmReference, SampleRange, retained_sample_range
from vlogsrt.project.speech import SourceTrace, UnavailableSourceAudio

_SLICE_ID_DOMAIN = "vlogsrt-logical-slice-v1"
"""슬라이스 신원의 도메인 구분자 — 인스턴스·리소스 해시와 우연히 겹치지 않게 한다."""

_REQUEST_ID_DOMAIN = "vlogsrt-boundary-request-v1"
"""경계 요청 신원의 도메인 구분자 — 다른 해시 재료와 우연히 겹치지 않게 한다."""

_STATUS_SUCCESS = "success"
_STATUS_ERROR = "error"

_T = TypeVar("_T")

__all__ = [
    "AuthoredBlade",
    "LogicalSlice",
    "logical_slices",
    "ProjectCue",
    "BoundaryRequest",
    "UnavailableInstance",
    "BoundaryPlan",
    "plan_boundaries",
]


@dataclass(frozen=True, slots=True)
class AuthoredBlade:
    """저작된 `asset-clip` 한 칼날 — 합쳐진 뒤에도 XML의 자리를 정확히 가리킨다."""

    instance_id: str
    source_time: Fraction
    timeline_time: Fraction


@dataclass(frozen=True, slots=True)
class LogicalSlice:
    """논리 슬라이스 — 경계 작업이 보는 재생 단위. 신원은 `slice_id`다."""

    slice_id: str
    instance_ids: tuple[str, ...]
    resource_scoped_id: str
    source_id: str
    asset_start: Fraction
    source_start: Fraction
    source_end: Fraction
    timeline_start: Fraction
    timeline_end: Fraction
    audible: bool
    blades: tuple[AuthoredBlade, ...]


@dataclass(frozen=True, slots=True)
class _Blade:
    """합침 판정의 재료 — 칼날 하나의 문맥과 파생 끝점. 공개 기록은 봉인이 만든다."""

    clip: ClipInstance
    resource: AssetResource
    media: ResolvedMedia
    source_time: Fraction
    source_end: Fraction
    timeline_time: Fraction
    timeline_end: Fraction
    audible: bool


def logical_slices(
    document: ProjectDocument, media: Sequence[ResolvedMedia]
) -> tuple[LogicalSlice, ...]:
    """스토리라인을 논리 슬라이스로 도출한다 — 문서 순서 그대로, 갭은 슬라이스가 아니다."""
    media_by_resource = _media_by_resource(document, media)
    resources_by_scoped = {resource.scoped_id: resource for resource in document.resources}
    slices: list[LogicalSlice] = []
    group: list[_Blade] = []
    for item in document.storyline:
        if not isinstance(item, ClipInstance):
            # 갭은 합침을 끊는다 — 갭의 시간은 문서가 표현하므로 슬라이스로 내지 않는다.
            _seal(group, slices)
            group = []
            continue
        blade = _blade(
            item,
            resources_by_scoped[item.resource_scoped_id],
            media_by_resource[item.resource_scoped_id],
            document.sequence_tc_start,
        )
        if group and not _continues(group[-1], blade):
            _seal(group, slices)
            group = []
        group.append(blade)
    _seal(group, slices)
    return tuple(slices)


def _media_by_resource(
    document: ProjectDocument, media: Sequence[ResolvedMedia]
) -> dict[str, ResolvedMedia]:
    """미디어를 자원 신원으로 묶는다 — 빠짐·중복·모순은 거부한다(3-03과 같은 선)."""
    resources_by_scoped = {resource.scoped_id: resource for resource in document.resources}
    indexed: dict[str, ResolvedMedia] = {}
    for row in media:
        resource = resources_by_scoped.get(row.resource_scoped_id)
        if resource is None:
            raise ValueError(f"문서에 없는 자원을 가진 미디어다: {row.resource_scoped_id!r}")
        if row.resource_scoped_id in indexed:
            raise ValueError(
                f"미디어 자원이 중복이다: {row.resource_scoped_id!r} — 자원마다 정확히 하나다"
            )
        if row.has_audio != resource.has_audio:
            # 가청 선언의 정본은 문서의 asset이다 — 미디어 행이 어긋나면 조용한
            # 무음·조용한 대사가 되므로 고르지 않고 거부한다.
            raise ValueError(
                f"미디어의 가청 선언이 문서와 다르다: resource {resource.xml_id!r}는"
                f" hasAudio={int(resource.has_audio)}인데 media.has_audio={row.has_audio}"
                " — 자원의 has_audio는 문서의 asset 선언 그대로여야 한다"
            )
        indexed[row.resource_scoped_id] = row
    for item in document.storyline:
        if isinstance(item, ClipInstance) and item.resource_scoped_id not in indexed:
            missing = resources_by_scoped[item.resource_scoped_id]
            raise ValueError(
                f"클립이 가리키는 자원에 미디어가 없다: resource {missing.xml_id!r}"
                f" ({missing.scoped_id!r}) — 스토리라인의 모든 클립 자원이 해석되어야 한다"
            )
    return indexed


def _blade(
    clip: ClipInstance,
    resource: AssetResource,
    media: ResolvedMedia,
    tc_start: Fraction,
) -> _Blade:
    """칼날 하나의 시계와 가청 — 공식 그대로, 가청은 저작 상태만 본다."""
    source_time = clip.start - resource.start
    timeline_time = clip.offset - tc_start
    return _Blade(
        clip=clip,
        resource=resource,
        media=media,
        source_time=source_time,
        source_end=source_time + clip.duration,
        timeline_time=timeline_time,
        timeline_end=timeline_time + clip.duration,
        audible=media.has_audio and clip.enabled and clip.src_enable == "all",
    )


def _continues(previous: _Blade, following: _Blade) -> bool:
    """이웃 합침 판정 — 여섯 조건이 전부 성립해야 한다(계획 "Boundary transcripts")."""
    return (
        previous.clip.resource_scoped_id == following.clip.resource_scoped_id
        and previous.media.asset_content_id == following.media.asset_content_id
        and previous.resource.start == following.resource.start
        and previous.source_end == following.source_time
        and previous.timeline_end == following.timeline_time
        and previous.audible == following.audible
    )


def _seal(group: list[_Blade], slices: list[LogicalSlice]) -> None:
    """쌓인 이웃을 슬라이스 하나로 봉인한다 — 빈 그룹은 그냥 지나친다."""
    if not group:
        return
    first, last = group[0], group[-1]
    instance_ids = tuple(blade.clip.instance_id for blade in group)
    slices.append(
        LogicalSlice(
            slice_id=_slice_id(
                instance_ids,
                first.source_time,
                last.source_end,
                first.timeline_time,
                last.timeline_end,
            ),
            instance_ids=instance_ids,
            resource_scoped_id=first.clip.resource_scoped_id,
            source_id=first.media.asset_content_id,
            asset_start=first.resource.start,
            source_start=first.source_time,
            source_end=last.source_end,
            timeline_start=first.timeline_time,
            timeline_end=last.timeline_end,
            audible=first.audible,
            blades=tuple(
                AuthoredBlade(
                    instance_id=blade.clip.instance_id,
                    source_time=blade.source_time,
                    timeline_time=blade.timeline_time,
                )
                for blade in group
            ),
        )
    )


def _slice_id(
    instance_ids: tuple[str, ...],
    source_start: Fraction,
    source_end: Fraction,
    timeline_start: Fraction,
    timeline_end: Fraction,
) -> str:
    """슬라이스 신원 — 순서 있는 인스턴스와 정확한 네 끝점의 결정적 해시다."""
    material = "\0".join(
        (
            _SLICE_ID_DOMAIN,
            *instance_ids,
            _exact(source_start),
            _exact(source_end),
            _exact(timeline_start),
            _exact(timeline_end),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _exact(value: Fraction) -> str:
    """끝점의 정확한 직렬 — 분모까지 담아야 같은 값이 늘 같은 글자가 된다."""
    return f"{value.numerator}/{value.denominator}"


# ═══════════════════════════════════════════════════════════════
#  P4-02 — 경계 전사 요청 계획 (고정점 폐쇄·자원 먼저 묶음)
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ProjectCue:
    """논리 슬라이스 위의 자막 한 장 — 배치 큐의 발표와 원본 기록을 함께 싣는다.

    `source_start/source_end`은 P3가 남긴 파일 시계 **원본 기록**이고
    `timeline_start/timeline_end`은 발표 시각이다. `provenance`는 아직
    'source'뿐이다 — 경계 전사 결과는 P4-03 이후에 'boundary'로 온다.
    """

    cue_id: str
    slice_id: str
    instance_ids: tuple[str, ...]
    source_id: str
    source_start: Fraction
    source_end: Fraction
    timeline_start: Fraction
    timeline_end: Fraction
    text: str
    provenance: Literal["source", "boundary"]
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundaryRequest:
    """다시 물을 경계 구간 하나 — 요청 경계는 파일 시계, 실측은 저장 프레임이다.

    `source_start/source_end`는 폐쇄가 요청한 경계 그대로고 `sample_range`는
    환산(ceil-start/floor-end) 뒤 실제 남는 프레임이다 — 둘은 다른 기록이다.
    실측 끝점의 파일 시계는 `Fraction(프레임, 주파수) + 참조 원점`으로 되돌린다.
    """

    request_id: str
    slice_id: str
    instance_ids: tuple[str, ...]
    source_id: str
    pcm_analysis_key: str
    source_start: Fraction
    source_end: Fraction
    sample_range: SampleRange
    source_segment_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnavailableInstance:
    """오디오가 없는 자원의 인스턴스 한 개 — 타입 있는 상태이지 빠진 큐가 아니다.

    `declared_no_audio`는 완결된 비과금 부재고 나머지 둘은 되풀이 가능한 실패다.
    셋 모두 제거 권한이 없다 — 문장으로 다시 지어낸 부재가 아니라 기록이다.
    """

    resource_scoped_id: str
    instance_id: str
    reason_code: Literal[
        "declared_no_audio",
        "stream_unavailable",
        "pcm_unavailable",
    ]


@dataclass(frozen=True, slots=True)
class BoundaryPlan:
    """한 번의 경계 계획 — 묶인 큐·요청·잠금·부재가 전부다."""

    clamped_cues: tuple[ProjectCue, ...]
    requests: tuple[BoundaryRequest, ...]
    locked_instance_ids: frozenset[str]
    unavailable_instances: tuple[UnavailableInstance, ...]


@dataclass(frozen=True, slots=True)
class _Seed:
    """잘린 큐에서 나온 폐쇄 씨앗 — 어느 슬라이스의 어떤 소스 범위다."""

    slice_: LogicalSlice
    trace: SourceTrace
    reference: PcmReference
    start: Fraction
    end: Fraction


@dataclass
class _Bucket:
    """한 슬라이스의 씨앗 묶음 — 슬라이스마다 정확히 하나의 소스 문맥만 있다."""

    slice_: LogicalSlice
    trace: SourceTrace
    reference: PcmReference
    spans: list[tuple[Fraction, Fraction]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _SourceDeps:
    """한 소스의 폐쇄 재료 — 배치가 끝난 큐와 골격 세그먼트의 파일 시계 범위."""

    cue_ranges: tuple[tuple[Fraction, Fraction], ...]
    segments_by_id: dict[str, tuple[Fraction, Fraction]]

    @property
    def ranges(self) -> tuple[tuple[Fraction, Fraction], ...]:
        return (*self.cue_ranges, *self.segments_by_id.values())


class _Dependencies:
    """폐쇄 재료의 조회표 — 소스 큐·세그먼트를 신원으로만 묶는다."""

    def __init__(self, source: SourceCaptionSet) -> None:
        self._source = source
        self._cues_by_id = _index_by(
            source.source_cues, "소스 큐 신원이 중복된다", lambda cue: cue.cue_id
        )
        self._by_source: dict[str, _SourceDeps] = {}

    def source_cue(self, cue_id: str) -> SourceCue:
        cue = self._cues_by_id.get(cue_id)
        if cue is None:
            raise ValueError(f"배치 큐의 소스 큐가 없다: {cue_id!r} — 지어내지 않는다")
        return cue

    def for_trace(self, trace: SourceTrace) -> _SourceDeps:
        deps = self._by_source.get(trace.source_id)
        if deps is None:
            deps = self._build(trace)
            self._by_source[trace.source_id] = deps
        return deps

    def _build(self, trace: SourceTrace) -> _SourceDeps:
        """원점은 여기서 정확히 한 번 더해 파일 시계 범위를 만든다."""
        origin = trace.pcm_sample0_file_time
        cue_ranges = tuple(
            (origin + cue.start, origin + cue.end)
            for cue in self._source.source_cues
            if cue.source_id == trace.source_id
        )
        segments_by_id: dict[str, tuple[Fraction, Fraction]] = {}
        for segment in trace.settled_spine:
            if segment.segment_id in segments_by_id:
                raise ValueError(
                    f"골격 세그먼트 신원이 중복된다: {segment.segment_id!r}"
                    f" (source_id={trace.source_id!r})"
                )
            segments_by_id[segment.segment_id] = (
                min(segment.file_start, segment.file_end),
                max(segment.file_start, segment.file_end),
            )
        return _SourceDeps(cue_ranges, segments_by_id)


class _Linkage:
    """소스별 트레이스·PCM 참조 짝 — 신원으로만 묶고 어긋나면 닫는다."""

    def __init__(self, traces: Sequence[SourceTrace], references: Sequence[PcmReference]) -> None:
        self._traces = _index_by(
            traces, "소스 하나에 트레이스가 둘 이상이다", lambda t: t.source_id
        )
        self._references = _index_by(
            references, "소스 하나에 PCM 참조가 둘 이상이다", lambda r: r.source_id
        )
        self._pairs: dict[str, tuple[SourceTrace, PcmReference]] = {}

    def resolve(self, source_id: str) -> tuple[SourceTrace, PcmReference]:
        pair = self._pairs.get(source_id)
        if pair is not None:
            return pair
        trace = self._traces.get(source_id)
        if trace is None:
            raise ValueError(
                f"소스의 전사 트레이스가 없다: source_id={source_id!r} — 지어내지 않는다"
            )
        reference = self._references.get(source_id)
        if reference is None:
            raise ValueError(
                f"소스의 PCM 참조가 없다: source_id={source_id!r}"
                " — 경계 전사는 저장 PCM에서만 나온다"
            )
        self._require_pair(trace, reference)
        pair = (trace, reference)
        self._pairs[source_id] = pair
        return pair

    @staticmethod
    def _require_pair(trace: SourceTrace, reference: PcmReference) -> None:
        if reference.analysis_key != trace.pcm_analysis_key:
            raise ValueError(
                "PCM 참조의 분석 키가 트레이스와 다르다:"
                f" reference {reference.analysis_key!r} 대비 trace {trace.pcm_analysis_key!r}"
                f" (source_id={trace.source_id!r})"
            )
        if reference.pcm_sample0_file_time != trace.pcm_sample0_file_time:
            raise ValueError(
                "PCM 참조의 원점이 트레이스와 다르다:"
                f" reference {reference.pcm_sample0_file_time} 대비"
                f" trace {trace.pcm_sample0_file_time} (source_id={trace.source_id!r})"
            )


def _index_by(records: Iterable[_T], message: str, key: Callable[[_T], str]) -> dict[str, _T]:
    """기록을 신원으로 묶는다 — 중복은 곧 모호함이므로 그 자리에서 거부한다."""
    indexed: dict[str, _T] = {}
    for record in records:
        identity = key(record)
        if identity in indexed:
            raise ValueError(f"{message}: {identity!r}")
        indexed[identity] = record
    return indexed


def _slices_by_instance(slices: Sequence[LogicalSlice]) -> dict[str, LogicalSlice]:
    """인스턴스 → 슬라이스 조회표 — 인스턴스는 정확히 하나의 슬라이스에 산다."""
    by_instance: dict[str, LogicalSlice] = {}
    for slice_ in slices:
        for instance_id in slice_.instance_ids:
            if instance_id in by_instance:
                raise ValueError(f"인스턴스가 슬라이스 둘에 있다: {instance_id!r}")
            by_instance[instance_id] = slice_
    return by_instance


def _require_record_matches_slice(slice_: LogicalSlice, record: UnavailableSourceAudio) -> None:
    """unavailable 기록은 슬라이스와 신원·사유가 정확히 같아야 한다."""
    if record.source_id != slice_.source_id:
        raise ValueError(
            "unavailable 기록의 소스 신원이 슬라이스와 다르다:"
            f" {record.source_id!r} 대비 slice {slice_.source_id!r}"
            f" (resource {slice_.resource_scoped_id!r})"
        )
    if record.reason_code == "declared_no_audio" and slice_.audible:
        raise ValueError(
            "가청 슬라이스에 선언된 무음 기록이 왔다:"
            f" resource {slice_.resource_scoped_id!r} — 선언된 무음은 불가청 자원에만 온다"
        )


def _resolve_slices(
    slices: Sequence[LogicalSlice],
    unavailable: dict[str, UnavailableSourceAudio],
    linkage: _Linkage,
    locked: set[str],
) -> list[UnavailableInstance]:
    """자원 먼저 해석 — 부재는 타입 기록으로, post-PCM 실패는 잠금으로 남는다."""
    records: list[UnavailableInstance] = []
    for slice_ in slices:
        record = unavailable.get(slice_.resource_scoped_id)
        if record is not None:
            _require_record_matches_slice(slice_, record)
            for instance_id in slice_.instance_ids:
                records.append(
                    UnavailableInstance(
                        resource_scoped_id=slice_.resource_scoped_id,
                        instance_id=instance_id,
                        reason_code=record.reason_code,
                    )
                )
                locked.add(instance_id)
            continue
        if not slice_.audible:
            continue
        trace, _reference = linkage.resolve(slice_.source_id)
        if trace.status == _STATUS_ERROR:
            locked.update(slice_.instance_ids)
    return records


def _convert_placed(
    source: SourceCaptionSet,
    by_instance: dict[str, LogicalSlice],
    unavailable: dict[str, UnavailableSourceAudio],
    linkage: _Linkage,
    deps: _Dependencies,
) -> tuple[list[ProjectCue], list[_Seed]]:
    """배치 큐를 슬라이스 큐로 옮기고 참된 절단의 씨앗을 모은다."""
    clamped: list[ProjectCue] = []
    seeds: list[_Seed] = []
    for placed in source.placed_cues:
        slice_ = by_instance.get(placed.instance_id)
        if slice_ is None:
            raise ValueError(
                f"배치 큐의 인스턴스가 슬라이스에 없다: {placed.instance_id!r}"
                f" (cue {placed.cue_id!r})"
            )
        if slice_.resource_scoped_id in unavailable:
            continue  # 그 자원은 발표도 요청도 내놓지 않는다
        trace, reference = linkage.resolve(slice_.source_id)
        _require_success(placed.cue_id, slice_, trace)
        clamped.append(
            ProjectCue(
                cue_id=placed.cue_id,
                slice_id=slice_.slice_id,
                instance_ids=(placed.instance_id,),
                source_id=placed.source_id,
                source_start=placed.source_start,
                source_end=placed.source_end,
                timeline_start=placed.timeline_start,
                timeline_end=placed.timeline_end,
                text=placed.text,
                provenance="source",
                flags=placed.flags,
            )
        )
        span = _seed_span(placed, slice_, deps, trace)
        if span is not None:
            seeds.append(_Seed(slice_, trace, reference, span[0], span[1]))
    return clamped, seeds


def _require_success(cue_id: str, slice_: LogicalSlice, trace: SourceTrace) -> None:
    """배치 큐는 가청 슬라이스의 성공 트레이스에서만 온다 — 모순은 거부한다."""
    if not slice_.audible or trace.status != _STATUS_SUCCESS:
        raise ValueError(
            f"배치 큐와 슬라이스 상태가 모순이다: cue {cue_id!r},"
            f" slice {slice_.slice_id} audible={slice_.audible},"
            f" trace status={trace.status!r}"
        )


def _seed_span(
    placed: PlacedSourceCue, slice_: LogicalSlice, deps: _Dependencies, trace: SourceTrace
) -> tuple[Fraction, Fraction] | None:
    """큐가 가리키는 세그먼트가 하나라도 잘렸는가 — 씨앗은 잘린 증거의 둘레다."""
    source_cue = deps.source_cue(placed.cue_id)
    source_deps = deps.for_trace(trace)
    lo: Fraction | None = None
    hi: Fraction | None = None
    for segment_id in source_cue.segment_ids:
        segment = source_deps.segments_by_id.get(segment_id)
        if segment is None:
            raise ValueError(
                f"큐가 가리키는 세그먼트가 골격에 없다: {segment_id!r} (cue {placed.cue_id!r})"
            )
        seg_lo, seg_hi = segment
        if seg_lo >= slice_.source_start and seg_hi <= slice_.source_end:
            continue  # 이 세그먼트는 온전히 들어 있다 — 잘림이 아니다
        lo, hi = _grow(lo, hi, seg_lo, seg_hi)
    if lo is None or hi is None:
        return None  # 세그먼트가 다 온전하다 — 발표만 묶인다
    return _grow(lo, hi, placed.source_start, placed.source_end)


def _grow(
    lo: Fraction | None, hi: Fraction | None, start: Fraction, end: Fraction
) -> tuple[Fraction, Fraction]:
    """범위 둘레를 넓힌다 — 없던 둘레는 그 값이 된다."""
    if lo is None or hi is None:
        return start, end
    return min(lo, start), max(hi, end)


def _closure(
    ranges: Sequence[tuple[Fraction, Fraction]], seed: tuple[Fraction, Fraction]
) -> tuple[Fraction, Fraction]:
    """겹치는 의존을 흡수해 둘레가 자라지 않을 때까지 되풀이한다 — 고정점."""
    lo, hi = seed
    while True:
        grew = False
        for start, end in ranges:
            if start < hi and lo < end and (start < lo or end > hi):
                lo, hi = min(lo, start), max(hi, end)
                grew = True
        if not grew:
            return lo, hi


def _coalesced(
    spans: Sequence[tuple[Fraction, Fraction]],
) -> tuple[tuple[Fraction, Fraction], ...]:
    """같은 슬라이스의 닿거나 겹친 범위를 하나로 합친다 — 호환되는 병합만."""
    merged: list[list[Fraction]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((span[0], span[1]) for span in merged)


def _build_requests(seeds: Sequence[_Seed], deps: _Dependencies) -> tuple[BoundaryRequest, ...]:
    """씨앗을 슬라이스별로 폐쇄·병합해 불변 요청으로 봉인한다."""
    buckets: dict[str, _Bucket] = {}
    for seed in seeds:
        bucket = buckets.get(seed.slice_.slice_id)
        if bucket is None:
            bucket = _Bucket(seed.slice_, seed.trace, seed.reference)
            buckets[seed.slice_.slice_id] = bucket
        start, end = _closure(deps.for_trace(seed.trace).ranges, (seed.start, seed.end))
        bucket.spans.append(
            (
                max(start, seed.slice_.source_start),
                min(end, seed.slice_.source_end),
            )
        )
    requests: list[BoundaryRequest] = []
    for bucket in buckets.values():
        for start, end in _coalesced(bucket.spans):
            requests.append(_sealed_request(bucket, start, end, deps))
    return tuple(requests)


def _sealed_request(
    bucket: _Bucket, start: Fraction, end: Fraction, deps: _Dependencies
) -> BoundaryRequest:
    """봉인된 요청 하나 — 원점은 환산 직전에 정확히 한 번 뺀다."""
    reference = bucket.reference
    origin = reference.pcm_sample0_file_time
    samples = retained_sample_range(reference, start - origin, end - origin)
    source_deps = deps.for_trace(bucket.trace)
    segment_ids = tuple(
        segment.segment_id
        for segment in bucket.trace.settled_spine
        if _overlaps(source_deps.segments_by_id[segment.segment_id], start, end)
    )
    instance_ids = _participating_instances(bucket.slice_, start, end)
    return BoundaryRequest(
        request_id=_request_id(
            bucket.slice_.slice_id,
            instance_ids,
            bucket.trace.source_id,
            bucket.trace.pcm_analysis_key,
            start,
            end,
            samples,
            segment_ids,
        ),
        slice_id=bucket.slice_.slice_id,
        instance_ids=instance_ids,
        source_id=bucket.trace.source_id,
        pcm_analysis_key=bucket.trace.pcm_analysis_key,
        source_start=start,
        source_end=end,
        sample_range=samples,
        source_segment_ids=segment_ids,
    )


def _participating_instances(
    slice_: LogicalSlice, start: Fraction, end: Fraction
) -> tuple[str, ...]:
    """요청 범위와 겹치는 칼날의 인스턴스 — 그 슬라이스 안에서만 고른다."""
    return tuple(
        instance_id
        for instance_id, blade_start, blade_end in _blade_intervals(slice_)
        if _overlaps((blade_start, blade_end), start, end)
    )


def _blade_intervals(slice_: LogicalSlice) -> tuple[tuple[str, Fraction, Fraction], ...]:
    """칼날의 파일 시계 구간 — 합침이 보증하는 빈틈없음을 검사하고 쓴다."""
    intervals: list[tuple[str, Fraction, Fraction]] = []
    expected = slice_.source_start
    for index, blade in enumerate(slice_.blades):
        last = index == len(slice_.blades) - 1
        blade_end = slice_.source_end if last else slice_.blades[index + 1].source_time
        if blade.source_time != expected:
            raise ValueError(
                f"칼날이 슬라이스를 빈틈없이 이어지지 않는다: slice {slice_.slice_id},"
                f" blade {blade.instance_id!r} 시작 {blade.source_time} (기대 {expected})"
            )
        intervals.append((blade.instance_id, blade.source_time, blade_end))
        expected = blade_end
    if not intervals or expected != slice_.source_end:
        raise ValueError(
            f"칼날이 슬라이스 끝에 닿지 않는다: slice {slice_.slice_id},"
            f" 마지막 칼날 끝 {expected} (슬라이스 끝 {slice_.source_end})"
        )
    return tuple(intervals)


def _overlaps(span: tuple[Fraction, Fraction], start: Fraction, end: Fraction) -> bool:
    """반개 구간의 겹침 판정 — 닿기만 한 경계는 겹침이 아니다."""
    return span[0] < end and start < span[1]


def _request_id(
    slice_id: str,
    instance_ids: tuple[str, ...],
    source_id: str,
    pcm_analysis_key: str,
    start: Fraction,
    end: Fraction,
    samples: SampleRange,
    segment_ids: tuple[str, ...],
) -> str:
    """요청 신원 — 슬라이스·인스턴스·소스·PCM 키·정확한 경계·프레임·세그먼트의 해시."""
    material = "\0".join(
        (
            _REQUEST_ID_DOMAIN,
            slice_id,
            *instance_ids,
            source_id,
            pcm_analysis_key,
            _exact(start),
            _exact(end),
            str(samples.start),
            str(samples.end),
            *segment_ids,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def plan_boundaries(
    slices: Sequence[LogicalSlice],
    source: SourceCaptionSet,
    traces: Sequence[SourceTrace],
    references: Sequence[PcmReference],
) -> BoundaryPlan:
    """논리 슬라이스 위에서 경계 전사 요청을 계획한다.

    순수한 요청 계획이다 — 모델 호출·오디오 추출·큐 교체가 없다. 묶음은 자원
    신원이 먼저고 내용 신원은 그 다음이다. 모든 잠금(unavailable·post-PCM 실패·
    요청 참여)은 이 계획이 한 번에 모아 나간다.
    """
    by_instance = _slices_by_instance(slices)
    unavailable = _index_by(
        source.unavailable_audio,
        "자원 하나에 unavailable 기록이 둘 이상이다",
        lambda record: record.resource_scoped_id,
    )
    linkage = _Linkage(traces, references)
    deps = _Dependencies(source)
    locked: set[str] = set()
    unavailable_instances = _resolve_slices(slices, unavailable, linkage, locked)
    clamped, seeds = _convert_placed(source, by_instance, unavailable, linkage, deps)
    requests = _build_requests(seeds, deps)
    locked.update(instance_id for request in requests for instance_id in request.instance_ids)
    return BoundaryPlan(
        clamped_cues=tuple(clamped),
        requests=requests,
        locked_instance_ids=frozenset(locked),
        unavailable_instances=tuple(unavailable_instances),
    )
