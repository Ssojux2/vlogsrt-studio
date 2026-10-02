"""A7-3 수동 검토 리비전의 순수 도메인 — 불변 큐 기록·편집 감사·정준 직렬화.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.7·§11.11·§16 A7-3. 자동 결과는
얼어 붙은 증거이고, 사람의 손이 닿는 것은 **별도 리비전**이다(§11.7: "자동 결과는
불변이고 수동 변경은 별도 patch/revision"). 이 모듈은 값과 계산만 있다 — 파일·네트워크가
없고, 게시·게이트는 `caption_review.py`가, 자리는 `caption_store.py`가 가진다.

지키는 선:

- **모든 편집은 새 리비전이다.** 리비전을 이루는 기록은 전부 얼어 있고(Frozen
  dataclass + tuple), 거부된 편집은 이전 리비전을 그대로 둔다. 리비전은 자기를 만든
  편집 하나와 부모 해시를 달고 다녀 연쇄가 증명된다.
- **글자 편집은 시각을 절대 못 움직인다.** 시각을 고치려면 사용자가 준 값(유리수
  또는 밀리초 정수)으로 명시적인 시각 편집을 해야 한다 — 추측 배분이 없다(§11.7).
  분할은 나눌 시각과 양쪽 글자를 **모두** 사용자가 준다(균등 배분 금지).
- **병합은 맞닿은 큐만, 글자는 사용자 제공 또는 근거 보존.** 이웃을 건너뛰는
  병합과 지어낸 글자가 없다.
- **수동 큐는 근거가 `manual`로 찍힌다.** 미대응 말을 살리는 새 큐도 마찬가지 —
  ASR 증거를 꾸며 붙이지 않는다(§11.5).
- **직렬화는 엄격하다.** 모르는 스키마·키·타입·범위는 못 들어온다(§13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any

from vlogsrt.project.raw.caption_store import MANUAL_SCHEMA, sha256_text

TEXT_BASIS_AUTO = "auto"
TEXT_BASIS_MANUAL = "manual"
TEXT_BASES: tuple[str, ...] = (TEXT_BASIS_AUTO, TEXT_BASIS_MANUAL)
"""큐 글자의 근거 — 자동(모델 증거)과 수동(사람 제공)만 있다(§11.5)."""

MANUAL_EDIT_KINDS: tuple[str, ...] = (
    "text",
    "time",
    "split",
    "merge",
    "add",
    "allow",
    "resolve",
    "no_speech",
)
"""리비전을 만드는 편집의 여덟 종류 — 이외의 종류는 적용 거부다."""


class ManualReviewError(RuntimeError):
    """수동 검토 도메인의 오류 — 메시지가 대상·좌표·값을 말한다."""


class ManualValidationError(ManualReviewError):
    """리비전 구성이 계약을 어겼다 — 만들어지지 않고 이유가 나온다."""


class ManualEditError(ManualReviewError):
    """편집이 거부됐다 — 이전 리비전은 그대로다."""


class StaleManualRevision(ManualEditError):
    """편집이 겨냥한 리비전이 지금 손에 있는 것이 아니다 — 못박힌 대상만 고친다."""


class ManualParseError(ManualReviewError):
    """직렬화 문서가 계약 밖이다 — 모르는 스키마·타입·범위는 못 들어온다."""


def ms(value: int) -> Fraction:
    """밀리초 정수를 정확한 유리수 초로 — 시각 편집 입력의 하나뿐인 변환."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManualEditError(f"밀리초는 정수여야 한다: {value!r}")
    return Fraction(value, 1000)


