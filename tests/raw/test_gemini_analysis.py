"""W-30c2 — Gemini 의미 분석 경로(D86): 클립 + 프레임 본문, 판정, 응답자, 예산, 영수증, 클라이언트.

사용자 지시(2026-10-25): 「주로 Flash를 이용해서 처리 · 영상을 직접 모델이 보고 평가해도 돼」 —
창마다 클립(그림 + 소리, D85) + 지금의 프레임 여덟 장. 전송은 모듈 경계에서 가로챈다 — 유료 호출
0회.

시험이 못 박는 것:

- Gemini 본문은 클립 → 프레임 → 안내문 차례이고, 부분마다 해상도가 고정이며 클립은 길이를 적는다 —
  그래서 견적 상한이 잴 수 있다. 클립이 없는 창은 프레임만으로 묻고 안내문도 그렇게 말한다.
- 클립은 Gemini에만 간다 — Claude·Luna 본문(과 그 요청 신원)은 클립이 있어도 한 바이트도 다르지
  않다.
- 판정은 STOP 후보의 생각 아닌 text만 받고, 잘림·거절·막힘은 이유를 남긴 거부다.
- 응답자는 Gemini 클라이언트만 받고 Gemini 문으로만 보낸다. 공식 단가 상한이 묶인 승인이어야 서고,
  알린 인상일 뒤(가격 미정)나 인라인 상한을 넘는 창이 있으면 서지 않는다.
- 영수증은 Gemini 단가로 잰다. 키가 없으면 유료 호출 전에 `GEMINI_API_KEY`를 말하며 닫힌다.
- 작업 흐름은 Gemini일 때만 창 클립을 자르고, 파일 시각 = 창 시각 + 영상 원점이다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw.test_claude_payload import claude_client, parse_with
from tests.raw.test_understanding_luna import (
    SETTINGS,
    accepted_body,
    env_online,
    make_evidence,
    matched_approval,
    online_run,
    online_workflow,
)
from vlogsrt.project.raw import analysis_workflow, gemini_cost, media_refs, request_log
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport
from vlogsrt.project.raw.browser_runtime import (
    EnvironmentCredentials,
    GeminiCredentials,
    capture_gemini_credentials,
    live_transport_for_credentials,
)
from vlogsrt.project.raw.config import (
    GEMINI_ANALYSIS_PROFILES,
    GEMINI_FLASH_ANALYSIS_PROFILE,
    RawWorkflowConfig,
)
from vlogsrt.project.raw.request_usage import UsageObserver
from vlogsrt.project.raw.understanding import FrameSamplingResult
from vlogsrt.project.raw.understanding_luna import (
    AnalysisApprovalError,
    ClipEvidence,
    WindowEvidence,
    analysis_fingerprint,
    annotate_windows,
    build_claude_payload,
    build_gemini_payload,
    build_luna_payload,
    build_semantic_payload,
    luna_output_schema,
    luna_request_sha256,
    semantic_analysis_budget,
    validated_luna_responder,
)
from vlogsrt.transcribe import GENERATE_CONTENT_OPERATION, create_gemini_client

FLASH = "gemini-3.8-flash"
PROFILE = GEMINI_FLASH_ANALYSIS_PROFILE
TODAY = date(2026, 10, 25)
CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42synthetic-clip"


def with_clip(tmp_path: Path, evidence: WindowEvidence | None = None) -> WindowEvidence:
    base = evidence if evidence is not None else make_evidence(tmp_path)
    path = tmp_path / "clip.mp4"
    path.write_bytes(CLIP_BYTES)
    window = base.window
    seconds = window.context_end - window.context_start
    return replace(base, clip=ClipEvidence(path, window.context_start, seconds))


def gemini_client() -> Any:
    return create_gemini_client(
        api_key="AIza-synthetic",
        base_url=PROFILE.api_base_url,
        api_version=PROFILE.api_version,
        timeout_seconds=PROFILE.timeout_seconds,
    )


def gemini_reply(body: dict[str, Any], *, finish: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "{생각 — 판정이 아니다", "thought": True},
                        {"text": json.dumps(body, ensure_ascii=False)},
                    ],
                },
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        "modelVersion": FLASH,
        "responseId": "resp_1",
    }


# ── 본문 ─────────────────────────────────────────────────────────────────


def test_the_gemini_body_is_clip_then_frames_then_instruction(tmp_path: Path) -> None:
    evidence = with_clip(tmp_path)
    payload = build_gemini_payload(evidence, SETTINGS, PROFILE)
    assert set(payload) == {"contents", "generationConfig"}  # 모델은 전송 경계의 인자다
    (turn,) = payload["contents"]  # type: ignore[misc]
    parts = turn["parts"]
    assert turn["role"] == "user"
    assert parts[0]["text"].startswith("clip: 문맥 구간")
    video = parts[1]
    assert video["inlineData"]["mimeType"] == "video/mp4"
    assert video["mediaResolution"] == {"level": "MEDIA_RESOLUTION_HIGH"}
    seconds = evidence.clip.seconds  # type: ignore[union-attr]
    assert video["videoMetadata"] == {
        "startOffset": "0s",
        "endOffset": f"{float(seconds):.3f}s",
        "fps": 1,
    }
    frames = parts[2:-1]
    assert [p["text"] for p in frames[0::2]] == [f"{f.frame_id}:" for f in evidence.frames]
    assert all(p["mediaResolution"] == {"level": "MEDIA_RESOLUTION_HIGH"} for p in frames[1::2])
    instruction = parts[-1]["text"]
    assert "clip(그림과 소리" in instruction and "clip으로 판단하라" in instruction
    assert "오디오·영상 파일은 주어지지 않습니다" not in instruction
    assert payload["generationConfig"] == {
        "maxOutputTokens": PROFILE.max_output_tokens,
        "responseMimeType": "application/json",
        "responseJsonSchema": luna.gemini_output_schema(),
        "thinkingConfig": {"thinkingLevel": "MEDIUM"},
    }


def test_a_window_without_a_clip_asks_with_frames_only(tmp_path: Path) -> None:
    payload = build_gemini_payload(make_evidence(tmp_path), SETTINGS, PROFILE)
    parts = payload["contents"][0]["parts"]  # type: ignore[index]
    assert not any("videoMetadata" in part for part in parts)
    assert "오디오·영상 파일은 주어지지 않습니다" in parts[-1]["text"]
    assert "clip으로 판단하라" not in parts[-1]["text"]


def test_the_body_fits_the_window_ceiling_the_quote_shows(tmp_path: Path) -> None:
    payload = build_gemini_payload(with_clip(tmp_path), SETTINGS, PROFILE)
    bound = gemini_cost.payload_bound(payload, PROFILE, on=TODAY)
    ceiling = gemini_cost.window_ceiling_usd(
        PROFILE, SETTINGS, schema_bytes=luna.gemini_output_schema_bytes(), on=TODAY
    )
    assert ceiling is not None and bound.usd <= ceiling


def test_clips_go_to_gemini_only(tmp_path: Path) -> None:
    """Claude·Luna 본문과 요청 신원은 클립이 있어도 그대로다 — 판정 캐시가 흔들리지 않는다."""
    plain = make_evidence(tmp_path)
    clipped = with_clip(tmp_path, plain)
    from vlogsrt.project.raw.config import claude_profile

    assert build_luna_payload(clipped, SETTINGS) == build_luna_payload(plain, SETTINGS)
    sonnet = claude_profile("claude-sonnet-5-5")
    assert build_claude_payload(clipped, SETTINGS, sonnet) == build_claude_payload(
        plain, SETTINGS, sonnet
    )


def test_request_identities_differ_by_provider_and_follow_the_clip_bytes(tmp_path: Path) -> None:
    evidence = with_clip(tmp_path)
    gemini = build_semantic_payload(evidence, SETTINGS, FLASH)
    claude = build_semantic_payload(evidence, SETTINGS, "claude-sonnet-5-5")
    assert luna_request_sha256(FLASH, gemini) != luna_request_sha256("claude-sonnet-5-5", claude)
    again = build_semantic_payload(evidence, SETTINGS, FLASH)
    assert luna_request_sha256(FLASH, gemini) == luna_request_sha256(FLASH, again)
    evidence.clip.path.write_bytes(CLIP_BYTES + b"x")  # type: ignore[union-attr]
    changed = build_semantic_payload(evidence, SETTINGS, FLASH)
    assert luna_request_sha256(FLASH, changed) != luna_request_sha256(FLASH, gemini)


# ── 판정 ─────────────────────────────────────────────────────────────────


def test_a_stop_candidate_is_accepted_and_thought_parts_are_ignored(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    annotation = parse_with(FLASH, gemini_reply(accepted_body(evidence)), evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED
    assert annotation.luna_model == FLASH


@pytest.mark.parametrize(
    ("finish", "words"),
    [("MAX_TOKENS", "출력 한도"), ("SAFETY", "SAFETY"), ("RECITATION", "RECITATION"), ("", "''")],
)
def test_cut_or_refused_candidates_are_rejected_with_their_reason(
    tmp_path: Path, finish: str, words: str
) -> None:
    evidence = make_evidence(tmp_path)
    annotation = parse_with(FLASH, gemini_reply(accepted_body(evidence), finish=finish), evidence)
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert words in (annotation.defect or "")


def test_a_blocked_prompt_without_candidates_is_rejected(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    blocked = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "candidates": []}
    annotation = parse_with(FLASH, blocked, evidence)
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert "PROHIBITED_CONTENT" in (annotation.defect or "")


def test_only_thought_parts_mean_an_empty_verdict(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    reply = gemini_reply(accepted_body(evidence))
    reply["candidates"][0]["content"]["parts"] = [{"text": "{}", "thought": True}]
    assert parse_with(FLASH, reply, evidence).status == luna.ANNOTATION_REJECTED


def test_gemini_suitability_casing_is_normalized(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    body = accepted_body(
        evidence, suitability="Usable", suitability_reason="합성", visual_issues=["None"]
    )
    annotation = parse_with(FLASH, gemini_reply(body), evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED
    assert annotation.suitability == "usable"


# ── 응답자 ───────────────────────────────────────────────────────────────


def gemini_workflow() -> RawWorkflowConfig:
    workflow = online_workflow()
    return replace(workflow, models=replace(workflow.models, luna_model=FLASH))


def gemini_run(tmp_path: Path, *, on: date = TODAY) -> tuple[Any, Any, Any, Any]:
    audio, plan, utterances, _ = online_run(tmp_path)
    fingerprint = analysis_fingerprint(
        audio=audio,
        plan=plan,
        settings=SETTINGS,
        asr_model=gemini_workflow().models.timing_model,
        luna_model=FLASH,
    )
    budget = semantic_analysis_budget(
        FLASH, SETTINGS, max_luna_requests=len(plan.windows), max_asr_chunks=9, on=on
    )
    approval = replace(matched_approval(audio, plan, fingerprint), luna_model=FLASH, budget=budget)
    return audio, plan, utterances, approval


def clips_for(tmp_path: Path, plan: Any) -> dict[str, ClipEvidence]:
    clips = {}
    for window in plan.windows:
        path = tmp_path / f"{window.window_id}.mp4"
        path.write_bytes(CLIP_BYTES + window.window_id.encode())
        clips[window.window_id] = ClipEvidence(
            path, window.context_start, window.context_end - window.context_start
        )
    return clips


def responder_for(tmp_path: Path, client: Any, **overrides: Any) -> Any:
    audio, plan, utterances, approval = gemini_run(tmp_path)
    arguments: dict[str, Any] = {
        "client": client,
        "settings": gemini_workflow(),
        "understanding": SETTINGS,
        "audio": audio,
        "plan": plan,
        "utterances": utterances,
        "frames": FrameSamplingResult((), ()),
        "approval": approval,
        "clips": clips_for(tmp_path, plan),
    }
    arguments.update(overrides)
    return validated_luna_responder(**arguments)


def test_the_budget_binds_the_official_ceiling_until_the_announced_raise() -> None:
    official = semantic_analysis_budget(
        FLASH, SETTINGS, max_luna_requests=3, max_asr_chunks=1, on=TODAY
    )
    assert official.luna_price_status == luna.PRICE_OFFICIAL
    assert official.request_ceiling_usd == gemini_cost.window_ceiling_usd(
        PROFILE, SETTINGS, schema_bytes=luna.gemini_output_schema_bytes(), on=TODAY
    )
    late = semantic_analysis_budget(
        FLASH, SETTINGS, max_luna_requests=3, max_asr_chunks=1, on=date(2027, 1, 1)
    )
    assert late.luna_price_status == luna.PRICE_UNKNOWN and late.request_ceiling_usd is None


@pytest.mark.parametrize("client", ["claude", "object"])
def test_a_gemini_responder_refuses_other_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    env_online(monkeypatch)
    other = claude_client() if client == "claude" else object()
    with pytest.raises(AnalysisApprovalError, match="Gemini generateContent 클라이언트"):
        responder_for(tmp_path, other)


def test_a_claude_responder_refuses_a_gemini_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.raw.test_claude_payload import claude_run, claude_workflow

    env_online(monkeypatch)
    audio, plan, utterances, approval = claude_run(tmp_path)
    with pytest.raises(AnalysisApprovalError, match="Claude Messages 클라이언트"):
        validated_luna_responder(
            client=gemini_client(),
            settings=claude_workflow(),
            understanding=SETTINGS,
            audio=audio,
            plan=plan,
            utterances=utterances,
            frames=FrameSamplingResult((), ()),
            approval=approval,
        )


def test_a_validated_gemini_responder_sends_clips_through_the_gemini_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    responder = responder_for(tmp_path, gemini_client())
    windows = responder.context.evidence_windows
    assert all(item.clip is not None for item in windows)
    by_id = {item.window.window_id: item for item in windows}
    sent: list[dict[str, Any]] = []

    def fake_gemini(client: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append({"client": client, **kwargs})
        parts = kwargs["payload"]["contents"][0]["parts"]
        window_id = parts[-1]["text"].split("window_id: ")[1].splitlines()[0]
        assert parts[1]["inlineData"]["mimeType"] == "video/mp4"
        return gemini_reply(accepted_body(by_id[window_id]))

    def elsewhere(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini 모델이 다른 공급자의 문으로 새면 안 된다")

    monkeypatch.setattr(luna, "request_gemini_generate", fake_gemini)
    monkeypatch.setattr(luna, "request_claude_messages", elsewhere)
    monkeypatch.setattr(luna, "request_luna_vision", elsewhere)
    annotations = annotate_windows(
        windows, responder, settings=SETTINGS, model=FLASH, origin="live"
    )
    assert all(item.status == luna.ANNOTATION_ACCEPTED for item in annotations)
    assert len(sent) == len(windows)
    assert all(call["online"] is True and call["model"] == FLASH for call in sent)


def test_an_unpriced_approval_cannot_stand_up_a_gemini_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    audio, plan, utterances, _approval = gemini_run(tmp_path, on=date(2027, 1, 1))
    with pytest.raises(AnalysisApprovalError, match="공식 단가 상한"):
        validated_luna_responder(
            client=gemini_client(),
            settings=gemini_workflow(),
            understanding=SETTINGS,
            audio=audio,
            plan=plan,
            utterances=utterances,
            frames=FrameSamplingResult((), ()),
            approval=_approval,
            clips=clips_for(tmp_path, plan),
        )


def test_a_request_over_the_inline_limit_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    monkeypatch.setattr(gemini_cost, "INLINE_REQUEST_LIMIT_BYTES", 100)
    with pytest.raises(AnalysisApprovalError, match="인라인 상한"):
        responder_for(tmp_path, gemini_client())


# ── 영수증 · 클라이언트 · 키 ─────────────────────────────────────────────────


def test_the_usage_observer_prices_generate_content_with_the_gemini_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[Any] = []
    monkeypatch.setattr(request_log, "recording", lambda: True)
    monkeypatch.setattr(request_log, "record_usage", lambda usage, ticket: recorded.append(usage))
    monkeypatch.setattr(request_log, "finish", lambda *args, **kwargs: None)
    meta = SimpleNamespace(operation=GENERATE_CONTENT_OPERATION, model=FLASH, kind=None)
    UsageObserver().on_response("ticket", meta, gemini_reply({}))
    (usage,) = recorded
    assert usage.pricing_revision == PROFILE.pricing_revision
    assert usage.provider_request_id == "resp_1"


def test_without_a_gemini_key_the_run_closes_before_any_paid_call() -> None:
    transport = LiveAnalysisTransport(
        sender=None, cfg=None, client=object(), asr_approvals=dict, luna_approvals=dict
    )
    with pytest.raises(AnalysisApprovalError, match="GEMINI_API_KEY"):
        transport.semantic_client(FLASH)
    with pytest.raises(AnalysisApprovalError, match="등록되지 않은 Gemini"):
        transport.semantic_client("gemini-3.6-flash")


def test_the_live_transport_builds_one_gemini_client_per_profile() -> None:
    transport = live_transport_for_credentials(
        EnvironmentCredentials(api_key="sk-synthetic", base_url=None),
        asr_approvals=dict,
        luna_approvals=dict,
        gemini=GeminiCredentials(api_key="AIza-synthetic"),
    )
    assert set(transport.gemini_clients) == set(GEMINI_ANALYSIS_PROFILES)
    client = transport.semantic_client(FLASH)
    assert type(client).__name__ == "GeminiClient"
    assert "AIza-synthetic" not in repr(client)


def test_the_gemini_key_is_captured_by_its_own_name_only() -> None:
    assert capture_gemini_credentials({"GOOGLE_API_KEY": "x"}) is None
    assert capture_gemini_credentials({"GEMINI_API_KEY": "  "}) is None
    captured = capture_gemini_credentials({"GEMINI_API_KEY": " AIza-k "})
    assert captured is not None and captured.api_key == "AIza-k"
    assert "AIza-k" not in repr(captured)


# ── 작업 흐름의 창 클립 ────────────────────────────────────────────────────


class FakeSampler:
    def __init__(self, tmp_path: Path, fail: str | None = None) -> None:
        self.calls: list[tuple[Fraction, Fraction]] = []
        self.tmp_path = tmp_path
        self.fail = fail

    def clip(self, *, start: Fraction, seconds: Fraction) -> Path:
        self.calls.append((start, seconds))
        if self.fail is not None and len(self.calls) == 1:
            raise RuntimeError(self.fail)
        path = self.tmp_path / f"c{len(self.calls)}.mp4"
        path.write_bytes(CLIP_BYTES)
        return path


def window_clips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, sampler: FakeSampler
) -> tuple[dict[str, ClipEvidence], Any, Any]:
    audio, plan, _utterances, _ = online_run(tmp_path)
    monkeypatch.setattr(media_refs, "clip_sampler", lambda *a, **k: sampler)
    workflow = online_workflow()
    workflow = replace(workflow, models=replace(workflow.models, luna_model=model))
    grid = SimpleNamespace(presentation_origin=Fraction(1, 3))
    context = SimpleNamespace(check_cancel=lambda: None)
    clips = analysis_workflow._window_clips(
        context, None, SimpleNamespace(source_name="s"), workflow, grid=grid, audio=audio, plan=plan
    )
    return clips, plan, grid


def test_the_workflow_cuts_clips_only_for_gemini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampler = FakeSampler(tmp_path)
    clips, _plan, _grid = window_clips(tmp_path, monkeypatch, "claude-sonnet-5-5", sampler)
    assert clips == {} and sampler.calls == []


def test_clip_file_time_is_window_time_plus_the_video_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampler = FakeSampler(tmp_path)
    clips, plan, grid = window_clips(tmp_path, monkeypatch, FLASH, sampler)
    assert list(clips) == [window.window_id for window in plan.windows]
    for (start, seconds), window in zip(sampler.calls, plan.windows, strict=True):
        assert start == window.context_start + grid.presentation_origin
        assert seconds == window.context_end - window.context_start
        assert clips[window.window_id].start == window.context_start


def test_a_window_whose_clip_fails_is_asked_with_frames_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampler = FakeSampler(tmp_path, fail="ffmpeg 실패")
    clips, plan, _grid = window_clips(tmp_path, monkeypatch, FLASH, sampler)
    first, *rest = (window.window_id for window in plan.windows)
    assert first not in clips and all(window_id in clips for window_id in rest)


def test_decimal_ceiling_values_are_positive_and_finite() -> None:
    ceiling = gemini_cost.window_ceiling_usd(
        PROFILE, SETTINGS, schema_bytes=luna.gemini_output_schema_bytes(), on=TODAY
    )
    assert isinstance(ceiling, Decimal) and ceiling.is_finite() and ceiling > 0


# ── 검토가 짚은 틈(W-30c2 독립 검토) ─────────────────────────────────────────


class Always(FakeSampler):
    def clip(self, *, start: Fraction, seconds: Fraction) -> Path:
        self.calls.append((start, seconds))
        raise RuntimeError("AAC 인코더 없음")


def test_a_source_whose_every_clip_fails_is_not_sent_frames_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """계통 실패(모든 창)는 사람이 승인한 「클립 + 프레임」이 아니다 — 그 원본을 실패로 닫는다."""
    from vlogsrt.project.raw.models import AnalysisError

    with pytest.raises(AnalysisError, match="한 창도"):
        window_clips(tmp_path, monkeypatch, FLASH, Always(tmp_path))


def test_an_expired_price_closes_before_any_transcription_is_bought() -> None:
    """알린 인상일 뒤 — 1단계 전사(유료)를 사기 전, 클라이언트를 고르는 자리에서 닫힌다."""
    transport = live_transport_for_credentials(
        EnvironmentCredentials(api_key="sk-synthetic", base_url=None),
        asr_approvals=dict,
        luna_approvals=dict,
        on=date(2027, 1, 1),
        gemini=GeminiCredentials(api_key="AIza-synthetic"),
    )
    with pytest.raises(AnalysisApprovalError, match="가격 미정"):
        transport.semantic_client(FLASH)
    current = replace(transport, on=TODAY)
    assert current.semantic_client(FLASH) is current.gemini_clients[FLASH]


class _AfterTheRaise(date):
    @classmethod
    def today(cls) -> date:
        return date(2027, 1, 1)


def test_a_run_that_crosses_the_raise_stops_before_the_next_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실행이 인상일 자정을 넘으면 그 뒤의 창은 보내지 않는다 — 승인 상한은 옛 단가로 잰 값이다."""
    env_online(monkeypatch)
    responder = responder_for(tmp_path, gemini_client())
    sent: list[Any] = []
    monkeypatch.setattr(luna, "request_gemini_generate", lambda *a, **k: sent.append(k))
    monkeypatch.setattr(luna, "date", _AfterTheRaise)
    with pytest.raises(AnalysisApprovalError, match="가격 미정"):
        luna._send_semantic(responder.client, FLASH, {"contents": []})
    assert sent == []


