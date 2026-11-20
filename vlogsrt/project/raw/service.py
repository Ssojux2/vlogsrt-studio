"""UI·CLI가 공유하는 v2 작업공간 공개 서비스(계획 §4·§15).

`ui/workspace_service.py`가 가져올 수 있는 **정확히 이 모듈**이다(A0 계약 예외).
여기엔 gradio·fastapi가 없다 — HTTP와 화면 조립은 UI 계층의 몫이고, 이 계층은
작업공간·등록·탐색·작업·접근 정책을 값으로 다룬다. 네트워크 호출도 없다: 오프라인
기본은 `config.RawWorkflowConfig`가 이미 못 박고 있고 이 서비스는 그 값을 읽기만
한다(§14.3 — UI가 우회하지 않는다).
"""

from __future__ import annotations

import contextvars
import errno
import hashlib
import json
import math
import os
import threading
import types
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction
from functools import partial
from pathlib import Path
from typing import Any

from vlogsrt import log, oscompat, preprocess
from vlogsrt.env import ANALYSIS_CLAUDE_KEY_ENV, ANALYSIS_GEMINI_KEY_ENV
from vlogsrt.project.raw import (
    analysis_workflow,
    file_memo,
    read_scope,
    render_plan,
    segmentation,
    selection_analysis,
    source_verify,
    tiering,
    timing_engine,
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
    ANALYSIS_PROXY,
    CONTEXT_REVIEW,
    DEFAULT_PROMPT_REVISION,
    EDITABLE_TIER_KEYS,
    VISUAL_ISSUE_KINDS,
    RawAnalysisConcurrency,
    RawTierSettings,
    RawUnderstandingSettings,
    tier_settings_from_overrides,
    tier_settings_overrides,
    tier_settings_values,
    validate_analysis_concurrency,
    validate_understanding_settings,
)
from vlogsrt.project.raw.analysis_store import (
    ANALYSIS_ROOT,
    AnalysisStore,
    analyzing_revisions_on,
)
from vlogsrt.project.raw.browser_runtime import (
    STT_KEY_ENV,
    BrowserRuntimeError,
    ClaudeCredentials,
    EnvironmentCredentials,
    GeminiCredentials,
    asr_estimate_digest,
    browser_network_mode,
    capture_browser_credentials,
    capture_claude_credentials,
    capture_environment_credentials,
    capture_gemini_credentials,
    live_transport_for_credentials,
    mint_asr_approvals,
    mint_luna_approvals,
    production_live_transport,
    semantic_estimate_digest,
)
from vlogsrt.project.raw.config import (
    ANALYSIS_MODEL_CHOICES,
    ANALYSIS_PROVIDER_ANTHROPIC,
    ANALYSIS_PROVIDER_GOOGLE,
    DEFAULT_ANALYSIS_MODEL,
    FOLDER_WORKSPACE,
    GEMINI_MODEL_PREFIX,
    GEMINI_TRANSCRIBE_PROFILES,
    MODE_AUTO,
    MODE_CHOICES,
    MODE_LANDSCAPE,
    MODE_PORTRAIT,
    PREVIOUS_DEFAULT_ANALYSIS_MODELS,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
    WORKFLOW_SCHEMA_VERSION,
    RawWorkflowConfig,
    analysis_model_label,
    analysis_provider,
    claude_profile,
    gemini_profile,
    official_price_model,
    text_model_label,
    timing_engine_for_model,
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
from vlogsrt.project.raw.jobs import (
    JobCancelledError,
    JobContext,
    JobManager,
    active_fingerprints_on,
    poll_rows_on,
)
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
    UNSUPPORTED_GRID_REASON,
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
    UnsupportedSource,
    WorkspaceAlreadyExistsError,
    WorkspaceModuleUnavailableError,
    WorkspaceNotFoundError,
    WorkspaceStatus,
)
from vlogsrt.project.raw.review_notes import (
    REVIEW_NOTE_PREFIX,
    compose_review_notes,
    split_review_notes,
)
from vlogsrt.project.raw.review_notes import (
    auto_note_line as review_auto_note_line,
)
from vlogsrt.project.raw.review_notes import (
    has_auto_note as review_has_auto_note,
)
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    RANGE_NORMAL,
    RANGE_NOT_RECOMMENDED,
    RANGE_RECOMMENDED,
    SELECTION_EXCLUDED,
    ReviewError,
    ReviewRevision,
    ReviewSegment,
    StaleRevisionError,
    is_unreviewed_auto_exclusion,
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
    AnalysisApprovalError,
    OnlineAnalysisApproval,
    OnlineVisualAnalysisApproval,
    estimate_luna_cost,
    online_env_blocked,
    semantic_analysis_budget,
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
    anchor_wal,
    new_segment_plan_revision_id,
    release_wal_anchor,
)

__all__ = [
    # UI는 이 모듈만 가져갈 수 있다(정확 경로 예외) — 계약 오류도 여기서 다시 낸다.
    "TIMING_ENGINE_CHOICES",
    "WORKSPACE_MODE_KEY",
    "MODE_AUTO",
    "MODE_LANDSCAPE",
    "MODE_PORTRAIT",
    "TIMING_ENGINE_GEMINI",
    "TIMING_ENGINE_WHISPER",
    "timing_engine_facts",
    "timing_engine_key",
    "ACTIVE_JOB_STATUSES",
    "ANALYSIS_CLAUDE_KEY_ENV",
    "ANALYSIS_GEMINI_KEY_ENV",
    "ANALYSIS_CONCURRENCY",
    "ANALYSIS_JOB_KIND",
    "ANALYSIS_MODEL_CHOICES",
    "ANALYSIS_MODES",
    "ANALYSIS_PHASES",
    "ANALYSIS_PROVIDER_ANTHROPIC",
    "ANALYSIS_PROVIDER_GOOGLE",
    "ANALYSIS_ROOT",
    "ANALYSIS_SEMANTIC_JOB_KIND",
    "AnalysisApprovalError",
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
    "ClaudeCredentials",
    "GeminiCredentials",
    "DEFAULT_ANALYSIS_MODEL",
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
    "STT_KEY_ENV",
    "SEGMENTATION_BASIS_SEMANTIC",
    "SegmentCoverage",
    "SegmentPlanEntry",
    "SegmentPlanSnapshot",
    "SegmentationError",
    "SourceAsset",
    "SourceInspection",
    "SourceNestingError",
    "SourceOrderRevision",
    "UNSUPPORTED_GRID_REASON",
    "UnsupportedSource",
    "TIER_LABELS",
    "TIER_SETTINGS",
    "TIER_TAG_PREFIX",
    "UPKEEP_JOB_KIND",
    "ANALYSIS_PROXY_JOB_KIND",
    "RENDER_EXPORT_JOB_KIND",
    "TIMELINE_EXPORT_JOB_KIND",
    "SRT_TRANSLATE_JOB_KIND",
    "SUBTITLE_BURN_JOB_KIND",
    "SYNC_CHECK_JOB_KIND",
    "SELECTION_MODE_FULL",
    "SELECTION_MODE_TRANSCRIBE",
    "SELECTION_MODES",
    "TRANSCRIBED_STAGE",
    "RELISTEN_STAGE",
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
    "analysis_model_label",
    "analysis_provider",
    "text_model_label",
    "DEFAULT_NEW_HEARING_ENGINE",
    "gemini_text_per_minute",
    "asr_estimate_digest",
    "validate_analysis_concurrency",
    "REVIEW_NOTE_PREFIX",
    "ReviewError",
    "StaleRevisionError",
    "browse_folder",
    "capture_environment_credentials",
    "capture_browser_credentials",
    "capture_claude_credentials",
    "capture_gemini_credentials",
    "claude_profile",
    "gemini_profile",
    "official_price_model",
    "browser_network_mode",
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
    "compose_review_notes",
    "review_auto_note_line",
    "review_boundary_edit",
    "review_has_auto_note",
    "review_merge_edit",
    "review_note_edit",
    "review_override_edit",
    "review_selection_edit",
    "review_split_edit",
    "split_review_notes",
    "semantic_estimate_digest",
    "middle_frame_index",
    "red_ranges",
    "snapped_frame_index",
    "unmaterialized_red",
]

ANALYSIS_PROXY_JOB_KIND = "analysis_proxy"
"""4K 원본 하나의 25% 분석 프록시를 불러올 때 미리 만드는 작업(D100 §2) — 무료 로컬. 조리법·열쇠는
분석이 쓰는 그대로(`analysis-proxy/v1`, D47)라 1단계가 이것을 다시 쓴다."""


@dataclass(frozen=True)
class PollStamp:
    """화면 틱의 도장(W-46b) — 같으면 자막·작업·선택 상태가 그대로다(`poll_stamp`)."""

    transcripts: tuple[tuple[str, int], ...]
    recent_jobs: tuple[tuple[str, str, str, int], ...]
    active_jobs: tuple[tuple[str, str], ...]
    selection_states: int

    @property
    def job_states(self) -> frozenset[tuple[str, str]]:
        """작업 (아이디, 상태)의 묶음 — 최근 작업과 활성 작업 전부(D63의 정리 고리 열쇠)."""
        recent = {(job_id, status) for job_id, _kind, status, _progress in self.recent_jobs}
        return frozenset(recent | set(self.active_jobs))


RENDER_EXPORT_JOB_KIND = "render_export"
KEPT_HUMAN_REASON = render_plan.PART_HUMAN_RECOMMENDED
"""「내보낼 부분」 조각이 사람 추천에서 왔다는 까닭 — 레일이 자동 녹색과 따로 그린다(W-46b)."""
"""추천만 이어붙인 초안 영상 + SRT + 보고서를 내는 작업(W-44, D100 §5b) — 무료 로컬. 작업공간마다
한 번에 하나다(같은 지문이면 도는 작업에 다시 붙는다)."""

TIMELINE_EXPORT_JOB_KIND = "timeline_export"
"""「타임라인으로 보내기」(W-71b, D126) — 렌더 없이 원본 범위 타임라인을 만드는 작업(무료 로컬)."""

SUBTITLE_BURN_JOB_KIND = "subtitle_burn"
"""자막 입히기(W-65, D116) — 영상 하나에 한국어(+ 영어) SRT를 번인해 `<이름>.burned.mp4`를 쓰는 무료
로컬 작업(`burn_export`). 같은 영상이면 도는 작업에 다시 붙는다."""

SRT_TRANSLATE_JOB_KIND = "srt_translate"
"""한국어 SRT → 영어 SRT(W-63, D114) — 사람이 누른 SRT 하나를 옮겨 쓰는 작업. 옮길 것이 있으면
유료라 「라이브러리 닫기」가 막는다(닫기 목록 밖의 종류). 같은 SRT면 도는 작업에 다시 붙는다."""

FOLDER_WRITE_REFUSALS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
"""폴더 안에 작업공간을 만들 수 없다는 뜻의 오류 — 형제 폴더로 물러난다(D100 §2)."""

UPKEEP_JOB_KIND = "workspace_upkeep"
"""앱 열기·화면 갱신의 정리 작업 하나 — 촬영 시각·영상 순서·등급판·검토 순서(D52·D53)."""

SYNC_CHECK_JOB_KIND = "subtitle_sync_check"
"""자막 시각 확인 작업(D55) — 무료 로컬 VAD로 원본 자막과 말소리를 대조해 **표시만** 한다.
자막·전사·SRT·지문을 쓰지 않고 자동 정리·선택·출력·분할에 들어가지 않는다. 등급만 좁은 예외다
(D60·D65): 지금 리비전 보고서의 판정·표지가 `_tier_timing`을 거쳐 추천을 보통으로 내리고, 초록이면서
확인 필요 줄이 적은 영상에서만 표의 `usable` × 발화 많음 칸 하나를 추천으로 올린다."""

SYNC_ALL_SOURCES = "*"
"""'걸지 못함' 표지의 자리 — 원본을 모르는 실패(앱 열기 고리가 재료조차 못 읽음)는 전체에."""

OLD_PROMPT_REFUSAL = (
    "이 분석은 이전 분석 기준으로 시작해 이어서 진행할 수 없습니다. 새 분석을 시작해 주세요"
    " (이미 받은 전사는 다시 사지 않습니다)."
)
"""이전 안내문 리비전으로 시작한 분석을 이으려 할 때의 거절 문구(D58 18b) — 내부 리비전 이름은
싣지 않는다(D57)."""

WORKSPACE_MODE_KEY = "workspace_mode"
"""작업공간 모드(D97) 표지 — `auto|landscape|portrait`, 없거나 모르는 값이면 자동이다."""

FINALCUT_DROP_DISABLED_KEY = "finalcut_drop_disabled"
FINALCUT_DROP_DISABLED_ON = "on"
FINALCUT_DROP_DISABLED_OFF = "off"
"""「꺼진 클립 빼고 보내기」(D94) 작업공간 표지 — 없거나 모르는 값이면 끔(전체 타임라인)이다."""

TIER_SETTINGS_KEY = "tier_settings"
"""작업공간별 추천 기준(W-42, D100 §3) 표지 — 기본값과 다른 칸만의 JSON 덮기. 없거나 비었으면
기본 기준(`TIER_SETTINGS`)이다. 지문·승인·견적 밖이고, 기준 digest가 등급의 낡음 열쇠에 실린다."""

TIMING_ENGINE_CHOICES: tuple[str, ...] = timing_engine.NEW_HEARING_ENGINES
"""새로 들을 수 있는 엔진 — Gemini 하나다(whisper는 은퇴했다, D163)."""
timing_engine_facts = timing_engine.facts
DEFAULT_NEW_HEARING_ENGINE = timing_engine.DEFAULT_NEW_HEARING_ENGINE
"""「새로 듣는 엔진」의 기본(D104 — Gemini). 화면이 표지를 읽지 못할 때도 이 값을 보인다."""
timing_engine_key = timing_engine.key_for_model


def gemini_text_per_minute(on: date | None = None) -> Decimal | None:
    """Gemini 글자 경로(W-35b)의 공식 분당 단가 — 재확인 기한이 지나면 None(가격 미정)."""
    from vlogsrt.project.raw.capabilities import timing_price_per_minute
    from vlogsrt.project.raw.config import GEMINI_TEXT_SETTINGS

    return timing_price_per_minute(GEMINI_TEXT_SETTINGS.model, on or date.today())


SELECTION_MODE_FULL = selection_analysis.MODE_FULL
SELECTION_MODE_TRANSCRIBE = selection_analysis.MODE_TRANSCRIBE
SELECTION_MODES = selection_analysis.MODES
TRANSCRIBED_STAGE = selection_analysis.TRANSCRIBED_STAGE
RELISTEN_STAGE = selection_analysis.RELISTEN_STAGE
APPROVAL_MODE_META_KEY = "selection_approval_mode"
"""작업대의 승인 범위(전체 · 「전사만」, D59 19b) — 사람이 고른 값이 앱을 다시 열어도 남는다."""
RELISTEN_EDITED_REFUSAL = (
    "이 영상의 원본 자막에는 직접 고친 내용이 있어 다시 듣지 않습니다 — 고친 자막을 덮어쓰지"
    " 않습니다. 유료 호출은 시작하지 않았습니다."
)
RELISTEN_UNTRANSCRIBED_REFUSAL = (
    "이 영상은 아직 전사하지 않았습니다 — 분석을 승인하면 처음부터 듣습니다."
)
RELISTEN_SILENT_REFUSAL = (
    "이 영상에는 소리가 없어 다시 들을 것이 없습니다 — 끝난 영상 분석은 그대로 둡니다."
)
RELISTEN_MEDIA_MISSING_REFUSAL = (
    "원본 파일을 읽지 못했습니다 — 원본 연결을 확인하세요. 아무것도 바꾸지 않았습니다."
)
CONTEXT_NOTHING = "맥락을 평가할 분석이 없습니다 — 영상 분석을 먼저 마치세요"
REFILL_JOB_KIND = "speech_refill"
"""말소리 빈칸 재전사 작업(D59 19f) — 승인한 범위의 조각만 whisper(·두 번째 귀)에 다시
묻는다(유료)."""
REFILL_UNTRANSCRIBED = "이 영상은 아직 전사하지 않았습니다 — 분석을 승인하면 처음부터 듣습니다."
REFILL_WAITING = "자막 시각 확인이 끝나면 빈칸을 셉니다(무료) — 잠시 뒤에 다시 보세요."
REFILL_UNMEASURED = (
    "말소리를 잴 수 없어 빈칸을 고르지 못했습니다 — 「자막 시각 확인」 칸의 이유를 보세요."
)
REFILL_RELISTEN_PENDING = (
    "다시 듣기를 기다리는 영상입니다 — 다시 듣기를 먼저 끝낸 뒤에 빈칸을 채우세요. 빈칸을 먼저"
    " 채우면 다시 들은 전사가 이 자막을 덮지 못해 다시 듣기에 낸 돈이 헛됩니다."
)
REFILL_WHISPER_RETIRED = (
    "whisper로 들은 영상입니다 — whisper는 은퇴해(D163) 빈칸을 새로 묻지 않습니다. 「다시 듣기」로"
    " Gemini에 다시 들은 뒤에 빈칸을 채우세요."
)
REFILL_CHANGED = (
    "빈칸이 바뀌었습니다 — 바뀐 빈칸을 확인하고 다시 승인하세요. 유료 호출은 시작하지 않았습니다."
)
REFILL_GLOSSARY_CHANGED = (
    "용어집이 바뀌었습니다. 다시 승인하세요 — 유료 호출은 시작하지 않았습니다."
)
REFILL_RELISTEN_SETTLE_REFUSAL = (
    "다시 듣기를 기다리고 있어 받은 줄을 넣지 않았습니다 — 다시 들은 전사가 이 자막을 덮을 수 있게"
    " 그대로 둡니다. 「전사만」 승인으로 다시 듣기를 마친 뒤 빈칸을 다시 승인하세요 — 같은 구간은"
    " 받은 답이 남아 있어 다시 사지 않습니다."
)
RELISTEN_REFILL_RUNNING_REFUSAL = (
    "이 영상의 말소리 빈칸 재전사가 진행 중입니다 — 끝난 뒤 다시 눌러 주세요. 아무것도 바꾸지"
    " 않았습니다."
)
REFILL_PCM_MISSING = (
    "말소리를 잰 오디오를 찾지 못했습니다 — 「자막 시각 확인」을 다시 잰 뒤에 채우세요. 유료 호출은"
    " 시작하지 않았습니다."
)
# 시각 맞춤 적용·되돌리기(D59 19h) — 무료이고 사람이 누를 때만 한다. 화면 말은 존댓말이고 「VAD」
# 대신 「말소리 측정」이다(D59 19e-4). 금액·모델 이름은 없다(D57).
SNAP_UNTRANSCRIBED = "이 영상은 아직 전사하지 않았습니다 — 맞출 원본 자막이 없습니다."
SNAP_WAITING = (
    "자막 시각 확인이 지금 자막을 재면 시각 맞춤 제안이 보입니다(무료) — 잠시 뒤에 다시 보세요."
)
SNAP_UNMEASURED = (
    "말소리를 잴 수 없어 시각 맞춤을 제안하지 못했습니다 — 「자막 시각 확인」 칸의 이유를 보세요."
)
SNAP_SPEECH_MISSING = (
    "지금 자막을 잰 말소리 측정을 쓸 수 없습니다 — 「자막 시각 확인」 칸의 「다시 재기(무료)」를"
    " 누른 뒤에 다시 보세요."
)
SNAP_RELISTEN_PENDING = (
    "다시 듣기를 기다리는 영상입니다 — 다시 듣기를 먼저 끝낸 뒤에 시각을 맞추세요. 지금 맞추면 직접"
    " 고친 자막이 되어 다시 들은 전사가 이 자막을 덮지 못하고, 다시 듣기에 낸 돈이 헛됩니다."
)
SNAP_REFILL_RUNNING = (
    "이 영상의 말소리 빈칸 재전사가 진행 중이라 지금은 시각 맞춤을 적용하거나 되돌리지 않습니다 —"
    " 끝나면 할 수 있습니다."
)
SNAP_ANALYSIS_RUNNING = (
    "이 영상을 분석하는 중이라 지금은 시각 맞춤을 적용하거나 되돌리지 않습니다 — 분석이 끝나면"
    " 할 수 있습니다."
)
SNAP_APPLIED_ONCE = (
    "이미 시각 맞춤을 적용한 자막입니다 — 다시 맞추려면 「되돌리기」로 적용 전 자막으로 돌린 뒤 새"
    " 제안을 들어 보세요."
)
"""한 걸음 규칙(D59 19h) — 적용한 리비전은 다시 맞추지 않는다. 쓰기 계층의 거절
(`source_transcript.SNAP_ALREADY_APPLIED`)은 이 말에 「아무것도 바꾸지 않았습니다.」를 붙인
것이다."""
SNAP_CHANGED = (
    "시각 맞춤 제안이 화면에 보이는 것과 다릅니다 — 새 제안을 듣고 다시 적용하세요. 아무것도 바꾸지"
    " 않았습니다."
)
SNAP_NOTHING_CHANGED = "아무것도 바꾸지 않았습니다."
SNAP_ALL_CHANGED = (
    "확인한 뒤로 어느 영상의 시각 맞춤 제안이 바뀌었습니다 — 「모든 영상 시각 맞춤 확인」을 다시"
    " 누르세요."
)
"""일괄 적용(W-25e, D75)의 거절 — 확인 때 묶은 영상·sha가 지금과 다르면 어느 영상도 쓰지 않는다."""
SNAP_ALL_NOTHING = "말소리 경계에 맞춰 바꿀 시각이 없습니다."
TRUTH_NO_TRANSCRIPT = "원본 자막이 없어 정답을 찍을 수 없습니다."
TRUTH_REVISION_CHANGED = (
    "화면이 본 뒤에 원본 자막이 바뀌었습니다 — 새로고침한 뒤 다시 찍으세요. 아무것도 찍지"
    " 않았습니다."
)
TRUTH_LINE_MISSING = "그 자막 줄이 지금 원본 자막에 없습니다. 아무것도 찍지 않았습니다."
TRUTH_OUT_OF_RANGE = "재생 위치가 원본 길이 밖입니다. 아무것도 찍지 않았습니다."
SNAP_LISTEN_FIRST = 5
"""「시각 맞춤 제안」 칸이 먼저 들어 보라고 내놓는 줄 수 — 끝이 크게 움직인 줄부터(D59 19g (c)).
문구 길이일 뿐 판정이 아니다(적용은 언제나 모든 줄이다)."""
RELISTEN_SNAPPED_REFUSAL = (
    "시각 맞춤을 적용한 자막이라 다시 듣지 않습니다 — 다시 들으려면 「되돌리기」로 적용 전 자막으로"
    " 돌린 뒤에 누르세요. 유료 호출은 시작하지 않았습니다."
)


def _source_has_audio(path: Path) -> bool:
    """다시 듣기 전 소리 확인(ffprobe 한 번) — 원본을 못 읽으면 경로 없는 존댓말로 거절한다."""
    try:
        return bool(preprocess.probe_raw_media(path).audio)
    except (OSError, RuntimeError) as error:
        log.warn(f"다시 듣기: 원본을 읽지 못했다 — {type(error).__name__}")
        raise RawWorkspaceError(RELISTEN_MEDIA_MISSING_REFUSAL) from error


UPKEEP_CAPTURE = "촬영 시각"
UPKEEP_ORDER = "영상 순서"
UPKEEP_TIERS = "등급판"
UPKEEP_ALIGN = "검토 순서"
UPKEEP_COVER = "격자 맞춤"

TIER_SETTINGS = tiering.TIER_SETTINGS

ANALYSIS_DEFAULT_KEY = "analysis_model_default"
"""작업공간 표지 — 영상 분석 모델을 마지막으로 옮겨 준 기본의 이름(D87, 한 번만 옮긴다)."""
ANALYSIS_MODEL_BUSY_KINDS = (ANALYSIS_JOB_KIND, ANALYSIS_SEMANTIC_JOB_KIND)
"""이 작업이 도는 동안에는 모델을 옮기지 않는다 — 견적이 본 모델과 실행이 갈리지 않게."""
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
    # 부른 쪽(`_analysis_selection`)이 원본을 확인했다 — 같은 stat이면 다시 조사하지 않는다(W-45).
    probe = preprocess.probe_raw_media_memo(path)
    try:
        clock = preprocess.probe_extraction_clock_memo(path, probe=probe)
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
        **_estimate_cost_fields(workflow, settings, window_count, asr_chunks, note),
        **_timing_engine_fields(workflow, audio_seconds if probe.audio else None),
    )


def _timing_engine_fields(
    workflow: RawWorkflowConfig, audio_seconds: Fraction | None
) -> dict[str, Any]:
    """whisper가 아닌 시간 엔진(D92)의 견적 칸 — 모델과, 오디오 전체를 한 번 듣는 금액(올림).

    whisper 원본은 칸이 없다(견적 지문이 바이트 그대로 — whisper 전사비는 D57 결정 7대로 금액 없이
    둔다). 단가 재확인 기한이 지났으면 금액은 None이다(가격 미정)."""
    from vlogsrt.project.raw.capabilities import timing_price_per_minute

    engine = timing_engine_for_model(workflow.models.timing_model)
    if engine.key == TIMING_ENGINE_WHISPER or audio_seconds is None:
        return {}
    per_minute = timing_price_per_minute(engine.model, date.today())
    if per_minute is None:
        return {"timing_model": engine.model, "timing_usd": None}
    exact = Decimal(audio_seconds.numerator) / Decimal(audio_seconds.denominator) / Decimal(60)
    from vlogsrt.project.raw.cross_check import USD_STEP

    usd = (exact * per_minute).quantize(USD_STEP, rounding=ROUND_CEILING)
    return {"timing_model": engine.model, "timing_usd": str(usd)}


