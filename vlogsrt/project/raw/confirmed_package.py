"""A6 확정 내보내기 — 검증된 조각 미디어를 불변 폴더로 복사하는 순수 파일시스템 라이브러리.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §9(9.1·9.2·9.3·9.4)·화면 3·§14.4·16-A6.
DB·UI·Finder 열기·CLI는 여기에 없다(등록 신원 통합은 W1 소유). 이 모듈이 하는 일은
하나다: **이미 검증·게시된** A3 package의 실제 미디어를 서버가 예약한 새 확정 폴더에
**진짜 복사**로 옮겨 얼려 붙인다(§9.4 — 편집기가 참조한 파일은 앱 종료·캐시 삭제·
재분석 뒤에도 그대로 남아 있어야 한다).

지키는 선:

- **하드링크가 아니라 복사다.** 복사 앞뒤로 원본 stat+SHA를 재고, 목적지는 다시
  읽은 SHA로 확인한다 — 옛 원본이 나중에 바뀌어도 확정 미디어는 바뀌지 않는다.
- **스테이징은 홀로 소유한 디렉터리, 게시는 이름바꾸기 한 번.** 실패하면 내
  스테이징만 지운다 — 옛 package와 이미 확정된 폴더는 손대지 않는다. 같은 자리에
  이미 확정본이 있으면 지문이 정확히 같을 때만 쓰기 없이 멱등 검증하고, 다르면
  거부한다(§6.5 — 덮어쓰는 길이 없다).
- **신원은 서버 소유다.** 이 라이브러리는 package 번호를 할당하지 않는다 — 목적지는
  확정 전용 이름공간 `clips/confirmed_*`만 받아 A3 DB 예약과 부딪히지 않는다.
- **낡은 core는 못 쓴다.** 검토 리비전에서 경계가 바뀐 조각은 그 core에 맞게 다시
  추출·검증된 artifact만 확정한다. 프로파일·핸들도 artifact가 뽑힌 값이어야 한다.
- **폴더는 스스로 완결적이다.** 안의 경로는 폴더 기준 상대 경로로만 기록한다 —
  작업공간을 옮겨도, `cache/`·`previews/`가 지워져도 검증·재생된다(§9.2).
"""

from __future__ import annotations

import csv
import dataclasses
import errno
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from stat import S_ISREG

from vlogsrt.project.raw.models import ClipArtifact, PackageRevision, SourceAsset
from vlogsrt.project.raw.package import CSV_FIELDS, formula_safe_cell
from vlogsrt.project.raw.segment_review import ReviewRevision, ReviewSegment

__all__ = [
    "CONFIRMED_SCHEMA_VERSION",
    "EXPORT_SETTINGS_DIGEST",
    "ConfirmedClip",
    "ConfirmedExportError",
    "ConfirmedExportReport",
    "ConfirmedExportRequest",
    "ExportExistsError",
    "PathSafetyError",
    "ProfileMismatchError",
    "SourceMediaError",
    "StaleCoreError",
    "build_confirmed_request",
    "clean_abandoned_staging",
    "export_confirmed_package",
    "export_fingerprint",
    "verify_confirmed_export",
]

CONFIRMED_SCHEMA_VERSION = "raw-confirmed/v1"
"""확정 폴더 기록의 스키마 표기 — 매니페스트 재독기가 같는 것만 받는다."""

EXPORT_SETTINGS_DIGEST = "raw-confirmed/1:byte-copy+sha-reread+staging+atomic-rename"
"""확정 규칙 요약 — 규칙이 바뀌면 지문이 갈라져 옛 확정본과 섞이지 않는다."""

MANIFEST_NAME = "confirmed_manifest.json"
CLIPS_JSONL_NAME = "clips.jsonl"
CLIP_INDEX_NAME = "clip_index.csv"
README_NAME = "README.md"
MEDIA_DIR_NAME = "media"

PUBLISH_MODE_PUBLISHED = "published"
PUBLISH_MODE_VERIFIED_EXISTING = "verified_existing"

CONFIRMED_CLIP_STATUS = "confirmed"
"""확정 조각의 상태 표기 — 검증을 마쳤다는 뜻이지 복사본이 아니라는 뜻은 아니다."""

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DESTINATION_DIR = re.compile(r"^clips/confirmed_[A-Za-z0-9][A-Za-z0-9._-]*$")
_STAGING_NAME = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*\.[0-9a-z_]{8}\.confirmed-staging$")


class ConfirmedExportError(RuntimeError):
    """확정 내보내기 계약 위반 — 사유를 사람에게 그대로 보여준다."""


class PathSafetyError(ConfirmedExportError):
    """경로가 계약을 벗어났다 — 심볼릭 링크·작업공간 밖·이름공간 위반."""


class SourceMediaError(ConfirmedExportError):
    """원본 조각이 검증 기록과 어긋난다 — 없음·0바이트·복사 중 변경·재독출 불일치."""


class StaleCoreError(ConfirmedExportError):
    """검토 경계가 바뀌어 옛 artifact를 못 쓴다 — 다시 추출·검증이 필요하다."""


class ProfileMismatchError(ConfirmedExportError):
    """고른 프로파일·핸들이 artifact가 뽑힌 값과 다르다."""


class ExportExistsError(ConfirmedExportError):
    """이미 있는 확정 자리 — 지문이 다르면 덮어쓰지 않고 거부한다."""


