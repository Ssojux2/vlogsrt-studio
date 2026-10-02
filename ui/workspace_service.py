"""브라우저 작업공간 UI의 서버 측 조립 — v2 계획의 정확 경로 예외 파일.

이 파일만 `vlogsrt.project.raw.service`를 가져올 수 있다(docs/PROJECT_CONTRACTS.md §1
둘째 예외). HTTP 경계도 여기 산다: Host/Origin 가드가 Gradio 엔드포인트와 원본
엔드포인트(`/media/{id}`)를 함께 지키고(§4.4), 미디어는 **등록된 ID로만** Range로
제공된다. 화면 상태는 `gr.State`가 아니라 서버(DB)가 소유한다(§14.1) — 새로고침은
다시 붙고, 서버 시작 때만 작업 복구가 일어난다.

레거시 단일 영상 화면(`ui/service.py`)은 그대로다. 이 모듈은 그것을 건드리지 않는다.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import threading
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

# 로컬 전용 UI다 — gradio의 사용 보고가 네트워크를 만지지 않게 수입 전에 끈다.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402

from ui import ui_facade  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402

STATE_ENV = "VLOGSRT_RAW_UI_STATE"
"""서버 상태 파일 경로 재정의 — 루트의 합성 작업공간 시험이 쓴다(사용자 .env 불가)."""

MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""Origin/Referer 검사를 강제하는 메서드 — Gradio 변경·업로드 엔드포인트가 여기 온다."""

STREAM_CHUNK_BYTES = 262144

REVIEW_EXPORT_JOB_KIND = "review_export"
"""확정 내보내기 작업 종류 — 제출 때의 검토 리비전·선택에 묶인 본문을 가진다."""


