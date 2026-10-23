"""v5 시대 규칙 설정 — v6(D79)에서 설정 뒤로 물러난 규칙을 시험이 그대로 겨냥할 때 쓴다."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vlogsrt.project.raw import service, tiering
from vlogsrt.project.raw.analysis_config import TIER_SETTINGS

V5_RULES = replace(
    TIER_SETTINGS,
    usable_speech_green=False,
    scenery_green=False,
    good_sparse_green=False,
    visual_issue_caps=tuple(tiering.VISUAL_ISSUE_LABELS),
    segment_green_share=1.0,
)


def pin_v5_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """서비스가 매기는 설정(`service.TIER_SETTINGS`)을 v5 규칙으로 바꾼다 — 판 표지는 그대로다."""
    monkeypatch.setattr(service, "TIER_SETTINGS", V5_RULES)
