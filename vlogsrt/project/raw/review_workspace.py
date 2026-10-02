"""A5/A6 검토의 지속 작업공간 파사드 — 계획·package·분석 증거를 하나로 잇는다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §6·§9·§10·§12·§13·16-A5/A6.
받아들인 순수 라이브러리(`segment_review`·`review_projection`·`confirmed_package`·
`cache_policy`)와 A3 서비스 계획·추출 위에 **검토의 지속 상태**를 얹는 백엔드다:
초기화·다시 열기·편집(stale-head 가드)·재추출 판정·편집 덮개의 새 계획 환원·
측정 VTT·확정 내보내기·관리 정리·미리보기 복원. 유료 API가 없다 — ffmpeg는
원본 시계 재측정과 A3 추출(서비스 경유)에서만, 전부 로컬이다.

지키는 선:

- **리비전은 A5 순수 도메인의 것이다.** 이 모듈은 리비전을 만들고 저장할 뿐,
  고쳐 쓰지 않는다. 편집 종류도 여섯 종류(`segment_review.EDIT_KINDS`) 그대로다.
- **출판은 원자적이다.** 새 리비전 행과 헤드 이동(초기 출판이면 첫 헤드·입양
  매핑까지)은 SQLite 한 트랜잭션에서만 나간다 — 출판이 반쪽으로 끝나면 전부
  없던 일이 되므로 고아 리비전이 다음 편집을 막지 않는다.
- **경계 왕복은 항등이다.** 리비전 경계는 전부 프레임 격자 위의 정수 틱이므로,
  초 구간으로 바꿔 새 계획을 세우면 같은 격자가 돌아온다 — 환원 검증이 매핑의
  자격이다(어긋나면 매핑이 아니라 실패다).
- **시각은 잰 것만 쓴다.** VTT의 발화는 분석이 저장한 실측 증거에서, 창 길이는
  artifact가 잰 미디어 길이에서 온다. 좌표 변환은 문서 축→파일 축 한 번,
  파일 축→클립 국소 한 번(`review_projection`) — 겹쳐 적용하지 않는다(§13.3).
- **확정은 실제 복사다.** `confirmed_package`의 바이트 복사·원자 게시·멱등 검증을
  그대로 부르고, 서버 소유 `export_id`로 `clips/confirmed_reviewexport_*` 이름공간을
  연다. 원본·옛 확정은 손대지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from vlogsrt import preprocess
from vlogsrt.project.raw import cache_policy
from vlogsrt.project.raw import confirmed_package as confirmed
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.jobs import JobContext
from vlogsrt.project.raw.models import (
    SEGMENTATION_BASIS_PLANNED,
    AnalysisRevision,
    AnalysisSourceResult,
    ClipArtifact,
    PackageRevision,
    PlannedInterval,
    RawWorkspaceError,
    SegmentCoverage,
    SegmentPlanEntry,
    SegmentPlanSnapshot,
    SourceAsset,
)
from vlogsrt.project.raw.review_projection import (
    ClipWindow,
    ProjectionClock,
    RenderedVtt,
    TimedUtterance,
    project_review_vtt,
    render_review_vtt,
    timed_utterances,
)
from vlogsrt.project.raw.review_workspace_store import (
    RemappingInsert,
    ReviewWorkspaceStore,
    confirmed_destination_dir,
    new_review_export_id,
)
from vlogsrt.project.raw.segment_review import (
    EDIT_KINDS,
    SELECTION_EXCLUDED,
    SPEECH_ANALYSIS_FAILED,
    SPEECH_DETECTED,
    SPEECH_STATUSES,
    SPEECH_UNCONFIRMED_EMPTY,
    ReviewEdit,
    ReviewRevision,
    ReviewSegment,
    ReviewViewState,
    SourceClock,
    StaleRevisionError,
    apply_edit,
    boundary_edit,
    chronological,
    content_fingerprint,
    filter_segments,
    initial_revision,
    make_source_clock,
    merge_edit,
    note_edit,
    override_edit,
    parse_revision,
    reextract_segment_ids,
    selection_edit,
    serialize_revision,
    split_edit,
)
from vlogsrt.project.raw.workspace import RawWorkspace

if TYPE_CHECKING:  # 순환 방지 — 공개 서비스가 이 파사드를 import한다(W1).
    from vlogsrt.project.raw.service import RawWorkspaceService

REVIEW_VTT_ROOT = "review/vtt"
"""검토용 VTT가 사는 자리 — 캐시 정리 대상이 아니다(보호 이름 `review`)."""

PREVIEW_ROOT = "previews/review_playback"
"""검토 미리보기 복사의 자리 — 지워도 되는 이름으로만 만든다(§9.2).

