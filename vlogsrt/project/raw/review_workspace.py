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
import threading
from collections import Counter
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from vlogsrt import log, preprocess
from vlogsrt.project.raw import cache_policy, tiering
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
    AUTO_EXCLUDED_UNREVIEWED_TAG,
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
    confirm_tags,
    content_fingerprint,
    filter_segments,
    initial_revision,
    is_unreviewed_auto_exclusion,
    make_source_clock,
    merge_edit,
    note_edit,
    override_edit,
    parse_revision,
    reextract_segment_ids,
    selection_edit,
    serialize_revision,
    split_edit,
    unreviewed_auto_exclusions,
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


def require_automatic_exclusions_reviewed(revision: ReviewRevision) -> None:
    """확정 내보내기 관문 — 사람이 보지 않은 자동 제외가 있으면 잘라내지 않고 멈춘다(D35).

    실제로 잘라 내는 `export_confirmed`만 이 관문을 지난다. Final Cut 전체 타임라인 보내기는
    아무것도 지우지 않으므로(미검토 제외도 켠 채 키워드로만 간다) 관문이 없다(D56).
    """
    pending = unreviewed_auto_exclusions(revision)
    if pending:
        raise ReviewWorkspaceError(
            f"자동 제외 {len(pending)}개를 먼저 확인하세요 — 제외를 유지하려면 "
            "'자동 제외 확인'을, 되살리려면 해당 컷을 사용으로 바꾸세요"
        )


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


MANUAL_SELECTION_PREFIX = "수동 선택 · "
"""자동 제외 근거가 남은 조각을 사람이 사용/제외로 고른 표지 — `apply_edit`이 단다."""

_AUTO_NOTE_PREFIX = "자동 제외: "
"""자동 제외 근거 메모의 머리 — 뒤에 근거 JSON이 붙는다(작업대도 이 머리로 찾는다)."""

_MACHINE_NOTE_PREFIXES = (_AUTO_NOTE_PREFIX, "확인 필요: ")
"""조립이 쓰는 메모 머리 — 그 밖의 비지 않은 메모는 사람이 쓴 것이다."""

ADOPTION_REPLAY_LABEL = "입양 결정 다시 적용"
"""입양이 옛 머리의 결정을 다 다시 적은 자리 — 그 뒤의 편집만 이 기본에서 새로 생긴 것이다(D53)."""

CARRIED_DECISIONS_LABEL = "사람 결정 이어받음"
"""새 기본이 옛 머리에서 사람이 손댄 원본을 이어받은 자리 — `← 옛 머리 · 원본,…`로 적는다(D53).

게시는 새 기본(sequence 0)을 세우며 그 원본들의 조각을 옮겨 온다. 기록이 없으면 내용만으로는
사람 손인지 모르는 결정(통째 합친 경계·기계 메모 머리를 단 사람 메모)이 '손대지 않음'으로 읽혀
다음 게시·순서 맞춤이 다시 세우며 지운다.
"""

_UNREVIEWED_KEPT_LABEL = "자동 제외 미검토 유지"
"""입양이 되풀이 끝에 미검토 표지를 되돌려 놓는 스냅숏 — 옛 입양의 되풀이 창 마지막 편집이다."""

_MACHINE_EDIT_LABELS = ("등급 재계산 ", _UNREVIEWED_KEPT_LABEL)
"""기계가 단 스냅숏 이름 — 등급 다시 달기·미검토 표지 되돌리기. 그 밖의 편집은 사람 손이다."""

_HUMAN_SNAPSHOT_LABELS = ("자동 제외 확인", "자동 제외 전체 복원")
"""머리 전체에 걸치는 사람 스냅숏 — 앞 리비전과 견주어 실제로 바뀐 영상만 사람 손이다(D53)."""

_UNDO_PREFIX = "undo:"
"""되돌리기 스냅숏 `undo:N` — 그 리비전의 내용은 리비전 N과 같다(사이의 편집은 남은 것이 없다)."""


_MARKER_LABELS = (ADOPTION_REPLAY_LABEL, CARRIED_DECISIONS_LABEL)


def _marker_label(kind: str, prior: ReviewRevision, sources: Set[str]) -> str:
    """요약 표지 — `종류 ← 옛 머리 · 사람 손이 닿은 원본,…`. 그 앞의 기록을 이 한 줄이 대신한다."""
    return f"{kind} ← {prior.revision_id} · {','.join(sorted(sources))}"


def _marker_sources(label: str) -> tuple[bool, frozenset[str] | None]:
    """요약 표지인가, 그렇다면 적힌 원본 — 원본 목록이 없는 표지는 None(모든 원본으로 본다)."""
    if not label.startswith(tuple(kind + " ← " for kind in _MARKER_LABELS)):
        return False, None
    _prior, sep, sources = label.split(" ← ", 1)[1].partition(" · ")
    if not sep:
        return True, None
    return True, frozenset(source for source in sources.split(",") if source)


_EVIDENCE_STATUSES = ("analyzed", "cached", "partial")
"""판정 증거가 있는 분석 결과 상태 — 부분 완료도 창마다의 판정은 남아 있다."""


def _segment_seconds(revision: ReviewRevision, segment: ReviewSegment) -> tuple[Fraction, Fraction]:
    """조각의 공통 시계 초 — 분석 문서의 축(비디오 원점 0)과 같다."""
    clock = revision.clock_for(segment.source_id)
    assert clock is not None
    return (
        (segment.start_pts - clock.origin_pts) * clock.time_base,
        (segment.end_pts - clock.origin_pts) * clock.time_base,
    )


_RED_GRADE_TAG = tiering.TIER_TAG_PREFIX + tiering.TIER_LABELS[tiering.TIER_RED]


def _auto_provenance(notes: str) -> Mapping[str, object] | None:
    """근거 메모의 JSON 객체 — 사람이 메모 칸에서 말을 덧붙였거나 바꿨으면 None이다.

    작업대 메모 칸은 이 근거로 미리 채워져 있어 덧붙인 말이 JSON을 깬다. 깨진 메모는 계약
    위반이 아니라 사람의 흔적이므로 예외로 옮기기·정리를 멈추지 않는다.
    """
    try:
        provenance = json.loads(notes.removeprefix(_AUTO_NOTE_PREFIX))
    except ValueError:
        return None
    return provenance if isinstance(provenance, Mapping) else None


