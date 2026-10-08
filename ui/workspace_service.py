"""브라우저 작업공간 UI의 서버 측 조립 — v2 계획의 정확 경로 예외 파일.

이 파일만 `vlogsrt.project.raw.service`를 가져올 수 있다(docs/PROJECT_CONTRACTS.md §1
둘째 예외). HTTP 경계도 여기 산다: Host/Origin 가드가 Gradio 엔드포인트와 원본
엔드포인트(`/media/{id}`)를 함께 지키고(§4.4), 미디어는 **등록된 ID로만** Range로
제공된다. 화면 상태는 `gr.State`가 아니라 서버(DB)가 소유한다(§14.1) — 새로고침은
다시 붙고, 서버 시작 때만 작업 복구가 일어난다.

레거시 단일 영상 화면(`ui/service.py`)은 그대로다. 이 모듈은 그것을 건드리지 않는다.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

# 로컬 전용 UI다 — gradio의 사용 보고가 네트워크를 만지지 않게 수입 전에 끈다.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402

from ui import chooser as ui_chooser  # noqa: E402
from ui import ui_facade  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402

STATE_ENV = "VLOGSRT_RAW_UI_STATE"
"""서버 상태 파일 경로 재정의 — 루트의 합성 작업공간 시험이 쓴다(사용자 .env 불가)."""

MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""Origin/Referer 검사를 강제하는 메서드 — Gradio 변경·업로드 엔드포인트가 여기 온다."""

STREAM_CHUNK_BYTES = 262144

REVIEW_EXPORT_JOB_KIND = "review_export"
"""확정 내보내기 작업 종류 — 제출 때의 검토 리비전·선택에 묶인 본문을 가진다."""

_CUE_SEEK_JS = (
    "(cue) => { if (!cue) return;"
    " const player = document.getElementById('caption-video');"
    " if (!player) return;"
    " if (player._vlLoop) { player._vlLoop(); }"
    " const row = document.querySelector('[data-cue-id=\"' + cue + '\"]');"
    " if (row) { player.currentTime = Number(row.dataset.seekStart);"
    " player.play(); } }"
)
"""큐 선택 → 재생기 이동(클라이언트 전용) — 서버 잰 시각을 큐 표의 data-*에서 읽는다.

서버 왕복이 없으므로 미디어 재생성·제공자 호출이 일어나지 않는다. 시각 계산은
서버가 미리보기 원점까지 포함해 구웠다 — 클라이언트는 읽기만 한다. 큐를 새로
고르는 것은 평범한 이동이므로 살아 있던 반복(`_vlLoop`)을 먼저 뗀다 — 묵은 반복
손잡이가 새로 고른 큐에서 어김없이 발동하면 안 된다. 빈 선택(표시 갱신이 드롭다운을
되돌릴 때)은 아무것도 만지지 않고 돌아간다."""


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


CSS = (
    """
#exec-log textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
#folder-listing { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
footer { display: none !important; }
.gradio-container { background:#202124!important; color:#f1f3f4!important;
 --body-background-fill:#202124; --block-background-fill:#292b2f;
 --body-text-color:#f1f3f4; --body-text-color-subdued:#b6bec6;
 --block-border-color:#41464c; --border-color-primary:#555e66;
 --button-secondary-background-fill:#343a40; --button-secondary-text-color:#f1f3f4;
 font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif; }
 .gradio-container input:not([type="checkbox"]):not([type="radio"]):not([type="range"]),
 .gradio-container textarea, .gradio-container select {
 background:#30343a!important; color:#f1f3f4!important; border-color:#555e66!important; }
.gradio-container input::placeholder, .gradio-container textarea::placeholder {
 color:#aeb9c4!important; }
.gradio-container .label-wrap, .gradio-container .label-wrap span { color:#e4e9ed!important; }
.gradio-container button.primary { background:#20b9ae!important; color:#062e2b!important; }
.vl-thumb-sel,.vl-int-sel { outline:2px solid #36d5c6!important; }
.vl-badge-warn { color:#ffbd8c!important; }
/* Gradio light-theme toasts keep their own pale background; pair both colors. */
.gradio-container .toast-body {
 background:#fff!important; color:#17212b!important; border:1px solid #74818b!important;
}
.gradio-container .toast-body * { color:#17212b!important; }
.gradio-container .toast-body.error { border-left:4px solid #b42318!important; }
#quick-status { background:#292b2f; color:#f1f3f4; border:1px solid #555e66;
 padding:10px 12px; border-radius:6px; }
#quick-status p, #quick-status strong { color:#f1f3f4!important; }
#quick-api-panel { border:1px solid #74818b; }


"""
    + ui_facade.WORKBENCH_CSS
)


