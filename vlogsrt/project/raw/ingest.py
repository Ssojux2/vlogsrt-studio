"""SD 카드·폴더 → 작업공간 가져오기 — 발견·조사·확정·검증 복사(계획 §5).

경계를 지키는 방법이 이 파일의 내용이다:

- **ffprobe는 직접 부르지 않는다.** 실제 조사는 `vlogsrt.preprocess`의 공개
  함수들뿐이고, 이 모듈은 그것을 `MediaInspect` 주입점 뒤에 둔다 — 시험은 가짜
  조사자로, 실사용은 `ffprobe_media_inspector`로 갈아끼운다.
- **촬영 시각을 지어내지 않는다.** 조사가 알려주지 않으면 `capture_time`은
  `None`(미조사)이다. 파일 수정 시각(mtime)을 촬영 시각으로 바꿔 쓰지 않는다 —
  mtime·inode는 어디까지나 **변경 검출**용 스냅숏이다(§6.2).
- **원본은 읽기만 한다.** 복사·검증·등록이 모두 원본을 다시 쓰는 길은 없다.
- **보호 이름은 stat보다 먼저 가지치기한다.** `.env`·자격 증명 철자는 경로
  표기만 보고 버린다 — 조회조차 일으키지 않는다(코디네이터 재결).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat as stat_module
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from vlogsrt import preprocess
from vlogsrt.project.raw import source_order
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext
from vlogsrt.project.raw.models import (
    MEDIA_SUFFIXES,
    ORDER_BASIS_USER_CONFIRMED,
    IngestCopyError,
    IngestError,
    InsufficientSpaceError,
    MediaNotAllowedError,
    MediaRegistration,
    NoIngestCandidatesError,
    RawWorkspaceError,
    SourceAsset,
    SourceAssetDraft,
    SourceChangedError,
    SourceNestingError,
    SourceOrderRevision,
)
from vlogsrt.project.raw.workspace import RawWorkspace

__all__ = [
    "COPY_CHUNK_BYTES",
    "InspectionError",
    "IngestConfirmation",
    "IngestProposal",
    "MediaInspect",
    "PARTIAL_SUFFIX",
    "PROTECTED_SOURCE_NAMES",
    "RejectedSource",
    "SourceCandidate",
    "SourceInspection",
    "SourceScan",
    "SpaceEstimate",
    "confirm_ingest",
    "ffprobe_media_inspector",
    "ingest_fingerprint",
    "reconfirm_ingest",
    "run_ingest_copy",
    "scan_sources",
    "space_estimate",
]

PARTIAL_SUFFIX = ".partial"
"""복사 중 파일의 확장자 — 최종 이름으로 바꾸기 전까지 이 상태로만 존재한다."""

COPY_CHUNK_BYTES = 1 << 20
"""한 번에 읽는 복사 조각 — 통째로 메모리에 올리지 않는다(§4.3)."""

SPACE_HEADROOM = 1.05
"""공간 검사의 안전 여유 — 남은 공간이 이 배수를 못 넘기면 복사를 시작하지 않는다."""

PROTECTED_SOURCE_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.example",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "secrets.json",
    }
)
"""어휘로 가지치기하는 보호 철자 — 점으로 시작하는 이름 규칙과 함께, stat 이전에
버린다. 미디어 확장자가 아니어도 이 목록은 자격 정보 탐색의 첫걸음을 막는다."""

INGEST_SETTINGS_DIGEST = "raw-ingest/1:partial+sha256-reread+stat-snapshot"
"""작업 지문에 박는 가져오기 설정 요약 — 복사·검증 규칙이 바뀌면 지문이 달라진다.
임계값(여유 배수·조각 크기)이 설정(`raw/config.py`)으로 옮겨가면 그 값이 여기
자리를 대신한다(코디네이터 ACK — 지문은 관련 설정을 포함한다)."""

CLIP_SPACE_NOTE = "조각 생성 추가 공간은 A3 단계에서 별도로 안내한다"


class InspectionError(IngestError):
    """후보 조사 실패 — 후보에서 빠지되, 사유와 함께 눈에 보인다(조용한 제외 금지)."""


# ── 조사자 계약 — 주입형. 실제 구현은 preprocess 공개 함수만 쓴다 ──────────────


@dataclass(frozen=True, slots=True)
class SourceInspection:
    """실제 미디어 조사로만 채우는 값 — 모르면 `None` 그대로다(추측 금지).

    `capture_time`은 신뢰할 촬영 시각 근거다. A2의 ffprobe 조사자는 이 값을
    알려주지 않으므로 항상 `None`(미조사)이다 — 더 풍부한 메타데이터가
    묶이면 이 계약 그대로 값을 채운다(코디네이터 ACK).
    """

    duration_seconds: float | None
    width: int | None
    height: int | None
    audio_streams: int | None
    capture_time: str | None


MediaInspect = Callable[[Path], SourceInspection]
"""조사자 — 경로 하나를 받아 조사값을 돌려준다. 실패는 예외로 알린다."""


def ffprobe_media_inspector(path: Path) -> SourceInspection:
    """실제 조사자 — ffprobe를 `vlogsrt.preprocess` 공개 함수로만 만난다.

    비디오 스트림이 없거나 깨진 파일은 `RuntimeError`로 죽는다(preprocess 계약).
    그 예외를 스캔이 `InspectionError` 사유로 바꿔 기록한다.
    """
    duration = preprocess.probe_duration(path)
    summary = preprocess.probe_media_summary(path)
    return SourceInspection(
        duration_seconds=duration,
        width=summary.width,
        height=summary.height,
        audio_streams=summary.audio_streams,
        capture_time=None,  # ffprobe는 신뢰할 촬영 시각을 주지 않는다 — 미조사로 남긴다
    )


# ── 스캔 — 발견·조사. 아직 아무 것도 쓰지 않는다 ──────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """가져오기 후보 하나 — 조사까지 마친 상태. 확정 전에는 디스크에 아무 변화가 없다."""

    source_root: str
    source_relative_path: str
    source_name: str
    size_bytes: int
    mtime_ns: int
    inode: int
    inspection: SourceInspection

    @property
    def capture_time(self) -> str | None:
        """촬영 시각 — 조사값의 것만. 후보와 자산이 같은 정렬 규칙을 지나간다."""
        return self.inspection.capture_time


@dataclass(frozen=True, slots=True)
class RejectedSource:
    """후보에서 빠진 항목과 그 사유 — 조용히 사라지지 않고 이름을 남긴다."""

    source_relative_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class SourceScan:
    """한 번의 발견 결과 — 후보와 제외 항목 전부."""

    source_root: str
    candidates: tuple[SourceCandidate, ...]
    rejected: tuple[RejectedSource, ...]

    @property
    def total_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class SpaceEstimate:
    """공간 견적 — 복사 공간만 판정한다(§5.2: 조각 공간은 따로 안내)."""

    copy_bytes: int
    free_bytes: int
    required_bytes: int
    sufficient: bool
    note: str


@dataclass(frozen=True, slots=True)
class IngestProposal:
    """스캔 + 제안 순서 + 공간 견적 — 사람이 확인하기 전의 읽기전용 보기."""

    scan: SourceScan
    order: tuple[SourceCandidate, ...]
    space: SpaceEstimate


def scan_sources(
    source_root: Path,
    workspace_root: Path,
    inspect: MediaInspect = ffprobe_media_inspector,
) -> IngestProposal:
    """원본 루트를 발견·조사해 제안을 만든다 — 이 단계는 읽기만 한다.

    심볼릭 링크 디렉터리는 내려가지 않고, 링크 파일은 따라가지 않는다. 확장자만
    믿지 않고 조사자가 영상임을 확인한 파일만 후보가 된다(§5.2).
    """
    resolved = _safe_source_root(source_root, workspace_root)
    scan = _walk_sources(resolved, inspect)
    ordered = sorted(
        scan.candidates,
        key=lambda candidate: source_order.order_key(
            candidate.inspection.capture_time, candidate.source_relative_path
        ),
    )
    return IngestProposal(
        scan=scan,
        order=tuple(ordered),
        space=space_estimate(workspace_root, scan.total_bytes),
    )


def _safe_source_root(source_root: Path, workspace_root: Path) -> Path:
    """가져오기 원본 루트 검사 — 없는 곳·파일·작업공간과의 포함 관계를 끊는다.

    양쪽 다 resolve()로 심볼릭 링크를 편 뒤에 비교한다 — 링크로 위장한 재귀
    복사(원본 안에 작업공간, 작업공간 안에 원본)도 같은 판정에 걸린다(§5.2).
    """
    resolved = source_root.expanduser().resolve()
    if not resolved.exists():
        raise IngestError(f"원본 경로가 없다: {source_root}")
    if not resolved.is_dir():
        raise IngestError(f"원본 경로가 폴더가 아니다: {source_root}")
    workspace = workspace_root.expanduser().resolve()
    if resolved == workspace:
        raise SourceNestingError(
            "작업공간 그 자체를 가져올 수 없다 — 카드나 다른 원본 폴더를 고르세요"
        )
    if resolved.is_relative_to(workspace):
        raise SourceNestingError(
            f"원본 루트({resolved})가 작업공간({workspace}) 안이다 — 스스로를 다시 복사하게 된다"
        )
    if workspace.is_relative_to(resolved):
        raise SourceNestingError(
            f"작업공간({workspace})이 원본 루트({resolved}) 안이다 — "
            "복사 결과가 다시 복사 대상이 된다"
        )
    return resolved


def _walk_sources(root: Path, inspect: MediaInspect) -> SourceScan:
    """원본 트리를 한 번 걷는다 — 보호 이름은 어휘 단계에서, stat 이전에 버린다."""
    candidates: list[SourceCandidate] = []
    rejected: list[RejectedSource] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not _pruned_name(name))
        for name in sorted(filenames):
            entry = Path(dirpath) / name
            relative = entry.relative_to(root).as_posix()
            if _pruned_name(name):
                continue  # 어휘 가지치기 — 조회(stat)조차 하지 않는다
            if entry.suffix.lower() not in MEDIA_SUFFIXES:
                continue  # .LRF·썸네일 같은 보조 파일 — 영상 후보가 아니다(§5.2)
            _consider_entry(entry, relative, root, inspect, candidates, rejected)
    return SourceScan(
        source_root=str(root),
        candidates=tuple(candidates),
        rejected=tuple(rejected),
    )


def _consider_entry(
    entry: Path,
    relative: str,
    root: Path,
    inspect: MediaInspect,
    candidates: list[SourceCandidate],
    rejected: list[RejectedSource],
) -> None:
    """파일 하나를 후보로 들일지 결정 — 링크 검사 → 조사 → 후보/제외(사유 필수)."""
    try:
        probed = os.lstat(entry)
    except OSError as error:
        rejected.append(RejectedSource(relative, f"조회 실패: {error}"))
        return
    if stat_module.S_ISLNK(probed.st_mode):
        rejected.append(
            RejectedSource(relative, "심볼릭 링크 — 루트 밖으로 나가는 길이므로 건너뛴다")
        )
        return
    if not stat_module.S_ISREG(probed.st_mode):
        return  # 소켓·파이프 같은 특수 파일 — 영상 후보가 아니다
    try:
        inspection = inspect(entry)
    except Exception as error:  # noqa: BLE001 - 조사 실패 사유는 전부 눈에 보여야 한다
        rejected.append(RejectedSource(relative, f"영상 확인 실패: {error}"))
        return
    candidates.append(
        SourceCandidate(
            source_root=str(root),
            source_relative_path=relative,
            source_name=entry.name,
            size_bytes=probed.st_size,
            mtime_ns=probed.st_mtime_ns,
            inode=probed.st_ino,
            inspection=inspection,
        )
    )


def _pruned_name(name: str) -> bool:
    """보호 철자 판정 — 파일시스템을 전혀 묻지 않는 순수 어휘 검사."""
    return name.startswith(".") or name.casefold() in PROTECTED_SOURCE_NAMES


def space_estimate(workspace_root: Path, copy_bytes: int) -> SpaceEstimate:
    """복사 공간 견적 — 남은 공간이 필요량(여유 배수 포함)을 못 넘기면 부족하다."""
    usage = shutil.disk_usage(workspace_root)
    required = int(copy_bytes * SPACE_HEADROOM) + 1
    return SpaceEstimate(
        copy_bytes=copy_bytes,
        free_bytes=usage.free,
        required_bytes=required,
        sufficient=usage.free >= required,
        note=CLIP_SPACE_NOTE,
    )


# ── 확정 — 배치·자산·불변 순서 리비전. 사람의 확인 뒤에만 온다 ─────────────────


@dataclass(frozen=True, slots=True)
class IngestConfirmation:
    """확정된 가져오기 — 배치·순서 리비전·자산, 그리고 작업 지문.

    `job_fingerprint`는 원본 루트·순서대로 본 출처 신원(경로·크기·mtime·inode)
    스냅숏·순서 리비전·가져오기 설정을 함께 묶는다. 재시도는 이 지문이 같을
    때만 같은 작업에 다시 붙는다 — 입력이 바뀌면 새 가져오기다.
    """

    batch_dir: str
    revision: SourceOrderRevision
    assets: tuple[SourceAsset, ...]
    job_fingerprint: str
    space: SpaceEstimate


def confirm_ingest(workspace: RawWorkspace, proposal: IngestProposal) -> IngestConfirmation:
    """제안을 확정해 배치를 연다 — 스캔 뒤 원본이 바뀌었으면 아무 것도 쓰지 않고 실패."""
    if not proposal.order:
        raise NoIngestCandidatesError(
            "가져올 영상이 없다 — 원본 폴더를 다시 조사하고 제외 사유를 확인하세요"
        )
    revalidated = _revalidate_sources(proposal)
    drafts = tuple(
        _draft_from_candidate(index, candidate) for index, candidate in enumerate(revalidated)
    )
    assets, revision = workspace.create_ingest_batch(drafts, ORDER_BASIS_USER_CONFIRMED)
    fingerprint = ingest_fingerprint(
        revalidated[0].source_root,
        [
            (
                draft.source_relative_path,
                draft.size_bytes,
                draft.source_mtime_ns,
                draft.source_inode,
            )
            for draft in drafts
        ],
        revision.revision_id,
    )
    return IngestConfirmation(
        batch_dir=revision.batch_dir,
        revision=revision,
        assets=assets,
        job_fingerprint=fingerprint,
        space=space_estimate(workspace.root, proposal.scan.total_bytes),
    )


def _draft_from_candidate(index: int, candidate: SourceCandidate) -> SourceAssetDraft:
    """후보 하나를 확정 초안으로 — 순번은 제안 그대로 1부터, 근거는 미조사 표기."""
    return SourceAssetDraft(
        source_root=candidate.source_root,
        source_relative_path=candidate.source_relative_path,
        source_name=candidate.source_name,
        size_bytes=candidate.size_bytes,
        source_mtime_ns=candidate.mtime_ns,
        source_inode=candidate.inode,
        capture_time=candidate.inspection.capture_time,
        duration_seconds=candidate.inspection.duration_seconds,
        width=candidate.inspection.width,
        height=candidate.inspection.height,
        ordinal=index + 1,
        basis_note=source_order.capture_basis_note(candidate.inspection.capture_time),
    )


def _revalidate_sources(
    proposal: IngestProposal,
) -> tuple[SourceCandidate, ...]:
    """확정 직전 재검사 — 크기·mtime·inode·링크가 스캔과 같은지 다시 본다(§5.3)."""
    for candidate in proposal.order:
        entry = Path(candidate.source_root) / candidate.source_relative_path
        try:
            probed = os.lstat(entry)
        except OSError as error:
            raise SourceChangedError(
                f"원본이 사라졌다: {candidate.source_relative_path} ({error})"
            ) from error
        changed = (
            probed.st_size != candidate.size_bytes
            or probed.st_mtime_ns != candidate.mtime_ns
            or probed.st_ino != candidate.inode
            or stat_module.S_ISLNK(probed.st_mode)
        )
        if changed:
            raise SourceChangedError(
                f"원본이 스캔 뒤 바뀌었다: {candidate.source_relative_path} "
                f"(크기 {candidate.size_bytes}→{probed.st_size})"
            )
    return proposal.order


def ingest_fingerprint(
    source_root: str,
    ordered_stats: Sequence[tuple[str, int, int, int]],
    revision_id: str,
) -> str:
    """작업 지문 — 출처 신원 스냅숏·순서·설정까지 묶는 sha256(코디네이터 ACK)."""
    payload = json.dumps(
        {
            "settings": INGEST_SETTINGS_DIGEST,
            "source_root": source_root,
            "files": [
                {"path": path, "size": size, "mtime_ns": mtime_ns, "inode": inode}
                for path, size, mtime_ns, inode in ordered_stats
            ],
            "revision": revision_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reconfirm_ingest(workspace: RawWorkspace, batch_dir: str) -> IngestConfirmation:
    """DB에 남은 배치로 확인 객체를 다시 만든다 — 재시도가 처음과 같은 지문을 갖는다."""
    assets = workspace.list_source_assets(batch_dir)
    if not assets:
        raise IngestError(f"가져오기 배치가 없다: {batch_dir}")
    revision = workspace.order_revision(batch_dir)
    if revision is None:
        raise IngestError(f"배치에 순서 리비전이 없다: {batch_dir}")
    ordered = tuple(sorted(assets, key=lambda asset: asset.ordinal))
    fingerprint = ingest_fingerprint(
        ordered[0].source_root,
        [
            (
                asset.source_relative_path,
                asset.size_bytes,
                asset.source_mtime_ns,
                asset.source_inode,
            )
            for asset in ordered
        ],
        revision.revision_id,
    )
    pending_bytes = sum(asset.size_bytes for asset in ordered if asset.status != "ready")
    return IngestConfirmation(
        batch_dir=batch_dir,
        revision=revision,
        assets=ordered,
        job_fingerprint=fingerprint,
        space=space_estimate(workspace.root, pending_bytes),
    )


# ── 복사·검증 — 파일 단위. ready만 나가고, 나머지는 재개 가능하게 남는다 ─────────


def run_ingest_copy(
    context: JobContext, workspace: RawWorkspace, confirmation: IngestConfirmation
) -> None:
    """확정된 순서대로 파일 단위 복사·검증 — 작업 본문(`jobs`가 부른다).

    시작 전에 **실제** 남은 분량으로 공간을 다시 본다 — ready 표시라도 재검증을
    통과하지 못하면 다시 복사할 몫으로 센다. 진행값은 이번 실행 기준으로 다시
    맞춘다(재시도가 지난 진행 위에 두 번 쌓지 않는다). 검증된(ready) 파일은 다시
    읽어 확인한 뒤 건너뛰고, ready 표시가 재검증에 실패한 파일은 **건드리지 않고**
    실패로 남긴다 — 한번 검증된 원본을 지우는 길은 없다(코디네이터 재검토).
    """
    ordered = tuple(sorted(confirmation.assets, key=lambda asset: asset.ordinal))
    passes = _survey_pending(workspace, ordered)
    pending_bytes = sum(live.size_bytes for live, verified in passes if not verified)
    _require_copy_space(workspace.root, pending_bytes)
    context.set_total(len(passes))
    context.set_progress(0)
    failures: list[str] = []
    for live, verified in passes:
        context.check_cancel()
        failure = _attempt_one_pass(workspace, live, verified)
        if failure is not None:
            failures.append(failure)
        context.advance()
    if failures:
        raise IngestCopyError(
            f"{len(failures)}/{len(passes)}건이 실패했다 — "
            + " | ".join(failures[:3])
            + (" …" if len(failures) > 3 else "")
        )


def _attempt_one_pass(workspace: RawWorkspace, live: SourceAsset, verified: bool) -> str | None:
    """한 건의 이번 실행 처리 — 성공이면 None, 실패 문장을 돌려준다.

    검증됐으면 조용히 건너뛴다. ready 표시가 재검증에 실패했으면 파일을 그대로
    둔 채 실패로 남긴다(한번 검증된 원본을 지우는 길은 없다). 그 외는 복사한다.
    """
    if verified:
        return None
    if live.status == "ready":
        return _record_asset_failure(workspace, live.asset_id, _invalid_ready_error(live))
    try:
        _copy_one_asset(workspace, live.asset_id)
    except JobCancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 파일 단위 실패는 기록하고 다음 파일로
        return _record_asset_failure(workspace, live.asset_id, error)
    return None


def _survey_pending(
    workspace: RawWorkspace, ordered: tuple[SourceAsset, ...]
) -> list[tuple[SourceAsset, bool]]:
    """이번 실행의 실제 대상 — 상태표가 아니라 재검증 결과로 가른다.

    ready 표시가 깨졌거나(파일 변조) 알 수 없는 상태면 복사할 몫이다. 상태가
    ready가 아닌 것은 당연히 복사할 몫이다(공간 게이트가 이 합을 본다).
    """
    passes: list[tuple[SourceAsset, bool]] = []
    for asset in ordered:
        live = workspace.source_asset(asset.asset_id)
        verified = live.status == "ready" and _destination_still_verified(workspace, live)
        passes.append((live, verified))
    return passes


def _invalid_ready_error(live: SourceAsset) -> IngestError:
    """ready 표시 원본의 재검증 실패 문장 — 파일은 그대로 두고 사람에게 넘긴다."""
    return IngestError(
        f"ready로 표시된 원본이 재검증에 실패했다: {live.source_relative_path} — "
        "파일을 그대로 두었다. 다시 가져오려면 같은 원본을 새 가져오기로 확정하세요"
    )


def _require_copy_space(workspace_root: Path, copy_bytes: int) -> None:
    estimate = space_estimate(workspace_root, copy_bytes)
    if not estimate.sufficient:
        raise InsufficientSpaceError(
            f"복사 공간이 부족하다 — 필요 {estimate.required_bytes:,}B(여유 "
            f"{SPACE_HEADROOM:.0%} 포함) / 남은 {estimate.free_bytes:,}B. "
            "복사는 시작하지 않았다"
        )


def _copy_one_asset(workspace: RawWorkspace, asset_id: str) -> None:
    """자산 하나를 복사·검증해 ready로 만든다 — 어느 단계에서 죽어도 ready는 아니다."""
    live = workspace.source_asset(asset_id)
    source = _validated_source_path(live)
    pre_stat = _source_stat(source, live)
    workspace.update_source_asset(asset_id, status="copying", error=None)
    destination = _validated_destination_path(workspace, live)
    _prepare_destination(workspace, live, destination)
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    copied_hash = _copy_with_hash(source, partial)
    post_stat = os.lstat(source)
    if (post_stat.st_size, post_stat.st_mtime_ns, post_stat.st_ino) != (
        pre_stat.st_size,
        pre_stat.st_mtime_ns,
        pre_stat.st_ino,
    ):
        raise SourceChangedError(f"복사하는 동안 원본이 바뀌었다: {live.source_relative_path}")
    workspace.update_source_asset(asset_id, status="verifying")
    reread_hash = _sha256_file(partial)
    if reread_hash != copied_hash or partial.stat().st_size != pre_stat.st_size:
        raise IngestError("검증 실패 — 목적지를 다시 읽은 해시가 복사 때 해시와 다르다")
    os.replace(partial, destination)
    registration = _register_verified(workspace, live, destination)
    duplicate_of = workspace.first_ready_asset_with_hash(reread_hash, asset_id)
    workspace.update_source_asset(
        asset_id,
        status="ready",
        content_sha256=reread_hash,
        file_id=registration.file_id,
        duplicate_of=duplicate_of,
        error=None,
    )


def _lexically_safe_relative(relative: str, *, originals_only: bool) -> str:
    """경로 표기 검사 — 어느 칸도 비거나(`.`·`..`) 숨김이면 안 된다(링크 이전 단계)."""
    parts = relative.split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        raise IngestError(f"경로 표기가 안전하지 않다: {relative}")
    if any(part.startswith(".") for part in parts):
        raise IngestError(f"숨김 칸이 끼어 있다: {relative}")
    if originals_only and not relative.startswith("originals/"):
        raise IngestError(f"가져오기 목적지는 originals/ 아래만 가능하다: {relative}")
    return relative


def _validated_tree(root: Path, relative: str, *, allow_missing_tail: bool) -> Path:
    """루트 아래 각 칸을 lstat해 심볼릭 링크가 하나도 없음을 증명하고 경로를 돌려준다.

    링크가 끼어 있으면 그 자리에서 실패한다 — 읽기·쓰기·지우기 무엇도 하기 전이다.
    `allow_missing_tail`이면 처음으로 없는 칸에서 멈춘다(그 아래는 우리가 만들 것이다).
    """
    target = root / relative
    current = root
    for part in relative.split("/"):
        current = current / part
        try:
            probed = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail:
                return target
            raise OSError(f"경로 칸이 없다: {current}") from None
        if stat_module.S_ISLNK(probed.st_mode):
            raise IngestError(f"심볼릭 링크 칸을 지나갈 수 없다: {current}")
    return target


def _validated_source_path(live: SourceAsset) -> Path:
    """원본 전체 경로 증명 — 마지막 파일만 보지 않고 조상 칸까지 전부 링크가 아님을 본다."""
    source_root = Path(live.source_root)
    _validated_tree(source_root.parent, source_root.name, allow_missing_tail=False)
    return _validated_tree(source_root, live.source_relative_path, allow_missing_tail=False)


def _validated_destination_path(workspace: RawWorkspace, live: SourceAsset) -> Path:
    """목적지 전체 경로 증명 — 어휘 검사 뒤 조상 칸까지 확인한다(mkdir보다 앞선다)."""
    relative = _lexically_safe_relative(live.relative_path, originals_only=True)
    return _validated_tree(workspace.root, relative, allow_missing_tail=True)


def _source_stat(source: Path, live: SourceAsset) -> os.stat_result:
    """복사 직전 원본 스냅숏 — 없거나(카드 분리) 확정 때와 다르면 시작하지 않는다."""
    try:
        probed = os.lstat(source)
    except OSError as error:
        raise OSError(f"원본을 읽을 수 없다: {live.source_relative_path} ({error})") from error
    if stat_module.S_ISLNK(probed.st_mode):
        raise SourceChangedError(f"원본이 심볼릭 링크로 바뀌었다: {live.source_relative_path}")
    if (
        probed.st_size != live.size_bytes
        or probed.st_mtime_ns != live.source_mtime_ns
        or probed.st_ino != live.source_inode
    ):
        raise SourceChangedError(
            f"원본이 확정 때와 다르다: {live.source_relative_path} "
            f"(크기 {live.size_bytes}→{probed.st_size}) — 다시 조사·확정해야 한다"
        )
    return probed


def _prepare_destination(workspace: RawWorkspace, live: SourceAsset, destination: Path) -> None:
    """목적지 정리 — 잔해 partial은 덮고, 장부와 어긋난 최종 파일은 치운다.

    치울 수 있는 것은 **우리가 쓴 미검증 잔해**뿐이다. 경로 증명(`_validated_tree`)을
    지난 뒤에만 mkdir·unlink가 일어나고, 링크로 바뀐 칸이나 링크로 놓인 잔해는
    그 자리에서 실패한다 — 링크 너머로 무엇도 쓰지 않는다(코디네이터 재검토).
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    if partial.exists():
        if not _regular_file(partial):
            raise IngestError(f"잔해 partial이 일반 파일이 아니다 — 치우지 않는다: {partial}")
        partial.unlink()
    if destination.exists():
        if not _regular_file(destination):
            raise IngestError(f"목적지가 일반 파일이 아니다 — 치우지 않는다: {destination}")
        # ready 표시 없는 최종 파일은 이전 시도의 미검증 잔해다 — 등록 행도 함께
        # 치우고 다시 복사한다(검증되지 않은 파일을 ready로 믿지 않는다).
        workspace.discard_media_registration(live.relative_path)
        destination.unlink()


