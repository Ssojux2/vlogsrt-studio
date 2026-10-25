"""W-18 / D58 — 영상 프레임 추천 평가(의미 분석)의 기본 모델은 Claude Sonnet 5.5다.

새 작업공간의 기본이 Sonnet 5.5다. 이미 있는 작업공간은 저장된 모델을 그대로 쓴다 — 다시
분석하는 것은 유료라 사람이 고를 때만 옮긴다. Opus 5.5와 gpt-6-luna는 계속 고를 수 있다.
클라이언트는 고른 모델의 프로파일로 만든다. 전사 신원(whisper 청크 열쇠)은 W-14의 고정값에
묶여 있어 기본이 바뀌어도 한 바이트도 움직이지 않는다(`test_transcription_identity.py`).
유료 호출 0회 — 전송은 만들기만 하고 보내지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from tests.raw.test_raw_config import ACCEPTED_SETTINGS_JSON
from tests.raw.test_understanding_luna import SETTINGS, accepted_body, make_evidence
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.claude_cost import per_image_bound, window_ceiling_usd
from vlogsrt.project.raw.config import (
    ANALYSIS_MODEL_CHOICES,
    ANALYSIS_PROVIDER_ANTHROPIC,
    CLAUDE_ANALYSIS_PROFILES,
    CLAUDE_EFFORT_LEVELS,
    CLAUDE_MAX_OUTPUT_TOKENS_LIMIT,
    DEFAULT_ANALYSIS_MODEL,
    OPENAI_LUNA_MODEL,
    RawModelRoles,
    RawWorkflowConfig,
    analysis_model_label,
    analysis_provider,
    claude_profile,
    claude_profile_problems,
    raw_workflow_config_from_json,
    validate_raw_workflow_config,
)
from vlogsrt.project.raw.service import RawWorkspaceService
from vlogsrt.project.raw.understanding_luna import (
    build_semantic_payload,
    luna_output_schema,
    luna_output_schema_bytes,
    parse_luna_response,
)
from vlogsrt.project.raw.workspace import RawWorkspace

SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
LUNA = "gpt-6-luna"
ROOT = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────
#  프로파일 — 2026-10-11 공식 문서 확인값
# ─────────────────────────────────────────────────────────────


def test_the_sonnet_profile_carries_the_2026_10_11_snapshot() -> None:
    profile = claude_profile(SONNET)
    assert CLAUDE_ANALYSIS_PROFILES[SONNET] is profile
    assert profile.model == SONNET and profile.label == "Claude Sonnet 5.5"
    assert (profile.api_base_url, profile.api_version, profile.timeout_seconds) == (
        "https://api.anthropic.com",
        "2023-06-01",
        300.0,
    )
    # 생각 토큰도 max_tokens 안에 든다 — 12000은 판정 본문 위에 생각 여유를 둔 조정값이다
    assert (profile.effort, profile.max_output_tokens) == ("medium", 12000)
    assert profile.inference_geo == "global"  # US 전용은 1.1배 — 표준 단가 지역을 명시한다
    assert profile.pricing_checked_at == date(2026, 10, 11)
    assert profile.pricing_revision == "anthropic-claude-sonnet-5-5-2026-10-11"
    assert (profile.input_per_mtok, profile.output_per_mtok) == (Decimal("2"), Decimal("10"))
    assert profile.cache_write_5m_per_mtok == Decimal("2.50")
    assert profile.cache_write_1h_per_mtok == Decimal("4")
    assert profile.cache_read_per_mtok == Decimal("0.10")
    assert (profile.batch_input_per_mtok, profile.batch_output_per_mtok) == (
        Decimal("1"),
        Decimal("5"),
    )
    # 고해상도 등급(Claude 4.7 이후) — Opus 5.5와 같다
    assert (profile.image_patch_pixels, profile.image_max_visual_tokens) == (28, 4784)
    assert (profile.quote_text_tokens_per_window, profile.quote_overhead_tokens) == (12000, 2000)
    assert (profile.account_access_status, profile.integration_test_status) == (
        "unverified",
        "not_run",
    )
    assert "https://platform.claude.com/docs/en/about-claude/pricing" in profile.source_urls
    assert any("sonnet-5-5" in url for url in profile.source_urls)
    assert all(url.startswith("https://platform.claude.com/") for url in profile.source_urls)


def test_the_opus_profile_is_still_registered_unchanged() -> None:
    opus = claude_profile(OPUS)
    assert opus.label == "Claude Opus 5.5"
    assert (opus.input_per_mtok, opus.output_per_mtok) == (Decimal("4"), Decimal("20"))
    assert (opus.effort, opus.max_output_tokens) == ("medium", 8192)
    assert opus.pricing_revision == "anthropic-claude-opus-5-5-2026-10-09"


def test_every_registered_profile_passes_the_documented_limits() -> None:
    """두 모델 모두 effort 다섯 단계·출력 128K다(effort·모델 문서, 2026-10-11)."""
    assert CLAUDE_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")
    assert CLAUDE_MAX_OUTPUT_TOKENS_LIMIT == 128_000
    for profile in CLAUDE_ANALYSIS_PROFILES.values():
        assert claude_profile_problems(profile) == [], profile.model
    too_long = replace(claude_profile(SONNET), max_output_tokens=CLAUDE_MAX_OUTPUT_TOKENS_LIMIT + 1)
    assert any("max_output_tokens" in problem for problem in claude_profile_problems(too_long))


def test_the_sonnet_window_ceiling_is_about_sixteen_cents() -> None:
    """창 하나의 보수적 상한 — 같은 공식, Sonnet 단가와 출력 한도로(D58 · 약 $0.16)."""
    profile = claude_profile(SONNET)
    settings = RawUnderstandingSettings()
    schema_bytes = luna_output_schema_bytes()
    ceiling = window_ceiling_usd(profile, settings, schema_bytes=schema_bytes)
    input_tokens = (
        profile.quote_text_tokens_per_window
        + schema_bytes
        + profile.quote_overhead_tokens
        + settings.max_images_per_window * per_image_bound(profile, settings.image_long_edge)
    )
    expected = (
        Decimal(input_tokens) * Decimal("2") + Decimal(profile.max_output_tokens) * Decimal("10")
    ) / Decimal(1_000_000)
    assert ceiling == expected
    assert Decimal("0.16") < ceiling < Decimal("0.17")
    opus = window_ceiling_usd(claude_profile(OPUS), settings, schema_bytes=schema_bytes)
    assert ceiling < opus


# ─────────────────────────────────────────────────────────────
#  기본값과 선택지
# ─────────────────────────────────────────────────────────────


def test_sonnet_stays_selectable_after_flash_became_the_default() -> None:
    """D87 — 기본은 Gemini 3.8 Flash로 바뀌었고 Sonnet 5.5는 셋째로 그대로 고른다."""
    assert RawModelRoles().luna_model == DEFAULT_ANALYSIS_MODEL == "gemini-3.8-flash"
    assert ANALYSIS_MODEL_CHOICES == ("gemini-3.8-flash", "gemini-4-argon", SONNET, OPUS, LUNA)
    assert OPENAI_LUNA_MODEL == LUNA
    assert tuple(CLAUDE_ANALYSIS_PROFILES) == (SONNET, OPUS)
    validate_raw_workflow_config(RawWorkflowConfig())
    assert analysis_provider(SONNET) == ANALYSIS_PROVIDER_ANTHROPIC
    assert analysis_model_label(SONNET) == "Claude Sonnet 5.5"
    for model in ANALYSIS_MODEL_CHOICES:  # 다섯 다 그대로 고를 수 있다
        chosen = replace(RawWorkflowConfig(), models=replace(RawModelRoles(), luna_model=model))
        validate_raw_workflow_config(chosen)


def test_a_new_workspace_starts_on_flash_not_sonnet(tmp_path: Path) -> None:
    """D87 — 새 작업공간은 Flash로 시작한다(D58의 Sonnet 기본을 바꿨다)."""
    workspace = RawWorkspace.create(tmp_path / "new", "새 작업공간")
    assert workspace.load_workflow().models.luna_model == "gemini-3.8-flash"


# ─────────────────────────────────────────────────────────────
#  기존 작업공간은 조용히 옮겨 가지 않는다
# ─────────────────────────────────────────────────────────────


def _store_settings(root: Path, payload: str) -> None:
    with RawWorkspace.open(root).connect() as connection:
        connection.execute("UPDATE settings SET value = ? WHERE key = 'workflow'", (payload,))
        connection.commit()


def test_an_existing_luna_workspace_still_loads_as_luna(tmp_path: Path) -> None:
    """W-18 이전에 만든 작업공간은 그때의 기본(gpt-6-luna)을 저장했다 — 그대로 열린다."""
    root = tmp_path / "existing"
    RawWorkspace.create(root, "기존 작업공간")
    _store_settings(root, ACCEPTED_SETTINGS_JSON)  # A0/A1에서 수락된 저장 설정 글자 그대로
    assert RawWorkspace.open(root).load_workflow().models.luna_model == LUNA
    service = RawWorkspaceService()
    service.open_workspace(root)
    assert service.settings(root).models.luna_model == LUNA


def test_a_stored_config_without_the_model_is_refused_not_filled() -> None:
    """빠진 칸을 기본값으로 메우지 않는다 — 메우면 기존 작업공간이 몰래 Sonnet이 된다."""
    stored = json.loads(ACCEPTED_SETTINGS_JSON)
    del stored["models"]["luna_model"]
    with pytest.raises(ValueError, match="luna_model"):
        raw_workflow_config_from_json(json.dumps(stored))


# ─────────────────────────────────────────────────────────────
#  페이로드와 판정 파싱 — Sonnet 5.5가 400을 내는 칸은 싣지 않는다
# ─────────────────────────────────────────────────────────────

SONNET_400_KEYS = ("thinking", "temperature", "top_p", "top_k", "tools", "tool_choice")


def test_the_sonnet_payload_has_no_field_sonnet_rejects(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    payload = build_semantic_payload(evidence, SETTINGS, SONNET)
    assert set(payload) == {"max_tokens", "messages", "output_config", "inference_geo"}
    assert not set(SONNET_400_KEYS) & set(payload)
    assert "model" not in payload and "service_tier" not in payload
    assert payload["max_tokens"] == 12000
    assert payload["inference_geo"] == "global"
    assert payload["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": luna_output_schema()},
    }
    # 같은 증거·같은 안내문·같은 schema — 모델마다 다른 것은 출력 한도뿐이다
    opus = build_semantic_payload(evidence, SETTINGS, OPUS)
    assert {**payload, "max_tokens": opus["max_tokens"]} == opus


def _sonnet_reply(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "message", "model": SONNET, "content": content, "stop_reason": "end_turn"}


def _parse(evidence: Any, response: dict[str, Any]) -> Any:
    return parse_luna_response(
        response,
        evidence=evidence,
        model=SONNET,
        request_sha256="f" * 64,
        origin="offline_replay",
        prompt_revision=SETTINGS.prompt_revision,
    )


def test_adaptive_thinking_blocks_before_the_text_are_ignored(tmp_path: Path) -> None:
    """적응형 생각은 text 앞에 생각 블록을 낸다 — 판정은 text 블록만 읽는다."""
    evidence = make_evidence(tmp_path)
    body = accepted_body(evidence, suitability="usable", suitability_reason="흔들림이 적다")
    decoy = json.dumps(
        accepted_body(evidence, suitability="review", suitability_reason="생각 블록의 미끼"),
        ensure_ascii=False,
    )
    response = _sonnet_reply(
        [
            {"type": "thinking", "thinking": decoy, "signature": "synthetic"},
            {"type": "redacted_thinking", "data": "synthetic-redacted"},
            {"type": "text", "text": json.dumps(body, ensure_ascii=False)},
        ]
    )
    annotation = _parse(evidence, response)
    assert annotation.status == "accepted", annotation
    assert annotation.luna_model == SONNET
    assert (annotation.suitability, annotation.suitability_reason) == ("usable", "흔들림이 적다")


def test_a_reply_with_only_thinking_is_not_a_judgment(tmp_path: Path) -> None:
    evidence = make_evidence(tmp_path)
    body = json.dumps(accepted_body(evidence), ensure_ascii=False)
    response = _sonnet_reply([{"type": "thinking", "thinking": body, "signature": "s"}])
    annotation = _parse(evidence, response)
    assert annotation.status != "accepted"
    assert "text 블록이 없다" in annotation.defect


# ─────────────────────────────────────────────────────────────
#  클라이언트 — 고른 모델의 프로파일로 만든다
# ─────────────────────────────────────────────────────────────


def _transport(claude: Any) -> Any:
    from vlogsrt.project.raw import browser_runtime

    return browser_runtime.live_transport_for_credentials(
        browser_runtime.EnvironmentCredentials(api_key="sk-test-only", base_url=None),
        asr_approvals=lambda *a: {},
        luna_approvals=lambda *a: {},
        claude=claude,
    )


def test_each_claude_client_comes_from_its_own_model_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프로파일마다 엔드포인트·버전·시간 한도가 달라도 모델에 맞는 클라이언트가 나온다."""
    from vlogsrt.project.raw import browser_runtime
    from vlogsrt.project.raw.understanding_luna import AnalysisApprovalError
    from vlogsrt.transcribe import is_claude_client

    sonnet = replace(claude_profile(SONNET), timeout_seconds=111.0)
    opus = replace(claude_profile(OPUS), timeout_seconds=222.0, api_version="2023-06-02")
    registry = MappingProxyType({SONNET: sonnet, OPUS: opus})
    monkeypatch.setattr(browser_runtime, "CLAUDE_ANALYSIS_PROFILES", registry)
    transport = _transport(browser_runtime.ClaudeCredentials(api_key="sk-ant-x"))
    for profile in (sonnet, opus):
        client = transport.semantic_client(profile.model)
        assert is_claude_client(client)
        assert (client.base_url, client.api_version, client.timeout_seconds) == (
            profile.api_base_url,
            profile.api_version,
            profile.timeout_seconds,
        )
    assert transport.semantic_client(SONNET) is not transport.semantic_client(OPUS)
    assert transport.semantic_client(LUNA) is transport.client  # Luna는 OpenAI 클라이언트 그대로
    with pytest.raises(AnalysisApprovalError, match="등록되지 않은"):
        transport.semantic_client("claude-unknown-9")
    assert "sk-ant-x" not in repr(transport)


def test_the_default_sonnet_model_needs_the_claude_key_before_any_purchase() -> None:
    from vlogsrt.project.raw.understanding_luna import AnalysisApprovalError

    transport = _transport(None)
    for model in CLAUDE_ANALYSIS_PROFILES:
        with pytest.raises(AnalysisApprovalError, match="ANTHROPIC_API_KEY"):
            transport.semantic_client(model)
    assert transport.semantic_client(LUNA) is transport.client


# ─────────────────────────────────────────────────────────────
#  리터럴은 설정 한 곳에만
# ─────────────────────────────────────────────────────────────


def test_the_sonnet_literal_lives_only_in_the_raw_config() -> None:
    hits = sorted(
        path.relative_to(ROOT).as_posix()
        for directory in ("vlogsrt", "ui", "scripts")
        for path in (ROOT / directory).rglob("*.py")
        if SONNET in path.read_text(encoding="utf-8")
    )
    assert hits == ["vlogsrt/project/raw/config.py"]