# ── 얼어 붙은 기록 ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConfirmedClip:
    """확정할 조각 하나 — 검증된 artifact와 원본·검토 기록을 한데 얼린 지시.

    `source_order`·`segment_order`는 원본 그대로다(선택으로 걸러도 다시 붙이지
    않는다). `export_index`·`export_relative_path`가 폴더 안 병렬 번호다.
    """

    export_index: int
    export_relative_path: str
    clip_id: str
    segment_id: str
    source_id: str
    source_name: str
    source_sha256: str
    source_order: int
    segment_order: int
    time_base_num: int
    time_base_den: int
    core_start_pts: int
    core_end_pts: int
    media_start_pts: int
    media_end_pts: int
    media_local_duration: str
    frame_count: int | None
    profile_name: str
    handles_before: str
    handles_after: str
    origin_package_dir: str
    origin_relative_path: str
    content_sha256: str
    size_bytes: int
    container: str

    @property
    def time_base(self) -> Fraction:
        """원본 time_base — 틱을 초로 읽는 정확 유리수(§13.3)."""
        return Fraction(self.time_base_num, self.time_base_den)


@dataclass(frozen=True, slots=True)
class ConfirmedExportRequest:
    """확정 내보내기 요청 — 서버가 정한 신원·목적지와 얼어 붙은 스냅샷 전부."""

    export_id: str
    destination_dir: str
    project_id: str
    plan_revision_id: str
    order_revision_id: str
    review_revision_id: str
    review_fingerprint: str
    profile_name: str
    handles_before: str
    handles_after: str
    settings_digest: str
    tool_provenance: str
    created_at: str
    clips: tuple[ConfirmedClip, ...]


@dataclass(frozen=True, slots=True)
class ConfirmedExportReport:
    """확정 폴더 하나의 검증 요약 — 화면 3의 검증 결과에 그대로 쓴다."""

    export_id: str
    destination_dir: str
    clip_count: int
    total_bytes: int
    total_duration: str
    mode: str


# ── 요청 조립 — 미검증·낡은 core·어긋난 프로파일은 여기서 걸린다 ───────────────


def build_confirmed_request(
    *,
    export_id: str,
    destination_dir: str,
    project_id: str,
    package: PackageRevision,
    review: ReviewRevision,
    artifacts: Sequence[ClipArtifact],
    assets: Mapping[str, SourceAsset],
    selected_segment_ids: frozenset[str] | None,
    profile_name: str,
    handles_before: str,
    handles_after: str,
    tool_provenance: str,
    created_at: str,
) -> ConfirmedExportRequest:
    """검증된 조각들로 확정 요청을 만든다 — 전체든 선택이든 시간순은 그대로다."""
    _require_identity(export_id, destination_dir)
    _require_chosen_extraction(package, profile_name, handles_before, handles_after)
    segments = _selected_segments(review, selected_segment_ids)
    by_segment = _artifacts_by_segment(artifacts, package)
    clips = tuple(
        _confirmed_clip(review, segment, by_segment, assets, package, index)
        for index, segment in enumerate(segments, start=1)
    )
    if not clips:
        raise ConfirmedExportError("확정할 조각이 하나도 없다 — 빈 확정은 성공이 아니다")
    _require_chronology(clips)
    return ConfirmedExportRequest(
        export_id=export_id,
        destination_dir=destination_dir,
        project_id=project_id,
        plan_revision_id=package.plan_revision_id,
        order_revision_id=package.order_revision_id,
        review_revision_id=review.revision_id,
        review_fingerprint=_review_fingerprint(review.revision_id, segments),
        profile_name=package.profile_name,
        handles_before=package.handles_before,
        handles_after=package.handles_after,
        settings_digest=EXPORT_SETTINGS_DIGEST,
        tool_provenance=tool_provenance,
        created_at=created_at,
        clips=clips,
    )


def _require_identity(export_id: str, destination_dir: str) -> None:
    if not _SAFE_ID.fullmatch(export_id):
        raise PathSafetyError(f"확정 신원이 안전한 토큰이 아니다: {export_id!r}")
    if not _DESTINATION_DIR.fullmatch(destination_dir):
        raise PathSafetyError(
            f"확정 목적지는 clips/confirmed_* 이름공간이어야 한다: {destination_dir!r}"
        )


def _require_chosen_extraction(
    package: PackageRevision, profile_name: str, handles_before: str, handles_after: str
) -> None:
    """화면 3에서 고른 값이 package가 뽑힌 값과 같은가 — 다르면 확정이 거짓이 된다."""
    for label, value in (("앞 핸들", handles_before), ("뒤 핸들", handles_after)):
        try:
            Fraction(value)
        except (ValueError, ZeroDivisionError) as error:
            raise ConfirmedExportError(
                f"{label}이 정확 유리수 문자열이 아니다: {value!r}"
            ) from error
    if (profile_name, handles_before, handles_after) != (
        package.profile_name,
        package.handles_before,
        package.handles_after,
    ):
        raise ProfileMismatchError(
            f"고른 프로파일·핸들이 package와 다르다: {profile_name}/{handles_before}/"
            f"{handles_after} ≠ package의 {package.profile_name}/{package.handles_before}/"
            f"{package.handles_after}"
        )


def _selected_segments(
    review: ReviewRevision, selected: frozenset[str] | None
) -> tuple[ReviewSegment, ...]:
    """선택(또는 전체) 조각을 시간순으로 — 원본 순서는 검토 리비전의 것이다."""
    known = {segment.segment_id for segment in review.segments}
    if selected is not None and not selected <= known:
        missing = ", ".join(sorted(selected - known))
        raise ConfirmedExportError(f"검토 리비전에 없는 조각을 확정하려 한다: {missing}")
    chosen = [
        segment
        for segment in review.segments
        if segment.selection == "included" and (selected is None or segment.segment_id in selected)
    ]
    if not chosen:
        raise ConfirmedExportError("사용할 컷이 하나도 없다 — 컷을 복원한 뒤 내보내세요")
    return tuple(sorted(chosen, key=lambda s: (s.source_order, s.start_pts, s.segment_order)))


