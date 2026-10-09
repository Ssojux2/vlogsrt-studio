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
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import log, preprocess
from vlogsrt.project.raw import (
    analysis_workflow,
    segmentation,
    tiering,
    visual_review,
)
from vlogsrt.project.raw import package as package_layout
from vlogsrt.project.raw import runner as extract_runner
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    LiveAnalysisTransport,
    asr_settings_fingerprint,
)
from vlogsrt.project.raw.analysis_config import (
    ANALYSIS_CONCURRENCY,
    RawAnalysisConcurrency,
    RawUnderstandingSettings,
    validate_analysis_concurrency,
    validate_understanding_settings,
)
from vlogsrt.project.raw.analysis_store import ANALYSIS_ROOT, AnalysisStore
from vlogsrt.project.raw.browser_runtime import (
    BrowserRuntimeError,
    EnvironmentCredentials,
    asr_estimate_digest,
    browser_network_mode,
    caption_transport_providers,
    capture_browser_credentials,
    capture_environment_credentials,
    live_transport_for_credentials,
    mint_asr_approvals,
    mint_luna_approvals,
    production_live_transport,
    semantic_estimate_digest,
)
from vlogsrt.project.raw.caption_manual import ms as caption_ms
from vlogsrt.project.raw.caption_workspace_types import (
    CaptionWorkspaceError,
    ManualHeadConflict,
)
from vlogsrt.project.raw.config import (
    WORKFLOW_SCHEMA_VERSION,
    RawWorkflowConfig,
    validate_raw_workflow_config,
)
from vlogsrt.project.raw.fcp import (
    FCP_ROLE_ORIGINAL,
    FCP_ROLE_PROXY,
    FCP_ROLES,
    FcpBrokenLink,
    FcpLibraryError,
    FcpLibraryScan,
    FcpLogicalClip,
    FcpMediaFile,
    is_fcp_library,
)
from vlogsrt.project.raw.fcp import (
    managed_workspace_root as fcp_managed_workspace_root,
)
from vlogsrt.project.raw.fcp import (
    scan_fcp_library as fcp_scan_fcp_library,
)
from vlogsrt.project.raw.final_video import FinalVideoError
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
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext, JobManager
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
    VISUAL_PREPARE_JOB_KIND,
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
    SourceNestingError,
    SourceOrderRevision,
    WorkspaceAlreadyExistsError,
    WorkspaceModuleUnavailableError,
    WorkspaceNotFoundError,
    WorkspaceStatus,
)
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    ReviewError,
    ReviewRevision,
    StaleRevisionError,
)
from vlogsrt.project.raw.segment_review import (
    boundary_edit as review_boundary_edit,
)
from vlogsrt.project.raw.segment_review import (
    merge_edit as review_merge_edit,
)
from vlogsrt.project.raw.segment_review import (
    note_edit as review_note_edit,
)
from vlogsrt.project.raw.segment_review import (
    override_edit as review_override_edit,
)
from vlogsrt.project.raw.segment_review import (
    selection_edit as review_selection_edit,
)
from vlogsrt.project.raw.segment_review import (
    split_edit as review_split_edit,
)
from vlogsrt.project.raw.source_order import propose_global_order as _propose_global_order
from vlogsrt.project.raw.understanding_luna import (
    AnalysisBudget,
    OnlineAnalysisApproval,
    OnlineVisualAnalysisApproval,
    estimate_luna_cost,
    online_env_blocked,
)
from vlogsrt.project.raw.visual_review import (  # 공개 경계 재수출(계약: UI는 이 모듈만 가져간다)
    VISUAL_PAGE_SIZE,
    VisualAudioTrack,
    VisualReviewError,
    VisualReviewOverview,
    VisualSegmentCard,
    VisualSelectedSegment,
    VisualSnapProposal,
    VisualSourcePreview,
    middle_frame_index,
    snapped_frame_index,
)
from vlogsrt.project.raw.workspace import (
    CAPTURE_BASIS_KEY,
    CAPTURE_TIME_BASIS,
    DB_NAME,
    PROJECT_TOML_NAME,
    RawWorkspace,
    new_segment_plan_revision_id,
)

__all__ = [
    # UI는 이 모듈만 가져갈 수 있다(정확 경로 예외) — 계약 오류도 여기서 다시 낸다.
    "ACTIVE_JOB_STATUSES",
    "ANALYSIS_CONCURRENCY",
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
    "BrowserRuntimeError",
    "ByteRange",
    "EnvironmentCredentials",
    "FCP_ROLE_ORIGINAL",
    "FCP_ROLE_PROXY",
    "FCP_ROLES",
    "FcpBrokenLink",
    "FcpLibraryError",
    "FcpLibraryScan",
    "FcpLogicalClip",
    "FcpMediaFile",
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
    "JobCancelledError",
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
    "RawAnalysisConcurrency",
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
    "SourceNestingError",
    "SourceOrderRevision",
    "TIER_LABELS",
    "TIER_SETTINGS",
    "TIER_TAG_PREFIX",
    "UPKEEP_JOB_KIND",
    "SYNC_CHECK_JOB_KIND",
    "AUTO_EXCLUDED_UNREVIEWED_TAG",
    "VISUAL_PAGE_SIZE",
    "VISUAL_PREPARE_JOB_KIND",
    "VisualAudioTrack",
    "VisualReviewError",
    "VisualReviewOverview",
    "VisualSegmentCard",
    "VisualSelectedSegment",
    "VisualSnapProposal",
    "VisualSourcePreview",
    "WorkspaceModuleUnavailableError",
    "WorkspaceStatus",
    "asr_estimate_digest",
    "validate_analysis_concurrency",
    "caption_ms",
    "caption_run_identity",
    "CaptionPreviewMedia",
    "CaptionWorkspaceError",
    "FinalVideoError",
    "ManualHeadConflict",
    "ReviewError",
    "StaleRevisionError",
    "browse_folder",
    "capture_environment_credentials",
    "capture_browser_credentials",
    "browser_network_mode",
    "caption_transport_providers",
    "content_type_for",
    "ffprobe_media_inspector",
    "host_header_allowed",
    "is_fcp_library",
    "live_transport_for_credentials",
    "loopback_hosts",
    "mint_asr_approvals",
    "mint_luna_approvals",
    "mounted_device_candidates",
    "mutation_request_allowed",
    "origin_header_allowed",
    "parse_range",
    "production_live_transport",
    "referer_header_allowed",
    "review_boundary_edit",
    "review_merge_edit",
    "review_note_edit",
    "review_override_edit",
    "review_selection_edit",
    "review_split_edit",
    "semantic_estimate_digest",
    "middle_frame_index",
    "red_ranges",
    "snapped_frame_index",
    "unmaterialized_red",
]

UPKEEP_JOB_KIND = "workspace_upkeep"
"""앱 열기·화면 갱신의 정리 작업 하나 — 촬영 시각·영상 순서·등급판·검토 순서(D52·D53)."""

SYNC_CHECK_JOB_KIND = "subtitle_sync_check"
"""자막 시각 확인 작업(D55) — 무료 로컬 VAD로 원본 자막과 말소리를 대조해 **표시만** 한다.
자막·전사·SRT·지문을 쓰지 않고 등급·자동 정리·선택·출력·분할에 들어가지 않는다."""

SYNC_ALL_SOURCES = "*"
"""'걸지 못함' 표지의 자리 — 원본을 모르는 실패(앱 열기 고리가 재료조차 못 읽음)는 전체에."""

UPKEEP_CAPTURE = "촬영 시각"
UPKEEP_ORDER = "영상 순서"
UPKEEP_TIERS = "등급판"
UPKEEP_ALIGN = "검토 순서"

TIER_SETTINGS = tiering.TIER_SETTINGS
TIER_TAG_PREFIX = tiering.TIER_TAG_PREFIX
TIER_LABELS = tiering.TIER_LABELS
unmaterialized_red = tiering.unmaterialized_red
red_ranges = tiering.red_ranges

MEDIA_CONTENT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".jpg": "image/jpeg",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
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
    after: Callable[[], None] | None = None,
) -> Callable[[JobContext], None]:
    """분석 작업 본문 묶기 — 리비전·선택·호출 경계를 즉시 묶는다(§14.1).

    `after`는 본문이 끝난 뒤(성공·실패 모두) 한 번 부른다 — 원본 자막이 새로 심겼을 수
    있으니 자막 시각 확인(D55, 무료)을 거는 고리다.
    """

    def body(context: JobContext) -> None:
        from vlogsrt.project.raw import request_log

        try:
            with request_log.scope(workspace, context.job_id, live=providers.is_live()):
                analysis_workflow.run_analysis(
                    context, workspace, revision, order, assets, settings, workflow, providers
                )
        finally:
            if after is not None:
                after()

    return body


def _semantic_body(
    workspace: RawWorkspace,
    revision: AnalysisRevision,
    order: GlobalOrderRevision,
    assets: Mapping[str, SourceAsset],
    settings: RawUnderstandingSettings,
    workflow: RawWorkflowConfig,
    providers: analysis_workflow.AnalysisProviders,
    after: Callable[[], None] | None = None,
) -> Callable[[JobContext], None]:
    """2단계(의미) 작업 본문 묶기 — 같은 본문, 단계는 공급자가 이미 말한다."""
    return _analysis_body(workspace, revision, order, assets, settings, workflow, providers, after)


