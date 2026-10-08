"""3단 등급 — 자막·영상을 따로 판정하고 표 하나로 합친다(W-11, D51). 순수 계층 시험.

합성 문서(`tests/fixtures/synthetic/tier_source.analysis_transcript.json`)는 실제 분석 기록의
키 모양을 따른다. 60초 · 창 4개(15초) · 발화 5개 · 두 번째 ASR 청크(45–60초) 실패 · 마지막 창
프레임 추출 실패. PCM은 시험 안에서 만든다(스테레오 s16le 16kHz). 유료 호출 0회.
"""

from __future__ import annotations

import copy
import json
import math
from array import array
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
from vlogsrt.project.raw.tier_inputs import measure_gap_levels

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "synthetic"
    / "tier_source.analysis_transcript.json"
)
F = Fraction
QUIET_GAP = (F(143, 10), F(257, 10))  # 14→26초 공백에서 발화 쪽 0.3초씩 남긴 범위
AMBIENT_GAP = (F(273, 10), F(397, 10))  # 27→40초 공백
LOUD = 3000  # ≈ −20.8 dBFS
HUSH = 20  # ≈ −64 dBFS


def document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def write_pcm(path: Path, quiet: tuple[tuple[Fraction, Fraction], ...] = (QUIET_GAP,)) -> Path:
    """스테레오 s16le — 기본은 현장음, `quiet` 범위만 아주 작은 소리로 채운다."""
    rate, seconds = 16000, 60
    samples = array("h")
    for index in range(rate * seconds):
        t = F(index, rate)
        level = HUSH if any(a <= t < b for a, b in quiet) else LOUD
        value = level if index % 2 else -level
        samples.extend((value, -value))
    path.write_bytes(samples.tobytes())
    return path


def levels(quiet: float = -60.0, ambient: float = -20.0) -> dict:
    return {QUIET_GAP: quiet, AMBIENT_GAP: ambient}


def evidence(doc: dict | None = None, **kwargs) -> tiering.SourceEvidence:
    kwargs.setdefault("gap_levels", levels())
    return tiering.evidence_of(doc or document(), **kwargs)


def report(doc: dict | None = None, **kwargs) -> tiering.TierReport:
    kwargs.setdefault("gap_levels", levels())
    return tiering.classify(doc or document(), **kwargs)


def layout(result: tiering.TierReport) -> list[tuple[Fraction, Fraction, str, str]]:
    return [(s.start, s.end, s.tier, s.rule) for s in result.spans]


# ── 자막(소리) ───────────────────────────────────────────────────────────


def test_audio_speech_density_splits_dense_and_sparse():
    ev = evidence()
    dense = tiering.judge_audio(ev, F(0), QUIET_GAP[0])
    assert dense.status == tiering.AUDIO_SPEECH_DENSE
    assert dense.density == F(11) / F(143, 10)
    assert dense.evidence == ("utterance_000000", "utterance_000001", "utterance_000002")
    sparse = tiering.judge_audio(ev, QUIET_GAP[1], F(30))
    assert sparse.status == tiering.AUDIO_SPEECH_SPARSE
    assert sparse.density == F(1) / F(43, 10)
    # 경계값은 녹색 쪽이다(≥).
    at_threshold = tiering.evidence_of(
        document(), gap_levels={}, settings=replace(TIER_SETTINGS, speech_density_green=0.25)
    )
    assert tiering.judge_audio(at_threshold, F(25), F(29)).status == tiering.AUDIO_SPEECH_DENSE
    # 짧은 쉼(4–5초·9–10초)은 구간을 자르지 않는다 — 밀도에만 들어간다.
    points = tiering.atomic_boundaries(document(), gap_levels=levels())
    assert F(4) not in points and F(9) not in points and F(1) not in points
    assert QUIET_GAP[0] in points and F(22) in points  # 검증 장면 후보도 자리다


