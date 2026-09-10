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
- **연결과 병합은 신원으로 닫는다.** 배치 큐의 소스 신원은 슬라이스·소스 큐·
  트레이스와 정확히 같아야 한다 — 한 큐가 두 소스를 말하는 계획은 내놓지 않는다.
  엄격히 겹친 범위의 병합은 참여 칼날이 달라도 항상 합집합이다 — 같은 남긴
  프레임을 두 요청이 겹쳐 덮지 않는다. 닿기만 한 범위는 참여 칼날이 정확히 같을
  때만 합친다 — 다른 칼날에 앉은 닿은 범위는 각자 요청으로 남는다.
- **묶음은 자원 먼저다.** 자원 스코프 unavailable 기록이 있으면 그 자원의
  인스턴스마다 타입 기록과 잠금만 남는다 — 같은 내용의 건강한 트레이스가 있어도
  그 기록을 넘지 않는다. post-PCM 실패 트레이스는 unavailable로 강등되지 않고
  참조 증명과 잠금만 남는다.

이 계획은 요청을 만들 뿐이다 — 모델 호출·오디오 추출·큐 교체가 없다.

P4-03는 계획된 요청을 **실행**한다 — 위의 계획은 순수하게 남는다:

- **입력은 저장 PCM의 남긴 프레임 그대로다.** 요청의 `sample_range`를 바이트
  단위로 옮기고 그 밖의 프레임은 한 개도 안 들어온다. 재전사 조각도 같은 저장
  PCM에서 남긴 범위 안으로 묶여 잘린다 — 제거된 오디오의 덧붙임·문맥이 없다.
- **엔진은 P3 소스 분석과 같은 순서로 돈다.** 1차 전사 → 골격 → 되풀이 재전사 →
  불변 골격 → 큐 → 정제(조판 정리 포함, 정확히 한 번). 되풀이 재전사는 기본값
  그대로 살아 있고, 시각과 글자는 여전히 응답의 세그먼트에서만 온다.
- **시계는 세 층이다.** 엔진·정제는 경계 국소 시계(남긴 프레임 ÷ 주파수), 큐는
  국소 시각에 남긴 범위의 시작 프레임을 정확히 한 번 더한 디코드 오디오 국소
  시계, 파일 시계의 원점은 파일/XML 배치 때 한 번 더한다. 반올림은 SRT
  직렬화까지 없다.
- **실패는 조용한 성공이 아니다.** 호출·응답 계약 위반과 주입 커터 실패는 타입
  있는 error 기록이고 완성물은 나오지 않는다. 원본 소스 큐는 그대로 남는다.
- **경계는 재귀하지 않는다.** `cut_truncated`는 종결 메타데이터다 — 자기 끝점
  때문에 다시 경계 요청을 만들지 않지만 재전사·정제는 평범하게 받는다.

P4-04는 그 결과를 **갈아끼운다** — 위의 실행은 그대로 있고 투영만 더한다:

- **갈아끼우기는 정확히 (슬라이스, 인스턴스, 소스 구간) 스코프다.** 성공한 경계
  기록의 이미 정제된 큐가 그 스코프 안의 소스 큐를 대체한다 — 둘을 함께 내지
  않고 어느 쪽도 다시 정제·조판하지 않는다. 형제 인스턴스는 손대지 않는다.
- **타임라인 이동량은 묶인 큐의 두 시계에서 유도한다.** 슬라이스 안에서
  `타임라인 = 파일 시계 + k`의 k는 상수다(이웃 합침이 양쪽 시계를 같은 걸음으로
  이어놓았다). 경계 큐의 디코드 국소 시각에 원점을 정확히 한 번 더하고 k를 더해
  타임라인이 된다. 밀리초 검사는 교체 뒤에 다시 잰다 — 접힌 큐는 증거로 남고
  SRT에서 그 장만 뺀다(P3-03과 같은 선).
- **빈·실패 경계는 조용한 성공이 아니다.** 묶인 원문에 `boundary_unresolved`
  깃발과 요청 신원을 남기고 사람의 검토로 넘긴다. 잠금·부재는 계획의 것을
  그대로 내놓는다 — 선언된 무음은 완결이고 되풀이 가능한 실패만 수락을 막는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from bisect import bisect_right
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, TypeVar

from vlogsrt import log, pipeline, refine, repair, transcribe
from vlogsrt.config import Config
from vlogsrt.models import Chunk, Cue, Repair, Segment, Span
from vlogsrt.project.captions import (
    SUBMS_COLLAPSED,
    PlacedSourceCue,
    SourceCaptionSet,
    SourceCue,
)
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import AssetResource, ClipInstance, ProjectDocument
from vlogsrt.project.pcm import (
    PcmReference,
    SampleRange,
    quiet_sample,
    retained_sample_range,
    write_wav_slice,
)
from vlogsrt.project.speech import SourceSegment, SourceTrace, UnavailableSourceAudio
from vlogsrt.project.timebase import format_srt_time, milliseconds_half_up

_SLICE_ID_DOMAIN = "vlogsrt-logical-slice-v1"
"""슬라이스 신원의 도메인 구분자 — 인스턴스·리소스 해시와 우연히 겹치지 않게 한다."""

_REQUEST_ID_DOMAIN = "vlogsrt-boundary-request-v1"
"""경계 요청 신원의 도메인 구분자 — 다른 해시 재료와 우연히 겹치지 않게 한다."""

_BOUNDARY_KEY_DOMAIN = "vlogsrt-boundary-analysis-v1"
"""경계 분석 캐시 키의 도메인 구분자 — 소스 키·PCM 키와 우연히 겹치지 않게 한다."""

_BOUNDARY_ID_DOMAIN = "vlogsrt-boundary-id-v1"
"""경계 기록 신원의 도메인 구분자 — 다른 해시 재료와 우연히 겹치지 않게 한다."""

_BOUNDARY_SEGMENT_ID_DOMAIN = "vlogsrt-boundary-segment-v1"
"""경계 골격 세그먼트 신원의 도메인 구분자 — 소스 세그먼트와 갈라놓는다."""

_BOUNDARY_CUE_ID_DOMAIN = "vlogsrt-boundary-cue-v1"
"""경계 큐 신원의 도메인 구분자 — 소스 큐와 갈라놓는다."""

_MANIFEST_FORMAT = "vlogsrt-boundary-manifest-v1"
"""경계 완성물 기록의 형식 표지 — 다른 기록과 섞이지 않게 한다."""

_BOUNDARY_DIR = "boundary"
"""완성물 네임스페이스의 고정된 중간 디렉터리 — 애셋과 경계 캐시를 갈라놓는다."""

_SCRATCH_DIR = "work"
"""조각 WAV가 사는 휘발성 자리 — 완성물과 갈라진다(P3-02와 같은 형식 사실)."""

_SAMPLE_WIDTH = 2
"""s16le 한 샘플의 바이트 폭 — 조각 바이트 한도를 잴 때 쓰는 포맷의 사실이다."""

_WAV_HEADER_BYTES = 44
"""내는 WAV 헤더의 바이트 수 — 바이트 한도는 파일 전체로 잰다(포맷의 사실)."""

_STATUS_SUCCESS = "success"
_STATUS_EMPTY = "empty"
_STATUS_ERROR = "error"

_TRANSCRIPT_STATUSES = frozenset({_STATUS_SUCCESS, _STATUS_EMPTY, _STATUS_ERROR})
_TRANSCRIPT_TEXT_FIELDS = (
    "boundary_id",
    "cache_key",
    "request_id",
    "source_id",
    "pcm_analysis_key",
    "original_stem",
)

# 글자만 주는 호출. 시각을 만들지 않으므로 응답 계약도 글자만 요구한다(P3-02와
# 같은 선 — 포획 검증은 이 계층이 소유한 사유 복제본이다).
_SEGMENT_PAYLOAD_KINDS = frozenset({transcribe.FIXTURE_KIND, transcribe.RESCUE_KIND})
_KIND_LABELS = {
    transcribe.FIXTURE_KIND: "전사",
    transcribe.VERIFY_KIND: "검증",
    transcribe.RESCUE_KIND: "재전사",
    transcribe.ARBITER_KIND: "심판",
}

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
    "BoundaryTranscript",
    "transcribe_boundaries",
    "BOUNDARY_UNRESOLVED",
    "RETRYABLE_UNAVAILABLE_REASONS",
    "BoundaryProjection",
    "project_boundaries",
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
        _require_cue_linkage(placed, slice_, trace, deps)
        clamped.append(_clamped_cue(placed, slice_))
        span = _seed_span(placed, slice_, deps, trace)
        if span is not None:
            seeds.append(_Seed(slice_, trace, reference, span[0], span[1]))
    return clamped, seeds