`previews/` 아래라도 보호 이름(originals·clips·review·…)을 딴 중간 디렉터리는
캐시 정리가 물리적으로 거부한다("보호 이름은 위치를 못 속인다"). 미리보기는
본래 지우고 다시 만드는 물건이므로 보호받지 않는 이름을 쓴다 — 복원은
`restore_previews`가 검증된 매체에서 다시 한다."""


class ReviewWorkspaceError(RawWorkspaceError):
    """검토 작업공간 오류의 기본형 — 이유를 사람에게 그대로 보여준다."""


# ── 얼어 붙은 기록 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LoadedReview:
    """다시 연 검토 — 리비전과 원본 낡음 판정을 함께 돌려준다(숨기지 않는다)."""

    revision: ReviewRevision
    stale_sources: tuple[tuple[str, str], ...]

    @property
    def source_stale(self) -> bool:
        return bool(self.stale_sources)


@dataclass(frozen=True, slots=True)
class MappingPair:
    """매핑 한 쌍 — 같은 core 범위의 옛 조각 ID와 새 계획 조각 ID."""

    old_segment_id: str
    new_segment_id: str
    source_id: str
    core_start_pts: int
    core_end_pts: int


@dataclass(frozen=True, slots=True)
class ReviewPlanMapping:
    """검토 리비전 → 새 A3 계획의 신원 매핑 — 환원 검증을 통과한 쌍들."""

    old_revision_id: str
    new_plan_revision_id: str
    pairs: tuple[MappingPair, ...]

    def as_document(self) -> str:
        """매핑의 정렬된 JSON — 저장소 기록용."""
        return json.dumps(
            {
                "old_revision_id": self.old_revision_id,
                "new_plan_revision_id": self.new_plan_revision_id,
                "pairs": [
                    {
                        "old_segment_id": pair.old_segment_id,
                        "new_segment_id": pair.new_segment_id,
                        "source_id": pair.source_id,
                        "core": [pair.core_start_pts, pair.core_end_pts],
                    }
                    for pair in self.pairs
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class StaleSegment:
    """다시 뽑아야 하는 조각 — 이유까지 남긴다(조용히 넘기지 않는다)."""

    segment_id: str
    source_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewProvenance:
    """조각 하나의 출처 — 원본 original과 추출 preview를 나란히 실는다."""

    segment_id: str
    clip_id: str
    source_id: str
    source_original_relative_path: str
    source_sha256: str
    extracted_preview_relative_path: str
    extracted_sha256: str
    measured_media_duration: str


@dataclass(frozen=True, slots=True)
class ReviewVttOutput:
    """조각 하나의 검토용 자막 — 렌더 본문·출처의 묶음(상태 기록은 본문 안에)."""

    segment_id: str
    clip_id: str
    vtt: RenderedVtt
    provenance: ReviewProvenance


@dataclass(frozen=True, slots=True)
class ConfirmedExportOutcome:
    """확정 내보내기 결과 — 서버 소유 신원과 검증 요약까지."""

    export_id: str
    destination_dir: str
    report: confirmed.ConfirmedExportReport
    export_fingerprint: str


@dataclass(frozen=True, slots=True)
class RestoredPreview:
    """복원된 미리보기 하나 — 실제 파일 경로와 검증된 해시."""

    segment_id: str
    clip_id: str
    relative_path: str
    content_sha256: str
    size_bytes: int


class _PlanReviewEntry:
    """계획 조각 + 그 원본의 발화 상태 — `initial_revision`이 읽는 구조 계약.

    `SegmentPlanEntry`에 없는 `speech_status`·`visual_tags`만 덧붙인다 — 순수
    계획 기록을 고치지 않고 계약을 채운다. 덧붙임이 지나가는 물건이므로 얼지
    않는다: `SegmentPlanSource`가 멤버를 쓰기 가능 속성으로 읽기 때문에 얼어 붙은
    값은 구조 검사(mypy)에서 계약 밖이 된다.
    """

    def __init__(self, entry: SegmentPlanEntry, speech_status: str) -> None:
        self.segment_id = entry.segment_id
        self.asset_id = entry.asset_id
        self.source_order = entry.source_order
        self.segment_order = entry.segment_order
        self.time_base_num = entry.time_base_num
        self.time_base_den = entry.time_base_den
        self.core_start_pts = entry.core_start_pts
        self.core_end_pts = entry.core_end_pts
        self.recommendation = entry.recommendation
        self.speech_status = speech_status
        self.visual_tags: tuple[str, ...] = ()


@dataclass
class _UtteranceItem:
    """저장된 발화 — `timed_utterances`의 구조 계약을 채운 값.

    지나가는 값이라 얼지 않는다(쓰기 가능 속성이어야 구조 계약이 맞는다)."""

    utterance_id: str
    text: str
    start: Fraction
    end: Fraction
    evidence_id: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    """파일 내용의 SHA-256 — 스트리밍으로 읽는다(A3·A6과 같은 규칙)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fraction_from_json(value: object, where: str) -> Fraction:
    """분자·분모 쌍을 정확 유리수로 — 계약 밖 모양은 못 들어온다(§13.3)."""
    if not isinstance(value, Mapping) or set(value) != {"num", "den"}:
        raise ReviewWorkspaceError(f"{where}가 분자·분모 쌍이 아니다: {value!r}")
    numerator, denominator = value["num"], value["den"]
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        raise ReviewWorkspaceError(f"{where}의 분자·분모가 정수가 아니다: {value!r}")
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise ReviewWorkspaceError(f"{where}의 분자·분모가 정수가 아니다: {value!r}")
    if denominator == 0:
        raise ReviewWorkspaceError(f"{where}의 분모가 0이다")
    return Fraction(numerator, denominator)


def _document_value(document: Mapping[str, object], path: Sequence[str]) -> object:
    """문서 안의 값을 단계로 꺼낸다 — 없는 길은 계약 위반으로 못 박는다."""
    walked: object = document
    for key in path:
        if not isinstance(walked, Mapping) or key not in walked:
            joined = "/".join(path)
            raise ReviewWorkspaceError(f"분석 기록에 {joined}이(가) 없다")
        walked = walked[key]
    return walked


def _require_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ReviewWorkspaceError(f"{where}은(는) 배열이어야 한다: {value!r}")
    return value


def _require_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReviewWorkspaceError(f"{where}은(는) 객체여야 한다: {value!r}")
    return value


def _base_plan_slug(revision: ReviewRevision) -> str:
    """리비전의 기본 계획 ID — 파일 이름에 안전한 글자만 남긴다."""
    safe = "".join(
        character
        for character in revision.base_plan_id
        if character.isalnum() or character in "._-"
    )
    if not safe:
        raise ReviewWorkspaceError(
            f"기본 계획 ID가 파일 이름으로 안전하지 않다: {revision.base_plan_id!r}"
        )
    return safe


# ── 파사드 ────────────────────────────────────────────────────────────────────


