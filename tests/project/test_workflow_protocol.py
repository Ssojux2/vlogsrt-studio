from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.pricing import official_pricing_snapshot
from vlogsrt.project.workflow_protocol import (
    ANALYSIS_PROTOCOL_VERSION,
    CAPTION_PROTOCOL_VERSION,
    LEGACY_ANALYSIS_PROTOCOL_VERSION,
    PRIOR_ANALYSIS_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    ActualUsage,
    AnalysisAttempt,
    AnalysisAuthorization,
    AnalysisEstimate,
    AnalysisResult,
    CancellationRequest,
    CandidatePreview,
    CaptionAttempt,
    CaptionAuthorization,
    CaptionEstimate,
    CaptionJournal,
    CaptionRequest,
    CaptionResult,
    CaptionStateEvent,
    CutCandidate,
    CutResult,
    EditPlan,
    PricingSnapshot,
    ProjectRevision,
    ProjectSelection,
    RationalRange,
    UpstreamStages,
    VisionWindowDecision,
    WorkflowJournal,
    WorkflowStateEvent,
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
        max_image_tokens=42 * 308,
        cost_upper_bound="1.25",
        settings_sha256="e" * 64,
        pricing_snapshot_revision=official_pricing_snapshot().snapshot_revision,
        media_grant_revision="9" * 64,
    )


