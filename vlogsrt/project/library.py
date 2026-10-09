"""Final Cut 라이브러리(`.fcpbundle`)를 **읽는다. 쓰지 않는다.**

라이브러리는 Core Data SQLite 두 층이다 — 번들 뿌리의 `CurrentVersion.flexolibrary`가
이벤트·프로젝트가 어느 디렉터리인지 적고, 이벤트·프로젝트마다 놓인
`CurrentVersion.fcpevent`가 실제 객체 그래프를 담는다. 객체는 `ZCOLLECTIONMD`의
`bplist00` 블롭이고 그 안은 `NSKeyedArchiver` 그래프다. 키는 사람이 읽을 수 있는
문자열이고 시각은 `{(a/b),(c/d)}` 꼴 **정확한 분수**라, 표준 라이브러리만으로 푼다
(`sqlite3` + `plistlib` — 규칙 10에 새 의존성을 더하지 않는다).

**쓰기는 이 모듈에 없다.** 저장소 네 곳이 같은 규율을 적었고(2026-09-08 "FCP DB
untouched", 2026-09-13 "FCP 라이브러리 DB는 계속 안 건드린다", 2026-09-21 전역 제약
"읽기만 한다", `docs/FCP_MANUAL_ACCEPTANCE.md` "라이브러리 변경 0건"), 프로젝트를
만드는 일은 FCPXML의 `import-options`로 Final Cut에게 맡긴다(D43). 여는 방식도
`immutable=1`이라 잠금도 저널도 건드리지 않는다.

**근거의 한계**: 스키마는 비공개다. 실측 대상은 라이브러리 하나를 Final Cut Pro
**12.3**(`CurrentVersion.plist`의 catalogVersion `1193`)과 **12.4**(`1195; 12.4`)에서
읽은 것뿐이다. 12.4에서도 이 모듈이 읽는 키는 그대로였고, W-16은 클립 시작
(`mediaRange` 시작 = 타임코드 프레임 × 프레임 길이)과 `timecodeDisplayDropFrame`을
더 읽는다. 모르는 구조·빠진 키는 조용한 기본값 대신 `UnsupportedProject`로
거부한다 — 라이브러리가 바뀌면 읽기가 막히는 편이 틀린 값을 흘리는 것보다 낫다.
"""

from __future__ import annotations

import plistlib
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .models import UnsupportedProject

LIBRARY_DB = "CurrentVersion.flexolibrary"
EVENT_DB = "CurrentVersion.fcpevent"
LOCK_INFO = ".lock-info"

# `{(시작),(길이)}` — 두 값 모두 정확한 분수다. 소수 표기는 이 형식이 아니다.
_RANGE = re.compile(r"^\{\((-?\d+)(?:/(\d+))?\),\((-?\d+)(?:/(\d+))?\)\}$")


@dataclass(frozen=True, slots=True)
class LibraryEvent:
    """이벤트 하나 — 이름이 곧 번들 안 디렉터리 이름이다."""

    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class LibraryProject:
    """프로젝트 하나. 이벤트 디렉터리 아래 같은 이름의 디렉터리로 앉는다."""

    name: str
    event: str
    path: Path


@dataclass(frozen=True, slots=True)
class LibraryCatalog:
    bundle: Path
    catalog_version: str
    events: tuple[LibraryEvent, ...]
    projects: tuple[LibraryProject, ...]
    locked: bool
    """Final Cut이 잠금 기록을 남겨 두었다 — 디스크가 메모리보다 뒤처질 수 있다."""


