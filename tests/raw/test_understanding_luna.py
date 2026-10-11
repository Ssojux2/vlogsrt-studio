"""A4 Luna 의미 판정 — TEXT+IMAGE 증거, 제한 JSON, 승인·예산·지문·전송 경계.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §7.2·§7.5·§14.2·§14.3. 실제
호출은 없다 — 전송 경로(`request_luna_vision`)는 모듈 경계에서 가로채 가짜로
돌려준다(유료 실기는 루트의 별도 승인 뒤 NOT_RUN으로 남는다).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import OPENAI_LUNA_MODEL, RawModelRoles, RawWorkflowConfig
from vlogsrt.project.raw.understanding import (
    AsrEvidence,
    FrameEvidence,
    FrameSamplingResult,
    SelectedAudio,
    UtteranceEvidence,
    VideoGrid,
    WindowVerdict,
    propose_segmentation,
)
from vlogsrt.project.raw.understanding_luna import (
    AnalysisApprovalError,
    AnalysisBudget,
    BudgetExhausted,
    CountingReservationGate,
    LunaCostEstimate,
    OnlineAnalysisApproval,
    ReplaySemanticResponder,
    SemanticAnnotation,
    WindowEvidence,
    analysis_fingerprint,
    annotate_windows,
    annotation_cache_document,
    build_luna_payload,
    cacheable,
    document_execution_mode,
    estimate_luna_cost,
    luna_output_schema,
    luna_request_sha256,
    parse_luna_response,
    require_online_approval,
    stage1_document,
    validated_luna_responder,
    verdicts_from_annotations,
    window_evidence,
)

SETTINGS = RawUnderstandingSettings()
LUNA_WORKFLOW = RawWorkflowConfig(models=replace(RawModelRoles(), luna_model=OPENAI_LUNA_MODEL))
"""Luna(OpenAI) 경로의 설정 — W-18(D58) 전의 기본값 그대로다. 이 파일은 Luna 문을 시험한다."""


def make_audio(tmp_path: Path, *, seconds: float = 20.0) -> SelectedAudio:
    samples = int(seconds * stage1.PCM_SAMPLE_RATE)
    pcm = tmp_path / "analysis.pcm"
    pcm.write_bytes(b"synthetic-pcm" * (samples * 2 // 13 + 1))
    return SelectedAudio(
        source_id="source_test",
        source_path=tmp_path / "original.mp4",
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=pcm,
        pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
        sample_rate=stage1.PCM_SAMPLE_RATE,
        channels=1,
        sample_count=samples,
        pcm_sample0_file_time=Fraction(0),
    )


def make_utterance(
    ordinal: int, start: float, end: float, text: str = "안녕 망원동"
) -> UtteranceEvidence:
    start_sample = round(start * stage1.PCM_SAMPLE_RATE)
    end_sample = round(end * stage1.PCM_SAMPLE_RATE)
    return UtteranceEvidence(
        utterance_id=f"utterance_{ordinal:06d}",
        chunk_index=0,
        position=ordinal,
        text=text,
        start=Fraction(start_sample, stage1.PCM_SAMPLE_RATE),
        end=Fraction(end_sample, stage1.PCM_SAMPLE_RATE),
        start_sample=start_sample,
        end_sample=end_sample,
        evidence_id=f"run:{ordinal}",
    )


def make_evidence(tmp_path: Path) -> WindowEvidence:
    """창 하나의 증거 — 발화 2건·프레임 2장(실제 바이트)·경계 후보 1건."""
    audio = make_audio(tmp_path)
    utterances = [make_utterance(0, 1.0, 4.0), make_utterance(1, 8.0, 12.0)]
    video = VideoGrid(0, Fraction(0), Fraction(1, 25), 500)
    plan = stage1.plan_windows(audio, utterances, SETTINGS, video)
    frames = []
    for window in plan.windows[:1]:
        for item in window.frame_plans[:2]:
            path = tmp_path / f"{item.frame_id}.jpg"
            path.write_bytes(b"\xff\xd8fakejpeg")
            frames.append(
                FrameEvidence(
                    frame_id=item.frame_id,
                    frame_index=item.frame_index,
                    image_path=path,
                    image_sha256=hashlib.sha256(b"\xff\xd8fakejpeg").hexdigest(),
                    file_time=Fraction(item.frame_index, 25),
                )
            )
    result = FrameSamplingResult(tuple(frames), ())
    first = window_evidence(plan, result, utterances)[0]
    # 발화 종료 후보 하나만 남긴다 — 경계 검사를 명확하게
    return replace(
        first,
        candidates=tuple(item for item in first.candidates if item.kind == "utterance_end"),
    )


def accepted_body(evidence: WindowEvidence, **overrides: Any) -> dict[str, Any]:
    """스키마를 지키는 판정 본문 — 필요한 칸만 덮어쓴다."""
    body: dict[str, Any] = {
        "window_id": evidence.window.window_id,
        "speech_summary": "도착 인사",
        "visual_summary": "실내",
        "scene_tags": ["talking"],
        "evidence_ids": [evidence.utterances[0].utterance_id] if evidence.utterances else [],
        "suggested_boundary_ids": (
            [evidence.candidates[0].boundary_id] if evidence.candidates else []
        ),
        "editing_notes": [],
        "needs_review": False,
        "uncertainties": [],
    }
    body.update(overrides)
    return body


def respond(body: dict[str, Any]) -> dict[str, Any]:
    return {"output_text": json.dumps(body, ensure_ascii=False)}


def parse(body: dict[str, Any], evidence: WindowEvidence) -> SemanticAnnotation:
    return parse_luna_response(
        respond(body),
        evidence=evidence,
        model="luna-role",
        request_sha256="f" * 64,
        origin="offline_replay",
        prompt_revision=SETTINGS.prompt_revision,
    )


# ─────────────────────────────────────────────────────────────
#  페이로드 — TEXT+IMAGE 뿐, 다른 형태는 없다
# ─────────────────────────────────────────────────────────────


def test_payload_carries_text_and_images_only(tmp_path: Path) -> None:
    """오디오·영상 파일은 가지 않는다 — 사진은 data URL, 대화는 전사 문자열(§7.2)."""
    evidence = make_evidence(tmp_path)
    payload = build_luna_payload(evidence, SETTINGS)
    content = payload["input"][0]["content"]
    kinds = [item["type"] for item in content]
    assert kinds[0] == "input_text"
    assert kinds[1:] == ["input_image"] * len(evidence.frames)
    assert all(item["image_url"].startswith("data:image/jpeg;base64,") for item in content[1:])
    dumped = json.dumps(payload)
    assert ".mp4" not in dumped and ".pcm" not in dumped and ".wav" not in dumped
    text = content[0]["text"]
    for utterance in evidence.utterances:
        assert utterance.text in text  # 대화는 문자열 증거로만
        assert utterance.utterance_id in text
    for frame in evidence.frames:
        assert frame.frame_id in text
    for candidate in evidence.candidates:
        assert candidate.boundary_id in text
    assert evidence.window.window_id in text


def test_payload_enforces_the_strict_json_schema(tmp_path: Path) -> None:
    """출력 강제는 strict json_schema다 — confidence 칸 같은 것은 schema에 없다."""
    evidence = make_evidence(tmp_path)
    payload = build_luna_payload(evidence, SETTINGS)
    text_format = payload["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["strict"] is True
    schema = text_format["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(luna_output_schema()["properties"])
    assert "confidence" not in schema["properties"]


# ─────────────────────────────────────────────────────────────
#  판정 해석 — 제공된 아이디의 세계 밖은 없다
# ─────────────────────────────────────────────────────────────


def test_accepted_annotation_keeps_model_shape(tmp_path: Path) -> None:
    """스키마를 지키는 판정은 accepted — 요약·태그·경계가 그대로 올라간다."""
    evidence = make_evidence(tmp_path)
    annotation = parse(accepted_body(evidence), evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED
    assert annotation.window_id == evidence.window.window_id
    assert annotation.speech_summary == "도착 인사"
    assert annotation.suggested_boundary_ids == (evidence.candidates[0].boundary_id,)
    assert annotation.needs_review is False
    assert annotation.origin == "offline_replay"


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_id": "window_000999"},  # 남의 창
        {"evidence_ids": ["utterance_999999"]},  # 제공되지 않은 증거
        {"suggested_boundary_ids": ["boundary_999999999"]},  # 제공되지 않은 경계
        {"needs_review": "no"},  # bool이 아니다
        {"scene_tags": "talking"},  # 배열이 아니다
        {"speech_summary": 3},
        {"extra_column": "x"},  # schema에 없는 칸
    ],
)
def test_violations_are_rejected_not_applied(tmp_path: Path, overrides: dict[str, Any]) -> None:
    """어긋난 판정은 rejected — 어떤 경계·요약도 채택되지 않는다(§7.5)."""
    evidence = make_evidence(tmp_path)
    annotation = parse(accepted_body(evidence, **overrides), evidence)
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert annotation.defect
    assert annotation.suggested_boundary_ids == ()
    assert annotation.needs_review is True


def test_missing_field_and_broken_json_are_rejected(tmp_path: Path) -> None:
    """필수 칸 누락·JSON 오류·빈 본문도 rejected — 예외가 아니라 기록이다."""
    evidence = make_evidence(tmp_path)
    body = accepted_body(evidence)
    del body["speech_summary"]
    assert parse(body, evidence).status == luna.ANNOTATION_REJECTED
    payload = build_luna_payload(evidence, SETTINGS)
    assert (
        parse_luna_response(
            {"output_text": "{not json"},
            evidence=evidence,
            model="m",
            request_sha256="f" * 64,
            origin="live",
            prompt_revision="r",
        ).status
        == luna.ANNOTATION_REJECTED
    )
    assert (
        parse_luna_response(
            {"output_text": ""},
            evidence=evidence,
            model="m",
            request_sha256="f" * 64,
            origin="live",
            prompt_revision="r",
        ).status
        == luna.ANNOTATION_REJECTED
    )
    assert payload  # 조립은 성공했다 — 실패는 해석의 것이다


def test_annotate_windows_records_failures_per_window(tmp_path: Path) -> None:
    """응답자 실패(타임아웃 포함)는 그 창의 failed 판정이다 — 실행은 계속된다."""
    evidence = make_evidence(tmp_path)
    second = replace(
        evidence, window=replace(evidence.window, ordinal=1, window_id="window_000001")
    )

    class Flaky:
        def respond(self, *, window_id: str, payload: Any) -> Any:
            if window_id == evidence.window.window_id:
                raise TimeoutError("30초 초과")
            return respond(accepted_body(second))

    annotations = annotate_windows(
        (evidence, second), Flaky(), settings=SETTINGS, model="luna-role", origin="online_live"
    )
    assert [item.status for item in annotations] == [
        luna.ANNOTATION_FAILED,
        luna.ANNOTATION_ACCEPTED,
    ]
    assert annotations[0].error_code == "timeout"
    assert annotations[0].response_uncertain
    assert "시간 초과" in annotations[0].defect


NORMAL_WINDOW_REQUEST_SHA256 = "48eac75d44e2a91dbe4199c9e85c4621d49e5e16b8cb342cffc0a2baceb97324"
"""`make_evidence` 창(프레임 2장)의 요청 신원 — 7f74338에서 잰 값.