def _artifacts_by_segment(
    artifacts: Sequence[ClipArtifact], package: PackageRevision
) -> dict[str, ClipArtifact]:
    by_segment: dict[str, ClipArtifact] = {}
    for artifact in artifacts:
        _require_artifact(artifact, package)
        if artifact.segment_id in by_segment:
            raise ConfirmedExportError(
                f"조각 {artifact.segment_id!r}의 artifact가 둘 이상이다 — 하나만 있어야 한다"
            )
        by_segment[artifact.segment_id] = artifact
    return by_segment


def _require_artifact(artifact: ClipArtifact, package: PackageRevision) -> None:
    """확정에 쓸 artifact의 자격 — 게시됨·해시 있음·양수 크기·같은 package."""
    if artifact.status != "published":
        raise SourceMediaError(
            f"검증되지 않은 조각은 확정하지 못한다: {artifact.clip_id} ({artifact.status})"
        )
    if artifact.content_sha256 is None:
        raise SourceMediaError(f"조각 {artifact.clip_id!r}에 기록된 해시가 없다")
    if (artifact.size_bytes or 0) <= 0:
        raise SourceMediaError(f"조각 {artifact.clip_id!r}의 크기가 0바이트다")
    if artifact.package_dir != package.package_dir:
        raise SourceMediaError(
            f"다른 package의 조각이다: {artifact.package_dir!r} ≠ {package.package_dir!r}"
        )
    if artifact.profile_name != package.profile_name:
        raise ProfileMismatchError(
            f"조각 {artifact.clip_id!r}이 package와 다른 프로파일로 뽑혔다: "
            f"{artifact.profile_name!r} ≠ {package.profile_name!r}"
        )
    _require_relative_within_clips(artifact.relative_path, artifact.clip_id)


def _require_relative_within_clips(relative_path: str, owner: str) -> None:
    """artifact의 작업공간 상대 경로 — `clips/` 안의 이탈 없는 상대 경로만."""
    parts = relative_path.split("/")
    if os.path.isabs(relative_path) or ".." in parts or "." in parts or "" in parts:
        raise PathSafetyError(f"{owner}의 경로가 상대 경로 계약을 벗어났다: {relative_path!r}")
    if parts[0] != "clips":
        raise PathSafetyError(f"{owner}의 경로가 clips/ 안이 아니다: {relative_path!r}")


def _confirmed_clip(
    review: ReviewRevision,
    segment: ReviewSegment,
    by_segment: Mapping[str, ClipArtifact],
    assets: Mapping[str, SourceAsset],
    package: PackageRevision,
    index: int,
) -> ConfirmedClip:
    """검토 조각 하나를 확정 조각으로 — 낡은 core는 여기서 정직하게 걸린다."""
    artifact = by_segment.get(segment.segment_id)
    if artifact is None:
        raise StaleCoreError(
            f"stale_core: 조각 {segment.segment_id!r}의 검증된 artifact가 없다 — "
            "다시 추출·검증된 조각이 필요하다"
        )
    _require_fresh_core(review, segment, artifact)
    asset, source_sha256 = _require_source_asset(assets, segment)
    content_sha256 = _required_sha(artifact)
    return ConfirmedClip(
        export_index=index,
        export_relative_path=f"{MEDIA_DIR_NAME}/{_export_filename(artifact, index)}",
        clip_id=artifact.clip_id,
        segment_id=artifact.segment_id,
        source_id=artifact.asset_id,
        source_name=asset.source_name,
        source_sha256=source_sha256,
        source_order=artifact.source_order,
        segment_order=artifact.segment_order,
        time_base_num=artifact.time_base_num,
        time_base_den=artifact.time_base_den,
        core_start_pts=artifact.core_start_pts,
        core_end_pts=artifact.core_end_pts,
        media_start_pts=artifact.media_start_pts,
        media_end_pts=artifact.media_end_pts,
        media_local_duration=artifact.media_local_duration,
        frame_count=artifact.frame_count,
        profile_name=artifact.profile_name,
        handles_before=package.handles_before,
        handles_after=package.handles_after,
        origin_package_dir=artifact.package_dir,
        origin_relative_path=artifact.relative_path,
        content_sha256=content_sha256,
        size_bytes=artifact.size_bytes or 0,
        container=Path(artifact.relative_path).suffix,
    )


def _require_source_asset(
    assets: Mapping[str, SourceAsset], segment: ReviewSegment
) -> tuple[SourceAsset, str]:
    """확정 조각의 원본 자산과 그 해시 — 기록·검증 상태를 여기서 좁혀 돌려준다."""
    asset = assets.get(segment.source_id)
    if asset is None:
        raise SourceMediaError(f"원본 자산 기록이 없다: {segment.source_id}")
    if asset.status != "ready" or asset.content_sha256 is None:
        raise SourceMediaError(f"원본 자산이 검증 준비 상태가 아니다: {segment.source_id}")
    return asset, asset.content_sha256


def _export_filename(artifact: ClipArtifact, index: int) -> str:
    """확정 폴더 안 파일 이름 — 병렬 번호·원본 좌표 밀리초·신원 지문(§9.1 모양).

    원본 이름은 기록(clips.jsonl·CSV)으로 남고 파일 이름은 번호·틱·지문만으로
    정한다 — 이름이 곧 신원이 아니다(§9.1).
    """
    time_base = Fraction(artifact.time_base_num, artifact.time_base_den)
    digest = _clip_fingerprint(artifact)
    container = Path(artifact.relative_path).suffix
    if len(container) < 2:
        raise SourceMediaError(f"조각 {artifact.clip_id!r}에 컨테이너 확장자가 없다")
    return (
        f"{index:06d}__t{_ceil_ms(Fraction(artifact.core_start_pts) * time_base):09d}"
        f"-t{_ceil_ms(Fraction(artifact.core_end_pts) * time_base):09d}"
        f"__c_{digest[:4]}{container}"
    )


