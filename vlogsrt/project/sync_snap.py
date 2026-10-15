"""시각 맞춤 제안(D59 19g) — 원본 자막 큐의 경계를 말소리 경계에 맞춘 **제안**을 계산한다.

순수 계산이다.

입력은 값이다: PCM 시계(표본 0 = 0초)의 큐(`sync_check.Cue`), 자막 시각 확인(D55)이 잰 말소리
칸(`SpeechTrack`), 그 측정의 전체 어긋남(`ShiftEstimate`), 그리고 무엇을 재었는지(`SnapSource`).
이 모듈은 파일·DB·프로세스·전사·모델을 모른다 — 재료를 모으는 일과 적용·되돌리기는 서비스의
몫이다(19h).

순서(계획 19g):

1. 확인이 **센** 전체 어긋남(`counted` — `+`면 자막이 말보다 이르다)만큼 큐를 먼저 늦춘다. 밀면
   자막 범위 밖으로 나가는 큐, 길이 0·뒤집힌 큐, 그리고 그런 큐와 부딪히는 큐는 밀지 않는다 —
   순서가 바뀌지 않게.
2. 경계마다 찾는 폭(`reach_seconds`) 안의 말소리 경계에 맞춘다 — 시작은 말소리 머리, 끝은 꼬리.
   경계가 말소리 **한가운데**에 있으면 그 말소리의 바깥 경계(시작은 그 머리, 끝은 그 꼬리)에만
   맞춘다 — 큐가 덮던 말소리를 건너 안쪽으로 줄이지 않는다.
3. 맞춘 경계에만 lead_in·lead_out을 더한다(refine과 같은 값 — 실제로 있는 틈만 먹고, 함께 쓰는
   틈은 lead_in이 먼저다).

선(규칙 1·3·4·5·9):

- **제안일 뿐이다.** 아무것도 쓰지 않는다. 시각이 바뀌는 것은 사람이 19h에서 적용한 리비전뿐이다
  (D59 — 규칙 1·3의 좁은 예외, D55 「표시만」의 예외 「시각 맞춤 제안」).
- **적용 규칙(19h)**: 계획의 **모든** 제안의 새 시각을 판정과 무관하게 함께 적용하거나, 하나도
  적용하지 않는다. 막힘·못 찾음 큐도 센 어긋남만큼 옮겨질 수 있다(`SnapProposal.moves`) — 「옮긴
  큐만」 적용하면 민 이웃과 겹치거나 순서가 뒤집힌다.
- 큐를 빼거나 더하거나 순서를 바꾸거나 합치거나 나누지 않는다. 아이디·글자는 그대로 — 제안은
  큐마다 정확히 하나, 입력 순서다.
- 찾는 폭 안에 말소리가 없으면 「못 찾음」 **표시만** 한다(측정 한계일 수 있음, D55) — 「말이
  아니다」라고 하지 않고 지우지 않는다(규칙 4·9).
- 맞춘 결과가 `min_duration`보다 짧거나 뒤집히면 `blocked` — 그 전 시각(밀었으면 민 시각)이다.
  길이 0·뒤집힌 큐도 `blocked`이고 그 시각 그대로, 글자는 남는다.
- 맞춘 경계는 앞의 **모든** 큐의 그 전 꼬리 + `min_gap`보다 앞으로, 뒤의 모든 큐의 그 전 머리
  (끝은 − `min_gap`)보다 뒤로 가지 않는다 — **목록 순서로** 입력에 없던 겹침이나 시작 순서
  뒤집힘을 만들지 않는다(겹친 입력에서도). 시각 순이 아닌 목록에서는 시간상의 새 겹침까지 막지
  못하지만, 원본 자막은 언제나 시각 순이다(`source_transcript._validate_rows`).
  두 큐가 같은 말소리를 서로 가져가려 하면 둘 다 맞추지 않는다.
  맞추지 않은 경계는 그 전 자리 그대로다 — whisper가 붙여 낸 경계를 벌리는 일은 정제(refine)의
  몫이다.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from fractions import Fraction

from vlogsrt.project import sync_check
from vlogsrt.project.config import SyncSnapSettings, sync_snap_problems

STATUS_MOVED = "moved"
STATUS_UNCHANGED = "unchanged"
STATUS_BLOCKED = "blocked"
STATUS_NO_SPEECH = "no_speech"
STATUSES = (STATUS_MOVED, STATUS_UNCHANGED, STATUS_BLOCKED, STATUS_NO_SPEECH)

PLAN_SCHEMA = "vlogsrt-sync-snap/v1"

_HELD_RANGE = "range"
_HELD_NEIGHBOUR = "neighbour"
_HELD_DEGENERATE = "degenerate"
_KIND_DEGENERATE = "degenerate"
_KIND_SHORT = "short"
# 경계 하나를 맞추지 않은 까닭 — 화면이 「경계가 없음」과 「이웃 너머」를 갈라 말한다(19g 검증 뒤).
_WHY_FAR = "far"
_WHY_WALL = "wall"
_WHY_INSIDE_FAR = "inside_far"
_WHY_INSIDE_WALL = "inside_wall"
_WHY_CONTESTED = "contested"

# 화면(19h)에 그대로 나가는 말 — 존댓말이고, 「말이 아니다」라고 하지 않는다(규칙 4, D55).
NO_SPEECH_REASON = "앞뒤 {reach} 안에서 말소리를 못 찾아(측정 한계일 수 있음) 맞추지 않았습니다."
DEGENERATE_REASON = "시각이 길이 0이거나 거꾸로라 맞추지 않았습니다 — 글자는 그대로 남깁니다."
SHORT_REASON = "말소리 경계에 맞추면 {length}로 최소 {minimum}보다 짧아져 맞추지 않았습니다."
CROSSED_REASON = "말소리 경계에 맞추면 시작이 끝보다 늦어져 맞추지 않았습니다."
MOVED_REASON = "말소리 {sides}에 맞췄습니다 — {moves}."
SHIFT_ONLY_REASON = "말소리 경계에는 맞추지 않고 전체 어긋남만 옮겼습니다 — {moves}."
ALREADY_REASON = "이미 말소리 경계에 맞아 그대로 둡니다."
KEPT_REASON = "말소리 경계에 맞추지 않아 그대로 둡니다."
SIDE_NOTES = {
    _WHY_FAR: "{subject}은 앞뒤 {reach} 안에 맞출 말소리 경계가 없습니다.",
    _WHY_WALL: (
        "{subject}에 가까운 말소리 경계는 이웃 자막이나 자막 범위 너머에 있어 맞추지 않았습니다."
    ),
    _WHY_INSIDE_FAR: (
        "{subject}은 말소리 한가운데라 그 말소리의 바깥 경계에만 맞추는데, 그 경계가 앞뒤"
        " {reach} 밖입니다."
    ),
    _WHY_INSIDE_WALL: (
        "{subject}은 말소리 한가운데라 그 말소리의 바깥 경계에만 맞추는데, 그 경계가 이웃 자막이나"
        " 자막 범위 너머입니다."
    ),
    _WHY_CONTESTED: "{subject}은 이웃 자막과 같은 말소리를 두고 겹쳐 맞추지 않았습니다.",
}
SHIFT_FIRST_NOTE = "전체 어긋남 {shift}를 먼저 옮겼습니다."
SHIFT_ONLY_NOTE = "전체 어긋남 {shift}만 옮겼습니다."
HELD_NOTES = {
    _HELD_RANGE: "전체 어긋남 {shift}를 옮기면 원본 길이 밖으로 나가 이 자막은 옮기지 않았습니다.",
    _HELD_NEIGHBOUR: (
        "전체 어긋남 {shift}를 옮기면 옮기지 않은 자막과 겹쳐 이 자막은 옮기지 않았습니다."
    ),
    _HELD_DEGENERATE: "길이 0이거나 거꾸로인 자막이라 전체 어긋남 {shift}도 옮기지 않았습니다.",
}
KEPT_NOTE = "원래 시각을 그대로 둡니다."

Bounds = tuple[Fraction, Fraction]
Span = tuple[Fraction, Fraction]
Walls = tuple[Fraction, Fraction, Fraction]
"""(머리의 바닥, 머리의 천장, 꼬리의 천장) — 맞춘 경계가 갈 수 있는 끝."""


@dataclass(frozen=True, slots=True)
class SnapSource:
    """무엇을 재어 제안했는가 — 제안 sha의 입력이다(19h의 적용이 sha를 대조한다)."""

    source_id: str
    transcript_revision: str
    pcm_sha256: str
    vad_key: str
    audio_start: Fraction
    """PCM 표본 0의 공통 시계 위치 — 원본 자막 시각 = PCM 시각 + 이 값(D55 `audio_start`)."""
    transcript_duration: Fraction
    """원본 자막의 길이(공통 시계) — 제안은 이 범위와 PCM 범위가 겹친 곳 안에 머문다."""


@dataclass(frozen=True, slots=True)
class SnapProposal:
    """큐 하나의 제안 — 시각은 PCM 시계의 정확 유리수다(공통 시계는 `SnapPlan.on_common`).

    `status`는 **경계 맞춤**의 판정이다. 적용하면 시각이 바뀌는지는 `moves`가 말한다 — 막힘·못 찾음
    큐도 센 어긋남만큼은 옮겨진다(`shift_only`). 화면은 그 둘을 「그대로」로 보이면 안 된다.
    `start_anchor`·`end_anchor`는 맞춘 말소리 경계(lead를 더하기 전)다 — 맞추지 않은 쪽은 None.
    """

    cue_id: str
    text: str
    old_start: Fraction
    old_end: Fraction
    new_start: Fraction
    new_end: Fraction
    status: str
    reason: str
    shifted: bool
    start_anchor: Fraction | None = None
    end_anchor: Fraction | None = None

    @property
    def start_move(self) -> Fraction:
        return self.new_start - self.old_start

    @property
    def end_move(self) -> Fraction:
        return self.new_end - self.old_end

    @property
    def moves(self) -> bool:
        """적용하면 이 큐의 시각이 바뀌는가 — 판정(`status`)과 무관하다."""
        return (self.new_start, self.new_end) != (self.old_start, self.old_end)

    @property
    def shift_only(self) -> bool:
        """말소리 경계에는 맞추지 않고 센 전체 어긋남만큼만 옮긴다."""
        return self.shifted and self.start_anchor is None and self.end_anchor is None


@dataclass(frozen=True, slots=True)
class SnapPlan:
    """원본 하나의 제안 전부 — `sha256`은 입력·기준·출력의 정규 직렬화(`plan_document`)다.

    **적용 규칙**: 19h는 `proposals`의 새 시각을 **판정과 무관하게 전부** 적용하거나 하나도
    적용하지 않는다. 일부만 적용하면 민 큐와 안 민 큐가 겹치거나 순서가 뒤집힐 수 있다.
    """

    source: SnapSource
    settings: SyncSnapSettings
    speech_sha256: str
    offset_status: str
    offset_seconds: Fraction | None
    shift: Fraction
    """센 전체 어긋남(세지 않았으면 0) — 실제로 옮긴 큐는 `SnapProposal.shifted`가 말한다."""
    proposals: tuple[SnapProposal, ...]
    sha256: str = ""

    def on_common(self, value: Fraction) -> Fraction:
        """PCM 시계 → 원본 자막의 공통 시계(D55와 같은 산술)."""
        return value + self.source.audio_start

    def count(self, status: str) -> int:
        return sum(1 for proposal in self.proposals if proposal.status == status)


@dataclass(frozen=True, slots=True)
class _Base:
    """맞추기 전 시각 — 전체 어긋남을 옮겼거나(`shifted`) 못 옮긴 까닭(`held`)과 함께."""

    start: Fraction
    end: Fraction
    held: str
    shifted: bool

    @property
    def head(self) -> Fraction:
        return min(self.start, self.end)

    @property
    def tail(self) -> Fraction:
        return max(self.start, self.end)


@dataclass(frozen=True, slots=True)
class _Snap:
    """경계마다 맞춘 말소리 경계 — 맞추지 않은 경계의 까닭(`*_why`)·큐를 못 맞춘 까닭(`kind`)."""

    start: Fraction | None = None
    end: Fraction | None = None
    kind: str = ""
    start_why: str = ""
    end_why: str = ""
    attempted: Fraction | None = None

    @property
    def anchored(self) -> bool:
        return self.start is not None or self.end is not None


@dataclass(frozen=True, slots=True)
class _Edges:
    """말소리 구간의 머리·꼬리(초) — 꼬리는 PCM 길이에서 자른다(마지막 칸은 길이를 넘을 수 있다)."""

    onsets: tuple[Fraction, ...]
    offsets: tuple[Fraction, ...]

    @classmethod
    def of(cls, speech: sync_check.SpeechTrack) -> _Edges:
        runs = sync_check.merge_runs(speech.runs)
        return cls(
            tuple(start * speech.hop for start, _ in runs),
            tuple(min(end * speech.hop, speech.duration) for _, end in runs),
        )

    def speech_between(self, low: Fraction, high: Fraction) -> bool:
        """`(low, high)`에 말소리가 한 칸이라도 걸치는가 — 구간은 정렬·서로 떨어져 있다."""
        before = bisect_left(self.onsets, high)
        return before > 0 and self.offsets[before - 1] > low

    def around(self, moment: Fraction) -> int | None:
        """`moment`가 머리와 꼬리 **사이**(양끝 제외)에 든 말소리 구간의 번호 — 없으면 None."""
        index = bisect_right(self.onsets, moment) - 1
        if index >= 0 and self.onsets[index] < moment < self.offsets[index]:
            return index
        return None


def propose(
    cues: Sequence[sync_check.Cue],
    speech: sync_check.SpeechTrack,
    offset: sync_check.ShiftEstimate,
    *,
    source: SnapSource,
    settings: SyncSnapSettings,
) -> SnapPlan:
    """큐마다 제안 하나 — 전체 어긋남(센 것만) → 말소리 경계 맞춤 → lead_in·lead_out.

    적용은 전부이거나 전무다(`SnapPlan` 적용 규칙) — 판정과 무관하게 모든 새 시각을 함께 쓴다.
    """
    _require(settings)
    bounds = _bounds(speech, source)
    shift = offset.seconds if offset.counted and offset.seconds is not None else Fraction(0)
    bases = _shifted_bases(cues, shift, bounds, settings.min_gap)
    edges = _Edges.of(speech)
    walls = _walls(bases, bounds, settings.min_gap)
    snaps = [
        _snap(base, wall, edges, bounds, settings) for base, wall in zip(bases, walls, strict=True)
    ]
    finals, settled = _finish(bases, _settle_contests(snaps, settings.min_gap), bounds, settings)
    proposals = tuple(
        _proposal(cue, base, snapped, final, shift, settings)
        for cue, base, snapped, final in zip(cues, bases, settled, finals, strict=True)
    )
    plan = SnapPlan(
        source=source,
        settings=settings,
        speech_sha256=speech_sha256(speech),
        offset_status=offset.status,
        offset_seconds=offset.seconds,
        shift=shift,
        proposals=proposals,
    )
    return replace(plan, sha256=_digest(plan_document(plan)))


def _require(settings: SyncSnapSettings) -> None:
    problems = sync_snap_problems(settings)
    if problems:
        raise ValueError("시각 맞춤 기준이 계약을 어긴다: " + "; ".join(problems))


def _bounds(speech: sync_check.SpeechTrack, source: SnapSource) -> Bounds:
    """맞춘 경계가 머무는 PCM 시계 범위 — PCM `[0, 길이]`와 원본 자막 `[0, 길이]`가 겹친 곳."""
    low = max(Fraction(0), -source.audio_start)
    high = min(speech.duration, source.transcript_duration - source.audio_start)
    if high < low:
        raise ValueError(f"자막 범위와 PCM 범위가 겹치지 않는다: {low}–{high}")
    return low, high


def _prefix_max(values: Sequence[Fraction | None]) -> list[Fraction | None]:
    """칸마다 **앞의** 값들(None 빼고)의 최댓값 — 없으면 None."""
    found: list[Fraction | None] = []
    best: Fraction | None = None
    for value in values:
        found.append(best)
        if value is not None:
            best = value if best is None else max(best, value)
    return found


def _suffix_min(values: Sequence[Fraction | None]) -> list[Fraction | None]:
    """칸마다 **뒤의** 값들(None 빼고)의 최솟값 — 없으면 None."""
    found: list[Fraction | None] = []
    best: Fraction | None = None
    for value in reversed(values):
        found.append(best)
        if value is not None:
            best = value if best is None else min(best, value)
    return found[::-1]


# ─────────────────────────────────────────────────────────────
#  1. 전체 어긋남 — 센 것만, 밀어도 순서·범위가 지켜지는 큐만
# ─────────────────────────────────────────────────────────────


def _shifted_bases(
    cues: Sequence[sync_check.Cue], shift: Fraction, bounds: Bounds, gap: Fraction
) -> list[_Base]:
    """큐마다 맞추기 전 시각 — 밀면 범위 밖인 큐·길이 0이나 뒤집힌 큐는 밀지 않고, 민 큐가 안 민
    큐(앞뒤 어디든)와 부딪히면 민 쪽도 놓는다.

    놓은 큐는 언제나 원래 자리라 순서가 원래와 같다. 길이 0 큐를 밀지 않는 까닭: 민 길이 0 줄은
    「바꾼 줄」이 되어 19h 편집 검사(끝 > 시작)가 계획 전체를 거절한다(19g 검증 뒤 고침). 부딪힘이
    없어질 때까지 되풀이한다 — 놓기만 하므로 끝난다.
    """
    if shift == 0:
        return [_Base(cue.start, cue.end, "", False) for cue in cues]
    held = [_held_reason(cue, shift, bounds) for cue in cues]
    while culprits := _collisions(cues, held, shift, gap):
        for index in culprits:
            held[index] = _HELD_NEIGHBOUR
    return [
        _Base(cue.start, cue.end, why, False)
        if why
        else _Base(cue.start + shift, cue.end + shift, "", True)
        for cue, why in zip(cues, held, strict=True)
    ]


def _held_reason(cue: sync_check.Cue, shift: Fraction, bounds: Bounds) -> str:
    if cue.end <= cue.start:
        return _HELD_DEGENERATE
    low, high = bounds
    return "" if low <= cue.start + shift and cue.end + shift <= high else _HELD_RANGE


def _collisions(
    cues: Sequence[sync_check.Cue], held: Sequence[str], shift: Fraction, gap: Fraction
) -> list[int]:
    """민 큐 중 안 민 큐와 `min_gap`보다 가까워지는 것 — 밀어서 다가가는 쪽만 본다.

    `+`로 밀면 뒤의 안 민 큐의 머리, `−`로 밀면 앞의 안 민 큐의 꼬리가 벽이다(인접한 큐만이 아니라
    전부 — 정렬되지 않은 입력에서도 순서가 바뀌지 않게).
    """
    pairs = list(zip(cues, held, strict=True))
    heads = [min(cue.start, cue.end) if why else None for cue, why in pairs]
    tails = [max(cue.start, cue.end) if why else None for cue, why in pairs]
    if shift > 0:
        walls = _suffix_min(heads)
        return [
            index
            for index, (cue, why, wall) in enumerate(zip(cues, held, walls, strict=True))
            if not why and wall is not None and max(cue.start, cue.end) + shift > wall - gap
        ]
    walls = _prefix_max(tails)
    return [
        index
        for index, (cue, why, wall) in enumerate(zip(cues, held, walls, strict=True))
        if not why and wall is not None and min(cue.start, cue.end) + shift < wall + gap
    ]


# ─────────────────────────────────────────────────────────────
#  2. 경계 맞춤 — 찾는 폭 안의 말소리 경계, 다른 큐의 그 전 경계를 넘지 않는다
# ─────────────────────────────────────────────────────────────


def _walls(bases: Sequence[_Base], bounds: Bounds, gap: Fraction) -> list[Walls]:
    """큐마다 (머리의 바닥, 머리의 천장, 꼬리의 천장) — 앞의 모든 큐의 꼬리 + 간격, 뒤의 모든 큐의
    머리(꼬리는 − 간격). 정렬·겹침 없는 입력에서는 바로 앞뒤 큐의 경계와 같다."""
    low, high = bounds
    before = _prefix_max([base.tail for base in bases])
    after = _suffix_min([base.head for base in bases])
    return [
        (
            low if tail is None else max(low, tail + gap),
            high if head is None else min(high, head),
            high if head is None else min(high, head - gap),
        )
        for tail, head in zip(before, after, strict=True)
    ]


def _snap(
    base: _Base, walls: Walls, edges: _Edges, bounds: Bounds, settings: SyncSnapSettings
) -> _Snap:
    if base.end <= base.start:
        return _Snap(kind=_KIND_DEGENERATE)
    reach = settings.reach_seconds
    if not edges.speech_between(base.start - reach, base.end + reach):
        return _Snap(kind=STATUS_NO_SPEECH)
    floor, start_cap, ceiling = walls
    start, start_why = _choose(
        _start_candidates(edges, base.start, reach), base.start, reach, (floor, start_cap), False
    )
    end, end_why = _choose(
        _end_candidates(edges, base.end, reach), base.end, reach, (bounds[0], ceiling), True
    )
    return _Snap(start=start, end=end, start_why=start_why, end_why=end_why)


def _start_candidates(
    edges: _Edges, moment: Fraction, reach: Fraction
) -> tuple[tuple[Fraction, ...], bool]:
    """머리를 맞출 후보와, 머리가 말소리 한가운데인지.

    한가운데면 그 말소리의 머리 하나(바깥쪽)뿐이다. 아니면 앞쪽(바깥)의 머리 전부와, 머리와 그
    사이가 조용할 때만 뒤쪽(안쪽)의 첫 머리 하나 — 안쪽으로는 큐가 덮던 말소리를 하나도 건너지
    않는다(19g 검증 뒤 고침).
    """
    inside = edges.around(moment)
    if inside is not None:
        return (edges.onsets[inside],), True
    onsets = edges.onsets
    following = bisect_right(onsets, moment)
    outward = onsets[bisect_left(onsets, moment - reach) : following]
    quiet = following == 0 or edges.offsets[following - 1] <= moment
    return (*outward, *(onsets[following : following + 1] if quiet else ())), False


def _end_candidates(
    edges: _Edges, moment: Fraction, reach: Fraction
) -> tuple[tuple[Fraction, ...], bool]:
    """꼬리를 맞출 후보 — `_start_candidates`의 거울(바깥 = 뒤쪽, 안쪽 = 앞쪽의 마지막 꼬리)."""
    inside = edges.around(moment)
    if inside is not None:
        return (edges.offsets[inside],), True
    offsets = edges.offsets
    preceding = bisect_left(offsets, moment)
    outward = offsets[preceding : bisect_right(offsets, moment + reach)]
    quiet = preceding == len(offsets) or edges.onsets[preceding] >= moment
    return (*(offsets[max(0, preceding - 1) : preceding] if quiet else ()), *outward), False


def _choose(
    found: tuple[tuple[Fraction, ...], bool],
    target: Fraction,
    reach: Fraction,
    window: Bounds,
    later: bool,
) -> tuple[Fraction | None, str]:
    """맞출 경계(찾는 폭 안에서 가장 가까운 것, 같으면 큐가 길어지는 쪽)와, 못 맞추면 그 까닭 —
    폭 안에 후보가 없으면 「없음」, 있지만 `window`(다른 큐·자막 범위) 밖이면 「너머」다."""
    candidates, inside = found
    far, walled = (_WHY_INSIDE_FAR, _WHY_INSIDE_WALL) if inside else (_WHY_FAR, _WHY_WALL)
    near = [value for value in candidates if abs(value - target) <= reach]
    if not near:
        return None, far
    allowed = [value for value in near if window[0] <= value <= window[1]]
    if not allowed:
        return None, walled
    return min(allowed, key=lambda value: (abs(value - target), -value if later else value)), ""


def _settle_contests(snaps: Sequence[_Snap], gap: Fraction) -> list[_Snap]:
    """앞 큐의 끝과 뒤 큐의 시작이 같은 틈으로 들어와 `min_gap` 안이면 둘 다 놓는다(누구의 말인지
    모른다).

    벽(`_walls`) 덕에 이렇게 되는 것은 둘이 서로를 향해 움직였을 때뿐이다. 맞춘 그대로의 값으로
    쌍을 모두 찾은 뒤 한꺼번에 놓으므로 순서와 무관하다.
    """
    pairs = _contested_pairs(snaps, gap)
    lost_ends = {left for left, _ in pairs}
    lost_starts = {right for _, right in pairs}
    settled: list[_Snap] = []
    for index, snapped in enumerate(snaps):
        if index in lost_ends:
            snapped = replace(snapped, end=None, end_why=_WHY_CONTESTED)
        if index in lost_starts:
            snapped = replace(snapped, start=None, start_why=_WHY_CONTESTED)
        settled.append(snapped)
    return settled


def _contested_pairs(snaps: Sequence[_Snap], gap: Fraction) -> list[tuple[int, int]]:
    """(앞 큐, 뒤 큐) — 앞 큐의 맞춘 끝이 뒤 큐의 맞춘 시작과 `min_gap` 안인 쌍 전부."""
    pairs: list[tuple[int, int]] = []
    latest = _prefix_max([snapped.end for snapped in snaps])
    for right, snapped in enumerate(snaps):
        start, reach_end = snapped.start, latest[right]
        if start is None or reach_end is None or start - reach_end >= gap:
            continue
        pairs += [
            (left, right)
            for left in range(right)
            if (end := snaps[left].end) is not None and start - end < gap
        ]
    return pairs


# ─────────────────────────────────────────────────────────────
#  3. lead_in·lead_out과 최소 길이
# ─────────────────────────────────────────────────────────────


def _finish(
    bases: Sequence[_Base], snaps: Sequence[_Snap], bounds: Bounds, settings: SyncSnapSettings
) -> tuple[list[Span], list[_Snap]]:
    """lead를 더하고, `min_duration`보다 짧은 맞춤은 놓는다 — 놓을 것이 없을 때까지.

    맞춘 머리가 맞춘 꼬리보다 늦은(뒤집힌) 큐는 lead 전에 먼저 놓는다 — 그런 큐가 이웃의 lead
    자리를 잠깐 빼앗아 이웃까지 막지 않게. 놓으면 그 큐는 그 전 시각으로 돌아가 이웃의 lead 자리가
    바뀌므로 다시 잰다. 놓기만 하므로 끝난다.
    """
    current = [_uncrossed(base, snapped) for base, snapped in zip(bases, snaps, strict=True)]
    while True:
        finals = _with_leads(bases, current, bounds, settings)
        short = [
            index
            for index, (snapped, (start, end)) in enumerate(zip(current, finals, strict=True))
            if snapped.anchored and end - start < settings.min_duration
        ]
        if not short:
            return finals, current
        for index in short:
            start, end = finals[index]
            current[index] = _Snap(kind=_KIND_SHORT, attempted=end - start)


def _uncrossed(base: _Base, snapped: _Snap) -> _Snap:
    if not snapped.anchored:
        return snapped
    start, end = _pre(base, snapped)
    return snapped if start < end else _Snap(kind=_KIND_SHORT, attempted=end - start)


def _pre(base: _Base, snapped: _Snap) -> Span:
    start = base.start if snapped.start is None else snapped.start
    return start, base.end if snapped.end is None else snapped.end


def _with_leads(
    bases: Sequence[_Base], snaps: Sequence[_Snap], bounds: Bounds, settings: SyncSnapSettings
) -> list[Span]:
    """맞춘 머리 앞 lead_in(앞의 모든 큐의 lead 전 꼬리 + 간격까지), 맞춘 꼬리 뒤 lead_out(뒤의 모든
    큐의 lead_in 뒤 머리 − 간격까지, 남은 만큼만). 맞추지 않은 경계는 그대로다."""
    low, high = bounds
    gap = settings.min_gap
    pre = [_pre(base, snapped) for base, snapped in zip(bases, snaps, strict=True)]
    floors = _prefix_max([max(span) for span in pre])
    starts = [
        span[0]
        if snapped.start is None
        else snapped.start - _room(settings.lead_in, snapped.start - _floor(floor, low, gap))
        for span, snapped, floor in zip(pre, snaps, floors, strict=True)
    ]
    ceilings = _suffix_min([min(start, span[1]) for start, span in zip(starts, pre, strict=True)])
    return [
        (start, span[1])
        if snapped.end is None
        else (
            start,
            snapped.end + _room(settings.lead_out, _ceiling(ceiling, high, gap) - snapped.end),
        )
        for start, span, snapped, ceiling in zip(starts, pre, snaps, ceilings, strict=True)
    ]


def _room(lead: Fraction, space: Fraction) -> Fraction:
    return min(lead, max(Fraction(0), space))


def _floor(tail: Fraction | None, low: Fraction, gap: Fraction) -> Fraction:
    return low if tail is None else max(low, tail + gap)


def _ceiling(head: Fraction | None, high: Fraction, gap: Fraction) -> Fraction:
    return high if head is None else min(high, head - gap)


# ─────────────────────────────────────────────────────────────
#  제안 — 판정과 존댓말 이유
# ─────────────────────────────────────────────────────────────


def _proposal(
    cue: sync_check.Cue,
    base: _Base,
    snapped: _Snap,
    final: Span,
    shift: Fraction,
    settings: SyncSnapSettings,
) -> SnapProposal:
    status = _status(cue, snapped, final)
    reach = _plain_seconds(settings.reach_seconds)
    sentences = [_main_sentence(status, cue, snapped, final, settings)]
    sentences += _shift_sentences(status, base, snapped, shift)
    if status in (STATUS_MOVED, STATUS_UNCHANGED):
        sentences += _side_notes(snapped, reach)
    return SnapProposal(
        cue_id=cue.cue_id,
        text=cue.text,
        old_start=cue.start,
        old_end=cue.end,
        new_start=final[0],
        new_end=final[1],
        status=status,
        reason=" ".join(sentences),
        shifted=base.shifted,
        start_anchor=snapped.start,
        end_anchor=snapped.end,
    )


def _status(cue: sync_check.Cue, snapped: _Snap, final: Span) -> str:
    if snapped.kind == STATUS_NO_SPEECH:
        return STATUS_NO_SPEECH
    if snapped.kind in (_KIND_DEGENERATE, _KIND_SHORT):
        return STATUS_BLOCKED
    return STATUS_UNCHANGED if final == (cue.start, cue.end) else STATUS_MOVED


def _main_sentence(
    status: str, cue: sync_check.Cue, snapped: _Snap, final: Span, settings: SyncSnapSettings
) -> str:
    if status == STATUS_NO_SPEECH:
        return NO_SPEECH_REASON.format(reach=_plain_seconds(settings.reach_seconds))
    if snapped.kind == _KIND_DEGENERATE:
        return DEGENERATE_REASON
    if snapped.kind == _KIND_SHORT:
        length = snapped.attempted if snapped.attempted is not None else Fraction(0)
        if length <= 0:
            return CROSSED_REASON
        minimum = _plain_seconds(settings.min_duration)
        return SHORT_REASON.format(length=_seconds(length), minimum=minimum)
    sides = [
        word
        for word, anchor in (("시작", snapped.start), ("끝", snapped.end))
        if anchor is not None
    ]
    if status == STATUS_MOVED:
        moves = _moves(cue, final)
        if not sides:
            return SHIFT_ONLY_REASON.format(moves=moves)
        return MOVED_REASON.format(sides="과 ".join(sides), moves=moves)
    return ALREADY_REASON if snapped.anchored else KEPT_REASON


def _side_notes(snapped: _Snap, reach: str) -> list[str]:
    """맞추지 않은 경계마다 그 까닭 — 두 경계의 까닭이 같으면 한 문장으로."""
    unsnapped = [
        (word, why)
        for word, anchor, why in (
            ("시작", snapped.start, snapped.start_why),
            ("끝", snapped.end, snapped.end_why),
        )
        if anchor is None and why
    ]
    if len(unsnapped) == 2 and unsnapped[0][1] == unsnapped[1][1]:
        unsnapped = [("시작과 끝", unsnapped[0][1])]
    return [SIDE_NOTES[why].format(subject=word, reach=reach) for word, why in unsnapped]


def _shift_sentences(status: str, base: _Base, snapped: _Snap, shift: Fraction) -> list[str]:
    """전체 어긋남에 대한 말 — 옮겼는지, 못 옮긴 까닭, 맞추지 못한 큐의 시각이 어디인지."""
    kept = status in (STATUS_BLOCKED, STATUS_NO_SPEECH)
    if shift == 0:
        return [KEPT_NOTE] if kept else []
    amount = _signed(shift)
    if base.held:
        return [HELD_NOTES[base.held].format(shift=amount), *([KEPT_NOTE] if kept else [])]
    if kept:
        return [SHIFT_ONLY_NOTE.format(shift=amount)]
    return [SHIFT_FIRST_NOTE.format(shift=amount)] if snapped.anchored else []


def _moves(cue: sync_check.Cue, final: Span) -> str:
    return " · ".join(
        f"{word} {_signed(new - old)}" if new != old else f"{word} 그대로"
        for word, old, new in (("시작", cue.start, final[0]), ("끝", cue.end, final[1]))
    )


def _signed(value: Fraction) -> str:
    """움직임 — 원본 자막 편집 칸과 같은 소수 셋째 자리. 0이 아닌데 0.000으로 보일 움직임은
    「0.001초 미만 늦춤·앞당김」이라고 쓴다 — 「+0.000초」는 움직이지 않은 것처럼 읽히고,
    「-0.001초 미만」은 「−0.001초보다 작다」(반대 방향)로 읽힌다."""
    text = f"{float(value):+.3f}"
    if value != 0 and float(text) == 0:
        return f"0.001초 미만 {'늦춤' if value > 0 else '앞당김'}"
    return f"{text}초"


def _seconds(value: Fraction) -> str:
    return f"{float(value):.2f}초"


def _plain_seconds(value: Fraction) -> str:
    return f"{value.numerator}초" if value.denominator == 1 else f"{float(value):g}초"


# ─────────────────────────────────────────────────────────────
#  정규 직렬화 — sha의 재료
# ─────────────────────────────────────────────────────────────


def speech_sha256(speech: sync_check.SpeechTrack) -> str:
    """말소리 칸의 신원 — 칸 길이·칸 수·PCM 길이·구간 전부."""
    return _digest(
        {
            "hop": sync_check.fraction_text(speech.hop),
            "frame_count": speech.frame_count,
            "duration": sync_check.fraction_text(speech.duration),
            "runs": [[start, end] for start, end in speech.runs],
        }
    )


def plan_document(plan: SnapPlan) -> dict[str, object]:
    """입력(무엇을 재었나·큐 아이디·시각·글자·어긋남)·기준·출력(제안)의 정규형 — sha의 재료다."""
    source = plan.source
    return {
        "schema": PLAN_SCHEMA,
        "settings": {f.name: _text(getattr(plan.settings, f.name)) for f in fields(plan.settings)},
        "source": {
            "source_id": source.source_id,
            "transcript_revision": source.transcript_revision,
            "pcm_sha256": source.pcm_sha256,
            "vad_key": source.vad_key,
            "audio_start": sync_check.fraction_text(source.audio_start),
            "transcript_duration": sync_check.fraction_text(source.transcript_duration),
        },
        "speech_sha256": plan.speech_sha256,
        "offset": {"status": plan.offset_status, "seconds": _optional(plan.offset_seconds)},
        "shift": sync_check.fraction_text(plan.shift),
        "proposals": [_proposal_document(proposal) for proposal in plan.proposals],
    }


def _proposal_document(proposal: SnapProposal) -> dict[str, object]:
    return {
        "id": proposal.cue_id,
        "text": proposal.text,
        "old_start": sync_check.fraction_text(proposal.old_start),
        "old_end": sync_check.fraction_text(proposal.old_end),
        "new_start": sync_check.fraction_text(proposal.new_start),
        "new_end": sync_check.fraction_text(proposal.new_end),
        "status": proposal.status,
        "reason": proposal.reason,
        "shifted": proposal.shifted,
        "start_anchor": _optional(proposal.start_anchor),
        "end_anchor": _optional(proposal.end_anchor),
    }


def _text(value: object) -> str:
    return sync_check.fraction_text(value) if isinstance(value, Fraction) else str(value)


def _optional(value: Fraction | None) -> str | None:
    return None if value is None else sync_check.fraction_text(value)


def _digest(document: object) -> str:
    body = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


__all__ = [
    "PLAN_SCHEMA",
    "STATUSES",
    "STATUS_BLOCKED",
    "STATUS_MOVED",
    "STATUS_NO_SPEECH",
    "STATUS_UNCHANGED",
    "SnapPlan",
    "SnapProposal",
    "SnapSource",
    "plan_document",
    "propose",
    "speech_sha256",
]