바뀌면 저장된 판정이 전부 다시 청구된다 — 프레임 누락 처리는 이 값을 건드리지 못한다.
"""


def test_annotate_windows_skips_window_whose_planned_frames_all_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """계획된 프레임이 한 장도 없는 창은 부르지 않고 failed로 남는다 — 오디오만인 창은 간다."""
    normal = make_evidence(tmp_path)
    assert normal.frames and normal.window.frame_plans
    assert luna_request_sha256("luna-role", build_luna_payload(normal, SETTINGS)) == (
        NORMAL_WINDOW_REQUEST_SHA256
    )
    missing = replace(normal, frames=())
    audio_only = replace(
        normal,
        window=replace(normal.window, ordinal=1, window_id="window_000001", frame_plans=()),
        frames=(),
    )
    asked: list[str] = []
    begun: list[str] = []
    real_begin = luna.request_log.begin

    def record_begin(stage: str, fingerprint: str, **kwargs: Any) -> Any:
        begun.append(fingerprint)
        return real_begin(stage, fingerprint, **kwargs)

    class Recording:
        def respond(self, *, window_id: str, payload: Any) -> Any:
            asked.append(window_id)
            return respond(accepted_body(audio_only))

    monkeypatch.setattr(luna.request_log, "begin", record_begin)
    annotations = annotate_windows(
        (missing, audio_only), Recording(), settings=SETTINGS, model="luna-role", origin="live"
    )
    assert asked == ["window_000001"]
    skipped, sent = annotations
    assert skipped.window_id == missing.window.window_id
    assert skipped.status == luna.ANNOTATION_FAILED
    assert skipped.error_code == luna.ERROR_FRAMES_MISSING == "frames_missing"
    assert skipped.needs_review and skipped.defect
    assert not skipped.response_uncertain
    assert skipped.request_sha256 == luna_request_sha256(
        "luna-role", build_luna_payload(missing, SETTINGS)
    )
    assert not cacheable(skipped)
    assert sent.status == luna.ANNOTATION_ACCEPTED
    assert begun == [sent.request_sha256]  # 건너뛴 창은 영수증 행도 없다


def test_replay_responder_demands_a_known_window(tmp_path: Path) -> None:
    """재생 응답자는 창 아이디를 안다 — 모르는 창은 실행 설정 결함으로 닫는다."""
    evidence = make_evidence(tmp_path)
    replays = {evidence.window.window_id: respond(accepted_body(evidence))}
    responder = ReplaySemanticResponder(replays)
    assert responder.respond(window_id=evidence.window.window_id, payload={})
    with pytest.raises(luna.LunaEvidenceError, match="재생할 Luna 응답이 없다"):
        responder.respond(window_id="window_000999", payload={})


def test_verdicts_only_carry_accepted_boundaries(tmp_path: Path) -> None:
    """accepted만 경계를 싣고, 불확실성이 있으면 검토가 켜진다."""
    evidence = make_evidence(tmp_path)
    second = replace(
        evidence, window=replace(evidence.window, ordinal=1, window_id="window_000001")
    )
    third = replace(evidence, window=replace(evidence.window, ordinal=2, window_id="window_000002"))
    accepted = parse(accepted_body(evidence), evidence)
    uncertain = parse(accepted_body(second), second)
    uncertain = replace(uncertain, uncertainties=("장면 전환 불명확",))
    rejected = parse(accepted_body(third, window_id="window_000999"), third)
    verdicts = verdicts_from_annotations([accepted, uncertain, rejected])
    good = verdicts[evidence.window.window_id]
    assert good.accepted and not good.needs_review
    assert verdicts[uncertain.window_id].needs_review  # 불확실성 → 검토
    assert not verdicts[rejected.window_id].accepted


# ─────────────────────────────────────────────────────────────
#  예산·비용 — 미확인 가격을 0원으로 두지 않는다
# ─────────────────────────────────────────────────────────────


def test_budget_validates_its_own_shape() -> None:
    """한도·상한의 값역 — 깨진 예산은 만들어지지 않는다."""
    assert AnalysisBudget(max_luna_requests=4, max_asr_chunks=2)
    with pytest.raises(ValueError):
        AnalysisBudget(max_luna_requests=0, max_asr_chunks=2)
    with pytest.raises(ValueError):
        AnalysisBudget(max_luna_requests=4, max_asr_chunks=0)
    with pytest.raises(ValueError):
        AnalysisBudget(max_luna_requests=4, max_asr_chunks=2, luna_price_status="free")
    with pytest.raises(ValueError):
        AnalysisBudget(  # 사용자 상한인데 Decimal이 아니다
            max_luna_requests=4, max_asr_chunks=2, luna_price_status="user_supplied"
        )
    with pytest.raises(ValueError):
        AnalysisBudget(  # 상한이 0 — 무료로 두는 길
            max_luna_requests=4,
            max_asr_chunks=2,
            luna_price_status="user_supplied",
            luna_user_ceiling_per_request=Decimal("0"),
        )


def test_unknown_price_never_becomes_a_number() -> None:
    """단가 미확인이면 금액은 만들지 않는다 — 상태와 요청 수만 있다(§14.3)."""
    budget = AnalysisBudget(max_luna_requests=9, max_asr_chunks=9)
    estimate = estimate_luna_cost(5, budget)
    assert isinstance(estimate, LunaCostEstimate)
    assert estimate.price_status == luna.PRICE_UNKNOWN
    assert estimate.user_ceiling_total is None
    assert estimate.request_count == 5
    assert "0원" in estimate.note or "미확인" in estimate.note


def test_user_supplied_ceiling_is_labeled_as_a_bound() -> None:
    """사용자 상한은 상한일 뿐 — 실측 견적으로 표시하지 않는다."""
    budget = AnalysisBudget(
        max_luna_requests=9,
        max_asr_chunks=9,
        luna_price_status=luna.PRICE_USER_SUPPLIED,
        luna_user_ceiling_per_request=Decimal("0.01"),
    )
    estimate = estimate_luna_cost(4, budget)
    assert estimate.user_ceiling_total == Decimal("0.04")
    assert estimate.price_status == luna.PRICE_USER_SUPPLIED
    assert "실측" in estimate.note


def test_reservation_gate_counts_and_closes() -> None:
    """예약 문 — 같은 창 재시도는 세지 않고 한도 넘으면 닫는다."""
    gate = CountingReservationGate(AnalysisBudget(max_luna_requests=2, max_asr_chunks=9))
    gate.reserve(count=1, key="window_000000")
    gate.reserve(count=1, key="window_000000")  # 재시도 — 새 예약 아니다
    gate.reserve(count=1, key="window_000001")
    with pytest.raises(BudgetExhausted):
        gate.reserve(count=1, key="window_000002")


# ─────────────────────────────────────────────────────────────
#  온라인 승인 — 오프라인 설정이 항상 우선이다
# ─────────────────────────────────────────────────────────────


def approval(tmp_path: Path, *, fingerprint: str, windows: int = 2) -> OnlineAnalysisApproval:
    audio = make_audio(tmp_path)
    utterances = [make_utterance(0, 1.0, 4.0)]
    plan = stage1.plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    return (
        OnlineAnalysisApproval(
            approved=True,
            source_sha256=audio.source_sha256,
            audio_stream_index=audio.audio_stream_index,
            sample_count=audio.sample_count,
            window_count=len(plan.windows),
            fingerprint=fingerprint,
            asr_model=LUNA_WORKFLOW.models.timing_model,
            luna_model=LUNA_WORKFLOW.models.luna_model,
            budget=AnalysisBudget(max_luna_requests=windows, max_asr_chunks=9),
        ),
        audio,
        plan,
    )


def online_workflow() -> RawWorkflowConfig:
    return replace(LUNA_WORKFLOW, network_mode="online")


def env_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """환경 스위치도 열어둔다 — 기본(오프라인)이면 문이 환경에서 먼저 닫힌다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)


