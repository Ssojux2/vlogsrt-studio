"""FCPXML 1.14 지정 부분집합 파서의 계약 — P1-02가 여기서 얼어 붙는다.

계획("Input and output" ＋ "Source analysis, clocks, identity")의 정본: 평평한
주요 스토리라인(asset-clip/gap)만 받고, 그 밖의 구성은 전부 **위치＋이유**와 함께
결정적으로 거부한다. 조용히 눌러 펴지는(flaten) 일이 없어야 한다. 시각은 전부
`Fraction`이고 격자 검사는 P1-01 timebase의 정의를 그대로 쓴다.

모든 XML은 인라인 합성이다 — 픽스처·골든은 만지지 않는다(실행 규율).
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.fcpxml import parse_project, resolve_info_path
from vlogsrt.project.models import (
    AssetResource,
    ClipInstance,
    GapInstance,
    MediaRep,
    ProjectDocument,
    UnsupportedProject,
)
from vlogsrt.project.timebase import (
    SUPPORTED_FRAME_DURATIONS,
    format_fcpx_time,
    parse_fcpx_time,
)

# ─── 합성 XML 빌더 ────────────────────────────────────────────────────────

FORMAT_30 = (
    '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>'
)

ASSET_DEFAULT = (
    '<asset id="r2" name="source.mp4" uid="AAAA1111" start="0s" duration="600s"'
    ' hasAudio="1" hasVideo="1" format="r1">\n'
    '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
    '    <media-rep kind="proxy-media" src="file:///media/source_proxy.mov"/>\n'
    "  </asset>"
)

SPINE_DEFAULT = (
    '<asset-clip ref="r2" name="클립1" offset="0s" start="10s" duration="100s"'
    ' tcFormat="NDF"/>\n'
    '    <gap name="갭" offset="100s" start="0s" duration="10s"/>\n'
    '    <asset-clip ref="r2" name="클립2" offset="110s" start="200s"'
    ' duration="90s" enabled="0" srcEnable="video"/>'
)


def build_fcpxml(
    *,
    version: str = "1.14",
    resources: str = f"{FORMAT_30}\n  {ASSET_DEFAULT}",
    sequence_attrs: str = 'format="r1" tcStart="0s" duration="600s"',
    spine_items: str = SPINE_DEFAULT,
    spine_attrs: str = "",
    library_body: str | None = None,
) -> str:
    """지정 부분집합의 기본형 — 실험마다 한 조각씩 갈아끼운다."""
    if library_body is None:
        library_body = (
            '<event name="이벤트">\n'
            '      <project name="프로젝트">\n'
            f"        <sequence {sequence_attrs}>\n"
            f"          <spine{spine_attrs}>\n"
            f"            {spine_items}\n"
            "          </spine>\n"
            "        </sequence>\n"
            "      </project>\n"
            "    </event>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        f'<fcpxml version="{version}">\n'
        f"  <resources>\n    {resources}\n  </resources>\n"
        "  <library>\n"
        f"    {library_body}\n"
        "  </library>\n"
        "</fcpxml>\n"
    )


def write_project(tmp_path: Path, body: str, stem: str = "project") -> Path:
    path = tmp_path / f"{stem}.fcpxml"
    path.write_text(body, encoding="utf-8")
    return path


def parse(body: str, tmp_path: Path, stem: str = "project") -> ProjectDocument:
    return parse_project(write_project(tmp_path, body, stem))


def expect_reject(body: str, tmp_path: Path, location: str, reason: str) -> None:
    """거부는 위치와 이유 둘 다 말해야 한다 — 하나라도 빠지면 조용히 새는 것이다."""
    with pytest.raises(UnsupportedProject) as excinfo:
        parse(body, tmp_path)
    assert excinfo.value.location == location, str(excinfo.value)
    assert reason in excinfo.value.reason, str(excinfo.value)


SPINE = "/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]/spine[1]"


def _scoped(sha: str, xml_id: str) -> str:
    return hashlib.sha256(f"{sha}:resource:{xml_id}".encode()).hexdigest()


def _instance(sha: str, element_path: str) -> str:
    return hashlib.sha256(f"{sha}:instance:{element_path}".encode()).hexdigest()


# ─── 평평한 지정 형태가 그대로 산다 ───────────────────────────────────────


def test_flat_supported_shape_parses(tmp_path: Path) -> None:
    document = parse(build_fcpxml(), tmp_path)
    assert document.version == "1.14"
    assert document.sequence_tc_start == Fraction(0)
    assert document.frame_duration == Fraction(1, 30)

    resource = document.resources[0]
    assert isinstance(resource, AssetResource)
    assert resource.xml_id == "r2"
    assert resource.uid == "AAAA1111"
    assert resource.name == "source.mp4"
    assert resource.format_id == "r1"
    assert resource.frame_duration == Fraction(1, 30)
    assert resource.start == Fraction(0)
    assert resource.duration == Fraction(600)
    assert resource.has_audio is True
    assert resource.media_reps == (
        MediaRep(kind="original", src="file:///media/source.mp4"),
        MediaRep(kind="proxy", src="file:///media/source_proxy.mov"),
    )

    first, gap, second = document.storyline
    assert isinstance(first, ClipInstance)
    assert first.element_path == f"{SPINE}/asset-clip[1]"
    assert first.offset == Fraction(0)
    assert first.start == Fraction(10)
    assert first.duration == Fraction(100)
    assert first.enabled is True
    assert first.src_enable == "all"
    assert isinstance(gap, GapInstance)
    assert gap.element_path == f"{SPINE}/gap[1]"
    assert gap.offset == Fraction(100)
    assert gap.duration == Fraction(10)
    assert isinstance(second, ClipInstance)
    assert second.element_path == f"{SPINE}/asset-clip[2]"
    assert second.offset == Fraction(110)
    assert second.start == Fraction(200)
    assert second.duration == Fraction(90)
    assert second.enabled is False
    assert second.src_enable == "video"


def test_hash_and_raw_bytes_are_the_input_itself(tmp_path: Path) -> None:
    path = write_project(tmp_path, build_fcpxml())
    document = parse_project(path)
    raw = path.read_bytes()
    assert document.raw_xml == raw
    assert document.input_sha256 == hashlib.sha256(raw).hexdigest()
    assert document.input_path == path
    assert document.info_path == path


def test_id_formulas_are_hash_of_sha_scope_and_path(tmp_path: Path) -> None:
    """`scoped_id`·`instance_id`의 정의 그 자체 — 문서 해시가 스코프다."""
    document = parse(build_fcpxml(), tmp_path)
    resource = document.resources[0]
    assert resource.scoped_id == _scoped(document.input_sha256, "r2")
    first, _, second = document.storyline
    assert isinstance(first, ClipInstance) and isinstance(second, ClipInstance)
    assert first.resource_scoped_id == resource.scoped_id
    assert second.resource_scoped_id == resource.scoped_id
    assert first.instance_id == _instance(document.input_sha256, first.element_path)
    assert second.instance_id == _instance(document.input_sha256, second.element_path)


def test_parsing_is_deterministic(tmp_path: Path) -> None:
    body = build_fcpxml()
    assert parse(body, tmp_path) == parse(body, tmp_path)


def test_resources_keep_document_order(tmp_path: Path) -> None:
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r9" name="a.mp4" start="0s" duration="60s" hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/a.mp4"/>\n'
        "  </asset>\n"
        "  " + ASSET_DEFAULT
    )
    document = parse(build_fcpxml(resources=resources), tmp_path)
    assert [asset.xml_id for asset in document.resources] == ["r9", "r2"]
    assert document.resources[0].uid == ""
    assert document.resources[0].has_audio is True


def test_has_audio_zero_is_preserved(tmp_path: Path) -> None:
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="silent.mp4" start="0s" duration="600s" hasAudio="0"'
        ' format="r1"/>\n'
        "  " + ASSET_DEFAULT.replace('id="r2"', 'id="r3"')
    )
    document = parse(build_fcpxml(resources=resources), tmp_path)
    assert [asset.has_audio for asset in document.resources] == [False, True]


# ─── 번들과 경로 해석 ─────────────────────────────────────────────────────


def test_resolve_info_path_passes_plain_file_through(tmp_path: Path) -> None:
    path = tmp_path / "plain.fcpxml"
    path.write_text(build_fcpxml(), encoding="utf-8")
    assert resolve_info_path(path) == path


def test_resolve_info_path_maps_bundle_to_info_fcpxml(tmp_path: Path) -> None:
    bundle = tmp_path / "project.fcpxmld"
    bundle.mkdir()
    assert resolve_info_path(bundle) == bundle / "Info.fcpxml"


def test_resolve_info_path_rejects_other_extensions(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedProject) as excinfo:
        resolve_info_path(tmp_path / "project.xml")
    assert str(tmp_path / "project.xml") in excinfo.value.location
    assert ".fcpxml" in excinfo.value.reason


def test_bundle_parses_with_info_path_inside(tmp_path: Path) -> None:
    bundle = tmp_path / "project.fcpxmld"
    bundle.mkdir()
    (bundle / "Info.fcpxml").write_text(build_fcpxml(), encoding="utf-8")
    document = parse_project(bundle)
    assert document.input_path == bundle
    assert document.info_path == bundle / "Info.fcpxml"
    assert document.version == "1.14"
    direct = parse_project(write_project(tmp_path, build_fcpxml(), "same"))
    assert document.storyline == direct.storyline
    assert document.resources == direct.resources


def test_missing_bundle_info_is_named(tmp_path: Path) -> None:
    bundle = tmp_path / "empty.fcpxmld"
    bundle.mkdir()
    with pytest.raises(UnsupportedProject) as excinfo:
        parse_project(bundle)
    assert str(bundle / "Info.fcpxml") in excinfo.value.reason


def test_missing_plain_file_is_named(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.fcpxml"
    with pytest.raises(UnsupportedProject) as excinfo:
        parse_project(missing)
    assert str(missing) in excinfo.value.reason


# ─── 반복·비활성 인스턴스, 위치 경로 ──────────────────────────────────────


def test_repeated_identical_siblings_get_distinct_paths_and_ids(tmp_path: Path) -> None:
    """형제가 같은 모양이어도 위치 경로는 다르다 — 인스턴스는 다르다.

    시각은 겹칠 수 없으니 offset만 차례로 놓고 나머지 속성은 글자 그대로 같다.
    """
    twin = (
        '<asset-clip ref="r2" name="같은클립" offset="0s" start="10s" duration="5s"/>\n'
        '    <asset-clip ref="r2" name="같은클립" offset="5s" start="10s" duration="5s"/>\n'
        '    <asset-clip ref="r2" name="같은클립" offset="10s" start="10s" duration="5s"/>'
    )
    document = parse(build_fcpxml(spine_items=twin), tmp_path)
    paths = [clip.element_path for clip in document.storyline]
    ids = [clip.instance_id for clip in document.storyline]
    assert paths == [
        f"{SPINE}/asset-clip[1]",
        f"{SPINE}/asset-clip[2]",
        f"{SPINE}/asset-clip[3]",
    ]
    assert len(set(ids)) == 3
    assert all(
        clip.resource_scoped_id == document.resources[0].scoped_id for clip in document.storyline
    )


def test_disabled_clip_is_kept_not_dropped(tmp_path: Path) -> None:
    items = (
        '<asset-clip ref="r2" name="켜짐" offset="0s" start="10s" duration="5s"/>\n'
        '    <asset-clip ref="r2" name="꺼짐" offset="5s" start="10s" duration="5s"'
        ' enabled="0"/>'
    )
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    assert [clip.enabled for clip in document.storyline] == [True, False]


def test_gap_between_clips_is_a_timeline_item(tmp_path: Path) -> None:
    items = (
        '<asset-clip ref="r2" name="앞" offset="0s" start="10s" duration="5s"/>\n'
        '    <gap name="갭" offset="5s" start="0s" duration="7s"/>\n'
        '    <asset-clip ref="r2" name="뒤" offset="12s" start="10s" duration="5s"/>'
    )
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    assert [type(item).__name__ for item in document.storyline] == [
        "ClipInstance",
        "GapInstance",
        "ClipInstance",
    ]
    gap = document.storyline[1]
    assert isinstance(gap, GapInstance)
    assert gap.instance_id == _instance(document.input_sha256, f"{SPINE}/gap[1]")
    assert not hasattr(gap, "resource_scoped_id")


# ─── 0이 아닌 에포크 ─────────────────────────────────────────────────────


def test_nonzero_epochs_parse_exactly(tmp_path: Path) -> None:
    """`tcStart`·`asset.start`가 0이 아니어도 격자는 각 앵커에 묶인다."""
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" uid="AAAA1111" start="3600s" duration="600s"'
        ' hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    sequence_attrs = 'format="r1" tcStart="3600s" duration="600s"'
    items = (
        '<asset-clip ref="r2" name="클립" offset="3600s" start="3600s" duration="100s"/>\n'
        '    <asset-clip ref="r2" name="클립2" offset="3700s" start="3650s" duration="5s"'
        ' srcEnable="video"/>'
    )
    document = parse(
        build_fcpxml(resources=resources, sequence_attrs=sequence_attrs, spine_items=items),
        tmp_path,
    )
    assert document.sequence_tc_start == Fraction(3600)
    resource = document.resources[0]
    assert resource.start == Fraction(3600)
    first = document.storyline[0]
    assert isinstance(first, ClipInstance)
    assert first.offset == Fraction(3600)
    assert first.start == Fraction(3600)


def test_negative_epoch_anchor_still_grid_aligned(tmp_path: Path) -> None:
    sequence_attrs = 'format="r1" tcStart="-3600s" duration="600s"'
    items = '<asset-clip ref="r2" name="클립" offset="-3600s" start="0s" duration="100s"/>'
    document = parse(build_fcpxml(sequence_attrs=sequence_attrs, spine_items=items), tmp_path)
    assert document.sequence_tc_start == Fraction(-3600)
    first = document.storyline[0]
    assert isinstance(first, ClipInstance)
    assert first.offset == Fraction(-3600)


# ─── 지원 프레임 레이트 전부 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("frame_duration_text", "expected"),
    [
        ("1/24s", Fraction(1, 24)),
        ("1/25s", Fraction(1, 25)),
        ("1/30s", Fraction(1, 30)),
        ("1/50s", Fraction(1, 50)),
        ("1/60s", Fraction(1, 60)),
        ("1001/24000s", Fraction(1001, 24000)),
        ("1001/30000s", Fraction(1001, 30000)),
        ("1001/60000s", Fraction(1001, 60000)),
    ],
)
def test_every_supported_rate_parses(
    tmp_path: Path, frame_duration_text: str, expected: Fraction
) -> None:
    """여덟 레이트 전부 — 1001계(23.976·29.97·59.94)도 예외가 아니다.

    정수 초는 1001계 격자 위에 있지 않으므로 모든 시각을 프레임 수에서
    정준 형태로 쓴다(예: 3000프레임 × 1001/30000s = 1001/10s).
    """
    assert expected in SUPPORTED_FRAME_DURATIONS
    span = format_fcpx_time(9000 * expected)
    first = format_fcpx_time(3000 * expected)
    fmt = (
        f'<format id="r1" name="F" frameDuration="{frame_duration_text}"'
        ' width="1920" height="1080"/>'
    )
    asset = (
        f'<asset id="r2" name="source.mp4" uid="AAAA1111" start="0s" duration="{span}"'
        ' hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    items = (
        f'<asset-clip ref="r2" name="클립" offset="0s" start="0s" duration="{first}"/>\n'
        f'    <asset-clip ref="r2" name="클립2" offset="{first}" start="{first}"'
        f' duration="{first}"/>'
    )
    document = parse(
        build_fcpxml(
            resources=f"{fmt}\n  {asset}",
            sequence_attrs=f'format="r1" tcStart="0s" duration="{span}"',
            spine_items=items,
        ),
        tmp_path,
    )
    assert document.frame_duration == expected
    assert document.resources[0].frame_duration == expected
    assert parse_fcpx_time(frame_duration_text) == document.frame_duration


def test_noncanonical_but_equivalent_frame_duration_parses(tmp_path: Path) -> None:
    """`2/60s`는 1/30으로 정규화되어 통과한다 — 원본 문자열은 raw 그대로다."""
    fmt = '<format id="r1" name="F" frameDuration="2/60s" width="1920" height="1080"/>'
    body = build_fcpxml(resources=f"{fmt}\n  {ASSET_DEFAULT}")
    document = parse(body, tmp_path)
    assert document.frame_duration == Fraction(1, 30)
    assert b'frameDuration="2/60s"' in document.raw_xml


# ─── 거부 — 버전과 문서 구조 ──────────────────────────────────────────────


@pytest.mark.parametrize("version", ["1.9", "1.13", "1.15", "2.0"])
def test_version_other_than_114_is_named(tmp_path: Path, version: str) -> None:
    expect_reject(
        build_fcpxml(version=version),
        tmp_path,
        location="/fcpxml[1]",
        reason=f"{version}",
    )


def test_version_attribute_missing_is_rejected(tmp_path: Path) -> None:
    body = build_fcpxml().replace('<fcpxml version="1.14">', "<fcpxml>")
    expect_reject(body, tmp_path, location="/fcpxml[1]", reason="version")


def test_non_fcpxml_root_is_rejected(tmp_path: Path) -> None:
    path = write_project(tmp_path, "<root><library/></root>\n")
    with pytest.raises(UnsupportedProject) as excinfo:
        parse_project(path)
    assert str(path) == excinfo.value.location
    assert "fcpxml" in excinfo.value.reason


def test_malformed_xml_names_the_path(tmp_path: Path) -> None:
    path = write_project(tmp_path, "<fcpxml version='1.14'><library>\n")
    with pytest.raises(UnsupportedProject) as excinfo:
        parse_project(path)
    assert str(path) == excinfo.value.location
    assert "XML" in excinfo.value.reason


def test_entity_declarations_are_rejected_before_parsing(tmp_path: Path) -> None:
    """지정 부분집합에는 엔티티가 없다 — billion laughs도 XXE도 원천에서 끊는다."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml [<!ENTITY laugh "ha">]>\n'
        '<fcpxml version="1.14">&laugh;</fcpxml>\n'
    )
    path = write_project(tmp_path, body)
    with pytest.raises(UnsupportedProject) as excinfo:
        parse_project(path)
    assert str(path) == excinfo.value.location
    assert "엔티티" in excinfo.value.reason


