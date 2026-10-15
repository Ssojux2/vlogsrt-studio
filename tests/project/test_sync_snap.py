"""시각 맞춤 제안(D59 19g) — 큐 경계를 말소리 경계에 맞춘 **제안**의 순수 계산을 못 박는다.

시험이 못 박는 것:

- 큐는 하나도 빠지거나 늘거나 순서가 바뀌지 않는다 — 제안은 큐마다 정확히 하나, 입력 순서다.
  아이디·글자는 그대로, 시각은 정확 유리수다.
- 순서: 센 전체 어긋남(D55 `+` = 자막이 이르다)을 먼저 밀고 → 경계를 말소리 경계에 맞추고 →
  lead_in/lead_out을 더한다. 세지 않은 어긋남은 밀지 않고, 길이 0·뒤집힌 큐도 밀지 않는다.
- 다른 큐와 `min_gap`보다 가까워지지 않고, 입력에 없던 겹침·시작 순서 뒤집힘을 만들지 않으며
  (정렬되지 않거나 겹친 입력에서도), `[0, 길이]` 안이다. 너무 짧아지면 `blocked`(그 전 시각),
  말소리가 없으면 `no_speech` 표시만(그 전 시각) — 지우지 않는다(규칙 4·9).
- 경계가 말소리 한가운데면 그 말소리의 바깥 경계에만 맞춘다 — 큐가 덮던 말소리를 건너 안쪽으로
  줄이지 않는다.
- **적용 규칙**: 모든 제안을 함께 적용하면 원본 자막 편집 검사(`_validate_rows`)를 지난다.
- 제안의 sha는 입력·기준·출력 어느 하나라도 바뀌면 바뀌고, 같으면 같다.
- 기준의 lead·간격·최소 길이는 refine(`vlogsrt.config.Config`) 값과 정확 유리수로 같다.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import fields, replace
from fractions import Fraction

import pytest

from vlogsrt.config import Config
from vlogsrt.project import sync_check, sync_snap
from vlogsrt.project.config import (
    SYNC_CHECK_SETTINGS,
    SYNC_SNAP_SETTINGS,
    ProjectConfig,
    SyncSnapSettings,
    sync_snap_problems,
)
from vlogsrt.project.raw import source_transcript
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.sync_check import Cue, ShiftEstimate, SpeechTrack
from vlogsrt.project.sync_snap import SnapPlan, SnapSource

RATE = 16000
HOP = Fraction(512, RATE)  # VAD 창 하나 = 0.032초(ProjectConfig 기본)
S = SYNC_SNAP_SETTINGS
NO_SHIFT = ShiftEstimate(sync_check.SHIFT_LOW_CONFIDENCE, Fraction(0))


def track(runs: list[tuple[int, int]], frames: int) -> SpeechTrack:
    """칸 번호의 말소리 구간 — 칸 하나 = 0.032초(100칸 = 3.2초)."""
    return SpeechTrack(HOP, frames, frames * HOP, tuple(runs))


def cue(cue_id: str, start: str, end: str, text: str = "말") -> Cue:
    return Cue(cue_id, Fraction(start), Fraction(end), text)


def source_for(speech: SpeechTrack, **changes: object) -> SnapSource:
    base = SnapSource(
        source_id="source_a",
        transcript_revision="rev-1",
        pcm_sha256="a" * 64,
        vad_key="b" * 64,
        audio_start=Fraction(0),
        transcript_duration=speech.duration,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def snap(
    cues: list[Cue],
    speech: SpeechTrack,
    offset: ShiftEstimate = NO_SHIFT,
    *,
    settings: SyncSnapSettings = S,
    source: SnapSource | None = None,
) -> SnapPlan:
    return sync_snap.propose(
        cues, speech, offset, source=source or source_for(speech), settings=settings
    )


def counted(seconds: str) -> ShiftEstimate:
    return ShiftEstimate(sync_check.SHIFT_COUNTED, Fraction(seconds), Fraction(1, 2), 3.0)


def head(start: Fraction, end: Fraction) -> Fraction:
    return min(start, end)


def tail(start: Fraction, end: Fraction) -> Fraction:
    return max(start, end)


# ── 적용 — 19h가 원본 자막에 쓰는 그대로(공통 시계), 편집 검사 하나로 ───────────────────


def applied_rows(plan: SnapPlan, *, only_moved: bool = False) -> tuple[list[dict], set[str]]:
    """제안을 원본 자막 줄로 — `only_moved`면 「옮김」 판정만 새 시각(적용 규칙을 어긴 꼴)."""
    rows: list[dict] = []
    changed: set[str] = set()
    for proposal in plan.proposals:
        take = proposal.status == sync_snap.STATUS_MOVED if only_moved else True
        start, end = (proposal.new_start, proposal.new_end)
        if not take:
            start, end = proposal.old_start, proposal.old_end
        if (start, end) != (proposal.old_start, proposal.old_end):
            changed.add(proposal.cue_id)
        rows.append(
            {
                "utterance_id": proposal.cue_id,
                "start": source_transcript.encoded(plan.on_common(start)),
                "end": source_transcript.encoded(plan.on_common(end)),
                "text": proposal.text,
            }
        )
    return rows, changed


def validates(plan: SnapPlan, *, only_moved: bool = False) -> bool:
    rows, changed = applied_rows(plan, only_moved=only_moved)
    try:
        source_transcript._validate_rows(rows, plan.source.transcript_duration, changed)
    except RawWorkspaceError:
        return False
    return True


def input_validates(cues: list[Cue], duration: Fraction) -> bool:
    rows = [
        {
            "utterance_id": item.cue_id,
            "start": source_transcript.encoded(item.start),
            "end": source_transcript.encoded(item.end),
            "text": item.text,
        }
        for item in cues
    ]
    try:
        source_transcript._validate_rows(rows, duration, set())
    except RawWorkspaceError:
        return False
    return True


# ── 모든 계획이 지키는 계약 ──────────────────────────────────────────────────────


def assert_well_formed(
    plan: SnapPlan,
    cues: list[Cue],
    low: Fraction,
    high: Fraction,
    settings: SyncSnapSettings = S,
    speech: SpeechTrack | None = None,
) -> None:
    """모든 계획이 지키는 계약 — 시험마다, 무작위 입력마다 같은 검사다."""
    proposals = plan.proposals
    assert [p.cue_id for p in proposals] == [c.cue_id for c in cues]
    assert [p.text for p in proposals] == [c.text for c in cues]
    assert [(p.old_start, p.old_end) for p in proposals] == [(c.start, c.end) for c in cues]
    bases = [
        (
            p.old_start + (plan.shift if p.shifted else 0),
            p.old_end + (plan.shift if p.shifted else 0),
        )
        for p in proposals
    ]
    for proposal, base in zip(proposals, bases, strict=True):
        _assert_one(proposal, base, low, high, settings)
        if proposal.shifted:
            assert low <= head(*base) and tail(*base) <= high
            assert proposal.old_start < proposal.old_end  # 길이 0·뒤집힌 줄은 밀지 않는다
        if speech is not None and proposal.status in (
            sync_snap.STATUS_MOVED,
            sync_snap.STATUS_UNCHANGED,
        ):
            _assert_keeps_speech(proposal, base, speech)
    _assert_neighbour_gaps(proposals, bases, settings)
    _assert_nothing_new_crosses(proposals, cues)
    if input_validates(cues, plan.source.transcript_duration):
        assert validates(plan)  # 적용 규칙: 전부 적용하면 편집 검사를 지난다


def _assert_one(
    proposal: sync_snap.SnapProposal,
    base: tuple[Fraction, Fraction],
    low: Fraction,
    high: Fraction,
    settings: SyncSnapSettings,
) -> None:
    assert isinstance(proposal.new_start, Fraction) and isinstance(proposal.new_end, Fraction)
    assert proposal.status in sync_snap.STATUSES
    assert "니다" in proposal.reason and proposal.reason.endswith(".")  # 존댓말 문장
    assert "말이 아니" not in proposal.reason and "+0.000초" not in proposal.reason
    if proposal.status in (sync_snap.STATUS_BLOCKED, sync_snap.STATUS_NO_SPEECH):
        assert (proposal.new_start, proposal.new_end) == base
    if proposal.status == sync_snap.STATUS_UNCHANGED:
        assert (proposal.new_start, proposal.new_end) == (proposal.old_start, proposal.old_end)
    new, old = (proposal.new_start, proposal.new_end), (proposal.old_start, proposal.old_end)
    assert proposal.moves == (new != old)
    if proposal.shift_only:  # 밀기만 한 줄 — 판정이 막힘·못 찾음이어도 시각은 움직인다
        assert new == base and proposal.moves
    anchored = proposal.start_anchor is not None or proposal.end_anchor is not None
    if anchored:
        assert proposal.status in (sync_snap.STATUS_MOVED, sync_snap.STATUS_UNCHANGED)
        assert proposal.new_end - proposal.new_start >= settings.min_duration
    if proposal.start_anchor is not None:
        assert proposal.new_start <= proposal.start_anchor
    if proposal.end_anchor is not None:
        assert proposal.new_end >= proposal.end_anchor
    for new, old in ((proposal.new_start, base[0]), (proposal.new_end, base[1])):
        if new != old:
            assert low <= new <= high


def _assert_keeps_speech(
    proposal: sync_snap.SnapProposal, base: tuple[Fraction, Fraction], speech: SpeechTrack
) -> None:
    """맞춤은 큐가 덮던 말소리를 안쪽으로 건너 내놓지 않는다 — 덮던 말소리는 새 시각에도 있다."""
    for start, end in sync_check.merge_runs(speech.runs):
        low = max(start * speech.hop, base[0])
        high = min(min(end * speech.hop, speech.duration), base[1])
        if high > low:
            assert proposal.new_start <= low and high <= proposal.new_end, (proposal, low, high)


def _assert_neighbour_gaps(
    proposals: tuple[sync_snap.SnapProposal, ...],
    bases: list[tuple[Fraction, Fraction]],
    settings: SyncSnapSettings,
) -> None:
    for index in range(len(proposals) - 1):
        left, right = proposals[index], proposals[index + 1]
        gap = head(right.new_start, right.new_end) - tail(left.new_start, left.new_end)
        before = head(*bases[index + 1]) - tail(*bases[index])
        assert gap >= min(settings.min_gap, before), (left, right)
        moved = (left.new_end != bases[index][1]) or (right.new_start != bases[index + 1][0])
        if moved:
            assert gap >= settings.min_gap, (left, right)


def _assert_nothing_new_crosses(
    proposals: tuple[sync_snap.SnapProposal, ...], cues: list[Cue]
) -> None:
    """어느 두 큐(이웃만이 아니라)도 입력에 없던 겹침이나 시작 순서 뒤집힘을 얻지 않는다."""
    for i, left in enumerate(proposals):
        for j in range(i + 1, len(proposals)):
            right = proposals[j]
            if tail(left.new_start, left.new_end) > head(right.new_start, right.new_end):
                assert tail(cues[i].start, cues[i].end) > head(cues[j].start, cues[j].end)
            if left.new_start > right.new_start:
                assert cues[i].start > cues[j].start


# ── 큐는 그대로 하나씩 — 순서·아이디·글자 ─────────────────────────────────────────


def test_one_proposal_per_cue_in_input_order_with_ids_and_text_kept() -> None:
    speech = track([(100, 150), (300, 310)], 400)
    cues = [
        cue("u0", "3.0", "5.0", "맞춰질 말"),
        cue("u1", "6.0", "6.0", "길이 0"),
        cue("u2", "9.4", "10.2", "짧아질 말"),
        cue("u3", "11.0", "12.0", "말소리 없음"),
    ]
    plan = snap(cues, speech)
    assert len(plan.proposals) == len(cues)
    assert [p.status for p in plan.proposals] == [
        sync_snap.STATUS_MOVED,
        sync_snap.STATUS_BLOCKED,
        sync_snap.STATUS_BLOCKED,
        sync_snap.STATUS_NO_SPEECH,
    ]
    assert_well_formed(plan, cues, Fraction(0), speech.duration, speech=speech)


def test_the_same_input_gives_the_same_plan_and_sha() -> None:
    speech = track([(100, 150), (200, 250)], 400)
    cues = [cue("a", "3.0", "4.9"), cue("b", "6.5", "7.9")]
    first = snap(cues, speech)
    second = snap(list(cues), track([(100, 150), (200, 250)], 400))
    assert first == second
    assert first.sha256 == second.sha256 and len(first.sha256) == 64


def test_times_are_exact_fractions_anchor_minus_lead_in_plus_lead_out() -> None:
    speech = track([(100, 150)], 400)  # 3.2–4.8초
    (proposal,) = snap([cue("a", "3.0", "5.0")], speech).proposals
    assert proposal.status == sync_snap.STATUS_MOVED
    assert proposal.start_anchor == Fraction(16, 5) and proposal.end_anchor == Fraction(24, 5)
    assert proposal.new_start == Fraction(16, 5) - S.lead_in == Fraction(31, 10)
    assert proposal.new_end == Fraction(24, 5) + S.lead_out == Fraction(51, 10)
    assert "시작과 끝" in proposal.reason


def test_a_sub_millisecond_move_is_never_written_as_zero() -> None:
    """검증 잔손질 (f) — 0.0001초 움직임을 「+0.000초」(움직이지 않은 듯)로 쓰지 않는다."""
    speech = track([(100, 150)], 400)
    (proposal,) = snap([cue("a", "3.0999", "5.0")], speech).proposals
    assert proposal.start_move == Fraction(1, 10000)
    assert "시작 0.001초 미만 늦춤" in proposal.reason and "+0.000초" not in proposal.reason
    (earlier,) = snap([cue("a", "3.1001", "5.0")], speech).proposals
    assert earlier.start_move == Fraction(-1, 10000)
    assert "시작 0.001초 미만 앞당김" in earlier.reason
    assert "-0.000초" not in earlier.reason and "-0.001초 미만" not in earlier.reason


# ── 범위·간격·길이 ──────────────────────────────────────────────────────────────


def test_moves_stay_inside_zero_and_the_duration() -> None:
    speech = track([(1, 20), (170, 200)], 200)  # 0.032–0.64초 · 5.44–6.4초(끝)
    cues = [cue("head", "0", "0.5"), cue("tail", "5.5", "6.3")]
    first, last = snap(cues, speech).proposals
    assert first.new_start == 0  # lead_in은 0초 앞으로 가지 않는다
    assert last.new_end == speech.duration  # lead_out은 끝을 넘지 않는다
    assert_well_formed(snap(cues, speech), cues, Fraction(0), speech.duration, speech=speech)


def test_the_range_is_the_transcript_range_on_the_pcm_clock() -> None:
    """PCM 표본 0이 영상보다 0.5초 앞이면(audio_start −0.5) PCM 0.5초 앞으로는 가지 않는다."""
    speech = track([(16, 40), (140, 200)], 200)  # 0.512–1.28초 · 4.48–6.4초
    source = source_for(speech, audio_start=Fraction(-1, 2), transcript_duration=Fraction(5))
    cues = [cue("a", "0.6", "1.5"), cue("b", "4.6", "5.4")]
    plan = snap(cues, speech, source=source)
    first, second = plan.proposals
    assert first.new_start == Fraction(1, 2)  # 0.512 − 0.1이 아니라 범위 머리
    assert second.end_anchor is None  # 6.4초 끝은 자막 범위(5.5초) 밖이다
    assert "자막 범위 너머" in second.reason
    assert plan.on_common(first.new_start) == 0
    assert_well_formed(plan, cues, Fraction(1, 2), Fraction(11, 2), speech=speech)


def test_min_gap_holds_against_both_neighbours() -> None:
    """붙은 큐 셋 — 가운데 큐의 lead_in은 앞 큐 끝 + min_gap에서 멈추고, 붙은 끝은 그대로다."""
    speech = track([(31, 61), (66, 108), (112, 140)], 200)
    cues = [cue("a", "1.0", "2.0"), cue("b", "2.0", "3.5"), cue("c", "3.5", "4.5")]
    a, b, c = snap(cues, speech).proposals
    assert a.end_anchor is None and a.new_end == 2  # 1.952초 끝은 붙은 경계와 min_gap 안이다
    assert b.new_start - a.new_end == S.min_gap
    assert b.end_anchor is None and b.new_end == Fraction(7, 2)
    assert c.new_start - b.new_end == S.min_gap
    assert c.new_end == Fraction(448, 100) + S.lead_out
    assert_well_formed(snap(cues, speech), cues, Fraction(0), speech.duration, speech=speech)


def test_an_edge_beyond_a_neighbour_gets_its_own_reason() -> None:
    """검증 잔손질 (e) — 경계가 있는데 이웃 너머인 것은 「경계가 없음」과 다른 말이다."""
    speech = track([(31, 61), (66, 108), (112, 140)], 200)
    cues = [cue("a", "1.0", "2.0"), cue("b", "2.0", "3.5"), cue("c", "3.5", "4.5")]
    a, _, _ = snap(cues, speech).proposals
    assert "끝에 가까운 말소리 경계는 이웃 자막이나 자막 범위 너머에 있어" in a.reason
    assert "맞출 말소리 경계가 없습니다" not in a.reason


def test_lead_in_takes_the_shared_silence_before_lead_out() -> None:
    speech = track([(31, 59), (66, 108)], 200)  # 끝 1.888초 · 다음 시작 2.112초
    cues = [cue("a", "1.0", "2.0"), cue("b", "2.5", "3.5")]
    a, b = snap(cues, speech).proposals
    assert b.new_start == Fraction(2012, 1000)  # 2.112 − 0.1 (앞 끝 + min_gap 1.968보다 뒤)
    assert a.new_end == b.new_start - S.min_gap == Fraction(1932, 1000)  # 남은 만큼만
    assert_well_formed(snap(cues, speech), cues, Fraction(0), speech.duration, speech=speech)


def test_a_snap_shorter_than_min_duration_is_blocked_with_the_old_times() -> None:
    speech = track([(100, 110)], 200)  # 3.2–3.52초
    (proposal,) = snap([cue("a", "3.0", "3.8")], speech).proposals
    assert proposal.status == sync_snap.STATUS_BLOCKED
    assert (proposal.new_start, proposal.new_end) == (Fraction(3), Fraction(19, 5))
    assert proposal.start_anchor is None and proposal.end_anchor is None
    assert "0.72초" in proposal.reason and "0.85초" in proposal.reason
    assert "원래 시각을 그대로 둡니다" in proposal.reason
    assert not proposal.moves


def test_no_speech_within_reach_is_a_flag_only() -> None:
    speech = track([(300, 350)], 400)  # 9.6–11.2초
    (proposal,) = snap([cue("a", "2", "3", "어디 있지")], speech).proposals
    assert proposal.status == sync_snap.STATUS_NO_SPEECH
    assert (proposal.new_start, proposal.new_end) == (Fraction(2), Fraction(3))
    assert proposal.text == "어디 있지"
    assert "말소리를 못 찾아" in proposal.reason and "측정 한계일 수 있음" in proposal.reason
    assert "말이 아니" not in proposal.reason


# ── 말소리 한가운데의 경계 — 바깥 경계에만, 덮던 말소리를 건너 안쪽으로 줄이지 않는다 ──────


def test_a_start_inside_speech_only_snaps_out_to_that_runs_onset() -> None:
    """검증 지적 6 — 시작이 말소리 한가운데면 그 말소리의 머리(바깥)에만 맞춘다."""
    speech = track([(100, 200)], 400)  # 3.2–6.4초
    (proposal,) = snap([cue("a", "3.5", "6.0")], speech).proposals
    assert proposal.start_anchor == Fraction(16, 5)  # 3.2 — 바깥쪽 머리
    assert proposal.end_anchor == Fraction(32, 5)  # 끝도 한가운데라 그 꼬리 6.4로(바깥)
    far = track([(60, 200), (205, 260)], 400)  # 1.92–6.4초 · 6.56–8.32초
    (inside,) = snap([cue("b", "5.9", "8.0")], far).proposals
    assert inside.start_anchor is None  # 6.56초 머리가 더 가깝지만 5.9–6.4초 말소리를 내놓게 된다
    assert inside.new_start == Fraction(59, 10)
    assert "시작은 말소리 한가운데라 그 말소리의 바깥 경계에만 맞추는데" in inside.reason
    assert "앞뒤 1초 밖입니다" in inside.reason
    assert_well_formed(snap([cue("b", "5.9", "8.0")], far), [cue("b", "5.9", "8.0")],
                       Fraction(0), far.duration, speech=far)  # fmt: skip


def test_an_end_inside_speech_never_moves_inward() -> None:
    speech = track([(100, 200)], 400)  # 3.2–6.4초
    (proposal,) = snap([cue("a", "3.0", "5.0")], speech).proposals
    assert proposal.end_anchor is None  # 꼬리 6.4초는 1.4초 밖 — 안쪽 경계로 가지 않는다
    assert proposal.new_end == 5
    assert "끝은 말소리 한가운데라" in proposal.reason


def test_an_inside_edge_past_a_neighbour_gets_the_inside_wall_reason() -> None:
    speech = track([(100, 200)], 400)  # 3.2–6.4초
    cues = [cue("a", "3.0", "5.6"), cue("b", "5.8", "7.0")]
    a, _ = snap(cues, speech).proposals
    assert a.end_anchor is None and a.new_end == Fraction(28, 5)
    assert "끝은 말소리 한가운데라" in a.reason and "이웃 자막이나 자막 범위 너머입니다" in a.reason
    assert_well_formed(snap(cues, speech), cues, Fraction(0), speech.duration, speech=speech)


def test_a_start_never_jumps_over_speech_to_a_later_onset() -> None:
    """조용한 곳의 시작은 바로 뒤 첫 머리에만 — 그것이 벽 너머여도 더 뒤 머리로 건너뛰지 않는다."""
    speech = track([(64, 70), (80, 100)], 300)  # 2.048–2.24초 · 2.56–3.2초
    cues = [cue("p", "0.5", "2.0"), cue("c", "2.04", "3.5")]  # 붙어 있어 머리의 바닥은 2.08초
    _, after = snap(cues, speech).proposals
    assert after.start_anchor is None and after.new_start == Fraction(51, 25)
    assert "시작에 가까운 말소리 경계는 이웃 자막이나 자막 범위 너머에 있어" in after.reason
    assert_well_formed(snap(cues, speech), cues, Fraction(0), speech.duration, speech=speech)


# ── 전체 어긋남 — 센 것만, D55 부호 그대로 ─────────────────────────────────────────


def test_a_counted_shift_goes_first_and_an_uncounted_one_is_not_applied() -> None:
    speech = track([(100, 150), (200, 250)], 400)  # 3.2–4.8초 · 6.4–8.0초
    cues = [cue("a", "1.2", "2.8"), cue("b", "4.4", "6.0")]  # 2초 이르다
    shifted = snap(cues, speech, counted("2"))
    a, b = shifted.proposals
    assert shifted.shift == 2 and a.shifted and b.shifted
    assert (a.new_start, a.new_end) == (Fraction(31, 10), Fraction(51, 10))
    assert (b.new_start, b.new_end) == (Fraction(63, 10), Fraction(83, 10))
    assert "전체 어긋남 +2.000초를 먼저 옮겼습니다" in a.reason
    assert "시작 +1.900초 · 끝 +2.300초" in a.reason  # 원래 시각에서 잰 움직임
    assert_well_formed(shifted, cues, Fraction(0), speech.duration, speech=speech)
    for status in (sync_check.SHIFT_LOW_CONFIDENCE, sync_check.SHIFT_EDGE):
        kept = snap(cues, speech, ShiftEstimate(status, Fraction(2), Fraction(1, 2), 3.0))
        assert kept.shift == 0 and not any(p.shifted for p in kept.proposals)
        assert [p.status for p in kept.proposals] == [sync_snap.STATUS_UNCHANGED] * 2


def bursts(seconds: int, seed: int = 7) -> list[tuple[Fraction, Fraction]]:
    """말 덩어리 — 0.8–2.5초 말, 0.6–2.5초 쉼(`test_sync_check`와 같은 모양)."""
    rng = random.Random(seed)
    spans: list[tuple[Fraction, Fraction]] = []
    at = Fraction(1)
    while at < seconds - 4:
        length = Fraction(round(rng.uniform(0.8, 2.5) * 1000), 1000)
        spans.append((at, at + length))
        at += length + Fraction(round(rng.uniform(0.6, 2.5) * 1000), 1000)
    return spans


def test_the_sign_matches_shift_estimate_subtitles_early_move_later() -> None:
    """D55의 `+`(자막이 이르다)를 그대로 받아 큐를 늦춘다 — 다시 재면 어긋남이 사라진다."""
    spans = bursts(300)
    count = -(-300 * RATE // 512)
    runs = sync_check.merge_runs((int(a / HOP), -(-b // HOP)) for a, b in spans)
    speech = SpeechTrack(HOP, count, Fraction(300), sync_check.clip_runs(runs, 0, count))
    early = Fraction(3, 2)
    cues = [Cue(f"c{i}", a - early, b - early, f"자막 {i}") for i, (a, b) in enumerate(spans)]
    before = sync_check.measure(cues, speech, SYNC_CHECK_SETTINGS)
    assert before.offset.counted and before.offset.seconds is not None and before.offset.seconds > 0
    plan = snap(cues, speech, before.offset)
    assert plan.shift == before.offset.seconds
    moved = [
        replace(c, start=p.new_start, end=p.new_end)
        for c, p in zip(cues, plan.proposals, strict=True)
    ]
    after = sync_check.measure(moved, speech, SYNC_CHECK_SETTINGS)
    assert not after.offset.counted or abs(after.offset.seconds or 0) < Fraction(1, 5)
    assert after.precision is not None and before.precision is not None
    assert after.precision > before.precision
    assert all(p.new_start > p.old_start for p in plan.proposals if p.shifted)
    assert_well_formed(plan, cues, Fraction(0), speech.duration)


def test_the_shift_is_held_where_it_would_leave_the_file_or_hit_a_held_cue() -> None:
    """0021 꼴 — 밀면 끝을 넘는 꼬리 큐는 밀지 않고, 그 큐와 부딪히는 큐도 밀지 않는다(순서)."""
    speech = track([], 312)  # 9.984초, 말소리 없음
    cues = [
        cue("c0", "0", "1.5"),
        cue("c1", "1.5", "3.0"),
        cue("c2", "5.6", "6.0"),
        cue("c3", "6.0", "7.0"),
        cue("c4", "7.5", "9.0"),
    ]
    plan = snap(cues, speech, counted("2"))
    assert [p.shifted for p in plan.proposals] == [True, True, False, False, False]
    assert "원본 길이 밖" in plan.proposals[4].reason
    assert all("옮기지 않은 자막과 겹쳐" in p.reason for p in plan.proposals[2:4])
    assert (plan.proposals[1].new_start, plan.proposals[1].new_end) == (Fraction(7, 2), Fraction(5))
    assert all(p.status == sync_snap.STATUS_NO_SPEECH for p in plan.proposals)
    assert_well_formed(plan, cues, Fraction(0), speech.duration, speech=speech)


# ── 적용 규칙 — 전부이거나 전무 ─────────────────────────────────────────────────


def shifted_0011_like() -> tuple[list[Cue], SpeechTrack]:
    """0011 꼴 — 자막 전체가 2초 이르고, 줄마다 판정이 갈린다(맞춤·짧아 막힘·말소리 없음·밀기만)."""
    speech = track([(100, 150), (160, 170), (200, 260), (400, 500)], 700)
    cues = [
        cue("u0", "1.0", "2.8"),  # → 3.0–4.8: 맞춤
        cue("u1", "2.8", "3.4"),  # → 4.8–5.4: 짧아 막힘(밀기만)
        cue("u2", "3.4", "4.4"),  # → 5.4–6.4: 맞춤
        cue("u3", "5.0", "6.0"),  # → 7.0–8.0: 말소리 한가운데
        cue("u4", "6.0", "7.0"),  # → 8.0–9.0: 말소리 한가운데·끝 너머
        cue("u5", "7.0", "8.0"),  # → 9.0–10.0: 말소리 없음(밀기만)
        cue("u6", "9.0", "10.5"),
        cue("u7", "10.5", "12.0"),
    ]
    return cues, speech


def test_applying_every_proposal_passes_the_transcript_edit_check() -> None:
    """검증 지적 1 — 모든 제안을 함께 적용하면 편집 검사(`_validate_rows`)를 지나고, 「옮김」만
    적용하면 민 이웃과 겹친다. 막힘·못 찾음 줄도 밀기만큼은 움직인다(`moves`·`shift_only`)."""
    cues, speech = shifted_0011_like()
    plan = snap(cues, speech, counted("2"))
    statuses = {p.status for p in plan.proposals}
    assert {sync_snap.STATUS_MOVED, sync_snap.STATUS_BLOCKED} <= statuses
    kept = [p for p in plan.proposals if p.status != sync_snap.STATUS_MOVED]
    assert kept and all(p.moves and p.shift_only for p in kept if p.shifted)
    assert validates(plan)
    assert not validates(plan, only_moved=True)
    assert_well_formed(plan, cues, Fraction(0), speech.duration, speech=speech)


def test_a_zero_length_cue_in_a_shifted_file_keeps_the_plan_appliable() -> None:
    """검증 지적 2 — 길이 0 줄을 밀면 「바꾼 줄」이 되어 편집 검사가 계획 전체를 거절했다."""
    speech = track([(60, 130), (250, 320)], 400)
    cues = [cue("a", "2", "4"), cue("z", "7", "7", "길이 0"), cue("b", "8", "10")]
    plan = snap(cues, speech, counted("1.5"))
    zero = plan.proposals[1]
    assert not zero.shifted and not zero.moves
    assert (zero.new_start, zero.new_end) == (Fraction(7), Fraction(7))
    assert "길이 0이거나 거꾸로인 자막이라 전체 어긋남 +1.500초도 옮기지 않았습니다" in zero.reason
    assert plan.proposals[0].shifted and plan.proposals[2].shifted
    assert validates(plan)
    assert_well_formed(plan, cues, Fraction(0), speech.duration, speech=speech)


# ── 길이 0·뒤집힘·사이·겨루기·뒤섞인 입력 ─────────────────────────────────────────


@pytest.mark.parametrize("offset", [NO_SHIFT, counted("1")])
def test_zero_length_and_reversed_cues_keep_their_text_and_times(offset: ShiftEstimate) -> None:
    """길이 0·뒤집힌 줄은 막히고 **입력 시각 그대로**다 — 센 어긋남이 있어도 밀지 않는다(검증
    지적 2, 19g 처음 판은 밀었다)."""
    speech = track([(20, 60), (150, 200), (230, 260)], 300)
    cues = [
        cue("a", "1.0", "2.0"),
        cue("zero", "3.0", "3.0", "길이 0"),
        cue("reversed", "5.0", "4.5", "뒤집힘"),
        cue("b", "6.0", "7.0"),
    ]
    plan = snap(cues, speech, offset)
    zero, flipped = plan.proposals[1], plan.proposals[2]
    for proposal, source in ((zero, cues[1]), (flipped, cues[2])):
        assert proposal.status == sync_snap.STATUS_BLOCKED
        assert proposal.text == source.text
        assert "길이 0이거나 거꾸로" in proposal.reason
        assert not proposal.shifted
        assert (proposal.new_start, proposal.new_end) == (source.start, source.end)
    assert_well_formed(plan, cues, Fraction(0), speech.duration, speech=speech)


def test_a_cue_between_two_runs_is_never_stretched_across_them() -> None:
    """시작은 뒤 말의 머리로, 끝은 앞 말의 꼬리로 — 뒤집힐 맞춤은 막는다(그 전 시각)."""
    speech = track([(94, 140), (203, 250)], 300)  # 3.008–4.48초 · 6.496–8.0초
    cues = [cue("a", "5.0", "5.9")]
    wide = SyncSnapSettings(reach_seconds=Fraction(3, 2))
    (blocked,) = snap(cues, speech, settings=wide).proposals
    assert blocked.status == sync_snap.STATUS_BLOCKED
    assert (blocked.new_start, blocked.new_end) == (Fraction(5), Fraction(59, 10))
    (kept,) = snap(cues, speech).proposals  # 1초 안에는 경계가 없다
    assert kept.status == sync_snap.STATUS_UNCHANGED
    assert "시작과 끝은 앞뒤 1초 안에 맞출 말소리 경계가 없습니다" in kept.reason
    assert "시작이 끝보다 늦어져" in blocked.reason


def test_adjacent_cues_competing_for_the_same_run_leave_it_alone() -> None:
    """앞 큐의 끝과 뒤 큐의 시작이 같은 말소리(2.304–2.88초)를 서로 가져가려 하면 둘 다 놓는다."""
    speech = track([(72, 90)], 300)
    cues = [cue("a", "0.5", "2.0"), cue("b", "3.0", "4.5")]
    a, b = snap(cues, speech).proposals
    assert (a.status, b.status) == (sync_snap.STATUS_UNCHANGED, sync_snap.STATUS_UNCHANGED)
    assert "끝은 이웃 자막과 같은 말소리를 두고 겹쳐" in a.reason
    assert "시작은 이웃 자막과 같은 말소리를 두고 겹쳐" in b.reason
    alone = [cue("a", "0.5", "2.0"), cue("b", "5.0", "6.5")]
    first, _ = snap(alone, speech).proposals  # 뒤 큐가 멀면 앞 큐가 그 말소리 끝까지 간다
    assert first.end_anchor == Fraction(288, 100)
    assert first.new_end == Fraction(288, 100) + S.lead_out
    assert_well_formed(snap(cues, speech), cues, Fraction(0), speech.duration, speech=speech)


def test_overlapping_input_never_gets_a_new_start_inversion() -> None:
    """검증 지적 3 — c1 1.730–4.931 · c2 2.099–4.073(겹친 입력): c1의 머리를 2.304초로 맞추면
    c2의 머리를 넘는다. 뒤의 모든 큐의 머리가 천장이다."""
    speech = track([(72, 160)], 300)  # 2.304–5.12초
    cues = [cue("c1", "1.730", "4.931"), cue("c2", "2.099", "4.073")]
    c1, c2 = snap(cues, speech).proposals
    assert c1.new_start <= c2.new_start
    assert c1.start_anchor is None and "이웃 자막이나 자막 범위 너머" in c1.reason
    assert_well_formed(snap(cues, speech), cues, Fraction(0), speech.duration, speech=speech)


def test_unsorted_input_with_a_shift_never_crosses_a_held_cue() -> None:
    """정렬되지 않은 입력 — 민 큐가 인접하지 않은 안 민 큐를 넘지 않는다(앞뒤 모든 큐가 벽)."""
    speech = track([], 312)  # 9.984초
    cues = [cue("c0", "5", "6"), cue("c1", "1", "2"), cue("c2", "8", "9")]
    plan = snap(cues, speech, counted("3"))  # c2는 밀면 끝을 넘고, c0을 밀면 c2와 겹친다
    assert [p.shifted for p in plan.proposals] == [False, True, False]
    assert_well_formed(plan, cues, Fraction(0), speech.duration, speech=speech)


# ── 무작위 입력 — 계약은 언제나 ──────────────────────────────────────────────────


def random_cues(rng: random.Random, duration: Fraction) -> list[Cue]:
    cues: list[Cue] = []
    moment = Fraction(rng.randint(0, 3000), 1000)
    for index in range(rng.randint(0, 40)):
        length = Fraction(rng.randint(200, 4000), 1000)
        roll = rng.random()
        start, end = moment, moment + (0 if roll < 0.06 else length)
        if 0.06 <= roll < 0.1:
            start, end = moment + Fraction(3, 10), moment
        if tail(start, end) > duration:
            break
        cues.append(Cue(f"c{index}", start, end, f"말 {index}"))
        gap = Fraction(0) if rng.random() < 0.4 else Fraction(rng.randint(0, 2500), 1000)
        moment = tail(start, end) + gap
    style = rng.random()
    if style < 0.15:
        rng.shuffle(cues)  # 순서가 뒤섞인 입력
    elif style < 0.3:
        cues = [replace(c, start=max(Fraction(0), c.start - Fraction(rng.randint(0, 1500), 1000)))
                for c in cues]  # 겹친 입력  # fmt: skip
    return cues


def random_case(seed: int) -> tuple[list[Cue], SpeechTrack, ShiftEstimate]:
    rng = random.Random(seed)
    frames = rng.randint(300, 2000)
    runs: list[tuple[int, int]] = []
    at = rng.randint(0, 40)
    while at < frames:
        length = rng.randint(3, 80)
        runs.append((at, min(frames, at + length)))
        at += length + rng.randint(1, 60)
    cues = random_cues(rng, frames * HOP)
    offset = NO_SHIFT
    if rng.random() < 0.5:
        offset = counted(str(Fraction(rng.randint(-3000, 3000), 1000)))
    return cues, track(runs, frames), offset


@pytest.mark.parametrize("seed", range(80))
def test_random_inputs_always_keep_the_contract(seed: int) -> None:
    cues, speech, offset = random_case(seed)
    for settings in (S, SyncSnapSettings(reach_seconds=Fraction(2))):
        plan = snap(cues, speech, offset, settings=settings)
        assert_well_formed(plan, cues, Fraction(0), speech.duration, settings, speech)
        assert plan == snap(cues, speech, offset, settings=settings)


# ── sha — 입력·기준·출력 ─────────────────────────────────────────────────────────


def test_the_sha_moves_with_every_input_and_setting_and_only_then() -> None:
    speech = track([(100, 150), (200, 250)], 400)
    cues = [cue("a", "3.0", "4.9", "하나"), cue("b", "6.5", "7.9", "둘")]
    base = snap(cues, speech)
    assert base.sha256 == snap([replace(c) for c in cues], speech).sha256
    variants = [
        snap(cues, speech, source=source_for(speech, transcript_revision="rev-2")),
        snap(cues, speech, source=source_for(speech, source_id="source_b")),
        snap(cues, speech, source=source_for(speech, pcm_sha256="c" * 64)),
        snap(cues, speech, source=source_for(speech, vad_key="d" * 64)),
        snap(cues, speech, source=source_for(speech, audio_start=Fraction(-1, 1000))),
        snap(cues, speech, source=source_for(speech, transcript_duration=Fraction(13))),
        snap([replace(cues[0], cue_id="z"), cues[1]], speech),
        snap([replace(cues[0], start=Fraction(301, 100)), cues[1]], speech),
        snap([replace(cues[0], text="하나!"), cues[1]], speech),
        snap(cues, track([(100, 151), (200, 250)], 400)),
        snap(cues, track([(100, 150), (200, 250)], 401)),
        snap(cues, speech, counted("0.5")),
        snap(cues, speech, counted("0.25")),
        snap(cues, speech, ShiftEstimate(sync_check.SHIFT_EDGE, Fraction(1, 2))),
    ]
    variants += [
        snap(cues, speech, settings=replace(S, **{name: value}))
        for name, value in (
            ("version", "sync-snap/v1-test"),
            ("reach_seconds", Fraction(3, 2)),
            ("lead_in", Fraction(2, 10)),
            ("lead_out", Fraction(2, 10)),
            ("min_gap", Fraction(1, 10)),
            ("min_duration", Fraction(1)),
        )
    ]
    shas = [base.sha256, *(plan.sha256 for plan in variants)]
    assert len(set(shas)) == len(shas)


def test_the_sha_is_over_the_canonical_plan_document() -> None:
    speech = track([(100, 150)], 400)
    plan = snap([cue("a", "3.0", "5.0")], speech)
    document = sync_snap.plan_document(plan)
    body = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == plan.sha256
    assert document["schema"] == sync_snap.PLAN_SCHEMA
    (row,) = document["proposals"]  # type: ignore[misc]
    assert row["new_start"] == "31/10" and row["old_start"] == "3/1"
    assert document["settings"]["version"] == S.version  # type: ignore[index]


# ── 기준 — refine 값 그대로 · 지문 밖 ────────────────────────────────────────────


def test_lead_gap_and_length_are_exactly_the_refine_values() -> None:
    refine = Config()
    assert S.lead_in == Fraction(str(refine.lead_in)) == Fraction(1, 10)
    assert S.lead_out == Fraction(str(refine.lead_out)) == Fraction(3, 10)
    assert S.min_gap == Fraction(str(refine.min_gap)) == Fraction(2, 25)
    assert S.min_duration == Fraction(str(refine.min_duration)) == Fraction(17, 20)
    for name in ("reach_seconds", "lead_in", "lead_out", "min_gap", "min_duration"):
        assert type(getattr(S, name)) is Fraction, name


def test_settings_are_frozen_and_validated() -> None:
    assert sync_snap_problems(S) == []
    assert sync_snap_problems(SyncSnapSettings(reach_seconds=Fraction(0)))
    assert sync_snap_problems(SyncSnapSettings(lead_in=Fraction(-1, 10)))
    assert sync_snap_problems(SyncSnapSettings(min_duration=0.85))  # type: ignore[arg-type]
    assert sync_snap_problems(SyncSnapSettings(version=" "))
    with pytest.raises(ValueError, match="min_gap"):
        snap([], track([], 10), settings=SyncSnapSettings(min_gap=Fraction(-1)))
    with pytest.raises(AttributeError):
        S.reach_seconds = Fraction(9)  # type: ignore[misc]


def test_settings_live_outside_every_fingerprinted_config() -> None:
    from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
    from vlogsrt.project.raw.config import RawCaptionContract, RawWorkflowConfig, SourceTextSettings

    names = {f.name for f in fields(SyncSnapSettings)}
    owners = (
        ProjectConfig,
        RawWorkflowConfig,
        RawCaptionContract,
        RawUnderstandingSettings,
        SourceTextSettings,
    )
    for owner in owners:
        owned = {f.name for f in fields(owner)}
        assert not names & owned, owner
        assert not any("snap" in name for name in owned), owner


def test_an_empty_transcript_is_an_empty_plan() -> None:
    plan = snap([], track([(1, 5)], 100))
    assert plan.proposals == () and len(plan.sha256) == 64
