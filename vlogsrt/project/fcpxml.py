"""FCPXML 1.14 지정 부분집합 파서 — 표준 라이브러리만, 정규화 없이.

받는 것은 **평평한 주요 스토리라인**뿐이다: `library/event/project/sequence/spine`
하나씩, 스파인의 직속 자식은 `asset-clip`과 `gap`만. 그 밖의 구성 — 연결
클립/오디오, compound/sync/multicam, 리타임, 전환, 오디오 라우팅, 시간 가변
효과 — 은 전부 위치 경로와 이유와 함께 거부한다(계획 "Input and output").
눌러 펴지는(flaten) 일이 없다.

시각은 전부 P1-01 timebase의 `Fraction`이고, 격자 검사도 그 정의를 쓴다:
타임라인 격자는 `sequence.tcStart`에, 소스 격자는 `asset.start`에 묶는다.
프레임 레이트는 소스와 프로젝트가 **같은** 지원 레이트여야 한다 — 30 대 29.97
같은 어긋남은 리샘플링하지 않고 거부한다.

원본은 절대 다시 쓰지 않는다. `raw_xml` 바이트를 그대로 들고, 파싱 트리는
읽기만 한다 — 원본 시각 문자열의 정규화는 일어나지 않는다(P1-05 왕복의 기반).
DTD 검증은 이곳이 아니라 P1-04의 xmllint 경계다. 엔티티 전개 같은 악의적
입력의 방어도 그 경계와 함께 정해진다 — 여기서는 구문 해석만 한다.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Literal, cast

from vlogsrt.project import timebase
from vlogsrt.project.models import (
    AssetResource,
    ClipInstance,
    GapInstance,
    MediaRep,
    ProjectDocument,
    TimelineItem,
    UnsupportedProject,
)

SUPPORTED_VERSION = "1.14"
"""이 파서가 아는 유일한 FCPXML 버전 — 다른 버전은 이름을 들어 거부한다."""

_BUNDLE_INFO_NAME = "Info.fcpxml"

_BENIGN_CLIP_METADATA = frozenset({"note", "keyword", "marker", "rating"})
"""시각도 신원도 바꾸지 않는 정적 메타데이터 — 이것만 클립 안에 머물 수 있다."""

_SPINE_CHILD_REASONS = {
    "transition": "전환(transition)은 지원하지 않는다",
    "title": "타이틀(title)은 지원하지 않는다",
    "caption": "불러온 자막(caption)은 지원하지 않는다",
    "video": "연결 비디오(video)는 지원하지 않는다",
    "audio": "연결 오디오(audio)는 지원하지 않는다",
    "audio-clip": "연결 오디오(audio-clip)는 지원하지 않는다",
    "ref-clip": "compound/multicam 참조(ref-clip)는 지원하지 않는다",
    "sync-clip": "sync-clip은 지원하지 않는다",
    "compound-clip": "compound-clip은 지원하지 않는다",
    "spine": "중첩 스토리라인(spine)은 지원하지 않는다",
}

_RESOURCE_CHILD_REASONS = {
    "compound-clip": "compound-clip 리소스는 지원하지 않는다",
    "multicam-clip": "multicam-clip 리소스는 지원하지 않는다",
}

_CLIP_CHILD_REASONS = {
    "retime": "리타임(retime)은 지원하지 않는다 — 정상 속도만 지원한다",
    "audio": "오디오 라우팅(audio)은 지원하지 않는다",
    "video": "비디오 속성(video)은 지원하지 않는다",
    "effect": "시간 가변 효과(effect)는 지원하지 않는다",
}

_MEDIA_REP_KINDS = ("original", "proxy")


def resolve_info_path(path: Path) -> Path:
    """입력의 실제 XML 경로 — `.fcpxml` 파일 그대로, `.fcpxmld` 번들은 `Info.fcpxml`."""
    if path.suffix == ".fcpxmld":
        return path / _BUNDLE_INFO_NAME
    if path.suffix == ".fcpxml":
        return path
    raise UnsupportedProject(
        location=str(path),
        reason="지원하지 않는 입력 형태다 — .fcpxml 파일 또는 .fcpxmld 번들이어야 한다",
    )


def parse_project(path: Path) -> ProjectDocument:
    """입력 프로젝트를 불변 문서로 파싱한다. 읽기만 한다 — 절대 다시 쓰지 않는다."""
    info_path = resolve_info_path(path)
    if info_path.is_dir():
        raise UnsupportedProject(str(path), f"입력 경로가 디렉터리다: {info_path}")
    if not info_path.exists():
        raise UnsupportedProject(str(path), f"입력 파일이 없다: {info_path}")
    raw_xml = info_path.read_bytes()
    input_sha256 = hashlib.sha256(raw_xml).hexdigest()
    root = _parse_root(info_path, raw_xml)
    paths = _index_paths(root)
    version = _require_version(root, paths)
    library = _sole_child(root, "library", paths)
    event = _sole_child(library, "event", paths)
    project = _sole_child(event, "project", paths)
    sequence = _sole_child(project, "sequence", paths)
    spine = _sole_child(sequence, "spine", paths)
    resources_element = _sole_child(root, "resources", paths)
    frame_durations, seen_ids, asset_elements = _collect_formats(resources_element, paths)
    frame_duration, tc_start, sequence_duration = _parse_sequence_header(
        sequence, frame_durations, paths
    )
    assets = _parse_assets(
        asset_elements, frame_durations, seen_ids, paths, input_sha256, frame_duration
    )
    assets_by_id = {asset.xml_id: asset for asset in assets}
    storyline = _parse_storyline(
        spine, paths, assets_by_id, frame_duration, tc_start, sequence_duration, input_sha256
    )
    return ProjectDocument(
        input_path=path,
        info_path=info_path,
        input_sha256=input_sha256,
        raw_xml=raw_xml,
        version=version,
        sequence_tc_start=tc_start,
        frame_duration=frame_duration,
        resources=assets,
        storyline=storyline,
    )


# ─── 문서 구조 ───────────────────────────────────────────────────────────


def _parse_root(info_path: Path, raw_xml: bytes) -> ET.Element:
    if b"<!ENTITY" in raw_xml:
        raise UnsupportedProject(
            str(info_path),
            "엔티티 선언은 지원하지 않는다 — DTD 내부 선언이 있는 입력은 받지 않는다",
        )
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as error:
        raise UnsupportedProject(str(info_path), f"XML 파싱에 실패했다: {error}") from error
    if root.tag != "fcpxml":
        raise UnsupportedProject(str(info_path), f"루트 요소가 fcpxml이 아니다: {root.tag!r}")
    return root


def _require_version(root: ET.Element, paths: dict[int, str]) -> str:
    version = root.get("version")
    if version is None:
        raise UnsupportedProject(
            paths[id(root)], f"fcpxml 요소에 version 속성이 없다 — {SUPPORTED_VERSION}여야 한다"
        )
    if version != SUPPORTED_VERSION:
        raise UnsupportedProject(
            paths[id(root)],
            f"지원하지 않는 FCPXML 버전이다: {version!r} — {SUPPORTED_VERSION}만 지원한다",
        )
    return version


def _index_paths(root: ET.Element) -> dict[int, str]:
    """온 트리의 1기반 위치 경로 — 같은 태그의 형제끼리만 센다.

    `/fcpxml[1]/library[1]/…/asset-clip[3]` — 똑같이 생긴 형제도 자리가 다르면
    경로가 다르고, 그래서 인스턴스 신원도 다르다.
    """

    def visit(element: ET.Element, parent_path: str) -> None:
        counts: dict[str, int] = {}
        for child in element:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            path = f"{parent_path}/{child.tag}[{counts[child.tag]}]"
            paths[id(child)] = path
            visit(child, path)

    paths: dict[int, str] = {id(root): f"/{root.tag}[1]"}
    visit(root, paths[id(root)])
    return paths


def _sole_child(element: ET.Element, tag: str, paths: dict[int, str]) -> ET.Element:
    where = paths[id(element)]
    matches = [child for child in element if child.tag == tag]
    if not matches:
        raise UnsupportedProject(where, f"{tag} 요소가 없다 — 정확히 하나여야 한다")
    if len(matches) > 1:
        raise UnsupportedProject(where, f"{tag} 요소가 {len(matches)}개다 — 정확히 하나여야 한다")
    return matches[0]


# ─── 속성 읽기 ───────────────────────────────────────────────────────────


def _required_attribute(element: ET.Element, name: str, where: str) -> str:
    value = element.get(name)
    if value is None:
        raise UnsupportedProject(where, f"필수 속성 {name!r}이(가) 없다")
    return value


def _parse_time_attribute(element: ET.Element, name: str, where: str) -> Fraction:
    text = _required_attribute(element, name, where)
    try:
        return timebase.parse_fcpx_time(text)
    except ValueError as error:
        raise UnsupportedProject(where, f"{name} 값이 무효다: {text!r} ({error})") from error


def _bool_attribute(element: ET.Element, name: str, where: str, *, default: bool) -> bool:
    value = element.get(name)
    if value is None:
        return default
    if value == "1":
        return True
    if value == "0":
        return False
    raise UnsupportedProject(where, f"{name} 값이 무효다: {value!r} — 0 또는 1이어야 한다")


def _reject_forbidden_attribute(element: ET.Element, name: str, where: str, reason: str) -> None:
    if name in element.attrib:
        raise UnsupportedProject(where, reason)


def _scoped_resource_id(input_sha256: str, xml_id: str) -> str:
    """리소스 신원 — 문서 해시가 스코프다. `r2` 같은 낡은 id는 그 자체로 신원이 아니다."""
    return hashlib.sha256(f"{input_sha256}:resource:{xml_id}".encode()).hexdigest()


def _instance_id(input_sha256: str, element_path: str) -> str:
    """인스턴스 신원 — 같은 애셋의 반복도 자리가 다르면 다른 인스턴스다."""
    return hashlib.sha256(f"{input_sha256}:instance:{element_path}".encode()).hexdigest()


# ─── 리소스 ─────────────────────────────────────────────────────────────


def _collect_formats(
    resources_element: ET.Element, paths: dict[int, str]
) -> tuple[dict[str, Fraction], set[str], list[ET.Element]]:
    """자식 형태를 검사하고 format을 먼저 얻는다 — 애셋은 뒤에 온다."""
    frame_durations: dict[str, Fraction] = {}
    seen_ids: set[str] = set()
    asset_elements: list[ET.Element] = []
    for child in resources_element:
        where = paths[id(child)]
        if child.tag == "asset":
            asset_elements.append(child)
            continue
        if child.tag != "format":
            reason = _RESOURCE_CHILD_REASONS.get(
                child.tag, f"지원하지 않는 리소스 요소다: {child.tag}"
            )
            raise UnsupportedProject(where, reason)
        fmt_id = _required_attribute(child, "id", where)
        _reject_duplicate_id(fmt_id, seen_ids, where)
        frame_duration = _parse_time_attribute(child, "frameDuration", where)
        try:
            timebase.require_supported_frame_duration(frame_duration)
        except ValueError as error:
            raise UnsupportedProject(where, str(error)) from error
        frame_durations[fmt_id] = frame_duration
    return frame_durations, seen_ids, asset_elements


def _reject_duplicate_id(xml_id: str, seen_ids: set[str], where: str) -> None:
    if xml_id in seen_ids:
        raise UnsupportedProject(where, f"중복 리소스 id다: {xml_id!r}")
    seen_ids.add(xml_id)


def _parse_assets(
    asset_elements: list[ET.Element],
    frame_durations: dict[str, Fraction],
    seen_ids: set[str],
    paths: dict[int, str],
    input_sha256: str,
    sequence_frame_duration: Fraction,
) -> tuple[AssetResource, ...]:
    assets = [
        _parse_asset(
            element, frame_durations, seen_ids, paths, input_sha256, sequence_frame_duration
        )
        for element in asset_elements
    ]
    return tuple(assets)


def _parse_asset(
    element: ET.Element,
    frame_durations: dict[str, Fraction],
    seen_ids: set[str],
    paths: dict[int, str],
    input_sha256: str,
    sequence_frame_duration: Fraction,
) -> AssetResource:
    where = paths[id(element)]
    xml_id = _required_attribute(element, "id", where)
    _reject_duplicate_id(xml_id, seen_ids, where)
    name = _required_attribute(element, "name", where)
    format_id = _required_attribute(element, "format", where)
    start = _parse_time_attribute(element, "start", where)
    duration = _parse_time_attribute(element, "duration", where)
    has_audio = _bool_attribute(element, "hasAudio", where, default=True)
    frame_duration = frame_durations.get(format_id)
    if frame_duration is None:
        raise UnsupportedProject(where, f"참조를 해석할 수 없다: format {format_id!r}")
    if frame_duration != sequence_frame_duration:
        raise UnsupportedProject(
            where,
            f"소스/프로젝트 프레임 레이트가 다르다: 애셋 {frame_duration}초 ≠"
            f" 시퀀스 {sequence_frame_duration}초 — 리샘플링하지 않고 거부한다",
        )
    return AssetResource(
        xml_id=xml_id,
        scoped_id=_scoped_resource_id(input_sha256, xml_id),
        uid=element.get("uid", ""),
        name=name,
        format_id=format_id,
        frame_duration=frame_duration,
        start=start,
        duration=duration,
        has_audio=has_audio,
        media_reps=_parse_media_reps(element, paths),
    )


def _parse_media_reps(element: ET.Element, paths: dict[int, str]) -> tuple[MediaRep, ...]:
    reps: list[MediaRep] = []
    for child in element:
        where = paths[id(child)]
        if child.tag != "media-rep":
            raise UnsupportedProject(where, f"지원하지 않는 asset 자식 요소다: {child.tag}")
        kind = _required_attribute(child, "kind", where)
        if kind not in _MEDIA_REP_KINDS:
            raise UnsupportedProject(
                where, f"media-rep kind가 무효다: {kind!r} — original 또는 proxy여야 한다"
            )
        src = _required_attribute(child, "src", where)
        reps.append(MediaRep(kind=cast(Literal["original", "proxy"], kind), src=src))
    return tuple(reps)


# ─── 시퀀스와 스토리라인 ────────────────────────────────────────────────


def _parse_sequence_header(
    sequence: ET.Element, frame_durations: dict[str, Fraction], paths: dict[int, str]
) -> tuple[Fraction, Fraction, Fraction]:
    """(frame_duration, tc_start, duration) — 격자의 앵커가 여기 정해진다."""
    where = paths[id(sequence)]
    format_id = _required_attribute(sequence, "format", where)
    frame_duration = frame_durations.get(format_id)
    if frame_duration is None:
        raise UnsupportedProject(where, f"참조를 해석할 수 없다: format {format_id!r}")
    tc_start = _parse_time_attribute(sequence, "tcStart", where)
    duration = _parse_time_attribute(sequence, "duration", where)
    _require_on_grid(duration, Fraction(0), frame_duration, "duration", where)
    if duration <= 0:
        raise UnsupportedProject(where, f"duration이 0 이하다: {duration}초")
    return frame_duration, tc_start, duration


def _parse_storyline(
    spine: ET.Element,
    paths: dict[int, str],
    assets_by_id: dict[str, AssetResource],
    frame_duration: Fraction,
    tc_start: Fraction,
    sequence_duration: Fraction,
    input_sha256: str,
) -> tuple[TimelineItem, ...]:
    sequence_end = tc_start + sequence_duration
    items: list[TimelineItem] = []
    previous_end: Fraction | None = None
    for child in spine:
        where = paths[id(child)]
        if child.tag == "asset-clip":
            item: TimelineItem = _parse_asset_clip(
                child, paths, assets_by_id, frame_duration, tc_start, input_sha256
            )
        elif child.tag == "gap":
            item = _parse_gap(child, paths, frame_duration, tc_start, input_sha256)
        else:
            reason = _SPINE_CHILD_REASONS.get(
                child.tag, f"지원하지 않는 스토리라인 요소다: {child.tag}"
            )
            raise UnsupportedProject(where, reason)
        _check_placement(item, previous_end, tc_start, sequence_end, where)
        previous_end = item.offset + item.duration
        items.append(item)
    return tuple(items)


def _parse_asset_clip(
    element: ET.Element,
    paths: dict[int, str],
    assets_by_id: dict[str, AssetResource],
    frame_duration: Fraction,
    tc_start: Fraction,
    input_sha256: str,
) -> ClipInstance:
    where = paths[id(element)]
    _reject_forbidden_attribute(element, "lane", where, "연결 클립(lane)은 지원하지 않는다")
    _reject_forbidden_attribute(
        element, "audioOffset", where, "오디오 타이밍 오버라이드(audioOffset)는 지원하지 않는다"
    )
    _reject_clip_children(element, paths)
    ref = _required_attribute(element, "ref", where)
    asset = assets_by_id.get(ref)
    if asset is None:
        raise UnsupportedProject(where, f"참조를 해석할 수 없다: asset {ref!r}")
    offset = _parse_time_attribute(element, "offset", where)
    start = _parse_time_attribute(element, "start", where)
    duration = _parse_time_attribute(element, "duration", where)
    _require_on_grid(offset, tc_start, frame_duration, "offset", where)
    _require_positive_frames(duration, frame_duration, where)
    _require_on_grid(start, asset.start, frame_duration, "start", where)
    _require_source_bounds(start, duration, asset, where)
    return ClipInstance(
        instance_id=_instance_id(input_sha256, where),
        element_path=where,
        resource_scoped_id=asset.scoped_id,
        offset=offset,
        start=start,
        duration=duration,
        enabled=_bool_attribute(element, "enabled", where, default=True),
        src_enable=_src_enable_attribute(element, where),
    )


def _parse_gap(
    element: ET.Element,
    paths: dict[int, str],
    frame_duration: Fraction,
    tc_start: Fraction,
    input_sha256: str,
) -> GapInstance:
    where = paths[id(element)]
    _reject_forbidden_attribute(element, "lane", where, "연결 클립(lane)은 지원하지 않는다")
    _reject_clip_children(element, paths)
    offset = _parse_time_attribute(element, "offset", where)
    duration = _parse_time_attribute(element, "duration", where)
    _require_on_grid(offset, tc_start, frame_duration, "offset", where)
    _require_positive_frames(duration, frame_duration, where)
    return GapInstance(
        instance_id=_instance_id(input_sha256, where),
        element_path=where,
        offset=offset,
        duration=duration,
    )


def _src_enable_attribute(element: ET.Element, where: str) -> Literal["all", "video"]:
    value = element.get("srcEnable", "all")
    if value in ("all", "video"):
        return cast(Literal["all", "video"], value)
    if value == "audio":
        raise UnsupportedProject(
            where, "srcEnable='audio'는 이 부분집합 밖이다 — 오디오만 쓰는 클립은 지원하지 않는다"
        )
    raise UnsupportedProject(where, f"srcEnable 값이 무효다: {value!r} — all 또는 video여야 한다")


def _reject_clip_children(element: ET.Element, paths: dict[int, str]) -> None:
    """asset-clip/gap 안에 올 수 있는 것은 정적 메타데이터뿐이다."""
    for child in element:
        if child.tag in _BENIGN_CLIP_METADATA:
            continue
        where = paths[id(child)]
        reason = _CLIP_CHILD_REASONS.get(
            child.tag, f"지원하지 않는 asset-clip/gap 자식 요소다: {child.tag}"
        )
        raise UnsupportedProject(where, reason)


# ─── 격자와 구간 검사 ────────────────────────────────────────────────────


def _require_on_grid(
    value: Fraction, anchor: Fraction, frame_duration: Fraction, name: str, where: str
) -> None:
    if (value - anchor) % frame_duration != 0:
        raise UnsupportedProject(
            where, f"{name}이(가) 프레임 격자에서 벗어난다: {value}초 (앵커 {anchor}초)"
        )


def _require_positive_frames(duration: Fraction, frame_duration: Fraction, where: str) -> None:
    _require_on_grid(duration, Fraction(0), frame_duration, "duration", where)
    if duration <= 0:
        raise UnsupportedProject(where, f"duration이 0 이하다: {duration}초")


def _require_source_bounds(
    start: Fraction, duration: Fraction, asset: AssetResource, where: str
) -> None:
    asset_end = asset.start + asset.duration
    if start < asset.start or start + duration > asset_end:
        raise UnsupportedProject(
            where,
            f"소스 범위가 애셋(asset) 구간을 벗어난다:"
            f" [{start}초, {start + duration}초) ⊄ [{asset.start}초, {asset_end}초)",
        )


def _check_placement(
    item: TimelineItem,
    previous_end: Fraction | None,
    tc_start: Fraction,
    sequence_end: Fraction,
    where: str,
) -> None:
    item_end = item.offset + item.duration
    if item.offset < tc_start or item_end > sequence_end:
        raise UnsupportedProject(
            where,
            f"타임라인 범위가 시퀀스(sequence) 구간을 벗어난다:"
            f" [{item.offset}초, {item_end}초) ⊄ [{tc_start}초, {sequence_end}초)",
        )
    if previous_end is not None and item.offset < previous_end:
        raise UnsupportedProject(
            where,
            f"스토리라인 항목이 문서 순서·시각 순서에서 벗어난다(겹침 또는 역순):"
            f" offset {item.offset}초 < 앞 항목 끝 {previous_end}초",
        )
