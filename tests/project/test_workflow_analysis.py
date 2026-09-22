from __future__ import annotations

import base64
import hashlib
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.models import AssetResource, ClipInstance, MediaRep, ProjectDocument
from vlogsrt.project.pricing import official_pricing_snapshot
from vlogsrt.project.workflow_analysis import (
    AnalysisSettings,
    CandidateSeed,
    CostCeilingExceeded,
    LunaFrame,
    LunaWindow,
    TranscriptEvidence,
    UsageLedger,
    build_analysis_estimate,
    build_candidate_preview,
    build_luna_payload,
    effective_analysis_settings_sha256,
    eligible_transcript_evidence,
    finalize_candidate,
    luna_cache_key,
    luna_request_sha256,
    luna_usage,
    normalize_luna_response,
    plan_candidate_windows,
    project_transcript_evidence,
    safe_transcript_candidates,
    select_frame_times,
    transcription_usage,
)
from vlogsrt.project.workflow_protocol import CutCandidate, ProjectRevision, RationalRange


def _document() -> tuple[ProjectDocument, ProjectRevision]:
    digest = "a" * 64
    resources = (
        AssetResource(
            xml_id="r2",
            scoped_id="source-a",
            uid="A",
            name="A.mov",
            format_id="r1",
            frame_duration=Fraction(1, 30),
            start=Fraction(0),
            duration=Fraction(21),
            has_audio=True,
            media_reps=(MediaRep("original", "file:///A.mov"),),
        ),
        AssetResource(
            xml_id="r3",
            scoped_id="source-a-duplicate",
            uid="A",
            name="A duplicate",
            format_id="r1",
            frame_duration=Fraction(1, 30),
            start=Fraction(0),
            duration=Fraction(21),
            has_audio=True,
            media_reps=(MediaRep("original", "file:///A.mov"),),
        ),
        AssetResource(
            xml_id="r4",
            scoped_id="source-b",
            uid="B",
            name="B.mov",
            format_id="r1",
            frame_duration=Fraction(1, 30),
            start=Fraction(0),
            duration=Fraction(9),
            has_audio=True,
            media_reps=(MediaRep("original", "file:///B.mov"),),
        ),
    )
    storyline = (
        ClipInstance(
            "clip-a", "/a", "source-a", Fraction(0), Fraction(0), Fraction(21), True, "all"
        ),
        ClipInstance(
            "clip-b", "/b", "source-b", Fraction(21), Fraction(0), Fraction(9), True, "all"
        ),
    )
    document = ProjectDocument(
        input_path=Path("/tmp/in.fcpxml"),
        info_path=Path("/tmp/in.fcpxml"),
        input_sha256=digest,
        raw_xml=b"<fcpxml/>",
        version="1.14",
        sequence_tc_start=Fraction(0),
        frame_duration=Fraction(1, 30),
        resources=resources,
        storyline=storyline,
    )
    revision = ProjectRevision(digest, "P", "Project", Fraction(1, 30), Fraction(30))
    return document, revision


def test_estimate_deduplicates_source_audio_and_caps_windows_images_and_tokens() -> None:
    """같은 소스 재사용이 전사비를 중복 계산하거나 창당 6장 제한을 넘기는 버그를 잡는다."""
    document, revision = _document()
    pricing = official_pricing_snapshot()
    estimate = build_analysis_estimate(
        document,
        revision,
        AnalysisSettings(),
        pricing=pricing,
        media_grant_revision="9" * 64,
    )
    assert estimate.model == "gpt-5.6-luna"
    assert estimate.unique_source_audio_seconds == 30
    assert estimate.transcription_seconds == 120
    assert estimate.max_windows == 4
    assert estimate.max_images == 24
    assert estimate.max_image_tokens == 24 * 308
    assert estimate.pricing_snapshot_revision == pricing.snapshot_revision
    assert estimate.media_grant_revision == "9" * 64
    # 120s 전사 × (whisper 0.006 + gpt 0.0045) + 24×308 입력 + 4×256 출력 토큰.
    assert estimate.cost_upper_bound == "0.0237072"
    with pytest.raises(ValueError, match="다르다"):
        build_analysis_estimate(
            document,
            revision,
            AnalysisSettings(),
            pricing=official_pricing_snapshot("other-model"),
            media_grant_revision="9" * 64,
        )