def test_empty_project_library_is_rejected(tmp_path: Path) -> None:
    body = build_fcpxml(library_body="")
    expect_reject(body, tmp_path, location="/fcpxml[1]/library[1]", reason="event")


def test_two_events_are_rejected(tmp_path: Path) -> None:
    library_body = (
        '<event name="a"><project name="p"><sequence format="r1" tcStart="0s"'
        ' duration="600s"><spine/></sequence></project></event>\n'
        '    <event name="b"><project name="q"><sequence format="r1" tcStart="0s"'
        ' duration="600s"><spine/></sequence></project></event>'
    )
    expect_reject(
        build_fcpxml(library_body=library_body),
        tmp_path,
        location="/fcpxml[1]/library[1]",
        reason="event",
    )


def test_two_projects_are_rejected(tmp_path: Path) -> None:
    sequence = '<sequence format="r1" tcStart="0s" duration="600s"><spine/></sequence>'
    library_body = (
        f'<event name="a"><project name="p">{sequence}</project>\n'
        f'      <project name="q">{sequence}</project></event>'
    )
    expect_reject(
        build_fcpxml(library_body=library_body),
        tmp_path,
        location="/fcpxml[1]/library[1]/event[1]",
        reason="project",
    )


def test_sequence_and_spine_are_required(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(library_body='<event name="a"><project name="p"><spine/></project></event>'),
        tmp_path,
        location="/fcpxml[1]/library[1]/event[1]/project[1]",
        reason="sequence",
    )
    expect_reject(
        build_fcpxml(
            library_body=(
                '<event name="a"><project name="p"><sequence format="r1"'
                ' tcStart="0s" duration="600s"/></project></event>'
            )
        ),
        tmp_path,
        location="/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]",
        reason="spine",
    )


