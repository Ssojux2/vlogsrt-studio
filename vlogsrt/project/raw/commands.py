"""`vlogsrt media` — A2 가져오기·A3 분할 추출의 진단·자동화 CLI(계획 §15.2).

브라우저가 기본 실행 경로다. 이 명령은 시험·진단·자동화용이며, 공개 서비스
(`vlogsrt.project.raw.service`)만을 부른다 — 이 계층이 네트워크를 아는 길은
없다. `import --confirm`이 승인하는 것은 **로컬 복사뿐**이고, `extract`가 하는
일도 로컬 ffmpeg 추출뿐이다: 어떤 플래그도 유료 호출·온라인 분석을 켜지
않는다(코디네이터 ACK). 이 모듈은 `cli.py`를 import하지 못한다(계약 §1) —
조립은 `vlogsrt/project/commands.py`가 맡는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from vlogsrt import log
from vlogsrt.env import ANALYSIS_CLAUDE_KEY_ENV
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.config import (
    ANALYSIS_MODEL_CHOICES,
    ANALYSIS_PROVIDER_ANTHROPIC,
    analysis_model_label,
    analysis_provider,
    claude_profile,
)
from vlogsrt.project.raw.models import (
    ANALYSIS_JOB_KIND,
    ANALYSIS_SEMANTIC_JOB_KIND,
    AnalysisEstimate,
    PlannedInterval,
)
from vlogsrt.project.raw.segmentation import DEFAULT_TILE_SECONDS

JOB_WAIT_SECONDS = 600.0
"""복사·추출 작업 완료 대기 상한 — 진단 명령이 무한히 묶여 있지 않게 한다."""

JOB_POLL_SECONDS = 0.2
"""작업 상태 확인 간격 — DB 갱신을 얻어맞지 않게 짧게 쉰다."""

EXTRACT_WAIT_SECONDS = 3600.0
"""추출 작업 대기 상한 — 조각 수만큼 ffmpeg이 도는 시간을 담는다."""

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _service() -> raw_service.RawWorkspaceService:
    return raw_service.RawWorkspaceService()


def _human_bytes(size: int) -> str:
    scaled = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if scaled < 1024 or unit == "TB":
            return f"{scaled:,.0f}{unit}" if unit == "B" else f"{scaled:,.1f}{unit}"
        scaled /= 1024
    return f"{scaled:,.1f}TB"


def cmd_init(args: argparse.Namespace) -> int:
    """`media init` — 작업공간 생성(진단용). 브라우저 만들기와 같은 규칙을 지난다."""
    root = Path(args.path)
    try:
        status = _service().create_workspace(root, args.name)
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    log.out(f"작업공간을 만들었다: {status.workspace_root} (신원 {status.workspace_id})")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """`media import` — 조사·제안 인쇄. `--confirm`일 때만 확정·복사까지 간다.

    `--confirm`은 로컬 복사의 명시적 승인이다(사람의 순서 확인에 해당).
    없으면 제안만 보이고 아무 것도 쓰지 않는다 — 완료로 위장하지 않는다.
    """
    service = _service()
    workspace_root = Path(args.workspace)
    try:
        status = service.open_workspace(workspace_root)
        proposal = service.scan_sources(Path(status.workspace_root), Path(args.source))
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    _print_proposal(proposal)
    if not proposal.order:
        log.warn("가져올 영상이 없다 — 제외 사유를 확인하세요.")
        return 1
    if not args.confirm:
        log.out("\n--confirm 없이는 복사하지 않는다 — 이 제안을 확정하려면 --confirm을 붙여라.")
        return 0
    try:
        confirmation = service.confirm_ingest(Path(status.workspace_root), proposal)
    except raw_service.RawWorkspaceError as error:
        log.error(f"확정 실패(아무 것도 쓰지 않았다): {error}")
        return 1
    log.out(
        f"확정 — 배치 {confirmation.batch_dir} · 순서 리비전 {confirmation.revision.revision_id}"
        f" · 작업 지문 {confirmation.job_fingerprint[:12]}…"
    )
    handle = service.start_ingest_copy(Path(status.workspace_root), confirmation)
    return _await_ingest_job(service, Path(status.workspace_root), handle.job_id)


def cmd_retry(args: argparse.Namespace) -> int:
    """`media retry` — 실패·중단된 가져오기 작업의 명시적 재시도(같은 ID·지문)."""
    service = _service()
    workspace_root = Path(args.workspace)
    try:
        status = service.open_workspace(workspace_root)
        handle = service.retry_ingest_copy(Path(status.workspace_root), args.job)
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    log.out(f"재시도 제출 — {handle.job_id}")
    return _await_ingest_job(service, Path(status.workspace_root), handle.job_id)


def cmd_inspect(args: argparse.Namespace) -> int:
    """`media inspect` — 자산·순서 리비전·중복·작업의 현재 상태 인쇄."""
    service = _service()
    try:
        status = service.open_workspace(Path(args.workspace))
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    assets, revision, jobs = service.ingest_overview(Path(status.workspace_root), args.batch)
    if not assets:
        log.out("가져오기 자산이 없다.")
        return 0
    log.out(
        f"배치 {assets[0].batch_dir} · 순서 리비전 {revision.revision_id if revision else '?'}"
        f" · 근거 {revision.basis if revision else '?'}"
    )
    for asset in assets:
        digest = (asset.content_sha256 or "")[:12] or "—"
        duplicate = f" · 중복_of {asset.duplicate_of}" if asset.duplicate_of else ""
        error_note = f" · 오류 {asset.error}" if asset.error else ""
        log.out(
            f"{asset.ordinal:3d}. [{asset.status:8s}] {asset.source_relative_path}"
            f" · {_human_bytes(asset.size_bytes)} · sha256 {digest}…{duplicate}{error_note}"
        )
    for job in jobs:
        log.out(
            f"작업 {job.job_id} {job.status} {job.progress_current}/{job.progress_total}"
            + (f" — {job.error}" if job.error else "")
        )
    return 0


def _print_proposal(proposal: raw_service.IngestProposal) -> None:
    log.out(f"원본 루트: {proposal.scan.source_root}")
    log.out(
        f"후보 {len(proposal.order)}건 · 제외 {len(proposal.scan.rejected)}건 · 총 "
        f"{_human_bytes(proposal.scan.total_bytes)}"
    )
    for index, candidate in enumerate(proposal.order, start=1):
        inspection = candidate.inspection
        duration = (
            "미확인"
            if inspection.duration_seconds is None
            else f"{inspection.duration_seconds:.1f}초"
        )
        capture = _capture_note(inspection)
        log.out(
            f"{index:3d}. {candidate.source_relative_path} · "
            f"{_human_bytes(candidate.size_bytes)} · {duration} · 촬영 시각 {capture}"
        )
    for item in proposal.scan.rejected:
        log.warn(f"제외: {item.source_relative_path} — {item.reason}")
    space = proposal.space
    verdict = "충분" if space.sufficient else "부족 — 복사가 시작 전에 차단된다"
    log.out(
        f"공간: 필요 {_human_bytes(space.required_bytes)}(여유 포함) / "
        f"남은 {_human_bytes(space.free_bytes)} → {verdict}"
    )


def _capture_note(inspection: raw_service.SourceInspection) -> str:
    """촬영 시각 표기 — 유효하면 시각, 무효면 '무효', 아니면 '미조사'(§6.2)."""
    if inspection.capture_time is not None:
        return inspection.capture_time
    if inspection.capture_time_status == "invalid":
        return "무효(선언이 있으나 못 믿는다)"
    return "미조사"


def _await_ingest_job(service: raw_service.RawWorkspaceService, root: Path, job_id: str) -> int:
    """작업이 끝날 때까지 기다리고 결과를 인쇄한다 — 진단 CLI는 완결 보고가 몫이다."""
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in _TERMINAL_STATUSES:
            _print_ingest_result(service, root, record.status, record.error)
            return 0 if record.status == "succeeded" else 1
        time.sleep(JOB_POLL_SECONDS)
    log.warn(f"작업 {job_id}가 아직 끝나지 않았다 — 브라우저 또는 media inspect로 확인하세요.")
    return 1


def _print_ingest_result(
    service: raw_service.RawWorkspaceService, root: Path, status: str, error: str | None
) -> None:
    assets, _, _ = service.ingest_overview(root)
    ready = [asset for asset in assets if asset.status == "ready"]
    failed = [asset for asset in assets if asset.status == "failed"]
    incomplete = [asset for asset in assets if asset.status not in ("ready", "failed")]
    log.out(f"복사 작업 {status} — ready {len(ready)}/{len(assets)}건")
    for asset in ready:
        log.out(
            f"  ready  {asset.source_relative_path} · sha256 {(asset.content_sha256 or '')[:12]}…"
        )
    for asset in failed:
        log.warn(f"  failed {asset.source_relative_path} — {asset.error}")
    for asset in incomplete:
        log.warn(f"  {asset.status:8s} {asset.source_relative_path} (재시도 가능)")
    if error:
        log.error(f"작업 오류: {error}")


def media_command(args: argparse.Namespace) -> int:
    """`vlogsrt media` 진입 — 하위 명령으로 갈라 보낸다."""
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "init": cmd_init,
        "import": cmd_import,
        "retry": cmd_retry,
        "inspect": cmd_inspect,
        "plan": cmd_plan,
        "extract": cmd_extract,
        "clips": cmd_clips,
        "analyze": cmd_analyze,
        "review": cmd_review,
        "caption": cmd_caption,
        "finalcut": cmd_finalcut,
        "sync-check": cmd_sync_check,
        "settings": cmd_settings,
    }
    return handlers[args.media_command](args)


def add_media_parser(sub: argparse._SubParsersAction) -> None:
    """`vlogsrt media` 하위 명령 정의 — 조립은 이 함수 하나로 끝난다."""
    media = sub.add_parser(
        "media", help="원본 영상 브라우저 v2 — 작업공간·가져오기·분할 추출 진단 (로컬만)"
    )
    commands = media.add_subparsers(
        dest="media_command",
        required=True,
        metavar=(
            "{init,import,retry,inspect,plan,extract,clips,analyze,review,caption,finalcut,"
            "sync-check,settings}"
        ),
    )

    init = commands.add_parser("init", help="빈 폴더에 v2 작업공간을 만든다")
    init.add_argument("path", type=Path, help="작업공간으로 쓸 **빈** 폴더 경로")
    init.add_argument("--name", required=True, help="프로젝트 이름")

    source = commands.add_parser(
        "import", help="SD/폴더를 조사해 제안을 인쇄한다(--confirm이면 복사까지)"
    )
    source.add_argument("source", type=Path, help="원본 폴더(SD 카드) 경로")
    source.add_argument("--workspace", type=Path, required=True, help="가져올 작업공간 경로")
    source.add_argument(
        "--confirm",
        action="store_true",
        help="제안된 원본 순서를 확정하고 로컬 복사·검증을 시작한다(유료 호출 없음)",
    )

    retry = commands.add_parser("retry", help="실패·중단된 가져오기의 명시적 재시도")
    retry.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    retry.add_argument("job", help="재시도할 작업 ID(예: job_000001)")

    inspect = commands.add_parser("inspect", help="가져오기 자산·순서·작업 상태를 인쇄한다")
    inspect.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    inspect.add_argument(
        "--batch", default=None, help="볼 배치(예: import_000001). 없으면 최근 배치"
    )

    plan = commands.add_parser(
        "plan", help="확정된 전체 순서 위에서 결정적 분할 계획을 만든다(로컬만)"
    )
    plan.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    plan.add_argument(
        "--tile-seconds",
        type=Fraction,
        default=None,
        help="균일 타일 길이(초, 정확한 유리수). 기본은 설정 값이다",
    )
    plan.add_argument(
        "--intervals",
        default=None,
        help="사람이 정한 구간 파일 — 각 줄: 자산ID 시작 끝(초, 유리수). 전체를 덮아야 한다",
    )

    extract = commands.add_parser(
        "extract", help="분할 계획을 실제 조각 폴더로 추출한다(로컬 ffmpeg만, 유료 호출 없음)"
    )
    extract.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    extract.add_argument(
        "--plan", default=None, help="추출할 분할 계획 리비전 ID. 없으면 최근 계획"
    )
    extract.add_argument(
        "--profile",
        choices=raw_service.EXTRACT_PROFILES,
        default="source_matched",
        help="추출 프로파일(§8.1)",
    )
    extract.add_argument(
        "--handle-before", type=Fraction, default=Fraction(0), help="앞 편집 여유분(초)"
    )
    extract.add_argument(
        "--handle-after", type=Fraction, default=Fraction(0), help="뒤 편집 여유분(초)"
    )
    extract.add_argument(
        "--wait-seconds",
        type=float,
        default=EXTRACT_WAIT_SECONDS,
        help="작업 완료 대기 상한(초)",
    )

    clips = commands.add_parser("clips", help="package의 조각 목록·전달물 경로를 인쇄한다")
    clips.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    clips.add_argument(
        "--package", default=None, help="볼 package(예: clips/package_000001). 없으면 최근 것"
    )

    analyze = commands.add_parser(
        "analyze",
        help="의미 분석 견적을 인쇄하고 오프라인 재생 분석을 실행한다(유료 호출 없음)",
    )
    analyze.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    analyze.add_argument(
        "--sources",
        default=None,
        nargs="+",
        help="분석할 자산 ID(공백 구분). 없으면 확정 전체 순서 전부",
    )
    analyze.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="재생 픽스처 디렉터리(asr-fixtures.json·luna-fixtures.json)."
        " 기본은 작업공간 analysis/replay/",
    )
    analyze.add_argument(
        "--estimate-only",
        action="store_true",
        help="견적만 인쇄하고 분석을 시작하지 않는다",
    )
    analyze.add_argument(
        "--plan",
        action="store_true",
        help="분석이 완성되면 그 결과로 분할 계획 리비전을 만든다(로컬만)",
    )
    analyze.add_argument(
        "--wait-seconds",
        type=float,
        default=EXTRACT_WAIT_SECONDS,
        help="분석 작업 완료 대기 상한(초)",
    )

    review = commands.add_parser(
        "review",
        help="조각 검토 리비전을 만들거나 인쇄한다(로컬만 — 유료 호출·미디어 재생성 없음)",
    )
    review.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    review.add_argument(
        "--init",
        action="store_true",
        help="최신 계획·분석 증거에서 검토 기본 리비전을 만든다(이미 있으면 새로 만든다)",
    )

    caption = commands.add_parser(
        "caption",
        help="최종 영상을 등록하고 자막 실행 추정을 인쇄한다(로컬만 — 실행은 브라우저 승인 경로)",
    )
    caption.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    caption.add_argument("--path", type=Path, required=True, help="최종 영상 절대 경로")
    caption.add_argument("--source-id", default="final_1", help="출처 ID(기본 final_1)")
    caption.add_argument(
        "--audio-ordinal",
        type=int,
        default=None,
        help="오디오 트랙 서수(여럿일 때). 없으면 자동(첫 트랙)",
    )
    caption.add_argument("--run", default="run-1", help="실행 ID(기본 run-1)")
    _add_finalcut_parser(commands)
    _add_settings_parser(commands)


def _add_finalcut_parser(commands: argparse._SubParsersAction) -> None:
    finalcut = commands.add_parser(
        "finalcut",
        help="불러온 원본 전부를 태그 달린 Final Cut 타임라인 하나로 쓴다"
        "(로컬만 — Final Cut은 열지 않는다)",
    )
    finalcut.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")

    sync = commands.add_parser(
        "sync-check",
        help="원본 자막 시각을 말소리와 대조해 표시만 한다(D55 — 무료 로컬 VAD · 자막은 안 바뀐다)",
    )
    sync.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    sync.add_argument(
        "--source", action="append", default=[], help="잴 원본 ID(여러 번). 없으면 전부"
    )
    sync.add_argument("--json", action="store_true", help="보고서 JSON을 그대로 인쇄")
    sync.add_argument(
        "--no-write", action="store_true", help="작업공간에 보고서·캐시를 하나도 쓰지 않는다"
    )


def cmd_sync_check(args: argparse.Namespace) -> int:
    """`media sync-check` — 원본 자막의 시각 판정을 인쇄한다(표시만, 유료 호출 0)."""
    from vlogsrt.project import sync_check
    from vlogsrt.project.raw import sync_report

    documents = _service().measure_sync_checks(
        args.workspace, tuple(args.source) or None, write=not args.no_write
    )
    if args.json:
        sys.stdout.write(sync_check.report_json({"reports": documents}) + "\n")
        return 0
    for document in documents:
        for line in sync_report.summary_lines(document):
            log.out(line)
    if not documents:
        log.warn("잴 원본 자막이 없다 — 분석이 끝난 원본만 잰다")
    return 0


def _add_settings_parser(commands: argparse._SubParsersAction) -> None:
    """`media settings` — 의미 분석 모델을 인쇄하고 고른다(D54). 로컬 설정만 바꾼다."""
    settings = commands.add_parser(
        "settings", help="의미 분석 모델을 인쇄하고 고른다(로컬 설정만 — 유료 호출 없음)"
    )
    settings.add_argument("--workspace", type=Path, required=True, help="작업공간 경로")
    settings.add_argument(
        "--analysis-model",
        choices=ANALYSIS_MODEL_CHOICES,
        default=None,
        help="의미 분석 모델. 없으면 지금 설정을 인쇄만 한다",
    )


# ── W-14 — 의미 분석 모델 선택(로컬 설정만) ───────────────────────────────────


def cmd_settings(args: argparse.Namespace) -> int:
    """`media settings` — 의미 분석 모델을 인쇄하고, 주면 바꾼다(유료 호출 없음).

    분석이 도는 동안에는 바꾸지 않는다 — 견적·승인이 본 모델과 실행 모델이 갈린다.
    기본(gpt-6-luna)은 그대로다: 고르지 않은 작업공간의 모델이 조용히 바뀌지 않는다.
    """
    service = _service()
    try:
        status = service.open_workspace(Path(args.workspace))
        root = Path(status.workspace_root)
        workflow = service.settings(root)
        if args.analysis_model is not None and args.analysis_model != workflow.models.luna_model:
            if _analysis_active(service, root):
                log.error("분석이 진행 중이다 — 끝나거나 취소된 뒤에 의미 분석 모델을 바꾸세요")
                return 1
            models = replace(workflow.models, luna_model=args.analysis_model)
            workflow = replace(workflow, models=models)
            service.update_settings(root, workflow)
    except (raw_service.RawWorkspaceError, ValueError) as error:
        log.error(str(error))
        return 1
    for line in _analysis_model_lines(workflow.models.luna_model):
        log.out(line)
    return 0


def _analysis_active(service: raw_service.RawWorkspaceService, root: Path) -> bool:
    return any(
        job.kind in (ANALYSIS_JOB_KIND, ANALYSIS_SEMANTIC_JOB_KIND)
        and job.status not in _TERMINAL_STATUSES
        for job in service.list_jobs(root)
    )


def _analysis_model_lines(model: str) -> list[str]:
    """의미 분석 모델의 설명 — 키는 이름만 말하고 값은 어디에도 싣지 않는다."""
    label = analysis_model_label(model)
    if analysis_provider(model) != ANALYSIS_PROVIDER_ANTHROPIC:
        return [f"의미 분석 모델: {label} — 단가 미확인(금액 추정 없음, 0원 계산 금지)"]
    profile = claude_profile(model)
    return [
        f"의미 분석 모델: {label} ({model}) — 공급자 Anthropic, 키 {ANALYSIS_CLAUDE_KEY_ENV}(.env)",
        f"  공식 단가({profile.pricing_checked_at.isoformat()}): 입력 ${profile.input_per_mtok}"
        f" · 출력 ${profile.output_per_mtok} / 100만 토큰 — 견적은 창당 보수적 상한이다",
        f"  effort {profile.effort} · 출력 한도 {profile.max_output_tokens} 토큰",
    ]


# ── A3 — 결정적 분할 계획·추출·조각 목록 ──────────────────────────────────────


def cmd_plan(args: argparse.Namespace) -> int:
    """`media plan` — 확정 전체 순서 위에서 분할 계획 리비전을 만들어 인쇄한다."""
    service = _service()
    try:
        status = service.open_workspace(Path(args.workspace))
        root = Path(status.workspace_root)
        intervals = _read_intervals(args.intervals)
        snapshot = service.plan_segmentation(
            root,
            tile_seconds=args.tile_seconds or DEFAULT_TILE_SECONDS,
            intervals=intervals,
        )
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    log.out(
        f"분할 계획 — 리비전 {snapshot.revision_id} · 순서 리비전"
        f" {snapshot.order_revision_id} · 조각 {len(snapshot.entries)}개 · 지문"
        f" {snapshot.fingerprint[:12]}…"
    )
    for proof in snapshot.coverage:
        seconds = Fraction(proof.last_core_end_pts - proof.first_core_start_pts, 1) / Fraction(
            proof.time_base_num, proof.time_base_den
        )
        log.out(
            f"  원본 순번 {proof.source_order}: 조각 {proof.segment_count}개 ·"
            f" {proof.source_frame_count}프레임 · 덮음 {seconds}초"
        )
    for entry in snapshot.entries:
        log.out(
            f"  {entry.source_order}/{entry.segment_order:03d} · {entry.segment_id}"
            f" · pts [{entry.core_start_pts}, {entry.core_end_pts})"
        )
    return 0


def _read_intervals(path_text: str | None) -> dict[str, list[PlannedInterval]] | None:
    """구간 파일 읽기 — 각 줄이 `자산ID 시작 끝`이다. 없으면 None(균일 타일)."""
    if not path_text:
        return None
    intervals: dict[str, list[PlannedInterval]] = {}
    for line in Path(path_text).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        asset_id, start_text, end_text = text.split()
        spans = intervals.setdefault(asset_id, [])
        spans.append(PlannedInterval(start=Fraction(start_text), end=Fraction(end_text)))
    return intervals


def cmd_extract(args: argparse.Namespace) -> int:
    """`media extract` — 계획을 실제 조각 폴더로 추출하는 작업을 제출하고 기다린다."""
    service = _service()
    try:
        status = service.open_workspace(Path(args.workspace))
        root = Path(status.workspace_root)
        handle = service.start_segment_extraction(
            root,
            plan_revision_id=args.plan,
            profile_name=args.profile,
            handles_before=args.handle_before,
            handles_after=args.handle_after,
        )
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    note = " (기존 작업에 다시 붙음)" if handle.reattached else ""
    log.out(f"추출 작업 제출 — {handle.job_id}{note}")
    return _await_job(service, root, handle.job_id, args.wait_seconds)


def cmd_clips(args: argparse.Namespace) -> int:
    """`media clips` — package 상태·조각 목록·전달물 경로를 인쇄한다."""
    service = _service()
    try:
        status = service.open_workspace(Path(args.workspace))
        summary = service.package_overview(Path(status.workspace_root), args.package)
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    if summary is None:
        log.out("package가 없다 — 분할 계획과 추출을 먼저 실행하세요.")
        return 0
    package = summary.package
    log.out(
        f"package {package.package_dir} · 상태 {package.status} · 게시"
        f" {package.clip_published}/{package.clip_total} · 프로파일 {package.profile_name}"
    )
    for clip in summary.clips:
        note = f" — {clip.error}" if clip.error else ""
        log.out(
            f"  {clip.source_order}/{clip.segment_order:03d} · {clip.clip_id} ·"
            f" {clip.relative_path} · {clip.status}{note}"
        )
    for manifest in summary.manifest_relative_paths:
        log.out(f"  전달물: {manifest}")
    if package.error:
        log.error(f"package 오류: {package.error}")
    return 0 if package.complete else 1


def _await_job(
    service: raw_service.RawWorkspaceService, root: Path, job_id: str, wait_seconds: float
) -> int:
    """작업이 끝날 때까지 기다린다 — 추출 진단도 완결 보고가 몫이다."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in _TERMINAL_STATUSES:
            log.out(
                f"작업 {job_id} {record.status} {record.progress_current}/"
                f"{record.progress_total}" + (f" — {record.error}" if record.error else "")
            )
            return 0 if record.status == "succeeded" else 1
        time.sleep(JOB_POLL_SECONDS)
    log.warn(f"작업 {job_id}가 아직 끝나지 않았다 — media clips로 확인하세요.")
    return 1


