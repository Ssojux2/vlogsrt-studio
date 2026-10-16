"""A4-F1 승인 계약 — 1단계 전사 승인·시각(무성) 승인의 형식이 문을 지킨다.

유료 호출은 없다(환경 오프라인 우회 금지 — 시험은 `is_offline` 함수만 갈아끼운다).
이 묶음이 증명하는 것:

- 1단계 승인은 전사 전에 알 수 있는 값만 묶고, 환경·네트워크·출처·모델·설정
  지문·수명주기 중 하나라도 어긋나면 문을 열지 않는다(§14.3).
- 시각 지문은 오디오 지문과 schema부터 다르다 — 무성 원본을 오디오 승인으로
  우회하거나 그 반대가 되는 길이 없다.
- 시각 승인 문맥도 스스로 증명한다 — 검증 문맥 없는 전송 경계는 존재하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    asr_settings_fingerprint,
    require_live_asr_approval,
)
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import OPENAI_LUNA_MODEL, RawModelRoles, RawWorkflowConfig
from vlogsrt.project.raw.understanding_luna import (
    AnalysisApprovalError,
    AnalysisBudget,
    OnlineAnalysisApproval,
    OnlineVisualAnalysisApproval,
)


def _online_workflow() -> RawWorkflowConfig:
    """Luna(OpenAI) 경로의 온라인 설정 — W-18(D58) 전에는 기본값이 곧 이 모델이었다.

    이 파일의 2단계 시험은 OpenAI 가짜 클라이언트로 Luna 문을 지난다. 새 기본(Sonnet 5.5)은
    Claude 문(공식 단가 상한이 묶인 승인)을 요구하므로 모델을 명시해 같은 경로를 계속 본다.
    """
    luna = replace(RawModelRoles(), luna_model=OPENAI_LUNA_MODEL)
    return replace(RawWorkflowConfig(), models=luna, network_mode="online")


def _asr_approval(workflow: RawWorkflowConfig, **overrides) -> AsrPhaseApproval:
    settings = RawUnderstandingSettings()
    values = {
        "approved": True,
        "source_sha256": "a" * 64,
        "audio_stream_index": 1,
        "chunk_budget": 2,
        "settings_fingerprint": asr_settings_fingerprint(settings, workflow),
        "timing_model": workflow.models.timing_model,
        "approved_on": date(2026, 1, 1).isoformat(),
    }
    values.update(overrides)
    return AsrPhaseApproval(**values)


def _grid(frame_count: int = 90) -> stage1.VideoGrid:
    return stage1.VideoGrid(
        stream_index=0,
        presentation_origin=Fraction(0),
        frame_duration=Fraction(1, 25),
        frame_count=frame_count,
    )


def _audio() -> stage1.SelectedAudio:
    return stage1.SelectedAudio(
        source_id="s",
        source_path=Path("originals/x.mov"),
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=Path("analysis.pcm"),
        pcm_sha256="b" * 64,
        sample_rate=16000,
        channels=2,
        sample_count=96000,
        pcm_sample0_file_time=Fraction(1, 5),
    )


# ── 1단계(전사) 승인 — 설정 지문과 시작 문 ────────────────────────────────────


def test_asr_settings_fingerprint_binds_settings_and_models() -> None:
    """설정 지문은 같은 입력에 같고, 설정·전사 모델이 바뀌면 다르다(§14.2).

    의미 분석 모델은 묶지 않는다 — 전사는 그 모델을 듣지 않는다(D54에 적은, 기대값을 바꾼 시험).
    """
    workflow = _online_workflow()
    settings = RawUnderstandingSettings()
    same = asr_settings_fingerprint(settings, workflow)
    assert same == asr_settings_fingerprint(RawUnderstandingSettings(), workflow)
    assert same != asr_settings_fingerprint(RawUnderstandingSettings(window_seconds=11.0), workflow)
    assert same != asr_settings_fingerprint(
        settings, replace(workflow, models=replace(workflow.models, timing_model="whisper-2"))
    )
    assert same == asr_settings_fingerprint(
        settings, replace(workflow, models=replace(workflow.models, luna_model="luna-2"))
    )


def test_require_live_asr_approval_opens_only_the_full_contract(monkeypatch) -> None:
    """환경·네트워크·출처·모델·설정 지문·수명주기 — 하나라도 어긋나면 닫힌다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    workflow = _online_workflow()
    settings = RawUnderstandingSettings()
    approval = _asr_approval(workflow)
    # 전부 열려 있으면 지나간다.
    require_live_asr_approval(
        approval,
        workflow=workflow,
        understanding=settings,
        source_sha256="a" * 64,
        on=date(2026, 1, 1),
    )
    closed_cases = [
        (_asr_approval(workflow, approved=False), "승인되지 않은"),
        (_asr_approval(workflow, source_sha256="f" * 64), "본 원본과 지금의 원본이 다르다"),
        (_asr_approval(workflow, timing_model="stt-x"), "본 ASR 모델과 설정 모델이 다르다"),
        (
            _asr_approval(workflow, settings_fingerprint="0" * 64),
            "설정 지문과 지금의 설정 지문이 다르다",
        ),
        (_asr_approval(workflow, chunk_budget=0), "청크 예산은 1 이상"),
    ]
    for wrong, message in closed_cases:
        with pytest.raises(AnalysisApprovalError, match=message):
            require_live_asr_approval(
                wrong,
                workflow=workflow,
                understanding=settings,
                source_sha256="a" * 64,
                on=date(2026, 1, 1),
            )
    # 잘못된 형식의 레코드는 애초에 승인이 아니다.
    with pytest.raises(AnalysisApprovalError, match="AsrPhaseApproval이 아니다"):
        require_live_asr_approval(
            OnlineAnalysisApproval(  # type: ignore[arg-type]
                approved=True,
                source_sha256="a" * 64,
                audio_stream_index=1,
                sample_count=1,
                window_count=1,
                fingerprint="x",
                asr_model="m",
                luna_model="l",
                budget=AnalysisBudget(max_luna_requests=1, max_asr_chunks=1),
            ),
            workflow=workflow,
            understanding=settings,
            source_sha256="a" * 64,
            on=date(2026, 1, 1),
        )
    # 승인 날짜 모양 자체가 계약이다.
    with pytest.raises(AnalysisApprovalError, match="ISO 날짜가 아니다"):
        _asr_approval(workflow, approved_on="어제").approved_on_date()

    # 환경 오프라인 — 설정이 online이어도 닫힌다(§14.3).
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: True)
    with pytest.raises(AnalysisApprovalError, match="오프라인"):
        require_live_asr_approval(
            approval,
            workflow=workflow,
            understanding=settings,
            source_sha256="a" * 64,
            on=date(2026, 1, 1),
        )
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    # 네트워크 모드 offline.
    with pytest.raises(AnalysisApprovalError, match="네트워크 모드"):
        require_live_asr_approval(
            approval,
            workflow=RawWorkflowConfig(),
            understanding=settings,
            source_sha256="a" * 64,
            on=date(2026, 1, 1),
        )
    # 종료 예정일 이후 — 수명주기 문이 먼저 닫힌다(§11.10).
    with pytest.raises(luna.AnalysisApprovalError, match="종료 예정일"):
        require_live_asr_approval(
            approval,
            workflow=workflow,
            understanding=settings,
            source_sha256="a" * 64,
            on=date(2027, 3, 1),
        )


