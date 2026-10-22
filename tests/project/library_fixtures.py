"""합성 `.fcpbundle` 재료 — 실물 라이브러리 없이 읽기 계약을 시험한다.

Final Cut의 라이브러리는 Core Data SQLite 두 층이다. 여기서는 그 **최소 모양**만
손으로 조립한다 — `ZCOLLECTION`/`ZCOLLECTIONMD` 두 테이블과 각 행의
`NSKeyedArchiver` 블롭(`$version`/`$archiver`/`$top`/`$objects`, `NS.keys`/
`NS.objects`, `plistlib.UID`). 모양은 Final Cut Pro 12.3(catalogVersion 1193)
실물 덤프에서 냈고, 12.4(`1195; 12.4`)에서 읽는 키(`mediaRange`·
`timecodeDisplayDropFrame`)를 더했다(W-16). ffmpeg·ffprobe를 부르지 않는다.
"""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

CATALOG_DB = "CurrentVersion.flexolibrary"
CATALOG_VERSION = "1195; 12.4 (454072)"
"""실물 12.4 라이브러리의 `catalogVersion` 철자 그대로 — 번호와 앱 버전이 한 문자열이다."""
EVENT_DB = "CurrentVersion.fcpevent"
LOCK_INFO = ".lock-info"

NTSC = Fraction(1001, 30000)

RAD_ASSET = "FFMIORADAsset"
ASSET = "FFAsset"

NSNULL = object()
"""값 자리의 `NSNull` 인스턴스 — 실물 12.3 백업(제주 day1·day3)의 `videoFormatName`이 이 모양이다.
리더가 역참조하면 `{'$class': UID(n)}` 사전이 된다(문자열이 아니다)."""


def archived(record: dict[str, object]) -> bytes:
    """평범한 사전 하나를 NSKeyedArchiver bplist 블롭으로 조립한다.

    값이 리프(문자열·정수·불린)면 한 겹 역참조로 읽힌다. 값이 `NSNULL`이면 그 자리에 NSNull
    클래스를 가리키는 객체를 둔다. 값이 사전이면(12.4 `FFAsset`의 `videoProps`·
    `audioSourceDict`) 실물처럼 `NS.keys`/`NS.objects` 사전으로 접어 클래스 뒤에 붙인다 —
    리프만 든 기록의 바이트는 예전 그대로다.
    """
    count = len(record)
    dictionary_class = 2 + 2 * count
    root = {
        "NS.keys": [plistlib.UID(2 + 2 * index) for index in range(count)],
        "NS.objects": [plistlib.UID(3 + 2 * index) for index in range(count)],
        "$class": plistlib.UID(dictionary_class),
    }
    objects: list[object] = ["$null", root]
    for key in record:
        objects.extend([key, None])
    objects.append(
        {
            "$classname": "NSMutableDictionary",
            "$classes": ["NSMutableDictionary", "NSDictionary", "NSObject"],
        }
    )
    objects.append({"$classname": "NSNull", "$classes": ["NSNull", "NSObject"]})
    for index, value in enumerate(record.values()):
        objects[3 + 2 * index] = _node(value, objects, dictionary_class)
    return plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
    )


def _node(value: object, objects: list[object], dictionary_class: int) -> object:
    if value is NSNULL:
        return {"$class": plistlib.UID(dictionary_class + 1)}
    if isinstance(value, dict):
        keys = [_push(key, objects, dictionary_class) for key in value]
        values = [_push(item, objects, dictionary_class) for item in value.values()]
        return {"NS.keys": keys, "NS.objects": values, "$class": plistlib.UID(dictionary_class)}
    return value


def _push(value: object, objects: list[object], dictionary_class: int) -> plistlib.UID:
    objects.append(None)
    index = len(objects) - 1
    objects[index] = _node(value, objects, dictionary_class)
    return plistlib.UID(index)


def archived_sequence(values: Sequence[object]) -> bytes:
    """뿌리가 NSArray인 그래프 — 사전이 아니라고 거부되는 입력을 만드는 데 쓴다."""
    objects: list[object] = [
        "$null",
        {
            "NS.objects": [plistlib.UID(2 + index) for index in range(len(values))],
            "$class": plistlib.UID(2 + len(values)),
        },
        *values,
        {"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]},
    ]
    return plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
    )