def _regular_file(path: Path) -> bool:
    try:
        return stat_module.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _register_verified(
    workspace: RawWorkspace, live: SourceAsset, destination: Path
) -> MediaRegistration:
    """검증을 마친 최종 파일을 미디어로 등록한다 — 등록만 남은 창은 스스로 닫는다."""
    try:
        return workspace.register_media_file(destination, "original")
    except MediaNotAllowedError:
        # 앞선 시도가 개명까지만 끝내고 자산 표시를 못 남긴 창 — 등록 행을
        # 치우고 다시 등록한다. 장부(source_assets)가 정본이다.
        workspace.discard_media_registration(live.relative_path)
        return workspace.register_media_file(destination, "original")


def _destination_still_verified(workspace: RawWorkspace, live: SourceAsset) -> bool:
    """ready 표시의 재확인 — 크기·등록·내용 해시를 다시 읽어 믿는다(맹신 금지).

    재독출 전에 조상 칸까지 링크가 아님을 증명한다 — 링크 너머를 읽지 않는다.
    """
    try:
        destination = _validated_tree(
            workspace.root,
            _lexically_safe_relative(live.relative_path, originals_only=True),
            allow_missing_tail=False,
        )
        probed = os.lstat(destination)
    except (OSError, IngestError):
        return False
    if not stat_module.S_ISREG(probed.st_mode) or probed.st_size != live.size_bytes:
        return False
    if live.content_sha256 is None:
        return False
    if live.file_id is not None:
        try:
            workspace.resolve_media_file(live.file_id)
        except RawWorkspaceError:
            return False
    return _sha256_file(destination) == live.content_sha256


def _record_asset_failure(workspace: RawWorkspace, asset_id: str, error: Exception) -> str:
    """파실패 기록 — 자산을 failed로, 잔해 partial을 치우고, 보고 문장을 돌려준다."""
    live = workspace.source_asset(asset_id)
    destination = workspace.root / live.relative_path
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    detail = str(error)
    if partial.exists() and _regular_file(partial):
        try:
            partial.unlink()
        except OSError as cleanup_error:
            detail += f" (partial 제거도 실패: {cleanup_error})"
    workspace.update_source_asset(asset_id, status="failed", error=detail[:500])
    return f"{live.source_relative_path}: {detail}"


def _copy_with_hash(source: Path, target: Path) -> str:
    """복사하며 원본을 읽는 동시에 해시를 계산한다 — 두 번째 판독은 목적지에서 온다."""
    digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(COPY_CHUNK_BYTES):
            writer.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    """파일을 다시 읽어 sha256을 계산한다 — 검증은 언제나 재판독이다(§5.3)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