# ── 시각(무성) 지문·승인 — 오디오 지문과 우연히 같아지는 길이 없다 ─────────────


def _visual_plan(frame_count: int = 90) -> stage1.WindowPlan:
    timeline = stage1.common_timeline(source_id="s", video=_grid(frame_count), audio=None)
    return stage1.plan_common_windows(timeline, (), RawUnderstandingSettings())


def _planned_frames(plan: stage1.WindowPlan, directory: Path) -> stage1.FrameSamplingResult:
    """계획된 프레임을 전부 뽑힌 것으로 — 프레임 없는 창은 Luna로 가지 않는다(frames_missing)."""
    frames = []
    for item in {p.frame_id: p for w in plan.windows for p in w.frame_plans}.values():
        path = directory / f"{item.frame_id}.jpg"
        path.write_bytes(b"\xff\xd8fakejpeg")
        frames.append(
            stage1.FrameEvidence(
                frame_id=item.frame_id,
                frame_index=item.frame_index,
                image_path=path,
                image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                file_time=Fraction(item.frame_index, 25),
            )
        )
    return stage1.FrameSamplingResult(tuple(frames), ())


def test_visual_fingerprint_is_not_audio_fingerprint() -> None:
    """같은 원본·창 계획이어도 시각 지문과 오디오 지문은 다른 값이다."""
    settings = RawUnderstandingSettings()
    workflow = _online_workflow()
    plan = _visual_plan()
    audio = _audio()
    timeline = stage1.common_timeline(source_id="s", video=_grid(), audio=audio)
    audio_plan = stage1.plan_common_windows(timeline, (), settings)
    visual = luna.visual_analysis_fingerprint(
        source_sha256="a" * 64,
        video=_grid(),
        plan=plan,
        settings=settings,
        luna_model=workflow.models.luna_model,
    )
    audio_fp = luna.analysis_fingerprint(
        audio=audio,
        plan=audio_plan,
        settings=settings,
        asr_model=workflow.models.timing_model,
        luna_model=workflow.models.luna_model,
    )
    assert visual != audio_fp
    # 격자·창이 바뀌면 시각 지문도 바뀐다.
    assert visual != luna.visual_analysis_fingerprint(
        source_sha256="a" * 64,
        video=_grid(91),
        plan=_visual_plan(91),
        settings=settings,
        luna_model=workflow.models.luna_model,
    )
    assert visual != luna.visual_analysis_fingerprint(
        source_sha256="f" * 64,
        video=_grid(),
        plan=plan,
        settings=settings,
        luna_model=workflow.models.luna_model,
    )


