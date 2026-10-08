"""Final Cut 라이브러리(.fcpbundle) → 작업공간 — 이벤트 미디어 발견·논리 클립(읽기 전용).

라이브러리 안은 **읽기만 한다**. 복사·검증은 기존 A2 파이프라인
(`ingest.run_ingest_copy`)이 그대로 하므로 이 모듈은 발견·역할 선택·중복 제거만
담는다:

- **관리 작업공간**: 라이브러리와 같은 층의 `<이름>-vlogsrt`(라이브러리 **밖**).
  같은 라이브러리는 언제나 같은 자리를 얻는다(안정 경로).
- **이벤트 미디어**: 이벤트 폴더는 **임의 이름**에 `CurrentVersion.fcpevent`
  표지가 있는 한 층 디렉터리다(접미어 규칙 같은 건 없다 — 실제 규격). 그 안의
  `Original Media/`·`Transcoded Media/Proxy Media/`만 본다.
- **외부 링크 원본**: 그 안의 심볼릭 링크(파일 링크·통째 폴더 링크)는 정당한
  외부 미디어로 따라간다 — 대상이 살아 있고 일반 파일이고 미디어 확장자일 때만.
  대상이 없으면 **명시적인 끊어진 링크 기록**이 되고 조용히 사라지지 않는다.
- **논리 클립**: 같은 (이벤트, 이름줄기)의 원본·프록시는 한 클립이다. 시간순
  작업에는 선택한 역할 하나만 들어가고, 다른 쪽 파일도 기록에 남는다(지우지
  않는다). 역할은 명시적으로 고른다(기본 원본).
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

from vlogsrt.project.raw import source_order
from vlogsrt.project.raw.ingest import (
    PROTECTED_SOURCE_NAMES,
    IngestProposal,
    MediaInspect,
    RejectedSource,
    SourceCandidate,
    SourceInspection,
    SourceScan,
    ffprobe_media_inspector,
    space_estimate,
)
from vlogsrt.project.raw.models import (
    MEDIA_SUFFIXES,
    IngestError,
    SourceNestingError,
)

__all__ = [
    "FCP_ROLE_ORIGINAL",
    "FCP_ROLE_PROXY",
    "FCP_ROLES",
    "FcpBrokenLink",
    "FcpLibraryError",
    "FcpLibraryScan",
    "FcpLogicalClip",
    "FcpMediaFile",
    "is_fcp_library",
    "managed_workspace_root",
    "scan_fcp_library",
]

FCP_LIBRARY_SUFFIX = ".fcpbundle"
"""Final Cut 라이브러리 번들 확장자 — 디렉터리(package)다."""

FCP_EVENT_MARKER = "CurrentVersion.fcpevent"
"""이벤트 디렉터리 표지 — 실제 이벤트 폴더 이름은 임의(예: 날짜)고 이 표지가 있다."""

ORIGINAL_MEDIA_DIR = "Original Media"
"""이벤트 안 원본 미디어 폴더 이름."""

TRANSCODED_MEDIA_DIR = "Transcoded Media"
"""이벤트 안 트랜스코딩 미디어 폴더 — 그 아래 Proxy Media·High Quality가 산다."""

PROXY_MEDIA_DIR = "Proxy Media"
"""프록시 미디어 폴더 이름 — 반드시 Transcoded Media 아래만 프록시로 본다."""

MANAGED_WORKSPACE_SUFFIX = "-vlogsrt"
"""관리 작업공간 접미어 — `<라이브러리 이름>-vlogsrt`가 라이브러리 밖 안정 자리다."""

FCP_ROLE_ORIGINAL = "original"
FCP_ROLE_PROXY = "proxy"
FCP_ROLES = (FCP_ROLE_ORIGINAL, FCP_ROLE_PROXY)
"""논리 클립에서 시간순 작업으로 가져갈 역할 — 명시적 선택(기본 원본)."""


class FcpLibraryError(IngestError):
    """라이브러리 발견 계열의 오류 — 사람에게 그대로 보인다."""


@dataclass(frozen=True, slots=True)
class FcpMediaFile:
    """이벤트 미디어 파일 하나 — 읽을 실제 파일과 라이브러리 안 표기를 함께 담는다.

    외부 링크면 `media_path`는 푼 대상(라이브러리 밖)이고 `in_library_path`는
    링크가 놓인 자리의 라이브러리 상대 표기다. 복사는 `media_path`에서 일어나므로
    심볼릭 링크 칸을 지나가는 일이 없다(A2 원본 경로 증명과 같은 규율).
    """

    event_name: str
    role: str
    stem: str
    media_path: str
    in_library_path: str
    external: bool
    size_bytes: int
    mtime_ns: int
    inode: int
    inspection: SourceInspection


@dataclass(frozen=True, slots=True)
class FcpLogicalClip:
    """논리 클립 하나 — 원본·프록시가 같은 (이벤트, 이름줄기)로 묶인 상태."""

    event_name: str
    stem: str
    original: FcpMediaFile | None
    proxy: FcpMediaFile | None


@dataclass(frozen=True, slots=True)
class FcpBrokenLink:
    """끊어졌거나 가져올 수 없는 링크 — 사유와 함께 눈에 보인다(조용한 제외 금지)."""

    linked_from: str
    reason: str


@dataclass(frozen=True, slots=True)
class FcpLibraryScan:
    """라이브러리 조사 결과 — 논리 클립·끊어진 링크·선택 역할 제안까지 한 묶음."""

    library_root: str
    workspace_root: str
    role: str
    events: tuple[str, ...]
    clips: tuple[FcpLogicalClip, ...]
    selected: tuple[tuple[FcpMediaFile, str], ...]
    broken: tuple[FcpBrokenLink, ...]
    rejected: tuple[RejectedSource, ...]
    proposal: IngestProposal


def is_fcp_library(path: Path) -> bool:
    """Final Cut 라이브러리 번들인가 — 확장자와 디렉터리(package) 여부로만 판정한다."""
    return path.suffix.lower() == FCP_LIBRARY_SUFFIX and path.is_dir()


def managed_workspace_root(library: Path) -> Path:
    """라이브러리의 관리 작업공간 자리 — 같은 층 sibling(라이브러리 밖, 안정 경로).

    같은 라이브러리는 언제나 같은 자리를 돌려받는다. 만들기·열기 판정은
    서비스 계층이 이 자리를 가지고 한다.
    """
    resolved = library.expanduser().resolve()
    stem = resolved.stem
    if not stem or stem.startswith("."):
        raise FcpLibraryError(f"라이브러리 이름을 알 수 없다: {library}")
    return resolved.with_name(stem + MANAGED_WORKSPACE_SUFFIX)


def scan_fcp_library(
    library: Path,
    workspace_root: Path,
    inspect: MediaInspect = ffprobe_media_inspector,
    role: str = FCP_ROLE_ORIGINAL,
    *,
    originals_only: bool = False,
) -> FcpLibraryScan:
    """라이브러리를 발견·조사해 제안을 만든다 — 이 단계는 라이브러리를 읽기만 한다.

    확정·복사는 이 제안이 기존 A2 확정·복사 파이프라인으로 흐르며, 원본(라이브러리
    안 파일·외부 링크 대상 모두)은 한 번도 다시 쓰이지 않는다.
    """
    if role not in FCP_ROLES:
        raise FcpLibraryError(f"알 수 없는 원본 역할: {role!r} ({' 또는 '.join(FCP_ROLES)})")
    resolved = library.expanduser().resolve()
    if not is_fcp_library(resolved):
        raise FcpLibraryError(f"Final Cut 라이브러리(.fcpbundle)가 아니다: {library}")
    workspace = workspace_root.expanduser().resolve()
    if not workspace.is_dir():
        raise FcpLibraryError(f"작업공간이 없다: {workspace_root}")
    _require_not_nested(resolved, workspace)
    events = _library_events(resolved)
    files: list[FcpMediaFile] = []
    broken: list[FcpBrokenLink] = []
    rejected: list[RejectedSource] = []
    for event_name, event_dir in events:
        for media_dir, media_role in _event_media_dirs(event_dir):
            _collect_media_dir(
                media_dir,
                resolved,
                event_name,
                media_role,
                workspace,
                inspect,
                files,
                broken,
                rejected,
            )
    clips = _logical_clips(tuple(files))
    selected = _select_role(clips, role)
    if originals_only:
        # This primary import path never silently substitutes proxy-only media.
        original_paths: set[str] = set()
        strict = []
        for clip in clips:
            file = clip.original
            if file is not None and file.media_path not in original_paths:
                original_paths.add(file.media_path)
                strict.append((file, ""))
        selected = tuple(strict)
    candidates = _candidates(selected, resolved)
    ordered = sorted(
        candidates,
        key=lambda candidate: source_order.order_key(
            candidate.inspection.capture_time, candidate.source_relative_path
        ),
    )
    scan = SourceScan(
        source_root=str(resolved),
        candidates=tuple(ordered),
        rejected=tuple(rejected),
    )
    return FcpLibraryScan(
        library_root=str(resolved),
        workspace_root=str(workspace),
        role=role,
        events=tuple(name for name, _ in events),
        clips=clips,
        selected=tuple(selected),
        broken=tuple(broken),
        rejected=tuple(rejected),
        proposal=IngestProposal(
            scan=scan,
            order=tuple(ordered),
            space=space_estimate(workspace, scan.total_bytes),
        ),
    )


def _require_not_nested(library: Path, workspace: Path) -> None:
    """라이브러리·작업공간 포함 관계 끊기 — 재귀 복사를 만드는 자리를 미리 닫는다."""
    if library == workspace:
        raise SourceNestingError("라이브러리 그 자체를 작업공간으로 쓸 수 없다")
    if library.is_relative_to(workspace):
        raise SourceNestingError(
            f"라이브러리({library})가 작업공간({workspace}) 안이다 — 스스로를 다시 복사하게 된다"
        )
    if workspace.is_relative_to(library):
        raise SourceNestingError(
            f"작업공간({workspace})이 라이브러리({library}) 안이다 — "
            "복사 결과가 다시 복사 대상이 된다"
        )


def _library_events(root: Path) -> tuple[tuple[str, Path], ...]:
    """이벤트 디렉터리 목록 — 임의 이름 + `CurrentVersion.fcpevent` 표지로 판정한다.

    실제 라이브러리의 이벤트 폴더 이름은 사람이 정한 임의 이름(예: 날짜)이다 —
    접미어 규칙 같은 건 없다. 표지 파일이 있는 바로 그 한 층만 본다(링크·숨김 제외).
    렌더 폴더 등 다른 폴더에는 이 표지가 없으므로 자연히 빠진다.
    """
    events: list[tuple[str, Path]] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if _pruned_entry(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        if (entry / FCP_EVENT_MARKER).exists():
            events.append((entry.name, entry))
    return tuple(events)


def _event_media_dirs(event_dir: Path) -> tuple[tuple[Path, str], ...]:
    """이벤트 안 미디어 폴더들 — 이름으로 찾는다(원본·프록시; 최적화는 이름이 달라 빠진다).

    실제 규격에서 미디어 폴더는 보통 `CurrentVersion.fcpevent/` 아래에 있지만 그냥
    이벤트 바로 아래인 형태도 있다 — 둘 다 잡도록 이벤트를 걷는다. 프록시는
    반드시 `Transcoded Media/Proxy Media`일 때만 프록시다(High Quality 제외).
    """
    found: list[tuple[Path, str]] = []
    for dirpath, dirnames, _filenames in os.walk(event_dir, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not _pruned_entry(name))
        for name in list(dirnames):
            role = None
            if name == ORIGINAL_MEDIA_DIR:
                role = FCP_ROLE_ORIGINAL
            elif name == PROXY_MEDIA_DIR and Path(dirpath).name == TRANSCODED_MEDIA_DIR:
                role = FCP_ROLE_PROXY
            if role is not None:
                found.append((Path(dirpath) / name, role))
                dirnames.remove(name)  # 미디어 폴더 안은 아래 걷기가 따로 본다
    return tuple(found)


def _pruned_entry(name: str) -> bool:
    """보호 철자·숨김 판정 — ingest의 어휘 가지치기와 같은 규칙(조회 이전)."""
    return name.startswith(".") or name.casefold() in PROTECTED_SOURCE_NAMES


def _collect_media_dir(
    media_dir: Path,
    library: Path,
    event_name: str,
    role: str,
    workspace: Path,
    inspect: MediaInspect,
    files: list[FcpMediaFile],
    broken: list[FcpBrokenLink],
    rejected: list[RejectedSource],
) -> None:
    """미디어 폴더 한 곳을 걷는다 — 폴더 링크(재배치 미디어)도 풀어서 본다."""
    if _pruned_entry(media_dir.name):
        return
    if media_dir.is_symlink():
        # 폴더 링크도 칸마다 안전 풀기 — 어휘 보호 검사가 조회·걷기보다 앞선다.
        resolved_dir, reason, probed = _resolve_path_safely(media_dir)
        if reason:
            broken.append(FcpBrokenLink(media_dir.relative_to(library).as_posix(), reason))
            return
        if probed is None or not stat_module.S_ISDIR(probed.st_mode):
            broken.append(
                FcpBrokenLink(
                    media_dir.relative_to(library).as_posix(),
                    "미디어 폴더 링크 대상이 디렉터리가 아니다",
                )
            )
            return
    else:
        if not media_dir.is_dir():
            return
        resolved_dir = media_dir
    # 폴더 자체가 링크면(재배치 미디어) 그 안 파일은 전부 외부 미디어다.
    relocated = media_dir.is_symlink()
    for dirpath, dirnames, filenames in os.walk(resolved_dir, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not _pruned_entry(name))
        for name in sorted(filenames):
            if _pruned_entry(name):
                continue
            if Path(name).suffix.lower() not in MEDIA_SUFFIXES:
                continue  # 보조 파일 — 미디어 후보가 아니다
            entry = Path(dirpath) / name
            spelled = media_dir / entry.relative_to(resolved_dir)
            _consider_media_file(
                entry,
                spelled,
                library,
                event_name,
                role,
                workspace,
                inspect,
                files,
                broken,
                rejected,
                external=relocated,
            )


def _consider_media_file(
    entry: Path,
    spelled: Path,
    library: Path,
    event_name: str,
    role: str,
    workspace: Path,
    inspect: MediaInspect,
    files: list[FcpMediaFile],
    broken: list[FcpBrokenLink],
    rejected: list[RejectedSource],
    *,
    external: bool,
) -> None:
    """미디어 파일 하나를 판정 — 링크 검사 → 조사 → 기록/끊어진 링크(사유 필수)."""
    in_library = spelled.relative_to(library).as_posix()
    try:
        probed = os.lstat(entry)
    except OSError as error:
        rejected.append(RejectedSource(in_library, f"조회 실패: {error}"))
        return
    target = entry
    if stat_module.S_ISLNK(probed.st_mode):
        target, reason = _resolve_link(entry, workspace)
        if reason:
            broken.append(FcpBrokenLink(in_library, reason))
            return
        external = True
        try:
            probed = os.stat(target)
        except OSError as error:
            broken.append(FcpBrokenLink(in_library, f"대상 조회 실패: {error}"))
            return
    elif not stat_module.S_ISREG(probed.st_mode):
        return  # 특수 파일 — 미디어 후보가 아니다
    try:
        inspection = inspect(target)
    except Exception as error:  # noqa: BLE001 - 조사 실패 사유는 전부 눈에 보여야 한다
        rejected.append(RejectedSource(in_library, f"영상 확인 실패: {error}"))
        return
    files.append(
        FcpMediaFile(
            event_name=event_name,
            role=role,
            stem=entry.stem,
            media_path=str(target),
            in_library_path=in_library,
            external=external,
            size_bytes=probed.st_size,
            mtime_ns=probed.st_mtime_ns,
            inode=probed.st_ino,
            inspection=inspection,
        )
    )


def _resolve_link(entry: Path, workspace: Path) -> tuple[Path, str]:
    """심볼릭 링크 대상 판정 — (대상, 빈 사유) 또는 (대상 표기, 사유).

    풀기는 `_resolve_path_safely`가 칸마다 한다 — 어휘 보호 검사가 매 lstat보다
    앞서므로 숨김·보호 대상은(사슬 중간의 것도) 한 번도 조회되지 않고 사유만
    남는다. 정당한 대상은 일반 파일·미디어 확장자·작업공간 밖인지 마지막에 본다.
    """
    target, reason, probed = _resolve_path_safely(entry)
    if reason:
        return target, reason
    if probed is None:  # 칸이 있는 한 마지막 lstat은 항상 있다 — 파수꾼 분기다
        return target, "링크 대상을 풀 수 없다"
    if not stat_module.S_ISREG(probed.st_mode):
        return target, "링크 대상이 일반 파일이 아니다"
    if target.suffix.lower() not in MEDIA_SUFFIXES:
        return target, f"링크 대상이 미디어 확장자가 아니다({target.suffix or '없음'})"
    if target.is_relative_to(workspace):
        return target, "링크 대상이 작업공간 안이다 — 스스로를 다시 복사하게 된다"
    return target, ""


def _protected_target_path(target: Path) -> bool:
    """링크 대상 경로의 모든 칸 검사 — 하나라도 숨김이거나 보호 철자면 참.

    조사 대상이 라이브러리 밖 경로 전체이므로 조상 칸까지 본다(스캔 걷기의
    이름 가지치기와 같은 규칙을 푼 경로에 그대로 편다).
    """
    return any(_pruned_entry(part) for part in target.parts if part not in ("/",))


_LINK_HOP_LIMIT = 40
"""한 경로 풀기에서 따라갈 심볼릭 링크 수 상한 — macOS 커널 한도(32)보다 넉넉히.