def _visual_prepare_body(
    workspace: RawWorkspace,
    facade: Any,
    base_plan_id: str,
    page: int,
    audio_selections: Mapping[str, int | None],
    expected_revision_id: str | None,
) -> Callable[[JobContext], None]:
    """영상 검토 미리보기 준비 본문 묶기 — 페이지·소리 선택·리비전 묶음을 즉시 묶는다(V1F1)."""

    def body(context: JobContext) -> None:
        visual_review.run_page_preparation(
            context,
            workspace,
            facade,
            base_plan_id=base_plan_id,
            page=page,
            audio_selections=audio_selections,
            expected_revision_id=expected_revision_id,
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
    path = workspace.source_media_path(asset)
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


CHILD_JOB_POLL_SECONDS = 0.2
"""부모 작업이 자식 작업 상태를 다시 읽는 간격."""


@dataclass
class _MirroredProgress:
    """부모에 옮겨 적은 자식 진행의 마지막 값 — 다음 폴링과 비교하는 기준."""

    total: int | None = None
    current: int | None = None


def _mirror_child_progress(row: JobRecord, context: JobContext, mirror: _MirroredProgress) -> None:
    """폴링마다 DB를 연다 — 자식 진행이 바뀐 것만 부모에 다시 적는다."""
    total = max(row.progress_total, 1)
    if total != mirror.total:
        context.set_total(total)
        mirror.total = total
    if row.progress_current != mirror.current:
        context.set_progress(row.progress_current)
        mirror.current = row.progress_current


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
        self._waiting_children: dict[tuple[Path, str], str] = {}
        # 앱 열기 정리(등급판 옮기기 포함)는 (작업공간, 할 일, 그 상태)를 이번 실행에서 한 번만
        # 건다 — 실패한 일을 화면 갱신마다 다시 걸지 않고, 앱을 다시 열면 한 번 더 건다(D52·D53).
        self._upkeep_tries: set[tuple[Path, str, str]] = set()
        # 자막 시각 확인도 (작업공간, 원본, 재료 열쇠)마다 이번 실행에서 한 번만 건다(D55).
        self._sync_tries: set[tuple[Path, str, str]] = set()
        # 확인 작업은 작업공간마다 한 번에 하나다 — 도는 동안 온 요청은 (원본 → 다시 재기)로 합친다.
        self._sync_lock = threading.Lock()
        self._sync_running: dict[Path, str] = {}
        self._sync_waiting: dict[Path, dict[str, bool]] = {}
        # 확인을 걸지 못한 원본(작업공간 → 원본 또는 SYNC_ALL_SOURCES → 이유) — 화면의 회색 표지.
        self._sync_failures: dict[Path, dict[str, str]] = {}
        # 정리 작업은 작업공간마다 한 번에 하나다 — 확인과 제출을 한 자물쇠 안에서 한다.
        self._upkeep_lock = threading.Lock()
        # 순서 확정·되채움 쓰기·검토 순서 맞춤이 동시에 불려도 같은 일을 두 번 하지 않는다.
        self._order_lock = threading.Lock()
        self._align_lock = threading.Lock()

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
            db_schema_version=workspace.db_schema_version(),
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
        key = root.expanduser().resolve()
        with self._lock:
            record = self._jobs(key).cancel(job_id)
            child_id = self._waiting_children.get((key, job_id))
            if child_id:
                self.cancel_job(key, child_id)
            return record

    def get_job(self, root: Path, job_id: str) -> JobRecord:
        return self._jobs(root).get(job_id)

    def list_jobs(self, root: Path, limit: int = 50) -> tuple[JobRecord, ...]:
        return self._jobs(root).list_jobs(limit)

    def active_jobs(self, root: Path, kinds: Collection[str]) -> tuple[JobRecord, ...]:
        """그 종류의 활성 작업 전부 — 목록 창(최근 50개) 밖의 오래된 작업도 놓치지 않는다."""
        return self._jobs(root).active_of_kinds(kinds)

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

    def selected_asset_ids(self, root: Path, paths: Sequence[str]) -> tuple[str, ...]:
        """고른 경로의 ready 원본 — 촬영 시각 순(D53)이라 분석 줄도 화면 순서를 따른다."""
        selected = set(paths)
        return tuple(
            asset.asset_id
            for asset in self.global_order_proposal(root)
            if str(Path(asset.source_root) / asset.source_relative_path) in selected
        )

    def save_selection_state(
        self, root: Path, state: dict[str, Any], *, selected: bool = True
    ) -> None:
        from vlogsrt.project.raw.selection_analysis import save_state

        save_state(self, root, state, selected=selected)

    def load_selection_state(self, root: Path) -> dict[str, Any]:
        from vlogsrt.project.raw.selection_analysis import load_state

        return load_state(self, root)

    def load_selection_states(self, root: Path) -> tuple[dict[str, Any], ...]:
        from vlogsrt.project.raw.selection_analysis import load_states

        return load_states(self, root)

    def selection_completion(self, root: Path, revision_id: str) -> tuple[str, str | None]:
        from vlogsrt.project.raw.selection_analysis import completion, completion_error

        return completion(self, root, revision_id), completion_error(self, root, revision_id)

    def source_transcript(self, root: Path, asset_id: str) -> dict | None:
        from vlogsrt.project.raw.source_transcript import load

        return load(self._workspace(root), asset_id)

    def analysis_frame_progress(
        self, root: Path, revision_id: str | None, asset_ids: tuple[str, ...]
    ) -> int:
        if not revision_id:
            return 0
        workspace = self._workspace(root)
        return sum(
            len(
                list(
                    (
                        workspace.root
                        / "analysis"
                        / revision_id
                        / analysis_workflow._source_folder(row.source_order, row.asset_id)
                        / "frames"
                    ).glob("*.jpg")
                )
            )
            for row in AnalysisStore(workspace).list_source_results(revision_id)
            if row.asset_id in asset_ids
        )

    def source_repeat_candidates(self, root: Path, asset_id: str) -> list[dict]:
        from vlogsrt.project.raw.source_transcript import cross_source_candidates

        return cross_source_candidates(self._workspace(root), asset_id)

    def source_media_references(self, root: Path, asset_id: str) -> dict | None:
        from vlogsrt.project.raw.media_refs import load

        return load(self._workspace(root), asset_id)

    def edit_source_transcript(
        self, root: Path, asset_id: str, revision: str, **fields: Any
    ) -> dict:
        from vlogsrt.project.raw.source_transcript import edit

        self._workspace(root).source_asset(asset_id)
        edited = edit(self._workspace(root), asset_id, revision, **fields)
        self._queue_sync_checks(root, (asset_id,))  # 자막이 바뀌었다 — 시각 확인을 다시(D55)
        return edited

    def relink_source_original(self, root: Path, asset_id: str, path: Path) -> dict:
        from vlogsrt.project.raw.media_refs import relink_original

        self._workspace(root).source_asset(asset_id)
        return relink_original(self._workspace(root), asset_id, path)

    def job_usage_summary(self, root: Path, job_id: str) -> Any:
        """Measured usage and computed cost of one job's paid attempts (W-7)."""
        from vlogsrt.project.raw import request_usage

        return request_usage.summarize(self._workspace(root), (job_id,))

    def analysis_usage_summary(self, root: Path, revision_id: str | None = None) -> Any:
        """Usage of the jobs that ran one analysis revision (both phases); all jobs when None."""
        from vlogsrt.project.raw import request_usage

        workspace = self._workspace(root)
        if revision_id is None:
            return request_usage.summarize(workspace)
        revision = AnalysisStore(workspace).revision(revision_id)
        if revision is None:
            return request_usage.summarize(workspace, ())
        fingerprints = (
            (ANALYSIS_JOB_KIND, revision.fingerprint),
            (
                ANALYSIS_SEMANTIC_JOB_KIND,
                analysis_workflow.semantic_job_fingerprint(revision_id),
            ),
        )
        with workspace.connect() as db:
            ids = tuple(
                str(row[0])
                for kind, fingerprint in fingerprints
                for row in db.execute(
                    "SELECT job_id FROM jobs WHERE kind=? AND input_fingerprint=?",
                    (kind, fingerprint),
                )
            )
        return request_usage.summarize(workspace, ids)

    def analysis_usage_line(self, root: Path, revision_id: str) -> str:
        """The one-line status text for a revision's usage; empty before any paid attempt."""
        from vlogsrt.project.raw import request_usage

        return request_usage.format_usage_line(self.analysis_usage_summary(root, revision_id))

    def with_request_receipts(self, root: Path, body: Any, timing: object, text: object) -> Any:
        """Open the receipt scope around a final-caption run when its providers are live."""
        from vlogsrt.project.raw import request_log
        from vlogsrt.project.raw.caption_provider import (
            TransportTextProvider,
            TransportTimingProvider,
        )

        live = isinstance(timing, TransportTimingProvider) or isinstance(
            text, TransportTextProvider
        )
        workspace = self._workspace(root)

        def scoped(context: JobContext) -> None:
            with request_log.scope(workspace, context.job_id, live=live):
                body(context)

        return scoped

    def selection_quote(self, root: Path, asset_ids: tuple[str, ...]) -> Any:
        from vlogsrt.project.raw.selection_analysis import quote

        return quote(self, root, asset_ids)

    def selection_rebound_quote(self, root: Path, measured: Any, asset_ids: tuple[str, ...]) -> Any:
        from vlogsrt.project.raw.selection_analysis import rebound_quote

        return rebound_quote(self, root, measured, asset_ids)

    def cached_selection_analysis(self, root: Path, asset_ids: tuple[str, ...]) -> str | None:
        from vlogsrt.project.raw.selection_analysis import cached_revision

        return cached_revision(self, root, asset_ids)

    def run_selection_analysis(
        self,
        root: Path,
        quote: Any,
        transport: Any,
        context: Any,
        report: Callable[[str, str | None], None],
    ) -> str:
        from vlogsrt.project.raw.selection_analysis import execute

        return execute(self, root, quote, transport, context, report)

    def publish_selection_review(self, root: Path, revision_id: str, context: Any) -> str:
        from vlogsrt.project.raw.selection_analysis import publish_review

        return publish_review(self, root, revision_id, context)

    def selection_suitability(
        self, root: Path, revision_id: str, asset_ids: tuple[str, ...]
    ) -> dict[str, list[dict]]:
        """원본별 3단 등급 구간(W-11·W-12, D51·D52) — 옛 `recommendation` 칸도 함께 싣는다.

        저장된 분석 문서(AI 판정 + 생성 자막)에서 그 자리에서 계산한다 — 로컬 측정도 보관본도
        백그라운드 작업도 없다(원본 하나에 수 ms). 증거를 읽지 못한 원본은 이 화면에서
        빠진다(예전과 같다) — 분석 기록 자체는 그대로다.
        """
        summary = self.analysis_overview(root, revision_id)
        result: dict[str, list[dict]] = {}
        if summary is None:
            return result
        workspace_root = self._workspace(root).root
        for row in summary.results:
            if row.asset_id not in asset_ids:
                continue
            try:
                report = self._tier_report_for_row(workspace_root, row)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if report is not None:
                result[row.asset_id] = [span.document() for span in report.spans]
        return result

    def tier_report(self, root: Path, revision_id: str, asset_id: str) -> Any:
        """원본 하나의 3단 등급 보고서 — 저장된 분석 문서만 읽는다(유료 호출 0회, D51·D52).

        완성 문서가 있으면 그 문서로, Luna가 도는 동안이면 진행 문서로(자막 칸이 아직 없어
        빨강 없이) 계산한다. 분석이 없으면 None이다. 아무것도 쓰지 않는다.
        """
        workspace = self._workspace(root)
        row = AnalysisStore(workspace).source_result(revision_id, asset_id)
        return None if row is None else self._tier_report_for_row(workspace.root, row)

    def _tier_report_for_row(
        self, workspace_root: Path, row: AnalysisSourceResult
    ) -> tiering.TierReport | None:
        directory, document = _tier_paths(workspace_root, row)
        if document.is_file():
            body = document.read_bytes()
            return tiering.classify(
                json.loads(body),
                audio_start=_record_audio_start(workspace_root, row),
                document_sha256=hashlib.sha256(body).hexdigest(),
            )
        progress = directory / "suitability_progress.json"
        if not progress.is_file():
            return None
        payload = json.loads(progress.read_text(encoding="utf-8"))
        if payload.get("analysis_revision_id") != row.revision_id:
            return None
        return tiering.classify(payload)

    def start_reference_import(
        self,
        root: Path,
        proposal: IngestProposal,
        *,
        prepare_preview: bool = True,
        fcp_scan: Any = None,
    ) -> JobHandle:
        from vlogsrt.project.raw.reference_ingest import register_references

        workspace = self._workspace(root)
        fingerprint = hashlib.sha256(
            repr(
                [
                    (c.source_root, c.source_relative_path, c.size_bytes, c.mtime_ns, c.inode)
                    for c in proposal.order
                ]
            ).encode()
        ).hexdigest()

        def body(context: JobContext) -> None:
            register_references(workspace, proposal, context)
            if fcp_scan is not None:
                from vlogsrt.project.raw.media_refs import bind_library

                paths = tuple(
                    str(Path(i.source_root) / i.source_relative_path) for i in proposal.order
                )
                bind_library(workspace, fcp_scan, self.selected_asset_ids(root, paths))
            self.ensure_source_order(root)
            if prepare_preview:
                self.wait_child_job(root, self.start_source_preview(root), context)

        return self.submit_job(root, "reference_import", fingerprint, body)

    def ensure_source_order(self, root: Path) -> bool:
        """전체 순서를 제안(촬영 시각 순)에 맞춘다 — 순서가 다를 때만 새 리비전. 바꿨으면 True.

        예전엔 원본 묶음(집합)만 비교해 손으로 정한 순서가 살아남았다. "언제나 만든 시각 순"이
        사용자 지시라 이제 순서까지 맞춘다(D53). 비교와 확정을 한 자물쇠 안에서 해 동시에 불려도
        같은 순서의 리비전이 둘 생기지 않는다(같은 순서인데 리비전이 다르면 검토 머리를 괜히
        다시 옮긴다).
        """
        with self._order_lock:
            proposal = tuple(asset.asset_id for asset in self.global_order_proposal(root))
            current = self.latest_global_source_order(root)
            if current is not None and _order_ids(current) == proposal:
                return False
            self.confirm_global_source_order(root)
            return True

    def start_source_preview(self, root: Path, asset_ids: tuple[str, ...] = ()) -> JobHandle:
        """Preparing source playback is local and does not require paid analysis."""
        order = self.latest_global_source_order(root)
        if order is None:
            raise RawWorkspaceError("원본 확인이 끝난 뒤 미리보기를 준비합니다")

        def body(context: JobContext) -> None:
            facade = self.review_workspace_facade(root)
            head = self._preview_head(root, facade)
            self._prepare_preview_pages(root, facade, head, asset_ids, context)

        return self.submit_job(root, "source_preview", order.revision_id + repr(asset_ids), body)

    def _preview_head(self, root: Path, facade: Any) -> ReviewRevision:
        """미리보기가 쓸 검토 머리 — 같은 영상 묶음이면 머리를 다시 깔지 않는다(D53).

        순서만 바뀐 머리는 사람 결정을 들고 옮긴다. 옮기기가 미뤄져도(그 사이 편집) 머리는 그대로
        두고 다음 갱신이 맞춘다 — 원본마다 한 장으로 다시 까는 것은 머리가 없거나 영상이 새로
        들거나 빠졌을 때뿐이다.
        """
        self.align_review_order(root)
        head = _review_head(facade)
        latest = self.latest_global_source_order(root)
        if head is not None and latest is not None and _same_sources(head, latest):
            return head
        # 새로 든 영상만 한 장으로 깐다 — 이미 있던 영상(시계가 같은)은 사람 손이 닿았든 아니든
        # 그 조각·미검토 제안을 그대로 둔다(D53: 가져오기가 손대지 않은 영상의 분석을 지우지 않게).
        plan = self.plan_segmentation(root, tile_seconds=Fraction(10**12))
        kept = tuple(clock.source_id for clock in head.clocks) if head is not None else ()
        revision: ReviewRevision = facade.initialize_review(
            plan.revision_id, preserve_source_ids=kept
        )
        return revision

    def _prepare_preview_pages(
        self,
        root: Path,
        facade: Any,
        head: ReviewRevision,
        asset_ids: tuple[str, ...],
        context: JobContext,
    ) -> None:
        for page in range(1, (len(head.segments) + VISUAL_PAGE_SIZE - 1) // VISUAL_PAGE_SIZE + 1):
            context.check_cancel()

            def prepare_page(ctx: JobContext, page: int = page) -> None:
                visual_review.run_page_preparation(
                    ctx,
                    self._workspace(root),
                    facade,
                    base_plan_id=head.base_plan_id,
                    page=page,
                    asset_ids=asset_ids,
                )

            self.wait_child_job(
                root,
                self.submit_job(
                    root, "visual_prepare", f"{head.base_plan_id}:{page}:{asset_ids}", prepare_page
                ),
                context,
            )

    def remember_fcp_library(self, root: Path, library: Path) -> None:
        with self._workspace(root).connect() as connection:
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('fcp_library_source',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(library),),
            )
            connection.commit()

    def remembered_fcp_library(self, root: Path) -> Path | None:
        with self._workspace(root).connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='fcp_library_source'"
            ).fetchone()
        if row:
            return Path(str(row[0]))
        # Earlier copied-library projects have no remembered-library metadata.
        # Their recorded source roots identify the library without scanning media.
        roots = {
            Path(asset.source_root)
            for asset in self.list_source_assets(root)
            if Path(asset.source_root).suffix.lower() == ".fcpbundle"
        }
        return next(iter(roots)) if len(roots) == 1 else None

    def import_selected_video(self, root: Path, source: Path) -> JobHandle:
        from vlogsrt.project.raw.ingest import scan_selected_source

        proposal = scan_selected_source(source, self._workspace(root).root)
        confirmation = self.confirm_ingest(root, proposal)
        return self.start_ingest_copy(root, confirmation)

    def wait_child_job(
        self, root: Path, handle: JobHandle | JobRecord, context: JobContext
    ) -> None:
        import time

        # Lightweight test contexts and a few recovery callers only expose
        # ``check_cancel``.  The child still must be cancelled immediately;
        # the registry entry is an optional optimization for parent job IDs.
        parent_job_id = getattr(context, "job_id", None)
        key = (root.expanduser().resolve(), str(parent_job_id)) if parent_job_id else None
        if key is not None:
            with self._lock:
                self._waiting_children[key] = handle.job_id
        mirror = _MirroredProgress()
        try:
            while True:
                try:
                    context.check_cancel()
                except JobCancelledError:
                    self.cancel_job(root, handle.job_id)
                    raise
                row = self.get_job(root, handle.job_id)
                _mirror_child_progress(row, context, mirror)
                if row.status == "succeeded":
                    return
                if row.status in ("failed", "interrupted", "cancelled", "paused"):
                    raise RawWorkspaceError(row.error or f"작업 {row.status}")
                time.sleep(CHILD_JOB_POLL_SECONDS)
        finally:
            if key is not None:
                with self._lock:
                    self._waiting_children.pop(key, None)

    def start_cleanup(self, root: Path) -> JobHandle:
        summary = self.analysis_overview(root)
        if summary is None or summary.revision.status != "complete":
            raise RawWorkspaceError("분석이 끝난 뒤 자동 정리를 시작하세요")
        revision_id = summary.revision.revision_id
        initialized = any(
            job.kind == "automatic_cleanup"
            and job.input_fingerprint == revision_id
            and job.status == "succeeded"
            for job in self.list_jobs(root)
        )

        def body(context: JobContext) -> None:
            context.check_cancel()
            facade = self.review_workspace_facade(root)
            head = self._cleanup_head(root, facade, summary.revision, initialized=initialized)
            # Existing user edits are authoritative; materialize their current geometry.
            package = self.package_overview(root)
            snapshot = self.segment_plan_revision(root, head.base_plan_id)
            geometry = {(s.segment_id, s.start_pts, s.end_pts) for s in head.segments}
            planned = {(s.segment_id, s.core_start_pts, s.core_end_pts) for s in snapshot.entries}
            if geometry != planned or (
                package is not None and facade.stale_media(package.package.package_dir)
            ):
                plan, mapping = facade.plan_from_review(head.base_plan_id)
                head = facade.adopt_plan_review(plan, mapping, old_revision=head)
            if not (
                package is not None
                and package.package.complete
                and package.package.plan_revision_id == head.base_plan_id
                and not facade.stale_media(package.package.package_dir)
            ):
                self.wait_child_job(
                    root, self.start_segment_extraction(root, head.base_plan_id), context
                )
            context.check_cancel()
            for page in range(
                1, (len(head.segments) + VISUAL_PAGE_SIZE - 1) // VISUAL_PAGE_SIZE + 1
            ):
                context.check_cancel()
                self.wait_child_job(
                    root,
                    self.start_visual_page_preparation(
                        root, base_plan_id=head.base_plan_id, page=page
                    ),
                    context,
                )

        return self.submit_job(root, "automatic_cleanup", revision_id, body)

    def _cleanup_head(
        self, root: Path, facade: Any, analysis: AnalysisRevision, *, initialized: bool
    ) -> ReviewRevision:
        """자동 정리가 이어 쓸 검토 머리 — 새 분석이면 다시 세우고, 옛 등급판이면 옮긴다.

        순서만 바뀐 머리(D53)는 다시 세우지 않는다 — 먼저 새 순서로 옮기고, 머리는 그 **뒤에**
        읽는다(다른 작업이 먼저 옮겼어도 옛 머리를 들고 다시 세우지 않게). 다시 세울 때도 분석
        당시가 아니라 지금 순서(촬영 시각 순) 위에 세운다.
        """
        revision_id = analysis.revision_id
        if initialized:
            self.align_review_order(root)
        head = _review_head(facade)
        latest = self._order_revision(root, None)
        if (
            head is None
            or not initialized
            or self.segment_plan_revision(root, head.base_plan_id).order_revision_id
            != latest.revision_id
        ):
            plan = self.plan_segmentation_from_analysis(root, revision_id, use_current_order=True)
            # 이 분석이 다루지 않은 영상은 지금 조각·제안을 그대로 둔다(분석 게시와 같은 규칙).
            return facade.initialize_review(
                plan.revision_id,
                revision_id,
                preserve_source_ids=self.unanalyzed_sources(root, plan, revision_id),
            )
        if not facade.needs_tier_migration(head):
            return head
        # 옛 등급판 머리(D51) — 손대지 않았으면 빨강을 조각으로 떨어뜨리고, 손댔거나 사람
        # 결정이 실려 왔으면 등급만 다시 단다(확인한 제외는 확인된 채, 미검토는 미검토인 채).
        if facade.is_untouched(head):
            rebuilt = self._migrate_unedited_head(root, facade, head)
            return rebuilt if rebuilt is not None else facade.load_head().revision
        return facade.retier(head.base_plan_id)

    def _migrate_unedited_head(
        self, root: Path, facade: Any, head: ReviewRevision
    ) -> ReviewRevision | None:
        """손대지 않은 머리를 새 등급판으로 — 원본마다 가장 최근 판정 증거로 다시 계획한다.

        원본별로 다른 분석 리비전(선택 분석의 실제 모양)을 한 계획에 모은다. 판정 증거가
        없는 원본은 옛 머리의 조각을 그대로 둔다. 유료 호출은 없다(저장된 증거만 읽는다).
        다시 계획은 원본을 다시 해시해 길다 — 그 사이 머리가 바뀌었으면(새 분석 게시·편집) 읽어
        둔 `head`를 기준으로 세운 것을 덮지 않고, 경고하고 None을 돌려준다(다음 갱신이 다시 본다).
        """
        rows = facade.tier_evidence_rows()
        plan = self.plan_segmentation_from_evidence(root, rows)
        missing = tuple(c.asset_id for c in plan.coverage if c.asset_id not in rows)
        try:
            revision: ReviewRevision = facade.initialize_review(
                plan.revision_id,
                evidence_rows=rows,
                preserve_source_ids=missing,
                expected_head=head.revision_id,
            )
        except StaleRevisionError as error:
            log.warn(f"검토가 그 사이 바뀌어 다시 세우기를 다음 갱신으로 미룬다 — {error}")
            return None
        return revision

    def plan_segmentation_from_evidence(
        self, root: Path, rows: Mapping[str, AnalysisSourceResult]
    ) -> SegmentPlanSnapshot:
        """원본마다 주어진 분석 결과 행으로 계획한다 — 행이 없는 원본은 전체 한 조각이다."""
        workspace = self._workspace(root)
        order = self._order_revision(root, None)
        intervals = {
            entry.asset_id: self._analysis_intervals(
                workspace, rows.get(entry.asset_id), entry.asset_id
            )
            for entry in sorted(order.entries, key=lambda item: item.ordinal)
        }
        return self.plan_segmentation(
            root,
            order_revision_id=order.revision_id,
            intervals=intervals,
            basis=SEGMENTATION_BASIS_SEMANTIC,
        )

    def unanalyzed_sources(
        self, root: Path, plan: SegmentPlanSnapshot, revision_id: str
    ) -> tuple[str, ...]:
        """그 분석 리비전이 다루지 않은 영상 — 다시 세울 때 지금 조각을 그대로 둘 영상이다."""
        rows = AnalysisStore(self._workspace(root)).list_source_results(revision_id)
        analyzed = {row.asset_id for row in rows if row.status != "unanalyzed"}
        return tuple(c.asset_id for c in plan.coverage if c.asset_id not in analyzed)

    def _migrate_head_tiers(self, root: Path, facade: Any) -> None:
        """지금 머리를 다시 읽어 옮긴다 — 손대지 않았으면 증거로 다시 세우고, 아니면 등급만 단다.

        표지가 있어도 옛 판 제안이 실려 있으면 옮긴다(D53 실데이터 — 옛 머리 입양이 v1 제안을
        v2 표지와 함께 되살렸다). 손대지 않았는지는 내용으로 본다(`is_untouched`).
        """
        current = facade.load_head().revision
        if not facade.needs_tier_migration(current):
            return
        if facade.is_untouched(current):
            self._migrate_unedited_head(root, facade, current)
        else:
            facade.retier(current.base_plan_id)

    # ── 앱 열기 정리(D52·D53) — 고리 하나, 한 번에 작업 하나 ─────────

    def start_upkeep(self, root: Path) -> str | None:
        """앱 열기·화면 갱신의 정리 고리 — 작업공간을 지금 판으로 맞추는 작업 하나를 건다.

        할 일은 넷이다: 촬영 시각 되채움 → 전체 순서(촬영 시각 순) → 등급판 옮기기 → 검토
        머리 순서 맞춤. 유료 호출이 없고 원본을 다시 해시할 수 있어 백그라운드로 돈다. 할 일이
        없으면 None. 정리 작업이 이미 돌고 있으면 걸지 않고 하는 일만 알린다 — 작업이 스스로
        바꾼 상태로 할 일 열쇠가 달라져도 두 번째 작업이 겹쳐 돌지 않는다(실데이터: 두 작업이 같은
        재계획을 두 번 썼다). 같은 할 일은 이번 앱 실행에서 한 번만 건다 — 실패했으면 다시 걸지
        않고 그 이유를 돌려주며, 앱을 다시 열면 한 번 더 시도한다.
        """
        pending = self._upkeep_pending(root)
        if not pending:
            return None
        with self._upkeep_lock:
            latest = self.latest_job(root, UPKEEP_JOB_KIND)
            if latest is not None and latest.status in ACTIVE_JOB_STATUSES:
                return self._upkeep_message(root, pending)
            if not self._claim_upkeep(root, pending):
                return _upkeep_failure_note(latest)
            fingerprint = hashlib.sha256(
                json.dumps(sorted(pending.items()), ensure_ascii=False).encode("utf-8")
            ).hexdigest()

            def body(context: JobContext) -> None:
                self._run_upkeep(root, context)

            self.submit_job(root, UPKEEP_JOB_KIND, fingerprint, body)
        return self._upkeep_message(root, pending)

    def latest_job(self, root: Path, kind: str) -> JobRecord | None:
        """그 종류의 가장 최근 작업 — 최근 몇 개의 창이 아니라 종류로 찾는다."""
        return self._jobs(root).latest_of_kind(kind)

    def _upkeep_pending(self, root: Path) -> dict[str, str]:
        """할 일 → 그 상태의 열쇠(같은 열쇠는 같은 일이다). 읽기만 한다.

        전체 순서의 첫 확정은 여기서 하지 않는다 — 가져오기·영상 선택의 몫이다. 이미 있는
        순서가 제안(촬영 시각 순)과 다를 때만 맞춘다.
        """
        workspace = self._workspace(root)
        ready = self.global_order_proposal(root)
        pending: dict[str, str] = {}
        if ready and workspace.meta_value(CAPTURE_BASIS_KEY) != CAPTURE_TIME_BASIS:
            pending[UPKEEP_CAPTURE] = CAPTURE_TIME_BASIS
        order = workspace.latest_global_order_revision()
        proposal = tuple(asset.asset_id for asset in ready)
        if order is not None and _order_ids(order) != proposal:
            pending[UPKEEP_ORDER] = ",".join(proposal)
        facade = self.review_workspace_facade(root)
        head = _review_head(facade)
        if head is None:
            return pending
        if facade.needs_tier_migration(head) and facade.tier_evidence_rows():
            pending[UPKEEP_TIERS] = _tier_state(facade, head)
        if order is not None and self._reorder_pending(root, head, order):
            pending[UPKEEP_ALIGN] = f"{head.revision_id}>{order.revision_id}"
        return pending

    def _claim_upkeep(self, root: Path, pending: Mapping[str, str]) -> bool:
        """이번 실행에서 아직 걸지 않은 할 일이 있으면 건 것으로 적고 True."""
        key = root.expanduser().resolve()
        claims = {(key, step, state) for step, state in pending.items()}
        with self._lock:
            fresh = claims - self._upkeep_tries
            self._upkeep_tries.update(fresh)
        return bool(fresh)

    def _upkeep_message(self, root: Path, pending: Mapping[str, str]) -> str:
        """하는 일을 사람 말로 — 어느 단계도 사람이 정한 사용/제외·메모를 바꾸지 않는다."""
        lines: list[str] = []
        if {UPKEEP_CAPTURE, UPKEEP_ORDER, UPKEEP_ALIGN} & set(pending):
            lines.append(
                "영상을 촬영 시각 순으로 맞춥니다 — 원본의 촬영 날짜를 다시 읽고, 사람이 정한"
                " 사용/제외·메모는 그대로입니다. 분석 승인은 다시 받지 않습니다."
            )
        if UPKEEP_TIERS in pending:
            facade = self.review_workspace_facade(root)
            lines.append(_tier_migration_message(facade.is_untouched(facade.load_head().revision)))
        return "\n\n".join(lines)

    def _run_upkeep(self, root: Path, context: JobContext) -> None:
        """정리 본문 — 단계마다 실패를 경고로 남기고, 안전한 다음 단계는 계속 돈다.

        촬영 시각을 다 못 읽어도 기록된 시각으로 정렬은 한다(순서 바꿈은 사람 결정을 잃지
        않는다). 등급판 옮기기가 실패하면 검토 순서 맞춤은 미룬다 — 옛 머리를 먼저 옮기면 그
        옛 제안을 새 기본에 실어 나른다(D52·D53). 실패가 있으면 끝에 모아 작업을 실패로 남긴다
        — 안내와 다음 실행의 재시도가 그것을 읽는다.
        """
        steps: tuple[tuple[str, Callable[[Path], object]], ...] = (
            (UPKEEP_CAPTURE, self._capture_step),
            (UPKEEP_ORDER, self._order_step),
            (UPKEEP_TIERS, self._tier_step),
            (UPKEEP_ALIGN, self.align_review_order),
        )
        failures: dict[str, str] = {}
        for step, run in steps:
            context.check_cancel()
            if step == UPKEEP_ALIGN and UPKEEP_TIERS in failures:
                if self._align_pending(root):
                    failures[step] = "등급판 옮기기가 실패해 다음 열기로 미뤘다"
                    log.warn(f"작업공간 정리 — {step}: {failures[step]}")
                continue
            try:
                run(root)
            except (RuntimeError, OSError, ValueError) as error:
                log.warn(f"작업공간 정리 — {step} 실패: {error}")
                failures[step] = str(error)
        if failures:
            raise RawWorkspaceError(" · ".join(f"{step}: {why}" for step, why in failures.items()))

    def _capture_step(self, root: Path) -> None:
        missing = self.refresh_capture_times(root)
        if missing:
            raise RawWorkspaceError(
                f"촬영 시각을 읽지 못한 원본 {len(missing)}개({', '.join(missing[:3])}) — 원본"
                " 볼륨을 확인하세요. 기록된 시각으로 정렬했습니다"
            )

    def _order_step(self, root: Path) -> None:
        # 첫 확정은 가져오기·영상 선택의 몫이다 — 정리는 이미 있는 순서만 맞춘다.
        if self.latest_global_source_order(root) is not None:
            self.ensure_source_order(root)

    def _tier_step(self, root: Path) -> None:
        facade = self.review_workspace_facade(root)
        if _review_head(facade) is not None and facade.tier_evidence_rows():
            self._migrate_head_tiers(root, facade)

    # ── 자막 시각 확인(D55) — 표시 전용, 무료 로컬 VAD ─────────────

    def start_sync_checks(
        self, root: Path, source_ids: Sequence[str] | None = None, *, force: bool = False
    ) -> JobHandle | None:
        """원본 자막 시각을 말소리와 대조하는 작업 하나 — 잴 것이 없으면 None.

        보고서가 없거나 재료(자막 리비전·1단계 PCM·기준)가 바뀐 원본만 잰다. `force`면 고른
        원본 전부를 다시 잰다(화면의 「다시 재기」 — VAD 칸·표본 대조는 캐시에서 온다).
        유료 호출 0 · 자막·전사·SRT·지문은 쓰지 않는다(규칙 4·5의 대응).

        이 서비스의 확인 작업이 아직 본문을 돌고 있으면 새로 만들지 않고 요청을 거기에 합친다.
        작업 지문에는 요청마다 새 값이 섞인다 — DB에 같은 지문의 활성 줄(끝나 정산을 기다리는
        작업, 정산을 못 하고 running으로 남은 줄)이 있어도 거기에 붙어 요청이 버려지지 않는다.
        다른 인스턴스와 겹쳐 한 번 더 재는 일은 해롭지 않다(표시 전용).
        """
        from vlogsrt.project.raw import sync_report

        workspace = self._workspace(root)
        pending = sync_report.pending_sources(workspace, source_ids=source_ids, force=force)
        if not pending:
            return None
        key = root.expanduser().resolve()
        with self._sync_lock:
            merged = self._merge_into_running(key, root, pending, force)
            handle = merged or self.submit_job(
                root,
                SYNC_CHECK_JOB_KIND,
                _sync_fingerprint(pending),
                self._sync_body(key, workspace, tuple(sorted(pending))),
                total=len(pending),
            )
            if merged is None:
                self._sync_running[key] = handle.job_id
        self._clear_sync_failures(key, pending)
        return handle

    def _merge_into_running(
        self, key: Path, root: Path, pending: Mapping[str, str], force: bool
    ) -> JobHandle | None:
        """이 서비스의 확인 작업이 아직 본문을 돌면 요청을 합친다 — 본문이 사라진 칸은 비운다."""
        running = self._sync_running.get(key)
        if running is None:
            return None
        if not self._jobs(root).is_executing(running):
            del self._sync_running[key]  # 본문이 없는 칸이 확인을 막지 못한다
            self._sync_waiting.pop(key, None)
            return None
        waiting = self._sync_waiting.setdefault(key, {})
        for source_id in pending:
            waiting[source_id] = waiting.get(source_id, False) or force
        return JobHandle(job_id=running, reattached=True)

    def _sync_body(
        self, key: Path, workspace: RawWorkspace, ids: tuple[str, ...]
    ) -> Callable[[JobContext], None]:
        """확인 작업 본문 — 처음 고른 원본을 재고, 도는 동안 합쳐진 요청을 이어서 잰다.

        진행 총수는 합쳐진 만큼 늘어난다(18/18에서 1/1로 되돌아가지 않는다).
        """
        from vlogsrt.project.raw import sync_report

        def body(context: JobContext) -> None:
            done = 0
            try:
                batch: tuple[str, ...] | None = ids
                while batch is not None:
                    if batch:
                        sync_report.run_checks(context, workspace, batch, done=done)
                        done += len(batch)
                    batch = self._next_sync_batch(key, workspace)
            finally:
                self._release_sync_slot(key, context.job_id)

        return body

    def _release_sync_slot(self, key: Path, job_id: str) -> None:
        """본문이 예외로 끝났을 때만 남는 칸을 비운다 — 합쳐 둔 요청은 경고로 알린다."""
        with self._sync_lock:
            if self._sync_running.get(key) != job_id:
                return
            del self._sync_running[key]
            dropped = self._sync_waiting.pop(key, {})
        if dropped:
            log.warn(
                f"자막 시각 확인 작업이 멈춰 합쳐진 요청 {len(dropped)}개를 재지 못했다 — 다음 앱"
                " 열기나 「다시 재기」가 다시 잰다"
            )

    def _next_sync_batch(self, key: Path, workspace: RawWorkspace) -> tuple[str, ...] | None:
        """도는 동안 합쳐진 요청 — 없으면 None, 이미 지금 것이면 빈 묶음.

        '합쳐진 요청 없음'과 '칸 비움'을 한 자물쇠 안에서 함께 정한다. 그 뒤에 온 요청은 칸이
        비었으니 새 작업이 된다(정산 전 DB가 아직 running이어도 — 지문이 요청마다 달라 붙지 않는다).
        """
        from vlogsrt.project.raw import sync_report

        with self._sync_lock:
            waiting = self._sync_waiting.pop(key, {})
            if not waiting:
                self._sync_running.pop(key, None)
                return None
        forced = tuple(source_id for source_id, force in waiting.items() if force)
        normal = tuple(source_id for source_id, force in waiting.items() if not force)
        pending = {
            **(sync_report.pending_sources(workspace, source_ids=normal) if normal else {}),
            **(
                sync_report.pending_sources(workspace, source_ids=forced, force=True)
                if forced
                else {}
            ),
        }
        return tuple(sorted(pending))

    def sync_report(self, root: Path, source_id: str) -> dict[str, Any] | None:
        """원본 하나의 자막 시각 판정 문서 — `verdict`는 red·amber·green·skip. 읽기만 한다.

        Final Cut 출력(W-16)이 「자막 시각 확인 필요」 키워드를 달 때 읽는 값이다(`verdict ==
        "red"`). 지금 자막 리비전의 보고서가 아직 없으면 가장 최근 보고서를 `stale: True`로,
        보고서가 하나도 없으면 None을 돌려준다.
        """
        from vlogsrt.project.raw import sync_report

        return sync_report.current_report(self._workspace(root), source_id)

    def measure_sync_checks(
        self, root: Path, source_ids: Sequence[str] | None = None, *, write: bool = True
    ) -> list[dict[str, Any]]:
        """CLI(`media sync-check`)용 — 작업 없이 그 자리에서 잰다. `write=False`면 작업공간에
        아무것도 쓰지 않는다. 유료 호출 0."""
        from vlogsrt.project.raw import sync_report

        return sync_report.measure_workspace(self._workspace(root), source_ids, write=write)

    def sync_check_view(self, root: Path, source_id: str) -> dict[str, Any] | None:
        """작업대 표지·패널의 값 — 측정 불가·걸지 못함은 회색이다(결코 초록이 아니다)."""
        from vlogsrt.project.raw import sync_report

        key = root.expanduser().resolve()
        with self._lock:
            failures = self._sync_failures.get(key, {})
            failure = failures.get(source_id) or failures.get(SYNC_ALL_SOURCES)
        latest = self.latest_job(root, SYNC_CHECK_JOB_KIND)
        running = latest is not None and latest.status in ACTIVE_JOB_STATUSES
        if failure is not None:
            return sync_report.failure_view(failure, running=running)
        return sync_report.view_of(self.sync_report(root, source_id), running=running)

    def record_sync_failure(
        self, root: Path, source_ids: Sequence[str] | None, error: BaseException
    ) -> bool:
        """확인을 걸지 못한 원본을 화면의 회색 표지로 남긴다 — 원본을 모르면 작업공간 전체.

        콘솔 경고만으로 끝나지 않게 하는 자리다(CLAUDE.md: 로그만 찍고 넘어가지 않는다). 이유가
        새로우면 True — 1초 틱마다 같은 경고를 되풀이하지 않게 부른 쪽이 고른다. 확인 작업이
        다시 걸리면(「다시 재기」·다음 고리) 지워진다.
        """
        key = root.expanduser().resolve()
        reason = f"{type(error).__name__}: {error}"
        targets = tuple(source_ids) if source_ids is not None else (SYNC_ALL_SOURCES,)
        with self._lock:
            failures = self._sync_failures.setdefault(key, {})
            fresh = any(failures.get(target) != reason for target in targets)
            failures.update(dict.fromkeys(targets, reason))
        return fresh

    def _clear_sync_failures(self, key: Path, source_ids: Iterable[str]) -> None:
        """확인이 다시 걸렸다 — 그 원본과 작업공간 전체의 '걸지 못함' 표지를 지운다."""
        with self._lock:
            failures = self._sync_failures.get(key, {})
            for target in (*source_ids, SYNC_ALL_SOURCES):
                failures.pop(target, None)

    def _note_sync_failure(
        self, root: Path, source_ids: Sequence[str] | None, error: BaseException, *, repeat: bool
    ) -> None:
        """'걸지 못함'을 표지로 남기고 경고한다 — `repeat`이 아니면 새 이유일 때만 경고한다."""
        if self.record_sync_failure(root, source_ids, error) or repeat:
            log.warn(
                f"자막 시각 확인을 걸지 못했다({type(error).__name__}: {error}) — 원래 작업은"
                " 그대로다. 화면에 회색 표지로 남고 「다시 재기」로 다시 건다"
            )

    def start_sync_upkeep(self, root: Path) -> str | None:
        """앱 열기 고리 — 보고서가 없거나 낡은 원본을 (원본, 재료)마다 실행당 한 번 잰다.

        걸지 못하면 회색 표지로 남기고 None — 1초 틱마다 같은 경고를 되풀이하지 않는다.
        """
        from vlogsrt.project.raw import sync_report

        key = root.expanduser().resolve()
        try:
            pending = sync_report.pending_sources(self._workspace(root))
        except Exception as error:  # noqa: BLE001 - 표시 전용 고리는 화면을 막지 못한다
            self._note_sync_failure(root, None, error, repeat=False)
            return None
        self._clear_sync_failures(key, ())
        with self._lock:
            fresh = sorted(
                sid for sid, state in pending.items() if (key, sid, state) not in self._sync_tries
            )
            self._sync_tries.update((key, sid, pending[sid]) for sid in fresh)
        if not fresh:
            return None
        try:
            handle = self.start_sync_checks(root, tuple(fresh))
        except Exception as error:  # noqa: BLE001 - 표시 전용 고리는 화면을 막지 못한다
            self._note_sync_failure(root, fresh, error, repeat=False)
            return None
        if handle is None:
            return None
        return f"자막 시각 확인 {len(fresh)}개를 백그라운드에서 잽니다(무료 · 표시만 합니다)."

    def _queue_sync_checks(self, root: Path, source_ids: Sequence[str]) -> None:
        """분석·자막 수정 뒤의 고리 — 부른 쪽의 결과를 **절대** 바꾸지 않는다(D55).

        표시 전용 확인을 거는 일이 실패했다고 성공한 분석이 실패가 되거나 저장된 편집이
        오류로 보이면 안 된다. 그래서 이 경계는 어떤 오류든 받는다 — 그리고 삼키지 않는다:
        이유를 경고로 남기고, 그 원본에 '걸지 못함' 회색 표지를 세운다(「다시 재기」가 다시 건다).
        """
        try:
            self.start_sync_checks(root, source_ids)
        except Exception as error:  # noqa: BLE001 - 표시 전용 고리는 부른 쪽 결과를 못 바꾼다
            self._note_sync_failure(root, source_ids, error, repeat=True)

    def _sync_after(self, root: Path, assets: Mapping[str, SourceAsset]) -> Callable[[], None]:
        """분석 작업이 끝난 뒤의 고리 — 새로 심긴 원본 자막의 시각 확인을 건다(D55)."""
        ids = tuple(assets)
        return lambda: self._queue_sync_checks(root, ids)

    def refresh_capture_times(
        self, root: Path, probe: Callable[[Path], preprocess.CreationTime] | None = None
    ) -> tuple[str, ...]:
        """ready 원본의 촬영 시각을 지금 규칙(애플 촬영 날짜 먼저)으로 다시 읽는다(D53).

        컨테이너 태그만 읽는다(디코딩·유료 호출 없음). 전부 읽은 뒤 바뀐 값만 한 트랜잭션으로
        쓰고, 전부 읽었을 때만 같은 트랜잭션에서 근거 판 표지를 남긴다 — 쓰는 동안 순서 확정을
        막아 반쯤 고친 시각으로 순서가 서지 않는다. 못 읽은 원본(볼륨이 빠졌다)은 경고하고
        기록값을 둔 채 이름을 돌려준다. 표지가 이미 있으면 아무것도 읽지 않는다.
        """
        workspace = self._workspace(root)
        if workspace.meta_value(CAPTURE_BASIS_KEY) == CAPTURE_TIME_BASIS:
            return ()
        changes, missing = self._probed_capture_times(root, probe or preprocess.probe_creation_time)
        with self._order_lock:
            workspace.record_capture_times(changes, basis=None if missing else CAPTURE_TIME_BASIS)
        return missing

    def _probed_capture_times(
        self, root: Path, read: Callable[[Path], preprocess.CreationTime]
    ) -> tuple[dict[str, str | None], tuple[str, ...]]:
        """원본마다 태그를 읽어 바뀐 값만 모은다 — 쓰지 않는다. 못 읽은 원본은 경고와 이름."""
        workspace = self._workspace(root)
        changes: dict[str, str | None] = {}
        missing: list[str] = []
        for asset in self.global_order_proposal(root):
            try:
                creation = read(workspace.source_media_path(asset))
            except (RuntimeError, OSError) as error:
                log.warn(
                    f"촬영 시각을 다시 읽지 못했다: {asset.source_name} — {error}"
                    " (기록된 값으로 정렬한다)"
                )
                missing.append(asset.source_name)
                continue
            utc = creation.utc if creation.valid else None
            measured = utc.isoformat() if utc is not None else None
            if measured != asset.capture_time:
                changes[asset.asset_id] = measured
        return changes, tuple(missing)

    def align_review_order(self, root: Path) -> bool:
        """검토 머리를 최신 전체 순서 위로 옮긴다 — 순서만 바뀐 경우에만(D53). 옮겼으면 True.

        원본 묶음이 다르면(새 영상) 이 길이 아니다. 잃을 사람 결정이 없는 머리는 저장 증거로 새
        순서 위에 다시 세운다 — 기계 제안을 편집 수백 번으로 되풀이해 '손댄 머리'로 만들지 않는다.
        사람 결정이 있으면 입양한다(선택·메모·덮기·미검토 표지를 공식 편집으로 다시 적용). 원본을
        다시 해시하는 동안 머리가 바뀌었으면(편집·다른 작업의 새 기본) 입양이 거절되고, 경고하고
        미룬다 — 옛 머리로 덮지 않는다. 분석 승인도 유료 호출도 없다.
        """
        with self._align_lock:
            order = self.latest_global_source_order(root)
            facade = self.review_workspace_facade(root)
            head = _review_head(facade)
            if order is None or head is None or not self._reorder_pending(root, head, order):
                return False
            if facade.is_untouched(head):
                return self._migrate_unedited_head(root, facade, head) is not None
            plan, mapping = facade.plan_from_review(
                head.base_plan_id, order_revision_id=order.revision_id
            )
            try:
                facade.adopt_plan_review(plan, mapping, old_revision=head)
            except StaleRevisionError as error:
                log.warn(f"검토가 그 사이 바뀌어 검토 순서 맞춤을 다음 갱신으로 미룬다 — {error}")
                return False
            return True

    def _align_pending(self, root: Path) -> bool:
        order = self.latest_global_source_order(root)
        head = _review_head(self.review_workspace_facade(root))
        return order is not None and head is not None and self._reorder_pending(root, head, order)

    def _reorder_pending(
        self, root: Path, head: ReviewRevision, order: GlobalOrderRevision
    ) -> bool:
        """순서만 바뀐 머리인가 — 계획의 순서가 최신이 아니고 원본 묶음은 같다."""
        plan = self.segment_plan_revision(root, head.base_plan_id)
        if plan.order_revision_id == order.revision_id:
            return False
        return _same_sources(head, order)

    def order_covers_same_sources(self, root: Path, order_revision_id: str) -> bool:
        """그 전체 순서가 지금 순서와 같은 원본 묶음인가 — 순서만 다르면 같은 묶음이다(D53).

        분석은 원본마다의 것이라 순서가 바뀌어도 낡지 않는다 — 다시 승인받지 않는다.
        """
        latest = self.latest_global_source_order(root)
        if latest is None:
            return False
        recorded = self._order_revision(root, order_revision_id)
        return set(_order_ids(recorded)) == set(_order_ids(latest))

    def export_finalcut_timeline(self, root: Path, context: JobContext | None = None) -> Path:
        """불러온 원본 전부를 태그 달린 타임라인 하나로 — 새 FCPXML 경로를 돌려준다(D56).

        모으고 대조하고 쓰는 일은 `finalcut_export`가 한다. 지우지 않으므로 미검토 자동
        제외 관문이 없고, Final Cut은 열지 않는다(여는 것은 화면의 몫이다).
        """
        from vlogsrt.project.raw import finalcut_export

        return finalcut_export.export(self, root, context).fcpxml

    def scan_sources(
        self,
        root: Path,
        source_root: Path,
        inspect: MediaInspect = ffprobe_media_inspector,
    ) -> IngestProposal:
        """원본 폴더를 발견·조사해 제안을 만든다 — 읽기만 한다(§5.1의 확인 단계 전)."""
        workspace = self._workspace(root)
        return scan_sources(source_root, workspace.root, inspect)

    def managed_fcp_workspace(self, library: Path) -> tuple[WorkspaceStatus, bool]:
        """FCP 라이브러리의 관리 작업공간 — 라이브러리 밖 안정 자리를 만들거나 이어 연다.

        같은 라이브러리는 언제나 같은 자리(`<이름>-vlogsrt`)를 얻는다. 자리에 정본
        작업공간이 있으면 그대로 열고(만들지 않는다), 아무것도 없으면 만든다. 자리에
        작업공간 아닌 것이 있으면 명시적으로 실패한다 — 조용히 덮지 않는다.
        """
        resolved = library.expanduser().resolve()
        root = fcp_managed_workspace_root(resolved)
        has_identity = (root / PROJECT_TOML_NAME).is_file() or (root / DB_NAME).is_file()
        if has_identity:
            try:
                return self.open_workspace(root), False
            except WorkspaceNotFoundError as error:
                raise FcpLibraryError(
                    f"관리 작업공간 자리({root})가 이미 있지만 v2 작업공간이 아니다 — {error}"
                ) from error
        try:
            return self.create_workspace(root, resolved.stem), True
        except WorkspaceAlreadyExistsError as error:
            raise FcpLibraryError(
                f"관리 작업공간 자리({root})에 이미 다른 내용이 있다 — 비우거나 옮긴 뒤"
                " 다시 시도하세요"
            ) from error

    def scan_fcp_library(
        self,
        root: Path,
        library: Path,
        inspect: MediaInspect = ffprobe_media_inspector,
        role: str = FCP_ROLE_ORIGINAL,
        *,
        originals_only: bool = False,
    ) -> FcpLibraryScan:
        """FCP 라이브러리를 발견·조사해 제안을 만든다 — 라이브러리는 읽기만 한다.

        확정·복사는 일반 SD/폴더와 같은 A2 파이프라인으로 흐른다(원본·프록시 중복은
        논리 클립으로 미리 묶여 있다).
        """
        workspace = self._workspace(root)
        return fcp_scan_fcp_library(
            library, workspace.root, inspect, role, originals_only=originals_only
        )

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
        """가져오기 읽기 모델 — 자산·순서 리비전·가져오기 작업 최근 순.

        배치를 고르지 않으면 자산은 촬영 시각 순이다(D53 — 화면 어디서나 같은 순서). 배치를
        고르면 그 배치의 확정 순번순이다(가져오기 기록).
        """
        workspace = self._workspace(root)
        assets = workspace.list_source_assets(batch_dir)
        revision = None
        if assets:
            revision = workspace.order_revision(assets[0].batch_dir)
        elif batch_dir is not None:
            revision = workspace.order_revision(batch_dir)
        jobs = tuple(job for job in self.list_jobs(root) if job.kind == INGEST_JOB_KIND)
        return (assets if batch_dir else _propose_global_order(assets)), revision, jobs

    def list_source_assets(
        self, root: Path, batch_dir: str | None = None
    ) -> tuple[SourceAsset, ...]:
        return self._workspace(root).list_source_assets(batch_dir)

    def list_order_revisions(self, root: Path) -> tuple[SourceOrderRevision, ...]:
        return self._workspace(root).list_order_revisions()

    # ── A2F1 — 배치를 넘는 전체 순서 확정(A3 소비 계약) ────────

    def global_order_proposal(self, root: Path) -> tuple[SourceAsset, ...]:
        """전체 순서 제안 — 현재 ready 원본을 촬영 시각 순으로, 모르면 맨 뒤에 자연 경로 순(D53)."""
        ready = [
            asset for asset in self._workspace(root).list_source_assets() if asset.status == "ready"
        ]
        return _propose_global_order(ready)

    def confirm_global_source_order(
        self, root: Path, asset_ids: Sequence[str] | None = None
    ) -> GlobalOrderRevision:
        """현재 선택(ready) 원본 전부의 전체 순서를 확정한다 — 불변 리비전.

        순서를 주지 않으면 제안(촬영 시각 순, D53) 그대로다. 주면
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

        같은 지문(**계획 리비전**·프로파일·핸들·도구)의 완성된 package가 있으면
        다시 만들지 않고 알린다. 같은 형상의 **새 계획 리비전**(결정적 → 의미 등)은
        새 package에서 뽑는다 — 완성 재사용 판정이 형상만 보면 새 리비전이 영영
        시작하지 못한다(루트 브라우저 재현, §6.5). 진행 중이거나 실패한 package가
        있으면 그 package를 이어 쓴다 — 같은 작업 재개가 조각 ID·번호를 유지하는
        길이다(§6.3).
        """
        workspace = self._workspace(root)
        snapshot = self._plan_snapshot(root, plan_revision_id)
        profile = extract_runner.profile_for(profile_name)
        assets = self._plan_assets(root, snapshot)
        handles = _validated_handles(handles_before, handles_after)
        fingerprint = extract_runner.extraction_fingerprint(
            _plan_bound_material(snapshot), profile.name, handles, preprocess.ffmpeg_provenance()
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
        재결합이 거부된다(다른 도구의 출력을 같은 package에 섞지 않는다). 승격 전에
        예약된 package(계획 지문만 묶은 옛 키)도 그 키로 다시 붙는다.
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
            provenance = preprocess.ffmpeg_provenance()
            fresh = extract_runner.extraction_fingerprint(
                _plan_bound_material(plan), confirmed.profile_name, handles, provenance
            )
            if fresh != record.input_fingerprint:
                legacy = extract_runner.extraction_fingerprint(
                    plan.fingerprint, confirmed.profile_name, handles, provenance
                )
                if legacy != record.input_fingerprint:
                    raise ExtractJobError(
                        f"작업 {job_id}의 지문을 지금의 도구로 다시 계산하지 못했다 —"
                        " 도구가 바뀌었으면 새 추출을 시작하세요"
                    )
                fresh = legacy
            return self.resume_job(
                root,
                job_id,
                _extract_body(workspace, plan, confirmed, assets, handles),
                fresh,
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
            order.revision_id, assets, understanding, workflow, mode, root=root
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
        previous = next(
            (
                r
                for r in reversed(AnalysisStore(workspace).list_revisions())
                if r.fingerprint == fingerprint
            ),
            None,
        )
        if previous is not None:
            revision = previous
        else:
            AnalysisStore(workspace).insert_revision(revision)
        return self.submit_job(
            root,
            ANALYSIS_JOB_KIND,
            fingerprint,
            _analysis_body(
                workspace,
                revision,
                order,
                assets,
                understanding,
                workflow,
                providers,
                self._sync_after(root, assets),
            ),
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
            order.revision_id, assets, understanding, workflow, revision.mode, root=root
        )
        return self.resume_job(
            root,
            job_id,
            _analysis_body(
                workspace,
                revision,
                order,
                assets,
                understanding,
                workflow,
                providers,
                self._sync_after(root, assets),
            ),
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
        if revision.status not in (
            "awaiting_semantic",
            "analyzing",
            "failed",
            "interrupted",
            "partial",
            "complete",
        ):
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
                        workspace,
                        revision,
                        order,
                        assets,
                        understanding,
                        workflow,
                        providers,
                        self._sync_after(root, assets),
                    ),
                    fingerprint,
                )
            return JobHandle(job_id=active.job_id, reattached=True)
        if record.kind == ANALYSIS_SEMANTIC_JOB_KIND:
            return self.resume_job(
                root,
                job_id,
                _semantic_body(
                    workspace,
                    revision,
                    order,
                    assets,
                    understanding,
                    workflow,
                    providers,
                    self._sync_after(root, assets),
                ),
                fingerprint,
            )
        return self.submit_job(
            root,
            ANALYSIS_SEMANTIC_JOB_KIND,
            fingerprint,
            _semantic_body(
                workspace,
                revision,
                order,
                assets,
                understanding,
                workflow,
                providers,
                self._sync_after(root, assets),
            ),
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
        self,
        root: Path,
        analysis_revision_id: str | None = None,
        *,
        use_current_order: bool = False,
    ) -> SegmentPlanSnapshot:
        """받아들인 분석을 A3 분할 계획으로 잇는다 — 전체 순서는 항상 전부 산다.

        분석된 원본은 문서 기록의 정확한 유리수 구간이 비디오 격자 타일이 되고,
        선택 밖·실패 원본은 **원본 전체를 한 조각으로 보존**한다(삭제·재정렬 없음,
        §6.1·§7.6). 계획 검증·원본 재검증은 A3의 순수 계층이 다시 한다 — 이
        함수는 새 계획 리비전을 만들 뿐 옛 계획·package는 그대로 둔다(§6.5).
        """
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        revision = _finished_analysis(store, analysis_revision_id)
        order = self._order_revision(
            root, None if use_current_order else revision.order_revision_id
        )
        rows = {row.asset_id: row for row in store.list_source_results(revision.revision_id)}
        intervals: dict[str, list[PlannedInterval]] = {}
        for entry in sorted(order.entries, key=lambda item: item.ordinal):
            row = rows.get(entry.asset_id)
            if row is None and not use_current_order:
                raise AnalysisError(
                    f"분석 결과 행이 없다: {entry.asset_id} — 분석을 다시 실행하세요"
                )
            intervals[entry.asset_id] = self._analysis_intervals(workspace, row, entry.asset_id)
        # 지금 순서를 쓰라고 했으면 계획도 그 순서 위에 선다 — 분석 당시 순서에 세우면 촬영 시각
        # 순으로 맞춘 머리가 옛 순서로 되돌아간다(D53).
        return self.plan_segmentation(
            root,
            order_revision_id=order.revision_id,
            intervals=intervals,
            basis=SEGMENTATION_BASIS_SEMANTIC,
        )

    def register_review_vtt(
        self, root: Path, segment_id: str, package_dir: str | None = None
    ) -> ReviewVttRegistration | None:
        """검토 자막(VTT)을 제공 가능한 자리에 등록한다 — 내용은 백엔드가 잰 그대로.

        검토 저장소 자리(`review/`)는 브라우저 제공 대상이 아니다(§4.4). 이 함수는
        백엔드가 **측정으로** 만든 VTT 본문(`ReviewVttOutput.vtt.text`)을 내용 해시
        이름의 `previews/review-vtt/` 파일로 쓰고 미디어로 등록한다 — 같은 내용은
        같은 자리(멱등), 제공은 등록 ID로만. 출처(`ReviewProvenance` — 원본 경로·
        해시 대 추출 경로·해시·잰 길이)를 함께 돌려 화면이 나란히 보여 준다.
        """
        workspace = self._workspace(root)
        facade = self.review_workspace_facade(root)
        result = facade.review_vtt(segment_id, package_dir)
        text = _vtt_text_of(result)
        if text is None:
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        relative = f"previews/review-vtt/{digest[:24]}.vtt"
        target = workspace.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            temp = target.with_name(target.name + ".partial")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, target)
        for registered in self.list_media(root, kind="preview"):
            if registered.relative_path == relative:
                return ReviewVttRegistration(registered, result.provenance)
        registration = self.register_media(root, target, "preview")
        return ReviewVttRegistration(registration, result.provenance)

    def caption_preview(self, root: Path, registration: Any) -> CaptionPreviewMedia:
        """선택 오디오 미리보기 — 같은 (렌더, 서수, 도구)면 ffmpeg 없이 등록만 돌려준다.

        브라우저는 컨테이너의 오디오 트랙을 못 고르므로(Chromium `audioTracks=null`),
        자막이 묶인 **그 트랙만** 담은 MP4를 `previews/captions/`에 만들어 등록한다.
        원본(검증 사본)은 읽기만 한다. 캐시 키는 렌더 해시·오디오 서수·도구 판·조리법
        판이고, 잰 원점은 짝지은 기록 파일에 남아 재사용 때 ffmpeg이 다시 돌지
        않는다 — 미디어·트랙이 바뀐 경우에만 만든다(표시 갱신은 만들지 않는다).
        """
        workspace = self._workspace(root)
        if not registration.ready:
            raise RawWorkspaceError(
                f"등록이 {registration.status!r}다 — 오디오 선택이 확정된 등록만 미리보기를 만든다"
            )
        if registration.verified_copy_rel is None or registration.audio_stream_index is None:
            raise RawWorkspaceError(
                "준비된 등록에 검증 사본·오디오 스트림 번호가 없다 — 손상된 행이다"
            )
        if registration.audio_ordinal is None:
            raise RawWorkspaceError("준비된 등록에 오디오 서수가 없다 — 손상된 행이다")
        ordinal = int(registration.audio_ordinal)
        tool = self._preview_tool_line()
        recipe_key = "|".join(
            (
                registration.final_video_revision,
                f"o{ordinal}",
                tool,
                CAPTION_PREVIEW_RECIPE,
            )
        )
        media_key = hashlib.sha256(recipe_key.encode("utf-8")).hexdigest()[:24]
        relative = f"{CAPTION_PREVIEW_REL}/{media_key}.mp4"
        record_relative = f"{CAPTION_PREVIEW_REL}/{media_key}.json"
        target = workspace.root / relative
        record = workspace.root / record_relative
        reused = self._reused_caption_preview(target, record, media_key)
        built = False
        if reused is None:
            # 깨진 짝(영상만 있거나 기록이 못 믿을 때)은 이 이름공간의 캐시다 —
            # 버리고 다시 만든다. 원본·확정본은 이 디렉터리에 없다.
            target.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
            extraction = preprocess.extract_caption_preview(
                workspace.root / registration.verified_copy_rel,
                target,
                audio_stream_index=registration.audio_stream_index,
            )
            payload = {
                "schema": CAPTION_PREVIEW_RECIPE,
                "key": media_key,
                "source_revision": registration.final_video_revision,
                "audio_ordinal": ordinal,
                "audio_origin": str(extraction.audio_origin),
                "duration": str(extraction.duration),
                "remuxed": extraction.remuxed,
                "video_codec": extraction.video_codec,
                "audio_codec": extraction.audio_codec,
                "ffmpeg_version": extraction.ffmpeg_version,
                "warnings": list(extraction.warnings),
            }
            record.parent.mkdir(parents=True, exist_ok=True)
            temp = record.with_name(record.name + ".partial")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, record)
            reused = (
                Fraction(str(payload["audio_origin"])),
                Fraction(str(payload["duration"])),
                bool(payload["remuxed"]),
                str(payload["video_codec"]),
                str(payload["audio_codec"]),
                str(payload["ffmpeg_version"]),
                tuple(str(item) for item in payload["warnings"]),
            )
            built = True
        origin, duration, remuxed, video_codec, audio_codec, ffmpeg_version, warnings = reused
        media_registration = self._caption_preview_registration(
            workspace, root, target, relative, built
        )
        return CaptionPreviewMedia(
            registration=media_registration,
            media_key=media_key,
            source_revision=registration.final_video_revision,
            audio_ordinal=ordinal,
            audio_origin=origin,
            duration=duration,
            remuxed=remuxed,
            video_codec=video_codec,
            audio_codec=audio_codec,
            built=built,
            ffmpeg_version=ffmpeg_version,
            warnings=warnings,
        )

    @staticmethod
    def _preview_tool_line() -> str:
        """미리보기 키에 쓰는 도구 판 — 한 번 재고 가둔다."""
        if "ffmpeg" not in _FFMPEG_VERSION_LINE:
            _FFMPEG_VERSION_LINE["ffmpeg"] = preprocess.ffmpeg_provenance().splitlines()[0]
        return _FFMPEG_VERSION_LINE["ffmpeg"]

    @staticmethod
    def _reused_caption_preview(
        target: Path, record: Path, media_key: str
    ) -> tuple[Fraction, Fraction, bool, str, str, str, tuple[str, ...]] | None:
        """재사용 가능한 미리보기 — 영상·기록이 온전히 짝지어 있을 때만 그 값을 믿는다."""
        if not target.is_file() or not record.is_file():
            return None
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("key") != media_key:
            return None
        try:
            origin = Fraction(str(payload["audio_origin"]))
            duration = Fraction(str(payload["duration"]))
            remuxed = bool(payload["remuxed"])
            video_codec = str(payload["video_codec"])
            audio_codec = str(payload["audio_codec"])
            ffmpeg_version = str(payload["ffmpeg_version"])
            warnings = tuple(str(item) for item in payload["warnings"])
        except (KeyError, TypeError, ZeroDivisionError, ValueError):
            return None
        return origin, duration, remuxed, video_codec, audio_codec, ffmpeg_version, warnings

    def _caption_preview_registration(
        self, workspace: RawWorkspace, root: Path, target: Path, relative: str, rebuilt: bool
    ) -> MediaRegistration:
        """미리보기의 등록 행 — 재사용은 그대로, 다시 만들었으면 지문을 다시 박는다.

        정리(purge)는 파일만 지우고 행은 남긴다. 다시 만든 파일은 지문이 어긋나므로
        같은 경로의 행에 `refresh_media_fingerprint`로 새 지문을 박는다 — 낡은
        file_id는 그대로 다시 쓰고(제공 안전망은 그대로), 어긋난 지문의 문은 닫힌다.
        """
        for registered in self.list_media(root, kind="preview"):
            if registered.relative_path == relative:
                if rebuilt:
                    workspace.refresh_media_fingerprint(registered.file_id)
                    probed = target.lstat()
                    return MediaRegistration(
                        file_id=registered.file_id,
                        kind=registered.kind,
                        relative_path=registered.relative_path,
                        status=registered.status,
                        size_bytes=probed.st_size,
                        registered_at=registered.registered_at,
                    )
                return registered
        return self.register_media(root, target, "preview")

    # ── V1 — 영상 우선 검토(질의·미리보기 준비·리비전 묶음 편집) ──

    def visual_review_query(
        self,
        root: Path,
        *,
        base_plan_id: str | None = None,
        selected_segment_id: str | None = None,
        page: int = 1,
        expected_revision_id: str | None = None,
        audio_selections: Mapping[str, int | None] | None = None,
        include_all_sources: bool = False,
        selection_filter: str = "all",
    ) -> visual_review.VisualReviewOverview:
        """영상 검토 질의 — 미디어 생성·제공자 호출이 전혀 없다(읽기만 한다).

        검토 헤드의 조각·시계·간격·가운데 프레임·등록 ID와 미리보기 상태를 값으로
        돌려준다. 채우는 것은 `start_visual_page_preparation`의 몫이다 — 이 호출이
        파일을 쓰거나 ffmpeg을 돌리는 일은 없다. `expected_revision_id`를 주면 지금
        머리와 다를 때 `StaleRevisionError`로 거부한다(화면이 본 것이 낡았음을 아는
        길; 생략하면 언제나 최신). `audio_selections`는 원본별 명시적 소리 서수다 —
        준 원본은 그 선택의 미리보기 기록만 보고, 생략한 원본은 가장 최근 기록을
        본다. 원본의 트랙 목록(`sources[].audio_tracks`)은 준비가 남긴 기록에서 온다.
        """
        workspace = self._workspace(root)
        facade = self.review_workspace_facade(root)
        active = tuple(
            job
            for job in self.list_jobs(root)
            if job.kind == VISUAL_PREPARE_JOB_KIND and job.status in ACTIVE_JOB_STATUSES
        )
        return visual_review.query_overview(
            workspace,
            facade,
            base_plan_id=base_plan_id,
            selected_segment_id=selected_segment_id,
            page=page,
            active_jobs=active,
            expected_revision_id=expected_revision_id,
            audio_selections=audio_selections,
            include_all_sources=include_all_sources,
            selection_filter=selection_filter,
        )

    def start_visual_page_preparation(
        self,
        root: Path,
        *,
        base_plan_id: str | None = None,
        page: int = 1,
        audio_selections: Mapping[str, int | None] | None = None,
        expected_revision_id: str | None = None,
    ) -> JobHandle:
        """요청 페이지의 미리보기를 채우는 작업을 제출한다 — 없는 항목만 순서대로.

        같은 (기본 계획, 페이지, 소리 선택)의 활성 작업이 있으면 그 작업에 다시
        붙는다(재클릭·새로고침 중복 방지, §14.1). 작업 본문은 원본 전체 브라우저
        MP4와 그 페이지의 썸네일을 `previews/visual/`에 만든다 — 원본·package는
        읽기만 한다.

        `audio_selections`는 원본별 **명시적** 소리 선택이다: 키에 없는 원본은 기본
        (첫 트랙, 없으면 침묵), 값 `None`은 명시적 침묵(트랙이 있어도 영상만),
        서수(0 이상 정수)는 그 트랙만. 형이 틀린 값은 제출 순간에 거부하고, 트랙
        수 밖의 서수는 작업이 조사해 실패로 닫는다 — 어느 쪽도 첫 트랙으로 조용히
        환원하지 않는다. 선택은 작업 지문·캐시 짝에 묶인다(서수마다 별개 미리보기).
        `expected_revision_id`를 주면 제출 전에 머리와 맞는지 보고, 작업이 실행되는
        순간에도 다시 본다 — 그새 머리가 앞서 갔으면 실패로 닫지 새 기하를 몰래
        다시 해석하지 않는다.
        """
        workspace = self._workspace(root)
        facade = self.review_workspace_facade(root)
        head = facade.load_head(base_plan_id).revision
        visual_review.require_head_revision(head, expected_revision_id)
        resolved_base = head.base_plan_id
        selections = visual_review.validated_audio_selections(audio_selections)
        fingerprint = visual_review.preparation_fingerprint(
            resolved_base, page, visual_review.audio_selection_material(selections)
        )
        return self.submit_job(
            root,
            VISUAL_PREPARE_JOB_KIND,
            fingerprint,
            _visual_prepare_body(
                workspace, facade, resolved_base, page, selections, expected_revision_id
            ),
            total=visual_review.VISUAL_PAGE_SIZE,
        )

    # ── V1 편집 제안 — 저장보다 앞서고 아무 것도 쓰지 않는다 ──────

    def visual_snap_proposal(
        self,
        root: Path,
        *,
        base_plan_id: str | None = None,
        expected_revision_id: str,
        segment_id: str,
        desired_pts: Fraction | int,
    ) -> visual_review.VisualSnapProposal:
        """분할 점의 읽기 전용 제안 — 서버가 잰 스냅(동률 앞장)과 판정만 돌려준다.

        `expected_revision_id`는 화면이 보여주는 리비전이다 — 머리가 앞서 갔으면
        조각이 살아남았더라도 `StaleRevisionError`(유효 저장 토큰은 새로 고침한
        리비전의 제안에서만 나온다). `desired_pts`는 서브틱 유리수까지 정확히 받는다
        (int·Fraction; float·무한·NaN은 거부). 저장을 원하면 돌려온
        `head_revision_id`를 `expected_revision_id`로, `snap_pts`를 분할 점으로 쓰면
        된다. 이 호출은 리비전·파일·작업 어느 것도 바꾸지 않는다(코디네이터 계약 —
        제안이 저장에 앞선다).
        """
        return visual_review.snap_proposal(
            self.review_workspace_facade(root),
            base_plan_id=base_plan_id,
            expected_revision_id=expected_revision_id,
            segment_id=segment_id,
            desired_pts=desired_pts,
        )

    def visual_boundary_proposal(
        self,
        root: Path,
        *,
        base_plan_id: str | None = None,
        expected_revision_id: str,
        left_segment_id: str,
        right_segment_id: str,
        desired_pts: Fraction | int,
    ) -> visual_review.VisualSnapProposal:
        """경계 이동의 읽기 전용 제안 — 맞닿음·길이 0 판정까지 미리 보여준다.

        묶음·정확 유리수 계약은 `visual_snap_proposal`과 같다.
        """
        return visual_review.boundary_proposal(
            self.review_workspace_facade(root),
            base_plan_id=base_plan_id,
            expected_revision_id=expected_revision_id,
            left_segment_id=left_segment_id,
            right_segment_id=right_segment_id,
            desired_pts=desired_pts,
        )

    # ── V1 편집 — 화면이 준 리비전에 묶인다(stale 거부) ──────────

    def visual_split_segment(
        self,
        root: Path,
        *,
        base_plan_id: str,
        expected_revision_id: str,
        segment_id: str,
        split_pts: int,
    ) -> ReviewRevision:
        """조각을 둘로 나눈다 — 길이 0이 되는 자리는 거부, 출판은 파사드 트랜잭션."""
        return visual_review.split_segment(
            self.review_workspace_facade(root),
            base_plan_id=base_plan_id,
            expected_revision_id=expected_revision_id,
            segment_id=segment_id,
            split_pts=split_pts,
        )

    def visual_adjust_boundary(
        self,
        root: Path,
        *,
        base_plan_id: str,
        expected_revision_id: str,
        left_segment_id: str,
        right_segment_id: str,
        boundary_pts: int,
    ) -> ReviewRevision:
        """맞닿은 두 조각의 경계를 옮긴다 — 안 닿은 짝·길이 0 이동은 거부다."""
        return visual_review.adjust_boundary(
            self.review_workspace_facade(root),
            base_plan_id=base_plan_id,
            expected_revision_id=expected_revision_id,
            left_segment_id=left_segment_id,
            right_segment_id=right_segment_id,
            boundary_pts=boundary_pts,
        )

    def visual_merge_segments(
        self,
        root: Path,
        *,
        base_plan_id: str,
        expected_revision_id: str,
        first_segment_id: str,
        second_segment_id: str,
    ) -> ReviewRevision:
        """같은 원본의 맞닿은 두 조각을 합친다 — 건너뛴 짝은 거부다."""
        return visual_review.merge_segments(
            self.review_workspace_facade(root),
            base_plan_id=base_plan_id,
            expected_revision_id=expected_revision_id,
            first_segment_id=first_segment_id,
            second_segment_id=second_segment_id,
        )

    def visual_set_selection(
        self,
        root: Path,
        *,
        base_plan_id: str,
        expected_revision_id: str,
        segment_id: str,
        selection: str,
    ) -> ReviewRevision:
        """사용/제외 표시를 바꾼다 — 조각은 지워지지 않는다(§6.4)."""
        return visual_review.set_selection(
            self.review_workspace_facade(root),
            base_plan_id=base_plan_id,
            expected_revision_id=expected_revision_id,
            segment_id=segment_id,
            selection=selection,
        )

    # ── A5/A6·A7 백엔드 작업공간 — 공개 경계로만 내놓는다(병합 뒤 활성) ──

    def review_workspace_facade(self, root: Path) -> Any:
        """검토 작업공간 파사드(W3) — 검토 리비전·편집·측정 VTT·확정·정리의 정본.

        화면은 `vlogsrt.project.raw.service`만 가져가므로 백엔드 모듈도 이 handle로
        쓴다(계약: UI 임포트 경로 예외). 모듈이 이 빌드에 없으면 정직한 오류로
        닫는다 — 조용한 대체·부분 동작은 없다. 사업 논리는 전부 그 모듈에 있다
        (여기서 다시 쓰지 않는다).
        """
        try:
            from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
        except ImportError as error:
            raise WorkspaceModuleUnavailableError(
                "검토 작업공간 백엔드(review_workspace)가 이 빌드에 없다 — 병합 뒤"
                " 활성화된다. 그 전까지 검토 화면은 안내만 보인다."
            ) from error
        return ReviewWorkspaceFacade(self._workspace(root).root)

    def caption_workspace(self, root: Path) -> Any:
        """자막 작업공간(W2) — 등록→추정→승인→실행→검토→확정의 정본 handle.

        검토 파사드와 같은 규칙: 화면은 이 handle만 쓰고, 모듈이 없으면 정직하게
        닫는다. 유료 호출 재료는 사람이 주입하는 제공자뿐이다(이 handle은
        제공자를 스스로 만들지 않는다).
        """
        try:
            from vlogsrt.project.raw.caption_workspace import open_caption_workspace
        except ImportError as error:
            raise WorkspaceModuleUnavailableError(
                "자막 작업공간 백엔드(caption_workspace)가 이 빌드에 없다 — 병합 뒤"
                " 활성화된다. 그 전까지 자막 화면은 안내만 보인다."
            ) from error
        return open_caption_workspace(self._workspace(root))

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
            probe = preprocess.probe_raw_media(workspace.source_media_path(asset))
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
            prior = store.source_result(semantic_revision_id, asset_id)
            if analysis_workflow._complete_source_current(workspace, asset, prior):
                continue  # completed source makes no paid request on this retry
            if prior is not None and prior.status == "failed" and not prior.document_path:
                continue  # preparation failure remains a failed source; others may proceed
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
        *,
        root: Path | None = None,
        bindings: Mapping[str, list[str]] | None = None,
    ) -> str:
        """분석 리비전 지문 — 순서·선택·설정·모델·모드를 묶는다(작업 재결합의 키).

        `bindings`(원본별 자막·미디어 참조 지문)를 미리 잰 값으로 주면 다시 읽지 않는다 — 순서
        리비전마다 지문을 견줄 때 원본 결속은 한 번만 읽는다(D53).
        """
        if bindings is None:
            bindings = self.source_bindings(root, assets) if root is not None else {}
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
                "source_bindings": bindings,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def source_bindings(
        self, root: Path, assets: Mapping[str, SourceAsset]
    ) -> dict[str, list[str]]:
        """원본별 결속 지문 — 고친 자막·미디어 참조가 분석 지문을 바꾼다."""
        from vlogsrt.project.raw import media_refs, source_transcript

        workspace = self._workspace(root)
        return {
            asset_id: [
                source_transcript.fingerprint(workspace, asset_id),
                media_refs.fingerprint(workspace, asset_id),
            ]
            for asset_id in assets
        }

    def _analysis_intervals(
        self,
        workspace: RawWorkspace,
        row: AnalysisSourceResult | None,
        asset_id: str,
    ) -> list[PlannedInterval]:
        """원본 하나의 계획 구간 — 분석됨은 기록 그대로, 아니면 전체 보존 한 조각."""
        if (
            row is not None
            and row.status in ("analyzed", "cached", "partial")
            and row.record_path is not None
        ):
            recorded = self._recorded_intervals(workspace, row, asset_id)
            if recorded:
                return recorded
        asset = _verified_asset(workspace, asset_id)
        facts = _measured_source(workspace, asset)
        playable = (facts.exact_end_pts - facts.clock.first_pts) * facts.clock.time_base
        return [PlannedInterval(start=Fraction(0), end=playable)]

    def _recorded_intervals(
        self, workspace: RawWorkspace, row: AnalysisSourceResult, asset_id: str
    ) -> list[PlannedInterval]:
        """기록 구간 위에 자를 자리를 더한다 — 안전한 창 가장자리, 자동 제외·빨강 등급 후보.

        제거 후보의 가장자리는 프레임 안쪽으로 붙인다(시작 올림·끝 내림) — 발화 여유가
        줄지 않는다. 녹색·노랑의 가장자리는 자를 자리가 아니다(D51).
        """
        assert row.record_path is not None
        try:
            record = json.loads((workspace.root / row.record_path).read_text(encoding="utf-8"))
            from vlogsrt.project.raw.auto_cleanup import saved_decisions

            if row.document_path:
                record["auto_decisions"] = saved_decisions(
                    workspace.root / row.document_path, record, asset_id, row.revision_id
                )
            spans = record["intervals"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise AnalysisError(f"분석 기록을 읽지 못했다: {row.record_path} — {error}") from error
        intervals = [
            PlannedInterval(start=_json_fraction(item["start"]), end=_json_fraction(item["end"]))
            for item in spans
        ]
        if not intervals:
            return []
        cuts = {point for span in intervals for point in (span.start, span.end)}
        cuts.update(self._safe_window_cuts(workspace, row, intervals))
        cuts.update(self._removal_cuts(workspace, row, record))
        ordered = sorted(cuts)
        return [PlannedInterval(start=a, end=b) for a, b in zip(ordered, ordered[1:], strict=False)]

    @staticmethod
    def _safe_window_cuts(
        workspace: RawWorkspace, row: AnalysisSourceResult, intervals: list[PlannedInterval]
    ) -> set[Fraction]:
        if not row.document_path:
            return set()
        from vlogsrt.project.raw.suitability import safe_window_edges

        document = json.loads((workspace.root / row.document_path).read_text(encoding="utf-8"))
        return {
            point
            for point in safe_window_edges(document)
            if intervals[0].start < point < intervals[-1].end
        }

    def _removal_cuts(
        self, workspace: RawWorkspace, row: AnalysisSourceResult, record: Mapping[str, Any]
    ) -> set[Fraction]:
        decisions = list(record.get("auto_decisions", []))
        report = self._tier_report_for_row(workspace.root, row) if row.document_path else None
        if report is not None:
            decisions.extend(tiering.red_decisions(report))
        if not decisions:
            return set()
        frame = _json_fraction(record["timeline"]["video"]["frame_duration"])
        cuts: set[Fraction] = set()
        for decision in decisions:
            start, end = _json_fraction(decision["start"]), _json_fraction(decision["end"])
            a, b = math.ceil(start / frame) * frame, math.floor(end / frame) * frame
            if a < b:
                cuts.update((a, b))
        return cuts

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


def _sync_fingerprint(pending: Mapping[str, str]) -> str:
    """요청마다 다른 지문 — 끝나 정산을 기다리는 작업이나 running으로 남은 줄에 붙지 않게(D55)."""
    body = json.dumps([sorted(pending.items()), uuid.uuid4().hex], ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _order_ids(revision: GlobalOrderRevision) -> tuple[str, ...]:
    """전체 순서 리비전의 원본 순서 — 확정 순번대로."""
    return tuple(entry.asset_id for entry in sorted(revision.entries, key=lambda e: e.ordinal))


def _review_head(facade: Any) -> ReviewRevision | None:
    """검토 머리 — 아직 하나도 없으면 None(정상 상태다). 깨진 저장소는 그대로 오류다."""
    return facade.load_head().revision if facade.has_review() else None


def _finished_analysis(store: AnalysisStore, revision_id: str | None) -> AnalysisRevision:
    """계획에 쓸 분석 리비전 — 주면 그것, 아니면 가장 최근. 끝난 리비전만 받는다."""
    revisions = store.list_revisions()
    revision = None
    if revision_id is not None:
        revision = next((item for item in revisions if item.revision_id == revision_id), None)
    elif revisions:
        revision = revisions[-1]
    if revision is None:
        raise AnalysisError("분석 리비전이 없다 — 먼저 의미 분석을 실행하세요")
    if revision.status not in ("complete", "partial", "failed"):
        raise AnalysisError(
            f"분석이 완성 상태가 아니다({revision.status}) — 완료된 리비전만 계획에 쓴다"
        )
    return revision


def _tier_state(facade: Any, head: ReviewRevision) -> str:
    """등급판 옮기기의 할 일 열쇠 — 판·표지·옛 제안 자리로만 만든다(머리 리비전이 아니라).

    다시 세우면 머리 리비전이 늘 바뀐다 — 그것을 열쇠로 쓰면 옮겨도 남는 것이 있을 때 앱을
    열 때마다가 아니라 화면 갱신마다 다시 세우기(원본 재해시)가 되풀이된다. 같은 것이 남으면
    같은 열쇠라 이번 실행에서는 다시 걸지 않는다.
    """
    spans = json.dumps(facade.stale_tier_proposal_spans(head))
    digest = hashlib.sha256(spans.encode("utf-8")).hexdigest()[:16]
    return f"{TIER_SETTINGS.tier_version}|표지 {facade.has_tier_marker(head)}|옛 제안 {digest}"


def _same_sources(head: ReviewRevision, order: GlobalOrderRevision) -> bool:
    """검토 머리와 전체 순서가 같은 영상 묶음인가 — 순서만 다를 수 있다."""
    return {clock.source_id for clock in head.clocks} == set(_order_ids(order))


def _upkeep_failure_note(latest: JobRecord | None) -> str | None:
    """이미 건 할 일의 마지막 결과 — 실패했으면 그 이유, 아니면 None."""
    if latest is not None and latest.status == "failed":
        return f"작업공간 정리 실패: {latest.error} — 앱을 다시 열면 한 번 더 시도합니다."
    return None


def _tier_migration_message(untouched: bool) -> str:
    """등급판 옮기기 안내 — 손대지 않은 머리는 다시 세우고, 손댄 머리는 등급만 다시 단다."""
    if untouched:
        return (
            "등급 기준이 AI 판정 + 자막으로 바뀌어 검토를 다시 세웁니다 — 손대지 않은"
            " 검토라 제외 제안 구간을 조각으로 나눕니다."
        )
    return (
        "등급 기준이 AI 판정 + 자막으로 바뀌어 등급 표시만 다시 답니다 — 사람이 정한"
        " 사용/제외는 그대로이고, 근거가 사라진 옛 제외 제안은 사용으로 되돌립니다."
    )


def _json_fraction(value: Mapping[str, Any]) -> Fraction:
    return Fraction(int(value["num"]), int(value["den"]))


def _tier_paths(workspace_root: Path, row: AnalysisSourceResult) -> tuple[Path, Path]:
    """결과 행의 증거 폴더와 완성 문서 — 캐시된 행은 다른 리비전 폴더의 문서를 가리킨다."""
    from vlogsrt.project.raw.analysis_workflow import ANALYSIS_DOCUMENT_NAME, _source_folder

    directory = (
        workspace_root
        / ANALYSIS_ROOT
        / row.revision_id
        / _source_folder(row.source_order, row.asset_id)
    )
    document = (
        workspace_root / row.document_path
        if row.document_path
        else directory / ANALYSIS_DOCUMENT_NAME
    )
    return directory, document


def _record_audio_start(workspace_root: Path, row: AnalysisSourceResult) -> Fraction:
    """PCM 표본 0의 공통 시계 위치 — 기록이 없으면 0(파일 시각과 같다고 본다)."""
    if row.record_path is None:
        return Fraction(0)
    record = json.loads((workspace_root / row.record_path).read_text(encoding="utf-8"))
    start = record.get("timeline", {}).get("audio", {}).get("common_start")
    return _json_fraction(start) if start else Fraction(0)


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
    path = workspace.source_media_path(asset)
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
    path = workspace.source_media_path(asset)
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


EXTRACT_PLAN_BINDING_SCHEMA = "raw-extract-plan-binding/v1"
"""추출 캐시 키가 계획 리비전 신원까지 묶는다는 표기(같은 형상의 새 리비전 구분)."""


def _plan_bound_material(snapshot: SegmentPlanSnapshot) -> str:
    """작업·package 신원의 계획 재료 — 계획 지문에 **리비전 신원**까지 묶는다.

    묶는 것은 **작업/package 지문**뿐이다: 같은 형상의 새 계획 리비전(결정적 →
    의미 등)도 새 package에서 뽑고, 같은 리비전의 재추출은 멱등 경계로 막힌다
    (§6.5). 조각 매체의 렌더 재사용 판정은 이 키를 쓰지 않는다 — 매체는 언제나
    원본 SHA·core 범위·프로파일·도구 버전에서 결정되므로, 리비전이 갈라져도
    같은 범위의 매체 계산은 여전히 같은 입력을 본다.
    """
    return json.dumps(
        {
            "schema": EXTRACT_PLAN_BINDING_SCHEMA,
            "plan_revision": snapshot.revision_id,
            "plan_fingerprint": snapshot.fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def caption_run_identity(revision: str, run_id: str, fingerprint: str) -> Any:
    """자막 실행 신원 값 — 백엔드(W2)의 정의를 그대로 쓴다(여기서 다시 만들지 않는다).

    화면은 이 공개 경로로만 신원을 만든다(계약: UI 임포트 경로 예외). 백엔드
    모듈이 없는 빌드에서는 정직한 오류로 닫는다.
    """
    try:
        from vlogsrt.project.raw.caption_workspace_types import RunIdentity
    except ImportError as error:
        raise WorkspaceModuleUnavailableError(
            "자막 작업공간 백엔드(caption_workspace)가 이 빌드에 없다 — 병합 뒤 활성화된다."
        ) from error
    return RunIdentity(final_video_revision=revision, run_id=run_id, fingerprint=fingerprint)


@dataclass(frozen=True)
class ReviewVttRegistration:
    """검토 VTT 등록 결과 — 등록 미디어와 출처(원본 대 추출)를 함께 돌려준다.

    `provenance`는 W3 `ReviewProvenance` 그대로다(경로·SHA-256·잰 매체 길이).
    화면은 이 값을 나란히 보여 조각이 어느 원본에서 왔는지 숨기지 않는다.
    """

    registration: MediaRegistration
    provenance: Any


CAPTION_PREVIEW_REL = "previews/captions"
"""선택 오디오 미리보기의 자리 — `previews/`는 정리 대상이라 재생성이 원칙이다."""

CAPTION_PREVIEW_RECIPE = "caption-preview/v1"
"""미리보기 캐시 키의 조리법 판 — 키 재료나 인코딩 설정이 바뀌면 이 값을 올린다."""

_FFMPEG_VERSION_LINE: dict[str, str] = {}
"""미리보기 키에 쓰는 도구 판 — 프로세스에서 한 번만 재고 가둔다(조회마다 재생 없음)."""


@dataclass(frozen=True)
class CaptionPreviewMedia:
    """선택 오디오 미리보기의 등록 결과 — 표시 갱신 판정 키와 잰 원점까지.

    `audio_origin`은 미리보기 파일의 선택 오디오를 자막 시계와 같은 16 kHz
    파이프라인으로 잰 원점이다. 큐 시각은 최종 영상 시계(등록 원점 포함)이므로,
    미리보기 재생 위치는 `audio_origin − 등록 원점 + 큐 시각`이다. `built`는 이
    호출에서 ffmpeg을 실제로 돌렸는지를 가린다(미디어·트랙이 바뀐 경우에만 참이어야
    한다 — 표시 갱신은 거짓).
    """

    registration: MediaRegistration
    media_key: str
    source_revision: str
    audio_ordinal: int
    audio_origin: Fraction
    duration: Fraction
    remuxed: bool
    video_codec: str
    audio_codec: str
    built: bool
    ffmpeg_version: str
    warnings: tuple[str, ...]


def _plan_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _vtt_text_of(result: Any) -> str | None:
    """검토 VTT 결과(`ReviewVttOutput`)에서 렌더 본문만 꺼낸다 — 계약 밖 모양은 못 들어온다.

    백엔드의 `.vtt`는 `RenderedVtt`(본문 문자열)다. 본문이 비었으면 None —
    조용한 빈 성공이 아니라 호출자가 안내로 답하는 자리다.
    """
    text = getattr(getattr(result, "vtt", None), "text", None)
    if isinstance(text, str) and text.strip():
        return text
    return None