# ─── 거부 — 리소스 참조와 레이트 ──────────────────────────────────────────


def test_missing_sequence_format_ref_is_rejected(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(sequence_attrs='format="rX" tcStart="0s" duration="600s"'),
        tmp_path,
        location="/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]",
        reason="rX",
    )


def test_missing_asset_format_ref_is_rejected(tmp_path: Path) -> None:
    asset = ASSET_DEFAULT.replace('format="r1"', 'format="r9"')
    expect_reject(
        build_fcpxml(resources=f"{FORMAT_30}\n  {asset}"),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]",
        reason="r9",
    )


def test_unresolved_clip_ref_is_rejected(tmp_path: Path) -> None:
    items = '<asset-clip ref="rX" name="고아" offset="0s" start="10s" duration="5s"/>'
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="rX",
    )


def test_duplicate_resource_ids_are_rejected(tmp_path: Path) -> None:
    resources = f"{FORMAT_30}\n  {ASSET_DEFAULT}\n  {ASSET_DEFAULT}"
    expect_reject(
        build_fcpxml(resources=resources),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[2]",
        reason="r2",
    )


def test_unsupported_format_rate_is_rejected(tmp_path: Path) -> None:
    fmt = '<format id="r1" name="F" frameDuration="1/29s" width="1920" height="1080"/>'
    expect_reject(
        build_fcpxml(resources=f"{fmt}\n  {ASSET_DEFAULT}"),
        tmp_path,
        location="/fcpxml[1]/resources[1]/format[1]",
        reason="1/29",
    )


