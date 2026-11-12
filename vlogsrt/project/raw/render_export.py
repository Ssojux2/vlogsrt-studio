"""추천만 이어붙인 초안 영상 + SRT + 보고서 내보내기(W-44, D100 §5b).

순수 계획(`render_plan`)이 무엇을 남기고 빼는지 정하고, 이 모듈은 그 재료를 모으고(원본 확인 ·
시계 · 등급 · 검토 머리 · 원본 자막) 렌더러(`preprocess.render_part`·`concat_parts`)에 넘겨
`<작업공간>/export/<UTC 시각>/`에 셋을 낸다 — `draft.mp4` · `draft.srt` · `render_report.json`.

- **원본은 읽기만 한다.** 열어 재기만 하고(ffprobe·ffmpeg 입력), 재는 동안과 렌더하는 동안 크기·
  mtime·inode가 바뀌면 거부한다(`finalcut_export`와 같은 확인). 쓰는 곳은 `export/<시각>/`과 그 안의
  스테이징뿐이고, 이미 있는 이름은 덮지 않는다(`check_render_export_writes_only_export`).
- **초안에서 영상을 빼는 것은 파생 초안의 일이다(D35·규칙 9).** 원본·작업공간 기록은 그대로이고, 뺀
  구간은 모두 보고서의 `dropped`에 까닭·검토 상태와 함께 남는다. 확인하지 않은 제외 제안(빨강)도
  **막지 않는다**(사용자 결정 2026-10-27) — 경고와 보고서의 `unreviewed_red_dropped`로 알린다. D94의
  Final Cut 거부와 다른 선택이다: 그쪽은 되살릴 수 없는 프로젝트 손실, 여기는 다시 만들 수
  있는 초안이다.
- 프레임 격자를 증명하지 못한 원본(VFR · map 격자)은 계획이 통째로 빼고 보고서의
  `unsupported`에 남는다. 렌더 판정(`render_part_failures`)이 거부한 원본도 같다 — 초안 전체를
  막지 않고 빼서 까닭과 함께 알린다(D113). 원본을 읽지 못함·재는 동안 바뀜은 여전히 거부다.
- **병렬(D112).** 원본 조사(ffprobe 셋)와 렌더는 로컬 미디어 자리 수만큼 함께 돈다. 렌더는 계획의
  조각을 그대로 쓰지 않고 긴 조각을 `piece_max_seconds` 이하의 토막으로 나눠 함께 인코딩한 뒤 한
  번에 잇는다 — 계획·SRT·보고서의 `parts`는 그대로이고 나뉜 것은 렌더 토막뿐이다.
"""

from __future__ import annotations

import contextvars
import itertools
import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from vlogsrt import log, preprocess
from vlogsrt.oscompat import publish_exclusive
from vlogsrt.project.raw import finalcut_export, media_refs, render_plan
from vlogsrt.project.raw.config import FOLDER_WORKSPACE, RENDER_SETTINGS, RenderSettings
from vlogsrt.project.raw.models import AnalysisSourceResult, RawWorkspaceError, SourceAsset
from vlogsrt.project.raw.review_workspace import human_decided
from vlogsrt.project.raw.segment_review import ReviewRevision, SourceClock
from vlogsrt.project.raw.source_transcript import rational
from vlogsrt.task_runtime import check_cancel

if TYPE_CHECKING:
    from vlogsrt.project.raw.jobs import JobContext
    from vlogsrt.project.raw.service import RawWorkspaceService
    from vlogsrt.project.raw.workspace import RawWorkspace

EXPORT_FOLDER = "export"
"""작업공간의 초안 자리(`WORKSPACE_LAYOUT`) — 옛 작업공간에는 없을 수 있어 쓸 때 만든다."""
REPORT_SCHEMA = "vlogsrt-render-export/v1"
VIDEO_NAME = "draft.mp4"
SRT_NAME = "draft.srt"
"""영상과 같은 이름(줄기)이라 편집기·플레이어가 옆 자막으로 바로 읽는다."""
SRT_AGAIN_PREFIX = "draft-"
"""「SRT만 다시 내보내기」(D115)의 새 이름 줄기 — `draft-<UTC 시각>.srt`(있으면 `_2`…).
`draft.srt`는 덮지 않는다."""
REPORT_NAME = "render_report.json"
STAGING_NAME = "_staging"
"""한 번의 내보내기 안의 스테이징 — 조각·이은 영상·글을 다 만든 뒤에야 제 이름으로 낸다."""
_STAMP = "%Y%m%dT%H%M%SZ"
_T = TypeVar("_T")
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
_FILE_MODE = 0o644


