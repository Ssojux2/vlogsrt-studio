"""W-31e(D92) — 화면: 「새로 듣는 전사 엔진」 칸·견적 문장·다시 듣기 문구·편집기 후보·키 경고.

- 고르는 칸은 없다 — whisper는 은퇴했고(D163) 새로 듣는 엔진은 Gemini 하나다. 설정 서랍
  「분석 모델」의 상태 줄이 엔진·키를 말하고, 금액은 없다(D57 결정 7).
- 견적 문장은 Gemini로 들을 영상이 있을 때만 엔진·분·공식 단가·금액을 말한다(whisper 견적은 그대로).
- 승인 문: Gemini로 들을 영상이 있는데 키가 없거나 가격 미정이면 두 범위 모두 접수 전에 닫는다.
- 다시 듣기 칸은 어느 엔진으로 들었고 다시 들으면 어느 엔진인지 말한다.
- Gemini 원본의 다른 귀 글자는 편집기에 「후보」로, 「확인 필요 · 다른 귀가 다르게 들음」으로
  보인다.

유료 호출은 없다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)


from tests.raw.test_analysis_model_ui import PRICE_WORDS  # noqa: E402
from tests.raw.test_browser_runtime import _estimates  # noqa: E402
from ui import ui_facade  # noqa: E402
from ui import workspace_service as ws  # noqa: E402
from ui.workspace_service import RawWorkspaceUi  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw import source_transcript  # noqa: E402

GEMINI = raw_service.TIMING_ENGINE_GEMINI
WHISPER = raw_service.TIMING_ENGINE_WHISPER
GEMINI_MODEL = "gemini-3.5-transcribe"


def _server(tmp_path: Path) -> RawWorkspaceUi:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("전사 엔진", str(tmp_path / "workspace"))
    server._startup_gemini = None  # 이 실행의 환경과 무관하게 「키 없음」에서 출발한다
    return server


def _engine_of(server: RawWorkspaceUi) -> str:
    return str(server.raw.new_hearing_engine(server.workspace_root()))


# ── 칸 ───────────────────────────────────────────────────────────────────


def test_new_videos_hear_with_gemini_and_there_is_no_engine_choice(tmp_path: Path) -> None:
    """whisper는 은퇴했다(D163) — 고르는 칸이 없고, 표지에 남은 「whisper」는 무시한다."""
    server = _server(tmp_path)
    root = server.workspace_root()
    assert raw_service.TIMING_ENGINE_CHOICES == (GEMINI,)
    assert not hasattr(server, "settings_timing_engine_view")
    status = server.timing_engine_status()
    assert "새로 듣는 영상은 **Gemini 3.5 Transcribe**로 듣습니다" in status
    # 실응답 계약을 확인했다(W-52b2, D104) — 「H1 미확인」 줄은 더 뜨지 않는다
    assert ws.GEMINI_CONTRACT_UNCONFIRMED_NOTE not in status
    with pytest.raises(raw_service.RawWorkspaceError, match="은퇴"):
        server.raw.set_new_hearing_engine(root, WHISPER)
    server.raw._workspace(root).set_meta_value("stage1_timing_engine", WHISPER)  # 옛 작업공간
    assert _engine_of(server) == GEMINI


def test_an_unconfirmed_gemini_contract_still_gets_the_h1_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """등록표의 실응답 계약이 통과가 아니면 막지 않고 알린다 — 다음 엔진 판에도 같다(D104)."""
    from vlogsrt.project.raw import timing_engine

    profiles = dict(timing_engine.GEMINI_TRANSCRIBE_PROFILES)
    profiles[GEMINI_MODEL] = replace(profiles[GEMINI_MODEL], integration_test_status="not_run")
    monkeypatch.setattr(timing_engine, "GEMINI_TRANSCRIBE_PROFILES", profiles)
    status = _server(tmp_path).timing_engine_status()
    assert ws.GEMINI_CONTRACT_UNCONFIRMED_NOTE in status


def test_the_status_names_the_key_and_warns_to_use_a_paid_key(tmp_path: Path) -> None:
    server = _server(tmp_path)
    missing = server.timing_engine_status()
    assert "이미 들은 영상은 그대로" in missing and "「다시 듣기」" in missing
    assert "공식 단가 확인됨(2026-10-26 확인, 2027-01-26까지 다시 확인)" in missing
    # 상태 줄에는 금액이 없다 — 금액은 견적만 말한다(D57 결정 7)
    assert not any(word in missing for word in PRICE_WORDS), missing
    assert "Gemini 키(`GEMINI_API_KEY`) 없음" in missing
    assert ws.GEMINI_TRANSCRIBE_PAID_KEY_NOTE in missing
    server._startup_gemini = object()
    present = server.timing_engine_status()
    assert "Gemini 키(`GEMINI_API_KEY`) 확인됨 — 값은 표시하지 않습니다" in present
    assert ws.GEMINI_TRANSCRIBE_PAID_KEY_NOTE in present


# ── 견적 문장 · 승인 문 ────────────────────────────────────────────────────


def _rows(*usd: str | None) -> list[Any]:
    asr, _semantic = _estimates()
    whisper = replace(asr, asset_id="w", asr_seconds="30")
    gemini = [
        replace(asr, asset_id=f"g{i}", asr_seconds="45", timing_model=GEMINI_MODEL, timing_usd=v)
        for i, v in enumerate(usd)
    ]
    return [whisper, *gemini]


def test_the_approval_gate_needs_the_gemini_key_and_a_price_in_both_scopes(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    assert server._timing_engine_problem(SimpleNamespace(estimates=_rows())) is None
    quote = SimpleNamespace(estimates=_rows("0.0038"))
    missing = server._timing_engine_problem(quote)
    assert missing is not None and "`GEMINI_API_KEY`" in missing
    assert "유료 호출은 시작하지 않았습니다" in missing
    server._startup_gemini = object()
    assert server._timing_engine_problem(quote) is None
    server._live_transport = SimpleNamespace(timing_client=None)
    assert server._timing_engine_problem(quote) == missing
    server._live_transport = SimpleNamespace(timing_client=object())
    assert server._timing_engine_problem(quote) is None
    unpriced = server._timing_engine_problem(SimpleNamespace(estimates=_rows(None)))
    assert unpriced is not None and "가격 미정" in unpriced


def test_minting_refuses_an_unpriced_gemini_row() -> None:
    from vlogsrt.project.raw.browser_runtime import BrowserRuntimeError, mint_asr_approvals

    asr, _semantic = _estimates()
    row = replace(asr, timing_model=GEMINI_MODEL, timing_usd=None)
    service = SimpleNamespace(
        settings=lambda root: raw_service.RawWorkflowConfig(),
        analysis_estimate=lambda root, ids, settings: (row,),
        list_source_assets=lambda root: [SimpleNamespace(asset_id="a1", content_sha256="a" * 64)],
    )
    with pytest.raises(BrowserRuntimeError, match="가격 미정"):
        mint_asr_approvals(service, Path("."), ["a1"])


# ── 다시 듣기 · 편집기 ─────────────────────────────────────────────────────


def test_the_relisten_panel_names_the_engine_it_heard_with_and_will_hear_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    root = server.workspace_root()
    whisper_view = raw_service.RawWorkflowConfig()
    monkeypatch.setattr(server.raw, "source_settings", lambda root, sid: whisper_view)
    change = server._relisten_engine_text(root, "a1", pending=False)
    assert change == (
        "이 영상의 원본 자막은 whisper-1로 들었습니다. 다시 들으면 Gemini 3.5 Transcribe로"
        " 듣습니다 (whisper는 은퇴했습니다)."
    )
    gemini_view = replace(
        whisper_view, models=replace(whisper_view.models, timing_model=GEMINI_MODEL)
    )
    monkeypatch.setattr(server.raw, "source_settings", lambda root, sid: gemini_view)
    pending = server._relisten_engine_text(root, "a1", pending=True)
    assert pending == "다시 들을 전사 엔진: Gemini 3.5 Transcribe."
    same = server._relisten_engine_text(root, "a1", pending=False)
    assert same == (
        "이 영상의 원본 자막은 Gemini 3.5 Transcribe로 들었습니다. 다시 들어도 Gemini 3.5"
        " Transcribe로 듣습니다."
    )


def _bundle_with(*needles: str) -> str:
    bundles = [value for value in vars(ui_facade).values() if isinstance(value, str)]
    return next(text for text in bundles if all(needle in text for needle in needles))


def test_the_client_draws_the_engine_line_and_the_candidate_note() -> None:
    script = _bundle_with("function relistenPanel", "function earNote")
    assert "if (re.engine)" in script
    assert 'ear.mark === "candidate"' in script and "EAR_CANDIDATE_NOTE" in script
    assert '다른 귀의 글자는" +\n        " 후보로만 보입니다' in script


def test_a_gemini_candidate_is_marked_as_a_candidate_and_needs_a_listen() -> None:
    row = {
        "utterance_id": "utterance_000001",
        "text": "엔진 글자",
        "whisper_text": "엔진 글자",
        "text_source": source_transcript.TEXT_SOURCE_WHISPER,
        "retext_reason": source_transcript.REASON_CANDIDATE,
        "alt_text": "다른 귀 글자",
    }
    ear = source_transcript.ear_evidence(row)
    assert ear == {"mark": source_transcript.EAR_CANDIDATE, "other": "다른 귀 글자", "first": ""}
    plain = dict(row, retext_reason="gate")
    assert source_transcript.ear_evidence(plain)["mark"] == source_transcript.EAR_ALT  # type: ignore[index]
    from vlogsrt.project.raw.service import _ear_disagreed

    transcript = {"utterances": [row, dict(row, utterance_id="u2", retext_reason="agreed")]}
    assert _ear_disagreed(transcript) == frozenset({"utterance_000001"})


def test_the_engine_facts_are_dated_and_never_price_whisper_on_screen() -> None:
    whisper = raw_service.timing_engine_facts(WHISPER, date(2026, 10, 27))
    gemini = raw_service.timing_engine_facts(GEMINI, date(2026, 10, 27))
    assert whisper.retires_on == date(2027, 2, 26) and gemini.retires_on is None
    assert str(gemini.per_minute) == "0.005"
    assert raw_service.timing_engine_facts(GEMINI, date(2027, 1, 27)).per_minute is None
    assert raw_service.timing_engine_key(GEMINI_MODEL) == GEMINI
