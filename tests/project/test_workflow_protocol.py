from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.workflow_protocol import (
    AnalysisAttempt,
    AnalysisAuthorization,
    AnalysisEstimate,
    CaptionRequest,
    CaptionResult,
    CutCandidate,
    CutResult,
    EditPlan,
    ProjectRevision,
    ProjectSelection,
    RationalRange,
    VisionWindowDecision,
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
        window_id="window-001",
        decision_revision="d" * 64,
    )


def _estimate(revision: ProjectRevision) -> AnalysisEstimate:
    return AnalysisEstimate.create(
        input_revision=revision.revision,
        model="gpt-5.6-luna",
        unique_source_audio_seconds=Fraction(121, 2),
        transcription_seconds=Fraction(121, 2),
        max_windows=7,
        max_images=42,
        max_image_tokens=3570,
        cost_upper_bound="1.25",
        settings_sha256="e" * 64,
    )


def test_jsonl_protocol_round_trips_every_public_message_deterministically(tmp_path: Path) -> None:
    """필드 순서나 locale이 바뀌어 같은 응답의 JSONL 바이트가 달라지는 회귀를 잡는다."""
    revision = _revision()
    candidate = _candidate(revision)
    plan = EditPlan.create(
        input_revision=revision.revision,
        approved_candidate_ids=(candidate.candidate_id,),
        approved_decision_revisions=((candidate.candidate_id, candidate.decision_revision),),
        rejected_candidate_ids=("silence-002",),
        expected_output_duration=Fraction(1781, 10),
    )
    messages = (
        revision,
        ProjectSelection(
            project_uid=revision.project_uid,
            project_name=revision.project_name,
            timeline_start=Fraction(3600),
            duration=revision.duration,
            frame_duration=revision.frame_duration,
        ),
        _estimate(revision),
        AnalysisAuthorization(
            input_revision=revision.revision,
            estimate_revision=_estimate(revision).estimate_revision,
            authorized_at="2026-09-21T04:00:00Z",
        ),
        AnalysisAttempt(
            input_revision=revision.revision,
            estimate_revision=_estimate(revision).estimate_revision,
            attempt_id="attempt-001",
            status="running",
            started_at="2026-09-21T04:00:01Z",
            error=None,
        ),
        VisionWindowDecision.create(
            input_revision=revision.revision,
            window_id="window-001",
            choice="propose_cut",
            candidate_id=candidate.candidate_id,
            evidence_frame_ids=("f-001", "f-002"),
            confidence="0.93",
            response_sha256="f" * 64,
        ),
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
            render_audio_sha256="c" * 64,
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
    assert '"protocol_version":"1.1"' in first[1]
    assert '"protocol_version":"1.0"' in first[-1]


def test_analysis_11_rejects_stale_or_malformed_authority_and_decisions() -> None:
    """입력/추정 드리프트와 모델의 복수·미지 후보가 삭제 권한으로 바뀌는 회귀를 잡는다."""
    revision = _revision()
    estimate = _estimate(revision)
    authorization = AnalysisAuthorization(
        input_revision=revision.revision,
        estimate_revision=estimate.estimate_revision,
        authorized_at="2026-09-21T04:00:00Z",
    )
    authorization.require_matches(revision.revision, estimate.estimate_revision)
    with pytest.raises(ValueError, match="입력 리비전"):
        authorization.require_matches("b" * 64, estimate.estimate_revision)
    with pytest.raises(ValueError, match="추정 리비전"):
        authorization.require_matches(revision.revision, "c" * 64)

    keep = VisionWindowDecision.create(
        input_revision=revision.revision,
        window_id="window-001",
        choice="keep",
        candidate_id=None,
        evidence_frame_ids=(),
        confidence="0",
        response_sha256="f" * 64,
    )
    assert keep.choice == "keep"
    with pytest.raises(ValueError, match="keep"):
        VisionWindowDecision.create(
            input_revision=revision.revision,
            window_id="window-001",
            choice="keep",
            candidate_id="unexpected",
            evidence_frame_ids=(),
            confidence="0.5",
            response_sha256="f" * 64,
        )

    old_candidate = encode_message(_candidate(revision)).replace(
        '"protocol_version":"1.1"', '"protocol_version":"1.0"'
    )
    with pytest.raises(ValueError, match="프로토콜"):
        decode_message(old_candidate)


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
        approved_decision_revisions=(("a", "d" * 64), ("b", "e" * 64)),
        rejected_candidate_ids=("c",),
        expected_output_duration=Fraction(100),
    )
    same = EditPlan.create(
        input_revision=original.revision,
        approved_candidate_ids=("a", "b"),
        approved_decision_revisions=(("b", "e" * 64), ("a", "d" * 64)),
        rejected_candidate_ids=("c",),
        expected_output_duration=Fraction(100),
    )
    changed = EditPlan.create(
        input_revision=original.revision,
        approved_candidate_ids=("a",),
        approved_decision_revisions=(("a", "f" * 64),),
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


def test_two_open_journals_cannot_append_the_same_transition(tmp_path: Path) -> None:
    """두 프로세스의 낡은 cached state가 중복 전이를 써 원장을 깨뜨리는 회귀를 잡는다."""
    revision = _revision()
    first = WorkflowJournal.create(tmp_path / "workflow.jsonl", revision)
    stale = WorkflowJournal.open(first.path)
    first.transition(revision.revision, "analyzing")
    with pytest.raises(ValueError, match="허용되지 않는 상태 전이"):
        stale.transition(revision.revision, "analyzing")
    assert WorkflowJournal.open(first.path).state == "analyzing"


def test_protocol_rejects_unknown_versions_and_overlapping_approval_sets() -> None:
    """알 수 없는 앱 계약과 승인·거절 동시 표기가 조용히 통과하는 버그를 잡는다."""
    with pytest.raises(ValueError, match="지원하지 않는 프로토콜"):
        decode_message('{"protocol_version":"9.0","type":"ProjectRevision"}\n')
    with pytest.raises(ValueError, match="승인과 거절"):
        EditPlan.create(
            input_revision=_revision().revision,
            approved_candidate_ids=("same",),
            approved_decision_revisions=(("same", "d" * 64),),
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