def test_audio_quiet_gap_requires_measured_rms_and_min_length():
    ev = evidence()
    quiet = tiering.judge_audio(ev, F(16), F(22))
    assert quiet.status == tiering.AUDIO_QUIET and quiet.rms_dbfs == -60.0
    assert "pcm:rms=-60.0dBFS" in quiet.evidence
    ambient = tiering.judge_audio(ev, F(31), F(39))
    assert ambient.status == tiering.AUDIO_AMBIENT
    louder = evidence(gap_levels=levels(quiet=-34.9))
    assert tiering.judge_audio(louder, F(16), F(22)).status == tiering.AUDIO_AMBIENT
    exact = evidence(gap_levels=levels(quiet=-35.0))
    assert tiering.judge_audio(exact, F(16), F(22)).status == tiering.AUDIO_QUIET
    # 기준보다 짧은 공백은 조용해도 무음이 아니다.
    short = evidence(gap_levels={(F(16), F(19)): -80.0})
    assert tiering.judge_audio(short, F(16), F(19)).status == tiering.AUDIO_AMBIENT
    # 재지 않은 공백은 발화 없음(미측정)이다.
    unmeasured = evidence(gap_levels={})
    assert tiering.judge_audio(unmeasured, F(16), F(22)).status == tiering.AUDIO_UNCONFIRMED
    # 기준을 늘리면 그 공백은 후보조차 아니다.
    long_only = replace(TIER_SETTINGS, silence_min_seconds=12.5)
    assert QUIET_GAP not in tiering.quiet_gap_candidates(document(), long_only)


def test_audio_failed_chunk_gap_is_never_silence(tmp_path: Path):
    doc = document()
    candidates = tiering.quiet_gap_candidates(doc)
    assert candidates == (QUIET_GAP, AMBIENT_GAP)  # 41.3–60초는 실패 청크에 닿아 재지 않는다
    measured = measure_gap_levels(
        doc, write_pcm(tmp_path / "a.pcm", (QUIET_GAP, (F(41), F(60)))), audio_start=F(0)
    )
    assert set(measured) == {QUIET_GAP, AMBIENT_GAP}
    # 누가 실패 구간을 아주 조용하다고 넘겨도 실패는 실패다.
    forced = {**measured, (F(413, 10), F(60)): -90.0}
    judged = tiering.judge_audio(evidence(gap_levels=forced), F(45), F(60))
    assert judged.status == tiering.AUDIO_FAILED
    assert judged.evidence == ("asr:chunk-0001",)
    result = report(gap_levels=forced)
    assert all(s.tier != tiering.TIER_RED for s in result.spans if s.start >= 45)


def test_audio_without_pcm_is_unmeasured_and_never_red(tmp_path: Path):
    doc = document()
    assert measure_gap_levels(doc, tmp_path / "missing.pcm", audio_start=F(0)) == {}
    for gap_levels, status in ((None, tiering.AUDIO_UNMEASURED), ({}, tiering.AUDIO_UNCONFIRMED)):
        result = report(gap_levels=gap_levels)
        assert all(s.tier != tiering.TIER_RED for s in result.spans)
        assert tiering.judge_audio(evidence(gap_levels=gap_levels), F(16), F(22)).status == status
    # 진행 문서(소리 칸 없음)도 소리 쪽 빨강이 없다.
    progress = {k: doc[k] for k in ("windows", "utterances", "annotations")}
    assert all(s.tier != tiering.TIER_RED for s in tiering.classify(progress, gap_levels={}).spans)


def test_measured_pcm_levels_match_written_signal(tmp_path: Path):
    measured = measure_gap_levels(document(), write_pcm(tmp_path / "a.pcm"), audio_start=F(0))
    assert measured[QUIET_GAP] == pytest.approx(20 * math.log10(HUSH / 32768), abs=0.01)
    assert measured[AMBIENT_GAP] == pytest.approx(20 * math.log10(LOUD / 32768), abs=0.01)
    # 소리가 늦게 시작하면 표본 위치가 그만큼 당겨진다 — 표본 0 이전은 재지 않는다.
    assert measure_gap_levels(document(), tmp_path / "a.pcm", audio_start=F(-1)) == {}


# ── 영상 ─────────────────────────────────────────────────────────────────


