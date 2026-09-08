"""FCPXML 1.14 지정 부분집합 파서 — 표준 라이브러리만, 정규화 없이.

받는 것은 **평평한 주요 스토리라인**뿐이다: `library/event/project/sequence/spine`
하나씩, 스파인의 직속 자식은 `asset-clip`과 `gap`만. 그 밖의 구성 — 연결
클립/오디오, compound/sync/multicam, 리타임, 전환, 오디오 라우팅, 시간 가변
효과 — 은 전부 위치 경로와 이유와 함께 거부한다(계획 "Input and output").
눌러 펴지는(flaten) 일이 없다.

시각은 전부 P1-01 timebase의 `Fraction`이고, 격자 검사도 그 정의를 쓴다:
타임라인 격자는 `sequence.tcStart`에, 소스 격자는 `asset.start`에 묶는다.
프레임 레이트는 소스와 프로젝트가 **같은** 지원 레이트여야 한다 — 30 대 29.97
같은 어긋남은 리샘플링하지 않고 거부한다. spine·asset-clip의 선택 `format`
오버라이도 같은 격자여야만 한다.

어휘는 설치된 1.14 DTD를 따른다: media-rep kind는 `original-media`/
`proxy-media`(생략하면 original-media)이고 내부 `MediaRep.kind`로 옮겨 읽는다.
애셋에는 media-rep이 최소 하나 있어야 한다(내용 모델 `media-rep+`).
`hasAudio`는 명시적 0/1만 받는다 — 생략은 문법상 유효해도 이 준비 부분집합
밖이라 추측하지 않는다. 애셋 `name`은 선택(빈 문자열), asset-clip의
`offset`·`start`는 생략하면 정확히 0s다. `lane="0"`은 '부모 안'이라 주요
스토리라인으로 유효하고, 0 외의 lane은 연결 클립이라 거부한다. gap에는
lane이 아예 없다.

원본은 절대 다시 쓰지 않는다. `raw_xml` 바이트를 그대로 들고, 파싱 트리는
읽기만 한다 — 원본 시각 문자열의 정규화는 일어나지 않는다(P1-05 왕복의 기반).
DTD 검증은 이곳이 아니라 P1-04의 xmllint 경계다. 엔티티 전개 같은 악의적
입력의 방어도 그 경계와 함께 정해진다 — 여기서는 구문 해석만 한다.

P1-05 왕복은 이 파일의 두 번째 얼굴이다. `write_identity`가 `raw_xml`을 바이트
그대로 옮기고 `validate_roundtrip`이 그 사본을 증명한다 — 검증 판정은 전부 P1-04
경계(`xml_validation`)에서 오고 이 모듈은 subprocess를 모른다. 비교는 바이트
동등성 ＋ 해시 스코프 필드를 뺀 의미 동등성이다. 이것은 편집 직렬화가 아니다.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Literal, cast

from vlogsrt.project import timebase, xml_validation
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

_BENIGN_CLIP_METADATA = frozenset(
    {
        "note",
        "keyword",
        "marker",
        "chapter-marker",
        "rating",
        "analysis-marker",
        "hidden-clip-marker",
        "metadata",
    }
)
"""시각도 신원도 바꾸지 않는 정적 메타데이터 — DTD의 marker_item ∪ note ∪ metadata.

