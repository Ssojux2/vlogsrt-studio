"""A3 추출 작업 본문 — 순차 추출·검증·게시·등록, 그리고 같은 작업 재개(§6.3·§8.4).

한 원본씩, 원본 안에서는 조각 번호순으로 **한 개씩** 처리한다(§6.3). 조각 하나의
길은 정해져 있다: 추출(스테이징) → 구조 검증 → 원자 게시 → 미디어 등록 →
다음 조각. 앞 조각이 실패하면 그 원본의 게시는 거기서 멈춘다 — 실패한 조각을
실패로 표시하고, **이미 검증·게시된 앞 조각은 그대로 보존**한다(불완전 폴더를
완성으로 위장하지 않는다).

재개는 같은 작업·같은 계획 리비전에서만 일어난다(§6.3). 게시된 조각은 파일
해시를 다시 읽어 확인하고 지우거나 덮어쓰지 않는다 — 어긋나면 그 조각이
실패다. 조각 ID·번호는 계획에서 유도되므로 재개 전후로 같다.

추출 자체는 `vlogsrt.preprocess.extract_raw_clip` 하나다(경계 규칙 7의 짝 —
ffmpeg 호출은 preprocess 안에만 산다). 원본 오디오 꼬리 보존(마지막 클립만),
공통 기준 변환, 원본 불변 스테이징 검사도 그 헬퍼의 계약을 그대로 탄다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from vlogsrt import preprocess
from vlogsrt.project.raw import package as package_layout
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext
from vlogsrt.project.raw.models import (
    EXTRACT_PROFILES,
    ClipArtifact,
    ExtractJobError,
    PackageError,
    PackageRevision,
    SegmentPlanEntry,
    SegmentPlanSnapshot,
    SourceAsset,
)
from vlogsrt.project.raw.workspace import RawWorkspace, new_clip_id

__all__ = [
    "DEFAULT_EXTRACTOR",
    "EXTRACT_SETTINGS_DIGEST",
    "ExtractClip",
    "SourceEvidence",
    "extraction_fingerprint",
    "profile_for",
    "reconfirm_extraction",
    "run_segment_extraction",
]

EXTRACT_SETTINGS_DIGEST = "raw-extract/1:sequential+staging+hash-reread"
"""추출 작업 지문에 박는 규칙 요약 — 순서·스테이징·재독출 검증이 바뀌면 지문이
달라진다(재개가 다른 규칙의 결과에 묶이지 않게 한다)."""

ExtractClip = Callable[..., preprocess.RawClipExtraction]
"""추출기 주입점 — 시험은 실패 주입용 가짜를, 실사용은 `extract_raw_clip`을 쓴다."""

DEFAULT_EXTRACTOR: ExtractClip = preprocess.extract_raw_clip
"""작업 본문이 쓰는 추출기 — 호출 시점에 읽는다(시험이 감싸 끼울 자리).

