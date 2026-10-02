"""A7-3 수동 리비전 도메인 — 편집 계약·연쇄·정준 직렬화의 검사.

모든 편집은 새 리비전이고 거부는 이전 리비전을 그대로 둔다. 글자 편집이 시각을
건드리지 않는다·시각은 사용자가 준 값만 들어간다·분할은 균등 배분이 아니라
명시적 값이다·병합은 맞닿은 이웃만 — §11.7의 편집 계약을 그대로 잰다.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from vlogsrt.project.raw.caption_manual import (
    TEXT_BASIS_AUTO,
    TEXT_BASIS_MANUAL,
    AutoIdentity,
    CaptionViewState,
    ManualEditError,
    ManualParseError,
    ManualRevision,
    ReviewCue,
    StaleManualRevision,
    add_cue,
    allow_finding,
    content_fingerprint,
    edit_text,
    edit_time,
    initial_manual_revision,
    merge_cues,
    ms,
    parse_revision,
    record_no_speech,
    resolve_unplaced,
    serialize_revision,
    sha256_text,
    split_cue,
)

IDENTITY = AutoIdentity("a" * 64, "run-1", "f" * 64, "0" * 64)


def cue(cue_id: str, start: Fraction, end: Fraction, text: str) -> ReviewCue:
    return ReviewCue(
        cue_id=cue_id,
        start=start,
        end=end,
        text=text,
        text_basis=TEXT_BASIS_AUTO,
        auto_text=text,
        whisper_text=f"원문 {cue_id}",
        timing_segment_ids=(0,),
        timing_evidence_id="run-1:timing:0000",
        text_evidence_ids=("run-1:text:0000",),
        correction_status="whisper",
        flags=("retexted",),
    )


def baseline() -> ManualRevision:
    cues = (
        cue("a0000", Fraction(1, 2), Fraction(5, 2), "망원동 카페에 왔어요"),
        cue("a0001", Fraction(3), Fraction(5), "다음 장면"),
        cue("a0002", Fraction(6), Fraction(8), "마지막 장면"),
    )
    return initial_manual_revision(IDENTITY, cues, video_duration=Fraction(10), sample_count=160000)


# ─────────────────────────────────────────────────────────────
#  글자·시각 편집 — 시각은 절대 안 움직인다
# ─────────────────────────────────────────────────────────────


def test_text_edit_preserves_timing_exactly_and_old_revision_stays() -> None:
    """글자 편집 — 시각·근거 칸 그대로, 근거 표식만 manual. 예전 리비전은 무사하다."""
    parent = baseline()
    edited = edit_text(parent, "a0000", "망원동 커피숍에 왔어요")
    assert edited.sequence == 1 and edited.parent_revision == 0
    assert edited.cues[0].text == "망원동 커피숍에 왔어요"
    assert edited.cues[0].text_basis == TEXT_BASIS_MANUAL
    assert edited.cues[0].auto_text == "망원동 카페에 왔어요"  # 자동 채택 문구 보존
    assert (edited.cues[0].start, edited.cues[0].end) == (
        parent.cues[0].start,
        parent.cues[0].end,
    )  # 시각은 한 틱도 안 움직인다
    assert parent.cues[0].text == "망원동 카페에 왔어요"  # 예전 리비전 불변


def test_time_edit_takes_user_values_only_no_guesses() -> None:
    """시각 편집 — 유리수·밀리초 정수만. float 추측·음수·뒤집힘은 거부다."""
    parent = baseline()
    by_fraction = edit_time(parent, "a0001", start=Fraction(31, 10))
    assert by_fraction.cues[1].start == Fraction(31, 10)
    assert by_fraction.cues[1].end == parent.cues[1].end  # 못 받은 쪽은 그대로
    by_ms = edit_time(by_fraction, "a0001", end=ms(4250))
    assert by_ms.cues[1].end == Fraction(4250, 1000) == Fraction(17, 4)
    with pytest.raises(ManualEditError):
        edit_time(parent, "a0001", start=1.5)  # type: ignore[arg-type] — float는 못 쓴다
    with pytest.raises(ManualEditError):
        edit_time(parent, "a0001", start=Fraction(-1))
    with pytest.raises(ManualEditError):
        edit_time(parent, "a0001", start=Fraction(9), end=Fraction(2))


# ─────────────────────────────────────────────────────────────
#  분할·병합 — 명시적 값, 맞닿은 이웃만
# ─────────────────────────────────────────────────────────────


def test_split_requires_explicit_time_and_both_texts() -> None:
    """분할 — 나눈 시각·양쪽 글자 전부 사용자 제공. 균등 배분이 아니다(§11.7)."""
    parent = baseline()
    split = split_cue(parent, "a0000", ms(1500), "망원동", "카페에 왔어요")
    left, right = split.cues[0], split.cues[1]
    assert (left.cue_id, right.cue_id) == ("a0000", "m0001")
    assert (left.start, left.end) == (Fraction(1, 2), Fraction(3, 2))
    assert (right.start, right.end) == (Fraction(3, 2), Fraction(5, 2))
    assert left.text == "망원동" and right.text == "카페에 왔어요"
    assert right.text_basis == TEXT_BASIS_MANUAL and right.auto_text == parent.cues[0].auto_text
    with pytest.raises(ManualEditError):
        split_cue(parent, "a0000", ms(500), "망원동", "카페에 왔어요")  # 내부가 아니다
    with pytest.raises(ManualEditError):
        apply_split_missing_text(parent)


def apply_split_missing_text(parent: ManualRevision) -> ManualRevision:
    from vlogsrt.project.raw.caption_manual import ManualEdit, apply_edit

    edit = ManualEdit("split", parent.sequence, ("a0000",), text="망원동", at=ms(1500))
    return apply_edit(parent, edit)  # 오른쪽 글자 없는 분할 — 지어내면 안 된다


def test_merge_only_adjacent_neighbors_and_evidence_kept_text() -> None:
    """병합 — 맞닿은 이웃만. 글자는 사용자 제공 또는 근거 이어붙임이다."""
    parent = baseline()
    merged = merge_cues(parent, "a0000", "a0001")
    assert merged.cues[0].cue_id == "a0000"
    assert (merged.cues[0].start, merged.cues[0].end) == (Fraction(1, 2), Fraction(5))
    assert merged.cues[0].text == "망원동 카페에 왔어요 다음 장면"  # 근거 이어붙임
    assert merged.cues[0].whisper_text == "원문 a0000 원문 a0001"
    assert merged.cues[0].timing_segment_ids == (0, 0)
    explicit = merge_cues(parent, "a0000", "a0001", "사용자가 준 합친 글자")
    assert explicit.cues[0].text == "사용자가 준 합친 글자"
    assert explicit.cues[0].text_basis == TEXT_BASIS_MANUAL
    with pytest.raises(ManualEditError):
        merge_cues(parent, "a0000", "a0002")  # 이웃을 건너뛰는 병합은 없다


def test_add_cue_is_manual_basis_and_resolves_unplaced() -> None:
    """새 큐 — 근거 manual, ASR 증거 없음. 미배치 처리는 큐 또는 이유 중 하나."""
    parent = baseline()
    added = add_cue(parent, ms(9000), ms(9500), "사진을 찍었어요", note="미대응 말 살림")
    assert added.cues[-1].cue_id == "m0001"
    assert added.cues[-1].text_basis == TEXT_BASIS_MANUAL
    assert added.cues[-1].timing_evidence_id == ""  # 꾸며 붙인 ASR 증거가 없다
    resolved = resolve_unplaced(added, 0, "사진을 창고에 찍었어요", placed_cue_id="m0001")
    assert resolved.unplaced_resolutions[0].placed_cue_id == "m0001"
    dismissed = resolve_unplaced(parent, 0, "사진을 창고에 찍었어요", dismissed_reason="오인식이다")
    assert dismissed.unplaced_resolutions[0].dismissed_reason == "오인식이다"
    with pytest.raises(ManualEditError):
        resolve_unplaced(parent, 0, "사진", placed_cue_id="a0000", dismissed_reason="둘 다")
    with pytest.raises(ManualEditError):
        resolve_unplaced(parent, 0, "사진")  # 처리 없음


def test_no_speech_decision_only_on_empty_run() -> None:
    """무발화 확정은 빈 실행에만 — `no_audio`와 다른 갈래다(§11.11)."""
    parent = baseline()
    with pytest.raises(ManualEditError):
        record_no_speech(parent)
    empty = initial_manual_revision(IDENTITY, (), video_duration=Fraction(10), sample_count=160000)
    decided = record_no_speech(empty, note="무음 구간이다")
    assert decided.no_speech and decided.no_speech_note == "무음 구간이다"


def test_stale_edit_target_is_refused() -> None:
    """못박힌 대상만 고친다 — 앞선 편집 위로 늦은 완성이 덮지 못한다."""
    from vlogsrt.project.raw.caption_manual import ManualEdit, apply_edit

    parent = baseline()
    first = edit_text(parent, "a0000", "첫 편집")
    stale_edit = ManualEdit("text", parent.sequence, ("a0000",), text="늦은 편집")
    assert first.sequence == parent.sequence + 1
    with pytest.raises(StaleManualRevision):
        apply_edit(first, stale_edit)  # 리비전 0을 겨냥한 편집은 1에 못 붙는다


def test_parent_chain_hash_binds_to_parent_bytes() -> None:
    """리비전 1의 부모 해시는 리비전 0의 정준 바이트다 — 연쇄가 증명된다."""
    parent = baseline()
    child = edit_text(parent, "a0000", "고침")
    assert child.parent_sha256 == sha256_text(serialize_revision(parent))
    assert child.parent_sha256 != parent.parent_sha256  # 0은 스냅샷 내용 해시다
    assert parent.parent_sha256 == IDENTITY.auto_content_sha256


def test_allowance_and_resolution_accumulate_into_revision_content() -> None:
    """허용·처리는 그 리비전의 내용으로 축적된다 — 확정은 머리의 것만 본다."""
    parent = baseline()
    allowed = allow_finding(parent, "cue:a0000:reading_speed", "의도한 속도다")
    assert allowed.allowances[0].key == "cue:a0000:reading_speed"
    again = allow_finding(allowed, "run:repair_excluded:-", "접힌 되풀이를 확인했다")
    assert [item.key for item in again.allowances] == [
        "cue:a0000:reading_speed",
        "run:repair_excluded:-",
    ]


# ─────────────────────────────────────────────────────────────
#  직렬화 — 정준 왕복·엄격한 거부·보기 상태 배제
# ─────────────────────────────────────────────────────────────


def test_serialize_parse_roundtrip_is_exact() -> None:
    """정준 직렬화의 왕복은 정확히 같다 — 같은 내용은 같은 바이트다."""
    revision = edit_text(baseline(), "a0000", "고침")
    body = serialize_revision(revision)
    assert serialize_revision(parse_revision(body)) == body
    assert content_fingerprint(revision) == content_fingerprint(parse_revision(body))


def test_parse_rejects_unknown_schema_keys_types_and_ranges() -> None:
    """모르는 스키마·키·타입·범위 밖 값은 못 들어온다(§13)."""
    body = serialize_revision(edit_text(baseline(), "a0000", "고침"))
    with pytest.raises(ManualParseError):
        parse_revision(body.replace("raw-caption-manual/v1", "raw-caption-manual/v2"))
    with pytest.raises(ManualParseError):
        parse_revision(body.replace('"sequence":1', '"sequence":"one"'))
    with pytest.raises(ManualParseError):
        parse_revision(body.replace('"den":2', '"den":0'))  # 분모 0 유리수
    document = body.rstrip("\n")[:-1] + ',"extra":1}'
    with pytest.raises(ManualParseError):
        parse_revision(document + "\n")


def test_view_state_is_excluded_from_content_and_fingerprint() -> None:
    """보기 상태는 리비전이 아니다 — 지문이 흔들리지 않는다(§11.7)."""
    revision = baseline()
    before = content_fingerprint(revision)
    CaptionViewState(selected_cue_id="a0001", show_unplaced_only=True, query="카페")
    assert content_fingerprint(revision) == before
    assert "view" not in serialize_revision(revision)