def test_jsonl_protocol_round_trips_every_public_message_deterministically(tmp_path: Path) -> None:
    """필드 순서나 locale이 바뀌어 같은 응답의 JSONL 바이트가 달라지는 회귀를 잡는다."""
    revision = _revision()
    pricing = official_pricing_snapshot()
    candidate = _candidate(revision)
    plan = EditPlan.create(
        input_revision=revision.revision,
        approved_candidate_ids=(candidate.candidate_id,),
        approved_decision_revisions=((candidate.candidate_id, candidate.decision_revision),),
        rejected_candidate_ids=("silence-002",),
        expected_output_duration=Fraction(1781, 10),
    )
    caption_request = CaptionRequest(
        origin_revision=revision.revision,
        final_revision="b" * 64,
        render_audio=tmp_path / "mix.wav",
        render_audio_sha256="c" * 64,
        final_fcpxml=tmp_path / "final.fcpxml",
    )
    caption_estimate = CaptionEstimate.create(
        request_revision=caption_request.request_revision,
        origin_revision=caption_request.origin_revision,
        final_revision=caption_request.final_revision,
        render_audio_sha256=caption_request.render_audio_sha256,
        audio_seconds=Fraction(1801, 10),
        transcription_seconds=Fraction(1801, 5),
        cost_upper_bound="0.0543",
        pricing_snapshot_revision=pricing.snapshot_revision,
        settings_sha256="2" * 64,
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
        pricing,
        _estimate(revision),
        AnalysisAuthorization(
            input_revision=revision.revision,
            estimate_revision=_estimate(revision).estimate_revision,
            pricing_snapshot_revision=pricing.snapshot_revision,
            media_grant_revision="9" * 64,
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
        AnalysisResult.create(
            input_revision=revision.revision,
            attempt_id="attempt-001",
            estimate_revision=_estimate(revision).estimate_revision,
            status="success",
            windows_total=2,
            windows_successful=2,
            windows_failed=0,
            windows_skipped=0,
            candidate_count=1,
            upstream=UpstreamStages(
                audio_total=1,
                audio_failed=0,
                transcription_total=1,
                transcription_failed=0,
                vad_total=1,
                vad_failed=0,
            ),
            reason_codes=(),
        ),
        CancellationRequest.create(
            input_revision=revision.revision,
            attempt_id="attempt-001",
            requested_at="2026-09-21T04:01:00Z",
            reason="user",
        ),
        ActualUsage.create(
            input_revision=revision.revision,
            attempt_id="attempt-001",
            window_id="window-001",
            model=pricing.luna_model,
            input_tokens=3600,
            output_tokens=90,
            cost="0.000819",
        ),
        ActualUsage.create(
            input_revision=revision.revision,
            attempt_id="attempt-001",
            window_id=None,
            model=pricing.whisper_model,
            input_tokens=0,
            output_tokens=0,
            audio_seconds=Fraction(121, 2),
            cost="0.00605",
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
        CandidatePreview.create(
            candidate_id="a" * 64,
            input_revision=revision.revision,
            frame_id="f-001",
            thumbnail_path=tmp_path / "previews" / "silence-001.jpg",
            thumbnail_sha256="7" * 64,
            reason=candidate.reason,
            confidence="0.93",
            removal_duration=Fraction(2),
        ),
        plan,
        CutResult(
            plan_revision=plan.plan_revision,
            input_revision=revision.revision,
            output_fcpxml=tmp_path / "rough.fcpxml",
            review_report=tmp_path / "review.json",
        ),
        caption_request,
        caption_estimate,
        CaptionAuthorization(
            request_revision=caption_request.request_revision,
            estimate_revision=caption_estimate.estimate_revision,
            pricing_snapshot_revision=pricing.snapshot_revision,
            authorized_at="2026-09-21T05:00:00Z",
        ),
        CaptionResult(
            final_revision="b" * 64,
            srt_path=tmp_path / "Vlog 42.bbbbbbbbbbbb.ko.srt",
            warnings=("coverage:complete",),
            quality=("timing:pass",),
            speech_status="speech",
        ),
        CaptionAttempt(
            request_revision=caption_request.request_revision,
            estimate_revision=caption_estimate.estimate_revision,
            attempt_id="caption-attempt-001",
            status="failed",
            started_at="2026-09-21T05:01:00Z",
            error="전사 실패",
        ),
        CaptionStateEvent(
            request_revision=caption_request.request_revision,
            state="caption_ready",
        ),
    )

    first = [encode_message(message) for message in messages]
    second = [encode_message(message) for message in messages]

    assert first == second
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in first)
    assert [decode_message(line) for line in first] == list(messages)
    assert f'"protocol_version":"{PROTOCOL_VERSION}"' in first[0]
    assert f'"protocol_version":"{ANALYSIS_PROTOCOL_VERSION}"' in first[3]
    assert f'"protocol_version":"{CAPTION_PROTOCOL_VERSION}"' in first[-1]


def test_analysis_13_reads_legacy_versions_but_rejects_stale_authority() -> None:
    """입력/추정/가격/권한 드리프트와 모델의 복수·미지 후보가 삭제 권한으로 바뀌는 회귀를 잡는다."""
    revision = _revision()
    pricing = official_pricing_snapshot()
    estimate = _estimate(revision)
    authorization = AnalysisAuthorization(
        input_revision=revision.revision,
        estimate_revision=estimate.estimate_revision,
        pricing_snapshot_revision=pricing.snapshot_revision,
        media_grant_revision=estimate.media_grant_revision,
        authorized_at="2026-09-21T04:00:00Z",
    )
    authorization.require_matches(
        revision.revision,
        estimate.estimate_revision,
        pricing.snapshot_revision,
        estimate.media_grant_revision,
    )
    with pytest.raises(ValueError, match="입력 리비전"):
        authorization.require_matches(
            "b" * 64, estimate.estimate_revision, pricing.snapshot_revision, "9" * 64
        )
    with pytest.raises(ValueError, match="추정 리비전"):
        authorization.require_matches(
            revision.revision, "c" * 64, pricing.snapshot_revision, "9" * 64
        )
    with pytest.raises(ValueError, match="가격 스냅샷"):
        authorization.require_matches(
            revision.revision, estimate.estimate_revision, "8" * 64, "9" * 64
        )
    with pytest.raises(ValueError, match="미디어 권한"):
        authorization.require_matches(
            revision.revision, estimate.estimate_revision, pricing.snapshot_revision, "7" * 64
        )

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
    with pytest.raises(ValueError, match="근거 프레임"):
        VisionWindowDecision.create(
            input_revision=revision.revision,
            window_id="window-001",
            choice="propose_cut",
            candidate_id="silence-001",
            evidence_frame_ids=(),
            confidence="0.99",
            response_sha256="f" * 64,
        )

    for legacy in (PRIOR_ANALYSIS_PROTOCOL_VERSION, LEGACY_ANALYSIS_PROTOCOL_VERSION):
        # 낡은 분석 기록은 읽는다 — 후보 페이로드는 그대로다(S1B). 단 판정은
        # 결과 상태가 없어 legacy/unknown로만 읽힌다(위 재생 시험이 닫는다).
        stale = encode_message(_candidate(revision)).replace(
            f'"protocol_version":"{ANALYSIS_PROTOCOL_VERSION}"',
            f'"protocol_version":"{legacy}"',
        )
        assert decode_message(stale) == _candidate(revision)
    for rejected in (PROTOCOL_VERSION, "9.0"):
        stale = encode_message(_candidate(revision)).replace(
            f'"protocol_version":"{ANALYSIS_PROTOCOL_VERSION}"',
            f'"protocol_version":"{rejected}"',
        )
        with pytest.raises(ValueError, match="프로토콜"):
            decode_message(stale)


def test_pricing_snapshot_revision_binds_official_prices_and_models() -> None:
    """같은 가격은 같은 리비전, 하나라도 바뀌면 다른 리비전 — 승인 재사용을 끊는다."""
    base = official_pricing_snapshot()
    assert base.currency == "USD"
    assert base.processing_region == "global"
    assert base.service_tier == "standard"
    assert base.whisper_per_minute == "0.006"
    assert base.gpt_transcribe_per_minute == "0.0045"
    assert base.luna_input_per_million_tokens == "0.2"
    assert base.luna_output_per_million_tokens == "1.2"
    # 공식 문서 URL은 사용자 계획의 정확한 네 개 — 하나라도 바뀌면 스냅샷 리비전이
    # 바뀌어야 한다(아래 repriced 단언과 함께 이 계약을 못 박는다).
    assert base.documentation_urls == (
        "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        "https://developers.openai.com/api/docs/pricing",
        "https://developers.openai.com/api/docs/models/whisper-1",
        "https://developers.openai.com/api/docs/guides/images-vision",
    )
    assert base.snapshot_revision == official_pricing_snapshot().snapshot_revision
    assert (
        PricingSnapshot.create(
            effective_date=base.effective_date,
            currency=base.currency,
            processing_region=base.processing_region,
            service_tier=base.service_tier,
            luna_model=base.luna_model,
            luna_input_per_million_tokens=base.luna_input_per_million_tokens,
            luna_output_per_million_tokens=base.luna_output_per_million_tokens,
            whisper_model=base.whisper_model,
            whisper_per_minute=base.whisper_per_minute,
            gpt_transcribe_model=base.gpt_transcribe_model,
            gpt_transcribe_per_minute=base.gpt_transcribe_per_minute,
            documentation_urls=base.documentation_urls,
        )
        == base
    )
    repriced = PricingSnapshot.create(
        effective_date=base.effective_date,
        currency=base.currency,
        processing_region=base.processing_region,
        service_tier=base.service_tier,
        luna_model=base.luna_model,
        luna_input_per_million_tokens="0.30",
        luna_output_per_million_tokens=base.luna_output_per_million_tokens,
        whisper_model=base.whisper_model,
        whisper_per_minute=base.whisper_per_minute,
        gpt_transcribe_model=base.gpt_transcribe_model,
        gpt_transcribe_per_minute=base.gpt_transcribe_per_minute,
        documentation_urls=base.documentation_urls,
    )
    assert repriced.snapshot_revision != base.snapshot_revision
    with pytest.raises(ValueError, match="문서 URL"):
        PricingSnapshot.create(
            effective_date=base.effective_date,
            currency=base.currency,
            processing_region=base.processing_region,
            service_tier=base.service_tier,
            luna_model=base.luna_model,
            luna_input_per_million_tokens=base.luna_input_per_million_tokens,
            luna_output_per_million_tokens=base.luna_output_per_million_tokens,
            whisper_model=base.whisper_model,
            whisper_per_minute=base.whisper_per_minute,
            gpt_transcribe_model=base.gpt_transcribe_model,
            gpt_transcribe_per_minute=base.gpt_transcribe_per_minute,
            documentation_urls=(),
        )


def test_cancellation_and_usage_revisions_reject_tampered_records() -> None:
    """취소·사용량 기록의 리비전 계산이 드리프트를 못 잡으면 승인 상한이 무력해진다."""
    revision = _revision()
    cancellation = CancellationRequest.create(
        input_revision=revision.revision,
        attempt_id="attempt-001",
        requested_at="2026-09-21T04:01:00Z",
        reason="active_project_changed",
    )
    forged = encode_message(cancellation).replace("attempt-001", "attempt-002")
    with pytest.raises(ValueError, match="계산 리비전"):
        decode_message(forged)
    with pytest.raises(ValueError, match="취소 사유"):
        CancellationRequest.create(
            input_revision=revision.revision,
            attempt_id="attempt-001",
            requested_at="2026-09-21T04:01:00Z",
            reason="budget",
        )

    usage = ActualUsage.create(
        input_revision=revision.revision,
        attempt_id="attempt-001",
        window_id="window-001",
        model="gpt-5.6-luna",
        input_tokens=1000,
        output_tokens=256,
        cost="0.000456",
    )
    inflated = encode_message(usage).replace('"input_tokens":1000', '"input_tokens":9000')
    with pytest.raises(ValueError, match="계산 리비전"):
        decode_message(inflated)
    with pytest.raises(ValueError, match="0 이상"):
        ActualUsage.create(
            input_revision=revision.revision,
            attempt_id="attempt-001",
            window_id=None,
            model="gpt-5.6-luna",
            input_tokens=1,
            output_tokens=1,
            cost="-0.01",
        )


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


def test_journal_walks_the_authorized_state_machine_and_stops_at_terminals(
    tmp_path: Path,
) -> None:
    """오래된 분석·승인이 새 입력 워크플로에 붙거나 사람 게이트를 건너뛰는 버그를 잡는다."""
    revision = _revision()
    journal = WorkflowJournal.create(tmp_path / "workflow.jsonl", revision)
    assert journal.state == "received"

    journal.transition(revision.revision, "estimate_ready")
    journal.transition(revision.revision, "authorized")
    journal.transition(revision.revision, "analyzing")
    journal.transition(revision.revision, "review_required")
    reloaded = WorkflowJournal.open(journal.path)
    assert reloaded.state == "review_required"

    with pytest.raises(ValueError, match="리비전"):
        reloaded.transition(_revision(digest="c" * 64).revision, "roughcut_generated")
    with pytest.raises(ValueError, match="허용되지 않는 상태 전이"):
        # 낡은 이름은 이제 새 쓰기로 못 박지 않는다(S2C) — 기록은 읽기 호환일 뿐.
        reloaded.transition(revision.revision, "roughcut_imported")
    with pytest.raises(ValueError, match="허용되지 않는 상태 전이"):
        reloaded.transition(revision.revision, "caption_received")
    with pytest.raises(ValueError, match="허용되지 않는 상태 전이"):
        reloaded.transition(revision.revision, "estimate_ready")
    assert WorkflowJournal.open(journal.path).state == "review_required"

    reloaded.transition(revision.revision, "roughcut_generated")
    reloaded.transition(revision.revision, "caption_received")
    reloaded.transition(revision.revision, "caption_estimate_ready")
    reloaded.transition(revision.revision, "caption_authorized")
    reloaded.transition(revision.revision, "caption_ready")
    assert WorkflowJournal.open(journal.path).state == "caption_ready"

    lines = journal.path.read_bytes().splitlines(keepends=True)
    assert all(hashlib.sha256(line).digest() for line in lines)
    assert len(lines) == 10


def test_workflow_journal_reads_legacy_roughcut_imported_and_continues(
    tmp_path: Path,
) -> None:
    """S2C 읽기 호환 — 낡은 저널의 roughcut_imported는 재생되고 그 뒤는 이어진다."""
    revision = _revision()
    journal = WorkflowJournal.create(tmp_path / "workflow.jsonl", revision)
    for state in ("estimate_ready", "authorized", "analyzing", "review_required"):
        journal.transition(revision.revision, state)
    with (tmp_path / "workflow.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(encode_message(WorkflowStateEvent(revision.revision, "roughcut_imported")))

    reloaded = WorkflowJournal.open(tmp_path / "workflow.jsonl")
    assert reloaded.state == "roughcut_imported"
    reloaded.transition(revision.revision, "caption_received")
    assert WorkflowJournal.open(tmp_path / "workflow.jsonl").state == "caption_received"


def test_journal_accepts_terminal_states_from_running_states_only_as_endpoints(
    tmp_path: Path,
) -> None:
    """실행 상태에서 cancelled|failed로 닫히고, 종료 뒤에는 어떤 전이도 붙지 않는다."""
    revision = _revision()
    journal = WorkflowJournal.create(tmp_path / "workflow.jsonl", revision)
    journal.transition(revision.revision, "estimate_ready")
    journal.transition(revision.revision, "authorized")
    journal.transition(revision.revision, "analyzing")
    journal.transition(revision.revision, "cancelled")
    assert WorkflowJournal.open(journal.path).state == "cancelled"
    with pytest.raises(ValueError, match="종료 상태"):
        journal.transition(revision.revision, "review_required")
    with pytest.raises(ValueError, match="종료 상태"):
        journal.transition(revision.revision, "failed")

    failed = WorkflowJournal.create(tmp_path / "failed.jsonl", revision)
    failed.transition(revision.revision, "estimate_ready")
    failed.transition(revision.revision, "authorized")
    failed.transition(revision.revision, "failed")
    assert WorkflowJournal.open(failed.path).state == "failed"

    early = WorkflowJournal.create(tmp_path / "early.jsonl", revision)
    early.transition(revision.revision, "cancelled")
    assert WorkflowJournal.open(early.path).state == "cancelled"


def test_two_open_journals_cannot_append_the_same_transition(tmp_path: Path) -> None:
    """두 프로세스의 낡은 cached state가 중복 전이를 써 원장을 깨뜨리는 회귀를 잡는다."""
    revision = _revision()
    first = WorkflowJournal.create(tmp_path / "workflow.jsonl", revision)
    stale = WorkflowJournal.open(first.path)
    first.transition(revision.revision, "estimate_ready")
    with pytest.raises(ValueError, match="허용되지 않는 상태 전이"):
        stale.transition(revision.revision, "estimate_ready")
    assert WorkflowJournal.open(first.path).state == "estimate_ready"


def _caption_request(tmp_path: Path) -> CaptionRequest:
    return CaptionRequest(
        origin_revision="a" * 64,
        final_revision="b" * 64,
        render_audio=tmp_path / "mix.wav",
        render_audio_sha256="c" * 64,
        final_fcpxml=tmp_path / "final.fcpxml",
    )


def test_caption_journal_keeps_failed_attempts_retryable_under_one_request(
    tmp_path: Path,
) -> None:
    """자막 job 원장(S4A) — 시도 실패로 종료하지 않고 같은 요청·승인으로 재시도한다."""
    request = _caption_request(tmp_path)
    journal = CaptionJournal.create(tmp_path / "caption.jsonl", request)
    assert journal.state == "caption_received"

    journal.transition(request.request_revision, "caption_estimate_ready")
    journal.transition(request.request_revision, "caption_authorized")
    # 실패한 시도가 원장을 종료 상태로 옮기지 않는다 — 여기가 재시도의 문이다.
    assert CaptionJournal.open(journal.path).state == "caption_authorized"
    journal.transition(request.request_revision, "caption_ready")
    assert CaptionJournal.open(journal.path).state == "caption_ready"
    # 완료 상태는 종료가 아니지만 뒤로는 못 돌아간다 — 재실행은 새 job이다.
    with pytest.raises(ValueError, match="허용되지 않는 상태 전이"):
        journal.transition(request.request_revision, "caption_authorized")

    # 원장이 이미 있으면 배타 생성으로 거부다 — 승인 뒤 조용히 새 원장이 열리지 않는다.
    with pytest.raises(ValueError, match="이미 있다"):
        CaptionJournal.create(tmp_path / "caption.jsonl", request)

    # 다른 요청 리비전의 상태는 섞이지 않는다.
    with pytest.raises(ValueError, match="리비전"):
        journal.transition("d" * 64, "caption_ready")

    # 취소는 종료 상태다 — 어떤 실행 상태에서도 닫히고 그 뒤는 없다.
    cancelled = CaptionJournal.create(tmp_path / "cancelled.jsonl", request)
    cancelled.transition(request.request_revision, "caption_estimate_ready")
    cancelled.transition(request.request_revision, "cancelled")
    assert CaptionJournal.open(cancelled.path).state == "cancelled"
    with pytest.raises(ValueError, match="종료 상태"):
        cancelled.transition(request.request_revision, "caption_authorized")

    # 상태 줄이 아닌 기록은 자막 원장이 아니다 — 러프컷 상태와 자리가 다르다.
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text(encode_message(request) + encode_message(request), encoding="utf-8")
    with pytest.raises(ValueError, match="CaptionStateEvent"):
        CaptionJournal.open(foreign)

    # 러프컷 상태는 자막 원장에 못 들어간다 — 만들 때도, 읽을 때도 같은 규칙이다.
    with pytest.raises(ValueError, match="없는 상태"):
        CaptionStateEvent(request_revision=request.request_revision, state="received")
    valid_event = encode_message(
        CaptionStateEvent(request_revision=request.request_revision, state="caption_ready")
    )
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        encode_message(request) + valid_event.replace('"caption_ready"', '"received"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="없는 상태"):
        CaptionJournal.open(mixed)


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


def test_candidate_preview_binds_thumbnail_and_removal_duration(tmp_path: Path) -> None:
    """미리보기가 다른 후보의 썸네일이나 낡은 제거 길이를 가리키는 회귀를 잡는다."""
    revision = _revision()
    preview = CandidatePreview.create(
        candidate_id="a" * 64,
        input_revision=revision.revision,
        frame_id="f-001",
        thumbnail_path=tmp_path / "silence-001.jpg",
        thumbnail_sha256="7" * 64,
        reason="internal_silence",
        confidence="0.93",
        removal_duration=Fraction(2),
    )
    assert decode_message(encode_message(preview)) == preview
    swapped = encode_message(preview).replace(
        '"removal_duration":"2/1"', '"removal_duration":"9/1"'
    )
    with pytest.raises(ValueError, match="계산 리비전"):
        decode_message(swapped)
    with pytest.raises(ValueError, match="양수"):
        CandidatePreview.create(
            candidate_id="a" * 64,
            input_revision=revision.revision,
            frame_id="f-001",
            thumbnail_path=tmp_path / "silence-001.jpg",
            thumbnail_sha256="7" * 64,
            reason="internal_silence",
            confidence="0.93",
            removal_duration=Fraction(0),
        )