def test_offline_config_blocks_even_an_approved_run(tmp_path: Path) -> None:
    """오프라인 설정이 우선한다 — 승인이 있어도 문은 닫혀 있다(§14.3)."""
    audio = make_audio(tmp_path)
    plan = stage1.plan_windows(audio, [], SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    granted, _, _ = approval(tmp_path, fingerprint="f" * 64)
    with pytest.raises(AnalysisApprovalError, match="닫혀 있다"):
        require_online_approval(
            granted, settings=LUNA_WORKFLOW, audio=audio, plan=plan, fingerprint="f" * 64
        )


def test_matching_approval_opens_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """출처·지문·범위·모델이 전부 일치하면 문이 열린다."""
    env_online(monkeypatch)
    audio = make_audio(tmp_path)
    utterances = [make_utterance(0, 1.0, 4.0)]
    plan = stage1.plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    fingerprint = analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    granted, granted_audio, granted_plan = approval(tmp_path, fingerprint=fingerprint)
    require_online_approval(
        granted,
        settings=online_workflow(),
        audio=granted_audio,
        plan=granted_plan,
        fingerprint=fingerprint,
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "fingerprint",
        "source",
        "window_count",
        "asr_model",
        "luna_model",
        "budget",
        "unapproved",
    ],
)
def test_any_drift_from_the_approval_closes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """승인이 본 것과 실행이 다르면 닫힌다 — 새 추정과 새 승인이 필요하다."""
    env_online(monkeypatch)
    audio = make_audio(tmp_path)
    utterances = [make_utterance(0, 1.0, 4.0)]
    plan = stage1.plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    fingerprint = analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    granted, _, _ = approval(tmp_path, fingerprint=fingerprint)
    kwargs: dict[str, Any] = {}
    if tamper == "fingerprint":
        kwargs["fingerprint"] = "0" * 64
    elif tamper == "source":
        audio = replace(audio, source_sha256="b" * 64)
    elif tamper == "window_count":
        plan = stage1.plan_windows(
            replace(audio, sample_count=audio.sample_count + 16000 * 30),
            utterances,
            SETTINGS,
            None,
            video_status=stage1.VIDEO_ABSENT,
        )
    elif tamper == "asr_model":
        granted = replace(granted, asr_model="other-asr")
    elif tamper == "luna_model":
        granted = replace(granted, luna_model="other-luna")
    elif tamper == "budget":
        granted = replace(
            granted,
            budget=AnalysisBudget(max_luna_requests=1, max_asr_chunks=9),
        )
    else:
        granted = replace(granted, approved=False)
    kwargs.setdefault("fingerprint", fingerprint)
    with pytest.raises(AnalysisApprovalError):
        require_online_approval(
            granted, settings=online_workflow(), audio=audio, plan=plan, **kwargs
        )


