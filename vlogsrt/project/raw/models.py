"""A1·A2 데이터 계약 — 작업공간·미디어 등록·폴더 탐색·작업 상태·가져오기의 동결 기록.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §13.1·§14.1의 A1 부분과 §5·§6.2의
A2 부분(가져오기 자산·원본 순서 리비전)을 값으로 못 박는다. 여기엔 동작이 없다 —
DB 접근은 `workspace.py`·`jobs.py`, 조립은 `service.py`가 맡는다. 모든 레코드는
frozen이고, 시각 문자열은 UTC ISO-8601다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MEDIA_KINDS: tuple[str, ...] = ("original", "preview", "artifact")
"""브라우저에 제공할 수 있는 미디어 종류. 로그·설정·DB·API 응답은 없다(§4.4)."""

MEDIA_SUFFIXES: tuple[str, ...] = (
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".avi",
    ".mp3",
    ".wav",
    ".m4a",
)
"""미디어로 등록·제공할 수 있는 확장자. 이 목록 밖(.txt·.json·.log·.toml 등)은
종류·위치와 무관하게 등록이 거부된다 — 자격 증명·응답·기록 파일이 우회하지 못한다."""

KIND_PREFIXES: dict[str, tuple[str, ...]] = {
    "original": ("originals/",),
    "preview": ("previews/",),
    "artifact": ("clips/", "final/"),
}
"""종류별 디렉터리 짝 — 종류와 자리가 어긋난 등록도 어휘 단계에서 거부한다."""

MEDIA_STATUS_READY = "ready"
"""등록 즉시의 상태. A1에는 중간 상태가 없다 — 복사·검증 상태는 A2가 추가한다."""

SERVABLE_MEDIA_STATUSES: tuple[str, ...] = (MEDIA_STATUS_READY,)
"""제공 가능한 미디어 상태. 이 목록 밖의 상태면 HTTP 제공을 거부한다."""

JOB_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "cancel_requested",
    "cancelled",
    "succeeded",
    "failed",
    "paused",
    "interrupted",
)
"""§14.1의 작업 상태 전부. `interrupted`는 서버 재시작 복구가 만드는 재개 가능 상태."""

ACTIVE_JOB_STATUSES: tuple[str, ...] = ("queued", "running", "cancel_requested")
"""진행 중으로 치는 상태 — 같은 작업의 재제출은 이 상태일 때만 기존 작업에 붙는다."""


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    """화면 1에 그대로 뿌리는 작업공간 요약. 경로는 사용자가 선택한 실제 경로다.

    `workspace_id`는 이 작업공간의 불변 신원이다 — 미디어 ID와 함께, 화면의
    마지막 선택이 다른 프로젝트를 향할 때 그것을 밝혀내는 근거가 된다.
    """

    workspace_root: str
    workspace_id: str
    name: str
    workflow_schema_version: str
    db_schema_version: str
    network_mode: str
    created_at: str
    media_count: int
    active_jobs: int


@dataclass(frozen=True, slots=True)
class MediaRegistration:
    """등록된 미디어 한 건. 브라우저에는 `file_id`만 노출한다(§4.4)."""

    file_id: str
    kind: str
    relative_path: str
    status: str
    size_bytes: int
    registered_at: str


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    """등록 검증을 통과한 미디어의 실제 위치. HTTP 계층이 여기서만 경로를 본다."""

    file_id: str
    kind: str
    path: Path
    size_bytes: int
    content_type: str


@dataclass(frozen=True, slots=True)
class ByteRange:
    """구간 요청 하나 — 끝을 포함하는 닫힌 구간(RFC 9110 bytes 단위)."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class FolderEntry:
    """폴더 탐색 항목 하나. 점으로 시작하는 이름은 애초에 목록에 오지 않는다."""

    name: str
    path: str
    is_directory: bool
    is_media_candidate: bool