def _required_sha(artifact: ClipArtifact) -> str:
    """artifact의 해시 — 자격 검문을 통과한 값을 여기선 타입만 좁혀 돌려준다."""
    if artifact.content_sha256 is None:
        raise SourceMediaError(f"조각 {artifact.clip_id!r}에 기록된 해시가 없다")
    return artifact.content_sha256


def _require_fresh_core(
    review: ReviewRevision, segment: ReviewSegment, artifact: ClipArtifact
) -> None:
    """artifact가 바로 이 core를 위해 뽑·검증된 것인가 — 경계가 움직였으면 못 쓴다."""
    if artifact.asset_id != segment.source_id:
        raise StaleCoreError(f"stale_core: 조각 {segment.segment_id!r}의 artifact가 다른 원본이다")
    clock = review.clock_for(segment.source_id)
    if clock is None:
        raise ConfirmedExportError(f"검토 리비전에 원본 {segment.source_id!r}의 시계가 없다")
    if (artifact.time_base_num, artifact.time_base_den) != (
        clock.time_base_num,
        clock.time_base_den,
    ):
        raise StaleCoreError(
            f"stale_core: 조각 {segment.segment_id!r}의 timebase가 검토 시계와 다르다"
        )
    if (artifact.core_start_pts, artifact.core_end_pts) != (
        segment.start_pts,
        segment.end_pts,
    ):
        raise StaleCoreError(
            f"stale_core: 조각 {segment.segment_id!r} core [{segment.start_pts}, "
            f"{segment.end_pts}]가 검증 때 [{artifact.core_start_pts}, "
            f"{artifact.core_end_pts}]와 다르다 — 다시 추출·검증된 조각이 필요하다"
        )
    if artifact.media_start_pts > segment.start_pts or artifact.media_end_pts < segment.end_pts:
        raise SourceMediaError(f"조각 {artifact.clip_id!r}의 매체 범위가 core를 덮지 못한다")


def _require_chronology(clips: Sequence[ConfirmedClip]) -> None:
    """선택으로 걸러도 순서는 원본의 것이다 — 순번 겹침·역순은 계약 위반."""
    orders = [(clip.source_order, clip.segment_order) for clip in clips]
    if orders != sorted(orders) or len(set(orders)) != len(orders):
        raise ConfirmedExportError(f"조각 순서가 시간순이 아니다: {orders}")
    indexes = [clip.export_index for clip in clips]
    if indexes != list(range(1, len(clips) + 1)):
        raise ConfirmedExportError(f"병렬 번호가 1부터 이어지지 않는다: {indexes}")