def test_effective_settings_fingerprint_binds_engine_project_media_and_sources() -> None:
    """전사/VAD/media map 변경 뒤 낡은 비용 승인을 재사용하는 회귀를 잡는다."""
    base = effective_analysis_settings_sha256(
        AnalysisSettings(),
        engine_config={"transcribe_model": "whisper-1"},
        project_config={"silence_min_seconds": "2/1"},
        media_overrides_sha256="a" * 64,
        source_sha256s=("b" * 64,),
    )
    assert base != effective_analysis_settings_sha256(
        AnalysisSettings(),
        engine_config={"transcribe_model": "changed"},
        project_config={"silence_min_seconds": "2/1"},
        media_overrides_sha256="a" * 64,
        source_sha256s=("b" * 64,),
    )
    assert base != effective_analysis_settings_sha256(
        AnalysisSettings(),
        engine_config={"transcribe_model": "whisper-1"},
        project_config={"silence_min_seconds": "2/1"},
        media_overrides_sha256="c" * 64,
        source_sha256s=("d" * 64,),
    )


def test_frame_plan_combines_candidate_and_context_without_duplicates() -> None:
    """후보 경계 프레임 중복으로 6장 제한을 넘기거나 프레임 격자 밖 시각을 만드는 회귀를 잡는다."""
    selected = select_frame_times(
        RationalRange(Fraction(0), Fraction(10)),
        (RationalRange(Fraction(2), Fraction(5)),),
        frame_duration=Fraction(1, 30),
        max_images=6,
    )
    assert len(selected) == len(set(selected)) == 6
    assert Fraction(2) in selected
    assert Fraction(7, 2) in selected
    assert Fraction(149, 30) in selected
    assert all(value / Fraction(1, 30) == int(value / Fraction(1, 30)) for value in selected)

    anchor = Fraction(1001, 30000)
    anchored = select_frame_times(
        RationalRange(anchor, anchor + Fraction(10)),
        (RationalRange(anchor + 2, anchor + 5),),
        frame_duration=Fraction(1001, 30000),
        grid_anchor=anchor,
        max_images=6,
    )
    assert all(
        (value - anchor) / Fraction(1001, 30000) == int((value - anchor) / Fraction(1001, 30000))
        for value in anchored
    )


def test_unique_and_ng_only_speech_are_kept_until_a_completed_followup_exists() -> None:
    """고유 발화나 NG 표현 하나만으로 실제 발화를 삭제 후보로 만드는 회귀를 잡는다."""
    settings = AnalysisSettings()
    unique = TranscriptEvidence(
        "u1",
        "clip-a",
        RationalRange(Fraction(1), Fraction(3)),
        RationalRange(Fraction(1), Fraction(3)),
        "오늘 처음 왔어요",
        True,
    )
    ng_only = TranscriptEvidence(
        "u2",
        "clip-a",
        RationalRange(Fraction(4), Fraction(5)),
        RationalRange(Fraction(4), Fraction(5)),
        "잠깐 다시 할게",
        False,
    )
    assert safe_transcript_candidates((unique, ng_only), "a" * 64, settings) == ()

    completed = TranscriptEvidence(
        "u3",
        "clip-a",
        RationalRange(Fraction(7), Fraction(9)),
        RationalRange(Fraction(7), Fraction(9)),
        "오늘 처음 왔어요",
        True,
    )
    candidates = safe_transcript_candidates((unique, ng_only, completed), "a" * 64, settings)
    assert {row.reason for row in candidates} == {"retake_overlap", "ng_with_followup"}
    assert {row.timeline_range for row in candidates} == {
        unique.timeline_range,
        ng_only.timeline_range,
    }

    ordinary_english = TranscriptEvidence(
        "u4",
        "clip-a",
        RationalRange(Fraction(10), Fraction(12)),
        RationalRange(Fraction(10), Fraction(12)),
        "Today I am walking",
        True,
    )
    unrelated = TranscriptEvidence(
        "u5",
        "clip-a",
        RationalRange(Fraction(13), Fraction(15)),
        RationalRange(Fraction(13), Fraction(15)),
        "The food is nice",
        True,
    )
    assert safe_transcript_candidates((ordinary_english, unrelated), "a" * 64, settings) == ()
    assert (
        eligible_transcript_evidence(
            (unique, completed),
            successful_instance_ids={"clip-a"},
            locked_instance_ids={"clip-a"},
        )
        == ()
    )


