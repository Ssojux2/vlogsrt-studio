"""W-39a(D99) — 라이브러리 전체 맥락 평가: 순수 계층과 등급 `raw-tier/v7`.

시험이 못 박는 것:

- 맥락 판정은 등급의 마지막 단계에서만 쓰인다 — 녹색 + 중복·맥락 밖(확신 ≥ medium) → 보통
  (「추천 제한: 맥락: …」), 좁은 칸의 노랑 + 핵심(확신 high) → 추천(「추천 근거: 맥락: …」).
  경계·빨강·보류·추천 제한이 있는 노랑은 그대로다. 판정이 없거나 모르면 v6과 같은 등급이다.
- 입력: 빨강 구간은 싣지 않고, 구간 아이디는 `S001`부터 라이브러리 전체에 하나씩. 예산을 넘는
  줄·창·글자는 줄이고 센다.
- 응답 해석은 엄격하다 — 모르는·두 번 나온 아이디, 목록 밖 판정·확신, 없는 겹침 대상은 거부.
- 등급 값은 원본마다 나뉘고, 겹친 구간은 까닭에 그 자리(원본 · 분:초)를 붙인다.
- 입력 열쇠는 원본·리비전·자막 리비전·등급판·모델·예산에 민감하다.

유료 호출은 없다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction

import pytest

from tests.raw.test_tiering_v3 import chatter, make_document
from vlogsrt.project.raw import context_review as cr
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.analysis_config import (
    TIER_SETTINGS,
    ContextReviewSettings,
    tier_settings_problems,
)

F = Fraction
G, Y, R = tiering.TIER_GREEN, tiering.TIER_YELLOW, tiering.TIER_RED
SPARSE = [(F(t), F(t) + F(1, 2), "와 예쁘다") for t in (0, 4, 8, 12)]


def _signals(span: tiering.TierSpan, verdict: str, confidence: str, reason: str = "같은 장면"):
    return tiering.ContextSignals(
        {(span.start, span.end): tiering.ContextVerdict(verdict, confidence, reason)}
    )


# ── 등급 v7 ──────────────────────────────────────────────────────────────


def test_v7_is_the_published_version_and_context_knobs_are_checked() -> None:
    assert TIER_SETTINGS.tier_version == "raw-tier/v7"
    assert TIER_SETTINGS.context_lower_verdicts == ("redundant", "off_topic")
    assert TIER_SETTINGS.context_raise_verdicts == ("core",)
    assert (
        TIER_SETTINGS.context_lower_min_confidence,
        TIER_SETTINGS.context_raise_min_confidence,
    ) == (
        "medium",
        "high",
    )
    for changes in (
        {"context_lower_verdicts": ("core",)},  # 올리기와 겹친다
        {"context_raise_verdicts": ("great",)},
        {"context_lower_min_confidence": "sure"},
        {"context_raise_rules": ["speech-sparse"]},
    ):
        assert tier_settings_problems(replace(TIER_SETTINGS, **changes)), changes
    with pytest.raises(ValueError):
        ContextReviewSettings(max_line_chars=0)


def test_a_redundant_green_span_is_lowered_with_its_reason() -> None:
    document = make_document(15, [(0, 15, "usable")], chatter(0, 15))
    (green,) = tiering.classify(document).spans
    assert green.tier == G
    (lowered,) = tiering.classify(document, context=_signals(green, "redundant", "medium")).spans
    assert (lowered.tier, lowered.rule) == (Y, green.rule)
    assert lowered.caps[-1] == (tiering.CAP_CONTEXT, "맥락: 중복 — 같은 장면")
    (kept,) = tiering.classify(document, context=_signals(green, "redundant", "low")).spans
    assert kept.tier == G  # 확신이 문턱 밑이면 그대로
    report = tiering.classify(document, context=_signals(green, "off_topic", "high"))
    recommendation, tags = tiering.segment_grade(report, F(0), F(15))
    assert recommendation != "recommended"
    assert "추천 제한: 맥락: 맥락 밖 — 같은 장면" in tags and tags[-1] == "등급판 raw-tier/v7"


def test_only_a_narrow_yellow_cell_is_raised_and_only_with_high_core() -> None:
    document = make_document(15, [(0, 15, "usable")], SPARSE)
    (yellow,) = tiering.classify(document).spans
    assert (yellow.tier, yellow.rule) == (Y, "speech-sparse")
    (raised,) = tiering.classify(document, context=_signals(yellow, "core", "high", "도착")).spans
    assert (raised.tier, raised.lift) == (G, "맥락: 핵심 — 도착")
    (medium,) = tiering.classify(document, context=_signals(yellow, "core", "medium")).spans
    assert medium.tier == Y
    supporting = tiering.classify(document, context=_signals(yellow, "supporting", "high")).spans
    assert supporting[0].tier == Y


def test_red_hold_and_capped_spans_are_never_raised_and_bounds_never_move() -> None:
    red_doc = make_document(15, [(0, 15, "usable")], [])
    (red,) = tiering.classify(red_doc).spans
    assert red.tier == R
    assert tiering.classify(red_doc, context=_signals(red, "core", "high")).spans[0].tier == R
    hold_doc = make_document(15, [(0, 15, "usable")], SPARSE)
    hold_doc["annotations"] = []  # 영상 판정이 없다 — 판정 보류
    (hold,) = tiering.classify(hold_doc).spans
    assert hold.hold
    assert tiering.classify(hold_doc, context=_signals(hold, "core", "high")).spans[0] == hold
    dark = make_document(15, [(0, 15, "usable")], chatter(0, 15))
    for row in dark["annotations"]:
        row["visual_issues"] = ["dark"]
    (capped,) = tiering.classify(dark).spans
    assert capped.tier == Y and capped.caps
    assert tiering.classify(dark, context=_signals(capped, "core", "high")).spans[0] == capped


def test_unknown_or_unmatched_context_changes_nothing() -> None:
    document = make_document(15, [(0, 15, "usable"), (15, 30, "recommended")], chatter(0, 30))
    plain = tiering.classify(document)
    assert tiering.classify(document, context=tiering.UNKNOWN_CONTEXT) == plain
    stray = tiering.ContextSignals(
        {(F(1), F(2)): tiering.ContextVerdict("redundant", "high", "경계가 다른 구간")}
    )
    assert tiering.classify(document, context=stray) == plain


# ── 입력 · 해석 · 값 · 열쇠 ─────────────────────────────────────────────────


def _seg(span_id: str, verdict: str, confidence: str, reason: str, duplicate: str | None) -> dict:
    return {
        "span_id": span_id,
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason,
        "duplicate_of": duplicate,
    }


def _key(rows: list, *, tier: str = "raw-tier/v7", model: str = "gemini-3.8-flash", **kw) -> str:
    return cr.context_key(rows, tier_version=tier, model=model, **kw)


def _source(asset_id: str, name: str, *tiers: str, lines: int = 3) -> cr.ContextSource:
    spans = tuple(
        cr.ContextSpan(F(i * 10), F(i * 10 + 10), tier, "rule", "speech_dense", "usable")
        for i, tier in enumerate(tiers)
    )
    windows = (cr.ContextWindow(F(0), F(10), "인사", "카페 안", ("indoor",)),)
    rows = tuple(cr.ContextLine(F(i), F(i) + F(1, 2), f"줄 {i}") for i in range(lines))
    return cr.ContextSource(asset_id, name, spans, windows, rows)


def test_the_input_numbers_spans_across_the_library_and_skips_red() -> None:
    given = cr.build_input([_source("a", "IMG_1.MOV", G, R, Y), _source("b", "IMG_2.MOV", G)])
    assert list(given.spans) == ["S001", "S002", "S003"]
    assert given.spans["S002"] == cr.SpanRef("a", "IMG_1.MOV", F(20), F(30))
    body = json.loads(given.text)
    assert [row["name"] for row in body["sources"]] == ["IMG_1.MOV", "IMG_2.MOV"]
    assert body["sources"][0]["spans"][0] == {
        "span_id": "S001",
        "at": "00:00.0–00:10.0",
        "tier": "추천",
        "audio": "speech_dense",
        "video": "usable",
    }
    assert (given.dropped_lines, given.dropped_windows, given.clipped_texts) == (0, 0, 0)


def test_the_budget_trims_and_counts() -> None:
    settings = ContextReviewSettings(
        max_lines_per_source=2, max_line_chars=2, max_windows_per_source=1
    )
    source = _source("a", "IMG_1.MOV", G, lines=5)
    long = replace(source, windows=source.windows * 3)
    given = cr.build_input([long], settings)
    assert (given.dropped_lines, given.dropped_windows) == (3, 2)
    assert given.clipped_texts >= 2 and "줄 …" in given.text


def test_parse_is_strict_and_counts_unjudged_spans() -> None:
    given = cr.build_input([_source("a", "IMG_1.MOV", G, Y, G)])
    good = {
        "storyline_summary": " 카페에서 하루를 시작한다. ",
        "segments": [
            _seg("S001", "core", "high", "도착", None),
            _seg("S003", "redundant", "medium", "같은 말", "S001"),
        ],
    }
    review = cr.parse(good, given)
    assert review.storyline == "카페에서 하루를 시작한다." and review.unjudged == 1
    for broken in (
        {"segments": []},
        {**good, "segments": [{**good["segments"][0], "span_id": "S999"}]},
        {**good, "segments": [good["segments"][0], good["segments"][0]]},
        {**good, "segments": [{**good["segments"][0], "verdict": "great"}]},
        {**good, "segments": [{**good["segments"][0], "confidence": "sure"}]},
        {**good, "segments": [{**good["segments"][1], "duplicate_of": "S404"}]},
        {**good, "segments": ["S001"]},
    ):
        with pytest.raises(cr.ContextReviewError):
            cr.parse(broken, given)


def test_signals_are_split_per_source_and_name_the_duplicate_place() -> None:
    given = cr.build_input([_source("a", "IMG_1.MOV", G), _source("b", "IMG_2.MOV", G)])
    review = cr.parse(
        {
            "storyline_summary": "",
            "segments": [
                _seg("S001", "core", "high", "", None),
                _seg("S002", "redundant", "high", "같은 장면", "S001"),
            ],
        },
        given,
    )
    first = cr.signals_for(review, given, "a").verdicts
    second = cr.signals_for(review, given, "b").verdicts
    assert list(first) == [(F(0), F(10))] and first[(F(0), F(10))].verdict == "core"
    assert second[(F(0), F(10))].reason == "같은 장면 (IMG_1.MOV 00:00와 겹침)"
    assert cr.signals_for(review, given, "z").verdicts == {}


def test_the_key_is_sensitive_to_every_input() -> None:
    rows = [("a", "rev1", "sha1", "tr1"), ("b", "rev1", "sha2", "tr2")]
    base = _key(rows)
    assert base == _key(rows)
    changed = [
        _key([rows[0], ("b", "rev1", "sha2", "tr3")]),
        _key(rows[::-1]),
        _key(rows, tier="raw-tier/v8"),
        _key(rows, model="gemini-4-argon"),
        _key(rows, settings=ContextReviewSettings(max_lines_per_source=9)),
    ]
    assert base not in changed and len(set(changed)) == len(changed)