def write_database(path: Path, rows: Sequence[tuple[str, bytes]]) -> Path:
    path.unlink(missing_ok=True)  # 다시 쓰는 시험(행 일부만 남기기)을 허용한다
    connection = sqlite3.connect(path)
    try:
        connection.execute("create table ZCOLLECTION (Z_PK integer primary key, ZTYPE text)")
        connection.execute("create table ZCOLLECTIONMD (ZCOLLECTION integer, ZDICTIONARYDATA blob)")
        for primary_key, (kind, blob) in enumerate(rows, start=1):
            connection.execute(
                "insert into ZCOLLECTION (Z_PK, ZTYPE) values (?, ?)", (primary_key, kind)
            )
            connection.execute(
                "insert into ZCOLLECTIONMD (ZCOLLECTION, ZDICTIONARYDATA) values (?, ?)",
                (primary_key, blob),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _range(start: Fraction, duration: Fraction) -> str:
    return f"{{({start}),({duration})}}"


def _identity(name: str) -> str:
    # md5는 암호가 아니라 시험 신원을 만드는 치환일 뿐이다.
    return hashlib.md5(name.encode()).hexdigest().upper()


@dataclass
class MediaSpec:
    """이벤트에 넣을 영상 하나 — 선언값은 여기서 정하는 그대로 DB에 적힌다."""

    name: str
    frames: int = 100
    frame_duration: str = "1001/30000"
    format: object = "FFVideoFormat3840x2160p2997"
    """`NSNULL`이면 실물 백업의 깨진 기록처럼 `videoFormatName`이 NSNull이다."""
    has_video: bool = True
    has_audio: bool = True
    audio_rate: int | None = 48000
    audio_channels: int | None = 2
    proxy: bool = False
    declared_frames: int | None = None
    """선언 프레임 수를 실측과 다르게 적어 두는 구멍 — 거부 시험용."""
    duration_override: str | None = None
    """선언 길이를 통째로 정해 쓰는 구멍 — 프레임 격자 밖 선언을 만드는 데 쓴다."""
    start_frames: int = 0
    """`mediaRange` 시작(타임코드 프레임 수) — 실물 DJI 원본은 0이 아니다."""
    drop_frame: object = 1
    """`timecodeDisplayDropFrame` 값. 실물은 정수 1이다. `None`이면 키를 빼고 쓴다."""
    media_range_override: str | None = None
    """`mediaRange`를 통째로 정해 쓰는 구멍. 빈 문자열이면 키를 빼고 쓴다."""
    record: str = RAD_ASSET
    """자산 기록 종류 — `FFAsset`이면 12.4에서 새로 가져온 실물의 키 모양으로 적는다(D72)."""
    rotation: float | None = None
    """`FFAsset`의 `rotationDegrees` — 실물 세로 아이폰은 -90.0, 가로 원본에는 키가 없다."""
    display: tuple[int, int] = (3840, 2160)
    """`FFAsset`의 `videoProps.PB` — 회전 뒤 재생 화면 크기(실물 세로 `{{0, 0}, {2160, 3840}}`)."""
    omit: tuple[str, ...] = ()
    """`FFAsset` 기록에서 뺄 키 — 빠진 키 거부 시험용."""

    @property
    def identity(self) -> str:
        return _identity(self.name)

    @property
    def duration(self) -> str:
        if self.duration_override is not None:
            return self.duration_override
        frames = self.declared_frames if self.declared_frames is not None else self.frames
        return str(Fraction(frames) * Fraction(self.frame_duration))

    @property
    def media_range(self) -> str:
        if self.media_range_override is not None:
            return self.media_range_override
        start = Fraction(self.start_frames) * Fraction(self.frame_duration)
        return _range(start, Fraction(self.duration))


@dataclass
class ClipSpec:
    name: str
    start: Fraction = Fraction(0)
    frames: int = 100


@dataclass
class ProjectSpec:
    name: str
    frame_duration: str = "1001/30000"
    format: str = "FFVideoFormat3840x2160p2997"
    clips: Sequence[ClipSpec] = field(default_factory=list)

    @property
    def duration(self) -> str:
        total = sum((Fraction(clip.frames) for clip in self.clips), start=Fraction(0))
        return str(total * Fraction(self.frame_duration))


def _media_rows(event_dir: Path, media: Sequence[MediaSpec]) -> list[tuple[str, bytes]]:
    rows: list[tuple[str, bytes]] = []
    for spec in media:
        identity = spec.identity
        original = event_dir / "Original Media" / f"{spec.name}.mov"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"original")
        rows.append(
            (
                "FFMediaRep",
                archived(
                    {
                        "md5Seed": identity,
                        "projectRelativePath": f"Original Media/{spec.name}.mov",
                        "repType": "originalMediaRep",
                    }
                ),
            )
        )
        if spec.proxy:
            proxy = event_dir / "Proxy Media" / f"{spec.name}.mov"
            proxy.parent.mkdir(parents=True, exist_ok=True)
            proxy.write_bytes(b"proxy")
            rows.append(
                (
                    "FFMediaRep",
                    archived(
                        {
                            "md5Seed": identity,
                            "projectRelativePath": f"Proxy Media/{spec.name}.mov",
                            "repType": "proxyMediaRep",
                        }
                    ),
                )
            )
        if spec.record == ASSET:
            rows.append((ASSET, archived(asset_12_4_record(spec))))
        else:
            rows.append((RAD_ASSET, archived(_asset_record(spec))))
    return rows


def _video_props(spec: MediaSpec) -> dict[str, object]:
    """`PB`는 회전 뒤 재생 화면 크기, `FRD`는 프레임 길이의 CMTime 사전이다(실물 20/600)."""
    frame = Fraction(spec.frame_duration)
    width, height = spec.display
    return {
        "PB": f"{{{{0, 0}}, {{{width}, {height}}}}}",
        "FRD": {"flags": 1, "value": frame.numerator, "timescale": frame.denominator, "epoch": 0},
        "CSE": 65793,
    }