def _as_time(value: Fraction | int, name: str) -> Fraction:
    """사용자가 준 시각 — `Fraction` 그대로 또는 밀리초 정수. float 추측은 없다."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return ms(value)
    raise ManualEditError(f"{name}는 유리수 또는 밀리초 정수여야 한다: {value!r}")


# ─────────────────────────────────────────────────────────────
#  기록 — 신원·큐·편집·리비전
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AutoIdentity:
    """자동 스냅샷의 신원 — 렌더 해시·run·지문·자동 내용 해시가 전부 어긋나야 남(§11.1)."""

    final_video_revision: str
    run_id: str
    fingerprint: str
    auto_content_sha256: str


@dataclass(frozen=True, slots=True)
class ReviewCue:
    """검토 큐 하나 — 안정 ID와 세 겹의 글자를 함께 실는다(§11.7 화면 4).

    `whisper_text`가 시간 골격의 원문, `auto_text`가 자동 채택 문구, `text`가 지금
    글자다 — 셋을 나란히 보여 비교 검토를 한다. `text_basis`가 `manual`인 글자는
    ASR 증거가 아니라 사람 제공이다. 시각은 최종 영상 시각의 정확한 유리수다.
    """

    cue_id: str
    start: Fraction
    end: Fraction
    text: str
    text_basis: str
    auto_text: str
    whisper_text: str
    timing_segment_ids: tuple[int, ...]
    timing_evidence_id: str
    text_evidence_ids: tuple[str, ...]
    correction_status: str
    flags: tuple[str, ...]
    manual_note: str = ""

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Allowance:
    """경고 하나의 명시적 허용 — 이유와 함께 정확한 리비전에 묶인다(§11.11)."""

    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class UnplacedResolution:
    """미대응 문구 하나의 처리 — 수동 큐로 살리거나 버린 이유를 남긴다(D37·§11.5)."""

    index: int
    text: str
    placed_cue_id: str | None
    dismissed_reason: str | None


@dataclass(frozen=True, slots=True)
class ManualEdit:
    """하나의 편집 명령 — 겨냥한 리비전 번호와 매개변수를 얼어 붙인다."""

    kind: str
    target_revision: int
    cue_ids: tuple[str, ...] = ()
    text: str | None = None
    right_text: str | None = None
    start: Fraction | None = None
    end: Fraction | None = None
    at: Fraction | None = None
    key: str | None = None
    reason: str | None = None
    index: int | None = None
    placed_cue_id: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ManualRevision:
    """수동 리비전 — 자동 신원에 묶이고 부모 해시로 연쇄되는 불변 상태(§11.7).

    `sequence` 0은 자동 결과 그대로의 기본 리비전이다. 허용·미대응 처리·무발화
    결정은 만들어진 리비전의 내용으로 축적된다 — 확정 게이트는 **머리** 리비전의
    것만 본다. `video_duration`·`sample_count`는 스냅샷에서 얼어 온 검증 경계다.
    """

    identity: AutoIdentity
    sequence: int
    cues: tuple[ReviewCue, ...]
    video_duration: Fraction
    sample_count: int
    allowances: tuple[Allowance, ...] = ()
    unplaced_resolutions: tuple[UnplacedResolution, ...] = ()
    no_speech: bool = False
    no_speech_note: str = ""
    parent_revision: int | None = None
    parent_sha256: str = ""
    edit: ManualEdit | None = None

    @property
    def revision_id(self) -> str:
        return f"{self.identity.run_id}#{self.sequence:06d}"


@dataclass(frozen=True, slots=True)
class CaptionViewState:
    """보기 상태 — 선택·필터·검색은 리비전 내용이 아니다(§11.7·A5와 같은 선).

    이 기록은 직렬화되지 않고 `content_fingerprint`에 끼지 않는다. 표시 토글로
    API 호출·재생성이 일어나지 않는다는 계약을 타입으로 지킨다.
    """

    selected_cue_id: str | None = None
    show_unplaced_only: bool = False
    query: str = ""


# ─────────────────────────────────────────────────────────────
#  구성과 검증
# ─────────────────────────────────────────────────────────────


def initial_manual_revision(
    identity: AutoIdentity,
    cues: tuple[ReviewCue, ...],
    *,
    video_duration: Fraction,
    sample_count: int,
) -> ManualRevision:
    """자동 스냅샷 위의 기본 리비전(0) — 큐는 자동 것 그대로, 편집 없음."""
    return _validated(
        identity,
        0,
        cues,
        video_duration,
        sample_count,
        allowances=(),
        resolutions=(),
        no_speech=False,
        no_speech_note="",
        parent_revision=None,
        parent_sha256=identity.auto_content_sha256,
        edit=None,
    )


def _validated(
    identity: AutoIdentity,
    sequence: int,
    cues: tuple[ReviewCue, ...],
    video_duration: Fraction,
    sample_count: int,
    *,
    allowances: tuple[Allowance, ...],
    resolutions: tuple[UnplacedResolution, ...],
    no_speech: bool,
    no_speech_note: str,
    parent_revision: int | None,
    parent_sha256: str,
    edit: ManualEdit | None,
) -> ManualRevision:
    """모든 계약을 통과한 리비전만 나온다 — 어긋나면 리비전은 태어나지 않는다."""
    _check_identity(identity)
    if sequence < 0:
        raise ManualValidationError("sequence는 0 이상이어야 한다")
    if video_duration < 0 or sample_count < 0:
        raise ManualValidationError("영상 길이·샘플 수는 0 이상이어야 한다")
    _check_cues(cues)
    _check_decisions(allowances, resolutions, no_speech, cues)
    _check_lineage(sequence, parent_revision, parent_sha256, edit, identity)
    return ManualRevision(
        identity=identity,
        sequence=sequence,
        cues=cues,
        video_duration=video_duration,
        sample_count=sample_count,
        allowances=allowances,
        unplaced_resolutions=resolutions,
        no_speech=no_speech,
        no_speech_note=no_speech_note,
        parent_revision=parent_revision,
        parent_sha256=parent_sha256,
        edit=edit,
    )


def _check_decisions(
    allowances: tuple[Allowance, ...],
    resolutions: tuple[UnplacedResolution, ...],
    no_speech: bool,
    cues: tuple[ReviewCue, ...],
) -> None:
    """리비전에 실린 검토 결정들 — 허용·처리·무발화 각각의 원초적 계약."""
    for allowance in allowances:
        if not allowance.key or not allowance.reason:
            raise ManualValidationError("허용은 key와 reason을 모두 실어야 한다")
    for item in resolutions:
        _check_resolution(item, cues)
    if no_speech and cues:
        raise ManualValidationError("무발화 확정은 빈 실행에만 있다(§11.11)")


def _check_identity(identity: AutoIdentity) -> None:
    if (
        len(identity.final_video_revision) != 64
        or len(identity.fingerprint) != 64
        or len(identity.auto_content_sha256) != 64
    ):
        raise ManualValidationError("신원 해시는 64자여야 한다")
    if not identity.run_id:
        raise ManualValidationError("run_id는 비지 않은 문자열이어야 한다")


def _check_cues(cues: tuple[ReviewCue, ...]) -> None:
    seen: set[str] = set()
    for cue in cues:
        if not cue.cue_id:
            raise ManualValidationError("큐 ID는 비지 않은 문자열이어야 한다")
        if cue.cue_id in seen:
            raise ManualValidationError(f"큐 ID {cue.cue_id!r}가 중복이다")
        seen.add(cue.cue_id)
        if cue.text_basis not in TEXT_BASES:
            raise ManualValidationError(
                f"큐 {cue.cue_id!r}: 알 수 없는 글자 근거 {cue.text_basis!r}"
            )


def _check_resolution(item: UnplacedResolution, cues: tuple[ReviewCue, ...]) -> None:
    if item.index < 0 or not item.text:
        raise ManualValidationError("미대응 처리는 index와 text를 실어야 한다")
    if (item.placed_cue_id is None) == (item.dismissed_reason is None):
        raise ManualValidationError(
            f"미대응 {item.index}: 수동 큐 또는 버린 이유 중 정확히 하나만 실어야 한다"
        )
    if item.placed_cue_id is not None and item.placed_cue_id not in {cue.cue_id for cue in cues}:
        raise ManualValidationError(f"미대응 {item.index}: 가리킨 큐 {item.placed_cue_id!r}가 없다")


def _check_lineage(
    sequence: int,
    parent_revision: int | None,
    parent_sha256: str,
    edit: ManualEdit | None,
    identity: AutoIdentity,
) -> None:
    if edit is not None and edit.kind not in MANUAL_EDIT_KINDS:
        raise ManualValidationError(f"알 수 없는 편집 종류 {edit.kind!r}")
    if len(parent_sha256) != 64:
        raise ManualValidationError("부모 해시는 64자여야 한다")
    if sequence == 0:
        if parent_revision is not None or edit is not None:
            raise ManualValidationError("기본 리비전(0)은 부모도 편집도 없다")
        if parent_sha256 != identity.auto_content_sha256:
            raise ManualValidationError("기본 리비전은 자동 내용 해시에 묶여야 한다")
        return
    if parent_revision != sequence - 1 or edit is None:
        raise ManualValidationError("편집 리비전은 바로 앞 리비전과 편집을 실어야 한다")
    if edit.target_revision != parent_revision:
        raise ManualValidationError(
            f"편집이 겨냥한 {edit.target_revision}가 부모 {parent_revision}가 아니다"
        )


# ─────────────────────────────────────────────────────────────
#  편집 적용 — 늘 새 리비전, 거부면 이전 리비전 그대로
# ─────────────────────────────────────────────────────────────


def apply_edit(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """편집을 리비전에 적용해 **다음** 리비전을 만든다 — 원본은 절대 고쳐지지 않는다.

    `edit.target_revision`가 다르면 `StaleManualRevision`이다: 늦은 완성이 이미
    앞선 편집 위에 덮지 못하게 한다. 거부되는 순간 예외가 나고 리비전은 하나도
    바뀌지 않는다.
    """
    if edit.target_revision != revision.sequence:
        raise StaleManualRevision(
            f"편집은 리비전 {edit.target_revision}을(를) 겨냥했는데 지금 손에 있는 건"
            f" {revision.sequence}이다"
        )
    appliers = {
        "text": _apply_text,
        "time": _apply_time,
        "split": _apply_split,
        "merge": _apply_merge,
        "add": _apply_add,
        "allow": _apply_allow,
        "resolve": _apply_resolve,
        "no_speech": _apply_no_speech,
    }
    applier = appliers.get(edit.kind)
    if applier is None:
        raise ManualEditError(f"알 수 없는 편집 종류 {edit.kind!r}")
    return applier(revision, edit)


def _descendant(revision: ManualRevision, edit: ManualEdit, **changes: Any) -> ManualRevision:
    """다음 리비전의 공통 껍질 — 연쇄 해시는 직렬화된 부모에서 잰다."""
    return _validated(
        revision.identity,
        revision.sequence + 1,
        changes.pop("cues", revision.cues),
        revision.video_duration,
        revision.sample_count,
        allowances=changes.pop("allowances", revision.allowances),
        resolutions=changes.pop("resolutions", revision.unplaced_resolutions),
        no_speech=changes.pop("no_speech", revision.no_speech),
        no_speech_note=changes.pop("no_speech_note", revision.no_speech_note),
        parent_revision=revision.sequence,
        parent_sha256=sha256_text(serialize_revision(revision)),
        edit=edit,
    )


def _find_cue(revision: ManualRevision, cue_id: str) -> ReviewCue:
    for cue in revision.cues:
        if cue.cue_id == cue_id:
            return cue
    raise ManualEditError(f"리비전 {revision.revision_id}에 큐 {cue_id!r}가 없다")


def _require_text(value: str | None, what: str) -> str:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ManualEditError(f"{what}은(는) 겉보기에 비지 않은 문자열이어야 한다")
    return value


def _apply_text(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """글자 편집 — 시각·근거 칸은 그대로, 근거 표식만 `manual`로."""
    (cue_id,) = _require_cue_ids(edit, 1)
    cue = _find_cue(revision, cue_id)
    edited = replace(cue, text=_require_text(edit.text, "새 글자"), text_basis=TEXT_BASIS_MANUAL)
    return _descendant(
        revision,
        edit,
        cues=tuple(edited if item.cue_id == cue_id else item for item in revision.cues),
    )


def _apply_time(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """시각 편집 — 사용자가 준 값만. 음수·뒤집힘은 거부, 범위 이탈은 경고로 남는다."""
    (cue_id,) = _require_cue_ids(edit, 1)
    cue = _find_cue(revision, cue_id)
    start = cue.start if edit.start is None else _as_time(edit.start, "start")
    end = cue.end if edit.end is None else _as_time(edit.end, "end")
    if start < 0 or start >= end:
        raise ManualEditError(
            f"큐 {cue_id!r}: 시각은 0 이상·start < end여야 한다 — [{start}, {end})"
        )
    edited = replace(cue, start=start, end=end)
    return _descendant(
        revision,
        edit,
        cues=tuple(edited if item.cue_id == cue_id else item for item in revision.cues),
    )


def _apply_split(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """분할 — 나눌 시각과 양쪽 글자를 모두 사용자가 준다(균등 배분이 아니다, §11.7)."""
    (cue_id,) = _require_cue_ids(edit, 1)
    cue = _find_cue(revision, cue_id)
    if edit.at is None:
        raise ManualEditError("분할 편집은 나눌 시각을 실어야 한다")
    at = _as_time(edit.at, "나눌 시각")
    if not cue.start < at < cue.end:
        raise ManualEditError(
            f"나눌 시각 {at}은(는) 큐 {cue_id!r} 내부 엄격히 안이어야 한다 [{cue.start}, {cue.end})"
        )
    left_text = _require_text(edit.text, "왼쪽 글자")
    right_text = _require_text(edit.right_text, "오른쪽 글자")
    left = replace(cue, end=at, text=left_text, text_basis=TEXT_BASIS_MANUAL)
    right = ReviewCue(
        cue_id=_next_manual_id(revision),
        start=at,
        end=cue.end,
        text=right_text,
        text_basis=TEXT_BASIS_MANUAL,
        auto_text=cue.auto_text,
        whisper_text=cue.whisper_text,
        timing_segment_ids=cue.timing_segment_ids,
        timing_evidence_id=cue.timing_evidence_id,
        text_evidence_ids=cue.text_evidence_ids,
        correction_status=cue.correction_status,
        flags=(),
    )
    rebuilt: list[ReviewCue] = []
    for item in revision.cues:
        if item.cue_id == cue_id:
            rebuilt.extend((left, right))
        else:
            rebuilt.append(item)
    return _descendant(revision, edit, cues=tuple(rebuilt))


def _apply_merge(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """병합 — 목록에서 맞닿은 이웃만. 글자는 사용자 제공 또는 근거 이어붙임."""
    left_id, right_id = _require_cue_ids(edit, 2)
    left, right = _adjacent_pair(revision, left_id, right_id)
    text = edit.text if edit.text is not None else f"{left.text} {right.text}".strip()
    basis = (
        TEXT_BASIS_MANUAL
        if edit.text is not None or left.text_basis != right.text_basis
        else left.text_basis
    )
    merged = ReviewCue(
        cue_id=left.cue_id,
        start=left.start,
        end=right.end,
        text=_require_text(text, "합친 글자"),
        text_basis=basis,
        auto_text=_joined(left.auto_text, right.auto_text),
        whisper_text=_joined(left.whisper_text, right.whisper_text),
        timing_segment_ids=left.timing_segment_ids + right.timing_segment_ids,
        timing_evidence_id=left.timing_evidence_id or right.timing_evidence_id,
        text_evidence_ids=tuple(dict.fromkeys(left.text_evidence_ids + right.text_evidence_ids)),
        correction_status=left.correction_status,
        flags=(),
    )
    return _descendant(
        revision,
        edit,
        cues=tuple(
            merged if item.cue_id == left.cue_id else item
            for item in revision.cues
            if item.cue_id != right.cue_id
        ),
    )


def _joined(left: str, right: str) -> str:
    return f"{left} {right}".strip()


def _adjacent_pair(
    revision: ManualRevision, left_id: str, right_id: str
) -> tuple[ReviewCue, ReviewCue]:
    """목록에서 이웃한 두 큐 — 건너뛴 병합은 없다(§11.7 '인접 병합')."""
    for position, cue in enumerate(revision.cues):
        if cue.cue_id == left_id:
            if position + 1 >= len(revision.cues) or revision.cues[position + 1].cue_id != right_id:
                raise ManualEditError(
                    f"병합은 맞닿은 이웃만 가능하다: {left_id!r} 뒤는 {right_id!r}가 아니다"
                )
            return cue, revision.cues[position + 1]
    raise ManualEditError(f"리비전 {revision.revision_id}에 큐 {left_id!r}가 없다")


def _apply_add(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """새 큐 — 미대응 말을 수동 시각으로 살린다. 근거는 `manual`로 찍힌다(D37·§11.5)."""
    if edit.start is None or edit.end is None:
        raise ManualEditError("새 큐는 시작·끝 시각을 모두 실어야 한다")
    start = _as_time(edit.start, "start")
    end = _as_time(edit.end, "end")
    if start < 0 or start >= end:
        raise ManualEditError(f"새 큐 시각은 0 이상·start < end여야 한다 — [{start}, {end})")
    cue = ReviewCue(
        cue_id=_next_manual_id(revision),
        start=start,
        end=end,
        text=_require_text(edit.text, "새 큐 글자"),
        text_basis=TEXT_BASIS_MANUAL,
        auto_text="",
        whisper_text="",
        timing_segment_ids=(),
        timing_evidence_id="",
        text_evidence_ids=(),
        correction_status="manual",
        flags=(),
        manual_note=edit.note or "",
    )
    cues = list(revision.cues)
    position = len(cues)
    for index, item in enumerate(cues):
        if (start, end) < (item.start, item.end):
            position = index
            break
    cues.insert(position, cue)
    return _descendant(revision, edit, cues=tuple(cues))


def _apply_allow(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """경고 허용 — key와 이유를 이 리비전에 묶는다(§11.11: 허용 결정을 남긴다)."""
    key = edit.key
    reason = edit.reason
    if not key or not isinstance(key, str) or not reason or not isinstance(reason, str):
        raise ManualEditError("허용은 key와 reason을 모두 실어야 한다")
    return _descendant(revision, edit, allowances=(*revision.allowances, Allowance(key, reason)))


def _apply_resolve(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """미대응 처리 — 수동 큐로 살리거나, 버린 이유를 남긴다."""
    if edit.index is None or edit.index < 0:
        raise ManualEditError("미대응 처리는 index를 실어야 한다")
    record = UnplacedResolution(
        index=edit.index,
        text=_require_text(edit.text, "미대응 원문"),
        placed_cue_id=edit.placed_cue_id,
        dismissed_reason=edit.reason,
    )
    try:
        _check_resolution(record, revision.cues)
    except ManualValidationError as error:
        raise ManualEditError(str(error)) from error  # 편집 거부다 — 리비전은 그대로
    return _descendant(revision, edit, resolutions=(*revision.unplaced_resolutions, record))


def _apply_no_speech(revision: ManualRevision, edit: ManualEdit) -> ManualRevision:
    """무발화 확정 — 빈 실행에만, 사람 결정으로(§11.11: 엔진이 스스로 내리지 않는다)."""
    if revision.cues:
        raise ManualEditError("무발화 확정은 큐가 없는 실행에만 있을 수 있다")
    return _descendant(revision, edit, no_speech=True, no_speech_note=edit.note or "")


def _require_cue_ids(edit: ManualEdit, count: int) -> tuple[str, ...]:
    if len(edit.cue_ids) != count or not all(edit.cue_ids):
        raise ManualEditError(f"{edit.kind!r} 편집은 큐 ID {count}개를 실어야 한다: {edit.cue_ids}")
    return edit.cue_ids


def _next_manual_id(revision: ManualRevision) -> str:
    """다음 수동 큐 ID `m####` — 부모 상태에서 결정되므로 같은 편집은 같은 ID를 낳는다."""
    highest = 0
    for cue in revision.cues:
        if cue.cue_id.startswith("m") and cue.cue_id[1:].isdigit():
            highest = max(highest, int(cue.cue_id[1:]))
    return f"m{highest + 1:04d}"


def edit_text(revision: ManualRevision, cue_id: str, text: str) -> ManualRevision:
    """큐 글자를 고친다 — 시각은 손도 안 대고 그대로다."""
    return apply_edit(revision, ManualEdit("text", revision.sequence, (cue_id,), text=text))


def edit_time(
    revision: ManualRevision,
    cue_id: str,
    *,
    start: Fraction | int | None = None,
    end: Fraction | int | None = None,
) -> ManualRevision:
    """큐 시각을 사용자가 준 값으로 고친다 — `Fraction` 또는 밀리초 정수."""
    return apply_edit(
        revision,
        ManualEdit(
            "time",
            revision.sequence,
            (cue_id,),
            start=None if start is None else _as_time(start, "start"),
            end=None if end is None else _as_time(end, "end"),
        ),
    )


def split_cue(
    revision: ManualRevision,
    cue_id: str,
    at: Fraction | int,
    left_text: str,
    right_text: str,
) -> ManualRevision:
    """큐를 둘로 나눈다 — 시각과 양쪽 글자를 모두 명시적으로 받는다."""
    return apply_edit(
        revision,
        ManualEdit(
            "split",
            revision.sequence,
            (cue_id,),
            text=left_text,
            right_text=right_text,
            at=_as_time(at, "나눌 시각"),
        ),
    )


def merge_cues(
    revision: ManualRevision, left_id: str, right_id: str, text: str | None = None
) -> ManualRevision:
    """맞닿은 두 큐를 합친다 — `text`가 None이면 근거 글자를 이어붙인다."""
    return apply_edit(
        revision, ManualEdit("merge", revision.sequence, (left_id, right_id), text=text)
    )


def add_cue(
    revision: ManualRevision,
    start: Fraction | int,
    end: Fraction | int,
    text: str,
    *,
    note: str = "",
) -> ManualRevision:
    """수동 근거의 새 큐를 넣는다 — 미대응 말을 살릴 때 쓴다(§11.5)."""
    return apply_edit(
        revision,
        ManualEdit(
            "add",
            revision.sequence,
            (),
            text=text,
            start=_as_time(start, "start"),
            end=_as_time(end, "end"),
            note=note,
        ),
    )


def allow_finding(revision: ManualRevision, key: str, reason: str) -> ManualRevision:
    """열린 경고 하나를 이유와 함께 허용한다."""
    return apply_edit(revision, ManualEdit("allow", revision.sequence, (), key=key, reason=reason))


def resolve_unplaced(
    revision: ManualRevision,
    index: int,
    text: str,
    *,
    placed_cue_id: str | None = None,
    dismissed_reason: str | None = None,
) -> ManualRevision:
    """미대응 문구를 처리한다 — 수동 큐로 살리거나 버린 이유를 남긴다."""
    return apply_edit(
        revision,
        ManualEdit(
            "resolve",
            revision.sequence,
            (),
            text=text,
            index=index,
            placed_cue_id=placed_cue_id,
            reason=dismissed_reason,
        ),
    )


def record_no_speech(revision: ManualRevision, *, note: str = "") -> ManualRevision:
    """무발화를 사람이 확정한다 — 빈 실행에만 존재한다(§11.11)."""
    return apply_edit(revision, ManualEdit("no_speech", revision.sequence, (), note=note))


# ─────────────────────────────────────────────────────────────
#  직렬화 — 정준 JSON, 엄격한 재독기
# ─────────────────────────────────────────────────────────────


def serialize_revision(revision: ManualRevision) -> str:
    """리비전을 정준 JSON으로 — 정렬된 키·UTF-8·결정적 바이트. 보기 상태는 없다."""
    return (
        json.dumps(
            _revision_document(revision),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _revision_document(revision: ManualRevision) -> dict[str, object]:
    return {
        "schema_version": MANUAL_SCHEMA,
        "identity": {
            "final_video_revision": revision.identity.final_video_revision,
            "run_id": revision.identity.run_id,
            "fingerprint": revision.identity.fingerprint,
            "auto_content_sha256": revision.identity.auto_content_sha256,
        },
        "sequence": revision.sequence,
        "parent_revision": revision.parent_revision,
        "parent_sha256": revision.parent_sha256,
        "video_duration": _fraction(revision.video_duration),
        "sample_count": revision.sample_count,
        "cues": [cue_document(cue) for cue in revision.cues],
        "allowances": [{"key": item.key, "reason": item.reason} for item in revision.allowances],
        "unplaced_resolutions": [
            {
                "index": item.index,
                "text": item.text,
                "placed_cue_id": item.placed_cue_id,
                "dismissed_reason": item.dismissed_reason,
            }
            for item in revision.unplaced_resolutions
        ],
        "no_speech": revision.no_speech,
        "no_speech_note": revision.no_speech_note,
        "edit": None if revision.edit is None else _edit_document(revision.edit),
    }


def cue_document(cue: ReviewCue) -> dict[str, object]:
    return {
        "cue_id": cue.cue_id,
        "start": _fraction(cue.start),
        "end": _fraction(cue.end),
        "text": cue.text,
        "text_basis": cue.text_basis,
        "auto_text": cue.auto_text,
        "whisper_text": cue.whisper_text,
        "timing_segment_ids": list(cue.timing_segment_ids),
        "timing_evidence_id": cue.timing_evidence_id,
        "text_evidence_ids": list(cue.text_evidence_ids),
        "correction_status": cue.correction_status,
        "flags": list(cue.flags),
        "manual_note": cue.manual_note,
    }


def _edit_document(edit: ManualEdit) -> dict[str, object]:
    return {
        "kind": edit.kind,
        "target_revision": edit.target_revision,
        "cue_ids": list(edit.cue_ids),
        "text": edit.text,
        "right_text": edit.right_text,
        "start": None if edit.start is None else _fraction(edit.start),
        "end": None if edit.end is None else _fraction(edit.end),
        "at": None if edit.at is None else _fraction(edit.at),
        "key": edit.key,
        "reason": edit.reason,
        "index": edit.index,
        "placed_cue_id": edit.placed_cue_id,
        "note": edit.note,
    }


def _fraction(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def parse_revision(text: str) -> ManualRevision:
    """정준 문서를 리비전으로 되돌린다 — 모르는 스키마·키·타입·범위는 못 들어온다."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ManualParseError(f"JSON이 아니다: {error}") from error
    if not isinstance(document, dict):
        raise ManualParseError("문서 최상위는 객체여야 한다")
    _require_exact_keys(document, set(_revision_document(_blank_revision())))
    if document["schema_version"] != MANUAL_SCHEMA:
        raise ManualParseError(
            f"모르는 스키마 {document['schema_version']!r} — {MANUAL_SCHEMA!r}만 읽는다"
        )
    try:
        return _revision_from_document(document)
    except ManualValidationError as error:
        raise ManualParseError(f"범위 밖 값: {error}") from error