def test_a_failed_window_keeps_the_providers_redacted_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실측(2026-10-25) — 인증 실패가 「API 인증 실패」 한 줄로만 남아 까닭을 몰랐다. 공급자가 말한
    상태· 까닭·메시지(키는 가림)를 판정의 결함 문구에 붙인다."""
    from vlogsrt.gemini_messages import GeminiAPIStatusError

    env_online(monkeypatch)
    responder = responder_for(tmp_path, gemini_client())
    error = GeminiAPIStatusError(400, "INVALID_ARGUMENT", "API_KEY_INVALID", "API key not valid.")

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(luna, "request_gemini_generate", refuse)
    (first, *_rest) = responder.context.evidence_windows
    (annotation,) = annotate_windows(
        [first], responder, settings=SETTINGS, model=FLASH, origin="live"
    )
    assert annotation.status == luna.ANNOTATION_FAILED
    assert annotation.error_code == "authentication"
    assert "INVALID_ARGUMENT" in (annotation.defect or "")
    assert "API key not valid" in (annotation.defect or "")


def test_other_errors_do_not_echo_their_text() -> None:
    assert luna._provider_detail(RuntimeError("sk-secret in a body")) == ""


# ── Gemini가 들은 말(W-30e, D88) ─────────────────────────────────────────────


def test_only_the_gemini_schema_carries_heard_speech(tmp_path: Path) -> None:
    schema = luna.gemini_output_schema()
    assert schema["properties"]["heard_speech"] == {"type": "string"}  # type: ignore[index]
    assert "heard_speech" in schema["required"]  # type: ignore[operator]
    assert "heard_speech" not in luna_output_schema()["properties"]  # type: ignore[operator]
    evidence = with_clip(tmp_path)
    instruction = build_gemini_payload(evidence, SETTINGS, PROFILE)["contents"][0]["parts"][-1][
        "text"
    ]  # type: ignore[index]
    assert "heard_speech에는 clip에서 들린 말" in instruction
    from vlogsrt.project.raw.config import claude_profile

    claude = build_claude_payload(evidence, SETTINGS, claude_profile("claude-sonnet-5-5"))
    assert "heard_speech" not in json.dumps(claude, ensure_ascii=False)


def test_heard_speech_is_kept_on_the_verdict_and_only_then_in_the_document(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    body = accepted_body(evidence, heard_speech="  안녕 여기 바다야  ")
    annotation = parse_with(FLASH, gemini_reply(body), evidence)
    assert annotation.status == luna.ANNOTATION_ACCEPTED
    assert annotation.heard_speech == "안녕 여기 바다야"
    assert luna.annotation_json(annotation)["heard_speech"] == "안녕 여기 바다야"
    silent = parse_with(FLASH, gemini_reply(accepted_body(evidence, heard_speech="")), evidence)
    assert "heard_speech" not in luna.annotation_json(silent)  # Luna·Claude 문서와 같은 모양


def test_heard_speech_that_is_not_text_is_a_defect(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    annotation = parse_with(FLASH, gemini_reply(accepted_body(evidence, heard_speech=3)), evidence)
    assert annotation.status == luna.ANNOTATION_REJECTED
    assert "heard_speech" in (annotation.defect or "")