def test_video_recommended_with_uncertainties_is_good_not_flagged():
    judged = tiering.judge_video(evidence(), F(0), F(10))
    assert judged.status == tiering.VIDEO_GOOD
    assert "확인 메모: 원거리 인물 식별 불확실" in judged.reason
    assert "Luna 검토 요청" in judged.reason
    assert tiering.judge_video(evidence(), F(31), F(39)).status == tiering.VIDEO_FLAGGED


def test_video_missing_frames_is_unknown_not_defect():
    black = {f"frame_{i:08d}": {"yavg": 2.0, "blur": 0.0} for i in range(8)}
    ev = evidence(local_video=black)
    missing = tiering.judge_video(ev, F(46), F(59))
    assert missing.status == tiering.VIDEO_NO_FRAMES
    assert missing.reason == "프레임 증거 없음 · 재분석 전까지 판정 보류"
    # 있는 프레임이 검으면 그건 결함이다(측정).
    assert tiering.judge_video(ev, F(1), F(2)).status == tiering.VIDEO_DEFECT
    tail = report(local_video=None).spans[-1]
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
    dark = {"frame_00000002": {"yavg": 1.0}, "frame_00000003": {"yavg": 3.0}}
    assert tiering.judge_video(evidence(local_video=dark), F(10), F(50)).status == (
        tiering.VIDEO_DEFECT
    )
    # 3/4 이상이 검어야 결함이다 — 1/2은 아니다.
    half = {"frame_00000002": {"yavg": 1.0}, "frame_00000003": {"yavg": 90.0}}
    assert tiering.judge_video(evidence(local_video=half), F(16), F(20)).status == (
        tiering.VIDEO_USABLE
    )
    blurry = {"frame_00000002": {"yavg": 90.0, "blur": 9.0}}
    on = replace(TIER_SETTINGS, blur_min=8.0)
    assert tiering.judge_video(evidence(local_video=blurry), F(16), F(20)).status == "usable"
    assert tiering.judge_video(evidence(local_video=blurry, settings=on), F(16), F(20)).status == (
        tiering.VIDEO_DEFECT
    )


# ── 합치기 ───────────────────────────────────────────────────────────────

G, Y, R = tiering.TIER_GREEN, tiering.TIER_YELLOW, tiering.TIER_RED
UNKNOWN = ("no_frames", "failed", "unanalyzed")
TABLE = {
    #                    good   usable flagged unknown defect
    "speech_dense": (G, Y, Y, Y, R),
    "speech_sparse": (Y, Y, Y, Y, R),
    "ambient_no_speech": (Y, Y, Y, Y, R),
    "quiet_silence": (R, R, R, R, R),
    "unconfirmed_empty": (Y, Y, Y, Y, R),
    "analysis_failed": (Y, Y, Y, Y, R),
    "audio_absent": (G, Y, Y, Y, R),
    "unmeasured": (Y, Y, Y, Y, R),
}


@pytest.mark.parametrize("audio_status", list(TABLE))
@pytest.mark.parametrize("column", range(5))
def test_combination_table(audio_status: str, column: int):
    audio = tiering.AudioJudgment(audio_status, F(0), None, "a", ())
    videos = (["good"], ["usable"], ["flagged"], list(UNKNOWN), ["defect"])[column]
    for video_status in videos:
        video = tiering.VideoJudgment(video_status, "v", (), ())
        tier, rule, red = tiering.combine(audio, video)
        assert tier == TABLE[audio_status][column], (audio_status, video_status)
        if video_status == "defect":
            assert (rule, red) == ("V-defect", tiering.RED_VIDEO_DEFECT)
        elif audio_status == "quiet_silence":
            assert (rule, red) == ("A-quiet", tiering.RED_SILENCE)
        else:
            assert red is None
        if (audio_status, video_status) == ("speech_dense", "good"):
            assert rule == "both-good"
        if (audio_status, video_status) == ("audio_absent", "good"):
            assert rule == "video-only-good"
        if tier == Y and audio_status == "analysis_failed":
            assert rule == "audio-failed"
        elif tier == Y and video_status in UNKNOWN:
            assert rule == "video-unknown"
        span = tiering.TierSpan(F(0), F(1), tier, rule, audio, video, red)
        assert span.hold == (
            tier == Y and (video_status in UNKNOWN or audio_status == "analysis_failed")
        )