def test_window_plan_groups_candidates_and_maps_nonzero_timecode_on_frame_grid() -> None:
    """후보 수만큼 10초 창 비용이 불어나거나 tcStart를 두 번 더하는 회귀를 잡는다."""
    seeds = tuple(
        CandidateSeed(
            candidate_id=f"seed-{index}",
            window_id=f"old-{index}",
            clip_instance_id="clip-a",
            source_range=RationalRange(Fraction(index), Fraction(index + 1)),
            timeline_range=RationalRange(Fraction(3600 + index), Fraction(3601 + index)),
            reason="internal_silence",
            evidence=("vad:no_speech",),
        )
        for index in (1, 3, 6)
    )
    plans = plan_candidate_windows(
        seeds,
        input_revision="a" * 64,
        clip_ranges={"clip-a": RationalRange(Fraction(3600), Fraction(3610))},
        window_seconds=Fraction(10),
    )
    assert len(plans) == 1
    assert len(plans[0].candidates) == 3

    evidence = project_transcript_evidence(
        utterance_id="cue-1",
        clip_instance_id="clip-a",
        source_range=RationalRange(Fraction(1001, 30000), Fraction(61061, 30000)),
        text="다시 할게",
        complete=True,
        clip_source_start=Fraction(0),
        clip_timeline_start=Fraction(3600),
        resource_start=Fraction(0),
        frame_duration=Fraction(1001, 30000),
    )
    assert evidence is not None
    assert evidence.timeline_range.start == Fraction(3600) + evidence.source_range.start
    assert all(
        value / Fraction(1001, 30000) == int(value / Fraction(1001, 30000))
        for value in (evidence.source_range.start, evidence.source_range.end)
    )


def _window(seed: CandidateSeed) -> LunaWindow:
    return LunaWindow(
        input_revision="a" * 64,
        window_id=seed.window_id,
        candidate_ids=(seed.candidate_id,),
        frame_ids=("f1", "f2"),
        transcript_context="앞: 안녕 / 뒤: 안녕하세요",
        source_hashes=("b" * 64,),
    )


def test_luna_payload_uses_responses_low_detail_and_never_includes_paths() -> None:
    """Chat Completions 형식이나 로컬 경로/API 비밀이 모델 입력으로 새는 회귀를 잡는다."""
    seed = CandidateSeed(
        candidate_id="candidate-1",
        window_id="window-1",
        clip_instance_id="clip-a",
        source_range=RationalRange(Fraction(1), Fraction(2)),
        timeline_range=RationalRange(Fraction(1), Fraction(2)),
        reason="internal_silence",
        evidence=("vad:no_speech",),
    )
    frames = (
        LunaFrame(
            "f1",
            b"jpeg-one",
            hashlib.sha256(b"jpeg-one").hexdigest(),
            source_time=Fraction(1),
            timeline_time=Fraction(3601),
        ),
        LunaFrame(
            "f2",
            b"jpeg-two",
            hashlib.sha256(b"jpeg-two").hexdigest(),
            source_time=Fraction(2),
            timeline_time=Fraction(3602),
        ),
    )
    payload = build_luna_payload(_window(seed), (seed,), frames)
    assert "messages" not in payload
    assert payload["max_output_tokens"] == 256
    assert payload["reasoning"] == {"effort": "none"}
    content = payload["input"][0]["content"]
    images = [item for item in content if item["type"] == "input_image"]
    assert [item["detail"] for item in images] == ["low", "low"]
    assert base64.b64decode(images[0]["image_url"].split(",", 1)[1]) == b"jpeg-one"
    texts = [item for item in content if item["type"] == "input_text"]
    # 이미지마다 바로 앞 input_text가 frame_id·소스·타임라인 시각을 밝힌다.
    assert len(texts) == 3
    assert "frame_id=f1" in texts[1]["text"]
    assert "source_time=1" in texts[1]["text"]
    assert "timeline_time=3601" in texts[1]["text"]
    assert "frame_id=f2" in texts[2]["text"]
    order = [item["type"] for item in content]
    assert order.index("input_text") < order.index("input_image")
    rendered = str(payload)
    assert "/tmp" not in rendered and "OPENAI_API_KEY" not in rendered


