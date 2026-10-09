"""라이브러리 리더의 계약 — 합성 `.fcpbundle`로 증명한다 (R-08b, W-16에서 이식).

실물 라이브러리는 Final Cut Pro 12.3(1193)에서 12.4(`1195; 12.4`)로 올라간 하나뿐이라
재료는 전부 테스트가 직접 만든다: `sqlite3` 두 테이블 + `plistlib` NSKeyedArchiver
블롭. 시각 비교는 전부 `Fraction` 단언이다 — 부동소수를 거치는 순간 「정확한 분수」
계약이 사라진다. W-16은 클립 시작(`mediaRange` 시작)과 드롭프레임 표시를 더 읽는다.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from pathlib import Path

import pytest
from library_fixtures import (
    CATALOG_VERSION,
    EVENT_DB,
    ClipSpec,
    MediaSpec,
    ProjectSpec,
    archived,
    archived_sequence,
    make_bundle,
    write_database,
)

from vlogsrt.project import library
from vlogsrt.project.library import parse_range
from vlogsrt.project.models import UnsupportedProject

NTSC = Fraction(1001, 30000)

_QUERY = (
    "select c.ZTYPE, m.ZDICTIONARYDATA from ZCOLLECTION c"
    " join ZCOLLECTIONMD m on m.ZCOLLECTION = c.Z_PK"
)


def _media(name: str, frames: int = 100, **kwargs: object) -> MediaSpec:
    return MediaSpec(name=name, frames=frames, **kwargs)  # type: ignore[arg-type]


def _rows_of(database: Path) -> list[tuple[str, bytes]]:
    connection = sqlite3.connect(f"file:{database}?immutable=1", uri=True)
    try:
        return [(kind, blob) for kind, blob in connection.execute(_QUERY)]
    finally:
        connection.close()


def _catalog_event(bundle: Path, name: str) -> library.LibraryEvent:
    return next(event for event in library.read_catalog(bundle).events if event.name == name)


# ─── parse_range — 정확한 분수 ───────────────────────────────────────────


def test_parse_range_reads_exact_fractions() -> None:
    start, duration = library.parse_range("{(1688359673/30000),(1065064/30000)}", "시험")
    assert start == Fraction(1688359673, 30000)
    assert duration == Fraction(1065064, 30000)


def test_parse_range_accepts_integers_and_negatives() -> None:
    assert library.parse_range("{(0),(100)}", "시험") == (0, 100)
    assert library.parse_range("{(-5/2),(3)}", "시험") == (Fraction(-5, 2), 3)


@pytest.mark.parametrize("value", ["0.5s", "{(1/2)}", "(1/2),(3/4)", ""])
def test_parse_range_rejects_other_notations(value: str) -> None:
    with pytest.raises(UnsupportedProject):
        library.parse_range(value, "시험")


# ─── read_catalog ────────────────────────────────────────────────────────


def test_catalog_lists_events_projects_version_and_lock(tmp_path: Path) -> None:
    bundle = make_bundle(
        tmp_path,
        media={"clips": (_media("a"),)},
        projects={"편집": (ProjectSpec(name="거친컷", clips=[ClipSpec("a")]),)},
        catalog_version="1193",
    )
    catalog = library.read_catalog(bundle)
    assert [event.name for event in catalog.events] == ["clips", "편집"]  # 이름 오름차순
    project = catalog.projects[0]
    assert (project.name, project.event) == ("거친컷", "편집")
    assert project.path == bundle / "편집" / "거친컷"
    assert catalog.catalog_version == "1193"
    assert catalog.locked is False


def test_catalog_reports_the_12_4_version_string_verbatim(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)})
    assert library.read_catalog(bundle).catalog_version == CATALOG_VERSION == "1195; 12.4 (454072)"


def test_catalog_lock_follows_lock_info_file(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)}, locked=True)
    assert library.read_catalog(bundle).locked is True


def test_catalog_without_version_plist_reports_empty_version(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)}, catalog_version="")
    assert library.read_catalog(bundle).catalog_version == ""


def test_catalog_drops_events_without_database(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)})
    (bundle / "clips" / EVENT_DB).unlink()
    catalog = library.read_catalog(bundle)
    assert catalog.events == ()
    assert catalog.projects == ()


def test_non_fcpbundle_path_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-bundle.txt"
    plain.mkdir()
    with pytest.raises(UnsupportedProject) as caught:
        library.read_catalog(plain)
    assert "라이브러리(.fcpbundle)가 아니다" in caught.value.reason
    with pytest.raises(UnsupportedProject):
        library.read_catalog(tmp_path / "없는.fcpbundle")


def test_missing_catalog_database_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)})
    (bundle / "CurrentVersion.flexolibrary").unlink()
    with pytest.raises(UnsupportedProject) as caught:
        library.read_catalog(bundle)
    assert "데이터베이스가 없다" in caught.value.reason


# ─── read_event_media ────────────────────────────────────────────────────


def test_event_media_pairs_original_and_proxy_by_content_identity(tmp_path: Path) -> None:
    bundle = make_bundle(
        tmp_path, media={"clips": (_media("b", 200), _media("a", 100, proxy=True))}
    )
    media = library.read_event_media(_catalog_event(bundle, "clips"))
    assert [row.name for row in media] == ["a", "b"]  # 이름 오름차순 — 곧 타임라인 순서
    first, second = media
    assert first.original == bundle / "clips" / "Original Media" / "a.mov"
    assert first.proxy == bundle / "clips" / "Proxy Media" / "a.mov"
    assert second.proxy is None  # proxy rep이 없으면 None
    assert first.media_identifier == _media("a").identity  # md5Seed ↔ mediaIdentifier 조인
    assert first.frame_duration == NTSC
    assert first.frame_count == 100
    assert first.duration == Fraction(100) * NTSC
    assert (first.has_audio, first.audio_rate, first.audio_channels) == (True, 48000, 2)


def test_event_media_proxy_missing_on_disk_is_none(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a", proxy=True),)})
    (bundle / "clips" / "Proxy Media" / "a.mov").unlink()
    (media,) = library.read_event_media(_catalog_event(bundle, "clips"))
    assert media.proxy is None


def test_event_media_without_original_rep_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)})
    event = _catalog_event(bundle, "clips")
    rows = [
        (kind, blob)
        for kind, blob in _rows_of(event.path / EVENT_DB)
        if not (kind == "FFMediaRep" and b"Original Media" in blob)
    ]
    write_database(event.path / EVENT_DB, rows)
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(event)
    assert "original media-rep이 없다" in caught.value.reason


def test_event_media_with_missing_key_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)})
    event = _catalog_event(bundle, "clips")
    rows = [
        (kind, blob) for kind, blob in _rows_of(event.path / EVENT_DB) if kind != "FFMIORADAsset"
    ]
    rows.append(
        (
            "FFMIORADAsset",
            archived({"mediaIdentifier": _media("a").identity, "displayName": "a"}),
        )
    )
    write_database(event.path / EVENT_DB, rows)
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(event)
    assert "timecodeFrameDuration" in caught.value.reason


def test_event_media_with_broken_blob_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)})
    event = _catalog_event(bundle, "clips")
    write_database(event.path / EVENT_DB, [("FFMIORADAsset", "\x00 깨진 블롭".encode())])
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(event)
    assert "binary plist" in caught.value.reason


def test_event_media_with_non_dict_root_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a"),)})
    event = _catalog_event(bundle, "clips")
    write_database(event.path / EVENT_DB, [("FFMIORADAsset", archived_sequence(["항목"]))])
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(event)
    assert "뿌리가 사전이 아니다" in caught.value.reason


def test_frame_count_off_grid_is_rejected(tmp_path: Path) -> None:
    # 선언 길이가 프레임 격자에 안 떨어진다 — 100.5프레임어치
    off_grid = _media("a", frames=100, duration_override=str(Fraction(201, 2) * NTSC))
    bundle = make_bundle(tmp_path, media={"clips": (off_grid,)})
    (media,) = library.read_event_media(_catalog_event(bundle, "clips"))
    with pytest.raises(UnsupportedProject) as caught:
        _ = media.frame_count
    assert "프레임 격자" in caught.value.reason


# ─── read_project_timeline ───────────────────────────────────────────────


def test_project_timeline_keeps_exact_fractions(tmp_path: Path) -> None:
    project = ProjectSpec(
        name="거친컷",
        clips=[ClipSpec("a", start=Fraction(0), frames=100), ClipSpec("b", frames=200)],
    )
    bundle = make_bundle(tmp_path, projects={"clips": (project,)})
    timeline = library.read_project_timeline(library.read_catalog(bundle).projects[0])
    assert timeline.name == "거친컷"
    assert timeline.format_name == "FFVideoFormat3840x2160p2997"
    assert timeline.frame_duration == NTSC
    assert timeline.duration == Fraction(300) * NTSC
    assert [(clip.name, clip.source_start, clip.duration) for clip in timeline.clips] == [
        ("a", Fraction(0), Fraction(100) * NTSC),
        ("b", Fraction(0), Fraction(200) * NTSC),
    ]


def test_project_timeline_without_sequence_info_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, projects={"clips": (ProjectSpec("p"),)})
    project = library.read_catalog(bundle).projects[0]
    rows = [
        (kind, blob) for kind, blob in _rows_of(project.path / EVENT_DB) if kind != "FFSequenceInfo"
    ]
    write_database(project.path / EVENT_DB, rows)
    with pytest.raises(UnsupportedProject) as caught:
        library.read_project_timeline(project)
    assert "FFSequenceInfo가 없다" in caught.value.reason


def test_range_with_zero_denominator_is_rejected() -> None:
    """정규식은 분모 자리에 0도 받는다 — 거기서 죽으면 거부가 아니라 사고다."""
    with pytest.raises(UnsupportedProject) as caught:
        parse_range("{(1/0),(2)}", "어딘가")
    assert "분모가 0" in caught.value.reason


# ─── W-16: 클립 시작(mediaRange)·드롭프레임 ─────────────────────────────


def test_event_media_reads_media_start_and_drop_frame_like_the_real_library(
    tmp_path: Path,
) -> None:
    """실물 0008: `mediaRange` 시작 `1397678282/30000` = 1396282프레임, 드롭프레임 1."""
    real = _media("DJI_0008", frames=1168, start_frames=1396282, drop_frame=1)
    bundle = make_bundle(tmp_path, media={"clips": (real,)})
    (media,) = library.read_event_media(_catalog_event(bundle, "clips"))
    assert media.media_start == Fraction(1397678282, 30000)
    assert media.start_frame == 1396282
    assert media.drop_frame is True
    assert media.duration == Fraction(1169168, 30000)


@pytest.mark.parametrize(
    ("flag", "expected"), [(0, False), (1, True), (False, False), (True, True)]
)
def test_drop_frame_flag_accepts_integer_and_boolean_spellings(
    tmp_path: Path, flag: object, expected: bool
) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a", drop_frame=flag),)})
    (media,) = library.read_event_media(_catalog_event(bundle, "clips"))
    assert media.drop_frame is expected


@pytest.mark.parametrize("flag", [None, 2, "1", -1])
def test_unknown_or_missing_drop_frame_flag_is_rejected(tmp_path: Path, flag: object) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a", drop_frame=flag),)})
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(_catalog_event(bundle, "clips"))
    assert "timecodeDisplayDropFrame" in caught.value.reason


def test_missing_media_range_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_media("a", media_range_override=""),)})
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(_catalog_event(bundle, "clips"))
    assert "mediaRange" in caught.value.reason


def test_media_range_length_must_agree_with_declared_duration(tmp_path: Path) -> None:
    """선언 둘이 어긋나면 어느 쪽이 맞는지 모른다 — 고르지 않고 거부한다."""
    spec = _media("a", frames=100, media_range_override="{(0),(3003/30000)}")
    bundle = make_bundle(tmp_path, media={"clips": (spec,)})
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(_catalog_event(bundle, "clips"))
    assert "mediaRange" in caught.value.reason and "길이" in caught.value.reason


def test_media_start_off_the_frame_grid_is_rejected(tmp_path: Path) -> None:
    length = Fraction(100) * NTSC
    spec = _media("a", frames=100, media_range_override=f"{{(1/2),({length})}}")
    bundle = make_bundle(tmp_path, media={"clips": (spec,)})
    (media,) = library.read_event_media(_catalog_event(bundle, "clips"))
    assert media.media_start == Fraction(1, 2)
    with pytest.raises(UnsupportedProject) as caught:
        _ = media.start_frame
    assert "프레임 격자" in caught.value.reason