def asset_12_4_record(spec: MediaSpec) -> dict[str, object]:
    """12.4가 새로 가져온 미디어의 `FFAsset` — 실물(reelog IMG_8877) 키 모양 그대로.

    `durationForOfflineProvider`가 없고 길이는 `mediaRange`뿐이며, `videoFormatName`은 NSNull
    객체이고, 재생 화면은 `videoProps.PB`, 회전은 `rotationDegrees`(가로 원본에는 없다)다.
    """
    record: dict[str, object] = {
        "alphaHandling": 2,
        "audioChannelCount": spec.audio_channels,
        "audioSampleRate": float(spec.audio_rate or 0),
        "audioSourceCount": 1,
        "audioSourceDict": {"a1": {"FFMediaAudioSourceDefaultEnableKey": True}},
        "audioSourceDictVersion": 3,
        "displayName": spec.name,
        "fieldDominanceOverride": 1,
        "hasAudio": spec.has_audio,
        "hasClosedCaptionTrack": 1,
        "hasVideo": spec.has_video,
        "md5String": spec.identity,
        "mediaIdentifier": spec.identity,
        "mediaRange": spec.media_range,
        "rotationDegrees": spec.rotation,
        "timecodeDisplayDropFrame": spec.drop_frame,
        "timecodeFrameDuration": spec.frame_duration,
        "uttype": "com.apple.quicktime-movie",
        "videoFormatName": spec.format,
        "videoProps": _video_props(spec),
    }
    dropped = {"rotationDegrees"} if spec.rotation is None else set()
    if spec.drop_frame is None:
        dropped.add("timecodeDisplayDropFrame")
    if not spec.media_range:
        dropped.add("mediaRange")
    return {key: value for key, value in record.items() if key not in {*dropped, *spec.omit}}


def _asset_record(spec: MediaSpec) -> dict[str, object]:
    record: dict[str, object] = {
        "mediaIdentifier": spec.identity,
        "displayName": spec.name,
        "timecodeFrameDuration": spec.frame_duration,
        "durationForOfflineProvider": spec.duration,
        "videoFormatName": spec.format,
        "hasVideo": spec.has_video,
        "hasAudio": spec.has_audio,
        "audioSampleRate": spec.audio_rate,
        "audioChannelCount": spec.audio_channels,
    }
    if spec.media_range:
        record["mediaRange"] = spec.media_range
    if spec.drop_frame is not None:
        record["timecodeDisplayDropFrame"] = spec.drop_frame
    return record


def _project_rows(project: ProjectSpec) -> list[tuple[str, bytes]]:
    rows: list[tuple[str, bytes]] = [
        (
            "FFSequenceInfo",
            archived(
                {
                    "videoFormatKey": project.format,
                    "timecodeFrameDuration": project.frame_duration,
                    "mediaRange": _range(Fraction(0), Fraction(project.duration)),
                }
            ),
        )
    ]
    for clip in project.clips:
        duration = Fraction(clip.frames) * Fraction(project.frame_duration)
        rows.append(
            (
                "FFAnchoredCollection",
                archived({"displayName": clip.name, "clippedRange": _range(clip.start, duration)}),
            )
        )
    return rows


def make_bundle(
    root: Path,
    *,
    media: dict[str, Sequence[MediaSpec]] | None = None,
    projects: dict[str, Sequence[ProjectSpec]] | None = None,
    catalog_version: str = CATALOG_VERSION,
    locked: bool = False,
    name: str = "synthetic.fcpbundle",
) -> Path:
    """번들을 만든다 — `media`·`projects`의 키가 이벤트 이름이다."""
    bundle = root / name
    bundle.mkdir(parents=True)
    event_names = sorted({*(media or {}), *(projects or {})})
    catalog_rows: list[tuple[str, bytes]] = [
        ("FFEventRecord", archived({"relativePath": event})) for event in event_names
    ]
    project_names = {project.name for specs in (projects or {}).values() for project in specs}
    catalog_rows.extend(
        ("FFSequenceRecord", archived({"relativePath": project_name}))
        for project_name in sorted(project_names)
    )
    write_database(bundle / CATALOG_DB, catalog_rows)
    for event_name in event_names:
        event_dir = bundle / event_name
        event_dir.mkdir()
        rows = _media_rows(event_dir, (media or {}).get(event_name, ()))
        write_database(event_dir / EVENT_DB, rows)
        for project in (projects or {}).get(event_name, ()):
            project_dir = event_dir / project.name
            project_dir.mkdir()
            write_database(project_dir / EVENT_DB, _project_rows(project))
    if catalog_version:
        plist = plistlib.dumps({"catalogVersion": catalog_version})
        (bundle / "CurrentVersion.plist").write_bytes(plist)
    if locked:
        (bundle / LOCK_INFO).write_bytes(b"")
    return bundle