def _revision_from_document(document: dict[str, object]) -> ManualRevision:
    identity_values = _require_dict(document["identity"], "identity")
    _require_exact_keys(
        identity_values,
        {"final_video_revision", "run_id", "fingerprint", "auto_content_sha256"},
    )
    edit_value = document["edit"]
    return _validated(
        AutoIdentity(
            final_video_revision=_require_str(identity_values["final_video_revision"], "identity"),
            run_id=_require_str(identity_values["run_id"], "identity"),
            fingerprint=_require_str(identity_values["fingerprint"], "identity"),
            auto_content_sha256=_require_str(identity_values["auto_content_sha256"], "identity"),
        ),
        _require_int(document["sequence"], "sequence"),
        tuple(_cue_from(item) for item in _require_list(document["cues"], "cues")),
        _fraction_from(document["video_duration"], "video_duration"),
        _require_int(document["sample_count"], "sample_count"),
        allowances=tuple(
            _allowance_from(item) for item in _require_list(document["allowances"], "allowances")
        ),
        resolutions=tuple(
            _resolution_from(item)
            for item in _require_list(document["unplaced_resolutions"], "unplaced_resolutions")
        ),
        no_speech=_require_bool(document["no_speech"], "no_speech"),
        no_speech_note=_require_str(document["no_speech_note"], "no_speech_note"),
        parent_revision=_optional_int(document["parent_revision"], "parent_revision"),
        parent_sha256=_require_str(document["parent_sha256"], "parent_sha256"),
        edit=None if edit_value is None else _edit_from(_require_dict(edit_value, "edit")),
    )


