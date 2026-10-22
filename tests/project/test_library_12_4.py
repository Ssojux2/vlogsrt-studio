"""12.4가 새로 가져온 미디어의 `FFAsset` 기록을 읽는다 (W-28, D72).

실물 reelog(아이폰 세로 HEVC 34개)와 vibeDL lec1(가로 mp4 1개)은 Final Cut Pro 12.4에서 새로
가져왔고, 자산 기록이 `FFMIORADAsset`이 아니라 `FFAsset`이다 — `durationForOfflineProvider`가
없고 길이는 `mediaRange`뿐이며 `videoFormatName`은 NSNull이다. 12.3에서 가져온 기록(wangbang)은
12.4로 올려도 `FFMIORADAsset` 그대로이고 예전 읽기 그대로여야 한다.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from pathlib import Path

import pytest
from library_fixtures import (
    ASSET,
    EVENT_DB,
    NSNULL,
    NTSC,
    MediaSpec,
    make_bundle,
)

from vlogsrt.project import library
from vlogsrt.project.models import UnsupportedProject

_QUERY = (
    "select c.ZTYPE, m.ZDICTIONARYDATA from ZCOLLECTION c"
    " join ZCOLLECTIONMD m on m.ZCOLLECTION = c.Z_PK"
)


def _rows_of(database: Path) -> list[tuple[str, bytes]]:
    connection = sqlite3.connect(f"file:{database}?immutable=1", uri=True)
    try:
        return [(kind, blob) for kind, blob in connection.execute(_QUERY)]
    finally:
        connection.close()


def _event(bundle: Path, name: str) -> library.LibraryEvent:
    return next(event for event in library.read_catalog(bundle).events if event.name == name)


def _old(name: str, frames: int = 100, **kwargs: object) -> MediaSpec:
    return MediaSpec(name=name, frames=frames, **kwargs)  # type: ignore[arg-type]


def _new(name: str, frames: int = 100, **kwargs: object) -> MediaSpec:
    """실물 reelog 모양 — 30fps · 시작 0 · 드롭프레임 0 · NSNull 포맷 · 세로 회전."""
    fields: dict[str, object] = {
        "record": ASSET,
        "frame_duration": "1/30",
        "format": NSNULL,
        "drop_frame": 0,
        "rotation": -90.0,
        "display": (2160, 3840),
    }
    fields.update(kwargs)
    return _old(name, frames, **fields)


def test_an_ffasset_record_is_indexed_and_read_with_length_from_media_range(
    tmp_path: Path,
) -> None:
    """IMG_8844 모양: `mediaRange` `{(0/600),(16961/600)}` = 848.05프레임 — 선언 길이 키는 없다."""
    spec = _new("IMG_8844", duration_override="16961/600")
    bundle = make_bundle(tmp_path, media={"raw_vid": (spec,)})
    event = _event(bundle, "raw_vid")
    kind, blob = _rows_of(event.path / EVENT_DB)[-1]
    record = library._unarchive(blob, "시험")
    assert kind == "FFAsset" and "durationForOfflineProvider" not in record
    assert record["videoProps"] == {
        "PB": "{{0, 0}, {2160, 3840}}",
        "FRD": {"flags": 1, "value": 1, "timescale": 30, "epoch": 0},
        "CSE": 65793,
    }  # 실물처럼 접힌 사전 — 리더가 한 겹씩 푼다
    index = library.index_event(event)
    assert index.problems == ()
    assert index.asset_types == {spec.identity: "FFAsset"}
    media = index.media(spec.identity)
    assert media.original == bundle / "raw_vid" / "Original Media" / "IMG_8844.mov"
    assert (media.duration, media.media_start) == (Fraction(16961, 600), Fraction(0))
    assert media.frame_duration == Fraction(1, 30)
    assert media.format_name is None  # NSNull — 문자열이 아니어도 거부하지 않는다
    assert (media.record_type, media.container_length) == ("FFAsset", True)
    assert media.frame_count == 848  # 848.05 → 가장 가까운 프레임
    assert (media.has_audio, media.audio_rate, media.audio_channels) == (True, 48000, 2)
    assert media.drop_frame is False and media.start_frame == 0


@pytest.mark.parametrize(
    ("length_ticks", "expected"),
    [(16961, 848), (9030, 452), (18090, 905), (18089, 904), (3799, 190), (3572, 179)],
)
def test_an_ffasset_length_rounds_to_the_nearest_frame_half_up(
    tmp_path: Path, length_ticks: int, expected: int
) -> None:
    """실측 848.05 · 8877 451.5 → 452 · 8865 904.5 → 905(은행가 반올림이면 904) · 8874 189.95
    → 190 · 8850 178.6 → 179."""
    spec = _new("a", duration_override=f"{length_ticks}/600")
    bundle = make_bundle(tmp_path, media={"raw_vid": (spec,)})
    (media,) = library.read_event_media(_event(bundle, "raw_vid"))
    assert media.frame_count == expected


def test_an_ffasset_with_a_format_string_keeps_it(tmp_path: Path) -> None:
    spec = _new("a", format="FFVideoFormat2160x3840p30", rotation=None)
    bundle = make_bundle(tmp_path, media={"raw_vid": (spec,)})
    (media,) = library.read_event_media(_event(bundle, "raw_vid"))
    assert media.format_name == "FFVideoFormat2160x3840p30"


@pytest.mark.parametrize(
    "key", ["mediaRange", "timecodeFrameDuration", "displayName", "timecodeDisplayDropFrame"]
)
def test_an_ffasset_missing_a_required_key_is_refused(tmp_path: Path, key: str) -> None:
    bundle = make_bundle(tmp_path, media={"raw_vid": (_new("a", omit=(key,)),)})
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(_event(bundle, "raw_vid"))
    assert key in caught.value.reason
    assert caught.value.location.endswith(":FFAsset")


def test_an_ffasset_missing_its_identity_is_a_problem_not_a_failure(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"raw_vid": (_new("a", omit=("mediaIdentifier",)),)})
    index = library.index_event(_event(bundle, "raw_vid"))
    assert index.assets == {}
    (problem,) = index.problems
    assert "mediaIdentifier" in problem.reason and problem.location.endswith(":FFAsset")


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"format": 7}, "videoFormatName"),
        ({"format": ""}, "videoFormatName"),
        ({"duration_override": "0"}, "양수"),
        ({"media_range_override": "{(0/600),(-20/600)}"}, "양수"),
        ({"media_range_override": "28.26s"}, "구간 표기"),
    ],
)
def test_an_ffasset_with_an_unknown_shape_is_refused(
    tmp_path: Path, override: dict[str, object], why: str
) -> None:
    bundle = make_bundle(tmp_path, media={"raw_vid": (_new("a", **override),)})
    with pytest.raises(UnsupportedProject) as caught:
        library.read_event_media(_event(bundle, "raw_vid"))
    assert why in caught.value.reason


def test_ffasset_and_ffmioradasset_records_mix_in_one_event(tmp_path: Path) -> None:
    """12.3에서 가져온 것(FFMIORADAsset)과 12.4에서 새로 가져온 것(FFAsset)이 한 이벤트에 있다."""
    old = _old("old", 100)
    new = _new("new", duration_override="1697/60")  # 848.5프레임 → 849
    bundle = make_bundle(tmp_path, media={"clips": (old, new)})
    event = _event(bundle, "clips")
    index = library.index_event(event)
    assert index.asset_types == {old.identity: "FFMIORADAsset", new.identity: "FFAsset"}
    first, second = library.read_event_media(event)
    assert (first.name, first.record_type, first.container_length) == ("new", "FFAsset", True)
    assert (first.frame_count, first.format_name) == (849, None)
    assert (second.name, second.record_type, second.frame_count) == ("old", "FFMIORADAsset", 100)
    assert second.format_name == "FFVideoFormat3840x2160p2997"
    assert second.container_length is False


def test_ffmioradasset_still_refuses_an_off_grid_length_and_a_null_format(tmp_path: Path) -> None:
    """예전 기록은 예전 그대로 — 가장 가까운 프레임은 FFAsset만의 읽기이고, NSNull 포맷의 거부
    문구도 한 글자 바뀌지 않는다(보내기 리포트의 경고 줄)."""
    off_grid = _old("a", duration_override=str(Fraction(201, 2) * NTSC))
    bundle = make_bundle(tmp_path, media={"clips": (off_grid, _old("b", format=NSNULL))})
    index = library.index_event(_event(bundle, "clips"))
    with pytest.raises(UnsupportedProject, match="프레임 격자"):
        _ = index.media(off_grid.identity).frame_count
    with pytest.raises(UnsupportedProject) as caught:
        index.media(_old("b").identity)
    assert caught.value.reason.startswith("videoFormatName를 문자열로 읽지 못했다: {'$class': UID(")
    assert caught.value.location.endswith(":FFMIORADAsset")


def test_a_missing_asset_record_names_both_record_types(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, media={"clips": (_old("a"),)})
    index = library.index_event(_event(bundle, "clips"))
    with pytest.raises(UnsupportedProject) as caught:
        index.media("0" * 32)
    assert "FFMIORADAsset·FFAsset 기록이 없다" in caught.value.reason