def test_env_offline_blocks_even_an_online_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VLOGSRT_OFFLINE이 사람의 손으로 열리기 전까지 유료 호출은 없다(§14.3).

    설정이 online이고 승인이 전부 일치해도 환경이 오프라인(기본)이면 승인
    문도, 검증 문맥 공장도 닫혀 있다 — UI가 설정을 바꿔서 환경을 우회하는
    길이 없다.
    """
    audio = make_audio(tmp_path)
    utterances = [make_utterance(0, 1.0, 4.0)]
    plan = stage1.plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    fingerprint = analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    granted, granted_audio, granted_plan = approval(tmp_path, fingerprint=fingerprint)
    with pytest.raises(AnalysisApprovalError, match="VLOGSRT_OFFLINE"):
        require_online_approval(
            granted,
            settings=online_workflow(),
            audio=granted_audio,
            plan=granted_plan,
            fingerprint=fingerprint,
        )
    with pytest.raises(AnalysisApprovalError, match="VLOGSRT_OFFLINE"):
        validated_luna_responder(
            client=object(),
            settings=online_workflow(),
            understanding=SETTINGS,
            audio=granted_audio,
            plan=granted_plan,
            utterances=utterances,
            frames=FrameSamplingResult((), ()),
            approval=granted,
        )


# ─────────────────────────────────────────────────────────────
# 전송 경계 — 검증 문맥 없이는 응답자 자체가 존재하지 않는다
# ─────────────────────────────────────────────────────────────


def online_run(
    tmp_path: Path, *, seconds: float = 20.0
) -> tuple[SelectedAudio, Any, list[UtteranceEvidence], str]:
    """검증 문맥 시험용 실행 재료 — 실제 레지스트리 모델로 지문을 잰다."""
    audio = make_audio(tmp_path, seconds=seconds)
    both = [make_utterance(0, 1.0, 4.0), make_utterance(1, 16.0, 18.0)]
    utterances = both[: 2 if seconds > 16 else 1]
    plan = stage1.plan_windows(audio, utterances, SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    workflow = online_workflow()
    fingerprint = analysis_fingerprint(
        audio=audio,
        plan=plan,
        settings=SETTINGS,
        asr_model=workflow.models.timing_model,
        luna_model=workflow.models.luna_model,
    )
    return audio, plan, utterances, fingerprint


def matched_approval(
    audio: SelectedAudio, plan: Any, fingerprint: str, *, approved: bool = True
) -> OnlineAnalysisApproval:
    """실행 재료와 전부 일치하는 승인 — 어긋남 시험의 출발점."""
    return OnlineAnalysisApproval(
        approved=approved,
        source_sha256=audio.source_sha256,
        audio_stream_index=audio.audio_stream_index,
        sample_count=audio.sample_count,
        window_count=len(plan.windows),
        fingerprint=fingerprint,
        asr_model=LUNA_WORKFLOW.models.timing_model,
        luna_model=LUNA_WORKFLOW.models.luna_model,
        budget=AnalysisBudget(max_luna_requests=len(plan.windows), max_asr_chunks=9),
    )


def test_responder_cannot_exist_without_a_validated_context(tmp_path: Path) -> None:
    """미승인·모델 어긋남·예약 문 없음·검증 문맥 없음·위조 문맥 — 응답자가
    만들어지지 않는다(전송 경계보다 앞선 문이다)."""
    audio, plan, _, fingerprint = online_run(tmp_path)
    granted = matched_approval(audio, plan, fingerprint)
    gate = CountingReservationGate(granted.budget)
    with pytest.raises(AnalysisApprovalError, match="승인되지 않은"):  # 루트 probe 경로 그대로
        luna.LunaSemanticResponder(object(), "luna-role", replace(granted, approved=False))
    with pytest.raises(AnalysisApprovalError, match="승인되지 않은"):
        luna.LunaSemanticResponder(
            object(), granted.luna_model, replace(granted, approved=False), gate=gate
        )
    with pytest.raises(AnalysisApprovalError, match="다르다"):  # 모델이 승인과 다르다
        luna.LunaSemanticResponder(object(), "other-luna", granted, gate=gate)
    with pytest.raises(AnalysisApprovalError, match="예약 문"):
        luna.LunaSemanticResponder(object(), granted.luna_model, granted)
    with pytest.raises(AnalysisApprovalError, match="실행 문맥"):
        luna.LunaSemanticResponder(object(), granted.luna_model, granted, gate=gate)
    with pytest.raises(AnalysisApprovalError, match="실행 문맥"):  # 위조 문맥
        luna.LunaSemanticResponder(
            object(), granted.luna_model, granted, gate=gate, context=object()
        )


def test_validated_responder_sends_bounded_fake_live_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증 문맥을 지난 응답자만 전송한다 — 가짜 전송 경로로 실제 호출 없이 증명.

    창마다 페이로드 신원이 검증된 그대로 나가고(online=True), 예약은 창 수만큼만
    열리며, 예산이 닫히면 새 창은 문 앞에서 멈춘다.
    """
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    granted = matched_approval(audio, plan, fingerprint)
    responder = validated_luna_responder(
        client=object(),
        settings=online_workflow(),
        understanding=SETTINGS,
        audio=audio,
        plan=plan,
        utterances=utterances,
        frames=FrameSamplingResult((), ()),
        approval=granted,
    )
    context = responder.context
    sent: list[dict[str, Any]] = []
    by_id = {item.window.window_id: item for item in context.evidence_windows}

    def fake_send(*args: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(kwargs)
        instruction = kwargs["payload"]["input"][0]["content"][0]["text"]
        window_id = next(
            line.split(":", 1)[1].strip()
            for line in instruction.splitlines()
            if line.startswith("window_id:")
        )
        return respond(accepted_body(by_id[window_id]))

    monkeypatch.setattr(luna, "request_luna_vision", fake_send)
    annotations = annotate_windows(
        context.evidence_windows,
        responder,
        settings=SETTINGS,
        model=online_workflow().models.luna_model,
        origin="live",
    )
    assert all(item.status == luna.ANNOTATION_ACCEPTED for item in annotations)
    assert len(sent) == len(context.evidence_windows)
    assert all(call["online"] is True for call in sent)
    assert all(call["model"] == online_workflow().models.luna_model for call in sent)
    assert document_execution_mode(annotations) == "online_live"
    gate = context.gate
    assert isinstance(gate, CountingReservationGate)
    assert gate.remaining == 0  # 창 수만큼만 — 예산이 정확히 닫힌다
    assert responder.respond(  # 같은 창 재시도는 새 예약이 아니다
        window_id=context.evidence_windows[0].window.window_id,
        payload=build_luna_payload(context.evidence_windows[0], SETTINGS),
    )
    assert len(sent) == len(context.evidence_windows) + 1  # 재전송은 일어나도
    assert gate.remaining == 0  # 예약은 두 번 세지 않는다


def test_validated_responder_denies_foreign_windows_and_drifted_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """승인·검증된 창이 아니거나 페이로드가 검증 때와 다르면 전송은 없다."""
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    granted = matched_approval(audio, plan, fingerprint)
    responder = validated_luna_responder(
        client=object(),
        settings=online_workflow(),
        understanding=SETTINGS,
        audio=audio,
        plan=plan,
        utterances=utterances,
        frames=FrameSamplingResult((), ()),
        approval=granted,
    )
    first = responder.context.evidence_windows[0]
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        luna, "request_luna_vision", lambda *a, **k: sent.append(k) or {"output_text": "{}"}
    )
    payload = build_luna_payload(first, SETTINGS)
    with pytest.raises(AnalysisApprovalError, match="승인·검증된 창"):
        responder.respond(window_id="window_000999", payload=payload)
    forged = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "위조"}]}]}
    with pytest.raises(AnalysisApprovalError, match="요청 신원과 다르다"):
        responder.respond(window_id=first.window.window_id, payload=forged)
    second = responder.context.evidence_windows[1]
    with pytest.raises(AnalysisApprovalError, match="요청 신원과 다르다"):  # 남의 창 페이로드
        responder.respond(
            window_id=first.window.window_id,
            payload=build_luna_payload(second, SETTINGS),
        )
    assert sent == []  # 세 길 모두 전송 경계 앞에서 닫혔다