def _clamped_cue(placed: PlacedSourceCue, slice_: LogicalSlice) -> ProjectCue:
    """배치 큐 한 장을 슬라이스 큐로 옮긴다 — 원본 기록과 발표를 함께 싣는다."""
    return ProjectCue(
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


def _require_cue_linkage(
    placed: PlacedSourceCue, slice_: LogicalSlice, trace: SourceTrace, deps: _Dependencies
) -> None:
    """내구 큐 연결 — 배치 큐·소스 큐·슬라이스·트레이스의 소스 신원이 정확히 같다.

    하나의 큐가 두 소스 신원을 말하면 ProjectCue와 BoundaryRequest가 서로 다른
    소스를 말하는 계획이 나간다 — 그런 계획은 내놓지 않고 그 자리에서 닫는다.
    """
    if placed.source_id != slice_.source_id:
        raise ValueError(
            f"배치 큐의 소스 신원이 슬라이스와 다르다: cue {placed.cue_id!r}는"
            f" {placed.source_id!r}인데 slice는 {slice_.source_id!r}다"
            f" (slice {slice_.slice_id}, instance {placed.instance_id!r})"
            " — 한 큐가 두 소스를 말할 수 없다"
        )
    source_cue = deps.source_cue(placed.cue_id)
    if source_cue.source_id != placed.source_id:
        raise ValueError(
            f"소스 큐의 신원이 배치 큐와 다르다: cue {placed.cue_id!r}의 소스 큐는"
            f" {source_cue.source_id!r}인데 배치 큐는 {placed.source_id!r}다"
            f" (trace {trace.source_id!r}) — 연결은 신원으로만 한다"
        )


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


@dataclass(frozen=True, slots=True)
class _ClampedSpan:
    """묶인 범위 하나와 그에 참여하는 칼날 — 병합 호환 판정의 재료다."""

    start: Fraction
    end: Fraction
    instance_ids: tuple[str, ...]


def _coalesced(
    slice_: LogicalSlice, spans: Sequence[_ClampedSpan]
) -> tuple[tuple[Fraction, Fraction], ...]:
    """엄격히 겹친 범위는 참여 칼날이 달라도 항상 합집합으로 합친다 — 닿기만
    한 범위는 참여 칼날이 정확히 같을 때만 합친다.

    겹친 범위를 각자 내보내면 같은 남긴 프레임을 두 요청이 다시 물으며 겹쳐
    덮는다. 닿기만 한 범위는 프레임을 공유하지 않으므로 합침이 의무가 아니다 —
    다른 칼날에 앉은 닿은 범위는 각자 요청으로 남는다. 합쳐진 묶음의 참여
    칼날은 합쳐진 범위에서 다시 읽는다 — 병합 재료의 순서를 믿지 않는다.
    """
    merged: list[list[Fraction]] = []
    for span in sorted(spans, key=lambda item: (item.start, item.end, item.instance_ids)):
        if merged and _joinable(slice_, merged[-1], span):
            merged[-1][1] = max(merged[-1][1], span.end)
        else:
            merged.append([span.start, span.end])
    return tuple((row[0], row[1]) for row in merged)


def _joinable(slice_: LogicalSlice, entry: list[Fraction], span: _ClampedSpan) -> bool:
    """합침 판정 — 겹침은 무조건 합치고 닿음은 참여 칼날이 같을 때만 합친다."""
    if span.start < entry[1]:
        return True  # 엄격한 겹침 — 남긴 프레임을 공유하므로 반드시 하나가 된다
    if span.start > entry[1]:
        return False
    return span.instance_ids == _participating_instances(slice_, entry[0], entry[1])


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
        # 병합 재료는 닫힌 범위와 그에 참여하는 칼날이다 — 칼날부터 세고 나서 합친다.
        spans = [
            _ClampedSpan(start, end, _participating_instances(bucket.slice_, start, end))
            for start, end in bucket.spans
        ]
        for start, end in _coalesced(bucket.slice_, spans):
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


# ═══════════════════════════════════════════════════════════════
#  P4-03 — 남긴 경계 오디오만 다시 전사하는 실행
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class BoundaryTranscript:
    """경계 하나의 다시 전사 결과. 완성되면 불변이고 캐시 신원은 `cache_key`다.

    `cues`는 **디코드 오디오 국소 시계**다 — 국소 시각에 남긴 범위의 시작 프레임을
    정확히 한 번 더했다. 원점(`pcm_sample0_file_time`)은 아직 아니며 파일/XML 배치
    때 한 번 더한다. `cut_truncated=True`는 종결 메타데이터다 — 이 기록의 끝점
    때문에 다시 경계 요청을 만들 수 없다는 뜻이지, 재전사·정제를 뺀 뜻이 아니다.
    """

    boundary_id: str
    cache_key: str
    request_id: str
    source_id: str
    pcm_analysis_key: str
    original_stem: str
    source_start: Fraction
    source_end: Fraction
    sample_range: SampleRange
    status: Literal["success", "empty", "error"]
    draft: tuple[SourceSegment, ...]
    settled_spine: tuple[SourceSegment, ...]
    repairs: tuple[Repair, ...]
    cues: tuple[SourceCue, ...]
    error: str | None
    cut_truncated: bool = True

    def __post_init__(self) -> None:
        if self.status not in _TRANSCRIPT_STATUSES:
            raise ValueError(f"알 수 없는 상태다: {self.status!r}")
        for name in _TRANSCRIPT_TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name}은 비지 않은 문자열이어야 한다: {value!r}")
        if (self.status == _STATUS_ERROR) != (self.error is not None):
            raise ValueError(
                f"status={self.status!r}와 error={self.error!r}가 어긋난다 — 실패는 상태로 남는다"
            )
        if self.status == _STATUS_EMPTY and (self.draft or self.settled_spine or self.cues):
            raise ValueError("빈 성공인데 초안·골격·큐가 남아 있다")
        if self.status == _STATUS_SUCCESS and not self.cues:
            raise ValueError("성공인데 큐가 하나도 없다")


@dataclass(frozen=True, slots=True)
class _BoundaryRun:
    """경계 하나의 이 번 실행 자리 — 참조·설정·캐시 키·네임스페이스가 다닌다."""

    reference: PcmReference
    trace: SourceTrace
    request: BoundaryRequest
    cfg: Config
    cache_key: str
    boundary_id: str
    namespace: Path

    @property
    def scratch(self) -> Path:
        return self.namespace / _SCRATCH_DIR

    @property
    def local_duration(self) -> float:
        """엔진이 보는 경계 국소 길이 — 남긴 프레임 수 그대로다."""
        span = self.request.sample_range
        return float(Fraction(span.end - span.start, self.reference.sample_rate))

    @property
    def decoded_origin(self) -> Fraction:
        """국소 → 디코드 국소 변환량 — 남긴 범위의 시작 프레임을 주파수로 나눈 값."""
        return Fraction(self.request.sample_range.start, self.reference.sample_rate)


@dataclass
class _BoundaryProgress:
    """이미 완성된 단계의 증거 — 실패 기록에도 완성된 것은 버리지 않는다."""

    ranges: tuple[SampleRange, ...] = ()
    draft: tuple[Segment, ...] = ()
    settled: tuple[Segment, ...] = ()
    repairs: tuple[Repair, ...] = ()
    cues: tuple[Cue, ...] = ()


class _BoundaryLinkage:
    """요청·참조·트레이스를 (source_id, pcm_analysis_key) 쌍으로만 묶는다."""

    def __init__(self, references: Sequence[PcmReference], traces: Sequence[SourceTrace]) -> None:
        self._references = _index_by(
            references, "소스와 PCM 키 하나에 참조가 둘 이상이다", _reference_pair
        )
        self._traces = _index_by(traces, "소스와 PCM 키 하나에 트레이스가 둘 이상이다", _trace_pair)

    def resolve(self, request: BoundaryRequest) -> tuple[PcmReference, SourceTrace]:
        pair = f"{request.source_id}\0{request.pcm_analysis_key}"
        reference = self._references.get(pair)
        if reference is None:
            raise ValueError(
                f"소스의 PCM 참조가 없다: source_id={request.source_id!r},"
                f" pcm_analysis_key={request.pcm_analysis_key!r}"
                " — 경계 전사는 저장 PCM에서만 나온다"
            )
        trace = self._traces.get(pair)
        if trace is None:
            raise ValueError(
                f"소스의 전사 트레이스가 없다: source_id={request.source_id!r},"
                f" pcm_analysis_key={request.pcm_analysis_key!r} — 지어내지 않는다"
            )
        _require_boundary_pair(reference, trace)
        return reference, trace


def _reference_pair(reference: PcmReference) -> str:
    return f"{reference.source_id}\0{reference.analysis_key}"


def _trace_pair(trace: SourceTrace) -> str:
    return f"{trace.source_id}\0{trace.pcm_analysis_key}"