def test_luna_malformed_unknown_and_api_errors_fail_closed_but_low_confidence_is_visible() -> None:
    """모델 결함이 임의 후보를 고르거나 저신뢰 유효 후보를 리뷰에서 숨기는 회귀를 잡는다."""
    seed_evidence = ("vad:no_speech",)
    seed_id = CutCandidate.identity(
        input_revision="a" * 64,
        clip_instance_id="clip-a",
        source_range=RationalRange(Fraction(1), Fraction(2)),
        timeline_range=RationalRange(Fraction(1), Fraction(2)),
        reason="internal_silence",
        evidence=seed_evidence,
        window_id="window-1",
    )
    seed = CandidateSeed(
        candidate_id=seed_id,
        window_id="window-1",
        clip_instance_id="clip-a",
        source_range=RationalRange(Fraction(1), Fraction(2)),
        timeline_range=RationalRange(Fraction(1), Fraction(2)),
        reason="internal_silence",
        evidence=seed_evidence,
    )
    window = _window(seed)
    for raw in (
        {},
        {"output_text": "not json"},
        {"output_text": '{"choice":"unknown","confidence":0.99,"evidence_frame_ids":[]}'},
        {"output_text": f'{{"choice":["{seed_id}"],"confidence":0.99,"evidence_frame_ids":[]}}'},
        RuntimeError("network failed"),
    ):
        decision = normalize_luna_response(window, raw)
        assert decision.choice == "keep"
        assert decision.candidate_id is None

    decision = normalize_luna_response(
        window,
        {"output_text": f'{{"choice":"{seed_id}","confidence":0.42,"evidence_frame_ids":["f1"]}}'},
    )
    candidate = finalize_candidate(seed, decision)
    assert candidate is not None
    assert candidate.candidate_id == candidate.identity(
        input_revision=candidate.input_revision,
        clip_instance_id=candidate.clip_instance_id,
        source_range=candidate.source_range,
        timeline_range=candidate.timeline_range,
        reason=candidate.reason,
        evidence=candidate.evidence,
        window_id=candidate.window_id,
    )
    assert candidate.confidence == "0.42"
    assert candidate.selected_by_default is False