def _blank_revision() -> ManualRevision:
    identity = AutoIdentity("a" * 64, "run", "f" * 64, "0" * 64)
    return ManualRevision(identity, 0, (), Fraction(0), 0)


def _cue_from(item: object) -> ReviewCue:
    values = _require_dict(item, "cue")
    _require_exact_keys(values, set(cue_document(_blank_cue())))
    return ReviewCue(
        cue_id=_require_str(values["cue_id"], "cue.cue_id"),
        start=_fraction_from(values["start"], "cue.start"),
        end=_fraction_from(values["end"], "cue.end"),
        text=_require_str(values["text"], "cue.text"),
        text_basis=_require_str(values["text_basis"], "cue.text_basis"),
        auto_text=_require_str(values["auto_text"], "cue.auto_text"),
        whisper_text=_require_str(values["whisper_text"], "cue.whisper_text"),
        timing_segment_ids=tuple(
            _require_int(seg, "cue.timing_segment_ids[]")
            for seg in _require_list(values["timing_segment_ids"], "cue.timing_segment_ids")
        ),
        timing_evidence_id=_require_str(values["timing_evidence_id"], "cue.timing_evidence_id"),
        text_evidence_ids=tuple(
            _require_str(evidence, "cue.text_evidence_ids[]")
            for evidence in _require_list(values["text_evidence_ids"], "cue.text_evidence_ids")
        ),
        correction_status=_require_str(values["correction_status"], "cue.correction_status"),
        flags=tuple(
            _require_str(flag, "cue.flags[]")
            for flag in _require_list(values["flags"], "cue.flags")
        ),
        manual_note=_require_str(values["manual_note"], "cue.manual_note"),
    )


