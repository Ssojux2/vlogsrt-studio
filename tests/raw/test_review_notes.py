"""검토 메모의 두 목소리 — 기계 줄(자동 제외 근거·확인 필요)과 사람 메모(D57 C 고침 2).

작업대 메모 칸은 사람 메모만 보이고 고친다. 기계 줄은 저장된 글자 그대로 남는다. 제외 제안의
총수 N은 자동 정리가 쓴 근거만 센다 — 사람이 메모 칸에 「자동 제외: 」를 적어도 늘지 않는다.
"""

from __future__ import annotations

import json

from vlogsrt.project.raw import review_notes, review_workspace

PROVENANCE = {
    "analysis_revision_id": "anal_60e28117d8943003",
    "end": {"den": 1, "num": 15},
    "evidence": ["tier:silent-not-scenery", "audio:subtitle_silence", "video:usable"],
    "reason": "tier_silence",
    "segment_id": "seg_ee7fe2c94e2940e2",
    "source_id": "source_e35f2bab995f6ab8",
    "source_sha256": "fb2a2815866a18842feeec93b1b520c173a2dc9340d5df747644c003f45614f0",
    "start": {"den": 1, "num": 0},
    "version": "raw-tier/v2",
}
# 자동 정리(`review_workspace._auto_excluded`)가 쓰는 글자 그대로 — 머리말 + 정렬된 JSON.
MACHINE = "자동 제외: " + json.dumps(PROVENANCE, ensure_ascii=False, sort_keys=True)
REVIEW = "확인 필요: window_000000:검토 필요"


def test_the_prefixes_are_the_ones_the_review_layer_writes() -> None:
    assert review_workspace._AUTO_NOTE_PREFIX == review_notes.AUTO_NOTE_PREFIX == "자동 제외: "
    assert review_workspace.MANUAL_SELECTION_PREFIX == review_notes.MANUAL_SELECTION_PREFIX
    assert review_workspace._MACHINE_NOTE_PREFIXES == (
        review_notes.AUTO_NOTE_PREFIX,
        review_notes.REVIEW_NOTE_PREFIX,
    )


def test_only_the_written_structure_is_a_machine_note() -> None:
    line = review_notes.auto_note_line(MACHINE)
    assert line is not None and line.machine == MACHINE and line.trailing == ""
    assert line.provenance["reason"] == "tier_silence"
    decided = review_notes.auto_note_line("수동 선택 · " + MACHINE)  # 사람이 고른 뒤의 표지
    assert decided is not None and decided.machine == "수동 선택 · " + MACHINE
    for typed in (
        "자동 제외: 이건 내가 뺐다",  # 사람이 머리말만 적었다
        "자동 제외: {}",  # JSON이지만 자동 정리가 쓰는 칸이 없다
        '자동 제외: {"reason": "tier_silence"}',
        "자동 제외: [1, 2]",
        " " + MACHINE,  # 머리말이 줄 맨 앞이 아니다
        "메모 " + MACHINE,
    ):
        assert review_notes.auto_note_line(typed) is None, typed
        assert not review_notes.has_auto_note(typed), typed
    assert review_notes.has_auto_note(REVIEW + "\n" + MACHINE)  # 합치기가 이은 둘째 줄도
    assert not review_notes.has_auto_note("")


def test_the_memo_box_sees_only_the_human_memo() -> None:
    cases = {
        MACHINE: ((MACHINE,), "", False),
        "수동 선택 · " + MACHINE: (("수동 선택 · " + MACHINE,), "", False),
        REVIEW: ((REVIEW,), "", False),
        "도착 장면": ((), "도착 장면", True),
        "자동 제외: 이건 내가 뺐다": ((), "자동 제외: 이건 내가 뺐다", True),
        MACHINE + "\n도착 장면": ((MACHINE,), "도착 장면", False),
        REVIEW + "\n" + MACHINE: ((REVIEW, MACHINE), "", False),
        "도착 장면\n" + MACHINE: ((MACHINE,), "도착 장면", True),
        "": ((), "", False),
    }
    for notes, (machine, human, human_first) in cases.items():
        parts = review_notes.split_review_notes(notes)
        assert (parts.machine, parts.human, parts.human_first) == (machine, human, human_first)