class ReviewWorkspaceFacade:
    """검토의 지속 작업공간 — 저장소·서비스·순수 라이브러리를 한 자리에서 잇는다."""

    def __init__(self, root: Path, service: RawWorkspaceService | None = None) -> None:
        # 서비스 import는 호출 시점(늦은 결합)로만 — 공개 서비스가 이 파사드를
        # import할 때 모듈 수준 순환이 생기지 않게 한다(W1 연동 계약).
        if service is None:
            from vlogsrt.project.raw.service import RawWorkspaceService

            service = RawWorkspaceService()
        self._workspace = RawWorkspace.open(root)
        self._service = service
        self._store = ReviewWorkspaceStore(self._workspace)
        self._project_id = self._workspace.workspace_id()

    @property
    def root(self) -> Path:
        return self._workspace.root

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def workspace(self) -> RawWorkspace:
        return self._workspace

    # ── 초기화 — 실제 계획·시계·분석 증거에서 ───────────────────

    def initialize_review(
        self,
        plan_revision_id: str | None = None,
        analysis_revision_id: str | None = None,
    ) -> ReviewRevision:
        """확정 분할 계획을 검토 기본 리비전으로 — 원본은 지금 다시 검증·재측정한다.

        계획의 timebase·원점·끝틱·프레임 수가 지금 측정과 어긋나면 초기화가
        거부된다(stale profile). 발화 상태는 분석 증거에서: `analyzed/cached`는
        기록된 상태, `transcribed`는 1단계 기록의 발화 유무, `failed`는 실패,
        행이 없으면 전사 없음을 명시한다.
        """
        snapshot = self._snapshot(plan_revision_id)
        if self._store.head_revision_id(self._project_id, snapshot.revision_id) is not None:
            raise ReviewWorkspaceError(
                f"이 계획의 검토가 이미 있다: {snapshot.revision_id} — load_head으로 여세요"
            )
        statuses = self._speech_statuses(snapshot, analysis_revision_id)
        clocks = tuple(
            self._review_clock(coverage)
            for coverage in sorted(snapshot.coverage, key=lambda item: item.source_order)
        )
        entries = [_PlanReviewEntry(entry, statuses[entry.asset_id]) for entry in snapshot.entries]
        revision = initial_revision(self._project_id, snapshot.revision_id, clocks, entries)
        self._persist_new_base(revision)
        return revision

    def _snapshot(self, plan_revision_id: str | None) -> SegmentPlanSnapshot:
        if plan_revision_id is not None:
            return self._service.segment_plan_revision(self.root, plan_revision_id)
        latest = self._service.latest_segment_plan(self.root)
        if latest is None:
            raise ReviewWorkspaceError("분할 계획이 없다 — 먼저 A3 계획을 세우세요")
        return latest

    def _review_clock(self, coverage: SegmentCoverage) -> SourceClock:
        """원본을 지금 다시 검증·재측정해 시계를 만든다 — 계획 기록과 어긋나면 거부."""
        asset = self._workspace.source_asset(coverage.asset_id)
        self._verify_source_unchanged(asset)
        path = self._workspace.root / asset.relative_path
        probe = preprocess.probe_raw_media(path)
        clock = preprocess.probe_extraction_clock(path, probe=probe)
        if not clock.uniform_grid:
            raise ReviewWorkspaceError(
                f"stale_profile: 원본이 균일 격자가 아니다(VFR): {asset.source_relative_path}"
            )
        self._require_profile_match(coverage, clock, asset)
        assert asset.content_sha256 is not None  # 검증을 통과했으므로
        return make_source_clock(
            source_id=asset.asset_id,
            source_sha256=asset.content_sha256,
            time_base_num=clock.time_base.numerator,
            time_base_den=clock.time_base.denominator,
            origin_pts=clock.first_pts,
            end_pts=coverage.exact_end_pts,
            frame_duration=clock.frame_duration,
        )

    @staticmethod
    def _require_profile_match(
        coverage: SegmentCoverage, clock: preprocess.RawVideoClock, asset: SourceAsset
    ) -> None:
        """계획이 남긴 측정과 지금의 측정 — timebase·원점·끝·프레임 수가 같아야 한다."""
        measured = (
            clock.time_base,
            clock.first_pts,
            math.ceil(clock.exact_end / clock.time_base),
            clock.frame_count,
        )
        recorded = (
            Fraction(coverage.time_base_num, coverage.time_base_den),
            coverage.presentation_origin_pts,
            coverage.exact_end_pts,
            coverage.source_frame_count,
        )
        if measured != recorded:
            raise ReviewWorkspaceError(
                f"stale_profile: 원본 {asset.source_relative_path}의 지금 측정 {measured}가"
                f" 계획 기록 {recorded}와 다르다 — 새 계획을 세우세요"
            )

    def _verify_source_unchanged(self, asset: SourceAsset) -> None:
        """ready 원본의 재검증 — 크기·SHA-256을 지금 다시 읽는다(A3와 같은 계약)."""
        if asset.status != "ready" or asset.content_sha256 is None:
            raise ReviewWorkspaceError(
                f"원본이 검증 준비 상태가 아니다: {asset.source_relative_path} ({asset.status})"
            )
        path = self._workspace.root / asset.relative_path
        probed = path.stat()
        if probed.st_size != asset.size_bytes:
            raise ReviewWorkspaceError(
                f"원본 크기가 가져오기 때와 다르다: {asset.source_relative_path}"
                f" ({asset.size_bytes} → {probed.st_size}B)"
            )
        if _sha256_file(path) != asset.content_sha256:
            raise ReviewWorkspaceError(
                f"원본 내용이 가져오기 때와 다르다: {asset.source_relative_path}"
                " — 다시 가져오세요(새 배치)"
            )

    # ── 분석 증거 → 발화 상태 ────────────────────────────────────

    def _speech_statuses(
        self, snapshot: SegmentPlanSnapshot, analysis_revision_id: str | None
    ) -> dict[str, str]:
        """원본별 발화 상태 — 분석 증거가 말하는 것만, 없으면 전사 없음을 명시한다."""
        store = AnalysisStore(self._workspace)
        revision = self._analysis_revision(store, analysis_revision_id)
        results = (
            {row.asset_id: row for row in store.list_source_results(revision.revision_id)}
            if revision is not None
            else {}
        )
        return {
            coverage.asset_id: self._row_speech_status(results.get(coverage.asset_id))
            for coverage in snapshot.coverage
        }

    def _analysis_revision(
        self, store: AnalysisStore, analysis_revision_id: str | None
    ) -> AnalysisRevision | None:
        """쓸 분석 리비전 — 명시 없으면 가장 최근 완성본(완성만 증거가 된다)."""
        revisions = store.list_revisions()
        if analysis_revision_id is not None:
            found = next(
                (item for item in revisions if item.revision_id == analysis_revision_id), None
            )
            if found is None:
                raise ReviewWorkspaceError(f"없는 분석 리비전이다: {analysis_revision_id}")
            return found
        return next((item for item in reversed(revisions) if item.status == "complete"), None)

    def _row_speech_status(self, row: AnalysisSourceResult | None) -> str:
        """결과 행 하나의 발화 상태 — 상태 집계를 다시 지어내지 않는다."""
        if row is None:
            return SPEECH_UNCONFIRMED_EMPTY
        if row.status == "failed":
            return SPEECH_ANALYSIS_FAILED
        if row.status in ("analyzed", "cached"):
            return (
                row.speech_status
                if row.speech_status in SPEECH_STATUSES
                else (SPEECH_UNCONFIRMED_EMPTY)
            )
        if row.status == "transcribed":
            utterances = self._saved_transcribed_utterances(row)
            return (
                SPEECH_DETECTED
                if any(item.end > item.start for item in utterances)
                else SPEECH_UNCONFIRMED_EMPTY
            )
        return SPEECH_UNCONFIRMED_EMPTY

    # ── 다시 열기·편집 ────────────────────────────────────────────

    def load_head(self, base_plan_id: str | None = None) -> LoadedReview:
        """저장된 헤드를 되돌린다 — 내용 지문을 다시 잰다(깨진 행은 못 지난다)."""
        base = base_plan_id or self._latest_base_plan()
        head_id = self._store.head_revision_id(self._project_id, base)
        if head_id is None:
            raise ReviewWorkspaceError(f"검토 헤드가 없다: {base} — 먼저 초기화하세요")
        document, recorded = self._store.revision_document(
            self._project_id, base, self._sequence_of_revision_id(head_id)
        )
        revision = parse_revision(document)
        if revision.revision_id != head_id:
            raise ReviewWorkspaceError(
                f"검토 행의 sequence가 헤드와 어긋난다: {revision.revision_id} ≠ {head_id}"
            )
        if content_fingerprint(revision) != recorded:
            raise ReviewWorkspaceError(
                f"검토 리비전 본문이 기록된 지문과 다르다: {head_id} — 저장소가 깨졌다"
            )
        return LoadedReview(revision, self._source_staleness(revision))

    def _latest_base_plan(self) -> str:
        bases = self._store.list_base_plan_ids(self._project_id)
        if not bases:
            raise ReviewWorkspaceError("검토 리비전이 하나도 없다 — 먼저 초기화하세요")
        return bases[-1]

    @staticmethod
    def _sequence_of_revision_id(revision_id: str) -> int:
        tail = revision_id.rsplit("/", 1)[-1]
        try:
            return int(tail)
        except ValueError as error:
            raise ReviewWorkspaceError(
                f"검토 리비전 ID에서 sequence를 읽지 못했다: {revision_id}"
            ) from error

    def _source_staleness(self, revision: ReviewRevision) -> tuple[tuple[str, str], ...]:
        """원본의 현재 상태 — 없거나 크기가 달라진 원본을 숨기지 않고 나열한다."""
        stale: list[tuple[str, str]] = []
        for clock in revision.clocks:
            try:
                asset = self._workspace.source_asset(clock.source_id)
            except RawWorkspaceError:
                stale.append((clock.source_id, "자산 기록이 없다"))
                continue
            path = self._workspace.root / asset.relative_path
            if not path.is_file():
                stale.append((clock.source_id, "원본 파일이 없다"))
            elif path.stat().st_size != asset.size_bytes:
                stale.append((clock.source_id, "원본 크기가 기록과 다르다"))
        return tuple(stale)

    def apply_edit(self, base_plan_id: str, edit: ReviewEdit) -> ReviewRevision:
        """편집을 헤드에 적용해 저장한다 — 리비전과 헤드 이동은 한 트랜잭션이다.

        헤드가 편집 대상이 아니면 거부다(stale-head). 출판 도중 무엇이 실패해도
        (크래시·잠금·동시 편집) 리비전과 헤드는 함께 되돌아가므로, 실패한 편집이
        다음 편집의 자리를 막는 일은 없다.
        """
        head = self.load_head(base_plan_id).revision
        if edit.target_revision_id != head.revision_id:
            raise StaleRevisionError(
                f"편집은 {edit.target_revision_id!r}를 겨냥했는데 지금 헤드는 "
                f"{head.revision_id!r}다"
            )
        if edit.kind not in EDIT_KINDS:
            raise ReviewWorkspaceError(f"알 수 없는 편집 종류 {edit.kind!r}")
        edited = apply_edit(head, edit)
        self._store.publish_edit_revision(
            project_id=self._project_id,
            base_plan_id=base_plan_id,
            document_json=serialize_revision(edited),
            content_fingerprint=content_fingerprint(edited),
            expected_head_revision_id=head.revision_id,
            new_head_revision_id=edited.revision_id,
            updated_at=_now(),
        )
        return edited

    def split_segment(self, base_plan_id: str, segment_id: str, split_pts: int) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        return self.apply_edit(base_plan_id, split_edit(head.revision_id, segment_id, split_pts))

    def merge_segments(
        self, base_plan_id: str, first_segment_id: str, second_segment_id: str
    ) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        return self.apply_edit(
            base_plan_id, merge_edit(head.revision_id, first_segment_id, second_segment_id)
        )

    def adjust_boundary(
        self,
        base_plan_id: str,
        left_segment_id: str,
        right_segment_id: str,
        boundary_pts: int,
    ) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        return self.apply_edit(
            base_plan_id,
            boundary_edit(head.revision_id, left_segment_id, right_segment_id, boundary_pts),
        )

    def set_selection(self, base_plan_id: str, segment_id: str, selection: str) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        return self.apply_edit(
            base_plan_id, selection_edit(head.revision_id, segment_id, selection)
        )

    def set_override(
        self, base_plan_id: str, segment_id: str, override: str | None
    ) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        return self.apply_edit(base_plan_id, override_edit(head.revision_id, segment_id, override))

    def set_notes(self, base_plan_id: str, segment_id: str, notes: str) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        return self.apply_edit(base_plan_id, note_edit(head.revision_id, segment_id, notes))

    # ── 순수 조회 — 보기·필터는 상태가 아니다(§10.3) ──────────────

    def chronological(self, revision: ReviewRevision) -> tuple[ReviewSegment, ...]:
        return chronological(revision)

    def filter(
        self,
        revision: ReviewRevision,
        *,
        query: str | None = None,
        tags: Sequence[str] = (),
        selection: str | None = None,
    ) -> tuple[ReviewSegment, ...]:
        return filter_segments(revision, query=query, tags=tags, selection=selection)

    @staticmethod
    def view_state(**fields: object) -> ReviewViewState:
        """보기 상태 — 표시 모드·선택·필터의 순수 기록(리비전 지문에 끼지 않는다)."""
        return ReviewViewState(**fields)  # type: ignore[arg-type]

    # ── 재추출 판정 ───────────────────────────────────────────────

    def reextract_ids(self, previous: ReviewRevision, current: ReviewRevision) -> frozenset[str]:
        """경계 변화로 다시 뽑을 조각 — 메타데이터·선택만 바뀌면 늘 빈 집합이다."""
        return reextract_segment_ids(previous, current)

    def stale_media(
        self, package_dir: str, base_plan_id: str | None = None
    ) -> tuple[StaleSegment, ...]:
        """현재 헤드 대비 낡은 매체 — 게시 artifact가 없거나 core·해시가 어긋난 조각."""
        revision = self.load_head(base_plan_id).revision
        package = self._require_package(package_dir)
        published = {
            clip.segment_id: clip
            for clip in self._workspace.list_clip_artifacts(package.package_dir)
            if clip.status == "published"
        }
        stale: list[StaleSegment] = []
        for segment in chronological(revision):
            reason = self._segment_media_reason(
                revision, segment, published.get(segment.segment_id)
            )
            if reason is not None:
                stale.append(StaleSegment(segment.segment_id, segment.source_id, reason))
        return tuple(stale)

    def _segment_media_reason(
        self, revision: ReviewRevision, segment: ReviewSegment, artifact: ClipArtifact | None
    ) -> str | None:
        """조각 하나의 매체 자격 — 이유가 None이면 지금 매체로 확정할 수 있다."""
        if artifact is None:
            return "게시된 artifact가 없다 — 경계가 움직였으면 새 계획·추출이 필요하다"
        clock = revision.clock_for(segment.source_id)
        if clock is None:
            return "원본 시계가 리비전에 없다"
        if artifact.asset_id != segment.source_id:
            return "artifact가 다른 원본의 것이다"
        if (artifact.core_start_pts, artifact.core_end_pts) != (
            segment.start_pts,
            segment.end_pts,
        ):
            return (
                f"stale_core: artifact core [{artifact.core_start_pts}, {artifact.core_end_pts}]"
                f"가 검토 [{segment.start_pts}, {segment.end_pts}]와 다르다"
            )
        if artifact.media_start_pts > segment.start_pts or artifact.media_end_pts < segment.end_pts:
            return "artifact 매체 범위가 core를 덮지 못한다"
        path = self._workspace.root / artifact.relative_path
        if not path.is_file():
            return "artifact 파일이 없다"
        if _sha256_file(path) != artifact.content_sha256:
            return "artifact 파일 해시가 기록과 다르다"
        return None

    # ── 편집 덮개 → 새 A3 계획 ───────────────────────────────────

    def plan_from_review(
        self, base_plan_id: str | None = None
    ) -> tuple[SegmentPlanSnapshot, ReviewPlanMapping]:
        """편집된 덮개를 새 분할 계획으로 — 경계 왕복이 항등임을 검증하고 매핑을 남긴다."""
        revision = self.load_head(base_plan_id).revision
        snapshot = self._snapshot(revision.base_plan_id)
        intervals: dict[str, list[PlannedInterval]] = {}
        for source_id, spans in self._coverage_by_source(revision).items():
            clock = revision.clock_for(source_id)
            if clock is None:
                raise ReviewWorkspaceError(f"원본 {source_id!r}의 시계가 리비전에 없다")
            intervals[source_id] = [
                PlannedInterval(
                    start=(span.start_pts - clock.origin_pts) * clock.time_base,
                    end=(span.end_pts - clock.origin_pts) * clock.time_base,
                )
                for span in spans
            ]
        new_snapshot = self._service.plan_segmentation(
            self.root,
            order_revision_id=snapshot.order_revision_id,
            intervals=intervals,
            basis=SEGMENTATION_BASIS_PLANNED,
        )
        return new_snapshot, self._verified_mapping(revision, new_snapshot)

    @staticmethod
    def _coverage_by_source(revision: ReviewRevision) -> dict[str, list[ReviewSegment]]:
        """원본별 시간순 타일 — 전체 덮개·연접은 모델이 이미 보증한다(§6.1)."""
        grouped: dict[str, list[ReviewSegment]] = {}
        for segment in chronological(revision):
            grouped.setdefault(segment.source_id, []).append(segment)
        return grouped

    def _verified_mapping(
        self, revision: ReviewRevision, new_snapshot: SegmentPlanSnapshot
    ) -> ReviewPlanMapping:
        """옛 조각 ↔ 새 계획 조각 — core 범위 집합이 정확히 같을 때만 매핑이다."""
        by_range = {
            (segment.source_id, segment.start_pts, segment.end_pts): segment.segment_id
            for segment in revision.segments
        }
        pairs: list[MappingPair] = []
        for entry in new_snapshot.entries:
            old_id = by_range.get((entry.asset_id, entry.core_start_pts, entry.core_end_pts))
            if old_id is None:
                raise ReviewWorkspaceError(
                    f"환원 검증 실패: 새 계획 조각 {entry.segment_id!r}의 core가 검토"
                    " 덮개에 없다 — 경계 왕복이 항등이 아니다"
                )
            pairs.append(
                MappingPair(
                    old_segment_id=old_id,
                    new_segment_id=entry.segment_id,
                    source_id=entry.asset_id,
                    core_start_pts=entry.core_start_pts,
                    core_end_pts=entry.core_end_pts,
                )
            )
        if len(pairs) != len(revision.segments):
            raise ReviewWorkspaceError(
                f"환원 검증 실패: 새 계획의 조각 수가 검토 덮개와 다르다"
                f" ({len(pairs)} ≠ {len(revision.segments)}) — 전체 덮음이 깨졌다"
            )
        return ReviewPlanMapping(
            old_revision_id=revision.revision_id,
            new_plan_revision_id=new_snapshot.revision_id,
            pairs=tuple(pairs),
        )

    def adopt_plan_review(
        self,
        new_snapshot: SegmentPlanSnapshot,
        mapping: ReviewPlanMapping,
        old_revision: ReviewRevision | None = None,
    ) -> ReviewRevision:
        """새 계획 위의 검토 리비전 — 신원은 계획을 따르고 메타데이터는 검토를 따른다.

        새 계획의 조각 ID가 곧 리비전의 조각 ID다(확정 내보내기의 `stale_core`
        가드와 artifact 조회가 이 신원으로 맞는다). 사용자의 선택·등급 덮기·메모는
        옛 리비전에서 **공식적인 편집 기록으로** 다시 적용된다 — 몰래 옮기지 않는다.
        """
        old = old_revision if old_revision is not None else self.load_head().revision
        if mapping.old_revision_id != old.revision_id:
            raise ReviewWorkspaceError(
                f"매핑이 {mapping.old_revision_id!r}용인데 검토 리비전은 {old.revision_id!r}다"
            )
        if self._store.head_revision_id(self._project_id, new_snapshot.revision_id) is not None:
            raise ReviewWorkspaceError(
                f"이 계획의 검토가 이미 있다: {new_snapshot.revision_id} — load_head으로 여세요"
            )
        statuses = {segment.source_id: segment.speech_status for segment in old.segments}
        entries = [
            _PlanReviewEntry(entry, statuses[entry.asset_id]) for entry in new_snapshot.entries
        ]
        adopted = initial_revision(
            self._project_id,
            new_snapshot.revision_id,
            self._adopted_clocks(old, new_snapshot),
            entries,
        )
        self._persist_new_base(adopted, mapping)
        return self._replay_metadata(new_snapshot.revision_id, old, mapping)

    def _adopted_clocks(
        self, old: ReviewRevision, new_snapshot: SegmentPlanSnapshot
    ) -> tuple[SourceClock, ...]:
        """새 계획의 덮개가 옛 시계와 같은 시계 위에 있는가 — 다르면 입양이 거짓이다."""
        new_coverage = {coverage.asset_id: coverage for coverage in new_snapshot.coverage}
        clocks: list[SourceClock] = []
        for clock in old.clocks:
            coverage = new_coverage.get(clock.source_id)
            if coverage is None:
                raise ReviewWorkspaceError(
                    f"새 계획에 원본 {clock.source_id!r}의 덮개가 없다 — 전체 덮음 위반"
                )
            if (
                Fraction(coverage.time_base_num, coverage.time_base_den) != clock.time_base
                or coverage.presentation_origin_pts != clock.origin_pts
                or coverage.exact_end_pts != clock.end_pts
            ):
                raise ReviewWorkspaceError(
                    f"새 계획의 시계가 검토 시계와 다르다: {clock.source_id!r}"
                )
            clocks.append(clock)
        return tuple(clocks)

    def _replay_metadata(
        self, new_base_plan_id: str, old: ReviewRevision, mapping: ReviewPlanMapping
    ) -> ReviewRevision:
        """옛 검토의 메타데이터를 새 리비전에 공식 편집으로 다시 적용한다."""
        new_ids = {pair.old_segment_id: pair.new_segment_id for pair in mapping.pairs}
        revision = self.load_head(new_base_plan_id).revision
        for old_segment in chronological(old):
            segment_id = new_ids[old_segment.segment_id]
            if old_segment.selection == SELECTION_EXCLUDED:
                revision = self.set_selection(new_base_plan_id, segment_id, old_segment.selection)
            if old_segment.override_recommendation is not None:
                revision = self.set_override(
                    new_base_plan_id, segment_id, old_segment.override_recommendation
                )
            if old_segment.notes:
                revision = self.set_notes(new_base_plan_id, segment_id, old_segment.notes)
        return revision

    # ── 측정 VTT — 저장된 발화 + 잰 매체 길이 ────────────────────

    def review_vtt(
        self,
        segment_id: str,
        package_dir: str,
        base_plan_id: str | None = None,
    ) -> ReviewVttOutput:
        """조각 하나의 검토용 VTT — 시각은 잰 발화·잰 매체 길이만으로 만든다."""
        revision = self.load_head(base_plan_id).revision
        segment = self._require_segment(revision, segment_id)
        artifact = self._require_fresh_artifact(revision, segment, package_dir)
        clock = revision.clock_for(segment.source_id)
        if clock is None:
            raise ReviewWorkspaceError(f"원본 {segment.source_id!r}의 시계가 리비전에 없다")
        window = ClipWindow(
            clip_id=artifact.clip_id,
            source_id=artifact.asset_id,
            media_start_pts=artifact.media_start_pts,
            media_end_pts=artifact.media_end_pts,
            measured_media_duration=self._window_duration(artifact, clock),
        )
        result = project_review_vtt(
            revision,
            segment_id,
            window,
            self._timed_evidence(segment.source_id),
            ProjectionClock(
                time_base_num=clock.time_base_num,
                time_base_den=clock.time_base_den,
                file_zero_pts=Fraction(0),
            ),
        )
        return ReviewVttOutput(
            segment_id=segment.segment_id,
            clip_id=artifact.clip_id,
            vtt=render_review_vtt(result),
            provenance=self._provenance(segment, artifact),
        )

    @staticmethod
    def _window_duration(artifact: ClipArtifact, clock: SourceClock) -> Fraction | None:
        """검토 창의 잰 길이 — 소리꼬리는 원본 끝에 닿은 클립에만 창이 된다(§10.2).

        뽑힌 매체의 잰 길이에는 중간 클립에도 AAC 프레임 여백이 몇십 밀리초 붙는다
        (인코더 양자화 잔여 — 원본의 소리가 아니다). 투영 계약은 끝 클립의
        소리꼬리만 창으로 인정하므로, 그 외의 클립은 잰 값이 영상 범위와 같을 때만
        실고 나머지는 영상 범위가 곧 창인 기본값(None)을 쓴다 — 시각을 지어내는
        길은 없다.
        """
        measured = Fraction(artifact.media_local_duration)
        video_range = Fraction(artifact.media_end_pts - artifact.media_start_pts) * clock.time_base
        if artifact.media_end_pts == clock.end_pts or measured == video_range:
            return measured
        return None

    def _require_segment(self, revision: ReviewRevision, segment_id: str) -> ReviewSegment:
        for segment in revision.segments:
            if segment.segment_id == segment_id:
                return segment
        raise ReviewWorkspaceError(f"리비전 {revision.revision_id!r}에 조각 {segment_id!r}가 없다")

    def _require_fresh_artifact(
        self, revision: ReviewRevision, segment: ReviewSegment, package_dir: str
    ) -> ClipArtifact:
        """이 조각을 위해 뽑·검증된 artifact — 낡은 core는 VTT의 재료가 아니다."""
        package = self._require_package(package_dir)
        artifact = self._published_artifact(package.package_dir, segment.segment_id)
        reason = self._segment_media_reason(revision, segment, artifact)
        if reason is not None:
            raise ReviewWorkspaceError(f"stale_media: 조각 {segment.segment_id!r} — {reason}")
        assert artifact is not None and artifact.content_sha256 is not None
        return artifact

    def _provenance(self, segment: ReviewSegment, artifact: ClipArtifact) -> ReviewProvenance:
        """원본 original 대 추출 preview — 같은 조각의 두 출처를 나란히 남긴다."""
        asset = self._workspace.source_asset(segment.source_id)
        assert artifact.content_sha256 is not None
        return ReviewProvenance(
            segment_id=segment.segment_id,
            clip_id=artifact.clip_id,
            source_id=segment.source_id,
            source_original_relative_path=asset.relative_path,
            source_sha256=asset.content_sha256 or "",
            extracted_preview_relative_path=artifact.relative_path,
            extracted_sha256=artifact.content_sha256,
            measured_media_duration=artifact.media_local_duration,
        )

    def _timed_evidence(self, source_id: str) -> tuple[TimedUtterance, ...]:
        """저장된 실측 발화를 파일 축으로 — 좌표 덧셈은 축마다 딱 한 번(§13.3).

        `analyzed/cached` 문서의 발화는 공통 시계(비디오 원점 0)이므로 제시 원점을
        더하고, `transcribed` 1단계 기록의 발화는 정규화 오디오 축이므로 PCM 원점을
        더한다(`review_projection.timed_utterances`). 그 밖의 변환은 없다.
        """
        row = self._latest_evidence_row(source_id)
        if row is None:
            return ()
        if row.status in ("analyzed", "cached"):
            return self._analyzed_evidence(row)
        if row.status == "transcribed":
            return self._transcribed_evidence(row)
        return ()

    def _analyzed_evidence(self, row: AnalysisSourceResult) -> tuple[TimedUtterance, ...]:
        """완성 분석의 발화 — 공통 시계 값에 제시 원점을 딱 한 번 더한다."""
        if not row.document_path or not row.record_path:
            return ()
        document = self._read_json(row.document_path, "분석 문서")
        record = self._read_json(row.record_path, "분석 기록")
        origin = _fraction_from_json(
            _document_value(record, ("timeline", "video", "presentation_origin")),
            "timeline.video.presentation_origin",
        )
        return tuple(
            TimedUtterance(
                utterance_id=str(item["utterance_id"]),
                text=str(item["text"]),
                start_file=origin + _fraction_from_json(item["start"], "utterance.start"),
                end_file=origin + _fraction_from_json(item["end"], "utterance.end"),
                evidence_id=str(item["evidence_id"]),
            )
            for item in (
                _require_mapping(raw, "utterances[]")
                for raw in _require_list(_document_value(document, ("utterances",)), "utterances")
            )
        )

    def _transcribed_evidence(self, row: AnalysisSourceResult) -> tuple[TimedUtterance, ...]:
        """1단계 전사 기록의 발화 — 정규화 축에 PCM 원점을 딱 한 번 더한다."""
        if not row.document_path:
            return ()
        artifact = self._read_json(row.document_path, "1단계 전사 기록")
        pcm_origin = _fraction_from_json(
            _document_value(artifact, ("audio", "pcm_sample0_file_time")),
            "audio.pcm_sample0_file_time",
        )
        return timed_utterances(self._saved_transcribed_utterances(row), pcm_origin)

    def _saved_transcribed_utterances(
        self, row: AnalysisSourceResult
    ) -> tuple[_UtteranceItem, ...]:
        """1단계 기록의 발화를 값으로 — 상태 판정과 VTT가 같은 기록을 읽는다."""
        if not row.document_path:
            return ()
        artifact = self._read_json(row.document_path, "1단계 전사 기록")
        return tuple(
            _UtteranceItem(
                utterance_id=str(item["utterance_id"]),
                text=str(item["text"]),
                start=_fraction_from_json(item["start"], "utterance.start"),
                end=_fraction_from_json(item["end"], "utterance.end"),
                evidence_id=str(item["evidence_id"]),
            )
            for item in (
                _require_mapping(raw, "utterances[]")
                for raw in _require_list(_document_value(artifact, ("utterances",)), "utterances")
            )
        )

    def _latest_evidence_row(self, source_id: str) -> AnalysisSourceResult | None:
        """가장 최근 완성 분석의 이 원본 결과 — 증거는 완성된 기록에서만 온다."""
        store = AnalysisStore(self._workspace)
        for revision in reversed(store.list_revisions()):
            if revision.status != "complete":
                continue
            row = store.source_result(revision.revision_id, source_id)
            if row is not None:
                return row
        return None

    def _read_json(self, relative_path: str, label: str) -> dict[str, object]:
        path = self._workspace.root / relative_path
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ReviewWorkspaceError(
                f"{label}을 읽지 못했다: {relative_path} — {error}"
            ) from error
        if not isinstance(body, dict):
            raise ReviewWorkspaceError(f"{label}이 JSON 객체가 아니다: {relative_path}")
        return body

    def write_review_vtts(
        self,
        package_dir: str,
        base_plan_id: str | None = None,
        context: JobContext | None = None,
    ) -> tuple[Path, ...]:
        """현재 헤드의 조각별 VTT를 `review/vtt/`에 쓴다 — 재료가 없는 조각은 건너뛴다."""
        revision = self.load_head(base_plan_id).revision
        segments = self._media_fresh_segments(revision, package_dir)
        if context is not None:
            context.set_total(len(segments))
        directory = self.root / REVIEW_VTT_ROOT / _base_plan_slug(revision)
        written: list[Path] = []
        for position, segment in enumerate(segments, start=1):
            if context is not None:
                context.check_cancel()
            output = self.review_vtt(segment.segment_id, package_dir, base_plan_id)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{segment.segment_id}.review.vtt"
            temp = target.with_name(target.name + ".partial")
            temp.write_text(output.vtt.text, encoding="utf-8")
            temp.replace(target)
            written.append(target)
            if context is not None:
                context.set_progress(position)
        return tuple(written)

    def _media_fresh_segments(
        self, revision: ReviewRevision, package_dir: str
    ) -> list[ReviewSegment]:
        """지금 매체로 VTT를 만들 수 있는 조각 — 낡은 매체는 건너뛰고 목록에 남긴다."""
        return [
            segment
            for segment in chronological(revision)
            if self._segment_media_reason(
                revision,
                segment,
                self._published_artifact(package_dir, segment.segment_id),
            )
            is None
        ]

    def _published_artifact(self, package_dir: str, segment_id: str) -> ClipArtifact | None:
        return next(
            (
                clip
                for clip in self._workspace.list_clip_artifacts(package_dir)
                if clip.segment_id == segment_id and clip.status == "published"
            ),
            None,
        )

    # ── 확정 내보내기 — 서버 소유 이름공간 ────────────────────────

    def export_confirmed(
        self,
        package_dir: str,
        base_plan_id: str | None = None,
        selected_segment_ids: frozenset[str] | None = None,
        context: JobContext | None = None,
    ) -> ConfirmedExportOutcome:
        """검토 리비전을 확정 폴더로 — 실제 바이트 복사·no-replace(전체 또는 선택)."""
        revision = self.load_head(base_plan_id).revision
        package = self._require_package(package_dir)
        artifacts = self._workspace.list_clip_artifacts(package.package_dir)
        assets = {
            asset.asset_id: asset
            for asset in (
                self._workspace.source_asset(asset_id)
                for asset_id in {clip.asset_id for clip in artifacts}
            )
        }
        export_id = new_review_export_id()
        request = confirmed.build_confirmed_request(
            export_id=export_id,
            destination_dir=confirmed_destination_dir(export_id),
            project_id=self._project_id,
            package=package,
            review=revision,
            artifacts=artifacts,
            assets=assets,
            selected_segment_ids=selected_segment_ids,
            profile_name=package.profile_name,
            handles_before=package.handles_before,
            handles_after=package.handles_after,
            tool_provenance=str(preprocess.ffmpeg_provenance()),
            created_at=_now(),
        )
        if context is not None:
            context.set_total(len(request.clips))
            context.check_cancel()
        report = confirmed.export_confirmed_package(self.root, request)
        if context is not None:
            context.set_progress(len(request.clips))
        fingerprint = confirmed.export_fingerprint(request)
        self._store.insert_export_revision(
            export_id=export_id,
            destination_dir=request.destination_dir,
            project_id=self._project_id,
            base_plan_id=revision.base_plan_id,
            review_revision_id=revision.revision_id,
            review_fingerprint=request.review_fingerprint,
            export_fingerprint=fingerprint,
            mode=report.mode,
            clip_count=report.clip_count,
            total_bytes=report.total_bytes,
            created_at=_now(),
        )
        return ConfirmedExportOutcome(
            export_id=export_id,
            destination_dir=request.destination_dir,
            report=report,
            export_fingerprint=fingerprint,
        )

    def verify_export(self, destination_dir: str) -> confirmed.ConfirmedExportReport:
        """이미 게시된 확정 폴더를 다시 검증한다 — 캐시 정리·재시작 뒤의 증명."""
        row = next(
            (
                item
                for item in self._store.list_export_revisions(self._project_id)
                if str(item["destination_dir"]) == destination_dir
            ),
            None,
        )
        if row is None:
            raise ReviewWorkspaceError(f"기록된 내보내기가 아니다: {destination_dir}")
        return confirmed.verify_confirmed_export(self.root, destination_dir)

    def _require_package(self, package_dir: str) -> PackageRevision:
        package = self._workspace.package_revision(package_dir)
        if package is None:
            raise ReviewWorkspaceError(f"없는 package다: {package_dir}")
        return package

    # ── 관리 정리·미리보기 복원 ───────────────────────────────────

    def managed_cleanup(self) -> cache_policy.CachePurgeResult:
        """캐시 정리 — `cache/`·`previews/`만. 보호 이름은 정책이 물리적으로 거부한다."""
        plan = cache_policy.plan_cache_purge(self.root)
        return cache_policy.execute_cache_purge(self.root, plan)

    def restore_previews(
        self,
        package_dir: str,
        base_plan_id: str | None = None,
        context: JobContext | None = None,
    ) -> tuple[RestoredPreview, ...]:
        """게시된 조각을 `previews/review/`로 검증 복사해 실제 파일을 돌려준다.

        확정 폴더(`clips/confirmed_*`)는 읽지도 쓰지도 않는다 — 복사의 원천은
        언제나 뽑힌 package의 검증된 매체다. 이미 같은 내용이 있으면 다시 쓰지
        않고 그 파일을 그대로 돌려준다(멱등).
        """
        revision = self.load_head(base_plan_id).revision
        package = self._require_package(package_dir)
        segments = self._media_fresh_segments(revision, package.package_dir)
        if context is not None:
            context.set_total(len(segments))
        restored: list[RestoredPreview] = []
        for position, segment in enumerate(segments, start=1):
            if context is not None:
                context.check_cancel()
            restored.append(self._restore_one_preview(segment, package))
            if context is not None:
                context.set_progress(position)
        return tuple(restored)

    def _restore_one_preview(
        self, segment: ReviewSegment, package: PackageRevision
    ) -> RestoredPreview:
        artifact = self._published_artifact(package.package_dir, segment.segment_id)
        if artifact is None or artifact.content_sha256 is None:
            raise ReviewWorkspaceError(
                f"조각 {segment.segment_id!r}의 게시된 artifact가 없다 — 미리보기를 만들 수 없다"
            )
        source = self.root.joinpath(*artifact.relative_path.split("/"))
        if artifact.relative_path.startswith("clips/confirmed_"):
            raise ReviewWorkspaceError(
                f"확정 폴더는 미리보기의 원천이 아니다: {artifact.relative_path}"
            )
        sha = _sha256_file(source)
        if sha != artifact.content_sha256:
            raise ReviewWorkspaceError(f"원천 매체 해시가 기록과 다르다: {artifact.relative_path}")
        relative = f"{PREVIEW_ROOT}/{package.package_dir.split('/')[-1]}/{source.name}"
        target = self.root.joinpath(*relative.split("/"))
        if not (target.is_file() and _sha256_file(target) == sha):
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".partial")
            with open(source, "rb") as reader, open(temp, "xb") as writer:
                for block in iter(lambda: reader.read(1 << 20), b""):
                    writer.write(block)
            temp.replace(target)
        return RestoredPreview(
            segment_id=segment.segment_id,
            clip_id=artifact.clip_id,
            relative_path=relative,
            content_sha256=sha,
            size_bytes=target.stat().st_size,
        )

    # ── 저장 — 불변 리비전 + 첫 헤드는 한 트랜잭션 ────────────────

    def _persist_new_base(
        self, revision: ReviewRevision, mapping: ReviewPlanMapping | None = None
    ) -> None:
        """새 기본 계획의 첫 출판 — 리비전·첫 헤드(·입양 매핑)가 한 몸으로 들어간다."""
        self._store.publish_initial_revision(
            project_id=self._project_id,
            base_plan_id=revision.base_plan_id,
            document_json=serialize_revision(revision),
            content_fingerprint=content_fingerprint(revision),
            head_revision_id=revision.revision_id,
            created_at=_now(),
            remapping=(
                RemappingInsert(
                    old_revision_id=mapping.old_revision_id,
                    new_base_plan_id=mapping.new_plan_revision_id,
                    new_revision_id=revision.revision_id,
                    mapping_json=mapping.as_document(),
                )
                if mapping is not None
                else None
            ),
        )