def test_fixture_layout_reads_both_sides():
    assert layout(report()) == [
        (F(0), QUIET_GAP[0], G, "both-good"),
        (QUIET_GAP[0], QUIET_GAP[1], R, "A-quiet"),
        (QUIET_GAP[1], AMBIENT_GAP[0], Y, "video-usable"),
        (AMBIENT_GAP[0], F(30), Y, "scenery-no-speech"),
        (F(30), AMBIENT_GAP[1], Y, "scenery-no-speech"),
        (AMBIENT_GAP[1], F(413, 10), Y, "video-flagged"),
        (F(413, 10), F(45), Y, "speech-unconfirmed"),
        (F(45), F(60), Y, "audio-failed"),
    ]


def test_spans_tile_extent_without_gaps_or_overlaps():
    for gap_levels in (None, {}, levels(), {**levels(), (F(5, 2), F(7, 2)): -90.0}):
        spans = report(gap_levels=gap_levels).spans
        assert spans[0].start == 0 and spans[-1].end == 60
        assert all(
            a.end == b.start and a.start < a.end for a, b in zip(spans, spans[1:], strict=False)
        )
    assert (
        tiering.classify({"windows": {"extent": {"num": 0, "den": 1}}}, gap_levels={}).spans == ()
    )


def test_merge_adjacent_same_tier_and_absorb_short_fragments_red_never_grows():
    # 14.3–15초(0.7초, 창0)는 빨강이고 앞이 녹색이라 뒤의 빨강(창1)으로 흡수된다.
    red = [s for s in report().spans if s.tier == R]
    assert [(s.start, s.end) for s in red] == [QUIET_GAP]
    assert red[0].video.windows == ("window_000000", "window_000001")
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
    left, right = (QUIET_GAP[0], F(197, 10)), (F(206, 10), QUIET_GAP[1])
    split = report(doc, gap_levels={left: -60.0, right: -60.0, AMBIENT_GAP: -20.0}).spans
    reds = [(s.start, s.end) for s in split if s.tier == R]
    assert reds == [left, right]
    middle = next(s for s in split if s.start == left[1])
    assert (middle.end, middle.tier) == (right[0], Y)
    # 빨강이 아닌 짧은 조각은 앞 이웃으로, 첫 조각은 뒤 이웃으로 간다.
    first = report(doc, gap_levels={}).spans[0]
    assert first.start == 0 and first.end - first.start >= 1


def test_report_is_deterministic_and_embeds_settings():
    first, second = report(), report()
    assert first == second and first.document() == second.document()
    assert first.document()["settings"] == asdict(TIER_SETTINGS)
    assert first.tier_version == "raw-tier/v1" and len(first.document_sha256) == 64
    assert tiering.report_from_document(json.loads(json.dumps(first.document()))) == first
    strict = report(settings=replace(TIER_SETTINGS, silence_rms_dbfs=-70.0))
    assert all(s.tier != R for s in strict.spans)
    assert strict.document()["settings"]["silence_rms_dbfs"] == -70.0
    changed = document()
    changed["utterances"][0]["text"] = "다른 말"
    assert report(changed).document_sha256 != first.document_sha256


def test_red_decisions_shape_matches_auto_decisions():
    rows = tiering.red_decisions(report())
    assert len(rows) == 1
    (row,) = rows
    assert set(row) == {"start", "end", "reason", "evidence", "version"}
    assert row["start"] == {"num": 143, "den": 10} and row["end"] == {"num": 257, "den": 10}
    assert row["reason"] == tiering.RED_SILENCE and row["version"] == "raw-tier/v1"
    assert row["evidence"][:3] == ["tier:A-quiet", "audio:quiet_silence", "video:usable"]
    assert "pcm:rms=-60.0dBFS" in row["evidence"]
    dark = {f"frame_{i:08d}": {"yavg": 0.0} for i in range(2, 4)}
    both = tiering.red_decisions(report(local_video=dark))
    assert {r["reason"] for r in both} == {tiering.RED_VIDEO_DEFECT}
    # 무음과 결함이 겹치면 영상이 이기고 근거엔 둘 다 남는다.
    assert both[0]["start"] == {"num": 143, "den": 10}
    assert {"audio:quiet_silence", "video:defect"} <= set(both[0]["evidence"])


