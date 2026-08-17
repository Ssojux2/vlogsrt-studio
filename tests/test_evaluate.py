"""지표 8종."""

from __future__ import annotations

import pytest

from vlogsrt.config import Config
from vlogsrt.evaluate import (
    boundary_f1,
    char_error_rate,
    cue_start_hit_rate,
    cue_start_mae,
    evaluate_cues,
    flag_precision,
    flag_recall,
    levenshtein,
    proper_noun_accuracy,
    violation_rate,
)

from .conftest import make_cue


def test_levenshtein_basics() -> None:
    assert levenshtein("", "") == 0
    assert levenshtein("가나다", "가나다") == 0
    assert levenshtein("가나다", "가라다") == 1
    assert levenshtein("가나", "가나다") == 1
    assert levenshtein("abc", "") == 3


def test_identical_srt_scores_perfect() -> None:
    cues = [make_cue(1.0, 2.0, "안녕하세요"), make_cue(3.0, 4.0, "반갑습니다", index=2)]
    metrics = evaluate_cues(cues, cues, Config())
    assert metrics.cer == 0.0
    assert metrics.cue_start_mae == 0.0
    assert metrics.boundary_f1 == 1.0
    assert metrics.cue_start_hit_rate == 1.0


def test_cer_ignores_punctuation_and_linebreaks() -> None:
    reference = [make_cue(0.0, 1.0, "오늘은 낮술을 마셨다")]
    hypothesis = [make_cue(0.0, 1.0, "오늘은, 낮술을 마셨다!")]
    assert char_error_rate(hypothesis, reference) == 0.0


def test_cer_counts_real_substitutions() -> None:
    reference = [make_cue(0.0, 1.0, "블루펭귄")]
    hypothesis = [make_cue(0.0, 1.0, "블루팽귄")]
    assert char_error_rate(hypothesis, reference) == pytest.approx(0.25)


def test_proper_noun_accuracy_scores_only_words_in_reference() -> None:
    reference = [make_cue(0.0, 1.0, "블루펭귄에서 만났다")]
    good = [make_cue(0.0, 1.0, "블루펭귄에서 만났다")]
    bad = [make_cue(0.0, 1.0, "블루팽귄에서 만났다")]
    assert proper_noun_accuracy(good, reference, ["블루펭귄", "없는말"]) == 1.0
    assert proper_noun_accuracy(bad, reference, ["블루펭귄"]) == 0.0


def test_start_mae_and_hit_rate() -> None:
    reference = [make_cue(1.0, 2.0, "가"), make_cue(3.0, 4.0, "나", index=2)]
    hypothesis = [make_cue(1.1, 2.0, "가"), make_cue(3.5, 4.0, "나", index=2)]
    assert cue_start_mae(hypothesis, reference) == pytest.approx(0.3)
    assert cue_start_hit_rate(hypothesis, reference, tol=0.2) == 0.5


def test_boundary_f1_penalises_extra_cues() -> None:
    reference = [make_cue(0.0, 4.0, "한 큐")]
    split = [make_cue(0.0, 2.0, "한"), make_cue(2.0, 4.0, "큐", index=2)]
    score = boundary_f1(split, reference, tol=0.3)
    assert 0.0 < score < 1.0


def test_boundary_f1_is_one_when_identical() -> None:
    cues = [make_cue(0.0, 2.0, "가"), make_cue(2.5, 4.0, "나", index=2)]
    assert boundary_f1(cues, cues, tol=0.3) == 1.0


def test_violation_rate_uses_config_thresholds() -> None:
    cfg = Config()
    clean = make_cue(0.0, 2.0, "짧고 좋은 자막")
    too_fast = make_cue(0.0, 1.0, "가" * 40)
    assert violation_rate([clean], cfg) == 0.0
    assert violation_rate([clean, too_fast], cfg) == 0.5


def test_flag_metrics_are_not_implemented_yet() -> None:
    cues = [make_cue(0.0, 1.0, "가")]
    with pytest.raises(NotImplementedError):
        flag_precision(cues, cues)
    with pytest.raises(NotImplementedError):
        flag_recall(cues, cues)
