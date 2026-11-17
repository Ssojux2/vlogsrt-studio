"""최종 편집 설정(W-66, D117) — 자막 경고 넷은 refine 값 그대로, 나머지는 검사만."""

from __future__ import annotations

from fractions import Fraction

import pytest

from vlogsrt.config import Config
from vlogsrt.project.raw.final_edit_config import (
    FINAL_EDIT_SETTINGS,
    FinalEditSettings,
    client_settings,
)


def test_cue_limits_equal_refine_config() -> None:
    refine = Config()
    assert Fraction(FINAL_EDIT_SETTINGS.min_duration_ms, 1000) == Fraction(str(refine.min_duration))
    assert Fraction(FINAL_EDIT_SETTINGS.max_duration_ms, 1000) == Fraction(str(refine.max_duration))
    assert Fraction(FINAL_EDIT_SETTINGS.min_gap_ms, 1000) == Fraction(str(refine.min_gap))
    assert Fraction(FINAL_EDIT_SETTINGS.max_cps) == Fraction(str(refine.max_cps))


@pytest.mark.parametrize(
    "changes",
    [
        {"undo_limit": 0},
        {"min_gap_ms": -1},
        {"zoom_max_px_per_s": 1},
        {"zoom_step_percent": 100},
        {"shuttle_rates": ()},
        {"caption_step_ms": 0},
        {"caption_small_step_ms": -500},
        {"shuttle_rates": (1, 0)},
    ],
)
def test_settings_refuse_broken_values(changes: dict) -> None:
    with pytest.raises(ValueError):
        FinalEditSettings(**changes)


def test_client_settings_carry_every_number_for_the_browser() -> None:
    sent = client_settings()
    assert sent["shuttleRates"] == [1, 2, 4, 8]
    # 자막 창의 ← · → · ⌥← · ⌥→ — Subtitle Edit 기본 걸음 그대로.
    assert (sent["captionStepMs"], sent["captionSmallStepMs"]) == (1000, 500)
    assert sent["minDurationMs"] == 850
    assert all(isinstance(value, (int, list)) for value in sent.values())