def test_segment_grade_maps_to_closed_recommendation_set():
    result = report()
    closed = {"recommended", "normal", "review_required"}
    green = tiering.segment_grade(result, F(1), F(14))
    assert green[0] == "recommended"
    assert green[1][0] == "등급: 추천" and green[1][-1] == "등급판 raw-tier/v1"
    assert green[1][1].startswith("자막: 발화가 구간의") and green[1][2].startswith("영상: ")
    red = tiering.segment_grade(result, F(16), F(25))
    assert red == ("normal", red[1]) and red[1][0] == "등급: 제외 제안"
    mixed = tiering.segment_grade(result, F(10), F(20))
    assert mixed[0] == "normal" and mixed[1][0] == "등급: 보통"
    failed = tiering.segment_grade(result, F(46), F(50))
    assert failed[0] == "review_required" and failed[1][0] == "등급: 보통"
    for a, b in ((0, 60), (30, 40), (59, 60)):
        assert tiering.segment_grade(result, F(a), F(b))[0] in closed
    assert tiering.strip_grade_tags(("Luna 평가: x", *green[1], "실내")) == ("실내",)


def test_unmaterialized_red_counts_spans_without_frame_edges():
    result = report()
    frame = F(1, 25)
    snapped = {F(358, 25), F(642, 25)}  # 14.3초 → 14.32, 25.7초 → 25.68
    assert tiering.unmaterialized_red(tiering.red_ranges(result), snapped, frame) == 0
    assert tiering.unmaterialized_red(tiering.red_ranges(result), {F(358, 25)}, frame) == 1
    assert tiering.unmaterialized_red(tiering.red_ranges(result), set(), frame) == 1


@pytest.mark.parametrize(
    "settings",
    [
        RawTierSettings(silence_min_seconds=0.5, min_span_seconds=1.0),
        RawTierSettings(min_span_seconds=0.0),
        RawTierSettings(speech_handle_seconds=2.5),
        RawTierSettings(silence_rms_dbfs=1.0),
        RawTierSettings(silence_rms_dbfs=-101.0),
        RawTierSettings(speech_density_green=0.0),
        RawTierSettings(speech_density_green=1.5),
        RawTierSettings(black_frame_fraction=0.0),
        RawTierSettings(blur_frame_fraction=1.2),
        RawTierSettings(blur_min=-1.0),
        RawTierSettings(black_luma_max=300.0),
        RawTierSettings(silence_rms_dbfs=float("nan")),
        RawTierSettings(tier_version=" "),
    ],
)
def test_tier_settings_reject_contract_violations(settings: RawTierSettings):
    with pytest.raises(ValueError):
        validate_tier_settings(settings)
    with pytest.raises(ValueError):
        tiering.classify(document(), gap_levels={}, settings=settings)


def test_default_tier_settings_are_valid_and_calibrated():
    validate_tier_settings(TIER_SETTINGS)
    assert TIER_SETTINGS.speech_density_green == 0.35
    assert (TIER_SETTINGS.silence_min_seconds, TIER_SETTINGS.silence_rms_dbfs) == (5.0, -35.0)
    assert TIER_SETTINGS.blur_min == 0.0  # 결함 표본이 없어 끈다(D51)
    assert TIER_SETTINGS.black_luma_max < 36.48  # 실측 정상 프레임 최솟값보다 아래(D51)


def test_document_is_not_mutated_by_classification():
    doc = document()
    before = copy.deepcopy(doc)
    tiering.classify(doc, gap_levels=levels())
    assert doc == before