def test_30_vs_2997_mismatch_is_rejected_not_resampled(tmp_path: Path) -> None:
    """프로젝트 30fps와 소스 29.97fps는 같은 격자가 아니다 — 리샘플링하지 않는다."""
    ntsc = (
        '<format id="rNTSC" name="FFVideoFormat1080p2997"'
        ' frameDuration="1001/30000s" width="1920" height="1080"/>'
    )
    asset = ASSET_DEFAULT.replace('format="r1"', 'format="rNTSC"')
    expect_reject(
        build_fcpxml(resources=f"{FORMAT_30}\n  {ntsc}\n  {asset}"),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]",
        reason="1001/30000",
    )


def test_same_rate_under_two_format_ids_is_accepted(tmp_path: Path) -> None:
    """레이트가 같으면 format id가 달라도 정상 속도 매칭이다."""
    twin = (
        '<format id="rA" name="A" frameDuration="1/30s" width="1920" height="1080"/>\n'
        '    <format id="rB" name="B" frameDuration="1/30s" width="1920" height="1080"/>'
    )
    asset = ASSET_DEFAULT.replace('format="r1"', 'format="rB"')
    document = parse(
        build_fcpxml(
            resources=f"{twin}\n  {asset}",
            sequence_attrs='format="rA" tcStart="0s" duration="600s"',
        ),
        tmp_path,
    )
    assert document.frame_duration == Fraction(1, 30)
    assert document.resources[0].format_id == "rB"


# ─── 거부 — 격자와 구간 ──────────────────────────────────────────────────


def ntsc_document(spine_items: str) -> str:
    """29.97 문서 — 1001계 격자에서 정수 초는 프레임 위가 아니다.

    1001의 배수 초는 정확히 격자 위다(1001s = 30000프레임). 애셋·시퀀스 길이는
    4004s(120000프레임)로 둔다.
    """
    fmt = (
        '<format id="r1" name="FFVideoFormat1080p2997" frameDuration="1001/30000s"'
        ' width="1920" height="1080"/>'
    )
    asset = (
        '<asset id="r2" name="source.mp4" uid="AAAA1111" start="0s" duration="4004s"'
        ' hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    return build_fcpxml(
        resources=f"{fmt}\n  {asset}",
        sequence_attrs='format="r1" tcStart="0s" duration="4004s"',
        spine_items=spine_items,
    )


def test_sequence_duration_off_grid_is_rejected(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(sequence_attrs='format="r1" tcStart="0s" duration="61/60s"'),
        tmp_path,
        location="/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]",
        reason="duration",
    )


def test_clip_offset_off_grid_is_rejected(tmp_path: Path) -> None:
    """1001계 격자에서 `2003s`는 프레임 위가 아니다 — 2002s는 60000프레임, 2003s는 아니다."""
    items = (
        '<asset-clip ref="r2" name="클립" offset="0s" start="1001s" duration="1001s"/>\n'
        '    <asset-clip ref="r2" name="클립2" offset="1001s" start="2002s"'
        ' duration="1001s"/>\n'
        '    <asset-clip ref="r2" name="어긋남" offset="2003s" start="1001s"'
        ' duration="1001s"/>'
    )
    expect_reject(
        ntsc_document(items),
        tmp_path,
        location=f"{SPINE}/asset-clip[3]",
        reason="offset",
    )


def test_clip_duration_off_grid_is_rejected(tmp_path: Path) -> None:
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="61/60s"/>'
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="duration",
    )