@dataclass(frozen=True, slots=True)
class LibraryMedia:
    """이벤트가 가진 영상 하나. 선언값이며 **증명이 아니다** — 실측과 대조해야 한다."""

    name: str
    original: Path
    proxy: Path | None
    media_identifier: str
    frame_duration: Fraction
    duration: Fraction
    format_name: str
    has_video: bool
    has_audio: bool
    audio_rate: int | None
    audio_channels: int | None
    media_start: Fraction
    """클립 시작(`mediaRange` 시작) — 실물 DJI 원본은 타임코드 프레임 × 프레임 길이다."""
    drop_frame: bool
    """타임코드를 드롭프레임으로 표시한다(`timecodeDisplayDropFrame`)."""

    @property
    def frame_count(self) -> int:
        """선언 길이를 프레임으로. 격자에 떨어지지 않으면 선언이 깨진 것이다."""
        return self._frames(self.duration, "선언 길이")

    @property
    def start_frame(self) -> int:
        """클립 시작을 프레임으로 — 시작 타임코드에서 잰 프레임 수와 대조할 값이다."""
        return self._frames(self.media_start, "클립 시작")

    def _frames(self, value: Fraction, label: str) -> int:
        frames = value / self.frame_duration
        if frames.denominator != 1:
            raise UnsupportedProject(
                str(self.original),
                f"{label}이 프레임 격자에 떨어지지 않는다: {value}초 / "
                f"{self.frame_duration}초 = {frames}",
            )
        return int(frames)


@dataclass(frozen=True, slots=True)
class LibraryClip:
    """프로젝트 타임라인의 클립 하나 — 검증에만 쓴다."""

    name: str
    source_start: Fraction
    duration: Fraction


@dataclass(frozen=True, slots=True)
class LibraryTimeline:
    name: str
    format_name: str
    frame_duration: Fraction
    duration: Fraction
    clips: tuple[LibraryClip, ...]


# ─── 여는 자리 ───────────────────────────────────────────────────────────


class LibraryBusy(UnsupportedProject):
    """Final Cut이 라이브러리를 저장하는 중이다 — 롤백 저널이 데이터베이스 옆에 있다.

    `immutable=1`은 저널을 보지 않으므로 그 사이에 읽으면 반쯤 쓴 쪽을 읽을 수 있다. 기다렸다
    다시 읽는 것은 사람의 몫이다 — 여기서 되풀이해 기다리지 않는다.
    """


JOURNAL_SUFFIX = "-journal"


def _require_quiet(database: Path) -> None:
    if database.with_name(database.name + JOURNAL_SUFFIX).exists():
        raise LibraryBusy(
            str(database),
            "Final Cut이 라이브러리를 저장하는 중이다 — 잠시 뒤 다시 시도하세요. 계속되면"
            " Final Cut에서 라이브러리를 닫았다 다시 열어 저장을 끝낸 뒤 보내세요",
        )


def _rows(database: Path, types: tuple[str, ...]) -> list[tuple[str, bytes]]:
    """`immutable=1`로 열어 (타입, 블롭)을 모은다 — 잠금도 저널도 건드리지 않는다.

    읽기 앞뒤로 롤백 저널을 본다: 그 사이에 Final Cut이 쓰기 시작했으면 읽은 것을 믿지 않는다.
    """
    if not database.is_file():
        raise UnsupportedProject(str(database), "라이브러리 데이터베이스가 없다")
    _require_quiet(database)
    placeholders = ",".join("?" for _ in types)
    query = (
        "select c.ZTYPE, m.ZDICTIONARYDATA from ZCOLLECTION c"
        " join ZCOLLECTIONMD m on m.ZCOLLECTION = c.Z_PK"
        f" where c.ZTYPE in ({placeholders})"
    )
    connection = sqlite3.connect(f"file:{database}?immutable=1", uri=True)
    try:
        rows = connection.execute(query, types).fetchall()
    except sqlite3.DatabaseError as error:
        raise UnsupportedProject(str(database), f"데이터베이스를 읽지 못했다: {error}") from error
    finally:
        connection.close()
    _require_quiet(database)
    for kind, blob in rows:
        if not isinstance(blob, bytes):
            raise UnsupportedProject(str(database), f"{kind} 항목이 블롭이 아니다")
    return [(str(kind), blob) for kind, blob in rows]


