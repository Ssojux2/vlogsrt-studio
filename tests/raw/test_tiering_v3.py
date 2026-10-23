"""추천 기준 강화 v3(W-20 20a, D60) — 확인된 말소리 밀도 · 최소 3초 · 시각 빨강이면 보통까지.

순수 계층 시험이다: 문서와 값(`TimingSignals`)만 넣고 보고서를 본다. 자막 시각 확인(D55)의 신호는
**녹색을 노랑으로 내릴 수만** 있다 — 구간 경계를 옮기지 않고, 빨강(제외 제안)을 만들거나 지우거나
옮기지 못하며, 자동 제외 후보(`red_decisions`)가 바이트까지 같다. 유료 호출 0회.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from fractions import Fraction

import pytest

from tests.raw.tier_rules import V5_RULES
from vlogsrt.project import sync_check
from vlogsrt.project.raw import subtitle_suspects, tiering
from vlogsrt.project.raw.analysis_config import (
    TIER_SETTINGS,
    RawTierSettings,
    validate_tier_settings,
)

F = Fraction
G, Y, R = tiering.TIER_GREEN, tiering.TIER_YELLOW, tiering.TIER_RED
V2_RULES = replace(
    V5_RULES,
    same_text_run_min=10_000,
    slow_filler_chars_per_second=0.0,
    recommended_min_seconds=0.0,
)
"""v3의 문서 규칙(같은 말 런·느린 추임새·최소 3초)을 끈 설정 — v2와 같은 판정을 낸다.
v6(D79)의 넓힌 추천(칸 셋·화면 문제 종류·조각 몫)도 끈다 — v5 규칙 위에서 잰다."""


def _rj(value: Fraction | int | str) -> dict[str, int]:
    value = F(value)
    return {"num": value.numerator, "den": value.denominator}


def make_document(
    extent: Fraction | int,
    windows: list[tuple[Fraction | int, Fraction | int, str]],
    utterances: list[tuple[Fraction | int | str, Fraction | int | str, str]],
) -> dict:
    """합성 분석 문서 — 실제 기록의 키 모양(창·판정·소리·발화)만 갖춘다."""
    return {
        "windows": {
            "extent": _rj(extent),
            "windows": [
                {
                    "window_id": f"window_{i:06d}",
                    "core_start": _rj(a),
                    "core_end": _rj(b),
                    "frame_ids": [],
                }
                for i, (a, b, _) in enumerate(windows)
            ],
        },
        "annotations": [
            {
                "window_id": f"window_{i:06d}",
                "status": "accepted",
                "suitability": suitability,
                "suitability_evaluated": True,
                "suitability_reason": "합성",
            }
            for i, (_, _, suitability) in enumerate(windows)
        ],
        "audio": {"status": "decoded", "extent": _rj(extent)},
        "utterances": [
            {
                "utterance_id": f"utterance_{i:06d}",
                "start": _rj(a),
                "end": _rj(b),
                "text": text,
            }
            for i, (a, b, text) in enumerate(utterances)
        ],
    }


STEP = F(5, 2)
TIMING_RED_TEXT = (
    "자막 시각 확인이 빨강이라 보통까지만 추천합니다 — 시각을 맞춘 뒤 다시 재면 풀립니다"
)


def chatter(start: int, end: int) -> list[tuple[Fraction, Fraction, str]]:
    """빠르고 서로 다른 말을 2.5초씩 잇는다(15자/2.5초 — 런도 느린 추임새도 아니다)."""
    rows = []
    cursor, index = F(start), 0
    while cursor < end:
        rows.append(
            (cursor, min(cursor + STEP, F(end)), f"이야기 {index}번째 줄입니다 정말 좋네요")
        )
        cursor, index = cursor + STEP, index + 1
    return rows


def cue_key(document: dict, index: int) -> tuple[str, Fraction, Fraction]:
    row = document["utterances"][index]
    return (
        row["utterance_id"],
        F(row["start"]["num"], row["start"]["den"]),
        F(row["end"]["num"], row["end"]["den"]),
    )


def kinds(document: dict, timing: tiering.TimingSignals = tiering.UNKNOWN_TIMING) -> dict:
    """발화 아이디 → 밀도에서 뺀 까닭."""
    found = tiering.unconfirmed_speech(document, TIER_SETTINGS, timing)
    return {uid: kind for (_start, _end, uid), kind in found.items()}


def layout(report: tiering.TierReport) -> list[tuple[Fraction, Fraction, str]]:
    return [(s.start, s.end, s.tier) for s in report.spans]


TALK = "오늘은 계곡에 왔어요 물이 정말 맑네요"  # 16자 — 2–3초면 빠른 말이다


# ── 설정 ─────────────────────────────────────────────────────────────────


def test_v3_settings_are_the_published_values():
    validate_tier_settings(TIER_SETTINGS)
    validate_tier_settings(V2_RULES)  # 0은 「끔」이다 — 값역 안
    assert TIER_SETTINGS.tier_version == "raw-tier/v6"
    assert TIER_SETTINGS.same_text_run_min == 3
    assert TIER_SETTINGS.slow_filler_chars_per_second == 1.5
    assert TIER_SETTINGS.slow_filler_min_seconds == 2.0
    assert TIER_SETTINGS.recommended_min_seconds == 3.0
    # v2 값은 그대로다(D51·D52).
    assert TIER_SETTINGS.speech_density_green == 0.35
    assert (TIER_SETTINGS.silence_min_seconds, TIER_SETTINGS.speech_handle_seconds) == (5.0, 0.3)
    assert TIER_SETTINGS.min_span_seconds == 1.0


@pytest.mark.parametrize(
    "settings",
    [
        RawTierSettings(same_text_run_min=1),
        RawTierSettings(same_text_run_min=True),
        RawTierSettings(same_text_run_min=2.5),  # type: ignore[arg-type]
        RawTierSettings(slow_filler_chars_per_second=-0.1),
        RawTierSettings(slow_filler_chars_per_second=float("nan")),
        RawTierSettings(slow_filler_min_seconds=0.0),
        RawTierSettings(slow_filler_min_seconds=float("inf")),
        RawTierSettings(recommended_min_seconds=-1.0),
        RawTierSettings(recommended_min_seconds=float("nan")),
    ],
)
def test_v3_settings_reject_contract_violations(settings: RawTierSettings):
    with pytest.raises(ValueError):
        validate_tier_settings(settings)


def test_timing_red_is_the_sync_check_red_level():
    """등급은 확인 계층을 모른다 — 빨강의 값만 같다(시험이 못 박는다)."""
    assert tiering.TIMING_RED == sync_check.LEVEL_RED
    assert tiering.TimingSignals() == tiering.UNKNOWN_TIMING
    assert tiering.UNKNOWN_TIMING.level is None and tiering.UNKNOWN_TIMING.no_speech == frozenset()


# ── 규칙 1 — 밀도에서 빼는 줄(줄 하나하나의 결정표) ───────────────────────


def test_same_text_run_of_three_is_excluded_and_two_is_not():
    doc = make_document(
        30,
        [(0, 30, "recommended")],
        [
            (0, 1, "개좋아"),
            (1, 2, "개좋아!"),  # 부호만 다르다 — 같은 말(19e의 접기)
            (2, 3, " 개 좋아 "),  # 공백만 다르다
            (5, 6, "네"),
            (6, 7, "네."),  # 둘뿐 — 맞장구다
            (8, 9, "와"),
        ],
    )
    assert kinds(doc) == {
        "utterance_000000": tiering.UNCONFIRMED_REPEAT,
        "utterance_000001": tiering.UNCONFIRMED_REPEAT,
        "utterance_000002": tiering.UNCONFIRMED_REPEAT,
    }


def test_punctuation_only_line_breaks_a_run():
    doc = make_document(
        30,
        [(0, 30, "recommended")],
        [(0, 1, "좋아"), (1, 2, "좋아"), (2, 3, "..."), (3, 4, "좋아"), (4, 5, "좋아")],
    )
    assert kinds(doc) == {}
    longer = json.loads(json.dumps(doc))
    for row, text in zip(
        longer["utterances"], ["좋아", "좋아", "좋아", "...", "좋아"], strict=True
    ):
        row["text"] = text
    assert set(kinds(longer)) == {"utterance_000000", "utterance_000001", "utterance_000002"}


@pytest.mark.parametrize(
    ("start", "end", "text", "slow"),
    [
        ("0", "2", "음", True),  # 0.5자/초 · 정확히 2초 — 2초 이상이면 본다
        ("0", "2", "하아", True),  # 1.0자/초
        ("0", "2", "아이고", False),  # 정확히 1.5자/초 — 「보다 느림」이 아니다
        ("0", "201/100", "아이고", True),  # 3 / 2.01 = 1.49…자/초
        ("0", "199/100", "음", False),  # 2초보다 짧다 — 느려도 보지 않는다
        ("0", "24", "잘 나온다", True),  # 0024 실측 — 4자가 24초
        ("0", "2", "Cut!", False),  # 라틴은 철자마다 한 자 — 3자/2초 = 1.5
        ("0", "3", "Cut!", True),  # 3자/3초 = 1.0
        ("0", "2", "ㅋㅋㅋ", False),  # 호환 자모도 한 자씩 — 3자/2초 = 1.5
        ("0", "2", "♪", True),  # 글자·숫자가 없다 — 0자
        ("0", "2", "100%", False),  # 숫자는 센다 — 3자/2초
    ],
)
def test_slow_filler_decision_table(start: str, end: str, text: str, slow: bool):
    doc = make_document(30, [(0, 30, "recommended")], [(start, end, text)])
    expected = {"utterance_000000": tiering.UNCONFIRMED_SLOW} if slow else {}
    assert kinds(doc) == expected


def test_no_speech_flag_counts_only_for_the_same_cue_id_and_times():
    doc = make_document(30, [(0, 30, "recommended")], [(0, 3, TALK), (5, 8, TALK), (10, 13, TALK)])
    doc["utterances"][1]["text"] = TALK + " 둘"
    doc["utterances"][2]["text"] = TALK + " 셋"
    first, second, third = (cue_key(doc, i) for i in range(3))
    timing = tiering.TimingSignals(
        level=sync_check.LEVEL_AMBER,
        no_speech=frozenset(
            {
                first,  # 같은 줄 — 뺀다
                (second[0], second[1], second[2] + F(1, 10)),  # 시각이 다르다 — 다른 리비전의 줄
                ("utterance_999999", third[1], third[2]),  # 아이디가 다르다
            }
        ),
    )
    assert kinds(doc, timing) == {"utterance_000000": tiering.UNCONFIRMED_NO_SPEECH}


def test_one_kind_per_line_text_reasons_before_measurement():
    """되풀이·느린 추임새가 먼저다 — 「측정 한계일 수 있음」은 측정만으로 뺀 줄에만 붙는다."""
    doc = make_document(
        30,
        [(0, 30, "recommended")],
        [(0, 2, "음"), (2, 4, "음"), (4, 6, "음"), (10, 12, "응"), (20, 23, TALK)],
    )
    timing = tiering.TimingSignals(
        level=sync_check.LEVEL_GREEN,
        no_speech=frozenset(cue_key(doc, i) for i in range(5)),
    )
    assert kinds(doc, timing) == {
        "utterance_000000": tiering.UNCONFIRMED_REPEAT,
        "utterance_000001": tiering.UNCONFIRMED_REPEAT,
        "utterance_000002": tiering.UNCONFIRMED_REPEAT,
        "utterance_000003": tiering.UNCONFIRMED_SLOW,
        "utterance_000004": tiering.UNCONFIRMED_NO_SPEECH,
    }


def test_run_rule_matches_the_19e_display_rule():
    """같은 말 런은 19e 「확인 필요」와 같은 접기·같은 문턱이다 — 무작위 줄로 견준다."""
    words = ["개좋아", "개좋아!", "Cut", "cut.", "...", "네", "와우", " 와 우 "]
    rng = random.Random(20)
    for _ in range(200):
        texts = [rng.choice(words) for _ in range(rng.randint(1, 14))]
        doc = make_document(
            60,
            [(0, 60, "recommended")],
            [(i, F(i) + F(1, 2), text) for i, text in enumerate(texts)],
        )
        cues = [(row["utterance_id"], row["text"]) for row in doc["utterances"]]
        shown = {
            cue_id
            for cue_id, found in subtitle_suspects.suspects(cues).items()
            if any(item.code == subtitle_suspects.REPEAT for item in found)
        }
        ours = {uid for uid, kind in kinds(doc).items() if kind == tiering.UNCONFIRMED_REPEAT}
        assert ours == shown, texts
    assert (
        TIER_SETTINGS.same_text_run_min
        == subtitle_suspects.SUBTITLE_SUSPECT_SETTINGS.repeat_run_min
    )


# ── 규칙 1 — 구간의 밀도 ─────────────────────────────────────────────────


def test_density_counts_only_confirmed_speech_and_threshold_is_inclusive():
    # 0–10초 창 하나(추천) · 0–3.5초 빠른 말 · 6–9초 느린 추임새(1자/3초)
    doc = make_document(10, [(0, 10, "recommended")], [(0, "7/2", TALK), (6, 9, "음")])
    judged = tiering.judge_audio(tiering.evidence_of(doc), F(0), F(10))
    assert judged.status == tiering.AUDIO_SPEECH_DENSE and judged.density == F(35, 100)
    assert judged.excluded == (("utterance_000001", tiering.UNCONFIRMED_SLOW),)
    assert "느린 추임새 1개를 밀도에서 뺐습니다" in judged.reason
    # 0.35 바로 밑이면 보통이다 — 뺀 줄이 없었다면 0.649로 녹색이었다.
    short = make_document(10, [(0, 10, "recommended")], [(0, "349/100", TALK), (6, 9, "음")])
    judged = tiering.judge_audio(tiering.evidence_of(short), F(0), F(10))
    assert judged.status == tiering.AUDIO_SPEECH_SPARSE
    assert tiering.classify(short, settings=V5_RULES).spans[0].tier == Y
    assert tiering.classify(short, settings=V2_RULES).spans[0].tier == G


def test_speech_presence_still_uses_every_line_so_nothing_becomes_red():
    """빼는 것은 밀도뿐이다 — 말이 있던 자리는 여전히 '말이 있는 자리'라 빨강이 되지 않는다."""
    doc = make_document(
        30,
        [(0, 30, "usable")],
        [(i, i + 1, "개좋아") for i in range(0, 30, 2)],
    )
    report = tiering.classify(doc)
    assert all(span.tier != R for span in report.spans)
    assert {span.audio.status for span in report.spans} == {tiering.AUDIO_SPEECH_SPARSE}
    assert all(span.audio.density == 0 for span in report.spans)
    assert tiering.red_decisions(report) == []


def test_hallucinated_repetition_like_0024_no_longer_recommends():
    """0024 실측 모양 — 540–720초에 「이곳은 정말 아름다운 곳입니다.」 10초짜리 18장.

    환각 되풀이 하나만으로 180초가 추천이던 자리다.
    """
    utterances = [
        (540 + 10 * i, 550 + 10 * i, " 이곳은 정말 아름다운 곳입니다.") for i in range(18)
    ]
    doc = make_document(720, [(540, 720, "recommended")], utterances)
    v2 = tiering.classify(doc, settings=V2_RULES)
    assert [(s.start, s.end, s.tier) for s in v2.spans if s.end > 540] == [(F(540), F(720), G)]
    report = tiering.classify(doc, settings=V5_RULES)
    (tail,) = [s for s in report.spans if s.end > 540]
    assert (tail.start, tail.end, tail.tier) == (F(540), F(720), Y)
    assert tail.audio.status == tiering.AUDIO_SPEECH_SPARSE and tail.audio.density == 0
    assert "같은 말이 되풀이된 줄 18개를 밀도에서 뺐습니다" in tail.audio.reason
    assert tiering.segment_keyword(report, F(540), F(720)) == "보통"
    assert tiering.red_decisions(report) == tiering.red_decisions(v2)


# ── 규칙 1 — 말소리 측정상 없음(시각 확인 신호): 녹색 구간의 밀도만 다시 잰다 ─────────


def test_no_speech_reason_keeps_the_measurement_caveat_and_never_says_vad():
    doc = make_document(10, [(0, 10, "recommended")], [(0, 6, TALK)])
    timing = tiering.TimingSignals(
        level=sync_check.LEVEL_GREEN, no_speech=frozenset({cue_key(doc, 0)})
    )
    report = tiering.classify(doc, timing=timing, settings=V5_RULES)
    (span,) = report.spans
    assert (span.tier, span.rule, span.audio.density) == (Y, "speech-sparse", 0)
    assert "말소리 측정상 없는 줄 1개를 밀도에서 뺐습니다(측정 한계일 수 있음)" in span.audio.reason
    text = json.dumps(report.document(), ensure_ascii=False)
    assert "VAD" not in text and "vad" not in text.lower()
    assert span.document()["audio"]["excluded"] == [
        {"id": "utterance_000000", "kind": tiering.UNCONFIRMED_NO_SPEECH}
    ]


def test_no_speech_lowers_a_green_span_only_when_its_density_falls_below_the_line():
    """녹색 구간 하나(0–30초) — 측정이 짚은 줄을 빼도 0.35 이상이면 녹색 그대로, 근거만 붙는다."""
    rows = chatter(0, 30)
    doc = make_document(30, [(0, 30, "recommended")], rows)
    few = tiering.TimingSignals(
        level=sync_check.LEVEL_AMBER, no_speech=frozenset(cue_key(doc, i) for i in range(4))
    )
    kept = tiering.classify(doc, timing=few).spans[0]
    assert (kept.tier, kept.audio.density) == (G, F(20, 30))
    assert "말소리 측정상 없는 줄 4개를 밀도에서 뺐습니다(측정 한계일 수 있음)" in kept.audio.reason
    many = tiering.TimingSignals(
        level=sync_check.LEVEL_AMBER, no_speech=frozenset(cue_key(doc, i) for i in range(9))
    )
    lowered = tiering.classify(doc, timing=many, settings=V5_RULES).spans[0]
    assert (lowered.tier, lowered.audio.status) == (Y, tiering.AUDIO_SPEECH_SPARSE)
    assert lowered.audio.density == F(15, 2) / 30


def test_timing_signals_never_move_a_boundary_and_only_lower_green():
    """무작위 문서 300개 — 신호가 있든 없든 구간 경계가 같고, 바뀌는 것은 녹색 → 노랑뿐이다."""
    rng = random.Random(60)
    for _ in range(300):
        doc = _random_document(rng)
        plain = tiering.classify(doc)
        for level in (sync_check.LEVEL_RED, sync_check.LEVEL_GREEN, None):
            signalled = tiering.classify(doc, timing=_signals(doc, rng, level))
            assert [(s.start, s.end) for s in signalled.spans] == [
                (s.start, s.end) for s in plain.spans
            ]
            for before, after in zip(plain.spans, signalled.spans, strict=True):
                assert after.tier == before.tier or (before.tier, after.tier) == (G, Y)
                assert (after.red_reason, after.video) == (before.red_reason, before.video)
            assert json.dumps(tiering.red_decisions(signalled)) == json.dumps(
                tiering.red_decisions(plain)
            )
        _assert_issues_only_lower_green(doc, plain, rng)


def test_v3_document_rules_never_create_remove_or_move_red():
    """무작위 문서 300개 — v2 규칙(v3 문서 규칙을 끈 설정)과 v3의 빨강·제외 제안 후보가 같다."""
    rng = random.Random(61)
    for _ in range(300):
        doc = _random_document(rng)
        v3 = tiering.classify(doc, timing=_signals(doc, rng, sync_check.LEVEL_RED))
        v2 = tiering.classify(doc, settings=V2_RULES)
        assert json.dumps(tiering.red_decisions(v3)) == json.dumps(tiering.red_decisions(v2))
        assert tiering.red_ranges(v3) == tiering.red_ranges(v2)
        flagged = _with_issues(doc, rng)
        v4 = tiering.classify(flagged, timing=_signals(flagged, rng, sync_check.LEVEL_RED))
        assert json.dumps(tiering.red_decisions(v4)) == json.dumps(tiering.red_decisions(v2))
        assert tiering.red_ranges(v4) == tiering.red_ranges(v2)


def _random_document(rng: random.Random) -> dict:
    extent = rng.randint(20, 120)
    cuts = sorted(rng.sample(range(1, extent), rng.randint(0, 4)))
    edges = [0, *cuts, extent]
    suitability = ("recommended", "usable", "review")
    windows = [(a, b, rng.choice(suitability)) for a, b in zip(edges, edges[1:], strict=False)]
    texts = ["개좋아", "음", "하아", TALK, "와 진짜 대박이다", "♪", "Cut"]
    utterances = []
    cursor = F(0)
    while cursor < extent - 1:
        cursor += F(rng.randint(0, 80), 10)
        length = F(rng.randint(3, 40), 10)
        if cursor + length >= extent:
            break
        utterances.append((cursor, cursor + length, rng.choice(texts)))
        cursor += length
    return make_document(extent, windows, utterances)


ISSUE_CHOICES: tuple[list[str] | None, ...] = (
    None, [], ["none"], ["blur"], ["shake", "dark"], ["overexposed"], ["obstructed", "blur"],
)  # fmt: skip
"""판정의 `visual_issues` 모양 — 키 없음 · 빈 칸(알 수 없음) · `none` · 실제 문제(정규화 뒤)."""


def _with_issues(doc: dict, rng: random.Random) -> dict:
    """같은 문서에 창마다 무작위 `visual_issues`를 단 사본 — 그 밖은 바이트까지 같다."""
    flagged = json.loads(json.dumps(doc))
    for row in flagged["annotations"]:
        picked = rng.choice(ISSUE_CHOICES)
        if picked is not None:
            row["visual_issues"] = picked
    return flagged


def _assert_issues_only_lower_green(doc: dict, plain: tiering.TierReport, rng: random.Random):
    """화면 문제는 녹색 → 노랑(`visual_issues` 상한)만 — 경계·영상 판정·빨강·제외 후보 그대로."""
    flagged = tiering.classify(_with_issues(doc, rng))
    assert [(s.start, s.end) for s in flagged.spans] == [(s.start, s.end) for s in plain.spans]
    for before, after in zip(plain.spans, flagged.spans, strict=True):
        assert (after.rule, after.red_reason, after.video.status) == (
            before.rule,
            before.red_reason,
            before.video.status,
        )
        assert after.tier == before.tier or (before.tier, after.tier) == (G, Y)
        if after.tier != before.tier:
            assert after.caps[-1][0] == tiering.CAP_VISUAL_ISSUES
    assert json.dumps(tiering.red_decisions(flagged)) == json.dumps(tiering.red_decisions(plain))


def _signals(doc: dict, rng: random.Random, level: str | None) -> tiering.TimingSignals:
    picked = frozenset(cue_key(doc, i) for i in range(len(doc["utterances"])) if rng.random() < 0.5)
    return tiering.TimingSignals(level=level, no_speech=picked)


# ── 규칙 2 — 최소 3초 ────────────────────────────────────────────────────


def _green_island(length: Fraction) -> dict:
    """노랑 사이의 녹색 섬 — 10초부터 `length`초 동안만 추천 창이다."""
    end = 10 + length
    return make_document(
        30,
        [(0, 10, "usable"), (10, end, "recommended"), (end, 30, "usable")],
        chatter(0, 30),
    )


def test_green_span_shorter_than_three_seconds_is_yellow_with_a_reason():
    exact = tiering.classify(_green_island(F(3)), settings=V5_RULES)
    assert layout(exact) == [(F(0), F(10), Y), (F(10), F(13), G), (F(13), F(30), Y)]
    short = tiering.classify(_green_island(F(299, 100)), settings=V5_RULES)
    island = short.spans[1]
    assert (island.start, island.end, island.tier, island.rule) == (
        F(10),
        F(1299, 100),
        Y,
        "both-good",
    )
    assert island.caps == ((tiering.CAP_TOO_SHORT, "3초보다 짧아 추천하지 않습니다"),)
    assert island.recommendation == "normal" and not island.hold
    assert island.document()["caps"] == [
        {"code": tiering.CAP_TOO_SHORT, "text": "3초보다 짧아 추천하지 않습니다"}
    ]
    assert "추천 제한: 3초보다 짧아 추천하지 않습니다" in island.document()["reason"]


def test_short_review_piece_next_to_green_does_not_inherit_recommended():
    """0.3초 조각이 녹색 구간에 붙어도(또는 안에 있어도) 추천을 물려받지 않는다."""
    doc = make_document(30, [(0, 15, "recommended"), (15, 30, "usable")], chatter(0, 30))
    report = tiering.classify(doc, settings=V5_RULES)
    assert layout(report) == [(F(0), F(15), G), (F(15), F(30), Y)]
    # 녹색 0.2초 + 노랑 0.1초 — v2는 가장 많이 겹친 녹색으로 보았다.
    piece = (F(148, 10), F(151, 10))
    v2 = tiering.classify(doc, settings=V2_RULES)
    assert tiering.segment_keyword(v2, *piece) == "추천"
    recommendation, tags = tiering.segment_grade(report, *piece)
    assert recommendation == "normal" and tags[0] == "등급: 보통"
    assert "추천 제한: 3초보다 짧아 추천하지 않습니다" in tags
    assert tags[-1] == "등급판 raw-tier/v6"
    assert tiering.segment_keyword(report, *piece) == "보통"
    # 녹색 안의 0.3초도, 2.99초도 보통이다 — 정확히 3초부터 추천이다.
    assert tiering.segment_keyword(report, F(5), F(53, 10)) == "보통"
    assert tiering.segment_keyword(report, F(5), F(799, 100)) == "보통"
    assert tiering.segment_keyword(report, F(5), F(8)) == "추천"
    assert tiering.segment_grade(report, F(5), F(8))[0] == "recommended"
    assert not any(t.startswith("추천 제한") for t in tiering.segment_grade(report, F(5), F(8))[1])


def test_short_piece_over_red_keeps_its_tier():
    """최소 3초는 추천에만 — 짧은 빨강 조각은 여전히 제외 제안이다."""
    doc = make_document(
        30, [(0, 15, "usable"), (15, 30, "review")], [(0, 2, TALK), (28, 30, TALK + " 끝")]
    )
    report = tiering.classify(doc)
    reds = [(s.start, s.end) for s in report.spans if s.tier == R]
    assert reds
    start, _end = reds[0]
    assert tiering.segment_keyword(report, start + 1, start + F(13, 10)) == "제외 제안"


# ── 규칙 3 — 자막 시각 빨강이면 보통까지 ──────────────────────────────────


@pytest.mark.parametrize(
    ("level", "capped"),
    [
        (sync_check.LEVEL_RED, True),
        (sync_check.LEVEL_AMBER, False),
        (sync_check.LEVEL_GREEN, False),
        (sync_check.LEVEL_SKIP, False),
        (None, False),
    ],
)
def test_timing_red_caps_green_at_yellow_and_nothing_else_does(level: str | None, capped: bool):
    doc = make_document(30, [(0, 15, "recommended"), (15, 30, "usable")], chatter(0, 30))
    report = tiering.classify(doc, timing=tiering.TimingSignals(level=level))
    first = report.spans[0]
    if capped:
        assert (first.tier, first.rule) == (Y, "both-good")
        assert first.caps == ((tiering.CAP_TIMING_RED, TIMING_RED_TEXT),)
        tags = tiering.segment_grade(report, F(1), F(14))[1]
        assert tags[0] == "등급: 보통"
        assert any(tag.startswith("추천 제한: 자막 시각 확인이 빨강이라") for tag in tags)
        assert tiering.segment_keyword(report, F(1), F(14)) == "보통"
    else:
        assert (first.tier, first.caps) == (G, ())
        assert tiering.segment_keyword(report, F(1), F(14)) == "추천"


def test_both_caps_are_named_when_both_apply():
    report = tiering.classify(
        _green_island(F(2)), timing=tiering.TimingSignals(level=sync_check.LEVEL_RED)
    )
    island = report.spans[1]
    assert [code for code, _ in island.caps] == [tiering.CAP_TOO_SHORT, tiering.CAP_TIMING_RED]


def test_classification_stays_deterministic_and_tiles_with_signals():
    rng = random.Random(7)
    for _ in range(50):
        doc = _random_document(rng)
        timing = _signals(doc, rng, sync_check.LEVEL_RED)
        first = tiering.classify(doc, timing=timing)
        assert first == tiering.classify(doc, timing=timing)
        spans = first.spans
        assert spans[0].start == 0 and spans[-1].end == tiering.extent_of(doc)
        assert all(a.end == b.start for a, b in zip(spans, spans[1:], strict=False))
        assert all(s.tier != G for s in spans)  # 빨강 판정이면 녹색이 하나도 없다


def test_classify_takes_no_measurement_and_no_io():
    """D52 그대로 — 받는 것은 문서와 값뿐이다(PCM·말소리 칸·파일 없음)."""
    with pytest.raises(TypeError):
        tiering.classify(make_document(10, [], []), speech_track=())  # type: ignore[call-arg]


# ── 규칙 5 — 화면 문제가 있으면 보통까지(20b-final) ─────────────────────────


def _issue_document(issues: list[str] | None, *, second: list[str] | None = None) -> dict:
    """0–15초 추천 창(말 많음) + 15–30초 사용 가능 창 — 첫 창에 `visual_issues`를 단다."""
    doc = make_document(30, [(0, 15, "recommended"), (15, 30, "usable")], chatter(0, 30))
    if issues is not None:
        doc["annotations"][0]["visual_issues"] = issues
    if second is not None:
        doc["annotations"][1]["visual_issues"] = second
    return doc


ISSUE_TEXT = "화면 문제({})가 있어 보통까지만 추천합니다"


@pytest.mark.parametrize(
    ("issue", "label"),
    [("blur", "흐림"), ("shake", "흔들림"), ("dark", "어두움"),
     ("overexposed", "과노출"), ("obstructed", "가림")],
)  # fmt: skip
def test_each_visual_issue_caps_green_at_yellow_with_a_reason(issue: str, label: str):
    plain = tiering.classify(_issue_document(None), settings=V5_RULES)
    report = tiering.classify(_issue_document([issue]), settings=V5_RULES)
    assert layout(plain)[0] == (F(0), F(15), G)
    first = report.spans[0]
    assert (first.start, first.end, first.tier, first.rule) == (F(0), F(15), Y, "both-good")
    before = plain.spans[0].video
    assert (first.video.status, first.video.windows) == (before.status, before.windows)
    assert first.video.reason == f"{before.reason} · 화면 문제: {label}"  # 영상 판정은 그대로
    assert first.caps == ((tiering.CAP_VISUAL_ISSUES, ISSUE_TEXT.format(label)),)
    assert first.recommendation == "normal" and not first.hold
    assert f"추천 제한: {ISSUE_TEXT.format(label)}" in first.document()["reason"]
    recommendation, tags = tiering.segment_grade(report, F(1), F(14))
    assert recommendation == "normal" and tags[0] == "등급: 보통"
    assert f"추천 제한: {ISSUE_TEXT.format(label)}" in tags
    assert tags[-1] == "등급판 raw-tier/v6"
    assert tiering.segment_keyword(report, F(1), F(14)) == "보통"
    assert layout(report)[1:] == layout(plain)[1:]


@pytest.mark.parametrize("issues", [None, [], ["none"]])
def test_none_missing_and_unknown_issues_leave_green_alone(issues: list[str] | None):
    report = tiering.classify(_issue_document(issues))
    assert report.spans == tiering.classify(_issue_document(None)).spans
    assert (report.spans[0].tier, report.spans[0].caps) == (G, ())
    assert tiering.segment_keyword(report, F(1), F(14)) == "추천"


def test_issues_on_a_failed_or_frameless_window_do_not_cap():
    """받아들이지 않은 판정의 칸은 보지 않는다 — 그 창은 이미 판정 보류다(D51)."""
    doc = _issue_document(["blur"])
    doc["annotations"][0]["status"] = "rejected"
    report = tiering.classify(doc)
    assert all(code != tiering.CAP_VISUAL_ISSUES for s in report.spans for code, _ in s.caps)


def test_issue_labels_follow_the_fixed_order_once_each_across_windows():
    """두 창이 한 녹색 구간을 덮으면 두 창의 문제를 정해진 순서로 한 번씩 적는다."""
    doc = make_document(30, [(0, 15, "recommended"), (15, 30, "recommended")], chatter(0, 30))
    doc["annotations"][0]["visual_issues"] = ["obstructed", "blur"]
    doc["annotations"][1]["visual_issues"] = ["blur", "shake"]
    (span,) = tiering.classify(doc, settings=V5_RULES).spans
    assert span.caps == ((tiering.CAP_VISUAL_ISSUES, ISSUE_TEXT.format("흐림·흔들림·가림")),)


def test_a_window_touching_green_only_through_an_absorbed_sliver_does_not_cap_it():
    """1초보다 짧은 조각을 흡수한 녹색은 이웃 창과 조금만 겹친다 — 그 창의 문제로 내리지 않는다.

    `_significant_spans`와 같은 바닥(`min_span_seconds`, 구간이 더 짧으면 그 길이)이다.
    """
    utterances = [*chatter(0, 15), (F(15), F(154, 10), TALK)]
    doc = make_document(40, [(0, 15, "recommended"), (15, 40, "usable")], utterances)
    plain = tiering.classify(doc, settings=V5_RULES)
    assert (plain.spans[0].start, plain.spans[0].end, plain.spans[0].tier) == (F(0), F(157, 10), G)
    doc["annotations"][1]["visual_issues"] = ["blur"]
    flagged = tiering.classify(doc, settings=V5_RULES)
    # 이웃 빨강의 영상 이유 「화면 문제: 흐림」은 표시라 다르다 — 녹색과 구간 배치는 같다.
    assert flagged.spans[0] == plain.spans[0]
    assert layout(flagged) == layout(plain)
    doc["annotations"][0]["visual_issues"] = ["shake"]
    capped = tiering.classify(doc, settings=V5_RULES).spans[0]
    assert capped.caps == ((tiering.CAP_VISUAL_ISSUES, ISSUE_TEXT.format("흔들림")),)


def test_issue_cap_comes_after_the_other_caps():
    report = tiering.classify(
        _green_island(F(2)), timing=tiering.TimingSignals(level=sync_check.LEVEL_RED)
    )
    doc = _green_island(F(2))
    doc["annotations"][1]["visual_issues"] = ["dark"]
    capped = tiering.classify(doc, timing=tiering.TimingSignals(level=sync_check.LEVEL_RED))
    assert [code for code, _ in capped.spans[1].caps] == [
        tiering.CAP_TOO_SHORT,
        tiering.CAP_TIMING_RED,
        tiering.CAP_VISUAL_ISSUES,
    ]
    assert layout(capped) == layout(report)


def test_silent_span_under_an_issue_window_keeps_its_tier_and_never_turns_red():
    """자막 없는 긴 공백 × 추천 화면(풍경)은 노랑이다 — 화면 문제가 있어도 빨강이 되지 않는다.

    `combine`과 영상 판정을 바꾸지 않았기 때문이다: 화면 문제로 영상을 `usable`로 내렸다면
    「자막 없는 공백 × 사용 가능」이 빨강(새 제외 제안)이 되었을 것이다.
    """
    doc = make_document(
        30, [(0, 30, "recommended")], [(0, 2, TALK), (15, 17, TALK + " 다시"), (28, 30, TALK)]
    )
    plain = tiering.classify(doc, settings=V5_RULES)
    silent = [
        (s.start, s.end, s.tier) for s in plain.spans if s.audio.status == tiering.AUDIO_SILENT
    ]
    assert silent and all(tier == Y for *_, tier in silent)
    for issues in (["blur"], ["dark", "obstructed"], ["none"], []):
        doc["annotations"][0]["visual_issues"] = issues
        flagged = tiering.classify(doc, settings=V5_RULES)
        assert [
            (s.start, s.end, s.tier)
            for s in flagged.spans
            if s.audio.status == tiering.AUDIO_SILENT
        ] == silent
        assert tiering.red_ranges(flagged) == tiering.red_ranges(plain) == ()
        assert json.dumps(tiering.red_decisions(flagged)) == json.dumps(
            tiering.red_decisions(plain)
        )


def test_red_decisions_are_identical_with_and_without_visual_issues():
    """빨강이 있는 문서(공백 × 사용 가능·검토) — 어느 창에 어떤 문제를 달아도 제외 후보가 같다."""
    doc = make_document(
        40,
        [(0, 12, "recommended"), (12, 26, "usable"), (26, 40, "review")],
        [(0, 4, TALK), (4, 7, TALK + " 둘"), (24, 26, TALK), (38, 40, TALK)],
    )
    plain = tiering.classify(doc)
    assert tiering.red_ranges(plain)
    for picked in ISSUE_CHOICES:
        flagged = json.loads(json.dumps(doc))
        for row in flagged["annotations"]:
            if picked is not None:
                row["visual_issues"] = picked
        report = tiering.classify(flagged)
        assert json.dumps(tiering.red_decisions(report)) == json.dumps(tiering.red_decisions(plain))
        assert [(s.start, s.end) for s in report.spans] == [(s.start, s.end) for s in plain.spans]