def test_clip_start_off_source_grid_is_rejected(tmp_path: Path) -> None:
    """소스 격자는 `asset.start`에 묶인다 — 10s＋½프레임은 프레임 위가 아니다."""
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="61/60s" duration="5s"/>'
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="start",
    )


def test_gap_offset_off_grid_is_rejected(tmp_path: Path) -> None:
    items = (
        '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"/>\n'
        '    <gap name="갭" offset="301/60s" duration="1s"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/gap[1]",
        reason="offset",
    )


def test_offset_off_epoch_grid_is_rejected(tmp_path: Path) -> None:
    """격자는 `tcStart`에 묶인다 — 에포크가 프레임 위가 아니면 오프셋도 어긋난다."""
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"/>'
    expect_reject(
        build_fcpxml(
            sequence_attrs='format="r1" tcStart="61/60s" duration="600s"',
            spine_items=items,
        ),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="offset",
    )


def test_zero_duration_clip_is_rejected(tmp_path: Path) -> None:
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="0s"/>'
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="duration",
    )


def test_source_bounds_beyond_asset_end_are_rejected(tmp_path: Path) -> None:
    """소스 범위는 애셋 안이어야 한다 — 600초 애셋에서 599초 지점＋2초는 밖이다."""
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="599s" duration="2s"/>'
    expect_reject(
        build_fcpxml(spine_items=items), tmp_path, location=f"{SPINE}/asset-clip[1]", reason="asset"
    )


def test_source_bounds_before_asset_start_are_rejected(tmp_path: Path) -> None:
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" uid="AAAA1111" start="3600s" duration="600s"'
        ' hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="3599s" duration="5s"/>'
    expect_reject(
        build_fcpxml(resources=resources, spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="asset",
    )


def test_clip_beyond_sequence_duration_is_rejected(tmp_path: Path) -> None:
    """애셋은 700초, 시퀀스는 600초 — 소스 안이어도 시퀀스 밖이면 거부한다."""
    asset = ASSET_DEFAULT.replace('duration="600s"', 'duration="700s"')
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="601s"/>'
    expect_reject(
        build_fcpxml(resources=f"{FORMAT_30}\n  {asset}", spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="sequence",
    )


def test_clip_before_tc_start_is_rejected(tmp_path: Path) -> None:
    items = '<asset-clip ref="r2" name="클립" offset="3599s" start="10s" duration="5s"/>'
    expect_reject(
        build_fcpxml(
            sequence_attrs='format="r1" tcStart="3600s" duration="600s"',
            spine_items=items,
        ),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="sequence",
    )


def test_overlapping_items_are_rejected(tmp_path: Path) -> None:
    items = (
        '<asset-clip ref="r2" name="앞" offset="0s" start="10s" duration="10s"/>\n'
        '    <asset-clip ref="r2" name="겹침" offset="5s" start="10s" duration="10s"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items), tmp_path, location=f"{SPINE}/asset-clip[2]", reason="겹침"
    )


def test_out_of_document_order_items_are_rejected(tmp_path: Path) -> None:
    """문서 순서와 시각 순서가 어긋나면 거부한다 — 비격침이라도 안 된다."""
    items = (
        '<asset-clip ref="r2" name="나중" offset="100s" start="10s" duration="10s"/>\n'
        '    <asset-clip ref="r2" name="먼저" offset="0s" start="10s" duration="10s"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items), tmp_path, location=f"{SPINE}/asset-clip[2]", reason="순서"
    )


def test_adjacent_touching_items_are_accepted(tmp_path: Path) -> None:
    """앞 항목의 끝 == 뒤 항목의 시작 — 겹침이 아니라 이어짐이다."""
    items = (
        '<asset-clip ref="r2" name="앞" offset="0s" start="10s" duration="10s"/>\n'
        '    <asset-clip ref="r2" name="뒤" offset="10s" start="20s" duration="10s"/>'
    )
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    assert len(document.storyline) == 2


# ─── 거부 — 지정 밖 구성 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tag", "reason_token"),
    [
        ("transition", "transition"),
        ("title", "title"),
        ("caption", "caption"),
        ("video", "video"),
        ("audio", "audio"),
        ("ref-clip", "ref-clip"),
        ("sync-clip", "sync-clip"),
        ("spine", "spine"),
    ],
)
def test_unsupported_spine_children_are_rejected(
    tmp_path: Path, tag: str, reason_token: str
) -> None:
    items = (
        '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"/>\n'
        f'    <{tag} ref="r2" name="지정밖" offset="5s" start="0s" duration="5s"/>\n'
        '    <asset-clip ref="r2" name="클립2" offset="10s" start="20s" duration="5s"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/{tag}[1]",
        reason=reason_token,
    )


def test_unknown_spine_element_is_rejected(tmp_path: Path) -> None:
    items = '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"/>'
    body = build_fcpxml(spine_items=items).replace(
        "</spine>", '  <mystery-clip name="수수께끼"/>\n          </spine>'
    )
    expect_reject(body, tmp_path, location=f"{SPINE}/mystery-clip[1]", reason="mystery-clip")


@pytest.mark.parametrize("tag", ["compound-clip", "multicam-clip", "effect", "generator"])
def test_unsupported_resource_elements_are_rejected(tmp_path: Path, tag: str) -> None:
    resources = f'{FORMAT_30}\n  <{tag} id="r7" name="지정밖"/>\n  {ASSET_DEFAULT}'
    expect_reject(
        build_fcpxml(resources=resources),
        tmp_path,
        location=f"/fcpxml[1]/resources[1]/{tag}[1]",
        reason=tag,
    )


@pytest.mark.parametrize(
    ("child", "reason_token"),
    [
        ("<retime/>", "retime"),
        ("<audio/>", "audio"),
        ("<video/>", "video"),
        ("<effect/>", "effect"),
    ],
)
def test_unsupported_clip_children_are_rejected(
    tmp_path: Path, child: str, reason_token: str
) -> None:
    child_tag = child.removeprefix("<").removesuffix("/>").removesuffix(">")
    items = (
        f'<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s">{child}'
        "</asset-clip>"
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]/{child_tag}[1]",
        reason=reason_token,
    )


