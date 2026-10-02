"""브라우저 작업공간 UI의 서버 측 조립 — v2 계획의 정확 경로 예외 파일.

이 파일만 `vlogsrt.project.raw.service`를 가져올 수 있다(docs/PROJECT_CONTRACTS.md §1
둘째 예외). HTTP 경계도 여기 산다: Host/Origin 가드가 Gradio 엔드포인트와 원본
엔드포인트(`/media/{id}`)를 함께 지키고(§4.4), 미디어는 **등록된 ID로만** Range로
제공된다. 화면 상태는 `gr.State`가 아니라 서버(DB)가 소유한다(§14.1) — 새로고침은
다시 붙고, 서버 시작 때만 작업 복구가 일어난다.

레거시 단일 영상 화면(`ui/service.py`)은 그대로다. 이 모듈은 그것을 건드리지 않는다.
"""

from __future__ import annotations

import html
import json
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from fractions import Fraction
from pathlib import Path

# 로컬 전용 UI다 — gradio의 사용 보고가 네트워크를 만지지 않게 수입 전에 끈다.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402

from vlogsrt.project.raw import service as raw_service  # noqa: E402

STATE_ENV = "VLOGSRT_RAW_UI_STATE"
"""서버 상태 파일 경로 재정의 — 루트의 합성 작업공간 시험이 쓴다(사용자 .env 불가)."""

MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""Origin/Referer 검사를 강제하는 메서드 — Gradio 변경·업로드 엔드포인트가 여기 온다."""

STREAM_CHUNK_BYTES = 262144


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
    ) -> None:
        self.port = port
        self.raw = raw_service.RawWorkspaceService()
        self.state_path = state_path if state_path is not None else default_state_path()
        self._inspector = inspector  # None이면 서비스 기본(ffprobe). 시험은 가짜 조사자를 넣는다.
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
        self._media_dd: gr.Dropdown | None = None
        self._jobs_dd: gr.Dropdown | None = None
        self._devices_dd: gr.Dropdown | None = None
        self._ingest_jobs_dd: gr.Dropdown | None = None
        self._clip_dd: gr.Dropdown | None = None
        self._extract_jobs_dd: gr.Dropdown | None = None
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

    def _remember(self, root: Path | None) -> None:
        with self._selection_lock:
            self._root = root
            self._ingest_proposal = None
            self._ingest_signature = None
            self._ingest_confirmation = None
        if root is not None:
            self._write_state(root)

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
                    info="가져올 원본이 있는 폴더. 아직 가져오기 실행은 다음 단계에서 열린다.",
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
                [self._ingest_md, self._ingest_jobs_dd],
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

    def _review_tab(self) -> tuple[gr.Button, gr.Button, gr.Dropdown]:
        with gr.Tab("2 · 분석 · 조각 검토"):
            gr.Markdown(
                "> 이 단계는 **결정적 분할**이다 — 의미 분석(Luna) 없이 확정된 전체"
                " 순서 위에서 각 원본의 측정된 프레임 격자로 전체를 빈틈없이 타일링한다."
                " 유료 호출은 전혀 없다(오프라인)."
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
        return extract_button, refresh_segment, self._profile_dd

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
    ]:
        """작업공간 전환 뒤 화면이 받아야 하는 전체 보기(서명 포함).

        루트를 **한 번** 찍어 모든 조각에 통과시킨다 — 전환이 끼어들어도 이 보기가
        두 작업공간에 걸치지 않는다(코디네이터 재결). 마지막 두 값은 가져오기
        패널(안내·재시도 목록)이다.
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

    def ingest_refresh_view(self, signature: str | None) -> tuple[str, gr.Dropdown]:
        """진행 상태 새로고침 — 서버(DB)가 정본, 화면은 다시 읽기만 한다."""
        root = self._require_current_selection(signature)
        return self._ingest_state_markdown(root), gr.Dropdown(
            choices=self.resumable_ingest_choices(root)
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
        """조각 추출 시작 이벤트 — 로컬 ffmpeg 순차 추출 작업을 제출한다(유료 호출 없음)."""
        root = self._require_current_selection(signature)
        try:
            handle = self.raw.start_segment_extraction(
                root, profile_name=profile_name or "source_matched"
            )
        except raw_service.RawWorkspaceError as error:
            raise gr.Error(str(error)) from error
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
                "> Finder에서 열기는 OS 명령 경계가 필요해 이번 단계에 없다 — 위 경로를"
                " Finder 주소창에 붙여 넣으면 같은 자리가 열린다. 폴더 확정·선택본"
                " 내보내기는 A6가 연다."
            )

    def _caption_tab(self) -> None:
        with gr.Tab("4 · 최종 영상 · 자막"):
            self._settings_md = gr.Markdown(self.settings_markdown())
            gr.Markdown(
                "최종 영상 등록·정밀 자막 생성·수정·내보내기는 마지막 단계에서 열린다.\n\n"
                "지금은 설정 요약만 보인다 — 승인 없이 유료 호출이 일어나는 일은 없다."
            )
            gr.Button("정밀 자막 생성", interactive=False)
            gr.Button("SRT 내보내기", interactive=False)

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
        )

    def build_blocks(self) -> gr.Blocks:
        # theme·css는 gradio 6부터 launch/마운트 쪽 인자다(Blocks 생성자에 넣으면 경고).
        with gr.Blocks(title="vlogsrt 원본 브라우저") as app:
            gr.Markdown("### vlogsrt 원본 브라우저 · 원본 → 조각 → 편집 → 자막")
            # 화면이 마지막으로 본 작업공간 신원 — 선택의 최신 증명에만 쓴다.
            # 정본은 서버(DB)에 있고, 이 값은 새로고침마다 서버에서 다시 내려온다.
            self._signature_state = gr.State(None)
            open_button, create_button, ws_path_box, new_name_box = self._project_tab()
            extract_button, refresh_segment, profile_dd = self._review_tab()
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