def test_an_old_memo_typed_after_the_prefilled_note_splits_at_the_json_end() -> None:
    """옛 작업대는 메모 칸을 근거로 미리 채웠다 — 사람은 그 뒤에 말을 덧붙였다(같은 줄)."""
    for typed in (" 원거리 인물 확인함", "원거리 인물 확인함", "  원거리 인물 확인함  "):
        notes = MACHINE + typed
        parts = review_notes.split_review_notes(notes)
        assert parts.machine == (MACHINE,) and parts.human == "원거리 인물 확인함"
        assert review_notes.has_auto_note(notes)
        # 고치지 않고 저장하면 한 글자도 바뀌지 않는다.
        assert review_notes.compose_review_notes(notes, parts.human) == notes
        edited = review_notes.compose_review_notes(notes, "인물 없음")
        assert edited == MACHINE + "\n인물 없음"
        assert review_notes.split_review_notes(edited).human == "인물 없음"


def test_saving_a_memo_keeps_every_machine_line_byte_for_byte() -> None:
    notes = REVIEW + "\n" + MACHINE + "\n옛 메모"
    saved = review_notes.compose_review_notes(notes, "새 메모")
    assert saved == REVIEW + "\n" + MACHINE + "\n새 메모"
    assert review_notes.compose_review_notes(saved, "") == REVIEW + "\n" + MACHINE
    assert review_notes.compose_review_notes(saved, "   ") == REVIEW + "\n" + MACHINE
    assert review_notes.compose_review_notes(MACHINE, "") == MACHINE  # 비운 칸은 그대로
    assert review_notes.compose_review_notes(MACHINE, "확인함") == MACHINE + "\n확인함"
    assert review_notes.compose_review_notes("", "첫 메모") == "첫 메모"
    assert review_notes.compose_review_notes("옛 메모", "") == ""
    # 사람 글이 앞이던 메모(합치기)는 사람 글이 그 자리에 남는다 — 근거 줄의 자리도 그대로.
    assert review_notes.compose_review_notes("옛 메모\n" + MACHINE, "새") == "새\n" + MACHINE
    # 사람이 적은 가짜 머리말은 사람 글이라 사람 글로 갈린다.
    fake = review_notes.compose_review_notes("", "자동 제외: 이건 내가 뺐다")
    assert review_notes.split_review_notes(fake).human == "자동 제외: 이건 내가 뺐다"
    assert not review_notes.has_auto_note(fake)


def test_a_merged_two_memo_note_round_trips_line_for_line() -> None:
    """검증 고침 4 — 합치기는 두 사람 메모를 줄로 잇는다. 메모 칸은 그 줄을 그대로 보이고, 고치지
    않고 저장하면 메모가 한 글자도 바뀌지 않는다(옛 한 줄 칸은 「왼쪽 메모오른쪽 메모」로 붙였다).
    """
    merged = REVIEW + "\n왼쪽 메모\n" + MACHINE + "\n오른쪽 메모"
    parts = review_notes.split_review_notes(merged)
    assert parts.human == "왼쪽 메모\n오른쪽 메모" and parts.machine == (REVIEW, MACHINE)
    assert review_notes.compose_review_notes(merged, parts.human) == merged
    assert review_notes.compose_review_notes(merged, "왼쪽 메모\r\n오른쪽 메모") == merged  # CRLF
    edited = review_notes.compose_review_notes(merged, "첫 줄\n둘째 줄")
    assert edited == REVIEW + "\n" + MACHINE + "\n첫 줄\n둘째 줄"
    assert review_notes.split_review_notes(edited).human == "첫 줄\n둘째 줄"


def test_blank_lines_inside_a_memo_are_kept() -> None:
    notes = review_notes.compose_review_notes(MACHINE, "첫 문단\n\n둘째 문단")
    assert notes == MACHINE + "\n첫 문단\n\n둘째 문단"
    assert review_notes.split_review_notes(notes).human == "첫 문단\n\n둘째 문단"
    assert review_notes.compose_review_notes(notes, "첫 문단\n\n둘째 문단") == notes