class RenderExportError(RawWorkspaceError):
    """초안을 만들지 않았다 — 어느 원본의 무엇 때문인지 말한다."""


@dataclass(frozen=True, slots=True)
class GatheredSource:
    """원본 하나의 재료 — 렌더러가 다시 재지 않게 잰 값(프로브·시계·소리 패킷)을 함께 든다."""

    asset: SourceAsset
    path: Path
    snapshot: tuple[int, int, int]
    probe: preprocess.RawMediaProbe
    clock: preprocess.RawVideoClock
    audio: tuple[preprocess.AudioPacketExtent, ...]
    source: render_plan.RenderSource


@dataclass(frozen=True, slots=True)
class RenderPiece:
    """렌더 토막 하나 — 계획의 조각 `part_index`의 한 부분(나누지 않았으면 조각 전체)이다.
    `spec.index`는 토막의 차례(스테이징 이름)이고 계획의 조각 번호와 다를 수 있다."""

    part_index: int
    source_id: str
    spec: preprocess.RenderPartSpec


@dataclass(frozen=True, slots=True)
class ExportResult:
    """낸 초안 — 폴더와 세 파일, 브라우저 재생용 미디어 아이디, 계획과 쓴 인코더."""

    folder: Path
    video: Path
    srt: Path
    report: Path
    media_file_id: str
    plan: render_plan.RenderPlan
    encoder: str
    seconds: float


@dataclass(frozen=True, slots=True)
class SrtAgainResult:
    """「SRT만 다시 내보내기」(D115)가 쓴 SRT — 마지막 초안 폴더 안의 새 파일과 자막 수."""

    folder: Path
    srt: Path
    counts: render_plan.CueCounts
    cues: int


@dataclass(frozen=True, slots=True)
class _Scope:
    service: RawWorkspaceService
    root: Path
    workspace: RawWorkspace
    head: ReviewRevision | None
    evidence: Mapping[str, AnalysisSourceResult]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(asset: SourceAsset, reason: str) -> RenderExportError:
    return RenderExportError(f"{asset.source_name}: {reason}")


# ── 모으기 ───────────────────────────────────────────────────────────────


def gather_sources(
    service: RawWorkspaceService, root: Path, context: JobContext | None = None
) -> list[GatheredSource]:
    """원본마다(촬영 시각 순) 재료를 모은다 — 문제는 끝까지 다 모아 한 번에 이름을 대고 거부한다.

    분석 전 원본도 들어간다(계획이 `unanalyzed`로 뺀다). VFR·map 원본과 렌더 판정이 거부한 원본도
    들어간다(계획이 뺀다, D113).
    ffprobe 셋만 자리 수만큼 함께 돌고(D112), 작업공간 기록을 읽는 일은 부른 스레드에서 차례로 한다.
    """
    facade = service.review_workspace_facade(root)
    assets = service.global_order_proposal(root)
    if not assets:
        raise RenderExportError("이어붙일 원본이 없다 — 폴더를 먼저 불러오세요")
    head = finalcut_export._load_head(facade, assets)
    scope = _Scope(service, root, facade.workspace, head, facade.tier_evidence_rows())
    if context is not None:
        context.set_total(len(assets))
    located = [_locate(scope, asset) for asset in assets]
    probed = _run_pool([_probe_job(row) for row in located], "render-probe", context)
    gathered: list[GatheredSource] = []
    problems: list[str] = []
    for asset, row, measured in zip(assets, located, probed, strict=True):
        check_cancel()
        try:
            gathered.append(_gather(scope, asset, row, measured))
        except RawWorkspaceError as error:
            problems.append(str(error))
    if problems:
        listed = " / ".join(problems)
        raise RenderExportError(f"초안을 만들지 않았다 — 원본 {len(problems)}개: {listed}")
    return gathered


_Located = tuple[Path, tuple[int, int, int]] | RawWorkspaceError
_Probed = (
    tuple[
        preprocess.RawMediaProbe,
        preprocess.RawVideoClock,
        tuple[preprocess.AudioPacketExtent, ...],
    ]
    | RawWorkspaceError
    | None
)


def _locate(scope: _Scope, asset: SourceAsset) -> _Located:
    """원본 경로와 재기 전 stat — 작업공간 기록을 읽으므로 부른 스레드에서 한다."""
    try:
        return finalcut_export._export_path(scope.workspace, asset)
    except RawWorkspaceError as error:
        return error