WorkspaceView = tuple[
    str,  # 작업공간 머리글
    str | None,  # 서명(선택 검증 토큰)
    gr.Dropdown,  # 등록 미디어
    gr.Dropdown,  # 진행 중 작업
    str,  # 작업 로그
    str,  # 설정 요약
    str,  # 가져오기 패널
    gr.Dropdown,  # 재시도할 가져오기 작업
    str,  # 분할 패널
    gr.Dropdown,  # 재시도할 추출 작업
    str,  # package 패널
    gr.Dropdown,  # 게시된 조각
    str,  # 분석 패널
    gr.Dropdown,  # 재시도·재개 대상 분석 작업
    gr.CheckboxGroup,  # 분석 원본 선택
    str,  # 영상 작업대 껍데기
    str,  # 영상 작업대 본문
    str,  # 자막 작업대 본문
    str,  # 작업공간 경로 표시(읽기전용 — 선택기로만 정해진다)
    str,  # 원본 폴더 표시(읽기전용 — 전환마다 비운다: 낡은 출처가 남지 않는다)
    str,  # 최종 영상 경로 표시(읽기전용 — 전환마다 비운다)
]
"""작업공간 만들기·열기 이벤트가 한 번에 내리는 전체 보기 — `_selection_outputs`와 짝이다."""


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
        path_chooser: ui_chooser.PathChooser | None = None,
    ) -> None:
        self.port = port
        self.raw = raw_service.RawWorkspaceService()
        self.state_path = state_path if state_path is not None else default_state_path()
        self._inspector = inspector  # None이면 서비스 기본(ffprobe). 시험은 가짜 조사자를 넣는다.
        # 네이티브 경로 선택기 — 화면은 직접 입력 경로를 받지 않는다. 시험은 가짜를 넣는다.
        self._path_chooser = path_chooser or ui_chooser.applescript_path_chooser
        # 네이티브 대화상자는 한 번에 하나 — 서버 자물쇠가 보장한다(화면 단추의 공유
        # 동시성 묶음과 짝이다. 종류가 달라도 대화상자가 겹치지 않는다).
        self._chooser_lock = threading.Lock()
        # Explicit test transports remain injectable. Normal startup loads .env
        # through the app loader and keeps credentials only in process memory.
        # Client construction and paid requests still require the selected-scope approval.
        self._live_transport = live
        self._startup_credentials = raw_service.capture_browser_credentials()
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
        # 선택 오디오 미리보기 상태 — 미디어·트랙이 바뀐 경우에만 다시 만든다.
        # 표시 갱신(검토 새로고침·편집)은 재생기 구성요소를 그대로 둬서 재생 머리가
        # 살아 있고, ffmpeg도 다시 돌지 않는다.
        self._caption_preview: raw_service.CaptionPreviewMedia | None = None
        self._caption_preview_signature: str | None = None
        self._caption_cue_rows: tuple[ui_facade.CueRowView, ...] = ()
        self._caption_cue_clock: dict[str, tuple[Fraction, Fraction]] = {}
        # 마지막 확정 내보내기 자리 — Finder 열기·경로 복사·정리 뒤 재검증의 대상.
        self._last_export_path: str | None = None
        self._last_export_dir: str | None = None
        self._capcut_xml: dict[str, str] = {}
        self._capcut_quote_phase: dict[str, str] = {}
        self._capcut_render_key: tuple | None = None
        # 자막 작업대의 확정 조건 표시 — 검토 상태를 다시 읽을 때마다 갱신한다.
        self._caption_warnings_open: int = 0
        self._caption_unplaced_open: int = 0
        # Finder 여는 경계 — 시험은 가짜를 넣는다(실제는 macOS `open -R`, 인자 검증 후).
        self._finder_opener = finder_opener
        # 최종 자막 실제 실행 제공자 주입 경계(시험·루트 가짜). 없으면 생산 transport.
        self._caption_providers = caption_providers
        self._root: Path | None = None
        self._selection_lock = threading.Lock()
        self._quick: dict[str, Any] = {}
        self._quick_tasks: dict[str, dict[str, Any]] = {}
        self._retired_quick_tokens: set[str] = set()
        self._analysis_slot = threading.Semaphore(1)
        self._analysis_queue: list[str] = []
        self._library_quotes: dict[Path, Any] = {}
        self._library_cached_ids: dict[Path, set[str]] = {}
        self._review_publish_lock = threading.Lock()
        self._selection_auto_approval: Callable[[Any], bool] | None = None
        self._last_listing: raw_service.FolderListing | None = None
        # 가져오기 화면 상태 — 조사 제안과 확정은 **현재 작업공간 서명에 묶여**
        # 산다. 작업공간을 바꾸는 순간 함께 지운다(새 상태로 시작한다).
        self._ingest_proposal: raw_service.IngestProposal | None = None
        self._ingest_signature: str | None = None
        self._ingest_confirmation: raw_service.IngestConfirmation | None = None
        # FCP 라이브러리 가져오기 상태 — 열린 라이브러리·논리 클립 조사·역할 선택.
        # 일반 SD/폴더 조사와 같은 제안 칸을 쓰되, 라이브러리 사실은 여기 남는다.
        self._fcp_library: Path | None = None
        self._fcp_scan: raw_service.FcpLibraryScan | None = None
        self._fcp_signature: str | None = None
        self._fcp_error: str | None = None
        self._fcp_role: str = raw_service.FCP_ROLE_ORIGINAL
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
        # 읽기전용 경로 표시 칸들 — 사람이 직접 적는 경로 입력은 없다(선택기만).
        self._ws_path_box: gr.Textbox | None = None
        self._source_box: gr.Textbox | None = None
        self._caption_path_box: gr.Textbox | None = None
        self._project_name_box: gr.Textbox | None = None
        self._fcp_role_dd: gr.Dropdown | None = None
        # 선택기·조사 단추 참조 — 배선 증명은 실제 Blocks 의존성(app.config)이 한다.
        self._open_button: gr.Button | None = None
        self._create_button: gr.Button | None = None
        self._browse_button: gr.Button | None = None
        self._scan_button: gr.Button | None = None
        self._caption_pick_button: gr.Button | None = None
        self._caption_md: gr.Markdown | None = None
        self._caption_player_html: gr.HTML | None = None
        self._caption_table_html: gr.HTML | None = None
        self._caption_cue_dd: gr.Dropdown | None = None
        self._clip_player_html: gr.HTML | None = None
        # V2 작업대 구성요소 — 영상(껍데기·본문)과 자막(본문). 껍데기는 작업공간
        # 전환 때만 다시 그려 영상이 언로드되고, 본문은 서버 함수 응답마다 값이
        # 갈아지며 `watch`로 다시 묶인다(재생 머리는 본문 밖에 살아 있다).
        self._visual_player_block: gr.HTML | None = None
        self._visual_workbench_block: gr.HTML | None = None
        self._caption_workbench_block: gr.HTML | None = None
        # 작업대 수명 토큰 — 작업공간 전환마다 `_clear_screen_state`에서 다시
        # 파진다(서버 인스턴스 수명과도 묶인다: 재시작하면 새 값). 편집·제안·
        # 준비·자막 저장/확정은 화면이 보낸 토큰이 지금 것과 같아야 한다.
        self._visual_lifetime_token = uuid.uuid4().hex
        self._caption_lifetime_token = uuid.uuid4().hex
        self._signature_state: gr.State | None = None
        # 고급(기존) 패널의 클라이언트 문맥 상태 — 화면이 마지막으로 본 수명 토큰과
        # 검토 리비전/자막 머리. 편집·계획·재추출·내보내기·자막 수동 편집·확정의
        # 실제 배선은 이 값을 실어 오고, 서버는 지금 값과 같을 때만 적용한다
        # (다른 클라이언트가 머리를 옮긴 옛 사건·A→B→A 옛 사건 거부, A6 실측).
        # 전역 토큰은 새로 만들지 않는다 — 작업대 수명 토큰을 그대로 재사용한다.
        self._edit_context_state: gr.State | None = None
        self._review_head_state: gr.State | None = None
        self._caption_context_state: gr.State | None = None
        self._caption_head_state: gr.State | None = None
        # 실제 배선 증명 — 시험이 잡는 대상(이름 → (inputs, outputs)).
        self._advanced_wirings: dict[str, tuple[list[object], list[object]]] = {}
        # 배선에 실제로 묶인 핸들러 — 시험이 옛 문맥 사건을 실제 배선 경로로 잰다.
        self._advanced_handlers: dict[str, Callable[..., Any]] = {}

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
        self._quick = self.raw.load_selection_state(root)
        if (
            self._quick.get("asset_ids")
            and "VLOGSRT_OFFLINE" in str(self._quick.get("error", ""))
            and raw_service.browser_network_mode() == "online"
        ):
            # Rebuild the failed selection's local quote, never its paid phase.
            self._start_quick(root, (), asset_ids=tuple(self._quick["asset_ids"]), retry=True)
        note = f"작업공간 {status.name}({status.workspace_root})을 다시 열었다."
        if recovered:
            note += f" 서버 종료로 중단된 작업 {recovered}건을 재개 가능 상태로 돌렸다."
        self._prepare_library_quote(root)
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
            self._fcp_library = None
            self._fcp_scan = None
            self._fcp_signature = None
            self._fcp_error = None
            self._asr_approval_record = None
            self._semantic_approval_record = None
            self._clear_screen_state()

    def _remember(self, root: Path | None) -> None:
        with self._selection_lock:
            self._root = root
            self._ingest_proposal = None
            self._ingest_signature = None
            self._ingest_confirmation = None
            self._fcp_library = None
            self._fcp_scan = None
            self._fcp_signature = None
            self._fcp_error = None
            self._asr_approval_record = None
            self._semantic_approval_record = None
            self._clear_screen_state()
        if root is not None:
            self._write_state(root)
            self._quick = self.raw.load_selection_state(root)
            self._prepare_library_quote(root)

    def _clear_screen_state(self) -> None:
        """검토·자막·내보내기 화면 상태 초기화 — 작업공간 전환은 새 상태로 시작한다.

        작업대 **수명 토큰**도 여기서만 다시 파진다(작업공간 열기·만들기·같은
        프로젝트 재오픈 전부). 서명이 결정적이라 A→B→A에서 같아지는 것과 달리
        토큰은 무작위라 옛 화면의 편집·제안·준비 사건이 되살아 적용되지 않는다.
        보통의 새로고침·편집 재그림은 이 메서드를 거치지 않아 토큰이 그대로다.
        """
        self._quick = {}
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
        self._caption_preview = None
        self._caption_preview_signature = None
        self._caption_cue_rows = ()
        self._caption_cue_clock = {}
        self._caption_warnings_open = 0
        self._caption_unplaced_open = 0
        self._last_export_path = None
        self._last_export_dir = None
        self._visual_lifetime_token = uuid.uuid4().hex
        self._caption_lifetime_token = uuid.uuid4().hex

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

    def _require_visual_context(self, nonce: str | None, shown_revision: str | None) -> None:
        """고급 검토 편집의 클라이언트 문맥 검증 — 수명 토큰(ABA) + 화면이 본 리비전.

        기존 그라디오 단추는 서명+ID만 실어, 늦은 사건이 서버의 공용 화면 상태
        (머리)를 몰래 따라갔다(A6 실측: 다른 클라이언트가 저장해 머리가 옮겨도 옛
        머리 사건이 적용되고, A→B→A 뒤 옛 사건이 그대로 들어갔다). 검증은 적용보다
        앞서고, 값이 다르면 그 자리에서 거부한다 — 어떤 상태도 바꾸지 않는다.
        """
        if not nonce or nonce != self._visual_lifetime_token:
            raise gr.Error("검토 화면 수명 토큰이 늦었다 — 화면을 새로고침한 뒤 다시 시도하세요.")
        if shown_revision != self._review_revision_id:
            raise gr.Error(
                "편집이 늦었다 — 화면이 본 검토 리비전과 서버 머리가 다르다."
                " **검토 새로고침** 뒤 다시 시도하세요."
            )

    def _require_caption_context(self, nonce: str | None, shown_head: object) -> None:
        """자막 수동 편집·확정의 클라이언트 문맥 검증 — 수명 토큰 + 화면이 본 머리."""
        if not nonce or nonce != self._caption_lifetime_token:
            raise gr.Error("자막 화면 수명 토큰이 늦었다 — 화면을 새로고침한 뒤 다시 시도하세요.")
        try:
            head = None if shown_head is None else int(str(shown_head))
        except (TypeError, ValueError):
            head = -1
        if head != self._caption_head_sequence:
            raise gr.Error(
                "편집이 늦었다 — 화면이 본 머리와 서버 머리가 다르다."
                " **검토 새로고침** 뒤 다시 시도하세요."
            )

    def _review_panel_call(
        self, fn: Callable[..., Any], *, gated: bool
    ) -> Callable[..., tuple[Any, ...]]:
        """검토 패널 단추의 실제 배선 핸들러 — 문맥(토큰·화면이 본 리비전)을 주고받는다.

        gated=True(편집·계획 반영·재추출·내보내기 계열)은 문맥 검증을 **먼저**
        한다: 옛 토큰(작업공간 전환·재시작)·옛 머리(다른 클라이언트 편집) 사건은
        여기서 거부되고 원본 뷰 함수에 닿지 않는다. gated=False(시작·새로고침·
        조회)는 검증 없이 새 문맥만 내려 늦은 화면의 회복 길을 연다. 뷰 함수
        자체는 직접 호출 호환(시험·스크립트)을 위해 서명을 그대로 둔다.
        """

        def handler(
            nonce: str | None, shown_revision: str | None, signature: str | None, *args: object
        ) -> tuple[Any, ...]:
            if gated:
                self._require_visual_context(nonce, shown_revision)
            result = fn(signature, *args)
            tail: tuple[Any, ...] = (self._review_revision_id, self._visual_lifetime_token)
            return (*result, *tail) if isinstance(result, tuple) else (result, *tail)

        return handler

    def _wire_review_button(
        self,
        name: str,
        button: gr.Button,
        fn: Callable[..., Any],
        boxes: list[object],
        *,
        gated: bool,
        outputs: list[object] | None = None,
    ) -> None:
        """검토 계열 단추 배선 — 문맥 상태를 입력·출력에 함께 단다.

        입력의 앞두 값(토큰·화면이 본 리비전)은 사건이 일어난 순간의 클라이언트
        소유 값이다 — 늦은 큐 사건은 옛 값을 실어 온다. `_advanced_wirings`에
        실제 inputs/outputs을 남겨 시험이 배선 자체를 잰다.
        """
        assert self._edit_context_state is not None
        assert self._review_head_state is not None
        inputs = [
            self._edit_context_state,
            self._review_head_state,
            self._signature_state,
            *boxes,
        ]
        targets = [
            *(outputs if outputs is not None else [self._review_md]),
            self._review_head_state,
            self._edit_context_state,
        ]
        handler = self._review_panel_call(fn, gated=gated)
        button.click(handler, inputs, targets)
        self._advanced_wirings[name] = (inputs, targets)
        self._advanced_handlers[name] = handler

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
            return (
                "**폴더 찾아보기(선택기)** 또는 마운트된 장치 후보로 고르면 한 층만 나열한다"
                "(내용 읽지 않음)."
            )
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

    def _project_tab(self) -> tuple[gr.Button, gr.Button]:
        """1번 탭 조립 — 전환 단추를 돌려준다(배선은 build_blocks에서).

        경로는 **선택기로만** 정해진다 — 경로 칸은 전부 읽기전용 표시다. 사람이
        직접 적는 경로 입력은 이 화면에 없다(브라우저는 경로를 몰라도 된다).
        """
        with gr.Accordion(
            "프로젝트 · 라이브러리 가져오기", open=False, elem_id="project-tools"
        ) as self._project_tools:
            self._workspace_md = gr.Markdown(self.workspace_markdown(), visible=False)
            with gr.Row():
                self._ws_path_box = gr.Textbox(
                    label="작업공간 경로(표시)",
                    info="선택기로 고른 자리를 보여줄 뿐 직접 적을 수 없다.",
                    placeholder="아래 단추로 폴더 또는 Final Cut 라이브러리를 고르세요",
                    scale=6,
                    interactive=False,
                )
                open_button = gr.Button("프로젝트 열기", scale=2)
            with gr.Row():
                self._project_name_box = gr.Textbox(
                    label="새 프로젝트 이름",
                    placeholder="제주 3일 브이로그",
                    scale=6,
                )
                create_button = gr.Button("새 프로젝트", scale=2)
                self._fcp_primary_button = gr.Button("Final Cut 라이브러리에서 가져오기", scale=3)
            with gr.Row():
                self._direct_project_path = gr.Textbox(
                    label="선택 창이 안 뜨면 경로 붙여넣기",
                    placeholder="/프로젝트/폴더 또는 /라이브러리.fcpbundle",
                    scale=6,
                )
                self._direct_project_open = gr.Button("경로로 열기", scale=2)
            self._fcp_originals_group = gr.CheckboxGroup(
                label="가져올 오리지널 영상", choices=[], interactive=True, visible=False
            )
            self._fcp_primary_info = gr.Markdown(visible=False)
            self._fcp_primary_import = gr.Button(
                "선택한 오리지널 연결", visible=False, variant="primary"
            )

            with gr.Accordion("폴더 · SD · Final Cut 가져오기", open=False):
                gr.Markdown("---")
                with gr.Row():
                    self._source_box = gr.Textbox(
                        label="원본 폴더(SD 카드) — 선택기 표시",
                        info="가져올 원본이 있는 폴더. 선택기로 고른 뒤 조사 → 원본 순서 확인 →"
                        " 복사 및 검증이 이 탭에서 바로 이어진다.",
                        placeholder="폴더 찾아보기 또는 마운트된 장치 후보로 고르세요",
                        scale=6,
                        interactive=False,
                    )
                    browse_button = gr.Button("폴더 찾아보기(선택기)", scale=2)
                self._devices_dd = gr.Dropdown(
                    choices=self.device_choices(),
                    label="마운트된 장치 후보",
                    info="후보를 보여줄 뿐, 누르기 전에는 아무 것도 조사하지 않는다.",
                    interactive=True,
                )
                self._listing_md = gr.Markdown(
                    self._listing_markdown(None), elem_id="folder-listing"
                )

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

                self._browse_button = browse_button
                browse_button.click(
                    self.source_folder_chooser_view,
                    None,
                    [self._listing_md, self._source_box],
                    api_name=False,
                    concurrency_limit=1,
                    concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
                )
                self._devices_dd.select(
                    on_device, self._devices_dd, [self._listing_md, self._source_box]
                )

                # ── 가져오기 — 조사 → 순서 확인(명시적) → 복사·검증 → 새로고침/재시도 ──
                gr.Markdown("---")
                with gr.Row():
                    scan_button = gr.Button("조사 (가져올 영상 확인)", scale=2)
                    confirm_button = gr.Button("원본 순서 확인", variant="primary", scale=2)
                    copy_button = gr.Button("복사 및 검증", scale=2)
                    refresh_ingest = gr.Button("진행 상태 새로고침", scale=2)
                self._fcp_role_dd = gr.Dropdown(
                    choices=[
                        ("원본(Original Media)", raw_service.FCP_ROLE_ORIGINAL),
                        ("프록시(Proxy Media)", raw_service.FCP_ROLE_PROXY),
                    ],
                    value=raw_service.FCP_ROLE_ORIGINAL,
                    label="FCP 원본 역할 — 원본·프록시가 같은 클립이면 시간순 작업에는 이 역할만",
                    info="다른 역할 파일도 라이브러리에 그대로 남는다(읽기만 한다). 바꾸고 다시"
                    " 조사하면 제안이 해당 역할로 다시 만들어진다.",
                    interactive=True,
                )
                with gr.Row():
                    self._ingest_jobs_dd = gr.Dropdown(
                        choices=self.resumable_ingest_choices(),
                        label="재시도할 가져오기 작업",
                        interactive=True,
                        scale=6,
                    )
                    retry_button = gr.Button("재시도", scale=1)
                self._ingest_md = gr.Markdown(self._ingest_state_markdown(self._snapshot_root()))
                self._scan_button = scan_button
                scan_button.click(
                    self.ingest_scan_view,
                    [self._signature_state, self._source_box, self._fcp_role_dd],
                    self._ingest_md,
                )
                confirm_button.click(
                    self.ingest_confirm_view, self._signature_state, self._ingest_md
                )
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
        # 선택기 단추 참조 — 배선 증명은 실제 Blocks 의존성으로 한다(별도 장부 없음).
        self._open_button = open_button
        self._create_button = create_button
        return open_button, create_button

    # ── 화면 2 — 분석 · 조각 검토 ──────────────────────────────

    def _capcut_result(self, root: Path, message: str) -> tuple[str, str, str]:
        self._refresh_review_panel_state(root)
        pending = self._unreviewed_auto_count(root)
        if pending:
            message += (
                f"\n\n**자동 제외 {pending}개 미검토** · 확인하기 전에는 Final Cut으로 출력할 수"
                " 없습니다. 제외를 유지하려면 '자동 제외 확인'을 누르세요."
            )
        return message, *self.visual_workbench_render(root)

    def _unreviewed_auto_count(self, root: Path) -> int:
        """사람이 아직 보지 않은 자동 제외 수 — 검토 머리가 없으면 셀 것이 없다."""
        try:
            return self._review_facade(root).unreviewed_auto_exclusion_count()
        except (raw_service.RawWorkspaceError, raw_service.ReviewError):
            return 0

    def capcut_fcp_panel_view(self) -> tuple:
        root = self._snapshot_root()
        if self._fcp_scan is None and root is not None:
            library = self.raw.remembered_fcp_library(root)
            if library is not None:
                try:
                    self._scan_fcp_view(
                        root, library, raw_service.FCP_ROLE_ORIGINAL, originals_only=False
                    )
                except gr.Error as error:
                    return (
                        gr.CheckboxGroup(choices=[], value=[], visible=False),
                        gr.Markdown(f"원본 라이브러리 연결을 확인하세요: {error}", visible=True),
                        gr.Button(visible=False),
                    )
        scan = self._fcp_scan
        if scan is None or self._fcp_signature != self.current_signature():
            return (
                gr.CheckboxGroup(choices=[], value=[], visible=False),
                gr.Markdown(visible=False),
                gr.Button(visible=False),
            )
        original_paths = {file.media_path for file, _ in scan.selected}
        choices = [
            (item.source_relative_path, str(index))
            for index, item in enumerate(scan.proposal.order)
            if str(Path(item.source_root) / item.source_relative_path) in original_paths
        ]
        message = (
            f"**{Path(scan.library_root).name}** · 클립 {len(choices)}개 · "
            "원본·프록시 위치를 함께 연결하고 대응 여부를 검증합니다. 영상은 복사하지 않습니다."
        )
        if scan.broken:
            message += "\n\n확인 필요 · " + "\n\n".join(
                f"{b.linked_from}: {b.reason}" for b in scan.broken
            )
        proxy_only = sum(c.original is None for c in scan.clips)
        if proxy_only:
            message += f"\n\n프록시 단독 {proxy_only}개 · 검토 가능, 원본 출력 전 재연결 필요."
        imported = (
            {
                str(Path(asset.source_root) / asset.source_relative_path)
                for asset in self.raw.list_source_assets(root)
                if asset.status == "ready"
            }
            if root
            else set()
        )
        selected = [
            value
            for _, value in choices
            if not imported
            or str(
                Path(scan.proposal.order[int(value)].source_root)
                / scan.proposal.order[int(value)].source_relative_path
            )
            in imported
        ]
        return (
            gr.CheckboxGroup(choices=choices, value=selected, visible=True),
            gr.Markdown(message, visible=True),
            gr.Button(visible=bool(choices)),
        )

    def direct_project_open_view(self, path_text: str) -> tuple:
        path = Path((path_text or "").strip().strip("\"'")).expanduser()
        if not path.is_absolute():
            raise gr.Error("프로젝트 폴더 또는 .fcpbundle의 전체 경로를 붙여넣으세요")
        if raw_service.is_fcp_library(path):
            values = self.open_fcp_library_view(path, originals_only=False)
        else:
            values = self.open_workspace_view(str(path))
        return (*values, *self.capcut_fcp_panel_view())

    def capcut_fcp_choose_view(self) -> tuple:
        chosen = self._pick_path(ui_chooser.WORKSPACE_KIND)
        if chosen is None:
            raise gr.Error("라이브러리 선택을 취소했습니다")
        library = Path(chosen).expanduser()
        if not raw_service.is_fcp_library(library):
            raise gr.Error("Final Cut 라이브러리(.fcpbundle)를 선택하세요")
        values = self.open_fcp_library_view(library, originals_only=False)
        if self._fcp_scan is not None and self._fcp_scan.proposal.order:
            self.capcut_fcp_import_view(
                self.current_signature(),
                [str(i) for i in range(len(self._fcp_scan.proposal.order))],
            )
        return (*values, *self.capcut_fcp_panel_view())

    def _cancel_quick(self) -> None:
        previous = self._quick
        for key in ("job_id", "preview_job_id", "review_job_id"):
            if previous.get(key) and previous.get("root"):
                with contextlib.suppress(raw_service.RawWorkspaceError):
                    self.raw.cancel_job(previous["root"], previous[key])

    def _load_quick_tasks(self, root: Path) -> None:
        for state in self.raw.load_selection_states(root):
            if (
                state.get("token") not in self._quick_tasks
                and state.get("token") not in self._retired_quick_tokens
            ):
                self._quick_tasks[state["token"]] = state
        if self._quick.get("root") == root and self._quick.get("token"):
            self._quick_tasks.setdefault(self._quick["token"], self._quick)

    def _task_for(
        self, root: Path, asset_ids: tuple[str, ...], paths: tuple[str, ...] = ()
    ) -> dict | None:
        self._load_quick_tasks(root)
        for state in reversed(list(self._quick_tasks.values())):
            if state.get("root") == root and (
                asset_ids
                and set(asset_ids) <= set(state.get("asset_ids", ()))
                or not asset_ids
                and paths
                and paths == tuple(state.get("paths", ()))
            ):
                return state
        return None

    def _activate_quick(self, state: dict, source_id: str | None = None) -> None:
        with self._selection_lock:
            self._quick = state
            self._quick_tasks[state["token"]] = state
            if source_id:
                state["view_source_id"] = source_id
            self._visual_lifetime_token = uuid.uuid4().hex
            self._capcut_render_key = None
            self.raw.save_selection_state(state["root"], state)

    def _quick_update(self, expected_token: str, **values: Any) -> bool:
        with self._selection_lock:
            state = self._quick_tasks.get(expected_token)
            if state is None and self._quick.get("token") == expected_token:
                state = self._quick
                self._quick_tasks[expected_token] = state
            if state is None or state.get("token") != expected_token:
                return False
            state.update(values)
            if state["token"] != expected_token:
                self._retired_quick_tokens.add(expected_token)
                self._quick_tasks.pop(expected_token, None)
                self._quick_tasks[state["token"]] = state
            if state.get("root"):
                self.raw.save_selection_state(
                    state["root"],
                    state,
                    selected=self._quick is state and self._root == state["root"],
                )
            return True

    def _quick_assert_current(self, root: Path, token: str, context: Any) -> None:
        # A task is invalidated only by an explicit cancel/replacement, never navigation.
        state = self._quick_tasks.get(token)
        if state is None or state.get("root") != root or state.get("token") != token:
            self.raw.cancel_job(root, context.job_id)
        context.check_cancel()

    def _start_quick(
        self,
        root: Path,
        paths: tuple[str, ...],
        dependency: Any = None,
        asset_ids: tuple[str, ...] = (),
        retry: bool = False,
        approved_quote: Any = None,
        activate: bool = True,
    ) -> None:
        existing = self._task_for(root, asset_ids, paths)
        if (
            approved_quote is not None
            and existing is not None
            and existing.get("stage")
            not in ("분석 대기", "분석 시작", "전사·장면 준비", "Luna 추천 구간 분석")
            and tuple(existing.get("asset_ids", ())) != asset_ids
        ):
            existing = None
        if existing is not None:
            active = existing.get("stage") in (
                "영상 준비",
                "분석 대기",
                "분석 시작",
                "전사·장면 준비",
                "Luna 추천 구간 분석",
            )
            if (not retry and approved_quote is None) or active:
                if not activate:
                    return
                self._activate_quick(existing, asset_ids[0] if asset_ids else None)
                if asset_ids:
                    self.raw.start_source_preview(root, asset_ids=(asset_ids[0],))
                if existing.get("revision") and existing.get("stage") in (
                    "추천 구간 준비 완료",
                    "부분 완료 · 확인 필요",
                ):
                    view_token = self._visual_lifetime_token

                    def show_cached(context: Any) -> None:
                        with self._review_publish_lock:
                            if (
                                view_token == self._visual_lifetime_token
                                and self._snapshot_root() == root
                            ):
                                self.raw.publish_selection_review(
                                    root, existing["revision"], context
                                )

                    handle = self.raw.submit_job(root, "selection_review", view_token, show_cached)
                    self._quick_update(existing["token"], review_job_id=handle.job_id)
                return
        token = uuid.uuid4().hex
        state = {
            "root": root,
            "token": token,
            "paths": paths,
            "asset_ids": asset_ids,
            "stage": "영상 준비",
            "revision": None,
            "quote": None,
        }
        if activate:
            self._activate_quick(state, asset_ids[0] if asset_ids else None)
        else:
            self._quick_tasks[token] = state
            self.raw.save_selection_state(root, state, selected=False)

        if approved_quote is not None:
            with self._selection_lock:
                self._analysis_queue.append(token)

        def prepare(context: Any) -> None:
            try:
                if dependency is not None:
                    self.raw.wait_child_job(root, dependency, context)
                self._quick_assert_current(root, token, context)
                self.raw.ensure_source_order(root)
                ids = asset_ids or self.raw.selected_asset_ids(root, paths)
                if not ids:
                    raise raw_service.RawWorkspaceError("선택 영상의 준비가 끝나지 않았습니다")
                self._quick_update(token, asset_ids=ids)
                if activate:
                    preview_id = str(state.get("view_source_id") or ids[0])
                    preview = self.raw.start_source_preview(root, asset_ids=(preview_id,))
                    self._quick_update(token, preview_job_id=preview.job_id)
                cached = self.raw.cached_selection_analysis(root, ids)
                if cached:
                    self._quick_update(token, stage="저장된 Luna 결과", revision=cached)
                    with self._review_publish_lock:
                        if self._quick.get("token") == token and self._snapshot_root() == root:
                            self.raw.publish_selection_review(root, cached, context)
                    self._library_cached_ids.setdefault(root, set()).update(ids)
                    self._quick_update(token, stage="추천 구간 준비 완료")
                    return
                quote = approved_quote or self.raw.selection_quote(root, ids)
                if set(ids) == set(self._library_ids(root)):
                    self._library_quotes[root] = quote
                self._quick_assert_current(root, token, context)
                self._quick_update(token, quote=quote, stage="승인 대기")
                if approved_quote is not None or (
                    self._selection_auto_approval is not None
                    and self._selection_auto_approval(quote)
                ):
                    self._run_quick(root, token, replace(quote, approved=True), context)
            except Exception as error:
                self._quick_update(token, stage="분석 확인 필요", error=str(error))
                raise
            finally:
                with self._selection_lock:
                    if token in self._analysis_queue:
                        self._analysis_queue.remove(token)

        handle = self.raw.submit_job(root, "selection_analysis", token, prepare)
        self._quick_update(token, job_id=handle.job_id)

    def _run_quick(self, root: Path, token: str, quote: Any, context: Any) -> None:
        import time

        self._quick_update(token, stage="분석 대기")
        with self._selection_lock:
            if token not in self._analysis_queue:
                self._analysis_queue.append(token)
        acquired = False
        try:
            while not acquired:
                self._quick_assert_current(root, token, context)
                with self._selection_lock:
                    first = bool(self._analysis_queue) and self._analysis_queue[0] == token
                if first:
                    acquired = self._analysis_slot.acquire(blocking=False)
                if not acquired:
                    time.sleep(0.05)
            self._execute_quick(root, token, quote, context)
        finally:
            with self._selection_lock:
                if token in self._analysis_queue:
                    self._analysis_queue.remove(token)
            if acquired:
                self._analysis_slot.release()

    def _execute_quick(self, root: Path, token: str, quote: Any, context: Any) -> None:
        self._quick_assert_current(root, token, context)
        transport = self._live_transport_for_action()

        def report(stage: str, revision: str | None) -> None:
            self._quick_assert_current(root, token, context)
            self._quick_update(token, stage=stage, revision=revision)

        revision = self.raw.run_selection_analysis(root, quote, transport, context, report)
        self._quick_assert_current(root, token, context)
        with self._review_publish_lock:
            if self._quick.get("token") == token and self._snapshot_root() == root:
                self.raw.publish_selection_review(root, revision, context)
        self._quick_assert_current(root, token, context)
        stage, error = self.raw.selection_completion(root, revision)
        if stage == "추천 구간 준비 완료":
            self._library_cached_ids.setdefault(root, set()).update(quote.asset_ids)
        self._quick_update(token, stage=stage, revision=revision, error=error)

    def _library_ids(self, root: Path) -> tuple[str, ...]:
        ready = {a.asset_id for a in self.raw.list_source_assets(root) if a.status == "ready"}
        order = self.raw.latest_global_source_order(root)
        return tuple(e.asset_id for e in order.entries if e.asset_id in ready) if order else ()

    def _prepare_library_quote(self, root: Path) -> None:
        ids = self._library_ids(root)
        if not ids:
            return

        def prepare(context: Any) -> None:
            context.check_cancel()
            quote = self.raw.selection_quote(root, ids)
            tasks = self._task_views(root)
            cached = {
                source_id
                for source_id in ids
                if tasks.get(source_id, {}).get("stage") == "추천 구간 준비 완료"
                and self.raw.cached_selection_analysis(root, (source_id,))
            }
            context.check_cancel()
            self._library_cached_ids[root] = cached
            self._library_quotes[root] = quote

        self.raw.submit_job(root, "library_quote", repr(ids), prepare)

    def library_analysis_summary(self) -> str:
        root = self._snapshot_root()
        quote = self._library_quotes.get(root) if root else None
        if root is None or quote is None:
            return "라이브러리를 불러오면 분석 범위와 요청 상한을 안내합니다."
        cached = self._library_cached_ids.get(root, set()).intersection(quote.asset_ids)
        pending = [e for e in quote.estimates if e.asset_id not in cached]
        asr = sum(e.asr_chunk_count for e in pending)
        luna = sum(e.luna_request_count for e in pending)
        return (
            f"분석 대상 {len(pending)}개 · 저장 결과 {len(cached)}개 재사용 · "
            f"전사 최대 {asr}회 + 영상 분석 최대 {luna}회. 승인하면 두 단계가 이어집니다."
        )

    def library_analysis_button(self) -> Any:
        root = self._snapshot_root()
        quote = self._library_quotes.get(root) if root else None
        tasks = self._task_views(root) if root else {}
        active = any(
            t.get("stage") in ("분석 대기", "분석 시작", "전사·장면 준비", "Luna 추천 구간 분석")
            for t in tasks.values()
        )
        complete = quote is not None and all(
            tasks.get(a, {}).get("stage") == "추천 구간 준비 완료" for a in quote.asset_ids
        )
        label = "분석 진행 중" if active else "분석 완료" if complete else "자막·영상 분석 승인"
        return gr.Button(
            value=label, visible=True, interactive=bool(quote) and not active and not complete
        )

    def library_analysis_view(self, signature: str | None, api_key: str | None = "") -> tuple:
        root = self._require_current_selection(signature)
        ids = self._library_ids(root)
        quote = self._library_quotes.get(root)
        if not ids or quote is None or tuple(quote.asset_ids) != ids:
            self._prepare_library_quote(root)
            return (
                "분석 범위를 준비하고 있습니다. 표시된 범위를 확인한 뒤 분석을 시작하세요.",
                gr.Accordion(open=False),
                gr.Textbox(value=""),
            )
        fresh = self.raw.selection_quote(root, ids)
        if fresh.digest != quote.digest or fresh.workflow != quote.workflow:
            self._library_quotes[root] = fresh
            return (
                "영상이나 자막이 바뀌었습니다. 갱신된 분석 범위를 확인하고 다시 승인하세요.",
                gr.Accordion(open=False),
                gr.Textbox(value=""),
            )
        key = (api_key or "").strip()
        if key:
            self._startup_credentials = raw_service.EnvironmentCredentials(
                key, self._startup_credentials.base_url if self._startup_credentials else None
            )
        if self._startup_credentials is None and self._live_transport is None:
            if self._quick.get("token"):
                self._quick_update(self._quick["token"], connection_required=True)
            return (
                "API 연결에 키를 입력한 뒤 표시된 범위를 승인하세요.",
                gr.Accordion(open=True),
                gr.Textbox(value="", autofocus=True),
            )
        # This gesture approves the displayed aggregate ceiling; each worker
        # receives only its source's freshly checked quote and never auto-expands it.
        cached = self._library_cached_ids.get(root, set()).intersection(ids)
        invalid = {
            source_id
            for source_id in cached
            if not self.raw.cached_selection_analysis(root, (source_id,))
        }
        if invalid:
            self._library_cached_ids[root].difference_update(invalid)
            self._prepare_library_quote(root)
            return (
                "저장 결과의 입력이 바뀌었습니다. 갱신된 범위를 확인하세요.",
                gr.Accordion(open=False),
                gr.Textbox(value=""),
            )
        self.raw.update_settings(root, replace(quote.workflow, network_mode="online"))
        children = [
            replace(self.raw.selection_quote(root, (source_id,)), approved=True)
            for source_id in ids
            if source_id not in cached
        ]
        checked = self.raw.selection_quote(root, ids)
        if checked.digest != fresh.digest or checked.workflow != replace(
            fresh.workflow, network_mode="online"
        ):
            self._library_quotes[root] = checked
            return (
                "입력이 바뀌었습니다. 갱신된 범위를 확인하고 다시 승인하세요.",
                gr.Accordion(open=False),
                gr.Textbox(value=""),
            )
        selected = self._quick.get("view_source_id") or (self._quick.get("asset_ids") or ids)[0]
        for child in children:
            self._start_quick(
                root, (), asset_ids=child.asset_ids, approved_quote=child, activate=False
            )
        active = self._task_for(root, (selected,))
        if active:
            self._activate_quick(active, selected)
        self._library_quotes[root] = self.raw.selection_quote(root, ids)
        return (
            "승인한 영상들을 백그라운드에서 순서대로 분석합니다. "
            "다른 영상을 살펴봐도 계속 진행됩니다.",
            gr.Accordion(open=False),
            gr.Textbox(value=""),
        )

    def quick_approve_view(self, signature: str | None, api_key: str | None = "") -> str:
        root = self._require_current_selection(signature)
        state = self._quick
        if (
            state.get("root") != root
            or state.get("stage") != "승인 대기"
            or state.get("quote") is None
        ):
            raise gr.Error("영상을 선택하면 필요한 분석 범위가 먼저 표시됩니다")
        key = (api_key or "").strip()
        if key:
            self._startup_credentials = raw_service.EnvironmentCredentials(
                key, self._startup_credentials.base_url if self._startup_credentials else None
            )
        if self._startup_credentials is None and self._live_transport is None:
            self._quick_update(
                state["token"],
                connection_required=True,
                error="OpenAI API 키가 입력되지 않아 분석을 시작하지 않았습니다.",
            )
            return self._quick_status()
        token = state["token"]
        approved = replace(state["quote"], approved=True)
        self._quick_update(token, stage="분석 시작", error=None, connection_required=False)

        def execute(context: Any) -> None:
            try:
                self._run_quick(root, token, approved, context)
            except Exception as error:
                self._quick_update(token, stage="분석 확인 필요", error=str(error))
                raise

        handle = self.raw.submit_job(root, "selection_analysis", token + ":approved", execute)
        self._quick_update(token, job_id=handle.job_id)
        return "선택한 영상의 Luna 분석을 시작했습니다. 추천 구간이 타임라인 위에 표시됩니다."

    def quick_approve_panel_view(
        self, signature: str | None, api_key: str | None = ""
    ) -> tuple[str, gr.Accordion, gr.Textbox]:
        message = self.quick_approve_view(signature, api_key)
        needs_key = bool(self._quick.get("connection_required"))
        return (message, gr.Accordion(open=needs_key), gr.Textbox(value="", autofocus=needs_key))

    def quick_source_view(self, signature: str | None, source_id: str) -> tuple[str, str, str]:
        root = self._require_current_selection(signature)
        if source_id not in {
            a.asset_id for a in self.raw.list_source_assets(root) if a.status == "ready"
        }:
            raise gr.Error("현재 프로젝트의 영상을 선택하세요")
        if self._quick.get("view_source_id") != source_id:
            self._start_quick(root, (), asset_ids=(source_id,))
        return self._capcut_result(root, self._quick_status())

    def _quick_status(self) -> str:
        state = self._quick
        if not state:
            return "영상을 선택하면 미리보기와 Luna 추천 분석이 이어집니다."
        stage = str(state.get("stage", "영상 준비"))
        if state.get("connection_required"):
            return (
                "**연결 설정 필요 · 아직 분석하지 않았습니다**\n\n"
                "아래 **API 연결**에 OpenAI API 키를 입력한 뒤 **Luna 분석 시작**을 다시 누르세요. "
                "영상과 승인 범위는 그대로 유지됩니다."
            )
        if state.get("error"):
            return f"**{stage}** · {state['error']} · 원본은 유지됩니다."
        quote = state.get("quote")
        if stage == "전사·장면 준비" and state.get("root"):
            transcripts = [
                self.raw.source_transcript(state["root"], source_id)
                for source_id in state.get("asset_ids", ())
            ]
            if transcripts and all(t is not None for t in transcripts):
                count = sum(len(t["utterances"]) for t in transcripts if t is not None)
                prepared = self.raw.analysis_frame_progress(
                    state["root"], state.get("revision"), tuple(state.get("asset_ids", ()))
                )
                maximum = sum(e.frame_sample_count for e in quote.estimates) if quote else 0
                return (
                    f"**원본 자막 {count}개 표시 · 영상 프레임 준비 중 "
                    f"({prepared}/최대 {maximum})**\n\n"
                    "시간 자막을 먼저 확인할 수 있습니다. "
                    "이어서 같은 자막과 영상 프레임을 Luna가 함께 분석합니다."
                )
        if stage == "승인 대기" and quote is not None:
            requests = sum(max(1, e.luna_request_count) for e in quote.estimates)
            chunks = sum(e.asr_chunk_count for e in quote.estimates)
            duration = sum(float(Fraction(e.asr_seconds)) for e in quote.estimates)
            return (
                f"**Luna 분석 준비 완료** · 선택 영상 {len(quote.asset_ids)}개\n\n"
                f"전사 최대 {chunks}요청({duration:.0f}초) + Luna 최대 {requests}요청. "
                "실제 비용은 사용한 오디오·토큰에 따라 정해집니다.\n\n"
                "아래에서 한 번 승인하면 전사·장면 분석·추천 표시가 자동으로 이어집니다."
            )
        return f"**{stage}** · 추천은 청록색, 확인이 필요한 구간은 주황색으로 표시됩니다."

    def capcut_fcp_import_view(
        self, signature: str | None, selected: list[str] | None
    ) -> tuple[str, str, str]:
        root = self._require_current_selection(signature)
        scan = self._fcp_scan
        if scan is None or self._fcp_signature != signature or not selected:
            raise gr.Error("현재 라이브러리에서 가져올 오리지널을 선택하세요")
        original_paths = {file.media_path for file, _ in scan.selected}
        choices = {
            str(index): item
            for index, item in enumerate(scan.proposal.order)
            if str(Path(item.source_root) / item.source_relative_path) in original_paths
        }
        if not set(selected) <= choices.keys():
            raise gr.Error("라이브러리 목록이 바뀌었습니다. 다시 선택하세요")
        picked = tuple(item for key, item in choices.items() if key in selected)
        proposal = replace(
            scan.proposal, order=picked, scan=replace(scan.proposal.scan, candidates=picked)
        )
        dependency = self.raw.start_reference_import(
            root, proposal, prepare_preview=False, fcp_scan=scan
        )
        paths = tuple(str(Path(item.source_root) / item.source_relative_path) for item in picked)
        self._start_quick(root, paths, dependency)
        return self._capcut_result(root, self._quick_status())

    def capcut_choose_view(self, signature: str | None) -> tuple[str, str, str]:
        root = self._snapshot_root()
        if root is not None:
            self._require_current_selection(signature)
        chosen = self._pick_path(ui_chooser.VIDEO_KIND)
        if chosen is None:
            if root is None:
                raise gr.Error("영상 선택을 취소했습니다")
            return self._capcut_result(root, "영상 선택을 취소했습니다.")
        if root is None:
            project = Path(chosen).parent / (Path(chosen).stem + "-vlogsrt")
            if project.exists():
                self.raw.open_workspace(project)
            else:
                self.raw.create_workspace(project, Path(chosen).stem)
            self._remember(project)
            root = project
        else:
            self._require_current_selection(signature)
        child = self.raw.import_selected_video(root, Path(chosen))
        path = str(Path(chosen).parent.resolve() / Path(chosen).name)
        self._start_quick(root, (path,), child)
        return self._capcut_result(root, self._quick_status())

    def capcut_auto_view(self, signature: str | None, approve: bool) -> tuple[str, str, str]:
        root = self._require_current_selection(signature)
        summary = self.raw.analysis_overview(root)
        order = self.raw.latest_global_source_order(root)
        if summary and order and summary.revision.order_revision_id != order.revision_id:
            summary = None
        if summary is not None and summary.revision.status == "complete":
            self.raw.start_cleanup(root)
            return self._capcut_result(root, "자동 정리 중 · 컷 판정과 미리보기를 준비합니다.")
        phase = "semantic" if summary and summary.revision.status == "awaiting_semantic" else "asr"
        token = str(signature)
        if not approve or self._capcut_quote_phase.get(token) != phase:
            note = (
                self.analysis_semantic_estimate_view(signature)
                if phase == "semantic"
                else self.analysis_live_estimate_view(signature, None)
            )
            self._capcut_quote_phase[token] = phase
            return self._capcut_result(
                root, note + "\n\n비용·범위를 확인한 뒤 승인하고 자동 정리를 누르세요."
            )
        if phase == "asr":
            note, _ = self.analysis_live_asr_view(signature, None, True)
        else:
            assert summary is not None
            choices = self.resumable_analysis_choices(root)
            if not choices:
                raise gr.Error("재개할 분석이 없습니다")
            note, _ = self.analysis_semantic_resume_view(signature, choices[0][1], True)

            def finish(context: Any) -> None:
                # The semantic job is separately durable and owns provider calls.
                jobs = [
                    j
                    for j in self.raw.list_jobs(root)
                    if j.kind == raw_service.ANALYSIS_SEMANTIC_JOB_KIND
                ]
                if not jobs:
                    raise RuntimeError("의미 분석 작업을 찾을 수 없습니다")
                self.raw.wait_child_job(root, jobs[0], context)
                self.raw.wait_child_job(root, self.raw.start_cleanup(root), context)

            self.raw.submit_job(root, "cleanup_pipeline", summary.revision.revision_id, finish)
        self._capcut_quote_phase.pop(token, None)
        return self._capcut_result(root, note)

    def capcut_preview_view(self, signature: str | None) -> str:
        root = self._require_current_selection(signature)
        self.raw.ensure_source_order(root)
        self.raw.start_source_preview(root)
        return "원본 미리보기를 준비합니다. 분석 승인은 필요하지 않습니다."

    def capcut_refresh_view(self, signature: str | None) -> tuple[str, str, str]:
        root = self._require_current_selection(signature)
        return self._capcut_result(root, self._capcut_progress(root))

    def _capcut_progress(self, root: Path) -> str:
        names = {
            "selection_analysis": "Luna 분석",
            "automatic_cleanup": "자동 정리",
            "selected_video_import": "영상 가져오기",
            "reference_import": "원본 연결 · 복사 없음",
            "source_preview": "원본 미리보기",
            "cleanup_pipeline": "영상 정리",
            "capcut_export": "Final Cut 전달",
            "segment_extract": "컷 준비",
            "visual_prepare": "미리보기 준비",
            "understanding": "영상 분석",
            "understanding_semantic": "의미 분석",
        }
        jobs = self.raw.list_jobs(root, limit=8)
        labels = {
            "succeeded": "완료",
            "failed": "실패",
            "running": "진행 중",
            "queued": "대기",
            "cancelled": "취소됨",
            "paused": "중단",
            "interrupted": "중단",
        }
        lines = [
            f"{names.get(j.kind, '작업')} · {labels.get(j.status, j.status)} · "
            f"{j.progress_current}/{j.progress_total}" + (f" · {j.error}" if j.error else "")
            for j in jobs[:4]
        ]
        summary = self.raw.analysis_overview(root)
        if summary and summary.revision.status == "awaiting_semantic":
            lines.insert(0, "전사 완료 · 자동 정리를 눌러 다음 단계 비용을 확인하세요.")
        return "\n\n".join(lines) or "영상을 선택하고 자동 정리를 시작하세요."

    def _task_views(self, root: Path) -> dict[str, dict]:
        self._load_quick_tasks(root)
        result = {}
        for task in list(self._quick_tasks.values()):
            if task.get("root") == root:
                for source_id in task.get("asset_ids", ()):
                    result[source_id] = task
        return result

    def capcut_status_view(self, signature: str | None) -> Any:
        """Lightweight progress never waits behind a full workbench/media redraw."""
        if not signature or signature != self._signature_for(self._snapshot_root()):
            return gr.skip()
        root = self._snapshot_root()
        if root is None:
            return gr.skip()
        export = next(
            (j for j in self.raw.list_jobs(root, limit=10) if j.kind == "capcut_export"), None
        )
        if export and export.status in ("queued", "running"):
            return "**Final Cut 출력 준비 중** · 선택한 컷을 원본 기준으로 저장합니다."
        message = self._quick_status()
        if export and export.status == "succeeded" and self._capcut_xml.get(str(signature)):
            message += "\n\n**출력 완료** · 전달 메뉴에서 Final Cut으로 열 수 있습니다."
        others = {
            task["token"]
            for task in self._task_views(root).values()
            if task.get("token") != self._quick.get("token")
            and task.get("stage")
            in ("분석 대기", "분석 시작", "전사·장면 준비", "Luna 추천 구간 분석")
        }
        if others:
            message += f"\n\n다른 영상의 백그라운드 분석 {len(others)}개가 계속 진행됩니다."
        return message

    def capcut_tick_view(self, signature: str | None) -> tuple:
        if not signature or signature != self._signature_for(self._snapshot_root()):
            return gr.skip(), gr.skip(), gr.skip()
        root = self._require_current_selection(signature)
        view_token = self._visual_lifetime_token
        if not self._quick:
            self._quick = self.raw.load_selection_state(root)
        self._load_quick_tasks(root)
        jobs = self.raw.list_jobs(root, limit=8)
        transcript_revisions = tuple(
            (asset_id, (self.raw.source_transcript(root, asset_id) or {}).get("revision"))
            for asset_id in self._quick.get("asset_ids", ())
        )
        key = (
            signature,
            transcript_revisions,
            tuple((j.job_id, j.status, j.progress_current) for j in jobs),
            self._quick.get("stage"),
            self._quick.get("revision"),
        )
        if key == self._capcut_render_key and self._quick.get("stage") != "Luna 추천 구간 분석":
            return (
                self._quick_status() if self._quick else self._capcut_progress(root),
                gr.skip(),
                gr.skip(),
            )
        self._capcut_render_key = key
        message, _shell, workbench = self._capcut_result(
            root, self._quick_status() if self._quick else self._capcut_progress(root)
        )
        if view_token != self._visual_lifetime_token or root != self._snapshot_root():
            return gr.skip(), gr.skip(), gr.skip()
        return message, gr.skip(), workbench

    def capcut_edit_view(self, signature: str | None, action: str) -> tuple[str, str, str]:
        root = self._require_current_selection(signature)
        facade = self._review_facade(root)
        if action == "undo":
            facade.undo()
        else:
            facade.restore_automatic()
        return self._capcut_result(root, "검토 변경을 저장했습니다.")

    def capcut_confirm_auto_view(self, signature: str | None) -> tuple[str, str, str]:
        """미검토 자동 제외를 한 번에 확인한다 — 제외는 유지되고 새 리비전이 남는다(D35)."""
        root = self._require_current_selection(signature)
        facade = self._review_facade(root)
        before = facade.unreviewed_auto_exclusion_count()
        facade.confirm_automatic_exclusions()
        message = (
            f"자동 제외 {before}개를 확인했습니다. 제외는 그대로 유지됩니다."
            if before
            else "확인할 자동 제외가 없습니다."
        )
        return self._capcut_result(root, message)

    def capcut_cancel_view(self, signature: str | None) -> str:
        root = self._require_current_selection(signature)
        if self._quick.get("root") != root:
            return "취소할 선택 분석 작업이 없습니다."
        self._cancel_quick()
        if self._quick:
            self._quick_update(
                self._quick["token"], token=uuid.uuid4().hex, stage="취소됨", error=None
            )
        return "취소를 요청했습니다. 완료된 결과는 보존됩니다."

    def capcut_retry_view(self, signature: str | None) -> str:
        root = self._require_current_selection(signature)
        if self._quick.get("root") == root and self._quick.get("asset_ids"):
            self._start_quick(root, (), asset_ids=tuple(self._quick["asset_ids"]), retry=True)
            return "선택 영상의 실패·누락 단계를 확인합니다. 표시된 범위를 승인하면 재개합니다."
        for job in self.raw.list_jobs(root):
            if job.status not in ("failed", "interrupted", "paused"):
                continue
            if job.kind in (raw_service.ANALYSIS_JOB_KIND, raw_service.ANALYSIS_SEMANTIC_JOB_KIND):
                return self.analysis_retry_view(signature, job.job_id)
            if job.kind == "reference_import":
                if self._fcp_scan is None:
                    raise gr.Error("라이브러리를 다시 열어 원본 연결 상태를 확인하세요")
                self.raw.start_reference_import(root, self._fcp_scan.proposal)
                return "원본 참조를 다시 확인하고 미리보기를 준비합니다."
            if job.kind == "source_preview":
                self.raw.start_source_preview(root)
                return "원본 미리보기를 다시 준비합니다."
            if job.kind == "ingest_copy":
                self.raw.retry_ingest_copy(root, job.job_id)
                return "실패한 영상 가져오기를 다시 시작했습니다."
            if job.kind == raw_service.SEGMENT_JOB_KIND:
                child = self.raw.retry_segment_extraction(root, job.job_id)
                summary = self.raw.analysis_overview(root)

                def finish(context: Any, child: Any = child) -> None:
                    self.raw.wait_child_job(root, child, context)
                    head = self._review_facade(root).load_head().revision
                    for page in range(
                        1,
                        (len(head.segments) + raw_service.VISUAL_PAGE_SIZE - 1)
                        // raw_service.VISUAL_PAGE_SIZE
                        + 1,
                    ):
                        self.raw.wait_child_job(
                            root,
                            self.raw.start_visual_page_preparation(
                                root, base_plan_id=head.base_plan_id, page=page
                            ),
                            context,
                        )

                self.raw.submit_job(
                    root,
                    "automatic_cleanup",
                    summary.revision.revision_id if summary else child.job_id,
                    finish,
                )
                return "실패한 컷만 다시 준비하고 미리보기를 갱신합니다."
            if job.kind == "capcut_export":
                return self.capcut_handoff_view(signature)
        self.raw.start_cleanup(root)
        return "정리 작업을 다시 시작했습니다. 완료된 컷은 재사용합니다."

    def capcut_handoff_view(self, signature: str | None) -> str:
        root = self._require_current_selection(signature)
        facade = self._review_facade(root)
        head = facade.load_head().revision
        try:
            facade.require_automatic_exclusions_reviewed()
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error

        def body(context: Any) -> None:
            ready_revision = self.raw.materialize_review(root, context, head.revision_id)
            destination, xml = self.raw.export_final_cut(
                root, context, expected_review_revision_id=ready_revision
            )
            with self._selection_lock:
                if self._root == root and self._signature_for(root) == signature:
                    self._last_export_dir = destination
                    self._last_export_path = str(root / destination)
                    self._capcut_xml[str(signature)] = xml

        self.raw.submit_job(root, "capcut_export", head.revision_id, body)
        return (
            "사용할 컷의 패키지와 FCPXML을 생성·검증합니다. 완료 후 Final Cut에서 열기를 누르세요."
        )

    def capcut_open_view(self, signature: str | None) -> str:
        import subprocess

        root = self._require_current_selection(signature)
        xml = self._capcut_xml.get(str(signature))
        if not xml or not Path(xml).is_file() or root not in Path(xml).parents:
            raise gr.Error("검증된 FCPXML을 먼저 생성하세요")
        result = subprocess.run(
            ["/usr/bin/open", "-a", "Final Cut Pro", xml], capture_output=True, text=True
        )
        if result.returncode:
            raise gr.Error(
                "FCPXML은 생성됐지만 Final Cut을 열지 못했습니다: " + result.stderr[:200]
            )
        return "Final Cut에 열기를 요청했습니다. 앱 안에서 가져오기 결과를 확인하세요."

    def _review_tab(
        self,
    ) -> tuple[gr.Button, gr.Button, gr.Dropdown, gr.Button]:
        with gr.Tab("영상 정리"):
            with gr.Row():
                choose = gr.Button("영상 선택", visible=False)
                self._quick_library_button = gr.Button("라이브러리 불러오기")
                cancel = gr.Button("선택 분석 취소", scale=0)
                automatic = gr.Button("자동 정리", visible=False)
                quick_approve = gr.Button(
                    "자막·영상 분석 승인", visible=True, interactive=False, variant="primary"
                )
                handoff = gr.Button("선택한 컷 Final Cut으로 출력")
            library_summary = gr.Markdown(
                self.library_analysis_summary(), elem_id="library-analysis-scope"
            )
            self._capcut_status = gr.Markdown(visible=False)
            self._capcut_progress_md = gr.Markdown(self._quick_status(), elem_id="quick-status")
            # ── V2 영상 작업대 — 3단 무대(왼쪽 목록 · 가운데 재생기+렠 · 오른쪽 패널).
            # 두 구성요소가 같은 `#visual-stage` 격자에 들어가고, 껍데기·본문의
            # 중간 껍데기는 display:contents로 펴져 세 칸이 한 눈에 짝을 이룬다.
            # 영상 원소는 여전히 별도 구성요소(껍데기)라 같은 원본을 다시 고르는
            # 동안 재생이 이어진다(F1a·F1b).
            shell, workbench = self.visual_workbench_render(self._snapshot_root())
            with gr.Row(elem_id="visual-stage"):
                self._visual_player_block = gr.HTML(
                    shell,
                    elem_id="visual-player-block",
                    elem_classes=["vl-stage-cell"],
                )
                self._visual_workbench_block = gr.HTML(
                    workbench,
                    elem_id="visual-workbench-block",
                    elem_classes=["vl-stage-cell"],
                    js_on_load=ui_facade.VISUAL_BOOTSTRAP_JS,
                    server_functions=self._visual_server_functions(),
                )
            with gr.Accordion(
                "API 연결 · 처음 한 번", open=False, elem_id="quick-api-panel"
            ) as quick_api_panel:
                quick_api_key = gr.Textbox(
                    label="OpenAI API 키 (이 실행에서만 사용)",
                    type="password",
                    value="",
                    elem_id="quick-api-key",
                )
            with gr.Accordion("분석 실행 설정", open=False, visible=False):
                primary_network = gr.Dropdown(
                    choices=["offline", "online"],
                    value=self._current_network_mode(),
                    label="네트워크",
                    interactive=True,
                )
                primary_network.change(
                    self.settings_network_view,
                    [self._signature_state, primary_network],
                    self._capcut_status,
                )
            approval = gr.Checkbox(
                label="표시된 단계의 비용·범위를 확인하고 실행을 승인합니다",
                value=False,
                visible=False,
            )
            with gr.Accordion("복원 · 전달 · 고급 조작", open=False), gr.Row():
                confirm_auto = gr.Button("자동 제외 확인")
                restore = gr.Button("자동 제외 전체 복원")
                undo = gr.Button("실행 취소")
                refresh = gr.Button("결과 확인")
                preview = gr.Button("원본 미리보기 준비")
                retry = gr.Button("실패 구간 재시도")
                open_fcp = gr.Button("Final Cut에서 열기")
                open_folder = gr.Button("폴더 열기")
            outputs = [self._capcut_status, self._visual_player_block, self._visual_workbench_block]
            choose.click(
                self.capcut_choose_view,
                self._signature_state,
                outputs,
                api_name=False,
                concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
                concurrency_limit=1,
            ).then(self.current_signature, None, self._signature_state).then(
                self._ws_path_display, None, self._ws_path_box
            )
            quick_approve.click(
                self.library_analysis_view,
                [self._signature_state, quick_api_key],
                [self._capcut_progress_md, quick_api_panel, quick_api_key],
            ).then(
                fn=None,
                js="""() => {
                const status = document.getElementById('quick-status');
                if (status?.textContent.includes('연결 설정 필요')) {
                    requestAnimationFrame(() =>
                        document.querySelector('#quick-api-key input')?.focus());
                }
            }""",
            )
            gr.Timer(1).tick(
                self.library_analysis_button,
                None,
                quick_approve,
                show_progress="hidden",
            )
            gr.Timer(1).tick(
                self.library_analysis_summary,
                None,
                library_summary,
                queue=False,
                show_progress="hidden",
            )
            automatic.click(self.capcut_auto_view, [self._signature_state, approval], outputs).then(
                lambda: False, None, approval
            )
            handoff.click(self.capcut_handoff_view, self._signature_state, self._capcut_status)
            confirm_auto.click(self.capcut_confirm_auto_view, self._signature_state, outputs)
            restore.click(
                lambda sig: self.capcut_edit_view(sig, "restore"), self._signature_state, outputs
            )
            undo.click(
                lambda sig: self.capcut_edit_view(sig, "undo"), self._signature_state, outputs
            )
            refresh.click(self.capcut_refresh_view, self._signature_state, outputs)
            preview.click(self.capcut_preview_view, self._signature_state, self._capcut_status)
            cancel.click(self.capcut_cancel_view, self._signature_state, self._capcut_status)
            retry.click(self.capcut_retry_view, self._signature_state, self._capcut_status)
            open_fcp.click(self.capcut_open_view, self._signature_state, self._capcut_status)
            open_folder.click(
                self.review_export_open_view, self._signature_state, self._capcut_status
            )
            gr.Timer(1).tick(
                self.capcut_status_view,
                self._signature_state,
                self._capcut_progress_md,
                queue=False,
                show_progress="hidden",
            )
            gr.Timer(2).tick(
                self.capcut_tick_view,
                self._signature_state,
                [self._capcut_status, self._visual_player_block, self._visual_workbench_block],
                show_progress="hidden",
            )
            with gr.Accordion("고급 편집 도구", open=False):
                extract_button, refresh_segment, profile_dd, analysis_plan = (
                    self._review_advanced_tab()
                )
            self._package_tab()
        return extract_button, refresh_segment, profile_dd, analysis_plan

    def _review_advanced_tab(
        self,
    ) -> tuple[gr.Button, gr.Button, gr.Dropdown, gr.Button]:
        """2번 탭의 고급 패널 — 의미 분석·수동 조각 검토·결정적 분할(기존 흐름)."""
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
            live_asr_button = gr.Button("실제 호출 1단계(전사) 시작", variant="primary", scale=2)
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
        # 문맥 상태를 단다 — 시작·새로고침·조회는 검증 없이 새 문맥을 내리고(회복
        # 길), 편집·계획 반영·재추출은 옛 문맥 사건을 배선 핸들러에서 거부한다.
        self._wire_review_button(
            "review-initialize",
            review_init_button,
            self.review_initialize_view,
            [],
            gated=False,
        )
        self._wire_review_button(
            "review-refresh", review_refresh_button, self.review_refresh_view, [], gated=False
        )
        self._wire_review_button(
            "review-reextract", review_reextract_button, self.review_reextract_view, [], gated=True
        )
        self._wire_review_button(
            "review-plan", review_plan_button, self.review_plan_view, [], gated=True
        )
        self._wire_review_button(
            "review-filter",
            review_filter_button,
            self.review_filter_view,
            [review_query_box, review_tags_box],
            gated=False,
        )
        self._wire_review_button(
            "review-note",
            review_note_button,
            self.review_note_view,
            [review_segment_box, review_note_box],
            gated=True,
        )
        self._wire_review_button(
            "review-selection",
            review_selection_button,
            self.review_selection_view,
            [review_segment_box, review_selection_dd],
            gated=True,
        )
        self._wire_review_button(
            "review-boundary",
            review_boundary_button,
            self.review_boundary_view,
            [review_segment_box, review_boundary_box],
            gated=True,
        )
        self._wire_review_button(
            "review-split",
            review_split_button,
            self.review_split_view,
            [review_segment_box, review_split_box],
            gated=True,
        )
        self._wire_review_button(
            "review-merge",
            review_merge_button,
            self.review_merge_view,
            [review_merge_left_box, review_merge_right_box],
            gated=True,
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

    def _pick_path(self, kind: str) -> str | None:
        """네이티브 경로 선택기 — 한 번에 하나의 대화상자만 뜬다(자물쇠).

        취소(None)는 그대로 돌려오고 오류만 화면 오류로 옮긴다.
        """
        with self._chooser_lock:
            try:
                return self._path_chooser(kind)
            except ui_chooser.ChooserError as error:
                raise gr.Error(str(error)) from error

    def open_project_chooser_view(self) -> WorkspaceView:
        """기존 프로젝트 열기 — 파일시스템 선택기로만 경로가 들어온다(직접 입력 없음).

        폴더를 고르면 작업공간으로 열고, `.fcpbundle`을 고르면 라이브러리 밖 관리
        작업공간을 만들어 이벤트 미디어를 조사해 싣는다. 취소는 어떤 상태도 바꾸지
        않는다 — 지금 작업공간이 그대로다.
        """
        chosen = self._pick_path(ui_chooser.WORKSPACE_KIND)
        if chosen is None:
            raise gr.Error("취소 — 지금 작업공간을 그대로 둔다.")
        path = Path(chosen).expanduser()
        if raw_service.is_fcp_library(path):
            return self.open_fcp_library_view(path)
        if path.is_dir():
            return self.open_workspace_view(str(path))
        raise gr.Error(
            "폴더 또는 Final Cut 라이브러리(.fcpbundle)를 고르세요 — 파일 하나는 작업공간이 아니다."
        )

    def create_project_chooser_view(self, name: str) -> WorkspaceView:
        """새 프로젝트 만들기 — 이름만 적고 자리는 선택기로 고른다(경로 직접 입력 없음)."""
        if not (name or "").strip():
            raise gr.Error("새 프로젝트 이름을 입력하세요.")
        chosen = self._pick_path(ui_chooser.FOLDER_KIND)
        if chosen is None:
            raise gr.Error("취소 — 지금 작업공간을 그대로 둔다.")
        return self.create_workspace_view(name, chosen)

    def open_fcp_library_view(
        self, library: Path, *, originals_only: bool = False
    ) -> WorkspaceView:
        """FCP 라이브러리 열기 — 라이브러리 밖 관리 작업공간을 만들거나 이어 열고 미디어를 싣는다.

        라이브러리는 읽기만 한다. 작업공간은 같은 라이브러리에 대해 언제나 같은
        sibling 자리다(안정 경로). 조사 결과는 일반 SD/폴더와 같은 가져오기 패널로
        흐른다 — 확정·복사는 같은 A2 파이프라인이다. 조사가 실패해도 열기는 완결이다:
        실패 사유가 가져오기 패널에 정직하게 남는다(조용한 빈 성공이 아니다).
        """
        try:
            status, _created = self.raw.managed_fcp_workspace(library)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        root = Path(status.workspace_root)
        self._remember(root)
        resolved = library.expanduser().resolve()
        with self._selection_lock:
            self._fcp_library = resolved
        try:
            self._scan_fcp_view(
                root,
                resolved,
                raw_service.FCP_ROLE_ORIGINAL if originals_only else self._fcp_role,
                originals_only=originals_only,
            )
        except gr.Error as error:
            self._store_fcp_error(root, error)
        return self.workspace_view_values()

    def _store_fcp_error(self, root: Path, error: gr.Error) -> None:
        """FCP 조사 실패 기록 — 화면 오류 문장을 가져오기 패널 자리에 그대로 남긴다."""
        with self._selection_lock:
            self._ingest_proposal = None
            self._ingest_signature = None
            self._ingest_confirmation = None
            self._fcp_scan = None
            self._fcp_error = self._serve_error(error)
            self._fcp_signature = self._signature_for(root)

    def _current_fcp_library(self, signature: str | None) -> Path | None:
        """이 작업공간 서명에 묶인 FCP 라이브러리 — 없으면(다른 작업공간의 것이면) None."""
        with self._selection_lock:
            if self._fcp_library is not None and self._fcp_signature == signature:
                return self._fcp_library
            return None

    def _scan_fcp_view(
        self, root: Path, library: Path, role: str, *, originals_only: bool = False
    ) -> str:
        """FCP 라이브러리 조사 — 현재 작업공간 서명에 묶어 제안과 라이브러리 사실을 남긴다."""
        try:
            scan = self.raw.scan_fcp_library(
                root,
                library,
                self._inspector or raw_service.ffprobe_media_inspector,
                role,
                originals_only=originals_only,
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        self.raw.remember_fcp_library(root, library)
        signature = self._signature_for(root)
        with self._selection_lock:
            self._ingest_proposal = scan.proposal
            self._ingest_signature = signature
            self._ingest_confirmation = None
            self._fcp_library = library.expanduser().resolve()
            self._fcp_scan = scan
            self._fcp_signature = signature
            self._fcp_error = None
            self._fcp_role = role
        return self._ingest_state_markdown(root)

    def source_folder_chooser_view(self) -> tuple[str, str]:
        """SD/원본 폴더 선택 — 선택기로만 고른다. 취소면 지금 표시를 그대로 둔다."""
        chosen = self._pick_path(ui_chooser.FOLDER_KIND)
        if chosen is None:
            raise gr.Error("취소 — 원본 폴더 표시를 그대로 둔다.")
        try:
            listing = raw_service.browse_folder(chosen)
        except raw_service.BrowseNotAllowedError as error:
            raise gr.Error(str(error)) from error
        self._last_listing = listing
        return self._listing_markdown(listing), listing.path

    def final_video_chooser_view(self) -> str:
        """최종 영상 선택 — 선택기로만 고른다. 취소면 지금 표시를 그대로 둔다."""
        chosen = self._pick_path(ui_chooser.VIDEO_KIND)
        if chosen is None:
            raise gr.Error("취소 — 최종 영상 표시를 그대로 둔다.")
        return chosen

    def open_workspace_view(self, path_text: str) -> WorkspaceView:
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

    def create_workspace_view(self, name: str, path_text: str) -> WorkspaceView:
        """새 작업공간 만들기 이벤트 — 첫 생성(서명 None)에서도 같은 화면이 즉시 연결된다."""
        if not (name or "").strip():
            raise gr.Error("새 프로젝트 이름을 입력하세요.")
        if not (path_text or "").strip():
            raise gr.Error("작업공간으로 쓸 빈 폴더 경로를 선택기로 고르세요.")
        try:
            status = self.raw.create_workspace(Path(path_text), name)
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
        self._remember(Path(status.workspace_root))
        return self.workspace_view_values()

    def workspace_view_values(self) -> WorkspaceView:
        """작업공간 전환 뒤 화면이 받아야 하는 전체 보기(서명 포함).

        루트를 **한 번** 찍어 모든 조각에 통과시킨다 — 전환이 끼어들어도 이 보기가
        두 작업공간에 걸치지 않는다(코디네이터 재결). 마지막 세 값은 읽기전용 경로
        표시 칸이다(선택기로만 경로가 정해진다). 출처 표시는 이 작업공간이 FCP
        라이브러리에서 왔으면 그 경로를 보이고, 일반 전환이면 비운다 — 출처 선택은
        작업공간마다 새로 시작한다(낡은 출처가 다음 프로젝트에 남지 않는다).
        최종 영상 표시는 전환마다 비운다.
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
            *self._workbench_view_values(root),
            str(root) if root is not None else "",
            self._fcp_source_display(root),
            "",
        )

    def _fcp_source_display(self, root: Path | None) -> str:
        """읽기전용 출처 표시 — 이 작업공간이 FCP 라이브러리에서 왔으면 그 경로.

        조사 단추가 이 표시를 입력으로 읽으므로, 라이브러리를 열면 그 경로가
        보여야 역할을 바꿔 다시 조사할 수 있다. 일반 작업공간 전환이면 FCP 사실이
        이미 지워져 있어 비운다(낡은 출처가 남지 않는다).
        """
        if root is None:
            return ""
        library = self._current_fcp_library(self._signature_for(root))
        return str(library) if library is not None else ""

    def _workbench_view_values(self, root: Path | None) -> tuple[str, str, str]:
        """작업대 세 값(영상 껍데기·영상 본문·자막 본문) — 전환·새로고침 출력용.

        작업공간 전환마다 껍데기가 다시 그려져 영상이 능동적으로 언로드되고,
        본문 값의 epoch가 새로 파져 묵은 클라이언트 응답이 전부 버려진다.
        """
        shell, workbench = self.visual_workbench_render(root)
        return shell, workbench, self.caption_workbench_value(root)

    def _workbench_outputs(self) -> list[object]:
        """작업대 세 구성요소 — 만들어진 뒤에만 부른다(조립 순서 주의)."""
        assert (
            self._visual_player_block is not None
            and self._visual_workbench_block is not None
            and self._caption_workbench_block is not None
        )
        return [
            self._visual_player_block,
            self._visual_workbench_block,
            self._caption_workbench_block,
        ]

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
            and self._ws_path_box is not None
            and self._source_box is not None
            and self._caption_path_box is not None
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
            *self._workbench_outputs(),
            self._ws_path_box,
            self._source_box,
            self._caption_path_box,
        ]

    def cancel_view(self, signature: str | None, job_id: str | None) -> tuple[gr.Dropdown, str]:
        """취소 이벤트 — 안내 문장과 함께 진행 중 작업 목록도 다시 내린다."""
        message = self.cancel_selected(signature, job_id)
        return gr.Dropdown(choices=self.active_job_choices()), message

    # ── V2 영상 작업대 — 공개 질의만 조립, 편집은 화면이 본 리비전에 묶인다 ──

    def _visual_source_names(self, root: Path) -> dict[str, str]:
        """사람이 읽는 원본 이름 — 공개 목록(자산)에서만 온다(조사 없음)."""
        try:
            return {
                asset.asset_id: asset.source_name for asset in self.raw.list_source_assets(root)
            }
        except raw_service.RawWorkspaceError:
            return {}

    def _visual_plan_ranges(self, root: Path, base_plan_id: str) -> set[tuple[str, int, int]]:
        """기본 계획의 core 범위 — 재추출 판정(영상 갱신 필요 표시)의 재료."""
        try:
            plan = self.raw.segment_plan_revision(root, base_plan_id)
        except raw_service.RawWorkspaceError:
            return set()
        return {
            (entry.asset_id, entry.core_start_pts, entry.core_end_pts) for entry in plan.entries
        }

    def _visual_chronological(self, root: Path, base_plan_id: str | None) -> tuple[Any, Any]:
        """(리비전, 시간순 조각) — 전체 원본 레일·선호 선택 계산의 공개 읽기."""
        facade = self.raw.review_workspace_facade(root)
        loaded = facade.load_head(base_plan_id)
        return loaded.revision, facade.chronological(loaded.revision)

    def _visual_preferred_id(
        self,
        root: Path,
        *,
        playhead: Fraction | None,
        source_id: str | None,
        fallback_id: str | None,
    ) -> tuple[str | None, int | None]:
        """편집 뒤 유지할 선택 — 재생 머리가 속한 조각(시작<=t<끝), 없으면 옛 선택.

        전체 목록(페이지 경계를 넘는 이웃 포함)에서 고른다 — 질의 한 페이지만으로는
        왼쪽 이웃·전체 원본 레일을 알 수 없다. 리비전이 없으면 (None, None).
        """
        try:
            revision, ordered = self._visual_chronological(root, None)
        except (raw_service.RawWorkspaceError, raw_service.ReviewError):
            return None, None
        clocks = {clock.source_id: clock for clock in revision.clocks}
        if playhead is not None and source_id:
            clock = clocks.get(source_id)
            if clock is not None:
                for position, segment in enumerate(ordered):
                    if segment.source_id != source_id:
                        continue
                    start = Fraction(segment.start_pts - clock.origin_pts) * clock.time_base
                    end = Fraction(segment.end_pts - clock.origin_pts) * clock.time_base
                    if start <= playhead < end:
                        return segment.segment_id, position
        if fallback_id:
            for position, segment in enumerate(ordered):
                if segment.segment_id == fallback_id:
                    return fallback_id, position
        return None, None

    def _visual_mapped_refresh_id(
        self, root: Path, selected_id: str | None, old_plan: str | None
    ) -> str | None:
        """옛 계획의 선택 ID를 지금 머리의 같은 core 조각으로 — 재주조 방어(읽기 전용).

        재계획 뒤 같은 ID 문자열이 **다른 구간**에 재주조될 수 있다(실측: 대기 중
        재계획과 경합한 새로고침이 재주조된 앞조각을 골랐다). 검증 매핑의 불변
        (옛↔새 같은 core)으로 공개 검토 역사에서 옛 선택의 조각을 읽어 지금 머리의
        같은 core 조각을 찾는다. 요청이 같은 계획을 보고 있으면 그대로 돌려주고
        (같은 계획 안의 로컬 선택 보존), 옛 계획을 못 찾거나 core가 사라졌으면
        옛 ID를 믿지 않는다 — 서버 기본 선택이 낫다.
        """
        if not selected_id or not old_plan:
            return selected_id
        facade = self.raw.review_workspace_facade(root)
        try:
            current = facade.load_head().revision
            if current.base_plan_id == old_plan:
                return selected_id
            old = facade.load_head(old_plan).revision
        except (raw_service.RawWorkspaceError, raw_service.ReviewError):
            return None
        old_segment = next((seg for seg in old.segments if seg.segment_id == selected_id), None)
        if old_segment is None:
            return selected_id
        match = next(
            (
                seg
                for seg in current.segments
                if seg.source_id == old_segment.source_id
                and seg.start_pts == old_segment.start_pts
                and seg.end_pts == old_segment.end_pts
            ),
            None,
        )
        return match.segment_id if match is not None else None

    def visual_workbench_render(
        self,
        root: Path | None,
        *,
        page: int = 1,
        preferred_selected_id: str | None = None,
        playhead_sec: Fraction | None = None,
        source_id: str | None = None,
        selection_filter: str = "all",
    ) -> tuple[str, str]:
        """영상 작업대 (껍데기, 본문) — 질의·읽기만으로 만든다(미디어 생성 0).

        값은 전부 공개 서비스에서 온다: 조각·시계·미리보기는 `visual_review_query`,
        사람 이름은 자산 목록, 전체 원본 레일은 검토 파사드의 현재 머리(질의와
        **같은 리비전인지 비교**해 섞인 순간을 잡는다). 페이지가 닿는 **모든 원본**의
        미리보기 값(주소·상태·잰 원점·트랙·전체 레일)을 실는다 — 클라이언트가 카드를
        골라 원본을 바꿀 때도 서버 없이 재생기·렠·트랙을 갈아끼울 수 있게(F1b).
        선택·탐색 계산은 클라이언트 실행본이 이 값을 가지고 한다.
        """
        if root is None:
            note = "작업공간이 열려 있지 않다 — 1번 탭에서 만들거나 여세요."
            return (
                ui_facade.visual_player_shell_html(None, note),
                ui_facade.visual_workbench_html({"empty": note}),
            )
        quick = self._quick if self._quick.get("root") == root else {}
        if quick.get("stage") == "영상 준비" and not quick.get("asset_ids"):
            note = "선택한 영상을 준비하고 있습니다. 이전 영상의 결과는 표시하지 않습니다."
            return ui_facade.visual_player_shell_html(None, note), ui_facade.visual_workbench_html(
                {"empty": note}
            )
        if quick.get("asset_ids") and source_id is None:
            source_id = quick.get("view_source_id") or quick["asset_ids"][0]
            try:
                _head, _ordered = self._visual_chronological(root, None)
                preferred_selected_id = next(
                    (segment.segment_id for segment in _ordered if segment.source_id == source_id),
                    preferred_selected_id,
                )
            except (raw_service.RawWorkspaceError, raw_service.ReviewError):
                pass
        safe_page = max(1, page)
        preferred_id, preferred_pos = self._visual_preferred_id(
            root,
            playhead=playhead_sec,
            source_id=source_id,
            fallback_id=preferred_selected_id,
        )
        # 재생 머리 유지 재그림(편집 뒤)만 머리가 속한 조각의 페이지를 따라간다.
        # 그 외(명시적 새로고침·페이지 넘김)는 **요청 페이지가 이긴다** — 선택
        # 조각이 1번 페이지에 남아 있다고 페이지 넘김이 묶여 되돌아가지 않는다.
        query_page = safe_page
        if playhead_sec is not None and preferred_pos is not None:
            query_page = preferred_pos // raw_service.VISUAL_PAGE_SIZE + 1
        try:
            overview = self._visual_query(root, query_page, preferred_id, selection_filter)
        except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
            note = f"영상 검토를 열지 못했다 — {error}"
            return (
                ui_facade.visual_player_shell_html(None, note),
                ui_facade.visual_workbench_html({"empty": note}),
            )
        names = self._visual_source_names(root)
        plan_ranges = self._visual_plan_ranges(root, overview.base_plan_id)
        # 전체 원본 레일·분석 태그 — 질의와 같은 리비전인지 비교해 한 번만 공개 읽는다.
        revision: Any = None
        ordered: Any = None
        if overview.sources:
            try:
                revision, ordered = self._visual_chronological(root, overview.base_plan_id)
            except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
                note = f"검토 머리를 다시 읽지 못했다 — {error}"
                return (
                    ui_facade.visual_player_shell_html(None, note),
                    ui_facade.visual_workbench_html({"empty": note}),
                )
            if revision.revision_id != overview.revision_id:
                note = "검토 머리가 그 사이 바뀌었다 — 새로고침으로 다시 받으세요."
                return (
                    ui_facade.visual_player_shell_html(None, note),
                    ui_facade.visual_workbench_html({"empty": note}),
                )
        tags_by_segment: dict[str, tuple[str, ...]] = (
            {seg.segment_id: seg.visual_tags for seg in ordered} if ordered is not None else {}
        )
        # 원본별 미리보기 값 — 페이지(과 선택 조각)가 닿는 원본 전부를 실는다.
        sources_model: dict[str, dict[str, object]] = {}
        needs_plan = False
        if revision is not None and ordered is not None:
            segments_by_source: dict[str, list[Any]] = {}
            for segment in ordered:
                segments_by_source.setdefault(segment.source_id, []).append(segment)
            source_order = {segment.source_id: segment.source_order for segment in ordered}
            for preview in sorted(overview.sources, key=lambda item: source_order[item.source_id]):
                clock = revision.clock_for(preview.source_id)
                if clock is None:
                    raise gr.Error(f"원본 {preview.source_id!r}의 시계가 리비전에 없다")
                duration = Fraction(clock.end_pts - clock.origin_pts) * clock.time_base
                frame_sec = Fraction(clock.frame_ticks) * clock.time_base
                intervals: list[dict[str, object]] = []
                for segment in segments_by_source.get(preview.source_id, []):
                    start = Fraction(segment.start_pts - clock.origin_pts) * clock.time_base
                    end = Fraction(segment.end_pts - clock.origin_pts) * clock.time_base
                    needs = (
                        segment.source_id,
                        segment.start_pts,
                        segment.end_pts,
                    ) not in plan_ranges
                    needs_plan = needs_plan or needs
                    intervals.append(
                        {
                            "id": segment.segment_id,
                            "start": str(start),
                            "end": str(end),
                            "sel": segment.selection,
                            "rec": segment.recommendation,
                            "needsExtract": needs,
                        }
                    )
                sources_model[preview.source_id] = {
                    "src": (
                        f"/media/{preview.registered_file_id}"
                        if preview.registered_file_id and preview.state == "ready"
                        else None
                    ),
                    "state": preview.state,
                    "origin": preview.browser_origin_seconds or "0",
                    "duration": preview.duration_seconds,
                    "sourceId": preview.source_id,
                    "sourceName": names.get(preview.source_id, preview.source_id),
                    "audioOrdinal": preview.audio_ordinal,
                    "tracks": [
                        {
                            "ordinal": track.ordinal,
                            "label": (
                                f"트랙 {track.ordinal} · {track.codec_name}"
                                + (f" {track.sample_rate}Hz" if track.sample_rate else "")
                            ),
                        }
                        for track in preview.audio_tracks
                    ],
                    "error": preview.error,
                    "rail": {
                        "duration": str(duration),
                        "frameSec": str(frame_sec),
                        "intervals": intervals,
                    },
                }
        base_index = (overview.page - 1) * raw_service.VISUAL_PAGE_SIZE

        def _card_values(seg_card: Any, order: int) -> dict[str, object]:
            time_base = Fraction(seg_card.clock.time_base_num, seg_card.clock.time_base_den)
            origin = seg_card.clock.origin_pts
            start = Fraction(seg_card.start_pts - origin) * time_base
            end = Fraction(seg_card.end_pts - origin) * time_base
            return {
                "id": seg_card.segment_id,
                "sourceId": seg_card.source_id,
                "sourceName": names.get(seg_card.source_id, seg_card.source_id),
                "order": order,
                "start": str(start),
                "end": str(end),
                "dur": str(end - start),
                "sel": seg_card.selection,
                "rec": seg_card.recommendation,
                "speech": seg_card.speech_status,
                "tags": list(tags_by_segment.get(seg_card.segment_id, ())),
                "note": seg_card.notes,
                "thumb": {
                    "state": seg_card.thumb_state,
                    "src": (
                        f"/media/{seg_card.thumb_registered_file_id}"
                        if seg_card.thumb_registered_file_id
                        else None
                    ),
                },
                "neighbor": (
                    seg_card.right_neighbor.segment_id if seg_card.right_neighbor else None
                ),
                "needsExtract": (
                    seg_card.source_id,
                    seg_card.start_pts,
                    seg_card.end_pts,
                )
                not in plan_ranges,
            }

        cards = [
            _card_values(card, base_index + offset) for offset, card in enumerate(overview.segments)
        ]
        selected = overview.selected
        selected_card: dict[str, object] | None = None
        selected_order = 0
        if selected is not None and ordered is not None:
            selected_order = next(
                (
                    position
                    for position, seg in enumerate(ordered)
                    if seg.segment_id == selected.segment_id
                ),
                0,
            )
            selected_card = _card_values(selected, selected_order)
        task_views = self._task_views(root)
        for sid, source in sources_model.items():
            task = task_views.get(sid, {})
            if task.get("revision"):
                rows = self.raw.selection_suitability(root, task["revision"], (sid,)).get(sid, [])
                source["suggestions"] = [
                    {
                        "start": str(Fraction(row["start"]["num"], row["start"]["den"])),
                        "end": str(Fraction(row["end"]["num"], row["end"]["den"])),
                        "rec": row["recommendation"],
                        "reason": row["reason"],
                    }
                    for row in rows
                ]
            source["analysisState"] = task.get("stage", "분석 대기")
        for sid, source in sources_model.items():
            transcript = self.raw.source_transcript(root, sid)
            if transcript:
                source["transcriptRevision"] = transcript["revision"]
                source["crossSourceCandidates"] = self.raw.source_repeat_candidates(root, sid)
                source["subtitles"] = [
                    {
                        "id": u["utterance_id"],
                        "text": u["text"],
                        "start": str(Fraction(u["start"]["num"], u["start"]["den"])),
                        "end": str(Fraction(u["end"]["num"], u["end"]["den"])),
                    }
                    for u in transcript["utterances"]
                ]
                source["transcriptDirty"] = bool(
                    transcript["sequence"]
                    and quick.get("stage") in ("자막 변경 · 갱신 필요", "승인 대기")
                )
            refs = self.raw.source_media_references(root, sid)
            if refs:
                source["mediaStatus"] = refs["validation"]["reason"]
                source["originalMissing"] = refs["original"] is None
            source.setdefault("analysisState", "영상을 선택하면 Luna 분석을 시작합니다")
        media_cards = []
        for sid, source in sources_model.items():
            rail_data = source.get("rail")
            if not isinstance(rail_data, dict):
                continue
            representative = next((card for card in cards if card["sourceId"] == sid), None)
            intervals_for_source = rail_data["intervals"]
            if representative is None and intervals_for_source:
                first = intervals_for_source[0]
                representative = {
                    "id": first["id"],
                    "sourceId": sid,
                    "sourceName": source["sourceName"],
                    "start": first["start"],
                    "end": first["end"],
                    "sel": first["sel"],
                    "rec": "normal",
                    "speech": "unconfirmed_empty",
                    "thumb": {"state": "missing", "src": None},
                    "order": 0,
                    "needsExtract": False,
                }
            if representative is not None:
                media_cards.append(
                    {
                        **representative,
                        "media": True,
                        "start": "0",
                        "end": rail_data["duration"],
                        "dur": rail_data["duration"],
                    }
                )
        model: dict[str, object] = {
            "epoch": uuid.uuid4().hex,
            "token": self._visual_lifetime_token,
            "signature": self._signature_for(root),
            "basePlanId": overview.base_plan_id,
            "revisionId": overview.revision_id,
            "filter": selection_filter,
            "page": overview.page,
            "pageCount": overview.page_count,
            "total": overview.total_segments,
            "selectedId": selected.segment_id if selected else None,
            "selectedSourceId": selected.source_id if selected else None,
            "selectedCard": selected_card,
            "sources": sources_model,
            "selectionToken": quick.get("token", ""),
            "autoAnalyzeSelection": True,
            "cards": cards,
            "mediaCards": media_cards,
            "activeJobs": list(overview.active_preparation_jobs),
            "staleSources": [list(pair) for pair in overview.stale_sources],
            "needsPlan": needs_plan,
            "packageDir": self._latest_package_dir(root),
        }
        shell_src = None
        if selected is not None:
            src_value = sources_model.get(selected.source_id, {}).get("src")
            shell_src = src_value if isinstance(src_value, str) else None
        shell = ui_facade.visual_player_shell_html(
            shell_src,
            "선택 조각의 원본 전체 미리보기 — 시간 렠·재생 머리는 작업대가 채운다.",
        )
        return shell, ui_facade.visual_workbench_html(model)

    def _visual_query(
        self, root: Path, page: int, selected_segment_id: str | None, selection_filter: str = "all"
    ) -> Any:
        """질의 한 번 — 선택 조각이 리비전에 없으면(나누기 뒤 옛 ID) 선택 없이 다시."""
        try:
            return self.raw.visual_review_query(
                root,
                page=page,
                selected_segment_id=selected_segment_id,
                include_all_sources=True,
                selection_filter=selection_filter,
            )
        except raw_service.RawWorkspaceError:
            if selected_segment_id is None:
                raise
            return self.raw.visual_review_query(
                root, page=page, include_all_sources=True, selection_filter=selection_filter
            )

    # ── V2 서버 함수 — gradio 공개 경계(component_server)로만 간다 ──

    @staticmethod
    def _serve_error(error: object) -> str:
        if isinstance(error, gr.Error):
            message = getattr(error, "message", None)
            return str(message) if message else str(error)
        return str(error)

    @staticmethod
    def _serve_str(payload: Mapping[str, object], key: str) -> str:
        value = payload.get(key)
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _serve_int(payload: Mapping[str, object], key: str, default: int) -> int:
        value = payload.get(key)
        if isinstance(value, bool):
            raise gr.Error(f"{key}이 정수가 아니다: {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        return default

    @staticmethod
    def _serve_fraction(payload: Mapping[str, object], key: str) -> Fraction:
        """정확한 유리수 입력 — 문자열(`1001/30000`)·정수만, float는 십진 표기로 잡는다."""
        value = payload.get(key)
        if isinstance(value, bool) or value is None:
            raise gr.Error(f"{key}이 정확한 숫자가 아니다: {value!r}")
        if isinstance(value, int):
            return Fraction(value)
        if isinstance(value, float):
            return Fraction(str(value))
        if isinstance(value, str):
            try:
                return Fraction(value.strip())
            except (ValueError, ZeroDivisionError) as error:
                raise gr.Error(f"{key}이 유리수가 아니다: {value!r}") from error
        raise gr.Error(f"{key}이 정확한 숫자가 아니다: {value!r}")

    def _require_workbench_token(
        self, payload: Mapping[str, object], current: str | None, what: str
    ) -> None:
        """작업대 수명 토큰 검증 — 화면이 보낸 토큰이 지금 작업공간 것과 같아야 한다.

        서명은 결정적이라 A→B→A에서 같아지지만 토큰은 작업공간을 여는 순간마다
        새로 파지므로, 첫 A 시절의 편집·제안·준비 사건이 되돌아온 A에서 통과되지
        않는다(F1c). 리비전(CAS)·자막 머리 검사보다 **앞서** 판정한다 — 옛 사건에
        새 머리의 저장 토큰을 몰래 붙이는 길도 함께 닫힌다.
        """
        shown = self._serve_str(payload, "token")
        if not current or shown != current:
            raise gr.Error(
                f"{what} 수명 토큰이 늦었다 — 작업공간이 다시 열렸다. 화면을 새로 고침해"
                " 지금 보기를 다시 받은 뒤 시도하세요(머리가 같아도 옛 사건은 거부된다)."
            )

    def visual_workbench_action(self, payload: Mapping[str, object], kind: str) -> dict:
        """작업대 서버 함수의 본문 — 어떤 예외도 500이 아니라 값으로 돌아간다.

        모든 변경은 화면이 보낸 서명·수명 토큰·리비전에 묶인다(전역 최신 대체
        금지). 응답의 `echoEpoch`는 요청이 만들어진 화면의 epoch 그대로 — 클라이언트는
        지금 문서의 epoch와 다르면 적용하지 않는다(A→B→A 뒤 늦은 응답 버림).
        """
        epoch_value = payload.get("epoch")
        echo = epoch_value if isinstance(epoch_value, str) else ""

        def fail(error: object) -> dict[str, object]:
            return {"ok": False, "error": self._serve_error(error)}

        try:
            root = self._require_current_selection(self._serve_str(payload, "signature") or None)
        except gr.Error as error:
            return fail(error)
        try:
            if kind in (
                "prepare",
                "propose",
                "apply",
                "replan",
                "select-source",
                "source-caption",
                "relink-original",
            ):
                # 질의(refresh)는 열어 둔다 — 서버 재시작 뒤 화면이 스스로 회복하는 길.
                self._require_workbench_token(payload, self._visual_lifetime_token, "영상 작업대")
            if kind in ("source-caption", "relink-original"):
                source_id = self._serve_str(payload, "sourceId")
                if kind == "source-caption":
                    self.raw.edit_source_transcript(
                        root,
                        source_id,
                        self._serve_str(payload, "transcriptRevision"),
                        action=self._serve_str(payload, "action"),
                        utterance_id=self._serve_str(payload, "utteranceId"),
                        text=self._serve_str(payload, "text"),
                        start=self._serve_str(payload, "start") or None,
                        end=self._serve_str(payload, "end") or None,
                        split_at=self._serve_str(payload, "splitAt") or None,
                        right_text=self._serve_str(payload, "rightText") or None,
                    )
                    self._cancel_quick()
                    if self._quick:
                        self._quick_update(
                            self._quick["token"],
                            stage="자막 변경 · 갱신 필요",
                            token=uuid.uuid4().hex,
                            quote=None,
                            error=None,
                        )
                    self._prepare_library_quote(root)
                    message = (
                        "원본 자막을 저장했습니다. 재시도로 변경된 근거 구간만 갱신하세요. "
                        "수동 컷은 유지됩니다."
                    )
                else:
                    self.raw.relink_source_original(
                        root,
                        source_id,
                        Path(self._serve_str(payload, "path")).expanduser().resolve(),
                    )
                    message = "원본과 프록시의 대응을 확인하고 재연결했습니다."
                _shell, workbench = self.visual_workbench_render(root)
                return {
                    "ok": True,
                    "echoEpoch": echo,
                    "html": workbench,
                    "videoSrc": None,
                    "message": message,
                }
            if kind == "select-source":
                message, _shell, workbench = self.quick_source_view(
                    self._serve_str(payload, "signature"), self._serve_str(payload, "sourceId")
                )
                return {
                    "ok": True,
                    "echoEpoch": echo,
                    "html": workbench,
                    "videoSrc": None,
                    "message": message,
                }
            if kind == "refresh":
                page = self._serve_int(payload, "page", 1)
                preferred = self._serve_str(payload, "selectedId") or None
                # 대기 중 재계획과 경합한 새로고침은 옛 계획의 선택 ID를 실고 온다 —
                # 새 계획에서 같은 ID가 다른 구간에 재주조됐을 수 있으니 옛 계획의
                # core로 지금 머리의 의미 조각을 찾는다(읽기 전용, 페이지 계약 유지).
                preferred = self._visual_mapped_refresh_id(
                    root, preferred, self._serve_str(payload, "basePlanId") or None
                )
                if preferred is None:
                    preferred, _pos = self._visual_preferred_id(
                        root, playhead=None, source_id=None, fallback_id=None
                    )
                shell_unused, workbench = self.visual_workbench_render(
                    root,
                    page=page,
                    preferred_selected_id=preferred,
                    selection_filter=self._serve_str(payload, "filter") or "all",
                )
                return {
                    "ok": True,
                    "echoEpoch": echo,
                    "html": workbench,
                    "videoSrc": None,
                    "message": "",
                }
            if kind == "prepare":
                selections = self._visual_audio_selections(payload)
                page = self._serve_int(payload, "page", 1)
                handle = self.raw.start_visual_page_preparation(
                    root, page=page, audio_selections=selections or None
                )
                preferred = self._serve_str(payload, "selectedId") or None
                _shell, workbench = self.visual_workbench_render(
                    root,
                    page=page,
                    preferred_selected_id=preferred,
                    selection_filter=self._serve_str(payload, "filter") or "all",
                )
                return {
                    "ok": True,
                    "echoEpoch": echo,
                    "html": workbench,
                    "videoSrc": None,
                    "message": (
                        f"준비 작업 `{handle.job_id}` 제출 — 없는 항목만 만든다."
                        " 끝나면 새로고침으로 확인하세요."
                    ),
                }
            if kind == "propose":
                return self._visual_propose(root, payload, echo)
            if kind == "apply":
                return self._visual_apply(root, payload, echo)
            if kind == "replan":
                return self._visual_replan(root, payload, echo)
            return fail(f"모르는 작업대 요청이다: {kind!r}")
        except (gr.Error, RuntimeError, ValueError) as error:
            # RawWorkspaceError·ReviewError·Caption 계열 오류는 모두 RuntimeError
            # 집안이다 — 서버 함수는 500이 아니라 값으로 닫는다(gradio 전송 계약).
            return fail(error)

    def _visual_audio_selections(self, payload: Mapping[str, object]) -> dict[str, int | None]:
        """소리 선택 해석 — 서수는 정수, `None`은 명시적 침묵, 그 밖은 거부."""
        raw = payload.get("audioSelections")
        if not isinstance(raw, Mapping):
            return {}
        selections: dict[str, int | None] = {}
        for key, value in raw.items():
            source_id = str(key)
            if value is None:
                selections[source_id] = None
            elif isinstance(value, bool) or not isinstance(value, (int, str)):
                raise gr.Error(f"소리 서수가 정수가 아니다: {value!r}")
            elif isinstance(value, str):
                if not value.strip().lstrip("-").isdigit():
                    raise gr.Error(f"소리 서수가 정수가 아니다: {value!r}")
                selections[source_id] = int(value.strip())
            else:
                selections[source_id] = value
        return selections

    def _visual_propose(self, root: Path, payload: Mapping[str, object], echo: str) -> dict:
        """스냅 제안 — 저장보다 앞서고 아무 것도 쓰지 않는다(읽기 전용)."""
        revision_id = self._serve_str(payload, "revisionId")
        base_plan = self._serve_str(payload, "basePlanId")
        if not revision_id or not base_plan:
            raise gr.Error("화면 값이 부족하다 — 새로고침 뒤 다시 시도하세요.")
        op = self._serve_str(payload, "op")
        desired = self._serve_fraction(payload, "desiredSec")
        revision, _ordered = self._visual_chronological(root, base_plan)
        if op == "split":
            segment_id = self._serve_str(payload, "segmentId")
            segment = next((seg for seg in revision.segments if seg.segment_id == segment_id), None)
            if segment is None:
                raise gr.Error(f"조각 {segment_id!r}이(가) 리비전에 없다 — 새로고침하세요.")
            clock = revision.clock_for(segment.source_id)
            if clock is None:
                raise gr.Error(f"원본 {segment.source_id!r}의 시계가 없다")
            desired_pts = clock.origin_pts + desired / clock.time_base
            proposal = self.raw.visual_snap_proposal(
                root,
                base_plan_id=base_plan,
                expected_revision_id=revision_id,
                segment_id=segment_id,
                desired_pts=desired_pts,
            )
        elif op == "boundary":
            left_id = self._serve_str(payload, "leftId")
            right_id = self._serve_str(payload, "rightId")
            left = next((seg for seg in revision.segments if seg.segment_id == left_id), None)
            if left is None:
                raise gr.Error(f"조각 {left_id!r}이(가) 리비전에 없다 — 새로고침하세요.")
            clock = revision.clock_for(left.source_id)
            if clock is None:
                raise gr.Error(f"원본 {left.source_id!r}의 시계가 없다")
            desired_pts = clock.origin_pts + desired / clock.time_base
            proposal = self.raw.visual_boundary_proposal(
                root,
                base_plan_id=base_plan,
                expected_revision_id=revision_id,
                left_segment_id=left_id,
                right_segment_id=right_id,
                desired_pts=desired_pts,
            )
        else:
            raise gr.Error(f"모르는 제안 종류다: {op!r}")
        frame = Fraction(proposal.snap_pts - clock.origin_pts, clock.frame_ticks)
        return {
            "ok": True,
            "echoEpoch": echo,
            "valid": proposal.valid,
            "reason": proposal.reason,
            "snapSeconds": proposal.snap_seconds,
            "snapPts": proposal.snap_pts,
            "frame": frame.numerator if frame.denominator == 1 else None,
            "headRevisionId": proposal.head_revision_id,
        }

    def _visual_apply(self, root: Path, payload: Mapping[str, object], echo: str) -> dict:
        """실제 저장 — 화면이 본 리비전(CAS)에 묶이고, 성공은 새 리비전으로 그린다."""
        revision_id = self._serve_str(payload, "revisionId")
        base_plan = self._serve_str(payload, "basePlanId")
        if not revision_id or not base_plan:
            raise gr.Error("화면 값이 부족하다 — 새로고침 뒤 다시 시도하세요.")
        op = self._serve_str(payload, "op")
        page = self._serve_int(payload, "page", 1)
        playhead = None
        if self._serve_str(payload, "playheadSec"):
            playhead = self._serve_fraction(payload, "playheadSec")
        source_id = self._serve_str(payload, "sourceId") or None
        selected_id = self._serve_str(payload, "selectedId") or None
        if op == "selection":
            choice = self._serve_str(payload, "selection")
            if choice not in ("included", "excluded"):
                raise gr.Error(f"선택 표시가 알 수 없다: {choice!r}")
            edited = self.raw.visual_set_selection(
                root,
                base_plan_id=base_plan,
                expected_revision_id=revision_id,
                segment_id=self._serve_str(payload, "segmentId"),
                selection=choice,
            )
        elif op == "split":
            edited = self.raw.visual_split_segment(
                root,
                base_plan_id=base_plan,
                expected_revision_id=revision_id,
                segment_id=self._serve_str(payload, "segmentId"),
                split_pts=self._serve_int(payload, "splitPts", 0),
            )
        elif op == "boundary":
            edited = self.raw.visual_adjust_boundary(
                root,
                base_plan_id=base_plan,
                expected_revision_id=revision_id,
                left_segment_id=self._serve_str(payload, "leftId"),
                right_segment_id=self._serve_str(payload, "rightId"),
                boundary_pts=self._serve_int(payload, "boundaryPts", 0),
            )
        elif op == "merge":
            edited = self.raw.visual_merge_segments(
                root,
                base_plan_id=base_plan,
                expected_revision_id=revision_id,
                first_segment_id=self._serve_str(payload, "firstId"),
                second_segment_id=self._serve_str(payload, "secondId"),
            )
        elif op == "note":
            facade = self.raw.review_workspace_facade(root)
            edited = facade.apply_edit(
                base_plan,
                raw_service.review_note_edit(
                    revision_id,
                    self._serve_str(payload, "segmentId"),
                    self._serve_str(payload, "note"),
                ),
            )
        else:
            raise gr.Error(f"모르는 편집 종류다: {op!r}")
        self._refresh_review_panel_state(root)
        _shell, workbench = self.visual_workbench_render(
            root,
            page=page,
            preferred_selected_id=selected_id,
            playhead_sec=playhead,
            source_id=source_id,
            selection_filter=self._serve_str(payload, "filter") or "all",
        )
        return {
            "ok": True,
            "echoEpoch": echo,
            "html": workbench,
            "videoSrc": None,
            "message": f"적용했다 — 새 검토 리비전 `{edited.revision_id}`.",
        }

    def _visual_replan(self, root: Path, payload: Mapping[str, object], echo: str) -> dict:
        """계획 반영 + 재추출 — 화면이 본 리비전에서만, 새 package에 뽑는다."""
        revision_id = self._serve_str(payload, "revisionId")
        base_plan = self._serve_str(payload, "basePlanId")
        if not revision_id or not base_plan:
            raise gr.Error("화면 값이 부족하다 — 새로고침 뒤 다시 시도하세요.")
        facade = self.raw.review_workspace_facade(root)
        head = facade.load_head(base_plan).revision
        if head.revision_id != revision_id:
            raise gr.Error("화면이 본 리비전 뒤에 다른 편집이 먼저 갔다 — 새로고침 뒤 다시 하세요.")
        snapshot, mapping = facade.plan_from_review(base_plan)
        facade.adopt_plan_review(snapshot, mapping)
        self._refresh_review_panel_state(root)
        handle = self.raw.start_segment_extraction(root)
        selected_id = self._serve_str(payload, "selectedId") or None
        playhead = None
        if self._serve_str(payload, "playheadSec"):
            playhead = self._serve_fraction(payload, "playheadSec")
        # 계획 세대가 바뀌면 옛 선택 ID가 **다른 구간**에 재주조될 수 있다(실측:
        # 재계획 뒤 같은 ID 문자열이 다른 조각에 붙어 하이라이트가 재생 머리 밖
        # 구간을 가리켰다). 검증된 매핑(옛↔새 같은 core)으로 옛 선택의 의미 뒤
        # 조각을 찾고, 재생 머리·원본도 함께 실어 보존 규칙(머리가 속한 조각이
        # 이긴다)을 그대로 쓰게 한다.
        source_id = self._serve_str(payload, "sourceId") or None
        if selected_id:
            for pair in mapping.pairs:
                if pair.old_segment_id == selected_id:
                    selected_id = pair.new_segment_id
                    source_id = pair.source_id
                    break
        _shell, workbench = self.visual_workbench_render(
            root,
            page=1,
            preferred_selected_id=selected_id,
            playhead_sec=playhead,
            source_id=source_id,
            selection_filter=self._serve_str(payload, "filter") or "all",
        )
        return {
            "ok": True,
            "echoEpoch": echo,
            "html": workbench,
            "videoSrc": None,
            "message": (
                f"새 계획 리비전 `{mapping.new_plan_revision_id}` · 추출 작업"
                f" `{handle.job_id}` 시작 — 새 package에 뽑히고 옛 package는 불변이다."
            ),
        }

    def _refresh_review_panel_state(self, root: Path) -> None:
        """고급(기존) 검토 패널의 화면 상태를 새 머리로 맞춘다 — 최선 노력."""
        with contextlib.suppress(raw_service.RawWorkspaceError, raw_service.ReviewError, gr.Error):
            self._review_head_and_rows(root)

    def _visual_server_functions(self) -> list[Callable[[dict[str, object]], dict[str, object]]]:
        """영상 작업대의 서버 함수 — 클라이언트 `server.vl_*`로 직접 불린다."""

        def vl_refresh(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "refresh")

        def vl_prepare(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "prepare")

        def vl_propose(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "propose")

        def vl_apply(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "apply")

        def vl_replan(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "replan")

        def vl_select_source(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "select-source")

        def vl_source_caption(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "source-caption")

        def vl_relink_original(payload: dict[str, object]) -> dict[str, object]:
            return self.visual_workbench_action(payload, "relink-original")

        return [
            vl_refresh,
            vl_prepare,
            vl_propose,
            vl_apply,
            vl_replan,
            vl_select_source,
            vl_source_caption,
            vl_relink_original,
        ]

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
        fcp_scan = None
        fcp_error: str | None = None
        with self._selection_lock:
            if root is not None:
                signature = self._signature_for(root)
                if self._ingest_signature == signature:
                    proposal = self._ingest_proposal
                # FCP 사실은 조사 제안 서명과 별개로 이 작업공간에 묶여 있다 —
                # 조사가 실패해도(제안 서명 None) 라이브러리 사실·실패 사유가 보인다.
                if self._fcp_signature == signature:
                    fcp_scan = self._fcp_scan
                    fcp_error = self._fcp_error
        base = self._proposal_markdown(proposal)
        if fcp_scan is not None:
            base = self._fcp_scan_markdown(fcp_scan) + "\n\n" + base
        elif fcp_error is not None:
            base = f"**FCP 라이브러리 조사 실패** — {fcp_error}\n\n" + base
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

    def ingest_scan_view(
        self, signature: str | None, source_text: str, role: str | None = None
    ) -> str:
        """조사 이벤트 — 읽기만 한다. 제안은 현재 작업공간 서명에 묶어 보관한다.

        경로가 `.fcpbundle`이면 FCP 라이브러리 조사로 흐른다 — 원본·프록시 중복은
        논리 클립으로 묶이고 역할 선택이 제안을 정한다. 그 외 폴더는 일반 SD/폴더
        조사 그대로다.
        """
        root = self._require_current_selection(signature)
        text = (source_text or "").strip()
        if not text:
            # 출처 표시가 비어 있어도 이 작업공간이 FCP 라이브러리에서 왔으면 그것으로
            # 다시 조사한다 — 역할을 바꿔 누르는 길이 빈 입력 거절로 막히지 않는다.
            library = self._current_fcp_library(signature)
            if library is None:
                raise gr.Error("원본 폴더를 선택기로 고르세요 — 폴더 찾아보기 또는 장치 후보.")
            return self._scan_fcp_view(root, library, role or self._fcp_role)
        path = Path(text).expanduser()
        if raw_service.is_fcp_library(path):
            return self._scan_fcp_view(root, path, role or self._fcp_role)
        try:
            proposal = self.raw.scan_sources(
                root, path, self._inspector or raw_service.ffprobe_media_inspector
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

    def _fcp_scan_markdown(self, scan: raw_service.FcpLibraryScan) -> str:
        """FCP 라이브러리 조사 보기 — 논리 클립·역할·끊어진 링크를 숨기지 않고 적는다."""
        lines = [
            f"**FCP 라이브러리** `{scan.library_root}` — 라이브러리는 읽기만 하고,"
            " 라이브러리 **밖** 관리 작업공간에서 가져온다.",
            f"- 이벤트 {len(scan.events)}개({', '.join(scan.events) or '없음'}) ·"
            f" 논리 클립 {len(scan.clips)}개 · 시간순 작업 역할 **{scan.role}**",
            "- 같은 클립의 원본·프록시는 한 클립으로 묶였다 — 시간순 작업에는 선택"
            " 역할만 들어가고, 다른 역할 파일은 라이브러리에 그대로 있다(지우지 않는다).",
        ]
        fallback = [note for _file, note in scan.selected if note]
        if fallback:
            lines.append(f"- 역할 대체 {len(fallback)}건: {' · '.join(sorted(set(fallback))[:3])}")
        broken = scan.broken
        if broken:
            lines += ["", f"**끊어졌거나 가져올 수 없는 링크 {len(broken)}건**"]
            lines += [f"- `{item.linked_from}` — {item.reason}" for item in broken[:8]]
            if len(broken) > 8:
                lines.append(f"- … 외 {len(broken) - 8}건")
        return "\n".join(lines)

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
        with gr.Accordion("고급 · 클립 패키지", open=False):
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
            self._wire_review_button(
                "review-export",
                export_button,
                self.review_export_view,
                [export_selected_box, export_confirm_box],
                gated=True,
                outputs=[self._export_md, self._package_md],
            )
            self._wire_review_button(
                "review-export-open",
                finder_open_button,
                self.review_export_open_view,
                [],
                gated=True,
                outputs=[self._export_md],
            )
            self._wire_review_button(
                "review-export-copy",
                copy_path_button,
                self.review_export_copy_view,
                [],
                gated=True,
                outputs=[self._export_path_box],
            )
            self._wire_review_button(
                "review-cleanup",
                cleanup_button,
                self.review_cleanup_view,
                [cleanup_confirm_box],
                gated=True,
                outputs=[self._export_md],
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
            outcome = facade.export_confirmed(
                package_dir,
                base_plan_id=base_plan_id,
                selected_segment_ids=selected,
                context=context,
                expected_review_revision_id=displayed_revision,
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
            "- 제출 때의 리비전을 백엔드에 그대로 못 박았다(`expected_review_revision_id`)"
            " — 그 사이 머리가 움직이면 이 내보내기는 실패로 끝나고, 나중의 머리를"
            " 몰래 내보내지 않는다.",
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

    def _caption_player_for(
        self, root: Path, ws: Any, registration: Any
    ) -> tuple[raw_service.CaptionPreviewMedia, bool]:
        """선택 오디오 미리보기 확보 — (미리보기, 이번 호출에서 만들었는가).

        같은 작업공간에서 잡아 둔 미리보기가 아직 등록 행으로 살아 있으면 그대로
        돌려준다(ffmpeg·PCM 읽기 없음 — 표시 갱신이 미리보기를 다시 만들지 않는다).
        정리(purge) 뒤엔 서비스가 같은 키로 다시 만들고 지문을 다시 박는다.
        """
        signature = self._signature_for(root)
        cached = self._caption_preview
        if (
            cached is not None
            and self._caption_preview_signature == signature
            and registration.final_video_revision == cached.source_revision
            and registration.audio_ordinal == cached.audio_ordinal
        ):
            try:
                self.raw.resolve_media(root, cached.registration.file_id)
                return cached, False
            except raw_service.RawWorkspaceError:
                pass  # 정리 등으로 자리가 사라졌다 — 아래에서 다시 만든다
        try:
            preview = self.raw.caption_preview(root, registration)
        except (
            raw_service.RawWorkspaceError,
            raw_service.CaptionWorkspaceError,
            raw_service.FinalVideoError,
            RuntimeError,
        ) as error:
            raise gr.Error(
                f"선택 오디오 미리보기를 만들지 못했다 — {error}. 등록은 그대로다:"
                " 자막 생성·검토는 쓸 수 있고, 재생 듣기만 이번엔 없다."
            ) from error
        with self._selection_lock:
            self._caption_preview = preview
            self._caption_preview_signature = signature
        # 이 길(재시작 뒤 처음·미디어·트랙 변경·정리 뒤 재생성)에서는 재생기
        # 구성요소가 아직 이 미리보기를 그린 적이 없으므로 다시 그려야 한다.
        # ffmpeg은 서비스가 파일·기록으로 재사용 판정하므로 다시 돌지 않는다.
        return preview, True

    @staticmethod
    def _caption_provenance_line(
        registration: Any, preview: raw_service.CaptionPreviewMedia
    ) -> str:
        """재생기 출처 — 원본·선택 트랙·잰 원점·미리보기의 대응 관계를 숨기지 않는다."""
        origin = registration.origin
        return (
            f"출처 — 원본 `{registration.external_source}`(렌더 sha "
            f"{preview.source_revision[:12]}…) · 오디오 트랙 {preview.audio_ordinal}"
            "(선택 트랙만 담는다 — 브라우저는 트랙을 못 고른다) · 자막 시계 원점 "
            f"{ui_facade.format_seconds(origin)}초 · 미리보기 "
            f"{'코덱 복사' if preview.remuxed else '변환'}({preview.video_codec}/"
            f"{preview.audio_codec}, 잰 원점 {ui_facade.format_seconds(preview.audio_origin)}초"
            f" · 잰 길이 {ui_facade.format_seconds(preview.duration)}초 · 등록 ID "
            f"`{preview.registration.file_id}`) — 원본·확정 자막은 그대로다"
        )

    def _render_caption_player(
        self, registration: Any, preview: raw_service.CaptionPreviewMedia, wave: Any
    ) -> str:
        """재생기 HTML — 미리보기 영상·파형·재생 오버레이·출처를 안전하게 조립한다.

        V2부터 오버레이(`#caption-overlay`)와 토글 단추가 붙는다 — 오버레이 문구는
        작업대 실행본이 `timeupdate`에서 textContent로만 바꾼다(재생기 재렌더링 없음).
        """
        return ui_facade.caption_overlay_player_html(
            f"/media/{preview.registration.file_id}",
            waveform=ui_facade.waveform_svg([peak / wave.peak_scale for peak in wave.peaks]),
            provenance=self._caption_provenance_line(registration, preview),
        )

    @staticmethod
    def _caption_player_placeholder(note: str) -> str:
        safe = html.escape(note)
        return (
            '<div id="caption-player" class="vlogsrt-caption-player">\n'
            f'  <p style="color:#888;margin:4px 0;">{safe}</p>\n'
            "</div>"
        )

    def caption_register_view(
        self, signature: str | None, path_text: str, source_id: str, ordinal_text: str
    ) -> tuple[str, str]:
        """최종 영상 등록 — 검증 사본·오디오 선택과 함께 선택 트랙 미리보기를 만든다."""
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
        ws.select_registration(registration)
        identity = self._caption_identity
        if identity is None or (identity.get("revision"), identity.get("audio_ordinal")) != (
            registration.final_video_revision,
            registration.audio_ordinal,
        ):
            self._caption_identity = None
            self._caption_signature = None
            self._caption_head_sequence = None
            self._caption_cue_ids = ()
            self._caption_cue_rows = ()
            self._caption_cue_clock = {}
            self._caption_unplaced = {}
            self._caption_lifetime_token = uuid.uuid4().hex
        note = {
            "ready": "등록 완료 — 이 최종 영상을 기준으로 자막을 생성합니다.",
            "no_audio": "**오디오가 없다** — 무발화가 아니라 별개 상태다. 실행·추정은 닫혀 있다.",
            "audio_selection_required": (
                f"**오디오 트랙이 {registration.audio_stream_count}개다** — 오디오 트랙"
                " 칸에 번호(0, 1, …)를 골라 다시 등록하세요."
            ),
        }.get(registration.status, f"상태 `{registration.status}`")
        markdown = (
            f"**최종 영상 등록** — 리비전 `{registration.final_video_revision}` · 상태"
            f" `{registration.status}`\n\n{note}"
        )
        if not registration.ready:
            # 오디오가 없거나 선택이 확정되지 않았다 — 미리보기도 정직하게 없다.
            with self._selection_lock:
                self._caption_preview = None
                self._caption_preview_signature = None
            return markdown, self._caption_player_placeholder(
                "오디오 선택이 확정되지 않아 재생할 트랙이 없다 — 트랙을 골라 다시 등록하세요."
            )
        preview, _built_now = self._caption_player_for(root, ws, registration)
        wave = ws.waveform(registration, bin_seconds=0.05)
        return markdown, self._render_caption_player(registration, preview, wave)

    def caption_estimate_view(self, signature: str | None, run_id: str) -> str:
        """실행 추정 — 모델·수명주기·실측 청크·경계·비용을 백엔드가 잰 값 그대로."""
        root = self._require_current_selection(signature)
        run = (run_id or "").strip() or "run-1"
        ws = self._caption_ws(root)
        registration = ws.active_registration()
        if registration is None or not registration.ready:
            raise gr.Error("준비된 등록이 없다 — 먼저 최종 영상 등록(오디오 선택 확정)을 끝내세요.")
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
        self._caption_cue_rows = ()
        self._caption_cue_clock = {}
        self._caption_warnings_open = 0
        self._caption_unplaced_open = 0
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

    def caption_review_view(
        self, signature: str | None, run_id: str
    ) -> tuple[str, str, object, gr.Dropdown]:
        """검토 묶음 — 큐 편집표(HTML)·요약·재생기·선택 목록(로컬만, 원응답 없음).

        큐 표와 파형은 `gr.Markdown`이 지워버리므로 안전한 HTML 구성요소로 나간다.
        재생기 구성요소는 미리보기가 그대로면 **갱신하지 않는다**(`gr.update()` 무동작)
        — 재생 머리가 살아 있고 ffmpeg도 다시 돌지 않는다. 큐 단추의 시각은 서버가
        잰 원점 매핑(미리보기 원점 + 큐 시각) 그대로다.
        """
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
        except (raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError) as error:
            raise gr.Error(str(error)) from error
        # 미리보기 확보 — 같은 서버에서 잡아 둔 것이 살아 있으면 재생기는 무동작이다.
        preview, render_player = self._caption_player_for(root, ws, registration)
        player_update: object = gr.update()
        if render_player:
            wave = ws.waveform(registration, bin_seconds=0.05)
            player_update = self._render_caption_player(registration, preview, wave)
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
                text=row.cue.text,
                basis=row.cue.text_basis,
                chars_per_second=row.chars_per_second,
            )
            for row in state.rows
        ]
        self._caption_cue_ids = tuple(row.cue_id for row in rows)
        self._caption_cue_rows = tuple(rows)
        self._caption_cue_clock = {
            row.cue.cue_id: (row.cue.start, row.cue.end) for row in state.rows
        }
        self._caption_unplaced = {view.item.index: view.item.text for view in state.unplaced}
        self._caption_warnings_open = len(state.warnings)
        self._caption_unplaced_open = sum(1 for view in state.unplaced if view.resolution is None)
        # 큐 단추 시각 — 서버가 잰 값 그대로(클라이언트가 계산하지 않는다). 큐 시각은
        # 최종 영상 시계(등록 원점 포함)이므로, 미리보기 원점과 등록 원점의 **차이만**
        # 보정해 미리보기 시계로 옮긴다(코덱 복사면 차이가 0이어야 한다).
        seek_by_cue: dict[str, tuple[str, str]] | None = None
        if preview is not None and registration.origin is not None:
            shift = preview.audio_origin - registration.origin
            seek_by_cue = {
                row.cue.cue_id: (
                    f"{float(shift + row.cue.start):.6f}",
                    f"{float(shift + row.cue.end):.6f}",
                )
                for row in state.rows
            }
        table_html = ui_facade.cue_table_html(rows, seek_by_cue)
        lines = [
            f"**자막 검토** — 실행 `{run}` · 큐 {len(rows)}개 · 검토 머리"
            f" {self._caption_head_sequence}",
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
        return (
            "\n".join(lines),
            table_html,
            player_update,
            gr.Dropdown(choices=ui_facade.caption_cue_choices(rows), value=None),
        )

    def caption_cue_pick_view(
        self, signature: str | None, cue_id: str | None
    ) -> tuple[object, object, object, object]:
        """큐 선택 → 편집 칸 채우기 — 화면이 본 큐 행의 값을 그대로 옮긴다(호출·미디어 0회).

        시각은 사람이 준 편집 단위(ms 정수)로 보인다 — 백엔드의 정확한 유리수를
        1000배해 자른 값이고, 저장은 여전히 `시각 조정`의 ms 검증을 지난다.
        **빈 선택은 무동작이다**: 검토 새로고침이 드롭다운을 None으로 되돌릴 때마다
        `.change`가 빈 값으로 오는데, 그때 편집 칸과 재생기를 그대로 두고 조용히
        돌아간다(사용자가 고른 선택이 아니므로 오류도 아니다). 빈이 아닌 알 수
        없는 아이디는 여전히 정직하게 거부한다.
        """
        self._require_current_selection(signature)
        target = (cue_id or "").strip().strip("`")
        if not target:
            return gr.update(), gr.update(), gr.update(), gr.update()
        if not self._caption_cue_rows:
            raise gr.Error("큐 목록이 없다 — 먼저 **검토 새로고침**으로 큐를 보세요.")
        row = next((item for item in self._caption_cue_rows if item.cue_id == target), None)
        clock = self._caption_cue_clock.get(target)
        if row is None or clock is None:
            raise gr.Error(
                f"검토 표에 큐 `{target}`이(가) 없다 — 새로고침 뒤 목록에서 다시 고르세요."
            )
        start_ms = str(int(clock[0] * 1000))
        end_ms = str(int(clock[1] * 1000))
        return target, row.text, start_ms, end_ms

    # ── V2 자막 작업대 — 큐 목록·클라이언트 편집기(명시적 저장·확정만 서버) ──

    def _caption_reload_bundle(self, root: Path) -> None:
        """검토 머리를 다시 읽어 화면 상태를 갱신한다 — 작업대 저장 뒤 같은 눈으로.

        `caption_review_view`가 그리는 것과 같은 상태(머리 번호·큐 행·시계·미대응·
        경고)를 서버에 다시 묻는다 — 재생기 구성요소는 건드리지 않는다.
        """
        identity = self._require_caption_identity(root)
        ws = self._caption_ws(root)
        run_identity = self._caption_run_identity(identity)
        bundle = ws.review_bundle(run_identity)
        state = bundle.state
        self._caption_head_sequence = state.head_revision if state.head_revision is not None else 0
        rows = [
            ui_facade.CueRowView(
                cue_id=row.cue.cue_id,
                start_seconds=ui_facade.format_seconds(row.cue.start),
                end_seconds=ui_facade.format_seconds(row.cue.end),
                whisper_text=row.cue.whisper_text,
                auto_text=row.cue.auto_text,
                text=row.cue.text,
                basis=row.cue.text_basis,
                chars_per_second=row.chars_per_second,
            )
            for row in state.rows
        ]
        self._caption_cue_ids = tuple(row.cue_id for row in rows)
        self._caption_cue_rows = tuple(rows)
        self._caption_cue_clock = {
            row.cue.cue_id: (row.cue.start, row.cue.end) for row in state.rows
        }
        self._caption_unplaced = {view.item.index: view.item.text for view in state.unplaced}
        self._caption_warnings_open = len(state.warnings)
        self._caption_unplaced_open = sum(1 for view in state.unplaced if view.resolution is None)

    def caption_workbench_value(self, root: Path | None) -> str:
        """자막 작업대 값(본문+상세) — 서버가 가진 검토 상태에서 조립(생성 0).

        큐 행의 이동·오버레이 시각은 `caption_review_view`와 같은 잰 매핑(미리보기
        원점 − 등록 원점 보정) 그대로다 — 클라이언트는 이 값만 읽는다.
        """
        note = "최종 영상을 등록하고 실행·검토를 거치면 큐 목록이 여기 나타난다."
        if root is None or self._caption_identity is None:
            return ui_facade.caption_workbench_html(
                {"empty": note}
            ) + ui_facade.caption_workbench_details_html({"empty": ""})
        signature = self._signature_for(root)
        if self._caption_signature != signature:
            return ui_facade.caption_workbench_html(
                {"empty": note}
            ) + ui_facade.caption_workbench_details_html({"empty": ""})
        seek_by_cue = self._caption_seek_mapping(root)
        cues = []
        details = []
        for row in self._caption_cue_rows:
            clock = self._caption_cue_clock.get(row.cue_id)
            seek_pair = seek_by_cue.get(row.cue_id) if seek_by_cue else None
            # ms 환산은 정확히 — 이진 부동 잔여를 int()로 잘라 1200→1199로
            # 흐르는 왕복 드리프트를 막는다(A5 실측).
            start_ms = str(ui_facade.fraction_ms(clock[0])) if clock else ""
            end_ms = str(ui_facade.fraction_ms(clock[1])) if clock else ""
            cues.append(
                {
                    "id": row.cue_id,
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "text": row.text,
                    "basis": row.basis,
                    "seek": seek_pair[0] if seek_pair else "",
                    "endSeek": seek_pair[1] if seek_pair else "",
                }
            )
            details.append(
                {
                    "id": row.cue_id,
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "whisper": row.whisper_text,
                    "auto": row.auto_text,
                    "now": row.text,
                    "basis": row.basis,
                }
            )
        open_items: list[str] = []
        if self._caption_warnings_open:
            open_items.append(f"열린 경고 {self._caption_warnings_open}개")
        if self._caption_unplaced_open:
            open_items.append(f"미처리 미대응 {self._caption_unplaced_open}개")
        identity = self._caption_identity or {}
        preview_line = ""
        preview = self._caption_preview
        if preview is not None:
            preview_line = (
                f"실행 {identity.get('run_id')} · 검토 머리 {self._caption_head_sequence}"
                f" · 미리보기 `{preview.registration.file_id}`"
                f"(선택 트랙 {preview.audio_ordinal}, 잰 원점"
                f" {ui_facade.format_seconds(preview.audio_origin)}초)"
            )
        model: dict[str, object] = {
            "epoch": uuid.uuid4().hex,
            "token": self._caption_lifetime_token,
            "signature": signature,
            "runId": str(identity.get("run_id") or "run-1"),
            "head": self._caption_head_sequence,
            "cues": cues,
            "canConfirm": bool(cues) and not open_items,
            "blockReason": " · ".join(open_items),
            "provenance": preview_line,
            "details": details,
        }
        return ui_facade.caption_workbench_html(model) + ui_facade.caption_workbench_details_html(
            model
        )

    def _caption_seek_mapping(self, root: Path) -> dict[str, tuple[str, str]] | None:
        """큐→미리보기 이동 매핑 — 등록 원점과 미리보기 원점의 차이만 보정한다."""
        preview = self._caption_preview
        if preview is None:
            return None
        try:
            ws = self._caption_ws(root)
            registration = self._caption_registration(ws, root)
        except (gr.Error, raw_service.RawWorkspaceError, raw_service.CaptionWorkspaceError):
            return None
        if registration.origin is None:
            return None
        shift = preview.audio_origin - registration.origin
        return {
            cue_id: (
                f"{float(shift + clock[0]):.6f}",
                f"{float(shift + clock[1]):.6f}",
            )
            for cue_id, clock in self._caption_cue_clock.items()
        }

    def caption_workbench_action(self, payload: Mapping[str, object], kind: str) -> dict:
        """자막 작업대 서버 함수 본문 — 저장·확정만 서버에 가고 나머지는 클라이언트다.

        저장은 화면이 보낸 수명 토큰과 검토 머리 번호에 묶인다(W2 갱신 손실 방지,
        A→B→A 옛 사건 거부). 응답의 `html`은 작업대 본문+상세 값 — `props.value`로
        갈아끼워도 재생기는 다시 그려지지 않으므로 재생이 이어진다.
        """
        epoch_value = payload.get("epoch")
        echo = epoch_value if isinstance(epoch_value, str) else ""

        def fail(error: object) -> dict[str, object]:
            return {"ok": False, "error": self._serve_error(error)}

        try:
            root = self._require_current_selection(self._serve_str(payload, "signature") or None)
        except gr.Error as error:
            return fail(error)
        try:
            if kind in ("save", "confirm"):
                self._require_workbench_token(payload, self._caption_lifetime_token, "자막 작업대")
            if kind == "save":
                return self._caption_wb_save(root, payload, echo)
            if kind == "confirm":
                return self._caption_wb_confirm(root, payload, echo)
            return fail(f"모르는 작업대 요청이다: {kind!r}")
        except (gr.Error, RuntimeError, ValueError) as error:
            # 수동 편집 오류(ManualEditError 등)까지 값으로 닫는다 — 500 금지.
            return fail(error)

    def _caption_wb_save(self, root: Path, payload: Mapping[str, object], echo: str) -> dict:
        identity = self._require_caption_identity(root)
        ws = self._caption_ws(root)
        run_identity = self._caption_run_identity(identity)
        expected = self._serve_int(payload, "head", -1)
        shown = self._require_caption_head()
        if expected != shown:
            raise gr.Error(
                f"편집이 늦었다 — 화면이 본 머리({expected})가 서버 머리({shown})와 다르다."
                " 검토 새로고침 뒤 다시 저장하세요."
            )
        cue_id = self._serve_str(payload, "cueId").strip("`")
        if not cue_id:
            raise gr.Error("큐를 먼저 목록에서 고르세요.")
        op = self._serve_str(payload, "op")
        text = self._serve_str(payload, "text").strip()
        if op == "text":
            if not text:
                raise gr.Error("글자를 적으세요 — 지울 수 없다(수동 확정은 확정 단계다).")
            ws.edit_cue_text(run_identity, cue_id, text, expected_head=shown)
        elif op == "time":
            start_ms = self._serve_int(payload, "startMs", -1)
            end_ms = self._serve_int(payload, "endMs", -1)
            ws.edit_cue_time(
                run_identity,
                cue_id,
                start=raw_service.caption_ms(start_ms),
                end=raw_service.caption_ms(end_ms),
                expected_head=shown,
            )
        else:
            raise gr.Error(f"모르는 저장 종류다: {op!r}")
        self._caption_reload_bundle(root)
        return {
            "ok": True,
            "echoEpoch": echo,
            "html": self.caption_workbench_value(root),
            "videoSrc": None,
            "message": f"저장했다 — 큐 `{cue_id}`. 오버레이·목록이 갱신되고 재생은 이어진다.",
        }

    def _caption_wb_confirm(self, root: Path, payload: Mapping[str, object], echo: str) -> dict:
        """확정 게시 — 화면이 보낸 머리 번호에 묶인다(늦은 확정 거부, A5 실측).

        확정은 게시다 — 저장보다 더 엄격해야 한다: 화면이 본 머리가 서버 머리와
        다르면(그 사이 저장이 하나라도 앞서갔으면) **거부**하고 검토 새로고침을
        요청한다. 묵은 머리 번호로 최신 머리를 확정 게시하는 길은 없다.
        """
        identity = self._require_caption_identity(root)
        expected = self._serve_int(payload, "head", -1)
        shown = self._require_caption_head()
        if expected != shown:
            raise gr.Error(
                f"확정이 늦었다 — 화면이 본 머리({expected})가 서버 머리({shown})와"
                " 다르다. 검토 새로고침 뒤 다시 확정하세요."
            )
        # 확정 게시 — 작업대 흐름은 사람 문장 + 구조화된 내려받기(등록 미디어 ID
        # 로만)를 받는다. 원문 마크다운(지문·해시 포함)은 기존 고급 패널 흐름이
        # 그대로 쓴다(_caption_publish 공유, 이중 게시 없음).
        _legacy_note, links = self._caption_publish(
            self._serve_str(payload, "signature") or None,
            str(identity.get("run_id") or "run-1"),
            True,
        )
        self._caption_reload_bundle(root)
        return {
            "ok": True,
            "echoEpoch": echo,
            "html": self.caption_workbench_value(root),
            "videoSrc": None,
            "message": f"확정 게시 완료 — 검토 머리 {expected}. 내려받기는 아래 링크로.",
            "downloads": [{"label": label, "href": href} for label, href in links],
        }

    def _caption_server_functions(self) -> list[Callable[[dict[str, object]], dict[str, object]]]:
        """자막 작업대의 서버 함수 — 클라이언트 `server.cap_*`로 직접 불린다."""

        def cap_save(payload: dict[str, object]) -> dict[str, object]:
            return self.caption_workbench_action(payload, "save")

        def cap_confirm(payload: dict[str, object]) -> dict[str, object]:
            return self.caption_workbench_action(payload, "confirm")

        return [cap_save, cap_confirm]

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
    ) -> tuple[str, str, object, gr.Dropdown]:
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
        note, _links = self._caption_publish(signature, run_id, confirm)
        return note

    def _caption_publish(
        self, signature: str | None, run_id: str, confirm: bool
    ) -> tuple[str, list[tuple[str, str]]]:
        """확정 게시 공용 본문 — (원문 마크다운, 등록 미디어 링크)를 돌려준다.

        게시는 한 번만 일어난다: 고급 패널(caption_confirm_view)과 작업대
        서버 함수(_caption_wb_confirm)가 이 본문을 공유한다. 링크는 등록된
        미디어 ID(`/media/<id>`)로만 — 임의 경로·사용자 글자는 안 간다.
        """
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
        return "\n".join(lines), links

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

    def _caption_context_tail(self) -> tuple[str, int | None]:
        """자막 문맥 꼬리 — 지금 수명 토큰과 화면이 볼 머리(배선 출력용)."""
        return self._caption_lifetime_token, self._caption_head_sequence

    def _on_caption_register(
        self, signature: str | None, path_text: str, source_id: str, ordinal: str
    ) -> tuple[str, str, str, str, int | None]:
        """최종 영상 등록의 실제 배선 — 등록 뒤 화면 문맥(토큰·머리)을 내린다."""
        markdown, player = self.caption_register_view(signature, path_text, source_id, ordinal)
        return (
            markdown,
            player,
            self.caption_workbench_value(self._snapshot_root()),
            *self._caption_context_tail(),
        )

    def _on_caption_refresh(
        self, signature: str | None, run_id: str
    ) -> tuple[str, str, object, gr.Dropdown, str, str, int | None]:
        """검토 새로고침의 실제 배선 — 새 머리·토큰을 클라이언트 문맥으로 내린다."""
        markdown, table, player, cue_dd = self.caption_review_view(signature, run_id)
        return (
            markdown,
            table,
            player,
            cue_dd,
            self.caption_workbench_value(self._snapshot_root()),
            *self._caption_context_tail(),
        )

    def _on_caption_edit(
        self,
        nonce: str | None,
        shown_head: object,
        signature: str | None,
        operation: str,
        cue_id: str,
        text: str,
        start_ms: str,
        end_ms: str,
        second_id: str,
        index_text: str,
        warning_key: str,
    ) -> tuple[str, str, object, gr.Dropdown, str, str, int | None]:
        """수동 편집의 실제 배선 — 문맥 검증이 먼저다(뷰 함수 서명은 그대로).

        옛 토큰(작업공간 전환·재시작)·옛 머리(다른 클라이언트 편집) 사건은
        `caption_edit_view`에 닿기 전에 거부된다 — 어떤 큐·머리도 바뀌지 않는다.
        """
        self._require_caption_context(nonce, shown_head)
        markdown, table, player, cue_dd = self.caption_edit_view(
            signature, operation, cue_id, text, start_ms, end_ms, second_id, index_text, warning_key
        )
        return (
            markdown,
            table,
            player,
            cue_dd,
            self.caption_workbench_value(self._snapshot_root()),
            *self._caption_context_tail(),
        )

    def _on_caption_confirm(
        self,
        nonce: str | None,
        shown_head: object,
        signature: str | None,
        run_id: str,
        confirm: bool,
    ) -> tuple[str, str, str, int | None]:
        """확정 게시의 실제 배선 — 문맥 검증이 먼저다(옛 머리 확정 길 차단)."""
        self._require_caption_context(nonce, shown_head)
        markdown = self.caption_confirm_view(signature, run_id, confirm)
        return (
            markdown,
            self.caption_workbench_value(self._snapshot_root()),
            *self._caption_context_tail(),
        )

    @staticmethod
    def caption_public_status(markdown: str) -> str:
        """Keep cost, progress, errors and download links at the execution controls."""
        import re

        lines = [line for line in markdown.splitlines() if line.strip()]
        shown = [
            line
            for index, line in enumerate(lines)
            if index == 0
            or any(
                word in line
                for word in ("비용", "청크", "실패", "경고", "완료", "SRT", "VTT", "승인")
            )
        ]
        return re.sub(r"`[^`]*`", "", "\n\n".join(shown))

    def _caption_tab(self) -> None:
        with gr.Tab("자막", visible=False):
            # ── 접힌 세팅 — 설정 안내·등록·실행(기존 흐름)은 무대 위 한 줄로 접는다.
            # 1440x900에서 재생기·편집기·주 저장/확정 단추가 문서 스크롤 없이
            # 먼저 들어온다(ROOT 실측: 재생기 top 536 · 저장 1287 → 무대 재배치).
            with gr.Accordion("최종 영상 선택 · 자막 생성", open=True):
                with gr.Accordion("비용 · 모델 설정", open=False):
                    self._settings_md = gr.Markdown(self.settings_markdown())
                gr.Markdown(
                    "> **최종 영상 자막** — 등록(검증 사본·오디오 트랙 선택) → 추정"
                    "(모델·수명주기·실측 청크) → 확인란 승인 → 실행 작업 → 검토(큐 행·"
                    "파형·미대응) → 수동 편집 → 확정 게시. 실제 호출은 승인된 실행에서만"
                    " 열리고, 키는 서버를 띄운 환경에서만 온다(§14.3)."
                )
                with gr.Row():
                    self._caption_path_box = gr.Textbox(
                        label="최종 영상 절대 경로 — 선택기 표시",
                        placeholder="최종 영상 고르기(선택기)로 고르세요",
                        scale=4,
                        interactive=False,
                    )
                    caption_pick_button = gr.Button("최종 영상 고르기(선택기)", scale=2)
                with gr.Row():
                    caption_source_box = gr.Textbox(
                        label="출처 ID(선택)", placeholder="final_1", scale=2, visible=False
                    )
                    caption_ordinal_dd = gr.Dropdown(
                        choices=["자동(첫 트랙)", "0", "1", "2"],
                        value="자동(첫 트랙)",
                        label="오디오 트랙(여럿일 때 번호로)",
                        interactive=True,
                        scale=2,
                    )
                    caption_register_button = gr.Button(
                        "최종 영상 등록", variant="primary", scale=2
                    )
                caption_public_status = gr.Markdown(
                    "최종 영상을 등록하면 비용과 진행 상태가 표시됩니다."
                )
                with gr.Row():
                    caption_run_box = gr.Textbox(
                        label="실행 ID", value="run-1", interactive=False, scale=2, visible=False
                    )
                    caption_estimate_button = gr.Button("실행 추정(로컬)", scale=2)
                    caption_confirm_box = gr.Checkbox(
                        label="위 추정 지문 그대로 자막 생성을 승인한다",
                        interactive=True,
                        scale=3,
                    )
                    caption_run_button = gr.Button("자막 생성 실행", variant="primary", scale=2)
            # ── 자막 무대 — 왼쪽 재생기(영상·파형·이동) · 오른쪽 큐 목록+편집기.
            # 두 구성요소가 같은 `#caption-stage` 격자에 들어가고, 주 저장·확정
            # 단추는 오른쪽 칸 편집기 안에 있어 900px 안에 도달한다.
            with gr.Row(elem_id="caption-stage"):
                self._caption_player_html = gr.HTML(
                    self._caption_player_placeholder(
                        "최종 영상을 등록하면 선택한 오디오 트랙의 미리보기가 여기서 재생된다"
                        " — 큐 단추가 이 시계를 짚는다."
                    ),
                    elem_id="caption-player-block",
                    elem_classes=["vl-stage-cell"],
                )
                self._caption_workbench_block = gr.HTML(
                    self.caption_workbench_value(self._snapshot_root()),
                    elem_id="caption-workbench-block",
                    elem_classes=["vl-stage-cell"],
                    js_on_load=ui_facade.CAPTION_BOOTSTRAP_JS,
                    server_functions=self._caption_server_functions(),
                )
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
            with gr.Accordion("고급 — 수동 편집 칸·전체 큐 표(기존)", open=False):
                gr.Markdown(
                    "> 수동 편집 — 시각은 사람이 준 값만 들어간다(추측 배분 없음)."
                    " 큐 표에서 큐를 고르면 편집 칸이 그 행의 값으로 채워진다."
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
                with gr.Row():
                    self._caption_cue_dd = gr.Dropdown(
                        choices=[],
                        label="큐 선택 — 편집 칸 채우기·이동",
                        info="고르면 그 큐의 지금 글자·시각으로 편집 칸을 채우고 재생기를"
                        " 큐 시작으로 옮긴다(서버 미디어 재생성 없음).",
                        interactive=True,
                        scale=6,
                    )
                self._caption_table_html = gr.HTML(
                    '<p style="color:#888">큐 편집표는 검토 새로고침 뒤 여기 나타난다 —'
                    " 지금 글자 칸과 큐 이동·반복 단추까지.</p>",
                    elem_id="caption-cue-table",
                )
            # 등록 기록(64자리 지문 등)은 접힌 상세로 — 기본 흐름에는 재생기·작업대만.
            with gr.Accordion("자막 상세 — 등록·실행 기록", open=False):
                self._caption_md = gr.Markdown(
                    "_자막 상태가 여기 나타난다._", elem_id="caption-state"
                )

            self._caption_md.change(
                self.caption_public_status, self._caption_md, caption_public_status
            )

            # 문맥(수명 토큰·화면이 본 머리)을 주고받는다 — 등록·새로고침은 새
            # 문맥을 내리고, 수동 편집·확정은 옛 문맥 사건을 거부한다.
            assert self._caption_path_box is not None
            self._caption_pick_button = caption_pick_button
            caption_pick_button.click(
                self.final_video_chooser_view,
                None,
                [self._caption_path_box],
                api_name=False,
                concurrency_limit=1,
                concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
            )
            caption_register_inputs = [
                self._signature_state,
                self._caption_path_box,
                caption_source_box,
                caption_ordinal_dd,
            ]
            caption_register_outputs = [
                self._caption_md,
                self._caption_player_html,
                self._caption_workbench_block,
                self._caption_context_state,
                self._caption_head_state,
            ]
            caption_register_button.click(
                self._on_caption_register, caption_register_inputs, caption_register_outputs
            )
            self._advanced_wirings["caption-register"] = (
                caption_register_inputs,
                caption_register_outputs,
            )
            self._advanced_handlers["caption-register"] = self._on_caption_register
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

            caption_refresh_inputs = [self._signature_state, caption_run_box]
            caption_refresh_outputs = [
                self._caption_md,
                self._caption_table_html,
                self._caption_player_html,
                self._caption_cue_dd,
                self._caption_workbench_block,
                self._caption_context_state,
                self._caption_head_state,
            ]
            caption_refresh_button.click(
                self._on_caption_refresh, caption_refresh_inputs, caption_refresh_outputs
            )
            self._advanced_wirings["caption-refresh"] = (
                caption_refresh_inputs,
                caption_refresh_outputs,
            )
            self._advanced_handlers["caption-refresh"] = self._on_caption_refresh
            caption_trace_button.click(
                self.caption_trace_view,
                [self._signature_state, caption_run_box],
                self._caption_md,
            )

            caption_edit_inputs = [
                self._caption_context_state,
                self._caption_head_state,
                self._signature_state,
                caption_op_dd,
                caption_cue_box,
                caption_text_box,
                caption_start_box,
                caption_end_box,
                caption_second_box,
                caption_index_box,
                caption_warning_box,
            ]
            caption_edit_button.click(
                self._on_caption_edit,
                caption_edit_inputs,
                caption_refresh_outputs,
            )
            self._advanced_wirings["caption-edit"] = (caption_edit_inputs, caption_refresh_outputs)
            self._advanced_handlers["caption-edit"] = self._on_caption_edit

            caption_confirm_inputs = [
                self._caption_context_state,
                self._caption_head_state,
                self._signature_state,
                caption_run_box,
                caption_final_confirm_box,
            ]
            caption_confirm_outputs = [
                self._caption_md,
                self._caption_workbench_block,
                self._caption_context_state,
                self._caption_head_state,
            ]
            caption_confirm_button.click(
                self._on_caption_confirm, caption_confirm_inputs, caption_confirm_outputs
            )
            self._advanced_wirings["caption-confirm"] = (
                caption_confirm_inputs,
                caption_confirm_outputs,
            )
            self._advanced_handlers["caption-confirm"] = self._on_caption_confirm
            assert self._caption_cue_dd is not None
            self._caption_cue_dd.change(
                self.caption_cue_pick_view,
                [self._signature_state, self._caption_cue_dd],
                [caption_cue_box, caption_text_box, caption_start_box, caption_end_box],
            )
            self._caption_cue_dd.change(
                None,
                [self._caption_cue_dd],
                None,
                js=_CUE_SEEK_JS,
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
        str,
        str,
        str,
        str,
        str,
        str,
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
            *self._workbench_view_values(self._snapshot_root()),
            self._ws_path_display(),
            "",
            "",
        )

    def build_blocks(self) -> gr.Blocks:
        # theme·css는 gradio 6부터 launch/마운트 쪽 인자다(Blocks 생성자에 넣으면 경고).
        with gr.Blocks(title="vlogsrt 원본 브라우저") as app:
            gr.Markdown("### vlogsrt · Final Cut 컷 정리")
            gr.Markdown("라이브러리 불러오기 → 자막·영상 분석 → 사용할 컷 선택 → Final Cut 출력")
            gr.Markdown(
                "**온라인 모드** · 선택한 영상의 범위를 승인한 뒤 유료 분석을 실행합니다."
                if raw_service.browser_network_mode() == "online"
                else "**오프라인 모드** · 이 실행에서는 유료 분석을 차단합니다.",
                elem_id="app-analysis-mode",
            )
            # 화면이 마지막으로 본 작업공간 신원 — 선택의 최신 증명에만 쓴다.
            # 정본은 서버(DB)에 있고, 이 값은 새로고침마다 서버에서 다시 내려온다.
            self._signature_state = gr.State(None)
            # 고급 패널 문맥 상태 — 실제 배선이 주고받는 클라이언트 소유 값(세션별).
            # 편집 계열 콜백은 사건 순간의 이 값과 서버의 지금 값을 비교한다.
            self._edit_context_state = gr.State(None)
            self._review_head_state = gr.State(None)
            self._caption_context_state = gr.State(None)
            self._caption_head_state = gr.State(None)
            open_button, create_button = self._project_tab()
            extract_button, refresh_segment, profile_dd, analysis_plan = self._review_tab()
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
            # 경로는 선택기에서만 온다 — 열기는 폴더/`.fcpbundle`을 스스로 가르고,
            # 만들기는 이름만 받아 자리를 선택기로 얻는다. 선택기 단추는 대화상자가
            # 겹치지 않게 공유 동시성 묶음에 넣고 API 노출은 만들지 않는다(화면 전용).
            selection_outputs = self._selection_outputs()
            fcp_outputs = [
                self._fcp_originals_group,
                self._fcp_primary_info,
                self._fcp_primary_import,
            ]
            self._direct_project_open.click(
                self.direct_project_open_view,
                self._direct_project_path,
                [*selection_outputs, *fcp_outputs],
            )
            self._quick_library_button.click(
                self.capcut_fcp_choose_view,
                None,
                [*selection_outputs, *fcp_outputs],
                api_name=False,
                concurrency_limit=1,
                concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
            ).then(lambda: gr.update(open=False), None, self._project_tools)
            self._fcp_primary_button.click(
                self.capcut_fcp_choose_view,
                None,
                [*selection_outputs, *fcp_outputs],
                api_name=False,
                concurrency_limit=1,
                concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
            )
            self._fcp_primary_import.click(
                self.capcut_fcp_import_view,
                [self._signature_state, self._fcp_originals_group],
                [self._capcut_status, self._visual_player_block, self._visual_workbench_block],
                js="""(signature, selected) => {
                    const button = document.querySelector('#project-tools > button.label-wrap');
                    if (button && button.classList.contains('open')) button.click();
                    return [signature, selected];
                }""",
            )
            open_button.click(
                self.open_project_chooser_view,
                None,
                selection_outputs,
                api_name=False,
                concurrency_limit=1,
                concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
            ).then(self.capcut_fcp_panel_view, None, fcp_outputs)
            assert self._project_name_box is not None
            create_button.click(
                self.create_project_chooser_view,
                [self._project_name_box],
                selection_outputs,
                api_name=False,
                concurrency_limit=1,
                concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
            ).then(self.capcut_fcp_panel_view, None, fcp_outputs)
            app.load(self.refresh_outputs, None, self._load_outputs()).then(
                self.capcut_fcp_panel_view, None, fcp_outputs
            )
        return app

    def _ws_path_display(self) -> str:
        """읽기전용 경로 표시 칸의 값 — 지금 열린 작업공간 자리(선택기로만 정해진다)."""
        root = self._snapshot_root()
        return str(root) if root is not None else ""

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
            and self._ws_path_box is not None
            and self._source_box is not None
            and self._caption_path_box is not None
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
            *self._workbench_outputs(),
            self._ws_path_box,
            self._source_box,
            self._caption_path_box,
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