def _unarchive(blob: bytes, where: str) -> dict[str, object]:
    """`NSKeyedArchiver` 그래프에서 뿌리 사전을 꺼낸다 — UID를 한 겹만 역참조한다."""
    try:
        raw = plistlib.loads(blob)
    except Exception as error:  # noqa: BLE001 - plistlib이 던지는 종류가 문서화돼 있지 않다
        raise UnsupportedProject(where, f"binary plist를 읽지 못했다: {error}") from error
    objects = raw.get("$objects") if isinstance(raw, dict) else None
    top = raw.get("$top") if isinstance(raw, dict) else None
    if not isinstance(objects, list) or not isinstance(top, dict) or "root" not in top:
        raise UnsupportedProject(where, "NSKeyedArchiver 그래프가 아니다")

    def deref(value: object) -> object:
        return resolve(objects[value.data]) if isinstance(value, plistlib.UID) else value

    def resolve(node: object) -> object:
        if isinstance(node, dict) and "NS.keys" in node:
            return {
                str(deref(key)): deref(value)
                for key, value in zip(node["NS.keys"], node["NS.objects"], strict=True)
            }
        if isinstance(node, dict) and "NS.objects" in node:
            return [deref(item) for item in node["NS.objects"]]
        return node

    root = deref(top["root"])
    if not isinstance(root, dict):
        raise UnsupportedProject(where, "그래프의 뿌리가 사전이 아니다")
    return root