def _blank_cue() -> ReviewCue:
    return ReviewCue(
        cue_id="",
        start=Fraction(0),
        end=Fraction(0),
        text="",
        text_basis=TEXT_BASIS_AUTO,
        auto_text="",
        whisper_text="",
        timing_segment_ids=(),
        timing_evidence_id="",
        text_evidence_ids=(),
        correction_status="",
        flags=(),
    )


def _allowance_from(item: object) -> Allowance:
    values = _require_dict(item, "allowance")
    _require_exact_keys(values, {"key", "reason"})
    return Allowance(
        key=_require_str(values["key"], "allowance.key"),
        reason=_require_str(values["reason"], "allowance.reason"),
    )


def _resolution_from(item: object) -> UnplacedResolution:
    values = _require_dict(item, "unplaced_resolution")
    _require_exact_keys(values, {"index", "text", "placed_cue_id", "dismissed_reason"})
    placed = values["placed_cue_id"]
    dismissed = values["dismissed_reason"]
    return UnplacedResolution(
        index=_require_int(values["index"], "unplaced_resolution.index"),
        text=_require_str(values["text"], "unplaced_resolution.text"),
        placed_cue_id=None if placed is None else _require_str(placed, "placed_cue_id"),
        dismissed_reason=None if dismissed is None else _require_str(dismissed, "dismissed_reason"),
    )