# ── A4 — 의미 분석(오프라인 재생만, 실제 호출은 사람 승인 기록으로) ────────────


def cmd_analyze(args: argparse.Namespace) -> int:
    """`media analyze` — 견적 인쇄 후 오프라인 재생 분석 실행(유료 호출 없음).

    어떤 플래그도 실제 호출을 켜지 않는다: 이 명령이 만드는 실행의 근원은 언제나
    `replay`다(§14.3). 없는 픽스처는 실패로 기록될 뿐 지어내지 않는다.
    """
    service = _service()
    workspace_root = Path(args.workspace)
    try:
        status = service.open_workspace(workspace_root)
        estimates = service.analysis_estimate(Path(status.workspace_root), asset_ids=args.sources)
        model = service.settings(Path(status.workspace_root)).models.luna_model
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    _print_analysis_estimates(estimates, model)
    if args.estimate_only:
        return 0
    try:
        handle = service.start_analysis(
            Path(status.workspace_root),
            asset_ids=args.sources,
            mode="replay",
            fixtures_dir=args.fixtures,
        )
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    log.out(
        f"오프라인 재생 분석 작업 {handle.job_id} 제출"
        + (" (기존 활성 작업에 다시 붙음)" if handle.reattached else "")
        + " — 없는 픽스처는 실패로 기록된다(지어내지 않는다)."
    )
    outcome = _await_job(service, Path(status.workspace_root), handle.job_id, args.wait_seconds)
    summary = service.analysis_overview(Path(status.workspace_root))
    if summary is not None:
        revision = summary.revision
        log.out(
            f"분석 리비전 {revision.revision_id} · 모드 {revision.mode} · 상태"
            f" {revision.status} · 원본 {revision.source_done}/{revision.source_total}"
        )
        for result in summary.results:
            note = f" — {result.error}" if result.error else ""
            log.out(
                f"  {result.source_order:03d} {result.source_name} · {result.status} ·"
                f" 발화 {result.speech_status or '-'} · 창 {result.window_count} ·"
                f" 조각 {result.segment_count} · 증거 {result.execution_mode or '-'}{note}"
            )
    if outcome != 0:
        return outcome
    if args.plan:
        try:
            snapshot = service.plan_segmentation_from_analysis(Path(status.workspace_root))
        except raw_service.RawWorkspaceError as error:
            log.error(str(error))
            return 1
        log.out(
            f"분석 결과로 분할 계획 {snapshot.revision_id} · 근거 {snapshot.basis} ·"
            f" 조각 {len(snapshot.entries)}개 — `media extract`로 추출한다"
        )
    return 0