def _estimate_cost_fields(
    workflow: RawWorkflowConfig,
    settings: RawUnderstandingSettings,
    window_count: int,
    asr_chunks: int,
    note: str,
) -> dict[str, Any]:
    """1단계 견적의 금액 칸 — Claude는 공식 단가의 보수적 상한, Luna는 단가 미확인(D54).

    상한은 견적 지문(`asr_estimate_digest`)에 들어간다 — 사람이 본 금액이 승인에 묶인다.
    Luna 견적의 칸·문구는 W-14 이전과 같다.
    """
    model = workflow.models.luna_model
    budget = semantic_analysis_budget(
        model, settings, max_luna_requests=window_count, max_asr_chunks=max(1, asr_chunks)
    )
    estimate = estimate_luna_cost(window_count, budget)
    return {
        "luna_request_count": estimate.request_count,
        "price_status": estimate.price_status,
        "user_ceiling_total": _decimal_text(estimate.user_ceiling_total),
        "ceiling_usd": _decimal_text(estimate.ceiling_total),
        "note": f"{note}. {_semantic_cost_label(model)}: {estimate.note}",
    }


def _semantic_cost_fields(
    model: str, settings: RawUnderstandingSettings, window_count: int
) -> dict[str, Any]:
    """2단계 견적의 금액 칸 — 요청 하나의 상한은 승인 예산이 그대로 묶는 값이다(D54)."""
    budget = semantic_analysis_budget(
        model, settings, max_luna_requests=max(1, window_count), max_asr_chunks=1
    )
    estimate = estimate_luna_cost(window_count, budget)
    return {
        "luna_request_count": estimate.request_count,
        "price_status": estimate.price_status,
        "user_ceiling_total": _decimal_text(estimate.user_ceiling_total),
        "request_ceiling_usd": _decimal_text(budget.request_ceiling_usd),
        "ceiling_usd": _decimal_text(estimate.ceiling_total),
        "note": f"1단계 실측 지문에 묶였다 — {_semantic_cost_label(model)}: {estimate.note}",
    }


def _semantic_cost_label(model: str) -> str:
    """견적 문구의 모델 이름 — Luna는 W-14 이전 문구 그대로 'Luna'다."""
    if official_price_model(model):
        return analysis_model_label(model)
    return "Luna"


def _decimal_text(value: Any) -> str | None:
    return None if value is None else str(value)


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


def tier_criteria_choices() -> dict[str, list[tuple[str, str]]]:
    """「추천 기준」 낱말 목록 칸이 고를 수 있는 (쉬운 이름, 낱말) — 화면은 이 표만 보인다(W-46a).

    화면 문제 종류(`VISUAL_ISSUE_KINDS`) 차례 그대로이고 이름은 등급 근거와 같은 말이다.
    """
    return {
        "visual_issue_caps": [
            (tiering.VISUAL_ISSUE_LABELS[kind], kind) for kind in VISUAL_ISSUE_KINDS
        ]
    }


def _video_folder(folder: Path) -> Path:
    """폴더 불러오기의 입력 — 펴서(resolve) 폴더인지 본다. 아니면 사유와 함께 실패한다.

    작업공간 폴더 그 자체(`_vlogsrt` 등)는 영상 폴더가 아니다 — 받으면 그 안의 프록시·미리보기를
    원본으로 스캔하고 작업공간 안에 작업공간을 만든다(W-46a).
    """
    resolved = folder.expanduser().resolve()
    if not resolved.is_dir():
        raise IngestError(f"영상 폴더가 아니다: {folder}")
    if (resolved / PROJECT_TOML_NAME).is_file() or (resolved / DB_NAME).is_file():
        raise IngestError(f"작업공간 폴더다 — 그 위의 촬영 영상 폴더를 고르세요: {folder}")
    return resolved


