"""UI·CLI가 공유하는 v2 작업공간 공개 서비스(계획 §4·§15).

`ui/workspace_service.py`가 가져올 수 있는 **정확히 이 모듈**이다(A0 계약 예외).
여기엔 gradio·fastapi가 없다 — HTTP와 화면 조립은 UI 계층의 몫이고, 이 계층은
작업공간·등록·탐색·작업·접근 정책을 값으로 다룬다. 네트워크 호출도 없다: 오프라인
기본은 `config.RawWorkflowConfig`가 이미 못 박고 있고 이 서비스는 그 값을 읽기만
한다(§14.3 — UI가 우회하지 않는다).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import preprocess
from vlogsrt.project.raw import analysis_workflow, segmentation
from vlogsrt.project.raw import package as package_layout
from vlogsrt.project.raw import runner as extract_runner
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    LiveAnalysisTransport,
    asr_settings_fingerprint,
)
from vlogsrt.project.raw.analysis_config import (
    RawUnderstandingSettings,
    validate_understanding_settings,
)
from vlogsrt.project.raw.analysis_store import ANALYSIS_ROOT, AnalysisStore
from vlogsrt.project.raw.config import (
    WORKFLOW_SCHEMA_VERSION,
    RawWorkflowConfig,
    validate_raw_workflow_config,
)
from vlogsrt.project.raw.ingest import (
    IngestConfirmation,
    IngestProposal,
    MediaInspect,
    SourceInspection,
    confirm_ingest,
    ffprobe_media_inspector,
    reconfirm_ingest,
    run_ingest_copy,
    scan_sources,
)
from vlogsrt.project.raw.jobs import JobContext, JobManager
from vlogsrt.project.raw.models import (
    ACTIVE_JOB_STATUSES,
    ANALYSIS_JOB_KIND,
    ANALYSIS_MODES,
    ANALYSIS_PHASES,
    ANALYSIS_SEMANTIC_JOB_KIND,
    EXTRACT_PROFILES,
    INGEST_JOB_KIND,
    MEDIA_KINDS,
    MEDIA_SUFFIXES,
    ORDER_BASIS_USER_CONFIRMED,
    SEGMENT_JOB_KIND,
    SEGMENTATION_BASIS_DETERMINISTIC,
    SEGMENTATION_BASIS_PLANNED,
    SEGMENTATION_BASIS_SEMANTIC,
    AnalysisError,
    AnalysisEstimate,
    AnalysisRevision,
    AnalysisSemanticEstimate,
    AnalysisSourceResult,
    AnalysisSummary,
    BrowseNotAllowedError,
    ByteRange,
    ClipArtifact,
    ExtractJobError,
    FolderEntry,
    FolderListing,
    GlobalOrderEntry,
    GlobalOrderError,
    GlobalOrderRevision,
    IngestError,
    JobHandle,
    JobRecord,
    MediaNotAllowedError,
    MediaNotFoundError,
    MediaNotReadyError,
    MediaRegistration,
    MediaTargetChangedError,
    OrderNotConfirmedError,
    PackageError,
    PackageRevision,
    PackageSummary,
    PlannedInterval,
    PlanNotFoundError,
    RangeNotSatisfiableError,
    RawWorkspaceError,
    ResolvedMedia,
    SegmentationError,
    SegmentCoverage,
    SegmentPlanEntry,
    SegmentPlanSnapshot,
    SourceAsset,
    SourceOrderRevision,
    WorkspaceStatus,
)
from vlogsrt.project.raw.source_order import propose_global_order as _propose_global_order
from vlogsrt.project.raw.understanding_luna import (
    AnalysisBudget,
    OnlineAnalysisApproval,
    OnlineVisualAnalysisApproval,
    estimate_luna_cost,
    online_env_blocked,
)
from vlogsrt.project.raw.workspace import (
    DB_SCHEMA_VERSION,
    RawWorkspace,
    new_segment_plan_revision_id,
)

__all__ = [
    # UI는 이 모듈만 가져갈 수 있다(정확 경로 예외) — 계약 오류도 여기서 다시 낸다.
    "ACTIVE_JOB_STATUSES",
    "ANALYSIS_JOB_KIND",
    "ANALYSIS_MODES",
    "ANALYSIS_PHASES",
    "ANALYSIS_ROOT",
    "ANALYSIS_SEMANTIC_JOB_KIND",
    "AnalysisError",
    "AnalysisEstimate",
    "AnalysisRevision",
    "AnalysisSemanticEstimate",
    "AnalysisSourceResult",
    "AnalysisSummary",
    "AsrPhaseApproval",
    "BrowseNotAllowedError",
    "ByteRange",
    "ClipArtifact",
    "EXTRACT_PROFILES",
    "ExtractJobError",
    "FolderEntry",
    "FolderListing",
    "GlobalOrderEntry",
    "GlobalOrderError",
    "GlobalOrderRevision",
    "INGEST_JOB_KIND",
    "IngestConfirmation",
    "IngestError",
    "IngestProposal",
    "JobContext",
    "JobHandle",
    "JobRecord",
    "LiveAnalysisTransport",
    "MEDIA_KINDS",
    "MediaInspect",
    "MediaNotAllowedError",
    "MediaNotFoundError",
    "MediaNotReadyError",
    "MediaRegistration",
    "MediaTargetChangedError",
    "OrderNotConfirmedError",
    "PackageError",
    "PackageRevision",
    "PackageSummary",
    "PlannedInterval",
    "PlanNotFoundError",
    "RangeNotSatisfiableError",
    "RawWorkspaceError",
    "RawWorkspaceService",
    "ResolvedMedia",
    "SEGMENT_JOB_KIND",
    "SEGMENTATION_BASIS_SEMANTIC",
    "SegmentCoverage",
    "SegmentPlanEntry",
    "SegmentPlanSnapshot",
    "SegmentationError",
    "SourceAsset",
    "SourceInspection",
    "SourceOrderRevision",
    "WorkspaceStatus",
    "browse_folder",
    "content_type_for",
    "ffprobe_media_inspector",
    "host_header_allowed",
    "loopback_hosts",
    "mounted_device_candidates",
    "mutation_request_allowed",
    "origin_header_allowed",
    "parse_range",
    "referer_header_allowed",
]

MEDIA_CONTENT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


def _ingest_body(
    workspace: RawWorkspace, confirmation: IngestConfirmation
) -> Callable[[JobContext], None]:
    """복사 작업 본문 묶기 — 호출 시점의 확인 객체를 즉시 묶는다(늦은 묶음 없이)."""

    def body(context: JobContext) -> None:
        run_ingest_copy(context, workspace, confirmation)

    return body


def _extract_body(
    workspace: RawWorkspace,
    plan: SegmentPlanSnapshot,
    package: PackageRevision,
    assets: Mapping[str, SourceAsset],
    handles: preprocess.RawClipHandles,
) -> Callable[[JobContext], None]:
    """추출 작업 본문 묶기 — 예약된 package와 계획을 즉시 묶는다(§14.1)."""

    def body(context: JobContext) -> None:
        extract_runner.run_segment_extraction(context, workspace, plan, package, assets, handles)

    return body


def _analysis_body(
    workspace: RawWorkspace,
    revision: AnalysisRevision,
    order: GlobalOrderRevision,
    assets: Mapping[str, SourceAsset],
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: analysis_workflow.AnalysisProviders,
) -> Callable[[JobContext], None]:
    """분석 작업 본문 묶기 — 리비전·선택·호출 경계를 즉시 묶는다(§14.1)."""

    def body(context: JobContext) -> None:
        analysis_workflow.run_analysis(
            context, workspace, revision, order, assets, settings, workflow, providers
        )

    return body


def _semantic_body(
    workspace: RawWorkspace,
    revision: AnalysisRevision,
    order: GlobalOrderRevision,
    assets: Mapping[str, SourceAsset],
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: analysis_workflow.AnalysisProviders,
) -> Callable[[JobContext], None]:
    """2단계(의미) 작업 본문 묶기 — 같은 본문, 단계는 공급자가 이미 말한다."""

    def body(context: JobContext) -> None:
        analysis_workflow.run_analysis(
            context, workspace, revision, order, assets, settings, workflow, providers
        )

    return body


def _analysis_estimate_for(
    workspace: RawWorkspace,
    asset: SourceAsset,
    source_order: int,
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    window_width: Fraction,
) -> AnalysisEstimate:
    """원본 하나의 견적 — 창 수·청크 수는 측정값, 이미지 수는 상한, 금액은 상태.

    오디오 분량은 아직 PCM을 뽑지 않았으므로 컨테이너 선언 길이로 잰다(추정임을
    note가 말한다). 실제 실행은 이 값이 아니라 실측 원본·실측 분량으로 승인과
    맞춰진다(§14.3).
    """
    path = workspace.root / asset.relative_path
    probe = preprocess.probe_raw_media(path)
    try:
        clock = preprocess.probe_extraction_clock(path, probe=probe)
    except RuntimeError as error:
        return AnalysisEstimate(
            asset_id=asset.asset_id,
            source_order=source_order,
            source_name=asset.source_name,
            has_audio=bool(probe.audio),
            audio_stream_index=probe.audio[0].stream_index if probe.audio else None,
            sample_count=None,
            video_frame_count=0,
            extent_seconds="0",
            window_count=0,
            frame_sample_count=0,
            asr_chunk_count=0,
            asr_seconds="0",
            luna_request_count=0,
            price_status="unknown",
            user_ceiling_total=None,
            note=f"비디오 격자 조사 실패 — 분석 지원 밖이다: {error}",
        )
    note = "창·청크 수는 측정값, 이미지 수는 상한, 오디오 분량은 선언 길이 추정이다"
    audio_seconds = Fraction(0)
    if not clock.uniform_grid:
        return AnalysisEstimate(
            asset_id=asset.asset_id,
            source_order=source_order,
            source_name=asset.source_name,
            has_audio=bool(probe.audio),
            audio_stream_index=probe.audio[0].stream_index if probe.audio else None,
            sample_count=None,
            video_frame_count=clock.frame_count,
            extent_seconds=str(clock.exact_end - clock.presentation_origin),
            window_count=0,
            frame_sample_count=0,
            asr_chunk_count=0,
            asr_seconds="0",
            luna_request_count=0,
            price_status="unknown",
            user_ceiling_total=None,
            note=f"원본이 균일 격자가 아니다(VFR) — 분석 지원 밖이다: {clock.grid_note}",
        )
    video_extent = clock.frame_count * clock.frame_duration
    extent = video_extent
    if probe.audio:
        audio_seconds = max(Fraction(0), probe.duration)
        extent = max(extent, audio_seconds)
    window_count = max(1, math.ceil(extent / window_width))
    chunk_bound = min(
        int(float(settings.asr_chunk_max_seconds) * 16000),
        settings.asr_chunk_max_bytes // (2 * (probe.audio[0].channels if probe.audio else 1)),
    )
    asr_chunks = max(1, math.ceil(audio_seconds * 16000 / chunk_bound)) if probe.audio else 0
    estimate = estimate_luna_cost(
        window_count,
        AnalysisBudget(max_luna_requests=window_count, max_asr_chunks=max(1, asr_chunks)),
    )
    return AnalysisEstimate(
        asset_id=asset.asset_id,
        source_order=source_order,
        source_name=asset.source_name,
        has_audio=bool(probe.audio),
        audio_stream_index=probe.audio[0].stream_index if probe.audio else None,
        sample_count=int(audio_seconds * 16000) if probe.audio else None,
        video_frame_count=clock.frame_count,
        extent_seconds=str(extent),
        window_count=window_count,
        frame_sample_count=window_count * settings.max_images_per_window,
        asr_chunk_count=asr_chunks,
        asr_seconds=str(audio_seconds),
        luna_request_count=estimate.request_count,
        price_status=estimate.price_status,
        user_ceiling_total=(
            str(estimate.user_ceiling_total) if estimate.user_ceiling_total is not None else None
        ),
        note=f"{note}. Luna: {estimate.note}",
    )


BROWSE_ENTRY_LIMIT = 500
"""한 번의 명시적 탐색이 보여주는 최대 항목 수 — 재귀 스캔이 아닌 한 층이다(§4.2)."""


# ── 접근 정책 — 순수 함수. HTTP 계층은 이 판정만 실행한다. ────────────────────


def loopback_hosts(port: int) -> frozenset[str]:
    """이 서버가 인정하는 Host — loopback 고정, 포트까지 일치(§4.1·§4.4)."""
    return frozenset({"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"})


def host_header_allowed(host: str | None, port: int) -> bool:
    """Host 검사 — DNS 재바인딩으로 다른 이름이 들어오면 끊는다."""
    return host is not None and host.strip().lower() in loopback_hosts(port)


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def origin_header_allowed(origin: str | None, port: int) -> bool:
    """변경 요청의 Origin — 우리 페이지와 같은 근원만 인정한다."""
    return origin is not None and origin.strip().rstrip("/") in allowed_origins(port)


def referer_header_allowed(referer: str | None, port: int) -> bool:
    """Origin이 없는 브라우저를 위한 보조 검사 — 근원 접두만 인정한다."""
    if referer is None:
        return False
    return any(
        referer.startswith(f"{origin}/") or referer == origin for origin in allowed_origins(port)
    )


def mutation_request_allowed(origin: str | None, referer: str | None, port: int) -> bool:
    """변경(POST 등) 요청의 보호 — 머리글 없는 요청은 신뢰하지 않는다.

    Origin이 오면 우리 근원과 정확히 같아야 하고, 안 왔으면 Referer로, 둘 다
    없으면 거부한다. 브라우저의 같은 근원 POST는 항상 Origin(없어도 Referer)을
    싣는다 — 머리글 없는 변경 요청은 스크립트·재생 공격이지 로컬 도구가 아니다
    (로컬 진단은 GET으로 충분하다).
    """
    if origin is not None:
        return origin_header_allowed(origin, port)
    if referer is not None:
        return referer_header_allowed(referer, port)
    return False


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """단일 bytes 구간을 해석한다 — None이면 전체(200), 조건 위반은 416.

    알 수 없는 단위·모양이 어긋난 헤더는 RFC 9110대로 무시한다(전체 제공).
    여러 구간은 우리 계약에 없다 — 역시 전체로 응답한다.
    """
    if header is None or size <= 0:
        return None
    unit, _, specification = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or not specification or "," in specification:
        return None
    first, separator, last = specification.strip().partition("-")
    if not separator:
        return None
    first, last = first.strip(), last.strip()
    try:
        if not first:
            return _suffix_range(last, size)
        start = int(first)
        if start < 0 or start >= size:
            raise RangeNotSatisfiableError(f"구간이 파일 크기({size})를 벗어났다")
        if not last:
            return ByteRange(start, size - 1)
        end = int(last)
        if end < start:
            raise RangeNotSatisfiableError("구간의 끝이 시작보다 앞이다")
        return ByteRange(start, min(end, size - 1))
    except ValueError:
        return None


def _suffix_range(last: str, size: int) -> ByteRange:
    length = int(last)
    if length <= 0:
        raise RangeNotSatisfiableError("접미 구간의 길이는 1 이상이어야 한다")
    return ByteRange(max(0, size - length), size - 1)


def content_type_for(path: Path) -> str:
    return MEDIA_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


# ── 폴더 탐색 — 명시적 요청 한 번, 한 층. 내용은 읽지 않는다(§4.2). ────────────


def browse_folder(raw_path: str, base: Path | None = None) -> FolderListing:
    """사용자가 지정한 폴더 한 층을 나열한다.

    점으로 시작하는 항목(.env·.env.example·자격 증명 파일이 사는 자리)은 목록에
    올리지 않는다. 심볼릭 링크 항목은 따라가지 않고 건너뛴다. 파일 내용은
    절대 열지 않는다 — 이름·종류만 본다.
    """
    target = _browse_target(raw_path, base)
    entries: list[FolderEntry] = []
    with os.scandir(target) as scan:
        for item in sorted(scan, key=lambda entry: entry.name):
            if len(entries) >= BROWSE_ENTRY_LIMIT:
                break
            if item.name.startswith("."):
                continue
            if item.is_symlink():
                continue
            if item.is_dir(follow_symlinks=False):
                entries.append(
                    FolderEntry(
                        name=item.name,
                        path=str(item.path),
                        is_directory=True,
                        is_media_candidate=False,
                    )
                )
            elif item.is_file(follow_symlinks=False) and Path(item.name).suffix.lower() in (
                MEDIA_SUFFIXES
            ):
                entries.append(
                    FolderEntry(
                        name=item.name,
                        path=str(item.path),
                        is_directory=False,
                        is_media_candidate=True,
                    )
                )
    parent = str(target.parent) if str(target.parent) != str(target) else None
    return FolderListing(path=str(target), parent=parent, entries=tuple(entries))


def _browse_target(raw_path: str, base: Path | None) -> Path:
    text = (raw_path or "").strip().strip("\"'“”‘’").strip()
    if not text:
        raise BrowseNotAllowedError("폴더 경로를 입력하세요")
    target = Path(text).expanduser()
    if not target.is_absolute() and base is not None:
        target = base / target
    if not target.exists():
        raise BrowseNotAllowedError(f"없는 경로다: {target}")
    if not target.is_dir():
        raise BrowseNotAllowedError(f"폴더가 아니다: {target}")
    return target


def mounted_device_candidates(bases: tuple[Path, ...] | None = None) -> tuple[Path, ...]:
    """마운트된 장치 후보 — 버튼을 눌렀을 때만 본다(§4.2). 후보를 보여도 조사하지 않는다."""
    roots = bases if bases is not None else (Path("/Volumes"), Path("/media"))
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for item in sorted(root.iterdir(), key=lambda path: path.name):
            if item.name.startswith(".") or item.is_symlink() or not item.is_dir():
                continue
            candidates.append(item)
    return tuple(candidates)


# ── 공개 서비스 ──────────────────────────────────────────────────────────────


class RawWorkspaceService:
    """작업공간·미디어·작업의 공개 조립점 — 화면 새로고침과 무관하게 상태는 DB에 산다.

    작업공간별 JobManager는 **한 서비스 인스턴스에 정확히 하나**다. 잠금 없는 느린
    생성은 동시 첫 열기에서 관리자를 여럿 만들고, 두 번째 관리자의 시작 복구가
    살아 있는 작업을 중단으로 표시할 수 있다 — 그래서 캐시 전체를 자물쇠로 감싼다.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()  # _jobs가 _workspace를 안에서 다시 지난다
        self._workspaces: dict[Path, RawWorkspace] = {}
        self._job_managers: dict[Path, JobManager] = {}

    def _workspace(self, root: Path) -> RawWorkspace:
        key = root.expanduser().resolve()
        with self._lock:
            workspace = self._workspaces.get(key)
            if workspace is None:
                workspace = RawWorkspace.open(key)
                self._workspaces[key] = workspace
            return workspace

    def _jobs(self, root: Path) -> JobManager:
        key = root.expanduser().resolve()
        with self._lock:
            manager = self._job_managers.get(key)
            if manager is None:
                manager = JobManager(self._workspace(key))
                self._job_managers[key] = manager
            return manager

    # ── 작업공간 ───────────────────────────────────────────────

    def create_workspace(self, root: Path, name: str) -> WorkspaceStatus:
        workspace = RawWorkspace.create(root, name)
        self._workspaces[workspace.root] = workspace
        return self.workspace_status(workspace.root)

    def open_workspace(self, root: Path) -> WorkspaceStatus:
        """작업공간을 연다 — 작업 복구는 하지 않는다(서버 시작 때 한 번만, §14.1)."""
        workspace = self._workspace(root)
        return self.workspace_status(workspace.root)

    def workspace_status(self, root: Path) -> WorkspaceStatus:
        workspace = self._workspace(root)
        config = workspace.load_workflow()
        active = sum(
            1 for job in self._jobs(workspace.root).list_jobs() if job.status in ACTIVE_JOB_STATUSES
        )
        return WorkspaceStatus(
            workspace_root=str(workspace.root),
            workspace_id=workspace.workspace_id(),
            name=workspace.name,
            workflow_schema_version=WORKFLOW_SCHEMA_VERSION,
            db_schema_version=DB_SCHEMA_VERSION,
            network_mode=config.network_mode,
            created_at=workspace.created_at,
            media_count=workspace.media_count(),
            active_jobs=active,
        )

    def workspace_id(self, root: Path) -> str:
        """작업공간의 불변 신원 — 화면 선택 검증·재개 승인의 기준 토큰."""
        return self._workspace(root).workspace_id()

    # ── 설정 ───────────────────────────────────────────────────

    def settings(self, root: Path) -> RawWorkflowConfig:
        return self._workspace(root).load_workflow()

    def update_settings(self, root: Path, config: RawWorkflowConfig) -> None:
        """설정을 검증하고 저장한다 — 검증을 통과하지 못하면 아무 것도 바뀌지 않는다."""
        validate_raw_workflow_config(config)
        self._workspace(root).save_workflow(config)

    # ── 미디어 등록·해석 ───────────────────────────────────────

    def register_media(self, root: Path, path: Path, kind: str) -> MediaRegistration:
        if kind not in MEDIA_KINDS:
            raise ValueError(f"알 수 없는 미디어 종류: {kind}")
        return self._workspace(root).register_media_file(path, kind)

    def list_media(self, root: Path, kind: str | None = None) -> tuple[MediaRegistration, ...]:
        return self._workspace(root).list_media(kind)

    def resolve_media(self, root: Path, file_id: str) -> ResolvedMedia:
        """ID 하나를 검증해 실제 미디어로 푼다 — 제공 계층이 경로를 얻는 유일한 길."""
        path, size, kind = self._workspace(root).resolve_media_file(file_id)
        return ResolvedMedia(
            file_id=file_id,
            kind=kind,
            path=path,
            size_bytes=size,
            content_type=content_type_for(path),
        )

    # ── 작업 ───────────────────────────────────────────────────

    def submit_job(
        self,
        root: Path,
        kind: str,
        input_fingerprint: str,
        run: Callable[[JobContext], None],
        total: int = 0,
    ) -> JobHandle:
        return self._jobs(root).submit(kind, input_fingerprint, run, total)

    def cancel_job(self, root: Path, job_id: str) -> JobRecord:
        return self._jobs(root).cancel(job_id)

    def get_job(self, root: Path, job_id: str) -> JobRecord:
        return self._jobs(root).get(job_id)

    def list_jobs(self, root: Path, limit: int = 50) -> tuple[JobRecord, ...]:
        return self._jobs(root).list_jobs(limit)

    def resume_job(
        self,
        root: Path,
        job_id: str,
        run: Callable[[JobContext], None],
        expected_fingerprint: str,
    ) -> JobHandle:
        """명시적 재개 — interrupted·paused·failed 작업에만 새 본문을 다시 묶는다.

        작업 ID와 진행은 그대로 두고, **현재 입력/설정 지문이 비지 않고** 기록된
        것과 같을 때만 실행한다(코디네이터 재결 — 빈 지문의 조용한 재결합 금지).
        새로고침·열기가 이 길을 건드리지 않는다 — 재개는 사람의 명시적 호출뿐이다.
        A2/A3가 실제 가져오기·추출 본문을 이 자리에 묶는다.
        """
        if not expected_fingerprint.strip():
            raise RawWorkspaceError(
                "재개에는 현재 입력/설정 지문이 필요하다 — 빈 지문은 재결합하지 않는다"
            )
        return self._jobs(root).resume(job_id, run, expected_fingerprint)

    def recover_interrupted_jobs(self, root: Path) -> int:
        """서버 시작 경로 전용 — 진행 중 작업을 재개 가능 상태로 돌린다(§14.1)."""
        return self._jobs(root).recover_interrupted()

    # ── A2 — SD/폴더 가져오기·원본 순서 ───────────────────────

    def scan_sources(
        self,
        root: Path,
        source_root: Path,
        inspect: MediaInspect = ffprobe_media_inspector,
    ) -> IngestProposal:
        """원본 폴더를 발견·조사해 제안을 만든다 — 읽기만 한다(§5.1의 확인 단계 전)."""
        workspace = self._workspace(root)
        return scan_sources(source_root, workspace.root, inspect)

    def confirm_ingest(self, root: Path, proposal: IngestProposal) -> IngestConfirmation:
        """사람의 명시적 확인을 받은 제안만 확정한다 — 배치·자산·불변 순서 리비전."""
        workspace = self._workspace(root)
        return confirm_ingest(workspace, proposal)

    def start_ingest_copy(self, root: Path, confirmation: IngestConfirmation) -> JobHandle:
        """확정된 순서 그대로 복사·검증 작업을 제출한다 — 같은 지문의 활성 작업은 하나다."""
        workspace = self._workspace(root)
        return self.submit_job(
            root,
            INGEST_JOB_KIND,
            confirmation.job_fingerprint,
            _ingest_body(workspace, confirmation),
            total=len(confirmation.assets),
        )

    def retry_ingest_copy(self, root: Path, job_id: str) -> JobHandle:
        """실패·중단된 가져오기의 명시적 재시도 — 같은 작업 ID·같은 지문으로 다시 붙는다.

        확인 객체는 DB의 배치 기록에서 다시 만든다(`reconfirm_ingest`). 지문이
        어긋나면(원본이 바뀌었다면) 재결합하지 않고 새 가져오기를 요구한다.
        """
        workspace = self._workspace(root)
        record = self.get_job(root, job_id)
        if record.kind != INGEST_JOB_KIND:
            raise IngestError(f"가져오기 작업이 아니다: {job_id} ({record.kind})")
        for revision in workspace.list_order_revisions():
            confirmation = reconfirm_ingest(workspace, revision.batch_dir)
            if confirmation.job_fingerprint == record.input_fingerprint:
                return self.resume_job(
                    root,
                    job_id,
                    _ingest_body(workspace, confirmation),
                    record.input_fingerprint,
                )
        raise IngestError(
            f"작업 {job_id}의 지문과 같은 가져오기 배치를 찾지 못했다 — "
            "원본이 바뀌었으면 다시 조사·확정하세요"
        )

    def ingest_overview(
        self, root: Path, batch_dir: str | None = None
    ) -> tuple[tuple[SourceAsset, ...], SourceOrderRevision | None, tuple[JobRecord, ...]]:
        """가져오기 읽기 모델 — 자산(확정 순번순)·순서 리비전·가져오기 작업 최근 순."""
        workspace = self._workspace(root)
        assets = workspace.list_source_assets(batch_dir)
        revision = None
        if assets:
            revision = workspace.order_revision(assets[0].batch_dir)
        elif batch_dir is not None:
            revision = workspace.order_revision(batch_dir)
        jobs = tuple(job for job in self.list_jobs(root) if job.kind == INGEST_JOB_KIND)
        return assets, revision, jobs

    def list_source_assets(
        self, root: Path, batch_dir: str | None = None
    ) -> tuple[SourceAsset, ...]:
        return self._workspace(root).list_source_assets(batch_dir)

    def list_order_revisions(self, root: Path) -> tuple[SourceOrderRevision, ...]:
        return self._workspace(root).list_order_revisions()

    # ── A2F1 — 배치를 넘는 전체 순서 확정(A3 소비 계약) ────────

    def global_order_proposal(self, root: Path) -> tuple[SourceAsset, ...]:
        """전체 순서 제안 — 현재 ready 원본을 배치 연대순·배치 안 확정 순번으로."""
        ready = [
            asset for asset in self._workspace(root).list_source_assets() if asset.status == "ready"
        ]
        return _propose_global_order(ready)

    def confirm_global_source_order(
        self, root: Path, asset_ids: Sequence[str] | None = None
    ) -> GlobalOrderRevision:
        """현재 선택(ready) 원본 전부의 전체 순서를 확정한다 — 불변 리비전.

        순서를 주지 않으면 제안(배치 연대순·배치 안 확정 순번) 그대로다. 주면
        **지금 ready인 원본 전부**를 빠짐없이, 겹침 없이, 알맞은 ID로 담아야 한다 —
        모르는 ID·ready 아님·빠진 원본은 확정을 만들지 않고 알린다. 확정은 새
        리비전을 만들 뿐: 옛 스냅숏은 다시 쓰지 않고, 배치 안 리비전도 건드리지
        않는다(옛 패키지를 조용히 다시 섞지 않는다, §6.5).
        """
        workspace = self._workspace(root)
        ready = {asset.asset_id: asset for asset in self.global_order_proposal(root)}
        if not ready:
            raise GlobalOrderError("확정할 ready 원본이 없다 — 가져오기(복사·검증)를 먼저 끝내세요")
        ordered_ids = self._validated_global_ids(asset_ids, ready)
        entries = tuple(
            GlobalOrderEntry(
                asset_id=asset_id,
                ordinal=index + 1,
                batch_dir=ready[asset_id].batch_dir,
            )
            for index, asset_id in enumerate(ordered_ids)
        )
        return workspace.create_global_order_revision(entries, ORDER_BASIS_USER_CONFIRMED)

    @staticmethod
    def _validated_global_ids(
        asset_ids: Sequence[str] | None, ready: dict[str, SourceAsset]
    ) -> list[str]:
        """전체 순서 입력 검사 — 빈 값은 제안 순서, 그 외는 완전·유일해야 한다."""
        if asset_ids is None:
            return list(ready)
        given = [item.strip() for item in asset_ids if item.strip()]
        unknown = [item for item in given if item not in ready]
        if unknown:
            known_hint = ", ".join(sorted(ready)[:3])
            raise GlobalOrderError(
                f"모르는 자산 ID가 있다: {', '.join(unknown[:3])}"
                f" — 현재 ready 원본의 ID만 쓸 수 있다(예: {known_hint}…)"
            )
        duplicates = sorted({item for item in given if given.count(item) > 1})
        if duplicates:
            raise GlobalOrderError(f"중복된 자산 ID가 있다: {', '.join(duplicates[:3])}")
        missing = [asset_id for asset_id in ready if asset_id not in given]
        if missing:
            raise GlobalOrderError(
                f"순서에서 빠진 ready 원본이 {len(missing)}건이다"
                f" (예: {', '.join(missing[:3])}) — 전체 확정은 현재 ready 전부를"
                " 담아야 한다"
            )
        return given

    def latest_global_source_order(self, root: Path) -> GlobalOrderRevision | None:
        return self._workspace(root).latest_global_order_revision()

    def list_global_source_orders(self, root: Path) -> tuple[GlobalOrderRevision, ...]:
        return self._workspace(root).list_global_order_revisions()

    # ── A3 — 결정적 분할 계획(확정 전체 순서 위의 불변 리비전) ──

    def plan_segmentation(
        self,
        root: Path,
        order_revision_id: str | None = None,
        intervals: Mapping[str, Sequence[PlannedInterval]] | None = None,
        tile_seconds: Fraction = segmentation.DEFAULT_TILE_SECONDS,
        basis: str | None = None,
    ) -> SegmentPlanSnapshot:
        """확정된 전체 순서 위에서 원본별 시계를 잰 뒤 분할 계획 리비전을 만든다.

        원본은 **지금 다시 검증한다**(크기·SHA-256 재독출) — 계획의 지문이 원본
        내용을 묶으므로, 검증된 적 없거나 그 사이 바뀐 원본 위에 계획을 세우지
        않는다(§14.2). 구간을 주면 그 격자로, 안 주면 균일 타일로 전체를 덮는다.
        새 계획은 새 리비전일 뿐 옛 계획을 다시 쓰지 않는다(§6.5). `basis`는
        구간의 출처 표기다(기본: 사람 구간/결정적 타일) — A4 의미 분석이
        `plan_segmentation_from_analysis`로 조정 구간을 줄 때만 semantic 값을 쓴다.
        """
        workspace = self._workspace(root)
        revision = self._order_revision(root, order_revision_id)
        planned = intervals or {}
        unknown = sorted(set(planned) - {entry.asset_id for entry in revision.entries})
        if unknown:
            raise SegmentationError(
                f"순서 리비전에 없는 자산에게 구간을 주었다: {', '.join(unknown[:3])}"
            )
        entries: list[SegmentPlanEntry] = []
        coverage: list[SegmentCoverage] = []
        clocks: dict[str, dict[str, object]] = {}
        for order_entry in sorted(revision.entries, key=lambda item: item.ordinal):
            asset = _verified_asset(workspace, order_entry.asset_id)
            facts = _measured_source(workspace, asset)
            spans, proof = segmentation.plan_segments(
                asset_id=asset.asset_id,
                batch_dir=order_entry.batch_dir,
                source_order=order_entry.ordinal,
                time_base=facts.clock.time_base,
                presentation_origin_pts=facts.clock.first_pts,
                frame_count=facts.clock.frame_count,
                frame_duration=facts.clock.frame_duration,
                exact_end_pts=facts.exact_end_pts,
                intervals=planned.get(asset.asset_id),
                order_revision_id=revision.revision_id,
                tile_seconds=tile_seconds,
            )
            entries.extend(spans)
            coverage.append(proof)
            clocks[asset.asset_id] = _plan_fingerprint_inputs(asset, facts, spans)
        snapshot = SegmentPlanSnapshot(
            revision_id=new_segment_plan_revision_id(),
            order_revision_id=revision.revision_id,
            basis=basis
            or (SEGMENTATION_BASIS_PLANNED if planned else SEGMENTATION_BASIS_DETERMINISTIC),
            entries=tuple(
                sorted(entries, key=lambda item: (item.source_order, item.segment_order))
            ),
            coverage=tuple(coverage),
            fingerprint=segmentation.segmentation_fingerprint(revision.revision_id, clocks),
            created_at=_plan_timestamp(),
        )
        workspace.insert_segment_plan_revision(snapshot)
        return snapshot

    def segment_plan_revision(self, root: Path, revision_id: str) -> SegmentPlanSnapshot:
        return self._workspace(root).segment_plan_revision(revision_id)

    def list_segment_plan_revisions(self, root: Path) -> tuple[SegmentPlanSnapshot, ...]:
        return self._workspace(root).list_segment_plan_revisions()

    def latest_segment_plan(self, root: Path) -> SegmentPlanSnapshot | None:
        revisions = self.list_segment_plan_revisions(root)
        return revisions[-1] if revisions else None

    # ── A3 — 순차 추출 작업 제출·재개 ───────────────────────────

    def start_segment_extraction(
        self,
        root: Path,
        plan_revision_id: str | None = None,
        profile_name: str = "source_matched",
        handles_before: Fraction = Fraction(0),
        handles_after: Fraction = Fraction(0),
    ) -> JobHandle:
        """분할 계획을 실제 조각 폴더로 추출하는 작업을 제출한다 — API 없음(§14.3).

        같은 지문(계획·프로파일·핸들·도구)의 완성된 package가 있으면 다시 만들지
        않고 알린다. 진행 중이거나 실패한 package가 있으면 그 package를 이어
        쓴다 — 같은 작업 재개가 조각 ID·번호를 유지하는 길이다(§6.3).
        """
        workspace = self._workspace(root)
        snapshot = self._plan_snapshot(root, plan_revision_id)
        profile = extract_runner.profile_for(profile_name)
        assets = self._plan_assets(root, snapshot)
        handles = _validated_handles(handles_before, handles_after)
        fingerprint = extract_runner.extraction_fingerprint(
            snapshot.fingerprint, profile.name, handles, preprocess.ffmpeg_provenance()
        )
        package = self._reusable_or_reserved_package(
            workspace, snapshot, profile.name, handles, fingerprint
        )
        return self.submit_job(
            root,
            SEGMENT_JOB_KIND,
            fingerprint,
            _extract_body(workspace, snapshot, package, assets, handles),
            total=len(snapshot.entries),
        )

    def retry_segment_extraction(self, root: Path, job_id: str) -> JobHandle:
        """실패·중단된 추출의 명시적 재시도 — 같은 작업 ID·같은 지문으로 다시 붙는다.

        계획·프로파일·핸들은 package 행에서 다시 만들고(`reconfirm_extraction`),
        지문은 **지금의 도구 버전으로 다시 계산해** 기록과 맞춘다 — 도구가 바뀌었으면
        재결합이 거부된다(다른 도구의 출력을 같은 package에 섞지 않는다).
        """
        workspace = self._workspace(root)
        record = self.get_job(root, job_id)
        if record.kind != SEGMENT_JOB_KIND:
            raise ExtractJobError(f"추출 작업이 아니다: {job_id} ({record.kind})")
        for package in workspace.list_package_revisions():
            if package.job_fingerprint != record.input_fingerprint:
                continue
            plan, confirmed, handles = extract_runner.reconfirm_extraction(
                workspace, package.package_dir
            )
            assets = self._plan_assets(root, plan)
            fingerprint = extract_runner.extraction_fingerprint(
                plan.fingerprint,
                confirmed.profile_name,
                handles,
                preprocess.ffmpeg_provenance(),
            )
            return self.resume_job(
                root,
                job_id,
                _extract_body(workspace, plan, confirmed, assets, handles),
                fingerprint,
            )
        raise ExtractJobError(
            f"작업 {job_id}의 지문과 같은 package를 찾지 못했다 — 계획이 지워지지"
            " 않았다면 작업 목록을 확인하세요"
        )

    def package_overview(self, root: Path, package_dir: str | None = None) -> PackageSummary | None:
        """package 읽기 모델 — 조각 목록·총 길이·전달물 경로까지 한눈에(화면 3)."""
        workspace = self._workspace(root)
        packages = workspace.list_package_revisions()
        package = None
        if package_dir is not None:
            package = next((item for item in packages if item.package_dir == package_dir), None)
        elif packages:
            package = packages[-1]
        if package is None:
            return None
        clips = workspace.list_clip_artifacts(package.package_dir)
        total = sum((Fraction(clip.media_local_duration) for clip in clips), Fraction(0))
        bytes_used = sum(clip.size_bytes or 0 for clip in clips)
        manifests = tuple(
            f"{package.package_dir}/{name}"
            for name in package_layout.manifest_names()
            if (workspace.root / package.package_dir / name).is_file()
        )
        return PackageSummary(
            package=package,
            clips=clips,
            total_duration_seconds=str(total),
            total_bytes=bytes_used,
            manifest_relative_paths=manifests,
        )

    def list_package_clips(self, root: Path, package_dir: str) -> tuple[ClipArtifact, ...]:
        return self._workspace(root).list_clip_artifacts(package_dir)

    # ── A4 — 의미 분석(견적·실행·재개·읽기·A3 연결) ───────────

    def analysis_estimate(
        self,
        root: Path,
        asset_ids: Sequence[str] | None = None,
        settings: RawUnderstandingSettings | None = None,
    ) -> tuple[AnalysisEstimate, ...]:
        """선택 원본의 분석 견적 — 로컬 조사만, 실제 호출은 전혀 없다(§14.3).

        창·프레임·청크 수는 측정된 시계에서 잰 값이고(이미지 수는 상한),
        Luna 단가는 미확인이라 금액을 만들지 않는다. 실제 호출은 이 견적을 본
        사람의 승인 뒤에만 시작한다.
        """
        workspace = self._workspace(root)
        order = self._order_revision(root, None)
        understanding = settings or RawUnderstandingSettings()
        validate_understanding_settings(understanding)
        workflow = self.settings(root)
        selected = self._analysis_selection(root, order, asset_ids)
        width = Fraction(str(understanding.window_seconds))
        estimates: list[AnalysisEstimate] = []
        for entry in sorted(order.entries, key=lambda item: item.ordinal):
            asset = selected.get(entry.asset_id)
            if asset is None:
                continue
            estimates.append(
                _analysis_estimate_for(
                    workspace, asset, entry.ordinal, understanding, workflow, width
                )
            )
        return tuple(estimates)

    def start_analysis(
        self,
        root: Path,
        *,
        asset_ids: Sequence[str] | None = None,
        settings: RawUnderstandingSettings | None = None,
        mode: str = "replay",
        fixtures_dir: Path | None = None,
        asr_approvals: Mapping[str, object] | None = None,
        sender: object | None = None,
        cfg: object | None = None,
        client: object | None = None,
        on: date | None = None,
    ) -> JobHandle:
        """의미 분석 작업을 제출한다 — 기본은 **오프라인 재생**, 실제 호출은 두 단계 승인만.

        재생(`mode="replay"`)은 단일 패스로 전부 재생한다. 실제 호출
        (`mode="live"`)은 **1단계(내부 ASR)**만 시작한다: 사람이 만든 원본별
        `AsrPhaseApproval`(출처·오디오 스트림·청크 예산·설정 지문·모델·승인
        날짜)와 전송 경계(sender·cfg)가 전부 있어야 하고, 환경(`VLOGSRT_OFFLINE`)과
        작업공간 `network_mode`가 함께 열려 있어야 한다(§14.3). 1단계가 끝나면
        리비전은 `awaiting_semantic`에서 멈춘다 — Luna는
        `resume_analysis_semantic`의 두 번째 승인 전에 가지 않는다(A4-F1).
        """
        workspace = self._workspace(root)
        if mode not in ANALYSIS_MODES:
            supported = ", ".join(ANALYSIS_MODES)
            raise AnalysisError(f"알 수 없는 분석 모드: {mode!r} (지원: {supported})")
        order = self._order_revision(root, None)
        understanding = settings or RawUnderstandingSettings()
        validate_understanding_settings(understanding)
        workflow = self.settings(root)
        assets = self._analysis_selection(root, order, asset_ids)
        providers = self._analysis_providers(
            root,
            mode,
            "full" if mode == "replay" else "asr",
            fixtures_dir,
            asr_approvals,
            None,
            sender,
            cfg,
            client,
            workflow,
            understanding,
            on,
            assets,
        )
        fingerprint = self._analysis_fingerprint(
            order.revision_id, assets, understanding, workflow, mode
        )
        active = next(
            (
                job
                for job in self.list_jobs(root)
                if job.kind == ANALYSIS_JOB_KIND
                and job.status in ACTIVE_JOB_STATUSES
                and job.input_fingerprint == fingerprint
            ),
            None,
        )
        if active is not None:
            # 같은 지문의 활성 분석이 있으면 그 작업에 다시 붙는다 — 리비전을
            # 새로 만들지 않는다(예약만 남는 고아가 생기지 않게, §14.1).
            return JobHandle(job_id=active.job_id, reattached=True)
        revision = AnalysisRevision(
            revision_id=analysis_workflow.new_revision_id(),
            order_revision_id=order.revision_id,
            basis="user_selected" if asset_ids is not None else "all_ready_sources",
            mode=mode,
            settings_json=json.dumps(asdict(understanding), ensure_ascii=False, sort_keys=True),
            models_json=json.dumps(
                {"timing": workflow.models.timing_model, "luna": workflow.models.luna_model},
                ensure_ascii=False,
                sort_keys=True,
            ),
            fingerprint=fingerprint,
            status="reserved",
            source_total=len(order.entries),
            source_done=0,
            error=None,
            created_at=_plan_timestamp(),
            updated_at=_plan_timestamp(),
        )
        AnalysisStore(workspace).insert_revision(revision)
        return self.submit_job(
            root,
            ANALYSIS_JOB_KIND,
            fingerprint,
            _analysis_body(workspace, revision, order, assets, understanding, workflow, providers),
            total=len(order.entries),
        )

    def retry_analysis(
        self,
        root: Path,
        job_id: str,
        *,
        fixtures_dir: Path | None = None,
        asr_approvals: Mapping[str, object] | None = None,
        luna_approvals: Mapping[str, object] | None = None,
        sender: object | None = None,
        cfg: object | None = None,
        client: object | None = None,
        on: date | None = None,
    ) -> JobHandle:
        """실패·중단된 분석의 명시적 재시도 — 같은 작업 ID·같은 지문으로 다시 붙는다.

        1단계 작업은 1단계로(전사 기록이 있는 원본은 ASR 없이 이어 붙는다),
        2단계 작업은 2단계로(전사를 다시 부르지 않는다) 재시도한다. 선택·설정·
        모드는 리비전 기록에서 다시 만들고, 지문은 지금의 설정으로 다시 계산해
        기록과 맞춘다 — 설정이 바뀌었으면 재결합이 거부된다(§14.2).
        """
        record = self.get_job(root, job_id)
        if record.kind == ANALYSIS_SEMANTIC_JOB_KIND:
            if luna_approvals is None or client is None:
                raise AnalysisError(
                    "2단계 재시도에는 원본별 의미 승인(luna_approvals)과 클라이언트가"
                    " 필요하다 — 사람이 공급한다"
                )
            return self.resume_analysis_semantic(
                root, job_id, luna_approvals=luna_approvals, client=client, on=on
            )
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        if record.kind != ANALYSIS_JOB_KIND:
            raise AnalysisError(f"의미 분석 작업이 아니다: {job_id} ({record.kind})")
        revision = next(
            (
                item
                for item in store.list_revisions()
                if item.fingerprint == record.input_fingerprint
            ),
            None,
        )
        if revision is None:
            raise AnalysisError(
                f"작업 {job_id}의 지문과 같은 분석 리비전을 찾지 못했다 — 새 분석을 시작하세요"
            )
        order = self._order_revision(root, revision.order_revision_id)
        understanding = RawUnderstandingSettings(**json.loads(revision.settings_json))
        validate_understanding_settings(understanding)
        workflow = self.settings(root)
        rows = store.list_source_results(revision.revision_id)
        selected_ids = [row.asset_id for row in rows if row.status != "unanalyzed"]
        if not selected_ids:
            raise AnalysisError("재시도할 선택 원본이 기록에 없다 — 새 분석을 시작하세요")
        assets = self._analysis_selection(root, order, selected_ids)
        phase = "full" if revision.mode == "replay" else "asr"
        providers = self._analysis_providers(
            root,
            revision.mode,
            phase,
            fixtures_dir,
            asr_approvals,
            None,
            sender,
            cfg,
            client,
            workflow,
            understanding,
            on,
            assets,
        )
        fingerprint = self._analysis_fingerprint(
            order.revision_id, assets, understanding, workflow, revision.mode
        )
        return self.resume_job(
            root,
            job_id,
            _analysis_body(workspace, revision, order, assets, understanding, workflow, providers),
            fingerprint,
        )

    def resume_analysis_semantic(
        self,
        root: Path,
        job_id: str,
        *,
        luna_approvals: Mapping[str, object],
        client: object,
        on: date | None = None,
    ) -> JobHandle:
        """2단계(의미 Luna) 재개 — 두 번째 사람 승인이 1단계 실측 지문을 묶는다.

        `job_id`는 1단계 작업(같은 리비전을 찾는 열쇠) 또는 재시도할 2단계 작업.
        전사는 1단계 불변 기록에서 다시 검증해 재사용한다 — ASR는 0송신이다.
        승인이 없거나·날았거나(지문·원본·모델 어긋남) 환경이 닫혀 있으면 시작
        전에 0송신으로 거부된다(A4-F1, §14.3).
        """
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        record = self.get_job(root, job_id)
        if record.kind not in (ANALYSIS_JOB_KIND, ANALYSIS_SEMANTIC_JOB_KIND):
            raise AnalysisError(f"의미 분석 작업이 아니다: {job_id} ({record.kind})")
        revisions = store.list_revisions()
        if record.kind == ANALYSIS_SEMANTIC_JOB_KIND:
            revision = next(
                (
                    item
                    for item in revisions
                    if analysis_workflow.semantic_job_fingerprint(item.revision_id)
                    == record.input_fingerprint
                ),
                None,
            )
        else:
            revision = next(
                (item for item in revisions if item.fingerprint == record.input_fingerprint),
                None,
            )
        if revision is None:
            raise AnalysisError(f"작업 {job_id}의 분석 리비전을 찾지 못했다 — 새 분석을 시작하세요")
        if revision.mode != "live":
            raise AnalysisError(
                f"재생 리비전({revision.revision_id})에는 2단계가 없다 — 재생은 단일 패스다"
            )
        if revision.status not in ("awaiting_semantic", "analyzing", "failed", "interrupted"):
            raise AnalysisError(
                f"2단계를 시작할 상태가 아니다({revision.status}) — 1단계가"
                " `awaiting_semantic`에 도달해야 한다"
            )
        order = self._order_revision(root, revision.order_revision_id)
        understanding = RawUnderstandingSettings(**json.loads(revision.settings_json))
        validate_understanding_settings(understanding)
        workflow = self.settings(root)
        self._require_revision_models(revision, workflow)
        rows = store.list_source_results(revision.revision_id)
        selected_ids = [row.asset_id for row in rows if row.status != "unanalyzed"]
        if not selected_ids:
            raise AnalysisError("2단계로 나갈 선택 원본이 기록에 없다 — 새 분석을 시작하세요")
        assets = self._analysis_selection(root, order, selected_ids)
        providers = self._analysis_providers(
            root,
            "live",
            "semantic",
            None,
            None,
            luna_approvals,
            None,
            None,
            client,
            workflow,
            understanding,
            on,
            assets,
            semantic_revision_id=revision.revision_id,
        )
        fingerprint = analysis_workflow.semantic_job_fingerprint(revision.revision_id)
        active = next(
            (
                job
                for job in self.list_jobs(root)
                if job.kind == ANALYSIS_SEMANTIC_JOB_KIND
                and job.status in ACTIVE_JOB_STATUSES
                and job.input_fingerprint == fingerprint
            ),
            None,
        )
        if active is not None:
            if active.job_id == job_id and record.kind == ANALYSIS_SEMANTIC_JOB_KIND:
                return self.resume_job(
                    root,
                    job_id,
                    _semantic_body(
                        workspace, revision, order, assets, understanding, workflow, providers
                    ),
                    fingerprint,
                )
            return JobHandle(job_id=active.job_id, reattached=True)
        if record.kind == ANALYSIS_SEMANTIC_JOB_KIND:
            return self.resume_job(
                root,
                job_id,
                _semantic_body(
                    workspace, revision, order, assets, understanding, workflow, providers
                ),
                fingerprint,
            )
        return self.submit_job(
            root,
            ANALYSIS_SEMANTIC_JOB_KIND,
            fingerprint,
            _semantic_body(workspace, revision, order, assets, understanding, workflow, providers),
            total=len(order.entries),
        )

    def analysis_semantic_estimate(
        self, root: Path, revision_id: str | None = None
    ) -> tuple[AnalysisSemanticEstimate, ...]:
        """2단계(의미 Luna) 견적 — 1단계 기록의 **실측** 지문·창·이미지를 그대로 말한다.

        로컬 파일 읽기뿐, 호출은 전혀 없다. 사람은 이 견적의 `semantic_fingerprint`를
        본 뒤 2단계 승인을 만든다 — 승인이 묶는 값과 실행이 잰 값이 같은지의
        증명이 여기서 시작한다(A4-F1).
        """
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        revisions = store.list_revisions()
        revision = None
        if revision_id is not None:
            revision = next((item for item in revisions if item.revision_id == revision_id), None)
        elif revisions:
            revision = revisions[-1]
        if revision is None:
            return ()
        models = json.loads(revision.models_json)
        luna_model = str(models.get("luna") or "")
        estimates: list[AnalysisSemanticEstimate] = []
        for row in store.list_source_results(revision.revision_id):
            if row.status != "transcribed" or not row.document_path:
                continue
            try:
                binding = analysis_workflow.read_asr_phase_binding(
                    workspace.root / row.document_path
                )
            except AnalysisError as error:
                estimates.append(
                    AnalysisSemanticEstimate(
                        asset_id=row.asset_id,
                        source_order=row.source_order,
                        source_name=row.source_name,
                        phase="semantic",
                        semantic_fingerprint="-",
                        window_count=0,
                        image_count=0,
                        luna_model=luna_model,
                        luna_request_count=0,
                        price_status="unknown",
                        user_ceiling_total=None,
                        note=f"1단계 기록을 읽지 못했다 — 1단계부터 다시 받아라: {error}",
                    )
                )
                continue
            window_count = int(binding["window_count"])
            estimate = estimate_luna_cost(
                window_count,
                AnalysisBudget(max_luna_requests=max(1, window_count), max_asr_chunks=1),
            )
            estimates.append(
                AnalysisSemanticEstimate(
                    asset_id=row.asset_id,
                    source_order=row.source_order,
                    source_name=row.source_name,
                    phase=str(binding["phase_kind"]),
                    semantic_fingerprint=str(binding["semantic_fingerprint"]),
                    window_count=window_count,
                    image_count=int(binding["image_count"]),
                    luna_model=luna_model,
                    luna_request_count=estimate.request_count,
                    price_status=estimate.price_status,
                    user_ceiling_total=(
                        str(estimate.user_ceiling_total)
                        if estimate.user_ceiling_total is not None
                        else None
                    ),
                    note=f"1단계 실측 지문에 묶였다 — Luna: {estimate.note}",
                )
            )
        return tuple(estimates)

    def _require_revision_models(
        self, revision: AnalysisRevision, workflow: RawWorkflowConfig
    ) -> None:
        """2단계 재개의 모델 드리프트 검사 — 리비전이 본 모델이 지금도 설정이다."""
        recorded = json.loads(revision.models_json)
        if (
            str(recorded.get("timing")) != workflow.models.timing_model
            or str(recorded.get("luna")) != workflow.models.luna_model
        ):
            raise AnalysisError(
                "작업공간 모델이 리비전이 본 모델과 다르다 — 새 1단계 승인을 받아라(§14.2)"
            )

    def analysis_overview(
        self, root: Path, revision_id: str | None = None
    ) -> AnalysisSummary | None:
        """분석 읽기 모델 — 리비전과 원본별 결과(상태·창 수·실행 모드·오류)."""
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        revisions = store.list_revisions()
        revision = None
        if revision_id is not None:
            revision = next((item for item in revisions if item.revision_id == revision_id), None)
        elif revisions:
            revision = revisions[-1]
        if revision is None:
            return None
        return AnalysisSummary(
            revision=revision,
            results=store.list_source_results(revision.revision_id),
        )

    def analysis_document(
        self, root: Path, revision_id: str, asset_id: str
    ) -> dict[str, object] | None:
        """원본 하나의 첫 단계 문서(읽기 전용) — 요약·태그 읽기용, SRT는 없다."""
        workspace = self._workspace(root)
        row = AnalysisStore(workspace).source_result(revision_id, asset_id)
        if row is None or row.document_path is None:
            return None
        path = workspace.root / row.document_path
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise AnalysisError(
                f"분석 문서를 읽지 못했다: {row.document_path} — {error}"
            ) from error
        if not isinstance(body, dict):
            raise AnalysisError(f"분석 문서가 JSON 객체가 아니다: {row.document_path}")
        return body

    def plan_segmentation_from_analysis(
        self, root: Path, analysis_revision_id: str | None = None
    ) -> SegmentPlanSnapshot:
        """받아들인 분석을 A3 분할 계획으로 잇는다 — 전체 순서는 항상 전부 산다.

        분석된 원본은 문서 기록의 정확한 유리수 구간이 비디오 격자 타일이 되고,
        선택 밖·실패 원본은 **원본 전체를 한 조각으로 보존**한다(삭제·재정렬 없음,
        §6.1·§7.6). 계획 검증·원본 재검증은 A3의 순수 계층이 다시 한다 — 이
        함수는 새 계획 리비전을 만들 뿐 옛 계획·package는 그대로 둔다(§6.5).
        """
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        revisions = store.list_revisions()
        revision = None
        if analysis_revision_id is not None:
            revision = next(
                (item for item in revisions if item.revision_id == analysis_revision_id), None
            )
        elif revisions:
            revision = revisions[-1]
        if revision is None:
            raise AnalysisError("분석 리비전이 없다 — 먼저 의미 분석을 실행하세요")
        if revision.status != "complete":
            raise AnalysisError(
                f"분석이 완성 상태가 아니다({revision.status}) — 완료된 리비전만 계획에 쓴다"
            )
        order = self._order_revision(root, revision.order_revision_id)
        rows = {row.asset_id: row for row in store.list_source_results(revision.revision_id)}
        intervals: dict[str, list[PlannedInterval]] = {}
        for entry in sorted(order.entries, key=lambda item: item.ordinal):
            row = rows.get(entry.asset_id)
            if row is None:
                raise AnalysisError(
                    f"분석 결과 행이 없다: {entry.asset_id} — 분석을 다시 실행하세요"
                )
            intervals[entry.asset_id] = self._analysis_intervals(workspace, row, entry.asset_id)
        return self.plan_segmentation(
            root,
            order_revision_id=revision.order_revision_id,
            intervals=intervals,
            basis=SEGMENTATION_BASIS_SEMANTIC,
        )

    # ── A4 내부 — 선택·지문·공급자·구간 조립 ────────────────────

    def _analysis_selection(
        self, root: Path, order: GlobalOrderRevision, asset_ids: Sequence[str] | None
    ) -> dict[str, SourceAsset]:
        """분석 선택 원본 — 전체이거나 순서 리비전 안의 명시적 부분집합이다."""
        workspace = self._workspace(root)
        known = {entry.asset_id for entry in order.entries}
        if asset_ids is None:
            chosen = sorted(known)
        else:
            given = [item.strip() for item in asset_ids if item.strip()]
            unknown = [item for item in given if item not in known]
            if unknown:
                raise AnalysisError(
                    f"순서 리비전에 없는 자산이다: {', '.join(unknown[:3])} —"
                    " 확정된 전체 순서의 원본만 선택할 수 있다"
                )
            if not given:
                raise AnalysisError("선택한 원본이 비었다 — 전체 또는 한 개 이상을 고르세요")
            chosen = sorted(set(given))
        return {asset_id: _verified_asset(workspace, asset_id) for asset_id in chosen}

    def _analysis_providers(
        self,
        root: Path,
        mode: str,
        phase: str,
        fixtures_dir: Path | None,
        asr_approvals: Mapping[str, object] | None,
        luna_approvals: Mapping[str, object] | None,
        sender: object,
        cfg: object,
        client: object,
        workflow: RawWorkflowConfig,
        understanding: RawUnderstandingSettings,
        on: date | None,
        assets: Mapping[str, SourceAsset],
        semantic_revision_id: str | None = None,
    ) -> analysis_workflow.AnalysisProviders:
        """작업의 호출 경계 — 재생 픽스처 또는 단계별 사람 승인 실제 호출 재료.

        실제 호출의 빠른 실패 문이다: 환경·네트워크 모드·승인의 형식과 묶은 값
        (출처·모델·설정 지문·2단계 실측 지문)을 시작 전에 본다. 전송 순간의
        재검사는 작업 본문의 게이트가 맡는다(§14.3). 무성 원본은 1단계 승인이
        필요 없다 — ASR 작업 자체가 없으므로 승인할 전사가 없다(§7.3).
        """
        if phase not in ANALYSIS_PHASES:
            raise AnalysisError(f"알 수 없는 분석 단계: {phase!r}")
        if mode == "replay":
            if asr_approvals is not None or luna_approvals is not None:
                raise AnalysisError(
                    "오프라인 재생에는 실제 호출 승인이 필요 없다 — 승인 없이 재생한다"
                )
            directory = fixtures_dir
            if directory is None:
                directory = (
                    self._workspace(root).root
                    / ANALYSIS_ROOT
                    / analysis_workflow.REPLAY_FIXTURES_DIRNAME
                )
            return analysis_workflow.AnalysisProviders(
                origin="replay", replay=analysis_workflow.load_replay_fixtures(directory)
            )
        blocked = online_env_blocked()
        if blocked is not None:
            raise AnalysisError(blocked)
        if workflow.network_mode != "online":
            raise AnalysisError(
                "작업공간 network_mode가 online이 아니다 — 실제 호출 분석을 시작할 수 없다"
            )
        if phase == "asr":
            return self._live_asr_providers(
                root, asr_approvals, sender, cfg, workflow, understanding, on, assets
            )
        if phase == "semantic":
            return self._live_semantic_providers(
                root, luna_approvals, client, workflow, semantic_revision_id or "", assets
            )
        raise AnalysisError(
            "승인 없는 실제 호출 전체 실행은 없다 — 1단계(전사)와 2단계(의미) 승인을"
            " 각각 받아라(A4-F1)"
        )

    def _live_asr_providers(
        self,
        root: Path,
        asr_approvals: Mapping[str, object] | None,
        sender: object,
        cfg: object,
        workflow: RawWorkflowConfig,
        understanding: RawUnderstandingSettings,
        on: date | None,
        assets: Mapping[str, SourceAsset],
    ) -> analysis_workflow.AnalysisProviders:
        """1단계(전사) 공급자 — 원본별 `AsrPhaseApproval`의 빠른 실패 문."""
        if sender is None or cfg is None:
            raise AnalysisError(
                "1단계 실제 호출에는 전송 경계(sender·cfg)가 필요하다 — 사람이 공급한다"
            )
        if asr_approvals is None:
            raise AnalysisError(
                "1단계 실제 호출에는 원본별 전사 승인(asr_approvals)이 필요하다 —"
                " 사람이 만든다(브라우저는 만들 수 없다)"
            )
        workspace = self._workspace(root)
        checked: dict[str, AsrPhaseApproval] = {}
        for asset_id, approval in asr_approvals.items():
            if not isinstance(approval, AsrPhaseApproval) or not approval.approved:
                raise AnalysisError("1단계 승인 기록이 AsrPhaseApproval이 아니거나 미승인이다")
            checked[asset_id] = approval
        settings_fp = asr_settings_fingerprint(understanding, workflow)
        from vlogsrt.project.raw.capabilities import (
            ROLE_TIMING,
            preflight_online_transcription,
        )

        for asset_id, asset in assets.items():
            probe = preprocess.probe_raw_media(workspace.root / asset.relative_path)
            if not probe.audio:
                continue  # 무성 원본 — 승인할 전사가 없다(ASR 작업을 만들지 않는다)
            approval = checked.get(asset_id)
            if approval is None:
                raise AnalysisError(
                    f"1단계(전사) 승인이 없는 원본이다: {asset.source_name} — 새 추정과"
                    " 새 승인을 받아라(§14.3)"
                )
            if approval.source_sha256 != (asset.content_sha256 or ""):
                raise AnalysisError(
                    f"승인이 본 원본과 지금의 원본이 다르다: {asset.source_name} — 새 추정과"
                    " 새 승인을 받아라(§14.3)"
                )
            if approval.timing_model != workflow.models.timing_model:
                raise AnalysisError(
                    f"승인이 본 ASR 모델과 설정 모델이 다르다: {asset.source_name}"
                    f" ({approval.timing_model!r}/{workflow.models.timing_model!r})"
                )
            if approval.settings_fingerprint != settings_fp:
                raise AnalysisError(
                    f"승인이 본 설정 지문과 지금의 설정 지문이 다르다: {asset.source_name}"
                    " — 새 추정과 새 승인을 받아라(§14.3)"
                )
            try:
                preflight_online_transcription(workflow, on or date.today(), roles=(ROLE_TIMING,))
            except Exception as error:  # noqa: BLE001 - 시작 문의 수명주기 실패는 AnalysisError로
                raise AnalysisError(
                    f"1단계 실제 호출의 수명주기 문이 닫혔다: {asset.source_name} — {error}"
                ) from error
        return analysis_workflow.AnalysisProviders(
            origin="live",
            phase="asr",
            live_sender=sender,
            live_cfg=cfg,
            asr_approvals=checked,
            on=on,
        )

    def _live_semantic_providers(
        self,
        root: Path,
        luna_approvals: Mapping[str, object] | None,
        client: object,
        workflow: RawWorkflowConfig,
        semantic_revision_id: str,
        assets: Mapping[str, SourceAsset],
    ) -> analysis_workflow.AnalysisProviders:
        """2단계(의미) 공급자 — 승인이 1단계 기록의 실측 지문과 같은지 본다."""
        if client is None:
            raise AnalysisError(
                "2단계 실제 호출에는 Responses 클라이언트가 필요하다 — 사람이 공급한다"
            )
        if luna_approvals is None:
            raise AnalysisError(
                "2단계 실제 호출에는 원본별 의미 승인(luna_approvals)이 필요하다 — 사람이"
                " 만든다(브라우저는 만들 수 없다)"
            )
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        for asset_id, asset in assets.items():
            approval = luna_approvals.get(asset_id)
            if approval is None:
                raise AnalysisError(
                    f"2단계(의미) 승인이 없는 원본이다: {asset.source_name} — 1단계 견적을"
                    " 보고 새 승인을 받아라(§14.3)"
                )
            if not isinstance(approval, (OnlineAnalysisApproval, OnlineVisualAnalysisApproval)):
                raise AnalysisError(
                    f"2단계 승인 기록의 형식이 알 수 없다: {asset.source_name}"
                    " (오디오는 OnlineAnalysisApproval, 무성은 OnlineVisualAnalysisApproval)"
                )
            if not approval.approved:
                raise AnalysisError(f"미승인 2단계 승인이다: {asset.source_name}")
            if approval.source_sha256 != (asset.content_sha256 or ""):
                raise AnalysisError(
                    f"승인이 본 원본과 지금의 원본이 다르다: {asset.source_name} — 새 추정과"
                    " 새 승인을 받아라(§14.3)"
                )
            if approval.luna_model != workflow.models.luna_model:
                raise AnalysisError(
                    f"승인이 본 Luna 모델과 설정 모델이 다르다: {asset.source_name}"
                    f" ({approval.luna_model!r}/{workflow.models.luna_model!r})"
                )
            if isinstance(approval, OnlineAnalysisApproval) and (
                approval.asr_model != workflow.models.timing_model
            ):
                raise AnalysisError(f"승인이 본 ASR 모델과 설정 모델이 다르다: {asset.source_name}")
            row = (
                store.source_result(semantic_revision_id, asset_id)
                if semantic_revision_id
                else None
            )
            binding = self._semantic_binding_for(workspace, asset, row)
            if approval.fingerprint != binding["semantic_fingerprint"]:
                raise AnalysisError(
                    f"승인이 본 지문이 1단계 기록의 실측 지문과 다르다: {asset.source_name}"
                    " — 1단계 견적을 보고 새 승인을 받아라(§14.3)"
                )
            if approval.window_count != int(binding["window_count"]):
                raise AnalysisError(
                    f"승인이 본 창 수({approval.window_count})가 1단계 실측 창 수"
                    f"({binding['window_count']})와 다르다: {asset.source_name}"
                )
        return analysis_workflow.AnalysisProviders(
            origin="live",
            phase="semantic",
            live_client=client,
            luna_approvals=dict(luna_approvals),
        )

    def _semantic_binding_for(
        self, workspace: RawWorkspace, asset: SourceAsset, row: AnalysisSourceResult | None
    ) -> dict[str, Any]:
        """원본 하나의 1단계 실측 바인딩 — 기록이 없으면 2단계를 시작하지 않는다.

        2단계가 끝난 원본의 행 문서는 최종 문서로 바뀌므로, 1단계 기록은 같은
        폴더의 `asr_phase.json`에서 찾는다(전사 단계 행은 문서 자체가 그것이다).
        """
        if row is None or row.document_path is None:
            raise AnalysisError(
                f"1단계 기록이 없다: {asset.source_name} — 먼저 1단계(전사) 실제 호출을 마쳐야 한다"
            )
        document = workspace.root / row.document_path
        artifact = document.parent / analysis_workflow.ASR_PHASE_DOCUMENT_NAME
        if not artifact.is_file() and document.name == analysis_workflow.ASR_PHASE_DOCUMENT_NAME:
            artifact = document
        if not artifact.is_file():
            raise AnalysisError(
                f"1단계 기록이 없다: {asset.source_name} — 먼저 1단계(전사) 실제 호출을 마쳐야 한다"
            )
        return analysis_workflow.read_asr_phase_binding(artifact)

    def _analysis_fingerprint(
        self,
        order_revision_id: str,
        assets: Mapping[str, SourceAsset],
        settings: RawUnderstandingSettings,
        workflow: RawWorkflowConfig,
        mode: str,
    ) -> str:
        """분석 리비전 지문 — 순서·선택·설정·모델·모드를 묶는다(작업 재결합의 키)."""
        payload = json.dumps(
            {
                "schema": "raw-analysis-revision/v1",
                "order_revision": order_revision_id,
                "assets": sorted(
                    (asset_id, asset.content_sha256 or "") for asset_id, asset in assets.items()
                ),
                "settings": asdict(settings),
                "models": {
                    "timing": workflow.models.timing_model,
                    "luna": workflow.models.luna_model,
                },
                "mode": mode,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _analysis_intervals(
        self,
        workspace: RawWorkspace,
        row: AnalysisSourceResult,
        asset_id: str,
    ) -> list[PlannedInterval]:
        """원본 하나의 계획 구간 — 분석됨은 기록 그대로, 아니면 전체 보존 한 조각."""
        if row.status in ("analyzed", "cached") and row.record_path is not None:
            path = workspace.root / row.record_path
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                spans = record["intervals"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise AnalysisError(
                    f"분석 기록을 읽지 못했다: {row.record_path} — {error}"
                ) from error
            intervals = [
                PlannedInterval(
                    start=Fraction(int(item["start"]["num"]), int(item["start"]["den"])),
                    end=Fraction(int(item["end"]["num"]), int(item["end"]["den"])),
                )
                for item in spans
            ]
            if intervals:
                return intervals
        asset = _verified_asset(workspace, asset_id)
        facts = _measured_source(workspace, asset)
        playable = (facts.exact_end_pts - facts.clock.first_pts) * facts.clock.time_base
        return [PlannedInterval(start=Fraction(0), end=playable)]

    # ── A3 내부 — 계획·package 짝 맞추기 ────────────────────────

    def _order_revision(self, root: Path, revision_id: str | None) -> GlobalOrderRevision:
        """전체 순서 리비전 찾기 — 없으면(확정이 없으면) 계획은 시작하지 않는다."""
        workspace = self._workspace(root)
        if revision_id is None:
            revision = workspace.latest_global_order_revision()
            if revision is None:
                raise OrderNotConfirmedError(
                    "확정된 전체 원본 순서가 없다 — 가져오기를 마치고 전체 순서를"
                    " 확정한 뒤 계획하세요"
                )
            return revision
        for revision in workspace.list_global_order_revisions():
            if revision.revision_id == revision_id:
                return revision
        raise OrderNotConfirmedError(f"없는 전체 순서 리비전: {revision_id}")

    def _plan_snapshot(self, root: Path, revision_id: str | None) -> SegmentPlanSnapshot:
        workspace = self._workspace(root)
        if revision_id is None:
            snapshot = self.latest_segment_plan(root)
            if snapshot is None:
                raise PlanNotFoundError("분할 계획이 없다 — 먼저 결정적 분할 계획을 만드세요")
            return snapshot
        return workspace.segment_plan_revision(revision_id)

    def _plan_assets(self, root: Path, snapshot: SegmentPlanSnapshot) -> dict[str, SourceAsset]:
        """계획이 가리키는 원본 전부 — 하나라도 ready가 아니면 추출은 시작하지 않는다."""
        workspace = self._workspace(root)
        assets: dict[str, SourceAsset] = {}
        for asset_id in {entry.asset_id for entry in snapshot.entries}:
            assets[asset_id] = _verified_asset(workspace, asset_id)
        return assets

    def _reusable_or_reserved_package(
        self,
        workspace: RawWorkspace,
        snapshot: SegmentPlanSnapshot,
        profile_name: str,
        handles: preprocess.RawClipHandles,
        fingerprint: str,
    ) -> PackageRevision:
        """같은 지문의 package가 있으면 이어 쓰고, 없으면 새로 예약한다(§6.5)."""
        for package in workspace.list_package_revisions():
            if package.job_fingerprint != fingerprint:
                continue
            if package.status == "complete":
                raise PackageError(
                    f"이미 완성된 package가 있다: {package.package_dir} — 같은 계획·"
                    "프로파일·핸들·도구의 재추출은 새 계획 리비전에서 시작하세요"
                )
            return package
        return workspace.reserve_package(
            plan_revision_id=snapshot.revision_id,
            order_revision_id=snapshot.order_revision_id,
            profile_name=profile_name,
            handles_before=str(handles.before),
            handles_after=str(handles.after),
            job_fingerprint=fingerprint,
            clip_total=len(snapshot.entries),
        )


def _verified_asset(workspace: RawWorkspace, asset_id: str) -> SourceAsset:
    """ready 원본의 재검증 — 크기·SHA-256을 지금 다시 읽어 계획의 밑천을 확인한다.

    검증 이력(`content_sha256`)이 없는 원본은 계획 재료가 되지 못한다 — 상태표가
    아니라 내용으로 판단한다(§5.3의 완료 정의를 계획에도 그대로 적용한다).
    """
    asset = workspace.source_asset(asset_id)
    if asset.status != "ready" or asset.content_sha256 is None:
        raise SegmentationError(
            f"원본이 검증 준비 상태가 아니다: {asset.source_relative_path}"
            f" ({asset.status}) — 가져오기(복사·검증)를 먼저 끝내세요"
        )
    path = workspace.root / asset.relative_path
    probed = path.stat()
    if probed.st_size != asset.size_bytes:
        raise SegmentationError(
            f"원본 크기가 가져오기 때와 다르다: {asset.source_relative_path}"
            f" ({asset.size_bytes} → {probed.st_size}B)"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != asset.content_sha256:
        raise SegmentationError(
            f"원본 내용이 가져오기 때와 다르다: {asset.source_relative_path}"
            " — 다시 가져오세요(새 배치)"
        )
    return asset


@dataclass(frozen=True, slots=True)
class _MeasuredSource:
    """원본 하나의 계획용 측정 — 시계·원점·정규화 종료(§7.6의 1단계, 의미 없이).

    원본별 시계는 이 계층이 소유한다: 계획은 측정값을 값으로 받고, 추출은 같은
    시계 규칙(preprocess)을 다시 잰다 — 두 측정이 어긋나면 추출 검증이 잡는다.
    """

    clock: preprocess.RawVideoClock
    exact_end_pts: int


def _measured_source(workspace: RawWorkspace, asset: SourceAsset) -> _MeasuredSource:
    path = workspace.root / asset.relative_path
    probe = preprocess.probe_raw_media(path)
    clock = preprocess.probe_extraction_clock(path, probe=probe)
    if not clock.uniform_grid:
        raise SegmentationError(
            f"원본이 균일 격자가 아니다(VFR) — 추출 지원 밖이다: "
            f"{asset.source_relative_path} ({clock.grid_note})"
        )
    return _MeasuredSource(clock=clock, exact_end_pts=math.ceil(clock.exact_end / clock.time_base))


def _plan_fingerprint_inputs(
    asset: SourceAsset, facts: _MeasuredSource, spans: Sequence[SegmentPlanEntry]
) -> dict[str, object]:
    """계획 지문의 원본별 재료 — 내용 해시·시계·선택된 경계 틱(§14.2)."""
    clock = facts.clock
    return {
        "content_sha256": asset.content_sha256,
        "time_base": f"{clock.time_base.numerator}/{clock.time_base.denominator}",
        "origin_pts": clock.first_pts,
        "frame_count": clock.frame_count,
        "frame_duration": str(clock.frame_duration),
        "boundaries": [span.core_start_pts for span in spans] + [spans[-1].core_end_pts],
    }


def _validated_handles(before: Fraction, after: Fraction) -> preprocess.RawClipHandles:
    """핸들 검증 — 음수가 아니면 정확한 유리수로 묶는다(§6.5 기본 0)."""
    handles = preprocess.RawClipHandles(before=Fraction(before), after=Fraction(after))
    if handles.before < 0 or handles.after < 0:
        raise PackageError(f"핸들은 0 이상이어야 한다: 앞 {before}, 뒤 {after}")
    return handles


def _plan_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
