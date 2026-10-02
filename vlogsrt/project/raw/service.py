"""UI·CLI가 공유하는 v2 작업공간 공개 서비스(계획 §4·§15).

`ui/workspace_service.py`가 가져올 수 있는 **정확히 이 모듈**이다(A0 계약 예외).
여기엔 gradio·fastapi가 없다 — HTTP와 화면 조립은 UI 계층의 몫이고, 이 계층은
작업공간·등록·탐색·작업·접근 정책을 값으로 다룬다. 네트워크 호출도 없다: 오프라인
기본은 `config.RawWorkflowConfig`가 이미 못 박고 있고 이 서비스는 그 값을 읽기만
한다(§14.3 — UI가 우회하지 않는다).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from vlogsrt.project.raw.config import (
    WORKFLOW_SCHEMA_VERSION,
    RawWorkflowConfig,
    validate_raw_workflow_config,
)
from vlogsrt.project.raw.ingest import (
    IngestConfirmation,
    IngestProposal,
    MediaInspect,
    confirm_ingest,
    ffprobe_media_inspector,
    reconfirm_ingest,
    run_ingest_copy,
    scan_sources,
)
from vlogsrt.project.raw.jobs import JobContext, JobManager
from vlogsrt.project.raw.models import (
    ACTIVE_JOB_STATUSES,
    INGEST_JOB_KIND,
    MEDIA_KINDS,
    MEDIA_SUFFIXES,
    ORDER_BASIS_USER_CONFIRMED,
    BrowseNotAllowedError,
    ByteRange,
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
    RangeNotSatisfiableError,
    RawWorkspaceError,
    ResolvedMedia,
    SourceAsset,
    SourceOrderRevision,
    WorkspaceStatus,
)
from vlogsrt.project.raw.source_order import propose_global_order as _propose_global_order
from vlogsrt.project.raw.workspace import DB_SCHEMA_VERSION, RawWorkspace

__all__ = [
    # UI는 이 모듈만 가져갈 수 있다(정확 경로 예외) — 계약 오류도 여기서 다시 낸다.
    "ACTIVE_JOB_STATUSES",
    "BrowseNotAllowedError",
    "ByteRange",
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
    "MEDIA_KINDS",
    "MediaInspect",
    "MediaNotAllowedError",
    "MediaNotFoundError",
    "MediaNotReadyError",
    "MediaRegistration",
    "MediaTargetChangedError",
    "RangeNotSatisfiableError",
    "RawWorkspaceError",
    "RawWorkspaceService",
    "ResolvedMedia",
    "SourceAsset",
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