def test_cache_key_binds_prompt_model_settings_frames_and_sources() -> None:
    """프롬프트·모델·프레임·소스가 바뀐 뒤 낡은 Luna 응답을 재사용하는 회귀를 잡는다."""
    seed = CandidateSeed(
        "candidate",
        "window",
        "clip-a",
        RationalRange(Fraction(1), Fraction(2)),
        RationalRange(Fraction(1), Fraction(2)),
        "internal_silence",
        ("vad:no_speech",),
    )
    window = LunaWindow(
        "a" * 64,
        "window",
        ("candidate",),
        ("f1",),
        "first transcript",
        ("c" * 64,),
    )
    frames = (
        LunaFrame(
            "f1",
            b"jpeg",
            hashlib.sha256(b"jpeg").hexdigest(),
            source_time=Fraction(1),
            timeline_time=Fraction(2),
        ),
    )
    request = luna_request_sha256(window, (seed,), frames)
    base = luna_cache_key(
        prompt_revision="p1",
        model="gpt-5.6-luna",
        settings_sha256="a" * 64,
        request_sha256=request,
    )
    assert base != luna_cache_key(
        prompt_revision="p2",
        model="gpt-5.6-luna",
        settings_sha256="a" * 64,
        request_sha256=request,
    )
    assert base != luna_cache_key(
        prompt_revision="p1",
        model="gpt-5.6-luna",
        settings_sha256="a" * 64,
        request_sha256=luna_request_sha256(
            LunaWindow(
                window.input_revision,
                window.window_id,
                window.candidate_ids,
                window.frame_ids,
                "changed transcript",
                window.source_hashes,
            ),
            (seed,),
            frames,
        ),
    )


def test_usage_ledger_records_then_stops_the_next_call_at_the_authorized_ceiling() -> None:
    """실제 사용량이 상한을 넘었는데 다음 유료 호출이 계속되는 회귀를 잡는다."""
    pricing = official_pricing_snapshot()
    window = LunaWindow("a" * 64, "window-1", (), (), "", ())
    ledger = UsageLedger(Decimal("0.001"))
    first = luna_usage(
        window=window,
        raw={"usage": {"input_tokens": 2000, "output_tokens": 100}},
        pricing=pricing,
        attempt_id="attempt-1",
    )
    assert first is not None
    assert first.input_tokens == 2000 and first.output_tokens == 100
    # 2000×0.20 + 100×1.20 = 520 tokens-dollar per million → 0.00052
    assert first.cost == "0.00052"
    ledger.record(first)
    over = luna_usage(
        window=window,
        raw={"usage": {"input_tokens": 2000, "output_tokens": 100}},
        pricing=pricing,
        attempt_id="attempt-1",
    )
    assert over is not None
    with pytest.raises(CostCeilingExceeded):
        ledger.record(over)
    assert ledger.spent == Decimal("0.00104")
    # usage 블록이 없거나 숫자가 아니면 기록을 지어내지 않는다.
    assert luna_usage(window=window, raw={}, pricing=pricing, attempt_id="a") is None
    assert (
        luna_usage(
            window=window,
            raw={"usage": {"input_tokens": "many", "output_tokens": 1}},
            pricing=pricing,
            attempt_id="a",
        )
        is None
    )
    whisper = transcription_usage(
        input_revision="a" * 64,
        attempt_id="attempt-1",
        model=pricing.whisper_model,
        audio_seconds=Fraction(60),
        per_minute=pricing.whisper_per_minute,
    )
    assert whisper.cost == "0.006"
    assert whisper.window_id is None


def test_candidate_preview_binds_frame_hash_and_removal_duration(tmp_path: Path) -> None:
    """미리보기가 근거 프레임 해시와 후보 제거 길이를 그대로 실는지 확인한다."""
    decision_revision = "d" * 64
    candidate = CutCandidate(
        candidate_id="c" * 64,
        input_revision="a" * 64,
        clip_instance_id="clip-a",
        source_range=RationalRange(Fraction(1), Fraction(4)),
        timeline_range=RationalRange(Fraction(10), Fraction(13)),
        reason="retake_overlap",
        evidence=("transcript:retake_overlap",),
        confidence="0.93",
        window_id="window-1",
        decision_revision=decision_revision,
    )
    frame = LunaFrame(
        "f1",
        b"jpeg",
        hashlib.sha256(b"jpeg").hexdigest(),
        source_time=Fraction(1),
        timeline_time=Fraction(10),
    )
    preview = build_candidate_preview(candidate, frame, tmp_path / "c.jpg")
    assert preview.thumbnail_sha256 == frame.sha256
    assert preview.removal_duration == Fraction(3)
    assert preview.frame_id == "f1"
    assert preview.confidence == "0.93"
