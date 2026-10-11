"""W-14 / D54 — Claude Opus 5.5 의미 분석 페이로드·판정 파싱·응답자 경계.

Luna 경로는 한 바이트도 바뀌지 않는다(요청 신원 고정). Claude 페이로드는 Messages API
모양이고, 판정은 같은 결함 검사(`_annotation_defect`)를 지난다. 전송은 모듈 경계에서
가로챈다 — 유료 호출 0회.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_understanding_luna import (
    SETTINGS,
    accepted_body,
    env_online,
    make_evidence,
    matched_approval,
    online_run,
    online_workflow,
)
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.config import OPENAI_LUNA_MODEL, RawWorkflowConfig, claude_profile
from vlogsrt.project.raw.understanding import FrameEvidence, FrameSamplingResult
from vlogsrt.project.raw.understanding_luna import (
    AnalysisApprovalError,
    OnlineVisualAnalysisApproval,
    SemanticAnnotation,
    WindowEvidence,
    analysis_fingerprint,
    annotate_windows,
    build_claude_payload,
    build_luna_payload,
    build_semantic_payload,
    luna_output_schema,
    luna_request_sha256,
    parse_luna_response,
    semantic_analysis_budget,
    validated_luna_responder,
    validated_visual_luna_responder,
)
from vlogsrt.transcribe import create_claude_client

CLAUDE = "claude-opus-5-5"
LUNA = OPENAI_LUNA_MODEL  # W-18(D58) 전에는 기본값이었다 — 이 파일은 Luna 경로를 그대로 본다
FIXTURES = Path(__file__).parent.parent / "fixtures" / "synthetic" / "claude"
PRE_W14_LUNA_SHA = "d2131a8b1cb40b96dae9da82c1437c769e1d57040e2fd717a4bc395847f124d2"
"""`make_evidence`의 창에 대한 Luna 요청 신원 — 바뀌면 캐시가 깨진다.

