"""A1·A2 데이터 계약 — 작업공간·미디어 등록·폴더 탐색·작업 상태·가져오기의 동결 기록.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §13.1·§14.1의 A1 부분과 §5·§6.2의
A2 부분(가져오기 자산·원본 순서 리비전)을 값으로 못 박는다. 여기엔 동작이 없다 —
DB 접근은 `workspace.py`·`jobs.py`, 조립은 `service.py`가 맡는다. 모든 레코드는
frozen이고, 시각 문자열은 UTC ISO-8601다.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
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
    ".jpg",
    ".srt",
    ".vtt",
)
"""미디어로 등록·제공할 수 있는 확장자. 이 목록 밖(.txt·.json·.log·.toml 등)은
종류·위치와 무관하게 등록이 거부된다 — 자격 증명·응답·기록 파일이 우회하지 못한다.
자막(.srt·.vtt)은 **검토·확정 화면이 제한 이름공간(previews/review-vtt/·
final/captions/published/)에 내용 해시 이름으로 쓴 파일만** 등록된다 — 임의의
텍스트 파일은 여전히 어휘·위치 검사에서 거부된다. 썸네일(.jpg)도 같은 규율이다:
영상 검토 화면이 `previews/visual/` 아래에 쓴 파일만 등록되며, 등록 자체는
서비스 경로를 통해서만 일어난다."""

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


class WorkspaceModuleUnavailableError(RawWorkspaceError):
    """이 빌드에는 해당 백엔드 모듈이 없다 — 병합 뒤 활성화된다(조용한 대체 없음)."""


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


# ── A2 남은 몫 — 조사가 묶는 촬영 시각의 상태 ─────────────────────────────────


CAPTURE_STATUS_UNINSPECTED = "uninspected"
CAPTURE_STATUS_INVALID = "invalid"
CAPTURE_STATUS_VALID = "valid"
"""조사자가 촬영 시각에 대해 말할 수 있는 세 가지다. `valid`일 때만
`capture_time`에 값이 들어가고 순서 근거로 쓴다 — `invalid`는 값을 버린 채
'조사했으나 못 믿는다'로 남고, `uninspected`는 아예 묻지 않은 것이다.
어느 쪽도 mtime으로 메워지지 않는다(§6.2)."""


# ── A3 — 결정적 분할·실제 추출·package(계획 §6·§8·§9·§13.1) ───────────────────


SEGMENT_JOB_KIND = "segment_extract"
"""분할 추출 작업의 종류 표기 — 같은 지문의 활성 작업 재제출이 여기 묶인다."""

VISUAL_PREPARE_JOB_KIND = "visual_prepare"
"""영상 검토 미리보기 준비 작업의 종류 표기 — 한 페이지의 없는 항목만 순서대로 만든다."""

SEGMENTATION_BASIS_DETERMINISTIC = "deterministic_tiling"
"""A3 기본 분할 근거 — 의미 분석 없이 전체 재생 구간을 격자 위에 빈틈없이 타일링."""

SEGMENTATION_BASIS_PLANNED = "user_planned"
"""사람이 명시적으로 준 구간으로 타일링한 근거 — 역시 전체 덮음이 계약이다."""

SEGMENT_RECOMMENDATION_NORMAL = "normal"
"""A3 조각의 검토 상태 — 의미 등급(recommended/review_required)은 A4가 붙인다."""

SEGMENT_EXPORT_STATUSES: tuple[str, ...] = (
    "pending",
    "extracting",
    "published",
    "failed",
)
"""조각의 추출 상태 — 서버가 소유한다(계획 §14.1). `published`만 편집용 목록에
오르고, 실패 조각은 이유와 함께 눈에 보인다."""

PACKAGE_STATUSES: tuple[str, ...] = (
    "reserved",
    "publishing",
    "complete",
    "failed",
    "interrupted",
)
"""package의 서버 소유 상태 — 예약이 곧 경합 경계다(같은 package_dir은 하나만)."""

EXTRACT_PROFILES: tuple[str, ...] = ("source_matched", "edit_prores")
"""A3가 노출하는 추출 프로파일 — preprocess의 동결 프로파일과 한 쌍이다(§8.1)."""


@dataclass(frozen=True, slots=True)
class PlannedInterval:
    """사람이 준 구간 하나 — `[start, end)`초(정확한 유리수).

    구간은 **전체를 덮는 타일**이어야 한다: 첫 구간은 0에서 시작하고, 이웃 구간은
    경계를 공유하고, 마지막 구간은 원본 끝을 넘거나 닿는다(넘으면 원본 끝으로
    자른다). 덮지 못하는 입력은 계획이 되지 못하고 이유를 말한다(§6.1).
    """

    start: Fraction
    end: Fraction


@dataclass(frozen=True, slots=True)
class SegmentPlanEntry:
    """확정 분할 계획의 조각 하나 — 불변 기록(§13.1 SegmentPlan).

    시각은 전부 **원본 좌표 정수 틱**이고 `time_base`와 함께 읽는다(§13.3).
    `media_*`는 기본 핸들 0의 값이라 core와 같다 — 핸들은 추출 요청에 붙고,
    실제 매체 범위는 `ClipArtifact`가 측정값으로 남긴다. `export_status`는
    계획이 아니라 서버 소유 상태라 여기 없다.
    """

    segment_id: str
    asset_id: str
    batch_dir: str
    source_order: int
    segment_order: int
    time_base_num: int
    time_base_den: int
    core_start_pts: int
    core_end_pts: int
    media_start_pts: int
    media_end_pts: int
    core_first_frame: int
    core_after_last_frame: int
    source_frame_count: int
    recommendation: str
    basis: str

    @property
    def time_base(self) -> Fraction:
        return Fraction(self.time_base_num, self.time_base_den)

    @property
    def core_range_ticks(self) -> tuple[int, int]:
        return (self.core_start_pts, self.core_end_pts)


@dataclass(frozen=True, slots=True)
class SegmentCoverage:
    """원본 하나의 타일링 증명 — 계획과 함께 얼어 붙는다(§6.1).

    `first_core_start_pts == presentation 원점 틱`, `last_core_end_pts == 정규화된
    재생 종료 틱`, 이웃 조각의 경계 틱 일치, 순번 오름차순 = 시작 오름차순을
    여기 다시 못 박는다. 증명이 아니라 **증거 요약**이다 — 검증은 순수 함수가
    계획 생성 시에 이미 통과시켰다.
    """

    asset_id: str
    source_order: int
    segment_count: int
    source_frame_count: int
    first_core_start_pts: int
    last_core_end_pts: int
    presentation_origin_pts: int
    exact_end_pts: int
    time_base_num: int
    time_base_den: int


@dataclass(frozen=True, slots=True)
class SegmentPlanSnapshot:
    """분할 계획 리비전 — 전체 순서 리비전 위의 불변 스냅샷(§6.5).

    새 계획은 새 리비전일 뿐 옛 스냅샷을 다시 쓰지 않는다. `fingerprint`는
    원본 해시·시계·경계까지 묶은 sha256으로, 추출 작업 지문의 밑천이 된다.
    """

    revision_id: str
    order_revision_id: str
    basis: str
    entries: tuple[SegmentPlanEntry, ...]
    coverage: tuple[SegmentCoverage, ...]
    fingerprint: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ClipArtifact:
    """게시된 조각 파일 한 건의 정본 기록(§13.1 ClipArtifact).

    `content_sha256`은 게시된 **파일을 다시 읽은** 값이다. `core_local_*`·
    `media_local_duration`은 클립 지역 시각(초, 정확한 유리수의 문자열 직렬화)로
    `clips.jsonl`과 같은 값을 가진다. 실패 조각도 `error`와 함께 행으로 남는다 —
    조용히 사라지는 조각은 없다(§6.3).
    """

    clip_id: str
    package_dir: str
    segment_id: str
    asset_id: str
    source_order: int
    segment_order: int
    profile_name: str
    relative_path: str
    status: str
    content_sha256: str | None
    size_bytes: int | None
    file_id: str | None
    time_base_num: int
    time_base_den: int
    core_start_pts: int
    core_end_pts: int
    media_start_pts: int
    media_end_pts: int
    core_local_start: str
    core_local_end: str
    media_local_duration: str
    frame_count: int | None
    error: str | None
    created_at: str
    updated_at: str

    @property
    def time_base(self) -> Fraction:
        """원본 time_base — 조각 계획에서 물려받아 틱 해석에 쓴다(§13.3)."""
        return Fraction(self.time_base_num, self.time_base_den)


@dataclass(frozen=True, slots=True)
class PackageRevision:
    """편집용 폴더 한 벌의 기록 — 경합 예약에서 완전성까지(§13.1 PackageRevision).

    `package_dir`는 작업공간 상대 경로(`clips/package_000001`)이고 유일하다.
    옛 package는 다시 쓰지 않는다 — 새 추출은 새 package 연다(§6.5).
    """

    package_dir: str
    plan_revision_id: str
    order_revision_id: str
    profile_name: str
    handles_before: str
    handles_after: str
    job_fingerprint: str
    status: str
    clip_total: int
    clip_published: int
    error: str | None
    created_at: str
    updated_at: str

    @property
    def complete(self) -> bool:
        return self.status == "complete"


@dataclass(frozen=True, slots=True)
class PackageSummary:
    """화면 3에 그대로 뿌리는 package 요약 — 검증 결과까지 숨기지 않는다."""

    package: PackageRevision
    clips: tuple[ClipArtifact, ...]
    total_duration_seconds: str
    total_bytes: int
    manifest_relative_paths: tuple[str, ...]


# ── A3 공개 오류 ─────────────────────────────────────────────────────────────


class SegmentationError(RawWorkspaceError):
    """분할 계획 오류의 기본형 — 계획이 되지 못한 이유를 말한다."""


class PlanNotFoundError(SegmentationError):
    """없는 분할 계획 리비전 — 다시 계획을 세워야 한다."""


class OrderNotConfirmedError(SegmentationError):
    """확정된 전체 순서가 없다 — A3는 확정 순서 위에서만 계획한다."""


class PackageError(RawWorkspaceError):
    """package 계층 오류 — 예약 경합·경로 위반·완전성 위반."""


class ExtractJobError(RawWorkspaceError):
    """추출 작업 오류 — 실패 조각 정보를 실은 채 작업을 실패로 남긴다."""


# ── A4 — 의미 분석(계획 §7·§14) ──────────────────────────────────────────────


ANALYSIS_JOB_KIND = "understanding_analysis"
"""의미 분석 작업의 종류 표기 — 같은 지문의 활성 작업 재제출이 여기 묶인다."""

ANALYSIS_REVISION_STATUSES: tuple[str, ...] = (
    "partial",
    "reserved",
    "analyzing",
    "awaiting_semantic",
    "complete",
    "failed",
    "interrupted",
)
"""분석 리비전의 서버 소유 상태(§14.1). `awaiting_semantic`은 2단계 실제
호출(A4-F1) — 1단계 전사가 끝났고 두 번째 사람 승인만 남은 대기 상태다.
원본별 결과는 따로 남는다 — 하나의 상태 문자열로 부분 실패를 숨기지 않는다."""

ANALYSIS_SOURCE_STATUSES: tuple[str, ...] = (
    "partial",
    "pending",
    "analyzing",
    "transcribed",
    "analyzed",
    "cached",
    "failed",
    "unanalyzed",
)
"""리비전 안 원본 하나의 분석 상태. `analyzed`·`cached`만 성공 증거를 실고,
`transcribed`는 1단계(내부 ASR)만 끝난 상태다(2단계 승인 대기, A4-F1).
`failed`는 오류를 남긴 채 재시도 대상이며 `unanalyzed`는 선택에 들지 않아
보존만 되는 원본이다(전체 순서는 항상 전부 산다, §6.1)."""

ANALYSIS_MODES: tuple[str, ...] = ("replay", "live")
"""분석 실행 모드 — 재생(replay)은 픽스처만, 실제 호출(live)은 사람 승인만."""

ANALYSIS_PHASES: tuple[str, ...] = ("full", "asr", "semantic")
"""실제 호출 분석의 단계(A4-F1). `full`은 재생의 단일 패스, `asr`는 1단계
(전사만 — 끝나면 `awaiting_semantic`), `semantic`은 2단계(전사 기록 재사용,
Luna만). 잘못된 승인으로 2단계를 시작하면 전사도 Luna도 0송신이다."""

ANALYSIS_SEMANTIC_JOB_KIND = "understanding_semantic"
"""2단계(의미 Luna) 분석 작업의 종류 표기 — 1단계 작업과 재결합 키가 다르다."""

SEGMENTATION_BASIS_SEMANTIC = "semantic_analysis"
"""A4 분할 근거 — 받아들여진 의미 경계를 비디오 격자에 조정한 타일링."""


@dataclass(frozen=True, slots=True)
class AnalysisEstimate:
    """원본 하나의 분석 견적 — 창·프레임·청크 수는 사실, 금액은 상태가 말한다.

    실제 호출은 하나도 일어나지 않는다(§14.3 — 비용 표시 뒤에야 승인받는다).
    Luna 단가는 미확인이므로 금액을 0원으로 계산하지 않고 `price_status`로
    말한다(`unknown` | `user_supplied`).
    """

    asset_id: str
    source_order: int
    source_name: str
    has_audio: bool
    audio_stream_index: int | None
    sample_count: int | None
    video_frame_count: int
    extent_seconds: str
    window_count: int
    frame_sample_count: int
    asr_chunk_count: int
    asr_seconds: str
    luna_request_count: int
    price_status: str
    user_ceiling_total: str | None
    note: str


@dataclass(frozen=True, slots=True)
class AnalysisSemanticEstimate:
    """2단계(의미 Luna) 견적 — 1단계가 남긴 **실측** 전사 기록 위에서 잰다(A4-F1).

    `semantic_fingerprint`는 1단계 기록의 실측 지문이다 — 2단계 승인이 묶는
    바로 그 값이다(승인이 이 값을 보고 만들어졌는지의 증명). 창 수는 실측
    창 계획의 수, 이미지 수는 뽑혀 남은 프레임 수, Luna 요청 수는 창 수다.
    단가는 여전히 미확인 상태로만 말한다(0원 계산 금지).
    """

    asset_id: str
    source_order: int
    source_name: str
    phase: str
    """`semantic`(오디오 원본) | `visual`(무성 원본 — ASR 없음)."""
    semantic_fingerprint: str
    window_count: int
    image_count: int
    luna_model: str
    luna_request_count: int
    price_status: str
    user_ceiling_total: str | None
    note: str


@dataclass(frozen=True, slots=True)
class AnalysisRevision:
    """의미 분석 리비전 — 불변 서버 소유 상태(§13.1 AnalysisWindow 묶음의 실행)."""

    revision_id: str
    order_revision_id: str
    basis: str
    mode: str
    settings_json: str
    models_json: str
    fingerprint: str
    status: str
    source_total: int
    source_done: int
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AnalysisSourceResult:
    """리비전 안 원본 하나의 분석 결과 — 문서 경로는 상대 경로다(§9.2)."""

    revision_id: str
    asset_id: str
    source_order: int
    source_name: str
    fingerprint: str
    status: str
    speech_status: str | None
    execution_mode: str | None
    document_path: str | None
    record_path: str | None
    window_count: int
    segment_count: int
    cached_from: str | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AnalysisSummary:
    """분석 읽기 모델 — 리비전·원본별 결과·요약 읽기(화면 2의 분석 패널)."""

    revision: AnalysisRevision
    results: tuple[AnalysisSourceResult, ...]


class AnalysisError(RawWorkspaceError):
    """의미 분석 계층 오류의 기본형 — 이유를 사람에게 그대로 보여준다."""