def _probe_job(row: _Located) -> Callable[[], _Probed]:
    """일꾼 하나가 할 ffprobe 셋 — 거부는 값으로 돌려 모든 원본의 문제를 한 번에 댄다."""
    if isinstance(row, RawWorkspaceError):
        return lambda: None
    path = row[0]

    def probe() -> _Probed:
        try:
            # Final Cut 보내기와 같은 앞뒤 대조가 있다 — 같은 stat이면 조사 기억(W-45).
            media = preprocess.probe_raw_media_memo(path)
            clock = preprocess.probe_extraction_clock_memo(path, probe=media)
            return media, clock, preprocess.probe_audio_extents(path)
        except RuntimeError as error:
            return RawWorkspaceError(f"원본을 ffprobe로 읽지 못했다 — {error}")

    return probe


def _gather(scope: _Scope, asset: SourceAsset, row: _Located, measured: _Probed) -> GatheredSource:
    if isinstance(row, RawWorkspaceError):
        raise row
    if not isinstance(measured, tuple):
        raise _refuse(asset, str(measured))
    path, before = row
    probe, clock, audio = measured
    finalcut_export._require_unchanged(asset, path, before)
    grid = _grid_kind(clock)
    refusal = ""
    if grid != render_plan.GRID_MAP:
        failures = preprocess.render_part_failures(probe, clock)
        if failures:
            # 원본 하나가 초안 전체를 막지 않는다 — 계획이 통째로 빼고 까닭을 남긴다(D113).
            refusal = "렌더할 수 없다 — " + "; ".join(failures)
            log.warn(f"{asset.source_name}: {refusal} — 이 원본을 빼고 이어붙인다")
    source = _render_source(scope, asset, probe, clock, grid, refusal)
    return GatheredSource(asset, path, before, probe, clock, audio, source)


def _grid_kind(clock: preprocess.RawVideoClock) -> str:
    """격자 종류 — 증명하지 못한 격자(VFR)도 map과 같이 렌더하지 않는다(D68)."""
    if not clock.uniform_grid:
        return render_plan.GRID_MAP
    return clock.conform_kind


def _render_source(
    scope: _Scope,
    asset: SourceAsset,
    probe: preprocess.RawMediaProbe,
    clock: preprocess.RawVideoClock,
    grid: str,
    refusal: str = "",
) -> render_plan.RenderSource:
    frame = render_plan.FrameClock(
        clock.frame_duration, clock.frame_count, clock.presentation_origin
    )
    width, height = probe.video.display_size
    if grid == render_plan.GRID_MAP or refusal:
        # 통째로 빠질 원본은 검토 머리·자막을 읽지 않는다 — 머리 시계 대조로 거부하지 않게.
        return render_plan.RenderSource(
            asset.asset_id, asset.source_name, frame, width, height, grid, refusal=refusal
        )
    row = scope.evidence.get(asset.asset_id)
    report = scope.service.tier_report(scope.root, row.revision_id, asset.asset_id) if row else None
    return render_plan.RenderSource(
        source_id=asset.asset_id,
        name=asset.source_name,
        clock=frame,
        width=width,
        height=height,
        grid=grid,
        tiers=render_plan.tier_pieces(report) if report is not None else None,
        segments=_marks(scope, asset, frame),
        cues=_cues(scope, asset),
    )


def _marks(
    scope: _Scope, asset: SourceAsset, frame: render_plan.FrameClock
) -> tuple[render_plan.SegmentMark, ...] | None:
    """검토 머리의 사람 표시 — 머리 시계가 지금 원본의 시계와 다르면 거부한다(다시 세울 것)."""
    review = scope.head.clock_for(asset.asset_id) if scope.head is not None else None
    if scope.head is None or review is None:
        return None
    _require_same_clock(asset, review, frame)
    rows = sorted(
        (s for s in scope.head.segments if s.source_id == asset.asset_id),
        key=lambda s: s.start_pts,
    )
    return tuple(render_plan.segment_mark(row, review, decided=human_decided(row)) for row in rows)


def _require_same_clock(
    asset: SourceAsset, review: SourceClock, frame: render_plan.FrameClock
) -> None:
    if review.source_sha256 != asset.content_sha256:
        raise _refuse(asset, "검토 머리가 다른 원본 내용에 묶여 있다 — 검토를 다시 세우세요")
    try:
        held = render_plan.frame_clock(review)
    except render_plan.RenderPlanError as error:
        raise _refuse(asset, str(error)) from error
    if held != frame:
        raise _refuse(
            asset, f"검토 시계 {held}가 지금 원본의 시계 {frame}와 다르다 — 검토를 다시 세우세요"
        )