def test_require_online_visual_approval_checks_every_binding(monkeypatch) -> None:
    """시각 승인의 문 — 출처·프레임 수·창 수·지문·모델·예산을 전부 본다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    workflow = _online_workflow()
    settings = RawUnderstandingSettings()
    video = _grid()
    plan = _visual_plan()
    fingerprint = luna.visual_analysis_fingerprint(
        source_sha256="a" * 64,
        video=video,
        plan=plan,
        settings=settings,
        luna_model=workflow.models.luna_model,
    )

    def _approval(**overrides) -> OnlineVisualAnalysisApproval:
        values = {
            "approved": True,
            "source_sha256": "a" * 64,
            "frame_count": video.frame_count,
            "window_count": len(plan.windows),
            "fingerprint": fingerprint,
            "luna_model": workflow.models.luna_model,
            "budget": AnalysisBudget(max_luna_requests=len(plan.windows), max_asr_chunks=1),
        }
        values.update(overrides)
        return OnlineVisualAnalysisApproval(**values)

    def _require(approval) -> None:
        luna.require_online_visual_approval(
            approval,
            settings=workflow,
            source_sha256="a" * 64,
            video=video,
            plan=plan,
            fingerprint=fingerprint,
        )

    _require(_approval())  # 전부 일치하면 지나간다
    cases = [
        (_approval(fingerprint="f" * 64), "시각 지문과 실행 지문이 다르다"),
        (_approval(source_sha256="f" * 64), "본 원본과 실행 대상 원본이 다르다"),
        (_approval(frame_count=video.frame_count + 1), "프레임 수"),
        (_approval(window_count=len(plan.windows) + 1), "창 수"),
        (_approval(luna_model="luna-2"), "Luna 모델과 설정 모델이 다르다"),
        (
            _approval(budget=AnalysisBudget(max_luna_requests=1, max_asr_chunks=1))
            if len(plan.windows) > 1
            else _approval(approved=False),
            "승인되지 않은" if len(plan.windows) <= 1 else "요청 한도",
        ),
    ]
    for wrong, message in cases:
        with pytest.raises(AnalysisApprovalError, match=message):
            _require(wrong)
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: True)
    with pytest.raises(AnalysisApprovalError, match="오프라인"):
        _require(_approval())


def test_visual_responder_needs_validated_context_and_exact_payload(
    monkeypatch, tmp_path: Path
) -> None:
    """시각 전송 경계 — 검증 문맥 없이는 못 만들고, 페이로드가 어긋나면 안 간다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    workflow = _online_workflow()
    settings = RawUnderstandingSettings()
    video = _grid()
    plan = _visual_plan()
    fingerprint = luna.visual_analysis_fingerprint(
        source_sha256="a" * 64,
        video=video,
        plan=plan,
        settings=settings,
        luna_model=workflow.models.luna_model,
    )
    approval = OnlineVisualAnalysisApproval(
        approved=True,
        source_sha256="a" * 64,
        frame_count=video.frame_count,
        window_count=len(plan.windows),
        fingerprint=fingerprint,
        luna_model=workflow.models.luna_model,
        budget=AnalysisBudget(max_luna_requests=len(plan.windows), max_asr_chunks=1),
    )

    class _Client:
        def __init__(self) -> None:
            self.calls = 0
            self.responses = SimpleNamespace(create=self._create)

        def _create(self, **payload):
            self.calls += 1
            window_id = (
                str(payload["input"][0]["content"][0]["text"])
                .split("window_id: ")[1]
                .splitlines()[0]
            )
            body = {
                "window_id": window_id,
                "speech_summary": "",
                "visual_summary": "실내 풍경",
                "scene_tags": ["indoor"],
                "evidence_ids": [window_id],
                "suggested_boundary_ids": [],
                "editing_notes": [],
                "needs_review": False,
                "uncertainties": [],
            }
            return {"output_text": json.dumps(body, ensure_ascii=False)}

    client = _Client()
    responder = luna.validated_visual_luna_responder(
        client=client,
        settings=workflow,
        understanding=settings,
        source_sha256="a" * 64,
        video=video,
        plan=plan,
        frames=_planned_frames(plan, tmp_path),
        approval=approval,
    )
    annotations = luna.annotate_windows(
        responder.context.evidence_windows,
        responder,
        settings=settings,
        model=workflow.models.luna_model,
        origin="live",
    )
    assert client.calls == len(plan.windows)  # 창마다 실제(가짜) 호출
    assert all(item.status == "accepted" for item in annotations)
    assert annotations[0].origin == "live"

    # 위조 페이로드 — 검증된 요청 신원과 다르면 전송 전에 닫힌다.
    with pytest.raises(AnalysisApprovalError, match="페이로드가 검증된 요청 신원과 다르다"):
        responder.respond(
            window_id=plan.windows[0].window_id, payload={"input": [{"forged": True}]}
        )
    # 문맥 밖 승인 — 같은 값의 다른 객체로는 전송 경계를 못 만든다.
    lookalike = OnlineVisualAnalysisApproval(
        approved=approval.approved,
        source_sha256=approval.source_sha256,
        frame_count=approval.frame_count,
        window_count=approval.window_count,
        fingerprint=approval.fingerprint,
        luna_model=approval.luna_model,
        budget=approval.budget,
    )
    with pytest.raises(AnalysisApprovalError, match="문맥 밖 승인은 없다"):
        luna.VisualLunaSemanticResponder(
            client=client,
            model=workflow.models.luna_model,
            approval=lookalike,
            gate=responder.gate,
            context=responder.context,
        )