@pytest.mark.parametrize(
    "child",
    [
        "<note>메모</note>",
        '<keyword value="여행"/>',
        '<marker start="1s" value="장면"/>',
        '<chapter-marker start="2s" value="1장"/>',
        '<rating value="favorite"/>',
        '<metadata><md key="com.apple.fcpxml" value="v"/></metadata>',
    ],
)
def test_benign_clip_metadata_children_are_tolerated(tmp_path: Path, child: str) -> None:
    """정적 메타데이터(note·marker류·metadata)는 시각도 신원도 바꾸지 않는다.

    DTD의 marker_item은 marker·chapter-marker·rating·keyword·analysis-marker·
    hidden-clip-marker고 뒤에 metadata?가 온다 — 그 철자 그대로만 받는다.
    """
    items = (
        f'<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s">{child}'
        "</asset-clip>"
    )
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    assert len(document.storyline) == 1


def test_gap_metadata_children_are_tolerated(tmp_path: Path) -> None:
    """gap도 같은 정적 메타데이터 규칙을 따른다 — DTD가 허락하는 자식이다."""
    items = (
        '<gap name="갭" offset="0s" duration="5s"><note>메모</note>'
        '<metadata><md key="k"/></metadata></gap>'
    )
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    assert len(document.storyline) == 1


def test_connected_clip_lane_is_rejected(tmp_path: Path) -> None:
    items = (
        '<asset-clip ref="r2" name="주요" offset="0s" start="10s" duration="5s"/>\n'
        '    <asset-clip ref="r2" name="연결" lane="1" offset="1s" start="10s"'
        ' duration="1s"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items), tmp_path, location=f"{SPINE}/asset-clip[2]", reason="lane"
    )


def test_lane_zero_on_asset_clip_is_primary_and_valid(tmp_path: Path) -> None:
    """`lane="0"`은 '부모 안'이라는 뜻이다 — 주요 스토리라인 항목으로 유효하다."""
    items = '<asset-clip ref="r2" name="주요" lane="0" offset="0s" start="10s" duration="5s"/>'
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    assert len(document.storyline) == 1


@pytest.mark.parametrize("lane", ["0", "1"])
def test_gap_lane_attribute_is_always_invalid(tmp_path: Path, lane: str) -> None:
    """gap은 앵커될 수 없다 — lane 0조차 gap의 DTD에 없는 속성이다."""
    items = f'<gap name="갭" lane="{lane}" offset="0s" duration="5s"/>'
    expect_reject(
        build_fcpxml(spine_items=items), tmp_path, location=f"{SPINE}/gap[1]", reason="lane"
    )


def test_audio_offset_override_is_rejected(tmp_path: Path) -> None:
    items = (
        '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"'
        ' audioOffset="10/30s"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="audioOffset",
    )


# ─── 거부 — srcEnable·enabled·속성 값 ────────────────────────────────────


def test_src_enable_audio_is_outside_the_subset(tmp_path: Path) -> None:
    """`srcEnable=\"audio\"`는 문법적으로 유효하지만 이 부분집합 밖이다."""
    items = (
        '<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s" srcEnable="audio"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="audio",
    )


@pytest.mark.parametrize("value", ["none", "both", "ALL"])
def test_src_enable_invalid_values_are_rejected(tmp_path: Path, value: str) -> None:
    """`none`은 asset-clip에 무효한 값이다 — '지원 밖'이 아니라 '무효'다."""
    items = (
        f'<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"'
        f' srcEnable="{value}"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason=value,
    )


@pytest.mark.parametrize("value", ["2", "true", ""])
def test_enabled_invalid_values_are_rejected(tmp_path: Path, value: str) -> None:
    items = (
        f'<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"'
        f' enabled="{value}"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="enabled",
    )


def test_has_audio_invalid_value_is_rejected(tmp_path: Path) -> None:
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" hasAudio="maybe"'
        ' format="r1"/>'
    )
    expect_reject(
        build_fcpxml(resources=resources),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]",
        reason="hasAudio",
    )


@pytest.mark.parametrize("kind", ["cloud", "original", "proxy"])
def test_media_rep_non_dtd_kinds_are_rejected(tmp_path: Path, kind: str) -> None:
    """DTD의 kind는 original-media/proxy-media뿐이다 — 낡은 철자도 무효다."""
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" hasAudio="1" format="r1">\n'
        f'    <media-rep kind="{kind}" src="file:///media/x"/>\n'
        "  </asset>"
    )
    expect_reject(
        build_fcpxml(resources=resources),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]/media-rep[1]",
        reason=kind,
    )


@pytest.mark.parametrize(
    ("items", "missing"),
    [
        ('<asset-clip ref="r2" name="클립" offset="0s" start="10s"/>', "duration"),
        ('<asset-clip name="고아" offset="0s" start="10s" duration="5s"/>', "ref"),
    ],
)
def test_clip_missing_required_attributes_are_rejected(
    tmp_path: Path, items: str, missing: str
) -> None:
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason=missing,
    )


@pytest.mark.parametrize(
    ("gap", "missing"),
    [('<gap name="갭" duration="5s"/>', "offset"), ('<gap name="갭" offset="0s"/>', "duration")],
)
def test_gap_missing_required_attributes_are_rejected(
    tmp_path: Path, gap: str, missing: str
) -> None:
    expect_reject(
        build_fcpxml(spine_items=gap), tmp_path, location=f"{SPINE}/gap[1]", reason=missing
    )


@pytest.mark.parametrize(
    ("asset", "missing"),
    [
        ('<asset name="a.mp4" start="0s" duration="60s" format="r1"/>', "id"),
        ('<asset id="r9" name="a.mp4" duration="60s" format="r1"/>', "start"),
        ('<asset id="r9" name="a.mp4" start="0s" format="r1"/>', "duration"),
        ('<asset id="r9" name="a.mp4" start="0s" duration="60s"/>', "format"),
    ],
)
def test_asset_missing_required_attributes_are_rejected(
    tmp_path: Path, asset: str, missing: str
) -> None:
    expect_reject(
        build_fcpxml(resources=f"{FORMAT_30}\n  {asset}"),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]",
        reason=missing,
    )


def test_sequence_missing_required_attributes_are_rejected(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(sequence_attrs='format="r1" duration="600s"'),
        tmp_path,
        location="/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]",
        reason="tcStart",
    )
    expect_reject(
        build_fcpxml(sequence_attrs='format="r1" tcStart="0s"'),
        tmp_path,
        location="/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]",
        reason="duration",
    )


def test_format_missing_frame_duration_is_rejected(tmp_path: Path) -> None:
    fmt = '<format id="r1" name="F" width="1920" height="1080"/>'
    expect_reject(
        build_fcpxml(resources=f"{fmt}\n  {ASSET_DEFAULT}"),
        tmp_path,
        location="/fcpxml[1]/resources[1]/format[1]",
        reason="frameDuration",
    )