def _existing_folder_workspace(resolved: Path) -> Path | None:
    """그 폴더의 이미 있는 작업공간 — 안쪽(`_vlogsrt`), 형제(`-vlogsrt`) 순서. 없으면 None."""
    inside = resolved / FOLDER_WORKSPACE.workspace_dirname
    sibling = resolved.with_name(resolved.name + FOLDER_WORKSPACE.sibling_suffix)
    for root in (inside, sibling):
        if (root / PROJECT_TOML_NAME).is_file() or (root / DB_NAME).is_file():
            return root
    return None


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
        # 영상마다 첫 컷 썸네일도 (작업공간, 썸네일 키)마다 이번 실행에서 한 번만 건다(W-24 세션 I)
        # — 계속 실패하는 원본의 썸네일을 그리기마다 다시 걸지 않는다.
        self._thumb_tries: set[tuple[Path, str]] = set()
        # 계획이 덮지 못했다고 기록한 원본(D68)을 지금 시계로 덮을 수 있는가 — (작업공간, 원본,
        # 내용 해시)마다 이번 실행에서 한 번만 잰다(D69). 같은 내용·같은 규칙이면 답이 같다.
        self._coverable: dict[tuple[Path, str, str], bool] = {}
        self._thumb_epochs: dict[Path, int] = {}
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
        # 풀어 쓴 작업공간 뿌리(절대 경로만) — 그리기마다 같은 뿌리를 다시 풀지 않는다(D167).
        self._root_keys: dict[Path, Path] = {}

    def _root_key(self, root: Path) -> Path:
        """작업공간 열쇠(풀어 쓴 절대 경로) — 절대 경로는 한 번 풀어 기억한다(D167).

        그리기 한 번이 같은 뿌리를 약 200번 풀었다(칸마다 `lstat`). 상대 경로와 `~`는 작업 폴더·홈에
        따라 달라지므로 매번 푼다. 이미 연 작업공간은 예전부터 처음 푼 열쇠에 묶여 있었다."""
        if not root.is_absolute():
            return root.expanduser().resolve()
        with self._lock:
            known = self._root_keys.get(root)
        if known is None:
            known = root.resolve()
            with self._lock:
                self._root_keys[root] = known
        return known

    def _workspace(self, root: Path) -> RawWorkspace:
        key = self._root_key(root)
        with self._lock:
            workspace = self._workspaces.get(key)
            if workspace is None:
                workspace = RawWorkspace.open(key)
                self._workspaces[key] = workspace
            return workspace

    def _jobs(self, root: Path) -> JobManager:
        key = self._root_key(root)
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
        """작업공간을 연다 — 작업 복구는 하지 않는다(서버 시작 때 한 번만, §14.1).

        캐시된 작업공간을 다시 열 때도 WAL 닻을 다시 세운다 — 화면이 닫으며 닻을 놓았을 수
        있다(`close_workspace`). 이미 서 있으면 순서만 앞으로 당긴다(새 연결 0).
        """
        workspace = self._workspace(root)
        anchor_wal(workspace.root / DB_NAME)
        return self.workspace_status(workspace.root)

    def close_workspace(self, root: Path) -> None:
        """화면이 작업공간을 닫는다(D64 세션 F) — WAL 닻만 놓는다. DB·파일은 그대로다.

        캐시된 작업공간과 작업 관리자는 버리지 않는다 — 관리자를 새로 만들면 그 시작 복구가
        다른 탭에서 살아 있는 작업을 중단으로 적을 수 있다(관리자는 서비스에 하나). 다시 열면
        `open_workspace`가 닻을 다시 세운다.
        """
        release_wal_anchor(root.expanduser().resolve() / DB_NAME)

    def workspace_status(self, root: Path) -> WorkspaceStatus:
        workspace = self._workspace(root)
        config = workspace.load_workflow()
        # 최근 창이 아니라 상태로 센다 — 짧은 작업이 쌓여도 오래 도는 작업이 빠지지 않는다(W-24 J).
        active = len(self._jobs(workspace.root).active())
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

    def adopt_default_analysis_model(self, root: Path) -> str | None:
        """기존 작업공간의 영상 분석 모델을 지금 기본(Gemini 4 Hydrogen, D168)으로 **한 번**
        옮긴다(D87).

        사용자 지시(2026-10-25 「주로 Flash를 이용해서 처리」 · 2026-11-19 「Gemini 4 Hydrogen로
        대체해줘」) — 새 작업공간과 지금 작업공간 모두다.
        옮기는 것은 **예전 기본(Sonnet 5.5 · 3.8 Flash)에 머문 작업공간**뿐이다 — 사람이 따로 고른
        Opus·Luna·Argon은 그대로 두고 표지만 남긴다. 표지(`meta`의 `analysis_model_default`)에 옮긴
        기본의 이름을 남겨, 그 뒤 사람이 고른 모델은 다시 열어도 그대로다(기본이 또 바뀌면 그때 다시
        본다).
        분석 작업이 도는 동안에는 옮기지 않고 다음 열기에 다시 본다 — 견적이 본 모델과 실행이
        갈리지 않게. 옮기기는 설정 하나뿐이다 — 이미 산 분석·검토 머리·자막은 그대로이고, 다시
        분석하려면 새 견적을 보고 승인한다(유료). 옮겼으면 예전 모델 이름을 돌려준다.
        """
        workspace = self._workspace(root)
        if workspace.meta_value(ANALYSIS_DEFAULT_KEY) == DEFAULT_ANALYSIS_MODEL:
            return None
        config = workspace.load_workflow()
        previous = config.models.luna_model
        moving = previous in PREVIOUS_DEFAULT_ANALYSIS_MODELS
        if moving:
            if self.active_jobs(workspace.root, ANALYSIS_MODEL_BUSY_KINDS):
                return None
            models = replace(config.models, luna_model=DEFAULT_ANALYSIS_MODEL)
            self.update_settings(workspace.root, replace(config, models=models))
        workspace.set_meta_value(ANALYSIS_DEFAULT_KEY, DEFAULT_ANALYSIS_MODEL)
        if not moving:
            return None
        log.out(
            f"영상 분석 모델을 {analysis_model_label(DEFAULT_ANALYSIS_MODEL)}로 옮겼다"
            f"(예전: {previous})"
        )
        return previous

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

    def jobs_by_id(self, root: Path, job_ids: Collection[str]) -> dict[str, JobRecord]:
        """아이디로 찾은 작업 — 최근 목록 창과 무관하다. 없는 아이디는 빠진다(W-24 세션 G)."""
        return self._jobs(root).by_ids(job_ids)

    def active_jobs(self, root: Path, kinds: Collection[str]) -> tuple[JobRecord, ...]:
        """그 종류의 활성 작업 전부 — 목록 창(최근 50개) 밖의 오래된 작업도 놓치지 않는다."""
        return self._jobs(root).active_of_kinds(kinds)

    def recent_jobs(
        self, root: Path, kinds: Collection[str], limit: int | None = 50
    ) -> tuple[JobRecord, ...]:
        """그 종류의 최근 작업(최근 것이 먼저) — 다른 종류의 작업이 창을 밀어내지 못한다(W-24 J).

        `limit`이 None이면 그 종류 전부다.
        """
        return self._jobs(root).recent_of_kinds(kinds, limit)

    def poll_stamp(self, root: Path, *, recent: int) -> PollStamp:
        """화면 틱의 다시 그릴지 정하는 도장 — DB 연결 하나(W-46b, D100 §7b).

        원본 자막의 (원본, 최대 차례) · 최근 작업 `recent`개의 (아이디, 종류, 상태, 진행) ·
        활성 작업
        (아이디, 상태) · 저장된 선택 분석 상태들의 CRC. 본문은 풀지 않는다 — 예전 2초 틱은 틱마다
        원본 자막 본문 전부·선택 상태 전부·작업 목록을 따로 읽었다. 읽기만 한다.
        """
        from vlogsrt.project.raw import source_transcript

        with self._workspace(root).connect() as connection:
            transcripts = source_transcript.latest_sequences_on(connection)
            recent_jobs, active_jobs = poll_rows_on(connection, recent)
            states = selection_analysis.states_digest_on(connection)
        return PollStamp(transcripts, recent_jobs, active_jobs, states)

    def all_active_jobs(self, root: Path) -> tuple[JobRecord, ...]:
        """종류를 가리지 않는 활성 작업 전부(오래된 것부터) — 「지금 도는 작업」이 읽는다(D61)."""
        return self._jobs(root).active()

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

    def sweep_preview_leftovers(self, root: Path) -> int:
        """끊긴 미리보기·썸네일 만들기의 잔재를 치운다 — 지운 바이트 수(D62).

        죽은 pid의 임시 파일만 지우고 게시본은 건드리지 않는다. 끊긴 'preparing' 기록은 닫아
        「미리보기 다시 준비」가 보이게 한다. 서버 시작이 작업 복구 뒤에 부른다.
        """
        active = self.active_jobs(root, (VISUAL_PREPARE_JOB_KIND,))
        return visual_review.sweep_preview_leftovers(self._workspace(root), active)

    def source_media_names(self, root: Path) -> dict[str, tuple[str, float | None]]:
        """경로 → (원본 이름, 길이 초) — 「지금 도는 작업」이 ffmpeg 입력을 원본으로 읽는다(D61).

        읽기만 한다. 같은 경로를 두 원본이 가리키면 먼저 온 원본(배치·순번 순)이 이긴다.
        """
        from vlogsrt.project.raw.media_refs import known_media_paths

        workspace = self._workspace(root)
        names: dict[str, tuple[str, float | None]] = {}
        for asset in workspace.list_source_assets():
            for path in known_media_paths(workspace, asset):
                names.setdefault(str(path), (asset.source_name, asset.duration_seconds))
        return names

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
        """저장된 선택 분석 상태들 — 화면이 받아 고치므로 범위 안에서도 부를 때마다 새 사본이다."""
        import copy

        from vlogsrt.project.raw.selection_analysis import load_states

        key = ("load_selection_states", str(self._workspace(root).root))
        return copy.deepcopy(read_scope.memo(key, lambda: load_states(self, root)))

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

    def analysis_tier_stamp(
        self, root: Path, revision_id: str | None, asset_ids: tuple[str, ...]
    ) -> tuple[tuple[str, tuple[int, int] | None, tuple[int, int] | None], ...]:
        """원본마다 (아이디, 진행 문서, 완성 문서)의 (mtime_ns, 크기) — 등급 구간의 재료(D63).

        영상 분석이 도는 동안 창 판정은 `suitability_progress.json`에 쌓이고, 작업대는 그 문서로
        등급 구간을 그린다(`_tier_report_for_row`). 화면 타이머는 이 도장이 바뀔 때만 다시 그린다 —
        문서를 열지 않고 stat만 한다. 없는 문서는 None.
        """
        if not revision_id:
            return ()
        workspace = self._workspace(root)
        stamps = []
        for row in AnalysisStore(workspace).list_source_results(revision_id):
            if row.asset_id not in asset_ids:
                continue
            directory, document = _tier_paths(workspace.root, row)
            progress = directory / "suitability_progress.json"
            stamps.append((row.asset_id, _file_stamp(progress), _file_stamp(document)))
        return tuple(stamps)

    def source_repeat_candidates(self, root: Path, asset_id: str) -> list[dict]:
        from vlogsrt.project.raw.source_transcript import cross_source_candidates

        return cross_source_candidates(self._workspace(root), asset_id)

    def source_media_references(self, root: Path, asset_id: str) -> dict | None:
        from vlogsrt.project.raw.media_refs import load

        return load(self._workspace(root), asset_id)

    def edit_source_transcript(
        self, root: Path, asset_id: str, revision: str, **fields: Any
    ) -> dict:
        from vlogsrt.project.raw import asr_epoch
        from vlogsrt.project.raw.source_transcript import edit

        self._workspace(root).source_asset(asset_id)
        edited = edit(self._workspace(root), asset_id, revision, **fields)
        # 고친 자막은 다시 들은 전사가 덮지 않는다 — 기다리던 다시 듣기는 돈만 드니 거둔다(D59 19b).
        if asr_epoch.withdraw(self._workspace(root), asset_id):
            log.out(f"원본 자막을 고쳐 기다리던 다시 듣기를 거뒀습니다: {asset_id}")
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
        """The one-line status text for a revision's usage; empty before any paid attempt.

        1초 상태 틱은 이 줄을 두 번 쓴다(상태 줄과 기록 판의 실사용 칸) — 읽기 범위 안에서는 한
        번만 센다(W-24 세션 G). 범위 밖에서는 매번 센다.
        """
        from vlogsrt.project.raw import request_usage

        key = ("analysis_usage_line", str(root.expanduser().resolve()), revision_id)
        return read_scope.memo(
            key,
            lambda: request_usage.format_usage_line(self.analysis_usage_summary(root, revision_id)),
        )

    def selection_quote(
        self, root: Path, asset_ids: tuple[str, ...], mode: str = SELECTION_MODE_FULL
    ) -> Any:
        from vlogsrt.project.raw.selection_analysis import quote

        return quote(self, root, asset_ids, mode)

    def selection_rebound_quote(
        self, root: Path, measured: Any, asset_ids: tuple[str, ...], mode: str | None = None
    ) -> Any:
        from vlogsrt.project.raw.selection_analysis import rebound_quote

        return rebound_quote(self, root, measured, asset_ids, mode)

    def approval_mode(self, root: Path) -> str:
        """작업대의 승인 범위 — 고른 적이 없거나 모르는 값이면 전체(지금까지의 흐름)다."""
        with self._workspace(root).connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key=?", (APPROVAL_MODE_META_KEY,)
            ).fetchone()
        mode = str(row[0]) if row else SELECTION_MODE_FULL
        return mode if mode in SELECTION_MODES else SELECTION_MODE_FULL

    def set_approval_mode(self, root: Path, mode: str) -> str:
        if mode not in SELECTION_MODES:
            raise RawWorkspaceError(f"알 수 없는 승인 범위입니다: {mode!r}")
        with self._workspace(root).connect() as connection:
            connection.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (APPROVAL_MODE_META_KEY, mode),
            )
            connection.commit()
        return mode

    def cross_check_enabled(self, root: Path) -> bool:
        """「다른 귀로 교차 확인」(D59 19d) — 고른 적이 없으면 켜짐(기본)이다. 읽기만 한다."""
        from vlogsrt.project.raw import cross_check

        return cross_check.enabled(self._workspace(root))

    def workspace_mode(self, root: Path) -> Any:
        """작업공간 모드(D97) — 사람이 고른 값(표지)이 없으면 불러온 영상 개수 다수다(읽기만)."""
        from vlogsrt.project.raw import workspace_mode

        stored = self._workspace(root).meta_value(WORKSPACE_MODE_KEY)
        choice = stored if stored in MODE_CHOICES else MODE_AUTO
        sizes = [(asset.width, asset.height) for asset in self.list_source_assets(root)]
        return workspace_mode.decide_mode(sizes, choice)

    def set_workspace_mode(self, root: Path, choice: str) -> Any:
        """모드를 고른다(무료 — 표지 하나, 지문·승인·견적 밖). `auto`는 다시 개수 다수를 따른다."""
        if choice not in MODE_CHOICES:
            raise RawWorkspaceError(
                f"모드는 {', '.join(MODE_CHOICES)} 가운데 하나입니다: {choice!r}"
            )
        self._workspace(root).set_meta_value(WORKSPACE_MODE_KEY, choice)
        return self.workspace_mode(root)

    def translation_model(self) -> str:
        """한국어 SRT를 영어로 옮기는 모델(D114) — 변환이 그 클라이언트를 받는다."""
        from vlogsrt.project.raw.config import TRANSLATION

        return TRANSLATION.model

    def default_korean_srt(self, root: Path) -> Path | None:
        """칸을 비웠을 때의 한국어 SRT — 마지막 초안에 저장한 최종 편집이 있으면 편집본 SRT
        (`edit/final.srt`, D118), 없으면 그 초안의 가장 새 SRT. 초안이 없으면 None. 읽기만 한다."""
        from vlogsrt.project.raw import final_edit_io, render_export

        folders = render_export.report_folders(self._workspace(root).root)
        if not folders:
            return None
        return final_edit_io.saved_edit_srt(folders[0]) or render_export.newest_srt(folders[0])

    def srt_translation_cached(self, root: Path, srt: Path) -> bool:
        """그 SRT의 글을 이미 옮겼는가(무료, 부르지 않는다) — 화면이 클라이언트를 받을지 정한다."""
        from vlogsrt.project.raw import srt_english

        job = srt_english.prepare(self, root, srt)
        return srt_english.cached(self, root, job) is not None

    def start_srt_translation(
        self,
        root: Path,
        srt: Path,
        client: object | None = None,
        done: Callable[[dict[str, Any]], None] | None = None,
    ) -> JobHandle:
        """한국어 SRT 하나를 영어 SRT로(W-63, D114) — 입력 옆에 `<이름>.en.srt`를 쓰는 작업.

        누름이 승인이다(D110 — 견적 없이). 읽기·열쇠는 작업을 걸기 **전에** 한다 — 깨진 SRT는 작업이
        되지 않는다. 같은 글을 이미 옮겼으면 저장된 번역을 쓰고(무료), 아니면 온라인과 클라이언트를
        작업 전에 본다. `done`은 결과 사실(쓴 자리 · 센 수)을 받는다.
        """
        from vlogsrt.project.raw import srt_english

        job = srt_english.prepare(self, root, srt)
        if srt_english.cached(self, root, job) is None:
            srt_english.require_online(self, root)
            if client is None:
                raise RawWorkspaceError("영어 자막을 옮길 Gemini 연결이 없습니다")

        def body(context: JobContext) -> None:
            facts = srt_english.convert(self, root, job, client)
            if done is not None:
                done(facts)

        fingerprint = f"{SRT_TRANSLATE_JOB_KIND}:{srt}"
        return self.submit_job(root, SRT_TRANSLATE_JOB_KIND, fingerprint, body)

    # ── 자막 입히기(W-65, D116) — 화면은 이 얇은 문으로만 `burn_export`에 닿는다 ──

    def subtitle_burn_settings(self) -> Any:
        """번인 설정(`BurnSettings`) — 화면 손잡이의 범위·기본값."""
        from vlogsrt.project.raw.burn_config import BURN_SETTINGS

        return BURN_SETTINGS

    def subtitle_burn_style(self, root: Path | None = None, values: Any = None) -> Any:
        """모양(`BurnStyle`) — `values`가 있으면 그것을 검사해서, 없으면 그 작업공간에 저장한 모양
        (작업공간이 없으면 기본 모양). 범위 밖 값은 `RawWorkspaceError`다."""
        from vlogsrt.project.raw import burn_export, burn_in

        if values is not None:
            try:
                return burn_in.style_from_mapping(values)
            except burn_in.BurnInError as error:
                raise RawWorkspaceError(str(error)) from error
        if root is None:
            return burn_in.default_style()
        return burn_export.saved_style(self, root)

    def save_subtitle_burn_style(self, root: Path, values: Any) -> Any:
        """손잡이를 바꾸면 바로 저장한다(W-98, D173) — 공유의 번인이 화면에 보인 모양을 쓰게.
        검사해서 저장하고 저장한 모양을 돌려준다(범위 밖이면 `RawWorkspaceError` — 저장 안 함)."""
        from vlogsrt.project.raw import burn_export

        style = self.subtitle_burn_style(values=values)
        burn_export.save_style(self, root, style)
        return style

    def prepare_subtitle_burn(
        self, root: Path, video: Path | None, korean: Path | None, english: Path | None
    ) -> Any:
        """빈 칸(None)은 마지막 초안에서 채워 읽고 짝짓고 조사한 `BurnJob`(무료, 작업 전) —
        한국어 SRT가 최종 편집의 편집본 SRT면(다 비우면 마지막 초안에 저장한 편집) 그 컷과 함께다
        (D118)."""
        from vlogsrt.project.raw import burn_export

        return burn_export.plan(self, root, video, korean, english)

    def subtitle_burn_preview(self, job: Any, style: Any) -> bytes:
        """첫 한글 큐 프레임 하나에 자막을 얹은 JPEG(무료 로컬)."""
        from vlogsrt.project.raw import burn_export

        return burn_export.preview(job, style)

    def start_subtitle_burn(
        self,
        root: Path,
        job: Any,
        style: Any,
        done: Callable[[dict[str, Any]], None] | None = None,
    ) -> JobHandle:
        """번인 작업을 건다(무료 로컬, D116) — libass ffmpeg가 없으면 작업 전에 거부한다."""
        from vlogsrt.project.raw import burn_export

        return burn_export.start(self, root, job, style, done)

    def finalcut_drop_disabled(self, root: Path) -> bool:
        """Final Cut 보내기에서 꺼진 클립을 뺄까(D94) — 고른 적이 없으면 빼지 않는다(기본)."""
        value = self._workspace(root).meta_value(FINALCUT_DROP_DISABLED_KEY)
        return value == FINALCUT_DROP_DISABLED_ON

    def set_finalcut_drop_disabled(self, root: Path, on: bool) -> bool:
        """그 표지를 저장한다(무료, 문서 모양만 정한다 — 지문·승인·견적 밖)."""
        value = FINALCUT_DROP_DISABLED_ON if on else FINALCUT_DROP_DISABLED_OFF
        self._workspace(root).set_meta_value(FINALCUT_DROP_DISABLED_KEY, value)
        return self.finalcut_drop_disabled(root)

    # ── 작업공간별 추천 기준(W-42, D100 §3) ─────────────────────────

    def _tier_settings(self, workspace: RawWorkspace) -> RawTierSettings:
        """이 작업공간의 등급 기준 — 표지 `tier_settings`의 덮기를 기본값 위에(읽기만 한다).

        없거나 비었으면 기본 기준 그 자체다. 저장된 덮기를 못 읽으면(손으로 고친 DB · 지금 코드가
        모르는 칸) 기본 기준으로 매기고 경고한다 — 등급 그리기를 막지 않고, 서랍이 까닭을 보인다
        (`tier_settings_view`의 `stored_error`). 그리기 한 번에 한 번만 읽는다(읽기 범위)."""
        return read_scope.memo(
            ("tier_settings", str(workspace.root)), lambda: self._stored_tier_settings(workspace)[0]
        )

    def _stored_tier_settings(self, workspace: RawWorkspace) -> tuple[RawTierSettings, str | None]:
        """(기준, 저장된 덮기를 못 읽은 까닭 또는 None)."""
        stored = workspace.scoped_meta_value(TIER_SETTINGS_KEY)
        if not stored:
            return TIER_SETTINGS, None
        try:
            overrides = json.loads(stored)
            return tier_settings_from_overrides(overrides), None
        except ValueError as error:
            log.warn(f"저장된 추천 기준을 읽지 못해 기본 기준으로 매긴다 — {error}")
            return TIER_SETTINGS, str(error)

    def tier_key(self, root: Path) -> str:
        """이 작업공간 등급의 낡음 열쇠 — 기본 기준이면 등급판 그대로(D100 §3)."""
        return tiering.tier_key(self._tier_settings(self._workspace(root)))

    def tier_settings_view(self, root: Path) -> dict[str, Any]:
        """추천 기준 서랍의 재료(무료, 읽기만) — 지금 값 · 기본값 · 덮은 칸 · 보일 칸 · digest."""
        settings, stored_error = self._stored_tier_settings(self._workspace(root))
        return {
            "values": tier_settings_values(settings),
            "defaults": tier_settings_values(TIER_SETTINGS),
            "overrides": tier_settings_overrides(settings),
            "editable_keys": list(EDITABLE_TIER_KEYS),
            "choices": tier_criteria_choices(),
            "digest": tiering.criteria_digest(settings),
            "tier_key": tiering.tier_key(settings),
            "stored_error": stored_error,
        }

    def set_tier_settings(self, root: Path, overrides: Mapping[str, object]) -> dict[str, Any]:
        """추천 기준을 덮는다(무료 — 표지 하나, 지문·승인·견적 밖). 빈 덮기는 기본으로 돌린다.

        검사를 지난 덮기만 기본값과 다른 칸으로 줄여 쓴다(거부는 쓰지 않는다). 쓴 뒤 정리 고리의
        등급 단계(`_tier_step` — 맥락 평가·자막 시각 확인 뒤와 같은 길)가 검토 머리의 등급 표시를
        다시 단다 — 사람이 정한 사용/제외·메모·덮기·경계는 그대로다."""
        try:
            settings = tier_settings_from_overrides(overrides)
        except ValueError as error:
            raise RawWorkspaceError(str(error)) from error
        canonical = json.dumps(
            tier_settings_overrides(settings), ensure_ascii=False, sort_keys=True
        )
        self._workspace(root).set_meta_value(TIER_SETTINGS_KEY, canonical)
        _forget_read_scope()
        self._tier_step(root)
        _forget_read_scope()
        return self.tier_settings_view(root)

    def glossary(self, root: Path) -> Any:
        """프로젝트 용어집(D59 19c) — 마지막 리비전. 읽기만 한다(표를 만들지 않는다)."""
        from vlogsrt.project.raw import glossary

        return glossary.current(self._workspace(root))

    def save_glossary(self, root: Path, text: str) -> Any:
        """용어집을 새 리비전으로 저장한다(무료) — 1단계 글자 경로의 keywords로만 간다.

        whisper·설정에는 들어가지 않는다. 두 번째 귀의 요청 신원(견적·결속의 교차 확인 꼬리표,
        D59 19d)에 sha로 실리므로 저장한 뒤에는 견적을 다시 묶어야 한다(화면 19e가 한다). 쓸 수 없는
        글자가 있으면 저장하지 않는다.
        """
        from vlogsrt.project.raw import glossary

        return glossary.save(self._workspace(root), text)

    def asr_epoch_state(self, root: Path, asset_id: str) -> Any:
        """원본의 다시 듣기 차례(D59 19b) — 읽기만 한다(표를 만들지 않는다)."""
        from vlogsrt.project.raw import asr_epoch

        return asr_epoch.state(self._workspace(root), asset_id)

    def relisten_source(self, root: Path, asset_id: str) -> Any:
        """「다시 듣기」 — 원본의 차례를 올린다(무료). 유료 전사는 승인한 실행만 한다(D44).

        사람이 고친 원본 자막은 덮지 않는다: 고친 자막이 있으면 거절한다(seed는 고친 자막을
        건드리지 않으므로 다시 들어도 돈만 든다 — 판정은 지문과 같은 규칙 `human_edited`). 전사한
        적이 없는 원본도 거절한다 — 첫 분석이 이미 고친 온도로 듣는다. 소리가 없는 원본도 거절한다
        — 들을 것이 없어 차례가 끝나지 않고, 끝난 영상 분석만 낡게 만든다(ffprobe 한 번). 이미
        기다리는 중이면 차례를 또 올리지 않는다.
        """
        from vlogsrt.project.raw import asr_epoch, source_transcript

        workspace = self._workspace(root)
        asset = workspace.source_asset(asset_id)
        document = source_transcript.load(workspace, asset_id)
        if document is None:
            raise RawWorkspaceError(RELISTEN_UNTRANSCRIBED_REFUSAL)
        refusal = _relisten_refusal(document)
        if refusal is not None:
            raise RawWorkspaceError(refusal)
        # 도는 빈칸 재전사가 넣으면 고친 자막이 되어 다시 들은 전사가 덮지 못한다(D59 19f).
        if self.refill_running(root, asset_id):
            raise RawWorkspaceError(RELISTEN_REFILL_RUNNING_REFUSAL)
        if not _source_has_audio(workspace.source_media_path(asset)):
            raise RawWorkspaceError(RELISTEN_SILENT_REFUSAL)
        # 위 검사와 차례 올리기 사이(ffprobe 동안)에 적용·편집·빈칸 재전사가 끼면 고친 자막에 유료
        # 다시 듣기가 남는다 — 같은 쓰기 트랜잭션 안에서 다시 본다(D59 19h).
        return asr_epoch.bump(
            workspace, asset_id, guard=partial(_relisten_check, asset_id=asset_id)
        )

    # ── 말소리 빈칸 재전사(D59 19f) — 고르기는 무료, 묻기는 승인한 범위만 ──────────

    def refill_scope(self, root: Path, asset_id: str) -> Any:
        """빈칸 재전사의 지금 범위(무료) — 고를 수 없으면 그 이유(존댓말 한 줄)를 돌려준다.

        재료는 자막 시각 확인(D55)이 지금 자막 리비전을 잰 보고서·말소리 칸이다(읽기만 — VAD·디코드
        없음). 구간 고르기는 순수 `speech_gaps`, 범위·digest는 `speech_refill`이 짓는다(견적·금액은
        없다, D110).
        """
        from vlogsrt.project.raw import speech_refill

        plan = self._refill_plan(root, asset_id)
        return plan if isinstance(plan, str) else speech_refill.scope_for(plan)

    def refill_running(self, root: Path, asset_id: str) -> bool:
        """그 원본의 빈칸 재전사 작업이 도는 중인가 — 작업 지문은 `<원본>:<digest>`다."""
        return any(
            job.input_fingerprint.startswith(f"{asset_id}:")
            for job in self.active_jobs(root, (REFILL_JOB_KIND,))
        )

    def refill_last(self, root: Path, asset_id: str) -> dict | None:
        """가장 최근 빈칸 재전사 기록 — 화면의 「지난 빈칸 재전사」 한 줄. 읽기만 한다."""
        from vlogsrt.project.raw import speech_refill

        return speech_refill.latest_record(self._workspace(root).root, asset_id)

    def refill_skipped(self, root: Path, asset_id: str) -> list[dict[str, object]]:
        """넣지 않은 줄(겹침·범위 밖) 전부 — 화면이 글자째 보인다(규칙 9). 읽기만 한다."""
        from vlogsrt.project.raw import speech_refill

        return speech_refill.skipped_rows(self._workspace(root).root, asset_id)

    def refill_start(
        self,
        root: Path,
        asset_id: str,
        approved: Any,
        transport: Any,
        after: Callable[[], None] | None = None,
    ) -> JobHandle:
        """승인한 빈칸 재전사를 작업으로 건다 — 본문(`refill_source`)이 범위를 다시 재어 맞춘다.

        `after`는 본문이 끝난 뒤의 화면 고리(저장 결과 셈에서 빼기)다 — 실패한 본문 뒤에는
        돌지 않는다.
        """

        def body(context: JobContext) -> None:
            self.refill_source(root, asset_id, approved, transport, context)
            if after is not None:
                after()

        fingerprint = f"{asset_id}:{approved.digest}"
        return self.submit_job(root, REFILL_JOB_KIND, fingerprint, body)

    def refill_source(
        self, root: Path, asset_id: str, approved: Any, transport: Any, context: JobContext
    ) -> dict[str, Any]:
        """승인한 빈칸 재전사(작업 본문, 유료) — 범위가 승인한 것과 같을 때만 묻고 받은 줄을 넣는다.

        넣기(`source_transcript.insert_segments`)는 이 본문의 `_refill_settle`만 부른다(불변식).
        기존 줄은 바이트까지 그대로이고 겹치는 줄은 넣지 않고 WARN·기록한다. 넣으면 원본 자막은
        고친 자막(`edited`)이 된다. 실행은 다시 잰 범위만 쓴다 — 승인 객체는 digest만 대조한다.
        """
        from vlogsrt.project.raw import browser_runtime, cross_check, request_log, speech_refill

        workspace = self._workspace(root)
        identity = cross_check.request_identity(workspace)  # 다시 재기 전에 읽는다(19d와 같은 선)
        plan = self._refill_plan(root, asset_id)
        if isinstance(plan, str):
            raise RawWorkspaceError(plan)
        current = speech_refill.scope_for(plan)
        if current.digest != approved.digest:
            raise RawWorkspaceError(REFILL_CHANGED)
        if current.text_identity not in (None, identity):
            raise RawWorkspaceError(REFILL_GLOSSARY_CHANGED)
        audio = self._refill_audio(workspace, asset_id, plan)
        browser_runtime.prepare_approved_browser_network()
        self.update_settings(root, plan.workflow)  # 승인한 실행만 online — 일괄 승인과 같은 선
        context.check_cancel()
        with request_log.scope(workspace, context.job_id, live=True):
            result = speech_refill.run(
                plan, current, audio, transport, on=transport.on or datetime.now(UTC).date()
            )
        return self._refill_settle(root, plan, current, result)

    def _refill_settle(self, root: Path, plan: Any, scope: Any, result: Any) -> dict[str, Any]:
        """받은 줄을 넣고 기록한다(넣기는 여기 하나다, 불변식).

        넣지 못하면(그사이 자막이 바뀌었다) 기록·WARN을 남기고 거절을 올린다 — 받은 답은 캐시에
        남는다.
        """
        from vlogsrt.project.raw import asr_epoch, source_transcript, speech_refill

        workspace = self._workspace(root)
        if asr_epoch.state(workspace, plan.asset_id).pending:  # 다시 들은 전사가 덮을 수 있게 둔다
            speech_refill.write_record(
                plan, scope, _refill_record(result, None, REFILL_RELISTEN_SETTLE_REFUSAL)
            )
            log.warn(f"빈칸 재전사: {REFILL_RELISTEN_SETTLE_REFUSAL}")
            raise RawWorkspaceError(REFILL_RELISTEN_SETTLE_REFUSAL)
        speech_refill.claim(plan, scope, result.answered)  # 넣기 전에 — 멈춰도 다시 사지 않게
        try:
            outcome = source_transcript.insert_segments(
                workspace, plan.asset_id, scope.transcript_revision, result.rows
            )
        except RawWorkspaceError as error:
            speech_refill.write_record(plan, scope, _refill_record(result, None, str(error)))
            log.warn(f"빈칸 재전사: 받은 줄을 넣지 못했습니다 — {error} (받은 답은 캐시에 남았다)")
            raise
        speech_refill.mark_asked(plan, result.answered)
        record = _refill_record(result, outcome, None)
        speech_refill.write_record(plan, scope, record)
        _warn_refill(plan.asset_id, outcome, result)
        if outcome.inserted:  # 자막이 바뀌었다 — 시각 확인을 다시(D55)
            self._queue_sync_checks(root, (plan.asset_id,))
        return record

    def _refill_plan(self, root: Path, asset_id: str) -> Any:
        """범위 재료(값) — 다시 듣기를 기다리거나 확인 보고서가 없으면 그 이유(존댓말)."""
        from vlogsrt.project.raw import asr_epoch, sync_report
        from vlogsrt.project.raw.source_transcript import REFILL_FIELD

        workspace = self._workspace(root)
        asset = workspace.source_asset(asset_id)
        transcript = self.source_transcript(root, asset_id)
        if transcript is None:
            return REFILL_UNTRANSCRIBED
        if asr_epoch.state(workspace, asset_id).pending:
            return REFILL_RELISTEN_PENDING
        if timing_engine.resolve(workspace, asset_id) not in timing_engine.NEW_HEARING_ENGINES:
            return REFILL_WHISPER_RETIRED
        measured = sync_report.measured_speech(workspace, asset_id)
        if measured is None:
            report = sync_report.current_report(workspace, asset_id)
            unmeasured = report is not None and not report.get("stale") and not report.get("pcm")
            return REFILL_UNMEASURED if unmeasured else REFILL_WAITING
        refill_ids = frozenset(
            str(row[REFILL_FIELD]) for row in transcript["utterances"] if row.get(REFILL_FIELD)
        )
        return self._refill_plan_of(root, workspace, asset, measured, refill_ids)

    def _refill_plan_of(
        self,
        root: Path,
        workspace: RawWorkspace,
        asset: SourceAsset,
        measured: Any,
        refill_ids: frozenset[str],
    ) -> Any:
        from vlogsrt.project.config import GAP_REFILL_SETTINGS, SYNC_CHECK_SETTINGS
        from vlogsrt.project.raw import cross_check, glossary, speech_gaps, speech_refill, text_ear

        rate = analysis_workflow.stage1.PCM_SAMPLE_RATE
        spans = speech_gaps.select_gaps(
            measured.cues,
            measured.speech,
            sample_count=measured.sample_count,
            sample_rate=rate,
            failed=measured.failed,
            sync_settings=SYNC_CHECK_SETTINGS,
            settings=GAP_REFILL_SETTINGS,
        )
        return speech_refill.RefillPlan(
            root=workspace.root,
            asset_id=asset.asset_id,
            transcript_revision=str(measured.report["source"]["transcript_revision"]),
            source_sha256=asset.content_sha256 or "",
            pcm_sha256=measured.pcm_sha256,
            sample_count=measured.sample_count,
            audio_start=measured.audio_start,
            spans=spans,
            workflow=replace(self.source_settings(root, asset.asset_id), network_mode="online"),
            # Gemini 시간 엔진 원본에는 다른 귀가 없다(D104) — 빈칸 재전사도 gpt를 묻지 않는다.
            cross_check=cross_check.enabled(workspace)
            and text_ear.resolve(workspace, asset.asset_id) != text_ear.EAR_NONE,
            glossary=glossary.current(workspace).terms,
            chunk_bound=_refill_bound(int(measured.report["pcm"]["channels"])),
            refill_ids=refill_ids,
            failures_known=measured.failures_known,
        )

    @staticmethod
    def _refill_audio(workspace: RawWorkspace, asset_id: str, plan: Any) -> Any:
        """확인이 말소리를 잰 그 PCM — 해시가 같은 파일이 없으면 0송신으로 닫는다."""
        from vlogsrt.project.raw import speech_refill, sync_report

        asset = workspace.source_asset(asset_id)
        choice = sync_report.measured_pcm(workspace, asset, plan.pcm_sha256)
        if choice is None:
            raise RawWorkspaceError(REFILL_PCM_MISSING)
        return speech_refill.RefillAudio(
            source_id=asset_id,
            source_sha256=asset.content_sha256 or "",
            stream_index=choice.stream_index,
            pcm_path=choice.path,
            pcm_sha256=choice.pcm_sha256,
            channels=choice.channels,
            sample_count=choice.sample_count,
            origin=choice.origin,
            audio_start=plan.audio_start,
        )

    # ── 시각 맞춤 적용·되돌리기(D59 19h) — 제안은 무료로 다시 재고, 적용은 사람이 누를 때만 ──────
    # 제안 계산(`sync_snap`)과 원본 자막 쓰기(`apply_changes`·`restore`)는 이 아래의 `apply_snap*`·
    # `revert_snap*`만 부른다(불변식). 화면은 이 메서드들로만 닿는다.

    def apply_snap_preview(self, root: Path, asset_id: str) -> Any:
        """「시각 맞춤 제안」 칸의 값(무료) — 지금 제안의 sha·센 어긋남·판정 수·예측·먼저 들을 줄.

        제안을 만들 수 없으면 그 이유(존댓말 한 줄)를 돌려준다. 재료는 자막 시각 확인(D55)이 지금
        자막 리비전을 잰 보고서·저장된 말소리 칸이다(읽기만 — 말소리 측정·디코드 없음). 아무것도
        쓰지 않는다.
        """
        found = self._apply_snap_plan(root, asset_id, strict=False)
        if isinstance(found, str):
            return found
        plan, measured = found
        return _apply_snap_preview(plan, measured)

    def apply_snap_blocker(self, root: Path, asset_id: str) -> str | None:
        """지금 적용을 받지 않는 까닭 — 다시 듣기 대기 · 빈칸 재전사 진행 · 그 원본을 분석하는 중.

        적용하면 고친 자막이 된다: 다시 들은 전사가 덮지 못해 다시 듣기에 낸 돈이 헛되고, 도는 빈칸
        재전사는 넣을 리비전을 잃고(D59 19f와 같은 선), 분석은 심는 중인 자막과 어긋난다. 적용은 이
        검사를 쓰기 트랜잭션 안에서 한 번 더 한다(`_apply_snap_check`).
        """
        with self._workspace(root).connect() as db:
            return _apply_snap_hold(db, asset_id, relisten=True)

    def apply_snap(self, root: Path, asset_id: str, sha256: str) -> dict:
        """「시각 맞춤 적용(무료)」 — 지금 다시 잰 제안의 sha가 화면이 본 sha와 같을 때만 쓴다.

        **모든** 제안의 새 시각을 판정과 무관하게 리비전 하나로 쓴다(D59 19g 「적용 규칙」 — 막힘·못
        찾음 줄의 밀기 포함). 제안은 지금 리비전·그 리비전의 보고서(1단계 PCM·기준까지 대조)·저장된
        말소리 칸으로 다시 잰다 — sha가 다르면(다른 탭·자막 편집·다시 잰 측정·기준 변경) 아무것도
        쓰지 않고 거절한다. 원본 자막은 고친 자막(`edited`)이 되고 시각 확인을 다시 건다.
        **한 걸음**: 이미 적용한 리비전은 다시 맞추지 않는다(되돌린 뒤에만). 막는 사정은 쓰기
        트랜잭션 안에서 다시 본다.
        """
        from vlogsrt.project.raw import source_transcript

        workspace = self._workspace(root)
        workspace.source_asset(asset_id)
        blocker = self.apply_snap_blocker(root, asset_id)
        if blocker is not None:
            raise RawWorkspaceError(f"{blocker} {SNAP_NOTHING_CHANGED}")
        found = self._apply_snap_plan(root, asset_id, strict=True)
        if isinstance(found, str):
            raise RawWorkspaceError(f"{found} {SNAP_NOTHING_CHANGED}")
        plan, _measured = found
        if plan.sha256 != sha256:
            raise RawWorkspaceError(SNAP_CHANGED)
        times = [
            (item.cue_id, plan.on_common(item.new_start), plan.on_common(item.new_end))
            for item in plan.proposals
        ]
        document = source_transcript.apply_changes(
            workspace,
            asset_id,
            plan.source.transcript_revision,
            times,
            plan_sha256=plan.sha256,
            settings_version=plan.settings.version,
            guard=partial(_apply_snap_check, asset_id=asset_id, relisten=True),
        )
        log.out(f"시각 맞춤을 적용했습니다: {asset_id} — 바뀐 줄 {document['snap']['changed']}개")
        self._queue_sync_checks(root, (asset_id,))  # 자막이 바뀌었다 — 시각 확인을 다시(D55)
        return document

    def _apply_snap_plan(self, root: Path, asset_id: str, *, strict: bool) -> Any:
        """지금 자막 리비전·그 보고서·저장된 말소리 칸으로 잰 (제안, 재료) — 없으면 그
        이유(존댓말)."""
        from vlogsrt.project import sync_snap
        from vlogsrt.project.config import SYNC_SNAP_SETTINGS, ProjectConfig
        from vlogsrt.project.raw import source_transcript, sync_report

        workspace = self._workspace(root)
        transcript = source_transcript.load(workspace, asset_id)
        if transcript is None:
            return SNAP_UNTRANSCRIBED
        if source_transcript.snap_applied(transcript) is not None:
            return SNAP_APPLIED_ONCE  # 한 걸음 규칙 — 되돌린 뒤에만 다시 잰다
        measured = sync_report.measured_speech(workspace, asset_id, strict=strict)
        revision = str(transcript["revision"])
        if measured is None or measured.report["source"]["transcript_revision"] != revision:
            return _apply_snap_unready(sync_report.current_report(workspace, asset_id))
        source = sync_snap.SnapSource(
            source_id=asset_id,
            transcript_revision=revision,
            pcm_sha256=measured.pcm_sha256,
            vad_key=sync_report.vad_key(ProjectConfig()),
            audio_start=measured.audio_start,
            transcript_duration=source_transcript.rational(transcript["duration"]),
        )
        offset = _apply_snap_offset(measured.report)
        plan = sync_snap.propose(
            measured.cues, measured.speech, offset, source=source, settings=SYNC_SNAP_SETTINGS
        )
        return plan, measured

    def apply_snap_all_preview(self, root: Path) -> SnapAllPreview:
        """「모든 영상 시각 맞춤 확인(무료)」(D75) — 원본 자막이 있는 영상마다 적용할 수 있는지.

        영상마다 `apply_snap_preview`·`apply_snap_blocker`(19h) 그대로 잰다 — 적용할 수 있으면 그
        제안의 sha와 바뀔 줄 수, 아니면 까닭이다. 묶음 토큰은 적용할 영상과 sha를 묶는다. 아무것도
        쓰지 않는다. 아직 전사하지 않은 영상은 싣지 않는다.
        """
        rows: list[SnapAllRow] = []
        for asset in self._workspace(root).list_source_assets():
            preview = self.apply_snap_preview(root, asset.asset_id)
            if preview == SNAP_UNTRANSCRIBED:
                continue
            rows.append(self._apply_snap_all_row(root, asset.asset_id, asset.source_name, preview))
        return SnapAllPreview(tuple(rows), _apply_snap_all_token(rows))

    def _apply_snap_all_row(self, root: Path, asset_id: str, name: str, preview: Any) -> SnapAllRow:
        if isinstance(preview, str):
            return SnapAllRow(asset_id, name, None, 0, preview)
        if not preview.changes:
            return SnapAllRow(asset_id, name, None, 0, SNAP_ALL_NOTHING)
        blocker = self.apply_snap_blocker(root, asset_id)
        if blocker is not None:
            return SnapAllRow(asset_id, name, None, preview.changes, blocker)
        return SnapAllRow(asset_id, name, preview.sha256, preview.changes, None)

    def apply_snap_all(
        self, root: Path, token: str, *, held: Mapping[str, str] | None = None
    ) -> SnapAllResult:
        """「모든 영상 시각 맞춤 적용(무료)」(W-25e, D75) — 확인 때의 토큰이 지금과 같을 때만 쓴다.

        다르면(그사이 자막·측정·다시 듣기·적용이 바뀌었다) 어느 영상도 쓰지 않고 거절한다. 같으면
        적용할 수 있는 영상마다 그 sha로 `apply_snap`을 부른다 — 막힘·한 걸음·sha 대조·쓰기 트랜잭션
        안의 다시 검사가 영상마다 그대로다. 한 영상의 거절은 그 영상만 건너뛰고(까닭을 남긴다) 다음
        영상으로 간다. `held`는 화면이 붙잡아 달라는 영상(분석이 도는 영상)과 그 까닭이다.
        """
        preview = self.apply_snap_all_preview(root)
        if preview.token != token:
            raise RawWorkspaceError(f"{SNAP_ALL_CHANGED} {SNAP_NOTHING_CHANGED}")
        applied: list[SnapAllApplied] = []
        skipped: list[SnapAllSkipped] = []
        for row in preview.ready:
            reason = (held or {}).get(row.asset_id)
            if reason is None:
                try:
                    document = self.apply_snap(root, row.asset_id, str(row.sha256))
                except RawWorkspaceError as refused:
                    reason = str(refused)
                else:
                    changed = int(document["snap"]["changed"])
                    applied.append(SnapAllApplied(row.asset_id, row.source_name, changed))
                    continue
            skipped.append(SnapAllSkipped(row.asset_id, row.source_name, reason))
        log.out(f"모든 영상 시각 맞춤 — 적용 {len(applied)}개 · 건너뜀 {len(skipped)}개")
        return SnapAllResult(tuple(applied), tuple(skipped))

    def revert_snap_state(self, root: Path, asset_id: str) -> dict | None:
        """지금 리비전이 시각 맞춤을 적용한 리비전이면 그 기록 — 「되돌리기」 단추의 재료.
        읽기만 한다."""
        from vlogsrt.project.raw import source_transcript

        document = source_transcript.load(self._workspace(root), asset_id)
        return None if document is None else source_transcript.snap_applied(document)

    def revert_snap(self, root: Path, asset_id: str, revision: str) -> dict:
        """「되돌리기」(무료) — 적용 전 리비전의 줄과 `edited`를 바이트 그대로 새 리비전으로 쌓는다.

        화면이 본 리비전이 지금 리비전이고 그것이 시각 맞춤을 적용한 리비전일 때만 된다 — 그 뒤에
        고치거나 넣은 자막은 지우지 않는다(거절). 빈칸 재전사가 돌거나 그 원본을 분석하는 동안은
        받지 않는다 — 쓰기 트랜잭션 안에서 다시 본다. 되돌린 뒤 시각 확인을 다시 건다.
        """
        from vlogsrt.project.raw import source_transcript

        workspace = self._workspace(root)
        workspace.source_asset(asset_id)
        with workspace.connect() as db:
            _apply_snap_check(db, asset_id=asset_id, relisten=False)
        guard = partial(_apply_snap_check, asset_id=asset_id, relisten=False)
        document = source_transcript.restore(workspace, asset_id, revision, guard=guard)
        log.out(f"시각 맞춤을 되돌렸습니다: {asset_id}")
        self._queue_sync_checks(root, (asset_id,))  # 자막이 바뀌었다 — 시각 확인을 다시(D55)
        return document

    # ── 시각 정답(W-25b, D67) — 사람이 들어 찍은 참 시작·끝. 자막과 따로 둔다 ───────────────────
    # 찍어도 원본 자막 리비전·줄·시각 확인·지문·캐시 열쇠·승인·견적·등급은 그대로다. 읽는 곳은
    # 화면의 표시와 `scripts/timing_truth_report.py`뿐이다.

    def timing_truth_view(
        self,
        root: Path,
        asset_id: str,
        transcript: Mapping[str, Any],
        sync_view: Mapping[str, Any] | None,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """줄마다의 화면 값(권하는 층 `truthStratum` · 찍은 정답 `truth`)과 줄이 사라진 정답 수.

        글자가 바뀐 줄의 정답은 그 줄에 `stale`로 싣는다(다시 찍기). 읽기만 한다(표를 만들지
        않는다).
        """
        from vlogsrt.project.raw import timing_truth

        workspace = self._workspace(root)
        lines = timing_truth.lines_of(transcript)
        strata = _truth_strata(workspace, asset_id, transcript, lines, sync_view)
        notes: dict[str, dict[str, Any]] = {
            cue_id: {"truthStratum": stratum}
            for cue_id, stratum in timing_truth.suggest(lines, strata).items()
        }
        missing = 0
        for state in timing_truth.judge(timing_truth.marks(workspace, asset_id), lines):
            if state.stale == timing_truth.STALE_LINE_MISSING:
                missing += 1
                continue
            value: dict[str, Any] = {"at": str(state.mark.seconds)}
            if state.stale is not None:
                value["stale"] = True
            notes.setdefault(state.mark.utterance_id, {}).setdefault("truth", {})[
                state.mark.edge
            ] = value
        return notes, missing

    def mark_timing_truth(
        self,
        root: Path,
        asset_id: str,
        *,
        utterance_id: str,
        edge: str,
        seconds: Fraction,
        transcript_revision: str,
    ) -> Any:
        """「참 시작」·「참 끝」(무료) — 화면이 본 리비전의 그 줄에 정답 하나를 찍는다.

        줄은 한 칸도 움직이지 않고 원본 자막 리비전도 쓰지 않는다. 화면이 본 뒤 자막이 바뀌었거나
        줄이 없거나 시각이 원본 길이 밖이면 거절한다(아무것도 쓰지 않는다). 그 줄의 지금 층을 함께
        적는다 — 보고서는 찍을 때의 층으로 나눈다(나중에 시각 확인이 바뀌어도 기준선이 흔들리지
        않게).
        """
        from vlogsrt.project.raw import source_transcript, timing_truth

        workspace = self._workspace(root)
        transcript = source_transcript.load(workspace, asset_id)
        lines = _truth_lines(transcript, transcript_revision, seconds)
        line = next((item for item in lines if item.cue_id == utterance_id), None)
        if transcript is None or line is None:
            raise RawWorkspaceError(TRUTH_LINE_MISSING)
        sync_view = self.sync_check_view(root, asset_id)
        strata = _truth_strata(workspace, asset_id, transcript, lines, sync_view)
        entry = timing_truth.mark(
            workspace,
            asset_id,
            line,
            edge,
            seconds,
            transcript_revision=transcript_revision,
            stratum=strata[line.cue_id],
        )
        log.out(
            f"시각 정답을 찍었습니다: {asset_id} {utterance_id} {edge} = {float(seconds):.3f}초"
        )
        return entry

    def clear_timing_truth(
        self, root: Path, asset_id: str, *, utterance_id: str, edge: str
    ) -> bool:
        """찍은 정답 하나를 지운다(무료) — 지웠으면 True. 자막은 그대로다."""
        from vlogsrt.project.raw import timing_truth

        return timing_truth.clear(self._workspace(root), asset_id, utterance_id, edge)

    @staticmethod
    def transcript_human_edited(document: Mapping[str, Any]) -> bool:
        """원본 자막 문서가 사람이 고친 것인가 — 지문·다시 듣기 거절과 같은 규칙(D59 19b)."""
        from vlogsrt.project.raw.source_transcript import human_edited

        return human_edited(dict(document))

    def analysis_revision_on_old_prompt(self, root: Path, revision_id: str) -> bool:
        """이전 분석 기준(안내문 리비전)으로 시작한 분석 리비전인가(D58 18b) — 없으면 False."""
        revision = AnalysisStore(self._workspace(root)).revision(revision_id)
        if revision is None:
            return False
        return not _current_prompt(json.loads(revision.settings_json).get("prompt_revision"))

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
        workspace = self._workspace(root)
        for row in summary.results:
            if row.asset_id not in asset_ids:
                continue
            try:
                report = self._tier_report(workspace, row)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if report is not None:
                result[row.asset_id] = [span.document() for span in report.spans]
        return result

    def tier_report(self, root: Path, revision_id: str, asset_id: str) -> Any:
        """원본 하나의 3단 등급 보고서 — 저장된 분석 문서만 읽는다(유료 호출 0회, D51·D52).

        완성 문서가 있으면 그 문서로, Luna가 도는 동안이면 진행 문서로(자막 칸이 아직 없어
        빨강 없이) 계산한다. 분석이 없으면 None이다. 아무것도 쓰지 않는다. 자막 시각 확인 값은
        `_tier_report_for_row`가 지금 리비전 보고서에서 한 번 읽어 넘긴다(D60).
        """
        workspace = self._workspace(root)
        row = AnalysisStore(workspace).source_result(revision_id, asset_id)
        return None if row is None else self._tier_report(workspace, row)

    def _tier_report(
        self, workspace: RawWorkspace, row: AnalysisSourceResult, *, with_context: bool = True
    ) -> tiering.TierReport | None:
        """`_tier_report_for_row`의 읽기 범위 기억 — 같은 범위에서 같은 행은 한 번만 계산한다(D123).

        단추 하나가 정리 고리(다시 단 등급)와 그리기에서 원본마다 두 번 계산했다. 범위는 쓰기가 끝난
        뒤에만 열리므로 재료가 그 사이 바뀌지 않는다. 범위 밖은 예전처럼 매번 계산한다.
        """
        key = (
            "tier_report",
            str(workspace.root),
            row.revision_id,
            row.asset_id,
            row.status,
            row.document_path,
            row.record_path,
            with_context,
        )
        return read_scope.memo(
            key, lambda: self._tier_report_for_row(workspace, row, with_context=with_context)
        )

    def _tier_report_for_row(
        self, workspace: RawWorkspace, row: AnalysisSourceResult, *, with_context: bool = True
    ) -> tiering.TierReport | None:
        """등급 보고서의 유일한 자리 — 화면·검토 머리·자동 제외 후보·Final Cut 키워드가 모두 이
        길로 같은 값(`_tier_timing`)을 받아 같은 등급을 본다(D60, `check_invariants`).

        읽을 때마다 계산하고 어디에도 보관하지 않는다(D51·D52). 그리기 한 번은 원본마다 한 번만
        부른다(`selection_suitability`를 리비전마다 한 번, D63) — 재료 읽기(분석 개요·시각 확인
        보고서·원본 자막)는 읽기 범위가 한 번으로 줄인다. 맥락 평가 값(`_tier_context`, D99)도 이
        자리에서만 넘긴다 — 맥락 평가의 입력을 지을 때만 `with_context=False`(평가 전 등급)다.
        부르는 쪽은 `_tier_report`(읽기 범위 기억, D123)를 거친다."""
        if row.status == "transcribed":
            # 1단계만 끝난 행(「전사만」 · 영상 분석 승인 전, D59 19b) — 판정이 없다. 행의 문서는
            # 1단계 기록(`asr_phase.json`)이라 등급 문서로 읽지 않는다.
            return None
        directory, document = _tier_paths(workspace.root, row)
        if document.is_file():
            body = file_memo.read_bytes(document)  # 바이트만 기억한다 — 등급은 매번 계산(D51·D167)
            return tiering.classify(
                json.loads(body),
                audio_start=_record_audio_start(workspace.root, row),
                document_sha256=hashlib.sha256(body).hexdigest(),
                timing=self._tier_timing(workspace, row.asset_id),
                context=self._tier_context(workspace, row.asset_id, applied=with_context),
                settings=self._tier_settings(workspace),
            )
        progress = directory / "suitability_progress.json"
        if not progress.is_file():
            return None
        payload = json.loads(progress.read_text(encoding="utf-8"))
        if payload.get("analysis_revision_id") != row.revision_id:
            return None
        return tiering.classify(
            payload,
            timing=self._tier_timing(workspace, row.asset_id),
            context=self._tier_context(workspace, row.asset_id, applied=with_context),
            settings=self._tier_settings(workspace),
        )

    def _tier_timing(self, workspace: RawWorkspace, asset_id: str) -> tiering.TimingSignals:
        """등급에 넘길 자막 시각 확인 값 — **지금** 보고서에서만(D60·D65). 이 값의 유일한 생산자다.

        지금이란 보고서의 자막 리비전·1단계 PCM·기준이 지금과 같다는 뜻이다(`is_current` —
        Final Cut의 `자막 시각 확인 필요` 키워드와 같은 규칙). 없거나 낡았거나 다른 리비전을
        쟀으면 모름(아무것도 내리지도 올리지도 않는다). 「말소리를 못 찾음」 큐는 판정이 난
        보고서(빨강·노랑·초록)에서만 넘긴다 — 측정 불가(말소리 3초 미만 등)는 측정 자체를 믿을 수
        없다. 「자막 확인됨」(D65)은 그 보고서가 초록이고 같은 리비전 원본 자막의 확인 필요 줄 몫이
        기준 이하일 때만 참이다(`_timing_read`). 보고서를 못 읽으면 모름으로 두고 경고한다(표시용
        측정이 등급 계산을 막지 못한다). 읽기만 한다.
        """
        try:
            read = _timing_read(workspace, asset_id)
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.warn(
                f"자막 시각 확인 보고서를 읽지 못해 등급에는 모름으로 둔다 — {asset_id}: {error}"
            )
            return tiering.UNKNOWN_TIMING
        if read is None:
            return tiering.UNKNOWN_TIMING
        settings = self._tier_settings(workspace)
        if not tiering.verified_subtitles(read.level, read.suspect_lines, read.lines, settings):
            return tiering.TimingSignals(level=read.level, no_speech=read.no_speech)
        return tiering.TimingSignals(
            level=read.level,
            no_speech=read.no_speech,
            subtitle_verified=True,
            subtitle_suspect_lines=read.suspect_lines,
            subtitle_lines=read.lines,
        )

    def _tier_context(
        self, workspace: RawWorkspace, asset_id: str, *, applied: bool = True
    ) -> tiering.ContextSignals:
        """등급에 넘길 맥락 평가 값 — **지금** 입력 열쇠의 평가에서만(D99). 이 값의 유일한 생산자다.

        입력(원본 · 분석 · 원본 자막 리비전 · 등급판 · 모델 · 예산)이 바뀌어 열쇠가 다르면 평가가
        낡았다 — 모름으로 두고 아무것도 내리지도 올리지도 않는다. 평가를 못 읽으면 모름으로 두고
        경고한다(등급 계산을 막지 않는다). `applied`가 거짓이면(맥락 평가의 입력을 지을 때)
        모름이다.
        """
        if not applied:
            return tiering.UNKNOWN_CONTEXT
        from vlogsrt.project.raw import context_review_io

        folder = context_review_io.folder(workspace.root, ANALYSIS_ROOT)
        if not folder.is_dir():
            return tiering.UNKNOWN_CONTEXT  # 평가한 적이 없다 — 열쇠를 재지 않는다(읽기 0)
        try:
            state = read_scope.memo(
                ("context_review.current", str(workspace.root)),
                lambda: self._context_current(workspace.root),
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.warn(f"맥락 평가를 읽지 못해 등급에는 모름으로 둔다 — {error}")
            return tiering.UNKNOWN_CONTEXT
        document = state[1]
        if document is None:
            return tiering.UNKNOWN_CONTEXT
        return context_review_io.signals_from(document, asset_id)

    def _context_current(self, root: Path) -> tuple[str, dict[str, Any] | None]:
        """(지금 입력 열쇠, 그 열쇠의 평가 문서 또는 None) — 읽기만 한다."""
        from vlogsrt.project.raw import context_review_io as cio

        key, _model = self._context_key(root)
        found = cio.load(cio.result_path(root, ANALYSIS_ROOT, key), cio.RESULT_SCHEMA, key)
        return key, found

    def _context_model(self, root: Path) -> str:
        """맥락 평가 모델 — 분석 모델이 Gemini면 그 모델, 아니면 기본 분석 모델(Gemini, D87)."""
        model = self.settings(root).models.luna_model
        if analysis_provider(model) == ANALYSIS_PROVIDER_GOOGLE:
            return model
        return DEFAULT_ANALYSIS_MODEL

    def _context_rows(self, root: Path) -> list[tuple[SourceAsset, AnalysisSourceResult]]:
        """촬영 순서의 원본마다 가장 최근 판정 증거 행 — 분석이 없는 원본은 뺀다."""
        rows = self.review_workspace_facade(root).tier_evidence_rows()
        return [
            (asset, rows[asset.asset_id])
            for asset in self.global_order_proposal(root)
            if asset.asset_id in rows
        ]

    def _context_key(self, root: Path) -> tuple[str, str]:
        """(지금 입력 열쇠, 모델) — 원본·분석 리비전·증거 문서·자막 리비전, 촬영 순서대로."""
        from vlogsrt.project.raw import context_review

        model = self._context_model(root)
        parts = []
        for asset, row in self._context_rows(root):
            transcript = self.source_transcript(root, asset.asset_id) or {}
            parts.append(
                (
                    asset.asset_id,
                    row.revision_id,
                    row.document_path or "",
                    transcript.get("revision", ""),
                )
            )
        key = context_review.context_key(
            parts, tier_version=self.tier_key(root), model=model, settings=CONTEXT_REVIEW
        )
        return key, model

    def _context_sources(self, root: Path) -> list[Any]:
        """맥락 평가의 재료 — 원본마다 평가 전 등급 구간 · 창 요약 · 원본 자막 줄(공통 시계)."""
        from vlogsrt.project.raw import context_review as cr

        workspace = self._workspace(root)
        sources = []
        for asset, row in self._context_rows(root):
            report = self._tier_report(workspace, row, with_context=False)
            if report is None:
                continue
            _directory, path = _tier_paths(workspace.root, row)
            document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            shift = _record_audio_start(workspace.root, row)
            transcript = self.source_transcript(root, asset.asset_id) or {}
            spans = tuple(
                cr.ContextSpan(s.start, s.end, s.tier, s.rule, s.audio.status, s.video.status)
                for s in report.spans
            )
            sources.append(
                cr.ContextSource(
                    asset.asset_id,
                    asset.source_name,
                    spans,
                    _context_windows(document, shift),
                    _context_lines(transcript),
                )
            )
        return sources

    def context_review_model(self, root: Path) -> str:
        """맥락 평가를 보낼 모델(D99) — 화면이 그 클라이언트를 받는다. 평가할 분석이 없으면
        클라이언트를 받기 전에 그렇게 말한다."""
        if not self._context_rows(root):
            raise RawWorkspaceError(CONTEXT_NOTHING)
        return self._context_model(root)

    def run_context_review(self, root: Path, client: object) -> dict[str, Any]:
        """맥락 평가 한 번(유료, D44) — 누른 순간의 입력으로 보낸다(견적 없이, D110).

        같은 열쇠의 평가가 이미 있으면 부르지 않고 그것을 돌려준다(다시 사지 않는다). 오프라인이면
        보내기 전에 닫는다. 실패는 까닭과 함께 남기고 등급은 그대로다. 성공하면 검토 머리의 등급
        표시만 다시 단다(사람 결정 그대로)."""
        from vlogsrt.project.raw import context_review_io as cio

        key, model = self._context_key(root)
        if not self._context_sources(root):
            raise RawWorkspaceError(CONTEXT_NOTHING)
        path = cio.result_path(root, ANALYSIS_ROOT, key)
        existing = cio.load(path, cio.RESULT_SCHEMA, key)
        if existing is not None:
            return existing
        self._require_context_online(root)
        document = self._ask_context(root, key, model, client)
        cio.write(path, document)
        self._tier_step(root)
        return document

    def _require_context_online(self, root: Path) -> None:
        blocked = online_env_blocked()
        if blocked is not None:
            raise AnalysisError(blocked)
        if self.settings(root).network_mode != "online":
            raise AnalysisError(
                "작업공간 network_mode가 online이 아니다 — 맥락 평가를 보낼 수 없다"
            )

    def _ask_context(self, root: Path, key: str, model: str, client: object) -> dict[str, Any]:
        """보내고 해석한다 — 어느 단계에서 실패하든 실패 문서를 남기고 이름을 대고 멈춘다."""
        from vlogsrt.project.raw import context_review as cr
        from vlogsrt.project.raw import context_review_io as cio
        from vlogsrt.project.raw import gemini_cost
        from vlogsrt.project.raw.understanding_luna import gemini_text
        from vlogsrt.transcribe import request_gemini_generate

        given = cr.build_input(self._context_sources(root), CONTEXT_REVIEW)
        body = cio.payload(given, gemini_profile(model), CONTEXT_REVIEW)
        failure = cio.failure_path(root, ANALYSIS_ROOT, key)
        try:
            response = request_gemini_generate(client, model=model, payload=body, online=True)
            text, reason = gemini_text(response)
            if text is None:
                raise cr.ContextReviewError(reason)
            review = cr.parse(json.loads(text), given)
        except Exception as error:
            cio.write(failure, cio.failure_document(key, model, str(error)))
            raise AnalysisError(f"맥락 평가가 실패했습니다 — {error}") from error
        usage = gemini_cost.measure_gemini_usage(
            types.SimpleNamespace(model=model), response, on=date.today()
        )
        return cio.result_document(key, model, review, given, asdict(usage))

    def context_review_state(self, root: Path) -> dict[str, Any]:
        """화면의 맥락 평가 상태 — 지금 평가(줄거리·날짜·모델) · 낡음 · 지난 실패. 읽기만."""
        from vlogsrt.project.raw import context_review_io as cio

        key, model = self._context_key(root)
        current = cio.load(cio.result_path(root, ANALYSIS_ROOT, key), cio.RESULT_SCHEMA, key)
        failed = cio.load(cio.failure_path(root, ANALYSIS_ROOT, key), cio.FAILURE_SCHEMA, key)
        folder = cio.folder(root, ANALYSIS_ROOT)
        any_result = folder.is_dir() and any(
            p for p in folder.glob("*.json") if not p.name.endswith(".failed.json")
        )
        return {
            "model": model,
            "current": current,
            "stale": current is None and bool(any_result),
            "failure": None if failed is None else failed.get("reason"),
        }

    def start_reference_import(
        self,
        root: Path,
        proposal: IngestProposal,
        *,
        prepare_preview: bool = True,
        fcp_scan: Any = None,
        prepare_proxies: bool = False,
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
            log.info(f"촬영 시각 순 정렬: {len(proposal.order)}개")
            if prepare_proxies:
                self.start_analysis_proxies(root)
            if prepare_preview:
                self.wait_child_job(root, self.start_source_preview(root), context)

        return self.submit_job(root, "reference_import", fingerprint, body)

    def start_analysis_proxies(
        self, root: Path, asset_ids: tuple[str, ...] = ()
    ) -> tuple[JobHandle, ...]:
        """4K 원본마다 25% 분석 프록시를 미리 만드는 작업 하나씩(D100 §2) — 무료 로컬.

        만들기는 분석이 쓰는 길 그대로다(`media_refs.resolve(…, "frames")`) — 같은 조리법·같은
        자리라 1단계가 다시 만들지 않고, 결제 열쇠(`media_refs.fingerprint`)는 건드리지 않는다.
        4K가 아닌 원본·준비되지 않은 원본은 건너뛴다. 동시에 도는 ffmpeg는 로컬 미디어 한도가
        묶는다.
        """
        from vlogsrt.project.raw import media_refs

        workspace = self._workspace(root)
        wanted = set(asset_ids)
        handles = []
        for asset in self.global_order_proposal(root):
            if wanted and asset.asset_id not in wanted:
                continue
            if asset.status != "ready" or not FOLDER_WORKSPACE.is_uhd(asset.width, asset.height):
                continue

            def body(context: JobContext, asset: SourceAsset = asset) -> None:
                context.set_total(1)  # 0/0으로 머물면 「작업 진행」 막대가 없다(D101)
                context.check_cancel()
                media_refs.resolve(workspace, asset, "frames")
                context.set_progress(1)

            identity = f"{asset.asset_id}|{asset.content_sha256}|{ANALYSIS_PROXY.tag()}"
            handles.append(self.submit_job(root, ANALYSIS_PROXY_JOB_KIND, identity, body))
        return tuple(handles)

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
            wanted = self._previewable(root, head, asset_ids)
            if asset_ids and not wanted:
                return  # 고른 원본이 모두 덮지 못한 원본이다 — 빈 고름은 「모든 쪽」이라 멈춘다
            self._prepare_preview_pages(root, facade, head, wanted, context)

        return self.submit_past_cancel(
            root, "source_preview", order.revision_id + repr(asset_ids), body
        )

    def submit_past_cancel(
        self, root: Path, kind: str, input_fingerprint: str, run: Callable[[JobContext], None]
    ) -> JobHandle:
        """`submit_job`과 같되, 같은 지문의 활성 작업이 취소를 요청받은 중이면 그 뒤에 새로 건다.

        멈추는 중인 작업에 다시 붙으면 아무것도 만들지 않고 끝난다 — 영상을 바꿨다가 곧바로 돌아온
        미리보기가 그랬다(앞 영상의 미리보기는 영상을 바꾸면 멈춘다). 평소의 지문은 그대로다.
        """
        handle = self.submit_job(root, kind, input_fingerprint, run)
        while handle.reattached and self.get_job(root, handle.job_id).cancellation_requested:
            handle = self.submit_job(root, kind, f"{input_fingerprint}#after:{handle.job_id}", run)
        return handle

    def _previewable(
        self, root: Path, head: ReviewRevision, asset_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        """미리보기를 만들 원본 — 계획이 덮지 못한 원본(VFR, D68)은 까닭을 남기고 뺀다.

        그 원본은 머리에 조각이 없어 만들 것이 없다. 화면은 같은 까닭을 카드에 보인다."""
        if not asset_ids:
            return asset_ids
        unsupported = self._unsupported_ids(root, head)
        for asset_id in asset_ids:
            if asset_id in unsupported:
                log.warn(f"미리보기를 만들지 않는다: {asset_id} — {UNSUPPORTED_GRID_REASON}")
        return tuple(asset_id for asset_id in asset_ids if asset_id not in unsupported)

    def _preview_head(self, root: Path, facade: Any) -> ReviewRevision:
        """미리보기가 쓸 검토 머리 — 같은 영상 묶음이면 머리를 다시 깔지 않는다(D53).

        순서만 바뀐 머리는 사람 결정을 들고 옮긴다. 옮기기가 미뤄져도(그 사이 편집) 머리는 그대로
        두고 다음 갱신이 맞춘다 — 원본마다 한 장으로 다시 까는 것은 머리가 없거나 영상이 새로
        들거나 빠졌을 때뿐이다.
        """
        self.align_review_order(root)
        head = _review_head(facade)
        latest = self.latest_global_source_order(root)
        if (
            head is not None
            and latest is not None
            and _same_sources(head, latest, self._unsupported_ids(root, head))
            and not self._newly_coverable(root, head)
        ):
            return head
        # 새로 든 영상(새로 덮인 영상, D69 포함)만 한 장으로 깐다 — 이미 있던 영상(시계가 같은)은
        # 사람 손이 닿았든 아니든 그 조각·미검토 제안을 그대로 둔다(D53: 가져오기가 손대지 않은
        # 영상의 분석을 지우지 않게).
        return self._lay_new_sources(root, facade, head)

    def _lay_new_sources(
        self,
        root: Path,
        facade: Any,
        head: ReviewRevision | None,
        *,
        expected_head: str | None = None,
    ) -> ReviewRevision:
        """지금 순서의 원본을 모두 다시 재어 계획한다 — 이미 머리에 있던 원본은 그 조각 그대로."""
        plan = self.plan_segmentation(root, tile_seconds=segmentation.WHOLE_SOURCE_TILE_SECONDS)
        kept = tuple(clock.source_id for clock in head.clocks) if head is not None else ()
        revision: ReviewRevision = facade.initialize_review(
            plan.revision_id, preserve_source_ids=kept, expected_head=expected_head
        )
        return revision

    def _newly_coverable(self, root: Path, head: ReviewRevision) -> tuple[str, ...]:
        """머리의 계획이 덮지 못했다고 기록한 원본(D68) 가운데 지금 시계로는 덮을 수 있는 원본.

        기록은 그때의 측정이다. 같은 내용의 원본이 근접 CFR 맞춤(D69)으로 이제 격자를 증명하면,
        옛 머리를 쓰는 한 「분석 지원 밖」으로 남는다(D68 (e)) — 그것이 다시 계획할 까닭이다.
        """
        workspace = self._workspace(root)
        # 기록만 읽는다 — 그리기 범위의 연결을 함께 써 단추 한 번에 DB를 더 열지 않는다.
        recorded = workspace.segment_plan_unsupported(head.base_plan_id)
        return tuple(
            item.asset_id
            for item in recorded
            if self._coverable_now(root, workspace, item.asset_id)
        )

    def _coverable_now(self, root: Path, workspace: RawWorkspace, asset_id: str) -> bool:
        """원본 하나를 지금 시계로 덮을 수 있는가 — 패킷만 훑고(디코드 없음) 실행마다 한 번.

        화면 갱신(정리 고리의 할 일 읽기)마다 다시 재지 않는다. 재지 못하면 경고하고 지원 밖
        그대로 둔다 — 답을 기억하지 않으므로 다음 갱신이 다시 잰다.
        """
        asset = workspace.source_asset(asset_id)
        key = (root.expanduser().resolve(), asset_id, asset.content_sha256 or "")
        with self._lock:
            known = self._coverable.get(key)
        if known is not None:
            return known
        try:
            covered = _measured_source(workspace, asset).clock.uniform_grid
        except (OSError, RuntimeError) as error:
            log.warn(f"덮지 못한 원본을 다시 재지 못했다: {asset.source_name} — {error}")
            return False
        if covered:
            log.info(f"덮지 못했던 원본을 이제 덮을 수 있다(격자 맞춤, D69): {asset.source_name}")
        with self._lock:
            self._coverable[key] = covered
        return covered

    def _prepare_preview_pages(
        self,
        root: Path,
        facade: Any,
        head: ReviewRevision,
        asset_ids: tuple[str, ...],
        context: JobContext,
    ) -> None:
        # 고른 원본의 조각이 없는 쪽은 걸지 않는다 — 그 쪽의 준비는 만들 것이 없다(W-24 세션 J).
        for page in visual_review.pages_with_sources(head, asset_ids):
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
                self.submit_past_cancel(
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
        # 그 종류 전부에서 찾는다 — 최근 창 밖으로 밀린 성공을 놓치면 사람 결정이 실린 검토
        # 머리를 다시 세운다(W-24 세션 J).
        initialized = any(
            job.input_fingerprint == revision_id and job.status == "succeeded"
            for job in self.recent_jobs(root, ("automatic_cleanup",), limit=None)
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
        intervals: dict[str, list[PlannedInterval]] = {}
        for entry in sorted(order.entries, key=lambda item: item.ordinal):
            spans = self._analysis_intervals(workspace, rows.get(entry.asset_id), entry.asset_id)
            if spans is not None:  # None은 VFR — 계획이 덮지 않고 까닭을 남긴다(D68)
                intervals[entry.asset_id] = spans
        return self.plan_segmentation(
            root,
            order_revision_id=order.revision_id,
            intervals=intervals,
            basis=SEGMENTATION_BASIS_SEMANTIC,
        )

    def review_reflects_analysis(
        self, root: Path, revision_id: str, asset_ids: tuple[str, ...]
    ) -> bool:
        """지금 검토 머리의 그 영상들이 이 분석 리비전의 행으로 지은 그대로인가(D124).

        그렇다면 다시 게시해도 같은 조각이 나온다 — 영상을 다시 볼 때 라이브러리 전체 계획을 새로
        세우지(원본마다 전체 해시 셋) 않아도 된다. 근거 기록이 없거나 다르면 False(다시 게시)다.
        """
        from vlogsrt.project.raw.review_workspace import basis_entry

        if not asset_ids:
            return False
        facade = self.review_workspace_facade(root)
        if not facade.has_review():
            return False
        basis = facade.source_basis()
        tier_key = facade.tier_key()
        rows = {
            row.asset_id: row
            for row in AnalysisStore(self._workspace(root)).list_source_results(revision_id)
        }
        return all(
            asset in rows and basis.get(asset) == basis_entry(rows[asset], tier_key)
            for asset in asset_ids
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
        v2 표지와 함께 되살렸다). 손대지 않았는지는 내용으로 본다(`is_untouched`). 판이 같아도
        등급의 재료(자막 시각 확인 값 등, D60)가 바뀌어 다시 매긴 등급이 다르면 등급만 다시
        단다 — 빨강은 그 재료와 무관하므로 다시 세울 일이 없다(바뀐 것이 없으면 리비전도 없다).
        """
        current = facade.load_head().revision
        if not facade.needs_tier_migration(current):
            if facade.stale_grade_segments(current):
                facade.retier(current.base_plan_id)
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
        tier_state = _tier_state(facade, head) if facade.tier_evidence_rows() else None
        if tier_state is not None:
            pending[UPKEEP_TIERS] = tier_state
        if order is not None and self._reorder_pending(root, head, order):
            pending[UPKEEP_ALIGN] = f"{head.revision_id}>{order.revision_id}"
        covered = self._newly_coverable(root, head)
        if covered:
            pending[UPKEEP_COVER] = f"{head.revision_id}+{','.join(covered)}"
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
        if UPKEEP_COVER in pending:
            lines.append(_COVER_MESSAGE)
        if UPKEEP_TIERS in pending and pending[UPKEEP_TIERS].startswith(_REGRADE_STATE):
            lines.append(_REGRADE_MESSAGE)
        elif UPKEEP_TIERS in pending:
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
            (UPKEEP_COVER, self._cover_step),
        )
        failures: dict[str, str] = {}
        for step, run in steps:
            context.check_cancel()
            if step in (UPKEEP_ALIGN, UPKEEP_COVER) and UPKEEP_TIERS in failures:
                if self._align_pending(root) or self._cover_pending(root):
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

    def _cover_pending(self, root: Path) -> bool:
        head = _review_head(self.review_workspace_facade(root))
        return head is not None and bool(self._newly_coverable(root, head))

    def _cover_step(self, root: Path) -> bool:
        """기록된 지원 밖 원본이 지금 덮이면 검토를 다시 깐다 — 사람 결정은 덮은 원본에 그대로(D69).

        순서 맞춤이 먼저 돌아 손댄 머리의 옮기기는 이미 그 원본을 덮었다(`plan_from_review`).
        여기 오는 것은 순서가 그대로인 머리다. 다시 계획은 원본을 다시 해시해 길다 — 그 사이
        머리가 바뀌었으면 덮지 않고 미룬다(`_migrate_unedited_head`와 같은 규율). 했으면 True.
        """
        facade = self.review_workspace_facade(root)
        head = _review_head(facade)
        if head is None or not self._newly_coverable(root, head):
            return False
        try:
            self._lay_new_sources(root, facade, head, expected_head=head.revision_id)
        except StaleRevisionError as error:
            log.warn(f"검토가 그 사이 바뀌어 새로 덮인 원본 깔기를 다음 갱신으로 미룬다 — {error}")
            return False
        return True

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
            batch: tuple[str, ...] | None = ids
            try:
                while batch is not None:
                    if batch:
                        sync_report.run_checks(context, workspace, batch, done=done)
                        done += len(batch)
                    batch = self._next_sync_batch(key, workspace)
            except JobCancelledError:
                raise
            except Exception as error:
                self._grey_unmeasured(key, workspace, batch or (), error)
                raise
            finally:
                self._release_sync_slot(key, context.job_id)

        return body

    def _grey_unmeasured(
        self, key: Path, workspace: RawWorkspace, batch: Sequence[str], error: BaseException
    ) -> None:
        """작업 본문이 터졌다 — 잴 차례였던(묶음·합쳐 둔) 원본 중 지금 보고서가 없는 것만 회색으로.

        이미 잰 원본은 제 보고서가 있으니 덮지 않는다. 그것조차 가리지 못하면 묶음 전체를 회색으로
        둔다 — 작업 기록·콘솔에만 남고 화면은 멀쩡해 보이는 일이 없게.
        """
        from vlogsrt.project.raw import sync_report

        with self._sync_lock:
            candidates = (*batch, *self._sync_waiting.get(key, {}))
        try:
            unmeasured = sorted(sync_report.pending_sources(workspace, source_ids=candidates))
        except Exception as lookup:  # noqa: BLE001 - 표지를 남기는 길이 또 터져도 표지는 남긴다
            log.warn(f"자막 시각 확인 — 못 잰 원본을 못 가려 묶음 전체를 회색으로 둔다: {lookup}")
            unmeasured = sorted(set(candidates))
        if unmeasured:
            self._note_sync_failure(key, unmeasured, error, repeat=True)

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

    def sync_report(
        self, root: Path, source_id: str, *, strict: bool = False
    ) -> dict[str, Any] | None:
        """원본 하나의 자막 시각 판정 문서 — `verdict`는 red·amber·green·skip. 읽기만 한다.

        지금 자막 리비전의 보고서가 아직 없으면 가장 최근 보고서를 `stale: True`로, 보고서가
        하나도 없으면 None을 돌려준다. `strict`면 1단계 PCM까지 대조해 낡음을 정한다(확인 작업의
        `is_current` 그대로) — Final Cut 키워드는 이 값으로만 단다(`caption_timing_flags`).
        """
        from vlogsrt.project.raw import sync_report

        return sync_report.current_report(self._workspace(root), source_id, strict=strict)

    def measure_sync_checks(
        self, root: Path, source_ids: Sequence[str] | None = None, *, write: bool = True
    ) -> list[dict[str, Any]]:
        """CLI(`media sync-check`)용 — 작업 없이 그 자리에서 잰다. `write=False`면 작업공간에
        아무것도 쓰지 않는다. 유료 호출 0."""
        from vlogsrt.project.raw import sync_report

        return sync_report.measure_workspace(self._workspace(root), source_ids, write=write)

    def sync_check_view(self, root: Path, source_id: str) -> dict[str, Any] | None:
        """작업대 표지·패널의 값 — 측정 불가·걸지 못함은 회색이다(결코 초록이 아니다)."""
        key = ("sync_check_view", str(root.expanduser().resolve()), source_id)
        return read_scope.memo(key, lambda: self._read_sync_check_view(root, source_id))

    def _read_sync_check_view(self, root: Path, source_id: str) -> dict[str, Any] | None:
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

    @staticmethod
    def subtitle_cue_notes(
        transcript: Mapping[str, Any],
        sync_view: Mapping[str, Any] | None,
        timing: Any = None,
    ) -> dict[str, dict[str, Any]]:
        """원본 자막 줄마다 화면 표시(D59 19e) — 「다른 귀」 증거와 「확인 필요」 이유. 표시만 한다.

        줄을 지우거나 숨기거나 고치지 않는다(규칙 4·9) — 아이디로 이유를 달 뿐이다. 말소리
        측정상 없음은 **지금 자막 리비전**의 확인 보고서(`stale`이 아닌 것)에서만 값으로 넘긴다 —
        다른 리비전을 잰 표지는 그 줄의 것이 아닐 수 있다. 표시가 없는 줄은 싣지 않는다.
        `timing`(`subtitle_cue_timing`의 값)이 있을 때만 시각 표시 셋(D76)이 더해진다 — 등급의
        「자막 확인됨」 몫(D65)은 그것 없이 센다(`_suspect_share`).
        """
        from vlogsrt.project.raw import source_transcript, subtitle_suspects

        rows = list(transcript.get("utterances", []))
        flagged = subtitle_suspects.suspects(
            [(str(row["utterance_id"]), str(row["text"])) for row in rows],
            _current_no_speech(sync_view),
            timing=timing,
        )
        notes: dict[str, dict[str, Any]] = {}
        for row in rows:
            cue_id = str(row["utterance_id"])
            note: dict[str, Any] = {}
            ear = source_transcript.ear_evidence(row)
            if ear is not None:
                note["ear"] = ear
            if cue_id in flagged:
                note["suspects"] = [{"code": s.code, "text": s.text} for s in flagged[cue_id]]
            heard = timing.gemini_heard.get(cue_id, "") if timing is not None else ""
            if heard.strip():
                note["gemini"] = {"heard": heard}
            if note:
                notes[cue_id] = note
        return notes

    @staticmethod
    def subtitle_cue_timing(transcript: Mapping[str, Any], stage1: Any) -> Any:
        """「확인 필요」 시각 표시 셋(W-25f, D76)의 재료 — 1단계 기록의 조각을 줄의 `evidence_id`로
        잇는다.

        `evidence_id`(`<조각 요청 아이디>:<세그먼트 위치>`)가 기록의 조각과 이어지는 줄만 싣는다
        — 빈칸 재전사로 넣은 줄·다른 전사의 줄은 빠진다. 조각 시계는 줄의 공통 시계에서 PCM 0의
        자리(PCM 원점 − 제시 원점)와 조각 시작을 뺀 값이다. 기록이 없으면 빈 재료다(표시를 지어내지
        않는다).
        """
        from vlogsrt.project.raw import source_transcript, subtitle_suspects

        disagreed = _ear_disagreed(transcript)
        if stage1 is None or stage1.absent:
            return subtitle_suspects.CueTiming(ear_disagreed=disagreed)
        by_request = {chunk.request_id: chunk for chunk in stage1.chunks}
        pcm_zero = stage1.origin - stage1.presentation_origin
        clamped: set[str] = set()
        local: dict[str, tuple[Fraction, Fraction]] = {}
        scores: dict[str, tuple[float | None, float | None]] = {}
        noted: dict[str, tuple[str, ...]] = {}
        for row in transcript.get("utterances", []):
            request, _, position = str(row.get("evidence_id") or "").rpartition(":")
            chunk = by_request.get(request)
            if chunk is None or not position.isdigit():
                continue
            cue_id, at = str(row["utterance_id"]), int(position)
            shift = pcm_zero + chunk.start
            local[cue_id] = (
                source_transcript.rational(row["start"]) - shift,
                source_transcript.rational(row["end"]) - shift,
            )
            if at in chunk.clamped:
                clamped.add(cue_id)
            if at < len(chunk.scores):
                scores[cue_id] = chunk.scores[at]
            names = tuple(name for where, name in chunk.word_notes if where == at)
            if names:
                noted[cue_id] = names
        return subtitle_suspects.CueTiming(
            frozenset(clamped), local, scores, disagreed, engine_words=noted
        )

    def subtitle_timing(self, root: Path, source_id: str, transcript: Mapping[str, Any]) -> Any:
        """화면의 「확인 필요」 시각 표시 셋 재료 — 원본 자막이 묶인 원본 해시의 가장 최근 1단계
        기록으로."""
        from vlogsrt.project.raw import sync_report

        stage1 = sync_report.stage1_audio(
            self._workspace(root).root, source_id, str(transcript.get("source_sha256") or "")
        )
        timing = self.subtitle_cue_timing(transcript, stage1)
        return self.with_gemini_heard(
            timing, transcript, self._gemini_heard_windows(root, source_id)
        )

    @staticmethod
    def with_gemini_heard(
        timing: Any, transcript: Mapping[str, Any], windows: Sequence[Any]
    ) -> Any:
        """「Gemini가 다르게 들음」(W-30e, D88)의 재료를 시각 재료에 얹는다 — 창이 없으면
        그대로다."""
        from vlogsrt.project.raw import source_transcript, subtitle_suspects

        if not windows:
            return timing
        cues = [
            (
                str(row["utterance_id"]),
                str(row["text"]),
                source_transcript.rational(row["start"]),
                source_transcript.rational(row["end"]),
            )
            for row in transcript.get("utterances", [])
        ]
        heard, disagreed = subtitle_suspects.heard_comparison(cues, windows)
        return replace(timing, gemini_heard=heard, gemini_disagreed=disagreed)

    def _gemini_heard_windows(self, root: Path, source_id: str) -> tuple[Any, ...]:
        """그 원본의 **가장 최근** 영상 분석이 Gemini 판정이면 창마다 들은 말(D88) — 아니면 빈 값.

        더 최근에 다른 모델로 다시 분석했으면 옛 Gemini의 말을 쓰지 않는다(그 분석이 지금의
        판정이다). 받아들인 판정의 창만 싣는다 — 실패·거부한 창은 들은 말이 없다(빈 말로 「듣지
        못함」을 짓지 않는다). 창 시각은 원본 자막과 같은 공통 시계다.
        """
        from vlogsrt.project.raw import source_transcript, subtitle_suspects

        workspace = self._workspace(root)
        document = self._latest_analysis_document(workspace, source_id)
        if document is None:
            return ()
        annotations = {
            str(row.get("window_id")): row
            for row in document.get("annotations", [])
            if isinstance(row, Mapping)
            and row.get("status") == "accepted"
            and str(row.get("luna_model", "")).startswith(GEMINI_MODEL_PREFIX)
        }
        windows = (document.get("windows") or {}).get("windows", [])
        return tuple(
            subtitle_suspects.HeardWindow(
                source_transcript.rational(window["core_start"]),
                source_transcript.rational(window["core_end"]),
                str(annotations[str(window["window_id"])].get("heard_speech", "")),
            )
            for window in windows
            if str(window.get("window_id")) in annotations
        )

    def _latest_analysis_document(
        self, workspace: RawWorkspace, source_id: str
    ) -> dict[str, Any] | None:
        """그 원본의 가장 최근 판정 문서 — 1단계만 끝난 행(「전사만」)은 건너뛴다.

        원본마다 최근 행은 그리기 한 번에 쿼리 한 번으로 모두 읽는다(읽기 범위, D63)."""
        store = AnalysisStore(workspace)
        latest = read_scope.memo(
            ("latest_judged_results", str(workspace.root)), store.latest_judged_results
        )
        row = latest.get(source_id)
        if row is None:
            return None
        _directory, path = _tier_paths(workspace.root, row)
        if not path.is_file():
            return None
        try:
            body = json.loads(file_memo.read_text(path))
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None

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
        _forget_read_scope()  # 표지는 `sync_check_view`가 읽는다 — 범위 안의 기억은 낡았다
        return fresh

    def _clear_sync_failures(self, key: Path, source_ids: Iterable[str]) -> None:
        """확인이 다시 걸렸다 — 그 원본과 작업공간 전체의 '걸지 못함' 표지를 지운다."""
        with self._lock:
            failures = self._sync_failures.get(key, {})
            cleared = [failures.pop(target, None) for target in (*source_ids, SYNC_ALL_SOURCES)]
        if any(reason is not None for reason in cleared):
            _forget_read_scope()

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
        return (
            f"자막 시각 확인 {len(fresh)}개를 백그라운드에서 잽니다(무료 · 자막과 컷은"
            " 그대로입니다)."
        )

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
        return _same_sources(head, order, {item.asset_id for item in plan.unsupported})

    def _unsupported_ids(self, root: Path, head: ReviewRevision) -> set[str]:
        """머리의 계획이 덮지 못한 원본(D68) — 머리에 시계가 없어도 순서에서 빠진 영상이 아니다."""
        plan = self.segment_plan_revision(root, head.base_plan_id)
        return {item.asset_id for item in plan.unsupported}

    def review_unsupported_sources(self, root: Path) -> tuple[UnsupportedSource, ...]:
        """지금 검토 머리의 계획이 덮지 못한 원본과 까닭(D68) — 머리가 없으면 빈 값이다."""
        head = _review_head(self.review_workspace_facade(root))
        if head is None:
            return ()
        return self.segment_plan_revision(root, head.base_plan_id).unsupported

    def order_covers_same_sources(self, root: Path, order_revision_id: str) -> bool:
        """그 전체 순서가 지금 순서와 같은 원본 묶음인가 — 순서만 다르면 같은 묶음이다(D53).

        분석은 원본마다의 것이라 순서가 바뀌어도 낡지 않는다 — 다시 승인받지 않는다.
        """
        latest = self.latest_global_source_order(root)
        if latest is None:
            return False
        recorded = self._order_revision(root, order_revision_id)
        return set(_order_ids(recorded)) == set(_order_ids(latest))

    def source_settings(self, root: Path, asset_id: str) -> RawWorkflowConfig:
        """그 원본을 듣는 설정 보기(D92) — whisper 원본은 저장된 설정 그대로(같은 객체)다."""
        return timing_engine.source_workflow(self._workspace(root), asset_id, self.settings(root))

    def new_hearing_engine(self, root: Path) -> str:
        """처음 듣거나 다시 듣는 영상이 쓸 시간 엔진의 열쇠(D92)."""
        return timing_engine.new_hearing_engine(self._workspace(root))

    def set_new_hearing_engine(self, root: Path, key: str) -> str:
        """「새로 듣는 엔진」을 바꾼다 — 이미 들은 영상은 그대로다(바꾸려면 다시 듣기, D92)."""
        timing_engine.set_new_hearing_engine(self._workspace(root), key)
        return timing_engine.new_hearing_engine(self._workspace(root))

    def export_finalcut_timeline(self, root: Path, context: JobContext | None = None) -> Path:
        """불러온 원본 전부를 태그 달린 타임라인 하나로 — 새 FCPXML 경로를 돌려준다(D56).

        모으고 대조하고 쓰는 일은 `finalcut_export`가 한다. 지우지 않으므로 미검토 자동
        제외 관문이 없고, Final Cut은 열지 않는다(여는 것은 화면의 몫이다). 「꺼진 클립 빼고
        보내기」(D94)가 켜져 있으면 꺼진 클립을 프로젝트에서 빼고, 그때만 빠질 클립의 미검토
        제외 제안을 먼저 확인하게 한다.
        """
        from vlogsrt.project.raw import finalcut_export

        flags = self._caption_timing_flags_or_reason(root)
        gaps = self._caption_gaps_or_unknown(root)
        outcome = finalcut_export.export(
            self,
            root,
            context,
            caption_timing=flags,
            caption_gaps=gaps,
            drop_disabled=self.finalcut_drop_disabled(root),
        )
        return outcome.fcpxml

    def export_finalcut_titles(
        self, root: Path, srt: Path, context: JobContext | None = None
    ) -> Path:
        """같은 전체 타임라인에 원본 자막 대신 SRT 줄마다 Basic Title 타이틀을 단다 — 새 FCPXML
        경로(D91).

        SRT는 읽기만 한다(못 읽으면 이름을 대고 거부). 원본 자막의 시각 표지·빠진 구간은 그 자막의
        것이라 읽지 않는다. Final Cut은 열지 않는다(여는 것은 화면의 몫이다).
        """
        from vlogsrt.project.raw import finalcut_export
        from vlogsrt.project.raw import timeline_srt as srt_file

        lines = srt_file.read(srt)
        drop = self.finalcut_drop_disabled(root)
        return finalcut_export.export(
            self, root, context, titles_srt=lines, drop_disabled=drop
        ).fcpxml

    def start_render_export(self, root: Path) -> JobHandle:
        """추천만 이어붙인 초안(W-44, D100 §5b) — 새 `export/<시각>/`에 영상·SRT·보고서를 내는 작업.

        확인하지 않은 제외 제안이 있어도 막지 않는다(사용자 결정 2026-10-27) — 뺀 구간은 보고서에
        남는다. 원본은 읽기만 한다. 결과는 `latest_render_export`가 읽는다. 무료 로컬이다 — 영어
        자막은 낸 SRT를 「영어 SRT로 변환」이 따로 옮긴다(D114).
        """
        from vlogsrt.project.raw import render_export

        def body(context: JobContext) -> None:
            render_export.export_draft(self, root, context)

        return self.submit_job(root, RENDER_EXPORT_JOB_KIND, "render_export/draft", body)

    def start_timeline_render(self, root: Path, timeline_id: str, revision: int) -> JobHandle:
        """원본 범위 타임라인에서 렌더(W-71e, D130) — 초안과 같은 작업 종류·같은 `export/<시각>/`.

        결과 카드·영어 변환·자막 입히기·초안 편집이 그 폴더를 그대로 쓴다. 지문에 타임라인 아이디와
        리비전이 들어 같은 리비전이면 도는 작업에 다시 붙는다. 무료 로컬이다."""
        from vlogsrt.project.raw import timeline_render

        def body(context: JobContext) -> None:
            timeline_render.export_timeline(self, root, timeline_id, revision, context)

        fingerprint = f"render_export/timeline/{timeline_id}@{revision}"
        return self.submit_job(root, RENDER_EXPORT_JOB_KIND, fingerprint, body)

    def start_timeline_share(
        self,
        root: Path,
        timeline_id: str,
        revision: int,
        burn: str,
        client: object | None = None,
    ) -> JobHandle:
        """「최종출력」(W-88 · D147) — 렌더(D130)하고 `burn`이 「없음」이 아니면 자막을
        입힌다(D116).

        작업 종류·지문 머리는 렌더와 같다(결과 카드·「다시 열기」가 그대로 본다). 번인을 고르면
        libass ffmpeg를 작업 전에 확인한다. 렌더·번인은 무료 로컬이다. 「한국어 + 영어」에서 화면이
        Gemini 클라이언트를 넘기면(그 글의 번역이 없을 때, W-108 · D182) 작업 안에서 옮긴다 —
        유료이고 누름이 승인이다(D110). 온라인 문은 작업 전에 본다. 없으면 사지 않는다(D114)."""
        from vlogsrt.project.raw import burn_export, srt_english, timeline_share

        mode = timeline_share.require_mode(burn)
        style = burn_export.saved_style(self, root)
        if mode != timeline_share.BURN_NONE:
            burn_export.executable()
        translator = client if mode == timeline_share.BURN_KOREAN_ENGLISH else None
        if translator is not None:
            srt_english.require_online(self, root)

        def body(context: JobContext) -> None:
            timeline_share.share(
                self, root, timeline_id, revision, mode, style, context, translator=translator
            )

        fingerprint = f"render_export/timeline/{timeline_id}@{revision}/{mode}"
        return self.submit_job(root, RENDER_EXPORT_JOB_KIND, fingerprint, body)

    def export_srt_again(self, root: Path) -> Any:
        """「SRT만 다시 내보내기」(D115) — 마지막 초안 폴더에 지금 원본 자막으로 새 SRT를 쓴다.

        영상은 다시 만들지 않고 보고서의 조각을 그대로 쓴다. 무료 로컬이고 짧아 작업으로 걸지
        않는다.
        """
        from vlogsrt.project.raw import render_export

        return render_export.export_srt_again(self, root)

    def has_render_export(self, root: Path) -> bool:
        """다 낸 초안(보고서가 있는 폴더)이 하나라도 있는가 — 파일만 본다(DB를 열지 않는다, W-46a).

        머리줄 「내보내기」 단계가 그릴 때마다 묻는다 — 그리기의 DB 열기 수를 늘리지 않는다.
        """
        from vlogsrt.project.raw import render_export

        base = root.expanduser() / render_export.EXPORT_FOLDER
        if not base.is_dir():
            return False
        return any((item / render_export.REPORT_NAME).is_file() for item in base.iterdir())

    def latest_render_export(self, root: Path) -> dict[str, Any] | None:
        """가장 최근에 다 낸 초안의 경로·요약(보고서가 있는 폴더) — 없으면 None. 읽기만 한다."""
        from vlogsrt.project.raw import render_export

        return render_export.latest(self._workspace(root))

    def final_edit_open(
        self, root: Path, export_id: str | None = None, *, reseed: bool = False
    ) -> dict[str, Any]:
        """최종 편집 화면의 재료(D117) — 저장본 또는 초안 SRT로 심은 문서. 읽기만 한다."""
        from vlogsrt.project.raw import final_edit_io

        return final_edit_io.open_edit(self._workspace(root), export_id, reseed=reseed)

    def final_edit_save(
        self,
        root: Path,
        export_id: str,
        base_revision: int,
        payload: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """최종 편집 저장(D117) — 문서 전체를 다시 검사하고 `edit/`에만 쓴다."""
        from vlogsrt.project.raw import final_edit_io

        return final_edit_io.save_edit(
            self._workspace(root), export_id, base_revision, payload, force=force
        )

    def start_timeline_export(self, root: Path) -> JobHandle:
        """「타임라인으로 보내기」(W-71b, D126) — 추천 조각을 렌더하지 않고 원본 범위 타임라인으로
        만들어 새 `timeline/<시각>/`에 쓰는 작업. 원본 조사(ffprobe)만 하고 무료 로컬이다."""
        from vlogsrt.project.raw import timeline_edit_io
        from vlogsrt.project.raw.config import OUTPUT_FORMAT

        def body(context: JobContext) -> None:
            # 새 타임라인은 4K · 29.97fps, 방향은 남긴 길이가 많은 쪽(D172).
            timeline_edit_io.create_timeline(self, root, context, output=OUTPUT_FORMAT)

        return self.submit_job(root, TIMELINE_EXPORT_JOB_KIND, "timeline_export/create", body)

    def has_timeline(self, root: Path) -> bool:
        """보낸 타임라인이 하나라도 있는가 — 파일만 본다(DB를 열지 않는다)."""
        from vlogsrt.project.raw import timeline_edit_io

        return bool(timeline_edit_io.timeline_folders(root))

    def timeline_open(self, root: Path, timeline_id: str | None = None) -> dict[str, Any]:
        """원본 범위 타임라인의 재료(D126) — 파일의 문서·요약과 바뀐 원본 자막. 읽기만 한다."""
        from vlogsrt.project.raw import timeline_edit_io

        return timeline_edit_io.open_timeline(self, root, timeline_id)

    def timeline_media(self, root: Path, timeline_id: str | None = None) -> dict[str, Any]:
        """원본 범위 타임라인 재생 재료(W-71c) — 클립이 있는 원본마다 원본 전체 미리보기.

        완성본이면 등록 ID와 브라우저 시각 원점(`video_origin_seconds` — 원본 국소 s초는 미리보기
        `원점 + s`초)을, 아니면 상태만 준다(지어내지 않는다). 읽기만 한다."""
        from vlogsrt.project.raw import timeline_edit_io

        timeline, source_ids = timeline_edit_io.clipped_sources(root.expanduser(), timeline_id)
        active = self.active_jobs(root, (VISUAL_PREPARE_JOB_KIND,))
        previews = visual_review.source_previews(self._workspace(root), source_ids, active)
        sources: dict[str, dict[str, Any]] = {}
        for source_id, preview in previews.items():
            ready = (
                preview.state == "ready"
                and preview.registered_file_id is not None
                and preview.browser_origin_seconds is not None
            )
            sources[source_id] = {
                "state": preview.state,
                "fileId": preview.registered_file_id if ready else None,
                "origin": preview.browser_origin_seconds if ready else None,
                "width": preview.width if ready else None,
                "height": preview.height if ready else None,
                "rotation": preview.rotation_degrees if ready else None,
            }
        return {"timelineId": timeline, "sources": sources}

    def timeline_waveform(self, root: Path, source_id: str) -> dict[str, Any]:
        """원본 하나의 파형(W-87b) — 자막 레인이 그린다. 처음 한 번 재고 캐시한다(무료 로컬)."""
        from vlogsrt.project.raw import source_waveform

        return source_waveform.waveform(self._workspace(root), source_id)

    def timeline_korean_srt(self, root: Path, timeline_id: str, revision: int) -> Path:
        """저장한 리비전의 자막 SRT(W-99, D174) — 「영어 만들기」가 `start_srt_translation`에
        넘긴다. 무료(파일 하나). 화면의 리비전이 지금 저장본과 다르면 거부한다."""
        from vlogsrt.project.raw import timeline_edit_io

        return timeline_edit_io.korean_srt(root, timeline_id, revision)

    def timeline_english(self, root: Path, timeline_id: str) -> dict[str, str]:
        """타임라인 자막의 영어 열(W-87d) — 렌더한 초안들의 저장된 번역을 한국어 글로.

        읽기만 한다(무료 — 사지 않는다)."""
        from vlogsrt.project.raw import timeline_english

        return timeline_english.english_by_text(self, root, timeline_id)

    def timeline_tiers(self, root: Path, source_ids: Sequence[str]) -> dict[str, list[list[str]]]:
        """원본마다 등급 구간 [시작 초, 끝 초, 등급](원본 시계, 원점 0) — 범위 조정의 띠(W-71d).

        렌더 계획이 쓰는 같은 보고서(`tier_report`)다. 표시만 하고 아무것도 쓰지 않는다 — 분석이
        없으면 빈 목록."""
        rows = self.review_workspace_facade(root).tier_evidence_rows()
        tiers: dict[str, list[list[str]]] = {}
        for source_id in source_ids:
            row = rows.get(source_id)
            report = self.tier_report(root, row.revision_id, source_id) if row else None
            spans = report.spans if report is not None else ()
            tiers[source_id] = [[str(span.start), str(span.end), span.tier] for span in spans]
        return tiers

    def timeline_save(
        self,
        root: Path,
        timeline_id: str,
        base_revision: int,
        payload: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """원본 범위 타임라인 저장(D126) — 파일의 사실로 다시 검사하고 그 폴더에만 쓴다."""
        from vlogsrt.project.raw import timeline_edit_io

        return timeline_edit_io.save_timeline(
            root.expanduser(), timeline_id, base_revision, payload, force=force
        )

    def timeline_add_source(
        self,
        root: Path,
        timeline_id: str,
        base_revision: int,
        payload: Mapping[str, Any],
        source_id: str,
    ) -> dict[str, Any]:
        """클립이 없는 원본을 원본 전체 클립으로 넣는다(W-71g) — 처음 넣으면 지금 자막을 심는다."""
        from vlogsrt.project.raw import timeline_edit_io

        return timeline_edit_io.add_source_to_timeline(
            self, root.expanduser(), timeline_id, base_revision, payload, source_id
        )

    def final_edit_waveform(self, root: Path, export_id: str) -> dict[str, Any]:
        """초안 영상의 파형(D117) — 처음 한 번 재고 `edit/waveform.json`에 둔다(무료 로컬)."""
        from vlogsrt.project.raw import final_edit_io

        return final_edit_io.waveform(self._workspace(root), export_id)

    def _caption_gaps_or_unknown(self, root: Path) -> dict[str, Any] | None:
        """보내기 보고서의 전사 실패 구간 — 원본마다 1단계 기록을 읽기만 한다(D59).

        표시 전용 칸이라 못 읽어도 보내기를 막지 않는다 — 그때는 None(모름)으로 남기고 이유를
        로그에 남긴다. 빈 목록으로 꾸미지 않는다(빈 목록은 '실패 구간 없음'이다).
        """
        from vlogsrt.project.raw import sync_report

        workspace = self._workspace(root)
        try:
            return {
                asset.asset_id: sync_report.caption_gaps(workspace, asset)
                for asset in self.global_order_proposal(root)
            }
        except Exception as error:  # noqa: BLE001 - 표시 전용 보고서 칸이 보내기를 막지 못한다
            log.warn(
                "자막이 빠진 구간을 읽지 못해 보내기 보고서에 '모름'으로 남깁니다"
                f" — {type(error).__name__}: {error}"
            )
            return None

    def _caption_timing_flags_or_reason(self, root: Path) -> Any:
        """표지는 표시 전용이다 — 못 만들면 키워드 없이 보내고, 이유를 보고서 칸과 로그에 남긴다.

        빈 표지로 꾸미지 않는다: 칸에는 `error`만 있고 `flagged` 같은 목록은 없다(재지 못한
        것을 '걸린 것 없음'으로 읽히게 두지 않는다). 화면은 그 `error`를 보내기 결과에 보인다.
        """
        from vlogsrt.project.raw import finalcut_export, finalcut_timeline

        try:
            return self.caption_timing_flags(root)
        except Exception as error:  # noqa: BLE001 - 표시 전용 키워드가 보내기를 막지 못한다
            reason = f"{type(error).__name__}: {error}"
            log.warn(f"자막 시각 확인 키워드를 달지 못해 키워드 없이 보낸다 — {reason}")
            section = {
                "rule": "current_red_only",
                "keyword": finalcut_timeline.KEYWORD_CAPTION_TIMING,
                "error": reason,
            }
            return finalcut_export.CaptionTimingFlags(flagged=frozenset(), section=section)

    def caption_timing_flags(self, root: Path) -> Any:
        """Final Cut 보내기의 `자막 시각 확인 필요` 표지 — **지금 빨강** 보고서인 원본만(D55·D56).

        지금이란 보고서의 자막 리비전·1단계 PCM·기준이 지금과 같다는 뜻이다(확인 작업의 낡음
        규칙 그대로). 낡았거나 없거나 측정 불가·초록·노랑은 달지 않고 보고서 칸에 이름을 남긴다.
        저장된 보고서를 읽기만 한다 — 보내기가 확인 작업을 걸거나 재는 일은 없다.
        """
        from vlogsrt.project.raw import finalcut_export, finalcut_timeline

        buckets: dict[str, list[dict[str, str]]] = {name: [] for name in _SYNC_EXPORT_BUCKETS}
        for asset in self.global_order_proposal(root):
            document = self.sync_report(root, asset.asset_id, strict=True)
            buckets[_sync_export_bucket(document)].append(
                {"source_id": asset.asset_id, "name": asset.source_name}
            )
        flagged = frozenset(row["source_id"] for row in buckets["flagged"])
        keyword = finalcut_timeline.KEYWORD_CAPTION_TIMING
        # 확인 작업이 도는 중이면 `missing`·`stale`은 아직 쓰이는 중인 보고서다(실측 09:17 보내기,
        # D59) — 화면이 「확인이 끝난 뒤 다시 보내면 붙습니다」를 말할 근거로 남긴다.
        latest = self.latest_job(root, SYNC_CHECK_JOB_KIND)
        running = latest is not None and latest.status in ACTIVE_JOB_STATUSES
        section = {"rule": "current_red_only", "keyword": keyword, "running": running, **buckets}
        return finalcut_export.CaptionTimingFlags(flagged=flagged, section=section)

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

    def managed_folder_workspace(self, folder: Path) -> tuple[WorkspaceStatus, bool]:
        """촬영 영상 폴더의 작업공간 — `<폴더>/_vlogsrt/`를 만들거나 이어 연다(D100 §2).

        폴더 안에 쓸 수 없으면(읽기 전용 SD 카드·권한) 형제 폴더 `<폴더>-vlogsrt`로 물러난다. 이미
        있는 작업공간은 안쪽, 형제 순서로 찾아 그대로 연다(만들지 않는다). 자리에 작업공간 아닌 것이
        있으면 조용히 덮지 않고 실패한다. (상태, 새로 만들었는가)를 돌려준다.
        """
        resolved = _video_folder(folder)
        inside = resolved / FOLDER_WORKSPACE.workspace_dirname
        sibling = resolved.with_name(resolved.name + FOLDER_WORKSPACE.sibling_suffix)
        existing = _existing_folder_workspace(resolved)
        if existing is not None:
            return self._open_folder_workspace(existing), False
        try:
            return self._create_folder_workspace(inside, resolved.name), True
        except OSError as error:
            if error.errno not in FOLDER_WRITE_REFUSALS:
                raise
            log.warn(f"폴더에 쓸 수 없어 옆 폴더에 작업공간을 둡니다: {sibling} ({error})")
        return self._create_folder_workspace(sibling, resolved.name), True

    def _open_folder_workspace(self, root: Path) -> WorkspaceStatus:
        try:
            return self.open_workspace(root)
        except WorkspaceNotFoundError as error:
            raise IngestError(
                f"작업공간 자리({root})가 이미 있지만 v2 작업공간이 아니다 — {error}"
            ) from error

    def _create_folder_workspace(self, root: Path, name: str) -> WorkspaceStatus:
        try:
            return self.create_workspace(root, name)
        except WorkspaceAlreadyExistsError as error:
            raise IngestError(
                f"작업공간 자리({root})에 이미 다른 내용이 있다 — 비우거나 옮긴 뒤 다시 시도하세요"
            ) from error

    def start_folder_import(
        self,
        folder: Path,
        inspect: MediaInspect = ffprobe_media_inspector,
        *,
        prepare_preview: bool = True,
    ) -> tuple[WorkspaceStatus, JobHandle]:
        """폴더 불러오기(D100 §2) — 작업공간을 열고, 스캔하고, 원본을 복사하지 않고 참조로 들인다.

        원본은 읽기만 한다(해시만 잰다 — `reference_ingest.register_references`). 들인 뒤 4K 원본만
        분석 프록시 작업을 따로 걸고(`start_analysis_proxies`), 원본 전체 미리보기를 기다린다.
        영상이 하나도 없으면 작업공간을 만들기 전에 사유와 함께 실패한다 — 빈 성공으로 넘어가지
        않고, 빈 `_vlogsrt`도 남기지 않는다(W-46a).
        """
        resolved = _video_folder(folder)
        # 작업공간을 만들기 **전에** 스캔한다 — 영상이 없는 폴더에 빈 `_vlogsrt`를 남기지 않는다.
        planned = _existing_folder_workspace(resolved) or (
            resolved / FOLDER_WORKSPACE.workspace_dirname
        )
        proposal = scan_sources(resolved, planned, inspect)
        if not proposal.order:
            reasons = "; ".join(
                f"{r.source_relative_path}: {r.reason}" for r in proposal.scan.rejected
            )
            raise IngestError(
                f"불러올 영상이 없다: {folder}" + (f" — {reasons}" if reasons else "")
            )
        status, created = self.managed_folder_workspace(resolved)
        root = Path(status.workspace_root)
        # 짧게 — rich가 터미널 폭에서 줄을 나눠 긴 경로가 서버 줄 여럿으로 갈린다.
        log.info(f"작업공간 {'만듦' if created else '다시 엶'}: {root.parent.name}/{root.name}")
        if root.resolve() != planned.resolve():  # 안쪽에 쓸 수 없어 형제로 물러났다
            proposal = self.scan_sources(root, resolved, inspect)
        handle = self.start_reference_import(
            root, proposal, prepare_preview=prepare_preview, fcp_scan=None, prepare_proxies=True
        )
        return status, handle

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
        jobs = self.recent_jobs(root, (INGEST_JOB_KIND,))
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
        프레임 격자를 증명하지 못한 원본(VFR)은 덮지 않고 까닭과 함께 `unsupported`에
        남긴다(D68) — 원본 하나가 라이브러리 전체의 계획을 막지 않는다.
        """
        workspace = self._workspace(root)
        revision = self._order_revision(root, order_revision_id)
        planned = intervals or {}
        unknown = sorted(set(planned) - {entry.asset_id for entry in revision.entries})
        if unknown:
            raise SegmentationError(
                f"순서 리비전에 없는 자산에게 구간을 주었다: {', '.join(unknown[:3])}"
            )
        planned_sources = _planned_sources(workspace, revision, planned, tile_seconds)
        snapshot = SegmentPlanSnapshot(
            revision_id=new_segment_plan_revision_id(),
            order_revision_id=revision.revision_id,
            basis=basis
            or (SEGMENTATION_BASIS_PLANNED if planned else SEGMENTATION_BASIS_DETERMINISTIC),
            entries=tuple(
                sorted(
                    planned_sources.entries,
                    key=lambda item: (item.source_order, item.segment_order),
                )
            ),
            coverage=tuple(planned_sources.coverage),
            fingerprint=segmentation.segmentation_fingerprint(
                revision.revision_id, planned_sources.clocks
            ),
            created_at=_plan_timestamp(),
            unsupported=tuple(planned_sources.unsupported),
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
        *,
        trust_stat: bool = False,
    ) -> tuple[AnalysisEstimate, ...]:
        """선택 원본의 분석 견적 — 로컬 조사만, 실제 호출은 전혀 없다(§14.3).

        창·프레임·청크 수는 측정된 시계에서 잰 값이고(이미지 수는 상한),
        Luna 단가는 미확인이라 금액을 만들지 않는다(Claude는 공식 단가의 보수적
        상한, D54). 실제 호출은 이 견적을 본 사람의 승인 뒤에만 시작한다.
        `trust_stat`은 원본 재검증의 stat 믿기다(D62 A2) — 승인 견적만 켠다. 1단계 승인을
        만드는 `mint_asr_approvals`와 실행 문(`start_analysis`)은 끈 채 완전 해시한다.
        """
        workspace = self._workspace(root)
        order = self._order_revision(root, None)
        understanding = settings or RawUnderstandingSettings()
        validate_understanding_settings(understanding)
        workflow = self.settings(root)
        selected = self._analysis_selection(root, order, asset_ids, trust_stat=trust_stat)
        width = Fraction(str(understanding.window_seconds))
        estimates: list[AnalysisEstimate] = []
        for entry in sorted(order.entries, key=lambda item: item.ordinal):
            asset = selected.get(entry.asset_id)
            if asset is None:
                continue
            view = timing_engine.source_workflow(workspace, asset.asset_id, workflow)
            estimates.append(
                _analysis_estimate_for(workspace, asset, entry.ordinal, understanding, view, width)
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
        timing_client: object | None = None,
        preencode_clips: bool = False,
    ) -> JobHandle:
        """의미 분석 작업을 제출한다 — 기본은 **오프라인 재생**, 실제 호출은 두 단계 승인만.

        `preencode_clips`는 전체 승인(전사 + 영상 분석)이 1단계 동안 Gemini 창 클립을 미리 자르게
        한다(P-4, D158) — 「전사만」 승인은 넘기지 않는다.

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
            timing_client=timing_client,
        )
        if preencode_clips:
            providers = replace(providers, preencode_clips=True)
        fingerprint = self._analysis_fingerprint(
            order.revision_id, assets, understanding, workflow, mode, root=root
        )
        active = next(
            (
                job
                for job in reversed(self.active_jobs(root, (ANALYSIS_JOB_KIND,)))
                if job.input_fingerprint == fingerprint
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
        timing_client: object | None = None,
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
        _require_current_prompt(understanding)
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
            timing_client=timing_client,
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
        _require_current_prompt(understanding)
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
                for job in reversed(self.active_jobs(root, (ANALYSIS_SEMANTIC_JOB_KIND,)))
                if job.input_fingerprint == fingerprint
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
        understanding = RawUnderstandingSettings(**json.loads(revision.settings_json))
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
                    **_semantic_cost_fields(luna_model, understanding, window_count),
                )
            )
        return tuple(estimates)

    def revision_matches_settings(self, root: Path, revision_id: str) -> bool:
        """분석 리비전이 지금 설정의 모델로 만든 것인가(D54) — 읽기만 한다.

        의미 분석 모델을 바꾸면 다른 모델로 끝난 분석은 끝났다고 보이지 않아야 한다(되돌리면
        다시 맞는다). 없는 리비전은 맞지 않는다.
        """
        key = ("revision_matches_settings", str(self._workspace(root).root), revision_id)
        return read_scope.memo(key, lambda: self._read_revision_matches(root, revision_id))

    def _read_revision_matches(self, root: Path, revision_id: str) -> bool:
        revision = AnalysisStore(self._workspace(root)).revision(revision_id)
        if revision is None:
            return False
        models = self.settings(root).models
        recorded = json.loads(revision.models_json)
        return recorded == {"timing": models.timing_model, "luna": models.luna_model}

    def stored_analysis_for_settings(self, root: Path, asset_ids: Sequence[str]) -> str | None:
        """지금 설정의 모델로 끝난 저장 분석이 그 원본들을 덮는가 — 화면 문구용 가벼운 확인(D54).

        원본 재해시·지문 대조는 하지 않는다(초마다 그리는 문구라서). 실제 재사용은 영상을
        고를 때 `cached_selection_analysis`가 전부 확인한다 — 이 답만으로 호출을 건너뛰지 않는다.
        """
        key = ("stored_analysis_for_settings", str(self._workspace(root).root), tuple(asset_ids))
        return read_scope.memo(key, lambda: self._read_stored_analysis(root, asset_ids))

    def _read_stored_analysis(self, root: Path, asset_ids: Sequence[str]) -> str | None:
        store = AnalysisStore(self._workspace(root))
        models = self.settings(root).models
        expected = {"timing": models.timing_model, "luna": models.luna_model}
        default = asdict(RawUnderstandingSettings())
        wanted = set(asset_ids)
        for revision in reversed(store.list_revisions()):
            if revision.status != "complete" or json.loads(revision.models_json) != expected:
                continue
            if json.loads(revision.settings_json) != default:
                continue
            rows = store.list_source_results(revision.revision_id)
            if wanted <= {row.asset_id for row in rows if row.status in ("analyzed", "cached")}:
                return revision.revision_id
        return None

    def analysis_jobs_on_old_prompt(self, root: Path) -> frozenset[str]:
        """이전 분석 기준(안내문 리비전)으로 시작해 이어 갈 수 없는 분석 작업 ID(D58 18b).

        재시도·2단계 재개는 그런 리비전을 0송신으로 거절한다(`_require_current_prompt`) — 화면의
        재개 목록은 이 집합을 빼서 누를 수 없는 항목을 보이지 않는다.
        """
        old = [
            revision
            for revision in AnalysisStore(self._workspace(root)).list_revisions()
            if not _current_prompt(json.loads(revision.settings_json).get("prompt_revision"))
        ]
        keys = {revision.fingerprint for revision in old} | {
            analysis_workflow.semantic_job_fingerprint(revision.revision_id) for revision in old
        }
        kinds = (ANALYSIS_JOB_KIND, ANALYSIS_SEMANTIC_JOB_KIND)
        return frozenset(
            job.job_id
            for job in self.recent_jobs(root, kinds, limit=None)
            if job.input_fingerprint in keys
        )

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
        """분석 읽기 모델 — 리비전과 원본별 결과(상태·창 수·실행 모드·오류).

        읽기 범위(그리기 한 번) 안에서는 리비전 목록과 리비전마다의 결과를 한 번만 읽는다(D63) —
        리비전을 주든(지금 것) 안 주든 같은 리비전이면 같은 답이다. 값은 얼린 데이터클래스다."""
        workspace = self._workspace(root)
        store = AnalysisStore(workspace)
        key_root = str(workspace.root)
        revisions = read_scope.memo(("analysis_revisions", key_root), store.list_revisions)
        revision = None
        if revision_id is not None:
            revision = next((item for item in revisions if item.revision_id == revision_id), None)
        elif revisions:
            revision = revisions[-1]
        if revision is None:
            return None
        key = ("analysis_overview", key_root, revision)
        return read_scope.memo(key, lambda: self._read_analysis_overview(store, revision))

    def _read_analysis_overview(
        self, store: AnalysisStore, revision: AnalysisRevision
    ) -> AnalysisSummary:
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
        _reserve_probe_room(len(order.entries))
        ordered = sorted(order.entries, key=lambda item: item.ordinal)
        _warm_source_probes(workspace, (entry.asset_id for entry in ordered))
        for entry in ordered:
            row = rows.get(entry.asset_id)
            if row is None and not use_current_order:
                raise AnalysisError(
                    f"분석 결과 행이 없다: {entry.asset_id} — 분석을 다시 실행하세요"
                )
            spans = self._analysis_intervals(workspace, row, entry.asset_id)
            if spans is not None:  # None은 VFR — 계획이 덮지 않고 까닭을 남긴다(D68)
                intervals[entry.asset_id] = spans
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
            oscompat.replace(temp, target)
        for registered in self.list_media(root, kind="preview"):
            if registered.relative_path == relative:
                return ReviewVttRegistration(registered, result.provenance)
        registration = self.register_media(root, target, "preview")
        return ReviewVttRegistration(registration, result.provenance)

    @staticmethod
    def _preview_tool_line() -> str:
        """미리보기 키에 쓰는 도구 판 — 한 번 재고 가둔다."""
        if "ffmpeg" not in _FFMPEG_VERSION_LINE:
            _FFMPEG_VERSION_LINE["ffmpeg"] = preprocess.ffmpeg_provenance().splitlines()[0]
        return _FFMPEG_VERSION_LINE["ffmpeg"]

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
        # 상태로 찾는다 — 최근 50개 창으로 거르면 짧은 준비 작업이 쌓인 뒤 오래 도는 원본 전체
        # 인코딩이 창 밖으로 밀려 「준비 중」이 「미리보기 낡음」으로 보였다(W-24 J, 실측 0024).
        active = self.active_jobs(root, (VISUAL_PREPARE_JOB_KIND,))
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

    def start_representative_thumbnails(self, root: Path) -> JobHandle | None:
        """영상마다 첫 컷의 썸네일이 없으면 그것만 만드는 작업 하나를 건다(W-24 세션 I).

        라이브러리 열기·화면 갱신의 정리 고리가 부른다. 무료 로컬이고 원본 전체 미리보기는 만들지
        않는다. 없을 것이 없거나, 이 일을 하는 작업이 이미 돌거나, 남은 썸네일을 이번 실행에서
        이미 한 번 시도했으면 None — 계속 실패하는 원본을 그리기마다 다시 걸지 않는다(서버를 다시
        띄우면 한 번 더 시도한다). 시도는 작업이 그 썸네일에서 멈추면(성공·실패·취소) 적는다 —
        사람이 작업을 취소하면 다음 고리가 다시 걸지 않는다. 「라이브러리 닫기」는
        `forget_thumbnail_tries`로 그 작업공간의 기록을 지우고 세대를 올려, 취소된 작업이 늦게
        적는 시도도 버린다 — 다시 열면 끝내지 못한 것을 다시 건다. 종류는 미리보기 준비라
        「라이브러리 닫기」가 취소하고, 진행이 작업대를 다시 그린다. 분석 승인과는 무관하다(다른
        스레드의 작업이다).
        """
        facade = self.review_workspace_facade(root)
        head = _review_head(facade)
        if head is None:
            return None
        workspace = self._workspace(root)
        key = root.expanduser().resolve()
        missing = tuple(
            item
            for item in visual_review.representative_thumbs_missing(workspace, head)
            if (key, item[1]) not in self._thumb_tries
        )
        if not missing:
            return None

        epoch = self._thumb_epochs.get(key, 0)

        def attempted(thumb_key: str) -> None:
            with self._lock:
                if self._thumb_epochs.get(key, 0) == epoch:
                    self._thumb_tries.add((key, thumb_key))

        def body(context: JobContext) -> None:
            visual_review.run_representative_thumbnails(
                context, workspace, head, missing, attempted
            )

        fingerprint = visual_review.representative_thumbs_fingerprint([k for _, k in missing])
        with self._lock:  # 확인과 제출을 한 덩어리로 — 두 고리가 겹쳐 작업 둘을 걸지 않는다
            if any(
                job.input_fingerprint.startswith(visual_review.REPRESENTATIVE_THUMBS_PREFIX)
                for job in self.active_jobs(root, (VISUAL_PREPARE_JOB_KIND,))
            ):
                return None
            return self.submit_job(
                root, VISUAL_PREPARE_JOB_KIND, fingerprint, body, total=len(missing)
            )

    def forget_thumbnail_tries(self, root: Path) -> None:
        """닫은 작업공간의 첫 컷 썸네일 시도 기록을 지운다 — 다시 열면 다시 건다(W-24 세션 I).

        세대를 올려, 닫기가 취소한 작업이 그 뒤에 적는 시도도 버린다.
        """
        key = root.expanduser().resolve()
        with self._lock:
            self._thumb_epochs[key] = self._thumb_epochs.get(key, 0) + 1
            self._thumb_tries = {item for item in self._thumb_tries if item[0] != key}

    def start_visual_source_preparation(
        self,
        root: Path,
        *,
        source_id: str,
        audio_selections: Mapping[str, int | None] | None = None,
        expected_revision_id: str | None = None,
    ) -> JobHandle:
        """고른 영상 하나의 미리보기를 채우는 작업 — 쪽과 무관하게 그 영상의 컷 전부(D64).

        같은 (계획, 영상, 소리 선택)의 활성 작업이 있으면 그 작업에 다시 붙는다. 소리 선택·리비전
        묶음·없는 것만 만드는 규칙은 쪽 준비(`start_visual_page_preparation`)와 같다.
        """
        workspace = self._workspace(root)
        facade = self.review_workspace_facade(root)
        head = facade.load_head(None).revision
        visual_review.require_head_revision(head, expected_revision_id)
        if head.clock_for(source_id) is None:
            raise visual_review.VisualReviewError(f"검토 머리에 원본 {source_id!r}이(가) 없다")
        selections = visual_review.validated_audio_selections(audio_selections)
        fingerprint = visual_review.source_preparation_fingerprint(
            head.base_plan_id, source_id, visual_review.audio_selection_material(selections)
        )
        base_plan_id = head.base_plan_id

        def body(context: JobContext) -> None:
            visual_review.run_source_preparation(
                context,
                workspace,
                facade,
                source_id=source_id,
                base_plan_id=base_plan_id,
                audio_selections=selections,
                expected_revision_id=expected_revision_id,
            )

        return self.submit_job(root, VISUAL_PREPARE_JOB_KIND, fingerprint, body)

    def visual_source_preparation_missing(
        self,
        root: Path,
        *,
        source_id: str,
        audio_selections: Mapping[str, int | None] | None = None,
    ) -> int:
        """고른 영상에서 준비가 만들 것의 수(읽기만).

        0이면 「미리보기 다시 준비」가 할 일이 없다 — 작업을 걸지 않는다(D64).
        """
        return visual_review.source_preparation_missing(
            self._workspace(root),
            self.review_workspace_facade(root),
            source_id=source_id,
            audio_selections=audio_selections,
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

    def visual_mark_range(
        self,
        root: Path,
        *,
        base_plan_id: str | None = None,
        expected_revision_id: str,
        source_id: str,
        mark: str,
        start_pts: int | None = None,
        end_pts: int | None = None,
        start_seconds: Fraction | int | None = None,
        end_seconds: Fraction | int | None = None,
    ) -> visual_review.VisualRangeMark:
        """시작·끝으로 범위를 표시한다(D100 §4) — 추천 · 보통 · 비추천 · 표시 지우기.

        `mark`는 `recommended`·`normal`·`not_recommended`·`clear`다. 양 끝은 각각 정수 PTS(격자
        위여야 한다) 또는 원점 기준 초(`Fraction`·int — 원본 시계로 가장 가까운 프레임에 스냅,
        한가운데면 앞장) 가운데 하나다. 화면이 본 리비전(`expected_revision_id`)이 지금 머리가
        아니면 `StaleRevisionError`다. 나누기·합치기·표시가 한 편집이라 리비전 하나·되돌리기
        한 걸음이고, 원본·전사·다른 원본의 조각은 그대로다. 비추천은 사람이 확정한 제외라
        렌더 계획이 그 범위를 빼고 보고서에 `human_excluded`로 남긴다(D100 §5a).
        """
        return visual_review.mark_range(
            self.review_workspace_facade(root),
            base_plan_id=base_plan_id,
            expected_revision_id=expected_revision_id,
            source_id=source_id,
            mark=mark,
            start_pts=start_pts,
            end_pts=end_pts,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )

    def visual_kept_ranges(
        self,
        root: Path,
        revision: ReviewRevision,
        source_id: str,
        spans: Sequence[Mapping[str, Any]] | None,
    ) -> tuple[render_plan.KeptRange, ...]:
        """작업대 레일의 「내보낼 부분」(W-46b, D100 §7b) — 초안 렌더와 같은 순수 계획으로 잰다.

        재료는 화면이 이미 읽은 값뿐이다: 검토 머리의 그 원본 조각·시계와 등급 구간 문서
        (`selection_suitability`의 행 — None이면 분석 전, 계획이 `unanalyzed`로 뺀다). 원본 파일은
        조사하지 않는다 — map 원본(초안이 통째로 뺀다, D68)인지는 계획이 남긴 표지로 본다.
        돌려주는 것은 `render_plan.KeptRange` 묶음이다. 재료가 어긋나면 `RenderPlanError`다.
        """
        from vlogsrt.project.raw.review_workspace import human_decided

        clock = revision.clock_for(source_id)
        if clock is None:
            return ()
        rows = sorted(
            (s for s in revision.segments if s.source_id == source_id), key=lambda s: s.start_pts
        )
        mapped = visual_review.marked_mapped(self._workspace(root).root, clock.source_sha256)
        source = render_plan.RenderSource(
            source_id=source_id,
            name=source_id,
            clock=render_plan.frame_clock(clock),
            width=0,
            height=0,
            grid=render_plan.GRID_MAP if mapped else render_plan.GRID_UNIFORM,
            tiers=None if spans is None else tuple(_tier_piece(row) for row in spans),
            segments=tuple(
                render_plan.segment_mark(row, clock, decided=human_decided(row)) for row in rows
            ),
        )
        return render_plan.kept_ranges(source)

    @staticmethod
    def visual_range_mark(segment: ReviewSegment) -> str | None:
        """조각에 실린 사람 표시(D100 §4의 대응을 거꾸로) — 레일이 자동 등급과 따로 그린다(W-46b).

        사람이 확정한 제외는 비추천, 추천·보통 덮기는 그 이름이다. 미검토 자동 제외는 사람
        표시가 아니다(렌더 계획과 같은 판단). 표시가 없으면 None이다."""
        if segment.selection == SELECTION_EXCLUDED and not is_unreviewed_auto_exclusion(segment):
            return RANGE_NOT_RECOMMENDED
        if segment.override_recommendation in (RANGE_RECOMMENDED, RANGE_NORMAL):
            return str(segment.override_recommendation)
        return None

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
        resolved = self._workspace(root).root
        # 그리기 한 번 안에서는 작업공간마다 하나다(D63) — 파사드는 상태를 들지 않는다.
        return read_scope.memo(
            ("review_workspace_facade", str(resolved)), lambda: ReviewWorkspaceFacade(resolved)
        )

    # ── A4 내부 — 선택·지문·공급자·구간 조립 ────────────────────

    def _analysis_selection(
        self,
        root: Path,
        order: GlobalOrderRevision,
        asset_ids: Sequence[str] | None,
        *,
        trust_stat: bool = False,
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
        return {
            asset_id: _verified_asset(workspace, asset_id, trust_stat=trust_stat)
            for asset_id in chosen
        }

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
        *,
        timing_client: object | None = None,
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
                root,
                asr_approvals,
                (sender, cfg, timing_client),
                workflow,
                understanding,
                on,
                assets,
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
        transport: tuple[object, object, object],
        workflow: RawWorkflowConfig,
        understanding: RawUnderstandingSettings,
        on: date | None,
        assets: Mapping[str, SourceAsset],
    ) -> analysis_workflow.AnalysisProviders:
        """1단계(전사) 공급자 — 원본별 `AsrPhaseApproval`의 빠른 실패 문.

        승인은 그 원본의 시간 엔진 보기(D92)와 대조한다 — whisper 원본은 저장된 설정 그대로다.
        Gemini로 듣는 원본이 하나라도 있는데 그 키가 없으면 유료 호출 전에 닫는다."""
        sender, cfg, timing_client = transport
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
        for asset_id, asset in assets.items():
            probe = preprocess.probe_raw_media_memo(workspace.source_media_path(asset))
            if not probe.audio:
                continue  # 무성 원본 — 승인할 전사가 없다(ASR 작업을 만들지 않는다)
            view = timing_engine.source_workflow(workspace, asset_id, workflow)
            self._require_live_asr_source(
                asset, checked.get(asset_id), view, understanding, on, timing_client
            )
        return analysis_workflow.AnalysisProviders(
            origin="live",
            phase="asr",
            live_sender=sender,
            live_cfg=cfg,
            timing_client=timing_client,
            asr_approvals=checked,
            on=on,
        )

    @staticmethod
    def _require_live_asr_source(
        asset: SourceAsset,
        approval: AsrPhaseApproval | None,
        workflow: RawWorkflowConfig,
        understanding: RawUnderstandingSettings,
        on: date | None,
        timing_client: object | None,
    ) -> None:
        """원본 하나의 1단계 시작 문 — 승인이 본 원본·엔진·설정 지문과 수명주기·키."""
        from vlogsrt.project.raw.capabilities import ROLE_TIMING, preflight_online_transcription

        name = asset.source_name
        if approval is None:
            raise AnalysisError(
                f"1단계(전사) 승인이 없는 원본이다: {name} — 새 추정과 새 승인을 받아라(§14.3)"
            )
        if approval.source_sha256 != (asset.content_sha256 or ""):
            raise AnalysisError(
                f"승인이 본 원본과 지금의 원본이 다르다: {name} — 새 추정과 새 승인을 받아라(§14.3)"
            )
        if approval.timing_model != workflow.models.timing_model:
            raise AnalysisError(
                f"승인이 본 ASR 모델과 설정 모델이 다르다: {name}"
                f" ({approval.timing_model!r}/{workflow.models.timing_model!r})"
            )
        if approval.settings_fingerprint != asr_settings_fingerprint(understanding, workflow):
            raise AnalysisError(
                f"승인이 본 설정 지문과 지금의 설정 지문이 다르다: {name}"
                " — 새 추정과 새 승인을 받아라(§14.3)"
            )
        try:
            preflight_online_transcription(workflow, on or date.today(), roles=(ROLE_TIMING,))
        except Exception as error:  # noqa: BLE001 - 시작 문의 수명주기 실패는 AnalysisError로
            raise AnalysisError(
                f"1단계 실제 호출의 수명주기 문이 닫혔다: {name} — {error}"
            ) from error
        engine = timing_engine_for_model(workflow.models.timing_model)
        if engine.key != TIMING_ENGINE_WHISPER and timing_client is None:
            raise AnalysisError(
                f"{name}은(는) {engine.label}로 듣는다 — .env에 {ANALYSIS_GEMINI_KEY_ENV}(결제 연결"
                " 키)를 넣고 앱을 다시 켜세요. 유료 호출은 시작하지 않았습니다"
            )
        if approval.text_model in GEMINI_TRANSCRIBE_PROFILES and timing_client is None:
            raise AnalysisError(
                f"{name}의 자막 글자는 Gemini 3.5 Transcribe로 받는다 — .env에"
                f" {ANALYSIS_GEMINI_KEY_ENV}(결제 연결 키)를 넣고 앱을 다시 켜세요. 유료 호출은"
                " 시작하지 않았습니다(W-35b)"
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
                    f"2단계(의미) 승인이 없는 원본이다: {asset.source_name} — 새 1단계"
                    " 승인을 받아라(§14.3)"
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
            heard_with = timing_engine.source_workflow(workspace, asset_id, workflow)
            if isinstance(approval, OnlineAnalysisApproval) and (
                approval.asr_model != heard_with.models.timing_model
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
                    " — 새 1단계 승인을 받아라(§14.3)"
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
        """원본별 결속 지문 — 고친 자막·미디어 참조·다시 듣기 차례가 분석 지문을 바꾼다.

        다시 듣기 차례(D59 19b)는 0이 아닐 때만 셋째 칸으로 붙는다 — 0이면 결속은 예전 두 칸
        그대로라 끝난 분석의 지문이 바뀌지 않는다. 「다른 귀로 교차 확인」(D59 19d)이 켜져 있으면
        끝에 꼬리표가 붙고, 자막 지문은 두 번째 귀가 얹은 글자를 뺀 것이다 — 실행 전에 묶은 결속이
        실행이 글자를 얹은 뒤에도 같아 끝난 분석을 다시 찾는다. 꺼져 있으면 19d 이전 바이트
        그대로다.
        """
        from vlogsrt.project.raw import asr_epoch, cross_check, media_refs, source_transcript

        workspace = self._workspace(root)
        bindings: dict[str, list[str]] = {}
        for asset_id in assets:
            cross_tag = cross_check.source_tag(workspace, asset_id)  # 글자 귀마다(W-35b)
            row = [
                source_transcript.binding_fingerprint(workspace, asset_id, cross_tag is not None),
                media_refs.fingerprint(workspace, asset_id),
            ]
            tag = asr_epoch.binding_tag(asr_epoch.current(workspace, asset_id))
            if tag is not None:
                row.append(tag)
            engine_tag = timing_engine.binding_tag(workspace, asset_id)
            if engine_tag is not None:
                row.append(engine_tag)  # whisper 원본은 붙지 않는다 — 결속이 바이트 그대로(D92)
            bindings[asset_id] = row if cross_tag is None else [*row, cross_tag]
        return bindings

    def _analysis_intervals(
        self,
        workspace: RawWorkspace,
        row: AnalysisSourceResult | None,
        asset_id: str,
    ) -> list[PlannedInterval] | None:
        """원본 하나의 계획 구간 — 분석됨은 기록 그대로, 아니면 전체 보존 한 조각.

        격자를 증명하지 못한 원본(VFR)은 None이다 — 구간이 없으니 계획이 덮지 않고
        까닭을 남긴다(D68).
        """
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
        if not facts.clock.uniform_grid:
            return None
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
        report = self._tier_report(workspace, row) if row.document_path else None
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


_SYNC_EXPORT_BUCKETS = ("flagged", "amber", "green", "skip", "stale", "missing")


def _sync_export_bucket(document: Mapping[str, Any] | None) -> str:
    """보내기 표지의 칸 — 지금 빨강만 `flagged`, 나머지는 왜 안 달았는지의 이름이다."""
    if document is None:
        return "missing"
    if document.get("stale"):
        return "stale"
    verdict = str(document.get("verdict"))
    return {"red": "flagged", "amber": "amber", "green": "green"}.get(verdict, "skip")


@dataclass(frozen=True)
class _TimingRead:
    """지금 보고서에서 읽은 등급 재료 — `_tier_timing`이 값(`TimingSignals`)으로 바꾼다(D60·D65).

    줄 수(`suspect_lines`/`lines`)는 초록 보고서에서만 센다 — 아니면 둘 다 0이다(「자막 확인됨」은
    초록에서만 참이라 셀 까닭이 없고, 원본 자막을 한 번 더 읽지 않는다).
    """

    level: str
    no_speech: frozenset[tuple[str, Fraction, Fraction]]
    suspect_lines: int = 0
    lines: int = 0


def _timing_read(workspace: RawWorkspace, asset_id: str) -> _TimingRead | None:
    """지금(엄격한 `is_current`) 보고서의 판정 · 「말소리를 못 찾음」 큐 · 초록이면 확인 필요 줄 몫.

    없거나 낡았으면 None(모름)이다. 읽기만 한다 — 못 읽은 오류는 부른 쪽이 모름으로 바꾸고 경고한다.
    """
    from vlogsrt.project import sync_check
    from vlogsrt.project.raw import sync_report

    report = sync_report.current_report(workspace, asset_id, strict=True)
    if report is None or report.get("stale"):
        return None
    level = str(report.get("verdict"))
    judged = level in (sync_check.LEVEL_RED, sync_check.LEVEL_AMBER, sync_check.LEVEL_GREEN)
    cues = (report.get("cues") or ()) if judged else ()
    flagged = frozenset(
        (str(cue["id"]), Fraction(str(cue["start"])), Fraction(str(cue["end"])))
        for cue in cues
        if sync_check.FLAG_NO_SPEECH in (cue.get("flags") or ())
    )
    if level != sync_check.LEVEL_GREEN:
        return _TimingRead(level, flagged)
    suspect_lines, lines = _suspect_share(workspace, asset_id, report)
    return _TimingRead(level, flagged, suspect_lines, lines)


def _suspect_share(
    workspace: RawWorkspace, asset_id: str, report: Mapping[str, Any]
) -> tuple[int, int]:
    """(확인 필요 줄 수, 줄 수) — 보고서가 잰 바로 그 원본 자막 리비전에서, 화면의 「확인 필요」와
    같은 부르기(`subtitle_cue_notes`)로 센다(D59 19e · D65). 리비전이 다르면 (0, 0) — 확인 안 됨.
    """
    from vlogsrt.project.raw import source_transcript

    transcript = source_transcript.load(workspace, asset_id)
    revision = report["source"]["transcript_revision"]
    if transcript is None or str(transcript.get("revision")) != revision:
        return 0, 0
    notes = RawWorkspaceService.subtitle_cue_notes(transcript, report)
    flagged = sum(1 for note in notes.values() if note.get("suspects"))
    return flagged, len(transcript.get("utterances") or ())


def _truth_lines(
    transcript: Mapping[str, Any] | None, revision: str, seconds: Fraction
) -> tuple[Any, ...]:
    """시각 정답을 찍을 수 있는 지금 리비전의 줄 — 자막이 없거나 화면이 본 리비전이 아니거나 시각이
    원본 길이 밖이면 거절한다(아무것도 쓰지 않는다)."""
    from vlogsrt.project.raw import source_transcript, timing_truth

    if transcript is None:
        raise RawWorkspaceError(TRUTH_NO_TRANSCRIPT)
    if str(transcript["revision"]) != revision:
        raise RawWorkspaceError(TRUTH_REVISION_CHANGED)
    if not 0 <= seconds <= source_transcript.rational(transcript["duration"]):
        raise RawWorkspaceError(TRUTH_OUT_OF_RANGE)
    return timing_truth.lines_of(transcript)


def _truth_strata(
    workspace: RawWorkspace,
    asset_id: str,
    transcript: Mapping[str, Any],
    lines: Sequence[Any],
    sync_view: Mapping[str, Any] | None,
) -> dict[str, str]:
    """시각 정답의 층(W-25b) — 1단계 기록의 청크 시작·PCM 원점과 지금 확인의 「못 찾음」 줄로.

    1단계 기록은 원본 자막이 묶인 원본 해시로 찾는다(읽기 범위가 그리기 한 번에 한 번만 읽는다).
    기록이 없으면 청크 층이 비고 PCM 원점은 0이다 — 층을 지어내지 않는다.
    """
    from vlogsrt.project.raw import sync_report, timing_truth

    stage1 = sync_report.stage1_audio(
        workspace.root, asset_id, str(transcript.get("source_sha256") or "")
    )
    origin = Fraction(0)
    starts: tuple[Fraction, ...] = ()
    if stage1 is not None and not stage1.absent:
        origin = stage1.origin - stage1.presentation_origin
        starts = tuple(origin + start for start in stage1.chunk_starts)
    return timing_truth.stratify(
        lines, chunk_starts=starts, audio_start=origin, no_speech=_current_no_speech(sync_view)
    )


def _ear_disagreed(transcript: Mapping[str, Any]) -> frozenset[str]:
    """다른 귀가 조각을 다르게 들어 D37 조각 게이트가 거절한 줄(W-29a, D77) — 합친 줄은 `+`로 이은
    이유 가운데 하나라도. Gemini 원본에서 다른 귀 글자를 후보로만 둔 줄도 같다(W-31e, D92) — 글자를
    바꾸지 않았으니 사람이 들어 볼 줄이다."""
    from vlogsrt.project.raw.source_text import REASON_CANDIDATE
    from vlogsrt.retext import REASON_CHUNK

    reasons = {REASON_CHUNK, REASON_CANDIDATE}
    return frozenset(
        str(row["utterance_id"])
        for row in transcript.get("utterances", [])
        if reasons.intersection(str(row.get("retext_reason") or "").split("+"))
    )


def _current_no_speech(sync_view: Mapping[str, Any] | None) -> frozenset[str]:
    """지금 자막 리비전의 확인 보고서가 「말소리를 못 찾음」으로 짚은 큐 아이디(D55) — 값으로만.

    보고서가 없거나 낡았으면(다른 리비전을 쟀다) 빈 집합이다 — 표시를 지어내지 않는다.
    """
    from vlogsrt.project import sync_check

    if not sync_view or sync_view.get("stale"):
        return frozenset()
    return frozenset(
        str(cue.get("id"))
        for cue in sync_view.get("cues", [])
        if sync_check.FLAG_NO_SPEECH in (cue.get("flags") or ())
    )


def _sync_fingerprint(pending: Mapping[str, str]) -> str:
    """요청마다 다른 지문 — 끝나 정산을 기다리는 작업이나 running으로 남은 줄에 붙지 않게(D55)."""
    body = json.dumps([sorted(pending.items()), uuid.uuid4().hex], ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _refill_bound(channels: int) -> int:
    """빈칸 재전사 조각의 표본 상한 — 1단계 청크 계획과 같은 두 한도(초·바이트) 중 작은 쪽."""
    settings = RawUnderstandingSettings()
    rate = analysis_workflow.stage1.PCM_SAMPLE_RATE
    width = analysis_workflow.stage1.PCM_SAMPLE_WIDTH * max(1, channels)
    return min(int(settings.asr_chunk_max_seconds * rate), settings.asr_chunk_max_bytes // width)


def _refill_record(result: Any, outcome: Any, refusal: str | None) -> dict[str, Any]:
    """빈칸 재전사 실행 기록 — 넣은 줄·겹쳐 넣지 않은 줄·범위 밖·실패 조각·글자 경로(규칙 9)."""
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "answered": [list(piece) for piece in result.answered],
        "failed": list(result.failed),
        "rows": len(result.rows),
        "inserted": [] if outcome is None else list(outcome.inserted),
        "overlapped": [] if outcome is None else list(outcome.overlapped),
        "out_of_range": [] if outcome is None else list(outcome.out_of_range),
        "refused": refusal,
        "asr": analysis_workflow.stage1.asr_evidence_json(result.evidence),
        "asr_text": result.text_record,
    }


def _warn_refill(asset_id: str, outcome: Any, result: Any) -> None:
    """넣지 않은 줄·실패한 조각은 조용하지 않다(규칙 9) — 수와 기록 자리를 경고로 남긴다."""
    log.out(
        f"빈칸 재전사({asset_id}): 조각 {len(result.answered) + len(result.failed)}개 ·"
        f" 받은 줄 {len(result.rows)}개 · 넣은 줄 {len(outcome.inserted)}개"
    )
    if outcome.overlapped or outcome.out_of_range:
        log.warn(
            f"빈칸 재전사({asset_id}): 기존 자막과 겹친 줄 {len(outcome.overlapped)}개 · 원본 범위"
            f" 밖 줄 {len(outcome.out_of_range)}개는 넣지 않았습니다 — 실행 기록에 글자째"
            " 남겼습니다"
        )
    if result.failed:
        log.warn(
            f"빈칸 재전사({asset_id}): 조각 {len(result.failed)}개를 받지 못했습니다 — 다음 승인이"
            " 그 조각을 다시 셉니다"
        )


@dataclass(frozen=True, slots=True)
class SnapCue:
    """먼저 들어 볼 줄 하나(D59 19h) — 시각은 원본 자막의 공통 시계(정확 유리수)다."""

    cue_id: str
    text: str
    old_start: Fraction
    old_end: Fraction
    new_start: Fraction
    new_end: Fraction
    status: str
    """경계 맞춤의 판정 — moved · unchanged · blocked · no_speech(D59 19g)."""
    shift_only: bool
    """경계는 맞추지 않고 센 전체 어긋남만큼만 옮긴다 — 막힘·못 찾음 줄도 그렇게 옮겨진다."""
    reason: str


@dataclass(frozen=True, slots=True)
class SnapPreview:
    """「시각 맞춤 제안」 칸의 값(D59 19h) — 화면은 이 값만 본다(제안 모듈을 모른다).

    `level`·`predicted`는 같은 말소리 칸으로 잰 판정이다 — 맞춘 경계가 그 칸에서 왔으니 예측은
    좋아지게 마련이다(자기 참조, D59 19g (c)). 화면은 「같은 측정으로 잰 예측」이라고 밝힌다.
    """

    sha256: str
    transcript_revision: str
    shift: Fraction
    """센 전체 어긋남(`+` = 자막이 말보다 이르다) — 세지 않았으면 0."""
    shifted: int
    """그 어긋남만큼 옮긴 줄 수 — 원본 길이 밖·옮기지 않은 줄과 겹침·길이 0 줄은 옮기지 않는다."""
    counts: tuple[tuple[str, int], ...]
    """경계 맞춤 판정별 줄 수 — (moved, unchanged, blocked, no_speech) 순서."""
    shift_only: int
    changes: int
    """적용하면 시각이 바뀌는 줄 수(판정과 무관 — 막힘·못 찾음 줄의 밀기 포함)."""
    total: int
    level: str
    predicted: str
    listen: tuple[SnapCue, ...]


@dataclass(frozen=True, slots=True)
class SnapAllRow:
    """일괄 확인의 영상 하나(W-25e) — 적용할 수 있으면 `sha256`, 아니면 `reason`."""

    asset_id: str
    source_name: str
    sha256: str | None
    changed: int
    reason: str | None


@dataclass(frozen=True, slots=True)
class SnapAllPreview:
    """일괄 확인(W-25e) — 영상들과 묶음 토큰(적용할 영상·sha의 해시)."""

    rows: tuple[SnapAllRow, ...]
    token: str

    @property
    def ready(self) -> tuple[SnapAllRow, ...]:
        return tuple(row for row in self.rows if row.sha256 is not None)


@dataclass(frozen=True, slots=True)
class SnapAllApplied:
    asset_id: str
    source_name: str
    changed: int


@dataclass(frozen=True, slots=True)
class SnapAllSkipped:
    asset_id: str
    source_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class SnapAllResult:
    """일괄 적용(W-25e)의 결과 — 적용한 영상과 건너뛴 영상(까닭)."""

    applied: tuple[SnapAllApplied, ...]
    skipped: tuple[SnapAllSkipped, ...]


def _apply_snap_all_token(rows: Sequence[SnapAllRow]) -> str:
    """적용할 영상과 그 제안 sha의 해시 — 확인과 적용 사이에 하나라도 바뀌면 다르다."""
    ready = [[row.asset_id, row.sha256] for row in rows if row.sha256 is not None]
    payload = json.dumps({"schema": "snap-all/v1", "ready": ready}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refill_active_on(db: Any, asset_id: str) -> bool:
    """그 원본의 빈칸 재전사 작업이 활성인가 — 연결 하나 위에서(작업 지문은 `<원본>:<digest>`)."""
    return any(
        fingerprint.startswith(f"{asset_id}:")
        for fingerprint in active_fingerprints_on(db, (REFILL_JOB_KIND,))
    )


def _relisten_refusal(document: Mapping[str, Any]) -> str | None:
    """고친 자막이면 다시 듣기 거절 문구 — 시각 맞춤만 얹은 자막이면 되돌리는 길을 밝힌다
    (D59 19h)."""
    from vlogsrt.project.raw import source_transcript

    if not source_transcript.human_edited(dict(document)):
        return None
    snapped = source_transcript.snap_applied(document)
    undoable = snapped is not None and not snapped.get("from_edited")
    return RELISTEN_SNAPPED_REFUSAL if undoable else RELISTEN_EDITED_REFUSAL


def _relisten_check(db: Any, *, asset_id: str) -> None:
    """차례를 올리는 쓰기 트랜잭션 안의 다시 검사 — 그사이 고친 자막(적용·편집·넣기)이 되었거나
    빈칸 재전사가 걸렸으면 거절한다(같은 연결, D59 19h)."""
    from vlogsrt.project.raw import source_transcript

    document = source_transcript.latest_on(db, asset_id)
    refusal = None if document is None else _relisten_refusal(document)
    if refusal is not None:
        raise RawWorkspaceError(refusal)
    if _refill_active_on(db, asset_id):
        raise RawWorkspaceError(RELISTEN_REFILL_RUNNING_REFUSAL)


def _apply_snap_hold(db: Any, asset_id: str, *, relisten: bool) -> str | None:
    """지금 시각 맞춤을 적용·되돌리지 않는 까닭(연결 하나 위에서) — 다시 듣기 대기(적용만) · 빈칸
    재전사 · 그 원본을 지금 분석(`analyzing`)하는 작업. 셋 다 같은 DB라 쓰기 트랜잭션 안에서도
    읽는다.
    """
    from vlogsrt.project.raw import asr_epoch

    if relisten and asr_epoch.pending_on(db, asset_id):
        return SNAP_RELISTEN_PENDING
    if _refill_active_on(db, asset_id):
        return SNAP_REFILL_RUNNING
    active = active_fingerprints_on(db, (ANALYSIS_JOB_KIND, ANALYSIS_SEMANTIC_JOB_KIND))
    for revision_id, fingerprint in analyzing_revisions_on(db, asset_id):
        if (
            fingerprint in active
            or analysis_workflow.semantic_job_fingerprint(revision_id) in active
        ):
            return SNAP_ANALYSIS_RUNNING
    return None


def _apply_snap_check(db: Any, *, asset_id: str, relisten: bool) -> None:
    """`_apply_snap_hold`의 거절판 — 쓰기 트랜잭션 안의 다시 검사(`_store_edit`의 `guard`)."""
    reason = _apply_snap_hold(db, asset_id, relisten=relisten)
    if reason is not None:
        raise RawWorkspaceError(f"{reason} {SNAP_NOTHING_CHANGED}")


def _apply_snap_unready(report: Mapping[str, Any] | None) -> str:
    """제안을 지금 잴 수 없는 까닭 — 확인을 기다림 · 측정 불가 · 지금 보고서는 있는데 그 말소리 칸을
    쓸 수 없음(저장 칸이 없거나 1단계 PCM·기준이 바뀌었다 — 저절로 다시 재지 않을 수 있어 「다시
    재기」를 말한다)."""
    if report is None or report.get("stale"):
        return SNAP_WAITING
    return SNAP_UNMEASURED if not report.get("pcm") else SNAP_SPEECH_MISSING


def _apply_snap_offset(report: Mapping[str, Any]) -> Any:
    """보고서의 전체 어긋남 추정(`metrics.offset`) — 확인이 쓴 그대로의 값(정확 유리수)."""
    from vlogsrt.project import sync_check

    body = report["metrics"]["offset"]
    seconds = None if body.get("seconds") is None else Fraction(str(body["seconds"]))
    gain = None if body.get("gain") is None else Fraction(str(body["gain"]))
    peak = None if body.get("peak_z") is None else float(body["peak_z"])
    return sync_check.ShiftEstimate(str(body["status"]), seconds, gain, peak)


def _apply_snap_preview(plan: Any, measured: Any) -> SnapPreview:
    """제안 → 칸의 값. 판정 전후는 같은 말소리 칸·같은 기준으로 잰다(보고서의 덧붙은 이유는
    뺀다)."""
    from vlogsrt.project import sync_check, sync_snap
    from vlogsrt.project.config import SYNC_CHECK_SETTINGS

    after = tuple(
        replace(cue, start=item.new_start, end=item.new_end)
        for cue, item in zip(measured.cues, plan.proposals, strict=True)
    )
    levels = [
        sync_check.judge(
            sync_check.measure(cues, measured.speech, SYNC_CHECK_SETTINGS), SYNC_CHECK_SETTINGS
        ).level
        for cues in (measured.cues, after)
    ]
    moving = [item for item in plan.proposals if item.moves]
    return SnapPreview(
        sha256=plan.sha256,
        transcript_revision=plan.source.transcript_revision,
        shift=plan.shift,
        shifted=sum(1 for item in plan.proposals if item.shifted),
        counts=tuple((status, plan.count(status)) for status in sync_snap.STATUSES),
        shift_only=sum(1 for item in moving if item.shift_only),
        changes=len(moving),
        total=len(plan.proposals),
        level=levels[0],
        predicted=levels[1],
        listen=_apply_snap_listen(plan),
    )


def _apply_snap_listen(plan: Any) -> tuple[SnapCue, ...]:
    """먼저 들어 볼 줄 — 시각이 바뀌는 줄 중 **끝이 크게 움직인 것부터**(같으면 시작, 그다음 순서).

    시작 맞춤은 다른 신호(포락선)로 뒷받침되지만 끝 맞춤은 우연보다 낫다는 것이 보이지 않았다
    (D59 19g (c)) — 사람이 끝 경계부터 듣는다. 시각이 바뀌지 않는 줄은 들어 볼 것이 없다.
    """
    moving = [(index, item) for index, item in enumerate(plan.proposals) if item.moves]
    ranked = sorted(
        moving, key=lambda pair: (-abs(pair[1].end_move), -abs(pair[1].start_move), pair[0])
    )
    return tuple(
        SnapCue(
            cue_id=item.cue_id,
            text=item.text,
            old_start=plan.on_common(item.old_start),
            old_end=plan.on_common(item.old_end),
            new_start=plan.on_common(item.new_start),
            new_end=plan.on_common(item.new_end),
            status=item.status,
            shift_only=item.shift_only,
            reason=item.reason,
        )
        for _, item in ranked[:SNAP_LISTEN_FIRST]
    )


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


def _require_current_prompt(understanding: RawUnderstandingSettings) -> None:
    """재시도·2단계 재개할 리비전의 안내문이 지금 코드의 것인가(D58 18b) — 아니면 0송신으로 닫는다.

    안내문·schema는 코드에 있고 리비전에는 그 이름(`prompt_revision`)만 있다. 옛 이름의 리비전을
    이으면 새 안내문을 보내 놓고 옛 이름을 단다 — 그 결과는 지금 설정과 맞지 않아 다시 쓰이지
    않으므로 돈만 든다. 새 분석은 전사를 다시 사지 않는다(전사 열쇠는 안내문을 묶지 않는다).
    """
    if not _current_prompt(understanding.prompt_revision):
        raise AnalysisError(OLD_PROMPT_REFUSAL)


def _current_prompt(prompt_revision: object) -> bool:
    return prompt_revision == DEFAULT_PROMPT_REVISION


_REGRADE_STATE = "다시 매김|"
"""등급 재료만 바뀐 머리(D60)의 할 일 열쇠 머리 — 안내 글을 고르는 표지다."""

_REGRADE_MESSAGE = (
    "자막 시각 확인 결과나 분석 근거가 바뀌어 등급 표시만 다시 답니다 — 사람이 정한"
    " 사용/제외·메모·경계는 그대로입니다."
)

_COVER_MESSAGE = (
    "프레임이 조금 흔들리던 영상을 이제 일정한 프레임에 맞춰 받을 수 있어 검토에 넣습니다 —"
    " 이미 있던 영상의 사용/제외·메모·경계는 그대로입니다."
)
"""기록된 「분석 지원 밖」 원본이 맞춤(D69)으로 덮이게 된 머리의 안내."""


def _tier_state(facade: Any, head: ReviewRevision) -> str | None:
    """등급판 옮기기의 할 일 열쇠 — 할 일이 없으면 None.

    옮기기(판·표지·옛 제안)는 그 자리로만 만든다(머리 리비전이 아니라): 다시 세우면 머리 리비전이
    늘 바뀐다 — 그것을 열쇠로 쓰면 옮겨도 남는 것이 있을 때 앱을 열 때마다가 아니라 화면 갱신마다
    다시 세우기(원본 재해시)가 되풀이된다. 같은 것이 남으면 같은 열쇠라 이번 실행에서는 다시 걸지
    않는다. 판이 같은데 다시 매긴 등급만 다르면(D60 — 자막 시각 확인을 다시 쟀다) 등급만 다시 다는
    할 일이고, 그 열쇠는 머리 리비전과 바뀔 조각으로 만든다 — 다시 세우기가 없어 싸고, 사람이 그
    사이 편집했으면(그래서 한 번 실패했으면) 다음 갱신이 다시 건다.
    """
    if facade.needs_tier_migration(head):
        spans = json.dumps(facade.stale_tier_proposal_spans(head))
        digest = hashlib.sha256(spans.encode("utf-8")).hexdigest()[:16]
        return f"{facade.tier_key()}|표지 {facade.has_tier_marker(head)}|옛 제안 {digest}"
    return _regrade_state(facade, head)


def _regrade_state(facade: Any, head: ReviewRevision) -> str | None:
    """등급 재료만 바뀐 머리의 할 일 열쇠(D60) — 다시 매길 것이 없으면 None.

    다시 매긴 등급을 재다가 증거를 못 읽으면 화면 그리기를 막지 않고 할 일로 넘긴다 — 정리
    작업이 같은 계산을 다시 해 실패를 작업 기록과 「작업공간 정리 실패」 안내로 남긴다(조용히
    넘기지 않는다).
    """
    head_key = f"{_REGRADE_STATE}{facade.tier_key()}|{head.revision_id}"
    try:
        stale = facade.stale_grade_segments(head)
    except (OSError, ValueError, KeyError, TypeError, RawWorkspaceError) as error:
        return f"{head_key}|읽기 실패 {type(error).__name__}"
    if not stale:
        return None
    digest = hashlib.sha256(json.dumps(stale, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"{head_key}|{digest[:16]}"


def _same_sources(
    head: ReviewRevision, order: GlobalOrderRevision, unsupported: Collection[str] = ()
) -> bool:
    """검토 머리와 전체 순서가 같은 영상 묶음인가 — 순서만 다를 수 있다.

    머리의 계획이 덮지 못한 원본(VFR, D68)은 시계 없이 묶음에 든다 — 빼면 미리보기마다 다시
    계획한다(원본 전부 재해시)."""
    sources = {clock.source_id for clock in head.clocks} | set(unsupported)
    return sources == set(_order_ids(order))


def _forget_read_scope() -> None:
    """화면 값이 읽는 서비스 기억(메모리)을 바꿨다 — 열린 읽기 범위의 기억을 버린다(W-24 세션 G).

    단추 길은 정리 고리를 읽기 범위 안에서 잰다. 고리가 '걸지 못함' 표지를 세우거나 지우면 그 뒤의
    그리기가 방금 바뀐 표지를 봐야 한다. 범위가 없으면 할 일이 없다.
    """
    scope = read_scope.current()
    if scope is not None:
        scope.forget()


def _upkeep_failure_note(latest: JobRecord | None) -> str | None:
    """이미 건 할 일의 마지막 결과 — 실패했으면 그 이유, 아니면 None."""
    if latest is not None and latest.status == "failed":
        return f"작업공간 정리 실패: {latest.error} — 앱을 다시 열면 한 번 더 시도합니다."
    return None


def _tier_migration_message(untouched: bool) -> str:
    """등급판 옮기기 안내 — 손대지 않은 머리는 다시 세우고, 손댄 머리는 등급만 다시 단다."""
    if untouched:
        return (
            "등급 기준이 바뀌어(AI 판정 + 자막, 추천 기준 강화) 검토를 다시 세웁니다 — 손대지"
            " 않은 검토라 제외 제안 구간을 조각으로 나눕니다."
        )
    return (
        "등급 기준이 바뀌어(AI 판정 + 자막, 추천 기준 강화) 등급 표시만 다시 답니다 — 사람이"
        " 정한 사용/제외는 그대로이고, 근거가 사라진 옛 제외 제안은 사용으로 되돌립니다."
    )


def _json_fraction(value: Mapping[str, Any]) -> Fraction:
    return Fraction(int(value["num"]), int(value["den"]))


def _tier_piece(row: Mapping[str, Any]) -> render_plan.TierPiece:
    """등급 구간 문서(`TierSpan.document`) → 렌더 계획의 등급 구간 값(작업대 레일, W-46b)."""
    reason = row.get("red_reason")
    return render_plan.TierPiece(
        _json_fraction(row["start"]),
        _json_fraction(row["end"]),
        str(row["tier"]),
        None if reason is None else str(reason),
        bool(row.get("hold")),
        tuple(str(cap.get("code", "")) for cap in row.get("caps") or ()),
    )


def _context_windows(document: Mapping[str, Any], shift: Fraction) -> tuple[Any, ...]:
    """분석 문서의 창 요약(받아들인 판정만) — 공통 시계로 옮긴 코어 구간 · 말 · 화면 · 태그."""
    from vlogsrt.project.raw import context_review as cr

    annotations = {
        str(row.get("window_id")): row
        for row in document.get("annotations", [])
        if isinstance(row, Mapping) and row.get("status") == "accepted"
    }
    found = []
    for window in document.get("windows", {}).get("windows", []):
        row = annotations.get(str(window.get("window_id")))
        if row is None:
            continue
        found.append(
            cr.ContextWindow(
                _json_fraction(window["core_start"]) + shift,
                _json_fraction(window["core_end"]) + shift,
                str(row.get("speech_summary") or ""),
                str(row.get("visual_summary") or ""),
                tuple(str(tag) for tag in row.get("scene_tags") or ()),
            )
        )
    return tuple(found)


def _context_lines(transcript: Mapping[str, Any]) -> tuple[Any, ...]:
    """원본 자막 줄(공통 시계) — 맥락 평가의 재료."""
    from vlogsrt.project.raw import context_review as cr

    return tuple(
        cr.ContextLine(_json_fraction(row["start"]), _json_fraction(row["end"]), str(row["text"]))
        for row in transcript.get("utterances", [])
    )


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


def _file_stamp(path: Path) -> tuple[int, int] | None:
    """파일의 (mtime_ns, 크기) — 없으면 None(아직 쓰지 않은 문서는 정상 상태다)."""
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_mtime_ns, status.st_size


def _record_audio_start(workspace_root: Path, row: AnalysisSourceResult) -> Fraction:
    """PCM 표본 0의 공통 시계 위치 — 기록이 없으면 0(파일 시각과 같다고 본다)."""
    if row.record_path is None:
        return Fraction(0)
    record = json.loads(file_memo.read_text(workspace_root / row.record_path))
    start = record.get("timeline", {}).get("audio", {}).get("common_start")
    return _json_fraction(start) if start else Fraction(0)


def _verified_asset(
    workspace: RawWorkspace, asset_id: str, *, trust_stat: bool = False
) -> SourceAsset:
    """ready 원본의 재검증 — 크기·SHA-256을 지금 다시 읽어 계획의 밑천을 확인한다.

    검증 이력(`content_sha256`)이 없는 원본은 계획 재료가 되지 못한다 — 상태표가
    아니라 내용으로 판단한다(§5.3의 완료 정의를 계획에도 그대로 적용한다).
    `trust_stat`은 승인 견적(`selection_analysis.quote`)만 켠다 — stat이 지난 완전 검증과
    같으면 기록된 해시를 믿는다(D62 A2). 계획·추출·분석 실행·1단계 승인 만들기는 끈 채다.
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
    if source_verify.source_digest(workspace, asset, path, trust_stat=trust_stat) != (
        asset.content_sha256
    ):
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


def _reserve_probe_room(sources: int) -> None:
    """라이브러리를 훑기 전에 원본 조사 기억이 그 원본 전부를 담게 한다(D121)."""
    if sources > 0:
        preprocess.reserve_probe_memo(preprocess.PROBE_MEMO_SLOTS_PER_SOURCE * sources)


def _warm_source_probes(workspace: RawWorkspace, asset_ids: Iterable[str]) -> None:
    """원본 조사 기억을 로컬 미디어 자리 수만큼 함께 채운다(W-76, D135) — 뒤 루프가 기억에 맞는다.

    아무것도 돌려주지도 바꾸지도 않는다 — 검증(해시) 차례·만드는 계획·실패는 뒤따르는 차례 루프가
    예전 그대로 한다. 기억은 성공한 값만 담으므로 여기서 실패한 원본은 루프가 제 자리에서 다시 재
    알린다(실패를 버리는 것이 아니라 알릴 자리를 루프에 남긴다). 취소만은 곧바로 올린다.
    """
    ids = list(asset_ids)
    workers = FOLDER_WORKSPACE.local_media.limit(os.cpu_count())
    if workers < 2 or len(ids) < 2:
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe-warm") as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _warm_one, workspace, asset_id)
            for asset_id in ids
        ]
        for future in futures:
            if isinstance(future.exception(), JobCancelledError):
                for waiting in futures:
                    waiting.cancel()
                future.result()


def _warm_one(workspace: RawWorkspace, asset_id: str) -> None:
    path = workspace.source_media_path(workspace.source_asset(asset_id))
    preprocess.probe_extraction_clock_memo(path, probe=preprocess.probe_raw_media_memo(path))


def _measured_source(workspace: RawWorkspace, asset: SourceAsset) -> _MeasuredSource:
    """원본의 시계를 잰다 — 격자 증명(`uniform_grid`)의 판정은 부른 쪽이 본다(D68)."""
    path = workspace.source_media_path(asset)
    probe = preprocess.probe_raw_media_memo(path)  # `_verified_asset`가 먼저 확인했다(W-45)
    clock = preprocess.probe_extraction_clock_memo(path, probe=probe)
    return _MeasuredSource(clock=clock, exact_end_pts=math.ceil(clock.exact_end / clock.time_base))


def _unsupported_source(
    asset: SourceAsset, source_order: int, clock: preprocess.RawVideoClock
) -> UnsupportedSource:
    """격자를 증명하지 못한 원본의 기록 — 빼지 않고 이름·까닭을 남긴다(D68, 규칙 9)."""
    log.warn(
        f"분할 계획에서 덮지 않았다: {asset.source_relative_path} — {UNSUPPORTED_GRID_REASON}"
        f" ({clock.grid_note})"
    )
    return UnsupportedSource(
        asset_id=asset.asset_id,
        source_order=source_order,
        reason=UNSUPPORTED_GRID_REASON,
        detail=clock.grid_note or "",
    )


@dataclass(slots=True)
class _PlannedSources:
    """계획 한 번의 원본별 재료 — 덮은 원본의 조각·증명·지문 재료와 덮지 못한 원본."""

    entries: list[SegmentPlanEntry]
    coverage: list[SegmentCoverage]
    clocks: dict[str, dict[str, object]]
    unsupported: list[UnsupportedSource]


def _planned_sources(
    workspace: RawWorkspace,
    revision: GlobalOrderRevision,
    planned: Mapping[str, Sequence[PlannedInterval]],
    tile_seconds: Fraction,
) -> _PlannedSources:
    """순서의 원본마다 다시 검증·측정해 조각을 짓는다 — VFR은 덮지 않고 기록한다(D68)."""
    result = _PlannedSources([], [], {}, [])
    _reserve_probe_room(len(revision.entries))
    ordered = sorted(revision.entries, key=lambda item: item.ordinal)
    _warm_source_probes(workspace, (entry.asset_id for entry in ordered))
    for order_entry in ordered:
        asset = _verified_asset(workspace, order_entry.asset_id)
        facts = _measured_source(workspace, asset)
        if not facts.clock.uniform_grid:
            result.unsupported.append(_unsupported_source(asset, order_entry.ordinal, facts.clock))
            continue
        visual_review.note_grid_conform(workspace.root, asset.content_sha256 or "", facts.clock)
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
        result.entries.extend(spans)
        result.coverage.append(proof)
        result.clocks[asset.asset_id] = _plan_fingerprint_inputs(asset, facts, spans)
    if result.unsupported and not result.coverage:
        names = ", ".join(
            workspace.source_asset(u.asset_id).source_name for u in result.unsupported
        )
        raise SegmentationError(
            f"계획할 수 있는 원본이 없다 — 모두 {UNSUPPORTED_GRID_REASON}: {names[:300]}."
            " 일정한 프레임(예: 30fps)으로 다시 저장해 불러오세요"
        )
    return result


def _plan_fingerprint_inputs(
    asset: SourceAsset, facts: _MeasuredSource, spans: Sequence[SegmentPlanEntry]
) -> dict[str, object]:
    """계획 지문의 원본별 재료 — 내용 해시·시계·선택된 경계 틱(§14.2).

    map 원본(D70)만 맞춤 규칙 판(`grid_conform`)을 더 싣는다 — 같은 원본이라도 규칙이 바뀌면
    슬롯이 보이는 프레임(추출물)이 바뀐다. 균일·snap 원본은 칸이 없다(지문 바이트 그대로).
    """
    clock = facts.clock
    material: dict[str, object] = {
        "content_sha256": asset.content_sha256,
        "time_base": f"{clock.time_base.numerator}/{clock.time_base.denominator}",
        "origin_pts": clock.first_pts,
        "frame_count": clock.frame_count,
        "frame_duration": str(clock.frame_duration),
        "boundaries": [span.core_start_pts for span in spans] + [spans[-1].core_end_pts],
    }
    if clock.conform_kind == "map":
        material["grid_conform"] = preprocess.GRID_CONFORM_TAG
    return material


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