def _review_fingerprint(revision_id: str, segments: Sequence[ReviewSegment]) -> str:
    """선택·경계까지 묶은 검토 지문 — 검토가 바뀌면 확정 지문도 갈라진다."""
    payload = {
        "revision": revision_id,
        "segments": [
            {
                "id": segment.segment_id,
                "source": segment.source_id,
                "core": [segment.start_pts, segment.end_pts],
                "selection": segment.selection,
            }
            for segment in segments
        ],
    }
    return _sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _clip_fingerprint(artifact: ClipArtifact) -> str:
    """확정 파일 이름의 `c_` 몫 — 신원·순서·경계·timebase를 묶는 sha256."""
    payload = json.dumps(
        {
            "clip_id": artifact.clip_id,
            "source_order": artifact.source_order,
            "segment_order": artifact.segment_order,
            "core": [artifact.core_start_pts, artifact.core_end_pts],
            "time_base": [artifact.time_base_num, artifact.time_base_den],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return _sha256_text(payload)


def export_fingerprint(request: ConfirmedExportRequest) -> str:
    """요청 전체의 지문 — 이미 있는 확정본과 '정확히 같은 요청'인가의 근거."""
    payload = {
        "schema": CONFIRMED_SCHEMA_VERSION,
        "settings": request.settings_digest,
        "export_id": request.export_id,
        "destination_dir": request.destination_dir,
        "project_id": request.project_id,
        "plan_revision_id": request.plan_revision_id,
        "order_revision_id": request.order_revision_id,
        "review": {
            "revision_id": request.review_revision_id,
            "fingerprint": request.review_fingerprint,
        },
        "profile": request.profile_name,
        "handles": {"before": request.handles_before, "after": request.handles_after},
        "tool": request.tool_provenance,
        "created_at": request.created_at,
        "clips": [dataclasses.asdict(clip) for clip in request.clips],
    }
    return _sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


# ── 복사·스테이징·게시 ───────────────────────────────────────────────────────


def export_confirmed_package(
    workspace_root: Path, request: ConfirmedExportRequest
) -> ConfirmedExportReport:
    """요청 하나를 확정 폴더로 게시한다 — 실패하면 스테이징만 정리하고 손대지 않는다."""
    root = _require_real_directory(workspace_root, "작업공간 뿌리")
    destination = root.joinpath(*request.destination_dir.split("/"))
    parent = destination.parent
    _require_real_directory(parent, "확정 목적지의 부모", root=root)
    if destination.is_symlink():
        raise PathSafetyError(f"확정 자리가 심볼릭 링크다: {destination}")
    if destination.exists():
        return _verify_existing(destination, request)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{request.export_id}.", suffix=".confirmed-staging", dir=parent)
    )
    try:
        _stage_export(root, staging, request)
        report = _verify_directory(staging, request)
        _freeze_directory(staging)
        published = _publish(staging, destination, request)
        if not published:
            # 진 사람은 치운다 — 이긴 쪽이 같은 요청을 이미 게시했다(멱등 검증 통과).
            _discard_staging(staging)
    except BaseException:
        _discard_staging(staging)
        raise
    mode = PUBLISH_MODE_PUBLISHED if published else PUBLISH_MODE_VERIFIED_EXISTING
    return dataclasses.replace(report, mode=mode)


def _stage_export(root: Path, staging: Path, request: ConfirmedExportRequest) -> None:
    """스테이징을 채운다 — 복사본 세 개의 전달물과 그 해시를 실은 매니페스트."""
    media = staging / MEDIA_DIR_NAME
    media.mkdir()
    for clip in request.clips:
        _copy_verified_media(root, media, clip)
    bodies = (
        (CLIPS_JSONL_NAME, _clips_jsonl(request).encode("utf-8")),
        (CLIP_INDEX_NAME, _clip_index_csv(request).encode("utf-8-sig")),
        (README_NAME, _readme_text(request).encode("utf-8")),
    )
    for name, body in bodies:
        (staging / name).write_bytes(body)
    manifest = _manifest_payload(
        request, {name: _sha256_file(staging / name) for name, _ in bodies}
    )
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
    (staging / MANIFEST_NAME).write_bytes(payload.encode("utf-8"))


def _copy_verified_media(root: Path, media_dir: Path, clip: ConfirmedClip) -> None:
    """조각 하나를 진짜 복사로 옮긴다 — 앞뒤 stat+SHA, 목적지 재독출 SHA."""
    source = root.joinpath(*clip.origin_relative_path.split("/"))
    _require_tree_without_symlinks(root, clip.origin_relative_path)
    before = _stat_source(source, clip, "복사 전")
    _require_content_sha(source, clip.content_sha256, f"{clip.clip_id} 복사 전")
    target = media_dir / Path(clip.export_relative_path).name
    _copy_bytes(source, target)
    after = _stat_source(source, clip, "복사 후")
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise SourceMediaError(f"복사하는 동안 원본이 바뀌었다: {clip.origin_relative_path}")
    _require_content_sha(source, clip.content_sha256, f"{clip.clip_id} 복사 후")
    if _sha256_file(target) != clip.content_sha256:
        raise SourceMediaError(f"목적지 재독출 해시가 어긋난다: {clip.clip_id!r}")
    if target.stat().st_size != clip.size_bytes:
        raise SourceMediaError(f"목적지 크기가 어긋난다: {clip.clip_id!r}")


def _copy_bytes(source: Path, target: Path) -> None:
    """바이트를 읽어 쓴다 — 하드링크가 아니라 갈라진 복사다(옛 원본과 독립)."""
    with open(source, "rb") as reader, open(target, "xb") as writer:
        for block in iter(lambda: reader.read(1 << 20), b""):
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())