def test_media_rep_missing_src_is_rejected(tmp_path: Path) -> None:
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media"/>\n'
        "  </asset>"
    )
    expect_reject(
        build_fcpxml(resources=resources),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]/media-rep[1]",
        reason="src",
    )


# ─── 교정 — 설치된 1.14 DTD에 맞춘 어휘와 좌표 ──────────────────────────


def test_media_rep_kinds_translate_from_dtd_spellings(tmp_path: Path) -> None:
    """XML의 original-media/proxy-media가 내부 kind original/proxy로 온다."""
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" uid="AAAA1111" start="0s" duration="600s"'
        ' hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        '    <media-rep kind="proxy-media" src="file:///media/source_proxy.mov"/>\n'
        "  </asset>"
    )
    document = parse(build_fcpxml(resources=resources), tmp_path)
    assert [rep.kind for rep in document.resources[0].media_reps] == ["original", "proxy"]


def test_media_rep_kind_defaults_to_original_media(tmp_path: Path) -> None:
    """DTD 기본값은 original-media다 — kind를 빼면 원본으로 읽는다."""
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        '    <media-rep src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    document = parse(build_fcpxml(resources=resources), tmp_path)
    assert document.resources[0].media_reps == (
        MediaRep(kind="original", src="file:///media/source.mp4"),
    )


@pytest.mark.parametrize(
    ("attribute", "token"),
    [
        ("audioStart", "audioStart"),
        ("audioDuration", "audioDuration"),
    ],
)
def test_split_edit_overrides_are_rejected(tmp_path: Path, attribute: str, token: str) -> None:
    """분할 편집(audioStart·audioDuration)은 오디오 시각을 따로 미는 오버라이드다."""
    items = (
        f'<asset-clip ref="r2" name="클립" offset="0s" start="10s" duration="5s"'
        f' {attribute}="1/30s"/>'
    )
    expect_reject(
        build_fcpxml(spine_items=items),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason=token,
    )


FORMAT_NTSC = (
    '<format id="rNTSC" name="FFVideoFormat1080p2997" frameDuration="1001/30000s"'
    ' width="1920" height="1080"/>'
)


def test_clip_format_override_matching_rate_is_accepted(tmp_path: Path) -> None:
    """asset-clip의 format은 선택 오버라이이다 — 같은 격자면 그대로 통과한다."""
    items = '<asset-clip ref="r2" name="클립" format="r1" offset="0s" start="10s" duration="5s"/>'
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    assert len(document.storyline) == 1


def test_clip_format_override_unresolved_is_rejected(tmp_path: Path) -> None:
    items = '<asset-clip ref="r2" name="클립" format="rX" offset="0s" start="10s" duration="5s"/>'
    expect_reject(
        build_fcpxml(spine_items=items), tmp_path, location=f"{SPINE}/asset-clip[1]", reason="rX"
    )


def test_clip_format_override_rate_mismatch_is_rejected(tmp_path: Path) -> None:
    """오버라이가 시퀀스 격자와 다른 레이트를 가리키면 거부한다 — 리샘플링하지 않는다."""
    items = (
        '<asset-clip ref="r2" name="클립" format="rNTSC" offset="0s" start="10s" duration="5s"/>'
    )
    expect_reject(
        build_fcpxml(
            resources=f"{FORMAT_30}\n  {FORMAT_NTSC}\n  {ASSET_DEFAULT}", spine_items=items
        ),
        tmp_path,
        location=f"{SPINE}/asset-clip[1]",
        reason="1001/30000",
    )


def test_spine_format_override_matching_rate_is_accepted(tmp_path: Path) -> None:
    document = parse(build_fcpxml(spine_attrs=' format="r1"'), tmp_path)
    assert document.frame_duration == Fraction(1, 30)


def test_spine_format_override_unresolved_is_rejected(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(spine_attrs=' format="rX"'),
        tmp_path,
        location=SPINE,
        reason="rX",
    )


def test_spine_format_override_rate_mismatch_is_rejected(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(
            resources=f"{FORMAT_30}\n  {FORMAT_NTSC}\n  {ASSET_DEFAULT}",
            spine_attrs=' format="rNTSC"',
        ),
        tmp_path,
        location=SPINE,
        reason="1001/30000",
    )


def test_spine_offset_zero_or_absent_is_accepted(tmp_path: Path) -> None:
    """주요 스토리라인의 offset은 없거나 정확히 0s여야 한다 — 없어도 뜻은 같다."""
    assert len(parse(build_fcpxml(spine_attrs=' offset="0s"'), tmp_path).storyline) == 3


def test_spine_offset_nonzero_is_rejected(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(spine_attrs=' offset="1s"'),
        tmp_path,
        location=SPINE,
        reason="offset",
    )


def test_spine_lane_zero_or_absent_is_accepted(tmp_path: Path) -> None:
    assert len(parse(build_fcpxml(spine_attrs=' lane="0"'), tmp_path).storyline) == 3


def test_spine_lane_nonzero_is_rejected(tmp_path: Path) -> None:
    expect_reject(
        build_fcpxml(spine_attrs=' lane="1"'),
        tmp_path,
        location=SPINE,
        reason="lane",
    )


def test_asset_metadata_child_is_ordinary_content(tmp_path: Path) -> None:
    """애셋의 metadata는 평범한 내용이다 — raw_xml이 그대로 들고 간다."""
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        '    <metadata><md key="com.apple.fcpxml.mediarep" value="v"/></metadata>\n'
        "  </asset>"
    )
    document = parse(build_fcpxml(resources=resources), tmp_path)
    assert document.resources[0].media_reps[0].kind == "original"
    assert b"<md key=" in document.raw_xml


def test_asset_name_absent_maps_to_empty_string(tmp_path: Path) -> None:
    """애셋 name은 DTD에서 선택이다 — 없으면 빈 문자열로 정한다."""
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" start="0s" duration="600s" hasAudio="1" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    document = parse(build_fcpxml(resources=resources), tmp_path)
    assert document.resources[0].name == ""