def _edit_from(item: object) -> ManualEdit:
    values = _require_dict(item, "edit")
    _require_exact_keys(values, set(_edit_document(ManualEdit("", -1))))
    start, end, at = values["start"], values["end"], values["at"]
    return ManualEdit(
        kind=_require_str(values["kind"], "edit.kind"),
        target_revision=_require_int(values["target_revision"], "edit.target_revision"),
        cue_ids=tuple(
            _require_str(cue_id, "edit.cue_ids[]")
            for cue_id in _require_list(values["cue_ids"], "edit.cue_ids")
        ),
        text=_optional_str(values["text"], "edit.text"),
        right_text=_optional_str(values["right_text"], "edit.right_text"),
        start=None if start is None else _fraction_from(start, "edit.start"),
        end=None if end is None else _fraction_from(end, "edit.end"),
        at=None if at is None else _fraction_from(at, "edit.at"),
        key=_optional_str(values["key"], "edit.key"),
        reason=_optional_str(values["reason"], "edit.reason"),
        index=_optional_int(values["index"], "edit.index"),
        placed_cue_id=_optional_str(values["placed_cue_id"], "edit.placed_cue_id"),
        note=_optional_str(values["note"], "edit.note"),
    )


def _fraction_from(item: object, where: str) -> Fraction:
    values = _require_dict(item, where)
    _require_exact_keys(values, {"num", "den"})
    den = _require_int(values["den"], f"{where}.den")
    if den <= 0:
        raise ManualParseError(f"{where}.den은(는) 양수여야 한다")
    return Fraction(_require_int(values["num"], f"{where}.num"), den)