def test_budget_closes_the_send_boundary_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """예약이 바닥나면 다음 창은 전송 경계 앞에서 멈춘다 — 문맥이 옳아도."""
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    granted = matched_approval(audio, plan, fingerprint)
    one_shot = CountingReservationGate(
        AnalysisBudget(max_luna_requests=1, max_asr_chunks=9)
    )  # 공유 게이트가 이미 대부분을 쓴 모양
    responder = validated_luna_responder(
        client=object(),
        settings=online_workflow(),
        understanding=SETTINGS,
        audio=audio,
        plan=plan,
        utterances=utterances,
        frames=FrameSamplingResult((), ()),
        approval=granted,
        gate=one_shot,
    )
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        luna, "request_luna_vision", lambda *a, **k: sent.append(k) or {"output_text": "{}"}
    )
    first, second = responder.context.evidence_windows[:2]
    assert responder.respond(
        window_id=first.window.window_id, payload=build_luna_payload(first, SETTINGS)
    )
    with pytest.raises(BudgetExhausted):
        responder.respond(
            window_id=second.window.window_id, payload=build_luna_payload(second, SETTINGS)
        )
    assert len(sent) == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "unapproved",
        "fingerprint",
        "source",
        "stream",
        "extent",
        "window_count",
        "budget",
        "offline_config",
    ],
)
def test_factory_denies_forged_or_drifted_contexts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """승인이 본 것과 공장이 본 것이 다르면 검증 문맥은 만들어지지 않는다."""
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    settings: RawWorkflowConfig = online_workflow()
    granted = matched_approval(audio, plan, fingerprint)
    if tamper == "unapproved":
        granted = replace(granted, approved=False)
    elif tamper == "fingerprint":
        granted = replace(granted, fingerprint="0" * 64)
    elif tamper == "source":
        audio = replace(audio, source_sha256="b" * 64)
    elif tamper == "stream":
        audio = replace(audio, audio_stream_index=7)
    elif tamper == "extent":
        audio = replace(audio, sample_count=audio.sample_count - 16000)
    elif tamper == "window_count":
        granted = replace(granted, window_count=99)
    elif tamper == "budget":
        granted = replace(granted, budget=AnalysisBudget(max_luna_requests=1, max_asr_chunks=9))
    else:
        settings = LUNA_WORKFLOW  # 오프라인 설정 — 공장이 닫는다
    with pytest.raises(AnalysisApprovalError):
        validated_luna_responder(
            client=object(),
            settings=settings,
            understanding=SETTINGS,
            audio=audio,
            plan=plan,
            utterances=utterances,
            frames=FrameSamplingResult((), ()),
            approval=granted,
        )


