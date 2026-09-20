from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.workflow_protocol import (
    CaptionRequest,
    CaptionResult,
    CutCandidate,
    CutResult,
    EditPlan,
    ProjectRevision,
    RationalRange,
    WorkflowJournal,
    decode_message,
    encode_message,
)


def _revision(*, digest: str = "a" * 64) -> ProjectRevision:
    return ProjectRevision(
        input_sha256=digest,
        project_uid="project-42",
        project_name="Vlog 42",
        frame_duration=Fraction(1001, 30000),
        duration=Fraction(1801, 10),
    )


def _candidate(revision: ProjectRevision, *, confidence: str = "0.93") -> CutCandidate:
    return CutCandidate(
        candidate_id="silence-001",
        input_revision=revision.revision,
        clip_instance_id="clip-001",
        source_range=RationalRange(Fraction(3), Fraction(5)),
        timeline_range=RationalRange(Fraction(13), Fraction(15)),
        reason="internal_silence",
        evidence=("vad:no-speech", "transcript:empty"),
        confidence=confidence,
    )


def test_jsonl_protocol_round_trips_every_public_message_deterministically(tmp_path: Path) -> None:
    """필드 순서나 locale이 바뀌어 같은 응답의 JSONL 바이트가 달라지는 회귀를 잡는다."""
    revision = _revision()
    candidate = _candidate(revision)
    plan = EditPlan.create(
        input_revision=revision.revision,
        approved_candidate_ids=(candidate.candidate_id,),
        rejected_candidate_ids=("silence-002",),
        expected_output_duration=Fraction(1781, 10),
    )
    messages = (
        revision,
        candidate,
        plan,
        CutResult(
            plan_revision=plan.plan_revision,
            input_revision=revision.revision,
            output_fcpxml=tmp_path / "rough.fcpxml",
            review_report=tmp_path / "review.json",
        ),
        CaptionRequest(
            origin_revision=revision.revision,
            final_revision="b" * 64,
            render_audio=tmp_path / "mix.wav",
            final_fcpxml=tmp_path / "final.fcpxml",
        ),
        CaptionResult(
            final_revision="b" * 64,
            srt_path=tmp_path / "Vlog 42.bbbbbbbbbbbb.ko.srt",
            warnings=("coverage:complete",),
            quality=("timing:pass",),
            speech_status="speech",
        ),
    )

    first = [encode_message(message) for message in messages]
    second = [encode_message(message) for message in messages]

    assert first == second
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in first)
    assert [decode_message(line) for line in first] == list(messages)
    assert '"protocol_version":"1.0"' in first[0]


def test_project_and_plan_revisions_bind_all_semantic_inputs() -> None:
    """XML·UID·승인 목록이 바뀌었는데 낡은 리비전을 재사용하는 버그를 잡는다."""
    original = _revision()
    changed_xml = _revision(digest="b" * 64)
    same_again = _revision()
    assert original.revision == same_again.revision
    assert original.revision != changed_xml.revision

    first = EditPlan.create(
        input_revision=original.revision,
        approved_candidate_ids=("b", "a", "a"),
        rejected_candidate_ids=("c",),
        expected_output_duration=Fraction(100),
    )
    same = EditPlan.create(
        input_revision=original.revision,
        approved_candidate_ids=("a", "b"),
        rejected_candidate_ids=("c",),
        expected_output_duration=Fraction(100),
    )
    changed = EditPlan.create(
        input_revision=original.revision,
        approved_candidate_ids=("a",),
        rejected_candidate_ids=("b", "c"),
        expected_output_duration=Fraction(101),
    )
    assert first == same
    assert first.plan_revision == same.plan_revision
    assert first.plan_revision != changed.plan_revision
    assert first.approved_candidate_ids == ("a", "b")


def test_candidate_default_selection_uses_decimal_threshold_without_float_rounding() -> None:
    """0.90 미만 후보가 float 반올림 탓에 기본 선택되는 안전성 회귀를 잡는다."""
    revision = _revision()
    assert _candidate(revision, confidence="0.90").selected_by_default is True
    assert _candidate(revision, confidence="0.8999999999999999").selected_by_default is False


def test_journal_only_accepts_forward_transitions_for_its_bound_revision(tmp_path: Path) -> None:
    """오래된 분석·승인이 새 입력 워크플로에 붙거나 사람 게이트를 건너뛰는 버그를 잡는다."""
    revision = _revision()
    journal = WorkflowJournal.create(tmp_path / "workflow.jsonl", revision)
    assert journal.state == "received"

    journal.transition(revision.revision, "analyzing")
    journal.transition(revision.revision, "review_required")
    reloaded = WorkflowJournal.open(journal.path)
    assert reloaded.state == "review_required"

    with pytest.raises(ValueError, match="리비전"):
        reloaded.transition(_revision(digest="c" * 64).revision, "roughcut_imported")
    with pytest.raises(ValueError, match="허용되지 않는 상태 전이"):
        reloaded.transition(revision.revision, "caption_requested")
    assert WorkflowJournal.open(journal.path).state == "review_required"

    reloaded.transition(revision.revision, "roughcut_imported")
    reloaded.transition(revision.revision, "caption_requested")
    reloaded.transition(revision.revision, "caption_ready")
    assert WorkflowJournal.open(journal.path).state == "caption_ready"

    lines = journal.path.read_bytes().splitlines(keepends=True)
    assert all(hashlib.sha256(line).digest() for line in lines)
    assert len(lines) == 6


def test_protocol_rejects_unknown_versions_and_overlapping_approval_sets() -> None:
    """알 수 없는 앱 계약과 승인·거절 동시 표기가 조용히 통과하는 버그를 잡는다."""
    with pytest.raises(ValueError, match="지원하지 않는 프로토콜"):
        decode_message('{"protocol_version":"9.0","type":"ProjectRevision"}\n')
    with pytest.raises(ValueError, match="승인과 거절"):
        EditPlan.create(
            input_revision=_revision().revision,
            approved_candidate_ids=("same",),
            rejected_candidate_ids=("same",),
            expected_output_duration=Fraction(1),
        )


def test_an_empty_review_is_a_valid_noop_plan() -> None:
    """후보가 없는 안전한 프로젝트를 오류로 만들어 상태 기계를 멈추는 회귀를 잡는다."""
    plan = EditPlan.create(
        input_revision=_revision().revision,
        approved_candidate_ids=(),
        rejected_candidate_ids=(),
        expected_output_duration=Fraction(10),
    )
    assert plan.approved_candidate_ids == plan.rejected_candidate_ids == ()