def _print_analysis_estimates(estimates: Sequence[AnalysisEstimate], model: str) -> None:
    """견적 표 — 의미 분석 모델의 이름으로 요청 수를 말한다(Luna 문구는 W-14 이전 그대로)."""
    if analysis_provider(model) == ANALYSIS_PROVIDER_ANTHROPIC:
        label = analysis_model_label(model)
        log.out(
            f"** 분석 견적(로컬 — 실제 호출 없음) — 의미 분석 {label}:"
            " 공식 단가 스냅샷 기준 보수적 상한(청구서가 아니다)"
        )
    else:
        label = "Luna"
        log.out("** 분석 견적(로컬 — 실제 호출 없음) — Luna 단가는 미확인이라 금액을 만들지 않는다")
    for item in estimates:
        audio = (
            f"있음(청크 {item.asr_chunk_count}, {item.asr_seconds}초)" if item.has_audio else "없음"
        )
        ceiling = f" · 상한 ${item.ceiling_usd}" if item.ceiling_usd is not None else ""
        log.out(
            f"  {item.source_order:03d} {item.source_name} · 창 {item.window_count} ·"
            f" 프레임 상한 {item.frame_sample_count} · 오디오 {audio} ·"
            f" {label} 요청 {item.luna_request_count}{ceiling}"
        )
        if "지원 밖" in item.note:
            log.warn(f"    {item.note}")