def direct_context(
    audio: SelectedAudio,
    plan: Any,
    utterances: list[UtteranceEvidence],
    fingerprint: str,
) -> luna.OnlineLunaExecutionContext:
    """공장 없이 직접 구성하는 올바른 문맥 — 증거에서 지은 해시와 잰 지문만 넣는다."""
    granted = matched_approval(audio, plan, fingerprint)
    settings = online_workflow()
    evidence = window_evidence(plan, FrameSamplingResult((), ()), utterances)
    hashes = {
        item.window.window_id: luna_request_sha256(
            settings.models.luna_model, build_luna_payload(item, SETTINGS)
        )
        for item in evidence
    }
    return luna.OnlineLunaExecutionContext(
        settings=settings,
        understanding=SETTINGS,
        audio=audio,
        plan=plan,
        approval=granted,
        evidence_windows=evidence,
        request_sha256s=hashes,
        gate=CountingReservationGate(granted.budget),
        fingerprint=fingerprint,
    )


def test_direct_context_construction_revalidates_every_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """루트 probe의 우회 경로 — 공장 없는 직접 구성은 문맥 스스로 닫는다.

    잘못된 출처·스트림·분량·지문·ASR의 승인, 빈 페이로드의 임의 해시 지도,
    거짓 지문 칸을 몰아넣어도 `AnalysisApprovalError`로 죽고 전송은 없다.
    """
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    settings = online_workflow()
    evidence = window_evidence(plan, FrameSamplingResult((), ()), utterances)
    window = plan.windows[0].window_id
    forged_approval = OnlineAnalysisApproval(
        approved=True,
        source_sha256="wrong-source",
        audio_stream_index=99,
        sample_count=7,
        window_count=1,
        fingerprint="wrong-fingerprint",
        asr_model="wrong-asr",
        luna_model=settings.models.luna_model,
        budget=AnalysisBudget(max_luna_requests=1, max_asr_chunks=1),
    )
    gate = CountingReservationGate(forged_approval.budget)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        luna, "request_luna_vision", lambda *a, **k: sent.append(k) or {"output_text": "{}"}
    )
    with pytest.raises(AnalysisApprovalError):
        forged = luna.OnlineLunaExecutionContext(  # 루트 probe 그대로 — 공장 우회
            settings,
            SETTINGS,
            audio,
            plan,
            forged_approval,
            evidence,
            {window: luna_request_sha256(settings.models.luna_model, {})},
            gate,
            "totally-unvalidated",
        )
        luna.LunaSemanticResponder(
            object(), settings.models.luna_model, forged_approval, gate, context=forged
        ).respond(window_id=window, payload={})
    assert sent == []  # 가짜 전송조차 경계 앞에서 죽었다