def _require_boundary_pair(reference: PcmReference, trace: SourceTrace) -> None:
    """짝의 원점 증명 — 참조와 트레이스가 같은 저장 PCM을 말하는지 확인한다."""
    if reference.pcm_sample0_file_time != trace.pcm_sample0_file_time:
        raise ValueError(
            "PCM 참조의 원점이 트레이스와 다르다:"
            f" reference {reference.pcm_sample0_file_time} 대비"
            f" trace {trace.pcm_sample0_file_time} (source_id={trace.source_id!r})"
        )


def transcribe_boundaries(
    requests: Sequence[BoundaryRequest],
    references: Sequence[PcmReference],
    source_traces: Sequence[SourceTrace],
    cfg: Config,
    transcriber: transcribe.Transcriber,
    artifact_dir: Path,
) -> tuple[BoundaryTranscript, ...]:
    """계획된 경계 요청들을 저장 PCM의 남긴 프레임만으로 다시 전사한다.

    연결·범위 검사는 **전 요청을 호출 앞에서** 끝낸다 — 깨진 입력에 유료 호출이
    값을 쓰지 않는다(P3-02와 같은 선). 기록은 요청 순서 그대로 돌아가고, 실행 중
    실패는 예외가 아니라 타입 있는 error 기록이다. 완성물은 캐시 키로 주소 붙은
    네임스페이스에 원자적으로 독점 출판된다.
    """
    linkage = _BoundaryLinkage(references, source_traces)
    runs: list[_BoundaryRun] = []
    seen: set[str] = set()
    for request in requests:
        if request.request_id in seen:
            raise ValueError(
                f"요청 신원이 중복된다: {request.request_id!r} — 같은 요청을 두 번 돌리지 않는다"
            )
        seen.add(request.request_id)
        reference, trace = linkage.resolve(request)
        _require_retainable(reference, request)
        runs.append(_plan_run(reference, trace, request, cfg, artifact_dir))
    return tuple(_transcribe_one(run, transcriber) for run in runs)


def _plan_run(
    reference: PcmReference,
    trace: SourceTrace,
    request: BoundaryRequest,
    cfg: Config,
    artifact_dir: Path,
) -> _BoundaryRun:
    cache_key = _boundary_cache_key(reference, trace, request, cfg)
    return _BoundaryRun(
        reference=reference,
        trace=trace,
        request=request,
        cfg=cfg,
        cache_key=cache_key,
        boundary_id=_boundary_id(cache_key, request.request_id),
        namespace=artifact_dir / request.source_id / _BOUNDARY_DIR / cache_key,
    )


def _require_retainable(reference: PcmReference, request: BoundaryRequest) -> None:
    """요청 범위는 참조가 줄 수 있는 프레임 안에 정확히 있어야 한다."""
    samples = request.sample_range
    if _bad_count(samples.start) or _bad_count(samples.end):
        raise ValueError(f"요청 범위는 정수 프레임 번호다: {samples.start!r}..{samples.end!r}")
    if reference.sample_rate < 1 or reference.channels < 1:
        raise ValueError(
            f"참조의 주파수·채널이 1 이상이어야 한다: {reference.sample_rate!r},"
            f" {reference.channels!r} (source_id={request.source_id!r})"
        )
    if samples.start < 0 or samples.end > reference.sample_count or samples.end <= samples.start:
        raise ValueError(
            f"참조가 줄 수 없는 범위다: [{samples.start}, {samples.end})"
            f" (source_id={request.source_id!r}, 저장 {reference.sample_count}프레임)"
        )


def _transcribe_one(run: _BoundaryRun, transcriber: transcribe.Transcriber) -> BoundaryTranscript:
    """경계 하나를 엔진 전체로 돈다. 예외를 밖으로 새지 않고 상태로 남긴다."""
    capture = _BoundaryCapture()
    progress = _BoundaryProgress()
    wrapped = _BoundaryCapturingTranscriber(transcriber, capture)
    try:
        return _analyze_boundary(run, wrapped, capture, progress)
    except Exception as exc:  # noqa: BLE001 - 상태로 바꿔 남긴다. 조용히 두지는 않는다
        capture.record("경계 엔진", exc)
        return _record(run, progress, status=_STATUS_ERROR, error=capture.describe())


def _analyze_boundary(
    run: _BoundaryRun,
    wrapped: _BoundaryCapturingTranscriber,
    capture: _BoundaryCapture,
    progress: _BoundaryProgress,
) -> BoundaryTranscript:
    """1차 전사의 상태를 갈라 보내고, 초안이 있으면 재전사·조판으로 간다."""
    outcome = _first_pass(run, wrapped, progress)
    if outcome.status == _STATUS_ERROR:
        return _record(
            run, progress, status=_STATUS_ERROR, error=outcome.error or "경계 전사가 실패했다"
        )
    progress.draft = outcome.segments
    if outcome.status == _STATUS_EMPTY:
        return _empty_transcript(run, progress)
    if not progress.draft:
        # 걸러져 빈 성공은 쓸 초안이 없는 것이다(EmptyTranscript) — 무음의 증거가
        # 아니므로 빈 성공으로 내주지 않는다(P3-02와 같은 선).
        return _record(
            run,
            progress,
            status=_STATUS_ERROR,
            error="조립 가능한 초안이 하나도 없습니다 — 공백 글자만 걸러져 빈 성공이다",
        )
    return _settled_transcript(run, wrapped, capture, progress)


def _first_pass(
    run: _BoundaryRun,
    wrapped: _BoundaryCapturingTranscriber,
    progress: _BoundaryProgress,
) -> transcribe.TranscriptionOutcome:
    """남긴 범위를 조각으로 만들어 1차 전사를 부른다. 조각 증거는 progress에 남는다."""
    progress.ranges = _retained_ranges(run.reference, run.request.sample_range, run.cfg)
    job = transcribe.TranscriptionJob(
        stem=run.reference.original_stem,
        chunks=_initial_chunks(
            run.reference, run.request.sample_range, progress.ranges, run.scratch
        ),
        namespace=run.namespace,
    )
    return transcribe.transcribe_outcome(wrapped, job, run.cfg)


def _settled_transcript(
    run: _BoundaryRun,
    wrapped: _BoundaryCapturingTranscriber,
    capture: _BoundaryCapture,
    progress: _BoundaryProgress,
) -> BoundaryTranscript:
    """재전사(주입 커터) → 불변 골격 → 큐 → 정제 → 완성물 출판."""
    settled = repair.settle(
        list(progress.draft),
        repair.Source(
            stem=run.reference.original_stem,
            duration=run.local_duration,
            namespace=run.namespace,
            cutter=_repair_cutter(run.reference, run.request.sample_range, run.scratch, capture),
        ),
        run.cfg,
        wrapped,
    )
    progress.settled = tuple(settled.spine)
    progress.repairs = settled.repairs
    if capture.failures:
        return _record(run, progress, status=_STATUS_ERROR, error=capture.describe())
    progress.cues = tuple(
        refine.refine(pipeline.spine_to_cues(list(settled.spine)), run.cfg, run.local_duration)
    )
    if not progress.cues:
        return _record(
            run, progress, status=_STATUS_ERROR, error="정제를 끝내고 큐가 하나도 남지 않았다"
        )
    return _published_transcript(run, progress)


def _empty_transcript(run: _BoundaryRun, progress: _BoundaryProgress) -> BoundaryTranscript:
    """빈 성공 — 결정적인 빈 기록을 낸다. 조용한 실패가 아니다(P3-02와 같은 선)."""
    transcript = _record(run, progress, status=_STATUS_EMPTY, error=None)
    if _publish_transcript(run, transcript) is None:
        return _record(run, progress, status=_STATUS_ERROR, error=_conflict_error(run))
    log.info(f"경계 전사 완료: {run.reference.original_stem} · 빈 (무음의 증거가 아니다)")
    return transcript


def _published_transcript(run: _BoundaryRun, progress: _BoundaryProgress) -> BoundaryTranscript:
    """성공 기록을 완성물로 출판한다 — 충돌이면 있는 바이트를 보존하고 error로 끝낸다."""
    transcript = _record(run, progress, status=_STATUS_SUCCESS, error=None)
    if _publish_transcript(run, transcript) is None:
        return _record(run, progress, status=_STATUS_ERROR, error=_conflict_error(run))
    log.info(f"경계 전사 완료: {run.reference.original_stem} · 큐 {len(progress.cues)}장")
    return transcript


def _conflict_error(run: _BoundaryRun) -> str:
    stem = run.reference.original_stem
    return (
        f"완성물이 이미 다른 바이트로 있다: {run.cache_key}/{stem}.BOUNDARY.json — "
        "있는 것을 보존하고 이 번 결과를 버린다"
    )