def _cues(scope: _Scope, asset: SourceAsset) -> tuple[render_plan.SourceCue, ...]:
    return _transcript_cues(scope.service, scope.root, asset)


def _transcript_cues(
    service: RawWorkspaceService, root: Path, asset: SourceAsset
) -> tuple[render_plan.SourceCue, ...]:
    """사람이 고친 최신 원본 자막 — 다른 원본 내용에 묶인 자막은 쓰지 않고 거부한다."""
    document = service.source_transcript(root, asset.asset_id)
    if document is None:
        return ()
    if document.get("source_sha256") != asset.content_sha256:
        raise _refuse(asset, "원본 자막이 다른 원본 내용에 묶여 있다")
    return tuple(
        render_plan.SourceCue(
            str(row["utterance_id"]), rational(row["start"]), rational(row["end"]), str(row["text"])
        )
        for row in document.get("utterances", [])
    )


# ── 계획 ─────────────────────────────────────────────────────────────────


def _build(gathered: Sequence[GatheredSource], settings: RenderSettings) -> render_plan.RenderPlan:
    try:
        plan = render_plan.build_plan([item.source for item in gathered], settings)
    except render_plan.RenderPlanError as error:
        raise RenderExportError(f"초안 계획을 세우지 못했다 — {error}") from error
    if not plan.parts:
        reasons: dict[str, int] = {}
        for row in plan.dropped:
            reasons[row.reason] = reasons.get(row.reason, 0) + 1
        listed = ", ".join(f"{reason} {count}곳" for reason, count in sorted(reasons.items()))
        raise RenderExportError(
            "남길 추천 구간이 없어 초안을 만들지 않았다 — 추천(초록)이 없거나 모두"
            f" {float(settings.min_part_seconds):g}초보다 짧다 (뺀 구간: {listed or '없음'})"
        )
    if plan.unreviewed_red_dropped:
        log.warn(
            f"확인하지 않은 제외 제안 {plan.unreviewed_red_dropped}곳을 빼고 이어붙인다 —"
            f" 막지 않는다(사용자 결정 2026-10-27). 뺀 구간은 {REPORT_NAME}에 남는다"
        )
    return plan


def _encoding(settings: RenderSettings, codec: str) -> preprocess.RenderEncoding:
    return preprocess.RenderEncoding(
        video_codec=codec,
        crf=settings.crf,
        preset=settings.preset,
        hardware_bits_per_pixel=settings.hardware_bits_per_pixel,
        keyframe_seconds=settings.keyframe_seconds,
        audio_codec=settings.audio_codec,
        audio_rate=settings.audio_rate,
        audio_channels=settings.audio_channels,
        audio_bitrate_kbps=settings.audio_bitrate_kbps,
        pad_color=settings.pad_color,
        track_timescale_min=settings.track_timescale_min,
        timeout_floor_seconds=settings.timeout_floor_seconds,
        timeout_media_ratio=settings.timeout_media_ratio,
        priority=settings.priority,
        # CPU 인코드는 로컬 미디어 자리 몫의 스레드만(W-45, D100 §6) — 조각 일꾼이 그 자리를 다툰다.
        threads=FOLDER_WORKSPACE.local_media.encode_threads(os.cpu_count()),
    )


def _target(plan: render_plan.RenderPlan) -> preprocess.RenderTarget:
    sequence = plan.sequence
    assert sequence is not None  # 조각이 있으면 시퀀스가 있다(_build가 확인했다)
    return preprocess.RenderTarget(sequence.width, sequence.height, sequence.frame_duration)


# ── 렌더 ─────────────────────────────────────────────────────────────────