W-14·W-18 18a는 이 값을 한 바이트도 바꾸지 않았다(909bc236…3e563c13). 18b(D58)가 안내문의
`recommended` 기준과 schema의 `visual_issues`를 더해 일부러 바꿨다 — 안내문 리비전도 v3로 올라
옛 판정은 어차피 다시 쓰지 않는다. 이름은 추적을 위해 그대로 둔다."""


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def claude_reply(body: dict[str, Any], *, stop_reason: str | None = "end_turn") -> dict[str, Any]:
    return {
        "type": "message",
        "model": CLAUDE,
        "content": [
            {"type": "thinking", "thinking": "{not the answer", "signature": "s"},
            {"type": "text", "text": json.dumps(body, ensure_ascii=False)},
        ],
        "stop_reason": stop_reason,
    }


def parse_with(
    model: str, response: dict[str, Any], evidence: WindowEvidence
) -> SemanticAnnotation:
    return parse_luna_response(
        response,
        evidence=evidence,
        model=model,
        request_sha256="f" * 64,
        origin="offline_replay",
        prompt_revision=SETTINGS.prompt_revision,
    )


def claude_client() -> Any:
    profile = claude_profile(CLAUDE)
    return create_claude_client(
        api_key="sk-ant-synthetic",
        base_url=profile.api_base_url,
        api_version=profile.api_version,
        timeout_seconds=profile.timeout_seconds,
    )


# ─────────────────────────────────────────────────────────────
#  페이로드 — Luna는 그대로, Claude는 Messages 모양
# ─────────────────────────────────────────────────────────────


def test_the_luna_payload_and_request_identity_are_byte_identical(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    images = [
        {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64,"
            + base64.b64encode(frame.image_path.read_bytes()).decode("ascii"),
        }
        for frame in evidence.frames
    ]
    expected = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": luna._instruction(evidence)}, *images],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "raw_understanding_window_v1",
                "strict": True,
                "schema": luna_output_schema(),
            }
        },
    }
    assert build_luna_payload(evidence, SETTINGS) == expected
    semantic = build_semantic_payload(evidence, SETTINGS, LUNA)
    assert semantic == expected
    assert luna_request_sha256(LUNA, semantic) == PRE_W14_LUNA_SHA


def test_the_claude_payload_is_a_messages_body_without_model_or_thinking(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    payload = build_semantic_payload(evidence, SETTINGS, CLAUDE)
    assert set(payload) == {"max_tokens", "messages", "output_config", "inference_geo"}
    # 표준 단가 지역을 명시한다 — US로 묶인 작업공간은 청구 전에 4xx다. service_tier는 싣지
    # 않는다: Opus 5.5에는 우선 등급이 없고, 받아 주는지 문서로 확인되지 않는다(400 위험).
    assert payload["inference_geo"] == "global"
    assert "service_tier" not in payload
    assert payload["max_tokens"] == 8192
    assert payload["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": luna_output_schema()},
    }
    (message,) = payload["messages"]
    assert message["role"] == "user"
    content = message["content"]
    frame_blocks = content[:-1]
    assert len(frame_blocks) == 2 * len(evidence.frames)
    for index, frame in enumerate(evidence.frames):
        label, image = frame_blocks[2 * index], frame_blocks[2 * index + 1]
        assert label == {"type": "text", "text": f"{frame.frame_id}:"}
        assert image == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(frame.image_path.read_bytes()).decode("ascii"),
            },
        }
    assert content[-1] == {"type": "text", "text": luna._instruction(evidence)}
    dumped = json.dumps(payload)
    for forbidden in ("thinking", "tools", "temperature", "system", ".mp4", ".pcm", ".wav"):
        assert forbidden not in dumped


def test_request_identities_differ_by_model_and_by_profile(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    claude_payload = build_semantic_payload(evidence, SETTINGS, CLAUDE)
    claude_sha = luna_request_sha256(CLAUDE, claude_payload)
    assert claude_sha != luna_request_sha256(LUNA, build_semantic_payload(evidence, SETTINGS, LUNA))
    assert claude_sha != luna_request_sha256(LUNA, claude_payload)
    stronger = build_claude_payload(
        evidence, SETTINGS, replace(claude_profile(CLAUDE), effort="high")
    )
    assert luna_request_sha256(CLAUDE, stronger) != claude_sha


# ─────────────────────────────────────────────────────────────
#  판정 파싱 — end_turn의 text만, 결함은 분명한 거부
# ─────────────────────────────────────────────────────────────


def test_an_end_turn_message_is_accepted_and_thinking_is_ignored(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    annotation = parse_with(CLAUDE, fixture("message_accepted.json"), evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED, annotation.defect
    assert annotation.luna_model == CLAUDE
    assert annotation.suitability == "usable"
    assert annotation.visual_issues == ("none",)  # 18b(D58) — 보고 문제 없다고 답했다
    built = parse_with(CLAUDE, claude_reply(accepted_body(evidence)), evidence)
    assert built.status == luna.ANNOTATION_ACCEPTED, built.defect


@pytest.mark.parametrize(
    ("name", "fragments"),
    [
        ("message_max_tokens.json", ("max_tokens", "출력 한도")),
        ("message_refusal.json", ("refusal", "거절")),
    ],
)
def test_cut_or_refused_messages_are_rejected_with_a_clear_defect(
    tmp_path: Path, name: str, fragments: tuple[str, ...]
) -> None:
    annotation = parse_with(CLAUDE, fixture(name), make_evidence(tmp_path))
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert all(fragment in annotation.defect for fragment in fragments), annotation.defect
    assert annotation.suggested_boundary_ids == ()


@pytest.mark.parametrize(
    "stop", ["pause_turn", None, "model_context_window_exceeded", "stop_sequence", "tool_use"]
)
def test_a_valid_body_under_any_other_stop_reason_is_rejected(
    tmp_path: Path, stop: str | None
) -> None:
    """본문이 schema를 지켜도 end_turn이 아니면 판정으로 받지 않는다 — 끝났다는 보장이 없다."""
    evidence = make_evidence(tmp_path)
    annotation = parse_with(
        CLAUDE, claude_reply(accepted_body(evidence), stop_reason=stop), evidence
    )
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert repr(stop) in annotation.defect


def test_a_refusal_names_its_category_when_the_provider_gives_one(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    reply = {
        **claude_reply(accepted_body(evidence), stop_reason="refusal"),
        "stop_details": {"type": "refusal", "category": "bio", "explanation": "synthetic"},
    }
    annotation = parse_with(CLAUDE, reply, evidence)
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert "bio" in annotation.defect
    assert "synthetic" not in annotation.defect  # 설명문은 되뇌지 않는다 — 범주만


def test_only_text_blocks_are_read_whatever_other_blocks_carry(tmp_path: Path) -> None:
    """생각 블록이 `text` 칸을 갖고 와도 판정 본문에 섞이지 않는다 — 블록 종류가 문이다."""
    evidence = make_evidence(tmp_path)
    reply = claude_reply(accepted_body(evidence))
    reply["content"].insert(0, {"type": "thinking", "thinking": "", "text": "{junk"})
    annotation = parse_with(CLAUDE, reply, evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED, annotation.defect


@pytest.mark.parametrize(
    "response",
    [
        {"type": "message", "stop_reason": "end_turn"},
        {"type": "message", "stop_reason": "end_turn", "content": "not a list"},
        {"type": "message", "stop_reason": "end_turn", "content": [{"type": "thinking"}]},
        {"type": "message", "stop_reason": "pause_turn", "content": []},
        {"output_text": "{}"},
    ],
)
def test_malformed_or_luna_shaped_claude_responses_are_rejected(
    tmp_path: Path, response: dict[str, Any]
) -> None:
    annotation = parse_with(CLAUDE, response, make_evidence(tmp_path))
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert annotation.defect


def test_a_claude_shaped_reply_under_the_luna_model_is_still_rejected(tmp_path: Path) -> None:
    """OpenAI 경로는 그대로다 — Messages 모양을 Luna 판정으로 받아 주지 않는다."""
    evidence = make_evidence(tmp_path)
    annotation = parse_with(LUNA, claude_reply(accepted_body(evidence)), evidence)
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert "output_text" in annotation.defect


@pytest.mark.parametrize(("spelled", "expected"), [("Usable", "usable"), ("REVIEW", "review")])
def test_claude_suitability_casing_is_normalized(
    tmp_path: Path, spelled: str, expected: str
) -> None:
    evidence = make_evidence(tmp_path)
    body = accepted_body(evidence, suitability=spelled, suitability_reason="흔들림이 적다")
    annotation = parse_with(CLAUDE, claude_reply(body), evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED, annotation.defect
    assert annotation.suitability == expected
    luna_reply = {"output_text": json.dumps(body, ensure_ascii=False)}
    assert parse_with(LUNA, luna_reply, evidence).status == luna.ANNOTATION_REJECTED


def test_saved_annotations_are_never_reused_across_models(tmp_path: Path) -> None:
    """같은 창이어도 다른 모델의 판정은 다시 청구하지 않는 근거가 아니다 — 양방향."""
    evidence = make_evidence(tmp_path)
    luna_sha = luna_request_sha256(LUNA, build_semantic_payload(evidence, SETTINGS, LUNA))
    claude_sha = luna_request_sha256(CLAUDE, build_semantic_payload(evidence, SETTINGS, CLAUDE))
    luna_prior = replace(
        parse_with(LUNA, {"output_text": json.dumps(accepted_body(evidence))}, evidence),
        request_sha256=luna_sha,
    )
    claude_prior = replace(
        parse_with(CLAUDE, claude_reply(accepted_body(evidence)), evidence),
        request_sha256=claude_sha,
    )
    for prior, model, sha in ((luna_prior, CLAUDE, claude_sha), (claude_prior, LUNA, luna_sha)):
        assert not luna._reusable(
            prior, sha, settings=SETTINGS, model=model, origin="offline_replay"
        )
        assert _asked_again(evidence, prior, model) == [evidence.window.window_id]


def _asked_again(evidence: WindowEvidence, prior: SemanticAnnotation, model: str) -> list[str]:
    """다른 모델의 저장 판정을 캐시로 주고 판정을 받는다 — 응답자에게 물은 창을 돌려준다."""
    asked: list[str] = []

    class Recorder:
        def respond(self, *, window_id: str, payload: Any) -> Any:
            asked.append(window_id)
            if model == CLAUDE:
                return claude_reply(accepted_body(evidence))
            return {"output_text": json.dumps(accepted_body(evidence))}

    (annotation,) = annotate_windows(
        [evidence],
        Recorder(),
        settings=SETTINGS,
        model=model,
        origin="offline_replay",
        cached={evidence.window.window_id: prior},
    )
    assert annotation.luna_model == model
    return asked


# ─────────────────────────────────────────────────────────────
#  응답자 — 클라이언트 종류가 공급자와 맞아야 존재한다
# ─────────────────────────────────────────────────────────────


def claude_workflow() -> RawWorkflowConfig:
    workflow = online_workflow()
    return replace(workflow, models=replace(workflow.models, luna_model=CLAUDE))


def claude_run(tmp_path: Path) -> tuple[Any, Any, Any, Any]:
    """Claude 모델로 잰 지문과 공식 단가 예산의 승인 — 검증 문맥의 출발점."""
    audio, plan, utterances, _ = online_run(tmp_path)
    workflow = claude_workflow()
    fingerprint = analysis_fingerprint(
        audio=audio,
        plan=plan,
        settings=SETTINGS,
        asr_model=workflow.models.timing_model,
        luna_model=CLAUDE,
    )
    budget = semantic_analysis_budget(
        CLAUDE, SETTINGS, max_luna_requests=len(plan.windows), max_asr_chunks=9
    )
    approval = replace(matched_approval(audio, plan, fingerprint), luna_model=CLAUDE, budget=budget)
    return audio, plan, utterances, approval


def test_a_luna_responder_refuses_a_claude_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    with pytest.raises(AnalysisApprovalError, match="클라이언트"):
        validated_luna_responder(
            client=claude_client(),
            settings=online_workflow(),
            understanding=SETTINGS,
            audio=audio,
            plan=plan,
            utterances=utterances,
            frames=FrameSamplingResult((), ()),
            approval=matched_approval(audio, plan, fingerprint),
        )


def test_a_claude_responder_refuses_a_non_claude_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    audio, plan, utterances, approval = claude_run(tmp_path)
    with pytest.raises(AnalysisApprovalError, match="클라이언트"):
        validated_luna_responder(
            client=object(),
            settings=claude_workflow(),
            understanding=SETTINGS,
            audio=audio,
            plan=plan,
            utterances=utterances,
            frames=FrameSamplingResult((), ()),
            approval=approval,
        )


def test_a_validated_claude_responder_sends_through_the_messages_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    audio, plan, utterances, approval = claude_run(tmp_path)
    responder = validated_luna_responder(
        client=claude_client(),
        settings=claude_workflow(),
        understanding=SETTINGS,
        audio=audio,
        plan=plan,
        utterances=utterances,
        frames=FrameSamplingResult((), ()),
        approval=approval,
    )
    windows = responder.context.evidence_windows
    by_id = {item.window.window_id: item for item in windows}
    sent: list[dict[str, Any]] = []

    def fake_claude(client: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append({"client": client, **kwargs})
        instruction = kwargs["payload"]["messages"][0]["content"][-1]["text"]
        window_id = instruction.split("window_id: ")[1].splitlines()[0]
        return claude_reply(accepted_body(by_id[window_id]))

    def no_luna(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Claude 모델이 Responses 경계로 새면 안 된다")

    monkeypatch.setattr(luna, "request_claude_messages", fake_claude)
    monkeypatch.setattr(luna, "request_luna_vision", no_luna)
    annotations = annotate_windows(
        windows, responder, settings=SETTINGS, model=CLAUDE, origin="live"
    )
    assert all(item.status == luna.ANNOTATION_ACCEPTED for item in annotations)
    assert all(item.luna_model == CLAUDE for item in annotations)
    assert len(sent) == len(plan.windows)
    assert all(call["online"] is True and call["model"] == CLAUDE for call in sent)
    assert all(call["client"] is responder.client for call in sent)


# ─────────────────────────────────────────────────────────────
#  무성 원본 응답자 — 같은 클라이언트 종류 문
# ─────────────────────────────────────────────────────────────


def sof_jpeg(width: int, height: int) -> bytes:
    """SOF0 헤더만 있는 최소 JPEG — 상한 계산이 읽는 칸(가로·세로)만 진짜다."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def visual_frames(
    plan: Any, directory: Path, *, width: int = 768, height: int = 432
) -> FrameSamplingResult:
    """계획된 프레임을 전부 SOF 헤더가 있는 JPEG로 — 분석 프레임의 실제 크기(16:9, 긴 변 768)."""
    frames = []
    for item in {p.frame_id: p for w in plan.windows for p in w.frame_plans}.values():
        body = sof_jpeg(width, height)
        path = directory / f"{item.frame_id}.jpg"
        path.write_bytes(body)
        frames.append(
            FrameEvidence(
                frame_id=item.frame_id,
                frame_index=item.frame_index,
                image_path=path,
                image_sha256=hashlib.sha256(body).hexdigest(),
                file_time=Fraction(item.frame_index, 25),
            )
        )
    return FrameSamplingResult(tuple(frames), ())


