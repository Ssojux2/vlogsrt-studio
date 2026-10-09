"""자막 시각 확인의 순수 계산(D55) — 합성 말소리·자막으로 판정 계약을 못 박는다.

`+`는 자막이 말보다 이르다는 뜻이다. 측정 불가(skip)는 결코 초록이 아니다. 판정은
표시만 하고, '말소리를 못 찾음'이라고만 말한다 — '말이 아니다'라고 말하지 않는다.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from fractions import Fraction

import pytest

from vlogsrt.project import sync_check
from vlogsrt.project.config import (
    SYNC_CHECK_SETTINGS,
    ProjectConfigError,
    SyncCheckSettings,
    sync_check_settings_sha256,
    validate_sync_check_settings,
)
from vlogsrt.project.sync_check import Cue, SpeechTrack

HOP = Fraction(512, 16000)
SETTINGS = SYNC_CHECK_SETTINGS


def bursts(seconds: int, seed: int = 7) -> list[tuple[Fraction, Fraction]]:
    """말 덩어리 — 0.8–2.5초 말, 0.6–2.5초 쉼(실제 브이로그 발화의 모양)."""
    rng = random.Random(seed)
    spans: list[tuple[Fraction, Fraction]] = []
    at = Fraction(1)
    while at < seconds - 4:
        length = Fraction(round(rng.uniform(0.8, 2.5) * 1000), 1000)
        spans.append((at, at + length))
        at += length + Fraction(round(rng.uniform(0.6, 2.5) * 1000), 1000)
    return spans


def track(spans: list[tuple[Fraction, Fraction]], seconds: int) -> SpeechTrack:
    count = -(-seconds * 16000 // 512)
    runs = sync_check.merge_runs((int(start / HOP), -(-end // HOP)) for start, end in spans)
    return SpeechTrack(HOP, count, Fraction(seconds), sync_check.clip_runs(runs, 0, count))


def cues_from(spans: list[tuple[Fraction, Fraction]], shift: Fraction = Fraction(0)) -> list[Cue]:
    """말 덩어리마다 자막 하나 — `shift`만큼 **이르게**(+) 놓는다."""
    return [
        Cue(f"c{index}", start - shift, end - shift, f"자막 {index}")
        for index, (start, end) in enumerate(spans)
    ]


def verdict_of(cues: list[Cue], speech: SpeechTrack) -> sync_check.Verdict:
    return sync_check.judge(sync_check.measure(cues, speech, SETTINGS), SETTINGS)


# ─────────────────────────────────────────────────────────────
#  판정
# ─────────────────────────────────────────────────────────────


def test_aligned_subtitles_are_green() -> None:
    spans = bursts(300)
    speech = track(spans, 300)
    metrics = sync_check.measure(cues_from(spans), speech, SETTINGS)
    verdict = sync_check.judge(metrics, SETTINGS)
    assert verdict.level == sync_check.LEVEL_GREEN, verdict.reasons
    assert metrics.precision is not None and metrics.precision > Fraction(95, 100)
    assert metrics.recall is not None and metrics.recall > Fraction(95, 100)
    assert not metrics.flagged


@pytest.mark.parametrize("shift", ["0.7", "1", "2", "-1.5", "5"])
def test_shifted_subtitles_are_red_with_the_right_offset(shift: str) -> None:
    """어긋남은 ±0.1초 안으로 찾고, 0.7초 이상이면 빨강이다 — 늦은 쪽(−)도 같다."""
    spans = bursts(300)
    truth = Fraction(shift)
    metrics = sync_check.measure(cues_from(spans, truth), track(spans, 300), SETTINGS)
    assert metrics.offset.counted, metrics.offset
    assert metrics.offset.seconds is not None
    assert abs(metrics.offset.seconds - truth) <= Fraction(1, 10)
    verdict = sync_check.judge(metrics, SETTINGS)
    assert verdict.level == sync_check.LEVEL_RED
    assert verdict.reasons[0].code == "offset"
    assert ("빠름" if truth > 0 else "늦음") in verdict.reasons[0].text


def test_small_shift_stays_green() -> None:
    spans = bursts(300)
    verdict = verdict_of(cues_from(spans, Fraction(3, 10)), track(spans, 300))
    assert verdict.level == sync_check.LEVEL_GREEN, verdict.reasons


def test_continuous_speech_offset_is_not_measurable_never_a_pass() -> None:
    """말이 쉼 없이 이어지면 어디로 밀어도 겹침이 같다 — 어긋남 검사는 통과가 아니다."""
    spans = bursts(300)
    speech = SpeechTrack(HOP, -(-300 * 16000 // 512), Fraction(300), ((0, -(-300 * 16000 // 512)),))
    metrics = sync_check.measure(cues_from(spans, Fraction(2)), speech, SETTINGS)
    assert not metrics.offset.counted
    offset_check = next(
        c for c in sync_check.metric_checks(metrics, SETTINGS) if "어긋남" in c.name
    )
    assert offset_check.status == sync_check.STATUS_SKIP


def test_drift_one_percent_is_amber_and_point_two_percent_is_not() -> None:
    """가운데를 축으로 1% 늘인 자막 — 머리·꼬리가 반대로 밀려 폭이 벌어진다."""
    spans = bursts(600)
    speech = track(spans, 600)
    middle = Fraction(300)

    def stretched(factor: Fraction) -> list[Cue]:
        return [
            Cue(f"c{i}", middle + (start - middle) * factor, middle + (end - middle) * factor, "x")
            for i, (start, end) in enumerate(spans)
        ]

    metrics = sync_check.measure(stretched(Fraction(101, 100)), speech, SETTINGS)
    assert metrics.drift is not None and metrics.drift >= SETTINGS.amber_drift_seconds
    verdict = sync_check.judge(metrics, SETTINGS)
    assert verdict.level == sync_check.LEVEL_AMBER, verdict.reasons
    assert "drift" in {reason.code for reason in verdict.reasons}
    gentle = sync_check.measure(stretched(Fraction(1002, 1000)), speech, SETTINGS)
    assert gentle.drift is None or gentle.drift < SETTINGS.amber_drift_seconds
    assert sync_check.judge(gentle, SETTINGS).level == sync_check.LEVEL_GREEN


def test_silence_or_no_cues_is_skip_never_green() -> None:
    spans = bursts(120)
    silent = SpeechTrack(HOP, 3750, Fraction(120), ())
    assert verdict_of(cues_from(spans), silent).level == sync_check.LEVEL_SKIP
    no_cues = verdict_of([], track(spans, 120))
    assert no_cues.level == sync_check.LEVEL_SKIP
    assert any("잴 수 없음" in reason.text for reason in no_cues.reasons)
    two = verdict_of(cues_from(spans)[:2], track(spans, 120))
    assert two.level == sync_check.LEVEL_SKIP


def test_blockers_force_skip_even_when_metrics_look_fine() -> None:
    spans = bursts(300)
    metrics = sync_check.measure(cues_from(spans), track(spans, 300), SETTINGS)
    blocker = sync_check.Reason("vad_unavailable", sync_check.LEVEL_SKIP, "VAD 런타임이 없다")
    verdict = sync_check.judge(metrics, SETTINGS, blockers=(blocker,))
    assert verdict.level == sync_check.LEVEL_SKIP and verdict.reasons == (blocker,)


def test_shift_beyond_the_search_range_is_not_a_pass() -> None:
    """찾는 폭(±10초) 밖의 어긋남 — 어긋남 검사가 통과로 보이면 안 되고 판정도 초록이 아니다."""
    spans = bursts(300)
    metrics = sync_check.measure(cues_from(spans, Fraction(15)), track(spans, 300), SETTINGS)
    verdict = sync_check.judge(metrics, SETTINGS)
    assert verdict.level != sync_check.LEVEL_GREEN
    assert not (
        metrics.offset.counted
        and metrics.offset.seconds is not None
        and abs(metrics.offset.seconds) < SETTINGS.red_offset_seconds
    )


def test_edge_peak_is_not_counted() -> None:
    curve_peak = sync_check._estimate([0, 1, 2, 3, 9], 4, 2, 10, HOP, SETTINGS)
    assert curve_peak.status == sync_check.SHIFT_EDGE and not curve_peak.counted


def test_cues_past_either_end_are_clipped_not_lost() -> None:
    """PCM 앞뒤로 삐져나온 큐 — 칸은 잘리지만 큐는 그대로 세고 표지를 받는다."""
    spans = bursts(60)
    speech = track(spans, 60)
    edge = [
        Cue("before", Fraction(-1, 2), Fraction(3, 10), "앞"),
        *cues_from(spans),
        Cue("after", Fraction(59), Fraction(65), "뒤"),
    ]
    runs = sync_check.cue_runs(edge, HOP, speech.frame_count)
    assert runs[0][0] == 0 and runs[-1][1] == speech.frame_count
    metrics = sync_check.measure(edge, speech, SETTINGS)
    assert metrics.cue_count == len(edge)
    assert "after" in {item.cue.cue_id for item in metrics.flagged}


def test_overlap_curve_matches_brute_force() -> None:
    rng = random.Random(3)
    for _ in range(25):
        count = 400
        speech = sync_check.speech_runs(rng.random() < 0.4 for _ in range(count))
        cues = sync_check.speech_runs(rng.random() < 0.3 for _ in range(count))
        max_lag = rng.randint(1, 60)
        mask_s = [sync_check.contains(speech, t) for t in range(count)]
        mask_c = [sync_check.contains(cues, t) for t in range(count)]
        brute = [
            sum(1 for t in range(count) if mask_c[t] and 0 <= t + k < count and mask_s[t + k])
            for k in range(-max_lag, max_lag + 1)
        ]
        assert sync_check.overlap_curve(speech, cues, max_lag) == brute


def test_no_speech_wording_never_says_it_is_not_speech() -> None:
    spans = bursts(300)
    shifted = cues_from(spans) + [
        Cue(f"silent{i}", Fraction(1000 + i * 5), Fraction(1002 + i * 5), "x") for i in range(60)
    ]
    speech = track(spans, 1400)
    verdict = verdict_of(shifted, speech)
    texts = " ".join(reason.text for reason in verdict.reasons)
    assert "못 찾음" in texts and "측정 한계일 수 있음" in texts
    assert "말이 아니" not in texts and "말이 아님" not in texts
    assert sync_check.FLAG_NO_SPEECH in {
        flag
        for item in sync_check.measure(shifted, speech, SETTINGS).flagged
        for flag in item.flags
    }


def test_timestamp_fingerprint_never_changes_the_verdict() -> None:
    spans = bursts(300)
    metrics = sync_check.measure(cues_from(spans, Fraction(1)), track(spans, 300), SETTINGS)
    for whole, glued in ((Fraction(1), Fraction(1)), (Fraction(0), Fraction(0)), (None, None)):
        changed = replace(metrics, whole_second_share=whole, glued_share=glued)
        assert (
            sync_check.judge(changed, SETTINGS).level == sync_check.judge(metrics, SETTINGS).level
        )


def test_settings_are_validated_and_fingerprinted() -> None:
    validate_sync_check_settings(SETTINGS)
    with pytest.raises(ProjectConfigError):
        validate_sync_check_settings(SyncCheckSettings(red_precision_below=Fraction(8, 10)))
    with pytest.raises(ProjectConfigError):
        validate_sync_check_settings(SyncCheckSettings(shift_search_seconds=Fraction(0)))
    with pytest.raises(ProjectConfigError):
        validate_sync_check_settings(SyncCheckSettings(min_cues=0))
    assert sync_check_settings_sha256(SETTINGS) == sync_check_settings_sha256(SyncCheckSettings())
    other = SyncCheckSettings(amber_drift_seconds=Fraction(2))
    assert sync_check_settings_sha256(other) != sync_check_settings_sha256(SETTINGS)


def test_report_json_is_stable_with_exact_fractions() -> None:
    spans = bursts(120)
    metrics = sync_check.measure(cues_from(spans, Fraction(1)), track(spans, 120), SETTINGS)
    verdict = sync_check.judge(metrics, SETTINGS)
    document = {
        "metrics": sync_check.metrics_document(metrics),
        "cues": sync_check.flagged_document(metrics.flagged),
        **sync_check.verdict_document(verdict),
    }
    first = sync_check.report_json(document)
    assert first == sync_check.report_json(json.loads(first))
    loaded = json.loads(first)
    assert Fraction(loaded["metrics"]["precision"]) == metrics.precision
    assert Fraction(loaded["metrics"]["duration"]) == Fraction(120)
    assert "/" in loaded["metrics"]["offset"]["seconds"]


def test_envelope_alignment_finds_lag_and_identity() -> None:
    rng = random.Random(5)
    reference = [rng.random() for _ in range(400)]
    assert sync_check.envelope_alignment(reference, reference, 20) == (0, 1.0)
    shifted = [0.0] * 7 + reference[:-7]
    lag, score = sync_check.envelope_alignment(reference, shifted, 20)
    assert lag == 7 and score > 0.99
    data = bytes([0x10, 0x00, 0xF0, 0xFF] * 160)
    assert sync_check.pcm_envelope(data, 2, 160) == [16.0]


def test_peak_ties_break_toward_zero_lag() -> None:
    """같은 최고점이 여럿이면 0초에 가장 가까운 지연 — 어긋남을 크게 말하지 않는다."""
    assert sync_check.peak_index([5, 0, 5, 0, 5], 2) == 2
    assert sync_check.peak_index([5, 5, 0, 1, 0], 2) == 1
    assert sync_check.peak_index([0, 1, 0, 5, 5], 2) == 3
    assert sync_check.peak_index([9, 0, 1, 0, 5], 2) == 0  # 뚜렷한 최고점은 그대로