def _text(record: Mapping[str, object], key: str, where: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise UnsupportedProject(where, f"{key}를 문자열로 읽지 못했다: {value!r}")
    return value


def _fraction(record: Mapping[str, object], key: str, where: str) -> Fraction:
    value = record.get(key)
    if not isinstance(value, str):
        raise UnsupportedProject(where, f"{key}를 시각 문자열로 읽지 못했다: {value!r}")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise UnsupportedProject(where, f"{key} 값이 분수가 아니다: {value!r}") from error


def parse_range(value: str, where: str) -> tuple[Fraction, Fraction]:
    """`{(시작),(길이)}`를 분수 둘로. 부동소수를 거치지 않는다."""
    match = _RANGE.match(value)
    if match is None:
        raise UnsupportedProject(where, f"구간 표기가 아니다: {value!r}")
    start_n, start_d, length_n, length_d = match.groups()
    try:
        return (
            Fraction(int(start_n), int(start_d) if start_d else 1),
            Fraction(int(length_n), int(length_d) if length_d else 1),
        )
    except ZeroDivisionError as error:
        # 정규식은 분모 자리에 0도 받는다 — 거기서 죽으면 거부가 아니라 사고다.
        raise UnsupportedProject(where, f"구간의 분모가 0이다: {value!r}") from error


# ─── 카탈로그 ────────────────────────────────────────────────────────────


def read_catalog(bundle: Path) -> LibraryCatalog:
    """번들의 이벤트·프로젝트 목록. 디렉터리가 실제로 있는 것만 돌려준다."""
    if not bundle.is_dir() or bundle.suffix != ".fcpbundle":
        raise UnsupportedProject(str(bundle), "Final Cut 라이브러리(.fcpbundle)가 아니다")
    events: list[LibraryEvent] = []
    project_names: set[str] = set()
    for kind, blob in _rows(bundle / LIBRARY_DB, ("FFEventRecord", "FFSequenceRecord")):
        where = f"{bundle}/{LIBRARY_DB}:{kind}"
        relative = _text(_unarchive(blob, where), "relativePath", where)
        if kind == "FFEventRecord":
            events.append(LibraryEvent(name=relative, path=bundle / relative))
        else:
            project_names.add(relative)
    events.sort(key=lambda event: event.name)
    projects = [
        LibraryProject(name=name, event=event.name, path=event.path / name)
        for event in events
        for name in sorted(project_names)
        if (event.path / name / EVENT_DB).is_file()
    ]
    return LibraryCatalog(
        bundle=bundle,
        catalog_version=_catalog_version(bundle),
        events=tuple(event for event in events if (event.path / EVENT_DB).is_file()),
        projects=tuple(projects),
        locked=(bundle / LOCK_INFO).is_file(),
    )


def _catalog_version(bundle: Path) -> str:
    """`CurrentVersion.plist`의 catalogVersion — 읽은 스키마가 어느 버전인지 남긴다."""
    plist = bundle / "CurrentVersion.plist"
    if not plist.is_file():
        return ""
    with plist.open("rb") as handle:
        parsed = plistlib.load(handle)
    version = parsed.get("catalogVersion") if isinstance(parsed, dict) else None
    return version if isinstance(version, str) else ""


# ─── 이벤트의 영상 ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EventIndex:
    """이벤트의 원본 파일 ↔ 내용 신원 색인 — 행마다 따로 읽는다.

    깨진 행 하나가 이벤트 전체를 막지 않게 하려는 것이다(실물: 12.3 백업의 day1·day3에는
    `videoFormatName`이 NSNull인 기록이 있다). 읽지 못한 행은 `problems`로 돌려주고, 자산
    기록은 아직 풀지 않은 채 들고 있다 — 쓰려는 기록 하나만 `media`가 엄격하게 푼다.
    """

    event: LibraryEvent
    originals: Mapping[str, Path]
    proxies: Mapping[str, Path]
    assets: Mapping[str, Mapping[str, object]]
    problems: tuple[UnsupportedProject, ...]

    def media(self, identity: str) -> LibraryMedia:
        """신원 하나의 선언 — 모르는 구조·빠진 키는 그대로 거부한다(기본값 없음)."""
        where = f"{self.event.path / EVENT_DB}:FFMIORADAsset"
        record = self.assets.get(identity)
        if record is None:
            raise UnsupportedProject(where, f"자산 {identity}의 FFMIORADAsset 기록이 없다")
        return _media(identity, record, self.originals, self.proxies, where)


def index_event(event: LibraryEvent) -> EventIndex:
    """이벤트의 원본 ↔ 신원 색인 — 데이터베이스를 못 읽으면 거부, 행이 깨지면 problems."""
    database = event.path / EVENT_DB
    originals: dict[str, Path] = {}
    proxies: dict[str, Path] = {}
    assets: dict[str, Mapping[str, object]] = {}
    problems: list[UnsupportedProject] = []
    for kind, blob in _rows(database, ("FFMediaRep", "FFMIORADAsset")):
        where = f"{database}:{kind}"
        try:
            record = _unarchive(blob, where)
            if kind == "FFMediaRep":
                _place_rep(record, where, event, originals, proxies)
            else:
                assets[_text(record, "mediaIdentifier", where)] = record
        except UnsupportedProject as error:
            problems.append(error)
    return EventIndex(event, originals, proxies, assets, tuple(problems))


def _place_rep(
    record: Mapping[str, object],
    where: str,
    event: LibraryEvent,
    originals: dict[str, Path],
    proxies: dict[str, Path],
) -> None:
    identity = _text(record, "md5Seed", where)
    relative = _text(record, "projectRelativePath", where)
    rep_type = _text(record, "repType", where)
    if rep_type == "originalMediaRep":
        originals[identity] = event.path / relative
    elif rep_type == "proxyMediaRep":
        proxies[identity] = event.path / relative


def read_event_media(event: LibraryEvent) -> tuple[LibraryMedia, ...]:
    """이벤트가 가진 영상 목록 — 행 하나라도 깨지면 거부한다(엄격). 이름 오름차순.

    `FFMediaRep`(파일)과 `FFMIORADAsset`(시계·포맷)은 서로 다른 객체라 내용 신원으로
    잇는다 — rep의 `md5Seed`와 asset의 `mediaIdentifier`가 같은 값이다. 깨진 행을 건너뛰고
    읽어야 하는 쪽은 `index_event`를 쓴다.
    """
    index = index_event(event)
    if index.problems:
        raise index.problems[0]
    media = [index.media(identity) for identity in index.assets]
    return tuple(sorted(media, key=lambda item: item.name))


def _media(
    identity: str,
    record: Mapping[str, object],
    originals: Mapping[str, Path],
    proxies: Mapping[str, Path],
    where: str,
) -> LibraryMedia:
    original = originals.get(identity)
    if original is None:
        raise UnsupportedProject(where, f"자산 {identity}에 original media-rep이 없다")
    proxy = proxies.get(identity)
    audio_rate = record.get("audioSampleRate")
    audio_channels = record.get("audioChannelCount")
    has_audio = record.get("hasAudio") is True
    return LibraryMedia(
        name=_text(record, "displayName", where),
        original=original,
        proxy=proxy if proxy is not None and proxy.is_file() else None,
        media_identifier=identity,
        frame_duration=_fraction(record, "timecodeFrameDuration", where),
        duration=_fraction(record, "durationForOfflineProvider", where),
        format_name=_text(record, "videoFormatName", where),
        has_video=record.get("hasVideo") is True,
        has_audio=has_audio,
        audio_rate=int(audio_rate) if has_audio and isinstance(audio_rate, (int, float)) else None,
        audio_channels=(
            int(audio_channels) if has_audio and isinstance(audio_channels, int) else None
        ),
        media_start=_media_start(record, where),
        drop_frame=_drop_frame(record, where),
    )


def _media_start(record: Mapping[str, object], where: str) -> Fraction:
    """`mediaRange` 시작 — 길이가 선언 길이와 다르면 어느 쪽이 맞는지 모르니 거부한다."""
    start, length = record_range(record, where)
    declared = _fraction(record, "durationForOfflineProvider", where)
    if length != declared:
        raise UnsupportedProject(
            where, f"mediaRange 길이 {length}초가 선언 길이 {declared}초와 다르다"
        )
    return start


def _drop_frame(record: Mapping[str, object], where: str) -> bool:
    """실물은 정수 1이다. 0/1 밖의 값이나 빠진 키를 False로 읽으면 표시가 조용히 바뀐다."""
    value = record.get("timecodeDisplayDropFrame")
    if isinstance(value, bool) or value in (0, 1):
        return bool(value)
    raise UnsupportedProject(where, f"timecodeDisplayDropFrame을 읽지 못했다: {value!r}")


# ─── 프로젝트 타임라인 (검증용) ──────────────────────────────────────────


def read_project_timeline(project: LibraryProject) -> LibraryTimeline:
    """프로젝트의 시퀀스와 클립 구간. 보낸 것이 들어갔는지 대조하는 데만 쓴다."""
    database = project.path / EVENT_DB
    info: dict[str, object] | None = None
    clips: list[LibraryClip] = []
    for kind, blob in _rows(database, ("FFSequenceInfo", "FFAnchoredCollection")):
        where = f"{database}:{kind}"
        record = _unarchive(blob, where)
        if kind == "FFSequenceInfo":
            info = record
            continue
        clipped = record.get("clippedRange")
        if not isinstance(clipped, str):
            continue  # 프로젝트 자신을 나타내는 collection에는 구간이 없다
        start, duration = parse_range(clipped, where)
        clips.append(
            LibraryClip(
                name=_text(record, "displayName", where),
                source_start=start,
                duration=duration,
            )
        )
    if info is None:
        raise UnsupportedProject(str(database), "FFSequenceInfo가 없다 — 프로젝트가 아니다")
    info_where = f"{database}:FFSequenceInfo"
    media_range = record_range(info, info_where)
    return LibraryTimeline(
        name=project.name,
        format_name=_text(info, "videoFormatKey", info_where),
        frame_duration=_fraction(info, "timecodeFrameDuration", info_where),
        duration=media_range[1],
        clips=tuple(clips),
    )


def record_range(record: Mapping[str, object], where: str) -> tuple[Fraction, Fraction]:
    value = record.get("mediaRange")
    if not isinstance(value, str):
        raise UnsupportedProject(where, f"mediaRange를 읽지 못했다: {value!r}")
    return parse_range(value, where)