def _optional_int(item: object, where: str) -> int | None:
    return None if item is None else _require_int(item, where)


def _optional_str(item: object, where: str) -> str | None:
    return None if item is None else _require_str(item, where)


def _require_exact_keys(values: dict[str, object], expected: set[str]) -> None:
    unknown = sorted(set(values) - expected)
    missing = sorted(expected - set(values))
    if unknown or missing:
        raise ManualParseError(f"모르는 키 {unknown} / 없는 키 {missing} — 문서가 계약 밖이다")


def _require_dict(item: object, where: str) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ManualParseError(f"{where}은(는) 객체여야 한다")
    return item


def _require_list(item: object, where: str) -> list[object]:
    if not isinstance(item, list):
        raise ManualParseError(f"{where}은(는) 배열이어야 한다")
    return item


def _require_str(item: object, where: str) -> str:
    if not isinstance(item, str):
        raise ManualParseError(f"{where}은(는) 문자열이어야 한다")
    return item


def _require_int(item: object, where: str) -> int:
    if isinstance(item, bool) or not isinstance(item, int):
        raise ManualParseError(f"{where}은(는) 정수여야 한다")
    return item


def _require_bool(item: object, where: str) -> bool:
    if not isinstance(item, bool):
        raise ManualParseError(f"{where}은(는) 참거짓이어야 한다")
    return item


def content_fingerprint(revision: ManualRevision) -> str:
    """리비전 내용의 지문 — 정준 직렬화의 SHA-256. 보기 상태는 애초에 없다(§11.7)."""
    return sha256_text(serialize_revision(revision))