def _stat_source(source: Path, clip: ConfirmedClip, phase: str) -> os.stat_result:
    """원본의 현재 상태 — 심볼릭 링크를 열지 않고(링크 거부) 크기까지만 본다."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, os.O_RDONLY | no_follow)
    except OSError as error:
        raise PathSafetyError(
            f"원본을 열지 못한다({phase}): {source} — {error.strerror}"
        ) from error
    try:
        facts = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not S_ISREG(facts.st_mode):
        raise PathSafetyError(f"원본이 일반 파일이 아니다({phase}): {source}")
    if facts.st_size != clip.size_bytes:
        raise SourceMediaError(
            f"원본 크기가 검증 기록과 다르다({phase}): {clip.clip_id!r} "
            f"{clip.size_bytes} → {facts.st_size}B"
        )
    return facts


def _require_content_sha(source: Path, expected: str, label: str) -> None:
    """원본을 다시 읽은 해시 — 검증 기록과 어긋나면 확정의 밑천이 아니다."""
    if _sha256_file(source) != expected:
        raise SourceMediaError(f"원본 해시가 검증 기록과 다르다: {label}")


def _require_tree_without_symlinks(root: Path, relative_path: str) -> None:
    """상대 경로의 모든 이음초가 실제 디렉터리인가 — 링크 경유 유입을 막는다."""
    parts = relative_path.split("/")
    walked = root
    for part in parts:
        walked = walked / part
        if walked.is_symlink():
            raise PathSafetyError(f"경로에 심볼릭 링크가 끼어 있다: {walked}")


def _freeze_directory(staging: Path) -> None:
    """스테이징을 얼린다 — 파일 0444·디렉터리 0555, 게시 뒤에는 못 고친다."""
    for path in sorted(staging.rglob("*")):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(staging, 0o555)


def _publish(staging: Path, destination: Path, request: ConfirmedExportRequest) -> bool:
    """이름바꾸기 한 번으로 게시 — 그 사이 자리가 생겼으면 멱등 검증에 맡긴다."""
    try:
        os.rename(staging, destination)
    except OSError as error:
        # macOS은 겹치는 자리를 ENOTEMPTY 대신 EACCES로 알리기도 한다 — 자리가
        # 실제로 생겼을 때만 경합으로 읽는다(없으면 진짜 권한 오류니 끝낸다).
        collided = error.errno in (
            errno.EEXIST,
            errno.ENOTEMPTY,
            errno.ENOTDIR,
            errno.EISDIR,
            errno.EACCES,
            errno.EPERM,
        ) and os.path.lexists(destination)
        if not collided:
            raise
        _verify_existing(destination, request)
        return False
    return True


def _discard_staging(staging: Path) -> None:
    """확정 스테이징만 지운다 — 이름이 그 모양이 아니면 손대지 않는다(오인 삭제 방지)."""
    if not _STAGING_NAME.fullmatch(staging.name):
        raise PathSafetyError(f"스테이징 이름이 아닌 디렉터리는 지우지 않는다: {staging.name}")
    if not staging.exists():
        return
    for path in sorted(staging.rglob("*"), reverse=True):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    os.chmod(staging, 0o700)
    shutil.rmtree(staging)


def clean_abandoned_staging(workspace_root: Path, export_id: str) -> int:
    """이 확정 신원의 버려진 스테이징만 지운다 — 아이디가 정확히 같은 것만."""
    if not _SAFE_ID.fullmatch(export_id):
        raise PathSafetyError(f"확정 신원이 안전한 토큰이 아니다: {export_id!r}")
    root = _require_real_directory(workspace_root, "작업공간 뿌리")
    parent = root / "clips"
    _require_real_directory(parent, "확정 스테이징의 부모", root=root)
    pattern = re.compile(rf"^\.{re.escape(export_id)}\.[0-9a-z_]{{8}}\.confirmed-staging$")
    removed = 0
    for entry in parent.iterdir():
        if pattern.fullmatch(entry.name):
            _discard_staging(entry)
            removed += 1
    return removed


# ── 검증 — 게시 전 스테이징과 게시 뒤 폴더가 같은 눈으로 본다 ─────────────────


def verify_confirmed_export(
    workspace_root: Path, destination_dir: str, *, expected: ConfirmedExportRequest | None = None
) -> ConfirmedExportReport:
    """확정 폴더를 읽기만으로 다시 검증한다 — 캐시 삭제·작업공간 이동 뒤의 검증."""
    root = _require_real_directory(workspace_root, "작업공간 뿌리")
    if not _DESTINATION_DIR.fullmatch(destination_dir):
        raise PathSafetyError(
            f"확정 목적지는 clips/confirmed_* 이름공간이어야 한다: {destination_dir!r}"
        )
    destination = root.joinpath(*destination_dir.split("/"))
    if destination.is_symlink() or not destination.is_dir():
        raise PathSafetyError(f"확정 폴더가 실제 디렉터리가 아니다: {destination}")
    report = _verify_directory(destination, expected)
    if expected is not None:
        manifest = _manifest_json(destination)
        if manifest["export_fingerprint"] != export_fingerprint(expected):
            raise ExportExistsError(
                f"확정본의 지문이 요청과 다르다: {destination_dir} — 덮어쓰지 않는다"
            )
    return report


def _verify_existing(destination: Path, request: ConfirmedExportRequest) -> ConfirmedExportReport:
    """이미 있는 자리 — 지문이 같아야 멱등 검증, 다르면 덮어쓰지 않고 거부한다."""
    if destination.is_symlink() or not destination.is_dir():
        raise ExportExistsError(f"확정 자리가 폴더가 아니다: {destination}")
    manifest = _manifest_json(destination)
    if manifest["export_fingerprint"] != export_fingerprint(request):
        raise ExportExistsError(
            f"이미 확정된 폴더이고 요청 지문이 다르다 — 덮어쓰지 않는다: {request.destination_dir}"
        )
    report = _verify_directory(destination, request)
    return dataclasses.replace(report, mode=PUBLISH_MODE_VERIFIED_EXISTING)


def _verify_directory(
    directory: Path, request: ConfirmedExportRequest | None
) -> ConfirmedExportReport:
    """매니페스트를 다시 읽어 폴더 전체를 검증 — 참조·스키마·해시·수·순서."""
    manifest = _manifest_json(directory)
    _require_manifest_shape(manifest)
    if request is not None:
        if (manifest["export_id"], manifest["destination_dir"]) != (
            request.export_id,
            request.destination_dir,
        ):
            raise ExportExistsError("확정본의 신원이 요청과 다르다")
        if manifest["clips"] != _manifest_clips(request):
            raise ExportExistsError("확정본의 조각 기록이 요청과 다르다")
    # 참조의 안전성을 먼저 본다 — 이탈 경로를 비교·조회에 끌어들이지 않는다.
    for name in manifest["files"]:
        _require_artifact_name(name)
    for clip in manifest["clips"]:
        _require_export_relative(clip["export_relative_path"])
    expected = {MANIFEST_NAME, *manifest["files"]} | {
        clip["export_relative_path"] for clip in manifest["clips"]
    }
    if manifest["clips"]:
        expected.add(MEDIA_DIR_NAME)
    found = _walk_real_files(directory)
    if found != expected:
        detail = f"없음 {sorted(expected - found)} · 남음 {sorted(found - expected)}"
        raise ConfirmedExportError(f"확정 폴더의 파일이 매니페스트와 다르다: {detail}")
    for name, digest in manifest["files"].items():
        _require_content_sha(directory / name, digest, name)
    return _verify_clip_records(directory, manifest)


def _verify_clip_records(directory: Path, manifest: dict) -> ConfirmedExportReport:
    """매니페스트의 조각 기록을 하나씩 — 해시·크기·순서·병렬 번호."""
    total_bytes = 0
    previous: tuple[int, int] | None = None
    for position, clip in enumerate(manifest["clips"], start=1):
        _require_export_relative(clip["export_relative_path"])
        path = directory.joinpath(*clip["export_relative_path"].split("/"))
        _require_content_sha(path, clip["content_sha256"], clip["clip_id"])
        if path.stat().st_size != clip["size_bytes"]:
            raise ConfirmedExportError(f"조각 크기가 기록과 다르다: {clip['clip_id']}")
        order = (clip["source_order"], clip["segment_order"])
        if previous is not None and order <= previous:
            raise ConfirmedExportError(f"조각 순서가 시간순이 아니다: {clip['clip_id']}")
        previous = order
        if clip["export_index"] != position:
            raise ConfirmedExportError(f"병렬 번호가 이어지지 않는다: {clip['clip_id']}")
        total_bytes += clip["size_bytes"]
    if manifest["clip_count"] != len(manifest["clips"]):
        raise ConfirmedExportError("조각 수가 매니페스트 기록과 다르다")
    return ConfirmedExportReport(
        export_id=manifest["export_id"],
        destination_dir=manifest["destination_dir"],
        clip_count=len(manifest["clips"]),
        total_bytes=total_bytes,
        total_duration=manifest["total_duration"],
        mode="verified",
    )


def _manifest_json(directory: Path) -> dict:
    """매니페스트를 읽는다 — 없거나 JSON이 아니면 정직하게 거부한다."""
    path = directory / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        raise ConfirmedExportError(f"확정 매니페스트가 없다: {path}")
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfirmedExportError(f"확정 매니페스트를 읽지 못한다: {error}") from error
    if not isinstance(payload, dict):
        raise ConfirmedExportError("확정 매니페스트가 JSON 객체가 아니다")
    return payload


def _require_manifest_shape(manifest: dict) -> None:
    """스키마와 필수 칸 — 이 모듈이 쓰는 모양 밖은 거부한다(재독기의 좁은 문법)."""
    required = (
        "schema_version",
        "export_fingerprint",
        "export_id",
        "destination_dir",
        "files",
        "clips",
        "clip_count",
        "total_duration",
    )
    if manifest.get("schema_version") != CONFIRMED_SCHEMA_VERSION:
        raise ConfirmedExportError(f"모르는 확정 스키마다: {manifest.get('schema_version')!r}")
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ConfirmedExportError(f"매니페스트에 필수 칸이 없다: {', '.join(missing)}")
    if not isinstance(manifest["clips"], list) or not isinstance(manifest["files"], dict):
        raise ConfirmedExportError("매니페스트의 files·clips 모양이 아니다")


def _require_artifact_name(name: str) -> None:
    """전달물 파일 이름 — 한 겹짜리 이름만, 경로 구분·이탈이 없어야 한다."""
    if os.path.isabs(name) or "/" in name or name in (".", "..") or not name:
        raise PathSafetyError(f"전달물 이름이 계약을 벗어났다: {name!r}")


def _require_export_relative(relative_path: str) -> None:
    """폴더 안 상대 경로 — media/ 한 겹, 이탈 없음(재독기의 경로 검문)."""
    parts = relative_path.split("/")
    if (
        os.path.isabs(relative_path)
        or len(parts) != 2
        or parts[0] != MEDIA_DIR_NAME
        or ".." in parts
        or any(not part for part in parts)
    ):
        raise PathSafetyError(f"확정 조각 경로가 계약을 벗어났다: {relative_path!r}")


def _walk_real_files(directory: Path) -> set[str]:
    """폴더를 훑는다 — 심볼릭 링크가 끼어 있으면 그 자리에서 거부한다."""
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        here = Path(dirpath)
        for name in dirnames + filenames:
            if (here / name).is_symlink():
                raise PathSafetyError(f"확정 폴더 안에 심볼릭 링크가 있다: {here / name}")
            found.add((here / name).relative_to(directory).as_posix())
    return found


def _require_real_directory(directory: Path, label: str, root: Path | None = None) -> Path:
    """실제 디렉터리인가 — 심볼릭 링크·없음·작업공간 밖은 다 거부한다."""
    if directory.is_symlink():
        raise PathSafetyError(f"{label}이(가) 심볼릭 링크다: {directory}")
    if not directory.is_dir():
        raise PathSafetyError(f"{label}이(가) 실제 디렉터리가 아니다: {directory}")
    if root is not None:
        resolved_root = root.resolve()
        resolved = directory.resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise PathSafetyError(f"{label}이(가) 작업공간 밖이다: {directory}")
    return directory


# ── 전달물 직렬화 — 매니페스트가 곧 기록의 정본이다 ──────────────────────────


def _manifest_payload(request: ConfirmedExportRequest, artifact_hashes: dict[str, str]) -> dict:
    """매니페스트 몸통 — 지문·신원·전달물 해시·조각 기록 전부."""
    return {
        "schema_version": CONFIRMED_SCHEMA_VERSION,
        "export_fingerprint": export_fingerprint(request),
        "export_id": request.export_id,
        "destination_dir": request.destination_dir,
        "project_id": request.project_id,
        "plan_revision_id": request.plan_revision_id,
        "order_revision_id": request.order_revision_id,
        "review": {
            "revision_id": request.review_revision_id,
            "fingerprint": request.review_fingerprint,
        },
        "profile_name": request.profile_name,
        "handles": {"before": request.handles_before, "after": request.handles_after},
        "settings_digest": request.settings_digest,
        "tool_provenance": request.tool_provenance,
        "created_at": request.created_at,
        "clip_count": len(request.clips),
        "total_bytes": sum(clip.size_bytes for clip in request.clips),
        "total_duration": _total_duration(request),
        "files": dict(artifact_hashes),
        "clips": _manifest_clips(request),
    }


def _manifest_clips(request: ConfirmedExportRequest) -> list[dict]:
    """조각 기록 — 매니페스트와 `clips.jsonl`이 같은 몸을 공유한다(정수 틱 그대로)."""
    return [
        {
            "export_index": clip.export_index,
            "export_relative_path": clip.export_relative_path,
            "clip_id": clip.clip_id,
            "segment_id": clip.segment_id,
            "source_id": clip.source_id,
            "source_filename": clip.source_name,
            "source_sha256": clip.source_sha256,
            "source_order": clip.source_order,
            "segment_order": clip.segment_order,
            "time_base": {"num": clip.time_base_num, "den": clip.time_base_den},
            "core_range_pts": [clip.core_start_pts, clip.core_end_pts],
            "media_range_pts": [clip.media_start_pts, clip.media_end_pts],
            "media_local_duration": clip.media_local_duration,
            "frame_count": clip.frame_count,
            "profile_name": clip.profile_name,
            "handles": {"before": clip.handles_before, "after": clip.handles_after},
            "origin": {
                "package_dir": clip.origin_package_dir,
                "relative_path": clip.origin_relative_path,
            },
            "content_sha256": clip.content_sha256,
            "size_bytes": clip.size_bytes,
            "status": CONFIRMED_CLIP_STATUS,
        }
        for clip in request.clips
    ]


def _clips_jsonl(request: ConfirmedExportRequest) -> str:
    """`clips.jsonl` — 정수 틱 좌표의 불변 스냅샷(§13.3), 한 조각 한 줄."""
    lines = []
    for record in _manifest_clips(request):
        line = {
            "schema_version": CONFIRMED_SCHEMA_VERSION,
            "export_id": request.export_id,
            **record,
        }
        lines.append(json.dumps(line, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + "\n"


def _clip_index_csv(request: ConfirmedExportRequest) -> str:
    """`clip_index.csv` — §9.3 머리 순서 그대로, 모든 칸은 수식 주입 방지를 지난다."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for clip in request.clips:
        writer.writerow(_csv_row(clip))
    return buffer.getvalue()