def render_pieces(
    parts: Sequence[render_plan.RenderPart], frame_duration: Fraction, max_seconds: Fraction
) -> tuple[RenderPiece, ...]:
    """계획의 조각 → 렌더 토막(D112). 순수하다 — 계획은 바꾸지 않는다.

    원본 프레임과 출력 프레임이 1:1인 조각(레이트 변환이 없고 D71 누적 맞춤이 한 장도 더하거나
    빼지 않은 조각)만 `max_seconds` 이하의 고른 토막으로 나눈다 — 토막 경계가 원본 프레임 경계라
    출력 프레임 수의 합이 조각과 같다. 그 밖의 조각과 `max_seconds = 0`이면 조각 그대로다.
    """
    limit = math.floor(max_seconds / frame_duration) if max_seconds > 0 else 0
    pieces: list[RenderPiece] = []
    for part in parts:
        frames = part.output_frames
        one_to_one = not part.fps_convert and frames == part.end_frame - part.start_frame
        count = math.ceil(frames / limit) if one_to_one and limit > 0 else 1
        for k in range(count):
            index = len(pieces)
            if count == 1:
                spec = preprocess.RenderPartSpec(
                    index, part.start_frame, part.end_frame, part.output_frames
                )
            else:
                lo, hi = frames * k // count, frames * (k + 1) // count
                spec = preprocess.RenderPartSpec(
                    index, part.start_frame + lo, part.start_frame + hi, hi - lo
                )
            pieces.append(RenderPiece(part.index, part.source_id, spec))
    return tuple(pieces)


def _run_pool(jobs: Sequence[Callable[[], _T]], name: str, context: JobContext | None) -> list[_T]:
    """일을 로컬 미디어 자리 수만큼 함께 돌려 차례대로 돌려준다 — 끝날 때마다 진행을 하나 올리고,
    취소 범위는 일꾼마다 복사한 문맥으로 따라간다. 하나가 실패하면 기다리는 일은 시작하지 않는다."""
    workers = FOLDER_WORKSPACE.local_media.limit(os.cpu_count())
    done: dict[int, _T] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name) as pool:
        futures: dict[Future[_T], int] = {
            pool.submit(contextvars.copy_context().run, job): order
            for order, job in enumerate(jobs)
        }
        try:
            for future in as_completed(futures):
                done[futures[future]] = future.result()
                if context is not None:
                    context.advance()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return [done[order] for order in range(len(jobs))]


def _render_one(
    item: GatheredSource,
    piece: RenderPiece,
    target: preprocess.RenderTarget,
    encoding: preprocess.RenderEncoding,
    staging: Path,
) -> Path:
    try:
        with media_refs.local_media_slot():
            return preprocess.render_part(
                item.path, piece.spec, target, encoding, staging,
                probe=item.probe, clock=item.clock, audio_extents=item.audio,
            )  # fmt: skip
    except RuntimeError as error:
        where = f"조각 {piece.part_index}(토막 {piece.spec.index})"
        raise _refuse(item.asset, f"{where}을 렌더하지 못했다 — {error}") from error


def _render_parts(
    pieces: Sequence[RenderPiece],
    plan: render_plan.RenderPlan,
    gathered: Sequence[GatheredSource],
    encoding: preprocess.RenderEncoding,
    staging: Path,
    context: JobContext | None,
) -> list[Path]:
    """토막을 자리 수만큼 함께 렌더한다 — 결과는 토막 차례(= 출력 차례)다."""
    by_id = {item.asset.asset_id: item for item in gathered}
    target = _target(plan)

    def job(piece: RenderPiece) -> Callable[[], Path]:
        return lambda: _render_one(by_id[piece.source_id], piece, target, encoding, staging)

    return _run_pool([job(piece) for piece in pieces], "render-part", context)


def _produce(
    plan: render_plan.RenderPlan,
    gathered: Sequence[GatheredSource],
    encoding: preprocess.RenderEncoding,
    staging: Path,
    context: JobContext | None,
) -> tuple[preprocess.RenderedDraft, dict[str, float], dict[str, Any]]:
    """토막 → 원본이 그대로인지 다시 확인 → 이어붙이기. (다시 잰 초안, 걸린 시간, 병렬 사실)."""
    target = _target(plan)
    pieces = render_pieces(plan.parts, target.frame_duration, plan.settings.piece_max_seconds)
    if context is not None:
        context.set_total(len(pieces) + 1)
        context.set_progress(0)
    started = time.monotonic()
    parts = _render_parts(pieces, plan, gathered, encoding, staging, context)
    rendered = time.monotonic()
    for item in gathered:
        finalcut_export._require_unchanged(item.asset, item.path, item.snapshot)
    check_cancel()
    draft = preprocess.concat_parts(
        parts, staging / VIDEO_NAME, target, encoding, expected_frames=plan.total_frames
    )
    if context is not None:
        context.advance()
    for part in parts:
        part.unlink()
    finished = time.monotonic()
    timings = {"render_seconds": rendered - started, "concat_seconds": finished - rendered}
    return draft, timings, _parallel_facts(plan, pieces)


