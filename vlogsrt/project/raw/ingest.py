"""SD 카드·폴더 → 작업공간 가져오기 — 발견·조사·확정·검증 복사(계획 §5).

경계를 지키는 방법이 이 파일의 내용이다:

- **ffprobe는 직접 부르지 않는다.** 실제 조사는 `vlogsrt.preprocess`의 공개
  함수들뿐이고, 이 모듈은 그것을 `MediaInspect` 주입점 뒤에 둔다 — 시험은 가짜
  조사자로, 실사용은 `ffprobe_media_inspector`로 갈아끼운다.
- **촬영 시각을 지어내지 않는다.** 조사가 알려주지 않으면 `capture_time`은
  `None`(미조사)이다. 파일 수정 시각(mtime)을 촬영 시각으로 바꿔 쓰지 않는다 —
  mtime·inode는 어디까지나 **변경 검출**용 스냅숏이다(§6.2).
- **원본은 읽기만 한다.** 복사·검증·등록이 모두 원본을 다시 쓰는 길은 없다.
- **보호 이름은 stat보다 먼저 가지치기한다.** `.env`·자격 증명 철자는 경로
  표기만 보고 버린다 — 조회조차 일으키지 않는다(코디네이터 재결).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat as stat_module
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from vlogsrt import log, oscompat, preprocess
from vlogsrt.project.raw import source_order
from vlogsrt.project.raw.config import FOLDER_WORKSPACE
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext
from vlogsrt.project.raw.models import (
    CAPTURE_STATUS_INVALID,
    CAPTURE_STATUS_UNINSPECTED,
    CAPTURE_STATUS_VALID,
    ORDER_BASIS_USER_CONFIRMED,
    SOURCE_VIDEO_SUFFIXES,
    IngestCopyError,
    IngestError,
    InsufficientSpaceError,
    MediaNotAllowedError,
    MediaRegistration,
    NoIngestCandidatesError,
    RawWorkspaceError,
    SourceAsset,
    SourceAssetDraft,
    SourceChangedError,
    SourceNestingError,
    SourceOrderRevision,
)
from vlogsrt.project.raw.workspace import RawWorkspace

__all__ = [
    "COPY_CHUNK_BYTES",
    "InspectionError",
    "IngestConfirmation",
    "IngestProposal",
    "MediaInspect",
    "PARTIAL_SUFFIX",
    "PROTECTED_SOURCE_NAMES",
    "RejectedSource",
    "SourceCandidate",
    "SourceInspection",
    "SourceScan",
    "SpaceEstimate",
    "confirm_ingest",
    "ffprobe_media_inspector",
    "ingest_fingerprint",
    "reconfirm_ingest",
    "run_ingest_copy",
    "scan_sources",
    "space_estimate",
]

PARTIAL_SUFFIX = ".partial"
"""복사 중 파일의 확장자 — 최종 이름으로 바꾸기 전까지 이 상태로만 존재한다."""

COPY_CHUNK_BYTES = 1 << 20
"""한 번에 읽는 복사 조각 — 통째로 메모리에 올리지 않는다(§4.3)."""

SPACE_HEADROOM = 1.05
"""공간 검사의 안전 여유 — 남은 공간이 이 배수를 못 넘기면 복사를 시작하지 않는다."""

PROTECTED_SOURCE_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.example",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "secrets.json",
    }
)
"""어휘로 가지치기하는 보호 철자 — 점으로 시작하는 이름 규칙과 함께, stat 이전에
버린다. 미디어 확장자가 아니어도 이 목록은 자격 정보 탐색의 첫걸음을 막는다."""

INGEST_SETTINGS_DIGEST = "raw-ingest/1:partial+sha256-reread+stat-snapshot"
"""작업 지문에 박는 가져오기 설정 요약 — 복사·검증 규칙이 바뀌면 지문이 달라진다.
임계값(여유 배수·조각 크기)이 설정(`raw/config.py`)으로 옮겨가면 그 값이 여기
자리를 대신한다(코디네이터 ACK — 지문은 관련 설정을 포함한다)."""

CLIP_SPACE_NOTE = "조각 생성 추가 공간은 A3 단계에서 별도로 안내한다"


class InspectionError(IngestError):
    """후보 조사 실패 — 후보에서 빠지되, 사유와 함께 눈에 보인다(조용한 제외 금지)."""


# ── 조사자 계약 — 주입형. 실제 구현은 preprocess 공개 함수만 쓴다 ──────────────


@dataclass(frozen=True, slots=True)
class SourceInspection:
    """실제 미디어 조사로만 채우는 값 — 모르면 `None` 그대로다(추측 금지).

    `capture_time`은 신뢰할 촬영 시각 근거다. `capture_time_status`는 그 근거의
    상태다 — `valid`일 때만 값이 있고 순서 정렬에 쓴다. `invalid`는 컨테이너가
    적은 무언가가 있었으나 파싱·시간대·상식 bound를 못 넘었다는 뜻이고,
    `uninspected`는 아예 묻지 않은 것이다. 어느 쪽도 mtime으로 메워지지
    않는다(§6.2).

    `width`·`height`는 **재생 화면**의 크기다 — 회전 선언(90°·270°)이 있으면 저장된 가로·세로를
    뒤바꾼 값이다(W-26, D66). 아이폰 세로 영상이 가져오기 표에 1920×1080으로 보이던 자리다. 이
    값은 표시와 `source_assets` 기록에만 쓰이고 어느 지문·캐시 열쇠·승인·견적에도 들지 않는다.
    """

    duration_seconds: float | None
    width: int | None
    height: int | None
    audio_streams: int | None
    capture_time: str | None
    capture_time_status: str = CAPTURE_STATUS_UNINSPECTED


MediaInspect = Callable[[Path], SourceInspection]
"""조사자 — 경로 하나를 받아 조사값을 돌려준다. 실패는 예외로 알린다."""


def ffprobe_media_inspector(path: Path) -> SourceInspection:
    """실제 조사자 — `probe_raw_media` 한 번으로 길이·기하·오디오·촬영 시각을 묶는다.

    촬영 시각은 컨테이너 선언을 파싱해 **유효할 때만** 묶는다(ISO-8601·시간대·
    상식 bound — preprocess의 `CreationTime` 판정). 못 미치면 값은 버리고
    `invalid`로 남긴다 — 있는데 못 믿는 것과 아예 없는 것을 갈라 기록한다.
    비디오 스트림이 없거나 깨진 파일은 `RuntimeError`로 죽고(preprocess 계약),
    스캔이 `InspectionError` 사유로 바꿔 기록한다.

    같은 파일은 이 프로세스 안에서 한 번만 조사한다(W-24 세션 G) — 작업대의 첫 Final Cut 판이
    원본·프록시마다 ffprobe를 돌렸다(원본 18개 + 프록시 ≈ 36번, 2–8초). 같은 파일인지는 경로와
    stat 신원(크기·수정 시각 ns·inode·장치)으로 가른다 — 하나라도 바뀌면 다시 조사한다. 기억은
    디스크에 남지 않고, 실패(예외)는 기억하지 않는다.
    """
    try:
        before = _file_identity(path)
    except OSError:
        return _inspect(path)  # 없는 파일 — 조사자가 예전 그대로 실패를 알린다
    key = str(path)
    with _INSPECTED_LOCK:
        held = _INSPECTED.get(key)
    if held is not None and held[0] == before:
        return held[1]
    inspection = _inspect(path)
    with contextlib.suppress(OSError):
        if _file_identity(path) == before:  # 조사하는 동안 바뀐 파일은 기억하지 않는다
            with _INSPECTED_LOCK:
                _INSPECTED[key] = (before, inspection)
    return inspection


_INSPECTED: dict[str, tuple[tuple[object, ...], SourceInspection]] = {}
"""경로 → (stat 신원, 조사값) — 프로세스 메모리에만 산다. 경로마다 마지막 하나만 둔다."""
_INSPECTED_LOCK = threading.Lock()


def _file_identity(path: Path) -> tuple[object, ...]:
    """조사값을 다시 써도 되는 같은 파일인가의 열쇠 — 조사자 함수까지 넣는다(갈아 끼우면 새로).
    변경 시각(ctime)은 크기·수정 시각을 그대로 둔 덮어쓰기(`cp -p`, 거친 시각의 카메라 카드)를
    잡는다."""
    info = path.stat()
    return (
        preprocess.probe_raw_media,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_ino,
        info.st_dev,
    )


def _inspect(path: Path) -> SourceInspection:
    probe = preprocess.probe_raw_media(path)
    creation = probe.creation_time
    if creation.valid and creation.utc is not None:
        status = CAPTURE_STATUS_VALID
        capture_time: str | None = creation.utc.isoformat()
    elif creation.raw:
        status = CAPTURE_STATUS_INVALID
        capture_time = None
    else:
        status = CAPTURE_STATUS_UNINSPECTED
        capture_time = None
    width, height = probe.video.display_size  # 회전 선언을 반영한 재생 화면(W-26)
    return SourceInspection(
        duration_seconds=float(probe.duration),
        width=width,
        height=height,
        audio_streams=len(probe.audio),
        capture_time=capture_time,
        capture_time_status=status,
    )


# ── 스캔 — 발견·조사. 아직 아무 것도 쓰지 않는다 ──────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """가져오기 후보 하나 — 조사까지 마친 상태. 확정 전에는 디스크에 아무 변화가 없다."""

    source_root: str
    source_relative_path: str
    source_name: str
    size_bytes: int
    mtime_ns: int
    inode: int
    inspection: SourceInspection

    @property
    def capture_time(self) -> str | None:
        """촬영 시각 — 조사값의 것만. 후보와 자산이 같은 정렬 규칙을 지나간다."""
        return self.inspection.capture_time


@dataclass(frozen=True, slots=True)
class RejectedSource:
    """후보에서 빠진 항목과 그 사유 — 조용히 사라지지 않고 이름을 남긴다."""

    source_relative_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class SourceScan:
    """한 번의 발견 결과 — 후보와 제외 항목 전부."""

    source_root: str
    candidates: tuple[SourceCandidate, ...]
    rejected: tuple[RejectedSource, ...]

    @property
    def total_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class SpaceEstimate:
    """공간 견적 — 복사 공간만 판정한다(§5.2: 조각 공간은 따로 안내)."""

    copy_bytes: int
    free_bytes: int
    required_bytes: int
    sufficient: bool
    note: str


@dataclass(frozen=True, slots=True)
class IngestProposal:
    """스캔 + 제안 순서 + 공간 견적 — 사람이 확인하기 전의 읽기전용 보기."""

    scan: SourceScan
    order: tuple[SourceCandidate, ...]
    space: SpaceEstimate


def scan_sources(
    source_root: Path,
    workspace_root: Path,
    inspect: MediaInspect = ffprobe_media_inspector,
) -> IngestProposal:
    """원본 루트를 발견·조사해 제안을 만든다 — 이 단계는 읽기만 한다.

    심볼릭 링크 디렉터리는 내려가지 않고, 링크 파일은 따라가지 않는다. 확장자만
    믿지 않고 조사자가 영상임을 확인한 파일만 후보가 된다(§5.2).
    """
    resolved = _safe_source_root(source_root, workspace_root)
    scan = _walk_sources(resolved, inspect)
    ordered = sorted(
        scan.candidates,
        key=lambda candidate: source_order.order_key(
            candidate.inspection.capture_time, candidate.source_relative_path
        ),
    )
    return IngestProposal(
        scan=scan,
        order=tuple(ordered),
        space=space_estimate(workspace_root, scan.total_bytes),
    )


def scan_selected_source(
    source: Path, workspace_root: Path, inspect: MediaInspect = ffprobe_media_inspector
) -> IngestProposal:
    """Inspect only the chosen video; never walk neighbouring user files."""
    if (
        any(_pruned_name(part) for part in source.parts)
        or source.suffix.lower() not in SOURCE_VIDEO_SUFFIXES
    ):
        raise IngestError("지원하지 않는 영상 경로입니다")
    root = source.parent.expanduser().resolve()
    if root.is_relative_to(workspace_root.expanduser().resolve()):
        raise SourceNestingError("작업공간 안의 영상을 다시 가져올 수 없습니다")
    candidates: list[SourceCandidate] = []
    rejected: list[RejectedSource] = []
    _consider_entry(root / source.name, source.name, root, inspect, candidates, rejected)
    scan = SourceScan(str(root), tuple(candidates), tuple(rejected))
    if not candidates:
        raise IngestError("영상 선택 실패: " + "; ".join(r.reason for r in rejected))
    return IngestProposal(scan, tuple(candidates), space_estimate(workspace_root, scan.total_bytes))


def _safe_source_root(source_root: Path, workspace_root: Path) -> Path:
    """가져오기 원본 루트 검사 — 없는 곳·파일·작업공간과의 포함 관계를 끊는다.

    양쪽 다 resolve()로 심볼릭 링크를 편 뒤에 비교한다 — 링크로 위장한 재귀
    복사(원본 안에 작업공간, 작업공간 안에 원본)도 같은 판정에 걸린다(§5.2).
    """
    resolved = source_root.expanduser().resolve()
    if not resolved.exists():
        raise IngestError(f"원본 경로가 없다: {source_root}")
    if not resolved.is_dir():
        raise IngestError(f"원본 경로가 폴더가 아니다: {source_root}")
    workspace = workspace_root.expanduser().resolve()
    if _is_folder_workspace(resolved, workspace):
        return resolved  # 폴더 작업공간(D100 §2) — 스캔이 그 이름을 건너뛰므로 다시 읽지 않는다
    if resolved == workspace:
        raise SourceNestingError(
            "작업공간 그 자체를 가져올 수 없다 — 카드나 다른 원본 폴더를 고르세요"
        )
    if resolved.is_relative_to(workspace):
        raise SourceNestingError(
            f"원본 루트({resolved})가 작업공간({workspace}) 안이다 — 스스로를 다시 복사하게 된다"
        )
    if workspace.is_relative_to(resolved):
        raise SourceNestingError(
            f"작업공간({workspace})이 원본 루트({resolved}) 안이다 — "
            "복사 결과가 다시 복사 대상이 된다"
        )
    return resolved


def _is_folder_workspace(source_root: Path, workspace: Path) -> bool:
    """작업공간이 정확히 `원본 루트/_vlogsrt`인가 — 허용하는 중첩은 이 한 겹뿐이다(D100 §2).

    더 깊이 묻힌 작업공간(`원본/a/_vlogsrt`)이나 다른 이름의 작업공간은 그대로 거부된다 — 그
    자리는 사람이 고른 폴더 작업공간이 아니다. 비교는 resolve() 뒤의 경로다.
    """
    return workspace.parent == source_root and workspace.name == FOLDER_WORKSPACE.workspace_dirname


def _walk_sources(root: Path, inspect: MediaInspect) -> SourceScan:
    """원본 트리를 한 번 걷는다 — 보호 이름은 어휘 단계에서, stat 이전에 버린다.

    후보 이름을 먼저 다 모아 조사할 수를 안 뒤 하나씩 조사한다 — 폴더 불러오기의 스캔은 요청
    안에서 동기로 돌므로 파일마다 `i/N` 한 줄이 화면 「작업 진행」 판에 흐른다(D101).
    """
    candidates: list[SourceCandidate] = []
    rejected: list[RejectedSource] = []
    entries = _media_entries(root)
    for index, entry in enumerate(entries, 1):
        log.info(f"원본 확인 {index}/{len(entries)} · {entry.name}")
        relative = entry.relative_to(root).as_posix()
        _consider_entry(entry, relative, root, inspect, candidates, rejected)
    return SourceScan(
        source_root=str(root),
        candidates=tuple(candidates),
        rejected=tuple(rejected),
    )


def _media_entries(root: Path) -> list[Path]:
    """영상 후보 이름 — 어휘만 본다(조회 없음). 차례는 걷는 차례 그대로다."""
    entries: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not _pruned_name(name))
        for name in sorted(filenames):
            if _pruned_name(name):
                continue  # 어휘 가지치기 — 조회(stat)조차 하지 않는다
            if Path(name).suffix.lower() not in SOURCE_VIDEO_SUFFIXES:
                continue  # .LRF·썸네일·사진·소리·자막 — 영상 후보가 아니다(§5.2)
            entries.append(Path(dirpath) / name)
    return entries


def _consider_entry(
    entry: Path,
    relative: str,
    root: Path,
    inspect: MediaInspect,
    candidates: list[SourceCandidate],
    rejected: list[RejectedSource],
) -> None:
    """파일 하나를 후보로 들일지 결정 — 링크 검사 → 조사 → 후보/제외(사유 필수)."""
    try:
        probed = os.lstat(entry)
    except OSError as error:
        rejected.append(RejectedSource(relative, f"조회 실패: {error}"))
        return
    if stat_module.S_ISLNK(probed.st_mode):
        rejected.append(
            RejectedSource(relative, "심볼릭 링크 — 루트 밖으로 나가는 길이므로 건너뛴다")
        )
        return
    if not stat_module.S_ISREG(probed.st_mode):
        return  # 소켓·파이프 같은 특수 파일 — 영상 후보가 아니다
    try:
        inspection = inspect(entry)
    except Exception as error:  # noqa: BLE001 - 조사 실패 사유는 전부 눈에 보여야 한다
        rejected.append(RejectedSource(relative, f"영상 확인 실패: {error}"))
        return
    candidates.append(
        SourceCandidate(
            source_root=str(root),
            source_relative_path=relative,
            source_name=entry.name,
            size_bytes=probed.st_size,
            mtime_ns=probed.st_mtime_ns,
            inode=probed.st_ino,
            inspection=inspection,
        )
    )


def _pruned_name(name: str) -> bool:
    """보호 철자 판정 — 파일시스템을 전혀 묻지 않는 순수 어휘 검사.

    폴더 작업공간 이름(`_vlogsrt`, D100 §2)도 건너뛴다 — 그 안의 분석 프록시·미리보기가 새 원본으로
    잡히지 않게. 대소문자를 가리지 않는 파일 시스템(macOS·Windows)에서 같은 폴더라 접어 비교한다.
    """
    folded = name.casefold()
    return (
        name.startswith(".")
        or folded in PROTECTED_SOURCE_NAMES
        or folded == FOLDER_WORKSPACE.workspace_dirname.casefold()
    )


def space_estimate(workspace_root: Path, copy_bytes: int) -> SpaceEstimate:
    """복사 공간 견적 — 남은 공간이 필요량(여유 배수 포함)을 못 넘기면 부족하다.

    작업공간이 아직 없으면(폴더 불러오기는 만들기 전에 스캔한다) 있는 가장 가까운 위 폴더의
    디스크를 잰다 — 작업공간은 그 안에 생긴다.
    """
    present = workspace_root.expanduser().absolute()
    while not present.exists() and present.parent != present:
        present = present.parent
    usage = shutil.disk_usage(present)
    required = int(copy_bytes * SPACE_HEADROOM) + 1
    return SpaceEstimate(
        copy_bytes=copy_bytes,
        free_bytes=usage.free,
        required_bytes=required,
        sufficient=usage.free >= required,
        note=CLIP_SPACE_NOTE,
    )


# ── 확정 — 배치·자산·불변 순서 리비전. 사람의 확인 뒤에만 온다 ─────────────────


@dataclass(frozen=True, slots=True)
class IngestConfirmation:
    """확정된 가져오기 — 배치·순서 리비전·자산, 그리고 작업 지문.

    `job_fingerprint`는 원본 루트·순서대로 본 출처 신원(경로·크기·mtime·inode)
    스냅숏·순서 리비전·가져오기 설정을 함께 묶는다. 재시도는 이 지문이 같을
    때만 같은 작업에 다시 붙는다 — 입력이 바뀌면 새 가져오기다.
    """

    batch_dir: str
    revision: SourceOrderRevision
    assets: tuple[SourceAsset, ...]
    job_fingerprint: str
    space: SpaceEstimate


def confirm_ingest(workspace: RawWorkspace, proposal: IngestProposal) -> IngestConfirmation:
    """제안을 확정해 배치를 연다 — 스캔 뒤 원본이 바뀌었으면 아무 것도 쓰지 않고 실패."""
    if not proposal.order:
        raise NoIngestCandidatesError(
            "가져올 영상이 없다 — 원본 폴더를 다시 조사하고 제외 사유를 확인하세요"
        )
    revalidated = _revalidate_sources(proposal)
    drafts = tuple(
        _draft_from_candidate(index, candidate) for index, candidate in enumerate(revalidated)
    )
    assets, revision = workspace.create_ingest_batch(drafts, ORDER_BASIS_USER_CONFIRMED)
    fingerprint = ingest_fingerprint(
        revalidated[0].source_root,
        [
            (
                draft.source_relative_path,
                draft.size_bytes,
                draft.source_mtime_ns,
                draft.source_inode,
            )
            for draft in drafts
        ],
        revision.revision_id,
    )
    return IngestConfirmation(
        batch_dir=revision.batch_dir,
        revision=revision,
        assets=assets,
        job_fingerprint=fingerprint,
        space=space_estimate(workspace.root, proposal.scan.total_bytes),
    )


def _draft_from_candidate(index: int, candidate: SourceCandidate) -> SourceAssetDraft:
    """후보 하나를 확정 초안으로 — 순번은 제안 그대로 1부터, 근거는 미조사 표기."""
    return SourceAssetDraft(
        source_root=candidate.source_root,
        source_relative_path=candidate.source_relative_path,
        source_name=candidate.source_name,
        size_bytes=candidate.size_bytes,
        source_mtime_ns=candidate.mtime_ns,
        source_inode=candidate.inode,
        capture_time=candidate.inspection.capture_time,
        duration_seconds=candidate.inspection.duration_seconds,
        width=candidate.inspection.width,
        height=candidate.inspection.height,
        ordinal=index + 1,
        basis_note=source_order.capture_basis_note(candidate.inspection.capture_time),
    )


def _revalidate_sources(
    proposal: IngestProposal,
) -> tuple[SourceCandidate, ...]:
    """확정 직전 재검사 — 크기·mtime·inode·링크가 스캔과 같은지 다시 본다(§5.3)."""
    for candidate in proposal.order:
        entry = Path(candidate.source_root) / candidate.source_relative_path
        try:
            probed = os.lstat(entry)
        except OSError as error:
            raise SourceChangedError(
                f"원본이 사라졌다: {candidate.source_relative_path} ({error})"
            ) from error
        changed = (
            probed.st_size != candidate.size_bytes
            or probed.st_mtime_ns != candidate.mtime_ns
            or probed.st_ino != candidate.inode
            or stat_module.S_ISLNK(probed.st_mode)
        )
        if changed:
            raise SourceChangedError(
                f"원본이 스캔 뒤 바뀌었다: {candidate.source_relative_path} "
                f"(크기 {candidate.size_bytes}→{probed.st_size})"
            )
    return proposal.order


def ingest_fingerprint(
    source_root: str,
    ordered_stats: Sequence[tuple[str, int, int, int]],
    revision_id: str,
) -> str:
    """작업 지문 — 출처 신원 스냅숏·순서·설정까지 묶는 sha256(코디네이터 ACK)."""
    payload = json.dumps(
        {
            "settings": INGEST_SETTINGS_DIGEST,
            "source_root": source_root,
            "files": [
                {"path": path, "size": size, "mtime_ns": mtime_ns, "inode": inode}
                for path, size, mtime_ns, inode in ordered_stats
            ],
            "revision": revision_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reconfirm_ingest(workspace: RawWorkspace, batch_dir: str) -> IngestConfirmation:
    """DB에 남은 배치로 확인 객체를 다시 만든다 — 재시도가 처음과 같은 지문을 갖는다."""
    assets = workspace.list_source_assets(batch_dir)
    if not assets:
        raise IngestError(f"가져오기 배치가 없다: {batch_dir}")
    revision = workspace.order_revision(batch_dir)
    if revision is None:
        raise IngestError(f"배치에 순서 리비전이 없다: {batch_dir}")
    ordered = tuple(sorted(assets, key=lambda asset: asset.ordinal))
    fingerprint = ingest_fingerprint(
        ordered[0].source_root,
        [
            (
                asset.source_relative_path,
                asset.size_bytes,
                asset.source_mtime_ns,
                asset.source_inode,
            )
            for asset in ordered
        ],
        revision.revision_id,
    )
    pending_bytes = sum(asset.size_bytes for asset in ordered if asset.status != "ready")
    return IngestConfirmation(
        batch_dir=batch_dir,
        revision=revision,
        assets=ordered,
        job_fingerprint=fingerprint,
        space=space_estimate(workspace.root, pending_bytes),
    )


# ── 복사·검증 — 파일 단위. ready만 나가고, 나머지는 재개 가능하게 남는다 ─────────


PASS_SKIP = "skip"
"""이번 실행에서 그대로 두는 몫 — ready이고 재독출 재검증까지 통과했다."""

PASS_HEAL = "heal"
"""검증된 적 있고 내용이 다시 맞는 몫 — 파일을 건드리지 않고 ready로 되돌린다."""

PASS_INVALID = "invalid"
"""검증된 적 있으나 지금 어긋나는 몫 — 파일·해시 기록·등록 ID 모두 불가침이다."""

PASS_COPY = "copy"
"""아직 한 번도 검증된 적 없는 몫 — 이번 실행이 복사할 유일한 대상이다."""


def run_ingest_copy(
    context: JobContext, workspace: RawWorkspace, confirmation: IngestConfirmation
) -> None:
    """확정된 순서대로 파일 단위 복사·검증 — 작업 본문(`jobs`가 부른다).

    **한번 검증된 원본은 영원히 불가침이다**: 재검증이 어긋나면 파일을 그대로 둔 채
    실패로 남고, 몇 번을 재시도해도 다시 복사·삭제·덮어쓰기가 일어나지 않는다 —
    다시 가져오는 길은 새 가져오기(새 배치)뿐이다. 실제 복사할 몫(`PASS_COPY`)만
    공간 게이트에 들고, 진행값은 이번 실행 기준으로 다시 맞춘다.
    """
    ordered = tuple(sorted(confirmation.assets, key=lambda asset: asset.ordinal))
    passes = _survey_pending(workspace, ordered)
    pending_bytes = sum(live.size_bytes for live, kind in passes if kind == PASS_COPY)
    _require_copy_space(workspace.root, pending_bytes)
    context.set_total(len(passes))
    context.set_progress(0)
    failures: list[str] = []
    for live, kind in passes:
        context.check_cancel()
        failure = _attempt_one_pass(workspace, live, kind)
        if failure is not None:
            failures.append(failure)
        context.advance()
    if failures:
        raise IngestCopyError(
            f"{len(failures)}/{len(passes)}건이 실패했다 — "
            + " | ".join(failures[:3])
            + (" …" if len(failures) > 3 else "")
        )


def _attempt_one_pass(workspace: RawWorkspace, live: SourceAsset, kind: str) -> str | None:
    """한 건의 이번 실행 처리 — 성공이면 None, 실패 문장을 돌려준다."""
    if kind == PASS_SKIP:
        return None
    if kind == PASS_HEAL:
        try:
            _heal_ever_verified(workspace, live)
        except Exception as error:  # noqa: BLE001 - 회복 실패도 파일 없이 기록된다
            return _record_asset_failure(workspace, live.asset_id, error)
        return None
    if kind == PASS_INVALID:
        return _record_asset_failure(workspace, live.asset_id, _ever_verified_invalid_error(live))
    try:
        _copy_one_asset(workspace, live.asset_id)
    except JobCancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 파일 단위 실패는 기록하고 다음 파일로
        return _record_asset_failure(workspace, live.asset_id, error)
    return None


def _heal_ever_verified(workspace: RawWorkspace, live: SourceAsset) -> None:
    """검증 원본의 내용이 다시 맞으로 돌아왔다 — 파일은 건드리지 않고 상태만 되돌린다.

    등록 행은 **같은 file_id로** 지문만 다시 찍는다(되살린 파일의 mtime·inode가
    등록 때와 어긋나 있어도 제공이 끊기지 않게). ID·경로는 그대로다.
    """
    if live.file_id is not None:
        workspace.refresh_media_fingerprint(live.file_id)
    workspace.update_source_asset(live.asset_id, status="ready", error=None)


def _survey_pending(
    workspace: RawWorkspace, ordered: tuple[SourceAsset, ...]
) -> list[tuple[SourceAsset, str]]:
    """이번 실행의 실제 대상 — 상태표가 아니라 내용 재독출·검증 이력으로 가른다.

    `content_sha256`이 남아 있으면 **한번 검증된 원본**이다: 내용이 다시 맞으면
    heal, 어긋나면 invalid — 어느 쪽도 파일을 쓰지 않는다. 복사는 검증 이력이
    없는 몫에만 일어나므로, 실패 상태로 내버려둔 검증 원본이 다음 재시도에
    덮어쓰이는 일은 없다(루트 재현).
    """
    passes: list[tuple[SourceAsset, str]] = []
    for asset in ordered:
        live = workspace.source_asset(asset.asset_id)
        content_ok = _destination_content_matches(workspace, live)
        registration_ok = live.file_id is None or _registration_resolves(workspace, live)
        if live.status == "ready" and content_ok and registration_ok:
            kind = PASS_SKIP
        elif live.content_sha256 is not None:
            kind = PASS_HEAL if content_ok else PASS_INVALID
        else:
            kind = PASS_COPY
        passes.append((live, kind))
    return passes


def _registration_resolves(workspace: RawWorkspace, live: SourceAsset) -> bool:
    """등록 행이 아직 제공 가능한가 — 지문 어긋남은 '변했다'로 잡힌다(A1 계약)."""
    if live.file_id is None:
        return True
    try:
        workspace.resolve_media_file(live.file_id)
    except RawWorkspaceError:
        return False
    return True


def _ever_verified_invalid_error(live: SourceAsset) -> IngestError:
    """검증 이력이 있는 원본의 재검증 실패 문장 — 파일은 그대로 두고 사람에게 넘긴다."""
    return IngestError(
        f"검증된 적 있는 원본이 재검증에 실패했다: {live.source_relative_path} — "
        "파일·내용 해시 기록·등록 ID를 그대로 둔다. 다시 가져오려면 새 가져오기"
        "(새 배치)를 확정하세요"
    )


def _require_copy_space(workspace_root: Path, copy_bytes: int) -> None:
    estimate = space_estimate(workspace_root, copy_bytes)
    if not estimate.sufficient:
        raise InsufficientSpaceError(
            f"복사 공간이 부족하다 — 필요 {estimate.required_bytes:,}B(여유 "
            f"{SPACE_HEADROOM:.0%} 포함) / 남은 {estimate.free_bytes:,}B. "
            "복사는 시작하지 않았다"
        )


def _copy_one_asset(workspace: RawWorkspace, asset_id: str) -> None:
    """자산 하나를 복사·검증해 ready로 만든다 — 어느 단계에서 죽어도 ready는 아니다."""
    live = workspace.source_asset(asset_id)
    source = _validated_source_path(live)
    pre_stat = _source_stat(source, live)
    workspace.update_source_asset(asset_id, status="copying", error=None)
    destination = _validated_destination_path(workspace, live)
    _prepare_destination(workspace, live, destination)
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    copied_hash = _copy_with_hash(source, partial)
    post_stat = os.lstat(source)
    if (post_stat.st_size, post_stat.st_mtime_ns, post_stat.st_ino) != (
        pre_stat.st_size,
        pre_stat.st_mtime_ns,
        pre_stat.st_ino,
    ):
        raise SourceChangedError(f"복사하는 동안 원본이 바뀌었다: {live.source_relative_path}")
    workspace.update_source_asset(asset_id, status="verifying")
    reread_hash = _sha256_file(partial)
    if reread_hash != copied_hash or partial.stat().st_size != pre_stat.st_size:
        raise IngestError("검증 실패 — 목적지를 다시 읽은 해시가 복사 때 해시와 다르다")
    oscompat.replace(partial, destination)
    registration = _register_verified(workspace, live, destination)
    duplicate_of = workspace.first_ready_asset_with_hash(reread_hash, asset_id)
    workspace.update_source_asset(
        asset_id,
        status="ready",
        content_sha256=reread_hash,
        file_id=registration.file_id,
        duplicate_of=duplicate_of,
        error=None,
    )


def _lexically_safe_relative(relative: str, *, originals_only: bool) -> str:
    """경로 표기 검사 — 어느 칸도 비거나(`.`·`..`) 숨김이면 안 된다(링크 이전 단계)."""
    parts = relative.split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        raise IngestError(f"경로 표기가 안전하지 않다: {relative}")
    if any(part.startswith(".") for part in parts):
        raise IngestError(f"숨김 칸이 끼어 있다: {relative}")
    if originals_only and not relative.startswith("originals/"):
        raise IngestError(f"가져오기 목적지는 originals/ 아래만 가능하다: {relative}")
    return relative


def _validated_tree(root: Path, relative: str, *, allow_missing_tail: bool) -> Path:
    """루트 아래 각 칸을 lstat해 심볼릭 링크가 하나도 없음을 증명하고 경로를 돌려준다.

    링크가 끼어 있으면 그 자리에서 실패한다 — 읽기·쓰기·지우기 무엇도 하기 전이다.
    `allow_missing_tail`이면 처음으로 없는 칸에서 멈춘다(그 아래는 우리가 만들 것이다).
    """
    target = root / relative
    current = root
    for part in relative.split("/"):
        current = current / part
        try:
            probed = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail:
                return target
            raise OSError(f"경로 칸이 없다: {current}") from None
        if stat_module.S_ISLNK(probed.st_mode):
            raise IngestError(f"심볼릭 링크 칸을 지나갈 수 없다: {current}")
    return target


def _validated_source_path(live: SourceAsset) -> Path:
    """원본 전체 경로 증명 — 마지막 파일만 보지 않고 조상 칸까지 전부 링크가 아님을 본다."""
    source_root = Path(live.source_root)
    _validated_tree(source_root.parent, source_root.name, allow_missing_tail=False)
    return _validated_tree(source_root, live.source_relative_path, allow_missing_tail=False)


def _validated_destination_path(workspace: RawWorkspace, live: SourceAsset) -> Path:
    """목적지 전체 경로 증명 — 어휘 검사 뒤 조상 칸까지 확인한다(mkdir보다 앞선다)."""
    relative = _lexically_safe_relative(live.relative_path, originals_only=True)
    return _validated_tree(workspace.root, relative, allow_missing_tail=True)


def _source_stat(source: Path, live: SourceAsset) -> os.stat_result:
    """복사 직전 원본 스냅숏 — 없거나(카드 분리) 확정 때와 다르면 시작하지 않는다."""
    try:
        probed = os.lstat(source)
    except OSError as error:
        raise OSError(f"원본을 읽을 수 없다: {live.source_relative_path} ({error})") from error
    if stat_module.S_ISLNK(probed.st_mode):
        raise SourceChangedError(f"원본이 심볼릭 링크로 바뀌었다: {live.source_relative_path}")
    if (
        probed.st_size != live.size_bytes
        or probed.st_mtime_ns != live.source_mtime_ns
        or probed.st_ino != live.source_inode
    ):
        raise SourceChangedError(
            f"원본이 확정 때와 다르다: {live.source_relative_path} "
            f"(크기 {live.size_bytes}→{probed.st_size}) — 다시 조사·확정해야 한다"
        )
    return probed


def _prepare_destination(workspace: RawWorkspace, live: SourceAsset, destination: Path) -> None:
    """목적지 정리 — 잔해 partial은 덮고, 장부와 어긋난 최종 파일은 치운다.

    치울 수 있는 것은 **우리가 쓴 미검증 잔해**뿐이다. 검증 이력(`content_sha256`)이
    있는 자산은 어떤 상태로 와도 여기서 실패한다 — 한번 검증된 원본의 목적지를
    치우는 길은 존재하지 않는다(재시도가 몇 번이든, 루트 재현). 경로 증명
    (`_validated_tree`)을 지난 뒤에만 mkdir·unlink가 일어나고, 링크로 바뀐 칸이나
    링크로 놓인 잔해도 그 자리에서 실패한다 — 링크 너머로 무엇도 쓰지 않는다.
    """
    if live.content_sha256 is not None:
        raise IngestError(
            f"검증된 적 있는 원본의 목적지는 다시 준비하지 않는다: {live.source_relative_path}"
            " — 새 가져오기(새 배치)로 다시 가져오세요"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    if partial.exists():
        if not _regular_file(partial):
            raise IngestError(f"잔해 partial이 일반 파일이 아니다 — 치우지 않는다: {partial}")
        partial.unlink()
    if destination.exists():
        if not _regular_file(destination):
            raise IngestError(f"목적지가 일반 파일이 아니다 — 치우지 않는다: {destination}")
        # ready 표시 없는 최종 파일은 이전 시도의 미검증 잔해다 — 등록 행도 함께
        # 치우고 다시 복사한다(검증되지 않은 파일을 ready로 믿지 않는다).
        workspace.discard_media_registration(live.relative_path)
        destination.unlink()


def _regular_file(path: Path) -> bool:
    try:
        return stat_module.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _register_verified(
    workspace: RawWorkspace, live: SourceAsset, destination: Path
) -> MediaRegistration:
    """검증을 마친 최종 파일을 미디어로 등록한다 — 등록만 남은 창은 스스로 닫는다."""
    try:
        return workspace.register_media_file(destination, "original")
    except MediaNotAllowedError:
        # 앞선 시도가 개명까지만 끝내고 자산 표시를 못 남긴 창 — 등록 행을
        # 치우고 다시 등록한다. 장부(source_assets)가 정본이다.
        workspace.discard_media_registration(live.relative_path)
        return workspace.register_media_file(destination, "original")


def _destination_content_matches(workspace: RawWorkspace, live: SourceAsset) -> bool:
    """목적지 내용 재확인 — 크기·내용 해시를 다시 읽어 믿는다(맹신 금지).

    재독출 전에 조상 칸까지 링크가 아님을 증명한다 — 링크 너머를 읽지 않는다.
    검증 이력(`content_sha256`)이 없으면 거짓이다 — 비교할 근거가 없다. 등록 행
    상태는 여기 보지 않는다(제공 안전망과 회복 판정은 갈라 본다).
    """
    try:
        destination = _validated_tree(
            workspace.root,
            _lexically_safe_relative(live.relative_path, originals_only=True),
            allow_missing_tail=False,
        )
        probed = os.lstat(destination)
    except (OSError, IngestError):
        return False
    if not stat_module.S_ISREG(probed.st_mode) or probed.st_size != live.size_bytes:
        return False
    if live.content_sha256 is None:
        return False
    return _sha256_file(destination) == live.content_sha256


def _record_asset_failure(workspace: RawWorkspace, asset_id: str, error: Exception) -> str:
    """파실패 기록 — 자산을 failed로, 잔해 partial을 치우고, 보고 문장을 돌려준다."""
    live = workspace.source_asset(asset_id)
    destination = workspace.root / live.relative_path
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    detail = str(error)
    if partial.exists() and _regular_file(partial):
        try:
            partial.unlink()
        except OSError as cleanup_error:
            detail += f" (partial 제거도 실패: {cleanup_error})"
    workspace.update_source_asset(asset_id, status="failed", error=detail[:500])
    return f"{live.source_relative_path}: {detail}"


def _copy_with_hash(source: Path, target: Path) -> str:
    """복사하며 원본을 읽는 동시에 해시를 계산한다 — 두 번째 판독은 목적지에서 온다."""
    digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(COPY_CHUNK_BYTES):
            writer.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    """파일을 다시 읽어 sha256을 계산한다 — 검증은 언제나 재판독이다(§5.3)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