서비스가 묶은 본문도 이 이름을 늦게 보므로, 감싼 추출기가 실행 중인 작업에도
적용된다. ffmpeg 경계 자체는 preprocess 안에 그대로 있다."""


class SourceEvidence:
    """원본 하나의 실행 시점 증거 — 조사·시계·오디오 실측을 한 번만 얻는다."""

    def __init__(self, workspace: RawWorkspace, asset: SourceAsset) -> None:
        from vlogsrt.project.raw.media_refs import load, resolve

        path = resolve(workspace, asset, "export")
        refs = load(workspace, asset.asset_id)
        expected_size = (
            refs["original"]["size"] if refs and refs.get("original") else asset.size_bytes
        )
        probed = path.stat()
        if probed.st_size != expected_size:
            raise ExtractJobError(
                f"원본 크기가 계획 때와 다르다: {asset.source_relative_path}"
                f" ({asset.size_bytes} → {probed.st_size}B) — 다시 계획하세요"
            )
        self.asset = asset
        self.path = path
        self.probe = preprocess.probe_raw_media(path)
        self.clock = preprocess.probe_extraction_clock(path, probe=self.probe)
        if not self.clock.uniform_grid:
            raise ExtractJobError(
                f"원본이 균일 격자가 아니다(VFR): {asset.source_relative_path}"
                f" — {self.clock.grid_note}"
            )
        self.audio_extents = preprocess.probe_audio_extents(path)


def profile_for(name: str) -> preprocess.RawTranscodeProfile:
    """프로파일 이름 → preprocess의 동결 프로파일 — 이름은 계약 그대로다(§8.1)."""
    for profile in (preprocess.RAW_SOURCE_MATCHED, preprocess.RAW_EDIT_PRORES):
        if profile.name == name:
            return profile
    raise PackageError(f"알 수 없는 추출 프로파일: {name!r} (지원: {', '.join(EXTRACT_PROFILES)})")


def extraction_fingerprint(
    plan_fingerprint: str,
    profile_name: str,
    handles: preprocess.RawClipHandles,
    tool_provenance: str,
) -> str:
    """추출 작업 지문 — 계획 지문 + 프로파일 + 핸들 + 도구 버전(§14.2 추출 캐시 키).

    도구(ffmpeg)가 바뀌면 같은 계획이라도 출력이 달라질 수 있으므로 지문이 갈라진다
    — 재개는 같은 도구에서만 묶인다.
    """
    payload = json.dumps(
        {
            "settings": EXTRACT_SETTINGS_DIGEST,
            "plan": plan_fingerprint,
            "profile": profile_name,
            "handles": {"before": str(handles.before), "after": str(handles.after)},
            "tool": tool_provenance,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _clip_range(entry: SegmentPlanEntry) -> preprocess.RawPtsRange:
    """조각 계획의 core를 원본 PTS 범위로 — 틱·time_base는 계획의 것이다(§13.3)."""
    return preprocess.RawPtsRange(
        time_base=Fraction(entry.time_base_num, entry.time_base_den),
        start_pts=entry.core_start_pts,
        end_pts=entry.core_end_pts,
    )


def run_segment_extraction(
    context: JobContext,
    workspace: RawWorkspace,
    plan: SegmentPlanSnapshot,
    package: PackageRevision,
    assets: Mapping[str, SourceAsset],
    handles: preprocess.RawClipHandles,
    extractor: ExtractClip | None = None,
) -> None:
    """작업 본문 — `jobs`가 부른다. 전체 순서·조각 순서 그대로 한 개씩(§6.3).

    실패한 조각에서 멈추고(`ExtractJobError`) 그 앞의 게시물은 보존한다. 취소
    게이트는 조각 사이에만 선다 — 진행 중인 ffmpeg을 치지 않고 끝난 결과는
    그대로 둔다(§14.1).
    """
    profile = profile_for(package.profile_name)
    extract = extractor if extractor is not None else DEFAULT_EXTRACTOR
    ordered = sorted(plan.entries, key=lambda item: (item.source_order, item.segment_order))
    context.set_total(len(ordered))
    context.set_progress(0)
    workspace.update_package(package.package_dir, status="publishing")
    evidence: dict[str, SourceEvidence] = {}
    published = 0
    try:
        for entry in ordered:
            context.check_cancel()
            clip = _process_one(
                workspace, package, entry, assets, profile, handles, evidence, extract
            )
            if clip.status == "published":
                published += 1
                workspace.update_package(package.package_dir, clip_published=published)
            context.advance()
    except JobCancelledError:
        workspace.update_package(package.package_dir, status="interrupted")
        raise
    clips = workspace.list_clip_artifacts(package.package_dir)
    unpublished = [clip for clip in clips if clip.status != "published"]
    if unpublished:
        raise ExtractJobError(
            f"게시되지 않은 조각이 {len(unpublished)}개 있다 — 완성이 아니다"
            f" (예: {unpublished[0].clip_id} {unpublished[0].error or ''})"
        )
    package_layout.write_package_manifest(
        workspace.root, package, _assets_for(assets, clips), clips
    )
    workspace.update_package(package.package_dir, status="complete", error=None)


def _assets_for(
    assets: Mapping[str, SourceAsset], clips: Sequence[ClipArtifact]
) -> dict[str, SourceAsset]:
    return {clip.asset_id: assets[clip.asset_id] for clip in clips if clip.asset_id in assets}


def _process_one(
    workspace: RawWorkspace,
    package: PackageRevision,
    entry: SegmentPlanEntry,
    assets: Mapping[str, SourceAsset],
    profile: preprocess.RawTranscodeProfile,
    handles: preprocess.RawClipHandles,
    evidence: dict[str, SourceEvidence],
    extractor: ExtractClip,
) -> ClipArtifact:
    """조각 하나의 처리 — 게시된 것은 재검증, 아니면 추출·게시·등록한다."""
    asset = assets.get(entry.asset_id)
    if asset is None:
        raise ExtractJobError(f"조각의 원본 자산 기록이 없다: {entry.asset_id}")
    relative = package_layout.clip_relative_path(
        package.package_dir, entry, asset, profile.container
    )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    clip = workspace.ensure_clip_row(_pending_clip(entry, package, relative, now))
    destination = workspace.root / clip.relative_path
    if clip.status == "published":
        return _validate_published(workspace, package, clip, destination)
    if clip.content_sha256 is not None:
        # 한번 게시된 조각(재검증에 걸려 실패 상태가 된 것 포함)은 다시 추출하지
        # 않는다 — 같은 ID로 다른 내용을 얹으면 검증 이력이 거짓이 된다(루트
        # 재현: 실패 상태로 바뀐 뒤 재시도가 사라진 파일을 몰래 다시 만들었다).
        _fail(
            workspace,
            package,
            clip,
            "한번 게시된 조각은 다시 추출하지 않는다 — 이 package는 폐기하고 새"
            " package로 다시 추출하세요",
        )
        raise ExtractJobError(
            f"한번 게시된 조각이 실패 상태에 있다: {clip.clip_id}"
            f" ({clip.error or '재검증 실패'}) — 다시 추출하지 않는다"
        )
    workspace.update_clip_artifact(clip.clip_id, status="extracting", error=None)
    facts = evidence.get(entry.asset_id)
    if facts is None:
        facts = SourceEvidence(workspace, asset)
        evidence[entry.asset_id] = facts
    try:
        extraction = extractor(
            facts.path,
            destination,
            _clip_range(entry),
            profile,
            handles=handles,
            probe=facts.probe,
            clock=facts.clock,
            audio_extents=facts.audio_extents,
        )
    except JobCancelledError:
        _fail(workspace, package, clip, "추출 취소됨 · 다시 준비 가능")
        raise
    except Exception as error:  # noqa: BLE001 - 조각 단위 실패는 기록하고 멈춘다
        _fail(workspace, package, clip, f"{type(error).__name__}: {error}")
        raise ExtractJobError(
            f"조각 {clip.segment_order}번({entry.asset_id}) 추출이 실패했다: {error}"
            " — 이 조각 앞의 게시물은 보존된다"
        ) from error
    file_id = _registered_file_id(workspace, clip.relative_path, destination)
    return workspace.update_clip_artifact(
        clip.clip_id,
        status="published",
        content_sha256=_sha256_file(destination),
        size_bytes=destination.stat().st_size,
        file_id=file_id,
        media_start_pts=extraction.media_range.start_pts,
        media_end_pts=extraction.media_range.end_pts,
        core_local_start=str(extraction.core_local_start),
        core_local_end=str(extraction.core_local_end),
        media_local_duration=str(extraction.media_local_end),
        frame_count=extraction.frame_count,
        error=None,
    )


def _pending_clip(
    entry: SegmentPlanEntry, package: PackageRevision, relative_path: str, now: str
) -> ClipArtifact:
    """조각 행의 초기 모습 — 신원은 계획·package에서, 상태는 대기(측정값은 비어 있다)."""
    return ClipArtifact(
        clip_id=new_clip_id(
            package_layout.clip_digest(entry), package.package_dir, package.job_fingerprint
        ),
        package_dir=package.package_dir,
        segment_id=entry.segment_id,
        asset_id=entry.asset_id,
        source_order=entry.source_order,
        segment_order=entry.segment_order,
        profile_name=package.profile_name,
        relative_path=relative_path,
        status="pending",
        content_sha256=None,
        size_bytes=None,
        file_id=None,
        time_base_num=entry.time_base_num,
        time_base_den=entry.time_base_den,
        core_start_pts=entry.core_start_pts,
        core_end_pts=entry.core_end_pts,
        media_start_pts=entry.media_start_pts,
        media_end_pts=entry.media_end_pts,
        core_local_start="0",
        core_local_end="0",
        media_local_duration="0",
        frame_count=None,
        error=None,
        created_at=now,
        updated_at=now,
    )


def _validate_published(
    workspace: RawWorkspace, package: PackageRevision, clip: ClipArtifact, destination: Path
) -> ClipArtifact:
    """재개의 재검증 — 게시물 해시를 다시 읽는다. 지우거나 덮어쓰지 않는다."""
    reason = None
    if not destination.is_file():
        reason = "게시된 조각 파일이 없다"
    elif clip.content_sha256 is None:
        reason = "게시된 조각에 기록된 해시가 없다"
    elif _sha256_file(destination) != clip.content_sha256:
        reason = "게시된 조각이 기록된 해시와 다르다"
    if reason is not None:
        _fail(workspace, package, clip, reason)
        raise ExtractJobError(
            f"{reason}: {clip.relative_path} — 지우지 않는다. 이 package는 폐기하고"
            " 새 package로 다시 추출하세요"
        )
    return clip


def _registered_file_id(workspace: RawWorkspace, relative_path: str, destination: Path) -> str:
    """조각을 artifact 미디어로 등록한다 — 재개는 같은 file_id를 유지한다."""
    existing = workspace.media_registration_by_path(relative_path)
    if existing is not None:
        return existing.file_id
    return workspace.register_media_file(destination, "artifact").file_id


def _fail(workspace: RawWorkspace, package: PackageRevision, clip: ClipArtifact, note: str) -> None:
    """실패 기록 — 조각 행·package 행에 이유를 남긴다(파일은 그대로 둔다)."""
    trimmed = note[:480]
    workspace.update_clip_artifact(clip.clip_id, status="failed", error=trimmed)
    workspace.update_package(package.package_dir, status="failed", error=trimmed)


def reconfirm_extraction(
    workspace: RawWorkspace, package_dir: str
) -> tuple[SegmentPlanSnapshot, PackageRevision, preprocess.RawClipHandles]:
    """DB의 package 기록으로 추출 요청을 다시 만든다 — 재시도의 지문 재료(§14.2).

    계획 리비전·프로파일·핸들은 package 행에 얼어 있으므로, 재시도는 처음과 같은
    지문을 갖는다. 도구 버전이 그 사이 바뀌었다면 지문이 갈라져 재결합이 거부된다
    — 다른 도구의 출력을 같은 package에 섞지 않는다.
    """
    package = workspace.package_revision(package_dir)
    if package is None:
        raise PackageError(f"없는 package: {package_dir}")
    plan = workspace.segment_plan_revision(package.plan_revision_id)
    handles = preprocess.RawClipHandles(
        before=Fraction(package.handles_before),
        after=Fraction(package.handles_after),
    )
    return plan, package, handles