@dataclass(frozen=True, slots=True)
class FolderListing:
    """명시적 탐색 한 번의 결과. 재귀 스캔이 아니라 한 층의 목록이다(§4.2)."""

    path: str
    parent: str | None
    entries: tuple[FolderEntry, ...]


@dataclass(frozen=True, slots=True)
class JobRecord:
    """작업 한 건의 지속 상태 — `gr.State`가 아니라 DB가 정본이다(§14.1)."""

    job_id: str
    kind: str
    input_fingerprint: str
    status: str
    progress_current: int
    progress_total: int
    error: str | None
    cancellation_requested: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobHandle:
    """작업 제출 결과 — 같은 활성 작업에 다시 붙었으면 `reattached`가 True다."""

    job_id: str
    reattached: bool


# ── 공개 오류 — 계약의 일부다. HTTP 계층이 상태 코드로 옮긴다. ─────────────────


class RawWorkspaceError(RuntimeError):
    """사람에게 그대로 보여줄 raw 계층 오류의 기본형."""


class WorkspaceNotFoundError(RawWorkspaceError):
    """열려는 경로가 v2 작업공간이 아니다."""


class WorkspaceVersionError(RawWorkspaceError):
    """schema/version이 이 프로그램이 아는 것과 다르다 — 조용히 마이그레이션하지 않는다."""


class WorkspaceAlreadyExistsError(RawWorkspaceError):
    """이미 있는 작업공간을 새로 만들려 했다."""


class MediaNotAllowedError(RawWorkspaceError):
    """등록·제공이 금지된 대상(보호 경로·심볼릭 링크·작업공간 밖). HTTP는 404로 통한다."""


class MediaNotFoundError(RawWorkspaceError):
    """없는 미디어 ID — 파일이 삭제됐을 때도 같은 말로 응답한다."""


class MediaNotReadyError(RawWorkspaceError):
    """등록은 됐지만 아직 제공할 상태가 아니다. HTTP 409."""


class MediaTargetChangedError(RawWorkspaceError):
    """등록된 자리의 파일이 바뀌었다 — 다시 등록해야 한다. HTTP 409."""


class RangeNotSatisfiableError(RawWorkspaceError):
    """만족할 수 없는 구간 요청. HTTP 416."""


class BrowseNotAllowedError(RawWorkspaceError):
    """탐색할 수 없는 경로 — 없는 곳, 파일, 숨김 항목."""


# ── A2 — SD/폴더 가져오기·원본 순서(계획 §5·§6.2·§13.1) ───────────────────────

INGEST_ASSET_STATUSES: tuple[str, ...] = (
    "pending",
    "copying",
    "verifying",
    "ready",
    "failed",
)
"""가져오기 자산의 상태 전부(§5.3). `ready`만 검증을 마친 원본이고, 그 밖은
중간 상태다 — 검증되지 않은 파일은 후속 처리·브라우저 제공 대상이 아니다."""

INGEST_JOB_KIND = "ingest_copy"
"""가져오기 복사 작업의 종류 표기 — 같은 지문의 활성 작업 재제출이 여기 묶인다."""

ORDER_BASIS_USER_CONFIRMED = "user_confirmed"
"""순서 리비전의 근거 표기 — A2의 확정은 언제나 사람의 명시적 확인이다(§6.2)."""


@dataclass(frozen=True, slots=True)
class SourceAssetDraft:
    """확정 때 만드는 자산 초안 — 출처 신원과 스캔 때 조사값만 담는다.

    `batch_dir`·최종 상대 경로·자산 ID·상태는 작업공간이 배치를 여는 한
    트랜잭션에서 정한다. `source_mtime_ns`·`source_inode`는 **변경 검출**용
    스냅숏이지 촬영 시각이 아니다 — 촬영 시각은 `capture_time`뿐이다.
    """

    source_root: str
    source_relative_path: str
    source_name: str
    size_bytes: int
    source_mtime_ns: int
    source_inode: int
    capture_time: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    ordinal: int
    basis_note: str