`%marker_item;`은 marker·chapter-marker·rating·keyword·analysis-marker·
hidden-clip-marker고 asset-clip/gap 뒤에 `metadata?`가 온다. 그 철자
그대로만 머물 수 있다.
"""

# XML의 kind 철자(원본 DTD) → 내부 MediaRep.kind. kind를 빼면 DTD 기본값
# original-media다.
_MEDIA_REP_XML_KINDS: dict[str, Literal["original", "proxy"]] = {
    "original-media": "original",
    "proxy-media": "proxy",
}

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

# 분할 편집·오디오 시각 오버라이드 — 오디오 시각을 따로 미는 속성은 전부 거부한다.
_AUDIO_TIMING_OVERRIDE_REASONS = {
    "audioOffset": "오디오 타이밍 오버라이드(audioOffset)는 지원하지 않는다",
    "audioStart": "분할 편집 오버라이드(audioStart)는 지원하지 않는다",
    "audioDuration": "분할 편집 오버라이드(audioDuration)는 지원하지 않는다",
}


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
        spine,
        paths,
        assets_by_id,
        frame_durations,
        frame_duration,
        tc_start,
        sequence_duration,
        input_sha256,
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


def _required_bool_attribute(element: ET.Element, name: str, where: str) -> bool:
    """명시적 0/1만 받는다 — 생략은 문법상 유효해도 이 준비 부분집합 밖이다.

    `hasAudio`의 생략을 true/false로 추측하지 않는다: '오디오 없음'은 오직
    명시적인 0만 선언한다.
    """
    value = element.get(name)
    if value is None:
        raise UnsupportedProject(
            where,
            f"{name} 선언이 없다 — 이 준비 부분집합에서는 명시적인 0 또는 1이 필요하다",
        )
    if value == "1":
        return True
    if value == "0":
        return False
    raise UnsupportedProject(where, f"{name} 값이 무효다: {value!r} — 0 또는 1이어야 한다")


def _validate_01_attribute(element: ET.Element, name: str, where: str) -> None:
    """값을 모델에 들이지 않더라도 0/1 철자는 지킨다 — 해석은 하지 않는다."""
    value = element.get(name)
    if value is not None and value not in ("0", "1"):
        raise UnsupportedProject(where, f"{name} 값이 무효다: {value!r} — 0 또는 1이어야 한다")


def _optional_time_attribute(
    element: ET.Element, name: str, where: str, *, default: Fraction
) -> Fraction:
    """선택 시각 속성 — 없으면 호출자가 정한 정확한 기본값을 쓴다."""
    text = element.get(name)
    if text is None:
        return default
    try:
        return timebase.parse_fcpx_time(text)
    except ValueError as error:
        raise UnsupportedProject(where, f"{name} 값이 무효다: {text!r} ({error})") from error


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
    format_id = _required_attribute(element, "format", where)
    start = _parse_time_attribute(element, "start", where)
    duration = _parse_time_attribute(element, "duration", where)
    has_audio = _required_bool_attribute(element, "hasAudio", where)
    _validate_01_attribute(element, "hasVideo", where)
    frame_duration = frame_durations.get(format_id)
    if frame_duration is None:
        raise UnsupportedProject(where, f"참조를 해석할 수 없다: format {format_id!r}")
    if frame_duration != sequence_frame_duration:
        raise UnsupportedProject(
            where,
            f"소스/프로젝트 프레임 레이트가 다르다: 애셋 {frame_duration}초 ≠"
            f" 시퀀스 {sequence_frame_duration}초 — 리샘플링하지 않고 거부한다",
        )
    media_reps = _parse_media_reps(element, paths)
    if not media_reps:
        raise UnsupportedProject(
            where,
            "애셋에 media-rep이 없다 — DTD 내용 모델(media-rep+)대로 최소 하나의"
            " 미디어 표현이 있어야 한다. 가리키는 파일의 준비는 P1-03의 게이트다",
        )
    return AssetResource(
        xml_id=xml_id,
        scoped_id=_scoped_resource_id(input_sha256, xml_id),
        uid=element.get("uid", ""),
        name=element.get("name", ""),
        format_id=format_id,
        frame_duration=frame_duration,
        start=start,
        duration=duration,
        has_audio=has_audio,
        media_reps=media_reps,
    )


def _parse_media_reps(element: ET.Element, paths: dict[int, str]) -> tuple[MediaRep, ...]:
    reps: list[MediaRep] = []
    for child in element:
        where = paths[id(child)]
        if child.tag not in ("media-rep", "metadata"):
            raise UnsupportedProject(where, f"지원하지 않는 asset 자식 요소다: {child.tag}")
        if child.tag == "metadata":
            continue
        xml_kind = child.get("kind", "original-media")
        kind = _MEDIA_REP_XML_KINDS.get(xml_kind)
        if kind is None:
            raise UnsupportedProject(
                where,
                f"media-rep kind가 무효다: {xml_kind!r} — original-media 또는 proxy-media여야 한다",
            )
        src = _required_attribute(child, "src", where)
        reps.append(MediaRep(kind=kind, src=src))
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


def _require_format_override(
    element: ET.Element,
    frame_durations: dict[str, Fraction],
    sequence_frame_duration: Fraction,
    where: str,
) -> None:
    """선택 format 오버라이(spine·asset-clip)는 시퀀스 격자와 같아야 한다.

    DTD 주석 그대로 'default is same as parent'다 — 같지 않으면 리샘플링하지
    않고 거부한다.
    """
    override = element.get("format")
    if override is None:
        return
    override_duration = frame_durations.get(override)
    if override_duration is None:
        raise UnsupportedProject(where, f"참조를 해석할 수 없다: format {override!r}")
    if override_duration != sequence_frame_duration:
        raise UnsupportedProject(
            where,
            f"프레임 레이트 오버라이드가 시퀀스 격자와 다르다: format {override!r}"
            f" {override_duration}초 ≠ 시퀀스 {sequence_frame_duration}초 —"
            " 리샘플링하지 않고 거부한다",
        )


def _require_primary_spine(
    spine: ET.Element,
    frame_durations: dict[str, Fraction],
    frame_duration: Fraction,
    paths: dict[int, str],
) -> None:
    """주요 스토리라인의 좌표 — offset·lane은 없거나 정확히 0이어야 한다.

    spine에 붙는 offset·lane은 '연결된 스토리라인'의 좌표다. 이 부분집합은
    평평한 주요 스토리라인만 받으므로 0 외의 값은 좌표 오버라이로 거부한다.
    """
    where = paths[id(spine)]
    offset = _optional_time_attribute(spine, "offset", where, default=Fraction(0))
    if offset != 0:
        raise UnsupportedProject(
            where,
            f"spine offset 오버라이드는 지원하지 않는다 — 주요 스토리라인은 0s여야 한다:"
            f" {offset}초",
        )
    lane = spine.get("lane")
    if lane is not None and lane != "0":
        raise UnsupportedProject(
            where,
            f"spine lane 오버라이드는 지원하지 않는다 — 주요 스토리라인은 lane 0이다: {lane!r}",
        )
    _require_format_override(spine, frame_durations, frame_duration, where)


def _parse_storyline(
    spine: ET.Element,
    paths: dict[int, str],
    assets_by_id: dict[str, AssetResource],
    frame_durations: dict[str, Fraction],
    frame_duration: Fraction,
    tc_start: Fraction,
    sequence_duration: Fraction,
    input_sha256: str,
) -> tuple[TimelineItem, ...]:
    _require_primary_spine(spine, frame_durations, frame_duration, paths)
    sequence_end = tc_start + sequence_duration
    items: list[TimelineItem] = []
    previous_end: Fraction | None = None
    for child in spine:
        where = paths[id(child)]
        if child.tag == "asset-clip":
            item: TimelineItem = _parse_asset_clip(
                child, paths, assets_by_id, frame_durations, frame_duration, tc_start, input_sha256
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
    frame_durations: dict[str, Fraction],
    frame_duration: Fraction,
    tc_start: Fraction,
    input_sha256: str,
) -> ClipInstance:
    where = paths[id(element)]
    _require_primary_clip_lane(element, where)
    for attribute, reason in _AUDIO_TIMING_OVERRIDE_REASONS.items():
        _reject_forbidden_attribute(element, attribute, where, reason)
    _reject_clip_children(element, paths)
    ref = _required_attribute(element, "ref", where)
    asset = assets_by_id.get(ref)
    if asset is None:
        raise UnsupportedProject(where, f"참조를 해석할 수 없다: asset {ref!r}")
    _require_format_override(element, frame_durations, frame_duration, where)
    offset = _optional_time_attribute(element, "offset", where, default=Fraction(0))
    start = _optional_time_attribute(element, "start", where, default=Fraction(0))
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


def _require_primary_clip_lane(element: ET.Element, where: str) -> None:
    """`lane="0"`은 '부모 안'이라는 뜻이다 — 0 외의 lane은 연결 클립이다."""
    lane = element.get("lane")
    if lane is not None and lane != "0":
        raise UnsupportedProject(where, f"연결 클립(lane≠0)은 지원하지 않는다: lane={lane!r}")


def _parse_gap(
    element: ET.Element,
    paths: dict[int, str],
    frame_duration: Fraction,
    tc_start: Fraction,
    input_sha256: str,
) -> GapInstance:
    where = paths[id(element)]
    _reject_forbidden_attribute(
        element, "lane", where, "gap에는 lane 속성이 없다 — gap은 앵커될 수 없다"
    )
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


# ─── P1-05 왕복 — 신원 보존 복사와 그 증명 ────────────────────────────────

# 어긋남 진단의 상한 — P1-04 `xml_validation`과 같은 규율이다. 커다란 문서 하나가
# 로그를 채우지 못하게 한다. 임계값이 아니라 진단 위생의 상수다.
_MAX_MISMATCH_LINES = 8
_MAX_MISMATCH_LINE_CHARS = 400
_TRUNCATION_MARK = "…"

_ASSET_FIELDS = (
    "xml_id",
    "uid",
    "name",
    "format_id",
    "frame_duration",
    "start",
    "duration",
    "has_audio",
    "media_reps",
)
"""애셋의 정적 사실 전부 — `scoped_id`만 빼고 문서 간 비교 대상이다.

