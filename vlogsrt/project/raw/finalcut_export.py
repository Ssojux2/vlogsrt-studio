"""불러온 원본 전부를 Final Cut 타임라인 하나로 보낸다 — 모으고, 대조하고, 쓰는 계층(D56).

문서를 짜는 일은 순수 빌더(`finalcut_timeline`)가 하고, 여기서는 그 입력을 작업공간·원본·
라이브러리에서 **읽어 모으고**, 서로 대조하고, 결과를 `review/finalcut/`에 새 파일로 쓴다.

지키는 선:

- **원본 바이트를 다시 읽지 않는다.** 원본을 열거나 다시 해시하거나 복사하지 않는다(29GB) —
  저장된 `content_sha256`끼리 대조하고, 크기·mtime·inode로 바뀌지 않았음만 확인한다. 검토
  머리가 덮는 원본은 그 머리의 시계(내용 해시에 묶여 있다)를 쓰고 ffprobe로 머리 정보와
  타임코드만 읽는다. 머리가 덮지 않는 원본만 패킷 목록을 훑어 프레임 격자를 잰다 — 이때는
  ffprobe가 파일을 끝까지 읽는다. 원본은 Final Cut 라이브러리의 것으로 남는다.
- **지우지 않으니 관문이 없다.** 미검토 자동 제외도 보낸다(D35는 잘라내는 확정 내보내기의
  관문이다). 끄는 것은 사람이 확정한 제외와 미검토 빨강이다(W-29d, D80) — 꺼진 클립은 키워드와
  함께 타임라인에 남아 Final Cut에서 V로 되살아난다.
- **조용히 바꾸지 않는다.** 원본이 바뀌었거나, 검토 시계·라이브러리 선언·타임코드가 지금
  측정과 어긋나면 그 파일 이름을 대고 전체를 거부한다. 단 하나의 예외는 프레임 격자를 증명하지
  못한 원본(VFR, D68)이다 — 격자 계산 없이는 실을 수 없으므로 그 원본만 빼고 보내며, 이름과
  까닭을 리포트의 `not_exported`와 경고에 남긴다. 보낼 원본이 그것뿐이면 거부한다.
  라이브러리에서 온 원본에 해시 uid를 지어 붙이는 대체 경로는 없다 — 그러면 Final Cut이 같은
  미디어를 둘로 본다. 보낼 원본과 무관한 라이브러리 기록이 깨진 것은 거부하지 않고 리포트에
  경고로 남긴다.
- **덮어쓰지 않는다.** 매번 새 이름이고, 작업공간마다 한 번에 하나만 쓴다(폴더 잠금). 숨은
  초안(`.vlogsrt_….partial`)에 쓰고 fsync한 뒤 검증을 통과한 것만 링크로 제 이름을 얻는다.
  검증에 실패한 문서는 `.invalid.xml`로만 남는다 — 끝난 것처럼 보이지 않는다.

**SRT → 타이틀(W-30f, D91).** `titles_srt`를 주면 같은 전체 타임라인(원본·컷·켜짐·키워드·포맷)에
원본 자막 캡션 대신 그 SRT의 줄마다 Final Cut 타이틀(기본 Basic Title)을 스파인 클립에 붙인다
(`finalcut_titles`) — 원본 자막과 그 표지(D55)·빠진 구간(D59)은 읽지 않는다. 이름에 `_titles`를 달고
리포트의 `titles`에 SRT 이름·sha256·모양·센 수를 남긴다. SRT를 읽고 해시하는 일은 `timeline_srt`가
한다(이 계층은 hashlib을 모른다).

**읽는 쪽 규칙**: 보내기 하나는 `vlogsrt_<UTC>[_titles].fcpxml`이 **있을 때만** 끝난 것이다. 리포트
(`.json`)는 `.fcpxml`보다 먼저 제 이름을 얻으므로, `.fcpxml` 없는 리포트는 중간에 죽은 시도다.
숨은 `.partial` 초안은 다음 보내기가 치운다.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import filelock, log, oscompat, polish, preprocess
from vlogsrt.config import Config
from vlogsrt.project import library
from vlogsrt.project.models import UnsupportedProject
from vlogsrt.project.raw import finalcut_timeline as timeline
from vlogsrt.project.raw import finalcut_titles as titles
from vlogsrt.project.raw import media_refs
from vlogsrt.project.raw.config import (
    FINALCUT_TIMELINE,
    FINALCUT_TITLES,
    FinalCutTitleSettings,
)
from vlogsrt.project.raw.models import (
    INGEST_JOB_KIND,
    UNSUPPORTED_GRID_REASON,
    AnalysisSourceResult,
    RawWorkspaceError,
    SourceAsset,
)
from vlogsrt.project.raw.review_workspace import human_decided
from vlogsrt.project.raw.segment_review import (
    SELECTION_EXCLUDED,
    ReviewRevision,
    SourceClock,
    is_unreviewed_auto_exclusion,
)
from vlogsrt.project.raw.source_transcript import rational
from vlogsrt.project.raw.timeline_srt import TimelineSrt
from vlogsrt.project.timebase import timecode_frames
from vlogsrt.project.xml_validation import (
    XmlValidation,
    XmlValidationUnavailable,
    validate_xml,
)

if TYPE_CHECKING:
    from vlogsrt.project.raw.jobs import JobContext
    from vlogsrt.project.raw.service import RawWorkspaceService
    from vlogsrt.project.raw.workspace import RawWorkspace

INSTALLED_DTD = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework"
    "/Versions/A/Resources/FCPXMLv1_14.dtd"
)
"""이 기기 Final Cut Pro가 가진 FCPXML 1.14 DTD — 있으면 반드시 이것으로 검증한다."""

EXPORT_FOLDER = Path("review") / "finalcut"
REPORT_SCHEMA = "vlogsrt-finalcut-export/v1"
_FCPXML_SUFFIX = ".fcpxml"
_REPORT_SUFFIX = ".json"
_INVALID_SUFFIX = ".invalid.xml"
_DRAFT_SUFFIX = ".partial"
_BUNDLE_SUFFIX = ".fcpbundle"
_TITLES_TAG = "_titles"
"""SRT 타이틀을 단 타임라인의 이름 꼬리 — 같은 폴더에서 원본 자막 보내기와 갈린다(D91)."""
_TITLES_TITLE = "자막 타이틀"
_GREEN_TAG = "_green"
"""꺼진 클립을 뺀 타임라인의 이름 꼬리(D94) — 전체 타임라인과 같은 폴더에서 갈린다."""
_GREEN_TITLE = "추천만"
_DRAFT_NAME = re.compile(
    r"\.vlogsrt_[0-9]{8}T[0-9]{6}Z(?:_titles)?(?:_green)?(?:_[0-9]+)?\.(?:fcpxml|json)\.partial"
)
UNREVIEWED_DROP_TEXT = (
    "꺼진 클립을 빼고 보내려면 확인하지 않은 제외 제안 {count}개를 먼저 정하세요({names}) —"
    " 작업대의 「남은 M개 모두 제외 유지」를 누르거나 그 조각을 사용·제외로 정하면 됩니다."
    " 확인하지 않은 제안을 먼저 영상에서 빼지 않습니다(D35). 아무것도 쓰지 않았습니다."
)
"""이 계층이 만드는 숨은 초안의 이름 — 치울 수 있는 것은 이 꼴뿐이다."""
_IMPORT_JOB_KINDS = frozenset({INGEST_JOB_KIND, "reference_import"})
"""원본 목록을 바꾸는 작업 — 복사 가져오기와 라이브러리 참조 가져오기."""
_BINARY = getattr(os, "O_BINARY", 0)
"""Windows는 텍스트 모드 서술자에서 `\\n`을 `\\r\\n`으로 바꾼다 — 바이트 그대로 쓴다(D100 §1b)."""
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY
_FILE_MODE = 0o644


@dataclass(frozen=True, slots=True)
class CaptionTimingFlags:
    """자막 시각 확인(D55)의 몫 — 키워드를 달 원본과, 왜 나머지는 안 달았는지(보고서 칸).

    서비스가 저장된 보고서를 **읽기만** 해서 만든다 — 이 계층은 확인 계층을 모르고(표시 전용
    불변식), 재지도 않는다. `flagged`만 문서에 들어가고 `section`은 보고서 JSON의 `sync_check`다.
    """

    flagged: frozenset[str]
    section: Mapping[str, object]


CaptionGaps = Mapping[str, Sequence[Mapping[str, str]] | None]
"""원본 → 전사 실패 구간(공통 시계 `start`·`end`·`reason`) — 서비스가 1단계 기록을 읽어 값으로
넘긴다(D59). 이 계층은 확인 계층을 모른다. 값이 None인 원본은 기록이 없어 '모름'이다. 리포트에만
실린다 — 문서(FCPXML) 바이트는 이 값과 무관하다(D56)."""


class FinalCutExportError(RawWorkspaceError):
    """보내지 않았다 — 어느 원본·라이브러리의 무엇이 문제인지 이름으로 말한다."""


class _UnsupportedGrid(FinalCutExportError):
    """그 원본은 프레임 격자를 증명하지 못했다(VFR) — 그 원본만 빼고 보낸다(D68)."""

    def __init__(self, asset: SourceAsset, note: str) -> None:
        super().__init__(f"{asset.source_name}: 프레임 격자가 균일하지 않다(VFR) — {note}")
        self.asset = asset
        self.reason = f"{UNSUPPORTED_GRID_REASON} — {note}"


@dataclass(frozen=True, slots=True)
class ExportOutcome:
    fcpxml: Path
    report: Path
    counts: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Gathered:
    """원본 하나에서 모은 것 — 빌더 입력과 리포트 한 줄."""

    source: timeline.SourceInput
    facts: dict[str, object]
    from_library: bool


@dataclass(frozen=True, slots=True)
class _Timecode:
    frames: int
    drop_frame: bool
    label: preprocess.SourceTimecode | None


@dataclass(frozen=True, slots=True)
class _Grid:
    """원본의 프레임 격자 — 검토 머리의 시계에서, 머리 밖이면 패킷 측정에서."""

    frame_duration: Fraction
    frame_count: int
    deviation_frames: Fraction = Fraction(0)
    """맞춘 원본(D69)의 최대 어긋남을 프레임으로 — 머리 정보·라이브러리 선언 길이가 명목 프레임
    수에서 이만큼 벗어날 수 있다. 균일 원본은 0이라 예전처럼 정확히 같아야 한다. map 원본(D70)은
    한 프레임 미만(`(T−1)/T` — 틱 단위 차이라 1프레임에 닿지 않는다)이다(`_grid_tolerance`)."""
    tail_trim_frames: Fraction = Fraction(0)
    """균일 원본의 편집 목록이 끝에서 자른 몫(프레임, 반 프레임 미만 — D72). 0이면 자르지 않았다."""


def _grid_tolerance(clock: preprocess.RawVideoClock) -> Fraction:
    """머리 정보·라이브러리 선언 길이가 명목 프레임 수에서 벗어날 수 있는 몫(프레임).

    snap(D69)은 잰 최대 어긋남까지(반 프레임 미만). map(D70)은 명목 수가 마지막 패킷의 슬롯에서
    오므로 선언 끝(마지막 패킷 + 마지막 간격)과 한 프레임 안에서 갈린다(실측 0.2–0.5프레임) — 한
    프레임 미만까지 받는다. 한 프레임 이상이면 Final Cut이 다른 프레임 수를 본다. 균일은 0.
    """
    if clock.conform_kind == "map":
        ticks = clock.frame_duration / clock.time_base
        return (ticks - 1) / ticks
    return clock.max_deviation_ticks * clock.time_base / clock.frame_duration


def _refuse(asset: SourceAsset, reason: str) -> FinalCutExportError:
    return FinalCutExportError(f"{asset.source_name}: {reason}")


def _where(error: UnsupportedProject) -> str:
    return f"{error.location}: {error.reason}"


# ── 라이브러리 — 읽기만, immutable ─────────────────────────────────────────


def _owning_bundle(path: Path) -> Path | None:
    return next((parent for parent in path.parents if parent.suffix == _BUNDLE_SUFFIX), None)


_Entry = tuple[library.EventIndex, str]


class _LibraryShelf:
    """작업공간이 기억한 라이브러리의 원본 ↔ 신원 색인 — 처음 쓸 때 한 번 읽는다.

    색인은 너그럽게, 기록은 엄격하게: 모든 이벤트의 원본 파일 ↔ 신원 행을 읽되 깨진 행은
    건너뛰고, 자산 기록은 보낼 원본과 맞는 것만 엄격하게 푼다(실물 12.3 백업의 day1·day3에는
    `videoFormatName`이 NSNull인 기록이 있다 — 그 하나가 모든 보내기를 막으면 안 된다). Final
    Cut의 "제자리에 두기"는 `Original Media`에 바깥 원본을 가리키는 링크만 두고 작업공간은 바깥
    경로를 기록하므로, 라이브러리 안 경로와 링크 대상 둘 다로 찾는다.
    """

    def __init__(self, bundle: Path | None) -> None:
        self.bundle = bundle.resolve() if bundle is not None else None
        self.catalog: library.LibraryCatalog | None = None
        self._indexes: list[library.EventIndex] | None = None
        self._by_path: dict[Path, list[_Entry]] = {}
        self._by_target: dict[Path, list[_Entry]] = {}
        self._unreadable: list[str] = []
        self._used: set[str] = set()

    def media_for(self, asset: SourceAsset, path: Path) -> library.LibraryMedia | None:
        """라이브러리 원본이면 그 선언, 아니면 None — 확인할 수 없으면 거부한다."""
        owner = _owning_bundle(path)
        if owner is not None and (self.bundle is None or owner.resolve() != self.bundle):
            raise _refuse(
                asset, f"기억한 라이브러리({self.bundle}) 밖의 라이브러리 원본이다: {owner}"
            )
        if self.bundle is None or (owner is None and not self.bundle.is_dir()):
            return None
        self._index()
        hits = self._by_path.get(path) or self._by_target.get(path.resolve(), [])
        if len({identity for _, identity in hits}) > 1:
            raise _refuse(asset, f"{self.bundle.name}에 이 원본이 여러 신원으로 있다")
        if not hits:
            self._require_outside(asset, owner)
            return None
        index, identity = hits[0]
        self._used.add(identity)
        try:
            return index.media(identity)
        except UnsupportedProject as error:
            raise _refuse(
                asset,
                f"{self.bundle.name} 라이브러리의 이 원본 기록을 읽지 못했다 — {_where(error)}",
            ) from error

    def _require_outside(self, asset: SourceAsset, owner: Path | None) -> None:
        """기록에 없으면 라이브러리 밖 원본이다 — 단, 라이브러리 안이거나 색인이 덜 읽혔으면
        그렇다고 말할 수 없으니 거부한다."""
        assert self.bundle is not None
        if owner is not None:
            raise _refuse(
                asset,
                f"{self.bundle.name} 기록에 이 원본이 없다 — 라이브러리 원본에 해시 신원을"
                " 지어 붙이지 않는다",
            )
        if self._unreadable or any(index.problems for index in self._indexes or ()):
            raise _refuse(
                asset,
                f"{self.bundle.name}의 기록 일부를 읽지 못해 이 원본이 라이브러리 미디어인지"
                " 확인할 수 없다",
            )

    def _index(self) -> None:
        if self._indexes is not None:
            return
        assert self.bundle is not None
        try:
            self.catalog = library.read_catalog(self.bundle)
        except UnsupportedProject as error:
            raise FinalCutExportError(
                f"{self.bundle.name} 라이브러리를 읽지 못했다 — {_where(error)}"
            ) from error
        indexes: list[library.EventIndex] = []
        for event in self.catalog.events:
            try:
                indexes.append(library.index_event(event))
            except library.LibraryBusy as error:
                raise FinalCutExportError(f"{self.bundle.name}: {error.reason}") from error
            except UnsupportedProject as error:
                self._unreadable.append(_where(error))
        self._indexes = indexes
        for index in indexes:
            for identity, original in index.originals.items():
                self._by_path.setdefault(original, []).append((index, identity))
                self._by_target.setdefault(original.resolve(), []).append((index, identity))

    def warnings(self) -> list[str]:
        """보낼 원본과 무관해서 거부하지 않은 라이브러리 기록 문제 — 리포트에 남긴다."""
        found = list(self._unreadable)
        for index in self._indexes or ():
            found += [_where(problem) for problem in index.problems]
            for identity in index.assets:
                if identity in self._used:
                    continue
                try:
                    index.media(identity)
                except UnsupportedProject as error:
                    name = index.assets[identity].get("displayName")
                    found.append(f"{_where(error)} ({name or '이름 없음'} · {identity})")
        return found

    def facts(self) -> dict[str, object] | None:
        if self.bundle is None or self.catalog is None:
            return None
        return {
            "bundle": str(self.bundle),
            "catalog_version": self.catalog.catalog_version,
            "locked": self.catalog.locked,
            "events": len(self._indexes or ()),
            "warnings": self.warnings(),
        }


# ── 원본 하나 ────────────────────────────────────────────────────────────


def _snapshot(path: Path) -> tuple[int, int, int]:
    facts = path.stat()
    return facts.st_size, facts.st_mtime_ns, facts.st_ino


def _export_path(workspace: RawWorkspace, asset: SourceAsset) -> tuple[Path, tuple[int, int, int]]:
    """원본 경로와 지금의 stat — 저장된 해시끼리, 그리고 stat으로만 바뀌지 않았음을 본다."""
    if asset.content_sha256 is None:
        raise _refuse(asset, "검증된 내용 해시가 없다 — 가져오기를 먼저 끝내세요")
    try:
        path = media_refs.resolve(workspace, asset, "export")
        record = media_refs.load(workspace, asset.asset_id)
        copied = workspace.source_reference_path(asset.asset_id) is None
        before = _snapshot(path)
    except (RawWorkspaceError, OSError) as error:
        raise _refuse(asset, f"원본이 가져올 때와 다르거나 없다 — {error}") from error
    linked = (record or {}).get("original")
    if linked and linked.get("sha256") != asset.content_sha256:
        raise _refuse(asset, "연결된 원본의 기록 해시가 가져온 원본과 다르다 — 다시 연결하세요")
    if copied and not linked and before[0] != asset.size_bytes:
        raise _refuse(asset, f"원본 크기가 가져올 때와 다르다 ({asset.size_bytes} → {before[0]}B)")
    return path, before


def _require_unchanged(asset: SourceAsset, path: Path, before: tuple[int, int, int]) -> None:
    """잰 것과 보낼 것이 같은 파일이어야 한다 — 재는 사이에 바뀌었으면 거부한다."""
    try:
        after = _snapshot(path)
    except OSError as error:
        raise _refuse(asset, f"재는 사이에 원본이 사라졌다 — {error}") from error
    if after != before:
        raise _refuse(asset, f"재는 사이에 원본이 바뀌었다(크기·mtime·inode {before} → {after})")


def _probe(
    asset: SourceAsset, path: Path, review: SourceClock | None
) -> tuple[preprocess.RawMediaProbe, _Grid, _Timecode]:
    """머리 정보·격자·타임코드 — ffprobe 실패는 이름을 댄 거부다."""
    try:
        probe = preprocess.probe_raw_media(path)
        if review is not None:
            grid = _review_grid(asset, review, probe, path)
        else:
            clock = preprocess.probe_extraction_clock(path, probe=probe)
            grid = _measured_grid(asset, clock, probe)
        label = preprocess.probe_timecode(path)
    except FinalCutExportError:
        raise
    except RuntimeError as error:
        raise _refuse(asset, f"원본을 ffprobe로 읽지 못했다 — {error}") from error
    return probe, grid, _timecode(asset, label, grid.frame_duration)


def _measured_grid(
    asset: SourceAsset, clock: preprocess.RawVideoClock, probe: preprocess.RawMediaProbe
) -> _Grid:
    """검토 머리 밖 원본의 격자 — 패킷을 훑어 잰 것만 믿는다. VFR은 프레임 수가 길이가 아니다."""
    if not clock.uniform_grid:
        raise _UnsupportedGrid(asset, clock.grid_note or "")
    deviation = _grid_tolerance(clock)
    trim = _require_plain_timeline(
        asset, clock.first_pts, clock.frame_count, clock.frame_duration, probe, deviation
    )
    return _Grid(clock.frame_duration, clock.frame_count, deviation, trim)


def _require_plain_timeline(
    asset: SourceAsset,
    origin_pts: int,
    frames: int,
    frame_duration: Fraction,
    probe: preprocess.RawMediaProbe,
    deviation_frames: Fraction = Fraction(0),
) -> Fraction:
    """편집 목록(edit list)이 없는 파일이어야 한다 — 패킷 격자와 Final Cut이 보는 길이가 같다.

    스트림 복사로 자른 파일은 첫 프레임이 0보다 앞에 있고 머리 정보의 길이가 실제 프레임보다
    짧다. Final Cut은 머리 정보 쪽(0부터·짧은 길이)으로 보므로 그대로 보내면 모든 컷이 그만큼
    밀린다. 검토를 다시 세워도 같은 시계를 재므로 고칠 곳은 파일이다. 맞춘 원본(D69)의 머리
    정보는 마지막 패킷의 어긋남만큼 명목 길이와 다르다(실측 0–6틱) — 그 몫(`deviation_frames`,
    반 프레임 미만)까지만 받는다. 편집 목록이 자른 길이는 프레임 단위라 이 몫에 숨지 못한다. map
    원본(D70)의 몫은 한 프레임 미만이다 — 한 프레임 자른 편집 목록은 첫 프레임 원점으로도 잡힌다.

    균일 원본은 하나만 더 받는다(D72, 사용자 결정): 시작은 그대로이고 **끝만** 반 프레임 미만
    자른 편집 목록(실측 IMG_8874 — 패킷 190개가 정확히 균일한데 `elst`가 한 틱 짧다). 돌려주는
    값이 그 자른 몫(프레임)이고, 자르지 않았으면 0이다.
    """
    header = _header_frames(probe, frame_duration)
    if origin_pts >= 0 and (header is None or abs(header - frames) <= deviation_frames):
        return Fraction(0)
    trim = _tail_trim(origin_pts, header, frames) if not deviation_frames else None
    if trim is not None:
        return trim
    video = probe.video
    raise _refuse(
        asset,
        f"편집 목록(edit list)이 있는 파일이다 — 첫 프레임이 {origin_pts * video.time_base}초,"
        f" 실제 프레임 수 {frames}, 머리 정보의 프레임 수 {header}. Final Cut은 머리 정보 쪽으로"
        " 보므로 그대로 보내면 컷이 어긋난다. 그 파일을 완전히 다시 인코딩하거나 편집 목록 없이"
        " 다시 저장(re-wrap)한 뒤 다시 불러오세요",
    )


def _tail_trim(origin_pts: int, header: Fraction | None, frames: int) -> Fraction | None:
    """시작이 그대로(원점 0)이고 끝만 반 프레임 미만 자른 편집 목록이면 그 몫(프레임), 아니면 None.

    라이브러리 규칙(`library.nearest_frame`)과 같은 반올림의 말이다 — 머리 정보의 가장 가까운
    프레임이 측정 프레임 수이고, 그 가운데 끝을 자른 쪽만 받는다. 반 프레임 동점은 half-up이면
    측정 수로 가지만 받지 않는다: 사용자 결정이 「반 프레임 미만」이고, 동점에서 Final Cut이 어느
    쪽으로 세는지 모른다. 맞춘 원본(snap·map)은 이 갈래를 타지 않는다 — 그 몫(D69·D70)이 이미
    끝의 어긋남을 맡고, 둘을 더하면 각자가 뜻한 것보다 넓어진다.
    """
    if header is None or origin_pts != 0:
        return None
    trim = frames - header
    return trim if 0 < trim < library.HALF_FRAME else None


def _header_frames(probe: preprocess.RawMediaProbe, frame_duration: Fraction) -> Fraction | None:
    """머리 정보(duration_ts)의 길이를 프레임으로 — 선언이 없으면 None."""
    video = probe.video
    stated = video.duration_ts
    return None if stated is None else Fraction(stated) * video.time_base / frame_duration


def _review_grid(
    asset: SourceAsset, review: SourceClock, probe: preprocess.RawMediaProbe, path: Path
) -> _Grid:
    """검토 머리의 시계 — 내용 해시에 묶여 있고 stat이 같으면 원본을 다시 훑지 않는다.

    머리의 0프레임이 원본 첫 프레임이다(머리를 세울 때 첫 패킷 pts를 원점으로 쟀다). 지금
    선언과의 대조는 값싼 머리 정보로만 한다: 스트림 timebase와 프레임 레이트. 머리 정보의
    길이가 명목 프레임 수와 다를 때만 패킷을 훑어 맞춘 원본(D69)의 어긋남을 잰다 — 균일
    원본은 예전처럼 다시 훑지 않는다. 선언 r_frame_rate가 검토 시계의 프레임 길이와 다르면(D70 —
    명목 T를 패킷 간격에서 다시 골랐다) 그때도 다시 재어 같은 길이인지 본다.
    """
    if review.source_sha256 != asset.content_sha256:
        raise _refuse(asset, "검토 머리가 다른 원본 내용에 묶여 있다 — 검토를 다시 세우세요")
    ticks = review.end_pts - review.origin_pts
    frames = Fraction(ticks, review.frame_ticks)
    duration = review.frame_ticks * review.time_base
    declared = (probe.video.time_base, 1 / probe.video.r_frame_rate)
    clock = None
    if frames.denominator == 1 and review.time_base == declared[0] and duration != declared[1]:
        clock = preprocess.probe_extraction_clock(path, probe=probe)
        # 다시 고른 명목(D70)은 맞춘 시계(snap·map)에서만 나온다 — 균일 시계라면 어긋남이다.
        remapped = clock.uniform_grid and clock.conform_kind != "uniform"
        declared = (clock.time_base, clock.frame_duration) if remapped else declared
    if frames.denominator != 1 or (review.time_base, duration) != declared:
        raise _refuse(
            asset,
            f"검토 시계(timebase·프레임 길이) {(review.time_base, duration)}가 지금 원본 선언"
            f" {declared}과 다르다 — 검토를 다시 세우세요",
        )
    deviation = Fraction(0)
    if _header_frames(probe, duration) not in (None, frames):
        clock = clock or preprocess.probe_extraction_clock(path, probe=probe)
        deviation = _grid_tolerance(clock)
    trim = _require_plain_timeline(
        asset, review.origin_pts, frames.numerator, duration, probe, deviation
    )
    return _Grid(duration, frames.numerator, deviation, trim)


def _timecode(
    asset: SourceAsset, label: preprocess.SourceTimecode | None, frame_duration: Fraction
) -> _Timecode:
    """시작 타임코드 → 영상 프레임 수. 트랙 레이트가 영상과 다르면 프레임 수가 틀린다 — 거부."""
    if label is None:
        return _Timecode(0, False, None)
    if 1 / label.rate != frame_duration:
        raise _refuse(
            asset,
            f"타임코드 레이트 {label.rate}fps가 영상 프레임 레이트 {1 / frame_duration}fps와"
            " 다르다 — 클립 시작을 정할 수 없다",
        )
    try:
        frames, drop = timecode_frames(label.text, label.rate)
    except ValueError as error:
        raise _refuse(asset, f"시작 타임코드를 읽지 못했다 — {error}") from error
    return _Timecode(frames, drop, label)


def _declared_frames(declared: library.LibraryMedia, grid: _Grid) -> int:
    """라이브러리 선언 길이를 프레임으로 — 맞춘 원본(D69)만 어긋남 몫까지 명목 수로 읽는다.

    Final Cut은 지터 원본의 길이를 실제 마지막 패킷까지로 적는다(실측 848.05프레임). 같은
    프레임 길이이고 그 차가 측정한 어긋남(반 프레임 미만) 안이면 명목 수와 같은 원본이다. 그
    밖은 예전 그대로 격자에 떨어져야 한다(`LibraryMedia.frame_count`가 거부한다).

    12.4의 `FFAsset`(D72)은 길이가 영상 머리 정보의 실제 길이라 균일 원본도 격자에 떨어지지 않는다
    (실측 8874 189.95 대 190). 그 선언의 프레임 수는 가장 가까운 프레임(half-up)이고, 측정과
    **정확히** 같아야 한다 — 어긋남 몫(D69 snap 반 프레임 미만 · D70 map 한 프레임 미만)을 더
    얹지 않는다. 받는 창은 시계 종류와 무관하게 [n − ½, n + ½)이다(실측 34개 모두 이 창 안).
    """
    if declared.container_length:
        return declared.frame_count
    if grid.deviation_frames and declared.frame_duration == grid.frame_duration:
        frames = declared.duration / declared.frame_duration
        if abs(frames - grid.frame_count) <= grid.deviation_frames:
            return grid.frame_count
    return declared.frame_count


def _check_library(
    asset: SourceAsset, declared: library.LibraryMedia, grid: _Grid, code: _Timecode
) -> None:
    """라이브러리 선언은 증명이 아니다 — 프레임 길이·수·시작이 같아야 그 신원을 쓴다."""
    try:
        observed = (
            declared.frame_duration,
            _declared_frames(declared, grid),
            declared.start_frame,
        )
    except UnsupportedProject as error:
        raise _refuse(asset, f"라이브러리 선언이 프레임 격자 밖이다 — {error.reason}") from error
    measured = (grid.frame_duration, grid.frame_count, code.frames)
    if observed != measured:
        raise _refuse(
            asset,
            "라이브러리 선언(프레임 길이·프레임 수·시작 프레임)"
            f" {observed}이 지금 측정 {measured}과 다르다",
        )
    if code.label is not None and declared.drop_frame != code.drop_frame:
        raise _refuse(asset, "라이브러리의 드롭프레임 표시가 원본 타임코드와 다르다")


def _review_segments(
    asset: SourceAsset, head: ReviewRevision | None, review: SourceClock | None
) -> tuple[timeline.SegmentState, ...] | None:
    """검토 머리의 조각 → 프레임 구간(원점 0)과 사용/제외·미검토·사람 결정."""
    if head is None or review is None:
        return None
    rows = sorted(
        (s for s in head.segments if s.source_id == asset.asset_id), key=lambda s: s.start_pts
    )
    return tuple(
        timeline.SegmentState(
            start_frame=_frame(asset, review, row.start_pts),
            end_frame=_frame(asset, review, row.end_pts),
            excluded=row.selection == SELECTION_EXCLUDED,
            unreviewed=is_unreviewed_auto_exclusion(row),
            decided=human_decided(row),
        )
        for row in rows
    )


def _frame(asset: SourceAsset, review: SourceClock, pts: int) -> int:
    frames, remainder = divmod(pts - review.origin_pts, review.frame_ticks)
    if remainder:
        raise _refuse(asset, f"검토 경계 pts {pts}가 프레임 격자 밖이다")
    return frames


def _cues(
    service: RawWorkspaceService, root: Path, asset: SourceAsset
) -> tuple[timeline.Cue, ...] | None:
    """사람이 고친 최신 원본 자막 — 다른 원본 내용에 묶인 자막은 쓰지 않고 거부한다."""
    document = service.source_transcript(root, asset.asset_id)
    if document is None:
        return None
    if document.get("source_sha256") != asset.content_sha256:
        raise _refuse(asset, "원본 자막이 다른 원본 내용에 묶여 있다")
    return tuple(
        timeline.Cue(rational(row["start"]), rational(row["end"]), caption_text(str(row["text"])))
        for row in document.get("utterances", [])
    )


def caption_text(text: str) -> str:
    """Final Cut으로 보내는 자막 글자 — 줄마다 조판 정리(D30·D96): 앞뒤·겹친 공백을 하나로, 온점을
    지우고(숫자 사이는 그대로), 말줄임표를 `…`로. 원본 자막(작업공간 DB)은 그대로 두고 보내는 글자만
    다듬는다 — 지문·캐시 열쇠·편집기가 그 글자에 묶여 있다. 다 지워져 비면 원문 그대로다(규칙 9)."""
    config = Config()
    lines = [polish.polish_text(line, config) for line in text.splitlines()]
    sent = "\n".join(line for line in lines if line)
    return sent or text


def _display_geometry(video: preprocess.RawVideoMedia) -> tuple[int, int]:
    """재생 화면의 크기 — 90°·270° 회전이면 저장된 가로·세로가 뒤바뀐다. 가져오기 조사가 기록하는
    크기와 한 규칙이다(`RawVideoMedia.display_size`, W-26)."""
    return video.display_size


@dataclass(frozen=True, slots=True)
class _Scope:
    """한 번의 보내기가 모든 원본에 같이 쓰는 재료."""

    service: RawWorkspaceService
    root: Path
    workspace: RawWorkspace
    head: ReviewRevision | None
    evidence: Mapping[str, AnalysisSourceResult]
    shelf: _LibraryShelf
    caption_gaps: CaptionGaps | None = None
    original_captions: bool = True
    """원본 자막을 실을까 — SRT 타이틀로 보낼 때는 읽지도 않는다(D91)."""


def _gather(scope: _Scope, asset: SourceAsset) -> _Gathered:
    path, before = _export_path(scope.workspace, asset)
    review = scope.head.clock_for(asset.asset_id) if scope.head is not None else None
    probe, grid, code = _probe(asset, path, review)
    _require_unchanged(asset, path, before)
    declared = scope.shelf.media_for(asset, path)
    if declared is not None:
        _check_library(asset, declared, grid, code)
    row = scope.evidence.get(asset.asset_id)
    report = scope.service.tier_report(scope.root, row.revision_id, asset.asset_id) if row else None
    source = timeline.SourceInput(
        media=_media(asset, path, probe, grid, code, declared),
        segments=_review_segments(asset, scope.head, review),
        report=report,
        cues=_cues(scope.service, scope.root, asset) if scope.original_captions else None,
    )
    facts = _source_facts(
        asset, source, code, row if report is not None else None, scope.caption_gaps
    )
    facts["tail_trim_frames"] = str(grid.tail_trim_frames)
    return _Gathered(source, facts, from_library=declared is not None)


def _media(
    asset: SourceAsset,
    path: Path,
    probe: preprocess.RawMediaProbe,
    grid: _Grid,
    code: _Timecode,
    declared: library.LibraryMedia | None,
) -> timeline.SourceMedia:
    video = probe.video
    width, height = _display_geometry(video)
    audio = probe.audio[0] if probe.audio else None
    assert asset.content_sha256 is not None  # _export_path가 확인했다
    return timeline.SourceMedia(
        source_id=asset.asset_id,
        name=declared.name if declared is not None else path.stem,
        path=path,
        content_sha256=asset.content_sha256,
        media_identifier=declared.media_identifier if declared is not None else None,
        frame_duration=grid.frame_duration,
        frame_count=grid.frame_count,
        timecode_frames=code.frames,
        drop_frame=_drop_frame(code, declared),
        width=width,
        height=height,
        color=(video.color_primaries, video.color_transfer, video.color_matrix),
        audio_channels=audio.channels if audio is not None else None,
        audio_rate=audio.sample_rate if audio is not None else None,
    )


def _drop_frame(code: _Timecode, declared: library.LibraryMedia | None) -> bool:
    """타임코드가 있으면 그 표기(`;`)가, 없으면 라이브러리의 표시 설정이 정한다."""
    if code.label is not None or declared is None:
        return code.drop_frame
    return declared.drop_frame


def _source_facts(
    asset: SourceAsset,
    source: timeline.SourceInput,
    code: _Timecode,
    row: AnalysisSourceResult | None,
    gaps: CaptionGaps | None,
) -> dict[str, object]:
    """리포트 한 줄 — 자막은 정직하게: 실은 큐 수와 전사 실패 구간까지(D59).

    `captions`는 실제로 실은 큐가 있는가다. 예전에는 원본 자막 문서가 있는가였다 — 큐 0개인
    0008이 `true`로 적혔다. `caption_gaps`는 서비스가 넘긴 값 그대로이고, 넘기지 않았으면 None
    (모름)이다. 문서(FCPXML)는 이 칸들과 무관하다.
    """
    media = source.media
    label = code.label
    cues = source.cues or ()
    return {
        "asset_id": asset.asset_id,
        "name": media.name,
        "path": str(media.path),
        "content_sha256": media.content_sha256,
        "size_bytes": asset.size_bytes,
        "uid": timeline.asset_identity(media)[0],
        "timecode": None if label is None else {"text": label.text, "rate": str(label.rate)},
        "frames": media.frame_count,
        # 프로젝트 레이트와 다르면 Final Cut이 시간을 지키며 맞춰 재생한다(D71) — 어느 원본인지.
        "frame_duration": str(media.frame_duration),
        "segments": None if source.segments is None else len(source.segments),
        "analysis_revision": None if row is None else row.revision_id,
        "captions": bool(cues),
        "caption_count": len(cues),
        "caption_gaps": _gaps_of(gaps, asset.asset_id),
    }


def _gaps_of(gaps: CaptionGaps | None, asset_id: str) -> list[dict[str, str]] | None:
    if gaps is None:
        return None
    spans = gaps.get(asset_id)
    return None if spans is None else [dict(span) for span in spans]


# ── 모으기 ───────────────────────────────────────────────────────────────


def _load_head(facade: Any, assets: tuple[SourceAsset, ...]) -> ReviewRevision | None:
    """검토 머리 — 보낼 원본이 낡았다고 판정되면 그 파일 이름을 대고 거부한다."""
    if not facade.has_review():
        return None
    loaded = facade.load_head()
    names = {asset.asset_id: asset.source_name for asset in assets}
    stale = [f"{names[source]}: {why}" for source, why in loaded.stale_sources if source in names]
    if stale:
        raise FinalCutExportError("검토 머리의 원본이 낡았다 — " + " · ".join(stale))
    revision: ReviewRevision = loaded.revision
    return revision


def _require_no_import(service: RawWorkspaceService, root: Path) -> None:
    """가져오기가 도는 동안의 원본 목록은 아직 끝나지 않았다 — 반쪽 타임라인을 보내지 않는다.

    작업 기록이 진행 중인 채 남는 일도 있다(앱이 꺼졌다 켜졌는데 그 작업공간이 복구되지 않음).
    오래됐다고 지어 넘기지 않는다 — 어느 작업인지·언제 시작했는지와 푸는 길을 말한다.
    """
    running = service.active_jobs(root, _IMPORT_JOB_KINDS)
    if running:
        listed = ", ".join(f"{job.job_id}({job.kind}, {job.created_at} 시작)" for job in running)
        raise FinalCutExportError(
            f"영상 가져오기 작업이 아직 진행 중으로 기록돼 있다: {listed} — 끝난 뒤 다시 보내세요."
            " 가져오기가 실제로 돌고 있지 않은데 이 안내가 계속되면, 앱을 다시 시작하면 중단된"
            " 작업 기록이 정리된다"
        )


def _not_exported(
    service: RawWorkspaceService,
    root: Path,
    head: ReviewRevision | None,
    exported: set[str],
    reasons: Mapping[str, str],
) -> list[dict[str, str]]:
    """타임라인에 오르지 못한 것 — 조용히 빠지지 않게 리포트와 경고로 남긴다.

    `reasons`는 모으기가 뺀 원본의 까닭이다(VFR, D68). 그 밖의 원본은 가져오기 상태가 까닭이다.
    """
    rows = [
        {
            "asset_id": a.asset_id,
            "name": a.source_name,
            "reason": reasons.get(a.asset_id, f"상태 {a.status}"),
        }
        for a in service.list_source_assets(root)
        if a.asset_id not in exported
    ]
    known = {row["asset_id"] for row in rows} | exported
    rows += [
        {"asset_id": clock.source_id, "name": "", "reason": "검토 머리에만 있다"}
        for clock in (head.clocks if head is not None else ())
        if clock.source_id not in known
    ]
    for row in rows:
        log.warn(
            f"Final Cut 타임라인에 넣지 않았다: {row['name'] or row['asset_id']} — {row['reason']}"
        )
    return rows


def _gather_all(
    scope: _Scope, assets: tuple[SourceAsset, ...], context: JobContext | None
) -> tuple[list[_Gathered], dict[str, str]]:
    """원본마다 모은다 — 문제는 끝까지 다 모아 한 번에 이름을 대고 거부한다.

    격자를 증명하지 못한 원본(VFR)은 문제가 아니라 뺄 원본이다(D68) — (모은 것, 뺀 원본 → 까닭).
    보낼 것이 그것들뿐이면 거부한다(빈 타임라인을 보낸 성공으로 꾸미지 않는다)."""
    gathered: list[_Gathered] = []
    problems: list[str] = []
    skipped: dict[str, _UnsupportedGrid] = {}
    if context is not None:
        context.set_total(len(assets))
    for asset in assets:
        if context is not None:
            context.check_cancel()
        try:
            gathered.append(_gather(scope, asset))
        except _UnsupportedGrid as error:
            skipped[asset.asset_id] = error
        except FinalCutExportError as error:
            problems.append(str(error))
        if context is not None:
            context.advance()
    if not problems and not gathered:
        problems = [str(error) for error in skipped.values()]
    if problems:
        raise FinalCutExportError(
            f"Final Cut으로 보내지 않았다 — 원본 {len(problems)}개: " + " / ".join(problems)
        )
    return gathered, {asset_id: error.reason for asset_id, error in skipped.items()}


# ── 쓰기 ─────────────────────────────────────────────────────────────────


@contextmanager
def _exclusive(folder: Path) -> Iterator[None]:
    """작업공간마다 보내기는 한 번에 하나 — 폴더 잠금이고, 기다리지 않는다.

    POSIX는 폴더 자체를 flock하고, Windows는 폴더 안의 숨은 잠금 파일을 잠근다(D100 §1).
    """
    descriptor = filelock.open_directory_lock(folder)
    try:
        try:
            filelock.lock_exclusive(descriptor, blocking=False)
        except BlockingIOError as error:
            raise FinalCutExportError(
                "이 작업공간에서 다른 Final Cut 보내기가 진행 중이다 — 끝난 뒤 다시 보내세요"
            ) from error
        except OSError as error:
            raise FinalCutExportError(
                f"{folder}에 파일 잠금을 걸 수 없다(네트워크 드라이브 등) — 작업공간을 이 Mac의"
                f" 디스크에 두고 다시 보내세요 ({error})"
            ) from error
        try:
            yield
        finally:
            filelock.unlock(descriptor)  # 닫기 전에 푼다(Windows — D100 §1b)
    finally:
        os.close(descriptor)


def _sweep_drafts(folder: Path) -> None:
    """지난 시도가 남긴 숨은 초안만 지운다 — 잠금 안이므로 지금 쓰는 초안은 없다."""
    for entry in folder.iterdir():
        if _DRAFT_NAME.fullmatch(entry.name) and not entry.is_symlink() and entry.is_file():
            entry.unlink()


def _sync_folder(folder: Path) -> None:
    oscompat.fsync_directory(folder)


def _write(descriptor: int, payload: bytes) -> None:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _taken(folder: Path, stem: str) -> bool:
    names = (_FCPXML_SUFFIX, _REPORT_SUFFIX, _INVALID_SUFFIX)
    return any((folder / f"{stem}{suffix}").exists() for suffix in names)


def _reserve(folder: Path, moment: datetime, tag: str = "") -> tuple[str, Path]:
    """새 이름 하나를 잡는다 — 숨은 초안을 O_EXCL로 만들고, 그 사이 이름이 생겼으면 다음 번호."""
    base = f"vlogsrt_{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}{tag}"
    for attempt in itertools.count(1):
        stem = base if attempt == 1 else f"{base}_{attempt}"
        if _taken(folder, stem):
            continue
        draft = folder / f".{stem}{_FCPXML_SUFFIX}{_DRAFT_SUFFIX}"
        try:
            os.close(os.open(draft, _NEW_FILE, _FILE_MODE))
        except FileExistsError:
            continue
        if _taken(folder, stem):
            draft.unlink()
            continue
        return stem, draft
    raise AssertionError("unreachable")


def _settle(draft: Path, final: Path) -> None:
    """초안을 제 이름으로 — 링크는 대상이 있으면 실패하므로 덮어쓰지 않는다."""
    try:
        os.link(draft, final)
    except FileExistsError as error:
        draft.unlink()
        raise FinalCutExportError(f"{final.name}이 이미 있다 — 덮어쓰지 않는다") from error
    draft.unlink()
    _sync_folder(final.parent)


def _validate(draft: Path, dtd_path: Path) -> XmlValidation:
    try:
        return validate_xml(draft, dtd_path=dtd_path if dtd_path.is_file() else None)
    except XmlValidationUnavailable as error:
        draft.unlink(missing_ok=True)
        raise FinalCutExportError(f"FCPXML을 검증할 수 없어 보내지 않는다 — {error}") from error


def _reject(folder: Path, stem: str, draft: Path, validation: XmlValidation) -> None:
    kept = folder / f"{stem}{_INVALID_SUFFIX}"
    _settle(draft, kept)
    raise FinalCutExportError(
        "FCPXML이 Final Cut DTD를 통과하지 못해 보내지 않는다 — "
        + " · ".join(validation.diagnostics)
        + f" (검사한 문서: {kept})"
    )


def _publish(
    folder: Path,
    payload: bytes,
    report: dict[str, object],
    moment: datetime,
    dtd_path: Path,
    tag: str = "",
) -> ExportOutcome:
    """초안 → fsync → 검증 → 리포트 → `.fcpxml` 순서로 제 이름을 준다(읽는 쪽 규칙 참고)."""
    _sweep_drafts(folder)
    stem, draft = _reserve(folder, moment, tag)
    _write(os.open(draft, os.O_WRONLY | os.O_TRUNC | _BINARY), payload)
    validation = _validate(draft, dtd_path)
    if not validation.valid:
        _reject(folder, stem, draft, validation)
    fcpxml = folder / f"{stem}{_FCPXML_SUFFIX}"
    report = {**report, "fcpxml": fcpxml.name, "validation": _validation(validation, dtd_path)}
    report_path = folder / f"{stem}{_REPORT_SUFFIX}"
    report_draft = folder / f".{stem}{_REPORT_SUFFIX}{_DRAFT_SUFFIX}"
    encoded = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    _write(os.open(report_draft, _NEW_FILE, _FILE_MODE), encoded)
    _settle(report_draft, report_path)
    _settle(draft, fcpxml)
    counts = report["counts"]
    assert isinstance(counts, dict)
    return ExportOutcome(fcpxml=fcpxml, report=report_path, counts=counts)


def _validation(validation: XmlValidation, dtd_path: Path) -> dict[str, object]:
    return {
        "valid": validation.valid,
        "dtd_checked": validation.dtd_checked,
        "dtd": str(dtd_path) if validation.dtd_checked else None,
    }


# ── 입구 ─────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(UTC)


def export(
    service: RawWorkspaceService,
    root: Path,
    context: JobContext | None = None,
    *,
    now: Callable[[], datetime] = _utc_now,
    dtd_path: Path = INSTALLED_DTD,
    caption_timing: CaptionTimingFlags | None = None,
    caption_gaps: CaptionGaps | None = None,
    titles_srt: TimelineSrt | None = None,
    title_settings: FinalCutTitleSettings = FINALCUT_TITLES,
    drop_disabled: bool = False,
) -> ExportOutcome:
    """ready 원본 전부(촬영 순) → 새 FCPXML + 리포트. Final Cut은 열지 않는다(D55 표지는 값으로).

    `titles_srt`(서비스가 읽은 SRT)면 원본 자막 캡션 대신 그 줄마다 타이틀을 단다(D91) — 원본
    자막의 표지·빠진 구간은 그 자막의 것이라 받아도 쓰지 않는다. `drop_disabled`면 꺼진 클립을
    프로젝트에서 뺀다(D94 — 원본 전체는 브라우저 클립으로 남는다).
    """
    if titles_srt is not None:
        caption_timing, caption_gaps = None, None
    facade = service.review_workspace_facade(root)
    workspace: RawWorkspace = facade.workspace
    folder = workspace.root / EXPORT_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    with _exclusive(folder):
        moment = now()
        _require_no_import(service, root)
        assets = service.global_order_proposal(root)
        if not assets:
            raise FinalCutExportError("보낼 원본이 없다 — 가져오기를 먼저 끝내세요")
        head = _load_head(facade, assets)
        shelf = _LibraryShelf(service.remembered_fcp_library(root))
        rows = facade.tier_evidence_rows()
        scope = _Scope(
            service, workspace.root, workspace, head, rows, shelf, caption_gaps, titles_srt is None
        )
        gathered, skipped = _gather_all(scope, assets, context)
        document = _document(
            gathered, workspace, moment, shelf, caption_timing, titles_srt, drop_disabled
        )
        section: dict[str, object] | None = None
        if titles_srt is not None:
            document, section = _with_titles(document, titles_srt, title_settings)
        exported = {asset.asset_id for asset in assets} - set(skipped)
        report: dict[str, object] = {
            "schema": REPORT_SCHEMA,
            "created_at": moment.astimezone(UTC).isoformat(),
            "title": document.title,
            "library": shelf.facts(),
            "review_head": None if head is None else head.revision_id,
            "sources": [item.facts for item in gathered],
            "not_exported": _not_exported(service, root, head, exported, skipped),
            "counts": _report_counts(document, gathered),
            "mode": _mode_section(service, root, document),
            **({} if caption_timing is None else {"sync_check": dict(caption_timing.section)}),
            **({} if section is None else {"titles": section}),
        }
        tag = ("" if titles_srt is None else _TITLES_TAG) + (_GREEN_TAG if drop_disabled else "")
        return _publish(folder, timeline.render(document), report, moment, dtd_path, tag)


def _mode_section(
    service: RawWorkspaceService, root: Path, document: timeline.TimelineDocument
) -> dict[str, object]:
    """작업공간 모드(D97)와 이 문서의 방향 — 모드는 문서를 바꾸지 않고 다르면 메모만 남긴다."""
    decision = service.workspace_mode(root)
    orientation = timeline.document_counts(document)["orientation"]
    return {
        "mode": decision.mode,
        "basis": decision.basis,
        "landscape_sources": decision.landscape,
        "portrait_sources": decision.portrait,
        "unknown_sources": decision.unknown,
        "timeline_orientation": orientation,
        "matches_timeline": decision.mode == orientation,
    }


def _with_titles(
    document: timeline.TimelineDocument, srt: TimelineSrt, settings: FinalCutTitleSettings
) -> tuple[timeline.TimelineDocument, dict[str, object]]:
    """같은 문서에 SRT 줄의 타이틀만 — 리포트 칸에는 파일·템플릿·모양·센 수와 마지막 줄의 끝(다른
    타임라인에서 나온 SRT면 타임라인 길이와 어긋나 보인다)."""
    cues = tuple(replace(cue, text=caption_text(cue.text)) for cue in srt.cues)
    try:
        titled, counts = titles.with_srt_titles(document, cues, settings)
    except timeline.FinalCutTimelineError as error:
        raise FinalCutExportError(
            f"SRT `{srt.path.name}`을 타이틀로 달지 못했다 — {error}"
        ) from error
    last_end = max((cue.end for cue in srt.cues), default=None)
    return titled, {
        "srt": {"name": srt.path.name, "path": str(srt.path), "sha256": srt.sha256},
        "effect": {"name": settings.effect_name, "uid": settings.effect_uid},
        "style": titles.style_facts(titled),
        "counts": {
            **asdict(counts),
            "last_cue_end_seconds": None if last_end is None else str(last_end),
        },
    }


def _report_counts(
    document: timeline.TimelineDocument, gathered: list[_Gathered]
) -> dict[str, object]:
    """빌더의 요약 + 큐 0개인 원본(D59) — 자막의 두 가지 「없음」을 따로 센다.

    `sources_without_captions`(빌더)는 원본 자막 **문서가 없는** 원본이다(분석 전 등).
    `sources_with_empty_captions`는 문서는 있지만 **큐가 0개**인 원본이다(실측 0008). 둘 다 원본
    줄의 `captions`가 false다. 문서(FCPXML)는 이 요약과 무관하다.
    """
    counts = timeline.document_counts(document)
    counts["sources_with_empty_captions"] = [
        item.source.media.name
        for item in gathered
        if item.source.cues is not None and not item.source.cues
    ]
    counts["tail_trimmed_sources"] = _tail_trimmed(gathered)
    return counts


def _tail_trimmed(gathered: list[_Gathered]) -> list[dict[str, str]]:
    """끝만 반 프레임 미만 자른 편집 목록으로 받은 원본(D72) — 이름과 자른 몫(프레임·초)."""
    rows: list[dict[str, str]] = []
    for item in gathered:
        trim = Fraction(str(item.facts["tail_trim_frames"]))
        if trim:
            media = item.source.media
            seconds = trim * media.frame_duration
            rows.append({"name": media.name, "frames": str(trim), "seconds": str(seconds)})
    return rows


def _document(
    gathered: list[_Gathered],
    workspace: RawWorkspace,
    moment: datetime,
    shelf: _LibraryShelf,
    caption_timing: CaptionTimingFlags | None = None,
    titles_srt: TimelineSrt | None = None,
    drop_disabled: bool = False,
) -> timeline.TimelineDocument:
    """모은 것 → 문서. 자막 시각 확인(D55)이 걸린 원본에는 빌더가 키워드를 단다. 프로젝트 방향은
    설정(`FINALCUT_TIMELINE`)의 규칙으로 빌더가 고른다 — 켠 길이가 많은 쪽(D66)."""
    bundle = shelf.bundle if any(item.from_library for item in gathered) else None
    flagged = frozenset() if caption_timing is None else caption_timing.flagged
    tags = (
        *(() if titles_srt is None else (_TITLES_TITLE,)),
        *((_GREEN_TITLE,) if drop_disabled else ()),
    )
    try:
        return timeline.plan(
            [item.source for item in gathered],
            title=timeline.export_title(
                workspace.name, moment.astimezone(), " · ".join(tags) if tags else None
            ),
            library_bundle=bundle,
            caption_timing=flagged,
            settings=replace(FINALCUT_TIMELINE, drop_disabled=drop_disabled),
        )
    except timeline.UnreviewedDropError as error:
        names = ", ".join(error.names)
        raise FinalCutExportError(
            UNREVIEWED_DROP_TEXT.format(count=error.count, names=names)
        ) from error
    except timeline.FinalCutTimelineError as error:
        raise FinalCutExportError(f"Final Cut 타임라인을 짜지 못했다 — {error}") from error