@dataclass(frozen=True, slots=True)
class SourceAsset:
    """가져오기 원본 한 건 — 원본 출처(provenance)와 검증 상태의 정본 기록(§13.1).

    `content_sha256`은 목적지를 **다시 읽어** 확인한 값이다(§5.3). `capture_time`은
    촬영 시각의 신뢰할 근거가 생겼을 때만 채운다 — 파일 수정 시각(mtime)을 촬영
    시각으로 대체하지 않는다(§6.2).
    """

    asset_id: str
    batch_dir: str
    relative_path: str
    status: str
    size_bytes: int
    source_root: str
    source_relative_path: str
    source_name: str
    content_sha256: str | None
    file_id: str | None
    duplicate_of: str | None
    capture_time: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    error: str | None
    source_mtime_ns: int
    source_inode: int
    ordinal: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SourceOrderEntry:
    """순서 리비전의 한 항목 — 자산 ID와 확정 순번, 그리고 이 순서의 근거."""

    asset_id: str
    ordinal: int
    basis_note: str


@dataclass(frozen=True, slots=True)
class SourceOrderRevision:
    """확정된 원본 순서 — 불변 기록이다. 바꾸려면 새 리비전을 만든다(§6.5).

    항목은 확정 때 고른 자산(candidate)을 가리키고, 그 자산이 `ready`로 검증된
    뒤에도 같은 ID로 읽힌다 — 순서와 검증 상태가 어긋나지 않는다.
    """

    revision_id: str
    batch_dir: str
    basis: str
    entries: tuple[SourceOrderEntry, ...]
    created_at: str


# ── A2F1 — 가져온 원본 전체(배치를 넘어)의 확정 순서 ─────────────────────────


@dataclass(frozen=True, slots=True)
class GlobalOrderEntry:
    """전체 순서 리비전의 한 항목 — 자산과 그 확정 순번, 소속 배치."""

    asset_id: str
    ordinal: int
    batch_dir: str


@dataclass(frozen=True, slots=True)
class GlobalOrderRevision:
    """모든 배치의 현재 선택(ready) 원본을 묶은 불변 전체 순서 — A3 소비 계약.

    배치 안의 순서는 그 배치의 `SourceOrderRevision`이 그대로 지킨다. 이 리비전은
    배치 **사이**를 잇는 확정 기록이다: 순서가 바뀌면 새 리비전을 만들 뿐, 옛
    스냅숏은 다시 쓰지 않는다(§6.5 — 앞 시각 원본을 늦게 넣어도 옛 패키지는 유지).
    """

    revision_id: str
    basis: str
    entries: tuple[GlobalOrderEntry, ...]
    created_at: str


# ── A2 공개 오류 ─────────────────────────────────────────────────────────────


class IngestError(RawWorkspaceError):
    """가져오기 계층 오류의 기본형."""


class SourceNestingError(IngestError):
    """원본 루트와 작업공간이 서로 안쪽이다 — 재귀 복사·자기 복사가 된다(§5.2)."""


class SourceChangedError(IngestError):
    """원본이 스캔 뒤 바뀌었다 — 확인(확정)이나 복사를 시작할 수 없다(§5.3)."""


class InsufficientSpaceError(IngestError):
    """복사 공간이 부족하다 — 복사 시작 전에 차단한다(§5.2)."""


class NoIngestCandidatesError(IngestError):
    """가져올 영상이 없다 — 빈 확정·빈 복사는 성공으로 위장하지 않는다."""


class IngestCopyError(IngestError):
    """복사·검증 실패 모음 — 작업을 실패 상태로 남겨 재개하게 한다(§5.3)."""


class GlobalOrderError(IngestError):
    """전체 순서 확정의 입력 오류 — 모르는 자산·ready 아님·빠진 원본."""
