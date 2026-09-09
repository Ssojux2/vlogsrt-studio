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
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import AssetResource, ClipInstance, ProjectDocument

_SLICE_ID_DOMAIN = "vlogsrt-logical-slice-v1"
"""슬라이스 신원의 도메인 구분자 — 인스턴스·리소스 해시와 우연히 겹치지 않게 한다."""

__all__ = ["AuthoredBlade", "LogicalSlice", "logical_slices"]


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
