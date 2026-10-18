"""3단 등급 — 자막·영상을 따로 판정하고 표 하나로 합친다(W-11·W-12, D51·D52). 순수 계층 시험.

입력은 AI 판정(Luna 창)과 생성 자막뿐이다 — 로컬 측정(PCM RMS·프레임 화질)은 없다(D52).
합성 문서(`tests/fixtures/synthetic/tier_source.analysis_transcript.json`)는 실제 분석 기록의
키 모양을 따른다. 60초 · 창 4개(15초: 추천·사용 가능·검토·추천이지만 프레임 없음) · 발화 5개 ·
두 번째 ASR 청크(45–60초) 실패. 유료 호출 0회.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.analysis_config import (
    TIER_SETTINGS,
    RawTierSettings,
    validate_tier_settings,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "synthetic"
    / "tier_source.analysis_transcript.json"
)
F = Fraction
SILENT_A = (F(143, 10), F(257, 10))  # 14→26초 자막 공백에서 발화 쪽 0.3초씩 남긴 범위
SILENT_B = (F(273, 10), F(397, 10))  # 27→40초 자막 공백


def document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def evidence(doc: dict | None = None, **kwargs) -> tiering.SourceEvidence:
    return tiering.evidence_of(doc or document(), **kwargs)


def report(doc: dict | None = None, **kwargs) -> tiering.TierReport:
    return tiering.classify(doc or document(), **kwargs)


def layout(result: tiering.TierReport) -> list[tuple[Fraction, Fraction, str, str]]:
    return [(s.start, s.end, s.tier, s.rule) for s in result.spans]


# ── 자막 ─────────────────────────────────────────────────────────────────


def test_audio_speech_density_splits_dense_and_sparse():
    ev = evidence()
    dense = tiering.judge_audio(ev, F(0), SILENT_A[0])
    assert dense.status == tiering.AUDIO_SPEECH_DENSE
    assert dense.density == F(11) / F(143, 10)
    assert dense.evidence == ("utterance_000000", "utterance_000001", "utterance_000002")
    sparse = tiering.judge_audio(ev, SILENT_A[1], F(30))
    assert sparse.status == tiering.AUDIO_SPEECH_SPARSE
    assert sparse.density == F(1) / F(43, 10)
    # 경계값은 녹색 쪽이다(≥).
    at_threshold = evidence(settings=replace(TIER_SETTINGS, speech_density_green=0.25))
    assert tiering.judge_audio(at_threshold, F(25), F(29)).status == tiering.AUDIO_SPEECH_DENSE
    # 짧은 쉼(4–5초·9–10초)은 구간을 자르지 않는다 — 밀도에만 들어간다.
    points = tiering.atomic_boundaries(document())
    assert F(4) not in points and F(9) not in points and F(1) not in points
    assert SILENT_A[0] in points and F(22) in points  # 검증 장면 후보도 자리다


def test_audio_silence_is_a_long_subtitle_gap_only():
    ev = evidence()
    assert ev.silent == (SILENT_A, SILENT_B)
    silent = tiering.judge_audio(ev, F(16), F(22))
    assert silent.status == tiering.AUDIO_SILENT
    assert silent.evidence == ("subtitle_gap:143/10-257/10",)
    assert tiering.AUDIO_LABELS[silent.status] == "자막 없음(긴 공백)"
    assert "dBFS" not in silent.reason
    assert tiering.judge_audio(ev, F(31), F(39)).status == tiering.AUDIO_SILENT
    # 기준을 늘리면 그 공백은 무음 후보조차 아니다 — 발화 없음으로만 남는다.
    long_only = replace(TIER_SETTINGS, silence_min_seconds=12.5)
    assert SILENT_A not in tiering.silent_gaps(document(), long_only)
    judged = tiering.judge_audio(evidence(settings=long_only), F(16), F(22))
    assert judged.status == tiering.AUDIO_UNCONFIRMED
    assert tiering.AUDIO_LABELS[judged.status] == "발화 없음"


def test_audio_failed_chunk_gap_is_never_silence():
    doc = document()
    # 41.3–60초 공백은 실패 청크에 닿아 무음 후보가 아니다.
    assert tiering.silent_gaps(doc) == (SILENT_A, SILENT_B)
    judged = tiering.judge_audio(evidence(doc), F(45), F(60))
    assert judged.status == tiering.AUDIO_FAILED
    assert judged.evidence == ("asr:chunk-0001",)
    assert tiering.judge_audio(evidence(doc), F(413, 10), F(45)).status == (
        tiering.AUDIO_UNCONFIRMED
    )
    assert all(s.tier != tiering.TIER_RED for s in report(doc).spans if s.start >= 40)


def test_audio_without_subtitles_yet_is_pending_and_never_red():
    doc = document()
    # 진행 문서(Luna가 도는 동안) — 자막 칸(audio·asr)이 아직 없다.
    progress = {k: doc[k] for k in ("windows", "utterances", "annotations")}
    assert tiering.silent_gaps(progress) == ()
    assert tiering.judge_audio(evidence(progress), F(16), F(22)).status == tiering.AUDIO_PENDING
    spans = tiering.classify(progress).spans
    assert all(s.tier != tiering.TIER_RED for s in spans)
    assert {s.audio.status for s in spans} <= {
        tiering.AUDIO_SPEECH_DENSE,
        tiering.AUDIO_SPEECH_SPARSE,
        tiering.AUDIO_PENDING,
    }
    # 오디오 트랙이 없는 원본은 무음이 아니라 소리 없음이다.
    absent = {**doc, "audio": {"status": "audio_absent"}, "utterances": []}
    assert tiering.judge_audio(evidence(absent), F(16), F(22)).status == tiering.AUDIO_ABSENT
    assert all(s.tier != tiering.TIER_RED for s in tiering.classify(absent).spans)


def test_classify_refuses_local_measurements():
    """D52 — 공백 RMS·프레임 화질 같은 로컬 측정 입력 자체가 없다."""
    for measurement in ({"gap_levels": {}}, {"local_video": {}}):
        with pytest.raises(TypeError):
            tiering.classify(document(), **measurement)
        with pytest.raises(TypeError):
            tiering.evidence_of(document(), **measurement)
    fields = {field.name for field in dataclasses.fields(tiering.SourceEvidence)}
    assert not fields & {"gaps", "local_video"}
    assert "rms_dbfs" not in {field.name for field in dataclasses.fields(tiering.AudioJudgment)}
    assert not hasattr(tiering, "report_from_document")
    assert "defect" not in tiering.VIDEO_STATUSES


# ── 영상 ─────────────────────────────────────────────────────────────────


def test_video_recommended_with_uncertainties_is_good_not_flagged():
    judged = tiering.judge_video(evidence(), F(0), F(10))
    assert judged.status == tiering.VIDEO_GOOD
    assert "확인 메모: 원거리 인물 식별 불확실" in judged.reason
    assert "Luna 검토 요청" in judged.reason
    assert tiering.judge_video(evidence(), F(31), F(39)).status == tiering.VIDEO_FLAGGED


def test_video_missing_frames_is_unknown_not_defect():
    ev = evidence()
    missing = tiering.judge_video(ev, F(46), F(59))
    assert missing.status == tiering.VIDEO_NO_FRAMES
    assert missing.reason == "프레임 증거 없음 · 재분석 전까지 판정 보류"
    tail = report().spans[-1]
    assert (tail.tier, tail.hold, tail.end) == (tiering.TIER_YELLOW, True, F(60))
    # Luna를 부르지 않은 '프레임 없음' 실패 판정도 결함이 아니라 모름이다.
    skipped = document()
    skipped["annotations"][3].update(
        status="failed", error_code="frames_missing", defect="프레임 추출 실패"
    )
    judged = tiering.judge_video(evidence(skipped), F(46), F(59))
    assert judged.status == tiering.VIDEO_NO_FRAMES
    # 진행 문서처럼 프레임 칸이 없으면 '모름'이다 — 프레임 없음으로 내리지 않는다.
    progress = {k: v for k, v in document().items() if k != "frames"}
    assert tiering.judge_video(evidence(progress), F(46), F(59)).status == tiering.VIDEO_GOOD


def test_video_rejected_annotation_is_failed():
    doc = document()
    doc["annotations"][1].update(status="rejected", defect="JSON이 아니다: 합성")
    judged = tiering.judge_video(evidence(doc), F(16), F(20))
    assert (judged.status, judged.reason) == (tiering.VIDEO_FAILED, "JSON이 아니다: 합성")
    doc["annotations"] = doc["annotations"][:1]
    assert tiering.judge_video(evidence(doc), F(16), F(20)).status == tiering.VIDEO_UNANALYZED
    doc["annotations"][0]["suitability_evaluated"] = False
    assert tiering.judge_video(evidence(doc), F(1), F(2)).status == tiering.VIDEO_UNANALYZED


def test_video_worst_window_wins_inside_a_span():
    ev = evidence()
    spanning = tiering.judge_video(ev, F(10), F(20))
    assert spanning.status == tiering.VIDEO_USABLE
    assert spanning.windows == ("window_000000", "window_000001")
    assert tiering.judge_video(ev, F(10), F(50)).status == tiering.VIDEO_NO_FRAMES


# ── 합치기 ───────────────────────────────────────────────────────────────

G, Y, R = tiering.TIER_GREEN, tiering.TIER_YELLOW, tiering.TIER_RED
UNKNOWN = ("no_frames", "failed", "unanalyzed")
TABLE = {
    #                    good   usable flagged unknown
    "speech_dense": (G, Y, Y, Y),
    "speech_sparse": (Y, Y, Y, Y),
    "subtitle_silence": (Y, R, R, Y),
    "unconfirmed_empty": (Y, Y, Y, Y),
    "analysis_failed": (Y, Y, Y, Y),
    "audio_absent": (G, Y, Y, Y),
    "subtitle_pending": (Y, Y, Y, Y),
}


def test_combination_table_covers_every_status():
    assert set(TABLE) == set(tiering.AUDIO_STATUSES)
    assert {"good", "usable", "flagged", *UNKNOWN} == set(tiering.VIDEO_STATUSES)


@pytest.mark.parametrize("audio_status", list(TABLE))
@pytest.mark.parametrize("column", range(4))
def test_combination_table(audio_status: str, column: int):
    audio = tiering.AudioJudgment(audio_status, F(0), "a", ())
    videos = (["good"], ["usable"], ["flagged"], list(UNKNOWN))[column]
    for video_status in videos:
        video = tiering.VideoJudgment(video_status, "v", (), ())
        tier, rule, red = tiering.combine(audio, video)
        assert tier == TABLE[audio_status][column], (audio_status, video_status)
        if tier == R:
            assert (rule, red) == ("silent-not-scenery", tiering.RED_SILENCE)
        else:
            assert red is None
        if (audio_status, video_status) == ("speech_dense", "good"):
            assert rule == "both-good"
        if (audio_status, video_status) == ("audio_absent", "good"):
            assert rule == "video-only-good"
        if (audio_status, video_status) == ("subtitle_silence", "good"):
            assert rule == "scenery-no-speech"
        if tier == Y and audio_status == "analysis_failed":
            assert rule == "audio-failed"
        elif tier == Y and video_status in UNKNOWN:
            assert rule == "video-unknown"
        elif tier == Y and audio_status == "subtitle_pending":
            assert rule == "audio-pending"
        span = tiering.TierSpan(F(0), F(1), tier, rule, audio, video, red)
        assert span.hold == (
            tier == Y and (video_status in UNKNOWN or audio_status == "analysis_failed")
        )


def test_scenery_without_speech_is_yellow_and_only_ai_doubt_makes_silence_red():
    """자막 없는 긴 공백은 Luna가 풍경으로 본 화면이면 노랑, 의심한 화면이면 빨강이다(D52)."""
    doc = document()
    doc["annotations"][1]["suitability"] = "recommended"
    by_start = {s.start: s for s in report(doc).spans}
    scenery = by_start[SILENT_A[0]]
    assert (scenery.tier, scenery.rule, scenery.end) == (Y, "scenery-no-speech", SILENT_A[1])
    assert scenery.audio.status == tiering.AUDIO_SILENT and scenery.video.status == "good"
    assert not tiering.red_decisions(replace(report(doc), spans=(scenery,)))
    # 판정이 거부된 창·판정이 없는 창·프레임 없는 창은 모름이다 — 빨강이 아니라 보류다.
    rejected = document()
    rejected["annotations"][1].update(status="rejected", defect="JSON이 아니다: 합성")
    removed = document()
    removed["annotations"] = [
        a for a in removed["annotations"] if a["window_id"] != "window_000001"
    ]
    frameless = document()
    frameless["annotations"][1].update(status="failed", error_code="frames_missing")
    for unknown in (rejected, removed, frameless):
        held = next(s for s in report(unknown).spans if s.start == 15)
        assert (held.tier, held.hold, held.rule) == (Y, True, "video-unknown")
        assert held.audio.status == tiering.AUDIO_SILENT


def test_fixture_layout_reads_both_sides():
    result = report()
    assert layout(result) == [
        (F(0), F(15), G, "both-good"),  # 14.3–15초(0.7초) 풍경 자투리는 앞 녹색에 흡수된다
        (F(15), SILENT_A[1], R, "silent-not-scenery"),
        (SILENT_A[1], SILENT_B[0], Y, "video-usable"),
        (SILENT_B[0], F(30), R, "silent-not-scenery"),
        (F(30), SILENT_B[1], R, "silent-not-scenery"),
        (SILENT_B[1], F(413, 10), Y, "video-flagged"),
        (F(413, 10), F(45), Y, "speech-unconfirmed"),
        (F(45), F(60), Y, "audio-failed"),
    ]
    assert [s.hold for s in result.spans] == [False] * 7 + [True]


def test_spans_tile_extent_without_gaps_or_overlaps():
    for settings in (TIER_SETTINGS, replace(TIER_SETTINGS, silence_min_seconds=2.0)):
        spans = report(settings=settings).spans
        assert spans[0].start == 0 and spans[-1].end == 60
        assert all(
            a.end == b.start and a.start < a.end for a, b in zip(spans, spans[1:], strict=False)
        )
    assert tiering.classify({"windows": {"extent": {"num": 0, "den": 1}}}).spans == ()


def test_merge_adjacent_same_tier_and_absorb_short_fragments_red_never_grows():
    # 14.3–15초(0.7초, 창0)는 노랑 풍경이고 앞이 녹색이라 녹색으로 흡수된다 — 빨강은 자라지 않는다.
    red = [(s.start, s.end) for s in report().spans if s.tier == R]
    assert red[0] == (F(15), SILENT_A[1])
    # 두 빨강 사이의 0.9초 발화 구역은 어느 빨강에도 먹히지 않는다.
    doc = document()
    doc["utterances"].insert(
        3,
        {
            "utterance_id": "utterance_000099",
            "start": {"num": 20, "den": 1},
            "end": {"num": 203, "den": 10},
            "text": "응",
        },
    )
    split = report(doc).spans
    reds = [(s.start, s.end) for s in split if s.tier == R and s.end <= SILENT_A[1]]
    assert reds == [(F(15), F(197, 10)), (F(206, 10), SILENT_A[1])]
    middle = next(s for s in split if s.start == F(197, 10))
    assert (middle.end, middle.tier) == (F(206, 10), Y)
    # 빨강이 아닌 짧은 조각은 앞 이웃으로, 첫 조각은 뒤 이웃으로 간다.
    first = report(doc).spans[0]
    assert first.start == 0 and first.end - first.start >= 1


def test_short_red_without_red_neighbour_is_demoted_not_proposed():
    """D52 — 긴 공백이 Luna 창 경계를 1초 못 되게 넘어 생긴 빨강 자투리는 제안하지 않는다.

    창0을 '사용 가능', 창1을 '추천'으로 바꾸면 공백 A(14.3–25.7초)의 14.3–15초(0.7초)만 풍경 아닌
    화면 위에 놓인다. 붙을 빨강 이웃이 없으니 노랑으로 내려 앞 이웃에 흡수된다 — 빨강은 자라지
    않고, 미검토 제외(출력을 막는다)가 0.7초짜리로 생기지 않는다.
    """
    doc = document()
    doc["annotations"][0]["suitability"] = "usable"
    doc["annotations"][1]["suitability"] = "recommended"
    result = report(doc)
    assert layout(result)[:2] == [
        (F(0), F(15), Y, "video-usable"),
        (F(15), SILENT_A[1], Y, "scenery-no-speech"),
    ]
    assert all(s.end - s.start >= 1 for s in result.spans if s.tier == R)
    assert [(r["start"], r["end"]) for r in tiering.red_decisions(result)] == [
        ({"num": 30, "den": 1}, {"num": 397, "den": 10}),
    ]
    # 빨강 이웃이 있는 짧은 빨강은 그 빨강에 붙는다 — 빨강 넓이는 그대로다.
    doc = document()
    doc["windows"]["windows"][1]["core_end"] = {"num": 277, "den": 10}
    doc["windows"]["windows"][2]["core_start"] = {"num": 277, "den": 10}
    reds = [(s.start, s.end) for s in report(doc).spans if s.tier == R]
    assert (SILENT_B[0], SILENT_B[1]) in reds  # 27.3–27.7초(0.4초, 사용 가능)가 뒤 빨강에 붙었다


def test_report_is_deterministic_and_embeds_settings():
    first, second = report(), report()
    assert first == second and first.document() == second.document()
    assert first.document()["settings"] == asdict(TIER_SETTINGS)
    assert first.tier_version == "raw-tier/v5" and len(first.document_sha256) == 64
    assert set(first.document()) == {"tier_version", "settings", "document_sha256", "spans"}
    # 공백 기준을 두 공백(12초·13초)보다 길게 잡으면 빨강이 없다.
    strict = report(settings=replace(TIER_SETTINGS, silence_min_seconds=13.5))
    assert all(s.tier != R for s in strict.spans)
    assert strict.document()["settings"]["silence_min_seconds"] == 13.5
    changed = document()
    changed["utterances"][0]["text"] = "다른 말"
    assert report(changed).document_sha256 != first.document_sha256
    assert report(document_sha256="ab" * 32).document_sha256 == "ab" * 32


def test_red_decisions_shape_matches_auto_decisions():
    rows = tiering.red_decisions(report())
    assert len(rows) == 3
    row = rows[0]
    assert set(row) == {"start", "end", "reason", "evidence", "version"}
    assert row["start"] == {"num": 15, "den": 1} and row["end"] == {"num": 257, "den": 10}
    assert row["reason"] == tiering.RED_SILENCE and row["version"] == "raw-tier/v5"
    assert row["evidence"][:3] == [
        "tier:silent-not-scenery",
        "audio:subtitle_silence",
        "video:usable",
    ]
    assert "subtitle_gap:143/10-257/10" in row["evidence"]
    assert "window:window_000001" in row["evidence"]
    assert {r["reason"] for r in rows} == {tiering.RED_SILENCE}
    assert rows[2]["evidence"][:3] == [
        "tier:silent-not-scenery",
        "audio:subtitle_silence",
        "video:flagged",
    ]
    assert not any(e.startswith("pcm:") for r in rows for e in r["evidence"])


def test_segment_grade_maps_to_closed_recommendation_set():
    result = report()
    closed = {"recommended", "normal", "review_required"}
    green = tiering.segment_grade(result, F(1), F(14))
    assert green[0] == "recommended"
    assert green[1][0] == "등급: 추천" and green[1][-1] == "등급판 raw-tier/v5"
    assert green[1][1].startswith("자막: 발화가 구간의") and green[1][2].startswith("영상: ")
    red = tiering.segment_grade(result, F(16), F(25))
    assert red == ("normal", red[1]) and red[1][0] == "등급: 제외 제안"
    assert red[1][1].startswith("자막: 발화 없음 · 자막 없는 공백")
    mixed = tiering.segment_grade(result, F(10), F(20))
    assert mixed[0] == "normal" and mixed[1][0] == "등급: 보통"
    failed = tiering.segment_grade(result, F(46), F(50))
    assert failed[0] == "review_required" and failed[1][0] == "등급: 보통"
    for a, b in ((0, 60), (30, 40), (59, 60)):
        assert tiering.segment_grade(result, F(a), F(b))[0] in closed
    assert tiering.strip_grade_tags(("Luna 평가: x", *green[1], "실내")) == ("실내",)


def test_segment_keyword_names_the_same_tier_as_segment_grade():
    """Final Cut 키워드(W-16)는 검토 조각 등급과 한 규칙이다 — 둘이 갈라질 수 없다."""
    result = report()
    assert tiering.HOLD_LABEL == "판정 보류"
    assert tiering.segment_keyword(result, F(1), F(14)) == "추천"
    assert tiering.segment_keyword(result, F(16), F(25)) == "제외 제안"
    assert tiering.segment_keyword(result, F(10), F(20)) == "보통"
    # 45–60초는 전사 실패(보류)뿐이다 — 노랑이지만 근거가 없어 보통이 아니다.
    assert tiering.segment_keyword(result, F(46), F(50)) == "판정 보류"
    # 보류와 판정된 노랑이 섞이면 판정이 있는 쪽이다 — 전부 보류일 때만 보류다.
    assert tiering.segment_keyword(result, F(42), F(50)) == "보통"
    tier_word = {f"등급: {label}": label for label in tiering.TIER_LABELS.values()}
    for a in range(0, 60, 3):
        for b in range(a + 1, 61, 7):
            keyword = tiering.segment_keyword(result, F(a), F(b))
            grade = tier_word[tiering.segment_grade(result, F(a), F(b))[1][0]]
            assert keyword == grade or (keyword, grade) == (tiering.HOLD_LABEL, "보통")


def test_segment_keyword_without_any_span_is_hold():
    empty = tiering.classify({"windows": {"extent": {"num": 0, "den": 1}}})
    assert tiering.segment_keyword(empty, F(0), F(1)) == tiering.HOLD_LABEL
    assert tiering.segment_grade(empty, F(0), F(1))[0] == "review_required"


def test_unmaterialized_red_counts_spans_without_frame_edges():
    ranges = tiering.red_ranges(report())
    frame = F(1, 25)
    # 15 · 25.7→25.68 · 27.3→27.32 · 30 · 39.7→39.68
    snapped = {F(15), F(642, 25), F(683, 25), F(30), F(992, 25)}
    assert tiering.unmaterialized_red(ranges, snapped, frame) == 0
    assert tiering.unmaterialized_red(ranges, snapped - {F(683, 25)}, frame) == 1
    assert tiering.unmaterialized_red(ranges, set(), frame) == 3


@pytest.mark.parametrize(
    "settings",
    [
        RawTierSettings(silence_min_seconds=0.5, min_span_seconds=1.0),
        RawTierSettings(min_span_seconds=0.0),
        RawTierSettings(speech_handle_seconds=2.5),
        RawTierSettings(speech_handle_seconds=-0.1),
        RawTierSettings(speech_density_green=0.0),
        RawTierSettings(speech_density_green=1.5),
        RawTierSettings(silence_min_seconds=float("nan")),
        RawTierSettings(tier_version=" "),
    ],
)
def test_tier_settings_reject_contract_violations(settings: RawTierSettings):
    with pytest.raises(ValueError):
        validate_tier_settings(settings)
    with pytest.raises(ValueError):
        tiering.classify(document(), settings=settings)


def test_default_tier_settings_hold_only_subtitle_and_span_values():
    """D52 — 등급 설정에는 자막 공백·발화 밀도·구간 길이만 있다(로컬 측정 값 없음).

    D60이 넷을 더했다 — 모두 생성 자막의 글자·시각과 구간 길이에서 오는 값이다(같은 말 런 ·
    느린 추임새 속도·길이 · 추천 최소 길이). D65가 하나를 더했다 — 원본 자막 「확인 필요」 줄의
    몫(자막의 글자에서 오는 값). 측정 값은 여전히 없다.
    """
    assert {field.name for field in dataclasses.fields(RawTierSettings)} == {
        "tier_version",
        "silence_min_seconds",
        "speech_handle_seconds",
        "min_span_seconds",
        "speech_density_green",
        "same_text_run_min",
        "slow_filler_chars_per_second",
        "slow_filler_min_seconds",
        "recommended_min_seconds",
        "subtitle_verified_suspect_share",
    }


def test_default_tier_settings_are_valid_and_unchanged():
    validate_tier_settings(TIER_SETTINGS)
    assert TIER_SETTINGS.tier_version == "raw-tier/v5"
    assert TIER_SETTINGS.speech_density_green == 0.35
    assert (TIER_SETTINGS.silence_min_seconds, TIER_SETTINGS.speech_handle_seconds) == (5.0, 0.3)
    assert TIER_SETTINGS.min_span_seconds == 1.0


def test_document_is_not_mutated_by_classification():
    doc = document()
    before = copy.deepcopy(doc)
    tiering.classify(doc)
    assert doc == before