def _stale_tier_proposal(segment: ReviewSegment) -> bool | None:
    """옛 등급판이 낸, 지금 등급으로는 빨강이 아닌 미검토 제외 제안인가 — 근거를 못 읽으면 None.

    판단만 한다(경고 없음) — 화면 갱신마다 불리는 정리 고리의 '할 일' 계산도 이것을 쓴다.
    """
    if not is_unreviewed_auto_exclusion(segment) or _RED_GRADE_TAG in segment.visual_tags:
        return False
    if not segment.notes.startswith(_AUTO_NOTE_PREFIX):
        return False
    provenance = _auto_provenance(segment.notes)
    if provenance is None:
        return None
    stale = provenance.get("version") != tiering.TIER_SETTINGS.tier_version
    return provenance.get("reason") in tiering.TIER_RED_REASONS and stale


def _release_stale_tier_proposal(segment: ReviewSegment) -> ReviewSegment:
    """옛 등급판이 낸 미검토 제외 제안을 푼다 — 지금 등급이 빨강이 아닐 때만(D52).

    기준이 바뀌어 근거가 사라진 제안이 사람의 확인을 기다리며 출력을 막으면 안 된다.
    사람이 확인한 제외·자동 정리의 제안·지금 판의 제안은 건드리지 않는다. 근거 메모도
    지운다 — 남기면 「자동 제외 전체 복원」과 같은 사람 결정으로 읽힌다. 근거 메모를 사람이
    고쳤으면 풀지 않는다 — 지우면 사람이 쓴 말이 사라지므로 미검토 제외로 남겨 경고한다.
    """
    stale = _stale_tier_proposal(segment)
    if stale is None:
        log.warn(
            f"{segment.segment_id}: 자동 제외 근거 메모를 읽을 수 없다(사람이 고친 메모) — "
            "옛 등급판 제안을 풀지 않고 미검토 제외로 둔다"
        )
        return segment
    if not stale:
        return segment
    return replace(
        segment, selection="included", visual_tags=confirm_tags(segment.visual_tags), notes=""
    )