def _csv_row(clip: ConfirmedClip) -> dict[str, str]:
    """CSV 한 줄 — 상대 경로는 폴더 기준, 시각 표기는 틱에서 온 고정소수(§13.3)."""
    time_base = clip.time_base
    row = {
        "source_order": str(clip.source_order),
        "segment_order": str(clip.segment_order),
        "clip_id": clip.clip_id,
        "relative_path": clip.export_relative_path,
        "source_id": clip.source_id,
        "source_filename": clip.source_name,
        "core_start": _seconds_text(Fraction(clip.core_start_pts) * time_base),
        "core_end": _seconds_text(Fraction(clip.core_end_pts) * time_base),
        "media_start": _seconds_text(Fraction(clip.media_start_pts) * time_base),
        "media_end": _seconds_text(Fraction(clip.media_end_pts) * time_base),
        "duration": _seconds_text(Fraction(clip.media_local_duration)),
        "speech_summary": "",
        "visual_summary": "",
        "tags": "",
        "review_status": CONFIRMED_CLIP_STATUS,
    }
    return {key: formula_safe_cell(value) for key, value in row.items()}


def _readme_text(request: ConfirmedExportRequest) -> str:
    """사용 안내 — 무엇이 어떤 순서로 얼려 있는지, Final Cut 가져가기(§9.4)."""
    total = _total_duration(request)
    sources = len({clip.source_id for clip in request.clips})
    lines = [
        "# 확정 편집용 조각 폴더",
        "",
        f"- 확정 내보내기: `{request.export_id}` → `{request.destination_dir}`",
        f"- 프로젝트 `{request.project_id}` · 분할 계획 `{request.plan_revision_id}`"
        f" · 전체 순서 `{request.order_revision_id}`",
        f"- 검토 리비전 `{request.review_revision_id}` (지문 `{request.review_fingerprint[:12]}`)",
        f"- 프로파일 `{request.profile_name}` · 핸들: 앞 {request.handles_before}초 /"
        f" 뒤 {request.handles_after}초",
        f"- 원본 {sources}건 · 조각 {len(request.clips)}건 · 총 길이 {total}초",
        f"- 확정 시각: {request.created_at}",
        "",
        "`media/` 안 파일 이름의 앞 번호는 확정 순서의 병렬 번호다 — 원본 순서와 조각",
        "번호는 기록 그대로 남아 있고 다시 붙이지 않았다. 목록의 정본 기록은",
        "`clips.jsonl`과 `clip_index.csv`다.",
        "",
        "이 폴더는 얼어 붙었다 — 파일을 고치거나 지우지 말 것. 앱을 종료하거나 캐시를",
        "지우거나 작업공간을 옮겨도 그대로 남아 있고, `confirmed_manifest.json`의 해시로",
        "다시 검증된다.",
        "",
        "## Final Cut Pro로 가져가기",
        "",
        "1. Finder에서 이 폴더를 연다.",
        "2. 폴더나 파일을 Final Cut의 이벤트로 가져온다(가져오기는 Finder 흐름을 쓴다).",
        "3. 정렬은 이름으로 확인한다 — 번호가 있어도 앱이 원하는 타임라인을 자동으로",
        "   만들지는 않는다(§9.4).",
    ]
    return "\n".join(lines) + "\n"


def _total_duration(request: ConfirmedExportRequest) -> str:
    """전체 길이 — 매체 국소 길이(정확 유리수 문자열)의 합을 6자리 고정소수로."""
    total = sum((Fraction(clip.media_local_duration) for clip in request.clips), Fraction(0))
    return _seconds_text(total)


def _ceil_ms(seconds: Fraction) -> int:
    """정확한 유리수 초를 밀리초 정수로(천장) — 파일 이름 표기, 근거는 틱이다."""
    scaled = seconds * 1000
    return -((-scaled.numerator) // scaled.denominator)


def _seconds_text(value: Fraction) -> str:
    """정확한 유리수 초를 6자리 고정소수로 — 전달물 표기, 근거는 틱에 있다(§13.3)."""
    scaled = value * 1_000_000
    whole = scaled.numerator // scaled.denominator
    return f"{whole // 1_000_000}.{whole % 1_000_000:06d}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