def test_clip_offset_and_start_default_to_zero(tmp_path: Path) -> None:
    """asset-clip의 offset·start는 각각 정확히 0s가 기본이다."""
    items = '<asset-clip ref="r2" name="클립" duration="5s"/>'
    document = parse(build_fcpxml(spine_items=items), tmp_path)
    clip = document.storyline[0]
    assert isinstance(clip, ClipInstance)
    assert clip.offset == Fraction(0)
    assert clip.start == Fraction(0)


def test_has_audio_omission_is_outside_the_readiness_subset(tmp_path: Path) -> None:
    """hasAudio 빠짐은 문법상 유효하지만 이 준비 부분집합 밖이다 — 추측하지 않는다.

    기본값을 true/false로 두지 않는다: '오디오 없음'은 오직 명시적인 0만 선언한다.
    """
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    expect_reject(
        build_fcpxml(resources=resources),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]",
        reason="hasAudio",
    )


def test_has_video_omission_is_uninterpreted(tmp_path: Path) -> None:
    """hasVideo는 모델에 필드가 없다 — 없으면 아무 결론도 내리지 않는다."""
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        '    <media-rep kind="original-media" src="file:///media/source.mp4"/>\n'
        "  </asset>"
    )
    document = parse(build_fcpxml(resources=resources), tmp_path)
    assert not hasattr(document.resources[0], "has_video")


def test_has_video_invalid_value_is_rejected(tmp_path: Path) -> None:
    resources = (
        f"{FORMAT_30}\n"
        '  <asset id="r2" name="source.mp4" start="0s" duration="600s" hasAudio="1"'
        ' hasVideo="maybe" format="r1"/>\n'
        "  " + ASSET_DEFAULT
    )
    expect_reject(
        build_fcpxml(resources=resources),
        tmp_path,
        location="/fcpxml[1]/resources[1]/asset[1]",
        reason="hasVideo",
    )


# ─── 불변 모델과 얼어 붙은 인터페이스 ────────────────────────────────────


def test_parsed_models_cannot_be_mutated(tmp_path: Path) -> None:
    document = parse(build_fcpxml(), tmp_path)
    resource = document.resources[0]
    clip, gap = document.storyline[0], document.storyline[1]
    assert isinstance(clip, ClipInstance) and isinstance(gap, GapInstance)
    rep = resource.media_reps[0]
    for field in ("kind", "src"):
        with pytest.raises(FrozenInstanceError):
            setattr(rep, field, "x")  # type: ignore[misc]
    for field in ("xml_id", "scoped_id", "uid", "frame_duration", "media_reps"):
        with pytest.raises(FrozenInstanceError):
            setattr(resource, field, "x")  # type: ignore[misc]
    for field in ("instance_id", "offset", "start", "enabled", "src_enable"):
        with pytest.raises(FrozenInstanceError):
            setattr(clip, field, "x")  # type: ignore[misc]
    for field in ("instance_id", "offset", "duration"):
        with pytest.raises(FrozenInstanceError):
            setattr(gap, field, "x")  # type: ignore[misc]
    for field in ("raw_xml", "storyline"):
        with pytest.raises(FrozenInstanceError):
            setattr(document, field, "x")  # type: ignore[misc]


def test_all_project_models_are_frozen_and_slotted() -> None:
    from dataclasses import fields

    for model in (MediaRep, AssetResource, ClipInstance, GapInstance, ProjectDocument):
        assert model.__dataclass_params__.frozen, model
        field_names = tuple(field.name for field in fields(model))
        assert field_names == tuple(model.__slots__), model


def test_frozen_interface_signatures_match_the_contract() -> None:
    """P1-03·P1-05가 소비하는 이름·순서 — 계약 그대로다."""
    from dataclasses import fields

    assert [field.name for field in fields(MediaRep)] == ["kind", "src"]
    assert [field.name for field in fields(AssetResource)] == [
        "xml_id",
        "scoped_id",
        "uid",
        "name",
        "format_id",
        "frame_duration",
        "start",
        "duration",
        "has_audio",
        "media_reps",
    ]
    assert [field.name for field in fields(ClipInstance)] == [
        "instance_id",
        "element_path",
        "resource_scoped_id",
        "offset",
        "start",
        "duration",
        "enabled",
        "src_enable",
    ]
    assert [field.name for field in fields(GapInstance)] == [
        "instance_id",
        "element_path",
        "offset",
        "duration",
    ]
    assert [field.name for field in fields(ProjectDocument)] == [
        "input_path",
        "info_path",
        "input_sha256",
        "raw_xml",
        "version",
        "sequence_tc_start",
        "frame_duration",
        "resources",
        "storyline",
    ]


def test_unsupported_project_carries_location_and_reason(tmp_path: Path) -> None:
    assert issubclass(UnsupportedProject, RuntimeError)
    error = UnsupportedProject(location="/fcpxml[1]", reason="이유")
    assert error.location == "/fcpxml[1]"
    assert error.reason == "이유"
    assert "/fcpxml[1]" in str(error)
    assert "이유" in str(error)


# ─── 입력은 결코 다시 쓰이지 않는다 ──────────────────────────────────────


def test_input_file_is_never_rewritten(tmp_path: Path) -> None:
    path = write_project(tmp_path, build_fcpxml())
    before = path.read_bytes()
    stat_before = path.stat()
    parse_project(path)
    parse_project(path)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == stat_before.st_mtime_ns


def test_parser_uses_p1_01_timebase_exactly(tmp_path: Path) -> None:
    """시각은 전부 `parse_fcpx_time`의 `Fraction`이고 격자는 timebase 정의를 쓴다."""
    body = build_fcpxml()
    document = parse(body, tmp_path)
    clip = document.storyline[0]
    assert isinstance(clip, ClipInstance)
    assert clip.offset == parse_fcpx_time("0s")
    assert clip.duration == parse_fcpx_time("100s")
    assert document.frame_duration == parse_fcpx_time("1/30s")


def test_parser_module_stays_off_legacy_and_subprocess() -> None:
    """파서는 표준 라이브러리 XML만 쓴다 — subprocess·openai·레거시 import가 없다."""
    import ast
    import sys

    module = sys.modules["vlogsrt.project.fcpxml"]
    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            roots = [node.module or ""]
        else:
            continue
        for root in roots:
            assert root not in {"subprocess", "openai"}, f"파서가 {root}을(를) import한다"
            if root.startswith("vlogsrt"):
                assert root in {
                    "vlogsrt.project",
                    "vlogsrt.project.models",
                    "vlogsrt.project.timebase",
                }, f"파서가 허용 밖 모듈을 import한다: {root}"