# ── A5/A6·A7 — 검토·자막 진단(로컬만) ─────────────────────────────────────────


def cmd_review(args: argparse.Namespace) -> int:
    """`media review` — 검토 리비전 생성·인쇄(순수 조회, 유료 호출 0회).

    백엔드 모듈(W3 review_workspace)이 없는 빌드에서는 정직한 오류로 끝난다 —
    조용한 대체·빈 성공이 없다.
    """
    service = _service()
    workspace_root = Path(args.workspace)
    try:
        status = service.open_workspace(workspace_root)
        root = Path(status.workspace_root)
        facade = service.review_workspace_facade(root)
        if args.init:
            facade.initialize_review()
            log.out("검토 기본 리비전을 만들었다 — 최신 계획·분석 증거에서.")
        loaded = facade.load_head()
    except (raw_service.RawWorkspaceError, raw_service.ReviewError) as error:
        log.error(str(error))
        return 1
    revision = loaded.revision
    log.out(f"검토 헤드 리비전 {revision.revision_id} · 조각 {len(revision.segments)}개(시간순)")
    for segment in revision.segments:
        log.out(
            f"  {segment.segment_id} · 원본 순번"
            f" {segment.source_order} · 발화"
            f" {segment.speech_status} · 선택"
            f" {segment.selection}"
        )
    for source_id, reason in loaded.stale_sources:
        log.warn(f"낡은 원본 {source_id} — {reason}")
    return 0