def _parallel_facts(plan: render_plan.RenderPlan, pieces: Sequence[RenderPiece]) -> dict[str, Any]:
    """보고서의 `parallel` 칸 — 몇 토막을 몇 자리로 함께 렌더했나(D112). 표시만 한다."""
    return {
        "workers": FOLDER_WORKSPACE.local_media.limit(os.cpu_count()),
        "pieces": len(pieces),
        "piece_max_seconds": _rational(plan.settings.piece_max_seconds),
    }


# ── 쓰기 ─────────────────────────────────────────────────────────────────


def _reserve_folder(base: Path, moment: datetime) -> Path:
    """새 시각 폴더 하나를 잡는다 — 있으면 덮지 않고 다음 번호(`_2`, `_3`…)."""
    base.mkdir(parents=True, exist_ok=True)
    stamp = moment.astimezone(UTC).strftime(_STAMP)
    for attempt in itertools.count(1):
        folder = base / (stamp if attempt == 1 else f"{stamp}_{attempt}")
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return folder
    raise AssertionError("unreachable")


def _write_new(path: Path, payload: bytes) -> None:
    """새 파일로만 쓴다 — 있으면 `FileExistsError`(덮어쓰지 않는다)."""
    descriptor = os.open(path, _NEW_FILE, _FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _sweep(directory: Path) -> None:
    """이 내보내기가 만든 폴더를 걷는다 — 파일만 든 평평한 폴더다(하위 폴더가 있으면 남긴다)."""
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            _sweep(entry)
        else:
            entry.unlink(missing_ok=True)
    directory.rmdir()


def _publish(folder: Path, staging: Path) -> None:
    """스테이징의 셋을 제 이름으로 — 보고서가 마지막이다(보고서가 있으면 다 낸 초안이다)."""
    for name in (VIDEO_NAME, SRT_NAME, REPORT_NAME):
        publish_exclusive(staging / name, folder / name)
        (staging / name).unlink()


def _rational(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _source_facts(item: GatheredSource) -> dict[str, Any]:
    source = item.source
    return {
        "source_id": source.source_id,
        "name": source.name,
        "path": str(item.path),
        "grid": source.grid,
        "analysed": source.tiers is not None,
        "reviewed": source.segments is not None,
        "cues": len(source.cues),
        "width": source.width,
        "height": source.height,
        "frame_duration": _rational(source.clock.frame_duration),
        "rotation": item.probe.video.rotation_degrees,
        "color_transfer": item.probe.video.color_transfer,
        "audio_tracks": len(item.audio),
    }


def _report(
    plan: render_plan.RenderPlan,
    gathered: Sequence[GatheredSource],
    draft: preprocess.RenderedDraft,
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    """`render_report.json` — 계획(남긴 조각·뺀 구간·자막 수·포맷)에 인코더·버전·시간·경로를
    더한다."""
    return {
        "schema": REPORT_SCHEMA,
        **render_plan.report_document(plan),
        "sources": [_source_facts(item) for item in gathered],
        "verified": {
            "frames": draft.frame_count,
            "frame_duration": _rational(draft.frame_duration),
            "duration": _rational(draft.duration),
            "width": draft.width,
            "height": draft.height,
            "video_codec": draft.video_codec,
            "audio_codec": draft.audio_codec,
        },
        **facts,
    }


def _write_texts(
    staging: Path,
    plan: render_plan.RenderPlan,
    gathered: Sequence[GatheredSource],
    draft: preprocess.RenderedDraft,
    facts: Mapping[str, Any],
) -> None:
    """SRT(UTF-8 · BOM 없음 · LF — 운영체제와 무관하게 같은 바이트)와 보고서를 스테이징에 쓴다."""
    _write_new(staging / SRT_NAME, render_plan.srt_text(plan).encode("utf-8"))
    document = _report(plan, gathered, draft, facts)
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_new(staging / REPORT_NAME, text.encode("utf-8"))


# ── 들머리 ───────────────────────────────────────────────────────────────


def export_draft(
    service: RawWorkspaceService,
    root: Path,
    context: JobContext | None = None,
    *,
    now: Callable[[], datetime] = _utc_now,
    settings: RenderSettings = RENDER_SETTINGS,
) -> ExportResult:
    """추천만 이어붙인 초안 — 새 시각 폴더에 영상·SRT·보고서. 실패·취소면 폴더를 남기지 않는다."""
    started = time.monotonic()
    finalcut_export._require_no_import(service, root)
    gathered = gather_sources(service, root, context)
    plan = _build(gathered, settings)
    encoder = preprocess.pick_video_encoder(settings.video_codecs)
    moment = now()
    workspace: RawWorkspace = service.review_workspace_facade(root).workspace
    folder = _reserve_folder(workspace.root / EXPORT_FOLDER, moment)
    staging = folder / STAGING_NAME
    gathered_at = time.monotonic()
    try:
        staging.mkdir()
        draft, timings, parallel = _produce(
            plan, gathered, _encoding(settings, encoder), staging, context
        )
        facts = _facts(folder, moment, encoder, gathered_at - started, timings, started)
        facts["parallel"] = parallel
        _write_texts(staging, plan, gathered, draft, facts)
        _publish(folder, staging)
        _sweep(staging)
    except BaseException:
        _sweep(folder)
        raise
    registered = service.register_media(root, folder / VIDEO_NAME, "artifact")
    log.info(f"초안을 냈다: {folder / VIDEO_NAME} ({float(plan.total_seconds):.1f}초, {encoder})")
    seconds = time.monotonic() - started
    return _result(folder, registered.file_id, plan, encoder, seconds)


def _result(
    folder: Path,
    file_id: str,
    plan: render_plan.RenderPlan,
    encoder: str,
    seconds: float,
) -> ExportResult:
    return ExportResult(
        folder=folder,
        video=folder / VIDEO_NAME,
        srt=folder / SRT_NAME,
        report=folder / REPORT_NAME,
        media_file_id=file_id,
        plan=plan,
        encoder=encoder,
        seconds=seconds,
    )


def _facts(
    folder: Path,
    moment: datetime,
    encoder: str,
    gather_seconds: float,
    timings: Mapping[str, float],
    started: float,
) -> dict[str, Any]:
    return {
        "created_at": moment.astimezone(UTC).isoformat(),
        "encoder": encoder,
        "ffmpeg_version": preprocess.ffmpeg_version(),
        "timings": {
            "gather_seconds": round(gather_seconds, 3),
            **{name: round(value, 3) for name, value in timings.items()},
            "total_seconds": round(time.monotonic() - started, 3),
        },
        "outputs": {
            "folder": str(folder),
            "video": VIDEO_NAME,
            "srt": SRT_NAME,
            "report": REPORT_NAME,
        },
    }


# ── SRT만 다시 ────────────────────────────────────────────────────────────


def export_srt_again(
    service: RawWorkspaceService,
    root: Path,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> SrtAgainResult:
    """마지막 초안의 조각(보고서 `parts`)에 지금 원본 자막을 다시 얹어 SRT만 새로 쓴다(D115).

    영상은 다시 만들지 않는다 — 계획을 다시 세우지 않고 보고서의 조각을 그대로 쓰므로 등급·검토가
    그 뒤에 바뀌어도 `draft.mp4`와 시각이 맞는다. 새 파일(`draft-<시각>.srt`)로만 쓰고 `draft.srt`는
    덮지 않는다. 조각의 원본이 사라졌거나 자막이 다른 원본 내용에 묶였으면 쓰지 않고 거부한다.
    """
    workspace: RawWorkspace = service.review_workspace_facade(root).workspace
    folder = _latest_folder(workspace)
    if folder is None:
        raise RenderExportError(
            "아직 내보낸 초안이 없다 — 「추천만 이어붙여 내보내기」를 먼저 하세요"
        )
    report = json.loads((folder / REPORT_NAME).read_text(encoding="utf-8"))
    try:
        parts = tuple(render_plan.part_from_document(row) for row in report["parts"])
        total = render_plan.rational_from_document(report["total_seconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise RenderExportError(f"{folder.name}의 {REPORT_NAME}을 읽지 못했다 — {error}") from error
    transcripts = _part_transcripts(service, root, parts)
    try:
        cues, counts = render_plan.project_cues(transcripts, parts, total)
    except render_plan.RenderPlanError as error:
        raise RenderExportError(f"자막을 다시 얹지 못했다 — {error}") from error
    target = _write_srt_again(folder, now(), render_plan.cues_srt_text(cues).encode("utf-8"))
    log.info(f"SRT만 다시 냈다: {target} (자막 {len(cues)}장, 늘린 장 {counts.lengthened})")
    return SrtAgainResult(folder, target, counts, len(cues))


def _part_transcripts(
    service: RawWorkspaceService, root: Path, parts: Sequence[render_plan.RenderPart]
) -> dict[str, tuple[render_plan.SourceCue, ...]]:
    """조각의 원본마다(조각 차례 = 촬영 시각 순) 지금 원본 자막 — 원본이 없으면 거부한다."""
    assets = {asset.asset_id: asset for asset in service.list_source_assets(root)}
    transcripts: dict[str, tuple[render_plan.SourceCue, ...]] = {}
    for source_id in dict.fromkeys(part.source_id for part in parts):
        asset = assets.get(source_id)
        if asset is None:
            raise RenderExportError(
                f"초안의 원본 {source_id}이 작업공간에 없다 — 초안을 다시 만드세요"
            )
        transcripts[source_id] = _transcript_cues(service, root, asset)
    return transcripts


def _write_srt_again(folder: Path, moment: datetime, payload: bytes) -> Path:
    """`draft-<시각>.srt` 새 파일 하나 — 있으면 덮지 않고 다음 번호. 쓰다 실패하면 걷는다."""
    stamp = moment.astimezone(UTC).strftime(_STAMP)
    for attempt in itertools.count(1):
        stem = stamp if attempt == 1 else f"{stamp}_{attempt}"
        target = folder / f"{SRT_AGAIN_PREFIX}{stem}.srt"
        try:
            _write_new(target, payload)
        except FileExistsError:
            continue
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target
    raise AssertionError("unreachable")


# ── 읽기 ─────────────────────────────────────────────────────────────────


def _stamp_order(folder: Path) -> tuple[str, int]:
    stamp, _, attempt = folder.name.partition("_")
    return stamp, int(attempt) if attempt.isdigit() else 1


def latest(workspace: RawWorkspace) -> dict[str, Any] | None:
    """가장 최근에 다 낸 초안(보고서가 있는 폴더)의 경로와 요약 — 화면의 결과 카드 재료. 읽기만
    한다."""
    folder = _latest_folder(workspace)
    if folder is None:
        return None
    report = json.loads((folder / REPORT_NAME).read_text(encoding="utf-8"))
    return _summary(workspace, folder, report)


def _latest_folder(workspace: RawWorkspace) -> Path | None:
    """가장 최근에 다 낸 초안 폴더(보고서가 있다) — 없으면 None."""
    base = workspace.root / EXPORT_FOLDER
    if not base.is_dir():
        return None
    folders = sorted((p for p in base.iterdir() if p.is_dir()), key=_stamp_order, reverse=True)
    return next((folder for folder in folders if (folder / REPORT_NAME).is_file()), None)


def newest_srt(folder: Path) -> Path:
    """초안 폴더의 가장 새 SRT — 「SRT만 다시 내보내기」로 쓴 것이 있으면 그 가장 늦은 것(D115)."""
    again = [
        path
        for path in folder.glob(f"{SRT_AGAIN_PREFIX}*.srt")
        if path.is_file() and path.stem[len(SRT_AGAIN_PREFIX) :][:1].isdigit()
    ]
    if not again:
        return folder / SRT_NAME
    return max(again, key=lambda path: _stamp_order(Path(path.stem[len(SRT_AGAIN_PREFIX) :])))


def _summary(workspace: RawWorkspace, folder: Path, report: Mapping[str, Any]) -> dict[str, Any]:
    video = folder / VIDEO_NAME
    srt = newest_srt(folder)
    relative = video.relative_to(workspace.root).as_posix()
    media = {row.relative_path: row.file_id for row in workspace.list_media("artifact")}
    total = report.get("total_seconds") or {"num": 0, "den": 1}
    return {
        "folder": str(folder),
        "video": str(video),
        "srt": str(srt),
        "srt_again": srt.name != SRT_NAME,
        "report": str(folder / REPORT_NAME),
        "media_file_id": media.get(relative),
        "created_at": report.get("created_at"),
        "encoder": report.get("encoder"),
        "total_seconds": float(Fraction(total["num"], total["den"])),
        "parts": len(report.get("parts", [])),
        "dropped": len(report.get("dropped", [])),
        "unreviewed_red_dropped": report.get("unreviewed_red_dropped", 0),
        "unsupported": [
            row.get("name")
            for row in report.get("unsupported", [])
            if row.get("reason") != render_plan.DROP_UNRENDERABLE
        ],
        "unrenderable": [
            f"{row.get('name')} — {row.get('detail')}"
            for row in report.get("unsupported", [])
            if row.get("reason") == render_plan.DROP_UNRENDERABLE
        ],
        "cues": report.get("cue_counts", {}),
    }