def test_replace_cannot_drift_a_validated_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dataclasses.replace`로 하나라도 바꾸면 문맥의 문이 다시 닫힌다."""
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    context = direct_context(audio, plan, utterances, fingerprint)
    window = plan.windows[0].window_id
    with pytest.raises(AnalysisApprovalError, match="지문"):
        replace(context, fingerprint="0" * 64)
    with pytest.raises(AnalysisApprovalError, match="지문"):
        replace(context, audio=replace(audio, sample_count=audio.sample_count - 16000))
    with pytest.raises(AnalysisApprovalError, match="해시"):
        replace(context, request_sha256s={window: "0" * 64})
    with pytest.raises(AnalysisApprovalError, match="승인되지 않은"):
        replace(context, approval=replace(context.approval, approved=False))
    with pytest.raises(AnalysisApprovalError, match="창 계획"):
        replace(context, evidence_windows=())
    with pytest.raises(AnalysisApprovalError, match="네트워크 모드"):
        replace(context, settings=LUNA_WORKFLOW)  # 오프라인 설정 — 전송 순간 이전에 닫힌다


def test_request_hash_mapping_is_frozen_against_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청 신원 지도는 얼어붙은 사본이다 — 문맥 밖에서 바꿀 수 없다."""
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    context = direct_context(audio, plan, utterances, fingerprint)
    window = plan.windows[0].window_id
    frozen = context.request_sha256s
    with pytest.raises(TypeError):
        frozen[window] = "0" * 64  # type: ignore[index]
    with pytest.raises(TypeError):
        del frozen[window]  # type: ignore[arg-type]
    assert dict(frozen)[window] == luna_request_sha256(
        online_workflow().models.luna_model,
        build_luna_payload(context.evidence_windows[0], SETTINGS),
    )


def test_responder_rejects_gate_or_approval_not_from_its_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 예산의 새 게이트·모양이 같은 다른 승인 — 문맥이 검증한 객체가 아니면 안 된다."""
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    context = direct_context(audio, plan, utterances, fingerprint)
    model = online_workflow().models.luna_model
    lookalike_gate = CountingReservationGate(context.approval.budget)  # 예산 이중 계산의 길
    with pytest.raises(AnalysisApprovalError, match="예약 문"):
        luna.LunaSemanticResponder(
            object(), model, context.approval, lookalike_gate, context=context
        )
    lookalike_approval = replace(context.approval, source_sha256="c" * 64)  # 승인됐고 모델도 같다
    with pytest.raises(AnalysisApprovalError, match="승인"):
        luna.LunaSemanticResponder(
            object(), model, lookalike_approval, context.gate, context=context
        )


def test_valid_direct_context_still_sends_bounded_fake_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """올바른 직접 구성은 살아 있다 — 검증이 통과되면 전송 경계가 열린다(과잉 차단이 아니다)."""
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    context = direct_context(audio, plan, utterances, fingerprint)
    responder = luna.LunaSemanticResponder(
        object(),
        online_workflow().models.luna_model,
        context.approval,
        context.gate,
        context=context,
    )
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        luna, "request_luna_vision", lambda *a, **k: sent.append(k) or {"output_text": "{}"}
    )
    first = context.evidence_windows[0]
    responder.respond(window_id=first.window.window_id, payload=build_luna_payload(first, SETTINGS))
    assert len(sent) == 1  # 문맥이 검증한 창·페이로드 그대로 — 예약 1회로 정확히 한 번
    assert sent[0]["online"] is True


def test_unverified_registry_still_cannot_reach_the_gate(tmp_path: Path) -> None:
    """설정 자체가 동결 계약을 어기면 승인 문은 열리지 않는다(우회 없음)."""
    audio = make_audio(tmp_path)
    plan = stage1.plan_windows(audio, [], SETTINGS, None, video_status=stage1.VIDEO_ABSENT)
    granted, _, _ = approval(tmp_path, fingerprint="f" * 64)
    gutted = replace(
        online_workflow(), models=RawModelRoles(text_model="", timing_model="", luna_model="")
    )
    with pytest.raises(ValueError):
        require_online_approval(
            gutted, settings=gutted, audio=audio, plan=plan, fingerprint="f" * 64
        )


# ─────────────────────────────────────────────────────────────
#  지문·캐시 — 결과에 영향을 주는 입력만 묶는다
# ─────────────────────────────────────────────────────────────


def full_plan(tmp_path: Path, *, video: VideoGrid | None = None) -> tuple[SelectedAudio, Any]:
    audio = make_audio(tmp_path)
    utterances = [make_utterance(0, 1.0, 4.0)]
    return audio, stage1.plan_windows(audio, utterances, SETTINGS, video)


def test_fingerprint_is_stable_for_identical_inputs(tmp_path: Path) -> None:
    """같은 입력은 같은 지문 — 재개·캐시의 열쇠다(§14.2)."""
    audio, plan = full_plan(tmp_path)
    first = analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    second = analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    assert first == second and len(first) == 64


def test_fingerprint_tracks_every_cost_relevant_input(tmp_path: Path) -> None:
    """출처·프레임 표본·모델·프롬프트가 바뀌면 지문이 달라진다."""
    audio, plan = full_plan(tmp_path)
    base = analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    other_video = VideoGrid(0, Fraction(0), Fraction(1, 50), 1000)  # 표본이 달라진다
    _, replanned = full_plan(tmp_path, video=other_video)
    assert base != analysis_fingerprint(
        audio=audio, plan=replanned, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    assert base != analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m2", luna_model="l"
    )
    assert base != analysis_fingerprint(
        audio=audio, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l2"
    )
    longer = replace(audio, sample_count=audio.sample_count + 16000)
    assert base != analysis_fingerprint(
        audio=longer, plan=plan, settings=SETTINGS, asr_model="m", luna_model="l"
    )
    tuned = replace(SETTINGS, prompt_revision="raw-understanding-v2")
    assert base != analysis_fingerprint(
        audio=audio, plan=plan, settings=tuned, asr_model="m", luna_model="l"
    )


def test_rejected_annotations_never_enter_the_success_cache(tmp_path: Path) -> None:
    """성공 캐시에는 받아들인 판정만 — 거부·실패가 재생되지 않는다(§14.2)."""
    evidence = make_evidence(tmp_path)
    accepted = parse(accepted_body(evidence), evidence)
    rejected = parse(accepted_body(evidence, window_id="window_000999"), evidence)
    failed = SemanticAnnotation(
        window_id="window_000002",
        status=luna.ANNOTATION_FAILED,
        request_sha256="f" * 64,
        luna_model="l",
        prompt_revision="r",
        origin="online_live",
        defect="timeout",
    )
    assert cacheable(accepted) and not cacheable(rejected) and not cacheable(failed)
    document = annotation_cache_document("f" * 64, [accepted, rejected, failed])
    assert document["fingerprint"] == "f" * 64
    assert [item["status"] for item in document["annotations"]] == ["accepted"]


# ─────────────────────────────────────────────────────────────
#  문서 — 재생과 실제 호출이 다른 표시를 가진다
# ─────────────────────────────────────────────────────────────


def annotation_of(window_id: str, origin: str) -> SemanticAnnotation:
    """실행 근원이 확정된 판정 — 문서 모드 파생의 재료."""
    return SemanticAnnotation(
        window_id=window_id,
        status=luna.ANNOTATION_ACCEPTED,
        request_sha256="r" * 64,
        luna_model="l",
        prompt_revision=SETTINGS.prompt_revision,
        origin=origin,
    )


def test_stage1_document_mode_comes_from_execution_evidence(tmp_path: Path) -> None:
    """실행 모드는 판정 근원 증거가 정한다 — 설정 online은 재생을 live로 못 박지 못한다."""
    audio, plan = full_plan(tmp_path)
    asr = AsrEvidence(request_id="run", sample_rate=stage1.PCM_SAMPLE_RATE, chunks=())
    utterances = [make_utterance(0, 1.0, 4.0)]
    frames = FrameSamplingResult((), ())
    verdicts = {"window_000000": WindowVerdict("window_000000", False, (), True, "replay")}
    segmentation = propose_segmentation(plan, verdicts, utterances)

    def document(
        *, workflow: RawWorkflowConfig, annotations: tuple[SemanticAnnotation, ...]
    ) -> Any:
        return stage1_document(
            audio=audio,
            asr=asr,
            utterances=utterances,
            plan=plan,
            frames=frames,
            segmentation=segmentation,
            annotations=annotations,
            settings=SETTINGS,
            workflow=workflow,
            fingerprint="f" * 64,
        )

    # 루트 probe 그대로 — 설정이 online이어도 재생 판정은 replay다
    replay = document(
        workflow=online_workflow(), annotations=(annotation_of("window_000000", "replay"),)
    )
    assert replay["execution_mode"] == "offline_replay"
    # 호출이 없으면(빈 판정) online 설정과 무관하게 replay다
    assert (
        document(workflow=online_workflow(), annotations=())["execution_mode"] == "offline_replay"
    )
    # 실제 호출 판정만 live다 — 설정이 offline로 바뀌어도 증거가 이긴다
    live = document(workflow=LUNA_WORKFLOW, annotations=(annotation_of("window_000000", "live"),))
    assert live["execution_mode"] == "online_live"
    # 섞인 실행은 섞였다고 말한다 — 재생을 live로 묻는 길은 없다
    mixed = document(
        workflow=online_workflow(),
        annotations=(
            annotation_of("window_000000", "replay"),
            annotation_of("window_000001", "live"),
        ),
    )
    assert mixed["execution_mode"] == "mixed_live_and_replay"
    assert live["models"]["asr"] == LUNA_WORKFLOW.models.timing_model


def test_unknown_annotation_origin_cannot_reach_the_document(tmp_path: Path) -> None:
    """근원을 모르는 판정은 진실을 말할 수 없다 — 조립과 파생 양쪽에서 닫는다."""
    evidence = make_evidence(tmp_path)
    replays = {evidence.window.window_id: respond(accepted_body(evidence))}
    with pytest.raises(luna.LunaEvidenceError, match="판정 근원"):
        annotate_windows(
            (evidence,),
            ReplaySemanticResponder(replays),
            settings=SETTINGS,
            model="l",
            origin="cargo",
        )
    with pytest.raises(luna.LunaEvidenceError, match="판정 근원"):
        document_execution_mode((annotation_of("window_000000", "cargo"),))


def test_gate_rejects_non_integer_reservation_counts() -> None:
    """예약 수는 엄격한 양의 정수다 — bool·0·음수·실수·문자열은 받지 않는다."""
    gate = CountingReservationGate(AnalysisBudget(max_luna_requests=3, max_asr_chunks=9))
    for bad in (True, 0, -1, 1.5, "1"):
        with pytest.raises(ValueError):
            gate.reserve(count=bad, key="window_000000")  # type: ignore[arg-type]
    gate.reserve(count=2, key="window_000000")
    assert gate.remaining == 1