def cmd_caption(args: argparse.Namespace) -> int:
    """`media caption` — 최종 영상 등록·실행 추정 인쇄(로컬만).

    실제 자막 실행은 브라우저 화면의 확인란 승인 경로뿐이다 — 이 명령은 등록과
    추정(측정)까지만 한다. 백엔드(W2 caption_workspace)가 없으면 정직하게 끝난다.
    """
    service = _service()
    workspace_root = Path(args.workspace)
    path = Path(args.path).expanduser()
    if not path.is_absolute():
        log.error("최종 영상 경로는 절대 경로여야 한다")
        return 1
    try:
        status = service.open_workspace(workspace_root)
        root = Path(status.workspace_root)
        workspace = service.caption_workspace(root)
        registration = workspace.register_final_video(
            path, source_id=args.source_id, audio_ordinal=args.audio_ordinal
        )
        estimate = workspace.estimate(registration, run_id=args.run)
    except (
        raw_service.RawWorkspaceError,
        raw_service.CaptionWorkspaceError,
        raw_service.FinalVideoError,
    ) as error:
        log.error(str(error))
        return 1
    log.out(
        f"최종 영상 등록 리비전 {registration.final_video_revision}"
        f" · 상태 {registration.status}"
        + (f" · 오디오 서수 {registration.audio_ordinal}" if registration.ready else "")
    )
    log.out(
        f"실행 {args.run} 추정 — 청크 {estimate.chunk_count}"
        f" · 샘플 {estimate.sample_count} · 문구 모델 {estimate.text_model}"
        f" · 시간 모델 {estimate.timing_model} · 예상 비용 ${estimate.estimated_cost_usd}"
        " · 실제 실행은 브라우저 승인 경로에서만 연다(§14.3)"
    )
    return 0