def _record(
    run: _BoundaryRun, progress: _BoundaryProgress, *, status: str, error: str | None
) -> BoundaryTranscript:
    """완성된 단계의 증거를 기록으로 옮긴다 — 지어내지도 조금도 지우지도 않는다."""
    draft = _boundary_segments(run, progress.ranges, progress.draft)
    settled = _boundary_segments(run, progress.ranges, progress.settled)
    return BoundaryTranscript(
        boundary_id=run.boundary_id,
        cache_key=run.cache_key,
        request_id=run.request.request_id,
        source_id=run.request.source_id,
        pcm_analysis_key=run.request.pcm_analysis_key,
        original_stem=run.reference.original_stem,
        source_start=run.request.source_start,
        source_end=run.request.source_end,
        sample_range=run.request.sample_range,
        status=status,  # type: ignore[arg-type]
        draft=draft,
        settled_spine=settled,
        repairs=progress.repairs,
        cues=_boundary_cues(run, progress.cues, settled),
        error=error,
    )


# ─────────────────────────────────────────────────────────────
#  캐시 신원 — 내용·정책·남긴 범위·소스 분석·설정이 키다
# ─────────────────────────────────────────────────────────────


def _boundary_cache_key(
    reference: PcmReference, trace: SourceTrace, request: BoundaryRequest, cfg: Config
) -> str:
    """경계 분석 키 — 경로·stem·목록 위치는 재료가 아니다.

    원본 해시·PCM 정책(분석 키가 묶는다)·PCM 내용·채널·주파수·남긴 프레임의 정확한
    범위·소스 트레이스의 분석 키·모델과 설정 지문을 모은다. 어느 하나라도 달라지면
    다른 분석이고, 다른 완성물 자리를 얻는다.
    """
    material = "\n".join(
        [
            _BOUNDARY_KEY_DOMAIN,
            f"original_sha256={reference.original_sha256}",
            f"pcm_analysis_key={reference.analysis_key}",
            f"pcm_sha256={reference.pcm_sha256}",
            f"channels={reference.channels}",
            f"sample_rate={reference.sample_rate}",
            f"sample_range={request.sample_range.start}/{request.sample_range.end}",
            f"source_analysis_key={trace.analysis_key}",
            f"model={cfg.transcribe_model}",
            f"config_sha256={_config_sha256(cfg)}",
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _boundary_id(cache_key: str, request_id: str) -> str:
    """경계 기록 신원 — 캐시 키와 요청 신원의 결정적 해시다."""
    material = "\n".join(
        [_BOUNDARY_ID_DOMAIN, f"cache_key={cache_key}", f"request_id={request_id}"]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _config_sha256(cfg: Config) -> str:
    """설정 스냅샷의 지문 — 같은 설정은 같은 지문을 낸다(P3-02와 같은 자)."""
    body = json.dumps(cfg.to_dict(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────
#  남긴 범위의 조각 — 저장 PCM을 정확한 프레임으로 덮는다
# ─────────────────────────────────────────────────────────────


def _retained_ranges(
    reference: PcmReference, span: SampleRange, cfg: Config
) -> tuple[SampleRange, ...]:
    """남긴 범위를 빈틈없이 덮는 조각 범위 — 자리만 고르고 구간은 못 고른다.

    P3-02의 전체 조각 계획과 같은 규칙을 남긴 범위 안에서만 적용한다: 두 한도가
    조각 수를 정하고 경계는 목표 지점 주위의 가장 조용한 창에서 골라 exact 샘플로
    자른다. 합은 언제나 `[span.start, span.end)`다(규칙 4). 제거된 오디오의 창은
    경계 **후보**를 잴 수 있어도 경계 자리가 될 수는 없다 — 한도 밴드가 묶는다.
    """
    frames = span.end - span.start
    if frames < 1:
        raise ValueError(f"남긴 프레임이 없다: {frames!r} — 덮을 오디오가 없다")
    rate = reference.sample_rate
    max_len = _max_chunk_frames(reference, cfg)
    count = max(1, -(-frames // max_len))
    search = max(0, round(cfg.cut_search_seconds * rate))
    window = min(frames, max(1, round(cfg.cut_quiet_window_ms * rate / 1000)))
    cuts: list[int] = []
    previous = span.start
    for index in range(1, count):
        target = span.start + frames * index // count
        remaining = count - index
        lo = max(previous + 1, span.end - max_len * remaining)
        hi = min(previous + max_len, span.end - remaining)
        if lo > hi:
            raise ValueError(
                f"조각 한도를 채울 수 없다: {frames}프레임을 한도 {max_len}프레임인 "
                f"{count}조각으로 못 나눈다 ({index}번째 경계 [{lo}, {hi}])"
            )
        cuts.append(_bounded_quiet_cut(reference, target, search, window, lo, hi))
        previous = cuts[-1]
    edges = [span.start, *cuts, span.end]
    return tuple(SampleRange(start, end) for start, end in pairwise(edges))


def _max_chunk_frames(reference: PcmReference, cfg: Config) -> int:
    """한 조각이 담을 수 있는 최대 프레임 — 초 한도와 실제 WAV 바이트 한도 중 작은 쪽.

    바이트 한도는 내는 파일 전체(44바이트 헤더 포함)로 잰다(P3-02와 같은 자).
    한도가 헤더·한 프레임조차 못 담으면 명시적으로 죽는다 — 조용히 한 조각을
    통째로 내는 길이 될 수 없기 때문이다.
    """
    if cfg.chunk_max_bytes <= _WAV_HEADER_BYTES:
        raise ValueError(
            f"chunk_max_bytes가 WAV 헤더({_WAV_HEADER_BYTES}바이트)보다 작다: "
            f"{cfg.chunk_max_bytes!r} — 조각 하나를 못 만든다"
        )
    frame_bytes = _SAMPLE_WIDTH * reference.channels
    by_bytes = (cfg.chunk_max_bytes - _WAV_HEADER_BYTES) // frame_bytes
    if by_bytes < 1:
        raise ValueError(
            f"chunk_max_bytes가 한 프레임({frame_bytes}바이트)도 못 담는다: {cfg.chunk_max_bytes!r}"
        )
    if cfg.chunk_max_seconds > 0:
        by_seconds = int(cfg.chunk_max_seconds * reference.sample_rate)
        if by_seconds < 1:
            raise ValueError(
                f"chunk_max_seconds가 한 프레임(1/{reference.sample_rate}초)도 못 담는다: "
                f"{cfg.chunk_max_seconds!r} — 조각 하나를 못 만든다"
            )
        return min(by_bytes, by_seconds)
    return by_bytes


def _bounded_quiet_cut(
    reference: PcmReference, target: int, search: int, window: int, lo: int, hi: int
) -> int:
    """조용한 창의 경계 후보를 한도가 허락하는 범위로 묶는다(P3-02와 같은 자)."""
    candidate = _quiet_cut(reference, target, search, window)
    if lo <= candidate <= hi:
        return candidate
    if lo <= target <= hi:
        return target
    return min(max(target, lo), hi)


def _quiet_cut(reference: PcmReference, target: int, search: int, window: int) -> int:
    """목표 주위의 묶인 창 안에서 가장 조용한 창의 시작 프레임.

    창을 놓을 자리가 없으면 목표 그대로다 — 자를 자리를 못 고르는 것이 구간을
    잃을 이유가 되지 못한다(규칙 4). 말인지 침묵인지는 판단하지 않는다.
    """
    lo = max(0, target - search)
    hi = min(target + search, reference.sample_count - window)
    if lo > hi:
        return target
    return quiet_sample(reference, target=target, search_samples=search, window_samples=window)


def _initial_chunks(
    reference: PcmReference,
    retained: SampleRange,
    ranges: Sequence[SampleRange],
    scratch: Path,
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for index, span in enumerate(ranges):
        path = scratch / f"initial-{index:04d}.wav"
        write_wav_slice(reference, span, path)
        chunks.append(_snapped_chunk(reference, retained, span, index, path))
    return tuple(chunks)


def _repair_cutter(
    reference: PcmReference,
    retained: SampleRange,
    scratch: Path,
    capture: _BoundaryCapture,
) -> Callable[[Sequence[Span]], list[Chunk]]:
    """재전사 구간을 같은 저장 PCM의 남긴 범위 안에서 자르는 주입 커터."""

    def cut(spans: Sequence[Span]) -> list[Chunk]:
        try:
            return _repair_chunks(reference, retained, spans, scratch)
        except Exception as exc:  # noqa: BLE001 - 기록하고 그대로 다시 던진다
            capture.record("경계 재전사 조각", exc)
            raise

    return cut


def _repair_chunks(
    reference: PcmReference,
    retained: SampleRange,
    spans: Sequence[Span],
    scratch: Path,
) -> list[Chunk]:
    """재전사 조각들 — 구간마다 남긴 범위로 묶어 실제 프레임으로 자른다."""
    chunks: list[Chunk] = []
    for index, (low, high) in enumerate(spans):
        span = _clamped_repair_range(reference, retained, low, high)
        path = scratch / f"repair-{index:04d}.wav"
        write_wav_slice(reference, span, path)
        chunks.append(_snapped_chunk(reference, retained, span, index, path))
    return chunks


def _clamped_repair_range(
    reference: PcmReference, retained: SampleRange, low: float, high: float
) -> SampleRange:
    """국소 재전사 구간을 남긴 범위로 묶어 프레임으로 환산한다 — 시작 ceil·끝 floor.

    여유가 경계 밖으로 나가면 남긴 범위 끝에서 묶인다 — 제거된 오디오의 덧붙임이나
    문맥은 없다. 묶은 뒤 남는 프레임이 없으면 그 자리에서 죽는다: 여유를 더해
    살리는 길은 없다.
    """
    rate = reference.sample_rate
    duration = Fraction(retained.end - retained.start, rate)
    lo = min(max(Fraction(str(low)), Fraction(0)), duration)
    hi = min(max(Fraction(str(high)), Fraction(0)), duration)
    start = retained.start + math.ceil(lo * rate)
    end = retained.start + math.floor(hi * rate)
    if end <= start:
        raise ValueError(
            f"재전사 구간이 남긴 범위 안에서 비었다: 국소 [{lo}, {hi}) (요청 [{low!r}, {high!r}])"
        )
    return SampleRange(start, end)


def _snapped_chunk(
    reference: PcmReference, retained: SampleRange, span: SampleRange, index: int, path: Path
) -> Chunk:
    """잘린 프레임에서 국소 좌표를 만든다 — 요청한 초 원본이 아니다.

    국소 오프셋은 `Fraction(실제 시작 프레임 − 남긴 범위 시작, 주파수)`이고 길이는
    실제 잘린 프레임 수다. 엔진 전체가 이 국소 시계에서 돈다.
    """
    rate = reference.sample_rate
    offset = float(Fraction(span.start - retained.start, rate))
    duration = float(Fraction(span.end - span.start, rate))
    return Chunk(path=path, offset=offset, duration=duration, index=index)


# ─────────────────────────────────────────────────────────────
#  시계 — 국소 엔진 시각을 디코드 국소·파일 기록으로 정확히 옮긴다
# ─────────────────────────────────────────────────────────────


def _boundary_segments(
    run: _BoundaryRun,
    ranges: Sequence[SampleRange],
    segments: Sequence[Segment],
) -> tuple[SourceSegment, ...]:
    """레거시 세그먼트를 디코드 국소 시계의 기록으로 옮긴다 — 값은 고치지 않는다.

    `audio_*`는 국소 시각에 남긴 범위의 시작을 정확히 한 번 더한 디코드 국소
    시각이고 `file_*`는 거기에 원점을 정확히 한 번 더한 파일 시각이다(P3-02와
    같은 두 시계). 세그먼트가 어느 조각에서 왔는지는 시작 시각을 덮는 조각으로
    정한다.
    """
    if not ranges or not segments:
        return ()
    retained = run.request.sample_range
    starts = [
        float(Fraction(span.start - retained.start, run.reference.sample_rate)) for span in ranges
    ]
    origin = run.reference.pcm_sample0_file_time
    records: list[SourceSegment] = []
    for segment in segments:
        chunk_index = max(0, bisect_right(starts, segment.start) - 1)
        span = ranges[chunk_index]
        audio_start = Fraction(str(segment.start)) + run.decoded_origin
        audio_end = Fraction(str(segment.end)) + run.decoded_origin
        records.append(
            SourceSegment(
                segment_id=_boundary_segment_id(run.cache_key, chunk_index, segment.id, span),
                audio_start=audio_start,
                audio_end=audio_end,
                file_start=audio_start + origin,
                file_end=audio_end + origin,
                text=segment.text,
                compression_ratio=segment.compression_ratio,
                avg_logprob=segment.avg_logprob,
                no_speech_prob=segment.no_speech_prob,
            )
        )
    return tuple(records)


def _boundary_segment_id(
    cache_key: str, chunk_index: int, legacy_id: int, span: SampleRange
) -> str:
    """경계 세그먼트 신원 — 캐시 키·조각 번호·레거시 id·정확한 조각 범위의 해시."""
    material = "\n".join(
        [
            _BOUNDARY_SEGMENT_ID_DOMAIN,
            f"cache_key={cache_key}",
            f"chunk_index={chunk_index}",
            f"legacy_segment_id={legacy_id}",
            f"retained_range={span.start}/{span.end}",
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _boundary_cues(
    run: _BoundaryRun, cues: Sequence[Cue], spine: Sequence[SourceSegment]
) -> tuple[SourceCue, ...]:
    """정제된 큐를 디코드 국소 시계의 소스 큐로 옮긴다 — 다듬지 않는다.

    원점은 더하지 않는다: 파일 시계 변환은 파일/XML 배치 때 정확히 한 번 한다.
    `segment_ids`는 경계 골격의 결정적 신원이다 — 큐는 골격을 역추적할 수 있어야
    한다(규칙 2).
    """
    records: list[SourceCue] = []
    for cue in cues:
        start = Fraction(str(cue.start)) + run.decoded_origin
        end = Fraction(str(cue.end)) + run.decoded_origin
        segment_ids = tuple(_spine_segment_id(spine, legacy_id) for legacy_id in cue.seg_ids)
        records.append(
            SourceCue(
                cue_id=_boundary_cue_id(run.cache_key, cue.index, start, end, segment_ids),
                source_id=run.request.source_id,
                segment_ids=segment_ids,
                start=start,
                end=end,
                text="\n".join(cue.lines),
                flags=cue.flags,
            )
        )
    return tuple(records)


def _spine_segment_id(spine: Sequence[SourceSegment], legacy_id: int) -> str:
    """레거시 `seg_id`를 경계 골격 신원으로 — settle이 골격을 0부터 다시 세므로
    위치가 곧 레거시 id다(P3-03과 같은 자)."""
    if not 0 <= legacy_id < len(spine):
        raise ValueError(f"큐의 seg_id {legacy_id}가 골격에 없다 — 골격은 {len(spine)}장이다")
    return spine[legacy_id].segment_id


def _boundary_cue_id(
    cache_key: str,
    index: int,
    start: Fraction,
    end: Fraction,
    segment_ids: tuple[str, ...],
) -> str:
    """경계 큐 신원 — 캐시 키·큐 번호·정확한 시각·세그먼트의 해시."""
    material = "\n".join(
        [
            _BOUNDARY_CUE_ID_DOMAIN,
            f"cache_key={cache_key}",
            f"cue_index={index}",
            f"start={start}",
            f"end={end}",
            f"segment_ids={','.join(segment_ids)}",
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────
#  실패 포획 — 사유 래퍼가 계약을 지키고 기록만 남긴다 (P3-02의 사유 복제본)
# ─────────────────────────────────────────────────────────────


@dataclass
class _BoundaryCapture:
    """포획된 운영 실패의 목록 — 실행기가 settle 뒤에 보는 유일한 증거."""

    failures: list[str] = field(default_factory=list)

    def record(self, stage: str, exc: BaseException | str) -> None:
        text = " ".join(str(exc).split())
        self.failures.append(f"{stage}: {text}" if text else f"{stage}: {exc.__class__.__name__}")

    def describe(self) -> str:
        return "; ".join(self.failures)


class _BoundaryCapturingTranscriber:
    """주입된 전사기를 싸는 사유 래퍼 — 계약 위반을 기록하고 그대로 다시 던진다.

    레거시 재전사는 이 예외를 오늘과 같이 잡아 되풀이 정책(유지·접기)으로 흘려
    보내지만, 실행기는 settle 뒤 포획 기록을 보고 완성물을 내주지 않는다.
    세그먼트를 주는 호출(전사·재전사)은 엄격한 꼴을, 글자만 주는 호출(심판·검증)은
    명시적 문자열 `text` 또는 `parse_text`가 읽는 목록 형태를 요구한다.
    """

    def __init__(self, inner: transcribe.Transcriber, capture: _BoundaryCapture) -> None:
        self._inner = inner
        self._capture = capture

    def transcribe(
        self, job: transcribe.TranscriptionJob, cfg: Config
    ) -> list[transcribe.ChunkResponse]:
        label = _KIND_LABELS.get(job.kind, job.kind)
        try:
            responses = self._inner.transcribe(job, cfg)
            for chunk, response in _match_requested(job, responses):
                defect = _response_defect(job.kind, response.payload, f"{label} 조각 {chunk.index}")
                if defect is not None:
                    raise transcribe.MalformedTranscript(defect)
        except Exception as exc:  # noqa: BLE001 - 기록하고 그대로 다시 던진다
            self._capture.record(f"{label} 호출", exc)
            raise
        return responses


def _match_requested(
    job: transcribe.TranscriptionJob, responses: Sequence[transcribe.ChunkResponse]
) -> list[tuple[Chunk, transcribe.ChunkResponse]]:
    """요청한 조각마다 응답이 정확히 하나 — 누락·중복·요청 밖·오프셋 어긋남은 위반.

    위치로 짝짓지 않는다. 네임스페이스 안의 부분집합 선택은 픽스처 경계가 이미
    결정적으로 걸렀다(P3-01) — 여기는 어떤 응답이 왔는지의 계약을 검사한다.
    """
    requested = {chunk.index: chunk for chunk in job.chunks}
    matched: dict[int, transcribe.ChunkResponse] = {}
    for response in responses:
        index = response.index
        if isinstance(index, bool) or not isinstance(index, int):
            raise transcribe.MalformedTranscript(f"응답 조각 번호가 정수가 아닙니다: {index!r}")
        if index not in requested:
            raise transcribe.MalformedTranscript(f"요청하지 않은 조각 응답이 돌아왔다: {index}")
        if index in matched:
            raise transcribe.MalformedTranscript(f"조각 {index} 응답이 둘 이상 돌아왔다")
        chunk = requested[index]
        if not _finite(response.offset) or response.offset != chunk.offset:
            raise transcribe.MalformedTranscript(
                f"조각 {index} 응답 오프셋이 요청({chunk.offset})과 다릅니다: {response.offset!r}"
            )
        matched[index] = response
    absent = sorted(set(requested) - set(matched))
    if absent:
        raise transcribe.MalformedTranscript(f"응답이 돌아오지 않은 조각: {absent}")
    ordered = sorted(job.chunks, key=lambda chunk: chunk.index)
    return [(chunk, matched[chunk.index]) for chunk in ordered]


def _response_defect(kind: str, payload: Any, where: str) -> str | None:
    """호출 종류별 응답 계약. 위반을 하나만 골라 돌려주고 맞으면 None."""
    if kind in _SEGMENT_PAYLOAD_KINDS:
        return _segment_defect(payload, where)
    return _text_defect(payload, where)


def _segment_defect(payload: Any, where: str) -> str | None:
    """세그먼트를 주는 호출의 엄격한 꼴 — P3-01·P3-02의 전사 결과형과 같은 선.

    공개 결과형을 글자 전용 응답까지 끌어들이는 확장은 하지 않는다 — 이 계층이
    소유한 사유 복제본이다(P3-02의 결정).
    """
    if not isinstance(payload, dict):
        return f"{where}: 응답이 JSON 객체가 아닙니다: {type(payload).__name__}"
    if "segments" not in payload:
        return f"{where}: 응답에 segments가 없습니다"
    segments = payload["segments"]
    if not isinstance(segments, list):
        return f"{where}: segments가 리스트가 아닙니다: {type(segments).__name__}"
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            return f"{where}: 세그먼트 {position}이 JSON 객체가 아닙니다"
        if "text" not in segment:
            return f"{where}: 세그먼트 {position}에 text가 없습니다"
        if not isinstance(segment["text"], str):
            return f"{where}: 세그먼트 {position}의 text가 문자열이 아닙니다: {segment['text']!r}"
        for key in ("start", "end"):
            if key not in segment:
                return f"{where}: 세그먼트 {position}에 {key}가 없습니다"
            if not _finite(segment[key]):
                return (
                    f"{where}: 세그먼트 {position}의 {key}가 유한 숫자가 아닙니다: {segment[key]!r}"
                )
        for key in ("compression_ratio", "avg_logprob", "no_speech_prob"):
            if key in segment and not _finite(segment[key]):
                return (
                    f"{where}: 세그먼트 {position}의 {key}가 유한 숫자가 아닙니다: {segment[key]!r}"
                )
    return None


def _text_defect(payload: Any, where: str) -> str | None:
    """글자만 주는 호출(심판·검증)의 꼴 — 시각은 요구하지도 지어내지도 않는다.

    명시적인 문자열 `text`(빈 문자열은 말이 없다는 증거) 또는 `parse_text`가 읽는
    형태 — `text`가 문자열인 객체들의 목록. 최상위 목록은 이 형태가 아니고, 둘 다
    없는 꼴(빈 객체 포함)은 위반이다.
    """
    if not isinstance(payload, dict):
        return f"{where}: 응답이 JSON 객체가 아닙니다: {type(payload).__name__}"
    if "text" in payload:
        if not isinstance(payload["text"], str):
            return f"{where}: text가 문자열이 아닙니다: {payload['text']!r}"
        return None
    if "segments" in payload:
        segments = payload["segments"]
        if not isinstance(segments, list):
            return f"{where}: segments가 리스트가 아닙니다: {type(segments).__name__}"
        for position, segment in enumerate(segments):
            if not isinstance(segment, dict):
                return f"{where}: 세그먼트 {position}이 JSON 객체가 아닙니다"
            if "text" not in segment:
                return f"{where}: 세그먼트 {position}에 text가 없습니다"
            if not isinstance(segment["text"], str):
                return (
                    f"{where}: 세그먼트 {position}의 text가 문자열이 아닙니다: {segment['text']!r}"
                )
        return None
    return f"{where}: 응답에 text도 segments도 없습니다"


def _finite(value: Any) -> bool:
    """숫자이면서 유한한 값만 받는다 — bool과 NaN·무한대는 시각이 될 수 없다."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bad_count(value: object) -> bool:
    """bool은 int인 척한다 — 세는 수에서는 걸러낸다."""
    return isinstance(value, bool) or not isinstance(value, int)


# ─────────────────────────────────────────────────────────────
#  완성물 — 캐시 키로 주소 붙은 자리에 원자적으로 독점 출판한다
# ─────────────────────────────────────────────────────────────


def _publish_transcript(run: _BoundaryRun, transcript: BoundaryTranscript) -> Path | None:
    """경계 기록을 완성물로 독점 출판한다. 충돌이면 None — 있는 바이트를 보존한다."""
    destination = run.namespace / f"{run.reference.original_stem}.BOUNDARY.json"
    return _publish_exclusive(destination, _manifest_bytes(transcript))


def _manifest_bytes(transcript: BoundaryTranscript) -> bytes:
    """기록의 결정적 직렬 — 같은 기록은 언제나 같은 바이트를 낸다."""
    body: dict[str, object] = {
        "format": _MANIFEST_FORMAT,
        "boundary_id": transcript.boundary_id,
        "cache_key": transcript.cache_key,
        "request_id": transcript.request_id,
        "source_id": transcript.source_id,
        "pcm_analysis_key": transcript.pcm_analysis_key,
        "original_stem": transcript.original_stem,
        "source_start": _exact(transcript.source_start),
        "source_end": _exact(transcript.source_end),
        "sample_range": [transcript.sample_range.start, transcript.sample_range.end],
        "status": transcript.status,
        "draft": [_segment_body(record) for record in transcript.draft],
        "settled_spine": [_segment_body(record) for record in transcript.settled_spine],
        "repairs": [asdict(record) for record in transcript.repairs],
        "cues": [_cue_body(cue) for cue in transcript.cues],
        "error": transcript.error,
        "cut_truncated": transcript.cut_truncated,
    }
    return (json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _segment_body(segment: SourceSegment) -> dict[str, object]:
    return {
        "segment_id": segment.segment_id,
        "audio_start": _exact(segment.audio_start),
        "audio_end": _exact(segment.audio_end),
        "file_start": _exact(segment.file_start),
        "file_end": _exact(segment.file_end),
        "text": segment.text,
        "compression_ratio": segment.compression_ratio,
        "avg_logprob": segment.avg_logprob,
        "no_speech_prob": segment.no_speech_prob,
    }


def _cue_body(cue: SourceCue) -> dict[str, object]:
    return {
        "cue_id": cue.cue_id,
        "source_id": cue.source_id,
        "segment_ids": list(cue.segment_ids),
        "start": _exact(cue.start),
        "end": _exact(cue.end),
        "text": cue.text,
        "flags": list(cue.flags),
    }


def _publish_exclusive(destination: Path, body: bytes) -> Path | None:
    """완전한 파일을 원자적으로 독점 생성한다 — 조사 후 덮어쓰는 경주가 아니다.

    임시 이름에 전부 쓴 뒤 `os.link`로 옮겨 닮는다. 링크는 이미 자리가 차 있으면
    실패하므로, 반쯤 쓴 파일이 완성물 자리에 나타나는 일이 없다. 임시 파일은 이 번
    호출의 것을 치우는 것뿐 — 완성물을 치우는 길은 없다(P3-02와 같은 자).
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return destination if destination.read_bytes() == body else None
        return destination
    finally:
        temporary.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════
#  P4-04 — 경계 전사 결과를 슬라이스 큐로 갈아끼우는 투영
# ═══════════════════════════════════════════════════════════════


BOUNDARY_UNRESOLVED = "boundary_unresolved"
"""빈·실패 경계의 깃발 — 묶인 원문이 사람의 검토로 남아 있다는 기록이다."""

RETRYABLE_UNAVAILABLE_REASONS: frozenset[str] = frozenset({"stream_unavailable", "pcm_unavailable"})
"""되풀이 가능한 pre-PCM 부재 — 최종 수락을 막는 사유다. 선언된 무음은 완결이라 막지 않는다."""


@dataclass(frozen=True, slots=True)
class BoundaryProjection:
    """경계까지 반영된 입력 타임라인의 슬라이스 큐 — 자동 러프컷 이전의 SOURCE SRT.

    `cues`는 타임라인 순서 그대로다. 갈아끼운 큐는 `provenance="boundary"`이고,
    스코프 밖과 빈·실패 경계의 큐는 `provenance="source"` 그대로다. 트레이스는
    하나도 고치지 않는다 — 소스·경계 기록 모두 이 투영의 증거로 불변이다.
    """

    input_sha256: str
    cues: tuple[ProjectCue, ...]
    source_srt: str
    locked_instance_ids: frozenset[str]
    unavailable_instances: tuple[UnavailableInstance, ...]
    unresolved_request_ids: tuple[str, ...]
    unresolved_cue_ids: tuple[str, ...]


class _ProjectionLinkage:
    """요청·경계 기록·소스 트레이스를 신원으로만 묶는다 — 순서는 재료가 아니다."""

    def __init__(
        self,
        plan: BoundaryPlan,
        transcripts: Sequence[BoundaryTranscript],
        source_traces: Sequence[SourceTrace],
    ) -> None:
        self._requests = _index_by(
            plan.requests, "요청 신원이 중복된다", lambda request: request.request_id
        )
        self._traces = _index_by(
            source_traces, "소스와 PCM 키 하나에 트레이스가 둘 이상이다", _trace_pair
        )
        self._transcripts = _index_by(
            transcripts, "요청 하나에 경계 기록이 둘 이상이다", lambda item: item.request_id
        )
        for request_id in self._transcripts:
            if request_id not in self._requests:
                raise ValueError(
                    f"계획에 없는 요청 신원을 가진 경계 기록이다: {request_id!r} — 지어내지 않는다"
                )
        self._pairs = {request.request_id: self._resolve(request) for request in plan.requests}
        self.unresolved_request_ids = tuple(
            request.request_id
            for request in plan.requests
            if self._pairs[request.request_id][0].status != _STATUS_SUCCESS
        )

    def pair(self, request: BoundaryRequest) -> tuple[BoundaryTranscript, SourceTrace]:
        """요청 하나의 (경계 기록, 소스 트레이스) — 이미 검사된 짝이다."""
        return self._pairs[request.request_id]

    def _resolve(self, request: BoundaryRequest) -> tuple[BoundaryTranscript, SourceTrace]:
        transcript = self._transcripts.get(request.request_id)
        if transcript is None:
            raise ValueError(f"요청에 경계 기록이 없다: {request.request_id!r} — 지어내지 않는다")
        _require_transcript_matches_request(request, transcript)
        trace = self._traces.get(_request_pair(request))
        if trace is None:
            raise ValueError(
                f"소스의 전사 트레이스가 없다: source_id={request.source_id!r},"
                f" pcm_analysis_key={request.pcm_analysis_key!r} — 지어내지 않는다"
            )
        _require_transcript_origin(transcript, trace)
        return transcript, trace


def _request_pair(request: BoundaryRequest) -> str:
    return f"{request.source_id}\0{request.pcm_analysis_key}"


def _require_transcript_matches_request(
    request: BoundaryRequest, transcript: BoundaryTranscript
) -> None:
    """경계 기록은 요청의 연결 재료와 정확히 같아야 한다 — 어긋남은 모순이다."""
    if (
        transcript.source_id != request.source_id
        or transcript.pcm_analysis_key != request.pcm_analysis_key
        or transcript.source_start != request.source_start
        or transcript.source_end != request.source_end
        or transcript.sample_range != request.sample_range
    ):
        raise ValueError(
            f"경계 기록이 요청과 다르다: request {request.request_id!r}는"
            f" ({request.source_id!r}, {request.pcm_analysis_key!r},"
            f" [{request.source_start}, {request.source_end}), {request.sample_range})인데"
            f" 기록은 ({transcript.source_id!r}, {transcript.pcm_analysis_key!r},"
            f" [{transcript.source_start}, {transcript.source_end}),"
            f" {transcript.sample_range})다"
        )


def _require_transcript_origin(transcript: BoundaryTranscript, trace: SourceTrace) -> None:
    """경계 기록이 증명하는 원점 — 세그먼트의 두 시계 차이가 트레이스와 같아야 한다.

    경계 기록의 `audio_*`·`file_*`는 녹화 때 참조의 원점을 정확히 한 번 더해 만든
    값이다. 트레이스가 다른 저장 PCM 세대를 가리키면 이 차이가 어긋난다 — 큐의
    파일 시계가 통째로 밀리는 조용한 실패가 되므로 그 자리에서 닫는다.
    """
    for segment in (*transcript.draft, *transcript.settled_spine):
        baked = segment.file_start - segment.audio_start
        if baked != trace.pcm_sample0_file_time:
            raise ValueError(
                "경계 기록이 증명하는 원점이 트레이스와 다르다:"
                f" 기록 {baked} 대비 trace {trace.pcm_sample0_file_time}"
                f" (source_id={trace.source_id!r}, segment {segment.segment_id!r})"
            )


def _slice_timeline_shift(cues: Sequence[ProjectCue], request: BoundaryRequest) -> Fraction:
    """슬라이스의 타임라인 이동량 k — 묶인 큐의 두 시계에서 정확히 유도한다.

    슬라이스 안에서 `타임라인 = 파일 시계 + k`의 k는 상수다(이웃 합침이 양쪽
    시계를 같은 걸음으로 이어놓았다). 큐 하나가 주는 것은 k를 담는 닫힌 구간이다 —
    묶음은 시작을 늦게 만들고 끝을 일찍 만들 뿐이다. 구간들이 한 점으로 좁아지면
    그 점이 k다. 좁아지지 않는 세계는 큐 하나가 자루를 통째로 덮은 세계뿐인데,
    그때 요청은 자루 전체이므로 닿은 자루 끝 가설이 k를 정확히 고정한다. 그 밖의
    어긋남은 모순이거나 모호함이고, 둘 다 지어내지 않고 닫는다.
    """
    lo: Fraction | None = None
    hi: Fraction | None = None
    for cue in cues:
        end_gap = cue.timeline_end - cue.source_end
        start_gap = cue.timeline_start - cue.source_start
        lo = end_gap if lo is None else max(lo, end_gap)
        hi = start_gap if hi is None else min(hi, start_gap)
    if lo is None or hi is None:
        raise ValueError(
            f"타임라인 닻이 될 큐가 없다: slice {request.slice_id!r} — 투영할 재료가 아니다"
        )
    if lo > hi:
        raise ValueError(
            f"타임라인 닻이 모순이다: slice {request.slice_id!r}의 큐들이 공유하는"
            f" 이동량이 없다 [{lo}, {hi}]"
        )
    if lo == hi:
        return lo
    earliest = min(cue.timeline_start for cue in cues)
    latest = max(cue.timeline_end for cue in cues)
    anchored = {
        shift
        for shift in (earliest - request.source_start, latest - request.source_end)
        if lo <= shift <= hi
    }
    if len(anchored) == 1:
        return anchored.pop()
    raise ValueError(
        f"타임라인 닻이 유일하지 않다: slice {request.slice_id!r}의 이동량이"
        f" [{lo}, {hi}]에서 하나로 좁혀지지 않는다"
    )


def _in_scope(cue: ProjectCue, request: BoundaryRequest) -> bool:
    """큐가 요청의 (슬라이스, 인스턴스, 소스 구간) 스코프 안인가 — 반개 겹침."""
    return (
        cue.slice_id == request.slice_id
        and not set(cue.instance_ids).isdisjoint(request.instance_ids)
        and cue.source_start < request.source_end
        and request.source_start < cue.source_end
    )


def _with_flag(cue: ProjectCue, flag: str) -> ProjectCue:
    """큐에 깃발 하나를 얹는다 — 이미 있으면 그대로 돌려준다."""
    if flag in cue.flags:
        return cue
    return replace(cue, flags=(*cue.flags, flag))


def _replaced_cues(
    plan: BoundaryPlan, linkage: _ProjectionLinkage
) -> tuple[tuple[ProjectCue, ...], tuple[ProjectCue, ...]]:
    """스코프 안의 소스 큐를 치우고 경계 큐를 세운다 — 빈·실패는 깃발만 남긴다."""
    cues = plan.clamped_cues
    by_slice: dict[str, tuple[ProjectCue, ...]] = {}
    for cue in cues:
        by_slice[cue.slice_id] = (*by_slice.get(cue.slice_id, ()), cue)
    shifts: dict[str, Fraction] = {}
    doomed: set[int] = set()
    unresolved: set[int] = set()
    additions: list[ProjectCue] = []
    for request in plan.requests:
        transcript, trace = linkage.pair(request)
        scope = [index for index, cue in enumerate(cues) if _in_scope(cue, request)]
        if transcript.status != _STATUS_SUCCESS:
            unresolved.update(scope)  # 원문이 그대로 검토로 남는다
            continue
        doomed.update(scope)
        shift = shifts.get(request.slice_id)
        if shift is None:
            shift = _slice_timeline_shift(by_slice.get(request.slice_id, ()), request)
            shifts[request.slice_id] = shift
        additions.extend(_boundary_project_cues(request, transcript, trace, shift))
    kept = tuple(
        _with_flag(cue, BOUNDARY_UNRESOLVED) if index in unresolved else cue
        for index, cue in enumerate(cues)
        if index not in doomed
    )
    return kept, tuple(additions)


def _boundary_project_cues(
    request: BoundaryRequest,
    transcript: BoundaryTranscript,
    trace: SourceTrace,
    shift: Fraction,
) -> tuple[ProjectCue, ...]:
    """경계 큐를 디코드 국소 시계에서 슬라이스 큐로 옮긴다 — 다듬지 않는다.

    원점은 여기서 정확히 한 번 더해 파일 시계가 되고, 슬라이스 이동량을 더해
    타임라인이 된다. 큐는 요청 구간 안에 순서 있게 양수 길이로 산다 — 위조된
    기록은 그 자리에서 닫는다.
    """
    origin = trace.pcm_sample0_file_time
    projected: list[ProjectCue] = []
    previous_end: Fraction | None = None
    for cue in transcript.cues:
        if cue.source_id != transcript.source_id:
            raise ValueError(
                f"경계 큐의 소스 신원이 기록과 다르다: cue {cue.cue_id!r}는"
                f" {cue.source_id!r}인데 기록은 {transcript.source_id!r}다"
            )
        file_start = cue.start + origin
        file_end = cue.end + origin
        previous_end = _require_boundary_span(cue, request, file_start, file_end, previous_end)
        projected.append(
            ProjectCue(
                cue_id=cue.cue_id,
                slice_id=request.slice_id,
                instance_ids=request.instance_ids,
                source_id=cue.source_id,
                source_start=file_start,
                source_end=file_end,
                timeline_start=shift + file_start,
                timeline_end=shift + file_end,
                text=cue.text,
                provenance="boundary",
                flags=cue.flags,
            )
        )
    return tuple(projected)


def _require_boundary_span(
    cue: SourceCue,
    request: BoundaryRequest,
    file_start: Fraction,
    file_end: Fraction,
    previous_end: Fraction | None,
) -> Fraction:
    """경계 큐 한 장의 구간 검사 — 통과하면 이 큐의 파일 시계 끝을 돌려준다."""
    if file_end <= file_start:
        raise ValueError(
            f"경계 큐의 길이가 0 이하다: cue {cue.cue_id!r}, [{file_start}, {file_end})"
        )
    if file_start < request.source_start or file_end > request.source_end:
        raise ValueError(
            f"경계 큐가 요청 구간 밖이다: cue {cue.cue_id!r}는 [{file_start}, {file_end})"
            f"인데 요청은 [{request.source_start}, {request.source_end})다"
        )
    if previous_end is not None and file_start < previous_end:
        raise ValueError(
            f"경계 큐가 서로 겹치거나 순서가 뒤집혔다: cue {cue.cue_id!r}의 시작"
            f" {file_start}초가 앞 큐 끝 {previous_end}초보다 앞이다"
        )
    return file_end


def _ordered_projection(cues: Sequence[ProjectCue]) -> tuple[ProjectCue, ...]:
    """투영 큐를 타임라인 순서로 세운다 — 겹침·길이 0 이하는 닫는다.

    소스 큐는 스토리라인 순서 그대로 왔고 경계 큐는 정확한 자리에 들어가므로
    정렬은 순서를 고치는 것이 아니라 두 출처를 한 타임라인에서 다시 확인하는 것이다.
    """
    ordered = tuple(sorted(cues, key=lambda cue: (cue.timeline_start, cue.timeline_end)))
    previous_end: Fraction | None = None
    for cue in ordered:
        if cue.timeline_end <= cue.timeline_start:
            raise ValueError(
                f"투영 큐의 길이가 0 이하다: cue {cue.cue_id!r},"
                f" [{cue.timeline_start}, {cue.timeline_end})"
            )
        if previous_end is not None and cue.timeline_start < previous_end:
            raise ValueError(
                f"투영 큐가 타임라인에서 겹친다: cue {cue.cue_id!r}의 시작"
                f" {cue.timeline_start}초가 앞 큐 끝 {previous_end}초보다 앞이다"
            )
        previous_end = cue.timeline_end
    return ordered


def _split_by_representability(
    cues: Sequence[ProjectCue],
) -> tuple[tuple[ProjectCue, ...], tuple[str, ...]]:
    """half-up 뒤 0이 되는 투영 큐를 갈라낸다 — 큐는 남기고 SRT만 뺀다.

    미해결 신원은 **지금 투영된 큐**에서 다시 유도한다: 성공한 경계가 옛 묶인
    큐를 대체하면 그 옛 신원은 따라 내려가고, 남은 접힘은 그대로 남는다. 상류
    `SourceCaptionSet`은 옛 접힘의 불변 증거로 그 자리에 있다.
    """
    final: list[ProjectCue] = []
    unresolved: list[str] = []
    for cue in cues:
        if milliseconds_half_up(cue.timeline_end) <= milliseconds_half_up(cue.timeline_start):
            cue = _with_flag(cue, SUBMS_COLLAPSED)
            if cue.cue_id not in unresolved:
                unresolved.append(cue.cue_id)
        final.append(cue)
    return tuple(final), tuple(unresolved)


def _projection_srt(cues: Sequence[ProjectCue]) -> str:
    """투영 큐의 SOURCE SRT — 접힌 장만 빼고 P3-03과 같은 형식으로 직렬화한다.

    half-up 양자화는 `format_srt_time`이 직렬화 순간에 딱 한 번 한다. 순서·양수
    길이·겹침 검사는 이미 끝났고, 접힌 큐는 호출 쪽이 이미 걷어냈다.
    """
    blocks: list[str] = []
    emitted = (cue for cue in cues if SUBMS_COLLAPSED not in cue.flags)
    for number, cue in enumerate(emitted, start=1):
        timeline = f"{format_srt_time(cue.timeline_start)} --> {format_srt_time(cue.timeline_end)}"
        blocks.append(f"{number}\n{timeline}\n{cue.text}\n")
    return "\n".join(blocks)


def _require_locked_unavailable(plan: BoundaryPlan) -> None:
    """unavailable 기록의 인스턴스는 잠겨 있어야 한다 — 아니면 계획이 모순이다."""
    for record in plan.unavailable_instances:
        if record.instance_id not in plan.locked_instance_ids:
            raise ValueError(
                "unavailable 기록의 인스턴스가 잠겨 있지 않다:"
                f" instance {record.instance_id!r}"
                f" (resource {record.resource_scoped_id!r}, {record.reason_code!r})"
                " — 부재는 잠금과 함께 온다"
            )


def project_boundaries(
    plan: BoundaryPlan,
    transcripts: Sequence[BoundaryTranscript],
    source: SourceCaptionSet,
    source_traces: Sequence[SourceTrace],
) -> BoundaryProjection:
    """성공한 경계 전사를 슬라이스 큐에 갈아끼워 입력 타임라인 SOURCE SRT를 다시 낸다.

    순수한 투영이다 — 모델 호출·오디오 추출·경계 재계획·조판이 없고, 소스·경계
    트레이스는 하나도 고치지 않는다. 갈아끼우기는 정확히 (슬라이스, 인스턴스,
    소스 구간) 스코프이고, 빈·실패 경계는 묶인 원문에 `boundary_unresolved` 깃발과
    요청 신원만 남긴다. 잠금·부재는 계획의 것을 그대로 내놓는다.
    """
    _require_locked_unavailable(plan)
    linkage = _ProjectionLinkage(plan, transcripts, source_traces)
    kept, additions = _replaced_cues(plan, linkage)
    ordered = _ordered_projection((*kept, *additions))
    final, unresolved_cue_ids = _split_by_representability(ordered)
    return BoundaryProjection(
        input_sha256=source.input_sha256,
        cues=final,
        source_srt=_projection_srt(final),
        locked_instance_ids=plan.locked_instance_ids,
        unavailable_instances=plan.unavailable_instances,
        unresolved_request_ids=linkage.unresolved_request_ids,
        unresolved_cue_ids=unresolved_cue_ids,
    )