_WRITE_LOCKS: dict[Path, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def review_write_lock(root: Path) -> threading.RLock:
    """검토 머리를 쓰는 모든 길(편집·새 기본 출판·입양)의 작업공간별 자물쇠(D53).

    순서 맞춤·등급판 옮기기는 원본을 다시 해시하느라 길고, 입양은 결정을 편집으로 다시 적는다
    (실데이터 137번·약 14초). 그 사이 끼어든 편집·출판이 옛 기본에 내려앉아 새 머리에서 조용히
    사라지지 않게, 쓰기는 이 자물쇠 안에서 '지금 최신인가'를 확인한 뒤에만 한다. 한 프로세스
    안의 자물쇠다 — 같은 스레드의 되들어옴(입양 → 되풀이 편집)은 허용한다.
    """
    key = root.expanduser().resolve()
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(key, threading.RLock())


def _human_segment(segment: ReviewSegment) -> bool:
    """조각 내용에 사람 결정이 실렸는가 — 덮기·사람 메모·확인한 제외·복원·깨진 근거 메모."""
    notes = segment.notes
    return (
        segment.override_recommendation is not None
        or notes.startswith(MANUAL_SELECTION_PREFIX)
        or (bool(notes) and not notes.startswith(_MACHINE_NOTE_PREFIXES))
        # 미리 채워진 근거 메모에 사람이 덧붙이면 JSON이 깨진다 — 사람이 쓴 메모다.
        or (notes.startswith(_AUTO_NOTE_PREFIX) and _auto_provenance(notes) is None)
        or (
            segment.selection == SELECTION_EXCLUDED
            and AUTO_EXCLUDED_UNREVIEWED_TAG not in segment.visual_tags
        )
        # 「자동 제외 전체 복원」은 근거 메모를 남긴 채 사용으로 되돌린다.
        or (segment.selection != SELECTION_EXCLUDED and notes.startswith(_AUTO_NOTE_PREFIX))
    )


def _segment_states(revision: ReviewRevision) -> dict[str, tuple[tuple[object, ...], ...]]:
    """영상마다 조각의 (경계, 선택, 메모, 덮기, 태그) — 두 리비전의 영상별 차이를 견준다."""
    states: dict[str, list[tuple[object, ...]]] = {}
    for s in sorted(revision.segments, key=lambda s: (s.source_id, s.start_pts)):
        states.setdefault(s.source_id, []).append(
            (s.start_pts, s.end_pts, s.selection, s.notes, s.override_recommendation, s.visual_tags)
        )
    return {source: tuple(rows) for source, rows in states.items()}


def _edit_sources(
    edit: ReviewEdit, label: str, parent: ReviewRevision, revision: ReviewRevision
) -> set[str]:
    """사람 편집 하나가 닿은 원본 — 머리 전체 스냅숏은 앞 리비전과 실제로 다른 영상만이다.

    「자동 제외 확인」 한 번이 모든 영상을 영원히 '손댄 영상'으로 만들면, 다음 분석 게시가 손대지
    않은 영상의 새 조각·제안을 내보이지 못한다. 조각을 못 찾는 편집은 모든 원본이다.
    """
    if label.startswith(_HUMAN_SNAPSHOT_LABELS):
        before, after = _segment_states(parent), _segment_states(revision)
        return {
            source
            for source in before.keys() | after.keys()
            if before.get(source) != after.get(source)
        }
    everything = {clock.source_id for clock in parent.clocks}
    sources = {s.source_id for s in parent.segments if s.segment_id in edit.segment_ids}
    found_ids = {s.segment_id for s in parent.segments if s.segment_id in edit.segment_ids}
    return sources if found_ids == set(edit.segment_ids) else everything


@dataclass(frozen=True, slots=True)
class _LegacyReplay:
    """표지 없는 옛 입양(D53 이전 앱)의 되풀이 창 — 끝 순번·옛 머리·창에 있어야 할 편집.

    끝 순번 0은 확인할 창이 없다는 뜻이다 — 되풀이가 비었거나, 다시 셀 수 없는 입양(매핑 밖 조각·
    리비전보다 긴 창)이거나, 창 확인이 어긋났다. 어느 쪽이든 편집을 모두 사람 편집으로 걷고 맨 앞
    (순번 0)에서 옛 머리로 넘어간다.
    """

    end: int
    old_revision_id: str
    expected: Counter[tuple[str, str, str]]
    snapshot: bool
    new_ids: frozenset[str]


_REPLAY_FIELDS = {"selection": "selection", "override": "override", "note": "notes"}
"""되풀이가 쓰는 편집 종류와 그 값이 실린 칸 — 제외 선택·등급 덮기·메모."""


def _expected_replay(
    old: ReviewRevision, new_ids: Mapping[str, str]
) -> Counter[tuple[str, str, str]] | None:
    """옛 머리를 되풀이한 입양이 남겼을 편집 — (종류, 새 조각, 값)마다 몇 번. 매핑 밖이면 None."""
    expected: Counter[tuple[str, str, str]] = Counter()
    for s in old.segments:
        new_id = new_ids.get(s.segment_id)
        if new_id is None:
            return None
        if s.selection == SELECTION_EXCLUDED:
            expected["selection", new_id, s.selection] += 1
        if s.override_recommendation is not None:
            expected["override", new_id, s.override_recommendation] += 1
        if s.notes:
            expected["note", new_id, s.notes] += 1
    return expected


def _replay_key(edit_json: str | None) -> tuple[str, str, str] | None:
    """편집 하나를 (종류, 조각, 값)으로 — 조각 하나짜리 선택·덮기·메모가 아니면 None."""
    if edit_json is None:
        return None
    edit = json.loads(edit_json)
    kind = edit.get("kind")
    field = _REPLAY_FIELDS.get(kind) if isinstance(kind, str) else None
    ids = edit.get("segment_ids")
    value = None if field is None else edit.get(field)
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(value, str):
        return None
    return str(kind), str(ids[0]), value


def _replay_window_matches(edits: Sequence[str | None], legacy: _LegacyReplay) -> bool:
    """창 안의 편집이 옛 머리의 되풀이 그대로인가 — 종류·조각·값·개수와 끝의 미검토 스냅숏까지.

    끼어든 사람 편집(기계 메모 머리를 단 메모 포함)은 매핑에 없는 값이거나 개수를 어긋나게 한다.
    """
    if len(edits) != legacy.end:
        return False
    keys = [_replay_key(edit) for edit in edits]
    if legacy.snapshot:
        last = keys.pop() if keys else None
        if last is None or last[0] != "note" or last[1] not in legacy.new_ids:
            return False
        if last[2] != _UNREVIEWED_KEPT_LABEL:
            return False
    return None not in keys and Counter(keys) == legacy.expected


def _auto_excluded(
    segment: ReviewSegment,
    revision: ReviewRevision,
    row: AnalysisSourceResult,
    match: Mapping[str, object],
) -> ReviewSegment:
    """자동 제외 후보에 든 조각 — 미검토 표지와 근거를 함께 단다(D35)."""
    clock = revision.clock_for(segment.source_id)
    assert clock is not None
    provenance = {
        **match,
        "source_id": segment.source_id,
        "source_sha256": clock.source_sha256,
        "analysis_revision_id": row.revision_id,
        "segment_id": segment.segment_id,
    }
    return replace(
        segment,
        selection="excluded",
        visual_tags=(*confirm_tags(segment.visual_tags), AUTO_EXCLUDED_UNREVIEWED_TAG),
        notes=_AUTO_NOTE_PREFIX + json.dumps(provenance, ensure_ascii=False, sort_keys=True),
    )


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
        *,
        preserve_source_ids: tuple[str, ...] = (),
        evidence_rows: Mapping[str, AnalysisSourceResult] | None = None,
        expected_head: str | None = None,
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
        return self._publish_new_base(
            revision, analysis_revision_id, evidence_rows, preserve_source_ids, expected_head
        )

    def _publish_new_base(
        self,
        revision: ReviewRevision,
        analysis_revision_id: str | None,
        evidence_rows: Mapping[str, AnalysisSourceResult] | None,
        preserve_source_ids: tuple[str, ...],
        expected_head: str | None,
    ) -> ReviewRevision:
        """옛 머리를 읽어 사람 결정을 넘겨받는 데서 출판까지 한 자물쇠 안(D53).

        그 사이 편집이 옛 기본에 내려앉아 조용히 사라지지 않게. `expected_head`를 주면 옛 머리가
        그것이 아닐 때 거절한다 — 오래 걸린 다시 세우기가 그 사이 나온 새 머리를 덮지 않게.
        사람이 손댄 원본을 이어받았으면 그 기록을 스냅숏으로 남긴다(`CARRIED_DECISIONS_LABEL`).
        """
        with self._write_lock():
            prior = self.load_head().revision if self.has_review() else None
            if expected_head is not None and (prior is None or prior.revision_id != expected_head):
                raise StaleRevisionError(
                    f"다시 세우는 동안 검토가 바뀌었다 — 읽은 머리 {expected_head}, 지금 머리"
                    f" {prior.revision_id if prior else '없음'}"
                )
            human = self.human_source_ids(prior) if prior is not None else frozenset()
            revision, carried = self._automatic_selections(
                revision, analysis_revision_id, evidence_rows, prior, human
            )
            if preserve_source_ids and prior is not None:
                revision, preserved = self._preserved_sources(revision, preserve_source_ids, prior)
                carried = carried | (preserved & human)
            self._persist_new_base(revision)
            if prior is not None and carried:
                revision = self._publish_snapshot_locked(
                    revision,
                    revision.segments,
                    _marker_label(CARRIED_DECISIONS_LABEL, prior, carried),
                )
        return revision

    def _preserved_sources(
        self, revision: ReviewRevision, preserve_source_ids: tuple[str, ...], prior: ReviewRevision
    ) -> tuple[ReviewRevision, frozenset[str]]:
        """보존할 원본은 옛 머리의 조각을 그대로 — 순서만 새 계획의 것. 보존한 원본도 준다."""
        retained = {
            source_id
            for source_id in preserve_source_ids
            if prior.clock_for(source_id) == revision.clock_for(source_id)
            and revision.clock_for(source_id) is not None
        }
        orders = {segment.source_id: segment.source_order for segment in revision.segments}
        segments = [segment for segment in revision.segments if segment.source_id not in retained]
        # 지금 판정 증거가 없어 옛 조각을 들고 오는 원본도 옛 판 제안은 거둔다(D52·D53) — 근거가
        # 사라진 기계 제안이 출력을 막고, 머리를 늘 '옮길 머리'로 남겨 다시 세우기를 되풀이시킨다.
        segments.extend(
            _release_stale_tier_proposal(replace(segment, source_order=orders[segment.source_id]))
            for segment in prior.segments
            if segment.source_id in retained
        )
        ordered = sorted(segments, key=lambda segment: (segment.source_order, segment.start_pts))
        return replace(revision, segments=tuple(ordered)), frozenset(retained)

    def _write_lock(self) -> threading.RLock:
        return review_write_lock(self.root)

    def _require_latest_base(self, base_plan_id: str) -> None:
        """쓰기는 최신 기본에만 — 옛 기본에 쓰면 새 머리에서 조용히 사라진다(D53).

        순서 맞춤·다시 세우기가 새 기본을 낸 뒤 옛 화면이 보낸 편집은 시끄럽게 거절된다.
        """
        latest = self._latest_base_plan()
        if base_plan_id != latest:
            raise StaleRevisionError(
                f"검토가 새 계획 {latest}로 옮겨졌다(순서 맞춤·다시 세우기) — 새로고침 뒤 다시"
                f" 하세요 (보낸 계획 {base_plan_id})"
            )

    def _automatic_selections(
        self,
        revision: ReviewRevision,
        analysis_revision_id: str | None,
        evidence_rows: Mapping[str, AnalysisSourceResult] | None,
        prior: ReviewRevision | None,
        human: frozenset[str],
    ) -> tuple[ReviewRevision, frozenset[str]]:
        """분석 증거로 자동 제외 후보를 붙이고 3단 등급을 매긴다(D35·D51).

        `evidence_rows`는 원본마다 다른 분석 리비전의 결과 행이다(등급판 옮기기). 없으면
        분석 리비전 하나의 행을 쓴다. 옛 머리(`prior`)에서 사람이 손댄 원본(`human`)은 그
        조각을 이어받는다 — 이어받은 원본을 함께 돌려준다.
        """
        if evidence_rows is None:
            store = AnalysisStore(self._workspace)
            analysis = self._analysis_revision(store, analysis_revision_id)
            if analysis is None:
                # 분석 전에도 사람 손(미리보기 카드 빼기·메모)은 이어받는다 — 버리지 않는다.
                kept_segments, kept = self._keep_edited_sources(
                    revision, list(revision.segments), prior, human
                )
                return replace(revision, segments=tuple(kept_segments)), kept
            rows = {row.asset_id: row for row in store.list_source_results(analysis.revision_id)}
        else:
            rows = dict(evidence_rows)
        reports: dict[str, tiering.TierReport | None] = {}
        segments = [
            self._automatic_exclusion(segment, revision, rows.get(segment.source_id), reports)
            for segment in revision.segments
        ]
        segments, kept = self._keep_edited_sources(revision, segments, prior, human)
        graded = tuple(self._graded(segment, revision, rows, reports) for segment in segments)
        return replace(revision, segments=graded), kept

    def _tier_for(
        self, row: AnalysisSourceResult, reports: dict[str, tiering.TierReport | None]
    ) -> tiering.TierReport | None:
        """원본의 등급 보고서 — 한 번의 조립 안에서는 한 번만 읽는다."""
        if row.asset_id not in reports:
            reports[row.asset_id] = self._service.tier_report(
                self.root, row.revision_id, row.asset_id
            )
        return reports[row.asset_id]

    def _removal_candidates(
        self,
        row: AnalysisSourceResult,
        record: Mapping[str, object],
        reports: dict[str, tiering.TierReport | None],
    ) -> list[Mapping[str, object]]:
        """저장된 자동 정리 판정 + 빨강 등급 제안 — 둘 다 같은 미검토 제외로 간다."""
        from vlogsrt.project.raw.auto_cleanup import saved_decisions

        automatic = dict(record)
        if row.document_path:
            automatic["auto_decisions"] = saved_decisions(
                self.root / row.document_path, automatic, row.asset_id, row.revision_id
            )
        candidates = [
            _require_mapping(d, "auto_decisions[]")
            for d in _require_list(automatic.get("auto_decisions", []), "auto_decisions")
        ]
        report = self._tier_for(row, reports) if row.document_path else None
        return candidates + (tiering.red_decisions(report) if report is not None else [])

    def _automatic_exclusion(
        self,
        segment: ReviewSegment,
        revision: ReviewRevision,
        row: AnalysisSourceResult | None,
        reports: dict[str, tiering.TierReport | None],
    ) -> ReviewSegment:
        if row is None or row.status not in _EVIDENCE_STATUSES or not row.record_path:
            return replace(
                segment,
                recommendation="review_required",
                notes="확인 필요: 분석 미완료 또는 실패",
            )
        start, end = _segment_seconds(revision, segment)
        record = self._read_json(row.record_path, "자동 정리 근거")
        match = next(
            (
                d
                for d in self._removal_candidates(row, record, reports)
                if _fraction_from_json(d["start"], "start") <= start
                and _fraction_from_json(d["end"], "end") >= end
            ),
            None,
        )
        if match is not None:
            return _auto_excluded(segment, revision, row, match)
        if record.get("review_reasons") or segment.speech_status == SPEECH_ANALYSIS_FAILED:
            reasons = _require_list(record.get("review_reasons", []), "review_reasons")
            return replace(
                segment,
                recommendation="review_required",
                notes="확인 필요: " + "; ".join(str(r) for r in reasons),
            )
        return segment

    def _keep_edited_sources(
        self,
        revision: ReviewRevision,
        segments: list[ReviewSegment],
        prior: ReviewRevision | None,
        human: frozenset[str],
    ) -> tuple[list[ReviewSegment], frozenset[str]]:
        """재분석은 사람이 이미 편집한 원본을 덮어쓰지 않는다 — 그 원본만 이어받는다(D53).

        sequence가 아니라 원본마다의 사람 손으로 본다 — 이어받은 새 기본(sequence 0)도 사람 결정을
        싣고 있고, 기계가 다시 적은 편집뿐인 머리는 아무것도 붙잡지 않는다. 사람이 손대지 않은
        원본은 새 분석 결과로 선다(영상을 바꿔 고를 때 그 영상의 추천이 보이게).
        """
        if prior is None or not human:
            return segments, frozenset()
        old_clocks = {c.source_id: c for c in prior.clocks}
        retained = {
            c.source_id
            for c in revision.clocks
            if c.source_id in human and old_clocks.get(c.source_id) == c
        }
        source_orders = {s.source_id: s.source_order for s in segments}
        kept = [s for s in segments if s.source_id not in retained] + [
            replace(s, source_order=source_orders[s.source_id])
            for s in prior.segments
            if s.source_id in retained
        ]
        return sorted(kept, key=lambda s: (s.source_order, s.start_pts)), frozenset(retained)

    def _graded(
        self,
        segment: ReviewSegment,
        revision: ReviewRevision,
        rows: Mapping[str, AnalysisSourceResult],
        reports: dict[str, tiering.TierReport | None],
    ) -> ReviewSegment:
        """3단 등급 — 조각을 덮는 등급 구간에서 권고와 태그를 읽는다(D51)."""
        row = rows.get(segment.source_id)
        if row is None or row.status not in _EVIDENCE_STATUSES or not row.document_path:
            return segment
        report = self._tier_for(row, reports)
        if report is None:
            return segment
        start, end = _segment_seconds(revision, segment)
        recommendation, tags = tiering.segment_grade(report, start, end)
        return replace(
            segment,
            recommendation=recommendation,
            visual_tags=(*tiering.strip_grade_tags(segment.visual_tags), *tags),
        )

    # ── 등급판 옮기기(W-11, D51) ──────────────────────────────────

    def tier_evidence_rows(self) -> dict[str, AnalysisSourceResult]:
        """원본마다 가장 최근의 판정 증거 행 — 원본별로 다른 분석 리비전일 수 있다."""
        store = AnalysisStore(self._workspace)
        rows: dict[str, AnalysisSourceResult] = {}
        for revision in reversed(store.list_revisions()):
            for row in store.list_source_results(revision.revision_id):
                if (
                    row.asset_id not in rows
                    and row.status in _EVIDENCE_STATUSES
                    and row.document_path
                    and row.record_path
                ):
                    rows[row.asset_id] = row
        return rows

    @staticmethod
    def has_human_decisions(revision: ReviewRevision) -> bool:
        """사람이 내린 결정이 조각 내용에 실린 머리인가 — sequence 0이어도 옛 머리에서 실려 온다.

        `initialize_review(preserve_source_ids=…)`와 재분석의 원본 보존은 옛 머리의 편집된
        조각을 새 기본 리비전(sequence 0)에 옮긴다. 내용만 본다 — 경계·기록까지 보는 판정은
        `human_source_ids`다.
        """
        return any(_human_segment(segment) for segment in revision.segments)

    def human_source_ids(self, revision: ReviewRevision) -> frozenset[str]:
        """사람 손이 닿은 원본 — 내용·경계·편집 기록으로 원본마다 본다(D53).

        내용: 사람 결정 표지·깨진 근거 메모. 경계: 기계가 세운 계획(의미 분석·결정적 타일)의
        (원본, 경계) 밖 — 입양은 사람이 나눈 경계도 사람 구간 계획(`planned`)에 옮기므로 그것과는
        견주지 않는다. 기록: 미리 채워진 메모 칸에 덧붙인 사람 메모·통째 합친 경계처럼 내용으로는
        모르는 손은 편집 기록이 말한다(`_history_sources`).
        """
        present = {clock.source_id for clock in revision.clocks}
        found = {s.source_id for s in revision.segments if _human_segment(s)}
        history, summarized = self._history_sources(revision)
        found |= history
        if not summarized:
            found |= self._unplanned_geometry_sources(revision)
        return frozenset(found & present)

    def is_untouched(self, revision: ReviewRevision) -> bool:
        """증거로 다시 세워도 잃을 것이 없는 머리 — 어느 원본에도 사람 손이 없다.

        sequence가 아니라 내용·경계·기록으로 본다(D53) — 입양은 기계 제안을 편집으로 다시 적어
        sequence를 올리고(실데이터 136), 게시는 사람 결정을 이어받은 새 기본을 sequence 0으로 낸다.
        """
        return not self.human_source_ids(revision)

    def _unplanned_geometry_sources(self, revision: ReviewRevision) -> set[str]:
        """기계 계획 어디에도 없는 경계의 원본 — 요약 표지가 없는 머리만 모든 계획을 훑는다.

        요약 표지(입양·이어받음)는 쓴 순간 그 앞의 사람 경계까지 원본 목록으로 적어 두었다. 표지가
        없는 머리(사람 구간 계획 위 머리·D53 이전 코드가 표지 없이 이어받은 분할)는 경계를 기계 계획
        전부와 견주어야 사람 경계를 찾는다(실데이터 약 12 ms).
        """
        machine = {
            (entry.asset_id, entry.core_start_pts, entry.core_end_pts)
            for plan in self._service.list_segment_plan_revisions(self.root)
            if plan.basis != SEGMENTATION_BASIS_PLANNED
            for entry in plan.entries
        }
        return {
            s.source_id
            for s in revision.segments
            if (s.source_id, s.start_pts, s.end_pts) not in machine
        }

    def _history_sources(self, revision: ReviewRevision) -> tuple[set[str], bool]:
        """편집 기록을 거꾸로 걸어 사람 손이 닿은 원본을 모은다 — (원본, 요약됐나).

        가장 가까운 요약 표지(입양·이어받음)에서 멈춘다 — 표지는 쓴 순간 그 앞의 사람 손을 원본
        목록으로 적어 두었다. 그래서 걸음은 이 기본에서 표지 뒤에 생긴 편집만큼이고, 게시가 몇 번
        이어져도 길어지지 않는다(재귀 없음). 되돌리기 `undo:N`은 리비전 N으로 건너뛴다 — 사이의
        편집은 남은 것이 없다. 기계 스냅숏은 지나고, 기록이 끊기면 모두 사람 손이다(요약된 셈).
        """
        present = {clock.source_id for clock in revision.clocks}
        found: set[str] = set()
        cursor, legacy = revision, self._legacy_replay(revision)
        while True:
            if legacy is not None and cursor.sequence <= legacy.end:
                if legacy.end and not self._replay_matches(cursor.base_plan_id, legacy):
                    # 확인 못 한 창은 건너뛰지 않는다 — 되풀이까지 걷고 맨 앞에서 옛 머리로 간다.
                    legacy = replace(legacy, end=0)
                    continue
                # 확인한 옛 입양의 되풀이 — 옛 머리의 기록으로 건너뛴다(재귀 없이).
                old = self._revision_by_id(legacy.old_revision_id)
                if old is None:
                    return present, True
                cursor, legacy = old, self._legacy_replay(old)
                continue
            if cursor.edit is None or found >= present:
                return found, found >= present
            label = (cursor.edit.notes or "") if cursor.edit.kind == "note" else ""
            is_marker, summarized = _marker_sources(label)
            if is_marker:
                return found | (present if summarized is None else set(summarized)), True
            restored = self._undo_target(cursor, label)
            parent = restored or self._revision_by_id(cursor.parent_revision_id)
            if parent is None:
                return present, True
            if restored is None and not label.startswith(_MACHINE_EDIT_LABELS):
                found |= _edit_sources(cursor.edit, label, parent, cursor)
            cursor = parent

    def _legacy_replay(self, revision: ReviewRevision) -> _LegacyReplay | None:
        """표지 없는 옛 입양(D53 이전 앱)의 되풀이 창 — 입양 기록과 옛 머리로 다시 센다.

        입양은 옛 조각마다 제외 선택·덮기·메모를 한 편집씩 다시 적고, 미검토 제외가 있었으면 표지를
        되돌리는 스냅숏 하나를 더한다. 창을 개수로만 믿지 않는다 — 건너뛰기 전에 `_replay_matches`가
        편집 하나하나를 확인한다. 옛 머리가 없으면 None(건너뛸 곳이 없다 — 모두 걷는다).
        """
        record = self._store.adoption(revision.base_plan_id)
        old = None if record is None else self._revision_by_id(record[0])
        if record is None or old is None:
            return None
        new_ids = {
            str(pair["old_segment_id"]): str(pair["new_segment_id"])
            for pair in json.loads(record[1])["pairs"]
        }
        expected = _expected_replay(old, new_ids)
        snapshot = any(is_unreviewed_auto_exclusion(s) for s in old.segments)
        end = -1 if expected is None else sum(expected.values()) + int(snapshot)
        return _LegacyReplay(
            end=end if 0 <= end <= revision.sequence else 0,
            old_revision_id=record[0],
            expected=expected or Counter(),
            snapshot=snapshot,
            new_ids=frozenset(new_ids.values()),
        )

    def _replay_matches(self, base_plan_id: str, legacy: _LegacyReplay) -> bool:
        """옛 입양의 창 안 편집이 모두 되풀이인가 — 하나라도 다르면 그 기본은 다시 셀 수 없다."""
        edits = self._store.edit_documents(self._project_id, base_plan_id, legacy.end)
        return _replay_window_matches(edits, legacy)

    def _undo_target(self, revision: ReviewRevision, label: str) -> ReviewRevision | None:
        """`undo:N` 스냅숏이 되돌린 리비전 N — 같은 기본의 더 앞 리비전일 때만(아니면 None)."""
        if not label.startswith(_UNDO_PREFIX):
            return None
        sequence = label.removeprefix(_UNDO_PREFIX)
        if not sequence.isdigit() or int(sequence) >= revision.sequence:
            return None
        return self._revision_by_id(
            f"{self._project_id}/{revision.base_plan_id}/{int(sequence):06d}"
        )

    def _revision_by_id(self, revision_id: str | None) -> ReviewRevision | None:
        """`프로젝트/기본 계획/순번` 리비전 하나 — 없으면 None(기록이 끊겼으면 사람 손으로 본다)."""
        if revision_id is None or revision_id.count("/") < 2:
            return None
        _project, base_plan_id, _sequence = revision_id.rsplit("/", 2)
        try:
            document, _ = self._store.revision_document(
                self._project_id, base_plan_id, self._sequence_of_revision_id(revision_id)
            )
        except RawWorkspaceError:
            return None
        return parse_revision(document)

    @staticmethod
    def stale_tier_proposal_spans(revision: ReviewRevision) -> tuple[tuple[str, int, int], ...]:
        """옛 등급판이 낸, 지금은 빨강이 아닌 미검토 제외의 (원본, 경계)(D53 실데이터 모양)."""
        return tuple(
            sorted(
                (segment.source_id, segment.start_pts, segment.end_pts)
                for segment in revision.segments
                if _stale_tier_proposal(segment)
            )
        )

    @classmethod
    def has_stale_tier_proposals(cls, revision: ReviewRevision) -> bool:
        return bool(cls.stale_tier_proposal_spans(revision))

    def needs_tier_migration(self, revision: ReviewRevision) -> bool:
        """등급판을 옮길 머리인가 — 지금 판 표지가 없거나, 표지가 있어도 옛 판 제안이 실려 있다."""
        return not self.has_tier_marker(revision) or self.has_stale_tier_proposals(revision)

    @staticmethod
    def has_tier_marker(revision: ReviewRevision) -> bool:
        """지금 등급판으로 매긴 조각이 하나라도 있으면 옮긴 머리다."""
        marker = tiering.tier_marker()
        return any(marker in segment.visual_tags for segment in revision.segments)

    def retier(self, base_plan_id: str | None = None) -> ReviewRevision:
        """권고와 등급 태그를 다시 매긴다 — 사람이 정한 선택·메모·덮기와 경계는 그대로(D51, Q8).

        사람이 등급을 덮은 조각은 권고도 그대로 두고 태그만 새로 단다. 예외 하나: 옛 등급판이 낸
        미검토 제외 제안은 지금 등급이 빨강이 아니면 사용으로 되돌린다(D52 — 사람이 아직 정하지
        않은 기계 제안이다). 바뀐 것이 없으면 리비전을 만들지 않는다.
        """
        head = self.load_head(base_plan_id).revision
        rows = self.tier_evidence_rows()
        reports: dict[str, tiering.TierReport | None] = {}
        segments = []
        for segment in head.segments:
            graded = self._graded(segment, head, rows, reports)
            if segment.override_recommendation is not None:
                graded = replace(graded, recommendation=segment.recommendation)
            segments.append(_release_stale_tier_proposal(graded))
        if tuple(segments) == head.segments:
            return head
        return self._publish_snapshot(
            head, tuple(segments), "등급 재계산 " + tiering.TIER_SETTINGS.tier_version
        )

    def _publish_snapshot(
        self, head: ReviewRevision, segments: tuple[ReviewSegment, ...], label: str
    ) -> ReviewRevision:
        with self._write_lock():
            self._require_latest_base(head.base_plan_id)
            return self._publish_snapshot_locked(head, segments, label)

    def _publish_snapshot_locked(
        self, head: ReviewRevision, segments: tuple[ReviewSegment, ...], label: str
    ) -> ReviewRevision:
        edited = replace(
            head,
            sequence=head.sequence + 1,
            segments=segments,
            parent_revision_id=head.revision_id,
            edit=note_edit(head.revision_id, head.segments[0].segment_id, label),
        )
        # Round-trip through domain validation before the atomic compare-and-swap.
        edited = parse_revision(serialize_revision(edited))
        self._store.publish_edit_revision(
            project_id=self._project_id,
            base_plan_id=head.base_plan_id,
            document_json=serialize_revision(edited),
            content_fingerprint=content_fingerprint(edited),
            expected_head_revision_id=head.revision_id,
            new_head_revision_id=edited.revision_id,
            updated_at=_now(),
        )
        return edited

    def undo(self, base_plan_id: str | None = None) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        cursor = head
        if head.edit and head.edit.notes and head.edit.notes.startswith("undo:"):
            document, _ = self._store.revision_document(
                self._project_id, head.base_plan_id, int(head.edit.notes.split(":", 1)[1])
            )
            cursor = parse_revision(document)
        marker = (cursor.edit.notes or "") if cursor.edit and cursor.edit.kind == "note" else ""
        # 요약 표지(입양·이어받음)는 이 기본의 출발점이다 — 되돌릴 사람 편집이 아니다. 넘어가면
        # 그 앞의 되풀이까지 되돌려 이어받은 결정이 사라지고, 사람 손의 요약도 끊긴다(D53).
        if cursor.parent_revision_id is None or _marker_sources(marker)[0]:
            raise ReviewWorkspaceError("실행 취소할 편집이 없습니다")
        sequence = self._sequence_of_revision_id(cursor.parent_revision_id)
        document, _ = self._store.revision_document(self._project_id, head.base_plan_id, sequence)
        return self._publish_snapshot(head, parse_revision(document).segments, f"undo:{sequence}")

    def restore_automatic(self, base_plan_id: str | None = None) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        manually_selected: set[str] = set()
        cursor = head
        legacy = self._legacy_replay(head)
        while cursor.parent_revision_id is not None:
            if legacy is not None and 0 < cursor.sequence <= legacy.end:
                if self._replay_matches(head.base_plan_id, legacy):
                    break  # 확인한 옛 입양의 되풀이는 사람 선택이 아니다
                legacy = None  # 확인 못 한 창 — 그 선택까지 사람 선택으로 셈해 제외를 남긴다
            label = (cursor.edit.notes or "") if cursor.edit and cursor.edit.kind == "note" else ""
            # 입양 표지 앞은 입양의 되풀이다 — 사람 선택은 `수동 선택 · ` 메모로 이미 실려 와 복원이
            # 건너뛴다. 되풀이의 선택 편집까지 사람 선택으로 셈하면 복원이 아무것도 하지 않는다.
            if label.startswith(ADOPTION_REPLAY_LABEL + " ← "):
                break
            if cursor.edit and cursor.edit.kind == "selection":
                manually_selected.update(cursor.edit.segment_ids)
            document, _ = self._store.revision_document(
                self._project_id,
                head.base_plan_id,
                self._sequence_of_revision_id(cursor.parent_revision_id),
            )
            cursor = parse_revision(document)
        segments = tuple(
            replace(s, selection="included", visual_tags=confirm_tags(s.visual_tags))
            if s.notes.startswith(_AUTO_NOTE_PREFIX) and s.segment_id not in manually_selected
            else s
            for s in head.segments
        )
        return self._publish_snapshot(head, segments, "자동 제외 전체 복원")

    def confirm_automatic_exclusions(self, base_plan_id: str | None = None) -> ReviewRevision:
        """미검토 자동 제외를 모두 확인한다 — 제외는 그대로 두고 표지만 거둔다(D35).

        사람이 누른 이 한 번이 새 리비전이다. 확인할 것이 없으면 리비전을 만들지 않는다.
        """
        head = self.load_head(base_plan_id).revision
        if not unreviewed_auto_exclusions(head):
            return head
        segments = tuple(
            replace(s, visual_tags=confirm_tags(s.visual_tags))
            if is_unreviewed_auto_exclusion(s)
            else s
            for s in head.segments
        )
        return self._publish_snapshot(head, segments, "자동 제외 확인")

    def unreviewed_auto_exclusion_count(self, base_plan_id: str | None = None) -> int:
        return len(unreviewed_auto_exclusions(self.load_head(base_plan_id).revision))

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
        path = self._workspace.source_media_path(asset)
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
        path = self._workspace.source_media_path(asset)
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
        if row.status in ("analyzed", "cached", "partial"):
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

    def has_review(self) -> bool:
        """검토 리비전이 하나라도 있는가 — 없는 것은 오류가 아니라 아직 시작 전이다."""
        return bool(self._store.list_base_plan_ids(self._project_id))

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
            try:
                path = self._workspace.source_media_path(asset)
            except RawWorkspaceError as error:
                stale.append((clock.source_id, str(error)))
                continue
            if not path.is_file():
                stale.append((clock.source_id, "원본 파일이 없다"))
            elif path.stat().st_size != asset.size_bytes:
                stale.append((clock.source_id, "원본 크기가 기록과 다르다"))
        return tuple(stale)

    def apply_edit(self, base_plan_id: str, edit: ReviewEdit) -> ReviewRevision:
        """편집을 헤드에 적용해 저장한다 — 리비전과 헤드 이동은 한 트랜잭션이다.

        헤드가 편집 대상이 아니면 거부다(stale-head). 최신 기본이 아니어도 거부다 — 입양·다시
        세우기가 도는 동안 온 편집은 그것이 끝날 때까지 기다렸다가 시끄럽게 거절된다(D53).
        출판 도중 무엇이 실패해도(크래시·잠금·동시 편집) 리비전과 헤드는 함께 되돌아가므로,
        실패한 편집이 다음 편집의 자리를 막는 일은 없다.
        """
        with self._write_lock():
            self._require_latest_base(base_plan_id)
            return self._apply_edit_locked(base_plan_id, edit)

    def _apply_edit_locked(self, base_plan_id: str, edit: ReviewEdit) -> ReviewRevision:
        head = self.load_head(base_plan_id).revision
        if edit.target_revision_id != head.revision_id:
            raise StaleRevisionError(
                f"편집은 {edit.target_revision_id!r}를 겨냥했는데 지금 헤드는 "
                f"{head.revision_id!r}다"
            )
        if edit.kind not in EDIT_KINDS:
            raise ReviewWorkspaceError(f"알 수 없는 편집 종류 {edit.kind!r}")
        edited = apply_edit(head, edit)
        if edit.kind == "selection":
            edited = replace(
                edited,
                segments=tuple(
                    replace(segment, notes=MANUAL_SELECTION_PREFIX + segment.notes)
                    if segment.segment_id in edit.segment_ids
                    and segment.notes.startswith(_AUTO_NOTE_PREFIX)
                    else segment
                    for segment in edited.segments
                ),
            )
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
        self, base_plan_id: str | None = None, *, order_revision_id: str | None = None
    ) -> tuple[SegmentPlanSnapshot, ReviewPlanMapping]:
        """편집된 덮개를 새 분할 계획으로 — 경계 왕복이 항등임을 검증하고 매핑을 남긴다.

        `order_revision_id`를 주면 그 전체 순서 위에 세운다 — 순서만 바뀐 머리를 옮길 때다(D53).
        매핑은 (원본, 경계)로 맞추므로 순서와 무관하다.
        """
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
            order_revision_id=order_revision_id or snapshot.order_revision_id,
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
        옛 리비전이 지금 최신 머리가 아니면 거부한다 — 그 사이 나온 새 기본·편집을 옛 머리로
        덮어 조용히 지우지 않는다(D53). 입양 내내 쓰기 자물쇠를 쥐어 편집은 기다렸다 거절된다.
        """
        with self._write_lock():
            latest = self.load_head().revision
            old = old_revision if old_revision is not None else latest
            if latest.revision_id != old.revision_id:
                raise StaleRevisionError(
                    f"입양할 머리 {old.revision_id}가 지금 최신 머리 {latest.revision_id}가"
                    " 아니다 — 그 사이 검토가 바뀌었다"
                )
            return self._adopt_locked(new_snapshot, mapping, old)

    def _adopt_locked(
        self,
        new_snapshot: SegmentPlanSnapshot,
        mapping: ReviewPlanMapping,
        old: ReviewRevision,
    ) -> ReviewRevision:
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
        # 입양은 경계만 옮긴다 — 등급은 저장된 증거로 새 조각마다 다시 매긴다(D51).
        rows = self.tier_evidence_rows()
        reports: dict[str, tiering.TierReport | None] = {}
        adopted = replace(
            adopted,
            segments=tuple(self._graded(s, adopted, rows, reports) for s in adopted.segments),
        )
        self._persist_new_base(adopted, mapping)
        human = self.human_source_ids(old)
        replayed = self._replay_metadata(new_snapshot.revision_id, old, mapping)
        # 되풀이가 끝난 자리에 옛 머리의 사람 손을 요약해 둔다 — 이 뒤의 편집만 새로 생긴 손이다.
        return self._publish_snapshot_locked(
            replayed, replayed.segments, _marker_label(ADOPTION_REPLAY_LABEL, old, human)
        )

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
        # 선택 편집을 되풀이하면 표지가 지워지므로, 사람이 보지 않은 것은 따로 되돌려 놓는다.
        carried = {
            new_ids[old_segment.segment_id]
            for old_segment in old.segments
            if is_unreviewed_auto_exclusion(old_segment)
        }
        if not carried:
            return revision
        segments = tuple(
            replace(s, visual_tags=(*confirm_tags(s.visual_tags), AUTO_EXCLUDED_UNREVIEWED_TAG))
            if s.segment_id in carried and s.selection == SELECTION_EXCLUDED
            else s
            for s in revision.segments
        )
        return self._publish_snapshot(revision, segments, _UNREVIEWED_KEPT_LABEL)

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
            source_original_relative_path=(
                self._workspace.source_reference_path(asset.asset_id) or asset.relative_path
            ),
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
        if row.status in ("analyzed", "cached", "partial"):
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
        expected_review_revision_id: str | None = None,
    ) -> ConfirmedExportOutcome:
        """검토 리비전을 확정 폴더로 — 실제 바이트 복사·no-replace(전체 또는 선택).

        `expected_review_revision_id`는 호출자가 **제출 때 본 리비전**이다. 주어지면
        처음 잡은 리비전과 아이디가 같은지를 복사 전에 비교하고, 어긋나면
        `StaleRevisionError`로 0바이트 상태로 닫는다 — 비동기 작업 본문이 제출과
        실행 사이에 머리가 움직였는지 스스로 증명하는 장치다. 검사를 지난 뒤에는
        이 호출이 처음 잡은 **그 리비전 객체**로 끝까지 복사·기록한다(머리를 다시
        읽지 않는다).
        """
        revision = self.load_head(base_plan_id).revision
        if expected_review_revision_id is not None and (
            revision.revision_id != expected_review_revision_id
        ):
            raise StaleRevisionError(
                f"내보내기가 묶인 검토 리비전 {expected_review_revision_id!r}이(가)"
                f" 지금 머리 {revision.revision_id!r}와 다르다 — 제출 뒤 검토가"
                " 바뀌었다. 다시 불러서 다시 내보내야 한다"
            )
        require_automatic_exclusions_reviewed(revision)
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
