"""W-29c — 추천 규칙 `raw-tier/v6`(D79): 손셀피 브이로그에 맞게 초록을 늘린다.

실데이터(아이폰 34개)에서 검토 조각 58개 중 추천이 0이었다 — AI 판정이 대부분 `usable`이고
가림·흔들림·흐림이 거의 모든 창에 붙으며, 조각은 「전부 초록」이어야 초록이었다. 사용자
결정(2026-10-23): 초록을 충분히 늘리고, 화면 문제 제한은 어두움·과노출만. 시험이 못 박는 것:

- 표: `usable` × 발화 많음 → 초록(`usable-speech`) · `recommended` × 자막 공백 → 초록
  (`scenery-good`) · `recommended` × 발화 적음 → 초록(`good-sparse`). 빨강 칸(공백 ×
  `usable`·`flagged`)은 그대로다.
- 화면 문제 제한은 설정(`visual_issue_caps`)에 든 문제(어두움·과노출)만 녹색을 내린다.
- 조각: 빨강이 없고 녹색이 조각 길이의 절반(`segment_green_share`) 이상이며 3초 이상이면 초록이다.
- 설정을 끄면(v5 규칙) 예전 결과 그대로다 — 규칙마다 손잡이가 있다.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from fractions import Fraction

import pytest

from tests.raw.test_tiering_v3 import (
    _random_document,
    _with_issues,
    chatter,
    layout,
    make_document,
)
from tests.raw.tier_rules import V5_RULES
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.analysis_config import (
    TIER_SETTINGS,
    RawTierSettings,
    tier_settings_problems,
)

F = Fraction
G, Y, R = tiering.TIER_GREEN, tiering.TIER_YELLOW, tiering.TIER_RED


SPARSE = [(F(t), F(t) + F(1, 2), "와 예쁘다") for t in (0, 4, 8, 12)]
"""4초마다 0.5초 말 — 5초 공백이 없지만 밀도가 기준(0.35) 밑이다(발화 적음)."""


def with_issues(document: dict, issues: list[str]) -> dict:
    for row in document["annotations"]:
        row["visual_issues"] = list(issues)
    return document


# ── 설정 ─────────────────────────────────────────────────────────────────


def test_v6_settings_are_the_published_values() -> None:
    assert TIER_SETTINGS.tier_version == "raw-tier/v7"
    assert TIER_SETTINGS.usable_speech_green is True
    assert TIER_SETTINGS.scenery_green is True
    assert TIER_SETTINGS.good_sparse_green is True
    assert TIER_SETTINGS.visual_issue_caps == ("dark", "overexposed")
    assert TIER_SETTINGS.segment_green_share == 0.5


@pytest.mark.parametrize(
    "changes",
    [
        {"visual_issue_caps": ("glare",)},
        {"visual_issue_caps": ["dark"]},
        {"visual_issue_caps": (1,)},
        {"segment_green_share": 0.0},
        {"segment_green_share": 1.5},
    ],
)
def test_v6_settings_reject_contract_violations(changes: dict) -> None:
    assert tier_settings_problems(replace(TIER_SETTINGS, **changes))


# ── 표 ───────────────────────────────────────────────────────────────────


def test_usable_video_with_dense_speech_is_recommended() -> None:
    document = make_document(15, [(0, 15, "usable")], chatter(0, 15))
    (span,) = tiering.classify(document).spans
    assert (span.tier, span.rule) == (G, tiering.RULE_USABLE_SPEECH)
    (old,) = tiering.classify(document, settings=V5_RULES).spans
    assert (old.tier, old.rule) == (Y, "video-usable")


def test_recommended_scenery_without_speech_is_recommended() -> None:
    document = make_document(15, [(0, 15, "recommended")], [])
    (span,) = tiering.classify(document).spans
    assert (span.tier, span.rule) == (G, tiering.RULE_SCENERY_GOOD)
    assert tiering.classify(document, settings=V5_RULES).spans[0].tier == Y


def test_recommended_video_with_sparse_speech_is_recommended() -> None:
    document = make_document(15, [(0, 15, "recommended")], SPARSE)
    (span,) = tiering.classify(document).spans
    assert span.audio.status == tiering.AUDIO_SPEECH_SPARSE
    assert (span.tier, span.rule) == (G, tiering.RULE_GOOD_SPARSE)


@pytest.mark.parametrize("suitability", ["usable", "review"])
def test_silence_over_a_not_recommended_picture_stays_red(suitability: str) -> None:
    document = make_document(15, [(0, 15, suitability)], [])
    assert tiering.classify(document).spans[0].tier == R


def test_flagged_or_sparse_usable_stays_yellow() -> None:
    sparse = make_document(15, [(0, 15, "usable")], SPARSE)
    assert tiering.classify(sparse).spans[0].tier == Y


# ── 화면 문제 제한 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("issues", "tier"),
    [
        (["obstructed", "shake", "blur"], G),
        (["dark"], Y),
        (["overexposed", "shake"], Y),
    ],
)
def test_only_dark_or_overexposed_lowers_a_green_span(issues: list[str], tier: str) -> None:
    document = with_issues(make_document(15, [(0, 15, "usable")], chatter(0, 15)), issues)
    (span,) = tiering.classify(document).spans
    assert span.tier == tier
    if tier == Y:
        assert span.caps[-1][0] == tiering.CAP_VISUAL_ISSUES
    old = tiering.classify(document, settings=V5_RULES).spans[0]
    assert old.tier == Y


# ── 조각 과반 ─────────────────────────────────────────────────────────────


def _half_green(green_until: int) -> tiering.TierReport:
    """0초부터 `green_until`초까지 추천 화면 × 빠른 말, 그 뒤 15초까지 흐린 화면 × 빠른 말(노랑)."""
    document = make_document(
        15, [(0, green_until, "recommended"), (green_until, 15, "review")], chatter(0, 15)
    )
    return tiering.classify(document)


def test_a_segment_mostly_green_without_red_is_recommended() -> None:
    report = _half_green(10)
    assert [tier for _a, _b, tier in layout(report)] == [G, Y]
    assert tiering.segment_keyword(report, F(0), F(15)) == "추천"
    recommendation, tags = tiering.segment_grade(report, F(0), F(15))
    assert recommendation == "recommended" and tags[-1] == "등급판 raw-tier/v7"


def test_exactly_half_green_is_enough_and_less_is_not() -> None:
    half = _half_green(8)
    assert tiering.segment_keyword(half, F(1), F(15)) == "추천"  # 7초 녹색 / 14초
    assert tiering.segment_keyword(half, F(0), F(15)) == "추천"  # 8 / 15
    less = _half_green(7)
    assert tiering.segment_keyword(less, F(0), F(15)) == "보통"  # 7 / 15


def test_any_red_inside_keeps_the_segment_from_green() -> None:
    document = make_document(30, [(0, 20, "recommended"), (20, 30, "usable")], chatter(0, 20))
    report = tiering.classify(document)
    assert [tier for _a, _b, tier in layout(report)] == [G, R]
    assert tiering.segment_keyword(report, F(0), F(30)) == "보통"


def test_a_short_segment_still_needs_three_seconds() -> None:
    report = _half_green(10)
    assert tiering.segment_keyword(report, F(8), F(10) + F(1, 2)) == "보통"


def test_a_held_half_keeps_the_segment_from_green() -> None:
    """뒤 5초는 AI 판정이 없다(보류) — 앞 10초 녹색이 보류를 덮어 추천으로 만들지 않는다."""
    document = make_document(15, [(0, 10, "recommended"), (10, 15, "recommended")], chatter(0, 15))
    document["annotations"][1]["status"] = "failed"
    report = tiering.classify(document)
    assert [tier for _a, _b, tier in layout(report)] == [G, Y]
    assert report.spans[-1].hold
    assert tiering.segment_keyword(report, F(0), F(15)) == "보통"


def test_a_dark_half_keeps_the_segment_from_green_and_keeps_its_reason() -> None:
    """뒤 5초가 어둡다 — 녹색 절반이 그 까닭을 지우지 않고 조각은 보통이다."""
    document = make_document(15, [(0, 10, "recommended"), (10, 15, "usable")], chatter(0, 15))
    document["annotations"][1]["visual_issues"] = ["dark"]
    report = tiering.classify(document)
    assert [tier for _a, _b, tier in layout(report)] == [G, Y]
    assert tiering.segment_keyword(report, F(0), F(15)) == "보통"
    _recommendation, tags = tiering.segment_grade(report, F(0), F(15))
    assert any(tag.startswith(tiering.CAP_TAG_PREFIX) for tag in tags)


def test_subtitle_verified_still_names_its_rule_and_lift_under_v6() -> None:
    """v6에서 `usable` × 발화 많음은 이미 녹색이다 — 「자막 확인됨」은 규칙 이름과 근거만
    남긴다(D65)."""
    document = make_document(15, [(0, 15, "usable")], chatter(0, 15))
    timing = tiering.TimingSignals(
        level=tiering.TIMING_GREEN, subtitle_verified=True, subtitle_lines=10
    )
    (span,) = tiering.classify(document, timing=timing).spans
    assert (span.tier, span.rule) == (G, tiering.RULE_SUBTITLE_VERIFIED)
    assert span.lift


def test_v6_never_creates_removes_or_moves_red() -> None:
    """무작위 문서 300개 — v5 규칙과 v6의 경계·빨강·제외 제안 후보가 같다(화면 문제를 달아도)."""
    rng = random.Random(79)
    for _ in range(300):
        document = _random_document(rng)
        for doc in (document, _with_issues(document, rng)):
            v6 = tiering.classify(doc)
            v5 = tiering.classify(doc, settings=V5_RULES)
            assert [(s.start, s.end) for s in v6.spans] == [(s.start, s.end) for s in v5.spans]
            assert json.dumps(tiering.red_decisions(v6)) == json.dumps(tiering.red_decisions(v5))
            assert tiering.red_ranges(v6) == tiering.red_ranges(v5)
            for new, old in zip(v6.spans, v5.spans, strict=True):
                assert (new.tier == R) == (old.tier == R)


def test_v5_rules_keep_the_all_green_segment_rule() -> None:
    document = make_document(15, [(0, 10, "recommended"), (10, 15, "review")], chatter(0, 15))
    report = tiering.classify(document, settings=V5_RULES)
    assert tiering.segment_keyword(report, F(0), F(15)) == "보통"


def test_red_decisions_are_the_same_under_v5_and_v6() -> None:
    document = make_document(
        60,
        [(0, 20, "recommended"), (20, 40, "usable"), (40, 60, "review")],
        chatter(0, 15) + chatter(45, 50),
    )
    v6 = tiering.red_decisions(tiering.classify(document))
    v5 = tiering.red_decisions(tiering.classify(document, settings=V5_RULES))
    assert [(d["start"], d["end"]) for d in v6] == [(d["start"], d["end"]) for d in v5]


def test_settings_type_keeps_the_new_fields() -> None:
    fields = set(RawTierSettings.__dataclass_fields__)
    assert {
        "usable_speech_green",
        "scenery_green",
        "good_sparse_green",
        "visual_issue_caps",
        "segment_green_share",
    } <= fields


def test_the_issue_kinds_match_the_labels() -> None:
    from vlogsrt.project.raw.analysis_config import VISUAL_ISSUE_KINDS

    assert set(VISUAL_ISSUE_KINDS) == set(tiering.VISUAL_ISSUE_LABELS)