def _parse_intervals(text: str) -> dict[str, list[raw_service.PlannedInterval]] | None:
    """구간 계획 입력 해석 — 각 줄 `자산ID 시작 끝`(초, 유리수). 빈 입력은 None.

    모양이 어긋난 줄은 안내와 함께 거부한다 — 조용히 무시하면 전체 덮음 계약을
    어기는 계획이 나간다(§6.1).
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    intervals: dict[str, list[raw_service.PlannedInterval]] = {}
    for number, line in enumerate(cleaned.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 3:
            raise gr.Error(f"{number}번째 줄이 `자산ID 시작 끝` 모양이 아니다: {stripped!r}")
        asset_id, start_text, end_text = parts
        try:
            span = raw_service.PlannedInterval(start=Fraction(start_text), end=Fraction(end_text))
        except (ValueError, ZeroDivisionError) as error:
            raise gr.Error(f"{number}번째 줄의 시각이 유리수가 아니다: {stripped!r}") from error
        intervals.setdefault(asset_id, []).append(span)
    return intervals


def _capture_cell(inspection: raw_service.SourceInspection) -> str:
    """촬영 시각 칸 — 유효하면 시각, 무효면 '무효', 아니면 '미조사'(§6.2).

    조사 계약은 가져오기 계층의 것이다 — 여기는 값을 세 칸으로만 옮긴다.
    """
    if inspection.capture_time is not None:
        return inspection.capture_time
    if inspection.capture_time_status == "invalid":
        return "무효(선언이 있으나 못 믿는다)"
    return "미조사"


def _coverage_count(snapshot: raw_service.SegmentPlanSnapshot, source_order: int) -> int:
    """덮음 증명 표의 조각 수 — 원본 순번에 맞는 coverage를 찾아 센다."""
    for proof in snapshot.coverage:
        if proof.source_order == source_order:
            return proof.segment_count
    return -1


@dataclass(frozen=True)
class _AsrApprovalRecord:
    """1단계 브라우저 승인 기록 — 사람이 본 견적 표를 서버가 그대로 간직한다.

    브라우저가 보낸 값은 쓰지 않는다(gr.State은 클라이언트 소유다). 견적을
    그린 순간 서버가 잰 측정값과 지문을 여기 남기고, 시작할 때 다시 잰 값과
    같은지 비교한다 — 다르면 승인은 낡았고 0송신으로 거부된다.
    """

    signature: str
    estimates: tuple[raw_service.AnalysisEstimate, ...]
    digest: str


@dataclass(frozen=True)
class _SemanticApprovalRecord:
    """2단계 브라우저 승인 기록 — 1단계 실측 지문 표를 리비전에 묶어 남긴다."""

    signature: str
    revision_id: str
    estimates: tuple[raw_service.AnalysisSemanticEstimate, ...]
    digest: str


CSS = """
#exec-log textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
#folder-listing { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
footer { display: none !important; }
"""


def default_state_path() -> Path:
    """마지막 작업공간 기록 위치 — 서버를 다시 띄워도 같은 프로젝트를 연다(§4.1)."""
    override = os.environ.get(STATE_ENV)
    return Path(override) if override else Path.cwd() / ".vlogsrt-ui-state.json"


class RawWorkspaceUi:
    """작업공간 브라우저 서버 하나 — 상태 파일·Gradio 화면·HTTP 경계를 소유한다."""

    def __init__(
        self,
        port: int = 7860,
        state_path: Path | None = None,
        inspector: raw_service.MediaInspect | None = None,
        live: raw_service.LiveAnalysisTransport | None = None,
        finder_opener: Callable[[Path], None] | None = None,
        caption_providers: Callable[[], tuple[object, object]] | None = None,
    ) -> None:
        self.port = port
        self.raw = raw_service.RawWorkspaceService()
        self.state_path = state_path if state_path is not None else default_state_path()
        self._inspector = inspector  # None이면 서비스 기본(ffprobe). 시험은 가짜 조사자를 넣는다.
        # 실제 호출 재료는 두 길이다. 사람이 주입한 transport(시험·루트 가짜
        # 브라우저)가 우선이고, 없으면 **승인된 행동에서 늦게 만드는** 생산
        # transport(브라우저_runtime)가 그 갭을 채운다. 생산 쪽 키는 **서버를
        # 띄운 환경**에서만 온다 — 게이트가 `.env`를 적재하기 전에 여기서 한 번
        # 찍어 둔다(이때 없던 키는 이후 어떤 적재로도 문을 열지 못한다).
        self._live_transport = live
        self._startup_credentials = raw_service.capture_environment_credentials()
        # 브라우저 승인 기록 — 작업공간 서명에 묶여 산다(작업공간을 바꾸면 지운다).
        self._asr_approval_record: _AsrApprovalRecord | None = None
        self._semantic_approval_record: _SemanticApprovalRecord | None = None
        # 검토 화면 상태 — 마지막으로 그린 헤드 리비전과 기본 계획(편집·내보내기는
        # 이 신원에 묶인다). 시계·조각 범위는 그 리비전의 것을 그대로 둔다.
        self._review_revision_id: str | None = None
        self._review_base_plan_id: str | None = None
        self._review_signature: str | None = None
        self._review_clocks: dict[str, Any] = {}
        self._review_spans: dict[str, tuple[str, int, int]] = {}
        # 자막 화면 상태 — 마지막 실행 신원과 검토 머리 번호(편집·확정은 이 머리에 묶인다).
        self._caption_identity: dict[str, str | int | None] | None = None
        self._caption_signature: str | None = None
        self._caption_head_sequence: int | None = None
        self._caption_cue_ids: tuple[str, ...] = ()
        self._caption_unplaced: dict[int, str] = {}
        self._caption_chunk_count: int | None = None
        # 마지막 확정 내보내기 자리 — Finder 열기·경로 복사·정리 뒤 재검증의 대상.
        self._last_export_path: str | None = None
        self._last_export_dir: str | None = None
        # Finder 여는 경계 — 시험은 가짜를 넣는다(실제는 macOS `open -R`, 인자 검증 후).
        self._finder_opener = finder_opener
        # 최종 자막 실제 실행 제공자 주입 경계(시험·루트 가짜). 없으면 생산 transport.
        self._caption_providers = caption_providers
        self._root: Path | None = None
        self._selection_lock = threading.Lock()
        self._last_listing: raw_service.FolderListing | None = None
        # 가져오기 화면 상태 — 조사 제안과 확정은 **현재 작업공간 서명에 묶여**
        # 산다. 작업공간을 바꾸는 순간 함께 지운다(새 상태로 시작한다).
        self._ingest_proposal: raw_service.IngestProposal | None = None
        self._ingest_signature: str | None = None
        self._ingest_confirmation: raw_service.IngestConfirmation | None = None
        # 화면 구성요소 참조 — 만들어지기 전에는 None(조립은 build_blocks에서 끝난다).
        self._workspace_md: gr.Markdown | None = None
        self._settings_md: gr.Markdown | None = None
        self._listing_md: gr.Markdown | None = None
        self._jobs_md: gr.Markdown | None = None
        self._ingest_md: gr.Markdown | None = None
        self._segment_md: gr.Markdown | None = None
        self._package_md: gr.Markdown | None = None
        self._analysis_md: gr.Markdown | None = None
        self._media_dd: gr.Dropdown | None = None
        self._jobs_dd: gr.Dropdown | None = None
        self._devices_dd: gr.Dropdown | None = None
        self._ingest_jobs_dd: gr.Dropdown | None = None
        self._clip_dd: gr.Dropdown | None = None
        self._extract_jobs_dd: gr.Dropdown | None = None
        self._analysis_jobs_dd: gr.Dropdown | None = None
        self._analysis_sources_group: gr.CheckboxGroup | None = None
        self._network_dd: gr.Dropdown | None = None
        self._review_md: gr.Markdown | None = None
        self._export_md: gr.Markdown | None = None
        self._export_path_box: gr.Textbox | None = None
        self._caption_md: gr.Markdown | None = None
        self._clip_player_html: gr.HTML | None = None
        self._signature_state: gr.State | None = None

    # ── 서버 상태 — 새로고침·재시작을 건넌다 ───────────────────

    def startup(self) -> str:
        """서버 시작 경로. 마지막 작업공간을 다시 열고 **여기서만** 작업을 복구한다."""
        state = self._read_state()
        root_text = state.get("workspace_root", "")
        if not root_text:
            return "이전 작업공간 기록이 없다 — 1번 탭에서 만들거나 여세요."
        try:
            status = self.raw.open_workspace(Path(root_text))
        except raw_service.RawWorkspaceError as error:
            self._set_root(None)
            return f"이전 작업공간({root_text})을 열지 못했다: {error}"
        root = Path(status.workspace_root)
        self._set_root(root)
        recovered = self.raw.recover_interrupted_jobs(root)
        note = f"작업공간 {status.name}({status.workspace_root})을 다시 열었다."
        if recovered:
            note += f" 서버 종료로 중단된 작업 {recovered}건을 재개 가능 상태로 돌렸다."
        return note

    def _read_state(self) -> dict[str, str]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, root: Path) -> None:
        self.state_path.write_text(
            json.dumps({"workspace_root": str(root)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _set_root(self, root: Path | None) -> None:
        """선택된 작업공간 교체 — 자물쇠 안에서 한 번에(코디네이터 재결).

        가져오기 화면 상태(조사 제안·확정)도 같은 자물쇠 안에서 지운다 — 작업공간을
        바꾸고 나면 이전 프로젝트의 제안이 남아 있지 않아야 한다(새 상태로 시작).
        """
        with self._selection_lock:
            self._root = root
            self._ingest_proposal = None
            self._ingest_signature = None
            self._ingest_confirmation = None
            self._asr_approval_record = None
            self._semantic_approval_record = None
            self._clear_screen_state()

    def _remember(self, root: Path | None) -> None:
        with self._selection_lock:
            self._root = root
            self._ingest_proposal = None
            self._ingest_signature = None
            self._ingest_confirmation = None
            self._asr_approval_record = None
            self._semantic_approval_record = None
            self._clear_screen_state()
        if root is not None:
            self._write_state(root)

    def _clear_screen_state(self) -> None:
        """검토·자막·내보내기 화면 상태 초기화 — 작업공간 전환은 새 상태로 시작한다."""
        self._review_revision_id = None
        self._review_base_plan_id = None
        self._review_signature = None
        self._review_clocks = {}
        self._review_spans = {}
        self._caption_identity = None
        self._caption_signature = None
        self._caption_head_sequence = None
        self._caption_cue_ids = ()
        self._caption_unplaced = {}
        self._caption_chunk_count = None
        self._last_export_path = None
        self._last_export_dir = None

    def _snapshot_root(self) -> Path | None:
        """선택을 **한 번에** 찍는다 — 이후 읽기는 전부 이 스냅숏을 따른다."""
        with self._selection_lock:
            return self._root

    def _require_root(self) -> Path:
        root = self._snapshot_root()
        if root is None:
            raise gr.Error("작업공간이 열려 있지 않다 — 1번 탭에서 만들거나 여세요.")
        return root

    def _signature_for(self, root: Path | None) -> str | None:
        if root is None:
            return None
        try:
            return self.raw.workspace_id(root)
        except raw_service.RawWorkspaceError:
            return None

    def current_signature(self) -> str | None:
        """지금 열린 작업공간의 불변 신원 — 변경 콜백이 자기 선택이 최신인지 증명한다.

        화면은 마지막으로 그린 작업공간의 이 토큰을 들고 돌아온다. 서버가 그 사이
        다른 프로젝트로 옮겼다면 토큰이 어긋나고, 콜백은 그 선택을 **적용하지 않고**
        새로고침을 요구한다 — 오래된 선택이 다른 프로젝트에 떨어지지 않는다.
        """
        return self._signature_for(self._snapshot_root())

    def _require_current_selection(self, signature: str | None) -> Path:
        """변경 계열 콜백의 선택 검증 — 루트를 **한 번** 찍어 그대로 돌려준다.

        서명을 읽고 루트를 다시 읽으면 그 사이에 전환이 끼어들 수 있다 — 맞은 A
        서명이 B 루트를 돌려주는 순간이다. 스냅숏 하나로 서명 판정과 반환 루트가
        항상 같은 작업공간을 가리킨다(코디네이터 재결).
        """
        root = self._snapshot_root()
        if root is None:
            raise gr.Error("작업공간이 열려 있지 않다 — 1번 탭에서 만들거나 여세요.")
        current = self._signature_for(root)
        if signature is None or signature != current:
            raise gr.Error(
                "선택한 작업공간이 이미 바뀌었다 — 화면을 새로고침한 뒤 다시 시도하세요."
            )
        return root

    def workspace_root(self) -> Path | None:
        """지금 열린 작업공간 루트 — 없으면 None. 화면 어디서나 이것만 믿는다."""
        return self._snapshot_root()

    # ── 화면용 포맷 — 상태는 매번 서버에서 다시 읽는다 ───────────

    def workspace_markdown(self, root: Path | None = None) -> str:
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return (
                "열린 작업공간이 없다 — 1번 탭에서 **새 프로젝트 만들기** 또는 "
                "**기존 프로젝트 열기**."
            )
        try:
            status = self.raw.workspace_status(root)
        except raw_service.RawWorkspaceError as error:
            return f"작업공간 상태를 읽지 못했다: {error}"
        return "\n".join(
            [
                f"**프로젝트** {status.name}  ·  `{status.workspace_root}`",
                f"- workflow schema `{status.workflow_schema_version}` · DB schema "
                f"`{status.db_schema_version}` · 생성 {status.created_at}",
                f"- 네트워크 **{status.network_mode}** (오프라인 기본 — 유료 호출 없음)",
                f"- 등록 미디어 {status.media_count}건 · 진행 중 작업 {status.active_jobs}건",
            ]
        )

    def media_choices(self, root: Path | None = None) -> list[tuple[str, str]]:
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return []
        try:
            media = self.raw.list_media(root)
        except raw_service.RawWorkspaceError:
            return []
        return [
            (f"{item.file_id} · {item.relative_path} · {item.size_bytes:,}B", item.file_id)
            for item in media
        ]

    def jobs_markdown(self, root: Path | None = None) -> str:
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return "작업 목록은 작업공간을 연 뒤 보인다."
        jobs = self.raw.list_jobs(root)
        if not jobs:
            return "작업이 없다 — 가져오기·분석이 열리면 여기에 쌓인다."
        rows = ["| 작업 | 종류 | 상태 | 진행 |", "|---|---|---|---|"]
        rows += [
            f"| `{job.job_id}` | {job.kind} | {job.status} | {job.progress_current}/"
            f"{job.progress_total} |" + (f" 오류: {job.error}" if job.error else "")
            for job in jobs
        ]
        return "\n".join(rows)

    def active_job_choices(self, root: Path | None = None) -> list[tuple[str, str]]:
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return []
        jobs = self.raw.list_jobs(root)
        return [
            (f"{job.job_id} · {job.kind} · {job.status}", job.job_id)
            for job in jobs
            if job.status in raw_service.ACTIVE_JOB_STATUSES
        ]

    def device_choices(self) -> list[tuple[str, str]]:
        return [(str(path), str(path)) for path in raw_service.mounted_device_candidates()]

    def settings_markdown(self, root: Path | None = None) -> str:
        """4번 탭의 읽기전용 설정 요약 — 값은 설정에서 온다(화면이 못 박지 않는다)."""
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return "작업공간을 열면 자막 프리셋·모델·수명주기 요약이 여기 나타난다."
        config = self.raw.settings(root)
        registry = config.registry
        lines = [
            f"**자막 프리셋** {config.caption.profile} · schema `{config.caption.schema_version}`",
            f"- 문구 모델 `{config.models.text_model}` "
            f"(응답 `{config.caption.text_response_format}`)",
            f"- 시간 제공자 `{config.models.timing_model}` via `{config.caption.timing_provider}`"
            " (과도기)",
            f"- 의미 분석 `{config.models.luna_model}`",
            f"- 공식 확인일 {registry.docs_checked_at} · 계정 접근 "
            f"`{registry.account_access_status}` · 실기 시험 `{registry.integration_test_status}`",
            f"- 시간 제공자 종료 예정 **{registry.timing_shutdown_date}** — 이후 신규 온라인"
            " 전사는 차단된다",
            f"- 요금(분) 문구 {config.pricing.text_per_minute} · 시간 "
            f"{config.pricing.timing_per_minute} {config.pricing.currency}",
            f"- 네트워크 **{config.network_mode}** · 명시적 승인 필요 — 승인 없이 유료 호출은"
            " 일어나지 않는다",
        ]
        return "\n".join(lines)

    @staticmethod
    def _player_html(file_id: str | None) -> str:
        if not file_id:
            return (
                '<p style="color:#888">등록된 미디어를 고르면 여기서 재생된다 — 원본은 등록된'
                " ID로만, 이 서버를 벗어나지 않고 제공된다.</p>"
            )
        safe = html.escape(file_id, quote=True)
        return (
            f'<video controls preload="metadata" style="width:100%;max-height:480px;'
            f'background:#000" src="/media/{safe}"></video>'
        )

    @staticmethod
    def _listing_markdown(listing: raw_service.FolderListing | None) -> str:
        if listing is None:
            return "폴더 경로를 넣고 **폴더 찾아보기**를 누르면 한 층만 나열한다(내용 읽지 않음)."
        lines = [f"**{listing.path}**", ""]
        if listing.parent:
            lines.append(f"· 상위 `{listing.parent}`")
        if not listing.entries:
            lines.append("· 보일 항목이 없다(숨김 항목·심볼릭 링크는 건너뛴다).")
        for entry in listing.entries:
            mark = "📁" if entry.is_directory else "🎞"
            lines.append(f"- {mark} `{entry.name}`")
        return "\n".join(lines)

    # ── 화면 1 — 프로젝트 · 가져오기 ───────────────────────────

    def _project_tab(self) -> tuple[gr.Button, gr.Button, gr.Textbox, gr.Textbox]:
        """1번 탭 조립 — 전환 단추·입력 칸을 돌려준다(배선은 build_blocks에서)."""
        with gr.Tab("1 · 프로젝트 · 가져오기"):
            self._workspace_md = gr.Markdown(self.workspace_markdown())
            with gr.Row():
                ws_path_box = gr.Textbox(
                    label="작업공간 경로",
                    info="기존 작업공간 폴더를 가리킵니다. 업로드하지 않습니다.",
                    placeholder="/Volumes/WORK/VlogProject",
                    scale=6,
                )
                open_button = gr.Button("기존 프로젝트 열기", scale=1)
            with gr.Row():
                new_name_box = gr.Textbox(
                    label="새 프로젝트 이름",
                    placeholder="제주 3일 브이로그",
                    scale=6,
                )
                create_button = gr.Button("새 프로젝트 만들기", variant="primary", scale=1)

            gr.Markdown("---")
            with gr.Row():
                source_box = gr.Textbox(
                    label="원본 폴더(SD 카드)",
                    info="가져올 원본이 있는 폴더. 아래 조사 → 원본 순서 확인 → 복사 및 검증이"
                    " 이 탭에서 바로 이어진다.",
                    placeholder="/Volumes/SDCARD/DCIM",
                    scale=6,
                )
                browse_button = gr.Button("폴더 찾아보기", scale=1)
            self._devices_dd = gr.Dropdown(
                choices=self.device_choices(),
                label="마운트된 장치 후보",
                info="후보를 보여줄 뿐, 누르기 전에는 아무 것도 조사하지 않는다.",
                interactive=True,
            )
            self._listing_md = gr.Markdown(self._listing_markdown(None), elem_id="folder-listing")

            def on_browse(path_text: str) -> tuple[str, str]:
                try:
                    listing = raw_service.browse_folder(path_text or "")
                except raw_service.BrowseNotAllowedError as error:
                    raise gr.Error(str(error)) from error
                self._last_listing = listing
                return self._listing_markdown(listing), listing.path

            def on_device(device_path: str) -> tuple[str, str]:
                if not device_path:
                    raise gr.Error("장치 후보를 하나 고르세요.")
                return on_browse(device_path)

            def on_devices_refresh() -> gr.Dropdown:
                return gr.Dropdown(choices=self.device_choices())

            browse_button.click(on_browse, source_box, [self._listing_md, source_box])
            self._devices_dd.select(on_device, self._devices_dd, [self._listing_md, source_box])

            # ── 가져오기 — 조사 → 순서 확인(명시적) → 복사·검증 → 새로고침/재시도 ──
            gr.Markdown("---")
            with gr.Row():
                scan_button = gr.Button("조사 (가져올 영상 확인)", scale=2)
                confirm_button = gr.Button("원본 순서 확인", variant="primary", scale=2)
                copy_button = gr.Button("복사 및 검증", scale=2)
                refresh_ingest = gr.Button("진행 상태 새로고침", scale=2)
            with gr.Row():
                self._ingest_jobs_dd = gr.Dropdown(
                    choices=self.resumable_ingest_choices(),
                    label="재시도할 가져오기 작업",
                    interactive=True,
                    scale=6,
                )
                retry_button = gr.Button("재시도", scale=1)
            self._ingest_md = gr.Markdown(self._ingest_state_markdown(self._snapshot_root()))
            scan_button.click(
                self.ingest_scan_view, [self._signature_state, source_box], self._ingest_md
            )
            confirm_button.click(self.ingest_confirm_view, self._signature_state, self._ingest_md)
            copy_button.click(
                self.ingest_copy_view,
                self._signature_state,
                [self._ingest_md, self._ingest_jobs_dd],
            )
            refresh_ingest.click(
                self.ingest_refresh_view,
                self._signature_state,
                [self._workspace_md, self._ingest_md, self._ingest_jobs_dd],
            )
            retry_button.click(
                self.ingest_retry_view,
                [self._signature_state, self._ingest_jobs_dd],
                self._ingest_md,
            )

            # ── 전체 원본 순서 — 가져온 배치를 넘어 사람이 명시적으로 확정 ──
            with gr.Row():
                global_order_box = gr.Textbox(
                    label="전체 원본 순서(선택)",
                    info="비우면 제안(배치 순서·배치 안 확정 순번) 그대로다. "
                    "직접 정하려면 ready 원본의 자산 ID를 줄/쉼표로 모두 나열한다.",
                    placeholder="source_…, source_… (모든 ready 원본)",
                    scale=6,
                )
                global_confirm_button = gr.Button("전체 원본 순서 확정", scale=1)
            global_confirm_button.click(
                self.ingest_global_confirm_view,
                [self._signature_state, global_order_box],
                self._ingest_md,
            )
        return open_button, create_button, ws_path_box, new_name_box

    # ── 화면 2 — 분석 · 조각 검토 ──────────────────────────────

    def _review_tab(
        self,
    ) -> tuple[gr.Button, gr.Button, gr.Dropdown, gr.Button]:
        with gr.Tab("2 · 분석 · 조각 검토"):
            gr.Markdown(
                "> 이 단계는 두 갈래다. **의미 분석**(Luna)은 내부 전사 증거와 프레임"
                " 표본으로 대화·장면을 요약하고 경계를 **제안**한다 — 브라우저에서는"
                " **오프라인 재생**만 가능하고(명시적으로 준비한 합성 픽스처만 재생,"
                " 실제 호출 아님), 실제 호출은 사람이 만든 승인 기록으로만 시작된다."
                " **결정적 분할**은 의미 분석 없이 측정된 프레임 격자로 전체를 타일링한다"
                " — 유료 호출은 전혀 없다."
            )
            # ── 의미 분석(오프라인 재생) ──────────────────────────
            with gr.Row():
                self._analysis_sources_group = gr.CheckboxGroup(
                    choices=self.analysis_source_choices(),
                    label="분석할 원본(선택) — 비우면 전체",
                    interactive=True,
                )
            self._analysis_settings_md = gr.Markdown(self._analysis_settings_markdown())
            with gr.Row():
                estimate_button = gr.Button("분석 견적(로컬)", scale=2)
                analysis_start = gr.Button("오프라인 재생 분석 시작", variant="primary", scale=2)
                analysis_refresh = gr.Button("분석 상태 새로고침", scale=2)
                analysis_plan = gr.Button("분석 결과로 분할 계획", scale=2)
            # ── 실제 호출(두 단계 승인) — 견적 표 + 확인란으로 승인한다 ──
            gr.Markdown(
                "> **실제 호출은 두 단계다.** 1단계 견적 → 전사 승인(확인란) →"
                " 전사·프레임 기록 → **의미 승인 대기** → 2단계 견적 → Luna 승인"
                "(확인란) → 완료. 승인은 화면이 잰 견적 표를 서버가 간직하는 것으로"
                " 남는다 — 키는 브라우저 서버를 띄운 환경에서만 오고 화면은"
                " 자격증명을 찾지 않는다(§14.3)."
            )
            with gr.Row():
                live_estimate_button = gr.Button("실제 호출 1단계 견적(승인용)", scale=2)
                asr_confirm_box = gr.Checkbox(
                    label="위 1단계 견적 그대로 전사를 승인한다",
                    value=False,
                    interactive=True,
                    scale=3,
                )
                live_asr_button = gr.Button(
                    "실제 호출 1단계(전사) 시작", variant="primary", scale=2
                )
            with gr.Row():
                semantic_estimate_button = gr.Button("2단계 견적(로컬·전사 후 실측)", scale=2)
                semantic_confirm_box = gr.Checkbox(
                    label="위 2단계 견적의 실측 지문으로 Luna 호출을 승인한다",
                    value=False,
                    interactive=True,
                    scale=3,
                )
                semantic_resume_button = gr.Button(
                    "실제 호출 2단계(의미) 재개", variant="primary", scale=2
                )
            with gr.Row():
                self._network_dd = gr.Dropdown(
                    choices=["offline", "online"],
                    value=self._current_network_mode(),
                    label="작업공간 네트워크 모드(설정)",
                    interactive=True,
                    scale=2,
                )
                network_apply = gr.Button("네트워크 모드 적용", scale=1)
            with gr.Row():
                self._analysis_jobs_dd = gr.Dropdown(
                    choices=self.resumable_analysis_choices(),
                    label="재시도·2단계 재개 대상 분석 작업",
                    interactive=True,
                    scale=6,
                )
                analysis_retry = gr.Button("재시도", scale=1)
            self._analysis_md = gr.Markdown(
                self._analysis_state_markdown(self._snapshot_root()), elem_id="analysis-state"
            )
            estimate_button.click(
                self.analysis_estimate_view,
                [self._signature_state, self._analysis_sources_group],
                self._analysis_md,
            )
            analysis_start.click(
                self.analysis_start_view,
                [self._signature_state, self._analysis_sources_group],
                [self._analysis_md, self._analysis_jobs_dd],
            )
            analysis_refresh.click(
                self.analysis_refresh_view,
                self._signature_state,
                [self._analysis_md, self._analysis_jobs_dd, self._analysis_sources_group],
            )
            analysis_retry.click(
                self.analysis_retry_view,
                [self._signature_state, self._analysis_jobs_dd],
                self._analysis_md,
            )
            live_estimate_button.click(
                self.analysis_live_estimate_view,
                [self._signature_state, self._analysis_sources_group],
                self._analysis_md,
            )
            live_asr_button.click(
                self.analysis_live_asr_view,
                [self._signature_state, self._analysis_sources_group, asr_confirm_box],
                [self._analysis_md, self._analysis_jobs_dd],
            )
            semantic_estimate_button.click(
                self.analysis_semantic_estimate_view,
                self._signature_state,
                self._analysis_md,
            )
            semantic_resume_button.click(
                self.analysis_semantic_resume_view,
                [self._signature_state, self._analysis_jobs_dd, semantic_confirm_box],
                [self._analysis_md, self._analysis_jobs_dd],
            )
            network_apply.click(
                self.settings_network_view,
                [self._signature_state, self._network_dd],
                self._analysis_settings_md,
            )
            gr.Markdown("---")
            # ── 조각 검토(W3 백엔드) — 순수 편집·조회, 재추출은 계획으로 ──
            gr.Markdown(
                "> **조각 검토** — 추출 계획 위에 시간순 검토 리비전이 산다. 편집은"
                " 화면이 본 헤드 리비전에 묶이고, 경계를 고치면 그 조각은 새 추출로만"
                " 갱신된다(메타데이터 편집은 재추출을 만들지 않는다). 유료 호출·"
                " 미디어 재생성은 전혀 없다."
            )
            with gr.Row():
                review_init_button = gr.Button("검토 시작", variant="primary", scale=2)
                review_refresh_button = gr.Button("검토 새로고침", scale=2)
                review_reextract_button = gr.Button("다시 뽑을 조각", scale=2)
                review_plan_button = gr.Button("검토 계획 반영", scale=2)
            with gr.Row():
                review_query_box = gr.Textbox(label="검색(원본·메모)", scale=3)
                review_tags_box = gr.Textbox(label="태그(쉼표)", scale=3)
                review_filter_button = gr.Button("조회", scale=1)
            with gr.Row():
                review_segment_box = gr.Textbox(label="조각 ID", scale=3)
                review_note_box = gr.Textbox(label="메모", scale=4)
                review_note_button = gr.Button("메모 저장", scale=1)
            with gr.Row():
                review_selection_dd = gr.Dropdown(
                    choices=["사용", "제외"], value="사용", label="선택 표시", scale=1
                )
                review_selection_button = gr.Button("선택 적용", scale=1)
                review_boundary_box = gr.Textbox(label="경계 초", scale=2)
                review_boundary_button = gr.Button("경계 조정", scale=1)
            with gr.Row():
                review_split_box = gr.Textbox(label="나눌 초", scale=2)
                review_split_button = gr.Button("나누기", scale=1)
                review_merge_left_box = gr.Textbox(label="왼쪽 조각 ID", scale=3)
                review_merge_right_box = gr.Textbox(label="오른쪽 조각 ID", scale=3)
                review_merge_button = gr.Button("이웃 합치기", scale=1)
            _review_root = self._snapshot_root()
            self._review_md = gr.Markdown(
                self._review_panel_markdown(_review_root)
                if _review_root is not None
                else "_검토 상태가 여기 나타난다 — 검토 시작으로 만든다._",
                elem_id="review-state",
            )
            review_init_button.click(
                self.review_initialize_view, self._signature_state, self._review_md
            )
            review_refresh_button.click(
                self.review_refresh_view, self._signature_state, self._review_md
            )
            review_reextract_button.click(
                self.review_reextract_view, self._signature_state, self._review_md
            )
            review_plan_button.click(self.review_plan_view, self._signature_state, self._review_md)
            review_filter_button.click(
                self.review_filter_view,
                [self._signature_state, review_query_box, review_tags_box],
                self._review_md,
            )
            review_note_button.click(
                self.review_note_view,
                [self._signature_state, review_segment_box, review_note_box],
                self._review_md,
            )
            review_selection_button.click(
                self.review_selection_view,
                [self._signature_state, review_segment_box, review_selection_dd],
                self._review_md,
            )
            review_boundary_button.click(
                self.review_boundary_view,
                [self._signature_state, review_segment_box, review_boundary_box],
                self._review_md,
            )
            review_split_button.click(
                self.review_split_view,
                [self._signature_state, review_segment_box, review_split_box],
                self._review_md,
            )
            review_merge_button.click(
                self.review_merge_view,
                [self._signature_state, review_merge_left_box, review_merge_right_box],
                self._review_md,
            )
            gr.Markdown("---")
            # ── 결정적 분할(오프라인) ─────────────────────────────
            gr.Markdown(
                "> **결정적 분할** — 의미 분석 없이 확정된 전체 순서 위에서 각 원본의"
                " 측정된 프레임 격자로 전체를 빈틈없이 타일링한다. 유료 호출은 전혀"
                " 없다(오프라인)."
            )
            with gr.Row():
                plan_button = gr.Button("결정적 분할 계획", variant="primary", scale=2)
                extract_button = gr.Button("조각 추출 시작", scale=2)
                refresh_segment = gr.Button("진행 상태 새로고침", scale=2)
            with gr.Row():
                self._profile_dd = gr.Dropdown(
                    choices=[(name, name) for name in raw_service.EXTRACT_PROFILES],
                    value="source_matched",
                    label="추출 프로파일",
                    interactive=True,
                    scale=2,
                )
                intervals_box = gr.Textbox(
                    label="구간 계획(선택)",
                    info="각 줄: `자산ID 시작 끝`(초). 전체를 빈틈없이 덮아야 한다."
                    " 비우면 균일 타일(기본 30초)로 전체를 덮는다.",
                    placeholder="source_ab12 0 2.5\nsource_ab12 2.5 6",
                    scale=4,
                )
            with gr.Row():
                self._extract_jobs_dd = gr.Dropdown(
                    choices=self.resumable_extract_choices(),
                    label="재시도할 추출 작업",
                    interactive=True,
                    scale=6,
                )
                retry_extract = gr.Button("재시도", scale=1)
            self._segment_md = gr.Markdown(
                self._segment_state_markdown(self._snapshot_root()), elem_id="segment-state"
            )
            plan_button.click(
                self.segment_plan_view,
                [self._signature_state, intervals_box],
                self._segment_md,
            )
            retry_extract.click(
                self.segment_retry_view,
                [self._signature_state, self._extract_jobs_dd],
                self._segment_md,
            )
            gr.Markdown("---")
            with gr.Row():
                self._clip_dd = gr.Dropdown(
                    choices=self.clip_choices(),
                    label="게시된 조각(재생)",
                    interactive=True,
                    scale=6,
                )
                refresh_media = gr.Button("조각 목록 새로고침", scale=1)
            self._clip_player_html = gr.HTML(self._player_html(None))
            with gr.Row():
                clip_vtt_box = gr.Checkbox(
                    label="검토 자막(VTT) 함께 재생 — 측정된 발화 자막",
                    value=False,
                    interactive=True,
                    scale=4,
                )
                clip_vtt_button = gr.Button("자막 토글 적용", scale=1)
                clip_vtt_button.click(
                    self.review_vtt_toggle_view,
                    [self._signature_state, self._clip_dd, clip_vtt_box],
                    self._clip_player_html,
                )
            with gr.Row():
                self._media_dd = gr.Dropdown(
                    choices=self.media_choices(),
                    label="등록된 미디어 전체",
                    interactive=True,
                    scale=6,
                )
                refresh_all_media = gr.Button("미디어 목록 새로고침", scale=1)
            media_player = gr.HTML(self._player_html(None))

            def on_clip_pick(file_id: str | None) -> str:
                return self._player_html(file_id)

            def on_refresh_clips() -> gr.Dropdown:
                return gr.Dropdown(choices=self.clip_choices())

            def on_media_pick(file_id: str | None) -> str:
                return self._player_html(file_id)

            def on_refresh_media() -> gr.Dropdown:
                return gr.Dropdown(choices=self.media_choices())

            assert self._clip_dd is not None and self._clip_player_html is not None
            self._clip_dd.change(on_clip_pick, self._clip_dd, self._clip_player_html)
            refresh_media.click(on_refresh_clips, None, self._clip_dd)
            assert self._media_dd is not None
            self._media_dd.change(on_media_pick, self._media_dd, media_player)
            refresh_all_media.click(on_refresh_media, None, self._media_dd)
            with gr.Row():
                self._jobs_dd = gr.Dropdown(
                    choices=self.active_job_choices(),
                    label="진행 중 작업",
                    interactive=True,
                    scale=6,
                )
                refresh_jobs = gr.Button("작업 목록 새로고침", scale=1)
                cancel_button = gr.Button("선택 작업 취소", scale=1)
            self._jobs_md = gr.Markdown(self.jobs_markdown(), elem_id="exec-log")

            def on_refresh_jobs() -> tuple[gr.Dropdown, str]:
                return gr.Dropdown(choices=self.active_job_choices()), self.jobs_markdown()

            def on_cancel(signature: str | None, job_id: str | None) -> tuple[gr.Dropdown, str]:
                return self.cancel_view(signature, job_id)

            assert (
                self._media_dd is not None
                and self._jobs_dd is not None
                and self._signature_state is not None
            )
            refresh_jobs.click(on_refresh_jobs, None, [self._jobs_dd, self._jobs_md])
            cancel_button.click(
                on_cancel, [self._signature_state, self._jobs_dd], [self._jobs_dd, self._jobs_md]
            )
        return extract_button, refresh_segment, self._profile_dd, analysis_plan

    def cancel_selected(self, signature: str | None, job_id: str | None) -> str:
        """변경 콜백의 선택 검증본보기 — 오래된 작업공간 선택은 거기서 끝난다.

        취소는 서버가 아니라 **화면이 본 작업공간**을 대상으로 요청된다. 그 사이
        서버가 다른 프로젝트로 옮겼다면 어긋난 신원 때문에 실패하고, 다른 프로젝트의
        작업에 적용되지 않는다.
        """
        if not job_id:
            raise gr.Error("취소할 진행 중 작업을 고르세요.")
        root = self._require_current_selection(signature)
        record = self.raw.cancel_job(root, job_id)
        return f"`{record.job_id}` 상태: {record.status}"

    # ── 같은 화면의 작업공간 전환 — 전체 보기를 바로 내린다 ─────

    def open_workspace_view(
        self, path_text: str
    ) -> tuple[
        str,
        str | None,
        gr.Dropdown,
        gr.Dropdown,
        str,
        str,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        gr.CheckboxGroup,
    ]:
        """기존 작업공간 열기 이벤트 — 서명·미디어·작업·설정까지 한 번에 갱신한다.

        새로고침 없이 같은 화면에서 프로젝트를 바꿀 수 있어야 한다(루트 재현:
        Markdown만 갱신되면 이 화면의 서명은 옛 프로젝트에 남아, 방금 만든
        프로젝트의 작업 취소가 '선택이 바뀌었다'로 실패했다). 가져오기 패널도
        같이 내린다 — 이전 프로젝트의 제안·확정이 남지 않는다(새 상태).
        """
        text = (path_text or "").strip()
        if not text:
            raise gr.Error("작업공간 경로를 입력하세요.")
        try:
            status = self.raw.open_workspace(Path(text))
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        self._remember(Path(status.workspace_root))
        return self.workspace_view_values()

    def create_workspace_view(
        self, name: str, path_text: str
    ) -> tuple[
        str,
        str | None,
        gr.Dropdown,
        gr.Dropdown,
        str,
        str,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        gr.CheckboxGroup,
    ]:
        """새 작업공간 만들기 이벤트 — 첫 생성(서명 None)에서도 같은 화면이 즉시 연결된다."""
        if not (name or "").strip():
            raise gr.Error("새 프로젝트 이름을 입력하세요.")
        if not (path_text or "").strip():
            raise gr.Error("작업공간으로 쓸 빈 폴더 경로를 입력하세요.")
        try:
            status = self.raw.create_workspace(Path(path_text), name)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        self._remember(Path(status.workspace_root))
        return self.workspace_view_values()

    def workspace_view_values(
        self,
    ) -> tuple[
        str,
        str | None,
        gr.Dropdown,
        gr.Dropdown,
        str,
        str,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        gr.CheckboxGroup,
    ]:
        """작업공간 전환 뒤 화면이 받아야 하는 전체 보기(서명 포함).

        루트를 **한 번** 찍어 모든 조각에 통과시킨다 — 전환이 끼어들어도 이 보기가
        두 작업공간에 걸치지 않는다(코디네이터 재결). 마지막 네 값은 가져오기·
        분할·분석 패널이다.
        """
        root = self._snapshot_root()
        return (
            self.workspace_markdown(root),
            self._signature_for(root),
            gr.Dropdown(choices=self.media_choices(root)),
            gr.Dropdown(choices=self.active_job_choices(root)),
            self.jobs_markdown(root),
            self.settings_markdown(root),
            self._ingest_state_markdown(root),
            gr.Dropdown(choices=self.resumable_ingest_choices(root)),
            self._segment_state_markdown(root),
            gr.Dropdown(choices=self.resumable_extract_choices(root)),
            self._package_state_markdown(root),
            gr.Dropdown(choices=self.clip_choices(root)),
            self._analysis_state_markdown(root),
            gr.Dropdown(choices=self.resumable_analysis_choices(root)),
            gr.CheckboxGroup(choices=self.analysis_source_choices(root)),
        )

    def _selection_outputs(self) -> list[object]:
        """만들기·열기 이벤트의 출력 대상 — `workspace_view_values`와 짝이다."""
        assert (
            self._workspace_md is not None
            and self._settings_md is not None
            and self._jobs_md is not None
            and self._ingest_md is not None
            and self._media_dd is not None
            and self._jobs_dd is not None
            and self._ingest_jobs_dd is not None
            and self._segment_md is not None
            and self._extract_jobs_dd is not None
            and self._package_md is not None
            and self._clip_dd is not None
            and self._analysis_md is not None
            and self._analysis_jobs_dd is not None
            and self._analysis_sources_group is not None
            and self._signature_state is not None
        )
        return [
            self._workspace_md,
            self._signature_state,
            self._media_dd,
            self._jobs_dd,
            self._jobs_md,
            self._settings_md,
            self._ingest_md,
            self._ingest_jobs_dd,
            self._segment_md,
            self._extract_jobs_dd,
            self._package_md,
            self._clip_dd,
            self._analysis_md,
            self._analysis_jobs_dd,
            self._analysis_sources_group,
        ]

    def cancel_view(self, signature: str | None, job_id: str | None) -> tuple[gr.Dropdown, str]:
        """취소 이벤트 — 안내 문장과 함께 진행 중 작업 목록도 다시 내린다."""
        message = self.cancel_selected(signature, job_id)
        return gr.Dropdown(choices=self.active_job_choices()), message

    # ── 화면 1 — SD/폴더 가져오기(조사 → 순서 확인 → 복사·검증) ──

    @staticmethod
    def _human_bytes(size: float) -> str:
        scaled = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if scaled < 1024 or unit == "TB":
                return f"{scaled:,.0f}{unit}" if unit == "B" else f"{scaled:,.1f}{unit}"
            scaled /= 1024
        return f"{scaled:,.1f}TB"

    def _proposal_markdown(self, proposal: raw_service.IngestProposal | None) -> str:
        """조사 제안 보기 — 촬영 시각은 미조사임을 숨기지 않고 적는다(§6.2)."""
        if proposal is None:
            return (
                "원본 폴더를 넣고 **조사**를 누르면 가져올 영상 목록·공간 견적이 여기"
                " 나타난다. 아직 아무 것도 복사하지 않는다."
            )
        lines = [
            f"**조사 결과** — 후보 {len(proposal.order)}건 · 제외 {len(proposal.scan.rejected)}건",
            "",
        ]
        lines += ["| 순서 | 파일 | 크기 | 길이 | 해상도 | 촬영 시각 |", "|---|---|---|---|---|---|"]
        for index, candidate in enumerate(proposal.order, start=1):
            inspection = candidate.inspection
            duration = (
                "미확인"
                if inspection.duration_seconds is None
                else f"{inspection.duration_seconds:.1f}초"
            )
            resolution = (
                "미확인"
                if inspection.width is None or inspection.height is None
                else f"{inspection.width}×{inspection.height}"
            )
            lines.append(
                f"| {index} | `{candidate.source_relative_path}` | "
                f"{self._human_bytes(candidate.size_bytes)} | {duration} | {resolution} |"
                f" {_capture_cell(inspection)} |"
            )
        space = proposal.space
        verdict = "충분" if space.sufficient else "**부족 — 복사가 시작 전에 차단된다**"
        lines += [
            "",
            f"- 예상 복사 공간 {self._human_bytes(space.copy_bytes)} · 작업공간 남은 "
            f"{self._human_bytes(space.free_bytes)} → {verdict}",
            "- 촬영 시각은 컨테이너 선언이 **유효할 때만** 묶는다(ISO-8601·시간대·상식"
            " bound). 못 미치면 무효로 표시되고 순서는 자연 파일 번호 정렬을 따른다"
            " — mtime은 쓰지 않는다(§6.2).",
        ]
        if proposal.scan.rejected:
            lines += ["", "**제외된 항목(사유)**"]
            lines += [
                f"- `{item.source_relative_path}` — {item.reason}"
                for item in proposal.scan.rejected
            ]
        return "\n".join(lines)

    def _ingest_state_markdown(self, root: Path | None) -> str:
        """가져오기 패널 전체 보기 — 제안·확정·복사 진행을 한눈에(상태는 DB에서)."""
        proposal = None
        with self._selection_lock:
            if root is not None and self._ingest_signature == self._signature_for(root):
                proposal = self._ingest_proposal
        base = self._proposal_markdown(proposal)
        if root is None:
            return base
        assets, revision, jobs = self.raw.ingest_overview(root)
        if not assets:
            return base
        lines = [
            base,
            "",
            f"**확정 순서** — 배치 `{revision.batch_dir if revision else '?'}`"
            f" · 근거 {revision.basis if revision else '?'}"
            f" · 리비전 `{revision.revision_id if revision else '?'}`",
            "",
        ]
        lines += ["| 순번 | 파일 | 상태 | 크기 | 비고 |", "|---|---|---|---|---|"]
        for asset in assets:
            note = "중복(같은 내용 먼저 검증본 있음)" if asset.duplicate_of else ""
            if asset.error:
                note = asset.error
            lines.append(
                f"| {asset.ordinal} | `{asset.source_relative_path}` | {asset.status} | "
                f"{self._human_bytes(asset.size_bytes)} | {note} |"
            )
        if jobs:
            lines += ["", "**가져오기 작업**"]
            lines += [
                f"- `{job.job_id}` {job.status} {job.progress_current}/{job.progress_total}"
                + (f" — {job.error}" if job.error else "")
                for job in jobs
            ]
        return "\n".join(lines) + self._global_order_markdown(root)

    def _global_order_markdown(self, root: Path) -> str:
        """전체 원본 순서 보기 — 배치를 넘는 확정 기록(옛 리비전은 다시 쓰지 않는다)."""
        revision = self.raw.latest_global_source_order(root)
        proposal = self.raw.global_order_proposal(root)
        if revision is None:
            if not proposal:
                return ""
            names = ", ".join(
                f"{index}. `{asset.asset_id}`({asset.batch_dir}/{asset.source_name})"
                for index, asset in enumerate(proposal, start=1)
            )
            return (
                "\n\n**전체 원본 순서** — 아직 확정하지 않았다. 제안(배치 순서·배치 안"
                f" 확정 순번): {names}"
            )
        entries = "\n".join(
            f"- {entry.ordinal}. `{entry.asset_id}` (배치 {entry.batch_dir})"
            for entry in revision.entries
        )
        return (
            f"\n\n**전체 원본 순서** — 리비전 `{revision.revision_id}` · 근거 "
            f"{revision.basis} · 확정 {revision.created_at}\n{entries}\n"
            f"(리비전 총 {len(self.raw.list_global_source_orders(root))}개 — 순서를 바꾸면"
            " 새 리비전이 쌓이고, 배치 안 확정 순서·옛 스냅숏은 그대로다.)"
        )

    def ingest_global_confirm_view(self, signature: str | None, order_text: str) -> str:
        """전체 원본 순서 확정 이벤트 — 배치를 넘는 순서를 사람이 명시적으로 못 박는다.

        빈 입력은 제안(배치 순서·배치 안 확정 순번) 확정이다. 이 확정은 새 전체
        리비전을 만들 뿐 — 배치 안 순서나 옛 리비전을 조용히 다시 섞지 않는다.
        """
        root = self._require_current_selection(signature)
        asset_ids: list[str] | None = None
        text = (order_text or "").strip()
        if text:
            asset_ids = [
                chunk.strip()
                for chunk in text.replace(",", "\n").replace(";", "\n").splitlines()
                if chunk.strip()
            ]
        try:
            revision = self.raw.confirm_global_source_order(root, asset_ids)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        # 성공 증거는 갱신된 패널 자체다 — 새 리비전 번호와 항목이 거기 나타난다.
        _ = revision
        return self._ingest_state_markdown(root)

    def ingest_scan_view(self, signature: str | None, source_text: str) -> str:
        """조사 이벤트 — 읽기만 한다. 제안은 현재 작업공간 서명에 묶어 보관한다."""
        root = self._require_current_selection(signature)
        text = (source_text or "").strip()
        if not text:
            raise gr.Error("원본 폴더(SD 카드) 경로를 입력하세요.")
        try:
            proposal = self.raw.scan_sources(
                root, Path(text), self._inspector or raw_service.ffprobe_media_inspector
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        except OSError as error:
            raise gr.Error(f"원본 폴더를 읽지 못했다: {error}") from error
        with self._selection_lock:
            self._ingest_proposal = proposal
            self._ingest_signature = self._signature_for(root)
            self._ingest_confirmation = None
        return self._ingest_state_markdown(root)

    def _require_ingest_proposal(self, root: Path) -> raw_service.IngestProposal:
        """이 작업공간을 위해 조사한 제안 — 없으면(또는 다른 작업공간의 것이면) 안내한다."""
        signature = self._signature_for(root)
        with self._selection_lock:
            proposal = self._ingest_proposal if self._ingest_signature == signature else None
        if proposal is None:
            raise gr.Error(
                "먼저 이 작업공간에서 원본 폴더 조사를 실행하세요 — 확정은 조사 제안만 받는다."
            )
        return proposal

    def ingest_confirm_view(self, signature: str | None) -> str:
        """원본 순서 확인 이벤트 — 명시적 확인으로 배치·불변 순서 리비전을 만든다."""
        root = self._require_current_selection(signature)
        proposal = self._require_ingest_proposal(root)
        try:
            confirmation = self.raw.confirm_ingest(root, proposal)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        with self._selection_lock:
            self._ingest_confirmation = confirmation
        return self._ingest_state_markdown(root)

    def ingest_copy_view(self, signature: str | None) -> tuple[str, gr.Dropdown]:
        """복사 및 검증 이벤트 — 확정된 순서 그대로 작업을 제출한다(완료를 가장하지 않는다)."""
        root = self._require_current_selection(signature)
        with self._selection_lock:
            confirmation = self._ingest_confirmation
        if confirmation is None:
            assets, _, _ = self.raw.ingest_overview(root)
            if not assets:
                raise gr.Error(
                    "먼저 조사 후 원본 순서 확인을 누르세요 — 확정 없이 복사하지 않는다."
                )
            raise gr.Error("새로 고침으로 확정이 풀렸다 — 다시 조사·확인한 뒤 복사하세요.")
        self.raw.start_ingest_copy(root, confirmation)
        return self._ingest_state_markdown(root), gr.Dropdown(
            choices=self.resumable_ingest_choices(root)
        )

    def ingest_refresh_view(self, signature: str | None) -> tuple[str, str, gr.Dropdown]:
        """진행 상태 새로고침 — 서버(DB)가 정본, 화면은 다시 읽기만 한다.

        머리글(등록 미디어 수)도 함께 내린다 — 복사가 끝났는데 머리글이 0건인
        채로 남는 오래된 표시를 새로고침이 못 고치는 일이 없게 한다.
        """
        root = self._require_current_selection(signature)
        return (
            self.workspace_markdown(root),
            self._ingest_state_markdown(root),
            gr.Dropdown(choices=self.resumable_ingest_choices(root)),
        )

    def ingest_retry_view(self, signature: str | None, job_id: str | None) -> str:
        """명시적 재시도 — 같은 작업 ID·같은 지문으로 다시 붙고 ready는 건너뛴다."""
        root = self._require_current_selection(signature)
        if not job_id:
            raise gr.Error("재시도할 가져오기 작업을 고르세요.")
        try:
            handle = self.raw.retry_ingest_copy(root, job_id)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        return f"재시도 제출 — `{handle.job_id}`. 진행은 새로고침으로 확인하세요."

    def resumable_ingest_choices(self, root: Path | None = None) -> list[tuple[str, str]]:
        """재시도 가능한 가져오기 작업 — 실패·중단·이어하기 상태만 골라 보인다."""
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return []
        resumable = {"failed", "interrupted", "paused"}
        return [
            (
                f"{job.job_id} · {job.status} · {job.progress_current}/{job.progress_total}",
                job.job_id,
            )
            for job in self.raw.list_jobs(root)
            if job.kind == raw_service.INGEST_JOB_KIND and job.status in resumable
        ]

    # ── 화면 2 — 결정적 분할 계획·추출(오프라인) ─────────────────

    # ── 화면 2 — 의미 분석(오프라인 재생 기본, 실제 호출은 승인만) ──

    _SPEECH_LABELS: dict[str, str] = {
        "speech_detected": "발화 확인",
        "unconfirmed_empty": "빈 골격(미확인)",
        "analysis_failed": "분석 실패",
        "audio_absent": "오디오 없음",
    }
    _EXECUTION_LABELS: dict[str, str] = {
        "offline_replay": "오프라인 재생",
        "online_live": "실제 호출",
        "mixed_live_and_replay": "혼합(재생+실제)",
    }
    _SOURCE_STATUS_LABELS: dict[str, str] = {
        "pending": "대기",
        "analyzing": "분석 중",
        "transcribed": "전사 완료(2단계 대기)",
        "analyzed": "분석 완료",
        "cached": "캐시",
        "failed": "실패",
        "unanalyzed": "미선택 보존",
    }
    _REVISION_STATUS_LABELS: dict[str, str] = {
        "reserved": "예약",
        "analyzing": "실행 중",
        "awaiting_semantic": "의미 승인 대기",
        "complete": "완성",
        "failed": "실패",
        "interrupted": "중단",
    }

    def analysis_source_choices(self, root: Path | None = None) -> list[tuple[str, str]]:
        """분석 선택 목록 — 확정 전체 순서의 ready 원본(순서 그대로)."""
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return []
        try:
            order = self.raw.latest_global_source_order(root)
        except raw_service.RawWorkspaceError:
            return []
        if order is None:
            return []
        names = {asset.asset_id: asset.source_name for asset in self.raw.list_source_assets(root)}
        return [
            (
                f"{entry.ordinal:03d} · {names.get(entry.asset_id, entry.asset_id)}",
                entry.asset_id,
            )
            for entry in sorted(order.entries, key=lambda item: item.ordinal)
        ]

    def _analysis_settings_markdown(self, root: Path | None = None) -> str:
        """분석 설정 요약 — 동결 기본값(15초 창·2초 문맥·8장·768)과 모델 표시."""
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return (
                "분석 설정(동결 기본값): 시간창 15초 · 문맥 앞뒤 2초 · 창당 프레임 8장 ·"
                " 긴변 768px — 작업공간을 열면 모델까지 함께 보인다."
            )
        config = self.raw.settings(root)
        return (
            f"분석 설정(동결 기본값): 시간창 15초 · 문맥 앞뒤 2초 · 창당 프레임 8장 ·"
            f" 긴변 768px · 내부 ASR `{config.models.timing_model}`(단일 패스) ·"
            f" 의미 판정 `{config.models.luna_model}` — 최종 자막 파이프라인은"
            " 아니다(이 단계의 산출은 요약·태그·경계 제안뿐)\n\n"
            f"- 수명주기: 시간 제공자 종료 예정 **{config.registry.timing_shutdown_date}**"
            " — 이후 신규 온라인 전사(1단계)는 차단된다(§11.10)\n"
            f"- 네트워크 **{config.network_mode}** · Luna 단가 **미확인** — 금액 추정을"
            " 만들지 않는다(0원 계산 금지). 실제 호출은 1단계(전사)·2단계(의미)"
            " 승인이 각각 사람 기록으로만 열린다."
        )

    def _current_network_mode(self) -> str:
        """네트워크 모드 칸의 초기값 — 작업공간이 없으면 기본 offline."""
        root = self._snapshot_root()
        if root is None:
            return "offline"
        try:
            return self.raw.settings(root).network_mode
        except raw_service.RawWorkspaceError:
            return "offline"

    def _analysis_state_markdown(self, root: Path | None) -> str:
        """분석 패널 보기 — 리비전·원본별 상태·요약 읽기(요약·태그는 읽기 전용)."""
        if root is None:
            return (
                "작업공간을 열면 의미 분석 상태가 여기 나타난다 — 브라우저에서는"
                " **오프라인 재생**만 실행할 수 있다(실제 호출은 사람 승인 기록이"
                " 필요하다)."
            )
        try:
            summary = self.raw.analysis_overview(root)
        except raw_service.RawWorkspaceError as error:
            return f"분석 상태를 읽지 못했다: {error}"
        if summary is None:
            return (
                "**의미 분석** 리비전이 없다 — **분석 견적(로컬)**을 본 뒤 **오프라인"
                " 재생 분석 시작**을 누르면 시작된다. 견적은 로컬 조사만 하고 유료"
                " 호출은 없다."
            )
        revision = summary.revision
        mode_label = "오프라인 재생" if revision.mode == "replay" else "실제 호출(두 단계 승인)"
        status_label = self._REVISION_STATUS_LABELS.get(revision.status, revision.status)
        lines = [
            f"**의미 분석** — 리비전 `{revision.revision_id}` · 모드 **{mode_label}** ·"
            f" 상태 **{status_label}** · 원본 {revision.source_done}/{revision.source_total}",
            "",
            "| 순번 | 원본 | 상태 | 발화 상태 | 창 | 조각 | 실행 증거 |",
            "|---|---|---|---|---|---|---|",
        ]
        for result in summary.results:
            speech = self._SPEECH_LABELS.get(result.speech_status or "", "-")
            execution = self._EXECUTION_LABELS.get(result.execution_mode or "", "-")
            status = self._SOURCE_STATUS_LABELS.get(result.status, result.status)
            lines.append(
                f"| {result.source_order} | {result.source_name} | {status} |"
                f" {speech} | {result.window_count} | {result.segment_count}"
                f" | {execution} |"
            )
        if revision.status == "awaiting_semantic":
            lines += [
                "",
                "**의미 승인 대기** — 1단계(전사)가 끝났고 Luna는 아직 한 번도 가지"
                " 않았다. **2단계 견적(로컬·전사 후 실측)**으로 원본별 실측 지문·창·"
                " 이미지 수를 보고, 사람이 만든 2단계 승인을 주입한 뒤 위의"
                " 재개 대상에 1단계 작업을 골라 **실제 호출 2단계(의미) 재개**를"
                " 누른다. 잘못된 승인은 전사도 Luna도 0송신으로 거부된다.",
            ]
        if revision.error:
            lines += ["", f"**오류**: {revision.error}"]
        for result in summary.results[:4]:
            excerpt = self._annotation_excerpt(root, summary.revision.revision_id, result)
            if excerpt:
                lines += ["", excerpt]
        return "\n".join(lines)

    def _annotation_excerpt(
        self, root: Path, revision_id: str, result: raw_service.AnalysisSourceResult
    ) -> str:
        """원본 하나의 요약·태그 읽기 — 문서의 accepted 판정 앞두어만(표시 전용)."""
        if result.status not in ("analyzed", "cached"):
            return ""
        try:
            document = self.raw.analysis_document(root, revision_id, result.asset_id)
        except raw_service.RawWorkspaceError:
            return ""
        if not document:
            return ""
        annotations = document.get("annotations")
        if not isinstance(annotations, list):
            return ""
        lines: list[str] = []
        for item in annotations:
            if not isinstance(item, dict) or item.get("status") != "accepted":
                continue
            tags = ", ".join(str(tag) for tag in item.get("scene_tags", [])[:4])
            speech = str(item.get("speech_summary", "")).strip()
            visual = str(item.get("visual_summary", "")).strip()
            lines.append(
                f"- `{result.source_name}` {item.get('window_id')}: {speech or '(발화 요약 없음)'}"
                f" · {visual or '(장면 요약 없음)'}{(' · ' + tags) if tags else ''}"
            )
            if len(lines) >= 3:
                break
        return "\n".join([f"**{result.source_name} 요약(읽기 전용)**", *lines]) if lines else ""

    def analysis_estimate_view(self, signature: str | None, selected: list[str] | None) -> str:
        """분석 견적 이벤트 — 로컬 조사만으로 창·청크·이미지 수와 비용 상태를 보인다."""
        root = self._require_current_selection(signature)
        try:
            estimates = self.raw.analysis_estimate(
                root, asset_ids=self._selected_asset_ids(selected)
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        lines = [
            "**분석 견적(로컬 — 실제 호출 없음)**",
            "",
            "| 순번 | 원본 | 오디오 | 창 | 이미지(상한) | ASR 청크 | 오디오 분량(초) | Luna 요청 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for item in estimates:
            lines.append(
                f"| {item.source_order} | {item.source_name} |"
                f" {'있음' if item.has_audio else '없음'} | {item.window_count} |"
                f" {item.frame_sample_count} | {item.asr_chunk_count} | {item.asr_seconds} |"
                f" {item.luna_request_count} |"
            )
        first = estimates[0] if estimates else None
        price = first.price_status if first else "unknown"
        ceiling = first.user_ceiling_total if first else None
        price_note = (
            f"사용자 상한 합 {ceiling}"
            if ceiling
            else "Luna 단가 미확인 — 금액 추정을 만들지 않는다"
        )
        lines += [
            "",
            f"- 비용 상태: **{price}** — {price_note}(0원 계산 금지).",
            "- 실제 호출은 이 견적을 본 사람의 승인 기록(원본별 승인·예산)과 환경"
            " 설정이 함께 열려 있어야만 시작한다 — 브라우저에서는 만들 수 없다.",
        ]
        if first is not None and first.note:
            lines.append(f"- {first.note}")
        return "\n".join(lines)

    def analysis_start_view(
        self, signature: str | None, selected: list[str] | None
    ) -> tuple[str, gr.Dropdown]:
        """오프라인 재생 분석 시작 — 명시적 합성 픽스처만 재생한다(실제 호출 아님)."""
        root = self._require_current_selection(signature)
        try:
            handle = self.raw.start_analysis(
                root, asset_ids=self._selected_asset_ids(selected), mode="replay"
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        message = (
            f"분석 작업 `{handle.job_id}` 제출(오프라인 재생)"
            + (" (기존 활성 작업에 다시 붙음)" if handle.reattached else "")
            + " — `analysis/replay/`의 명시적 픽스처만 재생하며 없는 창·청크는"
            " 실패로 기록된다(지어내지 않는다)."
        )
        return (
            message + "\n\n" + self._analysis_state_markdown(root),
            gr.Dropdown(choices=self.resumable_analysis_choices(root)),
        )

    def analysis_refresh_view(
        self, signature: str | None
    ) -> tuple[str, gr.Dropdown, gr.CheckboxGroup]:
        """분석 새로고침 — 상태는 DB가 정본, 화면은 다시 읽기만 한다."""
        root = self._require_current_selection(signature)
        return (
            self._analysis_state_markdown(root),
            gr.Dropdown(choices=self.resumable_analysis_choices(root)),
            gr.CheckboxGroup(choices=self.analysis_source_choices(root)),
        )

    def analysis_retry_view(self, signature: str | None, job_id: str | None) -> str:
        """분석 재시도 이벤트 — 완료 원본은 캐시로, 실패 원본만 다시 분석한다."""
        root = self._require_current_selection(signature)
        if not job_id:
            raise gr.Error("재시도할 분석 작업을 고르세요.")
        try:
            handle = self.raw.retry_analysis(root, job_id)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        return (
            f"재시도 제출 — `{handle.job_id}`. 같은 지문의 완료 원본은 캐시로 이어"
            " 붙고(다시 부르지 않는다) 실패 원본만 다시 분석한다."
        )

    # ── 실제 호출(두 단계 승인) — 주입된 전송 경계가 있을 때만 움직인다 ──

    def _live_transport_for_action(self) -> raw_service.LiveAnalysisTransport:
        """이 행동이 쓸 실제 호출 재료 — 주입 transport가 우선, 없으면 생산.

        생산 transport는 사람이 승인한 행동 안에서 늦게 만든다. 키는 서버를
        띄운 환경에서 **시작 때** 찍어 둔 것뿐이다 — 게이트가 `.env`를 적재한
        뒤라도 이 문은 그 파일로 열리지 않는다(A4 생산 갭).
        """
        if self._live_transport is not None:
            return self._live_transport
        if self._startup_credentials is None:
            raise gr.Error(
                "실제 호출에는 서버를 띄운 환경의 OPENAI_API_KEY가 필요하다 —"
                " 키를 내보내고 브라우저 서버를 다시 띄우세요. 이 화면은 .env"
                " 파일을 읽지 않는다(키는 시작 환경에서만 온다)."
            )
        return raw_service.live_transport_for_credentials(
            self._startup_credentials,
            asr_approvals=self._production_asr_approvals,
            luna_approvals=self._production_luna_approvals,
        )

    # ── 생산 승인 공급자 — 서버가 간직한 견적 기록에서 주조한다 ──

    def _require_asr_record(self, root: Path) -> _AsrApprovalRecord:
        """1단계 승인 기록 — 없거나 다른 작업공간 것이면 0송신으로 닫는다."""
        record = self._asr_approval_record
        if record is None:
            raise gr.Error(
                "1단계 승인 기록이 없다 — 먼저 **실제 호출 1단계 견적(승인용)**을"
                " 보고, 확인란을 체크한 뒤 시작하세요."
            )
        if self._signature_for(root) != record.signature:
            raise gr.Error("승인 기록이 다른 작업공간의 것이다 — 새 견적을 보고 다시 승인받으세요.")
        return record

    def _production_asr_approvals(
        self, root: Path, asset_ids: Sequence[str] | None
    ) -> dict[str, object]:
        """브라우저가 만든 1단계 승인 — 승인 때 본 견적과 지금 견적이 같을 때만."""
        record = self._require_asr_record(root)
        approved = {item.asset_id for item in record.estimates}
        requested = list(asset_ids) if asset_ids else sorted(approved)
        unknown = [item for item in requested if item not in approved]
        if unknown:
            raise gr.Error(
                f"승인하지 않은 원본이 있다: {', '.join(unknown[:3])} — 새 견적을"
                " 보고 다시 승인받으세요."
            )
        fresh = self.raw.analysis_estimate(root, requested)
        approved_subset = tuple(
            item for item in record.estimates if item.asset_id in set(requested)
        )
        if raw_service.asr_estimate_digest(fresh) != raw_service.asr_estimate_digest(
            approved_subset
        ):
            raise gr.Error(
                "견적이 승인 때와 다르다(원본·설정·모델이 바뀌었다) — 새 견적을 보고"
                " 다시 승인받으세요. 이 요청은 0송신으로 거부되었다."
            )
        return dict(raw_service.mint_asr_approvals(self.raw, root, requested))

    def _production_luna_approvals(
        self, root: Path, revision_id: str, asset_ids: Sequence[str]
    ) -> dict[str, object]:
        """브라우저가 만든 2단계 승인 — 실측 지문이 승인 때 그대로일 때만."""
        record = self._semantic_approval_record
        if record is None:
            raise gr.Error(
                "2단계 승인 기록이 없다 — 먼저 **2단계 견적(로컬·전사 후 실측)**을"
                " 보고, 확인란을 체크한 뒤 재개하세요."
            )
        if self._signature_for(root) != record.signature:
            raise gr.Error("승인 기록이 다른 작업공간의 것이다 — 새 견적을 보고 다시 승인받으세요.")
        if revision_id != record.revision_id:
            raise gr.Error(
                "승인 기록이 다른 분석 리비전 것이다 — 새 견적을 보고 다시 승인받으세요."
            )
        approved = {item.asset_id for item in record.estimates}
        requested = list(asset_ids) if asset_ids else sorted(approved)
        unknown = [item for item in requested if item not in approved]
        if unknown:
            raise gr.Error(
                f"승인하지 않은 원본이 있다: {', '.join(unknown[:3])} — 새 견적을"
                " 보고 다시 승인받으세요."
            )
        fresh = self.raw.analysis_semantic_estimate(root, revision_id)
        fresh_subset = tuple(item for item in fresh if item.asset_id in set(requested))
        approved_subset = tuple(
            item for item in record.estimates if item.asset_id in set(requested)
        )
        if raw_service.semantic_estimate_digest(
            fresh_subset
        ) != raw_service.semantic_estimate_digest(approved_subset):
            raise gr.Error(
                "2단계 실측 지문이 승인 때와 다르다 — 새 견적을 보고 다시 승인받으세요."
                " 이 요청은 전사도 Luna도 0송신으로 거부되었다."
            )
        return dict(raw_service.mint_luna_approvals(self.raw, root, revision_id, requested))

    def analysis_live_estimate_view(self, signature: str | None, selected: list[str] | None) -> str:
        """실제 호출 1단계 견적(승인용) — 사람이 승인할 측정값을 서버가 잰다."""
        root = self._require_current_selection(signature)
        try:
            estimates = self.raw.analysis_estimate(root, self._selected_asset_ids(selected))
            workflow = self.raw.settings(root)
            assets = {a.asset_id: a for a in self.raw.list_source_assets(root)}
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        digest = raw_service.asr_estimate_digest(estimates)
        self._asr_approval_record = _AsrApprovalRecord(
            signature=self._signature_for(root) or "",
            estimates=tuple(estimates),
            digest=digest,
        )
        lines = [
            "**실제 호출 1단계(전사) 견적 — 승인이 묶는 값(로컬 측정, 호출 없음)**",
            "",
            "| 순번 | 원본 | 출처 해시(앞 12자) | 오디오 | 스트림 | 청크 예산 | 창 |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in estimates:
            asset = assets.get(item.asset_id)
            sha = (asset.content_sha256 or "") if asset is not None else ""
            audio = "있음" if item.has_audio else "없음(전사 없음)"
            stream = str(item.audio_stream_index) if item.audio_stream_index is not None else "-"
            lines.append(
                f"| {item.source_order} | {item.source_name} | `{sha[:12]}…` | {audio}"
                f" | {stream} | {max(1, item.asr_chunk_count)} | {item.window_count} |"
            )
        lines += [
            "",
            f"- 내부 ASR `{workflow.models.timing_model}` · 창 계획은 1단계가 잰 뒤"
            " 2단계 승인이 다시 묶는다.",
            f"- 수명주기: 시간 제공자 종료 예정 **{workflow.registry.timing_shutdown_date}**"
            " — 이후 신규 1단계는 차단된다(§11.10).",
            f"- 작업공간 네트워크 **{workflow.network_mode}** · Luna 단가 **미확인**"
            " — 금액 추정을 만들지 않는다. 환경 `VLOGSRT_OFFLINE`은 설정과 무관하게"
            " 문을 닫는다(시작 게이트가 판정한다).",
            f"- 견적 지문 `{digest[:12]}…` — 이 표를 본 뒤 확인란을 체크하고"
            " **실제 호출 1단계(전사) 시작**을 누르면 승인으로 남는다. 그 사이"
            " 원본·설정이 바뀌면 시작이 0송신으로 거부된다.",
        ]
        return "\n".join(lines)

    def analysis_live_asr_view(
        self, signature: str | None, selected: list[str] | None, confirm: bool
    ) -> tuple[str, gr.Dropdown]:
        """실제 호출 1단계(전사) — 브라우저 승인(견적 표+확인란)이 문을 연다."""
        root = self._require_current_selection(signature)
        if not confirm:
            raise gr.Error(
                "1단계 승인 확인이 필요하다 — 위 견적을 보고 '위 견적 그대로 전사를"
                " 승인한다' 확인란을 체크하세요."
            )
        transport = self._live_transport_for_action()
        asset_ids = self._selected_asset_ids(selected)
        try:
            approvals = dict(transport.asr_approvals(root, asset_ids))
            handle = self.raw.start_analysis(
                root,
                asset_ids=asset_ids,
                mode="live",
                asr_approvals=approvals,
                sender=transport.sender,
                cfg=transport.cfg,
                on=transport.on,
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        message = (
            f"1단계(전사) 작업 `{handle.job_id}` 제출"
            + (" (기존 활성 작업에 다시 붙음)" if handle.reattached else "")
            + " — 전사·프레임만 남기고 의미 승인 대기에서 멈춘다(Luna 0호출)."
            " 전사 기록이 있는 원본은 다시 부르지 않는다."
        )
        return (
            message + "\n\n" + self._analysis_state_markdown(root),
            gr.Dropdown(choices=self.resumable_analysis_choices(root)),
        )

    def analysis_semantic_estimate_view(self, signature: str | None) -> str:
        """2단계 견적 — 1단계 기록의 실측 지문·창·이미지·비용 상태(로컬만)."""
        root = self._require_current_selection(signature)
        try:
            summary = self.raw.analysis_overview(root)
            estimates = self.raw.analysis_semantic_estimate(
                root, summary.revision.revision_id if summary else None
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        if not estimates:
            return (
                "**2단계 견적** — 아직 1단계(전사)가 끝난 원본이 없다. 실제 호출"
                " 1단계를 마친 뒤 여기에 원본별 실측 지문·창·이미지 수가 나타난다."
            )
        digest = raw_service.semantic_estimate_digest(estimates)
        self._semantic_approval_record = _SemanticApprovalRecord(
            signature=self._signature_for(root) or "",
            revision_id=summary.revision.revision_id if summary else "",
            estimates=tuple(estimates),
            digest=digest,
        )
        lines = [
            "**2단계(의미 Luna) 견적 — 1단계 실측 기록 기준(로컬, 호출 없음)**",
            "",
            "| 순번 | 원본 | 단계 | 실측 지문(앞 12자) | 창 | 이미지 | Luna 요청 |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in estimates:
            phase_label = "의미(전사+이미지)" if item.phase == "semantic" else "시각(이미지만)"
            lines.append(
                f"| {item.source_order} | {item.source_name} | {phase_label} |"
                f" `{item.semantic_fingerprint[:12]}…` | {item.window_count} |"
                f" {item.image_count} | {item.luna_request_count} |"
            )
        lines += [
            "",
            f"- Luna 모델 `{estimates[0].luna_model}` · 비용 상태 **{estimates[0].price_status}**"
            " — 미확인 단가는 금액을 만들지 않는다(0원 계산 금지).",
            "- 2단계 승인은 이 표의 **실측 지문 전체값**을 묶는다 — 이 표를 본 뒤"
            " 확인란을 체크하고 재개하면 승인으로 남는다. 승인이 본 값과 1단계"
            " 기록이 다르면 재개가 0송신으로 거부된다.",
            f"- 견적 지문 `{digest[:12]}…`",
        ]
        return "\n".join(lines)

    def analysis_semantic_resume_view(
        self, signature: str | None, job_id: str | None, confirm: bool
    ) -> tuple[str, gr.Dropdown]:
        """실제 호출 2단계(의미) 재개 — 두 번째 승인이 실측 지문을 검증받는다."""
        root = self._require_current_selection(signature)
        if not confirm:
            raise gr.Error(
                "2단계 승인 확인이 필요하다 — 위 2단계 견적의 실측 지문을 보고"
                " '위 실측 지문으로 Luna 호출을 승인한다' 확인란을 체크하세요."
            )
        transport = self._live_transport_for_action()
        if not job_id:
            raise gr.Error(
                "재개 대상 분석 작업을 고르세요 — 의미 승인 대기 리비전의 1단계 작업이다."
            )
        summary = self.raw.analysis_overview(root)
        selected: list[str] = []
        if summary is not None:
            selected = [
                row.asset_id for row in summary.results if row.status not in ("unanalyzed",)
            ]
        try:
            approvals: dict[str, object] = {}
            if summary is not None:
                approvals = dict(
                    transport.luna_approvals(root, summary.revision.revision_id, selected)
                )
            handle = self.raw.resume_analysis_semantic(
                root,
                job_id,
                luna_approvals=approvals,
                client=transport.client,
                on=transport.on,
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        message = (
            f"2단계(의미) 작업 `{handle.job_id}` 제출"
            + (" (기존 활성 작업에 다시 붙음)" if handle.reattached else "")
            + " — 1단계 전사 기록을 다시 검증해 재사용한다(ASR 0호출)."
        )
        return (
            message + "\n\n" + self._analysis_state_markdown(root),
            gr.Dropdown(choices=self.resumable_analysis_choices(root)),
        )

    def settings_network_view(self, signature: str | None, mode: str | None) -> str:
        """작업공간 네트워크 모드 변경 — 환경 스위치는 우회할 수 없다(§14.3)."""
        root = self._require_current_selection(signature)
        if mode not in ("offline", "online"):
            raise gr.Error(f"알 수 없는 네트워크 모드다: {mode!r} (offline 또는 online)")
        try:
            config = self.raw.settings(root)
            self.raw.update_settings(root, replace(config, network_mode=mode))
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        note = f"작업공간 네트워크 모드를 **{mode}**(으)로 바꿨다."
        if mode == "online":
            note += (
                " 환경 `VLOGSRT_OFFLINE`이 닫혀 있으면 설정과 무관하게 실제 호출은"
                " 여전히 0송신이다 — 환경 스위치는 사람의 손으로만 움직인다(§14.3)."
            )
        return note + "\n\n" + self._analysis_settings_markdown(root)

    def analysis_plan_view(self, signature: str | None) -> str:
        """분석 결과를 A3 분할 계획으로 — 선택 밖·실패 원본은 전체 보존 한 조각."""
        root = self._require_current_selection(signature)
        try:
            snapshot = self.raw.plan_segmentation_from_analysis(root)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        _ = snapshot  # 성공 증거는 갱신된 분할 패널 자체다
        return self._segment_state_markdown(root)

    @staticmethod
    def _selected_asset_ids(selected: Sequence[str] | None) -> list[str] | None:
        """체크 선택 — 비었거나 None이면 전체(서비스가 전체로 해석한다)."""
        if not selected:
            return None
        return [str(item) for item in selected]

    def resumable_analysis_choices(self, root: Path | None = None) -> list[tuple[str, str]]:
        """재시도·2단계 재개 대상 분석 작업 — 대기 리비전의 1단계 작업도 보인다."""
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return []
        resumable = {"failed", "interrupted", "paused"}
        kinds = {raw_service.ANALYSIS_JOB_KIND, raw_service.ANALYSIS_SEMANTIC_JOB_KIND}
        awaiting_fingerprint: str | None = None
        try:
            summary = self.raw.analysis_overview(root)
        except raw_service.RawWorkspaceError:
            summary = None
        if summary is not None and summary.revision.status == "awaiting_semantic":
            awaiting_fingerprint = summary.revision.fingerprint
        choices: list[tuple[str, str]] = []
        for job in self.raw.list_jobs(root):
            in_awaiting = (
                awaiting_fingerprint is not None
                and job.kind == raw_service.ANALYSIS_JOB_KIND
                and job.input_fingerprint == awaiting_fingerprint
            )
            if job.kind not in kinds or not (job.status in resumable or in_awaiting):
                continue
            stage = "2단계" if job.kind == raw_service.ANALYSIS_SEMANTIC_JOB_KIND else "1단계"
            suffix = " · 의미 승인 대기" if in_awaiting else ""
            choices.append(
                (
                    f"{job.job_id} · {stage} · {job.status}"
                    f" · {job.progress_current}/{job.progress_total}{suffix}",
                    job.job_id,
                )
            )
        return choices

    def _segment_state_markdown(self, root: Path | None) -> str:
        """분할 패널 보기 — 계획·추출 작업·게시된 조각을 DB에서 다시 읽는다."""
        if root is None:
            return (
                "작업공간을 열면 결정적 분할 계획이 여기 나타난다 — 의미 분석 없이,"
                " 유료 호출 없이(오프라인)."
            )
        lines: list[str] = []
        try:
            plans = self.raw.list_segment_plan_revisions(root)
            summary = self.raw.package_overview(root)
        except raw_service.RawWorkspaceError as error:
            return f"분할 상태를 읽지 못했다: {error}"
        order = self.raw.latest_global_source_order(root)
        if order is None:
            lines.append(
                "**확정된 전체 원본 순서가 없다** — 1번 탭에서 가져오기와 전체 순서"
                " 확정을 먼저 끝내야 계획이 만들어진다."
            )
        if not plans:
            lines.append("분할 계획이 없다 — **결정적 분할 계획**을 누르면 만든다.")
            return "\n".join(lines)
        latest = plans[-1]
        lines += [
            f"**분할 계획** — 리비전 `{latest.revision_id}` · 근거 {latest.basis} ·"
            f" 순서 리비전 `{latest.order_revision_id}` · 조각 {len(latest.entries)}개"
            f" (리비전 총 {len(plans)}개)",
            "",
            "| 원본 순번 | 조각 | 원본 pts 범위 | 덮음 |",
            "|---|---|---|---|",
        ]
        by_source: dict[int, list[raw_service.SegmentPlanEntry]] = {}
        for entry in latest.entries:
            by_source.setdefault(entry.source_order, []).append(entry)
        for source_order, entries in sorted(by_source.items()):
            first, last = entries[0], entries[-1]
            time_base = f"{first.time_base_num}/{first.time_base_den}"
            lines.append(
                f"| {source_order} | {len(entries)}개 | [{first.core_start_pts},"
                f" {last.core_end_pts}) @ {time_base} |"
                f" {len(entries) == _coverage_count(latest, source_order)} |"
            )
        jobs = [job for job in self.raw.list_jobs(root) if job.kind == raw_service.SEGMENT_JOB_KIND]
        if jobs:
            lines += ["", "**추출 작업**"]
            lines += [
                f"- `{job.job_id}` {job.status} {job.progress_current}/{job.progress_total}"
                + (f" — {job.error}" if job.error else "")
                for job in jobs[:8]
            ]
        if summary is not None:
            package = summary.package
            lines += [
                "",
                f"**package** `{package.package_dir}` · 상태 {package.status} · 게시"
                f" {package.clip_published}/{package.clip_total} · 등록 미디어로 재생 가능"
                f" {sum(1 for clip in summary.clips if clip.file_id)}개",
            ]
            broken = [clip for clip in summary.clips if clip.status != "published"]
            for clip in broken[:5]:
                lines.append(f"- 미게시 `{clip.clip_id}` — {clip.error or clip.status}")
        return "\n".join(lines)

    def segment_plan_view(self, signature: str | None, intervals_text: str) -> str:
        """분할 계획 이벤트 — 확정 전체 순서 위에서 결정적 계획 리비전을 만든다."""
        root = self._require_current_selection(signature)
        intervals = _parse_intervals(intervals_text)
        try:
            snapshot = self.raw.plan_segmentation(root, intervals=intervals)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        _ = snapshot  # 성공 증거는 갱신된 패널 자체다
        return self._segment_state_markdown(root)

    def segment_extract_view(
        self, signature: str | None, profile_name: str | None
    ) -> tuple[str, gr.Dropdown, str]:
        """조각 추출 시작 이벤트 — 로컬 ffmpeg 순차 추출 작업을 제출한다(유료 호출 없음).

        제어된 실패(완성 package 경계 등)는 예외로 올리지 않고 패널 마크다운으로
        돌린다 — gr.Error는 출력 칸(재시도 목록·package 상태)을 무효화해 브라우저에
        Error 표시를 남긴다(루트 브라우저 재현). 내용은 그대로 정직하게 보인다.
        """
        root = self._require_current_selection(signature)
        try:
            handle = self.raw.start_segment_extraction(
                root, profile_name=profile_name or "source_matched"
            )
        except raw_service.RawWorkspaceError as error:
            return (
                f"**추출을 시작하지 못했다** — {error}\n\n"
                "완성 package 경계라면 같은 형상이라도 **새 계획 리비전**에서 다시"
                " 시작하면 새 package로 뽑는다(옛 package는 불변).\n\n"
                + self._segment_state_markdown(root),
                gr.Dropdown(choices=self.resumable_extract_choices(root)),
                self._package_state_markdown(root),
            )
        message = (
            f"추출 작업 `{handle.job_id}` 제출"
            + (" (기존 활성 작업에 다시 붙음)" if handle.reattached else "")
            + " — 진행은 새로고침으로 확인하세요."
        )
        return (
            message + "\n\n" + self._segment_state_markdown(root),
            gr.Dropdown(choices=self.resumable_extract_choices(root)),
            self._package_state_markdown(root),
        )

    def segment_refresh_view(
        self, signature: str | None
    ) -> tuple[str, gr.Dropdown, str, gr.Dropdown]:
        """진행 새로고침 — 서버(DB)가 정본, 화면은 다시 읽기만 한다."""
        root = self._require_current_selection(signature)
        return (
            self._segment_state_markdown(root),
            gr.Dropdown(choices=self.resumable_extract_choices(root)),
            self._package_state_markdown(root),
            gr.Dropdown(choices=self.clip_choices(root)),
        )

    def segment_retry_view(self, signature: str | None, job_id: str | None) -> str:
        """추출 재시도 이벤트 — 같은 작업 ID·같은 지문, 게시물은 재검증만 한다."""
        root = self._require_current_selection(signature)
        if not job_id:
            raise gr.Error("재시도할 추출 작업을 고르세요.")
        try:
            handle = self.raw.retry_segment_extraction(root, job_id)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        return (
            f"재시도 제출 — `{handle.job_id}`. 게시된 조각은 해시 재검증만 지나고"
            " 번호·ID는 그대로다."
        )

    def resumable_extract_choices(self, root: Path | None = None) -> list[tuple[str, str]]:
        """재시도 가능한 추출 작업 — 실패·중단 상태만 골라 보인다."""
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return []
        resumable = {"failed", "interrupted", "paused"}
        return [
            (
                f"{job.job_id} · {job.status} · {job.progress_current}/{job.progress_total}",
                job.job_id,
            )
            for job in self.raw.list_jobs(root)
            if job.kind == raw_service.SEGMENT_JOB_KIND and job.status in resumable
        ]

    def clip_choices(self, root: Path | None = None) -> list[tuple[str, str]]:
        """게시된 조각의 재생 목록 — 등록 미디어 ID로만 제공된다(§4.3)."""
        root = root if root is not None else self._snapshot_root()
        if root is None:
            return []
        try:
            summary = self.raw.package_overview(root)
        except raw_service.RawWorkspaceError:
            return []
        if summary is None:
            return []
        return [
            (
                f"{clip.source_order}/{clip.segment_order:03d} · {clip.relative_path}",
                str(clip.file_id),
            )
            for clip in summary.clips
            if clip.file_id is not None and clip.status == "published"
        ]

    # ── 화면 3 — 편집용 폴더(package) ───────────────────────────

    def _package_state_markdown(self, root: Path | None) -> str:
        """package 패널 보기 — 폴더·수·길이·전달물·검증 결과를 숨기지 않고(§9.3)."""
        if root is None:
            return "추출이 끝나면 편집용 폴더의 구성이 여기 나타난다."
        try:
            summary = self.raw.package_overview(root)
        except raw_service.RawWorkspaceError as error:
            return f"package 상태를 읽지 못했다: {error}"
        if summary is None:
            return "package가 없다 — 2번 탭에서 결정적 분할 계획과 조각 추출을 실행하세요."
        package = summary.package
        lines = [
            f"**package** `{package.package_dir}` · 상태 **{package.status}**",
            f"- 분할 계획 `{package.plan_revision_id}` · 전체 순서 `{package.order_revision_id}`",
            f"- 프로파일 `{package.profile_name}` · 핸들 앞 {package.handles_before}초 /"
            f" 뒤 {package.handles_after}초",
            f"- 게시 {package.clip_published}/{package.clip_total}개 · 총 길이"
            f" {summary.total_duration_seconds}초 · 사용 공간"
            f" {self._human_bytes(summary.total_bytes)}",
        ]
        if summary.manifest_relative_paths:
            delivered = ", ".join(f"`{item}`" for item in summary.manifest_relative_paths)
            lines.append(f"- 전달물: {delivered}")
        else:
            lines.append("- 전달물(clips.jsonl·clip_index.csv·README)은 완성 뒤 생긴다.")
        if package.error:
            lines.append(f"- **오류**: {package.error}")
        if summary.clips:
            lines += ["", "| 원본 순번 | 조각 | 상대 경로 | 상태 |", "|---|---|---|---|"]
            for clip in summary.clips[:20]:
                lines.append(
                    f"| {clip.source_order} | {clip.segment_order:03d} |"
                    f" `{clip.relative_path}` | {clip.status} |"
                )
            if len(summary.clips) > 20:
                lines.append(f"| … | … | {len(summary.clips) - 20}개 더 | |")
        return "\n".join(lines)

    def _latest_package_path(self, root: Path | None) -> str:
        """복사용 절대 경로 — package가 없으면 안내 문장으로 자리를 지킨다."""
        if root is None:
            return ""
        try:
            summary = self.raw.package_overview(root)
        except raw_service.RawWorkspaceError:
            return ""
        return str(root / summary.package.package_dir) if summary is not None else ""

    def package_refresh_view(self, signature: str | None) -> tuple[str, str]:
        """package 새로고침 이벤트 — 상태는 DB에서 다시 읽는다."""
        root = self._require_current_selection(signature)
        return self._package_state_markdown(root), self._latest_package_path(root)

    # ── 화면 3 · 화면 4 — 편집용 폴더 · 최종 자막 ────────────────

    def _package_tab(self) -> None:
        with gr.Tab("3 · 편집용 폴더"):
            self._package_md = gr.Markdown(self._package_state_markdown(self._snapshot_root()))
            self._package_path_box = gr.Textbox(
                label="편집용 폴더 절대 경로(복사용)",
                value=self._latest_package_path(self._snapshot_root()),
                interactive=True,
            )
            refresh_package = gr.Button("package 상태 새로고침")
            refresh_package.click(
                self.package_refresh_view,
                self._signature_state,
                [self._package_md, self._package_path_box],
            )
            gr.Markdown(
                "> **확정 내보내기** — 검토가 끝난 조각만, 서버 소유 내보내기 ID로"
                " 읽기전용 폴더에 베낀다(덮어쓰는 길이 없다). 원본 package는 그대로다."
            )
            with gr.Row():
                export_selected_box = gr.Textbox(
                    label="선택 조각 ID(쉼표 구분, 비우면 전체)",
                    placeholder="segment_000000001, segment_000000004",
                    scale=4,
                )
                export_confirm_box = gr.Checkbox(
                    label="위 대상을 읽기전용 폴더로 내보낸다",
                    interactive=True,
                    scale=3,
                )
                export_button = gr.Button("확정 내보내기", variant="primary", scale=2)
            with gr.Row():
                finder_open_button = gr.Button("Finder에서 열기", scale=2)
                copy_path_button = gr.Button("내보내기 경로 복사", scale=2)
                cleanup_confirm_box = gr.Checkbox(
                    label="cache/·previews/ 정리를 확인했다(확정본은 남는다)",
                    interactive=True,
                    scale=3,
                )
                cleanup_button = gr.Button("캐시 정리", scale=1)
            self._export_path_box = gr.Textbox(
                label="마지막 내보내기 절대 경로(복사용)",
                interactive=True,
            )
            self._export_md = gr.Markdown("_내보내기 결과가 여기 나타난다._")
            export_button.click(
                self.review_export_view,
                [self._signature_state, export_selected_box, export_confirm_box],
                [self._export_md, self._package_md],
            )
            finder_open_button.click(
                self.review_export_open_view,
                self._signature_state,
                self._export_md,
            )
            copy_path_button.click(
                self.review_export_copy_view,
                self._signature_state,
                self._export_path_box,
            )
            cleanup_button.click(
                self.review_cleanup_view,
                [self._signature_state, cleanup_confirm_box],
                self._export_md,
            )

    # ── 화면 2·3 — 검토 작업공간(W3 백엔드) — 정본은 파사드(DB)에 있다 ──

    def _review_facade(self, root: Path) -> Any:
        """검토 파사드 — 백엔드가 없는 빌드에서는 정직한 오류로 닫는다."""
        try:
            return self.raw.review_workspace_facade(root)
        except raw_service.WorkspaceModuleUnavailableError as error:
            raise gr.Error(str(error)) from error

    def _review_head_and_rows(self, root: Path) -> tuple[Any, list[dict[str, object]]]:
        """현재 헤드(`LoadedReview`)와 시간순 행 값 — 백엔드 형 그대로만 옮긴다.

        화면이 본 리비전 신원(헤드·기본 계획)과 편집 재료(원본별 시계·조각
        범위)를 여기서 한 번 잡아 서버에 둔다 — 경계·나누기 초 입력은 이 시계로
        틱(정수 PTS)으로 바뀐다. 칸이 없으면 조용히 비우지 않고 오류로 밝힌다.
        """
        facade = self.raw.review_workspace_facade(root)  # 그리기 경로 — 패널이 정직한 안내로 잡는다
        loaded = facade.load_head()
        revision = loaded.revision
        names = {a.asset_id: a.source_name for a in self.raw.list_source_assets(root)}
        clocks = {clock.source_id: clock for clock in revision.clocks}
        rows: list[dict[str, object]] = []
        spans: dict[str, tuple[str, int, int]] = {}
        for segment in revision.segments:
            clock = clocks.get(segment.source_id)
            if clock is None:
                raise gr.Error(
                    f"검토 리비전에 원본 {segment.source_id}의 시계가 없다 — 저장소가 깨졌다"
                )
            spans[segment.segment_id] = (segment.source_id, segment.start_pts, segment.end_pts)
            rows.append(
                {
                    "segment_id": segment.segment_id,
                    "source_order": segment.source_order,
                    "source_name": names.get(segment.source_id, segment.source_id),
                    "start_seconds": ui_facade.format_seconds(
                        Fraction(segment.start_pts - clock.origin_pts) * clock.time_base
                    ),
                    "end_seconds": ui_facade.format_seconds(
                        Fraction(segment.end_pts - clock.origin_pts) * clock.time_base
                    ),
                    "speech_status": segment.speech_status,
                    "selection": segment.selection,
                    "recommendation": segment.override_recommendation or segment.recommendation,
                    "visual_tags": ", ".join(segment.visual_tags),
                    "notes": segment.notes,
                }
            )
        self._review_clocks = clocks
        self._review_spans = spans
        self._review_revision_id = revision.revision_id
        self._review_base_plan_id = revision.base_plan_id
        self._review_signature = self._signature_for(root)
        return loaded, rows

    def _review_panel_markdown(self, root: Path, extra: str = "") -> str:
        """검토 패널 — 헤드 리비전·시간순 행 표·안내.

        패널은 **그리는 시점**(화면 조립 포함)에도 불러지므로 백엔드가 없거나
        리비전이 없으면 오류를 던지지 않고 안내 문구로 그린다 — 단추를 누른
        행동 경로는 여전히 정직한 오류로 닫힌다.
        """
        suffix = f"\n\n{extra}" if extra else ""
        try:
            loaded, rows = self._review_head_and_rows(root)
        except raw_service.WorkspaceModuleUnavailableError as error:
            return f"**검토 백엔드 없음** — {error}{suffix}"
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            return f"검토 상태를 읽지 못했다 — {error}{suffix}"
        revision = loaded.revision
        lines = [
            f"**검토** — 헤드 리비전 `{revision.revision_id}` · 기본 계획"
            f" `{revision.base_plan_id}` · 조각 {len(rows)}개 — 편집은 이 리비전에"
            " 묶인다(그 사이 바뀌면 거부된다).",
        ]
        if loaded.stale_sources:
            lines += [
                "",
                "**낡은 원본** — "
                + ", ".join(
                    f"`{source_id}` {reason}" for source_id, reason in loaded.stale_sources
                ),
            ]
        lines += ["", ui_facade.review_rows_markdown(rows)]
        if extra:
            lines += ["", extra]
        return "\n".join(lines)

    def review_initialize_view(self, signature: str | None) -> str:
        """검토 시작 — 최신 계획과 분석 증거에서 기본 리비전을 만든다."""
        root = self._require_current_selection(signature)
        facade = self._review_facade(root)
        try:
            facade.initialize_review()
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            raise gr.Error(str(error)) from error
        return self._review_panel_markdown(root, "검토 기본 리비전을 만들었다.")

    def review_refresh_view(self, signature: str | None) -> str:
        """검토 새로고침 — 헤드를 다시 읽기만 한다(편집 없음)."""
        root = self._require_current_selection(signature)
        return self._review_panel_markdown(root)

    def review_filter_view(self, signature: str | None, query: str, tags_text: str) -> str:
        """검색·태그 조회 — 순수 조회이며 원본 헤드는 그대로다."""
        root = self._require_current_selection(signature)
        facade = self._review_facade(root)  # 행동 경로 — 백엔드 없는 빌드는 정직한 오류로 닫는다
        try:
            loaded, rows = self._review_head_and_rows(root)
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            raise gr.Error(str(error)) from error
        query_text = (query or "").strip()
        tags = tuple(t.strip() for t in (tags_text or "").split(",") if t.strip())
        try:
            matched_segments = facade.filter(
                loaded.revision, query=query_text or None, tags=tags or ()
            )
        except raw_service.ReviewError as error:
            raise gr.Error(str(error)) from error
        kept = {segment.segment_id for segment in matched_segments}
        matched = [row for row in rows if row["segment_id"] in kept]
        tag_note = ", ".join(tags) or "(없음)"
        note = f"조회 `{query_text or '(전체)'}` · 태그 `{tag_note}` — {len(matched)}개"
        return "\n".join(
            [
                f"**검토** — 헤드 리비전 `{self._review_revision_id}`",
                "",
                ui_facade.review_rows_markdown(matched),
                "",
                note,
            ]
        )

    def _apply_review_edit(self, root: Path, build: Callable[[str], Any], label: str) -> str:
        """검토 편집 공통 꼬리 — 화면이 본 헤드에 묶어 적용하고 패널을 다시 그린다."""
        facade = self._review_facade(root)  # 백엔드 없는 빌드 — 정직한 오류가 먼저다
        if self._review_signature != self._signature_for(root):
            raise gr.Error("검토 기록이 다른 작업공간의 것이다 — 새로고침 뒤 다시 시도하세요.")
        if not self._review_revision_id or not self._review_base_plan_id:
            raise gr.Error("검토 리비전이 없다 — 먼저 **검토 시작**을 누르세요.")
        try:
            edited = facade.apply_edit(self._review_base_plan_id, build(self._review_revision_id))
        except raw_service.StaleRevisionError as error:
            raise gr.Error(
                f"편집이 늦었다 — 화면이 본 리비전 뒤에 다른 편집이 먼저 갔다({error})."
                " **검토 새로고침** 뒤 다시 편집하세요."
            ) from error
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            raise gr.Error(str(error)) from error
        return self._review_panel_markdown(
            root, f"**{label}** 편집을 적용했다 — 새 리비전 `{edited.revision_id}`."
        )

    def _require_segment_id(self, segment_id: str | None) -> str:
        cleaned = (segment_id or "").strip().strip("`")
        if not cleaned:
            raise gr.Error("조각 ID를 적으세요 — 검토 표의 `segment_…` 값 그대로.")
        return cleaned

    def _review_span(self, segment_id: str) -> tuple[str, int, int]:
        """화면이 본 리비전의 조각 범위 — (원본, 시작 틱, 끝 틱)."""
        span = self._review_spans.get(segment_id)
        if span is None:
            raise gr.Error(
                f"검토 표에 조각 `{segment_id}`이(가) 없다 — 새로고침 뒤 다시 시도하세요."
            )
        return span

    def _review_pts(self, source_id: str, seconds: Fraction, what: str) -> int:
        """초를 틱(정수 PTS)으로 — 원본 시계의 프레임 격자 위 점만 통과시킨다.

        몰래 반올림하지 않는다: 격자 밖 초는 이유(프레임 간격)와 함께 거부한다.
        백엔드의 격자 검사(`_require_frame_point`)와 같은 판정을 입력 자리에서
        미리 보여주는 것뿐이다.
        """
        clock = self._review_clocks.get(source_id)
        if clock is None:
            raise gr.Error(f"원본 {source_id}의 시계를 모른다 — 검토 새로고침 뒤 다시 시도하세요.")
        ticks = seconds / clock.time_base
        if ticks < 0:
            raise gr.Error(f"{what}이 음수다 — 0 이상의 초를 적으세요.")
        if ticks.denominator != 1 or int(ticks) % clock.frame_ticks != 0:
            frame_seconds = Fraction(clock.frame_ticks) * clock.time_base
            raise gr.Error(
                f"{what}({ui_facade.format_seconds(seconds)}초)가 프레임 격자 위가 아니다"
                f" — 이 원본은 {ui_facade.format_seconds(frame_seconds)}초 간격 격자다"
                "(정수 배수 초로 적으세요)."
            )
        return clock.origin_pts + int(ticks)

    def review_note_view(self, signature: str | None, segment_id: str, note: str) -> str:
        """메모 저장 — 메타데이터 편집(재추출 대상은 늘 없다)."""
        root = self._require_current_selection(signature)
        target = self._require_segment_id(segment_id)
        return self._apply_review_edit(
            root,
            lambda head: raw_service.review_note_edit(head, target, note or ""),
            "메모 저장",
        )

    def review_selection_view(self, signature: str | None, segment_id: str, choice: str) -> str:
        """사용·제외 — 조각은 지워지지 않고 선택 표시만 바뀐다(전체 보존)."""
        root = self._require_current_selection(signature)
        target = self._require_segment_id(segment_id)
        value = "excluded" if choice == "제외" else "included"
        return self._apply_review_edit(
            root,
            lambda head: raw_service.review_selection_edit(head, target, value),
            "선택 표시(" + choice + ")",
        )

    def review_boundary_view(
        self, signature: str | None, segment_id: str, seconds_text: str
    ) -> str:
        """경계 초 조정 — 맞닿은 이웃과의 공유 경계를 옮긴다(프레임 격자 위만)."""
        root = self._require_current_selection(signature)
        target = self._require_segment_id(segment_id)
        try:
            seconds = ui_facade.parse_seconds(seconds_text, what="경계 초")
        except ValueError as error:
            raise gr.Error(str(error)) from error

        def build(head: str) -> Any:
            source_id, _start, end = self._review_span(target)
            neighbor = next(
                (
                    other
                    for other, (asset, start, _other_end) in self._review_spans.items()
                    if other != target and asset == source_id and start == end
                ),
                None,
            )
            if neighbor is None:
                raise gr.Error(
                    f"이 조각의 오른쪽 이웃이 없다 — 마지막 조각의 경계는 옮길 수 없다: {target}"
                )
            pts = self._review_pts(source_id, seconds, "경계 초")
            return raw_service.review_boundary_edit(head, target, neighbor, pts)

        return self._apply_review_edit(root, build, "경계 조정")

    def review_split_view(self, signature: str | None, segment_id: str, seconds_text: str) -> str:
        """조각 나누기 — 자를 자리(초)만 정한다(구간을 고르지 않는다)."""
        root = self._require_current_selection(signature)
        target = self._require_segment_id(segment_id)
        try:
            seconds = ui_facade.parse_seconds(seconds_text, what="나눌 초")
        except ValueError as error:
            raise gr.Error(str(error)) from error

        def build(head: str) -> Any:
            source_id, _start, _end = self._review_span(target)
            pts = self._review_pts(source_id, seconds, "나눌 자리")
            return raw_service.review_split_edit(head, target, pts)

        return self._apply_review_edit(root, build, "나누기")

    def review_merge_view(self, signature: str | None, left_id: str, right_id: str) -> str:
        """이웃 합치기 — 맞닿은 같은 원본 조각만(백엔드가 검증)."""
        root = self._require_current_selection(signature)
        left = self._require_segment_id(left_id)
        right = self._require_segment_id(right_id)
        return self._apply_review_edit(
            root,
            lambda head: raw_service.review_merge_edit(head, left, right),
            "이웃 합치기",
        )

    def review_reextract_view(self, signature: str | None) -> str:
        """다시 뽑을 조각 — 경계 편집(계획과 달라진 core)과 낡은 매체를 나열한다.

        경계 판정은 **기본 계획의 core 범위**와 지금 리비전을 직접 비교한다 —
        재시작 뒤에도 같은 판정이고, 메타데이터 편집(메모·선택·권고)은 늘 0개다.
        매체 판정은 백엔드 `stale_media`(게시 artifact의 core·해시 대조) 그대로다.
        """
        root = self._require_current_selection(signature)
        facade = self._review_facade(root)
        if self._review_signature != self._signature_for(root):
            raise gr.Error("검토 기록이 다른 작업공간의 것이다 — 새로고침 뒤 다시 시도하세요.")
        if not self._review_revision_id or not self._review_base_plan_id:
            raise gr.Error("검토 리비전이 없다 — 먼저 **검토 시작**을 누르세요.")
        try:
            loaded = facade.load_head(self._review_base_plan_id)
            plan = self.raw.segment_plan_revision(root, self._review_base_plan_id)
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            raise gr.Error(str(error)) from error
        revision = loaded.revision
        if revision.revision_id != self._review_revision_id:
            raise gr.Error("검토 헤드가 화면이 본 뒤 바뀌었다 — **검토 새로고침** 뒤 다시 보세요.")
        plan_ranges = {
            (entry.asset_id, entry.core_start_pts, entry.core_end_pts) for entry in plan.entries
        }
        boundary_changed = sorted(
            segment.segment_id
            for segment in revision.segments
            if (segment.source_id, segment.start_pts, segment.end_pts) not in plan_ranges
        )
        package_dir = self._latest_package_dir(root)
        media_stale = (
            facade.stale_media(package_dir, self._review_base_plan_id)
            if package_dir is not None
            else ()
        )
        lines = [
            "**다시 뽑을 조각** — 낡은 core는 새 추출로만 갱신된다(재추출 없이 확정되지 않는다).",
            "",
            f"- 계획과 달라진 경계를 가진 조각: {len(boundary_changed)}개"
            + (
                f" — {', '.join(f'`{item}`' for item in boundary_changed[:8])}"
                if boundary_changed
                else ""
            ),
            f"- 게시 매체가 없거나 core·해시가 어긋난 것: {len(media_stale)}개"
            + (
                f" — {', '.join(f'`{item.segment_id}`({item.reason})' for item in media_stale[:4])}"
                if media_stale
                else ""
            ),
            "",
            "메타데이터 편집(메모·선택·권고)은 재추출 대상을 만들지 않는다 — 0개가 정상이다.",
            "**검토 계획 반영**으로 새 계획 리비전을 만들고 추출하면 낡은 core가 갱신된다.",
        ]
        return self._review_panel_markdown(root, "\n".join(lines))

    def _latest_package_dir(self, root: Path) -> str | None:
        """마지막 package 이름 — 검토·확정 화면이 기본으로 쓰는 자리."""
        summary = self.raw.package_overview(root)
        if summary is None:
            return None
        return summary.package.package_dir

    def review_plan_view(self, signature: str | None) -> str:
        """검토 계획 반영 — 편집된 덮개로 새 A3 계획을 만들고 검토를 그 위로 옮긴다."""
        root = self._require_current_selection(signature)
        facade = self._review_facade(root)
        if not self._review_revision_id or not self._review_base_plan_id:
            raise gr.Error("검토 리비전이 없다 — 먼저 **검토 시작**을 누르세요.")
        try:
            snapshot, mapping = facade.plan_from_review(self._review_base_plan_id)
            adopted = facade.adopt_plan_review(snapshot, mapping)
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            raise gr.Error(str(error)) from error
        # 새 기본 계획의 검토 상태(시계·범위·리비전)를 화면이 곧바로 쓰게 갱신한다.
        try:
            self._review_head_and_rows(root)
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            raise gr.Error(str(error)) from error
        _ = adopted
        extra = (
            f"검토 편집을 새 계획 리비전 `{mapping.new_plan_revision_id}`로 옮겼다"
            f" (매핑 쌍 {len(mapping.pairs)}개) — 아래 **조각 추출 시작**으로 뽑는다"
            "(새 리비전은 같은 형상이라도 새 package에 뽑히고 옛 package는 불변이다)."
        )
        return self._segment_state_markdown(root) + "\n\n" + extra

    def review_vtt_toggle_view(
        self, signature: str | None, clip_file_id: str | None, want_vtt: bool
    ) -> str:
        """클립 재생의 자막(VTT) 토글 — 등록된 검토 자막 ID만 붙인다(호출 0회)."""
        root = self._require_current_selection(signature)
        if not clip_file_id:
            return self._player_html(None)
        track_path = None
        caption = None
        if want_vtt:
            segment_id = self._segment_id_for_clip(root, clip_file_id)
            if segment_id is None:
                raise gr.Error("이 클립의 검토 자막을 못 찾았다 — 검토 리비전을 먼저 만드세요.")
            package_dir = self._latest_package_dir(root)
            if package_dir is None:
                raise gr.Error("게시된 package가 없다 — 먼저 조각 추출을 끝내세요.")
            try:
                registration = self.raw.register_review_vtt(root, segment_id, package_dir)
            except (
                raw_service.RawWorkspaceError,
                raw_service.ReviewError,
                raw_service.WorkspaceModuleUnavailableError,
            ) as error:
                raise gr.Error(str(error)) from error
            if registration is None:
                raise gr.Error("검토 자막이 비었다 — 측정된 발화가 없다(빈 성공이 아니다).")
            track_path = f"/media/{registration.registration.file_id}"
            prov = registration.provenance
            caption = (
                f"출처 — 원본 `{prov.source_original_relative_path}`"
                f"(sha {prov.source_sha256[:12]}…) · 추출 `{prov.extracted_preview_relative_path}`"
                f"(sha {prov.extracted_sha256[:12]}…, 잰 길이 {prov.measured_media_duration}초)"
            )
        return ui_facade.media_player_html(
            f"/media/{clip_file_id}", track_path=track_path, caption=caption
        )

    def _segment_id_for_clip(self, root: Path, clip_file_id: str) -> str | None:
        """재생 중인 클립의 조각 ID — 등록 미디어 → 조각 표에서 짝짓는다."""
        try:
            resolved = self.raw.resolve_media(root, clip_file_id)
        except raw_service.RawWorkspaceError:
            return None
        package_dir = self._latest_package_dir(root)
        if package_dir is None:
            return None
        for clip in self.raw.list_package_clips(root, package_dir):
            if clip.file_id == clip_file_id or resolved.path.name == Path(clip.relative_path).name:
                return clip.segment_id
        return None

    # ── 화면 3 — 확정 내보내기·Finder·캐시 정리(W3 백엔드) ────────

    def review_export_view(
        self, signature: str | None, selected_text: str, confirm: bool
    ) -> tuple[str, str]:
        """확정 내보내기 작업 제출 — **제출 때의 검토 리비전·선택**에 묶인다.

        본문은 제출 순간 화면이 본 리비전과 선택을 폐쇄로 갖고, 실행 때 머리가
        그대로인지 다시 검증한다 — 그 사이 다른 편집이 머리를 옮겼으면 실패로
        끝나고, 나중의 머리를 몰래 내보내지 않는다. 복사 자체는 W3
        `export_confirmed`(실제 바이트·no-replace)가 하고, 뒤이어 `verify_export`로
        게시 검증까지 본문 안에서 마친다.
        """
        root = self._require_current_selection(signature)
        if not confirm:
            raise gr.Error(
                "내보내기 확인이 필요하다 — 대상과 선택을 다시 보고 확인란을 체크하세요."
            )
        facade = self._review_facade(root)  # 백엔드 없는 빌드 — 정직한 오류가 먼저다
        if self._review_signature != self._signature_for(root):
            raise gr.Error("검토 기록이 다른 작업공간의 것이다 — 새로고침 뒤 다시 시도하세요.")
        if not self._review_revision_id or not self._review_base_plan_id:
            raise gr.Error("검토 리비전이 없다 — 먼저 **검토 시작**을 누르세요.")
        package_dir = self._latest_package_dir(root)
        if package_dir is None:
            raise gr.Error("게시된 package가 없다 — 먼저 조각 추출을 끝내세요.")
        try:
            current = facade.load_head(self._review_base_plan_id).revision
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            raise gr.Error(str(error)) from error
        if current.revision_id != self._review_revision_id:
            raise gr.Error(
                f"검토 헤드가 화면이 본 뒤 바뀌었다(`{self._review_revision_id}` →"
                f" `{current.revision_id}`) — **검토 새로고침** 뒤 다시 내보내세요."
            )
        known = {segment.segment_id for segment in current.segments}
        selected_names = [
            item.strip().strip("`") for item in (selected_text or "").split(",") if item.strip()
        ]
        unknown = [item for item in selected_names if item not in known]
        if unknown:
            raise gr.Error(
                f"검토 표에 없는 조각이 있다: {', '.join(unknown[:3])} — ID를 다시 확인하세요."
            )
        selected = frozenset(selected_names) or None
        base_plan_id = self._review_base_plan_id
        displayed_revision = self._review_revision_id
        signature_now = self._signature_for(root)

        def body(context: Any) -> None:
            head_now = facade.load_head(base_plan_id).revision
            if head_now.revision_id != displayed_revision:
                raise raw_service.RawWorkspaceError(
                    f"내보내기가 제출 때의 리비전 `{displayed_revision}`에 묶여 있는데"
                    f" 지금 머리는 `{head_now.revision_id}`다 — 새로 고침 뒤 다시 내보내세요."
                )
            outcome = facade.export_confirmed(
                package_dir,
                base_plan_id=base_plan_id,
                selected_segment_ids=selected,
                context=context,
            )
            facade.verify_export(outcome.destination_dir)
            with self._selection_lock:
                if self._root == root and self._signature_for(root) == signature_now:
                    self._last_export_path = str(root / outcome.destination_dir)
                    self._last_export_dir = outcome.destination_dir

        fingerprint = hashlib.sha256(
            "|".join(
                (
                    base_plan_id,
                    displayed_revision,
                    package_dir,
                    ",".join(sorted(selected)) if selected else "*",
                )
            ).encode("utf-8")
        ).hexdigest()
        total = len(selected) if selected is not None else len(current.segments)
        handle = self.raw.submit_job(root, REVIEW_EXPORT_JOB_KIND, fingerprint, body, total=total)
        lines = [
            f"**확정 내보내기 작업 `{handle.job_id}` 제출** — 리비전 `{displayed_revision}`에"
            " 묶었다" + (f" · 선택 {len(selected)}개" if selected is not None else " · 전체"),
            "",
            "- 진행은 작업 목록 새로고침으로 본다. 완료되면 **Finder에서 열기**·"
            "**내보내기 경로 복사**가 그 자리를 가리킨다.",
            "- 제출 뒤 검토 머리가 움직이면 이 내보내기는 실패로 끝난다 — 나중의"
            " 머리를 몰래 내보내지 않는다.",
        ]
        return "\n".join(lines), self._package_state_markdown(root)

    def review_export_open_view(self, signature: str | None) -> str:
        """Finder에서 열기 — 인자를 검증한 뒤에만 OS 경계를 부른다(시험은 가짜)."""
        root = self._require_current_selection(signature)
        target_text = self._last_export_path
        if not target_text:
            raise gr.Error("마지막 내보내기가 없다 — 내보내기 작업이 끝난 뒤 누르세요.")
        target = Path(target_text)
        root_resolved = root.expanduser().resolve()
        if not target.is_absolute():
            raise gr.Error("내보내기 경로가 절대 경로가 아니다 — 다시 내보내세요.")
        resolved = target.resolve()
        if root_resolved not in resolved.parents:
            raise gr.Error("내보내기 경로가 작업공간 안이 아니다 — 다시 내보내세요.")
        if not resolved.is_dir():
            raise gr.Error(f"내보내기 폴더가 없다: {target_text}")
        opener = self._finder_opener or self._system_finder_open
        opener(resolved)
        return f"Finder에서 열었다: `{resolved}`"

    @staticmethod
    def _system_finder_open(target: Path) -> None:
        """실제 Finder 열기 — macOS `open -R`, 인자는 리스트로 주입 없이 전달."""
        import subprocess

        completed = subprocess.run(
            ["/usr/bin/open", "-R", str(target)], capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            raise gr.Error(f"Finder를 열지 못했다: {(completed.stderr or '').strip()[:200]}")

    def review_export_copy_view(self, signature: str | None) -> str:
        """경로 복사용 텍스트 — 마지막 내보내기 자리를 그대로 보여준다."""
        self._require_current_selection(signature)
        if not self._last_export_path:
            raise gr.Error("마지막 내보내기가 없다 — 내보내기 작업이 끝난 뒤 누르세요.")
        return self._last_export_path

    def review_cleanup_view(self, signature: str | None, confirm: bool) -> str:
        """캐시 정리 — 계획을 보여주고 실행한다(확정본은 물리적으로 보호된다)."""
        root = self._require_current_selection(signature)
        if not confirm:
            raise gr.Error("정리 확인이 필요하다 — cache/·previews/만 지우고 확정본은 남는다.")
        facade = self._review_facade(root)
        try:
            result = facade.managed_cleanup()
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        lines = [
            "**캐시 정리 완료** — `cache/`·`previews/`만 대상이다.",
            "",
            f"- 지운 파일 {result.removed_files}개 · 돌려받은 용량"
            f" {ui_facade.format_bytes(result.removed_bytes)}",
            f"- 보호 거부(원본·확정·분석·자막 등): {len(result.refused)}개"
            + (
                f" — 예 `{result.refused[0].relative_path}`({result.refused[0].reason})"
                if result.refused
                else ""
            ),
        ]
        if self._last_export_dir:
            try:
                facade.verify_export(self._last_export_dir)
                lines.append(
                    f"- 확정 내보내기 `{self._last_export_dir}` 정리 뒤 재검증 **통과**"
                    " — 바이트가 그대로다(불변)."
                )
            except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
                lines.append(f"- 확정 내보내기 재검증 실패: {error}")
        lines.append("- 확정 내보내기(`clips/confirmed_*`)는 정리 대상이 아니다 — 불변이다.")
        return "\n".join(lines)

    # ── 화면 4 — 최종 영상 등록·자막 실행·검토·확정(W2 백엔드) ──

    def _caption_ws(self, root: Path) -> Any:
        """자막 작업공간 — 백엔드가 없는 빌드에서는 정직한 오류로 닫는다."""
        try:
            return self.raw.caption_workspace(root)
        except raw_service.WorkspaceModuleUnavailableError as error:
            raise gr.Error(str(error)) from error

    def _require_caption_identity(self, root: Path) -> dict[str, str | int | None]:
        """마지막으로 그린 실행 신원 — 등록·승인·편집·확정이 전부 이 신원에 묶인다."""
        if self._caption_identity is None or self._caption_signature != self._signature_for(root):
            raise gr.Error("실행 신원이 없다 — 먼저 최종 영상을 등록하고 추정을 보여주세요.")
        return self._caption_identity

    def _caption_run_identity(self, identity: dict[str, str | int | None]) -> Any:
        """신원 사전 → W2 `RunIdentity` — 공개 서비스 경로로만 만든다."""
        return raw_service.caption_run_identity(
            str(identity["revision"]), str(identity["run_id"]), str(identity["fingerprint"])
        )

    def _caption_registration(self, ws: Any, root: Path) -> Any:
        """신원의 등록 행 — (렌더 해시, 오디오 서수)로 저장소에서 다시 찾는다.

        재시작 뒤에도 같은 행이 돌아온다(등록표가 정본). 못 찾으면 조용히 새로
        만들지 않고 정직하게 닫는다.
        """
        identity = self._require_caption_identity(root)
        registration = ws.registration(str(identity["revision"]), identity["audio_ordinal"])
        if registration is None:
            raise gr.Error(
                f"등록 행을 찾지 못했다 — 렌더 {str(identity['revision'])[:12]}… · 오디오"
                f" 서수 {identity['audio_ordinal']}. 다시 등록·추정하세요."
            )
        return registration

    def caption_register_view(
        self, signature: str | None, path_text: str, source_id: str, ordinal_text: str
    ) -> str:
        """최종 영상 등록 — 경로에서 검증 사본을 만들고 오디오 트랙을 고른다."""
        root = self._require_current_selection(signature)
        cleaned = (path_text or "").strip().strip("\"'")
        if not cleaned:
            raise gr.Error("최종 영상 경로를 적으세요 — 작업공간 밖 파일도 검증 사본으로 들어온다.")
        path = Path(cleaned).expanduser()
        if not path.is_absolute():
            raise gr.Error("절대 경로로 적으세요.")
        sid = (source_id or "").strip() or "final_1"
        ordinal: int | None = None
        if ordinal_text and ordinal_text != "자동(첫 트랙)":
            try:
                ordinal = int(ordinal_text)
            except ValueError as error:
                raise gr.Error(f"오디오 서수가 숫자가 아니다: {ordinal_text!r}") from error
        ws = self._caption_ws(root)
        try:
            registration = ws.register_final_video(path, source_id=sid, audio_ordinal=ordinal)
        except (
            raw_service.RawWorkspaceError,
            raw_service.CaptionWorkspaceError,
            raw_service.FinalVideoError,
        ) as error:
            raise gr.Error(str(error)) from error
        note = {
            "ready": "등록 완료 — 아래 실행 ID로 추정·실행한다.",
            "no_audio": "**오디오가 없다** — 무발화가 아니라 별개 상태다. 실행·추정은 닫혀 있다.",
            "audio_selection_required": (
                f"**오디오 트랙이 {registration.audio_stream_count}개다** — 오디오 트랙"
                " 칸에 번호(0, 1, …)를 골라 다시 등록하세요."
            ),
        }.get(registration.status, f"상태 `{registration.status}`")
        return (
            f"**최종 영상 등록** — 리비전 `{registration.final_video_revision}` · 상태"
            f" `{registration.status}`\n\n{note}"
        )

    def caption_estimate_view(self, signature: str | None, run_id: str) -> str:
        """실행 추정 — 모델·수명주기·실측 청크·경계·비용을 백엔드가 잰 값 그대로."""
        root = self._require_current_selection(signature)
        run = (run_id or "").strip() or "run-1"
        ws = self._caption_ws(root)
        ready = [item for item in ws.registrations() if item.ready]
        if not ready:
            raise gr.Error("준비된 등록이 없다 — 먼저 최종 영상 등록(오디오 선택 확정)을 끝내세요.")
        registration = ready[-1]
        try:
            estimate = ws.estimate(registration, run_id=run)
        except (raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError) as error:
            raise gr.Error(str(error)) from error
        self._caption_identity = {
            "revision": registration.final_video_revision,
            "run_id": run,
            "fingerprint": estimate.identity.fingerprint,
            "audio_ordinal": registration.audio_ordinal,
        }
        self._caption_signature = self._signature_for(root)
        self._caption_chunk_count = estimate.chunk_count
        # 새 실행 신원 — 검토 머리·큐 목록은 이 실행의 것으로 다시 만든다.
        self._caption_head_sequence = None
        self._caption_cue_ids = ()
        self._caption_unplaced = {}
        chunk_preview = ", ".join(
            f"[{ui_facade.format_seconds(start)}~{ui_facade.format_seconds(end)}]"
            for start, end in estimate.chunk_seconds[:4]
        )
        lifecycle_lines = [
            f"  - `{item.model_id}`({item.role}) — 종료 예정 {item.shutdown_date or '없음'}"
            for item in estimate.lifecycle
        ]
        lines = [
            f"**자막 실행 추정** — 실행 `{run}` · 실행 지문"
            f" `{estimate.identity.fingerprint[:12]}…`",
            "",
            f"- 문구 모델 `{estimate.text_model}` · 시간 모델 `{estimate.timing_model}`"
            " — 힌트·keywords는 문구 모델에만 간다(규칙 6)",
            f"- 실측 청크 {estimate.chunk_count}개 · 샘플 {estimate.sample_count:,}"
            f" ({estimate.sample_rate:,}Hz {estimate.channels}채널 · 원점"
            f" {ui_facade.format_seconds(estimate.origin)}초)",
            f"- 청크 경계(정규화 축): {chunk_preview or '(없음)'}"
            + (" …" if len(estimate.chunk_seconds) > 4 else ""),
            f"- 예상 비용 **${estimate.estimated_cost_usd}** · 상한 예약"
            f" ${estimate.reserved_cost_usd} — 백엔드가 잰 값 그대로(지어내지 않는다).",
            "- 수명주기(등록표 확인 값):",
            *lifecycle_lines,
            "",
            "아래 확인란을 체크하고 **자막 생성 실행**을 누르면 이 지문에 묶인 승인이"
            " 서버에 남는다.",
        ]
        return "\n".join(lines)

    def caption_run_view(self, signature: str | None, run_id: str, confirm: bool) -> str:
        """자막 생성 실행 — 화면이 본 지문에 묶인 승인 뒤에만 제공자를 만든다."""
        root = self._require_current_selection(signature)
        run = (run_id or "").strip() or "run-1"
        if not confirm:
            raise gr.Error("실행 승인 확인이 필요하다 — 위 추정의 지문을 보고 확인란을 체크하세요.")
        ws = self._caption_ws(root)  # 백엔드 없는 빌드 — 정직한 오류가 먼저다
        identity = self._require_caption_identity(root)
        if identity["run_id"] != run:
            raise gr.Error(
                f"승인된 실행(`{identity['run_id']}`)과 요청 실행(`{run}`)이 다르다 —"
                " 추정을 다시 보고 승인하세요."
            )
        registration = self._caption_registration(ws, root)
        if not registration.ready:
            raise gr.Error("등록이 준비되지 않았다 — 오디오 선택을 확정하고 다시 추정하세요.")
        try:
            plan = ws.plan(registration, run_id=run)
            ws.approve(plan, note="브라우저 화면 확인란 승인")
            timing, text = self._caption_providers_for_action()
            body = ws.run_body(registration, run, timing_provider=timing, text_provider=text)
        except (raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError) as error:
            raise gr.Error(str(error)) from error
        total = self._caption_chunk_count or 1
        handle = self.raw.submit_job(
            root,
            "caption_run",
            str(identity["fingerprint"]),
            body,
            total=max(1, total),
        )
        return (
            f"자막 실행 작업 `{handle.job_id}` 제출"
            + (" (기존 활성 작업에 다시 붙음)" if handle.reattached else "")
            + " — 진행은 아래 새로고침으로 확인한다. 재개는 같은 실행·같은 지문에서만"
            " 이어 붙는다(중복 호출 0)."
        )

    def _caption_providers_for_action(self) -> tuple[object, object]:
        """이 실행이 쓸 제공자 — 주입 경계가 우선, 없으면 생산 transport(키는 시작 환경)."""
        if self._caption_providers is not None:
            return self._caption_providers()
        if self._startup_credentials is None:
            raise gr.Error(
                "실제 호출에는 서버를 띄운 환경의 OPENAI_API_KEY가 필요하다 — 키를"
                " 내보내고 브라우저 서버를 다시 띄우세요(이 화면은 .env 파일을 읽지"
                " 않는다)."
            )
        return raw_service.caption_transport_providers(self._startup_credentials)

    def caption_review_view(self, signature: str | None, run_id: str) -> str:
        """검토 묶음 — 큐 행·경고·미대응·파형(로컬만, 원응답은 나가지 않는다)."""
        root = self._require_current_selection(signature)
        run = (run_id or "").strip() or "run-1"
        ws = self._caption_ws(root)  # 백엔드 없는 빌드 — 정직한 오류가 먼저다
        identity = self._require_caption_identity(root)
        if identity["run_id"] != run:
            raise gr.Error(
                f"화면이 본 실행은 `{identity['run_id']}`다 — 그 실행의 검토부터 여세요."
            )
        run_identity = self._caption_run_identity(identity)
        registration = self._caption_registration(ws, root)
        try:
            bundle = ws.review_bundle(run_identity)
            wave = ws.waveform(registration, bin_seconds=0.05)
        except (raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError) as error:
            raise gr.Error(str(error)) from error
        state = bundle.state
        # 화면이 본 머리 번호·큐 목록 — 편집·확정은 이 값에 묶인다(W2 머리 충돌 게이트).
        self._caption_head_sequence = state.head_revision if state.head_revision is not None else 0
        rows = [
            ui_facade.CueRowView(
                cue_id=row.cue.cue_id,
                start_seconds=ui_facade.format_seconds(row.cue.start),
                end_seconds=ui_facade.format_seconds(row.cue.end),
                whisper_text=row.cue.whisper_text,
                auto_text=row.cue.auto_text,
                basis=row.cue.text_basis,
                chars_per_second=row.chars_per_second,
            )
            for row in state.rows
        ]
        self._caption_cue_ids = tuple(row.cue_id for row in rows)
        self._caption_unplaced = {view.item.index: view.item.text for view in state.unplaced}
        lines = [
            f"**자막 검토** — 실행 `{run}` · 큐 {len(rows)}개 · 검토 머리"
            f" {self._caption_head_sequence}",
            "",
            ui_facade.cue_table_markdown(rows),
            "",
        ]
        if not state.text_model_complete or not state.timing_model_complete:
            lines.append(
                "- **실패한 청크가 있는 초안이다** — 문구/시간 일부 청크가 비었다"
                "(증거·초안은 보존되었다). 편집으로 메우거나 다시 실행하세요."
            )
        if state.warnings:
            shown = ", ".join(f"`{item.key}`" for item in state.warnings[:5])
            lines.append(
                f"열린 경고 {len(state.warnings)}개 — 편집 칸 `경고 허용`로 남긴다: {shown}"
            )
        else:
            lines.append("열린 경고가 없다.")
        unresolved = [view for view in state.unplaced if view.resolution is None]
        if unresolved:
            first = unresolved[0]
            lines.append(
                f"미대응 말 {len(unresolved)}개(전체 {len(state.unplaced)}개 중) — 편집 칸"
                f" `미대응 해결`로 남긴다: {first.item.index}번 `{first.item.text[:24]}…`"
            )
        elif state.unplaced:
            lines.append(
                f"미대응 {len(state.unplaced)}개 전부 처리되었다 — 확정 게이트가 열려 있다."
            )
        lines += [
            "",
            ui_facade.waveform_svg([peak / wave.peak_scale for peak in wave.peaks]),
        ]
        return "\n".join(lines)

    def _require_caption_head(self) -> int:
        """화면이 본 검토 머리 번호 — 편집·확정의 기대 머리(W2 갱신 손실 방지)."""
        if self._caption_head_sequence is None:
            raise gr.Error(
                "검토 머리를 모른다 — 먼저 **검토 새로고침**으로 화면이 본 머리를 만드세요."
            )
        return self._caption_head_sequence

    def caption_edit_view(
        self,
        signature: str | None,
        operation: str,
        cue_id: str,
        text: str,
        start_ms: str,
        end_ms: str,
        second_id: str,
        index_text: str,
        warning_key: str,
    ) -> str:
        """수동 편집 — 시각은 사람이 준 값만(추측 배분 없음), 저장은 백엔드가 검증한다."""
        root = self._require_current_selection(signature)
        identity = self._require_caption_identity(root)
        ws = self._caption_ws(root)
        run_identity = self._caption_run_identity(identity)
        expected = self._require_caption_head()

        def _int_field(value: str, what: str) -> int:
            cleaned = (value or "").strip()
            if not cleaned:
                raise gr.Error(f"{what}이 비었다 — ms 정수로 적으세요.")
            try:
                return int(cleaned)
            except ValueError as error:
                raise gr.Error(f"{what}이 정수가 아니다: {cleaned!r}") from error

        target = (cue_id or "").strip().strip("`")
        try:
            if operation == "글자 교정":
                if not target or not text.strip():
                    raise gr.Error("큐 ID와 새 글자를 모두 적으세요.")
                edited = ws.edit_cue_text(
                    run_identity, target, text.strip(), expected_head=expected
                )
            elif operation == "시각 조정":
                if not target:
                    raise gr.Error("큐 ID를 적으세요.")
                edited = ws.edit_cue_time(
                    run_identity,
                    target,
                    start=raw_service.caption_ms(_int_field(start_ms, "시작 ms")),
                    end=raw_service.caption_ms(_int_field(end_ms, "끝 ms")),
                    expected_head=expected,
                )
            elif operation == "큐 나누기":
                if not target or "|" not in (text or ""):
                    raise gr.Error("큐 ID와 나눌 글자(`앞|뒤`)를 적으세요.")
                at = raw_service.caption_ms(_int_field(start_ms, "나눌 시각 ms"))
                left, right = (text or "").split("|", 1)
                edited = ws.split_cue(
                    run_identity, target, at, left.strip(), right.strip(), expected_head=expected
                )
            elif operation == "큐 합치기":
                other = (second_id or "").strip().strip("`")
                if not target or not other:
                    raise gr.Error("맞닿은 두 큐 ID를 적으세요.")
                edited = ws.merge_cues(run_identity, target, other, expected_head=expected)
            elif operation == "새 큐 추가":
                if not text.strip():
                    raise gr.Error("새 큐 글자를 적으세요.")
                edited = ws.add_manual_cue(
                    run_identity,
                    raw_service.caption_ms(_int_field(start_ms, "시작 ms")),
                    raw_service.caption_ms(_int_field(end_ms, "끝 ms")),
                    text.strip(),
                    note="미대응 말을 수동 시각으로 살렸다",
                    expected_head=expected,
                )
            elif operation == "경고 허용":
                key = (warning_key or "").strip()
                if not key:
                    raise gr.Error("허용할 경고 key를 적으세요.")
                edited = ws.allow_warning(
                    run_identity, key, "브라우저에서 봤다", expected_head=expected
                )
            elif operation == "미대응 해결":
                index = _int_field(index_text, "미대응 번호")
                item_text = text.strip() or self._caption_unplaced.get(index)
                if item_text is None:
                    raise gr.Error(
                        f"미대응 {index}번의 글자를 모른다 — 검토 새로고침 뒤 글자칸에"
                        " 그대로 적어주세요."
                    )
                edited = ws.resolve_unplaced_item(
                    run_identity,
                    index,
                    item_text,
                    placed_cue_id=target or None,
                    expected_head=expected,
                )
            elif operation == "무발화 기록":
                edited = ws.record_no_speech(
                    run_identity, note=text.strip(), expected_head=expected
                )
            else:
                raise gr.Error(f"모르는 편집 종류다: {operation!r}")
        except raw_service.ManualHeadConflict as error:
            raise gr.Error(
                f"편집이 늦었다 — 앞선 편집 위에 덮지 못한다({error}). **검토 새로고침**"
                " 뒤 다시 편집하세요."
            ) from error
        except (raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError) as error:
            raise gr.Error(str(error)) from error
        self._caption_head_sequence = edited.sequence
        return self.caption_review_view(signature, str(identity["run_id"]))

    def caption_confirm_view(self, signature: str | None, run_id: str, confirm: bool) -> str:
        """확정 — 화면이 짚은 큐 전부·기대 머리와 함께 final 게시를 연다(게이트는 백엔드)."""
        root = self._require_current_selection(signature)
        run = (run_id or "").strip() or "run-1"
        if not confirm:
            raise gr.Error("확정 확인이 필요하다 — 전 큐를 검토했는지 보고 확인란을 체크하세요.")
        ws = self._caption_ws(root)  # 백엔드 없는 빌드 — 정직한 오류가 먼저다
        identity = self._require_caption_identity(root)
        if identity["run_id"] != run:
            raise gr.Error(
                f"승인된 실행(`{identity['run_id']}`)과 요청 실행(`{run}`)이 다르다 —"
                " 검토를 다시 여세요."
            )
        expected = self._require_caption_head()
        cue_ids = self._caption_cue_ids
        if not cue_ids:
            raise gr.Error("확인할 큐 목록이 없다 — 먼저 **검토 새로고침**으로 큐를 보세요.")
        try:
            exported = ws.confirm(
                self._caption_run_identity(identity),
                expected_head=expected,
                cue_ids=cue_ids,
                note="브라우저 화면 전 큐 검토 확인",
            )
        except raw_service.ManualHeadConflict as error:
            raise gr.Error(
                f"확정이 늦었다 — 검토 머리가 바뀌었다({error}). **검토 새로고침** 뒤"
                " 다시 확인하세요."
            ) from error
        except (raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError) as error:
            raise gr.Error(str(error)) from error
        links = self._register_final_subtitles(root, exported)
        lines = [
            f"**자막 확정 완료** — 실행 `{run}` · 검토 머리 {expected} · 큐"
            f" {len(cue_ids)}개 명시 확인",
            "",
            "- 확정 게이트(미해결 미대응·미허용 경고·시각 무효)를 전부 지났다.",
            f"- 내보내기 지문 `{exported.export.export_sha256[:12]}…` · SRT sha"
            f" `{exported.export.srt_sha256[:12]}…` · VTT sha"
            f" `{exported.export.vtt_sha256[:12]}…`",
        ]
        if links:
            lines += [
                "- 내려받기(등록 미디어 ID로만 제공된다):",
                *(f"  - [{label}]({path}) — `{path}`" for label, path in links),
            ]
        lines.append("- 낡은 확정 바이트는 다시 쓰지 않는다 — 다시 내려면 재구문 분석부터.")
        return "\n".join(lines)

    def _register_final_subtitles(self, root: Path, exported: Any) -> list[tuple[str, str]]:
        """확정 SRT·VTT를 내용 해시 봉인 이름으로 등록해 내려받기 링크를 만든다.

        검토 저장소의 final 자리(`review/captions/final/…`)는 브라우저 제공 대상이
        아니다(§4.4). 여기는 그 바이트를 `final/captions/published/<내용sha24>.srt|.vtt`
        검증 사본으로 옮겨 등록한다 — 같은 내용은 같은 자리(멱등), 제공은 등록
        ID로만 한다.
        """
        final = exported.export
        links: list[tuple[str, str]] = []
        for label, source_path in (("SRT", final.srt), ("VTT", final.vtt)):
            if not source_path or not Path(source_path).is_file():
                continue
            source = Path(source_path)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = root / "final" / "captions" / "published" / f"{digest[:24]}{source.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                shutil.copyfile(source, target)
            registered = None
            relative = target.relative_to(root).as_posix()
            for item in self.raw.list_media(root, kind="artifact"):
                if item.relative_path == relative:
                    registered = item
                    break
            if registered is None:
                registered = self.raw.register_media(root, target, "artifact")
            links.append((label, f"/media/{registered.file_id}"))
        return links

    def caption_trace_view(self, signature: str | None, run_id: str) -> str:
        """실행 개요 — 조각·판정 요약만(원응답은 검토 저장소 자리에 있고 나가지 않는다)."""
        root = self._require_current_selection(signature)
        run = (run_id or "").strip() or "run-1"
        ws = self._caption_ws(root)  # 백엔드 없는 빌드 — 정직한 오류가 먼저다
        identity = self._require_caption_identity(root)
        try:
            bundle = ws.review_bundle(self._caption_run_identity(identity))
        except (raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError) as error:
            raise gr.Error(str(error)) from error
        state = bundle.state
        resolved = sum(1 for row in state.rows if row.cue.text_basis == "manual")
        lines = [
            f"**실행 개요** — 실행 `{run}` · 큐 {len(state.rows)}개(수동 {resolved}개 포함)",
            "",
            f"- 열린 경고 {len(state.warnings)}개 · 미대응 {len(state.unplaced)}개",
            f"- 청크 게이트 — 시간 완료 {state.timing_model_complete} · 문구 완료"
            f" {state.text_model_complete} (실패 상태를 숨기지 않는다)",
            "- 원시 모델 응답은 브라우저에 제공되지 않는다 — 검토 저장소의 비제공 자리에"
            " 불변으로 남을 뿐이다.",
            "- 재구문 분석(재파싱)은 새 리비전에서만 — 낡은 확정 바이트는 다시 쓰지 않는다.",
        ]
        return "\n".join(lines)

    def _caption_tab(self) -> None:
        with gr.Tab("4 · 최종 영상 · 자막"):
            self._settings_md = gr.Markdown(self.settings_markdown())
            gr.Markdown(
                "> **최종 영상 자막** — 등록(검증 사본·오디오 트랙 선택) → 추정"
                "(모델·수명주기·실측 청크) → 확인란 승인 → 실행 작업 → 검토(큐 행·"
                "파형·미대응) → 수동 편집 → 확정 게시. 실제 호출은 승인된 실행에서만"
                " 열리고, 키는 서버를 띄운 환경에서만 온다(§14.3)."
            )
            with gr.Row():
                caption_path_box = gr.Textbox(
                    label="최종 영상 절대 경로",
                    placeholder="/Users/…/final.mp4",
                    scale=4,
                )
                caption_source_box = gr.Textbox(
                    label="출처 ID(선택)", placeholder="final_1", scale=2
                )
                caption_ordinal_dd = gr.Dropdown(
                    choices=["자동(첫 트랙)", "0", "1", "2"],
                    value="자동(첫 트랙)",
                    label="오디오 트랙(여럿일 때 번호로)",
                    interactive=True,
                    scale=2,
                )
                caption_register_button = gr.Button("최종 영상 등록", variant="primary", scale=2)
            with gr.Row():
                caption_run_box = gr.Textbox(
                    label="실행 ID", value="run-1", interactive=True, scale=2
                )
                caption_estimate_button = gr.Button("실행 추정(로컬)", scale=2)
                caption_confirm_box = gr.Checkbox(
                    label="위 추정 지문 그대로 자막 생성을 승인한다",
                    interactive=True,
                    scale=3,
                )
                caption_run_button = gr.Button("자막 생성 실행", variant="primary", scale=2)
            with gr.Row():
                caption_refresh_button = gr.Button("검토 새로고침", scale=2)
                caption_trace_button = gr.Button("실행 개요(원응답 없음)", scale=2)
                caption_final_confirm_box = gr.Checkbox(
                    label="전 큐를 검토했다 — 확정 게시를 승인한다",
                    interactive=True,
                    scale=3,
                )
                caption_confirm_button = gr.Button(
                    "확정 · SRT/VTT 게시", variant="primary", scale=2
                )
            gr.Markdown(
                "> 수동 편집 — 시각은 사람이 준 값만 들어간다(추측 배분 없음)."
                " 큐 행의 `a…`(자동)·`m…`(수동) ID를 그대로 쓴다."
            )
            with gr.Row():
                caption_op_dd = gr.Dropdown(
                    choices=[
                        "글자 교정",
                        "시각 조정",
                        "큐 나누기",
                        "큐 합치기",
                        "새 큐 추가",
                        "경고 허용",
                        "미대응 해결",
                        "무발화 기록",
                    ],
                    value="글자 교정",
                    label="편집 종류",
                    interactive=True,
                    scale=2,
                )
                caption_cue_box = gr.Textbox(label="큐 ID", scale=2)
                caption_text_box = gr.Textbox(
                    label="글자(나누기는 `앞|뒤`)",
                    scale=3,
                )
                caption_start_box = gr.Textbox(label="시작 ms", scale=1)
                caption_end_box = gr.Textbox(label="끝 ms", scale=1)
            with gr.Row():
                caption_second_box = gr.Textbox(label="짝 큐 ID(합치기)", scale=2)
                caption_index_box = gr.Textbox(label="미대응 번호", scale=2)
                caption_warning_box = gr.Textbox(label="경고 key", scale=2)
                caption_edit_button = gr.Button("편집 적용", scale=2)
            self._caption_md = gr.Markdown("_자막 상태가 여기 나타난다._", elem_id="caption-state")
            caption_register_button.click(
                self.caption_register_view,
                [self._signature_state, caption_path_box, caption_source_box, caption_ordinal_dd],
                self._caption_md,
            )
            caption_estimate_button.click(
                self.caption_estimate_view,
                [self._signature_state, caption_run_box],
                self._caption_md,
            )
            caption_run_button.click(
                self.caption_run_view,
                [self._signature_state, caption_run_box, caption_confirm_box],
                self._caption_md,
            )
            caption_refresh_button.click(
                self.caption_review_view,
                [self._signature_state, caption_run_box],
                self._caption_md,
            )
            caption_trace_button.click(
                self.caption_trace_view,
                [self._signature_state, caption_run_box],
                self._caption_md,
            )
            caption_edit_button.click(
                self.caption_edit_view,
                [
                    self._signature_state,
                    caption_op_dd,
                    caption_cue_box,
                    caption_text_box,
                    caption_start_box,
                    caption_end_box,
                    caption_second_box,
                    caption_index_box,
                    caption_warning_box,
                ],
                self._caption_md,
            )
            caption_confirm_button.click(
                self.caption_confirm_view,
                [self._signature_state, caption_run_box, caption_final_confirm_box],
                self._caption_md,
            )

    # ── 조립 ───────────────────────────────────────────────────

    def refresh_outputs(
        self,
    ) -> tuple[
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        str | None,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        str,
        gr.Dropdown,
        gr.CheckboxGroup,
    ]:
        """페이지 로드·새로고침 — 상태를 전부 서버에서 다시 읽는다.

        복구(recover)는 서버 시작 때 한 번뿐이다 — 새로고침이 작업 상태를 바꾸지
        않는다. 마지막 값은 이 화면이 그린 작업공간의 신원 토큰이다: 변경 콜백이
        이 토큰을 다시 실어 오면 서버가 그 사이 다른 프로젝트로 옮겼는지 밝힌다.
        """
        return (
            self.workspace_markdown(),
            gr.Dropdown(choices=self.device_choices()),
            self._listing_markdown(self._last_listing),
            gr.Dropdown(choices=self.media_choices()),
            self.jobs_markdown(),
            gr.Dropdown(choices=self.active_job_choices()),
            self.settings_markdown(),
            self.current_signature(),
            self._ingest_state_markdown(self._snapshot_root()),
            gr.Dropdown(choices=self.resumable_ingest_choices()),
            self._segment_state_markdown(self._snapshot_root()),
            gr.Dropdown(choices=self.resumable_extract_choices()),
            self._package_state_markdown(self._snapshot_root()),
            gr.Dropdown(choices=self.clip_choices()),
            self._analysis_state_markdown(self._snapshot_root()),
            gr.Dropdown(choices=self.resumable_analysis_choices()),
            gr.CheckboxGroup(choices=self.analysis_source_choices()),
        )

    def build_blocks(self) -> gr.Blocks:
        # theme·css는 gradio 6부터 launch/마운트 쪽 인자다(Blocks 생성자에 넣으면 경고).
        with gr.Blocks(title="vlogsrt 원본 브라우저") as app:
            gr.Markdown("### vlogsrt 원본 브라우저 · 원본 → 조각 → 편집 → 자막")
            # 화면이 마지막으로 본 작업공간 신원 — 선택의 최신 증명에만 쓴다.
            # 정본은 서버(DB)에 있고, 이 값은 새로고침마다 서버에서 다시 내려온다.
            self._signature_state = gr.State(None)
            open_button, create_button, ws_path_box, new_name_box = self._project_tab()
            extract_button, refresh_segment, profile_dd, analysis_plan = self._review_tab()
            self._package_tab()
            self._caption_tab()
            # 추출 시작·새로고침은 3번 탭(package 패널)까지 만들어진 뒤에 묶는다.
            extract_button.click(
                self.segment_extract_view,
                [self._signature_state, profile_dd],
                [self._segment_md, self._extract_jobs_dd, self._package_md],
            )
            refresh_segment.click(
                self.segment_refresh_view,
                self._signature_state,
                [self._segment_md, self._extract_jobs_dd, self._package_md, self._clip_dd],
            )
            # 분석 결과 → 분할 계획: 결정적 분할 패널이 뒤에 만들어지므로 여기 묶는다.
            analysis_plan.click(self.analysis_plan_view, self._signature_state, self._segment_md)
            # 만들기·열기는 **같은 화면**에 작업공간 전체 보기(서명·미디어·작업·설정)를
            # 바로 내린다 — 새로고침을 강제하지 않는다(루트 재현). 전환 이벤트의 출력
            # 목록은 모든 탭의 구성요소가 만들어진 뒤에야 완성되므로 여기서 묶는다.
            open_button.click(self.open_workspace_view, ws_path_box, self._selection_outputs())
            create_button.click(
                self.create_workspace_view,
                [new_name_box, ws_path_box],
                self._selection_outputs(),
            )
            app.load(self.refresh_outputs, None, self._load_outputs())
        return app

    def _load_outputs(self) -> list[object]:
        assert (
            self._workspace_md is not None
            and self._settings_md is not None
            and self._listing_md is not None
            and self._jobs_md is not None
            and self._ingest_md is not None
            and self._media_dd is not None
            and self._jobs_dd is not None
            and self._devices_dd is not None
            and self._ingest_jobs_dd is not None
            and self._segment_md is not None
            and self._extract_jobs_dd is not None
            and self._package_md is not None
            and self._clip_dd is not None
            and self._analysis_md is not None
            and self._analysis_jobs_dd is not None
            and self._analysis_sources_group is not None
            and self._signature_state is not None
        )
        return [
            self._workspace_md,
            self._devices_dd,
            self._listing_md,
            self._media_dd,
            self._jobs_md,
            self._jobs_dd,
            self._settings_md,
            self._signature_state,
            self._ingest_md,
            self._ingest_jobs_dd,
            self._segment_md,
            self._extract_jobs_dd,
            self._package_md,
            self._clip_dd,
            self._analysis_md,
            self._analysis_jobs_dd,
            self._analysis_sources_group,
        ]

    # ── HTTP 경계 — 모든 요청에 Host/Origin 가드, 미디어는 ID로만 ──

    def build_fastapi(self, mount_blocks: bool = True) -> FastAPI:
        app = FastAPI(
            title="vlogsrt 원본 브라우저", docs_url=None, redoc_url=None, openapi_url=None
        )
        self._add_guard_middleware(app)
        self._add_media_routes(app)
        if mount_blocks:
            # loopback 고정·share 없음·allowed_paths 없음(§4.4) — 이 마운트가 유일한
            # 화면 경로다. gradio의 파일 제공은 자기 임시 캐시로 한정된다.
            gr.mount_gradio_app(
                app,
                self.build_blocks(),
                path="/",
                server_name="127.0.0.1",
                server_port=self.port,
                allowed_paths=None,
                blocked_paths=None,
                ssr_mode=False,
                theme=gr.themes.Soft(),
                css=CSS,
            )
        return app

    def _add_guard_middleware(self, app: FastAPI) -> None:
        @app.middleware("http")
        async def host_origin_guard(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            """Gradio 엔드포인트·원본 엔드포인트 가리지 않고 전부 지난다(§4.4).

            Host는 loopback 고정(DNS 재바인딩 차단), 변경 요청은 Origin/Referer
            검사 — 머리글 없는 변경 요청도 거부한다. Gradio의 file= 경로는 이 화면이
            쓰지 않는다: 미디어 제공은 등록된 ID의 /media 경로뿐이므로 여기서 닫는다
            (allowed_paths 기본값은 닫힘의 증명이 아니라는 코디네이터 재결).
            """
            if request.url.path.startswith(("/gradio_api/file=", "/file=")):
                return JSONResponse(
                    {"detail": "파일 경로 제공은 닫혀 있다 — 등록된 미디어 ID만 제공된다"},
                    status_code=403,
                )
            if not raw_service.host_header_allowed(request.headers.get("host"), self.port):
                return JSONResponse(
                    {"detail": "허용되지 않은 Host다 — 이 서버는 로컬(loopback) 전용이다"},
                    status_code=403,
                )
            if request.method in MUTATION_METHODS and not raw_service.mutation_request_allowed(
                request.headers.get("origin"), request.headers.get("referer"), self.port
            ):
                return JSONResponse({"detail": "허용되지 않은 변경 요청이다"}, status_code=403)
            return await call_next(request)

    def _add_media_routes(self, app: FastAPI) -> None:
        @app.get("/media/{file_id}")
        def get_media(file_id: str, request: Request) -> Response:
            return self.media_response(file_id, request.headers.get("range"), want_body=True)

        @app.head("/media/{file_id}")
        def head_media(file_id: str, request: Request) -> Response:
            return self.media_response(file_id, request.headers.get("range"), want_body=False)

    def media_response(self, file_id: str, range_header: str | None, want_body: bool) -> Response:
        """등록된 미디어 하나를 제공한다 — 경로는 오지 않고 ID만 온다(§4.3·§4.4)."""
        root = self._snapshot_root()
        if root is None:
            return JSONResponse({"detail": "작업공간이 열려 있지 않다"}, status_code=404)
        try:
            resolved = self.raw.resolve_media(root, file_id)
        except (raw_service.MediaNotFoundError, raw_service.MediaNotAllowedError):
            # 미디어 ID는 전역 유일하고 해석은 **현재 작업공간에 묶여** 있다. 다른
            # 프로젝트의 ID는 이 프로젝트에서 존재하지 않는다 — 절대 그 파일로
            # 이어지지 않고, 소유 프로젝트를 다시 열라고 안내한다(루트 재현).
            return JSONResponse(
                {"detail": "없는 미디어 ID다 — 현재 작업공간의 등록 미디어만 제공된다"},
                status_code=404,
            )
        except raw_service.MediaNotReadyError:
            return JSONResponse({"detail": "아직 준비되지 않은 미디어다"}, status_code=409)
        except raw_service.MediaTargetChangedError:
            return JSONResponse(
                {"detail": "등록된 자리의 파일이 바뀌었다 — 다시 등록해야 한다"}, status_code=409
            )
        try:
            span = raw_service.parse_range(range_header, resolved.size_bytes)
        except raw_service.RangeNotSatisfiableError:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{resolved.size_bytes}",
                    "Accept-Ranges": "bytes",
                },
            )
        status = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Content-Disposition": "inline",
        }
        if span is None:
            headers["Content-Length"] = str(resolved.size_bytes)
            iterable: Iterator[bytes] = self._slices(resolved.path, 0, resolved.size_bytes - 1)
        else:
            status = 206
            headers["Content-Range"] = f"bytes {span.start}-{span.end}/{resolved.size_bytes}"
            headers["Content-Length"] = str(span.length)
            iterable = self._slices(resolved.path, span.start, span.end)
        if not want_body:
            return Response(status_code=status, headers=headers, media_type=resolved.content_type)
        return StreamingResponse(
            iterable, status_code=status, headers=headers, media_type=resolved.content_type
        )

    @staticmethod
    def _slices(path: Path, start: int, end: int) -> Iterator[bytes]:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(STREAM_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