def cmd_finalcut(args: argparse.Namespace) -> int:
    """`media finalcut` — 전체 타임라인 FCPXML과 리포트를 새로 쓴다(D56, 로컬만).

    쓰기만 한다: Final Cut을 열지 않는다(여는 것은 브라우저 화면의 몫이다). 원본을 다시
    해시하거나 복사하지 않으며, 바뀐 원본은 이름을 대고 거부한다.
    """
    service = _service()
    try:
        status = service.open_workspace(Path(args.workspace))
        path = service.export_finalcut_timeline(Path(status.workspace_root))
    except raw_service.RawWorkspaceError as error:
        log.error(str(error))
        return 1
    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    counts = report["counts"]
    captions = counts["captions"]
    log.out(f"Final Cut 타임라인을 썼다: {path}")
    log.out(f"리포트: {path.with_suffix('.json')}")
    log.out(
        f"원본 {counts['sources']}개(분석 {counts['analysed']})"
        f" · 스파인 클립 {counts['spine_clips']}개"
        f" · 꺼진 클립 {counts['disabled_clips']}개"
        f" · 미검토 제외 제안 {counts['unreviewed_clips']}개"
        f" · 캡션 {captions['placed']}개(조각 {captions['pieces']})"
    )
    for row in report["not_exported"]:
        log.out(f"넣지 않은 원본: {row['name'] or row['asset_id']} — {row['reason']}")
    return 0
