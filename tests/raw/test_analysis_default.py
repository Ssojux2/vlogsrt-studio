"""영상 분석의 기본은 Gemini 4 Hydrogen이다: 새 작업공간과 지금 작업공간 모두(D168 — 그 전 D87은 3.8
Flash).

사용자 지시(2026-11-19): 「Gemini 4 Hydrogen로 대체해줘. 멀티모달 입력이 가능한지도 확인해」 — 3.8
Flash는 2027-01-01에, 4 Argon은 2026-12-01에 값이 오르고 확인한 단가의 기한이 지나 새 분석이 막힌다.
Hydrogen은 입력 $1.50 · 출력(생각 포함) $7.50이고 글·이미지·소리·영상을 입력으로 받는다(공식 문서,
2026-11-19 확인). 유료 호출 0회. 시험이 못 박는 것:

- 기본·선택지: Hydrogen이 맨 앞(「(기본)」), 그다음 Flash · Argon · Sonnet · Opus · gpt-6-luna.
- 예전 기본(Sonnet 5.5 · 3.8 Flash)에 머문 기존 작업공간은 열 때 **한 번** Hydrogen으로 옮긴다 —
  표지가 남아 그 뒤 사람이 고른 모델은 그대로다. 다른 Gemini(Argon)면 표지만, 분석이 도는 동안에는
  옮기지 않는다.
  옮겨도 이미 산 분석은 그대로다.
- 화면: 단가 상태는 Flash가 인상일 뒤 가격 미정이고 Hydrogen은 그날도 확인됨이다. 키 상태 줄은 키
이름만
  말하고(값 없음) 결제 연결 키 경고를 단다. 키가 없으면 전사 전에 `GEMINI_API_KEY`를 말한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.raw.test_analysis_model_ui import PRICE_WORDS, bare_server, model_of
from ui import workspace_service
from ui.workspace_service import GEMINI_PAID_KEY_NOTE
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.config import (
    ANALYSIS_MODEL_CHOICES,
    DEFAULT_ANALYSIS_MODEL,
    GEMINI_ANALYSIS_PROFILES,
    RawModelRoles,
)
from vlogsrt.project.raw.gemini_cost import price_current

HYDROGEN = "gemini-4-hydrogen"
FLASH = "gemini-3.8-flash"
ARGON = "gemini-4-argon"
SONNET = "claude-sonnet-5-5"
SECRET = "AIza-synthetic-never-shown"


def test_hydrogen_is_the_default_and_leads_the_choices() -> None:
    assert RawModelRoles().luna_model == DEFAULT_ANALYSIS_MODEL == HYDROGEN
    assert ANALYSIS_MODEL_CHOICES == (
        HYDROGEN, FLASH, ARGON, SONNET, "claude-opus-5-5", "gpt-6-luna",
    )  # fmt: skip


def test_the_choice_label_marks_only_hydrogen_as_default(tmp_path: Path) -> None:
    server = bare_server(tmp_path, model=None)
    labels = {value: label for label, value in server.analysis_model_choices()}
    assert labels[HYDROGEN] == "Gemini 4 Hydrogen (기본)"
    assert labels[FLASH] == "Gemini 3.8 Flash" and labels[ARGON] == "Gemini 4 Argon"
    assert labels[SONNET] == "Claude Sonnet 5.5"


def test_the_hydrogen_profile_is_the_checked_official_price() -> None:
    """공식 가격표(2026-11-19 확인): Standard 입력 $1.50 · 출력(생각 포함) $7.50, 모달리티·길이로
    나누지
    않고 인상 예고가 없다. 기한은 인상일이 아니라 재확인 기한이다(전사 프로파일과 같은 뜻)."""
    profile = GEMINI_ANALYSIS_PROFILES[HYDROGEN]
    assert profile.label == "Gemini 4 Hydrogen"
    assert (profile.input_per_mtok, profile.output_per_mtok) == (Decimal("1.50"), Decimal("7.50"))
    assert profile.thinking_level == "MEDIUM"  # Hydrogen 기본은 HIGH — Flash와 같은 깊이로 둔다
    assert profile.pricing_checked_at == date(2026, 11, 19)
    assert price_current(profile, on=date(2027, 1, 1))  # Flash가 막히는 날에도 매긴다
    assert not price_current(profile, on=profile.price_valid_through + timedelta(days=1))


# ── 기존 작업공간을 한 번 옮기기 ─────────────────────────────────────────────


@pytest.mark.parametrize("previous", [SONNET, FLASH])
def test_an_existing_workspace_moves_to_hydrogen_once_then_keeps_the_human_choice(
    tmp_path: Path, previous: str
) -> None:
    server = bare_server(tmp_path, model=previous)
    root = server.workspace_root()
    assert server.raw.adopt_default_analysis_model(root) == previous
    assert model_of(server) == HYDROGEN
    assert server.raw.adopt_default_analysis_model(root) is None  # 두 번째는 아무것도 안 한다
    server.settings_analysis_model_view(server.current_signature(), SONNET)
    assert model_of(server) == SONNET
    assert server.raw.adopt_default_analysis_model(root) is None
    assert model_of(server) == SONNET  # 사람이 고른 모델은 다시 열어도 그대로다


def test_opening_the_workspace_on_screen_moves_it(tmp_path: Path) -> None:
    server = bare_server(tmp_path, model=FLASH)
    root = server.workspace_root()
    server.open_workspace_view(str(root))
    assert model_of(server) == HYDROGEN


@pytest.mark.parametrize("chosen", ["claude-opus-5-5", "gpt-6-luna"])
def test_a_model_the_person_chose_is_not_moved(tmp_path: Path, chosen: str) -> None:
    """옮기는 것은 예전 기본(Sonnet 5.5 · 3.8 Flash)에 머문 작업공간뿐이다 — 따로 고른 Opus·Luna는
    그대로다."""
    server = bare_server(tmp_path, model=chosen)
    root = server.workspace_root()
    assert server.raw.adopt_default_analysis_model(root) is None
    server.open_workspace_view(str(root))
    assert model_of(server) == chosen


def test_a_workspace_already_on_gemini_only_gets_the_mark(tmp_path: Path) -> None:
    server = bare_server(tmp_path, model=ARGON)
    root = server.workspace_root()
    assert server.raw.adopt_default_analysis_model(root) is None
    assert model_of(server) == ARGON
    workspace = server.raw._workspace(root)
    assert workspace.meta_value(raw_service.ANALYSIS_DEFAULT_KEY) == HYDROGEN


def test_a_busy_workspace_is_not_moved_until_it_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = bare_server(tmp_path, model=SONNET)
    root = server.workspace_root()
    monkeypatch.setattr(server.raw, "active_jobs", lambda *a, **k: (object(),))
    assert server.raw.adopt_default_analysis_model(root) is None
    assert model_of(server) == SONNET
    monkeypatch.setattr(server.raw, "active_jobs", lambda *a, **k: ())
    assert server.raw.adopt_default_analysis_model(root) == SONNET


def test_a_new_workspace_starts_on_hydrogen(tmp_path: Path) -> None:
    server = bare_server(tmp_path, model=None)
    assert model_of(server) == HYDROGEN


# ── 화면 문구 ─────────────────────────────────────────────────────────────


class _AfterTheRaise(date):
    @classmethod
    def today(cls) -> date:
        return date(2027, 1, 1)


def test_the_price_status_says_unpriced_after_the_announced_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "공식 단가 확인됨" in workspace_service._price_status_note(FLASH)
    monkeypatch.setattr(workspace_service, "date", _AfterTheRaise)
    late = workspace_service._price_status_note(FLASH)
    assert "가격 미정" in late and "2026-12-31" in late
    assert "공식 단가 확인됨" in workspace_service._price_status_note(HYDROGEN)


def test_the_key_status_names_both_keys_and_warns_about_free_keys(tmp_path: Path) -> None:
    server = bare_server(tmp_path, model=None)
    server._startup_gemini = raw_service.GeminiCredentials(api_key=SECRET)
    present = server.analysis_model_status()
    assert "GEMINI_API_KEY" in present and "확인됨" in present and GEMINI_PAID_KEY_NOTE in present
    assert "ANTHROPIC_API_KEY" in present
    assert SECRET not in present and "AIza" not in present
    server._startup_gemini = None
    assert "GEMINI_API_KEY" in server.analysis_model_status()
    assert "없음" in server.analysis_model_status()


def test_without_a_gemini_key_the_approval_names_it_before_transcription(tmp_path: Path) -> None:
    server = bare_server(tmp_path, model=FLASH)
    server._startup_gemini = None
    server._startup_claude = raw_service.ClaudeCredentials(api_key="sk-ant-x")  # 다른 키는 상관없다
    problem = server._claude_key_problem(server.workspace_root())
    assert problem is not None and "GEMINI_API_KEY" in problem
    server._startup_gemini = raw_service.GeminiCredentials(api_key=SECRET)
    assert server._claude_key_problem(server.workspace_root()) is None


@pytest.mark.parametrize("model", [HYDROGEN, FLASH, ARGON])
def test_no_screen_text_outside_quotes_shows_a_gemini_unit_price(
    tmp_path: Path, model: str
) -> None:
    server = bare_server(tmp_path, model=model)
    root = server.workspace_root()
    for text in (
        server.analysis_model_status(),
        server._analysis_settings_markdown(root),
    ):
        assert not any(word in text for word in PRICE_WORDS), text


def test_replace_keeps_a_frozen_roles_value() -> None:
    roles = replace(RawModelRoles(), luna_model=SONNET)
    assert roles.luna_model == SONNET and RawModelRoles().luna_model == HYDROGEN