def visual_responder(
    tmp_path: Path,
    model: str,
    client: object,
    *,
    ceiling: Decimal | None = None,
    frames: FrameSamplingResult | None = None,
) -> Any:
    """무성 원본의 검증된 응답자 — 모델에 맞는 예산(Claude는 공식 상한)으로 만든다."""
    from tests.raw.test_analysis_approval import _grid, _visual_plan

    video, plan = _grid(), _visual_plan()
    fingerprint = luna.visual_analysis_fingerprint(
        source_sha256="a" * 64, video=video, plan=plan, settings=SETTINGS, luna_model=model
    )
    budget = semantic_analysis_budget(
        model, SETTINGS, max_luna_requests=len(plan.windows), max_asr_chunks=1
    )
    if ceiling is not None:
        budget = replace(budget, request_ceiling_usd=ceiling)
    approval = OnlineVisualAnalysisApproval(
        approved=True,
        source_sha256="a" * 64,
        frame_count=video.frame_count,
        window_count=len(plan.windows),
        fingerprint=fingerprint,
        luna_model=model,
        budget=budget,
    )
    return validated_visual_luna_responder(
        client=client,
        settings=claude_workflow() if model == CLAUDE else online_workflow(),
        understanding=SETTINGS,
        source_sha256="a" * 64,
        video=video,
        plan=plan,
        frames=frames if frames is not None else visual_frames(plan, tmp_path),
        approval=approval,
    )


def test_a_visual_claude_responder_refuses_a_non_claude_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    with pytest.raises(AnalysisApprovalError, match="클라이언트"):
        visual_responder(tmp_path, CLAUDE, object())
    assert visual_responder(tmp_path, CLAUDE, claude_client()).model == CLAUDE


def test_a_visual_luna_responder_refuses_a_claude_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    with pytest.raises(AnalysisApprovalError, match="클라이언트"):
        visual_responder(tmp_path, LUNA, claude_client())
    assert visual_responder(tmp_path, LUNA, object()).model == LUNA