고리(A→B→A)는 여기서 명시적으로 끊는다 — 풀기가 끝나야 조사도 끝난다.
"""


def _resolve_path_safely(link: Path) -> tuple[Path, str, os.stat_result | None]:
    """경로를 **칸마다** 푼다 — 어휘 보호 검사가 매 lstat보다 앞선다(사슬 대상).

    `Path.resolve`는 사슬을 한 번에 따라가며 숨김 대상까지 lstat하고 본다(솔
    재현: outer→alias→.hidden/blocked). 이 풀기는 한 칸씩: 칸까지의 표기를 어휘로
    보호 검사하고, 링크를 만나면 `os.readlink` 표기로 다음 후보 **전체**를 다시
    검사한 뒤에만 따라간다. 돌려주는 것은 (푼 경로, 빈 사유면 성공, 마지막 칸 lstat).
    """
    remaining = list(_normalized_absolute(link).parts)[1:]
    resolved: list[str] = []
    hops = 0
    while remaining:
        part = remaining.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = Path("/", *resolved, part)
        if _protected_target_path(candidate):
            return candidate, "링크 대상이 숨김·보호 경로다 — 가져올 수 없다", None
        try:
            probed = os.lstat(candidate)
        except OSError as error:
            return candidate, f"링크 대상이 없다: {error}", None
        if stat_module.S_ISLNK(probed.st_mode):
            hops += 1
            if hops > _LINK_HOP_LIMIT:
                return candidate, "링크 고리(또는 너무 긴 사슬) — 풀기를 끊는다", None
            remaining = _hop_parts(candidate, resolved, remaining)
            resolved = []
            continue
        resolved.append(part)
        if remaining and not stat_module.S_ISDIR(probed.st_mode):
            return candidate, "링크 대상 경로의 중간 칸이 디렉터리가 아니다", None
        if not remaining:
            return candidate, "", probed
    return Path("/", *resolved), "", None


def _hop_parts(link: Path, resolved: list[str], remaining: list[str]) -> list[str]:
    """링크 한 칸을 표기로만 넘는다 — 대상(+남은 칸)의 새 칸 목록. 조회는 안 한다."""
    try:
        text = os.readlink(link)
    except OSError as error:
        raise FcpLibraryError(f"링크 표기를 읽지 못했다: {link} ({error})") from error
    if os.path.isabs(text):
        return list(Path(os.path.normpath(text)).parts)[1:] + remaining
    joined = os.path.normpath(os.path.join(os.fspath(Path("/", *resolved)), text))
    return list(Path(joined).parts)[1:] + remaining


def _normalized_absolute(path: Path) -> Path:
    """어휘 정규화만 한 절대 경로 — 심볼릭 링크는 전혀 따라가지 않는다."""
    return Path(os.path.normpath(os.path.abspath(os.fspath(path))))


def _logical_clips(files: tuple[FcpMediaFile, ...]) -> tuple[FcpLogicalClip, ...]:
    """원본·프록시를 (이벤트, 이름줄기)로 묶는다 — 대소문자는 잊고 본다.

    묶음은 **짝이 애매하지 않을 때만** 성립한다 — 원본·프록시가 한 개씩일 때뿐이다.
    같은 줄기의 원본이 여럿이면(A.mov·A.mp4) 어느 쪽이 프록시의 짝인지 알 수
    없으므로 묶지 않는다: 파일마다 제각각 클립으로 남겨 **하나도 잃지 않는다**
    (사전 덮어쓰기로 A.mov이 사라지던 흠이다). 표기 줄기는 처음 본 원래 표기다.
    """
    originals: dict[tuple[str, str], list[FcpMediaFile]] = {}
    proxies: dict[tuple[str, str], list[FcpMediaFile]] = {}
    display: dict[tuple[str, str], str] = {}
    for file in files:
        key = (file.event_name, file.stem.casefold())
        grouped = originals if file.role == FCP_ROLE_ORIGINAL else proxies
        grouped.setdefault(key, []).append(file)
        display.setdefault(key, file.stem)
    clips: list[FcpLogicalClip] = []
    for event, stem in sorted(originals.keys() | proxies.keys()):
        key = (event, stem)
        origs = originals.get(key, [])
        pros = proxies.get(key, [])
        if len(origs) == 1 and len(pros) == 1:
            clips.append(FcpLogicalClip(event, display[key], origs[0], pros[0]))
            continue
        clips.extend(FcpLogicalClip(event, display[key], file, None) for file in origs)
        clips.extend(FcpLogicalClip(event, display[key], None, file) for file in pros)
    return tuple(clips)


def _select_role(
    clips: tuple[FcpLogicalClip, ...], role: str
) -> tuple[tuple[FcpMediaFile, str], ...]:
    """클립마다 시간순 작업으로 가져갈 파일 하나 — 요청 역할이 없으면 반대쪽으로."""
    other = FCP_ROLE_PROXY if role == FCP_ROLE_ORIGINAL else FCP_ROLE_ORIGINAL
    selected: list[tuple[FcpMediaFile, str]] = []
    for clip in clips:
        preferred = clip.original if role == FCP_ROLE_ORIGINAL else clip.proxy
        fallback = clip.proxy if role == FCP_ROLE_ORIGINAL else clip.original
        if preferred is not None:
            selected.append((preferred, ""))
        elif fallback is not None:  # 그룹이 존재하면 둘 중 하나는 있다
            selected.append((fallback, f"{other}만 있다 — {other}(으)로 가져온다"))
    unique: dict[str, tuple[FcpMediaFile, str]] = {}
    for file, note in selected:
        unique.setdefault(file.media_path, (file, note))
    return tuple(unique.values())


def _candidates(
    selected: tuple[tuple[FcpMediaFile, str], ...], library: Path
) -> tuple[SourceCandidate, ...]:
    """선택 파일을 A2 후보로 — 출처 신원은 **실제 뿌리+실제 상대경로 그대로**다.

    외부 파일 여럿은 푼 대상 부모들의 **공통 조상**을 하나의 출처 뿌리로 삼고
    그 아래 **실제** 상대경로(external-a/same.mov·external-b/same.mov)로 적는다 —
    파일 신원과 목적지 이름이 자연히 어울려 꾸민 경로가 없다. 조상을 다시 훑지
    않는다: 훑을 대상은 이미 푼 링크 대상 파일들뿐이다. 라이브러리 안 파일은
    라이브러리 뿌리 + 라이브러리 상대경로 그대로다.
    """
    externals = [Path(file.media_path) for file, _note in selected if file.external]
    ancestor = _common_external_root(externals) if externals else library
    candidates: list[SourceCandidate] = []
    for file, _note in selected:
        media = Path(file.media_path)
        if file.external:
            source_root = str(ancestor)
            relative = media.relative_to(ancestor).as_posix()
        else:
            source_root = str(library)
            relative = file.in_library_path
        candidates.append(
            SourceCandidate(
                source_root=source_root,
                source_relative_path=relative,
                source_name=media.name,
                size_bytes=file.size_bytes,
                mtime_ns=file.mtime_ns,
                inode=file.inode,
                inspection=file.inspection,
            )
        )
    return tuple(candidates)


def _common_external_root(externals: list[Path]) -> Path:
    """외부 파일들의 공통 조상 — 전부 푼 실제 경로라 칸마다 링크가 없다.

    대상 하나뿐이면 그 부모가 조상이다(상대경로는 파일 이름만). 조상은 훑는
    대상이 아니라 신원 표기의 뿌리일 뿐이다.
    """
    if not externals:
        raise FcpLibraryError("외부 원본이 없는데 공통 조상을 물었다")
    return Path(os.path.commonpath([str(path.parent) for path in externals]))