`scoped_id`·`instance_id`는 입력 해시가 스코프라 문서마다 값이 다른 것이 정상이다
(같은 바이트라면 우연히 같아질 뿐). 참조는 각 문서 안에서 `resource_scoped_id`가
가리키는 **자리**로 비교한다.
"""

_CLIP_FIELDS = ("offset", "start", "duration", "enabled", "src_enable")
_GAP_FIELDS = ("offset", "duration")


def write_identity(document: ProjectDocument, destination: Path) -> Path:
    """문서의 원본 바이트를 목적지에 그대로 쓴다 — 정규화는 일절 없다.

    같은 디렉터리 임시파일에 쓴 뒤 `os.replace`로 통째로 바꾼다. 그래서 목적지는
    언제나 온전한 옛 내용이거나 온전한 새 내용뿐이다. 쓰기나 교체가 실패하면
    임시파일을 치우고 예외를 그대로 흘린다 — 원본 문서와 `raw_xml`은 건드리지
    않는다. 이것은 신원 복사이지 직렬화가 아니다(P6의 소관).
    """
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(document.raw_xml)
        os.replace(temporary, destination)
    except BaseException:
        if temporary is not None:  # 생성 자체가 실패하면 치울 임시파일도 없다
            temporary.unlink(missing_ok=True)
        raise
    return destination


def validate_roundtrip(
    source: ProjectDocument, written: Path, *, dtd_path: Path | None = None
) -> xml_validation.XmlValidation:
    """쓴 사본이 원본과 같은 문서인지 증명한다 — 판정은 기록으로, 부작용은 없다.

    순서가 계약이다. 먼저 P1-04 xmllint 경계가 사본을 본다 — 도구나 요청한 DTD가
    없으면 `XmlValidationUnavailable`이 그대로 샌다(판정을 내린 적이 없다). 판정이
    나왔다면 P1-02 파서로 사본을 다시 읽어 원본과 비교한다: 의미가 전부 같아도
    바이트가 다르면 어긋남이고, 정규화로 덮지 않는다. 무엇이 어긋나든 합성 통과
    대신 `valid=False`와 상한 있는 진단으로 돌아온다 — `dtd_checked`는 실제로 DTD를
    봤는지 그대로 말한다.
    """
    validation = xml_validation.validate_xml(written, dtd_path=dtd_path)
    if not validation.valid:
        return validation
    try:
        reparsed = parse_project(written)
    except UnsupportedProject as error:
        return _roundtrip_failure(
            validation.dtd_checked,
            [f"재구문 해석 실패 — {error.location}: {error.reason}"],
        )
    mismatches = _semantic_mismatches(source, reparsed)
    written_bytes = written.read_bytes()
    if not mismatches and written_bytes != source.raw_xml:
        mismatches = [_first_byte_mismatch(source.raw_xml, written_bytes)]
    if mismatches:
        return _roundtrip_failure(validation.dtd_checked, mismatches)
    return validation


def _roundtrip_failure(dtd_checked: bool, mismatches: list[str]) -> xml_validation.XmlValidation:
    """왕복 어긋남의 기록 — 상한 있는 진단만, XML 덤프는 없다."""
    return xml_validation.XmlValidation(
        valid=False,
        diagnostics=_bounded_lines(mismatches),
        dtd_checked=dtd_checked,
    )


def _semantic_mismatches(source: ProjectDocument, reparsed: ProjectDocument) -> list[str]:
    """해시 스코프 필드를 뺀 의미 비교 — 어긋난 자리를 정한 순서로 말한다."""
    mismatches: list[str] = []
    for field in ("version", "sequence_tc_start", "frame_duration"):
        left, right = getattr(source, field), getattr(reparsed, field)
        if left != right:
            mismatches.append(_field_mismatch(field, left, right))
    mismatches += _resource_mismatches(source, reparsed)
    mismatches += _storyline_mismatches(source, reparsed)
    return mismatches


def _resource_mismatches(source: ProjectDocument, reparsed: ProjectDocument) -> list[str]:
    if len(source.resources) != len(reparsed.resources):
        return [_field_mismatch("resources 길이", len(source.resources), len(reparsed.resources))]
    mismatches: list[str] = []
    for index, (left, right) in enumerate(zip(source.resources, reparsed.resources, strict=True)):
        for field in _ASSET_FIELDS:
            left_value, right_value = getattr(left, field), getattr(right, field)
            if left_value != right_value:
                mismatches.append(
                    _field_mismatch(f"resources[{index}].{field}", left_value, right_value)
                )
    return mismatches


def _storyline_mismatches(source: ProjectDocument, reparsed: ProjectDocument) -> list[str]:
    if len(source.storyline) != len(reparsed.storyline):
        return [_field_mismatch("storyline 길이", len(source.storyline), len(reparsed.storyline))]
    mismatches: list[str] = []
    for index, (left, right) in enumerate(zip(source.storyline, reparsed.storyline, strict=True)):
        where = f"storyline[{index}]"
        if type(left) is not type(right):
            mismatches.append(
                _field_mismatch(f"{where} 항목 종류", type(left).__name__, type(right).__name__)
            )
            continue
        fields = (
            ("element_path", *_CLIP_FIELDS)
            if isinstance(left, ClipInstance)
            else ("element_path", *_GAP_FIELDS)
        )
        for field in fields:
            left_value, right_value = getattr(left, field), getattr(right, field)
            if left_value != right_value:
                mismatches.append(_field_mismatch(f"{where}.{field}", left_value, right_value))
        if isinstance(left, ClipInstance) and isinstance(right, ClipInstance):
            left_ref, right_ref = _resource_ref(source, left), _resource_ref(reparsed, right)
            if left_ref != right_ref:
                mismatches.append(_field_mismatch(f"{where}.ref", left_ref, right_ref))
    return mismatches


def _resource_ref(document: ProjectDocument, clip: ClipInstance) -> str:
    """클립이 가리키는 애셋의 문서 안 자리 — 스코프 id 대신 비교하는 참조의 뜻."""
    for index, asset in enumerate(document.resources):
        if asset.scoped_id == clip.resource_scoped_id:
            return f"resources[{index}]({asset.xml_id})"
    return "참조를 잃은 애셋"


def _field_mismatch(name: str, left: object, right: object) -> str:
    return f"{name}: 원본 {_describe(left)} ≠ 사본 {_describe(right)}"


def _describe(value: object) -> str:
    """진단용 한눈 표기 — 시각은 FCPXML 정준형으로, 나머지는 그 값 그대로."""
    if isinstance(value, Fraction):
        return timebase.format_fcpx_time(value)
    if isinstance(value, MediaRep):
        return f"{value.kind}:{value.src}"
    if isinstance(value, tuple):
        return "{" + ", ".join(_describe(item) for item in value) + "}"
    return str(value)


def _first_byte_mismatch(source: bytes, written: bytes) -> str:
    """바이트 어긋남 한 줄 — 길이와 첫 차이 지점만 말한다(내용은 토하지 않는다)."""
    limit = min(len(source), len(written))
    offset = next(
        (index for index in range(limit) if source[index] != written[index]),
        limit,
    )
    return (
        f"바이트가 다르다: 원본 {len(source)}바이트 ≠ 사본 {len(written)}바이트,"
        f" 첫 차이 {offset}번째 바이트"
    )


def _bounded_lines(lines: list[str]) -> tuple[str, ...]:
    """어긋남 몇 줄로 — 상한을 넘으면 표지를 남긴다(P1-04와 같은 규율)."""
    if len(lines) <= _MAX_MISMATCH_LINES:
        return tuple(_bounded_line(line) for line in lines)
    kept = [_bounded_line(line) for line in lines[: _MAX_MISMATCH_LINES - 1]]
    kept.append(f"{_TRUNCATION_MARK} 이후 {len(lines) - len(kept)}줄이 잘렸다")
    return tuple(kept)


def _bounded_line(line: str) -> str:
    """한 줄의 상한 — 잘리면 표지를 붙인다(길이는 상한 ＋ 표지 1글자)."""
    if len(line) <= _MAX_MISMATCH_LINE_CHARS:
        return line
    return line[:_MAX_MISMATCH_LINE_CHARS] + _TRUNCATION_MARK
